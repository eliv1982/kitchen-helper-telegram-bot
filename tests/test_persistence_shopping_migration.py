"""Migration 0012: the shopping tables on a database that already holds rows.

The 0011 schema is built for real and given ordinary rows in a representative set of
tables: users, conversations, messages and message_recipes. Every other table of that
revision exists but is empty in this fixture; the snapshot comparison checks both that
the seeded rows survive byte for byte and that the empty tables stay empty. The three
new tables must start empty, and the migrated schema must be exactly the models' one.
The way back drops all three and restores the 0011 schema; every other row stays.

The backup test at the end is the deployment's own whole-database snapshot: the lists,
items and request keys are ordinary tables in the same file, so the SQLite online backup
must carry them without any new step.
"""

import sqlite3
import uuid
from datetime import timedelta

import pytest

from house_helper.core.recipes import Ingredient, Recipe
from house_helper.persistence import message_recipes as mr
from house_helper.persistence.database import (
    create_db_engine,
    create_session_factory,
    downgrade_database,
    upgrade_database,
)
from house_helper.persistence.models import (
    ShoppingList,
    ShoppingListItem,
    ShoppingListRequest,
)
from tests.deploy_support import dbtool
from tests.persistence_support import (
    digest,
    new_conversation,
    new_message,
    new_user,
    utc,
)
from tests.test_persistence_migrations import (
    SHOPPING,
    TELEGRAM_ACTIVE,
    _fingerprint,
    _offline_sql,
    _snapshot_of,
    _tables,
    _version,
)
from tests.test_persistence_schema import TABLES

NOW = utc(2026, 11, 8, 12)
SHOPPING_TABLES = {"shopping_lists", "shopping_list_items", "shopping_list_requests"}

# The tables _at_0011 gives rows to. The rest of the 0011 schema exists but stays empty,
# and the snapshot comparison verifies exactly that; the test below asserts these are
# non-empty so the preservation check cannot pass over an empty fixture.
SEEDED_TABLES = {"users", "conversations", "messages", "message_recipes"}


def _at_0011(engine):
    """A 0011 database with an ordinary row in each seeded table, and a recipe."""
    upgrade_database(engine, TELEGRAM_ACTIVE)
    factory = create_session_factory(engine)
    with factory() as db, db.begin():
        user = new_user(db, display_name="Елена")
        conversation = new_conversation(db, user, created_at=NOW, updated_at=NOW)
        answer = new_message(
            db,
            conversation,
            role="assistant",
            content="Вот рецепт.",
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
        mr.record_validated_recipes(
            db,
            answer.id,
            [
                Recipe(
                    index=0,
                    title="Борщ",
                    servings="4 порции",
                    ingredients=(Ingredient("свёкла", "2 шт."), Ingredient("капуста", "300 г")),
                    steps=("Сварите.",),
                )
            ],
            now=NOW,
        )
        return {"user": user.id, "conversation": conversation.id, "answer": answer.id}


@pytest.fixture
def engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'shopping_migration.db'}")
    yield engine
    engine.dispose()


def test_0012_adds_three_empty_tables_and_changes_no_other_row(engine):
    found = _at_0011(engine)
    before = _snapshot_of(engine, TABLES - SHOPPING_TABLES)
    assert _version(engine) == [TELEGRAM_ACTIVE]
    assert {table for table, rows in before.items() if rows} == SEEDED_TABLES

    upgrade_database(engine, SHOPPING)

    assert _version(engine) == [SHOPPING]
    assert _tables(engine) == TABLES | {"alembic_version"}
    assert _snapshot_of(engine, TABLES - SHOPPING_TABLES) == before  # every old row
    with engine.connect() as connection:
        for table in SHOPPING_TABLES:
            assert connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
    assert found["answer"]  # the recipe the list will be made from is still there


def test_the_migrated_schema_is_the_models_schema_exactly(engine, tmp_path):
    upgrade_database(engine)
    modeled = create_db_engine(f"sqlite:///{tmp_path / 'modeled.db'}")
    try:
        from house_helper.persistence.base import Base

        Base.metadata.create_all(modeled)
        assert _fingerprint(engine) == _fingerprint(modeled)
    finally:
        modeled.dispose()


def test_the_tables_have_the_keys_and_rules_the_design_says(engine):
    upgrade_database(engine)
    fingerprint = _fingerprint(engine)
    lists, items = fingerprint["shopping_lists"], fingerprint["shopping_list_items"]

    assert list(lists["columns"]) == [
        "id",
        "user_id",
        "title",
        "status",
        "source_kind",
        "source_message_id",
        "source_recipe_index",
        "source_favorite_id",
        "source_favorite_version",
        "idempotency_key",
        "request_fingerprint",
        "created_at",
        "updated_at",
    ]
    assert lists["pk"]["name"] == "pk_shopping_lists"
    assert lists["uniques"] == [
        (
            "uq_shopping_lists_user_id_idempotency_key",
            ("user_id", "idempotency_key"),
        )
    ]
    assert lists["fks"] == [
        (
            "fk_shopping_lists_user_id_users",
            ("user_id",),
            "users",
            ("id",),
            (("ondelete", "CASCADE"),),
        )
    ]
    assert [name for name, _ in lists["checks"]] == sorted(
        [
            "ck_shopping_lists_title_max_length",
            "ck_shopping_lists_title_not_empty",
            "ck_shopping_lists_status_valid",
            "ck_shopping_lists_source_kind_valid",
            "ck_shopping_lists_source_consistent",
            "ck_shopping_lists_source_message_id_positive",
            "ck_shopping_lists_source_recipe_index_valid",
            "ck_shopping_lists_source_favorite_version_valid",
            "ck_shopping_lists_idempotency_key_length",
            "ck_shopping_lists_idempotency_key_printable_ascii",
            "ck_shopping_lists_fingerprint_length",
        ]
    )
    assert ("ix_shopping_lists_user_id_created_at", ("user_id", "created_at"), False) in lists[
        "indexes"
    ]

    assert list(items["columns"]) == [
        "id",
        "list_id",
        "position",
        "name",
        "amount",
        "state",
        "created_at",
        "updated_at",
    ]
    assert items["pk"]["name"] == "pk_shopping_list_items"
    assert items["uniques"] == [
        ("uq_shopping_list_items_list_id_position", ("list_id", "position"))
    ]
    assert items["fks"] == [
        (
            "fk_shopping_list_items_list_id_shopping_lists",
            ("list_id",),
            "shopping_lists",
            ("id",),
            (("ondelete", "CASCADE"),),
        )
    ]
    assert [name for name, _ in items["checks"]] == sorted(
        [
            "ck_shopping_list_items_position_valid",
            "ck_shopping_list_items_name_max_length",
            "ck_shopping_list_items_name_not_empty",
            "ck_shopping_list_items_amount_max_length",
            "ck_shopping_list_items_state_valid",
        ]
    )

    requests = fingerprint["shopping_list_requests"]
    assert list(requests["columns"]) == [
        "id",
        "user_id",
        "idempotency_key",
        "request_fingerprint",
        "list_id",
        "created_at",
    ]
    assert requests["pk"]["name"] == "pk_shopping_list_requests"
    assert requests["uniques"] == [
        (
            "uq_shopping_list_requests_user_id_idempotency_key",
            ("user_id", "idempotency_key"),
        )
    ]
    assert requests["fks"] == [
        (
            "fk_shopping_list_requests_list_id_shopping_lists",
            ("list_id",),
            "shopping_lists",
            ("id",),
            (("ondelete", "SET NULL"),),
        ),
        (
            "fk_shopping_list_requests_user_id_users",
            ("user_id",),
            "users",
            ("id",),
            (("ondelete", "CASCADE"),),
        ),
    ]
    assert [name for name, _ in requests["checks"]] == sorted(
        [
            "ck_shopping_list_requests_idempotency_key_length",
            "ck_shopping_list_requests_idempotency_key_printable_ascii",
            "ck_shopping_list_requests_fingerprint_length",
        ]
    )
    assert ("ix_shopping_list_requests_list_id", ("list_id",), False) in requests[
        "indexes"
    ]


def test_the_way_back_drops_all_three_tables_and_every_saved_list(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    pristine = create_db_engine(f"sqlite:///{tmp_path / 'pristine.db'}")
    try:
        found = _at_0011(migrated)
        upgrade_database(migrated)
        upgrade_database(pristine, TELEGRAM_ACTIVE)
        before = _snapshot_of(migrated, TABLES - SHOPPING_TABLES)

        with create_session_factory(migrated)() as db, db.begin():
            list_id = uuid.uuid4()
            db.add(
                ShoppingList(
                    id=list_id,
                    user_id=found["user"],
                    title="Борщ",
                    status="active",
                    source_kind="message",
                    source_message_id=found["answer"],
                    source_recipe_index=0,
                    idempotency_key="list-1",
                    request_fingerprint=digest("list-1"),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.flush()
            db.add(
                ShoppingListItem(
                    list_id=list_id,
                    position=0,
                    name="свёкла",
                    amount="2 шт.",
                    state="at_home",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.add(
                ShoppingListRequest(
                    user_id=found["user"],
                    idempotency_key="list-1",
                    request_fingerprint=digest("list-1"),
                    list_id=list_id,
                    created_at=NOW,
                )
            )

        downgrade_database(migrated, TELEGRAM_ACTIVE)

        assert _version(migrated) == [TELEGRAM_ACTIVE]
        assert _tables(migrated) == (TABLES - SHOPPING_TABLES) | {"alembic_version"}
        assert _fingerprint(migrated) == _fingerprint(pristine)  # the 0011 schema, exactly
        assert _snapshot_of(migrated, TABLES - SHOPPING_TABLES) == before
        with migrated.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]

        upgrade_database(migrated)  # forward again: empty tables, as they began
        with migrated.connect() as connection:
            for table in SHOPPING_TABLES:
                assert connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar() == 0
    finally:
        migrated.dispose()
        pristine.dispose()


def test_the_revision_renders_as_three_create_tables_and_two_indexes_for_postgresql():
    sql = _offline_sql("up")
    revision = sql.split(f"Running upgrade {TELEGRAM_ACTIVE} -> 0012_shopping_lists")[1].split(
        "-- Running"
    )[0]

    statements = [part.strip() for part in revision.split(";") if part.strip() not in ("", "COMMIT")]
    created = [part.split(" (")[0] for part in statements if part.startswith("CREATE TABLE")]
    assert created == [
        "CREATE TABLE shopping_lists",
        "CREATE TABLE shopping_list_items",
        "CREATE TABLE shopping_list_requests",
    ]
    indexes = [part for part in statements if part.startswith("CREATE INDEX")]
    assert indexes == [
        "CREATE INDEX ix_shopping_lists_user_id_created_at ON shopping_lists (user_id, created_at)",
        "CREATE INDEX ix_shopping_list_requests_list_id ON shopping_list_requests (list_id)",
    ]
    assert statements[-1].startswith(
        "UPDATE alembic_version SET version_num='0012_shopping_lists'"
    )
    assert "ALTER TABLE" not in revision and "DROP" not in revision and "DELETE FROM" not in revision
    assert "~ '^[!-~]+$'" in revision  # the printable-ASCII rule, PostgreSQL's spelling
    assert "GLOB" not in revision and "typeof(" not in revision
    assert "ON DELETE SET NULL" in revision  # the tombstone transition


# --- the deployment's backup ---------------------------------------------------------------


def test_the_whole_database_backup_carries_the_lists_and_their_items(tmp_path):
    live = tmp_path / "live.db"
    engine = create_db_engine(f"sqlite:///{live}")
    try:
        upgrade_database(engine)
        with create_session_factory(engine)() as db, db.begin():
            user = new_user(db)
            list_id = uuid.uuid4()
            db.add(
                ShoppingList(
                    id=list_id,
                    user_id=user.id,
                    title="Борщ",
                    status="active",
                    source_kind="message",
                    source_message_id=1,
                    source_recipe_index=0,
                    idempotency_key="list-1",
                    request_fingerprint=digest("list-1"),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.flush()
            db.add(
                ShoppingListItem(
                    list_id=list_id,
                    position=0,
                    name="свёкла",
                    amount="2 шт.",
                    state="at_home",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            db.add(
                ShoppingListRequest(
                    user_id=user.id,
                    idempotency_key="list-1",
                    request_fingerprint=digest("list-1"),
                    list_id=list_id,
                    created_at=NOW,
                )
            )
    finally:
        engine.dispose()

    copy = tmp_path / "backup.db"
    dbtool.snapshot_database(live, copy)

    connection = sqlite3.connect(copy)
    try:
        assert connection.execute("SELECT title FROM shopping_lists").fetchall() == [("Борщ",)]
        assert connection.execute(
            "SELECT name, amount, state FROM shopping_list_items"
        ).fetchall() == [("свёкла", "2 шт.", "at_home")]
        assert connection.execute(
            "SELECT idempotency_key, request_fingerprint, list_id FROM shopping_list_requests"
        ).fetchall() == [("list-1", digest("list-1"), list_id.hex)]
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        connection.close()
