"""The PostgreSQL operational one-shots (HH-DB-01 Stage 2 WP-2B / WP-2C-A).

    python /opt/house-helper/ops/pg_oneshot.py migrate|schema|provision|purge|backup|restore

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

`provision`, `purge`, `backup` and `restore` remain boundaries that refuse with
EX_CONFIG (78). The application image intentionally has no `pg_dump`/`pg_restore`
(BAK-13), so the backup/restore roles are executed by the host tool
`deploy/postgres/maintenance.py` in disposable pinned `postgres:16.x` client containers,
not by these app-image one-shots. Production account enrollment is WP-2C/WP-2E; the
systemd re-arm is WP-2F.

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
  0   the migration/initialization completed
  2   usage: the role argument is missing or unknown
  3   refused: a wiring, authorization, identity or state condition failed
  4   database unavailable
  78  EX_CONFIG: the one-shot is not implemented, or the activation/wiring is wrong
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


def _refuse(code: str, message: str, exit_code: int = EX_CONFIG) -> int:
    # Nothing here formats an environment value into the message.
    print(f"refused [{code}]: {message}", file=sys.stderr)
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
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = dict(os.environ if environ is None else environ)
    if len(arguments) != 1 or arguments[0] not in ROLES:
        print(f"usage: pg_oneshot.py {{{','.join(ROLES)}}}", file=sys.stderr)
        return EX_USAGE
    role = arguments[0]

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

    return _refuse(
        "oneshot_not_implemented",
        f"the PostgreSQL {role} one-shot is not implemented in WP-2C (backup and restore are "
        "WP-2D; enrollment and the scheduled operations follow). No statement was executed "
        "and nothing was changed.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
