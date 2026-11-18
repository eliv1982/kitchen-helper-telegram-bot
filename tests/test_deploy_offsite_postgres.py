"""WP-2E: the PostgreSQL format-v2 source of the off-host backup and the v1/v2 rehearsal dispatcher.

These are offline tests. There is no Docker and no PostgreSQL: the accepted WP-2D host tool
(`deploy/postgres/maintenance.py`) is stood in for by a fake runner that writes a real format-v2
archive (the frozen contract's own `write_archive`) and answers `verify-archive`. Everything from
`age` onward is the REAL v1 code path, with the existing fake age and fake rclone.

What is held:
  * `--database postgresql` is the only thing that selects the v2 path; without it the SQLite tool
    runs as before, and the PostgreSQL options are refused;
  * the archive is created and validated before `age` sees it, and a zero exit status alone is not
    taken as validation (a v1 tar written by a lying subprocess is refused);
  * the uploaded object is the encrypted archive and a committed recovery point, and no plaintext
    archive reaches the remote;
  * the rehearsal dispatches on the format inside the commit-bound archive, never on the name;
  * a v2 point is restored through the accepted WP-2D isolated restore, with no identity argument,
    and every failure is a non-zero status with the scratch directory removed.
"""

import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from house_helper.persistence import pg_backup as backup
from tests.deploy_support import DEPLOY, REPO_ROOT, posix_only
from tests.offsite_support import (
    FIXED_NAME,
    FakeSystem,
    Rig,
    fake_decrypt,
    fake_encrypt,
    load_rehearsal,
    offsite,
)
from tests.test_deploy_offsite_rehearsal import RehearsalSystem, World
from tests.test_pg_backup import _manifest

rehearsal = load_rehearsal()

DUMP = b"synthetic PostgreSQL custom-format dump for WP-2E (never a real pg_dump)\n"
CREATED = datetime(2026, 10, 25, 4, 15, 0, tzinfo=timezone.utc)


# --- building real format-v2 archives ---------------------------------------------------------


def v2_manifest(dump: bytes = DUMP, **overrides) -> dict:
    manifest = _manifest()
    manifest["snapshot"] = {"bytes": len(dump), "sha256": backup.sha256_bytes(dump)}
    manifest.update(overrides)
    return manifest


def write_v2_archive(path: Path, dump: bytes = DUMP, **overrides) -> bytes:
    """A real v2 archive: the frozen writer validates and reads it back."""
    backup.write_archive(path, manifest=v2_manifest(dump, **overrides), dump=dump)
    return path.read_bytes()


def craft_archive(path: Path, manifest: dict, dump: bytes = DUMP) -> bytes:
    """A tar of the right member names with an arbitrary (possibly invalid) manifest, for the
    dispatcher's negative cases. Only the frozen writer is allowed to make a *valid* archive."""
    raw = json.dumps(manifest, sort_keys=True).encode("utf-8")
    checksum = f"{hashlib.sha256(dump).hexdigest()}  snapshot.dump\n".encode("ascii")
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in (
            ("MANIFEST.json", raw),
            ("snapshot.dump.sha256", checksum),
            ("snapshot.dump", dump),
        ):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, int(CREATED.timestamp())
            archive.addfile(info, io.BytesIO(data))
    return path.read_bytes()


def v1_archive(path: Path) -> bytes:
    snapshot = path.with_name("snapshot.db")
    snapshot.write_bytes(b"not really SQLite; the v1 tar writer only hashes it")
    offsite.build_recovery_tar(path, snapshot, CREATED, "abc123def456")
    return path.read_bytes()


# --- the backup: the v2 source ------------------------------------------------------------------


class PostgresBackupFake(FakeSystem):
    """FakeSystem plus the accepted WP-2D host tool, recognized by its script operand. `backup`
    writes `archive_bytes`; `verify-archive` answers `verify_exit`. The two hooks are where a test
    changes the archive after (or while) the validators look at it (WP-2E F1)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.maintenance_calls = []
        self.archive_bytes = None
        self.backup_exit = 0
        self.verify_exit = 0
        self.backup_writes_v1 = False
        self.after_verify = None   # callable(argv) right after `verify-archive` returns 0
        self.before_age = None     # callable(stdin_path) inside the age call, before it reads
        self.after_age = None      # callable(stdin_path) inside the age call, after it writes

    def run(self, argv, *, timeout, stdin_path=None, stdin_handle=None, stdout_path=None, capture=False, env_extra=None):
        argv = [str(a) for a in argv]
        if any(a.endswith("maintenance.py") for a in argv):
            self.calls.append(argv)
            self.maintenance_calls.append(argv)
            if "backup" in argv:
                if self.backup_exit:
                    return offsite.Result(self.backup_exit, b"", b"refused [backup_refused]: injected\n")
                output = Path(argv[argv.index("--output") + 1])
                data = v1_archive(output) if self.backup_writes_v1 else write_v2_archive(output)
                if not self.backup_writes_v1:
                    self.archive_bytes = data
                return offsite.Result(0, b"ok [backup]: synthetic\n", b"")
            if "verify-archive" in argv:
                if self.verify_exit:
                    return offsite.Result(self.verify_exit, b"", b"refused [archive_contract]: injected\n")
                if self.after_verify is not None:
                    self.after_verify(argv)
                return offsite.Result(0, b"ok [verify-archive]: synthetic\n", b"")
            return offsite.Result(99, b"", b"unexpected maintenance command\n")
        return super().run(
            argv, timeout=timeout, stdin_path=stdin_path, stdin_handle=stdin_handle,
            stdout_path=stdout_path, capture=capture, env_extra=env_extra,
        )

    def _age(self, argv, stdin_path, stdout_path, env):
        if self.before_age is not None:
            self.before_age(stdin_path)
        result = super()._age(argv, stdin_path, stdout_path, env)
        if self.after_age is not None:
            self.after_age(stdin_path)
        return result


def postgres_rig(tmp_path: Path) -> tuple:
    rig = Rig(tmp_path)
    fake = PostgresBackupFake(rig.root, rig.database, remote_account=rig.fake.account, folder=rig.fake.folder_name)
    rig.fake = fake
    return rig, fake


def test_a_v2_run_builds_the_archive_with_the_wp2d_tool_validates_it_then_encrypts_and_commits_it(tmp_path):
    rig, fake = postgres_rig(tmp_path)
    assert rig.run("--database", "postgresql") == 0, rig.text

    # The accepted tool created the archive and validated it, both before age ran.
    assert len(fake.maintenance_calls) == 2
    created, checked = fake.maintenance_calls
    assert created[1] == str(DEPLOY / "postgres" / "maintenance.py")
    assert "backup" in created and "verify-archive" in checked
    produced = Path(created[created.index("--output") + 1])
    # F1: the tool's own pathname is never validated or encrypted. Its bytes are copied once into a
    # private, exclusively created snapshot, and both validators see that one snapshot.
    validated = Path(checked[checked.index("verify-archive") + 1])
    assert validated != produced and validated.name == "recovery-point.snapshot.tar"
    age_position = next(i for i, call in enumerate(fake.calls) if Path(call[0]).name == "age")
    assert fake.calls.index(created) < fake.calls.index(checked) < age_position
    assert "sanitize" not in fake.events and "backup" not in fake.events  # no SQLite rewrite, no backup.sh

    # The remote holds the ENCRYPTED archive, bound to a commit record, and nothing else.
    files = fake.remote_files()
    assert set(files) == {FIXED_NAME, FIXED_NAME + offsite.COMMIT_SUFFIX}
    sealed = files[FIXED_NAME]
    assert sealed.startswith(offsite.AGE_HEADER) and sealed != fake.archive_bytes
    assert fake_decrypt(sealed) == fake.archive_bytes
    assert b"PostgreSQL custom-format dump" not in sealed
    assert rig.committed_names() == {FIXED_NAME}
    with tarfile.open(fileobj=io.BytesIO(fake_decrypt(sealed))) as archive:
        assert archive.getnames() == ["MANIFEST.json", "snapshot.dump.sha256", "snapshot.dump"]


def test_without_the_flag_the_sqlite_path_runs_even_when_the_postgresql_tool_is_present(tmp_path):
    rig, fake = postgres_rig(tmp_path)
    assert rig.run() == 0, rig.text
    assert fake.maintenance_calls == []
    assert "sanitize" in fake.events and "backup" in fake.events  # the unchanged v1 source


def test_a_failed_v2_backup_uploads_nothing_and_reports_failure(tmp_path):
    rig, fake = postgres_rig(tmp_path)
    fake.backup_exit = 3
    assert rig.run("--database", "postgresql") == offsite.EXIT_FAILED
    assert fake.remote_files() == {}
    assert "backup failed (exit 3)" in rig.text


def test_an_archive_that_fails_verify_archive_is_never_encrypted_or_uploaded(tmp_path):
    rig, fake = postgres_rig(tmp_path)
    fake.verify_exit = 3
    assert rig.run("--database", "postgresql") == offsite.EXIT_FAILED
    assert fake.remote_files() == {}
    assert "did not pass verify-archive (exit 3)" in rig.text
    assert "age" not in fake.events


def test_a_zero_exit_status_is_not_validation_a_v1_tar_is_refused_before_age(tmp_path):
    # The subprocess says it wrote a backup and says verify-archive passed; the bytes are not v2.
    rig, fake = postgres_rig(tmp_path)
    fake.backup_writes_v1 = True
    assert rig.run("--database", "postgresql") == offsite.EXIT_FAILED
    assert fake.remote_files() == {}
    assert "not a valid format-v2 recovery point" in rig.text
    assert "age" not in fake.events


def test_a_missing_tag_is_still_a_usage_error_for_the_sqlite_path(tmp_path):
    rig, fake = postgres_rig(tmp_path)
    argv = [item for item in rig.args() if not item.startswith("--tag=")]  # omitted, not empty
    assert rig.run(argv=argv) == offsite.EXIT_USAGE
    assert fake.calls == []


def test_the_postgresql_options_are_refused_without_the_postgresql_database(tmp_path):
    rig, fake = postgres_rig(tmp_path)
    assert rig.run("--postgres-compose-file", str(tmp_path / "compose.yaml")) == offsite.EXIT_REFUSED
    assert fake.calls == []  # nothing ran, not even rclone


# --- F1: the bytes validated are the bytes encrypted ---------------------------------------------
#
# The Codex reproduction replaced the tool's archive after both validators returned; `age` reopened
# the pathname and encrypted bytes no validator had seen. The v2 source now copies the tool's file
# once into a private snapshot (hashed as it is copied), validates that snapshot, and gives `age` the
# snapshot's open descriptor. It is hashed again through the same descriptor before and after `age`.


def _mutate_in_place(stdin_path, *, at):
    path = Path(stdin_path.name)
    data = bytearray(path.read_bytes())
    data[at] ^= 0xFF
    path.write_bytes(data)  # the same file the descriptor was opened on


def test_a_snapshot_mutated_after_validation_is_never_encrypted_or_uploaded(tmp_path):
    rig, fake = postgres_rig(tmp_path)
    fake.before_age = lambda stdin_path: _mutate_in_place(stdin_path, at=0)
    assert rig.run("--database", "postgresql") == offsite.EXIT_FAILED
    assert fake.remote_files() == {}
    assert "changed while it was being encrypted" in rig.text
    assert "stored" not in rig.text


def test_an_unmodified_archive_still_succeeds(tmp_path):
    # The control for the two mutation tests: without a change, the same path passes and uploads.
    rig, fake = postgres_rig(tmp_path)
    assert rig.run("--database", "postgresql") == 0, rig.text
    assert fake.remote_files()[FIXED_NAME].startswith(offsite.AGE_HEADER)


def test_a_snapshot_mutated_while_age_runs_is_never_committed(tmp_path):
    # Here age encrypts the validated bytes; the file changes while it runs. Continuity cannot be
    # established, so no recovery point is published: a successful age exit is not validity.
    rig, fake = postgres_rig(tmp_path)
    fake.after_age = lambda stdin_path: _mutate_in_place(stdin_path, at=0)
    assert rig.run("--database", "postgresql") == offsite.EXIT_FAILED
    assert fake.remote_files() == {}
    assert "changed while it was being encrypted" in rig.text


@posix_only
def test_a_snapshot_path_replaced_after_validation_cannot_change_the_encrypted_bytes(tmp_path):
    # On POSIX the open descriptor keeps the validated inode alive while the name is replaced with
    # bytes no validator saw. `age` reads the descriptor, so the replacement is never encrypted.
    rig, fake = postgres_rig(tmp_path)

    def replace(stdin_path):
        replacement = tmp_path / "replacement.tar"
        replacement.write_bytes(b"bytes that no validator ever saw")
        os.replace(replacement, Path(stdin_path.name))

    fake.before_age = replace
    assert rig.run("--database", "postgresql") == 0, rig.text
    assert fake_decrypt(fake.remote_files()[FIXED_NAME]) == fake.archive_bytes
    assert b"no validator ever saw" not in fake_decrypt(fake.remote_files()[FIXED_NAME])


# The same two properties, executed through a real Linux kernel: on Windows a file that is open
# cannot be replaced by name, so the POSIX-only test above is skipped there. This probe runs the
# production `encrypt_validated` itself (locally on POSIX, or through WSL on Windows).
F1_POSIX_PROBE = r'''
import hashlib, importlib.util, os, sys
from pathlib import Path

repo, work = Path(sys.argv[1]), Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("offsite_backup", repo / "deploy" / "offsite_backup.py")
core = importlib.util.module_from_spec(spec)
sys.modules["offsite_backup"] = core
spec.loader.exec_module(core)

ORIGINAL = b"the bytes both validators read\n" * 64
REPLACEMENT = b"bytes that no validator ever saw\n" * 64


def job(runner, age="age"):
    config = core.Config(
        remote="fake:house-helper", rclone_config=work / "rclone.conf", tag="t",
        recipients_file=work / "recipients.txt", work_dir=work,
    )
    built = core.Job(config, runner, repo / "deploy", lambda: None, lambda: "x", lambda message: None)
    built.tools = {"age": age}
    return built


class ReplacingRunner:
    def run(self, argv, *, timeout, stdin_path=None, stdin_handle=None, stdout_path=None, capture=False, env_extra=None):
        assert stdin_path is None and stdin_handle is not None, "age must receive a descriptor, not a path"
        staging = work / "replacement.bin"
        staging.write_bytes(REPLACEMENT)
        os.replace(staging, Path(stdin_handle.name))     # a NEW inode under the same name
        data = stdin_handle.read()                       # exactly what age reads
        with core.open_exclusive(Path(stdout_path)) as out:
            out.write(core.AGE_HEADER + b"fake" + data)
        return core.Result(0)


class MutatingRunner(ReplacingRunner):
    def run(self, argv, **kwargs):
        Path(kwargs["stdin_handle"].name).write_bytes(b"mutated while age ran\n" * 64)   # same inode
        with core.open_exclusive(Path(kwargs["stdout_path"])) as out:
            out.write(core.AGE_HEADER + b"fake" + b"x" * 4096)
        return core.Result(0)


snapshot_path = work / "snapshot.tar"
snapshot_path.write_bytes(ORIGINAL)
snapshot = core.Snapshot(snapshot_path, len(ORIGINAL), hashlib.sha256(ORIGINAL).hexdigest())
sealed = job(ReplacingRunner()).encrypt_validated(snapshot, work / "point.age", step="probe")
data = Path(sealed).read_bytes()
assert data.startswith(core.AGE_HEADER)
assert data[len(core.AGE_HEADER) + 4:] == ORIGINAL, "the descriptor did not survive the replacement"
print("OK path-replacement")

snapshot_path.write_bytes(ORIGINAL)
snapshot = core.Snapshot(snapshot_path, len(ORIGINAL), hashlib.sha256(ORIGINAL).hexdigest())
try:
    job(MutatingRunner()).encrypt_validated(snapshot, work / "point2.age", step="probe")
except core.Failed as failure:
    assert "changed while it was being encrypted" in str(failure), str(failure)
    assert not (work / "point2.age").exists(), "the ciphertext of a changed snapshot must not remain"
    print("OK mutation-rejected")
else:
    raise SystemExit("a mutation during encryption was not detected")

# The production Runner, a real child process, a real descriptor: the same bytes come back.
age = work / "fake-age"
age.write_bytes(
    b'#!/usr/bin/env python3\nimport sys\n'
    b'data = sys.stdin.buffer.read()\n'
    b'sys.stdout.buffer.write(b"age-encryption.org/v1\\n" + b"fake" + data)\n'
)
age.chmod(0o755)
snapshot_path.write_bytes(ORIGINAL)
snapshot = core.Snapshot(snapshot_path, len(ORIGINAL), hashlib.sha256(ORIGINAL).hexdigest())
sealed = job(core.Runner(), age=str(age)).encrypt_validated(snapshot, work / "point3.age", step="probe")
data = Path(sealed).read_bytes()
assert data[len(core.AGE_HEADER) + 4:] == ORIGINAL, "the real Runner did not deliver the snapshot bytes"
print("OK real-runner-descriptor")
'''


def _linux_shell():
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


def test_the_posix_probe_confirms_the_descriptor_survives_a_name_replacement(tmp_path):
    probe = tmp_path / "f1-probe"
    probe.mkdir()
    script = probe / "probe.py"
    script.write_bytes(F1_POSIX_PROBE.encode("utf-8"))
    done = subprocess.run(
        _linux_shell() + [f'python3 "{_linux_path(script)}" "{_linux_path(REPO_ROOT)}" "{_linux_path(probe)}"'],
        capture_output=True, timeout=120, check=False,
    )
    output = done.stdout.decode("utf-8", "replace")
    assert "OK path-replacement" in output, output + done.stderr.decode("utf-8", "replace")
    assert "OK mutation-rejected" in output, output + done.stderr.decode("utf-8", "replace")
    assert "OK real-runner-descriptor" in output, output + done.stderr.decode("utf-8", "replace")



# --- the rehearsal: dispatch on the archive's own format ----------------------------------------


class PostgresRehearsalSystem(RehearsalSystem):
    """RehearsalSystem plus the accepted WP-2D `rehearse` command; it answers `restore_exit` and
    never runs anything itself. It also answers the `cleanup-rehearsal` fallback that the rehearsal
    issues after a signal, a timeout or a kill, and records the run id both commands were given.

    Since R3-F3 the rehearsal's verdict comes from the tool's machine-readable status record, so the
    fake writes one exactly as `deploy/postgres/maintenance.py` does: `restore_status` /
    `restore_cleanup` / `restore_code` (and the clean-up command's own trio) are what the parent
    reads. `restore_stderr` and `cleanup_stderr` remain pure diagnostics - the tests deliberately
    fill them with misleading text that must change no classification. `restore_record` /
    `cleanup_record` replace the generated record verbatim, for malformed-record cases."""

    STATUS_CONTRACT = "house-helper-maintenance-status/1"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.maintenance_calls = []
        self.cleanup_calls = []
        self.restore_archive_bytes = None
        self.restore_run_id = None
        self.restore_action = None      # None, "signal" (raise Interrupted), "timeout" (raise Failed)
        self.restore_exit = 0
        self.restore_stdout = (
            b"ok [rehearse]: isolated --network none restore verified; 18 tables, "
            b"revision 0014_user_measure_overrides; provenance system_identifier 7709166311476633635\n"
            b"     attachments: 0 attachment(s), 0 bytes\n"
        )
        self.restore_stderr = b""
        self.restore_status = "passed"
        self.restore_cleanup = "confirmed"
        self.restore_code = None
        self.restore_record = None      # verbatim bytes/text instead of the generated record
        self.cleanup_exit = 0
        self.cleanup_stderr = b""
        self.cleanup_record_status = "passed"
        self.cleanup_record_cleanup = "confirmed"
        self.cleanup_code = None
        self.cleanup_record = None     # verbatim bytes/text instead of the generated record

    @classmethod
    def _record(cls, command, run_id, status, cleanup, code):
        return {
            "contract": cls.STATUS_CONTRACT,
            "command": command,
            "run_id": run_id,
            "status": status,
            "cleanup": cleanup,
            "code": code,
        }

    @classmethod
    def _write_status(cls, argv, record, run_id=None) -> None:
        if record is None or "--status-file" not in argv:
            return
        if callable(record):
            record = record(run_id)
        target = Path(argv[argv.index("--status-file") + 1])
        if isinstance(record, dict):
            record = json.dumps(record, sort_keys=True) + "\n"
        if isinstance(record, str):
            record = record.encode("utf-8")
        target.write_bytes(record)

    def run(self, argv, *, timeout, stdin_path=None, stdin_handle=None, stdout_path=None, capture=False, env_extra=None):
        argv = [str(a) for a in argv]
        if any(a.endswith("maintenance.py") for a in argv):
            self.calls.append(argv)
            self.maintenance_calls.append(argv)
            if "cleanup-rehearsal" in argv:
                self.cleanup_calls.append(argv)
                run_id = argv[argv.index("--run-id") + 1]
                if self.cleanup_record is not None:
                    record = self.cleanup_record
                elif self.cleanup_exit == 0:
                    record = self._record(
                        "cleanup-rehearsal", run_id, "passed", "confirmed", None
                    )
                else:
                    record = self._record(
                        "cleanup-rehearsal", run_id, "failed", "unconfirmed",
                        self.cleanup_code or "rehearsal_cleanup",
                    )
                self._write_status(argv, record, run_id)
                return offsite.Result(self.cleanup_exit, b"", self.cleanup_stderr)
            if "rehearse" in argv:
                self.restore_run_id = argv[argv.index("--run-id") + 1]
                archive = next(item for item in argv[argv.index("rehearse") + 1:] if item.endswith(".tar"))
                # The archive lives in the scratch directory that the rehearsal removes on exit, so
                # take the bytes while the accepted tool would have had them.
                self.restore_archive_bytes = Path(archive).read_bytes()
                if self.restore_action == "signal":
                    raise offsite.Interrupted(signal.SIGTERM)
                if self.restore_action == "timeout":
                    raise offsite.Failed("maintenance.py did not finish within 3600 s and was stopped.")
                record = self.restore_record if self.restore_record is not None else self._record(
                    "rehearse", self.restore_run_id, self.restore_status, self.restore_cleanup, self.restore_code
                )
                self._write_status(argv, record, self.restore_run_id)
                return offsite.Result(self.restore_exit, self.restore_stdout, self.restore_stderr)
            return offsite.Result(99, b"", b"unexpected maintenance command\n")
        return super().run(
            argv, timeout=timeout, stdin_path=stdin_path, stdin_handle=stdin_handle,
            stdout_path=stdout_path, capture=capture, env_extra=env_extra,
        )


def v2_world(tmp_path: Path, *, manifest: dict = None, craft: bool = False, dump: bytes = DUMP) -> tuple:
    world = World(tmp_path, make_point=False)
    source = tmp_path / "point.tar"
    data = craft_archive(source, manifest, dump) if craft else write_v2_archive(source, dump)
    world.rig.fake.put_point(FIXED_NAME, fake_encrypt(data))
    world.fake = PostgresRehearsalSystem(
        tmp_path, world.rig.database, remote_account=world.rig.fake.account, folder=world.rig.fake.folder_name
    )
    return world, data


def test_a_v2_point_is_restored_through_the_wp2d_tool_and_passes(tmp_path):
    world, data = v2_world(tmp_path)
    before = world.rig.fake.remote_files()
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_OK, world.text
    assert "rehearsal PASSED" in world.text and "PostgreSQL 16" in world.text

    # The accepted tool got the decrypted archive; the private identity reached no argument of it.
    assert len(world.fake.maintenance_calls) == 1
    argv = world.fake.maintenance_calls[0]
    assert argv[1] == str(DEPLOY / "postgres" / "maintenance.py")
    assert "rehearse" in argv
    assert world.fake.restore_archive_bytes == data
    assert str(world.identity) not in " ".join(argv) and "/keys/" not in " ".join(argv)
    # The evidence of the isolated restore is reported.
    assert "system_identifier" in world.text
    # Nothing on the remote changed, and the scratch area is gone.
    assert world.rig.fake.remote_files() == before
    assert world.scratch_is_empty()


def test_a_v1_point_never_reaches_the_postgresql_path(tmp_path):
    world = World(tmp_path)  # a real v1 point, made by the real backup tool
    assert world.rehearse() == rehearsal.EXIT_OK, world.text
    assert all("maintenance.py" not in " ".join(call) for call in world.fake.calls)


def test_a_v2_restore_that_does_not_verify_is_not_restorable_and_leaves_no_scratch(tmp_path):
    world, _ = v2_world(tmp_path)
    world.fake.restore_exit = 3
    world.fake.restore_status = "failed"
    world.fake.restore_code = "isolated_verification"
    world.fake.restore_stderr = b"refused [isolated_verification]: injected\n"
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert "NOT RESTORABLE" in world.text and "did not verify the point" in world.text
    assert "PASSED" not in world.text
    assert world.scratch_is_empty()


def test_an_unknown_format_inside_a_committed_point_is_refused(tmp_path):
    world, _ = v2_world(tmp_path, manifest={"format": "house-helper-recovery-point/9"}, craft=True)
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert "does not declare a format this tool knows" in world.text
    assert world.fake.maintenance_calls == []


def test_a_v2_manifest_with_a_contradictory_database_is_refused(tmp_path):
    world, _ = v2_world(tmp_path, manifest=v2_manifest(database="sqlite"), craft=True)
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert "does not declare a format this tool knows" in world.text


def test_a_v2_manifest_outside_the_frozen_contract_is_refused(tmp_path):
    manifest = v2_manifest()
    manifest["extra"] = True
    world, _ = v2_world(tmp_path, manifest=manifest, craft=True)
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert "fails the frozen format-v2 contract" in world.text
    assert world.fake.maintenance_calls == []


def test_the_expected_tables_are_checked_against_the_manifest_not_against_the_name(tmp_path):
    world, _ = v2_world(tmp_path)
    # `messages` records 3 rows in the fixture manifest; 4 is not met, and an unknown table is refused.
    assert world.rehearse(expect=("messages=4",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert "does not record that many" in world.text
    assert world.rehearse(expect=("no_such_table=1",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert world.fake.maintenance_calls == []


def test_a_v2_point_whose_downloaded_bytes_differ_from_its_record_is_not_restorable(tmp_path):
    world, _ = v2_world(tmp_path)
    world.fake.fail("download", "corrupt")
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert "not the one its commit record describes" in world.text
    assert world.fake.maintenance_calls == [] and world.scratch_is_empty()


def test_a_v2_point_that_does_not_decrypt_with_this_identity_is_not_restorable(tmp_path):
    world, _ = v2_world(tmp_path)
    other = world.write_identity("AGE-SECRET-KEY-1" + "Z" * 58, "other.txt")
    assert world.rehearse(identity=other, expect=("messages=1",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert "could not decrypt" in world.text
    assert world.fake.maintenance_calls == []


def test_an_object_without_its_commit_record_is_never_rehearsed(tmp_path):
    world, data = v2_world(tmp_path)
    name = "house-helper-20261025T050000Z-11111111.tar.age"
    world.rig.fake.put_remote(name, fake_encrypt(data))
    assert world.rehearse("--point", name, expect=("messages=1",)) == rehearsal.EXIT_REFUSED
    assert "UNRESOLVED" in world.text and "not a recovery point, so not rehearsed" in world.text


def test_a_missing_restore_tool_is_a_failure_not_a_pass(tmp_path):
    world, _ = v2_world(tmp_path)
    status = world.rehearse(
        "--postgres-maintenance-script", str(tmp_path / "absent.py"), expect=("messages=1",)
    )
    assert status == rehearsal.EXIT_FAILED
    assert "cannot be restored and verified" in world.text
    assert world.scratch_is_empty()


# --- F2: a cancelled v2 rehearsal removes its one container, or says it could not -----------------
#
# Killing the helper does not remove a detached container and the helper's own `finally` is bypassed
# by the default SIGTERM disposition. The rehearsal now gives the helper a run id, the helper turns
# SIGTERM/SIGINT into an exception (so its `finally` runs), and the rehearsal also asks the accepted
# tool's `cleanup-rehearsal` to remove exactly that run's container and confirm it is gone.


def test_a_v2_rehearsal_passes_the_run_id_and_calls_no_cleanup_when_it_completes(tmp_path):
    world, _ = v2_world(tmp_path)
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_OK, world.text
    argv = world.fake.maintenance_calls[0]
    run_id = argv[argv.index("--run-id") + 1]
    assert len(run_id) == 32 and all(ch in "0123456789abcdef" for ch in run_id)
    assert world.fake.cleanup_calls == []           # the helper's own verified removal is the normal path


def test_a_signal_during_the_v2_restore_removes_the_runs_container_and_leaves_no_scratch(tmp_path):
    world, _ = v2_world(tmp_path)
    world.fake.restore_action = "signal"
    assert world.rehearse(expect=("messages=1",)) == 128 + signal.SIGTERM, world.text
    assert world.fake.restore_run_id is not None
    cleanup = world.fake.cleanup_calls[0]
    assert cleanup[cleanup.index("--run-id") + 1] == world.fake.restore_run_id
    assert "cleanup-rehearsal" in cleanup
    assert world.scratch_is_empty()
    assert "PASSED" not in world.text


def test_a_timeout_during_the_v2_restore_removes_the_runs_container_and_reports_failure(tmp_path):
    world, _ = v2_world(tmp_path)
    world.fake.restore_action = "timeout"
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    assert "did not finish within 3600 s" in world.text and "confirmed removed" in world.text
    cleanup = world.fake.cleanup_calls[0]
    assert cleanup[cleanup.index("--run-id") + 1] == world.fake.restore_run_id
    assert world.scratch_is_empty()


def test_forced_termination_without_a_confirmable_cleanup_is_a_failure_naming_the_container(tmp_path):
    world, _ = v2_world(tmp_path)
    world.fake.restore_action = "timeout"
    world.fake.cleanup_exit = 4
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    name = rehearsal.REHEARSAL_CONTAINER_PREFIX + world.fake.restore_run_id
    assert name in world.text and "could not be confirmed removed" in world.text
    assert "PASSED" not in world.text
    assert world.scratch_is_empty()


def test_a_signal_whose_cleanup_cannot_be_confirmed_is_a_failed_status_not_a_signal_exit(tmp_path):
    world, _ = v2_world(tmp_path)
    world.fake.restore_action = "signal"
    world.fake.cleanup_exit = 4
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    assert rehearsal.REHEARSAL_CONTAINER_PREFIX in world.text
    assert "PASSED" not in world.text


def test_the_cleanup_fallback_uses_the_accepted_tool_and_never_removes_anything_itself(tmp_path):
    world, _ = v2_world(tmp_path)
    world.fake.restore_action = "signal"
    assert world.rehearse(expect=("messages=1",)) == 128 + signal.SIGTERM
    cleanup = world.fake.cleanup_calls[0]
    assert any(part.endswith("maintenance.py") for part in cleanup)
    assert "--compose-file" in cleanup and "cleanup-rehearsal" in cleanup
    # The rehearsal itself ran no Docker command: the container is removed by the accepted tool.
    assert world.fake.docker_calls == []


# --- R2-F2: an unconfirmed clean-up takes precedence over the rehearsal verdict -------------------
#
# The accepted tool resolves its own verified removal BEFORE propagating a rehearsal failure and
# records both facts independently: `status` is the rehearsal outcome and `cleanup` the resource
# outcome. The parent must invoke its fallback whenever the record says the removal is unconfirmed;
# only after the fallback confirms the removal may the tool's own artifact refusal stand. If the
# fallback fails, the status is FAILED and the exact container name and recovery command are printed.
# Neither path may report NOT RESTORABLE while a plaintext container may remain.
#
# The misleading stderr below (an `underlying [...]` token, a `refused [...]` line) is deliberately
# kept: since R3-F3 it is diagnostics only and must change no classification.

CLEANUP_REFUSAL = (
    b"refused [rehearsal_cleanup]: the disposable container could not be confirmed removed "
    b"(house-helper-restore-verify-00000000000000000000000000000000; "
    b"remove it with: python3 maintenance.py cleanup-rehearsal --run-id 00000000000000000000000000000000)\n"
)
VERIFICATION_UNDERLYING = (
    b"underlying [isolated_verification]: the isolated restored database did not match the archive\n"
)
INFRASTRUCTURE_UNDERLYING = (
    b"underlying [docker_infrastructure]: the archive listing could not be carried out by Docker\n"
)


def _unconfirmed(world, *, status="failed", code=None):
    """The accepted tool's own removal could not be confirmed, with the rehearsal outcome under test."""
    world.fake.restore_exit = 4
    world.fake.restore_status = status
    world.fake.restore_code = code
    world.fake.restore_cleanup = "unconfirmed"


def test_an_unconfirmed_cleanup_is_retried_by_the_fallback_before_a_verdict(tmp_path):
    world, _ = v2_world(tmp_path)
    _unconfirmed(world, code="isolated_verification")
    world.fake.restore_stderr = CLEANUP_REFUSAL + VERIFICATION_UNDERLYING
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert "refused [isolated_verification]" in world.text
    assert "rehearsal PASSED" not in world.text
    # The parent asked the accepted tool's fallback to remove exactly this run's container.
    assert len(world.fake.cleanup_calls) == 1
    cleanup = world.fake.cleanup_calls[0]
    assert cleanup[cleanup.index("--run-id") + 1] == world.fake.restore_run_id
    assert world.scratch_is_empty()


def test_a_failed_fallback_keeps_the_unconfirmed_cleanup_as_a_failure_with_the_identity(tmp_path):
    world, _ = v2_world(tmp_path)
    _unconfirmed(world, code="isolated_verification")
    world.fake.restore_stderr = CLEANUP_REFUSAL + VERIFICATION_UNDERLYING
    world.fake.cleanup_exit = 4
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    name = rehearsal.REHEARSAL_CONTAINER_PREFIX + world.fake.restore_run_id
    assert name in world.text and "could not be confirmed removed" in world.text
    assert "cleanup-rehearsal" in world.text and world.fake.restore_run_id in world.text
    assert "Its own recorded outcome was [isolated_verification]" in world.text
    assert "NOT RESTORABLE" not in world.text
    assert "PASSED" not in world.text


def test_a_verified_restore_whose_own_cleanup_failed_passes_once_the_fallback_confirms(tmp_path):
    """R3 invariant: confirmed clean-up (the fallback's) + a proven successful restore -> PASSED.

    The tool verified the restore and reported `passed`, but its own removal was unconfirmed. The
    fallback removed exactly this run's container and confirmed it is gone, so both independent
    facts now hold. Before R3 no machine-readable "the restore verified" fact existed; the old
    refusal-only text could not establish it, so this was conservatively a failure."""
    world, _ = v2_world(tmp_path)
    _unconfirmed(world, status="passed", code=None)
    world.fake.restore_stderr = CLEANUP_REFUSAL
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_OK
    assert len(world.fake.cleanup_calls) == 1
    assert "rehearsal PASSED" in world.text
    assert world.scratch_is_empty()


def test_an_infrastructure_failure_is_not_restorable_even_after_the_fallback_cleans_up(tmp_path):
    world, _ = v2_world(tmp_path)
    _unconfirmed(world, code="docker_infrastructure")
    world.fake.restore_stderr = CLEANUP_REFUSAL + INFRASTRUCTURE_UNDERLYING
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    assert len(world.fake.cleanup_calls) == 1
    assert "docker_infrastructure" in world.text
    assert "NOT RESTORABLE" not in world.text


def test_an_infrastructure_failure_with_an_unconfirmed_cleanup_names_the_container(tmp_path):
    world, _ = v2_world(tmp_path)
    _unconfirmed(world, code="docker_infrastructure")
    world.fake.restore_stderr = CLEANUP_REFUSAL + INFRASTRUCTURE_UNDERLYING
    world.fake.cleanup_exit = 4
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED
    name = rehearsal.REHEARSAL_CONTAINER_PREFIX + world.fake.restore_run_id
    assert name in world.text and "could not be confirmed removed" in world.text
    assert "cleanup-rehearsal" in world.text
    assert "docker_infrastructure" in world.text
    assert "NOT RESTORABLE" not in world.text


# --- F3/R3-F3: the status record - never stderr and never a bare exit status - decides -----------
#
# Each row gives the tool's exit status, its machine-readable record (status, cleanup, code) and the
# misleading stderr it also prints. The stderr text is deliberately shaped like a refusal line in
# the rows that must be FAILED: a human-readable string can never establish an artifact verdict.


@pytest.mark.parametrize(
    "returncode, record, stderr, expected, words",
    [
        (4, ("failed", "confirmed", "docker_unavailable"),
         b"refused [isolated_verification]: misleading\n", rehearsal.EXIT_FAILED, "could not be run to a verdict"),
        (4, ("failed", "confirmed", "docker_infrastructure"),
         b"refused [archive_integrity]: misleading\n", rehearsal.EXIT_FAILED, "could not be run to a verdict"),
        (4, ("failed", "confirmed", "image_unavailable"),
         b"refused [sequence_extraction]: misleading\n", rehearsal.EXIT_FAILED, "could not be run to a verdict"),
        (1, ("failed", "confirmed", "internal"),
         b"refused [attachment_proof]: misleading\n", rehearsal.EXIT_FAILED, "could not be run to a verdict"),
        (3, ("failed", "confirmed", "container_start"),
         b"no structured line at all\n", rehearsal.EXIT_FAILED, "could not be run to a verdict"),
        (6, ("cancelled", "confirmed", None),
         b"cancelled [rehearsal_cleanup]: signal 15\n", rehearsal.EXIT_FAILED, "interrupted by a signal"),
        (4, ("failed", "confirmed", "rehearsal_cleanup"),
         b"refused [isolated_verification]: misleading\n", rehearsal.EXIT_FAILED, "could not be run to a verdict"),
        (3, ("failed", "confirmed", "isolated_verification"),
         b"refused [docker_infrastructure]: misleading\n", rehearsal.EXIT_NOT_RESTORABLE, "refused [isolated_verification]"),
        (3, ("failed", "confirmed", "archive_integrity"),
         b"plain text\n", rehearsal.EXIT_NOT_RESTORABLE, "refused [archive_integrity]"),
        (3, ("failed", "confirmed", "isolated_restore"),
         b"plain text\n", rehearsal.EXIT_NOT_RESTORABLE, "refused [isolated_restore]"),
    ],
    ids=[
        "docker-unavailable", "docker-infrastructure", "image-unavailable", "internal-error",
        "container-will-not-start", "cancelled", "cleanup-precedence", "isolated-verification",
        "archive-integrity", "isolated-restore",
    ],
)
def test_a_tool_failure_is_classified_by_its_status_record_not_by_its_stderr(
    tmp_path, returncode, record, stderr, expected, words
):
    world, _ = v2_world(tmp_path)
    world.fake.restore_exit = returncode
    world.fake.restore_status, world.fake.restore_cleanup, world.fake.restore_code = record
    world.fake.restore_stderr = stderr
    assert world.rehearse(expect=("messages=1",)) == expected, world.text
    assert words in world.text
    assert ("NOT RESTORABLE" in world.text) == (expected == rehearsal.EXIT_NOT_RESTORABLE)
    assert world.scratch_is_empty()
