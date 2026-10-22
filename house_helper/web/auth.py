"""Telegram sign-in and the server-side session: the HTTP surface.

Four routes (the email sign-in's are in email_auth.py, and share the service and the
session cookie below):

  GET  /auth/telegram/login     start: remember the login, send the browser to Telegram
  GET  /auth/telegram/callback  finish: verify, admit through the allowlist, open a session
  GET  /auth/me                 who the session cookie belongs to (the minimal identity)
  POST /auth/logout             end the session, if it is the expected user's

and `require_user`, the dependency every route that serves a user's data must use.
The Telegram routes answer 503 unless Telegram login is configured; /auth/me,
/auth/logout and `require_user` work with either way of signing in, or both.
The user is whoever the session cookie resolves to on the server. A user id sent by
the client, in a query, a body or a header, is never read, with one exception: logout
reads the X-Expected-User header (below), to compare it with the session's owner and
for nothing else.

Logout and a lost answer. A page that asked to sign out and never heard back asks
again, and by then the browser's one session cookie may be somebody else's (another
tab signed in as another person). So the request says whose session it means to end:
X-Expected-User, the user_id /auth/me gave the page, sent with the first request and
with every repeat. The session is the one in the cookie of that very request, and it is
ended only if it is that user's: the user is part of the UPDATE's own condition
(persistence.authn.revoke_session_of), so a session of anybody else is not touched by
it. If the cookie names a session of someone else the answer is 409 session_mismatch,
nothing is revoked and no cookie is set or cleared. The header is a comparison and not
a credential: it never selects, authenticates or authorizes anything, and it is not
trusted for who the user is. A header that is missing, repeated or not a UUID is 400
invalid_request, again with nothing revoked or cleared; a database that cannot say
is 503, the same. A cookie that names no session, or one that was already ended (the
repeat after a lost answer), is 204 as before, since there is nothing left to end, and
the answer clears the cookie. Neither answer says who anybody is.

A browser signed in as somebody else. The browser has one session cookie, and the answer to a
login replaces it. So the callback of ANY Telegram login (ordinary or by invitation) reads the
session cookie the request carried, and if it names a live session of a user other than the one
the verified identity belongs to, the answer is 409 signed_in_as_another_user (a page in
Russian), with the existing session and cookie untouched, no session issued for the other
user, and nothing else changed (the login's state is spent, as for every login that ends).
The comparison is made inside the transaction that would issue the session
(persistence.authn.complete_login, `browser_session_token`), so there is no gap between the
last look at who the browser is and the issuing; a session that has ended (expired, revoked,
its identity withdrawn, its user disabled) is no obstacle, and neither is the same user
signing in again (a new session, the old one kept). What the cookie says is read when the
request arrives: a session the browser acquired after the callback request was sent is not
seen (the server cannot see a cookie that was not sent).

Linking a Telegram identity (a login begun with an invitation). The same two routes serve
it; what differs is where the login starts and what the callback does at the end.

  start     POST /auth/invite/start (invite_auth.py) verifies the invitation's secret, which
            arrives in a POST body and nowhere else, and only then stores the login's state
            with the invitation's id (persistence.invitations.begin_invitation_login), so
            the id the callback claims is the one verified for this request. The cookie
            that binds a login to its browser is set exactly as for any login.
  callback  everything below is unchanged (the cookie check, the state consumed first, the
            code exchanged, the ID token verified). The invitation is whatever the consumed
            state row says (persistence.authn.consume_oidc_login) and nothing the request
            says. One transaction then claims it for the verified `sub`, opens the session
            and checks that it is the invited user's (persistence.invitations.
            complete_invitation_login), so any failure leaves the invitation valid and
            nothing linked. A browser signed in as a DIFFERENT user is refused (409) with
            the invitation untouched, as the email link does it. The answer is a redirect
            to a page of its own saying it worked (INVITATION_DONE_PATH), and a failure of
            a login that began with an invitation is a Russian page, not a JSON code (the
            ordinary login's answers are as they were, but for the refusal below).
  next time the identity is on the allowlist and linked, so signing in with Telegram needs
            no invitation: it is the ordinary login, on this device or another.

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
            again. The answer is a redirect to the application's page ("/"), so the
            code and state do not stay in the address bar or the history, and a
            Set-Cookie.
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
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from house_helper.persistence import authn, invitations
from house_helper.persistence.authn import AuthenticatedSession, IssuedSession
from house_helper.web import invite_pages
from house_helper.web.database import DatabaseClosed, WebDatabase
from house_helper.web.settings import AuthSettings, SessionSettings
from house_helper.web.telegram_oidc import (
    PROVIDER,
    InvalidIdToken,
    ProviderRejected,
    ProviderUnavailable,
    AuthorizationRequest,
    TelegramOidc,
    new_authorization,
)

logger = logging.getLogger(__name__)

# The answer to a successful sign-in: the application's page (ui.py), which asks
# /auth/me who is signed in and shows the conversations. (Before the page existed this
# was /auth/me itself.) A path, never a value from the request.
LANDING_PATH = "/"

# The answer to a successful sign-in that linked a Telegram identity by invitation: a page
# that says so (invite_auth.py), from which the person opens the application.
INVITATION_DONE_PATH = "/auth/invite/done"

MAX_CODE_LENGTH = 2048
_CODE = re.compile(r"[\x21-\x7e]{1,%d}" % MAX_CODE_LENGTH)

# Whose session a logout means to end (see the module docstring): a UUID as /auth/me writes it.
EXPECTED_USER_HEADER = "X-Expected-User"
_USER_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")

_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class LoginFailure(Exception):
    """A sign-in that stopped: the HTTP status and the fixed code to answer with.
    `invitation` is True if the login had begun with a Telegram invitation, which is
    answered with a page in Russian instead of the JSON code."""

    def __init__(self, status_code: int, code: str, *, invitation: bool = False) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.invitation = invitation


@dataclass(frozen=True)
class FinishedLogin:
    """A sign-in that succeeded: the session, and whether it linked a Telegram identity
    by invitation (the answer then goes to INVITATION_DONE_PATH)."""

    issued: IssuedSession
    invitation: bool = False


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

    async def start_invitation_login(
        self, invitation_id: str, token: str, signed_in_user: Optional[UUID]
    ) -> Optional[AuthorizationRequest]:
        """The start of a login that links a Telegram identity: the login's state is stored
        bound to the invitation, in one transaction that first verifies the secret. None
        (nothing stored) if it is not a live invitation's, whatever the reason. Raises
        invitations.SignedInAsAnotherUser (see there) and HTTPException 503."""
        authorization = new_authorization(self.settings)
        now = self._clock()
        stored = await self.run(
            lambda session: invitations.begin_invitation_login(
                session,
                invitation_id,
                token,
                authorization.state,
                authorization.nonce,
                authorization.code_verifier,
                now=now,
                lifetime=self.settings.login_lifetime,
                signed_in_user=signed_in_user,
            )
        )
        if not stored:
            logger.info("A Telegram invitation was not valid")
            return None
        logger.info("A Telegram invitation sign-in was started")
        return authorization

    async def began_with_invitation(self, state: str) -> bool:
        """Whether `state` is that of a login begun with an invitation, for wording a
        failure of the callback to a person who did begin with one; False when it cannot
        be told (it never fails: a failure is worded as an ordinary login's)."""
        try:
            return await self._database.run(
                lambda session: authn.is_invitation_login(session, PROVIDER, state)
            )
        except (DatabaseClosed, SQLAlchemyError):
            return False

    async def finish_login(
        self,
        state: str,
        code: Optional[str],
        *,
        provider_error: bool,
        session_token: Optional[str] = None,
    ) -> FinishedLogin:
        """Raises LoginFailure (and HTTPException 503 for a database failure before the
        state is known). `session_token` is the browser's session cookie, read for every
        login and for nothing but one comparison: a browser signed in as another user than
        the one signing in is refused (409), whatever way the login began."""
        now = self._clock()
        login = await self.run(
            lambda session: authn.consume_oidc_login(session, PROVIDER, state, now=now)
        )
        if login is None:
            logger.info("A sign-in callback carried an unknown, used or expired state")
            raise LoginFailure(
                400, "invalid_login_request", invitation=await self.began_with_invitation(state)
            )
        # Which invitation, if any, comes from the consumed state row and from nowhere else.
        invitation = login.invitation_id is not None
        try:
            return await self._finish(login, code, provider_error, session_token)
        except LoginFailure:
            raise
        except HTTPException as unavailable:
            # (From self.run, which has logged the class of the database error.) The same
            # answer as the route would give it, but worded for the invitation if it is one.
            raise LoginFailure(
                unavailable.status_code, str(unavailable.detail), invitation=invitation
            ) from None
        except Exception as exc:
            # Whatever else: a bug. Logged by class only (see the module docstring).
            logger.error("An unexpected error stopped a sign-in (%s)", type(exc).__name__)
            raise LoginFailure(500, "server_error", invitation=invitation) from None

    async def _finish(
        self,
        login: authn.OidcLoginSecrets,
        code: Optional[str],
        provider_error: bool,
        session_token: Optional[str],
    ) -> FinishedLogin:
        invitation = login.invitation_id is not None
        if provider_error:
            logger.info("Telegram sent the browser back with an error")
            # (A cancelled or refused sign-in at Telegram: the person is told so in words
            # for the invitation page; an ordinary login's code is as it was.)
            raise LoginFailure(
                400, "login_failed" if invitation else "invalid_login_request", invitation=invitation
            )
        if code is None or _CODE.fullmatch(code) is None:
            logger.info("A sign-in callback carried no usable code")
            raise LoginFailure(400, "invalid_login_request", invitation=invitation)

        try:
            subject = await self._oidc.redeem(code, login.code_verifier, login.nonce)
        except ProviderUnavailable:
            raise LoginFailure(502, "provider_unavailable", invitation=invitation) from None
        except ProviderRejected:
            raise LoginFailure(400, "login_failed", invitation=invitation) from None
        except InvalidIdToken as exc:
            logger.warning("The ID token was rejected: %s", exc.reason)
            raise LoginFailure(400, "login_failed", invitation=invitation) from None

        if invitation:
            return await self._link_by_invitation(login.invitation_id, subject, session_token)

        now = self._clock()
        try:
            issued = await self.run(
                lambda session: authn.complete_login(
                    session,
                    PROVIDER,
                    subject,
                    now=now,
                    lifetime=self.settings.session_lifetime,
                    browser_session_token=session_token,
                )
            )
        except authn.LoginDenied:
            logger.warning("A verified Telegram identity is not on the allowlist")
            raise LoginFailure(403, "not_allowed") from None
        except authn.SignedInAsAnotherUser:
            logger.info("A sign-in was refused: the browser is signed in as another user")
            raise LoginFailure(409, "signed_in_as_another_user") from None
        logger.info("User %s signed in", issued.user_id)
        return FinishedLogin(issued)

    async def _link_by_invitation(
        self, invitation_id: UUID, subject: str, session_token: Optional[str]
    ) -> FinishedLogin:
        """The end of a login that began with an invitation: claim it for the verified
        `sub`, open the session, check whose it is, all in one transaction (see
        persistence.invitations.complete_invitation_login)."""
        current = await self.authenticate(session_token)
        signed_in_user = None if current is None else current.user_id
        now = self._clock()
        try:
            issued = await self.run(
                lambda session: invitations.complete_invitation_login(
                    session,
                    str(invitation_id),
                    subject,
                    now=now,
                    lifetime=self.settings.session_lifetime,
                    signed_in_user=signed_in_user,
                )
            )
        except invitations.InvitationNotUsable:
            logger.info("A Telegram invitation could not be used: it is not valid any more")
            raise LoginFailure(400, "invitation_not_usable", invitation=True) from None
        except invitations.IdentityRefused:
            logger.warning("A verified Telegram identity could not be linked by an invitation")
            raise LoginFailure(409, "identity_not_linkable", invitation=True) from None
        except invitations.SignedInAsAnotherUser:
            logger.info("An invitation sign-in was refused: the browser is signed in as another user")
            raise LoginFailure(409, "signed_in_as_another_user", invitation=True) from None
        except authn.LoginDenied:
            logger.error("An invitation sign-in was refused: the session could not be issued")
            raise LoginFailure(400, "login_failed", invitation=True) from None
        logger.info("User %s linked Telegram by invitation and signed in", issued.user_id)
        return FinishedLogin(issued, invitation=True)

    async def authenticate(self, token: Optional[str]) -> Optional[AuthenticatedSession]:
        if token is None:
            return None
        now = self._clock()
        return await self.run(lambda session: authn.authenticate(session, token, now=now))

    async def logout(self, token: Optional[str], expected_user: UUID) -> None:
        """End the session behind `token` if it is `expected_user`'s. Raises
        authn.SessionOfAnotherUser (nothing changed) if it is somebody else's, and
        HTTPException 503 for a database failure; never ends a session it could not
        check the owner of."""
        if not authn.is_session_token(token):
            return
        now = self._clock()
        await self.run(
            lambda session: authn.revoke_session_of(session, token, expected_user, now=now)
        )

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


def _failure(
    auth: AuthService, status_code: int, code: str, *, invitation: bool = False
) -> Response:
    """An answer to the sign-in endpoints that failed: the JSON code, or a page in Russian
    for the person (invite_pages) when the login began with an invitation, and for the
    refusal of a browser that is signed in as another user (the JSON codes of the other
    refusals of an ordinary login are as they were). The login cookie is spent in any
    case: it belongs to one attempt. The session cookie is never touched here."""
    page = None
    if invitation:
        page = invite_pages.failure_page(code)
    elif code == "signed_in_as_another_user":
        page = invite_pages.login_other_user_page()
    if page is not None:
        response: Response = Response(
            page.html, status_code=page.status_code, media_type="text/html", headers=page.headers
        )
    else:
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
        # The state is not trusted for anything here: it is only looked up to word the
        # failure for a person who began with an invitation (see began_with_invitation).
        invitation = (
            state is not None
            and authn.is_state_value(state)
            and await auth.began_with_invitation(state)
        )
        return _failure(auth, 400, "invalid_login_request", invitation=invitation)
    try:
        finished = await auth.finish_login(
            state,
            query.get("code"),
            provider_error="error" in query,
            session_token=request.cookies.get(auth.settings.session_cookie_name),
        )
    except LoginFailure as failure:
        return _failure(auth, failure.status_code, failure.code, invitation=failure.invitation)
    except HTTPException as unavailable:
        return _failure(auth, unavailable.status_code, str(unavailable.detail))
    except Exception as exc:
        # Whatever else: a bug. Logged by class only (see the module docstring).
        logger.error("An unexpected error stopped a sign-in (%s)", type(exc).__name__)
        return _failure(auth, 500, "server_error")

    target = INVITATION_DONE_PATH if finished.invitation else LANDING_PATH
    response = RedirectResponse(target, status_code=303, headers=_PRIVATE_HEADERS)
    auth.set_cookie(
        response,
        auth.settings.session_cookie_name,
        finished.issued.token,
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


def _expected_user(request: Request) -> Optional[UUID]:
    """The user the page means to sign out: exactly one X-Expected-User header holding a
    UUID in its usual written form; None for anything else. (Read by hand, not declared:
    a declared header that is missing would be answered by FastAPI's validation error,
    which quotes the input back.)"""
    values = request.headers.getlist(EXPECTED_USER_HEADER)
    if len(values) != 1 or _USER_ID.fullmatch(values[0]) is None:
        return None
    return UUID(values[0])


@router.post("/logout")
async def logout(request: Request, auth: AuthService = Depends(get_auth_service)) -> Response:
    origin = request.headers.get("origin")
    if origin is not None and origin != auth.settings.origin:
        return JSONResponse(
            {"detail": "forbidden_origin"}, status_code=403, headers=_PRIVATE_HEADERS
        )
    expected = _expected_user(request)
    if expected is None:
        return JSONResponse(
            {"detail": "invalid_request"}, status_code=400, headers=_PRIVATE_HEADERS
        )
    try:
        await auth.logout(request.cookies.get(auth.settings.session_cookie_name), expected)
    except authn.SessionOfAnotherUser:
        logger.info("A sign-out was refused: the session in the cookie is not the expected user's")
        return JSONResponse(
            {"detail": "session_mismatch"}, status_code=409, headers=_PRIVATE_HEADERS
        )
    response = Response(status_code=204, headers=_PRIVATE_HEADERS)
    auth.clear_cookie(response, auth.settings.session_cookie_name)
    return response
