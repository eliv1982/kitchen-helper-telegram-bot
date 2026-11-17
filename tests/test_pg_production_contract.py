"""Production contract vs the accepted WP-1B/WP-1C golden fixtures (HH-DB-01 WP-2C-A).

The production classifier must be behaviorally identical to the accepted test-side
classifier, and the production artifact renderer must produce exactly the accepted text.
These tests import both implementations and compare them; neither is copied into the
other's runtime.
"""

from __future__ import annotations

import pytest

from house_helper.persistence import pg_artifacts as artifacts
from house_helper.persistence import pg_contract as contract
from tests import postgres_e4 as accepted_e4
from tests import postgres_sql as accepted_sql


HARDENED = (
    ("hh_owner", "CONNECT", False),
    ("hh_owner", "CREATE", False),
    ("hh_owner", "TEMPORARY", False),
    ("hh_migrate", "CONNECT", False),
)


def _accepted_connect(entries, materialized):
    return {
        role: accepted_e4.database_privilege(materialized, tuple(entries), role, "CONNECT")
        for role in accepted_e4.PROJECT_ROLE_NAMES
    }


def _accepted_temporary(entries, materialized):
    return {
        role: accepted_e4.database_privilege(materialized, tuple(entries), role, "TEMPORARY")
        for role in accepted_e4.PROJECT_ROLE_NAMES
    }


def _pair(**overrides):
    """One fixture as (production facts, accepted facts) with identical values."""
    values = dict(
        exists=True,
        owner="hh_owner",
        encoding="UTF8",
        collate="C",
        ctype="C",
        provider="c",
        allowconn=False,
        acl_materialized=False,
        acl_entries=(),
        sessions=(),
        probe_pid=None,
        target_inventory=None,
        subscriptions=(),
        observation=(),
    )
    values.update(overrides)
    entries = tuple(values["acl_entries"])
    materialized = values["acl_materialized"]
    connect = _accepted_connect(entries, materialized)
    temporary = _accepted_temporary(entries, materialized)
    production = contract.DatabaseFacts(
        **values, connect=connect, temporary=temporary
    )
    accepted = accepted_e4.DatabaseFacts(
        **values,
        connect=connect,
        temporary=temporary,
    )
    return production, accepted


FIXTURES = {
    "absent": dict(exists=False),
    "disabled_null_acl": dict(exists=True),
    "disabled_hardened": dict(
        exists=True, acl_materialized=True, acl_entries=HARDENED
    ),
    "enabled_hardened_unread": dict(
        exists=True, allowconn=True, acl_materialized=True, acl_entries=HARDENED
    ),
    "enabled_hardened_empty": dict(
        exists=True,
        allowconn=True,
        acl_materialized=True,
        acl_entries=HARDENED,
        target_inventory=accepted_e4.TargetInventory(()),
    ),
    "enabled_public": dict(
        exists=True,
        allowconn=True,
        acl_materialized=True,
        acl_entries=HARDENED + (("PUBLIC", "CONNECT", False),),
    ),
    "enabled_not_hardened": dict(exists=True, allowconn=True),
    "disabled_foreign": dict(
        exists=True,
        acl_materialized=True,
        acl_entries=HARDENED + (("someone_else", "CONNECT", False),),
    ),
    "wrong_owner": dict(exists=True, owner="toor", acl_materialized=True, acl_entries=HARDENED),
    "wrong_locale": dict(
        exists=True, collate="en_US.utf8", acl_materialized=True, acl_entries=HARDENED
    ),
    "subscription": dict(
        exists=True, acl_materialized=True, acl_entries=HARDENED, subscriptions=((1, "sub"),)
    ),
    "unexpected_object": dict(
        exists=True,
        allowconn=True,
        acl_materialized=True,
        acl_entries=HARDENED,
        target_inventory=accepted_e4.TargetInventory((("relation", "public", "foreign_table"),)),
    ),
}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_production_e4_matches_the_accepted_classifier(name):
    production, accepted = _pair(**FIXTURES[name])
    if isinstance(production.target_inventory, contract.TargetInventory):
        accepted_values = dict(FIXTURES[name])
        accepted_values["target_inventory"] = accepted_e4.TargetInventory(
            production.target_inventory.entries
        )
        production, accepted = _pair(**accepted_values)
    ours = contract.classify_facts(
        production, database="house_helper", grantee="hh_migrate"
    )
    theirs = accepted_e4.classify_facts(
        accepted, database="house_helper", grantee="hh_migrate"
    )
    assert (ours.state, ours.action, ours.failures) == (
        theirs.state,
        theirs.action,
        theirs.failures,
    )


def test_the_role_matrix_and_settings_equal_the_accepted_constants():
    assert dict(contract.ROLE_MATRIX) == dict(accepted_sql.ROLE_MATRIX)
    assert dict(contract.ROLE_SETTINGS) == dict(accepted_sql.ROLE_SETTINGS)
    assert dict(contract.EXPECTED_EDGES) == dict(accepted_sql.EXPECTED_EDGES)
    assert contract.PROJECT_ROLE_NAMES == accepted_sql.PROJECT_ROLE_NAMES


def test_the_bootstrap_and_schema_rendering_equals_the_accepted_renderer():
    assert artifacts.render_bootstrap("house_helper", "hh_migrate") == accepted_sql.render_bootstrap(
        "house_helper", "hh_migrate"
    )
    assert artifacts.render_schema("house_helper") == accepted_sql.render_schema("house_helper")
    assert artifacts.render_privileges("house_helper") == accepted_sql.render_privileges(
        "house_helper"
    )
    assert artifacts.render_roles() == accepted_sql._load("roles.sql")


def test_the_renderer_never_interpolates_an_unvalidated_identifier():
    for bad in ("house_helper; DROP DATABASE postgres", "HouseHelper", "x" * 64, 7):
        with pytest.raises(artifacts.ArtifactError):
            artifacts.render_schema(bad)
        with pytest.raises(artifacts.ArtifactError):
            artifacts.render_bootstrap(bad, "hh_migrate")
    with pytest.raises(artifacts.ArtifactError):
        artifacts.render_bootstrap("house_helper", "hh_app")
    with pytest.raises(artifacts.ArtifactError):
        artifacts.render_bootstrap("house_helper_restore_x", "hh_migrate")


def _facts(entries, *, materialized=True, connect=None):
    return contract.DatabaseFacts(
        exists=True,
        owner="hh_owner",
        encoding="UTF8",
        collate="C",
        ctype="C",
        provider="c",
        allowconn=True,
        acl_materialized=materialized,
        acl_entries=tuple(entries),
        connect=connect
        or {
            role: contract.database_privilege(materialized, tuple(entries), role, "CONNECT")
            for role in contract.PROJECT_ROLE_NAMES
        },
        temporary={
            role: contract.database_privilege(materialized, tuple(entries), role, "TEMPORARY")
            for role in contract.PROJECT_ROLE_NAMES
        },
        sessions=(),
        probe_pid=None,
    )


def test_the_initialization_fence_requires_the_runtime_handoffs_to_be_absent():
    facts = _facts(HARDENED)
    assert contract.migration_fence_failures(facts) == ()
    handed_off = HARDENED + tuple(
        (role, "CONNECT", False) for role in contract.RUNTIME_ROLES
    )
    facts = _facts(handed_off)
    failures = contract.migration_fence_failures(facts)
    assert "runtime_connect:hh_app" in failures


def test_a_public_entry_or_a_foreign_grantee_breaks_the_fence():
    public = _facts(HARDENED + (("PUBLIC", "CONNECT", False),))
    assert contract.FAIL_PUBLIC_ACCESS in contract.database_acl_failures(
        database="house_helper", facts=public, grantee="hh_migrate"
    )
    foreign = _facts(HARDENED + (("outsider", "CONNECT", False),))
    assert contract.FAIL_UNEXPECTED_GRANTS in contract.database_acl_failures(
        database="house_helper", facts=foreign, grantee="hh_migrate"
    )


def test_marker_matching_checks_both_the_token_and_the_identifier():
    from house_helper.persistence import pg_journal

    text = pg_journal.make_marker("f" * 32, "726485930221")
    contract.marker_matches(text, marker="f" * 32, system_identifier="726485930221")
    with pytest.raises(contract.ContractError) as wrong_token:
        contract.marker_matches(text, marker="0" * 32, system_identifier="726485930221")
    assert wrong_token.value.code == "marker_mismatch"
    with pytest.raises(contract.ContractError) as wrong_id:
        contract.marker_matches(text, marker="f" * 32, system_identifier="111")
    assert wrong_id.value.code == "marker_identifier"
