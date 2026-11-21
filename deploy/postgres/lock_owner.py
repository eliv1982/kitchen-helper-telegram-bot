"""HOF-03 item 4: who actually holds the web service's shared state-volume flock.

The withdrawn design inferred "the web process holds the shared lock" from two sequential
*failed acquisitions*: `check-idle` (exclusive) was refused, and a temporary shared probe
then succeeded. That is not a proof. An exclusive holder can release between the two probes,
the probe's own shared lock can succeed, the holder can reacquire, and the tool publishes
"observed" although the web process never held the lock. Sequential probes describe
attempts, not state.

This module reads the state itself. On Linux the kernel records every `flock(2)` on the
open file description, and `/proc/<pid>/fdinfo/<fd>` exposes the locks held through that
descriptor:

    lock:   1: FLOCK  ADVISORY  READ  4242 08:01:12345 0 EOF

The web process is identified by facts observed on the host:

1. its cgroup contains the immutable Docker container id (`/proc/<pid>/cgroup`);
2. its argv is the deployed image's real uvicorn command - after `dbtool.py serve` execs
   the console script, the kernel rewrites a `#!` script's argv, so the live record is
   `[/opt/venv/bin/python, /opt/venv/bin/uvicorn, house_helper.web.asgi:app, ...]`
   (the `[uvicorn, app, ...]` and `[python, -m, uvicorn, app, ...]` forms are also
   accepted);
3. one of its open descriptors is the state-volume lock file and that descriptor's
   fdinfo carries a `FLOCK ... READ` record owned by that pid.

A shared lock taken by a *probe* cannot satisfy this: the probe is a different process with
a different argv (and normally outside the web container's cgroup), and it is the web
process's own descriptor that must carry the kernel lock record. Because a shared flock and
an exclusive flock on one file cannot coexist, an observed shared lock held by the web
process also establishes that no exclusive holder exists at that moment. `check-idle` is
still run (the frozen specification names it) and must be refused with
`maintenance_lock_busy`, but it is corroboration, never the proof.

Descriptor reuse is refused explicitly. The fdinfo lock record itself carries the locked
inode's device and inode (`08:01:12345` is hex major:minor, then the decimal inode). A
descriptor that resolves to the target file when it is stat'ed, but whose lock record names
another device/inode by the time the fdinfo is read, was closed and reused in between;
that observation is indeterminate and raises instead of being counted as a holder. The
descriptor is stat'ed again after the record is read, so the window is closed in both
directions.

Observation and the host-held witness
-------------------------------------
One snapshot is a read of the kernel's lock ownership at one instant. The host command
takes one before the frozen `check-idle` observation and one after, and publishes only
when both agree. Option A adds a host-held shared flock (`SharedWitness`) on the same
validated lock inode. It is acquired after `check-idle` and held across the final
snapshot, the fresh verification of the current web container identity, the journal
outcome append and its fsync. An exclusive acquisition cannot occur in that interval, so
"no exclusive holder remains" is a fact about the whole publication window, not just an
instant. The witness's own lock is never evidence of web ownership: it belongs to the
host process, it is not in the container's cgroup, and only the web process's own
descriptor can satisfy the holder test. The web process's ownership is attested at the
two snapshots only; the journal claims nothing about it after the fsync.

The reader is deliberately fail-closed: a file that cannot be read, a descriptor table that
cannot be inspected, a descriptor whose lock record names another inode, or more than one
candidate holder raises `LockInspectionError` and the caller leaves the journal intent
UNKNOWN instead of publishing an outcome.

`proc_root` is injectable so the parser can be exercised with a fixture and with real
`flock(2)` locks on Linux. Nothing here generates, prints or reads credential material.
"""

from __future__ import annotations

import errno
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Tuple

try:  # POSIX - the deployment target
    import fcntl
except ImportError:  # pragma: no cover - Windows developer machines
    fcntl = None

LOCK_FILE_NAME = ".house-helper.lock"
LOCK_ACCESS_READ = "read"
LOCK_ACCESS_WRITE = "write"
LOCK_ACCESSES = frozenset({LOCK_ACCESS_READ, LOCK_ACCESS_WRITE})
# The ASGI application the recorded Compose web command starts. It is the one argv token
# that distinguishes the serving process from every probe inside the same container.
WEB_APP_SPEC = "house_helper.web.asgi:app"

# `lock:\t1: FLOCK  ADVISORY  READ  153391 08:40:448746 0 EOF`
# Only flock records matter: dbtool takes flock(2), and a POSIX/OFD record on the same
# inode would not conflict with it, so it is not the lock this proof is about. The leading
# kernel record id is deliberately not used as evidence: it is reused by later
# acquisitions, so it does not distinguish a persistent lock from a released one. The
# device is the kernel's hex `major:minor`; the following number is the decimal inode.
_FDINFO_LOCK = re.compile(
    r"^lock:\s*\d+:\s+FLOCK\s+(?:ADVISORY|MANDATORY)\s+(READ|WRITE)\s+(\d+)\s+"
    r"([0-9a-fA-F]+):([0-9a-fA-F]+):(\d+)\s+(?:\d+|EOF)\s+(?:\d+|EOF)\s*$"
)
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_PYTHON_INTERPRETER = re.compile(r"python(?:3(?:\.\d+)*)?")
_BUSY_ERRNOS = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}
if hasattr(errno, "EDEADLOCK"):  # pragma: no branch - platform dependent
    _BUSY_ERRNOS.add(errno.EDEADLOCK)


class LockInspectionError(Exception):
    """The lock state could not be established; the caller must stay UNKNOWN."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LockWitnessBusy(LockInspectionError):
    """A shared witness was refused because an exclusive holder exists.

    This is a determinate fact about the instant of acquisition, but the caller decides
    whether it is a negative observation or a contradiction with an earlier snapshot.
    """

    def __init__(self) -> None:
        super().__init__(
            "lock_witness_busy",
            "an exclusive holder owns the state-volume lock",
        )


@dataclass(frozen=True)
class Holder:
    """One process that holds a flock on the state-volume lock file."""

    pid: int
    start_ticks: int
    access: str


@dataclass(frozen=True)
class Snapshot:
    """One atomic read of the kernel's lock ownership for the validated web process.

    `lock_present` is False when the state volume has no lock file at all (a determinate
    negative: nobody, web included, can hold a lock on a file that does not exist).
    `identity` is the validated `(st_dev, st_ino)` of that file, or None when absent.
    `holders` is every in-container web process whose own descriptor carries a FLOCK
    record for that file. More than one is reported here and refused by the caller as
    ambiguous.
    """

    lock_present: bool
    identity: Optional[Tuple[int, int]]
    holders: Tuple[Holder, ...]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise LockInspectionError(
            "lock_inspection_proc", "the process state needed for the lock proof cannot be read"
        ) from None


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        raise LockInspectionError(
            "lock_inspection_proc", "the process state needed for the lock proof cannot be read"
        ) from None


def _argv(pid_directory: Path) -> Tuple[str, ...]:
    data = _read_bytes(pid_directory / "cmdline")
    return tuple(part.decode("utf-8", "replace") for part in data.split(b"\0") if part)


def _start_ticks(pid_directory: Path) -> int:
    """Field 22 of /proc/<pid>/stat: the process start time since boot.

    The process name (field 2) may contain spaces and parentheses, so the fixed fields are
    read from after the last `)`. Start ticks disambiguate a reused pid between snapshots.
    """
    text = _read_text(pid_directory / "stat")
    close = text.rfind(")")
    if close < 0:
        raise LockInspectionError(
            "lock_inspection_proc", "a process stat record is malformed"
        )
    fields = text[close + 2:].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        raise LockInspectionError(
            "lock_inspection_proc", "a process start time could not be read"
        ) from None


def _basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _is_python(value: str) -> bool:
    return _PYTHON_INTERPRETER.fullmatch(_basename(value)) is not None


def is_web_process(argv: Sequence[str], app_spec: str = WEB_APP_SPEC) -> bool:
    """argv is the deployed web command, not merely "some program that mentions uvicorn".

    The deployed image runs `dbtool.py serve -- uvicorn <app> ...`. dbtool replaces itself
    with `os.execvp`, and because `uvicorn` is a `#!` console script the kernel rewrites
    the live argv to `[interpreter, script, <original argv[1:]>]`:

        /opt/venv/bin/python\0/opt/venv/bin/uvicorn\0house_helper.web.asgi:app\0...

    That is the form this proof must accept; a matcher for `("uvicorn", app)` alone never
    sees the real process (Codex's deployed-argv failure). The direct `[uvicorn, app]`
    form and `[python, -m, uvicorn, app]` are accepted as well. A probe
    (`python .../dbtool.py exec --shared -- ...`) never has this shape, and the app spec
    must be the token immediately after the uvicorn program, not somewhere later in an
    unrelated command line.
    """
    if not argv or not app_spec:
        return False
    if _basename(argv[0]) == "uvicorn":
        return len(argv) >= 2 and argv[1] == app_spec
    if _is_python(argv[0]):
        if len(argv) >= 3 and _basename(argv[1]) == "uvicorn" and argv[2] == app_spec:
            return True
        if (
            len(argv) >= 4
            and argv[1] == "-m"
            and argv[2] == "uvicorn"
            and argv[3] == app_spec
        ):
            return True
    return False


def _fd_lock_records(
    fdinfo: str, pid: int
) -> Tuple[Tuple[str, int, int, int], ...]:
    """Every flock record this descriptor holds for `pid`, as `(access, major, minor, ino)`.

    A descriptor cannot normally carry both a READ and a WRITE flock for the same open
    file description, but if the kernel ever shows more than one record both are returned;
    the caller treats more than one access as ambiguous, never as shared.
    """
    records: list[Tuple[str, int, int, int]] = []
    for line in fdinfo.splitlines():
        match = _FDINFO_LOCK.match(line)
        if match is None or int(match.group(2)) != pid:
            continue
        records.append(
            (
                match.group(1).lower(),
                int(match.group(3), 16),
                int(match.group(4), 16),
                int(match.group(5)),
            )
        )
    return tuple(records)


def device_parts(device: int) -> Tuple[int, int]:
    """The `(major, minor)` a flock record names, on every platform.

    Linux exposes `os.major`/`os.minor`, which decode a real `dev_t`. Windows has neither
    (only the offline tests run there); the same 8/8 split keeps the parser consistent
    with the fixtures that write the record.
    """
    if hasattr(os, "major") and hasattr(os, "minor"):
        return os.major(device), os.minor(device)
    return (device >> 8) & 0xFF, device & 0xFF


def _record_matches(identity: Tuple[int, int], record: Tuple[str, int, int, int]) -> bool:
    major, minor = device_parts(identity[0])
    return major == record[1] and minor == record[2] and identity[1] == record[3]


def _descriptor_identity(path: Path) -> Optional[Tuple[int, int]]:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _process_access(
    pid_directory: Path, identity: Tuple[int, int]
) -> tuple[str, ...]:
    """The distinct flock accesses this process's own descriptors hold on the lock file.

    Every descriptor is stat'ed; a descriptor that resolves to the validated lock-file
    inode is then read through fdinfo. The kernel's own device/inode in that record must
    agree with the target, and the descriptor is stat'ed again afterwards. A disagreement
    is descriptor reuse - the fd moved to another file between the two reads - and is
    indeterminate, never "not held" and never a holder. A failure to enumerate the
    descriptors of a process already identified as the web process is also indeterminate.
    """
    try:
        descriptors = os.listdir(pid_directory / "fd")
    except OSError:
        raise LockInspectionError(
            "lock_inspection_web_process",
            "the web process descriptor table cannot be read for the lock proof",
        ) from None
    pid = int(pid_directory.name)
    accesses: list[str] = []
    for descriptor in descriptors:
        fd_path = pid_directory / "fd" / descriptor
        if _descriptor_identity(fd_path) != identity:
            # The process may be closing descriptors while it serves; a descriptor that
            # does not name the target cannot carry a lock on it.
            continue
        fdinfo = _read_text(pid_directory / "fdinfo" / descriptor)
        records = _fd_lock_records(fdinfo, pid)
        for access, _major, _minor, _inode in records:
            record = (access, _major, _minor, _inode)
            if not _record_matches(identity, record):
                raise LockInspectionError(
                    "lock_inspection_descriptor",
                    "a web descriptor's flock record names another file: the descriptor "
                    "was replaced during the observation",
                )
        if records:
            # Both reads must describe the same file. A stat before and a stat after the
            # fdinfo read close the reuse window in either direction.
            if _descriptor_identity(fd_path) != identity:
                raise LockInspectionError(
                    "lock_inspection_descriptor",
                    "a web descriptor changed files during the lock observation",
                )
            for access, _major, _minor, _inode in records:
                if access not in accesses:
                    accesses.append(access)
    return tuple(accesses)


def inspect(
    proc_root: Path,
    mountpoint: Path,
    container_id: str,
    app_spec: str = WEB_APP_SPEC,
) -> Snapshot:
    """Read one snapshot of the validated web container's lock ownership.

    Raises `LockInspectionError` when the state cannot be established. A missing lock file
    is a determinate negative (no holder is possible); a missing process, a mismatched
    argv or an unrelated cgroup simply does not contribute a holder.
    """
    if not isinstance(container_id, str) or _CONTAINER_ID.fullmatch(container_id) is None:
        raise LockInspectionError(
            "lock_inspection_identity", "the web container id is malformed"
        )
    lock_path = Path(mountpoint) / LOCK_FILE_NAME
    if not Path(mountpoint).is_dir():
        raise LockInspectionError(
            "lock_inspection_volume",
            "the state volume's host mountpoint cannot be inspected",
        )
    try:
        info = os.stat(lock_path)
    except FileNotFoundError:
        return Snapshot(lock_present=False, identity=None, holders=())
    except OSError:
        raise LockInspectionError(
            "lock_inspection_volume", "the state-volume lock file cannot be inspected"
        ) from None
    identity = (info.st_dev, info.st_ino)
    try:
        entries: Iterable[str] = os.listdir(proc_root)
    except OSError:
        raise LockInspectionError(
            "lock_inspection_proc", "the process table cannot be read for the lock proof"
        ) from None
    holders: list[Holder] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid_directory = Path(proc_root) / entry
        try:
            cgroup = (pid_directory / "cgroup").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            # The process exited between listing and reading; it cannot be a live holder.
            continue
        if container_id not in cgroup:
            continue
        try:
            argv = _argv(pid_directory)
        except LockInspectionError:
            # Same race: a vanished process contributes nothing. A process that is still
            # present but unreadable will be seen and refused on the second snapshot if it
            # is the real holder; a transient absence cannot prove a positive.
            continue
        if not is_web_process(argv, app_spec):
            continue
        start_ticks = _start_ticks(pid_directory)
        access = _process_access(pid_directory, identity)
        if len(access) > 1:
            raise LockInspectionError(
                "lock_inspection_ambiguous",
                "the web process holds more than one flock access on the state-volume lock",
            )
        if access:
            holders.append(
                Holder(
                    pid=int(entry),
                    start_ticks=start_ticks,
                    access=access[0],
                )
            )
    return Snapshot(lock_present=True, identity=identity, holders=tuple(holders))


def device_text(identity: Tuple[int, int]) -> str:
    """The `major:minor` form of a stat device, matching the kernel's decimal view."""
    major, minor = device_parts(identity[0])
    return f"{major}:{minor}"


class SharedWitness:
    """A host-held shared flock on the validated state-volume lock inode.

    It is taken `LOCK_SH|LOCK_NB`: it coexists with the web service's shared lock and is
    refused while an exclusive holder exists. It is held from before the final ownership
    snapshot through the journal outcome append and its fsync, so no exclusive holder can
    appear inside the publication window. It is *not* evidence of the web process's
    ownership and must never be read as such: the host process is outside the container
    cgroup, and only a web process's own descriptor satisfies `inspect`.
    """

    def __init__(
        self,
        path: Path,
        identity: Tuple[int, int],
        *,
        on_held: Optional[Callable[[], None]] = None,
    ) -> None:
        self.path = Path(path)
        self.identity = identity
        self.on_held = on_held
        self.descriptor: Optional[int] = None

    def __enter__(self) -> "SharedWitness":
        if fcntl is None:
            raise LockInspectionError(
                "lock_witness_unavailable",
                "the host cannot take a shared flock on this platform",
            )
        try:
            descriptor = os.open(str(self.path), os.O_RDONLY)
        except FileNotFoundError:
            raise LockInspectionError(
                "lock_witness_missing",
                "the state-volume lock file disappeared before the witness was taken",
            ) from None
        except OSError:
            raise LockInspectionError(
                "lock_witness_open",
                "the state-volume lock file cannot be opened for the witness",
            ) from None
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != self.identity:
                raise LockInspectionError(
                    "lock_witness_identity",
                    "the witness opened a different lock file than the observed one",
                )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in _BUSY_ERRNOS:
                    raise LockWitnessBusy() from None
                raise LockInspectionError(
                    "lock_witness_unavailable",
                    "the shared witness could not be taken",
                ) from None
            # The path may have been replaced while it was opened. We would then hold a
            # lock on an inode no later holder uses; that is not the validated file.
            current = _descriptor_identity(self.path)
            if current != self.identity:
                raise LockInspectionError(
                    "lock_witness_identity",
                    "the state-volume lock file was replaced before the witness was taken",
                )
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        if self.on_held is not None:
            self.on_held()
        return self

    def __exit__(self, *exc_info) -> None:
        if self.descriptor is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            except OSError:  # closing the descriptor releases it anyway
                pass
            os.close(self.descriptor)
            self.descriptor = None


def shared_witness(
    path: Path,
    identity: Tuple[int, int],
    *,
    on_held: Optional[Callable[[], None]] = None,
) -> SharedWitness:
    """The production factory; tests inject an equivalent context manager."""
    return SharedWitness(path, identity, on_held=on_held)
