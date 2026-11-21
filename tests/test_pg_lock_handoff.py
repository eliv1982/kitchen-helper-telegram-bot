"""HOF-03 item 4: the journaled lock handoff (event `handoff:lock`).

The frozen specification (§7.4) requires the re-arm to observe that the web process holds
the shared state-volume lock (`dbtool check-idle` refuses) and that no exclusive holder
remains, with the journal recording `handoff:lock`.

The proof is not a successful acquisition probe: a probe lock can be taken while an
exclusive holder is briefly absent and then released again, which says nothing about the
web process. These tests therefore drive the kernel-level reader in
`deploy.postgres.lock_owner`: the validated web process (container cgroup + the deployed
image's real uvicorn argv) must carry a `FLOCK ... READ` record on its own descriptor for
the lock file, and that record's device/inode must be the validated lock file.

Option A adds a host-held shared witness. The unit tests inject an equivalent context
manager; the Linux-only tests at the end use real `flock(2)` locks, real `/proc` records
and the real witness, and an opt-in test executes the actual application image.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from deploy.postgres import enroll
from deploy.postgres import lock_owner
from deploy.postgres import maintenance
from house_helper.persistence import pg_enrollment as enrollment
from house_helper.persistence import pg_journal as journal
from tests.test_pg_enroll_tool import (
    SPEC,
    WEB_ID,
    FakeDocker,
    _enrolled_at_head,
    _run_handoff,
)

NEW_WEB_ID = "e" * 64
WEB_APP = enroll.WEB_APP_SPEC
# The kernel's live cmdline for the deployed image: dbtool execs the `uvicorn` console
# script, and the kernel rewrites a `#!` script's argv to [interpreter, script, ...].
REAL_WEB_ARGV = (
    "/opt/venv/bin/python",
    "/opt/venv/bin/uvicorn",
    WEB_APP,
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
)
START_TICKS = 700_700
# A stable identity for journal-only records. The command tests use the real lock file's.
DEFAULT_IDENTITY = (os.makedev(8, 1) if hasattr(os, "makedev") else 0x801, 12345)


def _identity(path: Path) -> tuple[int, int]:
    info = os.stat(path)
    return (info.st_dev, info.st_ino)


def _device(identity: tuple[int, int]) -> str:
    return lock_owner.device_text(identity)


def _fdinfo_line(
    pid: int, access: str, identity: tuple[int, int], *, lock_id: int = 1
) -> str:
    major, minor = lock_owner.device_parts(identity[0])
    return (
        f"lock:\t{lock_id}: FLOCK  ADVISORY  {access.upper()} {pid} "
        f"{major:02x}:{minor:02x}:{identity[1]} 0 EOF\n"
    )


def _with_web(fake, **overrides):
    """The project's running web service row, as `docker ps -a` reports it."""
    row = {
        "Id": WEB_ID,
        "State": "running",
        "Names": "house-helper-pg-house-helper-1",
        "Project": SPEC.project,
        "Service": enroll.WEB_SERVICE,
    }
    row.update(overrides)
    fake.worker_containers.append(row)
    fake.web_container = row
    return fake


def _write_process(
    proc_root: Path,
    container_id: str,
    *,
    pid: int = 4242,
    start_ticks: int = START_TICKS,
    argv=REAL_WEB_ARGV,
    cgroup: str | None = None,
) -> Path:
    directory = proc_root / str(pid)
    (directory / "fd").mkdir(parents=True, exist_ok=True)
    (directory / "fdinfo").mkdir(parents=True, exist_ok=True)
    (directory / "cgroup").write_text(
        cgroup if cgroup is not None else f"0::/system.slice/docker-{container_id}.scope\n",
        encoding="utf-8",
    )
    (directory / "cmdline").write_bytes(
        b"\0".join(part.encode("utf-8") for part in argv) + b"\0"
    )
    # /proc/<pid>/stat: field 22 (starttime) is index 19 after the command name.
    fields = ["S"] + ["0"] * 18 + [str(start_ticks)] + ["0"] * 3
    (directory / "stat").write_text(
        f"{pid} (uvicorn) " + " ".join(fields) + "\n", encoding="utf-8"
    )
    return directory


def _link_lock(
    directory: Path,
    lock_path: Path,
    pid: int,
    access: str,
    *,
    lock_id: int = 1,
    record_identity: tuple[int, int] | None = None,
) -> None:
    # A hard link has the same st_dev/st_ino, exactly like the real /proc/<pid>/fd magic
    # link resolves to the same inode. The fdinfo record is the kernel's flock record; its
    # device/inode must be the target's, or it is a descriptor that was reused.
    identity = _identity(lock_path) if record_identity is None else record_identity
    os.link(lock_path, directory / "fd" / "7")
    (directory / "fdinfo" / "7").write_text(
        _fdinfo_line(pid, access, identity, lock_id=lock_id), encoding="utf-8"
    )


class _FakeWitness:
    """A shared witness equivalent enough for the offline tests.

    It verifies the path's identity exactly as the production witness does and supports a
    simulated exclusive holder (`busy`). The real Linux tests use `flock(2)` instead.
    """

    def __init__(self, factory, path, identity, on_held):
        self.factory = factory
        self.path = Path(path)
        self.identity = identity
        self.on_held = on_held

    def __enter__(self):
        if self.factory.busy:
            raise lock_owner.LockWitnessBusy()
        self.factory.calls.append((self.path, self.identity))
        if not self.path.exists():
            raise lock_owner.LockInspectionError(
                "lock_witness_missing", "the lock file disappeared"
            )
        if _identity(self.path) != self.identity:
            raise lock_owner.LockInspectionError(
                "lock_witness_identity", "the witness opened a different lock file"
            )
        self.factory.held.append(self)
        if self.on_held is not None:
            self.on_held()
        return self

    def __exit__(self, *exc_info):
        if self in self.factory.held:
            self.factory.held.remove(self)


class _WitnessFactory:
    def __init__(self):
        self.busy = False
        self.held: list[_FakeWitness] = []
        self.calls: list[tuple[Path, tuple[int, int]]] = []

    def __call__(self, path, identity, *, on_held=None):
        return _FakeWitness(self, path, identity, on_held)


@dataclass
class Fixture:
    fake: FakeDocker
    journal: journal.Journal
    docker: enroll.Docker
    proc_root: Path
    state: Path
    lock_path: Path
    identity: tuple[int, int]


def _fixture(
    tmp_path,
    *,
    holder=True,
    real_witness=False,
    witness_busy=False,
) -> Fixture:
    fake, journal_file = _enrolled_at_head(tmp_path)
    _with_web(fake)
    state = tmp_path / "state"
    state.mkdir()
    lock_path = state / lock_owner.LOCK_FILE_NAME
    lock_path.write_bytes(b"")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    fake.state_mountpoint = state
    fake.proc_root = proc_root
    fake.witness = _WitnessFactory()
    fake.witness.busy = witness_busy
    if holder:
        directory = _write_process(proc_root, WEB_ID)
        _link_lock(directory, lock_path, 4242, "read")
    docker = enroll.Docker(SPEC, runner=fake, proc_root=proc_root)
    docker.witness_factory = (
        lock_owner.shared_witness if real_witness else fake.witness
    )
    return Fixture(
        fake=fake,
        journal=journal_file,
        docker=docker,
        proc_root=proc_root,
        state=state,
        lock_path=lock_path,
        identity=_identity(lock_path),
    )


def _run_lock(docker, journal_file, approved_by="owner", **kwargs):
    return enroll.run_lock_handoff(
        docker, journal_file, approved_by=approved_by, **kwargs
    )


def _lock_records(journal_file):
    return [
        record
        for record in journal_file.read().records
        if record.kind == "handoff_lock"
    ]


def _outcomes(journal_file):
    return [
        record
        for record in _lock_records(journal_file)
        if record.payload["state"] == "outcome"
    ]


def _expected_evidence(identity=DEFAULT_IDENTITY, **overrides):
    value = {
        "container_id": WEB_ID,
        "state_volume": SPEC.state_volume,
        "lock_present": True,
        "lock_device": _device(identity),
        "lock_inode": identity[1],
        "idle_exit": 3,
        "idle_code": "maintenance_lock_busy",
        "holder_pid": 4242,
        "holder_start_ticks": START_TICKS,
        "holder_access": "read",
        "observations": 2,
        "witness_access": "read",
        "observation_boundary": enrollment.LOCK_OBSERVATION_BOUNDARY,
    }
    value.update(overrides)
    return value


def test_the_lock_handoff_journals_an_observed_intent_then_outcome(tmp_path, capsys):
    fx = _fixture(tmp_path)
    before = len(fx.journal.read().records)
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    output = capsys.readouterr().out
    assert "ok [lock-handoff]" in output
    assert "host-held shared witness" in output
    records = fx.journal.read().records[before:]
    assert [record.kind for record in records] == ["handoff_lock", "handoff_lock"]
    intent, outcome = records
    assert intent.payload == {
        "state": "intent",
        "action": "observe_lock_handoff",
        "handoff": "lock",
        "operator": "owner",
        "run_id": intent.payload["run_id"],
    }
    assert outcome.payload == {
        "state": "outcome",
        "action": "observe_lock_handoff",
        "handoff": "lock",
        "operator": "owner",
        "run_id": intent.payload["run_id"],
        "outcome": "observed",
        "evidence": _expected_evidence(fx.identity),
    }
    # The frozen `check-idle` observation ran inside the one verified web container, by
    # immutable id; the ownership proof read the fixture /proc twice; the witness was
    # taken on the validated lock identity and released after publication.
    assert fx.fake.lock_probe_targets == [WEB_ID]
    assert fx.fake.state_volume_inspections == 1
    assert fx.fake.witness.calls == [(fx.lock_path, fx.identity)]
    assert fx.fake.witness.held == []
    # Durable readback through the validator, not merely the records the command wrote.
    view = fx.journal.read()
    assert enrollment.lock_handoff_observed(view.records).seq == outcome.seq
    assert enrollment.pending_lock_handoff_intent(view.records) is None
    assert enrollment.pending_lock_handoff_refusal(view.records) is None


def test_a_repeated_lock_handoff_records_a_fresh_observation_epoch(tmp_path, capsys):
    """A successful re-observation is a new epoch, not a silent no-op.

    The new intent is durable before the re-observation so an UNKNOWN cannot leave the
    earlier `observed` outcome authoritative; the repeated success therefore closes that
    intent with a fresh `observed` outcome.
    """
    fx = _fixture(tmp_path)
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    first_events = enrollment.lock_handoff_events(fx.journal.read().records)
    assert [record.payload["state"] for record in first_events] == ["intent", "outcome"]
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    assert "re-observed" in capsys.readouterr().out
    events = enrollment.lock_handoff_events(fx.journal.read().records)
    assert [record.payload["state"] for record in events] == [
        "intent",
        "outcome",
        "intent",
        "outcome",
    ]
    assert [record.payload.get("outcome") for record in events] == [
        None,
        "observed",
        None,
        "observed",
    ]
    # The new epoch names its own run id, and the latest observed outcome is the state.
    assert events[2].payload["run_id"] != events[1].payload["run_id"]
    assert events[2].seq < events[3].seq
    state = enrollment.lock_handoff_state(fx.journal.read().records)
    assert state.status == "observed"
    assert state.pending is None
    assert state.observed.seq == events[3].seq
    assert state.observed.payload["evidence"]["container_id"] == WEB_ID


def test_a_negative_reobservation_supersedes_the_observed_record(tmp_path, capsys):
    fx = _fixture(tmp_path)
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    observed = _outcomes(fx.journal)[0]
    # The web process no longer holds the lock: remove its fd record.
    os.unlink(fx.proc_root / "4242" / "fd" / "7")
    (fx.proc_root / "4242" / "fdinfo" / "7").write_text("", encoding="utf-8")
    fx.fake.lock_answers["idle"] = (3, b"refused [maintenance_lock_busy]: held elsewhere")
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_REFUSED
    error = capsys.readouterr().err
    assert "superseded" in error
    # The historical observed record is retained, but it is not the latest state and can
    # no longer authorize a re-arm.
    records = fx.journal.read().records
    assert observed in records
    assert enrollment.lock_handoff_observed(records) is None
    assert enrollment.lock_handoff_state(records).status == "failed"
    outcomes = _outcomes(fx.journal)
    assert [record.payload["outcome"] for record in outcomes] == ["observed", "failed"]
    assert outcomes[-1].payload["evidence"]["holder_pid"] is None
    # The determinate negative resolves the intent that was published before the
    # re-observation: a new epoch that opened and closed, not a hidden state change.
    events = enrollment.lock_handoff_events(records)
    assert [record.payload["state"] for record in events] == [
        "intent",
        "outcome",
        "intent",
        "outcome",
    ]
    assert events[2].seq < outcomes[-1].seq
    assert events[2].payload["run_id"] == outcomes[-1].payload["run_id"]
    assert enrollment.pending_lock_handoff_intent(records) is None


@pytest.mark.parametrize(
    "idle, holder, access, reason",
    [
        ((0, b"ok [check_idle]: idle"), False, None, "the exclusive idle check succeeded"),
        (
            (3, b"refused [state_dir_unusable]: no /state"),
            False,
            None,
            "the idle check exited 3 (state_dir_unusable)",
        ),
        (
            (3, b"refused [maintenance_lock_busy]: held elsewhere"),
            False,
            None,
            "the state-volume lock is held, but not by the validated web process",
        ),
        (
            (3, b"refused [maintenance_lock_busy]: held elsewhere"),
            True,
            "write",
            "the web process holds the state-volume lock exclusively",
        ),
    ],
)
def test_a_determinate_negative_is_recorded_failed_never_observed(
    tmp_path, capsys, idle, holder, access, reason
):
    fx = _fixture(tmp_path)
    fx.fake.lock_answers["idle"] = idle
    if holder:
        # The web process holds the lock exclusively, so a real shared witness would be
        # refused (`LOCK_SH` conflicts with `LOCK_EX`). That is still a determinate
        # negative: it must be recorded `failed`, never left UNKNOWN.
        fx.fake.witness.busy = True
        os.unlink(fx.proc_root / "4242" / "fd" / "7")
        (fx.proc_root / "4242" / "fdinfo" / "7").write_text("", encoding="utf-8")
        os.link(fx.lock_path, fx.proc_root / "4242" / "fd" / "7")
        (fx.proc_root / "4242" / "fdinfo" / "7").write_text(
            _fdinfo_line(4242, "write", fx.identity), encoding="utf-8"
        )
    else:
        os.unlink(fx.proc_root / "4242" / "fd" / "7")
        (fx.proc_root / "4242" / "fdinfo" / "7").write_text("", encoding="utf-8")
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_REFUSED
    assert reason in capsys.readouterr().err
    outcomes = _outcomes(fx.journal)
    assert len(outcomes) == 1 and outcomes[0].payload["outcome"] == "failed"
    assert enrollment.lock_handoff_observed(fx.journal.read().records) is None
    assert enrollment.pending_lock_handoff_intent(fx.journal.read().records) is None


def test_no_lock_evidence_without_a_running_web_service(tmp_path):
    fx = _fixture(tmp_path)
    before = fx.journal.read().records
    fx.fake.worker_containers.clear()
    fx.fake.web_container = None
    with pytest.raises(enroll.DockerError) as refused:
        _run_lock(fx.docker, fx.journal)
    assert refused.value.code == "web_container"
    assert fx.journal.read().records == before


def test_a_foreign_project_web_container_is_not_the_lock_handoff(tmp_path):
    fx = _fixture(tmp_path)
    fx.fake.web_container["Project"] = "another-project"
    with pytest.raises(enroll.DockerError) as refused:
        _run_lock(fx.docker, fx.journal)
    assert refused.value.code == "web_container"
    assert _lock_records(fx.journal) == []


def test_a_foreign_state_volume_is_not_the_lock_handoff(tmp_path):
    fx = _fixture(tmp_path)
    fx.fake.web_container["Mounts"] = [
        {
            "Type": "volume",
            "Name": "some-other-state",
            "Destination": "/state",
            "RW": True,
        }
    ]
    with pytest.raises(enroll.DockerError) as refused:
        _run_lock(fx.docker, fx.journal)
    assert refused.value.code == "web_container"
    assert _lock_records(fx.journal) == []


def test_an_indeterminate_idle_probe_leaves_the_intent_pending(tmp_path, capsys):
    fx = _fixture(tmp_path)
    fx.fake.lock_answers["idle"] = (125, b"docker: cannot exec")
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    assert "unknown [lock-handoff]" in capsys.readouterr().err
    events = enrollment.lock_handoff_events(fx.journal.read().records)
    assert [record.payload["state"] for record in events] == ["intent"]
    assert enrollment.pending_lock_handoff_intent(fx.journal.read().records) is not None

    # The same command resolves the SAME intent: no second intent, one outcome. A
    # different operator label may do the resolving; each record keeps its own label.
    fx.fake.lock_answers["idle"] = (3, b"refused [maintenance_lock_busy]: held")
    assert _run_lock(fx.docker, fx.journal, approved_by="operator-two") == enroll.EX_OK
    events = enrollment.lock_handoff_events(fx.journal.read().records)
    assert [record.payload["state"] for record in events] == ["intent", "outcome"]
    assert events[0].payload["run_id"] == events[1].payload["run_id"]
    assert events[0].seq < events[1].seq
    assert events[0].payload["operator"] == "owner"
    assert events[1].payload["operator"] == "operator-two"


def test_a_docker_client_failure_is_unknown_not_failed(tmp_path, capsys):
    fx = _fixture(tmp_path)
    fx.fake.lock_answers["idle"] = (1, b"Error: No such container: " + WEB_ID.encode())
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    assert "could not be run" in capsys.readouterr().err
    assert enrollment.pending_lock_handoff_intent(fx.journal.read().records) is not None


def test_an_unreadable_proc_tree_leaves_the_intent_pending(tmp_path, capsys):
    fx = _fixture(tmp_path)
    empty = tmp_path / "not-a-proc"
    empty.write_text("not a directory")
    fx.docker.proc_root = empty
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    assert "unknown [lock-handoff]" in capsys.readouterr().err
    assert enrollment.pending_lock_handoff_intent(fx.journal.read().records) is not None


def test_an_uninspectable_state_volume_leaves_the_intent_pending(tmp_path, capsys):
    fx = _fixture(tmp_path)
    fx.fake.state_mountpoint = tmp_path / "not-mounted"
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    assert "host mountpoint cannot be inspected" in capsys.readouterr().err
    assert enrollment.pending_lock_handoff_intent(fx.journal.read().records) is not None


def test_a_probe_process_cannot_impersonate_the_web_holder(tmp_path, capsys):
    # The Codex race: an exclusive holder releases, a *probe* takes a shared lock and
    # exits, and the exclusive holder reacquires. The old design's shared probe ran
    # `dbtool exec --shared` inside the web container, so it would be in the SAME cgroup:
    # the deployed web argv distinguishes it from uvicorn. Its lock record is real kernel
    # state, but it must never be published as the web process's lock.
    fx = _fixture(tmp_path, holder=False)
    probe = _write_process(
        fx.proc_root,
        WEB_ID,
        pid=5151,
        argv=("python", enroll.DBTOOL_PATH, "exec", "--shared", "--", "python", "-c", "pass"),
    )
    os.link(fx.lock_path, probe / "fd" / "7")
    (probe / "fdinfo" / "7").write_text(
        _fdinfo_line(5151, "read", fx.identity), encoding="utf-8"
    )
    fx.fake.lock_answers["idle"] = (3, b"refused [maintenance_lock_busy]: held")
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_REFUSED
    assert "not by the validated web process" in capsys.readouterr().err
    outcome = _outcomes(fx.journal)[0]
    assert outcome.payload["outcome"] == "failed"
    assert outcome.payload["evidence"]["holder_pid"] is None
    assert enrollment.lock_handoff_observed(fx.journal.read().records) is None


def test_a_web_process_without_the_shared_lock_is_not_observed(tmp_path, capsys):
    fx = _fixture(tmp_path, holder=False)
    fx.fake.lock_answers["idle"] = (0, b"ok [check_idle]: idle")
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_REFUSED
    assert "exclusive idle check succeeded" in capsys.readouterr().err
    assert enrollment.lock_handoff_observed(fx.journal.read().records) is None


def test_two_candidate_holders_are_ambiguous_and_stay_unknown(tmp_path, capsys):
    fx = _fixture(tmp_path)
    second = _write_process(
        fx.proc_root, WEB_ID, pid=4243, start_ticks=START_TICKS + 1
    )
    _link_lock(second, fx.lock_path, 4243, "read", lock_id=2)
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    assert "more than one web process" in capsys.readouterr().err
    assert enrollment.pending_lock_handoff_intent(fx.journal.read().records) is not None


def test_lock_state_change_between_the_two_snapshots_is_unknown(tmp_path, capsys):
    fx = _fixture(tmp_path)

    def change(_target):
        # The idle probe runs between the two snapshots; replace the holder there.
        os.unlink(fx.proc_root / "4242" / "fd" / "7")
        replacement = _write_process(fx.proc_root, WEB_ID, pid=4244)
        _link_lock(replacement, fx.lock_path, 4244, "read", lock_id=3)
        return (3, b"refused [maintenance_lock_busy]: held")

    fx.fake.lock_answers["idle"] = change
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    assert "changed between the two observations" in capsys.readouterr().err
    assert enrollment.pending_lock_handoff_intent(fx.journal.read().records) is not None


def test_a_web_container_that_disappears_during_the_observation_is_unknown(tmp_path):
    fx = _fixture(tmp_path)

    def vanish(_target):
        fx.fake.web_container = None
        fx.fake.worker_containers.clear()
        return (3, b"refused [maintenance_lock_busy]: held")

    fx.fake.lock_answers["idle"] = vanish
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    assert enrollment.lock_handoff_observed(fx.journal.read().records) is None


def test_idle_success_and_a_kernel_holder_disagree_is_unknown(tmp_path, capsys):
    fx = _fixture(tmp_path)
    fx.fake.lock_answers["idle"] = (0, b"")
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    assert "disagree" in capsys.readouterr().err
    assert enrollment.pending_lock_handoff_intent(fx.journal.read().records) is not None


def test_a_pending_migration_unknown_blocks_the_lock_handoff(tmp_path):
    fx = _fixture(tmp_path)
    fx.journal.append(
        "migration",
        {"state": "intent", "run_id": "a" * 32, "action": "migrate"},
    )
    with pytest.raises(enrollment.EnrollmentError) as refused:
        _run_lock(fx.docker, fx.journal)
    assert refused.value.code == "migration_unknown"
    assert _lock_records(fx.journal) == []


def test_a_pending_grant_handoff_blocks_the_lock_handoff(tmp_path, capsys):
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: "interrupt"
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN
    fake.state_mountpoint = tmp_path / "state"
    fake.proc_root = tmp_path / "proc"
    docker = enroll.Docker(SPEC, runner=fake, proc_root=fake.proc_root)
    before = journal_file.read().records
    assert _run_lock(docker, journal_file) == enroll.EX_UNKNOWN
    assert "unknown [lock-handoff]" in capsys.readouterr().err
    assert journal_file.read().records == before


def test_status_reports_a_pending_lock_handoff_as_unknown(tmp_path, capsys):
    fx = _fixture(tmp_path)
    fx.fake.lock_answers["idle"] = (126, b"docker: cannot exec")
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    capsys.readouterr()
    assert enroll._print_status(fx.journal) == enroll.EX_OK
    assert "lock handoff UNKNOWN" in capsys.readouterr().out


def test_a_pending_lock_handoff_blocks_maintenance(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_intent())
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.verify_target(
            enroll.DEFAULT_COMPOSE,
            tmp_path / "cutover",
            journal_file=journal_file,
        )
    assert refused.value.code == "lock_handoff_unknown"


def test_a_pending_lock_handoff_blocks_mode_a(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_intent())
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.ModeA(journal_file, object()).run()
    assert refused.value.code == "lock_handoff_unknown"


def test_existing_journals_stay_valid_and_gain_no_lock_records(tmp_path):
    fake, journal_file = _enrolled_at_head(tmp_path)
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
    records = journal_file.read().records
    assert enrollment.lock_handoff_events(records) == ()
    assert enrollment.lock_handoff_observed(records) is None
    assert enrollment.pending_lock_handoff_intent(records) is None
    assert enrollment.successful_handoff_roles(records) == frozenset({"hh_app"})


def test_a_lock_record_never_enters_the_grant_handoff_history(tmp_path):
    fx = _fixture(tmp_path)
    assert _run_handoff(fx.fake, fx.journal) == enroll.EX_OK
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    records = fx.journal.read().records
    events = enrollment.handoff_events(records)
    assert [record.payload["state"] for record in events] == ["intent", "outcome"]
    assert enrollment.successful_handoff_roles(records) == frozenset({"hh_app"})


# --- the reproduced Codex failures ------------------------------------------------------------


def test_a_descriptor_reused_for_another_file_is_not_a_holder(tmp_path, capsys):
    """Codex failure 2: fdinfo referring to a different inode than the target.

    The descriptor is stat'ed as the lock file, but by the time its fdinfo is read the
    kernel record names another device/inode: the descriptor was closed and reused. The
    old reader counted the FLOCK line without checking its identity and published
    `observed`. The explicit device/inode comparison makes this indeterminate.
    """
    fx = _fixture(tmp_path)
    os.unlink(fx.proc_root / "4242" / "fd" / "7")
    os.link(fx.lock_path, fx.proc_root / "4242" / "fd" / "7")
    (fx.proc_root / "4242" / "fdinfo" / "7").write_text(
        _fdinfo_line(4242, "read", (fx.identity[0], fx.identity[1] + 1)),
        encoding="utf-8",
    )
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    error = capsys.readouterr().err
    assert "another file" in error
    assert _outcomes(fx.journal) == []
    assert enrollment.pending_lock_handoff_intent(fx.journal.read().records) is not None


@pytest.mark.parametrize(
    "argv, expected",
    [
        # The kernel's live cmdline for the deployed image (captured with the real image).
        (REAL_WEB_ARGV, True),
        (("uvicorn", WEB_APP), True),
        (("python3", "-m", "uvicorn", WEB_APP), True),
        (("/usr/local/bin/python3.12", "/usr/local/bin/uvicorn", WEB_APP), True),
        # Probes and unrelated programs never have the app token immediately after uvicorn.
        (("python", enroll.DBTOOL_PATH, "check-idle"), False),
        (("python", enroll.DBTOOL_PATH, "exec", "--shared", "--", "python", "-c", "pass"), False),
        (("uvicorn", "some.other:app"), False),
        (("node", "uvicorn", WEB_APP), False),
        ((WEB_APP,), False),
        ((), False),
    ],
)
def test_the_deployed_uvicorn_argv_shapes_are_recognised(argv, expected):
    """Codex failure 3: the proof must match the image's real, kernel-rewritten argv."""
    assert lock_owner.is_web_process(argv, WEB_APP) is expected


def test_the_image_argv_is_observed_as_the_web_holder(tmp_path):
    # The real deployed shape, not the pre-exec `("uvicorn", app)` tuple.
    fx = _fixture(tmp_path, holder=False)
    directory = _write_process(fx.proc_root, WEB_ID, argv=REAL_WEB_ARGV)
    _link_lock(directory, fx.lock_path, 4242, "read")
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    assert _outcomes(fx.journal)[0].payload["evidence"]["holder_pid"] == 4242


def test_an_exclusive_holder_at_witness_time_is_unknown(tmp_path, capsys):
    """Codex failure 4/7: shared -> exclusive -> shared must not publish `observed`."""
    fx = _fixture(tmp_path, witness_busy=True)
    fx.fake.lock_answers["idle"] = (3, b"refused [maintenance_lock_busy]: held")
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_UNKNOWN
    assert "exclusive holder acquired" in capsys.readouterr().err
    assert _outcomes(fx.journal) == []
    assert enrollment.pending_lock_handoff_intent(fx.journal.read().records) is not None


def test_web_disappearance_before_publication_is_unknown(tmp_path, capsys):
    """Codex failure 6: the web may die between the witness and the append."""
    fx = _fixture(tmp_path)

    def disappear():
        os.unlink(fx.proc_root / "4242" / "fd" / "7")
        (fx.proc_root / "4242" / "fdinfo" / "7").write_text("", encoding="utf-8")

    assert (
        _run_lock(fx.docker, fx.journal, on_witness_held=disappear)
        == enroll.EX_UNKNOWN
    )
    assert "changed between the two observations" in capsys.readouterr().err
    assert _outcomes(fx.journal) == []


def test_web_replacement_before_publication_is_unknown(tmp_path, capsys):
    fx = _fixture(tmp_path)

    def replace():
        # The old id remains a running process in the fake /proc, but the project's
        # current web container is now a different one. The fresh verification refuses.
        fx.fake.web_container = None
        fx.fake.worker_containers.clear()

    assert (
        _run_lock(fx.docker, fx.journal, on_witness_held=replace) == enroll.EX_UNKNOWN
    )
    assert "unknown [lock-handoff]" in capsys.readouterr().err
    assert _outcomes(fx.journal) == []


def test_a_stale_observation_is_not_reported_as_already_observed(tmp_path, capsys):
    """Codex failure 7: a recorded observed evidence names an older container."""
    fx = _fixture(tmp_path)
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    first = _outcomes(fx.journal)[0]
    # A replacement container with a new immutable id now runs the web service.
    fx.fake.web_container["Id"] = NEW_WEB_ID
    replacement = _write_process(
        fx.proc_root, NEW_WEB_ID, pid=4343, start_ticks=START_TICKS + 10
    )
    _link_lock(replacement, fx.lock_path, 4343, "read", lock_id=4)
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    output = capsys.readouterr().out
    assert "differs from the earlier observation" in output
    assert "already observed" not in output
    outcomes = _outcomes(fx.journal)
    assert [record.payload["outcome"] for record in outcomes] == ["observed", "observed"]
    assert outcomes[0].payload["evidence"]["container_id"] == WEB_ID
    assert outcomes[1].payload["evidence"]["container_id"] == NEW_WEB_ID
    assert first.payload["evidence"]["container_id"] == WEB_ID
    state = enrollment.lock_handoff_state(fx.journal.read().records)
    assert state.observed.seq == outcomes[1].seq


def test_an_unknown_reobservation_fences_the_previous_observed_outcome(tmp_path, capsys):
    """Codex C7-P3 scenario 7: UNKNOWN during a re-observation must not leave `observed`.

    The old lifecycle re-observed without a new intent. When the Web descriptor
    disappeared mid-observation, the command exited 5 but the earlier `observed` outcome
    stayed the latest state, so a later `hh_purge` handoff granted CONNECT. Starting the
    re-observation now publishes a durable intent first; the UNKNOWN is that pending
    intent and every later transition refuses until the same command reconciles it.
    """
    fx = _fixture(tmp_path)
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    observed = _outcomes(fx.journal)[0]
    before_grant = tuple(
        record for record in fx.journal.read().records if record.kind == "handoff"
    )

    def disappear():
        # The web process's descriptor acquisition this observation: the first snapshot
        # saw it, the second must not.
        os.unlink(fx.proc_root / "4242" / "fd" / "7")
        (fx.proc_root / "4242" / "fdinfo" / "7").write_text("", encoding="utf-8")

    assert (
        _run_lock(fx.docker, fx.journal, on_witness_held=disappear) == enroll.EX_UNKNOWN
    )
    assert "changed between the two observations" in capsys.readouterr().err

    records = fx.journal.read().records
    events = enrollment.lock_handoff_events(records)
    # The initial epoch, then the intent published before the fallible re-observation.
    assert [record.payload["state"] for record in events] == [
        "intent",
        "outcome",
        "intent",
    ]
    pending = enrollment.pending_lock_handoff_intent(records)
    assert pending is not None and pending.seq > observed.seq
    state = enrollment.lock_handoff_state(records)
    assert state.status == "pending"
    assert state.observed is None
    assert enrollment.lock_handoff_observed(records) is None
    assert enrollment.pending_lock_handoff_refusal(records) is not None
    # The reader that pairs grant records refuses; the old observed outcome is history.
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.successful_handoff_roles(records)
    assert refused.value.code == "lock_handoff_unknown"
    # A subsequent `hh_purge` handoff is refused before it reads or changes the ACL.
    assert _run_handoff(fx.fake, fx.journal, role="hh_purge") == enroll.EX_UNKNOWN
    assert "no CONNECT was granted" in capsys.readouterr().err
    assert fx.fake.grants == []
    assert (
        tuple(
            record for record in fx.journal.read().records if record.kind == "handoff"
        )
        == before_grant
    )
    # Recovery is the existing fail-closed contract: the same command resolves THIS
    # intent, and a fresh observation restores an authoritative `observed` state.
    restored = _write_process(fx.proc_root, WEB_ID)
    _link_lock(restored, fx.lock_path, 4242, "read")
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    records = fx.journal.read().records
    state = enrollment.lock_handoff_state(records)
    assert state.status == "observed"
    assert state.pending is None
    assert enrollment.pending_lock_handoff_intent(records) is None


def test_an_interruption_after_the_new_intent_leaves_it_pending(tmp_path):
    """A killed re-observation cannot fall back to the previous `observed` outcome."""
    fx = _fixture(tmp_path)
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK

    def interrupt():
        raise RuntimeError("simulated process interruption")

    with pytest.raises(RuntimeError):
        _run_lock(fx.docker, fx.journal, on_witness_held=interrupt)
    records = fx.journal.read().records
    pending = enrollment.pending_lock_handoff_intent(records)
    assert pending is not None
    assert enrollment.lock_handoff_state(records).status == "pending"
    assert enrollment.lock_handoff_observed(records) is None
    assert _run_handoff(fx.fake, fx.journal, role="hh_purge") == enroll.EX_UNKNOWN
    assert fx.fake.grants == []


def test_grant_history_is_preserved_across_lock_observation_epochs(tmp_path):
    fx = _fixture(tmp_path)
    assert _run_handoff(fx.fake, fx.journal) == enroll.EX_OK
    assert enrollment.successful_handoff_roles(fx.journal.read().records) == frozenset(
        {"hh_app"}
    )
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    assert _run_lock(fx.docker, fx.journal) == enroll.EX_OK
    records = fx.journal.read().records
    assert enrollment.successful_handoff_roles(records) == frozenset({"hh_app"})
    assert [record.payload["state"] for record in enrollment.handoff_events(records)] == [
        "intent",
        "outcome",
    ]


def test_a_pending_lock_intent_is_not_ignored_by_the_grant_reader(tmp_path):
    """Codex failure 7: the grant reader must refuse a pending lock UNKNOWN."""
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_intent(run_id="a" * 32))
    _append(journal_file, _lock_outcome(run_id="a" * 32, outcome="failed"))
    _append(journal_file, _lock_intent(run_id="b" * 32))
    records = journal_file.read().records
    assert enrollment.lock_handoff_observed(records) is None
    assert enrollment.lock_handoff_state(records).status == "pending"
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.successful_handoff_roles(records)
    assert refused.value.code == "lock_handoff_unknown"


# --- journal semantic validation --------------------------------------------------------------


def _lock_intent(run_id="e" * 32, operator="owner"):
    return {
        "state": "intent",
        "action": "observe_lock_handoff",
        "handoff": "lock",
        "operator": operator,
        "run_id": run_id,
    }


def _lock_outcome(run_id="e" * 32, *, outcome="observed", **evidence):
    value = _expected_evidence(**evidence)
    if outcome == "failed":
        # A failed outcome cannot satisfy the success condition. Without a witness it is
        # unambiguously not observed, whatever the holder fields say.
        value["witness_access"] = evidence.get("witness_access")
        value["observation_boundary"] = (
            enrollment.LOCK_OBSERVATION_BOUNDARY
            if evidence.get("witness_access") == "read"
            else None
        )
    return {
        "state": "outcome",
        "action": "observe_lock_handoff",
        "handoff": "lock",
        "operator": "owner",
        "run_id": run_id,
        "outcome": outcome,
        "evidence": value,
    }


def _append(journal_file, payload):
    journal_file.append("handoff_lock", payload)


def test_a_later_epoch_supersedes_the_latest_state(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_intent(run_id="a" * 32))
    _append(journal_file, _lock_outcome(run_id="a" * 32))
    _append(journal_file, _lock_intent(run_id="b" * 32))
    _append(journal_file, _lock_outcome(run_id="b" * 32))
    state = enrollment.lock_handoff_state(journal_file.read().records)
    assert state.pending is None
    assert state.status == "observed"
    assert state.observed.payload["run_id"] == "b" * 32
    # A historical observed outcome is not "the observation": only the latest outcome is.
    assert enrollment.lock_handoff_observed(journal_file.read().records).seq == 3


def test_an_observed_outcome_followed_by_failed_is_not_observed(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_intent(run_id="a" * 32))
    _append(journal_file, _lock_outcome(run_id="a" * 32))
    _append(journal_file, _lock_intent(run_id="b" * 32))
    _append(journal_file, _lock_outcome(run_id="b" * 32, outcome="failed"))
    records = journal_file.read().records
    assert enrollment.lock_handoff_observed(records) is None
    assert enrollment.lock_handoff_state(records).status == "failed"


def test_a_pending_intent_hides_a_historical_observed_state(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_intent(run_id="a" * 32))
    _append(journal_file, _lock_outcome(run_id="a" * 32))
    _append(journal_file, _lock_intent(run_id="b" * 32))
    records = journal_file.read().records
    state = enrollment.lock_handoff_state(records)
    assert state.status == "pending"
    assert state.observed is None
    assert enrollment.lock_handoff_observed(records) is None


def test_an_overlapping_intent_history_is_corruption(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_intent(run_id="a" * 32))
    _append(journal_file, _lock_intent(run_id="b" * 32))
    _append(journal_file, _lock_outcome(run_id="b" * 32))
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.pending_lock_handoff_intent(journal_file.read().records)
    assert refused.value.code == "lock_handoff_journal"
    assert "overlaps an earlier intent" in refused.value.message


def test_an_outcome_that_resolves_a_different_intent_is_corruption(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_intent(run_id="a" * 32))
    _append(journal_file, _lock_outcome(run_id="b" * 32))
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.lock_handoff_events(journal_file.read().records)
    assert refused.value.code == "lock_handoff_journal"


def test_an_intent_with_the_same_run_id_as_an_outcome_is_corruption(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_intent(run_id="a" * 32))
    _append(journal_file, _lock_outcome(run_id="a" * 32))
    _append(journal_file, _lock_intent(run_id="a" * 32))
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.lock_handoff_events(journal_file.read().records)
    assert "operation id appears more than once" in refused.value.message


def test_a_lock_outcome_without_its_intent_is_corruption(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_outcome())
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.lock_handoff_events(journal_file.read().records)
    assert refused.value.code == "lock_handoff_journal"


@pytest.mark.parametrize(
    "outcome, evidence, message",
    [
        ("observed", {"idle_exit": 0, "idle_code": None}, "records a lock holder"),
        ("observed", {"holder_access": "write"}, "contradicts its evidence"),
        ("observed", {"holder_pid": None, "holder_start_ticks": None, "holder_access": None},
         "records no web lock holder"),
        ("observed", {"idle_exit": 0, "idle_code": "maintenance_lock_busy"},
         "successful idle check carries a refusal code"),
        ("failed", {"idle_exit": 0, "idle_code": None}, "records a lock holder"),
        ("failed", {"idle_exit": 3, "idle_code": None}, "refused idle check has no semantic code"),
        ("observed", {"observations": 1}, "required two observations"),
        ("failed", {}, "records the exact success condition"),
        # The Option A witness is required for `observed`, and it must name its boundary.
        ("observed", {"witness_access": None, "observation_boundary": None},
         "contradicts its evidence"),
        ("observed", {"observation_boundary": None}, "does not name its publication boundary"),
        ("failed", {"lock_device": None, "lock_inode": None}, "lock device is malformed"),
        ("failed", {"lock_device": "not-a-device"}, "lock device is malformed"),
        ("failed", {"lock_inode": 0}, "lock inode is malformed"),
        ("failed", {"witness_access": "write"}, "witness access is not recognized"),
    ],
)
def test_a_lock_outcome_must_match_its_evidence(tmp_path, outcome, evidence, message):
    journal_file = journal.Journal(tmp_path / "cutover")
    payload = _expected_evidence()
    payload.update(evidence)
    if outcome == "failed" and payload["witness_access"] is None:
        payload["observation_boundary"] = None
    _append(journal_file, _lock_intent())
    _append(
        journal_file,
        {
            "state": "outcome",
            "action": "observe_lock_handoff",
            "handoff": "lock",
            "operator": "owner",
            "run_id": "e" * 32,
            "outcome": outcome,
            "evidence": payload,
        },
    )
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.lock_handoff_events(journal_file.read().records)
    assert refused.value.code == "lock_handoff_journal"
    assert message in refused.value.message


def test_a_coherent_failed_outcome_is_valid_and_not_unknown(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    payload = _expected_evidence(
        idle_exit=0,
        idle_code=None,
        holder_pid=None,
        holder_start_ticks=None,
        holder_access=None,
        witness_access=None,
        observation_boundary=None,
    )
    _append(journal_file, _lock_intent())
    _append(
        journal_file,
        {
            "state": "outcome",
            "action": "observe_lock_handoff",
            "handoff": "lock",
            "operator": "owner",
            "run_id": "e" * 32,
            "outcome": "failed",
            "evidence": payload,
        },
    )
    records = journal_file.read().records
    assert enrollment.pending_lock_handoff_intent(records) is None
    assert enrollment.pending_lock_handoff_refusal(records) is None
    assert enrollment.lock_handoff_observed(records) is None
    assert enrollment.lock_handoff_state(records).status == "failed"


def test_a_corrupt_lock_history_is_not_ignored_by_the_grant_reader(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    _append(journal_file, _lock_intent(run_id="a" * 32))
    _append(journal_file, _lock_intent(run_id="b" * 32))
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.successful_handoff_roles(journal_file.read().records)
    assert refused.value.code == "lock_handoff_journal"


def test_the_cli_accepts_the_credential_directory_before_or_after_the_subcommand():
    args = enroll._parser().parse_args(
        ["--credentials-dir", "/approved", "lock-handoff", "--approved-by", "owner"]
    )
    assert args.credentials_dir == Path("/approved")
    assert args.command == "lock-handoff"
    args = enroll._parser().parse_args(
        ["--credentials-dir", "/approved", "check-credentials"]
    )
    assert args.check_credentials_dir is None
    assert args.credentials_dir == Path("/approved")
    args = enroll._parser().parse_args(
        ["check-credentials", "--credentials-dir", "/after"]
    )
    assert args.check_credentials_dir == Path("/after")
    assert args.credentials_dir is None


def test_the_journal_vocabulary_accepts_the_new_kind_and_keeps_the_old_ones():
    assert "handoff_lock" in journal.JOURNAL_KINDS
    assert {"handoff", "cluster_enrollment", "migration"} <= journal.JOURNAL_KINDS


def test_the_recorded_web_command_is_the_one_the_lock_proof_matches():
    # The proof's app token must be the one the Compose surface actually starts; drift
    # here would make every honest observation fail closed (loudly, never silently).
    text = enroll.DEFAULT_COMPOSE.read_text(encoding="utf-8")
    for token in enroll.WEB_PROCESS_ARGV:
        assert f"- {token}" in text
    assert enroll.WEB_APP_SPEC == WEB_APP == lock_owner.WEB_APP_SPEC


# --- real Linux flock(2) ----------------------------------------------------------------------

_LINUX_PROC = os.name == "posix" and Path("/proc/locks").exists()

_SHARED_HOLDER = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_SH)
print(os.getpid(), flush=True)
time.sleep(30)
"""

_EXCLUSIVE_ATTEMPT = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(3)
sys.exit(0)
"""


def _start_holder(lock_path: Path) -> subprocess.Popen:
    child = subprocess.Popen(
        [sys.executable, "-c", _SHARED_HOLDER, str(lock_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip()
    return child


def _exclusive_attempt(lock_path: Path) -> int:
    return subprocess.run(
        [sys.executable, "-c", _EXCLUSIVE_ATTEMPT, str(lock_path)],
        capture_output=True,
        timeout=30,
    ).returncode


@pytest.mark.skipif(not _LINUX_PROC, reason="needs a Linux /proc and flock(2)")
def test_real_flock_is_read_from_the_holders_own_descriptor(tmp_path):
    lock_path = tmp_path / "real.lock"
    lock_path.write_bytes(b"")
    script = """
import fcntl, os, sys, time
mode = fcntl.LOCK_SH if sys.argv[2] == "read" else fcntl.LOCK_EX
fd = os.open(sys.argv[1], os.O_RDWR)
fcntl.flock(fd, mode)
print(os.getpid(), flush=True)
time.sleep(30)
"""
    identity = _identity(lock_path)

    def holder(mode: str):
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_path), mode],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        pid = int(child.stdout.readline().strip())
        try:
            records = lock_owner._process_access(Path("/proc") / str(pid), identity)
            assert records == (mode,)
        finally:
            child.terminate()
            child.wait(timeout=10)
        return pid

    read_pid = holder("read")
    write_pid = holder("write")
    assert read_pid != write_pid


@pytest.mark.skipif(not _LINUX_PROC, reason="needs a Linux /proc and flock(2)")
def test_the_real_fdinfo_record_is_checked_against_the_target_inode(tmp_path):
    # The kernel's own device/inode in fdinfo is the target's, so the real record is a
    # holder; a record naming a different inode for the same descriptor is not.
    lock_path = tmp_path / lock_owner.LOCK_FILE_NAME
    lock_path.write_bytes(b"")
    identity = _identity(lock_path)
    child = _start_holder(lock_path)
    pid = int(child.pid)
    try:
        directory = Path("/proc") / str(pid)
        assert lock_owner._process_access(directory, identity) == ("read",)
        assert lock_owner._process_access(
            directory, (identity[0], identity[1] + 1)
        ) == ()
        # The descriptor is the target, but a record for another inode is raised only
        # when the descriptor itself stat'ed as the target. Reuse is modelled above in
        # the offline test; here the positive and the wrong-target negative are real.
    finally:
        child.terminate()
        child.wait(timeout=10)


@pytest.mark.skipif(not _LINUX_PROC, reason="needs a Linux /proc and flock(2)")
def test_real_unrelated_flock_is_not_a_web_holder(tmp_path):
    lock_path = tmp_path / lock_owner.LOCK_FILE_NAME
    lock_path.write_bytes(b"")
    child = _start_holder(lock_path)
    try:
        snapshot = lock_owner.inspect(Path("/proc"), tmp_path, WEB_ID, WEB_APP)
        assert snapshot.lock_present is True
        assert snapshot.identity == _identity(lock_path)
        assert snapshot.holders == ()
    finally:
        child.terminate()
        child.wait(timeout=10)


@pytest.mark.skipif(not _LINUX_PROC, reason="needs a Linux /proc and flock(2)")
def test_the_host_witness_is_never_web_ownership_evidence(tmp_path):
    # The witness is the host process's own descriptor. It is outside the web container's
    # cgroup and has the wrong argv, so the holder test must still find nobody.
    lock_path = tmp_path / lock_owner.LOCK_FILE_NAME
    lock_path.write_bytes(b"")
    identity = _identity(lock_path)
    with lock_owner.shared_witness(lock_path, identity):
        snapshot = lock_owner.inspect(Path("/proc"), tmp_path, WEB_ID, WEB_APP)
        assert snapshot.lock_present is True
        assert snapshot.holders == ()


@pytest.mark.skipif(not _LINUX_PROC, reason="needs a Linux /proc and flock(2)")
def test_a_real_shared_witness_blocks_an_exclusive_holder(tmp_path):
    lock_path = tmp_path / lock_owner.LOCK_FILE_NAME
    lock_path.write_bytes(b"")
    identity = _identity(lock_path)
    with lock_owner.shared_witness(lock_path, identity):
        assert _exclusive_attempt(lock_path) == 3
    # Released: the exclusive acquisition now succeeds.
    assert _exclusive_attempt(lock_path) == 0


@pytest.mark.skipif(not _LINUX_PROC, reason="needs a Linux /proc and flock(2)")
def test_real_shared_exclusive_shared_interleaving_refuses_the_witness(tmp_path):
    lock_path = tmp_path / lock_owner.LOCK_FILE_NAME
    lock_path.write_bytes(b"")
    identity = _identity(lock_path)
    web = _start_holder(lock_path)
    try:
        # The web releases; an exclusive holder takes the lock; the web has not returned.
        web.terminate()
        web.wait(timeout=10)
        exclusive = subprocess.Popen(
            [sys.executable, "-c", _SHARED_HOLDER.replace("LOCK_SH", "LOCK_EX"),
             str(lock_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert exclusive.stdout is not None
        assert exclusive.stdout.readline().strip()
        try:
            with pytest.raises(lock_owner.LockWitnessBusy):
                with lock_owner.shared_witness(lock_path, identity):
                    pass
        finally:
            exclusive.terminate()
            exclusive.wait(timeout=10)
        # Shared again: the witness is available and coexists.
        again = _start_holder(lock_path)
        try:
            with lock_owner.shared_witness(lock_path, identity):
                pass
        finally:
            again.terminate()
            again.wait(timeout=10)
    finally:
        if web.poll() is None:
            web.terminate()
            web.wait(timeout=10)


@pytest.mark.skipif(not _LINUX_PROC, reason="needs a Linux /proc and flock(2)")
def test_exclusive_acquisition_is_refused_during_journal_publication(
    tmp_path, capsys
):
    """Codex failure 5: the witness is held across the outcome append."""
    fx = _fixture(tmp_path, real_witness=True)
    attempts: list[int] = []

    def during_publication():
        # This runs after the final snapshot while the witness is held, immediately
        # before the journal outcome append. An exclusive acquisition must be refused.
        attempts.append(_exclusive_attempt(fx.lock_path))

    assert (
        _run_lock(fx.docker, fx.journal, publication_hook=during_publication)
        == enroll.EX_OK
    )
    assert attempts == [3]
    assert _outcomes(fx.journal)[0].payload["outcome"] == "observed"


# --- the actual application image (opt-in) ----------------------------------------------------

APP_IMAGE = os.environ.get("HOUSE_HELPER_TEST_APP_IMAGE")
_DOCKER = shutil.which("docker")


@pytest.mark.skipif(
    not APP_IMAGE or not _DOCKER,
    reason="set HOUSE_HELPER_TEST_APP_IMAGE to an image built from this Dockerfile",
)
def test_the_real_image_uvicorn_is_read_as_the_web_holder(tmp_path):
    """Execute the deployed image's real start path and read its kernel lock record.

    The image's `dbtool.py exec --shared -- uvicorn house_helper.web.asgi:app ...` takes
    the shared lock and replaces itself with the console script. A sibling container in
    the host PID namespace runs `lock_owner.inspect` against that real process and the
    same named state volume. This is the end-to-end argv + fdinfo proof.
    """
    # A named volume, not a host bind: the image's state volume is owned by the
    # application uid, whereas Docker Desktop bind mounts a host directory as root and
    # the SQLite start gate would refuse it. Both containers mount the same volume, so
    # the lock file is the same inode in the host namespace.
    state_volume = f"hh-lock-image-{os.getpid()}-{int(time.time())}"
    name = f"hh-lock-image-{os.getpid()}-{int(time.time())}"
    repo = Path(__file__).resolve().parent.parent

    def docker(*arguments, check=True):
        return subprocess.run(
            [_DOCKER, *arguments], capture_output=True, text=True, check=check
        )

    docker("volume", "create", state_volume)
    try:
        started = docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "-v",
            f"{state_volume}:/state",
            "-e",
            "HOUSE_HELPER_DATABASE_URL=sqlite:////state/probe.db",
            "-e",
            "HOUSE_HELPER_STATE_DIR=/state",
            "--entrypoint",
            "/opt/venv/bin/python",
            APP_IMAGE,
            "/opt/house-helper/ops/dbtool.py",
            "exec",
            "--shared",
            "--",
            "uvicorn",
            WEB_APP,
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--lifespan",
            "off",
        )
    except BaseException:
        docker("volume", "rm", "-f", state_volume, check=False)
        raise
    container_id = started.stdout.strip()
    inspector = """
import json, sys
from pathlib import Path
sys.path.insert(0, "/repo/deploy/postgres")
import lock_owner
snapshot = lock_owner.inspect(Path("/proc"), Path("/state"), sys.argv[1], lock_owner.WEB_APP_SPEC)
print(json.dumps({
    "lock_present": snapshot.lock_present,
    "holders": [
        {"pid": holder.pid, "start_ticks": holder.start_ticks, "access": holder.access}
        for holder in snapshot.holders
    ],
}))
"""
    try:
        deadline = time.time() + 30
        result = None
        while time.time() < deadline:
            probe = docker(
                "run",
                "--rm",
                "--pid=host",
                # The web process runs as the image's unprivileged uid; reading its
                # /proc/<pid>/fd needs SYS_PTRACE in the inspecting container.
                "--cap-add=SYS_PTRACE",
                "-v",
                f"{repo}:/repo:ro",
                "-v",
                f"{state_volume}:/state",
                "--entrypoint",
                "/opt/venv/bin/python",
                APP_IMAGE,
                "-c",
                inspector,
                container_id,
                check=False,
            )
            if probe.returncode == 0:
                result = json.loads(probe.stdout)
                if result["holders"]:
                    break
            time.sleep(0.5)
        assert result is not None, "the inspector container never ran"
        assert result["lock_present"] is True
        assert result["holders"], "the real uvicorn process was not recognised as the holder"
        assert result["holders"][0]["access"] == "read"
        assert result["holders"][0]["pid"] > 0
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", "-f", state_volume, check=False)
