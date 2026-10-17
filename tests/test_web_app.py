"""The application factory, the health endpoint, and the lifespan.

Everything runs in this process: requests go through httpx's ASGITransport and
the lifespan is driven message by message (see tests/web_support.py). The
environment the bot needs (BOT_TOKEN, OPENAI_API_KEY) is removed in the tests that
say so; the fresh-interpreter versions of those claims are in
tests/test_web_boundary.py.
"""

import logging

import pytest
import sqlalchemy as sa
from alembic import command
from fastapi import Depends

from house_helper.persistence import database as persistence_database
from house_helper.persistence.base import Base
from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    create_db_engine,
    create_session_factory,
    upgrade_database,
)
from house_helper.persistence.models import User
from house_helper.web import app as app_module
from house_helper.web.app import create_app
from house_helper.web.database import DatabaseClosed
from house_helper.web.dependencies import get_database
from house_helper.web.settings import WebConfigError, WebSettings
from tests.persistence_support import count
from tests.web_support import (
    LifespanFailed,
    running,
    serving,
    settings_for,
    sqlite_url,
)


@pytest.fixture
def without_bot_environment(monkeypatch):
    for name in ("BOT_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def engines(monkeypatch):
    """Every engine the application creates, and every disposal of an engine."""
    created, disposed = [], []
    real_create = app_module.create_db_engine
    real_dispose = sa.engine.Engine.dispose

    def recording_create(url):
        engine = real_create(url)
        created.append(engine)
        return engine

    def recording_dispose(self, *args, **kwargs):
        disposed.append(self)
        return real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(app_module, "create_db_engine", recording_create)
    monkeypatch.setattr(sa.engine.Engine, "dispose", recording_dispose)
    return created, disposed


# --- creating the application ---------------------------------------------------------


def test_creating_the_application_needs_no_bot_or_provider_environment(
    without_bot_environment, monkeypatch, tmp_path
):
    monkeypatch.setenv(DATABASE_URL_ENV, sqlite_url(tmp_path / "env.db"))

    app = create_app()

    assert app.state.settings.database_url == sqlite_url(tmp_path / "env.db")


def test_creating_the_application_reads_the_environment_when_it_is_called_not_before(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(DATABASE_URL_ENV, sqlite_url(tmp_path / "first.db"))
    first = create_app()
    monkeypatch.setenv(DATABASE_URL_ENV, sqlite_url(tmp_path / "second.db"))
    second = create_app()

    assert first.state.settings.database_url.endswith("first.db")
    assert second.state.settings.database_url.endswith("second.db")


def test_unusable_configuration_stops_the_application_from_being_created(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV, "sqlite:///:memory:")

    with pytest.raises(WebConfigError):
        create_app()


def test_each_application_is_independent(tmp_path):
    first = create_app(settings_for(tmp_path / "a.db"))
    second = create_app(settings_for(tmp_path / "b.db"))

    assert first is not second
    assert first.state is not second.state
    assert first.router is not second.router


def test_creating_the_application_touches_neither_the_database_nor_the_disk(tmp_path, engines):
    created, _ = engines
    target = tmp_path / "not" / "yet" / "there.db"

    app = create_app(settings_for(target))

    assert created == []
    assert not target.parent.exists()
    assert not hasattr(app.state, "database")


def test_the_application_exposes_the_health_endpoint_and_nothing_else(tmp_path):
    # No user-data route may exist before authentication and ownership checks do.
    # Not even the interactive API pages FastAPI would add by default.
    app = create_app(settings_for(tmp_path / "routes.db"))

    # openapi() is the public listing (app.routes holds FastAPI internals); the
    # schema page itself is off, which test_nothing_else_is_served checks.
    assert set(app.openapi()["paths"]) == {"/healthz"}


# --- /healthz -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz_answers_200_with_a_small_fixed_body(
    without_bot_environment, tmp_path
):
    app = create_app(settings_for(tmp_path / "health.db"))

    async with serving(app) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_healthz_needs_no_authentication_and_sets_nothing(tmp_path):
    app = create_app(settings_for(tmp_path / "health.db"))

    async with serving(app) as client:
        response = await client.get("/healthz")  # no cookie, no Authorization

    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert "www-authenticate" not in response.headers


@pytest.mark.asyncio
async def test_healthz_is_liveness_not_readiness_it_never_touches_the_database(tmp_path):
    # A database that could not possibly be opened: the directory is a file.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    app = create_app(settings_for(blocker / "unreachable.db"))

    async with serving(app) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert blocker.read_text() == "not a directory"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_healthz_only_answers_get(tmp_path, method):
    app = create_app(settings_for(tmp_path / "health.db"))

    async with serving(app) as client:
        response = await getattr(client, method)("/healthz")

    assert response.status_code == 405


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json", "/api", "/healthz/x"])
async def test_nothing_else_is_served(tmp_path, path):
    app = create_app(settings_for(tmp_path / "health.db"))

    async with serving(app) as client:
        response = await client.get(path)

    assert response.status_code == 404


# --- the lifespan -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_creates_the_engine_and_shutdown_disposes_it(tmp_path, engines):
    created, disposed = engines
    app = create_app(settings_for(tmp_path / "life.db"))
    assert created == []

    async with running(app):
        assert len(created) == 1
        assert disposed == []
        assert created[0].url.database == str(tmp_path / "life.db")
        assert app.state.database is not None
        assert not app.state.database.closed

    assert disposed == created
    assert app.state.database.closed
    assert created[0].pool.checkedout() == 0


@pytest.mark.asyncio
async def test_shutdown_disposes_the_engine_even_when_the_run_ended_in_an_error(
    tmp_path, engines
):
    created, disposed = engines
    app = create_app(settings_for(tmp_path / "life.db"))

    with pytest.raises(RuntimeError, match="the server fell over"):
        async with running(app):
            raise RuntimeError("the server fell over")

    assert disposed == created != []


@pytest.mark.asyncio
async def test_a_failed_startup_leaves_nothing_to_clean_up(tmp_path, monkeypatch, engines):
    created, disposed = engines

    def refuse(url):
        raise ModuleNotFoundError("No module named 'psycopg2'")

    monkeypatch.setattr(app_module, "create_db_engine", refuse)
    app = create_app(settings_for(tmp_path / "life.db"))

    with pytest.raises(LifespanFailed, match="psycopg2"):
        async with running(app):
            pytest.fail("the application must not start")

    assert not hasattr(app.state, "database")
    assert created == [] and disposed == []


@pytest.mark.asyncio
async def test_the_application_can_be_started_again_after_it_stopped(tmp_path, engines):
    created, disposed = engines
    app = create_app(settings_for(tmp_path / "again.db"))

    async with running(app):
        first = app.state.database
    async with running(app):
        second = app.state.database
        assert not second.closed

    assert first is not second and first.closed and second.closed
    assert len(created) == 2 and disposed == created


@pytest.mark.asyncio
async def test_the_database_dependency_serves_the_running_application_and_refuses_the_stopped_one(
    tmp_path,
):
    app = create_app(settings_for(tmp_path / "dependency.db"))
    seen = []

    @app.get("/test-only/database")
    async def probe(database=Depends(get_database)):
        seen.append(database)
        return {"closed": database.closed}

    async with serving(app) as client:
        assert (await client.get("/test-only/database")).json() == {"closed": False}
        assert seen[0] is app.state.database

    stopped = create_app(settings_for(tmp_path / "never_started.db"))

    class FakeRequest:
        app = stopped

    with pytest.raises(DatabaseClosed):
        await get_database(FakeRequest())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_handler_uses_the_database_through_run_and_its_work_is_committed(tmp_path):
    url = sqlite_url(tmp_path / "handler.db")
    engine = create_db_engine(url)
    upgrade_database(engine)  # the operator's job, never the application's
    app = create_app(WebSettings(database_url=url))

    @app.post("/test-only/users")
    async def create_user(database=Depends(get_database)):
        def work(session):
            session.add(User(display_name="Елена"))

        await database.run(work)
        return {"created": True}

    try:
        async with serving(app) as client:
            assert (await client.post("/test-only/users")).json() == {"created": True}

        with create_session_factory(engine)() as witness:
            assert count(witness, User) == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_the_lifespan_logs_the_backend_and_never_the_url(tmp_path, monkeypatch, caplog):
    secret = "hunter2-do-not-log"
    # A PostgreSQL URL is valid settings; the engine behind it is a local SQLite
    # file only because no PostgreSQL driver is installed here and nothing connects.
    monkeypatch.setattr(
        app_module, "create_db_engine", lambda url: create_db_engine(sqlite_url(tmp_path / "log.db"))
    )
    app = create_app(
        WebSettings(database_url=f"postgresql://kitchen:{secret}@db.internal:5432/kitchen")
    )

    with caplog.at_level(logging.DEBUG):
        async with running(app):
            pass

    assert "postgresql" in caplog.text
    for forbidden in (secret, "db.internal", "kitchen:"):
        assert forbidden not in caplog.text


# --- starting changes no schema and runs no migration --------------------------------------------


def _snapshot(path):
    """What is in a SQLite database, independent of how the file is laid out."""
    with sa.create_engine(f"sqlite:///{path}").connect() as connection:
        objects = connection.exec_driver_sql(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        versions = (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall()
            if any(name == "alembic_version" for _, name, _ in objects)
            else None
        )
    return objects, versions


@pytest.mark.asyncio
async def test_starting_and_stopping_creates_no_database_file(tmp_path):
    target = tmp_path / "fresh.db"
    app = create_app(settings_for(target))

    async with serving(app) as client:
        await client.get("/healthz")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_starting_and_stopping_leaves_an_empty_database_file_empty(tmp_path):
    target = tmp_path / "empty.db"
    target.write_bytes(b"")
    app = create_app(settings_for(target))

    async with serving(app) as client:
        await client.get("/healthz")

    assert target.read_bytes() == b""  # not even the header SQLite writes on first use
    assert sorted(path.name for path in tmp_path.iterdir()) == ["empty.db"]


@pytest.mark.asyncio
async def test_starting_and_stopping_leaves_a_migrated_database_as_it_was(tmp_path):
    target = tmp_path / "migrated.db"
    engine = create_db_engine(sqlite_url(target))
    upgrade_database(engine)
    engine.dispose()
    before = _snapshot(target)
    assert before[1] == [("0001_persistence_foundation",)]

    async with serving(create_app(settings_for(target))) as client:
        await client.get("/healthz")

    assert _snapshot(target) == before


@pytest.mark.asyncio
async def test_a_database_from_another_revision_is_not_touched_or_refused(tmp_path):
    # init_database would refuse this (see test_persistence_database.py). The web
    # application neither migrates nor checks: it starts, and leaves the file alone.
    target = tmp_path / "newer.db"
    engine = create_db_engine(sqlite_url(target))
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('from_the_future')")
    engine.dispose()
    before = _snapshot(target)

    async with serving(create_app(settings_for(target))) as client:
        assert (await client.get("/healthz")).status_code == 200

    assert _snapshot(target) == before


@pytest.mark.asyncio
async def test_no_migration_or_schema_creation_is_ever_called(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the web application must not change the schema")

    for owner, name in [
        (command, "upgrade"),
        (command, "downgrade"),
        (command, "stamp"),
        (persistence_database, "upgrade_database"),
        (persistence_database, "downgrade_database"),
        (persistence_database, "init_database"),
        (sa.MetaData, "create_all"),
        (sa.MetaData, "drop_all"),
        (Base.metadata, "create_all"),
    ]:
        monkeypatch.setattr(owner, name, forbidden)
    app = create_app(settings_for(tmp_path / "guarded.db"))

    async with serving(app) as client:
        assert (await client.get("/healthz")).status_code == 200
        with pytest.raises(sa.exc.OperationalError, match="no such table"):
            await app.state.database.run(lambda session: count(session, User))
