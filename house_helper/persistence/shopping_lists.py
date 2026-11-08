"""Saved shopping checklists: a complete ingredient snapshot, its items and its life.

A shopping list is a COPY, made on request and kept apart from what it came from
(models.ShoppingList says why): it has no deadline and no foreign key to a conversation,
a message, a message recipe or a favorite, so it survives the expiry of the answer, the
purge of the conversation and the deletion of both, and the editing or deletion of the
favorite it was copied from. Two sources exist, and both are read from the database in
the transaction that creates the list, never taken from the request:

  * a message recipe, named by (conversation_id, message_id, recipe_index): the message
    must be an unexpired assistant message of the user's conversation and its stored
    recipe must still exist (message_recipes.get_recipe_in_conversation);
  * a structured favorite, named by (favorite_id, expected_version): the version must
    still be the one the person selected (favorites.structured_favorite_snapshot). A
    changed favorite is a conflict, never a silent substitution.

EVERY ingredient of the recipe is copied, including the ones the person already has at
home: the list is a snapshot of the recipe, not a filter of it. Each copied ingredient
becomes one shopping_list_items row in the recipe's order. Its initial state is the one
the request named for that position; an ingredient the request does not name starts
'unechecked', because unknown availability must never be recorded as confirmed absence.
The states are 'unchecked', 'at_home' and 'purchased', and any item may move between all
three while the list is active; 'at_home' never deletes it. A list with every ingredient
at home is a valid list (it has nothing to buy, but it is still the snapshot).

Ownership. Every function takes the user id of the server-side session and finds a list
only through it: a list that does not exist, belongs to somebody else or was removed is
one answer (ShoppingListNotFound), and so is its item (ShoppingListItemNotFound):
nothing tells the caller which. The owner is never read from anywhere else.

Editing. While a list is active a person may add a manual item (appended at
max(position) + 1), change an item's name, amount or state, and delete one item. Every
item write locks the list row first, checks that it is active and then performs one
conditional statement scoped to the list; two writers therefore serialize instead of
losing an edit. A completed list is read-only: every item change raises
ShoppingListCompleted until the list is moved back to 'active' by set_status. Reopening
keeps every item exactly as it was; completing a list deletes nothing.

Limits. One user keeps at most LISTS_LIMIT lists and one list at most ITEMS_LIMIT items.
Creation takes the user's row lock (a no-op UPDATE, the repository's write-first rule)
and counts the lists under it, so two creations cannot both pass the cap; adding an item
takes the list's row lock and counts under it. A recipe's own ingredient maximum is 100,
so every recipe snapshot fits, and manual items may fill the list to the cap.

Idempotent creation. (user_id, idempotency_key) is reserved by a row of the
shopping_list_requests ledger, written in the same transaction as the list it creates
and kept for the lifetime of the user account. create_list first looks the key up under
the user's lock: the same key with the same fingerprint answers the list that exists
(created False), and the same key with another request raises IdempotencyKeyReused;
nothing new is written either way. A savepoint around the insert makes an IntegrityError
from a racing writer harmless in the same way. A retry whose source has since expired,
been deleted or been edited therefore still answers the list that was already saved: the
snapshot, not its source, is the result.

The key outlives the list. The ledger's list_id is ON DELETE SET NULL, so deleting a
list leaves its request row behind as a tombstone (list_id NULL) that holds the key and
the request fingerprint and nothing of the shopping list itself. A retry of the same
request after the deletion raises ShoppingListDeleted instead of recreating the list,
and the same key with a different request still raises IdempotencyKeyReused; a new list
needs a new key. A creation that fails or is rolled back leaves no ledger row, because
the row is part of the same transaction: a failed request never consumes its key.

Retention. Nothing here expires, and admin.retention does not know these tables. Only
deleting the list (delete_list), deleting its user (the foreign key) or the database's
cascade removes a saved list; the request ledger is removed with its user and otherwise
kept, which is what keeps a consumed key consumed.

Writers write first, as everywhere in this layer (see persistence.database):
create_list starts with the user's lock, item writes start with the list's lock, and
deletions are DELETEs. Reads write nothing. Nothing here commits; the caller owns the
transaction (web.database.WebDatabase.run). Results are plain values, not ORM objects.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.core.recipes import Ingredient, MAX_RECIPES_PER_REPLY
from house_helper.persistence import favorites, message_recipes
from house_helper.persistence.identifiers import validate_idempotency_key
from house_helper.persistence.models import (
    ShoppingListItem,
    ShoppingListItemState,
    ShoppingListRequest,
    ShoppingListSourceKind,
    ShoppingListStatus,
    ShoppingList,
    User,
)

# How many saved lists one user keeps at most, and how many items one list holds.
LISTS_LIMIT = 100
ITEMS_LIMIT = 200

# The bounds of a copied or manually added item (the protocol's own ingredient bounds).
MAX_NAME_LENGTH = 200
MAX_AMOUNT_LENGTH = 100

# The largest message id a list may name (the same bound the favorites API uses).
MAX_MESSAGE_ID = 2**62

# A name or an amount is one line: no control character at all (the recipe protocol's
# rule for its ingredient fields), and nothing that cannot be stored as UTF-8.
_ITEM_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")

_ITEM_STATES = frozenset(state.value for state in ShoppingListItemState)
_LIST_STATUSES = frozenset(status.value for status in ShoppingListStatus)
_SOURCE_KINDS = frozenset(kind.value for kind in ShoppingListSourceKind)


class ShoppingListNotFound(Exception):
    """No such shopping list for this user (unknown, somebody else's, removed)."""


class ShoppingListItemNotFound(Exception):
    """No such item in this user's shopping list (unknown, another list's, removed)."""


class ShoppingListCompleted(Exception):
    """The list is completed: its items are read-only until it is reopened."""


class ShoppingListsFull(Exception):
    """The user already keeps LISTS_LIMIT saved lists."""


class ShoppingListItemsFull(Exception):
    """The list already holds ITEMS_LIMIT items."""


class IdempotencyKeyReused(Exception):
    """The key was used before for a different creation request. Nothing was written."""


class ShoppingListDeleted(Exception):
    """The key already made a list that has since been deleted. Nothing is recreated:
    the key stays reserved, and a new list needs a new key."""


class InvalidShoppingList(ValueError):
    """A source, a field or a state that cannot be stored. The caller answers 400
    invalid_request; nothing is written."""


@dataclass(frozen=True)
class ShoppingItemView:
    """One item of a saved list, as it is read and as a write answers it."""

    id: UUID
    position: int
    name: str
    amount: Optional[str]
    state: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ShoppingListSummary:
    """Enough of a saved list for the user's list: no items, only their counts."""

    id: UUID
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    item_count: int
    unchecked_count: int
    at_home_count: int
    purchased_count: int


@dataclass(frozen=True)
class ShoppingListDetail:
    """A saved list with every item, in the recipe's order."""

    id: UUID
    title: str
    status: str
    source_kind: str
    source_message_id: Optional[int]
    source_recipe_index: Optional[int]
    source_favorite_id: Optional[UUID]
    source_favorite_version: Optional[int]
    created_at: datetime
    updated_at: datetime
    items: tuple[ShoppingItemView, ...]


@dataclass(frozen=True)
class CreatedShoppingList:
    shopping_list: ShoppingListDetail
    created: bool  # False: this request had already made this list


# --- what a creation request is ---------------------------------------------------------


def creation_fingerprint(
    *,
    source: str,
    conversation_id: Optional[UUID] = None,
    message_id: Optional[int] = None,
    recipe_index: Optional[int] = None,
    favorite_id: Optional[UUID] = None,
    expected_version: Optional[int] = None,
    item_states: Optional[Mapping[int, str]] = None,
) -> str:
    """The SHA-256 hex digest of a creation request in canonical form.

    The same request written two ways (a UUID in either case, item states in another
    order) is the same fingerprint; a request that names another source, another version
    or another initial state is not. The server computes it from the parsed request, so
    it describes what the request means, not how its JSON happened to be spelled.
    """
    document: dict[str, Any] = {"source": source}
    if source == ShoppingListSourceKind.MESSAGE.value:
        document["conversation_id"] = str(conversation_id)
        document["message_id"] = message_id
        document["recipe_index"] = recipe_index
    else:
        document["favorite_id"] = str(favorite_id)
        document["expected_version"] = expected_version
    document["item_states"] = sorted(
        [int(position), state] for position, state in (item_states or {}).items()
    )
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- validation ---------------------------------------------------------------------------


def _text(value: object, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise InvalidShoppingList(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise InvalidShoppingList(f"{field} must not be empty")
    if len(text) > max_length:
        raise InvalidShoppingList(f"{field} is too long")
    if _ITEM_CONTROL.search(text) is not None:
        raise InvalidShoppingList(f"{field} contains a control character")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidShoppingList(f"{field} is not valid Unicode text") from error
    return text


def _validated_name(value: object) -> str:
    return _text(value, field="name", max_length=MAX_NAME_LENGTH)


def _validated_amount(value: object) -> Optional[str]:
    """The amount as written, trimmed; None when absent or blank."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidShoppingList("amount must be a string or null")
    if not value.strip():
        return None
    return _text(value, field="amount", max_length=MAX_AMOUNT_LENGTH)


def _validated_state(value: object) -> str:
    if not isinstance(value, str) or value not in _ITEM_STATES:
        raise InvalidShoppingList("state must be unchecked, at_home or purchased")
    return value


def _validated_status(value: object) -> str:
    if not isinstance(value, str) or value not in _LIST_STATUSES:
        raise InvalidShoppingList("status must be active or completed")
    return value


def _validated_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidShoppingList("expected_version must be a positive whole number")
    return value


def _validated_message_id(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_MESSAGE_ID
    ):
        raise InvalidShoppingList("message_id must be a positive whole number")
    return value


def _validated_recipe_index(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < MAX_RECIPES_PER_REPLY
    ):
        raise InvalidShoppingList("recipe_index must be an accepted recipe position")
    return value


def _validated_states(
    value: Optional[Mapping[int, object]], *, ingredients: int
) -> dict[int, str]:
    """The initial state of each ingredient the request named; the others are unchecked."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidShoppingList("item_states must be an object")
    states: dict[int, str] = {}
    for position, state in value.items():
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position < ingredients
        ):
            raise InvalidShoppingList("an item state names no ingredient of the recipe")
        states[position] = _validated_state(state)
    return states


# --- reading rows -----------------------------------------------------------------------


def _item_view(row: Any) -> ShoppingItemView:
    return ShoppingItemView(
        id=row.id,
        position=row.position,
        name=row.name,
        amount=row.amount,
        state=row.state,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _items(session: OrmSession, list_id: UUID) -> tuple[ShoppingItemView, ...]:
    rows = session.scalars(
        sa.select(ShoppingListItem)
        .where(ShoppingListItem.list_id == list_id)
        .order_by(ShoppingListItem.position)
        .execution_options(populate_existing=True)
    ).all()
    return tuple(_item_view(row) for row in rows)


def _detail(session: OrmSession, row: ShoppingList) -> ShoppingListDetail:
    return ShoppingListDetail(
        id=row.id,
        title=row.title,
        status=row.status,
        source_kind=row.source_kind,
        source_message_id=row.source_message_id,
        source_recipe_index=row.source_recipe_index,
        source_favorite_id=row.source_favorite_id,
        source_favorite_version=row.source_favorite_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        items=_items(session, row.id),
    )


def _ledger(
    session: OrmSession, user_id: UUID, idempotency_key: str
) -> Optional[ShoppingListRequest]:
    return session.scalars(
        sa.select(ShoppingListRequest)
        .where(
            ShoppingListRequest.user_id == user_id,
            ShoppingListRequest.idempotency_key == idempotency_key,
        )
        .execution_options(populate_existing=True)
    ).first()


def _replay(
    session: OrmSession,
    user_id: UUID,
    request_row: ShoppingListRequest,
    fingerprint: str,
) -> CreatedShoppingList:
    """Answer a key that was consumed before: its list if it still exists, a conflict
    for another request, ShoppingListDeleted for a list that has been deleted."""
    if request_row.request_fingerprint != fingerprint:
        raise IdempotencyKeyReused
    if request_row.list_id is None:
        raise ShoppingListDeleted
    row = session.scalars(
        sa.select(ShoppingList)
        .where(
            ShoppingList.id == request_row.list_id,
            ShoppingList.user_id == user_id,
        )
        .execution_options(populate_existing=True)
    ).first()
    if row is None:  # the ledger's SET NULL is not visible to this read: deleted anyway
        raise ShoppingListDeleted
    return CreatedShoppingList(_detail(session, row), created=False)


# --- the source snapshot ----------------------------------------------------------------


@dataclass(frozen=True)
class _Snapshot:
    title: str
    ingredients: tuple[Ingredient, ...]
    source_kind: str
    source_message_id: Optional[int] = None
    source_recipe_index: Optional[int] = None
    source_favorite_id: Optional[UUID] = None
    source_favorite_version: Optional[int] = None


def _message_snapshot(
    session: OrmSession,
    user_id: UUID,
    conversation_id: UUID,
    message_id: int,
    recipe_index: int,
    *,
    now: datetime,
) -> _Snapshot:
    stored = message_recipes.get_recipe_in_conversation(
        session, user_id, conversation_id, message_id, recipe_index, now=now
    )
    return _Snapshot(
        title=stored.title,
        ingredients=stored.ingredients,
        source_kind=ShoppingListSourceKind.MESSAGE.value,
        source_message_id=message_id,
        source_recipe_index=recipe_index,
    )


def _favorite_snapshot(
    session: OrmSession,
    user_id: UUID,
    favorite_id: UUID,
    expected_version: int,
) -> _Snapshot:
    copied = favorites.structured_favorite_snapshot(
        session, user_id, favorite_id, expected_version=expected_version
    )
    return _Snapshot(
        title=copied.title,
        ingredients=copied.ingredients,
        source_kind=ShoppingListSourceKind.FAVORITE.value,
        source_favorite_id=copied.favorite_id,
        source_favorite_version=copied.version,
    )


# --- creating ---------------------------------------------------------------------------


def create_list(
    session: OrmSession,
    user_id: UUID,
    *,
    source: object,
    idempotency_key: object,
    fingerprint: object,
    now: datetime,
    conversation_id: Optional[UUID] = None,
    message_id: Optional[int] = None,
    recipe_index: Optional[int] = None,
    favorite_id: Optional[UUID] = None,
    expected_version: Optional[int] = None,
    item_states: Optional[Mapping[int, object]] = None,
) -> CreatedShoppingList:
    """Save a complete snapshot of one recipe as a new active shopping list.

    `source` is 'message' (with conversation_id, message_id, recipe_index) or 'favorite'
    (with favorite_id, expected_version); the other source's arguments must be absent.
    `item_states` maps a recipe ingredient position to the state the draft named; every
    other ingredient starts 'unchecked'. `now` is the application's clock for every
    timestamp of the new rows.

    Exceptions: InvalidShoppingList for a malformed request; ConversationNotFound,
    MessageNotFound, RecipeNotFound for a message source; FavoriteNotFound,
    favorite.FavoriteConflict and favorite.InvalidFavorite for one; ShoppingListsFull at
    the cap. A repeated (user, idempotency_key) answers the list that exists
    (created=False) when the fingerprint is the same, raises IdempotencyKeyReused when it
    is not, and raises ShoppingListDeleted when the list it made has been deleted. The
    first statement is the user's row lock (the repository's write-first rule, and what
    makes the cap exact).
    """
    try:
        key = validate_idempotency_key(idempotency_key)
    except ValueError as error:
        raise InvalidShoppingList("the idempotency key is unusable") from error
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise InvalidShoppingList("the request fingerprint is unusable")

    if source == ShoppingListSourceKind.MESSAGE.value:
        if (
            not isinstance(conversation_id, UUID)
            or message_id is None
            or recipe_index is None
            or favorite_id is not None
            or expected_version is not None
        ):
            raise InvalidShoppingList("a message source names its conversation and recipe")
        message_id = _validated_message_id(message_id)
        recipe_index = _validated_recipe_index(recipe_index)
    elif source == ShoppingListSourceKind.FAVORITE.value:
        if not isinstance(favorite_id, UUID) or expected_version is None or (
            conversation_id is not None or message_id is not None or recipe_index is not None
        ):
            raise InvalidShoppingList("a favorite source names its favorite and version")
        expected_version = _validated_version(expected_version)
    else:
        raise InvalidShoppingList("source must be message or favorite")

    # The user's lock: the write that decides the transaction and holds the list cap.
    session.execute(
        sa.update(User)
        .where(User.id == user_id)
        .values(updated_at=User.updated_at)
        .execution_options(synchronize_session=False)
    )

    consumed = _ledger(session, user_id, key)
    if consumed is not None:
        return _replay(session, user_id, consumed, fingerprint)

    if source == ShoppingListSourceKind.MESSAGE.value:
        snapshot = _message_snapshot(
            session, user_id, conversation_id, message_id, recipe_index, now=now
        )
    else:
        snapshot = _favorite_snapshot(session, user_id, favorite_id, expected_version)

    states = _validated_states(item_states, ingredients=len(snapshot.ingredients))

    kept = session.scalar(
        sa.select(sa.func.count())
        .select_from(ShoppingList)
        .where(ShoppingList.user_id == user_id)
    )
    if kept >= LISTS_LIMIT:
        raise ShoppingListsFull

    row = ShoppingList(
        user_id=user_id,
        title=snapshot.title,
        status=ShoppingListStatus.ACTIVE.value,
        source_kind=snapshot.source_kind,
        source_message_id=snapshot.source_message_id,
        source_recipe_index=snapshot.source_recipe_index,
        source_favorite_id=snapshot.source_favorite_id,
        source_favorite_version=snapshot.source_favorite_version,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        created_at=now,
        updated_at=now,
    )
    # The list, its items and the key reservation are one unit: a failure inside the
    # savepoint takes all three back, so a rolled-back creation never consumes its key.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
            session.add_all(
                ShoppingListItem(
                    list_id=row.id,
                    position=position,
                    name=ingredient.name,
                    amount=ingredient.amount,
                    state=states.get(position, ShoppingListItemState.UNCHECKED.value),
                    created_at=now,
                    updated_at=now,
                )
                for position, ingredient in enumerate(snapshot.ingredients)
            )
            session.add(
                ShoppingListRequest(
                    user_id=user_id,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    list_id=row.id,
                    created_at=now,
                )
            )
            session.flush()
    except sa.exc.IntegrityError:
        # A writer that raced this one under the same key: the savepoint took the
        # attempt back. If the key is not there, some other rule refused the row.
        consumed = _ledger(session, user_id, key)
        if consumed is None:
            raise
        return _replay(session, user_id, consumed, fingerprint)

    return CreatedShoppingList(_detail(session, row), created=True)


# --- reading ----------------------------------------------------------------------------


def list_lists(session: OrmSession, user_id: UUID) -> list[ShoppingListSummary]:
    """The user's saved lists, newest first (ties by id, so the order is total), with the
    count of items in each state and without the items themselves. At most LISTS_LIMIT."""
    rows = session.scalars(
        sa.select(ShoppingList)
        .where(ShoppingList.user_id == user_id)
        .order_by(ShoppingList.created_at.desc(), ShoppingList.id.desc())
        .limit(LISTS_LIMIT)
        .execution_options(populate_existing=True)
    ).all()
    if not rows:
        return []
    counts: dict[UUID, dict[str, int]] = {}
    grouped = session.execute(
        sa.select(
            ShoppingListItem.list_id, ShoppingListItem.state, sa.func.count()
        )
        .where(ShoppingListItem.list_id.in_([row.id for row in rows]))
        .group_by(ShoppingListItem.list_id, ShoppingListItem.state)
    ).all()
    for list_id, state, number in grouped:
        counts.setdefault(list_id, {})[state] = number
    return [
        ShoppingListSummary(
            id=row.id,
            title=row.title,
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
            item_count=sum(counts.get(row.id, {}).values()),
            unchecked_count=counts.get(row.id, {}).get(
                ShoppingListItemState.UNCHECKED.value, 0
            ),
            at_home_count=counts.get(row.id, {}).get(
                ShoppingListItemState.AT_HOME.value, 0
            ),
            purchased_count=counts.get(row.id, {}).get(
                ShoppingListItemState.PURCHASED.value, 0
            ),
        )
        for row in rows
    ]


def get_list(session: OrmSession, user_id: UUID, list_id: UUID) -> ShoppingListDetail:
    """One saved list of the user with every item. ShoppingListNotFound if it is not
    theirs."""
    row = session.scalars(
        sa.select(ShoppingList)
        .where(ShoppingList.id == list_id, ShoppingList.user_id == user_id)
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise ShoppingListNotFound
    return _detail(session, row)


# --- items ------------------------------------------------------------------------------


def _locked_list(session: OrmSession, user_id: UUID, list_id: UUID) -> str:
    """Lock the user's list for an item write and return its status. The no-op UPDATE is
    the transaction's first statement; ShoppingListNotFound if it is not theirs."""
    locked = session.execute(
        sa.update(ShoppingList)
        .where(ShoppingList.id == list_id, ShoppingList.user_id == user_id)
        .values(updated_at=ShoppingList.updated_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if locked != 1:
        raise ShoppingListNotFound
    status = session.scalar(
        sa.select(ShoppingList.status).where(ShoppingList.id == list_id)
    )
    if status is None:  # the lock found it: not possible without a concurrent deletion
        raise ShoppingListNotFound
    return status


def _touch(session: OrmSession, list_id: UUID, now: datetime) -> None:
    session.execute(
        sa.update(ShoppingList)
        .where(ShoppingList.id == list_id)
        .values(updated_at=now)
        .execution_options(synchronize_session=False)
    )


def add_item(
    session: OrmSession,
    user_id: UUID,
    list_id: UUID,
    *,
    name: object,
    amount: object = None,
    state: object = None,
    now: datetime,
) -> ShoppingItemView:
    """Append a manual item to the user's active list and return it.

    ShoppingListNotFound if the list is not theirs, ShoppingListCompleted if it is
    completed, ShoppingListItemsFull at the cap, InvalidShoppingList for a value that
    cannot be stored. The position is max(position) + 1 under the list's lock, so two
    additions cannot claim one place. The first statement is the list's lock.
    """
    checked_name = _validated_name(name)
    checked_amount = _validated_amount(amount)
    checked_state = (
        ShoppingListItemState.UNCHECKED.value if state is None else _validated_state(state)
    )

    status = _locked_list(session, user_id, list_id)
    if status != ShoppingListStatus.ACTIVE.value:
        raise ShoppingListCompleted

    held = session.scalar(
        sa.select(sa.func.count())
        .select_from(ShoppingListItem)
        .where(ShoppingListItem.list_id == list_id)
    )
    if held >= ITEMS_LIMIT:
        raise ShoppingListItemsFull
    last = session.scalar(
        sa.select(sa.func.max(ShoppingListItem.position)).where(
            ShoppingListItem.list_id == list_id
        )
    )
    position = 0 if last is None else last + 1

    item = ShoppingListItem(
        list_id=list_id,
        position=position,
        name=checked_name,
        amount=checked_amount,
        state=checked_state,
        created_at=now,
        updated_at=now,
    )
    session.add(item)
    session.flush()
    _touch(session, list_id, now)
    return _item_view(item)


def update_item(
    session: OrmSession,
    user_id: UUID,
    list_id: UUID,
    item_id: UUID,
    *,
    changes: Mapping[str, Any],
    now: datetime,
) -> ShoppingItemView:
    """Change the name, amount and/or state of one item of the user's active list.

    A field the request does not name is left as it is; `amount: null` clears it. Every
    change is one conditional UPDATE scoped to the item's list, after the list's lock.
    ShoppingListNotFound if the list is not theirs, ShoppingListItemNotFound if the item
    is not in it, ShoppingListCompleted if the list is completed, InvalidShoppingList for
    an unknown field or a value that cannot be stored.
    """
    if not isinstance(changes, Mapping) or not changes:
        raise InvalidShoppingList("an item edit needs at least one field")
    unknown = set(changes) - {"name", "amount", "state"}
    if unknown:
        raise InvalidShoppingList("a field of an item cannot be edited")
    checked = {
        field: (
            _validated_name(value)
            if field == "name"
            else _validated_amount(value)
            if field == "amount"
            else _validated_state(value)
        )
        for field, value in changes.items()
    }

    status = _locked_list(session, user_id, list_id)
    if status != ShoppingListStatus.ACTIVE.value:
        raise ShoppingListCompleted

    row = session.execute(
        sa.select(
            ShoppingListItem.position,
            ShoppingListItem.name,
            ShoppingListItem.amount,
            ShoppingListItem.state,
            ShoppingListItem.created_at,
        ).where(
            ShoppingListItem.id == item_id, ShoppingListItem.list_id == list_id
        )
    ).first()
    if row is None:
        raise ShoppingListItemNotFound

    name = checked["name"] if "name" in checked else row.name
    amount = checked["amount"] if "amount" in checked else row.amount
    state = checked["state"] if "state" in checked else row.state
    updated = session.execute(
        sa.update(ShoppingListItem)
        .where(ShoppingListItem.id == item_id, ShoppingListItem.list_id == list_id)
        .values(
            name=name,
            amount=amount,
            state=state,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if updated != 1:
        raise ShoppingListItemNotFound
    _touch(session, list_id, now)
    return ShoppingItemView(
        id=item_id,
        position=row.position,
        name=name,
        amount=amount,
        state=state,
        created_at=row.created_at,
        updated_at=now,
    )


def delete_item(
    session: OrmSession, user_id: UUID, list_id: UUID, item_id: UUID, *, now: datetime
) -> None:
    """Delete one item of the user's active list. Only that item; the others keep their
    positions. ShoppingListNotFound, ShoppingListCompleted and ShoppingListItemNotFound
    as above."""
    status = _locked_list(session, user_id, list_id)
    if status != ShoppingListStatus.ACTIVE.value:
        raise ShoppingListCompleted
    removed = session.execute(
        sa.delete(ShoppingListItem)
        .where(ShoppingListItem.id == item_id, ShoppingListItem.list_id == list_id)
        .execution_options(synchronize_session=False)
    ).rowcount
    if removed != 1:
        raise ShoppingListItemNotFound
    _touch(session, list_id, now)


# --- the list's life --------------------------------------------------------------------


def set_status(
    session: OrmSession,
    user_id: UUID,
    list_id: UUID,
    *,
    status: object,
    now: datetime,
) -> ShoppingListDetail:
    """Complete or reopen the user's list and return it with its items.

    A completed list keeps every item and is read-only until it is moved back to active;
    nothing is deleted by completing it. Setting the status it already has changes
    nothing (and does not move updated_at). ShoppingListNotFound if it is not theirs,
    InvalidShoppingList for another status.
    """
    wanted = _validated_status(status)
    current = _locked_list(session, user_id, list_id)
    if current != wanted:
        session.execute(
            sa.update(ShoppingList)
            .where(ShoppingList.id == list_id, ShoppingList.user_id == user_id)
            .values(status=wanted, updated_at=now)
            .execution_options(synchronize_session=False)
        )
    row = session.scalars(
        sa.select(ShoppingList)
        .where(ShoppingList.id == list_id, ShoppingList.user_id == user_id)
        .execution_options(populate_existing=True)
    ).first()
    if row is None:  # the lock found it: not possible without a concurrent deletion
        raise ShoppingListNotFound
    return _detail(session, row)


def delete_list(session: OrmSession, user_id: UUID, list_id: UUID) -> None:
    """Delete the user's saved list and, by the database's cascade, all its items.

    ShoppingListNotFound if it is not theirs (or already gone). The statement is a write,
    and it is the first. The request-key ledger row survives as a tombstone: the
    database's ON DELETE SET NULL clears its list_id in the same statement, so the key
    stays consumed and a retry answers ShoppingListDeleted. Nothing else is touched: the
    source recipe or favorite is not affected."""
    removed = session.execute(
        sa.delete(ShoppingList)
        .where(ShoppingList.id == list_id, ShoppingList.user_id == user_id)
        .execution_options(synchronize_session=False)
    ).rowcount
    if removed != 1:
        raise ShoppingListNotFound
