"""Telegram sign-in and the server-side session: the HTTP surface.

Four routes (the email sign-in's are in email_auth.py, and share the service and the
session cookie below):

  GET  /auth/telegram/login     start: remember the login, send the browser to Telegram
  GET  /auth/telegram/callback  finish: verify, admit through the allowlist, open a session
  GET  /auth/me                 who the session cookie belongs to (the minimal identity)
  POST /auth/logout             end the session

and `require_user`, the dependency every route that serves a user's data must use.
The Telegram routes answer 503 unless Telegram login is configured; /auth/me,
/auth/logout and `require_user` work with either way of signing in, or both.
The user is whoever the session cookie resolves to on the server. A user id sent by
the client, in a query, a body or a header, is never read.

The flow, and where each protection sits:

  login     generates state, nonce and the PKCE verifier (telegram_oidc), stores the
            state's digest with the nonce and verifier for 10 minutes
            (persistence.authn), and sets a short-lived HttpOnly cookie holding the
            state. The cookie binds the login to the browser that began it: a
            callback link built by somebody else (login CSRF) arrives without it.
  callback  1. the state must be well formed and equal the cookie;
            2. the state is consumed in its own transaction, which commits before
               anything else is done: a second use of it, concurrent or later, finds
               it gone, so a code is never exchanged twice and no database lock is
               held while the provider is called;
            3. the code is exchanged and the ID token verified (telegram_oidc);
            4. in one transaction, the `sub` is matched on the allowlist, the
               identity link is created on the first login, and a session is
               created and tied to that identity link, so that revoking the
               identity ends it (persistence.authn.complete_login). Denied or
               failed, none of it is kept.
            The state stays consumed whatever happens after step 2; the user starts
            again. The answer is a redirect to "/auth/me", so the code and state do
            not stay in the address bar or the history, and a Set-Cookie.
  session   a random 256-bit token in a cookie; the database holds its digest. The
            cookie is HttpOnly, SameSite=Lax, path "/", with no Domain, and Secure
            (with the __Host- prefix) whenever the public URL is https. Lax, not
            Strict: the callback is a cross-site navigation from Telegram, and a
            Lax cookie is also what keeps cross-site POSTs (logout) from carrying
            it; logout additionally refuses a request whose Origin is not ours.

Every answer here says Cache-Control: no-store and Referrer-Policy: no-referrer.
Errors are a fixed code in {"detail": ...}, never an echo of the request, a reason
from the provider, or anything about why the allowlist said no beyond the status.
Nothing secret is logged: not the state, nonce, verifier, code, tokens, cookies or
the client secret, and a database error is logged by its class only (SQLAlchemy puts
the statement's parameters, which here are secrets, into its messages).
"""

import hmac
import logging
import re
from datetime import datetime
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from house_helper.persistence import authn
from house_helper.persistence.authn import AuthenticatedSession, IssuedSession
from house_helper.web.database import DatabaseClosed, WebDatabase
from house_helper.web.settings import AuthSettings, SessionSettings
from house_helper.web.telegram_oidc import (
    PROVIDER,
    InvalidIdToken,
    ProviderRejected,
    ProviderUnavailable,
    TelegramOidc,
    new_authorization,
)

logger = logging.getLogger(__name__)

# The answer to a successful sign-in. There is no frontend yet, so "/" would be a 404;
# /auth/me is a temporary destination that shows who is signed in. It moves to the
# application's pages when they exist. A path, never a value from the request.
LANDING_PATH = "/auth/me"

MAX_CODE_LENGTH = 2048
_CODE = re.compile(r"[\x21-\x7e]{1,%d}" % MAX_CODE_LENGTH)

_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class LoginFailure(Exception):
    """A sign-in that stopped: the HTTP status and the fixed code to answer with."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=code, headers=_PRIVATE_HEADERS)


class AuthService:
    """The sign-in logic, apart from HTTP. One per running application."""

    def __init__(
        self,
        settings: SessionSettings,
        database: WebDatabase,
        oidc: Optional[TelegramOidc],
        clock: Callable[[], datetime],
    ) -> None:
        """`settings` is the Telegram login's own (an AuthSettings) when `oidc` is
        given, and only the shared part otherwise (email login without Telegram)."""
        if oidc is not None and not isinstance(settings, AuthSettings):
            raise TypeError("Telegram login needs the Telegram settings")
        self.settings = settings
        self._database = database
        self._oidc = oidc
        self._clock = clock

    @property
    def telegram_enabled(self) -> bool:
        return self._oidc is not None

    def now(self) -> datetime:
        """The application's clock: the "now" of every expiry (tests move it)."""
        return self._clock()

    async def run(self, work):
        """database.run, with a database failure turned into a 503 whose log line
        names the class of the error and nothing else."""
        try:
            return await self._database.run(work)
        except DatabaseClosed:
            raise _error(503, "temporarily_unavailable") from None
        except SQLAlchemyError as exc:
            logger.error("A database error stopped an authentication request (%s)", type(exc).__name__)
            raise _error(503, "temporarily_unavailable") from None

    async def start_login(self):
        authorization = new_authorization(self.settings)
        now = self._clock()
        await self.run(
            lambda session: authn.begin_oidc_login(
                session,
                PROVIDER,
                authorization.state,
                authorization.nonce,
                authorization.code_verifier,
                now=now,
                lifetime=self.settings.login_lifetime,
            )
        )
        return authorization

    async def finish_login(
        self, state: str, code: Optional[str], *, provider_error: bool
    ) -> IssuedSession:
        """Raises LoginFailure (and HTTPException 503 for a database failure)."""
        now = self._clock()
        login = await self.run(
            lambda session: authn.consume_oidc_login(session, PROVIDER, state, now=now)
        )
        if login is None:
            logger.info("A sign-in callback carried an unknown, used or expired state")
            raise LoginFailure(400, "invalid_login_request")
        if provider_error:
            logger.info("Telegram sent the browser back with an error")
            raise LoginFailure(400, "invalid_login_request")
        if code is None or _CODE.fullmatch(code) is None:
            logger.info("A sign-in callback carried no usable code")
            raise LoginFailure(400, "invalid_login_request")

        try:
            subject = await self._oidc.redeem(code, login.code_verifier, login.nonce)
        except ProviderUnavailable:
            raise LoginFailure(502, "provider_unavailable") from None
        except ProviderRejected:
            raise LoginFailure(400, "login_failed") from None
        except InvalidIdToken as exc:
            logger.warning("The ID token was rejected: %s", exc.reason)
            raise LoginFailure(400, "login_failed") from None

        now = self._clock()
        try:
            issued = await self.run(
                lambda session: authn.complete_login(
                    session,
                    PROVIDER,
                    subject,
                    now=now,
                    lifetime=self.settings.session_lifetime,
                )
            )
        except authn.LoginDenied:
            logger.warning("A verified Telegram identity is not on the allowlist")
            raise LoginFailure(403, "not_allowed") from None
        logger.info("User %s signed in", issued.user_id)
        return issued

    async def authenticate(self, token: Optional[str]) -> Optional[AuthenticatedSession]:
        if token is None:
            return None
        now = self._clock()
        return await self.run(lambda session: authn.authenticate(session, token, now=now))

    async def logout(self, token: Optional[str]) -> None:
        if not authn.is_session_token(token):
            return
        now = self._clock()
        await self.run(lambda session: authn.revoke_session(session, token, now=now))

    # --- cookies ----------------------------------------------------------------------

    def set_cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path="/",
            secure=self.settings.secure_cookies,
            httponly=True,
            samesite="lax",
        )

    def clear_cookie(self, response: Response, name: str) -> None:
        response.delete_cookie(
            name,
            path="/",
            secure=self.settings.secure_cookies,
            httponly=True,
            samesite="lax",
        )


async def get_auth_service(request: Request) -> AuthService:
    """The running application's sign-in service; 503 when no way of signing in is
    configured (or the application is not running)."""
    service = getattr(request.app.state, "auth", None)
    if service is None:
        raise _error(503, "login_unavailable")
    return service


async def get_telegram_service(auth: AuthService = Depends(get_auth_service)) -> AuthService:
    """The same service, for the Telegram routes: 503 unless Telegram login is
    configured (email login may be, without it)."""
    if not auth.telegram_enabled:
        raise _error(503, "login_unavailable")
    return auth


async def require_user(
    request: Request, auth: AuthService = Depends(get_auth_service)
) -> AuthenticatedSession:
    """The session behind the request's cookie: the user, resolved on the server.
    401 for no cookie, a malformed, unknown, revoked or expired session, a disabled
    user, or one whose access has been withdrawn, all alike."""
    current = await auth.authenticate(request.cookies.get(auth.settings.session_cookie_name))
    if current is None:
        raise _error(401, "not_authenticated")
    return current


router = APIRouter(prefix="/auth")


def _failure(auth: AuthService, status_code: int, code: str) -> JSONResponse:
    """An answer to the sign-in endpoints that failed. The login cookie is spent in
    any case: it belongs to one attempt."""
    response = JSONResponse({"detail": code}, status_code=status_code, headers=_PRIVATE_HEADERS)
    auth.clear_cookie(response, auth.settings.login_cookie_name)
    return response


@router.get("/telegram/login")
async def telegram_login(auth: AuthService = Depends(get_telegram_service)) -> Response:
    authorization = await auth.start_login()
    response = RedirectResponse(authorization.url, status_code=303, headers=_PRIVATE_HEADERS)
    auth.set_cookie(
        response,
        auth.settings.login_cookie_name,
        authorization.state,
        int(auth.settings.login_lifetime.total_seconds()),
    )
    return response


@router.get("/telegram/callback")
async def telegram_callback(
    request: Request, auth: AuthService = Depends(get_telegram_service)
) -> Response:
    # Read by hand, not declared as parameters: a declared parameter that is missing
    # or too long would be answered by FastAPI's validation error, which quotes the
    # input back.
    query = request.query_params
    state = query.get("state")
    bound_state = request.cookies.get(auth.settings.login_cookie_name)
    if (
        state is None
        or not authn.is_state_value(state)
        or bound_state is None
        or not hmac.compare_digest(state.encode("utf-8"), bound_state.encode("utf-8"))
    ):
        logger.info("A sign-in callback did not match the login of this browser")
        return _failure(auth, 400, "invalid_login_request")
    try:
        issued = await auth.finish_login(
            state, query.get("code"), provider_error="error" in query
        )
    except LoginFailure as failure:
        return _failure(auth, failure.status_code, failure.code)
    except HTTPException as unavailable:
        return _failure(auth, unavailable.status_code, str(unavailable.detail))
    except Exception as exc:
        # Whatever else: a bug. Logged by class only (see the module docstring).
        logger.error("An unexpected error stopped a sign-in (%s)", type(exc).__name__)
        return _failure(auth, 500, "server_error")

    response = RedirectResponse(LANDING_PATH, status_code=303, headers=_PRIVATE_HEADERS)
    auth.set_cookie(
        response,
        auth.settings.session_cookie_name,
        issued.token,
        int(auth.settings.session_lifetime.total_seconds()),
    )
    auth.clear_cookie(response, auth.settings.login_cookie_name)
    return response


@router.get("/me")
async def me(
    response: Response, current: AuthenticatedSession = Depends(require_user)
) -> dict[str, str]:
    response.headers.update(_PRIVATE_HEADERS)
    return {
        "user_id": str(current.user_id),
        "session_expires_at": current.expires_at.isoformat(),
    }


@router.post("/logout")
async def logout(request: Request, auth: AuthService = Depends(get_auth_service)) -> Response:
    origin = request.headers.get("origin")
    if origin is not None and origin != auth.settings.origin:
        return JSONResponse(
            {"detail": "forbidden_origin"}, status_code=403, headers=_PRIVATE_HEADERS
        )
    await auth.logout(request.cookies.get(auth.settings.session_cookie_name))
    response = Response(status_code=204, headers=_PRIVATE_HEADERS)
    auth.clear_cookie(response, auth.settings.session_cookie_name)
    return response
