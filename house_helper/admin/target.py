"""Choosing the database an administrative command changes, and refusing a wrong one.

An administrative command writes to whatever database it is pointed at, so it is
stricter than the applications about WHICH database that is:

  * the URL must come from HOUSE_HELPER_DATABASE_URL, set on purpose. The development
    default of persistence.database (a SQLite file in the working directory) is never
    used here: a forgotten variable must not turn into "provisioned the wrong file";
  * the database must already exist. A SQLite file that is missing (or empty) is
    refused, not created: sqlite3 would happily make a new empty one. On PostgreSQL
    the connection itself tells (the server refuses a database that does not exist);
  * its Alembic revision must be exactly the repository's single head. Nothing is
    ever migrated, stamped or created here (reading the version is read-only). The
    check is a look, not a lock: it cannot stop a migration that runs at the same
    time, which is why migrations and this command must never run together (see
    provisioning.py and the README);
  * looking at a database, and refusing it, changes nothing about the file: not even
    the SQLite journal mode (see _leave_journal_mode_alone);
  * the URL, which holds a password on PostgreSQL, is never printed, and neither is
    the text of a driver error (it can name hosts, users and parameters). Every
    message below is fixed text, plus the two revision identifiers when the schema
    is not at the head (checked to be plain identifiers first).

TargetError carries a stable `code`; the command line turns it into an exit status.
"""

import re
from pathlib import Path
from typing import Mapping, Optional

import sqlalchemy as sa
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine, make_url

from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    _configure_sqlite_connection,
    alembic_config,
    create_db_engine,
)

SUPPORTED_BACKENDS = ("sqlite", "postgresql")

_REVISION_ID = re.compile(r"[A-Za-z0-9_]{1,64}")


class TargetError(Exception):
    """The database cannot be used by an administrative command. `code` is stable
    (tests and scripts may rely on it); the message is safe to print."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def open_target(environ: Mapping[str, str]) -> Engine:
    """An engine for the configured database, once it is known to exist and to be at
    the repository's head. TargetError otherwise; nothing has been written either way."""
    url_text = (environ.get(DATABASE_URL_ENV) or "").strip()
    if not url_text:
        raise TargetError(
            "not_configured",
            f"{DATABASE_URL_ENV} is not set. This command never falls back to the "
            "development default: set the variable to the database you mean to change.",
        )
    try:
        url = make_url(url_text)
    except Exception:  # the parser's message can quote the text it was given
        raise TargetError(
            "invalid_url",
            f"{DATABASE_URL_ENV} is not a valid database URL (its value is not shown).",
        ) from None
    if url.get_backend_name() not in SUPPORTED_BACKENDS:
        raise TargetError(
            "unsupported_backend",
            "this command works on SQLite and PostgreSQL databases only.",
        )
    if url.get_backend_name() == "sqlite":
        _require_existing_sqlite_file(url.database)

    try:
        engine = create_db_engine(url_text)
        if url.get_backend_name() == "sqlite":
            _leave_journal_mode_alone(engine)
    except ImportError:
        raise TargetError(
            "driver_missing", "the database driver for this URL is not installed."
        ) from None
    except Exception:
        raise TargetError(
            "invalid_url",
            f"{DATABASE_URL_ENV} is not a usable database URL (its value is not shown).",
        ) from None

    try:
        with engine.connect() as connection:
            check_schema_at_head(connection)
    except BaseException as exc:
        engine.dispose()
        if isinstance(exc, sa.exc.DBAPIError):
            raise _unavailable() from None
        raise
    return engine


def _leave_journal_mode_alone(engine: Engine) -> None:
    """The persistence layer's SQLite connection setup, minus the journal mode.

    create_db_engine runs `PRAGMA journal_mode=WAL` on every new connection. That is not
    a per-connection setting but a change of the database FILE (a rollback-journal
    database becomes a WAL one, for good), and it happens at the first connect, before
    this module has looked at what the file is: a database that is then refused would
    already have been rewritten. An administrative command uses a database as it finds
    it, so the engine it works with gets the rest of that setup and not this pragma. A
    database that is already in WAL mode (every one the applications have opened) stays
    one: the mode is read from the file.

    Everything else of create_db_engine is kept as it is, including its `begin` hook.
    tests/test_admin_target.py compares the settings of the two kinds of connection.
    """
    sa.event.remove(engine, "connect", _configure_sqlite_connection)
    sa.event.listen(engine, "connect", _configure_sqlite_connection_as_is)


def _configure_sqlite_connection_as_is(dbapi_connection, _connection_record) -> None:
    # What persistence.database._configure_sqlite_connection does, without the journal
    # mode (see there for why the sqlite3 module is told to manage no transactions).
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _require_existing_sqlite_file(database: Optional[str]) -> None:
    if not database or database == ":memory:":
        raise TargetError(
            "sqlite_not_a_file",
            "an in-memory SQLite database cannot be provisioned: point "
            f"{DATABASE_URL_ENV} at a database file.",
        )
    if database.startswith("file:"):
        raise TargetError(
            "sqlite_not_a_file", "SQLite 'file:' URIs are not supported: use a plain file path."
        )
    path = Path(database)
    if not path.is_file() or path.stat().st_size == 0:
        raise TargetError(
            "database_missing",
            "the SQLite database file does not exist or is empty. This command never creates "
            "a database: create it and run the migrations first (see the README).",
        )


def _unavailable() -> TargetError:
    # A PostgreSQL server that has no such database is reported by the driver like any
    # other failure to connect (verified with psycopg 3: the error has no SQLSTATE), so
    # the causes cannot be told apart and none is claimed.
    return TargetError(
        "database_unavailable",
        "could not use the database: it does not exist, is not reachable, refused the "
        "credentials, or is not a database. This command never creates a database. The "
        "driver's details are withheld because they can name hosts and users.",
    )


def repository_head() -> str:
    """The single Alembic head of the migrations in this repository."""
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    if len(heads) != 1:  # a branched history: nothing can be said to be "the head"
        raise TargetError("schema_not_head", "the repository's migrations have more than one head.")
    return heads[0]


def check_schema_at_head(connection: sa.Connection) -> None:
    """TargetError unless the database's revision is the repository's head. Only reads
    (MigrationContext does not create the version table until a migration runs)."""
    head = repository_head()
    current = MigrationContext.configure(connection).get_current_heads()
    if current != (head,):
        raise TargetError(
            "schema_not_head",
            f"the database schema is at revision {_describe(current)}, but this version of the "
            f"application needs {head}. This command never migrates: back up the database and "
            "run the migrations deliberately first (see the README).",
        )


def _describe(revisions: tuple) -> str:
    if not revisions:
        return "(none: not created by this application's migrations)"
    # The value comes from the database: print it only if it is a plain identifier.
    return ", ".join(
        r if isinstance(r, str) and _REVISION_ID.fullmatch(r) else "(unrecognised)"
        for r in revisions
    )
