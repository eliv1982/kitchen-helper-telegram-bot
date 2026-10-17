"""Authentication persistence: in-flight OIDC logins, the end of a login, and the
server-side session that follows.

Three things happen here and nowhere else: begin_oidc_login / consume_oidc_login
keep the state of one OIDC authorization round trip, complete_login turns a
provider-vouched identity into a session, and authenticate / revoke_session
resolve and end that session. Nothing here talks to a provider or to HTTP: the
caller passes the `sub` of an ID token it has already verified, the secrets it
generated, and the time ("now": the application's UTC clock, never the
database's, so that expiry is decided in one place and tests can move it).

Secrets: a session token and an OIDC `state` value are stored only as their
SHA-256 hex digest (the tokens are 256-bit random values, so a plain digest is
enough: there is nothing to guess and nothing a slow hash would protect). The
OIDC nonce and PKCE verifier have to be sent back to the provider, so they are
stored as given and live until the login's expires_at. A raw token or state is
never written to a column, a log or an exception message by this module.

Identity: a login resolves to a user only through allowlist.allowed_user, an
exact match on an entry provisioned in advance (never a name, a username or an
unverified claim). The identity link is created from that entry on the first
login: R1's composite foreign key already guarantees that a link exists only for
an allowlist entry of the very user it names, so nothing here can attach an
identity to a different user, or merge two users.

Transactions: none of the functions commits; the caller owns the transaction
(web.database.WebDatabase.run is one). The writers (begin_oidc_login,
consume_oidc_login, complete_login, revoke_session, and allowlist.revoke) all make
a WRITE their first statement. On SQLite the engine begins every transaction with a deferred BEGIN
(persistence.database), and a transaction that reads first and writes second
fails at once with "database is locked" (SQLITE_BUSY_SNAPSHOT) when another
writer committed in between. A transaction that writes first takes the write lock
there and then, waits its turn for it, and sees everything committed before it.
So concurrent logins queue instead of failing, and the get-or-create of the
identity link cannot race on SQLite. (On PostgreSQL there is no such snapshot
conflict; the one race left, two first logins creating the same link, is handled
in complete_login by a savepoint.) tests/test_persistence_authn.py pins the
order of statements.

authenticate is the opposite: it only reads, so validating a session on every
request never takes the write lock and never fails on a stale snapshot.
Consequence, deliberately: sessions.last_seen_at is set when the session is
created and is NOT maintained here. A "last seen" that is written on each
request would turn every authenticated request into a writer. The session
management increment decides whether and how often to touch it.

Provenance: a session records the identity link it was opened through
(sessions.identity_link_id), set by complete_login from the identity it has just
verified and admitted, and tied by the database to a link of the session's own
user. authenticate re-checks on every call that THAT identity is still allowed
(its own allowlist entry is not revoked) and that the user is not disabled. An
unrelated active identity of the same user authorizes nothing: the session is as
good as the way in it came through, no better.

Revocation of access: allowlist.revoke ends an identity's authorization and the
sessions it opened in one transaction, and a revoked session stays revoked when
the identity is allowed again (a person signs in again and gets a new session).
authenticate also reads the entry on every call, so a revocation is enforced the
moment it commits even if the sessions were not marked. A session without
provenance (one that predates the column; migration 0002 revokes them all) never
authenticates, whatever its revoked_at says.

Concurrent login and revocation: a login creates a session only after
allowed_user(lock=True) has said yes. On SQLite the login's first statement is a
write, so it and a revoke (also write-first) run one after the other, and the
later one sees what the earlier committed: a revoke after a login ends the
session; a login after a revoke is refused. On PostgreSQL the same order comes
from the shared row lock on the allowlist entry that lock=True takes: a revoke
waits for a login that has read "allowed" and then ends its session, and a login
that comes after a revoke is refused. Either way no session of a revoked identity
is left unrevoked by a revocation that has committed.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence import models
from house_helper.persistence.allowlist import allowed_user
from house_helper.persistence.identifiers import normalize_identity

# How long a started login may take to come back from the provider.
OIDC_LOGIN_LIFETIME = timedelta(minutes=10)

# How long a session lives. Absolute: it is not extended by use.
SESSION_LIFETIME = timedelta(days=14)

SESSION_TOKEN_BYTES = 32
# token_urlsafe(32) is always 43 characters of the URL-safe alphabet; anything
# else sent as a session cookie cannot be one of ours and is not even hashed.
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_STATE = re.compile(r"[A-Za-z0-9_-]{1,128}")


class LoginDenied(Exception):
    """The identity may not sign in (unknown, revoked, malformed, user disabled).
    The reason is deliberately not part of the exception. complete_login may have
    written before it raised: the caller must roll the transaction back, which
    WebDatabase.run does when the exception propagates out of the function."""


@dataclass(frozen=True)
class OidcLoginSecrets:
    """What a started login remembered for the callback."""

    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)


@dataclass(frozen=True)
class IssuedSession:
    """A session just created. `token` is the only copy of the secret: it goes
    into the cookie and is not recoverable from the database afterwards."""

    token: str = field(repr=False)
    user_id: UUID
    session_id: UUID
    expires_at: datetime


@dataclass(frozen=True)
class AuthenticatedSession:
    """Who a live session belongs to. Plain values, no ORM objects, so it can
    leave the worker thread that resolved it."""

    user_id: UUID
    session_id: UUID
    expires_at: datetime


def new_secret(nbytes: int = SESSION_TOKEN_BYTES) -> str:
    """A URL-safe random secret from the operating system's CSPRNG."""
    return secrets.token_urlsafe(nbytes)


def hash_secret(value: str) -> str:
    """The SHA-256 hex digest under which a token or state is stored."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_session_token(value: object) -> bool:
    """True if `value` has the shape of a token this module issues."""
    return isinstance(value, str) and _SESSION_TOKEN.fullmatch(value) is not None


def is_state_value(value: object) -> bool:
    """True if `value` could be an OIDC state: 1 to 128 URL-safe characters. What
    the application generates is 43; the room is for a state from elsewhere."""
    return isinstance(value, str) and _STATE.fullmatch(value) is not None


# --- an OIDC authorization round trip ---------------------------------------------


def begin_oidc_login(
    session: OrmSession,
    provider: str,
    state: str,
    nonce: str,
    code_verifier: str,
    *,
    now: datetime,
    lifetime: timedelta = OIDC_LOGIN_LIFETIME,
) -> None:
    """Remember a login that is about to be sent to the provider.

    Also deletes the logins that have expired (consumed or not): they can never
    be completed, and this is the only thing that removes them. The delete is the
    first statement, which makes the whole function a write-first transaction.
    """
    session.execute(
        sa.delete(models.OidcState)
        .where(models.OidcState.expires_at <= now)
        .execution_options(synchronize_session=False)
    )
    session.add(
        models.OidcState(
            state_hash=hash_secret(state),
            provider=provider,
            nonce=nonce,
            code_verifier=code_verifier,
            created_at=now,
            expires_at=now + lifetime,
        )
    )
    session.flush()


def consume_oidc_login(
    session: OrmSession, provider: str, state: str, *, now: datetime
) -> Optional[OidcLoginSecrets]:
    """Use up the login that `state` names, once.

    The secrets of the login if `state` belongs to a login of this provider that
    is neither used nor expired, and None for anything else (unknown, malformed,
    already used, expired, another provider's). Use and expiry are decided by one
    UPDATE, so of any number of concurrent calls with the same state exactly one
    gets the secrets. The expiry is exclusive: at expires_at the login is over.
    """
    if not is_state_value(state):
        return None
    state_hash = hash_secret(state)
    claimed = session.execute(
        sa.update(models.OidcState)
        .where(
            models.OidcState.state_hash == state_hash,
            models.OidcState.provider == provider,
            models.OidcState.consumed_at.is_(None),
            models.OidcState.expires_at > now,
        )
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None
    row = session.execute(
        sa.select(models.OidcState.nonce, models.OidcState.code_verifier).where(
            models.OidcState.state_hash == state_hash
        )
    ).one()
    return OidcLoginSecrets(nonce=row.nonce, code_verifier=row.code_verifier)


# --- the end of a login: a session ------------------------------------------------


def complete_login(
    session: OrmSession,
    provider: str,
    subject: str,
    *,
    now: datetime,
    lifetime: timedelta = SESSION_LIFETIME,
) -> IssuedSession:
    """Sign in the identity `(provider, subject)`: a new session for its user.

    `subject` is the `sub` of an ID token the caller has verified, nothing else.
    Raises LoginDenied unless allowlist.allowed_user finds a user for it (see the
    module docstring: roll back when it does). On success: the identity link is
    created if this is the identity's first login (from the allowlist entry that
    exists for it, for the user that entry names), its last_login_at is set, and
    a session is created with a fresh random token and the link it was opened
    through (the identity just verified, never another one of the user's).
    """
    try:
        provider, subject = normalize_identity(provider, subject)
    except ValueError:
        raise LoginDenied from None

    # The first statement is a write (see the module docstring). It matches
    # nothing on a first login, and it is rolled back with everything else if the
    # login is then denied.
    session.execute(
        sa.update(models.IdentityLink)
        .where(models.IdentityLink.provider == provider, models.IdentityLink.subject == subject)
        .values(last_login_at=now)
        .execution_options(synchronize_session=False)
    )
    user = allowed_user(session, provider, subject, lock=True)
    if user is None:
        raise LoginDenied

    link = _link(session, provider, subject)
    if link is None:
        try:
            with session.begin_nested():
                created = models.IdentityLink(
                    user_id=user.id, provider=provider, subject=subject, last_login_at=now
                )
                session.add(created)
                session.flush()
            link = _Link(id=created.id, user_id=user.id)
        except IntegrityError:
            # Another login created it between our look and our insert (possible
            # on PostgreSQL only). The savepoint is rolled back; it is theirs now.
            link = _link(session, provider, subject)
    if link is None or link.user_id != user.id:
        # Never true for a link that exists: the composite foreign key ties a link
        # to the user of its allowlist entry. Refused anyway (and for a link that
        # still cannot be found), because the alternative is to sign in as
        # somebody else.
        raise LoginDenied

    token = new_secret()
    row = models.Session(
        user_id=user.id,
        identity_link_id=link.id,
        token_hash=hash_secret(token),
        created_at=now,
        last_seen_at=now,
        expires_at=now + lifetime,
    )
    session.add(row)
    session.flush()
    return IssuedSession(token=token, user_id=user.id, session_id=row.id, expires_at=row.expires_at)


@dataclass(frozen=True)
class _Link:
    id: UUID
    user_id: UUID


def _link(session: OrmSession, provider: str, subject: str) -> Optional[_Link]:
    row = session.execute(
        sa.select(models.IdentityLink.id, models.IdentityLink.user_id).where(
            models.IdentityLink.provider == provider, models.IdentityLink.subject == subject
        )
    ).first()
    return None if row is None else _Link(id=row.id, user_id=row.user_id)


# --- sessions ---------------------------------------------------------------------


def authenticate(
    session: OrmSession, token: object, *, now: datetime
) -> Optional[AuthenticatedSession]:
    """The session `token` names, if it is live, else None.

    Live: it exists, is not revoked, expires after `now` (at expires_at it is
    over), its user is not disabled, it records the identity link it was opened
    through, and that identity's own allowlist entry is not revoked. No other
    entry of the user counts. Anything else, a malformed token included, is the
    same None. Reads only (one statement); see the module docstring.

    The identity is reached through joins that must all match, so a session with
    no link, or whose link is not of the session's user or has no entry, finds no
    row at all, whatever its other columns say.
    """
    if not is_session_token(token):
        return None
    row = session.execute(
        sa.select(
            models.Session.id,
            models.Session.user_id,
            models.Session.expires_at,
            models.Session.revoked_at,
            models.User.disabled_at,
            models.AllowlistEntry.revoked_at.label("identity_revoked_at"),
        )
        .select_from(models.Session)
        .join(models.User, models.User.id == models.Session.user_id)
        .join(
            models.IdentityLink,
            sa.and_(
                models.IdentityLink.id == models.Session.identity_link_id,
                models.IdentityLink.user_id == models.Session.user_id,
            ),
        )
        .join(
            models.AllowlistEntry,
            sa.and_(
                models.AllowlistEntry.user_id == models.IdentityLink.user_id,
                models.AllowlistEntry.provider == models.IdentityLink.provider,
                models.AllowlistEntry.subject == models.IdentityLink.subject,
            ),
        )
        .where(models.Session.token_hash == hash_secret(token))
    ).first()
    if row is None or row.revoked_at is not None or row.disabled_at is not None:
        return None
    if row.identity_revoked_at is not None or row.expires_at <= now:
        return None
    return AuthenticatedSession(user_id=row.user_id, session_id=row.id, expires_at=row.expires_at)


def revoke_session(session: OrmSession, token: object, *, now: datetime) -> bool:
    """End the session `token` names. True if it was live to this call's
    knowledge (not yet revoked); False for an unknown, malformed or already
    revoked one. Safe to repeat: the first revocation time is kept."""
    if not is_session_token(token):
        return False
    revoked = session.execute(
        sa.update(models.Session)
        .where(models.Session.token_hash == hash_secret(token), models.Session.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    return revoked.rowcount == 1
