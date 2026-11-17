"""The ordered initializer (HH-DB-01 Stage 2, WP-1C-C).

Frozen source: §5.1 BOOT-01/BOOT-02/BOOT-03/BOOT-04/BOOT-06 (E3, the connection fence and
the schema contract), §5.2 BOOT-07/BOOT-08 (the E4 allowed-state matrix), §5.3 BOOT-09
(ordered initialization) and BOOT-10 (object ownership), §5.1 BOOT-05 (the fence stays
closed for the runtime roles), §5.2 BOOT-08's rule that a journal is evidence only of what
it says, §3.9 ROL-22/ROL-23 (database-level ACL work from the maintenance database only),
and §9.4 RBK-08(a) (the recovery for an empty database that cannot be resumed).

What this module is
-------------------
It connects the accepted components in the one order the specification allows and does
nothing else:

    1. observe the cluster with the accepted E4 classifier (tests/postgres_e4.py);
    2. converge/verify the seven project roles with `db/roles.sql` (tests/postgres_sql.py);
    3. drive the E3 fence with `db/bootstrap.sql`: create disabled -> `PUBLIC` revoked ->
       `CONNECT` only for the one maintenance login -> E3-b verified -> enable -> E3-c
       verified (BOOT-01); an interrupted creation is resumed from the observed E4 row
       (BOOT-07 rows 2 and 3), never "fixed forward";
    4. re-observe E3-c with the classifier;
    5. open the ONE authorized maintenance-login connection and classify the target half;
       `verify_target_objects` is a request to look, never an authorization;
    6. apply `db/schema.sql` (BOOT-04) on that connection - only under a verdict whose
       `may_initialize_schema` is true;
    7. run the complete `0001-0014` chain in ONE transaction as `hh_migrate` (which assumes
       `hh_owner`), establishing and verifying the ROL-06 session contract on that same
       connection, then verify the version row, `alembic check` (with the same contract on
       its connection), object ownership and the untouched `messages` sequence (BOOT-09
       steps 2-3, BOOT-10, BOOT-15);
    8. re-apply `db/privileges.sql` (BOOT-09 step 4);
    9. verify the fence is still closed for `hh_app`/`hh_purge`/`hh_backup`/`hh_provision`
       (BOOT-05).

BOOT-09 steps 5 and 6 need no code and get none: the historical data revisions
(`0002`, `0004`, `0010`) run against zero rows because the database is empty, and
`alembic stamp` has no code path here - this architecture forbids adding one.

Nothing here redefines the architecture. Every SQL statement comes from an accepted
artifact, every state decision from the accepted classifier, and every verification that
already exists is called rather than restated.

Failure and interruption
------------------------
Every refusal is fail-closed and precedes the mutation it guards:

* a state BOOT-07 row 5 names (`FAIL_CLOSED`, including an `unexpected_subscription`, an
  unauthorized session, a stale or untrusted observation, or an inconsistent privilege
  observation) is refused before any statement of consequence - the classifier's failure
  codes are reported, never converted into an authorization;
* a state that changed between the plan and the mutation is refused (`state_changed`),
  never adjusted;
* E3-a/E3-b are re-verified immediately before the enabling `ALTER DATABASE` (BOOT-01
  Steps 4-5): a failed check never reaches it;
* the E3-c observation is repeated after enablement instead of trusting the mutator;
* the chain runs in one transaction, so an interrupted chain leaves the database at zero
  application tables and `alembic_version` absent (BOOT-09 step 2). Because BOOT-04's
  schema hardening has already committed by then, the accepted classifier does NOT
  re-authorize that database: it holds objects that are not part of the pristine
  baseline, which BOOT-07 row 5 refuses. That is the specified behavior, not a gap - the
  database is empty, so the recovery is RBK-08(a): destroy and recreate the empty
  database and run the initializer again. The initializer never drops, renames or
  "repairs" anything itself.
* a database that exists while the project roles do not is not a recognized partial state
  (ENR-10) and is refused before classification can trip over an unknown role.

The caller owns cleanup. This module never drops a database, a role or an object.

Servings versus restores
------------------------
This is the BOOT-09 initializer for the serving database. A `house_helper_restore_<token>`
database follows RST-04 (a different ordered sequence: no schema init, `pg_restore`, then
`privileges.sql`) and is refused with a stable code rather than silently run through the
wrong lifecycle.

Transaction ownership and the migration session
-----------------------------------------------
The initializer issues no `BEGIN`, `COMMIT` or `ROLLBACK` of its own. The classifier
issues none either (FRESH-4); `SchemaContract.apply` and `PrivilegesScript.apply` each
supply their own transaction, and `_upgrade_to_head` runs the chain in one
`engine.begin()`. Every connection the initializer uses is autocommit (the harness's
`project_connection` and `control()` are), so no caller transaction is left open or
committed on someone else's behalf.

ROL-05/ROL-06 apply to the migration connections themselves. `SET ROLE` does not load
`hh_owner`'s `ALTER ROLE ... SET` defaults, BOOT-04's settings were transaction-local and
are gone, and a pooled connection carries no contract from a previous user. The initializer
therefore sets AND reads back `search_path = public`, `application_name = 'hh_migrate'` and
`statement_timeout = 0` on the very connection that runs the chain and on the one
`alembic check` compares against, before either operation, refusing when the read-back
session is not the required one.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, Optional

from alembic import command

from house_helper.persistence.database import alembic_config
from tests import postgres_e4 as e4
from tests import postgres_sql as artifacts
from tests.revisions import HEAD_REVISION

# --- stable refusal codes --------------------------------------------------------------------

REFUSE_E4_FAIL_CLOSED = "e4_fail_closed"
REFUSE_STATE_CHANGED = "state_changed"
REFUSE_NOT_AUTHORIZED = "schema_initialization_not_authorized"
REFUSE_ROLES_MISSING = "roles_missing_for_existing_database"
REFUSE_RESTORE_DATABASE = "restore_initialization_not_supported"
REFUSE_CREDENTIAL = "maintenance_credential"
REFUSE_REVISION = "unexpected_schema_revision"
REFUSE_OWNERSHIP = "schema_ownership_not_owner"
REFUSE_SEQUENCE = "message_sequence_already_called"
REFUSE_FENCE = "runtime_role_has_connect"
REFUSE_SESSION_CONTRACT = "migration_session_contract"

# --- ordered step labels (`InitializationReport.actions`) ------------------------------------
# The labels name what actually ran, in the order it ran. A refusal raised inside a step
# leaves the labels of the steps that completed before it.

STEP_OBSERVE = "observe"
STEP_ROLES = "roles"
STEP_CREATE = "create_database"
STEP_HARDEN = "harden_database_acl"
STEP_ENABLE = "enable_connections"
STEP_VERIFY_TARGET = "verify_target_objects"
STEP_SCHEMA = "schema_contract"
STEP_MIGRATE = "alembic_upgrade_head"
STEP_VERIFY_INITIALIZED = "verify_initialized"
STEP_PRIVILEGES = "privileges"
STEP_VERIFY_FENCE = "verify_fence"

# The runtime roles that must NOT be able to connect before their H1/H2/H3 handoff
# (BOOT-05). `hh_migrate` (the grantee) and the owner are the two that may.
RUNTIME_ROLES = ("hh_app", "hh_purge", "hh_backup", "hh_provision")


class InitializationError(Exception):
    """A fail-closed refusal of the ordered initialization.

    `code` is stable and asserted by tests; `message` is fixed help text; `detail` holds
    only non-secret names, state names and failure codes - never a credential."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class InitializationReport:
    """What the initializer observed, authorized and did.

    `entry_state` is the first observed E4 state, `maintenance_action` its action, and
    `authorization_action` the action of the target-half verdict that authorized the
    schema work. `actions` lists every completed step in order, so a test can assert the
    ordered transition rather than only its end state."""

    database: str
    grantee: str
    entry_state: str
    maintenance_action: str
    authorization_action: str
    actions: tuple[str, ...]
    created: bool
    roles_created: frozenset[str]
    roles_existing: frozenset[str]
    revision: str


# --- the ROL-05/ROL-06 migration session contract (BOOT-09 step 2) ----------------------------

# The phase this initializer's schema work runs as: `hh_migrate` logging in, acting as
# `hh_owner`. ROL-05: `SET ROLE` does NOT apply `hh_owner`'s `ALTER ROLE ... SET` defaults,
# so the session settings are a per-connection obligation, never an inherited fact.
MIGRATION_SESSION_USER = "hh_migrate"
MIGRATION_CURRENT_USER = "hh_owner"

_SESSION_SETTINGS_SQL = (
    "SELECT session_user, current_user, current_database(), "
    "current_setting('search_path'), current_setting('application_name'), "
    "current_setting('statement_timeout'), "
    "(SELECT nspname FROM pg_namespace WHERE oid = 'public'::regnamespace)"
)


def require_migration_session(connection: Any, *, database: str) -> None:
    """The read-back half of ROL-06's "set and then verify" rule; it refuses anything else.

    The `(session_user, current_user)` pair is the BOOT-09 phase matrix's
    (`hh_migrate`/`hh_owner`), `current_database()` is the database being initialized,
    `search_path` is exactly `public`, `application_name` names the phase login and
    `statement_timeout` is `0`. A session whose settings are wrong is refused here rather
    than trusted: unqualified migration DDL resolves through `search_path`, so a session
    that silently kept another one would write somewhere else."""
    name = artifacts.validate_database_name(database)
    row = connection.exec_driver_sql(_SESSION_SETTINGS_SQL).fetchone()
    if row is None:  # pragma: no cover - PostgreSQL always answers this SELECT
        raise InitializationError(
            REFUSE_SESSION_CONTRACT, "the migration session contract could not be read back"
        )
    (
        session,
        current,
        current_database,
        search_path,
        application_name,
        statement_timeout,
        public_schema,
    ) = row
    if (session, current) != (MIGRATION_SESSION_USER, MIGRATION_CURRENT_USER):
        raise InitializationError(
            REFUSE_SESSION_CONTRACT,
            "the migration connection is not hh_migrate acting as hh_owner",
            f"{session}/{current}",
        )
    if current_database != name:
        raise InitializationError(
            REFUSE_SESSION_CONTRACT,
            "the migration connection is not to the database being initialized",
            str(current_database),
        )
    if search_path != "public":
        raise InitializationError(
            REFUSE_SESSION_CONTRACT, "search_path is not exactly public", str(search_path)
        )
    if application_name != session:
        raise InitializationError(
            REFUSE_SESSION_CONTRACT,
            "application_name is not the phase identity",
            f"{application_name}/{session}",
        )
    if statement_timeout != "0":
        raise InitializationError(
            REFUSE_SESSION_CONTRACT,
            "statement_timeout is not the ROL-06 value",
            str(statement_timeout),
        )
    if public_schema != "public":
        raise InitializationError(
            REFUSE_SESSION_CONTRACT, "public is not the sole non-system schema", str(public_schema)
        )


def establish_migration_session(connection: Any, *, database: str) -> None:
    """Establish and verify ROL-06 on THIS connection, before it runs any schema work.

    Three `SET`s make the session contract explicit; `require_migration_session` then
    reads the effective values back. Calling this on the exact connection Alembic will use
    is what makes the guarantee real: no other connection's settings, and no earlier
    transaction's `SET LOCAL`, are assumed to carry over (ROL-05, §3.3)."""
    name = artifacts.validate_database_name(database)
    connection.exec_driver_sql("SET search_path = public")
    connection.exec_driver_sql(f"SET application_name = '{MIGRATION_SESSION_USER}'")
    connection.exec_driver_sql("SET statement_timeout = 0")
    require_migration_session(connection, database=name)


# --- the post-initialization verification (BOOT-09 step 3, BOOT-10, BOOT-15) -----------------


def verify_initialized_database(engine: Any, database: str) -> str:
    """The C3/BOOT-09 acceptance facts that are read from the migrated database.

    * `alembic_version` holds exactly the one head revision (BOOT-09 step 3);
    * `alembic check` reports no drift against the models (BOOT-09 step 3);
    * every table, index and sequence in `public` - `alembic_version` included - is owned
      by `hh_owner` (BOOT-10);
    * the `messages` sequence is untouched: `is_called = false`, so the first message
      receives `1` (BOOT-15).

    Index ownership is checked too: BOOT-10 names indexes explicitly, and PostgreSQL
    creates the primary-key and unique indexes of the chain on the same table. Returns
    the revision so the caller can report it.

    `alembic check` compares the live catalog through the connection it is handed, so the
    ROL-06 contract is established and verified on that same connection first - reading it
    through a session with another `search_path` would compare the wrong schema."""
    name = artifacts.validate_database_name(database)
    with engine.connect() as connection:
        establish_migration_session(connection, database=name)
        versions = tuple(
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT version_num FROM public.alembic_version"
            )
        )
        if versions != (HEAD_REVISION,):
            raise InitializationError(
                REFUSE_REVISION,
                "alembic_version is not the single head revision",
                ", ".join(str(version) for version in versions) or "<empty>",
            )
        config = alembic_config()
        config.attributes["connection"] = connection
        command.check(config)  # raises CommandError when autogenerate would produce anything
        ownership = connection.exec_driver_sql(
            "SELECT c.relname, c.relkind, c.relowner::regrole::text "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S', 'i', 'I') "
            "ORDER BY c.relname"
        ).fetchall()
        if not any(row[1] == "r" for row in ownership):
            raise InitializationError(
                REFUSE_OWNERSHIP, "the migrated database holds no application table", name
            )
        strangers = [row[0] for row in ownership if row[2] != "hh_owner"]
        if strangers:
            raise InitializationError(
                REFUSE_OWNERSHIP,
                "a public-schema table, index or sequence is not owned by hh_owner",
                ", ".join(str(relname) for relname in strangers),
            )
        last_value, is_called = connection.exec_driver_sql(
            "SELECT last_value, is_called FROM public.messages_id_seq"
        ).fetchone()
        if is_called is not False:
            raise InitializationError(
                REFUSE_SEQUENCE,
                "the messages sequence was already called on a fresh database",
                f"last_value={last_value}",
            )
    return HEAD_REVISION


def _upgrade_to_head(engine: Any, database: str) -> None:
    """BOOT-09 step 2: the complete chain in ONE transaction.

    `engine.begin()` supplies the one connection Alembic will run every revision on; the
    ROL-06 contract is established and verified on that very connection before the first
    revision, so the chain never runs on a session whose settings it merely inherited. The
    outer transaction is what makes an interrupted chain disappear entirely (PostgreSQL
    transactional DDL)."""
    name = artifacts.validate_database_name(database)
    with engine.begin() as connection:
        establish_migration_session(connection, database=name)
        config = alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, HEAD_REVISION)


# --- the initializer -------------------------------------------------------------------------


class Initializer:
    """The BOOT-09 ordered initialization behind the accepted E4 fence.

    The collaborators are the accepted components themselves; the injection points exist
    so the order and the refusals can be tested without a server, and every default is
    the real accepted implementation:

    * `control` - a callable returning a context manager for the autocommit bootstrap
      connection to the maintenance database (the harness's `Run.control`);
    * `connect_target` - `(database, grantee, password)` -> a context manager for the one
      authorized maintenance-login connection to the database (`project_connection`);
    * `migrate_engine` - `(database, grantee, password)` -> a guarded engine whose
      connections are the migration login with `current_user = hh_owner`
      (`migration_engine`);
    * `classify`, `roles`, `bootstrap`, `schema`, `privileges`, `upgrade`,
      `verify_initialized` - the accepted classifier and scripts by default; `upgrade`
      receives the engine and the database name so the default can bind the ROL-06
      contract to the connection Alembic actually uses.

    `claim`, when given, receives the role names this run may create, BEFORE their
    creation transaction starts - the same cleanup contract `RolesScript.apply` offers."""

    def __init__(
        self,
        *,
        control: Callable[[], AbstractContextManager[Any]],
        connect_target: Callable[[str, str, str], AbstractContextManager[Any]],
        migrate_engine: Callable[[str, str, str], Any],
        classify: Callable[..., e4.E4Verdict] = e4.classify,
        roles: Optional[artifacts.RolesScript] = None,
        bootstrap: Optional[artifacts.BootstrapScript] = None,
        schema: Optional[artifacts.SchemaContract] = None,
        privileges: Optional[artifacts.PrivilegesScript] = None,
        upgrade: Callable[[Any, str], None] = _upgrade_to_head,
        verify_initialized: Optional[Callable[[Any, str], str]] = None,
        claim: Optional[Callable[[frozenset[str]], None]] = None,
    ) -> None:
        self.control = control
        self.connect_target = connect_target
        self.migrate_engine = migrate_engine
        self.classify = classify
        self.roles = artifacts.RolesScript() if roles is None else roles
        self.bootstrap = artifacts.BootstrapScript() if bootstrap is None else bootstrap
        self.schema = artifacts.SchemaContract() if schema is None else schema
        self.privileges = artifacts.PrivilegesScript() if privileges is None else privileges
        self.upgrade = upgrade
        self.verify_initialized = (
            verify_initialized_database if verify_initialized is None else verify_initialized
        )
        self.claim = claim

    @classmethod
    def for_run(cls, run: Any, **options: Any) -> "Initializer":
        """The live wiring: the harness's verified maintenance connections and the
        accepted artifact runners, exactly as the WP-1B/WP-1C-A suites use them."""

        def connect_target(database: str, grantee: str, password: str) -> Any:
            return artifacts.project_connection(run, database, grantee, password)

        def migrate_engine(database: str, grantee: str, password: str) -> Any:
            return artifacts.migration_engine(run, database, grantee, password)

        return cls(
            control=run.control,
            connect_target=connect_target,
            migrate_engine=migrate_engine,
            **options,
        )

    # --- the one public operation ------------------------------------------------------------

    def initialize(self, database: object, grantee: object, password: object) -> InitializationReport:
        """Initialize the serving database, or refuse.

        The sequence is the module docstring's, and every mutation is preceded by the
        observation or verification that authorizes it. Nothing is mutated before the
        entry observation, and nothing is dropped under any outcome."""
        name, role = artifacts.validate_database_grantee(database, grantee)  # before any SQL
        if name != artifacts.SERVING_DATABASE:
            raise InitializationError(
                REFUSE_RESTORE_DATABASE,
                "the ordered initializer initializes the serving database only; a restore "
                "database follows RST-04 and belongs to the restore stage",
                name,
            )
        if not isinstance(password, str) or not password:
            raise InitializationError(
                REFUSE_CREDENTIAL,
                "a non-empty maintenance-login credential is required to open the target connection",
            )
        actions: list[str] = []

        # 0. An existing database without the project roles is not a recognized partial
        #    state (ENR-10); it is refused before classification asks the server about a
        #    role that does not exist.
        self._refuse_missing_roles_for_an_existing_database(name)

        # 1. The entry observation. Read-only; no mutation precedes it.
        entry = self._observe(name, role)
        self._refuse_fail_closed(entry)
        entry_state, maintenance_action = entry.state, entry.action
        actions.append(_step(STEP_OBSERVE, entry.state))

        # 2. The role inventory (BOOT-09 step 1). Converges only what `db/roles.sql`
        #    defines and refuses attribute/membership drift; roles that already existed
        #    are verified, not adopted from an unknown shape.
        with self.control() as control:
            applied = self.roles.apply(control, claim=self.claim)
        actions.append(STEP_ROLES)

        # 3. One more read-only observation immediately before the first mutation. A state
        #    that changed since step 1 is planned for again from the observed row; a state
        #    BOOT-07 refuses is refused here, before anything is created or hardened.
        entry = self._observe(name, role)
        self._refuse_fail_closed(entry)

        # 4. Drive the E3 fence to E3-c from whichever allowed row was observed.
        if entry.state == e4.E4_ABSENT:
            with self.control() as control:
                self.bootstrap.run(control, database=name, grantee=role)
            actions.extend((STEP_CREATE, STEP_HARDEN, STEP_ENABLE))
        elif entry.state == e4.E4_DISABLED_UNHARDENED:
            self._finish_hardening_and_enable(name, role, already_hardened=False)
            actions.extend((STEP_HARDEN, STEP_ENABLE))
        elif entry.state == e4.E4_DISABLED_HARDENED:
            self._finish_hardening_and_enable(name, role, already_hardened=True)
            actions.append(STEP_ENABLE)
        # ENABLED_MAINTENANCE_ONLY: the fence is already behind the database; the target
        # half below is still required.

        # 5. E3-c, observed again: the mutators' own checks are not the authorization.
        enabled = self._observe(name, role)
        self._refuse_fail_closed(enabled)
        if enabled.state != e4.E4_ENABLED_MAINTENANCE_ONLY:
            raise InitializationError(
                REFUSE_STATE_CHANGED,
                "the database did not reach the enabled, hardened, maintenance-only state",
                enabled.state,
            )
        actions.append(_step(STEP_OBSERVE, enabled.state))

        # 6. The target half on the one authorized maintenance-login connection. The
        #    verdict action `verify_target_objects` only asks for this observation; the
        #    authorization is `may_initialize_schema` on the resulting verdict.
        with self.connect_target(name, role, password) as target:
            verdict = self._observe(name, role, target=target)
            self._refuse_fail_closed(verdict)
            if not verdict.may_initialize_schema:
                raise InitializationError(
                    REFUSE_NOT_AUTHORIZED,
                    "the observed target state does not authorize schema initialization",
                    f"{verdict.state}/{verdict.action}",
                )
            actions.append(STEP_VERIFY_TARGET)
            # BOOT-04. The artifact establishes and verifies the schema contract inside
            # its own transaction, before anything else can use the database.
            self.schema.apply(target, database=name)
            actions.append(STEP_SCHEMA)

        # 7. BOOT-09 steps 2-3: the chain in one transaction, then its acceptance facts.
        engine = self.migrate_engine(name, role, password)
        try:
            self.upgrade(engine, name)
            actions.append(STEP_MIGRATE)
            revision = self.verify_initialized(engine, name)
            actions.append(STEP_VERIFY_INITIALIZED)
        finally:
            engine.dispose()

        # 8. BOOT-09 step 4: the object privileges, after the objects exist.
        with self.connect_target(name, role, password) as target:
            self.privileges.apply(target, database=name)
        actions.append(STEP_PRIVILEGES)

        # 9. BOOT-05: schema work did not open the database to the runtime roles.
        with self.control() as control:
            self._verify_fence(control, name, role)
        actions.append(STEP_VERIFY_FENCE)

        return InitializationReport(
            database=name,
            grantee=role,
            entry_state=entry_state,
            maintenance_action=maintenance_action,
            authorization_action=verdict.action,
            actions=tuple(actions),
            created=name in self.bootstrap.created,
            roles_created=frozenset(applied.created),
            roles_existing=frozenset(applied.existing),
            revision=revision,
        )

    # --- the pieces ---------------------------------------------------------------------------

    def _observe(self, database: str, grantee: str, *, target: Any = None) -> e4.E4Verdict:
        """One classification through a fresh verified maintenance connection. The
        classifier issues no transaction control and leaves the target session exactly as
        it found it (FRESH-4)."""
        with self.control() as control:
            return self.classify(control=control, database=database, grantee=grantee, target=target)

    def _refuse_fail_closed(self, verdict: e4.E4Verdict) -> None:
        """BOOT-07 row 5 is refused whatever it contains. An `unexpected_subscription`, a
        stale or untrusted observation and an inconsistent privilege observation are
        failure codes like any other: none of them is turned into an authorization."""
        if verdict.fail_closed:
            raise InitializationError(
                REFUSE_E4_FAIL_CLOSED,
                "the observed database state is not one BOOT-07 authorizes",
                ", ".join(verdict.failures) or verdict.state,
            )

    def _refuse_missing_roles_for_an_existing_database(self, database: str) -> None:
        """ENR-10: a recognized partial state is a subset of the authorized inventory. The
        seven roles are part of that inventory, so a database that exists while they do
        not is not a partial state - it is an unrecognized cluster."""
        with self.control() as control:
            exists = control.execute(
                "SELECT 1 FROM pg_catalog.pg_database WHERE datname = %s", (database,)
            ).fetchone() is not None
            if not exists:
                return
            missing = [
                role
                for role in artifacts.PROJECT_ROLE_NAMES
                if control.execute(
                    "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s", (role,)
                ).fetchone() is None
            ]
        if missing:
            raise InitializationError(
                REFUSE_ROLES_MISSING,
                "the database exists but the project roles do not; this is not a recognized "
                "partial state and is never adopted",
                ", ".join(missing),
            )

    def _finish_hardening_and_enable(self, database: str, grantee: str, *, already_hardened: bool) -> None:
        """BOOT-07 rows 2 and 3, from the maintenance database only (BOOT-06, ROL-22).

        The observed state is re-read first; then `db/bootstrap.sql`'s harden phase (row 2)
        and enable phase (both rows) are used - the same statements BOOT-01 defines. E3-b is
        verified immediately before the enabling `ALTER` (after the harden phase on row 2);
        a failure never reaches the enable, and E3-c is verified after it."""
        rendered = artifacts.render_bootstrap(database, grantee)
        expected = e4.E4_DISABLED_HARDENED if already_hardened else e4.E4_DISABLED_UNHARDENED
        with self.control() as control:
            fresh = self.classify(control=control, database=database, grantee=grantee)
            self._refuse_fail_closed(fresh)
            if fresh.state != expected:
                raise InitializationError(
                    REFUSE_STATE_CHANGED,
                    "the database state changed before its ACL could be finished",
                    f"{expected} -> {fresh.state}",
                )
            if not already_hardened:
                # REVOKE + GRANT are transactional; nothing is reachable while disabled.
                with control.transaction():
                    control.execute(rendered["harden"])
            # BOOT-01 Step 4: the E3-b gate runs BEFORE the one statement that opens the
            # database, on this very connection.
            self.bootstrap.verify_disabled(control, database, grantee)
            control.execute(rendered["enable"])
            self.bootstrap.verify_enabled(control, database, grantee)

    def _verify_fence(self, control: Any, database: str, grantee: str) -> None:
        """BOOT-05: after the schema work, `CONNECT` is exactly the owner's and the one
        maintenance login's, and `PUBLIC` holds nothing. The runtime roles receive theirs
        only at H1/H2/H3."""
        matrix = self.bootstrap.connect_matrix(control, database)
        for required in (e4.OWNER_ROLE, grantee):
            if matrix.get(required) is not True:
                raise InitializationError(
                    REFUSE_FENCE, "the database CONNECT matrix lost a required grant", required
                )
        for runtime in RUNTIME_ROLES:
            if matrix.get(runtime) is not False:
                raise InitializationError(
                    REFUSE_FENCE,
                    "a runtime role can connect before its handoff",
                    runtime,
                )
        if self.bootstrap.public_entries(control, database) != 0:
            raise InitializationError(
                REFUSE_FENCE, "PUBLIC still holds a database privilege after initialization", database
            )


def _step(label: str, state: str) -> str:
    return f"{label}:{state}"
