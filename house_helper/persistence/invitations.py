"""Telegram invitations: how a provisioned user gets their Telegram identity, once.

The rule that matters most: an invitation names the USER whose Telegram identity it
links, and nothing else decides that. A person who presents a valid invitation and
signs in with Telegram gets THEIR verified `sub` onto the allowlist for the invitation's
user, and for nobody else. Nothing here matches an identity to a user by an email
address, a username, a display name or a Telegram numeric id; nothing creates a user,
merges two users or moves an identity from one user to another. The secret is a
capability (whoever holds it, within its two hours, may link THEIR OWN Telegram account
to that user), so it is single use, short and withdrawable, and it is only ever
handed to the operator who issued it.

  issue_invitation    an operator gives a provisioned, enabled user with no Telegram
                      identity a new invitation. Every earlier outstanding invitation
                      of that user is revoked first (reissuing invalidates).
  revoke_invitations  an operator withdraws the user's invitation that can still be used
                      (an expired one is left as it is).
  verify_invitation   the START of a sign-in: is this (id, secret) a live invitation
                      of a user who can still be linked? Reads only; uses nothing up.
                      This is the one place the secret is checked.
  claim_invitation    the END of a sign-in: the verified `sub` of an ID token is put on
                      the allowlist for the invitation's user, and the invitation is
                      used up, in the caller's transaction. Takes the invitation's
                      id only (see below).

Nothing here talks to Telegram or to HTTP, and nothing here signs anyone in. Linking is
`claim_invitation` followed, in the SAME transaction, by authn.complete_login, which
creates the identity link and the session through the existing, unchanged path (and so a
session still records the identity it was opened through). The web claim endpoint that
does that is a later increment; the contract it must keep is in the docstring of
claim_invitation.

Secrets: the secret is 256 random bits from the operating system's CSPRNG (the same
generator as session tokens); only its SHA-256 hex digest is stored. The raw secret is
returned once, by issue_invitation, and appears in no column, log or exception message of
this module. The invitation's id is a separate random UUID and is not a secret. The
"invitation code" is `<id>.<secret>`: one string an operator can pass on (see
format_invitation_code).

One use: claiming sets consumed_at with a single UPDATE ... WHERE consumed_at IS NULL AND
revoked_at IS NULL AND expires_at > now, so of any number of concurrent claims of one
invitation exactly one gets rowcount 1, and expiry is exclusive (at expires_at the
invitation is over). A revoked or expired invitation is never used again: nothing clears
revoked_at or moves expires_at, and a CHECK keeps consumed_at and revoked_at apart.

Why claim takes no secret. The secret lives in the page's URL fragment, which the browser
never sends; the sign-in itself is a full-page round trip through Telegram, so by the time
the verified `sub` arrives the secret is gone, and storing it for the round trip would
put a raw secret in the database. So the secret is checked at the START (verify_invitation)
and the invitation's ID is what the server remembers for the round trip, in its own
state, bound to the browser like any other login state. claim_invitation therefore trusts
its `invitation_id`: it must come from that server-side state, which exists only because
verify_invitation said yes, and NEVER from a request field. (A revocation or a reissue
between the two still wins: claim re-checks everything, atomically.)

Transactions: none of the functions commits; the caller owns the transaction. The writers
are write-first on SQLite (a deferred BEGIN that reads first and writes second fails with
SQLITE_BUSY_SNAPSHOT when another writer committed in between; authn explains it at
length), and on PostgreSQL they share one lock order: the user's row (FOR NO KEY UPDATE:
it serialises the invitation operations on one user without blocking the inserts that
merely reference the row) before the invitation's row. Reissuing and claiming both take
it, so "the user has no Telegram identity yet" is decided and acted on under one lock:
either the claim wins (and the reissue is refused, the user being linked) or the reissue
does (and the claim finds its invitation revoked). Revoking is one conditional UPDATE and
needs no more: it and a claim contend for one row under the same conditions (neither used,
nor withdrawn, nor expired at `now`), and the loser sees the winner.
"""

import enum
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.allowlist import allow
from house_helper.persistence.authn import hash_secret, new_secret
from house_helper.persistence.identifiers import PROVIDER_TELEGRAM, normalize_identity
from house_helper.persistence.models import AllowlistEntry, IdentityInvitation, User

# How long an invitation may be used after it was issued. Policy, not configuration: the
# issuing function is the only writer of expires_at, and callers cannot choose it.
INVITATION_LIFETIME = timedelta(hours=2)

INVITATION_TOKEN_BYTES = 32  # 256 bits
# token_urlsafe(32) is always 43 characters of the URL-safe alphabet; anything else cannot
# be one of ours and is not even hashed.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
# str(uuid.uuid4()): the only spelling an invitation carries. UUID() alone also accepts
# braces, URN prefixes and missing hyphens, which no invitation of ours has.
_INVITATION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CODE = re.compile(rf"({_INVITATION_ID.pattern})\.({_TOKEN.pattern})")


class InvitationRefusal(str, enum.Enum):
    """Why an invitation cannot be issued. The value is a stable code."""

    UNKNOWN_USER = "unknown_user"
    USER_DISABLED = "user_disabled"
    TELEGRAM_PROVISIONED = "telegram_already_provisioned"


class InvitationRefused(Exception):
    """An invitation cannot be issued for this user. Nothing was changed by the call that
    raised it (the caller rolls back whatever it began)."""

    def __init__(self, reason: InvitationRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class IdentityRefused(Exception):
    """The verified identity cannot be linked: it is already on the allowlist (for this
    user or another; a revoked entry counts, and is never lifted by an invitation), or it
    is not an identity this application stores. Raised, never returned, and (but for one
    race on PostgreSQL, below) BEFORE the invitation is used up; either way the caller's
    rollback leaves the invitation valid for the right person. The message carries nothing
    about the identity or about any user."""


class InvitationState(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    USED = "used"
    REVOKED = "revoked"


@dataclass(frozen=True)
class IssuedInvitation:
    """An invitation just issued. `token` is the only copy of the secret: it is for the
    operator to hand over and is not recoverable from the database afterwards."""

    invitation_id: uuid.UUID
    user_id: uuid.UUID
    token: str = field(repr=False)
    expires_at: datetime
    superseded: int  # how many earlier outstanding invitations this one revoked

    @property
    def code(self) -> str:
        return format_invitation_code(self.invitation_id, self.token)


@dataclass(frozen=True)
class LiveInvitation:
    """What verify_invitation may say: the invitation exists, is live, and its user can be
    linked. Never the secret, and not whose invitation it is."""

    invitation_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class ClaimedInvitation:
    """A claim that succeeded: the identity is on the allowlist for `user_id` and the
    invitation is used up. The caller now signs the identity in (authn.complete_login) in
    the same transaction."""

    invitation_id: uuid.UUID
    user_id: uuid.UUID
    subject: str = field(repr=False)  # the normalized Telegram `sub`


def is_invitation_id(value: object) -> bool:
    return isinstance(value, str) and _INVITATION_ID.fullmatch(value) is not None


def is_invitation_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def format_invitation_code(invitation_id: uuid.UUID, token: str) -> str:
    """The one string that carries an invitation: `<id>.<secret>`."""
    return f"{invitation_id}.{token}"


def parse_invitation_code(code: object) -> Optional[tuple[str, str]]:
    """(id, secret) of a string made by format_invitation_code, or None for anything else
    (surrounding whitespace included: a pasted code is trimmed by the caller)."""
    if not isinstance(code, str):
        return None
    found = _CODE.fullmatch(code)
    return None if found is None else (found.group(1), found.group(2))


def invitation_state(
    *,
    expires_at: datetime,
    consumed_at: Optional[datetime],
    revoked_at: Optional[datetime],
    now: datetime,
) -> InvitationState:
    """What an invitation is at `now`. Used up and withdrawn come before expired, and
    expiry is exclusive: at expires_at it is over."""
    if consumed_at is not None:
        return InvitationState.USED
    if revoked_at is not None:
        return InvitationState.REVOKED
    if expires_at <= now:
        return InvitationState.EXPIRED
    return InvitationState.ACTIVE


def _take_the_write_lock(session: OrmSession) -> None:
    """A write that changes nothing, as the first statement of a transaction that has
    nothing meaningful to write yet (see the module docstring): on SQLite it takes the
    write lock there and then; on PostgreSQL it locks nothing."""
    session.execute(
        sa.update(IdentityInvitation)
        .where(sa.false())
        .values(consumed_at=IdentityInvitation.consumed_at)
        .execution_options(synchronize_session=False)
    )


def _lock_user(session: OrmSession, user_id: uuid.UUID) -> Optional[User]:
    """The user, locked for the rest of the transaction on PostgreSQL (FOR NO KEY UPDATE:
    it conflicts with another such lock but not with the FOR KEY SHARE that an insert of a
    session, a conversation or an allowlist entry of this user takes). SQLite ignores the
    clause."""
    return session.scalar(sa.select(User).where(User.id == user_id).with_for_update(key_share=True))


def _has_telegram_entry(user_id: uuid.UUID) -> sa.ColumnElement:
    return sa.exists().where(
        AllowlistEntry.user_id == user_id, AllowlistEntry.provider == PROVIDER_TELEGRAM
    )


# --- issuing ----------------------------------------------------------------------


def issue_invitation(session: OrmSession, user_id: uuid.UUID, *, now: datetime) -> IssuedInvitation:
    """A new invitation for `user_id`, with every earlier outstanding one revoked.

    InvitationRefused unless the user exists, is not disabled, and has NO Telegram
    allowlist entry. A revoked entry counts: an administrator took that identity away on
    purpose, and an invitation must not be a way around that. (The email entries of the
    user do not matter.) Does not look at conversations, sessions or any other identity.

    The earlier invitations are revoked even if they have expired: an expired one that
    was never used is superseded all the same, and the partial unique index allows only
    one outstanding row per user.
    """
    _take_the_write_lock(session)
    user = _lock_user(session, user_id)
    if user is None:
        raise InvitationRefused(InvitationRefusal.UNKNOWN_USER)
    if user.disabled_at is not None:
        raise InvitationRefused(InvitationRefusal.USER_DISABLED)
    if session.scalar(sa.select(_has_telegram_entry(user.id))):
        raise InvitationRefused(InvitationRefusal.TELEGRAM_PROVISIONED)

    superseded = session.execute(
        sa.update(IdentityInvitation)
        .where(
            IdentityInvitation.user_id == user.id,
            IdentityInvitation.consumed_at.is_(None),
            IdentityInvitation.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount

    token = new_secret(INVITATION_TOKEN_BYTES)
    row = IdentityInvitation(
        user_id=user.id,
        token_hash=hash_secret(token),
        created_at=now,
        expires_at=now + INVITATION_LIFETIME,
    )
    session.add(row)
    session.flush()
    return IssuedInvitation(
        invitation_id=row.id,
        user_id=user.id,
        token=token,
        expires_at=row.expires_at,
        superseded=superseded,
    )


def revoke_invitations(session: OrmSession, user_id: uuid.UUID, *, now: datetime) -> int:
    """Withdraw the user's invitation that can still be used: how many were revoked (0 or
    1). Safe to repeat; a used, an already withdrawn and an expired invitation are not
    touched, and nothing is un-revoked. The end is exclusive, as in claim: at expires_at the
    invitation is over and there is nothing to withdraw. (An expired one that was never used
    stays outstanding in the table's terms until a reissue supersedes it; only issuing,
    which has to clear the way for the one outstanding row, takes it out.) One conditional
    UPDATE, so it is also the first statement."""
    return session.execute(
        sa.update(IdentityInvitation)
        .where(
            IdentityInvitation.user_id == user_id,
            IdentityInvitation.consumed_at.is_(None),
            IdentityInvitation.revoked_at.is_(None),
            IdentityInvitation.expires_at > now,
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount


# --- looking at an invitation -----------------------------------------------------


def _live(now: datetime) -> list[sa.ColumnElement]:
    """The conditions of a live invitation of a user who can still be linked."""
    return [
        IdentityInvitation.consumed_at.is_(None),
        IdentityInvitation.revoked_at.is_(None),
        IdentityInvitation.expires_at > now,
        User.disabled_at.is_(None),
        ~_has_telegram_entry(User.id),
    ]


def verify_invitation(
    session: OrmSession, invitation_id: object, token: object, *, now: datetime
) -> Optional[LiveInvitation]:
    """The invitation `(invitation_id, token)` names, if it is live and its user can still
    be linked; otherwise None, and every reason is alike to the caller (malformed, unknown,
    an id and a secret that are not of the same invitation, used, revoked, expired, its user
    disabled or already linked). Reads only, one statement, and uses nothing up: a flow that
    is started and abandoned costs the invitation nothing but time."""
    if not is_invitation_id(invitation_id) or not is_invitation_token(token):
        return None
    row = session.execute(
        sa.select(IdentityInvitation.id, IdentityInvitation.expires_at)
        .join(User, User.id == IdentityInvitation.user_id)
        .where(
            IdentityInvitation.id == uuid.UUID(invitation_id),
            IdentityInvitation.token_hash == hash_secret(token),
            *_live(now),
        )
    ).first()
    return None if row is None else LiveInvitation(invitation_id=row.id, expires_at=row.expires_at)


# --- claiming ---------------------------------------------------------------------


def claim_invitation(
    session: OrmSession, invitation_id: object, subject: object, *, now: datetime
) -> Optional[ClaimedInvitation]:
    """Put the verified Telegram identity `subject` on the allowlist for the user the
    invitation names, and use the invitation up.

    THE CONTRACT OF THE CALLER (the web claim endpoint, a later increment):

      * `subject` is the `sub` of an ID token that Telegram signed and that the caller has
        fully verified (signature, issuer, audience, expiry, nonce). Nothing here can tell.
      * `invitation_id` comes from server-side state that was created after
        verify_invitation succeeded for this browser, never from a request field.
      * It runs in ONE write transaction with authn.complete_login(session, "telegram",
        claimed.subject, ...), which creates the identity link and the session, and the
        caller checks that the session's user is claimed.user_id, rolling back if it is not.
      * Any exception rolls the whole transaction back; None writes nothing.

    None for an invitation that cannot be used: malformed or unknown id, used, revoked,
    expired, its user disabled or already holding a Telegram identity. All alike; nothing
    was written. IdentityRefused (raised) if the identity cannot be linked; the
    invitation is not used up (or, in the PostgreSQL race below, the rollback un-uses it)
    and is still valid for the right person.

    The user is locked first (see the module docstring), the invitation is claimed by one
    conditional UPDATE, and the allowlist entry is created by allowlist.allow. An identity
    that is already on the allowlist, for any user and revoked or not, is refused and never
    adopted or un-revoked here; on PostgreSQL two claims of the same identity racing past
    that check meet the unique (provider, subject) key, and the loser is refused too.
    """
    if not is_invitation_id(invitation_id):
        return None
    wanted = uuid.UUID(invitation_id)

    _take_the_write_lock(session)
    owner = session.scalar(
        sa.select(IdentityInvitation.user_id).where(IdentityInvitation.id == wanted)
    )
    if owner is None:
        return None
    user = _lock_user(session, owner)
    if user is None:
        return None
    live = session.scalar(
        sa.select(IdentityInvitation.id)
        .join(User, User.id == IdentityInvitation.user_id)
        .where(IdentityInvitation.id == wanted, *_live(now))
    )
    if live is None:
        return None

    try:
        provider, subject = normalize_identity(PROVIDER_TELEGRAM, subject)
    except ValueError:
        raise IdentityRefused from None
    taken = session.scalar(
        sa.select(AllowlistEntry.id).where(
            AllowlistEntry.provider == provider, AllowlistEntry.subject == subject
        )
    )
    if taken is not None:
        raise IdentityRefused

    claimed = session.execute(
        sa.update(IdentityInvitation)
        .where(
            IdentityInvitation.id == wanted,
            IdentityInvitation.consumed_at.is_(None),
            IdentityInvitation.revoked_at.is_(None),
            IdentityInvitation.expires_at > now,
        )
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None  # never true under the lock; the UPDATE is what decides, not the read

    try:
        allow(session, user, provider, subject)
    except (IntegrityError, ValueError):
        raise IdentityRefused from None
    return ClaimedInvitation(invitation_id=wanted, user_id=user.id, subject=subject)
