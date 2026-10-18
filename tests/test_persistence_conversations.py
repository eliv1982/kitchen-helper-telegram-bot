"""Conversations and their messages: ownership, what a read shows, the context.

The functions under test are persistence.conversations. The turns that fill a
conversation are made with persistence.turns (begin_turn / complete_turn / fail_turn),
which is what the web chat uses, so that what is read here is what is really written.
Every test runs twice (tests/persistence_support.py): on a schema created from the
models and on one built by the migrations.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from house_helper.persistence import conversations as cv
from house_helper.persistence import turns
from house_helper.persistence.models import Conversation, Message, MessageRole, User
from tests.persistence_support import (  # noqa: F401  (fixtures)
    engine,
    factory,
    session,
    utc,
)

NOW = utc(2026, 10, 18, 12)
LEASE = timedelta(seconds=90)
YEAR = timedelta(days=365)


def run(factory, work):
    with factory() as db, db.begin():
        return work(db)


@pytest.fixture
def user_id(factory):
    def make(db):
        user = User()
        db.add(user)
        db.flush()
        return user.id

    return run(factory, make)


@pytest.fixture
def other_user_id(factory):
    def make(db):
        user = User()
        db.add(user)
        db.flush()
        return user.id

    return run(factory, make)


@pytest.fixture
def conversation_id(factory, user_id):
    return run(factory, lambda db: cv.create_conversation(db, user_id, now=NOW)).id


def begin(factory, user_id, conversation_id, key, text, now=NOW, max_context=20):
    return run(
        factory,
        lambda db: turns.begin_turn(
            db, user_id, conversation_id, key, text, clock=lambda: now, lease=LEASE, max_context_messages=max_context
        ),
    )


def finish(factory, claim, reply, now=NOW):
    return run(factory, lambda db: turns.complete_turn(db, claim, reply, truncated=False, clock=lambda: now))


def exchange(factory, user_id, conversation_id, number, now=NOW):
    claim = begin(factory, user_id, conversation_id, f"k-{number}", f"q{number}", now=now)
    finish(factory, claim, f"a{number}", now=now)
    return claim


def fail(factory, user_id, conversation_id, number, now=NOW, code="provider_error"):
    claim = begin(factory, user_id, conversation_id, f"k-{number}", f"q{number}", now=now)
    assert run(factory, lambda db: turns.fail_turn(db, claim, code, clock=lambda: now))
    return claim


def context(factory, conversation_id, current_id, now=NOW, max_messages=20):
    return run(
        factory,
        lambda db: cv.build_context(db, conversation_id, current_id, now=now, max_messages=max_messages),
    )


def texts(items):
    return [(item["role"], item["content"]) for item in items]


def add_message(factory, conversation_id, role, content, created_at, expires_at=None):
    def add(db):
        db.add(
            Message(
                conversation_id=conversation_id,
                role=role,
                content=content,
                created_at=created_at,
                expires_at=expires_at or created_at + YEAR,
            )
        )
        db.flush()

    run(factory, add)


# --- conversations --------------------------------------------------------------------------------------


def test_a_conversation_is_created_empty_for_its_owner(engine, factory, user_id):
    seen = []
    sa.event.listen(engine, "before_cursor_execute", lambda c, cur, st, p, ctx, m: seen.append(st))

    created = run(factory, lambda db: cv.create_conversation(db, user_id, now=NOW))

    assert created.title is None
    assert created.created_at == created.updated_at == NOW
    with factory() as db:
        stored = db.get(Conversation, created.id)
        assert stored.user_id == user_id
    first = next(s for s in seen if not s.startswith(("BEGIN", "PRAGMA")))
    assert first.lstrip().upper().startswith("INSERT")  # a writer starts with a write


def test_conversations_are_listed_newest_activity_first_with_a_total_order(factory, user_id):
    ids = [
        run(factory, lambda db, n=n: cv.create_conversation(db, user_id, now=NOW + timedelta(minutes=n))).id
        for n in (1, 2, 3)
    ]
    tied = [run(factory, lambda db: cv.create_conversation(db, user_id, now=NOW)).id for _ in range(3)]

    page = run(factory, lambda db: cv.list_conversations(db, user_id, limit=50))

    listed = [c.id for c in page.conversations]
    assert listed[:3] == [ids[2], ids[1], ids[0]]
    assert listed[3:] == sorted(tied, key=lambda value: value.int, reverse=True)  # ties by id
    assert page.has_more is False


def test_the_list_is_bounded_and_says_when_there_is_more(factory, user_id):
    for n in range(5):
        run(factory, lambda db, n=n: cv.create_conversation(db, user_id, now=NOW + timedelta(seconds=n)))

    three = run(factory, lambda db: cv.list_conversations(db, user_id, limit=3))
    five = run(factory, lambda db: cv.list_conversations(db, user_id, limit=5))

    assert (len(three.conversations), three.has_more) == (3, True)
    assert (len(five.conversations), five.has_more) == (5, False)
    assert three.conversations == five.conversations[:3]


def test_only_the_owners_conversations_are_listed_or_owned(factory, user_id, other_user_id, conversation_id):
    theirs = run(factory, lambda db: cv.create_conversation(db, other_user_id, now=NOW)).id

    mine_listed = run(factory, lambda db: cv.list_conversations(db, user_id, limit=50))
    theirs_listed = run(factory, lambda db: cv.list_conversations(db, other_user_id, limit=50))

    assert [c.id for c in mine_listed.conversations] == [conversation_id]
    assert [c.id for c in theirs_listed.conversations] == [theirs]
    assert run(factory, lambda db: cv.owns_conversation(db, user_id, conversation_id)) is True
    assert run(factory, lambda db: cv.owns_conversation(db, user_id, theirs)) is False
    assert run(factory, lambda db: cv.owns_conversation(db, user_id, uuid4())) is False


# --- history, as shown -----------------------------------------------------------------------------------


def history(factory, user_id, conversation_id, now=NOW, limit=50, before=None):
    return run(
        factory,
        lambda db: cv.list_messages(db, user_id, conversation_id, now=now, limit=limit, before=before),
    )


def test_the_history_of_a_conversation_that_is_not_the_users_is_not_found(
    factory, user_id, other_user_id, conversation_id
):
    exchange(factory, user_id, conversation_id, 1)

    for owner, target in ((user_id, uuid4()), (other_user_id, conversation_id)):
        with pytest.raises(cv.ConversationNotFound):
            history(factory, owner, target)


def test_the_history_is_oldest_first_and_pages_back(factory, user_id, conversation_id):
    for n in range(1, 5):
        exchange(factory, user_id, conversation_id, n)

    everything = history(factory, user_id, conversation_id)
    newest = history(factory, user_id, conversation_id, limit=3)
    older = history(factory, user_id, conversation_id, limit=3, before=newest.messages[0].id)

    ids = [m.id for m in everything.messages]
    assert ids == sorted(ids) and len(ids) == 8 and everything.has_more is False
    assert [m.id for m in newest.messages] == ids[5:] and newest.has_more is True
    assert [m.id for m in older.messages] == ids[2:5] and older.has_more is True


def test_system_notes_and_expired_messages_are_not_shown(factory, user_id, conversation_id):
    exchange(factory, user_id, conversation_id, 1)
    add_message(factory, conversation_id, MessageRole.SYSTEM_NOTE.value, "reset", NOW)
    old = NOW - timedelta(days=400)  # created 400 days ago: expired 35 days ago
    add_message(factory, conversation_id, MessageRole.USER.value, "забытое", old)
    add_message(factory, conversation_id, MessageRole.ASSISTANT.value, "забытый ответ", old)

    shown = history(factory, user_id, conversation_id)

    assert [(m.role, m.text) for m in shown.messages] == [("user", "q1"), ("assistant", "a1")]
    assert history(factory, user_id, conversation_id, now=NOW + YEAR + timedelta(seconds=1)).messages == []


def test_a_message_is_shown_until_the_instant_it_expires(factory, user_id, conversation_id):
    exchange(factory, user_id, conversation_id, 1)
    expires = NOW + YEAR

    assert len(history(factory, user_id, conversation_id, now=expires - timedelta(microseconds=1)).messages) == 2
    assert history(factory, user_id, conversation_id, now=expires).messages == []


def test_what_each_user_message_looks_like_to_the_person(factory, user_id, conversation_id):
    exchange(factory, user_id, conversation_id, 1)  # completed
    fail(factory, user_id, conversation_id, 2, code="provider_timeout")  # failed
    begin(factory, user_id, conversation_id, "k-3", "q3", now=NOW + timedelta(seconds=1))  # being answered

    shown = history(factory, user_id, conversation_id, now=NOW + timedelta(seconds=2)).messages
    stale = history(factory, user_id, conversation_id, now=NOW + timedelta(minutes=5)).messages

    first, _, second, third = shown
    assert (first.status, first.idempotency_key, first.error_code) == ("completed", None, None)
    assert (second.status, second.idempotency_key, second.error_code) == ("failed", "k-2", "provider_timeout")
    assert (third.status, third.idempotency_key, third.error_code) == ("in_progress", "k-3", None)
    # a request whose worker is gone is, to the person, one that failed
    assert (stale[3].status, stale[3].idempotency_key, stale[3].error_code) == ("failed", "k-3", None)
    assert shown[1].status == "completed" and shown[1].idempotency_key is None  # the assistant's


def test_reading_issues_no_write_and_leaves_the_activity_time_alone(engine, factory, user_id, conversation_id):
    exchange(factory, user_id, conversation_id, 1)
    with factory() as db:
        before = db.get(Conversation, conversation_id).updated_at
    seen = []
    sa.event.listen(engine, "before_cursor_execute", lambda c, cur, st, p, ctx, m: seen.append(st.lstrip().split(None, 1)[0].upper()))

    history(factory, user_id, conversation_id, now=NOW + timedelta(days=3))
    run(factory, lambda db: cv.list_conversations(db, user_id, limit=10))
    context(factory, conversation_id, 2, now=NOW + timedelta(days=3))

    assert set(seen) <= {"BEGIN", "SELECT", "PRAGMA"}
    with factory() as db:
        assert db.get(Conversation, conversation_id).updated_at == before


# --- history, as the AI is shown it -----------------------------------------------------------------------


def test_the_context_is_the_completed_exchanges_and_the_message_being_answered(factory, user_id, conversation_id):
    exchange(factory, user_id, conversation_id, 1)
    exchange(factory, user_id, conversation_id, 2)
    claim = begin(factory, user_id, conversation_id, "k-3", "q3")

    assert texts(claim.context) == [
        ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2"), ("user", "q3")
    ]
    assert claim.context == context(factory, conversation_id, claim.user_message_id)


def test_failed_abandoned_and_unfinished_messages_are_left_out_of_the_context(factory, user_id, conversation_id):
    exchange(factory, user_id, conversation_id, 1)
    fail(factory, user_id, conversation_id, 2)
    begin(factory, user_id, conversation_id, "k-3", "q3", now=NOW + timedelta(seconds=1))  # then its worker dies
    later = NOW + timedelta(minutes=5)

    claim = begin(factory, user_id, conversation_id, "k-4", "q4", now=later)

    assert texts(claim.context) == [("user", "q1"), ("assistant", "a1"), ("user", "q4")]


def test_a_retry_is_answered_with_what_came_before_it_and_nothing_after(factory, user_id, conversation_id):
    exchange(factory, user_id, conversation_id, 1)
    failed = fail(factory, user_id, conversation_id, 2)

    retry = begin(factory, user_id, conversation_id, "k-2", "q2", now=NOW + timedelta(seconds=1))

    assert retry.retry is True and retry.user_message_id == failed.user_message_id
    assert texts(retry.context) == [("user", "q1"), ("assistant", "a1"), ("user", "q2")]
    # The conversation grows after it ...
    finish(factory, retry, "a2", now=NOW + timedelta(seconds=2))
    exchange(factory, user_id, conversation_id, 3, now=NOW + timedelta(seconds=3))
    # ... and the context of the older message still stops at that message.
    assert texts(context(factory, conversation_id, retry.user_message_id)) == [
        ("user", "q1"), ("assistant", "a1"), ("user", "q2"),
    ]


def test_the_context_never_has_more_than_the_bound_including_the_message_being_answered(
    factory, user_id, conversation_id
):
    for n in range(1, 16):
        exchange(factory, user_id, conversation_id, n)
    claim = begin(factory, user_id, conversation_id, "k-16", "q16")

    assert len(claim.context) <= 20
    assert claim.context[0]["role"] == "user" and claim.context[-1] == {"role": "user", "content": "q16"}
    assert [c["content"] for c in claim.context if c["role"] == "user"] == [f"q{n}" for n in range(7, 17)]
    roles = [c["role"] for c in claim.context]
    assert roles == ["user", "assistant"] * 9 + ["user"]


@pytest.mark.parametrize(
    ("bound", "expected"),
    [(1, ["q9"]), (2, ["q9"]), (3, ["q8", "a8", "q9"]), (4, ["q8", "a8", "q9"]), (5, ["q7", "a7", "q8", "a8", "q9"])],
)
def test_a_smaller_bound_still_starts_on_a_question(factory, user_id, conversation_id, bound, expected):
    for n in range(1, 9):
        exchange(factory, user_id, conversation_id, n)
    claim = begin(factory, user_id, conversation_id, "k-9", "q9", max_context=bound)

    assert [c["content"] for c in claim.context] == expected


def test_messages_that_expired_are_left_out_of_the_context_and_never_leave_an_answer_without_its_question(
    factory, user_id, conversation_id
):
    exchange(factory, user_id, conversation_id, 1, now=NOW)
    exchange(factory, user_id, conversation_id, 2, now=NOW + timedelta(days=10))
    after_the_first_but_before_the_second = NOW + YEAR + timedelta(days=1)

    claim = begin(factory, user_id, conversation_id, "k-3", "q3", now=after_the_first_but_before_the_second)

    assert texts(claim.context) == [("user", "q2"), ("assistant", "a2"), ("user", "q3")]


def test_an_answer_whose_question_expired_first_is_not_sent_alone(factory, user_id, conversation_id):
    exchange(factory, user_id, conversation_id, 1, now=NOW)
    # The answer outlives its question (as it does by the seconds a model takes to
    # answer; here by an hour, to be certain).
    with factory() as db, db.begin():
        db.execute(
            sa.update(Message)
            .where(Message.content == "a1")
            .values(created_at=NOW + timedelta(hours=1), expires_at=NOW + timedelta(hours=1) + YEAR)
        )
    later = NOW + YEAR + timedelta(minutes=1)  # q1 has expired, a1 has not

    claim = begin(factory, user_id, conversation_id, "k-2", "q2", now=later)

    assert texts(claim.context) == [("user", "q2")]


def test_the_latest_reset_marker_ends_the_context(factory, user_id, conversation_id):
    exchange(factory, user_id, conversation_id, 1)
    add_message(factory, conversation_id, MessageRole.SYSTEM_NOTE.value, "reset", NOW)
    exchange(factory, user_id, conversation_id, 2)
    add_message(factory, conversation_id, MessageRole.SYSTEM_NOTE.value, "reset again", NOW)

    claim = begin(factory, user_id, conversation_id, "k-3", "q3")

    assert texts(claim.context) == [("user", "q3")]


def test_conversations_have_separate_contexts(factory, user_id, conversation_id):
    other = run(factory, lambda db: cv.create_conversation(db, user_id, now=NOW)).id
    exchange(factory, user_id, conversation_id, 1)
    claim = begin(factory, user_id, other, "k-1", "q-other")

    assert texts(claim.context) == [("user", "q-other")]
