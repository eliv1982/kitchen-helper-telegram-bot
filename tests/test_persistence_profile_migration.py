"""Migration 0013: the profile's text size on a database that already holds rows.

The 0012 schema is built for real and given ordinary rows: two users (one with a display
name, one without), their identities and a live session, a conversation with messages, a
text favorite, a shopping list with its item and its creation key. The upgrade adds exactly
one column to users; every other table's rows must stay byte for byte, and every user must
get the default 'normal' without its display name or anything else changing.

The way back drops the column and restores the 0012 schema exactly; the choice it held is
the only thing lost.

The migration must not rebuild users on SQLite: users is the root of the ownership foreign
keys, and a batch rebuild would cascade-delete every user-owned table. The fixture seeds
those tables precisely so that such an accident would be visible (and the upgrade itself
would fail on the foreign keys it would trip).
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
    PROFILE,
    SHOPPING,
    _fingerprint,
    _offline_sql,
    _tables,
    _version,
)
from tests.test_persistence_schema import TABLES

START = utc(2026, 11, 9, 9)
EXPIRES = START + timedelta(days=180)


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


def _database_at_0012(engine) -> dict:
    """A 0012 database with rows in the tables a profile change can touch and in the
    tables a users rebuild would cascade. Returns the ids the tests look for.

    The rows are written with plain SQL (the models now have users.text_size, which the
    0012 table does not have yet), so the fixture describes the older schema, not today's.
    """
    upgrade_database(engine, SHOPPING)
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
def profile_engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'profile.db'}")
    yield engine
    engine.dispose()


def test_the_text_size_migration_adds_one_column_and_changes_no_other_value(profile_engine):
    found = _database_at_0012(profile_engine)
    before = _snapshot(profile_engine)
    assert _version(profile_engine) == [SHOPPING]

    upgrade_database(profile_engine)

    assert _version(profile_engine) == [PROFILE]
    assert _tables(profile_engine) == TABLES | {"alembic_version"}
    after = _snapshot(profile_engine)
    for table in sorted(TABLES - {"users"}):
        assert after[table] == before[table], table  # byte for byte, every row
    # users: nothing but the new last column
    assert [row[:-1] for row in after["users"]] == before["users"]
    assert [row[-1] for row in after["users"]] == ["normal", "normal"]
    columns = {column["name"]: column for column in sa.inspect(profile_engine).get_columns("users")}
    assert list(columns)[-1] == "text_size"
    assert columns["text_size"]["nullable"] is False
    assert columns["text_size"]["default"] == "'normal'"
    with profile_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
        assert connection.exec_driver_sql("SELECT count(*) FROM shopping_lists").scalar() == 1
        assert connection.exec_driver_sql("SELECT count(*) FROM favorite_recipes").scalar() == 1
    with create_session_factory(profile_engine)() as db:
        assert db.get(User, found["elena"]).text_size == "normal"
        assert db.get(User, found["unnamed"]).text_size == "normal"
        assert db.get(User, found["unnamed"]).display_name is None


def test_the_migration_does_not_rebuild_users_and_so_cascades_nothing(profile_engine):
    found = _database_at_0012(profile_engine)
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
    with profile_engine.connect() as connection:
        before = {
            table: connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()
            for table in seeded
        }
    assert before["users"] == 2 and before["shopping_lists"] == 1

    upgrade_database(profile_engine)

    with profile_engine.connect() as connection:
        after = {
            table: connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()
            for table in seeded
        }
    assert after == before
    assert found["favorite"] is not None and found["list"] is not None


def test_the_column_defaults_and_its_closed_list_are_the_database_rules(profile_engine):
    upgrade_database(profile_engine)
    with create_session_factory(profile_engine)() as db, db.begin():
        user = new_user(db, display_name="Кто-то")

    with profile_engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT text_size FROM users WHERE id = ?", (user.id.hex,)
        ).scalar() == "normal"
        with pytest.raises(sa.exc.IntegrityError, match="ck_users_text_size_valid"):
            connection.exec_driver_sql(
                "UPDATE users SET text_size = 'huge' WHERE id = ?", (user.id.hex,)
            )
        connection.rollback()
        with pytest.raises(sa.exc.IntegrityError, match="ck_users_text_size_max_length"):
            connection.exec_driver_sql(
                "UPDATE users SET text_size = ? WHERE id = ?", ("x" * 17, user.id.hex)
            )


def test_downgrading_drops_only_the_column_and_restores_the_0012_schema(tmp_path):
    migrated = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    pristine = create_db_engine(f"sqlite:///{tmp_path / 'pristine.db'}")
    try:
        found = _database_at_0012(migrated)
        upgrade_database(pristine, SHOPPING)
        upgrade_database(migrated)
        with create_session_factory(migrated)() as db, db.begin():
            db.get(User, found["elena"]).text_size = "large"
        before = _snapshot(migrated)

        downgrade_database(migrated, SHOPPING)

        assert _version(migrated) == [SHOPPING]
        assert _fingerprint(migrated) == _fingerprint(pristine)  # the 0012 schema, exactly
        after = _snapshot(migrated)
        for table in sorted(TABLES - {"users"}):
            assert after[table] == before[table], table
        assert after["users"] == [row[:-1] for row in before["users"]]
        with migrated.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]

        upgrade_database(migrated)  # and forward again: the choice is gone, the default is back
        with create_session_factory(migrated)() as db:
            assert db.get(User, found["elena"]).text_size == "normal"
            assert db.get(User, found["elena"]).display_name == "Елена"
    finally:
        migrated.dispose()
        pristine.dispose()


def test_the_profile_revision_renders_as_plain_postgresql_alters():
    up, down = _offline_sql("up"), _offline_sql("down_from_head")
    revision = up.split(f"Running upgrade {SHOPPING} -> {PROFILE}")[1].split("-- Running upgrade")[0]

    statements = [re.sub(r"\s+", " ", part.strip()) for part in revision.split(";")]
    statements = [part for part in statements if part not in ("", "COMMIT")]
    assert statements[0] == (
        "ALTER TABLE users ADD COLUMN text_size VARCHAR(16) DEFAULT 'normal' NOT NULL"
    )
    assert statements[1] == (
        "ALTER TABLE users ADD CONSTRAINT ck_users_text_size_max_length "
        "CHECK (length(text_size) <= 16)"
    )
    assert statements[2] == (
        "ALTER TABLE users ADD CONSTRAINT ck_users_text_size_valid "
        "CHECK (text_size IN ('normal', 'large'))"
    )
    assert statements[3].startswith("UPDATE alembic_version SET version_num='" + PROFILE + "'")
    assert len(statements) == 4  # nothing else is created, altered, updated or dropped
    assert "CREATE TABLE" not in revision and "DROP" not in revision
    assert re.findall(r"^ALTER TABLE [^;]*", down, re.MULTILINE) == [
        "ALTER TABLE users DROP COLUMN text_size"
    ]
