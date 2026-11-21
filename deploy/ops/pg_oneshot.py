"""The PostgreSQL operational one-shots (HH-DB-01 Stage 2 WP-2B / WP-2C-A).

    python /opt/house-helper/ops/pg_oneshot.py migrate|schema|provision|purge|backup|restore
    python /opt/house-helper/ops/pg_oneshot.py provision enroll-user --display-name <name>
    python /opt/house-helper/ops/pg_oneshot.py provision enrollment-status

WP-2B defined the five per-role service boundaries. WP-2C-A implements **migrate** and the
enrollment-time **schema** step (F8):

  * fresh initialization and upgrades through `house_helper.persistence.pg_executor`, with
    the mandatory post-migration re-application of `db/privileges.sql`;
  * no migration without a Mode B authorization bound to this image's SQL artifacts and to
    the live database's enrolled marker and inventory;
  * `schema` is not a sixth Compose service: the host runs it through the `migrate` service
    (`docker compose run migrate schema`). It executes only the unchanged `db/schema.sql`
    under the authenticated `hh_migrate` -> `hh_owner` session, before the marker, and can
    never run Alembic or arbitrary SQL.

**provision** (P0 cutover) runs exactly two administrative commands, `enroll-user` and
`enrollment-status`, under the `hh_provision` login. It takes the shared state-volume lock,
requires the enrolled-cluster marker and the H2 CONNECT handoff (a successful connection is
server-enforced evidence of it), and delegates to `house_helper.admin.cli` without
reimplementing an invitation, a user rule or a transaction. The invitation secret is
printed only by the admin CLI on stdout, exactly once; the provisioning service has no log
driver and the one-shot never copies the secret anywhere.

**purge** (C7-P3) runs the existing `purge-expired` administrative command (optionally
`--dry-run`) under the `hh_purge` login. It takes the same shared state-volume lock, so it
runs beside the web service and a backup and is refused while a migration or restore holds
the volume exclusively. Before the command runs it observes its own session, database and
CONNECT privilege and the enrolled marker: a missing H3 `hh_purge` handoff is refused by
the server before any statement of the command runs, and no SQL of the command is ever
executed without the CONNECT privilege that handoff grants. Naming that refusal is a
separate question (`_connection_failure`): the pinned psycopg 3.3.6 exposes no SQLSTATE for
a failed startup, so it is reported fail-closed as `purge_database_unavailable` (exit 4)
with the handoff named, and never guessed from the driver's (localized) message text.
Retention semantics, batching, transaction boundaries and the report stay in
`house_helper.admin.retention`; this one-shot does not reimplement them. A partial, degraded
or refused run keeps the admin command's non-zero exit status, so the systemd unit (and its
heartbeat) can never record a success that did not happen.

`backup` and `restore` remain boundaries that refuse with EX_CONFIG (78). The
application image intentionally has no `pg_dump`/`pg_restore` (BAK-13), so the
backup/restore roles are executed by the host tool `deploy/postgres/maintenance.py` in
disposable pinned `postgres:16.x` client containers, not by these app-image one-shots.
The scheduled purge path and its systemd units are C7-P3 (`deploy/postgres/scheduled_maintenance.md`).

The authority channel
---------------------
The host tool (`deploy/postgres/enroll.py`) runs

    docker compose ... run -T --name house-helper-pg-migrate-run-<run_id> \
      -e HOUSE_HELPER_PG_AUTHORIZATION_STDIN=1 migrate

The migration worker is not launched with `--rm`: the retained, run-specific container is
the reconciler's lifecycle evidence after a client timeout (F10).

and writes the non-secret authorization JSON to the container's stdin. The channel is
opened only by that explicit runtime variable; the compose file does not set it. A direct
`docker compose run migrate` therefore refuses before it reads anything, and even an
explicitly opened channel is not an authorization: `pg_executor` validates the record,
re-reads the live cluster, and compares the artifact manifest with this image's own files.

Exit codes (the same shape as deploy/ops/dbtool.py):
  0   the migration/initialization or the retention command completed
  1   the retention command failed unexpectedly (its own report says what committed)
  2   usage: the role argument is missing or unknown, or a purge argument is not accepted
  3   refused: a wiring, authorization, identity or state condition failed, including a
      handoff refusal the driver *did* report with SQLSTATE 42501 (`<role>_connect_handoff`,
      i.e. `purge_connect_handoff` / `provision_connect_handoff`) and `database_busy`
  4   database unavailable: the connection did not complete, or the schema is not the image's
      head. A startup refusal (including a missing H3 CONNECT handoff) carries no SQLSTATE in
      the pinned driver, so it is reported here, fail-closed, with the handoff named
  78  EX_CONFIG: the one-shot is not implemented, or the activation/wiring is wrong
  130 the provisioning or purge command was interrupted
"""

import os
import sys
from pathlib import Path
from typing import Mapping, Optional

# The only roles the PostgreSQL deploy surface assigns to a one-shot. `schema` is not a
# sixth Compose service: the host runs it through the `migrate` service
# (`docker compose run migrate schema`) so the accepted WP-2B topology is unchanged.
ROLES = ("migrate", "schema", "provision", "purge", "backup", "restore")

EXPECTED_LOGIN = {
    "migrate": "hh_migrate",
    # F8: `db/schema.sql` requires session_user = hh_migrate, current_user = hh_owner.
    "schema": "hh_migrate",
    "provision": "hh_provision",
    "purge": "hh_purge",
    "backup": "hh_backup",
    "restore": "hh_restore",
}

DB_HOST = "house-helper-db"
DB_PORT = "5432"
DB_NAME = "house_helper"
STATE_DIR = "/state"
PASSFILE_DIR = "/run/credentials"
ACTIVATION_VAR = "HOUSE_HELPER_PG_ACTIVATE"
ACTIVATION_VALUE = "1"
AUTHORIZATION_CHANNEL_VAR = "HOUSE_HELPER_PG_AUTHORIZATION_STDIN"
AUTHORIZATION_CHANNEL_VALUE = "1"
AUTHORIZATION_MAX_BYTES = 64 * 1024

EX_OK = 0
EX_INTERNAL = 1
EX_USAGE = 2
EX_REFUSED = 3
EX_DATABASE = 4
EX_CONFIG = 78


def _refuse(
    code: str, message: str, exit_code: int = EX_CONFIG, *, status: str = "refused"
) -> int:
    # Nothing here formats an environment value into the message.
    print(f"{status} [{code}]: {message}", file=sys.stderr)
    return exit_code


def _dsn_login(raw: str) -> Optional[str]:
    """The login named by a postgresql+psycopg URL, or None. The value itself is never shown."""
    prefix = "postgresql+psycopg://"
    if not raw.startswith(prefix):
        return None
    authority, _, path = raw[len(prefix):].partition("/")
    if not authority or path != DB_NAME:
        return None
    if "@" in authority:
        credentials, host = authority.rsplit("@", 1)
    else:
        credentials, host = "", authority
    if ":" in credentials:
        # A password in the DSN is forbidden by the surface, whatever its value.
        return None
    user = credentials
    if host != f"{DB_HOST}:{DB_PORT}" or not user:
        return None
    return user


def _wiring_refusal(role: str, environment: Mapping[str, str]) -> Optional[int]:
    """The WP-2B wiring checks, unchanged and in the same order."""
    login = EXPECTED_LOGIN[role]
    if environment.get(ACTIVATION_VAR) != ACTIVATION_VALUE:
        return _refuse(
            "oneshot_activation",
            f"the {role} one-shot requires {ACTIVATION_VAR} to be exactly "
            f"{ACTIVATION_VALUE}; nothing was executed.",
        )
    for forbidden in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "PGSSLMODE"):
        if forbidden in environment:
            return _refuse(
                "oneshot_credential_source",
                f"the {role} one-shot carries a second credential source or a TLS choice "
                f"({forbidden}); nothing was executed.",
            )
    if environment.get("PGUSER") != login:
        return _refuse(
            "oneshot_login",
            f"the {role} one-shot is not configured for its own login; nothing was executed.",
        )
    if environment.get("PGHOST") != DB_HOST or environment.get("PGPORT") != DB_PORT:
        return _refuse(
            "oneshot_host",
            f"the {role} one-shot does not name the database network's host and port; nothing "
            "was executed.",
        )
    if _dsn_login(environment.get("HOUSE_HELPER_DATABASE_URL", "")) != login:
        return _refuse(
            "oneshot_dsn",
            f"the {role} one-shot does not carry its own password-free DSN; nothing was "
            "executed.",
        )
    if environment.get("PGPASSFILE") != f"{PASSFILE_DIR}/{login}.pgpass":
        return _refuse(
            "oneshot_passfile",
            f"the {role} one-shot does not name its own passfile; nothing was executed.",
        )
    if environment.get("HOUSE_HELPER_STATE_DIR") != STATE_DIR:
        return _refuse(
            "oneshot_state",
            f"the {role} one-shot does not name the shared state volume; nothing was executed.",
        )
    if role == "restore":
        if "PGDATABASE" in environment:
            return _refuse(
                "oneshot_database",
                "the restore one-shot must not pre-select the OD-2 maintenance database; "
                "nothing was executed.",
            )
    elif environment.get("PGDATABASE") != DB_NAME:
        return _refuse(
            "oneshot_database",
            f"the {role} one-shot does not name the serving database; nothing was executed.",
        )
    return None


def _read_authorization(stdin) -> Optional[bytes]:
    """Read at most one bounded record from a deliberately opened, non-TTY channel."""
    if stdin is None:
        stdin = sys.stdin.buffer
    isatty = getattr(stdin, "isatty", None)
    if callable(isatty) and isatty():
        return None
    try:
        payload = stdin.read(AUTHORIZATION_MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, bytes) or not payload or len(payload) > AUTHORIZATION_MAX_BYTES:
        return None
    return payload


def _default_lock(purpose: str = "the PostgreSQL migration"):
    """The WP-2A state-volume lock, imported from `dbtool.py` beside this script."""
    try:
        import dbtool
    except ImportError:  # pragma: no cover - the image and the repository both contain it
        raise RuntimeError("dbtool.py is unavailable") from None
    return dbtool.MaintenanceLock(
        Path(STATE_DIR) / dbtool.LOCK_FILE_NAME,
        exclusive=True,
        purpose=purpose,
        location="state volume",
    )


# --- provision: the two supported administrative commands -----------------------------------

PROVISION_COMMANDS = ("enroll-user", "enrollment-status")
PROVISION_LOGIN = EXPECTED_LOGIN["provision"]


class ProvisionError(Exception):
    """A fail-closed provisioning refusal. `code` is stable and carries no secret."""

    def __init__(self, code: str, message: str, exit_code: int = EX_REFUSED) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _shared_lock(purpose: str = "the PostgreSQL provisioning command"):
    """The WP-2A state-volume lock, SHARED (LCK-01/LCK-02).

    Provisioning coexists with the serving app, a backup and a purge; it is refused while
    a migration or restore holds the volume exclusively. This is not the worker's
    exclusive lock and never the journal lock.
    """
    try:
        import dbtool
    except ImportError:  # pragma: no cover - the image and the repository both contain it
        raise RuntimeError("dbtool.py is unavailable") from None
    return dbtool.MaintenanceLock(
        Path(STATE_DIR) / dbtool.LOCK_FILE_NAME,
        exclusive=False,
        purpose=purpose,
        location="state volume",
    )


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value) in ("t", "true", "True", "1")


def _sqlstate(error: BaseException) -> str:
    """The driver's SQLSTATE, from either psycopg 3 (`sqlstate`) or psycopg 2 (`pgcode`)."""
    for candidate in (getattr(error, "orig", None), error):
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(candidate, attribute, None)
            if value:
                return str(value)
    return ""


def _connection_failure(
    error: BaseException, *, role: str, login: str, handoff: str, handoff_command: str
) -> tuple[str, str, int]:
    """Classify a failed connection from structured driver evidence ONLY (F-02).

    A connection that fails at *startup* carries no SQLSTATE in this deployment's driver.
    psycopg 3.3.6 (pinned in requirements.txt) has `generators._connect` raise
    `psycopg.OperationalError` from `PQconnectPoll` with only `pgconn=`: no `info=` is
    supplied, so `Error.sqlstate`, `Error.diag.sqlstate` and `pgcode` are all absent, and
    SQLAlchemy wraps it unchanged with `.orig` pointing at that exception. Reproduced with
    the pinned driver against PostgreSQL 16.15 for a refused CONNECT (the server's
    ErrorResponse does carry 42501), a refused password (28P01), a missing database (3D000)
    and a closed port (`psycopg.errors.ConnectionTimeout`). The only evidence the driver
    keeps is the finished `PGconn`; its message is libpq's, localized, and is therefore never
    read here as an authorization fact - nor as a cause.

    So 42501 is believed only when the driver actually supplies it (psycopg 2's `pgcode`, a
    driver that attaches `info=`, or a denial raised after the session exists). Every other
    startup failure is one fail-closed `database unavailable` (exit 4) that names the
    ambiguity and the handoff to verify, and states that nothing was executed.
    """
    if _sqlstate(error) == "42501":
        return (
            f"{role}_connect_handoff",
            f"the database refused the {login} connection: the {handoff} CONNECT handoff "
            "is not in place; nothing was executed.",
            EX_REFUSED,
        )
    return (
        f"{role}_database_unavailable",
        f"the database could not be used: the {login} connection did not complete "
        "(unreachable, a refused credential, or a refused CONNECT). The driver supplies no "
        f"SQLSTATE for a connection that fails at startup, so a missing {handoff} CONNECT "
        "handoff cannot be told apart from the others here. Nothing was executed; if that "
        f"handoff was expected, check `enroll.py status` and re-run `{handoff_command}`.",
        EX_DATABASE,
    )


def provision_target_failures(
    *,
    session_user: object,
    current_user: object,
    database: object,
    connect: object,
    temporary: object,
    marker: object,
) -> tuple[str, ...]:
    """The enrolled-cluster/H2-handoff gate, as one pure decision.

    A valid target is: this login (`hh_provision`), in the serving database, holding
    CONNECT and no TEMPORARY privilege, with a parseable enrollment marker. A missing or
    foreign marker is not an enrolled cluster; a failed connection never reaches here.
    """
    from house_helper.persistence import pg_journal

    failures: list[str] = []
    if str(session_user) != PROVISION_LOGIN:
        failures.append("session_user")
    if str(current_user) != PROVISION_LOGIN:
        failures.append("current_user")
    if str(database) != DB_NAME:
        failures.append("database")
    if _as_bool(connect) is not True:
        failures.append("connect_handoff")
    if _as_bool(temporary) is not False:
        failures.append("temporary_privilege")
    try:
        pg_journal.parse_marker(marker)
    except pg_journal.JournalError:
        failures.append("marker")
    return tuple(failures)


def _verify_provision_connection(engine) -> None:
    """Read the live session, privilege and marker facts, and fail closed.

    This is a pre-check, not a replacement for the admin CLI's own schema-at-head check:
    it proves the connection really is `hh_provision` on the enrolled serving database and
    that the H2 CONNECT handoff is in effect. The password, DSN and driver text are never
    formatted into a message.

    The physical `system_identifier` is host-authoritative, exactly as for the migrate
    executor: an `hh_provision` session cannot read `pg_control_system()`. The host's H2
    `handoff` binds that identity with Mode B; this function enforces the half the login
    can observe (its own session, the serving database, the marker and the privilege).
    """
    import sqlalchemy as sa

    from house_helper.persistence import pg_contract as contract

    try:
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT session_user, current_user, pg_catalog.current_database()"
            ).fetchone()
            if row is None or len(row) != 3:
                raise ProvisionError(
                    "provision_observation",
                    "the provisioning session could not be read; nothing was executed.",
                )
            session_user, current_user, database = (str(value) for value in row)
            privilege = connection.exec_driver_sql(
                "SELECT pg_catalog.has_database_privilege(current_user, "
                "pg_catalog.current_database(), 'CONNECT'), "
                "pg_catalog.has_database_privilege(current_user, "
                "pg_catalog.current_database(), 'TEMPORARY')"
            ).fetchone()
            if privilege is None or len(privilege) != 2:
                raise ProvisionError(
                    "provision_observation",
                    "the provisioning privileges could not be read; nothing was executed.",
                )
            connect, temporary = _as_bool(privilege[0]), _as_bool(privilege[1])
            marker_row = connection.exec_driver_sql(
                contract.marker_sql(contract.SERVING_DATABASE)
            ).fetchone()
            marker = "" if marker_row is None else str(marker_row[0] or "")
    except sa.exc.DBAPIError as error:
        code, message, exit_code = _connection_failure(
            error, role="provision", login=PROVISION_LOGIN, handoff="H2",
            handoff_command="enroll.py handoff --role hh_provision",
        )
        raise ProvisionError(code, message, exit_code) from None
    except ProvisionError:
        raise

    failures = provision_target_failures(
        session_user=session_user,
        current_user=current_user,
        database=database,
        connect=connect,
        temporary=temporary,
        marker=marker,
    )
    if failures:
        if "connect_handoff" in failures:
            code = "provision_connect_handoff"
        elif "temporary_privilege" in failures:
            code = "provision_privilege_boundary"
        elif "marker" in failures:
            code = "provision_marker"
        else:
            code = "provision_identity"
        raise ProvisionError(
            code,
            "the provisioning target is not the enrolled hh_provision database ("
            + ", ".join(failures)
            + "); nothing was executed.",
        )


def _provision(
    environment: Mapping[str, str],
    arguments: list,
    *,
    admin_main=None,
    engine_factory=None,
    lock_factory=None,
    verifier=None,
) -> int:
    """Run exactly one supported administrative command as `hh_provision`.

    The admin CLI owns the user/invitation rules (cap, unique display name, advisory
    lock, single-use invitation, no identity matching). This function adds only the
    deployment's missing preconditions: its own wiring, its passfile, the shared lock,
    the enrolled marker and the H2 CONNECT handoff.
    """
    if not arguments:
        return _refuse(
            "provision_command",
            "the provision one-shot needs exactly one supported administrative command "
            "(enroll-user or enrollment-status); nothing was executed.",
            EX_USAGE,
            status="usage",
        )
    if arguments[0] not in PROVISION_COMMANDS:
        # Never echo the rejected text: an operator may have pasted a secret by mistake.
        return _refuse(
            "provision_command",
            "the provision one-shot runs only enroll-user and enrollment-status; "
            "nothing was executed.",
            EX_REFUSED,
        )

    from house_helper.persistence import pg_credentials as credentials

    try:
        credentials.validate_passfile(
            environment.get("PGPASSFILE"),
            login=EXPECTED_LOGIN["provision"],
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            require_private=os.name == "posix",
            require_owner=os.name == "posix",
        )
    except credentials.PassfileError as error:
        return _refuse(error.code, error.message, EX_REFUSED)

    try:
        with (lock_factory or _shared_lock)("the PostgreSQL provisioning command"):
            if engine_factory is None:
                from house_helper.persistence import pg_executor

                engine_factory = pg_executor.create_db_engine
            engine = engine_factory()
            try:
                (verifier or _verify_provision_connection)(engine)
            finally:
                engine.dispose()
            if admin_main is None:
                from house_helper.admin import cli as admin_cli

                admin_main = admin_cli.main
            return int(admin_main(list(arguments), environ=dict(environment)))
    except ProvisionError as error:
        return _refuse(error.code, error.message, error.exit_code)
    except KeyboardInterrupt:
        print(
            "error [interrupted]: the provisioning command was interrupted; an unfinished "
            "change was rolled back or not confirmed. Run the same command again to see "
            "the state (it is safe to repeat).",
            file=sys.stderr,
        )
        return 130
    except Exception as error:  # an exception's text can carry SQL, a DSN or a secret
        if hasattr(error, "code") and hasattr(error, "exit_code"):
            # A WP-2A dbtool ToolError (the shared state-volume lock is busy, ...).
            return _refuse(str(error.code), str(error.message), EX_REFUSED)
        print(
            f"error [internal]: the provision command failed with {type(error).__name__}.",
            file=sys.stderr,
        )
        return EX_INTERNAL


# --- purge: the scheduled retention command under hh_purge -----------------------------------

PURGE_LOGIN = EXPECTED_LOGIN["purge"]


class PurgeError(Exception):
    """A fail-closed purge refusal. `code` is stable and carries no secret."""

    def __init__(self, code: str, message: str, exit_code: int = EX_REFUSED) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _purge_arguments(arguments: list) -> Optional[list]:
    """The only accepted purge arguments: none (a real run) or exactly `--dry-run`.

    The admin command's own parser is intentionally narrow, but this one-shot adds the
    deployment checks ahead of it, so it must not forward an arbitrary argument list that
    could change what is deleted. Anything else is a usage refusal before a connection.
    """
    if not arguments:
        return []
    if arguments == ["--dry-run"]:
        return ["--dry-run"]
    return None


def purge_target_failures(
    *,
    session_user: object,
    current_user: object,
    database: object,
    connect: object,
    temporary: object,
    marker: object,
) -> tuple[str, ...]:
    """The enrolled-cluster/H3-handoff gate for the purge login, as one pure decision.

    A valid target is this login (`hh_purge`), in the serving database, holding CONNECT
    and no TEMPORARY privilege, with a parseable enrollment marker. A failed connection
    never reaches here: the server refuses it when the H3 handoff is absent.
    """
    from house_helper.persistence import pg_journal

    failures: list[str] = []
    if str(session_user) != PURGE_LOGIN:
        failures.append("session_user")
    if str(current_user) != PURGE_LOGIN:
        failures.append("current_user")
    if str(database) != DB_NAME:
        failures.append("database")
    if _as_bool(connect) is not True:
        failures.append("connect_handoff")
    if _as_bool(temporary) is not False:
        failures.append("temporary_privilege")
    try:
        pg_journal.parse_marker(marker)
    except pg_journal.JournalError:
        failures.append("marker")
    return tuple(failures)


def _verify_purge_connection(engine) -> None:
    """Observe the purge session, privilege boundary and enrolled marker before any DELETE.

    This is the H3 half of ENR-15/§7.3 that the login itself can see. The physical
    `system_identifier` and the container identity are host-authoritative and are checked
    by the journaled `enroll.py handoff --role hh_purge` (Mode B) that must precede this
    command. A missing CONNECT handoff makes PostgreSQL refuse the startup before any
    statement of `purge-expired` runs; whether the driver can *name* that refusal is a
    separate question, answered in `_connection_failure`: the pinned psycopg 3.3.6 supplies
    no SQLSTATE for a failed startup, so the refusal is reported fail-closed as a database
    refusal that names the handoff, never guessed from the message text. The host journal
    itself is deliberately not readable from this container: CONNECT is the server-enforced
    effect of the journaled grant. The password, DSN and driver text are never formatted
    into a message.
    """
    import sqlalchemy as sa

    from house_helper.persistence import pg_contract as contract

    try:
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT session_user, current_user, pg_catalog.current_database()"
            ).fetchone()
            if row is None or len(row) != 3:
                raise PurgeError(
                    "purge_observation",
                    "the purge session could not be read; nothing was executed.",
                )
            session_user, current_user, database = (str(value) for value in row)
            privilege = connection.exec_driver_sql(
                "SELECT pg_catalog.has_database_privilege(current_user, "
                "pg_catalog.current_database(), 'CONNECT'), "
                "pg_catalog.has_database_privilege(current_user, "
                "pg_catalog.current_database(), 'TEMPORARY')"
            ).fetchone()
            if privilege is None or len(privilege) != 2:
                raise PurgeError(
                    "purge_observation",
                    "the purge privileges could not be read; nothing was executed.",
                )
            connect, temporary = _as_bool(privilege[0]), _as_bool(privilege[1])
            marker_row = connection.exec_driver_sql(
                contract.marker_sql(contract.SERVING_DATABASE)
            ).fetchone()
            marker = "" if marker_row is None else str(marker_row[0] or "")
    except sa.exc.DBAPIError as error:
        code, message, exit_code = _connection_failure(
            error, role="purge", login=PURGE_LOGIN, handoff="H3",
            handoff_command="enroll.py handoff --role hh_purge",
        )
        raise PurgeError(code, message, exit_code) from None
    except PurgeError:
        raise

    failures = purge_target_failures(
        session_user=session_user,
        current_user=current_user,
        database=database,
        connect=connect,
        temporary=temporary,
        marker=marker,
    )
    if failures:
        if "connect_handoff" in failures:
            code = "purge_connect_handoff"
        elif "temporary_privilege" in failures:
            code = "purge_privilege_boundary"
        elif "marker" in failures:
            code = "purge_marker"
        else:
            code = "purge_identity"
        raise PurgeError(
            code,
            "the purge target is not the enrolled hh_purge database ("
            + ", ".join(failures)
            + "); nothing was executed.",
        )


def _purge(
    environment: Mapping[str, str],
    arguments: list,
    *,
    admin_main=None,
    engine_factory=None,
    lock_factory=None,
    verifier=None,
) -> int:
    """Run exactly the retention command as `hh_purge`, under the shared volume lock.

    The shared lock is the same one the web process and a backup hold: purge coexists with
    them and is refused while a migration or restore holds the state volume exclusively.
    `house_helper.admin.retention` owns the deletion policy, the batches and the report;
    this function adds only the deployment's preconditions (its own wiring and passfile,
    the lock, the enrolled marker and the H3 CONNECT handoff) and returns the admin
    command's exit status unchanged. It therefore cannot turn a partial run into success.
    """
    permitted = _purge_arguments(arguments)
    if permitted is None:
        # Never echo the rejected text: an operator may have pasted a secret by mistake.
        return _refuse(
            "purge_arguments",
            "the purge one-shot accepts no arguments or exactly --dry-run; nothing was "
            "executed.",
            EX_USAGE,
            status="usage",
        )

    from house_helper.persistence import pg_credentials as credentials

    try:
        credentials.validate_passfile(
            environment.get("PGPASSFILE"),
            login=PURGE_LOGIN,
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            require_private=os.name == "posix",
            require_owner=os.name == "posix",
        )
    except credentials.PassfileError as error:
        return _refuse(error.code, error.message, EX_REFUSED)

    try:
        with (lock_factory or _shared_lock)("the PostgreSQL purge command"):
            if engine_factory is None:
                from house_helper.persistence import pg_executor

                engine_factory = pg_executor.create_db_engine
            engine = engine_factory()
            try:
                (verifier or _verify_purge_connection)(engine)
            finally:
                engine.dispose()
            if admin_main is None:
                from house_helper.admin import cli as admin_cli

                admin_main = admin_cli.main
            return int(
                admin_main(["purge-expired", *permitted], environ=dict(environment))
            )
    except PurgeError as error:
        return _refuse(error.code, error.message, error.exit_code)
    except KeyboardInterrupt:
        print(
            "error [interrupted]: the purge command was interrupted; the batch in progress "
            "was rolled back and the batches that committed are gone. Run the same command "
            "again to finish the job.",
            file=sys.stderr,
        )
        return 130
    except Exception as error:  # an exception's text can carry SQL, a DSN or a secret
        if hasattr(error, "code") and hasattr(error, "exit_code"):
            # A WP-2A dbtool ToolError (the shared state-volume lock is busy, ...).
            return _refuse(str(error.code), str(error.message), EX_REFUSED)
        print(
            f"error [internal]: the purge command failed with {type(error).__name__}.",
            file=sys.stderr,
        )
        return EX_INTERNAL


def _migrate(
    environment: Mapping[str, str],
    *,
    stdin,
    executor=None,
    lock_factory=None,
) -> int:
    if environment.get(AUTHORIZATION_CHANNEL_VAR) != AUTHORIZATION_CHANNEL_VALUE:
        return _refuse(
            "authorization_channel",
            "the migrate one-shot accepts only a host-authorized run; "
            f"{AUTHORIZATION_CHANNEL_VAR} is not {AUTHORIZATION_CHANNEL_VALUE}.",
            EX_REFUSED,
        )
    payload = _read_authorization(stdin)
    if payload is None:
        return _refuse(
            "authorization",
            "no bounded Mode B authorization was supplied on the authorized channel.",
            EX_REFUSED,
        )

    from house_helper.persistence import pg_artifacts
    from house_helper.persistence import pg_contract
    from house_helper.persistence import pg_credentials as credentials
    from house_helper.persistence import pg_executor
    from house_helper.persistence import pg_journal

    try:
        credentials.validate_passfile(
            environment.get("PGPASSFILE"),
            login=EXPECTED_LOGIN["migrate"],
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            require_private=os.name == "posix",
            require_owner=os.name == "posix",
        )
    except credentials.PassfileError as error:
        return _refuse(error.code, error.message, EX_REFUSED)

    if executor is None:
        executor = pg_executor.Executor(
            lock=lock_factory or _default_lock,
            engine_factory=pg_executor.create_db_engine,
        )
    try:
        report = executor.run(payload)
    except (
        pg_executor.ExecutorError,
        pg_journal.JournalError,
        pg_artifacts.ArtifactError,
        pg_contract.ContractError,
    ) as error:
        return _refuse(
            error.code,
            error.message,
            EX_DATABASE if getattr(error, "code", "") == "database_unavailable" else EX_REFUSED,
        )
    except Exception as error:  # an exception's text can quote SQL or a path: class only
        if hasattr(error, "code") and hasattr(error, "exit_code"):
            # A WP-2A dbtool ToolError (the exclusive state-volume lock is busy).
            return _refuse(str(error.code), str(error.message), EX_REFUSED)
        print(
            f"error [internal]: the migration failed with {type(error).__name__}.",
            file=sys.stderr,
        )
        return EX_INTERNAL
    print(
        f"ok [migrate]: {report.plan}; revision {report.revision}; "
        f"{len(report.actions)} authorized steps."
    )
    return EX_OK


def _schema(
    environment: Mapping[str, str],
    *,
    stdin,
    engine_factory=None,
    lock_factory=None,
) -> int:
    """The enrollment-time `db/schema.sql` step (F8), never a migration.

    The host opens the same explicit stdin channel it uses for `migrate`, but with a
    schema-only record. This function executes exactly one frozen artifact under the
    authenticated `hh_migrate` -> `hh_owner` session, holds the state-volume exclusive
    lock, and refuses if any application relation or revision row already exists. There is
    no path from here to Alembic, to arbitrary SQL, or to the bootstrap superuser's local
    peer session.
    """
    if environment.get(AUTHORIZATION_CHANNEL_VAR) != AUTHORIZATION_CHANNEL_VALUE:
        return _refuse(
            "authorization_channel",
            "the schema one-shot accepts only a host-authorized run; "
            f"{AUTHORIZATION_CHANNEL_VAR} is not {AUTHORIZATION_CHANNEL_VALUE}.",
            EX_REFUSED,
        )
    payload = _read_authorization(stdin)
    if payload is None:
        return _refuse(
            "authorization",
            "no bounded schema authorization was supplied on the authorized channel.",
            EX_REFUSED,
        )

    from house_helper.persistence import pg_artifacts
    from house_helper.persistence import pg_contract
    from house_helper.persistence import pg_credentials as credentials
    from house_helper.persistence import pg_executor
    from house_helper.persistence import pg_journal

    try:
        credentials.validate_passfile(
            environment.get("PGPASSFILE"),
            login=EXPECTED_LOGIN["schema"],
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            require_private=os.name == "posix",
            require_owner=os.name == "posix",
        )
    except credentials.PassfileError as error:
        return _refuse(error.code, error.message, EX_REFUSED)

    try:
        value = pg_journal.decode_schema_authorization(payload)
        failures = pg_executor.authorization_artifact_failures(value)
        if failures:
            raise pg_journal.JournalError(
                "artifact_identity",
                "the authorization does not name the SQL artifacts in this image",
                ", ".join(failures),
            )
    except (pg_journal.JournalError, pg_contract.ContractError) as error:
        return _refuse(error.code, error.message, EX_REFUSED)

    engine = (engine_factory or pg_executor.create_db_engine)()
    try:
        try:
            with (lock_factory or _default_lock)(
                "the PostgreSQL enrollment schema step"
            ):
                # F8: the decoded record is consumed by the step itself, which compares
                # its authorized target with the live catalog and this image's artifacts.
                pg_executor.apply_schema_contract(engine, value)
        except (
            pg_executor.ExecutorError,
            pg_journal.JournalError,
            pg_artifacts.ArtifactError,
            pg_contract.ContractError,
            credentials.PassfileError,
        ) as error:
            return _refuse(
                error.code,
                error.message,
                EX_DATABASE
                if getattr(error, "code", "") == "database_unavailable"
                else EX_REFUSED,
            )
        except Exception as error:  # an exception's text can quote SQL or a path: class only
            if hasattr(error, "code") and hasattr(error, "exit_code"):
                return _refuse(str(error.code), str(error.message), EX_REFUSED)
            print(
                f"error [internal]: the schema step failed with {type(error).__name__}.",
                file=sys.stderr,
            )
            return EX_INTERNAL
    finally:
        engine.dispose()
    print("ok [schema]: db/schema.sql applied under hh_migrate -> hh_owner.")
    return EX_OK


def main(
    argv: Optional[list] = None,
    environ: Optional[Mapping[str, str]] = None,
    *,
    stdin=None,
    executor=None,
    lock_factory=None,
    engine_factory=None,
    admin_main=None,
    verifier=None,
    purge_verifier=None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = dict(os.environ if environ is None else environ)
    if not arguments or arguments[0] not in ROLES:
        print(f"usage: pg_oneshot.py {{{','.join(ROLES)}}}", file=sys.stderr)
        return EX_USAGE
    role = arguments[0]
    extra = arguments[1:]
    # `provision` and `purge` each accept their own bounded argument list; every other
    # role takes only its name. The purge list is validated again inside `_purge` before a
    # connection exists.
    if role not in ("provision", "purge") and extra:
        print(f"usage: pg_oneshot.py {{{','.join(ROLES)}}}", file=sys.stderr)
        return EX_USAGE

    refused = _wiring_refusal(role, environment)
    if refused is not None:
        return refused

    if role == "migrate":
        return _migrate(
            environment,
            stdin=stdin,
            executor=executor,
            lock_factory=lock_factory,
        )

    if role == "schema":
        # A direct invocation without the channel is refused inside `_schema`.
        return _schema(
            environment,
            stdin=stdin,
            engine_factory=engine_factory,
            lock_factory=lock_factory,
        )

    if role == "provision":
        return _provision(
            environment,
            extra,
            admin_main=admin_main,
            engine_factory=engine_factory,
            lock_factory=lock_factory,
            verifier=verifier,
        )

    if role == "purge":
        return _purge(
            environment,
            extra,
            admin_main=admin_main,
            engine_factory=engine_factory,
            lock_factory=lock_factory,
            verifier=purge_verifier,
        )

    return _refuse(
        "oneshot_not_implemented",
        f"the PostgreSQL {role} one-shot is not implemented (backup and restore run from "
        "the host tool; the scheduled operations follow). No statement was executed and "
        "nothing was changed.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
