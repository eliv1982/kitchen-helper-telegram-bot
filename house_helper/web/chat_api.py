"""The web chat API: conversations and their messages.

Four routes, all under /api, all answering JSON:

  POST /api/conversations                       a new, empty conversation (body: {})
  GET  /api/conversations                       the user's conversations, newest activity first,
                                                each named by its first unexpired message
  GET  /api/conversations/{id}/messages         the history (?limit=1..100, ?before=<message id>)
  POST /api/conversations/{id}/messages         send a message, get the assistant's answer

Who. The user is whoever the session cookie resolves to on the server
(auth.require_user); a user id, a conversation owner or a role in the request is never
read. A conversation id that does not exist, belongs to somebody else or is not even a
UUID is the same 404 "conversation_not_found" for every route: nothing tells the caller
which of them it was.

Sending a message (POST .../messages). The header `Idempotency-Key` (1 to 128 printable
ASCII characters, chosen by the client, one per message) is required, and the body is
{"text": "..."}:

  201  the message was answered:
         {"conversation_id", "user_message": {...}, "assistant_message": {...},
          "truncated": bool, "replayed": false}
  200  the same key again with the same text, for a message that was answered: the same
         body, "replayed": true, and the header Idempotent-Replayed: true. Nothing is
         generated twice.
  409  conversation_busy (another message of this conversation is being answered) or
         turn_in_progress (this very request is): retryable, with Retry-After. Or
         turn_superseded / idempotency_key_unusable: not retryable with this key. Or
         idempotency_key_reused: the key was used before for a different text, a conflict
         with that earlier request and not a retry of it (send the message with a new key).
  5xx  the assistant failed (provider_*, interrupted) or the service did: the user's
         message is kept, marked "failed" in the history, `user_message_id` is in the
         error, and sending again with the SAME key tries again. A failed message does
         not become part of what the assistant is shown later.

Errors. The catalogue and the shape of every error body are in diagnostics.py. Every
response, success or error, carries X-Correlation-ID (made by the server for this
request), `Cache-Control: no-store` and `Referrer-Policy: no-referrer`. Nothing the
client sent is echoed in an error: a request that cannot be understood is "invalid_request"
and not a quotation of it, and FastAPI's own validation errors (which quote the input) can
never reach the client because ChatRoute turns them into that. An unexpected exception is
logged by its class name and answered "internal_error": no traceback, no message.

Requests. A mutation must be application/json (a cross-site HTML form cannot send it),
at most MAX_BODY bytes, strict UTF-8 JSON without duplicate keys, with exactly the
fields it names. Its Origin, if the browser sends one, must be this site's (a request
without one passes, as for logout, unless it says it is cross-site in Sec-Fetch-Site).
The message text is trimmed, must not be empty, must not exceed MAX_INPUT_LENGTH
characters, and must not contain a control character other than tab, newline and
carriage return, or a lone surrogate: those cannot be stored everywhere (NUL cannot be
in a PostgreSQL text value, a surrogate cannot be encoded as UTF-8). No CORS header is
sent, ever: the page is served from the same origin as the API.

Reading writes nothing: the two GET routes take no write lock and never move a
conversation's updated_at.
"""

import json
import re
from typing import Any, Callable, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import ClientDisconnect

from house_helper.core.limits import MAX_INPUT_LENGTH
from house_helper.persistence import conversations
from house_helper.persistence.authn import AuthenticatedSession
from house_helper.persistence.conversations import ConversationNotFound, MessageView
from house_helper.persistence.identifiers import validate_idempotency_key
from house_helper.web.auth import AuthService, get_auth_service, require_user
from house_helper.web.database import DatabaseClosed, WebDatabase
from house_helper.web.dependencies import get_database
from house_helper.web.diagnostics import (
    CORRELATION_HEADER,
    ApiError,
    error_response,
    log_error,
    new_correlation_id,
)
from house_helper.web.turn_runner import TurnResult, WebTurnRunner

# 2000 characters, each at worst a 12-byte JSON escape of a character outside the
# basic plane ("😀"), and the framing around them.
MAX_BODY = 32 * 1024

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100

_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_UNSTORABLE_TEXT = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_NUMBER = re.compile(r"[0-9]{1,18}")


# --- the route wrapper ---------------------------------------------------------------


def _error_from_http(exc: HTTPException) -> ApiError:
    """An HTTPException raised by a dependency (auth.require_user, get_auth_service)."""
    if exc.status_code == 401:
        return ApiError("not_authenticated")
    if exc.status_code == 503:
        return ApiError("service_unavailable", category=f"HTTP{exc.status_code}")
    return ApiError("internal_error", category=f"HTTP{exc.status_code}")


class ChatRoute(APIRoute):
    """Every route of the chat API goes through here: the correlation id is made, and
    whatever goes wrong becomes one of the catalogue's errors with that id in it, in
    the log and in the body, without anything from the exception reaching either."""

    def get_route_handler(self) -> Callable[[Request], Any]:
        original = super().get_route_handler()
        route = f"{'/'.join(sorted(self.methods or ()))} {self.path}"

        async def handler(request: Request) -> Response:
            correlation_id = new_correlation_id()  # never taken from the request
            request.state.correlation_id = correlation_id
            request.state.route = route
            headers = {**_PRIVATE_HEADERS, CORRELATION_HEADER: correlation_id}
            try:
                response = await original(request)
            except ApiError as caught:
                error = caught  # (the name after "as" does not outlive its clause)
            except HTTPException as exc:
                error = _error_from_http(exc)
            except RequestValidationError:
                error = ApiError("invalid_request", category="RequestValidationError")
            except ClientDisconnect:
                error = ApiError("invalid_request", category="ClientDisconnect")
            except DatabaseClosed:
                error = ApiError("service_unavailable", category="DatabaseClosed")
            except SQLAlchemyError as exc:
                error = ApiError("service_unavailable", category=type(exc).__name__)
            except Exception as exc:  # a bug; CancelledError is not an Exception
                error = ApiError("internal_error", category=type(exc).__name__)
            else:
                response.headers.update(headers)
                return response
            if not error.logged:
                log_error(error, correlation_id, route)
            return error_response(error, correlation_id, headers)

        return handler


router = APIRouter(prefix="/api", route_class=ChatRoute)


# --- who is asking -------------------------------------------------------------------


def _origin_is_foreign(request: Request, own_origin: str) -> bool:
    """True if the browser says the request comes from another site: an Origin that is
    not ours ("null" included), or, with no Origin, a Sec-Fetch-Site that is neither
    ours nor the address bar. A request with neither header is not from a browser
    (a browser sends Origin with every POST) and carries no cookie it did not choose to."""
    origins = request.headers.getlist("origin")
    if origins:
        return len(origins) != 1 or origins[0] != own_origin
    return request.headers.get("sec-fetch-site", "same-origin").lower() not in ("same-origin", "none")


async def authenticated(
    request: Request, auth: AuthService = Depends(get_auth_service)
) -> AuthenticatedSession:
    """The user of a read: 401 for anything but a live session (require_user)."""
    return await require_user(request, auth)


async def authenticated_mutation(
    request: Request, auth: AuthService = Depends(get_auth_service)
) -> AuthenticatedSession:
    """The user of a write. The Origin is checked first: a request from another site is
    refused whether or not it carries a session."""
    if _origin_is_foreign(request, auth.settings.origin):
        raise ApiError("forbidden_origin")
    return await require_user(request, auth)


# --- reading a request ---------------------------------------------------------------


def _conversation_id(raw: str) -> UUID:
    if _UUID.fullmatch(raw) is None:
        raise ApiError("conversation_not_found")
    return UUID(raw)


def _query_number(
    request: Request, name: str, *, default: Optional[int], low: int, high: int
) -> Optional[int]:
    values = request.query_params.getlist(name)
    if not values:
        return default
    if len(values) != 1 or _NUMBER.fullmatch(values[0]) is None:
        raise ApiError("invalid_query")
    number = int(values[0])
    if not low <= number <= high:
        raise ApiError("invalid_query")
    return number


async def _read_json_object(request: Request) -> dict:
    """The body as a JSON object. ApiError for anything else: a content type other than
    application/json (UTF-8 if it says), a body over MAX_BODY, bytes that are not UTF-8,
    JSON that does not parse or repeats a key, or a value that is not an object."""
    media, *parameters = (part.strip() for part in request.headers.get("content-type", "").split(";"))
    if media.lower() != "application/json":
        raise ApiError("unsupported_media_type")
    for parameter in parameters:
        name, _, value = parameter.partition("=")
        if name.strip().lower() != "charset" or value.strip().strip('"').lower() != "utf-8":
            raise ApiError("unsupported_media_type")

    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isascii() or not declared.isdigit() or int(declared) > MAX_BODY):
        raise ApiError("request_too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise ApiError("request_too_large")
        chunks.append(chunk)

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate key")
        return dict(pairs)

    try:
        document = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=no_duplicates)
    except (ValueError, RecursionError):  # not UTF-8, not JSON, nested too deep, repeats a key
        raise ApiError("invalid_request", category="MalformedJson") from None
    if not isinstance(document, dict):
        raise ApiError("invalid_request", category="NotAnObject")
    return document


def _idempotency_key(request: Request) -> str:
    values = request.headers.getlist("idempotency-key")
    if not values:
        raise ApiError("idempotency_key_required")
    try:
        if len(values) != 1:
            raise ValueError("repeated header")
        return validate_idempotency_key(values[0])
    except ValueError:
        raise ApiError("invalid_idempotency_key") from None


def _message_text(document: dict) -> str:
    if set(document) != {"text"} or not isinstance(document["text"], str):
        raise ApiError("invalid_request", category="UnexpectedFields")
    text = document["text"].strip()
    if not text:
        raise ApiError("empty_message")
    if _UNSTORABLE_TEXT.search(text) is not None:
        raise ApiError("invalid_message_text")
    if len(text) > MAX_INPUT_LENGTH:
        raise ApiError("message_too_long", max_length=MAX_INPUT_LENGTH)
    return text


# --- writing a response --------------------------------------------------------------


def _timestamp(value: Any) -> str:
    return value.isoformat()


def _conversation_json(summary: conversations.ConversationSummary) -> dict[str, Any]:
    return {
        "id": str(summary.id),
        "title": summary.title,
        "created_at": _timestamp(summary.created_at),
        "updated_at": _timestamp(summary.updated_at),
    }


def _message_json(view: MessageView) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": view.id,
        "role": view.role,
        "text": view.text,
        "created_at": _timestamp(view.created_at),
        "status": view.status,
    }
    if view.idempotency_key is not None:
        body["idempotency_key"] = view.idempotency_key
    if view.error_code is not None:
        body["error_code"] = view.error_code
    return body


def _turn_json(result: TurnResult) -> dict[str, Any]:
    done = result.completed
    return {
        "conversation_id": str(done.conversation_id),
        "user_message": {
            "id": done.user_message.id,
            "role": "user",
            "text": done.user_message.text,
            "created_at": _timestamp(done.user_message.created_at),
            "status": conversations.MESSAGE_COMPLETED,
        },
        "assistant_message": {
            "id": done.assistant_message.id,
            "role": "assistant",
            "text": done.assistant_message.text,
            "created_at": _timestamp(done.assistant_message.created_at),
            "status": conversations.MESSAGE_COMPLETED,
        },
        "truncated": done.truncated,
        "replayed": result.replayed,
    }


# --- the routes ----------------------------------------------------------------------


@router.post("/conversations")
async def create_conversation(
    request: Request,
    current: AuthenticatedSession = Depends(authenticated_mutation),
    database: WebDatabase = Depends(get_database),
) -> Response:
    if await _read_json_object(request):
        raise ApiError("invalid_request", category="UnexpectedFields")
    now = request.app.state.clock()
    created = await database.run(
        lambda session: conversations.create_conversation(session, current.user_id, now=now)
    )
    return JSONResponse(_conversation_json(created), status_code=201)


@router.get("/conversations")
async def list_conversations(
    request: Request,
    current: AuthenticatedSession = Depends(authenticated),
    database: WebDatabase = Depends(get_database),
) -> Response:
    limit = _query_number(
        request, "limit", default=DEFAULT_LIST_LIMIT, low=1, high=MAX_LIST_LIMIT
    )
    now = request.app.state.clock()
    page = await database.run(
        lambda session: conversations.list_conversations(session, current.user_id, now=now, limit=limit)
    )
    return JSONResponse(
        {
            "conversations": [_conversation_json(item) for item in page.conversations],
            "has_more": page.has_more,
        }
    )


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    request: Request,
    conversation_id: str,
    current: AuthenticatedSession = Depends(authenticated),
    database: WebDatabase = Depends(get_database),
) -> Response:
    wanted = _conversation_id(conversation_id)
    limit = _query_number(
        request, "limit", default=DEFAULT_LIST_LIMIT, low=1, high=MAX_LIST_LIMIT
    )
    before = _query_number(request, "before", default=None, low=1, high=2**62)
    now = request.app.state.clock()
    try:
        page = await database.run(
            lambda session: conversations.list_messages(
                session, current.user_id, wanted, now=now, limit=limit, before=before
            )
        )
    except ConversationNotFound:
        raise ApiError("conversation_not_found") from None
    return JSONResponse(
        {
            "conversation_id": str(wanted),
            "messages": [_message_json(view) for view in page.messages],
            "has_more": page.has_more,
            "next_before": page.messages[0].id if page.has_more and page.messages else None,
        }
    )


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    request: Request,
    conversation_id: str,
    current: AuthenticatedSession = Depends(authenticated_mutation),
) -> Response:
    runner: Optional[WebTurnRunner] = request.app.state.chat
    if runner is None:
        raise ApiError("chat_unavailable", category="NotConfigured")
    wanted = _conversation_id(conversation_id)
    document = await _read_json_object(request)
    key = _idempotency_key(request)
    text = _message_text(document)

    result = await runner.submit(
        user_id=current.user_id,
        conversation_id=wanted,
        idempotency_key=key,
        text=text,
        correlation_id=request.state.correlation_id,
        route=request.state.route,
    )
    if result.replayed:
        return JSONResponse(
            _turn_json(result), status_code=200, headers={"Idempotent-Replayed": "true"}
        )
    return JSONResponse(_turn_json(result), status_code=201)
