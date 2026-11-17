"""The in-container `migrate` executor (HH-DB-01 Stage 2 WP-2C-A).

Frozen source: §4.5 (Mode B preflight, ENR-15/ENR-16), §5.1/§5.3 (BOOT-01..BOOT-10),
§5.2 (BOOT-07/BOOT-08), §7.1 (LCK-01..LCK-05) and §3.3 (ROL-05/ROL-06).

What this module refuses to be
------------------------------
There is no path from a bare environment flag to `alembic upgrade head`.

1. `pg_oneshot.py` refuses to read an authorization unless the host tool explicitly opened
   the stdin channel for this run. `docker compose run migrate` alone does not set it.
2. The authorization record is structurally validated and time-bounded.
3. Its artifact manifest must equal the SHA-256 of the `db/*.sql` files in THIS image.
4. The live database must carry the enrolled marker, the marker's embedded
   `system_identifier` must equal the authorization's cluster identifier, and the live
   project-role/database/predefined-role inventories must equal the record.
5. The database must already be enrolled (marker present) and in a state that authorizes
   either fresh initialization (nothing but PostgreSQL's baseline) or an upgrade (one
   known revision that is an ancestor of this image's head).
6. The migrate one-shot takes the state-volume exclusive lock before it connects and holds
   it for the whole operation.

Container-level identity (container name/id, image digest, PGDATA volume,
`system_identifier`) is verified authoritatively on the host by Mode B. The executor
re-verifies the database-observable half and binds the run to this image's SQL artifacts;
it does not pretend it can inspect Docker from inside the application container.

Fresh versus upgrade
--------------------
They are separate functions on purpose. `initialize_fresh` applies the BOOT-04 schema
contract, the complete Alembic chain in ONE transaction, verifies revision/ownership and
the untouched `messages` sequence, then re-applies `db/privileges.sql`. `upgrade_existing`
verifies the enrolled, initialized database and its revision history first, then runs the
chain and re-applies `db/privileges.sql`. The privilege re-application is unconditional,
including when the database is already at the head.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from house_helper.persistence import pg_artifacts as artifacts
from house_helper.persistence import pg_contract as contract
from house_helper.persistence import pg_journal as journal
from house_helper.persistence.database import alembic_config, create_db_engine

MIGRATE_HEAD_ROLE = "hh_migrate"


class ExecutorError(Exception):
    """A fail-closed executor refusal. `code` is stable and carries no secret."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


FRESH = "fresh_initialization"
UPGRADE = "upgrade"
NOOP = "already_at_head"
REFUSE = "refuse"

STEP_AUTHORIZATION = "authorization"
STEP_LOCK = "maintenance_lock"
STEP_CONNECT = "connect"
STEP_SESSION = "migration_session"
STEP_IDENTITY = "cluster_identity"
STEP_INVENTORY = "inventory"
STEP_SCHEMA_STATE = "schema_state"
STEP_SCHEMA = "schema_contract"
STEP_MIGRATE = "alembic_upgrade_head"
STEP_VERIFY = "verify_initialized"
STEP_PRIVILEGES = "privileges"
STEP_FENCE = "verify_fence"


@dataclass(frozen=True)
class MigrationPlan:
    kind: str
    revision: Optional[str]
    head: str
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionReport:
    plan: str
    revision: str
    head: str
    actions: tuple[str, ...]
    locks: str = "exclusive"


# --- the artifact binding --------------------------------------------------------------------


def authorization_artifact_failures(value: Mapping[str, Any]) -> tuple[str, ...]:
    """The authorization must name the exact SQL artifacts this image carries."""
    recorded = value.get("artifacts")
    if not isinstance(recorded, Mapping):
        return ("artifacts:missing",)
    actual = artifacts.manifest()
    failures = []
    for name in artifacts.ARTIFACT_FILES:
        if recorded.get(name) != actual[name]:
            failures.append(f"artifact:{name}")
    for name in recorded:
        if name not in actual:
            failures.append(f"artifact:unexpected:{name}")
    return tuple(failures)


# --- observation from one hh_migrate connection ----------------------------------------------


@dataclass(frozen=True)
class LiveFacts:
    marker: str
    databases: tuple[str, ...]
    roles: Mapping[str, contract.RoleFacts]
    memberships: tuple[contract.MembershipEdge, ...]
    predefined_roles: tuple[str, ...]
    database: contract.DatabaseFacts
    inventory: contract.TargetInventory
    versions: tuple[str, ...]
    public_tables: tuple[str, ...]
    sequence_is_called: Optional[bool]
    other_sessions: int
    default_acls: tuple[tuple[str, str, str, str, str, bool], ...] = ()
    # The `public` schema ownership/PUBLIC/USAGE+CREATE matrix (F8). None means the
    # observation was not made and is never treated as verified.
    schema_acl: Optional[tuple[Any, ...]] = None


def _rows(connection: Any, sql: str) -> list[Any]:
    return [tuple(row) for row in connection.exec_driver_sql(sql).fetchall()]


def observe_live(connection: Any) -> LiveFacts:
    """Read everything the executor verifies, on the one migration connection.

    The connection must already have been established and verified (ROL-06) by the
    caller. Every query is a read; none changes the session or a catalog object.
    """
    current_database, own_pid = connection.exec_driver_sql(contract.CURRENT_DATABASE_SQL).fetchone()
    database = str(current_database)
    if database != contract.SERVING_DATABASE:
        raise ExecutorError("database", "the migration connection is not to the serving database")

    marker_rows = _rows(connection, contract.marker_sql(database))
    marker = "" if not marker_rows else str(marker_rows[0][0] or "")
    databases = tuple(str(row[0]) for row in _rows(
        connection,
        "SELECT d.datname FROM pg_catalog.pg_database d ORDER BY d.datname",
    ))
    role_rows = _rows(connection, contract.ROLES_SQL)
    setting_rows = _rows(connection, contract.ROLE_SETTINGS_SQL)
    roles = contract.role_facts(role_rows, setting_rows)
    memberships = contract.membership_edges(_rows(connection, contract.MEMBERSHIPS_SQL))
    predefined = contract.observed_predefined_roles(roles)

    db_rows = _rows(connection, contract.DATABASES_SQL)
    db_row = next((row for row in db_rows if str(row[0]) == database), None)
    acl_rows = _rows(connection, contract.DATABASE_ACLS_SQL)
    privilege_rows = connection.exec_driver_sql(
        contract.has_database_privilege_sql(database)
    ).fetchone()
    connect = {
        role: bool(privilege_rows[index])
        for index, role in enumerate(contract.PROJECT_ROLE_NAMES)
    }
    temporary = {
        role: bool(privilege_rows[len(contract.PROJECT_ROLE_NAMES) + index])
        for index, role in enumerate(contract.PROJECT_ROLE_NAMES)
    }
    sessions = tuple(
        (int(pid), str(usename))
        for datname, pid, usename, backend_type in _rows(connection, contract.SESSIONS_SQL)
        if str(datname) == database
        and str(backend_type) == "client backend"
        and int(pid) != int(own_pid)
    )
    # `pg_subscription` has no PUBLIC SELECT (it holds connection strings). The
    # subscription check is made by the host's superuser Mode B observation, which the
    # authorization binds; the hh_migrate executor cannot read that catalog.
    inventory = contract.inventory_from_rows(_rows(connection, contract.INVENTORY_SQL))
    default_acls = contract.default_acl_entries(
        _rows(connection, contract.DEFAULT_ACLS_SQL)
    )
    # F8: `public` ownership and the USAGE/CREATE matrix are readable by the migration
    # login. The enrollment step verifies the committed schema half from the catalog.
    schema_acl_row = connection.exec_driver_sql(
        contract.PUBLIC_SCHEMA_ACL_SQL
    ).fetchone()
    schema_acl = tuple(schema_acl_row) if schema_acl_row is not None else None
    facts = contract.database_facts(
        row=db_row,
        acl_rows=acl_rows,
        connect=connect,
        temporary=temporary,
        sessions=sessions,
        subscriptions=(),
        target_inventory=inventory,
        database=database,
        probe_pid=int(own_pid),
        default_acls=default_acls,
    )

    versions: tuple[str, ...] = ()
    public_tables: tuple[str, ...] = ()
    sequence_is_called: Optional[bool] = None
    table_exists = connection.exec_driver_sql(
        "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
        "AND c.relname = 'alembic_version' AND c.relkind = 'r'"
    ).fetchone()
    if table_exists:
        versions = tuple(
            str(row[0]) for row in connection.exec_driver_sql(
                "SELECT version_num FROM public.alembic_version"
            ).fetchall()
        )
    public_tables = tuple(
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT c.relname FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname"
        ).fetchall()
    )
    sequence = connection.exec_driver_sql(
        "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
        "AND c.relname = 'messages_id_seq' AND c.relkind = 'S'"
    ).fetchone()
    if sequence:
        row = connection.exec_driver_sql(
            "SELECT is_called FROM public.messages_id_seq"
        ).fetchone()
        sequence_is_called = bool(row[0]) if row else None

    return LiveFacts(
        marker=marker,
        databases=databases,
        roles=roles,
        memberships=memberships,
        predefined_roles=predefined,
        database=facts,
        inventory=inventory,
        versions=versions,
        public_tables=public_tables,
        sequence_is_called=sequence_is_called,
        other_sessions=len(sessions),
        default_acls=default_acls,
        schema_acl=schema_acl,
    )


# --- schema/revision state -------------------------------------------------------------------


def _known_revisions() -> tuple[str, Mapping[str, Optional[str]], str]:
    """The revision chart of THIS image, and its single head."""
    try:
        head, chart = contract.known_revision_chart()
    except contract.ContractError as error:
        raise ExecutorError(error.code, error.message, error.detail) from None
    return head, chart, head


def _expected_tables() -> frozenset[str]:
    return contract.expected_tables()


def _revision_ancestry(current: str, chart: Mapping[str, Optional[str]], head: str) -> bool:
    return contract.revision_ancestry(current, chart, head)


def plan_migration(live: LiveFacts) -> MigrationPlan:
    """Dispatch on the OBSERVED schema state. The only mutable paths are fresh, upgrade
    and the idempotent head retry; everything else fails closed.

    F6: the target inventory is authorized by state. Before initialization it must be
    empty (or exactly the BOOT-04 schema/default-ACL partial state); after initialization
    it may contain this image's tables, indexes, sequences, derived toast storage and the
    exact ROL-14 default ACLs, and nothing else.
    """
    head, chart, _single_head = _known_revisions()
    state, failures = contract.classify_schema_state(
        versions=live.versions,
        public_tables=live.public_tables,
        schema_contract_present=bool(live.default_acls),
        head=head,
        chart=chart,
    )
    if failures:
        return MigrationPlan(REFUSE, live.versions[0] if live.versions else None, head, failures)

    acl_failures = contract.default_acl_failures(live.default_acls)
    inventory_failures = contract.authorized_inventory_failures(
        live.inventory,
        default_acls_ok=not acl_failures,
        # F6: the Alembic version table and its `alembic_version_pkc` backing index are
        # authorized only when a revision row was actually observed.
        alembic_initialized=bool(live.versions),
    )
    if inventory_failures:
        return MigrationPlan(
            REFUSE, live.versions[0] if live.versions else None, head, inventory_failures
        )

    if state == contract.SCHEMA_HEAD:
        return MigrationPlan(NOOP, live.versions[0], head)
    if state == contract.SCHEMA_UPGRADE:
        return MigrationPlan(UPGRADE, live.versions[0], head)
    # No revision row and no application table: fresh initialization (a schema-only
    # partial state is re-entered through db/schema.sql, which is idempotent).
    return MigrationPlan(FRESH, None, head)


# --- the artifact application ----------------------------------------------------------------


def _apply_script(connection: Any, sql: str, *, label: str) -> None:
    """Execute one frozen artifact inside the caller's transaction.

    `db/schema.sql` and `db/privileges.sql` install their own transaction-local ROL-06
    contract and transaction guard; the caller supplies the transaction and nothing else.

    `no_parameters` is mandatory: the artifacts contain literal `%` characters in
    `format()` calls and `RAISE` messages. Without this execution option SQLAlchemy hands
    psycopg an empty parameter mapping and psycopg treats `%` as a placeholder delimiter.
    """
    try:
        connection.exec_driver_sql(sql, execution_options={"no_parameters": True})
    except Exception as error:  # psycopg/SQLAlchemy; never echo a value or SQL text
        code = getattr(error, "sqlstate", None) or getattr(
            getattr(error, "orig", None), "sqlstate", None
        )
        raise ExecutorError(
            "artifact_refused", f"{label} refused the current state", code or type(error).__name__
        ) from None


def _verify_initialized(connection: Any, *, head: str, fresh: bool) -> None:
    from alembic import command

    contract.establish_migration_session(connection, database=contract.SERVING_DATABASE)
    versions = tuple(
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT version_num FROM public.alembic_version"
        ).fetchall()
    )
    if versions != (head,):
        raise ExecutorError("revision", "alembic_version is not the single head revision")
    config = alembic_config()
    config.attributes["connection"] = connection
    try:
        command.check(config)
    except Exception:
        raise ExecutorError("schema_drift", "alembic check reports drift") from None
    ownership = connection.exec_driver_sql(
        "SELECT c.relname, c.relkind, c.relowner::regrole::text "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
        "AND c.relkind IN ('r', 'p', 'S', 'i', 'I') ORDER BY c.relname"
    ).fetchall()
    if not any(row[1] == "r" for row in ownership):
        raise ExecutorError("ownership", "the migrated database holds no application table")
    strangers = [str(row[0]) for row in ownership if str(row[2]) != contract.OWNER_ROLE]
    if strangers:
        raise ExecutorError(
            "ownership",
            "a public-schema table, index or sequence is not owned by hh_owner",
            ", ".join(strangers[:6]),
        )
    if fresh:
        row = connection.exec_driver_sql(
            "SELECT last_value, is_called FROM public.messages_id_seq"
        ).fetchone()
        if row is None or row[1] is not False:
            raise ExecutorError(
                "sequence", "the messages sequence was already called on a fresh database"
            )


def _fence_failures(live: LiveFacts, *, fresh: bool) -> tuple[str, ...]:
    failures = []
    facts = live.database
    if not facts.acl_materialized:
        failures.append(contract.FAIL_ACL_NOT_HARDENED)
        return tuple(failures)
    if facts.acl_entries and any(entry[0] == "PUBLIC" for entry in facts.acl_entries):
        failures.append(contract.FAIL_PUBLIC_ACCESS)
    if any(
        entry[0] not in (contract.OWNER_ROLE, MIGRATE_HEAD_ROLE, *contract.RUNTIME_ROLES)
        for entry in facts.acl_entries
    ):
        failures.append(contract.FAIL_UNEXPECTED_GRANTS)
    if facts.connect.get(contract.OWNER_ROLE) is not True:
        failures.append("owner_connect_missing")
    if facts.connect.get(MIGRATE_HEAD_ROLE) is not True:
        failures.append("migrate_connect_missing")
    if fresh:
        for role in contract.RUNTIME_ROLES:
            if facts.connect.get(role) is not False:
                failures.append(f"runtime_connect:{role}")
    if live.other_sessions:
        failures.append("unauthorized_sessions")
    return tuple(failures)


def _verify_live_identity(
    value: Mapping[str, Any], live: LiveFacts, *, fresh: bool
) -> None:
    failures = list(authorization_artifact_failures(value))
    if failures:
        raise ExecutorError(
            "artifact_identity",
            "the authorization does not name the SQL artifacts in this image",
            ", ".join(failures),
        )
    cluster = value["cluster"]
    try:
        token, identifier = journal.parse_marker(value["marker"])
        live_token, live_identifier = journal.parse_marker(live.marker)
    except journal.JournalError as error:
        raise ExecutorError(error.code, error.message) from None
    if (token, identifier) != (live_token, live_identifier):
        raise ExecutorError("marker", "the live database marker is not the authorization's")
    if identifier != cluster["system_identifier"] or live_identifier != cluster["system_identifier"]:
        raise ExecutorError(
            "system_identifier", "the marker's system identifier is not the authorized one"
        )
    if live.database.owner != contract.OWNER_ROLE:
        raise ExecutorError("ownership", "the database is not owned by hh_owner")
    if live.database.encoding != contract.EXPECTED_ENCODING:
        raise ExecutorError("encoding", "the database encoding is not UTF8")
    if (live.database.collate, live.database.ctype, live.database.provider) != contract.EXPECTED_LOCALE:
        raise ExecutorError("locale", "the database locale is not the frozen C/C locale")

    role_failures = list(contract.project_role_failures(live.roles))
    role_failures.extend(
        contract.foreign_role_failures(live.roles, live.predefined_roles)
    )
    role_failures.extend(contract.predefined_role_failures(live.predefined_roles))
    edge_failures = list(contract.membership_failures(live.memberships, live.predefined_roles))
    edge_failures.extend(
        contract.predefined_membership_failures(live.memberships, live.predefined_roles)
    )
    if role_failures or edge_failures:
        raise ExecutorError(
            "role_inventory",
            "the live project-role inventory is not the enrolled one",
            ", ".join(role_failures + edge_failures)[:400],
        )
    bootstrap = live.roles.get(contract.BOOTSTRAP_ROLE)
    if bootstrap is None or not bootstrap.superuser:
        raise ExecutorError("bootstrap_superuser", "the bootstrap superuser is not present")
    if sorted(live.predefined_roles) != sorted(value["predefined_roles"]):
        raise ExecutorError(
            "predefined_roles", "the predefined-role set is not the enrolled one"
        )
    if sorted(live.databases) != sorted(value["databases"]):
        raise ExecutorError("database_inventory", "the database inventory is not the enrolled one")
    fence = _fence_failures(live, fresh=fresh)
    if fence:
        raise ExecutorError(
            "database_fence",
            "the live database ACL is not the enrolled fence",
            ", ".join(fence),
        )


# --- the migration operations ----------------------------------------------------------------


def _upgrade(connection: Any, head: str) -> None:
    from alembic import command

    config = alembic_config()
    config.attributes["connection"] = connection
    try:
        command.upgrade(config, head)
    except Exception as error:
        raise ExecutorError(
            "migration_failed", "alembic upgrade head failed", type(error).__name__
        ) from None


def _apply_schema(connection: Any) -> None:
    _apply_script(
        connection,
        artifacts.render_schema(contract.SERVING_DATABASE),
        label="db/schema.sql",
    )


def _apply_privileges(connection: Any) -> None:
    try:
        connection.exec_driver_sql(
            artifacts.render_privileges(contract.SERVING_DATABASE),
            execution_options={"no_parameters": True},
        )
    except Exception as error:
        code = getattr(error, "sqlstate", None) or getattr(
            getattr(error, "orig", None), "sqlstate", None
        )
        raise ExecutorError(
            "privileges_refused",
            "db/privileges.sql refused the current state; the migration is not complete",
            code or type(error).__name__,
        ) from None


# --- the enrollment-time schema/default-ACL step (F8) ----------------------------------------


STEP_SCHEMA_CONTRACT = "enrollment_schema_contract"


def _schema_step_failures(live: LiveFacts, *, after: bool) -> tuple[str, ...]:
    """The ENR-04 schema/default-ACL half, observed before and after `db/schema.sql`.

    Before: the database is at E3-c (connections enabled, ACL hardened), the role
    inventory is the enrolled one, the fence still excludes the four runtime roles, and
    no application relation exists. A recognized schema-only partial state (a resumed
    step) may already carry the exact ROL-14 default ACLs; nothing else may.
    After: the ROL-14 default ACL matrix is exact, no application relation exists, and
    the fence is unchanged.
    """
    failures: list[str] = []
    if live.versions or live.public_tables:
        # The enrollment step never owns an initialized schema: `alembic upgrade head`
        # is the migrate executor's operation, reached only through Mode B.
        failures.append("schema:application_objects")
    if after:
        failures.extend(contract.default_acl_failures(live.default_acls))
    elif live.default_acls:
        failures.extend(contract.default_acl_failures(live.default_acls))
    role_failures = list(contract.project_role_failures(live.roles))
    role_failures.extend(
        contract.foreign_role_failures(live.roles, live.predefined_roles)
    )
    role_failures.extend(contract.predefined_role_failures(live.predefined_roles))
    if role_failures:
        failures.extend(role_failures)
    edge_failures = list(contract.membership_failures(live.memberships, live.predefined_roles))
    edge_failures.extend(
        contract.predefined_membership_failures(live.memberships, live.predefined_roles)
    )
    failures.extend(edge_failures)
    failures.extend(contract.migration_fence_failures(live.database))
    if live.other_sessions:
        failures.append("unauthorized_sessions")
    inventory_failures = contract.authorized_inventory_failures(
        live.inventory,
        default_acls_ok=after
        or (bool(live.default_acls) and not contract.default_acl_failures(live.default_acls)),
        alembic_initialized=False,
    )
    failures.extend(inventory_failures)
    return tuple(failures)


def _schema_authorization_failures(
    value: Mapping[str, Any], live: LiveFacts, *, after: bool
) -> tuple[str, ...]:
    """F8: the decoded schema authorization consumed against live, observable facts.

    The authorization is not a bearer token that is decoded and then forgotten. Before
    `db/schema.sql` runs, and again after it commits, the worker compares every fact a
    project login can actually observe with the authorized target:

    * the authorization names the serving database, the accepted artifact manifest, the
      frozen deployment container name and the frozen PGDATA volume;
    * the live cluster is at the applicable Mode A phase: the database is enabled (E3-c),
      carries no ENR-05 marker yet, no application relation and no Alembic revision, and
      its owner/encoding/locale and database inventory are the frozen target;
    * the role, membership and predefined-role inventory is the enrolled one;
    * the database ACL fence and the schema/default-ACL state are exact (an absent
      default-ACL matrix is the pre-step state; after the step it must be ROL-14).

    The physical `system_identifier` (and the Docker container id/image) cannot be read by
    an `hh_migrate` login, so it is not pretended here: the host binds those fields to the
    enrollment intent before it issues this record (see `ClusterAdapter.harden_schema`).
    """
    failures: list[str] = list(authorization_artifact_failures(value))
    cluster = value.get("cluster")
    if not isinstance(cluster, Mapping):
        failures.append("authorization:cluster")
    else:
        if cluster.get("container_name") != contract.DEPLOYMENT_CONTAINER_NAME:
            failures.append("authorization:container_name")
        if cluster.get("volume") != contract.DEPLOYMENT_VOLUME:
            failures.append("authorization:volume")
    if value.get("database") != contract.SERVING_DATABASE:
        failures.append("authorization:database")
    if value.get("role") != journal.SCHEMA_ROLE:
        failures.append("authorization:role")

    if live.marker:
        failures.append("marker:present")
    facts = live.database
    if facts is None or not facts.exists:
        failures.append("database:not_observed")
    else:
        if facts.owner != contract.OWNER_ROLE:
            failures.append("database:ownership")
        if facts.encoding != contract.EXPECTED_ENCODING:
            failures.append("database:encoding")
        if (facts.collate, facts.ctype, facts.provider) != contract.EXPECTED_LOCALE:
            failures.append("database:locale")
        if facts.allowconn is not True:
            failures.append("database:not_connectable")

    expected_databases = set(contract.BASE_DATABASES) | {contract.SERVING_DATABASE}
    observed_databases = set(live.databases)
    for name in sorted(observed_databases - expected_databases):
        failures.append(f"unexpected_database:{name}")
    for name in sorted(expected_databases - observed_databases):
        failures.append(f"missing_database:{name}")
    if live.versions or live.public_tables:
        failures.append("schema:application_objects")

    bootstrap = live.roles.get(contract.BOOTSTRAP_ROLE)
    if bootstrap is None or not bootstrap.superuser:
        failures.append("bootstrap_superuser")
    failures.extend(contract.project_role_failures(live.roles))
    failures.extend(contract.foreign_role_failures(live.roles, live.predefined_roles))
    failures.extend(contract.predefined_role_failures(live.predefined_roles))
    failures.extend(
        contract.membership_failures(live.memberships, live.predefined_roles)
    )
    failures.extend(
        contract.predefined_membership_failures(
            live.memberships, live.predefined_roles
        )
    )
    if facts is not None and facts.exists:
        failures.extend(contract.migration_fence_failures(facts))
    if live.other_sessions:
        failures.append("unauthorized_sessions")
    # Before the step an absent default-ACL matrix is the E3-c state; a matrix that is
    # present must already be exactly ROL-14 (a resumed step). After the step it must be
    # exact.
    acl_failures = (
        contract.default_acl_failures(live.default_acls)
        if (after or live.default_acls)
        else ()
    )
    failures.extend(acl_failures)
    if after:
        # The schema half is verified from the committed catalog, not trusted from the
        # one-shot: `public` must be owned by hh_owner with no PUBLIC entry and the exact
        # USAGE/CREATE matrix.
        failures.extend(contract.public_schema_acl_failures(live.schema_acl))
    failures.extend(
        contract.authorized_inventory_failures(
            live.inventory,
            default_acls_ok=not acl_failures,
            alembic_initialized=False,
        )
    )
    return tuple(dict.fromkeys(failures))


def apply_schema_contract(engine: Any, authorization: Mapping[str, Any]) -> None:
    """Execute the unchanged `db/schema.sql` as the authenticated migration login (F8).

    ENR-04 requires `harden public` and the ROL-14 default ACLs to be part of enrollment,
    before the ENR-05 marker. The artifact requires `session_user = hh_migrate` and
    `current_user = hh_owner` (ROL-03/ROL-06), so this step is reached only through the
    one-shot's `hh_migrate` connection: it is never simulated from the bootstrap
    superuser's local peer session, and it never runs Alembic or arbitrary SQL. The
    transaction guard, the ROL-06 session contract and the acceptance checks are the
    artifact's own; this function supplies one transaction and verifies the committed
    catalog afterwards.

    `authorization` is the decoded schema record. It is consumed here, not discarded:
    `_schema_authorization_failures` compares the authorized deployment/database target
    with the live catalog and the accepted artifact manifest both before and after the
    artifact runs. A direct one-shot invocation therefore cannot reach `db/schema.sql`
    from a foreign database, an already-enrolled cluster, a wrong locale/owner or a
    different SQL artifact set.
    """
    value = dict(authorization)
    try:
        with engine.begin() as connection:
            contract.establish_migration_session(
                connection, database=contract.SERVING_DATABASE
            )
            live = observe_live(connection)
            failures = _schema_authorization_failures(value, live, after=False)
            failures = failures + _schema_step_failures(live, after=False)
            if failures:
                raise ExecutorError(
                    "schema_state",
                    "the database is not at the enrollment state db/schema.sql requires",
                    ", ".join(dict.fromkeys(failures))[:400],
                )
            _apply_schema(connection)
    except ExecutorError:
        raise
    except Exception as error:
        raise ExecutorError(
            "schema_contract",
            "db/schema.sql could not be applied",
            type(error).__name__,
        ) from None
    with engine.connect() as connection:
        contract.establish_migration_session(
            connection, database=contract.SERVING_DATABASE
        )
        after = observe_live(connection)
    failures = _schema_authorization_failures(value, after, after=True)
    failures = failures + _schema_step_failures(after, after=True)
    if failures:
        raise ExecutorError(
            "schema_contract",
            "db/schema.sql did not leave the frozen schema/default-ACL contract",
            ", ".join(dict.fromkeys(failures))[:400],
        )


def initialize_fresh(engine: Any, *, head: str) -> None:
    """BOOT-09 steps 2-4 for a database whose only content is PostgreSQL's baseline."""
    with engine.begin() as connection:
        contract.establish_migration_session(connection, database=contract.SERVING_DATABASE)
        _apply_schema(connection)
        _upgrade(connection, head)
        _verify_initialized(connection, head=head, fresh=True)
        _apply_privileges(connection)


def upgrade_existing(engine: Any, *, head: str) -> None:
    """The verified upgrade path. `head` may already be the current revision; the object
    privileges are re-applied unconditionally."""
    with engine.begin() as connection:
        contract.establish_migration_session(connection, database=contract.SERVING_DATABASE)
        _upgrade(connection, head)
        _verify_initialized(connection, head=head, fresh=False)
        _apply_privileges(connection)


def _post_verify(engine: Any, *, fresh: bool) -> LiveFacts:
    with engine.connect() as connection:
        contract.establish_migration_session(connection, database=contract.SERVING_DATABASE)
        live = observe_live(connection)
    failures = list(_fence_failures(live, fresh=fresh))
    if failures:
        raise ExecutorError(
            "database_fence",
            "the database ACL/fence is not the enrolled one after the migration",
            ", ".join(failures),
        )
    acl_failures = contract.default_acl_failures(live.default_acls)
    if acl_failures:
        raise ExecutorError(
            "default_acls",
            "the ROL-14 default-ACL matrix is not the frozen one after the migration",
            ", ".join(acl_failures)[:400],
        )
    state, state_failures = contract.classify_schema_state(
        versions=live.versions, public_tables=live.public_tables
    )
    if state != contract.SCHEMA_HEAD:
        raise ExecutorError(
            "schema_state",
            "the database did not reach the image's complete schema state",
            ", ".join(state_failures) or state,
        )
    inventory_failures = contract.authorized_inventory_failures(
        live.inventory,
        default_acls_ok=True,
        alembic_initialized=True,
    )
    if inventory_failures:
        raise ExecutorError(
            "inventory",
            "the initialized database holds an object outside the authorized inventory",
            ", ".join(inventory_failures)[:400],
        )
    return live


# --- the one public operation ----------------------------------------------------------------


@dataclass
class Executor:
    """The migrate one-shot.

    `lock` is a context-manager factory for the state-volume exclusive lock and
    `engine_factory` creates the hh_migrate engine from the DSN. The remaining callables
    are the accepted operations; the injection points exist so the order and every
    refusal can be tested without a server, and every default is the production one.
    """

    lock: Callable[[], AbstractContextManager[Any]]
    engine_factory: Callable[[], Any] = field(default_factory=lambda: create_db_engine)
    observe: Callable[[Any], LiveFacts] = observe_live
    plan: Callable[[LiveFacts], MigrationPlan] = plan_migration
    # The defaults are keyword-only functions. The callables are invoked with the same
    # keyword names so a substituted mock cannot hide a positional-call defect (F2).
    fresh: Callable[..., None] = initialize_fresh
    upgrade: Callable[..., None] = upgrade_existing
    post_verify: Callable[..., LiveFacts] = _post_verify

    def run(self, authorization: bytes, *, now: Optional[datetime] = None) -> ExecutionReport:
        actions: list[str] = []
        value = journal.decode_authorization(authorization, now=now)
        actions.append(STEP_AUTHORIZATION)
        with self.lock():
            actions.append(STEP_LOCK)
            try:
                engine = self.engine_factory()
            except Exception as error:
                raise ExecutorError(
                    "database_unavailable",
                    "the migration engine could not be created",
                    type(error).__name__,
                ) from None
            try:
                try:
                    with engine.connect() as connection:
                        contract.establish_migration_session(
                            connection, database=contract.SERVING_DATABASE
                        )
                        actions.append(STEP_SESSION)
                        live = self.observe(connection)
                except ExecutorError:
                    raise
                except Exception as error:
                    raise ExecutorError(
                        "database_unavailable",
                        "the migration connection or observation could not be completed",
                        type(error).__name__,
                    ) from None
                actions.append(STEP_CONNECT)
                failures = authorization_artifact_failures(value)
                if failures:
                    raise ExecutorError(
                        "artifact_identity",
                        "the authorization does not name the SQL artifacts in this image",
                        ", ".join(failures),
                    )
                # The fresh/upgrade decision is made from the observed schema state; the
                # identity check needs to know whether the runtime CONNECT handoff may
                # already exist, so it is made first, then verified.
                plan = self.plan(live)
                if plan.kind == REFUSE:
                    raise ExecutorError(
                        "schema_state",
                        "the database is not enrolled at a known revision and is not empty",
                        ", ".join(plan.failures),
                    )
                _verify_live_identity(value, live, fresh=plan.kind == FRESH)
                actions.append(STEP_IDENTITY)
                actions.append(STEP_INVENTORY)
                actions.append(_state_step(plan.kind))
                if plan.kind == FRESH:
                    self.fresh(engine, head=plan.head)
                    actions.extend((STEP_SCHEMA, STEP_MIGRATE, STEP_VERIFY, STEP_PRIVILEGES))
                elif plan.kind in (UPGRADE, NOOP):
                    self.upgrade(engine, head=plan.head)
                    actions.extend((STEP_MIGRATE, STEP_VERIFY, STEP_PRIVILEGES))
                after = self.post_verify(engine, fresh=plan.kind == FRESH)
                if HEAD_REVISION_MISMATCH(after, plan.head):
                    raise ExecutorError(
                        "revision", "the database did not reach the image's head revision"
                    )
                actions.append(STEP_FENCE)
                return ExecutionReport(
                    plan=plan.kind,
                    revision=after.versions[0] if after.versions else plan.head,
                    head=plan.head,
                    actions=tuple(actions),
                )
            finally:
                engine.dispose()


def HEAD_REVISION_MISMATCH(live: LiveFacts, head: str) -> bool:
    return live.versions != (head,)


def _state_step(kind: str) -> str:
    return {
        FRESH: STEP_SCHEMA_STATE + ":fresh",
        UPGRADE: STEP_SCHEMA_STATE + ":upgrade",
        NOOP: STEP_SCHEMA_STATE + ":already_at_head",
    }.get(kind, STEP_SCHEMA_STATE)
