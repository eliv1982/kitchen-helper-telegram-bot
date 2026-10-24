"""Encrypted off-host backup of the House Helper database: take a verified backup, rewrite it so
that deleted rows leave no trace, pack it with its checksum and a manifest into ONE recovery
point, encrypt it for a public key, upload it, check the copy that arrived, and keep 7 daily and
4 weekly points. Runs on the VPS host (Python 3.10+, standard library only):

    python3 deploy/offsite_backup.py run  --tag TAG --recipients-file F --rclone-config F --remote NAME:DIR
    python3 deploy/offsite_backup.py list --rclone-config F --remote NAME:DIR        (read only)
    python3 deploy/offsite_backup.py unpack RECOVERY.tar --output-dir DIR       (on the machine that decrypts)

`run` is the whole job and what the opt-in systemd service starts (deploy/systemd/house-helper-offsite-
backup.*, RUNBOOK section 15). It uses what already exists and rewrites none of it:

  1. `deploy/backup.sh` takes the backup into a private directory: SQLite's online backup, checked and
     verified by a disposable container, published as a NAME.db / NAME.db.sha256 pair. This tool
     re-checks that pair byte for byte (the rule of RUNBOOK section 10).
  2. `dbtool sanitize`, in a disposable container with no network and a read-only root, rewrites the
     snapshot (VACUUM INTO: live rows only). SQLite does not erase a deleted row unless it was built or
     told to (secure_delete): the bytes stay in the file, and the online backup copies them. The
     production image's SQLite is built with it on, so rows its own connections delete are already
     zeroed; the rewrite does not depend on that (older deletes, other builds, free pages). A second
     disposable container runs `dbtool verify --require-head` on the result, and its SHA-256 on the host
     must equal the one the tool reported.
  3. One tar holds MANIFEST.json, snapshot.db.sha256 (the one-line format of section 10) and snapshot.db.
  4. `age` encrypts it for the PUBLIC keys in a recipients file. No private key is needed here, and a
     recipients file that holds one is refused.
  5. `rclone` uploads it under NAME.partial, reads the object list (size, and every checksum the
     remote offers), downloads it again and compares SHA-256, renames it to NAME, and checks the
     listing of NAME once more. NAME is then a CANDIDATE, not yet a recovery point.
  6. The commit: a small record NAME.commit (the object's name, size, SHA-256, SHA-1 and MD5) is
     uploaded and read back. A recovery point is an object NAME together with a valid NAME.commit
     whose size AND MD5 are the size and MD5 that the remote's listing reports for NAME. Yandex Disk
     lists MD5 only, and a listing that gives no MD5 for the object (or another one) never makes a
     point, whatever its size: the shape of a name is never proof, and neither is a size. The
     SHA-256 in the record was checked once, by the read-back of step 5; later runs do not read the
     object again, so what they re-check is size and MD5, and nothing is said about the SHA-256 of
     an object after the upload. An object without a valid, matching record (killed between 5 and 6,
     a rename that damaged it, a record that could not be confirmed, an object changed afterwards,
     a listing without its MD5) is UNRESOLVED: never counted, never relied on, never deleted by this
     tool, reported by every run (exit 6) and by `list` until an operator has looked at it (RUNBOOK 15.7).
  7. The final check, then retention. The folder is listed once more and classified as in 6. If that
     cannot be done, or does not show this run's point as committed, the run ends with exit 7 and
     deletes nothing; the point is not called stored. Retention runs only after the final check
     and never on a failed run (below).

What is promised, and what is not:

  * A failed run deletes nothing that is not its own. The only remote objects a run ever deletes are
    (a) its own NAME.partial when it fails, (b) NAME.partial files an earlier killed run left, (c) its
    own NAME if the listing right after the rename proves that the rename damaged it, and (d) points
    (the object, then its record) that retention drops AFTER the new point has been committed, and
    records whose object is gone. An unresolved object is never deleted by the tool, and while one
    exists (d) shrinks to the records whose object is gone: no committed point is dropped.
    Every delete goes through one function that accepts only a name of this tool's own shape
    (house-helper-<UTC time>-<8 hex>.tar.age[.partial|.commit]) inside the one configured folder,
    one object at a time (`rclone deletefile`, never delete/purge/sync/cleanup, nothing on the
    account's trash). No delete is reported as done on the strength of an exit status: the folder is
    listed again and the object must be absent. Whatever cannot be confirmed gone is reported (exit 6).
  * Retention: of the committed points in the folder (size and MD5 bound to their records; an unresolved
    object takes no slot), keep the newest of each of the 7 most recent UTC days that have one, plus the
    newest of each of the 4 most recent ISO weeks whose newest point is not already kept as a daily one.
    The point just made and the newest one are never dropped. Nothing is deleted unless the final check
    has confirmed the point just made. And no committed point is dropped while ANY unresolved object
    exists in the folder, however old the point: a final-looking object that is not understood may be
    the copy an operator needs, so nothing older is removed until an operator has dealt with it
    (RUNBOOK 15.11). New points are still made and verified; orphaned commit records and leftover
    partial uploads are still tidied.
    --max-deletions (default 5) is ONE budget of the run, shared by the leftover partial uploads it
    removes and the points and orphaned records retention drops; retention that would exceed what is
    left of it deletes nothing and fails (exit 6). Objects that are not of the tool's shape are never
    touched.
  * The clock. Names carry the host's clock, which retention then trusts to order points. A committed
    point dated later than the host clock (plus an hour), a point dated more than a week after the
    previous one, or a clock that moved by hours during the run, stops ALL deleting by retention for
    that run (exit 6), and the future-dated point is neither counted nor deleted: it cannot take the
    place of a real point, and it is reported by every run until an operator has looked at it.
  * Deleted conversations: the live purge removes them from the database, the rewrite keeps them out
    of the snapshot, but a point made BEFORE a deletion still holds them until retention drops it:
    about 36 days at most with a daily schedule. Hard delete on the remote is requested
    (--yandex-hard-delete) so that the provider's trash does not keep a dropped point.
  * Temporary plaintext (the backup pair, the rewritten snapshot, the tar) lives in a private 0700
    directory, each file removed as soon as the next stage no longer needs it, the directory removed
    on every exit path including SIGTERM/SIGINT/SIGHUP, and leftovers of a killed run swept at the next
    start. Removal is unlink, not erasure: file systems, journals, SSD wear levelling and the Docker
    volume of the disposable containers may keep the bytes. Put the work directory on tmpfs (the unit
    does) and treat the host disk as holding plaintext for a short time. A directory that cannot be
    removed is never a success: after a committed backup the run ends with exit 6, after any other
    outcome its own status stands, and a run REFUSES to start (exit 3, nothing done) while a leftover
    directory of an earlier run cannot be removed.
  * Nothing here prints content. Log lines carry step names, object names, sizes, checksums of the
    ENCRYPTED object and counts. The output of age and rclone is not forwarded (rclone's can name
    paths and accounts); the exit status is. Run the failing command by hand to see its own message.
  * One run at a time (flock on a lock file in the work directory).
  * Fail closed on the remote. Before anything is done the tool refuses unless (a) the rclone config
    section of the remote is a Yandex Disk remote with its OWN OAuth application (client_id, client_secret,
    token) and `app_folder = true`; (b) this rclone knows the Yandex `app_folder` option (`rclone config
    providers`; stable 1.75.2 does not, the first stable release that does is planned as 1.76.0), because an
    rclone that ignores the setting would address the whole disk; and (c) the remote renames server-side
    (`rclone backend features`), because the commit starts with a rename. It never falls back to another
    access mode.

Exit status: 0 stored, verified, final check passed, retention done, nothing needing review; 1 failed
(no new point; nothing of another run touched; an object may remain UNRESOLVED, see above); 2 usage;
3 refused before anything was done (configuration, missing tool, a leftover work directory that cannot
be removed); 6 the new point is committed and the final check confirmed it, but something needs
attention: retention did not complete, plaintext could not be removed, an unresolved or future-dated
object exists (safe: extra objects remain, and no committed point is pruned while an unresolved one
does); 7 NOT CONFIRMED: this run wrote the point and its commit
record, but the final check (the listing and classification of the folder afterwards) failed or does not
show the point as committed. It is not reported as stored, nothing is deleted, and the operator runs
`list` (RUNBOOK 15.7, 15.11); 128+N a signal. Only 0 means "stored and verified" with nothing left to look at;
6 says the point is stored and verified; 7 never does.
"""

import argparse
import configparser
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set

try:
    import fcntl
except ImportError:  # Windows: the tests run there; this tool runs on the Linux host
    fcntl = None

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED, EXIT_RETENTION, EXIT_UNCONFIRMED = 0, 1, 2, 3, 6, 7

FORMAT = "house-helper-recovery-point/1"
SNAPSHOT_NAME = "snapshot.db"
CHECKSUM_NAME = "snapshot.db.sha256"
MANIFEST_NAME = "MANIFEST.json"
TAR_MEMBERS = (MANIFEST_NAME, CHECKSUM_NAME, SNAPSHOT_NAME)  # the order they are written in
AGE_HEADER = b"age-encryption.org/v1\n"

KEEP_DAILY = 7
KEEP_WEEKLY = 4
DEFAULT_MAX_DELETIONS = 5

# What is implausible about a clock, judged by the points in the folder. A point dated later than the
# host clock plus FUTURE_TOLERANCE, a point dated more than MAX_FORWARD_GAP after the previous one (a week:
# the schedule is daily), or a clock that moved by more than MAX_RUN_DURATION during one run (the unit
# stops a run after two hours), stops every deletion by retention for the run.
FUTURE_TOLERANCE = timedelta(hours=1)
MAX_FORWARD_GAP = timedelta(days=7)
MAX_RUN_DURATION = timedelta(hours=3)

COMMIT_FORMAT = "house-helper-commit/1"
COMMIT_SUFFIX = ".commit"
MAX_RECORD_BYTES = 1024  # a record is about 400 bytes; a larger object of that name is not one

MAX_DATABASE_BYTES = 1 << 30  # dbtool.MAX_DATABASE_BYTES: a larger snapshot cannot be restored
MAX_STDERR_BYTES = 1 << 16
SPACE_HEADROOM = 64 << 20
MAX_LISTING_BYTES = 8 << 20
CHUNK = 1 << 20

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
RUN_PREFIX = "hh-offsite-"
LOCK_NAME = ".house-helper-offsite.lock"

# One rule for what this tool may name, create and delete on the remote. Anything else in the folder
# is somebody else's and is left alone.
POINT_PATTERN = r"house-helper-([0-9]{8}T[0-9]{6}Z)-([0-9a-f]{8})\.tar\.age"
POINT_RE = re.compile(POINT_PATTERN, re.ASCII)
PARTIAL_RE = re.compile(POINT_PATTERN + r"\.partial", re.ASCII)
COMMIT_RE = re.compile(POINT_PATTERN + r"\.commit", re.ASCII)
SHA1_RE = re.compile(r"[0-9a-f]{40}", re.ASCII)
MD5_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
# NAME:DIR with a folder of its own, 1 to 4 plain components. Never a bare remote (its root), a local
# path, a connection string (:backend:) or a path that climbs.
REMOTE_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}:[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}){0,3}",
    re.ASCII,
)
TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", re.ASCII)
BACKUP_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}\.db", re.ASCII)  # backup.sh NAME_PATTERN
# An X25519 age public key: "age1" and 58 characters of bech32. age checks the checksum itself.
RECIPIENT_RE = re.compile(r"age1[023456789acdefghjklmnpqrstuvwxyz]{58}", re.ASCII)
PRIVATE_KEY_MARKER = b"AGE-SECRET-KEY-"  # what every age X25519 private key starts with (bech32 is case-insensitive: compared upper-cased)
SHA256_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)

# The same isolation as the verification in backup.sh: no network, no volume of the data, a read-only
# root, no capability, and /staging for the scratch copies.
DOCKER_ISOLATION = (
    "run", "--rm", "-i", "--init", "--network", "none", "--read-only", "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges", "--log-driver", "none",
    "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m,uid=10001,gid=10001", "-v", "/staging",
)
DBTOOL = "/opt/house-helper/ops/dbtool.py"

BACKUP_TIMEOUT = 1800
CONTAINER_TIMEOUT = 1800
AGE_TIMEOUT = 1800
RCLONE_QUICK_TIMEOUT = 300
RCLONE_TRANSFER_TIMEOUT = 3600
CLEANUP_TIMEOUT = 120
MAX_PROVIDERS_BYTES = 16 << 20


# --- errors ------------------------------------------------------------------------------------


class OffsiteError(Exception):
    exit_code = EXIT_FAILED


class Refused(OffsiteError):
    """Nothing has been done: the configuration or the host is not one this tool will work with."""

    exit_code = EXIT_REFUSED


class Failed(OffsiteError):
    """A step failed. The message is safe to print."""


class Unverifiable(Failed):
    """The remote's listing does not offer the checksum that ties an object to its commit record."""


class RetentionIncomplete(OffsiteError):
    """The new recovery point is committed and the final check confirmed it; the housekeeping after it did not complete."""

    exit_code = EXIT_RETENTION


class Unconfirmed(OffsiteError):
    """The commit record was written, but the final check of the folder could not confirm the point: it is not
    reported as stored, and nothing is deleted."""

    exit_code = EXIT_UNCONFIRMED


class Interrupted(BaseException):
    """A signal arrived. BaseException so that no `except Exception` swallows it."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def say(message: str) -> None:
    print(f"offsite-backup: {message}", file=sys.stderr, flush=True)


# --- files and checksums -----------------------------------------------------------------------


def open_exclusive(path: Path) -> BinaryIO:
    """A new private (0600) file for writing; an existing one is an error, never replaced."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


@dataclasses.dataclass(frozen=True)
class Digests:
    size: int
    sha256: str
    sha1: str
    md5: str


def digests_of(path: Path) -> Digests:
    sha256, sha1, md5 = hashlib.sha256(), hashlib.sha1(), hashlib.md5(usedforsecurity=False)  # noqa: S324
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            size += len(chunk)
            sha256.update(chunk)
            sha1.update(chunk)
            md5.update(chunk)
    return Digests(size, sha256.hexdigest(), sha1.hexdigest(), md5.hexdigest())


def sha256_of(path: Path) -> str:
    return digests_of(path).sha256


def checksum_line(digest: str, name: str) -> bytes:
    """The one line of a checksum file (RUNBOOK section 10): digest, two spaces, name, newline."""
    return f"{digest}  {name}\n".encode("ascii")


def is_plain_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


# --- the command runner ------------------------------------------------------------------------


@dataclasses.dataclass
class Result:
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class Runner:
    """Runs the external commands (bash, docker, age, rclone). Tests substitute their own."""

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        stdin_path: Optional[Path] = None,
        stdout_path: Optional[Path] = None,
        capture: bool = False,
        env_extra: Optional[Mapping[str, str]] = None,
    ) -> Result:
        """Run `argv` without a shell. stdin comes from `stdin_path` (or nothing); stdout goes to the
        NEW file `stdout_path`, or is returned if `capture`, or is dropped. stderr is returned,
        bounded. On a timeout, or when an exception such as Interrupted passes through, the command is
        asked to stop (SIGTERM, which `docker run` forwards to its container) and then killed."""
        environment = {k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")}
        environment.update(env_extra or {})
        with contextlib.ExitStack() as stack:
            stdin = stack.enter_context(open(stdin_path, "rb")) if stdin_path is not None else subprocess.DEVNULL
            if stdout_path is not None:
                stdout = stack.enter_context(open_exclusive(stdout_path))
            else:
                stdout = subprocess.PIPE if capture else subprocess.DEVNULL
            process = subprocess.Popen(
                list(argv), stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, env=environment
            )
            try:
                out, err = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._stop(process)
                raise Failed(f"{Path(argv[0]).name} did not finish within {int(timeout)} s and was stopped.") from None
            except BaseException:
                self._stop(process)
                raise
        return Result(process.returncode, out or b"", (err or b"")[:MAX_STDERR_BYTES])

    @staticmethod
    def _stop(process: "subprocess.Popen") -> None:
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            process.wait()


# --- retention ---------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RecoveryPoint:
    name: str
    moment: datetime  # UTC, from the name


def parse_point_name(name: str) -> Optional[RecoveryPoint]:
    """The recovery point a final object name stands for, or None if it is not one of this tool's."""
    match = POINT_RE.fullmatch(name)
    if match is None:
        return None
    try:
        moment = datetime.strptime(match.group(1), STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return RecoveryPoint(name, moment)


@dataclasses.dataclass
class Plan:
    keep: Dict[str, str]  # name -> why
    drop: List[RecoveryPoint]  # oldest first
    suspect: List[RecoveryPoint] = dataclasses.field(default_factory=list)  # dated in the future: neither kept nor dropped


def plan_retention(
    points: Sequence[RecoveryPoint],
    protect: Set[str] = frozenset(),
    daily: int = KEEP_DAILY,
    weekly: int = KEEP_WEEKLY,
    now: Optional[datetime] = None,
) -> Plan:
    """Which of `points` to keep. Pure: no I/O, and the only clock is the `now` the caller passes, so
    the same arguments always give the same answer.

    daily:  the newest point of each of the `daily` most recent UTC days that have one;
    weekly: the newest point of each ISO week, newest week first, that is not already kept as a daily
            point, until `weekly` have been added;
    always: the names in `protect` that are in `points`.
    The newest point is the newest of its day, so it is always kept.

    With `now`, a point dated later than now + FUTURE_TOLERANCE cannot be a real point of this schedule
    (the host clock was wrong when it was made, or is wrong now). It is set aside in `suspect`: it does
    not take a daily or weekly place from a real point, and it is not dropped either, because this
    function cannot know which clock is right. Callers must not delete anything while `suspect` is not
    empty (clock_concerns says why)."""
    suspect = [] if now is None else [p for p in points if p.moment > now + FUTURE_TOLERANCE]
    if suspect:
        points = [p for p in points if p not in suspect]
    ordered = sorted(points, key=lambda p: (p.moment, p.name), reverse=True)
    keep: Dict[str, str] = {}
    days: Set[date] = set()
    for point in ordered:
        day = point.moment.date()
        if day in days:
            continue
        if len(days) >= daily:
            break
        days.add(day)
        keep[point.name] = "daily"
    weeks: Set[tuple] = set()
    added = 0
    for point in ordered:
        if added >= weekly:
            break
        week = tuple(point.moment.isocalendar())[:2]
        if week in weeks:
            continue
        weeks.add(week)  # `point` is the newest of its week
        if point.name not in keep:
            keep[point.name] = "weekly"
            added += 1
    known = {p.name for p in points}
    for name in sorted(protect & known):
        keep.setdefault(name, "protected")
    return Plan(keep, [p for p in reversed(ordered) if p.name not in keep], sorted(suspect, key=lambda p: (p.moment, p.name)))


def clock_concerns(
    plan: Plan, committed: Sequence[RecoveryPoint], this_point: str, created: datetime, now: datetime
) -> List[str]:
    """Why the clock cannot be trusted to decide what to delete, from the points in the folder and the
    two readings of the host clock in this run (`created` at its start, `now` before retention). Empty
    if nothing is implausible. Messages carry names, dates and counts only."""
    concerns = [
        f"{p.name} is dated {p.moment.strftime(STAMP_FORMAT)}, later than the host clock ({now.strftime(STAMP_FORMAT)})"
        for p in plan.suspect
    ]
    prior = [p for p in committed if p.name != this_point and p not in plan.suspect]
    if prior:
        newest = max(prior, key=lambda p: (p.moment, p.name))
        if created - newest.moment > MAX_FORWARD_GAP:
            concerns.append(
                f"this run's point is dated {(created - newest.moment).days} days after the previous one ({newest.name}): "
                "either the host did not run for that long or its clock jumped forward"
            )
    if now - created > MAX_RUN_DURATION:
        concerns.append(f"the host clock moved forward by {int((now - created).total_seconds() // 3600)} hours during this run")
    return concerns


# --- the remote --------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Entry:
    name: str
    size: int
    hashes: Mapping[str, str]  # "md5", "sha1", "sha256": lower case, no dashes


def parse_listing(raw: bytes) -> List[Entry]:
    """The files in an `rclone lsjson` answer. Anything this tool cannot read exactly is an error,
    never a guess: retention decides from this list."""
    try:
        items = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise Failed("rclone's listing of the folder is not JSON.") from None
    if not isinstance(items, list):
        raise Failed("rclone's listing of the folder is not a list.")
    entries: List[Entry] = []
    seen: Set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise Failed("rclone's listing of the folder has an entry this tool cannot read.")
        if item.get("IsDir"):
            continue
        name, size = item.get("Name"), item.get("Size")
        if not isinstance(name, str) or isinstance(size, bool) or not isinstance(size, int) or size < -1:
            raise Failed("rclone's listing of the folder has an entry this tool cannot read.")
        if name in seen:
            raise Failed("rclone's listing of the folder names an object twice.")
        seen.add(name)
        hashes = {}
        given = item.get("Hashes")
        if isinstance(given, dict):
            for kind, value in given.items():
                if isinstance(kind, str) and isinstance(value, str):
                    hashes[kind.lower().replace("-", "")] = value.lower()
        entries.append(Entry(name, size, hashes))
    return entries


def commit_record(name: str, digests: Digests) -> bytes:
    """The commit record NAME.commit of the encrypted object NAME: canonical JSON (sorted keys, ASCII)
    holding the object's name, size and checksums. It is written only after the object has been read
    back and its final listing checked, so a record that matches an object is the evidence that both
    happened. It holds no content, no key and no path."""
    document = {
        "format": COMMIT_FORMAT, "name": name, "bytes": digests.size,
        "sha256": digests.sha256, "sha1": digests.sha1, "md5": digests.md5,
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("ascii")


def read_commit_record(raw: bytes, name: str) -> Optional[Digests]:
    """The digests a commit record states for the object `name`, or None unless `raw` is EXACTLY the
    record this tool writes for that name: nothing cut short, nothing added, nobody else's name."""
    try:
        document = json.loads(raw.decode("ascii"))
        size, sha256, sha1, md5 = document["bytes"], document["sha256"], document["sha1"], document["md5"]
    except (ValueError, KeyError, TypeError):
        return None
    if (
        isinstance(size, bool) or not isinstance(size, int) or size <= 0
        or not all(isinstance(v, str) for v in (sha256, sha1, md5))
        or not (SHA256_RE.fullmatch(sha256) and SHA1_RE.fullmatch(sha1) and MD5_RE.fullmatch(md5))
    ):
        return None
    digests = Digests(size, sha256, sha1, md5)
    return digests if raw == commit_record(name, digests) else None


# Why an object with a final name is not a recovery point. Fixed texts: they are printed and tested.
NO_RECORD = "it has no commit record"
EMPTY_OBJECT = "it is empty"
BAD_RECORD = "its commit record is not one this tool writes for it, or is cut short"
DIFFERS = "it is not the object its commit record describes (size or checksum differs)"
NO_MD5 = "the remote's listing gives no MD5 for it, so it cannot be tied to its commit record"


@dataclasses.dataclass
class Folder:
    """What is in the remote folder, by what this tool can prove about it."""

    committed: List[RecoveryPoint]  # object + a valid record whose size and MD5 equal the listing's: the only recovery points
    unresolved: Dict[str, str]      # final-named objects that are not (name -> why): never counted, never deleted
    stray_records: List[str]        # NAME.commit whose NAME is not in the folder (the half of a deletion that was interrupted)
    others: List[str]               # everything else, including leftover .partial uploads: not this function's business


def classify_folder(entries: Sequence[Entry], read_record: Callable[[str], bytes]) -> Folder:
    """Sort the folder's entries. `read_record(point_name)` returns the bytes of that point's record and
    raises Failed if rclone cannot read it: then nothing can be decided and nothing is guessed. Used by
    the run, by retention and by `list`, so there is one definition of a recovery point.

    The shape of a name alone never makes a point. A point is a final-named, non-empty object whose record
    parses exactly, names that object, and states the size AND the MD5 that the listing reports for it. A
    listing without an MD5 for the object (or with another one) leaves it unresolved, whatever its size:
    this is the cryptographic binding between the object and its record. Only size and MD5 are re-checked
    here, on every run; the SHA-256 in the record was verified once, by the read-back at upload."""
    objects = {e.name: e for e in entries if parse_point_name(e.name) is not None}
    records = {
        e.name[: -len(COMMIT_SUFFIX)]: e for e in entries
        if COMMIT_RE.fullmatch(e.name) and parse_point_name(e.name[: -len(COMMIT_SUFFIX)]) is not None
    }
    record_names = {f"{base}{COMMIT_SUFFIX}" for base in records}
    folder = Folder([], {}, sorted(f"{base}{COMMIT_SUFFIX}" for base in records if base not in objects), [])
    folder.others = sorted(e.name for e in entries if e.name not in set(objects) | record_names)
    for name in sorted(objects):
        entry, record = objects[name], records.get(name)
        if record is None:
            folder.unresolved[name] = NO_RECORD
        elif entry.size <= 0:
            folder.unresolved[name] = EMPTY_OBJECT
        elif not 0 < record.size <= MAX_RECORD_BYTES:
            folder.unresolved[name] = BAD_RECORD
        else:
            digests = read_commit_record(read_record(name), name)
            if digests is None:
                folder.unresolved[name] = BAD_RECORD
                continue
            try:
                check_entry(entry, digests)
            except Unverifiable:
                folder.unresolved[name] = NO_MD5
                continue
            except Failed:
                folder.unresolved[name] = DIFFERS
                continue
            folder.committed.append(parse_point_name(name))
    return folder


def validate_remote(spec: str) -> str:
    if not REMOTE_RE.fullmatch(spec):
        raise Refused(
            "--remote must be NAME:FOLDER, a configured rclone remote and a folder of its own inside it "
            "(letters, digits, _ . - ; not the root of the remote, not a path)."
        )
    return spec


class Remote:
    """The one folder on the remote this tool works in, and the only code that names, creates or
    deletes objects there."""

    def __init__(self, spec: str, config: Path, runner: Runner, rclone: str) -> None:
        self.spec = validate_remote(spec)
        self.config = config
        self.runner = runner
        self.rclone = rclone

    def _object(self, name: str) -> str:
        if not (POINT_RE.fullmatch(name) or PARTIAL_RE.fullmatch(name) or COMMIT_RE.fullmatch(name)):
            raise Failed("refusing to address a remote object of a shape this tool does not create.")
        return f"{self.spec}/{name}"

    def _call(self, step: str, command: str, *operands: str, timeout: float, capture: bool = False) -> Result:
        argv = [self.rclone, command, "--config", str(self.config), "--log-level", "ERROR", *operands]
        done = self.runner.run(argv, timeout=timeout, capture=capture)
        if done.returncode != 0:
            raise Failed(f"rclone {command} failed (exit {done.returncode}) while {step}.")
        return done

    def require_server_side_move(self) -> None:
        """The commit starts with a rename, which must be atomic. An rclone that cannot rename server-side on
        this remote would copy and delete, and a stop in the middle could leave a final name on partial data."""
        root = self.spec.split(":", 1)[0] + ":"
        done = self._call("checking the remote's features", "backend", "features", root,
                          timeout=RCLONE_QUICK_TIMEOUT, capture=True)
        try:
            can_move = json.loads(done.stdout.decode("utf-8"))["Features"]["Move"]
        except (ValueError, KeyError, TypeError):
            raise Failed("rclone's description of the remote's features is not in a form this tool understands.") from None
        if can_move is not True:
            raise Refused("the remote cannot rename server-side, so the commit of a recovery point would not be atomic.")

    def ensure_folder(self) -> None:
        self._call("preparing the folder", "mkdir", self.spec, timeout=RCLONE_QUICK_TIMEOUT)

    def listing(self, step: str = "listing the folder", timeout: float = RCLONE_QUICK_TIMEOUT) -> List[Entry]:
        done = self._call(
            step, "lsjson", "--files-only", "--hash", self.spec, timeout=timeout, capture=True
        )
        if len(done.stdout) > MAX_LISTING_BYTES:
            raise Failed("rclone's listing of the folder is implausibly large.")
        return parse_listing(done.stdout)

    def upload(self, local: Path, name: str) -> None:
        # --immutable: an object that already has this name is never overwritten. One attempt of the whole
        # command (a second would meet its own half-written object and be refused); rclone still retries
        # each request itself (--low-level-retries), and a transient failure that gets through fails the run,
        # which is then repeated by hand or by the next scheduled one.
        self._call("uploading", "copyto", "--immutable", "--retries", "1", str(local), self._object(name),
                   timeout=RCLONE_TRANSFER_TIMEOUT)

    def download(self, name: str, local: Path) -> None:
        self._call("reading the upload back", "copyto", self._object(name), str(local),
                   timeout=RCLONE_TRANSFER_TIMEOUT)

    def move(self, source: str, destination: str) -> None:
        self._call("committing", "moveto", self._object(source), self._object(destination),
                   timeout=RCLONE_QUICK_TIMEOUT)

    def read_record(self, name: str) -> bytes:
        """The commit record of the point `name`: read only. Callers have checked its listed size, which
        bounds what is read; an answer longer than any record is cut just past that size and fails to parse."""
        return self._call("reading a commit record", "cat", self._object(name + COMMIT_SUFFIX),
                          timeout=RCLONE_QUICK_TIMEOUT, capture=True).stdout[: MAX_RECORD_BYTES + 1]

    def delete(self, name: str, timeout: float = RCLONE_QUICK_TIMEOUT) -> None:
        # One object by its exact name; hard delete so that the provider's trash does not keep it.
        self._call("deleting", "deletefile", "--yandex-hard-delete", self._object(name), timeout=timeout)

    def delete_confirmed(self, names: Sequence[str], timeout: float = RCLONE_QUICK_TIMEOUT) -> "Deleted":
        """Delete each of `names`, one at a time (the first rclone failure ends the attempts), then list the
        folder AGAIN and report what that listing shows. A delete counts as done only if the object is
        absent from the new listing: not because rclone exited 0 (a backend can accept a delete and keep
        the object), and not for a failure status alone (the object may be gone all the same)."""
        why: List[str] = []
        if not names:
            return Deleted([], [], None, why)
        for name in names:
            try:
                self.delete(name, timeout)
            except Failed as failure:
                why.append(str(failure))
                break
        try:
            after = {e.name for e in self.listing("confirming the deletion", timeout)}
        except Failed as failure:
            why.append(f"{failure} The deletion could not be confirmed.")
            return Deleted([], list(names), None, why)
        return Deleted([n for n in names if n not in after], [n for n in names if n in after], after, why)


@dataclasses.dataclass
class Deleted:
    """The outcome of Remote.delete_confirmed, from the listing taken after the deletes."""

    gone: List[str]              # requested, and absent from that listing
    left: List[str]              # requested, and still listed, never attempted, or not provable (see `after`)
    after: Optional[Set[str]]    # every name in that listing; None if it could not be taken
    why: List[str]               # what rclone failed at, in words that are safe to print


def deletion_problems(what: str, result: Deleted) -> List[str]:
    """What to report about a deletion, from the listing taken after it (never from exit statuses alone): nothing
    if every requested object is proven absent, even if rclone said it failed; otherwise what failed and what remains."""
    problems: List[str] = []
    if result.left:
        problems.extend(result.why)
        shown = ", ".join(result.left[:5]) + (f" and {len(result.left) - 5} more" if len(result.left) > 5 else "")
        problems.append(f"{len(result.left)} {what}(s) are not confirmed deleted and remain in the folder: {shown}")
    return problems


def is_there(path: Path) -> bool:
    """Whether `path` exists; a path that cannot even be examined is treated as still there."""
    try:
        return path.exists()
    except OSError:
        return True


def check_entry(entry: Entry, expected: Digests) -> List[str]:
    """Compare what the remote reports about an object with the digests held for it (the local file's, or
    a commit record's). Returns the checksum kinds that were compared; raises if the size or any checksum
    the listing offers differs.

    The listing MUST carry an MD5 and it must equal `expected.md5`: a size alone, or a listing without it,
    never vouches for an object (Unverifiable). Yandex Disk lists MD5 only, so MD5 is the one binding
    between an object on the remote and its record after the upload. The SHA-256 is checked once, by the
    read-back of step 5, and not by this function."""
    if entry.size != expected.size:
        raise Failed("the remote reports a different size than the file that was uploaded.")
    if not entry.hashes.get("md5"):
        raise Unverifiable("the remote's listing gives no MD5 for the object, so it cannot be tied to the checksums that were verified.")
    compared = []
    for kind, local in (("sha256", expected.sha256), ("sha1", expected.sha1), ("md5", expected.md5)):
        if kind in entry.hashes:
            if entry.hashes[kind] != local:
                raise Failed(f"the remote's {kind} of the uploaded object differs from the file's.")
            compared.append(kind)
    return compared


# --- the recovery point (tar) ------------------------------------------------------------------


def build_recovery_tar(destination: Path, snapshot: Path, created: datetime, image_tag: str) -> Digests:
    """Write the tar: MANIFEST.json, snapshot.db.sha256, snapshot.db (the snapshot last). Returns the
    snapshot's digests. The manifest holds a format name, a time, the image tag and the snapshot's
    size and checksum: no host, no path, no count, no content."""
    snap = digests_of(snapshot)
    manifest = {
        "format": FORMAT,
        "created_utc": created.strftime(STAMP_FORMAT),
        "image_tag": image_tag,
        "sanitized": "vacuum-into",
        "snapshot": {"name": SNAPSHOT_NAME, "bytes": snap.size, "sha256": snap.sha256},
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("ascii")
    stamp = int(created.timestamp())

    def info(name: str, size: int) -> tarfile.TarInfo:
        member = tarfile.TarInfo(name)
        member.size, member.mode, member.mtime = size, 0o600, stamp
        member.uid = member.gid = 0
        member.uname = member.gname = ""
        return member

    checksum_bytes = checksum_line(snap.sha256, SNAPSHOT_NAME)
    with open_exclusive(destination) as handle:
        with tarfile.open(fileobj=handle, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            archive.addfile(info(MANIFEST_NAME, len(manifest_bytes)), fileobj=io.BytesIO(manifest_bytes))
            archive.addfile(info(CHECKSUM_NAME, len(checksum_bytes)), fileobj=io.BytesIO(checksum_bytes))
            with open(snapshot, "rb") as source:
                archive.addfile(info(SNAPSHOT_NAME, snap.size), fileobj=source)
        handle.flush()
        os.fsync(handle.fileno())
    return snap


def read_recovery_tar(path: Path, extract_to: Optional[Path] = None) -> dict:
    """Check a recovery-point tar completely and return its manifest; extract its three files into
    the existing directory `extract_to` if given (new files only, by fixed names, never extractall).
    Raises Failed unless the tar is exactly what build_recovery_tar writes; whatever was extracted
    before the problem was found is removed again, so a bad tar leaves nothing that could be taken
    for a backup."""
    created: List[Path] = []
    try:
        return _read_recovery_tar(path, extract_to, created)
    except BaseException:
        for leftover in created:
            with contextlib.suppress(OSError):
                leftover.unlink()
        raise


def _read_recovery_tar(path: Path, extract_to: Optional[Path], created: List[Path]) -> dict:
    expected_names = sorted(TAR_MEMBERS)
    try:
        archive = tarfile.open(path, mode="r:")
    except (tarfile.TarError, OSError):
        raise Failed("this is not a recovery-point tar.") from None
    with archive:
        try:
            members = archive.getmembers()
        except tarfile.TarError:
            raise Failed("this is not a recovery-point tar.") from None
        if sorted(m.name for m in members) != expected_names or not all(m.isreg() for m in members):
            raise Failed("the tar does not hold exactly MANIFEST.json, snapshot.db.sha256 and snapshot.db.")
        by_name = {m.name: m for m in members}
        if by_name[MANIFEST_NAME].size > 4096 or by_name[CHECKSUM_NAME].size > 256:
            raise Failed("the manifest or the checksum file is larger than any this tool writes.")
        if not 0 < by_name[SNAPSHOT_NAME].size <= MAX_DATABASE_BYTES:
            raise Failed("the snapshot in the tar is empty or larger than this kit restores.")
        manifest_raw = archive.extractfile(by_name[MANIFEST_NAME]).read()
        manifest = _check_manifest(manifest_raw)
        checksum_raw = archive.extractfile(by_name[CHECKSUM_NAME]).read()
        digest = hashlib.sha256()
        with contextlib.ExitStack() as stack:
            sink = None
            if extract_to is not None:
                sink = stack.enter_context(open_exclusive(extract_to / SNAPSHOT_NAME))
                created.append(extract_to / SNAPSHOT_NAME)
            source = archive.extractfile(by_name[SNAPSHOT_NAME])
            size = 0
            for chunk in iter(lambda: source.read(CHUNK), b""):
                size += len(chunk)
                digest.update(chunk)
                if sink is not None:
                    sink.write(chunk)
        if size != manifest["snapshot"]["bytes"] or digest.hexdigest() != manifest["snapshot"]["sha256"]:
            raise Failed("the snapshot in the tar does not match its manifest.")
        if checksum_raw != checksum_line(digest.hexdigest(), SNAPSHOT_NAME):
            raise Failed("the checksum file in the tar is not exactly the one line for snapshot.db.")
        if extract_to is not None:
            for name, data in ((MANIFEST_NAME, manifest_raw), (CHECKSUM_NAME, checksum_raw)):
                with open_exclusive(extract_to / name) as handle:
                    created.append(extract_to / name)
                    handle.write(data)
    return manifest


def _check_manifest(raw: bytes) -> dict:
    try:
        manifest = json.loads(raw.decode("ascii"))
    except ValueError:
        raise Failed("the manifest is not JSON.") from None
    snapshot = manifest.get("snapshot") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"format", "created_utc", "image_tag", "sanitized", "snapshot"}
        or manifest["format"] != FORMAT
        or manifest["sanitized"] != "vacuum-into"
        or not isinstance(manifest["image_tag"], str)
        or not TAG_RE.fullmatch(manifest["image_tag"])
        or not isinstance(manifest["created_utc"], str)
        or not isinstance(snapshot, dict)
        or set(snapshot) != {"name", "bytes", "sha256"}
        or snapshot["name"] != SNAPSHOT_NAME
        or isinstance(snapshot["bytes"], bool)
        or not isinstance(snapshot["bytes"], int)
        or not isinstance(snapshot["sha256"], str)
        or not SHA256_RE.fullmatch(snapshot["sha256"])
    ):
        raise Failed("the manifest is not one this tool writes.")
    try:
        datetime.strptime(manifest["created_utc"], STAMP_FORMAT)
    except ValueError:
        raise Failed("the manifest is not one this tool writes.") from None
    return manifest


# --- configuration -----------------------------------------------------------------------------


def read_recipients(path: Path) -> int:
    """Check the recipients file and return how many recipients it names. Only X25519 public keys
    are accepted; private key material anywhere in the file is refused (the VPS must never hold one),
    and neither the key nor its line is ever printed.

    The raw bytes are searched for the private-key marker BEFORE comments, whitespace or encoding are
    looked at: a key that is commented out, indented, behind a public key on the same line or beside a
    byte that is not ASCII is still a private key on this host."""
    try:
        if not is_plain_file(path) or path.stat().st_size > 65536:
            raise Refused("--recipients-file must be a regular file (not a link) of at most 64 KiB.")
        content = path.read_bytes()
    except OSError:
        raise Refused("--recipients-file cannot be read as an ASCII text file.") from None
    if PRIVATE_KEY_MARKER in content.upper():
        raise Refused(
            f"--recipients-file contains a PRIVATE key ({PRIVATE_KEY_MARKER.decode('ascii')}...), possibly "
            "commented out. The private key must never be on this host: keep it elsewhere and list only "
            "the public key (age1...)."
        )
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError:
        raise Refused("--recipients-file cannot be read as an ASCII text file.") from None
    count = 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "SECRET-KEY" in line.upper():
            raise Refused(
                f"--recipients-file line {number} looks like a PRIVATE key. The private key must never be "
                "on this host: keep it elsewhere and list only the public key (age1...)."
            )
        if not RECIPIENT_RE.fullmatch(line):
            raise Refused(f"--recipients-file line {number} is not an age public key (age1 followed by 58 characters).")
        count += 1
    if not 1 <= count <= 8:
        raise Refused("--recipients-file must name between 1 and 8 age public keys.")
    return count


def check_rclone_config(path: Path) -> None:
    if not is_plain_file(path) or path.stat().st_size > (1 << 20):
        raise Refused("--rclone-config must be an existing regular file (not a link) of at most 1 MiB.")
    if os.name != "nt" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise Refused("--rclone-config holds the remote's access token: make it readable by its owner only (chmod 600).")


def check_remote_config(path: Path, remote_spec: str) -> None:
    """Fail closed unless the rclone config section of the remote is a Yandex Disk remote with the
    application's own OAuth client and the application-folder mode on. Reads the file; prints no value."""
    name = remote_spec.split(":", 1)[0]
    parser = configparser.RawConfigParser(delimiters=("=",), strict=True)
    try:
        parser.read_string(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, configparser.Error):
        raise Refused("--rclone-config cannot be read as an rclone configuration file (or repeats a setting).") from None
    if not parser.has_section(name):
        raise Refused(f"--rclone-config has no [{name}] section for the remote named by --remote.")
    section = parser[name]

    def value(key: str) -> str:
        return section.get(key, "").strip()

    if value("type") != "yandex":
        raise Refused(f"the remote [{name}] is not a Yandex Disk remote (type = yandex): nothing else is supported.")
    if value("app_folder").lower() != "true":
        raise Refused(
            f"the remote [{name}] does not set app_folder = true. This tool works only inside the Yandex Disk "
            "application folder and never with access to the whole disk."
        )
    if not value("client_id") or not value("client_secret"):
        raise Refused(
            f"the remote [{name}] has no client_id and client_secret of its own: rclone's built-in application "
            "is shared and its access is wider than the application folder."
        )
    if not value("token"):
        raise Refused(f"the remote [{name}] has no token: authorise it with rclone config first.")


def check_rclone_capability(runner: Runner, rclone: str, config: Path) -> None:
    """Fail closed unless this rclone knows the Yandex `app_folder` option. An rclone that does not would
    ignore the setting and address the whole disk (or fail with 403 for a restricted token); either way it
    is not one to run this job with."""
    done = runner.run([rclone, "config", "providers", "--config", str(config)], timeout=RCLONE_QUICK_TIMEOUT, capture=True)
    if done.returncode != 0 or len(done.stdout) > MAX_PROVIDERS_BYTES:
        raise Refused(f"`rclone config providers` failed (exit {done.returncode}); the rclone version cannot be checked.")
    try:
        providers = json.loads(done.stdout.decode("utf-8"))
        yandex = [p for p in providers if isinstance(p, dict) and p.get("Name") == "yandex"]
        options = {o.get("Name") for o in yandex[0]["Options"]}
    except (ValueError, IndexError, KeyError, TypeError, AttributeError):
        raise Refused("rclone's list of backends is not in a form this tool understands; the rclone version cannot be checked.") from None
    if "app_folder" not in options:
        raise Refused(
            "this rclone has no app_folder option for Yandex Disk (stable 1.75.2 does not; the first stable release "
            "that does is planned as 1.76.0). Install a stable release that has it; nothing was done."
        )


@dataclasses.dataclass
class Config:
    remote: str
    rclone_config: Path
    tag: str = ""
    recipients_file: Optional[Path] = None
    work_dir: Optional[Path] = None
    max_deletions: int = DEFAULT_MAX_DELETIONS


# --- the job -----------------------------------------------------------------------------------


class Job:
    def __init__(
        self,
        config: Config,
        runner: Runner,
        deploy_dir: Path,
        clock: Callable[[], datetime],
        token: Callable[[], str],
        log: Callable[[str], None] = say,
    ) -> None:
        self.config, self.runner, self.deploy_dir = config, runner, deploy_dir
        self.clock, self.token, self.log = clock, token, log
        self.tools: Dict[str, str] = {}
        self.remote: Optional[Remote] = None
        self.run_dir: Optional[Path] = None
        self.partial: Optional[str] = None  # the .partial this run uploaded and has not yet renamed
        self.committed: Optional[str] = None  # the point this run committed (object AND commit record confirmed)
        self.created: Optional[datetime] = None  # the host clock at the start of this run: the time in the point's name
        self.deletions_used = 0  # of --max-deletions: leftovers of earlier runs and points retention drops, together
        self.problems: List[str] = []  # housekeeping that did not complete: reported at the end, never only warned

    def deletions_left(self) -> int:
        return max(self.config.max_deletions - self.deletions_used, 0)

    # preflight ---------------------------------------------------------------------------------

    def preflight(self) -> None:
        config = self.config
        validate_remote(config.remote)
        check_rclone_config(config.rclone_config)
        if not TAG_RE.fullmatch(config.tag):
            raise Refused("--tag is not an image tag.")
        read_recipients(config.recipients_file)
        work = config.work_dir
        if work is None or not work.is_absolute() or work.is_symlink() or not work.is_dir() or not os.access(work, os.W_OK | os.X_OK):
            raise Refused("--work-dir must be an existing absolute directory (not a link) that this user can write.")
        for name in ("bash", "docker", "age", "rclone"):
            found = self.runner.which(name)
            if not found:
                raise Refused(f"{name} is not installed on this host (RUNBOOK section 15 lists what is needed).")
            self.tools[name] = found
        if not is_plain_file(self.deploy_dir / "backup.sh"):
            raise Refused("deploy/backup.sh is not next to this script.")
        check_remote_config(config.rclone_config, config.remote)
        check_rclone_capability(self.runner, self.tools["rclone"], config.rclone_config)
        self.remote = Remote(config.remote, config.rclone_config, self.runner, self.tools["rclone"])
        self.remote.require_server_side_move()

    # the whole run -----------------------------------------------------------------------------

    def run(self) -> None:
        self.preflight()
        with self.lock():
            self.sweep_stale_work_dirs()
            self.run_dir = Path(tempfile.mkdtemp(prefix=RUN_PREFIX, dir=self.config.work_dir))
            try:
                self.make_recovery_point()
            finally:
                self.clean_up()
            self.retain()

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """One run at a time. The kernel drops an flock when the process ends, so it is never stale."""
        if fcntl is None:
            yield
            return
        path = self.config.work_dir / LOCK_NAME
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Failed("another off-host backup is running (its lock is held); this run did nothing.") from None
            yield
        finally:
            os.close(descriptor)

    def sweep_stale_work_dirs(self) -> None:
        """Remove what a killed run (SIGKILL, power) left: directories of this tool's name in the work
        directory. The lock is held, so no other run is using one. Directories this user does not own
        are not touched, but they are not ignored either: a leftover that cannot be removed may hold
        plaintext, and a new run would put more next to it, so the run is REFUSED (nothing is done)."""
        stuck = []
        for entry in sorted(self.config.work_dir.iterdir()):
            if not entry.name.startswith(RUN_PREFIX) or entry.is_symlink() or not entry.is_dir():
                continue
            try:
                foreign = hasattr(os, "getuid") and entry.stat().st_uid != os.getuid()
            except OSError:
                foreign = True
            if not foreign:
                shutil.rmtree(entry, ignore_errors=True)
            if is_there(entry):
                stuck.append(entry.name)
                self.log(f"WARNING: could not remove the leftover work directory {entry}; it may hold plaintext")
            else:
                self.log(f"removed the leftover work directory {entry.name} of an earlier run")
        if stuck:
            raise Refused(
                f"{len(stuck)} leftover work director{'y' if len(stuck) == 1 else 'ies'} of earlier runs cannot be removed "
                f"({', '.join(stuck)}) and may hold plaintext. Remove by hand (RUNBOOK 15.7); no new run starts "
                "while one exists. Nothing was done."
            )

    # making the point --------------------------------------------------------------------------

    def make_recovery_point(self) -> None:
        run_dir = self.run_dir
        created = self.created = self.clock().astimezone(timezone.utc).replace(microsecond=0)

        pair = self.take_backup(run_dir / "pair")
        size = pair.stat().st_size
        free = free_bytes(run_dir)
        if free < 3 * size + SPACE_HEADROOM:
            raise Failed(f"not enough room in the work directory: {free >> 20} MiB free, {(3 * size + SPACE_HEADROOM) >> 20} MiB needed.")

        sanitized = self.sanitize(pair, run_dir / "sanitized.db")
        self.discard(pair.parent)  # the unsanitized copy is not needed again
        self.verify_in_container(sanitized)

        tar_path = run_dir / "recovery-point.tar"
        snap = build_recovery_tar(tar_path, sanitized, created, self.config.tag)
        read_recovery_tar(tar_path)  # the tar is read back completely before it is encrypted
        self.log(f"packed {SNAPSHOT_NAME} ({snap.size} bytes) with its checksum and manifest.")
        self.discard(sanitized)

        sealed = self.encrypt(tar_path, run_dir / "recovery-point.tar.age")
        self.discard(tar_path)

        name = f"house-helper-{created.strftime(STAMP_FORMAT)}-{self.token()}.tar.age"
        if not POINT_RE.fullmatch(name):
            raise Failed("internal error: the recovery point name is not of the tool's own shape.")
        self.upload_and_commit(sealed, name)

    def take_backup(self, directory: Path) -> Path:
        self.log("step 1/6: backup (deploy/backup.sh) ...")
        done = self.runner.run(
            [self.tools["bash"], str(self.deploy_dir / "backup.sh"), "--tag", self.config.tag,
             "--dir", str(directory), "--label", "offsite"],
            timeout=BACKUP_TIMEOUT, capture=True, env_extra={"TMPDIR": str(self.run_dir)},
        )
        self.forward(done.stderr)
        if done.returncode != 0:
            raise Failed(f"deploy/backup.sh failed (exit {done.returncode}); no recovery point was made.")
        lines = [line for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        if len(lines) != 1:
            raise Failed("deploy/backup.sh did not report exactly one path.")
        path = Path(lines[0])
        sidecar = path.with_name(path.name + ".sha256")
        try:
            inside = path.parent.resolve() == directory.resolve()
        except OSError:
            inside = False
        if not (inside and BACKUP_NAME_RE.fullmatch(path.name) and is_plain_file(path) and is_plain_file(sidecar)):
            raise Failed("deploy/backup.sh reported a backup that is not a regular pair in the private directory.")
        # The rule of RUNBOOK section 10, byte for byte: not `sha256sum -c`, which follows any name.
        if sidecar.read_bytes() != checksum_line(sha256_of(path), path.name):
            raise Failed("the backup's checksum file is not exactly the one line for it.")
        return path

    def sanitize(self, pair: Path, destination: Path) -> Path:
        self.log("step 2/6: rewriting the snapshot without deleted rows (dbtool sanitize) ...")
        done = self.runner.run(
            self.docker("sanitize", "--input", "-", "--output", "-"),
            timeout=CONTAINER_TIMEOUT, stdin_path=pair, stdout_path=destination,
        )
        self.forward(done.stderr)
        if done.returncode != 0:
            raise Failed(f"dbtool sanitize failed (exit {done.returncode}); nothing was uploaded.")
        reported = set(re.findall(r"sha256 ([0-9a-f]{64})", done.stderr.decode("utf-8", "replace")))
        digests = digests_of(destination)
        if not 0 < digests.size <= MAX_DATABASE_BYTES:
            raise Failed("the rewritten snapshot is empty or larger than this kit restores.")
        with open(destination, "rb") as handle:
            if not handle.read(16).startswith(b"SQLite format 3\x00"):
                raise Failed("the rewritten snapshot does not start like a SQLite database.")
        if reported != {digests.sha256}:
            raise Failed("the rewritten snapshot on the host is not what the tool wrote: the stream was damaged.")
        return destination

    def verify_in_container(self, snapshot: Path) -> None:
        self.log("step 3/6: verifying the rewritten snapshot (dbtool verify --require-head) ...")
        done = self.runner.run(
            self.docker("verify", "--input", "-", "--require-head"),
            timeout=CONTAINER_TIMEOUT, stdin_path=snapshot,
        )
        self.forward(done.stderr)
        if done.returncode != 0:
            raise Failed(f"verification of the rewritten snapshot failed (exit {done.returncode}); nothing was uploaded.")

    def docker(self, *dbtool_args: str) -> List[str]:
        return [self.tools["docker"], *DOCKER_ISOLATION, f"house-helper:{self.config.tag}",
                "python", DBTOOL, *dbtool_args]

    def encrypt(self, plaintext: Path, destination: Path) -> Path:
        self.log("step 4/6: encrypting for the recipients' public keys (age) ...")
        done = self.runner.run(
            [self.tools["age"], "-R", str(self.config.recipients_file)],
            timeout=AGE_TIMEOUT, stdin_path=plaintext, stdout_path=destination,
        )
        if done.returncode != 0:
            raise Failed(f"age failed (exit {done.returncode}); nothing was uploaded.")
        with open(destination, "rb") as handle:
            header = handle.read(len(AGE_HEADER))
        if header != AGE_HEADER or destination.stat().st_size <= plaintext.stat().st_size:
            raise Failed("the encrypted file is not an age file; nothing was uploaded.")
        return destination

    def upload_and_commit(self, sealed: Path, name: str) -> None:
        remote = self.remote
        local = digests_of(sealed)
        partial = name + ".partial"
        self.log(f"step 5/6: uploading {name} ({local.size} bytes, sha256 {local.sha256}) ...")
        remote.ensure_folder()
        before = remote.listing("checking the folder before the upload")
        if any(entry.name in (name, partial, name + COMMIT_SUFFIX) for entry in before):
            raise Failed("an object of this run's name already exists on the remote; nothing was uploaded.")
        self.drop_stale_partials(before)

        self.partial = partial  # from here on a failure takes its own partial back
        remote.upload(sealed, partial)
        # The listing must carry an MD5 already here: it is what binds the object to its record later, and a
        # remote that cannot give one is found out now, while the object is still this run's own partial.
        listed = self.find(remote.listing("checking the upload"), partial)
        compared = check_entry(listed, local)
        downloaded = self.run_dir / "readback.bin"
        remote.download(partial, downloaded)
        if digests_of(downloaded).sha256 != local.sha256:
            raise Failed("the object read back from the remote is not the file that was uploaded.")
        self.discard(downloaded)
        self.log(f"the upload was read back and its SHA-256 equals the file's (the remote's own {'/'.join(compared)} agreed too).")

        # The object takes its final name. From here until its commit record has been written and read
        # back it is only a CANDIDATE: the shape of its name is not proof. A stop, a failure or an answer
        # that cannot be read in between leaves an object that every later run reports as unresolved and
        # never counts, relies on or deletes.
        remote.move(partial, name)
        listing = remote.listing("confirming the rename")
        final = self.find(listing, name)
        if not any(entry.name == partial for entry in listing):
            self.partial = None  # renamed: there is no partial to take back
        try:
            check_entry(final, local)
        except Unverifiable as failure:
            # Not proven damaged, so not deleted either; but no record can be tied to it, so none is written.
            raise Failed(
                f"{failure} {name} was read back before the rename, but nothing can tie the renamed object to a commit "
                "record, so none was written: it is left in the folder untouched and reported by every run until an "
                "operator has looked at it (RUNBOOK 15.7)."
            ) from None
        except Failed:
            # The rename itself damaged it (the listing proves it): this run's own object, by its exact
            # name, is not a point. Nothing older is touched.
            self.reject(name)
            raise
        self.write_commit_record(name, local)
        self.committed = name
        self.log(f"committed {name} (commit record {name}{COMMIT_SUFFIX}, read back); the final check of the folder follows.")

    def write_commit_record(self, name: str, local: Digests) -> None:
        """Upload the commit record of the verified object and read it back. Only an object with this
        record is a recovery point (classify_folder); if it cannot be confirmed the object stays where it
        is, untouched, as an unresolved candidate."""
        record = commit_record(name, local)
        path = self.run_dir / "commit-record.json"
        with open_exclusive(path) as handle:
            handle.write(record)
        try:
            self.remote.upload(path, name + COMMIT_SUFFIX)
            if self.remote.read_record(name) != record:
                raise Failed("the commit record read back from the remote is not the one that was written.")
        except Failed as failure:
            raise Failed(
                f"{failure} {name} was uploaded and read back, but its commit record is not confirmed, so it is NOT a "
                "recovery point: it is left in the folder untouched and reported by every run until an operator has "
                "looked at it (RUNBOOK 15.7)."
            ) from None

    def reject(self, name: str) -> None:
        """Take back this run's own object that the listing proved damaged. The delete is confirmed by a
        listing; if it cannot be, the object stays WITHOUT a commit record, which is what keeps every later
        run from counting it, and the log says so."""
        result = self.remote.delete_confirmed([name], CLEANUP_TIMEOUT)
        if result.left:
            self.log(
                f"WARNING: {name} is damaged and could not be removed (or its removal could not be confirmed). It has no "
                "commit record, so it is not a recovery point and no run counts it; every run reports it until an "
                "operator has removed it (RUNBOOK 15.7)."
            )
        else:
            self.log(f"removed {name}: the rename damaged it.")

    def drop_stale_partials(self, entries: Sequence[Entry]) -> None:
        """Remove the .partial uploads that earlier killed runs left. They draw on the same --max-deletions
        budget as retention; whatever is not removed, or not confirmed removed, is reported at the end."""
        stale = sorted(e.name for e in entries if PARTIAL_RE.fullmatch(e.name))
        take = stale[: self.deletions_left()]
        if take:
            self.deletions_used += len(take)
            for name in take:
                self.log(f"removing {name}, left by an earlier run that did not finish.")
            result = self.remote.delete_confirmed(take)  # housekeeping must not stop the backup it is tidying up for ...
            for name in result.gone:
                self.log(f"removed {name}.")
            reason = "; ".join(result.why) or "it is still listed after the delete"
            for name in result.left:  # ... but it must not go unseen either: the end of the run reports it (exit 6)
                self.problems.append(f"could not remove the leftover partial upload {name} ({reason})")
        if len(stale) > len(take):
            self.problems.append(
                f"{len(stale) - len(take)} more leftover partial uploads were not removed "
                f"(--max-deletions {self.config.max_deletions} is one budget for this run's cleanup and retention together)"
            )

    @staticmethod
    def find(entries: Sequence[Entry], name: str) -> Entry:
        matches = [e for e in entries if e.name == name]
        if len(matches) != 1:
            raise Failed("the object is not in the remote folder as expected.")
        return matches[0]

    # retention ---------------------------------------------------------------------------------

    def retain(self) -> None:
        """After the commit: the final check, then retention. Three outcomes, three statuses:

          * the final check confirms the point and nothing else needs attention: return (exit 0);
          * it confirms the point, but the housekeeping after it did not complete: RetentionIncomplete
            (exit 6). The point is stored; the report says what else needs a look;
          * it cannot confirm the point: Unconfirmed (exit 7), raised before anything is deleted.

        Nothing that could not be pruned or tidied is only logged: it fails the run, every run it persists.

        Retention works on COMMITTED points only (an object, and a valid commit record whose size and MD5
        equal the listing's). Unresolved objects are not counted, relied on or deleted; they are reported,
        and while any exists no committed point is dropped (prune). Nothing is deleted at all while the
        clock is implausible (clock_concerns)."""
        self.log("step 6/6: final check and retention ...")
        folder = self.confirm_commit()
        problems = list(self.problems)
        plan: Optional[Plan] = None
        dropped = 0
        try:
            problems.extend(self.report_unresolved(folder))
            now = self.clock().astimezone(timezone.utc)
            plan = plan_retention(folder.committed, protect={self.committed}, now=now)
            concerns = clock_concerns(plan, folder.committed, self.committed, self.created, now)
            if concerns:
                problems.append(
                    "retention deleted nothing, because the clock cannot be trusted: " + "; ".join(concerns)
                    + " (RUNBOOK 15.7)"
                )
            else:
                dropped = self.prune(plan, folder, problems)
        except Failed as failure:
            problems.append(str(failure))
        except Exception as failure:  # the text of an unexpected exception can quote paths: class only
            problems.append(f"unexpected {type(failure).__name__} during retention")
        if problems:
            raise RetentionIncomplete(
                " ".join(p if p.endswith(".") else p + "." for p in problems)
                + f" The new recovery point {self.committed} IS stored and verified: its SHA-256 was checked by the read-back "
                "before the commit, and the final check of the folder matched its size and MD5 to its commit record."
            )
        self.log(
            f"stored {self.committed} (the final check matched its size and MD5 to its commit record); "
            f"{len(plan.keep)} points kept, {dropped} deleted"
            + (f", {len(folder.others)} other object(s) in the folder left alone." if folder.others else ".")
        )

    def confirm_commit(self) -> Folder:
        """The final check: list the folder again, after everything this run did to it, and classify it.
        Returns the folder if (and only if) it shows the point this run committed as a committed one. Raises
        Unconfirmed otherwise, with nothing deleted: if the listing or a record cannot be read, if the object
        has no valid record, or if its size or MD5 no longer equals its record's. In none of those cases is the
        point called stored or verified: the run wrote it and read its record back, and the folder does not
        agree now."""
        try:
            folder = classify_folder(self.remote.listing("listing before retention"), self.remote.read_record)
        except Failed as failure:
            why = str(failure)
        except Exception as failure:  # the text of an unexpected exception can quote paths: class only
            why = f"unexpected {type(failure).__name__}"
        else:
            if self.committed in {p.name for p in folder.committed}:
                return folder
            reason = folder.unresolved.get(self.committed)
            why = f"the folder shows it as unresolved: {reason}" if reason else "it is not in the folder listing"
        raise Unconfirmed(
            f"the final check of the folder did not confirm the recovery point {self.committed}: {why.rstrip('.')}. It is "
            "NOT reported as stored: this run wrote its object and its commit record and read the record back, but the folder, "
            "listed again afterwards, does not show it as a committed point (or could not be read). Nothing was deleted. "
            "Run `list` and see RUNBOOK 15.7 and 15.11."
            + "".join(f" Also: {p.rstrip('.')}." for p in self.problems)
        )

    def report_unresolved(self, folder: Folder) -> List[str]:
        """One log line per unresolved object, and the one problem that fails the run for as long as any exists."""
        if not folder.unresolved:
            return []
        for name, why in folder.unresolved.items():
            self.log(f"UNRESOLVED {name}: {why}. It is not a recovery point; it is left untouched.")
        names = list(folder.unresolved)
        return [
            f"{len(names)} object(s) with a final name are NOT recovery points ({', '.join(names[:5])}"
            + (f" and {len(names) - 5} more" if len(names) > 5 else "")
            + "): each lacks a valid commit record that matches it. They are not counted, relied on or deleted, and "
            "while any exists retention drops no committed recovery point; an operator must look at them (RUNBOOK 15.11)"
        ]

    def prune(self, plan: Plan, folder: Folder, problems: List[str]) -> int:
        """Delete the points `plan` drops (the object first, then its commit record, so that an interruption
        leaves a harmless orphaned record and never an object that looks unresolved) and the orphaned records.
        Every step is confirmed by listing the folder again. Returns how many points are completely gone.
        While the folder holds an unresolved object no point is dropped at all: only the orphaned records go.
        Raises Failed, having deleted nothing, if the work is more than what is left of --max-deletions."""
        drops = [p.name for p in plan.drop]
        strays = list(folder.stray_records)
        if drops and folder.unresolved:
            self.log(
                f"retention holds back {len(drops)} committed point(s) it would drop ({', '.join(drops)}): "
                f"{len(folder.unresolved)} unresolved object(s) are in the folder, and no committed point is dropped "
                "while one exists (RUNBOOK 15.11)."
            )
            drops = []
        wanted = len(drops) + len(strays)
        if not wanted:
            return 0
        if wanted > self.deletions_left():
            raise Failed(
                f"retention would delete {wanted} ({len(drops)} points, {len(strays)} orphaned commit records), more than "
                f"--max-deletions ({self.config.max_deletions}) leaves for it ({self.deletions_left()} left this run, "
                f"{self.deletions_used} used by leftover partial uploads); nothing was deleted."
            )
        for name in drops:
            if name in plan.keep or name == self.committed:
                raise Failed("internal error: retention chose a point it keeps.")
        self.deletions_used += wanted
        objects = self.remote.delete_confirmed(drops)
        problems.extend(deletion_problems("point", objects))
        # A record goes only after its object is PROVEN absent; a point whose object is still there stays whole.
        records = self.remote.delete_confirmed([n + COMMIT_SUFFIX for n in objects.gone] + strays)
        problems.extend(deletion_problems("commit record", records))
        removed = [n for n in objects.gone if n + COMMIT_SUFFIX in records.gone]
        for name in removed:
            self.log(f"deleted {name} and its commit record.")
        for name in records.gone:
            if name in strays:
                self.log(f"deleted {name}: an orphaned commit record.")
        latest = records.after if records.after is not None else objects.after
        if latest is not None and not {*plan.keep, *(f"{k}{COMMIT_SUFFIX}" for k in plan.keep)} <= latest:
            raise Failed("a point that retention keeps, or its commit record, is no longer in the folder.")
        return len(removed)

    # clean-up ----------------------------------------------------------------------------------

    def clean_up(self) -> None:
        """On every exit path of the making of the point. Ignores further signals; never raises. A work
        directory that cannot be removed is recorded in `problems`: the run then ends with exit 6 if the
        point was committed, and with its own failure status otherwise; the warning is logged either way,
        and the next run refuses to start while the directory is there."""
        previous = {}
        for number in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if number is not None:
                with contextlib.suppress(Exception):
                    previous[number] = signal.signal(number, signal.SIG_IGN)
        try:
            if self.run_dir is not None:
                shutil.rmtree(self.run_dir, ignore_errors=True)
                if is_there(self.run_dir):
                    self.log(
                        f"WARNING: could not remove the work directory {self.run_dir}; it may hold plaintext. Remove it "
                        "by hand: no new run starts while it exists."
                    )
                    self.problems.append(
                        f"the work directory {self.run_dir.name} could not be removed and may hold plaintext (remove it "
                        "by hand: no new run starts while it exists)"
                    )
            if self.partial is not None:
                self.take_back_partial(self.partial)
                self.partial = None
        finally:
            for number, handler in previous.items():
                with contextlib.suppress(Exception):
                    signal.signal(number, handler)

    def take_back_partial(self, name: str) -> None:
        """Delete this run's own unfinished upload, if it is there. Only that name: the object under
        the final name was read back before it was renamed, so it is never removed here."""
        try:
            try:
                present = any(e.name == name for e in self.remote.listing("looking for the unfinished upload", CLEANUP_TIMEOUT))
            except Failed:
                present = True  # cannot tell: try the delete, which touches only this exact name
            if present:
                if self.remote.delete_confirmed([name], CLEANUP_TIMEOUT).left:
                    self.log(f"WARNING: could not remove the unfinished upload {name} (or confirm that it is gone); the next run removes it.")
                else:
                    self.log(f"removed this run's unfinished upload {name}.")
        except Exception:
            self.log(f"WARNING: could not remove the unfinished upload {name}; the next run removes it.")

    def discard(self, path: Path) -> None:
        """Remove a temporary file or directory as soon as it is not needed. Best effort: the final
        clean-up removes whatever is left."""
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            self.log(f"WARNING: could not remove {path.name} yet; the clean-up at the end removes it.")

    def forward(self, stderr: bytes) -> None:
        """The progress lines of backup.sh and dbtool (written to be log-safe: names, sizes, counts)."""
        for line in stderr.decode("utf-8", "replace").splitlines()[:40]:
            line = "".join(ch for ch in line if ch.isprintable())[:300]
            if line:
                self.log(f"  | {line}")


# --- list --------------------------------------------------------------------------------------


def list_points(remote: Remote, log: Callable[[str], None] = say, clock: Optional[Callable[[], datetime]] = None) -> int:
    """Read only. What is in the folder by what this tool can prove: the committed recovery points and what a
    run would do with them, then everything that needs an operator (RUNBOOK 15.7). Changes nothing."""
    entries = remote.listing()
    folder = classify_folder(entries, remote.read_record)
    now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    plan = plan_retention(folder.committed, now=now)
    held = bool(folder.unresolved)  # while an unresolved object exists, a run drops no committed point
    log(
        f"{len(folder.committed)} recovery points in {remote.spec} (each an object with a valid commit record whose size and MD5 the listing confirms); "
        + (
            f"a run now would delete none of them, though retention alone would drop {len(plan.drop)}: unresolved objects hold it back."
            if held else f"a run now would keep {len(plan.keep)} and delete {len(plan.drop)}."
        )
    )
    for point in sorted(folder.committed, key=lambda p: (p.moment, p.name), reverse=True):
        if point in plan.suspect:
            log(f"  SUSPECT  {point.name}  dated in the future: not counted, not deleted")
        else:
            label = "keep" if point.name in plan.keep else "hold" if held else "drop"
            log(f"  {label}  {point.name}  {plan.keep.get(point.name, '')}".rstrip())
    if plan.suspect:
        log(
            f"ATTENTION: {len(plan.suspect)} point(s) are dated later than the host clock ({now.strftime(STAMP_FORMAT)}). "
            "A run deletes NOTHING while one exists; check the clock, then see RUNBOOK 15.7."
        )
    if folder.unresolved:
        log(
            f"ATTENTION: {len(folder.unresolved)} object(s) have a final name but NO valid commit record. They are NOT recovery "
            "points: never counted, relied on or deleted by this tool; every run ends with exit 6 and drops no committed point until an operator has looked (RUNBOOK 15.11):"
        )
        for name, why in folder.unresolved.items():
            log(f"  UNRESOLVED  {name}  ({why})")
    if folder.stray_records:
        log(f"{len(folder.stray_records)} orphaned commit record(s) (their object is gone); a run whose retention may delete removes them: {', '.join(folder.stray_records)}")
    if folder.others:
        log(f"{len(folder.others)} other object(s) in the folder would be left alone.")
    return EXIT_OK


# --- command line ------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        say(f"usage: {message}")
        raise SystemExit(EXIT_USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="offsite_backup.py", description="Encrypted off-host backup of the House Helper database.", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    run = commands.add_parser("run", help="take, rewrite, pack, encrypt, upload, verify and rotate")
    run.add_argument("--tag", required=True, help="the image that matches the database (the one RUNNING)")
    run.add_argument("--recipients-file", required=True, type=Path, help="age PUBLIC keys, one per line")
    run.add_argument("--rclone-config", required=True, type=Path, help="the rclone config file of the remote (mode 0600)")
    run.add_argument("--remote", required=True, help="NAME:FOLDER, the dedicated folder on the rclone remote")
    run.add_argument("--work-dir", required=True, type=Path, help="absolute directory for temporary plaintext (tmpfs)")
    run.add_argument("--max-deletions", type=int, default=DEFAULT_MAX_DELETIONS,
                     help="most leftovers and old points one run may delete: one budget for its cleanup and its retention together")
    show = commands.add_parser("list", help="show the points in the remote folder and what retention would do (read only)")
    show.add_argument("--rclone-config", required=True, type=Path)
    show.add_argument("--remote", required=True)
    unpack = commands.add_parser("unpack", help="check a decrypted recovery point and extract it")
    unpack.add_argument("tar", type=Path, help="the decrypted recovery-point tar")
    unpack.add_argument("--output-dir", required=True, type=Path, help="an existing directory; nothing in it is replaced")
    return parser


def _signal_handler(signum, frame):
    raise Interrupted(signum)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runner: Optional[Runner] = None,
    clock: Optional[Callable[[], datetime]] = None,
    token: Optional[Callable[[], str]] = None,
    deploy_dir: Optional[Path] = None,
    log: Callable[[str], None] = say,
) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)
    os.umask(0o077)
    runner = runner or Runner()
    installed = {}
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(Exception):
                installed[number] = signal.signal(number, _signal_handler)
    try:
        if args.command == "unpack":
            return _unpack(args, log)
        if args.command == "list":
            rclone = runner.which("rclone")
            if not rclone:
                raise Refused("rclone is not installed on this host.")
            check_rclone_config(args.rclone_config)
            check_remote_config(args.rclone_config, args.remote)
            check_rclone_capability(runner, rclone, args.rclone_config)
            remote = Remote(args.remote, args.rclone_config, runner, rclone)
            remote.require_server_side_move()
            return list_points(remote, log, clock)
        if args.max_deletions < 0:
            raise Refused("--max-deletions cannot be negative.")
        config = Config(
            remote=args.remote, rclone_config=args.rclone_config, tag=args.tag,
            recipients_file=args.recipients_file, work_dir=args.work_dir, max_deletions=args.max_deletions,
        )
        Job(
            config, runner, deploy_dir or Path(__file__).resolve().parent,
            clock or (lambda: datetime.now(timezone.utc)), token or (lambda: secrets.token_hex(4)), log,
        ).run()
        return EXIT_OK
    except OffsiteError as failure:
        log(f"{'refused' if isinstance(failure, Refused) else 'FAILED'}: {failure}")
        return failure.exit_code
    except Interrupted as stop:
        log(f"interrupted by signal {stop.signum}; the clean-up ran.")
        return 128 + stop.signum
    except Exception as failure:  # the text of an unexpected exception can quote paths: class only
        log(f"FAILED: unexpected {type(failure).__name__}.")
        return EXIT_FAILED
    finally:
        for number, handler in installed.items():
            with contextlib.suppress(Exception):
                signal.signal(number, handler)


def _unpack(args: argparse.Namespace, log: Callable[[str], None]) -> int:
    directory = args.output_dir
    if not directory.is_dir() or directory.is_symlink():
        raise Refused("--output-dir must be an existing directory (not a link).")
    if any((directory / name).exists() or (directory / name).is_symlink() for name in TAR_MEMBERS):
        raise Refused("--output-dir already holds one of the files; nothing is replaced.")
    try:
        manifest = read_recovery_tar(args.tar, extract_to=directory)
    except OSError:
        raise Failed("the tar cannot be read or the files cannot be written.") from None
    log(
        f"the recovery point is intact: {SNAPSHOT_NAME} {manifest['snapshot']['bytes']} bytes, sha256 "
        f"{manifest['snapshot']['sha256']}, made {manifest['created_utc']} by image {manifest['image_tag']}."
    )
    log(f"extracted to {directory}. Restore it with the image tag above (RUNBOOK section 10, Restore).")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
