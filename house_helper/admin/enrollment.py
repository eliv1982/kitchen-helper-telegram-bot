"""Enrolling the initial users: create a user, give them a Telegram invitation, take it
back, say where things stand.

Onboarding is closed: nobody signs in who an operator did not provision. This module is
how the operator provisions the INITIAL users, who then link their own Telegram account
with a one-time invitation (persistence.invitations; the web endpoint that redeems it is
a later increment). It never creates an allowlist entry, never links an identity and never
signs anybody in: a user created here can sign in nowhere until their invitation has been
claimed with a verified Telegram login. Nothing is matched by an email address, a name, a
username or a Telegram numeric id.

  enroll_user          a NEW user and their first invitation, in one transaction.
  reissue_invitation   a new invitation for an EXISTING user who has no Telegram identity
                       yet (also the first one, for a user that has none: the user that
                       provision-email made, for example). Earlier outstanding ones are
                       revoked in the same transaction.
  revoke_invitation    the user's active invitation is withdrawn (one that has expired is
                       left as it is).
  enrollment_status    a read-only report: no Telegram `sub`, no digest, no secret.

The enrollment limit (ENROLLMENT_USER_CAP, two) is a TEMPORARY POLICY OF THIS COMMAND. It
is not in the schema (no constraint, no trigger) and nothing else reads it: a database can
hold any number of users, conversations are owned by their user exactly as before, and
raising or removing the limit is a change to one constant here. It counts every row of
`users`, disabled ones included, and applies to CREATING a user only: reissuing for a user
that exists is never blocked by it. A user is never deleted, replaced or merged here.

Decision tables (each is one transaction that commits whole or changes nothing):

  enroll_user
    a user with this display name exists (compared case-insensitively)   refuse
         (a repeated command must not spend the second place on a duplicate)
    the users already number ENROLLMENT_USER_CAP or more                 refuse
    otherwise                                                            create the user
                                                                         and the invitation
  reissue_invitation
    no such user / the user is disabled / the user has a Telegram         refuse
    allowlist entry, revoked or not (persistence.invitations says why)
    otherwise                                                            revoke the earlier
                                                                         outstanding, issue
  revoke_invitation
    no such user                                                         refuse
    otherwise                                                            revoke the active
                                                                         invitation (maybe
                                                                         none: unchanged)

Transactions: every mutation is serialised with every other administrative command that
creates users, by the lock provision-email takes (provisioning._serialize, the same
advisory-lock key): on SQLite a write comes first (the persistence layer's convention,
there explained), on PostgreSQL a transaction-scoped advisory lock at READ COMMITTED, so
that the count of users which follows the lock sees every run that committed before it. Two
runs enrolling at the same time therefore cannot both see one free place; and this command
and provision-email cannot both create "the first user". The invitations' own row locks
(persistence.invitations) then serialise a reissue with a web claim. The lock serialises
administrative commands with each other, and a migration is not one: the operating rule
of provisioning.py holds here too (migrations and these commands never run together).
The schema revision is checked once more inside the transaction, after the lock.

The time of a run is read after the lock is held (`now` may be a clock, a function that
returns it, and the command line passes one): runs that queue behind each other then stamp
their rows in the order they took the lock, and a run does not date its invitation by the
moment it began to wait. A datetime is used as it is, which is how tests give one.

What the report says about a user's invitations (enrollment_status): the one that can be used
if there is one (neither used, nor withdrawn, nor expired at the time of the report), and
otherwise the one whose last event, being used, withdrawn or having expired, is the latest.
The rows' created_at is not consulted: concurrent runs, a clock that was set back and rows
written by other code can all leave the newest row with the oldest created_at.

The secret leaves this module once, in the IssuedInvitation that enroll_user and
reissue_invitation return, and the command line prints it once. Nothing here logs.
"""

import enum
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Optional, Union

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from house_helper.admin import provisioning
from house_helper.admin.provisioning import MAX_DISPLAY_NAME_LENGTH, InvalidInput, _clean_text
from house_helper.admin.target import check_schema_at_head
from house_helper.persistence import invitations
from house_helper.persistence.identifiers import PROVIDER_TELEGRAM
from house_helper.persistence.invitations import (
    InvitationRefusal,
    InvitationRefused,
    InvitationState,
    IssuedInvitation,
    invitation_state,
)
from house_helper.persistence.models import AllowlistEntry, IdentityInvitation, User

# The temporary enrollment policy: at most this many users can be CREATED here.
ENROLLMENT_USER_CAP: int = 2

_USER_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class EnrollmentRefusalReason(str, enum.Enum):
    ENROLLMENT_FULL = "enrollment_full"
    DISPLAY_NAME_IN_USE = "display_name_in_use"
    UNKNOWN_USER = InvitationRefusal.UNKNOWN_USER.value
    USER_DISABLED = InvitationRefusal.USER_DISABLED.value
    TELEGRAM_PROVISIONED = InvitationRefusal.TELEGRAM_PROVISIONED.value


class EnrollmentRefusal(Exception):
    """The database is in a state in which the command declines to act. Expected, not a
    failure: nothing was changed."""

    def __init__(self, reason: EnrollmentRefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


_REFUSALS = {
    InvitationRefusal.UNKNOWN_USER: (
        EnrollmentRefusalReason.UNKNOWN_USER,
        "no user has this id. enrollment-status lists the users.",
    ),
    InvitationRefusal.USER_DISABLED: (
        EnrollmentRefusalReason.USER_DISABLED,
        "this user is disabled. This command never enables a user and never invites a "
        "disabled one.",
    ),
    InvitationRefusal.TELEGRAM_PROVISIONED: (
        EnrollmentRefusalReason.TELEGRAM_PROVISIONED,
        "this user already has a Telegram identity (approved or revoked). An invitation links "
        "a FIRST Telegram identity only: taking one away, or replacing it, is not something "
        "this command does.",
    ),
}


def _refusal(why: InvitationRefusal) -> EnrollmentRefusal:
    reason, message = _REFUSALS[why]
    return EnrollmentRefusal(reason, message)


def prepare_display_name(display_name: Optional[str]) -> str:
    """The display name for a new user, or InvalidInput. The same rules as provision-email's
    (trimmed, at most 200 characters, no control or invisible characters), and required: it
    is how an operator tells the users apart. Nothing touches a database."""
    cleaned = _clean_text(display_name, MAX_DISPLAY_NAME_LENGTH, "the display name")
    if cleaned is None:
        raise InvalidInput("the display name is required")
    return cleaned


def prepare_user_id(text: object) -> uuid.UUID:
    """The user id as enrollment-status prints it, or InvalidInput (the text is not echoed)."""
    if not isinstance(text, str) or _USER_ID.fullmatch(text) is None:
        raise InvalidInput(
            "the user id is not valid: it is written as enrollment-status prints it, eight, "
            "four, four, four and twelve hexadecimal digits separated by hyphens"
        )
    return uuid.UUID(text)


# --- the operations ---------------------------------------------------------------


Clock = Union[datetime, Callable[[], datetime]]


def enroll_user(factory: sessionmaker, display_name: str, *, now: Clock) -> IssuedInvitation:
    """A new user named `display_name` and their first invitation, in one transaction.
    EnrollmentRefusal if the table says no; any other exception propagates; either way
    the transaction has been rolled back."""
    with factory() as session:
        with session.begin():
            return _enroll(session, display_name, now)


def reissue_invitation(factory: sessionmaker, user_id: uuid.UUID, *, now: Clock) -> IssuedInvitation:
    """A new invitation for an existing user without a Telegram identity; the earlier
    outstanding ones are revoked in the same transaction."""
    with factory() as session:
        with session.begin():
            return _reissue(session, user_id, now)


def revoke_invitation(factory: sessionmaker, user_id: uuid.UUID, *, now: Clock) -> int:
    """Withdraw the user's active invitation, one that has expired is left as it is: how
    many were revoked (0 or 1)."""
    with factory() as session:
        with session.begin():
            return _revoke(session, user_id, now)


def _begin(session: OrmSession) -> None:
    """The first statements of every mutation (see the module docstring)."""
    provisioning._serialize(session)
    check_schema_at_head(session.connection())


def _moment(now: Clock) -> datetime:
    """The time of this run: `now` itself, or what the clock says. Read once, with the lock
    held (see the module docstring)."""
    return now() if callable(now) else now


def _enroll(session: OrmSession, display_name: str, now: Clock) -> IssuedInvitation:
    _begin(session)
    now = _moment(now)
    existing = session.execute(sa.select(User.id, User.display_name)).all()
    wanted = display_name.casefold()
    for row in existing:
        if row.display_name is not None and row.display_name.casefold() == wanted:
            raise EnrollmentRefusal(
                EnrollmentRefusalReason.DISPLAY_NAME_IN_USE,
                f"user {row.id} already has this display name. This command creates a NEW "
                "user every time, so a repeated command would spend one of the places on a "
                "duplicate. To give that user a new invitation, use reissue-invitation.",
            )
    if len(existing) >= ENROLLMENT_USER_CAP:
        raise EnrollmentRefusal(
            EnrollmentRefusalReason.ENROLLMENT_FULL,
            f"{len(existing)} users already exist and this command enrolls at most "
            f"{ENROLLMENT_USER_CAP} (a temporary policy of the command, not a database rule). "
            "It never removes or replaces a user. To give an existing user a new invitation, "
            "use reissue-invitation.",
        )
    user = User(display_name=display_name, created_at=now, updated_at=now)
    session.add(user)
    session.flush()
    return invitations.issue_invitation(session, user.id, now=now)


def _reissue(session: OrmSession, user_id: uuid.UUID, now: Clock) -> IssuedInvitation:
    _begin(session)
    now = _moment(now)
    try:
        return invitations.issue_invitation(session, user_id, now=now)
    except InvitationRefused as refused:
        raise _refusal(refused.reason) from None


def _revoke(session: OrmSession, user_id: uuid.UUID, now: Clock) -> int:
    _begin(session)
    now = _moment(now)
    if session.scalar(sa.select(User.id).where(User.id == user_id)) is None:
        raise _refusal(InvitationRefusal.UNKNOWN_USER)
    return invitations.revoke_invitations(session, user_id, now=now)


# --- the report -------------------------------------------------------------------


class TelegramState(str, enum.Enum):
    NOT_LINKED = "not linked"
    LINKED = "linked"
    REVOKED = "revoked"


@dataclass(frozen=True)
class InvitationSummary:
    """The invitation that says where the user stands (see _latest_invitation). `at` is when
    it ends or ended: its expiry for an active or expired one, the time it was used or
    withdrawn otherwise."""

    state: InvitationState
    at: datetime


@dataclass(frozen=True)
class UserStatus:
    user_id: uuid.UUID
    display_name: Optional[str]
    created_at: datetime
    disabled: bool
    telegram: TelegramState
    invitation: Optional[InvitationSummary]


@dataclass(frozen=True)
class EnrollmentStatus:
    users: tuple
    cap: int = ENROLLMENT_USER_CAP


def enrollment_status(factory: sessionmaker, *, now: datetime) -> EnrollmentStatus:
    """Where each user stands. Read only, and never selects a Telegram `sub`, a secret's
    digest or an email address: whether a user has a Telegram identity is the existence of
    an entry, not its value. A report, not a lock: on PostgreSQL its few statements are not
    one snapshot, so a change that commits in the middle can show in part."""
    with factory() as session:
        with session.begin():
            check_schema_at_head(session.connection())
            return _status(session, now)


def _status(session: OrmSession, now: datetime) -> EnrollmentStatus:
    users = session.execute(
        sa.select(User.id, User.display_name, User.created_at, User.disabled_at).order_by(
            User.created_at, User.id
        )
    ).all()
    telegram: dict = {}
    for entry in session.execute(
        sa.select(AllowlistEntry.user_id, AllowlistEntry.revoked_at).where(
            AllowlistEntry.provider == PROVIDER_TELEGRAM
        )
    ):
        state = TelegramState.REVOKED if entry.revoked_at is not None else TelegramState.LINKED
        # An active entry outweighs a revoked one.
        if telegram.get(entry.user_id) is not TelegramState.LINKED:
            telegram[entry.user_id] = state
    history: dict = {}
    for row in session.execute(
        sa.select(
            IdentityInvitation.user_id,
            IdentityInvitation.expires_at,
            IdentityInvitation.consumed_at,
            IdentityInvitation.revoked_at,
        )
    ):
        history.setdefault(row.user_id, []).append(row)

    report = []
    for user in users:
        report.append(
            UserStatus(
                user_id=user.id,
                display_name=user.display_name,
                created_at=user.created_at,
                disabled=user.disabled_at is not None,
                telegram=telegram.get(user.id, TelegramState.NOT_LINKED),
                invitation=_latest_invitation(history.get(user.id, ()), now),
            )
        )
    return EnrollmentStatus(users=tuple(report))


# When two invitations ended at the same instant, the more final outcome is reported.
_FINALITY = {
    InvitationState.USED: 3,
    InvitationState.REVOKED: 2,
    InvitationState.EXPIRED: 1,
    InvitationState.ACTIVE: 0,
}


def _latest_invitation(rows: Iterable, now: datetime) -> Optional[InvitationSummary]:
    """Which of a user's invitations the report is about: the one that can be used, if
    there is one; otherwise the one whose last event (used, withdrawn, or its expiry) is the
    latest, and, if two ended at the same instant, the more final outcome (used before
    withdrawn before expired). Whatever else ties is the same line of output, so the result
    never depends on the order of `rows` or on their ids.

    The rows' created_at is deliberately not used. It is the caller's clock reading, and the
    newest invitation can carry the oldest one (runs that read the clock before queueing at
    the lock, a clock that was set back, rows written by other code); ordering by it, an
    invitation that is still usable could be hidden behind a withdrawn one. A usable
    invitation is at most one per user (the partial unique index), and it outranks every
    event, wherever in time that was."""
    best = None
    for row in rows:
        state = invitation_state(
            expires_at=row.expires_at,
            consumed_at=row.consumed_at,
            revoked_at=row.revoked_at,
            now=now,
        )
        at = {
            InvitationState.USED: row.consumed_at,
            InvitationState.REVOKED: row.revoked_at,
        }.get(state, row.expires_at)
        rank = (state is InvitationState.ACTIVE, at, _FINALITY[state])
        if best is None or rank > best[0]:
            best = (rank, InvitationSummary(state, at))
    return None if best is None else best[1]
