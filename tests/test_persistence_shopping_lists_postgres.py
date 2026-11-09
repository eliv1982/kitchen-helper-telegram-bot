"""Shopping lists and migration 0012 against a real PostgreSQL server. Opt-in.

The SQLite tests cannot show the PostgreSQL half of the migration (a foreign key over a
UUID primary key, the JSON-free tables, the exact constraint DDL), the behaviour of the
item writes and the key reservation under the server's own isolation, or what happens
when two real transactions overlap. These do: the concurrency tests at the end hold one
transaction inside a function while a second opens its own, and assert the outcome of the
overlap (a replay, a conflict, a serialized write), not a timing. They CREATE and DROP
databases on a throwaway server on this machine; see tests/test_admin_postgres.py for
the safe recipe:

    docker run -d --rm --name hh-test-pg -e POSTGRES_PASSWORD=PASSWORD \\
        -p 127.0.0.1:55432:5432 postgres:16-alpine
    export HOUSE_HELPER_TEST_POSTGRES_URL="postgresql+psycopg://postgres:PASSWORD@127.0.0.1:55432/postgres"

Without the variable every test is skipped (not a pass). CI does not set it.
"""

import os
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from house_helper.core.recipes import Ingredient, Recipe
from house_helper.persistence import message_recipes as mr
from house_helper.persistence import shopping_lists as sl
from house_helper.persistence.database import (
    create_session_factory,
    downgrade_database,
    upgrade_database,
)
from house_helper.persistence.models import (
    Conversation,
    ShoppingList,
    ShoppingListItem,
    ShoppingListRequest,
    User,
)
from tests.persistence_support import new_conversation, new_message, new_user, utc
from tests.postgres_harness import POSTGRES_URL_ENV, Scratch
from tests.test_admin_postgres import pg_run, template  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL (see the module docstring)",
)

PREVIOUS = "0011_telegram_active_selection"
NOW = utc(2026, 11, 8, 12)
BORSH = Recipe(
    index=0,
    title="Борщ",
    servings="4 порции",
    ingredients=(
        Ingredient("свёкла", "2 шт."),
        Ingredient("капуста", "300 г"),
    ),
    steps=("Сварите.",),
)


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


def answered(db, user):
    conversation = new_conversation(db, user, created_at=NOW, updated_at=NOW)
    answer = new_message(
        db,
        conversation,
        role="assistant",
        content="Вот рецепт.",
        created_at=NOW,
        expires_at=NOW + timedelta(days=365),
    )
    mr.record_validated_recipes(db, answer.id, [BORSH], now=NOW)
    return conversation, answer


def make_list(db, user, conversation, answer, *, key="list-1", **request):
    fingerprint = sl.creation_fingerprint(
        source="message",
        conversation_id=conversation.id,
        message_id=answer.id,
        recipe_index=0,
        item_states=request.get("item_states"),
    )
    return sl.create_list(
        db,
        user.id,
        source="message",
        conversation_id=conversation.id,
        message_id=answer.id,
        recipe_index=0,
        idempotency_key=key,
        fingerprint=fingerprint,
        now=NOW,
        **request,
    )


# --- the migration -------------------------------------------------------------------------


def test_a_populated_0011_database_upgrades_and_downgrades_on_postgres(pg_run):
    name = pg_run.create_database()
    scratch = Scratch(pg_run, name)
    try:
        engine = scratch.engine()
        try:
            upgrade_database(engine, PREVIOUS)
            factory = create_session_factory(engine)
            with factory() as db, db.begin():
                user = new_user(db, display_name="Elena")
                conversation = new_conversation(db, user, created_at=NOW, updated_at=NOW)
                answer = new_message(
                    db,
                    conversation,
                    role="assistant",
                    content="Борщ",
                    created_at=NOW,
                    expires_at=NOW + timedelta(days=365),
                )
                mr.record_validated_recipes(db, answer.id, [BORSH], now=NOW)

            def snapshot():
                with factory() as db:
                    return (
                        db.scalar(sa.select(sa.func.count()).select_from(User)),
                        db.scalar(sa.select(sa.func.count()).select_from(Conversation)),
                    )

            before = snapshot()
            upgrade_database(engine, "0012_shopping_lists")

            with factory() as db:
                assert db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == (
                    "0012_shopping_lists"
                )
                assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingList)) == 0
                assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingListItem)) == 0
                assert (
                    db.scalar(sa.select(sa.func.count()).select_from(ShoppingListRequest)) == 0
                )
                for table in (
                    "shopping_lists",
                    "shopping_list_items",
                    "shopping_list_requests",
                ):
                    assert (
                        db.scalar(
                            sa.text(f"SELECT to_regclass('{table}') IS NOT NULL")
                        )
                        is True
                    )
            assert snapshot() == before

            downgrade_database(engine, PREVIOUS)
            with factory() as db:
                assert db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == (
                    PREVIOUS
                )
                for table in (
                    "shopping_lists",
                    "shopping_list_items",
                    "shopping_list_requests",
                ):
                    assert (
                        db.scalar(sa.text(f"SELECT to_regclass('{table}') IS NULL"))
                        is True
                    )
            assert snapshot() == before
        finally:
            engine.dispose()
    finally:
        pg_run.drop_database(name)


# --- the lists on PostgreSQL ----------------------------------------------------------------


def test_a_list_is_created_edited_and_deleted_under_read_committed(read_committed):
    engine, factory = factory_for(read_committed)
    try:
        with factory() as db, db.begin():
            user = new_user(db, display_name="Elena")
            conversation, answer = answered(db, user)
            made = make_list(db, user, conversation, answer, item_states={0: "at_home"})
            assert [item.state for item in made.shopping_list.items] == ["at_home", "unchecked"]
            user_id, list_id = user.id, made.shopping_list.id
            item_id = made.shopping_list.items[1].id

        with factory() as db, db.begin():
            item = sl.update_item(
                db, user_id, list_id, item_id, changes={"state": "purchased"}, now=NOW
            )
            assert item.state == "purchased"
            added = sl.add_item(db, user_id, list_id, name="соль", amount=None, now=NOW)
            assert added.position == 2
            sl.set_status(db, user_id, list_id, status="completed", now=NOW)
            with pytest.raises(sl.ShoppingListCompleted):
                sl.delete_item(db, user_id, list_id, item_id, now=NOW)

        with factory() as db:
            detail = sl.get_list(db, user_id, list_id)
            assert detail.status == "completed"
            assert [(item.position, item.state) for item in detail.items] == [
                (0, "at_home"),
                (1, "purchased"),
                (2, "unchecked"),
            ]

        with factory() as db, db.begin():
            sl.set_status(db, user_id, list_id, status="active", now=NOW)
            sl.delete_item(db, user_id, list_id, item_id, now=NOW)
            sl.delete_list(db, user_id, list_id)
        with factory() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingList)) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingListItem)) == 0
            # The key is still consumed: the deletion nulled the ledger's reference.
            assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingListRequest)) == 1
            assert db.scalars(sa.select(ShoppingListRequest.list_id)).one() is None
            with pytest.raises(sl.ShoppingListDeleted):
                make_list(
                    db,
                    SimpleNamespace(id=user_id),
                    SimpleNamespace(id=conversation.id),
                    SimpleNamespace(id=answer.id),
                    item_states={0: "at_home"},  # the request that consumed the key
                )
    finally:
        engine.dispose()


def test_the_idempotency_key_is_unique_per_user_on_postgres(read_committed):
    engine, factory = factory_for(read_committed)
    try:
        with factory() as db, db.begin():
            user = new_user(db, display_name="Elena")
            conversation, answer = answered(db, user)
            first = make_list(db, user, conversation, answer)
            assert first.created is True

        with factory() as db, db.begin():
            again = make_list(db, user, conversation, answer)
            assert again.created is False
            assert again.shopping_list.id == first.shopping_list.id

        with factory() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingList)) == 1
            with pytest.raises(sa.exc.IntegrityError):
                with db.begin_nested():
                    db.add(
                        ShoppingList(
                            user_id=user.id,
                            title="Другой",
                            status="active",
                            source_kind="message",
                            source_message_id=answer.id,
                            source_recipe_index=0,
                            idempotency_key="list-1",
                            request_fingerprint="0" * 64,
                            created_at=NOW,
                            updated_at=NOW,
                        )
                    )
    finally:
        engine.dispose()


def test_ownership_and_the_user_cascade_on_postgres(read_committed):
    engine, factory = factory_for(read_committed)
    try:
        with factory() as db, db.begin():
            owner = new_user(db, display_name="A")
            other = new_user(db, display_name="B")
            conversation, answer = answered(db, owner)
            made = make_list(db, owner, conversation, answer)
            owner_id, other_id, list_id = owner.id, other.id, made.shopping_list.id

        with factory() as db:
            with pytest.raises(sl.ShoppingListNotFound):
                sl.get_list(db, other_id, list_id)
            with pytest.raises(sl.ShoppingListNotFound):
                sl.add_item(db, other_id, list_id, name="соль", now=NOW)

        with factory() as db, db.begin():
            db.execute(sa.delete(User).where(User.id == owner_id))

        with factory() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingList)) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingListItem)) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingListRequest)) == 0
    finally:
        engine.dispose()


def test_a_list_survives_the_deletion_of_its_source_on_postgres(read_committed):
    engine, factory = factory_for(read_committed)
    try:
        with factory() as db, db.begin():
            user = new_user(db, display_name="Elena")
            conversation, answer = answered(db, user)
            first = make_list(db, user, conversation, answer)
            user_id, list_id = user.id, first.shopping_list.id

        with factory() as db, db.begin():
            db.execute(sa.delete(Conversation).where(Conversation.id == conversation.id))

        with factory() as db:
            detail = sl.get_list(db, user_id, list_id)
            assert [(item.name, item.amount) for item in detail.items] == [
                ("свёкла", "2 шт."),
                ("капуста", "300 г"),
            ]
    finally:
        engine.dispose()


# --- overlapping transactions ---------------------------------------------------------------
#
# Every test here makes one transaction pause inside a function while a second runs, and
# then lets the first finish. The assertion is on the outcome, never on how long anything
# took: the pause is an event, and the only waiting is for PostgreSQL to report that the
# second backend is blocked on a lock. The row lock of the user (a creation) or of the
# list (an item write) is what serializes the two; the second then sees the first's work.


def _thread(problems: list, target) -> threading.Thread:
    """Run `target` on its own thread; an exception is kept for the test's own thread."""

    def run() -> None:
        try:
            target()
        except BaseException as error:  # noqa: BLE001 - reported by the test that started it
            problems.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _wait_until_a_backend_waits_on_a_lock(engine, deadline_seconds: float = 30.0) -> None:
    """Wait until some session of this database is blocked on a lock (a server fact, not
    a sleep: pg_stat_activity reports the wait as soon as it begins)."""
    sql = (
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock'"
    )
    deadline = time.monotonic() + deadline_seconds
    while True:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            if connection.execute(sa.text(sql)).scalar() >= 1:
                return
        if time.monotonic() >= deadline:
            raise AssertionError("the second transaction never waited on a lock")
        time.sleep(0.05)


def _make_list_by_ids(db, ids, **request):
    """make_list for a thread that carries only the ids, not the ORM objects."""
    user_id, conversation_id, answer_id = ids
    return make_list(
        db,
        SimpleNamespace(id=user_id),
        SimpleNamespace(id=conversation_id),
        SimpleNamespace(id=answer_id),
        **request,
    )


def _seed(read_committed) -> tuple:
    engine, factory = factory_for(read_committed)
    with factory() as db, db.begin():
        user = new_user(db, display_name="Elena")
        conversation, answer = answered(db, user)
        made = make_list(db, user, conversation, answer, item_states={0: "at_home"})
        ids = (user.id, conversation.id, answer.id)
        list_id = made.shopping_list.id
        item_ids = [item.id for item in made.shopping_list.items]
    return engine, factory, ids, list_id, item_ids


def test_two_overlapping_creations_with_one_key_make_one_list(read_committed):
    engine, factory, ids, _, _ = _seed(read_committed)
    try:
        made, problems = {}, []
        inside, release = threading.Event(), threading.Event()

        def first():
            with factory() as db, db.begin():
                made["first"] = _make_list_by_ids(db, ids, key="same")
                inside.set()
                if not release.wait(30):
                    raise AssertionError("the other transaction never finished")

        def second():
            if not inside.wait(30):
                raise AssertionError("the first transaction never started")
            with factory() as db, db.begin():
                made["second"] = _make_list_by_ids(db, ids, key="same")

        one = _thread(problems, first)
        two = None
        try:
            assert inside.wait(30)  # the first holds the user row, uncommitted
            two = _thread(problems, second)
            _wait_until_a_backend_waits_on_a_lock(engine)  # the second is at the user lock
        finally:
            release.set()
            one.join(30)
            if two is not None:
                two.join(30)
        assert not one.is_alive() and (two is None or not two.is_alive())

        assert problems == []
        assert made["first"].created is True and made["second"].created is False
        assert made["first"].shopping_list.id == made["second"].shopping_list.id
        with factory() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingList)) == 2  # the seed's + this
            assert (
                db.scalar(
                    sa.select(sa.func.count())
                    .select_from(ShoppingListRequest)
                    .where(ShoppingListRequest.idempotency_key == "same")
                )
                == 1
            )
    finally:
        engine.dispose()


def test_two_overlapping_creations_with_conflicting_requests_refuse_the_second(read_committed):
    engine, factory, ids, _, _ = _seed(read_committed)
    try:
        made, problems = {}, []
        inside, release = threading.Event(), threading.Event()

        def first():
            with factory() as db, db.begin():
                made["first"] = _make_list_by_ids(db, ids, key="same")
                inside.set()
                if not release.wait(30):
                    raise AssertionError("the other transaction never finished")

        def second():
            if not inside.wait(30):
                raise AssertionError("the first transaction never started")
            with factory() as db, db.begin():
                _make_list_by_ids(db, ids, key="same", item_states={1: "purchased"})

        one = _thread(problems, first)
        two = None
        try:
            assert inside.wait(30)
            two = _thread(problems, second)
            _wait_until_a_backend_waits_on_a_lock(engine)
        finally:
            release.set()
            one.join(30)
            if two is not None:
                two.join(30)
        assert not one.is_alive() and (two is None or not two.is_alive())

        assert len(problems) == 1 and isinstance(problems[0], sl.IdempotencyKeyReused)
        assert made["first"].created is True
        with factory() as db:
            assert (
                db.scalar(
                    sa.select(sa.func.count())
                    .select_from(ShoppingListRequest)
                    .where(ShoppingListRequest.idempotency_key == "same")
                )
                == 1
            )
            assert (
                db.scalar(
                    sa.select(sa.func.count()).select_from(ShoppingList)
                )
                == 2
            )
    finally:
        engine.dispose()


def test_a_replay_overlapping_a_deletion_never_resurrects_the_list(read_committed):
    engine, factory, ids, list_id, _ = _seed(read_committed)
    user_id = ids[0]
    try:
        results, problems = {}, []
        deleted, release = threading.Event(), threading.Event()

        def deleting():
            with factory() as db, db.begin():
                sl.delete_list(db, user_id, list_id)
                deleted.set()
                if not release.wait(30):
                    raise AssertionError("the replay never finished")

        def replaying():
            if not deleted.wait(30):
                raise AssertionError("the deletion never started")
            with factory() as db, db.begin():
                results["replay"] = _make_list_by_ids(
                    db, ids, item_states={0: "at_home"}  # the seed's own request
                )

        one = _thread(problems, deleting)
        two = None
        try:
            assert deleted.wait(30)  # the deletion is done but not committed
            two = _thread(problems, replaying)
            two.join(30)  # a read: it answers from the pre-deletion snapshot, no lock needed
        finally:
            release.set()
            one.join(30)
            if two is not None and two.is_alive():
                two.join(30)
        assert not one.is_alive() and (two is None or not two.is_alive())

        assert problems == []
        assert results["replay"].created is False  # the replay wrote nothing
        with factory() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingList)) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingListItem)) == 0
            request_row = db.scalars(sa.select(ShoppingListRequest)).one()
            assert request_row.list_id is None  # the tombstone, after both committed
            with pytest.raises(sl.ShoppingListDeleted):
                _make_list_by_ids(db, ids, item_states={0: "at_home"})
    finally:
        engine.dispose()


def test_overlapping_item_writes_on_different_items_serialize_without_loss(read_committed):
    engine, factory, ids, list_id, item_ids = _seed(read_committed)
    user_id = ids[0]
    try:
        problems = []
        inside, release = threading.Event(), threading.Event()

        def first():
            with factory() as db, db.begin():
                sl.update_item(
                    db, user_id, list_id, item_ids[0],
                    changes={"state": "purchased"}, now=NOW,
                )
                inside.set()
                if not release.wait(30):
                    raise AssertionError("the other transaction never finished")

        def second():
            if not inside.wait(30):
                raise AssertionError("the first transaction never started")
            with factory() as db, db.begin():
                sl.update_item(
                    db, user_id, list_id, item_ids[1],
                    changes={"state": "purchased"}, now=NOW,
                )

        one = _thread(problems, first)
        two = None
        try:
            assert inside.wait(30)
            two = _thread(problems, second)
            _wait_until_a_backend_waits_on_a_lock(engine)  # both need the list row
        finally:
            release.set()
            one.join(30)
            if two is not None:
                two.join(30)
        assert not one.is_alive() and (two is None or not two.is_alive())

        assert problems == []  # neither write was lost or refused
        with factory() as db:
            detail = sl.get_list(db, user_id, list_id)
            assert [item.state for item in detail.items] == ["purchased", "purchased"]
    finally:
        engine.dispose()


def test_a_completion_that_wins_the_list_lock_refuses_the_item_write(read_committed):
    engine, factory, ids, list_id, item_ids = _seed(read_committed)
    user_id = ids[0]
    try:
        problems = []
        inside, release = threading.Event(), threading.Event()

        def completing():
            with factory() as db, db.begin():
                sl.set_status(db, user_id, list_id, status="completed", now=NOW)
                inside.set()
                if not release.wait(30):
                    raise AssertionError("the other transaction never finished")

        def editing():
            if not inside.wait(30):
                raise AssertionError("the first transaction never started")
            with factory() as db, db.begin():
                sl.update_item(
                    db, user_id, list_id, item_ids[0],
                    changes={"state": "purchased"}, now=NOW,
                )

        one = _thread(problems, completing)
        two = None
        try:
            assert inside.wait(30)
            two = _thread(problems, editing)
            _wait_until_a_backend_waits_on_a_lock(engine)
        finally:
            release.set()
            one.join(30)
            if two is not None:
                two.join(30)
        assert not one.is_alive() and (two is None or not two.is_alive())

        assert len(problems) == 1 and isinstance(problems[0], sl.ShoppingListCompleted)
        with factory() as db:
            detail = sl.get_list(db, user_id, list_id)
            assert detail.status == "completed"
            assert detail.items[0].state == "at_home"  # the refused edit changed nothing
    finally:
        engine.dispose()


def test_the_item_cap_holds_when_two_additions_overlap(read_committed):
    engine, factory, ids, list_id, _ = _seed(read_committed)
    user_id = ids[0]
    try:
        with factory() as db, db.begin():
            for position in range(2, 199):  # the seed list has two items: 199 in total
                db.add(
                    ShoppingListItem(
                        list_id=list_id,
                        position=position,
                        name=f"item {position}",
                        state="unchecked",
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )

        problems = []
        inside, release = threading.Event(), threading.Event()

        def first():
            with factory() as db, db.begin():
                sl.add_item(db, user_id, list_id, name="последний", now=NOW)
                inside.set()
                if not release.wait(30):
                    raise AssertionError("the other transaction never finished")

        def second():
            if not inside.wait(30):
                raise AssertionError("the first transaction never started")
            with factory() as db, db.begin():
                sl.add_item(db, user_id, list_id, name="лишний", now=NOW)

        one = _thread(problems, first)
        two = None
        try:
            assert inside.wait(30)
            two = _thread(problems, second)
            _wait_until_a_backend_waits_on_a_lock(engine)
        finally:
            release.set()
            one.join(30)
            if two is not None:
                two.join(30)
        assert not one.is_alive() and (two is None or not two.is_alive())

        assert len(problems) == 1 and isinstance(problems[0], sl.ShoppingListItemsFull)
        with factory() as db:
            assert (
                db.scalar(
                    sa.select(sa.func.count())
                    .select_from(ShoppingListItem)
                    .where(ShoppingListItem.list_id == list_id)
                )
                == 200
            )
    finally:
        engine.dispose()
