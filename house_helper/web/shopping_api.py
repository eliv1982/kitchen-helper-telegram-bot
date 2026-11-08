"""The shopping-list API: saved checklists of a recipe's ingredients.

Nine routes under /api, all answering JSON, built the way chat_api.py's are (the same
route class, so the same correlation id, headers, error catalogue and log line; the same
session, Origin and body rules):

  GET    /api/conversations/{conversation_id}/messages/{message_id}/recipes/{position}
                                         one stored recipe, for the draft screen
  GET    /api/shopping-lists             the user's saved lists, newest first, without items
  POST   /api/shopping-lists             save a complete snapshot of a recipe (idempotent)
  GET    /api/shopping-lists/{id}        one list with every item
  PATCH  /api/shopping-lists/{id}        complete or reopen it ({"status": ...})
  DELETE /api/shopping-lists/{id}        remove the list and its items
  POST   /api/shopping-lists/{id}/items  add a manual item
  PATCH  /api/shopping-lists/{id}/items/{item_id}
                                         change a name, an amount or a state
  DELETE /api/shopping-lists/{id}/items/{item_id}
                                         remove one item

Who. The user is whoever the session cookie resolves to on the server; a user id or an
owner in the request is never read. A list that does not exist, belongs to somebody else
or is not even a UUID is the same 404 "shopping_list_not_found" for every list route; an
item is the same 404 "shopping_list_item_not_found"; a conversation that is not the
user's (or a message of it that is not an unexpired assistant answer) is the chat API's
"conversation_not_found"/"message_not_found". Nothing tells the caller which of "unknown"
and "somebody else's" it was.

The list of saved lists. GET /api/shopping-lists answers each list's id, title, status,
timestamps and the count of its items in each state (`counts`: total, unchecked, at_home,
purchased), newest first, without the items themselves; GET /api/shopping-lists/{id}
answers the list with every item, in order.

Reading a recipe. The draft screen names the conversation, the answer and the recipe's
position, and gets the stored validated fields (title, servings, ingredients, steps) with
no prose parsing and no provider call. It is the same recipe the creation route copies;
a message with no accepted recipe at that position is "recipe_not_found".

Saving a list (POST). The header `Idempotency-Key` (1 to 128 printable ASCII characters,
not starting with "tg-", as for a message) is required. The body names its source and
the draft's initial item states, and NOTHING ELSE:

    {"source": "message", "conversation_id": "...", "message_id": 123,
     "recipe_index": 0, "item_states": [{"position": 0, "state": "at_home"}]}
    {"source": "favorite", "favorite_id": "...", "expected_version": 2,
     "item_states": [...]}

"item_states" is optional; a position it does not name starts "unchecked". Every
ingredient of the recipe is copied, including the ones marked "at_home": the list is a
snapshot of the recipe, not a filter of it. The source is read from the database in the
transaction that saves the list, never from the request. A changed structured favorite
is refused with "favorite_conflict" (the client reloads and lets the person choose
again); a text favorite has no structured fields to copy and is "invalid_request".

  201  saved: the list with every item, and "created": true
  200  the same key and the same request again: the same list, "created": false, and the
       header Idempotent-Replayed: true. Nothing is written, even if the source has
       since expired or been deleted
  409  shopping_list_key_reused: the key was used for a different request
  409  shopping_list_deleted: the key already made a list that has since been deleted.
       The key stays consumed for the life of the account, so nothing is recreated; a
       new list needs a new key
  409  shopping_lists_full: the user keeps as many lists as they may (100)

A completed list. PATCH with {"status": "completed"} makes the list read-only: adding,
editing, moving or deleting an item answers 409 "shopping_list_completed", and nothing is
deleted. PATCH with {"status": "active"} reopens it exactly as it was. Completing an
already completed list (or reopening an active one) changes nothing.

Items. While the list is active, an item may be added (its position is the end of the
list), and its name, amount and state may each be changed independently; "amount": null
clears it. The three states are "unchecked", "at_home" and "purchased", and an item may
move between any two of them; "at_home" never deletes it. A list holds at most 200 items
("shopping_list_items_full"). DELETE removes only the named item; the others keep their
order. No route edits the list's title or its source.

Errors. The catalogue and the shape of every error body are in diagnostics.py; the new
codes are "shopping_list_not_found", "shopping_list_item_not_found",
"shopping_list_completed", "shopping_lists_full", "shopping_list_items_full",
"shopping_list_key_reused" and "shopping_list_deleted". Every response carries
X-Correlation-ID, Cache-Control:
no-store and Referrer-Policy: no-referrer, and a mutation is application/json with the
Origin rule. Nothing the client sent is echoed in an error.

No model is called anywhere here: a list is copied from rows that already exist, and a
draft's states are the person's own selections.
"""

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from house_helper.core.recipes import MAX_INGREDIENTS, MAX_RECIPES_PER_REPLY
from house_helper.persistence import favorites, message_recipes, shopping_lists
from house_helper.persistence.authn import AuthenticatedSession
from house_helper.persistence.conversations import ConversationNotFound
from house_helper.persistence.message_recipes import MessageNotFound, RecipeNotFound
from house_helper.persistence.models import ShoppingListItemState
from house_helper.persistence.shopping_lists import (
    IdempotencyKeyReused,
    InvalidShoppingList,
    ShoppingItemView,
    ShoppingListCompleted,
    ShoppingListDeleted,
    ShoppingListDetail,
    ShoppingListItemsFull,
    ShoppingListNotFound,
    ShoppingListItemNotFound,
    ShoppingListSummary,
    ShoppingListsFull,
)
from house_helper.web.chat_api import (
    ChatRoute,
    _UUID,
    _conversation_id,
    _idempotency_key,
    _read_json_object,
    _timestamp,
    authenticated,
    authenticated_mutation,
)
from house_helper.web.database import WebDatabase
from house_helper.web.dependencies import get_database
from house_helper.web.diagnostics import ApiError

# The largest message id a path may name (the same bound as the history's ?before=).
_MAX_MESSAGE_ID = 2**62

_DIGITS = re.compile(r"[0-9]{1,18}")

# The states a draft may name. The persistence layer validates them again; this is what
# makes a malformed request a 400 before anything is read.
_ITEM_STATES = frozenset(state.value for state in ShoppingListItemState)

router = APIRouter(prefix="/api", route_class=ChatRoute)


@contextmanager
def _errors() -> Iterator[None]:
    """Turn the persistence layer's exceptions into the catalogue's answers, all with
    `from None`: no storage detail reaches the client or the log."""
    try:
        yield
    except ConversationNotFound:
        raise ApiError("conversation_not_found") from None
    except MessageNotFound:
        raise ApiError("message_not_found") from None
    except RecipeNotFound:
        raise ApiError("recipe_not_found") from None
    except favorites.FavoriteNotFound:
        raise ApiError("favorite_not_found") from None
    except favorites.FavoriteConflict:
        raise ApiError("favorite_conflict") from None
    except favorites.InvalidFavorite:
        raise ApiError("invalid_request", category="InvalidFavoriteField") from None
    except InvalidShoppingList:
        raise ApiError("invalid_request", category="InvalidShoppingList") from None
    except ShoppingListNotFound:
        raise ApiError("shopping_list_not_found") from None
    except ShoppingListItemNotFound:
        raise ApiError("shopping_list_item_not_found") from None
    except ShoppingListCompleted:
        raise ApiError("shopping_list_completed") from None
    except ShoppingListsFull:
        raise ApiError("shopping_lists_full") from None
    except ShoppingListItemsFull:
        raise ApiError("shopping_list_items_full") from None
    except IdempotencyKeyReused:
        raise ApiError("shopping_list_key_reused") from None
    except ShoppingListDeleted:
        raise ApiError("shopping_list_deleted") from None


# --- identifiers in the path -------------------------------------------------------------


def _list_id(raw: str) -> UUID:
    if _UUID.fullmatch(raw) is None:
        raise ApiError("shopping_list_not_found")
    return UUID(raw)


def _item_id(raw: str) -> UUID:
    if _UUID.fullmatch(raw) is None:
        raise ApiError("shopping_list_item_not_found")
    return UUID(raw)


def _message_id(raw: str) -> int:
    if _DIGITS.fullmatch(raw) is None or not 1 <= int(raw) <= _MAX_MESSAGE_ID:
        raise ApiError("message_not_found")
    return int(raw)


def _position(raw: str) -> int:
    if _DIGITS.fullmatch(raw) is None or not 0 <= int(raw) < MAX_RECIPES_PER_REPLY:
        raise ApiError("recipe_not_found")
    return int(raw)


# --- reading a creation request ----------------------------------------------------------


@dataclass(frozen=True)
class _Creation:
    source: str
    conversation_id: Optional[UUID]
    message_id: Optional[int]
    recipe_index: Optional[int]
    favorite_id: Optional[UUID]
    expected_version: Optional[int]
    item_states: dict[int, str]
    fingerprint: str


def _favorite_id(value: object) -> UUID:
    # The favorites API's rule: anything that is not a UUID is a favorite that is not there.
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise ApiError("favorite_not_found")
    return UUID(value)


def _whole(value: object, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ApiError("invalid_request", category="UnexpectedFields")
    return value


def _item_states(value: object) -> dict[int, str]:
    """The draft's initial states as {position: state}; every position once, in range."""
    if value is None:
        return {}
    if not isinstance(value, list):
        raise ApiError("invalid_request", category="UnexpectedFields")
    states: dict[int, str] = {}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"position", "state"}:
            raise ApiError("invalid_request", category="UnexpectedFields")
        position = entry["position"]
        state = entry["state"]
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or not 0 <= position < MAX_INGREDIENTS
            or position in states
            or not isinstance(state, str)
            or state not in _ITEM_STATES
        ):
            raise ApiError("invalid_request", category="UnexpectedFields")
        states[position] = state
    return states


def _creation(document: dict) -> _Creation:
    source = document.get("source")
    if source == "message":
        allowed = {"source", "conversation_id", "message_id", "recipe_index", "item_states"}
        required = {"source", "conversation_id", "message_id", "recipe_index"}
    elif source == "favorite":
        allowed = {"source", "favorite_id", "expected_version", "item_states"}
        required = {"source", "favorite_id", "expected_version"}
    else:
        raise ApiError("invalid_request", category="UnexpectedFields")
    if not required <= set(document) or set(document) - allowed:
        raise ApiError("invalid_request", category="UnexpectedFields")

    if source == "message":
        conversation = document["conversation_id"]
        if not isinstance(conversation, str):
            raise ApiError("invalid_request", category="UnexpectedFields")
        conversation_id = _conversation_id(conversation)
        message_id = _whole(document["message_id"], low=1, high=_MAX_MESSAGE_ID)
        recipe_index = _whole(
            document["recipe_index"], low=0, high=MAX_RECIPES_PER_REPLY - 1
        )
        favorite_id = None
        expected_version = None
    else:
        conversation_id = None
        message_id = None
        recipe_index = None
        favorite_id = _favorite_id(document["favorite_id"])
        expected_version = _whole(document["expected_version"], low=1, high=2**62)

    states = _item_states(document.get("item_states"))
    return _Creation(
        source=source,
        conversation_id=conversation_id,
        message_id=message_id,
        recipe_index=recipe_index,
        favorite_id=favorite_id,
        expected_version=expected_version,
        item_states=states,
        fingerprint=shopping_lists.creation_fingerprint(
            source=source,
            conversation_id=conversation_id,
            message_id=message_id,
            recipe_index=recipe_index,
            favorite_id=favorite_id,
            expected_version=expected_version,
            item_states=states,
        ),
    )


def _item_document(document: dict) -> dict[str, Any]:
    """The fields of a new manual item: name required, amount and state optional."""
    if set(document) - {"name", "amount", "state"} or "name" not in document:
        raise ApiError("invalid_request", category="UnexpectedFields")
    if not isinstance(document["name"], str):
        raise ApiError("invalid_request", category="UnexpectedFields")
    if "amount" in document and document["amount"] is not None and not isinstance(
        document["amount"], str
    ):
        raise ApiError("invalid_request", category="UnexpectedFields")
    if "state" in document and not isinstance(document["state"], str):
        raise ApiError("invalid_request", category="UnexpectedFields")
    return dict(document)


def _edit_document(document: dict) -> dict[str, Any]:
    """The fields an item edit names: at least one of name, amount and state."""
    if not document or set(document) - {"name", "amount", "state"}:
        raise ApiError("invalid_request", category="UnexpectedFields")
    if "name" in document and not isinstance(document["name"], str):
        raise ApiError("invalid_request", category="UnexpectedFields")
    if "amount" in document and document["amount"] is not None and not isinstance(
        document["amount"], str
    ):
        raise ApiError("invalid_request", category="UnexpectedFields")
    if "state" in document and not isinstance(document["state"], str):
        raise ApiError("invalid_request", category="UnexpectedFields")
    return dict(document)


def _status_document(document: dict) -> str:
    if set(document) != {"status"} or not isinstance(document["status"], str):
        raise ApiError("invalid_request", category="UnexpectedFields")
    return document["status"]


# --- writing a response ------------------------------------------------------------------


def _item_json(item: ShoppingItemView) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "position": item.position,
        "name": item.name,
        "amount": item.amount,
        "state": item.state,
        "created_at": _timestamp(item.created_at),
        "updated_at": _timestamp(item.updated_at),
    }


def _source_json(found: ShoppingListDetail) -> dict[str, Any]:
    if found.source_kind == "message":
        return {
            "kind": "message",
            "message_id": found.source_message_id,
            "recipe_index": found.source_recipe_index,
        }
    return {
        "kind": "favorite",
        "favorite_id": str(found.source_favorite_id),
        "version": found.source_favorite_version,
    }


def _detail_json(found: ShoppingListDetail) -> dict[str, Any]:
    return {
        "id": str(found.id),
        "title": found.title,
        "status": found.status,
        "source": _source_json(found),
        "created_at": _timestamp(found.created_at),
        "updated_at": _timestamp(found.updated_at),
        "items": [_item_json(item) for item in found.items],
    }


def _summary_json(found: ShoppingListSummary) -> dict[str, Any]:
    return {
        "id": str(found.id),
        "title": found.title,
        "status": found.status,
        "created_at": _timestamp(found.created_at),
        "updated_at": _timestamp(found.updated_at),
        "counts": {
            "total": found.item_count,
            "unchecked": found.unchecked_count,
            "at_home": found.at_home_count,
            "purchased": found.purchased_count,
        },
    }


# --- the routes --------------------------------------------------------------------------


@router.get("/conversations/{conversation_id}/messages/{message_id}/recipes/{position}")
async def read_recipe(
    conversation_id: str,
    message_id: str,
    position: str,
    request: Request,
    current: AuthenticatedSession = Depends(authenticated),
    database: WebDatabase = Depends(get_database),
) -> Response:
    """One structured recipe of the user's unexpired assistant message, for a draft."""
    wanted_conversation = _conversation_id(conversation_id)
    wanted_message = _message_id(message_id)
    wanted_position = _position(position)
    now = request.app.state.clock()
    with _errors():
        stored = await database.run(
            lambda session: message_recipes.get_recipe_in_conversation(
                session,
                current.user_id,
                wanted_conversation,
                wanted_message,
                wanted_position,
                now=now,
            )
        )
    return JSONResponse(
        {
            "message_id": stored.message_id,
            "position": stored.position,
            "title": stored.title,
            "servings": stored.servings,
            "ingredients": [
                {"name": ingredient.name, "amount": ingredient.amount}
                for ingredient in stored.ingredients
            ],
            "steps": list(stored.steps),
        }
    )


@router.get("/shopping-lists")
async def list_shopping_lists(
    current: AuthenticatedSession = Depends(authenticated),
    database: WebDatabase = Depends(get_database),
) -> Response:
    saved = await database.run(
        lambda session: shopping_lists.list_lists(session, current.user_id)
    )
    return JSONResponse({"lists": [_summary_json(item) for item in saved]})


@router.post("/shopping-lists")
async def create_shopping_list(
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    created = _creation(await _read_json_object(request))
    key = _idempotency_key(request)
    now = request.app.state.clock()
    with _errors():
        saved = await database.run(
            lambda session: shopping_lists.create_list(
                session,
                current.user_id,
                source=created.source,
                conversation_id=created.conversation_id,
                message_id=created.message_id,
                recipe_index=created.recipe_index,
                favorite_id=created.favorite_id,
                expected_version=created.expected_version,
                item_states=created.item_states,
                idempotency_key=key,
                fingerprint=created.fingerprint,
                now=now,
            )
        )
    body = {**_detail_json(saved.shopping_list), "created": saved.created}
    if saved.created:
        return JSONResponse(body, status_code=201)
    return JSONResponse(
        body, status_code=200, headers={"Idempotent-Replayed": "true"}
    )


@router.get("/shopping-lists/{list_id}")
async def read_shopping_list(
    list_id: str,
    current: AuthenticatedSession = Depends(authenticated),
    database: WebDatabase = Depends(get_database),
) -> Response:
    wanted = _list_id(list_id)
    with _errors():
        found = await database.run(
            lambda session: shopping_lists.get_list(session, current.user_id, wanted)
        )
    return JSONResponse(_detail_json(found))


@router.patch("/shopping-lists/{list_id}")
async def change_shopping_list_status(
    list_id: str,
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    wanted = _list_id(list_id)
    status = _status_document(await _read_json_object(request))
    now = request.app.state.clock()
    with _errors():
        found = await database.run(
            lambda session: shopping_lists.set_status(
                session, current.user_id, wanted, status=status, now=now
            )
        )
    return JSONResponse(_detail_json(found))


@router.delete("/shopping-lists/{list_id}")
async def delete_shopping_list(
    list_id: str,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    wanted = _list_id(list_id)
    with _errors():
        await database.run(
            lambda session: shopping_lists.delete_list(session, current.user_id, wanted)
        )
    return Response(status_code=204)


@router.post("/shopping-lists/{list_id}/items")
async def add_shopping_item(
    list_id: str,
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    wanted = _list_id(list_id)
    document = _item_document(await _read_json_object(request))
    now = request.app.state.clock()
    with _errors():
        item = await database.run(
            lambda session: shopping_lists.add_item(
                session,
                current.user_id,
                wanted,
                name=document["name"],
                amount=document.get("amount"),
                state=document.get("state"),
                now=now,
            )
        )
    return JSONResponse(_item_json(item), status_code=201)


@router.patch("/shopping-lists/{list_id}/items/{item_id}")
async def edit_shopping_item(
    list_id: str,
    item_id: str,
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    wanted = _list_id(list_id)
    wanted_item = _item_id(item_id)
    changes = _edit_document(await _read_json_object(request))
    now = request.app.state.clock()
    with _errors():
        item = await database.run(
            lambda session: shopping_lists.update_item(
                session,
                current.user_id,
                wanted,
                wanted_item,
                changes=changes,
                now=now,
            )
        )
    return JSONResponse(_item_json(item))


@router.delete("/shopping-lists/{list_id}/items/{item_id}")
async def delete_shopping_item(
    list_id: str,
    item_id: str,
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    wanted = _list_id(list_id)
    wanted_item = _item_id(item_id)
    now = request.app.state.clock()
    with _errors():
        await database.run(
            lambda session: shopping_lists.delete_item(
                session, current.user_id, wanted, wanted_item, now=now
            )
        )
    return Response(status_code=204)
