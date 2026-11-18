"""WP-2E R1 (F2): a cancelled PostgreSQL rehearsal removes exactly its own disposable container.

The first half is offline: the `ToolContainers.remove_verified` contract and the
`cleanup-rehearsal` command, against a fake Docker command boundary. The second half is the
Linux/POSIX probe the finding asks for: the REAL `deploy/postgres/maintenance.py rehearse`
process is started through a real process boundary (locally on POSIX, or through WSL on
Windows) with a controlled fake `docker` on PATH, is sent a real SIGTERM while it is blocked
inside a container command, and must remove its detached container and exit 6.

Nothing here starts a real container, touches a real Docker daemon or writes outside
pytest's tmp_path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.postgres import maintenance
from tests.deploy_support import REPO_ROOT

IMAGE = "postgres@sha256:" + "a" * 64
# The image the REAL `_image_from_compose` reads from the checked-in compose file: the probe's fake
# Docker must answer `inspect` with it, or the identity check refuses (which is the point of it).
PINNED_IMAGE = maintenance._image_from_compose(REPO_ROOT / "deploy" / "compose.postgres.yaml")
RUN_ID = "0123456789abcdef0123456789abcdef"
NAME = maintenance.REHEARSAL_CONTAINER_PREFIX + RUN_ID


# --- the removal contract, offline -------------------------------------------------------------


class FakeDocker:
    """The `docker` calls `ToolContainers` may make, as compact as the production uses them."""

    def __init__(self, containers=None, *, listing_fails=False, image=IMAGE):
        self.containers = dict(containers or {})  # id -> name
        self.listing_fails = listing_fails
        self.image = image
        self.calls = []

    def __call__(self, argv, *, input=None, capture_output=True, timeout=120, check=False, env=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["docker", "container", "ls"]:
            if self.listing_fails:
                return subprocess.CompletedProcess(argv, 1, b"", b"cannot list")
            wanted = argv[argv.index("--filter") + 1]
            assert wanted.startswith("name=^/") and wanted.endswith("$"), wanted  # anchored, exact
            match = wanted[len("name=^/"):-1]
            ids = [cid for cid, name in self.containers.items() if name == match]
            return subprocess.CompletedProcess(argv, 0, ("".join(cid + "\n" for cid in ids)).encode(), b"")
        if argv[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, (self.image + "\n").encode(), b"")
        if argv[:3] == ["docker", "rm", "-f"]:
            self.containers.pop(argv[3], None)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 99, b"", b"unexpected")


def containers(fake: FakeDocker) -> maintenance.ToolContainers:
    return maintenance.ToolContainers(IMAGE, runner=fake)


def test_removal_is_by_the_exact_run_name_and_is_confirmed():
    fake = FakeDocker({"c" * 64: NAME})
    tools = containers(fake)
    assert tools.present_container(NAME) == "c" * 64
    assert tools.remove_verified(NAME, image=IMAGE) is True
    assert fake.containers == {}
    # the explicit look, then removal's own look / identity check / removal / confirmation
    assert [call[1] for call in fake.calls if call[0] == "docker"] == [
        "container", "container", "inspect", "rm", "container",
    ]


def test_an_absent_container_is_already_clean():
    fake = FakeDocker()
    assert containers(fake).remove_verified(NAME, image=IMAGE) is True
    assert not any(call[2:3] == ["rm"] for call in fake.calls)


def test_a_container_squatting_the_name_with_another_image_is_never_removed():
    fake = FakeDocker({"c" * 64: NAME}, image="postgres@sha256:" + "b" * 64)
    assert containers(fake).remove_verified(NAME, image=IMAGE) is False
    assert fake.containers == {"c" * 64: NAME}                 # untouched
    assert not any(call[:3] == ["docker", "rm", "-f"] for call in fake.calls)


def test_a_listing_that_fails_is_never_read_as_gone():
    fake = FakeDocker({"c" * 64: NAME}, listing_fails=True)
    assert containers(fake).remove_verified(NAME, image=IMAGE) is False
    assert fake.containers == {"c" * 64: NAME}


@pytest.mark.parametrize("run_id", ["", "ABC", "0123456789abcde", "z" * 32, "a" * 65, "a" * 32 + "\n"])
def test_a_run_id_that_is_not_lowercase_hex_is_refused(run_id):
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance._rehearsal_container_name(run_id)
    assert refused.value.code == "rehearsal_identity"


def test_a_valid_run_id_names_exactly_one_container():
    assert maintenance._rehearsal_container_name(RUN_ID) == NAME


def test_the_rehearsal_and_the_tool_agree_on_the_container_prefix():
    # The rehearsal never removes a container itself: it names it in a failure message and asks the
    # tool to remove it. The two spellings must be one.
    import deploy.offsite_rehearsal as rehearsal

    assert rehearsal.REHEARSAL_CONTAINER_PREFIX == maintenance.REHEARSAL_CONTAINER_PREFIX


def _install(monkeypatch, fake):
    real = maintenance.ToolContainers
    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: IMAGE)
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: real(image, runner=fake))


def test_the_cleanup_command_reports_success_only_when_the_removal_is_confirmed(monkeypatch):
    fake = FakeDocker({"c" * 64: NAME})
    _install(monkeypatch, fake)
    assert maintenance.main(["cleanup-rehearsal", "--run-id", RUN_ID]) == maintenance.EX_OK
    assert fake.containers == {}


def test_the_cleanup_command_returns_unavailable_when_it_cannot_confirm(monkeypatch):
    fake = FakeDocker({"c" * 64: NAME}, listing_fails=True)
    _install(monkeypatch, fake)
    assert maintenance.main(["cleanup-rehearsal", "--run-id", RUN_ID]) == maintenance.EX_UNAVAILABLE


def test_the_cleanup_command_refuses_a_malformed_run_id_as_usage(monkeypatch):
    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: IMAGE)
    assert maintenance.main(["cleanup-rehearsal", "--run-id", "../x"]) == maintenance.EX_USAGE


class _Unconfirmed(maintenance.ToolContainers):
    """Every removal is unconfirmed: the stub for the states the fix must never call a pass."""

    def remove_verified(self, name, *, image=None):
        return False


def _rehearse_argv(tmp_path):
    return [
        "--compose-file", str(tmp_path / "compose.yaml"),
        "--work-dir", str(tmp_path),
        "rehearse", str(tmp_path / "point.tar"),
    ]


def test_a_completion_whose_removal_cannot_be_confirmed_is_not_reported_as_success(monkeypatch, tmp_path):
    argv = _verified_rehearsal(
        monkeypatch, tmp_path, lambda image: _Unconfirmed(image, runner=FakeDocker())
    )
    assert maintenance.main(argv) == maintenance.EX_UNAVAILABLE


def test_a_cancellation_whose_removal_cannot_be_confirmed_exits_4_and_names_the_container(monkeypatch, tmp_path, capsys):
    def cancelled(*args, **kwargs):
        raise maintenance.RehearsalCancelled(15)

    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: IMAGE)
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: _Unconfirmed(image, runner=FakeDocker()))
    monkeypatch.setattr(maintenance, "validate_archive", cancelled)
    assert maintenance.main(_rehearse_argv(tmp_path)) == maintenance.EX_UNAVAILABLE
    shown = capsys.readouterr().err
    assert maintenance.REHEARSAL_CONTAINER_PREFIX in shown and "could not be confirmed removed" in shown


# --- R2-F2: the clean-up outcome is resolved before the rehearsal outcome -----------------------
#
# The R1 fix ran the verified removal on every path, but a rehearsal exception still propagated
# before the clean-up result was checked, so an unconfirmed removal could be suppressed by the
# archive verdict. These tests hold the precedence: the clean-up is the dominant fact, the original
# reason survives only as diagnostics, and the exact container name plus the safe recovery command
# are always available.

_RECOVERY_HEADING = "refused [rehearsal_cleanup]"


def _raise_maintenance(code: str, message: str):
    def refuse(*args, **kwargs):
        raise maintenance.MaintenanceError(code, message)

    return refuse


def test_a_verification_refusal_cannot_suppress_an_unconfirmed_cleanup(monkeypatch, tmp_path, capsys):
    """The exact reproduction: isolated_verification + remove_verified() returns False."""
    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: IMAGE)
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: _Unconfirmed(image, runner=FakeDocker()))
    monkeypatch.setattr(
        maintenance, "validate_archive",
        _raise_maintenance(
            "isolated_verification",
            "the isolated restored database did not match the archive",
        ),
    )
    argv = _rehearse_argv(tmp_path) + ["--run-id", RUN_ID]
    assert maintenance.main(argv) == maintenance.EX_UNAVAILABLE
    errors = capsys.readouterr().err
    first = re.search(r"refused \[([a-z0-9_]+)\]", errors)
    assert first is not None and first.group(1) == "rehearsal_cleanup"   # precedence, not the verdict
    # The original reason survives as diagnostics, in structured form, and cannot be the verdict.
    assert "underlying [isolated_verification]: the isolated restored database did not match the archive" in errors
    # The run-owned identity and the safe recovery command are both present.
    assert NAME in errors
    assert "cleanup-rehearsal --run-id " + RUN_ID in errors
    assert "Traceback" not in errors


def test_a_verification_refusal_stands_when_the_cleanup_is_confirmed(monkeypatch, tmp_path, capsys):
    """Verification fails, clean-up succeeds: the original artifact classification is unchanged."""
    _install(monkeypatch, FakeDocker())          # no container: the removal is already confirmed
    monkeypatch.setattr(
        maintenance, "validate_archive",
        _raise_maintenance(
            "isolated_verification",
            "the isolated restored database did not match the archive",
        ),
    )
    assert maintenance.main(_rehearse_argv(tmp_path) + ["--run-id", RUN_ID]) == maintenance.EX_REFUSED
    errors = capsys.readouterr().err
    assert "refused [isolated_verification]" in errors
    assert "rehearsal_cleanup" not in errors
    assert "underlying [" not in errors


def _verified_rehearsal(monkeypatch, tmp_path, tools_factory):
    fixture = maintenance.backup.load_object_contract_fixture(
        REPO_ROOT / "tests" / "fixtures" / "pg_object_contract.json"
    )
    point = SimpleNamespace(manifest={"alembic_revision": fixture["revision"], "tables": {}})
    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: IMAGE)
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: tools_factory(image))
    monkeypatch.setattr(maintenance, "validate_archive", lambda *a, **k: (point, ()))
    monkeypatch.setattr(maintenance.backup, "toc_contract_failures", lambda *a, **k: ())
    monkeypatch.setattr(
        maintenance, "rehearse",
        lambda *a, **k: {
            "tables": 0, "revision": fixture["revision"], "system_identifier": "1",
            "attachments": {"count": 0, "bytes": 0, "declared_bytes": 0, "sample": False},
        },
    )
    return _rehearse_argv(tmp_path) + ["--run-id", RUN_ID]


def test_a_verified_rehearsal_whose_cleanup_is_unconfirmed_is_an_explicit_failure(monkeypatch, tmp_path, capsys):
    """Verification succeeds, clean-up fails: an explicit unavailable outcome, never a pass."""
    argv = _verified_rehearsal(
        monkeypatch, tmp_path, lambda image: _Unconfirmed(image, runner=FakeDocker())
    )
    assert maintenance.main(argv) == maintenance.EX_UNAVAILABLE
    errors = capsys.readouterr().err
    assert _RECOVERY_HEADING in errors
    assert "underlying [" not in errors          # there is no underlying failure to preserve
    assert NAME in errors
    assert "cleanup-rehearsal --run-id " + RUN_ID in errors
    assert "ok [rehearse]" not in errors


def test_an_infrastructure_failure_and_an_unconfirmed_cleanup_keep_the_recovery_identity(monkeypatch, tmp_path, capsys):
    """F2 + F3: an infrastructure error plus an unconfirmed clean-up is still exit 4, never a verdict."""
    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: IMAGE)
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: _Unconfirmed(image, runner=FakeDocker()))
    monkeypatch.setattr(
        maintenance, "validate_archive",
        _raise_maintenance(
            "docker_infrastructure",
            "the archive listing could not be carried out by Docker",
        ),
    )
    assert maintenance.main(_rehearse_argv(tmp_path) + ["--run-id", RUN_ID]) == maintenance.EX_UNAVAILABLE
    errors = capsys.readouterr().err
    first = re.search(r"refused \[([a-z0-9_]+)\]", errors)
    assert first is not None and first.group(1) == "rehearsal_cleanup"
    assert "underlying [docker_infrastructure]" in errors
    assert NAME in errors
    assert "cleanup-rehearsal --run-id " + RUN_ID in errors


# --- the Linux/POSIX signal probe --------------------------------------------------------------

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""A controlled `docker` for the probe: only the commands ToolContainers uses, all under STATE."""
import os, pathlib, sys, time

state = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
state.mkdir(parents=True, exist_ok=True)
log = state / "log"
log.open("a").write(" ".join(sys.argv[1:]) + "\n")

def present():
    marker = state / "present"
    return marker.read_text().split() if marker.exists() else None

argv = sys.argv[1:]
if argv[:1] == ["run"]:
    name = argv[argv.index("--name") + 1]
    (state / "present").write_text("c" * 64 + " " + name)
    print("c" * 64)
    sys.exit(0)
if argv[:1] == ["exec"]:
    (state / "blocked").write_text("1")
    time.sleep(300)
    sys.exit(0)
if argv[:2] == ["container", "ls"]:
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
"""Run the REAL maintenance.py `rehearse` command, with only the archive parsing stood in for.

Everything that matters to F2 is real: the process, the signal handlers, the container start
through the `docker` on PATH, the work directory and the verified removal.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

repo, archive, fixture_path, work, compose = map(Path, sys.argv[1:6])
run_id = sys.argv[6] if len(sys.argv) > 6 else None
sys.path.insert(0, str(repo))
from deploy.postgres import maintenance  # noqa: E402
from house_helper.persistence import pg_backup as backup  # noqa: E402

fixture = backup.load_object_contract_fixture(fixture_path)
point = SimpleNamespace(manifest={"alembic_revision": fixture["revision"], "tables": {}})
maintenance.validate_archive = lambda path, tools, work=None: (point, ())
backup.toc_contract_failures = lambda entries, fixture: ()
extra = ["--run-id", run_id] if run_id else []
raise SystemExit(maintenance.main([
    "--compose-file", str(compose),
    "--object-contract", str(fixture_path),
    "--work-dir", str(work),
    "rehearse", *extra, str(archive),
]))
'''


def _linux_shell() -> list:
    if os.name == "posix":
        return ["sh", "-c"]
    executable = shutil.which("wsl.exe")
    if executable is None:
        pytest.skip("a real Linux shell (WSL) is unavailable")
    return [executable, "-e", "sh", "-c"]


def _linux_path(path: Path) -> str:
    value = Path(path).resolve()
    if os.name == "posix":
        return str(value)
    drive = value.drive.rstrip(":")
    if len(drive) != 1:
        pytest.skip("the Linux probe needs a local drive path visible to WSL")
    return "/mnt/" + drive.lower() + value.as_posix()[2:]


def _probe(script: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(_linux_shell() + [script], capture_output=True, timeout=timeout, check=False)


def test_sigterm_during_the_restore_makes_the_real_helper_remove_its_container(tmp_path):
    """The original reproduction: SIGTERM while the helper is blocked in a container command.

    Before the fix the default disposition killed the process between bytecodes, its `finally`
    never ran, and the detached container stayed. Now the helper catches the signal, removes the
    one container named by its run id, confirms it is gone, and exits 6.
    """
    probe = tmp_path / "probe"
    (probe / "fakebin").mkdir(parents=True)
    fake_docker = probe / "fakebin" / "docker"
    fake_docker.write_bytes(FAKE_DOCKER.encode("utf-8"))   # LF: WSL executes the shebang
    fake_docker.chmod(0o755)
    state = probe / "state"
    state.mkdir()
    (state / "image").write_text(PINNED_IMAGE + "\n")
    driver = probe / "driver.py"
    driver.write_bytes(PROBE_DRIVER.encode("utf-8"))
    work = probe / "work"
    work.mkdir()
    archive = probe / "point.tar"
    archive.write_bytes(b"not read before the signal")
    compose = REPO_ROOT / "deploy" / "compose.postgres.yaml"
    fixture = REPO_ROOT / "tests" / "fixtures" / "pg_object_contract.json"

    script = f'''
set -u
cd "{_linux_path(probe)}"
FAKE_DOCKER_STATE="{_linux_path(state)}" PATH="{_linux_path(probe / "fakebin")}:$PATH" \
  python3 "{_linux_path(driver)}" "{_linux_path(REPO_ROOT)}" "{_linux_path(archive)}" \
  "{_linux_path(fixture)}" "{_linux_path(work)}" "{_linux_path(compose)}" > out.log 2> err.log &
pid=$!
i=0
while [ ! -f "{_linux_path(state / "blocked")}" ] && [ $i -lt 200 ]; do i=$((i+1)); sleep 0.1; done
if [ ! -f "{_linux_path(state / "blocked")}" ]; then echo "NO-BLOCK"; kill -9 $pid 2>/dev/null; wait $pid; exit 90; fi
kill -TERM $pid
wait $pid
echo "EXIT=$?"
if [ -f "{_linux_path(state / "present")}" ]; then echo "REMAINS"; else echo "GONE"; fi
cat "{_linux_path(state / "log")}"
'''
    done = _probe(script)
    output = done.stdout.decode("utf-8", "replace")
    assert "NO-BLOCK" not in output, output + done.stderr.decode("utf-8", "replace")
    assert "EXIT=6" in output, output + done.stderr.decode("utf-8", "replace")
    assert "GONE" in output and "REMAINS" not in output
    assert "rm -f cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" in output
    # The probe's own error log must show the cancellation, never a traceback.
    errors = (probe / "err.log").read_text()
    assert "cancelled [rehearsal_cleanup]" in errors and "Traceback" not in errors


def test_sigterm_with_an_unconfirmed_cleanup_exits_4_with_the_exact_identity(tmp_path):
    """The R2 precedence, through a real POSIX process and a real SIGTERM: the signal outcome is
    captured, the verified removal still runs, and its failure is the one that is reported."""
    probe = tmp_path / "probe"
    (probe / "fakebin").mkdir(parents=True)
    fake_docker = probe / "fakebin" / "docker"
    fake_docker.write_bytes(FAKE_DOCKER.encode("utf-8"))   # LF: WSL executes the shebang
    fake_docker.chmod(0o755)
    state = probe / "state"
    state.mkdir()
    (state / "image").write_text(PINNED_IMAGE + "\n")
    driver = probe / "driver.py"
    driver.write_bytes(PROBE_DRIVER.encode("utf-8"))
    work = probe / "work"
    work.mkdir()
    archive = probe / "point.tar"
    archive.write_bytes(b"not read before the signal")
    compose = REPO_ROOT / "deploy" / "compose.postgres.yaml"
    fixture = REPO_ROOT / "tests" / "fixtures" / "pg_object_contract.json"

    script = f'''
set -u
cd "{_linux_path(probe)}"
FAKE_DOCKER_STATE="{_linux_path(state)}" FAKE_DOCKER_UNCONFIRMED=1 PATH="{_linux_path(probe / "fakebin")}:$PATH" \
  python3 "{_linux_path(driver)}" "{_linux_path(REPO_ROOT)}" "{_linux_path(archive)}" \
  "{_linux_path(fixture)}" "{_linux_path(work)}" "{_linux_path(compose)}" "{RUN_ID}" > out.log 2> err.log &
pid=$!
i=0
while [ ! -f "{_linux_path(state / "blocked")}" ] && [ $i -lt 200 ]; do i=$((i+1)); sleep 0.1; done
if [ ! -f "{_linux_path(state / "blocked")}" ]; then echo "NO-BLOCK"; kill -9 $pid 2>/dev/null; wait $pid; exit 90; fi
kill -TERM $pid
wait $pid
echo "EXIT=$?"
if [ -f "{_linux_path(state / "present")}" ]; then echo "REMAINS"; else echo "GONE"; fi
'''
    done = _probe(script)
    output = done.stdout.decode("utf-8", "replace")
    assert "NO-BLOCK" not in output, output + done.stderr.decode("utf-8", "replace")
    # The removal could not be confirmed, so the signal cannot be an exit 6 (or a verdict).
    assert "EXIT=4" in output, output + done.stderr.decode("utf-8", "replace")
    assert "REMAINS" in output                       # the fake never removed it
    errors = (probe / "err.log").read_text()
    first = re.search(r"refused \[([a-z0-9_]+)\]", errors)
    assert first is not None and first.group(1) == "rehearsal_cleanup"
    assert "underlying [cancelled]" in errors
    assert NAME in errors                            # the exact run-owned identity
    assert "cleanup-rehearsal --run-id " + RUN_ID in errors
    assert "cancelled [rehearsal_cleanup]" not in errors
    assert "Traceback" not in errors


def test_the_fallback_cleanup_removes_the_exact_name_and_nothing_else(tmp_path):
    """`cleanup-rehearsal` is the path a SIGKILLed helper leaves to the operator: exact name only."""
    probe = tmp_path / "probe"
    (probe / "state").mkdir(parents=True)
    (probe / "fakebin").mkdir()
    fake_docker = probe / "fakebin" / "docker"
    fake_docker.write_bytes(FAKE_DOCKER.encode("utf-8"))   # LF: WSL executes the shebang
    fake_docker.chmod(0o755)
    state = probe / "state"
    (state / "image").write_text(PINNED_IMAGE + "\n")
    # Two containers: ours, and one whose name merely starts with the same prefix.
    other = maintenance.REHEARSAL_CONTAINER_PREFIX + "f" * 32
    script = f'''
set -u
cd "{_linux_path(probe)}"
printf 'c%.0s' $(seq 1 64) > "{_linux_path(state / "present")}"
printf ' %s' "{NAME}" >> "{_linux_path(state / "present")}"
FAKE_DOCKER_STATE="{_linux_path(state)}" PATH="{_linux_path(probe / "fakebin")}:$PATH" \
  python3 "{_linux_path(REPO_ROOT / "deploy" / "postgres" / "maintenance.py")}" \
  --compose-file "{_linux_path(REPO_ROOT / "deploy" / "compose.postgres.yaml")}" \
  cleanup-rehearsal --run-id "{RUN_ID}"
echo "EXIT=$?"
if [ -f "{_linux_path(state / "present")}" ]; then cat "{_linux_path(state / "present")}"; else echo GONE; fi
'''
    done = _probe(script)
    output = done.stdout.decode("utf-8", "replace")
    assert "EXIT=0" in output, output + done.stderr.decode("utf-8", "replace")
    assert "GONE" in output
    # A container whose name is a different run id is untouched: the filter is anchored to ours.
    untouched = _probe(f'''
set -u
cd "{_linux_path(probe)}"
printf 'd%.0s' $(seq 1 64) > "{_linux_path(state / "present")}"
printf ' %s' "{other}" >> "{_linux_path(state / "present")}"
FAKE_DOCKER_STATE="{_linux_path(state)}" PATH="{_linux_path(probe / "fakebin")}:$PATH" \
  python3 "{_linux_path(REPO_ROOT / "deploy" / "postgres" / "maintenance.py")}" \
  --compose-file "{_linux_path(REPO_ROOT / "deploy" / "compose.postgres.yaml")}" \
  cleanup-rehearsal --run-id "{RUN_ID}"
echo "EXIT=$?"
cat "{_linux_path(state / "present")}"
''')
    text = untouched.stdout.decode("utf-8", "replace")
    assert "EXIT=0" in text, text + untouched.stderr.decode("utf-8", "replace")
    assert other in text  # never touched
