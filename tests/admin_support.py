"""Shared fixtures and helpers for the administrative-command tests.

Everything is a SQLite file under pytest's tmp_path, copied from one database that the
migrations built once per session: nothing leaves the machine and no setting is read
from the environment (the command takes its environment as an argument).

The database is read back with plain SQL on a fresh engine, so a test sees what is
committed and not what a session happens to have cached.
"""

import hashlib
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from house_helper.admin.cli import main
from house_helper.admin.target import open_target
from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    create_db_engine,
    create_session_factory,
    upgrade_database,
)
from tests import backend_support
from tests.backend_support import sqlite_only
from tests.revisions import HEAD_REVISION


TABLES = (
    "users",
    "allowlist_entries",
    "identity_links",
    "sessions",
    "email_login_challenges",
    "identity_invitations",
    "conversations",
    "messages",
)


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory):
    """A SQLite database at the head, built once; each test gets a copy. The SQLite-scoped
    admin tests (journal mode, PRAGMA, the write-first convention) use it explicitly."""
    path = tmp_path_factory.mktemp("admin-template") / "template.db"
    engine = create_db_engine(f"sqlite:///{path}")
    upgrade_database(engine, HEAD_REVISION)  # the revision is stated, not defaulted (CI-08)
    engine.dispose()
    assert not path.with_name(path.name + "-wal").exists()  # fully checkpointed: one file
    return path


@pytest.fixture
def db_path(tmp_path, migrated_template):
    path = tmp_path / "kitchen.db"
    shutil.copyfile(migrated_template, path)
    return path


@pytest.fixture
def sqlite_db_url(db_path):
    """The explicit SQLite file (the dialect-scoped admin tests)."""
    return f"sqlite:///{db_path}"


@pytest.fixture
def admin_database(database_backend, tmp_path):
    """One database on the configured backend (CI-14): PostgreSQL in CI, SQLite locally."""
    database = database_backend.new_database(schema="migrations", tmp_path=tmp_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def db_url(admin_database):
    """The restricted application login's URL (SQLite: the file)."""
    return admin_database.url_text


@pytest.fixture
def provision_db_url(admin_database):
    """The provisioning login's URL (SQLite: the same file, one identity)."""
    return admin_database.url_text_for("provision")


@pytest.fixture
def purge_db_url(admin_database):
    """The purge login's URL (SQLite: the same file, one identity)."""
    return admin_database.url_text_for("purge")


@dataclass(frozen=True)
class Run:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


def run_cli(capsys, url, *argv: str) -> Run:
    """The command, in this process, against `url` (None: the variable is not set)."""
    environ = {} if url is None else {DATABASE_URL_ENV: url}
    capsys.readouterr()
    code = main(["provision-email", *argv], environ=environ)
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def run_admin(capsys, url, *argv: str, clock=None) -> Run:
    """Any command, in this process, against `url` (None: the variable is not set), with
    the real clock unless `clock` is given."""
    environ = {} if url is None else {DATABASE_URL_ENV: url}
    capsys.readouterr()
    code = main(list(argv), environ=environ, clock=clock)
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def provision(capsys, url, email="Elena@example.com", *extra: str) -> Run:
    return run_cli(capsys, url, "--email", email, *extra)


def _neutral(value):
    """A value as the SQLite tests have always seen it, so one assertion serves both
    backends: UUIDs as 32 hex digits, timestamps as `stored()` text, booleans as 0/1."""
    if isinstance(value, uuid.UUID):
        return value.hex
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    if isinstance(value, bool):
        return int(value)
    return value


def rows(url, sql: str, **params) -> list:
    """`sql` on a fresh engine, as a list of tuples. (The engine is the persistence
    layer's, which switches the file to WAL: look at the journal mode BEFORE reading.)"""
    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            return [
                tuple(_neutral(value) for value in row)
                for row in connection.execute(sa.text(sql), params)
            ]
    finally:
        engine.dispose()


def counts(url) -> dict:
    return {table: rows(url, f"SELECT count(*) FROM {table}")[0][0] for table in TABLES}


def everything(url) -> dict:
    """All rows of the tables the command can touch, for 'nothing changed' checks."""
    return {
        table: sorted(map(repr, rows(url, f"SELECT * FROM {table}")))
        for table in TABLES
    }


def open_factory(url):
    engine = create_db_engine(url)
    return engine, create_session_factory(engine)


def open_admin_factory(url):
    """The engine the command itself works with (it leaves the journal mode alone)."""
    engine = open_target({DATABASE_URL_ENV: url})
    return engine, create_session_factory(engine)


# --- the SQLite journal mode, read and set with the plain sqlite3 module -----------------
# (never through create_db_engine, which switches every database it opens to WAL)


def journal_mode(path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()


def set_journal_mode(path, mode: str) -> None:
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(f"PRAGMA journal_mode={mode}").fetchone()[0] == mode.lower()
    finally:
        connection.close()


def snapshot(directory) -> dict:
    """Every file in `directory` (name -> SHA-256 of its bytes): equal before and after
    means nothing was written, created or left behind (-wal, -shm, -journal included)."""
    return {
        entry.name: hashlib.sha256(entry.read_bytes()).hexdigest()
        for entry in sorted(Path(directory).iterdir())
    }
