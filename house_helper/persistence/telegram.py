"""Linking a Telegram account to a user, and finding the conversation a Telegram message
belongs to.

Who is who. The website knows a user by a server-side session, and the bot knows a sender by
the numeric id the Bot API puts on every update. Telegram's own documentation shows the `sub`
of its sign-in token and the numeric id as different values, so nothing here assumes they
match, and nothing here reads the sign-in identity at all. The link between the two worlds
is a pairing code:

  1. a signed-in person asks the website for a code (issue_link_code): a random secret,
     valid for ten minutes, stored only as its SHA-256 digest, owned by that user;
  2. they present it to the bot from the Telegram account they want to link
     (consume_link_code, with the id the Bot API reported for the sender);
  3. the code is spent in one conditional UPDATE, and the link is made for the user the
     code belongs to and for nobody else.

So a link needs both halves: a live website session (to get the code) and control of the
Telegram account (to send it). The account that sends the code never says whose it is. A
code that was seen by someone else within its ten minutes is the residual risk; it is
single use, the person sees the linked account's name on the website at once, and can
unlink it (unlink).

What counts as an enrolled user (user_is_enrolled): not disabled, and at least one way of
signing in that has not been revoked. The bot checks it on every message, so withdrawing a
person's access (revoking every identity, disabling the user) also ends their Telegram
access without anyone unlinking anything. It checks again when a queued message's turn
comes, in the transaction that claims the turn, and before anything is sent (web.telegram_chat,
"Who is served, at the moment of serving"); unlink(telegram_user_id=...) and
start_conversation_for exist so that those who act on a tie they saw earlier act only on that tie.

Conversations. A Telegram message goes to the conversation most recently active on either
channel (latest_conversation_id: the one the website lists first and opens first), or to a
new one when the person has none. A redelivered update goes to the conversation its first
delivery used (conversation_of_request): the idempotency key of a Telegram turn is made of
the update id, and a key lives in one conversation. An update id also belongs to the bot and not
to the person the account is tied to: an update that was admitted for one user is never admitted
for another (update_admitted_for_another, admit_update; "the identity of an update" below).

Writers write first, as everywhere in this layer (see persistence.database): issue_link_code
starts with a DELETE (which takes the write lock on SQLite even when it deletes nothing),
consume_link_code with the conditional UPDATE that spends the code, unlink is a DELETE.
Reads write nothing. Nothing here commits; the caller owns the transaction
(web.database.WebDatabase.run). Results are plain values, not ORM objects.
"""

import enum
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.authn import hash_secret
from house_helper.persistence.conversations import create_conversation
from house_helper.persistence.identifiers import TELEGRAM_UPDATE_KEY_PREFIX
from house_helper.persistence.models import (
    AllowlistEntry,
    Conversation,
    Message,
    TelegramLink,
    TelegramLinkCode,
    TurnRequest,
    User,
)

# How long a pairing code works. Long enough to switch to the phone and press Start, short
# enough that a code left on a screen is soon worth nothing.
CODE_LIFETIME = timedelta(minutes=10)

# 24 random bytes are 32 URL-safe characters: 192 bits, and inside the 64 characters
# (A-Z, a-z, 0-9, "_" and "-") that Telegram allows in the payload of a /start link.
CODE_BYTES = 24
_CODE = re.compile(r"[A-Za-z0-9_-]{32}")

MAX_DISPLAY_NAME = 100


class AlreadyLinked(Exception):
    """The user already has a Telegram account linked: unlink it first."""


class UserUnavailable(Exception):
    """The user is disabled or has no way left to sign in: no code is issued."""


class LinkOutcome(str, enum.Enum):
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"  # this very account was already linked to this user
    INVALID_CODE = "invalid_code"  # unknown, used, expired or malformed: not told apart
    ACCOUNT_UNAVAILABLE = "account_unavailable"  # the code's user was disabled or lost access
    TELEGRAM_TAKEN = "telegram_taken"  # this Telegram account is linked to another user
    USER_HAS_ANOTHER = "user_has_another"  # the user has another Telegram account linked


@dataclass(frozen=True)
class IssuedCode:
    code: str  # the secret, shown once
    expires_at: datetime


@dataclass(frozen=True)
class LinkInfo:
    telegram_user_id: int
    display_name: Optional[str]
    created_at: datetime


@dataclass(frozen=True)
class LinkResult:
    outcome: LinkOutcome
    user_id: Optional[UUID] = None  # set for LINKED and ALREADY_LINKED


def new_code() -> str:
    """A URL-safe random secret from the operating system's CSPRNG."""
    return secrets.token_urlsafe(CODE_BYTES)


def is_code(value: object) -> bool:
    """True if `value` has the shape of a code this module issues (so that anything else
    is refused without touching the database)."""
    return isinstance(value, str) and _CODE.fullmatch(value) is not None


def _is_account_id(value: object) -> bool:
    """A Telegram account id: a positive integer (a bool is an int to Python, and not an id)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def clean_display_name(raw: object) -> Optional[str]:
    """What the account called itself, fit to keep and to show: control characters and
    newlines turned into spaces, whitespace collapsed, at most MAX_DISPLAY_NAME
    characters. None for anything that is not a non-empty string."""
    if not isinstance(raw, str):
        return None
    text = " ".join("".join(" " if (not char.isprintable()) else char for char in raw).split())
    if not text:
        return None
    return text[:MAX_DISPLAY_NAME].rstrip() or None


def user_is_enrolled(session: OrmSession, user_id: UUID) -> bool:
    """True for a user who is not disabled and has a way of signing in that has not been
    revoked (the allowlist is how a person is admitted at all)."""
    return (
        session.scalar(
            sa.select(User.id).where(
                User.id == user_id,
                User.disabled_at.is_(None),
                sa.exists().where(
                    AllowlistEntry.user_id == User.id, AllowlistEntry.revoked_at.is_(None)
                ),
            )
        )
        is not None
    )


# --- the link ------------------------------------------------------------------------


def get_link(session: OrmSession, user_id: UUID) -> Optional[LinkInfo]:
    row = session.scalar(sa.select(TelegramLink).where(TelegramLink.user_id == user_id))
    if row is None:
        return None
    return LinkInfo(row.telegram_user_id, row.display_name, row.created_at)


def unlink(session: OrmSession, user_id: UUID, *, telegram_user_id: Optional[int] = None) -> bool:
    """Remove the user's link (and the codes they still hold). True if there was a link.

    With `telegram_user_id` the link goes only if it is the link to that very account, and
    nothing at all is changed otherwise (False): whoever asks to remove the tie between a user
    and an account they saw a moment ago does not remove a tie that was made since."""
    link = sa.delete(TelegramLink).where(TelegramLink.user_id == user_id)
    if telegram_user_id is not None:
        link = link.where(TelegramLink.telegram_user_id == telegram_user_id)
    removed = session.execute(link.execution_options(synchronize_session=False)).rowcount
    if telegram_user_id is None or removed:
        session.execute(
            sa.delete(TelegramLinkCode)
            .where(TelegramLinkCode.user_id == user_id)
            .execution_options(synchronize_session=False)
        )
    return removed == 1


def withdraw_codes_unless_linked(session: OrmSession, user_id: UUID) -> bool:
    """The "nothing to unlink" half of unlinking: the pairing codes the user still holds are
    withdrawn, but only while the user has no link, in one statement that checks and deletes
    together. True if the user had no link (the codes are gone). False if they have one: then
    nothing at all is changed, and in particular no link is removed. A link that exists at this
    point was made after whoever asks looked and found none, and what is made after a look is
    not what the look asked to remove; removing a link is `unlink(telegram_user_id=...)`, under
    the binding lock of that account (web.telegram_chat). Write first: the DELETE takes the
    write lock on SQLite even when it deletes nothing."""
    session.execute(
        sa.delete(TelegramLinkCode)
        .where(
            TelegramLinkCode.user_id == user_id,
            ~sa.exists().where(TelegramLink.user_id == user_id),
        )
        .execution_options(synchronize_session=False)
    )
    return get_link(session, user_id) is None


def issue_link_code(session: OrmSession, user_id: UUID, *, now: datetime) -> IssuedCode:
    """A new pairing code for the user; the codes they still held are withdrawn.

    AlreadyLinked if the user has an account linked, UserUnavailable if they are disabled
    or have no sign-in left (nothing is stored for either: the exception rolls back the
    withdrawal too).
    """
    session.execute(
        sa.delete(TelegramLinkCode)
        .where(TelegramLinkCode.user_id == user_id, TelegramLinkCode.consumed_at.is_(None))
        .execution_options(synchronize_session=False)
    )
    if not user_is_enrolled(session, user_id):
        raise UserUnavailable
    if get_link(session, user_id) is not None:
        raise AlreadyLinked
    code = new_code()
    expires_at = now + CODE_LIFETIME
    session.add(
        TelegramLinkCode(
            user_id=user_id, code_hash=hash_secret(code), created_at=now, expires_at=expires_at
        )
    )
    session.flush()
    return IssuedCode(code, expires_at)


def consume_link_code(
    session: OrmSession,
    code: object,
    telegram_user_id: int,
    display_name: object,
    *,
    now: datetime,
) -> LinkResult:
    """Spend `code` and link `telegram_user_id` to the user it was issued for.

    The code is spent first, by one conditional UPDATE (not consumed, not expired), and
    stays spent whatever else is found out afterwards: a refusal for any reason but a bad
    code uses the code up, so a code cannot be tried again from another account. The
    caller must COMMIT the transaction for every outcome (this function does not raise for
    a refusal); only INVALID_CODE changes nothing.

    LINKED: the link was made. ALREADY_LINKED: the same account was already linked to the
    same user (nothing changed). ACCOUNT_UNAVAILABLE: the user was disabled or lost every
    way in since the code was issued. TELEGRAM_TAKEN: this account is linked to another
    user. USER_HAS_ANOTHER: the user has a different account linked.
    """
    if not is_code(code) or not _is_account_id(telegram_user_id):
        return LinkResult(LinkOutcome.INVALID_CODE)
    digest = hash_secret(code)
    spent = session.execute(
        sa.update(TelegramLinkCode)
        .where(
            TelegramLinkCode.code_hash == digest,
            TelegramLinkCode.consumed_at.is_(None),
            TelegramLinkCode.expires_at > now,
        )
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    if spent != 1:
        return LinkResult(LinkOutcome.INVALID_CODE)
    user_id = session.scalar(
        sa.select(TelegramLinkCode.user_id).where(TelegramLinkCode.code_hash == digest)
    )
    if not user_is_enrolled(session, user_id):
        return LinkResult(LinkOutcome.ACCOUNT_UNAVAILABLE)

    existing = session.scalar(
        sa.select(TelegramLink).where(
            sa.or_(TelegramLink.user_id == user_id, TelegramLink.telegram_user_id == telegram_user_id)
        )
    )
    if existing is not None:
        if existing.user_id == user_id and existing.telegram_user_id == telegram_user_id:
            return LinkResult(LinkOutcome.ALREADY_LINKED, user_id)
        if existing.telegram_user_id == telegram_user_id:
            return LinkResult(LinkOutcome.TELEGRAM_TAKEN)
        return LinkResult(LinkOutcome.USER_HAS_ANOTHER)

    session.add(
        TelegramLink(
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            display_name=clean_display_name(display_name),
            created_at=now,
        )
    )
    session.flush()
    return LinkResult(LinkOutcome.LINKED, user_id)


def enrolled_user_for_telegram(
    session: OrmSession, telegram_user_id: int, *, hold: bool = False
) -> Optional[UUID]:
    """The user a Telegram sender is, or None: the sender has no link, or the linked user
    is not enrolled (disabled, or every way of signing in revoked). The only question the
    bot asks before it does anything for a sender.

    `hold=True` keeps the link in place until the transaction ends: PostgreSQL takes a share
    lock on the link row, which a concurrent unlink (a DELETE of that row) has to wait for;
    SQLite ignores the clause and needs it not (a transaction that has written holds the
    writer lock, see admit_update)."""
    if not _is_account_id(telegram_user_id):
        return None
    query = (
        sa.select(TelegramLink.user_id)
        .join(User, User.id == TelegramLink.user_id)
        .where(
            TelegramLink.telegram_user_id == telegram_user_id,
            User.disabled_at.is_(None),
            sa.exists().where(
                AllowlistEntry.user_id == User.id, AllowlistEntry.revoked_at.is_(None)
            ),
        )
    )
    if hold:
        query = query.with_for_update(read=True, of=TelegramLink)
    return session.scalar(query)


def chat_of_user(session: OrmSession, user_id: UUID) -> Optional[int]:
    """The Telegram chat to write to for this user (the id of their private chat), or
    None when they have no link or are not enrolled."""
    return session.scalar(
        sa.select(TelegramLink.telegram_user_id)
        .join(User, User.id == TelegramLink.user_id)
        .where(
            TelegramLink.user_id == user_id,
            User.disabled_at.is_(None),
            sa.exists().where(
                AllowlistEntry.user_id == User.id, AllowlistEntry.revoked_at.is_(None)
            ),
        )
    )


# --- conversations ---------------------------------------------------------------------


def latest_conversation_id(session: OrmSession, user_id: UUID) -> Optional[UUID]:
    """The user's most recently active conversation: first in the order the website lists
    them (newest activity first, ties by id), or None if they have none."""
    return session.scalar(
        sa.select(Conversation.id)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(1)
    )


def start_conversation(session: OrmSession, user_id: UUID, *, now: datetime) -> UUID:
    """The conversation for a person who asked to begin a new one: the latest one if it is
    still empty (asking twice does not litter the list), otherwise a new one. The first
    statement is a write (the user's row, assigned to itself), which takes the write lock
    before anything is read, as every writer here does."""
    session.execute(
        sa.update(User)
        .where(User.id == user_id)
        .values(updated_at=User.updated_at)
        .execution_options(synchronize_session=False)
    )
    latest = latest_conversation_id(session, user_id)
    if latest is not None:
        holds_something = session.scalar(
            sa.select(
                sa.exists().where(Message.conversation_id == latest, Message.expires_at > now)
            )
        )
        if not holds_something:
            return latest
    return create_conversation(session, user_id, now=now).id


def start_conversation_for(
    session: OrmSession, telegram_user_id: int, user_id: UUID, *, now: datetime
) -> Optional[UUID]:
    """start_conversation for the user, but only while `telegram_user_id` is still the
    enrolled sender of that user; None, with nothing written, otherwise. The check and the
    write are one transaction, so a person whose account was unlinked or whose access was
    withdrawn after the message arrived gets no conversation made for them. The first
    statement is the write (write first, as everywhere in this layer)."""
    session.execute(
        sa.update(User)
        .where(User.id == user_id)
        .values(updated_at=User.updated_at)
        .execution_options(synchronize_session=False)
    )
    if enrolled_user_for_telegram(session, telegram_user_id) != user_id:
        return None
    return start_conversation(session, user_id, now=now)


def conversation_of_request(session: OrmSession, user_id: UUID, key: str) -> Optional[UUID]:
    """The conversation of the user's that already holds a turn request with this
    idempotency key, or None. Found through the user's own conversations only."""
    return session.scalar(
        sa.select(TurnRequest.conversation_id)
        .join(Conversation, Conversation.id == TurnRequest.conversation_id)
        .where(Conversation.user_id == user_id, TurnRequest.idempotency_key == key)
        .limit(1)
    )


# --- the identity of an update -----------------------------------------------------------
#
# An update id belongs to the bot, not to whoever the sending account is tied to this week. The
# first time an update is claimed, its turn request (key "tg-<update id>") is written in the
# claim's own transaction, in a conversation of the user the account was tied to then; that row
# IS the record that the update was admitted, and the owner of its conversation IS the owner of
# the update. Nothing else is stored: what was missing was only a look across owners, and an
# update must never be admitted a second time for anybody other than that owner (it would run the
# assistant on the first owner's words and put them into somebody else's history).
#
# No row is written for an update that never reached a claim (it was dropped on the way: an
# unlinked sender, a message that was too long, a crash before the claim). Nothing is known of such
# an update, and a redelivery of it is a new message for whoever the account is tied to when it
# comes. The record begins at the claim; the module docstring of web.telegram_chat says so.
#
# The look is by key over all conversations: the unique index of turn_requests starts with the
# conversation, so this is a scan of the table. It is small (two people, 365 days) and it is the
# price of not adding a table; if the volume ever made it matter, an index on the key is the
# remedy, as a decision of its own.


def update_key(update_id: int) -> str:
    """The idempotency key of the turn that answers a Telegram update."""
    return f"{TELEGRAM_UPDATE_KEY_PREFIX}{update_id}"


def update_admitted_for_another(session: OrmSession, user_id: UUID, update_id: int) -> bool:
    """True if this update already has a turn request in a conversation of somebody who is not
    `user_id`: it was admitted for another owner, and `user_id` has no claim on it. An update of
    the user's own (any state) or one never admitted is False."""
    return (
        session.scalar(
            sa.select(TurnRequest.id)
            .join(Conversation, Conversation.id == TurnRequest.conversation_id)
            .where(
                TurnRequest.idempotency_key == update_key(update_id),
                Conversation.user_id != user_id,
            )
            .limit(1)
        )
        is not None
    )


def admit_update(session: OrmSession, telegram_user_id: int, user_id: UUID, update_id: int) -> bool:
    """The precondition of the claim of a Telegram turn, asked INSIDE the claim's transaction
    (the `authorize` of web.turn_runner.WebTurnRunner.submit), so that "still this person's",
    "not somebody else's update" and "claimed" are one step. True for the claim to stand.

    (1) The account is still the enrolled sender of `user_id`, and the link is held until the
    transaction ends. (2) The update was not admitted for another owner.

    Why (1) makes (2) airtight. For the update to be admitted for B after it was admitted for A,
    the account must be unlinked from A and linked to B in between. On SQLite the claim's first
    statement (the conversation lock) took the writer lock, so every claim is whole before the
    next one looks. On PostgreSQL the share lock on the link row keeps the unlink away until this
    transaction commits; a claim for B can only follow a link to B, which can only follow that
    unlink, which therefore comes after A's commit, so B's look (a new statement, READ COMMITTED)
    sees A's row. A claim that looks before the unlink is a claim for A. (PostgreSQL: reasoned,
    not run in this repository's tests.)"""
    if enrolled_user_for_telegram(session, telegram_user_id, hold=True) != user_id:
        return False
    return not update_admitted_for_another(session, user_id, update_id)
