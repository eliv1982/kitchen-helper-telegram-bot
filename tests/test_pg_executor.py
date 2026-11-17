"""Offline executor tests: fresh/upgrade dispatch, mandatory privileges, refusals.

No database is opened. The executor's operation hooks are injected; `plan_migration` is
exercised against the image's real revision chart and model metadata.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from house_helper.persistence import pg_artifacts
from house_helper.persistence import pg_contract as contract
from house_helper.persistence import pg_executor as executor
from house_helper.persistence import pg_journal as journal
from tests.test_pg_enrollment_mode_a import _hardened_acl, _role, bootstrap_role

SID = "726485930221"
TOKEN = "a" * 32
MARKER = journal.make_marker(TOKEN, SID)
BASE = ("house_helper", "postgres", "template0", "template1")


def _live(**overrides) -> executor.LiveFacts:
    roles = {name: _role(name) for name in contract.PROJECT_ROLE_NAMES}
    roles["postgres"] = bootstrap_role()
    entries = _hardened_acl()
    facts = contract.DatabaseFacts(
        exists=True,
        owner="hh_owner",
        encoding="UTF8",
        collate="C",
        ctype="C",
        provider="c",
        allowconn=True,
        acl_materialized=True,
        acl_entries=entries,
        connect={
            role: contract.database_privilege(True, entries, role, "CONNECT")
            for role in contract.PROJECT_ROLE_NAMES
        },
        temporary={
            role: contract.database_privilege(True, entries, role, "TEMPORARY")
            for role in contract.PROJECT_ROLE_NAMES
        },
        sessions=(),
        probe_pid=None,
    )
    values = dict(
        marker=MARKER,
        databases=BASE,
        roles=roles,
        memberships=tuple(
            contract.MembershipEdge(member, granted, admin, inherit, set_option)
            for member, granted, admin, inherit, set_option in (
                contract.FROZEN_PREDEFINED_MEMBERSHIPS
            )
        )
        + (
            contract.MembershipEdge("hh_migrate", "hh_owner", False, False, True),
            contract.MembershipEdge("hh_restore", "hh_owner", False, False, True),
        ),
        predefined_roles=contract.FROZEN_PREDEFINED_ROLES,
        database=facts,
        inventory=contract.TargetInventory(()),
        versions=(),
        public_tables=(),
        sequence_is_called=False,
        other_sessions=0,
        default_acls=(),
    )
    values.update(overrides)
    return executor.LiveFacts(**values)


def _head() -> str:
    return executor._known_revisions()[0]


def _authorization(live: executor.LiveFacts, *, issued=None, system_identifier=SID, **overrides):
    issued = issued or datetime.now(timezone.utc)
    value = journal.build_authorization(
        cluster={
            "container_name": "house-helper-db",
            "container_id": "b" * 64,
            "image_digest": "postgres@sha256:" + "c" * 64,
            "volume": "house-helper-pgdata",
            "system_identifier": system_identifier,
        },
        marker=live.marker,
        database="house_helper",
        project_roles=list(contract.PROJECT_ROLE_NAMES),
        databases=list(live.databases),
        predefined_roles=list(live.predefined_roles),
        artifacts=dict(pg_artifacts.manifest()),
        journal_seq=6,
        journal_head="sha256:" + "d" * 64,
        run_id="e" * 32,
        issued=issued,
    )
    value.update(overrides)
    return journal.encode_authorization(value, lifetime_seconds=900), issued


# --- the pure fresh/upgrade dispatch ---------------------------------------------------------


def test_fresh_is_the_only_mutable_path_for_an_empty_enrolled_database():
    plan = executor.plan_migration(_live())
    assert plan.kind == executor.FRESH
    assert plan.head == _head()


def test_an_empty_database_with_a_foreign_table_is_refused():
    plan = executor.plan_migration(_live(public_tables=("someone_elses",)))
    assert plan.kind == executor.REFUSE
    assert any(item.startswith("foreign_tables:") for item in plan.failures)


def test_an_empty_database_with_an_unexpected_catalog_object_is_refused():
    plan = executor.plan_migration(
        _live(inventory=contract.TargetInventory((("relation", "public", "x"),)))
    )
    assert plan.kind == executor.REFUSE
    assert "unexpected_object:relation:public:x" in plan.failures


def test_a_known_behind_revision_is_an_upgrade():
    expected = tuple(sorted(executor._expected_tables()))
    plan = executor.plan_migration(
        _live(versions=("0013_user_text_size",), public_tables=expected)
    )
    assert plan.kind == executor.UPGRADE
    assert plan.revision == "0013_user_text_size"


def test_the_head_revision_is_a_noop_that_still_reapplies_privileges():
    expected = tuple(sorted(executor._expected_tables()))
    plan = executor.plan_migration(_live(versions=(_head(),), public_tables=expected))
    assert plan.kind == executor.NOOP


def test_an_unknown_revision_or_a_foreign_database_is_refused():
    expected = tuple(sorted(executor._expected_tables()))
    assert executor.plan_migration(
        _live(versions=("9999_foreign",), public_tables=expected)
    ).kind == executor.REFUSE
    assert executor.plan_migration(
        _live(versions=(_head(),), public_tables=expected + ("evil",))
    ).kind == executor.REFUSE
    assert executor.plan_migration(_live(versions=("a", "b"))).kind == executor.REFUSE
    # A missing table at an older revision may be created by the upgrade itself; the
    # post-upgrade `alembic check` is the drift gate. At the head, a missing table is drift.
    assert executor.plan_migration(
        _live(versions=("0013_user_text_size",), public_tables=expected[:-1])
    ).kind == executor.UPGRADE
    assert executor.plan_migration(
        _live(versions=(_head(),), public_tables=expected[:-1])
    ).kind == executor.REFUSE


def test_a_revision_off_the_head_chain_is_refused(monkeypatch):
    expected = tuple(sorted(executor._expected_tables()))
    monkeypatch.setattr(
        executor,
        "_known_revisions",
        lambda: ("head", {"head": None, "branch": None}, "head"),
    )
    plan = executor.plan_migration(
        _live(versions=("branch",), public_tables=expected)
    )
    assert plan.kind == executor.REFUSE
    assert "revision_not_an_ancestor_of_head" in plan.failures


class _BeginEngine:
    def begin(self):
        return _Connection()


def test_the_composite_fresh_path_applies_schema_then_chain_then_privileges(monkeypatch):
    log = []
    monkeypatch.setattr(executor, "_apply_schema", lambda connection: log.append("schema"))
    monkeypatch.setattr(executor, "_upgrade", lambda connection, head: log.append("migrate"))
    monkeypatch.setattr(
        executor, "_verify_initialized", lambda connection, **kw: log.append("verify")
    )
    monkeypatch.setattr(executor, "_apply_privileges", lambda connection: log.append("privileges"))
    executor.initialize_fresh(_BeginEngine(), head=_head())
    assert log == ["schema", "migrate", "verify", "privileges"]


def test_the_composite_upgrade_path_always_reapplies_privileges(monkeypatch):
    log = []
    monkeypatch.setattr(executor, "_upgrade", lambda connection, head: log.append("migrate"))
    monkeypatch.setattr(
        executor, "_verify_initialized", lambda connection, **kw: log.append("verify")
    )
    monkeypatch.setattr(executor, "_apply_privileges", lambda connection: log.append("privileges"))
    executor.upgrade_existing(_BeginEngine(), head=_head())
    assert log == ["migrate", "verify", "privileges"]


def test_a_composite_privilege_failure_propagates_and_never_reports_success(monkeypatch):
    monkeypatch.setattr(executor, "_upgrade", lambda connection, head: None)
    monkeypatch.setattr(executor, "_verify_initialized", lambda connection, **kw: None)

    def failing(connection):
        raise executor.ExecutorError("privileges_refused", "db/privileges.sql refused")

    monkeypatch.setattr(executor, "_apply_privileges", failing)
    with pytest.raises(executor.ExecutorError) as refused:
        executor.upgrade_existing(_BeginEngine(), head=_head())
    assert refused.value.code == "privileges_refused"


# --- the executor order and refusals ---------------------------------------------------------


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def exec_driver_sql(self, sql):
        if sql.startswith("SET "):
            return _Result(None)
        if "session_user" in sql:
            return _Result(
                ("hh_migrate", "hh_owner", "house_helper", "public", "hh_migrate", "0")
            )
        raise AssertionError("the offline test must not execute other SQL")


class _Engine:
    def __init__(self):
        self.disposed = False

    def connect(self):
        return _Connection()

    def dispose(self):
        self.disposed = True


class _Lock:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        self.log.append("lock_enter")
        return self

    def __exit__(self, *exc_info):
        self.log.append("lock_exit")
        return False


def _executor(live, *, plan=None, fresh=None, upgrade=None, post=None, log=None):
    log = [] if log is None else log
    engine = _Engine()

    def fresh_default(engine_, *, head):
        log.append("fresh")

    def upgrade_default(engine_, *, head):
        log.append("upgrade")

    def post_default(engine_, *, fresh):
        log.append("post")
        return _live(versions=(_head(),), public_tables=tuple(sorted(executor._expected_tables())))

    return (
        executor.Executor(
            lock=lambda: _Lock(log),
            engine_factory=lambda: engine,
            observe=lambda connection: live,
            plan=plan or executor.plan_migration,
            fresh=fresh or fresh_default,
            upgrade=upgrade or upgrade_default,
            post_verify=post or post_default,
        ),
        log,
        engine,
    )


def test_fresh_execution_runs_schema_then_migration_then_privileges():
    authorization, issued = _authorization(_live())
    running, log, engine = _executor(_live())
    report = running.run(authorization, now=issued)
    assert report.plan == executor.FRESH
    assert log == ["lock_enter", "fresh", "post", "lock_exit"]
    assert report.actions == (
        executor.STEP_AUTHORIZATION,
        executor.STEP_LOCK,
        executor.STEP_SESSION,
        executor.STEP_CONNECT,
        executor.STEP_IDENTITY,
        executor.STEP_INVENTORY,
        executor.STEP_SCHEMA_STATE + ":fresh",
        executor.STEP_SCHEMA,
        executor.STEP_MIGRATE,
        executor.STEP_VERIFY,
        executor.STEP_PRIVILEGES,
        executor.STEP_FENCE,
    )
    assert engine.disposed is True


def test_an_upgrade_and_a_noop_both_reapply_privileges():
    expected = tuple(sorted(executor._expected_tables()))
    for version, kind in (("0013_user_text_size", executor.UPGRADE), (_head(), executor.NOOP)):
        live = _live(versions=(version,), public_tables=expected)
        authorization, issued = _authorization(live)
        running, log, _ = _executor(live)
        report = running.run(authorization, now=issued)
        assert report.plan == kind
        assert log == ["lock_enter", "upgrade", "post", "lock_exit"]
        assert executor.STEP_PRIVILEGES in report.actions
        assert executor.STEP_SCHEMA not in report.actions


def test_a_privilege_failure_is_fail_closed_and_releases_the_lock():
    log = []

    def failing_upgrade(engine, *, head):
        log.append("upgrade")
        raise executor.ExecutorError("privileges_refused", "db/privileges.sql refused")

    live = _live(versions=("0013_user_text_size",), public_tables=tuple(sorted(executor._expected_tables())))
    authorization, issued = _authorization(live)
    running, log, _ = _executor(live, upgrade=failing_upgrade, log=log)
    with pytest.raises(executor.ExecutorError) as refused:
        running.run(authorization, now=issued)
    assert refused.value.code == "privileges_refused"
    assert log == ["lock_enter", "upgrade", "lock_exit"]


def test_a_live_marker_mismatch_stops_before_any_mutation():
    live = _live()
    authorization, issued = _authorization(live)
    other = _live(marker=journal.make_marker("b" * 32, SID))
    running, log, _ = _executor(other)
    with pytest.raises(executor.ExecutorError) as refused:
        running.run(authorization, now=issued)
    assert refused.value.code == "marker"
    assert log == ["lock_enter", "lock_exit"]


def test_a_system_identifier_mismatch_stops_before_any_mutation():
    live = _live()
    authorization, issued = _authorization(live, system_identifier="111111111111")
    running, log, _ = _executor(live)
    with pytest.raises(executor.ExecutorError) as refused:
        running.run(authorization, now=issued)
    assert refused.value.code == "system_identifier"
    assert log == ["lock_enter", "lock_exit"]


def test_the_authorization_must_name_this_image_s_artifacts():
    live = _live()
    authorization, issued = _authorization(
        live, artifacts={"roles.sql": "0" * 64}
    )
    running, log, _ = _executor(live)
    with pytest.raises(executor.ExecutorError) as refused:
        running.run(authorization, now=issued)
    assert refused.value.code == "artifact_identity"
    assert log == ["lock_enter", "lock_exit"]


def test_an_unexpected_inventory_or_acl_stops_before_any_mutation():
    clean = _live()
    authorization, issued = _authorization(clean)
    other = _live(databases=BASE + ("stranger",))
    running, log, _ = _executor(other)
    with pytest.raises(executor.ExecutorError) as refused:
        running.run(authorization, now=issued)
    assert refused.value.code == "database_inventory"
    assert log == ["lock_enter", "lock_exit"]


def test_a_refused_schema_state_stops_before_any_mutation():
    live = _live(public_tables=("foreign_table",))
    authorization, issued = _authorization(live)
    running, log, _ = _executor(live)
    with pytest.raises(executor.ExecutorError) as refused:
        running.run(authorization, now=issued)
    assert refused.value.code == "schema_state"
    assert log == ["lock_enter", "lock_exit"]


def test_a_post_migration_fence_failure_is_not_success():
    def failing_post(engine, *, fresh):
        raise executor.ExecutorError("database_fence", "runtime role can connect")

    live = _live()
    authorization, issued = _authorization(live)
    running, log, _ = _executor(live, post=failing_post)
    with pytest.raises(executor.ExecutorError) as refused:
        running.run(authorization, now=issued)
    assert refused.value.code == "database_fence"
    assert log == ["lock_enter", "fresh", "lock_exit"]


def _expected_default_acls():
    return tuple(("hh_owner",) + entry for entry in contract.EXPECTED_DEFAULT_ACLS)


def _initialized_inventory():
    entries = [
        ("relation", "public", name)
        for name in sorted(contract.expected_relation_names())
    ]
    entries.append(("default acl", "", "oid:16400"))
    return contract.TargetInventory(tuple(entries))


def test_plan_accepts_a_completed_initialized_inventory():
    expected = tuple(sorted(executor._expected_tables()))
    live = _live(
        versions=(_head(),),
        public_tables=expected,
        inventory=_initialized_inventory(),
        default_acls=_expected_default_acls(),
    )
    assert executor.plan_migration(live).kind == executor.NOOP


def test_plan_accepts_an_older_revision_with_authorized_objects():
    expected = tuple(sorted(executor._expected_tables()))
    live = _live(
        versions=("0013_user_text_size",),
        public_tables=expected,
        inventory=_initialized_inventory(),
        default_acls=_expected_default_acls(),
    )
    assert executor.plan_migration(live).kind == executor.UPGRADE


def test_plan_still_refuses_a_foreign_object_in_an_initialized_inventory():
    expected = tuple(sorted(executor._expected_tables()))
    live = _live(
        versions=(_head(),),
        public_tables=expected,
        inventory=contract.TargetInventory(
            _initialized_inventory().entries + (("relation", "public", "evil"),)
        ),
        default_acls=_expected_default_acls(),
    )
    plan = executor.plan_migration(live)
    assert plan.kind == executor.REFUSE
    assert "unexpected_object:relation:public:evil" in plan.failures


def _alembic_backing_names() -> set[str]:
    """Derived independently from Alembic's own metadata construction (F6)."""
    import sqlalchemy as sa
    from alembic.migration import MigrationContext

    context = MigrationContext.configure(dialect_name="postgresql")
    table = context.impl.version_table_impl(
        version_table="alembic_version",
        version_table_schema=None,
        version_table_pk=True,
    )
    names = {table.name}
    for constraint in table.constraints:
        if isinstance(constraint, (sa.PrimaryKeyConstraint, sa.UniqueConstraint)):
            if constraint.name:
                names.add(constraint.name)
    for index in table.indexes:
        if index.name:
            names.add(index.name)
    return names


def test_the_authorized_inventory_derives_alembic_s_backing_index():
    names = _alembic_backing_names()
    assert "alembic_version_pkc" in names
    assert names <= contract.expected_relation_names()


def test_a_completed_inventory_with_alembic_version_pkc_is_accepted():
    expected = tuple(sorted(executor._expected_tables()))
    # The model relations from the image plus the relations Alembic itself constructs.
    relations = contract.expected_relation_names(include_alembic_version=False) | (
        _alembic_backing_names()
    )
    inventory = contract.TargetInventory(
        tuple(("relation", "public", name) for name in sorted(relations))
        + (("default acl", "", "oid:16400"),)
    )
    live = _live(
        versions=(_head(),),
        public_tables=expected,
        inventory=inventory,
        default_acls=_expected_default_acls(),
    )
    assert executor.plan_migration(live).kind == executor.NOOP


def test_an_unrelated_index_is_still_foreign():
    expected = tuple(sorted(executor._expected_tables()))
    live = _live(
        versions=(_head(),),
        public_tables=expected,
        inventory=contract.TargetInventory(
            _initialized_inventory().entries
            + (("relation", "public", "alembic_version_pkc_extra"),)
        ),
        default_acls=_expected_default_acls(),
    )
    plan = executor.plan_migration(live)
    assert plan.kind == executor.REFUSE
    assert (
        "unexpected_object:relation:public:alembic_version_pkc_extra" in plan.failures
    )


def test_the_alembic_relations_are_not_authorized_before_initialization():
    # A schema-only partial state (default ACLs, no revision) may not carry the version
    # table or its backing index.
    for name in ("alembic_version", "alembic_version_pkc"):
        live = _live(
            inventory=contract.TargetInventory(
                (("default acl", "", "oid:16400"), ("relation", "public", name))
            ),
            default_acls=_expected_default_acls(),
        )
        plan = executor.plan_migration(live)
        assert plan.kind == executor.REFUSE, name
        assert f"unexpected_object:relation:public:{name}" in plan.failures


def _public_schema_acl(*, hardened: bool) -> tuple:
    usage = tuple(True for _ in contract.PROJECT_ROLE_NAMES)
    if hardened:
        return ("hh_owner", 0) + usage + tuple(
            role == contract.OWNER_ROLE for role in contract.PROJECT_ROLE_NAMES
        )
    return ("pg_database_owner", 2) + usage + tuple(
        True for _ in contract.PROJECT_ROLE_NAMES
    )


def _schema_live(*, applied: bool, **overrides):
    values = dict(
        # The ENR-04 schema step runs at E3-c, strictly before the ENR-05 marker.
        marker="",
        default_acls=_expected_default_acls() if applied else (),
        inventory=contract.TargetInventory(
            (("default acl", "", "oid:16400"),) if applied else ()
        ),
        schema_acl=_public_schema_acl(hardened=applied),
    )
    values.update(overrides)
    return _live(**values)


def _schema_authorization(**overrides):
    """The decoded schema-only record the host writes to the one-shot's stdin (F8)."""
    value = journal.build_schema_authorization(
        cluster={
            "container_name": contract.DEPLOYMENT_CONTAINER_NAME,
            "container_id": "b" * 64,
            "image_digest": "postgres@sha256:" + "c" * 64,
            "volume": contract.DEPLOYMENT_VOLUME,
            "system_identifier": SID,
        },
        database="house_helper",
        artifacts=dict(pg_artifacts.manifest()),
        run_id="e" * 32,
        issued=datetime.now(timezone.utc),
    )
    value.update(overrides)
    return value


def test_the_enrollment_schema_step_applies_exactly_schema_sql(monkeypatch):
    log: list[str] = []
    observations = iter([_schema_live(applied=False), _schema_live(applied=True)])
    monkeypatch.setattr(executor, "observe_live", lambda connection: next(observations))
    monkeypatch.setattr(executor, "_apply_schema", lambda connection: log.append("schema"))
    executor.apply_schema_contract(_RuntimeEngine(), _schema_authorization())
    assert log == ["schema"]


def test_the_enrollment_schema_step_refuses_an_initialized_database(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(
        executor,
        "observe_live",
        lambda connection: _live(marker="", public_tables=("messages",)),
    )
    monkeypatch.setattr(executor, "_apply_schema", lambda connection: log.append("schema"))
    with pytest.raises(executor.ExecutorError) as refused:
        executor.apply_schema_contract(_RuntimeEngine(), _schema_authorization())
    assert refused.value.code == "schema_state"
    assert log == []


def test_the_enrollment_schema_step_refuses_an_unhardened_result(monkeypatch):
    observations = iter(
        [
            _schema_live(applied=False),
            _schema_live(applied=False),  # db/schema.sql did not install ROL-14
        ]
    )
    monkeypatch.setattr(executor, "observe_live", lambda connection: next(observations))
    monkeypatch.setattr(executor, "_apply_schema", lambda connection: None)
    with pytest.raises(executor.ExecutorError) as refused:
        executor.apply_schema_contract(_RuntimeEngine(), _schema_authorization())
    assert refused.value.code == "schema_contract"
    assert "missing_default_acl:" in str(refused.value)


def test_the_enrollment_schema_step_refuses_an_existing_enrollment_marker(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(
        executor,
        "observe_live",
        lambda connection: _schema_live(applied=False, marker=MARKER),
    )
    monkeypatch.setattr(executor, "_apply_schema", lambda connection: log.append("schema"))
    with pytest.raises(executor.ExecutorError) as refused:
        executor.apply_schema_contract(_RuntimeEngine(), _schema_authorization())
    assert refused.value.code == "schema_state"
    assert "marker:present" in str(refused.value)
    assert log == []


def test_the_enrollment_schema_step_refuses_a_foreign_database_inventory(monkeypatch):
    log: list[str] = []
    monkeypatch.setattr(
        executor,
        "observe_live",
        lambda connection: _schema_live(
            applied=False, databases=BASE + ("someone_else",)
        ),
    )
    monkeypatch.setattr(executor, "_apply_schema", lambda connection: log.append("schema"))
    with pytest.raises(executor.ExecutorError) as refused:
        executor.apply_schema_contract(_RuntimeEngine(), _schema_authorization())
    assert refused.value.code == "schema_state"
    assert "unexpected_database:someone_else" in str(refused.value)
    assert log == []


@pytest.mark.parametrize(
    "override, item",
    [
        ({"owner": "unrelated_admin"}, "database:ownership"),
        ({"encoding": "SQL_ASCII"}, "database:encoding"),
        ({"collate": "en_US.utf8"}, "database:locale"),
        ({"ctype": "en_US.utf8"}, "database:locale"),
        ({"provider": "i"}, "database:locale"),
    ],
)
def test_the_enrollment_schema_step_refuses_a_wrong_owner_encoding_or_locale(
    monkeypatch, override, item
):
    log: list[str] = []
    facts = _schema_live(applied=False).database
    assert facts is not None
    live = replace(_schema_live(applied=False), database=replace(facts, **override))
    monkeypatch.setattr(executor, "observe_live", lambda connection: live)
    monkeypatch.setattr(executor, "_apply_schema", lambda connection: log.append("schema"))
    with pytest.raises(executor.ExecutorError) as refused:
        executor.apply_schema_contract(_RuntimeEngine(), _schema_authorization())
    assert refused.value.code == "schema_state"
    assert item in str(refused.value)
    assert log == []


@pytest.mark.parametrize(
    "field, value, item",
    [
        ("container_name", "another-db", "authorization:container_name"),
        ("volume", "another-volume", "authorization:volume"),
    ],
)
def test_the_enrollment_schema_step_refuses_a_non_deployment_authorized_identity(
    monkeypatch, field, value, item
):
    log: list[str] = []
    monkeypatch.setattr(executor, "observe_live", lambda connection: _schema_live(applied=False))
    monkeypatch.setattr(executor, "_apply_schema", lambda connection: log.append("schema"))
    authorization = _schema_authorization()
    authorization["cluster"] = dict(authorization["cluster"])
    authorization["cluster"][field] = value
    with pytest.raises(executor.ExecutorError) as refused:
        executor.apply_schema_contract(_RuntimeEngine(), authorization)
    assert refused.value.code == "schema_state"
    assert item in str(refused.value)
    assert log == []


def test_the_enrollment_schema_step_refuses_an_authorization_for_another_database(
    monkeypatch,
):
    log: list[str] = []
    monkeypatch.setattr(executor, "observe_live", lambda connection: _schema_live(applied=False))
    monkeypatch.setattr(executor, "_apply_schema", lambda connection: log.append("schema"))
    with pytest.raises(executor.ExecutorError) as refused:
        executor.apply_schema_contract(
            _RuntimeEngine(), _schema_authorization(database="postgres")
        )
    assert refused.value.code == "schema_state"
    assert "authorization:database" in str(refused.value)
    assert log == []


def test_the_enrollment_schema_step_refuses_an_unexpected_inventory_or_default_acl_state(
    monkeypatch,
):
    log: list[str] = []
    foreign = contract.TargetInventory((("relation", "public", "evil"),))
    monkeypatch.setattr(
        executor,
        "observe_live",
        lambda connection: _schema_live(applied=False, inventory=foreign),
    )
    monkeypatch.setattr(executor, "_apply_schema", lambda connection: log.append("schema"))
    with pytest.raises(executor.ExecutorError) as refused:
        executor.apply_schema_contract(_RuntimeEngine(), _schema_authorization())
    assert refused.value.code == "schema_state"
    assert "unexpected_object:relation:public:evil" in str(refused.value)
    assert log == []

    # A default ACL matrix that is not exactly ROL-14 is never a recognized partial state.
    partial = tuple(("hh_owner",) + entry for entry in contract.EXPECTED_DEFAULT_ACLS)[:-1]
    monkeypatch.setattr(
        executor,
        "observe_live",
        lambda connection: _schema_live(
            applied=False,
            default_acls=partial,
            inventory=contract.TargetInventory((("default acl", "", "oid:16400"),)),
        ),
    )
    with pytest.raises(executor.ExecutorError) as refused:
        executor.apply_schema_contract(_RuntimeEngine(), _schema_authorization())
    assert refused.value.code == "schema_state"
    assert "missing_default_acl:" in str(refused.value)
    assert log == []


def test_the_executor_accepts_the_pg16_predefined_membership_structure():
    live = _live()
    authorization, issued = _authorization(live)
    report = _executor(live)[0].run(authorization, now=issued)
    assert report.plan == executor.FRESH


def test_the_executor_refuses_a_project_role_granted_a_predefined_role():
    live = _live()
    authorization, issued = _authorization(live)
    tampered = _live(
        memberships=live.memberships
        + (contract.MembershipEdge("hh_app", "pg_monitor", False, False, False),)
    )
    running, _log, _engine = _executor(tampered)
    with pytest.raises(executor.ExecutorError) as refused:
        running.run(authorization, now=issued)
    assert refused.value.code == "role_inventory"


def test_the_executor_refuses_an_unrelated_superuser():
    from tests.test_pg_enrollment_mode_a import bootstrap_role

    live = _live()
    authorization, issued = _authorization(live)
    roles = dict(live.roles)
    roles["unrelated_admin"] = bootstrap_role()
    tampered = _live(roles=roles)
    running, _log, _engine = _executor(tampered)
    with pytest.raises(executor.ExecutorError) as refused:
        running.run(authorization, now=issued)
    assert refused.value.code == "role_inventory"


class _RuntimeEngine:
    """A real keyword-call path with no database: begin/connect yield the same double."""

    def __init__(self):
        self.disposed = False

    def begin(self):
        return _Connection()

    def connect(self):
        return _Connection()

    def dispose(self):
        self.disposed = True


def test_executor_run_uses_the_real_keyword_only_default_callbacks(monkeypatch):
    log: list[str] = []

    def schema(connection):
        log.append("schema")

    def upgrade(connection, head):
        log.append("migrate")

    def verify(connection, **kwargs):
        log.append("verify")

    def privileges(connection):
        log.append("privileges")

    monkeypatch.setattr(executor, "_apply_schema", schema)
    monkeypatch.setattr(executor, "_upgrade", upgrade)
    monkeypatch.setattr(executor, "_verify_initialized", verify)
    monkeypatch.setattr(executor, "_apply_privileges", privileges)
    head_live = _live(
        versions=(_head(),),
        public_tables=tuple(sorted(executor._expected_tables())),
        inventory=_initialized_inventory(),
        default_acls=_expected_default_acls(),
    )
    monkeypatch.setattr(executor, "observe_live", lambda connection: head_live)
    fresh = _live()
    authorization, issued = _authorization(fresh)
    running = executor.Executor(
        lock=lambda: _Lock(log),
        engine_factory=_RuntimeEngine,
        observe=lambda connection: fresh,
        # The real composite defaults: no positional-compatible substitute.
        fresh=executor.initialize_fresh,
        upgrade=executor.upgrade_existing,
        post_verify=executor._post_verify,
    )
    report = running.run(authorization, now=issued)
    assert report.plan == executor.FRESH
    assert log == ["lock_enter", "schema", "migrate", "verify", "privileges", "lock_exit"]


def test_a_positional_call_to_the_keyword_only_initializer_is_refused():
    with pytest.raises(TypeError):
        executor.initialize_fresh(_BeginEngine(), _head())


def _psycopg_sqlalchemy_connection(monkeypatch):
    """A real SQLAlchemy psycopg-dialect connection over a stub DBAPI connection.

    The dialect and SQLAlchemy's execution path are real; only the libpq connection is
    replaced. The stub cursor behaves like psycopg3: when a parameter collection is
    supplied it treats `%` as placeholder syntax.
    """
    import psycopg
    from sqlalchemy import create_engine

    calls: list[tuple[str, object]] = []

    class Cursor:
        description = None
        rowcount = -1
        pgresult = None

        def execute(self, statement, parameters=None):
            calls.append((statement, parameters))
            if parameters is not None and "%" in statement:
                raise ValueError("psycopg placeholder parsing rejected a literal %")

        def close(self):
            pass

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class DbapiConnection:
        def __init__(self):
            self._cursor = Cursor()

        def cursor(self, *args, **kwargs):
            return self._cursor

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

        def add_notice_handler(self, handler):
            pass

        @property
        def info(self):
            return None

    original = psycopg.connect
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: DbapiConnection())
    engine = create_engine("postgresql+psycopg://user@house-helper-db/db")
    # These tests do not need PostgreSQL's version handshake.
    engine.dialect.initialize = lambda connection: None
    return engine, calls, original


def test_frozen_sql_with_literal_percent_runs_through_sqlalchemy_psycopg(monkeypatch):
    engine, calls, original = _psycopg_sqlalchemy_connection(monkeypatch)
    try:
        sql = (
            "DO $$ BEGIN RAISE EXCEPTION 'literal %s and %', 1; END $$;\n"
            "SELECT format('%s|%s', 1, 2);"
        )
        with engine.connect() as connection:
            # The pre-fix path passed an empty parameter mapping and psycopg rejected it.
            with pytest.raises(ValueError):
                connection.exec_driver_sql(sql)
            executor._apply_script(connection, sql, label="schema.sql")
    finally:
        monkeypatch.setattr(psycopg_connect_module(), "connect", original)
        engine.dispose()
    assert calls[-1] == (sql, None)


def psycopg_connect_module():
    import psycopg

    return psycopg


def test_a_runtime_connect_handoff_is_allowed_on_upgrade_but_not_on_fresh():
    expected = tuple(sorted(executor._expected_tables()))
    handed_off = _hardened_acl() + tuple(
        (role, "CONNECT", False) for role in contract.RUNTIME_ROLES
    )
    facts = contract.DatabaseFacts(
        exists=True,
        owner="hh_owner",
        encoding="UTF8",
        collate="C",
        ctype="C",
        provider="c",
        allowconn=True,
        acl_materialized=True,
        acl_entries=handed_off,
        connect={
            role: contract.database_privilege(True, handed_off, role, "CONNECT")
            for role in contract.PROJECT_ROLE_NAMES
        },
        temporary={
            role: contract.database_privilege(True, handed_off, role, "TEMPORARY")
            for role in contract.PROJECT_ROLE_NAMES
        },
        sessions=(),
        probe_pid=None,
    )
    upgrade_live = _live(
        versions=("0013_user_text_size",), public_tables=expected, database=facts
    )
    authorization, issued = _authorization(upgrade_live)
    report = _executor(upgrade_live)[0].run(authorization, now=issued)
    assert report.plan == executor.UPGRADE

    fresh_live = _live(database=facts)
    authorization, issued = _authorization(fresh_live)
    with pytest.raises(executor.ExecutorError) as refused:
        _executor(fresh_live)[0].run(authorization, now=issued)
    assert refused.value.code == "database_fence"
