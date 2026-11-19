"""HH-DB-01 Stage 8 FIX-01: the combined `restore` command owns its rehearsal container.

The staged rehearsal found that `restore` (the command that restores into the target cluster,
verifies it, and then rehearses the same archive in an isolated `--network none` container)
called `rehearse()` without its required `run_id`. The TypeError surfaced only after the
temporary database had been verified and the isolated restore had run, so the combined
command exited 1 with a disposable container left behind - the standalone `rehearse` command,
which always generated the run id, was unaffected.

These tests drive the COMPLETE `restore_command` path (the host interval, the archive
entries, the temporary-database journal, the real `rehearse` body behind a fake Docker
boundary, the drop) rather than the standalone command:

  * the run id passed to the rehearsal is the restore's own journaled operation id, and it
    names the one disposable container;
  * the container is removed and confirmed absent on success, on a rehearsal failure, and
    after the temporary database is dropped;
  * an unconfirmed removal is the dominant outcome (never an archive verdict), and names the
    exact recovery command built from the same run id;
  * `--keep-rehearsal` retains the container without making the restore fail.

The Docker boundary is a fake object here; the real PostgreSQL rehearsal is the separate
live evidence (RUNBOOK 15.13), and is rerun by the disposable rehearsal environment.
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import types
from pathlib import Path

import pytest

from house_helper.persistence import pg_backup as backup
from house_helper.persistence import pg_journal as journal
from tests.test_pg_backup import _manifest

from deploy.postgres import maintenance

IMAGE = "postgres@sha256:" + "0" * 64
SYSTEM_IDENTIFIER = "7709166311476633635"
REVISION = "0014_user_measure_overrides"
OPERATION_ID = "a" * 32          # the restore's journaled operation identity
TOKEN = "b" * 16
DATABASE = "house_helper_restore_" + "b" * 16
CONTAINER_ID = "c" * 64
DUMP = b"custom-format-dump-bytes"
CONTAINER_NAME = maintenance.REHEARSAL_CONTAINER_PREFIX + OPERATION_ID
# One public-schema entry (which `_filtered_restore_list` requires exactly once) and the
# sequence entry the frozen TOC grammar accepts.
TOC = (
    "1; 2615 0 SCHEMA - public hh_owner\n"
    "2; 0 0 SEQUENCE SET public messages_id_seq hh_owner\n"
)


def _archive(tmp_path: Path) -> tuple[Path, dict]:
    manifest = _manifest(
        snapshot={"bytes": len(DUMP), "sha256": backup.sha256_bytes(DUMP)}
    )
    path = tmp_path / "point.tar"
    backup.write_archive(path, manifest=manifest, dump=DUMP)
    return path, manifest


def _restore_args(tmp_path: Path, archive: Path, **overrides) -> types.SimpleNamespace:
    values = dict(
        compose_file=tmp_path / "compose.yaml",
        journal_dir=tmp_path / "journal",
        credentials_dir=tmp_path / "credentials",
        object_contract=tmp_path / "contract.json",
        work_dir=tmp_path,
        keep_work=False,
        archive=archive,
        maintenance_database="postgres",
        app_image="house-helper:local",
        no_rehearse=False,
        keep_rehearsal=False,
        keep_database=False,
    )
    values.update(overrides)
    return types.SimpleNamespace(**values)


class _RestoreTools:
    """The Docker/SQL boundary of the combined restore and of the real `rehearse` body.

    It answers every container operation the rehearsal performs (identity, SQL, the
    filtered restore list, the smoke revision) and records the one container name the run
    starts and the one it removes. `run` is the `psql` boundary used by the real
    `_drop_temporary_database`: the DROP can be made to fail without a real PostgreSQL."""

    def __init__(self, *, remove_result: bool = True, drop_fails: bool = False) -> None:
        self.image = IMAGE
        self.started: list[str] = []
        self.removed: list[tuple[str, object]] = []
        self.remove_result = remove_result
        self.drop_fails = drop_fails
        self.drop_attempts = 0

    def image_digest(self):
        return {}

    def run(self, argv, *, stdin=None, timeout=120, **kwargs):
        if stdin and b"DROP DATABASE" in stdin:
            self.drop_attempts += 1
            if self.drop_fails:
                return subprocess.CompletedProcess(
                    argv, 1, b"", b"ERROR: permission denied for database"
                )
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        # The absence observation after a successful DROP: the database is gone.
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def pg_restore_list(self, work):
        return TOC

    def start_detached(self, program, *, name, environment=(), network="none", tmpfs=()):
        self.started.append(name)
        return CONTAINER_ID

    def exec(self, container, program, *, stdin=None, user="postgres", timeout=300):
        if stdin and b"system_identifier" in stdin:
            return subprocess.CompletedProcess(
                program, 0, SYSTEM_IDENTIFIER.encode() + b"\n", b""
            )
        if stdin and b"version_num" in stdin:
            return subprocess.CompletedProcess(program, 0, REVISION.encode() + b"\n", b"")
        return subprocess.CompletedProcess(program, 0, b"", b"")

    def copy_in(self, container, source, target):
        pass

    def remove_verified(self, name, *, image=None):
        self.removed.append((name, image))
        return self.remove_result


class _Rig:
    def __init__(
        self,
        monkeypatch,
        tmp_path: Path,
        *,
        real_drop: bool = False,
        drop_fails: bool = False,
        **tools_kwargs,
    ) -> None:
        self.monkeypatch = monkeypatch
        self.archive, self.manifest = _archive(tmp_path)
        self.tools = _RestoreTools(drop_fails=drop_fails, **tools_kwargs)
        self.drops: list[str] = []
        self.real_drop = real_drop
        self.args = _restore_args(tmp_path, self.archive)
        self._install()

    def _install(self) -> None:
        monkeypatch = self.monkeypatch
        real_journal = journal.Journal

        def portable_journal(directory, require_root=False):
            return real_journal(directory, require_root=False)

        def verify_target(compose_file, journal_dir, *, journal_file=None):
            return types.SimpleNamespace(
                spec=types.SimpleNamespace(image=IMAGE, network="house-helper-pg-db"),
                system_identifier=SYSTEM_IDENTIFIER,
                journal_file=journal_file
                or real_journal(journal_dir, require_root=False),
            )

        monkeypatch.setattr(maintenance.journal, "Journal", portable_journal)
        monkeypatch.setattr(maintenance, "verify_target", verify_target)
        monkeypatch.setattr(maintenance, "ToolContainers", lambda image: self.tools)
        monkeypatch.setattr(
            maintenance,
            "validate_archive",
            lambda archive, tools, *, work: (backup.read_archive(archive), ()),
        )
        monkeypatch.setattr(
            backup,
            "load_object_contract_fixture",
            lambda path: {"revision": self.manifest["alembic_revision"]},
        )
        monkeypatch.setattr(backup, "toc_contract_failures", lambda entries, fixture: ())
        monkeypatch.setattr(
            maintenance,
            "passfile",
            lambda *a, **k: maintenance.Passfile(
                role="hh_restore", host_path=self.args.credentials_dir / "hh_restore.pgpass"
            ),
        )
        monkeypatch.setattr(maintenance, "maintenance_preflight", lambda *a, **k: None)
        monkeypatch.setattr(
            maintenance, "reconcile_all_temporary_databases", lambda *a, **k: None
        )
        monkeypatch.setattr(
            maintenance,
            "_new_plan",
            lambda maintenance_database: maintenance.RestorePlan(
                database=DATABASE,
                maintenance_database=maintenance_database,
                token=TOKEN,
                operation_id=OPERATION_ID,
            ),
        )
        monkeypatch.setattr(
            maintenance, "reconcile_temporary_database", lambda *a, **k: None
        )
        monkeypatch.setattr(
            maintenance, "_establish_fenced_database", lambda *a, **k: None
        )
        monkeypatch.setattr(maintenance, "pg_restore", lambda *a, **k: None)
        monkeypatch.setattr(maintenance, "_apply_artifacts", lambda *a, **k: None)
        monkeypatch.setattr(
            maintenance,
            "verify_database",
            lambda *a, **k: types.SimpleNamespace(failures=()),
        )
        monkeypatch.setattr(maintenance, "alembic_check", lambda *a, **k: None)
        monkeypatch.setattr(maintenance, "_await_postgres", lambda *a, **k: None)
        monkeypatch.setattr(
            maintenance,
            "_parse_verification",
            lambda *a, **k: types.SimpleNamespace(
                failures=(),
                evidence={"revision": self.manifest["alembic_revision"]},
            ),
        )
        monkeypatch.setattr(
            maintenance,
            "attachment_proof",
            lambda *a, **k: {
                "count": 0,
                "bytes": 0,
                "declared_bytes": 0,
                "sample": False,
            },
        )
        if not self.real_drop:
            monkeypatch.setattr(
                maintenance,
                "_drop_temporary_database",
                lambda journal_file, **kwargs: self.drops.append(kwargs["plan"].database),
            )

    def fail_verification(self) -> None:
        self.monkeypatch.setattr(
            maintenance,
            "_parse_verification",
            lambda *a, **k: types.SimpleNamespace(
                failures=("the isolated restored database did not match the archive",),
                evidence={},
            ),
        )

    def run(self) -> int:
        return maintenance.restore_command(self.args)

    def run_cli(self) -> int:
        """Drive the real entry point so the documented exit status is observed, not inferred."""
        return maintenance.main(self.cli_argv())

    def cli_argv(self) -> list[str]:
        argv = [
            "--compose-file", str(self.args.compose_file),
            "--journal-dir", str(self.args.journal_dir),
            "--credentials-dir", str(self.args.credentials_dir),
            "--object-contract", str(self.args.object_contract),
            "--work-dir", str(self.args.work_dir),
            "restore", str(self.args.archive),
            "--maintenance-database", self.args.maintenance_database,
            "--app-image", self.args.app_image,
        ]
        if self.args.no_rehearse:
            argv.append("--no-rehearse")
        if self.args.keep_rehearsal:
            argv.append("--keep-rehearsal")
        if self.args.keep_database:
            argv.append("--keep-database")
        return argv

    def journal_records(self) -> list[dict]:
        view = journal.Journal(self.args.journal_dir, require_root=False).read()
        return [dict(record.payload) for record in view.records if record.kind == "temp_db"]


def test_the_combined_restore_rehearses_with_the_operations_run_id_and_confirms_removal(
    monkeypatch, tmp_path, capsys
):
    """The regression: before FIX-01 this path raised TypeError after verification.

    `rehearse` requires a run id; the combined restore now passes its own journaled
    operation id, which names exactly one disposable container, and removes it on the way
    out."""
    rig = _Rig(monkeypatch, tmp_path)

    assert rig.run() == maintenance.EX_OK

    # The real `rehearse` body ran and named its container from the restore's operation id.
    assert rig.tools.started == [CONTAINER_NAME]
    # The caller - not `rehearse` - removed it, verified against the pinned image.
    assert rig.tools.removed == [(CONTAINER_NAME, IMAGE)]
    # The verified temporary database was dropped as before.
    assert rig.drops == [DATABASE]
    output = capsys.readouterr().out
    assert "ok [rehearse]: isolated --network none restore verified" in output
    assert "attachments: 0 attachments" in output
    assert f"ok [cleanup]: {DATABASE} dropped and absence verified" in output
    # The run id is the record an operator can read, not a value held only in memory.
    authorized = [r for r in rig.journal_records() if r.get("action") == "authorized"]
    assert [r["operation_id"] for r in authorized] == [OPERATION_ID]


def test_a_rehearsal_failure_still_removes_the_container_and_drops_the_temporary_database(
    monkeypatch, tmp_path
):
    rig = _Rig(monkeypatch, tmp_path)
    rig.fail_verification()

    with pytest.raises(maintenance.MaintenanceError) as refused:
        rig.run()

    assert refused.value.code == "isolated_verification"
    assert rig.tools.removed == [(CONTAINER_NAME, IMAGE)]
    assert rig.drops == [DATABASE]


def test_an_unconfirmed_removal_is_the_dominant_outcome_and_names_the_recovery_command(
    monkeypatch, tmp_path
):
    """R2: a container that may still hold plaintext is never suppressed by a verdict."""
    rig = _Rig(monkeypatch, tmp_path, remove_result=False)

    with pytest.raises(maintenance.MaintenanceError) as refused:
        rig.run()

    assert refused.value.code == "rehearsal_cleanup"
    assert rig.tools.removed == [(CONTAINER_NAME, IMAGE)]
    # The temporary database is still reconciled; the clean-up refusal is the verdict.
    assert rig.drops == [DATABASE]
    assert refused.value.detail is not None
    assert f"cleanup-rehearsal --run-id {OPERATION_ID}" in refused.value.detail
    assert CONTAINER_NAME in refused.value.detail


def test_keep_rehearsal_retains_the_container_without_failing_the_restore(
    monkeypatch, tmp_path, capsys
):
    rig = _Rig(monkeypatch, tmp_path)
    rig.args.keep_rehearsal = True

    assert rig.run() == maintenance.EX_OK

    assert rig.tools.started == [CONTAINER_NAME]
    assert rig.tools.removed == []
    assert rig.drops == [DATABASE]
    assert "retained disposable container" in capsys.readouterr().out


# --- FIX-01B: both clean-ups are attempted and neither failure overwrites the other -----------
#
# `restore` performs two independent clean-ups: the verified removal of its one disposable
# container, then the drop of the temporary database. Before FIX-01B a database refusal raised
# from the second one escaped before the container outcome was examined, so an unconfirmed
# removal (a container that may still hold plaintext) was reported as exit 3 `sql_refused`
# without its identity or recovery command. These tests drive the REAL `_drop_temporary_database`
# behind the fake Docker/SQL boundary above, so the journal transitions are the production ones.


def _combined_restore(
    monkeypatch, tmp_path, *, container_removed, database_dropped
):
    return _Rig(
        monkeypatch,
        tmp_path,
        real_drop=True,
        drop_fails=not database_dropped,
        remove_result=container_removed,
    )


@pytest.mark.parametrize(
    "container_removed, database_dropped, expected",
    [
        (True, True, maintenance.EX_OK),
        (False, True, maintenance.EX_UNAVAILABLE),
        (True, False, maintenance.EX_REFUSED),
        (False, False, maintenance.EX_UNAVAILABLE),
    ],
)
def test_each_cleanup_combination_keeps_its_documented_exit_status(
    monkeypatch, tmp_path, container_removed, database_dropped, expected
):
    rig = _combined_restore(
        monkeypatch,
        tmp_path,
        container_removed=container_removed,
        database_dropped=database_dropped,
    )

    assert rig.run_cli() == expected
    # Both clean-ups are always attempted; the DB attempt never suppresses the container attempt.
    assert rig.tools.removed == [(CONTAINER_NAME, IMAGE)]
    assert rig.tools.drop_attempts == 1


def test_combined_cleanup_failure_preserves_both_failures_and_the_journal_state(
    monkeypatch, tmp_path, capsys
):
    """The exact Codex scenario: unconfirmed removal, then a failed DROP, must be exit 4."""
    rig = _combined_restore(
        monkeypatch, tmp_path, container_removed=False, database_dropped=False
    )

    assert rig.run_cli() == maintenance.EX_UNAVAILABLE

    errors = capsys.readouterr().err
    assert "refused [rehearsal_cleanup]" in errors
    # The container is the dominant fact, with its exact identity and recovery command.
    assert CONTAINER_NAME in errors
    assert f"cleanup-rehearsal --run-id {OPERATION_ID}" in errors
    # The later database refusal is preserved alongside it, never instead of it.
    assert f"temporary database {DATABASE}" in errors
    assert "[sql_refused]" in errors
    assert "cleanup_pending" in errors
    assert "[sql_refused] the database refused a statement" in errors
    # Both clean-ups were actually attempted.
    assert rig.tools.removed == [(CONTAINER_NAME, IMAGE)]
    assert rig.tools.drop_attempts == 1
    # The journal is left in the unresolved state the operator must review.
    records = [r for r in rig.journal_records() if r.get("database") == DATABASE]
    assert records[-1]["action"] == "cleanup_pending"


def test_a_corrupt_journal_head_never_masks_the_cleanup_refusal(
    monkeypatch, tmp_path, capsys
):
    """FIX-01C: an unreadable (non-UTF-8) journal head is unavailable evidence, not a crash.

    Codex's scenario: the container removal is unconfirmed, the DROP fails, and the
    supplementary journal observation then hits a head anchor that is not valid UTF-8. Before
    FIX-01C the `UnicodeDecodeError` escaped `_temporary_journal_action` and the run exited 1
    as an internal error, losing the recovery identity. It must stay exit 4 `rehearsal_cleanup`
    with the container identity, the recovery command and the database failure preserved."""
    rig = _combined_restore(
        monkeypatch, tmp_path, container_removed=False, database_dropped=False
    )
    real_drop = maintenance._drop_temporary_database
    head = rig.args.journal_dir / journal.HEAD_FILE

    def drop_then_corrupt_head(*args, **kwargs):
        try:
            real_drop(*args, **kwargs)
        finally:
            # The failed DROP leaves the journal in `cleanup_pending`; corrupt only the head,
            # exactly as interrupted storage would, so only the supplementary read is affected.
            head.write_bytes(b"\xff")

    monkeypatch.setattr(maintenance, "_drop_temporary_database", drop_then_corrupt_head)

    assert rig.run_cli() == maintenance.EX_UNAVAILABLE

    errors = capsys.readouterr().err
    # The dominant refusal and every recovery detail survive the unreadable journal.
    assert "refused [rehearsal_cleanup]" in errors
    assert CONTAINER_NAME in errors
    assert f"cleanup-rehearsal --run-id {OPERATION_ID}" in errors
    assert f"temporary database {DATABASE}" in errors
    assert "[sql_refused] the database refused a statement" in errors
    # The journal's action is simply unavailable; the corrupt file is never repaired.
    assert "its journal entry remains" not in errors
    assert head.read_bytes() == b"\xff"
    assert rig.tools.removed == [(CONTAINER_NAME, IMAGE)]
    assert rig.tools.drop_attempts == 1


def test_only_a_database_cleanup_failure_keeps_the_documented_sql_refusal(
    monkeypatch, tmp_path, capsys
):
    """Container confirmed removed: the database refusal is unchanged (exit 3, no cleanup code)."""
    rig = _combined_restore(
        monkeypatch, tmp_path, container_removed=True, database_dropped=False
    )

    with pytest.raises(maintenance.MaintenanceError) as refused:
        rig.run()

    assert refused.value.code == "sql_refused"
    assert rig.tools.removed == [(CONTAINER_NAME, IMAGE)]
    assert rig.tools.drop_attempts == 1
    assert "rehearsal_cleanup" not in capsys.readouterr().err


def test_keep_rehearsal_still_surfaces_a_database_cleanup_failure(monkeypatch, tmp_path):
    rig = _combined_restore(
        monkeypatch, tmp_path, container_removed=True, database_dropped=False
    )
    rig.args.keep_rehearsal = True

    with pytest.raises(maintenance.MaintenanceError) as refused:
        rig.run()

    assert refused.value.code == "sql_refused"
    assert rig.tools.removed == []
    assert rig.tools.drop_attempts == 1


def test_keep_database_skips_the_drop_but_keeps_the_container_outcome(monkeypatch, tmp_path):
    rig = _Rig(monkeypatch, tmp_path, real_drop=True, remove_result=False)
    rig.args.keep_database = True

    with pytest.raises(maintenance.MaintenanceError) as refused:
        rig.run()

    assert refused.value.code == "rehearsal_cleanup"
    assert rig.tools.removed == [(CONTAINER_NAME, IMAGE)]
    assert rig.tools.drop_attempts == 0


def test_no_rehearse_has_no_container_outcome_and_keeps_the_database_refusal(
    monkeypatch, tmp_path
):
    rig = _Rig(monkeypatch, tmp_path, real_drop=True, drop_fails=True)
    rig.args.no_rehearse = True

    with pytest.raises(maintenance.MaintenanceError) as refused:
        rig.run()

    assert refused.value.code == "sql_refused"
    assert rig.tools.started == []
    assert rig.tools.removed == []
    assert rig.tools.drop_attempts == 1


def _signalled_cleanup(signum: int = signal.SIGTERM):
    """Simulate a signal recorded inside the clean-up's deferred critical section."""

    @contextlib.contextmanager
    def deferred():
        recorder = maintenance._DeferredSignals()
        recorder.signums.append(signum)
        yield recorder

    return deferred


def test_sigterm_during_a_confirmed_cleanup_still_exits_6(monkeypatch, tmp_path):
    rig = _combined_restore(
        monkeypatch, tmp_path, container_removed=True, database_dropped=True
    )
    monkeypatch.setattr(
        maintenance, "_deferred_cancellation", _signalled_cleanup()
    )

    assert rig.run_cli() == maintenance.EX_CANCELLED
    assert rig.tools.removed == [(CONTAINER_NAME, IMAGE)]
    assert rig.tools.drop_attempts == 1


def test_sigterm_with_an_unconfirmed_cleanup_and_a_database_failure_reports_both(
    monkeypatch, tmp_path, capsys
):
    rig = _combined_restore(
        monkeypatch, tmp_path, container_removed=False, database_dropped=False
    )
    monkeypatch.setattr(
        maintenance, "_deferred_cancellation", _signalled_cleanup()
    )

    assert rig.run_cli() == maintenance.EX_UNAVAILABLE
    errors = capsys.readouterr().err
    assert "refused [rehearsal_cleanup]" in errors
    assert f"cleanup-rehearsal --run-id {OPERATION_ID}" in errors
    assert "[sql_refused] the database refused a statement" in errors
