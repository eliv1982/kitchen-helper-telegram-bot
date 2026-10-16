"""Engine, session factory and schema initialisation.

The schema is created and changed only by the Alembic migrations in
./migrations; init_database() brings a database up to the latest one. Nothing
here reads a .env file or any provider setting: the database URL comes from the
caller or from the HOUSE_HELPER_DATABASE_URL environment variable, and is never
logged (a PostgreSQL URL carries a password).

The engine is synchronous. Choosing how the web application drives it (a thread
pool, or an async driver) is left to the increment that adds the web layer.
"""

import os
from pathlib import Path
from typing import Optional

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

DATABASE_URL_ENV = "HOUSE_HELPER_DATABASE_URL"

# Relative to the working directory, like the .env file; set the variable (or
# pass a URL) to put the database somewhere fixed.
DEFAULT_DATABASE_URL = "sqlite:///house_helper.db"

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def database_url_from_environment() -> str:
    return os.environ.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    # Take transaction control away from the sqlite3 module. In its default
    # ("legacy") mode it opens a transaction only before INSERT, UPDATE, DELETE
    # and REPLACE: a SELECT, a SAVEPOINT or DDL runs outside any transaction. A
    # SAVEPOINT taken while none is open is then the outermost one, and RELEASE
    # commits it, so a session.begin_nested() that began with only reads behind
    # it would survive the caller's rollback. With isolation_level None the
    # module never issues BEGIN or COMMIT by itself; _begin_sqlite_transaction
    # issues the BEGIN, when SQLAlchemy begins a transaction. (The same on
    # Python 3.10, 3.11 and 3.12: the sqlite3 `autocommit` attribute of 3.12 is
    # left at its default and is not needed.)
    #
    # For migrations: alembic's engine.begin() now opens the transaction before
    # the first DDL statement, so a failed upgrade rolls back whole. The price is
    # that PRAGMA foreign_keys=OFF is ignored inside a transaction. The first
    # migration that rebuilds a table in batch mode (env.py renders as batch) must
    # switch foreign keys off before upgrade_database() begins, or the DROP of the
    # old table cascades into its children.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        # Off by default in SQLite, per connection: without it no foreign key,
        # ON DELETE CASCADE or ON DELETE SET NULL is enforced.
        cursor.execute("PRAGMA foreign_keys=ON")
        # Readers do not block the writer, nor the writer the readers. A no-op
        # for an in-memory database.
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def _begin_sqlite_transaction(connection: sa.Connection) -> None:
    # A deferred BEGIN, as the sqlite3 module would have issued, but now at the
    # start of every transaction instead of before its first write. Not
    # IMMEDIATE: that would make a read-only session take the write lock, so a
    # reader could no longer run beside a writer, which is what WAL is for. The
    # price of deferring: a transaction that has read and then writes fails at
    # once with "database is locked" (SQLITE_BUSY_SNAPSHOT) if another writer
    # committed in between; it is rolled back and retried as a whole (see
    # tests/test_persistence_transactions.py, "losing the race").
    connection.exec_driver_sql("BEGIN")


def create_db_engine(url: Optional[str] = None) -> Engine:
    """An engine for `url` (default: the environment, then a local SQLite file)."""
    engine = sa.create_engine(url or database_url_from_environment())
    if engine.dialect.name == "sqlite":
        sa.event.listen(engine, "connect", _configure_sqlite_connection)
        sa.event.listen(engine, "begin", _begin_sqlite_transaction)
    return engine


def create_session_factory(engine: Engine) -> "sessionmaker[OrmSession]":
    # expire_on_commit=False: a committed object stays readable (an answer is
    # built from objects after the transaction that stored them has ended).
    return sessionmaker(engine, expire_on_commit=False)


def alembic_config(url: Optional[str] = None) -> Config:
    """Alembic's configuration, built in code so it does not depend on the
    working directory or on alembic.ini (which exists for the alembic command)."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url is not None:
        # Read by migrations/env.py. Not a main option: ConfigParser would
        # misread a '%' in an escaped password.
        config.attributes["url"] = url
    return config


def upgrade_database(engine: Engine, revision: str = "head") -> None:
    """Apply migrations through `revision`, on a connection of `engine`."""
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def downgrade_database(engine: Engine, revision: str) -> None:
    config = alembic_config()
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def _ensure_sqlite_directory(url: str) -> None:
    parsed = make_url(url)
    database = parsed.database
    if parsed.get_backend_name() != "sqlite" or not database:
        return
    if database == ":memory:" or database.startswith("file:"):
        return
    Path(database).parent.mkdir(parents=True, exist_ok=True)


def init_database(url: Optional[str] = None) -> Engine:
    """Create the database if needed, bring it to the latest schema and return
    an engine on it. Safe to call on every start: an up-to-date database is left
    alone."""
    url = url or database_url_from_environment()
    _ensure_sqlite_directory(url)
    engine = create_db_engine(url)
    try:
        upgrade_database(engine)
    except BaseException:
        engine.dispose()
        raise
    return engine
