"""HH-DB-01 Stage 2 WP-1C-C - the ordered initializer (tests/postgres_initializer.py).

Two layers, deliberately separated:

* the ORDER and the REFUSALS are tested without a server, with fakes for the accepted
  collaborators. The fakes record what was called in which order, so the frozen sequence
  (observe -> roles -> E3 fence -> re-observe E3-c -> target verification -> schema ->
  chain -> privileges -> fence) and every fail-closed refusal are asserted deterministically.
* the LIFECYCLE is integration-tested against the SAME disposable PostgreSQL 16 cluster of
  compose.test-postgres.yaml that WP-1B/WP-1C-A/WP-1C-B use, through the same independent
  identity and safety boundary (tests/postgres_harness.py): the seven literal project roles
  are provisioned once by the accepted WP-1B fixture, and each test creates and drops the
  serving database `house_helper` it uses.

The live tests exercise every E4 row the specification permits initialization from
(BOOT-07 rows 1-4), the refusals (row 5 at the maintenance half and at the target half),
the non-authorizing nature of a connection that is not the grantee's own session, the
chain's transactional rollback on an interruption, the fail-closed re-entry that follows
(BOOT-07 row 5 / RBK-08(a)), and the acceptance facts of a completed initialization
(BOOT-05, BOOT-09 steps 3-4, BOOT-10, BOOT-15).

No Production, n8n or ETL service is contacted.

Opt-in, like the other PostgreSQL suites:

    docker compose -f compose.test-postgres.yaml up -d
    python -m tests.postgres_preflight
    HOUSE_HELPER_TEST_POSTGRES_URL=postgresql+psycopg://hh_test:hh_test_only_not_a_secret@127.0.0.1:55432/postgres \\
        pytest tests/test_postgres_initializer.py -v -ra
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import psycopg.pq
import pytest
import sqlalchemy as sa

from tests import postgres_e4 as e4
from tests import postgres_initializer as initializer_module
from tests import postgres_sql as artifacts
from tests import test_postgres_sql_artifacts as artifact_tests
from tests.postgres_harness import (
    POSTGRES_URL_ENV,
    RoleIdentity,
    run_from_environment,
)
from tests.postgres_initializer import InitializationError, Initializer
from tests.revisions import HEAD_REVISION

SERVING = artifacts.SERVING_DATABASE

LIVE = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_URL_ENV),
        reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL "
        "(see tests/test_admin_postgres.py)",
    ),
]


# =============================================================================================
# Offline: the order and the refusals
# =============================================================================================

# The collaborator calls the initializer makes, as the fake collaborators record them.
# Server-side SQL is not in this list: the accepted artifacts own it and are tested where
# they live (tests/test_postgres_sql_artifacts.py).


class _Reply:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False


class _FakeControl:
    """Answers only the two read-only lookups the initializer itself issues, and records
    the statements the accepted bootstrap phases would have sent."""

    def __init__(self, events, *, exists=False, roles_missing=()):
        self.events = events
        self.exists = exists
        self.roles_missing = frozenset(roles_missing)

    def execute(self, statement, params=None):
        text = " ".join(str(statement).split())
        if "FROM pg_catalog.pg_database WHERE datname" in text:
            return _Reply((1,) if self.exists else None)
        if "FROM pg_catalog.pg_roles WHERE rolname" in text:
            return _Reply(None if params[0] in self.roles_missing else (1,))
        if text.startswith("REVOKE ALL ON DATABASE"):
            self.events.append("harden:revoke")
            if "GRANT CONNECT ON DATABASE" in text:  # the phase is one query message
                self.events.append("harden:grant")
        elif text.startswith("GRANT CONNECT ON DATABASE"):
            self.events.append("harden:grant")
        elif text.startswith("ALTER DATABASE"):
            self.events.append("enable")
        else:  # pragma: no cover - the initializer issues no other statement
            raise AssertionError(f"unexpected statement: {text}")
        return _Reply(None)

    def transaction(self):
        return _Transaction()


class _FakeRoles:
    def __init__(self, events):
        self.events = events

    def apply(self, connection, *, claim=None):
        self.events.append("roles")
        return artifacts.RoleApplyResult(
            created=frozenset(), existing=frozenset(artifacts.PROJECT_ROLE_NAMES)
        )


class _FakeBootstrap:
    def __init__(self, events):
        self.events = events
        self.created = []

    def run(self, control, *, database, grantee, after_harden=None):
        self.events.append("bootstrap:create+harden+enable")
        self.created.append(database)

    def verify_disabled(self, control, database, grantee):
        self.events.append("verify:e3_b")

    def verify_enabled(self, control, database, grantee):
        self.events.append("verify:e3_c")

    def connect_matrix(self, control, database):
        self.events.append("fence:matrix")
        return {
            "hh_owner": True,
            "hh_migrate": True,
            "hh_app": False,
            "hh_purge": False,
            "hh_backup": False,
            "hh_provision": False,
            "hh_restore": False,
        }

    def public_entries(self, control, database):
        self.events.append("fence:public")
        return 0


class _FakeSchema:
    def __init__(self, events):
        self.events = events

    def apply(self, connection, *, database):
        self.events.append("schema")


class _FakePrivileges:
    def __init__(self, events):
        self.events = events

    def apply(self, connection, *, database):
        self.events.append("privileges")


class _FakeEngine:
    def __init__(self, events):
        self.events = events

    def dispose(self):
        self.events.append("engine:dispose")


class _Verdicts:
    """A classify collaborator returning canned verdicts in order and recording the call."""

    def __init__(self, events, *verdicts):
        self.events = events
        self._verdicts = list(verdicts)

    def __call__(self, *, control, database, grantee, target=None):
        if not self._verdicts:
            raise AssertionError("the initializer observed more states than the test planned")
        planned = self._verdicts.pop(0)
        if isinstance(planned, e4.E4Verdict):
            self.events.append(("classify", planned.state, target is not None))
            return planned
        state, action, tables_checked = planned
        self.events.append(("classify", state, target is not None))
        return e4.E4Verdict(
            state=state,
            action=action,
            database=database,
            grantee=grantee,
            tables_checked=tables_checked,
        )


def _maintenance(state, action, *, tables_checked=False):
    return (state, action, tables_checked)


ABSENT = (e4.E4_ABSENT, e4.ACTION_CREATE_DATABASE, False)
UNHARDENED = (e4.E4_DISABLED_UNHARDENED, e4.ACTION_HARDEN_DATABASE_ACL, False)
HARDENED = (e4.E4_DISABLED_HARDENED, e4.ACTION_ENABLE_CONNECTIONS, False)
ENABLED = (e4.E4_ENABLED_MAINTENANCE_ONLY, e4.ACTION_VERIFY_TARGET_OBJECTS, False)
AUTHORIZED = (e4.E4_ENABLED_MAINTENANCE_ONLY, e4.ACTION_INITIALIZE_SCHEMA, True)


def _initializer(events, *verdicts, **options):
    """An initializer whose collaborators are all fakes; only the accepted script DEFAULTS
    are replaced, so the test exercises this module's order and not the artifacts."""
    control_options = options.pop("control", {})

    def control():
        return _context(_FakeControl(events, **control_options))

    def connect_target(database, grantee, password):
        return _target(events)

    def migrate_engine(database, grantee, password):
        events.append("engine:create")
        return _FakeEngine(events)

    return Initializer(
        control=control,
        connect_target=connect_target,
        migrate_engine=migrate_engine,
        classify=_Verdicts(events, *verdicts),
        roles=_FakeRoles(events),
        bootstrap=_FakeBootstrap(events),
        schema=_FakeSchema(events),
        privileges=_FakePrivileges(events),
        upgrade=lambda engine, database: events.append("upgrade"),
        verify_initialized=lambda engine, database: events.append("verify_initialized")
        or HEAD_REVISION,
        **options,
    )


@contextmanager
def _context(value):
    yield value


@contextmanager
def _target(events):
    events.append("target:open")
    try:
        yield object()
    finally:
        events.append("target:close")


def test_the_absent_state_runs_the_frozen_order():
    events = []
    initializer = _initializer(events, ABSENT, ABSENT, ENABLED, AUTHORIZED)
    report = initializer.initialize(SERVING, "hh_migrate", "pw")
    assert events == [
        ("classify", e4.E4_ABSENT, False),
        "roles",
        ("classify", e4.E4_ABSENT, False),
        "bootstrap:create+harden+enable",
        ("classify", e4.E4_ENABLED_MAINTENANCE_ONLY, False),
        "target:open",
        ("classify", e4.E4_ENABLED_MAINTENANCE_ONLY, True),
        "schema",
        "target:close",
        "engine:create",
        "upgrade",
        "verify_initialized",
        "engine:dispose",
        "target:open",
        "privileges",
        "target:close",
        "fence:matrix",
        "fence:public",
    ]
    assert report.entry_state == e4.E4_ABSENT
    assert report.maintenance_action == e4.ACTION_CREATE_DATABASE
    assert report.authorization_action == e4.ACTION_INITIALIZE_SCHEMA
    assert report.created is True
    assert report.actions == (
        "observe:absent",
        "roles",
        "create_database",
        "harden_database_acl",
        "enable_connections",
        "observe:enabled_maintenance_only",
        "verify_target_objects",
        "schema_contract",
        "alembic_upgrade_head",
        "verify_initialized",
        "privileges",
        "verify_fence",
    )


def test_the_disabled_unhardened_state_finishes_hardening_then_enables():
    events = []
    initializer = _initializer(
        events, UNHARDENED, UNHARDENED, UNHARDENED, ENABLED, AUTHORIZED,
        control={"exists": True},
    )
    report = initializer.initialize(SERVING, "hh_migrate", "pw")
    assert events == [
        ("classify", e4.E4_DISABLED_UNHARDENED, False),
        "roles",
        ("classify", e4.E4_DISABLED_UNHARDENED, False),
        ("classify", e4.E4_DISABLED_UNHARDENED, False),
        "harden:revoke",
        "harden:grant",
        "verify:e3_b",
        "enable",
        "verify:e3_c",
        ("classify", e4.E4_ENABLED_MAINTENANCE_ONLY, False),
        "target:open",
        ("classify", e4.E4_ENABLED_MAINTENANCE_ONLY, True),
        "schema",
        "target:close",
        "engine:create",
        "upgrade",
        "verify_initialized",
        "engine:dispose",
        "target:open",
        "privileges",
        "target:close",
        "fence:matrix",
        "fence:public",
    ]
    assert report.entry_state == e4.E4_DISABLED_UNHARDENED
    assert report.actions[:4] == (
        "observe:disabled_unhardened",
        "roles",
        "harden_database_acl",
        "enable_connections",
    )
    assert report.created is False


def test_the_disabled_hardened_state_enables_only_after_the_e3_b_gate():
    events = []
    initializer = _initializer(
        events, HARDENED, HARDENED, HARDENED, ENABLED, AUTHORIZED,
        control={"exists": True},
    )
    report = initializer.initialize(SERVING, "hh_migrate", "pw")
    assert events[3:8] == [
        ("classify", e4.E4_DISABLED_HARDENED, False),
        "verify:e3_b",
        "enable",
        "verify:e3_c",
        ("classify", e4.E4_ENABLED_MAINTENANCE_ONLY, False),
    ]
    assert "harden:revoke" not in events and "harden:grant" not in events
    assert report.actions[:3] == (
        "observe:disabled_hardened",
        "roles",
        "enable_connections",
    )


def test_the_enabled_maintenance_only_state_mutates_nothing_before_the_target_gate():
    events = []
    initializer = _initializer(events, ENABLED, ENABLED, ENABLED, AUTHORIZED, control={"exists": True})
    report = initializer.initialize(SERVING, "hh_migrate", "pw")
    assert events[:6] == [
        ("classify", e4.E4_ENABLED_MAINTENANCE_ONLY, False),
        "roles",
        ("classify", e4.E4_ENABLED_MAINTENANCE_ONLY, False),
        ("classify", e4.E4_ENABLED_MAINTENANCE_ONLY, False),
        "target:open",
        ("classify", e4.E4_ENABLED_MAINTENANCE_ONLY, True),
    ]
    assert "enable" not in events and "harden:revoke" not in events
    assert report.entry_state == e4.E4_ENABLED_MAINTENANCE_ONLY


def test_the_maintenance_verdict_verify_target_objects_is_not_an_authorization():
    """The row-4 action only asks for the target observation. A target observation that
    is still not `may_initialize_schema` leaves the schema untouched."""
    events = []
    initializer = _initializer(events, ENABLED, ENABLED, ENABLED, ENABLED, control={"exists": True})
    with pytest.raises(InitializationError) as refused:
        initializer.initialize(SERVING, "hh_migrate", "pw")
    assert refused.value.code == initializer_module.REFUSE_NOT_AUTHORIZED
    assert "schema" not in events
    assert "engine:create" not in events
    assert events[-1] == "target:close"


@pytest.mark.parametrize(
    "failure",
    [
        e4.FAIL_SUBSCRIPTIONS,
        e4.FAIL_STALE_OBSERVATION,
        e4.FAIL_UNTRUSTED_ENVIRONMENT,
        e4.FAIL_INCONSISTENT_PRIVILEGE,
        e4.FAIL_SESSIONS,
        e4.FAIL_TABLES,
    ],
)
def test_a_fail_closed_maintenance_verdict_is_refused_before_any_mutation(failure):
    """An unexpected subscription, a stale or untrusted observation and an inconsistent
    privilege observation are failure codes like any other: none becomes authorization."""
    events = []
    verdict = e4.E4Verdict(
        state=e4.E4_FAIL_CLOSED,
        action=e4.ACTION_REMEDIATE,
        database=SERVING,
        grantee="hh_migrate",
        failures=(failure,),
    )
    initializer = _initializer(events, [], control={"exists": True})
    initializer.classify = lambda **kwargs: (events.append("classify"), verdict)[1]
    with pytest.raises(InitializationError) as refused:
        initializer.initialize(SERVING, "hh_migrate", "pw")
    assert refused.value.code == initializer_module.REFUSE_E4_FAIL_CLOSED
    assert failure in refused.value.detail
    assert events == ["classify"]


def test_a_fail_closed_target_verdict_is_refused_after_the_observation():
    events = []
    verdict = e4.E4Verdict(
        state=e4.E4_FAIL_CLOSED,
        action=e4.ACTION_REMEDIATE,
        database=SERVING,
        grantee="hh_migrate",
        failures=(e4.FAIL_TABLES,),
        tables_checked=True,
    )
    initializer = _initializer(events, ENABLED, ENABLED, ENABLED, verdict, control={"exists": True})
    with pytest.raises(InitializationError) as refused:
        initializer.initialize(SERVING, "hh_migrate", "pw")
    assert refused.value.code == initializer_module.REFUSE_E4_FAIL_CLOSED
    assert e4.FAIL_TABLES in refused.value.detail
    assert "schema" not in events


def test_a_state_that_changes_before_the_mutation_is_refused():
    events = []
    initializer = _initializer(
        events,
        HARDENED,
        HARDENED,
        (e4.E4_FAIL_CLOSED, e4.ACTION_REMEDIATE, False),
        control={"exists": True},
    )
    with pytest.raises(InitializationError) as refused:
        initializer.initialize(SERVING, "hh_migrate", "pw")
    assert refused.value.code == initializer_module.REFUSE_E4_FAIL_CLOSED
    assert "enable" not in events


def test_an_existing_database_without_the_project_roles_is_refused_before_classification():
    events = []
    initializer = _initializer(
        events, [], control={"exists": True, "roles_missing": ("hh_backup",)}
    )
    with pytest.raises(InitializationError) as refused:
        initializer.initialize(SERVING, "hh_migrate", "pw")
    assert refused.value.code == initializer_module.REFUSE_ROLES_MISSING
    assert refused.value.detail == "hh_backup"
    assert events == []


def test_a_restore_database_is_refused_before_any_statement():
    events = []
    initializer = _initializer(events, [])
    with pytest.raises(InitializationError) as refused:
        initializer.initialize("house_helper_restore_abc123", "hh_restore", "pw")
    assert refused.value.code == initializer_module.REFUSE_RESTORE_DATABASE
    assert events == []


def test_a_wrong_grantee_is_refused_before_any_statement():
    events = []
    initializer = _initializer(events, [])
    with pytest.raises(artifacts.ArtifactError) as refused:
        initializer.initialize(SERVING, "hh_restore", "pw")
    assert refused.value.code == "grantee_database"
    assert events == []


def test_an_empty_credential_is_refused_before_any_statement():
    events = []
    initializer = _initializer(events, [])
    with pytest.raises(InitializationError) as refused:
        initializer.initialize(SERVING, "hh_migrate", "")
    assert refused.value.code == initializer_module.REFUSE_CREDENTIAL
    assert events == []


# =============================================================================================
# Offline: the ROL-06 contract on the Alembic connections
# =============================================================================================

# The exact read-back row the contract requires for this phase (ROL-06 / §8.8's migration
# pair). Every field is one the helper must check; the parameterized tests below change one
# at a time and assert the refusal, so no field can silently pass unverified.


def _session_row(**overrides):
    row = {
        "session": "hh_migrate",
        "current": "hh_owner",
        "database": SERVING,
        "search_path": "public",
        "application_name": "hh_migrate",
        "statement_timeout": "0",
        "public": "public",
    }
    row.update(overrides)
    return (
        row["session"],
        row["current"],
        row["database"],
        row["search_path"],
        row["application_name"],
        row["statement_timeout"],
        row["public"],
    )


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _SessionConnection:
    """A fake Alembic connection: it records every statement the contract sends and answers
    the read-back (and the acceptance queries) from canned rows, so the ORDER, the refusal
    and the transaction boundary are all assertable without a server."""

    def __init__(self, events, *, session_row, versions=(HEAD_REVISION,)):
        self.events = events
        self.session_row = session_row
        self.versions = versions

    def exec_driver_sql(self, statement, params=None):
        text = " ".join(str(statement).split())
        if text.startswith("SELECT session_user, current_user"):
            self.events.append("verify:session")
            return _Rows([self.session_row])
        if text.startswith("SELECT version_num"):
            self.events.append("read:version")
            return _Rows((version,) for version in self.versions)
        if text.startswith("SELECT c.relname, c.relkind"):
            self.events.append("read:ownership")
            return _Rows([("users", "r", "hh_owner")])
        if text.startswith("SELECT last_value"):
            self.events.append("read:sequence")
            return _Rows([(1, False)])
        self.events.append(text)
        return _Rows([])


class _SessionEngine:
    """The `engine.begin()` / `engine.connect()` shapes the two Alembic paths use; a failed
    body is recorded as a rollback, so transaction ownership is visible in the events."""

    def __init__(self, events, connection):
        self.events = events
        self.connection = connection

    @contextmanager
    def begin(self):
        self.events.append("begin")
        try:
            yield self.connection
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    @contextmanager
    def connect(self):
        yield self.connection


def test_the_chain_gets_the_session_contract_before_alembic_runs(monkeypatch):
    events = []
    engine = _SessionEngine(events, _SessionConnection(events, session_row=_session_row()))
    monkeypatch.setattr(
        initializer_module.command,
        "upgrade",
        lambda config, revision: events.append(("alembic:upgrade", revision)),
    )
    initializer_module._upgrade_to_head(engine, SERVING)
    assert events == [
        "begin",
        "SET search_path = public",
        "SET application_name = 'hh_migrate'",
        "SET statement_timeout = 0",
        "verify:session",
        ("alembic:upgrade", HEAD_REVISION),
        "commit",
    ]


def test_the_check_gets_the_session_contract_before_the_comparison(monkeypatch):
    events = []
    engine = _SessionEngine(events, _SessionConnection(events, session_row=_session_row()))
    monkeypatch.setattr(
        initializer_module.command, "check", lambda config: events.append("alembic:check")
    )
    assert initializer_module.verify_initialized_database(engine, SERVING) == HEAD_REVISION
    assert events == [
        "SET search_path = public",
        "SET application_name = 'hh_migrate'",
        "SET statement_timeout = 0",
        "verify:session",
        "read:version",
        "alembic:check",
        "read:ownership",
        "read:sequence",
    ]


@pytest.mark.parametrize(
    "session_row",
    [
        _session_row(session="hh_restore"),
        _session_row(current="hh_migrate"),
        _session_row(database="postgres"),
        _session_row(search_path='"$user", public'),
        _session_row(application_name="hh_app"),
        _session_row(statement_timeout="5000"),
        _session_row(public="other"),
    ],
    ids=[
        "session_user",
        "current_user",
        "current_database",
        "search_path",
        "application_name",
        "statement_timeout",
        "public_schema",
    ],
)
def test_a_session_that_did_not_take_the_contract_is_refused_before_the_chain(
    session_row, monkeypatch
):
    """The `SET`s are not trusted: the read-back is what authorizes the chain. A session
    that silently kept a different value is refused and its transaction rolled back."""
    events = []
    engine = _SessionEngine(events, _SessionConnection(events, session_row=session_row))
    monkeypatch.setattr(
        initializer_module.command,
        "upgrade",
        lambda config, revision: pytest.fail(
            "the chain must not run on a session that failed the contract"
        ),
    )
    with pytest.raises(InitializationError) as refused:
        initializer_module._upgrade_to_head(engine, SERVING)
    assert refused.value.code == initializer_module.REFUSE_SESSION_CONTRACT
    assert events == [
        "begin",
        "SET search_path = public",
        "SET application_name = 'hh_migrate'",
        "SET statement_timeout = 0",
        "verify:session",
        "rollback",
    ]


def test_the_check_is_refused_when_its_connection_fails_the_contract(monkeypatch):
    events = []
    engine = _SessionEngine(
        events,
        _SessionConnection(events, session_row=_session_row(statement_timeout="5000")),
    )
    monkeypatch.setattr(
        initializer_module.command,
        "check",
        lambda config: pytest.fail("alembic check must not run on a session that failed"),
    )
    with pytest.raises(InitializationError) as refused:
        initializer_module.verify_initialized_database(engine, SERVING)
    assert refused.value.code == initializer_module.REFUSE_SESSION_CONTRACT
    assert events == [
        "SET search_path = public",
        "SET application_name = 'hh_migrate'",
        "SET statement_timeout = 0",
        "verify:session",
    ]


# =============================================================================================
# Live: the lifecycle on the disposable cluster
# =============================================================================================


@pytest.fixture(scope="module")
def project_run():
    run = run_from_environment()
    run.verified_cluster()
    yield run
    run.close()


@pytest.fixture(scope="module")
def project(project_run):
    with project_run.control() as control:
        provisioned = artifact_tests._provision_project(project_run, control)
    try:
        yield provisioned
    finally:
        artifact_tests._deprovision_project(project_run)


class _Live:
    pytestmark = LIVE


@contextmanager
def _serving_database(project, *, harden=False, enable=False, allowconn=False):
    """`house_helper` in exactly the E3 state the caller asked for. A pre-existing database
    is refused, never adopted; the database this test created is dropped again."""
    name = SERVING
    with project.run.control() as control:
        if control.execute(
            "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (name,)
        ).fetchone() is not None:
            pytest.fail(f"{name!r} already exists on this cluster; refusing to adopt or drop it")
        control.execute(
            f'CREATE DATABASE "{name}" OWNER "hh_owner" TEMPLATE template0 '
            "ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' "
            f"ALLOW_CONNECTIONS {'true' if allowconn else 'false'}"
        )
    try:
        if harden:
            with project.run.control() as control:
                with control.transaction():
                    control.execute(
                        artifacts.render_bootstrap(name, "hh_migrate")["harden"]
                    )
        if enable:
            with project.run.control() as control:
                control.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
        yield name
    finally:
        _drop_serving(project, name)


def _drop_serving(project, name):
    """Drop exactly the database this module created, after re-checking its owner and that
    no session is attached. Never forced, never adopted."""
    with project.run.control() as control:
        row = control.execute(
            "SELECT pg_catalog.pg_get_userbyid(d.datdba), "
            "(SELECT count(*) FROM pg_catalog.pg_stat_activity a "
            " WHERE a.datname = d.datname "
            " AND a.backend_type = 'client backend' "
            " AND a.pid <> pg_catalog.pg_backend_pid()) "
            "FROM pg_catalog.pg_database d WHERE d.datname = %s",
            (name,),
        ).fetchone()
        if row is None:
            return
        owner, sessions = str(row[0]), int(row[1])
        if owner != "hh_owner":
            raise AssertionError(f"{name!r} is owned by {owner!r}; it is not dropped")
        if sessions:
            raise AssertionError(f"{name!r} still has {sessions} session(s); it is not dropped")
        control.execute(f'DROP DATABASE "{name}"')


def _migrate(project, name=SERVING):
    return artifacts.project_connection(
        project.run, name, "hh_migrate", project.passwords["hh_migrate"]
    )


def _tables(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
        ).fetchall()
    }


def _assert_untouched(project, name=SERVING):
    """No schema hardening, no object and no version row: the refusal changed nothing."""
    with _migrate(project, name) as connection:
        assert _tables(connection) == set()
        snapshot = artifacts.schema_snapshot(connection)
        assert snapshot["owner"] == "pg_database_owner"
        assert snapshot["defaults"] == ()
        assert connection.execute(
            "SELECT to_regclass('public.alembic_version')"
        ).fetchone()[0] is None


def _assert_initialized(project, database):
    """The acceptance facts of a completed initialization (BOOT-05/09/10/15)."""
    with _migrate(project, database) as connection:
        assert [row[0] for row in connection.execute(
            "SELECT version_num FROM public.alembic_version"
        ).fetchall()] == [HEAD_REVISION]
        ownership = connection.execute(
            "SELECT c.relname, c.relowner::regrole::text FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S', 'i', 'I')"
        ).fetchall()
        assert ownership and all(owner == "hh_owner" for _, owner in ownership)
        assert connection.execute(
            "SELECT last_value, is_called FROM public.messages_id_seq"
        ).fetchone() == (1, False)
        assert _tables(connection) == set(artifact_tests.APPLICATION_TABLES) | {
            "alembic_version"
        }
    # The accepted privilege matrix, measured over the whole catalog.
    artifact_tests._assert_acl_matrix(SimpleNamespace(name=database, project=project))
    # BOOT-05: the fence is still closed for every runtime role.
    with project.run.control() as control:
        matrix = {
            role: control.execute(
                "SELECT has_database_privilege(%s, %s, 'CONNECT')", (role, database)
            ).fetchone()[0]
            for role in artifacts.PROJECT_ROLE_NAMES
        }
        assert matrix["hh_owner"] is True and matrix["hh_migrate"] is True
        for role in ("hh_app", "hh_purge", "hh_backup", "hh_provision", "hh_restore"):
            assert matrix[role] is False, role
        assert artifacts.BootstrapScript().public_entries(control, database) == 0
    # And a real connection as an application login is refused.
    role = RoleIdentity(
        key="hh_app",
        name="hh_app",
        password=project.passwords["hh_app"],
        session_user="hh_app",
        current_user="hh_app",
    )
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(autocommit=True, **project.run.target.connect_params(database, role))


class TestOrderedInitialization(_Live):
    def test_initializes_from_the_absent_state(self, project):
        try:
            initializer = Initializer.for_run(project.run)
            report = initializer.initialize(
                SERVING, "hh_migrate", project.passwords["hh_migrate"]
            )
            assert report.entry_state == e4.E4_ABSENT
            assert report.maintenance_action == e4.ACTION_CREATE_DATABASE
            assert report.authorization_action == e4.ACTION_INITIALIZE_SCHEMA
            assert report.created is True
            assert report.revision == HEAD_REVISION
            assert report.roles_created == frozenset()
            assert report.roles_existing == frozenset(artifacts.PROJECT_ROLE_NAMES)
            assert report.actions == (
                "observe:absent",
                "roles",
                "create_database",
                "harden_database_acl",
                "enable_connections",
                "observe:enabled_maintenance_only",
                "verify_target_objects",
                "schema_contract",
                "alembic_upgrade_head",
                "verify_initialized",
                "privileges",
                "verify_fence",
            )
            _assert_initialized(project, report.database)
        finally:
            _drop_serving(project, SERVING)

    def test_initializes_from_the_disabled_unhardened_state(self, project):
        with _serving_database(project) as name:
            report = Initializer.for_run(project.run).initialize(
                name, "hh_migrate", project.passwords["hh_migrate"]
            )
            assert report.entry_state == e4.E4_DISABLED_UNHARDENED
            assert report.maintenance_action == e4.ACTION_HARDEN_DATABASE_ACL
            assert report.created is False
            assert report.actions[:4] == (
                "observe:disabled_unhardened",
                "roles",
                "harden_database_acl",
                "enable_connections",
            )
            _assert_initialized(project, name)

    def test_initializes_from_the_disabled_hardened_state(self, project):
        with _serving_database(project, harden=True) as name:
            report = Initializer.for_run(project.run).initialize(
                name, "hh_migrate", project.passwords["hh_migrate"]
            )
            assert report.entry_state == e4.E4_DISABLED_HARDENED
            assert report.maintenance_action == e4.ACTION_ENABLE_CONNECTIONS
            assert report.actions[:3] == (
                "observe:disabled_hardened",
                "roles",
                "enable_connections",
            )
            assert "harden_database_acl" not in report.actions
            _assert_initialized(project, name)

    def test_initializes_from_the_enabled_maintenance_only_state(self, project):
        with _serving_database(project, harden=True, enable=True) as name:
            report = Initializer.for_run(project.run).initialize(
                name, "hh_migrate", project.passwords["hh_migrate"]
            )
            assert report.entry_state == e4.E4_ENABLED_MAINTENANCE_ONLY
            assert report.maintenance_action == e4.ACTION_VERIFY_TARGET_OBJECTS
            assert report.authorization_action == e4.ACTION_INITIALIZE_SCHEMA
            assert report.actions[:2] == ("observe:enabled_maintenance_only", "roles")
            assert "create_database" not in report.actions
            _assert_initialized(project, name)


class TestRefusals(_Live):
    def test_a_fail_closed_maintenance_state_is_refused_and_nothing_changes(self, project):
        with _serving_database(project, allowconn=True) as name:
            with pytest.raises(InitializationError) as refused:
                Initializer.for_run(project.run).initialize(
                    name, "hh_migrate", project.passwords["hh_migrate"]
                )
            assert refused.value.code == initializer_module.REFUSE_E4_FAIL_CLOSED
            assert e4.FAIL_PUBLIC_ACCESS in refused.value.detail
            _assert_untouched(project, name)

    def test_an_application_object_makes_the_target_verdict_fail_closed(self, project):
        with _serving_database(project, harden=True, enable=True) as name:
            with _migrate(project, name) as connection:
                with connection.transaction():
                    connection.execute("CREATE TABLE public.wp1cc_probe (id integer)")
            with pytest.raises(InitializationError) as refused:
                Initializer.for_run(project.run).initialize(
                    name, "hh_migrate", project.passwords["hh_migrate"]
                )
            assert refused.value.code == initializer_module.REFUSE_E4_FAIL_CLOSED
            assert e4.FAIL_TABLES in refused.value.detail
            # The schema contract never ran: the database still holds exactly the probe.
            with _migrate(project, name) as connection:
                assert _tables(connection) == {"wp1cc_probe"}
                snapshot = artifacts.schema_snapshot(connection)
                assert snapshot["owner"] == "pg_database_owner"
                assert snapshot["defaults"] == ()

    def test_a_connection_that_is_not_the_grantee_session_never_authorizes(self, project):
        """The only authorized target is the grantee's own session (BOOT-06 / row 4).
        A bootstrap-superuser connection is an unauthorized session: the verdict fails
        closed, `may_initialize_schema` is false and no schema work happens."""
        with _serving_database(project, harden=True, enable=True) as name:

            @contextmanager
            def superuser_target(database, grantee, password):
                # The same database, but the bootstrap superuser's session - not the
                # grantee's own session, which is the only one row 4 accepts.
                connection = psycopg.connect(
                    autocommit=True, **project.run.target.connect_params(database)
                )
                try:
                    yield connection
                finally:
                    connection.close()

            initializer = Initializer(
                control=project.run.control,
                connect_target=superuser_target,
                migrate_engine=lambda *arguments: pytest.fail(
                    "an initializer that was not authorized must not run the chain"
                ),
            )
            with pytest.raises(InitializationError) as refused:
                initializer.initialize(name, "hh_migrate", project.passwords["hh_migrate"])
            assert refused.value.code == initializer_module.REFUSE_E4_FAIL_CLOSED
            assert e4.FAIL_SESSIONS in refused.value.detail
            with _migrate(project, name) as connection:
                assert _tables(connection) == set()
                assert artifacts.schema_snapshot(connection)["defaults"] == ()


class TestInterruptionAndReentry(_Live):
    def test_an_interrupted_chain_leaves_no_application_tables_and_reentry_fails_closed(
        self, project
    ):
        """BOOT-09 step 2: the chain is one transaction. An interrupted chain leaves zero
        application tables and `alembic_version` absent. BOOT-04 has already committed by
        then, so the accepted classifier does not re-authorize that database (BOOT-07 row
        5): a subsequent invocation refuses and changes nothing. RBK-08(a)'s recovery -
        destroy and recreate - then initializes normally."""
        with _serving_database(project, harden=True, enable=True) as name:

            def factory(database, grantee, password):
                return artifacts.migration_engine(
                    project.run, database, grantee, project.passwords[grantee]
                )

            def interrupted(engine, database):
                # A conflicting object is planted AFTER the E4 authorization, in its own
                # transaction, so it survives the chain's rollback: the chain must fail at
                # 0007 and take every earlier revision with it. The chain itself runs
                # through the initializer's own helper, so this also exercises the ROL-06
                # contract and the single `engine.begin()` it relies on.
                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        "CREATE TABLE public.favorite_recipes (planted integer)"
                    )
                initializer_module._upgrade_to_head(engine, database)

            initializer = Initializer(
                control=project.run.control,
                connect_target=lambda database, grantee, password: _migrate(project, database),
                migrate_engine=factory,
                upgrade=interrupted,
            )
            with pytest.raises(sa.exc.ProgrammingError) as failed:
                initializer.initialize(name, "hh_migrate", project.passwords["hh_migrate"])
            assert failed.value.orig.sqlstate == "42P07"  # duplicate_table

            with _migrate(project, name) as connection:
                assert connection.execute(
                    "SELECT to_regclass('public.alembic_version')"
                ).fetchone()[0] is None
                assert _tables(connection) == {"favorite_recipes"}  # only the planted object
                # BOOT-04 did commit: this is the partial state the classifier refuses.
                assert artifacts.schema_snapshot(connection)["owner"] == "hh_owner"
                # Remove the planted foreign object, then attempt the safe re-entry.
                with connection.transaction():
                    connection.execute("DROP TABLE public.favorite_recipes")

            with pytest.raises(InitializationError) as refused:
                Initializer.for_run(project.run).initialize(
                    name, "hh_migrate", project.passwords["hh_migrate"]
                )
            assert refused.value.code == initializer_module.REFUSE_E4_FAIL_CLOSED
            with _migrate(project, name) as connection:
                assert _tables(connection) == set()  # the refusal created nothing

        # RBK-08(a): destroy and recreate the empty database, then run it again.
        try:
            report = Initializer.for_run(project.run).initialize(
                SERVING, "hh_migrate", project.passwords["hh_migrate"]
            )
            assert report.created is True
            _assert_initialized(project, SERVING)
        finally:
            _drop_serving(project, SERVING)

    def test_the_initialization_leaves_the_target_session_untouched(self, project):
        """The classifier issues no transaction control (FRESH-4), and the artifacts'
        `SET LOCAL` settings revert at COMMIT: the maintenance-login session ends every
        phase in exactly the state it began, and idle."""
        recorded = []

        @contextmanager
        def recording_target(database, grantee, password):
            with _migrate(project, database) as connection:
                before = connection.execute(
                    "SELECT current_setting('search_path'), "
                    "current_setting('application_name'), "
                    "current_setting('statement_timeout'), "
                    "current_setting('transaction_isolation'), "
                    "current_user, session_user"
                ).fetchone()
                try:
                    yield connection
                finally:
                    after = connection.execute(
                        "SELECT current_setting('search_path'), "
                        "current_setting('application_name'), "
                        "current_setting('statement_timeout'), "
                        "current_setting('transaction_isolation'), "
                        "current_user, session_user"
                    ).fetchone()
                    recorded.append(
                        (before, after, connection.info.transaction_status)
                    )

        with _serving_database(project, harden=True, enable=True) as name:
            report = Initializer(
                control=project.run.control,
                connect_target=recording_target,
                migrate_engine=lambda database, grantee, password: artifacts.migration_engine(
                    project.run, database, grantee, project.passwords[grantee]
                ),
            ).initialize(name, "hh_migrate", project.passwords["hh_migrate"])
            assert report.database == name
        assert len(recorded) == 2  # the schema phase and the privileges phase
        for before, after, status in recorded:
            assert before == after
            assert status == psycopg.pq.TransactionStatus.IDLE


class TestMigrationSessionContract(_Live):
    def test_the_chain_and_check_connections_carry_the_rol06_contract(self, project, monkeypatch):
        """The SAME real connections Alembic is handed - the chain's and the one
        `alembic check` compares through - hold exactly the frozen ROL-06 contract at the
        moment the operation runs. Neither relies on BOOT-04's transaction-local settings
        or on whatever an earlier pooled transaction left behind."""
        with _serving_database(project, harden=True, enable=True) as name:
            report = Initializer.for_run(project.run).initialize(
                name, "hh_migrate", project.passwords["hh_migrate"]
            )
            assert report.revision == HEAD_REVISION

            observed = []

            def record(config, label):
                row = config.attributes["connection"].exec_driver_sql(
                    "SELECT session_user, current_user, current_database(), "
                    "current_setting('search_path'), current_setting('application_name'), "
                    "current_setting('statement_timeout')"
                ).fetchone()
                observed.append((label, tuple(row)))

            monkeypatch.setattr(
                initializer_module.command,
                "upgrade",
                lambda config, revision: record(config, "upgrade"),
            )
            monkeypatch.setattr(
                initializer_module.command, "check", lambda config: record(config, "check")
            )

            engine = artifacts.migration_engine(
                project.run, name, "hh_migrate", project.passwords["hh_migrate"]
            )
            try:
                initializer_module._upgrade_to_head(engine, name)
                assert initializer_module.verify_initialized_database(engine, name) == HEAD_REVISION
            finally:
                engine.dispose()

            assert [label for label, _ in observed] == ["upgrade", "check"]
            for label, row in observed:
                assert row == ("hh_migrate", "hh_owner", name, "public", "hh_migrate", "0"), label

    def test_a_connection_that_did_not_establish_the_contract_is_refused(self, project):
        """Incorrect initial session settings cannot silently pass. A real migration
        connection that has not run the ROL-06 preamble is refused by the read-back (its
        inherited `search_path` is `"$user", public`, not `public`)."""
        with _serving_database(project, harden=True, enable=True) as name:
            engine = artifacts.migration_engine(
                project.run, name, "hh_migrate", project.passwords["hh_migrate"]
            )
            try:
                with engine.connect() as connection:
                    with pytest.raises(InitializationError) as refused:
                        initializer_module.require_migration_session(connection, database=name)
                    assert refused.value.code == initializer_module.REFUSE_SESSION_CONTRACT
                    assert refused.value.message == "search_path is not exactly public"
            finally:
                engine.dispose()
