"""HH-DB-01 Stage 2 WP-1C-B - the E4 state classifier (tests/postgres_e4.py).

Two layers, deliberately separated:

* the PURE decision rule (`classify_facts`) is tested with hand-built `DatabaseFacts`.
  Every BOOT-07 row, every §5.2 boundary, every stable failure code and the exact
  action of each verdict is asserted here. These tests need no server and run in the
  ordinary suite. Every hand-built fact set is consistent with the PostgreSQL 16
  privilege model (`tests/postgres_e4.py::database_privilege`), which the live
  `TestPrivilegeModel` pins against the server; an impossible ACL/effective-privilege
  combination therefore cannot be used as a fixture (Codex R1 F4).
* the OBSERVATION (`classify`, over a real catalog) is opt-in, like every PostgreSQL
  suite: it creates `house_helper_restore_<token>` databases owned by the literal
  `hh_owner` role on the SAME disposable cluster of compose.test-postgres.yaml, through
  the same independent identity and safety boundary (tests/postgres_harness.py). Each
  test builds exactly one state of the matrix, classifies it, and drops it again.

Codex R1 remediation coverage (this module is where each finding is reproduced):

* F1 - unsafe catalog resolution and side effects: a shadow `public.pg_class` cannot
  conceal an application object, a shadow function cannot run during a classification,
  the observation statements resolve to `pg_catalog` on a hostile `search_path`, and a
  static audit proves every observation statement is qualified and predicate-free.
* F2 (and its R2 replacement) - the pre-initialization inventory is a baseline
  difference, not a blacklist: every catalog object with an OID at or above
  `FirstNormalObjectId` - and every object of a class `initdb` leaves empty, at any OID -
  is outside the authorized inventory, in *every* independently creatable class -
  relations, routines, types, schemas, collations, conversions, operators, operator
  classes and families, access methods, casts, text-search
  configurations/dictionaries/parsers/templates, extensions, publications, event
  triggers, foreign data wrappers and servers, large objects, languages, default ACLs and
  transforms - whether created by `hh_owner`, by a maintenance login or by the bootstrap
  superuser.
* F3 - stale transaction and activity observations: a `repeatable read`/`serializable`
  target snapshot and a second maintenance session inside one caller transaction are both
  reproduced, the supported states still classify, and the caller's transaction is shown
  to be untouched.
* F4 - owner privilege convergence: the privilege model is pinned against the server, the
  disabled database whose owner `CONNECT` comes only from `PUBLIC` is shown to be
  non-convergent (the prescribed hardening really does take the owner's `CONNECT` away),
  and an ACL that contradicts the server's readings fails closed.
* F5 / R2-02 - unsafe test resource cleanup: the module's cleanup registry carries each
  database's OID, owner and marker token; a renamed database, a replacement under a
  registered name, a changed owner, a missing marker and a database that still has a
  session attached are all refused and preserved. Cleanup is a sequential, identity-
  checked `DROP DATABASE` - never `WITH (FORCE)`, never a rename, a lock queue, a
  background worker or a cancellation. The cluster is disposable and used exclusively by
  this suite; that assumption, and what it does NOT protect against, is stated at the
  protocol below.
* R2-03 - the maintenance/control isolation guard: repeatable-read and serializable
  control transactions, a database created after the control snapshot was taken, and the
  stale `absent` verdict that would authorize CREATE DATABASE are all covered, together
  with an in-memory mutation showing the guard is load-bearing.
* WP-1C-B fixes - the two authorization gaps closed in this round: an object of a class
  `initdb` leaves empty is rejected whatever its OID (a large object really can be
  created with OID 1000, below FirstNormalObjectId), and a logical-replication
  subscription whose `subdbid` is the target database is fail-closed, read from
  credential-free columns only.

No Production, n8n or ETL service is contacted. The seven literal project roles are
provisioned by the accepted WP-1B fixture (`_provision_project`), refused if already
present and removed afterwards.

Opt-in, like the other PostgreSQL suites:

    docker compose -f compose.test-postgres.yaml up -d
    python -m tests.postgres_preflight
    HOUSE_HELPER_TEST_POSTGRES_URL=postgresql+psycopg://hh_test:hh_test_only_not_a_secret@127.0.0.1:55432/postgres pytest tests/test_postgres_e4.py -v -ra
"""

import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, replace

import psycopg
import psycopg.pq
import pytest

from tests import postgres_e4 as e4
from tests import postgres_sql as artifacts
from tests.postgres_harness import (
    POSTGRES_URL_ENV,
    RoleIdentity,
    run_from_environment,
)
from tests import test_postgres_sql_artifacts as artifact_tests

RESTORE_NAME = "house_helper_restore_abc123"  # the allowed restore shape, never created
LIVE = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_URL_ENV),
        reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL "
        "(see tests/test_admin_postgres.py)",
    ),
]


# =============================================================================================
# The pure decision rule
# =============================================================================================


def _acl(
    *,
    owner=True,
    public=(),
    grantee=None,
    grantee_privileges=(("CONNECT", False),),
    extra=(),
):
    """An EXPLICIT `datacl`: the owner's three default entries (unless `owner` is False),
    the grantee's entries, the `PUBLIC` entries and any extra named grantee."""
    entries = []
    if owner:
        entries += [
            ("hh_owner", privilege, False) for privilege in ("CONNECT", "CREATE", "TEMPORARY")
        ]
    if grantee is not None:
        entries += [(grantee, privilege, grantable) for privilege, grantable in grantee_privileges]
    entries += [("PUBLIC", privilege, grantable) for privilege, grantable in public]
    entries += [(name, "CONNECT", False) for name in extra]
    return tuple(entries)


def _facts(
    grantee="hh_restore",
    *,
    allowconn=False,
    acl=None,
    sessions=(),
    probe_pid=None,
    inventory=None,
    subscriptions=(),
    observation=(),
    **overrides,
):
    """Facts whose effective `CONNECT`/`TEMPORARY` readings are DERIVED from the ACL by
    the PostgreSQL 16 model, so a fixture can never describe a combination the server
    cannot produce. `acl=None` is a NULL `datacl` (the catalog default applies)."""
    materialized = acl is not None
    entries = tuple(acl) if materialized else ()
    base = dict(
        exists=True,
        owner="hh_owner",
        encoding="UTF8",
        collate="C",
        ctype="C",
        provider="c",
        allowconn=allowconn,
        acl_materialized=materialized,
        acl_entries=entries,
        connect={
            role: e4.database_privilege(materialized, entries, role, "CONNECT")
            for role in artifacts.PROJECT_ROLE_NAMES
        },
        temporary={
            role: e4.database_privilege(materialized, entries, role, "TEMPORARY")
            for role in artifacts.PROJECT_ROLE_NAMES
        },
        sessions=sessions,
        probe_pid=probe_pid,
        target_inventory=inventory,
        subscriptions=subscriptions,
        observation=observation,
    )
    base.update(overrides)
    return e4.DatabaseFacts(**base)


def _hardened(grantee="hh_restore", *, allowconn=True, acl=None, **overrides):
    """The E3-b/E3-c ACL: the owner's default entries and a plain `CONNECT` for the one
    maintenance login. `acl` replaces that shape when a test needs a different ACL; every
    other keyword is a `DatabaseFacts` override."""
    return _facts(
        grantee,
        allowconn=allowconn,
        acl=_acl(grantee=grantee) if acl is None else acl,
        **overrides,
    )


def _classify(facts, name=RESTORE_NAME, grantee="hh_restore"):
    return e4.classify_facts(facts, database=name, grantee=grantee)


def _inv(*objects):
    """An observed inventory holding exactly these `(class, schema, name)` objects."""
    return e4.TargetInventory(objects=tuple(objects))


class _InventoryRows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeTarget:
    """A target connection that answers the inventory statement with canned rows, so the
    baseline rule itself can be tested without a server. Rows are the statement's own
    shape: `(class, namespace-oid, name, oid, kind, typrelid, typelem)`."""

    def __init__(self, rows):
        self._rows = tuple(rows)
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        assert statement == e4._INVENTORY, statement
        return _InventoryRows(self._rows)


def _inventory_of(*rows):
    return e4._inventory(_FakeTarget(rows))


def test_the_frozen_state_action_and_failure_sets_are_exactly_these():
    assert e4.E4_STATES == (
        "absent",
        "disabled_unhardened",
        "disabled_hardened",
        "enabled_maintenance_only",
        "fail_closed",
    )
    assert e4.E4_ACTIONS == (
        "create_database",
        "harden_database_acl",
        "enable_connections",
        "verify_target_objects",
        "initialize_schema",
        "remediate",
    )
    assert e4.FAILURE_ORDER == (
        "ownership_conflict",
        "encoding_mismatch",
        "locale_mismatch",
        "unexpected_public_access",
        "unexpected_database_grants",
        "database_acl_not_hardened",
        "unrecognized_database_acl",
        "unauthorized_sessions",
        "unexpected_tables",
        "unexpected_subscription",
        "unrecognized_connection_state",
        "untrusted_observation_environment",
        "stale_observation",
        "inconsistent_privilege_observation",
    )


# --- BOOT-07 row 1: absent -------------------------------------------------------------------


def test_an_absent_database_is_the_create_row():
    verdict = _classify(e4.DatabaseFacts(exists=False))
    assert verdict.state == e4.E4_ABSENT
    assert verdict.action == e4.ACTION_CREATE_DATABASE
    assert verdict.failures == ()
    assert verdict.tables_checked is False
    assert verdict.may_initialize_schema is False


# --- BOOT-07 row 2: disabled, not hardened ---------------------------------------------------


def test_a_disabled_database_with_the_default_public_acl_is_the_harden_row():
    verdict = _classify(_facts())
    assert verdict.state == e4.E4_DISABLED_UNHARDENED
    assert verdict.action == e4.ACTION_HARDEN_DATABASE_ACL
    assert verdict.failures == ()
    assert verdict.evidence["allowconn"] is False
    assert verdict.evidence["acl_materialized"] is False
    assert verdict.evidence["public_entries"] == (("CONNECT", False), ("TEMPORARY", False))


def test_a_materialized_default_public_pair_is_still_the_harden_row():
    verdict = _classify(
        _facts(acl=_acl(public=(("CONNECT", False), ("TEMPORARY", False))))
    )
    assert verdict.state == e4.E4_DISABLED_UNHARDENED
    assert verdict.action == e4.ACTION_HARDEN_DATABASE_ACL
    assert verdict.failures == ()


# --- BOOT-07 row 3: disabled and hardened ----------------------------------------------------


def test_a_disabled_hardened_database_is_the_enable_row():
    verdict = _classify(_hardened(allowconn=False))
    assert verdict.state == e4.E4_DISABLED_HARDENED
    assert verdict.action == e4.ACTION_ENABLE_CONNECTIONS
    assert verdict.failures == ()
    assert verdict.may_initialize_schema is False


def test_a_disabled_database_that_hardening_cannot_converge_is_fail_closed():
    # An explicit grant to another role is not removed by REVOKE PUBLIC + GRANT the
    # grantee, so it is not the "not fully hardened" row 2 state.
    verdict = _classify(_facts(acl=_acl(extra=("hh_app",))))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.action == e4.ACTION_REMEDIATE
    assert verdict.failures == ("unexpected_database_grants", "unrecognized_database_acl")


def test_a_disabled_grantee_temporary_grant_is_not_repairable():
    verdict = _classify(
        _facts(
            acl=_acl(
                grantee="hh_restore",
                grantee_privileges=(("CONNECT", False), ("TEMPORARY", False)),
            )
        )
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unrecognized_database_acl",)


def test_a_public_privilege_outside_the_built_in_default_pair_is_unexpected():
    verdict = _classify(_facts(acl=_acl(public=(("CREATE", False),))))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unexpected_public_access", "unrecognized_database_acl")


def test_a_public_grant_option_is_unexpected_even_for_the_default_pair():
    verdict = _classify(_facts(acl=_acl(public=(("CONNECT", True),))))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unexpected_public_access", "unrecognized_database_acl")


def test_a_grantee_grant_option_is_not_removed_by_the_hardening():
    verdict = _classify(
        _facts(acl=_acl(grantee="hh_restore", grantee_privileges=(("CONNECT", True),)))
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unrecognized_database_acl",)


def test_a_disabled_database_whose_owner_connect_comes_only_from_public_is_not_repairable():
    """Codex R1 F4, pure half. The owner has no ACL entry of its own, so its `CONNECT`
    comes from `PUBLIC`; `REVOKE ALL ... FROM PUBLIC` takes it away and the E3-b state the
    classifier would promise is never reached. PostgreSQL 16 really does answer
    `has_database_privilege(<owner>, ..., 'CONNECT')` with false in the projected state."""
    facts = _facts(acl=_acl(owner=False, public=(("CONNECT", False), ("TEMPORARY", False))))
    assert facts.connect["hh_owner"] is True  # through PUBLIC, not through an owner entry
    verdict = _classify(facts)
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.action == e4.ACTION_REMEDIATE
    assert verdict.failures == ("unrecognized_database_acl",)


def test_a_disabled_database_whose_owner_grant_is_missing_after_the_revoke_is_the_same_case():
    # A materialized ACL that named only the grantee: the owner entry is absent, PUBLIC
    # holds nothing, so the owner already has no CONNECT. Not a state any E3 action fixes.
    verdict = _classify(_facts(acl=_acl(owner=False, grantee="hh_restore")))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unrecognized_database_acl",)


# --- BOOT-07 row 4/5: enabled -----------------------------------------------------------------


def test_an_enabled_unhardened_database_is_fail_closed_never_fixed_forward():
    verdict = _classify(_facts(allowconn=True))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.action == e4.ACTION_REMEDIATE
    assert verdict.failures == ("unexpected_public_access", "database_acl_not_hardened")
    assert verdict.may_initialize_schema is False


def test_an_enabled_database_with_an_extra_grantee_is_fail_closed():
    verdict = _classify(
        _hardened(acl=_acl(grantee="hh_restore", extra=("hh_app",)))
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unexpected_database_grants", "database_acl_not_hardened")


def test_an_enabled_database_with_public_access_is_fail_closed():
    verdict = _classify(
        _hardened(acl=_acl(grantee="hh_restore", public=(("CONNECT", False),)))
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unexpected_public_access", "database_acl_not_hardened")


def test_an_enabled_database_missing_the_grantee_connect_is_fail_closed():
    verdict = _classify(_facts(allowconn=True, acl=_acl(grantee=None)))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("database_acl_not_hardened",)


def test_an_enabled_hardened_database_needs_the_target_check():
    verdict = _classify(_hardened())
    assert verdict.state == e4.E4_ENABLED_MAINTENANCE_ONLY
    assert verdict.action == e4.ACTION_VERIFY_TARGET_OBJECTS
    assert verdict.tables_checked is False
    assert verdict.may_initialize_schema is False


def test_a_clean_target_is_the_only_proceed_verdict():
    verdict = _classify(_hardened(inventory=_inv()))
    assert verdict.state == e4.E4_ENABLED_MAINTENANCE_ONLY
    assert verdict.action == e4.ACTION_INITIALIZE_SCHEMA
    assert verdict.tables_checked is True
    assert verdict.failures == ()
    assert verdict.may_initialize_schema is True
    assert verdict.evidence["target_inventory"] == ()


def test_an_application_object_is_fail_closed():
    inventory = _inv(("relation", "public", "alembic_version"))
    verdict = _classify(_hardened(inventory=inventory))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unexpected_tables",)
    assert verdict.may_initialize_schema is False
    assert verdict.evidence["target_inventory"] == (("relation", "public", "alembic_version"),)


def test_an_unrecognized_connection_state_fails_closed():
    verdict = _classify(_facts(allowconn=None))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unrecognized_connection_state",)


# --- ownership, encoding, locale -------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"owner": "hh_restore"}, "ownership_conflict"),
        ({"encoding": "LATIN1"}, "encoding_mismatch"),
        ({"collate": "en_US.utf8", "ctype": "en_US.utf8"}, "locale_mismatch"),
        ({"provider": "i"}, "locale_mismatch"),
    ],
)
def test_a_database_that_is_not_the_frozen_one_fails_closed(overrides, code):
    verdict = _classify(_facts(**overrides))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == (code,)


# --- sessions ---------------------------------------------------------------------------------


def test_a_session_that_is_not_the_probe_connection_fails_closed():
    verdict = _classify(_hardened(sessions=((101, "hh_restore"),)))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unauthorized_sessions",)


def test_a_second_grantee_session_is_unauthorized_too():
    verdict = _classify(
        _hardened(sessions=((101, "hh_restore"), (102, "hh_restore")), probe_pid=101)
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unauthorized_sessions",)


def test_only_the_probe_pid_is_authorized():
    verdict = _classify(
        _hardened(
            sessions=((101, "hh_restore"),), probe_pid=101, inventory=_inv()
        )
    )
    assert verdict.state == e4.E4_ENABLED_MAINTENANCE_ONLY
    assert verdict.action == e4.ACTION_INITIALIZE_SCHEMA
    # The same session is unauthorized when it is NOT the supplied probe connection.
    assert _classify(_hardened(sessions=((101, "hh_restore"),))).failures == (
        "unauthorized_sessions",
    )


def test_a_session_on_a_disabled_database_is_still_fail_closed():
    # A backend that connected before the fence was raised keeps its session; the fence
    # is not evidence that the session is gone.
    verdict = _classify(_hardened(allowconn=False, sessions=((101, "hh_test"),)))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unauthorized_sessions",)


def test_a_probe_session_the_inventory_does_not_show_fails_closed():
    """FRESH-3, pure half: the probe is known to be alive (it is the connection the caller
    supplied), so an inventory that does not list it was not read live."""
    verdict = _classify(
        _hardened(inventory=_inv(), sessions=(), probe_pid=101)
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("stale_observation",)
    assert verdict.may_initialize_schema is False


# --- ordering and validation -------------------------------------------------------------------


def test_every_observed_failure_is_reported_in_the_stable_order():
    verdict = _classify(
        _facts(
            owner="hh_restore",
            collate="en_US.utf8",
            ctype="en_US.utf8",
            allowconn=True,
            sessions=((101, "hh_test"),),
        )
    )
    assert verdict.failures == (
        "ownership_conflict",
        "locale_mismatch",
        "unexpected_public_access",
        "database_acl_not_hardened",
        "unauthorized_sessions",
    )


@pytest.mark.parametrize(
    ("name", "grantee"),
    [
        ("house_helper", "hh_restore"),
        (RESTORE_NAME, "hh_migrate"),
        ("house_helper_admin", "hh_migrate"),
        ("house_helper_restore_UPPER", "hh_restore"),
        (RESTORE_NAME, "hh_app"),
    ],
)
def test_the_database_grantee_binding_is_refused_before_classification(name, grantee):
    with pytest.raises(artifacts.ArtifactError) as refused:
        e4.classify_facts(_facts(), database=name, grantee=grantee)
    assert refused.value.code in ("grantee_database", "database_name", "grantee")


def test_the_serving_binding_is_the_mirror_of_the_restore_binding():
    facts = _hardened("hh_migrate")
    verdict = e4.classify_facts(facts, database="house_helper", grantee="hh_migrate")
    assert verdict.state == e4.E4_ENABLED_MAINTENANCE_ONLY
    assert verdict.grantee == "hh_migrate"
    assert verdict.database == "house_helper"


# =============================================================================================
# F1/F3 - an observation that cannot be trusted never authorizes
# =============================================================================================


def test_an_untrusted_environment_fails_closed_whatever_else_was_observed():
    """A session whose `search_path` puts a user schema before `pg_catalog` can define the
    operators and functions the SQL uses (measured on PostgreSQL 16). It is not a source of
    facts, so it is not a source of an authorizing verdict either."""
    verdict = _classify(
        _hardened(
            inventory=_inv(),  # even a clean-looking rest of the facts
            observation=(
                (e4.FAIL_UNTRUSTED_ENVIRONMENT, "target search_path is public, pg_catalog"),
            ),
        )
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("untrusted_observation_environment",)
    assert verdict.may_initialize_schema is False
    assert verdict.evidence["observation"] == (
        ("untrusted_observation_environment", "target search_path is public, pg_catalog"),
    )


@pytest.mark.parametrize("isolation", ["repeatable read", "serializable"])
def test_a_snapshot_isolation_observation_fails_closed(isolation):
    verdict = _classify(
        _hardened(
            inventory=_inv(),
            observation=(
                (e4.FAIL_STALE_OBSERVATION, f"target transaction isolation is {isolation}"),
            ),
        )
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("stale_observation",)
    assert verdict.may_initialize_schema is False


def test_the_accepted_isolation_levels_are_the_statement_fresh_ones():
    assert e4.FRESH_ISOLATION == ("read committed", "read uncommitted")


def test_an_observation_problem_is_reported_before_any_other_failure():
    verdict = _classify(
        _facts(
            owner="hh_restore",
            allowconn=True,
            observation=((e4.FAIL_STALE_OBSERVATION, "maintenance transaction isolation is serializable"),),
        )
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("stale_observation",)


def test_an_absence_is_not_established_from_an_untrusted_observation():
    """`absent` authorizes a CREATE, so it needs the same trusted observation as any other
    authorizing verdict: an unreadable session cannot authorize creation either."""
    verdict = _classify(
        e4.DatabaseFacts(
            exists=False,
            observation=(
                (e4.FAIL_UNTRUSTED_ENVIRONMENT, "maintenance search_path is public, pg_catalog"),
            ),
        )
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.action == e4.ACTION_REMEDIATE
    assert verdict.failures == ("untrusted_observation_environment",)


# =============================================================================================
# F2 - the pre-initialization inventory (ENR-10: nothing outside it may exist)
# =============================================================================================


# Every independently creatable class the R2 inventory contract enumerates. A verdict
# must fail closed for an object in ANY of them: the decision rule is class-agnostic, and
# a class missing from the observation is exactly the bypass Codex R2 demonstrated.
OUTSIDE_INVENTORY = (
    ("schema", "extra", "extra"),
    ("relation", "public", "alembic_version"),
    ("relation", "public", "wp1cb_view"),
    ("relation", "public", "wp1cb_seq"),
    ("relation", "pg_temp_3", "alembic_version"),
    ("routine", "public", "wp1cb_probe"),
    ("type", "public", "wp1cb_enum"),
    ("collation", "public", "wp1cb_collation"),
    ("conversion", "public", "wp1cb_conversion"),
    ("operator", "public", "==="),
    ("operator class", "public", "wp1cb_opclass"),
    ("operator family", "public", "wp1cb_opfamily"),
    ("access method", "", "wp1cb_am"),
    ("cast", "", "oid:16384"),
    ("text search configuration", "public", "wp1cb_ts_config"),
    ("text search dictionary", "public", "wp1cb_ts_dict"),
    ("text search parser", "public", "wp1cb_ts_parser"),
    ("text search template", "public", "wp1cb_ts_template"),
    ("extension", "", "wp1cb_extension"),
    ("publication", "", "wp1cb_publication"),
    ("event trigger", "", "wp1cb_trigger"),
    ("foreign data wrapper", "", "wp1cb_fdw"),
    ("foreign server", "", "wp1cb_server"),
    ("large object", "", "oid:16385"),
    ("language", "", "wp1cb_language"),
    ("default acl", "", "oid:16386"),
    ("transform", "", "oid:16387"),
)


@pytest.mark.parametrize("entry", OUTSIDE_INVENTORY, ids=lambda entry: entry[0])
def test_any_object_outside_the_pre_initialization_inventory_fails_closed(entry):
    """An empty RELATION inventory is not a safe database. An empty extra schema, a
    routine, a standalone type, a temporary object, and every class R2 demonstrated -
    collations, conversions, operators, operator classes and families, access methods,
    casts, text-search objects, extensions, publications, event triggers, foreign data
    wrappers and servers, large objects, languages, default ACLs and transforms - are all
    outside ENR-10's authorized inventory and must fail closed (Codex R1 F2, R2-01)."""
    verdict = _classify(_hardened(inventory=_inv(entry)))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("unexpected_tables",)
    assert verdict.evidence["target_inventory"] == (entry,)
    assert verdict.may_initialize_schema is False


def test_many_outside_objects_are_all_reported_in_order():
    """Nothing is collapsed to the first finding: the evidence lists every unexpected
    object, sorted, so an operator can see the whole bypass."""
    verdict = _classify(_hardened(inventory=_inv(*reversed(OUTSIDE_INVENTORY))))
    assert verdict.failures == ("unexpected_tables",)
    assert verdict.evidence["target_inventory"] == tuple(sorted(OUTSIDE_INVENTORY))


def test_an_empty_inventory_is_distinct_from_an_unread_one():
    read = _classify(_hardened(inventory=_inv()))
    unread = _classify(_hardened(inventory=None))
    assert read.tables_checked is True and read.action == e4.ACTION_INITIALIZE_SCHEMA
    assert unread.tables_checked is False and unread.action == e4.ACTION_VERIFY_TARGET_OBJECTS


def test_the_inventory_entries_are_sorted_and_class_tagged():
    inventory = e4.TargetInventory(
        objects=(
            ("schema", "zeta", "zeta"),
            ("relation", "public", "b"),
            ("relation", "public", "a"),
            ("routine", "public", "f"),
            ("type", "public", "t"),
        )
    )
    assert inventory.entries == (
        ("relation", "public", "a"),
        ("relation", "public", "b"),
        ("routine", "public", "f"),
        ("schema", "zeta", "zeta"),
        ("type", "public", "t"),
    )


def test_the_baseline_boundary_is_first_normal_object_id():
    """The inventory's baseline is `pg_class.h`'s FirstNormalObjectId, measured on the
    pinned PostgreSQL 16.15 image (a pristine `template0` database has zero catalog rows
    at or above it). It is a contract constant, not an incidental value."""
    assert e4.FIRST_NORMAL_OBJECT_ID == 16384
    assert e4._SESSION_SCHEMA_PREFIXES == ("pg_temp_", "pg_toast_temp_")


def test_the_empty_baseline_classes_are_exactly_the_ones_initdb_leaves_empty():
    """The OID threshold is a difference from a baseline. A class with no intrinsic
    instance has nothing to differ from, so it must reject every row instead."""
    assert e4._EMPTY_BASELINE_CLASSES == frozenset(
        {
            "large object",
            "publication",
            "event trigger",
            "foreign data wrapper",
            "foreign server",
            "default acl",
            "transform",
        }
    )


def test_a_low_oid_object_of_an_empty_baseline_class_is_still_reported():
    """WP-1C-B A1: `lo_create(1000)` really does create a large object with OID 1000 on
    PostgreSQL 16.15 (see the live test below), which is below FirstNormalObjectId. An
    OID threshold alone would call it part of the baseline and authorize initialization
    over it. Every empty-baseline class is reported whatever its OID."""
    inventory = _inventory_of(
        ("large object", None, None, 1000, None, None, None),
        ("publication", None, "wp1cb_publication", 1000, None, None, None),
        ("event trigger", None, "wp1cb_trigger", e4.FIRST_NORMAL_OBJECT_ID - 1, None, None, None),
        ("foreign data wrapper", None, "wp1cb_fdw", 1, None, None, None),
        ("foreign server", None, "wp1cb_server", 1000, None, None, None),
        ("default acl", None, None, 1000, None, None, None),
        ("transform", None, None, 1000, None, None, None),
    )
    assert inventory.objects == (
        ("default acl", "", "oid:1000"),
        ("event trigger", "", "wp1cb_trigger"),
        ("foreign data wrapper", "", "wp1cb_fdw"),
        ("foreign server", "", "wp1cb_server"),
        ("large object", "", "oid:1000"),
        ("publication", "", "wp1cb_publication"),
        ("transform", "", "oid:1000"),
    )


def test_the_oid_baseline_is_kept_for_the_classes_initdb_populates():
    """The empty-baseline rule is narrow: a class with an intrinsic baseline is still
    decided by the OID difference, so PostgreSQL's own catalogs are not work."""
    inventory = _inventory_of(
        ("schema", None, "pg_catalog", 11, None, None, None),
        ("relation", 2200, "pg_class", 1259, "r", None, None),
        ("type", 11, "int4", 23, "b", None, 0),  # a baseline type
        ("extension", None, "plpgsql", 13585, None, None, None),
    )
    assert inventory.objects == ()
    # One object above the boundary, resolved through the `public` schema row.
    above = _inventory_of(
        ("schema", None, "public", 2200, None, None, None),
        ("relation", 2200, "alembic_version", e4.FIRST_NORMAL_OBJECT_ID, "r", None, None),
    )
    assert above.objects == (("relation", "public", "alembic_version"),)


def test_a_clean_inventory_of_a_pristine_database_is_empty():
    """The other half: nothing in an empty-baseline class is reported for a database that
    has no row in it, so a pristine database still reaches the proceed verdict."""
    verdict = _classify(_hardened(inventory=_inventory_of()))
    assert verdict.action == e4.ACTION_INITIALIZE_SCHEMA
    assert verdict.may_initialize_schema is True


def test_an_unexpected_subscription_of_the_target_database_fails_closed():
    """WP-1C-B A2: a database that is the target of a logical-replication subscription
    has been adopted by something other than BOOT-04, like an application table."""
    verdict = _classify(
        _hardened(inventory=_inv(), subscriptions=((16390, "wp1cb_subscription"),))
    )
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.action == e4.ACTION_REMEDIATE
    assert verdict.failures == ("unexpected_subscription",)
    assert verdict.may_initialize_schema is False
    assert verdict.evidence["subscriptions"] == ((16390, "wp1cb_subscription"),)


def test_a_subscription_is_reported_after_an_unexpected_object_and_before_the_rest():
    """The stable order, with the new code in it: objects first, then the subscription."""
    verdict = _classify(
        _facts(
            allowconn=None,
            inventory=_inv(("relation", "public", "alembic_version")),
            subscriptions=((16390, "wp1cb_subscription"),),
        )
    )
    assert verdict.failures == (
        "unexpected_tables",
        "unexpected_subscription",
        "unrecognized_connection_state",
    )


def test_the_subscription_reading_is_credential_free():
    """The observation may name only the OID, the name and the target database OID of a
    subscription; `subconninfo` holds a connection string and is never selected."""
    assert "subconninfo" not in e4._SUBSCRIPTIONS
    assert e4._SUBSCRIPTIONS == (
        "SELECT s.oid, s.subname, s.subdbid FROM pg_catalog.pg_subscription s"
    )
    assert e4._SUBSCRIPTIONS in e4.OBSERVATION_STATEMENTS


# =============================================================================================
# F4 - the privilege model, and an ACL that contradicts it
# =============================================================================================


def test_the_null_acl_default_grants_connect_and_temporary_to_every_role():
    for role in artifacts.PROJECT_ROLE_NAMES:
        assert e4.database_privilege(False, (), role, "CONNECT") is True
        assert e4.database_privilege(False, (), role, "TEMPORARY") is True
    # CREATE is the owner's, and only the owner's.
    assert e4.database_privilege(False, (), "hh_owner", "CREATE") is True
    assert e4.database_privilege(False, (), "hh_app", "CREATE") is False


def test_a_database_owner_has_no_privilege_without_an_acl_entry():
    # Measured on PostgreSQL 16: an empty materialized ACL grants the owner nothing.
    entries = ()
    assert e4.database_privilege(True, entries, "hh_owner", "CONNECT") is False
    granted = (("hh_owner", "CONNECT", False),)
    assert e4.database_privilege(True, granted, "hh_owner", "CONNECT") is True


def test_a_public_entry_grants_every_role_including_the_owner():
    entries = (("PUBLIC", "CONNECT", False),)
    for role in artifacts.PROJECT_ROLE_NAMES:
        assert e4.database_privilege(True, entries, role, "CONNECT") is True


def test_the_privilege_readings_must_agree_with_the_acl():
    """A reading the ACL cannot explain - an unexpected membership, a resolver the
    observer does not model - is never allowed to authorize anything."""
    facts = _hardened(inventory=_inv())
    tampered = replace(facts, connect={**facts.connect, "hh_app": True})
    verdict = _classify(tampered)
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("inconsistent_privilege_observation",)
    assert verdict.may_initialize_schema is False


def test_a_missing_privilege_reading_fails_closed():
    facts = _hardened()
    incomplete = dict(facts.connect)
    del incomplete["hh_purge"]
    verdict = _classify(replace(facts, connect=incomplete))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("inconsistent_privilege_observation",)


def test_an_observation_with_no_privilege_reading_at_all_fails_closed():
    """No reading is not the same as a clean reading: only the early fail-closed path may
    carry facts that were never taken."""
    facts = _hardened(inventory=_inv())
    verdict = _classify(replace(facts, connect={}, temporary={}))
    assert verdict.state == e4.E4_FAIL_CLOSED
    assert verdict.failures == ("inconsistent_privilege_observation",)


def test_the_hardening_projection_is_the_e3_b_state():
    """What the approved `REVOKE PUBLIC` + `GRANT CONNECT` really produces."""
    from_null = e4._hardening_projection(_facts(), "hh_restore")
    assert set(from_null) == {
        ("hh_owner", "CONNECT", False),
        ("hh_owner", "CREATE", False),
        ("hh_owner", "TEMPORARY", False),
        ("hh_restore", "CONNECT", False),
    }
    # A NULL datacl is materialized by the REVOKE: the owner's default entries come with it.
    assert ("hh_owner", "CONNECT", False) in from_null


# =============================================================================================
# F1 - the observation SQL cannot be answered by an application object
# =============================================================================================

# Every catalog relation/function the observer is allowed to name. A statement may only
# contain one of these names with an explicit `pg_catalog.` qualifier in front of it.
_AUDITED_NAMES = (
    "pg_database",
    "pg_roles",
    "pg_stat_activity",
    "pg_namespace",
    "pg_class",
    "pg_proc",
    "pg_type",
    "pg_collation",
    "pg_conversion",
    "pg_operator",
    "pg_opclass",
    "pg_opfamily",
    "pg_am",
    "pg_cast",
    "pg_ts_config",
    "pg_ts_dict",
    "pg_ts_parser",
    "pg_ts_template",
    "pg_extension",
    "pg_publication",
    "pg_event_trigger",
    "pg_foreign_data_wrapper",
    "pg_foreign_server",
    "pg_largeobject_metadata",
    "pg_language",
    "pg_default_acl",
    "pg_transform",
    "pg_subscription",
    "aclexplode",
    "has_database_privilege",
    "pg_encoding_to_char",
    "pg_get_userbyid",
    "shobj_description",
    "current_setting",
    "current_schemas",
    "current_database",
    "pg_backend_pid",
    "pg_stat_clear_snapshot",
)
_FORBIDDEN = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "ALTER", "DROP", "GRANT", "REVOKE",
    "TRUNCATE", "COMMENT", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "SET", "RESET",
)


def test_every_observation_statement_is_qualified_and_predicate_free():
    """The static half of F1: no observation statement can be resolved through a session's
    `search_path`, and none of them writes, casts or filters by name."""
    assert e4.OBSERVATION_STATEMENTS
    for statement in e4.OBSERVATION_STATEMENTS:
        assert statement.startswith("SELECT "), statement
        if statement != e4._SESSION_IDENTITY:
            # The session identity read names no catalog object at all (keywords only).
            assert "pg_catalog." in statement, statement
        assert " WHERE " not in statement, statement
        assert "::" not in statement, statement
        # No operator resolves in an observation statement either: an operator can be
        # redefined in a user schema (PostgreSQL 16 answered a shadowed `=` with true).
        assert re.search(r"[=<>]", statement) is None, statement
        assert " LIKE " not in statement.upper(), statement
        assert " IS DISTINCT " not in statement.upper(), statement
        for name in _AUDITED_NAMES:
            unqualified = re.search(rf"(?<![\w.]){name}\b", statement)
            assert unqualified is None, (name, statement)
        for verb in _FORBIDDEN:
            assert re.search(rf"\b{verb}\b", statement) is None, (verb, statement)


def test_the_trusted_schema_path_is_the_one_that_starts_with_pg_catalog():
    assert e4._trusted_environment(["pg_catalog", "public"]) is True
    assert e4._trusted_environment(["pg_temp_3", "pg_catalog", "public"]) is True
    assert e4._trusted_environment(["public", "pg_catalog"]) is False
    assert e4._trusted_environment(["pg_temp_3", "public", "pg_catalog"]) is False
    assert e4._trusted_environment([]) is False


# =============================================================================================
# F5 - the cleanup registry: identity before destruction
# =============================================================================================
#
# Why this is not `harness.Run.create_database`/`drop_database` (Codex R1 F5 asked for reuse
# "where practical"): the E4 tests must build arbitrary E3 states - owned by the literal
# `hh_owner`, `ALLOW_CONNECTIONS false`, a NULL or partially materialized `datacl`, an ACL
# that does not converge - whereas `create_database()` always applies the accepted WP-1B
# fence (its own per-run owner, PUBLIC revoked, migrate granted, then enabled) and accepts
# only `house_helper_test_<token>_<n>` names (`_SCRATCH_NAME`), while E4's frozen naming is
# `house_helper_restore_<token>`. The harness's own evidence is reused instead of its
# lifecycle: the privileges of the privileged connection (`project.run.control()`), and the
# same identity facts it verifies before a drop - the COMMENT marker and `datdba` - plus
# `pg_database.oid`, which the harness does not record and which is what makes a rename or
# a replacement detectable.

_MARKER_PREFIX = "house-helper-e4-test:"


@dataclass(frozen=True)
class _OwnedDatabase:
    """What this module knows about a database it created: the OID the server assigned,
    the COMMENT marker it wrote, the owner it asked for and whether the marker was written
    back before the record was confirmed."""

    oid: int
    marker: str
    owner: str
    marked: bool


# Only a resource registered here is ever dropped, and only while every one of its
# identity facts still matches. `_ATTEMPTED` holds CREATEs whose outcome is unknown: those
# are preserved and reported, never adopted and never dropped.
_CREATED: dict = {}
_ATTEMPTED: dict = {}

_PROVENANCE = (
    "SELECT d.oid, pg_catalog.shobj_description(d.oid, 'pg_database'), "
    "pg_catalog.pg_get_userbyid(d.datdba) FROM pg_catalog.pg_database d "
    "WHERE d.datname = %s"
)
_NAMED_BY_OID = "SELECT d.datname FROM pg_catalog.pg_database d WHERE d.oid = %s"
_OID_BY_NAME = "SELECT d.oid FROM pg_catalog.pg_database d WHERE d.datname = %s"
_EXISTS = "SELECT 1 FROM pg_catalog.pg_database d WHERE d.datname = %s"


def _name():
    """A database name this module owns by construction: the frozen restore shape plus 64
    bits of randomness, so two runs - or two tests - cannot collide on a name."""
    return "house_helper_restore_" + secrets.token_hex(8)


def _create(project, name, *, owner="hh_owner", collate="C", ctype="C", allowconn=False):
    """Create one database and register its FULL identity before it is used.

    The attempt is recorded before the statement (F5): a CREATE whose acknowledgement is
    lost is never mistaken for "nothing was created". Only a returned CREATE is registered;
    the record then carries the OID, owner and marker read back from the server, and the
    marker is what a later drop re-checks."""
    database = artifacts.validate_database_name(name)
    if database in _CREATED or database in _ATTEMPTED:
        raise AssertionError(f"{database!r} is already registered by this test module")
    marker = _MARKER_PREFIX + secrets.token_hex(16)
    _ATTEMPTED[database] = marker
    try:
        with project.run.control() as control:
            if control.execute(_EXISTS, (database,)).fetchone() is not None:
                _ATTEMPTED.pop(database, None)
                raise AssertionError(f"{database!r} already exists; it is never adopted")
            control.execute(
                f'CREATE DATABASE "{database}" OWNER "{owner}" TEMPLATE template0 '
                f"ENCODING 'UTF8' LC_COLLATE '{collate}' LC_CTYPE '{ctype}' "
                f"ALLOW_CONNECTIONS {'true' if allowconn else 'false'}"
            )
            row = control.execute(_PROVENANCE, (database,)).fetchone()
            if row is None:  # cannot happen for a confirmed CREATE
                raise AssertionError(f"{database!r} was not found after its CREATE")
            oid, _comment, datdba = int(row[0]), row[1], str(row[2])
            _CREATED[database] = _OwnedDatabase(
                oid=oid, marker=marker, owner=datdba, marked=False
            )
            _ATTEMPTED.pop(database, None)
            control.execute(f"COMMENT ON DATABASE \"{database}\" IS '{marker}'")
            _CREATED[database] = replace(_CREATED[database], marked=True)
    except BaseException as error:
        # A duplicate name is the one failure that proves nothing of ours exists; every
        # other failure keeps the attempt for `_resolve_attempts` to report.
        if getattr(error, "sqlstate", None) == "42P04":
            _ATTEMPTED.pop(database, None)
        raise


# --- the identity-checked drop --------------------------------------------------------------
#
# OPERATIONAL ASSUMPTION: the cluster is the disposable one compose.test-postgres.yaml
# describes, and this suite uses it EXCLUSIVELY. No other actor creates, renames, owns or
# drops databases on it while these tests run. That is what makes a simple cleanup
# proportionate:
#
#   1. read the row that currently bears the registered name;
#   2. its OID, owner and COMMENT marker must be the ones the registration recorded - an
#      adopted resource, a renamed one, a replacement under the name and a changed owner
#      are all refused and preserved;
#   3. no client backend may be attached to it;
#   4. `DROP DATABASE "<name>"` - a plain drop, never `WITH (FORCE)`.
#
# All four steps run in sequence on `run.control()`, whose autocommit session is the only
# one that needs to exist.
#
# LIMITATION, stated rather than papered over: check-then-drop is NOT atomic against a
# non-cooperating concurrent actor. PostgreSQL 16 has no "drop the database with this OID"
# statement, and `DROP DATABASE <name>` resolves the name itself, so a session that renamed
# the registered database and created a replacement under the same name between steps 2
# and 4 would have that replacement dropped. An earlier revision closed exactly that window
# with an uncommitted rename, a lock-queue inspection and a cancelled DROP worker; that
# protocol was disproportionate for a disposable, single-suite cluster whose whole purpose
# is to be thrown away, and it is gone. Every refusal keeps the registry record and reports
# the leftover; nothing is ever forced.

_PROVENANCE_BY_OID = (
    "SELECT d.oid, d.datname, pg_catalog.shobj_description(d.oid, 'pg_database'), "
    "pg_catalog.pg_get_userbyid(d.datdba) FROM pg_catalog.pg_database d "
    "WHERE d.oid = %s"
)
_DATABASE_SESSIONS = (
    "SELECT a.pid, a.usename FROM pg_catalog.pg_stat_activity a "
    "WHERE a.datname = %s AND a.backend_type = 'client backend'"
)
# The one destructive statement this module issues. Interpolated with a name that
# `validate_database_name` accepted; there is no FORCE and no other option.
_DROP_DATABASE = 'DROP DATABASE "{}"'


def _identity_refusal(database, owned, oid, comment, owner):
    """Why the observed row is not the registered resource, or `None` if it is.

    `pg_database.oid` is the identity: it survives a rename and is never reused while the
    database exists. The owner (`datdba`) and the COMMENT marker are checked with it - a
    database created here whose marker write failed is accepted only while it is still
    unmarked (`marked=False`)."""
    if oid != owned.oid:
        return (
            f"{database!r} is oid {oid}, not the registered {owned.oid}; it is a different "
            "database and is not dropped"
        )
    if owner != owned.owner:
        return (
            f"{database!r} is owned by {owner!r}, not the registered {owned.owner!r}; it is "
            "not dropped"
        )
    if comment != owned.marker and not (comment is None and not owned.marked):
        return f"{database!r} does not carry this module's marker; it is not dropped"
    return None


def _drop_identity(project, database, owned):
    """ONE sequential check-then-drop: verify the registration's identity and that no
    session is attached, then `DROP DATABASE` by name. Returns False when nothing bears
    the name any more (the resource is already gone). Every refusal raises and leaves the
    database in place; the registry record is the caller's to keep or forget.

    The exclusive-cluster assumption and the TOCTOU this deliberately does not close are
    stated at the protocol above."""
    with project.run.control() as control:
        row = control.execute(_PROVENANCE, (database,)).fetchone()
        if row is None:
            return False
        refusal = _identity_refusal(database, owned, int(row[0]), row[1], str(row[2]))
        if refusal is not None:
            raise AssertionError(refusal)
        sessions = control.execute(_DATABASE_SESSIONS, (database,)).fetchall()
        if sessions:
            described = ", ".join(
                f"pid {int(pid)} ({'' if usename is None else str(usename)})"
                for pid, usename in sessions
            )
            raise AssertionError(
                f"{database!r} still has a session attached ({described}); it is not dropped "
                "and is reported as a leftover"
            )
        try:
            control.execute(_DROP_DATABASE.format(database))
        except psycopg.Error as error:
            raise AssertionError(
                f"the drop of {database!r} failed ({error}); the database is preserved and "
                "reported as a leftover"
            ) from error
    return True


def _drop(project, name):
    """Drop `name` only while it is still the exact resource this module registered.

    A database that does not carry the registered name any more was renamed by someone
    else; it is reported, not dropped under a name it was never registered with. A record
    whose database is simply gone is forgotten - nothing of ours is left."""
    database = artifacts.validate_database_name(name)
    owned = _CREATED.get(database)
    if owned is None:
        raise AssertionError(
            f"{database!r} is not registered by this test module; refusing to drop it"
        )
    if not _drop_identity(project, database, owned):
        with project.run.control() as control:
            moved = control.execute(_NAMED_BY_OID, (owned.oid,)).fetchone()
        if moved is not None:
            raise AssertionError(
                f"{database!r} (oid {owned.oid}) is now named {str(moved[0])!r}; refusing "
                "to drop a database under a name it was not registered with"
            )
        _CREATED.pop(database, None)  # already gone: nothing of ours is left
        return
    _CREATED.pop(database, None)


def _resolve_attempts(project):
    """Settle CREATE attempts whose outcome is unknown. A name that does not exist was
    never created and is forgotten; a name that exists cannot be shown to be this module's
    (its CREATE never returned, so no identity was ever read), so it is PRESERVED and
    reported. Nothing here drops anything."""
    if not _ATTEMPTED:
        return []
    problems = []
    with project.run.control() as control:
        for database in sorted(_ATTEMPTED):
            if control.execute(_EXISTS, (database,)).fetchone() is None:
                _ATTEMPTED.pop(database, None)
                continue
            problems.append(
                AssertionError(
                    f"an attempted CREATE of {database!r} did not resolve; the database is "
                    "preserved and reported instead of being dropped on a guess"
                )
            )
    return problems


def _hardening(project, name, grantee="hh_restore"):
    """The E3-b phase only, still with connections disabled."""
    with project.run.control() as control:
        with control.transaction():
            control.execute(artifacts.render_bootstrap(name, grantee)["harden"])


def _enable(project, name):
    with project.run.control() as control:
        control.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')


@contextmanager
def _database(project, *, name=None, grantee="hh_restore", harden=True, enable=False, **create):
    name = name or _name()
    _create(project, name, **create)
    try:
        if harden:
            _hardening(project, name, grantee)
        if enable:
            _enable(project, name)
        yield name
    finally:
        _drop(project, name)


@contextmanager
def _probe(project, name, login="hh_restore"):
    with artifacts.project_connection(
        project.run,
        name,
        login,
        project.passwords[login],
        expected_current_user="hh_restore" if login == "hh_restore" else None,
    ) as connection:
        yield connection


@contextmanager
def _superuser(project, name):
    connection = psycopg.connect(autocommit=True, **project.run.target.connect_params(name))
    try:
        yield connection
    finally:
        connection.close()


def _manual(project, name, login, *, autocommit=True):
    role = RoleIdentity(
        key=login, name=login, password=project.passwords[login],
        session_user=login, current_user=login,
    )
    return psycopg.connect(autocommit=autocommit, **project.run.target.connect_params(name, role))


def _maintenance(project, *, autocommit=True):
    """The bootstrap connection to the maintenance database (the URL's own identity)."""
    return psycopg.connect(
        autocommit=autocommit,
        **project.run.target.connect_params(project.run.target.maintenance_database),
    )


def _classify_observed(project, name, *, target=None, grantee="hh_restore"):
    """`e4.classify` through a verified maintenance connection, with the fixture's
    database/grantee binding."""
    with project.run.control() as control:
        return e4.classify(control, database=name, grantee=grantee, target=target)


class _Recorder:
    """A connection proxy that records every statement it is asked to run."""

    def __init__(self, connection):
        self._connection = connection
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        return self._connection.execute(statement, params)


# --- the registry, tested without a server ------------------------------------------------


class _Reply:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [] if self._row is None else [self._row]


class _FakeControl:
    """Answers the provenance questions and records every statement it is given."""

    def __init__(self, *, by_name=None, by_oid=None):
        self.by_name = by_name
        self.by_oid = by_oid
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        if " WHERE d.oid = " in statement:
            return _Reply(self.by_oid)
        if " WHERE d.datname = " in statement:
            return _Reply(self.by_name)
        return _Reply()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False

    @property
    def dropped(self):
        return [s for s in self.statements if s.startswith("DROP DATABASE")]


class _FakeRun:
    def __init__(self, control):
        self._control = control

    def control(self):
        return self._control


class _FakeProject:
    def __init__(self, control):
        self.run = _FakeRun(control)


class TestCleanupProvenance:
    """F5/R2-02 without a server: the registry refuses a resource it cannot prove is its
    own, and the identity rule used by the locked check is itself pure."""

    @pytest.fixture(autouse=True)
    def _isolated_registry(self):
        _CREATED.clear()
        _ATTEMPTED.clear()
        yield
        _CREATED.clear()
        _ATTEMPTED.clear()

    def test_an_unregistered_name_is_refused_before_any_statement(self):
        control = _FakeControl()
        with pytest.raises(AssertionError):
            _drop(_FakeProject(control), RESTORE_NAME)
        assert control.statements == []

    def test_the_identity_rule_accepts_the_registered_row(self):
        owned = _OwnedDatabase(oid=42, marker="mark", owner="hh_owner", marked=True)
        assert _identity_refusal("db", owned, 42, "mark", "hh_owner") is None

    @pytest.mark.parametrize(
        ("oid", "comment", "owner", "message"),
        [
            (43, "mark", "hh_owner", "different database"),
            (42, "mark", "hh_restore", "owned by"),
            (42, "someone-else", "hh_owner", "marker"),
            (42, None, "hh_owner", "marker"),
        ],
    )
    def test_the_identity_rule_refuses_every_disarrangement(self, oid, comment, owner, message):
        owned = _OwnedDatabase(oid=42, marker="mark", owner="hh_owner", marked=True)
        assert message in _identity_refusal("db", owned, oid, comment, owner)

    def test_an_unmarked_database_this_module_confirmed_creating_is_its_own(self):
        owned = _OwnedDatabase(oid=42, marker="mark", owner="hh_owner", marked=False)
        assert _identity_refusal("db", owned, 42, None, "hh_owner") is None
        # Once someone writes a marker it is no longer the unmarked resource we created.
        assert "marker" in _identity_refusal("db", owned, 42, "someone-else", "hh_owner")

    def test_an_ambiguous_creation_is_preserved_never_dropped(self):
        _ATTEMPTED[RESTORE_NAME] = "mark"
        control = _FakeControl(by_name=(1,))
        problems = _resolve_attempts(_FakeProject(control))
        assert len(problems) == 1 and "preserved" in str(problems[0])
        assert RESTORE_NAME in _ATTEMPTED
        assert control.dropped == []

    def test_an_attempt_that_created_nothing_is_forgotten(self):
        _ATTEMPTED[RESTORE_NAME] = "mark"
        control = _FakeControl(by_name=None)
        assert _resolve_attempts(_FakeProject(control)) == []
        assert RESTORE_NAME not in _ATTEMPTED


# =============================================================================================
# The observation
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


class _LiveTest:
    """Everything a PostgreSQL-marked class shares: the opt-in gate and the leftover sweep.

    The sweep drops ONLY what this module registered, through the identity-verified
    `_drop`, and reports every refusal instead of guessing."""

    pytestmark = LIVE

    @pytest.fixture(autouse=True)
    def _drop_leftovers(self, project):
        yield
        problems = []
        for name in sorted(_CREATED):
            try:
                _drop(project, name)
            except BaseException as problem:
                problems.append(problem)
        problems.extend(_resolve_attempts(project))
        if problems:
            raise problems[0]


class TestObservedE4(_LiveTest):
    def test_a_classification_of_an_absent_database(self, project):
        absent = _name()
        with project.run.control() as control:
            assert control.execute(_EXISTS, (absent,)).fetchone() is None
            verdict = e4.classify(control, database=absent, grantee="hh_restore")
        assert verdict.state == e4.E4_ABSENT
        assert verdict.action == e4.ACTION_CREATE_DATABASE

    def test_the_e3_a_state_is_the_harden_row(self, project):
        with _database(project, harden=False) as name:
            with project.run.control() as control:
                verdict = e4.classify(control, database=name, grantee="hh_restore")
            assert verdict.state == e4.E4_DISABLED_UNHARDENED
            assert verdict.action == e4.ACTION_HARDEN_DATABASE_ACL
            assert verdict.evidence["allowconn"] is False
            assert verdict.evidence["acl_materialized"] is False
            assert verdict.evidence["public_entries"] == (("CONNECT", False), ("TEMPORARY", False))
            # E3-a is recognized, but it is not the E3-b state the accepted WP-1B gate
            # requires - the two observations agree.
            with project.run.control() as control:
                with pytest.raises(artifacts.ArtifactError) as refused:
                    artifacts.BootstrapScript().verify_disabled(control, name, "hh_restore")
            assert refused.value.code == "e3_b"

    def test_the_e3_b_state_is_the_enable_row(self, project):
        with _database(project) as name:
            with project.run.control() as control:
                verdict = e4.classify(control, database=name, grantee="hh_restore")
                # The accepted WP-1B E3-b gate accepts exactly the state the classifier
                # calls hardened.
                artifacts.BootstrapScript().verify_disabled(control, name, "hh_restore")
            assert verdict.state == e4.E4_DISABLED_HARDENED
            assert verdict.action == e4.ACTION_ENABLE_CONNECTIONS
            assert verdict.evidence["acl_materialized"] is True
            assert set(verdict.evidence["acl_entries"]) == {
                ("hh_owner", "CREATE", False),
                ("hh_owner", "CONNECT", False),
                ("hh_owner", "TEMPORARY", False),
                ("hh_restore", "CONNECT", False),
            }

    def test_the_e3_c_state_needs_the_target_check_before_it_may_proceed(self, project):
        with _database(project, enable=True) as name:
            with project.run.control() as control:
                first = e4.classify(control, database=name, grantee="hh_restore")
                # The accepted WP-1B E3-c gate accepts exactly the state the classifier
                # calls enabled + hardened + maintenance-only.
                artifacts.BootstrapScript().verify_enabled(control, name, "hh_restore")
            assert first.state == e4.E4_ENABLED_MAINTENANCE_ONLY
            assert first.action == e4.ACTION_VERIFY_TARGET_OBJECTS
            assert first.tables_checked is False
            assert first.may_initialize_schema is False
            with _probe(project, name) as connection:
                with project.run.control() as control:
                    second = e4.classify(
                        control, database=name, grantee="hh_restore", target=connection
                    )
            assert second.state == e4.E4_ENABLED_MAINTENANCE_ONLY
            assert second.action == e4.ACTION_INITIALIZE_SCHEMA
            assert second.tables_checked is True
            assert second.may_initialize_schema is True
            assert second.evidence["target_inventory"] == ()

    def test_the_probe_connection_must_be_this_database(self, project):
        with _database(project, enable=True) as name:
            with project.run.control() as maintenance:
                with pytest.raises(artifacts.ArtifactError) as refused:
                    e4.classify(
                        maintenance, database=name, grantee="hh_restore", target=maintenance
                    )
            assert refused.value.code == "target_database"

    def test_no_connection_is_used_while_the_database_is_disabled(self, project):
        with _database(project) as name:
            with project.run.control() as control:
                with pytest.raises(artifacts.ArtifactError) as refused:
                    e4.classify(control, database=name, grantee="hh_restore", target=control)
            assert refused.value.code == "target_not_permitted"
            # The premise: a connection to the disabled database is refused by the server.
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(
                    autocommit=True, **project.run.target.connect_params(name)
                ).close()

    def test_a_superuser_probe_is_an_unauthorized_session_not_a_source_of_facts(self, project):
        with _database(project, enable=True) as name:
            with _superuser(project, name) as connection:
                with project.run.control() as control:
                    verdict = e4.classify(
                        control, database=name, grantee="hh_restore", target=connection
                    )
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.failures == ("unauthorized_sessions",)
            assert verdict.tables_checked is False
            assert verdict.evidence["target_inventory"] is None

    def test_an_application_object_fails_closed(self, project):
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                artifacts.assume_owner(connection)
                connection.execute("CREATE TABLE public.wp1cb_probe (id integer)")
                connection.execute("RESET ROLE")
                with project.run.control() as control:
                    verdict = e4.classify(
                        control, database=name, grantee="hh_restore", target=connection
                    )
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.failures == ("unexpected_tables",)
            assert verdict.may_initialize_schema is False

    def test_an_enabled_database_with_an_extra_grantee_fails_closed(self, project):
        with _database(project, enable=True) as name:
            with project.run.control() as control:
                control.execute(f'GRANT CONNECT ON DATABASE "{name}" TO "hh_app"')
                verdict = e4.classify(control, database=name, grantee="hh_restore")
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.failures == ("unexpected_database_grants", "database_acl_not_hardened")

    def test_enabled_public_access_is_fail_closed_and_never_enabled_forward(self, project):
        with _database(project, enable=True) as name:
            with project.run.control() as control:
                control.execute(f'GRANT CONNECT ON DATABASE "{name}" TO PUBLIC')
                verdict = e4.classify(control, database=name, grantee="hh_restore")
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.failures == ("unexpected_public_access", "database_acl_not_hardened")

    def test_an_open_session_that_is_not_the_probe_fails_closed(self, project):
        with _database(project, enable=True) as name:
            with _superuser(project, name):
                with project.run.control() as control:
                    verdict = e4.classify(control, database=name, grantee="hh_restore")
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.failures == ("unauthorized_sessions",)

    def test_a_changed_owner_is_a_conflict_not_a_resume(self, project):
        with _database(project, enable=True) as name:
            with project.run.control() as control:
                control.execute(f'ALTER DATABASE "{name}" OWNER TO hh_restore')
                verdict = e4.classify(control, database=name, grantee="hh_restore")
                # The ownership transfer is restored before the fixture drops the database:
                # the replacement owner is part of the cleanup identity (F5), so a database
                # left under another owner would be refused rather than dropped.
                control.execute(f'ALTER DATABASE "{name}" OWNER TO hh_owner')
            assert verdict.state == e4.E4_FAIL_CLOSED
            # The owner transfer also rewrites the ACL so the new owner holds the owner
            # privileges; both consequences are reported, in the stable order.
            assert verdict.failures == (
                "ownership_conflict",
                "database_acl_not_hardened",
                "unrecognized_database_acl",
            )

    def test_a_non_frozen_locale_is_a_conflict(self, project):
        with _database(
            project, harden=False, owner=project.run.target.username,
            collate="en_US.utf8", ctype="en_US.utf8",
        ) as name:
            with project.run.control() as control:
                verdict = e4.classify(control, database=name, grantee="hh_restore")
            assert verdict.state == e4.E4_FAIL_CLOSED
            # The harness-created name is not owned by hh_owner either; both facts are
            # reported rather than the first one hiding the second.
            assert verdict.failures == ("ownership_conflict", "locale_mismatch")

    def test_a_lingering_session_on_a_disabled_database_is_fail_closed(self, project):
        with _database(project, enable=True) as name:
            with _superuser(project, name):
                with project.run.control() as control:
                    control.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS false')
                    verdict = e4.classify(control, database=name, grantee="hh_restore")
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.failures == ("unauthorized_sessions",)

    def test_the_serving_database_is_classified_with_hh_migrate(self, project):
        with _database(project, name="house_helper", grantee="hh_migrate", enable=True) as name:
            with project.run.control() as control:
                first = e4.classify(control, database=name, grantee="hh_migrate")
            with _probe(project, name, "hh_migrate") as connection:
                with project.run.control() as control:
                    second = e4.classify(
                        control, database=name, grantee="hh_migrate", target=connection
                    )
            assert first.action == e4.ACTION_VERIFY_TARGET_OBJECTS
            assert second.action == e4.ACTION_INITIALIZE_SCHEMA
            assert second.grantee == "hh_migrate"

    def test_classification_issues_only_the_audited_statements(self, project):
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                with project.run.control() as control:
                    maintenance = _Recorder(control)
                    target = _Recorder(connection)
                    verdict = e4.classify(
                        maintenance, database=name, grantee="hh_restore", target=target
                    )
            assert verdict.action == e4.ACTION_INITIALIZE_SCHEMA
            assert maintenance.statements and target.statements
            for statement in maintenance.statements + target.statements:
                # Not merely "a SELECT": exactly one of the audited, qualified statements.
                assert statement in e4.OBSERVATION_STATEMENTS, statement

    def test_classification_changes_nothing(self, project):
        with _database(project, enable=True) as name:
            # With no session open, the maintenance half alone is observable.
            with project.run.control() as control:
                maintenance_only = e4.classify(control, database=name, grantee="hh_restore")
            with _probe(project, name) as connection:
                before = _snapshot(project, name, connection)
                with project.run.control() as control:
                    first = e4.classify(
                        control, database=name, grantee="hh_restore", target=connection
                    )
                    second = e4.classify(
                        control, database=name, grantee="hh_restore", target=connection
                    )
                after = _snapshot(project, name, connection)
            assert maintenance_only.action == e4.ACTION_VERIFY_TARGET_OBJECTS
            assert (first.state, second.state) == (
                e4.E4_ENABLED_MAINTENANCE_ONLY,
                e4.E4_ENABLED_MAINTENANCE_ONLY,
            )
            assert first.action == e4.ACTION_INITIALIZE_SCHEMA
            assert before == after

    def test_a_callers_transaction_is_neither_committed_nor_rolled_back(self, project):
        with _database(project, enable=True) as name:
            target = _manual(project, name, "hh_restore", autocommit=False)
            maintenance = psycopg.connect(
                autocommit=False,
                **project.run.target.connect_params(
                    project.run.target.maintenance_database
                ),
            )
            try:
                target.execute("SELECT 1")
                maintenance.execute("SELECT 1")
                assert (
                    target.info.transaction_status
                    == psycopg.pq.TransactionStatus.INTRANS
                )
                assert (
                    maintenance.info.transaction_status
                    == psycopg.pq.TransactionStatus.INTRANS
                )
                before = maintenance.execute(
                    "SELECT pg_backend_pid(), current_setting('transaction_isolation')"
                ).fetchone()
                verdict = e4.classify(
                    maintenance, database=name, grantee="hh_restore", target=target
                )
                assert verdict.action == e4.ACTION_INITIALIZE_SCHEMA
                assert (
                    target.info.transaction_status
                    == psycopg.pq.TransactionStatus.INTRANS
                )
                assert (
                    maintenance.info.transaction_status
                    == psycopg.pq.TransactionStatus.INTRANS
                )
                # Same session, same isolation level: the observation neither replaced the
                # transaction nor changed a setting.
                after = maintenance.execute(
                    "SELECT pg_backend_pid(), current_setting('transaction_isolation')"
                ).fetchone()
                assert after == before
            finally:
                target.rollback()
                maintenance.rollback()
                target.close()
                maintenance.close()

    def test_classification_requires_the_bootstrap_maintenance_boundary(self, project):
        with _database(project, enable=True) as name:
            maintenance_name = project.run.target.maintenance_database
            with _manual(project, maintenance_name, "hh_restore") as as_login:
                with pytest.raises(artifacts.ArtifactError) as refused:
                    e4.classify(as_login, database=name, grantee="hh_restore")
                assert refused.value.code == "bootstrap_superuser"
            with _manual(project, name, "hh_restore") as target_login:
                with pytest.raises(artifacts.ArtifactError) as refused:
                    e4.classify(target_login, database=name, grantee="hh_restore")
                assert refused.value.code == "maintenance_database"


class TestTrustedObservation(_LiveTest):
    """F1: no application-controlled object can answer for a catalog object."""

    def _arm(self, connection, view_query, *, objects):
        """Arm the traps Codex R1 used on the target session: application objects and a
        `public.pg_class` view in front of `pg_catalog.pg_class` on a hostile `search_path`."""
        artifacts.assume_owner(connection)
        for statement in objects:
            connection.execute(statement)
        connection.execute(f"CREATE VIEW public.pg_class AS {view_query}")
        # The trap has to be usable by the maintenance login for the exploit to be armed.
        connection.execute("GRANT SELECT ON public.pg_class TO PUBLIC")
        connection.execute("RESET ROLE")
        connection.execute("SET search_path = public, pg_catalog")

    def _concealing_shadow(self, connection):
        """Exploit 1: a view that answers for `pg_class` and filters the real table (and
        itself) out, so the concealed database looks empty to an unqualified scan. The
        table is the ONLY application object, so nothing else can give the concealment
        away."""
        self._arm(
            connection,
            "SELECT c.relnamespace, c.relname, c.relkind FROM pg_catalog.pg_class c "
            "WHERE c.relname NOT IN ('wp1cb_real', 'pg_class')",
            objects=("CREATE TABLE public.wp1cb_real (id integer)",),
        )

    def _mutating_shadow(self, connection):
        """Exploit 2: a view whose very scan runs an application function (and whose
        columns are the ones an unqualified relation scan projects, so the scan succeeds)."""
        self._arm(
            connection,
            "SELECT c.relnamespace, c.relname, c.relkind FROM pg_catalog.pg_class c "
            "WHERE public.wp1cb_bump() IS NOT NULL",
            objects=(
                "CREATE SEQUENCE public.wp1cb_mutation",
                "CREATE FUNCTION public.wp1cb_bump() RETURNS integer LANGUAGE sql "
                "SECURITY DEFINER AS $$ SELECT nextval('public.wp1cb_mutation')::integer $$",
            ),
        )

    def _mutations(self, project, name):
        with _superuser(project, name) as reader:
            return reader.execute(
                "SELECT last_value, is_called FROM public.wp1cb_mutation"
            ).fetchone()

    def _public_table_visible_unqualified(self, connection):
        return connection.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'wp1cb_real'"
        ).fetchone()[0]

    def test_a_shadow_catalog_view_cannot_conceal_an_application_object(self, project):
        """Codex R1 F1, exploit 1: with `search_path = public, pg_catalog` an unqualified
        `pg_class` resolves to the application's view, which is the only way the committed
        table could be missed. The classifier refuses the environment instead of reading
        it, so the concealed object cannot be missed."""
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                self._concealing_shadow(connection)
                # The trap is armed: the unqualified read conceals the real table, while
                # the qualified read still sees it.
                assert self._public_table_visible_unqualified(connection) == 0
                assert connection.execute(
                    "SELECT count(*) FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relname = 'wp1cb_real'"
                ).fetchone()[0] == 1
                with project.run.control() as control:
                    verdict = e4.classify(
                        control, database=name, grantee="hh_restore", target=connection
                    )
                assert verdict.state == e4.E4_FAIL_CLOSED
                assert verdict.failures == ("untrusted_observation_environment",)
                assert verdict.may_initialize_schema is False
                assert verdict.tables_checked is False

    def test_a_shadow_function_cannot_mutate_during_a_classification(self, project):
        """Codex R1 F1, exploit 2: reading the shadow view ran an application function.
        The classification must not execute any application-controlled function."""
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                self._mutating_shadow(connection)
                # The trap is armed: scanning the shadow view runs the function.
                assert connection.execute("SELECT count(*) FROM pg_class").fetchone()[0] > 1
                connection.execute("SET ROLE hh_owner")
                connection.execute("ALTER SEQUENCE public.wp1cb_mutation RESTART")
                connection.execute("RESET ROLE")
                assert self._mutations(project, name) == (1, False)  # armed but not fired
                with project.run.control() as control:
                    verdict = e4.classify(
                        control, database=name, grantee="hh_restore", target=connection
                    )
                # The function did not run: the sequence is exactly where it was restarted.
                assert self._mutations(project, name) == (1, False)
                assert verdict.state == e4.E4_FAIL_CLOSED
                assert verdict.failures == ("untrusted_observation_environment",)

    def test_the_observation_statements_ignore_a_shadow_catalog(self, project):
        """The qualification layer on its own: even on a hostile `search_path` (where the
        environment gate would refuse), the observer's own statements read `pg_catalog`."""
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                self._concealing_shadow(connection)
                # The shadow answers the unqualified name ...
                assert self._public_table_visible_unqualified(connection) == 0
                # ... and does not participate in the audited statement.
                inventory = e4._inventory(connection)
                assert ("relation", "public", "wp1cb_real") in inventory.objects

    def test_an_untrusted_maintenance_session_is_refused_without_reading_anything(self, project):
        """F1: the same gate applies to the maintenance connection. It is not "fixed" with
        a `SET`; the session is refused and its settings are left as the caller had them."""
        with _database(project, enable=True) as name:
            with _manual(project, project.run.target.maintenance_database, "hh_restore") as as_login:
                # not a superuser: refused before the gate, so no facts are read at all
                with pytest.raises(artifacts.ArtifactError):
                    e4.classify(as_login, database=name, grantee="hh_restore")
            maintenance = _maintenance(project)
            try:
                maintenance.execute("SET search_path = public, pg_catalog")
                verdict = e4.classify(maintenance, database=name, grantee="hh_restore")
                assert verdict.state == e4.E4_FAIL_CLOSED
                assert verdict.failures == ("untrusted_observation_environment",)
                # FRESH-4: the caller's setting is exactly what it was.
                assert maintenance.execute("SHOW search_path").fetchone()[0] == "public, pg_catalog"
            finally:
                maintenance.close()

    def test_the_documented_search_path_of_the_project_roles_is_trusted(self, project):
        """The frozen matrix sets `search_path = public` for `hh_app` and nothing for the
        maintenance logins; both are trusted (`pg_catalog` is implicitly first)."""
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                assert connection.execute("SHOW search_path").fetchone()[0] == '"$user", public'
                with project.run.control() as control:
                    verdict = e4.classify(
                        control, database=name, grantee="hh_restore", target=connection
                    )
            assert verdict.action == e4.ACTION_INITIALIZE_SCHEMA


class TestPreInitializationInventory(_LiveTest):
    """F2/R2-01: every object outside ENR-10's authorized inventory fails closed, in every
    independently creatable class, whoever creates it.

    `creator` is the role that R2 established is authorized for that class:
    `owner` - `hh_owner`, assumed by the maintenance login (the normal authorized creator);
    `superuser` - the bootstrap superuser, on a session closed before the observation (the
    privileged creator); `probe` - the `hh_restore` maintenance login itself (R2 called out
    a large object created directly by it)."""

    CASES = (
        # Relations, routines, types and schemas (R1 F2's original coverage).
        (
            "owner",
            ("CREATE TABLE public.wp1cb_table (id integer)",),
            ("relation", "public", "wp1cb_table"),
        ),
        (
            "owner",
            ("CREATE VIEW public.wp1cb_view AS SELECT 1 AS id",),
            ("relation", "public", "wp1cb_view"),
        ),
        (
            "owner",
            ("CREATE MATERIALIZED VIEW public.wp1cb_matview AS SELECT 1 AS id",),
            ("relation", "public", "wp1cb_matview"),
        ),
        (
            "owner",
            ("CREATE SEQUENCE public.wp1cb_sequence",),
            ("relation", "public", "wp1cb_sequence"),
        ),
        (
            "owner",
            ("CREATE TABLE public.wp1cb_partitioned (id integer) PARTITION BY RANGE (id)",),
            ("relation", "public", "wp1cb_partitioned"),
        ),
        (
            "owner",
            (
                "CREATE FUNCTION public.wp1cb_function() RETURNS integer LANGUAGE sql "
                "AS $$ SELECT 1 $$",
            ),
            ("routine", "public", "wp1cb_function"),
        ),
        (
            "owner",
            ("CREATE PROCEDURE public.wp1cb_procedure() LANGUAGE sql AS $$ SELECT 1 $$",),
            ("routine", "public", "wp1cb_procedure"),
        ),
        (
            "owner",
            ("CREATE TYPE public.wp1cb_enum AS ENUM ('a', 'b')",),
            ("type", "public", "wp1cb_enum"),
        ),
        (
            "owner",
            ("CREATE DOMAIN public.wp1cb_domain AS integer",),
            ("type", "public", "wp1cb_domain"),
        ),
        (
            "owner",
            ("CREATE TYPE public.wp1cb_range AS RANGE (subtype = integer)",),
            ("type", "public", "wp1cb_range"),
        ),
        (
            "owner",
            ("CREATE TYPE public.wp1cb_composite AS (a integer, b text)",),
            ("relation", "public", "wp1cb_composite"),
        ),
        (
            "owner",
            ("CREATE SCHEMA wp1cb_schema",),
            ("schema", "wp1cb_schema", "wp1cb_schema"),
        ),
        (
            # The name `public` is not an exemption: the built-in schema is below the
            # baseline boundary, a re-created one is not, and schema work is schema work.
            "owner",
            ("DROP SCHEMA public CASCADE", "CREATE SCHEMA public"),
            ("schema", "public", "public"),
        ),
        # The twelve classes Codex R2 reproduced, plus the classes beyond them.
        (
            "owner",
            ("CREATE COLLATION public.wp1cb_collation (provider = icu, locale = 'en-US')",),
            ("collation", "public", "wp1cb_collation"),
        ),
        (
            "owner",
            ("CREATE TEXT SEARCH CONFIGURATION public.wp1cb_ts_config "
             "(COPY = pg_catalog.english)",),
            ("text search configuration", "public", "wp1cb_ts_config"),
        ),
        (
            "owner",
            ("CREATE TEXT SEARCH DICTIONARY public.wp1cb_ts_dict "
             "(TEMPLATE = pg_catalog.simple)",),
            ("text search dictionary", "public", "wp1cb_ts_dict"),
        ),
        (
            "owner",
            ("ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public "
             "GRANT SELECT ON TABLES TO hh_app",),
            ("default acl", "", None),
        ),
        (
            "superuser",
            ("CREATE CONVERSION public.wp1cb_conversion FOR 'UTF8' TO 'LATIN1' "
             "FROM pg_catalog.utf8_to_iso8859_1",),
            ("conversion", "public", "wp1cb_conversion"),
        ),
        (
            "probe",
            ("SELECT pg_catalog.lo_create(0)",),
            ("large object", "", None),
        ),
        (
            "superuser",
            ("CREATE OPERATOR public.=== (FUNCTION = pg_catalog.int4eq, "
             "LEFTARG = int4, RIGHTARG = int4)",),
            ("operator", "public", "==="),
        ),
        (
            "superuser",
            ("CREATE PUBLICATION wp1cb_publication",),
            ("publication", "", "wp1cb_publication"),
        ),
        (
            "superuser",
            ("CREATE TEXT SEARCH PARSER public.wp1cb_ts_parser "
             "(START = pg_catalog.prsd_start, GETTOKEN = pg_catalog.prsd_nexttoken, "
             "END = pg_catalog.prsd_end, LEXTYPES = pg_catalog.prsd_lextype)",),
            ("text search parser", "public", "wp1cb_ts_parser"),
        ),
        (
            "superuser",
            ("CREATE TEXT SEARCH TEMPLATE public.wp1cb_ts_template "
             "(INIT = pg_catalog.dsimple_init, LEXIZE = pg_catalog.dsimple_lexize)",),
            ("text search template", "public", "wp1cb_ts_template"),
        ),
        (
            "superuser",
            ("CREATE OPERATOR FAMILY public.wp1cb_opfamily USING btree",),
            ("operator family", "public", "wp1cb_opfamily"),
        ),
        (
            "superuser",
            ("CREATE OPERATOR CLASS public.wp1cb_opclass FOR TYPE int4 USING btree "
             "AS STORAGE int4, OPERATOR 1 pg_catalog.<, "
             "FUNCTION 1 pg_catalog.btint4cmp(int4, int4)",),
            ("operator class", "public", "wp1cb_opclass"),
        ),
        (
            "superuser",
            ("CREATE CAST (int4 AS date) WITH INOUT AS ASSIGNMENT",),
            ("cast", "", None),
        ),
        (
            "superuser",
            ("CREATE FOREIGN DATA WRAPPER wp1cb_fdw",),
            ("foreign data wrapper", "", "wp1cb_fdw"),
        ),
        (
            "superuser",
            (
                "CREATE FOREIGN DATA WRAPPER wp1cb_server_fdw",
                "CREATE SERVER wp1cb_server FOREIGN DATA WRAPPER wp1cb_server_fdw",
            ),
            ("foreign server", "", "wp1cb_server"),
        ),
        (
            "superuser",
            ("CREATE EXTENSION pg_trgm",),
            ("extension", "", "pg_trgm"),
        ),
        (
            "superuser",
            (
                "CREATE FUNCTION public.wp1cb_trigger_function() RETURNS event_trigger "
                "LANGUAGE plpgsql AS $$ BEGIN END $$",
                "CREATE EVENT TRIGGER wp1cb_trigger ON ddl_command_end "
                "EXECUTE FUNCTION public.wp1cb_trigger_function()",
            ),
            ("event trigger", "", "wp1cb_trigger"),
        ),
    )

    @pytest.mark.parametrize(
        ("creator", "statements", "expected"),
        CASES,
        ids=[f"{case[0]}-{case[2][0].replace(' ', '-')}" for case in CASES],
    )
    def test_an_object_outside_the_inventory_fails_closed(
        self, project, creator, statements, expected
    ):
        with _database(project, enable=True) as name:
            if creator == "superuser":
                # A privileged creator session: closed BEFORE the observation, so it is
                # not an "unauthorized session" and only the object it created is judged.
                with _superuser(project, name) as privileged:
                    for statement in statements:
                        privileged.execute(statement)
                with _probe(project, name) as connection:
                    verdict = _classify_observed(project, name, target=connection)
            else:
                with _probe(project, name) as connection:
                    if creator == "owner":
                        artifacts.assume_owner(connection)
                    try:
                        for statement in statements:
                            connection.execute(statement)
                    finally:
                        if creator == "owner":
                            connection.execute("RESET ROLE")
                    verdict = _classify_observed(project, name, target=connection)
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.failures == ("unexpected_tables",)
            assert verdict.may_initialize_schema is False
            kind, schema, object_name = expected
            entries = verdict.evidence["target_inventory"]
            assert any(
                entry[0] == kind
                and (schema is None or entry[1] == schema)
                and (object_name is None or entry[2] == object_name)
                for entry in entries
            ), (expected, entries)

    def test_the_intrinsic_baseline_is_not_reported(self, project):
        """The other half of R2-01: the design must not reject PostgreSQL's own baseline.
        A pristine `template0` database holds thousands of catalog objects; every one is
        below FirstNormalObjectId, none is reported, and the database is the proceed
        verdict."""
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                rows = connection.execute(e4._INVENTORY).fetchall()
                below = [row for row in rows if int(row[3]) < e4.FIRST_NORMAL_OBJECT_ID]
                above = [row for row in rows if int(row[3]) >= e4.FIRST_NORMAL_OBJECT_ID]
                # The premise: the baseline is large - a filter that accidentally dropped
                # everything would also "report nothing".
                assert len(below) > 5000
                assert above == []
                assert e4._inventory(connection).objects == ()
                verdict = _classify_observed(project, name, target=connection)
            assert verdict.state == e4.E4_ENABLED_MAINTENANCE_ONLY
            assert verdict.action == e4.ACTION_INITIALIZE_SCHEMA
            assert verdict.tables_checked is True

    def test_an_unexpected_empty_schema_fails_closed_on_its_own(self, project):
        """Codex R1 F2: an empty extra schema has no relation at all, so a relation-only
        inventory declared the database safe."""
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                artifacts.assume_owner(connection)
                connection.execute("CREATE SCHEMA wp1cb_empty")
                assert connection.execute(
                    "SELECT count(*) FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'wp1cb_empty'"
                ).fetchone()[0] == 0
                connection.execute("RESET ROLE")
                with project.run.control() as control:
                    verdict = e4.classify(
                        control, database=name, grantee="hh_restore", target=connection
                    )
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.evidence["target_inventory"] == (
                ("schema", "wp1cb_empty", "wp1cb_empty"),
            )

    def test_a_temporary_alembic_version_fails_closed(self, project):
        """Codex R1 F2: a temporary `alembic_version` lives in a `pg_temp_<n>` schema that
        a relation scan excluding `pg_%` schemas never saw."""
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                # A temporary object is the session's; hh_owner holds TEMPORARY on the
                # database, hh_restore does not (E3-b), so the object is created as the
                # owner and stays in this session's temp namespace.
                connection.execute("SET ROLE hh_owner")
                connection.execute("CREATE TEMP TABLE alembic_version (version_num text)")
                connection.execute("RESET ROLE")
                assert connection.execute(
                    "SELECT count(*) FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = 'alembic_version' AND n.nspname LIKE 'pg\\_temp\\_%'"
                ).fetchone()[0] == 1
                with project.run.control() as control:
                    verdict = e4.classify(
                        control, database=name, grantee="hh_restore", target=connection
                    )
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.failures == ("unexpected_tables",)
            # A temporary namespace is PostgreSQL's own, so it is not itself an
            # "unexpected schema"; the object in it is what fails closed.
            assert any(
                kind == "relation" and name_ == "alembic_version"
                for kind, schema, name_ in verdict.evidence["target_inventory"]
                if schema.startswith("pg_temp_")
            )

    def test_an_explicitly_assigned_low_oid_large_object_fails_closed(self, project):
        """WP-1C-B A1, live: PostgreSQL 16.15 really accepts `lo_create(<oid below
        FirstNormalObjectId>)`. The object is created by the maintenance login itself
        (the R2 creator case), the only object in the database, and it must be reported
        even though its OID sits below the baseline boundary every other class uses."""
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                oid = int(
                    connection.execute("SELECT pg_catalog.lo_create(1000)").fetchone()[0]
                )
                assert oid == 1000 and oid < e4.FIRST_NORMAL_OBJECT_ID
                assert connection.execute(
                    "SELECT count(*) FROM pg_catalog.pg_largeobject_metadata"
                ).fetchone()[0] == 1
                verdict = _classify_observed(project, name, target=connection)
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.failures == ("unexpected_tables",)
            assert verdict.may_initialize_schema is False
            assert verdict.evidence["target_inventory"] == (("large object", "", "oid:1000"),)

    def test_an_unexpected_subscription_fails_closed_and_never_exposes_credentials(
        self, project
    ):
        """WP-1C-B A2, live: a subscription whose `subdbid` is the database under
        classification is fail-closed. It is created without connecting to any publisher
        (`connect = false`, no slot), so no external service is contacted; the connection
        string it carries is never read by the classifier."""
        subscription = "wp1cb_subscription"
        with _database(project, enable=True) as name:
            with _superuser(project, name) as privileged:
                privileged.execute(
                    f'CREATE SUBSCRIPTION "{subscription}" '
                    "CONNECTION 'host=127.0.0.1 port=55432 dbname=nowhere' "
                    "PUBLICATION nowhere "
                    "WITH (connect = false, enabled = false, create_slot = false, "
                    "slot_name = NONE)"
                )
            try:
                with project.run.control() as control:
                    verdict = e4.classify(control, database=name, grantee="hh_restore")
                assert verdict.state == e4.E4_FAIL_CLOSED
                assert verdict.action == e4.ACTION_REMEDIATE
                assert verdict.failures == ("unexpected_subscription",)
                assert verdict.may_initialize_schema is False
                # The evidence is the non-secret identity of the subscription only.
                assert [entry[1] for entry in verdict.evidence["subscriptions"]] == [
                    subscription
                ]
                assert "nowhere" not in repr(verdict)
            finally:
                # A subscription keeps the database from being dropped, so it is removed
                # before the fixture's own cleanup - through the catalog's own command.
                with _superuser(project, name) as privileged:
                    privileged.execute(f'DROP SUBSCRIPTION "{subscription}"')


class TestFreshObservation(_LiveTest):
    """F3: stale snapshots and stale session inventories never authorize."""

    @pytest.mark.parametrize("isolation", ["repeatable read", "serializable"])
    def test_a_snapshot_isolation_target_cannot_authorize_initialization(self, project, isolation):
        with _database(project, enable=True) as name:
            target = _manual(project, name, "hh_restore", autocommit=False)
            try:
                target.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation.upper()}")
                target.execute("SELECT 1")  # the snapshot is now fixed
                assert target.execute(
                    "SHOW transaction_isolation"
                ).fetchone()[0] == isolation
                # A table committed by ANOTHER session, through a connection to the
                # database under classification (autocommit, so it is committed at once).
                with _superuser(project, name) as writer:
                    writer.execute("CREATE TABLE public.alembic_version (version_num text)")
                # The premise: the committed table is invisible through this snapshot ...
                stale_reader = target.execute(
                    "SELECT count(*) FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = 'alembic_version'"
                ).fetchone()[0]
                assert stale_reader == 0
                # ... while a fresh connection sees it.
                with _superuser(project, name) as reader:
                    assert reader.execute(
                        "SELECT count(*) FROM pg_catalog.pg_class c "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.relname = 'alembic_version'"
                    ).fetchone()[0] == 1
                with project.run.control() as control:
                    verdict = e4.classify(
                        control, database=name, grantee="hh_restore", target=target
                    )
                assert verdict.state == e4.E4_FAIL_CLOSED
                assert e4.FAIL_STALE_OBSERVATION in verdict.failures
                assert verdict.may_initialize_schema is False
                assert verdict.tables_checked is False
                # FRESH-4: the caller's transaction is still open and still at its level.
                assert target.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
                assert target.execute(
                    "SHOW transaction_isolation"
                ).fetchone()[0] == isolation
            finally:
                target.rollback()
                target.close()

    def test_a_read_committed_caller_transaction_still_classifies(self, project):
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                maintenance = _maintenance(project, autocommit=False)
                try:
                    maintenance.execute("SELECT 1")
                    verdict = e4.classify(
                        maintenance, database=name, grantee="hh_restore", target=connection
                    )
                    assert verdict.action == e4.ACTION_INITIALIZE_SCHEMA
                    assert (
                        maintenance.info.transaction_status
                        == psycopg.pq.TransactionStatus.INTRANS
                    )
                finally:
                    maintenance.rollback()
                    maintenance.close()

    def test_a_second_maintenance_session_is_detected_within_one_transaction(self, project):
        """Codex R1 F3.B: the statistics snapshot is cached for the caller's transaction, so
        a second backend that appears inside it stays invisible - unless the snapshot is
        discarded before the inventory is read (FRESH-2)."""
        with _database(project, enable=True) as name:
            with _probe(project, name) as connection:
                maintenance = _maintenance(project, autocommit=False)
                second = None
                try:
                    maintenance.execute("SELECT 1")
                    first = e4.classify(
                        maintenance, database=name, grantee="hh_restore", target=connection
                    )
                    assert first.action == e4.ACTION_INITIALIZE_SCHEMA
                    sessions = (
                        "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                        "WHERE datname = %s AND backend_type = 'client backend'"
                    )
                    before = maintenance.execute(sessions, (name,)).fetchone()[0]
                    second = _manual(project, name, "hh_restore")  # a second session
                    # The premise: an ordinary re-read inside this transaction is stale.
                    assert maintenance.execute(sessions, (name,)).fetchone()[0] == before
                    # The classifier discards the snapshot and sees the second session.
                    again = e4.classify(
                        maintenance, database=name, grantee="hh_restore", target=connection
                    )
                    assert again.state == e4.E4_FAIL_CLOSED
                    assert again.failures == ("unauthorized_sessions",)
                    assert again.may_initialize_schema is False
                finally:
                    if second is not None:
                        second.close()
                    maintenance.rollback()
                    maintenance.close()


class TestControlIsolation(_LiveTest):
    """R2-03: the maintenance/control transaction's isolation guard, covered independently.

    `observe()` refuses a maintenance session that is not in a statement-fresh isolation
    level (FRESH-1). Codex R2 removed that guard through an in-memory mutation and the
    whole suite still passed; these tests are what fail when it is removed."""

    @pytest.mark.parametrize("isolation", ["repeatable read", "serializable"])
    def test_a_fixed_snapshot_control_transaction_fails_closed(self, project, isolation):
        with _database(project, enable=True) as name:
            maintenance = _maintenance(project, autocommit=False)
            try:
                maintenance.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation.upper()}")
                maintenance.execute("SELECT 1")  # the snapshot is fixed
                verdict = e4.classify(maintenance, database=name, grantee="hh_restore")
                assert verdict.state == e4.E4_FAIL_CLOSED
                assert verdict.action == e4.ACTION_REMEDIATE
                assert verdict.failures == (e4.FAIL_STALE_OBSERVATION,)
                assert verdict.may_initialize_schema is False
                # FRESH-4: the caller's transaction is still open, at its own level.
                assert (
                    maintenance.info.transaction_status
                    == psycopg.pq.TransactionStatus.INTRANS
                )
                assert maintenance.execute(
                    "SHOW transaction_isolation"
                ).fetchone()[0] == isolation
            finally:
                maintenance.rollback()
                maintenance.close()

    def test_the_stale_control_cannot_authorize_a_create(self, project):
        """The verdict this guard prevents, live: the database is absent in the control
        transaction's snapshot but exists in the cluster. Without the guard, `absent`
        would authorize CREATE DATABASE against a database that is really there."""
        name = _name()
        maintenance = _maintenance(project, autocommit=False)
        try:
            maintenance.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            maintenance.execute("SELECT 1")  # the snapshot is fixed
            assert maintenance.execute(_EXISTS, (name,)).fetchone() is None
            _create(project, name)  # committed by another (autocommit) connection
            # Premise 1: the stale transaction still sees nothing ...
            assert maintenance.execute(_EXISTS, (name,)).fetchone() is None
            # Premise 2: ... while a fresh connection sees the database.
            with project.run.control() as fresh:
                assert fresh.execute(_EXISTS, (name,)).fetchone() is not None
            verdict = e4.classify(maintenance, database=name, grantee="hh_restore")
            assert verdict.state == e4.E4_FAIL_CLOSED
            assert verdict.action == e4.ACTION_REMEDIATE
            assert verdict.failures == (e4.FAIL_STALE_OBSERVATION,)
            assert verdict.may_initialize_schema is False
            assert verdict.action != e4.ACTION_CREATE_DATABASE
        finally:
            maintenance.rollback()
            maintenance.close()
            _drop(project, name)

    def test_a_read_committed_control_transaction_sees_the_new_database(self, project):
        """The freshness the guard protects: the same interleaving under read committed is
        observed, so the database is classified as it really is rather than refused."""
        name = _name()
        maintenance = _maintenance(project, autocommit=False)
        try:
            maintenance.execute("SELECT 1")
            assert maintenance.execute(_EXISTS, (name,)).fetchone() is None
            _create(project, name)
            verdict = e4.classify(maintenance, database=name, grantee="hh_restore")
            assert verdict.state == e4.E4_DISABLED_UNHARDENED
            assert verdict.action == e4.ACTION_HARDEN_DATABASE_ACL
            assert verdict.failures == ()
        finally:
            maintenance.rollback()
            maintenance.close()
            _drop(project, name)

    def test_the_isolation_guard_is_load_bearing(self, project, monkeypatch):
        """Mutation evidence (R2-03): widening ONLY `FRESH_ISOLATION` disables the guard,
        and the same stale control transaction then returns `absent`/`create_database` -
        the authorizing verdict the two tests above reject. The guard is therefore not
        incidental: removing it makes those regressions fail."""
        name = _name()
        maintenance = _maintenance(project, autocommit=False)
        try:
            maintenance.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            maintenance.execute("SELECT 1")
            _create(project, name)
            monkeypatch.setattr(
                e4,
                "FRESH_ISOLATION",
                e4.FRESH_ISOLATION + ("repeatable read", "serializable"),
            )
            verdict = e4.classify(maintenance, database=name, grantee="hh_restore")
            assert verdict.state == e4.E4_ABSENT
            assert verdict.action == e4.ACTION_CREATE_DATABASE
        finally:
            maintenance.rollback()
            maintenance.close()
            _drop(project, name)


class TestPrivilegeModel(_LiveTest):
    """F4: the privilege model is pinned against PostgreSQL 16, not assumed."""

    STATES = (
        ("the catalog default (NULL datacl)", ()),
        ("PUBLIC revoked", ("REVOKE ALL ON DATABASE {db} FROM PUBLIC",)),
        (
            "PUBLIC revoked and an extra grantee",
            ("REVOKE ALL ON DATABASE {db} FROM PUBLIC", "GRANT CONNECT ON DATABASE {db} TO hh_app"),
        ),
        (
            "an explicit PUBLIC grant on a NULL ACL",
            ("GRANT CONNECT ON DATABASE {db} TO PUBLIC",),
        ),
        (
            "the owner entry removed, PUBLIC defaults only (Codex F4)",
            (
                "REVOKE ALL ON DATABASE {db} FROM hh_owner",
                "GRANT CONNECT, TEMPORARY ON DATABASE {db} TO PUBLIC",
            ),
        ),
        (
            "the E3-b state",
            (
                "REVOKE ALL ON DATABASE {db} FROM PUBLIC",
                "GRANT CONNECT ON DATABASE {db} TO hh_restore",
            ),
        ),
    )

    @pytest.mark.parametrize(("label", "statements"), STATES)
    def test_the_privilege_model_matches_the_server(self, project, label, statements):
        with _database(project, harden=False) as name:
            with project.run.control() as control:
                for statement in statements:
                    control.execute(statement.format(db=f'"{name}"'))
                facts = e4.observe(control, database=name, grantee="hh_restore")
            assert facts.exists is True
            assert facts.observation == ()
            for role in artifacts.PROJECT_ROLE_NAMES:
                for privilege, observed in (
                    ("CONNECT", facts.connect),
                    ("TEMPORARY", facts.temporary),
                ):
                    assert observed[role] is e4.database_privilege(
                        facts.acl_materialized, facts.acl_entries, role, privilege
                    ), (label, role, privilege)

    def test_the_owner_derived_public_state_is_refused_and_would_not_converge(self, project):
        """Codex R1 F4, live: the disabled database whose owner `CONNECT` comes only from
        `PUBLIC` is not authorized for hardening, and the prescribed hardening really does
        take the owner's `CONNECT` away (the E3-b gate refuses it)."""
        with _database(project, harden=False) as name:
            with project.run.control() as control:
                control.execute(f'REVOKE ALL ON DATABASE "{name}" FROM hh_owner')
                control.execute(f'GRANT CONNECT, TEMPORARY ON DATABASE "{name}" TO PUBLIC')
                assert control.execute(
                    "SELECT pg_catalog.has_database_privilege(%s, %s, 'CONNECT')",
                    ("hh_owner", name),
                ).fetchone()[0] is True  # through PUBLIC only
                assert control.execute(
                    "SELECT pg_catalog.has_database_privilege(%s, %s, 'CONNECT')",
                    ("hh_restore", name),
                ).fetchone()[0] is True
                verdict = e4.classify(control, database=name, grantee="hh_restore")
                assert verdict.state == e4.E4_FAIL_CLOSED
                assert verdict.action == e4.ACTION_REMEDIATE
                assert verdict.failures == ("unrecognized_database_acl",)
                # The operation the classifier declined to promise does not converge.
                with pytest.raises(artifacts.ArtifactError) as refused:
                    with control.transaction():
                        control.execute(
                            artifacts.render_bootstrap(name, "hh_restore")["harden"]
                        )
                        artifacts.BootstrapScript().verify_disabled(control, name, "hh_restore")
                assert refused.value.code == "e3_b"
                # The rolled-back transaction leaves the observed state exactly as it was.
                assert control.execute(
                    "SELECT pg_catalog.has_database_privilege(%s, %s, 'CONNECT')",
                    ("hh_owner", name),
                ).fetchone()[0] is True

    def test_a_convergent_disabled_state_is_still_the_harden_row(self, project):
        """The other side of the same rule: the E3-a default ACL IS convergent, because the
        `REVOKE` materializes the owner's default entries with it."""
        with _database(project, harden=False) as name:
            with project.run.control() as control:
                verdict = e4.classify(control, database=name, grantee="hh_restore")
                assert verdict.state == e4.E4_DISABLED_UNHARDENED
                _hardening(project, name)
                artifacts.BootstrapScript().verify_disabled(control, name, "hh_restore")
                assert control.execute(
                    "SELECT pg_catalog.has_database_privilege(%s, %s, 'CONNECT')",
                    ("hh_owner", name),
                ).fetchone()[0] is True


def _drop_verified(project, name, oid):
    """Drop `name` only while it still carries `oid` (an identity a test read back after
    the CREATE). Used to clean up resources tests deliberately disarrange; it is not part
    of the module's registry.

    It goes through the same sequential protocol `_drop` uses: the row is read by OID, the
    name must still map to it, and `_drop_identity` re-checks OID, owner, marker and
    sessions before the plain DROP. A failure to read the identity leaves the resource in
    place, never adopted."""
    if not oid:
        return
    with project.run.control() as control:
        row = control.execute(_PROVENANCE_BY_OID, (oid,)).fetchone()
    if row is None:
        return
    assert str(row[1]) == name, f"oid {oid} is named {str(row[1])!r}, not {name!r}"
    expected = _OwnedDatabase(
        oid=oid, marker=row[2], owner=str(row[3]), marked=row[2] is not None
    )
    _drop_identity(project, name, expected)


class TestCleanupIdentity(_LiveTest):
    """F5: destructive cleanup needs the identity the resource was registered with."""

    def test_a_replacement_under_a_registered_name_is_not_dropped(self, project):
        original = _name()
        moved = _name()
        _create(project, original)
        owned = _CREATED[original]
        replacement_oid = None
        try:
            with project.run.control() as control:
                control.execute(f'ALTER DATABASE "{original}" RENAME TO "{moved}"')
                control.execute(
                    f'CREATE DATABASE "{original}" OWNER "hh_owner" TEMPLATE template0'
                )
                replacement_oid = int(
                    control.execute(_OID_BY_NAME, (original,)).fetchone()[0]
                )
            with pytest.raises(AssertionError) as refused:
                _drop(project, original)
            assert "different database" in str(refused.value)
            assert original in _CREATED  # kept for the operator, not silently forgotten
            with project.run.control() as control:
                assert control.execute(_EXISTS, (original,)).fetchone() is not None
                assert control.execute(_EXISTS, (moved,)).fetchone() is not None
        finally:
            _CREATED.pop(original, None)
            with project.run.control() as control:
                if replacement_oid is None:  # a failure before the OID was read
                    row = control.execute(_OID_BY_NAME, (original,)).fetchone()
                    replacement_oid = int(row[0]) if row is not None else 0
            _drop_verified(project, original, replacement_oid)
            _drop_verified(project, moved, owned.oid)

    def test_a_renamed_database_is_not_dropped_under_its_old_name(self, project):
        original = _name()
        moved = _name()
        _create(project, original)
        owned = _CREATED[original]
        try:
            with project.run.control() as control:
                control.execute(f'ALTER DATABASE "{original}" RENAME TO "{moved}"')
            with pytest.raises(AssertionError) as refused:
                _drop(project, original)
            assert "is now named" in str(refused.value)
            assert original in _CREATED
            with project.run.control() as control:
                assert control.execute(_EXISTS, (moved,)).fetchone() is not None
        finally:
            _CREATED.pop(original, None)
            _drop_verified(project, moved, owned.oid)

    def test_a_failed_setup_still_drops_the_database_by_its_identity(self, project):
        name = _name()
        with pytest.raises(RuntimeError):
            with _database(project, name=name, enable=True) as created:
                assert created == name
                raise RuntimeError("setup failed after the database existed")
        assert name not in _CREATED
        with project.run.control() as control:
            assert control.execute(_EXISTS, (name,)).fetchone() is None

    def test_a_disarranged_database_is_reported_and_preserved(self, project):
        """A changed owner is one of the identity facts, so cleanup refuses."""
        name = _name()
        _create(project, name)
        try:
            with project.run.control() as control:
                control.execute(f'ALTER DATABASE "{name}" OWNER TO "hh_restore"')
            with pytest.raises(AssertionError) as refused:
                _drop(project, name)
            assert "owned by" in str(refused.value)
            with project.run.control() as control:
                assert control.execute(_EXISTS, (name,)).fetchone() is not None
        finally:
            owned = _CREATED.pop(name)
            with project.run.control() as control:
                control.execute(f'ALTER DATABASE "{name}" OWNER TO "{owned.owner}"')
            _drop_verified(project, name, owned.oid)

    def test_the_verified_drop_removes_exactly_the_registered_oid(self, project):
        """The positive half: the protocol really drops the registered resource."""
        name = _name()
        _create(project, name)
        oid = _CREATED[name].oid
        _drop(project, name)
        assert name not in _CREATED
        with project.run.control() as control:
            assert control.execute(
                "SELECT count(*) FROM pg_catalog.pg_database d WHERE d.oid = %s", (oid,)
            ).fetchone()[0] == 0
            assert control.execute(_EXISTS, (name,)).fetchone() is None

    def test_cleanup_refuses_while_a_session_is_attached(self, project):
        """A database that still has a client backend is reported, not forced: there is no
        `WITH (FORCE)` anywhere, so the session is either closed by its owner or the
        database stays as a reported leftover."""
        name = _name()
        _create(project, name, allowconn=True)  # no ACL work: only the session matters
        with _superuser(project, name):
            with pytest.raises(AssertionError) as refused:
                _drop(project, name)
            assert "still has a session attached" in str(refused.value)
            assert name in _CREATED
            with project.run.control() as control:
                assert control.execute(_EXISTS, (name,)).fetchone() is not None
        _drop(project, name)  # the session is closed: the same record now drops cleanly
        assert name not in _CREATED

    def test_the_destructive_statement_is_a_plain_drop(self):
        """The simplification is a contract, not a style: no `WITH (FORCE)`, no rename,
        no option that could delete a database this module cannot identify."""
        assert _DROP_DATABASE == 'DROP DATABASE "{}"'
        assert "FORCE" not in _DROP_DATABASE.upper()
        assert "RENAME" not in _DROP_DATABASE.upper()


def _snapshot(project, name, target):
    """Everything a classification could conceivably change: the database row, the
    cluster's role/membership/setting counts, and the target catalog's object counts."""
    with project.run.control() as control:
        database = control.execute(
            "SELECT datallowconn, pg_get_userbyid(datdba), datacl::text, datcollate, "
            "datctype, datlocprovider FROM pg_database WHERE datname = %s",
            (name,),
        ).fetchone()
        roles = control.execute("SELECT count(*) FROM pg_roles").fetchone()[0]
        memberships = control.execute("SELECT count(*) FROM pg_auth_members").fetchone()[0]
        settings = control.execute("SELECT count(*) FROM pg_db_role_setting").fetchone()[0]
    catalog = (
        target.execute("SELECT count(*) FROM pg_class").fetchone()[0],
        target.execute("SELECT count(*) FROM pg_namespace").fetchone()[0],
    )
    return (tuple(database), roles, memberships, settings, catalog)
