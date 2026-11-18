#!/usr/bin/env python3
"""PostgreSQL backup and restore for HH-DB-01 Stage 2 WP-2D (host, root).

    python deploy/postgres/maintenance.py backup  --output PATH
    python deploy/postgres/maintenance.py verify-archive ARCHIVE
    python deploy/postgres/maintenance.py restore ARCHIVE --maintenance-database postgres
    python deploy/postgres/maintenance.py rehearse ARCHIVE

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

No credential is generated, printed, formatted or written by this tool. Passfiles are
validated on the host, mounted read-only into exactly one disposable container, copied
to a private `/tmp` path inside that container, and never appear in argv or logs.

Exit codes (the same shape as deploy/ops/dbtool.py and enroll.py):
  0 success, 1 internal, 2 usage, 3 refused, 4 environment unavailable, 5 UNKNOWN
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
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
STATE_LOCK = "/state/.house-helper.lock"
BACKUP_MUTEX = "/state/.house-helper.backup.lock"
# LCK-01/F5-B: the holder is not given an unconditional lifetime. The host keeps a
# heartbeat deadline fresh for as long as the backup operation is actually running; an
# abandoned holder expires only after the lease has not been refreshed. Publication is
# performed by the holder itself, so a lapsed or terminated holder cannot publish.
LOCK_HOLDER_LEASE_SECONDS = 15 * 60
LOCK_HOLDER_REFRESH_SECONDS = 60
# F5-B/R4: a recovery holder serves exactly one cleanup request and is removed by the
# host, so its deadline is a bound rather than a lease the host keeps refreshing.
LOCK_RECOVERY_LEASE_SECONDS = 120
LOCK_CONTROL_TARGET = "/ctl"
LOCK_OUTPUT_TARGET = "/out"
LOCK_READY_NAME = ".house-helper.ready"
LOCK_DEADLINE_NAME = ".house-helper.deadline"
LOCK_REQUEST_NAME = ".house-helper.publish.request"
LOCK_RESULT_NAME = ".house-helper.publish.result"
LOCK_CONFIRM_NAME = ".house-helper.confirm.request"
LOCK_CONFIRM_RESULT_NAME = ".house-helper.confirm.result"
# LCK-01/F5-B: failure cleanup is itself a mutex-held publication channel request, so the
# identity check and the conditional unlink cannot interleave with a successor's publish.
LOCK_CLEANUP_NAME = ".house-helper.cleanup.request"
LOCK_CLEANUP_RESULT_NAME = ".house-helper.cleanup.result"

EX_OK = 0
EX_INTERNAL = 1
EX_USAGE = 2
EX_REFUSED = 3
EX_UNAVAILABLE = 4
EX_UNKNOWN = 5

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


def _first_line(value: bytes, limit: int = 300) -> str:
    text = value.decode("utf-8", "replace").strip().splitlines()
    return (text[0] if text else "")[:limit]


def _write_control_request(control_dir: object, request_name: str, body: str) -> None:
    """Atomically place one request in a holder's control directory.

    LF only: the holder reads line-wise, and a host-side text mode would append `\\r`
    on Windows, making file names and identities invalid. A temporary name followed by
    `os.replace` means the holder can never read a half-written request.
    """
    control = Path(control_dir)
    temporary = control / f"{request_name}.{secrets.token_hex(4)}"
    temporary.write_bytes(body.encode("utf-8"))
    os.replace(temporary, control / request_name)


def _publish_request_body(staging_name: str, output_name: str, replace: bool) -> str:
    """The three-line publication request the holder parses, LF-only."""
    return f"{staging_name}\n{output_name}\n{'replace' if replace else 'noreplace'}\n"


def _cleanup_request_body(output_name: str, device: int, inode: int) -> str:
    """The three-line cleanup request the holder parses, LF-only."""
    return f"{output_name}\n{device}\n{inode}\n"


def _private_control_directory(prefix: str) -> Path:
    """A fresh private control directory for one cleanup-only recovery holder.

    It is deliberately not the failed operation's directory: it never contained a
    publication request, so there is nothing stale to inherit. The mode/ownership match
    `WorkDirectory`, so the same Docker bind-mount shape reaches the holder.
    """
    created = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        os.chmod(created, stat.S_IRWXU)
        if os.name == "posix":
            os.chown(created, credentials.RUNTIME_UID, credentials.RUNTIME_GID)
    except OSError:
        pass
    return created


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
        output_dir: object,
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
            mounts=[
                (control_dir, LOCK_CONTROL_TARGET, False),
                (output_dir, LOCK_OUTPUT_TARGET, False),
            ],
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
        output_dir: object,
        timeout: float = 30.0,
    ) -> str:
        """LCK-01/F5: start the detached owner of both locks for one backup.

        The holder also receives two narrow bind mounts: the private control directory
        (one heartbeat deadline and one publication request/result) and the output
        directory (the staging file and its final name only). No other host path is
        visible to it, and it runs with no network.
        """
        return self._start_holder(
            _lock_holder_script(),
            name=name,
            control_dir=control_dir,
            output_dir=output_dir,
            timeout=timeout,
        )

    def start_cleanup_holder(
        self,
        *,
        name: str,
        control_dir: object,
        output_dir: object,
        timeout: float = 30.0,
    ) -> str:
        """LCK-01/F5-B/R4: start a recovery holder that can only clean up.

        The same locks and the same mounts as `start_lock_holder`, but the script has
        no publication or confirmation branch, and the caller passes a fresh private
        control directory that never contained a publication request. This is the only
        holder started after the original holder is gone.
        """
        return self._start_holder(
            _cleanup_holder_script(),
            name=name,
            control_dir=control_dir,
            output_dir=output_dir,
            timeout=timeout,
        )

    def publish_in_holder(
        self,
        container: str,
        *,
        control_dir: object,
        staging_name: str,
        output_name: str,
        replace: bool,
        timeout: float = 120.0,
    ) -> str:
        """LCK-01/F5-B: ask the lock-owning process to publish the archive.

        The holder performs the no-replace link (or the explicitly authorized
        replacement) itself, so publication cannot happen in a process that does not
        hold the mutex, and there is no check-then-write window.
        """
        for value in (staging_name, output_name):
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", value):
                raise MaintenanceError("output_path", "an archive file name is not allowed")
        control = Path(control_dir)
        result = control / LOCK_RESULT_NAME
        try:
            result.unlink()
        except FileNotFoundError:
            pass
        # LF only: the holder reads line-wise, and a host-side text mode would append
        # `\r` on Windows, making the file names invalid.
        _write_control_request(
            control,
            LOCK_REQUEST_NAME,
            _publish_request_body(staging_name, output_name, replace),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result.exists():
                outcome = result.read_text(encoding="utf-8").strip()
                try:
                    result.unlink()
                except FileNotFoundError:
                    pass
                return outcome
            running = self._run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container],
                timeout=30,
            )
            if running.returncode != 0 or running.stdout.strip() == b"false":
                raise MaintenanceError(
                    "backup_lock",
                    "the lock holder terminated before the archive was published",
                )
            time.sleep(0.05)
        raise MaintenanceError(
            "backup_lock", "the lock holder did not confirm publication in time"
        )

    def cleanup_in_holder(
        self,
        container: str,
        *,
        control_dir: object,
        output_name: str,
        identity: tuple[int, int],
        timeout: float = 60.0,
    ) -> str:
        """LCK-01/F5-B: remove a failed publication from inside the lock-owning process.

        The holder stat()s the final path and unlinks it only when that identity is the
        exact file the failing operation staged, all in one uninterrupted section of the
        same loop iteration that holds the mutex. A successor's archive, published under
        that mutex, can never be removed: either it is not there yet (the holder still
        holds the mutex) or its inode is not ours.
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", output_name):
            raise MaintenanceError("output_path", "an archive file name is not allowed")
        try:
            device, inode = int(identity[0]), int(identity[1])
        except (TypeError, ValueError, IndexError):
            raise MaintenanceError("output_path", "a file identity is not valid") from None
        if device < 0 or inode < 0:
            raise MaintenanceError("output_path", "a file identity is not valid")
        control = Path(control_dir)
        result = control / LOCK_CLEANUP_RESULT_NAME
        try:
            result.unlink()
        except FileNotFoundError:
            pass
        # LF only, exactly like the publication request: the holder reads line-wise.
        _write_control_request(
            control, LOCK_CLEANUP_NAME, _cleanup_request_body(output_name, device, inode)
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result.exists():
                outcome = result.read_text(encoding="utf-8").strip()
                try:
                    result.unlink()
                except FileNotFoundError:
                    pass
                return outcome
            running = self._run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container],
                timeout=30,
            )
            if running.returncode != 0 or running.stdout.strip() == b"false":
                raise MaintenanceError(
                    "backup_lock",
                    "the lock holder terminated before the failed output was cleaned up",
                )
            time.sleep(0.05)
        raise MaintenanceError(
            "backup_lock", "the lock holder did not clean up the failed output in time"
        )

    def confirm_lock_holder(
        self, container: str, *, control_dir: object, timeout: float = 60.0
    ) -> None:
        """LCK-01/F5-B: the lock-owning process acknowledges success, not liveness.

        The host asks the holder itself, after the published archive has been read back.
        A process that has exited cannot answer, so an operation whose mutex was released
        before success was determined fails instead of reporting a backup.
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
                    "backup_lock", "the lock holder did not acknowledge success"
                )
            running = self._run(
                ["docker", "inspect", "--format", "{{.State.Running}}", container],
                timeout=30,
            )
            if running.returncode != 0 or running.stdout.strip() == b"false":
                raise MaintenanceError(
                    "backup_lock",
                    "the lock holder terminated before success was acknowledged",
                )
            time.sleep(0.05)
        raise MaintenanceError(
            "backup_lock", "the lock holder did not acknowledge success in time"
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
        self._run(["docker", "rm", "-f", container], timeout=60)

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


def _cleanup_request_block(control_dir: str, output_dir: str) -> str:
    """LCK-01/F5-B: the identity-checked cleanup request, shared by both holders.

    The final path is removed only when its identity is exactly the failed operation's
    own staged inode. The check and the unlink run in one uninterrupted section of the
    loop iteration that holds the mutex, so a successor's archive — published under that
    same mutex — can never be removed. A symlink is never treated as our archive.
    """
    return (
        f"  if [ -f {control_dir}/{LOCK_CLEANUP_NAME} ]; then "
        f'    output="$(sed -n 1p {control_dir}/{LOCK_CLEANUP_NAME})"; '
        f'    device="$(sed -n 2p {control_dir}/{LOCK_CLEANUP_NAME})"; '
        f'    inode="$(sed -n 3p {control_dir}/{LOCK_CLEANUP_NAME})"; '
        f"    rm -f {control_dir}/{LOCK_CLEANUP_NAME}; "
        "    valid=1; "
        "    outcome=error; "
        "    case \"$output\" in ''|*[!A-Za-z0-9._-]*) valid=0;; esac; "
        "    case \"$device\" in ''|*[!0-9]*) valid=0;; esac; "
        "    case \"$inode\" in ''|*[!0-9]*) valid=0;; esac; "
        '    if [ "$valid" = 1 ]; then '
        f'      if [ -L "{output_dir}/$output" ]; then :; '
        f'      elif [ ! -e "{output_dir}/$output" ]; then outcome=absent; '
        "      else "
        f'        identity="$(stat -c "%d %i" {output_dir}/"$output" 2>/dev/null)"; '
        '        if [ "$identity" = "$device $inode" ]; then '
        f'          if rm -f "{output_dir}/$output" 2>/dev/null; then outcome=removed; fi; '
        "        else outcome=retained; fi; "
        "      fi; "
        "    fi; "
        f"    printf '%s\\n' \"$outcome\" > {control_dir}/{LOCK_CLEANUP_RESULT_NAME}.tmp; "
        f"    mv -f {control_dir}/{LOCK_CLEANUP_RESULT_NAME}.tmp"
        f" {control_dir}/{LOCK_CLEANUP_RESULT_NAME}; "
        "  fi; "
    )


def _lock_holder_script(
    *,
    control_dir: str = LOCK_CONTROL_TARGET,
    output_dir: str = LOCK_OUTPUT_TARGET,
    state_lock: str = STATE_LOCK,
    backup_mutex: str = BACKUP_MUTEX,
) -> str:
    """LCK-01/F5-B: the detached owner of both locks, and the only publisher.

    The holder keeps the locks for the whole backup, signals readiness, and serves one
    publication request. It performs the publication in this same process, so the mutex
    is held at the instant the final path is created, and reports the published file's
    `st_dev st_ino` alongside the outcome so the host knows which inode it owns.

    It also serves one failure-cleanup request. The identity check and the conditional
    unlink run in the same loop iteration, under the same mutex: a successor operation
    cannot publish between them, so a successor's archive is never removed.

    A heartbeat deadline (refreshed by the host while the operation is active) is the
    only expiry: a live operation never expires, and an abandoned holder exits instead
    of blocking every later backup. A lapsed or terminated holder cannot publish,
    because it never sees the request.

    The path parameters exist so the contract can be executed end-to-end against a
    private directory in tests; production always uses the container mounts.
    """
    return (
        _lock_commands(state_lock=state_lock, backup_mutex=backup_mutex)
        + f"touch {control_dir}/{LOCK_READY_NAME}; "
        "while :; do "
        + _holder_deadline_check(control_dir)
        + f"  if [ -f {control_dir}/{LOCK_REQUEST_NAME} ]; then "
        f'    staging="$(sed -n 1p {control_dir}/{LOCK_REQUEST_NAME})"; '
        f'    output="$(sed -n 2p {control_dir}/{LOCK_REQUEST_NAME})"; '
        f'    mode="$(sed -n 3p {control_dir}/{LOCK_REQUEST_NAME})"; '
        f"    rm -f {control_dir}/{LOCK_REQUEST_NAME}; "
        "    valid=1; "
        "    case \"$staging\" in ''|*[!A-Za-z0-9._-]*) valid=0;; esac; "
        "    case \"$output\" in ''|*[!A-Za-z0-9._-]*) valid=0;; esac; "
        "    outcome=error; "
        '    if [ "$valid" = 1 ]; then '
        # The staged inode is the file that a successful link/rename publishes, so it is
        # also the identity a later cleanup must match. A failure to read it publishes
        # nothing and reports an error.
        f'      identity="$(stat -c "%d %i" {output_dir}/"$staging" 2>/dev/null)"; '
        '      if [ -n "$identity" ]; then '
        '        if [ "$mode" = replace ]; then '
        f'          if mv -f "{output_dir}/$staging" "{output_dir}/$output"'
        ' 2>/dev/null; then outcome="ok $identity"; fi; '
        "        else "
        f'          if ln "{output_dir}/$staging" "{output_dir}/$output"'
        " 2>/dev/null; then "
        f'            rm -f "{output_dir}/$staging"; outcome="ok $identity"; '
        f'          elif [ -e "{output_dir}/$output" ]'
        f' || [ -L "{output_dir}/$output" ]; then outcome=exists; fi; '
        "        fi; "
        "      fi; "
        "    fi; "
        f"    printf '%s\\n' \"$outcome\" > {control_dir}/{LOCK_RESULT_NAME}.tmp; "
        f"    mv -f {control_dir}/{LOCK_RESULT_NAME}.tmp"
        f" {control_dir}/{LOCK_RESULT_NAME}; "
        "  fi; "
        # F5-B failure cleanup: serialized with publication by construction, because the
        # holder owns the mutex for this whole iteration.
        + _cleanup_request_block(control_dir, output_dir)
        # Success is acknowledged by this same process, after the host has read the
        # published archive back. A holder that died cannot acknowledge, so its operation
        # is never reported successful.
        + f"  if [ -f {control_dir}/{LOCK_CONFIRM_NAME} ]; then "
        f"    rm -f {control_dir}/{LOCK_CONFIRM_NAME}; "
        f"    printf 'alive\\n' > {control_dir}/{LOCK_CONFIRM_RESULT_NAME}.tmp; "
        f"    mv -f {control_dir}/{LOCK_CONFIRM_RESULT_NAME}.tmp"
        f" {control_dir}/{LOCK_CONFIRM_RESULT_NAME}; "
        "  fi; "
        "  sleep 0.2; "
        "done"
    )


def _cleanup_holder_script(
    *,
    control_dir: str = LOCK_CONTROL_TARGET,
    output_dir: str = LOCK_OUTPUT_TARGET,
    state_lock: str = STATE_LOCK,
    backup_mutex: str = BACKUP_MUTEX,
) -> str:
    """LCK-01/F5-B/R4: a recovery holder that can only remove one exact inode.

    Recovery must never replay the failed operation's control directory: it may still
    hold a publication request the dead original holder never consumed, and replaying
    it would publish (and then delete) an archive a successor has since completed.

    This holder therefore takes the *same* maintenance lock and backup mutex with the
    same commands as the publishing holder, but it has no publication branch at all —
    no `ln`, no `mv` of an archive, no confirm branch. The only request it reads is the
    cleanup request. The restriction is structural in this script, so a stale
    publication message is inert even if one were present in its control directory.
    """
    return (
        _lock_commands(state_lock=state_lock, backup_mutex=backup_mutex)
        + f"touch {control_dir}/{LOCK_READY_NAME}; "
        "while :; do "
        + _holder_deadline_check(control_dir)
        + _cleanup_request_block(control_dir, output_dir)
        + "  sleep 0.2; "
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


def verify_target(compose_file: Path, journal_dir: Path) -> Target:
    spec = enroll.load_spec(compose_file)
    docker = enroll.Docker(spec)
    try:
        enroll.validate_image(spec, docker.inspect_image())
    except enroll.DockerError as error:
        raise MaintenanceError(error.code, error.message, error.detail) from None
    journal_file = journal.Journal(
        Path(journal_dir), require_root=(os.name == "posix")
    )
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
    """LCK-01/F5: the exclusive backup mutex and the publication it owns.

    The detached holder (a disposable, network-less client container) owns both flocks
    and performs the archive publication itself. The host refreshes a heartbeat deadline
    while the operation is active, so a legitimately active holder never expires, while
    a crashed host's holder exits after the unrefreshed lease instead of blocking every
    later backup forever.
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
            LOCK_REQUEST_NAME,
            LOCK_RESULT_NAME,
            LOCK_CONFIRM_NAME,
            LOCK_CONFIRM_RESULT_NAME,
            LOCK_CLEANUP_NAME,
            LOCK_CLEANUP_RESULT_NAME,
        ):
            try:
                (self.control_dir / name).unlink()
            except FileNotFoundError:
                pass
        self._write_deadline()
        self.container = self.tools.start_lock_holder(
            name=self.name,
            control_dir=self.control_dir,
            output_dir=self.output_dir,
        )
        self._thread = threading.Thread(
            target=self._refresh, name="backup-lock-heartbeat", daemon=True
        )
        self._thread.start()

    def publish(
        self, staging: Path, output: Path, replace: bool
    ) -> Optional[tuple[int, int]]:
        if (
            Path(staging).parent.resolve() != self.output_dir.resolve()
            or Path(output).parent.resolve() != self.output_dir.resolve()
        ):
            raise MaintenanceError(
                "output_path", "the archive is not published into the locked output directory"
            )
        if self.container is None:
            raise MaintenanceError("backup_lock", "the backup mutex is not held")
        try:
            recorded = Path(staging).stat()
        except OSError:
            raise MaintenanceError("output_path", "the staging file is absent") from None
        staged_identity = (recorded.st_dev, recorded.st_ino)
        try:
            outcome = self.tools.publish_in_holder(
                self.container,
                control_dir=self.control_dir,
                staging_name=Path(staging).name,
                output_name=Path(output).name,
                replace=replace,
            )
        except MaintenanceError:
            # The holder may have been killed in the instant after `ln`. If the final
            # path is the very inode we staged, it is ours and unreported. F5-B: that
            # removal is itself serialized by the mutex and identity-checked inside the
            # holder, so it cannot race a successor's publication. Never unlink directly
            # here: the holder may already be gone and a successor may hold the mutex.
            self.cleanup(output, staged_identity)
            raise
        fields = outcome.split()
        if fields and fields[0] == "ok":
            # The holder reports the published inode (`st_dev st_ino`) from inside the
            # mutex, so later failure cleanup verifies the exact file it created.
            if len(fields) == 3:
                try:
                    return int(fields[1]), int(fields[2])
                except ValueError:
                    return staged_identity
            return staged_identity
        if outcome == "exists":
            raise MaintenanceError(
                "output_exists",
                "the output path already exists; refusing to overwrite",
            )
        raise MaintenanceError(
            "output_path",
            "the lock holder could not publish the archive",
            outcome,
        )

    def cleanup(self, output: object, identity: Optional[Sequence[int]]) -> bool:
        """LCK-01/F5-B: remove the failed publication only while the mutex is held.

        The request goes to the lock-owning holder, which performs the identity check
        and the conditional unlink in one uninterrupted mutex-protected section. If the
        original holder is gone, a recovery holder acquires the same mutex with a
        non-blocking `flock` and performs the same check. If exclusive ownership cannot
        be established (a successor holds the mutex, or no holder can start), nothing is
        removed: the failure stays fail-closed. This method never raises.
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
        name = target.name
        try:
            if self.container is not None:
                try:
                    outcome = self.tools.cleanup_in_holder(
                        self.container,
                        control_dir=self.control_dir,
                        output_name=name,
                        identity=expected,
                    )
                except MaintenanceError:
                    pass
                else:
                    return outcome == "removed"
            return self._recover_cleanup(name, expected)
        except Exception:  # noqa: BLE001 - failure cleanup must never mask the failure
            return False

    def _recover_cleanup(self, name: str, expected: tuple[int, int]) -> bool:
        """LCK-01/F5-B/R4: re-acquire the mutex for cleanup when the original holder died.

        `flock -n` in the recovery holder makes this non-blocking: if a successor holds
        the mutex, the holder exits and the archive is left exactly as the successor
        published it. The original holder being alive is handled before this point, so
        no two live holders ever contend for the same mutex.

        R4: recovery never reuses the failed operation's control directory and never
        starts a publishing holder. A dead operation may have left an unconsumed
        publication request there; a recovery holder started on that directory would
        replay it, overwrite a successor's completed archive and then clean it up. The
        recovery holder is a distinct cleanup-only script mounted on a fresh private
        directory holding at most our one cleanup request, and that script has no
        publication branch to reach.
        """
        recovery: Optional[str] = None
        control: Optional[Path] = None
        try:
            control = _private_control_directory(f"{self.name}-cleanup-")
            # A recovery holder has no heartbeat thread: its deadline is a hard bound
            # on how long the host may wait for one cleanup request.
            (control / LOCK_DEADLINE_NAME).write_text(
                str(int(time.time()) + LOCK_RECOVERY_LEASE_SECONDS), encoding="utf-8"
            )
            recovery = self.tools.start_cleanup_holder(
                name=f"{self.name}-cleanup-{secrets.token_hex(4)}",
                control_dir=control,
                output_dir=self.output_dir,
            )
        except (MaintenanceError, OSError):
            if control is not None:
                shutil.rmtree(control, ignore_errors=True)
            return False
        try:
            outcome = self.tools.cleanup_in_holder(
                recovery,
                control_dir=control,
                output_name=name,
                identity=expected,
            )
        except MaintenanceError:
            return False
        finally:
            try:
                self.tools.remove(recovery)
            except Exception:  # noqa: BLE001 - the cleanup outcome is already decided
                pass
            shutil.rmtree(control, ignore_errors=True)
        return outcome == "removed"

    def confirm(self) -> None:
        if self.container is None:
            raise MaintenanceError("backup_lock", "the backup mutex is not held")
        self.tools.confirm_lock_holder(self.container, control_dir=self.control_dir)

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


def _unlink_if_identity(output: Path, identity: Optional[Sequence[int]]) -> None:
    """Remove `output` only when its identity is still the expected one.

    Used as the fallback failure cleanup when no lock holder is supplied (direct
    callers and tests). Production always cleans up through `_BackupLock.cleanup`,
    which performs the same check under the backup mutex so it cannot race a successor.
    """
    if identity is None:
        return
    try:
        expected = (int(identity[0]), int(identity[1]))
    except (TypeError, ValueError, IndexError):
        return
    if Path(output).is_symlink():
        return
    if _file_identity(output) != expected:
        return
    try:
        Path(output).unlink()
    except OSError:
        pass


def _host_publish(
    staging: Path, output: Path, replace: bool
) -> Optional[tuple[int, int]]:
    """The host-side atomic publication used when no lock holder is supplied.

    Production backups pass the holder's own `publish`; this default keeps the same
    no-replace guarantee for direct callers and makes the primitive independently
    testable. The returned identity is the published file's, which equals the staged
    file's because `link` and `replace` preserve the inode.
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
    """F5-A/F5-B: validate into a private staging file, then publish atomically.

    `publish` is the lock holder's own publication step when a backup runs under the
    exclusive mutex; the default is the equivalent host-side atomic no-replace link.
    With `overwrite=false` an existing final path is never replaced, even if it appears
    between the last check and publication. A failure leaves no output and removes the
    staging file, so an incomplete archive is never visible as a recovery point.

    F5-B: failure cleanup of a published output goes through `cleanup` when supplied
    (the lock holder's mutex-serialized, identity-checked removal). It removes the
    output only if it is still the exact inode `publish` returned, so an archive a
    successor published after the mutex was lost is never touched.
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
        # The lock owner acknowledges after publication *and* read-back, so success is
        # never reported for an operation whose holder has already gone.
        if confirm is not None:
            confirm()
    except BaseException:
        if published:
            # Never unlink unconditionally: after a lost holder a successor may have
            # published its own archive here. Removal happens under the mutex and only
            # for the exact inode this operation published.
            if cleanup is not None:
                try:
                    cleanup(output, identity)
                except Exception:  # noqa: BLE001 - preserve the original failure
                    pass
            elif publish is None:
                # The host-side primitive has no mutex to serialize against; the same
                # identity check still applies. A supplied publisher without its
                # mutex-held cleanup is treated as unknown ownership: fail closed.
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
    target = verify_target(args.compose_file, args.journal_dir)
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
        # LCK-01/F5: the exclusive backup mutex covers the dump, the manifest, the
        # output-path checks, archive publication and its read-back. The holder performs
        # the publication itself; it is released by removing the holder, so an internal
        # error cannot leave it held.
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
_ATTACHMENT_PROOF = r"""
import hashlib
import json
from datetime import datetime, timezone

import sqlalchemy as sa

from house_helper.persistence import attachments
from house_helper.persistence.database import create_db_engine, create_session_factory
from house_helper.persistence.models import MessageAttachment

engine = create_db_engine()
Session = create_session_factory(engine)
result = {"count": 0, "bytes": 0, "declared_bytes": 0, "sample": False}
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
            raise SystemExit(
                "the attachment byte total does not match the declared sizes"
            )
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
                raise SystemExit(
                    "the attachment loader returned nothing for an existing attachment"
                )
            if (
                loaded.id != row.id
                or len(loaded.data) != int(row.size_bytes)
                or hashlib.sha256(loaded.data).hexdigest() != row.sha256
            ):
                raise SystemExit(
                    "the attachment loader bytes do not match the stored evidence"
                )
            result["sample"] = True
            result["sample_id"] = str(row.id)
            result["sample_bytes"] = len(loaded.data)
finally:
    engine.dispose()
print(json.dumps(result, sort_keys=True))
"""


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
            "attachment_proof",
            "the attachment proof did not complete",
            _first_line(done.stderr),
        )
    lines = done.stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        value = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, Mapping):
        raise MaintenanceError(
            "attachment_proof", "the attachment proof did not return a structured result"
        )
    count = value.get("count")
    total = value.get("bytes")
    declared = value.get("declared_bytes")
    sample = value.get("sample")
    if (
        not isinstance(count, int)
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
        raise MaintenanceError(
            "attachment_proof", "the attachment proof failed its exactness checks"
        )
    if sample:
        sample_bytes = value.get("sample_bytes")
        if (
            not isinstance(value.get("sample_id"), str)
            or not isinstance(sample_bytes, int)
            or isinstance(sample_bytes, bool)
            or not 0 < sample_bytes <= total
        ):
            raise MaintenanceError(
                "attachment_proof", "the attachment sample evidence is malformed"
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
    keep: bool = False,
) -> Mapping[str, Any]:
    """§8.6 levels 2/3 in a throwaway `--network none` container."""
    work_path = Path(work)
    container_name = f"house-helper-restore-verify-{secrets.token_hex(8)}"
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
        if not keep:
            tools.remove(container)
        else:
            print(f"warning [rehearsal]: retained disposable container {container_name}")


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
    tools_template: Optional[ToolContainers] = None
    work = WorkDirectory(args.work_dir, prefix="house-helper-restore-")
    try:
        target = verify_target(args.compose_file, args.journal_dir)
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
        if not args.no_rehearse:
            try:
                rehearsal = rehearse(
                    tools,
                    point=point,
                    work=work.path,
                    fixture=fixture,
                    app_image=args.app_image,
                    keep=args.keep_rehearsal,
                )
                print(
                    "ok [rehearse]: isolated --network none restore verified; "
                    f"{rehearsal['tables']} tables, revision {rehearsal['revision']}; "
                    f"provenance system_identifier {rehearsal['system_identifier']}"
                )
                print(f"     attachments: {_attachment_summary(rehearsal['attachments'])}")
            except BaseException as error:  # still clean up the verified target
                rehearsal_failure = error
        if args.keep_database:
            print(
                "warning [restore]: the temporary database is retained under the explicit "
                f"Owner recovery decision; drop it with a reviewed operation ({plan.database})"
            )
            if rehearsal_failure is not None:
                raise rehearsal_failure
            return EX_OK
        _drop_temporary_database(
            target.journal_file,
            tools=tools,
            passfile_value=role_passfile,
            plan=plan,
            record=record,
            network=target.spec.network,
        )
        print(f"ok [cleanup]: {plan.database} dropped and absence verified")
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
    rehearse_cmd.add_argument("archive", type=Path)
    rehearse_cmd.add_argument("--app-image", default=DEFAULT_APP_IMAGE)
    rehearse_cmd.add_argument("--keep-container", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            return backup_command(args)
        if args.command == "verify-archive":
            return verify_archive_command(args)
        if args.command == "write-object-contract-fixture":
            return fixture_command(args)
        if args.command == "restore":
            return restore_command(args)
        if args.command == "rehearse":
            tools = ToolContainers(_image_from_compose(args.compose_file))
            work = WorkDirectory(args.work_dir, prefix="house-helper-rehearse-")
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
                    keep=args.keep_container,
                )
                print(
                    "ok [rehearse]: isolated --network none restore verified; "
                    f"{result['tables']} tables, revision {result['revision']}; "
                    f"provenance system_identifier {result['system_identifier']}"
                )
                print(f"     attachments: {_attachment_summary(result['attachments'])}")
                return EX_OK
            finally:
                work.cleanup(args.keep_work)
        return EX_USAGE
    except MaintenanceError as error:
        detail = "" if not error.detail else f" ({error.detail})"
        print(f"refused [{error.code}]: {error.message}{detail}", file=sys.stderr)
        return EX_UNAVAILABLE if error.code in ("docker_unavailable", "image_unavailable") else EX_REFUSED
    except enroll.DockerError as error:
        print(f"refused [{error.code}]: {error.message}", file=sys.stderr)
        return EX_UNAVAILABLE if error.code in ("docker_unavailable", "docker_inspect") else EX_REFUSED
    except (
        journal.JournalError,
        artifacts.ArtifactError,
        contract.ContractError,
        credentials.PassfileError,
        backup.BackupError,
    ) as error:
        detail = "" if not getattr(error, "detail", None) else f" ({error.detail})"
        print(f"refused [{error.code}]: {error.message}{detail}", file=sys.stderr)
        return EX_REFUSED
    except Exception as error:  # never echo SQL, a path or an environment value
        print(f"error [internal]: unexpected {type(error).__name__}.", file=sys.stderr)
        return EX_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
