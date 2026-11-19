#!/usr/bin/env python3
"""The root-controlled host enrollment and migration tool (HH-DB-01 Stage 2 WP-2C-A).

    python deploy/postgres/enroll.py status
    python deploy/postgres/enroll.py mode-a --approved-by <owner>
    python deploy/postgres/enroll.py verify
    python deploy/postgres/enroll.py handoff --role <hh_app|hh_provision|hh_backup> --approved-by <owner>
    python deploy/postgres/enroll.py migrate
    python deploy/postgres/enroll.py check-credentials --credentials-dir <dir>
    python deploy/postgres/enroll.py plan-bootstrap-retirement

This tool runs on the deployment host, as root, with the Docker socket. It is NOT copied
into the application image (the image contains only `house_helper/`, `deploy/ops/`,
`db/` and the configuration). It never connects to a PostgreSQL endpoint over the network:
every SQL statement runs as the container's `postgres` OS user through the local peer
socket, via `docker exec`. It never discovers a cluster: the container name, image
reference, PGDATA volume and database network come from the recorded deployment
configuration (the compose file), and every observed fact is checked against it.

The authority flow
------------------
Mode A: positively verify the container/volume/image, observe a pristine cluster, append
`cluster_enrollment:intent` OUTSIDE PGDATA, then create roles, the fenced database and the
marker, appending observed milestones. Resume is only the same intent (E1-E6); a foreign
object, drifted role or different `system_identifier` fails closed.

Mode B (`verify`, and the first half of `migrate`): require exactly one completed record,
validate the digest chain, compare the recorded container identity, volume, image and
`system_identifier` with Docker and the live cluster, compare the marker and the role/
database/predefined-role inventories, and refuse with a journaled
`cluster_identity:mismatch` on any difference. There is no Mode A fallback.

`handoff`: the ROL-16/HOF-01 connection handoff. One `GRANT CONNECT ON DATABASE
house_helper` to exactly one of `hh_app` (H1), `hh_provision` (H2) or `hh_backup` (H3),
after a Mode B identity/marker/inventory check and a schema-at-head gate. The intent -
including the complete pre-state ACL and a run-id-derived `application_name` - is journaled
before the statement. A non-zero psql status is classified by its exact meaning, and the
outcome is journaled only when the operation's termination and the complete ACL are both
established. An unobservable result stays UNKNOWN, blocks every later H1/H2/H3 transition,
and is never cleared from an instantaneous snapshot. Repeated invocation is a no-op that
records the observed state.

`migrate`: after Mode B passes, write a bounded non-secret authorization record to the
container's stdin (never argv, never the journal, never a file the web app can read) and
run the `migrate` one-shot under Compose. The one-shot re-reads the live database, checks
the artifact manifest against its own image, takes the state-volume exclusive lock and
only then runs the ordered initializer/upgrade. A direct `docker compose run migrate`
does not open the stdin channel and is refused.

The migration worker is launched under a deterministic run-specific name and WITHOUT
`--rm` (F10), so a client timeout leaves container lifecycle evidence. The reconciler
binds that evidence to the pending run and the frozen Compose project/service; a confirmed
terminal worker is removed only after the outcome is durable, by exact id, never by a
global cleanup.

No credential is generated, populated, printed or written. OD-8 remains open; the
bootstrap password retirement is a printed plan only.

Exit codes: 0 success, 1 internal, 2 usage, 3 refused, 4 environment unavailable,
5 UNKNOWN (a migrate or handoff client timed out or was interrupted, or a handoff's
termination/ACL could not be established; the outcome must be reconciled before another
operation).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from house_helper.persistence import pg_artifacts as artifacts  # noqa: E402
from house_helper.persistence import pg_contract as contract  # noqa: E402
from house_helper.persistence import pg_credentials as credentials  # noqa: E402
from house_helper.persistence import pg_enrollment as enrollment  # noqa: E402
from house_helper.persistence import pg_journal as journal  # noqa: E402

DEFAULT_COMPOSE = REPOSITORY_ROOT / "deploy" / "compose.postgres.yaml"
DEFAULT_JOURNAL_DIR = Path("/var/lib/house-helper/cutover")
DEFAULT_CREDENTIALS_DIR = REPOSITORY_ROOT / "deploy" / "postgres" / "credentials"

EX_OK = 0
EX_INTERNAL = 1
EX_USAGE = 2
EX_REFUSED = 3
EX_UNAVAILABLE = 4
EX_UNKNOWN = 5


class DockerError(Exception):
    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


class DockerLaunchError(DockerError):
    """The worker was confirmed NOT launched (the Docker CLI refused to start)."""


class DockerUnknown(DockerError):
    """The Docker client timed out or was interrupted after launch.

    The client process ending does NOT prove the worker stopped; its outcome is UNKNOWN
    until the cluster is observed (LCK-08).
    """


# --- the recorded deployment configuration ---------------------------------------------------


@dataclass(frozen=True)
class ClusterSpec:
    project: str
    container_name: str
    image: str
    volume: str
    network: str
    # F3: the full canonical host source of each accepted read-only bind, resolved from
    # the recorded Compose configuration (never from the inspected container).
    postgresql_conf_source: str
    pg_hba_source: str
    bootstrap_credential_source: str


# Docker Desktop for Windows reports a bind source as the path inside its Linux VM:
# C:\Users\me\file is reported as /run/desktop/mnt/host/c/Users/me/file, while Compose
# resolves the host (drive-letter) form. This is the one representation pair translated
# below; every other source is compared unchanged.
_DOCKER_DESKTOP_HOST_PREFIX = "/run/desktop/mnt/host/"
_IS_WINDOWS = os.name == "nt"


def _docker_desktop_windows_path(value: str) -> str:
    """The drive-letter form of an exact Docker Desktop VM source, or the value unchanged.

    Only `/run/desktop/mnt/host/<drive>/<rest>` is translated. A lookalike prefix, a
    malformed drive or a path outside the prefix is returned as-is, so the full-source
    comparison that follows cannot accept it by accident (F3).
    """
    if not value.startswith(_DOCKER_DESKTOP_HOST_PREFIX):
        return value
    remainder = value[len(_DOCKER_DESKTOP_HOST_PREFIX):]
    drive = remainder[:1]
    if (
        len(remainder) < 2
        or not drive.isascii()
        or not drive.isalpha()
        or remainder[1] != "/"
    ):
        return value
    return drive.upper() + ":\\" + remainder[2:].replace("/", "\\")


def _canonical_path(value: object) -> str:
    """The one comparison form for a mount source.

    Absolute, symlinks resolved and case-normalised on Windows - the same rule the
    accepted preflight uses. On Windows, Docker Desktop's `/run/desktop/mnt/host/`
    representation of a host path is first translated to its drive-letter form; the
    comparison remains the full resolved source. A basename match is deliberately not
    enough (F3).
    """
    text = str(value)
    if _IS_WINDOWS:
        text = _docker_desktop_windows_path(text)
    return os.path.normcase(os.path.realpath(text))


def _compose_path(compose_dir: Path, raw: str) -> str:
    path = Path(raw)
    if not path.is_absolute():
        path = compose_dir / path
    return _canonical_path(path)


def _single(pattern: str, text: str, key: str) -> str:
    found = re.findall(pattern, text, re.MULTILINE)
    if len(found) != 1:
        raise DockerError(
            "compose_identity",
            f"the compose file does not name exactly one {key}",
            str(len(found)),
        )
    return found[0]


def load_spec(compose_file: Path) -> ClusterSpec:
    """Read the container identity AND the authorized bind sources from Compose.

    Each fact is taken from an exactly shaped line. A missing or ambiguous fact is a
    refusal; the tool never guesses, never scans Docker for a candidate, and never trusts
    the inspected container's `Source` as the authority (F3). The credential directory is
    `${HOUSE_HELPER_PG_CREDENTIALS_DIR:-<default>}`, resolved exactly as Compose resolves
    it: relative paths are relative to the compose file's directory.
    """
    try:
        text = compose_file.read_text(encoding="utf-8")
    except OSError:
        raise DockerError("compose_file", "the compose file cannot be read") from None
    compose_dir = compose_file.resolve().parent
    patterns = {
        "project": r"^name:\s*(house-helper-pg)\s*$",
        "container_name": r"^\s*container_name:\s*(house-helper-db)\s*$",
        "image": r"^\s*image:\s*(postgres@sha256:[0-9a-f]{64})\s*$",
        "volume": r"^\s*name:\s*(house-helper-pgdata)\s*$",
        "network": r"^\s*name:\s*(house-helper-pg-db)\s*$",
    }
    values: dict[str, str] = {}
    for key, pattern in patterns.items():
        values[key] = _single(pattern, text, key)
    postgresql_conf = _single(
        r"^\s*source:\s*(\./postgres/postgresql\.conf)\s*$",
        text,
        "postgresql.conf bind source",
    )
    pg_hba = _single(
        r"^\s*source:\s*(\./postgres/pg_hba\.conf)\s*$",
        text,
        "pg_hba.conf bind source",
    )
    credential_default = _single(
        r"^\s*source:\s*\$\{HOUSE_HELPER_PG_CREDENTIALS_DIR:-([^}]*)\}"
        r"/postgres_superuser\.password\s*$",
        text,
        "bootstrap credential bind source",
    )
    credential_dir = (
        os.environ.get("HOUSE_HELPER_PG_CREDENTIALS_DIR") or credential_default
    )
    credential_path = Path(credential_dir)
    if not credential_path.is_absolute():
        credential_path = compose_dir / credential_path
    return ClusterSpec(
        project=values["project"],
        container_name=values["container_name"],
        image=values["image"],
        volume=values["volume"],
        network=values["network"],
        postgresql_conf_source=_compose_path(compose_dir, postgresql_conf),
        pg_hba_source=_compose_path(compose_dir, pg_hba),
        bootstrap_credential_source=_canonical_path(
            credential_path / "postgres_superuser.password"
        ),
    )


# --- the Docker/local-peer adapter -----------------------------------------------------------


class Docker:
    """Every call is a fixed argv list to `docker`; there is no shell and no discovery."""

    def __init__(self, spec: ClusterSpec, *, runner=subprocess.run) -> None:
        self.spec = spec
        self.runner = runner

    def _run(self, argv: Sequence[str], *, stdin: Optional[bytes] = None) -> subprocess.CompletedProcess:
        try:
            return self.runner(
                list(argv),
                input=stdin,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DockerError("docker_unavailable", "the Docker CLI could not be run") from None

    def _json(self, argv: Sequence[str]) -> Any:
        done = self._run(argv)
        if done.returncode != 0:
            raise DockerError("docker_inspect", "a Docker inspect command failed")
        try:
            return json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DockerError("docker_inspect", "a Docker inspect answer was not JSON") from None

    def inspect_container(self) -> Mapping[str, Any]:
        value = self._json(["docker", "inspect", "--type", "container", self.spec.container_name])
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise DockerError("container_identity", "the container inspection is not one object")
        return value[0]

    def inspect_container_id(self, container_id: str) -> Mapping[str, Any]:
        """Inspect the exact immutable container id, never a mutable name.

        Every handoff mutation and observation is bound to the id Mode B verified. A
        replacement container with the same name is therefore never a valid target: this
        inspect names the old id, and `docker exec` names the old id too.
        """
        if not isinstance(container_id, str) or not _ID.fullmatch(container_id):
            raise DockerError("container_identity", "the container id is malformed")
        value = self._json(
            ["docker", "inspect", "--type", "container", container_id]
        )
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise DockerError(
                "container_identity", "the container inspection is not one object"
            )
        container = value[0]
        if str(container.get("Id", "")) != container_id:
            raise DockerError(
                "container_identity", "the inspected container is not the verified one"
            )
        return container

    def verify_handoff_container(self, container_id: str) -> Mapping[str, Any]:
        """Re-verify the immutable identity before and after a handoff step (Finding 5).

        The container is re-inspected by id, its full WP-2B identity is validated, and the
        name must still resolve to the same id. A replaced, removed, stopped or
        renamed-away container fails closed; the caller retains UNKNOWN or refuses before
        any GRANT. The name is never used as the execution target.
        """
        container = self.inspect_container_id(container_id)
        validate_container(self.spec, container)
        named = self.inspect_container()
        if str(named.get("Id", "")) != container_id:
            raise DockerError(
                "container_replacement",
                "the container name no longer resolves to the verified container",
            )
        return container

    def _exec_target(self, container_id: Optional[str]) -> str:
        """The exact exec target: a validated immutable id, or the recorded name.

        A handoff passes the id Mode B verified. Other read-only observations keep the
        name, whose identity is validated at the start of their own operation.
        """
        if container_id is None:
            return self.spec.container_name
        if not isinstance(container_id, str) or not _ID.fullmatch(container_id):
            raise DockerError("container_identity", "the container id is malformed")
        return container_id

    def inspect_volume(self) -> Mapping[str, Any]:
        value = self._json(["docker", "volume", "inspect", self.spec.volume])
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise DockerError("volume_identity", "the volume inspection is not one object")
        return value[0]

    def inspect_image(self) -> Mapping[str, Any]:
        value = self._json(["docker", "image", "inspect", self.spec.image])
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise DockerError("image_identity", "the image inspection is not one object")
        return value[0]

    def attached_containers(self) -> tuple[str, ...]:
        done = self._run(
            [
                "docker",
                "ps",
                "-a",
                "--no-trunc",
                "--filter",
                f"volume={self.spec.volume}",
                "--format",
                "{{.ID}}",
            ]
        )
        if done.returncode != 0:
            raise DockerError("volume_attachment", "the volume attachment listing failed")
        return tuple(
            line.strip()
            for line in done.stdout.decode("utf-8", "replace").splitlines()
            if line.strip()
        )

    def psql(
        self, database: str, sql: str, *, container_id: Optional[str] = None
    ) -> str:
        """Run SQL as the container's `postgres` OS user over the local peer socket.

        `container_id` binds a handoff observation to the exact container Mode B verified;
        without it, the recorded name is used for the tool's other read-only operations.
        """
        if database not in ("postgres", contract.SERVING_DATABASE):
            raise DockerError("psql_database", "the maintenance SQL names another database")
        if not isinstance(sql, str) or not sql.strip():
            raise DockerError("psql_sql", "no SQL was supplied")
        done = self._run(
            [
                "docker",
                "exec",
                "-i",
                "-u",
                "postgres",
                self._exec_target(container_id),
                "psql",
                "-X",
                "-q",
                "-A",
                "-t",
                "-F",
                "\x1f",
                "-v",
                "ON_ERROR_STOP=1",
                "-d",
                database,
                "-f",
                "-",
            ],
            stdin=sql.encode("utf-8"),
        )
        if done.returncode != 0:
            # psql writes the failing statement and PostgreSQL's message; both are
            # non-secret catalog/SQL text. Only the first line is surfaced.
            message = done.stderr.decode("utf-8", "replace").strip().splitlines()
            raise DockerError(
                "psql_failed",
                "the local maintenance SQL was refused",
                (message[0] if message else "")[:200],
            )
        return done.stdout.decode("utf-8", "replace")

    def compose_migrate(
        self, compose_file: Path, authorization: bytes, *, run_id: str
    ) -> subprocess.CompletedProcess:
        """Run the migrate one-shot with the authorization on stdin, not on argv.

        F10: the container is named from the run id and is deliberately NOT removed with
        `--rm`. A timed-out client can therefore leave observable lifecycle evidence, and
        the reconciler can establish whether THIS run's worker terminated. The evidence is
        retained until the outcome is durably recorded; the host then removes the exact
        container (never `-f`, never a prune) only when its identity and terminal state
        are verified.
        """
        environment = dict(os.environ)
        environment["HOUSE_HELPER_PG_ACTIVATE"] = "1"
        environment["HOUSE_HELPER_PG_AUTHORIZATION_STDIN"] = "1"
        try:
            return self.runner(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "--profile",
                    "ops",
                    "run",
                    "-T",
                    "--name",
                    migrate_container_name(self.spec, run_id),
                    "-e",
                    "HOUSE_HELPER_PG_AUTHORIZATION_STDIN=1",
                    "migrate",
                ],
                input=authorization,
                capture_output=True,
                timeout=900,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # The client timed out; the container may still be running. Never journal
            # this as "not started".
            raise DockerUnknown(
                "migrate_timeout",
                "the migrate client timed out; the worker outcome is unknown",
            ) from None
        except OSError:
            # Popen itself failed: the worker was not launched.
            raise DockerLaunchError(
                "docker_unavailable", "the migrate one-shot could not be started"
            ) from None

    def compose_schema(self, compose_file: Path, authorization: bytes) -> subprocess.CompletedProcess:
        """Run the enrollment schema step through the accepted `migrate` service.

        `docker compose run` replaces the service's `migrate` command with `schema`, so
        the accepted WP-2B topology gains no service and no mount. The bounded
        schema-only authorization travels on stdin exactly like the Mode B record.
        """
        environment = dict(os.environ)
        environment["HOUSE_HELPER_PG_ACTIVATE"] = "1"
        environment["HOUSE_HELPER_PG_AUTHORIZATION_STDIN"] = "1"
        try:
            return self.runner(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose_file),
                    "--profile",
                    "ops",
                    "run",
                    "--rm",
                    "-T",
                    "-e",
                    "HOUSE_HELPER_PG_AUTHORIZATION_STDIN=1",
                    "migrate",
                    "schema",
                ],
                input=authorization,
                capture_output=True,
                timeout=300,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise DockerUnknown(
                "schema_timeout",
                "the enrollment schema client timed out; the schema step's outcome is unknown",
            ) from None
        except OSError:
            raise DockerLaunchError(
                "docker_unavailable", "the enrollment schema one-shot could not be started"
            ) from None

    def worker_containers(self) -> tuple[tuple[str, str, str, str, str], ...]:
        """Every container the daemon reports, with its Compose identity (F10).

        This is an observation, not a decision: the listing deliberately does NOT filter
        on the expected project/service/name, because the resolver - not the CLI adapter -
        must establish that an observed container is THIS run's `migrate` worker. The
        client process is never consulted: a timed-out client says nothing about the
        container it may have launched (LCK-12).
        """
        done = self._run(
            [
                "docker",
                "ps",
                "-a",
                "--no-trunc",
                "--format",
                "{{.ID}}\t{{.State}}\t{{.Names}}\t"
                '{{.Label "com.docker.compose.project"}}\t'
                '{{.Label "com.docker.compose.service"}}',
            ]
        )
        if done.returncode != 0:
            raise DockerError("worker_observation", "the one-shot container listing failed")
        rows: list[tuple[str, str, str, str, str]] = []
        for line in done.stdout.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 5 or not parts[0].strip() or not parts[2].strip():
                raise DockerError(
                    "worker_observation", "a container lifecycle row is malformed"
                )
            rows.append(
                (
                    parts[0].strip(),
                    parts[1].strip(),
                    parts[2].strip(),
                    parts[3].strip(),
                    parts[4].strip(),
                )
            )
        return tuple(rows)

    def inspect_worker(self, container_id: str) -> Mapping[str, Any]:
        """The exact observed identity/lifecycle of one worker container."""
        if not _ID.fullmatch(container_id):
            raise DockerError("worker_identity", "the worker container id is malformed")
        value = self._json(
            ["docker", "inspect", "--type", "container", container_id]
        )
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise DockerError(
                "worker_identity", "the worker inspection is not one object"
            )
        return value[0]

    def worker_evidence(self, run_id: str) -> enrollment.WorkerEvidence:
        """Evidence bound to `run_id`; every field is observed, not assumed (F10).

        The listing supplies candidate containers. Only a container whose name is the
        expected deterministic one is inspected; its id, name, Compose project/service,
        lifecycle state and exit code are then read back from `docker inspect`. Evidence
        from another run, service or project is never manufactured here.
        """
        try:
            expected = migrate_container_name(self.spec, run_id)
        except enrollment.EnrollmentError:
            # No valid pending run id: return unbound evidence and let the resolver report
            # `migration_not_pending` (or `worker:unbound`) instead of a Docker error.
            return enrollment.WorkerEvidence(
                run_id=run_id if isinstance(run_id, str) else "",
                project=self.spec.project,
                service=enrollment.MIGRATE_SERVICE,
            )
        observed: list[enrollment.WorkerContainer] = []
        for container_id, state, name, project, service in self.worker_containers():
            if name != expected:
                continue
            container = self.inspect_worker(container_id)
            state_value = container.get("State") or {}
            labels = (container.get("Config") or {}).get("Labels") or {}
            exit_code = state_value.get("ExitCode")
            observed.append(
                enrollment.WorkerContainer(
                    container_id=str(container.get("Id", "")),
                    name=str(container.get("Name", "")).lstrip("/"),
                    state=str(state_value.get("Status", state)),
                    project=str(
                        labels.get(
                            "com.docker.compose.project", project
                        )
                    ),
                    service=str(
                        labels.get(
                            "com.docker.compose.service", service
                        )
                    ),
                    exit_code=(
                        exit_code
                        if isinstance(exit_code, int)
                        and not isinstance(exit_code, bool)
                        else None
                    ),
                )
            )
        return enrollment.WorkerEvidence(
            run_id=run_id,
            container_name=expected,
            project=self.spec.project,
            service=enrollment.MIGRATE_SERVICE,
            observed=tuple(observed),
        )

    def worker_name_conflict(self, run_id: str) -> bool:
        """Whether any container already holds THIS run's deterministic name.

        A retained container can never be silently reused as the new worker: the caller
        refuses the launch and journals `not_started` before Compose is invoked.
        """
        expected = migrate_container_name(self.spec, run_id)
        return any(name == expected for _id, _state, name, _p, _s in self.worker_containers())

    def remove_worker(self, *, run_id: str, container_id: str) -> None:
        """The smallest safe cleanup of a confirmed terminal worker (F10).

        Re-inspect the exact container immediately before removal. Only the expected
        deterministic name, the frozen Compose project/service, the exact full id and a
        terminal `exited`/`dead` lifecycle are accepted. A running, unknown, unrecognized
        or identity-mismatched container is never removed. No `-f`, no global cleanup, no
        prune and no `--remove-orphans` is used: exactly `docker rm <container-id>`.
        """
        expected = migrate_container_name(self.spec, run_id)
        container = self.inspect_worker(container_id)
        state = container.get("State") or {}
        labels = (container.get("Config") or {}).get("Labels") or {}
        if str(container.get("Id", "")) != container_id:
            raise DockerError("worker_cleanup", "the observed worker id is not the supplied one")
        if str(container.get("Name", "")) != "/" + expected:
            raise DockerError("worker_cleanup", "the observed worker is not this run's container")
        if (
            labels.get("com.docker.compose.project") != self.spec.project
            or labels.get("com.docker.compose.service") != enrollment.MIGRATE_SERVICE
        ):
            raise DockerError("worker_cleanup", "the observed worker is not the migrate service")
        if str(state.get("Status", "")) not in ("exited", "dead") or _boolean(
            state.get("Running", False)
        ):
            raise DockerError("worker_cleanup", "the observed worker is not terminated")
        done = self._run(["docker", "rm", container_id])
        if done.returncode != 0:
            raise DockerError(
                "worker_cleanup", "the completed worker container could not be removed"
            )


# --- identity checks -------------------------------------------------------------------------

_ID = re.compile(r"[0-9a-f]{64}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_SYSTEM_IDENTIFIER = re.compile(r"[0-9]{1,20}")
_CONFIG_BIND_DESTINATIONS = frozenset(
    {"/etc/house-helper/postgresql.conf", "/etc/house-helper/pg_hba.conf"}
)
_BOOTSTRAP_CREDENTIAL_DESTINATION = "/run/credentials/postgres_superuser.password"


def migrate_container_name(spec: ClusterSpec, run_id: str) -> str:
    """The exact container name a migrate run launches (F10).

    Compose's `run` would otherwise pick an unpredictable name, so the host could not say
    whether an observed container is *this* run's worker. The run id is the same bounded
    32-hex value the authorization carries. The project and service are the frozen
    deployment facts; the resolver re-derives this name from the pending intent.
    """
    if spec.project != enrollment.MIGRATE_PROJECT:
        raise DockerError(
            "compose_identity", "the Compose project is not the recorded deployment project"
        )
    return enrollment.worker_container_name(run_id)


def _same_mount_source(observed: object, expected: str) -> bool:
    """Full canonical source equality. Never a basename comparison (F3)."""
    if not isinstance(observed, str) or not observed or not expected:
        return False
    return _canonical_path(observed) == _canonical_path(expected)


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (1, "t", "true", "True"):
        return True
    if value in (0, "f", "false", "False"):
        return False
    raise DockerError("boolean", "a Docker/catalog boolean was not a boolean")


def _rows(output: str) -> list[list[str]]:
    return [line.split("\x1f") for line in output.splitlines() if line]


def validate_container(spec: ClusterSpec, container: Mapping[str, Any]) -> None:
    """The positive container identity: exact name/id/running image, PGDATA volume, network,
    no published port."""
    if str(container.get("Name")) != "/" + spec.container_name:
        raise DockerError("container_name", "the container is not the recorded one")
    if not _ID.fullmatch(str(container.get("Id", ""))):
        raise DockerError("container_id", "the container id is malformed")
    state = container.get("State") or {}
    if _boolean(state.get("Running", False)) is not True:
        raise DockerError("container_state", "the PostgreSQL container is not running")
    config = container.get("Config") or {}
    if config.get("Image") != spec.image:
        raise DockerError("container_image", "the container was created from another image")
    # Docker reports a container's image ID as `sha256:<64 hex>` in `.Image`. It is an
    # identity, not the pinned `postgres@sha256:<digest>` reference; the two are compared
    # separately (container Config.Image / image RepoDigests vs. this field).
    if not _IMAGE_ID.fullmatch(str(container.get("Image", ""))):
        raise DockerError("container_image_id", "the container image id is malformed")
    labels = config.get("Labels") or {}
    if labels.get("com.docker.compose.project") != spec.project:
        raise DockerError("container_project", "the container is not this Compose project's")
    if labels.get("com.docker.compose.service") != spec.container_name:
        raise DockerError("container_service", "the container is not the database service")

    mounts = container.get("Mounts") or []
    data_mounts = [mount for mount in mounts if mount.get("Destination") == "/var/lib/postgresql/data"]
    if len(data_mounts) != 1:
        raise DockerError("volume_identity", "PGDATA is not exactly one mount")
    data = data_mounts[0]
    if data.get("Type") != "volume" or data.get("Name") != spec.volume:
        raise DockerError("volume_identity", "PGDATA is not the recorded named volume")
    if _boolean(data.get("RW", False)) is not True:
        raise DockerError("volume_identity", "the PGDATA volume is not writable")
    # The only other mounts the accepted WP-2B surface allows are the two read-only
    # configuration binds and the exact read-only bootstrap credential mount
    # (`POSTGRES_PASSWORD_FILE`), which exists only for the documented first-init /
    # pre-retirement lifecycle in deploy/postgres/credentials/README.md. Each source is
    # compared in full with the source the recorded Compose configuration authorizes
    # (F3): a file with the right basename from another directory is a substitution, not
    # the recorded container. Anything else - the SQLite volume, another database, a host
    # path - is refused too.
    authorized_sources = {
        "/etc/house-helper/postgresql.conf": spec.postgresql_conf_source,
        "/etc/house-helper/pg_hba.conf": spec.pg_hba_source,
    }
    bootstrap_mounts = 0
    for mount in mounts:
        if mount is data:
            continue
        destination = mount.get("Destination")
        read_only = _boolean(mount.get("RW", False)) is False
        source = mount.get("Source")
        if (
            mount.get("Type") == "bind"
            and destination in authorized_sources
            and read_only
            and _same_mount_source(source, authorized_sources[destination])
        ):
            continue
        if (
            mount.get("Type") == "bind"
            and destination == _BOOTSTRAP_CREDENTIAL_DESTINATION
            and read_only
            and _same_mount_source(source, spec.bootstrap_credential_source)
        ):
            bootstrap_mounts += 1
            continue
        raise DockerError("volume_identity", "the container carries an unexpected mount")
    if bootstrap_mounts > 1:
        raise DockerError("volume_identity", "the bootstrap credential is mounted twice")

    ports = (container.get("NetworkSettings") or {}).get("Ports") or {}
    if any(bindings for bindings in ports.values()):
        raise DockerError("published_port", "the PostgreSQL container publishes a port")
    host_ports = (container.get("HostConfig") or {}).get("PortBindings") or {}
    if any(bindings for bindings in host_ports.values()):
        raise DockerError("published_port", "the PostgreSQL container binds a host port")
    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    if spec.network not in networks:
        raise DockerError("network_identity", "the container is not on the recorded database network")


def validate_volume(spec: ClusterSpec, volume: Mapping[str, Any], attached: Sequence[str], container_id: str) -> None:
    if volume.get("Name") != spec.volume:
        raise DockerError("volume_identity", "the inspected volume is not the recorded one")
    if volume.get("Driver") != "local" or volume.get("Scope") != "local":
        raise DockerError("volume_identity", "the PGDATA volume is not a local volume")
    if set(attached) != {container_id}:
        raise DockerError(
            "volume_attachment", "the PGDATA volume is attached to another container"
        )


def validate_image(spec: ClusterSpec, image: Mapping[str, Any]) -> None:
    """The image ID and the pinned digest are distinct facts, both validated.

    `Id` is the local content ID Docker assigns (`sha256:<64 hex>`). The pinned digest is
    the `postgres@sha256:<64 hex>` reference recorded in Compose; it must be one of the
    image's `RepoDigests`. A bare 64-hex `Id` is not valid Docker inspect output.
    """
    if not _IMAGE_ID.fullmatch(str(image.get("Id", ""))):
        raise DockerError("image_identity", "the PostgreSQL image id is malformed")
    digests = image.get("RepoDigests") or []
    if not isinstance(digests, list) or spec.image not in digests:
        raise DockerError("image_identity", "the PostgreSQL image is not the pinned digest")


class ClusterAdapter:
    """The production `Cluster` implementation for `pg_enrollment.ModeA`."""

    def __init__(self, docker: Docker, *, compose_file: Path = DEFAULT_COMPOSE) -> None:
        self.docker = docker
        self.compose_file = compose_file

    def identity(self) -> enrollment.ClusterIdentity:
        container = self.docker.inspect_container()
        validate_container(self.docker.spec, container)
        volume = self.docker.inspect_volume()
        validate_volume(
            self.docker.spec,
            volume,
            self.docker.attached_containers(),
            str(container["Id"]),
        )
        image = self.docker.inspect_image()
        validate_image(self.docker.spec, image)
        if str(container.get("Image")) != str(image.get("Id")):
            raise DockerError(
                "image_identity", "the container image id is not the inspected image"
            )
        rows = _rows(self.docker.psql("postgres", contract.SYSTEM_IDENTIFIER_SQL))
        if len(rows) != 1 or len(rows[0]) != 1 or not _SYSTEM_IDENTIFIER.fullmatch(rows[0][0]):
            raise DockerError("system_identifier", "the cluster identifier could not be read")
        return enrollment.ClusterIdentity(
            container_name=self.docker.spec.container_name,
            container_id=str(container["Id"]),
            image_digest=self.docker.spec.image,
            volume=self.docker.spec.volume,
            system_identifier=rows[0][0],
        )

    def observe(self, *, read_target: bool = False) -> enrollment.Observation:
        identity = self.identity()
        databases = tuple(
            row[0] for row in _rows(
                self.docker.psql("postgres", "SELECT d.datname FROM pg_catalog.pg_database d ORDER BY d.datname")
            )
        )
        roles = contract.role_facts(
            _rows(self.docker.psql("postgres", contract.ROLES_SQL)),
            _rows(self.docker.psql("postgres", contract.ROLE_SETTINGS_SQL)),
        )
        memberships = contract.membership_edges(
            _rows(self.docker.psql("postgres", contract.MEMBERSHIPS_SQL))
        )
        predefined = contract.observed_predefined_roles(roles)
        db_rows = _rows(self.docker.psql("postgres", contract.DATABASES_SQL))
        db_row = next(
            (row for row in db_rows if row and row[0] == contract.SERVING_DATABASE), None
        )
        marker = str(db_row[9]) if db_row is not None else ""
        facts = None
        inventory = None
        effective_membership: tuple[tuple[str, str, str, bool], ...] = ()
        schema_versions: tuple[str, ...] = ()
        public_tables: tuple[str, ...] = ()
        default_acls: tuple[tuple[str, str, str, str, str, bool], ...] = ()
        schema_acl: Optional[tuple[Any, ...]] = None
        if db_row is not None:
            allowconn = _boolean_value(db_row[1])
            acl_rows = _rows(self.docker.psql("postgres", contract.DATABASE_ACLS_SQL))
            privilege = _rows(
                self.docker.psql("postgres", contract.has_database_privilege_sql(contract.SERVING_DATABASE))
            )
            connect = {
                role: _boolean_value(privilege[0][index])
                for index, role in enumerate(contract.PROJECT_ROLE_NAMES)
            }
            temporary = {
                role: _boolean_value(privilege[0][len(contract.PROJECT_ROLE_NAMES) + index])
                for index, role in enumerate(contract.PROJECT_ROLE_NAMES)
            }
            sessions = tuple(
                (int(row[1]), str(row[2]))
                for row in _rows(self.docker.psql("postgres", contract.SESSIONS_SQL))
                if row[0] == contract.SERVING_DATABASE and row[3] == "client backend"
            )
            subscriptions = tuple(
                (int(row[0]), str(row[1]), int(row[2]))
                for row in _rows(self.docker.psql("postgres", contract.SUBSCRIPTIONS_SQL))
            )
            if read_target and allowconn:
                inventory = contract.inventory_from_rows(
                    _rows(self.docker.psql(contract.SERVING_DATABASE, contract.INVENTORY_SQL))
                )
                effective_membership = tuple(
                    (str(login), str(role), str(inquiry), _boolean_value(result))
                    for login, role, inquiry, result in _rows(
                        self.docker.psql(
                            contract.SERVING_DATABASE,
                            contract.EFFECTIVE_MEMBERSHIP_SQL,
                        )
                    )
                )
                schema_versions, public_tables = self._schema_state()
                default_acls = contract.default_acl_entries(
                    _rows(
                        self.docker.psql(
                            contract.SERVING_DATABASE, contract.DEFAULT_ACLS_SQL
                        )
                    )
                )
                schema_acl_rows = _rows(
                    self.docker.psql(
                        contract.SERVING_DATABASE, contract.PUBLIC_SCHEMA_ACL_SQL
                    )
                )
                if schema_acl_rows:
                    schema_acl = tuple(schema_acl_rows[0])
            facts = contract.database_facts(
                row=db_row,
                acl_rows=acl_rows,
                connect=connect,
                temporary=temporary,
                sessions=sessions,
                subscriptions=subscriptions,
                target_inventory=inventory,
                database=contract.SERVING_DATABASE,
                default_acls=default_acls,
            )
        return enrollment.Observation(
            identity=identity,
            databases=databases,
            roles=roles,
            memberships=memberships,
            predefined_roles=predefined,
            database=facts,
            marker=marker,
            target_inventory=inventory,
            effective_membership=effective_membership,
            schema_versions=schema_versions,
            public_tables=public_tables,
            default_acls=default_acls,
            schema_acl=schema_acl,
        )

    def _schema_state(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The target's Alembic revision rows and public table names, read over the
        local peer socket exactly like every other host observation."""
        database = contract.SERVING_DATABASE
        public_tables = tuple(
            str(row[0])
            for row in _rows(self.docker.psql(database, contract.PUBLIC_TABLES_SQL))
        )
        table_exists = bool(
            _rows(self.docker.psql(database, contract.VERSIONS_TABLE_SQL))
        )
        versions = (
            tuple(
                str(row[0])
                for row in _rows(self.docker.psql(database, contract.VERSIONS_SQL))
            )
            if table_exists
            else ()
        )
        return versions, public_tables

    # -- mutations -----------------------------------------------------------------------

    def apply_roles(self) -> None:
        self.docker.psql("postgres", "BEGIN;\n" + artifacts.render_roles() + "\nCOMMIT;")

    def create_database(self) -> None:
        phases = artifacts.render_bootstrap(contract.SERVING_DATABASE, "hh_migrate")
        self.docker.psql("postgres", phases["create"] + "\n")

    def _database_row(self) -> list[str]:
        rows = _rows(
            self.docker.psql(
                "postgres",
                "SELECT d.datallowconn, d.datacl IS NOT NULL, "
                "(SELECT count(*) FROM pg_catalog.aclexplode(d.datacl) a WHERE a.grantee = 0), "
                "pg_catalog.has_database_privilege('hh_migrate', d.datname, 'CONNECT') "
                "FROM pg_catalog.pg_database d WHERE d.datname = 'house_helper'",
            )
        )
        if len(rows) != 1:
            raise DockerError("database_identity", "the serving database was not found")
        return rows[0]

    def harden_database(self) -> None:
        phases = artifacts.render_bootstrap(contract.SERVING_DATABASE, "hh_migrate")
        self.docker.psql("postgres", "BEGIN;\n" + phases["harden"] + "\nCOMMIT;")
        row = self._database_row()
        if _boolean_value(row[0]) is not False:
            raise DockerError("e3_b", "the database is accepting connections before verification")
        if int(row[2]) != 0:
            raise DockerError("e3_b", "PUBLIC still holds a database privilege")
        if _boolean_value(row[3]) is not True:
            raise DockerError("e3_b", "hh_migrate does not hold CONNECT")

    def enable_database(self) -> None:
        row = self._database_row()
        if _boolean_value(row[0]) is not False or int(row[2]) != 0:
            raise DockerError("e3_b", "the E3-b state changed before enablement")
        phases = artifacts.render_bootstrap(contract.SERVING_DATABASE, "hh_migrate")
        self.docker.psql("postgres", phases["enable"] + "\n")
        row = self._database_row()
        if _boolean_value(row[0]) is not True:
            raise DockerError("e3_c", "the database did not become connectable")
        if int(row[2]) != 0:
            raise DockerError("e3_c", "PUBLIC holds CONNECT after enablement")

    def harden_schema(self, expected: enrollment.ClusterIdentity) -> None:
        """The ENR-04 schema/default-ACL half, as an authenticated `hh_migrate` session.

        F8: `db/schema.sql` requires `session_user = hh_migrate` and
        `current_user = hh_owner`, so the host cannot run it through its bootstrap
        superuser local-peer session (ROL-03). It runs through the accepted `migrate`
        one-shot service, which already mounts exactly the `hh_migrate` passfile and the
        shared state volume. The one-shot refuses without this bounded authorization and
        executes only the frozen artifact - never Alembic, never arbitrary SQL.

        F8 binding: the authorization is issued only after a fresh identity observation
        proves the container name/id, image digest, PGDATA volume and physical
        `system_identifier` are still the enrollment intent's. The database login cannot
        read the physical identifier, so this host-side comparison is authoritative; the
        worker validates the observable half against the record.

        Dependency (reported, not created): the `hh_migrate` passfile must be populated
        for the one-shot to authenticate. OD-8 is the Owner's open decision; this tool
        generates no credential. Until it exists, Mode A stops after E3-c with the
        `database` milestone recorded, and a later run resumes exactly here.
        """
        identity = self.identity()
        observed = identity.as_journal()
        recorded = expected.as_journal()
        differing = [
            name for name in recorded if observed.get(name) != recorded.get(name)
        ]
        if differing:
            # Name the items only; never echo a value.
            raise enrollment.EnrollmentError(
                "cluster_identity",
                "the live cluster identity is not the enrollment intent's; the schema "
                "step was not authorized",
                ", ".join(sorted(differing)),
            )
        issued = _utcnow()
        value = journal.build_schema_authorization(
            cluster={
                "container_name": identity.container_name,
                "container_id": identity.container_id,
                "image_digest": identity.image_digest,
                "volume": identity.volume,
                "system_identifier": identity.system_identifier,
            },
            database=contract.SERVING_DATABASE,
            artifacts=dict(artifacts.manifest()),
            run_id=os.urandom(16).hex(),
            issued=issued,
        )
        authorization = journal.encode_schema_authorization(value)
        journal.decode_schema_authorization(authorization, now=issued)
        try:
            done = self.docker.compose_schema(self.compose_file, authorization)
        except DockerUnknown:
            raise
        if done.returncode != 0:
            message = (
                done.stderr.decode("utf-8", "replace").strip().splitlines() or [""]
            )[0]
            raise DockerError(
                "schema_refused",
                "the enrollment schema one-shot refused; the enrollment remains resumable",
                message[:200],
            )
        # The Mode A loop re-observes and verifies the committed contract independently.

    def worker_evidence(self, run_id: str) -> enrollment.WorkerEvidence:
        """The Docker lifecycle evidence the F10 reconciler requires, bound to the run."""
        return self.docker.worker_evidence(run_id)

    def write_marker(self, marker: str) -> None:
        literal = journal.marker_literal(marker)
        self.docker.psql(
            "postgres",
            f'COMMENT ON DATABASE "house_helper" IS {literal};\n',
        )
        rows = _rows(self.docker.psql("postgres", contract.marker_sql(contract.SERVING_DATABASE)))
        if len(rows) != 1 or rows[0][0] != marker:
            raise DockerError("marker", "the enrollment marker was not written")

    def mode_b_observation(self) -> enrollment.Observation:
        return self.observe(read_target=True)


def _boolean_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if str(value) in ("t", "true", "True", "1"):
        return True
    if str(value) in ("f", "false", "False", "0"):
        return False
    raise DockerError("boolean", "a catalog boolean was not t/f")


# --- Mode B and migrate ----------------------------------------------------------------------


@dataclass(frozen=True)
class ModeBReport:
    verdict: enrollment.ModeBVerdict
    authorization: Optional[bytes]
    exit_code: int


def verify_mode_b(
    journal_file: journal.Journal,
    adapter: ClusterAdapter,
    *,
    clock=None,
) -> ModeBReport:
    view = journal_file.read()
    observation = adapter.mode_b_observation()
    verdict = enrollment.mode_b(view.records, observation)
    if not verdict.ok:
        enrollment.record_mismatch(journal_file, verdict, clock=clock)
        return ModeBReport(verdict=verdict, authorization=None, exit_code=EX_REFUSED)
    completed = dict(verdict.completed or {})
    cluster = dict(completed.get("container") or {})
    cluster["volume"] = completed.get("volume")
    cluster["system_identifier"] = str(completed.get("system_identifier", ""))
    value = journal.build_authorization(
        cluster=cluster,
        marker=str(completed.get("marker", "")),
        database=contract.SERVING_DATABASE,
        project_roles=list(contract.PROJECT_ROLE_NAMES),
        databases=list(observation.databases),
        predefined_roles=list(observation.predefined_roles),
        artifacts=dict(artifacts.manifest()),
        journal_seq=view.head_seq,
        journal_head=view.head,
        run_id=os.urandom(16).hex(),
        issued=(clock or _utcnow)(),
    )
    authorization = journal.encode_authorization(value)
    # Structural self-check: a completed record that cannot produce a valid bounded
    # authorization is refused here, before any one-shot is invoked.
    journal.decode_authorization(authorization)
    return ModeBReport(
        verdict=verdict,
        authorization=authorization,
        exit_code=EX_OK,
    )


def pending_handoff_refusal(records: Iterable[journal.Record]) -> Optional[str]:
    """LCK-08/LCK-13: an unresolved handoff blocks every operation that advances the machine.

    The intent is never reinterpreted here. Only `handoff` may resolve it, after positive
    termination and a complete ACL comparison, so this gate cannot deadlock reconciliation.
    """
    pending = enrollment.pending_handoff_intents(records)
    if not pending:
        return None
    if len(pending) > 1:
        return (
            "more than one handoff intent has no observed outcome; each must be resolved "
            "from its own evidence before another operation"
        )
    role = str(pending[0].payload.get("role", ""))
    return (
        f"the {role} handoff intent has no observed outcome; run "
        f"`enroll.py handoff --role {role} --approved-by <owner>` to reconcile it before "
        "another operation"
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- §7.3 CONNECT handoffs -------------------------------------------------------------------


# --- the tagged handoff operation (Findings 1 and 2) -----------------------------------------
#
# A GRANT is executed in exactly one psql session whose `application_name` is derived from
# the intent's run id. A tiny container-side wrapper records, under `/tmp`, that the client
# started, its process ids and the psql exit status. The host never treats an instantaneous
# privilege snapshot as proof that the operation is over: reconciliation requires the
# tagged session and client to be gone (Finding 1). A non-zero psql status is classified by
# its exact meaning; only `ON_ERROR_STOP` exit 3 is a definitive server refusal, and even
# that is accepted only when the complete ACL readback shows the pre-state (Finding 2).

HANDOFF_GRANT_WRAPPER_NAME = "house-helper-handoff"
HANDOFF_PROBE_NAME = "house-helper-handoff-probe"
# The client-side wait is deliberately longer than the in-container psql bound, so a host
# timeout never races the client's own termination.
_HANDOFF_CLIENT_TIMEOUT = 120
_HANDOFF_CONTAINER_TIMEOUT = 60

# The wrapper captures stdin to a private file BEFORE it starts anything. POSIX assigns
# `/dev/null` as the standard input of an asynchronous list (job control is disabled in
# this container), so the previous `psql ... <&0 &` form delivered zero bytes on the
# pinned BusyBox runtime. Reading the stream synchronously into a file, then giving psql
# that file, makes the SQL bytes deterministic and independent of shell redirection
# ordering. The SQL is the fixed CONNECT statement for an allowlisted role; it is not a
# secret, but the file is still created under `umask 077` and removed on exit.
#
# `timeout` is BusyBox: it exits 143 (128+SIGTERM), not 124, and that status says only
# that the *command* was signalled. It is never treated as proof that PostgreSQL stopped
# executing the statement; termination is decided by the tagged client/session probe.
_HANDOFF_GRANT_SCRIPT = """\
tag="$1"
d=/tmp
umask 077
printf '%s' "$$" > "$d/$tag.wpid"
printf '%s' "$$" > "$d/$tag.pid"
printf started > "$d/$tag.start"
sql="$d/$tag.sql"
cat > "$sql" || exit 99
trap 'printf stopped > "$d/$tag.stop"; rm -f "$sql"' EXIT
timeout __HANDOFF_TIMEOUT__ env PGAPPNAME="$tag" psql \\
  -X -q -v ON_ERROR_STOP=1 -v hh_handoff_tag="$tag" -d postgres -f "$sql" &
child=$!
printf '%s %s' "$$" "$child" > "$d/$tag.pid"
wait "$child"
rc=$?
printf '%s' "$rc" > "$d/$tag.rc"
exit "$rc"
""".replace("__HANDOFF_TIMEOUT__", str(_HANDOFF_CONTAINER_TIMEOUT))

# Prints the recorded completion and the liveness of the wrapper shell and psql. The tag
# arrives in the environment, so neither the probe nor its subshells can match themselves;
# a surviving wrapper/psql is matched by `kill -0`, its `comm` (a shell or psql, never the
# `timeout`/`env` supervisors) and the tag in its `/proc/<pid>/cmdline`.
_HANDOFF_PROBE_SCRIPT = """\
# The tag arrives in the environment, never in this process's argv: the probe and its
# subshells therefore cannot match their own cmdline in the fallback scan below, and a
# reused pid that now belongs to the probe cannot impersonate the operation.
tag="${HANDOFF_PROBE_TAG:?}"
d=/tmp
if [ -e "$d/$tag.start" ]; then echo started=1; else echo started=0; fi
if [ -e "$d/$tag.stop" ]; then echo stopped=1; else echo stopped=0; fi
if [ -e "$d/$tag.rc" ]; then printf 'rc='; cat "$d/$tag.rc"; printf '\\n'; else echo rc=; fi
# A live client is the wrapper shell or the psql process itself. BusyBox `timeout`
# and `env` are transport supervisors: their cmdline also carries the tag, and the
# `timeout` supervisor can outlive a completed psql by a fraction of a second. They are
# never evidence that the SQL client is still running; counting them would turn every
# successful handoff into a spurious UNKNOWN.
consider() {
  pid="$1"
  case "$pid" in ''|*[!0-9]*) return ;; esac
  [ "$pid" = "$$" ] && return
  kill -0 "$pid" 2>/dev/null || return
  comm=$(cat "/proc/$pid/comm" 2>/dev/null) || return
  case "$comm" in psql|sh|ash|dash) ;; *) return ;; esac
  grep -aqF -- "$tag" "/proc/$pid/cmdline" 2>/dev/null && alive=1
}
alive=0
if [ -e "$d/$tag.pid" ]; then
  read -r wrapper child < "$d/$tag.pid" || true
  consider "$wrapper"
  consider "$child"
fi
if [ "$alive" = 0 ] && [ -e "$d/$tag.wpid" ]; then
  read -r wrapper < "$d/$tag.wpid" || true
  consider "$wrapper"
fi
# Fallback: an orphaned psql/shell may outlive a killed wrapper. Its argv carries the
# tag (psql via `-v hh_handoff_tag`, the wrapper via `$1`), so a full /proc scan closes the
# gap. The probe's own shell carries the tag in its environment, never its argv.
if [ "$alive" = 0 ]; then
  for p in /proc/[0-9]*; do
    pid=${p#/proc/}
    [ "$pid" = "$$" ] && continue
    comm=$(cat "$p/comm" 2>/dev/null) || continue
    case "$comm" in psql|sh|ash|dash) ;; *) continue ;; esac
    cmd=$(tr '\\000' '\\n' < "$p/cmdline" 2>/dev/null) || continue
    case "$cmd" in *"$tag"*) alive=1 ;; esac
  done
fi
echo client=$alive
"""


@dataclass(frozen=True)
class GrantAttempt:
    """What the host observed of the Docker *client* only.

    Nothing here establishes that the operation terminated. `returned=True` means the
    client process exited; statuses such as 2, -15, 124 and 125 do not prove the tagged
    psql session or wrapper is gone, so the container-side probe always decides.
    `transport_error="client_error"` means `Popen` itself failed and no exec was ever
    launched; that is a confirmed launch failure, not an inferred termination.
    """

    run_id: str
    returned: bool
    returncode: Optional[int]
    transport_error: str = ""


def _database_acl_state(
    docker: Docker, *, container_id: Optional[str] = None
) -> enrollment.DatabaseAclState:
    """The complete serving-database ACL: materialization, explicit entries, effective matrix.

    This is the Finding-3 observation. It reads every database privilege for every project
    role plus every explicit `datacl` entry, so grant options, `CREATE`, `TEMPORARY`,
    PUBLIC, foreign grantees and collateral changes cannot hide. A handoff reads it only
    with the verified container id and only after termination is established.
    """
    rows = _rows(
        docker.psql(
            "postgres",
            contract.database_acl_matrix_sql(contract.SERVING_DATABASE),
            container_id=container_id,
        )
    )
    expected_columns = 1 + len(contract.DATABASE_PRIVILEGES) * len(
        contract.PROJECT_ROLE_NAMES
    )
    if len(rows) != 1 or len(rows[0]) != expected_columns:
        raise DockerError(
            "handoff_observation", "the database ACL matrix could not be read"
        )
    materialized = _boolean_value(rows[0][0])
    effective: list[tuple[str, str, bool]] = []
    offset = 1
    for privilege in contract.DATABASE_PRIVILEGES:
        for index, role in enumerate(contract.PROJECT_ROLE_NAMES):
            effective.append(
                (role, privilege, _boolean_value(rows[0][offset + index]))
            )
        offset += len(contract.PROJECT_ROLE_NAMES)
    entries: list[tuple[str, str, bool]] = []
    for row in _rows(
        docker.psql(
            "postgres", contract.DATABASE_ACLS_SQL, container_id=container_id
        )
    ):
        if len(row) != 5:
            raise DockerError("handoff_observation", "a database ACL row is malformed")
        database, _grantee_oid, grantee, privilege, grantable = row
        if str(database) != contract.SERVING_DATABASE:
            continue
        if privilege not in contract.DATABASE_PRIVILEGES:
            raise DockerError(
                "handoff_observation",
                "a database ACL row names an unknown privilege",
            )
        entries.append((str(grantee), str(privilege), _boolean_value(grantable)))
    return enrollment.DatabaseAclState(
        acl_materialized=materialized,
        entries=tuple(entries),
        effective=tuple(effective),
    )


def _acl_evidence(state: enrollment.DatabaseAclState, role: str) -> dict[str, bool]:
    return {
        "has_database_privilege": state.privilege(role, "CONNECT") is True,
        "has_temporary": state.privilege(role, "TEMPORARY") is True,
        "has_create": state.privilege(role, "CREATE") is True,
    }


def _grant_connect_transport(
    docker: Docker, role: str, run_id: str, *, container_id: str
) -> GrantAttempt:
    """Run exactly the authorized GRANT through the tagged, bounded wrapper.

    No SQL text reaches argv: the statement is the fixed literal for the allowlisted role
    and database, and it travels on stdin. The tag is passed as an argument, never
    interpolated into the shell script. The exec target is the immutable container id Mode
    B verified, never the mutable Compose name (Finding 5).
    """
    tag = enrollment.handoff_application_name(run_id)
    sql = (
        f'GRANT CONNECT ON DATABASE "{contract.SERVING_DATABASE}" TO "{role}";\n'
    )
    argv = [
        "docker",
        "exec",
        "-i",
        "-u",
        "postgres",
        docker._exec_target(container_id),
        "sh",
        "-c",
        _HANDOFF_GRANT_SCRIPT,
        HANDOFF_GRANT_WRAPPER_NAME,
        tag,
    ]
    try:
        done = docker.runner(
            argv,
            input=sql.encode("utf-8"),
            capture_output=True,
            timeout=_HANDOFF_CLIENT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return GrantAttempt(run_id, False, None, "timeout")
    except OSError:
        return GrantAttempt(run_id, False, None, "client_error")
    return GrantAttempt(run_id, True, int(done.returncode), "")


def _handoff_transport(
    docker: Docker, run_id: str, *, container_id: str
) -> enrollment.HandoffTransportEvidence:
    """The operation's completion/liveness evidence; never inferred from the ACL.

    Both halves are read inside the exact verified container: the tagged client/wrapper
    processes (the positive-absence probe) and the tagged PostgreSQL backend
    (`pg_stat_activity`). Either being alive, or the probe failing, keeps the result
    unobserved/alive and therefore UNKNOWN.
    """
    tag = enrollment.handoff_application_name(run_id)

    def unobserved() -> enrollment.HandoffTransportEvidence:
        return enrollment.HandoffTransportEvidence(run_id=run_id, observed=False)

    try:
        done = docker.runner(
            [
                "docker",
                "exec",
                "-e",
                f"HANDOFF_PROBE_TAG={tag}",
                "-u",
                "postgres",
                docker._exec_target(container_id),
                "sh",
                "-c",
                _HANDOFF_PROBE_SCRIPT,
                HANDOFF_PROBE_NAME,
            ],
            capture_output=True,
            timeout=_HANDOFF_CLIENT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return unobserved()
    if done.returncode != 0:
        return unobserved()
    values: dict[str, str] = {}
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            return unobserved()
        values[key.strip()] = value.strip()
    if not {"started", "stopped", "rc", "client"} <= set(values):
        return unobserved()
    try:
        psql_exit = int(values["rc"]) if values["rc"] else None
    except ValueError:
        return unobserved()
    try:
        rows = _rows(
            docker.psql(
                "postgres",
                contract.handoff_session_sql(tag),
                container_id=container_id,
            )
        )
        if len(rows) != 1 or len(rows[0]) != 1:
            return unobserved()
        session_alive = int(rows[0][0]) > 0
    except (DockerError, contract.ContractError, ValueError):
        return unobserved()
    return enrollment.HandoffTransportEvidence(
        run_id=run_id,
        observed=True,
        started=values["started"] == "1",
        stopped=values["stopped"] == "1",
        psql_exit=psql_exit,
        client_alive=values["client"] == "1",
        session_alive=session_alive,
    )


def grant_connect(
    docker: Docker,
    role: str,
    *,
    run_id: str,
    container_id: str,
    database: str = contract.SERVING_DATABASE,
) -> GrantAttempt:
    """Issue exactly `GRANT CONNECT ON DATABASE house_helper TO <authorized role>`.

    The role is an allowlisted constant and the database is the serving one; neither is
    interpolated from operator input. There is deliberately no path to a broad grant,
    SUPERUSER, another database or a role outside the three P0 handoffs. The GRANT runs
    only against the immutable container id Mode B verified. The returned `GrantAttempt`
    distinguishes a returned client from a timeout/interruption; it never turns the psql
    status into an outcome by itself.
    """
    if role not in enrollment.HANDOFF_ROLES:
        raise DockerError("handoff_role", "the role is not an authorized handoff role")
    if database != contract.SERVING_DATABASE:
        raise DockerError("handoff_database", "the handoff names another database")
    enrollment.handoff_application_name(run_id)
    return _grant_connect_transport(
        docker, role, run_id, container_id=container_id
    )


def _handoff_outcome(
    journal_file: journal.Journal,
    intent: Mapping[str, Any],
    *,
    outcome: str,
    operator: str,
    evidence: Mapping[str, Any],
    clock=None,
) -> journal.Record:
    return journal_file.append(
        "handoff",
        {
            "state": "outcome",
            "action": intent["action"],
            "handoff": intent["handoff"],
            "role": intent["role"],
            "database": contract.SERVING_DATABASE,
            "run_id": intent["run_id"],
            "outcome": outcome,
            "operator": operator,
            "evidence": dict(evidence),
        },
        at=(clock or _utcnow)(),
    )


_HANDOFF_MAX_ATTEMPTS = 3
_HANDOFF_RETRY = "retry"


def _resolve_pending_handoffs(
    docker: Docker,
    journal_file: journal.Journal,
    *,
    operator: str,
    container_id: str,
    clock=None,
) -> Optional[tuple[str, int]]:
    """Resolve an earlier UNKNOWN, or explain why a new transition is blocked.

    Finding 1: a new H1/H2/H3 intent is never appended while an earlier one is unresolved.
    Resolution requires BOTH positive termination of the original tagged operation and a
    complete ACL state that is exactly its recorded pre-state or its exact single-CONNECT
    post-state. The ACL is therefore read only AFTER termination is established; an
    instantaneous privilege snapshot is never enough. An intent whose recorded pre-state
    already held CONNECT never issued a GRANT, so it has no operation to terminate and is
    resolved from the exact pre-state alone.
    """
    pending = enrollment.pending_handoff_intents(journal_file.read().records)
    if not pending:
        return None
    if len(pending) > 1:
        return (
            "unknown [handoff]: more than one handoff intent has no outcome; no new "
            "handoff was attempted. Each must be resolved from its own evidence.",
            EX_UNKNOWN,
        )
    intent = pending[0]
    payload = intent.payload
    run_id = payload["run_id"]
    role = payload["role"]
    before = enrollment.DatabaseAclState.from_journal(
        payload["before"], where=f"seq {intent.seq}"
    )
    # A no-op intent (the role already held CONNECT when the intent was written) never
    # issued a GRANT, so there is no tagged operation to terminate. Its only safe
    # resolution is the exact pre-state with the fence intact; requiring termination
    # evidence for an operation that never existed would keep it UNKNOWN forever.
    noop = before.privilege(role, "CONNECT") is True
    evidence: Optional[enrollment.HandoffTransportEvidence] = None
    try:
        docker.verify_handoff_container(container_id)
        if not noop:
            evidence = _handoff_transport(docker, run_id, container_id=container_id)
            termination = enrollment.handoff_termination_failures(evidence)
            if termination:
                return (
                    f"unknown [handoff]: the {role} intent at seq {intent.seq} remains "
                    "UNKNOWN: the original operation's termination is not established ("
                    + ", ".join(termination)
                    + "); no new handoff was attempted.",
                    EX_UNKNOWN,
                )
        after = _database_acl_state(docker, container_id=container_id)
    except DockerError:
        return (
            f"unknown [handoff]: the {role} intent at seq {intent.seq} remains UNKNOWN: "
            "the live ACL or the verified container could not be observed; no new "
            "handoff was attempted.",
            EX_UNKNOWN,
        )
    decision, failures = enrollment.reconcile_acl_decision(
        before,
        after,
        role,
        records=journal_file.read().records,
    )
    if decision == "unsafe":
        return (
            f"unknown [handoff]: the {role} intent at seq {intent.seq} remains UNKNOWN: "
            "the observed ACL is neither its recorded pre-state nor its exact "
            "single-CONNECT post-state (" + ", ".join(failures) + "); no new handoff was "
            "attempted.",
            EX_UNKNOWN,
        )
    observation = {
        **_acl_evidence(after, role),
        "reconciled": True,
        "psql_exit": None if evidence is None else evidence.psql_exit,
        "transport": "not_attempted" if evidence is None else "client_gone",
    }
    if decision == "applied":
        observation["applied"] = True
    else:
        observation["privileges_unchanged"] = True
    _handoff_outcome(
        journal_file,
        payload,
        outcome="reconciled",
        operator=operator,
        evidence=observation,
        clock=clock,
    )
    return None


def _record_handoff_attempt(
    journal_file: journal.Journal,
    intent: Mapping[str, Any],
    *,
    role: str,
    before: enrollment.DatabaseAclState,
    after: enrollment.DatabaseAclState,
    attempt: Optional[GrantAttempt],
    evidence: Optional[enrollment.HandoffTransportEvidence],
    changed: bool,
    operator: str,
    clock=None,
) -> Any:
    """Classify one attempt from termination and the complete ACL, never the client alone.

    Findings 1-3 in one place: an ambiguous client outcome is decided only after the
    operation is proven terminated AND the ACL is exactly the pre-state or the expected
    post-state; a definitive psql refusal is recorded `failed` only if the readback still
    shows the pre-state; an unsafe state is never recorded as a successful reconciliation.
    """
    records = journal_file.read().records
    event = enrollment.HANDOFF_EVENTS[role]

    if not changed:
        if not enrollment.acl_state_equal(before, after):
            print(
                "unknown [handoff]: the privilege state changed during an intended no-op "
                "handoff; the intent remains pending.",
                file=sys.stderr,
            )
            return EX_UNKNOWN
        failures = enrollment.database_acl_failures(after, records=records)
        if failures:
            print(
                "unknown [handoff]: the no-op handoff state is not the ACL contract ("
                + ", ".join(failures)
                + "); the intent remains pending.",
                file=sys.stderr,
            )
            return EX_UNKNOWN
        record = _handoff_outcome(
            journal_file,
            intent,
            outcome="observed",
            operator=operator,
            evidence={
                **_acl_evidence(after, role),
                "changed": False,
                "transport": "not_attempted",
                "psql_exit": None,
            },
            clock=clock,
        )
        print(
            f"ok [handoff]: {role} already holds CONNECT on "
            f"{contract.SERVING_DATABASE}; nothing was changed "
            f"(journal seq {record.seq}, event handoff:{event})."
        )
        return EX_OK

    assert attempt is not None

    # A confirmed launch failure is not a client return and not an inferred termination:
    # `Popen` itself failed, so no exec was ever sent. It may be resolved only when the
    # complete ACL is still the recorded pre-state and the fence is intact.
    if attempt.transport_error == "client_error":
        if enrollment.acl_state_equal(before, after) and not enrollment.database_acl_failures(
            after, records=records
        ):
            _handoff_outcome(
                journal_file,
                intent,
                outcome="failed",
                operator=operator,
                evidence={
                    "code": "client_not_started",
                    "psql_exit": None,
                    "privileges_unchanged": True,
                },
                clock=clock,
            )
            print(
                "refused [handoff]: the GRANT client could not be launched, so no GRANT "
                "was sent; the intent is resolved as failed and the readback shows the "
                "privilege unchanged.",
                file=sys.stderr,
            )
            return EX_REFUSED
        print(
            "unknown [handoff]: the GRANT client could not be launched but the readback "
            "is not the recorded pre-state; the intent remains pending.",
            file=sys.stderr,
        )
        return EX_UNKNOWN

    # Termination is established ONLY by the container-side evidence, never by the Docker
    # client's return status (2, -15, 124, 125, ...). The caller has already read the ACL
    # after this gate; re-checking here keeps the classification self-contained.
    assert evidence is not None
    termination_failures = enrollment.handoff_termination_failures(evidence)
    if termination_failures:
        print(
            "unknown [handoff]: the GRANT operation's termination is not established ("
            + ", ".join(termination_failures)
            + "); the intent remains pending. Run the same command again to reconcile.",
            file=sys.stderr,
        )
        return EX_UNKNOWN
    psql_exit = evidence.psql_exit
    transport = "client_returned" if attempt.returned else "client_gone"

    # A definitive server refusal needs the container-side completion status: the exit-3
    # meaning comes from psql's ON_ERROR_STOP, not from a generic client failure. BusyBox
    # `timeout` exits 143, which is ambiguous and is handled by the ACL comparison below.
    if psql_exit == enrollment.GRANT_SERVER_REFUSED:
        if enrollment.acl_state_equal(before, after) and not enrollment.database_acl_failures(
            after, records=records
        ):
            _handoff_outcome(
                journal_file,
                intent,
                outcome="failed",
                operator=operator,
                evidence={
                    "code": "server_refused",
                    "psql_exit": psql_exit,
                    "privileges_unchanged": True,
                },
                clock=clock,
            )
            print(
                "refused [handoff]: PostgreSQL refused the CONNECT grant; the intent is "
                "resolved as failed and the readback shows the privilege unchanged.",
                file=sys.stderr,
            )
            return EX_REFUSED
        print(
            "unknown [handoff]: the GRANT client reported a server refusal but the readback "
            "is not the recorded pre-state; the intent remains pending.",
            file=sys.stderr,
        )
        return EX_UNKNOWN

    if psql_exit == enrollment.GRANT_SUCCEEDED:
        failures = enrollment.acl_transition_failures(
            before, after, role, records=records
        )
        if not failures:
            record = _handoff_outcome(
                journal_file,
                intent,
                outcome="observed",
                operator=operator,
                evidence={
                    **_acl_evidence(after, role),
                    "changed": True,
                    "psql_exit": psql_exit,
                    "transport": transport,
                },
                clock=clock,
            )
            print(
                f"ok [handoff]: granted CONNECT on {contract.SERVING_DATABASE} to {role} "
                f"(journal seq {record.seq}, event handoff:{event})."
            )
            return EX_OK
        _handoff_outcome(
            journal_file,
            intent,
            outcome="failed",
            operator=operator,
            evidence={"items": list(failures), "psql_exit": psql_exit},
            clock=clock,
        )
        print(
            "refused [handoff]: the observed privilege state is not the handoff contract ("
            + ", ".join(failures)
            + "); the intent is resolved as failed.",
            file=sys.stderr,
        )
        return EX_REFUSED

    # An ambiguous psql/transport status. The operation is terminated (above); only the
    # complete ACL comparison may decide whether the GRANT committed.
    decision, failures = enrollment.reconcile_acl_decision(
        before, after, role, records=records
    )
    if decision == "unsafe":
        print(
            "unknown [handoff]: the operation terminated but the observed ACL is neither its "
            "recorded pre-state nor its exact single-CONNECT post-state ("
            + ", ".join(failures)
            + "); the intent remains pending.",
            file=sys.stderr,
        )
        return EX_UNKNOWN
    observation = {
        **_acl_evidence(after, role),
        "reconciled": True,
        "psql_exit": psql_exit,
        "transport": transport,
    }
    if decision == "applied":
        observation["applied"] = True
        record = _handoff_outcome(
            journal_file,
            intent,
            outcome="reconciled",
            operator=operator,
            evidence=observation,
            clock=clock,
        )
        print(
            f"ok [handoff]: the GRANT committed despite the client outcome; reconciled from "
            f"the complete ACL (journal seq {record.seq}, event handoff:{event})."
        )
        return EX_OK
    observation["privileges_unchanged"] = True
    _handoff_outcome(
        journal_file,
        intent,
        outcome="reconciled",
        operator=operator,
        evidence=observation,
        clock=clock,
    )
    print(
        "ok [handoff]: the GRANT client failed without applying the change; the intent was "
        "reconciled as unchanged. Retrying the handoff.",
        file=sys.stderr,
    )
    return _HANDOFF_RETRY


def run_handoff(
    docker: Docker,
    journal_file: journal.Journal,
    adapter: ClusterAdapter,
    *,
    role: str,
    approved_by: str,
    clock=None,
) -> int:
    """Journaled H1/H2/H3 CONNECT handoff (ROL-16, HOF-01/HOF-02, LCK-07/LCK-08).

    One host operation, under `operation.lock`: Mode B identity+marker+inventory
    verification, a schema-at-head gate, resolution of every earlier UNKNOWN from positive
    termination plus the complete ACL, then one intent, at most one GRANT, and one outcome.

    A GRANT whose client outcome is ambiguous is never recorded `failed`. If the operation
    has terminated, the complete ACL decides between "applied" and "unchanged"; otherwise
    the intent stays pending and every later H1/H2/H3 transition is blocked until evidence
    establishes termination (Findings 1 and 2). Every ACL check is total (Finding 3).
    """
    if role not in enrollment.HANDOFF_ROLES:
        raise enrollment.EnrollmentError(
            "handoff_role", "the role is not an authorized handoff role"
        )
    with journal_file.operation_lock():
        view = journal_file.read()
        if enrollment.pending_migration_unknown(view.records) is not None:
            raise enrollment.EnrollmentError(
                "migration_unknown",
                "an earlier migrate run has no observed outcome; run "
                "`enroll.py reconcile-migration` before a handoff",
            )
        verdict = enrollment.mode_b(view.records, adapter.mode_b_observation())
        if not verdict.ok:
            enrollment.record_mismatch(journal_file, verdict, clock=clock)
            print(
                "refused [cluster_identity]: Mode B found a mismatch: "
                + ", ".join(verdict.mismatches),
                file=sys.stderr,
            )
            return EX_REFUSED
        observation = verdict.observed
        # Finding 5: every mutation, probe and readback of this handoff is bound to the
        # exact immutable container id Mode B verified. The name is never an exec target.
        container_id = observation.identity.container_id
        try:
            docker.verify_handoff_container(container_id)
        except DockerError:
            print(
                "refused [container_identity]: the verified PostgreSQL container could not "
                "be re-established; CONNECT was not granted.",
                file=sys.stderr,
            )
            return EX_REFUSED
        state, schema_failures = contract.classify_schema_state(
            versions=observation.schema_versions,
            public_tables=observation.public_tables,
            schema_contract_present=bool(observation.default_acls),
        )
        if state != contract.SCHEMA_HEAD or schema_failures:
            print(
                "refused [schema_not_head]: the enrolled database is not initialized at "
                "this image's head; CONNECT was not granted.",
                file=sys.stderr,
            )
            return EX_REFUSED

        for _attempt_number in range(_HANDOFF_MAX_ATTEMPTS):
            blocked = _resolve_pending_handoffs(
                docker,
                journal_file,
                operator=approved_by,
                container_id=container_id,
                clock=clock,
            )
            if blocked is not None:
                message, code = blocked
                print(message, file=sys.stderr)
                return code

            records = journal_file.read().records
            try:
                before = _database_acl_state(docker, container_id=container_id)
            except DockerError:
                print(
                    "unknown [handoff]: the live ACL could not be observed; nothing was "
                    "changed and no intent was appended.",
                    file=sys.stderr,
                )
                return EX_UNKNOWN

            if before.privilege(role, "CONNECT") is True and role not in (
                enrollment.successful_handoff_roles(records)
            ):
                print(
                    f"refused [handoff_unrecorded]: {role} already holds CONNECT on "
                    f"{contract.SERVING_DATABASE} without a journaled handoff; this change "
                    "was not made by this tool. Revoke it and run the handoff again.",
                    file=sys.stderr,
                )
                return EX_REFUSED
            failures = enrollment.database_acl_failures(before, records=records)
            if failures:
                print(
                    "refused [handoff_privilege]: the database privilege fence is not in the "
                    "handoff state (" + ", ".join(failures) + "); nothing was changed.",
                    file=sys.stderr,
                )
                return EX_REFUSED

            run_id = os.urandom(16).hex()
            intent = {
                "state": "intent",
                "action": f"grant_connect:{role}",
                "handoff": enrollment.HANDOFF_EVENTS[role],
                "role": role,
                "database": contract.SERVING_DATABASE,
                "operator": approved_by,
                "run_id": run_id,
                "application_name": enrollment.handoff_application_name(run_id),
                "before": before.as_journal(),
            }
            journal_file.append("handoff", intent, at=(clock or _utcnow)())
            changed = before.privilege(role, "CONNECT") is not True
            attempt: Optional[GrantAttempt] = None
            evidence: Optional[enrollment.HandoffTransportEvidence] = None
            try:
                if changed:
                    # Re-verify immediately before the mutation: a replacement container
                    # with the same name is refused, never silently exercised (Finding 5).
                    docker.verify_handoff_container(container_id)
                    attempt = grant_connect(
                        docker, role, run_id=run_id, container_id=container_id
                    )
            except KeyboardInterrupt:
                print(
                    "unknown [handoff]: interrupted while the GRANT client was running; the "
                    "intent remains pending. Run the same command again to reconcile.",
                    file=sys.stderr,
                )
                return EX_UNKNOWN
            except DockerError:
                print(
                    "unknown [handoff]: the verified container's identity changed before the "
                    "GRANT; the intent remains pending and no GRANT was sent.",
                    file=sys.stderr,
                )
                return EX_UNKNOWN
            try:
                if changed:
                    # Finding 3: establish termination from the tagged client/session BEFORE
                    # the ACL snapshot. A GRANT committing in this window then appears in the
                    # readback instead of being masked by a stale pre-state.
                    evidence = _handoff_transport(
                        docker, run_id, container_id=container_id
                    )
                    if attempt is not None and attempt.transport_error == "client_error":
                        # A Popen failure is a *candidate* launch failure, not proof. Only
                        # positive container-side evidence that the wrapper never started
                        # and no tagged client/session exists may resolve it; an OSError
                        # raised after a launch must stay UNKNOWN.
                        never_started = (
                            evidence.observed
                            and not evidence.started
                            and not evidence.client_alive
                            and not evidence.session_alive
                        )
                        if not never_started:
                            print(
                                "unknown [handoff]: the GRANT client failed but the container "
                                "does not prove that no operation started; the intent remains "
                                "pending. Run the same command again to reconcile.",
                                file=sys.stderr,
                            )
                            return EX_UNKNOWN
                    else:
                        termination = enrollment.handoff_termination_failures(evidence)
                        if termination:
                            print(
                                "unknown [handoff]: the GRANT operation's termination is not "
                                "established (" + ", ".join(termination) + "); the intent "
                                "remains pending. Run the same command again to reconcile.",
                                file=sys.stderr,
                            )
                            return EX_UNKNOWN
                    # The readback must still come from the verified container.
                    docker.verify_handoff_container(container_id)
                after = _database_acl_state(docker, container_id=container_id)
            except KeyboardInterrupt:
                print(
                    "unknown [handoff]: interrupted while the handoff outcome was being "
                    "observed; the intent remains pending. Run the same command again to "
                    "reconcile.",
                    file=sys.stderr,
                )
                return EX_UNKNOWN
            except DockerError:
                print(
                    "unknown [handoff]: the live ACL could not be observed after the handoff "
                    "attempt; the intent remains pending. Run the same command again to "
                    "reconcile.",
                    file=sys.stderr,
                )
                return EX_UNKNOWN
            result = _record_handoff_attempt(
                journal_file,
                intent,
                role=role,
                before=before,
                after=after,
                attempt=attempt,
                evidence=evidence,
                changed=changed,
                operator=approved_by,
                clock=clock,
            )
            if result == _HANDOFF_RETRY:
                continue
            return result
        print(
            "refused [handoff]: the handoff did not apply after "
            f"{_HANDOFF_MAX_ATTEMPTS} attempts; each attempt's ambiguous outcome was "
            "reconciled from the complete ACL and every intent has an outcome. Run the "
            "command again to retry.",
            file=sys.stderr,
        )
        return EX_REFUSED


def _approved_by(value: object) -> Optional[str]:
    """The journaled operator label: non-empty, bounded, printable text.

    ASCII control characters (including the C1 range, which holds NEL) and the Unicode
    line separators are refused: the label is one JSON string, and it must stay one
    journal line even if a future reader splits lines differently.
    """
    text = str(value or "").strip()
    if not text or len(text) > 200:
        return None
    if any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or ord(character) in (0x2028, 0x2029)
        for character in text
    ):
        return None
    return text


def _handoff_command(args) -> int:
    """Validate the handoff request BEFORE anything touches Docker or the journal."""
    if args.role not in enrollment.HANDOFF_ROLES:
        print(
            "refused [handoff_role]: the handoff supports only "
            + ", ".join(enrollment.HANDOFF_ROLES)
            + "; nothing was changed.",
            file=sys.stderr,
        )
        return EX_REFUSED
    approved_by = _approved_by(args.approved_by)
    if approved_by is None:
        print(
            "usage [handoff_authorization]: --approved-by must be a non-empty, printable "
            "label of at most 200 characters; nothing was changed.",
            file=sys.stderr,
        )
        return EX_USAGE
    journal_file = _journal_file(args)
    spec = load_spec(args.compose_file)
    docker = Docker(spec)
    adapter = ClusterAdapter(docker, compose_file=args.compose_file)
    return run_handoff(
        docker,
        journal_file,
        adapter,
        role=args.role,
        approved_by=approved_by,
    )


def cleanup_worker(docker: Docker, run_id: str) -> None:
    """Remove a confirmed terminal worker, or retain it (F10).

    Cleanup runs only AFTER the migrate outcome has been durably journaled (or the
    reconciliation record appended), never before. It re-observes the exact container and
    accepts only the expected deterministic name, the frozen Compose project/service, a
    full container id and an exited/dead lifecycle. A running, UNKNOWN, unrecognized or
    identity-mismatched container is retained. A cleanup failure is reported as an
    operational warning: the migration outcome is already known and is never reclassified
    as UNKNOWN because a container could not be deleted.
    """
    try:
        evidence = docker.worker_evidence(run_id)
        failures = enrollment.worker_termination_failures(evidence, run_id=run_id)
        if failures:
            print(
                "warning [migrate-cleanup]: the worker container was retained ("
                + ", ".join(failures)[:200]
                + ").",
                file=sys.stderr,
            )
            return
        identifiers = {
            entry.container_id
            for entry in evidence.observed
            if entry.name == evidence.container_name
        }
        if len(identifiers) != 1:
            print(
                "warning [migrate-cleanup]: the worker container could not be identified "
                "uniquely; it was retained.",
                file=sys.stderr,
            )
            return
        docker.remove_worker(run_id=run_id, container_id=identifiers.pop())
    except DockerError as error:
        print(
            f"warning [migrate-cleanup]: {error.message}; the worker container was retained.",
            file=sys.stderr,
        )
    except Exception as error:  # never turn an operational failure into UNKNOWN
        print(
            "warning [migrate-cleanup]: the worker container was retained "
            f"({type(error).__name__}).",
            file=sys.stderr,
        )


def run_migrate(
    docker: Docker,
    compose_file: Path,
    journal_file: journal.Journal,
    adapter: ClusterAdapter,
    *,
    clock=None,
) -> int:
    """Mode B verification and the worker invocation inside ONE host operation interval.

    F1: the host operation lock is held from before Mode B until the worker is observed
    finished, so no other supported host operation can change the cluster between the
    check and the use. It is deliberately not the worker's state-volume maintenance lock:
    that non-reentrant flock stays on the worker side and is never acquired by the host.

    F10: a client timeout is never recorded as `not_started`. An intent without an
    observed outcome stays UNKNOWN (LCK-08), and a later migrate refuses until
    `reconcile-migration` observes the cluster.
    """
    with journal_file.operation_lock():
        records = journal_file.read().records
        pending = enrollment.pending_migration_unknown(records)
        if pending is not None:
            raise enrollment.EnrollmentError(
                "migration_unknown",
                "an earlier migrate run has no observed outcome; run "
                "`enroll.py reconcile-migration` before another migrate",
                f"run_id {pending.payload.get('run_id', '')}"[:80],
            )
        # LCK-08/LCK-13: an UNKNOWN handoff is reconciled before any operation advances the
        # machine. The worker cannot see the host journal, so the gate belongs here, where
        # both the journal and the operation interval are held.
        blocked = pending_handoff_refusal(records)
        if blocked is not None:
            print(
                f"unknown [handoff]: {blocked}; no migration was started.",
                file=sys.stderr,
            )
            return EX_UNKNOWN
        report = verify_mode_b(journal_file, adapter, clock=clock)
        if report.authorization is None:
            print(
                "refused [cluster_identity]: Mode B found a mismatch: "
                + ", ".join(report.verdict.mismatches),
                file=sys.stderr,
            )
            return EX_REFUSED
        run_id = json.loads(report.authorization.decode("utf-8"))["run_id"]
        from_revision = (
            report.verdict.observed.schema_versions[0]
            if len(report.verdict.observed.schema_versions) == 1
            else ""
        )
        journal_file.append(
            "migration",
            {
                "state": "intent",
                "run_id": run_id,
                "plan": "migrate",
                "from_revision": from_revision,
            },
            at=(clock or _utcnow)(),
        )
        try:
            # F10: a retained container with THIS run's deterministic name is never
            # silently reused. The launch is refused before Compose runs, and the intent
            # is resolved as a confirmed non-launch (no worker was started).
            if docker.worker_name_conflict(run_id):
                journal_file.append(
                    "migration",
                    {
                        "state": "outcome",
                        "run_id": run_id,
                        "outcome": "not_started",
                        "evidence": "worker_name_conflict",
                    },
                    at=(clock or _utcnow)(),
                )
                print(
                    "refused [migrate]: a container already holds this run's worker "
                    "name; nothing was started.",
                    file=sys.stderr,
                )
                return EX_REFUSED
            done = docker.compose_migrate(
                compose_file, report.authorization, run_id=run_id
            )
        except DockerUnknown as error:
            journal_file.append(
                "migration",
                {
                    "state": "outcome",
                    "run_id": run_id,
                    "outcome": "unknown",
                    "evidence": error.code,
                },
                at=(clock or _utcnow)(),
            )
            print(
                f"unknown [migrate]: {error.message}; run `enroll.py "
                "reconcile-migration` after confirming the worker has stopped.",
                file=sys.stderr,
            )
            return EX_UNKNOWN
        except KeyboardInterrupt:
            journal_file.append(
                "migration",
                {
                    "state": "outcome",
                    "run_id": run_id,
                    "outcome": "unknown",
                    "evidence": "client_interrupted",
                },
                at=(clock or _utcnow)(),
            )
            raise
        except DockerLaunchError:
            # Confirmed: the client never started the worker.
            journal_file.append(
                "migration",
                {
                    "state": "outcome",
                    "run_id": run_id,
                    "outcome": "not_started",
                    "evidence": "client_launch_failed",
                },
                at=(clock or _utcnow)(),
            )
            raise
        except DockerError as error:
            # The pre-launch Docker observation failed. Compose was never invoked, so the
            # intent is resolved as a confirmed non-launch, never as UNKNOWN.
            journal_file.append(
                "migration",
                {
                    "state": "outcome",
                    "run_id": run_id,
                    "outcome": "not_started",
                    "evidence": "worker_observation_failed",
                },
                at=(clock or _utcnow)(),
            )
            raise DockerLaunchError(error.code, error.message) from None
        journal_file.append(
            "migration",
            {
                "state": "outcome",
                "run_id": run_id,
                "outcome": "completed",
                "exit_code": int(done.returncode),
            },
            at=(clock or _utcnow)(),
        )
        if done.returncode != 0:
            message = (done.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[0]
            print(f"refused [migrate]: the one-shot exited {done.returncode}: {message[:200]}", file=sys.stderr)
            return EX_REFUSED
        # F1: the interval remains open through a final identity observation. A cluster
        # changed underneath the run is an identity failure, not a success.
        after = adapter.mode_b_observation()
        verdict = enrollment.mode_b(journal_file.read().records, after)
        if not verdict.ok:
            enrollment.record_mismatch(journal_file, verdict, clock=clock)
            print(
                "refused [cluster_identity]: the cluster changed during the migration: "
                + ", ".join(verdict.mismatches),
                file=sys.stderr,
            )
            return EX_REFUSED
        output = done.stdout.decode("utf-8", "replace").strip()
        if output:
            print(output)
        # The outcome is durably journaled and the final identity observation passed; only
        # now may the confirmed terminal worker be removed. Cleanup never changes EX_OK.
        cleanup_worker(docker, run_id)
        return EX_OK


# --- the command line ------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enroll.py",
        description="HH-DB-01 PostgreSQL enrollment and migration (host, root).",
        allow_abbrev=False,
    )
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument(
        "--journal-dir",
        type=Path,
        default=Path(os.environ.get("HOUSE_HELPER_PG_JOURNAL_DIR", DEFAULT_JOURNAL_DIR)),
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("status", help="print the durable enrollment state (non-secret)")
    mode_a = commands.add_parser("mode-a", help="perform or resume the one-time Mode A enrollment")
    mode_a.add_argument("--approved-by", default="", help="the Owner/operator authorization label")
    commands.add_parser("verify", help="authoritative Mode B verification (read-only)")
    handoff = commands.add_parser(
        "handoff",
        help="journaled CONNECT handoff to one authorized runtime role",
    )
    handoff.add_argument(
        "--role",
        required=True,
        help="the grantee: hh_app (H1), hh_provision (H2) or hh_backup (H3)",
    )
    handoff.add_argument(
        "--approved-by",
        required=True,
        help="the Owner/operator authorization label recorded with the intent",
    )
    commands.add_parser("migrate", help="Mode B verification followed by the migrate one-shot")
    commands.add_parser(
        "reconcile-migration",
        help="observe the cluster and resolve an UNKNOWN migrate outcome",
    )
    check = commands.add_parser("check-credentials", help="validate per-role passfiles, no values")
    check.add_argument("--credentials-dir", type=Path, default=DEFAULT_CREDENTIALS_DIR)
    commands.add_parser(
        "plan-bootstrap-retirement", help="print the bootstrap password retirement plan (no execution)"
    )
    return parser


def _journal_file(args) -> journal.Journal:
    # One operation interval: an explicit --journal-dir that names a different directory
    # than HOUSE_HELPER_PG_JOURNAL_DIR would take a different `operation.lock`, so it is
    # refused rather than silently interleaving with a differently-configured tool.
    journal.reject_conflicting_journal_directories(args.journal_dir)
    return journal.Journal(args.journal_dir, require_root=(os.name == "posix"))


def _print_status(journal_file: journal.Journal) -> int:
    view = journal_file.read()
    records = enrollment._enrollment_records(view.records)
    if not records:
        print("state E0 (no enrollment intent)")
    else:
        latest = records[-1]
        print(
            f"state {latest.payload.get('state', 'unknown')} "
            f"(seq {latest.seq}, system_identifier {latest.payload.get('system_identifier', '')})"
        )
    pending = enrollment.pending_migration_unknown(view.records)
    if pending is not None:
        print(
            "migration UNKNOWN: run `enroll.py reconcile-migration` before another "
            f"migrate (run_id {pending.payload.get('run_id', '')})"
        )
    for record in enrollment.pending_handoff_intents(view.records):
        role = record.payload.get("role", "")
        print(
            f"handoff UNKNOWN: {role} intent at seq {record.seq} has no observed outcome; "
            f"run `enroll.py handoff --role {role} --approved-by <owner>` to reconcile it"
        )
    return EX_OK


def _check_credentials(directory: Path) -> int:
    expected = {
        "hh_app": contract.SERVING_DATABASE,
        "hh_migrate": contract.SERVING_DATABASE,
        "hh_provision": contract.SERVING_DATABASE,
        "hh_purge": contract.SERVING_DATABASE,
        "hh_backup": contract.SERVING_DATABASE,
        "hh_restore": "*",
    }
    failed = False
    for login, database in expected.items():
        path = directory / f"{login}.pgpass"
        result = credentials.summary(
            path,
            login=login,
            host="house-helper-db",
            port="5432",
            database=database,
            # F11: the host validates the file against the container's runtime identity
            # (10001:10001), never against the root process reading the directory.
            owner_uid=credentials.RUNTIME_UID,
            owner_gid=credentials.RUNTIME_GID,
        )
        missing = [name for name, ok in result.items() if not ok]
        if missing:
            failed = True
            print(f"refused [{login}]: " + ", ".join(missing), file=sys.stderr)
        else:
            print(f"ok [{login}]: passfile present, private, owned and matching")
    return EX_REFUSED if failed else EX_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan-bootstrap-retirement":
            for number, step in enumerate(enrollment.bootstrap_password_retirement_plan(), 1):
                print(f"{number}. {step}")
            return EX_OK
        if args.command == "check-credentials":
            return _check_credentials(args.credentials_dir)
        if args.command == "handoff":
            return _handoff_command(args)
        journal_file = _journal_file(args)
        if args.command == "status":
            return _print_status(journal_file)
        spec = load_spec(args.compose_file)
        docker = Docker(spec)
        adapter = ClusterAdapter(docker, compose_file=args.compose_file)
        if args.command == "mode-a":
            report = enrollment.ModeA(
                journal_file, adapter, approved_by=args.approved_by
            ).run()
            print(
                f"ok [mode-a]: state {report.state}; marker {report.marker}; "
                f"system_identifier {report.system_identifier}; "
                f"{len(report.actions)} authorized steps."
            )
            return EX_OK
        if args.command == "verify":
            # Read-only: it does not take the identity-changing operation interval and
            # therefore does not create a journal on a host that has never enrolled.
            report = verify_mode_b(journal_file, adapter)
            if report.exit_code == EX_OK:
                print("ok [verify]: Mode B passed; the live cluster is the enrolled one.")
                return EX_OK
            print(
                "refused [cluster_identity]: " + ", ".join(report.verdict.mismatches),
                file=sys.stderr,
            )
            return EX_REFUSED
        if args.command == "reconcile-migration":
            with journal_file.operation_lock():
                # F10: the Docker container lifecycle of THE pending run is observed
                # inside the same host interval as the database observation. Neither the
                # client's exit nor an unchanged revision is evidence on its own.
                pending = enrollment.pending_migration_unknown(
                    journal_file.read().records
                )
                run_id = (
                    "" if pending is None else str(pending.payload.get("run_id", ""))
                )
                evidence = adapter.worker_evidence(run_id)
                record = enrollment.reconcile_migration(
                    journal_file,
                    adapter.mode_b_observation(),
                    worker=evidence,
                )
            # The reconciliation record is durable; a confirmed terminal worker may now be
            # removed with the same exact-identity checks. A cleanup failure is a warning,
            # not a reopened UNKNOWN.
            cleanup_worker(docker, run_id)
            print(
                f"ok [reconcile-migration]: run {record.payload.get('run_id', '')} "
                f"resolved as {record.payload.get('outcome', '')}."
            )
            return EX_OK
        if args.command == "migrate":
            return run_migrate(docker, args.compose_file, journal_file, adapter)
        return EX_USAGE
    except DockerError as error:
        print(f"refused [{error.code}]: {error.message}", file=sys.stderr)
        # An unavailable Docker CLI/daemon is an environment failure; every other
        # Docker-derived refusal (a container/volume/image identity mismatch) is a
        # fail-closed refusal of the operation.
        return EX_UNAVAILABLE if error.code in ("docker_unavailable", "docker_inspect") else EX_REFUSED
    except (
        enrollment.EnrollmentError,
        journal.JournalError,
        artifacts.ArtifactError,
        contract.ContractError,
        credentials.PassfileError,
    ) as error:
        print(f"refused [{error.code}]: {error.message}", file=sys.stderr)
        return EX_REFUSED
    except Exception as error:  # never echo SQL, a path or an environment value
        print(f"error [internal]: unexpected {type(error).__name__}.", file=sys.stderr)
        return EX_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
