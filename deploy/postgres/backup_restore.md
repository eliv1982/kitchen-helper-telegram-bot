# PostgreSQL backup and restore (HH-DB-01 Stage 2 WP-2D)

**Status: repository implementation only. Nothing here is deployed.** Production remains
on SQLite. No production credential was generated, no Production database was touched, no
DSN was switched, no timer was re-armed and no backup was uploaded off-host. The only
executed evidence is the isolated PostgreSQL 16.15 rehearsal recorded in the WP-2D final
report.

This document is the operator procedure for the WP-2D host tool. It is governed by
`docs/architecture/HH-DB-01_Architecture_Frozen_Specification_v1.0.md` §8; where this
text and the Frozen Specification differ, the specification wins.

## 1. What exists

| Component | Purpose |
|---|---|
| `deploy/postgres/maintenance.py` | Root host tool: Mode B verification, backup, archive verification, target-cluster restore, isolated rehearsal. It launches disposable `postgres:16.x` client containers (BAK-13). |
| `house_helper/persistence/pg_backup.py` | Pure format-v2 contract: frozen dump flags, manifest, table digests, scoped `SEQUENCE SET` extraction, sequence invariant, archive reader/writer, normalized object contract. |
| `tests/fixtures/pg_object_contract.json` | The normalized object contract generated from a fresh, disposable run of the real migration chain at `0014_user_measure_overrides` (never from an existing cluster). A new migration deliberately regenerates it with `maintenance.py write-object-contract-fixture --output tests/fixtures/pg_object_contract.json`; a hand edit is a review event. |
| `tests/test_pg_backup.py` | Offline contract tests (no Docker). |
| `tests/test_pg_backup_tool.py` | Offline host-tool boundary tests. |

The Compose `backup` and `restore` services remain fail-closed app-image one-shots. They
are **not** the implementation: the application image intentionally contains no
`pg_dump`/`pg_restore` (BAK-13). Do not repurpose them by adding client binaries.

## 2. Prerequisites

### 2.1 Cluster and role state

Mode B must pass (`enroll.py verify`). In addition:

* `hh_backup` must have `CONNECT` on `house_helper` (the H3 handoff, HOF-01). Before H3,
  backup correctly refuses at the connection. H3 is required before the first PostgreSQL
  backup of any kind, including a manual run; a backup is never an implicit handoff.
* `hh_backup` holds `SELECT` on every table and sequence and on `alembic_version`
  (ROL-12), and no `pg_read_all_data`.
* `hh_restore` holds `CREATEDB` and the `pg_database` `CONNECT` grant on the chosen
  maintenance database (RST-03). It holds **no** `CONNECT` on `house_helper`.
* No runtime role holds `pg_signal_backend`.

The host credential directory contains one mode-`0600` passfile per role, owned by the
container runtime identity `10001:10001`, exactly as the WP-2B surface requires:

```
<credentials-dir>/hh_backup.pgpass    house-helper-db:5432:house_helper:hh_backup:<secret>
<credentials-dir>/hh_restore.pgpass   house-helper-db:5432:*:hh_restore:<secret>
```

No credential is printed by the tool. The passfile is mounted read-only into exactly one
disposable container, copied to a private `/tmp` path inside it, and deleted with the
container.

### 2.2 OD-2 maintenance database (Owner choice, still open)

`restore --maintenance-database` is **required**. The tool never selects OD-2. Two
arrangements are permitted by the specification; apply exactly one, as the bootstrap
superuser on the maintenance database, before the first restore:

```sql
-- Option A (the recommendation, not an approval): the postgres maintenance database.
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
GRANT CONNECT ON DATABASE postgres TO hh_restore;
```

```sql
-- Option B: a dedicated empty maintenance database owned by hh_owner.
CREATE DATABASE house_helper_admin OWNER hh_owner TEMPLATE template0
    ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C';
REVOKE ALL ON DATABASE house_helper_admin FROM PUBLIC;
GRANT CONNECT ON DATABASE house_helper_admin TO hh_restore;
```

The tool's preflight requires: the selected maintenance database is connectable, its ACL
is materialized, `PUBLIC` holds nothing, `hh_restore` holds exactly `CONNECT` (no
`TEMPORARY`), no other runtime role holds anything, and `hh_restore` has exactly
`CREATEDB`. A failure is a refusal before any database is created.

## 3. Backup

```sh
python deploy/postgres/maintenance.py \
  --journal-dir /var/lib/house-helper/cutover \
  --credentials-dir /root/house-helper-credentials \
  backup --output /var/backups/house-helper/house-helper-<UTC>.tar
```

The output is a **format-v2 recovery point** (`house-helper-recovery-point/2`): a tar whose
members are, in order, `MANIFEST.json`, `snapshot.dump.sha256`, `snapshot.dump`. The
offsite tool (`deploy/offsite_backup.py --database postgresql`, WP-2E) builds this tar with
this same tool, re-validates it with `verify-archive`, encrypts it with `age` and uploads it;
`backup_restore.md` does not upload, and the offsite path is not activated.

What the tool does, in order:

0. Takes the **host operation interval** - the exclusive, non-blocking `flock` on
   `<journal-dir>/operation.lock` that `enroll.py` mode-a, migrate, reconcile-migration
   and handoff also take - before Mode B, and holds it until the archive has been
   published and read back. A handoff, migration or second backup/restore that is already
   running makes the run refuse (`operation_busy`) before anything is read, dumped or
   written; a handoff that never started cannot begin while the backup holds the
   interval. An UNKNOWN handoff (`handoff_unknown`), an UNKNOWN migration
   (`migration_unknown`) or an UNKNOWN lock handoff (`lock_handoff_unknown`) refuses the
   run before any state-changing step. All three UNKNOWN states are evaluated under this
   same interval.
1. Takes a **shared** `flock` on `/state/.house-helper.lock` and an independent
   **exclusive** `flock` on `/state/.house-helper.backup.lock` (LCK-01), in a detached
   holder that keeps both locks until the whole backup has finished. A migration's
   exclusive maintenance holder or a second backup makes the run refuse before any
   statement. These container-side locks are the second layer, and the host never takes
   them; the order is always host interval, then state-volume locks. They cover the
   dump, the facts, the manifest, the output existence checks, archive publication and
   its read-back; they are released by removing the holder (the kernel closes the
   descriptors) on success or failure. The holder is a **pure lock owner**: it has no
   output-directory mount and no publication branch, so it cannot publish, replace or
   remove an archive even if it outlives its host. It answers one ownership probe. The
   **host process** performs the final atomic publication while it still holds the
   operation interval, and asks the holder to acknowledge ownership immediately before
   publication and again after the read-back.
2. Verifies Mode B against the enrolled record (ENR-15..ENR-17). No fallback to Mode A.
3. Validates the `hh_backup` passfile and the pinned image digest.
4. In one disposable client container, opens `REPEATABLE READ`, exports a snapshot,
   runs `pg_dump --snapshot=<id>` **inside that transaction** with the frozen flags
   (`--format=custom --compress=zstd --schema=public --encoding=UTF8 --no-owner
   --no-privileges --no-tablespaces --no-subscriptions --no-publications
   --no-large-objects`), computes every table count/digest in the same transaction, and
   commits. A post-dump count would be a different dataset and is never used.
5. Refuses non-empty `pg_largeobject_metadata`, any non-system schema other than
   `public`, and any subscription.
6. Lists the archive TOC, requires exactly one `SEQUENCE SET` entry for
   `public.messages_id_seq`, extracts only that entry with `--use-list`, and parses the
   result as **data** under the strict SEQ-05 envelope. It never executes the extraction
   and never reads the sequence from the live database (SEQ-01).
7. Builds and validates the manifest: server/dump versions, provenance
   `system_identifier`, revision, encoding/locale, the exact seven roles, the ACL
   artifact digest, every table's `{rows,digest}`, the raw sequence pair, the dump's
   byte count and SHA-256, and the pinned dump settings.
8. Writes the tar to a private staging file in the output directory and reads it back;
   only then does the host publish it atomically, while the operation interval and the
   holder's ownership are both confirmed. With the default
   `overwrite=false` publication is an atomic same-filesystem **hard link**, which fails
   if the final path exists; an existing file is never replaced, even one that appears
   in the instant before publication. `--overwrite` is the explicit authorization to
   replace it. The published archive is then read back and the lock holder acknowledges
   ownership; a holder that terminated before that acknowledgement makes the run fail and
   the host removes the exact inode it published. A failure leaves no output and removes
   the staging file. A missing dump or success marker is a failure; no archive is reported.

The manifest's `system_identifier` is **provenance** ("this archive came from that
cluster"), never a restore precondition. A replacement cluster has a new identifier and
requires its own Mode A enrollment (ENR-17).

### 3.1 Verify an archive without a database

```sh
python deploy/postgres/maintenance.py verify-archive /var/backups/.../point.tar
```

This validates the member set/order, canonical manifest, checksum, dump size/hash, the
TOC, exactly one `SEQUENCE SET` entry, **the extracted `SEQUENCE SET` payload parsed as
data and compared with the manifest's raw pair**, and **exactly one `TABLE DATA` TOC
entry for every table of the frozen object contract**. A missing, duplicated or
unexpected `TABLE DATA` entry, extra sequence SQL, or a sequence pair that differs from
the manifest is refused before anything is restored. The check does not rely on TOC
metadata alone.

## 4. Restore into the existing instance (RST-04, level 4)

```sh
python deploy/postgres/maintenance.py \
  --journal-dir /var/lib/house-helper/cutover \
  --credentials-dir /root/house-helper-credentials \
  restore /var/backups/.../point.tar \
  --maintenance-database postgres
```

The ordered flow:

0. Takes the **host operation interval** (`<journal-dir>/operation.lock`), exactly as
   `backup` does, and holds it from before the target check until the temporary database
   has been verified and dropped (or retained). A concurrent handoff, migration, backup
   or restore is refused (`operation_busy`) before the archive is read or a database is
   created; an UNKNOWN handoff (`handoff_unknown`), an UNKNOWN migration
   (`migration_unknown`) or an UNKNOWN lock handoff (`lock_handoff_unknown`) is refused
   before anything changes.
1. Validates the archive (level 1) and Mode B on the target. The archive validation
   includes the extracted sequence payload and the `TABLE DATA` inventory; an invalid
   archive is refused **before** any target database is created.
2. Preflights the maintenance database and the `hh_restore` passfile, and reconciles
   every active temporary-database record (TMP-01/TMP-04).
3. Appends `temp_db:authorized` with the exact database name, operation/token, purpose,
   cluster identifier, expected owner/privilege and archive provenance.
4. As `hh_restore`, `CREATE DATABASE house_helper_restore_<token> OWNER hh_owner TEMPLATE
   template0 ENCODING 'UTF8' LC_COLLATE '<manifest>' LC_CTYPE '<manifest>'
   ALLOW_CONNECTIONS false`, then verifies the owner/encoding/locale/fence. A real
   `hh_restore` connection probe is **refused** while the database is disabled.
5. `SET ROLE hh_owner`; `REVOKE ALL ... FROM PUBLIC`; `GRANT CONNECT ... TO hh_restore`.
   The ACL is re-read while the database is **still disabled** and must contain exactly
   the owner's implicit rights and `hh_restore=CONNECT`; the connection probe still
   fails. Only then is `temp_db:created_fenced` appended. The `ALTER DATABASE ...
   ALLOW_CONNECTIONS true` step follows, the ACL is re-read and the probe must now
   succeed, and `temp_db:restoring` is appended. The pre-hardening window is closed to
   ordinary clients (RST-05), and each journal state is written only after that state
   has actually been established.
6. Runs `pg_restore --exit-on-error --role=hh_owner --no-owner --no-privileges` with a
   TOC list that omits only the archive's `CREATE SCHEMA public` entry (the new database
   already carries `public`; `db/schema.sql` hardens it afterwards).
7. Applies `db/schema.sql` and `db/privileges.sql` as `hh_restore` with
   `SET ROLE hh_owner`, each in one transaction.
8. Verifies before `verified`:
   * `alembic_version` equals the manifest;
   * every table's count and canonical digest equals the manifest;
   * no `NOT VALID` constraints;
   * every table/index/sequence is owned by `hh_owner`, `public` is owned by `hh_owner`
     with no `PUBLIC` entry, and the default-ACL matrix equals ROL-14;
   * the restored raw sequence pair equals the archive pair exactly, and
     `next > max(messages.id)`, within `[seqmin,seqmax]`, `seqincrement=1`,
     `seqcycle=false`, owned by `messages.id`;
   * the normalized object contract equals the checked-in fixture (constraint deparse is
     normalized for the `pg_restore` re-parse, RSK-04);
   * `alembic check` reports no drift in the restored database.
9. If any check fails, the record is **never** advanced to `verified`; the database is
   left for inspection and the run fails.
10. Runs the isolated rehearsal (§5) by default.
11. `temp_db:cleanup_pending`; `DROP DATABASE` as `hh_owner` (never `WITH (FORCE)`);
    re-reads the catalog to observe absence; `temp_db:retired`.

`--keep-database` leaves a verified database in place only for an explicit, reviewed
Owner recovery decision. `--no-rehearse` skips the disposable verification (the target
restore is still fully verified); use it only when the disposable Docker path is
deliberately unavailable.

## 5. Isolated rehearsal (`--network none`, levels 2/3)

`restore` runs this after a successful target restore; it can also be run alone:

```sh
python deploy/postgres/maintenance.py rehearse /var/backups/.../point.tar
```

The rehearsal starts a throwaway `postgres:16.15` container with:

* `--network none`;
* tmpfs `PGDATA`;
* **no production volume**, **no production credential**, no published port;
* a fresh `system_identifier`, recorded as provenance, never as a requirement.

Inside it the tool applies `db/roles.sql`, creates a database behind the same
`ALLOW_CONNECTIONS false` fence, restores with `--role=hh_owner --no-owner
--no-privileges`, applies `db/schema.sql` and `db/privileges.sql`, and repeats every
verification of §4.8. It then:

* proves the attachment payloads: the exact byte total (`sum(octet_length(data))`) must
  equal the declared `sum(size_bytes)`, and at least one attachment is read through the
  application's own loader (`house_helper.persistence.attachments.get_for_user`), whose
  bytes must match the stored `sha256`/`size_bytes`. An empty attachment set is reported
  as such (`loader sample none`), never as a passed nonempty sample. The stored row is
  part of `message_attachments`, whose logical digest is already compared with the
  archive manifest. The proof answers with exactly one framed stdout record,
  `HH-ATTACHMENT-PROOF <per-run token> <JSON>`, carrying a token generated by the tool
  for that invocation, and with a status of its own: 0 for a completed proof and 3 for a
  deliberate verification refusal. Only that record, with this run's token, the exact
  fields, an allowlisted refusal reason and an agreeing status is artifact evidence.
  Every other stdout byte (a dependency's diagnostics, a JSON-looking line) is ignored;
  an uncaught exception, a signal, a Docker failure, a missing or duplicate record, an
  unknown reason or a contradicted verdict is `attachment_proof_environment` (exit 4),
  never NOT RESTORABLE;
* runs `alembic check` from the application image in a container sharing the isolated
  container's network namespace (no external route), with `current_user = hh_owner`;
* creates a **discarded clone** from the verified database and performs a real `hh_app`
  smoke: `SELECT`, insert a synthetic user/conversation/message (consuming the sequence),
  delete it, read `alembic_version`, roll back;
* removes the disposable container and confirms it is gone. `rehearse` gives the container
  one run id (`--run-id`, or one generated here) and installs SIGTERM/SIGINT handlers, so a
  signal runs the same `finally` as a completion: the private work directory goes, then the
  container is removed by its exact name and its absence is confirmed. Exit 6 means the
  signal was caught and the container was confirmed removed; exit 4 means it could not be
  confirmed, and the container's exact name and safe recovery command are printed so an
  operator can reconcile. The clean-up outcome is resolved *before* the rehearsal outcome on
  every path: if the removal is not confirmed, the refusal is `rehearsal_cleanup` and the
  tool's own failure follows on an `underlying [<code>]` line for diagnostics only, so an
  archive verdict can never hide a container that may still hold plaintext. A hard
  kill (SIGKILL/power) runs no handler: `cleanup-rehearsal --run-id <id>` is the fallback,
  removes only that exact name (after checking its image is the pinned client image), and
  exits 0 only when absence is confirmed. A `house-helper-restore-verify-<run id>`
  container holds no production volume or credential; never remove a container by a prefix
  match and never touch the enrolled `house-helper-db` container.

## 6. Common failures and recovery

| Refusal | Meaning | Action |
|---|---|---|
| `backup_lock` | The maintenance lock is held exclusively, another backup holds the backup mutex, or the lock holder terminated before publication or before it could acknowledge ownership. | Wait for the competing operation; never bypass the lock. The holder owns both state-volume locks but cannot publish: the host performs the atomic publication while it holds `operation.lock`. A live operation refreshes the holder's heartbeat deadline, so it never expires; a holder left behind by a crashed host (`house-helper-backup-hold-<token>`) exits after its unrefreshed lease (`LOCK_HOLDER_LEASE_SECONDS`, 15 minutes) and releases the mutex by itself. Confirm no backup is running before removing one by exact name. A holder that has already died can neither publish nor acknowledge ownership. |
| `operation_busy` | Another host operation (`mode-a`, `migrate`, `reconcile-migration`, `handoff`, `backup` or `restore`) holds `<journal-dir>/operation.lock`. | Wait for it to finish and re-run; never delete the lock file or work around it. Exit 3, before anything was read or changed. |
| `handoff_unknown` | A handoff intent has no observed outcome (LCK-08/LCK-13). | Only `enroll.py handoff --role <role> --approved-by <owner>` may resolve it, from proven termination plus the complete ACL. Exit 5. Maintenance refuses before any state change. |
| `migration_unknown` | A `migrate` intent has no observed outcome: the run timed out, was interrupted, or was recorded UNKNOWN (LCK-08). | Only `enroll.py reconcile-migration` may resolve it, from the enrolled cluster, positive worker termination and the observed schema revision. Exit 5. Backup refuses before it publishes; restore refuses before any `CREATE DATABASE`. |
| `lock_handoff_unknown` | A `handoff_lock` intent has no observed outcome (LCK-08). | Only `enroll.py lock-handoff --approved-by <owner>` may resolve it, from the kernel-level ownership proof (the deployed image's real uvicorn process's own fdinfo record for the validated inode, with a host-held shared witness through publication) plus the frozen `check-idle` observation. Exit 5. Maintenance refuses before any state change. |
| `lock_handoff_journal` | The lock-handoff history is not a valid sequential intent/outcome pairing, or its evidence is internally contradictory. The latest outcome is the state: a pending intent hides earlier observed evidence, and a later failed outcome supersedes it. | Journal corruption: inspect the named `seq`, do not edit or truncate the journal, and do not infer an observation from a live probe. Exit 3, never an internal error. |
| `handoff_journal` | The handoff history is not a valid intent/outcome pairing - including a malformed outcome type such as `[]`, `{}`, `null` or an unknown string. | Journal corruption: inspect the named `seq`, do not edit or truncate the journal, and do not infer a handoff from the live ACL. Exit 3, never an internal error. |
| `journal_mismatch` | An explicit `--journal-dir` resolves to a different directory than `HOUSE_HELPER_PG_JOURNAL_DIR`, so the tools would take different `operation.lock` files. | Use one directory for every host tool (or unset the variable). Exit 3, before anything is read or changed. |
| `cluster_identity` | Mode B differs from the enrollment record. | Read the mismatch items; do not bypass. Reconcile the cluster or perform a separate authorized enrollment. |
| `image_unavailable` / `image_identity` | The pinned `postgres@sha256:...` client image is absent or was replaced. | Restore the pinned image. Do not substitute another tag. |
| `docker_infrastructure` | Docker did not execute the program at all: the CLI/daemon failed (exit 125/126/127, an unreachable daemon) or `docker exec` found the container stopped. | An environment failure, never evidence about the archive: fix Docker or the image and re-run. It is exit 4 and is never reported as `archive_integrity`, `sequence_extraction` or `isolated_restore`. |
| `passfile_*` | Missing, non-private, wrong-owner, duplicate/wildcard-mismatched passfile. | Fix the host file; the tool never prints its contents. |
| `maintenance_acl` / `maintenance_identity` | OD-2 database has `PUBLIC` access, a missing grant, or the wrong session identity. | Apply the §2.2 arrangement; never widen the grant. |
| `temp_database_unresolved` | An interrupted restore left a non-fenced temporary database. | Do not force it into service. Inspect `pg_database`, confirm no sessions, decide with the Owner. A reviewed cleanup is: connect as `hh_owner`, `DROP DATABASE "<exact name>"`, verify absence, append a `temp_db:retired` record. The tool never drops it automatically. |
| `temp_database_reappeared` | A retired name exists again. | Fail closed; find what recreated it. |
| `archive_members` / `archive_checksum` / `archive_manifest` | Wrong member set/order, dump SHA mismatch, non-canonical or invalid manifest. | Treat the archive as unusable; make a new backup. |
| `archive_v1` | A v1 recovery point was passed to the v2 tool. | v1 is read by the old tool only; no conversion is promised. |
| `prehardening_access` | A client connected while `ALLOW_CONNECTIONS` was false. | Stop: the fence did not hold. Do not use the database. |
| `sequence_set_selection` / `sequence_extraction` / `sequence_setval` | The archive has zero/two `SEQUENCE SET` entries, an unrecognized byte, a duplicate `setval`, or a value outside the signed 64-bit range. | Fail closed. Do not broaden the parser; this is an implementation finding (SEQ-04/SEQ-06). |
| `sequence:behind_rows` | The archive pair would allocate a message id `<= max(id)`. | Never restore into a serving database. Investigate the archive. |
| `sequence:*_mismatch` | The restored pair differs from the archive. | Do not promote; recreate the restore database. |
| `restore_verification` | A digest, ownership, ACL, revision or object-contract check failed. | The database is not `verified`. Inspect, do not switch the DSN. |
| `alembic_check` | The restored schema has drift from the image's models. | Do not serve it. Re-run the migration chain / use a matching image. |
| `isolated_*` | The disposable rehearsal failed. | The production cluster was not made `verified`-and-promoted by that run; fix the cause and re-run. |

A failure creates no automatic rollback and no automatic retry. The journal's
`temp_db` record names the exact state reached; absence and ownership are observed, never
inferred.

## 7. What this does not claim

* No Production deployment, cutover, DSN switch or timer re-arm (WP-2F).
* The off-host `age`/`rclone` integration and the v1/v2 rehearsal dispatcher are WP-2E and are
  implemented in the repository but **not activated**; see `deploy/postgres/offsite_v2.md`.
* No Linux/POSIX deployment evidence merely from the Windows development run. The live
  rehearsal was executed on the isolated Docker Desktop environment; the POSIX credential
  ownership/mode checks and the production host run remain separate gates.
* No point-in-time recovery, logical replication or cluster-role backup (GOV-13).
* No performance or size qualification beyond the isolated rehearsal.
* OD-2, OD-6 (TLS) and OD-8 (credential mechanism/rotation) remain Owner choices.
