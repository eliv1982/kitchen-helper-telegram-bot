"""Which database an administrative command will touch, and which it refuses to.

The rules (house_helper/admin/target.py): the URL comes from HOUSE_HELPER_DATABASE_URL
only, a missing SQLite file is never created, the Alembic revision must be the
repository's head, nothing is ever migrated, and neither the URL nor a driver's text is
printed. Every refusal here is checked for the other half too: the database, if there
was one, is exactly as it was.

PostgreSQL is not available to the suite (tests/test_admin_postgres.py runs against a
real server, opt-in). Its failures are simulated here by an engine whose connect() raises
what a driver raises (with a password and a host name inside the message, which must not
come out).
"""

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from house_helper.admin import cli, target
from house_helper.admin.target import TargetError, check_schema_at_head, open_target, repository_head
from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    create_db_engine,
    upgrade_database,
)
from tests.admin_support import (  # noqa: F401  (fixtures)
    counts,
    db_path,
    db_url,
    everything,
    journal_mode,
    migrated_template,
    provision,
    rows,
    run_cli,
    set_journal_mode,
    snapshot,
)

PASSWORD = "SECRETPW"
PG_URL = f"postgresql://adminuser:{PASSWORD}@db.internal.example:5432/prodkitchen"
PG_TOKENS = (PASSWORD, "adminuser", "db.internal.example", "prodkitchen", "5432")


def assert_refused(result, code, *, status="error"):
    assert result.code == 4
    assert result.out == ""
    assert result.err.startswith(f"{status} [{code}]: "), result.err
    assert "Traceback" not in result.err


# --- the variable ----------------------------------------------------------------------------------


def test_without_the_variable_nothing_is_used_not_even_the_development_default(
    capsys, tmp_path, monkeypatch, migrated_template
):
    import shutil

    default = tmp_path / "house_helper.db"  # what persistence.database would use
    shutil.copyfile(migrated_template, default)
    monkeypatch.chdir(tmp_path)
    before = everything(f"sqlite:///{default}")
    listing = sorted(p.name for p in tmp_path.iterdir())

    result = provision(capsys, None)

    assert_refused(result, "not_configured")
    assert DATABASE_URL_ENV in result.err
    assert everything(f"sqlite:///{default}") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == listing


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_an_empty_variable_is_not_a_configuration(capsys, value):
    assert_refused(provision(capsys, value), "not_configured")


def test_the_url_is_never_taken_from_an_argument(capsys, db_url):
    result = provision(capsys, None, "elena@example.com", "--database-url", db_url)

    assert result.code == 2  # an unknown option: and the URL it carried is not echoed
    assert db_url not in result.text


# --- a database that must already exist -------------------------------------------------------------


def test_a_missing_sqlite_file_is_refused_and_not_created(capsys, tmp_path):
    missing = tmp_path / "absent.db"

    result = provision(capsys, f"sqlite:///{missing}")

    assert_refused(result, "database_missing")
    assert "never creates a database" in result.err
    assert list(tmp_path.iterdir()) == []


def test_a_missing_directory_is_not_created_either(capsys, tmp_path):
    result = provision(capsys, f"sqlite:///{tmp_path / 'no' / 'such' / 'kitchen.db'}")

    assert_refused(result, "database_missing")
    assert list(tmp_path.iterdir()) == []


def test_an_empty_file_is_refused_and_left_empty(capsys, tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")

    result = provision(capsys, f"sqlite:///{empty}")

    assert_refused(result, "database_missing")
    assert empty.read_bytes() == b"" and [p.name for p in tmp_path.iterdir()] == ["empty.db"]


def test_a_directory_is_not_a_database(capsys, tmp_path):
    assert_refused(provision(capsys, f"sqlite:///{tmp_path}"), "database_missing")


def test_a_file_that_is_not_a_database_is_refused_and_left_alone(capsys, tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a SQLite database " * 200)

    result = provision(capsys, f"sqlite:///{junk}")

    assert_refused(result, "database_unavailable")
    assert junk.read_bytes() == b"this is not a SQLite database " * 200
    assert [p.name for p in tmp_path.iterdir()] == ["junk.db"]


@pytest.mark.parametrize(
    "url",
    ["sqlite://", "sqlite:///:memory:", "sqlite:///file:kitchen.db?mode=rw&uri=true"],
    ids=["no-database", "memory", "file-uri"],
)
def test_sqlite_forms_that_are_not_a_plain_file_are_refused(capsys, url):
    assert_refused(provision(capsys, url), "sqlite_not_a_file")


@pytest.mark.parametrize("scheme", ["mysql", "postgres", "oracle", "mssql+pyodbc"])
def test_other_database_types_are_refused_without_echoing_the_url(capsys, scheme):
    result = provision(capsys, f"{scheme}://adminuser:{PASSWORD}@db.internal.example/prodkitchen")

    assert_refused(result, "unsupported_backend")
    for token in PG_TOKENS:
        assert token not in result.text


def test_text_that_is_not_a_url_is_refused_without_echoing_it(capsys):
    result = provision(capsys, f"not a url {PASSWORD}")

    assert_refused(result, "invalid_url")
    assert PASSWORD not in result.text


# --- PostgreSQL failures, simulated ----------------------------------------------------------------------


class FailingEngine:
    """An engine whose connection attempt fails the way a driver's does."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.disposed = False

    def connect(self):
        raise self.error

    def dispose(self) -> None:
        self.disposed = True


def driver_failure():
    return sa.exc.OperationalError(
        "SELECT 1 /* adminuser */",
        {"password": PASSWORD},
        Exception(
            f'connection to server at "db.internal.example" (10.0.0.7), port 5432 failed: '
            f'FATAL: database "prodkitchen" does not exist or password authentication failed '
            f'for user "adminuser" ({PASSWORD})'
        ),
    )


def test_a_postgresql_server_that_cannot_be_used_is_reported_without_its_text(
    capsys, monkeypatch
):
    engine = FailingEngine(driver_failure())
    monkeypatch.setattr(target, "create_db_engine", lambda url: engine)

    result = provision(capsys, PG_URL)

    assert_refused(result, "database_unavailable")
    assert "never creates a database" in result.err
    for token in (*PG_TOKENS, "FATAL", "10.0.0.7", "SELECT"):
        assert token not in result.text
    assert engine.disposed


def test_a_missing_driver_is_reported_without_the_url(capsys, monkeypatch):
    def no_driver(url):
        raise ModuleNotFoundError(f"No module named 'psycopg2' ({url})")

    monkeypatch.setattr(target, "create_db_engine", no_driver)

    result = provision(capsys, PG_URL)

    assert_refused(result, "driver_missing")
    for token in PG_TOKENS:
        assert token not in result.text


def test_an_engine_that_cannot_be_built_is_reported_without_the_url(capsys, monkeypatch):
    def refuses(url):
        raise sa.exc.ArgumentError(f"cannot use {url}")

    monkeypatch.setattr(target, "create_db_engine", refuses)

    result = provision(capsys, PG_URL)

    assert_refused(result, "invalid_url")
    for token in PG_TOKENS:
        assert token not in result.text


# --- the schema must be at the head, and is never changed ---------------------------------------------


def database_at(tmp_path, revision, name="old.db"):
    path = tmp_path / name
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        upgrade_database(engine, revision)
    finally:
        engine.dispose()
    return f"sqlite:///{path}"


def test_the_head_is_the_one_the_migrations_in_the_repository_end_with(db_url):
    engine = create_db_engine(db_url)
    try:
        with engine.connect() as connection:
            check_schema_at_head(connection)  # does not raise
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).all() == [
                (repository_head(),)
            ]
    finally:
        engine.dispose()
    assert repository_head() == "0006_oidc_state_invitation"


@pytest.mark.parametrize(
    "revision",
    [
        "0001_persistence_foundation",
        "0002_session_provenance",
        "0003_email_login_challenges",
        "0004_email_identity_case",
    ],
)
def test_a_schema_behind_the_head_is_refused_and_not_migrated(capsys, tmp_path, revision):
    url = database_at(tmp_path, revision)
    tables = rows(url, "SELECT name FROM sqlite_master ORDER BY name")

    result = provision(capsys, url)

    assert_refused(result, "schema_not_head")
    assert f"revision {revision}" in result.err
    assert f"needs {repository_head()}" in result.err
    assert "never migrates" in result.err
    assert rows(url, "SELECT version_num FROM alembic_version") == [(revision,)]
    assert rows(url, "SELECT name FROM sqlite_master ORDER BY name") == tables
    assert rows(url, "SELECT count(*) FROM users") == [(0,)]


def test_a_database_that_was_not_made_by_the_migrations_is_refused(capsys, tmp_path):
    path = tmp_path / "foreign.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE users (id TEXT)")
    connection.commit()
    connection.close()

    result = provision(capsys, f"sqlite:///{path}")

    assert_refused(result, "schema_not_head")
    assert "(none: not created by this application's migrations)" in result.err
    assert rows(f"sqlite:///{path}", "SELECT name FROM sqlite_master ORDER BY name") == [("users",)]


def test_a_revision_that_is_not_a_plain_identifier_is_not_echoed(capsys, db_url, db_path):
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE alembic_version SET version_num = ?", ("\x1b[31m evil revision",))
    connection.commit()
    connection.close()

    result = provision(capsys, db_url)

    assert_refused(result, "schema_not_head")
    assert "(unrecognised)" in result.err
    assert "evil" not in result.err and "\x1b" not in result.err


def test_a_database_at_two_heads_is_not_at_the_head(capsys, db_url, db_path):
    connection = sqlite3.connect(db_path)
    connection.execute("INSERT INTO alembic_version (version_num) VALUES ('0003_email_login_challenges')")
    connection.commit()
    connection.close()

    assert_refused(provision(capsys, db_url), "schema_not_head")


def test_the_head_is_checked_again_inside_the_transaction(capsys, db_url, db_path, monkeypatch):
    """A schema change that was COMMITTED between the first look and the write
    transaction is caught by the second check, inside it: nothing is written. That
    narrows the window and nothing more: it is not protection against a migration that
    is running at the same time (see provisioning.py: they must never run together)."""
    real = cli.open_target

    def open_then_change_the_revision(environ):
        engine = real(environ)
        connection = sqlite3.connect(db_path)
        connection.execute("UPDATE alembic_version SET version_num = '0003_email_login_challenges'")
        connection.commit()
        connection.close()
        return engine

    monkeypatch.setattr(cli, "open_target", open_then_change_the_revision)

    result = provision(capsys, db_url)

    assert_refused(result, "schema_not_head")
    assert counts(db_url)["users"] == 0 and counts(db_url)["allowlist_entries"] == 0


def test_a_database_at_the_head_is_opened_and_nothing_is_written_by_opening_it(db_url):
    before = everything(db_url)

    engine = open_target({DATABASE_URL_ENV: db_url})
    engine.dispose()

    assert everything(db_url) == before


def test_open_target_raises_target_error_with_a_stable_code(tmp_path):
    with pytest.raises(TargetError) as refused:
        open_target({DATABASE_URL_ENV: f"sqlite:///{tmp_path / 'absent.db'}"})

    assert refused.value.code == "database_missing"
    with pytest.raises(TargetError) as not_set:
        open_target({})
    assert not_set.value.code == "not_configured"


# --- the file is not changed by looking at it: the SQLite journal mode ------------------------------------
#
# create_db_engine runs `PRAGMA journal_mode=WAL` on every connection it makes. That
# rewrites the database FILE (a rollback-journal database becomes a WAL one for good)
# and it used to happen before the command had decided to accept the database: a
# refused one was changed all the same. The engine of an administrative command leaves
# the journal mode alone and keeps the rest of the persistence layer's connection setup.


def built_behind_the_head(tmp_path, template):
    return Path(database_at(tmp_path, "0003_email_login_challenges").removeprefix("sqlite:///"))


def built_by_something_else(tmp_path, template):
    path = tmp_path / "foreign.db"
    connection = sqlite3.connect(path)  # the module's default journal mode: delete
    connection.execute("CREATE TABLE users (id TEXT)")
    connection.commit()
    connection.close()
    return path


def built_at_two_heads(tmp_path, template):
    import shutil

    path = tmp_path / "two-heads.db"
    shutil.copyfile(template, path)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO alembic_version (version_num) VALUES ('0003_email_login_challenges')")
    connection.commit()
    connection.close()
    return path


@pytest.mark.parametrize("mode", ["delete", "wal"])
@pytest.mark.parametrize(
    "build",
    [built_behind_the_head, built_by_something_else, built_at_two_heads],
    ids=["behind-the-head", "not-from-the-migrations", "two-heads"],
)
def test_a_refused_database_keeps_its_journal_mode_and_every_byte(
    capsys, tmp_path, migrated_template, build, mode
):
    path = build(tmp_path, migrated_template)
    set_journal_mode(path, mode)
    before = snapshot(tmp_path)

    result = provision(capsys, f"sqlite:///{path}")

    assert_refused(result, "schema_not_head")
    assert journal_mode(path) == mode
    assert snapshot(tmp_path) == before  # same bytes, and no -wal, -shm or -journal left over


def test_a_database_refused_for_its_contents_keeps_its_journal_mode_and_every_byte(
    capsys, db_path, db_url
):
    # Past the look at the schema: the refusal comes out of the write transaction.
    assert provision(capsys, db_url).code == 0
    set_journal_mode(db_path, "delete")
    before = snapshot(db_path.parent)

    result = provision(capsys, db_url, "other@example.com")

    assert result.code == 3 and result.err.startswith("refused [user_exists]")
    assert journal_mode(db_path) == "delete"
    assert snapshot(db_path.parent) == before


def test_a_successful_run_does_not_change_the_journal_mode_either(capsys, db_path, db_url):
    set_journal_mode(db_path, "delete")

    result = provision(capsys, db_url)

    assert result.code == 0, result.text
    assert journal_mode(db_path) == "delete"
    assert counts(db_url)["users"] == 1 and counts(db_url)["allowlist_entries"] == 1
    assert [p.name for p in db_path.parent.iterdir()] == [db_path.name]  # no side files left


def test_a_database_in_wal_mode_is_used_in_wal_mode(capsys, db_path, db_url):
    assert journal_mode(db_path) == "wal"  # the template is what the migrations leave

    assert provision(capsys, db_url).code == 0

    assert journal_mode(db_path) == "wal"


PRAGMAS = (
    "foreign_keys",
    "busy_timeout",
    "synchronous",
    "locking_mode",
    "recursive_triggers",
    "temp_store",
    "wal_autocheckpoint",
    "cache_size",
    "secure_delete",
    "journal_mode",
)


def connection_state(engine) -> dict:
    with engine.connect() as connection:
        state = {name: connection.exec_driver_sql(f"PRAGMA {name}").scalar() for name in PRAGMAS}
        dbapi_connection = connection.connection.dbapi_connection
        state["sqlite3_isolation_level"] = dbapi_connection.isolation_level
        state["begin_issued_by_the_engine"] = dbapi_connection.in_transaction
        return state


def test_the_engine_is_the_persistence_engine_apart_from_the_journal_mode(db_path, db_url, tmp_path):
    import shutil

    standard = create_db_engine(db_url)
    admin = open_target({DATABASE_URL_ENV: db_url})
    try:
        same = connection_state(admin)
        assert same == connection_state(standard)  # on a WAL database, in every setting
        assert same["foreign_keys"] == 1
        assert same["sqlite3_isolation_level"] is None and same["begin_issued_by_the_engine"]
    finally:
        standard.dispose()
        admin.dispose()

    # On a rollback-journal database the one difference shows, and only that one.
    other = tmp_path / "rollback.db"
    shutil.copyfile(db_path, other)
    set_journal_mode(other, "delete")
    standard = create_db_engine(f"sqlite:///{other}")
    admin = open_target({DATABASE_URL_ENV: f"sqlite:///{other}"})
    try:
        admin_state = connection_state(admin)
        assert admin_state.pop("journal_mode") == "delete"
        # (the control: this is what the standard engine does, and why the admin one differs)
        standard_state = connection_state(standard)
        assert standard_state.pop("journal_mode") == "wal"
        assert admin_state == standard_state
    finally:
        standard.dispose()
        admin.dispose()


def test_foreign_keys_are_enforced_inside_the_transaction_the_admin_engine_begins(db_url):
    engine = open_target({DATABASE_URL_ENV: db_url})
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.connection.dbapi_connection.in_transaction  # the BEGIN was issued
            connection.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE child (parent_id INTEGER REFERENCES parent (id))")
            with pytest.raises(sa.exc.IntegrityError, match="FOREIGN KEY"):
                connection.exec_driver_sql("INSERT INTO child VALUES (1)")
            connection.rollback()
    finally:
        engine.dispose()


# --- the engine is always released -----------------------------------------------------------------------


@pytest.fixture
def spied_engine(monkeypatch):
    """create_db_engine, with dispose() recorded."""
    made = []
    real = target.create_db_engine

    def make(url):
        engine = real(url)
        engine.dispose_calls = 0
        original = engine.dispose

        def dispose(*args, **kwargs):
            engine.dispose_calls += 1
            return original(*args, **kwargs)

        engine.dispose = dispose
        made.append(engine)
        return engine

    monkeypatch.setattr(target, "create_db_engine", make)
    return made


def test_the_engine_is_disposed_when_the_schema_is_refused(capsys, tmp_path, spied_engine):
    provision(capsys, database_at(tmp_path, "0003_email_login_challenges"))

    assert [engine.dispose_calls for engine in spied_engine] == [1]


@pytest.mark.parametrize("email, expected", [("Elena@example.com", 0), ("other@example.com", 3)])
def test_the_engine_is_disposed_after_a_run_and_after_a_refusal(
    capsys, db_url, spied_engine, email, expected
):
    if expected:
        assert provision(capsys, db_url, "Elena@example.com").code == 0
    spied_engine.clear()

    assert provision(capsys, db_url, email).code == expected

    assert [engine.dispose_calls for engine in spied_engine] == [1]
