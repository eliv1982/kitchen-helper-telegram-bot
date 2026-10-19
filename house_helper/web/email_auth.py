"""Email sign-in: the HTTP surface and the logic behind it.

Three routes, under /auth/email, next to the Telegram ones (auth.py):

  POST /auth/email/request   ask for a link for an address (JSON: {"email": "..."})
  GET  /auth/email/confirm   the page the emailed link opens (nothing is used up)
  POST /auth/email/confirm   confirm: use the link up, open a session

The point of the design is that an address is a way into an account that already
exists, never a way to make one or to join two. A link is mailed only to an address
that an administrator put on the allowlist for a user (persistence.email_login says
exactly when); possession of a mailbox proves nothing else. The routes never read a
user id from the client: the user is whoever owns the allowlisted address, and a
session is opened through that address's identity link like any other sign-in, so it
records its provenance and is ended by revoking the identity (allowlist.revoke).

Asking (POST /auth/email/request). The answer must not tell the asker whether the
address is registered, so every well-formed request gets the same 202 and the same
body, whether a link was mailed, the address is unknown or revoked, or the last link
was issued a moment ago (the cooldown). Only a malformed request differs (400),
because that is decided from the asker's own text alone. The mail is sent after the
answer has gone out (a background task), so that how long the answer takes does not
say either. If the mail cannot be sent, the link is withdrawn and the asker still sees
the same 202: a failure that showed only for registered addresses would be the same
leak (the cause is in the server log, by class name only; the person asks again after
the cooldown). The body of a request is small and read up to a fixed size; it must be
JSON, which a cross-site HTML form cannot send; and Origin, if the browser sends one,
must be this site's.

The link. https://<public url>/auth/email/confirm?c=<challenge id>#<secret>. The
secret is in the fragment, which browsers do not send, so it is in no log, no Referer
and no redirect (see email_pages for what the page's script does with it). The id in
the query is not a secret; it lets the page say which address the link is for.

Looking (GET). The page shows the address masked ("e***@example.com") and a button.
It is served for a link that could be used and for none other; every other case, the
unknown, the used, the expired, the withdrawn, gets the same "invalid link" page.
Nothing is used up by looking: mail scanners fetch links.

Confirming (POST). The form carries the id and the secret. In one transaction the link
is used up and a session is opened (persistence.email_login.redeem_email_login); the
answer is a redirect to the application's page (auth.LANDING_PATH) with the session
cookie. Origin is checked as logout checks it. The link may be opened on another device
than the one that asked: nothing
ties it to the requesting browser, because its secret travels only in the mailbox.
What it must not do is change who a browser is: if the browser presented a session of
a DIFFERENT user, the answer is 409 and the link is not used up (sign out, then open it
again); the same user just gets a new session and a replaced cookie.

Nothing secret is logged: not the address, the link, its secret or the id. A database
error is logged by its class only (see auth.py).
"""

import json
import logging
import math
from typing import Optional
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.background import BackgroundTask

from house_helper.persistence import email_login
from house_helper.persistence.authn import IssuedSession, LoginDenied
from house_helper.persistence.email_login import (
    EmailLoginLink,
    IssueResult,
    LinkPreview,
    SignInConflict,
)
from house_helper.persistence.identifiers import PROVIDER_EMAIL, normalize_identity
from house_helper.web import email_pages
from house_helper.web.auth import LANDING_PATH, AuthService, get_auth_service
from house_helper.web.mailer import (
    EmailDeliveryError,
    EmailSender,
    compose_login_email,
    login_link,
    minutes_in_russian,
)
from house_helper.web.settings import EmailSettings

logger = logging.getLogger(__name__)

MAX_REQUEST_BODY = 2048  # bytes; an address is at most 320 characters
MAX_FORM_BODY = 1024  # the id (36) and the secret (43) and their names

_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

# The user-facing texts of the JSON endpoint. Fixed: nothing from the request is
# echoed into them.
_MESSAGE_INVALID_EMAIL = "Введите корректный адрес электронной почты."
_MESSAGE_INVALID_REQUEST = "Запрос не удалось разобрать."
_MESSAGE_FORBIDDEN = "Запрос отклонён: он пришёл не с нашего сайта."
_MESSAGE_UNAVAILABLE = "Сервис временно недоступен. Попробуйте ещё раз чуть позже."
_MESSAGE_ERROR = "Что-то пошло не так. Попробуйте ещё раз чуть позже."


def _request_accepted_message(settings: EmailSettings) -> str:
    minutes = max(1, math.ceil(settings.link_lifetime.total_seconds() / 60))
    return (
        "Если этот адрес есть в списке разрешённых, мы отправили на него письмо со ссылкой "
        f"для входа. Ссылка действует {minutes_in_russian(minutes)}. Если письма нет, проверьте "
        "папку «Спам» или подождите немного и запросите ссылку снова."
    )


class EmailLoginService:
    """The email sign-in logic, apart from HTTP. One per running application."""

    def __init__(self, settings: EmailSettings, auth: AuthService, sender: EmailSender) -> None:
        self.settings = settings
        self._auth = auth
        self._sender = sender

    async def request(self, address: str) -> IssueResult:
        """Issue a link if there is one to issue (see persistence.email_login)."""
        now = self._auth.now()
        result = await self._auth.run(
            lambda session: email_login.issue_email_login(
                session,
                address,
                now=now,
                lifetime=self.settings.link_lifetime,
                cooldown=self.settings.request_cooldown,
            )
        )
        logger.info("A sign-in link was requested (%s)", result.outcome.value)
        return result

    async def deliver(self, link: EmailLoginLink) -> None:
        """Mail the link, or withdraw it. Runs after the answer has been sent, so it
        must not raise: nobody is left to receive the exception."""
        try:
            message = compose_login_email(
                link.address,
                login_link(self.settings.origin, link.challenge_id, link.token),
                self.settings.link_lifetime,
            )
            await self._sender.send(message)
        except EmailDeliveryError as failure:
            logger.warning("A sign-in email could not be sent (%s)", failure.reason)
        except Exception as exc:  # a bug, or an address that cannot be mailed
            logger.warning("A sign-in email could not be sent (%s)", type(exc).__name__)
        else:
            logger.info("A sign-in email was handed to the mail server")
            return
        await self._withdraw(link.challenge_id)

    async def _withdraw(self, challenge_id: UUID) -> None:
        now = self._auth.now()
        try:
            await self._auth.run(
                lambda session: email_login.withdraw_email_login(session, challenge_id, now=now)
            )
        except HTTPException:
            # (AuthService.run has logged the class of the database error.) Nobody has
            # the secret, since the message was not delivered, so a link that stays on
            # record is harmless until it expires. Say so, and go on.
            logger.error("A sign-in link could not be withdrawn: the database failed")
        except Exception as exc:
            logger.error("A sign-in link could not be withdrawn (%s)", type(exc).__name__)

    async def preview(self, challenge_id: object) -> Optional[LinkPreview]:
        now = self._auth.now()
        return await self._auth.run(
            lambda session: email_login.preview_email_login(session, challenge_id, now=now)
        )

    async def redeem(
        self, challenge_id: str, token: str, signed_in_user: Optional[UUID]
    ) -> Optional[IssuedSession]:
        """A session for a live link, else None. Raises SignInConflict (see the module
        docstring) and HTTPException 503 for a database failure."""
        now = self._auth.now()
        issued = await self._auth.run(
            lambda session: email_login.redeem_email_login(
                session,
                challenge_id,
                token,
                now=now,
                signed_in_user=signed_in_user,
                lifetime=self.settings.session_lifetime,
            )
        )
        if issued is None:
            logger.info("A sign-in link was not valid")
        else:
            logger.info("User %s signed in by email", issued.user_id)
        return issued


async def get_email_service(request: Request) -> EmailLoginService:
    """The running application's email sign-in service; 503 when email login is not
    configured (or the application is not running)."""
    service = getattr(request.app.state, "email", None)
    if service is None:
        raise HTTPException(503, "login_unavailable", headers=_PRIVATE_HEADERS)
    return service


router = APIRouter(prefix="/auth/email")


# --- reading a request ----------------------------------------------------------------


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


def _origin_is_foreign(request: Request, settings: EmailSettings) -> bool:
    """True if the browser says the request comes from another site. A request with
    no Origin (not a browser, or one that does not send it) passes, as for logout; a
    browser always sends one with a POST, and "null" is not ours."""
    origin = request.headers.get("origin")
    return origin is not None and origin != settings.origin


def _json(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"detail": code, "message": message}, status_code=status_code, headers=_PRIVATE_HEADERS
    )


def _page(page: email_pages.Page) -> Response:
    return Response(
        page.html,
        status_code=page.status_code,
        media_type="text/html",
        headers=page.headers,
    )


# --- POST /auth/email/request ---------------------------------------------------------


async def _requested_address(request: Request) -> Optional[str]:
    """The normalized address in the body, None for a body that is not
    {"email": <text>}, and ValueError for text that cannot be an address."""
    if _media_type(request) != "application/json":
        return None
    body = await _read_limited(request, MAX_REQUEST_BODY)
    if body is None:
        return None
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):  # not JSON, or nested past what the parser takes
        return None
    if not isinstance(document, dict) or set(document) != {"email"}:
        return None
    given = document["email"]
    if not isinstance(given, str):
        return None
    return normalize_identity(PROVIDER_EMAIL, given)[1]  # ValueError: not an address


@router.post("/request")
async def request_link(
    request: Request, email: EmailLoginService = Depends(get_email_service)
) -> Response:
    if _origin_is_foreign(request, email.settings):
        return _json(403, "forbidden_origin", _MESSAGE_FORBIDDEN)
    try:
        address = await _requested_address(request)
    except ValueError:
        return _json(400, "invalid_email", _MESSAGE_INVALID_EMAIL)
    if address is None:
        return _json(400, "invalid_request", _MESSAGE_INVALID_REQUEST)

    try:
        result = await email.request(address)
    except HTTPException as unavailable:
        return _json(unavailable.status_code, str(unavailable.detail), _MESSAGE_UNAVAILABLE)
    except Exception as exc:
        logger.error("An unexpected error stopped a sign-in link request (%s)", type(exc).__name__)
        return _json(500, "server_error", _MESSAGE_ERROR)

    # The same answer for every outcome (see the module docstring). The mail, if
    # there is one, goes out once this has been sent.
    background = BackgroundTask(email.deliver, result.link) if result.link is not None else None
    response = JSONResponse(
        {"detail": "link_requested", "message": _request_accepted_message(email.settings)},
        status_code=202,
        headers=_PRIVATE_HEADERS,
        background=background,
    )
    return response


# --- GET /auth/email/confirm ----------------------------------------------------------


@router.get("/confirm")
async def confirm_page(
    request: Request, email: EmailLoginService = Depends(get_email_service)
) -> Response:
    # Read by hand, as in auth.py: a declared parameter would be answered with
    # FastAPI's validation error, which quotes the input back.
    challenge_id = request.query_params.get("c")
    if not email_login.is_challenge_id(challenge_id):
        return _page(email_pages.invalid_link_page())
    try:
        preview = await email.preview(challenge_id)
    except HTTPException:
        return _page(email_pages.unavailable_page())
    except Exception as exc:
        logger.error("An unexpected error stopped a sign-in page (%s)", type(exc).__name__)
        return _page(email_pages.error_page())
    if preview is None:
        return _page(email_pages.invalid_link_page())
    return _page(email_pages.confirm_page(challenge_id, preview.masked_address))


# --- POST /auth/email/confirm ---------------------------------------------------------


async def _confirmation(request: Request) -> Optional[tuple[str, str]]:
    """(challenge id, secret) from the form, None if it is not exactly those two."""
    if _media_type(request) != "application/x-www-form-urlencoded":
        return None
    body = await _read_limited(request, MAX_FORM_BODY)
    if body is None:
        return None
    try:
        fields = parse_qsl(
            body.decode("ascii"), keep_blank_values=True, strict_parsing=True, max_num_fields=2
        )
    except (UnicodeDecodeError, ValueError):
        return None
    names = [name for name, _ in fields]
    if sorted(names) != ["c", "token"]:
        return None
    values = dict(fields)
    return values["c"], values["token"]


@router.post("/confirm")
async def confirm(
    request: Request,
    auth: AuthService = Depends(get_auth_service),
    email: EmailLoginService = Depends(get_email_service),
) -> Response:
    if _origin_is_foreign(request, email.settings):
        return _page(email_pages.forbidden_page())
    form = await _confirmation(request)
    if form is None:
        return _page(email_pages.invalid_link_page())
    challenge_id, token = form

    try:
        current = await auth.authenticate(request.cookies.get(auth.settings.session_cookie_name))
        issued = await email.redeem(
            challenge_id, token, current.user_id if current is not None else None
        )
    except SignInConflict:
        logger.info("A sign-in link was refused: the browser is signed in as another user")
        return _page(email_pages.other_user_page())
    except LoginDenied:
        logger.info("A sign-in link was refused: its identity is not allowed")
        return _page(email_pages.invalid_link_page())
    except HTTPException:
        return _page(email_pages.unavailable_page())
    except Exception as exc:
        logger.error("An unexpected error stopped an email sign-in (%s)", type(exc).__name__)
        return _page(email_pages.error_page())
    if issued is None:
        return _page(email_pages.invalid_link_page())

    response = RedirectResponse(LANDING_PATH, status_code=303, headers=_PRIVATE_HEADERS)
    auth.set_cookie(
        response,
        auth.settings.session_cookie_name,
        issued.token,
        int(auth.settings.session_lifetime.total_seconds()),
    )
    return response
