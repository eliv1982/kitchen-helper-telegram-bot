"""`provision-email` against a real PostgreSQL server. Opt-in: skipped unless configured.

The rest of the suite has no PostgreSQL, so everything here is what the SQLite tests
cannot show: the transaction runs at READ COMMITTED even where the database says
otherwise, the advisory lock really serialises concurrent first runs, a refusal or a
failure rolls back and releases the lock, and the connection goes back to the
database's own isolation level.

To run (use a throwaway server on THIS machine; these tests CREATE and DROP databases on it):

    docker run -d --rm --name hh-test-pg -e POSTGRES_PASSWORD=PASSWORD \\
        -p 127.0.0.1:55432:5432 postgres:16-alpine
    pip install "psycopg[binary]"          # not in requirements.txt: the driver is optional
    export HOUSE_HELPER_TEST_POSTGRES_URL="postgresql+psycopg://postgres:PASSWORD@127.0.0.1:55432/postgres"
    pytest tests/test_admin_postgres.py -v -rs

(PowerShell: `$env:HOUSE_HELPER_TEST_POSTGRES_URL = "..."`.) The URL names a server and a
maintenance database to connect to, and the user needs the CREATEDB privilege. The tests
never use that database's data, and never HOUSE_HELPER_DATABASE_URL. What keeps them from
going anywhere else is tests/postgres_harness.py (read its docstring); in short:

  * the URL is checked BEFORE any connection: psycopg 3 driver, host localhost / 127.0.0.1
    / ::1 (and nothing else), an explicit port and user, NO query parameters (`?host=`,
    `?hostaddr=`, `?dbname=`, `?service=` ... override the URL's own fields), and none of
    the libpq variables that can route a connection (PGHOSTADDR, PGSERVICE, ...) set;
  * the connection parameters are built from those validated fields, host and hostaddr
    pinned to the loopback IP; every new connection is then checked (peer IP and port,
    database name, and for a scratch database the token of this run) before a statement
    runs on it;
  * each test gets a FRESH database `house_helper_test_<token>_<n>`, cloned from one
    migrated template of this run; nothing is ever truncated or reset. Only databases this
    run created, and still finds carrying its token, are dropped (`DROP DATABASE ... WITH
    (FORCE)`, so a failed test leaves nothing behind).

The URL is a fixture value only as a SQLAlchemy URL object, whose text hides the password
(pytest prints the arguments of a failed test); no assertion here shows a password or a URL.

Without the variable every test here is skipped, which is not a pass: `pytest -rs` lists
the skip. CI does not set it. The harness itself is tested offline, in CI, by
tests/test_admin_postgres_harness.py.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa

from house_helper.admin import provisioning
from house_helper.admin.cli import main
from house_helper.admin.provisioning import (
    ADVISORY_LOCK_KEY,
    Outcome,
    Refusal,
    RefusalReason,
    prepare_request,
    provision_email,
)
from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    create_session_factory,
    upgrade_database,
)
from tests.postgres_harness import (
    POSTGRES_URL_ENV,
    Scratch,
    UnsafeTarget,
    leaked,
    run_from_environment,
)
from tests.test_admin_provisioning import fail_after

DATABASE_DEFAULT = "repeatable read"  # what each scratch database is set to, below
ADDRESS = "Elena@example.com"
SPELLINGS = ["Elena@example.com", "elena@example.com", "ELENA@example.com"]

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL (see the module docstring)",
)

# The advisory locks held (or waited for) on KEY in the current database, by anyone.
_LOCKS = (
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 1 "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    f"AND ((classid::bigint << 32) | objid::bigint) = {ADVISORY_LOCK_KEY}"
)
_KEY = sa.literal(ADVISORY_LOCK_KEY, sa.BigInteger)


# --- databases of our own ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_run():
    """This module's run on the configured server. An unsafe configuration fails here, at
    setup, before anything is opened; whatever the run created is dropped at the end, also
    when a fixture or a test failed."""
    run = run_from_environment()
    try:
        yield run
    finally:
        run.close()


@pytest.fixture(scope="module")
def template(pg_run):
    """A migrated database that exists for this module only. Every connection the
    migrations use is verified (loopback peer, this database, this run's token) when it is
    opened, so before the first statement."""
    name = pg_run.create_database()
    engine = pg_run.engine(name)
    try:
        upgrade_database(engine)
    finally:
        engine.dispose()
    return name


@pytest.fixture
def scratch(pg_run, template):
    """A database of its own for one test: a copy of the migrated template, EMPTY of data,
    whose default isolation level is REPEATABLE READ (the server's own default is READ
    COMMITTED, which would hide the very thing the command must override)."""
    name = pg_run.create_database(template=template, isolation=DATABASE_DEFAULT)
    try:
        yield Scratch(pg_run, name)
    finally:
        pg_run.drop_database(name)


@pytest.fixture
def monitor(scratch):
    """Autocommit connections on the scratch database, for looking at locks and rows."""
    engine = scratch.engine()
    try:
        yield engine
    finally:
        engine.dispose()


def scalar(engine, sql, **params):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        return connection.execute(sa.text(sql), params).scalar()


def assert_the_lock_is_free(engine):
    assert scalar(engine, _LOCKS) == 0  # nobody holds it and nobody waits for it
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        assert connection.execute(sa.select(sa.func.pg_try_advisory_lock(_KEY))).scalar() is True
        connection.execute(sa.select(sa.func.pg_advisory_unlock(_KEY)))


def run_once(scratch, address):
    """A provisioning run on an engine of its own, as a separate process would have."""
    engine = scratch.engine()
    try:
        return provision_email(create_session_factory(engine), prepare_request(address))
    finally:
        engine.dispose()


# --- the harness itself, with the real driver ---------------------------------------------------------------


def _is_where_it_was_validated_to_be(connection, target):
    return (
        connection.info.hostaddr == target.address
        and connection.info.port == target.port
        and connection.info.dbname == target.maintenance_database
    )


def test_libpq_environment_set_after_validation_cannot_redirect_a_connection_the_harness_opens(
    pg_run, monkeypatch
):
    """The explicit hostaddr (and host, port, dbname) of the harness beat the libpq
    environment; the offline tests only see the parameters, this is the driver. The
    settings below point at 127.0.0.2 (nothing listens there), port 1 and a database that
    does not exist."""
    import psycopg  # (the module is skipped without the variable, and the fixtures need the driver)

    for name, value in {
        "PGHOSTADDR": "127.0.0.2",
        "PGHOST": "127.0.0.2",
        "PGPORT": "1",
        "PGDATABASE": "elsewhere",
    }.items():
        monkeypatch.setenv(name, value)

    # The control, so that this test cannot pass for nothing: the same parameters WITHOUT
    # the hostaddr pin (host, port and dbname stay explicit, and only the server named by
    # this run can answer there) are taken to 127.0.0.2, where they fail ...
    params = pg_run.target.connect_params(pg_run.target.maintenance_database)
    unpinned = {key: value for key, value in params.items() if key != "hostaddr"} | {"connect_timeout": 3}
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**unpinned).close()

    # ... and with the pin the harness opens, the connection is where it was validated to be.
    with pg_run.control() as connection:  # (verified on opening, too)
        assert _is_where_it_was_validated_to_be(connection, pg_run.target)


def test_a_libpq_service_file_set_after_validation_cannot_redirect_a_connection_the_harness_opens(
    pg_run, tmp_path, monkeypatch
):
    """Same for a service file (PGSERVICE), whose entry names another address, port and
    database AND an application name. The application name, which the harness does not
    pin, shows that the file really is in effect; the connection is still where it was
    validated to be. (With libpq 18 an explicit host already makes it ignore the file's
    hostaddr; the harness does not rely on that, and refuses PGSERVICE at validation.)"""
    service_file = tmp_path / "pg_service.conf"
    service_file.write_bytes(b"[redirect]\nhostaddr=127.0.0.2\nport=1\ndbname=elsewhere\napplication_name=from_service\n")
    monkeypatch.setenv("PGSERVICEFILE", str(service_file))
    monkeypatch.setenv("PGSERVICE", "redirect")

    with pg_run.control() as connection:  # (verified on opening)
        assert _is_where_it_was_validated_to_be(connection, pg_run.target)
        assert connection.execute("SHOW application_name").fetchone()[0] == "from_service"


def test_a_database_without_this_runs_token_is_refused_by_the_real_guard(scratch, monitor):
    with monitor.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f"COMMENT ON DATABASE \"{scratch.name}\" IS 'somebody else'")
        try:
            with pytest.raises(UnsafeTarget) as refused:
                scratch.engine().connect()
            assert refused.value.code == "not_this_run"
        finally:
            connection.exec_driver_sql(f"COMMENT ON DATABASE \"{scratch.name}\" IS '{scratch.run.marker}'")
    assert scalar(monitor, "SELECT 1") == 1  # (and it is the run's again)


# --- the isolation level the run really has ------------------------------------------------------------


def test_the_run_is_read_committed_although_the_database_default_is_repeatable_read(
    scratch, monitor, monkeypatch
):
    # The premise: an ordinary transaction on this database IS repeatable read.
    assert scalar(monitor, "SHOW default_transaction_isolation") == DATABASE_DEFAULT
    engine = scratch.engine()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SHOW transaction_isolation").scalar() == DATABASE_DEFAULT

    seen = {}
    real = provisioning.check_schema_at_head

    def look_inside_the_transaction(connection):  # called by the run, in its transaction
        seen["isolation"] = connection.exec_driver_sql("SHOW transaction_isolation").scalar()
        seen["advisory_locks_held_by_this_backend"] = connection.exec_driver_sql(
            _LOCKS + " AND pid = pg_backend_pid() AND granted"
        ).scalar()
        real(connection)

    monkeypatch.setattr(provisioning, "check_schema_at_head", look_inside_the_transaction)
    try:
        result = provision_email(create_session_factory(engine), prepare_request(ADDRESS))
    finally:
        engine.dispose()

    assert result.outcome is Outcome.CREATED
    assert seen == {"isolation": "read committed", "advisory_locks_held_by_this_backend": 1}


def test_the_connection_is_back_at_the_database_default_after_the_run(scratch, monkeypatch):
    engine = scratch.engine()
    backends = []
    real = provisioning.check_schema_at_head

    def note_the_backend(connection):
        backends.append(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar())
        real(connection)

    monkeypatch.setattr(provisioning, "check_schema_at_head", note_the_backend)
    try:
        provision_email(create_session_factory(engine), prepare_request(ADDRESS))
        assert engine.pool.checkedout() == 0
        with engine.connect() as connection:
            # The connection the run used (the pool holds only that one), not a new one ...
            assert connection.exec_driver_sql("SELECT pg_backend_pid()").scalar() == backends[0]
            # ... and its next transaction is not READ COMMITTED any more.
            assert connection.exec_driver_sql("SHOW transaction_isolation").scalar() == DATABASE_DEFAULT
    finally:
        engine.dispose()


# --- concurrent first runs --------------------------------------------------------------------------------


def waiters(engine) -> int:
    return scalar(engine, _LOCKS + " AND NOT granted")


def test_concurrent_runs_with_different_spellings_create_exactly_one_user(scratch, monitor):
    """Every run is held at the advisory lock, before it reads anything, until all of
    them are queued; then the lock is let go. Without READ COMMITTED a run that had
    already taken its snapshot (at the lock statement, under REPEATABLE READ) would find
    no user and make a second one; without the lock they would all make one."""
    with monitor.connect().execution_options(isolation_level="AUTOCOMMIT") as holder:
        holder.execute(sa.select(sa.func.pg_advisory_lock(_KEY)))
        try:
            with ThreadPoolExecutor(max_workers=len(SPELLINGS)) as pool:
                futures = [pool.submit(run_once, scratch, spelling) for spelling in SPELLINGS]
                try:
                    deadline = time.monotonic() + 60
                    while waiters(monitor) < len(SPELLINGS):
                        failed = [f.exception() for f in futures if f.done()]
                        assert not failed, f"a run ended before the lock was released: {failed}"
                        assert time.monotonic() < deadline, "the runs never queued behind the lock"
                        time.sleep(0.05)
                finally:
                    holder.execute(sa.select(sa.func.pg_advisory_unlock(_KEY)))
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result(timeout=60).outcome)
                    except Refusal as refusal:
                        outcomes.append(refusal.reason)
        finally:
            holder.execute(sa.select(sa.func.pg_advisory_unlock_all()))

    assert len(outcomes) == len(SPELLINGS)
    assert outcomes.count(Outcome.CREATED) == 1
    assert outcomes.count(RefusalReason.USER_EXISTS) == len(SPELLINGS) - 1
    assert scalar(monitor, "SELECT count(*) FROM users") == 1
    assert scalar(monitor, "SELECT count(*) FROM allowlist_entries") == 1
    assert_the_lock_is_free(monitor)


# --- rollback, and the lock is let go -------------------------------------------------------------------


def test_a_refusal_changes_nothing_and_releases_the_advisory_lock(scratch, monitor):
    # The engine stays open while the lock is looked at: it must be let go by the end of
    # the transaction, not by the connection closing (the pool keeps the connection).
    engine = scratch.engine()
    factory = create_session_factory(engine)
    try:
        assert provision_email(factory, prepare_request(ADDRESS)).outcome is Outcome.CREATED

        with pytest.raises(Refusal) as refused:
            provision_email(factory, prepare_request("other@example.com"))

        assert refused.value.reason is RefusalReason.USER_EXISTS
        assert engine.pool.checkedin() == 1  # the connection is open, in the pool
        assert scalar(monitor, "SELECT count(*) FROM users") == 1
        assert scalar(monitor, "SELECT subject FROM allowlist_entries") == ADDRESS
        assert_the_lock_is_free(monitor)
    finally:
        engine.dispose()


def test_a_failure_after_the_writes_rolls_everything_back_and_releases_the_advisory_lock(
    scratch, monitor, monkeypatch
):
    fail_after(monkeypatch, RuntimeError("boom"))  # the user and the entry have been flushed
    engine = scratch.engine()
    try:
        with pytest.raises(RuntimeError):
            provision_email(create_session_factory(engine), prepare_request(ADDRESS))

        assert engine.pool.checkedin() == 1  # the connection is open, in the pool
        assert scalar(monitor, "SELECT count(*) FROM users") == 0
        assert scalar(monitor, "SELECT count(*) FROM allowlist_entries") == 0
        assert_the_lock_is_free(monitor)
    finally:
        engine.dispose()


# --- the command line, end to end ---------------------------------------------------------------------


def test_the_command_line_provisions_the_first_user_and_repeats_safely(scratch, monitor, capsys):
    # Handed to main() as its environment: the process environment is not read.
    environ = {DATABASE_URL_ENV: scratch.url_text}

    assert main(["provision-email", "--email", ADDRESS], environ=environ) == 0
    created = capsys.readouterr()
    assert main(["provision-email", "--email", ADDRESS], environ=environ) == 0
    unchanged = capsys.readouterr()
    assert main(["provision-email", "--email", "other@example.com"], environ=environ) == 3
    refused = capsys.readouterr()

    assert created.out.startswith("ok [created]: ") and created.err == ""
    assert unchanged.out.startswith("ok [unchanged]: ")
    assert refused.err.startswith("refused [user_exists]: ") and refused.out == ""
    for output in (created, unchanged, refused):  # the URL is never printed
        # (an assertion on NAMES: a failure shows which secret leaked, never its value;
        # a URL without a password, or with an empty one, has no password to look for)
        assert leaked(scratch.url, output.out + output.err) == []
    assert scalar(monitor, "SELECT count(*) FROM users") == 1
    assert_the_lock_is_free(monitor)
