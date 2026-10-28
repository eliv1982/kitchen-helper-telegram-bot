"""A photograph kept for a message (table `message_attachments`, migration 0009) is in the database,
so it is in every backup the existing tooling takes: no list of tables anywhere names it, none has
to. This file shows that with a real photo's bytes through the shipped `dbtool` commands: the online
backup, `verify` (which also compares the backup's schema with this image's migrations, so a
database without the table would be refused), the off-host rewrite `sanitize`, and `restore`.

The tooling itself is unchanged and its own behaviour is test_deploy_dbtool.py and
test_deploy_sanitize.py; this is only the one fact those cannot know: the photos travel with it.
Synthetic bytes, SQLite files under tmp_path, nothing leaves the machine.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from house_helper.persistence import attachments
from house_helper.persistence.database import create_db_engine, create_session_factory
from house_helper.persistence.models import User
from tests.deploy_support import STAGING_ENV, dbtool, env_for, make_database, run, tables
from tests.photo_support import jpeg, png

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def live(tmp_path):
    """A migrated database holding one user and two stored photos (a JPEG and a PNG)."""
    data = tmp_path / "data"
    data.mkdir()
    path = make_database(data / "house_helper.db")
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        with create_session_factory(engine)() as session, session.begin():
            user = User(display_name="кухня")
            session.add(user)
            session.flush()
            for data_bytes in (jpeg(4096, "one"), png(3000, "two")):
                attachments.store(session, user.id, data_bytes, now=NOW)
    finally:
        engine.dispose()
    return path, {bytes(jpeg(4096, "one")), bytes(png(3000, "two"))}


def stored_photos(path) -> set:
    connection = sqlite3.connect(str(path))
    try:
        return {bytes(row[0]) for row in connection.execute("SELECT data FROM message_attachments")}
    finally:
        connection.close()


def test_the_backup_holds_every_photo_byte_for_byte(live, tmp_path, capsys):
    path, photos = live
    target = tmp_path / "backup.db"

    result = run(capsys, ["backup", "--output", str(target)], env_for(path))

    assert result.code == 0, result.err
    assert stored_photos(target) == photos
    assert tables(target)["message_attachments"] == 2
    assert "message_attachments=2" in result.out  # the report counts the table, never the bytes


def test_verify_accepts_the_backup_with_its_photos_and_refuses_one_without_the_table(live, tmp_path, capsys):
    path, _ = live
    target = tmp_path / "backup.db"
    assert run(capsys, ["backup", "--output", str(target)], env_for(path)).code == 0
    assert run(capsys, ["verify", "--input", str(target)], {}).code == 0

    connection = sqlite3.connect(str(target))
    try:
        connection.execute("DROP TABLE message_attachments")
        connection.commit()
    finally:
        connection.close()

    broken = run(capsys, ["verify", "--input", str(target)], {})
    assert broken.code != 0  # a backup that lost the photos is not a sound backup of this schema


def test_the_off_host_rewrite_keeps_the_photos(live, tmp_path, capsys):
    path, photos = live
    snapshot = tmp_path / "snapshot.db"
    dbtool.snapshot_database(path, snapshot)
    staging = tmp_path / "staging"
    staging.mkdir()

    done = run(capsys, ["sanitize", "--input", str(snapshot), "--output", "-"], {STAGING_ENV: str(staging)})

    assert done.code == 0, done.err
    cleaned = tmp_path / "cleaned.db"
    cleaned.write_bytes(done.data)
    assert stored_photos(cleaned) == photos


def test_a_restore_brings_the_photos_back(live, tmp_path, capsys):
    path, photos = live
    backup = tmp_path / "backup.db"
    assert run(capsys, ["backup", "--output", str(backup)], env_for(path)).code == 0
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    target = fresh / "house_helper.db"

    restored = run(capsys, ["restore", "--input", str(backup)], env_for(target))

    assert restored.code == 0, restored.err
    assert stored_photos(target) == photos
