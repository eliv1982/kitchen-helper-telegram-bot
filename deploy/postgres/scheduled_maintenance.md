# PostgreSQL scheduled maintenance: purge and encrypted offsite backup (C7-P3)

This is the owner procedure for switching the two scheduled maintenance jobs from the
legacy SQLite deployment to the approved PostgreSQL deployment. It changes what runs, not
what is deleted or backed up: retention semantics stay in
`house_helper/admin/retention.py`, and the recovery-point format stays the accepted WP-2E
v2 contract in `deploy/postgres/maintenance.py`.

Nothing in the repository installs, enables or starts a unit. Every step below marked
**[OWNER]** changes the host and needs the owner's explicit approval first.

Two rules are absolute here:

- **Stopping and disabling maintenance is the safe answer, not a rollback.** While
  PostgreSQL is the authoritative database, there is no supported reason to start, enable
  or re-arm any legacy SQLite maintenance unit. Section 6 is the whole of that statement.
- **A command that is not shown with its environment does not run as documented.** Manual
  Compose commands get `--env-file /etc/house-helper/purge.env`; host tools get explicit
  paths. The units get the same file from systemd. See section 3.5.

## 1. What the two units run

| Unit | Command | Lock | Failure is |
|---|---|---|---|
| `house-helper-purge.service` | `docker compose -f <checkout>/deploy/compose.postgres.yaml --profile ops run --rm -T purge` (`deploy/ops/pg_oneshot.py purge`) | shared state-volume lock, beside the web service and a backup | a non-zero exit of the one-shot or of `purge-expired` |
| `house-helper-offsite-backup.service` | `deploy/offsite_backup.py run --database postgresql ...` → `deploy/postgres/maintenance.py backup` | `maintenance.py`'s host `operation.lock` plus its container-side state-volume locks | a non-zero exit of `offsite_backup.py` |

Neither unit can fall back to SQLite: the purge unit names only the PostgreSQL compose
file and the `purge` service, and the backup unit passes `--database postgresql` explicitly.
The legacy SQLite unit files are preserved under `deploy/systemd/legacy-sqlite/` as
**historical rollback assets only** — they are not a procedure, and reactivating them is
prohibited by section 6.

The two unit names, timers and schedules are unchanged, so the existing Ops Sentinel,
Cronitor and n8n job identities and heartbeats keep working without edits. The monitoring
drop-in (`ExecStopPost`) observes the real unit result; a failed or degraded run keeps a
non-zero status and therefore **cannot** produce a success heartbeat. The report and the
failure reason go to `journalctl -u house-helper-purge.service` and
`journalctl -u house-helper-offsite-backup.service`.

After every upgrade and every rollback, update `HOUSE_HELPER_IMAGE_TAG` in
`/etc/house-helper/purge.env` to the tag that is now running, in the same step. Forgetting it
fails closed: a purge image whose schema is not the database's is refused (`schema_not_head`,
exit 4) and nothing is deleted. The offsite path does not use that tag; it uses the pinned
`postgres@sha256:` client image named by `deploy/compose.postgres.yaml` and the accepted
`maintenance.py` of the checkout.

### 1.1 Exit statuses and what monitoring makes of them

The emitter in `deploy/monitoring/` sees only the unit's exit status, and the current
contract maps statuses as follows (do not edit the monitoring contract from this procedure):

| Status | Purge unit | Backup unit |
|---|---|---|
| 0 | `success`, success heartbeat | `success`, success heartbeat |
| 1 | `failure` / `purge_failed` | `failure` / `backup_failed` |
| 2 | `failure` / `usage_error` | `failure` / `usage_error` |
| 3 | `failure` / `maintenance_lock_busy` | `failure` / `refused` |
| 4 | `failure` / **`schema_not_head`** | — |
| 6 | — | `degraded` / `retention_needs_review`, **no** success heartbeat |
| 7 | — | `failure` / `unconfirmed` |

The status is coarser than the reason. For the purge unit, exit 4 means either:

- the connection did not complete — unreachable database, refused credential, or a refused
  `CONNECT` (including a missing H3 `hh_purge` handoff); or
- `schema_not_head`: the image tag is not the running version.

Exit 3 covers the other refusals: the wiring and passfile checks, the state-volume lock, and
the target gate that runs **after** a successful connection (`purge_identity`,
`purge_privilege_boundary`, `purge_marker`, and `purge_connect_handoff` — the last also when
the driver itself reported SQLSTATE 42501).

**The journal line is authoritative for the reason.** The one-shot prints a stable code:
`refused [purge_database_unavailable]` (a failed startup: the pinned psycopg 3.3.6 supplies
no SQLSTATE for it, so a missing handoff cannot be named from structured evidence and is
*not* guessed from the driver's translated text), `refused [purge_connect_handoff]` (the
driver did supply SQLSTATE 42501), `error [schema_not_head]`, and so on. The emitter's
`schema_not_head` category for exit 4 is therefore a superset; read the journal before
concluding that the schema is wrong.

### 1.2 Reading a purge refusal

| Journal line (`journalctl -u house-helper-purge.service`) | Exit | What it means | What to do |
|---|---|---|---|
| `refused [oneshot_*]` | 78 | the resolved wiring is not the documented one | compare `systemctl show -p ExecStart` and `--env-file` with 3.3/3.5 |
| `refused [passfile_*]` | 3 | the passfile is missing, foreign, too open or for another login | 3.6 |
| `refused [maintenance_lock_busy]` | 3 | a migration or restore holds the state volume exclusively | the next run catches up; run it by hand when the other operation ends |
| `refused [purge_database_unavailable]` | 4 | the connection did not complete: unreachable, refused credential, or the H3 handoff is missing | verify the handoff (`enroll.py status`); this status cannot tell those apart |
| `refused [purge_connect_handoff]` | 3 | the driver reported SQLSTATE 42501, or the connected session has no `CONNECT` | run `enroll.py handoff --role hh_purge --approved-by <owner>` |
| `refused [purge_identity]` / `[purge_privilege_boundary]` / `[purge_marker]` | 3 | the session is not the enrolled `hh_purge` target | do not proceed; compare with `enroll.py verify` |
| `error [schema_not_head]` | 4 | the image tag is not the running version | set `HOUSE_HELPER_IMAGE_TAG` (1.1 / 3.3) |
| `error [purge_failed]` | 1 | a stage of the retention command failed; the report says what committed | re-run; it is safe to repeat |
| `error [interrupted]` | 130 | the command was interrupted; the batch in progress rolled back | re-run; committed batches are gone on purpose |

A non-zero status never produces a success heartbeat (section 1.1). Exit 4 is the widest
bucket: read the code in the line, not only the number.

## 2. Prerequisites (all must already be true)

- Production is the approved release and PostgreSQL is the running database (C1-C6 closed):
  the enrolled cluster passes `python deploy/postgres/enroll.py verify`, and the serving
  database is at the release's Alembic head.
- The H3 `hh_backup` CONNECT handoff is journaled and its records are intact
  (`enroll.py status`; the H1/H2/H3 `hh_backup` outcomes are preserved, never rewritten).
- `hh_purge.pgpass` exists in `HOUSE_HELPER_PG_CREDENTIALS_DIR`, is owned by the
  container's runtime identity and is mode 0600; `enroll.py check-credentials` passes.
- The legacy SQLite timers and services are stopped and disabled (C7-P1) and were verified
  inactive; the PostgreSQL local backup, isolated restore and encrypted offsite readback
  (C7-P2) are accepted.
- The canonical host journal directory exists, root-owned 0700, at
  `HOUSE_HELPER_PG_JOURNAL_DIR` (default `/var/lib/house-helper/cutover`). Both the one-time
  purge handoff and the backup tool use it; it is never mounted into a container.

## 3. Transition **[OWNER]**

Use one window. Do not enable a timer until its service has succeeded by hand, and do not
run a real purge until a dry run has succeeded and been read.

### 3.1 Record the timer state before anything changes (HOF-04) **[read-only]**

```bash
for t in house-helper-purge.timer house-helper-offsite-backup.timer; do
  sudo systemctl show "$t" -p LastTriggerUSec -p NextElapseUSecRealtime
done
sudo systemctl list-timers 'house-helper-*' --no-pager
sudo ls -l /var/lib/systemd/timers/stamp-house-helper-purge.timer \
             /var/lib/systemd/timers/stamp-house-helper-offsite-backup.timer
```

Record the exact output with a UTC timestamp. These are the "before" values that sections
3.13 and 4 compare against.

### 3.2 Stop the legacy schedule first

The installed units still carry the SQLite text, so this is the last moment they are used:

```bash
sudo systemctl disable --now house-helper-purge.timer house-helper-offsite-backup.timer
sudo systemctl stop house-helper-purge.service house-helper-offsite-backup.service || true
systemctl is-active house-helper-purge.service house-helper-offsite-backup.service \
  house-helper-purge.timer house-helper-offsite-backup.timer
systemctl list-timers 'house-helper-*'
```

`is-active` must print `inactive` four times; no PostgreSQL maintenance run is started
while a legacy one may still be finishing.

### 3.3 Install the PostgreSQL units and the shared environment

`purge.env` now carries the activation value, the credential directory, the canonical
journal directory and the running image tag in addition to the checkout and env-file
path. `offsite.env` is unchanged.

```bash
sudo install -m 0644 deploy/systemd/house-helper-purge.env.example /etc/house-helper/purge.env
sudoedit /etc/house-helper/purge.env
sudo install -m 0644 deploy/systemd/house-helper-purge.service deploy/systemd/house-helper-purge.timer \
  deploy/systemd/house-helper-offsite-backup.service deploy/systemd/house-helper-offsite-backup.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/house-helper-purge.service /etc/systemd/system/house-helper-purge.timer \
  /etc/systemd/system/house-helper-offsite-backup.service /etc/systemd/system/house-helper-offsite-backup.timer
```

Re-read everything systemd will actually use — the fragment, every drop-in and the resolved
argv, not only the file just installed:

```bash
for u in house-helper-purge.service house-helper-purge.timer \
         house-helper-offsite-backup.service house-helper-offsite-backup.timer; do
  echo "== $u"
  sudo systemctl cat "$u" | grep -E '^(# |\[|EnvironmentFile=|ExecStart=|OnCalendar=|Persistent=|Unit=)'
  sudo systemctl show "$u" -p FragmentPath -p DropInPaths -p ExecStart
done
```

Acceptance: `FragmentPath` is the file just installed; `DropInPaths` is exactly the set the
owner intended (the monitoring `ExecStopPost` drop-in if section 3.12 is in scope, and
nothing else); the resolved `ExecStart` of the purge unit is
`/usr/bin/docker compose -f <checkout>/deploy/compose.postgres.yaml --profile ops run --rm -T purge`
with the checkout expanded — no `--env-file` and no `-e` (the unit's own
`EnvironmentFile=` is its environment source); the backup unit's `ExecStart` names
`--database postgresql` and the journal and credential directories. A drop-in that changes
the command is a second installation path: remove it or describe it here before proceeding.

### 3.4 Prove the installed image contains C7-P3

Now that `purge.env` exists, tie the running image to the approved code:

```bash
CHECKOUT="$(sed -n 's/^HOUSE_HELPER_CHECKOUT=//p' /etc/house-helper/purge.env)"
TAG="$(sed -n 's/^HOUSE_HELPER_IMAGE_TAG=//p' /etc/house-helper/purge.env)"
IMAGE="house-helper:${TAG}"
# The revision label must be the deployed commit.
sudo docker image inspect "$IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
git -C "$CHECKOUT" rev-parse HEAD
# The one-shot inside the image must know the purge role. With no arguments it prints its
# usage line and exits 2; that is the expected result of this probe.
sudo docker run --rm --network none --entrypoint python "$IMAGE" \
  /opt/house-helper/ops/pg_oneshot.py
```

The last command must print
`usage: pg_oneshot.py {migrate,schema,provision,purge,backup,restore}`. If `purge` is
absent, the image is older than this release: build and deploy the approved release first,
then start this procedure again. Record the label, both commit ids and the usage line. If the
label prints `unknown`, the build recorded no revision: rebuild with `HOUSE_HELPER_GIT_SHA`
set, because the image cannot then be tied to a commit.

### 3.5 The manual-command environment (once per shell)

systemd gives a unit the variables in its `EnvironmentFile`. An ordinary `sudo` does
**not** carry the invoking shell's environment, and `sudo -E` is frequently disabled by
sudoers — so an exported variable is not a reliable way to run a manual command. Compose
reads `/etc/house-helper/purge.env` itself when it is named with `--env-file`, and the host
tools take their non-secret paths as options: `--compose-file`, `--journal-dir` and, on
every command that resolves the Compose surface, `--credentials-dir`. Both then resolve
the *same* approved values as the units.

`--credentials-dir` is not optional. Without it the host tool resolves
`HOUSE_HELPER_PG_CREDENTIALS_DIR` from its inherited environment (which an ordinary `sudo`
does not provide) and falls back to the repository's `deploy/postgres/credentials`
directory. The container's credential mount is then compared against the wrong directory
and the run is refused with `volume_identity` before anything changes. The approved value
is the one in the same file the units read — an external, root-owned directory, never the
repository.

Every path-valued option follows one resolver: a relative value (from the option, the
environment or the Compose `${VAR:-default}`) is resolved against the compose file's
directory, canonicalised (symlinks and case) and compared by identity. A relative spelling
and an absolute spelling of the same directory therefore agree; only a genuine difference is
`credentials_conflict`. `maintenance.py` uses that same resolver and has no repository
default of its own.

```bash
# Read the non-secret paths out of the same file the units use. No secret is in this file
# and no line of it is executed.
CHECKOUT="$(sed -n 's/^HOUSE_HELPER_CHECKOUT=//p' /etc/house-helper/purge.env)"
JOURNAL_DIR="$(sed -n 's/^HOUSE_HELPER_PG_JOURNAL_DIR=//p' /etc/house-helper/purge.env)"
CREDENTIALS_DIR="$(sed -n 's/^HOUSE_HELPER_PG_CREDENTIALS_DIR=//p' /etc/house-helper/purge.env)"
```

Every manual Compose command in this document is then written as:

```bash
sudo docker compose -f "$CHECKOUT/deploy/compose.postgres.yaml" \
  --env-file /etc/house-helper/purge.env --profile ops run --rm -T purge ...
```

`--env-file` is not optional. Without it Compose fails before it selects a service —
`error while interpolating services.<service>.environment.HOUSE_HELPER_PG_ACTIVATE:
required variable HOUSE_HELPER_PG_ACTIVATE is missing a value`. That is the deliberate
activation guard, not a fault. `purge.env` contains paths, the activation value and the
running image tag; it must never contain a credential (the passfiles are mounted, not read
from the environment).

Manual and scheduled execution therefore agree: `--env-file` and the unit's
`EnvironmentFile` name the same file, and both resolve `HOUSE_HELPER_IMAGE_TAG`,
`HOUSE_HELPER_PG_CREDENTIALS_DIR`, `HOUSE_HELPER_ENV_FILE` and
`HOUSE_HELPER_PG_ACTIVATE` from it.

### 3.6 Non-mutating preflight **[read-only]**

Nothing below starts a container, connects as a serving role, changes the database or
publishes anything. The two `enroll.py` reads open the journal directory and create its
lock files if they are missing (`O_CREAT`), and nothing else in it is written. Record the
output.

```bash
# The Compose surface resolves with the units' own environment (no container is created).
sudo docker compose -f "$CHECKOUT/deploy/compose.postgres.yaml" \
  --env-file /etc/house-helper/purge.env --profile ops config --quiet

# The read-only deployment preflight (guards, credential files, pg_hba, mounts, subnet).
sudo python3 "$CHECKOUT/deploy/postgres/preflight.py" \
  --env-file "$(sed -n 's/^HOUSE_HELPER_ENV_FILE=//p' /etc/house-helper/purge.env)" \
  --credentials-dir "$CREDENTIALS_DIR"

# Enrollment state, pending handoffs/migrations and the credential files (no values printed).
sudo python3 "$CHECKOUT/deploy/postgres/enroll.py" \
  --compose-file "$CHECKOUT/deploy/compose.postgres.yaml" \
  --journal-dir "$JOURNAL_DIR" status
sudo python3 "$CHECKOUT/deploy/postgres/enroll.py" \
  --compose-file "$CHECKOUT/deploy/compose.postgres.yaml" \
  --journal-dir "$JOURNAL_DIR" --credentials-dir "$CREDENTIALS_DIR" verify
sudo python3 "$CHECKOUT/deploy/postgres/enroll.py" \
  check-credentials --credentials-dir "$CREDENTIALS_DIR"

# The runtime identity the container uses (10001:10001) must own the passfile, mode 0600.
sudo stat -c '%u:%g %a %n' "$CREDENTIALS_DIR" "$CREDENTIALS_DIR/hh_purge.pgpass"
```

Acceptance: `config --quiet` exits 0; the preflight exits 0; `status` shows no
`handoff UNKNOWN` or `migration UNKNOWN` line; `verify` exits 0; `check-credentials`
passes; the directory is mode 0700 and `hh_purge.pgpass` is `10001:10001` mode 0600. The
passfile *authenticates* at the dry run below — that is the authentication evidence; the
ownership check cannot prove a password is right, and `check-credentials` must not print it.

### 3.7 Observe the refusal that proves the gate is live (failure propagation)

Before the H3 handoff, the manual dry run **must** refuse. This is the fail-closed evidence
that the wiring, the passfile and the connection gate all work, observed on the real host:

```bash
sudo docker compose -f "$CHECKOUT/deploy/compose.postgres.yaml" \
  --env-file /etc/house-helper/purge.env --profile ops run --rm -T purge purge --dry-run
echo "exit=$?"
```

Acceptance: non-zero exit, expected `4`, and `refused [purge_database_unavailable]` naming
the H3 handoff. A `refused [oneshot_*]`, `refused [passfile_*]` or `refused
[maintenance_lock_busy]` is a different finding: fix what the preflight missed and repeat.
No row is counted or deleted: without `CONNECT` there is no session to run a statement in.
Record the exit status and the console line.

### 3.8 Journal the H3 `hh_purge` CONNECT handoff

This is the one-time authority change; it runs Mode B, the schema-at-head gate, the existing
handoff reconciliation and the `operation.lock`, and appends a new `handoff:scheduled`
intent/outcome pair. It does not touch the completed H1/H2/H3 `hh_backup` records.

```bash
sudo python3 "$CHECKOUT/deploy/postgres/enroll.py" \
  --compose-file "$CHECKOUT/deploy/compose.postgres.yaml" \
  --journal-dir "$JOURNAL_DIR" --credentials-dir "$CREDENTIALS_DIR" \
  handoff --role hh_purge --approved-by "<owner>"
sudo python3 "$CHECKOUT/deploy/postgres/enroll.py" \
  --compose-file "$CHECKOUT/deploy/compose.postgres.yaml" \
  --journal-dir "$JOURNAL_DIR" status
```

The command prints `ok [handoff]: granted CONNECT ...` (or `already holds CONNECT` on a
repeat) and `status` shows no `handoff UNKNOWN` line.

### 3.9 Observe a manual dry run (authorization, lock and target gates)

```bash
sudo docker compose -f "$CHECKOUT/deploy/compose.postgres.yaml" \
  --env-file /etc/house-helper/purge.env --profile ops run --rm -T purge purge --dry-run
echo "exit=$?"
```

Acceptance: exit `0` and `ok [dry_run]: would delete <n> row(s): ... Nothing was changed.`
This run is the live evidence of all of these at once: the mounted passfile authenticates
as `hh_purge`; the H3 CONNECT handoff is in effect; the enrolled marker parses; the session
is `hh_purge` on `house_helper` with no `TEMPORARY`; the schema is the image's head; and the
shared state-volume lock was taken beside the web service. **Record the dry run's output
before any real purge.** A non-zero exit stops this procedure: no real purge follows a
failed or unread dry run.

**HOF-03, shared lock (the concurrency gate).** While the web service is running, the
exclusive idle check must be refused — the frozen specification's observation, corroborated
by the lock-handoff command's kernel ownership proof (the web process holds the shared lock;
nothing else holds it exclusively):

```bash
sudo docker compose -f "$CHECKOUT/deploy/compose.postgres.yaml" \
  --env-file /etc/house-helper/purge.env --profile pg exec house-helper \
  python /opt/house-helper/ops/dbtool.py check-idle
echo "exit=$?"
```

Acceptance: exit `3` and `refused [maintenance_lock_busy]`: the web service holds the
shared lock, so an exclusive command is refused.

Then record the lock handoff (HOF-03 item 4). The command runs the frozen `check-idle`
observation inside the verified running web container and reads the kernel's lock ownership
on the host, then journals them as a `handoff_lock` intent/outcome pair — the frozen
`handoff:lock` event:

```bash
sudo python3 "$CHECKOUT/deploy/postgres/enroll.py" \
  --compose-file "$CHECKOUT/deploy/compose.postgres.yaml" \
  --journal-dir "$JOURNAL_DIR" --credentials-dir "$CREDENTIALS_DIR" \
  lock-handoff --approved-by "<owner>"
sudo python3 "$CHECKOUT/deploy/postgres/enroll.py" \
  --compose-file "$CHECKOUT/deploy/compose.postgres.yaml" \
  --journal-dir "$JOURNAL_DIR" status
```

Acceptance: `ok [lock-handoff]: the web service holds the shared state-volume lock and no
exclusive holder remains`, and `status` shows no `lock handoff UNKNOWN` line. The command
runs the frozen `dbtool check-idle` (it must be refused with `maintenance_lock_busy`) and,
on the host, reads the kernel's lock ownership twice. The validated web process — its
cgroup names the immutable container id and its argv is the deployed image's real uvicorn
command, which the kernel records as
`[/opt/venv/bin/python, /opt/venv/bin/uvicorn, house_helper.web.asgi:app, ...]` — must hold
a `FLOCK ... READ` record on the state-volume lock file through its own descriptor. The
record's device/inode must equal the validated lock file's; a descriptor whose record
names another file was reused and is refused as indeterminate. A temporary acquisition
probe is never taken, so a probe's own shared lock can never be published as the web
process's lock.

After `check-idle`, the host takes a **shared witness** on that same validated inode and
holds it across the final snapshot, a fresh verification of the current running web
container (exact immutable id), the journal outcome append and its fsync. No exclusive
holder can acquire in that interval. The witness is not web-ownership evidence: the web
process was attested by the two kernel snapshots, and the journal records exactly that
closed interval (`final_verification_through_publication`) — nothing after the fsync.

A determinate negative is journaled as a `failed` observation and exits `3`; an
indeterminate, contradictory, changed or disappearing observation leaves the intent
`UNKNOWN` and appends no outcome. The latest journal outcome is the state, and every
re-observation first appends a new intent: a repeated successful run for the same
container is a fresh `observed` outcome in that new epoch; a replacement container is
observed in it; and a later failed re-observation supersedes an earlier observed one, so
the historical record can no longer authorize the re-arm. The intent is durable before the
observation, so an indeterminate or interrupted re-observation leaves a pending intent
that fences the later transitions rather than leaving the earlier `observed` state
authoritative. If the container or its state volume is not the recorded one, the command
refuses before it writes anything. The successful manual backup in section 3.11 is the
separate evidence for HOF-03 item 5.

### 3.10 Observe a manual real purge

```bash
sudo systemctl start house-helper-purge.service
systemctl status house-helper-purge.service --no-pager
journalctl -u house-helper-purge.service -n 40 --no-pager
```

The service run must show `Result: success`, `status=0/SUCCESS`, and the admin report
(`ok [purged]` or `ok [unchanged]` with counts and two timestamps, nothing private). A
refusal before the first DELETE is the expected fail-closed outcome of a missing handoff or
of the wrong schema revision — see section 1.1 for the exact statuses and categories.

### 3.11 Observe a manual offsite backup, and its external round trip

```bash
sudo systemctl start house-helper-offsite-backup.service
systemctl status house-helper-offsite-backup.service --no-pager
journalctl -u house-helper-offsite-backup.service -n 60 --no-pager
```

The log must show the `maintenance.py backup` step, the format-v2 validation, the
encrypted object upload, the readback and the commit record. A PostgreSQL point is
`house-helper-recovery-point/2` with `database: postgresql`.

Then read the **remote** back, independently of the tool that wrote it:

```bash
sudo python3 "$CHECKOUT/deploy/offsite_backup.py" list \
  --rclone-config "$(sed -n 's/^HOUSE_HELPER_RCLONE_CONFIG=//p' /etc/house-helper/offsite.env)" \
  --remote "$(sed -n 's/^HOUSE_HELPER_OFFSITE_REMOTE=//p' /etc/house-helper/offsite.env)"
```

Acceptance: the new object and its commit record are listed, and the listed retention
decision matches the accepted 7 daily + 4 weekly policy. A local success without this
external listing is not acceptance: the object has to exist on the offsite service.

### 3.12 Monitoring: a success check and a failed/degraded check

The emitter writes one line per unit stop and sends the event; it never turns a non-zero
exit into a success heartbeat (`deploy/monitoring/EXECSTOPPOST.md`, `EVENT_CONTRACT.md`).
Observe both directions before trusting the timer:

```bash
# The two real runs above must each have produced one emitter line, with status=success.
sudo journalctl -u house-helper-purge.service | grep 'hh-ops-emit'
sudo journalctl -u house-helper-offsite-backup.service | grep 'hh-ops-emit'
```

- **Success:** one `status=success` line, the n8n Data Table row updated, and the external
  dead-man check pinged for the same identity.
- **Failed or degraded:** the refusal observed in section 3.7 is the ready-made failure
  case. If the emitter was installed then, its line must read `status=failure` and the
  category the table in section 1.1 gives for that status, with **no** success heartbeat. If
  it was not installed yet, run the emitter's synthetic failure path
  (`deploy/monitoring/TEST_MATRIX.md`) before arming the timer and record the result. An
  alerting path that has never been seen to fail is not evidence.

### 3.13 Re-arm the timers sequentially, and record what systemd did

Only after the manual purge, the manual backup and both monitoring observations:

```bash
# One timer at a time. Record between the two steps, not only at the end.
sudo systemctl enable --now house-helper-purge.timer
sudo systemctl list-timers 'house-helper-purge.timer' --no-pager
sudo systemctl show house-helper-purge.timer -p LastTriggerUSec -p NextElapseUSecRealtime
sudo systemctl enable --now house-helper-offsite-backup.timer
sudo systemctl list-timers 'house-helper-*' --no-pager
sudo systemctl show house-helper-offsite-backup.timer -p LastTriggerUSec -p NextElapseUSecRealtime
sudo ls -l /var/lib/systemd/timers/stamp-house-helper-purge.timer \
             /var/lib/systemd/timers/stamp-house-helper-offsite-backup.timer
```

Acceptance: each timer is `active (waiting)` with the documented schedule
(`*-*-* 03:30:00 UTC` plus up to 5 minutes of randomized delay), its `NextElapseUSecRealtime`
is in the future, and the "after" timestamps and stamps are recorded next to the "before"
values from section 3.1. Enabling both at once is not sequential activation and leaves the
order of any catch-up run unrecorded.

## 4. `Persistent=true` catch-up

Both timers are `Persistent=true`. If the host was off (or a timer was not running) at the
scheduled time, systemd runs the service **once** when the timer next starts, never once per
missed day — but only if the schedule was actually missed, according to the timer's stamp.
A catch-up run is therefore **conditional, not guaranteed**, and must never be promised.

The before/after values from sections 3.1 and 3.13 are what show whether one happened:
`LastTriggerUSec` changing on the first start, a stamp newer than the enable, or an
immediate `journalctl` report for the service. Record which of those you observed.

Acceptance is "the observed behavior is recorded and understood", not "exactly one run
happened". If a catch-up run fires immediately after `enable --now`, read its journal entry
before re-arming the other timer; the first real PostgreSQL run is the evidence.

## 5. Verification of the first actual PostgreSQL runs

For each of the first calendar-triggered runs, record:

- `systemctl show <service> -p Result -p ExecMainStatus` (must be `Result=success`,
  `ExecMainStatus=0`);
- the `journalctl` report: the purge report names counts and two timestamps and nothing
  private; the backup report names the object, its encrypted checksum and the commit record;
- the heartbeat identity in Ops Sentinel/Cronitor is the same one used before the switch
  (same unit names); a failed unit produces no success heartbeat;
- for the backup, an operator `list` against the remote shows the new point and its record
  (`deploy/offsite_backup.py list ...`) — the external service round trip stays a
  production acceptance gate, not something the local run can demonstrate.

Do not treat "the timer fired" as acceptance. The unit result and the service journal are
the evidence.

## 6. Rollback: stop the automated maintenance; do not reactivate SQLite

**The default — and, while PostgreSQL is authoritative, the only supported — recovery
response is to stop and disable the automated maintenance.** Failures are fixed forward, or
PostgreSQL is recovered from a PostgreSQL recovery point (`deploy/postgres/backup_restore.md`,
and RUNBOOK section 15 for the off-host copy). The schedule is re-armed only after the cause
has been fixed and the gates of section 3 have been observed again.

```bash
sudo systemctl disable --now house-helper-purge.timer house-helper-offsite-backup.timer
sudo systemctl stop house-helper-purge.service house-helper-offsite-backup.service || true
systemctl is-active house-helper-purge.service house-helper-offsite-backup.service \
  house-helper-purge.timer house-helper-offsite-backup.timer
systemctl --failed
```

This keeps PostgreSQL authoritative and changes no data. Nothing is deleted by it. Record
the four `inactive` lines and the `systemctl --failed` output.

**Reactivating the legacy SQLite maintenance units is prohibited as a routine or automatic
step.** The files under `deploy/systemd/legacy-sqlite/` are preserved **historical
artefacts**: they are not installed by this procedure, they are not a rollback command, and
they must not be copied to `/etc` or enabled without the authorization below. Doing so while
PostgreSQL is authoritative would:

- mutate preserved SQLite data with a retention job while PostgreSQL holds the authoritative
  records — a deletion in a database nobody is using, which no procedure can undo;
- upload stale SQLite recovery points into the shared offsite destination; and
- displace PostgreSQL recovery points through the shared 7 daily + 4 weekly retention.

That is why the earlier "file replacement plus re-arm after a manual success" text was
withdrawn. It conflicted with the frozen rollback contract:

- **RBK-01** — after the H1 handoff (C4 onward) potential divergence is assumed; there is no
  automatic lossless return, and after PostgreSQL-specific user or Telegram activity a return
  to SQLite is a separately authorized, potentially lossy operation with written Owner
  acceptance of the lost delta.
- **RBK-06** — at C7-C8 the rollback path is closed, or requires a **newly authorized
  reverse project**. A systemd file replacement is neither.

Before any SQLite maintenance unit is installed, enabled or started again, a **separate,
written, Owner-approved data-rollback procedure** must exist and must record all of the
following decisions:

1. **Which database is authoritative**, and **which recovery point is accepted** (its exact
   name, checksum and timestamp), with the RBK-01/RBK-06 phase this host is in.
2. **The accepted loss of PostgreSQL data** created after that recovery point — named as the
   categories of records affected, with the Owner's written acceptance (RBK-06 C5-C8).
3. **That the service is shut down and connections are drained**: the web service stopped,
   `pg_stat_activity` showing no `hh_*` session, no one-shot or backup running, and the four
   maintenance units inactive.
4. **That every existing PostgreSQL and SQLite recovery asset is preserved**: the local
   PostgreSQL point, the offsite objects and their commit records, and the frozen SQLite
   pair with its checksum — none overwritten, pruned or renamed by the rollback.
5. **That the shared offsite destination is either separately protected (a distinct folder
   or remote) or explicitly approved for modification**, so that PostgreSQL retention cannot
   be displaced by the rollback's own uploads.
6. **That validation and monitoring come before any schedule is re-armed**: the SQLite pair
   verified against its checksum, the legacy unit files compared with
   `deploy/systemd/legacy-sqlite/`, a manual legacy run observed, and the heartbeat identity
   confirmed — before `enable --now`.

Until such a procedure exists and is approved, section 6 stops at the four `inactive`
lines. The journaled PostgreSQL handoff records stay in place: stopping maintenance never
revokes a `CONNECT`, and a later forward re-transition repeats section 3 (the `hh_purge`
handoff is idempotent).

## 7. What this procedure does **not** do

- It does not run a migration, a restore, an enrollment or a production command.
- It does not change retention, the recovery-point format, the monitoring job identities or
  the service unit names.
- It does not delete or rewrite any H1/H2/H3 handoff record.
- It does not reactivate, install or re-arm any legacy SQLite maintenance unit.
