"""Emailed sign-in links: issuing one, previewing it, redeeming it, withdrawing it.

The flow, and the one rule that matters most: a link signs in the user who OWNS the
address on the allowlist, and nobody else. Possession of a mailbox proves nothing
about who an internal user is; it is only ever a second way into an account whose
address an administrator has put on the allowlist for that user in advance
(allowlist.allow). Nothing here creates a user, adds an allowlist entry, matches an
address to a user by anything but that entry, or merges two users. An address that
is not on the list, or is revoked, or whose user is disabled, gets no link; and a
link that was issued cannot be used once any of that has become true.

  issue_email_login    a person asks for a link for an address. The answer says
                       whether a link now exists; the caller, which is the only one
                       holding the secret, mails it. Not on the allowlist, revoked,
                       disabled, malformed, or asked for again too soon: no link,
                       and the caller must not let the difference show to the asker.
  preview_email_login  what the confirmation page may say about a link: the masked
                       address, never the secret. Read only; a link is not used up
                       by being looked at (a mail scanner opens links).
  redeem_email_login   the person confirms. Turns a link into a session, through
                       the identity link of the address (authn.complete_login), so
                       the session records exactly that identity as its provenance
                       and is ended with it by allowlist.revoke.
  withdraw_email_login a link that could not be delivered is cancelled.

Secrets: the token is 256 random bits from the operating system's CSPRNG; only its
SHA-256 hex digest is stored (as for sessions: nothing to guess, nothing a slow hash
would protect). The raw token is returned once, by issue_email_login, and appears in
no column, log or exception message of this module. The challenge's id is a separate
random UUID and is not a secret: it names the row for the confirmation page (so the
page can say which address the link is for), but redeeming needs the token too, and
the two must belong to the same row.

One use: redeeming claims the row with a single UPDATE ... WHERE consumed_at IS NULL
AND revoked_at IS NULL AND expires_at > now, so of any number of concurrent attempts
with the same token exactly one gets rowcount 1, and expiry is exclusive (at
expires_at the link is over). A link that is presented while its identity is no
longer allowed is spent all the same and signs nobody in. One case leaves the link
usable: the browser is already signed in as a DIFFERENT user (SignInConflict). That
is raised, never returned, so that the transaction is rolled back and the claim with
it: the person signs out and opens the same link again. A browser is never switched
from one user to another by a link.

Transactions: none of the functions commits; the caller owns the transaction
(web.database.WebDatabase.run is one). The writers (issue, redeem, withdraw) are
write-first on SQLite, for the reason authn gives at length (a deferred BEGIN that
reads first and writes second fails with SQLITE_BUSY_SNAPSHOT when another writer
committed in between). Where there is no meaningful write to start with, they start
with one that changes nothing (_take_the_write_lock): on SQLite it takes the write
lock there and then, on PostgreSQL it locks nothing.

Locks, which matter on PostgreSQL only (SQLite has one writer): every path takes the
allowlist entry's row lock FIRST and the challenge rows after. issue takes it FOR
UPDATE (so two concurrent requests cannot both pass the cooldown, and a request
cannot slip a link past a revoke that is committing); redeem takes it FOR SHARE
(allowed_user(lock=True), like a login: a revoke waits for the redemption, then
ends the session it created, or comes first and the redemption is refused); revoke
takes it for UPDATE and then withdraws the challenges. One order for all of them is
what keeps two transactions from each holding what the other wants. withdraw locks
a single challenge row and nothing else, so it cannot be part of a cycle either.
"""

import enum
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence import authn
from house_helper.persistence.allowlist import allowed_user
from house_helper.persistence.authn import SESSION_LIFETIME, IssuedSession, hash_secret, new_secret
from house_helper.persistence.identifiers import PROVIDER_EMAIL, normalize_identity
from house_helper.persistence.models import AllowlistEntry, EmailLoginChallenge, User

# How long a link may be used after it was issued. Policy, not configuration.
EMAIL_LOGIN_LIFETIME = timedelta(minutes=10)

# The least time between two links for one address. Counted from the last link
# ISSUED, whether or not its email was delivered or the link used: it bounds the
# mail the address receives, and the mail the server tries to send. It must not
# exceed the lifetime, or the evidence of the last link would be purged first.
EMAIL_REQUEST_COOLDOWN = timedelta(seconds=60)

EMAIL_TOKEN_BYTES = 32  # 256 bits
# token_urlsafe(32) is always 43 characters of the URL-safe alphabet; anything else
# cannot be one of ours and is not even hashed.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
# str(uuid.uuid4()): the only spelling the links carry. UUID() alone also accepts
# braces, URN prefixes and missing hyphens, which no link of ours has.
_CHALLENGE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_MIN_LOCAL_PART_TO_SHOW_A_LETTER = 4


class SignInConflict(Exception):
    """The browser that presented a link is signed in as a different user than the
    link's. Raised (not returned) so that the transaction rolls back and the link is
    not used up. The caller must say nothing about the other user."""


class IssueOutcome(str, enum.Enum):
    ISSUED = "issued"
    NOT_ELIGIBLE = "not_eligible"  # malformed, unknown, revoked or disabled
    COOLDOWN = "cooldown"  # a link was issued for this address a moment ago


@dataclass(frozen=True)
class EmailLoginLink:
    """A link just issued. `token` is the only copy of the secret: it goes into the
    email and is not recoverable from the database afterwards."""

    challenge_id: uuid.UUID
    token: str = field(repr=False)
    address: str = field(repr=False)  # the allowlisted address, as stored: where to send it
    expires_at: datetime


@dataclass(frozen=True)
class IssueResult:
    outcome: IssueOutcome
    link: Optional[EmailLoginLink] = None


@dataclass(frozen=True)
class LinkPreview:
    masked_address: str
    expires_at: datetime


def is_challenge_id(value: object) -> bool:
    return isinstance(value, str) and _CHALLENGE_ID.fullmatch(value) is not None


def is_email_login_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def mask_address(address: str) -> str:
    """An address shown without giving it away: "e***@example.com". The domain stays
    (it is what tells a person which mailbox this is); the local part keeps its first
    letter only if it is long enough for that to hide something."""
    local, _, domain = address.partition("@")
    shown = local[:1] if len(local) >= _MIN_LOCAL_PART_TO_SHOW_A_LETTER else ""
    return f"{shown}***@{domain}"


def _take_the_write_lock(session: OrmSession) -> None:
    """A write that changes nothing, as the first statement of a transaction that has
    nothing meaningful to write yet (see the module docstring)."""
    session.execute(
        sa.update(EmailLoginChallenge)
        .where(sa.false())
        .values(consumed_at=EmailLoginChallenge.consumed_at)
        .execution_options(synchronize_session=False)
    )


# --- issuing ----------------------------------------------------------------------


def issue_email_login(
    session: OrmSession,
    address: object,
    *,
    now: datetime,
    lifetime: timedelta = EMAIL_LOGIN_LIFETIME,
    cooldown: timedelta = EMAIL_REQUEST_COOLDOWN,
) -> IssueResult:
    """A new link for `address`, if there is one to issue.

    NOT_ELIGIBLE unless allowed_user finds a user for exactly this address (see the
    module docstring); COOLDOWN if a link was issued for it less than `cooldown`
    ago; otherwise ISSUED with the link. Never raises on its input, so a caller cannot
    turn an exception into a difference the asker sees. The address is normalized the
    way the allowlist stores it (identifiers.normalize_identity: trimmed, the domain
    lower-cased, the local part kept exactly as given, nothing else folded, so
    "Cook@example.com" and "cook+tag@example.com" are other addresses than
    "cook@example.com"); the link's address is the stored one, which is the very
    address an administrator approved, and the mail goes to it, never to a spelling
    of the asker's own choosing.

    Also deletes this identity's expired challenges, which can never be used and
    which only this call removes. That happens after the entry's row lock is taken
    (see the module docstring), so an unknown address costs no write beyond the
    write lock itself.
    """
    _take_the_write_lock(session)
    try:
        provider, subject = normalize_identity(PROVIDER_EMAIL, address)
    except ValueError:
        return IssueResult(IssueOutcome.NOT_ELIGIBLE)
    user = allowed_user(session, provider, subject, lock=True, exclusive=True)
    if user is None:
        return IssueResult(IssueOutcome.NOT_ELIGIBLE)

    session.execute(
        sa.delete(EmailLoginChallenge)
        .where(
            EmailLoginChallenge.user_id == user.id,
            EmailLoginChallenge.provider == provider,
            EmailLoginChallenge.subject == subject,
            EmailLoginChallenge.expires_at <= now,
        )
        .execution_options(synchronize_session=False)
    )
    latest = session.scalar(
        sa.select(sa.func.max(EmailLoginChallenge.created_at)).where(
            EmailLoginChallenge.user_id == user.id,
            EmailLoginChallenge.provider == provider,
            EmailLoginChallenge.subject == subject,
        )
    )
    if latest is not None and latest > now - cooldown:
        return IssueResult(IssueOutcome.COOLDOWN)

    token = new_secret(EMAIL_TOKEN_BYTES)
    challenge = EmailLoginChallenge(
        user_id=user.id,
        provider=provider,
        subject=subject,
        token_hash=hash_secret(token),
        created_at=now,
        expires_at=now + lifetime,
    )
    session.add(challenge)
    session.flush()
    link = EmailLoginLink(
        challenge_id=challenge.id, token=token, address=subject, expires_at=challenge.expires_at
    )
    return IssueResult(IssueOutcome.ISSUED, link)


# --- looking at a link ------------------------------------------------------------


def preview_email_login(
    session: OrmSession, challenge_id: object, *, now: datetime
) -> Optional[LinkPreview]:
    """What may be said about the link `challenge_id` names, or None if it cannot be
    used (unknown, malformed, expired, used, withdrawn, its identity revoked, its
    user disabled: all alike). Reads only, one statement, and does not use the link
    up. The token is neither needed nor looked at: the page that calls this has not
    got it (it is in the address's fragment, which a browser keeps to itself)."""
    if not is_challenge_id(challenge_id):
        return None
    row = session.execute(
        sa.select(
            EmailLoginChallenge.subject,
            EmailLoginChallenge.expires_at,
            EmailLoginChallenge.consumed_at,
            EmailLoginChallenge.revoked_at,
            User.disabled_at,
            AllowlistEntry.revoked_at.label("identity_revoked_at"),
        )
        .select_from(EmailLoginChallenge)
        .join(
            AllowlistEntry,
            sa.and_(
                AllowlistEntry.user_id == EmailLoginChallenge.user_id,
                AllowlistEntry.provider == EmailLoginChallenge.provider,
                AllowlistEntry.subject == EmailLoginChallenge.subject,
            ),
        )
        .join(User, User.id == AllowlistEntry.user_id)
        .where(EmailLoginChallenge.id == uuid.UUID(challenge_id))
    ).first()
    if (
        row is None
        or row.consumed_at is not None
        or row.revoked_at is not None
        or row.disabled_at is not None
        or row.identity_revoked_at is not None
        or row.expires_at <= now
    ):
        return None
    return LinkPreview(masked_address=mask_address(row.subject), expires_at=row.expires_at)


# --- redeeming --------------------------------------------------------------------


def redeem_email_login(
    session: OrmSession,
    challenge_id: object,
    token: object,
    *,
    now: datetime,
    signed_in_user: Optional[uuid.UUID] = None,
    lifetime: timedelta = SESSION_LIFETIME,
) -> Optional[IssuedSession]:
    """Sign in with a link: a new session for the user that owns the address, opened
    through the address's identity link (created on the first sign-in).

    None, with nothing signed in, for anything that is not a live link of ours: a
    malformed or unknown id or token, an id and a token that are not of the same
    link, an expired, used or withdrawn link, an identity that is not allowed any
    more. (A link that was live but whose identity is not allowed is spent: it is
    claimed before it is refused, and the caller commits.) All alike to the caller.

    `signed_in_user` is the user the presenting browser is already signed in as, if
    any. SignInConflict if that is not the link's user; the link is then not used
    up (see the module docstring). The same user is fine: it is a new session
    (the browser's cookie is replaced; the old session is not ended here).
    """
    if not is_challenge_id(challenge_id) or not is_email_login_token(token):
        return None
    wanted = uuid.UUID(challenge_id)
    token_hash = hash_secret(token)

    _take_the_write_lock(session)
    bound = session.execute(
        sa.select(EmailLoginChallenge.user_id, EmailLoginChallenge.subject).where(
            EmailLoginChallenge.id == wanted, EmailLoginChallenge.token_hash == token_hash
        )
    ).first()
    if bound is None:
        return None

    # The entry's lock before the challenge's row (module docstring). Also the
    # allowlist check: this is the one that the login below repeats.
    user = allowed_user(session, PROVIDER_EMAIL, bound.subject, lock=True)
    claimed = session.execute(
        sa.update(EmailLoginChallenge)
        .where(
            EmailLoginChallenge.id == wanted,
            EmailLoginChallenge.token_hash == token_hash,
            EmailLoginChallenge.consumed_at.is_(None),
            EmailLoginChallenge.revoked_at.is_(None),
            EmailLoginChallenge.expires_at > now,
        )
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        return None
    if user is None or user.id != bound.user_id:
        return None  # spent, and nobody is signed in
    if signed_in_user is not None and signed_in_user != user.id:
        raise SignInConflict

    issued = authn.complete_login(session, PROVIDER_EMAIL, bound.subject, now=now, lifetime=lifetime)
    if issued.user_id != bound.user_id:
        # Never true: the login resolves the same entry, whose user the challenge's
        # composite foreign key already pins. Refused anyway, rolled back, because the
        # alternative is to sign in as somebody else.
        raise authn.LoginDenied
    return issued


# --- withdrawing ------------------------------------------------------------------


def withdraw_email_login(session: OrmSession, challenge_id: uuid.UUID, *, now: datetime) -> bool:
    """Cancel a link that was issued but could not be delivered, so that no live
    secret exists for a message nobody got. True if it was live to this call's
    knowledge (neither used nor already withdrawn). Safe to repeat."""
    withdrawn = session.execute(
        sa.update(EmailLoginChallenge)
        .where(
            EmailLoginChallenge.id == challenge_id,
            EmailLoginChallenge.consumed_at.is_(None),
            EmailLoginChallenge.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    return withdrawn.rowcount == 1
