"""Shared helpers for the web tests.

No socket is opened anywhere: requests go through httpx's ASGITransport straight
into the application, and the ASGI lifespan is driven by hand, with the same
messages a server sends. (starlette.testclient is not used: it warns that httpx
support is deprecated.)

Every database is a SQLite file under pytest's tmp_path, and no setting is read
from the environment or from a .env file.
"""

import asyncio
from contextlib import asynccontextmanager

import httpx

from house_helper.persistence.database import create_db_engine, upgrade_database
from house_helper.web.database import WebDatabase
from house_helper.web.settings import WebSettings

TIMEOUT = 10  # seconds; only ever reached when a test is already failing


class LifespanFailed(Exception):
    """The application answered a lifespan message with a failure."""


def sqlite_url(path) -> str:
    return f"sqlite:///{path}"


def settings_for(path) -> WebSettings:
    return WebSettings(database_url=sqlite_url(path))


@asynccontextmanager
async def running(app):
    """Start `app` through the ASGI lifespan protocol; shut it down on exit.

    The shutdown message is sent however the block ends, as a server does when it
    is stopped. Raises LifespanFailed if startup fails (shutdown is then not sent:
    the application has not started).
    """
    received: asyncio.Queue = asyncio.Queue()
    sent: asyncio.Queue = asyncio.Queue()
    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": {}}
    task = asyncio.ensure_future(app(scope, received.get, sent.put))
    try:
        await received.put({"type": "lifespan.startup"})
        reply = await asyncio.wait_for(sent.get(), TIMEOUT)
        if reply["type"] != "lifespan.startup.complete":
            raise LifespanFailed(reply)
        try:
            yield
        finally:
            await received.put({"type": "lifespan.shutdown"})
            reply = await asyncio.wait_for(sent.get(), TIMEOUT)
        if reply["type"] != "lifespan.shutdown.complete":
            raise LifespanFailed(reply)
    finally:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), TIMEOUT)


@asynccontextmanager
async def serving(app):
    """A started application and an httpx client that talks to it."""
    async with running(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://web.test") as client:
            yield client


@asynccontextmanager
async def opened_database(tmp_path, *, migrated: bool = True, **options):
    """A WebDatabase on a fresh file (with the real, migrated schema unless
    migrated=False). Yields (database, engine); closes the database on exit."""
    engine = create_db_engine(sqlite_url(tmp_path / "web.db"))
    if migrated:
        upgrade_database(engine)
    database = WebDatabase(engine, **options)
    try:
        yield database, engine
    finally:
        await database.aclose()
