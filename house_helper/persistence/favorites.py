"""Favorite recipes: answers, and single recipes of answers, that a user chose to keep.

A favorite is a COPY, made on request and kept apart from the conversation it came from
(models.FavoriteRecipe says why): it has no deadline and no foreign key to a message, so
it survives the expiry of the answer, the purge of the conversation and the deletion of
both. Two kinds share this table:

  * a text favorite (the original kind) copies the answer exactly as the chat stored it,
    never parsed, rewritten or summarised. Its title is a label made from its first line
    (favorite_title) and the only thing derived from the answer.
  * a structured favorite copies ONE validated recipe of an answer, named by its origin
    (message_id, recipe_index), out of persistence.message_recipes: title, servings,
    ingredients and steps. It has its own row and its own id, and no reference to the
    message recipe, so editing it never changes the conversation or message_recipes.

The content of a favorite. One function, favorite_content, is the contract:

  * text kind: content is the text the person saved, and nothing else. An edit of the
    text stores the new text exactly (no spelling fix, no AI rewrite, no re-wrapping);
    an edit of the title or of the notes does not touch content.
  * structured kind: content is the accepted renderer's plain text of the validated
    fields (core.recipes.render_recipe: title, servings phrase, "Ингредиенты", steps), and,
    when the personal note is not empty, one blank line, the line "Мои заметки:" and the
    note as it was written. It is rendered again from the validated fields on every
    edit, so it always matches them.

Content is what GET returns as "text" and what a later export or share would print; the
structured fields stay the source of truth for a structured favorite.

Ownership. Every function takes the user id of the server-side session and finds a row
only through it. A favorite that does not exist, belongs to somebody else or was removed
is one answer (FavoriteNotFound), and so is a conversation that is not the user's
(ConversationNotFound, as in conversations.py): nothing tells the caller which. The
owner is never read from anywhere else and never changed.

What may be saved. An assistant message of a conversation of the user's that has not
expired: the caller names the conversation and the message, and the text is read from the
database, never taken from the request. With a recipe_index, the named structured recipe
of that message is copied instead; it is read from message_recipes and must still exist
(RecipeNotFound). Saving the same message again (the same kind at the same position) is
not an error and not a second row: it answers the favorite that exists
(SavedFavorite.created is False). A user keeps at most FAVORITES_LIMIT favorites, both
kinds together. A Telegram save button goes through save_recipe_for_telegram, which
resolves the sender's account to that user inside the same transaction and then calls
save_favorite; it is not a second saving rule. Everything else the user creates expires, and this does not, so it needs a
bound of its own; the count is taken under the conversation's lock, and two saves from two
conversations at the same instant may overshoot it by one, which is harmless.

Editing. update_favorite changes only the fields a person may edit (the title, the text of
a text favorite, the servings, ingredients and steps of a structured one, and the personal
notes), and only for the owner. Every edit names the version it started from; the update
is one conditional statement matching id, owner and version, and increments version. A
stale version changes nothing and raises FavoriteConflict; the caller answers 409
favorite_conflict. updated_at is informational and never decides a conflict. Ownership,
source message identity, recipe position, kind and created_at cannot be edited.

Validation. Text and notes are stored as written, but bounded: no control character that
cannot be stored everywhere (tab, newline and carriage return are ordinary), no lone
surrogate, and within MAX_FAVORITE_TEXT_LENGTH / MAX_FAVORITE_NOTES_LENGTH. A title is a
single-line label, trimmed. The structured fields are validated by the protocol's own
rules (core.recipes.validate_recipe), without spelling them, so an edited recipe is exactly
the recipe the protocol would have accepted and is kept as entered.

Writers write first, as everywhere in this layer (see persistence.database): save_favorite
starts with the UPDATE that decides the conversation's ownership and locks it (the same
statement turns.begin_turn uses), update_favorite starts with a no-op UPDATE that locks
the favorite row, and remove_favorite is a DELETE. Reads write nothing. Nothing here
commits; the caller owns the transaction (web.database.WebDatabase.run). Results are plain
values, not ORM objects.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.core.recipes import (
    Ingredient,
    MAX_RECIPES_PER_REPLY,
    RECIPE_END,
    RECIPE_START,
    Recipe,
    render_recipe,
    validate_recipe,
)
from house_helper.persistence import message_recipes
from house_helper.persistence import telegram as telegram_store
from house_helper.persistence.conversations import ConversationNotFound
from house_helper.persistence.message_recipes import MessageNotFound, RecipeNotFound
from house_helper.persistence.models import (
    LEGACY_RECIPE_INDEX,
    MAX_FAVORITE_NOTES_LENGTH,
    Conversation,
    FavoriteKind,
    FavoriteRecipe,
    Message,
    MessageRole,
)

# How many favorites one user keeps at most, both kinds counted together.
FAVORITES_LIMIT = 100

# The longest title, in characters (code points) before the ellipsis; the column holds 200.
TITLE_LENGTH = 80
TITLE_ELLIPSIS = "…"
UNTITLED = "Рецепт"

# The longest text a legacy favorite may be edited to. It is a bound on one API edit, not
# on what can be saved from an answer (which is the answer's own, already stored, text).
MAX_FAVORITE_TEXT_LENGTH = 100_000

# The column is a 32-bit integer on PostgreSQL. The largest acceptable expected version is
# one whose successor still fits; a larger one can never be current, and refusing it here
# keeps a huge JSON integer from reaching the driver (SQLite raises an overflow, which
# would be a server error, not a bad request).
MAX_FAVORITE_VERSION = 2**31 - 2

# The largest message id a Telegram button may name (the same bound as the history's
# ?before= and the favorites API's message_id). A larger number cannot be a message.
MAX_MESSAGE_ID = 2**62

# What a structured favorite's content calls the personal note (the rendering contract).
NOTES_HEADING = "Мои заметки:"

# The fields an edit may name. What each kind actually accepts is decided after the row
# is read (kind cannot be changed, and a text favorite has no structured fields).
_EDITABLE_FIELDS = frozenset({"title", "text", "notes", "servings", "ingredients", "steps"})
_TEXT_FIELDS = frozenset({"title", "text", "notes"})
_STRUCTURED_FIELDS = frozenset({"title", "servings", "ingredients", "steps", "notes"})

# What a title is stripped of: the marks of Markdown headings, emphasis, quotes and list
# bullets at its start, and emphasis marks and a colon at its end ("Вот рецепт борща:").
_LEADING_MARKS = "#*_`>-–—•· "
_TRAILING_MARKS = "*_`:; "

# A title is one line: no control character at all.
_TITLE_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")
# The rest of a user's text may hold a tab, a newline and a carriage return, and nothing
# else from the control ranges; a lone surrogate cannot be stored as UTF-8 anywhere.
_UNSTORABLE_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")


class FavoriteNotFound(Exception):
    """No such favorite for this user (unknown, somebody else's, removed: not told apart)."""


class FavoritesFull(Exception):
    """The user already keeps FAVORITES_LIMIT favorites."""


class InvalidFavorite(ValueError):
    """An edit or a save names a field the favorite does not have, or a value that cannot
    be stored. The caller answers 400 invalid_request; nothing is written."""


class FavoriteConflict(Exception):
    """The edit started from a version that is no longer current. Nothing was written."""


class NotBound(Exception):
    """The Telegram sender is no longer tied to an enrolled user: nothing was written."""


@dataclass(frozen=True)
class FavoriteSummary:
    id: UUID
    title: str
    created_at: datetime
    source_message_id: int
    kind: str
    source_recipe_index: int
    version: int


@dataclass(frozen=True)
class FavoriteDetail:
    id: UUID
    title: str
    text: str
    created_at: datetime
    updated_at: datetime
    source_message_id: int
    kind: str
    source_recipe_index: int
    version: int
    notes: Optional[str]
    servings: Optional[str]
    ingredients: Optional[tuple[Ingredient, ...]]
    steps: Optional[tuple[str, ...]]


@dataclass(frozen=True)
class SavedFavorite:
    favorite: FavoriteSummary
    created: bool  # False: this answer (or this recipe of it) was already a favorite


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


# --- the content contract ------------------------------------------------------------


def favorite_content(
    *,
    kind: str,
    title: str,
    text: Optional[str],
    notes: Optional[str],
    servings: Optional[str],
    ingredients: Optional[Iterable[Ingredient]],
    steps: Optional[Iterable[str]],
) -> str:
    """The canonical plain text of a favorite; see the module docstring.

    For a text favorite it is `text` unchanged. For a structured one it is the accepted
    renderer's text of the fields, followed by the personal note (if any) under one
    heading. Every path that creates or edits a structured favorite goes through here.
    """
    if kind == FavoriteKind.TEXT.value:
        if text is None:
            raise ValueError("a text favorite needs its text")
        return text
    recipe = Recipe(
        index=0,
        title=title,
        servings=servings,
        ingredients=tuple(ingredients or ()),
        steps=tuple(steps or ()),
    )
    content = render_recipe(recipe)
    if notes:
        content = f"{content}\n\n{NOTES_HEADING}\n{notes}"
    return content


# --- what an edit may contain ----------------------------------------------------------


def _require_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidFavorite(f"{field} must be a string")
    if _UNSTORABLE_TEXT.search(value) is not None:
        raise InvalidFavorite(f"{field} contains a character that cannot be stored")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidFavorite(f"{field} is not valid Unicode text") from error
    return value


def _validated_title(value: object) -> str:
    """A title: trimmed, one line, 1 to 200 characters, no protocol marker."""
    if not isinstance(value, str):
        raise InvalidFavorite("title must be a string")
    title = value.strip()
    if not title:
        raise InvalidFavorite("title must not be empty")
    if len(title) > 200:
        raise InvalidFavorite("title is too long")
    if _TITLE_CONTROL.search(title) is not None:
        raise InvalidFavorite("title must be one line")
    if RECIPE_START in title or RECIPE_END in title:
        raise InvalidFavorite("title contains a protocol marker")
    try:
        title.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidFavorite("title is not valid Unicode text") from error
    return title


def _validated_text(value: object) -> str:
    """The full text of a text favorite: as written (not trimmed), non-empty, bounded."""
    text = _require_text(value, field="text")
    if not text.strip():
        raise InvalidFavorite("text must not be empty")
    if len(text) > MAX_FAVORITE_TEXT_LENGTH:
        raise InvalidFavorite("text is too long")
    return text


def _validated_notes(value: object) -> Optional[str]:
    """A personal note: None, or text as written (not trimmed), bounded."""
    if value is None:
        return None
    notes = _require_text(value, field="notes")
    if len(notes) > MAX_FAVORITE_NOTES_LENGTH:
        raise InvalidFavorite("notes are too long")
    return notes


def _validated_version(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_FAVORITE_VERSION
    ):
        raise InvalidFavorite("expected_version must be a positive whole number")
    return value


def _validated_recipe_index(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < MAX_RECIPES_PER_REPLY
    ):
        raise InvalidFavorite("recipe_index must be an accepted recipe position")
    return value


# --- reading rows ---------------------------------------------------------------------


def _summary(row: FavoriteRecipe) -> FavoriteSummary:
    return FavoriteSummary(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        source_message_id=row.source_message_id,
        kind=row.kind,
        source_recipe_index=row.source_recipe_index,
        version=row.version,
    )


def _ingredients(value: Any) -> Optional[tuple[Ingredient, ...]]:
    if value is None:
        return None
    return tuple(Ingredient(item["name"], item["amount"]) for item in value)


def _steps(value: Any) -> Optional[tuple[str, ...]]:
    if value is None:
        return None
    return tuple(value)


def _detail(row: FavoriteRecipe) -> FavoriteDetail:
    return FavoriteDetail(
        id=row.id,
        title=row.title,
        text=row.content,
        created_at=row.created_at,
        updated_at=row.updated_at,
        source_message_id=row.source_message_id,
        kind=row.kind,
        source_recipe_index=row.source_recipe_index,
        version=row.version,
        notes=row.notes,
        servings=row.servings,
        ingredients=_ingredients(row.ingredients),
        steps=_steps(row.steps),
    )


def _existing(
    session: OrmSession, user_id: UUID, message_id: int, source_recipe_index: int
) -> Optional[FavoriteRecipe]:
    return session.scalars(
        sa.select(FavoriteRecipe).where(
            FavoriteRecipe.user_id == user_id,
            FavoriteRecipe.source_message_id == message_id,
            FavoriteRecipe.source_recipe_index == source_recipe_index,
        )
    ).first()


# --- saving ---------------------------------------------------------------------------


def save_favorite(
    session: OrmSession,
    user_id: UUID,
    conversation_id: UUID,
    message_id: int,
    *,
    now: datetime,
    recipe_index: Optional[int] = None,
) -> SavedFavorite:
    """Keep the assistant's message `message_id` of the user's conversation, or one of its
    structured recipes when `recipe_index` is given.

    ConversationNotFound if the conversation is not the user's, MessageNotFound if it
    holds no such assistant message that has not expired at `now`, RecipeNotFound if the
    message has no accepted recipe at `recipe_index`, FavoritesFull at the limit. The
    first statement is the write that locks the conversation.
    """
    source_recipe_index = (
        LEGACY_RECIPE_INDEX if recipe_index is None else _validated_recipe_index(recipe_index)
    )
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

    existing = _existing(session, user_id, message_id, source_recipe_index)
    if existing is not None:
        return SavedFavorite(_summary(existing), created=False)

    if source_recipe_index == LEGACY_RECIPE_INDEX:
        kind = FavoriteKind.TEXT.value
        title = favorite_title(text)
        content = text
        servings: Optional[str] = None
        ingredients: Optional[list] = None
        steps: Optional[list] = None
    else:
        stored = message_recipes.recipe_at(session, message_id, source_recipe_index)
        checked = validate_recipe(
            stored.position, stored.title, stored.servings, stored.ingredients, stored.steps
        )
        kind = FavoriteKind.STRUCTURED.value
        title = checked.title
        servings = checked.servings
        ingredients = [
            {"name": ingredient.name, "amount": ingredient.amount}
            for ingredient in checked.ingredients
        ]
        steps = list(checked.steps)
        content = favorite_content(
            kind=kind,
            title=title,
            text=None,
            notes=None,
            servings=servings,
            ingredients=checked.ingredients,
            steps=checked.steps,
        )

    kept = session.scalar(
        sa.select(sa.func.count()).select_from(FavoriteRecipe).where(FavoriteRecipe.user_id == user_id)
    )
    if kept >= FAVORITES_LIMIT:
        raise FavoritesFull

    row = FavoriteRecipe(
        user_id=user_id,
        title=title,
        content=content,
        source_message_id=message_id,
        kind=kind,
        source_recipe_index=source_recipe_index,
        servings=servings,
        ingredients=ingredients,
        steps=steps,
        notes=None,
        version=1,
        created_at=now,
        updated_at=now,
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
        existing = _existing(session, user_id, message_id, source_recipe_index)
        if existing is None:
            raise
        return SavedFavorite(_summary(existing), created=False)
    return SavedFavorite(_summary(row), created=True)


def save_recipe_for_telegram(
    session: OrmSession,
    telegram_user_id: int,
    message_id: int,
    *,
    recipe_index: int,
    now: datetime,
) -> SavedFavorite:
    """Save one structured recipe of a Telegram button, for the user the sender is tied to.

    The caller names the Telegram account (the Bot API's sender id) and the recipe's origin
    (message_id, recipe_index) -- never a user id, a conversation or a title: those are read
    from the database. The account is resolved by the same binding every Telegram message
    uses (telegram.enrolled_user_for_telegram), inside this transaction and with the link
    held until it ends, so an unlink or a re-link that races the click is decided before or
    after this save rather than in its middle. The message is then found through that
    user's conversation by save_favorite, which is the same ownership rule the Web API
    uses: a callback that names somebody else's message, or a message that has expired,
    saves nothing and is told so by the caller.

    Writers write first: the first statement is the UPDATE that locks the conversation of
    the named message (it is also what establishes that the message exists and has not
    expired). NotBound when the account is not tied to an enrolled user; MessageNotFound
    when the message is not there or has expired; otherwise the exceptions and the result
    of save_favorite (RecipeNotFound, FavoritesFull, SavedFavorite with created False for
    a repeated click). This function makes no provider call: the recipe was already parsed
    and stored with the answer. The caller owns the transaction.
    """
    if (
        isinstance(message_id, bool)
        or not isinstance(message_id, int)
        or not 1 <= message_id <= MAX_MESSAGE_ID
    ):
        raise MessageNotFound
    position = _validated_recipe_index(recipe_index)

    # The write that locks the conversation of the named message, and the only statement
    # that decides whether it is there at all.
    located = (
        sa.select(Message.conversation_id)
        .where(
            Message.id == message_id,
            Message.role == MessageRole.ASSISTANT.value,
            Message.expires_at > now,
        )
        .scalar_subquery()
    )
    locked = session.execute(
        sa.update(Conversation)
        .where(Conversation.id == located)
        .values(updated_at=Conversation.updated_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if locked != 1:
        raise MessageNotFound

    user_id = telegram_store.enrolled_user_for_telegram(session, telegram_user_id, hold=True)
    if user_id is None:
        raise NotBound

    conversation_id = session.scalar(
        sa.select(Message.conversation_id).where(Message.id == message_id)
    )
    if conversation_id is None:  # removed between the lock and this read
        raise MessageNotFound
    return save_favorite(
        session,
        user_id,
        conversation_id,
        message_id,
        now=now,
        recipe_index=position,
    )


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
    """One favorite of the user with its text and fields. FavoriteNotFound if it is not
    theirs."""
    row = session.scalars(
        sa.select(FavoriteRecipe).where(
            FavoriteRecipe.id == favorite_id, FavoriteRecipe.user_id == user_id
        )
    ).first()
    if row is None:
        raise FavoriteNotFound
    return _detail(row)


def remove_favorite(session: OrmSession, user_id: UUID, favorite_id: UUID) -> None:
    """Remove the user's favorite. FavoriteNotFound if it is not theirs (or already gone).
    The statement is a write, and it is the first. A structured favorite is only this
    row: the message recipe it was copied from is not touched."""
    removed = session.execute(
        sa.delete(FavoriteRecipe)
        .where(FavoriteRecipe.id == favorite_id, FavoriteRecipe.user_id == user_id)
        .execution_options(synchronize_session=False)
    ).rowcount
    if removed != 1:
        raise FavoriteNotFound


# --- editing --------------------------------------------------------------------------


def update_favorite(
    session: OrmSession,
    user_id: UUID,
    favorite_id: UUID,
    *,
    expected_version: object,
    changes: Mapping[str, Any],
    now: datetime,
) -> FavoriteDetail:
    """Change the editable fields of the user's favorite, if `expected_version` is current.

    `changes` holds only the fields the request named: title, notes and text for a text
    favorite; title, notes, servings, ingredients and steps for a structured one. A field
    that is absent is left as it is. FavoriteNotFound if the favorite is not the user's;
    InvalidFavorite for a field the kind does not have or a value that cannot be stored;
    FavoriteConflict if the version is stale. The first statement is a no-op UPDATE that
    locks the row; the decision is the conditional UPDATE at the end, which matches id,
    owner and version and is the only statement that changes anything.
    """
    version = _validated_version(expected_version)
    if not isinstance(changes, Mapping) or not changes:
        raise InvalidFavorite("an edit needs at least one field")
    unexpected = set(changes) - _EDITABLE_FIELDS
    if unexpected:
        raise InvalidFavorite("a field cannot be edited")

    locked = session.execute(
        sa.update(FavoriteRecipe)
        .where(FavoriteRecipe.id == favorite_id, FavoriteRecipe.user_id == user_id)
        .values(version=FavoriteRecipe.version, updated_at=FavoriteRecipe.updated_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if locked != 1:
        raise FavoriteNotFound

    row = session.execute(
        sa.select(
            FavoriteRecipe.id,
            FavoriteRecipe.user_id,
            FavoriteRecipe.title,
            FavoriteRecipe.content,
            FavoriteRecipe.source_message_id,
            FavoriteRecipe.kind,
            FavoriteRecipe.source_recipe_index,
            FavoriteRecipe.servings,
            FavoriteRecipe.ingredients,
            FavoriteRecipe.steps,
            FavoriteRecipe.notes,
            FavoriteRecipe.version,
            FavoriteRecipe.created_at,
            FavoriteRecipe.updated_at,
        ).where(FavoriteRecipe.id == favorite_id, FavoriteRecipe.user_id == user_id)
    ).first()
    if row is None:  # the lock statement found it: not possible without a deletion by us
        raise FavoriteNotFound

    if row.kind == FavoriteKind.TEXT.value:
        if set(changes) - _TEXT_FIELDS:
            raise InvalidFavorite("a text favorite has no structured fields")
    elif set(changes) - _STRUCTURED_FIELDS:
        raise InvalidFavorite("a structured favorite has no full text")

    title = _validated_title(changes["title"]) if "title" in changes else row.title
    notes = _validated_notes(changes["notes"]) if "notes" in changes else row.notes

    servings = row.servings
    ingredients = row.ingredients
    steps = row.steps
    if row.kind == FavoriteKind.TEXT.value:
        content = _validated_text(changes["text"]) if "text" in changes else row.content
    else:
        raw_ingredients = changes["ingredients"] if "ingredients" in changes else ingredients
        raw_steps = changes["steps"] if "steps" in changes else steps
        raw_servings = changes["servings"] if "servings" in changes else servings
        try:
            checked = validate_recipe(
                row.source_recipe_index, title, raw_servings, raw_ingredients, raw_steps
            )
        except ValueError as error:
            raise InvalidFavorite("the structured fields are not valid") from error
        title = checked.title
        servings = checked.servings
        ingredients = [
            {"name": ingredient.name, "amount": ingredient.amount}
            for ingredient in checked.ingredients
        ]
        steps = list(checked.steps)
        content = favorite_content(
            kind=row.kind,
            title=title,
            text=None,
            notes=notes,
            servings=servings,
            ingredients=checked.ingredients,
            steps=checked.steps,
        )

    updated = session.execute(
        sa.update(FavoriteRecipe)
        .where(
            FavoriteRecipe.id == favorite_id,
            FavoriteRecipe.user_id == user_id,
            FavoriteRecipe.version == version,
        )
        .values(
            title=title,
            content=content,
            notes=notes,
            servings=servings,
            ingredients=ingredients,
            steps=steps,
            version=FavoriteRecipe.version + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if updated != 1:
        raise FavoriteConflict

    return FavoriteDetail(
        id=row.id,
        title=title,
        text=content,
        created_at=row.created_at,
        updated_at=now,
        source_message_id=row.source_message_id,
        kind=row.kind,
        source_recipe_index=row.source_recipe_index,
        version=version + 1,
        notes=notes,
        servings=servings,
        ingredients=_ingredients(ingredients),
        steps=_steps(steps),
    )
