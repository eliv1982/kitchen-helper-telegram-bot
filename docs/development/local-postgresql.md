# Local PostgreSQL 16 test server

HH-DB-01 makes PostgreSQL the default for integration tests, migrations and future
development. This page describes the disposable server the suite uses on a developer
machine. It is **not** the application's database, not Production, and not the n8n
cluster: the compose project has its own network, its own named volume and a loopback
port, and `tests/postgres_harness.py` refuses anything else.

## Start the server

```sh
docker compose -f compose.test-postgres.yaml up -d
```

The server is `postgres:16` (digest-pinned in the compose file), reachable only at
`127.0.0.1:55432`. Port `5432` is deliberately unused: the harness refuses it because a
long-lived installation usually lives there.

## Point the tests at it

```sh
export HOUSE_HELPER_TEST_POSTGRES_URL="postgresql+psycopg://hh_test:hh_test_only_not_a_secret@127.0.0.1:55432/postgres"
# PowerShell: $env:HOUSE_HELPER_TEST_POSTGRES_URL = "..."
```

The URL must name the psycopg 3 driver, a loopback host, an explicit port other than
5432, a user and a maintenance database, and **no query parameters**. The harness checks
this before opening anything, and checks every connection again before its first
statement.

## Preflight, then run

```sh
python -m tests.postgres_preflight              # CI-06: driver, server, version range, role
pytest -m postgres -ra                          # every PostgreSQL-marked suite
pytest -ra                                      # the whole suite, PostgreSQL suites included
```

Without the variable the PostgreSQL suites are skipped; with it set, a skipped
PostgreSQL test **fails** the run (`tests/conftest.py`, CI-07). CI sets the variable, so
skips are never a pass there.

`python -m tests.postgres_preflight --alembic-check` also upgrades a scratch database
and runs `alembic check` against the live catalog, then drops it - the same step CI runs.

## Remove it

```sh
docker compose -f compose.test-postgres.yaml down -v
```

`-v` removes the named volume. The test suites create and drop only databases they made
themselves (each carries a random token in a `COMMENT`), so leaving the server running
between runs is harmless; removing it is the way to start from an empty cluster.
