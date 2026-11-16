"""The validated renderer and runner for the WP-1B SQL artifacts (HH-DB-01 Stage 2).

`db/roles.sql`, `db/bootstrap.sql` and `db/privileges.sql` are the checked-in artifacts of
the frozen role/ACL contract. This module is the ONLY thing that renders or executes them:

  * every substituted value is validated against a fixed allowlist BEFORE any SQL text is
    built (`validate_database_name`, `validate_grantee`, `validate_role_name`,
    `validate_maintenance_database`). There is no general identifier substitution and no
    SQL text is derived from unvalidated input;
  * `db/bootstrap.sql` is phase-separated (`-- @phase: create|harden|enable`). The CREATE
    is sent alone on an autocommit connection (PostgreSQL refuses it in a transaction,
    SQLSTATE 25001); the E3-a and E3-b catalog checks run before E3-c may enable
    connections, and a failed check never reaches the ALTER;
  * `db/roles.sql` and `db/privileges.sql` each run in ONE transaction supplied here (the
    files contain no BEGIN/COMMIT);
  * every connection to a project database is built from the harness's validated `Target`
    and verified with the harness's own `Expected`/`verify_connection` (peer, port,
    database, owner, identity and the independently established system identifier).

The Stage 2 WP-1B brief limits this work to the isolated test environment: no deployment,
no Production access and no credential is embedded here. The test-only credentials the
suite sets after `roles.sql` are generated per process and never printed.

Refusal type: `ArtifactError` carries a stable `code` (asserted by tests) and fixed help
text. `detail` is optional and holds only non-secret names/values for a test's message.
"""

from __future__ import annotations

import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from tests import postgres_harness as harness
from tests.postgres_harness import (
    PURGE_COLUMN_UPDATES,
    PURGE_TABLES,
    PROVISION_TABLES,
    RoleIdentity,
    Run,
    UnsafeTarget,
)

SQL_DIR = Path(__file__).resolve().parent.parent / "db"

# §3.2/ROL-04: the exact seven project roles, and nothing else, is what the artifacts name.
PROJECT_ROLE_NAMES = (
    "hh_owner",
    "hh_migrate",
    "hh_app",
    "hh_purge",
    "hh_backup",
    "hh_provision",
    "hh_restore",
)
LOGIN_ROLE_NAMES = tuple(name for name in PROJECT_ROLE_NAMES if name != "hh_owner")
PROJECT_ROLE_SET = frozenset(PROJECT_ROLE_NAMES)

# The complete attribute matrix of roles.sql (accepted Stage 1 matrix, with NOINHERIT for
# all seven as approved for WP-1B).
ROLE_MATRIX: Mapping[str, Mapping[str, bool]] = {
    "hh_owner": {
        "login": False, "superuser": False, "createdb": False,
        "createrole": False, "inherit": False, "replication": False, "bypassrls": False,
    },
    "hh_migrate": {
        "login": True, "superuser": False, "createdb": False,
        "createrole": False, "inherit": False, "replication": False, "bypassrls": False,
    },
    "hh_app": {
        "login": True, "superuser": False, "createdb": False,
        "createrole": False, "inherit": False, "replication": False, "bypassrls": False,
    },
    "hh_purge": {
        "login": True, "superuser": False, "createdb": False,
        "createrole": False, "inherit": False, "replication": False, "bypassrls": False,
    },
    "hh_backup": {
        "login": True, "superuser": False, "createdb": False,
        "createrole": False, "inherit": False, "replication": False, "bypassrls": False,
    },
    "hh_provision": {
        "login": True, "superuser": False, "createdb": False,
        "createrole": False, "inherit": False, "replication": False, "bypassrls": False,
    },
    "hh_restore": {
        "login": True, "superuser": False, "createdb": True,
        "createrole": False, "inherit": False, "replication": False, "bypassrls": False,
    },
}

# ROL-17, the complete `rolconfig` set per role (sorted, as roles.sql verifies it).
ROLE_SETTINGS: Mapping[str, tuple[str, ...]] = {
    "hh_owner": (),
    "hh_migrate": ("application_name=hh_migrate", "role=hh_owner"),
    "hh_app": (
        "application_name=hh_web",
        "idle_in_transaction_session_timeout=60s",
        "lock_timeout=5s",
        "search_path=public",
        "statement_timeout=30s",
    ),
    "hh_purge": ("application_name=hh_purge",),
    "hh_backup": ("application_name=hh_backup",),
    "hh_provision": ("application_name=hh_provision",),
    "hh_restore": (),
}

# The only membership edges: (admin, inherit, set) options of PG 16 `pg_auth_members`.
EXPECTED_EDGES: Mapping[tuple[str, str], tuple[bool, bool, bool]] = {
    ("hh_migrate", "hh_owner"): (False, False, True),
    ("hh_restore", "hh_owner"): (False, False, True),
}

SERVING_DATABASE = "house_helper"
_RESTORE_DATABASE = re.compile(r"house_helper_restore_[a-z0-9]{1,32}")
MAINTENANCE_DATABASES = frozenset({"postgres", "house_helper_admin"})
GRANTEES = frozenset({"hh_migrate", "hh_restore"})

# The only databases the runner may create, and the only maintenance databases it may
# execute from (§5.1, §8.8 RST-03/RST-07, OD-2).
def validate_database_name(name: object) -> str:
    if not isinstance(name, str):
        raise ArtifactError("database_name", "a database name must be text")
    if name == SERVING_DATABASE or _RESTORE_DATABASE.fullmatch(name):
        return name
    raise ArtifactError(
        "database_name",
        "the database name is not house_helper or house_helper_restore_<token> "
        "with <token> matching [a-z0-9]{1,32}",
    )


def validate_maintenance_database(name: object) -> str:
    if isinstance(name, str) and name in MAINTENANCE_DATABASES:
        return name
    raise ArtifactError("maintenance_database", "the maintenance database is not an allowed one")


def validate_grantee(name: object) -> str:
    if isinstance(name, str) and name in GRANTEES:
        return name
    raise ArtifactError("grantee", "the CONNECT grantee is not hh_migrate or hh_restore")


def validate_database_grantee(database: object, grantee: object) -> tuple[str, str]:
    """The exact RST-04/RST-07 binding: a serving database takes `hh_migrate`, a restore
    database takes `hh_restore`, and NOTHING else. Both reverse combinations are refused
    here - before any SQL is rendered, any connection is used or anything is mutated - so a
    caller-supplied wrong grantee can never become the expected security state."""
    name = validate_database_name(database)
    role = validate_grantee(grantee)
    expected = "hh_migrate" if name == SERVING_DATABASE else "hh_restore"
    if role != expected:
        raise ArtifactError(
            "grantee_database",
            "the CONNECT grantee does not match the database kind "
            "(house_helper takes hh_migrate; house_helper_restore_<token> takes hh_restore)",
            f"{name}/{role}",
        )
    return name, role


def validate_role_name(name: object) -> str:
    if isinstance(name, str) and name in PROJECT_ROLE_SET:
        return name
    raise ArtifactError("role_name", "the role is not one of the seven project roles")


def validate_login_role(name: object) -> str:
    if isinstance(name, str) and name in LOGIN_ROLE_NAMES:
        return name
    raise ArtifactError("login_role", "the role is not one of the six project logins")


_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def _quote(validated: str) -> str:
    """Double-quote a value that has ALREADY passed a strict allowlist. No caller may pass
    raw input here; the validators above are the only producers of the seven role names and
    the two database shapes."""
    if not _IDENTIFIER.fullmatch(validated):  # defense in depth, never the only control
        raise ArtifactError("identifier", "an identifier that did not pass its allowlist")
    return f'"{validated}"'


class ArtifactError(Exception):
    """A refused artifact operation. `code` is stable and asserted; `message` is fixed
    help text; `detail` is optional non-secret context (a validated name or catalog fact)."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


# --- loading, rendering and phase boundaries -------------------------------------------------


def _load(filename: str) -> str:
    text = (SQL_DIR / filename).read_text(encoding="utf-8")
    if not text.strip():
        raise ArtifactError("empty_artifact", f"{filename} is empty")
    return text


_PHASE = re.compile(r"^--\s*@phase:\s*(create|harden|enable)\s*$")


def bootstrap_phases(sql: Optional[str] = None) -> Mapping[str, str]:
    """`db/bootstrap.sql` split on its phase markers, in the required order. A missing,
    duplicated, reordered or unknown phase is refused before any SQL runs."""
    text = _load("bootstrap.sql") if sql is None else sql
    order: list[str] = []
    content: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        marker = _PHASE.match(line.strip())
        if marker:
            current = marker.group(1)
            if current in content:
                raise ArtifactError("bootstrap_phase", f"phase {current} appears twice")
            order.append(current)
            content[current] = []
        elif current is not None:
            content[current].append(line)
    if order != ["create", "harden", "enable"]:
        raise ArtifactError("bootstrap_phase", "bootstrap.sql does not declare create, harden, enable in order")
    phases = {name: "\n".join(lines).strip() for name, lines in content.items()}
    for name in ("create", "enable"):
        if _statement_count(phases[name]) != 1:
            raise ArtifactError(
                "bootstrap_phase",
                f"the {name} phase must be exactly one statement (CREATE DATABASE cannot share a query message)",
            )
    return phases


def _statement_count(sql: str) -> int:
    """Top-level statement count for the two single-statement phases: comments and blank
    lines removed, then the terminating semicolons counted."""
    stripped = "\n".join(
        line for line in sql.splitlines() if line.strip() and not line.strip().startswith("--")
    ).strip()
    if not stripped.endswith(";"):
        raise ArtifactError("bootstrap_phase", "a bootstrap phase statement is not terminated")
    return stripped.count(";")


def render_bootstrap(database: object, grantee: object, *, phases: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    """The three phase texts with the two validated IDENTIFIERS substituted (and quoted)."""
    name, role = validate_database_grantee(database, grantee)
    source = bootstrap_phases() if phases is None else phases
    rendered = {}
    for phase, text in source.items():
        rendered[phase] = text.replace("{{database}}", _quote(name)).replace("{{grantee}}", _quote(role))
        if "{{" in rendered[phase]:
            raise ArtifactError("placeholder", "a bootstrap placeholder was not substituted")
    return rendered


def render_privileges(database: object, *, sql: Optional[str] = None) -> str:
    """`db/privileges.sql` with the validated database name in its string-literal guard.

    The placeholder here is inside `'...'` (a value compared with current_database()), so
    it receives the raw validated name, never a quoted identifier. The allowlist admits
    only [a-z0-9_], so the literal cannot be broken out of."""
    name = validate_database_name(database)
    text = _load("privileges.sql") if sql is None else sql
    rendered = text.replace("{{database}}", name)
    if "{{" in rendered:
        raise ArtifactError("placeholder", "the privileges placeholder was not substituted")
    return rendered


# --- connection guards -----------------------------------------------------------------------


def _require_autocommit(connection: Any) -> None:
    if getattr(connection, "autocommit", None) is not True:
        raise ArtifactError("not_autocommit", "the artifact connection must be in autocommit mode")


def _require_superuser(connection: Any) -> None:
    row = connection.execute(
        "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if row is None or row[0] is not True:
        raise ArtifactError("bootstrap_superuser", "this artifact must run as the bootstrap superuser")


def _current_database(connection: Any) -> str:
    return connection.execute("SELECT current_database()").fetchone()[0]


def identity(connection: Any) -> tuple[str, str]:
    row = connection.execute("SELECT session_user, current_user").fetchone()
    return (row[0], row[1])


# --- roles.sql -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleApplyResult:
    created: frozenset[str]
    existing: frozenset[str]


def _role_rows(connection: Any) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
        "rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil, rolconfig "
        "FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
        (list(PROJECT_ROLE_NAMES),),
    ).fetchall()
    return {
        row[0]: {
            "login": row[1], "superuser": row[2], "createdb": row[3],
            "createrole": row[4], "inherit": row[5], "replication": row[6],
            "bypassrls": row[7], "connlimit": row[8], "validuntil": row[9],
            "settings": tuple(sorted(row[10] or ())),
        }
        for row in rows
    }


def membership_edges(connection: Any) -> tuple[tuple[str, str, bool, bool, bool], ...]:
    rows = connection.execute(
        "SELECT member.rolname, granted.rolname, m.admin_option, m.inherit_option, m.set_option "
        "FROM pg_auth_members m "
        "JOIN pg_roles member ON member.oid = m.member "
        "JOIN pg_roles granted ON granted.oid = m.roleid "
        "WHERE member.rolname = ANY(%s) OR granted.rolname = ANY(%s) "
        "ORDER BY 1, 2",
        (list(PROJECT_ROLE_NAMES), list(PROJECT_ROLE_NAMES)),
    ).fetchall()
    return tuple(tuple(row) for row in rows)


class RolesScript:
    """`db/roles.sql`, applied in one transaction and verified against the frozen matrix.

    `preflight` refuses security-relevant drift with a stable code BEFORE the file runs;
    the file's own DO blocks keep the same refusal as the authoritative second layer (for a
    psql caller, or a race between the two checks)."""

    def __init__(self, sql: Optional[str] = None) -> None:
        self.sql = _load("roles.sql") if sql is None else sql

    def apply(
        self,
        connection: Any,
        *,
        claim: Optional[Callable[[frozenset[str]], None]] = None,
    ) -> RoleApplyResult:
        """Create/converge and verify in ONE transaction (R1-05/R2-01).

        Two properties close the cleanup gap that existed when `verify` ran after COMMIT:

        * `claim` receives the roles this run MAY create - the project roles absent from the
          pre-execution snapshot - and is called BEFORE the creation transaction starts.
          A caller that owns cleanup registers them there, so no failure at any later point
          (including a COMMIT whose acknowledgement is lost) can leave an unregistered role.
          A role that already existed is never claimed, so registering can never authorize
          dropping a role this run did not create; a claimed name that a clean rollback left
          uncreated is dropped with IF EXISTS and is otherwise absent, never adopted.
        * Verification runs INSIDE the creation transaction. A verification failure rolls
          the creation back, so there is no committed-but-unverified state to leak."""
        _require_autocommit(connection)
        self.preflight(connection)
        before = _role_rows(connection)
        reserved = frozenset(name for name in PROJECT_ROLE_NAMES if name not in before)
        if claim is not None:
            claim(reserved)
        try:
            with connection.transaction():  # ONE transaction; the file has no BEGIN/COMMIT
                connection.execute(self.sql)
                after = _role_rows(connection)
                self.verify(connection)  # still inside the transaction: no post-COMMIT gap
        except BaseException as error:
            if getattr(error, "sqlstate", None) == "P0001":
                raise ArtifactError(
                    "role_contract", "roles.sql refused the current state; nothing was changed"
                ) from None
            raise
        return RoleApplyResult(
            created=frozenset(set(after) - set(before)),
            existing=frozenset(set(after) & set(before)),
        )

    def preflight(self, connection: Any) -> None:
        """What an EXISTING role/edge may look like before creation and convergence:

        * a role that exists must already match the attribute matrix (a missing one is
          created); its session defaults may be missing or wrong - step 4 overwrites the
          frozen keys - but an EXTRA setting is refused because it is not removed;
        * the membership graph must already be a subset of the two frozen edges with their
          exact options (a missing edge is created);
        * no database-scoped role setting may exist."""
        rows = _role_rows(connection)
        for name, row in rows.items():
            expected = ROLE_MATRIX[name]
            actual = {
                "login": row["login"], "superuser": row["superuser"],
                "createdb": row["createdb"], "createrole": row["createrole"],
                "inherit": row["inherit"], "replication": row["replication"],
                "bypassrls": row["bypassrls"],
            }
            if actual != expected or row["connlimit"] != -1 or row["validuntil"] is not None:
                raise ArtifactError("role_attributes", "a role does not match the frozen matrix", name)
            extra = set(row["settings"]) - set(ROLE_SETTINGS[name])
            if extra:
                raise ArtifactError("role_settings", "a role has an unexpected session default", name)
        for member, granted, admin, inherit, set_option in membership_edges(connection):
            expected = EXPECTED_EDGES.get((member, granted))
            if expected is None:
                raise ArtifactError("role_membership", "an unexpected membership edge exists")
            if (admin, inherit, set_option) != expected:
                raise ArtifactError("role_membership", "a frozen edge has unexpected options")
        if connection.execute(
            "SELECT count(*) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
            "WHERE s.setdatabase <> 0 AND r.rolname = ANY(%s)",
            (list(PROJECT_ROLE_NAMES),),
        ).fetchone()[0] != 0:
            raise ArtifactError("role_settings", "a project role has a database-scoped setting")
        unsafe_parameters = parameter_acl_entries(connection)
        if unsafe_parameters:
            raise ArtifactError(
                "role_parameters",
                "an explicit parameter privilege names a project role or PUBLIC",
                "; ".join(f"{param}:{grantee}:{privilege}" for param, grantee, privilege, _ in unsafe_parameters),
            )

    def verify(self, connection: Any) -> None:
        rows = _role_rows(connection)
        missing = [name for name in PROJECT_ROLE_NAMES if name not in rows]
        if missing:
            raise ArtifactError("role_contract", "a project role is missing", ", ".join(missing))
        for name in PROJECT_ROLE_NAMES:
            row = rows[name]
            expected = ROLE_MATRIX[name]
            actual = {
                "login": row["login"], "superuser": row["superuser"],
                "createdb": row["createdb"], "createrole": row["createrole"],
                "inherit": row["inherit"], "replication": row["replication"],
                "bypassrls": row["bypassrls"],
            }
            if actual != expected:
                raise ArtifactError("role_attributes", "a role does not match the frozen matrix", name)
            if row["connlimit"] != -1 or row["validuntil"] is not None:
                raise ArtifactError("role_attributes", "a role has an unexpected limit or validity", name)
            if row["settings"] != tuple(sorted(ROLE_SETTINGS[name])):
                raise ArtifactError("role_settings", "a role has unexpected session defaults", name)

        edges = membership_edges(connection)
        expected_edges = tuple(
            (member, granted, options[0], options[1], options[2])
            for (member, granted), options in sorted(EXPECTED_EDGES.items())
        )
        if edges != expected_edges:
            raise ArtifactError("role_membership", "the project membership graph is not the frozen graph")

        database_settings = connection.execute(
            "SELECT count(*) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
            "WHERE s.setdatabase <> 0 AND r.rolname = ANY(%s)",
            (list(PROJECT_ROLE_NAMES),),
        ).fetchone()[0]
        if database_settings != 0:
            raise ArtifactError("role_settings", "a project role has a database-scoped setting")
        unsafe_parameters = parameter_acl_entries(connection)
        if unsafe_parameters:
            raise ArtifactError(
                "role_parameters",
                "an explicit parameter privilege names a project role or PUBLIC",
                "; ".join(f"{param}:{grantee}:{privilege}" for param, grantee, privilege, _ in unsafe_parameters),
            )

    def snapshot(self, connection: Any) -> dict[str, Any]:
        """Everything roles.sql owns, in a comparable form (never a password verifier)."""
        rows = _role_rows(connection)
        return {
            "attributes": {
                name: tuple(rows[name][key] for key in (
                    "login", "superuser", "createdb", "createrole", "inherit",
                    "replication", "bypassrls", "connlimit", "validuntil", "settings",
                ))
                for name in PROJECT_ROLE_NAMES
            },
            "memberships": membership_edges(connection),
            "database_settings": connection.execute(
                "SELECT count(*) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
                "WHERE s.setdatabase <> 0 AND r.rolname = ANY(%s)",
                (list(PROJECT_ROLE_NAMES),),
            ).fetchone()[0],
            "parameters": parameter_acl_entries(connection),
        }


def password_verifier(connection: Any, login: object) -> Optional[str]:
    """The stored SCRAM verifier, readable only by the bootstrap superuser. Never print it."""
    name = validate_login_role(login)
    row = connection.execute(
        "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (name,)
    ).fetchone()
    return None if row is None else row[0]


def set_test_password(connection: Any, login: object, password: str) -> None:
    """Configure a generated, test-only credential AFTER roles.sql (OD-8 is not approved).

    The credential is not part of the artifact and is never printed. This is also what
    lets a later `roles.sql` run be observed to preserve an existing verifier."""
    name = validate_login_role(login)
    if not isinstance(password, str) or not password:
        raise ArtifactError("password", "a test credential must be a non-empty string")
    from psycopg import sql as psql_sql

    connection.execute(
        psql_sql.SQL("ALTER ROLE {} PASSWORD {}").format(
            psql_sql.Identifier(name), psql_sql.Literal(password)
        )
    )


def generate_test_passwords() -> Mapping[str, str]:
    """One random, per-process credential for each project login. Never printed, never stored."""
    return {name: secrets.token_hex(24) for name in LOGIN_ROLE_NAMES}


# --- bootstrap.sql ---------------------------------------------------------------------------


@dataclass
class BootstrapScript:
    """`db/bootstrap.sql` with the E3 boundary enforced between its phases."""

    sql: Optional[str] = None

    def __post_init__(self) -> None:
        self._phases = bootstrap_phases(self.sql)
        self._created: list[str] = []

    @property
    def created(self) -> tuple[str, ...]:
        return tuple(self._created)

    def run(
        self,
        control: Any,
        *,
        database: object,
        grantee: object,
        after_harden: Optional[Callable[[Any, str], None]] = None,
    ) -> str:
        name, role = validate_database_grantee(database, grantee)  # before any SQL or mutation
        _require_autocommit(control)
        if _current_database(control) not in MAINTENANCE_DATABASES:
            raise ArtifactError(
                "maintenance_database",
                "bootstrap must run from an allowed maintenance database",
                _current_database(control),
            )
        _require_superuser(control)
        missing = [
            role for role in PROJECT_ROLE_NAMES
            if control.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None
        ]
        if missing:
            raise ArtifactError(
                "roles_missing", "the project roles are not all present; apply db/roles.sql first",
                ", ".join(missing),
            )
        if self._exists(control, name):
            raise ArtifactError("database_exists", "the database already exists; it is not adopted")

        rendered = render_bootstrap(name, role, phases=self._phases)
        try:
            control.execute(rendered["create"])  # one statement, autocommit, no BEGIN
        except BaseException as error:
            if _is_duplicate(error):
                raise ArtifactError("database_exists", "the database already exists; it is not adopted") from None
            raise
        # Only a confirmed CREATE is tracked for cleanup (the name did not pre-exist).
        self._created.append(name)
        self.verify_e3_a(control, name)
        with control.transaction():  # REVOKE + GRANT are transactional; no access before E3-c
            control.execute(rendered["harden"])
        if after_harden is not None:
            after_harden(control, name)
        self.verify_disabled(control, name, role)  # E3-b gates E3-c; no enable on a failure
        control.execute(rendered["enable"])
        self.verify_enabled(control, name, role)
        return name

    def _exists(self, control: Any, name: str) -> bool:
        return control.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone() is not None

    def database_facts(self, control: Any, name: str) -> Mapping[str, Any]:
        row = control.execute(
            "SELECT datallowconn, pg_get_userbyid(datdba), pg_encoding_to_char(encoding), "
            "datcollate, datctype, datlocprovider, datacl IS NOT NULL "
            "FROM pg_database WHERE datname = %s",
            (name,),
        ).fetchone()
        if row is None:
            raise ArtifactError("database_missing", "the database is not present")
        return {
            "allowconn": row[0], "owner": row[1], "encoding": row[2],
            "collate": row[3], "ctype": row[4], "provider": row[5], "acl_materialized": row[6],
        }

    def connect_matrix(self, control: Any, name: str) -> Mapping[str, bool]:
        return {
            role: control.execute(
                "SELECT has_database_privilege(%s, %s, 'CONNECT')", (role, name)
            ).fetchone()[0]
            for role in PROJECT_ROLE_NAMES
        }

    def public_entries(self, control: Any, name: str) -> int:
        return control.execute(
            "SELECT count(*) FROM pg_database d, aclexplode(d.datacl) a "
            "WHERE d.datname = %s AND a.grantee = 0 "
            "AND a.privilege_type IN ('CONNECT', 'TEMPORARY')",
            (name,),
        ).fetchone()[0]

    def verify_e3_a(self, control: Any, name: str) -> None:
        """E3-a: connections disabled, ownership/encoding/locale as frozen. Default PUBLIC
        privileges are a recognized intermediate state here and are NOT checked."""
        facts = self.database_facts(control, name)
        if facts["allowconn"] is not False:
            raise ArtifactError("e3_a", "E3-a failed: connections are not disabled", name)
        if facts["owner"] != "hh_owner" or facts["encoding"] != "UTF8":
            raise ArtifactError("e3_a", "the database ownership or encoding is not the frozen one", name)
        if (facts["collate"], facts["ctype"], facts["provider"]) != ("C", "C", "c"):
            raise ArtifactError("locale", "the database is not C/C libc", name)

    def verify_disabled(self, control: Any, name: str, grantee: str) -> None:
        """E3-b: after hardening and BEFORE enablement - still disabled, the ACL
        materialized, no PUBLIC entry, CONNECT exactly for the grantee, no TEMPORARY."""
        facts = self.database_facts(control, name)
        if facts["allowconn"] is not False:
            raise ArtifactError("e3_b", "E3-b failed: connections are not disabled", name)
        if facts["owner"] != "hh_owner" or facts["encoding"] != "UTF8":
            raise ArtifactError("e3_b", "the database ownership or encoding is not the frozen one", name)
        if (facts["collate"], facts["ctype"], facts["provider"]) != ("C", "C", "c"):
            raise ArtifactError("locale", "the database is not C/C libc", name)
        if facts["acl_materialized"] is not True:
            raise ArtifactError("e3_b", "E3-b failed: the ACL was not materialized", name)
        if self.public_entries(control, name) != 0:
            raise ArtifactError("e3_b", "E3-b failed: PUBLIC still holds a database privilege", name)
        matrix = self.connect_matrix(control, name)
        for role in PROJECT_ROLE_NAMES:
            expected = role in ("hh_owner", grantee)
            if matrix[role] is not expected:
                raise ArtifactError("e3_b", "E3-b failed: CONNECT is not exactly the grantee's", f"{role}")
        if control.execute(
            "SELECT has_database_privilege(%s, %s, 'TEMPORARY')", (grantee, name)
        ).fetchone()[0] is not False:
            raise ArtifactError("e3_b", "E3-b failed: the grantee holds TEMPORARY", name)

    def verify_enabled(self, control: Any, name: str, grantee: str) -> None:
        facts = self.database_facts(control, name)
        if facts["allowconn"] is not True:
            raise ArtifactError("e3_c", "E3-c failed: connections are still disabled", name)
        if self.public_entries(control, name) != 0:
            raise ArtifactError("e3_c", "E3-c failed: PUBLIC holds a database privilege", name)
        matrix = self.connect_matrix(control, name)
        for role in PROJECT_ROLE_NAMES:
            expected = role in ("hh_owner", grantee)
            if matrix[role] is not expected:
                raise ArtifactError("e3_c", "E3-c failed: CONNECT is not exactly the grantee's", role)

    def drop(self, control: Any, name: object) -> None:
        """Drop only a database this instance created, after re-checking its name and owner."""
        database = validate_database_name(name)
        if database not in self._created:
            raise ArtifactError("not_created", "the database was not created by this bootstrap instance")
        facts = self.database_facts(control, database)
        if facts["owner"] != "hh_owner":
            raise ArtifactError("not_ours", "the database is no longer owned by hh_owner; not dropped")
        control.execute(f"DROP DATABASE {_quote(database)} WITH (FORCE)")
        self._created.remove(database)

    def drop_all(self, control: Any) -> None:
        for name in reversed(list(self._created)):
            self.drop(control, name)


def _is_duplicate(error: BaseException) -> bool:
    return getattr(error, "sqlstate", None) == "42P04" or getattr(error, "pgcode", None) == "42P04"


# --- the BOOT-04 schema contract -------------------------------------------------------------


class SchemaContract:
    """The §5.1 BOOT-04 schema state needed before `db/privileges.sql` can run.

    It is not part of E3 (which ends at E3-c) and it is not the privilege contract: it
    makes `public` belong to `hh_owner` with no PUBLIC entry and exactly USAGE for the six
    logins. It runs on the TARGET database as `hh_migrate`/`hh_restore` with
    `current_user = hh_owner`."""

    def apply(self, connection: Any) -> None:
        session, current = identity(connection)
        if session not in ("hh_migrate", "hh_restore") or current != "hh_owner":
            raise ArtifactError(
                "wrong_phase_identity",
                "the schema contract needs hh_migrate/hh_restore with current_user hh_owner",
                f"{session}/{current}",
            )
        with connection.transaction():
            connection.execute("SET LOCAL search_path = public")
            connection.execute("ALTER SCHEMA public OWNER TO hh_owner")
            connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            connection.execute(
                "GRANT USAGE ON SCHEMA public TO "
                "hh_app, hh_purge, hh_backup, hh_provision, hh_migrate, hh_restore"
            )
        self.verify(connection)

    def verify(self, connection: Any) -> Mapping[str, Any]:
        owner, public_entries = connection.execute(
            "SELECT n.nspowner::regrole::text, "
            "(SELECT count(*) FROM aclexplode(n.nspacl) a WHERE a.grantee = 0) "
            "FROM pg_namespace n WHERE n.nspname = 'public'"
        ).fetchone()
        if owner != "hh_owner" or public_entries != 0:
            raise ArtifactError("schema_contract", "the public schema is not hardened as required")
        matrix = {
            role: (
                connection.execute(
                    "SELECT has_schema_privilege(%s, 'public', 'USAGE')", (role,)
                ).fetchone()[0],
                connection.execute(
                    "SELECT has_schema_privilege(%s, 'public', 'CREATE')", (role,)
                ).fetchone()[0],
            )
            for role in PROJECT_ROLE_NAMES
        }
        for role, (usage, create) in matrix.items():
            if role == "hh_owner":
                # The schema owner holds every schema privilege inherently; the contract
                # grants USAGE to the six LOGINS and no CREATE to any of them.
                if usage is not True or create is not True:
                    raise ArtifactError("schema_contract", "the schema owner lost a privilege", role)
            elif usage is not True or create is not False:
                raise ArtifactError("schema_contract", "schema privileges are not exactly USAGE", role)
        return {"owner": owner, "public_entries": public_entries, "matrix": matrix}


# --- privileges.sql --------------------------------------------------------------------------


class PrivilegesScript:
    """`db/privileges.sql` rendered for ONE validated database and executed in ONE transaction."""

    def __init__(self, sql: Optional[str] = None) -> None:
        self.sql = _load("privileges.sql") if sql is None else sql

    def apply(self, connection: Any, *, database: object) -> None:
        name = validate_database_name(database)
        _require_autocommit(connection)
        session, current = identity(connection)
        if session not in ("hh_migrate", "hh_restore") or current != "hh_owner":
            raise ArtifactError(
                "wrong_phase_identity",
                "privileges.sql needs hh_migrate/hh_restore with current_user hh_owner",
                f"{session}/{current}",
            )
        if _current_database(connection) != name:
            raise ArtifactError(
                "wrong_database", "the connection is not to the parameterized database", _current_database(connection)
            )
        if connection.execute(
            "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'alembic_version'"
        ).fetchone() is None:
            raise ArtifactError("schema_not_initialized", "alembic_version is absent")
        rendered = render_privileges(name, sql=self.sql)
        try:
            with connection.transaction():  # ONE transaction; SET LOCAL lives inside the file
                connection.execute(rendered)
        except BaseException as error:
            if getattr(error, "sqlstate", None) == "P0001":
                raise ArtifactError(
                    "privilege_contract", "privileges.sql refused the current ACL state; nothing was changed"
                ) from None
            raise


# --- connections to the project database ----------------------------------------------------


@contextmanager
def project_connection(
    run: Run,
    database: str,
    login: str,
    password: Optional[str],
    *,
    expected_current_user: Optional[str] = None,
) -> Iterator[Any]:
    """A verified psycopg connection as one project login to a WP-1B database."""
    name = validate_database_name(database)
    role_name = validate_login_role(login)
    if role_name == "hh_migrate":
        current = expected_current_user or "hh_owner"
    else:
        current = expected_current_user or "hh_restore"
    role = RoleIdentity(
        key=role_name, name=role_name, password=password,
        session_user=role_name, current_user=current,
    )
    cluster = run.verified_cluster()
    expected = harness.Expected(
        run.target, name, None, cluster.system_identifier,
        identity=role, database_owner="hh_owner",
    )
    connection = harness._psycopg_connect(run.target.connect_params(name, role))
    try:
        harness.verify_connection(connection, expected)
        yield connection
    finally:
        connection.close()


def assume_owner(connection: Any) -> tuple[str, str]:
    """The explicit RST-01/BOOT-03 transition, observed in both directions."""
    connection.execute("SET ROLE hh_owner")
    session, current = identity(connection)
    if current != "hh_owner":
        raise ArtifactError("wrong_phase_identity", "SET ROLE hh_owner did not take effect")
    connection.execute("SET search_path = public")
    return session, current


def _expected_for(run: Run, database: str, login: str, password: Optional[str], current: str) -> Any:
    role = RoleIdentity(
        key=login, name=login, password=password, session_user=login, current_user=current
    )
    cluster = run.verified_cluster()
    return harness.Expected(
        run.target, database, None, cluster.system_identifier,
        identity=role, database_owner="hh_owner",
    )


def role_engine(run: Run, database: str, login: str, password: Optional[str]) -> Any:
    """A guarded engine for a role whose current_user is fixed at connection time.

    `hh_migrate` arrives as `hh_owner` through its `ALTER ROLE ... SET role` default; the
    runtime logins arrive as themselves. Every connection is verified by the harness's
    guard before the first statement. (The restore login needs `migration_engine`: its
    order requires an explicit `SET ROLE` after connecting.)"""
    from house_helper.persistence.database import create_db_engine

    name = validate_database_name(database)
    role_name = validate_login_role(login)
    current = "hh_owner" if role_name == "hh_migrate" else role_name
    expected = _expected_for(run, name, role_name, password, current)
    url_text = run.target.url(name, expected.identity).render_as_string(hide_password=False)
    return harness.guard_engine(create_db_engine(url_text), expected)


def migration_engine(run: Run, database: str, login: str, password: Optional[str]) -> Any:
    """A guarded engine whose every connection performs the explicit owner transition.

    It verifies the connection as the login first (RST-01: session_user=hh_restore,
    current_user=hh_restore), executes `SET ROLE hh_owner`, COMMITS it (a ROLLBACK would
    revert it), and verifies the required (hh_restore, hh_owner) pair before handing the
    connection out. `hh_migrate` works too: its default already is the owner."""
    import sqlalchemy as sa

    from house_helper.persistence.database import create_db_engine

    name = validate_database_name(database)
    role_name = validate_login_role(login)
    arriving = "hh_owner" if role_name == "hh_migrate" else role_name
    before = _expected_for(run, name, role_name, password, arriving)
    after = _expected_for(run, name, role_name, password, "hh_owner")
    url_text = run.target.url(name, before.identity).render_as_string(hide_password=False)
    engine = create_db_engine(url_text)

    def do_connect(dialect, connection_record, cargs, cparams):
        harness.check_connect_params(before, cparams)
        connection = dialect.connect(*cargs, **cparams)
        try:
            harness.verify_connection(connection, before)
            connection.execute("SET ROLE hh_owner")
            connection.commit()
            harness.verify_connection(connection, after)
        except BaseException:
            connection.close()
            raise
        return connection

    sa.event.listen(engine, "do_connect", do_connect)
    return engine


# --- catalog evidence used by the tests ----------------------------------------------------


def acl_dump(connection: Any) -> Mapping[str, Any]:
    """A comparable dump of every ACL the privilege script owns: table ACLs, column ACLs,
    sequence ACLs and default ACLs. Two dumps equal means a re-run changed nothing."""
    tables = connection.execute(
        "SELECT c.relname, c.relacl::text, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' ORDER BY c.relname"
    ).fetchall()
    columns = connection.execute(
        "SELECT c.relname, a.attname, a.attacl::text FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND a.attnum > 0 AND NOT a.attisdropped "
        "AND a.attacl IS NOT NULL ORDER BY c.relname, a.attname"
    ).fetchall()
    defaults = connection.execute(
        "SELECT COALESCE(n.nspname, '<global>'), d.defaclobjtype, r.rolname, d.defaclacl::text "
        "FROM pg_default_acl d JOIN pg_roles r ON r.oid = d.defaclrole "
        "LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace "
        "WHERE r.rolname = 'hh_owner' ORDER BY 1, 2, 3"
    ).fetchall()
    return {
        "tables": tuple((row[0], row[1], row[2]) for row in tables),
        "columns": tuple((row[0], row[1], row[2]) for row in columns),
        "defaults": tuple(tuple(row) for row in defaults),
    }


def effective_matrix(connection: Any) -> Mapping[str, Any]:
    """The measured table/sequence/column privileges of every project role, for assertions."""
    relations = connection.execute(
        "SELECT c.oid, c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S') ORDER BY c.relname"
    ).fetchall()
    result: dict[str, Any] = {}
    for role in PROJECT_ROLE_NAMES:
        table_matrix: dict[str, dict[str, bool]] = {}
        sequence_matrix: dict[str, dict[str, bool]] = {}
        for oid, relname, kind in relations:
            if kind == "S":
                sequence_matrix[relname] = {
                    privilege: connection.execute(
                        "SELECT has_sequence_privilege(%s, %s, %s)", (role, oid, privilege)
                    ).fetchone()[0]
                    for privilege in ("SELECT", "USAGE", "UPDATE")
                }
            else:
                table_matrix[relname] = {
                    privilege: connection.execute(
                        "SELECT has_table_privilege(%s, %s, %s)", (role, oid, privilege)
                    ).fetchone()[0]
                    for privilege in (
                        "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER",
                    )
                }
        result[role] = {"tables": table_matrix, "sequences": sequence_matrix}
    return result


def purge_column_updates(connection: Any) -> Mapping[str, Mapping[str, bool]]:
    """`has_column_privilege(hh_purge, table, column, 'UPDATE')` for every public column."""
    rows = connection.execute(
        "SELECT c.relname, a.attname "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
        "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY c.relname, a.attname"
    ).fetchall()
    result: dict[str, dict[str, bool]] = {}
    for table, column in rows:
        result.setdefault(table, {})[column] = connection.execute(
            "SELECT has_column_privilege('hh_purge', %s, %s, 'UPDATE')",
            (f"public.{table}", column),
        ).fetchone()[0]
    return result


def default_acl_entries(connection: Any) -> tuple[tuple[str, str, str, str, bool], ...]:
    """Every default ACL entry of `hh_owner`, GLOBAL (`defaclnamespace = 0`) and
    schema-specific: `(namespace_or_<global>, objtype, grantee, privilege, grantable)`.

    A global entry and a schema-specific entry both apply to a new object, so both are
    inspected; an unexpected grant option is drift too."""
    rows = connection.execute(
        "SELECT COALESCE(n.nspname, '<global>'), d.defaclobjtype, "
        "       COALESCE(grantee.rolname, 'PUBLIC'), a.privilege_type, a.is_grantable "
        "FROM pg_default_acl d "
        "JOIN pg_roles owner_role ON owner_role.oid = d.defaclrole "
        "LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace "
        "CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
        "LEFT JOIN pg_roles grantee ON grantee.oid = a.grantee "
        "WHERE owner_role.rolname = 'hh_owner' "
        "ORDER BY 1, 2, 3, 4"
    ).fetchall()
    return tuple(tuple(row) for row in rows)


def parameter_acl_entries(connection: Any) -> tuple[tuple[str, str, str, bool], ...]:
    """Explicit parameter privileges (`pg_parameter_acl`) that name a project role or PUBLIC.

    `pg_parameter_acl` is cluster-wide and records only EXPLICIT grants: a role may SET an
    ordinary context-appropriate parameter with no row here at all. Any row naming a project
    role or PUBLIC is therefore an explicit elevation, not ordinary access."""
    rows = connection.execute(
        "SELECT p.parname, COALESCE(grantee.rolname, 'PUBLIC'), "
        "       a.privilege_type, a.is_grantable "
        "FROM pg_parameter_acl p "
        "CROSS JOIN LATERAL aclexplode(p.paracl) a "
        "LEFT JOIN pg_roles grantee ON grantee.oid = a.grantee "
        "WHERE a.grantee = 0 OR grantee.rolname = ANY(%s) "
        "ORDER BY 1, 2, 3, 4",
        (list(PROJECT_ROLE_NAMES),),
    ).fetchall()
    return tuple(tuple(row) for row in rows)


def public_acl_entries(connection: Any) -> tuple[tuple[str, Optional[str], str, bool], ...]:
    """Every PUBLIC (`grantee = 0`) table/sequence and column entry in `public`.

    The catalog itself is inspected because `has_table_privilege()` does NOT consider a
    column-level grant."""
    relations = connection.execute(
        "SELECT c.relname, NULL::text, a.privilege_type, a.is_grantable "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(c.relacl) a "
        "WHERE n.nspname = 'public' AND a.grantee = 0 "
        "ORDER BY 1, 3"
    ).fetchall()
    columns = connection.execute(
        "SELECT c.relname, att.attname, x.privilege_type, x.is_grantable "
        "FROM pg_attribute att JOIN pg_class c ON c.oid = att.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(att.attacl) x "
        "WHERE n.nspname = 'public' AND att.attnum > 0 AND NOT att.attisdropped "
        "AND x.grantee = 0 "
        "ORDER BY 1, 2, 3"
    ).fetchall()
    return tuple(tuple(row) for row in relations) + tuple(tuple(row) for row in columns)


def grant_option_entries(connection: Any) -> tuple[tuple[str, str, Optional[str], str, str], ...]:
    """Grant options on public-schema relations/columns held by anyone but `hh_owner`.

    The owner's own entries carry grant options inherently and are not part of the matrix;
    no other recipient, including PUBLIC, may have one."""
    rows = connection.execute(
        "SELECT 'table', c.relname, NULL::text, COALESCE(g.rolname, 'PUBLIC'), a.privilege_type "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(c.relacl) a "
        "LEFT JOIN pg_roles g ON g.oid = a.grantee "
        "WHERE n.nspname = 'public' AND a.is_grantable "
        "AND (g.rolname IS NULL OR g.rolname <> 'hh_owner') "
        "UNION ALL "
        "SELECT 'column', c.relname, att.attname, COALESCE(g.rolname, 'PUBLIC'), x.privilege_type "
        "FROM pg_attribute att JOIN pg_class c ON c.oid = att.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(att.attacl) x "
        "LEFT JOIN pg_roles g ON g.oid = x.grantee "
        "WHERE n.nspname = 'public' AND att.attnum > 0 AND NOT att.attisdropped "
        "AND x.is_grantable AND (g.rolname IS NULL OR g.rolname <> 'hh_owner') "
        "ORDER BY 1, 2, 3, 4"
    ).fetchall()
    return tuple(tuple(row) for row in rows)


def database_acl(connection: Any, database: str) -> Mapping[str, Any]:
    name = validate_database_name(database)
    row = connection.execute(
        "SELECT pg_get_userbyid(datdba), datallowconn, "
        "(SELECT count(*) FROM aclexplode(d.datacl) a WHERE a.grantee = 0) "
        "FROM pg_database d WHERE d.datname = %s",
        (name,),
    ).fetchone()
    if row is None:
        raise ArtifactError("database_missing", "the database is not present")
    return {"owner": row[0], "allowconn": row[1], "public_entries": row[2]}


# --- locale conformance ----------------------------------------------------------------------

# The WP-1A finding: a real table column is authoritative. The probes are the accepted
# identity corpus's non-ASCII values plus the two direct ROL-02 probes.
LOCALE_PROBES = ("A", "Ä", "А", "K", "Борщ")

_C_PROBES = {
    "A": "a",
    "Ä": "Ä",       # Ä must NOT fold in C
    "А": "А",       # Cyrillic А must NOT fold in C
    "K": "K",       # Kelvin sign must NOT become "k"
    "Борщ": "Борщ",  # Борщ stays as stored
}


def locale_facts(connection: Any, database: Optional[str] = None) -> Mapping[str, Any]:
    name = database or _current_database(connection)
    row = connection.execute(
        "SELECT pg_encoding_to_char(encoding), datcollate, datctype, datlocprovider "
        "FROM pg_database WHERE datname = %s",
        (name,),
    ).fetchone()
    if row is None:
        raise ArtifactError("database_missing", "the database is not present")
    return {"encoding": row[0], "collate": row[1], "ctype": row[2], "provider": row[3]}


def locale_probes(connection: Any) -> Mapping[str, Mapping[str, Any]]:
    """Run `lower()` on a REAL table column, not a literal (constant folding can differ)."""
    table = f"wp1b_locale_probe_{secrets.token_hex(8)}"
    probes: dict[str, dict[str, Any]] = {}
    with connection.transaction():
        connection.execute(f'CREATE TABLE public."{table}" (value text)')
        try:
            for value in LOCALE_PROBES:
                connection.execute(f'INSERT INTO public."{table}" (value) VALUES (%s)', (value,))
            for value, lowered, octets in connection.execute(
                f'SELECT value, lower(value), octet_length(value) FROM public."{table}" ORDER BY value'
            ).fetchall():
                probes[value] = {"lower": lowered, "octets": octets}
        finally:
            connection.execute(f'DROP TABLE public."{table}"')
    return probes


def assert_c_locale(connection: Any, database: Optional[str] = None) -> Mapping[str, Any]:
    """The C-locale acceptance gate. The facts AND direct column probes must both agree."""
    facts = locale_facts(connection, database)
    if (facts["encoding"], facts["collate"], facts["ctype"], facts["provider"]) != ("UTF8", "C", "C", "c"):
        raise ArtifactError("locale_not_c", "the database is not UTF8 C/C libc", str(facts))
    probes = locale_probes(connection)
    for value, expected in _C_PROBES.items():
        if probes[value]["lower"] != expected:
            raise ArtifactError(
                "locale_folding",
                "lower() folded a value that the ASCII-only rule must keep",
                f"{value!r} -> {probes[value]['lower']!r}",
            )
    if probes["Борщ"]["octets"] != 8:
        raise ArtifactError("locale_storage", "Cyrillic text is not stored as UTF-8")
    return {"facts": facts, "probes": probes}
