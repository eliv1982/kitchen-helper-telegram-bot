"""What a turn request may point at, and that it never exists without its input.

Two findings of the independent audit are pinned here:

  * a request could reference another user's (or another conversation's)
    message, a message of the wrong role, or a message already used elsewhere;
  * a request could exist without any recoverable input.

Every test runs against a schema created from the models and against one built
by the migrations (the `engine` fixture), and the reference tests run twice
more: once writing the request through the ORM and once as plain SQL, which
skips every application-side guard and meets only the database. SQLite reports
a foreign key violation without naming the constraint, so each negative test
differs from a positive control that succeeds in exactly one respect, and the
failure is therefore the rule under test.
"""

import hashlib
import warnings
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from house_helper.persistence import turns
from house_helper.persistence.models import (
    MESSAGE_RETENTION,
    Conversation,
    Message,
    TurnRequest,
)
from tests.persistence_support import (  # noqa: F401  (fixtures)
    count,
    engine,
    factory,
    insert_turn,
    new_conversation,
    new_message,
    new_user,
    raw_update,
    rejected,
    session,
    utc,
)

NOW = utc(2026, 10, 16, 12)
HOW = pytest.mark.parametrize("how", ["orm", "sql"])


@pytest.fixture
def world(session):
    """Two users. Each has a conversation with a user message, an assistant
    message and a context-reset note; the first also has a second conversation."""
    mine, theirs = new_user(session), new_user(session)

    def conversation_with_messages(owner):
        conversation = new_conversation(session, owner)
        return SimpleNamespace(
            conversation=conversation,
            asked=new_message(session, conversation, role="user", content="question"),
            answered=new_message(session, conversation, role="assistant", content="answer"),
            note=new_message(session, conversation, role="system_note", content="reset"),
        )

    world = SimpleNamespace(
        mine=conversation_with_messages(mine),
        mine_elsewhere=conversation_with_messages(mine),
        theirs=conversation_with_messages(theirs),
    )
    session.commit()
    return world


# --- the links a request may have -------------------------------------------------


@HOW
def test_a_request_links_messages_of_its_own_conversation(session, world, how):
    insert_turn(
        session, how, world.mine.conversation, world.mine.asked.id,
        assistant_message_id=world.mine.answered.id,
    )
    session.commit()

    turn = session.scalar(sa.select(TurnRequest))
    assert turn.user_message.content == "question"
    assert turn.assistant_message.content == "answer"


@HOW
def test_the_assistant_link_may_be_empty_until_the_reply_is_committed(session, world, how):
    insert_turn(session, how, world.mine.conversation, world.mine.asked.id)
    session.commit()

    assert session.scalar(sa.select(TurnRequest)).assistant_message_id is None


@HOW
@pytest.mark.parametrize("owner", ["another user", "the same user"])
def test_a_request_cannot_take_its_input_from_another_conversation(session, world, how, owner):
    stranger = world.theirs if owner == "another user" else world.mine_elsewhere

    with rejected(session, "FOREIGN KEY"):
        insert_turn(session, how, world.mine.conversation, stranger.asked.id)


@HOW
@pytest.mark.parametrize("owner", ["another user", "the same user"])
def test_a_request_cannot_link_an_answer_from_another_conversation(session, world, how, owner):
    stranger = world.theirs if owner == "another user" else world.mine_elsewhere

    with rejected(session, "FOREIGN KEY"):
        insert_turn(
            session, how, world.mine.conversation, world.mine.asked.id,
            assistant_message_id=stranger.answered.id,
        )


@HOW
def test_a_request_cannot_be_filed_under_a_conversation_its_messages_are_not_in(
    session, world, how
):
    # The conversation is the request's own; its messages must be in it. The
    # request below says "mine" but links messages of another user's conversation.
    with rejected(session, "FOREIGN KEY"):
        insert_turn(
            session, how, world.mine.conversation, world.theirs.asked.id,
            assistant_message_id=world.theirs.answered.id,
        )


@HOW
@pytest.mark.parametrize("message", ["answered", "note"])
def test_the_input_must_be_a_user_message(session, world, how, message):
    wrong = getattr(world.mine, message)

    with rejected(session, "FOREIGN KEY"):
        insert_turn(session, how, world.mine.conversation, wrong.id)


@HOW
@pytest.mark.parametrize("message", ["asked", "note"])
def test_the_answer_must_be_an_assistant_message(session, world, how, message):
    wrong = getattr(world.mine, message)

    with rejected(session, "FOREIGN KEY"):
        insert_turn(
            session, how, world.mine.conversation, world.mine.asked.id,
            assistant_message_id=wrong.id,
        )


@HOW
def test_a_request_cannot_use_one_message_as_both_input_and_answer(session, world, how):
    with rejected(session, "FOREIGN KEY"):
        insert_turn(
            session, how, world.mine.conversation, world.mine.asked.id,
            assistant_message_id=world.mine.asked.id,
        )


@HOW
def test_a_link_to_a_message_that_does_not_exist_is_refused(session, world, how):
    with rejected(session, "FOREIGN KEY"):
        insert_turn(session, how, world.mine.conversation, 999_999)
    with rejected(session, "FOREIGN KEY"):
        insert_turn(
            session, how, world.mine.conversation, world.mine.asked.id,
            assistant_message_id=999_999,
        )


@HOW
def test_a_request_must_belong_to_a_conversation_that_exists(session, world, how):
    import uuid

    with rejected(session, "FOREIGN KEY"):
        insert_turn(
            session, how, world.mine.conversation, world.mine.asked.id,
            conversation_id=uuid.uuid4(),
        )


@HOW
def test_a_message_is_the_input_of_at_most_one_request(session, world, how):
    insert_turn(session, how, world.mine.conversation, world.mine.asked.id, idempotency_key="a")

    with rejected(session, "turn_requests.user_message_id"):
        insert_turn(session, how, world.mine.conversation, world.mine.asked.id, idempotency_key="b")


@HOW
def test_a_message_is_the_answer_of_at_most_one_request(session, world, how):
    other_input = new_message(session, world.mine.conversation, role="user")
    insert_turn(
        session, how, world.mine.conversation, world.mine.asked.id,
        idempotency_key="a", assistant_message_id=world.mine.answered.id,
    )

    with rejected(session, "turn_requests.assistant_message_id"):
        insert_turn(
            session, how, world.mine.conversation, other_input.id,
            idempotency_key="b", assistant_message_id=world.mine.answered.id,
        )


@HOW
@pytest.mark.parametrize(
    "column, value",
    [("user_message_role", "assistant"), ("assistant_message_role", "user")],
)
def test_the_role_a_link_expects_cannot_be_changed_to_match_the_wrong_message(
    session, world, how, column, value
):
    # The *_role columns only exist to be part of the foreign keys. If a writer
    # could set them freely, pointing the link at a message of any role and
    # naming that role would defeat the check, so a CHECK pins each of them.
    wrong = world.mine.answered if column == "user_message_role" else world.mine.asked
    fields = {column: value}
    if column == "assistant_message_role":
        fields["assistant_message_id"] = wrong.id
    user_message = wrong if column == "user_message_role" else world.mine.asked

    with rejected(session, f"ck_turn_requests_{column}_is_"):
        insert_turn(session, how, world.mine.conversation, user_message.id, **fields)


# --- a request always has its input -----------------------------------------------

LEASE = {"processing_started_at": NOW, "lease_expires_at": NOW + timedelta(minutes=2)}
REPLY = {"outcome": "reply", "text": "answer"}
EVERY_STATUS = {
    "pending": {},
    "processing": {"status": "processing", **LEASE},
    "committing": {"status": "committing", "result_payload": REPLY, **LEASE},
    "completed": {"status": "completed", "result_payload": REPLY, "finished_at": NOW},
    "failed (generation)": {
        "status": "failed", "failure_kind": "generation", "error_code": "x", "finished_at": NOW,
    },
    "failed (persistence)": {
        "status": "failed", "failure_kind": "persistence", "error_code": "x",
        "result_payload": REPLY, "finished_at": NOW,
    },
    "cancelled": {"status": "cancelled", "finished_at": NOW},
}


@HOW
@pytest.mark.parametrize("fields", EVERY_STATUS.values(), ids=EVERY_STATUS)
def test_no_request_exists_without_its_input_in_any_status(session, world, how, fields):
    # The status is valid and complete, so the missing input is the only fault.
    insert_turn(session, how, world.mine.conversation, world.mine.asked.id, **fields)
    session.rollback()

    with rejected(session, "NOT NULL constraint failed: turn_requests.user_message_id"):
        insert_turn(session, how, world.mine.conversation, None, **fields)


# --- a linked message cannot be taken away or moved --------------------------------


def _linked(session, world, **fields):
    insert_turn(
        session, "orm", world.mine.conversation, world.mine.asked.id,
        assistant_message_id=world.mine.answered.id, **fields,
    )
    session.commit()


@pytest.mark.parametrize("message", ["asked", "answered"])
def test_a_message_that_a_request_links_cannot_be_deleted(session, world, message):
    _linked(session, world)

    with rejected(session, "FOREIGN KEY"):
        session.execute(sa.delete(Message).where(Message.id == getattr(world.mine, message).id))

    turn = session.scalar(sa.select(TurnRequest))
    assert turn.user_message.content == "question"  # nothing was lost
    assert turn.assistant_message.content == "answer"


def test_deleting_a_linked_message_through_the_orm_is_refused_too(session, world):
    _linked(session, world)

    with rejected(session, "FOREIGN KEY"):
        session.delete(session.get(Message, world.mine.asked.id))


def test_a_message_that_no_request_links_can_be_deleted(session, world):
    _linked(session, world)

    session.execute(sa.delete(Message).where(Message.id == world.mine.note.id))
    session.commit()

    assert session.get(Message, world.mine.note.id) is None


def test_deleting_the_request_first_frees_its_messages_for_the_purge(session, world):
    # The order the purge job has to follow: requests, then messages.
    _linked(session, world)

    session.execute(sa.delete(TurnRequest))
    session.execute(sa.delete(Message).where(Message.conversation_id == world.mine.conversation.id))
    session.commit()

    assert count(session, TurnRequest) == 0
    assert session.scalar(
        sa.select(sa.func.count()).select_from(Message).where(
            Message.conversation_id == world.mine.conversation.id
        )
    ) == 0


def test_a_linked_message_cannot_be_moved_to_another_conversation(session, world):
    _linked(session, world)

    with rejected(session, "FOREIGN KEY"):
        raw_update(
            session, "messages", "id = ?", (world.mine.asked.id,),
            conversation_id=world.mine_elsewhere.conversation.id.hex,
        )


def test_a_linked_message_cannot_change_its_role(session, world):
    _linked(session, world)

    with rejected(session, "FOREIGN KEY"):
        raw_update(session, "messages", "id = ?", (world.mine.asked.id,), role="system_note")


def test_an_unlinked_message_can_still_be_changed(session, world):
    # Control for the two above: the refusal is about the link, not the columns.
    raw_update(session, "messages", "id = ?", (world.mine.note.id,), role="user")
    session.commit()
    session.expire_all()  # raw SQL does not refresh objects the session already holds

    assert session.get(Message, world.mine.note.id).role == "user"


def test_a_stored_request_cannot_be_re_pointed_through_the_orm(session, world):
    _linked(session, world)
    other_input = new_message(session, world.mine.conversation, role="user")
    turn = session.scalar(sa.select(TurnRequest))

    turn.user_message_id = other_input.id
    with pytest.raises(ValueError, match="user_message_id cannot change once stored"):
        session.flush()


@pytest.mark.parametrize(
    "attribute, value",
    [("idempotency_key", "another"), ("request_fingerprint", "0" * 64)],
)
def test_the_identity_of_a_stored_request_cannot_change_through_the_orm(
    session, world, attribute, value
):
    _linked(session, world)
    turn = session.scalar(sa.select(TurnRequest))

    setattr(turn, attribute, value)
    with pytest.raises(ValueError, match=f"{attribute} cannot change once stored"):
        session.flush()


# --- deleting along ownership still works -----------------------------------------


@pytest.mark.parametrize("by", ["conversation", "user"])
def test_deleting_what_owns_linked_messages_removes_them_with_their_requests(session, world, by):
    # The conversation cascade deletes messages and requests in one statement;
    # the check on the links runs after both are gone.
    _linked(session, world)
    if by == "conversation":
        session.execute(sa.delete(Conversation).where(Conversation.id == world.mine.conversation.id))
    else:
        session.delete(session.get(Conversation, world.mine.conversation.id).user)
    session.commit()

    assert count(session, TurnRequest) == 0
    assert (
        session.scalar(
            sa.select(sa.func.count()).select_from(Message).where(
                Message.conversation_id == world.mine.conversation.id
            )
        )
        == 0
    )
    assert count(session, Message) > 0  # the other conversations' messages are untouched


@pytest.mark.parametrize("loaded", [False, True], ids=["children not loaded", "children loaded"])
@pytest.mark.parametrize("by", ["conversation", "user"])
def test_the_orm_deletes_an_owner_whose_linked_messages_and_requests_are_in_memory(
    session, world, by, loaded
):
    # With the children in the session the ORM deletes them itself, in an order
    # of its own. The links refuse a message that a request still points at, so
    # the ORM must not remove the messages first.
    _linked(session, world)
    mine = world.mine.conversation.id  # read before the objects are deleted and expired
    elsewhere = world.mine_elsewhere.conversation.id
    theirs = world.theirs.conversation.id
    conversation = session.get(Conversation, mine)
    owner = conversation.user
    if loaded:
        assert len(conversation.messages) == 3 and len(conversation.turn_requests) == 1
        assert len(owner.conversations) == 2
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a DELETE that matched nothing would only warn
        session.delete(conversation if by == "conversation" else owner)
        session.commit()

    def rows(model, column, value):
        return session.scalar(
            sa.select(sa.func.count()).select_from(model).where(column == value)
        )

    session.expire_all()
    assert rows(Conversation, Conversation.id, mine) == 0
    assert rows(Message, Message.conversation_id, mine) == 0
    assert rows(TurnRequest, TurnRequest.conversation_id, mine) == 0
    # the user's other conversation goes with the user, and only then
    assert rows(Conversation, Conversation.id, elsewhere) == (1 if by == "conversation" else 0)
    assert rows(Message, Message.conversation_id, elsewhere) == (3 if by == "conversation" else 0)
    assert rows(Conversation, Conversation.id, theirs) == 1


# --- opening a request: input and request come into being together ----------------


@pytest.fixture
def conversation(session):
    return new_conversation(session, new_user(session))


def test_opening_a_request_stores_the_message_and_the_request_together(session, conversation):
    turn, created = turns.open_turn_request(session, conversation, "key-1", "сколько варить яйца", now=NOW)
    session.commit()

    message = session.get(Message, turn.user_message_id)
    assert created is True
    assert (message.role, message.content, message.conversation_id) == (
        "user", "сколько варить яйца", conversation.id,
    )
    assert (turn.status, turn.attempt_count, turn.assistant_message_id) == ("pending", 0, None)
    assert turn.request_fingerprint == hashlib.sha256("сколько варить яйца".encode()).hexdigest()


def test_the_input_is_recoverable_from_the_stored_message_after_a_restart(factory):
    with factory() as s:
        conversation = new_conversation(s, new_user(s))
        turn, _ = turns.open_turn_request(s, conversation, "key-1", "recover me", now=NOW)
        turn_id = turn.id
        s.commit()

    with factory() as s:  # a new process, a new session: nothing carried over in memory
        turn = s.get(TurnRequest, turn_id)
        assert turn.user_message.content == "recover me"
        assert hashlib.sha256(turn.user_message.content.encode()).hexdigest() == turn.request_fingerprint


def test_the_request_and_its_message_start_their_retention_at_the_same_instant(session, conversation):
    turn, _ = turns.open_turn_request(session, conversation, "key-1", "hello", now=NOW)
    message = session.get(Message, turn.user_message_id)

    assert turn.created_at == message.created_at == NOW
    assert turn.expires_at == message.expires_at == NOW + MESSAGE_RETENTION
    assert conversation.updated_at == NOW


def test_the_text_is_stored_once_and_not_copied_into_the_request(session, conversation):
    turn, _ = turns.open_turn_request(session, conversation, "key-1", "a" * 5000, now=NOW)

    assert turn.result_payload is None  # nothing but the message holds the text
    assert not any(
        isinstance(value, str) and "a" * 100 in value
        for column, value in vars(turn).items()
        if column != "_sa_instance_state"
    )


def test_repeating_a_key_with_the_same_text_replays_without_a_second_message(session, conversation):
    first, created = turns.open_turn_request(session, conversation, "key-1", "hello", now=NOW)
    again, created_again = turns.open_turn_request(
        session, conversation, "key-1", "hello", now=NOW + timedelta(minutes=5)
    )

    assert (created, created_again) == (True, False)
    assert again.id == first.id
    assert count(session, Message) == 1 and count(session, TurnRequest) == 1
    assert conversation.updated_at == NOW  # a replay is not new activity


def test_repeating_a_key_with_other_text_is_a_conflict_that_stores_nothing(session, conversation):
    turns.open_turn_request(session, conversation, "key-1", "hello", now=NOW)

    with pytest.raises(turns.IdempotencyKeyConflict):
        turns.open_turn_request(session, conversation, "key-1", "something else", now=NOW)

    assert count(session, Message) == 1 and count(session, TurnRequest) == 1


def test_losing_the_race_for_a_key_leaves_no_orphan_message(session, conversation, monkeypatch):
    # The lookup finds nothing (the other writer has not committed yet), the
    # insert then meets the winner's row. The message written a moment earlier
    # must go with the failed attempt.
    winner, _ = turns.open_turn_request(session, conversation, "key-1", "hello", now=NOW)
    real_find = turns._find
    calls = []

    def blind_the_first_time(*args):
        calls.append(1)
        return None if len(calls) == 1 else real_find(*args)

    monkeypatch.setattr(turns, "_find", blind_the_first_time)

    turn, created = turns.open_turn_request(session, conversation, "key-1", "hello", now=NOW)

    assert (turn.id, created) == (winner.id, False)
    assert count(session, Message) == 1  # not 2
    session.commit()  # and the session is still sound


def test_a_failure_after_the_message_was_written_takes_the_message_back(session, conversation, monkeypatch):
    monkeypatch.setattr(turns, "request_fingerprint", lambda text: "too short")

    with pytest.raises(sa.exc.IntegrityError, match="ck_turn_requests_fingerprint_length"):
        turns.open_turn_request(session, conversation, "key-1", "hello", now=NOW)

    assert count(session, Message) == 0 and count(session, TurnRequest) == 0
    monkeypatch.undo()
    turns.open_turn_request(session, conversation, "key-1", "hello", now=NOW)  # and it can be retried
    assert count(session, Message) == 1


@pytest.mark.parametrize("key", ["", " ", "has space", "tab\t", "new\nline", "x" * 129, "ключ", None, 7])
def test_opening_a_request_with_an_invalid_key_stores_nothing(session, conversation, key):
    with pytest.raises(ValueError):
        turns.open_turn_request(session, conversation, key, "hello", now=NOW)

    assert count(session, Message) == 0 and count(session, TurnRequest) == 0


@pytest.mark.parametrize("text", ["", None, b"hello", 5])
def test_opening_a_request_without_text_stores_nothing(session, conversation, text):
    with pytest.raises(ValueError, match="needs text"):
        turns.open_turn_request(session, conversation, "key-1", text, now=NOW)

    assert count(session, Message) == 0 and count(session, TurnRequest) == 0


def test_a_request_in_flight_keeps_its_input_when_a_purge_forgets_it(session, conversation):
    # Retention and deletion must not orphan work that is not finished: a purge
    # that deletes the message of a pending request is refused, and
    # the request and its input are still there.
    turn, _ = turns.open_turn_request(session, conversation, "key-1", "still wanted", now=NOW)
    session.commit()
    message_id = turn.user_message_id

    with rejected(session, "FOREIGN KEY"):
        session.execute(sa.delete(Message).where(Message.expires_at <= NOW + MESSAGE_RETENTION))

    assert session.get(TurnRequest, turn.id).status == "pending"
    assert session.get(Message, message_id).content == "still wanted"
