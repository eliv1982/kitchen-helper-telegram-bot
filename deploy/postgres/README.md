# PostgreSQL deploy surface (HH-DB-01 Stage 2 WP-2B / WP-2C-A / WP-2D)

**Status: repository surface only. Nothing here is deployed.** No PostgreSQL cluster has been
initialized, no role or database has been created, no credential has been generated or
populated, no container, network or volume has been created for this surface, and the
Production deployment remains SQLite. **WP-2B, WP-2C-A and WP-2D do not authorize a
cutover.**
The Clean-Slate PostgreSQL Cutover is governed by
`docs/architecture/HH-DB-01_Architecture_Frozen_Specification_v1.0.md`.

WP-2C-A adds the host-side enrollment/verification tool and the in-container `migrate`
executor. The executor is reachable only through a Mode B authorization; the deployment
surface is unchanged. The authority flow is recorded in
`docs/architecture/HH-DB-01_WP-2C-A_Authority_Flow.md`.

The P0 cutover work adds the two missing application paths without changing the surface:

* `enroll.py handoff --role <hh_app|hh_provision|hh_backup> --approved-by <owner>` is the
  journaled ROL-16/HOF-01 connection handoff. It verifies Mode B and the schema head, records
  an intent, issues exactly one `GRANT CONNECT ON DATABASE house_helper`, re-reads the live
  privilege, and records the observed outcome. A repeated run is an observed no-op; an
  unobservable result stays UNKNOWN and is reconciled by the next run.
* the `provision` one-shot now runs exactly `enroll-user` and `enrollment-status` as
  `hh_provision`, inside the shared state-volume lock, after checking its own passfile, the
  enrolled marker and the H2 CONNECT handoff. It delegates the user/invitation rules to
  `house_helper.admin.cli`; it does not reimplement them.

WP-2D adds PostgreSQL backup and restore through `deploy/postgres/maintenance.py` and the
pure format-v2 contract in `house_helper/persistence/pg_backup.py`. The operating
procedure, prerequisites, failure matrix and explicit non-claims are in
[`backup_restore.md`](backup_restore.md). WP-2D was exercised against the isolated
WP-2C-B PostgreSQL 16.15 environment with a real backup, a target-cluster restore, a
disposable `--network none` restore, an `hh_app` smoke and `alembic check`; it was not
deployed and no Production database was touched.

This document records what WP-2B built, what it deliberately did not build, and the decisions
the Owner approved for this work package.

## 1. Decisions recorded for WP-2B

| Topic | Decision | Scope |
|---|---|---|
| **OD-8 credential mechanism** | A separate libpq `PGPASSFILE` for each application/operational role. Each file is mode `0600`, owned by the container's running uid, bind-mounted read-only into exactly one service, and selected by `PGPASSFILE`. No real credential is generated or committed. | WP-2B deployment surface. The production credential population and rotation procedure remain an OD-8 operational act for WP-2C/WP-2F; this document does not approve a rotation policy. |
| **Executor ownership** | **Option A.** WP-2B implements the deployment surface only. The production initializer/executor (Mode A enrollment, ordered schema initialization, post-migration `db/privileges.sql`, enrollment journal) belongs to **WP-2C**. | WP-2B left all five one-shots fail-closed. Later packages implement `migrate`/`schema` (WP-2C-A), `provision` (P0-2) and `purge` (C7-P3); `backup` and `restore` run from `maintenance.py`, never from the app-image one-shots. |
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
| `deploy/postgres/enroll.py` | WP-2C-A host tool plus the P0-1 handoff: Mode A enrollment (including the ENR-04 schema/default-ACL step before the marker), authoritative Mode B verification, the bounded migrate authorization, the journaled H1/H2/H3 `CONNECT` handoff, worker-lifecycle reconciliation evidence, credential checks and the bootstrap-retirement plan. Root + Docker socket; never copied into the image. |
| `deploy/postgres/maintenance.py` | WP-2D host tool: Mode B verification, format-v2 backup, archive verification, target-cluster restore with temporary-database records and isolated `--network none` rehearsal. Root + Docker socket; never copied into the image. |
| `deploy/postgres/backup_restore.md` | The WP-2D operating procedure, prerequisites, failure matrix and explicit non-claims. |
| `deploy/postgres/scheduled_maintenance.md` | The C7-P3 owner procedure for the PostgreSQL purge and encrypted offsite systemd units: image/unit/credential preflight, the manual-command environment, the live gates (refusal, dry run, real run, backup round trip, monitoring), `Persistent=true` catch-up, first-run verification and the section 6 recovery response (stop the automated maintenance; the legacy SQLite units are historical assets, not a rollback step). |
| `deploy/postgres/credentials/README.md` | The credential layout. Real files are git-ignored and are not generated here. |
| `deploy/ops/pg_oneshot.py` | The fail-closed entrypoint of the five one-shots. `migrate` and the enrollment-time `schema` step are implemented in WP-2C-A; `provision` is implemented by P0-2 (`enroll-user`, `enrollment-status` only); `purge` is implemented by C7-P3 (`purge-expired`, optionally `--dry-run`, under `hh_purge`); `backup` and `restore` still exit `78` before connecting or writing and run from `maintenance.py`. `schema` is run through the `migrate` service, so the Compose topology is unchanged. |

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

## 7. One-shot boundaries

| Service | Login | Status |
|---|---|---|
| `migrate` | `hh_migrate` | **Implemented in WP-2C-A**, reachable only through `enroll.py migrate`. It re-verifies the live cluster, runs the ordered fresh initializer or a verified upgrade, and **re-applies `db/privileges.sql` after every migration**. |
| `provision` | `hh_provision` | **Implemented by P0-2**, reachable only with `enroll-user` or `enrollment-status`. Takes the shared state-volume lock, validates its own passfile, and refuses unless the live session is `hh_provision` on the enrolled `house_helper` with CONNECT and no TEMPORARY. The admin CLI owns every user and invitation rule. |
| `purge` | `hh_purge` | **Implemented by C7-P3**, reachable only as `purge` or `purge --dry-run`. Takes the shared state-volume lock, validates its own passfile, and refuses unless the live session is `hh_purge` on the enrolled `house_helper` with CONNECT and no TEMPORARY. A missing H3 handoff makes the startup fail; because the pinned psycopg 3.3.6 supplies no SQLSTATE for a startup refusal, that case exits 4 as `purge_database_unavailable` (naming the handoff) rather than being guessed as `purge_connect_handoff` — which exit 3 reports only when the driver does supply SQLSTATE 42501 or the connected session fails the CONNECT gate. `schema_not_head` is also exit 4; `scheduled_maintenance.md` §1.1 has the complete status table and the monitoring categories. It then delegates to the unchanged `house_helper.admin purge-expired` and returns its exit status. **H3 (`enroll.py handoff --role hh_purge`) must succeed first.** |
| `backup` | `hh_backup` | Refuses. The application image has no client binaries (BAK-13); WP-2D runs `pg_dump` in a disposable pinned `postgres:16.x` client container from `maintenance.py`. **H3 (`enroll.py handoff --role hh_backup`) must succeed before the first backup, including a manual one.** |
| `restore` | `hh_restore` | Refuses for the same reason. WP-2D runs the ordered restore and isolated rehearsal from `maintenance.py`; the OD-2 maintenance database is an explicit required argument. |

Each service has exactly one login, one read-only passfile, the database network only and the
`/state` lock volume. `backup` and `restore` also have an anonymous `/staging` volume, as the
SQLite operator tool does. `pg_oneshot.py` first requires the exact activation value, then
validates its own wiring; the two unimplemented roles (`backup`, `restore`) exit with
`EX_CONFIG` (78).

### 7.1 Provisioning invocation

The first word after `run` is the Compose service (`provision`). Its declared command
(`["provision"]`) is replaced by the arguments given, so the one-shot's role argument
(`provision`) is repeated first and the administrative command follows:

```bash
docker compose -f deploy/compose.postgres.yaml --profile ops run --rm -T \
  provision provision enroll-user --display-name "Owner"
docker compose -f deploy/compose.postgres.yaml --profile ops run --rm -T \
  provision provision enrollment-status
```

`HOUSE_HELPER_PG_ACTIVATE=1`, `HOUSE_HELPER_PG_CREDENTIALS_DIR` and
`HOUSE_HELPER_IMAGE_TAG` are the same as for `migrate`. The provisioning service has
`logging: driver: none`, so the invitation secret (`invitation code: ...`, printed once on
stdout by the admin CLI) is not kept in Docker's logs. Run it in a terminal whose scrollback
you control; do not pipe it through `tee`.

### 7.2 CONNECT handoffs

`enroll.py handoff` is the only supported way to give a runtime login its database access.
The H1/H2/H3 surface authorizes `hh_app` (H1, before the web container starts),
`hh_provision` (H2, before the first `enroll-user`) and the two H3 scheduled logins
`hh_purge` and `hh_backup` (C7-P3 and the P0 cutover respectively), each with its own
single-role grant and intent/outcome pair under the frozen `handoff:scheduled` event for
H3. **H3 is required before the first PostgreSQL purge or backup of any kind, including a
manual `maintenance.py backup` or `pg_dump`-equivalent run.** Before its H3 grant, the login
has no `CONNECT` and the operation refuses at the connection; such an attempt must never be
treated as an implicit handoff. That startup refusal is not distinguishable from any other
failed startup by structured driver evidence: psycopg 3.3.6 raises it with only `pgconn=`,
so `sqlstate`/`pgcode` are absent even though the server sent 42501. `pg_oneshot.py` reports
it fail-closed as `purge_database_unavailable` / `provision_database_unavailable` (exit 4),
naming the handoff to verify, and never classifies from the (localized) message text. The
command:

1. takes the host operation lock and requires an exactly-one completed enrollment record;
2. verifies the live container/volume/image identity, marker and role/database/predefined
   inventories (Mode B);
3. requires the schema at this image's head, a materialized database ACL, no PUBLIC entry,
   no grant option, no `CREATE`/`TEMPORARY` for any login role, and CONNECT provenance for
   **every** login role: only `hh_migrate` (pre-handoff) and roles with a journaled
   successful handoff may hold CONNECT. An unjournaled CONNECT on any role - including one
   not targeted by the current command - is refused;
4. resolves every earlier handoff intent that has no outcome **before** attempting a new
   transition (see below), and refuses a pre-existing CONNECT on the target role that has no
   journaled handoff;
5. appends a `handoff` intent carrying the complete pre-state ACL and the operation's unique
   `application_name`, issues exactly `GRANT CONNECT ON DATABASE "house_helper" TO "<role>"`
   in one tagged psql session, compares the complete post-state ACL, and appends the outcome.
   The post-state must equal the pre-state plus exactly one explicit, non-grantable CONNECT
   entry for that role. Any other entry, effective privilege or grant option - on any role -
   is a collateral change and is never accepted.

**Immutable operation identity (Finding 5).** Every GRANT, probe and ACL readback of one
handoff is bound to the exact 64-hex container **id** that Mode B verified; the mutable
Compose name is never an `exec` target. The id is re-inspected before the GRANT and again
after termination, and the name must still resolve to the same id. A replaced, removed,
stopped or renamed-away container fails closed: the intent stays pending and no GRANT is
sent to the replacement.

**SQL delivery (Alpine/BusyBox).** The pinned `postgres:16-alpine` wrapper reads stdin into
a private `/tmp` file **before** it backgrounds psql. An asynchronous list receives
`/dev/null` as its standard input under a non-interactive shell, so the former
`psql -f - <&0 &` form delivered zero bytes on that runtime. The file is removed on exit and
carries only the fixed, non-secret CONNECT statement for an allowlisted role.

**Termination before reconciliation (LCK-08/LCK-12).** A client timeout, interruption or
unobservable readback leaves the intent pending; `enroll.py status` reports it as UNKNOWN. A
later `handoff` invocation does **not** clear it from an instantaneous privilege snapshot,
and no Docker client return status (including `2`, `-15`, `124` or `125`) is ever read as
completion. The operation runs with a run-id-derived `application_name` and a bounded
container-side wrapper that records its own and its child's process ids, psql's exit status
and a start/stop marker under `/tmp`. The probe receives its tag in the environment, never
in its own argv, so it cannot match itself; it counts only the wrapper shell and `psql` as
live clients, not the BusyBox `timeout`/`env` transport supervisors. The reconciler reads
`pg_stat_activity` by `application_name`, checks those markers and probes for any surviving
tagged client. **Termination is established from the container-side client and tagged
session evidence before the complete ACL is read**; a snapshot taken earlier is never used
to decide the outcome. The intent is resolved only after:

* the tagged session and client are positively gone, and the wrapper recorded completion or
  was observed to have started and terminated; and
* the complete ACL is **exactly** the intent's recorded pre-state (the GRANT did not commit)
  or **exactly** the pre-state plus the one expected CONNECT (it committed).

An intent whose recorded pre-state already held CONNECT never issued a GRANT; it has no
operation to terminate and is resolved from the exact pre-state alone.

Anything else - including a still-live client, a never-started operation, `CONNECT WITH GRANT
OPTION`, a stray `CREATE`, or an unjournaled CONNECT on another role - keeps the intent
UNKNOWN and blocks every subsequent H1/H2/H3 transition. The journal is also validated as a
paired history: an outcome must follow exactly one intent with the same run id, role,
action, event and database; orphan outcomes, duplicate outcomes, duplicate operation ids and
cross-role pairings are journal corruption and are refused before provenance is derived.

**Client failure is not a server refusal (Finding 2).** With `ON_ERROR_STOP=1` and a script
whose only statement is the GRANT, psql exit `3` is a definitive server-side refusal; it is
recorded `failed` only when the complete readback still shows the pre-state. Exit `0` is
recorded `observed` only when the readback is the exact expected post-state. Connection loss
(`2`), Docker/client failures and signals are ambiguous. The pinned BusyBox `timeout`
returns **143** (128 + SIGTERM), not the GNU `124`; a timeout status never proves the SQL
statement stopped on the server. After termination is proven, the complete ACL decides
between `applied` and `unchanged`; otherwise the intent stays UNKNOWN and the privilege is
never claimed unchanged.

`enroll.py status` also reports any pending handoff. While one remains unresolved, `migrate`,
`maintenance.py backup` and `maintenance.py restore` refuse as well (LCK-08/LCK-13): only
`handoff` may reconcile it, from positive termination and the complete ACL. The same rule
applies to an unresolved **migration** (a `migrate` intent with no observed outcome): both
`backup` and `restore` refuse with `migration_unknown` (exit 5) until
`enroll.py reconcile-migration` records the resolution. An unresolved **lock handoff** (a
`handoff_lock` intent with no outcome) likewise blocks `mode-a`, the grant handoff,
`migrate`, `reconcile-migration`, backup and restore (`lock_handoff_unknown`, exit 5) until
`enroll.py lock-handoff` publishes an outcome; a corrupted or overlapping lock history is
journal corruption and is refused before any operation proceeds. `hh_purge` and `hh_backup`
are both part of the H3 surface; any future additional grantee requires the same separately
approved handoff design.

**One host operation interval (P0).** `mode-a`, `migrate`, `reconcile-migration`, `handoff`,
`maintenance.py backup` and `maintenance.py restore` all take the same exclusive,
non-blocking flock on `<journal-dir>/operation.lock`, and hold it from before Mode B until
their last state-changing step (backup: through archive publication and read-back; restore:
through the temporary database's verified drop; handoff: through the recorded outcome). A
competing operation is refused with `operation_busy` before it reads or changes anything, so
a handoff can neither begin inside a backup's window nor leave an UNKNOWN intent behind for
one. Under that interval an UNKNOWN handoff (`handoff_unknown`), an UNKNOWN migration
(`migration_unknown`) and an UNKNOWN lock handoff (`lock_handoff_unknown`) all refuse before
the compose parse, image inspection, Mode B, archive publication or any `CREATE DATABASE`.
The backup holder's state-volume locks are a second,
container-side layer; the holder is a **pure lock owner** with no output-directory mount, so
it cannot publish, replace or remove an archive even if it outlives its host. The one host
side exception is the lock handoff's **shared witness**: it opens the validated
`/state/.house-helper.lock` inode, takes `LOCK_SH|LOCK_NB`, and holds it through the journal
outcome fsync (HOF-03 item 4, below). It is shared, non-blocking and belongs to the host
process; a container holding a state-volume lock never waits for the operation interval or
the journal lock, so the wait-for graph still has no cycle. The **host process** publishes
the archive atomically while it still holds
the operation interval, and asks the holder to acknowledge ownership immediately before
publication and again after the read-back. The order is host interval, then state-volume
locks, and no acquisition blocks, so the locks cannot deadlock. An interrupted
process releases the interval when the kernel closes its descriptors, and that release never
resolves an unresolved database operation. **Both tools must name the same `--journal-dir`**
(or inherit the same `HOUSE_HELPER_PG_JOURNAL_DIR`): the interval is one file in that
directory, and a backup pointed at a different directory would take a different lock. An
explicit `--journal-dir` that resolves to a different directory than
`HOUSE_HELPER_PG_JOURNAL_DIR` is refused with `journal_mismatch` rather than silently taking
two disjoint locks; sameness is compared by `(st_dev, st_ino)` when both exist, not by the
text of the path.

For an isolated rehearsal, give every host tool the **same** journal directory and make the
environment agree with it:

```sh
export HOUSE_HELPER_PG_JOURNAL_DIR=/srv/hh-rehearsal/cutover
python deploy/postgres/enroll.py    --journal-dir /srv/hh-rehearsal/cutover ...
python deploy/postgres/maintenance.py --journal-dir /srv/hh-rehearsal/cutover ...
python deploy/offsite_backup.py run --postgres-journal-dir /srv/hh-rehearsal/cutover ...
```

Use the same `--compose-file`, `--credentials-dir` and `--object-contract` the deployed
tools use. `--compose-file` and `--object-contract` default to the installed layout;
`--credentials-dir` follows the shared resolver (explicit, then environment, then the
Compose default) and is normally passed explicitly. A rehearsal must not inherit a
production `HOUSE_HELPER_PG_JOURNAL_DIR`, because a `--journal-dir` that disagrees with it
is refused (`journal_mismatch`).

The `migrate` one-shot additionally requires the host tool's stdin channel
(`HOUSE_HELPER_PG_AUTHORIZATION_STDIN=1`) and a bounded Mode B record. A bare environment
flag is not enough: the executor validates the record, re-reads the live marker and
inventory, compares the artifact manifest with its own image's `db/*.sql`, and only then
takes the exclusive state-volume lock. A direct `docker compose run migrate` is refused.

The enrollment-time `schema` step uses the same stdin channel and a distinct bounded
schema authorization. The one-shot executes only `db/schema.sql`; the step itself consumes
the decoded record and compares its authorized deployment/database target with the live
E3-c catalog (no marker, no application relation, expected owner/encoding/locale, exact
database and role inventory, exact database ACL fence) before and after the artifact runs.

**Retained worker evidence (F10).** The migration worker is launched with a deterministic
name (`house-helper-pg-migrate-run-<run_id>`) and **without** `docker compose run --rm`.
A client timeout therefore leaves lifecycle evidence the reconciler can inspect
(`docker ps -a` plus `docker inspect`). Evidence is bound to the pending intent's run id;
the Compose project/service, exact name, full container id, terminal state and exit code
are all re-checked by the resolver. A confirmed terminal worker is removed with exactly
`docker rm <container-id>` only after the migrate outcome is durably journaled (or the
reconciliation record appended). Running, UNKNOWN, unrecognized or identity-mismatched
containers are never removed, and no prune/global cleanup/`--remove-orphans` is used. A
cleanup failure is reported as `warning [migrate-cleanup]`: the recorded migration outcome
is not reclassified as UNKNOWN. A refused or failed worker is retained for investigation;
an operator removes it only after confirming the exact run-specific name and an
`exited`/`dead` state.

**The lock handoff (HOF-03 item 4).** `enroll.py lock-handoff --approved-by <owner>` is the
journaled observation that the web process holds the shared state-volume lock and no
exclusive holder remains. It selects the project's one running web container by its Compose
labels and validates its `/state` mount against the recorded state volume. It then runs the
frozen `dbtool check-idle` inside that exact container id (it must be refused with
`maintenance_lock_busy`), and on the host it reads the kernel's lock ownership twice through
`deploy/postgres/lock_owner.py`.

The holder must be the **deployed image's real process**, not a guessed command string.
`dbtool.py serve` replaces itself with the `uvicorn` console script, and the kernel rewrites
a `#!` script's argv, so the live `/proc/<pid>/cmdline` is
`[/opt/venv/bin/python, /opt/venv/bin/uvicorn, house_helper.web.asgi:app, ...]`. The proof
accepts that form (and `[uvicorn, app]` / `[python, -m, uvicorn, app]`), and requires the
process's cgroup to name the immutable container id and one of its own descriptors to carry
a `FLOCK ... READ` record. The fdinfo record's own device/inode must equal the validated
lock file's: a descriptor that stat'ed as the lock file but whose record names another
inode was closed and reused, and is refused as indeterminate. No temporary shared
acquisition is taken, so a probe's own lock can never be published as the web process's.

Both ownership snapshots must agree. Between them the command runs `check-idle`, then takes
a **host-held shared witness** on the same validated inode. The witness is retained across
the final snapshot, a fresh verification of the project's current running web container
(exact immutable id), the journal outcome append and its fsync. An exclusive holder cannot
acquire in that interval; the witness itself is never web-ownership evidence (it belongs to
the host, outside the container cgroup). The recorded interval is exactly
`final_verification_through_publication`: ownership is attested at the two snapshots, and no
exclusive holder can exist from witness acquisition through the fsync. Nothing about the
web process after the fsync is claimed.

A determinate negative is a `failed` outcome; an indeterminate, contradictory, changing or
disappearing observation appends no outcome and leaves the intent UNKNOWN. Only the coherent
positive observation is journaled as an `observed` `handoff_lock` pair (the frozen
`handoff:lock` event). The **latest** outcome is the state, and every re-observation first
appends a new intent: a repeated confirmation of the same container is recorded as a fresh
`observed` outcome in that new epoch, a replacement container is observed in it, and a
later failed re-observation supersedes an earlier observed one so it can no longer
authorize a re-arm. Because the intent is durable before the observation, an indeterminate
or interrupted re-observation leaves a pending intent instead of the previous `observed`
state. An unresolved intent still fences every later transition (exit 5).

Every host command that resolves this surface takes `--credentials-dir` explicitly, because
an ordinary `sudo` does not carry `HOUSE_HELPER_PG_CREDENTIALS_DIR`. All path-valued
configuration follows one rule: a relative value from an option, the environment or the
Compose `${VAR:-default}` is resolved against the compose file's directory, canonicalised
(symlinks, case) and compared by identity. A genuine disagreement is `credentials_conflict`.
`maintenance.py` uses the same resolver and has no repository default of its own.

## 8. What WP-2C-A did not do

* No live cluster was initialized, no role/database was created, no schema was migrated and
  no privilege was applied. The executor was validated offline against controlled fixtures.
* No credential was generated, populated, rotated or disclosed. The bootstrap password
  retirement is a printed plan; it was not executed.
* No PostgreSQL backup/restore in WP-2C-A; WP-2D now implements the core backup,
  target-cluster restore, isolated rehearsal and format-v2 archive. The offsite
  `age`/`rclone` wrapper and the v1/v2 rehearsal dispatcher remain WP-2E.
* No timer re-arm, systemd change, DSN switch, container identity update (ENR-18) or
  Production access (WP-2F).
* No change to `deploy/compose.postgres.yaml`, the SQLite stack, the existing systemd units,
  `db/*.sql`, `deploy/ops/dbtool.py`, `.claude/` or `deploy/monitoring/`.

## 9. Next packages

* **WP-2D (implemented, repository only)** - PostgreSQL backup/restore, archive v2,
  isolated restore and the sequence contract. See [`backup_restore.md`](backup_restore.md).
* **WP-2E** - the offsite `age`/`rclone` v2 path, the v1/v2 rehearsal dispatcher and the
  remaining failure-injection matrix.
* **WP-2F** - scheduled operations, timer re-arm, DSN switch, container identity updates and
  the Production cutover.

The executor's live obligations still need the separately approved isolated PostgreSQL
environment: a real `hh_migrate` login and passfile, `alembic upgrade head` through
SQLAlchemy/psycopg, `db/schema.sql`/`db/privileges.sql` execution and `alembic check`, the
state-volume lock, and the full §5 fence/ACL assertions.
