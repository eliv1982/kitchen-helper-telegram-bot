"""Provisioning the first user and their email sign-in address.

The product has ONE initial user (persistence.allowlist says so), created before they
can sign in anywhere, and each way in is an allowlist entry that belongs to them. This
module creates that user together with an approved email entry, in one transaction,
and nothing else: no Telegram identity (a Telegram `sub` is never guessed, see
README), no second user, no second address, no account merging.

The decision table (every row is a transaction that either commits whole or changes
nothing; "the user" is the only row of `users`):

  no user, address not on the list     create the user and the entry          created
  the address is on the list, active   change nothing                         unchanged
    for an enabled user                (a display name or note that differs is
                                        NOT applied: a repeated run is a no-op)
  the address is on the list, revoked  refuse, unless BOTH --reapprove-revoked
                                       and --verified-mailbox were given: then
                                       lift the revocation on the existing row  reapproved
  the user is disabled                 refuse (this tool never enables a user)
  a user exists, address not on list   refuse (never a second user or address)
  more than one user                   refuse (it would have to choose between them)

The address is whatever persistence.identifiers.normalize_identity makes of it
(trimmed, the DOMAIN lower-cased, the local part exactly as typed, no alias folding),
and the entry is written by persistence.allowlist.allow, unmodified. A reapproval is
`allow` too, so it lifts the revocation on the same row and, like `allow`, does not
bring back the sessions and sign-in links that the revocation ended.

Transaction: the work is one transaction that begins by serialising with every other
provisioning run.
  * SQLite: with a write (a no-op UPDATE), the convention of the persistence layer. The
    engine begins every transaction with a deferred BEGIN, and a transaction that reads
    first and writes second fails with "database is locked" when another writer
    committed in between; a write-first one takes the lock, queues, and then sees
    everything committed before it.
  * PostgreSQL: with a transaction-scoped advisory lock (released at commit or
    rollback, so a crash cannot leave it held) on a key fixed for this command. Without
    it two runs that see an empty `users` table at the same time, each with a different
    spelling of the address (so the unique (provider, subject) index does not stop the
    second), would both create a user. The transaction runs at READ COMMITTED whatever
    the server's default is, so that the read which follows the lock sees the user the
    run before it committed. (A snapshot taken before the wait, as REPEATABLE READ would
    take it, would not.)
The schema revision is checked once more inside this transaction, after the lock. That
narrows the gap between looking and writing; it does not close it. The advisory lock is
taken by this command only: an Alembic migration does not take it, and nothing here
stops a migration from running at the same time. On PostgreSQL a migration's DDL is
invisible until it commits, so a check that runs beside one can pass just before it
commits, and the writes that follow then meet the new schema (or the migration meets
this command's rows). THIS COMMAND AND MIGRATIONS MUST NEVER RUN AT THE SAME TIME: that
is an operating rule (stop the applications, migrate, finish, then provision), not
something the code enforces. The lock serialises provisioning runs with each other,
and that guarantee is the only one made.
"""

import enum
import hashlib
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from house_helper.admin.target import TargetError, check_schema_at_head
from house_helper.persistence.allowlist import allow
from house_helper.persistence.identifiers import (
    PROVIDER_EMAIL,
    is_mailable_address,
    normalize_identity,
)
from house_helper.persistence.models import AllowlistEntry, User

MAX_DISPLAY_NAME_LENGTH: int = User.__table__.c.display_name.type.length
MAX_NOTE_LENGTH: int = AllowlistEntry.__table__.c.note.type.length

# The PostgreSQL advisory lock key: fixed, so that every version of this command takes
# the same lock; derived from a name rather than typed in, and shifted to fit the signed
# 64-bit integer pg_advisory_xact_lock takes. Another application sharing the server
# would have to pick the same 63 bits by accident to wait on it, and would only wait.
ADVISORY_LOCK_KEY: int = (
    int.from_bytes(hashlib.sha256(b"house_helper.admin.provision-email").digest()[:8], "big") >> 1
)


class InvalidInput(ValueError):
    """A command-line value that cannot be used. The message is fixed text: the value
    itself is never echoed (it may not be what the operator thinks it is)."""


class RefusalReason(str, enum.Enum):
    MULTIPLE_USERS = "multiple_users"
    USER_EXISTS = "user_exists"
    USER_DISABLED = "user_disabled"
    EMAIL_REVOKED = "email_revoked"
    IDENTITY_OTHER_USER = "identity_other_user"


class Refusal(Exception):
    """The database is in a state in which this command declines to act. Expected, not
    a failure: nothing was changed."""

    def __init__(self, reason: RefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class Outcome(str, enum.Enum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    REAPPROVED = "reapproved"


@dataclass(frozen=True)
class ProvisionRequest:
    """A validated request. `email` is already normalized (see prepare_request)."""

    email: str
    display_name: Optional[str] = None
    note: Optional[str] = None
    reapprove_revoked: bool = False
    verified_mailbox: bool = False


@dataclass(frozen=True)
class ProvisionResult:
    outcome: Outcome
    user_id: uuid.UUID
    email: str
    # Options the operator gave that did not apply to this run, by flag name.
    ignored: tuple = ()


def prepare_request(
    email: object,
    display_name: Optional[str] = None,
    note: Optional[str] = None,
    *,
    reapprove_revoked: bool = False,
    verified_mailbox: bool = False,
) -> ProvisionRequest:
    """The request for these values, or InvalidInput. Nothing touches a database."""
    try:
        _, address = normalize_identity(PROVIDER_EMAIL, email)
    except ValueError:
        raise InvalidInput(
            "the email address is not valid: printable ASCII, exactly one '@' with text on "
            "both sides, at most 255 characters"
        ) from None
    if not is_mailable_address(address):
        raise InvalidInput(
            "the email address cannot be used for sign-in links: before the '@' only letters, "
            "digits and . _ + - (not at the ends, no '..'), after it a dotted host name. "
            "Approving it would give an entry that no link can ever be sent to"
        )
    return ProvisionRequest(
        email=address,
        display_name=_clean_text(display_name, MAX_DISPLAY_NAME_LENGTH, "the display name"),
        note=_clean_text(note, MAX_NOTE_LENGTH, "the note"),
        reapprove_revoked=reapprove_revoked,
        verified_mailbox=verified_mailbox,
    )


def _clean_text(value: Optional[str], limit: int, what: str) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise InvalidInput(f"{what} must not be empty")
    if len(text) > limit:
        raise InvalidInput(f"{what} is at most {limit} characters")
    # Control, format (zero-width, bidirectional overrides), surrogate, private-use and
    # unassigned characters: what a terminal or a log could be made to misrepresent.
    # (A lone surrogate is what the OS argument decoder leaves for undecodable bytes.)
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise InvalidInput(f"{what} must not contain control or invisible characters")
    return text


def provision_email(factory: sessionmaker, request: ProvisionRequest) -> ProvisionResult:
    """Apply the decision table in one transaction. Refusal if the table says no; any
    other exception propagates; either way the transaction has been rolled back."""
    with factory() as session:
        with session.begin():
            return _provision(session, request)


def _provision(session: OrmSession, request: ProvisionRequest) -> ProvisionResult:
    _serialize(session)
    check_schema_at_head(session.connection())

    users = session.scalars(sa.select(User).order_by(User.created_at, User.id).limit(2)).all()
    if len(users) > 1:
        raise Refusal(
            RefusalReason.MULTIPLE_USERS,
            "this database has more than one user. This command provisions the single initial "
            "user only and never chooses between users.",
        )
    entry = session.scalar(
        sa.select(AllowlistEntry)
        .where(AllowlistEntry.provider == PROVIDER_EMAIL, AllowlistEntry.subject == request.email)
        .with_for_update()  # PostgreSQL: nobody revokes it under us; SQLite ignores the clause
    )

    if entry is None:
        if users:
            raise Refusal(
                RefusalReason.USER_EXISTS,
                "a user already exists and this address is not one of their sign-in addresses. "
                "This command creates the first user only: it never creates a second user or "
                "adds an address to an existing one (the README describes how an administrator "
                "gives an existing user another address).",
            )
        return _create(session, request)

    user = users[0] if users else None
    if user is None or user.id != entry.user_id:
        raise Refusal(
            RefusalReason.IDENTITY_OTHER_USER,
            "this address is on the allowlist for a user that is not the initial user.",
        )
    if user.disabled_at is not None:
        raise Refusal(
            RefusalReason.USER_DISABLED,
            "the user this address belongs to is disabled. This command never enables a user.",
        )
    if entry.revoked_at is None:
        return ProvisionResult(
            Outcome.UNCHANGED,
            user.id,
            request.email,
            _given(request, display_name=True, note=True, reapproval=True),
        )
    if not (request.reapprove_revoked and request.verified_mailbox):
        raise Refusal(
            RefusalReason.EMAIL_REVOKED,
            "this address is revoked and stays revoked. If you have confirmed that this exact "
            "spelling, including the capitals before the '@', is the mailbox that should be "
            "able to sign in, repeat the command with both --reapprove-revoked and "
            "--verified-mailbox.",
        )
    allow(session, user, PROVIDER_EMAIL, request.email, request.note)
    return ProvisionResult(
        Outcome.REAPPROVED, user.id, request.email, _given(request, display_name=True)
    )


def _create(session: OrmSession, request: ProvisionRequest) -> ProvisionResult:
    user = User(display_name=request.display_name)
    session.add(user)
    allow(session, user, PROVIDER_EMAIL, request.email, request.note)  # flushes the user first
    return ProvisionResult(
        Outcome.CREATED, user.id, request.email, _given(request, reapproval=True)
    )


def _given(
    request: ProvisionRequest,
    *,
    display_name: bool = False,
    note: bool = False,
    reapproval: bool = False,
) -> tuple:
    """The flags of `request` that were given but have no effect in this run."""
    ignored = []
    if display_name and request.display_name is not None:
        ignored.append("--display-name")
    if note and request.note is not None:
        ignored.append("--note")
    if reapproval and request.reapprove_revoked:
        ignored.append("--reapprove-revoked")
    if reapproval and request.verified_mailbox:
        ignored.append("--verified-mailbox")
    return tuple(ignored)


def _serialize(session: OrmSession) -> None:
    """The first statement of the transaction: serialise with other provisioning runs
    (see the module docstring)."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.connection(execution_options={"isolation_level": "READ COMMITTED"})
        session.execute(
            sa.select(sa.func.pg_advisory_xact_lock(sa.literal(ADVISORY_LOCK_KEY, sa.BigInteger)))
        )
    elif dialect == "sqlite":
        session.execute(
            sa.update(User)
            .where(sa.false())
            .values(updated_at=User.updated_at)  # (an explicit value: no onupdate default)
            .execution_options(synchronize_session=False)
        )
    else:  # open_target refuses these first; this is the second line of defence
        raise TargetError("unsupported_backend", "this command works on SQLite and PostgreSQL only.")
