"""Transactions on SQLite: the caller owns them, and a savepoint never commits.

Left alone, Python's sqlite3 module opens a transaction only before an INSERT,
UPDATE or DELETE. A SAVEPOINT taken after nothing but reads then starts the
transaction itself, and releasing it commits: work the caller meant to roll back
stayed. open_turn_request, which looks the key up before its savepoint, did
exactly that. create_db_engine now takes the BEGIN away from the module and issues
its own at the start of every transaction.

Every test here uses a file database and a second, independent session as the
witness, and runs against both the schema created from the models and the
migrated one (the `engine` fixture). Setup commits first, so the session under
test starts with a read, which is the case that failed.
"""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.persistence import turns
from house_helper.persistence.base import Base
from house_helper.persistence.database import (
    create_db_engine,
    create_session_factory,
    upgrade_database,
)
from house_helper.persistence.models import Conversation, Message, TurnRequest
from tests.persistence_support import (  # noqa: F401  (fixtures)
    count,
    engine,
    factory,
    new_conversation,
    new_message,
    new_turn_request,
    new_user,
    utc,
)

EARLIER = utc(2020, 1, 1)
NOW = utc(2026, 10, 16, 12)


@pytest.fixture
def conversation_id(factory):
    """A committed conversation whose last activity was long ago."""
    with factory() as setup:
        conversation = new_conversation(setup, new_user(setup), updated_at=EARLIER)
        setup.commit()
        return conversation.id


def witness(factory, conversation_id):
    """What an independent session sees: only what has been committed."""
    with factory() as other:
        return (
            count(other, Message),
            count(other, TurnRequest),
            other.scalar(
                sa.select(Conversation.updated_at).where(Conversation.id == conversation_id)
            ),
        )


def recorded_statements(engine):
    """A list that fills with the SQL text of every statement `engine` runs."""
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    return statements


# --- the engine begins a transaction itself ---------------------------------------


def test_the_first_statement_of_a_transaction_is_begin_even_when_it_is_a_read(engine):
    statements = recorded_statements(engine)

    with engine.connect() as connection:
        connection.exec_driver_sql("SELECT 1")

        assert connection.connection.dbapi_connection.in_transaction

    assert statements[:2] == ["BEGIN", "SELECT 1"]


def test_a_savepoint_is_never_the_outermost_one(engine, factory, conversation_id):
    statements = recorded_statements(engine)

    with factory() as s:
        conversation = s.get(Conversation, conversation_id)  # a read: all sqlite3 would see
        with s.begin_nested():
            new_message(s, conversation)  # SQLAlchemy emits SAVEPOINT with the first statement in it

    order = [text.split()[0] for text in statements if text.split()[0] in ("BEGIN", "SAVEPOINT", "RELEASE")]
    assert order == ["BEGIN", "SAVEPOINT", "RELEASE"]


# --- the original failure: a released savepoint outlived the caller's rollback -----


@pytest.mark.parametrize("how", ["a plain savepoint", "open_turn_request"])
def test_the_callers_rollback_takes_back_a_released_savepoint(factory, conversation_id, how):
    with factory() as s:
        conversation = s.get(Conversation, conversation_id)  # only a read so far
        if how == "a plain savepoint":
            with s.begin_nested():
                new_message(s, conversation)
        else:
            turns.open_turn_request(s, conversation, "key-1", "hello", now=NOW)
        s.rollback()

    assert witness(factory, conversation_id) == (0, 0, EARLIER)


def test_nothing_is_visible_to_others_until_the_caller_commits(factory, conversation_id):
    with factory() as s:
        conversation = s.get(Conversation, conversation_id)
        turns.open_turn_request(s, conversation, "key-1", "hello", now=NOW)

        assert witness(factory, conversation_id) == (0, 0, EARLIER)

        s.commit()

    assert witness(factory, conversation_id) == (1, 1, NOW)


def test_the_conversations_activity_time_commits_and_rolls_back_with_the_request(
    factory, conversation_id
):
    with factory() as s:
        turns.open_turn_request(s, s.get(Conversation, conversation_id), "key-1", "a", now=NOW)
        s.commit()
    with factory() as s:
        later = NOW + timedelta(hours=1)
        turns.open_turn_request(s, s.get(Conversation, conversation_id), "key-2", "b", now=later)
        s.rollback()

    assert witness(factory, conversation_id) == (1, 1, NOW)  # the second one never happened


def test_two_requests_in_one_transaction_stand_or_fall_together(factory, conversation_id):
    later = NOW + timedelta(hours=1)
    with factory() as s:
        conversation = s.get(Conversation, conversation_id)
        turns.open_turn_request(s, conversation, "key-1", "a", now=NOW)
        turns.open_turn_request(s, conversation, "key-2", "b", now=later)
        s.rollback()
    assert witness(factory, conversation_id) == (0, 0, EARLIER)

    with factory() as s:
        conversation = s.get(Conversation, conversation_id)
        turns.open_turn_request(s, conversation, "key-1", "a", now=NOW)
        turns.open_turn_request(s, conversation, "key-2", "b", now=later)
        s.commit()
    assert witness(factory, conversation_id) == (2, 2, later)


# --- savepoints: nested, rolled back, failing -------------------------------------


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
def test_nested_savepoints_end_with_the_transaction_around_them(factory, conversation_id, outcome):
    with factory() as s:
        conversation = s.get(Conversation, conversation_id)
        with s.begin_nested():
            new_message(s, conversation, content="outer")
            with s.begin_nested():
                new_message(s, conversation, content="inner")
        getattr(s, outcome)()

    messages, _, _ = witness(factory, conversation_id)
    assert messages == (2 if outcome == "commit" else 0)


def test_a_rolled_back_savepoint_takes_back_only_its_own_work(factory, conversation_id):
    with factory() as s:
        conversation = s.get(Conversation, conversation_id)
        new_message(s, conversation, content="before")
        savepoint = s.begin_nested()
        new_message(s, conversation, content="inside")
        savepoint.rollback()
        new_message(s, conversation, content="after")
        s.commit()

    with factory() as other:
        assert other.scalars(sa.select(Message.content).order_by(Message.id)).all() == [
            "before",
            "after",
        ]


def test_an_inner_savepoint_can_be_rolled_back_inside_an_outer_one_that_is_kept(
    factory, conversation_id
):
    with factory() as s:
        conversation = s.get(Conversation, conversation_id)
        with s.begin_nested():
            new_message(s, conversation, content="kept")
            inner = s.begin_nested()
            new_message(s, conversation, content="dropped")
            inner.rollback()
        s.commit()

    with factory() as other:
        assert other.scalars(sa.select(Message.content)).all() == ["kept"]


def test_an_exception_in_a_savepoint_block_takes_back_that_block_and_the_session_carries_on(
    factory, conversation_id
):
    with factory() as s:
        conversation = s.get(Conversation, conversation_id)
        with pytest.raises(RuntimeError, match="boom"):
            with s.begin_nested():
                new_message(s, conversation, content="doomed")
                raise RuntimeError("boom")
        new_message(s, conversation, content="kept")
        s.commit()

    with factory() as other:
        assert other.scalars(sa.select(Message.content)).all() == ["kept"]


def test_a_database_error_in_a_savepoint_leaves_the_transaction_usable(factory, conversation_id):
    with factory() as s:
        conversation = s.get(Conversation, conversation_id)
        turns.open_turn_request(s, conversation, "key-1", "hello", now=NOW)
        with pytest.raises(sa.exc.IntegrityError):
            with s.begin_nested():
                new_turn_request(s, conversation, key="key-1")  # the same key: UNIQUE
        s.commit()

    assert witness(factory, conversation_id) == (1, 1, NOW)


def test_an_exception_that_leaves_the_session_block_rolls_everything_back(factory, conversation_id):
    with pytest.raises(RuntimeError, match="boom"):
        with factory() as s:
            turns.open_turn_request(s, s.get(Conversation, conversation_id), "key-1", "a", now=NOW)
            raise RuntimeError("boom")

    assert witness(factory, conversation_id) == (0, 0, EARLIER)


# --- open_turn_request is one step ------------------------------------------------


def test_a_failure_after_the_message_and_request_takes_them_back_without_a_rollback(
    factory, conversation_id
):
    def fail(mapper, connection, target):
        raise RuntimeError("the conversation cannot be touched")

    sa.event.listen(Conversation, "before_update", fail)
    try:
        with factory() as s:
            conversation = s.get(Conversation, conversation_id)
            with pytest.raises(RuntimeError, match="cannot be touched"):
                turns.open_turn_request(s, conversation, "key-1", "hello", now=NOW)

            # The caller has not rolled back, and does not have to: the message,
            # the request and the conversation's time were one step.
            assert count(s, Message) == 0
            assert count(s, TurnRequest) == 0
            assert s.scalar(
                sa.select(Conversation.updated_at).where(Conversation.id == conversation_id)
            ) == EARLIER
    finally:
        sa.event.remove(Conversation, "before_update", fail)


def test_a_replay_and_a_conflict_change_nothing(factory, conversation_id):
    with factory() as s:
        turns.open_turn_request(s, s.get(Conversation, conversation_id), "key-1", "hello", now=NOW)
        s.commit()

    with factory() as s:
        conversation = s.get(Conversation, conversation_id)
        later = NOW + timedelta(hours=1)
        _, created = turns.open_turn_request(s, conversation, "key-1", "hello", now=later)
        with pytest.raises(turns.IdempotencyKeyConflict):
            turns.open_turn_request(s, conversation, "key-1", "something else", now=later)
        s.commit()

    assert created is False
    assert witness(factory, conversation_id) == (1, 1, NOW)


def test_losing_a_real_race_for_a_key_leaves_no_orphan_and_a_retry_replays(
    factory, conversation_id, monkeypatch
):
    # Writer A looks the key up and finds nothing; writer B then opens the same
    # request and commits; then A writes. In SQLite's WAL mode, A's transaction
    # read before it wrote, and B committed in between, so A's first write fails at
    # once with "database is locked" (SQLITE_BUSY_SNAPSHOT) instead of meeting the
    # UNIQUE constraint. Either way A must leave nothing behind, B's request must
    # stand, and A, retrying in a new transaction, must get B's request back.
    real_find = turns._find
    lookups = []

    def find_then_let_the_other_writer_win(session, conversation_id, key):
        found = real_find(session, conversation_id, key)
        lookups.append(1)
        if len(lookups) == 1:
            with factory() as b:
                turns.open_turn_request(b, b.get(Conversation, conversation_id), key, "hello", now=NOW)
                b.commit()
        return found

    monkeypatch.setattr(turns, "_find", find_then_let_the_other_writer_win)

    with factory() as a:
        try:
            _, created = turns.open_turn_request(
                a, a.get(Conversation, conversation_id), "key-1", "hello", now=NOW
            )
        except sa.exc.OperationalError as error:
            assert "locked" in str(error.orig)
            a.rollback()
        else:
            assert created is False  # a replay is an acceptable outcome too
            a.commit()

    assert witness(factory, conversation_id) == (1, 1, NOW)

    monkeypatch.undo()
    with factory() as retry:
        _, created = turns.open_turn_request(
            retry, retry.get(Conversation, conversation_id), "key-1", "hello", now=NOW
        )
    assert created is False


# --- the same holds off the file system, and for migrations ------------------------


def test_an_in_memory_database_has_the_same_transactions():
    engine = create_db_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        with factory() as setup:
            conversation = new_conversation(setup, new_user(setup))
            setup.commit()
            conversation_id = conversation.id

        with factory() as s:
            conversation = s.get(Conversation, conversation_id)  # a read
            with s.begin_nested():
                new_message(s, conversation)
            s.rollback()
        with factory() as s:
            assert count(s, Message) == 0

        with factory() as s:
            with s.begin_nested():
                new_message(s, s.get(Conversation, conversation_id))
            s.commit()
        with factory() as s:
            assert count(s, Message) == 1
    finally:
        engine.dispose()


def test_a_failed_upgrade_leaves_no_half_built_schema(tmp_path):
    # SQLite can roll DDL back, but only inside a transaction. Without the BEGIN,
    # every CREATE TABLE before the failing one stayed.
    engine = create_db_engine(f"sqlite:///{tmp_path / 'half.db'}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE turn_requests (id INTEGER)")  # built last

        with pytest.raises(sa.exc.OperationalError, match="already exists"):
            upgrade_database(engine)

        assert set(sa.inspect(engine).get_table_names()) == {"turn_requests"}
    finally:
        engine.dispose()
