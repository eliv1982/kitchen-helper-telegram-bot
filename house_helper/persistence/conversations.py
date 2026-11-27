"""Conversations and their messages, as the web chat API stores and reads them.

Ownership. Every function takes the user id of the server-side session and finds a
conversation only through it: an id that does not exist, that belongs to somebody
else or that is malformed is the same answer (None, or ConversationNotFound), so
nothing tells the caller which of them it was. This module never reads a user id from
anywhere else, and a conversation's owner is never changed.

Reads write nothing. list_conversations, list_messages and build_context are plain
SELECTs: they take no write lock (a reader never waits for a writer, nor the writer for
it), never move conversations.updated_at and never touch a row. The list is ordered by
the latest available message (see "Ordering" below), not by updated_at. The writers
(create_conversation and delete_conversation here, set/reset_conversation_title,
turns.begin_turn, complete_turn and fail_turn) make a WRITE their first statement, the
repository's rule for SQLite's deferred BEGIN (see persistence.database).

Deletion. delete_conversation deletes the owner's conversation in one statement of its
own; the database's ON DELETE CASCADE removes its messages, turn requests, stored
message recipes and attachments. Favourites are copies with no foreign key to a
conversation or a message (models.FavoriteRecipe), so they are not touched. A turn
whose conversation is deleted while it is being answered finds nothing to complete in,
stores no answer, and leaves no row behind.

Retention. A message is "there" only while its expires_at is in the future: every read
below takes `now` from the caller (the application's clock) and leaves an expired
message out, whether or not a purge job has deleted it yet. The conversation itself has
no deadline. A stored user title (see "Titles") has no deadline either; the derived
title is made from the person's own first words only while that message is retained.

Titles. A conversation has at most one stored, user-chosen title in
conversations.title (PP-01-A1). It is explicit navigation metadata, written only by
set_conversation_title and removed only by reset_conversation_title or by deleting
the conversation. It never changes what is said, is never sent to the assistant, and
carries no expiry of its own: the person's title stays until they reset it, while
the messages it describes keep their ordinary 365-day retention.

Without a stored title the name is made when it is read, from the first user
message that has not expired and that carries text a name can be taken from
(conversation_title): the person's own first words, shortened. A photo sent
without a caption is stored with the channel's placeholder text
(attachments.PHOTO_PLACEHOLDER); such a message is skipped while it has a photo
attachment, and the first real user text after it names the conversation. That
derived name lives exactly as long as that message does. When the message
expires, the next one names the conversation; when none is left the title is
None and the page falls back to the date. A stored title always wins over the
derived one, and resetting it restores whatever the derived (or missing) title
is at that moment -- it never restores an archived earlier title, because none
is kept.

Ordering. The list is ordered by the latest message that is still available at the
read's `now`, falling back to the conversation's creation time while it has none.
Conversations.updated_at is the time of the last write that added a message; it is
not shown and is not the ordering key, so a title rename neither moves a
conversation nor invents message activity.

Nothing here commits; the caller owns the transaction (web.database.WebDatabase.run).
Results are plain values, not ORM objects, so they can leave the worker thread.
"""

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm.attributes import set_committed_value

from house_helper.core import title_unicode
from house_helper.persistence import attachments as att
from house_helper.persistence.models import (
    Conversation,
    Message,
    MessageAttachment,
    MessageRole,
    TurnRequest,
    TurnStatus,
)


class ConversationNotFound(Exception):
    """No such conversation for this user (unknown, somebody else's: not told apart)."""


# The longest name a conversation gets from its first message, in characters (code
# points), before the ellipsis. The page applies the same rule to a conversation it has
# just made (chat-logic.js, deriveTitle); tests/test_web_ui_logic.py compares the two.
TITLE_LENGTH = 60
TITLE_ELLIPSIS = "…"

# The width of the stored, user-chosen title (the conversations.title column, in code
# points). A rename is authoritative here: the page mirrors this rule before it sends.
MAX_TITLE_LENGTH = 200

# What the title a read returns was made from.
TITLE_ORIGIN_MANUAL = "manual"    # the person's own stored title
TITLE_ORIGIN_DERIVED = "derived"  # made from the first available user message
TITLE_ORIGIN_NONE = "none"        # no message and no stored title: the page's date fallback


class InvalidConversationTitle(ValueError):
    """A title that cannot be stored. `reason` is "empty", "too_long" or "characters"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# The two format (Cf) characters a valid emoji sequence may need: ZERO WIDTH JOINER
# (U+200D, which joins the people of a family emoji) and the tag characters
# (U+E0020..U+E007F, which spell an emoji subdivision flag). Every other Unicode format
# character -- bidi controls, the BOM, soft hyphens and the rest -- is invisible
# punctuation a title does not need. Being in this set is necessary but not sufficient:
# `_format_characters_are_well_placed` permits each one only inside the sequence that
# needs it, never on its own or in ordinary text.
_ZWJ = 0x200D
_TAG_FIRST = 0xE0020
_TAG_LAST = 0xE007F
_TAG_END = 0xE007F
# A subdivision flag spells its region with lowercase tag letters only (a-z). A tag
# space, a digit tag or the cancel tag is not part of that alphabet and is refused.
_TAG_LETTER_FIRST = 0xE0061
_TAG_LETTER_LAST = 0xE007A
_FLAG_BASE = 0x1F3F4  # U+1F3F4 BLACK FLAG: the base of an emoji tag (subdivision) sequence
_VS15 = 0xFE0E
_VS16 = 0xFE0F
_SKIN_MODIFIERS = frozenset(range(0x1F3FB, 0x1F400))
_JOINER_LEFT_SKIP = frozenset((_VS15, _VS16)) | _SKIN_MODIFIERS


def _is_emoji_format(character: str) -> bool:
    point = ord(character)
    return point == _ZWJ or _TAG_FIRST <= point <= _TAG_LAST


def _forbidden_title_character(character: str) -> bool:
    """A character a title may not hold at all: controls (line breaks and tabs among
    them), unpaired surrogates, the Unicode line/paragraph separators, and every format
    character except the two kinds an emoji sequence may use."""
    category = unicodedata.category(character)
    if category in ("Cc", "Cs", "Zl", "Zp"):
        return True
    if category == "Cf":
        return not _is_emoji_format(character)
    return False


def _emoji_base_before(points: list[int], index: int) -> Optional[int]:
    """The emoji symbol that a ZWJ at `index` joins on its left, skipping the variation
    selector and skin-tone modifiers that may stand between them; None when there is no
    such symbol (so the joiner is misplaced). "Emoji symbol" is the frozen
    Extended_Pictographic table in core.title_unicode, not the runtime's category So:
    U+2605 BLACK STAR is So and is not an emoji."""
    position = index - 1
    while position >= 0 and points[position] in _JOINER_LEFT_SKIP:
        position -= 1
    if position < 0:
        return None
    candidate = points[position]
    return candidate if title_unicode.is_emoji_base(candidate) else None


def _is_emoji_base(point: int) -> bool:
    return title_unicode.is_emoji_base(point)


def _format_characters_are_well_placed(points: list[int]) -> bool:
    """True when every ZWJ joins two emoji bases and every tag character belongs to a
    well-formed subdivision-flag tag sequence (U+1F3F4, one or more tag letters a-z,
    then U+E007F). A standalone joiner or tag character, a tag character outside that
    sequence, a tag character that is not a letter (a tag space, for instance) and a tag
    sequence without its end are all refused. A bare U+1F3F4 without a tag sequence is
    ordinary emoji text."""
    index = 0
    total = len(points)
    while index < total:
        point = points[index]
        if _TAG_FIRST <= point <= _TAG_LAST:
            # Reached only when no flag base consumed it: a tag character is not free text.
            return False
        if (
            point == _FLAG_BASE
            and index + 1 < total
            and _TAG_LETTER_FIRST <= points[index + 1] <= _TAG_LETTER_LAST
        ):
            position = index + 1
            while position < total and _TAG_LETTER_FIRST <= points[position] <= _TAG_LETTER_LAST:
                position += 1
            if position >= total or points[position] != _TAG_END:
                return False
            index = position + 1
            continue
        if point == _ZWJ:
            right = points[index + 1] if index + 1 < total else None
            if (
                _emoji_base_before(points, index) is None
                or right is None
                or not _is_emoji_base(right)
            ):
                return False
        index += 1
    return True


def _blank_looking(points: list[int]) -> bool:
    """True when the title has no visible character at all: every code point is
    default-ignorable (U+FE0F, U+034F, a Hangul filler, a lone tag character) or the
    blank braille cell U+2800. A title of only those is blank, whatever the categories
    of ``str.strip`` say."""
    return all(title_unicode.is_blank_looking(point) for point in points)


def normalize_conversation_title(raw: str) -> str:
    """A user-chosen conversation title as it is stored, or InvalidConversationTitle.

    Trimmed at both ends (str.strip, as every stored text is), non-empty, at most
    MAX_TITLE_LENGTH Unicode code points, visibly non-blank, and without line breaks,
    control characters, unpaired surrogates or invisible format characters that are not
    part of an emoji sequence. Combining marks, keycap sequences and ordinary emoji are
    untouched. A longer title is refused, never silently shortened.

    Forbidden characters are refused before anything is trimmed, so a title ending in a
    newline or beginning with U+2028 is refused rather than silently "repaired" by
    str.strip (which treats both as whitespace). A title whose every remaining character
    is invisible (default-ignorable or the blank braille cell) is refused as characters.
    The two emoji format characters are allowed only where the sequence needs them: ZWJ
    between two emoji bases, and lowercase tag letters inside a U+1F3F4
    subdivision-flag sequence."""
    if not isinstance(raw, str):
        raise InvalidConversationTitle("characters")
    if any(_forbidden_title_character(character) for character in raw):
        raise InvalidConversationTitle("characters")
    text = raw.strip()
    if not text:
        raise InvalidConversationTitle("empty")
    if len(text) > MAX_TITLE_LENGTH:
        raise InvalidConversationTitle("too_long")
    points = [ord(character) for character in text]
    if _blank_looking(points):
        raise InvalidConversationTitle("characters")
    if not _format_characters_are_well_placed(points):
        raise InvalidConversationTitle("characters")
    return text


def conversation_title(text: Optional[str]) -> Optional[str]:
    """The name for a conversation whose first message is `text`: its words on one line
    (every run of whitespace is one space), cut at TITLE_LENGTH characters with an
    ellipsis when longer. None when there is nothing to name it by."""
    if not text:
        return None
    line = " ".join(text.split())
    if not line:
        return None
    if len(line) <= TITLE_LENGTH:
        return line
    return line[:TITLE_LENGTH].rstrip() + TITLE_ELLIPSIS


@dataclass(frozen=True)
class ConversationSummary:
    id: UUID
    title: Optional[str]
    created_at: datetime
    updated_at: datetime
    # When the latest message that is still available was created; None while the
    # conversation has no such message (so `created_at` is the ordering fallback).
    last_message_at: Optional[datetime] = None
    # "manual" (a stored user title), "derived" (made from the first available user
    # message) or "none" (nothing to name it by).
    title_origin: str = TITLE_ORIGIN_NONE
    # Whether a stored user title exists and can be reset. Always title_origin == manual.
    can_reset_title: bool = False


@dataclass(frozen=True)
class ConversationPage:
    conversations: list[ConversationSummary]
    has_more: bool


# What a user message looks like to the person who sent it. "failed" covers a request
# that failed and one whose worker is gone (a processing request past its lease): both
# are for the person to send again, with the same idempotency key.
MESSAGE_COMPLETED = "completed"
MESSAGE_IN_PROGRESS = "in_progress"
MESSAGE_FAILED = "failed"


@dataclass(frozen=True)
class MessageView:
    id: int
    role: str
    text: str
    created_at: datetime
    status: str
    # Only for a user message that is not completed: what the client needs to send it
    # again after a reload.
    idempotency_key: Optional[str] = None
    error_code: Optional[str] = None
    # The photo the message came with, if it has one that has not expired (no bytes).
    attachment: Optional[att.AttachmentRef] = None


@dataclass(frozen=True)
class MessagePage:
    messages: list[MessageView]  # oldest first
    has_more: bool  # older messages exist (page back with before=messages[0].id)


# --- conversations -------------------------------------------------------------------


def create_conversation(session: OrmSession, user_id: UUID, *, now: datetime) -> ConversationSummary:
    """A new, empty conversation of `user_id`. The insert is the transaction's first
    statement."""
    conversation = Conversation(user_id=user_id, created_at=now, updated_at=now)
    session.add(conversation)
    session.flush()
    return _summary(conversation)


def delete_conversation(session: OrmSession, user_id: UUID, conversation_id: UUID) -> None:
    """Delete the user's conversation and everything the database cascades from it.

    Ownership is the DELETE's own WHERE: a conversation that does not exist, belongs to
    somebody else or is not even a UUID (the route refuses that shape before this) is the
    same ConversationNotFound, so nothing tells the caller which of them it was. The
    statement is the transaction's first: it takes the write lock (SQLite) or the row lock
    (PostgreSQL) that a turn being answered also takes, so the two see one order.

    Messages, turn requests, message recipes and attachments go with it through their
    ON DELETE CASCADE foreign keys. Favourite recipes do not: they are copies with no key
    to this conversation (models.FavoriteRecipe), and nothing here touches them. A turn
    that was claimed and is still generating finds no conversation when it completes and
    stores nothing (turns.complete_turn, LeaseLost)."""
    deleted = session.execute(
        sa.delete(Conversation)
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        .execution_options(synchronize_session=False)
    ).rowcount
    if deleted != 1:
        raise ConversationNotFound


def _reconcile_loaded_title(session: OrmSession, conversation_id: UUID) -> None:
    """Give a Conversation already in this session's identity map the title the bulk
    UPDATE just persisted, without touching any other attribute.

    The title mutations update with synchronize_session=False, so a Conversation already
    loaded in the transaction would keep the title it had before the UPDATE and the
    summary read below would return that stale value. A whole-row refresh
    (Session.get(..., populate_existing=True)) would also overwrite unrelated unsaved
    changes on the same object -- an updated_at the caller has set, for instance -- and
    a refresh that begins by flushing would write them to the database. So the title is
    read on its own (one scalar SELECT, under no_autoflush) and set as the committed
    value: it is not marked dirty, it does not flush, and nothing else on the object or
    on any other object changes. An object that is not loaded is left alone: the summary
    read below loads the row as it now is."""
    with session.no_autoflush:
        loaded = session.get(Conversation, conversation_id)
        if loaded is None:
            return
        stored = session.scalar(
            sa.select(Conversation.title).where(Conversation.id == conversation_id)
        )
    set_committed_value(loaded, "title", stored)


def set_conversation_title(
    session: OrmSession, user_id: UUID, conversation_id: UUID, *, title: str, now: datetime
) -> ConversationSummary:
    """Store the user's title for their conversation, and return the conversation as the
    list shows it. The title is validated first; a conversation that does not exist or
    belongs to somebody else is ConversationNotFound and nothing is written.

    updated_at is assigned to itself: a rename is navigation metadata, not conversation
    activity, so it must not move the conversation in the list or extend the retention of
    an empty one. The row is found by the UPDATE's own WHERE, so a rename from another
    account changes no row (the two refusals are not told apart). Unrelated pending
    changes in the caller's session are neither overwritten nor flushed (the read-back
    runs under no_autoflush)."""
    text = normalize_conversation_title(title)
    with session.no_autoflush:
        changed = session.execute(
            sa.update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .values(title=text, updated_at=Conversation.updated_at)
            .execution_options(synchronize_session=False)
        ).rowcount
    if changed != 1:
        raise ConversationNotFound
    _reconcile_loaded_title(session, conversation_id)
    with session.no_autoflush:
        found = conversation_summary(session, user_id, conversation_id, now=now)
    if found is None:
        raise ConversationNotFound  # the row this transaction just updated is gone
    return found


def reset_conversation_title(
    session: OrmSession, user_id: UUID, conversation_id: UUID, *, now: datetime
) -> ConversationSummary:
    """Remove a stored user title, restoring the derived (or missing) one. Idempotent: a
    conversation that already had no stored title is returned unchanged. updated_at is
    assigned to itself, as in set_conversation_title, and unrelated pending changes in
    the caller's session are neither overwritten nor flushed."""
    with session.no_autoflush:
        changed = session.execute(
            sa.update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .values(title=None, updated_at=Conversation.updated_at)
            .execution_options(synchronize_session=False)
        ).rowcount
    if changed != 1:
        raise ConversationNotFound
    _reconcile_loaded_title(session, conversation_id)
    with session.no_autoflush:
        found = conversation_summary(session, user_id, conversation_id, now=now)
    if found is None:
        raise ConversationNotFound
    return found


def _first_question_subquery(now: datetime) -> sa.ScalarSelect:
    """The text naming a conversation: its first available user message that the person
    actually wrote.

    A photo sent without a caption is stored with the channel's placeholder text
    (attachments.PHOTO_PLACEHOLDER) and has a message_attachments row pointing at the
    message. Only that pair is skipped: the placeholder is technical text, not a name.
    A message whose text is the same characters but which carries no photo attachment is
    genuine user text and stays eligible. The existence test is a correlated subquery
    over the unique index on message_attachments.message_id, not a query per message; the
    message's own ``expires_at > now`` filter is unchanged, and the message the
    attachment points at expires with it.

    Known ambiguity (no migration introduced to resolve it): an uncaptioned photo and a
    photo whose caption is exactly the placeholder string have the same stored text and
    the same attachment metadata, so the two cannot be told apart here. Both are skipped.
    """
    has_attachment = sa.exists(
        sa.select(1).where(MessageAttachment.message_id == Message.id)
    ).correlate(Message)
    return (
        sa.select(Message.content)
        .where(
            Message.conversation_id == Conversation.id,
            Message.role == MessageRole.USER.value,
            Message.expires_at > now,
            sa.or_(Message.content != att.PHOTO_PLACEHOLDER, ~has_attachment),
        )
        .order_by(Message.id)
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )


def _last_message_at_subquery(now: datetime) -> sa.ScalarSelect:
    """When the latest message still available at `now` was created, or NULL.

    "Latest" is the greatest created_at among the eligible rows, not the row with the
    greatest id: an id follows insertion order, and a message written later may carry an
    earlier instant (a restored, imported or clock-adjusted history). The existing
    (conversation_id, id) index narrows the scan to the conversation; the aggregate then
    reads that conversation's available messages."""
    return (
        sa.select(sa.func.max(Message.created_at))
        .where(
            Message.conversation_id == Conversation.id,
            Message.expires_at > now,
            Message.role.in_((MessageRole.USER.value, MessageRole.ASSISTANT.value)),
        )
        .correlate(Conversation)
        .scalar_subquery()
    )


def activity_at(now: datetime) -> sa.ColumnElement:
    """The instant a conversation is ordered by: its latest available message, or its
    creation time while it has none. Used by the website and by the Telegram dialogs list
    so both sort one way."""
    return sa.func.coalesce(_last_message_at_subquery(now), Conversation.created_at)


def list_conversations(
    session: OrmSession,
    user_id: UUID,
    *,
    now: datetime,
    limit: int,
    offset: int = 0,
) -> ConversationPage:
    """The user's conversations, most recently active first (ties by id, so the order
    is total), at most `limit` of them after skipping `offset`, each named (see
    "Titles", above) by its stored title or its first user message that has not expired
    at `now`.

    Activity is the latest available message, not conversations.updated_at: a stale
    updated_at (the messages it counted have expired since) must not keep a conversation
    at the top with a false date. A conversation without an available message is ordered
    by its creation time.

    `has_more` says whether a conversation follows the page; with an offset it is the
    same question about the page, so paging through the list never reports a next page
    that is empty. The Telegram dialogs list uses the offset; the website uses 0."""
    if offset < 0:
        raise ValueError("offset must not be negative")
    last_message_at = _last_message_at_subquery(now)
    rows = session.execute(
        sa.select(Conversation, _first_question_subquery(now), last_message_at)
        .where(Conversation.user_id == user_id)
        .order_by(activity_at(now).desc(), Conversation.id.desc())
        .limit(limit + 1)
        .offset(offset)
    ).all()
    return ConversationPage(
        [_summary(row[0], row[1], row[2]) for row in rows[:limit]],
        has_more=len(rows) > limit,
    )


def count_conversations(session: OrmSession, user_id: UUID) -> int:
    """How many conversations the user has (empty ones included)."""
    return session.scalar(
        sa.select(sa.func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
    ) or 0


def conversation_summary(
    session: OrmSession, user_id: UUID, conversation_id: UUID, *, now: datetime
) -> Optional[ConversationSummary]:
    """One of the user's conversations as the list shows it (title included), or None
    when it is unknown or belongs to somebody else: the two are not told apart."""
    row = session.execute(
        sa.select(Conversation, _first_question_subquery(now), _last_message_at_subquery(now)).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    ).one_or_none()
    return None if row is None else _summary(row[0], row[1], row[2])


def _summary(
    conversation: Conversation,
    first_question: Optional[str] = None,
    last_message_at: Optional[datetime] = None,
) -> ConversationSummary:
    # A stored title of only whitespace cannot come from the API; treat such a legacy
    # value as absent rather than show an empty name.
    stored = conversation.title if conversation.title is not None and conversation.title.strip() else None
    derived = None if stored is not None else conversation_title(first_question)
    if stored is not None:
        origin = TITLE_ORIGIN_MANUAL
    elif derived is not None:
        origin = TITLE_ORIGIN_DERIVED
    else:
        origin = TITLE_ORIGIN_NONE
    return ConversationSummary(
        conversation.id,
        stored if stored is not None else derived,
        conversation.created_at,
        conversation.updated_at,
        last_message_at,
        origin,
        origin == TITLE_ORIGIN_MANUAL,
    )


def owns_conversation(session: OrmSession, user_id: UUID, conversation_id: UUID) -> bool:
    return (
        session.scalar(
            sa.select(Conversation.id).where(
                Conversation.id == conversation_id, Conversation.user_id == user_id
            )
        )
        is not None
    )


# --- history, as shown ---------------------------------------------------------------


def _shown_status(turn_status: Optional[str], lease_expires_at: Optional[datetime], now: datetime) -> str:
    if turn_status is None or turn_status == TurnStatus.COMPLETED.value:
        return MESSAGE_COMPLETED
    if turn_status in (TurnStatus.PROCESSING.value, TurnStatus.COMMITTING.value):
        if lease_expires_at is not None and lease_expires_at > now:
            return MESSAGE_IN_PROGRESS
    return MESSAGE_FAILED


def list_messages(
    session: OrmSession,
    user_id: UUID,
    conversation_id: UUID,
    *,
    now: datetime,
    limit: int,
    before: Optional[int] = None,
) -> MessagePage:
    """The conversation's visible messages, newest `limit` of them (older than the id
    `before`, when given), oldest first. ConversationNotFound if it is not the user's.

    Visible: a user or assistant message that has not expired. The user messages of
    failed requests are included, with status "failed": they are history (and what the
    person retries); it is the context sent to the AI that leaves them out
    (build_context). A system note is a marker for the application, never shown.
    """
    if not owns_conversation(session, user_id, conversation_id):
        raise ConversationNotFound
    query = (
        sa.select(
            Message.id,
            Message.role,
            Message.content,
            Message.created_at,
            TurnRequest.status,
            TurnRequest.lease_expires_at,
            TurnRequest.idempotency_key,
            TurnRequest.error_code,
        )
        .outerjoin(TurnRequest, TurnRequest.user_message_id == Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.expires_at > now,
            Message.role.in_((MessageRole.USER.value, MessageRole.ASSISTANT.value)),
        )
        .order_by(Message.id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        query = query.where(Message.id < before)
    rows = session.execute(query).all()

    shown = rows[:limit]
    photos = att.refs_for_messages(session, [row.id for row in shown], now=now)
    views = []
    for row in shown:
        status = _shown_status(row.status, row.lease_expires_at, now)
        unfinished = row.role == MessageRole.USER.value and status != MESSAGE_COMPLETED
        views.append(
            MessageView(
                id=row.id,
                role=row.role,
                text=row.content,
                created_at=row.created_at,
                status=status,
                idempotency_key=row.idempotency_key if unfinished else None,
                error_code=row.error_code if unfinished and status == MESSAGE_FAILED else None,
                attachment=photos.get(row.id),
            )
        )
    views.reverse()
    return MessagePage(views, has_more=len(rows) > limit)


# --- history, as the AI is shown it --------------------------------------------------


# How many photos of a context the assistant is shown, the newest first: a picture costs far more
# than a sentence, and a conversation about one photo does not need the ones before it. An older photo
# is mentioned in words (PHOTO_NOT_SHOWN), so the assistant knows there was one.
MAX_CONTEXT_IMAGES = 2
PHOTO_NOT_SHOWN = " [К этому сообщению было приложено фото, но здесь оно не показано.]"
PHOTO_WITHOUT_CAPTION = "Человек прислал фото без подписи."


def build_context(
    session: OrmSession,
    conversation_id: UUID,
    current_message_id: int,
    *,
    now: datetime,
    max_messages: int,
    max_images: int = MAX_CONTEXT_IMAGES,
) -> list[dict[str, Any]]:
    """The messages to send to the AI for the user message `current_message_id`:
    at most `max_messages` of them, the current one included and last.

    Eligible, besides the current message, are the messages that are part of the
    conversation as it happened: assistant messages, and user messages whose request
    completed. So the user messages of failed, abandoned and unfinished requests are
    left out (a failed turn is not a turn the assistant ever saw answered), as is
    everything that has expired and everything up to the latest system note (the
    context is "the messages after the latest reset"). What is left alternates user and
    assistant. Only messages older than the current one count: a request that is
    retried later does not see what came after it.

    The window is the newest max_messages - 1 of those, with a leading assistant
    message dropped when the window starts in the middle of an exchange or the exchange
    lost its question to expiry: the AI is never shown an answer without the question
    before it. A plain read.

    A message with a photo (attachments) is shown as its text plus the picture, for the newest
    `max_images` such messages (the current one among them); the content is then a list of parts,
    {"type": "text", "text": ...} and {"type": "image", "content_type": ..., "data": <bytes>},
    which the provider's backend turns into whatever its API takes. Every other message, and
    every older photo, is plain text.
    """
    reset_after = session.scalar(
        sa.select(sa.func.coalesce(sa.func.max(Message.id), 0)).where(
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.SYSTEM_NOTE.value,
            Message.expires_at > now,
        )
    )
    completed_questions = sa.select(TurnRequest.user_message_id).where(
        TurnRequest.conversation_id == conversation_id,
        TurnRequest.status == TurnStatus.COMPLETED.value,
    )
    earlier = session.execute(
        sa.select(Message.id, Message.role, Message.content)
        .where(
            Message.conversation_id == conversation_id,
            Message.id < current_message_id,
            Message.id > reset_after,
            Message.expires_at > now,
            sa.or_(
                Message.role == MessageRole.ASSISTANT.value,
                sa.and_(
                    Message.role == MessageRole.USER.value,
                    Message.id.in_(completed_questions),
                ),
            ),
        )
        .order_by(Message.id.desc())
        .limit(max(max_messages - 1, 0))
    ).all()
    earlier.reverse()
    while earlier and earlier[0].role != MessageRole.USER.value:
        earlier.pop(0)

    current = session.scalar(
        sa.select(Message.content).where(
            Message.id == current_message_id, Message.conversation_id == conversation_id
        )
    )
    wanted = [row.id for row in earlier] + [current_message_id]
    carrying = att.refs_for_messages(session, wanted, now=now)
    shown_photos = att.files_for_messages(
        session, sorted(carrying, reverse=True)[: max(max_images, 0)], now=now
    )

    def entry(role: str, message_id: int, text: str) -> dict[str, Any]:
        if message_id not in carrying:
            return {"role": role, "content": text}
        photo = shown_photos.get(message_id)
        if photo is None:
            return {"role": role, "content": text + PHOTO_NOT_SHOWN}
        caption = PHOTO_WITHOUT_CAPTION if text == att.PHOTO_PLACEHOLDER else text
        return {
            "role": role,
            "content": [
                {"type": "text", "text": caption},
                {"type": "image", "content_type": photo.content_type, "data": photo.data},
            ],
        }

    context = [entry(row.role, row.id, row.content) for row in earlier]
    context.append(entry(MessageRole.USER.value, current_message_id, current))
    return context
