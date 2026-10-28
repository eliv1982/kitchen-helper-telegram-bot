"""The favorites API: assistant answers a user chose to keep.

Four routes under /api, all answering JSON, built the way chat_api.py's are (the same route
class, so the same correlation id, headers, error catalogue and log line; the same session,
Origin and body rules):

  GET    /api/favorites                the user's favorites, newest first, without their text
  POST   /api/favorites                keep an answer (body: {"conversation_id", "message_id"})
  GET    /api/favorites/{id}           one favorite with its text
  DELETE /api/favorites/{id}           remove it

Who. The user is whoever the session cookie resolves to on the server; a user id or an owner
in the request is never read. A favorite that does not exist, belongs to somebody else or is
not even a UUID is the same 404 "favorite_not_found" for every route; a conversation that is
not the user's (or not a UUID) is the same 404 "conversation_not_found" as in the chat API.

Keeping an answer (POST). The request names a conversation and a message and nothing else:
the text that is kept is read from the database, from an assistant message of that
conversation that has not expired, and is never taken from the request, so a favorite can
only ever be a real answer of the assistant to this user. The answers:

  201  kept: {"id", "title", "message_id", "created_at", "created": true}
  200  that answer was already a favorite: the same body with "created": false, and nothing
         is written (so a tap that is repeated, or a retry after a lost reply, is harmless)
  404  conversation_not_found / message_not_found (no such answer in it, not the assistant's
         or expired)
  409  favorites_full: the user keeps as many as they may

A favorite is a copy (see persistence.favorites): it has no deadline and does not depend on
the conversation, so it stays when the history expires. Removing one (DELETE, 204) deletes
only the copy.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from house_helper.persistence import favorites
from house_helper.persistence.authn import AuthenticatedSession
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
    }


def _kept_answer(document: dict) -> tuple[UUID, int]:
    """The conversation and the message a request names: exactly those two fields. A
    conversation id that is not a UUID is a conversation that is not there; a message id
    that is not a positive whole number (a boolean included) is a request that cannot be
    understood."""
    if set(document) != {"conversation_id", "message_id"}:
        raise ApiError("invalid_request", category="UnexpectedFields")
    conversation, message = document["conversation_id"], document["message_id"]
    if not isinstance(conversation, str):
        raise ApiError("invalid_request", category="UnexpectedFields")
    if isinstance(message, bool) or not isinstance(message, int) or not 1 <= message <= _MAX_MESSAGE_ID:
        raise ApiError("invalid_request", category="UnexpectedFields")
    return _conversation_id(conversation), message


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
    conversation_id, message_id = _kept_answer(await _read_json_object(request))
    now = request.app.state.clock()
    try:
        saved = await database.run(
            lambda session: favorites.save_favorite(
                session, current.user_id, conversation_id, message_id, now=now
            )
        )
    except ConversationNotFound:
        raise ApiError("conversation_not_found") from None
    except favorites.MessageNotFound:
        raise ApiError("message_not_found") from None
    except favorites.FavoritesFull:
        raise ApiError("favorites_full") from None
    body = {**_summary_json(saved.favorite), "created": saved.created}
    return JSONResponse(body, status_code=201 if saved.created else 200)


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
    return JSONResponse(
        {
            "id": str(found.id),
            "title": found.title,
            "text": found.text,
            "message_id": found.source_message_id,
            "created_at": _timestamp(found.created_at),
        }
    )


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
