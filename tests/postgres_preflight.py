"""CI-06 preflight and the CI `alembic check` step of HH-DB-01 (CI-10).

Run by .github/workflows/ci.yml after `pip install -r requirements-dev.txt`, and by a
developer before `pytest -m postgres`:

    python -m tests.postgres_preflight
    python -m tests.postgres_preflight --alembic-check

The preflight asks the configured server everything the suite depends on (CI-06): the
URL is the harness's validated one (loopback, an explicit non-5432 port, no query
parameters, no routing libpq variables), the cluster is independently identified
(R3-01: the Compose container locally, the runner's service container in CI, its
control file's system_identifier against `pg_control_system()`), the psycopg 3 driver is
installed, the server answers, it is PostgreSQL 16 (server_version_num
160000-169999), and the configured role can connect to the maintenance database. The `--alembic-check` step creates ONE scratch
database through tests/postgres_harness.py, upgrades it to head and runs `alembic check`
against the live catalog, then drops it - the same guarded lifecycle the test suites use.

Output contains no credential: the URL is never printed, and only the non-secret fields
AUTH-04 allows (host, port, user, database) are shown. Exit code is 0 on success and 1 on
a refusal, with the harness's fixed message.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Mapping, Optional

from alembic import command
from alembic.util.exc import CommandError

from house_helper.persistence.database import alembic_config, upgrade_database
from tests import postgres_harness as harness
from tests.revisions import HEAD_REVISION


def preflight(environ: Optional[Mapping[str, str]] = None) -> int:
    """The CI-06 checks. Returns 0 and prints the non-secret facts, or raises UnsafeTarget.
    R3-01: the cluster is independently identified first (the Compose container locally,
    the runner's service container in CI), and the SQL connection must report the same
    `system_identifier` the container's control file names."""
    environ = os.environ if environ is None else environ
    target = harness.target_from_environment(environ)
    number = harness.preflight(target, environ=environ)
    print(
        "postgresql preflight ok: "
        f"server_version_num={number} host={target.address} port={target.port} "
        f"user={target.username} database={target.maintenance_database}"
    )
    return 0


def alembic_check(environ: Optional[Mapping[str, str]] = None) -> int:
    """CI-10: `alembic check` against a freshly migrated scratch database on the real
    server, through the guarded harness. Nothing survives except the server itself."""
    environ = os.environ if environ is None else environ
    target = harness.target_from_environment(environ)
    run = harness.Run(target, environ=environ)
    try:
        run.prepare_roles()  # CI-17: the migration runs as the per-run migrate login
        name = run.create_database()  # (inside the guard: a partial preparation is cleaned up)
        engine = run.migrate_engine(name)
        try:
            upgrade_database(engine, HEAD_REVISION)  # the revision is stated (CI-08)
            config = alembic_config()
            with engine.connect() as connection:
                config.attributes["connection"] = connection
                command.check(config)
        finally:
            engine.dispose()
    finally:
        run.close()  # drops every database and role this run created, whatever happened
    print("alembic check: no new upgrade operations detected")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="HH-DB-01 PostgreSQL test-server preflight")
    parser.add_argument(
        "--alembic-check",
        action="store_true",
        help="after the preflight, run `alembic check` on a scratch database",
    )
    options = parser.parse_args(argv)
    try:
        preflight()
        if options.alembic_check:
            alembic_check()
    except harness.UnsafeTarget as refusal:
        print(f"preflight refused [{refusal.code}]: {refusal.message}", file=sys.stderr)
        return 1
    except CommandError as drift:
        print(f"alembic check failed: {drift}", file=sys.stderr)
        return 1
    except Exception as problem:  # never print a value that may carry a credential
        print(f"preflight failed: {type(problem).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - the CI entry point
    raise SystemExit(main())
