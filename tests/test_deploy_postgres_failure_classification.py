"""WP-2E R2 (F3): a Docker execution failure is infrastructure, not an artifact verdict.

Codex reproduced three control flows where a Docker/tooling failure was translated into an artifact
refusal at the source, so `deploy/offsite_rehearsal.py` reported the point NOT RESTORABLE:

  1. `docker run` exit 125 / daemon unavailable while listing the archive -> `archive_integrity`;
  2. the same during the SEQUENCE SET extraction -> `sequence_extraction`;
  3. `docker exec` against a stopped container during the isolated `pg_restore` -> `isolated_restore`.

Each is now `docker_infrastructure` and helper exit 4 (environment unavailable), which the parent
classifies as a failure before a verdict (exit 1). The positive controls below prove that a program
which actually ran and refused the archive (or the restored data) keeps its artifact code; the fix
does not reclassify every artifact refusal as an environment failure.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from deploy.postgres import maintenance

IMAGE = "postgres@sha256:" + "a" * 64
# Captured before any test replaces the module attribute, so a stub factory can build the real class.
_REAL_TOOL_CONTAINERS = maintenance.ToolContainers
RUN_ID = "0123456789abcdef0123456789abcdef"
CONTAINER = "c" * 64
DAEMON_DOWN = (
    b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    b"Is the docker daemon running?\n"
)
STOPPED = b"Error response from daemon: Container " + CONTAINER.encode() + b" is not running\n"


class _Runner:
    """The one command boundary: every Docker invocation answers with the same result."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, argv, *, input=None, capture_output=True, timeout=120, check=False, env=None):
        self.calls.append(list(argv))
        return self.result


def _tools(result) -> maintenance.ToolContainers:
    return maintenance.ToolContainers(IMAGE, runner=_Runner(result))


def _refused(tools_call) -> str:
    with pytest.raises(maintenance.MaintenanceError) as refused:
        tools_call()
    return refused.value.code


# --- 1. the archive listing -------------------------------------------------------------------


def test_a_docker_run_failure_while_listing_is_infrastructure_not_archive_integrity(tmp_path):
    result = subprocess.CompletedProcess(["docker"], 125, b"", DAEMON_DOWN)
    code = _refused(lambda: _tools(result).pg_restore_list(tmp_path))
    assert code == "docker_infrastructure"


def test_a_pg_restore_that_runs_and_refuses_the_archive_stays_archive_integrity(tmp_path):
    result = subprocess.CompletedProcess(
        ["docker"], 1, b"", b"pg_restore: error: could not read from input file: end of file\n"
    )
    code = _refused(lambda: _tools(result).pg_restore_list(tmp_path))
    assert code == "archive_integrity"


# --- 2. the SEQUENCE SET extraction -----------------------------------------------------------


def test_a_docker_run_failure_during_sequence_extraction_is_infrastructure(tmp_path):
    result = subprocess.CompletedProcess(["docker"], 125, b"", DAEMON_DOWN)
    code = _refused(lambda: _tools(result).sequence_extraction(tmp_path, "1; 0 0 SEQUENCE SET\n"))
    assert code == "docker_infrastructure"


def test_a_pg_restore_that_runs_and_refuses_the_scoped_extraction_stays_sequence_extraction(tmp_path):
    result = subprocess.CompletedProcess(
        ["docker"], 1, b"", b"pg_restore: error: could not find entry 0001 1 0 SEQUENCE SET\n"
    )
    code = _refused(lambda: _tools(result).sequence_extraction(tmp_path, "1; 0 0 SEQUENCE SET\n"))
    assert code == "sequence_extraction"


# --- the invocation codes Docker itself uses --------------------------------------------------


@pytest.mark.parametrize("status", [125, 126, 127])
def test_every_docker_invocation_failure_status_is_infrastructure(tmp_path, status):
    result = subprocess.CompletedProcess(["docker"], status, b"", b"docker: unexpected failure\n")
    code = _refused(lambda: _tools(result).pg_restore_list(tmp_path))
    assert code == "docker_infrastructure"


# --- 3. the isolated restore's `docker exec` --------------------------------------------------


class _RehearsalTools:
    """`rehearse`'s container boundary, as far as the test needs it. The isolated `pg_restore`
    answers with the result under test; every other container command succeeds."""

    def __init__(self, pg_restore_result):
        self.pg_restore_result = pg_restore_result
        self.calls = []

    def start_detached(self, program, **kwargs):
        assert kwargs["name"] == maintenance.REHEARSAL_CONTAINER_PREFIX + RUN_ID
        return CONTAINER

    def copy_in(self, container, source, target):
        pass

    def pg_restore_list(self, work):
        return "1; 2615 0 SCHEMA - public hh_owner\n2; 0 0 SEQUENCE SET public messages_id_seq hh_owner\n"

    def exec(self, container, program, *, stdin=None, user="postgres", timeout=300):
        self.calls.append(tuple(program))
        if program[:1] == ["pg_restore"]:
            return self.pg_restore_result
        if stdin and b"system_identifier" in stdin:
            return subprocess.CompletedProcess(program, 0, b"7709166311476633635\n", b"")
        return subprocess.CompletedProcess(program, 0, b"", b"")


def _run_rehearse(monkeypatch, work, tools):
    monkeypatch.setattr(maintenance, "_await_postgres", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "_filtered_restore_list", lambda text: text)
    point = SimpleNamespace(
        manifest={
            "alembic_revision": "abc123",
            "datcollate": "C",
            "datctype": "C",
            "tables": {},
        }
    )
    return maintenance.rehearse(
        tools, point=point, work=work, fixture={}, app_image=IMAGE, run_id=RUN_ID
    )


def test_a_stopped_container_during_the_isolated_restore_is_infrastructure(monkeypatch, tmp_path):
    result = subprocess.CompletedProcess(["docker"], 1, b"", STOPPED)
    code = _refused(lambda: _run_rehearse(monkeypatch, tmp_path, _RehearsalTools(result)))
    assert code == "docker_infrastructure"


def test_a_pg_restore_that_runs_and_fails_on_the_restore_stays_isolated_restore(monkeypatch, tmp_path):
    result = subprocess.CompletedProcess(
        ["docker"], 1, b"", b"pg_restore: error: could not execute query: ERROR: relation does not exist\n"
    )
    code = _refused(lambda: _run_rehearse(monkeypatch, tmp_path, _RehearsalTools(result)))
    assert code == "isolated_restore"


# --- the attachment proof is an artifact refusal too ------------------------------------------


class _RunTools:
    def __init__(self, result):
        self.result = result

    def run(self, program, **kwargs):
        return self.result


def test_a_docker_failure_during_the_attachment_proof_is_infrastructure():
    tools = _RunTools(subprocess.CompletedProcess(["docker"], 125, b"", DAEMON_DOWN))
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.attachment_proof(
            tools,
            app_image=IMAGE,
            database="house_helper_restore_x",
            role="hh_restore",
            passfile_value=None,
            network="none",
        )
    assert refused.value.code == "docker_infrastructure"


# --- the helper's exit status and the parent's classification ---------------------------------


class _DaemonDownRunner:
    """`docker run` fails as an unreachable daemon (exit 125); the container listing is empty, so
    the run's verified removal is confirmed and the clean-up does not take precedence."""

    def __call__(self, argv, *, input=None, capture_output=True, timeout=120, check=False, env=None):
        argv = list(argv)
        if argv[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(argv, 125, b"", DAEMON_DOWN)
        if argv[:3] == ["docker", "container", "ls"]:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_a_docker_run_failure_in_the_rehearse_command_is_exit_4_not_archive_integrity(monkeypatch, tmp_path, capsys):
    """The Codex case at the command level: the first `docker run` is the archive listing."""
    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: IMAGE)
    monkeypatch.setattr(
        maintenance, "ToolContainers",
        lambda image: _REAL_TOOL_CONTAINERS(image, runner=_DaemonDownRunner()),
    )
    monkeypatch.setattr(
        maintenance.backup, "read_archive",
        lambda archive: SimpleNamespace(dump=b"", manifest={"tables": {}}),
    )
    archive = tmp_path / "point.tar"
    archive.write_bytes(b"")
    assert maintenance.main(
        ["--compose-file", str(tmp_path / "compose.yaml"), "--work-dir", str(tmp_path),
         "rehearse", "--run-id", RUN_ID, str(archive)]
    ) == maintenance.EX_UNAVAILABLE
    errors = capsys.readouterr().err
    assert "refused [docker_infrastructure]" in errors
    assert "archive_integrity" not in errors
    assert "Traceback" not in errors
