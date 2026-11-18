"""Restore rehearsal of an off-host recovery point: fetch the encrypted object, decrypt it with the owner's
PRIVATE age identity, unpack it into a scratch directory, check the archive and the database, say whether the
point is restorable, and remove the scratch directory. Runs on the OWNER'S OWN MACHINE, never on the VPS:

    python3 deploy/offsite_rehearsal.py --rclone-config F --remote NAME:DIR --identity-file F \\
        --work-dir DIR --expect-rows TABLE=N [--expect-rows ...] [--point NAME] [--access-mode M]

This is a separate program from deploy/offsite_backup.py on purpose. That one runs on the VPS, is given public
keys only, and a test pins that it never mentions an identity, a passphrase or a decryption. This one is the only
place the private identity is used, and nothing in deploy/systemd/ refers to it. It reuses the other program's
checks (the remote's folder and access mode, the commit record, the recovery-point tar) by importing it, so
there is one definition of each.

What it does, in this order, and stops at the first that fails:

  1. Refuses, before any command runs, unless: the identity file is a regular file of this user's, readable by
     no one else, holding an age secret key, and NOT inside the repository checkout; this host is not the
     production host (a /etc/house-helper directory exists there: the private identity must never be on it);
     the work directory is an existing absolute directory outside the checkout; the rclone configuration and
     the remote pass exactly the checks of the backup (the same --access-mode, the same fixed folder).
  2. Lists the folder and picks a COMMITTED point: the newest, or the one named by --point. An UNRESOLVED
     object, an object without a valid commit record, or one that is not in the folder is refused: it is not
     a recovery point and this tool does not rehearse it. A `.partial` or `.commit` name is never a point
     either: `--point` refuses one, and a leftover `.partial` in the folder is neither read, reported nor
     touched here (deploy/offsite_backup.py never deletes one and reports it; an operator removes it, RUNBOOK
     15.11), and it changes no status below.
  3. Downloads that one object into a new private directory under --work-dir and requires its size, SHA-256,
     SHA-1 and MD5 to equal the ones its commit record states (the record was written after a read-back at
     upload time, so this is the end-to-end check of the bytes that left the VPS).
  4. Decrypts it with `age -d -i IDENTITY` (the identity is passed as a path, never printed or logged).
  5. Reads the MANIFEST.json inside the decrypted, commit-bound archive and dispatches on the format it
     declares - never on the object's name, which has the same shape for both formats (BAK-16):
       * `house-helper-recovery-point/1` (SQLite): unpacks the tar with the backup tool's own reader (exactly
         three files, a manifest of the known shape, the snapshot equal to the manifest's size and SHA-256)
         into a fresh directory of the scratch area, checks that the manifest's time is the time in the
         point's name, then opens the snapshot READ ONLY and checks SQLite's integrity check, foreign keys,
         no free page (every point is rewritten by `VACUUM INTO`), exactly one Alembic revision, and every
         --expect-rows TABLE=N.
       * `house-helper-recovery-point/2` with `database: postgresql`: validates the manifest under the frozen
         format-v2 contract and checks every --expect-rows TABLE=N against the manifest's own counts, then
         hands the decrypted archive to the accepted WP-2D host tool (`deploy/postgres/maintenance.py
         rehearse`). That tool starts a disposable `--network none` PostgreSQL 16 server with a tmpfs PGDATA,
         restores the archive and runs section 8.6 level 2: schema revision, per-table counts and digests,
         object contract, the sequence raw pair, ownership/ACLs, the attachment loader and a real `hh_app`
         smoke on a discarded clone. Only the decrypted archive path is passed; the age identity reaches no
         argument of the tool. The tool names its one disposable container by a run id generated here; on a
         signal, a timeout or a kill, this program asks the tool to remove exactly that name and requires a
         confirmation (F2). The tool's verdict and its clean-up outcome are read from a versioned JSON
         status record this program asks it to write (`--status-file`), never from its stderr: a path or a
         message cannot create a synthetic record, and a failed or absent record is FAILED (R3-F3). A
         Docker/tooling/internal failure is exit 1, never NOT RESTORABLE (F3, R3-F2). An unknown,
         contradictory or unsupported format is refused, never converted and never retried as the other
         format.
  6. Removes the scratch directory, on every exit path including SIGTERM/SIGINT/SIGHUP. A directory that cannot
     be removed is a failure, never a pass: it holds a plaintext database or dump.

What it never does: write to the remote (it lists, reads one commit record, and downloads one object; none of
the commands that create, move or delete exists in this file), write anywhere but the new directory it makes
under --work-dir, open an existing file for writing, touch a Docker volume, a live database, `restore` or the
production configuration. It never runs a shell and never names Docker itself: for a v2 point it runs the
accepted WP-2D host tool by an absolute path, and that tool's disposable containers are the only containers
involved. It does not replace the restore of RUNBOOK 15.6 and section 10 onto a scratch volume with the image
of the manifest's tag (that also checks the schema against the application's migrations); it is the part of
the rehearsal that can run anywhere, on the point that actually left the VPS.

Nothing here prints content. Lines carry the point's name, sizes, the checksum of the ENCRYPTED object, the
manifest's image tag and time, a schema revision that is a plain identifier, and the counts you asked for. The
output of age and rclone is not forwarded.

Exit status: 0 the point was retrieved, decrypted, unpacked and verified (and the scratch directory removed);
1 failed before a verdict (rclone, age or the disk failed, the host tool could not run, Docker or its pinned
image was unavailable, an internal error, a signal, or a clean-up that could not be confirmed); 2 usage;
3 refused before anything was done (configuration, identity, host, no committed point); 4 NOT RESTORABLE: the
point was retrieved and the accepted tool's own structured refusal is about the artifact or its restored
data (its bytes differ from its record, it does not decrypt with this identity, the tar, the database or the
isolated restore does not verify, an expectation is not met) - never because Docker or another tool failed;
128+N a signal whose clean-up was confirmed. Only 0 says the point can be restored.

A v2 rehearsal owns exactly one disposable container, named by a run id generated here. Killing this process
does not remove a detached container, so a signal, a timeout or a kill is followed by a fallback: the accepted
tool is asked to remove that one name and to confirm it. Reading the helper's result, forwarding its output,
running that fallback and confirming it form ONE clean-up-responsibility boundary (R4-F1): a signal that lands
there is deferred until the resource outcome is resolved, so it can neither skip the fallback nor abandon a
removal half-way, and an unconfirmed removal is always a failure with the exact container name. The same
fallback runs when the accepted tool reports that its own verified clean-up could not be confirmed: its
clean-up failure takes precedence over the rehearsal outcome, and only after the fallback confirms the removal
may the tool's own artifact refusal stand. If the removal cannot be confirmed, the status is 1 and the
container's exact name is printed as recovery evidence; that is never a pass and never NOT RESTORABLE.

The classification is a truth table over two independent facts, each taken from the status record (never from
text or an exit status alone): the RESOURCE state (confirmed removal, possibly by the fallback) and the
ARTIFACT state (a verified restore, an artifact refusal the tool actually established, or anything else).
Confirmed removal + verified restore is the only pass; confirmed removal + an artifact refusal is the only NOT
RESTORABLE; confirmed removal + an environment/internal/ambiguous/cancelled outcome is FAILED; an unconfirmed
removal is retried and, if it stays unconfirmed, FAILED with the exact container name and recovery command.
"""

import argparse
import contextlib
import dataclasses
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence


def _load_backup_tool():
    """The sibling program deploy/offsite_backup.py, loaded by path so that the way this program is started
    (a script, `python -I`, a test) does not decide which file is meant. Already loaded: that module."""
    existing = sys.modules.get("offsite_backup")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("offsite_backup", Path(__file__).resolve().with_name("offsite_backup.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["offsite_backup"] = module
    spec.loader.exec_module(module)
    return module


core = _load_backup_tool()

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED, EXIT_NOT_RESTORABLE = 0, 1, 2, 3, 4

RUN_PREFIX = "hh-rehearsal-"
IDENTITY_MARKER = b"AGE-SECRET-KEY-1"  # what every age X25519 identity starts with; compared upper-cased
MAX_IDENTITY_BYTES = 1 << 16
MAX_EXPECTATIONS = 20
POSTGRES_DATABASE_KIND = "postgresql"
POSTGRES_RESTORE_TIMEOUT = 3600
MAX_FORWARDED_LINES = 20
# The accepted tool's exit statuses this program distinguishes. The status record is the authority;
# the exit status is only checked against it, never read on its own (R3-F3).
MAINTENANCE_INTERNAL = 1
MAINTENANCE_USAGE = 2
MAINTENANCE_REFUSED = 3
MAINTENANCE_UNAVAILABLE = 4
MAINTENANCE_CANCELLED = 6
MAINTENANCE_EXIT_STATUSES = frozenset(
    {0, MAINTENANCE_INTERNAL, MAINTENANCE_USAGE, MAINTENANCE_REFUSED, MAINTENANCE_UNAVAILABLE, MAINTENANCE_CANCELLED}
)
REHEARSAL_CLEANUP_TIMEOUT = 120
# The name `deploy/postgres/maintenance.py` gives the one container of a run: its ownership identity.
# This program never removes anything itself; it reports the name and asks the tool to remove it.
REHEARSAL_CONTAINER_PREFIX = "house-helper-restore-verify-"
# R3-F3: the accepted tool's machine-readable result contract (status contract v1). It is ONE JSON
# record in a file this program chooses, never a token parsed out of stderr: a compose path, a
# message or a tool's stderr cannot create a synthetic record. Every field is validated here.
STATUS_CONTRACT = "house-helper-maintenance-status/1"
STATUS_VALUES = frozenset({"passed", "failed", "cancelled", "usage"})
CLEANUP_VALUES = frozenset({"confirmed", "unconfirmed", "kept"})
STATUS_FIELDS = frozenset({"contract", "command", "run_id", "status", "cleanup", "code"})
STATUS_CODE_RE = re.compile(r"[a-z0-9_]{1,64}", re.ASCII)
STATUS_RUN_ID_RE = re.compile(r"[0-9a-f]{16,64}", re.ASCII)
STATUS_MAX_BYTES = 4096
# Refusal codes that are evidence ABOUT THE ARTIFACT: the archive or the restored data does not
# verify. Only these may make a retrieved point NOT RESTORABLE. Docker, image, container, work
# directory, SQL-execution and internal codes are environment/tooling failures and are exit 1.
ARTIFACT_REFUSALS = frozenset({
    # the archive itself (format-v2 contract, TOC, dump checksum, sequence payload)
    "archive_integrity", "archive_contract", "archive_sequence", "archive_manifest", "archive_members",
    "archive_unreadable", "archive_checksum", "archive_roundtrip", "archive_path", "snapshot_mismatch",
    "manifest_invalid", "checksum", "definition", "dump_empty", "toc", "table_data_selection",
    "table_name", "digest_count", "digest_value", "not_canonical",
    "facts_missing", "facts_duplicate", "facts_malformed", "facts_table", "facts_tables",
    "sequence_extraction", "sequence_wrapper", "sequence_command", "sequence_setval",
    # the isolated restore and its verification
    "isolated_restore", "isolated_verification", "isolated_smoke", "attachment_proof",
    "restore_verification",
})
# A directory whose existence means "this is the production host" (deploy/systemd/*.env.example install there).
PRODUCTION_MARKERS = (Path("/etc/house-helper"),)
TABLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
REVISION_RE = re.compile(r"[A-Za-z0-9_]{1,64}", re.ASCII)
EXPECT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]{0,63})=([0-9]{1,9})", re.ASCII)
REVISION_TABLE = "alembic_version"
SQLITE_MAGIC = b"SQLite format 3\x00"


def say(message: str) -> None:
    print(f"offsite-rehearsal: {message}", file=sys.stderr, flush=True)


def _safe_first_line(raw: bytes) -> str:
    """The first printable line of a tool's stderr, bounded. This is OPERATOR DIAGNOSTICS ONLY: it is
    never parsed and never decides a verdict (it may quote a path this program supplied)."""
    text = raw.decode("utf-8", "replace").splitlines()
    line = next((line for line in text if line.strip()), "")
    return "".join(ch for ch in line if ch.isprintable())[:300] or "no message"


def _reject_duplicate_keys(pairs: Sequence[tuple]) -> dict:
    """A JSON object hook that refuses a repeated key: two values for one field are ambiguous."""
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def read_status(path: Path, command: str, run_id: str) -> Optional[dict]:
    """The accepted tool's machine-readable outcome, or None.

    Only a complete, single, well-formed record naming THIS command and THIS run is usable. Missing
    files, a second object, a duplicate key, an unknown contract version, an unexpected field, an
    unknown status, a status/code contradiction, a different run id and a field of the wrong JSON
    type all yield None, which the caller classifies as FAILED - never NOT RESTORABLE (R3-F3,
    R4-F2)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > STATUS_MAX_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or set(value) != STATUS_FIELDS:
        return None
    if value.get("contract") != STATUS_CONTRACT or value.get("command") != command:
        return None
    if not isinstance(run_id, str) or not STATUS_RUN_ID_RE.fullmatch(run_id) or value.get("run_id") != run_id:
        return None
    status, cleanup, code = value.get("status"), value.get("cleanup"), value.get("code")
    # The membership tests below assume hashable, string-valued fields. A JSON array or object in
    # `status` or `cleanup` would raise TypeError there and escape the caller's fallback, losing
    # the container identity, so every malformed type fails closed HERE (R4-F2).
    if not isinstance(status, str) or not isinstance(cleanup, str):
        return None
    if status not in STATUS_VALUES or cleanup not in CLEANUP_VALUES:
        return None
    if status == "failed":
        if not isinstance(code, str) or not STATUS_CODE_RE.fullmatch(code):
            return None
    elif code is not None:
        return None
    return {"status": status, "cleanup": cleanup, "code": code}


class _DeferredSignals:
    """Records SIGTERM/SIGINT/SIGHUP instead of raising, for the bounded clean-up section.

    Recording (rather than ignoring) preserves the signal's outcome: the phase finishes, and the
    caller reports the cancellation afterwards. A repeated signal during the section is appended,
    never raised in the middle of a removal or a status read (R4-F1)."""

    def __init__(self) -> None:
        self.signums: List[int] = []

    def __call__(self, signum, frame) -> None:  # noqa: ARG002 (the handler signature is fixed)
        self.signums.append(signum)


@contextlib.contextmanager
def _deferred_cancellation():
    """Defer the rehearsal's cancellation signals for the duration of the block.

    This is deliberately narrow: it covers reading the accepted tool's result and the verified
    fallback removal (and that removal's own confirmation), which must not be abandoned half-way.
    Outside this section the raising handler installed by `main` applies again (R4-F1)."""
    deferred = _DeferredSignals()
    previous = {}
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(Exception):
                previous[number] = signal.signal(number, deferred)
    try:
        yield deferred
    finally:
        for number, handler in previous.items():
            with contextlib.suppress(Exception):
                signal.signal(number, handler)


def classify_status(record: Optional[dict], returncode: int) -> str:
    """`passed`, `artifact` or `failed`, from a validated record AND the exit status.

    The record is the authority and the exit status must agree with it:

    * the tool confirms its own removal (`cleanup: confirmed`): a verified restore is a pass only at
      exit 0, and an artifact code is NOT RESTORABLE only at the tool's refusal status;
    * the tool could not confirm its removal (`cleanup: unconfirmed`), so it exited 4 with the
      clean-up status and the caller's verified fallback has since confirmed the absence: the
      record's own verdict (a verified restore, or an artifact refusal it established) now stands;
    * anything absent, unknown, `kept` (never this caller) or contradictory is FAILED."""
    if record is None or returncode not in MAINTENANCE_EXIT_STATUSES:
        return "failed"
    status, cleanup, code = record["status"], record["cleanup"], record["code"]
    artifact = status == "failed" and code in ARTIFACT_REFUSALS
    if cleanup == "confirmed":
        if status == "passed":
            return "passed" if returncode == 0 else "failed"
        return "artifact" if artifact and returncode == MAINTENANCE_REFUSED else "failed"
    if cleanup == "unconfirmed" and returncode == MAINTENANCE_UNAVAILABLE:
        if status == "passed":
            return "passed"
        return "artifact" if artifact else "failed"
    return "failed"


class NotRestorable(core.OffsiteError):
    """The point was retrieved but does not verify. The message is safe to print."""

    exit_code = EXIT_NOT_RESTORABLE


# --- what is checked before anything runs ------------------------------------------------------


def _inside(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except (OSError, ValueError):
        return True  # cannot tell: treated as inside, which refuses


def _forbidden_places(deploy_dir: Path, markers: Sequence[Path]) -> List[Path]:
    """Where neither the private identity nor the scratch directory may be: the repository checkout (the
    directory that holds deploy/) and the production configuration."""
    return [deploy_dir.resolve().parent, *markers]


def check_production_host(markers: Sequence[Path]) -> None:
    for marker in markers:
        if core.is_there(marker):
            raise core.Refused(
                f"{marker} exists on this host, which marks it as the production host. The private age identity "
                "must never be on that host: run the rehearsal on your own machine (RUNBOOK 15.13)."
            )


def check_identity_file(path: Path, forbidden: Sequence[Path]) -> None:
    """The identity must be the owner's secret and treated as one. Reads the file; prints nothing from it."""
    if not core.is_plain_file(path):
        raise core.Refused("--identity-file must be an existing regular file (not a link).")
    if any(_inside(path, place) for place in forbidden):
        raise core.Refused(
            "--identity-file is inside the repository checkout or the production configuration. A private key "
            "must never be kept there: move it out of the tree."
        )
    try:
        size = path.stat().st_size
        if size > MAX_IDENTITY_BYTES:
            raise core.Refused("--identity-file is larger than any age identity file.")
        if os.name != "nt" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise core.Refused("--identity-file is readable by other users: make it readable by its owner only (chmod 600).")
        content = path.read_bytes()
    except OSError:
        raise core.Refused("--identity-file cannot be read.") from None
    if not any(line.strip().upper().startswith(IDENTITY_MARKER) for line in content.splitlines()):
        raise core.Refused("--identity-file holds no age secret key (a line AGE-SECRET-KEY-1...).")


def check_work_dir(work: Path, forbidden: Sequence[Path]) -> None:
    if not work.is_absolute() or work.is_symlink() or not work.is_dir() or not os.access(work, os.W_OK | os.X_OK):
        raise core.Refused("--work-dir must be an existing absolute directory (not a link) that this user can write.")
    if any(_inside(work, place) for place in forbidden):
        raise core.Refused("--work-dir is inside the repository checkout or the production configuration: use a scratch place elsewhere.")


def parse_expectation(text: str) -> tuple:
    match = EXPECT_RE.fullmatch(text)
    if match is None or int(match.group(2)) < 1:
        raise argparse.ArgumentTypeError("must be TABLE=N, a table name and a minimum row count of at least 1")
    return match.group(1), int(match.group(2))


# --- the database ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Verified:
    revision: str
    tables: int
    found: Dict[str, int]  # the expectations: table -> rows found


def verify_database(path: Path, expectations: Dict[str, int]) -> Verified:
    """Read-only checks of the unpacked snapshot. NotRestorable, with a message that quotes nothing from the
    database, unless it is sound, rewritten (no free page), at exactly one Alembic revision and holds what
    was asked for. Only counts are read."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(len(SQLITE_MAGIC))
    except OSError:
        raise core.Failed("the unpacked snapshot cannot be read.") from None
    if header != SQLITE_MAGIC:
        raise NotRestorable("the snapshot is not a SQLite database file.")
    connection = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        problems = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if problems != ["ok"]:
            raise NotRestorable(f"SQLite's integrity check of the snapshot failed ({len(problems)} problem(s)).")
        violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        if violations:
            raise NotRestorable(f"the snapshot has {violations} foreign-key violation(s).")
        if connection.execute("PRAGMA freelist_count").fetchone()[0] != 0:
            raise NotRestorable("the snapshot has free pages: it was not rewritten as every recovery point of this tool is.")
        tables = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        if REVISION_TABLE not in tables:
            raise NotRestorable("the snapshot has no Alembic revision table: it is not this application's database.")
        revisions = [str(row[0]) for row in connection.execute(f"SELECT version_num FROM {REVISION_TABLE}")]
        if len(revisions) != 1 or not REVISION_RE.fullmatch(revisions[0]):
            raise NotRestorable("the snapshot does not carry exactly one plain Alembic revision.")
        found: Dict[str, int] = {}
        for table, minimum in sorted(expectations.items()):
            if table not in tables or not TABLE_RE.fullmatch(table):
                raise NotRestorable(f"expected table {table} is not in the snapshot.")
            count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            found[table] = count
            if count < minimum:
                raise NotRestorable(f"expected at least {minimum} row(s) in {table}, found {count}.")
        return Verified(revisions[0], len(tables), found)
    except sqlite3.Error as failure:
        raise NotRestorable(f"SQLite could not read the snapshot ({type(failure).__name__}).") from None
    finally:
        if connection is not None:
            connection.close()


# --- the rehearsal ---------------------------------------------------------------------------


@dataclasses.dataclass
class Settings:
    remote: str
    rclone_config: Path
    identity_file: Path
    work_dir: Path
    expectations: Dict[str, int]
    point: Optional[str] = None
    access_mode: str = core.ACCESS_APP_FOLDER
    # The PostgreSQL v2 path (BAK-16): the accepted WP-2D host tool and the files it needs. None means
    # the checkout this program lives in (deploy/postgres/maintenance.py, deploy/compose.postgres.yaml,
    # tests/fixtures/pg_object_contract.json). Never contains an identity or a credential.
    postgres_maintenance_script: Optional[Path] = None
    postgres_compose_file: Optional[Path] = None
    postgres_object_contract: Optional[Path] = None
    postgres_app_image: Optional[str] = None


def choose_point(folder, requested: Optional[str], now: datetime) -> str:
    """The committed point to rehearse: the one named, or the newest not dated in the future. Refuses anything
    that is not committed, and says why when the folder knows."""
    committed = {p.name: p for p in folder.committed}
    if requested is not None:
        if not core.POINT_RE.fullmatch(requested):
            raise core.Refused("--point is not the name of a recovery point (house-helper-<UTC time>-<8 hex>.tar.age).")
        if requested in committed:
            return requested
        if requested in folder.unresolved:
            raise core.Refused(f"{requested} is UNRESOLVED ({folder.unresolved[requested]}): not a recovery point, so not rehearsed.")
        raise core.Refused(f"{requested} is not a committed recovery point in the folder.")
    usable = [p for p in committed.values() if p.moment <= now + core.FUTURE_TOLERANCE]
    if not usable:
        raise core.Refused("the folder holds no committed recovery point (nothing with a valid commit record that matches it).")
    return max(usable, key=lambda p: (p.moment, p.name)).name


class Rehearsal:
    def __init__(
        self,
        settings: Settings,
        runner: "core.Runner",
        deploy_dir: Path,
        clock: Callable[[], datetime],
        log: Callable[[str], None] = say,
        production_markers: Sequence[Path] = PRODUCTION_MARKERS,
    ) -> None:
        self.settings, self.runner, self.deploy_dir, self.clock, self.log = settings, runner, deploy_dir, clock, log
        self.markers = tuple(production_markers)
        repository = deploy_dir.resolve().parent
        self.maintenance_script = settings.postgres_maintenance_script or (deploy_dir / "postgres" / "maintenance.py")
        self.compose_file = settings.postgres_compose_file or (repository / "deploy" / "compose.postgres.yaml")
        self.object_contract = settings.postgres_object_contract or (repository / "tests" / "fixtures" / "pg_object_contract.json")
        self.tools: Dict[str, str] = {}
        self.remote = None
        self.run_dir: Optional[Path] = None

    def preflight(self) -> None:
        settings = self.settings
        forbidden = _forbidden_places(self.deploy_dir, self.markers)
        # The same offline authorization as `run` and `list` of the backup, first; only the rule that the full-disk
        # configuration must sit where root alone can change it is left out (trust=None): it is for the VPS.
        core.authorize_remote(settings.rclone_config, settings.remote, settings.access_mode, trust=None)
        check_production_host(self.markers)
        check_identity_file(settings.identity_file, forbidden)
        check_work_dir(settings.work_dir, forbidden)
        for name in ("rclone", "age"):
            found = self.runner.which(name)
            if not found:
                raise core.Refused(f"{name} is not installed on this machine.")
            self.tools[name] = found
        self.remote, has_app_folder = core.open_remote(
            self.runner, self.tools["rclone"], settings.rclone_config, settings.remote, settings.access_mode
        )
        if settings.access_mode == core.ACCESS_FULL_DISK:
            self.log(core.full_disk_notice(settings.remote, has_app_folder))

    def run(self) -> str:
        """The verdict line of a passed rehearsal. Raises OffsiteError (and Interrupted) otherwise."""
        self.preflight()
        for entry in sorted(self.settings.work_dir.iterdir()):
            if entry.name.startswith(RUN_PREFIX) and entry.is_dir() and not entry.is_symlink():
                self.log(f"WARNING: {entry.name} in the work directory is left by an earlier rehearsal and may hold a plaintext database: remove it.")
        self.run_dir = Path(tempfile.mkdtemp(prefix=RUN_PREFIX, dir=self.settings.work_dir))
        try:
            verdict = self.rehearse()
        finally:
            removed = self.clean_up()
        if not removed:
            raise core.Failed(
                f"the scratch directory {self.run_dir.name} could not be removed and holds the plaintext snapshot: "
                "remove it by hand. This is not a pass."
            )
        return verdict

    def rehearse(self) -> str:
        settings, remote, run_dir = self.settings, self.remote, self.run_dir

        self.log("step 1/5: listing the folder and choosing a committed recovery point ...")
        folder = core.classify_folder(remote.listing(), remote.read_record)
        for name, why in folder.unresolved.items():
            self.log(f"UNRESOLVED {name}: {why}. Not a recovery point; not used.")
        now = self.clock().astimezone(timezone.utc)
        name = choose_point(folder, settings.point, now)
        record = core.read_commit_record(remote.read_record(name), name)
        if record is None:  # classify_folder just read it; a record that changed since is not trusted
            raise core.Failed("the commit record of the chosen point changed while it was being read.")

        self.log(f"step 2/5: downloading {name} ({record.size} bytes) and comparing it with its commit record ...")
        sealed = run_dir / "point.tar.age"
        remote.download(name, sealed)
        if core.digests_of(sealed) != record:
            raise NotRestorable("the downloaded object is not the one its commit record describes (size or checksum differs).")
        with open(sealed, "rb") as handle:
            if handle.read(len(core.AGE_HEADER)) != core.AGE_HEADER:
                raise NotRestorable("the downloaded object is not an age file.")

        self.log("step 3/5: decrypting with the identity file (age) ...")
        tar_path = run_dir / "recovery-point.tar"
        done = self.runner.run(
            [self.tools["age"], "-d", "-i", str(settings.identity_file)],
            timeout=core.AGE_TIMEOUT, stdin_path=sealed, stdout_path=tar_path,
        )
        if done.returncode != 0:
            raise NotRestorable(
                f"age could not decrypt the point with this identity (exit {done.returncode}): it is not a recipient of this "
                "point, or the object is damaged."
            )
        self.discard(sealed)

        self.log("step 4/5: reading the archive's own manifest and dispatching on its format ...")
        try:
            declared = core.read_point_manifest(tar_path)
        except core.Failed as failure:
            raise NotRestorable(f"the decrypted archive is not a recovery point: {str(failure).rstrip('.')}.") from None
        # DISPATCH IS ON THE MANIFEST INSIDE THE ENCRYPTED, COMMIT-BOUND ARCHIVE, never on the object's
        # name: both formats use the same name shape. An unknown, contradictory or unsupported format is
        # refused; there is no fallback from one format to the other.
        if declared.get("format") == core.FORMAT:
            verdict = self.rehearse_sqlite(tar_path, name, settings, record)
        elif declared.get("format") == core.FORMAT_V2 and declared.get("database") == POSTGRES_DATABASE_KIND:
            verdict = self.rehearse_postgresql(tar_path, declared, name, record, settings)
        else:
            raise NotRestorable(
                "the archive does not declare a format this tool knows: it is neither a SQLite "
                f"({core.FORMAT}) nor a PostgreSQL ({core.FORMAT_V2}) recovery point, or its metadata is contradictory."
            )
        self.discard(tar_path)
        return verdict

    def rehearse_sqlite(self, tar_path: Path, name: str, settings: Settings, record: "core.Digests") -> str:
        """The v1 path, unchanged: the accepted tar reader and the read-only SQLite checks."""
        self.log("step 5/5: unpacking the SQLite recovery point and checking it read-only ...")
        restored = self.run_dir / "restored"
        restored.mkdir()
        try:
            manifest = core.read_recovery_tar(tar_path, extract_to=restored)
        except OSError:
            raise core.Failed("the recovery-point tar cannot be read or the scratch files cannot be written.") from None
        except core.Failed as failure:
            raise NotRestorable(f"the decrypted archive is not a recovery point: {str(failure).rstrip('.')}.") from None
        if core.POINT_RE.fullmatch(name).group(1) != manifest["created_utc"]:
            raise NotRestorable("the time in the manifest is not the time in the point's name.")
        verified = verify_database(restored / core.SNAPSHOT_NAME, settings.expectations)
        wanted = ", ".join(f"{table}>={settings.expectations[table]} (found {count})" for table, count in sorted(verified.found.items()))
        return (
            f"rehearsal PASSED: {name} (sha256 of the encrypted object {record.sha256}) matched its commit record, decrypted with the "
            f"supplied identity, unpacked and verified in a scratch directory that is now removed. Made {manifest['created_utc']} by image "
            f"{manifest['image_tag']}; schema revision {verified.revision}; {verified.tables} table(s); expected rows: {wanted}. "
            "Nothing was written to the remote or to any production location."
        )

    def rehearse_postgresql(self, tar_path: Path, declared: dict, name: str, record: "core.Digests", settings: Settings) -> str:
        """The v2 path (BAK-16): validate the manifest under the frozen contract, check the expectations
        against the manifest's own counts, then hand the archive to the accepted WP-2D isolated restore,
        which starts a disposable `--network none` PostgreSQL 16 server and runs section 8.6 level 2 (digests,
        object contract, sequence pair, ACLs, attachment loader and the real `hh_app` smoke). The age
        identity stops at this machine: only the decrypted archive path reaches that tool."""
        pg_backup = self._postgresql_contract()
        failures = pg_backup.manifest_failures(declared)
        if failures:
            raise NotRestorable(
                "the archive's PostgreSQL manifest fails the frozen format-v2 contract "
                f"({', '.join(failures[:6])}{'...' if len(failures) > 6 else ''})."
            )
        tables = declared["tables"]
        found: Dict[str, int] = {}
        for table, minimum in sorted(settings.expectations.items()):
            fact = tables.get(table)
            rows = fact.get("rows") if isinstance(fact, dict) else None
            if not isinstance(rows, int) or isinstance(rows, bool) or rows < minimum:
                raise NotRestorable(
                    f"expected at least {minimum} row(s) in {table}, but the archive's manifest does not record that many."
                )
            found[table] = rows
        for label, path in (
            ("deploy/postgres/maintenance.py", self.maintenance_script),
            ("the PostgreSQL compose file", self.compose_file),
            ("the object-contract fixture", self.object_contract),
        ):
            if not core.is_plain_file(path):
                raise core.Failed(
                    f"{label} is missing from this checkout, so the format-v2 point cannot be restored and verified. "
                    "Nothing was written to the remote."
                )
        self.log("step 5/5: isolated PostgreSQL 16 restore and verification (maintenance.py rehearse) ...")
        work = self.run_dir / "restore-work"
        work.mkdir(mode=0o700)
        # One run id, generated here, is the container's ownership identity. The tool names its one
        # disposable container with it; the fallback below removes exactly that name and nothing else.
        run_id = secrets.token_hex(16)
        # R3-F3: the result is read from a file this program chooses, never parsed out of stderr.
        status_file = self.run_dir / "maintenance-status.json"
        cleanup_file = self.run_dir / "cleanup-status.json"
        argv = [sys.executable, str(self.maintenance_script)]
        argv += ["--compose-file", str(self.compose_file)]
        argv += ["--object-contract", str(self.object_contract)]
        argv += ["--work-dir", str(work)]
        argv += ["rehearse", "--run-id", run_id, "--status-file", str(status_file), str(tar_path)]
        if settings.postgres_app_image:
            argv += ["--app-image", settings.postgres_app_image]
        try:
            done = self.runner.run(argv, timeout=POSTGRES_RESTORE_TIMEOUT, capture=True)
        except core.Interrupted:
            # This process was signalled; the runner has already asked the child to stop (and killed
            # it after a grace period). Killing the child does not remove a detached container: ask
            # the accepted tool to remove this run's one container, and require confirmation. A
            # second signal must not abandon that removal or its confirmation (R4-F1).
            if not self.stop_rehearsal_container_deferred(run_id, cleanup_file):
                raise core.Failed(self.unremoved_container_message(run_id)) from None
            raise
        except core.Failed as failure:
            # A timeout (or the child could not be started): stop() has already asked it to terminate
            # and, after the grace period, killed it. Its own cleanup may not have run.
            if not self.stop_rehearsal_container_deferred(run_id, cleanup_file):
                raise core.Failed(f"{failure} {self.unremoved_container_message(run_id)}") from None
            raise core.Failed(f"{failure} The disposable container of this run was confirmed removed.") from None
        # R4-F1: ONE clean-up-responsibility boundary spans the whole helper-result/fallback phase
        # - forwarding the helper's stdout, reading its status record, running the verified fallback
        # and confirming that fallback. Before this boundary a signal raised by the ordinary handler
        # escaped `Rehearsal.run`'s `finally`, which removes only the scratch directory: the helper's
        # container was left unaccounted for and no recovery identity was printed. A signal that lands
        # in this phase is recorded here and preserved after the resource outcome is resolved.
        with _deferred_cancellation() as deferred:
            for line in done.stdout.decode("utf-8", "replace").splitlines()[:MAX_FORWARDED_LINES]:
                safe = "".join(ch for ch in line if ch.isprintable())[:300]
                if safe:
                    self.log(f"  | {safe}")
            outcome = read_status(status_file, "rehearse", run_id)
            if outcome is None:
                # Diagnostics only: this line is never parsed and never decides the classification.
                self.log(f"  | maintenance.py stderr: {_safe_first_line(done.stderr)}")
            # Confirmed clean-up means the tool's own record says so AND it exited normally: a helper
            # killed by a signal cannot confirm anything about a container it no longer controls.
            cleanup_confirmed = (
                outcome is not None and outcome["cleanup"] == "confirmed" and done.returncode >= 0
            )
            if not cleanup_confirmed:
                # The helper terminated abnormally or its clean-up is uncertain: independently ask the
                # accepted tool to remove exactly this run's container and require a confirmation.
                if not self.stop_rehearsal_container(run_id, cleanup_file):
                    reason = f" Its own recorded outcome was [{outcome['code']}]." if outcome and outcome["code"] else ""
                    raise core.Failed(self.unremoved_container_message(run_id) + reason) from None
            if deferred.signums:
                # The removal (the helper's own, or the fallback's) is confirmed, so the resource is
                # accounted for; the cancellation is preserved and the run can never report a pass.
                raise core.Interrupted(deferred.signums[0])
        verdict = classify_status(outcome, done.returncode)
        if verdict == "artifact":
            raise NotRestorable(
                "the accepted isolated PostgreSQL restore did not verify the point "
                f"(maintenance.py rehearse refused [{outcome['code']}]); the disposable container "
                "of this run is confirmed removed."
            )
        if verdict != "passed":
            raise core.Failed(self.unverified_message(outcome, done.returncode))
        manifest = declared
        sequence = manifest["sequence"]
        total = sum(int(fact["rows"]) for fact in tables.values())
        wanted = ", ".join(f"{table}>={settings.expectations[table]} (manifest {count})" for table, count in sorted(found.items()))
        return (
            f"rehearsal PASSED: {name} (sha256 of the encrypted object {record.sha256}) matched its commit record, decrypted with the "
            "supplied identity, and was restored and verified in an isolated --network none PostgreSQL 16 container by the accepted "
            f"WP-2D tool. Schema revision {manifest['alembic_revision']}; {len(tables)} table(s), {total} row(s); sequence "
            f"({sequence['last_value']}, {str(sequence['is_called']).lower()}); expected rows: {wanted}. The age identity never left "
            "this machine; nothing was written to the remote or to any production location."
        )

    def unverified_message(self, outcome: Optional[dict], returncode: int) -> str:
        """Why a run whose container is confirmed gone is still not a pass. Only validated tokens
        from the status record reach this text; the tool's stderr is quoted nowhere here."""
        if outcome is None:
            return (
                "the isolated PostgreSQL restore could not be run to a verdict "
                f"(maintenance.py rehearse exit {returncode}): it left no usable machine-readable "
                "outcome, the point was not shown to be defective and was not verified."
            )
        if outcome["status"] == "passed":
            return (
                "the isolated PostgreSQL restore reported a verified restore but exited "
                f"{returncode}: the result is contradictory, the point was not verified and the run "
                "is not a pass."
            )
        if outcome["status"] == "cancelled":
            return (
                "the isolated PostgreSQL restore was interrupted by a signal; the point was not "
                "verified and the run is not a pass."
            )
        if outcome["status"] == "usage":
            return (
                "the isolated PostgreSQL restore was refused before it ran; the point was not "
                "verified and the run is not a pass."
            )
        return (
            "the isolated PostgreSQL restore could not be run to a verdict "
            f"(maintenance.py rehearse exit {returncode}, [{outcome['code']}]); the point was not "
            "shown to be defective and was not verified."
        )

    def stop_rehearsal_container_deferred(self, run_id: str, status_file: Path) -> bool:
        """The verified fallback with cancellation deferred across its execution and confirmation.

        Used where a cancellation is already being handled: a second signal must not abandon the
        removal half-way or skip the record that confirms it (R4-F1)."""
        with _deferred_cancellation():
            return self.stop_rehearsal_container(run_id, status_file)

    def stop_rehearsal_container(self, run_id: str, status_file: Path) -> bool:
        """The verified fallback clean-up (F2/R3-F1): ask the accepted tool to remove the ONE
        disposable container of this run and confirm it is gone.

        The fallback is used whenever the child terminated abnormally or its own clean-up is
        uncertain. Only the run id is passed; the tool removes a container only under that exact
        name. Success requires the tool's exit 0 AND its machine-readable `cleanup-rehearsal` record
        naming this run and reporting a confirmed absence: an exit status alone is never a
        confirmation of clean-up (R3-F3)."""
        argv = [sys.executable, str(self.maintenance_script)]
        argv += ["--compose-file", str(self.compose_file)]
        argv += ["cleanup-rehearsal", "--run-id", run_id, "--status-file", str(status_file)]
        try:
            done = self.runner.run(argv, timeout=REHEARSAL_CLEANUP_TIMEOUT, capture=True)
        except (core.Failed, core.Interrupted, OSError):
            # A signal while the fallback itself is removing defers to the tool; this program
            # reports FAILED with the container identity rather than claiming a clean-up.
            return False
        if done.returncode != 0:
            return False
        record = read_status(status_file, "cleanup-rehearsal", run_id)
        return record is not None and record["status"] == "passed" and record["cleanup"] == "confirmed"

    def unremoved_container_message(self, run_id: str) -> str:
        """Non-secret recovery evidence: the exact container name of this run and the command that
        removes it. Nothing here is a credential or a path inside a secret."""
        name = f"{REHEARSAL_CONTAINER_PREFIX}{run_id}"
        return (
            "The disposable PostgreSQL container of this run could not be confirmed removed and it may still "
            f"hold plaintext data. It is named {name}; take no further rehearsal until it is gone. Remove it with "
            f"`python3 {self.maintenance_script} --compose-file {self.compose_file} cleanup-rehearsal "
            f"--run-id {run_id}` and confirm the tool reports exit 0."
        )

    def _postgresql_contract(self):
        """The frozen format-v2 module from this checkout, imported only when a v2 point is rehearsed."""
        repository = self.deploy_dir.resolve().parent
        if str(repository) not in sys.path:
            sys.path.insert(0, str(repository))
        try:
            from house_helper.persistence import pg_backup  # noqa: PLC0415 (the v2 path only)
        except ImportError:
            raise core.Failed(
                "the format-v2 contract module (house_helper.persistence.pg_backup) is not importable from this checkout: "
                "the point cannot be validated. Nothing was written to the remote."
            ) from None
        return pg_backup

    def discard(self, path: Path) -> None:
        """Remove a plaintext or ciphertext scratch file as soon as it is not needed. Best effort: the clean-up removes the rest."""
        with contextlib.suppress(OSError):
            path.unlink()

    def clean_up(self) -> bool:
        """Remove the scratch directory, whatever happened. Ignores further signals; never raises. Whether it is gone."""
        previous = {}
        for number in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if number is not None:
                with contextlib.suppress(Exception):
                    previous[number] = signal.signal(number, signal.SIG_IGN)
        try:
            if self.run_dir is None:
                return True
            shutil.rmtree(self.run_dir, ignore_errors=True)
            gone = not core.is_there(self.run_dir)
            if not gone:
                self.log(f"WARNING: could not remove {self.run_dir}; it holds a plaintext database. Remove it by hand.")
            return gone
        finally:
            for number, handler in previous.items():
                with contextlib.suppress(Exception):
                    signal.signal(number, handler)


# --- command line ------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        say(f"usage: {message}")
        raise SystemExit(EXIT_USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="offsite_rehearsal.py", allow_abbrev=False,
        description="Restore rehearsal of an off-host recovery point, on the owner's own machine (never the VPS).",
    )
    parser.add_argument("--rclone-config", required=True, type=Path, help="an rclone config file of the remote, mode 0600")
    parser.add_argument("--remote", required=True, help="NAME:FOLDER, as for the backup")
    parser.add_argument(
        "--access-mode", choices=core.ACCESS_MODES, default=core.ACCESS_APP_FOLDER,
        help="as for the backup: the same mode the points were made in",
    )
    parser.add_argument("--identity-file", required=True, type=Path, help="the age private identity (never on the VPS, never in the repository)")
    parser.add_argument("--work-dir", required=True, type=Path, help="an existing absolute scratch directory outside the checkout")
    parser.add_argument(
        "--expect-rows", required=True, action="append", type=parse_expectation, metavar="TABLE=N",
        help=f"the restored database must have this table with at least N rows (N >= 1); at least one, at most {MAX_EXPECTATIONS}",
    )
    parser.add_argument("--point", help="a committed point to rehearse (default: the newest)")
    parser.add_argument(
        "--postgres-maintenance-script", type=Path, default=None,
        help="the accepted WP-2D host tool for a PostgreSQL v2 point (default: deploy/postgres/maintenance.py of this checkout)",
    )
    parser.add_argument(
        "--postgres-compose-file", type=Path, default=None,
        help="the compose file maintenance.py reads the pinned client image from (default: deploy/compose.postgres.yaml)",
    )
    parser.add_argument(
        "--postgres-object-contract", type=Path, default=None,
        help="the checked-in object contract of the point's revision (default: tests/fixtures/pg_object_contract.json)",
    )
    parser.add_argument(
        "--postgres-app-image", default=None,
        help="the application image for the isolated alembic check and attachment proof (default: maintenance.py's own)",
    )
    return parser


def _signal_handler(signum, frame):
    raise core.Interrupted(signum)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runner: Optional["core.Runner"] = None,
    clock: Optional[Callable[[], datetime]] = None,
    deploy_dir: Optional[Path] = None,
    log: Callable[[str], None] = say,
    production_markers: Sequence[Path] = PRODUCTION_MARKERS,
) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)
    if len(args.expect_rows) > MAX_EXPECTATIONS:
        log("usage: too many --expect-rows.")
        return EXIT_USAGE
    expectations = dict(args.expect_rows)
    if len(expectations) != len(args.expect_rows):
        log("usage: a table is named twice in --expect-rows.")
        return EXIT_USAGE
    os.umask(0o077)
    installed = {}
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(Exception):
                installed[number] = signal.signal(number, _signal_handler)
    try:
        settings = Settings(
            remote=args.remote, rclone_config=args.rclone_config, identity_file=args.identity_file, work_dir=args.work_dir,
            expectations=expectations, point=args.point, access_mode=args.access_mode,
            postgres_maintenance_script=args.postgres_maintenance_script,
            postgres_compose_file=args.postgres_compose_file,
            postgres_object_contract=args.postgres_object_contract,
            postgres_app_image=args.postgres_app_image,
        )
        verdict = Rehearsal(
            settings, runner or core.Runner(), deploy_dir or Path(__file__).resolve().parent,
            clock or (lambda: datetime.now(timezone.utc)), log, production_markers,
        ).run()
        log(verdict)
        return EXIT_OK
    except core.OffsiteError as failure:
        label = "refused" if isinstance(failure, core.Refused) else "NOT RESTORABLE" if isinstance(failure, NotRestorable) else "FAILED"
        log(f"{label}: {failure}")
        return failure.exit_code
    except core.Interrupted as stop:
        log(f"interrupted by signal {stop.signum}; the clean-up ran.")
        return 128 + stop.signum
    except Exception as failure:  # the text of an unexpected exception can quote paths: class only
        log(f"FAILED: unexpected {type(failure).__name__}.")
        return EXIT_FAILED
    finally:
        for number, handler in installed.items():
            with contextlib.suppress(Exception):
                signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
