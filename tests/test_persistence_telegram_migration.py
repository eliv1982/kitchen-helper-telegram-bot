"""Migrations 0008 (Telegram links and pairing codes) and 0009 (message attachments): each adds
new, empty tables and changes nothing that exists; the migration and the models describe the same
tables; the way back drops only what the revision added.

The shape follows tests/test_persistence_favorites_migration.py (0007), whose helpers it uses.
"""

import re

import pytest
import sqlalchemy as sa

from house_helper.persistence.authn import authenticate
from house_helper.persistence.base import Base
from house_helper.persistence.database import (
    create_db_engine,
    create_session_factory,
    downgrade_database,
    upgrade_database,
)
from tests.test_persistence_migrations import (
    FAVORITES,
    HEAD,
    LATER,
    TELEGRAM,
    _fingerprint,
    _offline_sql,
    _r4c_database,
    _snapshot_of,
    _tables,
    _version,
)
from tests.test_persistence_schema import TABLES

TELEGRAM_TABLES = {"telegram_links", "telegram_link_codes"}
PHOTO_TABLES = {"message_attachments"}
OLD_TABLES = TABLES - TELEGRAM_TABLES - PHOTO_TABLES


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'telegram_migration.db'}")
    yield engine
    engine.dispose()


def _at_0007(engine) -> dict:
    found = _r4c_database(engine)
    upgrade_database(engine, FAVORITES)
    return found


def test_each_revision_adds_only_its_own_empty_tables_and_changes_no_row(engine):
    found = _at_0007(engine)
    before = _snapshot_of(engine, OLD_TABLES)
    assert _version(engine) == [FAVORITES]

    upgrade_database(engine, TELEGRAM)
    assert _version(engine) == [TELEGRAM]
    assert _tables(engine) == OLD_TABLES | TELEGRAM_TABLES | {"alembic_version"}
    assert _snapshot_of(engine, OLD_TABLES) == before

    upgrade_database(engine)
    assert _version(engine) == [HEAD]
    assert _tables(engine) == TABLES | {"alembic_version"}
    assert _snapshot_of(engine, OLD_TABLES) == before  # every row of every old table
    with engine.connect() as connection:
        for table in TELEGRAM_TABLES | PHOTO_TABLES:
            assert connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
    with create_session_factory(engine)() as db:
        assert authenticate(db, found["telegram"].token, now=LATER) is not None  # nobody signed out


def test_the_migration_and_the_models_describe_the_same_tables(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    modeled = create_db_engine(f"sqlite:///{tmp_path / 'modeled.db'}")
    try:
        upgrade_database(migrated)
        Base.metadata.create_all(modeled)
        a, b = _fingerprint(migrated), _fingerprint(modeled)

        for table in sorted(TELEGRAM_TABLES | PHOTO_TABLES):
            assert a[table] == b[table], table
    finally:
        migrated.dispose()
        modeled.dispose()


def test_the_new_tables_have_the_keys_and_checks_the_design_says(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    try:
        upgrade_database(migrated)
        found = _fingerprint(migrated)

        links = found["telegram_links"]
        assert sorted(links["uniques"]) == [
            ("uq_telegram_links_telegram_user_id", ("telegram_user_id",)),
            ("uq_telegram_links_user_id", ("user_id",)),
        ]
        assert links["fks"] == [
            ("fk_telegram_links_user_id_users", ("user_id",), "users", ("id",), (("ondelete", "CASCADE"),))
        ]
        assert [name for name, _ in links["checks"]] == [
            "ck_telegram_links_display_name_max_length",
            "ck_telegram_links_telegram_user_id_positive",
        ]

        codes = found["telegram_link_codes"]
        assert codes["uniques"] == [("uq_telegram_link_codes_code_hash", ("code_hash",))]
        assert [name for name, _ in codes["checks"]] == [
            "ck_telegram_link_codes_code_hash_length",
            "ck_telegram_link_codes_expires_after_created",
        ]

        photos = found["message_attachments"]
        assert photos["uniques"] == [("uq_message_attachments_message_id", ("message_id",))]
        assert sorted(photos["fks"]) == [
            ("fk_message_attachments_message_id_messages", ("message_id",), "messages", ("id",), (("ondelete", "CASCADE"),)),
            ("fk_message_attachments_user_id_users", ("user_id",), "users", ("id",), (("ondelete", "CASCADE"),)),
        ]
        assert [name for name, _ in photos["checks"]] == [
            "ck_message_attachments_content_type_valid",
            "ck_message_attachments_data_matches_size",
            "ck_message_attachments_expires_after_created",
            "ck_message_attachments_sha256_length",
            "ck_message_attachments_size_bytes_valid",
        ]
    finally:
        migrated.dispose()


def test_nothing_that_existed_refers_to_the_new_tables(engine):
    upgrade_database(engine)

    inspector = sa.inspect(engine)
    new = TELEGRAM_TABLES | PHOTO_TABLES
    referring = {
        table: [key["referred_table"] for key in inspector.get_foreign_keys(table) if key["referred_table"] in new]
        for table in inspector.get_table_names()
        if table not in new
    }

    assert {table: found for table, found in referring.items() if found} == {}


def test_the_way_back_drops_only_what_each_revision_added(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    at_0007 = create_db_engine(f"sqlite:///{tmp_path / 'at_0007.db'}")
    at_0008 = create_db_engine(f"sqlite:///{tmp_path / 'at_0008.db'}")
    try:
        _at_0007(migrated)
        upgrade_database(at_0007, FAVORITES)
        upgrade_database(at_0008, TELEGRAM)
        upgrade_database(migrated)
        before = _snapshot_of(migrated, OLD_TABLES)

        downgrade_database(migrated, TELEGRAM)
        assert _version(migrated) == [TELEGRAM]
        assert _tables(migrated) == OLD_TABLES | TELEGRAM_TABLES | {"alembic_version"}
        assert _fingerprint(migrated) == _fingerprint(at_0008)

        downgrade_database(migrated, FAVORITES)
        assert _version(migrated) == [FAVORITES]
        assert _tables(migrated) == OLD_TABLES | {"alembic_version"}
        assert _fingerprint(migrated) == _fingerprint(at_0007)
        assert _snapshot_of(migrated, OLD_TABLES) == before  # the old rows are as they were

        upgrade_database(migrated)  # and forward again: empty tables, as they began
        assert _version(migrated) == [HEAD] and _tables(migrated) == TABLES | {"alembic_version"}
    finally:
        for opened in (migrated, at_0007, at_0008):
            opened.dispose()


def test_the_two_revisions_render_as_postgresql_sql_that_only_creates_tables_and_indexes():
    up = _offline_sql("up")

    for source, target, tables, indexes in [
        (FAVORITES, TELEGRAM, ["telegram_links", "telegram_link_codes"], 2),
        (TELEGRAM, HEAD, ["message_attachments"], 2),
    ]:
        revision = up.split(f"Running upgrade {source} -> {target}")[1].split("-- Running")[0]
        statements = [re.sub(r"\s+", " ", part.strip()) for part in revision.split(";")]
        statements = [part for part in statements if part not in ("", "COMMIT")]
        created = [part.split(" (")[0] for part in statements if part.startswith("CREATE TABLE")]
        assert created == [f"CREATE TABLE {table}" for table in tables]
        assert len([part for part in statements if part.startswith("CREATE INDEX")]) == indexes
        assert statements[-1].startswith("UPDATE alembic_version SET version_num='" + target + "'")
        assert len(statements) == len(tables) + indexes + 1  # nothing else is created, altered, updated or dropped
        assert "ALTER TABLE" not in revision and "DROP" not in revision and "DELETE FROM" not in revision

    assert "BYTEA" in up and "data BYTEA NOT NULL" in up
    down = _offline_sql("down_from_head")
    uncommented = re.sub(r"^--.*$", "", down, flags=re.MULTILINE)
    assert re.findall(r"^DROP [^;]*", uncommented, re.MULTILINE) == ["DROP TABLE message_attachments"]
    down = _offline_sql("down_from_telegram")
    uncommented = re.sub(r"^--.*$", "", down, flags=re.MULTILINE)
    assert re.findall(r"^DROP [^;]*", uncommented, re.MULTILINE) == [
        "DROP TABLE telegram_link_codes",
        "DROP TABLE telegram_links",
    ]
