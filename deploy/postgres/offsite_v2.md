# PostgreSQL format-v2 recovery points off-host (HH-DB-01 WP-2E)

**Status: repository implementation and the C7-P3 scheduled-unit configuration.** The
PostgreSQL deployment is the production surface (C1-C6), and
`house-helper-offsite-backup.service` now passes `--database postgresql`, so the scheduled
path is the v2 source. Nothing in the repository installs or enables the unit: activating it
is the owner's transition step in `deploy/postgres/scheduled_maintenance.md`. No PostgreSQL
point has been taken or uploaded by a scheduled run until that step is performed, and the
real external round trip has **not** been executed. *[Updated 2026-11-25: that transition step
has since been performed — scheduled runs have taken and uploaded PostgreSQL points, and the first
regular unattended backup completed on 2026-11-25 (details in the note below). The §5
synthetic-data external rehearsal has still not been executed.]* *[Note 2026-11-24, C7-P4: the restore half
has since been exercised once from the real offsite folder: an independent recovery of one
committed PostgreSQL point on a non-production host (A9-R3, operational PASS / CLEAN), recorded in
`docs/STATUS.md`. It was not the synthetic-data test-prefix procedure of §5, which has not been
run, and at that date both production maintenance timers were disabled (historical).]* *[Updated
2026-11-25, C7-P4-A12: both maintenance timers (`house-helper-purge.timer`,
`house-helper-offsite-backup.timer`) are enabled and active, and the first regular unattended cycle
completed successfully on 2026-11-25. The encrypted offsite backup started at 04:20:08Z and ended
`Result=success`; its point `house-helper-20261125T042010Z-d4659a83.tar.age` and its commit marker
were verified by a streamed readback, the remote held 10 committed points with no `.partial`
object, and the n8n end-to-end verification passed. A12 operational verification passed. The
maintenance image remains `house-helper:d02b34ed369c`, the serving image remains
`house-helper:ddca0966bd11`, PostgreSQL remains 16.15, and the schema remains
`0014_user_measure_overrides`; the evidence and its grades are in `docs/STATUS.md`. No point
produced by an unattended run has been restored: A9-R3 demonstrated the restore path on an earlier
point, and the synthetic-data test-prefix procedure of §5 remains unrun.]* This document is the
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
sudo python3 deploy/offsite_backup.py run --database postgresql --postgres-compose-file deploy/compose.postgres.yaml --postgres-journal-dir /var/lib/house-helper/cutover --postgres-credentials-dir <credentials-dir> --recipients-file /etc/house-helper/age-recipients.txt --rclone-config /etc/house-helper/rclone.conf --remote NAME:house-helper --work-dir /run/house-helper-offsite
```

Give the three `--postgres-*` options the values `house-helper-offsite-backup.service` passes (they come from
`house-helper-purge.env`): a manual run then takes the same host `operation.lock`, the same journal directory and the
same passfiles as the scheduled run, instead of the tools' defaults. `deploy/postgres/scheduled_maintenance.md`
section 3.5 shows how to read those non-secret values out of the installed file without exporting anything.

The `age` recipients file, the rclone configuration, the one folder, the access mode and the
work directory are the same ones as v1. The checkout must carry the object-contract fixture
(`tests/fixtures/pg_object_contract.json`) and the `db/*.sql` artifacts of the revision that
wrote the archive; a point from another revision is refused by `verify-archive`, deliberately.
The command refuses, before anything is uploaded, when: the archive is not produced;
`verify-archive` fails; the in-process v2 contract validation fails; `age` fails; the work
directory is not private; a remote object of the run's name already exists. The exit statuses
are the existing six: 0, 1, 2, 3, 6, 7.

**Activation is the owner's step.** The scheduled unit now passes `--database postgresql`;
installing it, filling in the shared `purge.env`/`offsite.env` values and re-arming the
timer are the C7-P3 transition in `deploy/postgres/scheduled_maintenance.md`. Do not enable
a v2 timer until the Owner records the activation approval and the prerequisites of that
procedure are observed. *[Updated 2026-11-25: the Owner recorded the activation approval in A11,
the timer is enabled and active, and the first regular unattended backup passed A12 (see the
status note above). The instruction is kept as the historical gate of the transition.]*

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
* Owner approval to activate the v2 backup path at all; the scheduled unit already passes
  `--database postgresql`, and installing/re-arming it is the transition in
  `deploy/postgres/scheduled_maintenance.md`. *[Closed 2026-11-25: activation was performed in A11
  and the first regular unattended backup passed A12 (status note above). The §5 synthetic-data
  test-prefix round trip above is a separate item and remains open.]*
* PostgreSQL production provisioning, enrollment, cutover and any production credential are
  governed by the C1-C6 cutover record and separately authorized; WP-2E changes none of them.
* Monitoring and alerting stay as they are (BAK-17): the existing exit codes and journal lines
  are the interface; WP-2E adds no field, unit or check.
* The WP-2D prerequisites themselves (OD-2 maintenance database, per-role passfiles, pinned
  client image) remain prerequisites of this path.
