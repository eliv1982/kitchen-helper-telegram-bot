"""Conversations and their messages, as the web chat API stores and reads them.

Ownership. Every function takes the user id of the server-side session and finds a
conversation only through it: an id that does not exist, that belongs to somebody
else or that is malformed is the same answer (None, or ConversationNotFound), so
nothing tells the caller which of them it was. This module never reads a user id from
anywhere else, and a conversation's owner is never changed.

Reads write nothing. list_conversations, list_messages and build_context are plain
SELECTs: they take no write lock (a reader never waits for a writer, nor the writer for
it), never move conversations.updated_at (the time of the last activity, which orders
the user's list) and never touch a row. The writers (create_conversation here,
turns.begin_turn, complete_turn and fail_turn) make a WRITE their first statement, the
repository's rule for SQLite's deferred BEGIN (see persistence.database).

Retention. A message is "there" only while its expires_at is in the future: every read
below takes `now` from the caller (the application's clock) and leaves an expired
message out, whether or not a purge job has deleted it yet. The conversation itself has
no deadline and no title is ever stored here: a title would be a copy of something the
user said that nothing could expire.

Titles. What the list shows as a conversation's name is made when it is read, from the
first user message that has not expired (conversation_title): the person's own first
words, shortened. It lives exactly as long as that message does. When the message
expires, the next one names the conversation; when none is left the title is None and
the page falls back to the date. (A title stored in conversations.title, if there ever
is one, wins.)

Nothing here commits; the caller owns the transaction (web.database.WebDatabase.run).
Results are plain values, not ORM objects, so they can leave the worker thread.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence import attachments as att
from house_helper.persistence.models import (
    Conversation,
    Message,
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


def list_conversations(
    session: OrmSession, user_id: UUID, *, now: datetime, limit: int
) -> ConversationPage:
    """The user's conversations, most recently active first (ties by id, so the order
    is total), at most `limit` of them, each named (see "Titles", above) by its first
    user message that has not expired at `now`."""
    first_question = (
        sa.select(Message.content)
        .where(
            Message.conversation_id == Conversation.id,
            Message.role == MessageRole.USER.value,
            Message.expires_at > now,
        )
        .order_by(Message.id)
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )
    rows = session.execute(
        sa.select(Conversation, first_question)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit + 1)
    ).all()
    return ConversationPage(
        [_summary(row[0], row[1]) for row in rows[:limit]], has_more=len(rows) > limit
    )


def _summary(conversation: Conversation, first_question: Optional[str] = None) -> ConversationSummary:
    return ConversationSummary(
        conversation.id,
        conversation.title or conversation_title(first_question),
        conversation.created_at,
        conversation.updated_at,
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
