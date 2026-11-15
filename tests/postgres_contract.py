"""The frozen role/ACL contracts as values and pure checks (HH-DB-01 R3-05, R3-06).

The live PostgreSQL suites in tests/test_postgres_roles.py measure the server; this module
holds what those measurements are compared with, in a form that can be exercised offline:

  * the **allowed membership graph** of specification §3.2 - the two delegated-owner edges
    and nothing else - so an unexpected edge such as `purge -> app` is reported, not
    overlooked, and the required edges' SET/INHERIT options are asserted independently;
  * the **`pg_database_owner` expectations** (§3.3 MEM-01..MEM-04), which are
    database-specific because the role is evaluated against the current database's owner;
  * the **ACL snapshot** the GRANT negative test compares before and after: full table and
    column ACLs *with grantors and grant options*, `pg_default_acl`, and the effective
    privileges of every relevant identity (including PUBLIC), read in one transaction;
  * the **notice copier** that turns a psycopg `Diagnostic` into plain values inside the
    notice callback - the object is only valid while the callback runs (R3-05).

Nothing here creates, grants or changes anything. The offline tests in
tests/test_postgres_contract.py drive every check with synthetic graphs, including the
escalations it must catch.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

# R5-07: the relation a PostgreSQL 16 "no privileges were granted" warning names. The
# table-level form is `no privileges were granted for "<relname>"`; the column-level form is
# `no privileges were granted for column "<column>" of relation "<relname>"`. Both are
# matched in FULL and the relation identifier is compared for EQUALITY, never by substring
# membership (`"users"` is not a relation match inside `"other_users"`).
_RELATION_NOTICE = re.compile(r'no privileges were granted for "([^"]*)"')
_COLUMN_NOTICE = re.compile(
    r'no privileges were granted for column "[^"]*" of relation "([^"]*)"'
)

# Specification §3.2: the seven project roles, by the key the harness uses.
PROJECT_KEYS = ("owner", "migrate", "app", "purge", "backup", "provision", "restore")

# The only project-role membership edges that may exist: the migration and restore logins
# are SET members of the NOLOGIN owner, never INHERIT members (ROL-03/MEM-03).
EXPECTED_EDGES = {
    ("migrate", "owner"): (True, False),  # (set_option, inherit_option)
    ("restore", "owner"): (True, False),
}

TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

# MEM-02..MEM-04: `pg_database_owner` is implicit and evaluated against the current
# database's owner. The tuples are (MEMBER, USAGE, SET).
#
# R4-05: corrected after the R3 source review. In a database owned by the run's owner:
#
#   * `owner` is the database owner, so it MEMBERs the implicit role by ownership, has its
#     privileges immediately (USAGE) and may assume it (SET);
#   * `migrate` and `restore` are `SET` members of the owner. MEM-03 states that indirect
#     MEMBER is then true for them in the owner's database; MEM-01 defines USAGE as
#     inheritance (the edge is INHERIT FALSE, so USAGE is false) and SET as the permission
#     to assume the role (transitive through the SET edge, so SET is true);
#   * every runtime login has nothing.
#
# STATUS: source-derived against PostgreSQL 16 membership semantics and the exact harness
# configuration (SET TRUE / INHERIT FALSE). It still requires confirmation on the pinned
# PostgreSQL 16.15 image by the first authorized live run (MEM-08). A mismatch there is a
# finding to record, never a value to relax. No role grants are changed to satisfy this
# matrix.
PG_DATABASE_OWNER_MATRIX_STATUS = (
    "source-derived (PostgreSQL 16 semantics, SET TRUE / INHERIT FALSE edges); "
    "confirmation on PostgreSQL 16.15 pending"
)
PG_DATABASE_OWNER_IN_OWNERS_DATABASE = {
    "owner": (True, True, True),
    "migrate": (True, False, True),
    "restore": (True, False, True),
    "app": (False, False, False),
    "purge": (False, False, False),
    "backup": (False, False, False),
    "provision": (False, False, False),
}
PG_DATABASE_OWNER_ELSEWHERE = {
    key: (False, False, False) for key in PROJECT_KEYS
}


def membership_violations(edges: Iterable[Sequence[object]]) -> list[str]:
    """Every difference between `edges` and the allowed graph.

    An edge is `(member, granted, set_option, inherit_option)` where the two names are
    role keys when they are project roles, or the raw role name otherwise. A missing
    required edge, an extra project edge, a wrong option or a project->non-project edge
    is reported. The result is empty only for the frozen graph."""
    violations: list[str] = []
    present: set[tuple[str, str]] = set()
    for member, granted, set_option, inherit_option in edges:
        pair = (str(member), str(granted))
        expected = EXPECTED_EDGES.get(pair)
        if expected is None:
            violations.append(
                f"unauthorized edge {pair[0]} -> {pair[1]} "
                f"(SET={set_option}, INHERIT={inherit_option})"
            )
            continue
        present.add(pair)  # the edge exists; only its options can still be wrong
        if (set_option, inherit_option) != expected:
            violations.append(
                f"edge {pair[0]} -> {pair[1]} has SET={set_option}, INHERIT={inherit_option}; "
                f"expected SET={expected[0]}, INHERIT={expected[1]}"
            )
    for member, granted in sorted(EXPECTED_EDGES):
        if (member, granted) not in present:
            violations.append(f"required edge {member} -> {granted} is missing")
    return violations


def pg_database_owner_violations(
    measured: Mapping[str, Sequence[bool]], *, owners_database: bool
) -> list[str]:
    """`measured` maps a project key to its (MEMBER, USAGE, SET) tuple, measured in one
    database. In a database owned by the run's owner the full shape is allowed; anywhere
    else nothing may be true."""
    expected = (
        PG_DATABASE_OWNER_IN_OWNERS_DATABASE if owners_database else PG_DATABASE_OWNER_ELSEWHERE
    )
    violations: list[str] = []
    for key in PROJECT_KEYS:
        actual = tuple(bool(value) for value in measured.get(key, ()))
        if actual != expected[key]:
            violations.append(f"{key}: {actual} (expected {expected[key]})")
    return violations


# --- notices: copy inside the callback ---------------------------------------------------------


def capture_notice(diagnostic: object) -> tuple[object, object, object]:
    """The three plain values the GRANT test needs, copied WHILE the notice callback runs.

    psycopg's `Diagnostic` is a view over the connection's last error; it must not be
    retained or dereferenced after the callback returns (R3-05)."""
    return (
        getattr(diagnostic, "sqlstate", None),
        getattr(diagnostic, "severity", None),
        getattr(diagnostic, "message_primary", None),
    )


def notice_recorder() -> tuple[list[tuple[object, object, object]], object]:
    """A list and a callback that appends copied values to it."""
    records: list[tuple[object, object, object]] = []

    def record(diagnostic: object) -> None:
        records.append(capture_notice(diagnostic))

    return records, record


# R4-07: the one diagnostic PostgreSQL 16 emits for an unauthorized GRANT (the PROPOSED
# erratum, Owner approval still pending). The test asserts this, together with the
# before/after ACL equality - the SQLSTATE alone is never the contract.
GRANT_WARNING_SQLSTATE = "01007"
GRANT_WARNING_SEVERITY = "WARNING"


def grant_notice_violations(
    notices: Iterable[Sequence[object]], *, table: str
) -> list[str]:
    """The problems with the notices captured BETWEEN the attempted `GRANT ... table` and
    the ACL re-read (R4-07).

    Exactly one diagnostic is expected: SQLSTATE `01007`, severity `WARNING`, and a message
    whose QUOTED relation identifier is exactly `table` - so the notice is correlated with
    the attempted statement. R5-07: equality of the actual identifier, not a substring
    match (`"other_users"` is not evidence for `users`). Any other diagnostic captured in
    that window is unrelated to the contract and is reported (by SQLSTATE only, never by
    message content). The result is empty only when all of that holds."""
    rows = [tuple(notice) for notice in notices]
    problems: list[str] = []
    unrelated = sorted(
        {str(sqlstate) for sqlstate, _severity, _message in rows if sqlstate != GRANT_WARNING_SQLSTATE}
    )
    if unrelated:
        problems.append(
            "unrelated diagnostics were captured with the attempted GRANT: "
            + ", ".join(unrelated)
        )
    warnings = [row for row in rows if row[0] == GRANT_WARNING_SQLSTATE]
    if len(warnings) != 1:
        problems.append(
            f"expected exactly one {GRANT_WARNING_SQLSTATE} warning for the attempted GRANT, "
            f"captured {len(warnings)}"
        )
    elif len(warnings[0]) < 3:
        problems.append("the captured warning does not carry a severity and a message")
    else:
        _sqlstate, severity, message = warnings[0]
        if not isinstance(severity, str) or severity.upper() != GRANT_WARNING_SEVERITY:
            problems.append(
                f"the {GRANT_WARNING_SQLSTATE} diagnostic does not have {GRANT_WARNING_SEVERITY} severity"
            )
        named = None
        if isinstance(message, str):
            match = _COLUMN_NOTICE.fullmatch(message) or _RELATION_NOTICE.fullmatch(message)
            named = match.group(1) if match else None
        if named != table:
            problems.append(
                "the captured warning does not name the table the GRANT tried to change"
            )
    return problems


# --- the ACL snapshot ---------------------------------------------------------------------------


def acl_snapshot(
    connection: object, *, table: str = "users", identities: Sequence[str] = ()
) -> tuple:
    """Every ACL fact an attempted GRANT could change, read in the caller's transaction.

    Complete for the table and its columns: grantee, grantor, privilege and grant option,
    plus `pg_default_acl` for the schema, plus the EFFECTIVE privileges of each named
    identity. PUBLIC is captured through its `aclexplode` entries (grantee 0) and the
    `information_schema` views: it is not a role a `has_*_privilege` call can name, and its
    ACL entries are exactly the effective privileges. Compared before and after the
    attempted statement, before any rollback can conceal a change (R3-05)."""
    table_acl = [
        tuple(row)
        for row in connection.exec_driver_sql(
            "SELECT a.grantee, a.grantor, a.privilege_type, a.is_grantable FROM pg_class c, "
            "aclexplode(c.relacl) a WHERE c.oid = %s::regclass ORDER BY 1, 2, 3, 4",
            (table,),
        )
    ]
    column_acl = [
        tuple(row)
        for row in connection.exec_driver_sql(
            "SELECT a.attname, x.grantee, x.grantor, x.privilege_type, x.is_grantable "
            "FROM pg_attribute a, aclexplode(a.attacl) x WHERE a.attrelid = %s::regclass "
            "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1, 2, 3, 4, 5",
            (table,),
        )
    ]
    default_acl = [
        tuple(row)
        for row in connection.exec_driver_sql(
            "SELECT owner.rolname, a.grantee, a.grantor, a.privilege_type, a.is_grantable "
            "FROM pg_default_acl d "
            "JOIN pg_roles owner ON owner.oid = d.defaclrole, aclexplode(d.defaclacl) a "
            "ORDER BY 1, 2, 3, 4, 5"
        )
    ]
    public = [
        tuple(row)
        for row in connection.exec_driver_sql(
            "SELECT privilege_type, is_grantable FROM information_schema.table_privileges "
            "WHERE table_schema = 'public' AND table_name = %s AND grantee = 'PUBLIC' "
            "ORDER BY 1, 2",
            (table,),
        )
    ]
    public_columns = [
        tuple(row)
        for row in connection.exec_driver_sql(
            "SELECT column_name, privilege_type, is_grantable "
            "FROM information_schema.column_privileges "
            "WHERE table_schema = 'public' AND table_name = %s AND grantee = 'PUBLIC' "
            "ORDER BY 1, 2, 3",
            (table,),
        )
    ]
    columns = [
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
            (table,),
        )
    ]
    effective_table = {}
    for identity in identities:
        effective_table[identity] = tuple(
            connection.exec_driver_sql(
                "SELECT has_table_privilege(%s, %s::regclass, %s)",
                (identity, table, privilege),
            ).scalar()
            for privilege in TABLE_PRIVILEGES
        )
    effective_columns = {}
    for identity in identities:
        effective_columns[identity] = tuple(
            (column, privilege, connection.exec_driver_sql(
                "SELECT has_column_privilege(%s, %s::regclass, %s, %s)",
                (identity, table, column, privilege),
            ).scalar())
            for column in columns
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES")
        )
    return (
        tuple(table_acl),
        tuple(column_acl),
        tuple(default_acl),
        tuple(public),
        tuple(public_columns),
        effective_table,
        effective_columns,
    )


def unauthorized_privileges(snapshot: tuple) -> list[str]:
    """What PUBLIC or a runtime identity holds in a snapshot where it must hold nothing.

    `snapshot` is an `acl_snapshot` result. The GRANT negative test asserts this is empty
    BEFORE and AFTER the attempted statement: the effect, not the SQLSTATE."""
    _, _, _, public_rows, public_columns, effective_table, effective_columns = snapshot
    problems: list[str] = []
    if public_rows:
        problems.append(f"PUBLIC holds table privileges: {public_rows}")
    if public_columns:
        problems.append(f"PUBLIC holds column privileges: {public_columns}")
    # Each runtime identity is asserted against its own before/after snapshot in the live
    # test; here PUBLIC is the one that must hold nothing at all.
    if any(any(values) for identity, values in effective_table.items() if identity == "PUBLIC"):
        problems.append("PUBLIC effectively holds a table privilege")
    held = [
        entry
        for identity, values in effective_columns.items()
        if identity == "PUBLIC"
        for entry in values
        if entry[2]
    ]
    if held:
        problems.append(f"PUBLIC effectively holds column privileges: {held}")
    return problems
