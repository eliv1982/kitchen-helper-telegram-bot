# PostgreSQL format-v2 recovery points off-host (HH-DB-01 WP-2E)

**Status: repository implementation only. Nothing here is activated.** Production remains on
SQLite and on the v1 off-host schedule (`deploy/offsite_backup.py` without `--database
postgresql`). No systemd unit passes `--database postgresql`; no PostgreSQL backup has been
taken or uploaded; the real external round trip has **not** been executed. This document is the
operator procedure and the list of gates that remain. It is governed by
`docs/architecture/HH-DB-01_Architecture_Frozen_Specification_v1.0.md` §8 (BAK-15..BAK-17);
where this text and the specification differ, the specification wins.

## 1. What exists and what is reused

| Component | Role in WP-2E |
|---|---|
| `deploy/offsite_backup.py` | The one off-host tool. `--database postgresql` selects the v2 source; without it nothing changed. |
| `deploy/postgres/maintenance.py` | The accepted WP-2D host tool. `backup` creates the snapshot-consistent v2 archive; `verify-archive` validates it; `rehearse` performs the isolated restore. |
| `house_helper/persistence/pg_backup.py` | The frozen v2 contract, imported by the backup path to validate the exact bytes that are encrypted. |
| `deploy/offsite_rehearsal.py` | The owner-side rehearsal. It dispatches on the format inside the decrypted, commit-bound archive. |
| `age`, `rclone`, the one remote folder, the commit record, the retention arithmetic, the `.partial` rule, the clock rules and the exit codes | **Unchanged.** WP-2E adds no second scheduler, storage configuration, retention engine or monitoring interface. |

There is no `dbtool sanitize` in the v2 path. A logical `pg_dump` contains only live rows, so
"deleted rows leave no trace" is inherent; the step is removed, not weakened (BAK-15).

## 2. The chain, and what each step proves

`validated PostgreSQL archive v2 -> age -> scoped rclone upload -> remote read-back -> commit
record -> final check -> retention`

1. **Create.** `maintenance.py backup` opens one `REPEATABLE READ` transaction, exports a
   snapshot, runs `pg_dump --snapshot=<id>` with the frozen flags, computes every count and
   digest in that same transaction, reads the sequence raw pair from exactly one `SEQUENCE SET`
   TOC entry, and writes the format-v2 archive. It also writes the manifest and reads the
   archive back (WP-2D, unchanged).
2. **Validate.** The produced file is copied **once** into a private, exclusively created
   snapshot, and the copy is hashed as it is written. Every later step reads that snapshot;
   the tool's own pathname is never validated or encrypted again. `maintenance.py
   verify-archive` re-reads the snapshot: members, the canonical manifest, the dump's
   SHA-256, exactly one `SEQUENCE SET` entry, one `TABLE DATA` entry per contract table, and
   the normalized object contract at the checked-in revision. Then the off-host tool
   validates the same snapshot again in its own process with
   `house_helper.persistence.pg_backup.read_archive`. A zero exit status alone is never taken
   as validation.
3. **Encrypt.** `age -R recipients` receives an already-open descriptor of the validated
   snapshot, not its path, so a name replaced after validation cannot change what is
   encrypted. The snapshot is hashed again through the same descriptor immediately before
   `age` and after it returns; any difference from the copied digest fails the run and the
   ciphertext is removed, so no unvalidated bytes are published. The private identity is not
   on the VPS and is not needed. The plaintext snapshot is removed as soon as `age` returns.
4. **Upload and read back.** The encrypted object goes up under `NAME.partial`, is listed (size
   and MD5), downloaded again, and its SHA-256 is compared. `NAME` is then a **candidate**, not
   a recovery point.
5. **Commit.** The object is renamed server-side to `NAME`; the rename is checked; then
   `NAME.commit` (name, size, SHA-256, SHA-1, MD5) is uploaded and read back. Only an object
   with a valid, matching record is a recovery point.
6. **Final check and retention.** The folder is listed once more and classified. The run is
   `stored` only if the new point is committed there. Retention runs only after that, and only
   on committed points; 7 daily + 4 weekly; a `.partial` is never deleted; while any unresolved
   object exists no committed point is dropped.

What a successful run **does not** prove: that the point can be restored. That is §3.

## 3. Rehearsal from the external artifact (owner's machine)

`deploy/offsite_rehearsal.py` runs on the owner's own machine, never on the VPS (it refuses
when `/etc/house-helper` exists). It lists the folder, chooses a committed point, downloads it,
compares size/SHA-256/SHA-1/MD5 with its commit record, decrypts with the private identity, and
**dispatches on the `format` in the archive's own `MANIFEST.json`** - never on the object name,
which has the same shape for both formats. An unknown or contradictory format is refused; there
is no fallback to the other format.

* **v1 (`house-helper-recovery-point/1`)** takes the unchanged SQLite path: the accepted tar
  reader, the name-time check, then the read-only SQLite checks and every `--expect-rows`.
* **v2 (`house-helper-recovery-point/2`, `database: postgresql`)** validates the manifest under
  the frozen contract, checks `--expect-rows` against the manifest's own counts, and runs the
  accepted WP-2D isolated restore (`maintenance.py rehearse`): a disposable `--network none`
  PostgreSQL 16 container with a tmpfs `PGDATA`, restore with `--role=hh_owner --no-owner
  --no-privileges`, `db/schema.sql` and `db/privileges.sql`, then schema revision, per-table
  counts and digests, the object contract, the sequence raw pair and `next > max(id)`,
  ownership/ACL assertions, the attachment loader proof, `alembic check`, and a real `hh_app`
  smoke on a discarded clone. Only the decrypted archive path reaches that tool: the age
  identity reaches no argument of it.

The tool never writes to the remote. It removes its scratch directory (which holds plaintext)
on every exit path; a directory that cannot be removed is a failure. Exit 0 is the only status
that says the point can be restored; 4 means it was retrieved and the accepted tool's own
structured refusal is about the artifact or the restored data. A Docker, image, tooling or
internal failure is exit 1, never exit 4: it is not evidence that the point is defective.

A v2 rehearsal owns exactly one disposable container. It generates a run id, passes it as
`--run-id`, and the helper installs SIGTERM/SIGINT handlers so a signal runs the same `finally`
as a completion: the private work directory goes, then the container is removed by its exact
name and its absence is confirmed (exit 6). Killing the parent does not remove a detached
container, so the rehearsal also issues the accepted tool's `cleanup-rehearsal --run-id <id>`
after a signal, a timeout or a kill, and requires exit 0 from it. When removal cannot be
confirmed the status is 1 and the container's exact name
(`house-helper-restore-verify-<run-id>`) is printed with the command that removes it; that is
never a pass. The fallback removes one exact name (after checking its image is the pinned
client image) and never a prefix match or the enrolled `house-helper-db` container.

### 3.1 Preconditions for a v2 rehearsal

* The same checkout revision as the point's `acl_contract_sha256` / object-contract fixture.
  A point from another revision is refused; this is deliberate.
* Docker with the digest-pinned `postgres@sha256:...` client image from
  `deploy/compose.postgres.yaml`, and (for the `alembic check` and attachment proof) the
  application image the run used. No PostgreSQL cluster, no credential and no enrollment is
  needed: the restore is isolated.
* Python 3.10+ for `deploy/offsite_rehearsal.py`; `age` and (for the download) `rclone`.
* The private age identity, outside the checkout, readable by its owner only.

```bash
python3 deploy/offsite_rehearsal.py --rclone-config "$HOME/keys/house-helper/rclone.conf" --remote NAME:house-helper --identity-file "$HOME/keys/house-helper/age-key.txt" --work-dir "$HOME/scratch/rehearsal" --expect-rows users=2
```

The command is the same for a v1 and a v2 point; the archive decides. `--postgres-compose-file`,
`--postgres-object-contract`, `--postgres-maintenance-script` and `--postgres-app-image` exist
only to point the v2 path at another checkout layout.

## 4. Creating a v2 off-host point (the v2 backup path)

Only after the PostgreSQL cluster is enrolled and WP-2D backup prerequisites hold (Mode B
passes, `hh_backup` has `CONNECT` and `SELECT`, the passfiles exist, the pinned client image is
present). Run as root, from the checkout:

```bash
sudo python3 deploy/offsite_backup.py run --database postgresql --recipients-file /etc/house-helper/age-recipients.txt --rclone-config /etc/house-helper/rclone.conf --remote NAME:house-helper --work-dir /run/house-helper-offsite
```

The `age` recipients file, the rclone configuration, the one folder, the access mode and the
work directory are the same ones as v1. The checkout must carry the object-contract fixture
(`tests/fixtures/pg_object_contract.json`) and the `db/*.sql` artifacts of the revision that
wrote the archive; a point from another revision is refused by `verify-archive`, deliberately.
The command refuses, before anything is uploaded, when: the archive is not produced;
`verify-archive` fails; the in-process v2 contract validation fails; `age` fails; the work
directory is not private; a remote object of the run's name already exists. The exit statuses
are the existing six: 0, 1, 2, 3, 6, 7.

**Not activated.** The systemd unit and timer are unchanged and pass no `--database`. Do not add
the flag to `/etc/house-helper/offsite.env` or to the unit, and do not enable a v2 timer, until
the Owner records the activation approval and the WP-2F cutover work begins.

## 5. The one real external rehearsal (Owner GO required)

Automated tests use a fake remote and a fake age; they are necessary and **not sufficient**.
The acceptance proof is one real round trip with **synthetic PostgreSQL test data only**:

1. Take a v2 point from the disposable test cluster with a dedicated, isolated test prefix in
   the approved remote scope (for example `NAME:house-helper-pg-test`), not the production
   folder:

   ```bash
   sudo python3 deploy/offsite_backup.py run --database postgresql --recipients-file /etc/house-helper/age-recipients.txt --rclone-config /etc/house-helper/rclone.conf --remote NAME:house-helper-pg-test --work-dir /run/house-helper-offsite
   ```

2. Confirm the commit record and the final check, and that the folder holds exactly the test
   point and its `.commit`:

   ```bash
   sudo python3 deploy/offsite_backup.py list --rclone-config /etc/house-helper/rclone.conf --remote NAME:house-helper-pg-test
   ```

3. On the owner's machine, rehearse that prefix and that point. The expected result is exit 0
   and a `rehearsal PASSED` line naming the point, the encrypted SHA-256, the schema revision,
   the table/row counts, the sequence raw pair and the isolated server's `system_identifier`.
   Any other exit status is a failed rehearsal; do not record it as one.

   ```bash
   python3 deploy/offsite_rehearsal.py --rclone-config "$HOME/keys/house-helper/rclone.conf" --remote NAME:house-helper-pg-test --identity-file "$HOME/keys/house-helper/age-key.txt" --work-dir "$HOME/scratch/rehearsal" --point house-helper-<UTC>-<8 hex>.tar.age --expect-rows users=2
   ```

4. Remove the test prefix's objects only with the Owner's explicit approval, one object at a
   time: the object first, then its `.commit` (`rclone deletefile NAME:house-helper-pg-test/NAME`,
   then `.../NAME.commit`), then confirm with `list`. Never delete a `.partial` by name (RUNBOOK
   15.11: nothing proves whose it is), and never touch the production folder.
5. Record date, point name, encrypted SHA-256, host, Docker image digest and result in the
   acceptance record. Nothing in step 4 is done by the tool.

No production user data may be used. No real encryption identity, credential or token belongs
in the repository, in a log or in a test.

## 6. Gates that remain open

* Owner approval to execute the external round trip of §5 (synthetic data, isolated prefix).
* Owner approval to activate the v2 backup path at all; until then no unit passes
  `--database postgresql`.
* PostgreSQL production provisioning, enrollment, cutover and any production credential remain
  WP-2F and separately authorized; WP-2E changes none of them.
* Monitoring and alerting stay as they are (BAK-17): the existing exit codes and journal lines
  are the interface; WP-2E adds no field, unit or check.
* The WP-2D prerequisites themselves (OD-2 maintenance database, per-role passfiles, pinned
  client image) remain prerequisites of this path.
