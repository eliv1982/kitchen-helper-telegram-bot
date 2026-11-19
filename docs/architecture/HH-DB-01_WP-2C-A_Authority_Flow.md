# HH-DB-01 Stage 2 WP-2C-A - Enrollment Authority Flow (DRAFT)

**Status: offline implementation evidence. Nothing is deployed.** This document specifies
the execution and trust flow implemented in `deploy/postgres/enroll.py`,
`house_helper/persistence/pg_enrollment.py`, `pg_journal.py` and `pg_executor.py`. It does
not amend the Frozen Specification; where the two differ, the Frozen Specification wins.

## 1. Actors and trust

| Actor | Authority | Never holds |
|---|---|---|
| Owner/operator (root, host) | Docker socket, journal directory, deployment configuration | a database credential in an argument, URL, log or journal record |
| `enroll.py` host tool | Runs as root; reads/writes the journal; invokes the local peer `psql` as the bootstrap superuser; invokes Compose | the web app's or a runtime role's password; a network discovery path |
| PostgreSQL container | Local socket only for the bootstrap superuser; SCRAM for project logins on the database network | a Docker socket; the host journal |
| `migrate` one-shot | `hh_migrate` passfile, database network, `/state` lock volume, bounded stdin | Docker socket, host journal, any other role's passfile |
| Web application | `hh_app` passfile, database network, `/state` shared lock | Docker socket, host journal, the migrate authorization channel |

Boundary B4 (host root ↔ Docker) is out of scope in the Frozen Specification. The controls
below are fail-closed protections against mistake and stale/foreign state, not a claim that
a host-root actor cannot modify a host-root file.

## 2. Mode A - enrollment

1. **Docker identity.** `docker inspect` the exact recorded container name, volume and
   image. Require: one object; exact full container id; running; `Config.Image` equal to
   the digest-pinned PostgreSQL reference; Compose project/service labels; exactly one
   writable volume at `/var/lib/postgresql/data` named `house-helper-pgdata`; no other
   mount except the two read-only configuration binds; no published/bound port; membership
   of `house-helper-pg-db`; the volume attached only to this container; a local-scope
   volume; a pinned image reference.
2. **`system_identifier`.** `docker exec -u postgres ... psql` on the local socket runs
   `pg_control_system()`. No TCP, TLS, or discovered endpoint is used.
3. **Pre-intent freshness (E0 only).** Databases are a subset of
   `{postgres, template0, template1}`; no `hh_*` role; no `house_helper`; no marker.
   Anything else fails closed and never becomes an intent.
4. **Intent.** Append `cluster_enrollment:intent` with the observed container id/name,
   image digest, volume, `system_identifier`, approved role/database inventory, observed
   predefined-role set and a fresh non-secret marker token. The journal is root-owned
   `0700`/`0600`, outside PGDATA, append-only and digest-chained; a separate `journal.lock`
   serializes writers.
5. **Roles.** `db/roles.sql` in one transaction as the bootstrap superuser. Existing roles
   must already match the frozen matrix; a drifted role is refused, never repaired.
6. **Database (E3).** `db/bootstrap.sql` phases: create with `ALLOW_CONNECTIONS false`;
   revoke PUBLIC and grant `hh_migrate`; verify disabled/hardened; only then enable; verify
   enabled and no PUBLIC entry.
7. **Schema/default ACLs (ENR-04, before the marker).** With E3-c complete, the host runs
   the unchanged `db/schema.sql` through the accepted `migrate` one-shot service
   (`docker compose run ... migrate schema`) over an explicitly opened, bounded
   schema-only authorization. That session is an authenticated `hh_migrate` login whose
   accepted role default is `current_user = hh_owner` (ROL-03/ROL-06); the bootstrap
   superuser's local-peer session is never used to impersonate it. The step executes the
   frozen artifact only - never Alembic, arbitrary SQL or a second role's credentials -
   holds the state-volume exclusive lock, and refuses if the database already has an
   application relation or a revision row. Its success is verified independently from the
   catalog (schema owner/ACL, the exact ROL-14 default ACL matrix, no relation) before
   the `hardened` milestone is appended. Failure leaves the journal at `database`: the
   enrollment stays resumable and is not E7.

   The authorization is issued only after the host re-observes the container name/id,
   image digest, volume and `system_identifier` and proves they are still the recorded
   intent's; the worker consumes the record instead of discarding it. Before and after
   the artifact it compares the authorized deployment/database target with the live E3-c
   state: absent marker, expected owner/encoding/locale, exact base-plus-serving database
   inventory, role/membership/predefined inventories, exact database ACL fence, no
   application relation and the accepted artifact manifest. The physical
   `system_identifier` is not readable by a project login, so it remains a host-side
   binding, not a worker claim.
8. **Marker.** `COMMENT ON DATABASE house_helper IS '<canonical JSON>'` with the token and
   `system_identifier`. A marker observed without the schema/default-ACL half is refused.
9. **Completion.** Re-observe roles, attributes, explicit `pg_auth_members` edges,
   effective `pg_has_role` MEMBER/USAGE/SET matrix in the serving database, database
   owner/encoding/locale/ACL, the ROL-14 default ACLs, marker, predefined-role set;
   append `cluster_enrollment:completed`. Database-level hardening alone is never
   sufficient for E7.

**Credential dependency (reported, not created).** The schema step authenticates as
`hh_migrate` with its per-role passfile. OD-8 is the Owner's open decision; this work
package generates and populates no credential. Until the passfile exists, Mode A stops
after E3-c with the `database` milestone durable and resumes exactly at the schema step.

Resume (E1-E6) is the same intent. A subset of in-shape objects is resumed; an extra
object, a role with different attributes, a different `system_identifier` or a changed
container/volume/image is refused. An observed milestone the journal has not recorded is
written as `reconcile`; the observation is never inferred from the absence of a record. A
journal that is ahead of the observation is refused.

## 3. Mode B - authoritative verification

Require exactly one completed record. Validate the digest chain and head anchor. Compare:

* container name, full id, image digest, volume and `system_identifier`;
* the live marker (parse both the token and the embedded identifier) with the record;
* project role attributes, session defaults, explicit membership edges and effective
  `pg_has_role` results;
* database inventory (base plus `house_helper`, nothing else), owner, encoding, locale;
* predefined-role set, the frozen predefined-to-predefined membership baseline
  (`pg_monitor` -> `pg_read_all_settings` / `pg_read_all_stats` /
  `pg_stat_scan_tables`), and no project-role edge to a predefined role;
* every role must be the bootstrap superuser, one of the seven project roles, or a member
  of the frozen predefined set (`unrelated_admin`, a planted `pg_*` name and any other
  foreign role are refused);
* the state-specific target inventory described in §5.1.

Any difference appends `cluster_identity:mismatch` naming the item (never a value) and
returns a refusal. **There is no Mode A fallback.**

## 4. Mode B → migrate binding

After a successful Mode B, the host tool builds a bounded canonical JSON record containing:

* `role=migrate`, `mode=B`, `verdict=verified`, a random `run_id`, `issued_at` and a
  15-minute `not_after`;
* the journal sequence/head it was derived from;
* the recorded cluster facts, marker, database and role/predefined inventories;
* the SHA-256 manifest of `db/roles.sql`, `bootstrap.sql`, `schema.sql`, `privileges.sql`
  from the host checkout.

It invokes

```
docker compose ... --profile ops run -T \
  --name house-helper-pg-migrate-run-<run_id> \
  -e HOUSE_HELPER_PG_AUTHORIZATION_STDIN=1 migrate
```

The migration worker is deliberately launched **without** `--rm`. Its deterministic
run-specific name and retained container are the F10 reconciler evidence; the schema step
is a separate bounded one-shot and keeps its own lifecycle.

and writes the record to the container's stdin. It is never on argv, in an environment
dump, in the journal, or in a file the web application can read.

**One host operation interval.** `enroll.py migrate` holds a host-side operation lock
(`<journal-dir>/operation.lock`) from before Mode B verification until the worker has
finished and a final identity observation has passed. Every supported host
identity-changing operation (`mode-a`, `migrate`, `reconcile-migration`, `handoff`, and
`maintenance.py backup`/`restore`) takes the same lock - maintenance holds it from before
its own Mode B until the archive is published and read back (backup) or the temporary
database is verified and dropped (restore). Read-only `verify` does not take it and
therefore still creates no journal on a
host that has never enrolled. This closes the previous check/use gap
between host Mode B and the worker's own lock acquisition, and the equivalent gap between
the maintenance target check and a concurrent handoff. It is deliberately a *different*
lock file from the worker's exclusive `/state/.house-helper.lock`: acquiring that
non-reentrant flock on both sides of the container boundary could deadlock. Journal
appends keep their own `journal.lock` inside the interval; the two locks are never nested
in reverse, and the container-side state locks come after both (lock order: operation,
journal, state volume).

**Maintenance refuses every UNKNOWN under that interval.** Before the compose parse, the
image inspection, Mode B, archive publication or any `CREATE DATABASE`, `backup` and
`restore` read the journal while holding `operation.lock`. An unresolved handoff refuses
with `handoff_unknown`; an unresolved migration (a `migrate` intent with no observed
outcome) refuses with `migration_unknown`. The migration reconciler is unaffected: it
takes the same interval itself and does not pass through this target check, so it can
always observe the cluster and record the resolution. A handoff or migration that becomes
UNKNOWN in a successor process can therefore never be bypassed by a surviving backup.

**The publication authority and the publication are the same process.** The detached
holder (a network-less disposable container) owns the two state-volume flocks as a second
layer, but it has no output-directory mount and no publication branch. It can only signal
readiness and answer an ownership probe. The host process publishes the archive atomically
while it still holds `operation.lock`, and probes the holder immediately before the
publication and again after the read-back. A holder that outlives its host is therefore
inert: it cannot create, replace or remove an archive. This replaces the earlier design in
which the holder itself published, which allowed a holder orphaned by a killed host to
publish after a successor had recorded an UNKNOWN.

The one-shot refuses unless the channel was explicitly opened and stdin is not a TTY. The
record is validated structurally and its lifetime bounded. The executor then:

1. takes the exclusive `/state/.house-helper.lock` (the web's shared lock blocks it);
2. connects as `hh_migrate`, which `db/roles.sql` makes `current_user = hh_owner`; sets and
   reads back `search_path=public`, `application_name=hh_migrate`,
   `statement_timeout=0`;
3. compares the artifact manifest with its own `/app/db` files - a record produced from
   different SQL is refused;
4. re-reads the live marker, the marker's `system_identifier`, the role/database/predefined
   inventories, the database ACL and the absence of other client sessions;
5. dispatches fresh vs upgrade from the observed schema state;
6. runs the ordered operations and **unconditionally re-applies `db/privileges.sql`**;
7. verifies head revision, `alembic check`, `hh_owner` ownership and the fence;
8. releases the lock (kernel release on process exit, so it is never stale).

Container name/id, image digest and the PGDATA volume are host-level facts. The executor
cannot see Docker from inside the application container and does not pretend otherwise;
it binds the run to the database-observable identity and to the exact SQL bytes. A direct
`docker compose run migrate` has no authorization channel and exits `3` before connecting.

## 5. Failure, retry and resume

| Failure | Result |
|---|---|
| Mode A: non-pristine before intent | Refused; no journal record, no mutation. |
| Mode A: Docker/volume/image mismatch | Refused before any SQL. |
| Mode A: identifier or attribute drift after intent | Refused; never auto-repaired. |
| Mode A: interrupted role/database/marker step | Resume the same intent from the observed state; record `reconcile` when the observation is ahead. |
| Mode A: `db/schema.sql` refused, failed or interrupted | No `hardened` milestone, no marker, not E7. The database remains at E3-c and a later run resumes the schema step; `db/schema.sql` is idempotent. |
| Mode A: marker observed without the schema/default-ACL half | Refused (`marker:present_without_schema`); never completed. |
| Mode B: any mismatch | Journaled mismatch, refusal, no fallback. |
| Migrate: missing/expired/invalid authorization | Exit `3` before connecting. |
| Migrate: live identity/inventory/artifact mismatch | Exit `3` before any DDL. |
| Migrate: unknown/ahead revision or foreign tables | Exit `3`; nothing runs. |
| Migrate: fresh chain interrupted | PostgreSQL transactional DDL leaves zero application tables; re-run is fresh. |
| Migrate: privileges re-application fails | The transaction rolls back; exit `3`; no success is reported. |
| Migrate: one-shot exits non-zero | Host appends `migration:outcome` with the exit code; a new attempt requires a new Mode B. |
| Migrate: client times out or is interrupted after launch | The outcome is **UNKNOWN** (`5`), never `not_started`; the client ending does not prove the worker stopped. A later `migrate` refuses until `reconcile-migration` positively establishes the worker's termination. |
| Migrate: Docker CLI fails before launch | The outcome is `not_started` (confirmed, not inferred). |
| Migrate: a container already holds this run's deterministic name | Launch refused before Compose; outcome `not_started` (`worker_name_conflict`). The container is never reused or removed. |
| Migrate: worker completed and verified | The exact terminal container is removed only after the outcome and final observation are durable; a cleanup failure is a warning, not UNKNOWN. |
| Migrate: cluster identity changes during the interval | The final Mode B observation fails closed, a `cluster_identity:mismatch` is appended, and the run is not reported as success. |
| `reconcile-migration`: no positively terminated worker (running/created container, no container observed, or missing exit code) | Refused; the UNKNOWN remains. An unchanged revision is never accepted as proof on its own. |
| `reconcile-migration`: evidence from another run, another service/project, a wrong container name or a mismatched container identity | Refused; the UNKNOWN remains. The resolver re-checks the binding itself and does not trust a pre-filtered adapter answer. |
| `reconcile-migration`: observation is not the enrolled cluster (full identity/inventory) | Refused; the UNKNOWN remains. |
| `reconcile-migration`: worker termination observed but the revision is neither the pre-migration revision nor the image head | Refused; the UNKNOWN remains and must be investigated. |

**Worker evidence and retention (F10).** The migration worker is launched with the
deterministic name `house-helper-pg-migrate-run-<run_id>` and **without**
`docker compose run --rm`, so a worker that exits leaves its container available for
inspection after a client timeout. The reconciler re-derives the expected name from the
pending intent's run id and accepts termination only when the resolver itself confirms:

* `evidence.run_id` is the pending intent's run id;
* the evidence names the exact deterministic container;
* the observed container carries the frozen Compose project and `migrate` service;
* its full container id is well formed, its lifecycle is `exited`/`dead` (not
  running/created/restarting), and its terminal exit code was observed.

Evidence from another run, service or project, a container under another name, or an
absent/unidentifiable container never releases UNKNOWN. A retained container from an old
run is ignored rather than mistaken for the current worker, and a container already
holding the new run's deterministic name is refused before Compose is invoked. A missing
container remains ambiguous - never created - and is not proof; no worker-side lifecycle
file or other new evidence channel is fabricated.

After the outcome is durably journaled (and, for success, after the final Mode B
observation), the host removes exactly the confirmed terminal worker with
`docker rm <container-id>`. The removal path re-inspects the container and refuses a
running, UNKNOWN, unrecognized or identity-mismatched one; it uses no `-f`, no prune and no
`--remove-orphans`. A cleanup failure is reported as `warning [migrate-cleanup]` and does
not reclassify a known successful outcome as UNKNOWN. Failed or refused workers are
retained for operator investigation.

### 5.1 Schema-state inventory (Mode B and the executor)

Mode B no longer treats every target object as foreign. It classifies the observed schema
state and authorizes exactly what that state may contain:

| State | Observed | Authorized target inventory |
|---|---|---|
| Initial enrollment | no `alembic_version` row, no application table | empty |
| Recognized partial initialization | no revision row, no application table, `pg_default_acl` exactly ROL-14 | the default-ACL objects only |
| Completed initialization | exactly the image head, every model table | the image's tables/indexes/sequences, derived `pg_toast` storage and the exact ROL-14 default ACLs |
| Upgrade required | one known ancestor of the head, a table subset | the same authorized object set |
| Retry at the head | the completed state | the same authorized object set |

Anything else - a foreign table/object, an unknown or off-chain revision, a missing table
at the head, a default-ACL recipient outside ROL-14 - still fails closed. The executor
applies the same rule before and after the migration; `db/schema.sql` and
`db/privileges.sql` remain the byte-for-byte source of the schema and default ACLs and are
executed with SQLAlchemy's `no_parameters` option because they contain literal `%`.

## 6. Credential handling

* The host Mode A/B path uses only the local socket as `postgres`; no password.
* Each project role has its own `PGPASSFILE` (mode `0600`, one service).
  `pg_credentials.py` validates kind, mode, owner/group, login and host/database entry
  without returning or printing a field value. **Host validation requires the container
  runtime identity `10001:10001`** (the image's `USER`), never the host root process's uid;
  inside a container the same check uses the current process identity. The bootstrap
  `postgres_superuser.password` (`0:0`) and the root-owned journal are separate ownership
  contracts and are not checked with the runtime identity.
* `PGPASSWORD`, `PGSERVICE`, `PGSERVICEFILE` and `PGSSLMODE` are refused (the last because
  OD-6 is open).
* No credential is generated or populated. OD-8 remains open.
* The bootstrap password retirement is a printed six-step plan
  (`enroll.py plan-bootstrap-retirement`): Mode B verification, journaled intent,
  `ALTER ROLE postgres PASSWORD NULL` over the local socket, verifier/remote-peer
  verification, removal of `POSTGRES_PASSWORD_FILE` and the host file, re-verification.
  It was not executed against any environment.
