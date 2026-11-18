"""WP-2D remediation R1: the six reproduced Codex findings (F2-F6 host contracts).

F1's normalizer regressions live in `tests/test_pg_backup.py` next to the deparse
equivalence test they constrain. This module covers the archive payload validation,
the temporary-database fence, the standalone rehearsal dispatch, the backup mutex
lifetime and the attachment proof.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest

from house_helper.persistence import pg_backup as backup
from house_helper.persistence import pg_contract
from house_helper.persistence import pg_journal as journal
from tests.test_pg_backup import PINNED_EXTRACTION, _manifest

from deploy.postgres import maintenance

_PINNED_IMAGE = "postgres@sha256:" + "0" * 64
DUMP_BYTES = b"custom-format-dump-bytes"


# --- F2: archive validation extracts the scoped payload and checks TABLE DATA ---------------


def _valid_manifest():
    return _manifest(
        snapshot={"bytes": len(DUMP_BYTES), "sha256": backup.sha256_bytes(DUMP_BYTES)}
    )


def _valid_archive(tmp_path):
    manifest = _valid_manifest()
    path = tmp_path / "point.tar"
    backup.write_archive(path, manifest=manifest, dump=DUMP_BYTES)
    return path, manifest


def _toc(manifest):
    lines = [";", "; header"]
    entry_id = 1
    for name in sorted(manifest["tables"]):
        lines.append(f"{entry_id}; 1259 {entry_id} TABLE DATA public {name} hh_owner")
        entry_id += 1
    lines.append(f"{entry_id}; 0 0 SEQUENCE SET public messages_id_seq hh_owner")
    return "\n".join(lines) + "\n"


def _extraction(last_value=5, is_called=True):
    return PINNED_EXTRACTION.replace(
        "setval('public.messages_id_seq', 1, false)",
        "setval('public.messages_id_seq', {}, {})".format(
            last_value, "true" if is_called else "false"
        ),
    )


class _ArchiveTools:
    def __init__(self, toc, extraction):
        self.toc = toc
        self.extraction = extraction
        self.requests = []
        self.removed = []

    def pg_restore_list(self, work):
        return self.toc

    def sequence_extraction(self, work, list_text):
        self.requests.append(list_text)
        return self.extraction

    def remove_verified(self, name, *, image=None):
        self.removed.append((name, image))
        return True


def _work(tmp_path):
    path = tmp_path / "work"
    path.mkdir(exist_ok=True)
    return path


def test_archive_validation_extracts_and_matches_the_sequence_payload(tmp_path):
    path, manifest = _valid_archive(tmp_path)
    tools = _ArchiveTools(_toc(manifest), _extraction())
    point, entries = maintenance.validate_archive(path, tools, work=_work(tmp_path))
    assert point.manifest == manifest
    assert tools.requests and "SEQUENCE SET public messages_id_seq" in tools.requests[0]


def test_archive_validation_rejects_extra_sequence_sql(tmp_path):
    path, manifest = _valid_archive(tmp_path)
    extra = _extraction().replace(
        "SELECT pg_catalog.setval", "SELECT 1;\nSELECT pg_catalog.setval"
    )
    with pytest.raises(backup.BackupError):
        maintenance.validate_archive(
            path, _ArchiveTools(_toc(manifest), extra), work=_work(tmp_path)
        )


def test_archive_validation_rejects_a_mismatched_sequence_pair(tmp_path):
    path, manifest = _valid_archive(tmp_path)
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.validate_archive(
            path, _ArchiveTools(_toc(manifest), _extraction(2, True)), work=_work(tmp_path)
        )
    assert refused.value.code == "archive_sequence"


def test_archive_validation_requires_one_table_data_entry_per_table(tmp_path):
    path, manifest = _valid_archive(tmp_path)
    lines = _toc(manifest).splitlines()
    table_lines = [line for line in lines if "TABLE DATA" in line]
    missing = "\n".join(line for line in lines if line != table_lines[0]) + "\n"
    duplicated = "\n".join(lines) + "\n" + table_lines[0] + "\n"
    unexpected = (
        "\n".join(lines) + "\n999; 1259 999 TABLE DATA public extra_table hh_owner\n"
    )
    for bad in (missing, duplicated, unexpected):
        with pytest.raises(backup.BackupError) as refused:
            maintenance.validate_archive(
                path, _ArchiveTools(bad, _extraction()), work=_work(tmp_path)
            )
        assert refused.value.code == "table_data_selection"


def test_verify_archive_rejects_an_invalid_archive_before_reporting_success(
    monkeypatch, tmp_path
):
    path, manifest = _valid_archive(tmp_path)
    lines = [line for line in _toc(manifest).splitlines() if "TABLE DATA" not in line]
    tools = _ArchiveTools("\n".join(lines) + "\n", _extraction())
    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: _PINNED_IMAGE)
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: tools)
    args = types.SimpleNamespace(
        compose_file=tmp_path / "compose.yaml",
        archive=path,
        object_contract=Path(__file__).parent / "fixtures" / "pg_object_contract.json",
        work_dir=tmp_path,
        keep_work=False,
    )
    with pytest.raises(backup.BackupError) as refused:
        maintenance.verify_archive_command(args)
    assert refused.value.code == "table_data_selection"


# --- F3: a `created_fenced` database is adopted only when it is fenced and hardened --------


TEMPORARY = "house_helper_restore_" + "c" * 16
SYSTEM_IDENTIFIER = "7709166311476633635"
# The exact expansion after fresh hardening (REVOKE ALL FROM PUBLIC; GRANT CONNECT TO
# hh_restore): the owner holds CONNECT/CREATE/TEMPORARY, the restore login CONNECT only,
# nothing grantable.
FENCED_ACL = (
    "hh_owner=CONNECT,hh_owner=CREATE,hh_owner=TEMPORARY,hh_restore=CONNECT"
)
FENCED_ROW = f"{TEMPORARY}|f|hh_owner|t|0|{FENCED_ACL}"


def _journal(tmp_path):
    return journal.Journal(tmp_path, require_root=False)


def _created_fenced_record():
    return backup.temp_database_record(
        action="created_fenced",
        operation_id="a" * 32,
        token="b" * 16,
        database=TEMPORARY,
        purpose="restore_verification",
        system_identifier=SYSTEM_IDENTIFIER,
    )


def _reconcile(journal_file, monkeypatch, row):
    monkeypatch.setattr(maintenance, "psql", lambda *a, **k: row)
    maintenance.reconcile_all_temporary_databases(
        journal_file,
        maintenance_database="postgres",
        passfile_value=object(),
        tools=object(),
        network="house-helper-pg-db",
        system_identifier=SYSTEM_IDENTIFIER,
    )


@pytest.mark.parametrize(
    "row",
    [
        # The interruption window after enablement: hardened, but no longer fenced.
        f"{TEMPORARY}|t|hh_owner|t|0|{FENCED_ACL}",
        # Not hardened: PUBLIC still has an entry.
        f"{TEMPORARY}|f|hh_owner|t|1|{FENCED_ACL},PUBLIC=CONNECT",
        # No materialized ACL at all.
        f"{TEMPORARY}|f|hh_owner|f|0|",
        # The required restore login is absent (only the owner's entries).
        f"{TEMPORARY}|f|hh_owner|t|0|hh_owner=CONNECT,hh_owner=CREATE,hh_owner=TEMPORARY",
        # CONNECT alone, without the owner's exact expansion, is not the contract.
        f"{TEMPORARY}|f|hh_owner|t|0|hh_restore=CONNECT",
        # CONNECT plus another privilege for the intended login.
        f"{TEMPORARY}|f|hh_owner|t|0|{FENCED_ACL},hh_restore=TEMPORARY",
        f"{TEMPORARY}|f|hh_owner|t|0|{FENCED_ACL},hh_restore=CREATE",
        # CONNECT WITH GRANT OPTION.
        f"{TEMPORARY}|f|hh_owner|t|0|hh_owner=CONNECT,hh_owner=CREATE,hh_owner=TEMPORARY,hh_restore=CONNECT*",
        # The owner itself carries a grant option or a reduced set.
        f"{TEMPORARY}|f|hh_owner|t|0|hh_owner=CONNECT*,hh_owner=CREATE,hh_owner=TEMPORARY,hh_restore=CONNECT",
        f"{TEMPORARY}|f|hh_owner|t|0|hh_owner=CONNECT,hh_owner=CREATE,hh_restore=CONNECT",
        # An unexpected grantee.
        f"{TEMPORARY}|f|hh_owner|t|0|{FENCED_ACL},hh_app=CONNECT",
        # An unexpected owner.
        f"{TEMPORARY}|f|postgres|t|0|{FENCED_ACL}",
        # An observation for another database.
        f"house_helper_restore_{'d' * 16}|f|hh_owner|t|0|{FENCED_ACL}",
        # Malformed observations.
        f"{TEMPORARY}|f|hh_owner|t|0",
        f"{TEMPORARY}|f|hh_owner|t|0|hh_restore",
        f"{TEMPORARY}|f|hh_owner|t|x|{FENCED_ACL}",
    ],
)
def test_a_created_fenced_record_is_refused_unless_the_fence_and_acl_hold(
    monkeypatch, tmp_path, row
):
    journal_file = _journal(tmp_path)
    journal_file.append("temp_db", _created_fenced_record())
    with pytest.raises(maintenance.MaintenanceError) as refused:
        _reconcile(journal_file, monkeypatch, row)
    assert refused.value.code == "temp_database_unresolved"


def test_a_properly_fenced_and_hardened_database_is_adopted(monkeypatch, tmp_path):
    journal_file = _journal(tmp_path)
    journal_file.append("temp_db", _created_fenced_record())
    _reconcile(journal_file, monkeypatch, FENCED_ROW)
    assert journal_file.read().records[-1].payload["action"] == "created_fenced"


def test_the_exact_acl_comparison_is_order_independent(monkeypatch, tmp_path):
    journal_file = _journal(tmp_path)
    journal_file.append("temp_db", _created_fenced_record())
    reversed_acl = ",".join(reversed(FENCED_ACL.split(",")))
    _reconcile(
        journal_file, monkeypatch, f"{TEMPORARY}|f|hh_owner|t|0|{reversed_acl}"
    )
    assert journal_file.read().records[-1].payload["action"] == "created_fenced"


def test_the_single_database_reconciliation_applies_the_same_fence_rule(
    monkeypatch, tmp_path
):
    journal_file = _journal(tmp_path)
    journal_file.append("temp_db", _created_fenced_record())
    monkeypatch.setattr(maintenance, "psql", lambda *a, **k: FENCED_ROW)

    def reconcile():
        maintenance.reconcile_temporary_database(
            journal_file,
            database=TEMPORARY,
            maintenance_database="postgres",
            passfile_value=object(),
            tools=object(),
            network="house-helper-pg-db",
            system_identifier=SYSTEM_IDENTIFIER,
        )

    reconcile()
    monkeypatch.setattr(
        maintenance, "psql", lambda *a, **k: FENCED_ROW.replace("|f|", "|t|", 1)
    )
    with pytest.raises(maintenance.MaintenanceError):
        reconcile()


def _restore_plan():
    return maintenance.RestorePlan(
        database=TEMPORARY,
        maintenance_database="postgres",
        token="b" * 16,
        operation_id="a" * 32,
    )


def test_the_fresh_restore_database_acl_check_accepts_only_the_exact_contract(monkeypatch):
    monkeypatch.setattr(maintenance, "psql", lambda *a, **k: FENCED_ACL + "\n")
    maintenance._verify_restore_database_acl(
        object(), object(), _restore_plan(), database="postgres", network="house-helper-pg-db"
    )
    for wrong in (
        FENCED_ACL + ",hh_restore=TEMPORARY",
        FENCED_ACL + ",hh_restore=CONNECT*",
        FENCED_ACL + ",hh_app=CONNECT",
        FENCED_ACL + ",PUBLIC=CONNECT",
        "hh_owner=CONNECT,hh_owner=CREATE,hh_owner=TEMPORARY",
        "hh_owner=CONNECT*,hh_owner=CREATE,hh_owner=TEMPORARY,hh_restore=CONNECT",
    ):
        monkeypatch.setattr(maintenance, "psql", lambda *a, **k: wrong)
        with pytest.raises(maintenance.MaintenanceError) as refused:
            maintenance._verify_restore_database_acl(
                object(),
                object(),
                _restore_plan(),
                database="postgres",
                network="house-helper-pg-db",
            )
        assert refused.value.code == "database_acl"


def test_created_fenced_is_journaled_only_after_hardening(monkeypatch):
    events = []
    monkeypatch.setattr(
        maintenance, "_create_fenced_database", lambda *a, **k: events.append("create")
    )
    monkeypatch.setattr(
        maintenance, "_harden_fenced", lambda *a, **k: events.append("harden")
    )
    monkeypatch.setattr(maintenance, "_enable", lambda *a, **k: events.append("enable"))
    monkeypatch.setattr(
        maintenance,
        "_append_temp",
        lambda journal_file, payload: events.append(payload["action"]),
    )
    plan = maintenance.RestorePlan(
        database=TEMPORARY,
        maintenance_database="postgres",
        token="b" * 16,
        operation_id="a" * 32,
    )
    maintenance._establish_fenced_database(
        object(),
        object(),
        object(),
        plan,
        _created_fenced_record(),
        collate="C",
        ctype="C",
        network="house-helper-pg-db",
    )
    assert events == ["create", "harden", "created_fenced", "enable", "restoring"]


# --- F4: the standalone `rehearse` command dispatches through the archive entries -----------


def test_the_standalone_rehearse_command_completes_dispatch(monkeypatch, tmp_path):
    path, manifest = _valid_archive(tmp_path)
    tools = _ArchiveTools(_toc(manifest), _extraction())
    calls = []

    def fake_rehearse(tools, *, point, work, fixture, app_image, run_id, keep=False):
        calls.append((point.manifest["alembic_revision"], run_id))
        return {
            "system_identifier": "1",
            "database": "house_helper_restore_x",
            "revision": point.manifest["alembic_revision"],
            "tables": len(point.manifest["tables"]),
            "attachments": {
                "count": 0,
                "bytes": 0,
                "declared_bytes": 0,
                "sample": False,
            },
        }

    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: _PINNED_IMAGE)
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: tools)
    monkeypatch.setattr(maintenance, "rehearse", fake_rehearse)
    monkeypatch.setattr(backup, "toc_contract_failures", lambda entries, fixture: ())
    assert maintenance.main(["rehearse", str(path)]) == maintenance.EX_OK
    ((revision, run_id),) = calls
    assert revision == manifest["alembic_revision"]
    # F2: the run id names the one disposable container, and removal is by that exact name only.
    assert maintenance.RUN_ID_RE.fullmatch(run_id)
    assert tools.removed == [(maintenance.REHEARSAL_CONTAINER_PREFIX + run_id, _PINNED_IMAGE)]


# --- F5: the backup mutex covers the whole logical backup and publication ------------------


def _backup_facts():
    lines = [
        "server_version_num|160015",
        "database|UTF8|C|C",
        "revision|0014_user_measure_overrides",
        "max_messages_id|",
        "sequence_catalog|public|messages_id_seq|hh_owner|public.messages_id_seq|1|bigint|1|1|9223372036854775807|1|1|f",
    ]
    for name in sorted(pg_contract.expected_tables()):
        lines.append(f"table|{name}|0|" + backup.sha256_bytes(b""))
    return "\n".join(lines) + "\n"


class _BackupTool:
    """The Docker boundary plus an in-process stand-in for the lock holder.

    The stand-in keeps the mutex for the same span as the real holder: acquisition
    through publication and read-back until the holder is removed. Publication options
    let a test terminate the holder immediately before or during the publication call.
    """

    def __init__(
        self,
        lock=None,
        *,
        die_before_publish=False,
        die_during_publish=False,
        die_before_confirm=False,
    ):
        self.lock = lock or threading.Lock()
        self.die_before_publish = die_before_publish
        self.die_during_publish = die_during_publish
        self.die_before_confirm = die_before_confirm
        self.acquired = 0
        self.released = 0
        self.holders = {}
        self.cleanup_holders = []
        self.holder_control_dirs = []
        self.cleanup_control_dirs = []
        self.publications = []
        self.confirmations = []
        self.cleanups = []
        self.dump_bytes = DUMP_BYTES
        self.before_failure = None
        self.toc = "1; 0 0 SEQUENCE SET public messages_id_seq hh_owner\n"
        self.extraction = _extraction()

    def image_digest(self):
        return {}

    def inspect_volume(self, name):
        return {}

    def start_lock_holder(self, *, name, control_dir, output_dir, timeout=30.0):
        if not self.lock.acquire(timeout=timeout):
            raise maintenance.MaintenanceError("backup_lock", "busy")
        self.acquired += 1
        container = f"holder-{self.acquired}"
        self.holders[container] = (Path(control_dir), Path(output_dir))
        self.holder_control_dirs.append(Path(control_dir))
        return container

    def start_cleanup_holder(self, *, name, control_dir, output_dir, timeout=30.0):
        """The cleanup-only recovery holder: same mutex, no publication path.

        It is a distinct start method so the production recovery path must ask for the
        cleanup-only holder explicitly. A control directory that still contains a
        publication request is never passed here by `_recover_cleanup`.
        """
        if not self.lock.acquire(timeout=timeout):
            raise maintenance.MaintenanceError("backup_lock", "busy")
        self.acquired += 1
        container = f"holder-{self.acquired}"
        self.holders[container] = (Path(control_dir), Path(output_dir))
        self.cleanup_holders.append(container)
        self.cleanup_control_dirs.append(Path(control_dir))
        return container

    def publish_in_holder(
        self, container, *, control_dir, staging_name, output_name, replace=False, timeout=120.0
    ):
        if container not in self.holders:
            raise maintenance.MaintenanceError("backup_lock", "holder terminated")
        if container in self.cleanup_holders:
            # A recovery holder is structurally unable to publish; the stand-in keeps
            # the same rule so a regression cannot pass through it.
            raise maintenance.MaintenanceError(
                "backup_lock", "the recovery holder cannot publish"
            )
        _, output_dir = self.holders[container]
        staging = output_dir / staging_name
        final = output_dir / output_name
        self.publications.append((container, self.lock.locked(), self.released))
        if self.die_before_publish:
            self._terminate(container)
            if self.before_failure is not None:
                self.before_failure()
            raise maintenance.MaintenanceError("backup_lock", "holder terminated")
        # The holder reports the staged inode, which is the inode a link or rename
        # publishes (the real holder does the same with `stat -c '%d %i'`).
        recorded = staging.stat()
        identity = f"{recorded.st_dev} {recorded.st_ino}"
        if self.die_during_publish:
            if replace:
                os.replace(staging, final)
            else:
                os.link(staging, final)
            self._terminate(container)
            if self.before_failure is not None:
                self.before_failure()
            raise maintenance.MaintenanceError(
                "backup_lock", "holder terminated during publication"
            )
        if replace:
            os.replace(staging, final)
            return f"ok {identity}"
        try:
            os.link(staging, final)
        except FileExistsError:
            return "exists"
        staging.unlink()
        return f"ok {identity}"

    def cleanup_in_holder(
        self, container, *, control_dir, output_name, identity, timeout=60.0
    ):
        """The stand-in for the holder's mutex-held identity check and unlink."""
        if container not in self.holders:
            raise maintenance.MaintenanceError("backup_lock", "holder terminated")
        _, output_dir = self.holders[container]
        final = output_dir / output_name
        held = self.lock.locked()
        if not held:
            self.cleanups.append((container, held, self.released, "refused"))
            raise maintenance.MaintenanceError("backup_lock", "the mutex is not held")
        if final.is_symlink():
            outcome = "error"
        elif not final.exists():
            outcome = "absent"
        elif (final.stat().st_dev, final.stat().st_ino) != tuple(identity):
            outcome = "retained"
        else:
            final.unlink()
            outcome = "removed"
        self.cleanups.append((container, held, self.released, outcome))
        return outcome

    def confirm_lock_holder(self, container, *, control_dir, timeout=60.0):
        self.confirmations.append((container, self.lock.locked(), self.released))
        if self.die_before_confirm:
            self._terminate(container)
            raise maintenance.MaintenanceError(
                "backup_lock", "holder terminated before success was acknowledged"
            )

    def _terminate(self, container):
        if container in self.holders:
            self.holders.pop(container)
            self.released += 1
            self.lock.release()

    def remove(self, container):
        self._terminate(container)

    def run(self, program, **kwargs):
        mounts = kwargs.get("mounts") or ()
        work = next(
            (Path(source) for source, target, _ in mounts if target == maintenance.STAGING),
            None,
        )
        assert work is not None
        (work / backup.DUMP_NAME).write_bytes(self.dump_bytes)
        (work / "dump.ok").write_text("ok", encoding="utf-8")
        (work / "facts.txt").write_text(_backup_facts(), encoding="utf-8")
        return subprocess.CompletedProcess(program, 0, stdout=b"", stderr=b"")

    def pg_restore_list(self, work):
        return self.toc

    def sequence_extraction(self, work, list_text):
        return self.extraction

    def pg_dump_version(self):
        return "16.15"


def _backup_args(tmp_path, output, *, overwrite=False):
    return types.SimpleNamespace(
        compose_file=tmp_path / "compose.yaml",
        journal_dir=tmp_path / "journal",
        credentials_dir=tmp_path / "credentials",
        work_dir=tmp_path,
        keep_work=False,
        output=output,
        overwrite=overwrite,
    )


def _install_target_fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        maintenance,
        "verify_target",
        lambda *a, **k: types.SimpleNamespace(
            spec=types.SimpleNamespace(image=_PINNED_IMAGE, network="house-helper-pg-db"),
            system_identifier=SYSTEM_IDENTIFIER,
        ),
    )
    monkeypatch.setattr(
        maintenance,
        "passfile",
        lambda *a, **k: maintenance.Passfile(
            role="hh_backup", host_path=tmp_path / "hh_backup.pgpass"
        ),
    )


def _install_backup_fakes(monkeypatch, tmp_path, tools):
    _install_target_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: tools)


def test_two_concurrent_backups_create_at_most_one_archive(monkeypatch, tmp_path):
    tools = _BackupTool()
    _install_backup_fakes(monkeypatch, tmp_path, tools)
    output = tmp_path / "point.tar"
    args = _backup_args(tmp_path, output)
    results = []

    def attempt():
        try:
            results.append(maintenance.backup_command(args))
        except maintenance.MaintenanceError as error:
            results.append(error.code)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(map(str, results)) == ["0", "output_exists"]
    assert output.is_file()
    assert backup.read_archive(output).manifest["sequence"]["last_value"] == 5
    assert tools.acquired == 2 and tools.released == 2


def test_a_failed_publication_leaves_neither_output_nor_staging(monkeypatch, tmp_path):
    output = tmp_path / "point.tar"

    def refuse(*args, **kwargs):
        raise backup.BackupError("write", "refused")

    monkeypatch.setattr(backup, "write_archive", refuse)
    with pytest.raises(backup.BackupError):
        maintenance._publish_archive(
            output, manifest=_valid_manifest(), dump=DUMP_BYTES, overwrite=False
        )
    assert not output.exists()
    assert list(tmp_path.glob(f".{output.name}.partial-*")) == []


def test_no_replace_publication_succeeds_atomically_and_cleans_staging(tmp_path):
    output = tmp_path / "point.tar"
    point = maintenance._publish_archive(
        output, manifest=_valid_manifest(), dump=DUMP_BYTES, overwrite=False
    )
    assert output.is_file()
    assert point.dump == DUMP_BYTES
    assert list(tmp_path.glob(f".{output.name}.partial-*")) == []


def test_a_competing_file_appearing_just_before_publication_is_never_replaced(
    monkeypatch, tmp_path
):
    output = tmp_path / "point.tar"
    competing = b"a competing file that must survive byte for byte"
    real_write = backup.write_archive

    def write_then_compete(path, **kwargs):
        real_write(path, **kwargs)
        # The race-injection point: the final path appears after the existence check
        # and before the publication call.
        output.write_bytes(competing)

    monkeypatch.setattr(backup, "write_archive", write_then_compete)
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance._publish_archive(
            output, manifest=_valid_manifest(), dump=DUMP_BYTES, overwrite=False
        )
    assert refused.value.code == "output_exists"
    assert output.read_bytes() == competing
    assert list(tmp_path.glob(f".{output.name}.partial-*")) == []


def test_an_explicitly_authorized_overwrite_still_replaces_the_archive(tmp_path):
    output = tmp_path / "point.tar"
    output.write_bytes(b"an older recovery point")
    point = maintenance._publish_archive(
        output, manifest=_valid_manifest(), dump=DUMP_BYTES, overwrite=True
    )
    assert backup.read_archive(output).dump == DUMP_BYTES
    assert point.dump == DUMP_BYTES


def test_the_lock_holder_script_takes_both_locks_signals_readiness_and_publishes():
    script = maintenance._lock_holder_script()
    assert maintenance.STATE_LOCK in script
    assert maintenance.BACKUP_MUTEX in script
    assert "flock -s -n 9" in script and "flock -x -n 8" in script
    assert f"touch {maintenance.LOCK_CONTROL_TARGET}/{maintenance.LOCK_READY_NAME}" in script
    # F5-B: no unconditional lifetime. The holder expires only when the host-refreshed
    # deadline is stale, and it performs the publication itself.
    assert "exec sleep" not in script
    assert maintenance.LOCK_DEADLINE_NAME in script
    assert 'ln "' in script and 'mv -f "' in script
    assert maintenance.LOCK_CONFIRM_NAME in script and "alive" in script
    # F5-B: publication reports the published inode, and the same holder performs the
    # identity-checked failure cleanup under the mutex it already owns.
    assert 'stat -c "%d %i"' in script and "ok $identity" in script
    assert maintenance.LOCK_CLEANUP_NAME in script
    assert "outcome=removed" in script and "outcome=retained" in script
    assert '"$identity" = "$device $inode"' in script


def test_a_holder_that_terminates_before_publication_publishes_nothing(
    monkeypatch, tmp_path
):
    tools = _BackupTool(die_before_publish=True)
    _install_backup_fakes(monkeypatch, tmp_path, tools)
    output = tmp_path / "point.tar"
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.backup_command(_backup_args(tmp_path, output))
    assert refused.value.code == "backup_lock"
    assert not output.exists()
    assert list(tmp_path.glob(".point.tar.partial-*")) == []
    # F5-B: the recovery holder re-acquires the released mutex to check that nothing
    # was published, then exits; the operation still reports failure.
    assert tools.acquired == 2 and tools.released == 2
    assert len(tools.cleanups) == 1
    assert tools.cleanups[0][1] is True and tools.cleanups[0][3] == "absent"
    # R4: recovery is a cleanup-only holder on a fresh control directory.
    assert tools.cleanup_holders
    assert tools.cleanup_control_dirs[0] != tools.holder_control_dirs[0]


def test_a_holder_that_terminates_during_publication_removes_its_unreported_archive(
    monkeypatch, tmp_path
):
    tools = _BackupTool(die_during_publish=True)
    _install_backup_fakes(monkeypatch, tmp_path, tools)
    output = tmp_path / "point.tar"
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.backup_command(_backup_args(tmp_path, output))
    assert refused.value.code == "backup_lock"
    # The link completed in the instant before the holder died; publication was never
    # reported, so the recovery holder removes the exact inode that was staged.
    assert not output.exists()
    assert list(tmp_path.glob(".point.tar.partial-*")) == []
    assert len(tools.cleanups) == 1
    assert tools.cleanups[0][1] is True and tools.cleanups[0][3] == "removed"
    # R4: the removing holder is the cleanup-only one, not a replaying publisher.
    assert tools.cleanup_holders
    assert tools.cleanup_control_dirs[0] != tools.holder_control_dirs[0]


def test_a_dead_holders_operation_cannot_publish_after_a_second_acquires_the_mutex(
    monkeypatch, tmp_path
):
    tools = _BackupTool(die_before_publish=True)
    _install_backup_fakes(monkeypatch, tmp_path, tools)
    output = tmp_path / "point.tar"
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.backup_command(_backup_args(tmp_path, output))
    assert not output.exists()
    # The second operation now acquires the released mutex and completes.
    tools.die_before_publish = False
    assert maintenance.backup_command(_backup_args(tmp_path, output)) == maintenance.EX_OK
    assert output.is_file()
    # The first operation's failure cleanup ran through one recovery holder.
    assert tools.acquired == 3 and tools.released == 3


def test_a_failed_acknowledgement_never_removes_a_successors_archive(
    monkeypatch, tmp_path
):
    """R3/F5-B: the exact A/B sequence — A's cleanup runs after B replaced the archive.

    A publishes and reads back, then its holder dies before the acknowledgement. B then
    acquires the released mutex and publishes a replacement with overwrite=true through
    the real publication functions. A's failure cleanup must run under that same mutex
    and leave B's completed archive exactly where it is.
    """
    output = tmp_path / "point.tar"
    failed = _BackupTool()
    successor = _BackupTool(lock=failed.lock)
    successor.dump_bytes = b"the successor recovery point, byte for byte"
    current = {"tools": failed}
    _install_target_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: current["tools"])
    outcomes = []
    completed = []

    def lose_holder_then_let_a_successor_publish(container, *, control_dir, timeout=60.0):
        failed.confirmations.append((container, failed.lock.locked(), failed.released))
        # A published and read back successfully; its lock holder now dies before it
        # can acknowledge, releasing the mutex.
        failed._terminate(container)
        current["tools"] = successor
        try:
            outcomes.append(
                maintenance.backup_command(_backup_args(tmp_path, output, overwrite=True))
            )
            completed.append(output.read_bytes())
        finally:
            current["tools"] = failed
        raise maintenance.MaintenanceError(
            "backup_lock", "the lock holder terminated before success was acknowledged"
        )

    failed.confirm_lock_holder = lose_holder_then_let_a_successor_publish
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.backup_command(_backup_args(tmp_path, output))

    # 2: A reports failure, never success.
    assert refused.value.code == "backup_lock"
    # 1: B's archive is present and byte-identical.
    assert outcomes == [maintenance.EX_OK]
    assert completed and output.read_bytes() == completed[0]
    assert backup.read_archive(output).dump == successor.dump_bytes
    # 3/4: A's cleanup ran with the mutex held and did not remove B's output. The dead
    # original holder could not clean up, so one recovery holder re-acquired the mutex.
    assert len(failed.cleanups) == 1
    _, held, released, outcome = failed.cleanups[0]
    assert held is True and released == 1 and outcome == "retained"
    assert backup.read_archive(output).dump == successor.dump_bytes


def test_a_failed_publication_never_removes_a_successors_archive(monkeypatch, tmp_path):
    """R3/F5-B: the same rule on the second failure-cleanup path.

    A's holder links the archive and dies before reporting it. B then publishes a
    replacement under the recovered mutex. A's unreported-output cleanup must find a
    different inode and leave B's completed archive untouched.
    """
    output = tmp_path / "point.tar"
    failed = _BackupTool(die_during_publish=True)
    successor = _BackupTool(lock=failed.lock)
    successor.dump_bytes = b"the successor archive for the failed-publication branch"
    current = {"tools": failed}
    _install_target_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(maintenance, "ToolContainers", lambda image: current["tools"])
    outcomes = []

    completed = []

    def let_a_successor_publish():
        failed.before_failure = None
        current["tools"] = successor
        try:
            outcomes.append(
                maintenance.backup_command(_backup_args(tmp_path, output, overwrite=True))
            )
            completed.append(output.read_bytes())
        finally:
            current["tools"] = failed

    failed.before_failure = let_a_successor_publish
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.backup_command(_backup_args(tmp_path, output))

    assert refused.value.code == "backup_lock"
    assert outcomes == [maintenance.EX_OK]
    assert completed and output.read_bytes() == completed[0]
    assert backup.read_archive(output).dump == successor.dump_bytes
    assert len(failed.cleanups) == 1
    _, held, released, outcome = failed.cleanups[0]
    assert held is True and released == 1 and outcome == "retained"


def test_a_live_holder_cleanup_removes_only_the_published_inode(monkeypatch, tmp_path):
    """R3/F5-B: even with the holder alive, a replaced inode is never removed."""
    tools = _BackupTool()
    _install_backup_fakes(monkeypatch, tmp_path, tools)
    output = tmp_path / "point.tar"
    replacement = b"an archive that is not the one this operation published"
    real_read = backup.read_archive

    def replace_then_mismatch(path):
        point = real_read(path)
        if Path(path).name == output.name:
            # The publication is over, but the final path now names another file.
            other = path.parent / "other.tar"
            other.write_bytes(replacement)
            os.replace(other, path)
            return backup.RecoveryPoint(point.manifest, point.dump + b"x", point.checksum)
        return point

    monkeypatch.setattr(backup, "read_archive", replace_then_mismatch)
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.backup_command(_backup_args(tmp_path, output))
    assert refused.value.code == "output_verify"
    assert output.read_bytes() == replacement
    assert tools.cleanups and tools.cleanups[0][1] is True
    assert tools.cleanups[0][3] == "retained"


def test_normal_backup_holds_the_mutex_through_publication_and_readback(
    monkeypatch, tmp_path
):
    tools = _BackupTool()
    _install_backup_fakes(monkeypatch, tmp_path, tools)
    output = tmp_path / "point.tar"
    reads = []
    real_read = backup.read_archive

    def recording_read(path):
        if Path(path).name == output.name:
            reads.append((tools.lock.locked(), tools.released))
        return real_read(path)

    monkeypatch.setattr(backup, "read_archive", recording_read)
    assert maintenance.backup_command(_backup_args(tmp_path, output)) == maintenance.EX_OK
    assert tools.publications and all(
        held and released == 0 for _, held, released in tools.publications
    )
    # The read-back of the published path happens while the mutex is still held and the
    # holder is still alive; it is released only in the final cleanup.
    assert reads and all(held and released == 0 for held, released in reads)
    assert tools.confirmations and all(
        held and released == 0 for _, held, released in tools.confirmations
    )
    assert tools.released == 1


# --- R4/F5-B: recovery must never replay a stale publication --------------------------------
#
# These tests run the production holder scripts under a real POSIX shell with the real
# `flock` binary — locally on a POSIX host, or through WSL on Windows. The host-side
# request/result files use the production protocol helpers, so nothing here is an
# in-memory stand-in for the mutex or the holder loop.


def _linux_shell_command() -> list[str]:
    """The real Linux shell used for the reproduction: local `sh`, or WSL on Windows."""
    if os.name == "posix":
        return ["sh", "-c"]
    executable = shutil.which("wsl.exe")
    if executable is None:
        pytest.skip("a real Linux shell with flock (WSL) is unavailable")
    return [executable, "-e", "sh", "-c"]


def _linux_run(script, *, timeout=60.0):
    return subprocess.run(
        _linux_shell_command() + [script],
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _linux_path(path) -> str:
    value = Path(path).resolve()
    if os.name == "posix":
        return str(value)
    drive = value.drive.rstrip(":")
    if len(drive) != 1:
        pytest.skip("the Linux reproduction needs a local drive path visible to WSL")
    return "/mnt/" + drive.lower() + value.as_posix()[2:]


def _linux_identity(path) -> tuple[int, int]:
    """The identity as the holder itself sees it (`stat -c '%d %i'`)."""
    done = _linux_run(f'stat -c "%d %i" "{_linux_path(path)}"')
    if done.returncode != 0:
        raise AssertionError(done.stderr.decode("utf-8", "replace"))
    device, inode = done.stdout.split()
    return int(device), int(inode)


class _LocalLinuxHolders:
    """The ToolContainers boundary backed by a real POSIX shell and real `flock`.

    Instead of Docker, the production holder script text runs under the local Linux
    shell. Requests and results travel through the same production control files, and
    the mutex is a real kernel `flock`, so a stale request is replayed (or not) exactly
    as it would be in the deployed holder container.
    """

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.processes = {}
        self.publish_holder_dirs = []
        self.cleanup_holder_dirs = []
        self.cleanup_requests_at_start = []
        self.mutex_held_while_cleanup = []
        self._count = 0
        probe = _linux_run("command -v flock")
        if probe.returncode != 0:
            pytest.skip("real Linux flock is unavailable")

    def _paths(self, control_dir, output_dir):
        for value in (self.state_dir, Path(control_dir), Path(output_dir)):
            if re.search(r"[^A-Za-z0-9._/\\:-]", str(value)):
                pytest.skip("the Linux reproduction needs shell-safe paths")
        return {
            "control_dir": _linux_path(control_dir),
            "output_dir": _linux_path(output_dir),
            "state_lock": _linux_path(self.state_dir / "maintenance.lock"),
            "backup_mutex": _linux_path(self.state_dir / "backup.lock"),
        }

    def start_lock_holder(self, *, name, control_dir, output_dir, timeout=30.0):
        script = maintenance._lock_holder_script(**self._paths(control_dir, output_dir))
        container = self._start(script, Path(control_dir), timeout)
        self.publish_holder_dirs.append(Path(control_dir))
        return container

    def start_cleanup_holder(self, *, name, control_dir, output_dir, timeout=30.0):
        script = maintenance._cleanup_holder_script(
            **self._paths(control_dir, output_dir)
        )
        control = Path(control_dir)
        self.cleanup_requests_at_start.append(
            (control / maintenance.LOCK_REQUEST_NAME).exists()
        )
        container = self._start(script, control, timeout)
        self.cleanup_holder_dirs.append(control)
        self.mutex_held_while_cleanup.append(self._mutex_is_held())
        return container

    def _mutex_is_held(self) -> bool:
        """A competing exclusive `flock` must fail while the holder runs."""
        done = _linux_run(
            f'flock -x -n 8 8>"{_linux_path(self.state_dir / "backup.lock")}"'
            " && echo free || echo held"
        )
        return done.stdout.strip() == b"held"

    def _start(self, script, control_dir, timeout):
        process = subprocess.Popen(
            _linux_shell_command() + [script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ready = control_dir / maintenance.LOCK_READY_NAME
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready.exists():
                self._count += 1
                container = f"linux-holder-{self._count}"
                self.processes[container] = process
                return container
            if process.poll() is not None:
                break
            time.sleep(0.02)
        process.kill()
        process.wait(timeout=10)
        raise AssertionError("the real holder did not signal readiness")

    def publish_in_holder(
        self,
        container,
        *,
        control_dir,
        staging_name,
        output_name,
        replace=False,
        timeout=120.0,
    ):
        control = Path(control_dir)
        maintenance._write_control_request(
            control,
            maintenance.LOCK_REQUEST_NAME,
            maintenance._publish_request_body(staging_name, output_name, replace),
        )
        return self._wait_result(
            container, control / maintenance.LOCK_RESULT_NAME, timeout
        )

    def cleanup_in_holder(
        self, container, *, control_dir, output_name, identity, timeout=60.0
    ):
        control = Path(control_dir)
        device, inode = int(identity[0]), int(identity[1])
        maintenance._write_control_request(
            control,
            maintenance.LOCK_CLEANUP_NAME,
            maintenance._cleanup_request_body(output_name, device, inode),
        )
        return self._wait_result(
            container, control / maintenance.LOCK_CLEANUP_RESULT_NAME, timeout
        )

    def _wait_result(self, container, result, timeout):
        process = self.processes[container]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result.exists():
                outcome = result.read_text(encoding="utf-8").strip()
                try:
                    result.unlink()
                except FileNotFoundError:
                    pass
                return outcome
            if process.poll() is not None:
                raise maintenance.MaintenanceError(
                    "backup_lock", "the real holder terminated"
                )
            time.sleep(0.02)
        raise maintenance.MaintenanceError("backup_lock", "the real holder timed out")

    def remove(self, container):
        process = self.processes.pop(container, None)
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _scenario_dirs(tmp_path):
    output = tmp_path / "out"
    state = tmp_path / "state"
    control_a = tmp_path / "ctl-a"
    for directory in (output, state, control_a):
        directory.mkdir()
    return output, state, control_a


def _write_stale_overwrite_request(control_a):
    """A's publication request, written before A's holder dies and never consumed."""
    maintenance._write_control_request(
        control_a,
        maintenance.LOCK_REQUEST_NAME,
        maintenance._publish_request_body("A_staging.tar", "point.tar", True),
    )


def test_the_recovery_holder_script_has_no_publication_path():
    """Structural proof: the recovery script cannot publish, even a stale message."""
    script = maintenance._cleanup_holder_script()
    assert maintenance.LOCK_CLEANUP_NAME in script
    assert maintenance.LOCK_CLEANUP_RESULT_NAME in script
    assert maintenance.LOCK_READY_NAME in script
    assert maintenance.LOCK_DEADLINE_NAME in script
    # No publication request, no confirmation, and no archive link/rename at all.
    assert maintenance.LOCK_REQUEST_NAME not in script
    assert maintenance.LOCK_CONFIRM_NAME not in script
    assert 'ln "' not in script
    assert "replace" not in script
    # The same two locks as the publishing holder — not a second locking mechanism.
    assert maintenance.STATE_LOCK in script and maintenance.BACKUP_MUTEX in script
    assert "flock -s -n 9" in script and "flock -x -n 8" in script


def test_recovery_never_replays_a_stale_publication_and_keeps_the_successor(tmp_path):
    """The exact Codex sequence, with the real production recovery script and flock.

    A's holder staged an archive and died with an unconsumed overwrite request. B then
    acquired the mutex and published its own archive. A's recovery must leave both the
    stale request and B's completed archive untouched.
    """
    output, state, control_a = _scenario_dirs(tmp_path)
    successor = b"B's completed recovery point, byte for byte"
    (output / "point.tar").write_bytes(successor)
    staging = output / "A_staging.tar"
    a_bytes = b"A's archive, which must never be published by recovery"
    staging.write_bytes(a_bytes)
    identity = _linux_identity(staging)
    _write_stale_overwrite_request(control_a)

    tools = _LocalLinuxHolders(state)
    lock = maintenance._BackupLock(
        tools, name="recover", control_dir=control_a, output_dir=output
    )
    # 1/2/4/5: recovery does not execute A's publication, reports "not ours" and
    # deletes nothing; A's own inode is not the successor's.
    assert lock._recover_cleanup("point.tar", identity) is False

    assert (output / "point.tar").read_bytes() == successor
    assert staging.read_bytes() == a_bytes
    assert (control_a / maintenance.LOCK_REQUEST_NAME).exists()
    # The recovery holder was the cleanup-only script, mounted on a fresh private
    # directory that never contained a publication request, holding the real mutex.
    assert tools.cleanup_holder_dirs
    assert all(directory != control_a for directory in tools.cleanup_holder_dirs)
    assert tools.cleanup_requests_at_start == [False]
    assert tools.mutex_held_while_cleanup == [True]


def test_recovery_still_removes_its_own_unreported_inode_under_the_mutex(tmp_path):
    """6: normal recovery still removes exactly the failed operation's own archive."""
    output, state, control_a = _scenario_dirs(tmp_path)
    staging = output / "A_staging.tar"
    a_bytes = b"A's unreported archive inode"
    staging.write_bytes(a_bytes)
    # The holder had linked the staged inode before it died and never reported it.
    os.link(staging, output / "point.tar")
    identity = _linux_identity(staging)

    tools = _LocalLinuxHolders(state)
    lock = maintenance._BackupLock(
        tools, name="recover", control_dir=control_a, output_dir=output
    )
    assert lock._recover_cleanup("point.tar", identity) is True
    assert not (output / "point.tar").exists()
    assert staging.exists() and staging.read_bytes() == a_bytes
    assert tools.mutex_held_while_cleanup == [True]


def test_the_removed_recovery_strategy_replays_the_stale_publication(tmp_path):
    """The defect, reproduced against the exact call the previous code made.

    The previous `_recover_cleanup` started the *publishing* holder on the failed
    operation's control directory. That holder consumes the stale request, replaces
    B's completed archive with A's staging, and the following identity-checked cleanup
    then deletes it. The current recovery path never makes this call.
    """
    output, state, control_a = _scenario_dirs(tmp_path)
    successor = b"B's completed recovery point, byte for byte"
    (output / "point.tar").write_bytes(successor)
    staging = output / "A_staging.tar"
    a_bytes = b"A's archive that must never be published"
    staging.write_bytes(a_bytes)
    identity = _linux_identity(staging)
    _write_stale_overwrite_request(control_a)

    tools = _LocalLinuxHolders(state)
    holder = tools.start_lock_holder(
        name="previous-recovery", control_dir=control_a, output_dir=output
    )
    try:
        replayed = tools._wait_result(
            holder, control_a / maintenance.LOCK_RESULT_NAME, timeout=60
        )
        # The stale overwrite was executed by the publishing holder.
        assert replayed.startswith("ok ")
        assert (output / "point.tar").read_bytes() == a_bytes
        # And the old recovery cleanup then removed B's archive.
        assert (
            tools.cleanup_in_holder(
                holder,
                control_dir=control_a,
                output_name="point.tar",
                identity=identity,
            )
            == "removed"
        )
        assert not (output / "point.tar").exists()
    finally:
        tools.remove(holder)


def test_a_holder_that_terminates_before_the_success_ack_reports_no_archive(
    monkeypatch, tmp_path
):
    tools = _BackupTool(die_before_confirm=True)
    _install_backup_fakes(monkeypatch, tmp_path, tools)
    output = tmp_path / "point.tar"
    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.backup_command(_backup_args(tmp_path, output))
    assert refused.value.code == "backup_lock"
    # Publication and read-back had completed, but success could not be acknowledged by
    # the lock owner, so the archive is not left looking like a successful backup.
    assert not output.exists()
    assert list(tmp_path.glob(".point.tar.partial-*")) == []
    # The dead holder could not clean up; the recovery holder re-acquired the mutex,
    # matched the staged inode and removed exactly its own archive.
    assert tools.released == 2
    assert len(tools.cleanups) == 1
    assert tools.cleanups[0][1] is True and tools.cleanups[0][3] == "removed"
    # R4: the removing holder is the cleanup-only one, not a replaying publisher.
    assert tools.cleanup_holders
    assert tools.cleanup_control_dirs[0] != tools.holder_control_dirs[0]


def test_an_internal_failure_releases_the_mutex_without_exposing_an_archive(
    monkeypatch, tmp_path
):
    tools = _BackupTool()
    _install_backup_fakes(monkeypatch, tmp_path, tools)

    def refuse(*args, **kwargs):
        raise backup.BackupError("write", "refused")

    monkeypatch.setattr(backup, "write_archive", refuse)
    output = tmp_path / "point.tar"
    with pytest.raises(backup.BackupError):
        maintenance.backup_command(_backup_args(tmp_path, output))
    assert not output.exists()
    assert list(tmp_path.glob(".point.tar.partial-*")) == []
    assert tools.released == 1


# --- F6: the isolated attachment proof ------------------------------------------------------


class _ProofTools:
    """A stand-in for the proof container.

    It frames the record with the per-run token the host put in the container environment, exactly
    as the real program does (R5); a caller that needs arbitrary stdout passes it explicitly.
    """

    def __init__(self, record=None, *, returncode=0, stdout=None):
        self.record = record
        self.returncode = returncode
        self.stdout = stdout

    def run(self, program, **kwargs):
        stdout = self.stdout
        if stdout is None:
            nonce = next(
                entry.split("=", 1)[1]
                for entry in kwargs["environment"]
                if entry.startswith(maintenance._ATTACHMENT_PROOF_NONCE_ENV + "=")
            )
            stdout = (
                f"{maintenance._ATTACHMENT_PROOF_PREFIX} {nonce} "
                f"{json.dumps(self.record, sort_keys=True)}\n"
            ).encode()
        return subprocess.CompletedProcess(
            program, self.returncode, stdout=stdout, stderr=b"loader refused"
        )


def _proof(value):
    # R4-F3/R5: a framed, status-agreeing verdict is the program's only intentional outcome.
    return {"verdict": "ok", **value}


def _run_proof(tools):
    return maintenance.attachment_proof(
        tools,
        app_image="house-helper:local",
        database="house_helper_restore_x",
        role="hh_restore",
        passfile_value=None,
        network="none",
        host="127.0.0.1",
        isolated_container="a" * 64,
    )


def test_a_nonempty_attachment_proof_requires_the_loader_sample():
    value = {
        "count": 2,
        "bytes": 30,
        "declared_bytes": 30,
        "sample": True,
        "sample_id": "00000000-0000-0000-0000-000000000000",
        "sample_bytes": 10,
    }
    assert _run_proof(_ProofTools(_proof(value)))["sample"] is True


def test_an_empty_attachment_proof_is_explicit_about_the_empty_set():
    value = {"count": 0, "bytes": 0, "declared_bytes": 0, "sample": False}
    assert _run_proof(_ProofTools(_proof(value)))["sample"] is False
    assert "none" in maintenance._attachment_summary(value)


@pytest.mark.parametrize(
    "value",
    [
        # A nonempty set without a loader sample is a false claim.
        {"count": 1, "bytes": 10, "declared_bytes": 10, "sample": False},
        # The byte total is not the declared total.
        {
            "count": 1,
            "bytes": 10,
            "declared_bytes": 9,
            "sample": True,
            "sample_id": "x",
            "sample_bytes": 10,
        },
        # An empty set cannot carry bytes.
        {"count": 0, "bytes": 4, "declared_bytes": 4, "sample": False},
        # The sample evidence is malformed.
        {
            "count": 1,
            "bytes": 10,
            "declared_bytes": 10,
            "sample": True,
            "sample_id": "x",
            "sample_bytes": 0,
        },
    ],
)
def test_an_inconsistent_attachment_proof_record_is_environment_not_artifact(value):
    """R5: a record that contradicts itself is unreliable evidence, not an archive defect."""
    with pytest.raises(maintenance.MaintenanceError) as refused:
        _run_proof(_ProofTools(_proof(value)))
    assert refused.value.code == "attachment_proof_environment"


def test_a_failed_attachment_loader_proof_without_a_verdict_is_environment_not_artifact():
    """R4-F3: exit 1 alone is not proof of an artifact defect.

    An uncaught Python/SQLAlchemy/connection/import/environment exception inside the proof also
    exits 1 and prints no verdict. It must be infrastructure (exit 4), never `attachment_proof`,
    which is reserved for a refusal the program explicitly established."""
    with pytest.raises(maintenance.MaintenanceError) as refused:
        _run_proof(_ProofTools(returncode=1, stdout=b""))
    assert refused.value.code == "attachment_proof_environment"
    assert refused.value.message == "the attachment proof did not reach a verdict"


def test_a_deliberate_attachment_refusal_keeps_its_artifact_classification():
    record = {"verdict": "refused", "reason": "loader_mismatch"}
    with pytest.raises(maintenance.MaintenanceError) as refused:
        _run_proof(_ProofTools(record, returncode=3))
    assert refused.value.code == "attachment_proof"
    assert refused.value.detail == "loader_mismatch"
