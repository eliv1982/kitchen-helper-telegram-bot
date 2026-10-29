"""Telegram invitation sign-in: the routes that begin it.

Three routes, under /auth/invite (the end of the sign-in is the Telegram callback in auth.py,
which serves ordinary logins too):

  GET  /auth/invite         the page the invitation link opens (nothing is used up)
  POST /auth/invite/start   begin: verify the invitation's secret, remember the login, say
                            where at Telegram to continue (JSON: {"code": "<id>.<secret>"})
  GET  /auth/invite/done    the page a person lands on once the identity is linked

The point is that an invitation is the only thing that says which user a new Telegram
identity belongs to, so the server, and nobody else, decides whether an invitation is live,
once, at the start; and what the callback later claims is the id that was verified here, kept
in the login's own server-side state (persistence.invitations.begin_invitation_login), never a
value that arrives with a request. The secret is in the link's fragment, which a browser
never sends; the page's script (invite_pages.py) posts it in the body of this request, over
TLS, to this origin, and it is in no query string, redirect, cookie, log or error.

Starting (POST /auth/invite/start). The answer says nothing about why an invitation is not
valid: a malformed code, an unknown one, a used, withdrawn, superseded or expired one, and
one of a user who cannot be linked (disabled, or already linked) are all alike, 400. A live
invitation gets 200 with the address at Telegram (the same URL a login's redirect would carry)
and the cookie that binds the login to this browser, exactly as for any login. Nothing is
used up: the invitation is used by the callback, and only after a verified Telegram identity
has come back. The request must be JSON (which a cross-site HTML form cannot send), is small
(read up to a fixed size), and it must not come from another site: its Origin, if the browser
sends one, must be this site's, and without one the fetch metadata may not say otherwise.
It never reads who the user is from the request: the one thing it asks of the session cookie
is whether the browser is signed in as somebody OTHER than the invitation's user, in which
case it is 409 (sign out, then use the invitation) and nothing is stored; the callback checks
the same again, because a browser can change who it is signed in as in between.

Nothing is answered when Telegram login is not configured: the page and the start are 503.
Nothing secret is logged: not the code, its secret or the id. A database error is logged by
its class only (see auth.py).
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from house_helper.persistence import invitations
from house_helper.web import invite_pages
from house_helper.web.auth import LANDING_PATH, AuthService

logger = logging.getLogger(__name__)

MAX_REQUEST_BODY = 512  # bytes; a code is 80 characters

_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

# The user-facing texts of the JSON endpoint. Fixed: nothing from the request is echoed into them.
_MESSAGE_INVALID_REQUEST = "Запрос не удалось разобрать."
_MESSAGE_INVALID_INVITATION = (
    "Приглашение недействительно, уже использовано или его срок истек. Попросите выдать новое."
)
_MESSAGE_OTHER_USER = (
    "В этом браузере уже выполнен вход в другую учетную запись. Выйдите из нее и откройте "
    "ссылку-приглашение снова."
)
_MESSAGE_FORBIDDEN = "Запрос отклонен: он пришел не с нашего сайта."
_MESSAGE_NOT_CONFIGURED = "Вход через Telegram сейчас не настроен. Обратитесь к тому, кто прислал приглашение."
_MESSAGE_UNAVAILABLE = "Сервис временно недоступен. Попробуйте еще раз чуть позже."
_MESSAGE_ERROR = "Что-то пошло не так. Попробуйте еще раз чуть позже."

router = APIRouter(prefix="/auth/invite")


def _telegram_service(request: Request) -> Optional[AuthService]:
    """The running application's sign-in service if Telegram login is configured, else None
    (the application is not running, or only email login is)."""
    service = getattr(request.app.state, "auth", None)
    return service if service is not None and service.telegram_enabled else None


def _json(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"detail": code, "message": message}, status_code=status_code, headers=_PRIVATE_HEADERS
    )


def _page(page: invite_pages.Page) -> Response:
    return Response(page.html, status_code=page.status_code, media_type="text/html", headers=page.headers)


async def _read_limited(request: Request, limit: int) -> Optional[bytes]:
    """The request body, or None if it is larger than `limit` (the rest is not read)."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _origin_is_foreign(request: Request, own_origin: str) -> bool:
    """True if the browser says the request comes from another site: an Origin that is not
    ours ("null" included, and more than one), or, with no Origin, a Sec-Fetch-Site that is
    neither ours nor the address bar. A request with neither header is not from a browser (a
    browser sends Origin with every POST). The rule of the chat API's writes (chat_api.py)."""
    origins = request.headers.getlist("origin")
    if origins:
        return len(origins) != 1 or origins[0] != own_origin
    return request.headers.get("sec-fetch-site", "same-origin").lower() not in ("same-origin", "none")


async def _requested_code(request: Request) -> Optional[str]:
    """The code in the body, None for a body that is not exactly {"code": <text>}."""
    if _media_type(request) != "application/json":
        return None
    body = await _read_limited(request, MAX_REQUEST_BODY)
    if body is None:
        return None
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):  # not JSON, or nested past what the parser takes
        return None
    if not isinstance(document, dict) or set(document) != {"code"}:
        return None
    code = document["code"]
    return code if isinstance(code, str) else None


# --- GET /auth/invite -------------------------------------------------------------------


@router.get("")
async def invitation_page(request: Request) -> Response:
    if _telegram_service(request) is None:
        return _page(invite_pages.not_configured_page())
    return _page(invite_pages.invitation_page())


# --- POST /auth/invite/start ------------------------------------------------------------


@router.post("/start")
async def start(request: Request) -> Response:
    auth = _telegram_service(request)
    if auth is None:
        return _json(503, "login_unavailable", _MESSAGE_NOT_CONFIGURED)
    if _origin_is_foreign(request, auth.settings.origin):
        return _json(403, "forbidden_origin", _MESSAGE_FORBIDDEN)
    code = await _requested_code(request)
    if code is None:
        return _json(400, "invalid_request", _MESSAGE_INVALID_REQUEST)
    parsed = invitations.parse_invitation_code(code)
    if parsed is None:
        logger.info("A Telegram invitation was not valid")
        return _json(400, "invitation_invalid", _MESSAGE_INVALID_INVITATION)
    invitation_id, token = parsed

    try:
        current = await auth.authenticate(request.cookies.get(auth.settings.session_cookie_name))
        authorization = await auth.start_invitation_login(
            invitation_id, token, current.user_id if current is not None else None
        )
    except invitations.SignedInAsAnotherUser:
        logger.info("An invitation sign-in was refused: the browser is signed in as another user")
        return _json(409, "signed_in_as_another_user", _MESSAGE_OTHER_USER)
    except HTTPException as unavailable:
        return _json(unavailable.status_code, str(unavailable.detail), _MESSAGE_UNAVAILABLE)
    except Exception as exc:
        logger.error("An unexpected error stopped an invitation sign-in (%s)", type(exc).__name__)
        return _json(500, "server_error", _MESSAGE_ERROR)
    if authorization is None:
        return _json(400, "invitation_invalid", _MESSAGE_INVALID_INVITATION)

    response = JSONResponse(
        {"authorization_url": authorization.url}, status_code=200, headers=_PRIVATE_HEADERS
    )
    auth.set_cookie(
        response,
        auth.settings.login_cookie_name,
        authorization.state,
        int(auth.settings.login_lifetime.total_seconds()),
    )
    return response


# --- GET /auth/invite/done --------------------------------------------------------------


@router.get("/done")
async def done(request: Request) -> Response:
    """The page after a successful link. It says so only to a browser that is signed in; for
    anybody else it is the application's page, which asks them to sign in."""
    auth = getattr(request.app.state, "auth", None)
    try:
        current = (
            None
            if auth is None
            else await auth.authenticate(request.cookies.get(auth.settings.session_cookie_name))
        )
    except HTTPException:
        return _page(invite_pages.unavailable_page())
    if current is None:
        return RedirectResponse(LANDING_PATH, status_code=303, headers=_PRIVATE_HEADERS)
    return _page(invite_pages.done_page())
