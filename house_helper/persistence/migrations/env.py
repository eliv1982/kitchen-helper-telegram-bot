"""Alembic environment.

Three ways in, in this order of precedence:
  * a connection handed over in config.attributes["connection"] (init_database
    and the tests: the migration runs on the caller's engine, with its SQLite
    pragmas);
  * a URL in config.attributes["url"];
  * otherwise the alembic command: HOUSE_HELPER_DATABASE_URL, or the default
    SQLite file (see house_helper.persistence.database).
Offline mode (--sql) needs only the URL and no database driver.
"""

from logging.config import fileConfig

from alembic import context

from house_helper.persistence.base import Base
from house_helper.persistence.database import create_db_engine, database_url_from_environment

# Importing the models is what puts the tables on Base.metadata: without it the
# metadata is empty and autogenerate would propose dropping everything.
from house_helper.persistence import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.attributes.get("url") or database_url_from_environment()


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER most things in place; batch mode rebuilds the
        # table. It is a no-op elsewhere, so later migrations written against
        # SQLite also run on PostgreSQL.
        render_as_batch=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_on(connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_on(connection)
        return
    engine = create_db_engine(_url())
    try:
        with engine.connect() as connection:
            _run_on(connection)
            connection.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
