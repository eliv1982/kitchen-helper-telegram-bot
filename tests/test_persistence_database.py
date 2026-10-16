"""Engine setup and database initialisation."""

import pytest
import sqlalchemy as sa
from alembic.util.exc import CommandError

from house_helper.persistence import database
from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    DEFAULT_DATABASE_URL,
    create_db_engine,
    create_session_factory,
    database_url_from_environment,
    init_database,
)
from house_helper.persistence.models import Conversation, User
from tests.persistence_support import new_conversation, new_user
from tests.test_persistence_schema import TABLES


def _sqlite_url(path) -> str:
    return f"sqlite:///{path}"


def _pragma(engine, name):
    with engine.connect() as connection:
        return connection.exec_driver_sql(f"PRAGMA {name}").scalar()


# --- where the database is -------------------------------------------------------


def test_the_database_url_comes_from_the_environment_variable(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV, "sqlite:///elsewhere.db")

    assert database_url_from_environment() == "sqlite:///elsewhere.db"


def test_without_the_variable_the_default_is_a_local_sqlite_file(monkeypatch):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)

    assert database_url_from_environment() == DEFAULT_DATABASE_URL
    assert DEFAULT_DATABASE_URL.startswith("sqlite:///")


def test_an_empty_variable_means_the_default(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV, "")

    assert database_url_from_environment() == DEFAULT_DATABASE_URL


def test_an_engine_is_built_for_the_environment_url_when_none_is_given(monkeypatch, tmp_path):
    monkeypatch.setenv(DATABASE_URL_ENV, _sqlite_url(tmp_path / "from_env.db"))
    engine = create_db_engine()
    try:
        assert engine.url.database == str(tmp_path / "from_env.db")
    finally:
        engine.dispose()


# --- SQLite connection setup ------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(_sqlite_url(tmp_path / "engine.db"))
    yield engine
    engine.dispose()


def test_foreign_keys_are_enforced_on_every_connection(engine):
    with engine.connect() as first, engine.connect() as second:
        assert first.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert second.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_foreign_keys_are_enforced_on_an_in_memory_database_too():
    engine = create_db_engine("sqlite://")
    try:
        assert _pragma(engine, "foreign_keys") == 1
    finally:
        engine.dispose()


def test_a_file_database_uses_write_ahead_logging(engine):
    assert _pragma(engine, "journal_mode") == "wal"


def test_the_sqlite_module_is_left_to_manage_no_transactions_itself(engine):
    # None: it never issues BEGIN or COMMIT on its own. create_db_engine issues the
    # BEGIN (see test_persistence_transactions.py for what that is for).
    with engine.connect() as connection:
        assert connection.connection.dbapi_connection.isolation_level is None


@pytest.mark.parametrize("url", ["file", "memory"])
def test_foreign_keys_and_write_ahead_logging_hold_inside_the_transaction_the_engine_begins(
    tmp_path, url
):
    engine = create_db_engine(_sqlite_url(tmp_path / "both.db") if url == "file" else "sqlite://")
    try:
        with engine.connect() as connection:
            dbapi_connection = connection.connection.dbapi_connection
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert dbapi_connection.in_transaction  # the BEGIN was issued, before this read
            if url == "file":
                assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"

            connection.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE child (parent_id INTEGER REFERENCES parent (id))"
            )
            with pytest.raises(sa.exc.IntegrityError, match="FOREIGN KEY"):
                connection.exec_driver_sql("INSERT INTO child VALUES (1)")
    finally:
        engine.dispose()


def test_a_connection_that_is_returned_to_the_pool_with_work_in_flight_comes_back_clean(engine):
    with engine.connect() as connection:
        connection.exec_driver_sql("CREATE TABLE t (x INTEGER)")
        connection.commit()
    with engine.connect() as connection:
        connection.exec_driver_sql("INSERT INTO t VALUES (1)")  # never committed

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM t").scalar() == 0


# --- sessions ---------------------------------------------------------------------


def test_objects_stay_readable_after_their_transaction_commits(tmp_path):
    engine = init_database(_sqlite_url(tmp_path / "detached.db"))
    try:
        with create_session_factory(engine)() as session:
            user = new_user(session, display_name="Елена")
            session.commit()

        assert user.display_name == "Елена"  # detached and not expired: no refresh needed
    finally:
        engine.dispose()


# --- initialising a database ------------------------------------------------------


def test_init_creates_the_database_with_every_table(tmp_path):
    engine = init_database(_sqlite_url(tmp_path / "fresh.db"))
    try:
        assert set(sa.inspect(engine).get_table_names()) == TABLES | {"alembic_version"}
    finally:
        engine.dispose()


def test_init_creates_a_missing_directory_for_a_sqlite_file(tmp_path):
    target = tmp_path / "not" / "yet" / "there" / "app.db"

    engine = init_database(_sqlite_url(target))
    engine.dispose()

    assert target.is_file()


def test_init_returns_a_usable_engine(tmp_path):
    engine = init_database(_sqlite_url(tmp_path / "usable.db"))
    try:
        with create_session_factory(engine)() as session:
            new_conversation(session, new_user(session), title="Борщ")
            session.commit()
            assert session.scalar(sa.select(Conversation.title)) == "Борщ"
    finally:
        engine.dispose()


def test_init_can_run_on_every_start_and_keeps_what_is_stored(tmp_path):
    url = _sqlite_url(tmp_path / "restarts.db")
    first = init_database(url)
    with create_session_factory(first)() as session:
        new_user(session, display_name="stays")
        session.commit()
    first.dispose()

    second = init_database(url)
    try:
        with create_session_factory(second)() as session:
            assert session.scalar(sa.select(User.display_name)) == "stays"
    finally:
        second.dispose()


def test_init_uses_the_environment_url_when_none_is_given(monkeypatch, tmp_path):
    monkeypatch.setenv(DATABASE_URL_ENV, _sqlite_url(tmp_path / "env_init.db"))

    engine = init_database()
    engine.dispose()

    assert (tmp_path / "env_init.db").is_file()


def test_init_does_not_create_directories_for_an_in_memory_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    engine = init_database("sqlite://")
    engine.dispose()

    assert list(tmp_path.iterdir()) == []


def test_init_refuses_a_database_stamped_with_a_revision_it_does_not_know(tmp_path):
    url = _sqlite_url(tmp_path / "newer.db")
    engine = create_db_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('from_the_future')")
    engine.dispose()

    with pytest.raises(CommandError, match="from_the_future"):
        init_database(url)


def test_init_releases_the_engine_when_the_upgrade_fails(tmp_path, monkeypatch):
    disposed = []
    real_dispose = sa.engine.Engine.dispose

    def recording_dispose(self, *args, **kwargs):
        disposed.append(self)
        return real_dispose(self, *args, **kwargs)

    def failing_upgrade(engine, revision="head"):
        raise RuntimeError("upgrade failed")

    monkeypatch.setattr(sa.engine.Engine, "dispose", recording_dispose)
    monkeypatch.setattr(database, "upgrade_database", failing_upgrade)

    with pytest.raises(RuntimeError, match="upgrade failed"):
        init_database(_sqlite_url(tmp_path / "failing.db"))

    assert len(disposed) == 1
