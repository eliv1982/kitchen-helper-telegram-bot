"""The safety properties of the deployment kit's database tool (deploy/ops/dbtool.py), each
one a failure that an independent audit of the first version reproduced:

  * a restore cannot replace a database that something is using. The web service holds a
    lock for as long as uvicorn runs; a restore refuses while it does; a writer in the middle
    of a transaction, or any other open connection, is refused even if it never asked for
    the lock; and a restore that fails halfway puts the previous database back;
  * a database larger than the 64 MiB /tmp can be backed up, verified and restored: scratch
    copies go to a directory of their own on disk, space is checked first, and the directory is
    private, separate from the data and removed afterwards;
  * a database that merely advertises the right Alembic revision is not accepted: its tables,
    columns, keys and indexes must be those the migrations produce;
  * a backup is published whole or not at all, and never over an existing file.

The lock tests need POSIX flock (the container's); the rest also run on Windows.
"""

import errno
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.deploy_support import (
    BEHIND,
    CRASHED_WRITER,
    HEAD,
    IDLE_CONNECTION,
    LOCK,
    STAGING_ENV,
    UNCOMMITTED_WRITER,
    alter,
    child_environ,
    dbtool,
    env_for,
    listing,
    lock_is_taken,
    make_big_database,
    make_database,
    posix_only,
    run,
    run_tool,
    sha256,
    staging_files,
    start_python,
    start_web_service,
    tables,
)

MIB = 1 << 20


@pytest.fixture
def staging(tmp_path):
    directory = tmp_path / "staging"
    directory.mkdir(mode=0o700)
    return directory


@pytest.fixture
def live(tmp_path, staging):
    """A migrated database at an absolute path, and the environment that names it and the
    scratch directory."""
    data = tmp_path / "data"
    data.mkdir()
    path = make_database(data / "house_helper.db")
    return path, env_for(path, **{STAGING_ENV: str(staging)})


@pytest.fixture
def good_backup(live, tmp_path, capsys):
    path, env = live
    target = tmp_path / "good.db"
    assert run(capsys, ["backup", "--output", str(target)], env).code == 0
    return target


def restore(capsys, env, backup_path, *extra):
    return run(capsys, ["restore", "--input", str(backup_path), *extra], env)


# --- the lock -------------------------------------------------------------------------------


@posix_only
def test_shared_holders_coexist_and_an_exclusive_holder_excludes_everyone(live):
    path, _ = live
    first = dbtool.DataDirLock(path, exclusive=False, purpose="the web service")
    second = dbtool.DataDirLock(path, exclusive=False, purpose="an enrollment")
    first.acquire()
    second.acquire()  # shared with shared: the web service and an enrollment run together
    with pytest.raises(dbtool.ToolError) as refused:
        dbtool.DataDirLock(path, exclusive=True, purpose="a restore").acquire()
    assert refused.value.code == "maintenance_lock_busy" and refused.value.exit_code == 3
    first.release()
    second.release()

    exclusive = dbtool.DataDirLock(path, exclusive=True, purpose="a restore")
    exclusive.acquire()
    with pytest.raises(dbtool.ToolError) as refused:
        dbtool.DataDirLock(path, exclusive=False, purpose="the web service").acquire()
    assert refused.value.code == "maintenance_lock_busy"
    exclusive.release()
    assert not lock_is_taken(path)


@posix_only
def test_restore_is_refused_while_the_web_service_is_running_and_works_once_it_is_stopped(
    live, good_backup, capsys
):
    path, env = live
    before_hash, before_names = sha256(path), listing(path.parent)
    with start_web_service(path) as web:
        refused = restore(capsys, env, good_backup, "--replace")
        assert refused.code == 3 and "[maintenance_lock_busy]" in refused.err
        assert "docker compose stop house-helper" in refused.err
        assert sha256(path) == before_hash and listing(path.parent) == before_names
        assert run(capsys, ["check-idle"], env).code == 3
        web.process.send_signal(signal.SIGTERM)  # what `docker compose stop` sends
        web.process.wait(timeout=30)

    assert run(capsys, ["check-idle"], env).code == 0
    assert restore(capsys, env, good_backup, "--replace").code == 0
    assert sha256(path) == sha256(good_backup)


@posix_only
def test_the_lock_belongs_to_the_program_serve_becomes_and_not_to_the_start_up_check(live):
    path, _ = live
    with start_web_service(path) as web:
        ready = next(line for line in web.lines if line.startswith("READY"))
        assert int(ready.split()[1]) == web.process.pid  # exec: the same process, the program now
        assert any("[check_schema]" in line for line in web.lines)  # the check ran first
        assert lock_is_taken(path)
        assert web.process.poll() is None and lock_is_taken(path)  # still, long after the check
    assert not lock_is_taken(path)  # killed (SIGKILL in stop()): the kernel let go, nothing stale


@posix_only
def test_the_lock_is_released_when_the_web_service_is_stopped_normally(live):
    path, _ = live
    with start_web_service(path) as web:
        web.process.send_signal(signal.SIGTERM)
        web.process.wait(timeout=30)
        assert not lock_is_taken(path)


@posix_only
def test_a_backup_is_possible_while_the_web_service_runs(live, tmp_path, capsys):
    path, env = live
    with start_web_service(path):
        target = tmp_path / "online.db"
        result = run(capsys, ["backup", "--output", str(target)], env)
        assert result.code == 0, result.err
        assert dbtool.inspect_database(target).sound


@posix_only
def test_an_enrollment_style_command_runs_beside_the_web_service_but_a_migration_does_not(live):
    path, _ = live
    program = (sys.executable, "-c", "print('ran')")
    with start_web_service(path):
        beside = run_tool("exec", "--shared", "--", *program, database=path)
        assert beside.returncode == 0 and b"ran" in beside.stdout
        alone = run_tool("exec", "--exclusive", "--", *program, database=path)
        assert alone.returncode == 3 and b"[maintenance_lock_busy]" in alone.stderr
        assert b"ran" not in alone.stdout  # the program was never started


@posix_only
def test_exec_keeps_the_lock_while_the_program_runs_and_passes_its_exit_status_on(live):
    path, _ = live
    lock_file = path.with_name(LOCK)
    probe = (
        "import fcntl, sys\n"
        "handle = open(sys.argv[1], 'a')\n"
        "try:\n"
        "    fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)\n"
        "    print('shared lock free')\n"
        "except BlockingIOError:\n"
        "    print('held exclusively')\n"
        "sys.exit(7)\n"
    )
    done = run_tool("exec", "--exclusive", "--", sys.executable, "-c", probe, str(lock_file), database=path)
    assert done.returncode == 7
    assert done.stdout.strip() == b"held exclusively"  # the program itself holds it, not a parent
    assert not lock_is_taken(path)


@posix_only
def test_a_migration_cannot_start_while_a_restore_holds_the_directory(live):
    path, _ = live
    with dbtool.DataDirLock(path, exclusive=True, purpose="a restore"):
        done = run_tool("exec", "--exclusive", "--", sys.executable, "-c", "print('ran')", database=path)
    assert done.returncode == 3 and b"ran" not in done.stdout


@pytest.mark.parametrize("exclusive", [False, True])
def test_the_program_is_handed_the_lock_and_nothing_leaks_when_exec_does_not_replace_us(
    live, monkeypatch, capsys, exclusive
):
    path, env = live
    seen = {}
    real = dbtool.DataDirLock.keep_across_exec

    def spy(self):
        real(self)
        seen["inheritable"] = os.get_inheritable(self.descriptor)
        seen["exclusive"] = self.exclusive
        seen["descriptor"] = self.descriptor

    monkeypatch.setattr(dbtool.DataDirLock, "keep_across_exec", spy)
    monkeypatch.setattr(dbtool, "_exec", lambda command: seen.setdefault("command", list(command)))
    argv = ["exec", "--exclusive" if exclusive else "--shared", "--", "alembic", "upgrade", "head"]
    assert run(capsys, argv, env).code == 0
    assert seen["command"] == ["alembic", "upgrade", "head"]
    assert seen["inheritable"] is True and seen["exclusive"] is exclusive
    with pytest.raises(OSError):
        os.fstat(seen["descriptor"])  # closed again: a real exec never returns, a hook must not leak


def test_serve_checks_the_schema_and_starts_nothing_for_a_database_that_is_not_at_the_head(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "data").mkdir()
    behind = make_database(tmp_path / "data" / "behind.db", BEHIND)
    started = []
    monkeypatch.setattr(dbtool, "_exec", started.append)
    result = run(capsys, ["serve", "--", "uvicorn"], env_for(behind))
    assert result.code == 4 and "[schema_not_head]" in result.err
    assert started == []
    assert not lock_is_taken(behind)  # the refused start holds nothing


def test_serve_does_not_start_while_a_restore_holds_the_directory(live, monkeypatch, capsys):
    path, env = live
    started = []
    monkeypatch.setattr(dbtool, "_exec", started.append)
    with dbtool.DataDirLock(path, exclusive=True, purpose="a restore"):
        turned_away = run(capsys, ["serve", "--", "uvicorn"], env)
    assert turned_away.code == 3 and "[maintenance_lock_busy]" in turned_away.err
    assert started == []
    assert run(capsys, ["serve", "--", "uvicorn", "--port", "8000"], env).code == 0
    assert started == [["uvicorn", "--port", "8000"]]


def test_a_web_service_or_backup_that_starts_in_the_middle_of_a_restore_is_turned_away(
    live, good_backup, monkeypatch, capsys
):
    path, env = live
    started, during = [], []
    monkeypatch.setattr(dbtool, "_exec", started.append)
    real_install = dbtool._install

    def install_while_others_try(*args, **kwargs):
        during.append(run(capsys, ["serve", "--", "uvicorn"], env).code)  # the container restarting
        during.append(run(capsys, ["backup", "--output", "-"], env).code)
        during.append(restore(capsys, env, good_backup, "--replace").code)  # a second operator
        return real_install(*args, **kwargs)

    monkeypatch.setattr(dbtool, "_install", install_while_others_try)
    assert restore(capsys, env, good_backup, "--replace").code == 0
    assert during == [3, 3, 3] and started == []
    assert run(capsys, ["serve", "--", "uvicorn"], env).code == 0 and started == [["uvicorn"]]


@pytest.mark.parametrize(
    "argv", [["exec", "--shared"], ["exec", "--shared", "--"], ["exec", "--", "ls"], ["serve"], ["serve", "--"]]
)
def test_exec_and_serve_need_a_mode_and_a_program(capsys, argv):
    assert run(capsys, argv, {}).code == 2


def test_a_program_that_does_not_exist_is_a_refusal_and_releases_the_lock(live, capsys):
    path, env = live
    result = run(capsys, ["exec", "--exclusive", "--", "no-such-program-anywhere"], env)
    assert result.code == 3 and "[command_not_runnable]" in result.err
    assert not lock_is_taken(path)


@posix_only
def test_serve_as_a_script_runs_the_program_after_the_check(live):
    path, _ = live
    done = run_tool("serve", "--", sys.executable, "-c", "print('the program')", database=path)
    assert done.returncode == 0
    assert done.stdout.index(b"[check_schema]") < done.stdout.index(b"the program")


# --- other users of the database, whether or not they asked for the lock --------------------------


def test_restore_is_refused_while_another_process_is_in_the_middle_of_a_write(
    live, good_backup, capsys, monkeypatch
):
    monkeypatch.setattr(dbtool, "_PROBE_BUSY_SECONDS", 0.3)
    path, env = live
    with start_python(UNCOMMITTED_WRITER, path) as writer:
        names = listing(path.parent)
        refused = restore(capsys, env, good_backup, "--replace")
        assert refused.code == 3 and "[database_busy]" in refused.err
        assert listing(path.parent) == names  # nothing moved aside, no .part left
        assert run(capsys, ["check-idle"], env).code == 3
        # The writer was not disturbed: its transaction commits into the live database.
        writer.send()
        assert writer.read_line() == "COMMITTED"
    assert tables(path)["zz_inflight"] == 1


def test_restore_is_refused_while_another_process_has_the_database_open(live, good_backup, capsys):
    path, env = live
    with start_python(IDLE_CONNECTION, path):
        before = tables(path)
        refused = restore(capsys, env, good_backup, "--replace")
        assert refused.code == 3 and "[database_in_use]" in refused.err
        assert tables(path) == before
        assert not [p for p in path.parent.iterdir() if p.name.startswith(("pre-restore", ".restore"))]
    assert restore(capsys, env, good_backup, "--replace").code == 0  # the process is gone


def test_restore_keeps_what_only_a_crashed_writers_log_held(live, good_backup, capsys):
    path, env = live
    crashed = subprocess.run(
        [sys.executable, "-c", CRASHED_WRITER, str(path)], env=child_environ(path), timeout=60
    )
    assert crashed.returncode == 0
    assert Path(str(path) + "-wal").exists()  # the premise: committed work only in a log left behind

    result = restore(capsys, env, good_backup, "--replace")
    assert result.code == 0, result.err
    kept = next(p for p in path.parent.iterdir() if p.name.startswith("pre-restore-"))
    assert tables(kept / path.name)["zz_crash"] == 1  # recovered into the kept file, not lost
    assert not Path(str(path) + "-wal").exists() and not Path(str(path) + "-shm").exists()
    assert sha256(path) == sha256(good_backup)


def test_check_idle_on_a_directory_without_a_database_is_fine(tmp_path, staging, capsys):
    (tmp_path / "data").mkdir()
    result = run(capsys, ["check-idle"], env_for(tmp_path / "data" / "house_helper.db"))
    assert result.code == 0 and "[check_idle]" in result.out


# --- a restore that fails halfway ---------------------------------------------------------------


def fail_replace(monkeypatch, when):
    real = os.replace

    def replace(source, destination, *args, **kwargs):
        if when(Path(source), Path(destination)):
            raise OSError(errno.EIO, "injected failure")
        return real(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def test_a_failure_while_installing_puts_the_previous_database_back(
    live, good_backup, monkeypatch, capsys
):
    path, env = live
    before_hash, before_names = sha256(path), listing(path.parent)
    fail_replace(monkeypatch, lambda src, dst: dst == path and src.name.startswith(".restore-"))

    result = restore(capsys, env, good_backup, "--replace")
    assert result.code == 4 and "[restore_failed]" in result.err
    assert "previous database is back in place" in result.err
    assert sha256(path) == before_hash
    assert listing(path.parent) == before_names  # no pre-restore directory, no .part
    assert dbtool.inspect_database(path).sound

    monkeypatch.undo()
    assert restore(capsys, env, good_backup, "--replace").code == 0  # and a retry works


def test_a_failure_while_moving_the_old_files_aside_puts_back_those_already_moved(
    live, good_backup, monkeypatch, capsys
):
    path, env = live
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    path.write_bytes(b"corrupted beyond repair" * 100)  # restoring over a damaged database is the point
    wal.write_bytes(b"log")
    shm.write_bytes(b"shared")
    contents = {p.name: p.read_bytes() for p in (path, wal, shm)}
    fail_replace(monkeypatch, lambda src, dst: src == shm)  # the third move

    result = restore(capsys, env, good_backup, "--replace")
    assert result.code == 4 and "[restore_failed]" in result.err
    assert {p.name: p.read_bytes() for p in (path, wal, shm)} == contents
    assert not [p for p in path.parent.iterdir() if p.name.startswith(("pre-restore", ".restore"))]


def test_when_the_old_files_cannot_be_put_back_the_message_says_where_they_are(
    live, good_backup, monkeypatch, capsys
):
    path, env = live
    before_hash = sha256(path)
    fail_replace(
        monkeypatch,
        lambda src, dst: src.name.startswith(".restore-") or src.parent.name.startswith("pre-restore-"),
    )
    result = restore(capsys, env, good_backup, "--replace")
    assert result.code == 4 and "[restore_failed]" in result.err
    kept = next(p for p in path.parent.iterdir() if p.name.startswith("pre-restore-"))
    assert str(kept) in result.err and "move them back" in result.err
    assert sha256(kept / path.name) == before_hash  # still whole, and said to be there


def test_an_interrupted_restore_puts_the_previous_database_back(live, good_backup, monkeypatch, capsys):
    path, env = live
    before_hash = sha256(path)

    def interrupted(source, destination, *args, **kwargs):
        raise KeyboardInterrupt

    real = os.replace
    monkeypatch.setattr(
        os,
        "replace",
        lambda s, d, *a, **k: interrupted(s, d) if Path(d) == path and Path(s).name.startswith(".restore-") else real(s, d, *a, **k),
    )
    result = restore(capsys, env, good_backup, "--replace")
    assert result.code == 130
    assert sha256(path) == before_hash
    assert not [p for p in path.parent.iterdir() if p.name.startswith(("pre-restore", ".restore"))]


# --- scratch space ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def big_database(tmp_path_factory):
    path = make_big_database(tmp_path_factory.mktemp("big") / "big.db", 70)
    assert path.stat().st_size > 64 * MIB, "the premise: more than the 64 MiB the old /tmp held"
    return path


@pytest.fixture
def big(big_database, tmp_path, staging):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "house_helper.db"
    shutil.copy(big_database, path)
    return path, env_for(path, **{STAGING_ENV: str(staging)})


def test_a_database_above_64_mib_can_be_backed_up_verified_and_restored(big, tmp_path, staging, capsys):
    path, env = big
    backup_file = tmp_path / "big-backup.db"
    made = run(capsys, ["backup", "--output", str(backup_file)], env)
    assert made.code == 0, made.err
    assert backup_file.stat().st_size > 64 * MIB and "messages=70" in made.out

    named = run(capsys, ["verify", "--input", str(backup_file), "--require-head"], {STAGING_ENV: str(staging)})
    assert named.code == 0, named.err
    streamed = run(capsys, ["verify", "--input", "-"], {STAGING_ENV: str(staging)}, stdin=backup_file.read_bytes())
    assert streamed.code == 0, streamed.err

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    installed = restore(capsys, env_for(fresh / "house_helper.db", **{STAGING_ENV: str(staging)}), backup_file)
    assert installed.code == 0, installed.err
    assert tables(fresh / "house_helper.db") == tables(backup_file)

    replaced = restore(capsys, env, backup_file, "--replace")
    assert replaced.code == 0, replaced.err
    assert staging_files(staging) == []  # scratch space is cleaned after all of it


def test_a_backup_to_stdout_works_above_64_mib_too(big, staging, capsys):
    path, env = big
    result = run(capsys, ["backup", "--output", "-"], env)
    assert result.code == 0, result.err
    assert len(result.data) > 64 * MIB
    assert staging_files(staging) == []


def test_scratch_copies_go_to_the_staging_directory_and_not_to_the_system_temp_directory(
    live, tmp_path, staging, capsys, monkeypatch
):
    path, env = live
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "not-the-system-temp"))
    where = []
    real = dbtool.snapshot_database
    monkeypatch.setattr(dbtool, "snapshot_database", lambda s, d: (where.append(Path(d).parent.parent), real(s, d))[1])
    assert run(capsys, ["backup", "--output", "-"], env).code == 0
    assert where == [staging]


def test_scratch_space_is_private_to_the_command(live, good_backup, staging, capsys, monkeypatch):
    path, env = live
    seen = []
    real = dbtool.assess

    def spy(file, scratch):
        seen.append((scratch, stat.S_IMODE(os.stat(scratch).st_mode), stat.S_IMODE(os.stat(file).st_mode)))
        return real(file, scratch)

    monkeypatch.setattr(dbtool, "assess", spy)
    assert run(capsys, ["verify", "--input", "-"], env, stdin=good_backup.read_bytes()).code == 0
    scratch, directory_mode, file_mode = seen[0]
    assert scratch.parent == staging
    if os.name != "nt":
        assert directory_mode == 0o700 and file_mode == 0o600


def test_sqlite_is_told_to_keep_its_own_temporary_files_in_the_scratch_directory(
    live, good_backup, staging, capsys, monkeypatch
):
    path, env = live
    seen = []
    real = dbtool.assess
    monkeypatch.setattr(dbtool, "assess", lambda f, s: (seen.append(os.environ.get("SQLITE_TMPDIR")), real(f, s))[1])
    before = os.environ.get("SQLITE_TMPDIR")
    assert run(capsys, ["verify", "--input", "-"], env, stdin=good_backup.read_bytes()).code == 0
    assert Path(seen[0]).parent == staging
    assert os.environ.get("SQLITE_TMPDIR") == before


@pytest.mark.parametrize("command", ["backup", "verify", "restore"])
def test_too_little_scratch_space_is_refused_before_anything_is_written(
    live, good_backup, tmp_path, staging, capsys, monkeypatch, command
):
    path, env = live
    before = {"db": sha256(path), "names": listing(path.parent)}
    monkeypatch.setattr(dbtool, "_free_bytes", lambda where: 1 * MIB)
    target = tmp_path / "never.db"
    argv = {
        "backup": ["backup", "--output", str(target)],
        "verify": ["verify", "--input", str(good_backup)],
        "restore": ["restore", "--input", str(good_backup), "--replace"],
    }[command]
    result = run(capsys, argv, env)
    assert result.code == 3 and "[staging_space]" in result.err
    assert "Nothing was changed" in result.err
    assert not target.exists() and not Path(str(target) + ".part").exists()
    assert sha256(path) == before["db"] and listing(path.parent) == before["names"]
    assert staging_files(staging) == []


def test_the_data_volume_needs_room_for_the_file_a_restore_installs(
    live, good_backup, staging, capsys, monkeypatch
):
    path, env = live
    before = sha256(path)
    data = os.path.realpath(path.parent)
    monkeypatch.setattr(dbtool, "_free_bytes", lambda where: 1 * MIB if os.path.realpath(where) == data else 1 << 40)
    result = restore(capsys, env, good_backup, "--replace")
    assert result.code == 3 and "[data_dir_space]" in result.err
    assert sha256(path) == before and staging_files(staging) == []
    assert not [p for p in path.parent.iterdir() if p.name.startswith(("pre-restore", ".restore"))]


def test_a_named_backup_needs_room_at_its_destination_as_well_as_in_scratch(
    live, tmp_path, staging, capsys, monkeypatch
):
    path, env = live
    destination = tmp_path / "elsewhere"
    destination.mkdir()
    there = os.path.realpath(destination)
    monkeypatch.setattr(dbtool, "_free_bytes", lambda where: 1 * MIB if os.path.realpath(where) == there else 1 << 40)
    target = destination / "backup.db"
    result = run(capsys, ["backup", "--output", str(target)], env)
    assert result.code == 3 and "[output_space]" in result.err and "No backup was written" in result.err
    assert not target.exists() and not Path(str(target) + ".part").exists()
    assert staging_files(staging) == []


def test_a_stream_of_unknown_size_is_planned_for_the_largest_database(
    live, good_backup, staging, capsys, monkeypatch
):
    path, env = live
    just_short = dbtool.MAX_DATABASE_BYTES + dbtool.STAGING_HEADROOM - 1
    monkeypatch.setattr(dbtool, "_free_bytes", lambda where: just_short)
    refused = run(capsys, ["verify", "--input", "-"], env, stdin=good_backup.read_bytes())
    assert refused.code == 3 and "[staging_space]" in refused.err
    # A named file has a size, and a small one needs only that and the headroom.
    assert run(capsys, ["verify", "--input", str(good_backup)], env).code == 0


def test_the_largest_database_is_the_declared_limit_and_a_backup_it_could_not_restore_is_not_made(
    live, tmp_path, staging, capsys, monkeypatch
):
    assert dbtool.MAX_DATABASE_BYTES == 1 << 30
    path, env = live
    monkeypatch.setattr(dbtool, "MAX_DATABASE_BYTES", 10_000)
    target = tmp_path / "too-big.db"
    result = run(capsys, ["backup", "--output", str(target)], env)
    assert result.code == 3 and "[database_too_large]" in result.err
    assert not target.exists() and staging_files(staging) == []


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_a_disk_that_fills_up_midway_is_a_clean_refusal_that_changes_nothing(
    live, good_backup, staging, capsys, monkeypatch, fail_on_call
):
    path, env = live
    before = sha256(path)
    names = listing(path.parent)
    real, calls = os.fdopen, []

    class Full:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self.handle.close()

        def write(self, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def fdopen(descriptor, mode="r", *args, **kwargs):
        handle = real(descriptor, mode, *args, **kwargs)
        if "w" in mode:
            calls.append(mode)
            if len(calls) == fail_on_call:
                return Full(handle)
        return handle

    monkeypatch.setattr(os, "fdopen", fdopen)
    result = restore(capsys, env, good_backup, "--replace")
    expected = "[staging_full]" if fail_on_call == 1 else "[data_dir_space]"
    assert result.code == 3 and expected in result.err
    assert sha256(path) == before and listing(path.parent) == names
    assert staging_files(staging) == []


def test_scratch_is_removed_however_the_command_ends(live, good_backup, staging, capsys, monkeypatch):
    path, env = live
    data = good_backup.read_bytes()

    assert run(capsys, ["verify", "--input", "-"], env, stdin=data).code == 0
    assert staging_files(staging) == []
    assert run(capsys, ["verify", "--input", "-"], env, stdin=data[:50_000]).code == 4  # refused input
    assert staging_files(staging) == []

    real = dbtool.assess
    monkeypatch.setattr(dbtool, "assess", lambda f, s: (_ for _ in ()).throw(RuntimeError("boom")))
    assert run(capsys, ["verify", "--input", "-"], env, stdin=data).code == 1  # a crash
    assert staging_files(staging) == []
    monkeypatch.setattr(dbtool, "assess", lambda f, s: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert run(capsys, ["verify", "--input", "-"], env, stdin=data).code == 130  # an interrupt
    assert staging_files(staging) == []
    monkeypatch.setattr(dbtool, "assess", real)


@pytest.mark.parametrize("command", ["verify", "backup", "restore"])
def test_the_staging_directory_must_be_an_absolute_existing_directory(
    live, good_backup, tmp_path, capsys, command
):
    path, env = live
    argv = {
        "backup": ["backup", "--output", "-"],
        "verify": ["verify", "--input", str(good_backup)],
        "restore": ["restore", "--input", str(good_backup), "--replace"],
    }[command]
    a_file = tmp_path / "a-file"
    a_file.write_bytes(b"x")
    for bad in ("relative/staging", str(tmp_path / "does-not-exist"), str(a_file)):
        result = run(capsys, argv, {**env, STAGING_ENV: bad})
        assert result.code == 3 and "[staging_unusable]" in result.err, bad
    # Not set at all means the system's temporary directory (development): allowed.
    unset = {k: v for k, v in env.items() if k != STAGING_ENV}
    assert run(capsys, argv, unset).code == 0


@pytest.mark.parametrize("command", ["backup", "restore"])
def test_the_staging_directory_cannot_be_the_data_directory_or_inside_it(
    live, good_backup, capsys, command
):
    path, env = live
    inside = path.parent / "scratch"
    inside.mkdir()
    argv = {
        "backup": ["backup", "--output", "-"],
        "restore": ["restore", "--input", str(good_backup), "--replace"],
    }[command]
    before = sha256(path)
    for forbidden in (path.parent, inside):
        result = run(capsys, argv, {**env, STAGING_ENV: str(forbidden)})
        assert result.code == 3 and "[staging_unusable]" in result.err and "separate" in result.err
    assert sha256(path) == before


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_a_staging_directory_that_is_a_symbolic_link_is_refused(live, good_backup, tmp_path, capsys):
    path, env = live
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "link"
    link.symlink_to(elsewhere)
    result = run(capsys, ["verify", "--input", str(good_backup)], {**env, STAGING_ENV: str(link)})
    assert result.code == 3 and "[staging_unusable]" in result.err
    assert list(elsewhere.iterdir()) == []


# --- the schema, not only the revision -----------------------------------------------------------


def head_row_only(path: Path, revision: str = HEAD) -> Path:
    """What an audit found to pass the first version: nothing but the Alembic row."""
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    connection.commit()
    connection.close()
    return path


def test_a_database_that_only_advertises_the_head_revision_is_refused(tmp_path, staging, capsys):
    fake = head_row_only(tmp_path / "head-only.db")
    env = {STAGING_ENV: str(staging)}
    for argv in (
        ["verify", "--input", str(fake)],
        ["verify", "--input", str(fake), "--require-head"],
    ):
        result = run(capsys, argv, env)
        assert result.code == 3 and "[schema_mismatch]" in result.err, argv
        assert "table conversations is missing" in result.err and "more)" in result.err

    empty = tmp_path / "empty"
    empty.mkdir()
    refused = restore(capsys, env_for(empty / "house_helper.db", **env), fake)
    assert refused.code == 3 and "[schema_mismatch]" in refused.err
    assert listing(empty) == []  # nothing installed


def test_the_same_holds_for_an_older_revision_that_is_only_advertised(tmp_path, staging, capsys):
    fake = head_row_only(tmp_path / "behind-only.db", BEHIND)
    result = run(capsys, ["verify", "--input", str(fake)], {STAGING_ENV: str(staging)})
    assert result.code == 3 and "[schema_mismatch]" in result.err and BEHIND in result.err


def an_index_name(path: Path) -> str:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL ORDER BY name"
        ).fetchone()[0]
    finally:
        connection.close()


DAMAGE = {
    "a missing table": (["DROP TABLE email_login_challenges"], "table email_login_challenges is missing"),
    "a missing column": (["ALTER TABLE users DROP COLUMN disabled_at"], "users.disabled_at is missing"),
    "an extra table": (["CREATE TABLE zz_extra (x)"], "unexpected table zz_extra"),
    "an extra column": (["ALTER TABLE users ADD COLUMN zz_extra TEXT"], "unexpected column users.zz_extra"),
    "a view": (["CREATE VIEW zz_view AS SELECT id FROM users"], "triggers or views differ"),
    "a trigger": (
        ["CREATE TRIGGER zz_trigger AFTER INSERT ON users BEGIN SELECT 1; END"],
        "triggers or views differ",
    ),
}


@pytest.mark.parametrize("name", sorted(DAMAGE))
def test_verify_and_restore_refuse_a_database_whose_schema_is_not_the_migrations(
    name, good_backup, tmp_path, staging, capsys
):
    statements, expected = DAMAGE[name]
    damaged = tmp_path / "damaged.db"
    damaged.write_bytes(good_backup.read_bytes())
    alter(damaged, *statements)
    env = {STAGING_ENV: str(staging)}

    result = run(capsys, ["verify", "--input", str(damaged), "--require-head"], env)
    assert result.code == 3 and "[schema_mismatch]" in result.err, result.err
    assert expected in result.err

    empty = tmp_path / "empty"
    empty.mkdir()
    refused = restore(capsys, env_for(empty / "house_helper.db", **env), damaged)
    assert refused.code == 3 and "[schema_mismatch]" in refused.err
    assert listing(empty) == []


def test_a_missing_index_is_a_schema_mismatch(good_backup, tmp_path, staging, capsys):
    damaged = tmp_path / "damaged.db"
    damaged.write_bytes(good_backup.read_bytes())
    alter(damaged, f'DROP INDEX "{an_index_name(damaged)}"')
    result = run(capsys, ["verify", "--input", str(damaged)], {STAGING_ENV: str(staging)})
    assert result.code == 3 and "[schema_mismatch]" in result.err and "indexes of" in result.err


def test_a_table_name_from_the_inspected_database_is_not_echoed_unless_it_is_plain(
    good_backup, tmp_path, staging, capsys
):
    damaged = tmp_path / "damaged.db"
    damaged.write_bytes(good_backup.read_bytes())
    alter(damaged, 'CREATE TABLE "x; DROP TABLE users --" (x)')
    result = run(capsys, ["verify", "--input", str(damaged)], {STAGING_ENV: str(staging)})
    assert result.code == 3 and "unexpected table (unrecognised)" in result.err
    assert "DROP TABLE" not in result.err


def test_a_database_of_an_older_revision_is_judged_against_that_revisions_own_schema(
    tmp_path, staging, capsys
):
    env = {STAGING_ENV: str(staging)}
    older = make_database(tmp_path / "older.db", BEHIND)
    assert run(capsys, ["verify", "--input", str(older)], env).code == 0
    assert run(capsys, ["verify", "--input", str(older), "--require-head"], env).code == 3  # behind
    damaged = tmp_path / "older-damaged.db"
    damaged.write_bytes(older.read_bytes())
    alter(damaged, "DROP TABLE identity_invitations")
    result = run(capsys, ["verify", "--input", str(damaged)], env)
    assert result.code == 3 and "[schema_mismatch]" in result.err


def test_every_revision_in_the_history_verifies_against_its_own_migrations(tmp_path, staging, capsys):
    from alembic.script import ScriptDirectory

    from house_helper.persistence.database import alembic_config

    revisions = [s.revision for s in ScriptDirectory.from_config(alembic_config()).walk_revisions()]
    assert HEAD in revisions and BEHIND in revisions and len(revisions) >= 6
    for revision in revisions:
        database = make_database(tmp_path / f"{revision}.db", revision)
        result = run(capsys, ["verify", "--input", str(database)], {STAGING_ENV: str(staging)})
        assert result.code == 0, (revision, result.err)


def test_a_backup_of_a_database_without_the_schema_is_not_released(live, tmp_path, staging, capsys):
    path, env = live
    alter(path, "ALTER TABLE users DROP COLUMN disabled_at")
    target = tmp_path / "never.db"

    streamed = run(capsys, ["backup", "--output", "-"], env)
    assert streamed.code == 3 and "[schema_mismatch]" in streamed.err and streamed.data == b""
    named = run(capsys, ["backup", "--output", str(target)], env)
    assert named.code == 3 and "[schema_mismatch]" in named.err
    assert not target.exists() and not Path(str(target) + ".part").exists()
    assert staging_files(staging) == []


def test_a_backup_of_a_revision_this_image_cannot_check_is_not_released(live, tmp_path, capsys):
    path, env = live
    alter(path, "UPDATE alembic_version SET version_num = '0007_future'")
    result = run(capsys, ["backup", "--output", str(tmp_path / "never.db")], env)
    assert result.code == 3 and "[revision_unknown]" in result.err
    assert not (tmp_path / "never.db").exists()


def test_the_report_of_a_good_backup_says_the_structure_was_checked(live, tmp_path, capsys):
    path, env = live
    result = run(capsys, ["backup", "--output", str(tmp_path / "ok.db")], env)
    assert result.code == 0 and "schema structure ok" in result.out


# --- a backup is published whole, or not at all, and never over a file -----------------------------


def test_a_backup_does_not_replace_an_existing_file_or_an_earlier_partial_one(live, tmp_path, capsys):
    path, env = live
    known_good = tmp_path / "known-good.db"
    assert run(capsys, ["backup", "--output", str(known_good)], env).code == 0
    digest = sha256(known_good)

    again = run(capsys, ["backup", "--output", str(known_good)], env)
    assert again.code == 3 and "[output_unusable]" in again.err
    assert sha256(known_good) == digest

    target = tmp_path / "second.db"
    partial = Path(str(target) + ".part")
    partial.write_bytes(b"an interrupted earlier attempt")
    blocked = run(capsys, ["backup", "--output", str(target)], env)
    assert blocked.code == 3 and "[output_unusable]" in blocked.err
    assert partial.read_bytes() == b"an interrupted earlier attempt"  # left alone, not deleted
    assert not target.exists()


def test_a_failed_attempt_leaves_a_known_good_backup_untouched(live, tmp_path, capsys):
    path, env = live
    known_good = tmp_path / "known-good.db"
    assert run(capsys, ["backup", "--output", str(known_good)], env).code == 0
    digest = sha256(known_good)
    alter(path, "DROP TABLE email_login_challenges")
    failed = tmp_path / "failed.db"
    assert run(capsys, ["backup", "--output", str(failed)], env).code == 3
    assert sha256(known_good) == digest
    assert not failed.exists() and not Path(str(failed) + ".part").exists()


def test_a_failure_while_publishing_leaves_neither_a_backup_nor_a_partial_file(
    live, tmp_path, monkeypatch, capsys
):
    path, env = live
    target = tmp_path / "never.db"

    def no_link(source, destination, *args, **kwargs):
        raise OSError(errno.EIO, "injected failure")

    monkeypatch.setattr(os, "link", no_link)
    result = run(capsys, ["backup", "--output", str(target)], env)
    assert result.code == 1
    assert not target.exists() and not Path(str(target) + ".part").exists()


def test_a_file_that_appears_while_the_backup_runs_is_not_overwritten(live, tmp_path, monkeypatch, capsys):
    path, env = live
    target = tmp_path / "raced.db"
    real = dbtool._write_new_file

    def appear_first(snapshot, destination):
        destination.write_bytes(b"someone else's file")
        real(snapshot, destination)

    monkeypatch.setattr(dbtool, "_write_new_file", appear_first)
    result = run(capsys, ["backup", "--output", str(target)], env)
    assert result.code == 3 and "[output_unusable]" in result.err
    assert target.read_bytes() == b"someone else's file"
    assert not Path(str(target) + ".part").exists()
