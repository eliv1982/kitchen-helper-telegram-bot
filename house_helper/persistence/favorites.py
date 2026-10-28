"""Favorite recipes: answers of the assistant that a user chose to keep.

A favorite is a COPY of an answer, made on request and kept apart from the conversation it
came from (models.FavoriteRecipe says why): it has no deadline, no link to a message and
no connection to the history's retention, so it survives the expiry of the answer, the
purge of the conversation and the deletion of both. The text is the answer exactly as the
chat stored it, never parsed, rewritten or summarised; the title is a label made from its
first line (favorite_title) and the only thing here that is derived.

Ownership. Every function takes the user id of the server-side session and finds a row
only through it. A favorite that does not exist, belongs to somebody else or was removed
is one answer (FavoriteNotFound), and so is a conversation that is not the user's
(ConversationNotFound, as in conversations.py): nothing tells the caller which. The
owner is never read from anywhere else and never changed.

What may be saved. An assistant message of a conversation of the user's that has not
expired: the caller names the conversation and the message, and the text is read from the
database, never taken from the request. (Whether the answer "is a recipe" is not decided:
there is no parsing, and the person chooses.) Saving the same message again is not an
error and not a second row: it answers the favorite that exists (SavedFavorite.created is
False). A user keeps at most FAVORITES_LIMIT of them. Everything else the user creates
expires, and this does not, so it needs a bound of its own; the count is taken under the
conversation's lock, and two saves from two conversations at the same instant may overshoot
it by one, which is harmless.

Writers write first, as everywhere in this layer (see persistence.database): save_favorite
starts with the UPDATE that decides the conversation's ownership and locks it (the same
statement turns.begin_turn uses), remove_favorite is a DELETE. Reads write nothing.
Nothing here commits; the caller owns the transaction (web.database.WebDatabase.run).
Results are plain values, not ORM objects.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.conversations import ConversationNotFound
from house_helper.persistence.models import Conversation, FavoriteRecipe, Message, MessageRole

# How many favorites one user keeps at most.
FAVORITES_LIMIT = 100

# The longest title, in characters (code points) before the ellipsis; the column holds 200.
TITLE_LENGTH = 80
TITLE_ELLIPSIS = "…"
UNTITLED = "Рецепт"

# What a title is stripped of: the marks of Markdown headings, emphasis, quotes and list
# bullets at its start, and emphasis marks and a colon at its end ("Вот рецепт борща:").
_LEADING_MARKS = "#*_`>-–—•· "
_TRAILING_MARKS = "*_`:; "


class FavoriteNotFound(Exception):
    """No such favorite for this user (unknown, somebody else's, removed: not told apart)."""


class MessageNotFound(Exception):
    """The user's conversation has no such answer to keep: no message with that id, one
    that is not the assistant's, or one that has expired."""


class FavoritesFull(Exception):
    """The user already keeps FAVORITES_LIMIT favorites."""


@dataclass(frozen=True)
class FavoriteSummary:
    id: UUID
    title: str
    created_at: datetime
    source_message_id: int


@dataclass(frozen=True)
class FavoriteDetail:
    id: UUID
    title: str
    text: str
    created_at: datetime
    source_message_id: int


@dataclass(frozen=True)
class SavedFavorite:
    favorite: FavoriteSummary
    created: bool  # False: this answer was already a favorite, and this is it


def favorite_title(text: str) -> str:
    """A label for the answer `text`: its first non-empty line, on one line, stripped of
    Markdown marks and cut at TITLE_LENGTH characters with an ellipsis when longer.
    UNTITLED when no line has anything but marks."""
    for raw in text.splitlines():
        line = " ".join(raw.split()).lstrip(_LEADING_MARKS).rstrip(_TRAILING_MARKS)
        if not line:
            continue
        if len(line) <= TITLE_LENGTH:
            return line
        return line[:TITLE_LENGTH].rstrip() + TITLE_ELLIPSIS
    return UNTITLED


def _summary(row: FavoriteRecipe) -> FavoriteSummary:
    return FavoriteSummary(row.id, row.title, row.created_at, row.source_message_id)


def _existing(session: OrmSession, user_id: UUID, message_id: int) -> Optional[FavoriteRecipe]:
    return session.scalars(
        sa.select(FavoriteRecipe).where(
            FavoriteRecipe.user_id == user_id, FavoriteRecipe.source_message_id == message_id
        )
    ).first()


def save_favorite(
    session: OrmSession, user_id: UUID, conversation_id: UUID, message_id: int, *, now: datetime
) -> SavedFavorite:
    """Keep the assistant's message `message_id` of the user's conversation.

    ConversationNotFound if the conversation is not the user's, MessageNotFound if it
    holds no such assistant message that has not expired at `now`, FavoritesFull at the
    limit. The first statement is the write that locks the conversation.
    """
    locked = session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        .values(updated_at=Conversation.updated_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if locked != 1:
        raise ConversationNotFound

    text = session.scalar(
        sa.select(Message.content).where(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.ASSISTANT.value,
            Message.expires_at > now,
        )
    )
    if not text:
        raise MessageNotFound

    existing = _existing(session, user_id, message_id)
    if existing is not None:
        return SavedFavorite(_summary(existing), created=False)
    kept = session.scalar(
        sa.select(sa.func.count()).select_from(FavoriteRecipe).where(FavoriteRecipe.user_id == user_id)
    )
    if kept >= FAVORITES_LIMIT:
        raise FavoritesFull

    row = FavoriteRecipe(
        user_id=user_id,
        title=favorite_title(text),
        content=text,
        source_message_id=message_id,
        created_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except sa.exc.IntegrityError:
        # Another writer kept the same answer between the look and the insert (the
        # conversation lock makes that impossible here, and this keeps it harmless
        # elsewhere); the savepoint took the attempt back. If that is not what
        # happened, the error is something else.
        existing = _existing(session, user_id, message_id)
        if existing is None:
            raise
        return SavedFavorite(_summary(existing), created=False)
    return SavedFavorite(_summary(row), created=True)


def list_favorites(session: OrmSession, user_id: UUID) -> list[FavoriteSummary]:
    """The user's favorites, the newest first (ties by id, so the order is total), without
    their text. At most FAVORITES_LIMIT of them."""
    rows = session.scalars(
        sa.select(FavoriteRecipe)
        .where(FavoriteRecipe.user_id == user_id)
        .order_by(FavoriteRecipe.created_at.desc(), FavoriteRecipe.id.desc())
        .limit(FAVORITES_LIMIT)
    ).all()
    return [_summary(row) for row in rows]


def get_favorite(session: OrmSession, user_id: UUID, favorite_id: UUID) -> FavoriteDetail:
    """One favorite of the user with its text. FavoriteNotFound if it is not theirs."""
    row = session.scalars(
        sa.select(FavoriteRecipe).where(
            FavoriteRecipe.id == favorite_id, FavoriteRecipe.user_id == user_id
        )
    ).first()
    if row is None:
        raise FavoriteNotFound
    return FavoriteDetail(row.id, row.title, row.content, row.created_at, row.source_message_id)


def remove_favorite(session: OrmSession, user_id: UUID, favorite_id: UUID) -> None:
    """Remove the user's favorite. FavoriteNotFound if it is not theirs (or already gone).
    The statement is a write, and it is the first."""
    removed = session.execute(
        sa.delete(FavoriteRecipe)
        .where(FavoriteRecipe.id == favorite_id, FavoriteRecipe.user_id == user_id)
        .execution_options(synchronize_session=False)
    ).rowcount
    if removed != 1:
        raise FavoriteNotFound
