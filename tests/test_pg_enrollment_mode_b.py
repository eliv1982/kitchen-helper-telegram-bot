"""Offline Mode B tests: authoritative verification, mismatch refusal, no fallback.

Frozen source: §4.1 (Mode B never adopts, never falls back), ENR-15/ENR-16, ENR-17.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from house_helper.persistence import pg_artifacts
from house_helper.persistence import pg_contract as contract
from house_helper.persistence import pg_enrollment as enrollment
from house_helper.persistence import pg_journal as journal
from tests.test_pg_enrollment_mode_a import FakeCluster, _role, _run


class FakeAdapter:
    def __init__(self, cluster: FakeCluster) -> None:
        self.cluster = cluster

    def mode_b_observation(self) -> enrollment.Observation:
        return self.cluster.observe(read_target=True)


def _enrolled(tmp_path):
    cluster = FakeCluster()
    journal_file, _ = _run(cluster, tmp_path)
    return cluster, journal_file


def test_mode_b_accepts_the_unchanged_enrolled_cluster(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    report = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert report.ok is True
    assert report.mismatches == ()
    assert report.completed["state"] == "completed"


def test_verify_mode_b_builds_a_bounded_non_secret_authorization(tmp_path):
    from deploy.postgres.enroll import verify_mode_b

    cluster, journal_file = _enrolled(tmp_path)
    report = verify_mode_b(journal_file, FakeAdapter(cluster))
    assert report.exit_code == 0 and report.authorization
    raw = report.authorization
    value = journal.decode_authorization(raw)
    assert value["mode"] == "B" and value["verdict"] == "verified"
    assert value["cluster"]["system_identifier"] == cluster.system_identifier
    assert value["marker"] == cluster.marker
    assert value["databases"] == sorted(cluster.databases)
    assert value["artifacts"] == dict(pg_artifacts.manifest())
    assert value["journal"]["seq"] == journal_file.read().head_seq
    assert b"password" not in raw.lower()


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda cluster: setattr(cluster, "container_id", "d" * 64), "container:id"),
        (
            lambda cluster: setattr(cluster, "system_identifier", "111111111111"),
            "system_identifier",
        ),
        (lambda cluster: setattr(cluster, "marker", ""), "marker:absent"),
        (
            lambda cluster: setattr(
                cluster, "marker", journal.make_marker("a" * 32, cluster.system_identifier)
            ),
            "marker:token",
        ),
        (
            lambda cluster: cluster.databases.add("house_helper_restore_abcd"),
            "unexpected_database:house_helper_restore_abcd",
        ),
        (
            lambda cluster: cluster.roles.__setitem__("hh_evil", _role("hh_app")),
            "unexpected:hh_evil",
        ),
        (
            lambda cluster: cluster.roles.__setitem__(
                "hh_app", replace(_role("hh_app"), superuser=True)
            ),
            "attributes:hh_app",
        ),
    ],
)
def test_mode_b_refuses_each_identity_and_inventory_mismatch(tmp_path, mutate, expected):
    cluster, journal_file = _enrolled(tmp_path)
    mutate(cluster)
    before = len(journal_file.read().records)
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is False
    assert expected in verdict.mismatches
    # The caller journals the mismatch; Mode B itself writes nothing and falls back to
    # nothing. There is no new intent record.
    record = enrollment.record_mismatch(journal_file, verdict)
    assert record.kind == "cluster_identity"
    assert record.payload["items"] == list(verdict.mismatches)
    assert len(journal_file.read().records) == before + 1
    assert not any(
        item.kind == "cluster_enrollment" and item.payload.get("state") == "intent"
        for item in journal_file.read().records[before:]
    )


def test_mode_b_without_a_completed_record_never_falls_back_to_mode_a(tmp_path):
    cluster = FakeCluster()
    journal_file = journal.Journal(tmp_path / "cutover")
    verdict = enrollment.mode_b(journal_file.read().records, cluster.observe(read_target=True))
    assert verdict.ok is False
    assert verdict.mismatches == ("completed_record:missing",)
    assert journal_file.read().records == ()


def test_a_duplicate_completed_record_is_refused(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    completed = journal_file.read().of_kind("cluster_enrollment")[-1]
    journal_file.append("cluster_enrollment", dict(completed.payload))
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is False
    assert verdict.mismatches == ("completed_record:duplicate",)


def _initialized_inventory(*, toast: bool = True):
    from house_helper.persistence import pg_contract as contract

    entries = [
        ("relation", "public", name)
        for name in sorted(contract.expected_relation_names())
    ]
    if toast:
        entries.extend(
            [
                ("relation", "pg_toast", "pg_toast_16390"),
                ("relation", "pg_toast", "pg_toast_16390_index"),
            ]
        )
    entries.append(("default acl", "", "oid:16400"))
    return contract.TargetInventory(tuple(entries))


def _expected_default_acls():
    from house_helper.persistence import pg_contract as contract

    return tuple(("hh_owner",) + entry for entry in contract.EXPECTED_DEFAULT_ACLS)


def _mark_initialized(cluster, *, version=None):
    from house_helper.persistence import pg_contract as contract

    head, _chart = contract.known_revision_chart()
    cluster.schema_versions = (version or head,)
    cluster.public_tables = tuple(sorted(contract.expected_tables()))
    cluster.inventory = _initialized_inventory()
    cluster.default_acls = _expected_default_acls()


def test_mode_b_accepts_a_completed_house_helper_schema(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    _mark_initialized(cluster)
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is True, verdict.mismatches


def test_mode_b_accepts_a_known_older_revision_awaiting_upgrade(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    _mark_initialized(cluster, version="0013_user_text_size")
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is True, verdict.mismatches


def test_mode_b_accepts_a_recognized_partial_initialization(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    # BOOT-04's schema/default-ACL contract committed, the Alembic chain did not.
    cluster.schema_versions = ()
    cluster.public_tables = ()
    cluster.inventory = contract.TargetInventory((("default acl", "", "oid:16400"),))
    cluster.default_acls = _expected_default_acls()
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is True, verdict.mismatches


def test_mode_b_refuses_an_initialized_database_with_a_foreign_table(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    _mark_initialized(cluster)
    cluster.inventory = contract.TargetInventory(
        tuple(cluster.inventory.entries) + (("relation", "public", "foreign_table"),)
    )
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is False
    assert "unexpected_object:relation:public:foreign_table" in verdict.mismatches


def test_mode_b_refuses_an_initialized_database_with_a_bad_default_acl(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    _mark_initialized(cluster)
    cluster.default_acls = _expected_default_acls()[:-1]
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is False
    assert any(item.startswith("missing_default_acl:") for item in verdict.mismatches)


def test_mode_b_refuses_an_unrelated_superuser(tmp_path):
    from tests.test_pg_enrollment_mode_a import bootstrap_role

    cluster, journal_file = _enrolled(tmp_path)
    cluster.roles["unrelated_admin"] = replace(
        bootstrap_role(), name="unrelated_admin"
    )
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is False
    assert "unexpected_role:unrelated_admin" in verdict.mismatches


def test_mode_b_verifies_the_predefined_membership_baseline_separately(tmp_path):
    from house_helper.persistence import pg_contract as contract

    cluster, journal_file = _enrolled(tmp_path)
    # The real PG16 structure (pg_monitor -> three read/stat roles) is accepted...
    assert contract.predefined_membership_failures(
        cluster.memberships, cluster.predefined_roles
    ) == ()
    # ...but a project-role grant of a predefined role is still a delegation failure.
    cluster.memberships = cluster.memberships + (
        contract.MembershipEdge("hh_app", "pg_monitor", False, False, False),
    )
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is False
    assert "unexpected_edge:hh_app->pg_monitor" in verdict.mismatches


def test_mode_b_refuses_a_changed_predefined_baseline(tmp_path):
    from house_helper.persistence import pg_contract as contract

    cluster, journal_file = _enrolled(tmp_path)
    cluster.predefined_roles = tuple(
        name
        for name in contract.FROZEN_PREDEFINED_ROLES
        if name != "pg_monitor"
    )
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is False
    assert "missing_predefined:pg_monitor" in verdict.mismatches


def test_an_extra_target_object_is_refused(tmp_path):
    from house_helper.persistence import pg_contract as contract

    cluster, journal_file = _enrolled(tmp_path)
    cluster.inventory = contract.TargetInventory(
        (("relation", "public", "foreign_table"),)
    )
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is False
    assert "unexpected_object:relation:public:foreign_table" in verdict.mismatches


def test_the_mismatch_record_names_items_not_values(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    cluster.container_id = "e" * 64
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    record = enrollment.record_mismatch(journal_file, verdict)
    serialized = journal.canonical_json(dict(record.payload)).decode("utf-8")
    assert record.payload["items"] == ["container:id"]
    assert "e" * 64 not in serialized
    assert "726485930221" not in serialized


def test_an_active_temporary_database_record_authorizes_its_exact_name(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    name = "house_helper_restore_abcd1234"
    journal_file.append(
        "temp_db",
        {
            "action": "authorized",
            "operation_id": "a" * 32,
            "token": "abcd1234",
            "database": name,
            "purpose": "restore_verification",
            "system_identifier": cluster.system_identifier,
        },
    )
    cluster.databases.add(name)
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is True, verdict.mismatches


def test_a_temporary_database_record_for_another_cluster_authorizes_nothing(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    name = "house_helper_restore_abcd1234"
    journal_file.append(
        "temp_db",
        {
            "action": "authorized",
            "operation_id": "a" * 32,
            "token": "abcd1234",
            "database": name,
            "purpose": "restore_verification",
            "system_identifier": "1",
        },
    )
    cluster.databases.add(name)
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert verdict.ok is False
    assert f"temp_database_identity:{name}" in verdict.mismatches
    assert f"unexpected_database:{name}" in verdict.mismatches


def test_a_retired_temporary_database_record_authorizes_nothing(tmp_path):
    cluster, journal_file = _enrolled(tmp_path)
    name = "house_helper_restore_abcd1234"
    journal_file.append(
        "temp_db",
        {
            "action": "retired",
            "database": name,
            "system_identifier": cluster.system_identifier,
        },
    )
    cluster.databases.add(name)
    verdict = enrollment.mode_b(
        journal_file.read().records, cluster.observe(read_target=True)
    )
    assert f"unexpected_database:{name}" in verdict.mismatches
