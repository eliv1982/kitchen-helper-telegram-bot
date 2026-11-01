"""The structured recipes of an assistant answer: where they are stored and how they are read.

The protocol is house_helper.core.recipes: an answer may carry several recipes, each in a
delimited JSON block. This module is the persistence half -- it runs the accepted parser
over an answer that has already been stored as a message and keeps every recipe it
accepted, in the order the answer gave them. It makes no provider call and no other
outside call; the parser is pure.

Identity is (message_id, position). position is the zero-based place of the recipe among
the accepted recipes of that answer, and it is what a person names when they save one of
them as a favourite. The protocol's content fingerprint (Recipe.recipe_id) is not stored:
it is a hash of the fields, not a key, and editing a favourite would make it meaningless.

Ownership. There is no user column here. A recipe belongs to an assistant message, which
belongs to a conversation, which belongs to a user; every read that serves a request takes
the user id of the server-side session and joins through the conversation, so a recipe of
somebody else's answer is not found. The supported writer, record_recipes_for_message, is
run by the code that owns the message (a later increment stores the recipes when the
answer is committed; this stage does not call it from the turn runner).

Retention. The parent is an assistant message and the foreign key is ON DELETE CASCADE:
a recipe is deleted with its message when the 365-day retention expires it, when its
conversation is purged, and when its user is deleted. A favourite made from a recipe is a
copy with no link here, so it is not affected (favorites.py).

What is stored. The validated fields of the protocol: title, the servings phrase (NULL
when the answer named none), the ordered ingredients (name and amount) and the ordered
steps. The conversational text stays in messages.content; it is not duplicated here. The
values are validated again by the protocol's own rules (recipes.validate_recipe) when they
are read for a copy, so a row hand-edited in the database cannot become a favourite that
the protocol would not have produced.

Writers write first, as everywhere in this layer (see persistence.database):
record_recipes_for_message starts with a DELETE (it replaces the recipes of its message,
so running it twice for one answer leaves one set). Reads write nothing. Nothing here
commits; the caller owns the transaction. Results are plain values, not ORM objects.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.core.recipes import Ingredient, parse_reply, validate_recipe
from house_helper.persistence.models import (
    Conversation,
    Message,
    MessageRecipe,
    MessageRole,
)


class MessageNotFound(Exception):
    """No such assistant message for this user: unknown, somebody else's, of the wrong
    role, or expired (not told apart)."""


class RecipeNotFound(Exception):
    """The answer has no accepted recipe at that position."""


class NotAnAnswer(Exception):
    """The message the recipes would belong to does not exist or is not the assistant's."""


@dataclass(frozen=True)
class StoredRecipe:
    """One validated recipe of an answer, as it is stored and as a copy reads it."""

    message_id: int
    position: int
    title: str
    servings: Optional[str]
    ingredients: tuple[Ingredient, ...]
    steps: tuple[str, ...]
    created_at: datetime


def _ingredients(value: Any) -> tuple[Ingredient, ...]:
    return tuple(Ingredient(item["name"], item["amount"]) for item in value)


def _stored(row: Any) -> StoredRecipe:
    return StoredRecipe(
        message_id=row.message_id,
        position=row.position,
        title=row.title,
        servings=row.servings,
        ingredients=_ingredients(row.ingredients),
        steps=tuple(row.steps),
        created_at=row.created_at,
    )


def _checked(recipe: StoredRecipe) -> StoredRecipe:
    """The same recipe after the protocol's rules. ValueError if a stored row could not
    have come from the protocol (a hand-edited database); the caller's transaction rolls
    back and nothing is copied."""
    checked = validate_recipe(
        recipe.position, recipe.title, recipe.servings, recipe.ingredients, recipe.steps
    )
    return StoredRecipe(
        message_id=recipe.message_id,
        position=recipe.position,
        title=checked.title,
        servings=checked.servings,
        ingredients=checked.ingredients,
        steps=checked.steps,
        created_at=recipe.created_at,
    )


def record_recipes_for_message(
    session: OrmSession, message_id: int, text: str, *, now: datetime
) -> tuple[StoredRecipe, ...]:
    """Parse `text` and replace the recipes of the assistant message `message_id` with them.

    An answer with no accepted recipe leaves none (and clears any earlier set, so a retry
    of the same completion does not duplicate or keep stale rows). Invalid blocks are not
    stored; the parser's notices are for the person, not the database. NotAnAnswer if the
    message does not exist or is not an assistant message. The DELETE is the first
    statement. The caller owns the transaction and has established that the message is
    the user's; this function has no user argument because a recipe has no owner.
    """
    session.execute(
        sa.delete(MessageRecipe)
        .where(MessageRecipe.message_id == message_id)
        .execution_options(synchronize_session=False)
    )
    role = session.scalar(sa.select(Message.role).where(Message.id == message_id))
    if role != MessageRole.ASSISTANT.value:
        raise NotAnAnswer
    parsed = parse_reply(text)
    rows = [
        MessageRecipe(
            message_id=message_id,
            position=recipe.index,
            title=recipe.title,
            servings=recipe.servings,
            ingredients=[
                {"name": ingredient.name, "amount": ingredient.amount}
                for ingredient in recipe.ingredients
            ],
            steps=list(recipe.steps),
            created_at=now,
        )
        for recipe in parsed.recipes
    ]
    session.add_all(rows)
    session.flush()
    return tuple(
        StoredRecipe(
            message_id=message_id,
            position=recipe.index,
            title=recipe.title,
            servings=recipe.servings,
            ingredients=recipe.ingredients,
            steps=recipe.steps,
            created_at=now,
        )
        for recipe in parsed.recipes
    )


def recipe_at(session: OrmSession, message_id: int, position: int) -> StoredRecipe:
    """The stored recipe at (message_id, position), or RecipeNotFound.

    No ownership check: the supported callers (favorites.save_favorite) have already
    found the message through the user's conversation, under its lock. A request-facing
    read uses recipes_for_message or get_recipe_for_user.
    """
    row = session.execute(
        sa.select(
            MessageRecipe.message_id,
            MessageRecipe.position,
            MessageRecipe.title,
            MessageRecipe.servings,
            MessageRecipe.ingredients,
            MessageRecipe.steps,
            MessageRecipe.created_at,
        ).where(
            MessageRecipe.message_id == message_id, MessageRecipe.position == position
        )
    ).first()
    if row is None:
        raise RecipeNotFound
    return _stored(row)


def _owned_message(session: OrmSession, user_id: UUID, message_id: int, *, now: datetime) -> int:
    found = session.scalar(
        sa.select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.id == message_id,
            Message.role == MessageRole.ASSISTANT.value,
            Message.expires_at > now,
            Conversation.user_id == user_id,
        )
    )
    if found is None:
        raise MessageNotFound
    return found


def recipes_for_message(
    session: OrmSession, user_id: UUID, message_id: int, *, now: datetime
) -> tuple[StoredRecipe, ...]:
    """Every stored recipe of the user's unexpired assistant message, in answer order.

    MessageNotFound if the message is not the user's, is not an assistant's or has
    expired. A message with no stored recipe answers with an empty tuple: the answer
    carried none (or none has been extracted yet). A plain read.
    """
    _owned_message(session, user_id, message_id, now=now)
    rows = session.execute(
        sa.select(
            MessageRecipe.message_id,
            MessageRecipe.position,
            MessageRecipe.title,
            MessageRecipe.servings,
            MessageRecipe.ingredients,
            MessageRecipe.steps,
            MessageRecipe.created_at,
        )
        .where(MessageRecipe.message_id == message_id)
        .order_by(MessageRecipe.position)
    ).all()
    return tuple(_checked(_stored(row)) for row in rows)


def get_recipe_for_user(
    session: OrmSession, user_id: UUID, message_id: int, position: int, *, now: datetime
) -> StoredRecipe:
    """One recipe of the user's unexpired assistant message (MessageNotFound,
    RecipeNotFound as above)."""
    _owned_message(session, user_id, message_id, now=now)
    return _checked(recipe_at(session, message_id, position))


def message_ids_with_recipes(
    session: OrmSession, message_ids: Iterable[int]
) -> set[int]:
    """Which of `message_ids` have at least one stored recipe. The caller has already
    found the messages through the user's conversation, so this makes no ownership
    decision of its own. A plain read."""
    ids = list(message_ids)
    if not ids:
        return set()
    found = session.scalars(
        sa.select(MessageRecipe.message_id).where(MessageRecipe.message_id.in_(ids)).distinct()
    ).all()
    return set(found)


__all__ = [
    "MessageNotFound",
    "NotAnAnswer",
    "RecipeNotFound",
    "StoredRecipe",
    "get_recipe_for_user",
    "message_ids_with_recipes",
    "recipe_at",
    "recipes_for_message",
    "record_recipes_for_message",
]
