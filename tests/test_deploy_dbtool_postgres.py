"""deploy/ops/dbtool.py on PostgreSQL (HH-DB-01 Stage 2 WP-2A).

The offline tests run everywhere: the configuration rules, the explicit refusal of the
SQLite backup/restore commands on a PostgreSQL URL, and the lock location (the state
volume for PostgreSQL, the data directory for SQLite, unchanged). The live tests need the
disposable server of compose.test-postgres.yaml and the safety preflight the other opt-in
suites use:

    docker compose -f compose.test-postgres.yaml up -d
    python -m tests.postgres_preflight
    export HOUSE_HELPER_TEST_POSTGRES_URL=postgresql+psycopg://hh_test:hh_test_only_not_a_secret@127.0.0.1:55432/postgres
    pytest tests/test_deploy_dbtool_postgres.py -m postgres -v

The harness creates the per-run roles and databases, so the application login is
`hh_t_<token>_app`, not `hh_app`: the live tests monkeypatch `dbtool.APP_ROLE` to the
run's own name. That is the only seam; the production default is the frozen `hh_app`.
Nothing here prints a URL or a password, and every assertion on a message checks the
fixed words the tool prints.
"""

import os

import pytest
import sqlalchemy as sa

from house_helper.persistence.database import upgrade_database
from tests import postgres_harness as harness
from tests.deploy_support import HEAD, URL_ENV, dbtool, make_head_database, run
from tests.postgres_harness import POSTGRES_URL_ENV

STATE_ENV = "HOUSE_HELPER_STATE_DIR"
LOCK = dbtool.LOCK_FILE_NAME

POSTGRES_URL = "postgresql+psycopg://hh_app:hunter2-password@db.example:5432/house_helper"

live = pytest.mark.postgres
needs_server = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL (see the module docstring)",
)


@pytest.fixture
def state_volume(tmp_path):
    path = tmp_path / "state"
    path.mkdir()
    return path


# --- offline: configuration -----------------------------------------------------------------


def test_check_config_for_postgresql_needs_the_state_volume(tmp_path, capsys):
    missing = run(capsys, ["check-config"], {URL_ENV: POSTGRES_URL})
    assert missing.code == 3 and "[state_dir_not_configured]" in missing.err
    assert "hunter2" not in missing.err + missing.out

    state = tmp_path / "state"
    state.mkdir()
    accepted = run(capsys, ["check-config"], {URL_ENV: POSTGRES_URL, STATE_ENV: str(state)})
    assert accepted.code == 0
    assert "house_helper" in accepted.out and str(state) in accepted.out
    assert "hunter2" not in accepted.err + accepted.out


@pytest.mark.parametrize(
    "url, code",
    [
        # the default driver is psycopg2, which this image does not pin (ST-02)
        ("postgresql://hh_app@db.example/house_helper", "driver_missing"),
        ("postgresql+psycopg://db.example/house_helper", "invalid_url"),  # no login
        ("postgresql+psycopg://hh_app@/house_helper", "invalid_url"),  # no host
        ("postgresql+psycopg://hh_app@db.example", "invalid_url"),  # no database
    ],
)
def test_a_postgresql_url_must_name_the_driver_the_login_the_host_and_the_database(
    url, code, state_volume, capsys
):
    result = run(capsys, ["check-config"], {URL_ENV: url, STATE_ENV: str(state_volume)})
    assert result.code == 3 and f"[{code}]" in result.err
    assert "hunter2" not in result.err + result.out


def test_the_state_volume_must_be_an_absolute_existing_directory(tmp_path, capsys):
    relative = run(capsys, ["check-config"], {URL_ENV: POSTGRES_URL, STATE_ENV: "state"})
    assert relative.code == 3 and "[state_dir_unusable]" in relative.err

    absent = run(
        capsys, ["check-config"], {URL_ENV: POSTGRES_URL, STATE_ENV: str(tmp_path / "absent")}
    )
    assert absent.code == 3 and "[state_dir_unusable]" in absent.err


def test_the_sqlite_backup_and_restore_commands_refuse_a_postgresql_url_explicitly(capsys):
    # WP-2A: PostgreSQL backup/restore is WP-2D. The refusal must be explicit, never a
    # silent attempt to treat the URL as a SQLite file.
    for argv in (
        ["backup", "--output", "-"],
        ["restore", "--input", "-"],
    ):
        result = run(capsys, argv, {URL_ENV: POSTGRES_URL})
        assert result.code == 3 and "[postgresql_not_implemented]" in result.err
        assert result.data == b""
        assert "hunter2" not in result.err + result.out


def test_the_postgresql_commands_do_not_start_a_program_without_a_state_volume(monkeypatch, capsys):
    started = []
    monkeypatch.setattr(dbtool, "_exec", started.append)
    for argv in (
        ["serve", "--", "uvicorn"],
        ["exec", "--shared", "--", "python", "-c", "print('ran')"],
        ["check-idle"],
    ):
        result = run(capsys, argv, {URL_ENV: POSTGRES_URL})
        assert result.code == 3 and "[state_dir_not_configured]" in result.err
    assert started == []  # nothing was executed, and no database was opened


def test_the_lock_lives_on_the_state_volume_for_postgresql_and_beside_the_file_for_sqlite(
    tmp_path, state_volume
):
    from sqlalchemy.engine import make_url

    postgres = dbtool.maintenance_lock(
        {STATE_ENV: str(state_volume)},
        make_url(POSTGRES_URL),
        exclusive=False,
        purpose="the web service",
    )
    assert postgres.path == state_volume / LOCK

    data = tmp_path / "data"
    data.mkdir()
    database = make_head_database(data / "house_helper.db")
    sqlite = dbtool.maintenance_lock(
        {URL_ENV: f"sqlite:///{database}", STATE_ENV: str(state_volume)},
        make_url(f"sqlite:///{database}"),
        exclusive=False,
        purpose="the web service",
    )
    assert sqlite.path == database.with_name(LOCK)  # unchanged: the state volume is ignored


def test_an_sqlite_start_still_locks_the_data_directory_when_a_state_volume_is_set(
    tmp_path, state_volume, monkeypatch, capsys
):
    data = tmp_path / "data"
    data.mkdir()
    database = make_head_database(data / "house_helper.db")
    started = []
    monkeypatch.setattr(dbtool, "_exec", started.append)
    result = run(
        capsys,
        ["serve", "--", "uvicorn"],
        {URL_ENV: f"sqlite:///{database}", STATE_ENV: str(state_volume)},
    )
    assert result.code == 0 and started == [["uvicorn"]]
    assert (data / LOCK).exists()  # the SQLite location, as before
    assert not (state_volume / LOCK).exists()


def test_an_sqlite_start_never_runs_the_postgresql_privilege_check(tmp_path, monkeypatch, capsys):
    # WP-2A R1: the database-CREATE rule is part of the PostgreSQL start gate only. The
    # SQLite path must not consult it (the check needs a PostgreSQL catalog).
    data = tmp_path / "data"
    data.mkdir()
    database = make_head_database(data / "house_helper.db")

    def forbidden(connection):
        raise AssertionError("the PostgreSQL privilege check must not run for SQLite")

    monkeypatch.setattr(dbtool, "_app_privilege_problems", forbidden)
    started = []
    monkeypatch.setattr(dbtool, "_exec", started.append)
    result = run(capsys, ["serve", "--", "uvicorn"], {URL_ENV: f"sqlite:///{database}"})
    assert result.code == 0, result.err
    assert started == [["uvicorn"]]


# --- live: the start gate on a real PostgreSQL 16 server -------------------------------------


@pytest.fixture(scope="module")
def pg_run():
    run_ = harness.run_from_environment()
    try:
        run_.prepare_roles()
        yield run_
    finally:
        run_.close()


def provisioned(run_, name):
    """Migrate `name` to head and apply the frozen ACL matrix, both as the owner."""
    engine = run_.migrate_engine(name)
    try:
        upgrade_database(engine, HEAD)
    finally:
        engine.dispose()
    engine = run_.migrate_engine(name)
    try:
        run_.apply_privileges(engine)
    finally:
        engine.dispose()


@pytest.fixture
def database(pg_run):
    name = pg_run.create_database()
    provisioned(pg_run, name)
    yield pg_run, name
    pg_run.drop_database(name)


@pytest.fixture
def environment(database, state_volume):
    run_, name = database
    url = run_.url(name, "app")
    return {
        URL_ENV: url.render_as_string(hide_password=False),
        STATE_ENV: str(state_volume),
    }, url


@live
@needs_server
def test_the_start_gate_accepts_a_provisioned_database_and_a_real_application_session(
    environment, monkeypatch, capsys
):
    env, url = environment
    monkeypatch.setattr(dbtool, "APP_ROLE", url.username)

    config = run(capsys, ["check-config"], env)
    assert config.code == 0, config.err
    assert (url.username) in config.out and "house_helper_test_" in config.out

    schema = run(capsys, ["check-schema"], env)
    assert schema.code == 0, schema.err
    assert HEAD in schema.out
    assert "application_name hh_web" in schema.out and "search_path public" in schema.out
    assert url.password not in schema.out + schema.err + config.out + config.err


@live
@needs_server
def test_the_start_gate_refuses_a_session_that_is_not_the_application_role(
    environment, monkeypatch, capsys
):
    env, _ = environment
    monkeypatch.setattr(dbtool, "APP_ROLE", "hh_app")  # the run's login has another name
    result = run(capsys, ["serve", "--", "uvicorn"], env)
    assert result.code == 3 and "[app_role]" in result.err


@live
@needs_server
def test_the_start_gate_refuses_a_provisioning_session_whose_settings_are_not_ROL_17(
    database, state_volume, monkeypatch, capsys
):
    run_, name = database
    env = {
        URL_ENV: run_.url(name, "provision").render_as_string(hide_password=False),
        STATE_ENV: str(state_volume),
    }
    monkeypatch.setattr(dbtool, "APP_ROLE", run_.identity("provision").name)
    result = run(capsys, ["check-schema"], env)
    assert result.code == 3 and "[app_session_settings]" in result.err
    assert "not public" in result.err  # the provisioning login has no ROL-17 search_path


@live
@needs_server
def test_the_start_gate_refuses_a_privilege_the_contract_does_not_give(
    database, state_volume, monkeypatch, capsys
):
    run_, name = database
    app = run_.identity("app").name
    engine = run_.migrate_engine(name)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'REVOKE DELETE ON messages FROM "{app}"')
    finally:
        engine.dispose()
    monkeypatch.setattr(dbtool, "APP_ROLE", app)
    env = {
        URL_ENV: run_.url(name, "app").render_as_string(hide_password=False),
        STATE_ENV: str(state_volume),
    }
    result = run(capsys, ["check-schema"], env)
    assert result.code == 3 and "[app_privileges]" in result.err
    assert "messages delete is False" in result.err


@live
@needs_server
def test_the_start_gate_refuses_create_on_the_database_before_the_program_starts(
    database, state_volume, monkeypatch, capsys
):
    # WP-2A R1: database-level CREATE is schema-creation authority even without CREATE on
    # schema public (§3.2/§3.6), so an otherwise valid runtime login that holds it must be
    # refused before `_exec`. Removing the grant restores the same login's acceptance.
    run_, name = database
    app = run_.identity("app").name
    engine = run_.migrate_engine(name)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'GRANT CREATE ON DATABASE "{name}" TO "{app}"')
    finally:
        engine.dispose()
    monkeypatch.setattr(dbtool, "APP_ROLE", app)
    env = {
        URL_ENV: run_.url(name, "app").render_as_string(hide_password=False),
        STATE_ENV: str(state_volume),
    }
    started = []
    monkeypatch.setattr(dbtool, "_exec", started.append)

    refused = run(capsys, ["serve", "--", "uvicorn"], env)
    assert refused.code == 3 and "[app_privileges]" in refused.err
    assert "may create schemas in this database" in refused.err
    assert started == []  # the refusal precedes application startup

    engine = run_.migrate_engine(name)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'REVOKE CREATE ON DATABASE "{name}" FROM "{app}"')
    finally:
        engine.dispose()
    accepted = run(capsys, ["serve", "--", "uvicorn"], env)
    assert accepted.code == 0, accepted.err
    assert started == [["uvicorn"]]


@live
@needs_server
def test_the_start_gate_sees_create_on_the_database_granted_to_public(
    database, state_volume, monkeypatch, capsys
):
    # The check is PostgreSQL's effective one: a privilege that reaches the login through
    # PUBLIC (no direct grant on the role) is not overlooked.
    run_, name = database
    app = run_.identity("app").name
    engine = run_.migrate_engine(name)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'GRANT CREATE ON DATABASE "{name}" TO PUBLIC')
    finally:
        engine.dispose()
    monkeypatch.setattr(dbtool, "APP_ROLE", app)
    env = {
        URL_ENV: run_.url(name, "app").render_as_string(hide_password=False),
        STATE_ENV: str(state_volume),
    }
    started = []
    monkeypatch.setattr(dbtool, "_exec", started.append)

    refused = run(capsys, ["serve", "--", "uvicorn"], env)
    assert refused.code == 3 and "[app_privileges]" in refused.err
    assert "may create schemas in this database" in refused.err
    assert started == []

    engine = run_.migrate_engine(name)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'REVOKE CREATE ON DATABASE "{name}" FROM PUBLIC')
    finally:
        engine.dispose()
    accepted = run(capsys, ["serve", "--", "uvicorn"], env)
    assert accepted.code == 0, accepted.err
    assert started == [["uvicorn"]]


@live
@needs_server
def test_the_start_gate_refuses_a_database_behind_this_images_head(
    pg_run, state_volume, monkeypatch, capsys
):
    name = pg_run.create_database()
    try:
        engine = pg_run.migrate_engine(name)
        try:
            upgrade_database(engine, "0005_identity_invitations")
        finally:
            engine.dispose()
        engine = pg_run.migrate_engine(name)
        try:
            pg_run.apply_privileges(engine)
        finally:
            engine.dispose()
        monkeypatch.setattr(dbtool, "APP_ROLE", pg_run.identity("app").name)
        env = {
            URL_ENV: pg_run.url(name, "app").render_as_string(hide_password=False),
            STATE_ENV: str(state_volume),
        }
        result = run(capsys, ["serve", "--", "uvicorn"], env)
        assert result.code == 4 and "[schema_not_head]" in result.err
    finally:
        pg_run.drop_database(name)


@live
@needs_server
def test_serve_checks_the_session_then_hands_the_state_volume_lock_to_the_program(
    environment, state_volume, monkeypatch, capsys
):
    env, url = environment
    monkeypatch.setattr(dbtool, "APP_ROLE", url.username)
    started = []
    monkeypatch.setattr(dbtool, "_exec", started.append)
    result = run(capsys, ["serve", "--", "uvicorn"], env)
    assert result.code == 0, result.err
    assert started == [["uvicorn"]]
    assert "[check_schema]" in result.out
    assert (state_volume / LOCK).exists()
    # The hook returned instead of exec()ing, so serve released the lock it took.
    with dbtool.MaintenanceLock(state_volume / LOCK, exclusive=True, purpose="the test"):
        pass


@live
@needs_server
def test_an_exclusive_command_excludes_a_serve_on_the_same_state_volume(
    environment, state_volume, monkeypatch, capsys
):
    env, url = environment
    monkeypatch.setattr(dbtool, "APP_ROLE", url.username)
    started = []
    monkeypatch.setattr(dbtool, "_exec", started.append)
    from sqlalchemy.engine import make_url

    with dbtool.MaintenanceLock(
        state_volume / LOCK, exclusive=True, purpose="a migration", location="state volume"
    ):
        refused = run(capsys, ["exec", "--shared", "--", "python", "-c", "print('ran')"], env)
    assert refused.code == 3 and "[maintenance_lock_busy]" in refused.err
    assert started == []
    # and the lock is free once the exclusive holder ended
    lock = dbtool.maintenance_lock(
        env, make_url(env[URL_ENV]), exclusive=False, purpose="the web service"
    )
    lock.acquire()
    lock.release()


@live
@needs_server
def test_check_idle_sees_another_session_and_is_quiet_once_it_closes(environment, capsys):
    env, url = environment
    holder = sa.create_engine(env[URL_ENV])
    connection = holder.connect()
    try:
        busy = run(capsys, ["check-idle"], env)
        assert busy.code == 3 and "[database_in_use]" in busy.err
    finally:
        connection.rollback()
        connection.close()
        holder.dispose()
    quiet = run(capsys, ["check-idle"], env)
    assert quiet.code == 0, quiet.err
