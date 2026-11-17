"""The PostgreSQL operational one-shots: a fail-closed boundary, not an implementation.

    python /opt/house-helper/ops/pg_oneshot.py migrate|provision|purge|backup|restore

HH-DB-01 Stage 2 WP-2B defines the five per-role service boundaries of the PostgreSQL deploy
surface. It does not implement their operations:

  * the initializer/executor (Mode A enrollment, ordered schema initialization, and the
    mandatory re-application of db/privileges.sql after every migration) is WP-2C;
  * PostgreSQL backup and restore are WP-2D;
  * production enrollment is WP-2C/WP-2E;
  * the systemd re-arm and the maintenance journal are WP-2F.

Every invocation therefore exits with EX_CONFIG (78) BEFORE opening a connection or writing
anything. That is deliberate. In particular there must be no operational path that runs
`alembic upgrade head` while omitting the post-migration privilege re-application: a migration
that leaves the application login with the previous objects' grants is worse than a migration
that refuses to start. `dbtool.py exec --exclusive` exists for the executor WP-2C will package;
nothing here calls it.

Before refusing, the script checks that the service was deliberately activated
(`HOUSE_HELPER_PG_ACTIVATE` must be exactly `1`) and that it was wired to the login and state
volume this work package assigned to it. A mismatch is also a refusal (the same exit code),
with a message that names the role, never a value: the DSN and passfile carry no password, but
the rule is kept uniformly. The activation check is first, because Compose's required
interpolation and profiles are selection helpers, not equality checks: `0` and `false` pass
interpolation, and naming the service explicitly activates its profile.

Exit codes (the same shape as deploy/ops/dbtool.py):
  2   usage: the role argument is missing or unknown
  78  EX_CONFIG: the operation is not implemented in WP-2B, or the wiring is wrong
"""

import os
import sys
from typing import Mapping, Optional

# The only roles the PostgreSQL deploy surface assigns to a one-shot. The role name is both
# the libpq login and the service name.
ROLES = ("migrate", "provision", "purge", "backup", "restore")

EXPECTED_LOGIN = {
    "migrate": "hh_migrate",
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

EX_CONFIG = 78
EX_USAGE = 2


def _refuse(code: str, message: str) -> int:
    # Nothing here formats an environment value into the message.
    print(f"refused [{code}]: {message}", file=sys.stderr)
    return EX_CONFIG


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


def main(argv: Optional[list] = None, environ: Optional[Mapping[str, str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = dict(os.environ if environ is None else environ)
    if len(arguments) != 1 or arguments[0] not in ROLES:
        print(f"usage: pg_oneshot.py {{{','.join(ROLES)}}}", file=sys.stderr)
        return EX_USAGE
    role = arguments[0]
    login = EXPECTED_LOGIN[role]

    # The exact-value guard, first. A missing or empty value already fails Compose's required
    # interpolation; `0` and `false` do not, and explicit service selection activates a profile.
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

    # restore's maintenance database is OD-2 and intentionally has no PGDATABASE. The other
    # four are about the serving database and must name it, because the executor WP-2C packages
    # will use these libpq settings as well as the DSN.
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

    return _refuse(
        "oneshot_not_implemented",
        f"the PostgreSQL {role} one-shot is not implemented in WP-2B (WP-2C packages the "
        "initializer/executor; backup and restore are WP-2D). No statement was executed and "
        "nothing was changed.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
