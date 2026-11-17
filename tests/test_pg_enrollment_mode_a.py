"""Offline Mode A / E0-E7 tests with a controlled cluster fixture (HH-DB-01 WP-2C-A).

The fixture implements the same `Cluster` protocol as `deploy/postgres/enroll.py`'s
Docker adapter. It changes its observed state only in the four mutation methods, so a
resume test exercises the real dispatch over the real journal, without Docker or SQL.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from house_helper.persistence import pg_contract as contract
from house_helper.persistence import pg_enrollment as enrollment
from house_helper.persistence import pg_journal as journal

BASE = tuple(contract.BASE_DATABASES)


def _predefined_edges() -> tuple[contract.MembershipEdge, ...]:
    return tuple(
        contract.MembershipEdge(member, granted, admin, inherit, set_option)
        for member, granted, admin, inherit, set_option in (
            contract.FROZEN_PREDEFINED_MEMBERSHIPS
        )
    )


def _role(name: str) -> contract.RoleFacts:
    matrix = contract.ROLE_MATRIX[name]
    return contract.RoleFacts(
        name=name,
        login=matrix["login"],
        superuser=matrix["superuser"],
        createdb=matrix["createdb"],
        createrole=matrix["createrole"],
        inherit=matrix["inherit"],
        replication=matrix["replication"],
        bypassrls=matrix["bypassrls"],
        connlimit=-1,
        validuntil=None,
        settings=tuple(sorted(contract.ROLE_SETTINGS[name])),
    )


def bootstrap_role() -> contract.RoleFacts:
    return contract.RoleFacts(
        name="postgres",
        login=True,
        superuser=True,
        createdb=True,
        createrole=True,
        inherit=True,
        replication=False,
        bypassrls=False,
        connlimit=-1,
        validuntil=None,
        settings=(),
    )


def _hardened_acl() -> tuple[tuple[str, str, bool], ...]:
    return (
        ("hh_owner", "CONNECT", False),
        ("hh_owner", "CREATE", False),
        ("hh_owner", "TEMPORARY", False),
        ("hh_migrate", "CONNECT", False),
    )


def _connect(entries, *, materialized: bool, role: str, privilege: str = "CONNECT") -> bool:
    return contract.database_privilege(materialized, tuple(entries), role, privilege)


def _public_schema_acl(*, hardened: bool) -> tuple:
    """The `public` schema observation `db/schema.sql` converges to (or the pre-state)."""
    usage = tuple(True for _ in contract.PROJECT_ROLE_NAMES)
    if hardened:
        create = tuple(
            role == contract.OWNER_ROLE for role in contract.PROJECT_ROLE_NAMES
        )
        return ("hh_owner", 0) + usage + create
    create = tuple(True for _ in contract.PROJECT_ROLE_NAMES)
    return ("pg_database_owner", 2) + usage + create


class FakeCluster:
    def __init__(self, *, system_identifier: str = "726485930221") -> None:
        self.system_identifier = system_identifier
        self.container_id = "a" * 64
        self.databases = set(BASE)
        self.roles = {"postgres": bootstrap_role()}
        self.predefined_roles = contract.FROZEN_PREDEFINED_ROLES
        self.memberships = _predefined_edges()
        self.marker = ""
        self.allowconn = False
        self.materialized = False
        self.acl_entries = ()
        self.inventory = None
        self.default_acls = ()
        self.schema_versions = ()
        self.public_tables = ()
        self.schema_acl = _public_schema_acl(hardened=False)
        self.operations: list[str] = []
        self.fail_next: str | None = None
        self.target_reads = 0
        # The identity the schema step is asked to bind to (F8). A test can override the
        # observed identity here to simulate a change between the E4 dispatch and the
        # schema authorization.
        self.schema_expected: enrollment.ClusterIdentity | None = None
        self.schema_identity_override: enrollment.ClusterIdentity | None = None

    # -- the protocol ------------------------------------------------------------------

    def identity(self) -> enrollment.ClusterIdentity:
        return enrollment.ClusterIdentity(
            container_name="house-helper-db",
            container_id=self.container_id,
            image_digest="postgres@sha256:" + "b" * 64,
            volume="house-helper-pgdata",
            system_identifier=self.system_identifier,
        )

    def observe(self, *, read_target: bool = False) -> enrollment.Observation:
        if read_target:
            self.target_reads += 1
        facts = None
        if contract.SERVING_DATABASE in self.databases:
            entries = tuple(self.acl_entries)
            connect = {
                role: _connect(entries, materialized=self.materialized, role=role)
                for role in contract.PROJECT_ROLE_NAMES
            }
            temporary = {
                role: _connect(
                    entries, materialized=self.materialized, role=role, privilege="TEMPORARY"
                )
                for role in contract.PROJECT_ROLE_NAMES
            }
            facts = contract.DatabaseFacts(
                exists=True,
                owner=contract.OWNER_ROLE,
                encoding=contract.EXPECTED_ENCODING,
                collate="C",
                ctype="C",
                provider="c",
                allowconn=self.allowconn,
                acl_materialized=self.materialized,
                acl_entries=entries,
                connect=connect,
                temporary=temporary,
                sessions=(),
                probe_pid=None,
                target_inventory=self.inventory if read_target else None,
                subscriptions=(),
            )
        return enrollment.Observation(
            identity=self.identity(),
            databases=tuple(sorted(self.databases)),
            roles=dict(self.roles),
            memberships=self.memberships,
            predefined_roles=self.predefined_roles,
            database=facts,
            marker=self.marker,
            target_inventory=self.inventory if read_target else None,
            schema_versions=self.schema_versions if read_target else (),
            public_tables=self.public_tables if read_target else (),
            default_acls=self.default_acls if read_target else (),
            schema_acl=self.schema_acl if read_target else None,
        )

    def apply_roles(self) -> None:
        self._mutate("roles")
        self.roles = {name: _role(name) for name in contract.PROJECT_ROLE_NAMES}
        self.roles["postgres"] = bootstrap_role()
        self.memberships = _predefined_edges() + (
            contract.MembershipEdge("hh_migrate", "hh_owner", False, False, True),
            contract.MembershipEdge("hh_restore", "hh_owner", False, False, True),
        )

    def create_database(self) -> None:
        self._mutate("create_database")
        self.databases.add(contract.SERVING_DATABASE)
        self.allowconn = False
        self.materialized = False
        self.acl_entries = ()
        self.inventory = contract.TargetInventory(())

    def harden_database(self) -> None:
        self._mutate("harden_database")
        self.materialized = True
        self.acl_entries = _hardened_acl()

    def enable_database(self) -> None:
        self._mutate("enable_database")
        self.allowconn = True

    def harden_schema(self, expected: enrollment.ClusterIdentity) -> None:
        # F8: the production adapter re-observes the cluster identity and refuses unless it
        # is still the enrollment intent's before it issues the schema authorization.
        self.schema_expected = expected
        observed = self.schema_identity_override or self.identity()
        if observed != expected:
            differing = [
                name
                for name in (
                    "container_name",
                    "container_id",
                    "image_digest",
                    "volume",
                    "system_identifier",
                )
                if getattr(observed, name) != getattr(expected, name)
            ]
            raise enrollment.EnrollmentError(
                "cluster_identity",
                "the live cluster identity is not the enrollment intent's",
                ", ".join(differing),
            )
        # The ENR-04 schema/default-ACL half: db/schema.sql under hh_migrate -> hh_owner.
        self._mutate("harden_schema")
        self.default_acls = tuple(
            ("hh_owner",) + entry for entry in contract.EXPECTED_DEFAULT_ACLS
        )
        self.inventory = contract.TargetInventory((("default acl", "", "oid:16400"),))
        self.schema_acl = _public_schema_acl(hardened=True)

    def write_marker(self, marker: str) -> None:
        self._mutate("write_marker")
        self.marker = marker

    # -- fixture control ----------------------------------------------------------------

    def _mutate(self, name: str) -> None:
        self.operations.append(name)
        if self.fail_next == name:
            self.fail_next = None
            raise RuntimeError(f"injected failure at {name}")


def _run(cluster: FakeCluster, tmp_path: Path, *, name: str = "cutover"):
    journal_file = journal.Journal(tmp_path / name)
    return journal_file, enrollment.ModeA(
        journal_file,
        cluster,
        approved_by="owner-test",
        token_source=lambda: "0" * 32,
    ).run()


def test_fresh_mode_a_follows_the_accepted_order(tmp_path):
    cluster = FakeCluster()
    journal_file, report = _run(cluster, tmp_path)
    assert cluster.operations == [
        "roles",
        "create_database",
        "harden_database",
        "enable_database",
        # F8/ENR-04: the schema/default-ACL half runs after E3-c and before the marker.
        "harden_schema",
        "write_marker",
    ]
    assert report.state == enrollment.E7
    # F8: the schema step is bound to the identity the intent recorded.
    assert cluster.schema_expected == cluster.identity()
    states = [record.payload.get("state") for record in journal_file.read().records]
    assert states == ["intent", "roles", "database", "hardened", "marker", "completed"]
    completed = journal_file.read().of_kind("cluster_enrollment")[-1].payload
    assert completed["system_identifier"] == cluster.system_identifier
    assert journal.parse_marker(completed["marker"])[1] == cluster.system_identifier
    assert completed["approved_by"] == "owner-test"


def test_concurrent_callers_cannot_create_competing_intents(tmp_path):
    cluster = FakeCluster()
    journal_file = journal.Journal(tmp_path / "cutover")
    journal_file.prepare()
    held = journal_file.operation_lock()
    held.__enter__()
    try:
        with pytest.raises(journal.JournalError) as refused:
            enrollment.ModeA(
                journal_file, cluster, token_source=lambda: "7" * 32
            ).run()
        assert refused.value.code == "operation_busy"
        assert journal_file.read().records == ()
    finally:
        held.__exit__(None, None, None)
    # Once the interval is released, a legitimate first run records exactly one intent.
    report = enrollment.ModeA(
        journal_file, cluster, token_source=lambda: "7" * 32
    ).run()
    assert report.state == enrollment.E7
    intents = [
        record
        for record in journal_file.read().records
        if record.kind == "cluster_enrollment" and record.payload.get("state") == "intent"
    ]
    assert len(intents) == 1


def test_a_journal_head_change_before_the_intent_is_fail_closed(tmp_path, monkeypatch):
    cluster = FakeCluster()
    journal_file = journal.Journal(tmp_path / "cutover")

    original = journal_file.append_if_head

    def changed_head(expected_head, expected_seq, kind, payload, *, at=None):
        # Another writer moved the head after this run observed E0. Let the real
        # compare-and-append see a stale expectation.
        journal_file.append("cluster_enrollment", {"state": "intent", "foreign": True})
        return original(expected_head, expected_seq, kind, payload, at=at)

    monkeypatch.setattr(journal_file, "append_if_head", changed_head)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "8" * 32).run()
    assert refused.value.code in ("journal_head_changed", "enrollment_refused")
    intents = [
        record
        for record in journal_file.read().records
        if record.kind == "cluster_enrollment" and record.payload.get("state") == "intent"
    ]
    assert len(intents) == 1


def test_a_non_pristine_cluster_is_never_adopted(tmp_path):
    cluster = FakeCluster()
    cluster.roles["hh_evil"] = _role("hh_app")
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster).run()
    assert "unexpected_role:hh_evil" in str(refused.value)
    assert journal_file.read().records == ()
    assert cluster.operations == []


def test_a_foreign_superuser_before_the_intent_fails_closed(tmp_path):
    cluster = FakeCluster()
    cluster.roles["unrelated_admin"] = replace(bootstrap_role(), name="unrelated_admin")
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster).run()
    assert "unexpected_role:unrelated_admin" in str(refused.value)
    assert journal_file.read().records == ()
    assert cluster.operations == []


def test_a_foreign_role_in_a_partial_enrollment_fails_closed(tmp_path):
    cluster = FakeCluster()
    cluster.fail_next = "create_database"
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(RuntimeError):
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "6" * 32).run()
    cluster.roles["unrelated_admin"] = replace(bootstrap_role(), name="unrelated_admin")
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster).run()
    assert "unexpected_role:unrelated_admin" in str(refused.value)
    assert cluster.operations.count("create_database") == 1


def test_the_frozen_pg16_predefined_structure_is_accepted_and_recorded(tmp_path):
    cluster = FakeCluster()
    journal_file, _report = _run(cluster, tmp_path)
    completed = journal_file.read().of_kind("cluster_enrollment")[-1].payload
    assert completed["predefined_roles"] == list(contract.FROZEN_PREDEFINED_ROLES)
    assert completed["predefined_memberships"] == [
        {
            "member": member,
            "granted": granted,
            "admin": admin,
            "inherit": inherit,
            "set": set_option,
        }
        for member, granted, admin, inherit, set_option in (
            contract.FROZEN_PREDEFINED_MEMBERSHIPS
        )
    ]


def test_completion_refuses_an_unhardened_database(tmp_path):
    cluster = FakeCluster()
    journal_file, _report = _run(cluster, tmp_path)
    completed = journal_file.read().of_kind("cluster_enrollment")[-1]
    cluster.materialized = False
    observation = cluster.observe(read_target=True)
    failures = enrollment._completion_failures(completed.payload, observation)
    assert any(
        item in failures
        for item in (
            contract.FAIL_ACL_NOT_HARDENED,
            "owner_connect_missing",
            "migrate_connect_missing",
        )
    )


def test_completion_requires_the_exact_rol14_default_acl_matrix(tmp_path):
    cluster = FakeCluster()
    journal_file, _report = _run(cluster, tmp_path)
    completed = journal_file.read().of_kind("cluster_enrollment")[-1]
    # F8/ENR-04: the exact ROL-14 default ACLs are part of the completed enrollment, so a
    # matrix with a missing entry is refused (it used to be refused only for existing).
    cluster.default_acls = tuple(
        ("hh_owner",) + entry for entry in contract.EXPECTED_DEFAULT_ACLS
    )[:-1]
    observation = cluster.observe(read_target=True)
    failures = enrollment._completion_failures(completed.payload, observation)
    assert any(item.startswith("missing_default_acl:") for item in failures), failures


def test_a_foreign_database_before_the_intent_fails_closed(tmp_path):
    cluster = FakeCluster()
    cluster.databases.add("someone_else")
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster).run()
    assert "unexpected_database:someone_else" in str(refused.value)
    assert journal_file.read().records == ()


def test_resume_after_an_interrupted_role_creation(tmp_path):
    cluster = FakeCluster()
    cluster.fail_next = "create_database"
    journal_file = journal.Journal(tmp_path / "cutover")
    mode = enrollment.ModeA(journal_file, cluster, token_source=lambda: "0" * 32)
    with pytest.raises(RuntimeError):
        mode.run()
    # The intent and the roles milestone are durable; the database is not.
    assert [record.payload.get("state") for record in journal_file.read().records] == [
        "intent",
        "roles",
    ]
    assert contract.SERVING_DATABASE not in cluster.databases
    second = enrollment.ModeA(journal_file, cluster, token_source=lambda: "1" * 32)
    report = second.run()
    assert report.state == enrollment.E7
    # The resumed run does not create the roles again.
    assert cluster.operations.count("roles") == 1
    assert cluster.operations.count("create_database") == 2


def test_mode_a_binds_the_intent_identity_before_the_schema_step(tmp_path):
    cluster = FakeCluster()
    journal_file = journal.Journal(tmp_path / "cutover")
    # The dispatch observed the intent's identity, but the live identity changes before the
    # schema authorization would be issued. F8 requires the adapter to refuse, not to
    # authorize the schema step against the newly observed cluster.
    cluster.schema_identity_override = replace(
        cluster.identity(), system_identifier="999999999999"
    )
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(
            journal_file, cluster, token_source=lambda: "9" * 32
        ).run()
    assert refused.value.code == "cluster_identity"
    assert "system_identifier" in str(refused.value)
    assert cluster.schema_expected == cluster.identity()
    # E3-c is complete, but neither the schema half nor any later milestone occurred.
    assert "harden_schema" not in cluster.operations
    assert [record.payload.get("state") for record in journal_file.read().records] == [
        "intent",
        "roles",
        "database",
    ]
    assert cluster.default_acls == ()
    assert cluster.marker == ""


def test_a_schema_hardening_failure_stays_resumable_before_e7(tmp_path):
    cluster = FakeCluster()
    cluster.fail_next = "harden_schema"
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(RuntimeError):
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "a" * 32).run()
    # E3-c completed, but neither the `hardened` milestone nor the marker exists: the
    # enrollment is not E7 and cannot be mistaken for one.
    assert [record.payload.get("state") for record in journal_file.read().records] == [
        "intent",
        "roles",
        "database",
    ]
    assert cluster.allowconn is True
    assert cluster.default_acls == ()
    assert cluster.marker == ""
    plan = enrollment.dispatch(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert plan.state == enrollment.E4
    assert plan.action == enrollment.ACTION_INITIALIZE_SCHEMA
    # Resume does not repeat the accepted database-level phases.
    report = enrollment.ModeA(journal_file, cluster, token_source=lambda: "b" * 32).run()
    assert report.state == enrollment.E7
    assert cluster.operations.count("create_database") == 1
    assert cluster.operations.count("harden_database") == 1
    assert cluster.operations.count("enable_database") == 1
    assert cluster.operations.count("harden_schema") == 2
    assert [record.payload.get("state") for record in journal_file.read().records] == [
        "intent",
        "roles",
        "database",
        "hardened",
        "marker",
        "completed",
    ]


def test_an_interruption_between_schema_hardening_and_its_milestone_is_reconciled(
    tmp_path, monkeypatch
):
    cluster = FakeCluster()
    journal_file = journal.Journal(tmp_path / "cutover")
    original = enrollment.ModeA._append_at_view

    def fail_before_hardened(self, view, kind, payload):
        if payload.get("state") == "hardened":
            # The schema transaction committed; the journal acknowledgment did not.
            raise RuntimeError("injected crash before the hardened milestone")
        return original(self, view, kind, payload)

    monkeypatch.setattr(enrollment.ModeA, "_append_at_view", fail_before_hardened)
    with pytest.raises(RuntimeError):
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "c" * 32).run()
    monkeypatch.setattr(enrollment.ModeA, "_append_at_view", original)
    assert cluster.default_acls  # the ENR-04 schema half really did commit
    assert [record.payload.get("state") for record in journal_file.read().records] == [
        "intent",
        "roles",
        "database",
    ]
    report = enrollment.ModeA(journal_file, cluster, token_source=lambda: "d" * 32).run()
    assert report.state == enrollment.E7
    records = journal_file.read().records
    assert any(
        record.kind == "reconcile" and record.payload.get("observed_state") == "hardened"
        for record in records
    ), [record.payload for record in records]
    # The reconciled resume did not re-execute schema.sql: it observed the committed state.
    assert cluster.operations.count("harden_schema") == 1


def test_a_wrong_public_schema_owner_is_refused_before_completion(tmp_path):
    cluster = FakeCluster()
    cluster.fail_next = "harden_schema"
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(RuntimeError):
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "f" * 32).run()
    # The default ACLs exist, but `public` was never re-owned (a schema half that the
    # enrollment must not accept as complete).
    cluster.default_acls = tuple(
        ("hh_owner",) + entry for entry in contract.EXPECTED_DEFAULT_ACLS
    )
    cluster.inventory = contract.TargetInventory((("default acl", "", "oid:16400"),))
    cluster.schema_acl = ("pg_database_owner", 0) + tuple(
        True for _ in contract.PROJECT_ROLE_NAMES
    ) + tuple(True for _ in contract.PROJECT_ROLE_NAMES)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster).run()
    assert "schema:ownership" in str(refused.value)
    assert not any(
        record.payload.get("state") == "completed"
        for record in journal_file.read().records
    )


def test_a_marker_that_precedes_the_schema_hardening_is_refused(tmp_path):
    cluster = FakeCluster()
    cluster.fail_next = "harden_schema"
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(RuntimeError):
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "e" * 32).run()
    # E3-c is complete, but the marker appeared without ENR-04's schema/default-ACL half.
    cluster.marker = journal.make_marker("e" * 32, cluster.system_identifier)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster).run()
    assert "marker:present_without_schema" in str(refused.value)


def test_resume_after_an_interrupted_marker_writes_only_the_marker(tmp_path):
    cluster = FakeCluster()
    cluster.fail_next = "write_marker"
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(RuntimeError):
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "2" * 32).run()
    assert cluster.marker == ""
    assert [record.payload.get("state") for record in journal_file.read().records] == [
        "intent",
        "roles",
        "database",
        "hardened",
    ]
    report = enrollment.ModeA(
        journal_file, cluster, token_source=lambda: "3" * 32
    ).run()
    assert report.state == enrollment.E7
    assert cluster.operations.count("write_marker") == 2
    assert cluster.operations.count("create_database") == 1


def test_a_different_system_identifier_is_not_resumed(tmp_path):
    cluster = FakeCluster()
    cluster.fail_next = "create_database"
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(RuntimeError):
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "0" * 32).run()
    cluster.system_identifier = "999999999999"
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster).run()
    assert "system_identifier" in str(refused.value)
    assert cluster.operations.count("create_database") == 1


def test_a_drifted_existing_role_is_refused_never_repaired(tmp_path):
    cluster = FakeCluster()
    cluster.fail_next = "create_database"
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(RuntimeError):
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "0" * 32).run()
    cluster.roles["hh_app"] = replace(_role("hh_app"), superuser=True)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster).run()
    assert "attributes:hh_app" in str(refused.value)
    assert cluster.operations.count("roles") == 1


def test_a_marker_on_a_disabled_database_is_inconsistent(tmp_path):
    cluster = FakeCluster()
    cluster.fail_next = "enable_database"
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(RuntimeError):
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "4" * 32).run()
    # A marker cannot precede enablement in the accepted order.
    cluster.marker = journal.make_marker("4" * 32, cluster.system_identifier)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster).run()
    assert "marker:present_while_disabled" in str(refused.value)


def test_an_existing_completed_record_makes_mode_a_refuse(tmp_path):
    cluster = FakeCluster()
    journal_file, _ = _run(cluster, tmp_path)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, cluster).run()
    assert refused.value.code == "already_enrolled"


def test_an_observed_milestone_ahead_of_the_journal_is_reconciled(tmp_path):
    cluster = FakeCluster()
    cluster.fail_next = "write_marker"
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(RuntimeError):
        enrollment.ModeA(journal_file, cluster, token_source=lambda: "5" * 32).run()
    journal_before = [record.payload.get("state") for record in journal_file.read().records]
    assert journal_before[-1] == "hardened"
    # The marker appeared without a journal acknowledgment; the state is observed, never
    # inferred from the absence of a record.
    cluster.marker = journal.make_marker("5" * 32, cluster.system_identifier)
    report = enrollment.ModeA(journal_file, cluster).run()
    assert report.state == enrollment.E7
    records = journal_file.read().records
    assert any(
        record.kind == "reconcile" and record.payload.get("observed_state") == "marker"
        for record in records
    )


def test_a_journal_state_ahead_of_the_observation_fails_closed(tmp_path):
    cluster = FakeCluster()
    journal_file, _ = _run(cluster, tmp_path)
    # Simulate a cluster whose project roles disappeared while the journal says complete.
    cluster.roles = {}
    observation = cluster.observe(read_target=False)
    plan = enrollment.dispatch(journal_file.read().records, observation)
    # A completed record is Mode B only; dispatch does not adopt the changed cluster.
    assert plan.state == enrollment.E7
    verdict = enrollment.mode_b(journal_file.read().records, observation)
    assert verdict.ok is False
    assert any(item.startswith("missing:") for item in verdict.mismatches)
