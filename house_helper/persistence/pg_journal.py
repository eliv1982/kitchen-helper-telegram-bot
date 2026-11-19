"""The durable enrollment journal, its marker and its per-run authorization record.

Frozen source: HH-DB-01 §4.4 (ENR-13/ENR-14), §7.2 (LCK-06/LCK-07/LCK-08/LCK-09),
§4.1 (the marker), and the WP-2C-A authority boundary.

What lives here
---------------
* `Journal` - an append-only, digest-chained JSON-lines file with a separate lock file,
  `fsync` of the file and its directory, and a small head anchor. The journal is created
  by the root-controlled host tool OUTSIDE `PGDATA`; it is never mounted into the web
  application and never handed to an application container.
* `make_marker` / `parse_marker` - the one frozen identification mechanism (ENR-05): a
  database `COMMENT` carrying a non-secret marker and the cluster's `system_identifier`.
* `encode_authorization` / `decode_authorization` - the bounded, non-secret record the
  host tool writes to the migrate one-shot's stdin after an authoritative Mode B pass.

Journal record
--------------
One canonical JSON object per line, one `os.write()` under the exclusive journal lock:

    {"at":"...Z","kind":"cluster_enrollment","payload":{...},
     "prev":"sha256:...","seq":0,"version":1}

`prev` is the SHA-256 of the previous line (without its newline). Sequence 0's `prev` is
the all-zero genesis digest. `journal.head` holds `{"seq":N,"digest":"sha256:..."}` and
is replaced atomically after each append, so a removed or edited tail is detected too.
The head file is a local tamper check, not a signature: an actor with root on the host
can rewrite both (boundary B4, out of scope). What this design does guarantee is that an
accidental truncation, a partially written line, a reordered record or an edited record
is refused, never inferred around.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

try:  # POSIX - the deployment target
    import fcntl
except ImportError:  # pragma: no cover - Windows developer machines
    fcntl = None
try:  # pragma: no cover - Windows developer machines
    import msvcrt
except ImportError:
    msvcrt = None

GENESIS = "sha256:" + "0" * 64
JOURNAL_VERSION = 1
JOURNAL_FILE = "enrollment.jsonl"
LOCK_FILE = "journal.lock"
HEAD_FILE = "journal.head"
# A host-side operation lock, distinct from the journal append lock and from the
# worker's state-volume maintenance lock (LCK-01/LCK-06). It serializes host-side
# identity-changing operations (Mode A, migrate, reconciliation, handoff, and the
# maintenance backup/restore) across the Mode B verification -> worker execution interval
# without ever taking the same non-reentrant
# flock on both sides of the host/worker boundary. Journal appends still take
# `journal.lock` inside this interval; the two lock files are never nested in reverse.
OPERATION_LOCK_FILE = "operation.lock"
# The one environment variable every host entry point reads for the default journal
# directory. `enroll.py` and `maintenance.py` both default here, and the offsite tool
# forwards its own `--postgres-journal-dir` to `maintenance.py`. The operation interval is
# the flock on `<journal-dir>/operation.lock`, so two processes exclude each other only
# when that resolves to the SAME underlying file. `reject_conflicting_journal_directories`
# refuses an explicit `--journal-dir` that names a different directory than the
# environment, rather than silently operating on two disjoint locks.
JOURNAL_DIRECTORY_ENV = "HOUSE_HELPER_PG_JOURNAL_DIR"
# Lock acquisition order for the whole host tooling - one direction, never reversed:
#   1. `operation.lock` (host operation interval: mode-a, migrate, reconcile-migration,
#      handoff, and maintenance backup/restore);
#   2. `journal.lock`   (one append, taken inside the interval);
#   3. the state-volume flocks (shared `/state/.house-helper.lock`, exclusive
#      `/state/.house-helper.backup.lock`), taken only by disposable containers, which
#      never take 1 or 2.
# Layers 1-2 belong to the host, layer 3 to a container, so no wait-for cycle exists.
# Every acquisition is non-blocking: a conflict is refused immediately, never waited on.

# Every record kind the WP-2C-A tooling can write or read. A journal line whose kind is
# not in this set is treated as corruption, not as a future extension to be ignored.
JOURNAL_KINDS = frozenset(
    {
        "cluster_enrollment",
        "cluster_identity",
        "identity_update",
        "migration",
        "reconcile",
        "temp_db",
        "serving_promotion",
        "handoff",
    }
)

MARKER_FORMAT = "house-helper-enrollment/1"
MARKER_KEYS = frozenset({"format", "marker", "system_identifier"})
_TOKEN = re.compile(r"[0-9a-f]{32}")
_SYSTEM_IDENTIFIER = re.compile(r"[0-9]{1,20}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

AUTHORIZATION_FORMAT = "house-helper-pg-authorization/1"
AUTHORIZATION_MAX_BYTES = 64 * 1024
# The enrollment-time `db/schema.sql` channel (F8). It is a DIFFERENT bounded record from
# the Mode B migrate authorization: it authorizes only the frozen schema/default-ACL step,
# never a migration, and it is valid only before the Alembic chain exists.
SCHEMA_AUTHORIZATION_FORMAT = "house-helper-pg-schema-authorization/1"
SCHEMA_AUTHORIZATION_KEYS = frozenset(
    {
        "format",
        "role",
        "database",
        "cluster",
        "artifacts",
        "run_id",
        "issued_at",
        "not_after",
    }
)
SCHEMA_ROLE = "schema"
AUTHORIZATION_KEYS = frozenset(
    {
        "format",
        "role",
        "mode",
        "verdict",
        "run_id",
        "issued_at",
        "not_after",
        "journal",
        "cluster",
        "marker",
        "database",
        "project_roles",
        "databases",
        "predefined_roles",
        "artifacts",
    }
)
CLUSTER_KEYS = frozenset(
    {"container_name", "container_id", "image_digest", "volume", "system_identifier"}
)
_RUN_ID = re.compile(r"[0-9a-f]{32}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
# The deployment surface pins the PostgreSQL image by digest; an authorization that names
# any other image reference is malformed, not a different-but-acceptable deployment.
_PINNED_IMAGE = re.compile(r"postgres@sha256:[0-9a-f]{64}")


class JournalError(Exception):
    """A refused journal/marker/authorization operation. `code` is stable."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


def same_directory(first: object, second: object) -> bool:
    """Whether two directory names denote the same underlying filesystem object.

    When both exist this compares `(st_dev, st_ino)` - a hard link, a bind mount or a
    symlink to the same directory compares equal even when the names differ. When one
    does not exist yet it falls back to the canonical, case-normalized real path. This
    never assumes that an identical textual path means the same object, nor that two
    different spellings mean different ones.
    """
    left, right = Path(first), Path(second)
    try:
        a, b = os.stat(left), os.stat(right)
    except OSError:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
            os.path.realpath(right)
        )
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def reject_conflicting_journal_directories(
    cli_value: object, *, environ: Optional[Mapping[str, str]] = None
) -> None:
    """Refuse when `--journal-dir` and `HOUSE_HELPER_PG_JOURNAL_DIR` name different dirs.

    Every cluster-advancing host operation excludes the others through the flock on
    `<journal-dir>/operation.lock`. If an operator passes one directory explicitly while
    the environment names another, the two silently take different locks. Failing closed
    here is the only way to keep one operation interval across `enroll.py`,
    `maintenance.py` and the offsite tool that forwards the value.
    """
    values = os.environ if environ is None else environ
    configured = values.get(JOURNAL_DIRECTORY_ENV)
    if not configured:
        return
    if not same_directory(cli_value, configured):
        raise JournalError(
            "journal_mismatch",
            "the explicit --journal-dir and HOUSE_HELPER_PG_JOURNAL_DIR name different "
            "directories; they would take different operation locks",
            f"{JOURNAL_DIRECTORY_ENV}={configured}",
        )


# --- canonical encoding ----------------------------------------------------------------------


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise JournalError("not_canonical", "the record is not canonical JSON data") from None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: Optional[datetime] = None) -> str:
    moment = utcnow() if value is None else value.astimezone(timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise JournalError("timestamp", "a timestamp must be UTC with a Z suffix")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise JournalError("timestamp", "a timestamp is not ISO-8601") from None
    return parsed


def record_digest(line: bytes) -> str:
    return "sha256:" + hashlib.sha256(line).hexdigest()


# --- platform lock and fsync -----------------------------------------------------------------


def _sync(fd: int) -> None:
    os.fsync(fd)


def _sync_directory(path: Path) -> None:
    """Persist a directory entry. POSIX only; Windows has no directory fsync."""
    if fcntl is None:
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _try_lock(fd: int, exclusive: bool) -> bool:
    busy = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}
    if hasattr(errno, "EDEADLOCK"):
        busy.add(errno.EDEADLOCK)
    try:
        if fcntl is not None:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        elif msvcrt is not None:  # pragma: no cover - Windows
            # msvcrt has one kind of lock: a shared request becomes exclusive locally.
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as error:
        if error.errno in busy:
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:  # a closed descriptor releases the lock anyway
        pass


# --- journal records -------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    seq: int
    kind: str
    payload: Mapping[str, Any]
    at: str
    prev: str
    digest: str

    def as_json(self) -> Mapping[str, Any]:
        return {
            "at": self.at,
            "kind": self.kind,
            "payload": dict(self.payload),
            "prev": self.prev,
            "seq": self.seq,
            "version": JOURNAL_VERSION,
        }


@dataclass(frozen=True)
class JournalView:
    records: tuple[Record, ...]
    head: str
    head_seq: int

    def of_kind(self, kind: str) -> tuple[Record, ...]:
        return tuple(record for record in self.records if record.kind == kind)

    def latest(self, kind: str) -> Optional[Record]:
        found = self.of_kind(kind)
        return found[-1] if found else None


def _parse_line(raw: bytes, seq: int, prev: str) -> Record:
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JournalError("journal_corrupt", "a journal line is not JSON", f"seq {seq}") from None
    if not isinstance(value, dict):
        raise JournalError("journal_corrupt", "a journal line is not an object", f"seq {seq}")
    if set(value) != {"at", "kind", "payload", "prev", "seq", "version"}:
        raise JournalError("journal_corrupt", "a journal line has unexpected fields", f"seq {seq}")
    if canonical_json(value) != raw:
        raise JournalError("journal_not_canonical", "a journal line is not canonical", f"seq {seq}")
    if value["version"] != JOURNAL_VERSION or value["seq"] != seq or value["prev"] != prev:
        raise JournalError("journal_chain", "a journal line is out of sequence", f"seq {seq}")
    if not isinstance(value["kind"], str) or value["kind"] not in JOURNAL_KINDS:
        raise JournalError("journal_kind", "a journal line names an unknown kind", f"seq {seq}")
    if not isinstance(value["payload"], dict):
        raise JournalError("journal_corrupt", "a journal payload is not an object", f"seq {seq}")
    if not isinstance(value["at"], str):
        raise JournalError("journal_corrupt", "a journal timestamp is not text", f"seq {seq}")
    parse_timestamp(value["at"])
    return Record(
        seq=seq,
        kind=value["kind"],
        payload=value["payload"],
        at=value["at"],
        prev=value["prev"],
        digest=record_digest(raw),
    )


class Journal:
    """The root-controlled enrollment journal in one directory outside `PGDATA`."""

    def __init__(self, directory: Path, *, require_root: bool = False) -> None:
        self.directory = Path(directory)
        self.require_root = require_root
        self.path = self.directory / JOURNAL_FILE
        self.lock_path = self.directory / LOCK_FILE
        self.head_path = self.directory / HEAD_FILE
        self.operation_path = self.directory / OPERATION_LOCK_FILE

    # -- preparation ---------------------------------------------------------------------

    def prepare(self) -> None:
        """Create or verify the directory and files.

        The directory is `0700` and the files `0600`. On POSIX a group/other-writable
        journal, or one not owned by the running (root) user, is refused. Windows has no
        equivalent bits; the tests there cover content, not ownership.
        """
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise JournalError("journal_unavailable", "the journal directory cannot be created") from None
        if fcntl is not None:
            info = self.directory.stat()
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise JournalError("journal_permissions", "the journal directory is not private")
            if self.require_root and info.st_uid != 0:
                raise JournalError("journal_owner", "the journal directory is not root-owned")
            # mkdir's mode is subject to umask; converge the directory to exactly 0700.
            if stat.S_IMODE(info.st_mode) != 0o700:
                os.chmod(self.directory, 0o700)
        for path in (self.path, self.lock_path, self.head_path, self.operation_path):
            if path.exists() and fcntl is not None:
                info = path.stat()
                if stat.S_IMODE(info.st_mode) & 0o077:
                    raise JournalError("journal_permissions", "a journal file is not private")
                if self.require_root and info.st_uid != 0:
                    raise JournalError("journal_owner", "a journal file is not root-owned")
        # Touch the lock files under the directory's permissions.
        for path in (self.lock_path, self.operation_path):
            descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
            os.close(descriptor)

    # -- reading -------------------------------------------------------------------------

    def _locked(self, exclusive: bool):
        return _JournalLock(self.lock_path, exclusive=exclusive)

    def operation_lock(self):
        """The host-side identity-changing operation interval (F1/F9).

        Held across Mode B verification and the subsequent worker invocation. It is NOT
        the worker's state-volume maintenance lock: acquiring that same non-reentrant
        flock on both sides of the container boundary could deadlock (the worker would
        block on the host's hold). This lock has no database or Docker authority; it only
        serializes this tool's own supported operations.
        """
        self.prepare()
        return _JournalLock(
            self.operation_path,
            exclusive=True,
            busy_code="operation_busy",
            busy_message="another host enrollment or migration operation is in progress",
        )

    def canonical_directory(self) -> Path:
        """The journal directory's real path (symlinks and `..` resolved)."""
        return Path(os.path.realpath(self.directory))

    def operation_lock_identity(self) -> tuple[int, int]:
        """The `(st_dev, st_ino)` of the one operation-lock file this directory names.

        Two processes exclude each other exactly when this identity is equal. It is
        exposed so a rehearsal can prove that `enroll.py`, `maintenance.py` and the
        offsite tool resolve to the same underlying lock file, not merely the same
        textual path.
        """
        self.prepare()
        info = os.stat(self.operation_path)
        return (info.st_dev, info.st_ino)

    def read(self) -> JournalView:
        # A read-only verification of a host that has never enrolled must not create the
        # journal directory: an absent journal is an empty journal, not a new one.
        if not self.directory.exists():
            return JournalView(records=(), head=GENESIS, head_seq=-1)
        self.prepare()
        with self._locked(exclusive=False):
            return self._read_unlocked()

    def _read_unlocked(self) -> JournalView:
        if not self.path.exists():
            if self.head_path.exists():
                raise JournalError("journal_head_orphan", "a head anchor exists without a journal")
            return JournalView(records=(), head=GENESIS, head_seq=-1)
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise JournalError("journal_truncated", "the journal does not end at a line boundary")
        lines = raw.splitlines()
        records: list[Record] = []
        prev = GENESIS
        for seq, line in enumerate(lines):
            if not line:
                raise JournalError("journal_corrupt", "the journal holds an empty line", f"seq {seq}")
            record = _parse_line(line, seq, prev)
            prev = record.digest
            records.append(record)
        head_seq = len(records) - 1
        if self.head_path.exists():
            try:
                anchor = json.loads(self.head_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise JournalError("journal_head", "the head anchor is unreadable") from None
            if anchor != {"seq": head_seq, "digest": prev}:
                raise JournalError("journal_head", "the head anchor does not match the journal")
        elif records:
            raise JournalError("journal_head", "the journal has records but no head anchor")
        return JournalView(records=tuple(records), head=prev, head_seq=head_seq)

    # -- appending -----------------------------------------------------------------------

    def append(self, kind: str, payload: Mapping[str, Any], *, at: Optional[datetime] = None) -> Record:
        if kind not in JOURNAL_KINDS:
            raise JournalError("journal_kind", "the record kind is not allowed")
        if not isinstance(payload, Mapping):
            raise JournalError("journal_payload", "the payload must be an object")
        self.prepare()
        with self._locked(exclusive=True):
            return self._append_unlocked(kind, payload, at=at)

    def append_if_head(
        self,
        expected_head: str,
        expected_seq: int,
        kind: str,
        payload: Mapping[str, Any],
        *,
        at: Optional[datetime] = None,
    ) -> Optional[Record]:
        """Append only if the journal head is still the one the decision observed.

        The whole read/compare/append runs under the journal's exclusive lock, so two
        callers that both observed E0 cannot both create an intent. A caller whose head
        changed gets `None` and must re-read and re-dispatch (F9).
        """
        if kind not in JOURNAL_KINDS:
            raise JournalError("journal_kind", "the record kind is not allowed")
        if not isinstance(payload, Mapping):
            raise JournalError("journal_payload", "the payload must be an object")
        self.prepare()
        with self._locked(exclusive=True):
            view = self._read_unlocked()
            if view.head != expected_head or view.head_seq != expected_seq:
                return None
            return self._append_unlocked(kind, payload, at=at)

    def _append_unlocked(
        self, kind: str, payload: Mapping[str, Any], *, at: Optional[datetime]
    ) -> Record:
        view = self._read_unlocked()
        seq = view.head_seq + 1
        record = Record(
            seq=seq,
            kind=kind,
            payload=payload,
            at=timestamp(at),
            prev=view.head,
            digest="",  # computed below
        )
        line = canonical_json(record.as_json())
        digest = record_digest(line)
        descriptor = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            written = 0
            while written < len(line) + 1:
                written += os.write(descriptor, (line + b"\n")[written:])
            _sync(descriptor)
        finally:
            os.close(descriptor)
        self._write_head(seq, digest)
        _sync_directory(self.directory)
        return Record(
            seq=seq,
            kind=kind,
            payload=dict(payload),
            at=record.at,
            prev=record.prev,
            digest=digest,
        )

    def _write_head(self, seq: int, digest: str) -> None:
        payload = canonical_json({"seq": seq, "digest": digest})
        self._write_new_file(self.head_path, payload)
        _sync_directory(self.directory)

    def _write_new_file(self, path: Path, payload: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(
            dir=str(self.directory), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            os.fchmod(descriptor, 0o600) if fcntl is not None else None
            os.write(descriptor, payload)
            _sync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)


class _JournalLock:
    def __init__(
        self,
        path: Path,
        *,
        exclusive: bool,
        busy_code: str = "journal_busy",
        busy_message: str = "another enrollment operation holds the journal lock",
    ) -> None:
        self.path = path
        self.exclusive = exclusive
        self.busy_code = busy_code
        self.busy_message = busy_message
        self.descriptor: Optional[int] = None

    def __enter__(self) -> "_JournalLock":
        descriptor = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        if not _try_lock(descriptor, self.exclusive):
            os.close(descriptor)
            raise JournalError(self.busy_code, self.busy_message)
        self.descriptor = descriptor
        return self

    def __exit__(self, *exc_info) -> None:
        if self.descriptor is not None:
            _unlock(self.descriptor)
            os.close(self.descriptor)
            self.descriptor = None


# --- the identification marker (ENR-05) ------------------------------------------------------


def make_marker(token: str, system_identifier: str) -> str:
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise JournalError("marker_token", "the marker token is not 32 lowercase hex characters")
    if not isinstance(system_identifier, str) or not _SYSTEM_IDENTIFIER.fullmatch(system_identifier):
        raise JournalError("marker_identifier", "the system identifier is not a decimal string")
    return canonical_json(
        {
            "format": MARKER_FORMAT,
            "marker": token,
            "system_identifier": system_identifier,
        }
    ).decode("utf-8")


def parse_marker(text: object) -> tuple[str, str]:
    """The marker token and the recorded `system_identifier`, or a refusal.

    The COMMENT is non-secret and is read back by both the host tool and the migrate
    executor. It carries identity evidence only; it is never an authorization token.
    """
    if not isinstance(text, str) or not text:
        raise JournalError("marker_absent", "the database carries no enrollment marker")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise JournalError("marker_malformed", "the enrollment marker is not JSON") from None
    if not isinstance(value, dict) or set(value) != MARKER_KEYS:
        raise JournalError("marker_malformed", "the enrollment marker has unexpected fields")
    if value["format"] != MARKER_FORMAT:
        raise JournalError("marker_format", "the enrollment marker names another format")
    if not isinstance(value["marker"], str) or not _TOKEN.fullmatch(value["marker"]):
        raise JournalError("marker_token", "the enrollment marker token is malformed")
    if not isinstance(value["system_identifier"], str) or not _SYSTEM_IDENTIFIER.fullmatch(
        value["system_identifier"]
    ):
        raise JournalError("marker_identifier", "the marker's system identifier is malformed")
    return value["marker"], value["system_identifier"]


def marker_literal(marker: str) -> str:
    """The SQL string literal for `COMMENT ON DATABASE`.

    The marker grammar admits no single quote, so the canonical JSON cannot break out of
    the literal. The value is still checked rather than assumed.
    """
    if "'" in marker or "\\" in marker or "\n" in marker:
        raise JournalError("marker_literal", "the marker cannot be represented as a literal")
    return "'" + marker + "'"


# --- the per-run authorization record --------------------------------------------------------


def build_authorization(
    *,
    cluster: Mapping[str, Any],
    marker: str,
    database: str,
    project_roles: Iterable[str],
    databases: Iterable[str],
    predefined_roles: Iterable[str],
    artifacts: Mapping[str, str],
    journal_seq: int,
    journal_head: str,
    run_id: str,
    issued: datetime,
    lifetime_seconds: int = 900,
) -> Mapping[str, Any]:
    return {
        "format": AUTHORIZATION_FORMAT,
        "role": "migrate",
        "mode": "B",
        "verdict": "verified",
        "run_id": run_id,
        "issued_at": timestamp(issued),
        "not_after": (
            datetime.fromtimestamp(issued.timestamp() + lifetime_seconds, timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "journal": {"seq": int(journal_seq), "head": journal_head},
        "cluster": dict(cluster),
        "marker": marker,
        "database": database,
        "project_roles": list(project_roles),
        "databases": list(databases),
        "predefined_roles": list(predefined_roles),
        "artifacts": dict(artifacts),
    }


def _not_after(issued: datetime, lifetime_seconds: int) -> str:
    return (
        datetime.fromtimestamp(issued.timestamp() + lifetime_seconds, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _check_window(
    value: Mapping[str, Any], *, now: Optional[datetime]
) -> datetime:
    """The issued/not_after window, identical for both bounded record kinds."""
    issued = parse_timestamp(value["issued_at"])
    not_after = parse_timestamp(value["not_after"])
    moment = utcnow() if now is None else now.astimezone(timezone.utc)
    if not_after <= issued:
        raise JournalError("authorization_window", "the authorization window is empty")
    if moment > not_after:
        raise JournalError("authorization_expired", "the authorization has expired")
    if moment < issued.replace(microsecond=0):
        raise JournalError("authorization_clock", "the authorization was issued in the future")
    return issued


def _validate_cluster(cluster: Any, code: str) -> None:
    if not isinstance(cluster, dict) or set(cluster) != CLUSTER_KEYS:
        raise JournalError(code, "the authorization cluster facts are malformed")
    if not isinstance(cluster["container_name"], str) or not cluster["container_name"]:
        raise JournalError(code, "the container name is missing")
    if not isinstance(cluster["container_id"], str) or not _CONTAINER_ID.fullmatch(
        cluster["container_id"]
    ):
        raise JournalError(code, "the container id is malformed")
    if not isinstance(cluster["image_digest"], str) or not _PINNED_IMAGE.fullmatch(
        cluster["image_digest"]
    ):
        raise JournalError(code, "the image digest is missing or not the pinned reference")
    if not isinstance(cluster["volume"], str) or not cluster["volume"]:
        raise JournalError(code, "the PGDATA volume is missing")
    if not isinstance(cluster["system_identifier"], str) or not _SYSTEM_IDENTIFIER.fullmatch(
        cluster["system_identifier"]
    ):
        raise JournalError(code, "the system identifier is malformed")


def _validate_artifacts(artifacts: Any, code: str) -> None:
    if not isinstance(artifacts, dict) or not artifacts or not all(
        isinstance(name, str)
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in artifacts.items()
    ):
        raise JournalError(code, "the artifact manifest is malformed")


def encode_authorization(value: Mapping[str, Any], *, lifetime_seconds: int = 900) -> bytes:
    """Canonical bytes for the one-shot's stdin.

    `not_after` is completed here from `issued_at`; callers do not have to compute it.
    """
    issued = parse_timestamp(value["issued_at"])
    complete = dict(value)
    complete["not_after"] = _not_after(issued, lifetime_seconds)
    return canonical_json(complete)


def decode_authorization(raw: bytes, *, now: Optional[datetime] = None) -> Mapping[str, Any]:
    """Validate the exact schema and lifetime; return the record.

    This is structural validation only. The executor must still re-read the live cluster
    and the image's own artifacts before it runs anything: a parseable record is not an
    authorization by itself.
    """
    if not isinstance(raw, bytes):
        raise JournalError("authorization", "the authorization is not bytes")
    if not raw:
        raise JournalError("authorization_absent", "no authorization record was supplied")
    if len(raw) > AUTHORIZATION_MAX_BYTES:
        raise JournalError("authorization_size", "the authorization record is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JournalError("authorization", "the authorization record is not JSON") from None
    if not isinstance(value, dict) or set(value) != AUTHORIZATION_KEYS:
        raise JournalError("authorization", "the authorization record has unexpected fields")
    if value["format"] != AUTHORIZATION_FORMAT:
        raise JournalError("authorization_format", "the authorization names another format")
    if (value["role"], value["mode"], value["verdict"]) != ("migrate", "B", "verified"):
        raise JournalError("authorization", "the authorization is not a verified Mode B record")
    if not isinstance(value["run_id"], str) or not _RUN_ID.fullmatch(value["run_id"]):
        raise JournalError("authorization_run", "the authorization run id is malformed")
    _check_window(value, now=now)
    journal = value["journal"]
    if (
        not isinstance(journal, dict)
        or set(journal) != {"seq", "head"}
        or not isinstance(journal["seq"], int)
        or journal["seq"] < 0
        or not isinstance(journal["head"], str)
        or not _DIGEST.fullmatch(journal["head"])
    ):
        raise JournalError("authorization_journal", "the authorization journal anchor is malformed")
    _validate_cluster(value["cluster"], "authorization_cluster")
    parse_marker(value["marker"])
    if value["database"] != "house_helper":
        raise JournalError("authorization_database", "the authorization names another database")
    for key in ("project_roles", "databases", "predefined_roles"):
        items = value[key]
        if (
            not isinstance(items, list)
            or not all(isinstance(item, str) and item for item in items)
            or len(set(items)) != len(items)
        ):
            raise JournalError("authorization_inventory", f"the {key} inventory is malformed")
    _validate_artifacts(value["artifacts"], "authorization_artifacts")
    return value


def build_schema_authorization(
    *,
    cluster: Mapping[str, Any],
    database: str,
    artifacts: Mapping[str, str],
    run_id: str,
    issued: datetime,
    lifetime_seconds: int = 900,
) -> Mapping[str, Any]:
    """The bounded record for the enrollment-time `db/schema.sql` step (F8).

    It names only the schema step. It carries no migration, no SQL text, no marker and no
    inventory, and it is not accepted by the migrate executor's Mode B decoder.
    """
    return {
        "format": SCHEMA_AUTHORIZATION_FORMAT,
        "role": SCHEMA_ROLE,
        "database": database,
        "cluster": dict(cluster),
        "artifacts": dict(artifacts),
        "run_id": run_id,
        "issued_at": timestamp(issued),
        "not_after": _not_after(issued.astimezone(timezone.utc), lifetime_seconds),
    }


def encode_schema_authorization(
    value: Mapping[str, Any], *, lifetime_seconds: int = 900
) -> bytes:
    issued = parse_timestamp(value["issued_at"])
    complete = dict(value)
    complete["not_after"] = _not_after(issued, lifetime_seconds)
    return canonical_json(complete)


def decode_schema_authorization(
    raw: bytes, *, now: Optional[datetime] = None
) -> Mapping[str, Any]:
    """Structurally validate the schema-step record; execution still re-reads the image.

    A parseable record is not an authorization: the one-shot also compares the artifact
    manifest with its own `db/` files and the executor re-reads the live catalog state.
    """
    if not isinstance(raw, bytes):
        raise JournalError("authorization", "the authorization is not bytes")
    if not raw:
        raise JournalError("authorization_absent", "no authorization record was supplied")
    if len(raw) > AUTHORIZATION_MAX_BYTES:
        raise JournalError("authorization_size", "the authorization record is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise JournalError("authorization", "the authorization record is not JSON") from None
    if not isinstance(value, dict) or set(value) != SCHEMA_AUTHORIZATION_KEYS:
        raise JournalError("authorization", "the authorization record has unexpected fields")
    if value["format"] != SCHEMA_AUTHORIZATION_FORMAT:
        raise JournalError("authorization_format", "the authorization names another format")
    if value["role"] != SCHEMA_ROLE:
        raise JournalError(
            "authorization", "the authorization is not for the enrollment schema step"
        )
    if value["database"] != "house_helper":
        raise JournalError("authorization_database", "the authorization names another database")
    if not isinstance(value["run_id"], str) or not _RUN_ID.fullmatch(value["run_id"]):
        raise JournalError("authorization_run", "the authorization run id is malformed")
    _check_window(value, now=now)
    _validate_cluster(value["cluster"], "authorization_cluster")
    _validate_artifacts(value["artifacts"], "authorization_artifacts")
    return value


def authorization_cluster(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return value["cluster"]
