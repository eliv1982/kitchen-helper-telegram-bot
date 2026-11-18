"""WP-2E R3: the three findings of the independent R2 review, and their shared failure classes.

R3-F1  A signal that arrives while the helper is already removing its container used to escape
       the verified removal, leave the container behind and make the parent report FAILED without
       ever running its fallback. Cleanup is now a bounded, cancellation-deferred critical
       section; a signal that lands there is preserved as the outcome afterwards, and the parent
       independently retries the removal whenever the helper terminated abnormally or its record
       does not confirm the absence.

R3-F2  `docker run`/`docker exec` statuses that are not the contained program's verdict (-9, -15,
       125-127, 137/143 from an OOM kill or `docker stop`, ...) used to be translated into
       `archive_integrity`, `sequence_extraction`, `isolated_restore` and `attachment_proof`, so
       the parent said NOT RESTORABLE with no evidence about the archive.

R3-F3  The parent classified by searching the helper's stderr for `refused [...]` and
       `underlying [...]` tokens. An operator-supplied compose path containing such text (the
       tool echoes it in its recovery command) could therefore manufacture an artifact verdict.
       The classification now reads ONE versioned JSON record from a file the parent chooses.

The first two sections are offline. The third drives the REAL helper through a real POSIX process
(native on POSIX, through WSL on Windows) with a controlled fake `docker` on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from deploy.postgres import maintenance
from tests.deploy_support import REPO_ROOT, posix_only
from tests.offsite_support import load_rehearsal
from tests.test_deploy_offsite_postgres import (
    INFRASTRUCTURE_UNDERLYING,
    PostgresRehearsalSystem,
    _unconfirmed,
    v2_world,
)
from tests.test_deploy_postgres_failure_classification import (
    _RehearsalTools,
    _RunTools,
    _run_rehearse,
)

rehearsal = load_rehearsal()

IMAGE = "postgres@sha256:" + "a" * 64
RUN_ID = "0123456789abcdef0123456789abcdef"
NAME = maintenance.REHEARSAL_CONTAINER_PREFIX + RUN_ID
DAEMON_DOWN = b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock.\n"
# Everything the Docker boundary must refuse: negative = the Docker CLI itself was signalled;
# 125-127 = Docker could not execute the program; 128+N = the program inside was signalled.
ABNORMAL_STATUSES = [-9, -15, 125, 126, 127, 128 + 9, 128 + 15, 2, 3]


def _refused(call) -> str:
    with pytest.raises(maintenance.MaintenanceError) as refused:
        call()
    return refused.value.code


# --- R3-F2: an abnormal Docker status is never artifact evidence --------------------------------


@pytest.mark.parametrize("status", ABNORMAL_STATUSES)
def test_an_abnormal_status_while_listing_the_archive_is_infrastructure(tmp_path, status):
    result = subprocess.CompletedProcess(["docker"], status, b"", b"")
    code = _refused(lambda: maintenance.ToolContainers(IMAGE, runner=lambda *a, **k: result).pg_restore_list(tmp_path))
    assert code == "docker_infrastructure"


@pytest.mark.parametrize("status", ABNORMAL_STATUSES)
def test_an_abnormal_status_during_sequence_extraction_is_infrastructure(tmp_path, status):
    result = subprocess.CompletedProcess(["docker"], status, b"", b"")
    tools = maintenance.ToolContainers(IMAGE, runner=lambda *a, **k: result)
    code = _refused(lambda: tools.sequence_extraction(tmp_path, "1; 0 0 SEQUENCE SET\n"))
    assert code == "docker_infrastructure"


@pytest.mark.parametrize("status", ABNORMAL_STATUSES)
def test_an_abnormal_status_during_the_isolated_restore_is_infrastructure(monkeypatch, tmp_path, status):
    result = subprocess.CompletedProcess(["docker"], status, b"", b"")
    code = _refused(lambda: _run_rehearse(monkeypatch, tmp_path, _RehearsalTools(result)))
    assert code == "docker_infrastructure"


@pytest.mark.parametrize("status", [status for status in ABNORMAL_STATUSES if status != 3])
def test_an_abnormal_status_during_the_attachment_proof_is_infrastructure(status):
    tools = _RunTools(subprocess.CompletedProcess(["docker"], status, b"", b""))
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.attachment_proof(
            tools, app_image=IMAGE, database="house_helper_restore_x", role="hh_restore",
            passfile_value=None, network="none",
        )
    assert refused.value.code == "docker_infrastructure"


def test_the_attachment_proofs_refusal_status_without_a_record_is_environment_not_artifact():
    """R5 reserves status 3 for the proof's own deliberate refusal, so it is no longer a Docker
    infrastructure status; without the framed record it is an unreliable proof channel."""
    tools = _RunTools(subprocess.CompletedProcess(["docker"], 3, b"", b""))
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.attachment_proof(
            tools, app_image=IMAGE, database="house_helper_restore_x", role="hh_restore",
            passfile_value=None, network="none",
        )
    assert refused.value.code == "attachment_proof_environment"


def test_a_container_termination_status_is_infrastructure_not_isolated_restore(monkeypatch, tmp_path):
    """137 is 128+9: the program inside the container was killed, so it produced no verdict."""
    result = subprocess.CompletedProcess(["docker"], 137, b"", b"")
    assert _refused(lambda: _run_rehearse(monkeypatch, tmp_path, _RehearsalTools(result))) == "docker_infrastructure"


@pytest.mark.parametrize(
    "result, expected",
    [
        (subprocess.CompletedProcess(["docker"], 1, b"", b"pg_restore: error: could not read from input file: end of file\n"),
         "archive_integrity"),
        (subprocess.CompletedProcess(["docker"], 0, b"", b""), None),
    ],
    ids=["corrupt-archive-still-artifact", "success-is-not-refused"],
)
def test_the_genuine_program_verdict_is_preserved(tmp_path, result, expected):
    """Positive control: a pg_restore that actually ran and refused the archive keeps its code."""
    tools = maintenance.ToolContainers(IMAGE, runner=lambda *a, **k: result)
    if expected is None:
        assert tools.pg_restore_list(tmp_path) == ""
    else:
        assert _refused(lambda: tools.pg_restore_list(tmp_path)) == expected


def test_a_daemon_error_with_a_program_status_of_one_is_still_infrastructure(tmp_path):
    stopped = b"Error response from daemon: Container deadbeef is not running\n"
    result = subprocess.CompletedProcess(["docker"], 1, b"", stopped)
    tools = maintenance.ToolContainers(IMAGE, runner=lambda *a, **k: result)
    assert _refused(lambda: tools.pg_restore_list(tmp_path)) == "docker_infrastructure"


# --- R3-F3: the status record is the only classification channel ---------------------------------

CONTRACT = "house-helper-maintenance-status/1"


def record_bytes(run_id: str, **overrides) -> bytes:
    value = {
        "contract": CONTRACT,
        "command": "rehearse",
        "run_id": run_id,
        "status": "passed",
        "cleanup": "confirmed",
        "code": None,
    }
    value.update(overrides)
    return (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")


def _record(**overrides):
    """The record the fake helper writes, built with the run id the parent actually generated."""
    return lambda run_id: record_bytes(run_id, **overrides)


def _cleanup_record(run_id: str, **overrides) -> bytes:
    value = {
        "contract": CONTRACT, "command": "cleanup-rehearsal", "run_id": run_id,
        "status": "passed", "cleanup": "confirmed", "code": None,
    }
    value.update(overrides)
    return (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")


def test_the_rehearsal_and_the_accepted_tool_agree_on_the_record_contract():
    assert rehearsal.STATUS_CONTRACT == maintenance.STATUS_CONTRACT
    assert rehearsal.STATUS_VALUES == maintenance.STATUS_VALUES
    assert rehearsal.CLEANUP_VALUES == maintenance.CLEANUP_VALUES
    assert rehearsal.STATUS_FIELDS == maintenance.STATUS_FIELDS
    assert rehearsal.STATUS_CODE_RE.pattern == maintenance.STATUS_CODE_RE.pattern
    assert rehearsal.STATUS_RUN_ID_RE.pattern == maintenance.RUN_ID_RE.pattern


def test_the_record_schema_is_exactly_the_six_fixed_fields():
    assert maintenance._Outcome().record() is None            # nothing to say before a status exists
    outcome = maintenance._Outcome()
    outcome.command, outcome.run_id = "rehearse", RUN_ID
    outcome.status, outcome.cleanup, outcome.code = "failed", "unconfirmed", "isolated_verification"
    assert outcome.record() == {
        "contract": CONTRACT, "command": "rehearse", "run_id": RUN_ID,
        "status": "failed", "cleanup": "unconfirmed", "code": "isolated_verification",
    }


def _status_reader(tmp_path: Path, text) -> object:
    path = tmp_path / "status.json"
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return rehearsal.read_status(path, "rehearse", RUN_ID)


@pytest.mark.parametrize(
    "text",
    [
        b"",                                                      # absent evidence
        b"not json at all\n",                                      # malformed
        b'{"contract":"house-helper-maintenance-status/1",\n',      # truncated
        record_bytes(RUN_ID) + record_bytes(RUN_ID),               # two records
        record_bytes(RUN_ID, code="isolated_verification"),        # passed with a code: contradictory
        record_bytes(RUN_ID, status="failed", code=None),          # failed without a code: contradictory
        record_bytes("f" * 32, status="failed", cleanup="unconfirmed",
                     code="isolated_verification"),                # a different run
        record_bytes(RUN_ID, contract="house-helper-maintenance-status/2"),   # unknown version
        record_bytes(RUN_ID, status="maybe"),                      # unknown status
        record_bytes(RUN_ID, cleanup="probably"),                  # unknown cleanup
        record_bytes(RUN_ID, extra="unexpected"),                  # unexpected field
        b'{"contract":"house-helper-maintenance-status/1","contract":"house-helper-maintenance-status/1",'
        b'"command":"rehearse","run_id":"0123456789abcdef0123456789abcdef","status":"passed",'
        b'"cleanup":"confirmed","code":null}\n',                   # duplicate key
    ],
    ids=[
        "empty", "not-json", "truncated", "two-records", "passed-with-code", "failed-without-code",
        "wrong-run", "wrong-contract", "unknown-status", "unknown-cleanup", "extra-field", "duplicate-key",
    ],
)
def test_an_unusable_record_is_rejected(tmp_path, text):
    assert _status_reader(tmp_path, text) is None


def test_a_complete_record_is_accepted(tmp_path):
    assert _status_reader(tmp_path, record_bytes(RUN_ID)) == {
        "status": "passed", "cleanup": "confirmed", "code": None
    }


def test_a_compose_path_containing_an_artifact_token_cannot_create_a_verdict(tmp_path):
    """The exact R2 finding: the recovery command echoes an operator-supplied compose path.

    Before R3 the parent searched stderr for `underlying [...]` and found the token inside the
    path (`underlying [isolated_verification]`) before the real `underlying [docker_infrastructure]`
    line, so a Docker failure became NOT RESTORABLE. The path still appears in the diagnostics;
    the classification comes from the record alone.
    """
    world, _ = v2_world(tmp_path)
    compose = tmp_path / "underlying [isolated_verification]" / "compose.yaml"
    compose.parent.mkdir()
    compose.write_text("services: {}\n")
    _unconfirmed(world, code="docker_infrastructure")
    recovery = maintenance._rehearsal_cleanup_command(compose, RUN_ID)
    world.fake.restore_stderr = (
        f"refused [rehearsal_cleanup]: the disposable container could not be confirmed removed "
        f"({NAME}; remove it with: {recovery})\n".encode()
        + INFRASTRUCTURE_UNDERLYING
    )
    assert world.rehearse("--postgres-compose-file", str(compose), expect=("messages=1",)) == rehearsal.EXIT_FAILED
    assert "docker_infrastructure" in world.text
    assert "NOT RESTORABLE" not in world.text
    assert "PASSED" not in world.text


@posix_only
def test_a_compose_path_with_an_embedded_newline_and_a_fake_record_creates_nothing(tmp_path):
    world, _ = v2_world(tmp_path)
    fake = 'offsite-status/v1 {"contract":"house-helper-maintenance-status/1"}\n'
    compose = tmp_path / f"x{fake}underlying [isolated_verification]" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services: {}\n")
    _unconfirmed(world, code="docker_infrastructure")
    world.fake.restore_stderr = f"refused [rehearsal_cleanup] {compose}\n".encode()
    assert world.rehearse("--postgres-compose-file", str(compose), expect=("messages=1",)) == rehearsal.EXIT_FAILED
    assert "NOT RESTORABLE" not in world.text


def test_a_newline_and_record_delimiters_in_stderr_are_never_parsed(tmp_path):
    """The same content in the text channel (no path characters needed) changes nothing."""
    world, _ = v2_world(tmp_path)
    _unconfirmed(world, code="docker_infrastructure")
    fake = record_bytes(RUN_ID, status="failed", cleanup="unconfirmed", code="archive_integrity").decode()
    world.fake.restore_stderr = (
        "refused [rehearsal_cleanup]: injected\n" + fake + "underlying [isolated_verification]: injected\n"
    ).encode()
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    assert "NOT RESTORABLE" not in world.text


@pytest.mark.parametrize(
    "record, expected",
    [
        (_record(status="failed", code="archive_integrity"), rehearsal.EXIT_NOT_RESTORABLE),
        (_record(status="failed", code="isolation_verification"), rehearsal.EXIT_FAILED),
        (_record(status="failed", code="docker_infrastructure"), rehearsal.EXIT_FAILED),
        (_record(status="failed", code="internal"), rehearsal.EXIT_FAILED),
        (_record(status="cancelled", code=None), rehearsal.EXIT_FAILED),
        (b"malformed\n", rehearsal.EXIT_FAILED),
        (b"", rehearsal.EXIT_FAILED),
    ],
    ids=["artifact", "unknown-code", "infrastructure", "internal", "cancelled", "malformed", "empty"],
)
def test_only_an_artifact_code_at_the_refusal_status_is_not_restorable(tmp_path, record, expected):
    world, _ = v2_world(tmp_path)
    world.fake.restore_exit = 3
    world.fake.restore_record = record
    assert world.rehearse(expect=("messages=1",)) == expected, world.text
    assert ("NOT RESTORABLE" in world.text) == (expected == rehearsal.EXIT_NOT_RESTORABLE)


@pytest.mark.parametrize(
    "returncode, record, expected, fallback",
    [
        (-9, _record(status="passed", cleanup="confirmed"), rehearsal.EXIT_FAILED, True),    # killed
        (3, _record(status="passed", cleanup="confirmed"), rehearsal.EXIT_FAILED, False),    # status contradicts exit
        (0, _record(status="failed", code="archive_integrity"), rehearsal.EXIT_FAILED, False),  # refused but zero
        (4, _record(status="failed", cleanup="unconfirmed", code="archive_integrity"),
         rehearsal.EXIT_NOT_RESTORABLE, True),                                               # confirmed by fallback
    ],
    ids=["killed", "passed-but-refused", "refused-but-zero", "cleanup-unconfirmed-artifact"],
)
def test_contradictory_or_abnormal_outcomes_are_never_a_bare_pass(tmp_path, returncode, record, expected, fallback):
    world, _ = v2_world(tmp_path)
    world.fake.restore_exit = returncode
    world.fake.restore_record = record
    assert world.rehearse(expect=("messages=1",)) == expected, world.text
    assert len(world.fake.cleanup_calls) == (1 if fallback else 0)
    assert ("NOT RESTORABLE" in world.text) == (expected == rehearsal.EXIT_NOT_RESTORABLE)
    assert "PASSED" not in world.text
    assert world.scratch_is_empty()


def test_an_abnormal_termination_with_a_confirmed_record_still_retries_the_cleanup(tmp_path):
    """A helper killed by a signal cannot be trusted to have confirmed anything."""
    world, _ = v2_world(tmp_path)
    world.fake.restore_exit = -9
    world.fake.restore_record = _record()
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    assert len(world.fake.cleanup_calls) == 1


def test_a_failed_cleanup_with_malformed_status_evidence_is_failed_with_the_identity(tmp_path):
    world, _ = v2_world(tmp_path)
    world.fake.restore_exit = 4
    world.fake.restore_record = b"not a record\n"
    world.fake.cleanup_exit = 4
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    name = rehearsal.REHEARSAL_CONTAINER_PREFIX + world.fake.restore_run_id
    assert name in world.text and "could not be confirmed removed" in world.text
    assert "cleanup-rehearsal --run-id" in world.text
    assert "NOT RESTORABLE" not in world.text and "PASSED" not in world.text


def test_an_exit_zero_without_a_cleanup_record_is_not_a_confirmed_cleanup(tmp_path):
    world, _ = v2_world(tmp_path)
    _unconfirmed(world, status="passed", code=None)
    world.fake.cleanup_exit = 0
    world.fake.cleanup_record = b"not a record\n"
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    assert "could not be confirmed removed" in world.text
    assert "PASSED" not in world.text


def test_a_cleanup_record_for_another_run_is_not_a_confirmation(tmp_path):
    world, _ = v2_world(tmp_path)
    _unconfirmed(world, status="passed", code=None)
    world.fake.cleanup_record = _cleanup_record("f" * 32)
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    assert "could not be confirmed removed" in world.text


def test_a_confirmed_cleanup_record_and_exit_zero_does_not_invoke_the_fallback(tmp_path):
    world, _ = v2_world(tmp_path)
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_OK
    assert world.fake.cleanup_calls == []


def test_the_tool_is_asked_to_write_the_record_in_the_scratch_directory(tmp_path):
    world, _ = v2_world(tmp_path)
    world.fake.restore_exit = 3
    world.fake.restore_status, world.fake.restore_code = "failed", "archive_integrity"
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_NOT_RESTORABLE
    argv = world.fake.maintenance_calls[0]
    status = Path(argv[argv.index("--status-file") + 1])
    assert status.parent == world.scratch or world.scratch in status.parents   # ephemeral, in the scratch area
    assert not status.exists()                                                # removed with it


# --- R3-F1: a signal that lands inside the verified removal (offline) ---------------------------
#
# The removal is driven by a stub that raises the signal at exactly that point and then returns, so
# the deferral, the preserved cancellation and the record can be checked on every platform. The
# real POSIX probes below do the same through a real process and a real SIGTERM.


class _SignalInsideRemoval(maintenance.ToolContainers):
    def __init__(self, image, *, confirmed=True, signums=(15,)):
        super().__init__(image)
        self.confirmed = confirmed
        self.signums = signums

    def remove_verified(self, name, *, image=None):
        for signum in self.signums:
            signal.raise_signal(signum)
        return self.confirmed


def _rehearse_argv(monkeypatch, tmp_path, factory, status):
    from tests.test_deploy_postgres_rehearsal_cleanup import _verified_rehearsal

    argv = _verified_rehearsal(monkeypatch, tmp_path, factory)
    return argv + ["--status-file", str(status)]


def test_a_signal_inside_the_verified_removal_is_deferred_and_preserved(monkeypatch, tmp_path):
    status = tmp_path / "status.json"
    argv = _rehearse_argv(monkeypatch, tmp_path, lambda image: _SignalInsideRemoval(image), status)
    assert maintenance.main(argv) == maintenance.EX_CANCELLED
    record = json.loads(status.read_text())
    assert record["status"] == "cancelled" and record["cleanup"] == "confirmed"


def test_repeated_signals_inside_the_verified_removal_still_confirm_the_cleanup(monkeypatch, tmp_path):
    status = tmp_path / "status.json"
    argv = _rehearse_argv(
        monkeypatch, tmp_path, lambda image: _SignalInsideRemoval(image, signums=(15, 15, 2)), status
    )
    assert maintenance.main(argv) == maintenance.EX_CANCELLED
    assert json.loads(status.read_text())["cleanup"] == "confirmed"


def test_a_signal_inside_an_unconfirmed_removal_is_the_canonical_refusal(monkeypatch, tmp_path, capsys):
    status = tmp_path / "status.json"
    argv = _rehearse_argv(
        monkeypatch, tmp_path, lambda image: _SignalInsideRemoval(image, confirmed=False), status
    )
    assert maintenance.main(argv) == maintenance.EX_UNAVAILABLE
    errors = capsys.readouterr().err
    first = re.search(r"refused \[([a-z0-9_]+)\]", errors)
    assert first is not None and first.group(1) == "rehearsal_cleanup"
    assert "underlying [cancelled]" in errors and NAME in errors
    record = json.loads(status.read_text())
    assert record["status"] == "cancelled" and record["cleanup"] == "unconfirmed"


def test_a_signal_inside_the_fallback_removal_does_not_abandon_it(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: IMAGE)
    monkeypatch.setattr(
        maintenance, "ToolContainers", lambda image: _SignalInsideRemoval(image, signums=(15, 15))
    )
    status = tmp_path / "status.json"
    assert maintenance.main(
        ["cleanup-rehearsal", "--run-id", RUN_ID, "--status-file", str(status)]
    ) == maintenance.EX_OK
    record = json.loads(status.read_text())
    assert record["command"] == "cleanup-rehearsal"
    assert record["status"] == "passed" and record["cleanup"] == "confirmed"


# --- R3-F1: real POSIX signals around the verified removal --------------------------------------
#
# These probes run the REAL `deploy/postgres/maintenance.py` process (native on POSIX, through WSL
# on Windows) with a controlled fake `docker` on PATH. Only the archive parsing is stubbed: the
# process, the signal handlers, the detached-container start, the work directory and the verified
# removal are the ones that ship. No real container, daemon or credential is involved.

from tests.test_deploy_postgres_rehearsal_cleanup import (  # noqa: E402 (probe section)
    PINNED_IMAGE,
    _linux_path,
    _linux_shell,
)

PROBE_DOCKER = r'''#!/usr/bin/env python3
"""A controlled `docker` for the F1 probes: only what ToolContainers uses, all under STATE."""
import os, pathlib, sys, time

state = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
state.mkdir(parents=True, exist_ok=True)
with (state / "log").open("a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")

def present():
    marker = state / "present"
    return marker.read_text().split() if marker.exists() else None

argv = sys.argv[1:]
if argv[:1] == ["run"] and "-d" in argv:
    name = argv[argv.index("--name") + 1]
    (state / "present").write_text("c" * 64 + " " + name)
    print("c" * 64)
    sys.exit(0)
if argv[:1] == ["run"]:
    (state / "blocked").write_text("1")
    time.sleep(float(os.environ.get("FAKE_DOCKER_BLOCK_SECONDS", "30")))
    sys.exit(0)
if argv[:2] == ["container", "ls"]:
    if os.environ.get("FAKE_DOCKER_BLOCK_CLEANUP") and not (state / "cleanup_blocked").exists():
        (state / "cleanup_blocked").write_text("1")
        time.sleep(0.8)
    if os.environ.get("FAKE_DOCKER_UNCONFIRMED"):
        sys.exit(1)
    current = present()
    wanted = argv[argv.index("--filter") + 1]
    if current and wanted == "name=^/" + current[1] + "$":
        print(current[0])
    sys.exit(0)
if argv[:1] == ["inspect"]:
    print((state / "image").read_text().strip())
    sys.exit(0)
if argv[:2] == ["rm", "-f"]:
    current = present()
    if current and current[0] == argv[2]:
        (state / "present").unlink()
    sys.exit(0)
sys.exit(99)
'''

PROBE_DRIVER = r'''#!/usr/bin/env python3
"""The REAL maintenance.py `rehearse` command, with only the archive parsing and the restore stood
in for. `PROBE_MODE` decides what the stood-in restore does before the real `finally` cleanup."""
import os, sys
from pathlib import Path
from types import SimpleNamespace

repo, archive, fixture_path, work, compose = map(Path, sys.argv[1:6])
run_id = sys.argv[6]
status_file = Path(sys.argv[7])
mode = os.environ.get("PROBE_MODE", "complete")
sys.path.insert(0, str(repo))
from deploy.postgres import maintenance  # noqa: E402
from house_helper.persistence import pg_backup as backup  # noqa: E402

fixture = backup.load_object_contract_fixture(fixture_path)
point = SimpleNamespace(manifest={"alembic_revision": fixture["revision"], "tables": {}})
maintenance.validate_archive = lambda path, tools, work=None: (point, ())
backup.toc_contract_failures = lambda entries, fixture: ()

def fake_rehearse(tools, *, point, work, fixture, app_image, run_id, keep=False):
    tools.start_detached(["postgres"], name=maintenance._rehearsal_container_name(run_id), network="none")
    if mode in ("timeout", "kill"):
        tools.run(["sleep"], timeout=1 if mode == "timeout" else 60)
    return {
        "tables": 0, "revision": "probe", "system_identifier": "1",
        "attachments": {"count": 0, "bytes": 0, "declared_bytes": 0, "sample": False},
    }

maintenance.rehearse = fake_rehearse
raise SystemExit(maintenance.main([
    "--compose-file", str(compose), "--object-contract", str(fixture_path),
    "--work-dir", str(work), "rehearse", "--run-id", run_id, "--status-file", str(status_file),
    str(archive),
]))
'''


def _probe_rig(tmp_path: Path) -> dict:
    probe = tmp_path / "probe"
    (probe / "fakebin").mkdir(parents=True)
    fake_docker = probe / "fakebin" / "docker"
    fake_docker.write_bytes(PROBE_DOCKER.encode("utf-8"))    # LF: WSL executes the shebang
    fake_docker.chmod(0o755)
    state = probe / "state"
    state.mkdir()
    (state / "image").write_text(PINNED_IMAGE + "\n")
    driver = probe / "driver.py"
    driver.write_bytes(PROBE_DRIVER.encode("utf-8"))
    work = probe / "work"
    work.mkdir()
    archive = probe / "point.tar"
    archive.write_bytes(b"read by the stood-in validator only")
    return {
        "probe": probe,
        "state": state,
        "driver": driver,
        "work": work,
        "archive": archive,
        "compose": REPO_ROOT / "deploy" / "compose.postgres.yaml",
        "fixture": REPO_ROOT / "tests" / "fixtures" / "pg_object_contract.json",
        "status": probe / "status.json",
    }


def _run_probe(rig: dict, *, wait_for: Path, signal_script: str = "", env: str = "", mode: str = "complete"):
    p = {name: (_linux_path(value) if isinstance(value, Path) else value) for name, value in rig.items()}
    p["wait_for"] = _linux_path(wait_for)
    script = f'''
set -u
cd "{p["probe"]}"
PROBE_MODE="{mode}" FAKE_DOCKER_STATE="{p["state"]}" {env} PATH="{_linux_path(rig["probe"] / "fakebin")}:$PATH" \\
  python3 "{p["driver"]}" "{_linux_path(REPO_ROOT)}" "{p["archive"]}" "{p["fixture"]}" \\
  "{p["work"]}" "{p["compose"]}" "{RUN_ID}" "{p["status"]}" > out.log 2> err.log &
pid=$!
i=0
while [ ! -f "{p["wait_for"]}" ] && [ $i -lt 300 ]; do i=$((i+1)); sleep 0.1; done
if [ ! -f "{p["wait_for"]}" ]; then echo "NO-BLOCK"; kill -9 $pid 2>/dev/null; wait $pid; exit 90; fi
{signal_script}
wait $pid
echo "EXIT=$?"
if [ -f "{p["state"]}/present" ]; then echo "REMAINS"; else echo "GONE"; fi
'''
    return subprocess.run(_linux_shell() + [script], capture_output=True, timeout=180, check=False)


def _status_record(rig: dict) -> dict:
    return json.loads(rig["status"].read_text())


def test_a_real_sigterm_during_the_verified_removal_is_deferred_and_confirmed(tmp_path):
    """R3-F1's exact reproduction: the signal arrives inside `remove_verified()`.

    Before R3 the `_raise_cancelled` handler fired inside the removal, escaped the `finally`, left
    the container behind and made the helper exit 4 with a `warning` line. Now the removal
    completes, its absence is confirmed, and the cancellation is reported afterwards as exit 6.
    """
    rig = _probe_rig(tmp_path)
    done = _run_probe(rig, wait_for=rig["state"] / "cleanup_blocked",
                      signal_script="kill -TERM $pid", env='FAKE_DOCKER_BLOCK_CLEANUP="1"')
    output = done.stdout.decode("utf-8", "replace")
    assert "NO-BLOCK" not in output, output + done.stderr.decode("utf-8", "replace")
    assert "EXIT=6" in output and "GONE" in output and "REMAINS" not in output, output
    errors = (rig["probe"] / "err.log").read_text()
    assert "cancelled [rehearsal_cleanup]" in errors and "Traceback" not in errors
    assert _status_record(rig)["status"] == "cancelled" and _status_record(rig)["cleanup"] == "confirmed"


def test_repeated_signals_during_the_verified_removal_do_not_abandon_it(tmp_path):
    rig = _probe_rig(tmp_path)
    done = _run_probe(rig, wait_for=rig["state"] / "cleanup_blocked",
                      signal_script="kill -TERM $pid; sleep 0.2; kill -TERM $pid; sleep 0.1; kill -INT $pid",
                      env='FAKE_DOCKER_BLOCK_CLEANUP="1"')
    output = done.stdout.decode("utf-8", "replace")
    assert "EXIT=6" in output and "GONE" in output, output + done.stderr.decode("utf-8", "replace")
    errors = (rig["probe"] / "err.log").read_text()
    assert "Traceback" not in errors
    assert _status_record(rig)["cleanup"] == "confirmed"


def test_a_signal_during_an_unconfirmable_removal_is_the_canonical_refusal(tmp_path):
    """The signal lands in the removal AND the listing cannot confirm the absence: exit 4 with the
    exact name and recovery command, never a `warning` that the parent cannot classify."""
    rig = _probe_rig(tmp_path)
    done = _run_probe(rig, wait_for=rig["state"] / "cleanup_blocked",
                      signal_script="kill -TERM $pid",
                      env='FAKE_DOCKER_BLOCK_CLEANUP="1" FAKE_DOCKER_UNCONFIRMED="1"')
    output = done.stdout.decode("utf-8", "replace")
    assert "EXIT=4" in output and "REMAINS" in output, output + done.stderr.decode("utf-8", "replace")
    errors = (rig["probe"] / "err.log").read_text()
    first = re.search(r"refused \[([a-z0-9_]+)\]", errors)
    assert first is not None and first.group(1) == "rehearsal_cleanup"
    assert "underlying [cancelled]" in errors
    assert NAME in errors and "cleanup-rehearsal --run-id " + RUN_ID in errors
    assert "Traceback" not in errors
    record = _status_record(rig)
    assert record["status"] == "cancelled" and record["cleanup"] == "unconfirmed"


def test_a_timeout_inside_the_rehearsal_still_runs_the_verified_removal(tmp_path):
    rig = _probe_rig(tmp_path)
    done = _run_probe(rig, wait_for=rig["state"] / "blocked", mode="timeout")
    output = done.stdout.decode("utf-8", "replace")
    assert "GONE" in output and "REMAINS" not in output, output + done.stderr.decode("utf-8", "replace")
    errors = (rig["probe"] / "err.log").read_text()
    assert "refused [docker_timeout]" in errors and "Traceback" not in errors
    record = _status_record(rig)
    assert record["status"] == "failed" and record["code"] == "docker_timeout"
    assert record["cleanup"] == "confirmed"


def test_a_sigkill_leaves_the_container_and_the_fallback_command_removes_it(tmp_path):
    rig = _probe_rig(tmp_path)
    done = _run_probe(rig, wait_for=rig["state"] / "blocked",
                      signal_script="kill -9 $pid", mode="kill",
                      env='FAKE_DOCKER_BLOCK_SECONDS="5"')
    output = done.stdout.decode("utf-8", "replace")
    assert "REMAINS" in output, output + done.stderr.decode("utf-8", "replace")
    assert "GONE" not in output
    # No helper left to write a record: the absence is NOT claimed.
    assert not rig["status"].exists() or _status_record(rig).get("cleanup") != "confirmed"
    fallback = subprocess.run(_linux_shell() + [f'''
set -u
cd "{_linux_path(rig["probe"])}"
FAKE_DOCKER_STATE="{_linux_path(rig["state"])}" PATH="{_linux_path(rig["probe"] / "fakebin")}:$PATH" \\
  python3 "{_linux_path(REPO_ROOT / "deploy" / "postgres" / "maintenance.py")}" \\
  --compose-file "{_linux_path(REPO_ROOT / "deploy" / "compose.postgres.yaml")}" \\
  cleanup-rehearsal --run-id "{RUN_ID}" --status-file "{_linux_path(rig["status"])}"
echo "EXIT=$?"
if [ -f "{_linux_path(rig["state"] / "present")}" ]; then echo REMAINS; else echo GONE; fi
'''], capture_output=True, timeout=120, check=False)
    text = fallback.stdout.decode("utf-8", "replace")
    assert "EXIT=0" in text and "GONE" in text, text + fallback.stderr.decode("utf-8", "replace")
    assert _status_record(rig)["command"] == "cleanup-rehearsal"
    assert _status_record(rig)["status"] == "passed" and _status_record(rig)["cleanup"] == "confirmed"