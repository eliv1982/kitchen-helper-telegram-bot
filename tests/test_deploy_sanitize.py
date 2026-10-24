"""`dbtool sanitize` (the off-host backup's rewrite): a snapshot of a database that has had rows deleted
still holds them, and the rewrite is what keeps them out of a copy that leaves the host.

Everything is real SQLite on synthetic data: the snapshot is taken by the shipped `dbtool backup`, the
rewrite by the shipped `dbtool sanitize`, and the result is read back as bytes. No production database,
no real conversation: the "conversations" are made-up marker strings.
"""

import io
import sqlite3

import pytest

from tests.deploy_support import (
    BEHIND,
    STAGING_ENV,
    alter,
    dbtool,
    make_database,
    run,
    run_tool,
    set_revision,
    sha256,
    staging_files,
    tables,
)
from tests.offsite_support import DELETED_MARKER, KEPT_MARKER, database_health, make_database_with_deleted_rows, row_counts


@pytest.fixture
def staging(tmp_path):
    path = tmp_path / "staging"
    path.mkdir()
    return path


@pytest.fixture
def snapshot(tmp_path):
    """A snapshot, taken the way the backup takes it, of a database that had rows deleted."""
    live = make_database_with_deleted_rows(tmp_path / "live.db", kept=40, deleted=40)  # enough rows to split pages
    path = tmp_path / "snapshot.db"
    dbtool.snapshot_database(live, path)
    return path


def env(staging):
    return {STAGING_ENV: str(staging)}  # no database URL: the command touches no database


def count(path, marker):
    return path.read_bytes().count(marker.encode())


# --- the problem, and the cure ---------------------------------------------------------------------------


def test_a_plain_snapshot_still_holds_the_rows_that_were_deleted(snapshot):
    if count(snapshot, DELETED_MARKER) == 0:
        pytest.skip("this SQLite build erases deleted content itself (SQLITE_SECURE_DELETE); nothing to remove")
    assert count(snapshot, KEPT_MARKER) > 0
    assert row_counts(snapshot)["messages"] == 40  # logically the rows are gone; physically they are not


def test_sanitize_leaves_no_trace_of_the_deleted_rows_and_keeps_every_live_row(snapshot, staging, capsys):
    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", "-"], env(staging))
    assert done.code == 0, done.err
    cleaned = snapshot.parent / "cleaned.db"
    cleaned.write_bytes(done.data)
    assert count(cleaned, DELETED_MARKER) == 0
    # Exactly one copy of each live row: with secure_delete on while the file is laid out, the bytes that page
    # rebuilds free are zeroed too (without it a fresh file of this size holds more copies than rows).
    assert count(cleaned, KEPT_MARKER) == 40
    assert row_counts(cleaned) == row_counts(snapshot)
    assert database_health(cleaned) == ("ok", 0)  # sound, and no free page to hide anything in
    assert done.data[18:20] == b"\x01\x01"  # a rollback-journal file: it needs no -wal beside it
    assert "sha256 " + sha256(cleaned) in done.err and "schema revision" in done.err
    # The report names tables and counts, never content.
    assert DELETED_MARKER not in done.err and KEPT_MARKER not in done.err


def test_sanitize_does_not_touch_its_input_or_leave_scratch_files(snapshot, staging, capsys):
    before = sha256(snapshot)
    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", "-"], env(staging))
    assert done.code == 0
    assert sha256(snapshot) == before
    assert staging_files(staging) == []


def test_sanitize_reads_standard_input_and_writes_a_new_file(snapshot, staging, capsys, tmp_path):
    target = tmp_path / "out.db"
    done = run(capsys, ["sanitize", "--input", "-", "--output", str(target)], env(staging), stdin=snapshot.read_bytes())
    assert done.code == 0, done.err
    assert count(target, DELETED_MARKER) == 0 and database_health(target) == ("ok", 0)
    assert done.data == b""  # nothing on stdout when the output is a file
    assert staging_files(staging) == [] and not (tmp_path / "out.db.part").exists()


def test_the_command_works_as_a_separate_process_without_a_database_url(snapshot, tmp_path):
    done = run_tool("sanitize", "--input", str(snapshot), "--output", "-", **{STAGING_ENV: str(tmp_path)})
    assert done.returncode == 0, done.stderr.decode()
    cleaned = tmp_path / "from-process.db"
    cleaned.write_bytes(done.stdout)
    assert count(cleaned, DELETED_MARKER) == 0 and row_counts(cleaned) == row_counts(snapshot)


def test_the_live_database_is_never_opened_by_the_command(tmp_path, staging, capsys):
    live = make_database_with_deleted_rows(tmp_path / "live.db")
    snap = tmp_path / "snap.db"
    dbtool.snapshot_database(live, snap)
    def settings():
        connection = sqlite3.connect(live)
        try:
            return [connection.execute(f"PRAGMA {name}").fetchone()[0] for name in ("secure_delete", "journal_mode", "auto_vacuum", "page_size")]
        finally:
            connection.close()

    before, before_settings = sha256(live), settings()
    done = run(capsys, ["sanitize", "--input", str(snap), "--output", "-"], {**env(staging), "HOUSE_HELPER_DATABASE_URL": f"sqlite:///{live}"})
    assert done.code == 0
    assert sha256(live) == before  # same bytes: not vacuumed, not checkpointed
    assert settings() == before_settings  # and no setting changed


# --- what it refuses -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "damage, code, words",
    [
        (lambda p: p.write_bytes(b"this is not a database, " * 100), 4, "not_a_database"),
        (lambda p: alter(p, "DROP TABLE messages"), 3, "schema_mismatch"),
        (lambda p: set_revision(p, "9999_from_the_future"), 3, "revision_unknown"),
    ],
    ids=["not a database", "wrong schema", "unknown revision"],
)
def test_a_backup_that_is_not_sound_and_at_the_head_is_not_rewritten(snapshot, staging, capsys, damage, code, words):
    damage(snapshot)
    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", "-"], env(staging))
    assert (done.code, done.data) == (code, b"")
    assert words in done.err
    assert staging_files(staging) == []


def test_a_genuinely_older_backup_is_refused_because_the_rewrite_is_for_the_running_version(tmp_path, staging, capsys):
    older = make_database(tmp_path / "older.db", BEHIND)
    done = run(capsys, ["sanitize", "--input", str(older), "--output", "-"], env(staging))
    assert (done.code, done.data) == (3, b"") and "revision_behind" in done.err


def test_a_damaged_file_is_refused(snapshot, staging, capsys):
    data = bytearray(snapshot.read_bytes())
    for offset in range(4096 * 3, 4096 * 3 + 64):
        data[offset] ^= 0xFF
    snapshot.write_bytes(bytes(data))
    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", "-"], env(staging))
    assert done.code in (3, 4) and done.data == b""
    assert staging_files(staging) == []


def test_an_existing_output_file_is_never_replaced(snapshot, staging, capsys, tmp_path):
    target = tmp_path / "exists.db"
    target.write_bytes(b"precious")
    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", str(target)], env(staging))
    assert done.code == 3 and "output_unusable" in done.err
    assert target.read_bytes() == b"precious"


def test_binary_output_is_never_written_to_a_terminal(snapshot, staging, capsys):
    class Terminal(io.BytesIO):
        def isatty(self):
            return True

    stdout = Terminal()
    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", "-"], env(staging), stdout=stdout)
    assert done.code == 2 and stdout.getvalue() == b"" and "stdout_is_a_terminal" in done.err


def test_an_old_sqlite_without_vacuum_into_refuses_instead_of_copying(snapshot, staging, capsys, monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 26, 0))
    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", "-"], env(staging))
    assert (done.code, done.data) == (3, b"") and "sanitize_unsupported" in done.err


def test_a_rewrite_that_still_has_free_pages_is_not_released(snapshot, staging, capsys, monkeypatch):
    # The defence against the rewrite silently not happening: a "rewrite" that is a copy keeps the
    # free pages (and the bytes in them), and the command must notice.
    def copy_instead(source, destination):
        destination.write_bytes(source.read_bytes())

    monkeypatch.setattr(dbtool, "_vacuum_into", copy_instead)
    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", "-"], env(staging))
    assert (done.code, done.data) == (4, b"") and "sanitize_incomplete" in done.err
    assert staging_files(staging) == []


def test_a_rewrite_that_lost_rows_is_not_released(snapshot, staging, capsys, monkeypatch):
    real = dbtool._vacuum_into

    def lossy(source, destination):
        real(source, destination)
        alter(destination, f"DELETE FROM messages WHERE content LIKE '{KEPT_MARKER}-00%'")

    monkeypatch.setattr(dbtool, "_vacuum_into", lossy)
    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", "-"], env(staging))
    assert (done.code, done.data) == (4, b"") and "sanitize_mismatch" in done.err


def test_not_enough_scratch_space_is_refused_before_the_rewrite(snapshot, staging, capsys, monkeypatch):
    # Room was found for the staged copy (the first look), none for the second one (the look after staging).
    real = dbtool._free_bytes
    monkeypatch.setattr(dbtool, "_free_bytes", lambda path: 1 if path.name.startswith("hh-sanitize-") else real(path))
    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", "-"], env(staging))
    assert (done.code, done.data) == (3, b"") and "staging_space" in done.err
    assert staging_files(staging) == []


def test_sanitize_of_an_empty_but_migrated_database_works(tmp_path, staging, capsys):
    path = make_database(tmp_path / "empty.db")
    snap = tmp_path / "snap.db"
    dbtool.snapshot_database(path, snap)
    done = run(capsys, ["sanitize", "--input", str(snap), "--output", "-"], env(staging))
    assert done.code == 0, done.err
    out = tmp_path / "out.db"
    out.write_bytes(done.data)
    assert tables(out) == tables(snap)
