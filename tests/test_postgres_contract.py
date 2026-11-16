"""The frozen role/ACL contracts, and the checks that catch escalation - OFFLINE (R3-05/R3-06).

The live measurements are in tests/test_postgres_roles.py. These tests prove the checks
themselves: the allowed membership graph accepts exactly the specification's two
delegated-owner edges, an extra edge such as `purge -> app` is reported, a wrong
SET/INHERIT option is reported, the `pg_database_owner` shape is database-specific, and a
psycopg notice is copied to plain values inside its callback (the `Diagnostic` is not
valid afterwards).
"""

import pytest

from tests import postgres_contract as contract

REQUIRED = [
    ("migrate", "owner", True, False),
    ("restore", "owner", True, False),
]


# --- R3-06: the allowed membership graph -------------------------------------------------------


def test_the_frozen_graph_is_accepted():
    assert contract.membership_violations(REQUIRED) == []


def test_a_missing_required_edge_is_reported():
    for missing in (("migrate", "owner", True, False), ("restore", "owner", True, False)):
        assert contract.membership_violations(
            [edge for edge in REQUIRED if edge != missing]
        ), missing


@pytest.mark.parametrize(
    "edge",
    [
        pytest.param(("purge", "app", False, True), id="purge-can-become-the-app"),
        pytest.param(("app", "owner", True, False), id="app-can-become-the-owner"),
        pytest.param(("migrate", "app", True, False), id="migrate-can-become-the-app"),
        pytest.param(("backup", "pg_read_all_data", False, True), id="backup-gets-a-predefined-role"),
        pytest.param(("migrate", "owner", True, True), id="inherit-instead-of-only-set"),
        pytest.param(("restore", "owner", False, False), id="membership-without-set"),
    ],
)
def test_an_unexpected_edge_is_reported(edge):
    violations = contract.membership_violations([*REQUIRED, edge])

    assert len(violations) == 1 and str(edge[0]) in violations[0] and str(edge[1]) in violations[0]


def test_the_set_and_inherit_options_are_checked_independently():
    violations = contract.membership_violations([("migrate", "owner", False, False), *REQUIRED[1:]])

    assert len(violations) == 1 and "SET=False" in violations[0]


# --- R3-06: pg_database_owner, database-specific -------------------------------------------------


def test_the_owners_database_allows_the_owner_and_its_delegated_chains():
    measured = contract.PG_DATABASE_OWNER_IN_OWNERS_DATABASE

    assert contract.pg_database_owner_violations(measured, owners_database=True) == []


def test_any_other_database_allows_nothing():
    measured = {key: (False, False, False) for key in contract.PROJECT_KEYS}

    assert contract.pg_database_owner_violations(measured, owners_database=False) == []


def test_the_owner_shape_outside_the_owners_database_is_reported():
    measured = dict(contract.PG_DATABASE_OWNER_ELSEWHERE) | {"owner": (True, True, False)}

    violations = contract.pg_database_owner_violations(measured, owners_database=False)

    assert len(violations) == 1 and violations[0].startswith("owner")


def test_an_unexpected_delegation_is_reported():
    measured = dict(contract.PG_DATABASE_OWNER_IN_OWNERS_DATABASE) | {"purge": (True, False, False)}

    violations = contract.pg_database_owner_violations(measured, owners_database=True)

    assert len(violations) == 1 and violations[0].startswith("purge")


def test_the_corrected_owner_and_delegated_tuples_are_the_source_derived_ones():
    """R4-05: the owner MEMBERs, USEs and SETs the implicit role; the two delegated SET
    chains MEMBER and SET it but do not inherit it (MEM-03/MEM-01). Runtime logins have
    nothing. No role grant is changed to make this true."""
    assert contract.PG_DATABASE_OWNER_IN_OWNERS_DATABASE == {
        "owner": (True, True, True),
        "migrate": (True, False, True),
        "restore": (True, False, True),
        "app": (False, False, False),
        "purge": (False, False, False),
        "backup": (False, False, False),
        "provision": (False, False, False),
    }
    # The frozen verification status is recorded, not presented as live-confirmed (MEM-08).
    assert "PostgreSQL 16" in contract.PG_DATABASE_OWNER_MATRIX_STATUS
    assert "pending" in contract.PG_DATABASE_OWNER_MATRIX_STATUS


def test_no_runtime_login_may_set_the_implicit_role():
    """The owner and the two delegated chains may assume the implicit role; the four
    runtime logins may not - in the owner's database and anywhere else."""
    assert {
        key
        for key, (_member, _usage, setting) in contract.PG_DATABASE_OWNER_IN_OWNERS_DATABASE.items()
        if setting
    } == {"owner", "migrate", "restore"}
    for owners_database in (True, False):
        expected = (
            contract.PG_DATABASE_OWNER_IN_OWNERS_DATABASE
            if owners_database
            else contract.PG_DATABASE_OWNER_ELSEWHERE
        )
        measured = {
            key: (member, usage, True) for key, (member, usage, _) in expected.items()
        }

        violations = contract.pg_database_owner_violations(
            measured, owners_database=owners_database
        )

        offenders = {item.split(":")[0] for item in violations}
        if owners_database:
            # the owner and its delegated chains may SET it; the four runtime logins may not
            assert offenders == {"app", "purge", "backup", "provision"}, violations
        else:
            # outside the owner's database nothing may be true at all
            assert offenders == set(contract.PROJECT_KEYS), violations


# --- R3-05: a Diagnostic is copied inside the callback -------------------------------------------


class ExpiringDiagnostic:
    """Valid while the callback runs; dereferencing it later raises (what psycopg's
    Diagnostic does when it is retained beyond the callback)."""

    def __init__(self):
        self.alive = True
        self.sqlstate = "01007"
        self.severity = "WARNING"
        self.message_primary = "no privileges were granted"

    def __getattribute__(self, name):
        if name in ("sqlstate", "severity", "message_primary") and not object.__getattribute__(self, "alive"):
            raise RuntimeError("the Diagnostic is no longer valid")
        return object.__getattribute__(self, name)


def test_the_notice_callback_copies_plain_values_and_keeps_no_diagnostic():
    records, record = contract.notice_recorder()
    diagnostic = ExpiringDiagnostic()

    record(diagnostic)
    diagnostic.alive = False  # the connection has moved on

    assert records == [("01007", "WARNING", "no privileges were granted")]
    assert all(not hasattr(value, "sqlstate") for row in records for value in row)


def test_the_erratum_path_is_recorded_as_approved():
    """R3-05 item 8, inverted by FE-6: the marker records the approval, and no stale
    pending marker may survive."""
    text = (
        __import__("pathlib").Path(__file__).with_name("test_postgres_roles.py").read_text(
            encoding="utf-8"
        )
    )

    assert "OWNER-APPROVED AMENDMENT FE-6" in text and "PROPOSED ERRATUM" not in text


# --- R3-05: the effect check ---------------------------------------------------------------------


def snapshot(public_rows=(), public_columns=(), effective_public=(), public_column_values=()):
    everything_false = (False,) * len(contract.TABLE_PRIVILEGES)
    public_column_values = tuple(public_column_values)
    return (
        (),
        (),
        (),
        tuple(public_rows),
        tuple(public_columns),
        {"PUBLIC": tuple(effective_public) or everything_false},
        {"PUBLIC": public_column_values or (("id", "SELECT", False),)},
    )


def test_a_snapshot_with_no_public_privilege_is_accepted():
    assert contract.unauthorized_privileges(snapshot()) == []


def test_a_public_table_grant_in_the_acl_is_reported():
    assert contract.unauthorized_privileges(snapshot(public_rows=[("SELECT", "NO")]))


def test_an_effective_public_privilege_is_reported_even_with_an_empty_acl_view():
    values = (True,) + (False,) * (len(contract.TABLE_PRIVILEGES) - 1)

    assert contract.unauthorized_privileges(snapshot(effective_public=values))


def test_an_effective_public_column_privilege_is_reported():
    assert contract.unauthorized_privileges(
        snapshot(public_column_values=(("token_hash", "SELECT", True),))
    )


# --- R4-07: the captured GRANT notice is strict -------------------------------------------------


THE_WARNING = ("01007", "WARNING", 'no privileges were granted for "users"')


def test_the_expected_grant_warning_is_accepted():
    assert contract.grant_notice_violations([THE_WARNING], table="users") == []


def test_a_missing_grant_warning_is_reported():
    violations = contract.grant_notice_violations([], table="users")

    assert len(violations) == 1 and "01007" in violations[0]


def test_a_warning_with_the_wrong_severity_is_reported():
    for severity in ("NOTICE", "WARNING ", None):
        violations = contract.grant_notice_violations(
            [("01007", severity, 'no privileges were granted for "users"')], table="users"
        )

        assert any("severity" in item for item in violations), severity


def test_a_warning_that_does_not_name_the_attempted_table_is_reported():
    violations = contract.grant_notice_violations(
        [("01007", "WARNING", 'no privileges were granted for "sessions"')], table="users"
    )

    assert len(violations) == 1 and "name the table" in violations[0]


def test_a_relation_whose_name_merely_contains_the_table_is_not_evidence():
    """R5-07: `"users"` is a substring of `"other_users"`; the identifier must be equal."""
    violations = contract.grant_notice_violations(
        [("01007", "WARNING", 'no privileges were granted for "other_users"')], table="users"
    )

    assert len(violations) == 1 and "name the table" in violations[0]


def test_the_column_level_notice_is_attributed_to_its_relation():
    violations = contract.grant_notice_violations(
        [
            (
                "01007",
                "WARNING",
                'no privileges were granted for column "token_hash" of relation "users"',
            )
        ],
        table="users",
    )

    assert violations == []


def test_a_warning_naming_two_relations_is_ambiguous_and_reported():
    violations = contract.grant_notice_violations(
        [("01007", "WARNING", 'no privileges were granted for "users" or "other_users"')],
        table="users",
    )

    assert len(violations) == 1 and "name the table" in violations[0]


def test_an_unrelated_diagnostic_in_the_windows_is_reported_by_sqlstate_only():
    violations = contract.grant_notice_violations(
        [THE_WARNING, ("42501", "ERROR", "permission denied for table users")], table="users"
    )

    assert len(violations) == 1 and "42501" in violations[0]
    assert "permission denied" not in violations[0]  # the message content is not echoed


def test_a_second_01007_diagnostic_is_unexpected():
    violations = contract.grant_notice_violations([THE_WARNING, THE_WARNING], table="users")

    assert len(violations) == 1 and "exactly one" in violations[0]
