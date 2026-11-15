"""The independent identity of the disposable PostgreSQL test cluster (HH-DB-01 R3-01, R4-02, R4-04).

The harness creates and drops roles and databases. A URL that looks local, a port, a
database name or a COMMENT is not proof that the server is a throwaway instance: a
normal installation can satisfy every one of those. This module answers the one question
the destructive suites must answer first:

    is the cluster I am about to change THIS repository's dedicated disposable
    test database, and is the SQL connection really reaching it?

The expected identity is established WITHOUT asking the connection that is about to be
authorized, and it is established from two independent facts:

  1. **The container.** There are exactly two identity modes, and they are mutually
     exclusive (R4-02):

       * **compose** - the default, on a developer machine and in any run that is not the
         GitHub Actions runner. The container of the documented Compose project
         (`compose.test-postgres.yaml`): its Compose project/service labels (and that it
         is not a one-off) are required, and PGDATA must be on the project's dedicated
         named volume.
       * **ci-service** - only when the GitHub Actions runner says so
         (`GITHUB_ACTIONS=true`) AND names the service container it created
         (`job.services.postgres.id`, passed as HH_TEST_POSTGRES_CONTAINER_ID) AND names
         the run and the attempt (`GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`). The workflow
         gives the service an explicitly provisioned, run-scoped volume
         (`hh-test-postgres-<run id>-<run attempt>`); the inspected container's data mount
         must be exactly that volume. GitHub Actions service containers are NOT Compose
         containers, so Compose labels are not required of them - but a container that
         belongs to this repository's Compose project is refused in CI mode.

     A container id in the environment alone never activates the CI contract: outside the
     runner it is refused as conflicting evidence instead of silently weakening the local
     checks. A run that cannot state its mode and its evidence completely is refused.

  2. **The cluster's persistent `system_identifier`**, read from the container through
     `pg_controldata` - i.e. from the data directory's control file, not through SQL -
     under an explicitly controlled locale, requiring a successful exit and exactly one
     well-formed identifier line (R4-04). This is the value the SQL connection must then
     report (`SELECT system_identifier::text FROM pg_control_system()`), on the bootstrap
     and on each later control connection. A connection that reports another identifier is
     a different cluster and nothing destructive runs on it.

The Docker facts are read strictly (R4-04, R5-04, R5-06): an explicit and consistent
running state, the inspected container id matching the selected one, `Config.Image` being
the digest-pinned reference (the container inspection has no `RepoDigests` field, so none
is assumed), an explicitly writable volume mount at the data directory, exactly one
loopback publication of the validated host port (a malformed extra entry is a refusal, not
something discarded), and the mode's labels/volume. The volume itself is inspected
(R5-04): its name, local driver/scope and its provenance labels are checked, and it must
be attached to no container other than the verified one. A missing, contradictory or
ambiguous field is a refusal - never a default that happens to pass.

R5.1: the volume's attachment listing is requested with `--no-trunc`. `docker ps` prints
12-character ids by default, while the comparison is against the full id `docker inspect`
reports; a truncated listing is parsed as a malformed listing (a refusal), never matched
against the inspected id by prefix.

R5-01: issuance is bound to facts, not to an attribute. `establish()`/`revalidate()`
record the complete fact tuple under a private proof token; `issued()` accepts an identity
only while its facts still equal the recorded tuple. `revalidate()` refuses an identity it
did not issue, re-runs the same mode/container/volume eligibility checks as initial
establishment, and never turns an unissued object into a valid one. A `dataclasses.replace`
copy that alters a fact therefore fails the registry comparison.

The SQL connection is never allowed to teach this module what the expected identifier
is. An environment variable alone is not proof either: it only says which container to
inspect, and that container still has to satisfy every fact above and agree with SQL.

R5-06: `pg_controldata`'s identifier evidence is evaluated across BOTH stdout and stderr;
conflicting identifiers, warning/failure text and a non-zero exit are refusals.

`IdentityError` carries a stable `code` and fixed text; neither ever contains a URL, a
password or a value read from the server or from `docker inspect`.
"""

from __future__ import annotations

import json
import re
import subprocess
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "compose.test-postgres.yaml"

# The runner-supplied service container id (`.github/workflows/ci.yml`). It selects the
# container to verify; it is never proof on its own, and alone it never selects CI mode.
CONTAINER_ID_ENV = "HH_TEST_POSTGRES_CONTAINER_ID"
# R4-02: CI mode is explicit. These three are set by the GitHub Actions runner itself; the
# workflow's service container mounts the volume the last two name.
GITHUB_ACTIONS_ENV = "GITHUB_ACTIONS"
RUN_ID_ENV = "GITHUB_RUN_ID"
RUN_ATTEMPT_ENV = "GITHUB_RUN_ATTEMPT"

# The documented Compose project and the facts it must produce (compose.test-postgres.yaml).
COMPOSE_PROJECT = "house-helper-test-postgres"
COMPOSE_SERVICE = "postgres"
DATA_VOLUME = "house-helper-test-postgres-data"
DATA_PATH = "/var/lib/postgresql/data"
DATABASE_PORT = "5432/tcp"
PINNED_IMAGE = (
    "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
)
LOOPBACK_HOST_IPS = frozenset({"127.0.0.1"})

# R5-04: Docker Compose labels the volumes IT creates with its project and the volume's
# compose key. Locally those labels are the provenance evidence that the volume mounted at
# the data directory was created by this project, not a foreign/persistent volume whose
# name happens to match. (CI service containers are created by the runner, not Compose, so
# no Compose labels are expected there - and any Compose label is then a refusal.)
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMPOSE_ONEOFF_LABEL = "com.docker.compose.oneoff"
COMPOSE_VOLUME_LABEL = "com.docker.compose.volume"

# R4-02: the volume the workflow gives the PostgreSQL service container of THIS run and
# attempt. It is provisioned explicitly (`volumes:` in ci.yml) so that disposability is
# the workflow's stated fact, not an assumption about a generated volume name.
CI_VOLUME_PREFIX = "hh-test-postgres"

# What the SQL connection must report; compared as text with the value from pg_controldata.
SYSTEM_IDENTIFIER_SQL = "SELECT system_identifier::text FROM pg_control_system()"

_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")
# R5.1: `docker ps` prints 12-character ids unless `--no-trunc` is given. The volume
# attachment listing is compared with the full id `docker inspect` reports, so it is
# parsed as a FULL id only: a truncated id is a refusal, never something a prefix match
# could let through.
_FULL_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_RUN_COMPONENT = re.compile(r"[0-9]{1,20}")
_IDENTIFIER = re.compile(r"^Database system identifier:\s*([0-9]+)\s*$", re.MULTILINE)
# R4-04: output that says the control file (or the command reading it) cannot be trusted
# is a refusal even when an exit code and an identifier line are also present.
_UNTRUSTWORTHY = re.compile(
    r"(?i)\b(error|fatal|panic|corrupt|corrupted|invalid|could not|warning|failed)\b"
)
_DOCKER_TIMEOUT = 20  # seconds: a Docker daemon that hangs fails the run instead of hanging it


class IdentityError(Exception):
    """The cluster could not be positively identified. `code` is stable; `message` is
    fixed text and holds no URL, no password and no server- or Docker-derived value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Proof:
    """The opaque, per-identity token under which this module records the facts it
    independently established (R5-01). It is unique even between equal identities."""

    __slots__ = ("__weakref__",)


# R5-01: proof token -> the COMPLETE fact tuple established with it. `issued()` accepts an
# identity only while every fact still equals the recorded tuple, so a copied token on an
# altered identity (including a `dataclasses.replace()` copy) does not authorize anything.
_ISSUED: "weakref.WeakKeyDictionary[_Proof, tuple]" = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class ClusterIdentity:
    """A cluster that passed every independent check. The public fields are facts; the
    proof token is not itself the authorization - `issued()` compares the facts with the
    tuple recorded when they were established (R5-01)."""

    system_identifier: str  # decimal digits, from the container's control file
    container_id: str  # the id `docker inspect` was given
    image: str  # the digest-pinned reference the container runs
    volume: str  # the verified data volume: the Compose volume, or the run-scoped CI one
    mode: str  # "compose" | "ci-service"
    port: int  # the host port the target was validated for
    _proof: _Proof = field(default_factory=_Proof, repr=False, compare=False, hash=False)

    def revalidate(
        self, docker: Any, target: Any, *, environ: Optional[Mapping[str, str]] = None
    ) -> "ClusterIdentity":
        """Re-establish the identity before cleanup, doing the SAME eligibility and
        provenance work as initial establishment (R5-01/R5-03).

        An identity this module did not issue is refused outright: revalidation is never a
        path by which a hand-built or altered object becomes authorized. The located mode,
        container and volume must still be the ones this identity was established with, and
        every Docker and control-file fact must come back equal; anything else (a replaced
        container, another volume, a different cluster behind the same port) is refused and
        nothing is destroyed."""
        if not issued(self):
            raise IdentityError(
                "cluster_identity_missing",
                "an identity that was never independently established cannot be "
                "revalidated; nothing is destroyed on it.",
            )
        located = _locate({} if environ is None else environ, docker)
        if located != (self.mode, self.container_id, self.volume):
            raise IdentityError(
                "cluster_identity_changed",
                "the test cluster's selected container, mode or volume changed between "
                "setup and cleanup; nothing is destroyed on it.",
            )
        mode, container_id, expected_volume = located
        renewed = _reestablish(
            target,
            container_id=container_id,
            mode=mode,
            expected_volume=expected_volume,
            docker=docker,
        )
        if renewed != self:
            raise IdentityError(
                "cluster_identity_changed",
                "the test cluster's identity changed between setup and cleanup; "
                "nothing is destroyed on a cluster that is not the verified one.",
            )
        return renewed


def _fact_tuple(cluster: ClusterIdentity) -> tuple:
    """The complete identity fact tuple, read directly from the six public fields. It is a
    module function (not an overridable method) so the registry comparison always sees the
    actual facts."""
    return (
        cluster.system_identifier,
        cluster.container_id,
        cluster.image,
        cluster.volume,
        cluster.mode,
        cluster.port,
    )


def issued(cluster: Any) -> bool:
    """Whether `cluster` carries facts this module independently established (R5-01).

    A token alone is not enough: the identity's COMPLETE facts must still equal the tuple
    recorded at issuance, which is what a copied token on an altered identity fails."""
    if not isinstance(cluster, ClusterIdentity) or not isinstance(cluster._proof, _Proof):
        return False
    recorded = _ISSUED.get(cluster._proof)
    return recorded is not None and recorded == _fact_tuple(cluster)


@dataclass(frozen=True)
class ClusterFacts:
    """The allowlisted facts read from `docker inspect` (never the whole output). Every
    field is what Docker actually returned; nothing here is defaulted."""

    selected_id: str  # the id the caller selected
    inspected_id: str  # the container's own `.Id`
    image: str  # `.Config.Image`
    volume: str  # the Name of the volume mounted at the data directory ("" if not one)
    volume_writable: Optional[bool]  # `.Mounts[].RW`: None when it is not stated
    labels: Mapping[str, str]
    bindings: Sequence[tuple[str, str]]  # (host ip, host port) for the 5432/tcp key
    running: bool
    status: str


@dataclass(frozen=True)
class VolumeFacts:
    """The allowlisted facts of `docker volume inspect` (R5-04): the actual volume Docker
    reports, its driver/scope, and the labels that carry its provenance. Nothing here is
    defaulted from the name."""

    name: str
    driver: str
    scope: str
    labels: Mapping[str, str]


class Docker:
    """The Docker CLI, as this contract uses it. `execute` is injectable so the contract
    can be tested offline; every method returns data or raises IdentityError with fixed
    text (never the command's output, which may contain environment values)."""

    def __init__(self, execute: Optional[Callable[[Sequence[str]], tuple[int, str, str]]] = None) -> None:
        self._execute = execute or _run_docker

    def inspect(self, container_id: str) -> Mapping[str, Any]:
        code, out, _ = self._execute(
            ["docker", "inspect", "--type", "container", "--format", "{{json .}}", container_id]
        )
        if code != 0:
            raise IdentityError(
                "cluster_identity_unavailable",
                "the test cluster container could not be inspected; refusing cluster-wide work.",
            )
        try:
            facts = json.loads(out)
        except ValueError:
            raise IdentityError(
                "cluster_identity_unavailable",
                "the container inspection did not answer with a readable description.",
            ) from None
        if not isinstance(facts, dict):
            raise IdentityError(
                "cluster_identity_unavailable",
                "the container inspection answered with an unexpected shape.",
            )
        return facts

    def compose_container_ids(self) -> list[str]:
        code, out, _ = self._execute(
            [
                "docker",
                "compose",
                "-f",
                str(COMPOSE_FILE),
                "--project-name",
                COMPOSE_PROJECT,
                "ps",
                "--quiet",
                COMPOSE_SERVICE,
            ]
        )
        if code != 0:
            raise IdentityError(
                "cluster_identity_unavailable",
                "the disposable test cluster of compose.test-postgres.yaml could not be listed; "
                "start it (docker compose -f compose.test-postgres.yaml up -d) or run in CI.",
            )
        found = [line.strip() for line in out.splitlines() if line.strip()]
        if any(not _CONTAINER_ID.fullmatch(name) for name in found):
            raise IdentityError(
                "cluster_identity_invalid",
                "the disposable test cluster listing did not name container ids; refusing "
                "cluster-wide work.",
            )
        return found

    def volume(self, name: str) -> Mapping[str, Any]:
        """`docker volume inspect` of the volume mounted at the data directory (R5-04).

        This is the volume's own record, not the mount entry that named it: its driver,
        scope and labels are what distinguish the project's dedicated volume from a foreign
        or persistent one that happens to carry the same name."""
        code, out, _ = self._execute(
            ["docker", "volume", "inspect", "--format", "{{json .}}", name]
        )
        if code != 0:
            raise IdentityError(
                "cluster_identity_unavailable",
                "the test cluster's data volume could not be inspected; refusing cluster-wide work.",
            )
        try:
            facts = json.loads(out)
        except ValueError:
            raise IdentityError(
                "cluster_identity_unavailable",
                "the volume inspection did not answer with a readable description.",
            ) from None
        if not isinstance(facts, dict):
            raise IdentityError(
                "cluster_identity_unavailable",
                "the volume inspection answered with an unexpected shape.",
            )
        return facts

    def volume_containers(self, name: str) -> list[str]:
        """Every container (running or stopped) attached to the volume (R5-04).

        A dedicated disposable volume is used by exactly the verified container; any other
        attachment is unexpected sharing and a refusal. The listing is requested with
        `--no-trunc` (R5.1): `docker ps` otherwise prints 12-character ids, which could
        never equal the full id `docker inspect` reports, so a legitimate container would
        be rejected as a different one."""
        code, out, _ = self._execute(
            [
                "docker",
                "ps",
                "-a",
                "--no-trunc",
                "--filter",
                f"volume={name}",
                "--format",
                "{{.ID}}",
            ]
        )
        if code != 0:
            raise IdentityError(
                "cluster_identity_unavailable",
                "the containers attached to the test cluster's data volume could not be listed.",
            )
        found = [line.strip() for line in out.splitlines() if line.strip()]
        if any(not _FULL_CONTAINER_ID.fullmatch(container) for container in found):
            raise IdentityError(
                "cluster_identity_invalid",
                "the volume attachment listing did not name full container ids; refusing "
                "cluster-wide work.",
            )
        return found

    def system_identifier(self, container_id: str) -> str:
        # The control file is read at the mounted data directory itself, not through the
        # container's environment or through SQL: the path that was verified above. The
        # locale is fixed so the line's spelling cannot vary with the image's environment
        # (R4-04).
        code, out, err = self._execute(
            [
                "docker",
                "exec",
                "-e",
                "LC_ALL=C",
                "-e",
                "LANG=C",
                container_id,
                "pg_controldata",
                "-D",
                DATA_PATH,
            ]
        )
        if code != 0:
            raise IdentityError(
                "cluster_identity_unavailable",
                "pg_controldata did not exit successfully inside the verified container; the "
                "cluster's control file is not trustworthy evidence.",
            )
        reported = f"{out}\n{err}"
        if _UNTRUSTWORTHY.search(reported):
            raise IdentityError(
                "cluster_identity_unavailable",
                "pg_controldata reported a problem with the control file; its output is not "
                "trustworthy evidence.",
            )
        # R5-06: identifier evidence is evaluated across BOTH streams. A second identifier
        # on either stream - conflicting or not - is contradictory evidence, not something
        # to discard because it is not on stdout.
        found = _IDENTIFIER.findall(out) + _IDENTIFIER.findall(err)
        if len(found) != 1:
            raise IdentityError(
                "cluster_identity_unavailable",
                "the control file did not report exactly one database system identifier.",
            )
        value = found[0]
        if not value.isdigit() or int(value) <= 0:
            raise IdentityError(
                "cluster_identity_unavailable",
                "the container's database system identifier is not a positive number.",
            )
        return value


def _run_docker(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        raise IdentityError(
            "cluster_identity_unavailable",
            "the Docker CLI could not be run; the test cluster cannot be independently identified.",
        ) from None
    return done.returncode, done.stdout or "", done.stderr or ""


# --- the identity contract -------------------------------------------------------------------


def establish(
    target: Any,
    *,
    environ: Optional[Mapping[str, str]] = None,
    docker: Optional[Any] = None,
) -> ClusterIdentity:
    """The verified identity of the disposable cluster behind `target`, or IdentityError.

    `environ` is os.environ unless a caller passes its own. `docker` is a `Docker` (or a
    stand-in with the same methods). Nothing is opened by SQL here."""
    docker = Docker() if docker is None else docker
    environ = {} if environ is None else environ
    mode, container_id, expected_volume = _locate(environ, docker)
    return _reestablish(
        target,
        container_id=container_id,
        mode=mode,
        expected_volume=expected_volume,
        docker=docker,
    )


def _reestablish(
    target: Any, *, container_id: str, mode: str, expected_volume: str, docker: Any
) -> ClusterIdentity:
    """Verify one already-located container (the first time and again before cleanup)."""
    facts = _facts(docker.inspect(container_id), container_id)
    # R5-04: the volume's OWN record is inspected, not just the name on the mount. Without
    # a mount there is nothing to inspect; `_require_facts` then refuses the missing volume.
    volume = _volume_facts(docker.volume(facts.volume)) if facts.volume else None
    attached = docker.volume_containers(facts.volume) if facts.volume else []
    _require_facts(facts, volume, attached, target, mode, expected_volume)
    identifier = docker.system_identifier(container_id)
    # Re-checked here as well: `Docker` already refuses a malformed value, and a stand-in
    # implementation must not be able to hand one through.
    if not identifier.isdigit() or int(identifier) <= 0:
        raise IdentityError(
            "cluster_identity_unavailable",
            "the container's database system identifier is not a positive number.",
        )
    verified = ClusterIdentity(
        system_identifier=identifier,
        container_id=container_id,
        image=PINNED_IMAGE,
        volume=expected_volume,
        mode=mode,
        port=target.port,
    )
    # R5-01: bind the COMPLETE facts to the proof token. `issued()` re-compares them, so a
    # copied token on an altered identity is not authorized.
    _ISSUED[verified._proof] = _fact_tuple(verified)
    return verified


def _locate(environ: Mapping[str, str], docker: Any) -> tuple[str, str, str]:
    """Which container to verify and which data volume it must keep, in exactly one
    explicit mode (R4-02). Conflicting or incomplete evidence is refused; the weaker CI
    contract is never selected by a container id alone."""
    github = (environ.get(GITHUB_ACTIONS_ENV) or "").strip().lower()
    named = (environ.get(CONTAINER_ID_ENV) or "").strip()
    if github not in ("", "true", "false"):
        raise IdentityError(
            "cluster_identity_invalid",
            "the GitHub Actions indicator has an unexpected value; refusing to guess the "
            "cluster identity mode.",
        )
    if github == "true":
        if not named:
            raise IdentityError(
                "cluster_identity_missing",
                "CI did not name the PostgreSQL service container (job.services.postgres.id); "
                "refusing cluster-wide work without an independent identity.",
            )
        if not _CONTAINER_ID.fullmatch(named):
            raise IdentityError(
                "cluster_identity_invalid",
                "the configured test-cluster container id is not a container id.",
            )
        return "ci-service", named, ci_volume(environ)
    if named:
        raise IdentityError(
            "cluster_identity_conflict",
            "a service-container id was named outside the GitHub Actions runner; the CI "
            "identity contract is not available here, and a container id alone is not "
            "evidence. Unset it to verify the documented Compose cluster.",
        )
    # R5-04 item 6: in local mode, runner-scoped evidence is contradictory, not ignored.
    # A run or attempt variable outside the runner means the environment is not the one
    # this mode's evidence describes, so the mode cannot be selected safely.
    if any((environ.get(name) or "").strip() for name in (RUN_ID_ENV, RUN_ATTEMPT_ENV)):
        raise IdentityError(
            "cluster_identity_conflict",
            "a GitHub Actions run or attempt was named outside the runner; the local "
            "Compose identity contract does not accept runner-scoped evidence.",
        )
    found = docker.compose_container_ids()
    if not found:
        raise IdentityError(
            "cluster_identity_missing",
            "the disposable test cluster is not running; refusing to create or drop roles and "
            "databases without an independently identified cluster.",
        )
    if len(found) > 1:
        raise IdentityError(
            "cluster_identity_ambiguous",
            "more than one container answers for the test cluster; refusing cluster-wide work.",
        )
    return "compose", found[0], DATA_VOLUME


def ci_volume(environ: Mapping[str, str]) -> str:
    """The run-scoped volume the CI service container must keep its data on (R4-02)."""
    run_id = (environ.get(RUN_ID_ENV) or "").strip()
    attempt = (environ.get(RUN_ATTEMPT_ENV) or "").strip()
    if not (_RUN_COMPONENT.fullmatch(run_id) and _RUN_COMPONENT.fullmatch(attempt)):
        raise IdentityError(
            "cluster_identity_incomplete",
            "the GitHub Actions runner did not name its run and attempt; the run-scoped "
            "test volume cannot be identified, so the service container cannot be shown "
            "to be disposable.",
        )
    return f"{CI_VOLUME_PREFIX}-{run_id}-{attempt}"


def _facts(raw: Mapping[str, Any], container_id: str) -> ClusterFacts:
    """The allowlisted facts, read strictly: a missing or mistyped field is a refusal in
    `_require_facts`, never a default that happens to pass (R4-04)."""
    if not isinstance(raw, dict):
        raise IdentityError(
            "cluster_identity_unavailable", "the container description is not readable."
        )
    state = raw.get("State")
    config = raw.get("Config")
    network = raw.get("NetworkSettings")
    ports = network.get("Ports") if isinstance(network, dict) else None
    mounts = raw.get("Mounts")
    for value, label in ((state, "state"), (config, "config"), (network, "network settings")):
        if not isinstance(value, dict):
            raise IdentityError(
                "cluster_identity_unavailable",
                f"the container description's {label} is not readable.",
            )
    if not isinstance(ports, dict) or not isinstance(mounts, list):
        raise IdentityError(
            "cluster_identity_unavailable",
            "the container's ports or mounts are not readable.",
        )

    volumes = [
        mount
        for mount in mounts
        if isinstance(mount, dict) and mount.get("Destination") == DATA_PATH
    ]
    if len(volumes) > 1:
        raise IdentityError(
            "cluster_identity_ambiguous",
            "more than one mount answers for the data directory; refusing cluster-wide work.",
        )
    volume = ""
    volume_writable: Optional[bool] = None
    if volumes:
        mount = volumes[0]
        if mount.get("Type") != "volume":
            raise IdentityError(
                "cluster_identity_mismatch",
                "the container's data directory is not on a Docker volume.",
            )
        volume = mount.get("Name") if isinstance(mount.get("Name"), str) else ""
        if not volume:
            raise IdentityError(
                "cluster_identity_mismatch",
                "the container's data volume is not named.",
            )
        volume_writable = mount.get("RW") is True  # an absent RW is not evidence of write access

    if set(ports) - {DATABASE_PORT}:  # R4-04: no other publication belongs to this contract
        raise IdentityError(
            "cluster_identity_mismatch",
            "the container publishes ports the test contract does not expect.",
        )
    # R5-06: the port contract is exact. A malformed entry is refusal, never discarded in
    # favour of a well-formed sibling - one well-formed binding plus garbage is not proof
    # that exactly one publication exists.
    published = ports.get(DATABASE_PORT)
    if published is None:
        published = []
    if not isinstance(published, (list, tuple)):
        raise IdentityError(
            "cluster_identity_invalid",
            "the container's port publication is not a list of bindings.",
        )
    bindings = []
    for binding in published:
        if not isinstance(binding, dict) or set(binding) != {"HostIp", "HostPort"}:
            raise IdentityError(
                "cluster_identity_invalid",
                "the container's port publication contains an entry that is not a host "
                "address and port.",
            )
        host_ip, host_port = binding["HostIp"], binding["HostPort"]
        if not isinstance(host_ip, str) or not isinstance(host_port, str) or not host_port.isdigit():
            raise IdentityError(
                "cluster_identity_invalid",
                "the container's port publication contains an entry that is not a host "
                "address and port.",
            )
        bindings.append((host_ip, host_port))

    inspected = raw.get("Id")
    image = config.get("Image")
    labels = config.get("Labels")
    running = state.get("Running")
    status = state.get("Status")
    if not isinstance(inspected, str) or not isinstance(image, str):
        raise IdentityError(
            "cluster_identity_unavailable",
            "the container description does not name its container id and image.",
        )
    if not isinstance(running, bool) or not isinstance(status, str):
        raise IdentityError(
            "cluster_identity_unavailable",
            "the container description does not state its running state.",
        )
    labels = {} if labels is None else labels
    if not isinstance(labels, dict):
        raise IdentityError(
            "cluster_identity_unavailable",
            "the container description's labels are not readable.",
        )

    return ClusterFacts(
        selected_id=container_id,
        inspected_id=inspected,
        image=image,
        volume=volume,
        volume_writable=volume_writable,
        labels={str(key): str(value) for key, value in labels.items()},
        bindings=tuple(bindings),
        running=running,
        status=status,
    )


def _volume_facts(raw: Mapping[str, Any]) -> VolumeFacts:
    """The volume's own record, read strictly (R5-04). A volume that Docker does not
    describe as a local, named volume is not the dedicated test volume; nothing is inferred
    from the mount's Name string."""
    if not isinstance(raw, dict):
        raise IdentityError(
            "cluster_identity_unavailable", "the volume description is not readable."
        )
    name = raw.get("Name")
    driver = raw.get("Driver")
    scope = raw.get("Scope")
    labels = raw.get("Labels")
    if not isinstance(name, str) or not name:
        raise IdentityError(
            "cluster_identity_unavailable", "the volume description does not name the volume."
        )
    if not isinstance(driver, str) or not isinstance(scope, str):
        raise IdentityError(
            "cluster_identity_unavailable",
            "the volume description does not state its driver and scope.",
        )
    labels = {} if labels is None else labels  # an unlabelled Docker volume reports null
    if not isinstance(labels, dict):
        raise IdentityError(
            "cluster_identity_unavailable", "the volume description's labels are not readable."
        )
    return VolumeFacts(
        name=name,
        driver=driver,
        scope=scope,
        labels={str(key): str(value) for key, value in labels.items()},
    )


def _require_facts(
    facts: ClusterFacts,
    volume: Optional[VolumeFacts],
    attached: Sequence[str],
    target: Any,
    mode: str,
    expected_volume: str,
) -> None:
    """Every required fact, or a refusal. Ordered from the container to the port so the
    message names the first disagreement. Nothing here repairs a missing fact."""
    # R4-04: running and status must agree and say "running"; either alone is not enough.
    if not facts.running or facts.status != "running":
        raise IdentityError(
            "cluster_identity_unavailable",
            "the test cluster container is not stated to be running.",
        )
    if not (
        _CONTAINER_ID.fullmatch(facts.inspected_id)
        and facts.inspected_id.startswith(facts.selected_id)
    ):
        raise IdentityError(
            "cluster_identity_mismatch",
            "the inspected container is not the container that was selected.",
        )
    if facts.image != PINNED_IMAGE:
        raise IdentityError(
            "cluster_identity_mismatch",
            "the container does not run the digest-pinned PostgreSQL 16 test image.",
        )
    if mode == "compose":
        if facts.labels.get(COMPOSE_PROJECT_LABEL) != COMPOSE_PROJECT:
            raise IdentityError(
                "cluster_identity_mismatch",
                "the container does not belong to the documented disposable Compose project.",
            )
        if facts.labels.get(COMPOSE_SERVICE_LABEL) != COMPOSE_SERVICE:
            raise IdentityError(
                "cluster_identity_mismatch",
                "the container is not the database service of the documented Compose project.",
            )
        oneoff = facts.labels.get(COMPOSE_ONEOFF_LABEL)
        if oneoff not in ("False", "false", "0"):
            raise IdentityError(
                "cluster_identity_mismatch",
                "the container is not stated to be a long-running Compose service (not a one-off run).",
            )
    else:
        # R4-02/R5-04 item 5: a Compose container is not a GitHub Actions service
        # container, and the modes are mutually exclusive. ANY Compose provenance label
        # (another project's container, or a one-off run) is refused, not only one that
        # names this repository's project.
        if any(key.startswith("com.docker.compose.") for key in facts.labels):
            raise IdentityError(
                "cluster_identity_conflict",
                "a Compose-managed container was named as the CI service container; "
                "refusing the weaker contract for a local cluster.",
            )
    if facts.volume != expected_volume:
        raise IdentityError(
            "cluster_identity_mismatch",
            "the container's data directory is not the volume this "
            + ("run" if mode == "ci-service" else "project")
            + " provisioned for it.",
        )
    # R5-04: the volume's own facts and provenance. A matching NAME is not evidence that
    # the volume is this project's dedicated one.
    if volume is None or volume.name != facts.volume:
        raise IdentityError(
            "cluster_identity_mismatch",
            "the container's data directory is not mounted from a verified Docker volume.",
        )
    if volume.driver != "local" or volume.scope != "local":
        raise IdentityError(
            "cluster_identity_mismatch",
            "the container's data volume is not a local Docker volume.",
        )
    if mode == "compose":
        if volume.labels.get(COMPOSE_PROJECT_LABEL) != COMPOSE_PROJECT:
            raise IdentityError(
                "cluster_identity_mismatch",
                "the data volume was not created by the documented disposable Compose project.",
            )
        if volume.labels.get(COMPOSE_VOLUME_LABEL) != DATA_VOLUME:
            raise IdentityError(
                "cluster_identity_mismatch",
                "the data volume is not the documented project's volume.",
            )
    elif any(key.startswith("com.docker.compose.") for key in volume.labels):
        raise IdentityError(
            "cluster_identity_conflict",
            "the data volume carries Compose provenance in a GitHub Actions run; the "
            "runner's service volume is not a Compose volume.",
        )
    # R5-04: attachment. A dedicated disposable volume is used by exactly the verified
    # container; another (including stopped) container sharing it is a refusal.
    if len(attached) != 1 or attached[0] != facts.inspected_id:
        raise IdentityError(
            "cluster_identity_mismatch",
            "the data volume is not attached to exactly the verified test container.",
        )
    if facts.volume_writable is not True:
        raise IdentityError(
            "cluster_identity_mismatch",
            "the container's data directory is not stated to be writable.",
        )
    if len(facts.bindings) != 1:
        raise IdentityError(
            "cluster_identity_ambiguous",
            "the test cluster does not publish exactly one port; refusing cluster-wide work.",
        )
    host_ip, host_port = facts.bindings[0]
    if host_ip not in LOOPBACK_HOST_IPS:
        raise IdentityError(
            "cluster_identity_mismatch",
            "the test cluster is published on an address that is not loopback.",
        )
    if not host_port.isdigit() or host_port != str(target.port):
        raise IdentityError(
            "cluster_identity_mismatch",
            "the container is not published on the port the test URL was validated for.",
        )
