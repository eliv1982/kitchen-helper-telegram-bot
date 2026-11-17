# PostgreSQL deploy surface (HH-DB-01 Stage 2 WP-2B)

**Status: repository surface only. Nothing here is deployed.** No PostgreSQL cluster has been
initialized, no role or database has been created, no credential has been generated or
populated, no container, network or volume has been created for this surface, and the
Production deployment remains SQLite. **WP-2B does not authorize a cutover.** The Clean-Slate
PostgreSQL Cutover is governed by
`docs/architecture/HH-DB-01_Architecture_Frozen_Specification_v1.0.md`.

This document records what WP-2B built, what it deliberately did not build, and the decisions
the Owner approved for this work package.

## 1. Decisions recorded for WP-2B

| Topic | Decision | Scope |
|---|---|---|
| **OD-8 credential mechanism** | A separate libpq `PGPASSFILE` for each application/operational role. Each file is mode `0600`, owned by the container's running uid, bind-mounted read-only into exactly one service, and selected by `PGPASSFILE`. No real credential is generated or committed. | WP-2B deployment surface. The production credential population and rotation procedure remain an OD-8 operational act for WP-2C/WP-2F; this document does not approve a rotation policy. |
| **Executor ownership** | **Option A.** WP-2B implements the deployment surface only. The production initializer/executor (Mode A enrollment, ordered schema initialization, post-migration `db/privileges.sql`, enrollment journal) belongs to **WP-2C**. | The `migrate`, `provision`, `purge`, `backup` and `restore` one-shots are boundaries that **fail closed**; they are not implemented. |
| **Compose identity** | A separate project, `house-helper-pg`. The running SQLite project `house-helper` is independent and untouched. | The PostgreSQL web service intentionally reuses the same Traefik router labels; a deliberate cutover must stop the SQLite project first (WP-2F). |
| **Database subnet** | `10.213.79.0/24`, a **candidate**, not a verified Production fact. It must be re-validated against the deployment host's existing Docker networks before any future deployment; the preflight fails closed on an overlap. | Changing it requires changing both `deploy/compose.postgres.yaml` and `deploy/postgres/pg_hba.conf`; the preflight refuses a mismatch. |

These decisions do **not** close any other open Owner choice:

* **OD-6 (TLS)** remains open. No `ssl` / `ssl_*` setting and no `hostssl` line exists. The
  unencrypted internal bridge is residual risk RSK-08, not an approved "no TLS" decision.
* **OD-7 (patch/config)** remains open. The image is pinned by digest; no patch cadence, no
  image-bump procedure and no tuning is invented here.
* **OD-2 (maintenance database)** remains open. The restore one-shot has no `PGDATABASE`, and
  its passfile's database field is `*`; WP-2D parameterizes the OD-2 choice.
* OD-1, OD-3, OD-4, OD-5 and OD-9 remain open as recorded in the Frozen Specification, §12.6.

## 2. Files

| File | Purpose |
|---|---|
| `deploy/compose.postgres.yaml` | The separate PostgreSQL Compose configuration: the pinned PostgreSQL 16 service, the web service, the five one-shot boundaries, the internal database network and the two fixed volumes. |
| `deploy/postgres/pg_hba.conf` | AUTH-01 host-based authentication, with explicit first-match ordering and remote-superuser rejection. |
| `deploy/postgres/postgresql.conf` | The minimal server configuration: listener, scram-sha-256, the explicit HBA file and the socket directory. |
| `deploy/postgres/preflight.py` | The read-only deployment preflight. It creates nothing and runs only `docker compose ... config`, `docker network ls` and `docker network inspect`. |
| `deploy/postgres/credentials/README.md` | The credential layout. Real files are git-ignored and are not generated here. |
| `deploy/ops/pg_oneshot.py` | The fail-closed entrypoint of the five one-shots. It exits `78` before connecting or writing. |

`deploy/compose.yaml`, `deploy/backup.sh`, `deploy/upgrade.sh`, the existing systemd units,
`db/*.sql` and the WP-2A start gate are unchanged.

## 3. Topology

```text
Internet -> Traefik (n8n_n8n_network)
                     |
                house-helper (web)  --- house-helper-pg-db (internal, 10.213.79.0/24)
                     |                                |
              /state (shared lock volume)        house-helper-db (PostgreSQL 16)
                                                      |
                                               house-helper-pgdata
```

* The web service is the **only** service on both the proxy and database networks.
* PostgreSQL is on the database network only. It publishes no port and has no Traefik label.
* Every one-shot is on the database network only. It has no proxy membership, no published
  port, no Traefik label and no log driver.
* The web service does **not** mount the SQLite `house-helper-data` volume (ARC-01).
* `/state` is the WP-2A maintenance-lock volume; PostgreSQL never mounts it.
* The PostgreSQL container name is fixed as `house-helper-db` because Mode A/B enrollment
  identifies the cluster by container name, image digest and PGDATA volume (ENR-20). A
  pre-existing container with that name makes Compose refuse rather than silently create a
  second database.
* The bootstrap superuser is local-`peer` only. An administrative `psql` therefore has to run
  as the container's `postgres` OS user, for example
  `docker exec -u postgres -it house-helper-db psql -U postgres`; a root `docker exec` does
  not match the peer identity. Remote superuser TCP access is explicitly rejected in
  `pg_hba.conf` before the generic scram rule.

## 4. Activation guard

A separate project name does not stop a second web container from registering the same
Traefik routers. WP-2B has three layers, and only the third one checks the value:

1. **A required interpolation.** The PostgreSQL service carries
   `house-helper.pg.activation: "${HOUSE_HELPER_PG_ACTIVATE:?set ...}"`. Compose interpolates
   the whole file before selecting a service, so `up`, `run`, `config` and every other
   ordinary invocation fail before any service starts when the variable is missing or empty.
   This syntax is not an equality check: `0` and `false` pass it.
2. **Profiles.** The database and web services are in profile `pg`; the one-shots are in
   profile `ops`. A plain `docker compose -f deploy/compose.postgres.yaml up` selects nothing
   even after the guard variable is set; a profile must be named explicitly.
3. **The runtime guard.** The database and web services use a `/bin/sh` entrypoint that
   refuses anything but the exact value `1` (exit `78`) and only then replaces itself with the
   explicit service `command`. Compose ignores an image's default CMD whenever a non-null
   entrypoint is declared, so the command is repeated verbatim: the official PostgreSQL
   entrypoint with `config_file=/etc/house-helper/postgresql.conf`, and the Dockerfile's
   WP-2A `dbtool.py serve` start gate. The one-shots perform the same exact check first,
   inside `pg_oneshot.py`. **Explicit service selection activates a Compose profile, but it
   does not bypass this runtime guard**: `up house-helper-db`, `run migrate` and a direct
   service name all still start through the guard.

The preflight requires the resolved database and web entrypoints to be this exact guard (not a
suffix or a copy), the resolved commands to be the exact documented ones (not arbitrary
overrides), every service to receive the unmodified value, and the exact operational
entrypoint on every one-shot.

## 5. Credentials

The WP-2B decision is `PGPASSFILE` per role. On the deployment host:

```text
deploy/postgres/credentials/            (or HOUSE_HELPER_PG_CREDENTIALS_DIR)
  hh_app.pgpass           0600 10001:10001
  hh_migrate.pgpass       0600 10001:10001
  hh_provision.pgpass     0600 10001:10001
  hh_purge.pgpass         0600 10001:10001
  hh_backup.pgpass        0600 10001:10001
  hh_restore.pgpass       0600 10001:10001
  postgres_superuser.password  0600 0:0
```

No file is created by this repository. The default directory is inside the checkout but every
credential file is git-ignored; an operator may point `HOUSE_HELPER_PG_CREDENTIALS_DIR` at a
directory outside the checkout, which is preferable. See
`deploy/postgres/credentials/README.md` for the libpq line format and the ownership rules.

The bootstrap superuser password is read only by the official PostgreSQL image's first-init
process through `POSTGRES_PASSWORD_FILE`. It is never mounted into the web or one-shot
containers. `POSTGRES_HOST_AUTH_METHOD=trust` is not used anywhere.

## 6. Read-only preflight

```bash
python deploy/postgres/preflight.py \
  --env-file /path/to/house-helper.env \
  --credentials-dir /path/to/credentials
```

It checks the activation guard, the env-file prohibition (ARC-03), the seven credential files,
the `pg_hba.conf` order, `postgresql.conf`, the resolved Compose surface and overlap of the
candidate subnet with every existing Docker network. The resolved surface is checked against
a per-service exact mount allowlist of type, resolved source path, destination and read-only
flag, so an extra mount, a replacement `pg_hba.conf`/`postgresql.conf` with the expected
basename, or a lost read-only credential mount is refused. It prints only non-secret facts.
Exit codes: `0` ok, `1` unexpected failure (the exception class only), `2` usage, `3` refused,
`4` Docker/Compose unavailable.

The ownership and mode checks assume a POSIX host with rootful Docker, which is the deployment
target. On a non-POSIX host the preflight skips those two checks (it still parses every file);
it is not a substitute for running it on the host itself.

The candidate subnet check is the reason `10.213.79.0/24` is a candidate: it has not been
validated against the Production host. Do not copy it into the enrollment record before the
Stage 7 inventory and this preflight both pass.

## 7. One-shot boundaries (not implemented)

| Service | Login | Status in WP-2B |
|---|---|---|
| `migrate` | `hh_migrate` | Refuses. WP-2C must run the ordered initializer and **re-apply `db/privileges.sql` after every migration**; no WP-2B path runs `alembic upgrade head`. |
| `provision` | `hh_provision` | Refuses. Production enrollment is WP-2C/WP-2E. |
| `purge` | `hh_purge` | Refuses. Scheduled execution and timer re-arm are WP-2F. |
| `backup` | `hh_backup` | Refuses. PostgreSQL backup is WP-2D. |
| `restore` | `hh_restore` | Refuses. PostgreSQL restore is WP-2D; the OD-2 maintenance database is not selected. |

Each service has exactly one login, one read-only passfile, the database network only and the
`/state` lock volume. `backup` and `restore` also have an anonymous `/staging` volume, as the
SQLite operator tool does. `pg_oneshot.py` first requires the exact activation value, then
validates its own wiring, and exits with `EX_CONFIG` (78). It never opens a connection and
never writes.

## 8. What WP-2B did not do

* No cluster initialization, role creation, database creation, schema migration or privilege
  application.
* No credential generation, population, rotation or disclosure.
* No PostgreSQL backup/restore, offsite archive v2 or restore rehearsal (WP-2D/WP-2E).
* No enrollment journal, maintenance journal, lock handoff, DSN switch, timer re-arm or
  systemd change (WP-2F).
* No Production access, cutover or deployment. No Docker resource was created, removed or
  modified while building this surface.
* No change to `deploy/compose.yaml`, the SQLite scripts, the existing systemd units,
  `db/*.sql`, `deploy/ops/dbtool.py`, `.claude/` or `deploy/monitoring/`.

## 9. Next package

WP-2C owns the production initializer/executor and the OD-8 credential population. Its
minimum obligations before any `migrate` one-shot becomes runnable:

1. Positively identify the cluster (container name, image digest, PGDATA volume,
   `system_identifier`) and write the Mode A intent outside PGDATA.
2. Package and execute `db/roles.sql`, `db/bootstrap.sql`, the Alembic chain and
   `db/privileges.sql` in the frozen order, with the E3/E4 connection fence.
3. Re-apply `db/privileges.sql` after every migration, in the same controlled path.
4. Populate each role's verifier under OD-8, then remove the bootstrap credential from the
   running surface.
5. Prove the refusal paths: a missing state volume, a wrong login, a wrong database, a
   pre-handoff connection and a migration without the privilege re-application all fail closed.
