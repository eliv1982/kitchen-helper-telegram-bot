"""P0: one host operation interval across every cluster-advancing entry point.

The defect these tests cover: `maintenance.verify_target` checked the journal for a
pending handoff OUTSIDE the lock `enroll.py` uses, so a backup could pass verification, a
concurrent handoff could append an intent (or leave one UNKNOWN), and the backup could
still publish an archive - and a restore could still create a database.

What is real here:

* the real `flock` (POSIX `fcntl.flock`, Windows `msvcrt.locking`) through the real
  `journal.Journal.operation_lock`;
* the real CLI entry points - `enroll.py handoff`, `enroll.py migrate`,
  `maintenance.py backup`, `maintenance.py restore` - called through `main()` in separate
  processes where the contract spans processes;
* real journal records, including a digest-valid malformed outcome.

What is not real: Docker. Every Docker use is either stood in for (the backup body) or
made a tripwire, because the contract is that a conflict is refused BEFORE any container
is started, and an UNKNOWN handoff refuses before anything at all.

The only relaxation is the journal's root-ownership requirement, which unprivileged CI
cannot satisfy. Nothing else about the lock is stubbed.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from deploy.postgres import enroll
from deploy.postgres import maintenance
from house_helper.persistence import pg_contract as contract
from house_helper.persistence import pg_enrollment as enrollment
from house_helper.persistence import pg_journal as journal
from tests.test_pg_backup_remediation import (
    _BackupTool,
    _backup_args,
    _install_backup_fakes,
    _install_target_fakes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
JOURNAL_DIR_NAME = "cutover"
RUN_ID = "ab" * 16


# --- fixtures --------------------------------------------------------------------------------


def _portable_journal(monkeypatch) -> None:
    """The journal directory is owned by the test user on CI; ownership is not under test."""
    real = journal.Journal
    monkeypatch.setattr(
        journal,
        "Journal",
        lambda directory, require_root=False: real(directory, require_root=False),
    )


def _journal(tmp_path: Path) -> journal.Journal:
    value = journal.Journal(tmp_path / JOURNAL_DIR_NAME, require_root=False)
    value.prepare()
    return value


def _before_state() -> dict:
    """The full pre-handoff privilege matrix: every project role, every privilege, absent."""
    return {
        "acl_materialized": True,
        "entries": [],
        "effective": [
            [role, privilege, False]
            for role in contract.PROJECT_ROLE_NAMES
            for privilege in contract.DATABASE_PRIVILEGES
        ],
    }


def _handoff_intent(role: str = "hh_app", run_id: str = RUN_ID) -> dict:
    return {
        "state": "intent",
        "action": f"grant_connect:{role}",
        "handoff": enrollment.HANDOFF_EVENTS[role],
        "role": role,
        "database": contract.SERVING_DATABASE,
        "operator": "owner",
        "run_id": run_id,
        "application_name": enrollment.handoff_application_name(run_id),
        "before": _before_state(),
    }


def _reconciled_outcome(role: str = "hh_app", run_id: str = RUN_ID) -> dict:
    """A valid outcome that resolves an intent as applied (no tagged operation to report)."""
    return {
        "state": "outcome",
        "action": f"grant_connect:{role}",
        "handoff": enrollment.HANDOFF_EVENTS[role],
        "role": role,
        "database": contract.SERVING_DATABASE,
        "operator": "owner",
        "run_id": run_id,
        "outcome": "reconciled",
        "evidence": {
            "has_database_privilege": True,
            "changed": True,
            "reconciled": True,
            "applied": True,
            "privileges_unchanged": False,
            "psql_exit": None,
            "transport": "client_gone",
        },
    }


def _unknown_handoff_journal(tmp_path: Path) -> journal.Journal:
    value = _journal(tmp_path)
    value.append("handoff", _handoff_intent())
    return value


def _interval_busy(journal_dir: Path) -> bool:
    """Whether ANOTHER actor holds the host operation interval right now.

    A second acquisition is a different descriptor (and, in the cross-process tests, a
    different process), so `operation_busy` here means exactly "someone else owns it".
    """
    actor = journal.Journal(Path(journal_dir), require_root=False)
    try:
        with actor.operation_lock():
            return False
    except journal.JournalError as error:
        assert error.code == "operation_busy", error
        return True


def _journal_bytes(journal_dir: Path) -> bytes:
    """The journal file's bytes, or nothing when it does not exist yet."""
    path = Path(journal_dir) / journal.JOURNAL_FILE
    return path.read_bytes() if path.exists() else b""


def _await_interval_release(journal_dir: Path, timeout: float = 10.0) -> bool:
    """Wait, boundedly, for a KILLED process's lock to be reaped by the OS.

    POSIX releases the flock when the last descriptor closes at process teardown. Windows
    releases byte-range locks when the terminated process's handles are closed, which is
    asynchronous by a few tens of milliseconds. This waits for exactly that hand-back; it
    is the force-kill path, not the normal one (a graceful exit unlocks synchronously).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _interval_busy(journal_dir):
            return True
        time.sleep(0.02)
    return False


class _TripwireDocker:
    """Any Docker use is a state-changing attempt that must not happen before the lock."""

    def __getattr__(self, name):
        raise AssertionError(
            f"the operation reached Docker.{name} before the host operation interval"
        )


# --- the interval covers the operations it authorizes ----------------------------------------


class _IntervalObservingTool(_BackupTool):
    """The real backup body, recording whether the host interval is held at each step."""

    def __init__(self, journal_dir: Path) -> None:
        super().__init__()
        self.journal_dir = Path(journal_dir)
        self.observed: list[tuple[str, bool]] = []

    def confirm_lock_holder(self, container, *, control_dir, timeout=60.0):
        # The holder is asked before publication and again after the read-back; both are
        # part of the backup's protected section.
        self.observed.append(("confirm", _interval_busy(self.journal_dir)))
        return super().confirm_lock_holder(
            container, control_dir=control_dir, timeout=timeout
        )


def test_the_backup_holds_the_interval_through_publication_and_readback(
    monkeypatch, tmp_path
):
    """Backup starts first: publication and its acknowledgement are inside the interval."""
    tools = _IntervalObservingTool(tmp_path / "journal")
    _install_backup_fakes(monkeypatch, tmp_path, tools)  # also relaxes journal ownership
    output = tmp_path / "point.tar"
    publishes: list[bool] = []
    real_publish = maintenance._host_publish

    def recording_publish(staging, output_path, replace):
        publishes.append(_interval_busy(tmp_path / "journal"))
        return real_publish(staging, output_path, replace)

    monkeypatch.setattr(maintenance, "_host_publish", recording_publish)

    assert maintenance.backup_command(_backup_args(tmp_path, output)) == maintenance.EX_OK

    assert output.is_file()
    # The host publication and both ownership probes observed the lock held by someone
    # else - i.e. this backup still owned the interval at that instant.
    assert publishes == [True]
    assert tools.observed == [("confirm", True), ("confirm", True)]
    # ...and it is released when the command returns.
    assert _interval_busy(tmp_path / "journal") is False


def test_the_interval_observation_detects_an_unheld_interval(monkeypatch, tmp_path):
    """Negative control for the assertion above: entering the body directly (the pre-P0
    path, no interval) shows the lock UNHELD at publication, so the check is sensitive."""
    tools = _IntervalObservingTool(tmp_path / "journal")
    _install_backup_fakes(monkeypatch, tmp_path, tools)
    output = tmp_path / "point.tar"
    publishes: list[bool] = []
    real_publish = maintenance._host_publish

    def recording_publish(staging, output_path, replace):
        publishes.append(_interval_busy(tmp_path / "journal"))
        return real_publish(staging, output_path, replace)

    monkeypatch.setattr(maintenance, "_host_publish", recording_publish)
    args = _backup_args(tmp_path, output)
    target = maintenance.verify_target(args.compose_file, args.journal_dir)

    assert maintenance._backup_command_locked(args, target) == maintenance.EX_OK

    assert output.is_file()
    assert publishes == [False]
    assert tools.observed == [("confirm", False), ("confirm", False)]


def _restore_args(tmp_path: Path, archive: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        compose_file=tmp_path / "compose.yaml",
        journal_dir=tmp_path / "journal",
        credentials_dir=tmp_path / "credentials",
        object_contract=tmp_path / "contract.json",
        work_dir=tmp_path,
        keep_work=False,
        archive=archive,
        maintenance_database="postgres",
        app_image="house-helper:local",
        no_rehearse=True,
        keep_rehearsal=False,
        keep_database=False,
    )


def test_the_restore_holds_the_interval_before_it_touches_the_cluster(
    monkeypatch, tmp_path
):
    """Restore starts first: the interval is held before the archive or a database is read."""
    tools = types.SimpleNamespace(image_digest=lambda: {})
    _install_target_fakes(monkeypatch, tmp_path)  # also relaxes journal ownership
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: tools)
    observed: list[bool] = []

    def stop_before_any_database(*args, **kwargs):
        observed.append(_interval_busy(tmp_path / "journal"))
        raise maintenance.MaintenanceError("archive_integrity", "stopped by the test")

    monkeypatch.setattr(maintenance, "validate_archive", stop_before_any_database)

    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.restore_command(_restore_args(tmp_path, tmp_path / "point.tar"))

    assert refused.value.code == "archive_integrity"
    assert observed == [True]
    assert _interval_busy(tmp_path / "journal") is False


def test_a_handoff_is_refused_while_a_real_restore_holds_the_interval(
    monkeypatch, tmp_path
):
    """Restore starts first; the real handoff entry function is refused from inside it."""
    tools = types.SimpleNamespace(image_digest=lambda: {})
    _install_target_fakes(monkeypatch, tmp_path)  # also relaxes journal ownership
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: tools)
    attempts: list[str] = []

    def attempt_handoff(*args, **kwargs):
        actor = journal.Journal(tmp_path / "journal", require_root=False)
        try:
            enroll.run_handoff(
                _TripwireDocker(),
                actor,
                adapter=object(),
                role="hh_app",
                approved_by="owner",
            )
            attempts.append("started")
        except journal.JournalError as error:
            attempts.append(error.code)
        raise maintenance.MaintenanceError("archive_integrity", "stopped by the test")

    monkeypatch.setattr(maintenance, "validate_archive", attempt_handoff)

    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.restore_command(_restore_args(tmp_path, tmp_path / "point.tar"))

    assert refused.value.code == "archive_integrity"
    assert attempts == ["operation_busy"]


def test_a_handoff_cannot_begin_while_the_interval_is_held(tmp_path):
    value = _journal(tmp_path)
    with value.operation_lock():
        with pytest.raises(journal.JournalError) as refused:
            enroll.run_handoff(
                _TripwireDocker(),
                value,
                adapter=object(),
                role="hh_app",
                approved_by="owner",
            )
    assert refused.value.code == "operation_busy"
    # Nothing was appended: no intent, so nothing can later be read as UNKNOWN.
    assert value.read().records == ()


def test_a_migration_cannot_begin_while_the_interval_is_held(tmp_path):
    value = _journal(tmp_path)
    with value.operation_lock():
        with pytest.raises(journal.JournalError) as refused:
            enroll.run_migrate(
                _TripwireDocker(), tmp_path / "compose.yaml", value, adapter=object()
            )
    assert refused.value.code == "operation_busy"
    assert value.read().records == ()


def test_a_lock_acquisition_failure_refuses_the_backup_before_anything_is_read(
    monkeypatch, tmp_path
):
    """A contested lock is a controlled refusal with the documented exit code."""
    _portable_journal(monkeypatch)
    reached: list[str] = []
    monkeypatch.setattr(
        maintenance, "verify_target", lambda *a, **k: reached.append("verified")
    )
    monkeypatch.setattr(journal, "_try_lock", lambda fd, exclusive: False)

    code = maintenance.main(
        [
            "--journal-dir",
            str(tmp_path / JOURNAL_DIR_NAME),
            "--work-dir",
            str(tmp_path),
            "backup",
            "--output",
            str(tmp_path / "point.tar"),
        ]
    )

    assert code == maintenance.EX_REFUSED
    assert reached == []
    assert not (tmp_path / "point.tar").exists()


# --- an UNKNOWN handoff refuses maintenance before any state change --------------------------


@pytest.mark.parametrize("command", ["backup", "restore"])
def test_an_unknown_handoff_refuses_maintenance_before_any_cluster_access(
    monkeypatch, tmp_path, command, capsys
):
    """Handoff became UNKNOWN first: backup and restore refuse ahead of every read."""
    _unknown_handoff_journal(tmp_path)
    _portable_journal(monkeypatch)
    reached: list[str] = []

    def record(name, value=None):
        reached.append(name)
        return value

    monkeypatch.setattr(
        maintenance.enroll,
        "Docker",
        lambda spec: types.SimpleNamespace(inspect_image=lambda: record("image", {})),
    )
    monkeypatch.setattr(
        maintenance.enroll, "validate_image", lambda *a, **k: record("image_check")
    )
    monkeypatch.setattr(
        maintenance.enroll, "verify_mode_b", lambda *a, **k: record("mode_b")
    )
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: record("tools"))

    argv = [
        "--journal-dir",
        str(tmp_path / JOURNAL_DIR_NAME),
        "--work-dir",
        str(tmp_path),
    ]
    if command == "backup":
        argv += ["backup", "--output", str(tmp_path / "point.tar")]
    else:
        argv += [
            "restore",
            str(tmp_path / "point.tar"),
            "--maintenance-database",
            "postgres",
        ]
    code = maintenance.main(argv)

    assert code == maintenance.EX_UNKNOWN
    assert "handoff_unknown" in capsys.readouterr().err
    # No compose parse, no image inspection, no Mode B, no client container.
    assert reached == []
    assert not (tmp_path / "point.tar").exists()


def test_a_reconciled_handoff_lets_the_next_backup_proceed(monkeypatch, tmp_path):
    """Normal sequential execution after a successful reconciliation."""
    value = _unknown_handoff_journal(tmp_path)
    assert enrollment.pending_handoff_intents(value.read().records)
    value.append("handoff", _reconciled_outcome())
    assert enrollment.pending_handoff_intents(value.read().records) == ()
    assert enrollment.successful_handoff_roles(value.read().records) == frozenset({"hh_app"})

    _portable_journal(monkeypatch)
    reached: list[str] = []

    def stop_at_the_boundary(image):
        reached.append("tools")
        raise maintenance.MaintenanceError("docker_unavailable", "stopped by the test")

    monkeypatch.setattr(
        maintenance.enroll,
        "Docker",
        lambda spec: types.SimpleNamespace(inspect_image=lambda: {}),
    )
    monkeypatch.setattr(maintenance.enroll, "validate_image", lambda *a, **k: None)
    monkeypatch.setattr(
        maintenance.enroll,
        "verify_mode_b",
        lambda *a, **k: types.SimpleNamespace(
            verdict=types.SimpleNamespace(ok=True, mismatches=(), completed={})
        ),
    )
    monkeypatch.setattr(maintenance, "ToolContainers", stop_at_the_boundary)

    code = maintenance.main(
        [
            "--journal-dir",
            str(tmp_path / JOURNAL_DIR_NAME),
            "--work-dir",
            str(tmp_path),
            "backup",
            "--output",
            str(tmp_path / "point.tar"),
        ]
    )

    # The gate opened: the command went on to its first cluster tool.
    assert code == maintenance.EX_UNAVAILABLE
    assert reached == ["tools"]


# --- an UNKNOWN migration refuses maintenance before any state change -----------------------

MIGRATION_RUN_ID = "cd" * 16
H3_RUN_ID = "ef" * 16


def _migration_intent(run_id: str = MIGRATION_RUN_ID) -> dict:
    return {
        "state": "intent",
        "run_id": run_id,
        "plan": "migrate",
        "from_revision": "",
    }


def _migration_outcome(
    run_id: str = MIGRATION_RUN_ID, outcome: str = "reconciled_not_started"
) -> dict:
    return {"state": "outcome", "run_id": run_id, "outcome": outcome}


def _unknown_migration_journal(tmp_path: Path, *, successful_h3: bool = False) -> journal.Journal:
    value = _journal(tmp_path)
    if successful_h3:
        # H3: the scheduled backup login's CONNECT handoff is fully resolved.
        value.append("handoff", _handoff_intent(role="hh_backup", run_id=H3_RUN_ID))
        value.append("handoff", _reconciled_outcome(role="hh_backup", run_id=H3_RUN_ID))
    value.append("migration", _migration_intent())
    return value


def _install_maintenance_tripwires(monkeypatch) -> list[str]:
    """Every cluster/database access records itself; none may be reached."""
    reached: list[str] = []

    def record(name, value=None):
        reached.append(name)
        return value

    monkeypatch.setattr(
        maintenance.enroll,
        "Docker",
        lambda spec: types.SimpleNamespace(inspect_image=lambda: record("image", {})),
    )
    monkeypatch.setattr(
        maintenance.enroll, "validate_image", lambda *a, **k: record("image_check")
    )
    monkeypatch.setattr(
        maintenance.enroll, "verify_mode_b", lambda *a, **k: record("mode_b")
    )
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: record("tools"))
    monkeypatch.setattr(maintenance, "psql", lambda *a, **k: record("psql"))
    return reached


def _maintenance_argv(tmp_path: Path, command: str) -> list[str]:
    argv = [
        "--journal-dir",
        str(tmp_path / JOURNAL_DIR_NAME),
        "--work-dir",
        str(tmp_path),
    ]
    if command == "backup":
        argv += ["backup", "--output", str(tmp_path / "point.tar")]
    else:
        argv += [
            "restore",
            str(tmp_path / "point.tar"),
            "--maintenance-database",
            "postgres",
        ]
    return argv


@pytest.mark.parametrize("command", ["backup", "restore"])
def test_an_unknown_migration_refuses_maintenance_before_any_cluster_access(
    monkeypatch, tmp_path, command, capsys
):
    """1: an interrupted initial migration stops a restore before CREATE DATABASE.

    The same gate stops a backup before it can publish an archive.
    """
    _unknown_migration_journal(tmp_path)
    _portable_journal(monkeypatch)
    reached = _install_maintenance_tripwires(monkeypatch)

    code = maintenance.main(_maintenance_argv(tmp_path, command))

    assert code == maintenance.EX_UNKNOWN
    assert "migration_unknown" in capsys.readouterr().err
    # No compose parse, no image inspection, no Mode B, no psql, no client container.
    assert reached == []
    assert not (tmp_path / "point.tar").exists()


def test_a_successful_h3_does_not_mask_a_later_migration_unknown(
    monkeypatch, tmp_path, capsys
):
    """2: a completed H3 does not let a later timed-out migration through to a backup."""
    value = _unknown_migration_journal(tmp_path, successful_h3=True)
    assert enrollment.successful_handoff_roles(value.read().records) == frozenset(
        {"hh_backup"}
    )
    _portable_journal(monkeypatch)
    reached = _install_maintenance_tripwires(monkeypatch)

    code = maintenance.main(_maintenance_argv(tmp_path, "backup"))

    assert code == maintenance.EX_UNKNOWN
    assert "migration_unknown" in capsys.readouterr().err
    assert reached == []
    assert not (tmp_path / "point.tar").exists()


@pytest.mark.parametrize("command", ["backup", "restore"])
def test_migration_reconciliation_lets_maintenance_proceed(
    monkeypatch, tmp_path, command
):
    """3: a valid reconciliation record releases the gate for backup and restore."""
    value = _unknown_migration_journal(tmp_path)
    value.append("migration", _migration_outcome())
    assert enrollment.pending_migration_unknown(value.read().records) is None
    _portable_journal(monkeypatch)
    reached: list[str] = []

    def stop_at_the_boundary(image):
        reached.append("tools")
        raise maintenance.MaintenanceError("docker_unavailable", "stopped by the test")

    monkeypatch.setattr(
        maintenance.enroll,
        "Docker",
        lambda spec: types.SimpleNamespace(inspect_image=lambda: {}),
    )
    monkeypatch.setattr(maintenance.enroll, "validate_image", lambda *a, **k: None)
    monkeypatch.setattr(
        maintenance.enroll,
        "verify_mode_b",
        lambda *a, **k: types.SimpleNamespace(
            verdict=types.SimpleNamespace(ok=True, mismatches=(), completed={})
        ),
    )
    monkeypatch.setattr(maintenance, "ToolContainers", stop_at_the_boundary)

    code = maintenance.main(_maintenance_argv(tmp_path, command))

    # The gate opened: the command went on to its first cluster tool.
    assert code == maintenance.EX_UNAVAILABLE
    assert reached == ["tools"]


def test_handoff_and_migration_unknown_together_stop_maintenance(
    monkeypatch, tmp_path, capsys
):
    """4: whichever UNKNOWN is observed first, no maintenance operation proceeds."""
    value = _journal(tmp_path)
    value.append("handoff", _handoff_intent())
    value.append("migration", _migration_intent())
    _portable_journal(monkeypatch)
    reached = _install_maintenance_tripwires(monkeypatch)

    code = maintenance.main(_maintenance_argv(tmp_path, "backup"))

    assert code == maintenance.EX_UNKNOWN
    assert "unknown" in capsys.readouterr().err
    assert reached == []
    assert not (tmp_path / "point.tar").exists()


def test_a_malformed_journal_is_a_controlled_refusal(monkeypatch, tmp_path, capsys):
    """5: an invalid journal is refused as a journal error, never an internal error."""
    value = _journal(tmp_path)
    value.append("migration", _migration_outcome())
    path = Path(value.directory) / journal.JOURNAL_FILE
    path.write_bytes(path.read_bytes() + b"this is not a journal line\n")
    _portable_journal(monkeypatch)
    reached = _install_maintenance_tripwires(monkeypatch)

    code = maintenance.main(_maintenance_argv(tmp_path, "backup"))

    assert code == maintenance.EX_REFUSED
    assert "[journal_" in capsys.readouterr().err
    assert reached == []
    assert not (tmp_path / "point.tar").exists()


def test_a_conflicting_journal_directory_is_refused_before_anything(
    monkeypatch, tmp_path, capsys
):
    """The explicit directory and the environment must name one operation lock."""
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv(journal.JOURNAL_DIRECTORY_ENV, str(other))
    _portable_journal(monkeypatch)
    reached = _install_maintenance_tripwires(monkeypatch)

    code = maintenance.main(_maintenance_argv(tmp_path, "backup"))

    assert code == maintenance.EX_REFUSED
    assert "journal_mismatch" in capsys.readouterr().err
    assert reached == []
    assert not (tmp_path / "point.tar").exists()


# --- a malformed outcome is a controlled journal refusal (P2) -------------------------------


@pytest.mark.parametrize(
    "outcome",
    [[], {}, ["observed"], {"applied": True}, None, 0, 1, True, "applied", "", "observed "],
)
def test_a_malformed_outcome_is_a_controlled_refusal_and_never_provenance(
    monkeypatch, tmp_path, outcome, capsys
):
    """P2: `outcome: []` used to raise TypeError (exit 1) from set membership."""
    value = _journal(tmp_path)
    value.append("handoff", _handoff_intent())
    malformed = _reconciled_outcome()
    malformed["outcome"] = outcome
    # The line is canonical and digest-chained: the journal itself is not corrupt.
    value.append("handoff", malformed)

    for probe in (
        enrollment.pending_handoff_intents,
        enrollment.successful_handoff_roles,
        enroll.pending_handoff_refusal,
    ):
        with pytest.raises(enrollment.EnrollmentError) as refused:
            probe(value.read().records)
        assert refused.value.code == "handoff_journal"
        assert "not recognized" in refused.value.message

    # The documented exit-code contract: a refusal (3), never an internal error (1).
    _portable_journal(monkeypatch)
    code = maintenance.main(
        [
            "--journal-dir",
            str(tmp_path / JOURNAL_DIR_NAME),
            "--work-dir",
            str(tmp_path),
            "backup",
            "--output",
            str(tmp_path / "point.tar"),
        ]
    )
    assert code == maintenance.EX_REFUSED
    assert "[handoff_journal]" in capsys.readouterr().err
    assert not (tmp_path / "point.tar").exists()


# --- separate processes, real entry points ---------------------------------------------------

_HOLD_INTERVAL = r"""
import sys
import types
repo, journal_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
from deploy.postgres import maintenance
from house_helper.persistence import pg_journal as journal

real = journal.Journal
maintenance.journal.Journal = lambda d, require_root=False: real(d, require_root=False)
maintenance.enroll.resolve_credentials_directory = (
    lambda *a, **k: __import__("pathlib").Path("/credentials")
)
maintenance.enroll.load_spec = lambda *a, **k: types.SimpleNamespace()
maintenance.enroll.Docker = lambda spec: types.SimpleNamespace(inspect_image=lambda: {})
maintenance.enroll.validate_image = lambda *a, **k: None
maintenance.enroll.verify_mode_b = lambda jf, adapter, **k: types.SimpleNamespace(
    verdict=types.SimpleNamespace(ok=True, mismatches=(), completed={})
)
with maintenance.maintenance_interval("compose.yaml", journal_dir):
    print("held", flush=True)
    sys.stdin.readline()
print("released", flush=True)
"""

_HANDOFF_CLI = r"""
import sys
repo, journal_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
from deploy.postgres import enroll
from house_helper.persistence import pg_journal as journal

real = journal.Journal
journal.Journal = lambda d, require_root=False: real(d, require_root=False)
raise SystemExit(enroll.main([
    "--journal-dir", journal_dir, "handoff", "--role", "hh_app", "--approved-by", "owner",
]))
"""

_BACKUP_CLI = r"""
import sys
repo, journal_dir, work, output = sys.argv[1:5]
sys.path.insert(0, repo)
from deploy.postgres import maintenance
from house_helper.persistence import pg_journal as journal

real = journal.Journal
journal.Journal = lambda d, require_root=False: real(d, require_root=False)
raise SystemExit(maintenance.main([
    "--journal-dir", journal_dir, "--work-dir", work, "backup", "--output", output,
]))
"""

_BACKUP_IN_FLIGHT = r"""
import sys
import time
import types
from pathlib import Path

repo, journal_dir, work, output, started, go = sys.argv[1:7]
sys.path.insert(0, repo)
from deploy.postgres import maintenance
from house_helper.persistence import pg_journal as journal
from tests.test_pg_backup_remediation import _BackupTool

real = journal.Journal
maintenance.journal.Journal = lambda d, require_root=False: real(d, require_root=False)


class BlockingTool(_BackupTool):
    # The dump session is the long middle of a backup: block there until the parent says go.
    def run(self, program, **kwargs):
        Path(started).write_text("in flight", encoding="utf-8")
        deadline = time.monotonic() + 120
        while not Path(go).exists():
            if time.monotonic() > deadline:
                raise SystemExit(9)
            time.sleep(0.01)
        return super().run(program, **kwargs)


tools = BlockingTool()
maintenance.verify_target = lambda *a, **k: types.SimpleNamespace(
    spec=types.SimpleNamespace(
        image="postgres@sha256:" + "0" * 64, network="house-helper-pg-db"
    ),
    system_identifier="726485930221",
    credentials_dir=Path(work),
)
maintenance.ToolContainers = lambda image: tools
maintenance.passfile = lambda *a, **k: maintenance.Passfile(
    role="hh_backup", host_path=Path(work) / "hh_backup.pgpass"
)
raise SystemExit(maintenance.main([
    "--journal-dir", journal_dir, "--work-dir", work, "backup", "--output", output,
]))
"""


def _start_child(script: str, *arguments: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, str(REPO_ROOT), *map(str, arguments)],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _run_child(script: str, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script, str(REPO_ROOT), *map(str, arguments)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_the_real_handoff_cli_cannot_begin_while_a_backup_holds_the_interval(tmp_path):
    """Backup starts first; the supported `enroll.py handoff` entry point is refused."""
    journal_dir = tmp_path / JOURNAL_DIR_NAME
    holder = _start_child(_HOLD_INTERVAL, journal_dir)
    try:
        assert holder.stdout.readline().strip() == "held"
        before = _journal_bytes(journal_dir)

        refused = _run_child(_HANDOFF_CLI, journal_dir)

        assert refused.returncode == enroll.EX_REFUSED, refused.stderr
        assert "[operation_busy]" in refused.stderr
        # The handoff never appended an intent, so no UNKNOWN can be left behind.
        after = _journal_bytes(journal_dir)
        assert after == before
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "released"
        assert holder.wait(timeout=60) == 0
    # Once the holder is gone the interval is free again.
    assert _interval_busy(journal_dir) is False


def test_the_real_backup_cli_is_refused_while_a_handoff_holds_the_interval(tmp_path):
    """Handoff operation active: `maintenance.py backup` is refused before any tooling."""
    journal_dir = tmp_path / JOURNAL_DIR_NAME
    holder = _start_child(_HOLD_INTERVAL, journal_dir)
    output = tmp_path / "point.tar"
    try:
        assert holder.stdout.readline().strip() == "held"

        refused = _run_child(_BACKUP_CLI, journal_dir, tmp_path, output)

        assert refused.returncode == maintenance.EX_REFUSED, refused.stderr
        assert "[operation_busy]" in refused.stderr
        assert not output.exists()
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "released"
        assert holder.wait(timeout=60) == 0


def test_the_real_backup_cli_refuses_a_handoff_that_is_already_unknown(tmp_path):
    """Handoff became UNKNOWN first: the real backup entry point refuses with EX_UNKNOWN."""
    value = _unknown_handoff_journal(tmp_path)
    journal_dir = value.directory
    before = _journal_bytes(journal_dir)
    output = tmp_path / "point.tar"

    refused = _run_child(_BACKUP_CLI, journal_dir, tmp_path, output)

    assert refused.returncode == maintenance.EX_UNKNOWN, refused.stderr
    assert "[handoff_unknown]" in refused.stderr
    assert not output.exists()
    # The refusal appended nothing of its own to the journal.
    assert _journal_bytes(journal_dir) == before


def _await_start(process: subprocess.Popen, marker: Path, timeout: float = 60.0) -> None:
    """Wait for the child to reach its in-flight marker, or fail with its own diagnostics."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            _, stderr = process.communicate()
            raise AssertionError(f"the backup child exited early: {stderr}")
        time.sleep(0.02)
    process.kill()
    process.wait(timeout=30)
    raise AssertionError("the backup child did not reach its dump session in time")


def test_a_handoff_is_refused_while_a_real_backup_is_in_flight(tmp_path):
    """The Codex reproduction, end to end: backup running, `enroll.py handoff` refused.

    Before the fix the backup held only the container-side state locks, so a handoff
    could append an intent - and reach the cluster - while the dump was being taken.
    """
    journal_dir = tmp_path / JOURNAL_DIR_NAME
    started = tmp_path / "started"
    go = tmp_path / "go"
    output = tmp_path / "point.tar"
    backup = _start_child(
        _BACKUP_IN_FLIGHT, journal_dir, tmp_path, output, started, go
    )
    try:
        _await_start(backup, started)
        # The backup is inside its dump session, publication not reached, interval held.
        refused = _run_child(_HANDOFF_CLI, journal_dir)
        assert refused.returncode == enroll.EX_REFUSED, refused.stderr
        assert "[operation_busy]" in refused.stderr
        # No intent exists, so nothing can be left UNKNOWN when the backup finishes.
        assert not (journal_dir / journal.JOURNAL_FILE).exists()
    finally:
        go.write_text("go", encoding="utf-8")
    _, stderr = backup.communicate(timeout=180)
    assert backup.returncode == maintenance.EX_OK, stderr
    assert output.is_file()



def test_an_interrupted_holder_releases_the_interval(tmp_path):
    """Interruption: the kernel releases the flock, and nothing is recorded as completed."""
    journal_dir = tmp_path / JOURNAL_DIR_NAME
    holder = _start_child(_HOLD_INTERVAL, journal_dir)
    assert holder.stdout.readline().strip() == "held"
    assert _interval_busy(journal_dir) is True

    # SIGKILL on POSIX, TerminateProcess on Windows: no `finally`, no cleanup, no unlock.
    holder.kill()
    holder.wait(timeout=60)

    assert _await_interval_release(journal_dir)
    # The interval itself records nothing: no journal file, hence no completed operation.
    assert not (journal_dir / journal.JOURNAL_FILE).exists()


def test_a_waiting_thread_observes_the_same_exclusion(tmp_path):
    """Mutual exclusion is real for threads of one process too (different descriptors)."""
    value = _journal(tmp_path)
    outcome: list[object] = []
    ready = threading.Event()

    def contender():
        ready.set()
        try:
            with value.operation_lock():
                outcome.append("acquired")
        except journal.JournalError as error:
            outcome.append(error.code)

    with value.operation_lock():
        thread = threading.Thread(target=contender)
        thread.start()
        assert ready.wait(timeout=30)
        thread.join(timeout=30)

    assert outcome == ["operation_busy"]
    # After release the same caller succeeds.
    with value.operation_lock():
        pass
