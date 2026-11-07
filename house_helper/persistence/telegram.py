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

Conversations. A Telegram message goes, in this order, to:

  1. the conversation its first delivery used (conversation_of_request), when Telegram
     delivers the update again: the idempotency key of a Telegram turn is made of the
     update id, and a key lives in one conversation, so a redelivery must never be moved
     by a selection made since. If that conversation is gone but the update's admission
     marker is not, the update WAS admitted and must not get a new conversation:
     conversation_for_message raises UpdateAlreadyAdmitted before creating or selecting
     anything (the ledger's identity is passed by the bot);
  2. the conversation this user explicitly selected in Telegram
     (telegram_active_conversations, models.TelegramActiveConversation), when that row
     exists and names one of their conversations;
  3. a NEW conversation, which is then selected, when the selected conversation was
     deleted (the row's conversation_id is NULL) or no longer belongs to the user. The
     bot never silently falls back to an unrelated conversation;
  4. otherwise the conversation most recently active on either channel
     (latest_conversation_id: the one the website lists first and opens first), or a new
     one when the person has none. This is the legacy behaviour, in force exactly while
     no selection row exists.

The order matters: a redelivery is answered where it was first admitted even if the
person has since selected another conversation. The result says whether the conversation
came from that earlier request (`admitted`), so the caller can refuse to run a redelivery
again elsewhere when its own conversation has been deleted. "Ask for a new conversation"
(/new) creates one unless the selected (or, without a selection, the latest) conversation
is still empty, and selects it, so asking twice does not litter the list. Selecting a
conversation writes only the selection row: it never moves conversations.updated_at,
so it does not reorder the website's list.

Every function that may create or move the cursor -- select_conversation,
conversation_for_message, start_conversation_for -- checks, inside its own transaction and
with the link held, that the Telegram account it acts for is still the user's enrolled
sender, when the caller names that account. So an unlink cannot be separated from a cursor
write: whichever transaction gets there first decides, and no cursor is left behind for a
link that is gone (PostgreSQL: a share lock on the link row the unlink's DELETE waits for;
SQLite: the writer lock the first statement already holds).

The selection is per user and independent of the website: the website keeps opening the
conversation it lists first. While a selection row exists, the Web-to-Telegram copy is
made only for that conversation (mirrors_to_telegram); with no row, it is made for every
conversation, as before. Unlinking removes the row with the link, so a new link starts in
the legacy state.

An update id also belongs to the bot and not to the person the account is tied to: an
update that was admitted for one user is never admitted for another, and an update that was
admitted at all is never admitted again after its conversation and turn request are gone
(update_admitted_for_another, admit_update, and the durable markers of
telegram_update_admissions; "the identity of an update" below).

Writers write first, as everywhere in this layer (see persistence.database): issue_link_code
starts with a DELETE (which takes the write lock on SQLite even when it deletes nothing),
consume_link_code with the conditional UPDATE that spends the code, unlink is a DELETE,
and the two functions that may create or select a conversation (conversation_for_message,
start_conversation) start with a no-op UPDATE of the user row, because they may write and
must take the writer lock before reading. admit_update writes the ledger marker but is
called inside the claim's transaction, after begin_turn has already written (its docstring
states that condition), and initialize_update_admissions starts with its insert attempt.
Reads write nothing
(active_conversation, mirrors_to_telegram, conversation_summary through conversations.py).
Nothing here commits; the caller owns the transaction (web.database.WebDatabase.run).
Results are plain values, not ORM objects.
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
from house_helper.persistence.conversations import (
    ConversationSummary,
    conversation_summary,
    create_conversation,
    owns_conversation,
)
from house_helper.persistence.identifiers import TELEGRAM_UPDATE_KEY_PREFIX
from house_helper.persistence.models import (
    TELEGRAM_ADMISSION_RETENTION,
    TELEGRAM_CUTOVER_UPDATE_ID,
    AllowlistEntry,
    Conversation,
    Message,
    TelegramActiveConversation,
    TelegramLink,
    TelegramLinkCode,
    TelegramUpdateAdmission,
    TelegramUpdateAdmissionKind,
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
        # The conversation cursor belongs to the tie that is being removed; a link
        # made later starts in the legacy "latest conversation" state.
        session.execute(
            sa.delete(TelegramActiveConversation)
            .where(TelegramActiveConversation.user_id == user_id)
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
    no_link = ~sa.exists().where(TelegramLink.user_id == user_id)
    session.execute(
        sa.delete(TelegramLinkCode)
        .where(TelegramLinkCode.user_id == user_id, no_link)
        .execution_options(synchronize_session=False)
    )
    # A cursor cannot outlive the tie it belongs to. The same guard as the codes:
    # a link made after the caller looked is not touched, and neither is its selection.
    session.execute(
        sa.delete(TelegramActiveConversation)
        .where(TelegramActiveConversation.user_id == user_id, no_link)
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


@dataclass(frozen=True)
class ActiveConversation:
    """The user's Telegram selection row: `conversation_id` is None when the selected
    conversation was deleted (the row's third state; no row at all is None from
    active_conversation, the legacy state)."""

    conversation_id: Optional[UUID]
    selected_at: datetime


class UpdateAlreadyAdmitted(Exception):
    """The update's admission ledger has a marker, and no turn request of this user's
    proves that the update still has a claim here: its original conversation (and with it
    the turn request) was deleted. Raised by conversation_for_message when it was asked to
    adjudicate an update, before it creates or selects anything, so a duplicate admission
    cannot leave a conversation behind. Nothing was written: the exception rolls the
    resolution's transaction back."""


@dataclass(frozen=True)
class ConversationResolution:
    """The conversation a Telegram update belongs to, and whether it was found through
    the update's own earlier turn request.

    `admitted` is True exactly for a redelivery: a turn request with this update's
    idempotency key already exists in one of the user's conversations. It is the durable
    record that the update was admitted before (persistence.turns claims a turn before
    the provider is called). Nothing here decides what to do with it; web.telegram_chat
    must not treat an update with `admitted` True as a fresh request in another
    conversation when its own conversation disappears.

    `created` is True when this resolution made the conversation. The caller uses it to
    take an empty conversation back if the claim's own admission check then refuses the
    update (a duplicate identified at the last moment): a refused duplicate must not leave
    a conversation a person never asked for."""

    conversation_id: UUID
    admitted: bool
    created: bool = False


def latest_conversation_id(session: OrmSession, user_id: UUID) -> Optional[UUID]:
    """The user's most recently active conversation: first in the order the website lists
    them (newest activity first, ties by id), or None if they have none."""
    return session.scalar(
        sa.select(Conversation.id)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(1)
    )


def active_conversation(session: OrmSession, user_id: UUID) -> Optional[ActiveConversation]:
    """The user's explicit Telegram selection, or None when there is no row (the legacy
    "follow the latest conversation" state). A returned `conversation_id` of None is the
    explicit "the selected conversation was deleted" state. A plain read."""
    row = session.get(TelegramActiveConversation, user_id)
    if row is None:
        return None
    return ActiveConversation(row.conversation_id, row.selected_at)


def mirrors_to_telegram(session: OrmSession, user_id: UUID, conversation_id: UUID) -> bool:
    """Whether a turn of `conversation_id` is copied from the website to this user's
    Telegram chat: True while there is no selection row (the legacy behaviour, every
    conversation is copied), and, once there is one, only for the selected conversation.
    A row whose conversation was deleted copies nothing (it names no conversation)."""
    row = session.get(TelegramActiveConversation, user_id)
    return row is None or row.conversation_id == conversation_id


def _touch_user(session: OrmSession, user_id: UUID) -> None:
    """A no-op write, first in the transaction: takes SQLite's writer lock before
    anything is read, as every writer in this package does. The user's updated_at is
    assigned to itself, so no value changes."""
    session.execute(
        sa.update(User)
        .where(User.id == user_id)
        .values(updated_at=User.updated_at)
        .execution_options(synchronize_session=False)
    )


def _write_selection(
    session: OrmSession, user_id: UUID, conversation_id: Optional[UUID], *, now: datetime
) -> None:
    """Put the user's selection row (or its NULL state) in place, creating the row the
    first time. Nothing outside this table is written; in particular the conversation's
    activity time is not touched."""
    updated = session.execute(
        sa.update(TelegramActiveConversation)
        .where(TelegramActiveConversation.user_id == user_id)
        .values(conversation_id=conversation_id, selected_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not updated:
        session.add(
            TelegramActiveConversation(
                user_id=user_id, conversation_id=conversation_id, selected_at=now
            )
        )
        session.flush()


def _holds_something(session: OrmSession, conversation_id: UUID, now: datetime) -> bool:
    """True when the conversation has a message that has not expired at `now`."""
    return bool(
        session.scalar(
            sa.select(
                sa.exists().where(
                    Message.conversation_id == conversation_id, Message.expires_at > now
                )
            )
        )
    )


def select_conversation(
    session: OrmSession,
    user_id: UUID,
    conversation_id: UUID,
    *,
    now: datetime,
    telegram_user_id: Optional[int] = None,
) -> Optional[ConversationSummary]:
    """Select one of the user's conversations for their Telegram chat, and return it as
    the dialogs list names it; None (with no selection written) when the conversation
    does not exist or belongs to somebody else.

    Ownership is the SELECT's own WHERE: the two refusals are not told apart. The
    selection row is the only thing written; the conversation's updated_at is left as it
    was, so selecting does not move it in the website's list. Write first (the no-op user
    touch), then read, then write, as the package's rule requires: this function may
    create a row, and on SQLite a transaction that read first and wrote afterwards can
    lose the race with another writer.

    With `telegram_user_id` (the bot's click) the tie is checked inside this transaction,
    with the link held until it ends, so "still this person's account" and "selected" are
    one step: a click that raced an unlink or a handover selects nothing. On SQLite the
    no-op write already holds the writer lock; on PostgreSQL `enrolled_user_for_telegram`
    takes a share lock on the link row that the unlink's DELETE must wait for."""
    _touch_user(session, user_id)
    if (
        telegram_user_id is not None
        and enrolled_user_for_telegram(session, telegram_user_id, hold=True) != user_id
    ):
        return None
    summary = conversation_summary(session, user_id, conversation_id, now=now)
    if summary is None:
        return None
    _write_selection(session, user_id, conversation_id, now=now)
    return summary


def conversation_for_message(
    session: OrmSession,
    user_id: UUID,
    key: str,
    *,
    now: datetime,
    telegram_user_id: Optional[int] = None,
    bot_id: Optional[int] = None,
    update_id: Optional[int] = None,
    admitted_here: bool = False,
) -> Optional[ConversationResolution]:
    """The conversation a Telegram message belongs to, in the order the module docstring
    gives: the redelivery's own conversation first, then the explicit selection, then a
    new conversation (selected) when the selected one was deleted or no longer belongs to
    the user, and otherwise the latest conversation or a new one (legacy). None, with
    nothing written, when `telegram_user_id` is no longer the enrolled sender of
    `user_id`.

    `admitted` in the result says whether the conversation was found through this key's
    earlier turn request (a redelivery), so the caller can tell a redelivery whose
    conversation has disappeared from a message that was never admitted.

    `admitted_here` is the caller's own earlier look: it saw this key's turn request, so
    the update was admitted for this user. If that request is gone by the time this
    function runs (its conversation was deleted in between), the update must NOT be
    resolved afresh: UpdateAlreadyAdmitted is raised before anything is created or
    selected, even when the ledger marker itself has already been purged. Admission
    provenance, once seen, survives the disappearance of the records that carried it.

    With `bot_id` and `update_id` (the bot's admission ledger identity) the resolution
    also asks the ledger before it creates anything: when the update's marker exists and
    no turn request of this user's survives, the update was admitted before and its
    conversation is gone, so UpdateAlreadyAdmitted is raised and NOTHING is created or
    selected. That is the check that keeps a duplicate admission from leaving a new
    conversation behind when its original one was deleted between the caller's first look
    and this resolution. Both arguments are optional on purpose: without them the function
    is the plain conversation resolver it always was; `admitted_here` alone is enough for
    the caller that already saw the provenance.

    The function may create a conversation and select it, so it writes first (the no-op
    user touch) and does all of it in one transaction: the conversation it returns is
    committed when this returns. It is still possible for another writer (the website
    deleting the conversation) to remove it before the turn claims it; the caller must
    treat ConversationNotFound from the claim as "resolve again", not as a failure, and
    call this once more with the same key (nothing was stored for it).

    With `telegram_user_id` the tie is checked inside this transaction, with the link held
    until it ends, exactly as select_conversation and start_conversation_for check it: a
    resolution that raced an unlink or a handover writes no cursor and returns None. That
    hold is what keeps a cursor from being recreated after the link it belongs to was
    removed (PostgreSQL: a share lock on the link row the unlink's DELETE must wait for;
    SQLite: the writer lock the first statement already holds)."""
    if (bot_id is None) != (update_id is None):
        raise ValueError("bot_id and update_id go together")
    _touch_user(session, user_id)
    if (
        telegram_user_id is not None
        and enrolled_user_for_telegram(session, telegram_user_id, hold=True) != user_id
    ):
        return None
    found = conversation_of_request(session, user_id, key)
    if found is None and admitted_here:
        # The caller's earlier look saw this update's own turn request, so the update was
        # admitted for this user. The request is gone now (its conversation was deleted in
        # between). Never build another conversation for it, and never fall back to a
        # selection: the update is not a new message whatever the ledger still holds.
        raise UpdateAlreadyAdmitted
    if (
        found is None
        and bot_id is not None
        and update_id is not None
        and update_admission_exists(session, bot_id, update_id)
    ):
        # The update was admitted, and its own conversation (with the turn request that
        # proved it) is gone. Never build another one for it.
        raise UpdateAlreadyAdmitted
    if found is not None:
        return ConversationResolution(found, admitted=True)
    selection = session.get(TelegramActiveConversation, user_id)
    if selection is not None:
        if selection.conversation_id is not None and owns_conversation(
            session, user_id, selection.conversation_id
        ):
            return ConversationResolution(selection.conversation_id, admitted=False)
        # The selection row is NULL (the conversation was deleted) or names a
        # conversation that is no longer this user's: never fall back to another one.
        created = create_conversation(session, user_id, now=now).id
        _write_selection(session, user_id, created, now=now)
        return ConversationResolution(created, admitted=False, created=True)
    latest = latest_conversation_id(session, user_id)
    if latest is not None:
        return ConversationResolution(latest, admitted=False)
    created = create_conversation(session, user_id, now=now).id
    return ConversationResolution(created, admitted=False, created=True)


def start_conversation(session: OrmSession, user_id: UUID, *, now: datetime) -> UUID:
    """The conversation for a person who asked to begin a new one, selected for them.

    The selected conversation is reused while it is empty (asking twice does not litter
    the list). Without a selection row, the latest conversation is reused while it is
    empty, as before; otherwise a new conversation is made. Either way the conversation
    returned is selected, so later messages of the chat go there and the Web-to-Telegram
    copy narrows to it. The first statement is a write (the user's row, assigned to
    itself), which takes the write lock before anything is read, as every writer here
    does. Selecting writes no activity time of any conversation."""
    _touch_user(session, user_id)
    selection = session.get(TelegramActiveConversation, user_id)
    if selection is not None:
        if selection.conversation_id is not None and owns_conversation(
            session, user_id, selection.conversation_id
        ):
            if not _holds_something(session, selection.conversation_id, now):
                return selection.conversation_id
        created = create_conversation(session, user_id, now=now).id
        _write_selection(session, user_id, created, now=now)
        return created
    latest = latest_conversation_id(session, user_id)
    if latest is not None and not _holds_something(session, latest, now):
        _write_selection(session, user_id, latest, now=now)
        return latest
    created = create_conversation(session, user_id, now=now).id
    _write_selection(session, user_id, created, now=now)
    return created


def start_conversation_for(
    session: OrmSession, telegram_user_id: int, user_id: UUID, *, now: datetime
) -> Optional[UUID]:
    """start_conversation for the user, but only while `telegram_user_id` is still the
    enrolled sender of that user; None, with nothing written, otherwise. The check and the
    write are one transaction, so a person whose account was unlinked or whose access was
    withdrawn after the message arrived gets no conversation made for them. The first
    statement is the write (write first, as everywhere in this layer). The link is held
    until the transaction ends, so an unlink cannot remove it between the check and the
    conversation's creation (PostgreSQL: a share lock on the link row; SQLite: the writer
    lock the first statement already holds)."""
    _touch_user(session, user_id)
    if enrolled_user_for_telegram(session, telegram_user_id, hold=True) != user_id:
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
# An update id belongs to the bot, not to whoever the sending account is tied to this week, and not
# to the conversation it first arrived in. There are two records of an admission:
#
#   * the turn request (key "tg-<update id>"), written in the claim's own transaction, in a
#     conversation of the user the account was tied to then. It is what lets a redelivery replay
#     a completed answer or refuse an attempt that began, and its owner is who the update was
#     admitted for;
#   * the marker in telegram_update_admissions, written in that SAME transaction. It stores the
#     bot id and the update id and nothing else. It survives the deletion of the conversation and
#     of the turn request, and it has no owner, so it keeps saying "this update was admitted"
#     after the history it produced is gone.
#
# The marker is why a redelivery whose conversation was deleted cannot be admitted again -- in
# another conversation of the same person, or, after the account was unlinked and linked to
# somebody else, in another person's history. Without it, the turn request was the only record,
# and it is cascaded away with the conversation.
#
# No marker is written for an update that never reached a claim (an unlinked sender, a message
# that was too long, a crash before the claim): a claim that rolls back leaves nothing. Such an
# update is a new message when Telegram brings it again, unless it is older than the replay
# horizon (web.telegram_chat states the stale policy and the cutover policy).
#
# The look across owners is by key over all conversations: the unique index of turn_requests
# starts with the conversation, so this is a scan of the table. It is small (two people, 365
# days); the marker's primary key is (bot_id, update_id) and needs no scan.

# The update id of the bot's cutover row, and the 48-hour window, are in models.py beside the
# table; this module owns what they mean.


class AdmissionStatus(str, enum.Enum):
    """What the ledger and the surviving turn requests say about one update, before a claim."""

    NEW = "new"  # no marker and no turn request anywhere: a message never admitted
    ADMITTED_HERE = "admitted_here"  # a turn request of this user's (redelivery: replay it)
    ADMITTED_ELSEWHERE = "admitted_elsewhere"  # a turn request of another user's
    ADMITTED_GONE = "admitted_gone"  # a marker, but no turn request anywhere: its conversation is gone


class AdmissionOutcome(str, enum.Enum):
    """What the claim's own transaction decided about one update."""

    ADMITTED = "admitted"  # the marker is in place (inserted now, or already there for this claim)
    BINDING_CHANGED = "binding_changed"  # not this account's person, or another owner's update
    ALREADY_ADMITTED = "already_admitted"  # a marker exists and this claim is new: refuse it


@dataclass(frozen=True)
class AdmissionLedger:
    """What the bot's startup reconciliation found."""

    cutover_at: datetime  # updates dated before this are pre-migration (see web.telegram_chat)
    backfilled: int  # markers recovered from surviving turn requests (0 when nothing was missing)


def update_key(update_id: int) -> str:
    """The idempotency key of the turn that answers a Telegram update."""
    return f"{TELEGRAM_UPDATE_KEY_PREFIX}{update_id}"


# The widest a signed BIGINT (the ledger's column) holds; Telegram's own ids are far smaller.
MAX_UPDATE_ID = 2**63 - 1

# The shape update_key produces: no leading zero, positive, at most 19 digits. Anything else
# that happens to share the prefix is not one of ours.
_UPDATE_KEY = re.compile(r"[1-9][0-9]{0,18}")


def update_id_from_key(key: object) -> Optional[int]:
    """The update id of a "tg-<id>" key this module wrote, or None. Used by the backfill and
    kept strict: a leading zero, a sign, a Unicode digit, an empty tail or a value a signed
    64-bit column cannot hold is not an update id."""
    if not isinstance(key, str) or not key.startswith(TELEGRAM_UPDATE_KEY_PREFIX):
        return None
    digits = key[len(TELEGRAM_UPDATE_KEY_PREFIX):]
    if _UPDATE_KEY.fullmatch(digits) is None:
        return None
    value = int(digits)
    return value if value <= MAX_UPDATE_ID else None


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


def update_admission_exists(session: OrmSession, bot_id: int, update_id: int) -> bool:
    """True when the bot's ledger has a marker for this update (a plain primary-key read)."""
    return (
        session.get(TelegramUpdateAdmission, (bot_id, update_id)) is not None
    )


def update_admission_status(
    session: OrmSession, bot_id: int, user_id: UUID, update_id: int, key: str
) -> AdmissionStatus:
    """What is known about this update before anything is fetched, selected, stored or asked.

    A turn request of this user's wins: it is the redelivery that can still be replayed. Then a
    turn request of somebody else's: the update is not this person's. Then the bare marker: it
    was admitted and its history is gone. Then nothing: a new message (the caller applies the
    cutover and staleness policies to it).

    The marker is the bot's, not the user's: a redelivery after the account changed hands and the
    first owner's conversation was deleted is ADMITTED_GONE, exactly like the same owner's. The
    caller's answer reveals nothing of either, because nothing of either is known here."""
    if not _is_account_id(bot_id):
        raise ValueError("a bot id is a positive integer")
    if not _is_update_id(update_id):
        raise ValueError("an update id is a positive integer")
    if conversation_of_request(session, user_id, key) is not None:
        return AdmissionStatus.ADMITTED_HERE
    if update_admitted_for_another(session, user_id, update_id):
        return AdmissionStatus.ADMITTED_ELSEWHERE
    if update_admission_exists(session, bot_id, update_id):
        return AdmissionStatus.ADMITTED_GONE
    return AdmissionStatus.NEW


def _is_update_id(value: object) -> bool:
    """A Telegram update id: a positive integer a signed BIGINT can hold (a bool is an int to
    Python, and not an id)."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= MAX_UPDATE_ID
    )


def _insert_admission(
    session: OrmSession,
    *,
    bot_id: int,
    update_id: int,
    kind: TelegramUpdateAdmissionKind,
    admitted_at: datetime,
    expires_at: Optional[datetime],
) -> bool:
    """Insert one ledger row in a SAVEPOINT; False if it was already there.

    The savepoint is what makes the conflict survivable inside the claim's transaction: on
    PostgreSQL a failed INSERT would otherwise abort the whole transaction, and the caller
    needs to go on to roll it back deliberately (or, for a surviving claim, to commit). A
    conflict that is NOT the primary key (a CHECK that the caller broke) is re-raised: the
    row is looked up to tell the two apart."""
    try:
        with session.begin_nested():
            session.execute(
                sa.insert(TelegramUpdateAdmission).values(
                    bot_id=bot_id,
                    update_id=update_id,
                    kind=kind.value,
                    admitted_at=admitted_at,
                    expires_at=expires_at,
                )
            )
    except sa.exc.IntegrityError:
        if session.get(TelegramUpdateAdmission, (bot_id, update_id)) is None:
            raise
        return False
    return True


def _insert_update_marker(
    session: OrmSession, bot_id: int, update_id: int, admitted_at: datetime
) -> bool:
    return _insert_admission(
        session,
        bot_id=bot_id,
        update_id=update_id,
        kind=TelegramUpdateAdmissionKind.UPDATE,
        admitted_at=admitted_at,
        expires_at=admitted_at + TELEGRAM_ADMISSION_RETENTION,
    )


def admit_update(
    session: OrmSession,
    telegram_user_id: int,
    user_id: UUID,
    update_id: int,
    *,
    bot_id: int,
    now: datetime,
    existing: bool,
) -> AdmissionOutcome:
    """The precondition of the claim of a Telegram turn AND the write of its ledger marker,
    asked INSIDE the claim's transaction (the `authorize` of
    web.turn_runner.WebTurnRunner.submit), so that "still this person's", "not somebody else's
    update", "not admitted before" and "claimed" are one step.

    (1) The account is still the enrolled sender of `user_id`, and the link is held until the
    transaction ends. (2) The update was not admitted for another owner. (3) The marker is
    inserted: absent, it is written now; already present, it stands only if `existing` says the
    update already has a turn request of this user's (`begin_turn` found the key: a replay or a
    retry). A NEW claim that meets an existing marker is ALREADY_ADMITTED: the update was
    admitted before and its original request is gone, so this claim must be rolled back whole.

    The caller must return False for the claim to be refused (turn_runner raises
    TurnNotAuthorized, which rolls the claim back). Nothing is inserted for a refused claim:
    BINDING_CHANGED returns before the marker, and ALREADY_ADMITTED is the marker of an EARLIER
    claim, never one this transaction wrote. The first statement of this transaction must have
    been a write (every claim locks its conversation first); the binding look is a read.

    Why (1) makes (2) airtight. For the update to be admitted for B after it was admitted for A,
    the account must be unlinked from A and linked to B in between. On SQLite the claim's first
    statement (the conversation lock) took the writer lock, so every claim is whole before the
    next one looks. On PostgreSQL the share lock on the link row keeps the unlink away until this
    transaction commits; a claim for B can only follow a link to B, which can only follow that
    unlink, which therefore comes after A's commit, so B's look (a new statement, READ COMMITTED)
    sees A's row. A claim that looks before the unlink is a claim for A. (PostgreSQL: reasoned,
    not run in this repository's tests.)"""
    if not _is_update_id(update_id):
        raise ValueError("an update id is a positive integer")
    if not _is_account_id(bot_id):
        raise ValueError("a bot id is a positive integer")
    if enrolled_user_for_telegram(session, telegram_user_id, hold=True) != user_id:
        return AdmissionOutcome.BINDING_CHANGED
    if update_admitted_for_another(session, user_id, update_id):
        return AdmissionOutcome.BINDING_CHANGED
    inserted = _insert_update_marker(session, bot_id, update_id, now)
    if not inserted and not existing:
        return AdmissionOutcome.ALREADY_ADMITTED
    return AdmissionOutcome.ADMITTED


def initialize_update_admissions(
    session: OrmSession,
    bot_id: int,
    *,
    now: datetime,
    horizon: timedelta = TELEGRAM_ADMISSION_RETENTION,
    cutover_at: Optional[datetime] = None,
) -> AdmissionLedger:
    """Prepare the bot's ledger at startup: write the cutover row once, then recover markers
    from the turn requests that still exist.

    The cutover row is written on the first start after the upgrade and is kept; every later
    start reads it. Updates dated before it are pre-migration updates.

    `cutover_at` is the instant the first attempt began (the process start), when the caller
    has one. A preparation that failed and is retried must not move the cutover forward: the
    updates that arrived during the failure were not acknowledged and are still Telegram's to
    redeliver, so they must not be classified as pre-migration ones. Without it the cutover is
    `now`, which is right for a caller with no earlier instant of its own. It is only used by
    the insert; an existing row always wins.

    The backfill is safe to run on every start: a marker is inserted only when the primary key
    is absent, so a marker that already exists (an admission of this process, or a previous
    backfill) is never changed, and the row's own 48-hour rule is what bounds the work. A
    preparation that fails part way leaves NOTHING behind: its cutover row and every recovered
    marker are in this one transaction, so the next attempt starts from a clean ledger.

    The first statement is the insert attempt, so on SQLite this takes the writer lock before
    its reads, as every writer in this package does. Call it with a transaction of its own,
    before polling begins."""
    if not _is_account_id(bot_id):
        raise ValueError("a bot id is a positive integer")
    begins_at = now if cutover_at is None else cutover_at
    inserted = _insert_admission(
        session,
        bot_id=bot_id,
        update_id=TELEGRAM_CUTOVER_UPDATE_ID,
        kind=TelegramUpdateAdmissionKind.CUTOVER,
        admitted_at=begins_at,
        expires_at=None,
    )
    if inserted:
        began = begins_at
    else:
        found = session.scalar(
            sa.select(TelegramUpdateAdmission.admitted_at).where(
                TelegramUpdateAdmission.bot_id == bot_id,
                TelegramUpdateAdmission.update_id == TELEGRAM_CUTOVER_UPDATE_ID,
                TelegramUpdateAdmission.kind == TelegramUpdateAdmissionKind.CUTOVER.value,
            )
        )
        if found is None:  # the primary key was taken by something that is not the cutover row
            raise RuntimeError("the update admission ledger has no usable cutover row")
        began = found
    backfilled = backfill_update_admissions(session, bot_id, now=now, horizon=horizon)
    return AdmissionLedger(began, backfilled)


def backfill_update_admissions(
    session: OrmSession,
    bot_id: int,
    *,
    now: datetime,
    horizon: timedelta = TELEGRAM_ADMISSION_RETENTION,
) -> int:
    """Recover markers from the turn requests admitted in the last `horizon`, and return how
    many were inserted.

    Only what can be reconstructed is: the update id is in the turn request's key ("tg-<id>")
    and the time it was admitted is the request's created_at. The Telegram message's own date
    was never stored, so it is not invented. A turn request older than the horizon proves
    nothing that is still useful: its update is beyond the replay horizon anyway, and the
    marker would be at or past its own deadline. The earliest request of an update wins, so a
    database that once admitted one update twice (the hole this ledger closes) still gets one
    marker, dated to the first admission.

    A turn request whose conversation was already deleted is gone: no provenance exists for it,
    and the cutover policy of web.telegram_chat refuses unknown pre-migration updates instead
    of guessing."""
    if not _is_account_id(bot_id):
        raise ValueError("a bot id is a positive integer")
    cutoff = now - horizon
    rows = session.execute(
        sa.select(TurnRequest.idempotency_key, TurnRequest.created_at)
        .where(
            TurnRequest.created_at > cutoff,
            TurnRequest.idempotency_key.startswith(TELEGRAM_UPDATE_KEY_PREFIX),
        )
        .order_by(TurnRequest.created_at, TurnRequest.id)
    ).all()
    first_admission: dict[int, datetime] = {}
    for key, created_at in rows:
        found = update_id_from_key(key)
        if found is not None:
            first_admission.setdefault(found, created_at)
    inserted = 0
    for update_id, admitted_at in first_admission.items():
        if admitted_at + horizon <= now:
            continue  # a marker that would be dead on arrival
        if _insert_update_marker(session, bot_id, update_id, admitted_at):
            inserted += 1
    return inserted
