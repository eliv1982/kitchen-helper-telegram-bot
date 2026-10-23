"""deploy/ops/dbtool.py: the operator tool the deployment runbook is built from.

It is a script, not a package module: it is loaded by path, and one test runs it the way the
container does (`python /opt/house-helper/ops/dbtool.py`, with PYTHONPATH set). Every database
is a SQLite file under pytest's tmp_path; nothing leaves the machine.

What the tests protect:
  * a backup is taken through SQLite's online backup, so data that exists only in the
    -wal file is in it (a copy of the main file is shown not to have it: the premise is real);
  * a backup is never overwritten, is mode 0600, and is refused when it is not sound;
  * `verify` and `restore` accept a sound database of a revision this image knows and refuse
    everything else, and a refused restore changes nothing;
  * `restore` moves the old database together with its -wal and -shm out of the way, so
    SQLite cannot replay an old log onto the restored file;
  * the start-up check is read only, and says the right thing for a database behind or
    ahead of the image;
  * no message ever contains the database URL.

The lock, the writer checks, the failure paths of restore, scratch space, schema completeness and
backup publication are in test_deploy_safety.py; the shell scripts are in test_deploy_scripts.py.
"""

import hashlib
import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from house_helper.persistence.database import create_db_engine, create_session_factory
from house_helper.persistence.models import User
from tests.deploy_support import (
    BEHIND,
    DBTOOL_PATH,
    HEAD,
    REPO_ROOT,
    URL_ENV,
    dbtool,
    env_for,
    listing,
    make_database,
    posix_only,
    run,
    set_revision,
    sha256,
    tables,
)


# --- helpers ------------------------------------------------------------------------------


@pytest.fixture
def live(tmp_path):
    """A migrated database at an absolute path, and the environment that names it."""
    data = tmp_path / "data"
    data.mkdir()
    path = make_database(data / "house_helper.db")
    return path, env_for(path)


@pytest.fixture
def good_backup(live, tmp_path, capsys):
    path, env = live
    target = tmp_path / "good.db"
    assert run(capsys, ["backup", "--output", str(target)], env).code == 0
    return target


# --- the configured database --------------------------------------------------------------


@pytest.mark.parametrize(
    "url, code",
    [
        (None, "not_configured"),
        ("", "not_configured"),
        ("sqlite://", "sqlite_not_a_file"),
        ("sqlite:///:memory:", "sqlite_not_a_file"),
        ("sqlite:///house_helper.db", "path_not_absolute"),
        ("postgresql+psycopg://user:hunter2-password@db.example/house", "unsupported_backend"),
        ("not a url at all hunter2-password", "invalid_url"),
    ],
)
def test_a_database_url_that_is_not_an_absolute_sqlite_file_is_refused_without_quoting_it(
    capsys, url, code
):
    env = {} if url is None else {URL_ENV: url}
    for command in (["check-config"], ["check-schema"], ["backup", "--output", "-"]):
        result = run(capsys, command, env)
        assert result.code != 0
        assert f"[{code}]" in result.err
        assert "hunter2" not in result.err + result.out


def test_check_config_describes_a_usable_directory_and_refuses_a_missing_one(tmp_path, capsys):
    absent = tmp_path / "data" / "house_helper.db"
    missing_dir = run(capsys, ["check-config"], env_for(absent))
    assert missing_dir.code == 3 and "[data_dir_missing]" in missing_dir.err

    (tmp_path / "data").mkdir()
    fresh = run(capsys, ["check-config"], env_for(absent))
    assert fresh.code == 0 and "absent or empty" in fresh.out

    make_database(absent)
    existing = run(capsys, ["check-config"], env_for(absent))
    assert existing.code == 0 and "present" in existing.out


def test_check_config_refuses_a_file_that_is_not_a_database(tmp_path, capsys):
    (tmp_path / "data").mkdir()
    path = tmp_path / "data" / "house_helper.db"
    path.write_bytes(b"this is not sqlite\n" * 50)
    result = run(capsys, ["check-config"], env_for(path))
    assert result.code == 4 and "[not_a_database]" in result.err


# --- the start-up check -------------------------------------------------------------------


def test_check_schema_accepts_the_head_and_changes_nothing(live, capsys):
    path, env = live
    before = sha256(path)
    result = run(capsys, ["check-schema"], env)
    assert result.code == 0 and HEAD in result.out
    assert sha256(path) == before


def test_check_schema_refuses_a_missing_database_and_does_not_create_it(tmp_path, capsys):
    (tmp_path / "data").mkdir()
    path = tmp_path / "data" / "house_helper.db"
    result = run(capsys, ["check-schema"], env_for(path))
    assert result.code == 4 and "[database_missing]" in result.err
    assert "First install" in result.err
    assert not path.exists()


def test_check_schema_tells_behind_from_ahead(tmp_path, capsys):
    (tmp_path / "data").mkdir()
    behind = make_database(tmp_path / "data" / "behind.db", BEHIND)
    result = run(capsys, ["check-schema"], env_for(behind))
    assert result.code == 4 and "[schema_not_head]" in result.err
    assert BEHIND in result.err and "BEHIND" in result.err and "Upgrade" in result.err

    ahead = make_database(tmp_path / "data" / "ahead.db")
    set_revision(ahead, "0007_future")
    result = run(capsys, ["check-schema"], env_for(ahead))
    assert result.code == 4 and "[schema_not_head]" in result.err
    assert "AHEAD" in result.err and "Rollback" in result.err and "Never migrate down" in result.err


# --- backup -------------------------------------------------------------------------------


def test_a_backup_holds_what_exists_only_in_the_wal_and_a_file_copy_does_not(
    live, tmp_path, capsys
):
    path, env = live
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:  # this pooled connection never checkpoints
            connection.exec_driver_sql("PRAGMA wal_autocheckpoint=0")
        with create_session_factory(engine)() as session, session.begin():
            session.add(User(display_name="committed, not yet checkpointed"))

        # The premise: copying the main file alone misses the committed row.
        naive = tmp_path / "naive.db"
        naive.write_bytes(path.read_bytes())
        assert tables(naive)["users"] == 0

        target = tmp_path / "backup.db"
        result = run(capsys, ["backup", "--output", str(target)], env)
        assert result.code == 0, result.err
    finally:
        engine.dispose()

    assert tables(target)["users"] == 1
    assert "users=1" in result.out


def test_a_backup_is_one_self_contained_file_in_rollback_journal_mode(live, tmp_path, capsys):
    path, env = live
    target = tmp_path / "backup.db"
    assert run(capsys, ["backup", "--output", str(target)], env).code == 0
    header = target.read_bytes()[:20]
    assert header.startswith(b"SQLite format 3\x00")
    assert header[18:20] == b"\x01\x01"  # file format 1/1: legacy journal, not WAL (2/2)
    assert not Path(str(target) + "-wal").exists()


def test_a_backup_reports_its_checksum_revision_and_counts_but_no_content(live, tmp_path, capsys):
    path, env = live
    target = tmp_path / "backup.db"
    result = run(capsys, ["backup", "--output", str(target)], env)
    assert f"sha256 {sha256(target)}" in result.out
    assert f"schema revision {HEAD}" in result.out
    assert "users=0" in result.out


def test_a_backup_never_overwrites_and_needs_a_directory(live, tmp_path, capsys):
    path, env = live
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"precious")
    result = run(capsys, ["backup", "--output", str(existing)], env)
    assert result.code == 3 and "[output_unusable]" in result.err
    assert existing.read_bytes() == b"precious"

    result = run(capsys, ["backup", "--output", str(tmp_path / "nope" / "b.db")], env)
    assert result.code == 3 and "[output_unusable]" in result.err


@posix_only
def test_a_backup_file_is_private(live, tmp_path, capsys):
    path, env = live
    target = tmp_path / "backup.db"
    assert run(capsys, ["backup", "--output", str(target)], env).code == 0
    assert (target.stat().st_mode & 0o777) == 0o600
    assert not Path(str(target) + ".part").exists()


def test_a_backup_to_stdout_is_the_file_and_the_report_goes_to_stderr(live, tmp_path, capsys):
    path, env = live
    result = run(capsys, ["backup", "--output", "-"], env)
    assert result.code == 0
    assert result.data.startswith(b"SQLite format 3\x00")
    assert f"sha256 {hashlib.sha256(result.data).hexdigest()}" in result.err
    assert result.out == ""  # stdout text stream stays clean: the data went to the binary stream


def test_a_backup_will_not_write_binary_data_to_a_terminal(live, capsys):
    class Terminal(io.BytesIO):
        def isatty(self):
            return True

    path, env = live
    stdout = Terminal()
    result = run(capsys, ["backup", "--output", "-"], env, stdout=stdout)
    assert result.code == 2 and "[stdout_is_a_terminal]" in result.err
    assert stdout.getvalue() == b""


def test_a_backup_of_a_missing_database_is_refused(tmp_path, capsys):
    (tmp_path / "data").mkdir()
    result = run(capsys, ["backup", "--output", "-"], env_for(tmp_path / "data" / "x.db"))
    assert result.code == 4 and "[database_missing]" in result.err


def test_a_backup_does_not_modify_the_live_database(live, tmp_path, capsys):
    path, env = live
    before = tables(path)
    assert run(capsys, ["backup", "--output", str(tmp_path / "b.db")], env).code == 0
    assert tables(path) == before


# --- verify -------------------------------------------------------------------------------


def verify(capsys, data: bytes, *extra):
    return run(capsys, ["verify", "--input", "-", *extra], {}, stdin=data)


def test_verify_needs_no_database_and_accepts_a_good_backup(good_backup, capsys):
    result = verify(capsys, good_backup.read_bytes())
    assert result.code == 0
    assert "the backup is sound" in result.out and HEAD in result.out


def test_verify_reads_a_named_file_too(good_backup, capsys):
    result = run(capsys, ["verify", "--input", str(good_backup)], {})
    assert result.code == 0


@pytest.mark.parametrize(
    "mangle, code, exit_code",
    [
        (lambda b: b[:100_000], "integrity_failed", 4),
        (lambda b: b"", "not_a_database", 4),
        (lambda b: b"hello, this is not a database\n", "not_a_database", 4),
    ],
)
def test_verify_refuses_a_damaged_or_foreign_input(good_backup, capsys, mangle, code, exit_code):
    result = verify(capsys, mangle(good_backup.read_bytes()))
    assert result.code == exit_code and f"[{code}]" in result.err


def test_verify_finds_page_level_damage(good_backup, capsys):
    data = bytearray(good_backup.read_bytes())
    for page in range(1, 12):  # the b-tree headers; slack space inside a page is not checked
        data[4096 * page : 4096 * page + 16] = b"\xff" * 16
    result = verify(capsys, bytes(data))
    assert result.code == 4 and "[integrity_failed]" in result.err


def test_verify_refuses_a_valid_sqlite_file_that_is_not_this_applications(tmp_path, capsys):
    other = tmp_path / "other.db"
    connection = sqlite3.connect(str(other))
    connection.execute("CREATE TABLE t (x)")
    connection.commit()
    connection.close()
    result = verify(capsys, other.read_bytes())
    assert result.code == 3 and "[no_schema_version]" in result.err


def test_verify_refuses_a_revision_this_image_does_not_know_without_echoing_odd_text(
    good_backup, tmp_path, capsys
):
    for revision, shown in (("0007_future", "0007_future"), ("evil; DROP TABLE x --", "(unrecognised)")):
        copy = tmp_path / "copy.db"
        copy.write_bytes(good_backup.read_bytes())
        set_revision(copy, revision)
        result = verify(capsys, copy.read_bytes())
        assert result.code == 3 and "[revision_unknown]" in result.err
        assert shown in result.err
        assert "DROP TABLE" not in result.err


def test_verify_accepts_an_older_known_revision_unless_the_head_is_required(tmp_path, capsys):
    older = make_database(tmp_path / "older.db", BEHIND)
    accepted = verify(capsys, older.read_bytes())
    assert accepted.code == 0 and "behind this image's head" in accepted.out
    required = verify(capsys, older.read_bytes(), "--require-head")
    assert required.code == 3 and "[revision_behind]" in required.err


def test_verify_rejects_an_input_larger_than_the_limit(good_backup, capsys, monkeypatch):
    monkeypatch.setattr(dbtool, "MAX_DATABASE_BYTES", 1000)
    result = verify(capsys, good_backup.read_bytes())
    assert result.code == 3 and "[input_too_large]" in result.err


# --- restore ------------------------------------------------------------------------------


def restore(capsys, env, backup_path, *extra):
    return run(capsys, ["restore", "--input", str(backup_path), *extra], env)


def test_restore_into_an_empty_directory_installs_a_sound_database(good_backup, tmp_path, capsys):
    data = tmp_path / "fresh"
    data.mkdir()
    target = data / "house_helper.db"
    result = restore(capsys, env_for(target), good_backup)
    assert result.code == 0, result.err
    assert tables(target) == tables(good_backup)
    assert listing(data) == ["house_helper.db"]
    assert run(capsys, ["check-schema"], env_for(target)).code == 0


def test_restore_refuses_to_touch_an_existing_database_without_replace(live, good_backup, capsys):
    path, env = live
    before = sha256(path)
    result = restore(capsys, env, good_backup)
    assert result.code == 3 and "[database_exists]" in result.err
    assert sha256(path) == before


def test_restore_moves_a_damaged_database_and_its_log_files_aside_together(
    live, good_backup, capsys
):
    path, env = live
    # Nothing can open a damaged file, so nothing can tidy its log files either: they must
    # not stay beside the new database, where SQLite would replay them onto it.
    path.write_bytes(b"corrupted beyond repair" * 100)
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    wal.write_bytes(b"stale write-ahead log")
    shm.write_bytes(b"stale shared memory")
    old_database = sha256(path)

    result = restore(capsys, env, good_backup, "--replace")
    assert result.code == 0, result.err

    assert not wal.exists() and not shm.exists()  # nothing beside the new file to be replayed
    kept = [p for p in path.parent.iterdir() if p.name.startswith("pre-restore-")]
    assert len(kept) == 1
    assert sorted(p.name for p in kept[0].iterdir()) == sorted([path.name, wal.name, shm.name])
    assert sha256(kept[0] / path.name) == old_database
    assert (kept[0] / wal.name).read_bytes() == b"stale write-ahead log"
    assert (kept[0] / shm.name).read_bytes() == b"stale shared memory"
    assert sha256(path) == sha256(good_backup)


def test_stale_log_files_beside_a_sound_old_database_never_reach_the_new_one(
    live, good_backup, capsys
):
    path, env = live
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA journal_mode=DELETE").fetchall()
    connection.close()
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    wal.write_bytes(b"stale write-ahead log")
    shm.write_bytes(b"stale shared memory")

    result = restore(capsys, env, good_backup, "--replace")
    assert result.code == 0, result.err

    assert not wal.exists() and not shm.exists()
    assert sha256(path) == sha256(good_backup)
    kept = [p for p in path.parent.iterdir() if p.name.startswith("pre-restore-")]
    assert len(kept) == 1 and (kept[0] / path.name).exists()  # the old database is kept
    assert dbtool.inspect_database(kept[0] / path.name).sound


def test_restore_replaces_a_corrupt_database(live, good_backup, capsys):
    path, env = live
    path.write_bytes(b"corrupted beyond repair" * 100)
    result = restore(capsys, env, good_backup, "--replace")
    assert result.code == 0, result.err
    assert tables(path) == tables(good_backup)


def test_a_bad_backup_changes_nothing(live, good_backup, tmp_path, capsys):
    path, env = live
    before = sorted(p.name for p in path.parent.iterdir())
    digest = sha256(path)
    bad = tmp_path / "bad.db"
    bad.write_bytes(good_backup.read_bytes()[:50_000])

    result = restore(capsys, env, bad, "--replace")
    assert result.code == 4 and "[integrity_failed]" in result.err
    assert sha256(path) == digest
    assert sorted(p.name for p in path.parent.iterdir()) == before  # no pre-restore dir, no .part


def test_restore_refuses_a_revision_this_image_does_not_know(live, good_backup, tmp_path, capsys):
    path, env = live
    future = tmp_path / "future.db"
    future.write_bytes(good_backup.read_bytes())
    set_revision(future, "0007_future")
    digest = sha256(path)
    result = restore(capsys, env, future, "--replace")
    assert result.code == 3 and "[revision_unknown]" in result.err
    assert sha256(path) == digest


def test_restore_accepts_an_older_revision_for_the_rollback_to_the_matching_version(
    live, tmp_path, capsys
):
    path, env = live
    older = make_database(tmp_path / "older.db", BEHIND)
    result = restore(capsys, env, older, "--replace")
    assert result.code == 0 and "behind this image's head" in result.out


@posix_only
def test_a_restored_database_is_private(live, good_backup, capsys):
    path, env = live
    assert restore(capsys, env, good_backup, "--replace").code == 0
    assert (path.stat().st_mode & 0o777) == 0o600


def test_restore_needs_the_data_directory(good_backup, tmp_path, capsys):
    result = restore(capsys, env_for(tmp_path / "missing" / "house_helper.db"), good_backup)
    assert result.code == 3 and "[data_dir_missing]" in result.err


# --- the command line ---------------------------------------------------------------------


def test_a_command_line_error_is_a_usage_error(capsys):
    result = run(capsys, ["backup"], {})
    assert result.code == 2 and "usage [arguments]" in result.err
    assert run(capsys, [], {}).code == 2


def test_an_unexpected_failure_prints_the_class_only(live, capsys, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret detail: /some/path and hunter2")

    monkeypatch.setattr(dbtool, "check_config", boom)
    path, env = live
    result = run(capsys, ["check-config"], env)
    assert result.code == 1 and "unexpected RuntimeError" in result.err
    assert "hunter2" not in result.err + result.out


def test_the_tool_runs_as_a_script_the_way_the_container_runs_it(live):
    """Not imported: `python <path>` with PYTHONPATH naming the application, as the image sets."""
    path, env = live
    environ = {k: v for k, v in os.environ.items() if not k.startswith("HOUSE_HELPER_")}
    environ.update(env)
    environ["PYTHONPATH"] = str(REPO_ROOT)
    done = subprocess.run(
        [sys.executable, str(DBTOOL_PATH), "check-schema"],
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert HEAD in done.stdout

    help_ = subprocess.run(
        [sys.executable, str(DBTOOL_PATH), "--help"],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert help_.returncode == 0 and "restore" in help_.stdout
