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
"""

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from house_helper.persistence.database import create_db_engine
from house_helper.web import health
from house_helper.web.database import WebDatabase
from house_helper.web.settings import WebSettings, load_web_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings: WebSettings = app.state.settings
    database = WebDatabase(create_db_engine(settings.database_url))
    app.state.database = database
    # The backend name only: never the URL, which may hold a password.
    logger.info("Web application started (database backend: %s)", settings.database_backend)
    try:
        yield
    finally:
        await database.aclose()
        logger.info("Web application stopped")


def create_app(settings: Optional[WebSettings] = None) -> FastAPI:
    """A new application. `settings` defaults to the process environment.

    Raises WebConfigError if the configuration is unusable. The interactive API
    pages (/docs, /redoc, /openapi.json) are off: they are routes nobody asked
    for, and the API they would describe does not exist yet.
    """
    app = FastAPI(
        title="Kitchen Helper",
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings if settings is not None else load_web_settings()
    app.include_router(health.router)
    return app
