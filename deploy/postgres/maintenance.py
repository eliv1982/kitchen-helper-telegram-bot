#!/usr/bin/env python3
"""PostgreSQL backup and restore for HH-DB-01 Stage 2 WP-2D (host, root).

    python deploy/postgres/maintenance.py backup  --output PATH
    python deploy/postgres/maintenance.py verify-archive ARCHIVE
    python deploy/postgres/maintenance.py restore ARCHIVE --maintenance-database postgres
    python deploy/postgres/maintenance.py rehearse ARCHIVE [--run-id HEX] [--status-file PATH]
    python deploy/postgres/maintenance.py cleanup-rehearsal --run-id HEX [--status-file PATH]

This is the host-side counterpart of `enroll.py`. It runs as root with the Docker
socket, verifies the enrolled cluster with the same authoritative Mode B procedure
(ENR-15..ENR-17), and then runs the backup/restore roles as **disposable PostgreSQL
client containers** built from the digest-pinned `postgres:16.x` image (BAK-13).

Why disposable client containers rather than the application image: the frozen
specification's BAK-13 says `pg_dump` and `pg_restore` come from the same pinned
PostgreSQL image as the server, and that the application image contains the Python
driver but no client binaries. The Compose `backup`/`restore` app-image one-shots
therefore remain fail-closed boundaries; this tool launches the client containers with
exactly one role's passfile, no PGDATA, no Docker socket and no published port.

Backup (BAK-03/BAK-06, §8.7)
    One `REPEATABLE READ` transaction exports a snapshot; `pg_dump --snapshot=<id>`
    runs from inside that transaction with the frozen flags; every table count/digest
    is computed in the same transaction; the sequence raw pair is extracted from
    exactly one `SEQUENCE SET` TOC entry and parsed as data, never read live. The
    result is a format-v2 recovery point (MANIFEST.json, snapshot.dump.sha256,
    snapshot.dump) whose manifest is validated and read back.

Restore (RST-01..RST-08, TMP-01..TMP-10)
    A new database is created behind `ALLOW_CONNECTIONS false`, hardened before it is
    enabled, restored with `pg_restore --role=hh_owner --no-owner --no-privileges`,
    then `db/schema.sql` and `db/privileges.sql` are applied. The restored catalog,
    ownership, default ACLs, table digests, migration revision, object contract and
    sequence invariant are verified before the database may be called `verified`.
    Each temporary database has its own journal record and lifecycle.

Isolated verification (§8.6 level 2/3)
    `rehearse` starts a throwaway `postgres:16` container with `--network none`, a
    tmpfs PGDATA, no production volume and no production credential, restores the same
    archive, verifies it, runs `alembic check`, and performs a real `hh_app` smoke on a
    discarded clone. A new `system_identifier` is recorded as provenance, never as a
    requirement (ENR-17).

Concurrency (P0)
    `backup` and `restore` hold the host operation interval (`<journal-dir>/operation.lock`,
    the same non-reentrant flock `enroll.py` uses for mode-a, migrate, reconcile-migration
    and handoff) from before Mode B until the last state-changing step. The interval is
    taken first and non-blockingly, so a conflicting operation is refused with
    `operation_busy` instead of interleaving. Under that interval both an UNKNOWN handoff
    (`handoff_unknown`) and an UNKNOWN migration (`migration_unknown`) refuse the operation
    before anything is changed. The state-volume locks the backup holder takes are a
    second, container-side layer; the host never takes them. The holder is a pure lock
    owner: the archive is published by the host process itself, which holds the operation
    interval, and the holder has no output-directory access at all. Lock order and the
    deadlock argument are documented on `maintenance_interval`.

No credential is generated, printed, formatted or written by this tool. Passfiles are
validated on the host, mounted read-only into exactly one disposable container, copied
to a private `/tmp` path inside that container, and never appear in argv or logs.

Exit codes (the same shape as deploy/ops/dbtool.py and enroll.py):
  0 success, 1 internal, 2 usage, 3 refused, 4 environment unavailable, 5 UNKNOWN,
  6 CANCELLED: `rehearse` was signalled and its one disposable container was confirmed removed
    (only when the removal could be confirmed; otherwise it is 4, with the container's exact name on
    stderr so the operator can run `cleanup-rehearsal --run-id` and reconcile)

A Docker execution failure (the CLI exits 125 when it could not run the container, 126/127 when the
contained program cannot be invoked, or `docker exec` reports a stopped container) is exit 4 with
`docker_infrastructure`; it is never reported as `archive_integrity`, `sequence_extraction`,
`isolated_restore` or `attachment_proof`. Those artifact codes are reserved for a program that
actually ran and produced evidence about the archive or the restored data (F3): the programs behind
these boundaries answer with their own verdict statuses (pg_restore and the sequence extraction
answer 0 or 1; the attachment proof answers 0 or 3), so a negative status (the CLI was signalled),
an invocation status Docker itself uses, a 128+N container termination or any other status is
infrastructure, never a verdict (R3-F2).

The attachment proof adds one explicit provenance rule (R4-F3/R5): its program answers with a
single framed record on stdout, `HH-ATTACHMENT-PROOF <per-run token> <JSON>`, and with a status of
its own: 0 for a completed proof, 3 for a deliberate verification refusal. Only that record, on
that line, with this run's token, and only agreeing with those statuses, is artifact evidence.
Every other stdout byte is dependency diagnostics and is never parsed as a verdict; an uncaught
Python, SQLAlchemy, connection, import or environment exception (the ordinary status 1), an
unknown status or a contradicting record is `attachment_proof_environment`, exit 4, never
`attachment_proof`.

`rehearse` and `cleanup-rehearsal` can be asked (`--status-file PATH`) to write ONE JSON result
record, contract `house-helper-maintenance-status/1`, atomically. It carries only fixed tokens: the
contract name, the command, a validated run id, the rehearsal `status` (passed/failed/cancelled/
usage), the independent `cleanup` outcome (confirmed/unconfirmed/kept) and at most one stable
refusal `code`. No message, path, exception text or tool stderr enters it. It is written on every
path, after both outcomes are known, and is what a machine caller classifies; its stderr is
diagnostics only (R3-F3). A signal that arrives during the verified removal is deferred until the
removal finishes and then preserved in the record and the exit status (R3-F1).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for entry in (REPOSITORY_ROOT, Path(__file__).resolve().parent):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import enroll  # noqa: E402  (the accepted WP-2C-A host boundary)

from house_helper.persistence import pg_artifacts as artifacts  # noqa: E402
from house_helper.persistence import pg_backup as backup  # noqa: E402
from house_helper.persistence import pg_contract as contract  # noqa: E402
from house_helper.persistence import pg_credentials as credentials  # noqa: E402
from house_helper.persistence import pg_enrollment as enrollment  # noqa: E402
from house_helper.persistence import pg_journal as journal  # noqa: E402

DEFAULT_COMPOSE = REPOSITORY_ROOT / "deploy" / "compose.postgres.yaml"
DEFAULT_JOURNAL_DIR = Path("/var/lib/house-helper/cutover")
DEFAULT_CREDENTIALS_DIR = REPOSITORY_ROOT / "deploy" / "postgres" / "credentials"
DEFAULT_OBJECT_CONTRACT = REPOSITORY_ROOT / "tests" / "fixtures" / "pg_object_contract.json"
DEFAULT_APP_IMAGE = os.environ.get("HOUSE_HELPER_IMAGE", "house-helper:local")

DB_HOST = "house-helper-db"
DB_PORT = "5432"
STAGING = "/staging"
CREDENTIAL_TARGET = "/run/credentials"
STATE_VOLUME = "house-helper-pg-state"
# The container-side layer: taken by the disposable lock-holder container, never by the
# host (a non-reentrant flock must not be acquired on both sides of the container
# boundary). The host-side layer that excludes `enroll.py`'s operations is the journal
# directory's `operation.lock`, taken by `maintenance_interval`.
STATE_LOCK = "/state/.house-helper.lock"
BACKUP_MUTEX = "/state/.house-helper.backup.lock"
# LCK-01/P0: the holder is not given an unconditional lifetime. The host keeps a heartbeat
# deadline fresh for as long as the backup operation is actually running; an abandoned
# holder expires only after the lease has not been refreshed. The holder never publishes:
# it owns the two state-volume flocks and answers one ownership probe, nothing else.
LOCK_HOLDER_LEASE_SECONDS = 15 * 60
LOCK_HOLDER_REFRESH_SECONDS = 60
LOCK_CONTROL_TARGET = "/ctl"
LOCK_READY_NAME = ".house-helper.ready"
LOCK_DEADLINE_NAME = ".house-helper.deadline"
# P0: the only request the holder serves is an ownership/liveness probe. The host asks it
# immediately before publication and again after the read-back. There is deliberately no
# publication, confirmation-of-success or cleanup request: the holder cannot reach the
# output directory at all, so none of those channels can exist.
LOCK_CONFIRM_NAME = ".house-helper.confirm.request"
LOCK_CONFIRM_RESULT_NAME = ".house-helper.confirm.result"

EX_OK = 0
EX_INTERNAL = 1
EX_USAGE = 2
EX_REFUSED = 3
EX_UNAVAILABLE = 4
EX_UNKNOWN = 5
EX_CANCELLED = 6  # a rehearsing process was signalled; its disposable container was removed

# R3-F3: the machine-readable result contract of `rehearse` and `cleanup-rehearsal`, written (only
# when `--status-file` is given) as ONE JSON object, and never derived from stderr. Every field is a
# fixed token: a contract name, a command name, a validated run id, one of the status/cleanup
# enumerations, and at most one stable refusal code. No free text, path, exception message, tool
# stderr or credential can enter it, so untrusted diagnostic text cannot create a synthetic record.
STATUS_CONTRACT = "house-helper-maintenance-status/1"
STATUS_COMMANDS = frozenset({"rehearse", "cleanup-rehearsal"})
STATUS_VALUES = frozenset({"passed", "failed", "cancelled", "usage"})
CLEANUP_VALUES = frozenset({"confirmed", "unconfirmed", "kept"})
STATUS_FIELDS = frozenset({"contract", "command", "run_id", "status", "cleanup", "code"})
STATUS_CODE_RE = re.compile(r"[a-z0-9_]{1,64}", re.ASCII)

# The rehearsal's ownership identity (F2): one run id, generated by the caller or here, names exactly
# one disposable container. Cleanup is by that exact name only, and only after the container's image is
# confirmed to be this tool's pinned client image; a container with any other identity is never touched.
REHEARSAL_CONTAINER_PREFIX = "house-helper-restore-verify-"
RUN_ID_RE = re.compile(r"[0-9a-f]{16,64}", re.ASCII)

_RESTORE_DATABASE = re.compile(r"house_helper_restore_[a-z0-9]{1,32}")
_LOCALE = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
_DUMP_VERSION = re.compile(r"pg_dump \(PostgreSQL\) ([0-9.]+)")
_SMOKE_MARKER = "house-helper backup smoke"


class MaintenanceError(Exception):
    """A fail-closed refusal. `code` is stable; no credential is ever included."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


class RehearsalCancelled(BaseException):
    """A signal arrived while a rehearsal ran. BaseException so no `except Exception` swallows it;
    the `finally` blocks still run, so the disposable container and the work directory are removed."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _rehearsal_container_name(run_id: str) -> str:
    """The one container name owned by a rehearsal run. The id is 16-64 lowercase hex characters;
    anything else is refused before a container could be named."""
    if not RUN_ID_RE.fullmatch(run_id):
        raise MaintenanceError(
            "rehearsal_identity", "the rehearsal run id must be 16-64 lowercase hex characters"
        )
    return REHEARSAL_CONTAINER_PREFIX + run_id


def _raise_cancelled(signum, frame):  # noqa: ARG001 (the handler signature is fixed)
    raise RehearsalCancelled(signum)


@contextlib.contextmanager
def _cancellation_handlers():
    """Turn SIGTERM/SIGINT into RehearsalCancelled for the duration of a rehearsal, then put the
    previous handlers back. Without this, the default disposition kills the process between two
    bytecodes and the `finally` cleanup never runs (F2)."""
    previous = {}
    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(Exception):
                previous[number] = signal.signal(number, _raise_cancelled)
    try:
        yield
    finally:
        for number, handler in previous.items():
            with contextlib.suppress(Exception):
                signal.signal(number, handler)


class _DeferredSignals:
    """Records SIGTERM/SIGINT instead of raising, for the bounded clean-up critical section.

    Recording (rather than ignoring) preserves the signal's outcome: the clean-up finishes, and the
    caller reports the cancellation afterwards. A repeated signal at any point during the section
    is appended, never raised in the middle of a removal (R3-F1)."""

    def __init__(self) -> None:
        self.signums: list[int] = []

    def __call__(self, signum: int, frame) -> None:  # noqa: ARG002 (the handler signature is fixed)
        self.signums.append(signum)


@contextlib.contextmanager
def _deferred_cancellation():
    """Defer SIGTERM/SIGINT for the duration of the block and yield the recorder.

    This is deliberately narrow: it covers the verified removal (and the work-directory cleanup
    before it), which must not be abandoned half-way once it has started. Outside this section the
    ordinary `RehearsalCancelled` handler applies again."""
    deferred = _DeferredSignals()
    previous = {}
    for name in ("SIGTERM", "SIGINT"):
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


def _first_line(value: bytes, limit: int = 300) -> str:
    text = value.decode("utf-8", "replace").strip().splitlines()
    return (text[0] if text else "")[:limit]


# Docker's own failures are decided before (or instead of) the program inside the container.
# `docker run` exits 125 when it could not run the container at all (the daemon is unreachable, an
# argument or the image is invalid) and 126/127 when the contained program cannot be invoked.
# `docker exec` against a stopped container reports a daemon-level error. None of these is evidence
# about the archive: a command that actually ran keeps its own status, and only its caller has the
# evidence to classify that (F3/R2).
_DOCKER_EXECUTION_STATUSES = frozenset({125, 126, 127})
_DOCKER_DAEMON_ERROR = re.compile(
    rb"(?:^|\n)(?:docker: )?(?:Error response from daemon:|error during connect:|"
    rb"Cannot connect to the Docker daemon|"
    rb"permission denied while trying to connect to the Docker daemon socket)"
)
# R3-F2: the programs reached through these boundaries answer with their own verdict statuses -
# pg_restore and the sequence extraction answer 0 for success and 1 for their own refusal (the
# attachment proof answers 0 and 3; see its own constants below). Nothing else is a verdict about
# the archive: a negative status is the Docker CLI killed by a signal, 128+N (137, 143, ...) is the
# contained program terminated by a signal, and any other value is an execution outcome this
# contract does not know. A nonzero status ALONE must never become an artifact refusal.
_PROGRAM_VERDICT_STATUSES = frozenset({0, 1})


def _refuse_docker_execution(
    done: subprocess.CompletedProcess,
    operation: str,
    *,
    verdict_statuses: frozenset = _PROGRAM_VERDICT_STATUSES,
) -> None:
    """Refuse as infrastructure unless the contained program could have reached its own verdict.

    Only the invocation contract's own statuses (by default 0 and 1, and no Docker daemon error)
    are left to the caller. Everything else - a signal-killed Docker CLI, an invocation Docker
    refused, a signal-killed container, an unknown status - is `docker_infrastructure`, never
    `archive_integrity`, `sequence_extraction`, `isolated_restore` or `attachment_proof` (F3/R2,
    R3-F2). A caller whose program also has a deliberate-refusal status passes it in
    `verdict_statuses`; classification of that status stays with the caller.
    """
    verdict = done.returncode in verdict_statuses
    if verdict and not _DOCKER_DAEMON_ERROR.search(done.stderr or b""):
        return
    detail = _first_line(done.stderr or b"") or None
    raise MaintenanceError(
        "docker_infrastructure",
        f"{operation} could not be carried out by Docker",
        detail,
    )


def _write_control_request(control_dir: object, request_name: str, body: str) -> None:
    """Atomically place one request in a holder's control directory.

    LF only: the holder reads line-wise, and a host-side text mode would append `\\r`
    on Windows. A temporary name followed by `os.replace` means the holder can never read
    a half-written request.
    """
    control = Path(control_dir)
    temporary = control / f"{request_name}.{secrets.token_hex(4)}"
    temporary.write_bytes(body.encode("utf-8"))
    os.replace(temporary, control / request_name)


# --- Docker tool containers -----------------------------------------------------------------


class ToolContainers:
    """The disposable client-container boundary: fixed argv, no shell discovery."""

    def __init__(self, image: str, *, runner=subprocess.run) -> None:
        if not re.fullmatch(r"postgres@sha256:[0-9a-f]{64}", image):
            raise MaintenanceError(
                "image_reference", "the PostgreSQL client image is not the pinned digest"
            )
        self.image = image
        self.runner = runner

    # -- container primitives ---------------------------------------------------------

    def _run(
        self,
        argv: Sequence[str],
        *,
        stdin: Optional[bytes] = None,
        timeout: int = 120,
        env: Optional[Mapping[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        try:
            return self.runner(
                list(argv),
                input=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=None if env is None else dict(env),
            )
        except subprocess.TimeoutExpired:
            raise MaintenanceError(
                "docker_timeout", "a disposable client container did not finish in time"
            ) from None
        except OSError:
            raise MaintenanceError(
                "docker_unavailable", "the Docker CLI could not be run"
            ) from None

    def image_digest(self) -> Mapping[str, Any]:
        done = self._run(["docker", "image", "inspect", self.image])
        if done.returncode != 0:
            raise MaintenanceError(
                "image_unavailable", "the pinned PostgreSQL client image is not present"
            )
        try:
            value = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MaintenanceError("image_identity", "the image inspection is not JSON") from None
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise MaintenanceError("image_identity", "the image inspection is not one object")
        if self.image not in (value[0].get("RepoDigests") or []):
            raise MaintenanceError(
                "image_identity", "the client image is not the pinned digest"
            )
        return value[0]

    def inspect_volume(self, name: str) -> Mapping[str, Any]:
        done = self._run(["docker", "volume", "inspect", name])
        if done.returncode != 0:
            raise MaintenanceError(
                "state_volume", "the maintenance-lock state volume is absent"
            )
        try:
            value = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MaintenanceError("state_volume", "the state volume inspection is not JSON") from None
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise MaintenanceError("state_volume", "the state volume inspection is not one object")
        labels = value[0].get("Labels") or {}
        if (
            value[0].get("Name") != name
            or labels.get("com.docker.compose.project") != contract.DEPLOYMENT_COMPOSE_PROJECT
            or labels.get("com.docker.compose.volume") != "state"
            or value[0].get("Scope") != "local"
        ):
            raise MaintenanceError(
                "state_volume", "the state volume is not the recorded deployment volume"
            )
        return value[0]

    def run(
        self,
        program: Sequence[str],
        *,
        mounts: Sequence[tuple[object, str, bool]] = (),
        volumes: Sequence[tuple[str, str]] = (),
        environment: Sequence[str] = (),
        network: Optional[str] = None,
        stdin: Optional[bytes] = None,
        timeout: int = 120,
        name: Optional[str] = None,
        user: str = "0:0",
        image: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        argv = ["docker", "run", "-i", "--rm", "--user", user]
        if name is not None:
            argv += ["--name", name]
        if network is not None:
            argv += ["--network", network]
        for key in environment:
            argv += ["-e", key]
        for source, target, read_only in mounts:
            mount = f"{_docker_path(source)}:{target}"
            if read_only:
                mount += ":ro"
            argv += ["-v", mount]
        for name, target in volumes:
            argv += ["-v", f"{name}:{target}"]
        argv += [image or self.image, *program]
        return self._run(argv, stdin=stdin, timeout=timeout)

    def start_detached(
        self,
        program: Sequence[str],
        *,
        name: str,
        environment: Sequence[str] = (),
        network: str = "none",
        tmpfs: Sequence[str] = (),
        volumes: Sequence[tuple[str, str]] = (),
        mounts: Sequence[tuple[object, str, bool]] = (),
        user: str = "0:0",
    ) -> str:
        argv = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            "--user",
            user,
        ]
        for value in tmpfs:
            argv += ["--tmpfs", value]
        for volume, target in volumes:
            argv += ["-v", f"{volume}:{target}"]
        for source, target, read_only in mounts:
            mount = f"{_docker_path(source)}:{target}"
            if read_only:
                mount += ":ro"
            argv += ["-v", mount]
        for key in environment:
            argv += ["-e", key]
        argv += [self.image, *program]
        done = self._run(argv, timeout=180)
        if done.returncode != 0:
            raise MaintenanceError(
                "container_start", "the disposable verification container did not start"
            )
        container = done.stdout.decode("utf-8", "replace").strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            self.remove(name)
            raise MaintenanceError(
                "container_identity", "the disposable container id is malformed"
            )
        return container

    # -- the backup mutex, held across the whole logical backup ------------------------

    def _start_holder(
        self,
        script: str,
        *,
        name: str,
        control_dir: object,
        timeout: float = 30.0,
    ) -> str:
        ready = Path(control_dir) / LOCK_READY_NAME
        try:
            ready.unlink()
        except FileNotFoundError:
            pass
        container = self.start_detached(
            ["sh", "-c", script],
            name=name,
            volumes=[(STATE_VOLUME, "/state")],
            # P0: the holder receives the state volume and its private control directory
            # ONLY. The output directory is deliberately not mounted: the holder cannot
            # create, replace or remove an archive, even after its host is gone.
            mounts=[(control_dir, LOCK_CONTROL_TARGET, False)],
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready.exists():
                return container
            running = self._run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container],
                timeout=30,
            )
            if running.returncode != 0 or running.stdout.strip() == b"false":
                break
            time.sleep(0.1)
        self.remove(container)
        raise MaintenanceError(
            "backup_lock",
            "the maintenance lock is busy or another backup holds the mutex",
        )

    def start_lock_holder(
        self,
        *,
        name: str,
        control_dir: object,
        timeout: float = 30.0,
    ) -> str:
        """LCK-01/P0: start the detached owner of both state-volume locks.

        The holder receives the shared state volume and one private control directory (a
        heartbeat deadline and the ownership probe). It has no output-directory mount and
        no publication branch: publication belongs to the host operation that holds
        `<journal-dir>/operation.lock`.
        """
        return self._start_holder(
            _lock_holder_script(),
            name=name,
            control_dir=control_dir,
            timeout=timeout,
        )

    def confirm_lock_holder(
        self, container: str, *, control_dir: object, timeout: float = 60.0
    ) -> None:
        """P0: the holder acknowledges that it is alive and still owns both flocks.

        The host asks the holder itself immediately before publication and again after the
        published archive has been read back. A process that has exited, been paused, or
        let its lease lapse cannot answer, so an operation whose state-volume mutex was
        lost fails instead of publishing or reporting a backup.
        """
        control = Path(control_dir)
        result = control / LOCK_CONFIRM_RESULT_NAME
        try:
            result.unlink()
        except FileNotFoundError:
            pass
        _write_control_request(control, LOCK_CONFIRM_NAME, "confirm\n")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result.exists():
                value = result.read_text(encoding="utf-8").strip()
                try:
                    result.unlink()
                except FileNotFoundError:
                    pass
                if value == "alive":
                    return
                raise MaintenanceError(
                    "backup_lock", "the lock holder did not acknowledge ownership"
                )
            running = self._run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container],
                timeout=30,
            )
            if running.returncode != 0 or running.stdout.strip() == b"false":
                raise MaintenanceError(
                    "backup_lock",
                    "the lock holder terminated before it could acknowledge ownership",
                )
            time.sleep(0.05)
        raise MaintenanceError(
            "backup_lock", "the lock holder did not acknowledge ownership in time"
        )

    def exec(
        self,
        container: str,
        program: Sequence[str],
        *,
        stdin: Optional[bytes] = None,
        user: str = "postgres",
        timeout: int = 300,
    ) -> subprocess.CompletedProcess:
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise MaintenanceError("container_identity", "the container id is malformed")
        return self._run(
            ["docker", "exec", "-i", "-u", user, container, *program],
            stdin=stdin,
            timeout=timeout,
        )

    def copy_in(self, container: str, source: object, target: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise MaintenanceError("container_identity", "the container id is malformed")
        done = self._run(["docker", "cp", str(source), f"{container}:{target}"], timeout=180)
        if done.returncode != 0:
            raise MaintenanceError(
                "container_copy", "a file could not be copied into the disposable container"
            )

    def remove(self, container: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            return
        # `-v` removes only ANONYMOUS volumes. Every container this boundary removes is one
        # it started itself and is disposable: the detached holders mount the deployment's
        # NAMED state volume at /state, which `docker rm -v` never removes (verified against
        # the pinned image). Without it, the pinned PostgreSQL image's anonymous
        # `/var/lib/postgresql/data` volume outlives every normal or forced holder removal.
        self._run(["docker", "rm", "-f", "-v", container], timeout=60)

    def present_container(self, name: str) -> Optional[str]:
        """The id of the ONE container whose name is exactly `name`, or None. The filter is anchored,
        so a similarly named container is not a match. A listing that fails is a refusal, never an
        empty answer: "cannot tell" must not be read as "gone"."""
        done = self._run(
            ["docker", "container", "ls", "-a", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"],
            timeout=60,
        )
        if done.returncode != 0:
            raise MaintenanceError("docker_inspect", "the disposable containers could not be listed")
        found = [line.strip() for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        if len(found) > 1:
            raise MaintenanceError("container_identity", "more than one container carries the rehearsal name")
        return found[0] if found else None

    def remove_verified(self, name: str, *, image: Optional[str] = None) -> bool:
        """Remove the disposable container of ONE run and confirm it is gone.

        Ownership is the exact name. When `image` is given, a container of that name whose
        `Config.Image` is not this tool's pinned image is left untouched (fail closed): a name is
        unique, but a container that merely squats the name is not this run's. Returns True only
        when the container is confirmed absent afterwards; any inability to list, inspect or remove
        returns False, never a false success."""
        try:
            identifier = self.present_container(name)
            if identifier is None:
                return True
            if image is not None:
                done = self._run(["docker", "inspect", "--format", "{{.Config.Image}}", identifier], timeout=60)
                if done.returncode != 0:
                    return False
                if done.stdout.decode("utf-8", "replace").strip() != image:
                    return False
            # `-v` removes only ANONYMOUS volumes of the one container this run owns (a
            # rehearsal container, whose PGDATA is a tmpfs and therefore has none). Named
            # volumes are never removed by `docker rm -v`, and the identity checks above are
            # unchanged.
            self._run(["docker", "rm", "-f", "-v", identifier], timeout=60)
            return self.present_container(name) is None
        except MaintenanceError:
            return False

    def pg_dump_version(self) -> str:
        done = self.run(["pg_dump", "--version"], timeout=60)
        match = _DUMP_VERSION.search(done.stdout.decode("utf-8", "replace"))
        if done.returncode != 0 or not match:
            raise MaintenanceError(
                "dump_version", "the pinned pg_dump version could not be read"
            )
        return match.group(1)

    def pg_restore_list(self, dump: object) -> str:
        done = self.run(
            ["pg_restore", "-l", f"{STAGING}/{backup.DUMP_NAME}"],
            mounts=[(dump, STAGING, True)],
            network=None,
            timeout=120,
        )
        # A Docker/tooling failure is not evidence about the archive (F3/R2): only a pg_restore
        # that actually ran and refused the file is an archive-integrity refusal.
        _refuse_docker_execution(done, "the archive listing")
        if done.returncode != 0:
            raise MaintenanceError(
                "archive_integrity",
                "pg_restore cannot list the archive",
                _first_line(done.stderr),
            )
        return done.stdout.decode("utf-8", "replace")

    def sequence_extraction(self, work: object, list_text: str) -> str:
        work_path = Path(work)
        (work_path / "sequence-set.list").write_text(list_text, encoding="utf-8")
        done = self.run(
            [
                "pg_restore",
                f"--use-list={STAGING}/sequence-set.list",
                "--no-owner",
                "--no-privileges",
                "--file=-",
                f"{STAGING}/{backup.DUMP_NAME}",
            ],
            mounts=[(work, STAGING, True)],
            network=None,
            timeout=120,
        )
        _refuse_docker_execution(done, "the SEQUENCE SET extraction")
        if done.returncode != 0:
            raise MaintenanceError(
                "sequence_extraction",
                "the scoped SEQUENCE SET extraction failed",
                _first_line(done.stderr),
            )
        return done.stdout.decode("utf-8", "replace")


def _docker_path(value: object) -> str:
    text = str(value)
    if os.name == "nt":
        return text.replace("\\", "/")
    return text


# --- passfiles -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Passfile:
    role: str
    host_path: Path

    @property
    def container_path(self) -> str:
        return f"{CREDENTIAL_TARGET}/{self.role}.pgpass"


def passfile(directory: object, role: str, *, database: str) -> Passfile:
    if role not in ("hh_backup", "hh_restore"):
        raise MaintenanceError("role", "the maintenance tool has no such role")
    path = Path(directory) / f"{role}.pgpass"
    try:
        credentials.validate_passfile(
            path,
            login=role,
            host=DB_HOST,
            port=DB_PORT,
            database=database,
            require_private=(os.name == "posix"),
            require_owner=(os.name == "posix"),
            owner_uid=credentials.RUNTIME_UID,
            owner_gid=credentials.RUNTIME_GID,
        )
    except credentials.PassfileError as error:
        raise MaintenanceError(error.code, error.message, error.detail) from None
    return Passfile(role=role, host_path=path)


def _wrapped(passfile_value: Passfile, program: Sequence[str]) -> list[str]:
    """Run `program` with a private in-container copy of exactly one passfile.

    A Docker Desktop bind mount from Windows is reportable as world-readable, and libpq
    refuses a passfile with group/other access. Copying it to `/tmp` with mode 0600
    inside the disposable container gives libpq the mode it requires without putting
    the secret in argv or in the container environment.
    """
    script = (
        'install -m 600 "$1" /tmp/house-helper.pgpass'
        ' && shift'
        ' && PGPASSFILE=/tmp/house-helper.pgpass exec "$@"'
    )
    return ["sh", "-c", script, "hh-passfile", passfile_value.container_path, *program]


def _lock_commands(
    *, state_lock: str = STATE_LOCK, backup_mutex: str = BACKUP_MUTEX
) -> str:
    """LCK-01: take the shared maintenance lock and the exclusive backup mutex, or exit 3.

    Both locks live in the shared state volume, exactly as the web process's shared lock
    and the backup mutex do. `flock -s` coexists with serving and fails closed when an
    exclusive maintenance holder exists; `flock -x` fails closed when another backup is
    already staging a point. The descriptors are inherited across `exec`, so the locks
    are released by the kernel when the process (or container) ends.

    The two paths are parameters only so the contract can be executed against a private
    directory in tests; every production caller uses the state volume.
    """
    return (
        "exec 9>" + state_lock + " || exit 1; "
        "exec 8>" + backup_mutex + " || exit 1; "
        'flock -s -n 9 || { echo "refused [backup_lock]: the maintenance lock is busy" >&2; exit 3; }; '
        'flock -x -n 8 || { echo "refused [backup_lock]: another backup holds the mutex" >&2; exit 3; }; '
    )


def _holder_deadline_check(control_dir: str) -> str:
    """The heartbeat deadline is the only expiry: an abandoned holder exits."""
    return (
        f'deadline="$(cat {control_dir}/{LOCK_DEADLINE_NAME} 2>/dev/null || echo 0)"; '
        'now="$(date +%s)"; '
        'if [ "$deadline" -gt 0 ] && [ "$now" -gt "$deadline" ]; then exit 7; fi; '
    )


def _lock_holder_script(
    *,
    control_dir: str = LOCK_CONTROL_TARGET,
    state_lock: str = STATE_LOCK,
    backup_mutex: str = BACKUP_MUTEX,
) -> str:
    """LCK-01/P0: the detached owner of both state-volume flocks. It cannot publish.

    The holder keeps the shared maintenance lock and the exclusive backup mutex for the
    whole backup, signals readiness, and answers one ownership probe. It has no
    output-directory mount and no publication, success or cleanup branch: publication is
    performed by the host process, which holds `<journal-dir>/operation.lock` across the
    atomic link/rename. A holder that outlives its host therefore cannot create, replace
    or remove an archive; it can only release the state-volume flocks when its lease
    lapses.

    A heartbeat deadline (refreshed by the host while the operation is active) is the
    only expiry: a live operation never expires, and an abandoned holder exits instead of
    blocking every later backup.

    The path parameters exist so the contract can be executed end-to-end against a
    private directory in tests; production always uses the container mounts.
    """
    return (
        _lock_commands(state_lock=state_lock, backup_mutex=backup_mutex)
        + f"touch {control_dir}/{LOCK_READY_NAME}; "
        "while :; do "
        + _holder_deadline_check(control_dir)
        # P0: the only request the holder serves. Readiness is written only after both
        # flocks are taken, so answering here proves the process is alive and still owns
        # them. The host asks before publication and again after the read-back.
        + f"  if [ -f {control_dir}/{LOCK_CONFIRM_NAME} ]; then "
        f"    rm -f {control_dir}/{LOCK_CONFIRM_NAME}; "
        f"    printf 'alive\\n' > {control_dir}/{LOCK_CONFIRM_RESULT_NAME}.tmp; "
        f"    mv -f {control_dir}/{LOCK_CONFIRM_RESULT_NAME}.tmp"
        f" {control_dir}/{LOCK_CONFIRM_RESULT_NAME}; "
        "  fi; "
        "  sleep 0.2; "
        "done"
    )


def _tool_environment(role: str) -> tuple[str, ...]:
    # AUTH-03: non-secret libpq settings. The credential is never one of them.
    return (
        f"PGHOST={DB_HOST}",
        f"PGPORT={DB_PORT}",
        f"PGUSER={role}",
        f"PGAPPNAME={role}",
    )


# --- SQL helpers -----------------------------------------------------------------------------


def psql(
    tools: ToolContainers,
    passfile_value: Passfile,
    *,
    database: str,
    sql: str,
    network: str,
    timeout: int = 300,
) -> str:
    program = [
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-F",
        "|",
        "-v",
        "ON_ERROR_STOP=1",
        "-d",
        database,
        "-f",
        "-",
    ]
    done = tools.run(
        _wrapped(passfile_value, program),
        mounts=[(passfile_value.host_path, passfile_value.container_path, True)],
        environment=_tool_environment(passfile_value.role),
        network=network,
        stdin=sql.encode("utf-8"),
        timeout=timeout,
    )
    if done.returncode != 0:
        raise MaintenanceError(
            "sql_refused",
            "the database refused a statement",
            _first_line(done.stderr),
        )
    return done.stdout.decode("utf-8", "replace")


def pg_restore(
    tools: ToolContainers,
    passfile_value: Passfile,
    *,
    database: str,
    work: object,
    use_list: bool,
    network: str,
    timeout: int = 900,
) -> None:
    program = [
        "pg_restore",
        "--exit-on-error",
        "--role=hh_owner",
        "--no-owner",
        "--no-privileges",
    ]
    if use_list:
        program.append(f"--use-list={STAGING}/restore.list")
    program += ["--dbname", database, f"{STAGING}/{backup.DUMP_NAME}"]
    done = tools.run(
        _wrapped(passfile_value, program),
        mounts=[
            (passfile_value.host_path, passfile_value.container_path, True),
            (work, STAGING, True),
        ],
        environment=_tool_environment(passfile_value.role),
        network=network,
        timeout=timeout,
    )
    if done.returncode != 0:
        raise MaintenanceError(
            "restore_failed",
            "pg_restore did not complete with --exit-on-error",
            _first_line(done.stderr),
        )


def connect_probe(
    tools: ToolContainers,
    passfile_value: Passfile,
    *,
    database: str,
    network: str,
) -> subprocess.CompletedProcess:
    """One `SELECT 1` as the role. The return code is the observed fact."""
    program = [
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-d",
        database,
        "-c",
        "SELECT 1;",
    ]
    return tools.run(
        _wrapped(passfile_value, program),
        mounts=[(passfile_value.host_path, passfile_value.container_path, True)],
        environment=_tool_environment(passfile_value.role),
        network=network,
        timeout=60,
    )


# --- Mode B ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    spec: enroll.ClusterSpec
    completed: Mapping[str, Any]
    journal_file: journal.Journal

    @property
    def system_identifier(self) -> str:
        return str(self.completed.get("system_identifier", ""))


def verify_target(
    compose_file: Path,
    journal_dir: Path,
    *,
    journal_file: Optional[journal.Journal] = None,
) -> Target:
    """Verify the target cluster for one maintenance operation.

    The result is an *authorization*: it is only meaningful while `maintenance_interval`
    holds the host operation interval. Callers hold that interval from before this check
    until the protected operation (publication, restore, drop) has finished; a bare call
    from a test exercises the verdicts, not the mutual exclusion.
    """
    if journal_file is None:
        journal_file = journal.Journal(
            Path(journal_dir), require_root=(os.name == "posix")
        )
    # LCK-08/LCK-13: a handoff intent without an observed outcome is UNKNOWN; only the
    # handoff entry point may reconcile it. Both callers of this target check (backup and
    # restore) advance the machine, so neither may proceed through that state. The gate is
    # the FIRST thing this check does - before the compose file, the image inspection and
    # Mode B - so a journaled UNKNOWN refuses ahead of every cluster read that could
    # journal a mismatch, and before any state-changing step. It is not inside Mode B
    # itself: the handoff reconciler calls Mode B to observe the cluster and must not be
    # blocked by the intent it is resolving.
    blocked = enroll.pending_handoff_refusal(journal_file.read().records)
    if blocked is not None:
        raise MaintenanceError("handoff_unknown", blocked)
    # LCK-08/LCK-13: a migrate intent without an observed outcome is UNKNOWN too. It is
    # checked under the same host operation interval (the caller holds
    # `operation.lock`), and only `enroll.py reconcile-migration` may release it. Both
    # maintenance operations advance the machine, so neither may proceed through that
    # state; the gate is ahead of the compose parse, the image inspection, Mode B, the
    # archive publication and any `CREATE DATABASE`.
    migration = enrollment.pending_migration_unknown(journal_file.read().records)
    if migration is not None:
        run_id = str(migration.payload.get("run_id", ""))[:80]
        raise MaintenanceError(
            "migration_unknown",
            "an earlier migrate run has no observed outcome; run "
            "`enroll.py reconcile-migration` before a backup or restore",
            f"run_id {run_id}",
        )
    spec = enroll.load_spec(compose_file)
    docker = enroll.Docker(spec)
    try:
        enroll.validate_image(spec, docker.inspect_image())
    except enroll.DockerError as error:
        raise MaintenanceError(error.code, error.message, error.detail) from None
    report = enroll.verify_mode_b(
        journal_file, enroll.ClusterAdapter(docker, compose_file=compose_file)
    )
    if not report.verdict.ok:
        raise MaintenanceError(
            "cluster_identity",
            "Mode B refused the live cluster; nothing was changed",
            ", ".join(report.verdict.mismatches),
        )
    return Target(spec=spec, completed=dict(report.verdict.completed or {}), journal_file=journal_file)


@contextlib.contextmanager
def maintenance_interval(compose_file: Path, journal_dir: Path):
    """P0: the host operation interval every cluster-advancing command holds.

    `backup` and `restore` take this interval at their entry point, before Mode B, and
    keep it until their last state-changing step (archive publication and read-back, the
    temporary database's creation/verification/drop). The handoff, migrate, mode-a and
    reconcile-migration entry points take the same interval inside `enroll.py`, so all of
    them are mutually exclusive ACROSS PROCESSES, not merely within one.

    Lock order - the one direction, never reversed:

    1. `<journal-dir>/operation.lock`  - this interval, host side, EXCLUSIVE. "Shared" in
       LCK-01 means the state-volume lock a backup holds in shared mode so the serving app
       keeps running; the host interval is the one common lock the host operations must
       share, and it has to be exclusive for them to exclude each other;
    2. `<journal-dir>/journal.lock`    - one journal append, inside the interval;
    3. the state-volume flocks          - `/state/.house-helper.lock` (shared: coexists
       with serving) and `/state/.house-helper.backup.lock` (exclusive), taken only by the
       disposable lock-holder CONTAINER, never by the host.

    Layers 1-2 are taken by the host, layer 3 by a container that never takes 1 or 2, so
    the wait-for graph has one direction and no cycle. Every acquisition is
    non-blocking (`flock -n` / `LOCK_NB`): a conflict is an immediate refusal, never a
    wait, so a stuck holder cannot deadlock a successor.

    The archive publication is performed by the host process while it holds layer 1, so
    the authorization boundary and the publication are the same process and the same
    lock. The container-side holder (layer 3) can never publish: a holder that outlives
    its host is inert and expires on its lease.

    The interval is NOT reentrant - an `flock` belongs to the descriptor - so a command
    takes it exactly once. It carries no database authority; it only serializes this
    tool's own supported operations. An interrupted process releases it when the kernel
    closes its descriptors, and releases it without any record that could be read as a
    completed operation: the journal intent/outcome records of the operation itself stay
    authoritative. Releasing the interval never resolves an unresolved database operation.
    """
    journal.reject_conflicting_journal_directories(journal_dir)
    journal_file = journal.Journal(
        Path(journal_dir), require_root=(os.name == "posix")
    )
    with journal_file.operation_lock():
        yield verify_target(compose_file, journal_dir, journal_file=journal_file)

# --- archive validation ----------------------------------------------------------------------


def validate_archive(
    archive: object, tools: ToolContainers, *, work: object
) -> tuple[backup.RecoveryPoint, tuple[backup.TocEntry, ...]]:
    """Level 1: the archive's structure *and* its scoped payloads, before any restore.

    The TOC is not trusted on its own (BAK-08/§8.6). Three independent facts are checked
    here, all before a target database exists:

    * exactly one ``SEQUENCE SET`` entry, and its extracted payload parsed as data and
      compared with the manifest's raw pair (SEQ-03/SEQ-05/SEQ-06/SEQ-07);
    * exactly one ``TABLE DATA`` entry for every table of the frozen object contract;
    * the checksum and canonical manifest already enforced by ``read_archive``.
    """
    point = backup.read_archive(archive)
    work_path = Path(work)
    (work_path / backup.DUMP_NAME).write_bytes(point.dump)
    toc = tools.pg_restore_list(work_path)
    entries = backup.parse_toc(toc)
    sequence_entry = backup.select_sequence_set(entries)
    backup.select_table_data(entries, point.manifest["tables"])
    extraction = backup.parse_sequence_extraction(
        tools.sequence_extraction(work_path, sequence_entry.list_line() + "\n")
    )
    mismatches = backup.compare_sequence(
        point.manifest["sequence"], (extraction.last_value, extraction.is_called)
    )
    if mismatches:
        raise MaintenanceError(
            "archive_sequence",
            "the archive's SEQUENCE SET payload does not match the manifest",
            ", ".join(mismatches),
        )
    return point, entries


def manifest_summary(point: backup.RecoveryPoint) -> str:
    manifest = point.manifest
    tables = manifest["tables"]
    rows = sum(int(fact["rows"]) for fact in tables.values())
    sequence = manifest["sequence"]
    return (
        f"{manifest['alembic_revision']}; {len(tables)} tables, {rows} rows; "
        f"sequence ({sequence['last_value']}, {str(sequence['is_called']).lower()}); "
        f"dump {manifest['snapshot']['bytes']} bytes "
        f"sha256:{manifest['snapshot']['sha256'][:16]}..."
    )


# --- the backup lock and publication channel -------------------------------------------------


class _BackupLock:
    """LCK-01/P0: the state-volume locks, owned by a holder that cannot publish.

    The detached holder (a disposable, network-less client container) owns both flocks for
    the whole backup and can do nothing else: it has no output-directory mount and serves
    only an ownership probe. Publication belongs to the HOST process, which performs the
    atomic link/rename while it holds `<journal-dir>/operation.lock`. The host refreshes a
    heartbeat deadline, so a legitimately active holder never expires, while a crashed
    host's holder exits after the unrefreshed lease instead of blocking every later backup.
    """

    def __init__(
        self,
        tools: ToolContainers,
        *,
        name: str,
        control_dir: object,
        output_dir: object,
        lease_seconds: int = LOCK_HOLDER_LEASE_SECONDS,
    ) -> None:
        self.tools = tools
        self.name = name
        self.control_dir = Path(control_dir)
        self.output_dir = Path(output_dir)
        self.lease_seconds = lease_seconds
        self.container: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def _deadline(self) -> Path:
        return self.control_dir / LOCK_DEADLINE_NAME

    def _write_deadline(self) -> None:
        temporary = self.control_dir / f"{LOCK_DEADLINE_NAME}.{secrets.token_hex(4)}"
        temporary.write_text(str(int(time.time()) + self.lease_seconds), encoding="utf-8")
        os.replace(temporary, self._deadline)

    def _refresh(self) -> None:
        while not self._stop.wait(LOCK_HOLDER_REFRESH_SECONDS):
            try:
                self._write_deadline()
            except OSError:
                return

    def acquire(self) -> None:
        for name in (
            LOCK_READY_NAME,
            LOCK_DEADLINE_NAME,
            LOCK_CONFIRM_NAME,
            LOCK_CONFIRM_RESULT_NAME,
        ):
            try:
                (self.control_dir / name).unlink()
            except FileNotFoundError:
                pass
        self._write_deadline()
        self.container = self.tools.start_lock_holder(
            name=self.name,
            control_dir=self.control_dir,
        )
        self._thread = threading.Thread(
            target=self._refresh, name="backup-lock-heartbeat", daemon=True
        )
        self._thread.start()

    def assert_owned(self) -> None:
        """The holder confirms it is alive and still owns both state-volume flocks."""
        if self.container is None:
            raise MaintenanceError("backup_lock", "the backup mutex is not held")
        self.tools.confirm_lock_holder(self.container, control_dir=self.control_dir)

    def publish(
        self, staging: Path, output: Path, replace: bool
    ) -> Optional[tuple[int, int]]:
        """Publish the archive on the host, while the host operation interval is held.

        The host is the authorization boundary: it holds `<journal-dir>/operation.lock`
        from before Mode B until after the read-back, so no handoff, migration or second
        backup/restore can change the cluster between this call and the completed backup.
        The holder is asked to confirm ownership immediately before the atomic link, so a
        holder that has died or lapsed refuses the publication; a holder that outlives the
        host can never publish, replace or remove anything because it has no access to
        this directory.
        """
        if (
            Path(staging).parent.resolve() != self.output_dir.resolve()
            or Path(output).parent.resolve() != self.output_dir.resolve()
        ):
            raise MaintenanceError(
                "output_path", "the archive is not published into the locked output directory"
            )
        try:
            Path(staging).stat()
        except OSError:
            raise MaintenanceError("output_path", "the staging file is absent") from None
        self.assert_owned()
        return _host_publish(staging, output, replace)

    def cleanup(self, output: object, identity: Optional[Sequence[int]]) -> bool:
        """Remove a failed publication only when it is exactly the inode we published.

        The removal runs in this host process while it still holds the host operation
        interval, so no successor operation can publish concurrently. The identity check
        additionally refuses to remove any file this operation did not create (a staging
        file that was replaced, or a path that never became ours). This method never
        raises: failure cleanup must not mask the original failure.
        """
        if identity is None:
            return False
        try:
            expected = (int(identity[0]), int(identity[1]))
        except (TypeError, ValueError, IndexError):
            return False
        target = Path(output)
        try:
            if target.parent.resolve() != self.output_dir.resolve():
                return False
        except OSError:
            return False
        try:
            return _unlink_if_identity(target, expected)
        except Exception:  # noqa: BLE001 - cleanup must never mask the failure
            return False

    def confirm(self) -> None:
        """The holder confirms it still owns both flocks after the archive was read back."""
        self.assert_owned()

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self.container is not None:
            self.tools.remove(self.container)
            self.container = None


def _file_identity(path: object) -> Optional[tuple[int, int]]:
    """The same-filesystem identity of a published archive (`st_dev`, `st_ino`)."""
    try:
        value = Path(path).stat()
    except OSError:
        return None
    return (value.st_dev, value.st_ino)


def _unlink_if_identity(output: Path, identity: Optional[Sequence[int]]) -> bool:
    """Remove `output` only when its identity is still the expected one.

    Used by `_publish_archive`'s failure cleanup and by `_BackupLock.cleanup`. The host
    that calls it holds the host operation interval, so no successor operation can
    publish concurrently; the identity check additionally refuses to remove anything this
    operation did not create. Returns whether a file was removed.
    """
    if identity is None:
        return False
    try:
        expected = (int(identity[0]), int(identity[1]))
    except (TypeError, ValueError, IndexError):
        return False
    if Path(output).is_symlink():
        return False
    if _file_identity(output) != expected:
        return False
    try:
        Path(output).unlink()
    except OSError:
        return False
    return True


def _host_publish(
    staging: Path, output: Path, replace: bool
) -> Optional[tuple[int, int]]:
    """The host-side atomic publication: a hard link, or an authorized rename.

    The caller (the host backup process) holds `<journal-dir>/operation.lock`, so this is
    the one authorization boundary for publication; no detached helper performs it. The
    returned identity is the published file's, which equals the staged file's because
    `link` and `replace` preserve the inode.
    """
    if replace:
        os.replace(staging, output)
        return _file_identity(output)
    try:
        # `link` is atomic and fails if the final path exists; unlike a separate
        # existence check followed by `replace`, it cannot overwrite a competing file.
        os.link(staging, output)
    except FileExistsError:
        raise MaintenanceError(
            "output_exists", "the output path already exists; refusing to overwrite"
        ) from None
    except OSError as error:
        raise MaintenanceError(
            "output_path",
            "the archive could not be published without replacing an existing path",
            str(error),
        ) from None
    identity = _file_identity(output)
    try:
        staging.unlink()
    except OSError:
        pass
    return identity


def _publish_archive(
    output: Path,
    *,
    manifest: Mapping[str, Any],
    dump: bytes,
    overwrite: bool,
    publish: Optional[Callable[[Path, Path, bool], Optional[tuple[int, int]]]] = None,
    confirm: Optional[Callable[[], None]] = None,
    cleanup: Optional[Callable[[Path, Optional[tuple[int, int]]], object]] = None,
) -> backup.RecoveryPoint:
    """Write into a private staging file, validate it, then publish atomically.

    `publish` is the host's publication step while it holds the host operation interval;
    the default is the same atomic no-replace link for direct callers. With
    `overwrite=false` an existing final path is never replaced, even if it appears between
    the last check and publication. A failure leaves no output and removes the staging
    file, so an incomplete archive is never visible as a recovery point.

    `cleanup` is the failure cleanup of a published output (identity-checked, and under
    the host interval that excludes a successor). It removes the output only if it is
    still the exact inode `publish` returned.
    """
    if output.is_symlink():
        raise MaintenanceError(
            "output_path", "the output path is a symlink; refusing to write through it"
        )
    if not output.parent.is_dir():
        raise MaintenanceError("output_path", "the output directory does not exist")
    if output.exists() and not overwrite:
        raise MaintenanceError(
            "output_exists", "the output path already exists; refusing to overwrite"
        )
    staging = output.parent / f".{output.name}.partial-{secrets.token_hex(8)}"
    published = False
    identity: Optional[tuple[int, int]] = None
    try:
        backup.write_archive(staging, manifest=manifest, dump=dump)
        point = backup.read_archive(staging)
        identity = (publish or _host_publish)(staging, output, overwrite)
        published = True
        # Read back the published path itself; this is the artifact an operator sees.
        check = backup.read_archive(output)
        if check.manifest != point.manifest or check.dump != point.dump:
            raise MaintenanceError(
                "output_verify", "the published archive did not read back"
            )
        # The holder confirms ownership after publication *and* read-back, so success is
        # never reported for an operation whose state-volume ownership is already gone.
        if confirm is not None:
            confirm()
    except BaseException:
        if published:
            # Never unlink unconditionally. The host interval excludes a successor, and
            # the identity check refuses to remove anything this operation did not
            # publish.
            if cleanup is not None:
                try:
                    cleanup(output, identity)
                except Exception:  # noqa: BLE001 - preserve the original failure
                    pass
            elif publish is None:
                _unlink_if_identity(output, identity)
        raise
    finally:
        try:
            if staging.exists():
                staging.unlink()
        except OSError:
            pass
    return point


def backup_command(args) -> int:
    """P0: the whole backup, publication included, runs inside one host operation interval."""
    with maintenance_interval(args.compose_file, args.journal_dir) as target:
        return _backup_command_locked(args, target)


def _backup_command_locked(args, target: Target) -> int:
    tools = ToolContainers(target.spec.image)
    tools.image_digest()
    role_passfile = passfile(args.credentials_dir, "hh_backup", database=contract.SERVING_DATABASE)
    tools.inspect_volume(STATE_VOLUME)
    work = WorkDirectory(args.work_dir, prefix="house-helper-backup-")
    output = Path(args.output)
    if output.is_symlink():
        raise MaintenanceError(
            "output_path", "the output path is a symlink; refusing to write through it"
        )
    if not output.parent.is_dir():
        raise MaintenanceError("output_path", "the output directory does not exist")
    lock = _BackupLock(
        tools,
        name=f"house-helper-backup-hold-{secrets.token_hex(8)}",
        control_dir=work.path,
        output_dir=output.parent.resolve(),
    )
    try:
        # LCK-01/P0: the container-side holder owns the shared maintenance lock and the
        # exclusive backup mutex for the dump, the manifest, the output-path checks,
        # archive publication and its read-back. It cannot publish or even see the output
        # directory: the host process publishes while it holds the operation interval. The
        # holder is released by removing it, so an internal error cannot leave it held.
        #
        # P0: this container-side mutex is the SECOND layer. The host operation interval
        # taken by `backup_command` is the one that excludes the handoff, migrate, mode-a
        # and restore entry points, which never take a state-volume flock. The order is
        # always host interval -> state-volume locks (see `maintenance_interval`).
        lock.acquire()
        script = backup.backup_script(
            tables=contract.expected_tables(), staging=STAGING
        )
        done = tools.run(
            _wrapped(
                role_passfile,
                [
                    "psql",
                    "-X",
                    "-q",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-d",
                    contract.SERVING_DATABASE,
                    "-f",
                    "-",
                ],
            ),
            mounts=[
                (role_passfile.host_path, role_passfile.container_path, True),
                (work.path, STAGING, False),
            ],
            environment=_tool_environment("hh_backup"),
            network=target.spec.network,
            stdin=script.encode("utf-8"),
            timeout=1800,
        )
        if done.returncode != 0:
            raise MaintenanceError(
                "backup_refused",
                "the backup session did not complete",
                _first_line(done.stderr),
            )
        if not (work.path / "dump.ok").exists():
            raise MaintenanceError(
                "backup_incomplete", "the dump success marker is absent; no archive was written"
            )
        dump_path = work.path / backup.DUMP_NAME
        if not dump_path.is_file():
            raise MaintenanceError(
                "backup_incomplete", "the dump file is absent; no archive was written"
            )
        dump_bytes = dump_path.read_bytes()
        if not dump_bytes:
            raise MaintenanceError("backup_incomplete", "the dump file is empty")

        entries = backup.parse_toc(tools.pg_restore_list(work.path))
        sequence_entry = backup.select_sequence_set(entries)
        extraction = tools.sequence_extraction(work.path, sequence_entry.list_line() + "\n")
        parsed_sequence = backup.parse_sequence_extraction(extraction)
        facts = backup.parse_facts((work.path / "facts.txt").read_text(encoding="utf-8"))
        catalog = facts["sequence_catalog"]
        sequence = backup.sequence_manifest(
            parsed_sequence,
            max_messages_id=facts["max_messages_id"],
            seqmin=catalog["seqmin"],
            seqmax=catalog["seqmax"],
        )
        manifest = backup.build_manifest(
            server_version_num=facts["server_version_num"],
            system_identifier=target.system_identifier,
            pg_dump_version=tools.pg_dump_version(),
            alembic_revision=facts["revision"],
            encoding=facts["encoding"],
            datcollate=facts["datcollate"],
            datctype=facts["datctype"],
            tables=facts["tables"],
            sequence=sequence,
            dump_bytes=len(dump_bytes),
            dump_sha256=backup.sha256_bytes(dump_bytes),
        )
        point = _publish_archive(
            output,
            manifest=manifest,
            dump=dump_bytes,
            overwrite=args.overwrite,
            publish=lock.publish,
            confirm=lock.confirm,
            cleanup=lock.cleanup,
        )
        print(f"ok [backup]: {output}")
        print(f"     {manifest_summary(point)}")
        print(
            "     provenance system_identifier "
            f"{manifest['system_identifier']} (from the enrolled cluster record)"
        )
        return EX_OK
    finally:
        lock.release()
        work.cleanup(args.keep_work)


# --- temporary database records --------------------------------------------------------------


def _temp_records(journal_file: journal.Journal, database: str) -> list[journal.Record]:
    return [
        record
        for record in journal_file.read().records
        if record.kind == "temp_db"
        and isinstance(record.payload, Mapping)
        and record.payload.get("database") == database
    ]


@dataclass(frozen=True)
class _TemporaryState:
    database: str
    datallowconn: bool
    owner: str
    acl_materialized: bool
    public_entries: int
    acl: frozenset


# TMP-04/TMP-08/F3: the exact expanded database ACL established by `_harden_fenced` on a
# fresh `template0` database — the owner holds CONNECT/CREATE/TEMPORARY (non-grantable)
# and exactly one restore login holds CONNECT (non-grantable). Effective privilege checks
# are not enough: `has_database_privilege` accepts a PUBLIC grant, an extra grant option
# or an extra privilege for the same role. The comparison is set equality, not a
# containment or "at least" check.
_CREATED_FENCED_ACL = frozenset(
    {
        (contract.OWNER_ROLE, "CONNECT", False),
        (contract.OWNER_ROLE, "CREATE", False),
        (contract.OWNER_ROLE, "TEMPORARY", False),
        ("hh_restore", "CONNECT", False),
    }
)


def _parse_expanded_acl(value: str) -> Optional[frozenset]:
    """Parse `role=PRIVILEGE[*],...` into a set of (grantee, privilege, grantable)."""
    entries = set()
    for item in value.split(","):
        if not item:
            continue
        grantee, separator, privilege = item.partition("=")
        if not separator or not grantee or not privilege:
            return None
        grantable = privilege.endswith("*")
        if grantable:
            privilege = privilege[:-1]
        if not privilege:
            return None
        entries.add((grantee, privilege, grantable))
    return frozenset(entries)


def _observe_temporary_state(row: str, database: str) -> Optional[_TemporaryState]:
    # Six fields are required; an empty observation is `absent` and handled by the caller.
    values = row.strip().split("|") if row.strip() else []
    if len(values) != 6 or values[0] != database:
        return None
    if not values[4].isdigit():
        return None
    acl = _parse_expanded_acl(values[5])
    if acl is None:
        return None
    return _TemporaryState(
        database=values[0],
        datallowconn=values[1] == "t",
        owner=values[2],
        acl_materialized=values[3] == "t",
        public_entries=int(values[4]),
        acl=acl,
    )


def _created_fenced_matches(state: Optional[_TemporaryState], database: str) -> bool:
    """TMP-04/F3: `created_fenced` is fenced *and* carries exactly the expected ACL.

    The fence is `datallowconn = false`; the ACL is the exact expansion produced by fresh
    database hardening: owner `hh_owner` with CONNECT/CREATE/TEMPORARY and the single
    restore login with CONNECT, none of them grantable, and nothing else — no PUBLIC
    entry, no extra grantee, no extra privilege and no grant option. A database that was
    enabled after hardening (an interrupted transition into `restoring`) is therefore
    *not* adopted: it is reported, never trusted or silently dropped.
    """
    return (
        state is not None
        and state.database == database
        and not state.datallowconn
        and state.owner == contract.OWNER_ROLE
        and state.acl_materialized
        and state.public_entries == 0
        and state.acl == _CREATED_FENCED_ACL
    )


def _expand_acl_sql(alias: str = "d") -> str:
    # The expanded, name-resolved ACL: `grantee=PRIVILEGE` with a trailing `*` when the
    # entry is grantable. `aclexplode(NULL)` yields no rows; such a database is rejected
    # separately because its ACL was never materialized by hardening.
    return (
        "coalesce((SELECT string_agg("
        "coalesce(r.rolname, 'PUBLIC') || '=' || a.privilege_type || "
        "CASE WHEN a.is_grantable THEN '*' ELSE '' END, ',' ORDER BY 1) "
        f"FROM pg_catalog.aclexplode({alias}.datacl) a "
        "LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee), '')"
    )


def _temporary_state_sql(database: str) -> str:
    return (
        "SELECT d.datname, d.datallowconn, pg_catalog.pg_get_userbyid(d.datdba), "
        "d.datacl IS NOT NULL, "
        "(SELECT count(*) FROM pg_catalog.aclexplode(d.datacl) a WHERE a.grantee = 0), "
        f"{_expand_acl_sql('d')} "
        f"FROM pg_catalog.pg_database d WHERE d.datname = '{database}';"
    )


def reconcile_temporary_database(
    journal_file: journal.Journal,
    *,
    database: str,
    maintenance_database: str,
    passfile_value: Passfile,
    tools: ToolContainers,
    network: str,
    system_identifier: str,
) -> None:
    """TMP-04/TMP-05: reconcile an existing record against the actual catalog."""
    records = _temp_records(journal_file, database)
    if not records:
        return
    latest = records[-1].payload
    action = latest.get("action")
    observed = psql(
        tools,
        passfile_value,
        database=maintenance_database,
        sql=_temporary_state_sql(database),
        network=network,
    )
    present = bool(observed.strip())
    if not present:
        if action != "retired":
            journal_file.append(
                "temp_db",
                {"action": "retired", "database": database, "reason": "absence verified"},
            )
        return
    if action in ("retired",):
        raise MaintenanceError(
            "temp_database_reappeared",
            "a retired temporary database is present again; fail closed",
        )
    state = _observe_temporary_state(observed, database)
    if action == "created_fenced" and _created_fenced_matches(state, database):
        return
    raise MaintenanceError(
        "temp_database_unresolved",
        "an existing temporary-database record does not match its observed state; "
        "an operator must reconcile it",
    )


def _append_temp(journal_file: journal.Journal, payload: Mapping[str, Any]) -> None:
    journal_file.append("temp_db", dict(payload))


def reconcile_all_temporary_databases(
    journal_file: journal.Journal,
    *,
    maintenance_database: str,
    passfile_value: Passfile,
    tools: ToolContainers,
    network: str,
    system_identifier: str,
) -> None:
    """TMP-01/TMP-04/TMP-05: reconcile every active record before a new operation.

    This is deliberately conservative. An absent database retires its authorization
    (absence is observed, not inferred). A database whose latest record is
    ``created_fenced`` and whose observed state is the fenced state is admissible; it
    is an interrupted operation that has not touched application data. Any other
    present database fails closed and is reported for an operator: this tool never
    drops a database it did not just create for the current run.
    """
    view = journal_file.read()
    active = backup.active_temporary_databases(view.records)
    for name in sorted(active):
        payload = active[name]
        if payload.get("system_identifier") != system_identifier:
            raise MaintenanceError(
                "temp_database_identity",
                "a temporary-database record names another cluster",
                name,
            )
        row = psql(
            tools,
            passfile_value,
            database=maintenance_database,
            sql=_temporary_state_sql(name),
            network=network,
        )
        if not row.strip():
            journal_file.append(
                "temp_db",
                {
                    "action": "retired",
                    "database": name,
                    "reason": "absence verified before a new operation",
                },
            )
            continue
        state = _observe_temporary_state(row, name)
        if payload.get("action") == "created_fenced" and _created_fenced_matches(state, name):
            continue
        raise MaintenanceError(
            "temp_database_unresolved",
            "an active temporary-database record is present in a state this tool will "
            "not adopt or drop; an operator must inspect it",
            name,
        )


def _drop_temporary_database(
    journal_file: journal.Journal,
    *,
    tools: ToolContainers,
    passfile_value: Passfile,
    plan: RestorePlan,
    record: Mapping[str, Any],
    network: str,
) -> None:
    """TMP-05: drop by hh_owner, then observe absence. Never `WITH (FORCE)`."""
    _append_temp(journal_file, {**record, "action": "cleanup_pending"})
    psql(
        tools,
        passfile_value,
        database=plan.maintenance_database,
        sql=(
            "SET ROLE hh_owner;\n"
            f'DROP DATABASE "{plan.database}";\n'
            "RESET ROLE;"
        ),
        network=network,
    )
    row = _database_row(
        tools,
        passfile_value,
        plan.database,
        maintenance_database=plan.maintenance_database,
        network=network,
    )
    if row:
        raise MaintenanceError(
            "cleanup", "the temporary database is still present after DROP"
        )
    _append_temp(journal_file, {**record, "action": "retired"})


# --- restore in the target cluster -----------------------------------------------------------


@dataclass
class RestorePlan:
    database: str
    maintenance_database: str
    token: str
    operation_id: str


def _new_plan(maintenance_database: str) -> RestorePlan:
    token = secrets.token_hex(8)
    return RestorePlan(
        database=f"house_helper_restore_{token}",
        maintenance_database=maintenance_database,
        token=token,
        operation_id=secrets.token_hex(16),
    )


def _validate_maintenance_database(name: object) -> str:
    return artifacts.validate_maintenance_database(name)


def maintenance_preflight(
    tools: ToolContainers,
    passfile_value: Passfile,
    *,
    maintenance_database: str,
    network: str,
) -> None:
    """RST-03/RST-08: the one maintenance database is restricted and usable."""
    sql = (
        "SELECT current_database(), session_user, current_user;"
        "SELECT d.datallowconn, pg_catalog.pg_get_userbyid(d.datdba), "
        "d.datacl IS NOT NULL, d.datacl::text "
        "FROM pg_catalog.pg_database d WHERE d.datname = current_database();"
        "SELECT a.grantee, coalesce(r.rolname, 'PUBLIC'), a.privilege_type, a.is_grantable "
        "FROM pg_catalog.pg_database d CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) a "
        "LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.grantee "
        "WHERE d.datname = current_database();"
        "SELECT rolcreatedb, rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user;"
    )
    output = psql(
        tools,
        passfile_value,
        database=maintenance_database,
        sql=sql,
        network=network,
    )
    lines = [line for line in output.splitlines() if line]
    if len(lines) < 2:
        raise MaintenanceError("maintenance_identity", "the maintenance identity is incomplete")
    identity = lines[0].split("|")
    if identity != [maintenance_database, "hh_restore", "hh_restore"]:
        raise MaintenanceError(
            "maintenance_identity",
            "hh_restore is not the session_user and current_user on the maintenance database",
        )
    row = lines[1].split("|")
    if len(row) < 4 or row[0] != "t" or row[2] != "t":
        raise MaintenanceError(
            "maintenance_acl", "the maintenance database is not connectable and hardened"
        )
    owner = row[1]
    acl = lines[2:-1]
    if not acl:
        raise MaintenanceError("maintenance_acl", "the maintenance database has no explicit ACL")
    seen: set[tuple[str, str]] = set()
    for line in acl:
        values = line.split("|")
        if len(values) != 4:
            raise MaintenanceError("maintenance_acl", "a maintenance ACL entry is malformed")
        _, grantee, privilege, grantable = values
        if grantee == "PUBLIC":
            raise MaintenanceError(
                "maintenance_acl", "PUBLIC still holds a privilege on the maintenance database"
            )
        seen.add((grantee, privilege))
        if grantee == "hh_restore" and (privilege != "CONNECT" or grantable != "f"):
            raise MaintenanceError(
                "maintenance_acl", "hh_restore holds more than CONNECT on the maintenance database"
            )
        if grantee not in ("hh_restore", owner):
            raise MaintenanceError(
                "maintenance_acl", "an unexpected role holds a maintenance privilege"
            )
    if ("hh_restore", "CONNECT") not in seen:
        raise MaintenanceError(
            "maintenance_acl", "hh_restore has no explicit CONNECT on the maintenance database"
        )
    attributes = lines[-1].split("|")
    if len(attributes) != 2 or attributes[0] != "t" or attributes[1] != "f":
        raise MaintenanceError(
            "restore_authority", "the restore login does not hold exactly CREATEDB"
        )


def _database_row(
    tools, passfile_value, database, *, maintenance_database: str, network: str
) -> list[str]:
    output = psql(
        tools,
        passfile_value,
        database=maintenance_database,
        sql=(
            "SELECT d.datname, d.datallowconn, pg_catalog.pg_get_userbyid(d.datdba), "
            "pg_catalog.pg_encoding_to_char(d.encoding), d.datcollate, d.datctype "
            f"FROM pg_catalog.pg_database d WHERE d.datname = '{database}';"
        ),
        network=network,
    ).strip()
    return output.split("|") if output else []


def _create_fenced_database(
    tools,
    passfile_value,
    plan: RestorePlan,
    *,
    collate: str,
    ctype: str,
    network: str,
) -> None:
    if not _LOCALE.fullmatch(collate) or not _LOCALE.fullmatch(ctype):
        raise MaintenanceError("locale", "the manifest locale is not an allowed value")
    identity = psql(
        tools,
        passfile_value,
        database=plan.maintenance_database,
        sql=(
            "SELECT current_database(), session_user, current_user;\n"
            f'CREATE DATABASE "{plan.database}" OWNER hh_owner TEMPLATE template0 '
            f"ENCODING 'UTF8' LC_COLLATE '{collate}' LC_CTYPE '{ctype}' "
            "ALLOW_CONNECTIONS false;"
        ),
        network=network,
    ).splitlines()
    if identity[:1] != [f"{plan.maintenance_database}|hh_restore|hh_restore"]:
        raise MaintenanceError(
            "restore_identity",
            "CREATE DATABASE did not run as session_user and current_user hh_restore",
        )
    row = _database_row(
        tools,
        passfile_value,
        plan.database,
        maintenance_database=plan.maintenance_database,
        network=network,
    )
    if (
        len(row) != 6
        or row[0] != plan.database
        or row[1] != "f"
        or row[2] != contract.OWNER_ROLE
        or row[3] != contract.EXPECTED_ENCODING
        or row[4] != collate
        or row[5] != ctype
    ):
        raise MaintenanceError(
            "created_database", "the created database is not the fenced target"
        )
    probe = connect_probe(
        tools, passfile_value, database=plan.database, network=network
    )
    if probe.returncode == 0:
        raise MaintenanceError(
            "prehardening_access",
            "a client connected while ALLOW_CONNECTIONS was false",
        )


def _verify_restore_database_acl(
    tools, passfile_value, plan: RestorePlan, *, database: str, network: str
) -> None:
    """F3/TMP-04: the expanded database ACL equals the fresh-hardening contract exactly.

    Every entry is compared: grantee name, privilege and grantability. The owner is not
    skipped — an owner grant option or an owner TEMPORARY/CREATE set that differs is a
    mismatch too. No PUBLIC entry, extra grantee, extra privilege or grant option is
    accepted.
    """
    acl = psql(
        tools,
        passfile_value,
        database=database,
        sql=(
            f"SELECT {_expand_acl_sql('d')} FROM pg_catalog.pg_database d "
            f"WHERE d.datname = '{plan.database}';"
        ),
        network=network,
    ).strip()
    observed = _parse_expanded_acl(acl)
    if observed is None:
        raise MaintenanceError("database_acl", "the restore-database ACL is malformed")
    if observed != _CREATED_FENCED_ACL:
        raise MaintenanceError(
            "database_acl",
            "the restore-database ACL is not exactly the hardened contract",
            ",".join(f"{grantee}={privilege}{'*' if grantable else ''}"
                     for grantee, privilege, grantable in sorted(observed)),
        )


def _harden_fenced(tools, passfile_value, plan: RestorePlan, *, network: str) -> None:
    """RST-04 step 5 / RST-05: revoke and grant while connections are still disabled.

    Enablement is a separate step (`_enable`) so that the journal's `created_fenced`
    record is written only after the database really is fenced *and* hardened.
    """
    identity = psql(
        tools,
        passfile_value,
        database=plan.maintenance_database,
        sql=(
            "SET ROLE hh_owner;\n"
            "SELECT current_database(), session_user, current_user;\n"
            f'REVOKE ALL ON DATABASE "{plan.database}" FROM PUBLIC;\n'
            f'GRANT CONNECT ON DATABASE "{plan.database}" TO hh_restore;\n'
            "RESET ROLE;"
        ),
        network=network,
    ).splitlines()
    if identity[:1] != [f"{plan.maintenance_database}|hh_restore|hh_owner"]:
        raise MaintenanceError(
            "restore_identity",
            "database ACL hardening did not run as session_user hh_restore and "
            "current_user hh_owner",
        )
    row = _database_row(
        tools,
        passfile_value,
        plan.database,
        maintenance_database=plan.maintenance_database,
        network=network,
    )
    if len(row) != 6 or row[1] != "f" or row[2] != contract.OWNER_ROLE:
        raise MaintenanceError(
            "database_acl", "the restore database did not stay fenced while its ACL was changed"
        )
    _verify_restore_database_acl(
        tools, passfile_value, plan, database=plan.maintenance_database, network=network
    )
    probe = connect_probe(
        tools, passfile_value, database=plan.database, network=network
    )
    if probe.returncode == 0:
        raise MaintenanceError(
            "prehardening_access",
            "a client connected while ALLOW_CONNECTIONS was false",
        )


def _enable(tools, passfile_value, plan: RestorePlan, *, network: str) -> None:
    """RST-04 step 5 (last): enable connections, after the ACL is already exact."""
    identity = psql(
        tools,
        passfile_value,
        database=plan.maintenance_database,
        sql=(
            "SET ROLE hh_owner;\n"
            "SELECT current_database(), session_user, current_user;\n"
            f'ALTER DATABASE "{plan.database}" ALLOW_CONNECTIONS true;\n'
            "RESET ROLE;"
        ),
        network=network,
    ).splitlines()
    if identity[:1] != [f"{plan.maintenance_database}|hh_restore|hh_owner"]:
        raise MaintenanceError(
            "restore_identity",
            "database enablement did not run as session_user hh_restore and "
            "current_user hh_owner",
        )
    row = _database_row(
        tools,
        passfile_value,
        plan.database,
        maintenance_database=plan.maintenance_database,
        network=network,
    )
    if len(row) != 6 or row[1] != "t":
        raise MaintenanceError("database_acl", "the restore database did not enable cleanly")
    _verify_restore_database_acl(
        tools, passfile_value, plan, database=plan.maintenance_database, network=network
    )
    probe = connect_probe(
        tools, passfile_value, database=plan.database, network=network
    )
    if probe.returncode != 0:
        raise MaintenanceError(
            "database_acl",
            "the one intended restore login cannot connect after enablement",
            _first_line(probe.stderr),
        )


def _establish_fenced_database(
    journal_file: journal.Journal,
    tools,
    passfile_value,
    plan: RestorePlan,
    record: Mapping[str, Any],
    *,
    collate: str,
    ctype: str,
    network: str,
) -> None:
    """TMP-04/RST-05: journal each state only after that state is actually established.

    `created_fenced` is written after the database exists *and* its ACL is hardened
    while it is still disabled. Only then is it enabled and the record advanced to
    `restoring`. An interruption in either window is observable and fails closed in
    reconciliation instead of silently adopting a partially prepared database.
    """
    _create_fenced_database(
        tools, passfile_value, plan, collate=collate, ctype=ctype, network=network
    )
    _harden_fenced(tools, passfile_value, plan, network=network)
    _append_temp(journal_file, {**record, "action": "created_fenced"})
    _enable(tools, passfile_value, plan, network=network)
    _append_temp(journal_file, {**record, "action": "restoring"})


def _filtered_restore_list(toc_text: str) -> str:
    """Drop the archive's `CREATE SCHEMA public` entry only.

    A database created from `template0` already carries `public`; the frozen
    `pg_restore --exit-on-error` command would otherwise stop at `CREATE SCHEMA
    public`. The schema is hardened by `db/schema.sql` after the restore, so the
    archive's schema entry is unnecessary. Every other TOC entry is preserved.
    """
    schema_entries: list[str] = []
    kept: list[str] = []
    for line in toc_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        entry = backup.parse_toc(line + "\n")
        if len(entry) != 1:
            raise MaintenanceError("toc", "a TOC line did not parse as one entry")
        if entry[0].object_class == "SCHEMA" and entry[0].schema == "-" and entry[0].name == "public":
            schema_entries.append(line)
            continue
        kept.append(line)
    if len(schema_entries) != 1:
        raise MaintenanceError(
            "toc", "the archive must carry exactly one public-schema entry"
        )
    return "\n".join(kept) + "\n"


def _apply_artifacts(tools, passfile_value, database: str, *, network: str) -> None:
    for filename, renderer in (
        ("schema.sql", artifacts.render_schema),
        ("privileges.sql", artifacts.render_privileges),
    ):
        sql = renderer(database)
        psql(
            tools,
            passfile_value,
            database=database,
            sql="BEGIN;\nSET ROLE hh_owner;\n" + sql + "\nCOMMIT;\n",
            network=network,
        )


# --- verification ----------------------------------------------------------------------------


@dataclass
class Verification:
    failures: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _verification_sql(manifest: Mapping[str, Any]) -> str:
    lines = [
        "SET datestyle = 'ISO, YMD';",
        "SET timezone = 'UTC';",
        "SET bytea_output = 'hex';",
        "SET extra_float_digits = 3;",
        "SET ROLE hh_owner;",
        "SELECT 'identity', session_user, current_user;",
        "SELECT 'revision', version_num FROM public.alembic_version;",
        "SELECT 'db_sequence', pg_catalog.pg_get_serial_sequence('public.messages','id'), "
        "(SELECT last_value FROM public.messages_id_seq), "
        "(SELECT is_called FROM public.messages_id_seq), "
        "q.seqmin, q.seqmax, q.seqincrement, q.seqcycle, "
        "pg_catalog.pg_get_userbyid(c.relowner), "
        "(SELECT count(*) FROM pg_catalog.pg_depend d "
        " WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid "
        " AND d.refclassid = 'pg_catalog.pg_class'::regclass AND d.deptype = 'a') "
        "FROM pg_catalog.pg_sequence q JOIN pg_catalog.pg_class c ON c.oid = q.seqrelid "
        "WHERE q.seqrelid = 'public.messages_id_seq'::regclass;",
        "SELECT 'max_messages_id', coalesce(max(id)::text, '') FROM public.messages;",
        "SELECT 'owners', string_agg(DISTINCT pg_catalog.pg_get_userbyid(c.relowner), ',') "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r','S','i','p');",
        "SELECT 'schema', n.nspowner::regrole::text, "
        "(SELECT count(*) FROM pg_catalog.aclexplode(n.nspacl) a WHERE a.grantee = 0) "
        "FROM pg_catalog.pg_namespace n WHERE n.nspname = 'public';",
        "SELECT 'not_valid', count(*)::text FROM pg_catalog.pg_constraint "
        "WHERE conrelid IN (SELECT c.oid FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public') "
        "AND NOT convalidated;",
    ]
    for name in sorted(manifest["tables"]):
        lines.append(
            "SELECT 'table', '{name}', count(*)::text, "
            "encode(sha256(coalesce(string_agg(h, '' ORDER BY h), '')::bytea), 'hex') "
            "FROM (SELECT md5(row_to_json(t)::text) AS h FROM public.\"{name}\" AS t) AS rows;".format(
                name=name
            )
        )
    lines.append(contract.DEFAULT_ACLS_SQL + ";")
    lines.append(backup.object_contract_sql().rstrip())
    return "\n".join(lines) + "\n"


def _parse_verification(
    output: str, manifest: Mapping[str, Any], *, fixture: Mapping[str, Any]
) -> Verification:
    result = Verification()
    tables: dict[str, dict[str, Any]] = {}
    default_acls: list[list[Any]] = []
    contract_rows: list[list[str]] = []
    in_contract = False
    for raw in output.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("|")
        kind = parts[0]
        if kind in ("relation", "column", "constraint", "index", "sequence"):
            in_contract = True
            contract_rows.append(parts)
            continue
        if in_contract:
            result.failures.append("verification:trailing_rows")
            continue
        if kind == "identity" and len(parts) == 3:
            if parts[1:] != ["hh_restore", "hh_owner"]:
                result.failures.append("identity:restore_phase")
        elif kind == "revision" and len(parts) == 2:
            result.evidence["revision"] = parts[1]
            if parts[1] != manifest["alembic_revision"]:
                result.failures.append("revision:mismatch")
        elif kind == "db_sequence" and len(parts) == 10:
            result.evidence["sequence"] = parts[1:]
            restored = (int(parts[2]), parts[3] == "t")
            result.failures.extend(backup.compare_sequence(manifest["sequence"], restored))
            seqmin, seqmax, increment, cycle = (
                int(parts[4]),
                int(parts[5]),
                int(parts[6]),
                parts[7] == "t",
            )
            if (
                parts[1] != f"public.{backup.SEQUENCE_NAME}"
                or increment != 1
                or cycle is not False
                or parts[8] != contract.OWNER_ROLE
                or parts[9] != "1"
            ):
                result.failures.append("sequence:catalog")
            expected_next = restored[0] + (1 if restored[1] else 0)
            if expected_next > seqmax or restored[0] < seqmin:
                result.failures.append("sequence:exhausted")
        elif kind == "max_messages_id" and len(parts) == 2:
            maximum = None if parts[1] == "" else int(parts[1])
            result.evidence["max_messages_id"] = maximum
            if maximum != manifest["sequence"].get("max_messages_id"):
                result.failures.append("sequence:max_messages_id")
            if maximum is not None:
                expected_next = manifest["sequence"]["next_allocatable"]
                if expected_next <= maximum:
                    result.failures.append("sequence:behind_rows")
        elif kind == "owners" and len(parts) == 2:
            if parts[1] != contract.OWNER_ROLE:
                result.failures.append("ownership:not_hh_owner")
        elif kind == "schema" and len(parts) == 3:
            if parts[1] != contract.OWNER_ROLE or parts[2] != "0":
                result.failures.append("schema:contract")
        elif kind == "not_valid" and len(parts) == 2:
            if parts[1] != "0":
                result.failures.append("constraints:not_valid")
        elif kind == "table" and len(parts) == 4:
            name = parts[1]
            if name in tables or name not in manifest["tables"]:
                result.failures.append("table:unexpected")
                continue
            tables[name] = backup.table_digest_value(parts[2], parts[3])
            if tables[name] != dict(manifest["tables"][name]):
                result.failures.append(f"table:mismatch:{name}")
        elif len(parts) == 6:
            default_acls.append(
                [
                    parts[0],
                    parts[1],
                    parts[2],
                    parts[3],
                    parts[4],
                    parts[5] == "t",
                ]
            )
        else:
            result.failures.append("verification:unrecognized")
    if sorted(tables) != sorted(manifest["tables"]):
        result.failures.append("table:coverage")
    observed_acls = contract.default_acl_entries(default_acls)
    result.failures.extend(contract.default_acl_failures(observed_acls))
    if not contract_rows:
        result.failures.append("object_contract:missing")
    else:
        try:
            observed_contract = backup.object_contract_from_rows(
                contract_rows, revision=str(manifest["alembic_revision"])
            )
        except backup.BackupError:
            result.failures.append("object_contract:malformed")
        else:
            result.failures.extend(backup.object_contract_failures(observed_contract, fixture))
    return result


def verify_database(
    tools: ToolContainers,
    passfile_value: Passfile,
    *,
    database: str,
    manifest: Mapping[str, Any],
    fixture: Mapping[str, Any],
    network: str,
) -> Verification:
    output = psql(
        tools,
        passfile_value,
        database=database,
        sql=_verification_sql(manifest),
        network=network,
        timeout=900,
    )
    return _parse_verification(output, manifest, fixture=fixture)


# --- alembic check ---------------------------------------------------------------------------


def alembic_check(
    tools: ToolContainers,
    *,
    app_image: str,
    database: str,
    role: str,
    passfile_value: Optional[Passfile],
    network: str,
    host: str = DB_HOST,
    isolated_container: Optional[str] = None,
) -> None:
    if passfile_value is not None:
        program = _wrapped(passfile_value, ["python", "-m", "alembic", "check"])
    else:
        program = ["sh", "-c", 'exec "$@"', "hh-alembic", "python", "-m", "alembic", "check"]
    environment = [
        f"HOUSE_HELPER_DATABASE_URL=postgresql+psycopg://{role}@{host}:{DB_PORT}/{database}",
        f"PGHOST={host}",
        f"PGPORT={DB_PORT}",
        f"PGUSER={role}",
        f"PGDATABASE={database}",
        f"PGAPPNAME={role}",
        "PGOPTIONS=-c role=hh_owner",
    ]
    mounts = []
    if passfile_value is not None:
        mounts.append((passfile_value.host_path, passfile_value.container_path, True))
    target_network = (
        f"container:{isolated_container}" if isolated_container is not None else network
    )
    done = tools.run(
        program,
        mounts=mounts,
        environment=environment,
        network=target_network,
        timeout=600,
        image=app_image,
    )
    if done.returncode != 0:
        raise MaintenanceError(
            "alembic_check",
            "alembic check reported drift",
            _first_line(done.stderr),
        )


# --- isolated rehearsal ----------------------------------------------------------------------


# F6/§8.6 level 2: the attachment proof runs in the application image, so the sample is read
# through `house_helper.persistence.attachments.get_for_user` - the application's own
# loader - never through a direct `SELECT data` used as a substitute. The stored `sha256`
# and `size_bytes` it compares against are part of `message_attachments`, whose logical
# digest the rehearsal already compared with the archive manifest, so the sampled bytes are
# tied to the archive evidence. The byte total is checked twice: the exact sum of
# `octet_length(data)` and the sum of the declared `size_bytes` (the schema's
# `data_matches_size` CHECK makes them equal only if the payload is intact).
# R4-F3/R5: the proof's outcome is a MACHINE-READABLE record framed by a per-run token the host
# generates, never an exit status alone and never arbitrary stdout. The program has two verdict
# statuses of its own: 0 (completed, framed `{"verdict": "ok", ...}`) and 3 (deliberate refusal,
# framed `{"verdict": "refused", "reason": <allowlisted token>}`). Status 1 is left to the ordinary
# uncaught-exception path (SQLAlchemy, connection, import, environment), and Docker's own failures
# are decided first; neither can be read as a refusal.
_ATTACHMENT_PROOF_PREFIX = "HH-ATTACHMENT-PROOF"
_ATTACHMENT_PROOF_NONCE_ENV = "HH_ATTACHMENT_PROOF_NONCE"
_ATTACHMENT_PROOF_OK_STATUS = 0
_ATTACHMENT_PROOF_REFUSED_STATUS = 3
# Status 1 (the ordinary uncaught-exception status) and status 4 (the program could not establish
# its result channel) are accepted here only so those failures are classified by `attachment_proof`
# as environment failures, not by the Docker boundary; neither is ever a verdict.
_ATTACHMENT_PROOF_VERDICT_STATUSES = frozenset(
    {_ATTACHMENT_PROOF_OK_STATUS, 1, _ATTACHMENT_PROOF_REFUSED_STATUS, 4}
)
_ATTACHMENT_REFUSAL_REASONS = frozenset(
    {"byte_total_mismatch", "loader_empty", "loader_mismatch"}
)
_ATTACHMENT_PROOF = r"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import sqlalchemy as sa

from house_helper.persistence import attachments
from house_helper.persistence.database import create_db_engine, create_session_factory
from house_helper.persistence.models import MessageAttachment

nonce = os.environ.get("HH_ATTACHMENT_PROOF_NONCE", "")
if len(nonce) != 32 or any(ch not in "0123456789abcdef" for ch in nonce):
    print("the attachment proof has no result-channel token", file=sys.stderr, flush=True)
    raise SystemExit(4)


def emit(record):
    # ONE framed record. The per-run token is generated by the host and is the only thing the
    # host accepts as a verdict, so arbitrary dependency output cannot reach this frame.
    print(
        "HH-ATTACHMENT-PROOF " + nonce + " " + json.dumps(record, sort_keys=True),
        flush=True,
    )


def refuse(reason):
    # An intentional verification rejection has its OWN status (3), never the ordinary
    # uncaught-exception status (1), so an execution failure cannot be read as a refusal.
    emit({"verdict": "refused", "reason": reason})
    print("the attachment proof refused: " + reason, file=sys.stderr, flush=True)
    raise SystemExit(3)


engine = create_db_engine()
Session = create_session_factory(engine)
result = {"verdict": "ok", "count": 0, "bytes": 0, "declared_bytes": 0, "sample": False}
try:
    with Session() as session:
        result["count"] = int(
            session.scalar(
                sa.select(sa.func.count()).select_from(MessageAttachment)
            )
            or 0
        )
        result["bytes"] = int(
            session.scalar(
                sa.select(
                    sa.func.coalesce(
                        sa.func.sum(sa.func.octet_length(MessageAttachment.data)), 0
                    )
                )
            )
            or 0
        )
        result["declared_bytes"] = int(
            session.scalar(
                sa.select(
                    sa.func.coalesce(sa.func.sum(MessageAttachment.size_bytes), 0)
                )
            )
            or 0
        )
        if result["bytes"] != result["declared_bytes"]:
            refuse("byte_total_mismatch")
        if result["count"]:
            row = session.execute(
                sa.select(
                    MessageAttachment.user_id,
                    MessageAttachment.id,
                    MessageAttachment.sha256,
                    MessageAttachment.size_bytes,
                )
                .order_by(MessageAttachment.id)
                .limit(1)
            ).one()
            loaded = attachments.get_for_user(
                session,
                row.user_id,
                row.id,
                now=datetime(1970, 1, 1, tzinfo=timezone.utc),
            )
            if loaded is None:
                refuse("loader_empty")
            if (
                loaded.id != row.id
                or len(loaded.data) != int(row.size_bytes)
                or hashlib.sha256(loaded.data).hexdigest() != row.sha256
            ):
                refuse("loader_mismatch")
            result["sample"] = True
            result["sample_id"] = str(row.id)
            result["sample_bytes"] = len(loaded.data)
finally:
    engine.dispose()
emit(result)
"""


def _reject_duplicate_json_keys(pairs):
    """A JSON object may not repeat a key: the verdict must be unambiguous."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _attachment_proof_record(
    done: subprocess.CompletedProcess, nonce: str
) -> Mapping[str, Any]:
    """Return the proof program's ONE deliberate result record, or fail closed.

    Only a line that starts with the fixed frame and carries this run's token is evidence. Every
    other stdout byte - including a dependency's JSON-looking line - is diagnostics and is never
    parsed as a verdict. Exactly one framed line may exist, it must be the final non-empty line,
    its JSON must be one complete duplicate-free object, and nothing may follow it. Anything else
    means the proof channel itself is unreliable: `attachment_proof_environment`, never artifact
    evidence.
    """
    try:
        text = (done.stdout or b"").decode("utf-8")
    except UnicodeDecodeError:
        raise MaintenanceError(
            "attachment_proof_environment", "the attachment proof output is not UTF-8"
        ) from None
    lines = [line for line in text.splitlines() if line.strip()]
    framed = [
        index
        for index, line in enumerate(lines)
        if line.startswith(_ATTACHMENT_PROOF_PREFIX)
    ]
    if len(framed) != 1:
        raise MaintenanceError(
            "attachment_proof_environment",
            "the attachment proof did not emit exactly one result record",
        )
    index = framed[0]
    if index != len(lines) - 1:
        raise MaintenanceError(
            "attachment_proof_environment",
            "the attachment proof result record is not the final record",
        )
    head = _ATTACHMENT_PROOF_PREFIX + " "
    line = lines[index]
    token, separator, payload = line[len(head) :].partition(" ")
    if not line.startswith(head) or not separator or token != nonce:
        raise MaintenanceError(
            "attachment_proof_environment",
            "the attachment proof result record is not this run's",
        )
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_json_keys)
    except ValueError:
        raise MaintenanceError(
            "attachment_proof_environment",
            "the attachment proof result record is not one complete JSON object",
        ) from None
    if not isinstance(value, dict):
        raise MaintenanceError(
            "attachment_proof_environment",
            "the attachment proof result record is not one complete JSON object",
        )
    return value


def attachment_proof(
    tools: ToolContainers,
    *,
    app_image: str,
    database: str,
    role: str,
    passfile_value: Optional[Passfile],
    network: str,
    host: str = DB_HOST,
    isolated_container: Optional[str] = None,
) -> Mapping[str, Any]:
    """§8.6 level 2: exact attachment byte total and one application-loader sample."""
    if passfile_value is not None:
        program = _wrapped(passfile_value, ["python", "-c", _ATTACHMENT_PROOF])
    else:
        program = [
            "sh",
            "-c",
            'exec "$@"',
            "hh-attachments",
            "python",
            "-c",
            _ATTACHMENT_PROOF,
        ]
    nonce = secrets.token_hex(16)
    environment = [
        f"HOUSE_HELPER_DATABASE_URL=postgresql+psycopg://{role}@{host}:{DB_PORT}/{database}",
        f"PGHOST={host}",
        f"PGPORT={DB_PORT}",
        f"PGUSER={role}",
        f"PGDATABASE={database}",
        f"PGAPPNAME={role}",
        "PGOPTIONS=-c role=hh_owner",
        f"{_ATTACHMENT_PROOF_NONCE_ENV}={nonce}",
    ]
    mounts = []
    if passfile_value is not None:
        mounts.append((passfile_value.host_path, passfile_value.container_path, True))
    target_network = (
        f"container:{isolated_container}" if isolated_container is not None else network
    )
    done = tools.run(
        program,
        mounts=mounts,
        environment=environment,
        network=target_network,
        timeout=600,
        image=app_image,
    )
    # The attachment proof code is an artifact refusal, so a Docker failure must not reach it.
    # Status 1 is admitted here only so the crashed-program case below is classified as an
    # attachment-proof environment failure instead of Docker infrastructure.
    _refuse_docker_execution(
        done, "the attachment proof", verdict_statuses=_ATTACHMENT_PROOF_VERDICT_STATUSES
    )
    if done.returncode not in (
        _ATTACHMENT_PROOF_OK_STATUS,
        _ATTACHMENT_PROOF_REFUSED_STATUS,
    ):
        # The program ran but crashed (an uncaught Python/SQLAlchemy/connection/import exception
        # is the ordinary status 1). No record was promised, so this is never artifact evidence.
        raise MaintenanceError(
            "attachment_proof_environment",
            "the attachment proof did not reach a verdict",
            _first_line(done.stderr) or None,
        )
    value = _attachment_proof_record(done, nonce)
    verdict = value.get("verdict")
    if done.returncode == _ATTACHMENT_PROOF_REFUSED_STATUS:
        reason = value.get("reason")
        if (
            verdict != "refused"
            or set(value) != {"verdict", "reason"}
            or not isinstance(reason, str)
            or reason not in _ATTACHMENT_REFUSAL_REASONS
            or STATUS_CODE_RE.fullmatch(reason) is None
        ):
            # A refusal status must carry exactly one allowlisted reason. Missing, extra or
            # unknown fields, and a success/refusal disagreement with the status, fail closed.
            raise MaintenanceError(
                "attachment_proof_environment",
                "the attachment proof refused without a valid allowlisted reason",
            )
        raise MaintenanceError(
            "attachment_proof",
            "the attachment proof refused the restored attachments",
            reason,
        )
    if verdict != "ok":
        raise MaintenanceError(
            "attachment_proof_environment",
            "the attachment proof reported success but did not record an ok verdict",
        )
    count = value.get("count")
    total = value.get("bytes")
    declared = value.get("declared_bytes")
    sample = value.get("sample")
    expected = {"verdict", "count", "bytes", "declared_bytes", "sample"}
    if isinstance(sample, bool) and sample:
        expected |= {"sample_id", "sample_bytes"}
    if (
        set(value) != expected
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
        or not isinstance(declared, int)
        or isinstance(declared, bool)
        or declared != total
        or not isinstance(sample, bool)
        or sample != (count > 0)
        or (count == 0 and total != 0)
    ):
        # The record claims success but its proof fields are not consistent. It is not evidence
        # about the archive, so it must not become NOT RESTORABLE.
        raise MaintenanceError(
            "attachment_proof_environment",
            "the attachment proof success record is not consistent",
        )
    if sample:
        sample_bytes = value.get("sample_bytes")
        if (
            not isinstance(value.get("sample_id"), str)
            or not value.get("sample_id")
            or not isinstance(sample_bytes, int)
            or isinstance(sample_bytes, bool)
            or not 0 < sample_bytes <= total
        ):
            raise MaintenanceError(
                "attachment_proof_environment",
                "the attachment sample evidence is malformed",
            )
    return value


def _attachment_summary(proof: Mapping[str, Any]) -> str:
    if proof["sample"]:
        return (
            f"{proof['count']} attachments, {proof['bytes']} bytes; "
            f"loader sample {proof['sample_id']} ({proof['sample_bytes']} bytes)"
        )
    return "0 attachments, 0 bytes; loader sample none (the archive has no attachments)"


def rehearse(
    tools: ToolContainers,
    *,
    point: backup.RecoveryPoint,
    work: object,
    fixture: Mapping[str, Any],
    app_image: str,
    run_id: str,
    keep: bool = False,
) -> Mapping[str, Any]:
    """§8.6 levels 2/3 in a throwaway `--network none` container.

    The container's name is the run's ownership identity (`run_id`). Removal is NOT done here: the
    caller removes the container by that name in its `finally`, so a cancellation follows exactly
    the same path as a completion (F2)."""
    work_path = Path(work)
    container_name = _rehearsal_container_name(run_id)
    container = tools.start_detached(
        ["postgres"],
        name=container_name,
        environment=(
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "POSTGRES_USER=postgres",
            "POSTGRES_DB=postgres",
        ),
        network="none",
        tmpfs=("/var/lib/postgresql/data:rw,noexec,nosuid,nodev,size=1g",),
    )
    database = "house_helper_restore_" + secrets.token_hex(6)
    cleanup_database = database
    smoke_database = database + "_smoke"
    try:
        _await_postgres(tools, container, timeout=90)
        identifier_done = tools.exec(
            container,
            ["psql", "-X", "-q", "-A", "-t", "-d", "postgres", "-f", "-"],
            stdin=b"SELECT system_identifier::text FROM pg_control_system();",
        )
        identifier = identifier_done.stdout.decode("utf-8", "replace").strip()
        if identifier_done.returncode != 0:
            raise MaintenanceError(
                "isolated_identity",
                "the isolated cluster identifier query failed",
                _first_line(identifier_done.stderr),
            )
        if not re.fullmatch(r"[0-9]{1,20}", identifier):
            raise MaintenanceError(
                "isolated_identity", "the isolated cluster identifier could not be read"
            )
        tools.copy_in(container, work_path / backup.DUMP_NAME, f"/tmp/{backup.DUMP_NAME}")
        restore_list = _filtered_restore_list(
            tools.pg_restore_list(work_path)
        )
        tools.exec(
            container,
            ["sh", "-c", "cat > /tmp/restore.list"],
            stdin=restore_list.encode("utf-8"),
        )
        _local_sql(
            tools,
            container,
            database="postgres",
            sql="BEGIN;\n" + artifacts.render_roles() + "\nCOMMIT;\n",
            user="postgres",
        )
        _local_sql(
            tools,
            container,
            database="postgres",
            sql=(
                f'CREATE DATABASE "{database}" OWNER hh_owner TEMPLATE template0 '
                f"ENCODING 'UTF8' LC_COLLATE '{point.manifest['datcollate']}' "
                f"LC_CTYPE '{point.manifest['datctype']}' ALLOW_CONNECTIONS false;"
            ),
            user="hh_restore",
        )
        _local_sql(
            tools,
            container,
            database="postgres",
            sql=(
                "SET ROLE hh_owner;\n"
                f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC;\n'
                f'GRANT CONNECT ON DATABASE "{database}" TO hh_restore;\n'
                f'ALTER DATABASE "{database}" ALLOW_CONNECTIONS true;\n'
                "RESET ROLE;"
            ),
            user="hh_restore",
        )
        done = tools.exec(
            container,
            [
                "pg_restore",
                "--exit-on-error",
                "--role=hh_owner",
                "--no-owner",
                "--no-privileges",
                "--use-list=/tmp/restore.list",
                "--dbname",
                database,
                f"/tmp/{backup.DUMP_NAME}",
            ],
            user="postgres",
        )
        _refuse_docker_execution(done, "the isolated pg_restore")
        if done.returncode != 0:
            raise MaintenanceError(
                "isolated_restore",
                "the isolated pg_restore did not complete",
                _first_line(done.stderr),
            )
        for filename, renderer in (
            ("schema.sql", artifacts.render_schema),
            ("privileges.sql", artifacts.render_privileges),
        ):
            _local_sql(
                tools,
                container,
                database=database,
                sql="BEGIN;\nSET ROLE hh_owner;\n" + renderer(database) + "\nCOMMIT;\n",
                user="hh_restore",
            )
        verification_sql = _verification_sql(point.manifest)
        output = _local_sql(
            tools,
            container,
            database=database,
            sql=verification_sql,
            user="hh_restore",
            timeout=900,
        )
        verification = _parse_verification(output, point.manifest, fixture=fixture)
        if verification.failures:
            raise MaintenanceError(
                "isolated_verification",
                "the isolated restored database did not match the archive",
                ", ".join(dict.fromkeys(verification.failures)),
            )

        # A discarded clone carries the real `hh_app` smoke (level 2). The clone is
        # created by the isolated bootstrap superuser because `CREATE DATABASE ...
        # TEMPLATE <owner>` requires ownership of the template; hh_app is then the only
        # login that touches it.
        _local_sql(
            tools,
            container,
            database="postgres",
            sql=(
                f'CREATE DATABASE "{smoke_database}" OWNER hh_owner TEMPLATE "{database}";\n'
                f'REVOKE ALL ON DATABASE "{smoke_database}" FROM PUBLIC;\n'
                f'GRANT CONNECT ON DATABASE "{smoke_database}" TO hh_app;\n'
                f'ALTER DATABASE "{smoke_database}" ALLOW_CONNECTIONS true;\n'
            ),
            user="postgres",
        )
        smoke = _local_sql(
            tools,
            container,
            database=smoke_database,
            sql=(
                "BEGIN;\n"
                f"INSERT INTO users (id, display_name, created_at, updated_at) "
                f"VALUES (gen_random_uuid(), '{_SMOKE_MARKER}', now(), now());\n"
                "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
                f"SELECT gen_random_uuid(), id, '{_SMOKE_MARKER}', now(), now() FROM users "
                f"WHERE display_name = '{_SMOKE_MARKER}';\n"
                "INSERT INTO messages (conversation_id, role, content, created_at, expires_at) "
                f"SELECT id, 'user', 'synthetic', now(), now() + interval '1 day' FROM conversations "
                f"WHERE title = '{_SMOKE_MARKER}';\n"
                "DELETE FROM messages WHERE content = 'synthetic';\n"
                "SELECT version_num FROM alembic_version;\n"
                "ROLLBACK;\n"
            ),
            user="hh_app",
        )
        if smoke.strip() != point.manifest["alembic_revision"]:
            raise MaintenanceError(
                "isolated_smoke", "the hh_app smoke did not read the expected revision"
            )
        alembic_check(
            tools,
            app_image=app_image,
            database=database,
            role="hh_restore",
            passfile_value=None,
            network="none",
            host="127.0.0.1",
            isolated_container=container,
        )
        proof = attachment_proof(
            tools,
            app_image=app_image,
            database=database,
            role="hh_restore",
            passfile_value=None,
            network="none",
            host="127.0.0.1",
            isolated_container=container,
        )
        return {
            "system_identifier": identifier,
            "database": database,
            "revision": verification.evidence.get("revision"),
            "tables": len(point.manifest["tables"]),
            "attachments": proof,
        }
    finally:
        if keep:
            print(f"warning [rehearsal]: retained disposable container {container_name}")
        # Not removed here: the caller's finally does it, verified, on every path including a signal.


def _await_postgres(tools: ToolContainers, container: str, *, timeout: int) -> None:
    """Wait for a real query against the *final* server, not the entrypoint's init server.

    The official image starts a temporary server while it initializes PGDATA. That
    server listens only on the Unix socket (`listen_addresses=''`); `pg_isready` and a
    socket `SELECT 1` can both succeed against it and then fail when the real server
    replaces it. A TCP connection to 127.0.0.1 is therefore the readiness fact this tool
    uses: the temporary server never opens a TCP listener.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done = tools.exec(
            container,
            [
                "psql",
                "-X",
                "-q",
                "-A",
                "-t",
                "-h",
                "127.0.0.1",
                "-d",
                "postgres",
                "-c",
                "SELECT 1;",
            ],
            timeout=10,
        )
        if done.returncode == 0 and done.stdout.strip() == b"1":
            return
        time.sleep(1)
    raise MaintenanceError("isolated_start", "the isolated PostgreSQL did not become ready")


def _local_sql(
    tools: ToolContainers,
    container: str,
    *,
    database: str,
    sql: str,
    user: str,
    timeout: int = 300,
) -> str:
    done = tools.exec(
        container,
        [
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-F",
            "|",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            user,
            "-d",
            database,
            "-f",
            "-",
        ],
        stdin=sql.encode("utf-8"),
        user="postgres",
        timeout=timeout,
    )
    if done.returncode != 0:
        raise MaintenanceError(
            "isolated_sql",
            "the isolated database refused a statement",
            _first_line(done.stderr),
        )
    return done.stdout.decode("utf-8", "replace")


# --- restore command -------------------------------------------------------------------------


def restore_command(args) -> int:
    """P0: the whole restore, from the target check to the final drop, is one interval."""
    with maintenance_interval(args.compose_file, args.journal_dir) as target:
        return _restore_command_locked(args, target)


def _restore_command_locked(args, target: Target) -> int:
    tools_template: Optional[ToolContainers] = None
    work = WorkDirectory(args.work_dir, prefix="house-helper-restore-")
    try:
        tools = ToolContainers(target.spec.image)
        tools.image_digest()
        point, entries = validate_archive(args.archive, tools, work=work.path)
        fixture = backup.load_object_contract_fixture(args.object_contract)
        if fixture.get("revision") != point.manifest["alembic_revision"]:
            raise MaintenanceError(
                "object_contract_revision",
                "the archive revision does not match the checked-in object contract",
            )
        contract_failures = backup.toc_contract_failures(entries, fixture)
        if contract_failures:
            raise MaintenanceError(
                "archive_contract",
                "the archive TOC does not name the checked-in object contract",
                ", ".join(contract_failures),
            )
        maintenance = _validate_maintenance_database(args.maintenance_database)
        role_passfile = passfile(args.credentials_dir, "hh_restore", database="*")
        maintenance_preflight(
            tools,
            role_passfile,
            maintenance_database=maintenance,
            network=target.spec.network,
        )
        reconcile_all_temporary_databases(
            target.journal_file,
            maintenance_database=maintenance,
            passfile_value=role_passfile,
            tools=tools,
            network=target.spec.network,
            system_identifier=target.system_identifier,
        )
        plan = _new_plan(maintenance)
        if not _RESTORE_DATABASE.fullmatch(plan.database):
            raise MaintenanceError("temp_database", "the temporary database name is malformed")
        reconcile_temporary_database(
            target.journal_file,
            database=plan.database,
            maintenance_database=maintenance,
            passfile_value=role_passfile,
            tools=tools,
            network=target.spec.network,
            system_identifier=target.system_identifier,
        )
        record = backup.temp_database_record(
            action="authorized",
            operation_id=plan.operation_id,
            token=plan.token,
            database=plan.database,
            purpose="restore_verification",
            system_identifier=target.system_identifier,
            archive_format=point.manifest["format"],
            archive_sha256=point.manifest["snapshot"]["sha256"],
            archive_system_identifier=point.manifest["system_identifier"],
        )
        _append_temp(target.journal_file, record)

        _establish_fenced_database(
            target.journal_file,
            tools,
            role_passfile,
            plan,
            record,
            collate=point.manifest["datcollate"],
            ctype=point.manifest["datctype"],
            network=target.spec.network,
        )

        (work.path / backup.DUMP_NAME).write_bytes(point.dump)
        toc_text = tools.pg_restore_list(work.path)
        (work.path / "restore.list").write_text(
            _filtered_restore_list(toc_text), encoding="utf-8"
        )
        pg_restore(
            tools,
            role_passfile,
            database=plan.database,
            work=work.path,
            use_list=True,
            network=target.spec.network,
        )
        _apply_artifacts(tools, role_passfile, plan.database, network=target.spec.network)
        verification = verify_database(
            tools,
            role_passfile,
            database=plan.database,
            manifest=point.manifest,
            fixture=fixture,
            network=target.spec.network,
        )
        if verification.failures:
            raise MaintenanceError(
                "restore_verification",
                "the restored database did not match the archive",
                ", ".join(dict.fromkeys(verification.failures)),
            )
        alembic_check(
            tools,
            app_image=args.app_image,
            database=plan.database,
            role="hh_restore",
            passfile_value=role_passfile,
            network=target.spec.network,
        )
        _append_temp(target.journal_file, {**record, "action": "verified"})
        print(f"ok [restore]: {plan.database} is verified")
        print(f"     {manifest_summary(point)}")
        print(
            "     identity: session_user hh_restore; current_user hh_owner for object "
            "creation and ACL application"
        )
        rehearsal_failure: Optional[BaseException] = None
        rehearsal_container: Optional[str] = None
        rehearsal_removed = True
        rehearsal_released = False

        def release_rehearsal() -> None:
            """Remove this run's ONE disposable container and confirm it is gone (F2).

            `rehearse` never removes its own container: its exact name is the run's ownership
            identity, and the caller owns the removal on every path. The critical section is
            cancellation-deferred, so a signal cannot abandon a removal half-way (R3-F1); a
            signal that arrived there becomes the run's outcome afterwards."""
            nonlocal rehearsal_failure, rehearsal_removed, rehearsal_released
            if rehearsal_released or rehearsal_container is None or args.keep_rehearsal:
                return
            rehearsal_released = True
            with _deferred_cancellation() as deferred:
                rehearsal_removed = tools.remove_verified(
                    rehearsal_container, image=tools.image
                )
            if deferred.signums and rehearsal_failure is None:
                rehearsal_failure = RehearsalCancelled(deferred.signums[0])

        if not args.no_rehearse:
            # The restore's own operation id is this rehearsal's ownership identity. The
            # journal already records it with the temporary database, so the one container a
            # run could leave behind is named from the record an operator can read.
            run_id = plan.operation_id
            rehearsal_container = _rehearsal_container_name(run_id)
            rehearsal_removed = False
            try:
                with _cancellation_handlers():
                    try:
                        rehearsal = rehearse(
                            tools,
                            point=point,
                            work=work.path,
                            fixture=fixture,
                            app_image=args.app_image,
                            run_id=run_id,
                            keep=args.keep_rehearsal,
                        )
                        print(
                            "ok [rehearse]: isolated --network none restore verified; "
                            f"{rehearsal['tables']} tables, revision {rehearsal['revision']}; "
                            f"provenance system_identifier {rehearsal['system_identifier']}"
                        )
                        print(
                            f"     attachments: {_attachment_summary(rehearsal['attachments'])}"
                        )
                    except BaseException as error:  # still clean up the verified target
                        rehearsal_failure = error
                    finally:
                        release_rehearsal()
            except BaseException as error:
                # A signal that landed outside the deferred removal section is still this
                # run's outcome; the removal above has already been attempted.
                if rehearsal_failure is None:
                    rehearsal_failure = error
        database_failure: Optional[BaseException] = None
        if args.keep_database:
            print(
                "warning [restore]: the temporary database is retained under the explicit "
                f"Owner recovery decision; drop it with a reviewed operation ({plan.database})"
            )
        else:
            try:
                _drop_temporary_database(
                    target.journal_file,
                    tools=tools,
                    passfile_value=role_passfile,
                    plan=plan,
                    record=record,
                    network=target.spec.network,
                )
                print(f"ok [cleanup]: {plan.database} dropped and absence verified")
            except BaseException as error:  # FIX-01B: never let it hide the container outcome
                database_failure = error
        # R2/FIX-01B: the resource outcome takes precedence over both the rehearsal's own verdict
        # and a database clean-up refusal. A container that may still hold plaintext is never
        # suppressed by the later failure, and both failures are reported together rather than
        # one overwriting the other.
        if (
            rehearsal_container is not None
            and not args.keep_rehearsal
            and not rehearsal_removed
        ):
            detail = (
                f"{rehearsal_container}; remove it with: "
                f"{_rehearsal_cleanup_command(args.compose_file, run_id)}"
            )
            if database_failure is not None:
                detail += (
                    f"; additionally the temporary database {plan.database} was not "
                    f"confirmed cleaned up: {_cleanup_failure_label(database_failure)}"
                )
                journal_action = _temporary_journal_action(
                    target.journal_file, plan.database
                )
                if journal_action is not None:
                    detail += f"; its journal entry remains '{journal_action}'"
            raise MaintenanceError(
                "rehearsal_cleanup",
                "the disposable container could not be confirmed removed",
                detail,
            ) from database_failure
        # FIX-01B: when only the temporary-database clean-up failed, its documented refusal and
        # exit-status contract are unchanged.
        if database_failure is not None:
            raise database_failure
        if isinstance(rehearsal_failure, RehearsalCancelled):
            signum = rehearsal_failure.signum
            state = (
                "is retained under --keep-rehearsal"
                if args.keep_rehearsal
                else "was confirmed removed"
            )
            print(
                f"cancelled [restore]: signal {signum}; the disposable container "
                f"{rehearsal_container} {state}",
                file=sys.stderr,
            )
            return EX_CANCELLED
        if rehearsal_failure is not None:
            raise rehearsal_failure
        return EX_OK
    finally:
        work.cleanup(args.keep_work)


def migration_chain_object_contract(
    tools: ToolContainers, *, app_image: str, keep: bool = False
) -> dict[str, Any]:
    """BAK-10: the normalized object contract of a real, fresh migration chain.

    A disposable `--network none` PostgreSQL 16 container is initialized from
    `db/roles.sql`; the database is created from `template0` owned by `hh_owner`; the
    image's Alembic chain is applied with `current_user = hh_owner`; and only then is the
    catalog observed. This is deliberately *not* a read of an existing cluster: a
    cluster object altered outside the migration chain cannot enter the fixture.
    """
    container_name = f"house-helper-fixture-{secrets.token_hex(8)}"
    container = tools.start_detached(
        ["postgres"],
        name=container_name,
        environment=(
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "POSTGRES_USER=postgres",
            "POSTGRES_DB=postgres",
        ),
        network="none",
        tmpfs=("/var/lib/postgresql/data:rw,noexec,nosuid,nodev,size=1g",),
    )
    try:
        _await_postgres(tools, container, timeout=90)
        _local_sql(
            tools,
            container,
            database="postgres",
            sql="BEGIN;\n" + artifacts.render_roles() + "\nCOMMIT;\n",
            user="postgres",
        )
        _local_sql(
            tools,
            container,
            database="postgres",
            sql=(
                'CREATE DATABASE "house_helper" OWNER hh_owner TEMPLATE template0 '
                "ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C';"
            ),
            user="postgres",
        )
        migration = tools.run(
            ["python", "-m", "alembic", "upgrade", "head"],
            environment=[
                "HOUSE_HELPER_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/house_helper",
                "PGHOST=127.0.0.1",
                "PGPORT=5432",
                "PGUSER=postgres",
                "PGDATABASE=house_helper",
                "PGAPPNAME=hh_fixture",
                "PGOPTIONS=-c role=hh_owner",
            ],
            network=f"container:{container}",
            timeout=900,
            image=app_image,
        )
        if migration.returncode != 0:
            raise MaintenanceError(
                "fixture_migration",
                "the migration chain did not complete in the disposable container",
                _first_line(migration.stderr),
            )
        output = _local_sql(
            tools,
            container,
            database="house_helper",
            sql=(
                "SELECT 'revision', version_num FROM public.alembic_version;\n"
                + backup.object_contract_sql()
            ),
            user="postgres",
            timeout=300,
        )
        revision = ""
        rows: list[list[str]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            if parts[0] == "revision" and len(parts) == 2:
                revision = parts[1]
                continue
            rows.append(parts)
        if not revision:
            raise MaintenanceError("fixture", "the live revision could not be read")
        return backup.object_contract_from_rows(rows, revision=revision)
    finally:
        if not keep:
            tools.remove(container)
        else:
            print(f"warning [fixture]: retained disposable container {container_name}")


def fixture_command(args) -> int:
    """Regenerate the checked-in normalized object contract from the real chain.

    BAK-10: the fixture is generated by an implementation test against the real
    migration chain. A new migration deliberately regenerates it; a hand edit is a
    review event. The chain runs in a disposable container against a fresh database, so
    the fixture can never be "whatever the enrolled cluster happens to contain".
    """
    tools = ToolContainers(_image_from_compose(args.compose_file))
    tools.image_digest()
    value = migration_chain_object_contract(tools, app_image=args.app_image)
    revision = value["revision"]
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"ok [fixture]: {output_path}; revision {revision}; "
        f"sha256:{backup.object_contract_digest(value)[:16]}..."
    )
    return EX_OK


def verify_archive_command(args) -> int:
    tools = ToolContainers(_image_from_compose(args.compose_file))
    work = WorkDirectory(args.work_dir, prefix="house-helper-verify-")
    try:
        point, entries = validate_archive(args.archive, tools, work=work.path)
        fixture = backup.load_object_contract_fixture(args.object_contract)
        if fixture.get("revision") != point.manifest["alembic_revision"]:
            raise MaintenanceError(
                "object_contract_revision",
                "the archive revision does not match the checked-in object contract",
            )
        contract_failures = backup.toc_contract_failures(entries, fixture)
        if contract_failures:
            raise MaintenanceError(
                "archive_contract",
                "the archive TOC does not name the checked-in object contract",
                ", ".join(contract_failures),
            )
        print(f"ok [verify-archive]: {args.archive}")
        print(f"     {manifest_summary(point)}")
        print(f"     {len(entries)} TOC entries; exactly one SEQUENCE SET entry")
        print(
            "     provenance system_identifier "
            f"{point.manifest['system_identifier']}"
        )
        return EX_OK
    finally:
        work.cleanup(args.keep_work)


def _image_from_compose(compose_file: Path) -> str:
    spec = enroll.load_spec(Path(compose_file))
    return spec.image


# --- work directory --------------------------------------------------------------------------


class WorkDirectory:
    def __init__(self, parent: object, *, prefix: str) -> None:
        base = None if parent is None else Path(parent)
        try:
            if base is not None:
                base.mkdir(parents=True, exist_ok=True)
            created = Path(tempfile.mkdtemp(prefix=prefix, dir=None if base is None else str(base)))
        except OSError:
            raise MaintenanceError(
                "work_directory", "a private work directory cannot be created"
            ) from None
        self.path = created
        try:
            os.chmod(created, stat.S_IRWXU)
            if os.name == "posix":
                os.chown(created, credentials.RUNTIME_UID, credentials.RUNTIME_GID)
        except OSError:
            pass

    def cleanup(self, keep: bool) -> None:
        if keep:
            print(f"warning [work]: retained private work directory {self.path}")
            return
        try:
            shutil.rmtree(self.path)
        except OSError:
            print(
                "warning [work]: the private work directory could not be fully removed",
                file=sys.stderr,
            )


# --- command line ----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maintenance.py",
        description="HH-DB-01 PostgreSQL backup and restore (host, root).",
        allow_abbrev=False,
    )
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument(
        "--journal-dir",
        type=Path,
        default=Path(os.environ.get("HOUSE_HELPER_PG_JOURNAL_DIR", DEFAULT_JOURNAL_DIR)),
    )
    parser.add_argument(
        "--credentials-dir", type=Path, default=DEFAULT_CREDENTIALS_DIR
    )
    parser.add_argument(
        "--object-contract", type=Path, default=DEFAULT_OBJECT_CONTRACT
    )
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--keep-work", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    backup_cmd = commands.add_parser("backup", help="create and validate a format-v2 recovery point")
    backup_cmd.add_argument("--output", type=Path, required=True)
    backup_cmd.add_argument("--overwrite", action="store_true")

    verify_cmd = commands.add_parser(
        "verify-archive", help="validate a format-v2 recovery point without a database"
    )
    verify_cmd.add_argument("archive", type=Path)

    fixture_cmd = commands.add_parser(
        "write-object-contract-fixture",
        help="regenerate the checked-in normalized object contract (maintainers)",
    )
    fixture_cmd.add_argument("--output", type=Path, required=True)
    fixture_cmd.add_argument("--app-image", default=DEFAULT_APP_IMAGE)

    restore_cmd = commands.add_parser(
        "restore", help="restore into a new database, verify, then rehearse in isolation"
    )
    restore_cmd.add_argument("archive", type=Path)
    restore_cmd.add_argument(
        "--maintenance-database",
        required=True,
        choices=sorted(artifacts.MAINTENANCE_DATABASES),
        help="OD-2 is an explicit Owner choice; this tool never selects it",
    )
    restore_cmd.add_argument("--app-image", default=DEFAULT_APP_IMAGE)
    restore_cmd.add_argument("--no-rehearse", action="store_true")
    restore_cmd.add_argument("--keep-rehearsal", action="store_true")
    restore_cmd.add_argument("--keep-database", action="store_true")

    rehearse_cmd = commands.add_parser(
        "rehearse", help="restore and verify in a disposable --network none container"
    )
    rehearse_cmd.add_argument(
        "--run-id",
        default=None,
        help="the run's ownership identity (16-64 lowercase hex); names its one disposable container",
    )
    rehearse_cmd.add_argument("archive", type=Path)
    rehearse_cmd.add_argument("--app-image", default=DEFAULT_APP_IMAGE)
    rehearse_cmd.add_argument("--keep-container", action="store_true")
    rehearse_cmd.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="write the machine-readable result record (status contract v1) to this file",
    )

    cleanup_cmd = commands.add_parser(
        "cleanup-rehearsal",
        help="remove the one disposable container of a rehearsal run, confirmed gone",
    )
    cleanup_cmd.add_argument("--run-id", required=True)
    cleanup_cmd.add_argument(
        "--status-file",
        type=Path,
        default=None,
        help="write the machine-readable result record (status contract v1) to this file",
    )
    return parser


def _rehearsal_cleanup_command(compose_file: object, run_id: str) -> str:
    """The exact, non-secret command that removes this run's one container.

    The identity is the run id, so the command can name nothing else; `cleanup-rehearsal` itself
    confirms the image identity before removing."""
    return (
        f"python3 {Path(__file__)} --compose-file {Path(compose_file)} "
        f"cleanup-rehearsal --run-id {run_id}"
    )


# Refusals whose `code` is a stable authored token and whose `message` is safe to print. They are
# the only failures whose classification survives an unconfirmed clean-up for the caller.
_STRUCTURED_FAILURES = (
    MaintenanceError,
    journal.JournalError,
    enroll.enrollment.EnrollmentError,
    artifacts.ArtifactError,
    contract.ContractError,
    credentials.PassfileError,
    backup.BackupError,
    enroll.DockerError,
)


def _cleanup_failure_label(failure: BaseException) -> str:
    """One captured clean-up failure as a safe `[code] message` token pair.

    Only authored fields are used; the exception's `detail` is deliberately excluded because it
    may quote a tool's stderr, matching `_refuse_unconfirmed_cleanup`. A failure this contract
    does not know is reported as internal, never with its own text."""
    if isinstance(failure, _STRUCTURED_FAILURES):
        return f"[{failure.code}] {failure.message}"
    return f"[internal] unexpected {type(failure).__name__}."


def _temporary_journal_action(
    journal_file: journal.Journal, database: str
) -> Optional[str]:
    """The last journal action recorded for one temporary database, or None if unavailable.

    A read-only observation used only to describe an unresolved clean-up to the operator. A
    journal that cannot be read is reported as unknown, never assumed retired. `UnicodeError`
    covers a head anchor (or record line) that is not valid UTF-8: `Journal.read` decodes the
    head before its own JSON guard can apply, so such a file surfaces as a decode failure
    rather than a `JournalError`. It is observation-only; the corrupt file is never repaired."""
    try:
        view = journal_file.read()
    except (journal.JournalError, OSError, UnicodeError):
        return None
    latest: Optional[tuple[int, str]] = None
    for record in view.records:
        if getattr(record, "kind", None) != "temp_db":
            continue
        payload = getattr(record, "payload", None)
        if not isinstance(payload, Mapping) or payload.get("database") != database:
            continue
        action = payload.get("action")
        if not isinstance(action, str):
            continue
        seq = int(getattr(record, "seq", -1))
        if latest is None or seq >= latest[0]:
            latest = (seq, action)
    return latest[1] if latest is not None else None


class _Outcome:
    """The machine-readable outcome of one `rehearse`/`cleanup-rehearsal` run (R3-F3).

    `status` is the REHEARSAL outcome and `cleanup` the RESOURCE outcome; they are independent, so a
    verified restore whose removal failed is still reported as such without ever being read as a
    pass. `code` is one authored refusal token, never a message."""

    def __init__(self) -> None:
        self.command: Optional[str] = None
        self.run_id: Optional[str] = None
        self.status: Optional[str] = None
        # Fail-closed default: until a confirmed absence is observed, the container may remain.
        self.cleanup: str = "unconfirmed"
        self.code: Optional[str] = None

    def record(self) -> Optional[Mapping[str, Any]]:
        if self.status is None or self.command not in STATUS_COMMANDS:
            return None
        run_id = self.run_id if isinstance(self.run_id, str) and RUN_ID_RE.fullmatch(self.run_id) else None
        code = self.code
        if not isinstance(code, str) or not STATUS_CODE_RE.fullmatch(code):
            code = "internal" if self.status == "failed" else None
        if self.status != "failed":
            code = None
        return {
            "contract": STATUS_CONTRACT,
            "command": self.command,
            "run_id": run_id,
            "status": self.status,
            "cleanup": self.cleanup,
            "code": code,
        }


def _record_outcome(
    outcome: _Outcome, failed: Optional[BaseException], cleanup: str
) -> None:
    """Map one captured failure (or its absence) onto the status contract.

    The clean-up outcome is passed separately and is never inferred from the failure: an artifact
    verdict and an unconfirmed removal are different facts about different things."""
    outcome.cleanup = cleanup
    if isinstance(failed, RehearsalCancelled):
        outcome.status, outcome.code = "cancelled", None
    elif isinstance(failed, _STRUCTURED_FAILURES):
        outcome.status, outcome.code = "failed", failed.code
    elif failed is not None:
        outcome.status, outcome.code = "failed", "internal"
    else:
        outcome.status, outcome.code = "passed", None


def _write_status(path: Optional[object], outcome: _Outcome) -> None:
    """Write the record atomically (temporary name + rename), LF only, never over another file.

    Absent evidence is safe: the caller falls back to a verified removal and a FAILED outcome. A
    write that cannot complete leaves no record rather than a partial one."""
    if path is None:
        return
    record = outcome.record()
    if record is None:
        return
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(4)}")
    try:
        temporary.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()


def _refuse_unconfirmed_cleanup(
    container_name: str, run_id: str, compose_file: object, failed: Optional[BaseException]
) -> None:
    """Report an unconfirmed clean-up as the dominant outcome (F2/R2).

    The first line is the stable, machine-readable refusal the caller classifies; it names the ONE
    container this run owns and the exact command that removes it. The rehearsal's own failure, if
    any, follows on a second structured line: it is diagnostics and classification evidence only,
    and never replaces the clean-up failure. Only an authored message is printed - never the
    exception detail, a path inside a secret or a tool's stderr.
    """
    print(
        "refused [rehearsal_cleanup]: the disposable container could not be confirmed removed "
        f"({container_name}; remove it with: {_rehearsal_cleanup_command(compose_file, run_id)})",
        file=sys.stderr,
    )
    if isinstance(failed, RehearsalCancelled):
        print(
            f"underlying [cancelled]: a signal ({failed.signum}) interrupted the rehearsal",
            file=sys.stderr,
        )
    elif isinstance(failed, _STRUCTURED_FAILURES):
        # `code` is a stable, authored refusal token and `message` carries no secret; the detail is
        # deliberately excluded because it may quote a tool's stderr.
        print(f"underlying [{failed.code}]: {failed.message}", file=sys.stderr)
    elif failed is not None:
        print(f"underlying [internal]: unexpected {type(failed).__name__}.", file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    outcome = _Outcome()
    try:
        return _dispatch(args, outcome)
    finally:
        # R3-F3: the machine-readable record is written on EVERY path, after the clean-up outcome
        # and the rehearsal outcome are both known. Human-readable stderr is never the contract.
        _write_status(getattr(args, "status_file", None), outcome)


def _dispatch(args: argparse.Namespace, outcome: _Outcome) -> int:
    container_name = ""
    confirmed = False
    run_id: Optional[str] = None
    failed: Optional[BaseException] = None
    release: Optional[Callable[[], None]] = None
    try:
        if args.command == "backup":
            return backup_command(args)
        if args.command == "verify-archive":
            return verify_archive_command(args)
        if args.command == "write-object-contract-fixture":
            return fixture_command(args)
        if args.command == "restore":
            return restore_command(args)
        if args.command == "cleanup-rehearsal":
            # The fallback cleanup of a rehearsal whose own process could not run it (SIGKILL, a
            # crashed helper). It touches exactly one name, and only after the image identity is
            # confirmed; exit 0 means "confirmed absent", never "probably fine".
            outcome.command = args.command
            try:
                name = _rehearsal_container_name(args.run_id)
            except MaintenanceError as error:
                outcome.status, outcome.code = "usage", None
                print(f"refused [{error.code}]: {error.message}", file=sys.stderr)
                return EX_USAGE
            outcome.run_id = args.run_id
            image = _image_from_compose(args.compose_file)
            tools = ToolContainers(image)
            # R3-F1: a repeated signal while the fallback is already removing must not abandon the
            # removal half-way. Cancellation is deferred and the confirmed absence is still reported.
            with _deferred_cancellation():
                confirmed = tools.remove_verified(name, image=image)
            if not confirmed:
                outcome.status, outcome.code, outcome.cleanup = "failed", "rehearsal_cleanup", "unconfirmed"
                print(
                    f"warning [rehearsal_cleanup]: the disposable container {name} could not be confirmed removed",
                    file=sys.stderr,
                )
                return EX_UNAVAILABLE
            outcome.status, outcome.code, outcome.cleanup = "passed", None, "confirmed"
            print(f"ok [rehearsal_cleanup]: no disposable container of run {args.run_id} remains")
            return EX_OK
        if args.command == "rehearse":
            outcome.command = args.command
            image = _image_from_compose(args.compose_file)
            tools = ToolContainers(image)
            try:
                run_id = args.run_id or secrets.token_hex(16)
                container_name = _rehearsal_container_name(run_id)
            except MaintenanceError as error:
                outcome.status, outcome.code = "usage", None
                print(f"refused [{error.code}]: {error.message}", file=sys.stderr)
                return EX_USAGE
            outcome.run_id = run_id
            work = WorkDirectory(args.work_dir, prefix="house-helper-rehearse-")
            result: Optional[Mapping[str, Any]] = None
            released = False

            def release() -> None:
                """The bounded clean-up critical section, run at most once.

                Cancellation is deferred across it (R3-F1), so a SIGTERM/SIGINT - repeated or not -
                cannot abandon the verified removal. A signal that DID arrive is preserved as the
                run's outcome afterwards, never as a successful rehearsal."""
                nonlocal failed, confirmed, released
                if released:
                    return
                released = True
                with _deferred_cancellation() as deferred:
                    work.cleanup(args.keep_work)
                    if not args.keep_container:
                        confirmed = tools.remove_verified(container_name, image=image)
                if deferred.signums and failed is None:
                    failed = RehearsalCancelled(deferred.signums[0])

            with _cancellation_handlers():
                try:
                    point, entries = validate_archive(args.archive, tools, work=work.path)
                    fixture = backup.load_object_contract_fixture(args.object_contract)
                    if fixture.get("revision") != point.manifest["alembic_revision"]:
                        raise MaintenanceError(
                            "object_contract_revision",
                            "the archive revision does not match the checked-in object contract",
                        )
                    contract_failures = backup.toc_contract_failures(entries, fixture)
                    if contract_failures:
                        raise MaintenanceError(
                            "archive_contract",
                            "the archive TOC does not name the checked-in object contract",
                            ", ".join(contract_failures),
                        )
                    result = rehearse(
                        tools,
                        point=point,
                        work=work.path,
                        fixture=fixture,
                        app_image=args.app_image,
                        run_id=run_id,
                        keep=args.keep_container,
                    )
                except BaseException as error:
                    # F2/R2: the rehearsal outcome is captured, not propagated, so the verified
                    # removal below always runs. The clean-up outcome is resolved BEFORE the
                    # rehearsal outcome: an archive verdict must never suppress a container that may
                    # still hold plaintext.
                    failed = error
                finally:
                    release()
            if not args.keep_container and not confirmed:
                # Clean-up has precedence. The exact container name and the safe recovery command
                # are the evidence; the rehearsal's own reason is preserved for diagnostics but
                # cannot turn this into a verdict.
                _refuse_unconfirmed_cleanup(container_name, run_id, args.compose_file, failed)
                _record_outcome(outcome, failed, "unconfirmed")
                return EX_UNAVAILABLE
            cleanup_state = "kept" if args.keep_container else "confirmed"
            if failed is not None:
                _record_outcome(outcome, failed, cleanup_state)
                raise failed
            _record_outcome(outcome, None, cleanup_state)
            print(
                "ok [rehearse]: isolated --network none restore verified; "
                f"{result['tables']} tables, revision {result['revision']}; "
                f"provenance system_identifier {result['system_identifier']}"
            )
            print(f"     attachments: {_attachment_summary(result['attachments'])}")
            return EX_OK
        return EX_USAGE
    except RehearsalCancelled as stop:
        # A signal that landed outside the deferred critical section. Run the removal if it has not
        # run yet: an unconfirmed clean-up is never reported as a clean outcome (R3-F1).
        if release is not None:
            release()
        if confirmed:
            outcome.cleanup = "confirmed"
            if outcome.status is None:
                _record_outcome(outcome, failed if failed is not None else stop, "confirmed")
            print(
                f"cancelled [rehearsal_cleanup]: signal {stop.signum}; the disposable container "
                f"{container_name} was confirmed removed",
                file=sys.stderr,
            )
            return EX_CANCELLED
        if args.command == "rehearse" and container_name and not getattr(args, "keep_container", False):
            _refuse_unconfirmed_cleanup(container_name, run_id or "", args.compose_file, failed or stop)
            _record_outcome(outcome, failed or stop, "unconfirmed")
            return EX_UNAVAILABLE
        if outcome.status is None:
            _record_outcome(outcome, stop, outcome.cleanup)
        print(
            f"warning [rehearsal_cleanup]: signal {stop.signum}; the disposable container "
            f"{container_name or 'of this run'} could not be confirmed removed",
            file=sys.stderr,
        )
        return EX_UNAVAILABLE
    except MaintenanceError as error:
        if outcome.status is None:
            _record_outcome(outcome, error, outcome.cleanup)
        detail = "" if not error.detail else f" ({error.detail})"
        print(f"refused [{error.code}]: {error.message}{detail}", file=sys.stderr)
        if error.code in ("handoff_unknown", "migration_unknown"):
            # The cluster is in a reconciled-first UNKNOWN, not a plain refusal (LCK-08).
            return EX_UNKNOWN
        return EX_UNAVAILABLE if error.code in (
            "docker_unavailable", "docker_infrastructure", "image_unavailable", "rehearsal_cleanup",
            "attachment_proof_environment",
        ) else EX_REFUSED
    except enroll.DockerError as error:
        if outcome.status is None:
            _record_outcome(outcome, error, outcome.cleanup)
        print(f"refused [{error.code}]: {error.message}", file=sys.stderr)
        return EX_UNAVAILABLE if error.code in ("docker_unavailable", "docker_inspect") else EX_REFUSED
    except (
        journal.JournalError,
        enroll.enrollment.EnrollmentError,
        artifacts.ArtifactError,
        contract.ContractError,
        credentials.PassfileError,
        backup.BackupError,
    ) as error:
        if outcome.status is None:
            _record_outcome(outcome, error, outcome.cleanup)
        detail = "" if not getattr(error, "detail", None) else f" ({error.detail})"
        print(f"refused [{error.code}]: {error.message}{detail}", file=sys.stderr)
        return EX_REFUSED
    except Exception as error:  # never echo SQL, a path or an environment value
        if outcome.status is None:
            _record_outcome(outcome, error, outcome.cleanup)
        print(f"error [internal]: unexpected {type(error).__name__}.", file=sys.stderr)
        return EX_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
