"""The application factory and its lifespan.

create_app() builds the application and reads the configuration, but acquires
nothing: it opens no connection and creates no file, so it can be called freely
(an import, a test). The engine is created when the application starts (the
ASGI lifespan) and disposed when it stops.

The lifespan does not touch the schema. It never runs a migration and never
creates a table or a database file: the schema is changed only by Alembic
(`alembic upgrade head`, or house_helper.persistence.database.init_database in
the places that deliberately do so), which an operator or a deployment step runs.
Starting against a database that is missing or not migrated therefore succeeds,
and a database error shows when something first uses the database, not before.

The two ways to sign in exist only when configured, each on its own (see
settings.py): Telegram login (auth.py) and email login (email_auth.py). The routes
are always there; without the configuration they answer 503, and the process neither
needs credentials nor opens an HTTP client nor a connection to a mail server. With
Telegram configured, the lifespan also creates the client used to call Telegram and
closes it on the way out. With email configured it creates the sender (which opens a
connection to the mail server only when it has a message to send).

The chat API (chat_api.py) is always routed, and answers a message only when the
assistant is configured (OPENAI_API_KEY, see settings.py): the lifespan then builds
the OpenAI backend (which loads the SDK and opens a client on the first message, not
before) and the turn runner over it. Without a key the conversations can still be
created and read, and a message is answered 503. `chat_backend` replaces the backend
in tests. On the way out the runner is stopped first (it lets running turns finish,
then cancels what is left), then the backend's client is closed, then the database.

The page (ui.py) is always served too: `GET /` and the few files under /static. It is
public and static -- it holds nothing of anyone's, and every byte of user data it shows
comes from the /auth and /api routes it calls with the visitor's own cookie -- and it
draws only the ways of signing in that are configured (it reads which are running from
the services above, never from a request).
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Callable, Optional

import httpx
from fastapi import FastAPI

from house_helper.core.backend import ChatBackend
from house_helper.core.openai_chat import StandaloneOpenAIBackend
from house_helper.persistence.base import utcnow
from house_helper.persistence.database import create_db_engine
from house_helper.web import auth, chat_api, email_auth, health, ui
from house_helper.web.auth import AuthService
from house_helper.web.database import WebDatabase
from house_helper.web.email_auth import EmailLoginService
from house_helper.web.logsafety import install_access_log_redaction
from house_helper.web.mailer import EmailSender, SmtpEmailSender
from house_helper.web.settings import WebSettings, load_web_settings
from house_helper.web.telegram_oidc import TelegramOidc
from house_helper.web.turn_runner import WebTurnRunner

logger = logging.getLogger(__name__)

# Every request to the provider is bounded: it must not hold a login (and a
# worker's attention) for longer than a person would wait.
PROVIDER_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings: WebSettings = app.state.settings
    install_access_log_redaction()
    database = WebDatabase(create_db_engine(settings.database_url))
    app.state.database = database
    http: Optional[httpx.AsyncClient] = None
    backend: Optional[ChatBackend] = app.state.chat_backend
    if backend is None and settings.provider is not None:
        backend = StandaloneOpenAIBackend(settings.provider)
    try:
        oidc: Optional[TelegramOidc] = None
        if settings.auth is not None:
            # Redirects are not followed: the endpoints are fixed, and an answer
            # that points elsewhere is a failure, not a place to go.
            http = httpx.AsyncClient(
                transport=app.state.provider_transport,
                timeout=PROVIDER_TIMEOUT,
                follow_redirects=False,
            )
            oidc = TelegramOidc(settings.auth, http, app.state.clock)
        if settings.session is not None:
            # The session and its cookie belong to the application, not to a way of
            # signing in: the service exists when either is configured.
            service = AuthService(
                settings.auth or settings.session, database, oidc, app.state.clock
            )
            app.state.auth = service
            if settings.email is not None:
                sender = app.state.email_sender
                if sender is None:
                    sender = SmtpEmailSender(settings.email)
                app.state.email = EmailLoginService(settings.email, service, sender)
        if backend is not None:
            app.state.chat = WebTurnRunner(database, backend, app.state.clock)
        # The backend name only: never the URL, which may hold a password.
        logger.info(
            "Web application started (database backend: %s, Telegram login: %s, "
            "email login: %s, assistant: %s)",
            settings.database_backend,
            "on" if settings.auth is not None else "off",
            "on" if settings.email is not None else "off",
            "on" if backend is not None else "off",
        )
        yield
    finally:
        app.state.auth = None
        app.state.email = None
        try:
            runner, app.state.chat = app.state.chat, None
            if runner is not None:
                await runner.aclose()
            close_backend = getattr(backend, "aclose", None)
            if close_backend is not None and app.state.chat_backend is None:
                await close_backend()  # only a backend this lifespan built
        finally:
            try:
                if http is not None:
                    await http.aclose()
            finally:
                await database.aclose()
        logger.info("Web application stopped")


def create_app(
    settings: Optional[WebSettings] = None,
    *,
    provider_transport: Optional[httpx.AsyncBaseTransport] = None,
    clock: Callable[[], datetime] = utcnow,
    email_sender: Optional[EmailSender] = None,
    chat_backend: Optional[ChatBackend] = None,
) -> FastAPI:
    """A new application. `settings` defaults to the process environment.

    Raises WebConfigError if the configuration is unusable. The interactive API
    pages (/docs, /redoc, /openapi.json) are off: they are routes nobody asked
    for.

    `provider_transport`, `clock`, `email_sender` and `chat_backend` are seams for
    tests: the transport stands in for the network to Telegram (None: the real one),
    the clock is the "now" of every expiry (sessions, logins, links, ID token claims,
    chat leases and timestamps), the sender stands in for the mail server (None: SMTP,
    as configured), and the backend answers chat messages instead of OpenAI (None: the
    one the settings describe, or none at all).
    """
    app = FastAPI(
        title="Kitchen Helper",
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings if settings is not None else load_web_settings()
    app.state.provider_transport = provider_transport
    app.state.clock = clock
    app.state.email_sender = email_sender
    app.state.chat_backend = chat_backend
    app.state.auth = None
    app.state.email = None
    app.state.chat = None
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(email_auth.router)
    app.include_router(chat_api.router)
    app.include_router(ui.router)
    return app
