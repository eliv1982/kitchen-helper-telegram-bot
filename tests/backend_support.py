"""The integration-test database backend (HH-DB-01 CI-01, CI-14, CI-17).

One process-wide switch decides where the application integration fixtures put their
databases:

  * `HOUSE_HELPER_TEST_POSTGRES_URL` names a throwaway PostgreSQL 16 server: every
    fixture gets a FRESH database cloned from a migrated template, owned by the run's
    NOLOGIN owner, with the frozen ACL contract, and the application connects as the
    restricted application login (or the provisioning/purge/backup login, where the test
    is about that command). This is the default in CI and the only backend a configured
    run may use: a misconfigured URL fails, it never falls back to SQLite.
  * with no URL, a local run uses SQLite files under pytest's `tmp_path` (CI-13 keeps
    local PostgreSQL opt-in).

The rule is a pure function of the environment (`backend_name`), tested offline; this
module is the one place that turns it into databases. Nothing here reads
HOUSE_HELPER_DATABASE_URL.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from typing import Optional

import sqlalchemy as sa

from house_helper.persistence.base import Base
from house_helper.persistence.database import create_db_engine, upgrade_database
from tests import postgres_harness as harness
from tests.revisions import HEAD_REVISION

POSTGRES = "postgres"
SQLITE = "sqlite"
BACKEND_ENV = "HOUSE_HELPER_TEST_BACKEND"
CI_ENV = "CI"


class BackendConfigurationError(RuntimeError):
    """The run's database configuration cannot be honoured. The message is fixed text:
    it never contains the URL or a password."""


def backend_name(environ: Optional[dict[str, str]] = None) -> str:
    """`postgres` when the test server is configured, `sqlite` for an unconfigured local
    run. In CI an unconfigured run is an error: the PostgreSQL suites are required there
    (CI-05, CI-07)."""
    environ = os.environ if environ is None else environ
    url = (environ.get(harness.POSTGRES_URL_ENV) or "").strip()
    requested = (environ.get(BACKEND_ENV) or "").strip().lower()
    if requested not in ("", POSTGRES, SQLITE):
        raise BackendConfigurationError(
            f"{BACKEND_ENV} must be '{POSTGRES}' or '{SQLITE}' (its value is not shown)."
        )
    if url:
        return POSTGRES
    if requested == POSTGRES:
        raise BackendConfigurationError(
            f"{BACKEND_ENV} says {POSTGRES}, but {harness.POSTGRES_URL_ENV} is not set."
        )
    if requested == SQLITE:
        return SQLITE
    if (environ.get(CI_ENV) or "").strip().lower() in ("1", "true", "yes"):
        raise BackendConfigurationError(
            f"CI must configure the PostgreSQL test server: {harness.POSTGRES_URL_ENV} is not set."
        )
    return SQLITE


# A test that asserts SQLite itself (journal mode, PRAGMA, the write-first convention, the
# on-disk snapshot) is scoped to SQLite (CI-02); everything else runs on the configured
# backend. The PostgreSQL counterparts of the scoped suites are separate modules.
sqlite_only = pytest.mark.skipif(
    backend_name() != SQLITE,
    reason="SQLite-scoped (journal mode, PRAGMA, write-first, on-disk snapshot; CI-02)",
)


def check_configuration(environ: Optional[dict[str, str]] = None) -> str:
    """The backend for this run, validated before any test: an unsafe URL fails here with
    the harness's fixed message (no URL, no password)."""
    environ = os.environ if environ is None else environ
    name = backend_name(environ)
    if name == POSTGRES:
        try:
            harness.target_from_environment(environ)
        except harness.UnsafeTarget as refusal:
            raise BackendConfigurationError(
                f"{harness.POSTGRES_URL_ENV} refused [{refusal.code}]: {refusal.message}"
            ) from None
    return name


class Database:
    """One test database. `app_engine()` is the restricted application login (or the SQLite
    engine); `close()` removes the database. Every accessor returns a NEW engine that the
    caller disposes."""

    name: str

    @property
    def url_text(self) -> str:  # the application login's URL
        raise NotImplementedError

    def app_engine(self) -> sa.Engine:
        raise NotImplementedError

    def migrate_engine(self) -> sa.Engine:
        raise NotImplementedError

    def provision_engine(self) -> sa.Engine:
        raise NotImplementedError

    def purge_engine(self) -> sa.Engine:
        raise NotImplementedError

    def backup_engine(self) -> sa.Engine:
        raise NotImplementedError

    def url_text_for(self, role: str) -> str:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    closed: bool = False


class _SqliteDatabase(Database):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.name

    @property
    def url_text(self) -> str:
        return f"sqlite:///{self.path}"

    def url_text_for(self, role: str) -> str:
        return self.url_text  # one identity: the file

    def app_engine(self) -> sa.Engine:
        return create_db_engine(self.url_text)

    migrate_engine = app_engine
    provision_engine = app_engine
    purge_engine = app_engine
    backup_engine = app_engine

    def close(self) -> None:  # a tmp_path file: pytest removes it
        self.closed = True


class _PostgresDatabase(Database):
    def __init__(self, run: harness.Run, name: str) -> None:
        self.run = run
        self.name = name

    @property
    def url_text(self) -> str:
        return self.url_text_for("app")

    def url_text_for(self, role: str) -> str:
        return self.run.url(self.name, role).render_as_string(hide_password=False)

    def app_engine(self) -> sa.Engine:
        return self.run.engine(self.name, "app")

    def migrate_engine(self) -> sa.Engine:
        return self.run.engine(self.name, "migrate")

    def provision_engine(self) -> sa.Engine:
        return self.run.engine(self.name, "provision")

    def purge_engine(self) -> sa.Engine:
        return self.run.engine(self.name, "purge")

    def backup_engine(self) -> sa.Engine:
        return self.run.engine(self.name, "backup")

    def close(self) -> None:
        if self.closed:
            return
        # A session teardown may have closed the whole run first; only a database the run
        # still tracks is dropped here (the name is this run's own generated one).
        if self.name in self.run._databases:
            self.run.drop_database(self.name)
        self.closed = True


class Backend:
    """Creates databases for the integration fixtures."""

    name: str

    def new_database(
        self,
        *,
        schema: str = "migrations",
        tmp_path: Optional[Path] = None,
    ) -> Database:
        """`schema` is "migrations" (the Alembic head, built as the migration login),
        "models" (Base.metadata.create_all, DDL as the owner) or "empty"."""
        raise NotImplementedError

    def close(self) -> None:
        return None


class _SqliteBackend(Backend):
    name = SQLITE

    def new_database(self, *, schema: str = "migrations", tmp_path: Optional[Path] = None) -> Database:
        if tmp_path is None:
            raise ValueError("a SQLite database needs tmp_path")
        path = Path(tmp_path) / f"integration-{uuid.uuid4().hex}.db"
        engine = create_db_engine(f"sqlite:///{path}")
        try:
            if schema == "migrations":
                upgrade_database(engine, HEAD_REVISION)  # the revision is stated (CI-08)
            elif schema == "models":
                Base.metadata.create_all(engine)
            elif schema != "empty":
                raise ValueError("unknown schema")
        finally:
            engine.dispose()
        return _remember(_SqliteDatabase(path))


class _PostgresBackend(Backend):
    name = POSTGRES

    def __init__(self, run: harness.Run) -> None:
        self.run = run
        self._templates: dict[str, str] = {}

    def _template(self, schema: str) -> str:
        if schema not in self._templates:
            # R2-05: prepare_roles() cleans up a partial preparation itself; the session
            # finalizer (conftest -> close_backend -> Run.close) covers everything else.
            self.run.prepare_roles()
            name = self.run.create_database(runtime_roles=())
            engine = self.run.migrate_engine(name)
            try:
                if schema == "migrations":
                    upgrade_database(engine, HEAD_REVISION)  # the revision is stated (CI-08)
                elif schema == "models":
                    Base.metadata.create_all(engine)
                elif schema != "empty":
                    raise ValueError("unknown schema")
                if schema != "empty":
                    self.run.apply_privileges(engine)
            finally:
                engine.dispose()
            self._templates[schema] = name
        return self._templates[schema]

    def new_database(self, *, schema: str = "migrations", tmp_path: Optional[Path] = None) -> Database:
        name = self.run.create_database(template=self._template(schema))
        return _remember(_PostgresDatabase(self.run, name))

    def close(self) -> None:
        self.run.close()


_BACKEND: Optional[Backend] = None
_ACTIVE: list[Database] = []


def _remember(database: Database) -> Database:
    _ACTIVE.append(database)
    return database


def untrack(database: Database) -> Database:
    """Hand a database to a fixture whose own finalizer drops it (a module-scoped server:
    the per-test teardown must not remove a database the server is still using)."""
    try:
        _ACTIVE.remove(database)
    except ValueError:
        pass
    return database


def verify_database(engine: sa.Engine, database: Database) -> None:
    """Assert the EFFECTIVE dialect and database identity of `engine` (R2-02): the
    configured URL is not evidence on its own."""
    if isinstance(database, _PostgresDatabase):
        assert engine.dialect.name == "postgresql", engine.dialect.name
        with engine.connect() as connection:
            name = connection.execute(sa.text("SELECT current_database()")).scalar()
            connection.rollback()
        assert name == database.name, (name, database.name)
        return
    assert engine.dialect.name == "sqlite", engine.dialect.name
    assert Path(engine.url.database).resolve() == Path(database.path).resolve()


def cleanup_databases() -> None:
    """Drop the databases this test created (pytest teardown). A database a fixture already
    closed is skipped; a failure is reported once the others have been tried."""
    global _ACTIVE
    active, _ACTIVE = _ACTIVE, []
    problems = []
    for database in reversed(active):
        try:
            database.close()
        except Exception as problem:
            problems.append(problem)
    if problems:
        raise problems[0]


def backend() -> Backend:
    """The process-wide backend. It opens nothing until the first database is asked for."""
    global _BACKEND
    if _BACKEND is None:
        name = check_configuration()
        if name == POSTGRES:
            _BACKEND = _PostgresBackend(harness.run_from_environment())
        else:
            _BACKEND = _SqliteBackend()
    return _BACKEND


def close_backend() -> None:
    """Drop every database and role the run created (session teardown)."""
    global _BACKEND
    if _BACKEND is not None:
        _BACKEND.close()
        _BACKEND = None


def reset_backend_for_tests() -> None:
    """Forget the cached backend without touching a server (the offline rule tests)."""
    global _BACKEND
    _BACKEND = None
