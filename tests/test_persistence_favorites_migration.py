"""Migration 0007 (favorite recipes): one new, empty table, and nothing else changes.

The helpers are those of test_persistence_migrations.py (the databases that earlier
revisions left, the fingerprint of a schema, the offline PostgreSQL SQL); this file only
adds what is particular to 0007. Policy under test: the table is added empty and nothing
that exists is touched (every row of every old table stays, nobody is signed out), the
table has no link to a conversation or a message (so the history's expiry and purge cannot
reach it), and the way back drops that table and nothing else.
"""

import re
import uuid

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
from tests.persistence_support import CREATED, raw_insert, stored
from tests.test_persistence_migrations import (
    HEAD,
    LATER,
    OIDC_STATE,
    _fingerprint,
    _offline_sql,
    _r4c_database,
    _snapshot_of,
    _tables,
    _version,
)
from tests.test_persistence_schema import TABLES

TABLES_BEFORE_0007 = TABLES - {"favorite_recipes"}


@pytest.fixture
def empty_engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'favorites_migration.db'}")
    yield engine
    engine.dispose()


def _at_0006(engine) -> dict:
    found = _r4c_database(engine)
    upgrade_database(engine, OIDC_STATE)
    return found


def test_the_favorites_migration_adds_one_empty_table_and_changes_no_row(empty_engine):
    found = _at_0006(empty_engine)
    before = _snapshot_of(empty_engine, TABLES_BEFORE_0007)
    assert _version(empty_engine) == [OIDC_STATE]

    upgrade_database(empty_engine)

    assert _version(empty_engine) == [HEAD]
    assert _tables(empty_engine) == TABLES | {"alembic_version"}
    assert _snapshot_of(empty_engine, TABLES_BEFORE_0007) == before  # every row of every old table
    with empty_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM favorite_recipes").scalar() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
    with create_session_factory(empty_engine)() as db:
        assert authenticate(db, found["telegram"].token, now=LATER) is not None  # nobody signed out


def test_the_favorites_table_has_the_keys_and_checks_the_models_say(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    modeled = create_db_engine(f"sqlite:///{tmp_path / 'modeled.db'}")
    try:
        upgrade_database(migrated)
        Base.metadata.create_all(modeled)

        for engine in (migrated, modeled):
            table = _fingerprint(engine)["favorite_recipes"]
            assert table["columns"]["source_message_id"][0] == "INTEGER"  # as messages.id on SQLite
            assert [column for column in table["columns"]] == [
                "id", "user_id", "title", "content", "source_message_id", "created_at",
            ]
            assert table["uniques"] == [
                ("uq_favorite_recipes_user_id_source_message_id", ("user_id", "source_message_id"))
            ]
            assert table["indexes"] == [
                ("ix_favorite_recipes_user_id_created_at", ("user_id", "created_at"), False)
            ]
            # The independence rule: the one link is to the owner, and it cascades. Nothing points
            # at a conversation or a message, so nothing that deletes them can reach a favorite.
            assert table["fks"] == [
                ("fk_favorite_recipes_user_id_users", ("user_id",), "users", ("id",), (("ondelete", "CASCADE"),))
            ]
            assert [name for name, _ in table["checks"]] == [
                "ck_favorite_recipes_content_not_empty",
                "ck_favorite_recipes_title_max_length",
                "ck_favorite_recipes_title_not_empty",
            ]
    finally:
        migrated.dispose()
        modeled.dispose()


def test_no_other_table_refers_to_the_favorites(empty_engine):
    upgrade_database(empty_engine)

    inspector = sa.inspect(empty_engine)
    referring = [
        table
        for table in inspector.get_table_names()
        if any(key["referred_table"] == "favorite_recipes" for key in inspector.get_foreign_keys(table))
    ]

    assert referring == []


def test_downgrading_the_favorites_drops_only_that_table(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    pristine = create_db_engine(f"sqlite:///{tmp_path / 'pristine.db'}")
    try:
        found = _at_0006(migrated)
        upgrade_database(pristine, OIDC_STATE)
        upgrade_database(migrated)
        with create_session_factory(migrated)() as db, db.begin():  # a favorite exists
            raw_insert(
                db,
                "favorite_recipes",
                id=uuid.uuid4().hex,
                user_id=found["elena"].hex,
                title="Борщ",
                content="Борщ: свёкла, капуста.",
                source_message_id=1,
                created_at=stored(CREATED),
            )
        before = _snapshot_of(migrated, TABLES_BEFORE_0007)

        downgrade_database(migrated, OIDC_STATE)

        assert _version(migrated) == [OIDC_STATE]
        assert _tables(migrated) == TABLES_BEFORE_0007 | {"alembic_version"}
        assert _fingerprint(migrated) == _fingerprint(pristine)  # the 0006 schema, exactly
        assert _snapshot_of(migrated, TABLES_BEFORE_0007) == before
        with create_session_factory(migrated)() as db:
            assert authenticate(db, found["telegram"].token, now=LATER) is not None

        upgrade_database(migrated)  # and forward again: an empty table, as it began
        with migrated.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM favorite_recipes").scalar() == 0
    finally:
        migrated.dispose()
        pristine.dispose()


def test_the_favorites_revision_renders_as_one_table_and_one_index_in_postgresql_sql():
    up, down = _offline_sql("up"), _offline_sql("down_from_head")
    revision = up.split(f"Running upgrade {OIDC_STATE} -> {HEAD}")[1]

    statements = [re.sub(r"\s+", " ", part.strip()) for part in revision.split(";")]
    statements = [part for part in statements if part not in ("", "COMMIT")]
    assert statements[0].startswith("CREATE TABLE favorite_recipes (")
    assert "source_message_id BIGINT NOT NULL" in statements[0]
    assert "content TEXT NOT NULL" in statements[0]
    assert "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE" in statements[0]
    assert "UNIQUE (user_id, source_message_id)" in statements[0]
    assert "CHECK (length(title) <= 200)" in statements[0]
    assert "CHECK (length(title) >= 1)" in statements[0]
    assert "CHECK (length(content) >= 1)" in statements[0]
    assert "conversations" not in statements[0] and "messages" not in statements[0]
    assert statements[1] == (
        "CREATE INDEX ix_favorite_recipes_user_id_created_at ON favorite_recipes (user_id, created_at)"
    )
    assert statements[2].startswith("UPDATE alembic_version SET version_num='" + HEAD + "'")
    assert len(statements) == 3  # nothing else is created, altered, updated or dropped
    assert "ALTER TABLE" not in revision and "DROP" not in revision
    assert "DELETE FROM" not in revision  # (ON DELETE CASCADE is the key's rule, not a data step)

    uncommented = re.sub(r"^--.*$", "", down, flags=re.MULTILINE)
    assert re.findall(r"^DROP [^;]*", uncommented, re.MULTILINE) == ["DROP TABLE favorite_recipes"]
