"""Migration 0014: personal household measures on a database that already holds rows.

The 0013 schema is built for real and given ordinary rows: two users (one with a display
name, one without), their identities and a live session, a conversation with a message, a
text favorite, and a shopping list with its item and its creation key. The upgrade adds
three nullable integer columns to users; every other table's rows must stay byte for byte,
and every user's three values must start as NULL -- no account receives a preconfigured,
seeded or migrated value, which is the whole of AC52's first sentence.

Each column carries its own named CHECK with the approved inclusive range. The tests
prove that the rule is the database's (a bad value is refused, both bounds are accepted)
and that the three are independent (no cross-field rule).

The migration must not rebuild users on SQLite, for the reason 0013 explains: users is the
root of the ownership foreign keys, and a batch rebuild would cascade-delete every
user-owned table. The fixture seeds those tables precisely so such an accident is visible.
The way back drops the three columns and restores the 0013 schema exactly; the capacities
are the only thing lost.
"""

import re
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.persistence.database import (
    create_db_engine,
    create_session_factory,
    downgrade_database,
    upgrade_database,
)
from house_helper.persistence.models import User
from tests.persistence_support import digest, new_user, stored, utc
from tests.test_persistence_migrations import (
    MEASURES,
    PROFILE,
    _fingerprint,
    _offline_sql,
    _tables,
    _version,
)
from tests.test_persistence_schema import TABLES

START = utc(2026, 11, 11, 9)
EXPIRES = START + timedelta(days=180)
COLUMNS = ("measure_glass_ml", "measure_teaspoon_ml", "measure_tablespoon_ml")
RANGES = {
    "measure_glass_ml": (50, 1000),
    "measure_teaspoon_ml": (1, 20),
    "measure_tablespoon_ml": (5, 50),
}


def _rows(engine, table: str, omit: tuple[str, ...] = ()) -> list:
    with engine.connect() as connection:
        result = connection.exec_driver_sql(f"SELECT * FROM {table}")
        keep = [i for i, name in enumerate(result.keys()) if name not in omit]
        return sorted(([row[i] for i in keep] for row in result.all()), key=repr)


def _snapshot(engine) -> dict[str, list]:
    return {table: _rows(engine, table) for table in sorted(TABLES)}


def _insert(connection, table: str, **values) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.exec_driver_sql(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def _database_at_0013(engine) -> dict:
    """A 0013 database with rows in the tables a measure change can touch and in the
    tables a users rebuild would cascade. Returns the ids the tests look for.

    The rows are written with plain SQL (the models now have the three measure columns,
    which the 0013 table does not have yet), so the fixture describes the older schema.
    """
    upgrade_database(engine, PROFILE)
    found = {
        "elena": uuid.uuid4(),
        "unnamed": uuid.uuid4(),
        "entry": uuid.uuid4(),
        "link": uuid.uuid4(),
        "session": uuid.uuid4(),
        "conversation": uuid.uuid4(),
        "favorite": uuid.uuid4(),
        "list": uuid.uuid4(),
        "item": uuid.uuid4(),
        "request": uuid.uuid4(),
    }
    with engine.begin() as connection:
        for name, display_name in (("elena", "Елена"), ("unnamed", None)):
            _insert(
                connection,
                "users",
                id=found[name].hex,
                display_name=display_name,
                created_at=stored(START),
                updated_at=stored(START),
                disabled_at=None,
            )
        _insert(
            connection,
            "allowlist_entries",
            id=found["entry"].hex,
            user_id=found["elena"].hex,
            provider="telegram",
            subject="1234123412341234123",
            note=None,
            created_at=stored(START),
            revoked_at=None,
        )
        _insert(
            connection,
            "identity_links",
            id=found["link"].hex,
            user_id=found["elena"].hex,
            provider="telegram",
            subject="1234123412341234123",
            email=None,
            created_at=stored(START),
            last_login_at=stored(START),
        )
        _insert(
            connection,
            "sessions",
            id=found["session"].hex,
            user_id=found["elena"].hex,
            identity_link_id=found["link"].hex,
            token_hash=digest("live-session"),
            created_at=stored(START),
            last_seen_at=stored(START),
            expires_at=stored(START + timedelta(days=14)),
            revoked_at=None,
        )
        _insert(
            connection,
            "conversations",
            id=found["conversation"].hex,
            user_id=found["elena"].hex,
            title="Борщ",
            created_at=stored(START),
            updated_at=stored(START),
        )
        message_id = connection.exec_driver_sql(
            "INSERT INTO messages (conversation_id, role, content, created_at, expires_at) "
            "VALUES (?, 'assistant', 'Борщ варить так.', ?, ?)",
            (found["conversation"].hex, stored(START), stored(EXPIRES)),
        ).lastrowid
        _insert(
            connection,
            "favorite_recipes",
            id=found["favorite"].hex,
            user_id=found["elena"].hex,
            title="Борщ",
            content="Борщ варить так.",
            source_message_id=message_id,
            created_at=stored(START),
            kind="text",
            source_recipe_index=-1,
            servings=None,
            ingredients=None,
            steps=None,
            notes=None,
            version=1,
            updated_at=stored(START),
        )
        _insert(
            connection,
            "shopping_lists",
            id=found["list"].hex,
            user_id=found["elena"].hex,
            title="Борщ",
            status="active",
            source_kind="message",
            source_message_id=message_id,
            source_recipe_index=0,
            source_favorite_id=None,
            source_favorite_version=None,
            idempotency_key="list-key-1",
            request_fingerprint=digest("list-key-1"),
            created_at=stored(START),
            updated_at=stored(START),
        )
        _insert(
            connection,
            "shopping_list_items",
            id=found["item"].hex,
            list_id=found["list"].hex,
            position=0,
            name="свёкла",
            amount="2 шт.",
            state="unchecked",
            created_at=stored(START),
            updated_at=stored(START),
        )
        _insert(
            connection,
            "shopping_list_requests",
            id=found["request"].hex,
            user_id=found["elena"].hex,
            idempotency_key="list-key-1",
            request_fingerprint=digest("list-key-1"),
            list_id=found["list"].hex,
            created_at=stored(START),
        )
    return found


@pytest.fixture
def measure_engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'measures.db'}")
    yield engine
    engine.dispose()


def test_the_measure_migration_adds_three_columns_and_changes_no_other_value(measure_engine):
    found = _database_at_0013(measure_engine)
    before = _snapshot(measure_engine)
    assert _version(measure_engine) == [PROFILE]

    upgrade_database(measure_engine)

    assert _version(measure_engine) == [MEASURES]
    assert _tables(measure_engine) == TABLES | {"alembic_version"}
    after = _snapshot(measure_engine)
    for table in sorted(TABLES - {"users"}):
        assert after[table] == before[table], table  # byte for byte, every row
    # users: nothing but the three new last columns
    assert [row[:-3] for row in after["users"]] == before["users"]
    assert [row[-3:] for row in after["users"]] == [[None, None, None], [None, None, None]]
    columns = {column["name"]: column for column in sa.inspect(measure_engine).get_columns("users")}
    assert list(columns)[-3:] == list(COLUMNS)
    for name in COLUMNS:
        assert columns[name]["nullable"] is True
        assert str(columns[name]["type"]) == "INTEGER"
    with measure_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
        assert connection.exec_driver_sql("SELECT count(*) FROM shopping_lists").scalar() == 1
        assert connection.exec_driver_sql("SELECT count(*) FROM favorite_recipes").scalar() == 1
    with create_session_factory(measure_engine)() as db:
        elena = db.get(User, found["elena"])
        assert (elena.measure_glass_ml, elena.measure_teaspoon_ml, elena.measure_tablespoon_ml) == (
            None,
            None,
            None,
        )
        # A display name survives exactly; a user without one still has none.
        assert elena.display_name == "Елена"
        assert db.get(User, found["unnamed"]).display_name is None


def test_the_migration_does_not_rebuild_users_and_so_cascades_nothing(measure_engine):
    _database_at_0013(measure_engine)
    seeded = (
        "users",
        "allowlist_entries",
        "identity_links",
        "sessions",
        "conversations",
        "messages",
        "favorite_recipes",
        "shopping_lists",
        "shopping_list_items",
        "shopping_list_requests",
    )
    with measure_engine.connect() as connection:
        before = {
            table: connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()
            for table in seeded
        }
    assert before["users"] == 2 and before["shopping_lists"] == 1

    upgrade_database(measure_engine)

    with measure_engine.connect() as connection:
        after = {
            table: connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()
            for table in seeded
        }
    assert after == before


def test_each_range_is_a_named_database_rule_and_the_three_are_independent(measure_engine):
    upgrade_database(measure_engine)
    with create_session_factory(measure_engine)() as db, db.begin():
        user = new_user(db, display_name="Кто-то")
    user_id = user.id.hex

    with measure_engine.connect() as connection:
        # Both bounds are accepted, and NULL keeps meaning "the default applies".
        for name, (minimum, maximum) in RANGES.items():
            for value in (minimum, maximum):
                connection.exec_driver_sql(
                    f"UPDATE users SET {name} = ? WHERE id = ?", (value, user_id)
                )
                assert connection.exec_driver_sql(
                    f"SELECT {name} FROM users WHERE id = ?", (user_id,)
                ).scalar() == value
            for value in (minimum - 1, maximum + 1):
                with pytest.raises(sa.exc.IntegrityError, match=f"ck_users_{name}_range"):
                    connection.exec_driver_sql(
                        f"UPDATE users SET {name} = ? WHERE id = ?", (value, user_id)
                    )
                connection.rollback()
        # No cross-field rule relates one capacity to another: a teaspoon larger than the
        # tablespoon is a person's choice, not a database error.
        connection.exec_driver_sql(
            "UPDATE users SET measure_teaspoon_ml = 20, measure_tablespoon_ml = 5 WHERE id = ?",
            (user_id,),
        )
        assert connection.exec_driver_sql(
            "SELECT measure_teaspoon_ml, measure_tablespoon_ml FROM users WHERE id = ?", (user_id,)
        ).one() == (20, 5)


def test_downgrading_drops_only_the_three_columns_and_restores_the_0013_schema(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    pristine = create_db_engine(f"sqlite:///{tmp_path / 'pristine.db'}")
    try:
        found = _database_at_0013(migrated)
        upgrade_database(pristine, PROFILE)
        upgrade_database(migrated)
        with create_session_factory(migrated)() as db, db.begin():
            db.get(User, found["elena"]).measure_glass_ml = 240
            db.get(User, found["elena"]).measure_teaspoon_ml = 6
            db.get(User, found["elena"]).measure_tablespoon_ml = 20
        before = _snapshot(migrated)

        downgrade_database(migrated, PROFILE)

        assert _version(migrated) == [PROFILE]
        assert _fingerprint(migrated) == _fingerprint(pristine)  # the 0013 schema, exactly
        after = _snapshot(migrated)
        for table in sorted(TABLES - {"users"}):
            assert after[table] == before[table], table
        assert after["users"] == [row[:-3] for row in before["users"]]
        with migrated.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]

        upgrade_database(migrated)  # forward again: the capacities are gone, the defaults are back
        with create_session_factory(migrated)() as db:
            elena = db.get(User, found["elena"])
            assert (elena.measure_glass_ml, elena.measure_teaspoon_ml, elena.measure_tablespoon_ml) == (
                None,
                None,
                None,
            )
            assert elena.display_name == "Елена"
    finally:
        migrated.dispose()
        pristine.dispose()


def test_the_measure_revision_renders_as_plain_postgresql_alters():
    up, down = _offline_sql("up"), _offline_sql("down_from_head")
    revision = up.split(f"Running upgrade {PROFILE} -> {MEASURES}")[1].split("-- Running upgrade")[0]

    statements = [re.sub(r"\s+", " ", part.strip()) for part in revision.split(";")]
    statements = [part for part in statements if part not in ("", "COMMIT")]
    expected = []
    for name, (minimum, maximum) in RANGES.items():
        expected.append(f"ALTER TABLE users ADD COLUMN {name} INTEGER DEFAULT NULL")
        expected.append(
            f"ALTER TABLE users ADD CONSTRAINT ck_users_{name}_range "
            f"CHECK ({name} IS NULL OR {name} BETWEEN {minimum} AND {maximum})"
        )
    assert statements[: len(expected)] == expected
    assert statements[len(expected)].startswith(
        "UPDATE alembic_version SET version_num='" + MEASURES + "'"
    )
    assert len(statements) == len(expected) + 1  # nothing else is created, altered or dropped
    assert "CREATE TABLE" not in revision and "DROP" not in revision
    assert re.findall(r"^ALTER TABLE [^;]*", down, re.MULTILINE) == [
        f"ALTER TABLE users DROP COLUMN {name}" for name in COLUMNS
    ]
