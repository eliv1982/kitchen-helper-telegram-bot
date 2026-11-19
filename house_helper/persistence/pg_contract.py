"""The production PostgreSQL contract: E4 classification, inventories and session guards.

Frozen source: HH-DB-01 §3.2/§3.4 (role and privilege matrix), §4.3 (ENR-10/ENR-11),
§4.5 (ENR-15/ENR-16), §5.2 (BOOT-07/BOOT-08 E4 matrix), §5.3 (BOOT-09/BOOT-10),
§5.1 (BOOT-05 fence) and §3.3 ROL-05/ROL-06.

This module is the production counterpart of the accepted test-side classifier in
`tests/postgres_e4.py`. It intentionally uses the SAME state names, action names and
failure codes, and the same decision rule, so that a verdict read by the host tooling is
the verdict the accepted WP-1C-B golden tests describe. The independent test suite still
exercises its own implementation; `tests/test_pg_production_contract.py` compares the two
on shared fact fixtures.

The queries here qualify every catalog name with `pg_catalog.`. They are executed either
through the host's controlled `psql` session or the migrate executor's `hh_migrate`
connection. The classifier itself is pure: callers observe, then classify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional, Tuple

OWNER_ROLE = "hh_owner"
PROJECT_ROLE_NAMES = (
    "hh_owner",
    "hh_migrate",
    "hh_app",
    "hh_purge",
    "hh_backup",
    "hh_provision",
    "hh_restore",
)
LOGIN_ROLE_NAMES = tuple(name for name in PROJECT_ROLE_NAMES if name != OWNER_ROLE)
SERVING_DATABASE = "house_helper"
BASE_DATABASES = ("postgres", "template0", "template1")
BOOTSTRAP_ROLE = "postgres"
# The frozen WP-2B deployment identity (deploy/compose.postgres.yaml). The schema-step
# authorization is compared with these deployment facts by the worker; Docker-level facts
# such as the container id and the physical `system_identifier` remain host-authoritative.
DEPLOYMENT_COMPOSE_PROJECT = "house-helper-pg"
DEPLOYMENT_CONTAINER_NAME = "house-helper-db"
DEPLOYMENT_VOLUME = "house-helper-pgdata"
MIGRATE_SERVICE_NAME = "migrate"
EXPECTED_ENCODING = "UTF8"
EXPECTED_LOCALE = ("C", "C", "c")
FIRST_NORMAL_OBJECT_ID = 16384

# ENR-07: the predefined-role baseline of the pinned PostgreSQL 16 image. The set is
# *observed and frozen* by Mode A; this is the measured 16.15 baseline against which an
# observation is recognised. A role that merely starts with `pg_` is not thereby a
# predefined role: the observed set must equal this baseline, which is what stops a
# planted `pg_*` role from being adopted as "predefined".
FROZEN_PREDEFINED_ROLES = (
    "pg_checkpoint",
    "pg_create_subscription",
    "pg_database_owner",
    "pg_execute_server_program",
    "pg_monitor",
    "pg_read_all_data",
    "pg_read_all_settings",
    "pg_read_all_stats",
    "pg_read_server_files",
    "pg_signal_backend",
    "pg_stat_scan_tables",
    "pg_use_reserved_connections",
    "pg_write_all_data",
    "pg_write_server_files",
)

# The predefined-to-predefined membership edges `initdb` installs on the pinned image
# (`GRANT ... TO pg_monitor`). They are PostgreSQL's own structure, never a project-role
# delegation, and are frozen separately in the enrollment record (ENR-06/ENR-07).
FROZEN_PREDEFINED_MEMBERSHIPS = (
    ("pg_monitor", "pg_read_all_settings", False, True, True),
    ("pg_monitor", "pg_read_all_stats", False, True, True),
    ("pg_monitor", "pg_stat_scan_tables", False, True, True),
)

# ROL-14, as verified by `db/schema.sql` and `db/privileges.sql`: every non-owner
# `pg_default_acl` entry of `hh_owner`. `(schema, objtype, grantee, privilege, grantable)`.
EXPECTED_DEFAULT_ACLS = (
    ("public", "S", "hh_app", "USAGE", False),
    ("public", "S", "hh_backup", "SELECT", False),
    ("public", "r", "hh_app", "DELETE", False),
    ("public", "r", "hh_app", "INSERT", False),
    ("public", "r", "hh_app", "SELECT", False),
    ("public", "r", "hh_app", "UPDATE", False),
    ("public", "r", "hh_backup", "SELECT", False),
)

_TOAST_NAME = re.compile(r"pg_toast_[0-9]+(?:_index)?")

# --- E4 states and actions (identical to the accepted WP-1C-B vocabulary) --------------------

E4_ABSENT = "absent"
E4_DISABLED_UNHARDENED = "disabled_unhardened"
E4_DISABLED_HARDENED = "disabled_hardened"
E4_ENABLED_MAINTENANCE_ONLY = "enabled_maintenance_only"
E4_FAIL_CLOSED = "fail_closed"

ACTION_CREATE_DATABASE = "create_database"
ACTION_HARDEN_DATABASE_ACL = "harden_database_acl"
ACTION_ENABLE_CONNECTIONS = "enable_connections"
ACTION_VERIFY_TARGET_OBJECTS = "verify_target_objects"
ACTION_INITIALIZE_SCHEMA = "initialize_schema"
ACTION_REMEDIATE = "remediate"

E4_STATES = (
    E4_ABSENT,
    E4_DISABLED_UNHARDENED,
    E4_DISABLED_HARDENED,
    E4_ENABLED_MAINTENANCE_ONLY,
    E4_FAIL_CLOSED,
)
E4_ACTIONS = (
    ACTION_CREATE_DATABASE,
    ACTION_HARDEN_DATABASE_ACL,
    ACTION_ENABLE_CONNECTIONS,
    ACTION_VERIFY_TARGET_OBJECTS,
    ACTION_INITIALIZE_SCHEMA,
    ACTION_REMEDIATE,
)

FAIL_OWNERSHIP = "ownership_conflict"
FAIL_ENCODING = "encoding_mismatch"
FAIL_LOCALE = "locale_mismatch"
FAIL_PUBLIC_ACCESS = "unexpected_public_access"
FAIL_UNEXPECTED_GRANTS = "unexpected_database_grants"
FAIL_ACL_NOT_HARDENED = "database_acl_not_hardened"
FAIL_ACL_UNRECOGNIZED = "unrecognized_database_acl"
FAIL_SESSIONS = "unauthorized_sessions"
FAIL_TABLES = "unexpected_tables"
FAIL_SUBSCRIPTIONS = "unexpected_subscription"
FAIL_CONNECTION_STATE = "unrecognized_connection_state"
FAIL_UNTRUSTED_ENVIRONMENT = "untrusted_observation_environment"
FAIL_STALE_OBSERVATION = "stale_observation"
FAIL_INCONSISTENT_PRIVILEGE = "inconsistent_privilege_observation"

FAILURE_ORDER = (
    FAIL_OWNERSHIP,
    FAIL_ENCODING,
    FAIL_LOCALE,
    FAIL_PUBLIC_ACCESS,
    FAIL_UNEXPECTED_GRANTS,
    FAIL_ACL_NOT_HARDENED,
    FAIL_ACL_UNRECOGNIZED,
    FAIL_SESSIONS,
    FAIL_TABLES,
    FAIL_SUBSCRIPTIONS,
    FAIL_CONNECTION_STATE,
    FAIL_UNTRUSTED_ENVIRONMENT,
    FAIL_STALE_OBSERVATION,
    FAIL_INCONSISTENT_PRIVILEGE,
)

_DEFAULT_PUBLIC = frozenset({("CONNECT", False), ("TEMPORARY", False)})
_DEFAULT_ACL = (
    (OWNER_ROLE, "CONNECT", False),
    (OWNER_ROLE, "CREATE", False),
    (OWNER_ROLE, "TEMPORARY", False),
    ("PUBLIC", "CONNECT", False),
    ("PUBLIC", "TEMPORARY", False),
)
_DEFAULT_GRANTS = frozenset({"CONNECT", "TEMPORARY"})
_EMPTY_BASELINE_CLASSES = frozenset(
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
_SESSION_SCHEMA_PREFIXES = ("pg_temp_", "pg_toast_temp_")


class ContractError(Exception):
    """A fail-closed contract refusal. `code` is stable and never carries a secret."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


# --- the frozen role/session matrix ----------------------------------------------------------


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

EXPECTED_EDGES: Mapping[tuple[str, str], tuple[bool, bool, bool]] = {
    ("hh_migrate", "hh_owner"): (False, False, True),
    ("hh_restore", "hh_owner"): (False, False, True),
}

RUNTIME_ROLES = ("hh_app", "hh_purge", "hh_backup", "hh_provision")


# --- facts -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetInventory:
    objects: Tuple[Tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "objects", tuple(sorted(self.objects)))

    @property
    def entries(self) -> Tuple[Tuple[str, str, str], ...]:
        return self.objects

    def __bool__(self) -> bool:
        return bool(self.objects)


@dataclass(frozen=True)
class DatabaseFacts:
    exists: bool
    owner: Optional[str] = None
    encoding: Optional[str] = None
    collate: Optional[str] = None
    ctype: Optional[str] = None
    provider: Optional[str] = None
    allowconn: Optional[bool] = None
    acl_materialized: bool = False
    acl_entries: Tuple[Tuple[str, str, bool], ...] = ()
    connect: Mapping[str, bool] = field(default_factory=dict)
    temporary: Mapping[str, bool] = field(default_factory=dict)
    sessions: Tuple[Tuple[int, str], ...] = ()
    probe_pid: Optional[int] = None
    target_inventory: Optional[TargetInventory] = None
    subscriptions: Tuple[Tuple[int, str], ...] = ()
    default_acls: Tuple[Tuple[str, str, str, str, str, bool], ...] = ()
    observation: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class E4Verdict:
    state: str
    action: str
    database: str
    grantee: str
    failures: Tuple[str, ...] = ()
    tables_checked: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fail_closed(self) -> bool:
        return self.state == E4_FAIL_CLOSED

    @property
    def may_initialize_schema(self) -> bool:
        return self.action == ACTION_INITIALIZE_SCHEMA


@dataclass(frozen=True)
class RoleFacts:
    name: str
    login: bool
    superuser: bool
    createdb: bool
    createrole: bool
    inherit: bool
    replication: bool
    bypassrls: bool
    connlimit: int
    validuntil: Optional[str]
    settings: Tuple[str, ...]

    def attributes(self) -> Mapping[str, bool]:
        return {
            "login": self.login,
            "superuser": self.superuser,
            "createdb": self.createdb,
            "createrole": self.createrole,
            "inherit": self.inherit,
            "replication": self.replication,
            "bypassrls": self.bypassrls,
        }


@dataclass(frozen=True)
class MembershipEdge:
    member: str
    granted: str
    admin: bool
    inherit: bool
    set_option: bool


# --- the pure privilege model (ported unchanged from the accepted classifier) -----------------


def database_privilege(
    acl_materialized: bool,
    acl_entries: Tuple[Tuple[str, str, bool], ...],
    role: str,
    privilege: str,
) -> bool:
    if not acl_materialized:
        return role == OWNER_ROLE or privilege in _DEFAULT_GRANTS
    return any(
        grantee in (role, "PUBLIC") and name == privilege
        for grantee, name, _grantable in acl_entries
    )


def _ordered(codes: Iterable[str]) -> Tuple[str, ...]:
    seen = set(codes)
    return tuple(code for code in FAILURE_ORDER if code in seen)


def _public_entries(facts: DatabaseFacts) -> Tuple[Tuple[str, bool], ...]:
    if not facts.acl_materialized:
        return tuple(sorted(_DEFAULT_PUBLIC))
    return tuple(
        (privilege, grantable)
        for grantee, privilege, grantable in facts.acl_entries
        if grantee == "PUBLIC"
    )


def _grantee_entries(facts: DatabaseFacts, grantee: str) -> Tuple[Tuple[str, bool], ...]:
    return tuple(
        (privilege, grantable)
        for named, privilege, grantable in facts.acl_entries
        if named == grantee
    )


def _foreign_entries(facts: DatabaseFacts, grantee: str) -> Tuple[Tuple[str, str, bool], ...]:
    return tuple(
        entry
        for entry in facts.acl_entries
        if entry[0] not in (OWNER_ROLE, grantee, "PUBLIC")
    )


def _connect_is_exact(facts: DatabaseFacts, grantee: str) -> bool:
    if facts.connect.get(OWNER_ROLE) is not True or facts.connect.get(grantee) is not True:
        return False
    return all(
        facts.connect.get(role) is False
        for role in PROJECT_ROLE_NAMES
        if role not in (OWNER_ROLE, grantee)
    )


def _acl_hardened(facts: DatabaseFacts, grantee: str) -> bool:
    if not facts.acl_materialized:
        return False
    if _public_entries(facts):
        return False
    if _foreign_entries(facts, grantee):
        return False
    if set(_grantee_entries(facts, grantee)) != {("CONNECT", False)}:
        return False
    if not _connect_is_exact(facts, grantee):
        return False
    return facts.temporary.get(grantee) is False


def _hardening_projection(facts: DatabaseFacts, grantee: str) -> Tuple[Tuple[str, str, bool], ...]:
    entries = _DEFAULT_ACL if not facts.acl_materialized else tuple(facts.acl_entries)
    kept = tuple(
        entry for entry in entries if entry[0] != "PUBLIC" and entry != (grantee, "CONNECT", False)
    )
    return kept + ((grantee, "CONNECT", False),)


def _projected_facts(facts: DatabaseFacts, grantee: str) -> DatabaseFacts:
    entries = _hardening_projection(facts, grantee)
    return replace(
        facts,
        exists=True,
        acl_materialized=True,
        acl_entries=entries,
        connect={
            role: database_privilege(True, entries, role, "CONNECT")
            for role in PROJECT_ROLE_NAMES
        },
        temporary={
            role: database_privilege(True, entries, role, "TEMPORARY")
            for role in PROJECT_ROLE_NAMES
        },
        sessions=(),
        probe_pid=None,
        target_inventory=None,
        subscriptions=(),
        observation=(),
    )


def _acl_convergent(facts: DatabaseFacts, grantee: str) -> bool:
    if _foreign_entries(facts, grantee):
        return False
    if any(entry not in _DEFAULT_PUBLIC for entry in _public_entries(facts)):
        return False
    if any(entry != ("CONNECT", False) for entry in _grantee_entries(facts, grantee)):
        return False
    return _acl_hardened(_projected_facts(facts, grantee), grantee)


def _unauthorized_sessions(facts: DatabaseFacts, grantee: str) -> Tuple[Tuple[int, str], ...]:
    authorized = facts.probe_pid
    return tuple(
        (pid, usename)
        for pid, usename in facts.sessions
        if not (authorized is not None and pid == authorized and usename == grantee)
    )


def _acl_failures(facts: DatabaseFacts, grantee: str) -> Tuple[str, ...]:
    public = _public_entries(facts)
    foreign = _foreign_entries(facts, grantee)
    grantee_entries = set(_grantee_entries(facts, grantee))
    public_default_only = all(entry in _DEFAULT_PUBLIC for entry in public)
    grantee_convergent = grantee_entries <= {("CONNECT", False)}

    if facts.allowconn is False:
        if _acl_hardened(facts, grantee) or _acl_convergent(facts, grantee):
            return ()
        codes = []
        if not public_default_only:
            codes.append(FAIL_PUBLIC_ACCESS)
        if foreign:
            codes.append(FAIL_UNEXPECTED_GRANTS)
        codes.append(FAIL_ACL_UNRECOGNIZED)
        return tuple(codes)

    if facts.allowconn is True:
        if _acl_hardened(facts, grantee):
            return ()
        codes = [FAIL_ACL_NOT_HARDENED]
        if public:
            codes.append(FAIL_PUBLIC_ACCESS)
        if foreign:
            codes.append(FAIL_UNEXPECTED_GRANTS)
        if not grantee_convergent:
            codes.append(FAIL_ACL_UNRECOGNIZED)
        return tuple(codes)

    return ()


def _observation_problems(facts: DatabaseFacts, grantee: str) -> Tuple[Tuple[str, str], ...]:
    problems = list(facts.observation)
    if facts.probe_pid is not None and not any(
        pid == facts.probe_pid and usename == grantee for pid, usename in facts.sessions
    ):
        problems.append(
            (FAIL_STALE_OBSERVATION, "the supplied probe session is not in the session inventory")
        )
    if facts.exists and not facts.observation:
        for privilege, observed in (("CONNECT", facts.connect), ("TEMPORARY", facts.temporary)):
            for role in PROJECT_ROLE_NAMES:
                value = observed.get(role)
                if not isinstance(value, bool):
                    problems.append(
                        (
                            FAIL_INCONSISTENT_PRIVILEGE,
                            f"the effective {privilege} of {role} was not observed",
                        )
                    )
                elif value is not database_privilege(
                    facts.acl_materialized, tuple(facts.acl_entries), role, privilege
                ):
                    problems.append(
                        (
                            FAIL_INCONSISTENT_PRIVILEGE,
                            f"the effective {privilege} of {role} disagrees with the database ACL",
                        )
                    )
    return tuple(problems)


def classify_facts(facts: DatabaseFacts, *, database: str, grantee: str) -> E4Verdict:
    """The E4 decision rule. Same states, actions and failures as the accepted classifier."""
    name, role = validate_database_grantee(database, grantee)

    def verdict(
        state: str,
        action: str,
        failures: Tuple[str, ...] = (),
        observation: Tuple[Tuple[str, str], ...] = (),
    ) -> E4Verdict:
        inventory = facts.target_inventory
        return E4Verdict(
            state=state,
            action=action,
            database=name,
            grantee=role,
            failures=_ordered(failures),
            tables_checked=inventory is not None,
            evidence={
                "owner": facts.owner,
                "encoding": facts.encoding,
                "collate": facts.collate,
                "ctype": facts.ctype,
                "provider": facts.provider,
                "allowconn": facts.allowconn,
                "acl_materialized": facts.acl_materialized,
                "acl_entries": facts.acl_entries,
                "public_entries": _public_entries(facts),
                "connect": dict(facts.connect),
                "temporary": dict(facts.temporary),
                "sessions": facts.sessions,
                "probe_pid": facts.probe_pid,
                "target_inventory": (None if inventory is None else inventory.entries),
                "subscriptions": facts.subscriptions,
                "observation": observation,
            },
        )

    problems = _observation_problems(facts, role)
    if problems:
        return verdict(
            E4_FAIL_CLOSED, ACTION_REMEDIATE, tuple(code for code, _ in problems), problems
        )

    if not facts.exists:
        return verdict(E4_ABSENT, ACTION_CREATE_DATABASE)

    failures = []
    if facts.owner != OWNER_ROLE:
        failures.append(FAIL_OWNERSHIP)
    if facts.encoding != EXPECTED_ENCODING:
        failures.append(FAIL_ENCODING)
    if (facts.collate, facts.ctype, facts.provider) != EXPECTED_LOCALE:
        failures.append(FAIL_LOCALE)
    if _unauthorized_sessions(facts, role):
        failures.append(FAIL_SESSIONS)
    failures.extend(_acl_failures(facts, role))
    if facts.allowconn not in (True, False):
        failures.append(FAIL_CONNECTION_STATE)
    if facts.target_inventory:
        failures.append(FAIL_TABLES)
    if facts.subscriptions:
        failures.append(FAIL_SUBSCRIPTIONS)

    if failures:
        return verdict(E4_FAIL_CLOSED, ACTION_REMEDIATE, tuple(failures))

    if facts.allowconn is False:
        state = (
            E4_DISABLED_HARDENED if _acl_hardened(facts, role) else E4_DISABLED_UNHARDENED
        )
        action = (
            ACTION_ENABLE_CONNECTIONS if state == E4_DISABLED_HARDENED else ACTION_HARDEN_DATABASE_ACL
        )
        return verdict(state, action)

    if facts.target_inventory is None:
        return verdict(E4_ENABLED_MAINTENANCE_ONLY, ACTION_VERIFY_TARGET_OBJECTS)
    return verdict(E4_ENABLED_MAINTENANCE_ONLY, ACTION_INITIALIZE_SCHEMA)


def validate_database_grantee(database: object, grantee: object) -> tuple[str, str]:
    from house_helper.persistence.pg_artifacts import validate_database_grantee as validate

    return validate(database, grantee)


# --- inventory and role verification ---------------------------------------------------------


def project_role_failures(roles: Mapping[str, RoleFacts]) -> Tuple[str, ...]:
    """The exact frozen role matrix, session defaults and absence of foreign `hh_` roles."""
    failures: list[str] = []
    for name in PROJECT_ROLE_NAMES:
        facts = roles.get(name)
        if facts is None:
            failures.append(f"missing:{name}")
            continue
        if facts.attributes() != ROLE_MATRIX[name]:
            failures.append(f"attributes:{name}")
        if facts.connlimit != -1:
            failures.append(f"connlimit:{name}")
        if facts.validuntil is not None:
            failures.append(f"validuntil:{name}")
        if tuple(sorted(facts.settings)) != tuple(sorted(ROLE_SETTINGS[name])):
            failures.append(f"settings:{name}")
    for name in roles:
        if name.startswith("hh_") and name not in PROJECT_ROLE_NAMES:
            failures.append(f"unexpected:{name}")
    return tuple(failures)


def existing_role_failures(roles: Mapping[str, RoleFacts]) -> Tuple[str, ...]:
    """Verify only the project roles that EXIST, plus the absence of foreign `hh_` names.

    This is the ENR-10 partial-state check: a subset is permitted while the enrollment is
    resuming, but each existing role must already match its intended attributes/settings,
    and nothing outside the seven may exist. `project_role_failures` is the complete
    seven-role check used once creation is expected to be finished."""
    failures: list[str] = []
    for name, facts in roles.items():
        if name not in PROJECT_ROLE_NAMES:
            if name.startswith("hh_"):
                failures.append(f"unexpected:{name}")
            continue
        if facts.attributes() != ROLE_MATRIX[name]:
            failures.append(f"attributes:{name}")
        if facts.connlimit != -1:
            failures.append(f"connlimit:{name}")
        if facts.validuntil is not None:
            failures.append(f"validuntil:{name}")
        if tuple(sorted(facts.settings)) != tuple(sorted(ROLE_SETTINGS[name])):
            failures.append(f"settings:{name}")
    return tuple(failures)


def observed_predefined_roles(roles: Iterable[str]) -> Tuple[str, ...]:
    """The predefined roles of the frozen PG16 baseline that are actually present.

    A `pg_*` name outside the baseline is *not* silently accepted as predefined; it is
    reported by `foreign_role_failures` and by `predefined_role_failures` as an
    unexpected/missing baseline member.
    """
    present = set(roles)
    return tuple(name for name in FROZEN_PREDEFINED_ROLES if name in present)


def predefined_role_failures(predefined_roles: Iterable[str]) -> Tuple[str, ...]:
    """ENR-07: the observed predefined set must EQUAL the frozen baseline."""
    actual = set(predefined_roles)
    expected = set(FROZEN_PREDEFINED_ROLES)
    failures = [f"missing_predefined:{name}" for name in sorted(expected - actual)]
    failures.extend(f"unexpected_predefined:{name}" for name in sorted(actual - expected))
    return tuple(failures)


def foreign_role_failures(
    roles: Iterable[str],
    predefined_roles: Iterable[str],
    *,
    project_roles: Iterable[str] = PROJECT_ROLE_NAMES,
) -> Tuple[str, ...]:
    """Every observed role must be the bootstrap superuser, one of the seven project
    roles, or a member of the observed/frozen predefined set. `unrelated_admin` and a
    planted `pg_evil` are both refused."""
    allowed = {BOOTSTRAP_ROLE, *predefined_roles, *project_roles}
    return tuple(f"unexpected_role:{name}" for name in sorted(set(roles) - allowed))


def membership_failures(
    edges: Iterable[MembershipEdge], predefined_roles: Iterable[str] = ()
) -> Tuple[str, ...]:
    """Project-role delegation validation.

    Predefined-to-predefined edges are PostgreSQL's own structure (`pg_monitor` and the
    three read/stat roles on PG16) and are never a project-role grant; they are compared
    separately by `predefined_membership_failures`. Every remaining edge must be exactly
    one of the two frozen `WITH SET TRUE, INHERIT FALSE` edges.
    """
    predefined = set(predefined_roles)
    actual = {
        (edge.member, edge.granted): (edge.admin, edge.inherit, edge.set_option)
        for edge in edges
        if not (edge.member in predefined and edge.granted in predefined)
    }
    failures: list[str] = []
    for (member, granted), options in EXPECTED_EDGES.items():
        if (member, granted) not in actual:
            failures.append(f"missing_edge:{member}->{granted}")
        elif actual[(member, granted)] != options:
            failures.append(f"edge_options:{member}->{granted}")
    for member, granted in actual:
        if (member, granted) not in EXPECTED_EDGES:
            failures.append(f"unexpected_edge:{member}->{granted}")
    return tuple(failures)


def existing_membership_failures(
    edges: Iterable[MembershipEdge], predefined_roles: Iterable[str] = ()
) -> Tuple[str, ...]:
    """A partial enrollment may not have created the frozen edges yet; an edge that DOES
    exist must be one of them and must have the frozen options. A missing edge is created
    later, never inferred. Predefined-to-predefined edges are excluded."""
    predefined = set(predefined_roles)
    failures: list[str] = []
    for edge in edges:
        if edge.member in predefined and edge.granted in predefined:
            continue
        options = EXPECTED_EDGES.get((edge.member, edge.granted))
        if options is None:
            failures.append(f"unexpected_edge:{edge.member}->{edge.granted}")
        elif (edge.admin, edge.inherit, edge.set_option) != options:
            failures.append(f"edge_options:{edge.member}->{edge.granted}")
    return tuple(failures)


def predefined_edges(
    edges: Iterable[MembershipEdge], predefined_roles: Iterable[str]
) -> Tuple[MembershipEdge, ...]:
    predefined = set(predefined_roles)
    return tuple(
        edge
        for edge in edges
        if edge.member in predefined and edge.granted in predefined
    )


def predefined_membership_failures(
    edges: Iterable[MembershipEdge], predefined_roles: Iterable[str]
) -> Tuple[str, ...]:
    """The frozen predefined baseline, compared separately from project delegation."""
    predefined = set(predefined_roles)
    actual = {
        (edge.member, edge.granted): (edge.admin, edge.inherit, edge.set_option)
        for edge in edges
        if edge.member in predefined and edge.granted in predefined
    }
    expected = {
        (member, granted): (admin, inherit, set_option)
        for member, granted, admin, inherit, set_option in FROZEN_PREDEFINED_MEMBERSHIPS
    }
    failures: list[str] = []
    for (member, granted), options in expected.items():
        if (member, granted) not in actual:
            failures.append(f"missing_predefined_edge:{member}->{granted}")
        elif actual[(member, granted)] != options:
            failures.append(f"predefined_edge_options:{member}->{granted}")
    for member, granted in actual:
        if (member, granted) not in expected:
            failures.append(f"unexpected_predefined_edge:{member}->{granted}")
    return tuple(failures)


def predefined_membership_edges(
    edges: Iterable[MembershipEdge], predefined_roles: Iterable[str]
) -> Tuple[MembershipEdge, ...]:
    """The baseline edges as journal payload rows, in the frozen order."""
    observed = {
        (edge.member, edge.granted): edge
        for edge in predefined_edges(edges, predefined_roles)
    }
    ordered = []
    for member, granted, _admin, _inherit, _set in FROZEN_PREDEFINED_MEMBERSHIPS:
        edge = observed.get((member, granted))
        if edge is not None:
            ordered.append(edge)
    return tuple(ordered)


def inventory_failures(
    roles: Mapping[str, RoleFacts],
    databases: Iterable[str],
    predefined_roles: Iterable[str],
    *,
    expected_predefined: Iterable[str],
) -> Tuple[str, ...]:
    """The ENR-15 inventory comparison: approved base plus nothing else.

    The approved database set at WP-2C-A is `postgres`, `template0`, `template1` and
    `house_helper`. Temporary databases are authorized by durable operation records, which
    WP-2C-A does not implement; an extra database therefore fails closed here.
    """
    failures = list(project_role_failures(roles))
    bootstrap = roles.get("postgres")
    if bootstrap is None or not bootstrap.superuser:
        failures.append("bootstrap_superuser")
    actual_databases = set(databases)
    expected_databases = set(BASE_DATABASES) | {SERVING_DATABASE}
    for name in sorted(expected_databases - actual_databases):
        failures.append(f"missing_database:{name}")
    for name in sorted(actual_databases - expected_databases):
        failures.append(f"unexpected_database:{name}")
    actual_predefined = set(predefined_roles)
    expected_set = set(expected_predefined)
    for name in sorted(expected_set - actual_predefined):
        failures.append(f"missing_predefined:{name}")
    for name in sorted(actual_predefined - expected_set):
        failures.append(f"unexpected_predefined:{name}")
    return tuple(failures)


def initializer_inventory_failures(
    roles: Mapping[str, RoleFacts],
    databases: Iterable[str],
    predefined_roles: Iterable[str],
    memberships: Iterable[MembershipEdge] = (),
) -> Tuple[str, ...]:
    """E0's pristine check: only the bootstrap databases and no project role or database.

    ENR-07 requires the predefined-role set to be the frozen baseline. A foreign role
    (including a planted `pg_*` name or `unrelated_admin`) is refused here, before an
    intent can be recorded.
    """
    failures: list[str] = []
    for name in BASE_DATABASES:
        if name not in set(databases):
            failures.append(f"missing_database:{name}")
    extras = set(databases) - set(BASE_DATABASES)
    for name in sorted(extras):
        failures.append(f"unexpected_database:{name}")
    for name in roles:
        if name.startswith("hh_"):
            failures.append(f"unexpected_role:{name}")
    bootstrap = roles.get(BOOTSTRAP_ROLE)
    if bootstrap is None or not bootstrap.superuser:
        failures.append("bootstrap_superuser")
    if predefined_roles is None:  # defensive; callers always pass an observation
        failures.append("predefined_roles:not_observed")
    else:
        failures.extend(predefined_role_failures(predefined_roles))
        failures.extend(
            foreign_role_failures(roles, predefined_roles, project_roles=())
        )
        failures.extend(predefined_membership_failures(memberships, predefined_roles))
    return tuple(failures)


# --- the ROL-06 migration session -------------------------------------------------------------


SESSION_SQL = (
    "SELECT session_user, current_user, current_database(), "
    "pg_catalog.current_setting('search_path'), "
    "pg_catalog.current_setting('application_name'), "
    "pg_catalog.current_setting('statement_timeout')"
)


def verify_migration_session(row: Any, *, database: str) -> None:
    """BOOT-09/ROL-06: `hh_migrate` acting as `hh_owner`, in the right database, with the
    exact session settings. A session whose values are wrong is refused, never trusted."""
    if row is None:
        raise ContractError("migration_session", "the migration session could not be read back")
    session, current, current_database, search_path, application_name, statement_timeout = row
    if (session, current) != ("hh_migrate", OWNER_ROLE):
        raise ContractError(
            "migration_session",
            "the migration connection is not hh_migrate acting as hh_owner",
            f"{session}/{current}",
        )
    if current_database != database:
        raise ContractError(
            "migration_session", "the migration connection is not to the serving database"
        )
    if search_path != "public":
        raise ContractError("migration_session", "search_path is not exactly public")
    if application_name != "hh_migrate":
        raise ContractError(
            "migration_session", "application_name is not the migration phase identity"
        )
    if statement_timeout != "0":
        raise ContractError("migration_session", "statement_timeout is not the ROL-06 value")


def establish_migration_session(connection: Any, *, database: str) -> None:
    """Set and read back ROL-06 on the exact connection Alembic will use.

    `hh_migrate`'s `ALTER ROLE ... SET role = hh_owner` applies at session establishment,
    so `current_user` is already `hh_owner`. `SET ROLE` alone would NOT load the owner's
    settings (ROL-05); the three `SET`s below are explicit and are verified immediately.
    """
    connection.exec_driver_sql("SET search_path = public")
    connection.exec_driver_sql("SET application_name = 'hh_migrate'")
    connection.exec_driver_sql("SET statement_timeout = 0")
    row = connection.exec_driver_sql(SESSION_SQL).fetchone()
    verify_migration_session(row, database=database)


# --- catalog observation ---------------------------------------------------------------------


# Every branch is a read of a pg_catalog relation. The UNION is one snapshot.
INVENTORY_SQL = " UNION ALL ".join(
    (
        "SELECT 'type', t.typnamespace, t.typname, t.oid, t.typtype, t.typrelid, t.typelem "
        "FROM pg_catalog.pg_type t",
        "SELECT 'schema', NULL, n.nspname, n.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_namespace n",
        "SELECT 'relation', c.relnamespace, c.relname, c.oid, c.relkind, NULL, NULL "
        "FROM pg_catalog.pg_class c",
        "SELECT 'routine', p.pronamespace, p.proname, p.oid, p.prokind, NULL, NULL "
        "FROM pg_catalog.pg_proc p",
        "SELECT 'collation', c.collnamespace, c.collname, c.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_collation c",
        "SELECT 'conversion', c.connamespace, c.conname, c.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_conversion c",
        "SELECT 'operator', o.oprnamespace, o.oprname, o.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_operator o",
        "SELECT 'operator class', c.opcnamespace, c.opcname, c.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_opclass c",
        "SELECT 'operator family', f.opfnamespace, f.opfname, f.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_opfamily f",
        "SELECT 'access method', NULL, a.amname, a.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_am a",
        "SELECT 'cast', NULL, NULL, c.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_cast c",
        "SELECT 'text search configuration', c.cfgnamespace, c.cfgname, c.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_ts_config c",
        "SELECT 'text search dictionary', d.dictnamespace, d.dictname, d.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_ts_dict d",
        "SELECT 'text search parser', p.prsnamespace, p.prsname, p.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_ts_parser p",
        "SELECT 'text search template', t.tmplnamespace, t.tmplname, t.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_ts_template t",
        "SELECT 'extension', NULL, e.extname, e.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_extension e",
        "SELECT 'publication', NULL, p.pubname, p.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_publication p",
        "SELECT 'event trigger', NULL, e.evtname, e.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_event_trigger e",
        "SELECT 'foreign data wrapper', NULL, f.fdwname, f.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_foreign_data_wrapper f",
        "SELECT 'foreign server', NULL, s.srvname, s.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_foreign_server s",
        "SELECT 'large object', NULL, NULL, m.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_largeobject_metadata m",
        "SELECT 'language', NULL, l.lanname, l.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_language l",
        "SELECT 'default acl', NULL, NULL, d.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_default_acl d",
        "SELECT 'transform', NULL, NULL, t.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_transform t",
    )
)


def inventory_from_rows(rows: Iterable[Any]) -> TargetInventory:
    """The baseline difference of `tests/postgres_e4.py`, applied to raw catalog rows."""
    rows = list(rows)
    namespaces = {
        int(oid): str(name)
        for class_, _schema, name, oid, _kind, _typrelid, _typelem in rows
        if str(class_) == "schema"
    }
    objects = []
    for class_, namespace_oid, name, oid, _kind, typrelid, typelem in rows:
        number = int(oid)
        class_ = str(class_)
        if class_ not in _EMPTY_BASELINE_CLASSES and number < FIRST_NORMAL_OBJECT_ID:
            continue
        if class_ == "schema":
            schema_name = str(name)
            if schema_name.startswith(_SESSION_SCHEMA_PREFIXES):
                continue
            objects.append(("schema", schema_name, schema_name))
            continue
        if class_ == "type" and (int(typrelid or 0) or int(typelem or 0)):
            continue
        # `psql -A -t` renders SQL NULL as an empty field (and a test fixture may pass
        # None). A `pg_default_acl` row legitimately has both a NULL namespace and a NULL
        # name; treating "" as a namespace oid would raise here (F8 exercises this path).
        if namespace_oid in (None, "", "None"):
            schema = ""
        else:
            schema = namespaces.get(int(namespace_oid), f"oid:{int(namespace_oid)}")
        object_name = (
            "oid:" + str(number) if name in (None, "", "None") else str(name)
        )
        objects.append((class_, schema, object_name))
    return TargetInventory(objects=tuple(sorted(objects)))


# --- the initialized-schema inventory (ENR-10 / BOOT-09 / F6) -------------------------------


def known_revision_chart() -> Tuple[str, Mapping[str, Optional[str]]]:
    """The single Alembic head of THIS image and the revision -> down_revision chart."""
    from alembic.script import ScriptDirectory

    from house_helper.persistence.database import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    heads = script.get_heads()
    if len(heads) != 1:
        raise ContractError("revision_chart", "the image does not have exactly one Alembic head")
    chart: dict[str, Optional[str]] = {}
    for revision in script.walk_revisions():
        down = revision.down_revision
        if isinstance(down, (tuple, list)):
            if len(down) > 1:  # a merge revision is not part of this chain
                raise ContractError("revision_chart", "a merge revision exists in the chain")
            down = down[0] if down else None
        chart[revision.revision] = down
    return heads[0], chart


ALEMBIC_VERSION_TABLE = "alembic_version"


def alembic_version_relation_names(
    version_table: str = ALEMBIC_VERSION_TABLE,
) -> frozenset[str]:
    """The relations Alembic itself creates for its version table (F6).

    This is derived from the pinned Alembic implementation, not from a hand-written
    allowlist: Alembic builds the version table with a named primary-key constraint
    (`alembic_version_pkc`), and PostgreSQL materializes that constraint as a relation
    with the same name. A future Alembic that names the constraint differently is
    reflected here without editing this module, and a name this module invented would
    never be accepted.
    """
    import sqlalchemy as sa
    from alembic.migration import MigrationContext

    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"version_table": version_table}
    )
    table = context.impl.version_table_impl(
        version_table=version_table,
        version_table_schema=None,
        version_table_pk=True,
    )
    names = {str(table.name)}
    for constraint in table.constraints:
        if isinstance(constraint, (sa.PrimaryKeyConstraint, sa.UniqueConstraint)):
            if constraint.name:
                names.add(str(constraint.name))
    for index in table.indexes:
        if index.name:
            names.add(str(index.name))
    return frozenset(names)


def expected_tables() -> frozenset[str]:
    """Every table this image's models define, plus `alembic_version`."""
    from house_helper.persistence import models  # noqa: F401  (registers every model)
    from house_helper.persistence.base import Base

    return frozenset(Base.metadata.tables) | {ALEMBIC_VERSION_TABLE}


def expected_relation_names(*, include_alembic_version: bool = True) -> frozenset[str]:
    """The public-schema relation names a fully initialized database carries.

    Tables, explicit and naming-convention indexes, primary/unique-constraint backing
    indexes and the one application sequence (`messages_id_seq`). The Alembic version
    table and its backing index are included only when the caller has observed that the
    version table exists (F6): pre-initialization and schema-only partial states may not
    carry them. A relation outside this set (and outside the toast namespace) is foreign,
    never adopted.
    """
    import sqlalchemy as sa

    from house_helper.persistence import models  # noqa: F401  (registers every model)
    from house_helper.persistence.base import Base

    names: set[str] = set(Base.metadata.tables)
    if include_alembic_version:
        names |= set(alembic_version_relation_names())
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            if index.name:
                names.add(index.name)
        for constraint in table.constraints:
            # Only primary-key and unique constraints materialize a relation (a backing
            # index). Check/foreign-key constraint names must never become an authorized
            # table name.
            if isinstance(constraint, (sa.PrimaryKeyConstraint, sa.UniqueConstraint)):
                if constraint.name:
                    names.add(constraint.name)
        for column in table.primary_key.columns:
            if (
                len(table.primary_key.columns) == 1
                and isinstance(column.type, sa.Integer)
                and column.autoincrement is True
            ):
                names.add(f"{table.name}_{column.name}_seq")
    return frozenset(names)


def revision_ancestry(current: str, chart: Mapping[str, Optional[str]], head: str) -> bool:
    seen: set[str] = set()
    revision: Optional[str] = head
    while revision is not None:
        if revision == current:
            return True
        if revision in seen or revision not in chart:
            return False
        seen.add(revision)
        revision = chart[revision]
    return False


# Schema states used by Mode B and the executor's fresh/upgrade dispatch.
SCHEMA_INITIAL = "initial"
SCHEMA_PARTIAL = "partial_initialization"
SCHEMA_HEAD = "completed"
SCHEMA_UPGRADE = "upgrade_required"


def classify_schema_state(
    *,
    versions: Iterable[str],
    public_tables: Iterable[str],
    schema_contract_present: bool = False,
    head: Optional[str] = None,
    chart: Optional[Mapping[str, Optional[str]]] = None,
) -> Tuple[str, Tuple[str, ...]]:
    """The state of the target schema, from observations only.

    `initial` - no application table and no revision row (fresh enrollment).
    `partial_initialization` - the BOOT-04 schema/default-ACL contract committed but the
        Alembic chain did not (no revision row, no application table).
    `completed` - exactly the image's single head and every expected table.
    `upgrade_required` - a known ancestor of the head with a subset of the expected
        tables. The chain may add the missing tables.
    """
    if head is None or chart is None:
        head, chart = known_revision_chart()
    expected = expected_tables()
    versions = tuple(versions)
    tables = set(public_tables)
    app_tables = tables - {"alembic_version"}
    failures: list[str] = []

    if not versions:
        if "alembic_version" in tables:
            failures.append("schema:version_table_without_row")
        extra = sorted(app_tables)
        if extra:
            failures.append("foreign_tables:" + ",".join(extra)[:200])
        if failures:
            return SCHEMA_INITIAL, tuple(failures)
        return (SCHEMA_PARTIAL if schema_contract_present else SCHEMA_INITIAL), ()

    if len(versions) != 1:
        return SCHEMA_INITIAL, ("revision_rows",)
    current = versions[0]
    if current not in chart:
        return SCHEMA_INITIAL, ("revision_unknown",)
    if not revision_ancestry(current, chart, head):
        return SCHEMA_INITIAL, ("revision_not_an_ancestor_of_head",)
    extra = sorted(tables - expected)
    if extra:
        return SCHEMA_INITIAL, ("foreign_tables:" + ",".join(extra)[:200],)
    if current == head:
        missing = sorted(expected - tables)
        if missing:
            return SCHEMA_INITIAL, ("missing_tables:" + ",".join(missing)[:200],)
        return SCHEMA_HEAD, ()
    # An upgrade may add tables; a missing table at an older revision is expected.
    return SCHEMA_UPGRADE, ()


def schema_state_failures(
    *, versions: Iterable[str], public_tables: Iterable[str]
) -> Tuple[str, ...]:
    _state, failures = classify_schema_state(
        versions=versions, public_tables=public_tables
    )
    return failures


def default_acl_entries(rows: Iterable[Any]) -> Tuple[Tuple[str, str, str, str, str, bool], ...]:
    """Normalize `DEFAULT_ACLS_SQL` rows: (owner, schema, objtype, grantee, privilege, grantable)."""
    entries = []
    for row in rows:
        if len(row) != 6:
            raise ContractError("default_acl_observation", "a default-ACL row is not normalized")
        owner, namespace, objtype, grantee, privilege, grantable = row
        entries.append(
            (
                str(owner),
                str(namespace) if namespace not in (None, "") else "<global>",
                str(objtype),
                str(grantee) if grantee not in (None, "") else "PUBLIC",
                str(privilege),
                _boolean(grantable),
            )
        )
    return tuple(sorted(entries))


def default_acl_failures(
    entries: Iterable[Tuple[str, str, str, str, str, bool]],
) -> Tuple[str, ...]:
    """The exact ROL-14 `hh_owner` default-ACL matrix, and no other owner's defaults."""
    failures: list[str] = []
    observed = tuple(entries)
    foreign_owners = sorted({entry[0] for entry in observed if entry[0] != OWNER_ROLE})
    for owner in foreign_owners:
        failures.append(f"default_acl_owner:{owner}")
    ours = {
        (schema, objtype, grantee, privilege, grantable)
        for owner, schema, objtype, grantee, privilege, grantable in observed
        if owner == OWNER_ROLE
    }
    expected = set(EXPECTED_DEFAULT_ACLS)
    for entry in sorted(expected - ours):
        failures.append("missing_default_acl:" + "|".join(str(part) for part in entry))
    for entry in sorted(ours - expected):
        failures.append("unexpected_default_acl:" + "|".join(str(part) for part in entry))
    return tuple(failures)


def authorized_inventory_failures(
    inventory: Optional[TargetInventory],
    *,
    default_acls_ok: bool,
    alembic_initialized: bool,
) -> Tuple[str, ...]:
    """F6: accept exactly the objects an initialized House Helper schema may carry.

    Foreign objects are never admitted. Derived toast storage (`pg_toast_<oid>` and its
    index) is PostgreSQL's own representation of an authorized table, so it is recognized
    structurally; everything else must be a named object of this image's model set.

    `alembic_initialized` says whether the `alembic_version` table was actually observed.
    Only then are that table, its backing index (`alembic_version_pkc`, derived from
    Alembic's own metadata), derived toast storage and the model relations authorized: a
    schema-only partial state that already carried them would not be partial at all, and
    pre-initialization must contain none of them. `default_acls_ok` separately authorizes
    `pg_default_acl` objects, because the ENR-04 partial state legitimately has those and
    no relation.
    """
    if inventory is None:
        return ("target:not_observed",)
    if not inventory:
        return ()
    expected = expected_relation_names(include_alembic_version=alembic_initialized)
    failures: list[str] = []
    for class_, schema, name in inventory.entries:
        if class_ == "default acl":
            if not default_acls_ok:
                failures.append(f"unexpected_object:{class_}:{schema}:{name}")
            continue
        if class_ == "relation" and schema == "pg_toast" and _TOAST_NAME.fullmatch(name):
            if alembic_initialized:
                continue
            failures.append(f"unexpected_object:{class_}:{schema}:{name}")
            continue
        if (
            alembic_initialized
            and class_ == "relation"
            and schema == "public"
            and name in expected
        ):
            continue
        failures.append(f"unexpected_object:{class_}:{schema}:{name}")
    return tuple(failures)


# SQL used by the host adapter (psql) and the executor. The executor can use SQLAlchemy
# `.execute()` on the same text; the host adapter parses psql's aligned-free output.
DATABASES_SQL = (
    "SELECT d.datname, d.datallowconn, pg_catalog.pg_get_userbyid(d.datdba), "
    "pg_catalog.pg_encoding_to_char(d.encoding), d.datcollate, d.datctype, "
    "d.datlocprovider, d.datacl IS NOT NULL, d.oid, "
    "coalesce(pg_catalog.shobj_description(d.oid, 'pg_database'), '') "
    "FROM pg_catalog.pg_database d ORDER BY d.datname"
)
DEFAULT_ACLS_SQL = (
    "SELECT o.rolname, coalesce(n.nspname, ''), d.defaclobjtype, "
    "coalesce(g.rolname, 'PUBLIC'), a.privilege_type, a.is_grantable "
    "FROM pg_catalog.pg_default_acl d "
    "JOIN pg_catalog.pg_roles o ON o.oid = d.defaclrole "
    "LEFT JOIN pg_catalog.pg_namespace n ON n.oid = d.defaclnamespace "
    "CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) a "
    "LEFT JOIN pg_catalog.pg_roles g ON g.oid = a.grantee "
    "WHERE a.grantee <> d.defaclrole "
    "ORDER BY 1, 2, 3, 4, 5, 6"
)
PUBLIC_TABLES_SQL = (
    "SELECT c.relname FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname"
)
VERSIONS_SQL = "SELECT version_num FROM public.alembic_version"
VERSIONS_TABLE_SQL = (
    "SELECT 1 FROM pg_catalog.pg_class c "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relname = 'alembic_version' AND c.relkind = 'r'"
)
DATABASE_ACLS_SQL = (
    "SELECT d.datname, a.grantee, coalesce(r.rolname, 'PUBLIC'), "
    "a.privilege_type, a.is_grantable "
    "FROM pg_catalog.pg_database d CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) a "
    "LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee"
)


def public_schema_acl_sql() -> str:
    """The `public` schema ownership, PUBLIC entries and effective privilege matrix.

    Read-only, and executable by any role. F8 requires the enrollment to verify the
    schema half of ENR-04, not only `pg_default_acl`: `db/schema.sql` is run by a separate
    process, so the host re-observes the committed catalog. The seven roles are an
    allowlist built from the frozen constant, never interpolated input.
    """
    columns = [
        "coalesce(n.nspowner::regrole::text, '')",
        "(SELECT count(*) FROM pg_catalog.aclexplode(n.nspacl) a WHERE a.grantee = 0)",
    ]
    columns += [
        f"pg_catalog.has_schema_privilege('{role}', 'public', 'USAGE')"
        for role in PROJECT_ROLE_NAMES
    ]
    columns += [
        f"pg_catalog.has_schema_privilege('{role}', 'public', 'CREATE')"
        for role in PROJECT_ROLE_NAMES
    ]
    return (
        "SELECT "
        + ", ".join(columns)
        + " FROM pg_catalog.pg_namespace n WHERE n.nspname = 'public'"
    )


PUBLIC_SCHEMA_ACL_SQL = public_schema_acl_sql()


def public_schema_acl_failures(row: Any) -> Tuple[str, ...]:
    """The ENR-04 schema contract: `hh_owner` owns `public`, no PUBLIC entry, and the
    exact USAGE/CREATE matrix (owner both; every LOGIN USAGE and no CREATE)."""
    if row is None:
        return ("schema:not_observed",)
    values = list(row)
    if len(values) != 2 + 2 * len(PROJECT_ROLE_NAMES):
        return ("schema:unrecognized_observation",)
    owner, public_entries = values[0], values[1]
    usage = values[2 : 2 + len(PROJECT_ROLE_NAMES)]
    create = values[2 + len(PROJECT_ROLE_NAMES) :]
    failures: list[str] = []
    if str(owner) != OWNER_ROLE:
        failures.append("schema:ownership")
    if int(public_entries) != 0:
        failures.append("schema:public_entries")
    for index, role in enumerate(PROJECT_ROLE_NAMES):
        if _boolean(usage[index]) is not True:
            failures.append(f"schema_usage:{role}")
        expected_create = role == OWNER_ROLE
        if _boolean(create[index]) is not expected_create:
            failures.append(f"schema_create:{role}")
    return tuple(failures)


def has_database_privilege_sql(database: str) -> str:
    """The effective CONNECT/TEMPORARY matrix for the seven project roles.

    The database name is allowlisted and then quoted as a SQL string literal; it is never
    interpolated from an unvalidated value."""
    from house_helper.persistence.pg_artifacts import validate_database_name

    name = validate_database_name(database)
    literal = "'" + name + "'"
    columns = [
        f"pg_catalog.has_database_privilege('{role}', {literal}, 'CONNECT')"
        for role in PROJECT_ROLE_NAMES
    ] + [
        f"pg_catalog.has_database_privilege('{role}', {literal}, 'TEMPORARY')"
        for role in PROJECT_ROLE_NAMES
    ]
    return "SELECT " + ", ".join(columns)


# The three database-level privileges PostgreSQL defines. A handoff is authorized to change
# exactly one of them (`CONNECT`) for exactly one role; every other value is collateral.
DATABASE_PRIVILEGES = ("CONNECT", "CREATE", "TEMPORARY")


def database_acl_matrix_sql(database: str) -> str:
    """The materialized/none state plus the complete effective privilege matrix.

    The handoff reconciler compares the *whole* state before and after, never a selected
    field. For each of the three database privileges and each project role the effective
    value is read from the catalog, so an inherited, PUBLIC or grant-option entry cannot
    hide behind a `has_database_privilege` answer for one role. The database name is
    allowlisted and quoted as a literal; nothing is interpolated from operator input.
    """
    from house_helper.persistence.pg_artifacts import validate_database_name

    name = validate_database_name(database)
    literal = "'" + name + "'"
    columns = ["d.datacl IS NOT NULL"]
    for privilege in DATABASE_PRIVILEGES:
        columns.extend(
            f"pg_catalog.has_database_privilege('{role}', {literal}, '{privilege}')"
            for role in PROJECT_ROLE_NAMES
        )
    return (
        "SELECT "
        + ", ".join(columns)
        + " FROM pg_catalog.pg_database d WHERE d.datname = "
        + literal
    )


_HANDOFF_APPLICATION_NAME = re.compile(r"hh-handoff-[0-9a-f]{32}")


def handoff_session_sql(application_name: str) -> str:
    """Count the live backends carrying one handoff operation's `application_name`.

    LCK-12 names `pg_stat_activity` by `application_name` as reconciler evidence. The value
    is checked against the exact generated grammar before it becomes a SQL literal, so a
    corrupted journal cannot inject SQL through it.
    """
    if not isinstance(application_name, str) or not _HANDOFF_APPLICATION_NAME.fullmatch(
        application_name
    ):
        raise ContractError(
            "handoff_session", "the handoff application name is malformed"
        )
    return (
        "SELECT count(*) FROM pg_catalog.pg_stat_activity "
        "WHERE application_name = '" + application_name + "'"
    )


ROLES_SQL = (
    "SELECT r.rolname, r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, "
    "r.rolinherit, r.rolreplication, r.rolbypassrls, r.rolconnlimit, "
    "coalesce(r.rolvaliduntil::text, '') "
    "FROM pg_catalog.pg_roles r ORDER BY r.rolname"
)
ROLE_SETTINGS_SQL = (
    "SELECT r.rolname, s.setting FROM pg_catalog.pg_roles r "
    "CROSS JOIN LATERAL pg_catalog.unnest(coalesce(r.rolconfig, "
    "array[]::text[])) AS s(setting)"
)
MEMBERSHIPS_SQL = (
    "SELECT m.rolname, g.rolname, a.admin_option, a.inherit_option, a.set_option "
    "FROM pg_catalog.pg_auth_members a "
    "JOIN pg_catalog.pg_roles m ON m.oid = a.member "
    "JOIN pg_catalog.pg_roles g ON g.oid = a.roleid "
    "ORDER BY m.rolname, g.rolname"
)
# MEM-01/MEM-09: the effective inquiry, measured separately from the explicit edge.
# It is executed in the serving database so `pg_database_owner` is evaluated there.
EFFECTIVE_MEMBERSHIP_SQL = (
    "SELECT m.rolname, g.rolname, p.inquiry, "
    "pg_catalog.pg_has_role(m.rolname, g.rolname, p.inquiry) "
    "FROM pg_catalog.pg_roles m CROSS JOIN pg_catalog.pg_roles g "
    "CROSS JOIN (VALUES ('MEMBER'), ('USAGE'), ('SET')) AS p(inquiry) "
    "WHERE m.rolname IN ('hh_owner', 'hh_migrate', 'hh_app', 'hh_purge', "
    "'hh_backup', 'hh_provision', 'hh_restore') "
    "AND (g.rolname IN ('hh_owner', 'hh_migrate', 'hh_app', 'hh_purge', "
    "'hh_backup', 'hh_provision', 'hh_restore') OR g.rolname LIKE 'pg\\_%') "
    "ORDER BY m.rolname, g.rolname, p.inquiry"
)
SESSIONS_SQL = (
    "SELECT a.datname, a.pid, coalesce(a.usename, ''), a.backend_type "
    "FROM pg_catalog.pg_stat_activity a"
)
SUBSCRIPTIONS_SQL = "SELECT s.oid, s.subname, s.subdbid FROM pg_catalog.pg_subscription s"
CURRENT_DATABASE_SQL = "SELECT pg_catalog.current_database(), pg_catalog.pg_backend_pid()"
SYSTEM_IDENTIFIER_SQL = "SELECT system_identifier::text FROM pg_catalog.pg_control_system()"
def marker_sql(database: str) -> str:
    from house_helper.persistence.pg_artifacts import validate_database_name

    name = validate_database_name(database)
    return (
        "SELECT coalesce(pg_catalog.shobj_description(d.oid, 'pg_database'), '') "
        "FROM pg_catalog.pg_database d WHERE d.datname = '" + name + "'"
    )


MARKER_SQL = marker_sql(SERVING_DATABASE)


def role_facts(rows: Iterable[Any], settings_rows: Iterable[Any]) -> Mapping[str, RoleFacts]:
    settings: dict[str, list[str]] = {}
    for name, setting in settings_rows:
        settings.setdefault(str(name), []).append(str(setting))
    result = {}
    for row in rows:
        (
            name,
            login,
            superuser,
            createdb,
            createrole,
            inherit,
            replication,
            bypassrls,
            connlimit,
            validuntil,
        ) = row
        name = str(name)
        result[name] = RoleFacts(
            name=name,
            login=_boolean(login),
            superuser=_boolean(superuser),
            createdb=_boolean(createdb),
            createrole=_boolean(createrole),
            inherit=_boolean(inherit),
            replication=_boolean(replication),
            bypassrls=_boolean(bypassrls),
            connlimit=int(connlimit),
            validuntil=str(validuntil) or None,
            settings=tuple(sorted(settings.get(name, []))),
        )
    return result


def membership_edges(rows: Iterable[Any]) -> Tuple[MembershipEdge, ...]:
    return tuple(
        MembershipEdge(
            member=str(member),
            granted=str(granted),
            admin=_boolean(admin),
            inherit=_boolean(inherit),
            set_option=_boolean(set_option),
        )
        for member, granted, admin, inherit, set_option in rows
    )


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (True, "t", "true", "True", 1):
        return True
    if value in (False, "f", "false", "False", 0):
        return False
    raise ContractError("boolean_observation", "a catalog boolean was not t/f or a bool")


def database_facts(
    *,
    row: Any,
    acl_rows: Iterable[Any],
    connect: Mapping[str, bool],
    temporary: Mapping[str, bool],
    sessions: Iterable[Any],
    subscriptions: Iterable[Any],
    target_inventory: Optional[TargetInventory],
    database: str,
    probe_pid: Optional[int] = None,
    default_acls: Iterable[Any] = (),
) -> DatabaseFacts:
    """Assemble one observation. `row` is one `DATABASES_SQL` row or None (absent)."""
    if row is None:
        return DatabaseFacts(exists=False)
    (
        datname,
        allowconn,
        owner,
        encoding,
        collate,
        ctype,
        provider,
        acl_materialized,
        oid,
        _marker,
    ) = row
    if str(datname) != database:
        raise ContractError("database_observation", "the observed database is not the target")
    entries = tuple(
        (
            "PUBLIC" if int(grantee) == 0 else str(grantee_role),
            str(privilege),
            _boolean(grantable),
        )
        for grantee, grantee_role, privilege, grantable in _expand_acl(acl_rows, database=database)
    )
    return DatabaseFacts(
        exists=True,
        owner=str(owner),
        encoding=str(encoding),
        collate=str(collate),
        ctype=str(ctype),
        provider=str(provider),
        allowconn=_boolean(allowconn),
        acl_materialized=_boolean(acl_materialized),
        acl_entries=entries,
        connect=dict(connect),
        temporary=dict(temporary),
        sessions=tuple(
            (int(pid), str(usename))
            for datname_, pid, usename, backend_type in sessions
            if str(datname_) == database and str(backend_type) == "client backend"
        ),
        probe_pid=probe_pid,
        target_inventory=target_inventory,
        default_acls=default_acl_entries(default_acls),
        subscriptions=tuple(
            sorted(
                (int(oid_), str(name))
                for oid_, name, subdbid in subscriptions
                if int(subdbid) == int(oid)
            )
        ),
    )


def _expand_acl(rows: Iterable[Any], *, database: str) -> Iterable[Any]:
    """Normalize the host adapter's ACL rows: (database, grantee, grantee_name, priv, grantable).

    The SQL implementation joins `pg_roles` to name the grantee; the host adapter does the
    same. A caller that supplies only four columns is refused rather than guessed."""
    normalized = []
    for row in rows:
        if len(row) != 5:
            raise ContractError("database_observation", "an ACL row is not normalized")
        datname, grantee, grantee_name, privilege, grantable = row
        if str(datname) == database:
            normalized.append((grantee, grantee_name, privilege, grantable))
    return normalized


def database_acl_failures(*, database: str, facts: DatabaseFacts, grantee: str) -> Tuple[str, ...]:
    """The database-level ACL half of BOOT-05/BOOT-07, on an initialized database."""
    if not facts.acl_materialized:
        return (FAIL_ACL_NOT_HARDENED,)
    if _public_entries(facts):
        return (FAIL_PUBLIC_ACCESS,)
    if _foreign_entries(facts, grantee):
        return (FAIL_UNEXPECTED_GRANTS,)
    if set(_grantee_entries(facts, grantee)) != {("CONNECT", False)}:
        return (FAIL_ACL_UNRECOGNIZED,)
    if not _connect_is_exact(facts, grantee):
        return (FAIL_ACL_NOT_HARDENED,)
    if facts.temporary.get(grantee) is not False:
        return (FAIL_ACL_UNRECOGNIZED,)
    return ()


def migration_fence_failures(facts: DatabaseFacts, grantee: str = "hh_migrate") -> Tuple[str, ...]:
    """BOOT-05 after schema work: the owner and `hh_migrate` may connect; the four runtime
    roles may not; `PUBLIC` holds nothing."""
    failures = list(database_acl_failures(database=SERVING_DATABASE, facts=facts, grantee=grantee))
    if facts.connect.get(OWNER_ROLE) is not True:
        failures.append("owner_connect_missing")
    if facts.connect.get(grantee) is not True:
        failures.append("migrate_connect_missing")
    for role in RUNTIME_ROLES:
        if facts.connect.get(role) is not False:
            failures.append(f"runtime_connect:{role}")
    return tuple(failures)


# --- markers ---------------------------------------------------------------------------------


def marker_matches(text: object, *, marker: str, system_identifier: str) -> None:
    from house_helper.persistence.pg_journal import JournalError, parse_marker

    try:
        token, recorded = parse_marker(text)
    except JournalError as error:
        raise ContractError(error.code, error.message, error.detail) from None
    if token != marker:
        raise ContractError("marker_mismatch", "the database marker is not the enrolled marker")
    if recorded != system_identifier:
        raise ContractError(
            "marker_identifier", "the marker's system identifier is not the live one"
        )


def expected_predefined_names(roles: Iterable[str]) -> Tuple[str, ...]:
    """The frozen predefined-role set is observed, never hand-assumed. This helper only
    selects the PostgreSQL predefined names a pristine cluster reports."""
    return tuple(sorted(name for name in roles if re.match(r"pg_[a-z_]+$", name)))
