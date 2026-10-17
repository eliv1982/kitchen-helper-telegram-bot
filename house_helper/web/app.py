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

Telegram login (auth.py) exists only when it is configured (see settings.py). The
routes are always there; without the configuration they answer 503, and the process
neither needs credentials nor opens an HTTP client. With it, the lifespan also
creates the client used to call Telegram and closes it on the way out.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Callable, Optional

import httpx
from fastapi import FastAPI

from house_helper.persistence.base import utcnow
from house_helper.persistence.database import create_db_engine
from house_helper.web import auth, health
from house_helper.web.auth import AuthService
from house_helper.web.database import WebDatabase
from house_helper.web.logsafety import install_access_log_redaction
from house_helper.web.settings import WebSettings, load_web_settings
from house_helper.web.telegram_oidc import TelegramOidc

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
    try:
        if settings.auth is not None:
            # Redirects are not followed: the endpoints are fixed, and an answer
            # that points elsewhere is a failure, not a place to go.
            http = httpx.AsyncClient(
                transport=app.state.provider_transport,
                timeout=PROVIDER_TIMEOUT,
                follow_redirects=False,
            )
            app.state.auth = AuthService(
                settings.auth,
                database,
                TelegramOidc(settings.auth, http, app.state.clock),
                app.state.clock,
            )
        # The backend name only: never the URL, which may hold a password.
        logger.info(
            "Web application started (database backend: %s, Telegram login: %s)",
            settings.database_backend,
            "on" if settings.auth is not None else "off",
        )
        yield
    finally:
        app.state.auth = None
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
) -> FastAPI:
    """A new application. `settings` defaults to the process environment.

    Raises WebConfigError if the configuration is unusable. The interactive API
    pages (/docs, /redoc, /openapi.json) are off: they are routes nobody asked
    for.

    `provider_transport` and `clock` are seams for tests: the transport stands in
    for the network to Telegram (None: the real one), the clock is the "now" of
    every expiry (sessions, logins, ID token claims).
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
    app.state.auth = None
    app.include_router(health.router)
    app.include_router(auth.router)
    return app
