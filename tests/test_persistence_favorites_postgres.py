"""Structured favorites and migration 0010 against a real PostgreSQL server. Opt-in.

The SQLite tests cannot show the PostgreSQL half of the migration (JSONB, ALTER TABLE on a
live table, a composite foreign key over a unique index) nor the optimistic-concurrency
statement under READ COMMITTED. These tests do. They CREATE and DROP databases on a
throwaway server on this machine; see tests/test_admin_postgres.py for the safe recipe:

    docker run -d --rm --name hh-test-pg -e POSTGRES_PASSWORD=PASSWORD \\
        -p 127.0.0.1:55432:5432 postgres:16-alpine
    export HOUSE_HELPER_TEST_POSTGRES_URL="postgresql+psycopg://postgres:PASSWORD@127.0.0.1:55432/postgres"

Without the variable every test is skipped (not a pass). CI does not set it.
"""

import os
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.core.recipes import Ingredient
from house_helper.persistence import favorites as fav
from house_helper.persistence import message_recipes as mr
from house_helper.persistence.database import (
    create_session_factory,
    downgrade_database,
    upgrade_database,
)
from house_helper.persistence.models import FavoriteRecipe, Message, MessageRecipe
from tests.persistence_support import new_conversation, new_message, new_user, utc
from tests.postgres_harness import POSTGRES_URL_ENV, Scratch
from tests.test_admin_postgres import pg_run, scratch, template  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL (see the module docstring)",
)

ATTACHMENTS = "0009_message_attachments"
NOW = utc(2026, 11, 1, 12)

OLD_FAVORITES = sa.table(
    "favorite_recipes",
    sa.column("id", sa.Uuid),
    sa.column("user_id", sa.Uuid),
    sa.column("title", sa.String),
    sa.column("content", sa.Text),
    sa.column("source_message_id", sa.BigInteger),
    sa.column("created_at", sa.DateTime(timezone=True)),
)

ANSWER = """Вот варианты.
<<<RECIPE>>>
{"title": "Борщ", "servings": "4 порции", "ingredients": [{"name": "свёкла", "amount": "2 шт."}], "steps": ["Сварите."]}
<<<END_RECIPE>>>
<<<RECIPE>>>
{"title": "Уха", "servings": null, "ingredients": [{"name": "рыба", "amount": "1 кг"}], "steps": ["Сварите.", "Дайте настояться."]}
<<<END_RECIPE>>>
"""


@pytest.fixture
def read_committed(pg_run, template):
    """A clone of the migrated template whose default isolation is the server's own
    (READ COMMITTED), as the web application uses it."""
    name = pg_run.create_database(template=template)
    try:
        yield Scratch(pg_run, name)
    finally:
        pg_run.drop_database(name)


def factory_for(scratch):
    engine = scratch.engine()
    return engine, create_session_factory(engine)


def make_answered(factory):
    with factory() as db, db.begin():
        user = new_user(db, display_name="Elena")
        conversation = new_conversation(db, user, created_at=NOW, updated_at=NOW)
        answer = new_message(
            db,
            conversation,
            role="assistant",
            content=ANSWER,
            created_at=NOW,
            expires_at=NOW + timedelta(days=365),
        )
        mr.record_recipes_for_message(db, answer.id, ANSWER, now=NOW)
        return user.id, conversation.id, answer.id


# --- the migration -------------------------------------------------------------------------


def test_a_legacy_favorite_survives_0009_to_0010_on_postgres(scratch):
    engine = scratch.engine()
    try:
        downgrade_database(engine, ATTACHMENTS)
        legacy_id = uuid.uuid4()
        with create_session_factory(engine)() as db, db.begin():
            user = new_user(db, display_name="Elena")
            conversation = new_conversation(db, user, created_at=NOW, updated_at=NOW)
            answer = new_message(db, conversation, role="assistant", content="Борщ", created_at=NOW)
            db.execute(
                OLD_FAVORITES.insert().values(
                    id=legacy_id,
                    user_id=user.id,
                    title="Борщ",
                    content="Борщ: свёкла.",
                    source_message_id=answer.id,
                    created_at=NOW,
                )
            )
            owner = user.id

        upgrade_database(engine)

        with create_session_factory(engine)() as db:
            row = db.get(FavoriteRecipe, legacy_id)
            assert row.user_id == owner
            assert row.content == "Борщ: свёкла."
            assert (row.kind, row.source_recipe_index, row.version) == ("text", -1, 1)
            assert (row.servings, row.ingredients, row.steps, row.notes) == (None, None, None, None)
            assert row.updated_at == row.created_at == NOW
    finally:
        engine.dispose()


def test_the_composite_key_refuses_a_recipe_of_a_user_message_on_postgres(scratch):
    engine, factory = factory_for(scratch)
    try:
        with factory() as db, db.begin():
            user = new_user(db)
            conversation = new_conversation(db, user)
            question = new_message(db, conversation, role="user", content="?")
            with pytest.raises(sa.exc.IntegrityError):
                with db.begin_nested():
                    db.add(
                        MessageRecipe(
                            message_id=question.id,
                            position=0,
                            title="a",
                            ingredients=[{"name": "x", "amount": "1"}],
                            steps=["s"],
                            created_at=NOW,
                        )
                    )
    finally:
        engine.dispose()


def test_the_downgrade_refuses_structured_data_on_postgres(scratch):
    engine, factory = factory_for(scratch)
    try:
        user_id, conversation_id, answer_id = make_answered(factory)
        with factory() as db, db.begin():
            fav.save_favorite(
                db, user_id, conversation_id, answer_id, now=NOW, recipe_index=0
            )

        with pytest.raises(RuntimeError, match="structured favorite"):
            downgrade_database(engine, ATTACHMENTS)

        with create_session_factory(engine)() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(FavoriteRecipe)) == 1
            assert db.scalar(sa.select(sa.func.count()).select_from(MessageRecipe)) == 2
    finally:
        engine.dispose()


# --- favorites on PostgreSQL ---------------------------------------------------------------


def test_each_recipe_is_saved_and_edited_on_postgres(read_committed):
    engine, factory = factory_for(read_committed)
    try:
        user_id, conversation_id, answer_id = make_answered(factory)

        with factory() as db, db.begin():
            first = fav.save_favorite(
                db, user_id, conversation_id, answer_id, now=NOW, recipe_index=0
            )
            second = fav.save_favorite(
                db, user_id, conversation_id, answer_id, now=NOW, recipe_index=1
            )

        assert (first.favorite.title, second.favorite.title) == ("Борщ", "Уха")
        detail = None
        with factory() as db, db.begin():
            detail = fav.update_favorite(
                db,
                user_id,
                first.favorite.id,
                expected_version=1,
                changes={
                    "title": "Мой борщ",
                    "servings": "2 порции",
                    "ingredients": [{"name": "морковь", "amount": "1 шт."}],
                    "steps": ["Иначе."],
                    "notes": "заметка",
                },
                now=NOW + timedelta(hours=1),
            )
        assert detail.version == 2
        assert detail.ingredients == (Ingredient("морковь", "1 шт."),)
        assert detail.text.endswith("Мои заметки:\nзаметка")

        with factory() as db:
            stored = fav.get_favorite(db, user_id, first.favorite.id)
            assert stored.version == 2 and stored.title == "Мой борщ"
    finally:
        engine.dispose()


def test_a_stale_edit_conflicts_under_read_committed(read_committed):
    engine, factory = factory_for(read_committed)
    try:
        user_id, conversation_id, answer_id = make_answered(factory)
        with factory() as db, db.begin():
            favorite = fav.save_favorite(
                db, user_id, conversation_id, answer_id, now=NOW, recipe_index=0
            ).favorite

        with factory() as first, factory() as stale:
            fav.update_favorite(
                first,
                user_id,
                favorite.id,
                expected_version=1,
                changes={"title": "Первая правка"},
                now=NOW,
            )
            first.commit()
            with pytest.raises(fav.FavoriteConflict):
                fav.update_favorite(
                    stale,
                    user_id,
                    favorite.id,
                    expected_version=1,
                    changes={"title": "Устаревшая"},
                    now=NOW,
                )
            stale.rollback()

        with factory() as db:
            found = fav.get_favorite(db, user_id, favorite.id)
            assert (found.title, found.version) == ("Первая правка", 2)
    finally:
        engine.dispose()


def test_the_database_refuses_two_favorites_at_one_position_on_postgres(scratch):
    engine, factory = factory_for(scratch)
    try:
        user_id, conversation_id, answer_id = make_answered(factory)
        with factory() as db, db.begin():
            fav.save_favorite(
                db, user_id, conversation_id, answer_id, now=NOW, recipe_index=0
            )

        with factory() as db:
            with pytest.raises(sa.exc.IntegrityError):
                with db.begin_nested():
                    db.add(
                        FavoriteRecipe(
                            user_id=user_id,
                            title="Борщ",
                            content="Борщ",
                            source_message_id=answer_id,
                            kind="structured",
                            source_recipe_index=0,
                            ingredients=[{"name": "x", "amount": "1"}],
                            steps=["s"],
                            created_at=NOW,
                            updated_at=NOW,
                        )
                    )
    finally:
        engine.dispose()


def test_a_message_recipe_is_deleted_with_its_message_on_postgres(scratch):
    engine, factory = factory_for(scratch)
    try:
        user_id, conversation_id, answer_id = make_answered(factory)
        with factory() as db, db.begin():
            fav.save_favorite(
                db, user_id, conversation_id, answer_id, now=NOW, recipe_index=0
            )

        with factory() as db, db.begin():
            db.execute(sa.delete(Message).where(Message.id == answer_id))

        with factory() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(MessageRecipe)) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(FavoriteRecipe)) == 1
    finally:
        engine.dispose()
