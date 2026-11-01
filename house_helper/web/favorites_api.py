"""The favorites API: answers, and recipes of answers, that a user chose to keep.

Five routes under /api, all answering JSON, built the way chat_api.py's are (the same route
class, so the same correlation id, headers, error catalogue and log line; the same session,
Origin and body rules, except that an edit's body has a larger bound of its own: it carries
every field of the favorite, see MAX_EDIT_BODY):

  GET    /api/favorites                the user's favorites, newest first, without their text
  POST   /api/favorites                keep an answer, or one recipe of it
  GET    /api/favorites/{id}           one favorite, with its text and its fields
  PATCH  /api/favorites/{id}           edit its title, text (text kind), structured fields
                                       (structured kind) or personal note, by version
  DELETE /api/favorites/{id}           remove it

Who. The user is whoever the session cookie resolves to on the server; a user id or an owner
in the request is never read. A favorite that does not exist, belongs to somebody else or is
not even a UUID is the same 404 "favorite_not_found" for every route; a conversation that is
not the user's (or not a UUID) is the same 404 "conversation_not_found" as in the chat API.

Keeping an answer (POST). The body is {"conversation_id", "message_id"} and, optionally,
"recipe_index" (0 to MAX_RECIPES_PER_REPLY - 1). Without it, the old behaviour is exactly
preserved: the whole assistant answer is copied as text. With it, only that structured
recipe of the answer is copied, from message_recipes, which the turn runner populates when
the answer is committed (KH-F06C). The text and the fields are read from the database,
never from the request, so a favorite can only ever be a real answer (or a real recipe of
one) of the assistant to this user. The answers:

  201  kept: the summary with "created": true
  200  that answer (that recipe) was already a favorite: the same summary with
         "created": false, and nothing is written (so a tap that is repeated, or a retry
         after a lost reply, is harmless)
  404  conversation_not_found / message_not_found (no such answer in it, not the
         assistant's or expired) / recipe_not_found (the answer has no accepted recipe at
         that position)
  409  favorites_full: the user keeps as many as they may (both kinds together)

A favorite is a copy (see persistence.favorites): it has no deadline and does not depend on
the conversation, so it stays when the history expires. Removing one (DELETE, 204) deletes
only the copy.

Editing (PATCH). The body names "expected_version" and at least one of "title", "text",
"notes" (a text favorite), or "title", "servings", "ingredients", "steps", "notes" (a
structured one). A field that is not named is left as it is; a field the kind does not have,
an unknown field, a missing or invalid expected_version and a value that cannot be stored
are all 400 invalid_request. Ownership, the source message, the recipe
position, the kind and created_at cannot be edited: they are not among the fields, and the
database refuses their change on the ORM path too. The answer is the detail body (the same
shape as GET, with the new version):

  200  changed: version is one more than the version named
  404  favorite_not_found (unknown, somebody else's, removed: not told apart)
  409  favorite_conflict: the favorite changed since expected_version; nothing is written
         and the client reloads (no UI for this is added in this stage)

Editing is server-side and DB-only: no model, no provider, no spelling correction. A text
favorite's text is stored as entered; a structured favorite's content is rendered again by
persistence.favorites.favorite_content from the validated fields.

Feature switch. HOUSE_HELPER_STRUCTURED_RECIPES (web.settings, off by default) switches
the *generation* of structured recipes: the assistant's prompt addition and the parser in
front of rendering (core.openai_chat), and the page's per-recipe Save controls. It is not a
switch of these routes, deliberately. GET and the detail read serve stored favorites of
both kinds; PATCH and DELETE must keep working for a structured favorite that already
exists (turning generation off is not a reason to strand it); and POST with
"recipe_index" copies a recipe that is already stored, makes no provider call and creates
no new structured content, so refusing it would add a failure mode without protecting
anything. What the switch does prevent is new structured rows: with it off the backend the
application builds is asked for no blocks and parses none, so the turn runner is given no
Recipe to store. It never deletes or rewrites a stored row, and the schema guard stays at
0010: disabling a feature is not a downgrade.
"""

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from house_helper.core.recipes import (
    MAX_RECIPE_BLOCK_LENGTH,
    MAX_RECIPES_PER_REPLY,
    MAX_TITLE_LENGTH,
)
from house_helper.persistence import favorites
from house_helper.persistence.authn import AuthenticatedSession
from house_helper.persistence.models import MAX_FAVORITE_NOTES_LENGTH
from house_helper.persistence.conversations import ConversationNotFound
from house_helper.web.chat_api import (
    _UUID,
    ChatRoute,
    _conversation_id,
    _read_json_object,
    _timestamp,
    authenticated,
    authenticated_mutation,
)
from house_helper.web.database import WebDatabase
from house_helper.web.dependencies import get_database
from house_helper.web.diagnostics import ApiError

# The largest message id the body may name (the same bound as the history's ?before=).
_MAX_MESSAGE_ID = 2**62

# The largest body a valid edit can send, which is much larger than the chat API's
# MAX_BODY: an edit carries every field of the favorite, and a favorite's own bounds are
# far wider than a message's. A text favorite's text is at most MAX_FAVORITE_TEXT_LENGTH
# characters; a structured favorite's title, servings, ingredients and steps are at most
# MAX_RECIPE_BLOCK_LENGTH characters together on the protocol's compact wire form. A
# title and the notes are bounded separately (MAX_TITLE_LENGTH, and the notes' own
# MAX_FAVORITE_NOTES_LENGTH). JSON may write one character as twelve bytes (the two
# \uXXXX escapes of one outside the basic plane; a quote or a backslash takes two, other
# characters one to four), and the field names, the expected version and the punctuation
# add a few dozen more. The larger kind decides. It is still one finite, defensive bound,
# larger than any valid edit and far smaller than an oversized request may claim.
MAX_EDIT_BODY = 12 * (
    max(favorites.MAX_FAVORITE_TEXT_LENGTH, MAX_RECIPE_BLOCK_LENGTH)
    + MAX_TITLE_LENGTH
    + MAX_FAVORITE_NOTES_LENGTH
) + 1024

_EDITABLE_FIELDS = frozenset(
    {"expected_version", "title", "text", "notes", "servings", "ingredients", "steps"}
)

router = APIRouter(prefix="/api", route_class=ChatRoute)


def _favorite_id(raw: str) -> UUID:
    if _UUID.fullmatch(raw) is None:
        raise ApiError("favorite_not_found")
    return UUID(raw)


def _summary_json(item: favorites.FavoriteSummary) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "title": item.title,
        "message_id": item.source_message_id,
        "created_at": _timestamp(item.created_at),
        "kind": item.kind,
        "recipe_index": item.source_recipe_index,
        "version": item.version,
    }


def _ingredient_json(item: Any) -> dict[str, str]:
    return {"name": item.name, "amount": item.amount}


def _detail_json(found: favorites.FavoriteDetail) -> dict[str, Any]:
    return {
        "id": str(found.id),
        "title": found.title,
        "text": found.text,
        "message_id": found.source_message_id,
        "created_at": _timestamp(found.created_at),
        "updated_at": _timestamp(found.updated_at),
        "kind": found.kind,
        "recipe_index": found.source_recipe_index,
        "version": found.version,
        "notes": found.notes,
        "servings": found.servings,
        "ingredients": (
            None
            if found.ingredients is None
            else [_ingredient_json(item) for item in found.ingredients]
        ),
        "steps": None if found.steps is None else list(found.steps),
    }


def _kept_answer(document: dict) -> tuple[UUID, int, Optional[int]]:
    """The conversation, the message and the optional recipe position a request names:
    exactly those fields, and nothing else. A conversation id that is not a UUID is a
    conversation that is not there; a message id that is not a positive whole number (a
    boolean included) is a request that cannot be understood."""
    if set(document) not in (
        {"conversation_id", "message_id"},
        {"conversation_id", "message_id", "recipe_index"},
    ):
        raise ApiError("invalid_request", category="UnexpectedFields")
    conversation, message = document["conversation_id"], document["message_id"]
    if not isinstance(conversation, str):
        raise ApiError("invalid_request", category="UnexpectedFields")
    if isinstance(message, bool) or not isinstance(message, int) or not 1 <= message <= _MAX_MESSAGE_ID:
        raise ApiError("invalid_request", category="UnexpectedFields")
    recipe_index = document.get("recipe_index")
    if "recipe_index" in document and (
        isinstance(recipe_index, bool)
        or not isinstance(recipe_index, int)
        or not 0 <= recipe_index < MAX_RECIPES_PER_REPLY
    ):
        raise ApiError("invalid_request", category="UnexpectedFields")
    return _conversation_id(conversation), message, recipe_index


def _edit(document: dict) -> tuple[int, dict[str, Any]]:
    """The expected version and the fields an edit names. Unknown fields, a missing or
    invalid expected_version and an edit that names no field are refused here; whether the
    kind has the field is decided against the row (persistence.favorites)."""
    if set(document) - _EDITABLE_FIELDS:
        raise ApiError("invalid_request", category="UnexpectedFields")
    if "expected_version" not in document:
        raise ApiError("invalid_request", category="UnexpectedFields")
    expected_version = document["expected_version"]
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise ApiError("invalid_request", category="UnexpectedFields")
    changes = {
        name: value for name, value in document.items() if name != "expected_version"
    }
    if not changes:
        raise ApiError("invalid_request", category="UnexpectedFields")
    return expected_version, changes


@router.get("/favorites")
async def list_favorites(
    current: AuthenticatedSession = Depends(authenticated),
    database: WebDatabase = Depends(get_database),
) -> Response:
    kept = await database.run(lambda session: favorites.list_favorites(session, current.user_id))
    return JSONResponse({"favorites": [_summary_json(item) for item in kept]})


@router.post("/favorites")
async def keep_favorite(
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    conversation_id, message_id, recipe_index = _kept_answer(await _read_json_object(request))
    now = request.app.state.clock()
    try:
        saved = await database.run(
            lambda session: favorites.save_favorite(
                session,
                current.user_id,
                conversation_id,
                message_id,
                now=now,
                recipe_index=recipe_index,
            )
        )
    except ConversationNotFound:
        raise ApiError("conversation_not_found") from None
    except favorites.MessageNotFound:
        raise ApiError("message_not_found") from None
    except favorites.RecipeNotFound:
        raise ApiError("recipe_not_found") from None
    except favorites.FavoritesFull:
        raise ApiError("favorites_full") from None
    body = {**_summary_json(saved.favorite), "created": saved.created}
    return JSONResponse(body, status_code=201 if saved.created else 200)


@router.patch("/favorites/{favorite_id}")
async def edit_favorite(
    favorite_id: str,
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    wanted = _favorite_id(favorite_id)
    expected_version, changes = _edit(
        await _read_json_object(request, limit=MAX_EDIT_BODY)
    )
    now = request.app.state.clock()
    try:
        updated = await database.run(
            lambda session: favorites.update_favorite(
                session,
                current.user_id,
                wanted,
                expected_version=expected_version,
                changes=changes,
                now=now,
            )
        )
    except favorites.FavoriteNotFound:
        raise ApiError("favorite_not_found") from None
    except favorites.FavoriteConflict:
        raise ApiError("favorite_conflict") from None
    except favorites.InvalidFavorite:
        raise ApiError("invalid_request", category="InvalidFavoriteField") from None
    return JSONResponse(_detail_json(updated))


@router.get("/favorites/{favorite_id}")
async def read_favorite(
    favorite_id: str,
    current: AuthenticatedSession = Depends(authenticated),
    database: WebDatabase = Depends(get_database),
) -> Response:
    wanted = _favorite_id(favorite_id)
    try:
        found = await database.run(
            lambda session: favorites.get_favorite(session, current.user_id, wanted)
        )
    except favorites.FavoriteNotFound:
        raise ApiError("favorite_not_found") from None
    return JSONResponse(_detail_json(found))


@router.delete("/favorites/{favorite_id}")
async def remove_favorite(
    favorite_id: str,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    wanted = _favorite_id(favorite_id)
    try:
        await database.run(
            lambda session: favorites.remove_favorite(session, current.user_id, wanted)
        )
    except favorites.FavoriteNotFound:
        raise ApiError("favorite_not_found") from None
    return Response(status_code=204)
