"""Migration 0010 (structured recipe favorites): the new table and the favorites' evolution.

Policy under test: message_recipes is added empty; favorite_recipes keeps every legacy row
(id, owner, title, content, source message, created_at) and gains only the fields and the
unique rule a structured favorite needs; every other table is untouched. The way back
refuses while a structured favorite exists, and otherwise restores the 0009 schema while
keeping every text favorite. The helpers are those of test_persistence_migrations.py.
"""

import json
import re
import uuid

import pytest
import sqlalchemy as sa

from house_helper.persistence import favorites as fav
from house_helper.persistence.base import Base
from house_helper.persistence.database import (
    create_db_engine,
    create_session_factory,
    downgrade_database,
    upgrade_database,
)
from tests.persistence_support import CREATED, new_conversation, new_message, new_user, raw_insert, stored, utc
from tests.test_persistence_migrations import (
    ATTACHMENTS,
    HEAD,
    RECIPES,
    _fingerprint,
    _offline_sql,
    _snapshot_of,
    _tables,
    _version,
)
from tests.test_persistence_schema import TABLES

OLD_COLUMNS = "id, user_id, title, content, source_message_id, created_at"
STRUCTURED_COLUMNS = (
    "id, user_id, title, content, source_message_id, created_at, kind, source_recipe_index, "
    "servings, ingredients, steps, notes, version, updated_at"
)
OTHER_TABLES = TABLES - {
    "favorite_recipes",
    "message_recipes",
    "telegram_active_conversations",
    "telegram_update_admissions",
    "shopping_lists",
    "shopping_list_items",
    "shopping_list_requests",
}

BORSH = {"name": "свёкла", "amount": "2 шт."}


@pytest.fixture
def empty_engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'recipes_migration.db'}")
    yield engine
    engine.dispose()


def _at_0009(engine) -> dict:
    """A database as 0009 left it, with two users and two legacy text favorites."""
    upgrade_database(engine, ATTACHMENTS)
    found = {}
    with create_session_factory(engine)() as db, db.begin():
        elena = new_user(db, display_name="Elena", created_at=CREATED, updated_at=CREATED)
        other = new_user(db, display_name="Other", created_at=CREATED, updated_at=CREATED)
        first = new_conversation(db, elena, created_at=CREATED, updated_at=CREATED)
        second = new_conversation(db, other, created_at=CREATED, updated_at=CREATED)
        found["elena"], found["other"] = elena.id, other.id
        found["message_one"] = new_message(
            db, first, role="assistant", content="Борщ: свёкла, капуста.", created_at=CREATED
        ).id
        found["message_two"] = new_message(
            db, second, role="assistant", content="Уха: рыба.", created_at=CREATED
        ).id
        found["first"] = uuid.uuid4()
        found["second"] = uuid.uuid4()
        raw_insert(
            db,
            "favorite_recipes",
            id=found["first"].hex,
            user_id=elena.id.hex,
            title="Борщ",
            content="Борщ: свёкла, капуста.",
            source_message_id=found["message_one"],
            created_at=stored(CREATED),
        )
        raw_insert(
            db,
            "favorite_recipes",
            id=found["second"].hex,
            user_id=other.id.hex,
            title="Уха",
            content="Уха: рыба.",
            source_message_id=found["message_two"],
            created_at=stored(utc(2026, 10, 16, 13)),
        )
    return found


def _favorite_rows(engine, columns=OLD_COLUMNS):
    with engine.connect() as connection:
        return connection.exec_driver_sql(
            f"SELECT {columns} FROM favorite_recipes ORDER BY id"
        ).all()


def _new_columns(engine):
    with engine.connect() as connection:
        return {
            row[0]: row[1:]
            for row in connection.exec_driver_sql(
                "SELECT id, kind, source_recipe_index, servings, ingredients, steps, notes, "
                "version, updated_at, created_at FROM favorite_recipes ORDER BY id"
            ).all()
        }


# --- upgrade ----------------------------------------------------------------------------------


def test_the_migration_adds_the_recipes_table_and_keeps_every_legacy_favorite(empty_engine):
    found = _at_0009(empty_engine)
    old_rows = _favorite_rows(empty_engine)
    others_before = _snapshot_of(empty_engine, OTHER_TABLES)
    assert _version(empty_engine) == [ATTACHMENTS]

    upgrade_database(empty_engine)

    assert _version(empty_engine) == [HEAD]
    assert _tables(empty_engine) == TABLES | {"alembic_version"}
    assert _favorite_rows(empty_engine) == old_rows  # id, owner, title, text, source, created_at
    assert _snapshot_of(empty_engine, OTHER_TABLES) == others_before
    with empty_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM message_recipes").scalar() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]

    new = _new_columns(empty_engine)
    for favorite_id in (found["first"].hex, found["second"].hex):
        kind, index, servings, ingredients, steps, notes, version, updated_at, created_at = new[favorite_id]
        assert (kind, index, version) == ("text", -1, 1)
        assert (servings, ingredients, steps, notes) == (None, None, None, None)  # nothing invented
        assert updated_at == created_at  # the row's own creation time, not the migration's

    with create_session_factory(empty_engine)() as db:
        detail = fav.get_favorite(db, found["elena"], found["first"])
        assert detail.kind == "text" and detail.version == 1
        assert detail.text == "Борщ: свёкла, капуста."
        assert [item.id for item in fav.list_favorites(db, found["elena"])] == [found["first"]]


def test_the_migrated_and_modeled_schemas_describe_the_same_new_tables(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    modeled = create_db_engine(f"sqlite:///{tmp_path / 'modeled.db'}")
    try:
        upgrade_database(migrated)
        Base.metadata.create_all(modeled)
        a, b = _fingerprint(migrated), _fingerprint(modeled)

        for table in ("favorite_recipes", "message_recipes", "messages"):
            assert a[table] == b[table], table
        # The protocol's content fingerprint is not a persistent key and has no column.
        assert "recipe_id" not in a["message_recipes"]["columns"]
        assert "recipe_id" not in a["favorite_recipes"]["columns"]
        assert ("uq_messages_id_role", ("id", "role"), True) in a["messages"]["indexes"]
        assert a["message_recipes"]["fks"] == [
            (
                "fk_message_recipes_message",
                ("message_id", "message_role"),
                "messages",
                ("id", "role"),
                (("ondelete", "CASCADE"),),
            )
        ]
    finally:
        migrated.dispose()
        modeled.dispose()


def test_one_message_may_yield_several_structured_favorites_but_one_position_only(empty_engine):
    found = _at_0009(empty_engine)
    upgrade_database(empty_engine)
    with create_session_factory(empty_engine)() as db, db.begin():
        for position, title in ((0, "Борщ"), (1, "Щи")):
            raw_insert(
                db,
                "message_recipes",
                message_id=found["message_one"],
                message_role="assistant",
                position=position,
                title=title,
                servings=None,
                ingredients=json.dumps([BORSH], ensure_ascii=False),
                steps=json.dumps(["Готовьте."], ensure_ascii=False),
                created_at=stored(CREATED),
            )
        for position in (0, 1):
            raw_insert(
                db,
                "favorite_recipes",
                id=uuid.uuid4().hex,
                user_id=found["elena"].hex,
                title=f"Рецепт {position}",
                content=f"Рецепт {position}",
                source_message_id=found["message_one"],
                created_at=stored(CREATED),
                kind="structured",
                source_recipe_index=position,
                servings=None,
                ingredients=json.dumps([BORSH], ensure_ascii=False),
                steps=json.dumps(["Готовьте."], ensure_ascii=False),
                version=1,
                updated_at=stored(CREATED),
            )

    assert len(_favorite_rows(empty_engine, STRUCTURED_COLUMNS)) == 4  # two legacy, two structured

    with pytest.raises(sa.exc.IntegrityError, match="source_recipe_index"):
        with empty_engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO favorite_recipes (id, user_id, title, content, source_message_id, "
                "created_at, kind, source_recipe_index, ingredients, steps, version, updated_at) "
                "VALUES (?, ?, 'x', 'x', ?, ?, 'structured', 0, '[]', '[]', 1, ?)",
                (uuid.uuid4().hex, found["elena"].hex, found["message_one"], stored(CREATED), stored(CREATED)),
            )
    with pytest.raises(sa.exc.IntegrityError, match="source_message_id"):
        with empty_engine.begin() as connection:  # the legacy unique rule still holds for text
            connection.exec_driver_sql(
                "INSERT INTO favorite_recipes (id, user_id, title, content, source_message_id, created_at) "
                "VALUES (?, ?, 'again', 'again', ?, ?)",
                (uuid.uuid4().hex, found["elena"].hex, found["message_one"], stored(CREATED)),
            )


def test_the_recipes_are_deleted_with_their_message_and_a_favorite_is_not(empty_engine):
    found = _at_0009(empty_engine)
    upgrade_database(empty_engine)
    with empty_engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO message_recipes (message_id, message_role, position, title, ingredients, steps, created_at) "
            "VALUES (?, 'assistant', 0, 'Борщ', ?, ?, ?)",
            (found["message_one"], json.dumps([BORSH], ensure_ascii=False), json.dumps(["s"]), stored(CREATED)),
        )
        connection.exec_driver_sql(
            "INSERT INTO favorite_recipes (id, user_id, title, content, source_message_id, created_at, "
            "kind, source_recipe_index, ingredients, steps, version, updated_at) "
            "VALUES (?, ?, 'Борщ', 'Борщ', ?, ?, 'structured', 0, ?, ?, 1, ?)",
            (
                uuid.uuid4().hex,
                found["elena"].hex,
                found["message_one"],
                stored(CREATED),
                json.dumps([BORSH], ensure_ascii=False),
                json.dumps(["s"]),
                stored(CREATED),
            ),
        )

    with empty_engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM messages WHERE id = ?", (found["message_one"],))

    with empty_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM message_recipes").scalar() == 0
        assert connection.exec_driver_sql(
            "SELECT count(*) FROM favorite_recipes WHERE kind = 'structured'"
        ).scalar() == 1
        assert connection.exec_driver_sql("SELECT count(*) FROM favorite_recipes").scalar() == 3


# --- downgrade --------------------------------------------------------------------------------


def _structured_setup(engine, found):
    with create_session_factory(engine)() as db, db.begin():
        raw_insert(
            db,
            "message_recipes",
            message_id=found["message_one"],
            message_role="assistant",
            position=0,
            title="Борщ",
            servings=None,
            ingredients=json.dumps([BORSH], ensure_ascii=False),
            steps=json.dumps(["Готовьте."], ensure_ascii=False),
            created_at=stored(CREATED),
        )
        raw_insert(
            db,
            "favorite_recipes",
            id=uuid.uuid4().hex,
            user_id=found["elena"].hex,
            title="Борщ",
            content="Борщ",
            source_message_id=found["message_one"],
            created_at=stored(CREATED),
            kind="structured",
            source_recipe_index=0,
            servings=None,
            ingredients=json.dumps([BORSH], ensure_ascii=False),
            steps=json.dumps(["Готовьте."], ensure_ascii=False),
            version=1,
            updated_at=stored(CREATED),
        )


def test_downgrading_refuses_while_a_structured_favorite_exists_and_changes_nothing(empty_engine):
    found = _at_0009(empty_engine)
    upgrade_database(empty_engine)
    _structured_setup(empty_engine, found)
    before = _favorite_rows(empty_engine, STRUCTURED_COLUMNS)

    with pytest.raises(RuntimeError, match="structured favorite"):
        downgrade_database(empty_engine, ATTACHMENTS)

    assert _version(empty_engine) == [HEAD]
    assert _tables(empty_engine) == TABLES | {"alembic_version"}
    assert _favorite_rows(empty_engine, STRUCTURED_COLUMNS) == before
    with empty_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM message_recipes").scalar() == 1


def test_the_way_back_restores_0009_and_keeps_every_text_favorite(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    pristine = create_db_engine(f"sqlite:///{tmp_path / 'pristine.db'}")
    try:
        found = _at_0009(migrated)
        upgrade_database(pristine, ATTACHMENTS)
        upgrade_database(migrated)
        _structured_setup(migrated, found)
        with migrated.begin() as connection:  # the operator removes the structured data first
            connection.exec_driver_sql("DELETE FROM favorite_recipes WHERE kind = 'structured'")
            connection.exec_driver_sql("DELETE FROM message_recipes")
        old_rows = _favorite_rows(migrated)
        others_before = _snapshot_of(migrated, OTHER_TABLES)

        downgrade_database(migrated, ATTACHMENTS)

        assert _version(migrated) == [ATTACHMENTS]
        assert _tables(migrated) == TABLES - {"message_recipes", "telegram_active_conversations", "telegram_update_admissions", "shopping_lists", "shopping_list_items", "shopping_list_requests"} | {"alembic_version"}
        assert _fingerprint(migrated) == _fingerprint(pristine)  # the 0009 schema, exactly
        assert _favorite_rows(migrated) == old_rows
        assert _snapshot_of(migrated, OTHER_TABLES) == others_before
        with migrated.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
        with pytest.raises(sa.exc.IntegrityError, match="source_message_id"):  # old unique is back
            with migrated.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO favorite_recipes (id, user_id, title, content, source_message_id, created_at) "
                    "VALUES (?, ?, 'x', 'x', ?, ?)",
                    (uuid.uuid4().hex, found["elena"].hex, found["message_one"], stored(CREATED)),
                )

        upgrade_database(migrated)  # and forward again: an empty recipes table, text rows as text
        assert _version(migrated) == [HEAD]
        assert _tables(migrated) == TABLES | {"alembic_version"}
        with migrated.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM message_recipes").scalar() == 0
        assert all(row[0] == "text" for row in _new_columns(migrated).values())
    finally:
        migrated.dispose()
        pristine.dispose()


# --- PostgreSQL, as far as it can be checked without a server ---------------------------------


def test_the_recipes_revision_renders_as_postgresql_sql():
    up, down = _offline_sql("up"), _offline_sql("down_from_recipes")
    revision = up.split(f"Running upgrade {ATTACHMENTS} -> {RECIPES}")[1].split("-- Running")[0]

    statements = [re.sub(r"\s+", " ", part.strip()) for part in revision.split(";")]
    statements = [part for part in statements if part not in ("", "COMMIT")]
    assert statements[0] == "CREATE UNIQUE INDEX uq_messages_id_role ON messages (id, role)"
    assert statements[1].startswith("CREATE TABLE message_recipes (")
    assert "message_role VARCHAR(16) DEFAULT 'assistant' NOT NULL" in statements[1]
    assert "ingredients JSONB NOT NULL" in statements[1] and "steps JSONB NOT NULL" in statements[1]
    assert (
        "FOREIGN KEY(message_id, message_role) REFERENCES messages (id, role) ON DELETE CASCADE"
        in statements[1]
    )
    assert "PRIMARY KEY (message_id, position)" in statements[1]
    assert "CHECK (message_role = 'assistant')" in statements[1]
    assert "CHECK (position BETWEEN 0 AND 4)" in statements[1]

    added = [part for part in statements if part.startswith("ALTER TABLE favorite_recipes ADD COLUMN")]
    assert added == [
        "ALTER TABLE favorite_recipes ADD COLUMN kind VARCHAR(16) DEFAULT 'text' NOT NULL",
        "ALTER TABLE favorite_recipes ADD COLUMN source_recipe_index INTEGER DEFAULT -1 NOT NULL",
        "ALTER TABLE favorite_recipes ADD COLUMN servings VARCHAR(100)",
        "ALTER TABLE favorite_recipes ADD COLUMN ingredients JSONB",
        "ALTER TABLE favorite_recipes ADD COLUMN steps JSONB",
        "ALTER TABLE favorite_recipes ADD COLUMN notes TEXT",
        "ALTER TABLE favorite_recipes ADD COLUMN version INTEGER DEFAULT 1 NOT NULL",
        "ALTER TABLE favorite_recipes ADD COLUMN updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL",
    ]
    assert "ALTER TABLE favorite_recipes DROP CONSTRAINT uq_favorite_recipes_user_id_source_message_id" in statements
    assert (
        "ALTER TABLE favorite_recipes ADD CONSTRAINT uq_favorite_recipes_user_message_recipe "
        "UNIQUE (user_id, source_message_id, source_recipe_index)"
    ) in statements
    assert "UPDATE favorite_recipes SET updated_at=favorite_recipes.created_at" in statements
    assert statements[-1].startswith("UPDATE alembic_version SET version_num='" + RECIPES + "'")
    assert "DROP TABLE" not in revision and "DELETE FROM" not in revision and "ALTER COLUMN" not in revision

    uncommented = re.sub(r"^--.*$", "", down, flags=re.MULTILINE)
    undo = [re.sub(r"\s+", " ", part.strip()) for part in uncommented.split(";")]
    undo = [part for part in undo if part.startswith(("DROP", "ALTER TABLE"))]
    assert undo == [
        "DROP TABLE message_recipes",
        "DROP INDEX uq_messages_id_role",
        "ALTER TABLE favorite_recipes DROP CONSTRAINT ck_favorite_recipes_kind_valid",
        "ALTER TABLE favorite_recipes DROP CONSTRAINT ck_favorite_recipes_source_recipe_index_valid",
        "ALTER TABLE favorite_recipes DROP CONSTRAINT ck_favorite_recipes_kind_consistent",
        "ALTER TABLE favorite_recipes DROP CONSTRAINT ck_favorite_recipes_version_valid",
        "ALTER TABLE favorite_recipes DROP CONSTRAINT ck_favorite_recipes_servings_max_length",
        "ALTER TABLE favorite_recipes DROP CONSTRAINT ck_favorite_recipes_notes_max_length",
        "ALTER TABLE favorite_recipes DROP CONSTRAINT uq_favorite_recipes_user_message_recipe",
        "ALTER TABLE favorite_recipes ADD CONSTRAINT uq_favorite_recipes_user_id_source_message_id "
        "UNIQUE (user_id, source_message_id)",
        "ALTER TABLE favorite_recipes DROP COLUMN updated_at",
        "ALTER TABLE favorite_recipes DROP COLUMN version",
        "ALTER TABLE favorite_recipes DROP COLUMN notes",
        "ALTER TABLE favorite_recipes DROP COLUMN steps",
        "ALTER TABLE favorite_recipes DROP COLUMN ingredients",
        "ALTER TABLE favorite_recipes DROP COLUMN servings",
        "ALTER TABLE favorite_recipes DROP COLUMN source_recipe_index",
        "ALTER TABLE favorite_recipes DROP COLUMN kind",
    ]
