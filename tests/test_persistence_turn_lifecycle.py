"""The smallest turn lifecycle (persistence.turns): begin, complete, fail.

What is pinned here is the transactional behaviour the web chat leans on: that a
writer writes first, that ownership is decided by that first write, that every
transition is a compare-and-swap that only the attempt holding the request can win,
that a refusal leaves nothing behind, and (with real threads on a real SQLite file)
that two callers for one conversation never both get to answer. It is not the full
R5 lifecycle, whose pieces this module does not have (see turns.py).
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from house_helper.persistence import turns
from house_helper.persistence.conversations import ConversationNotFound, create_conversation
from house_helper.persistence.models import Conversation, FailureKind, Message, TurnRequest, TurnStatus, User
from house_helper.persistence.turns import (
    Claim,
    CompletedTurn,
    ConversationBusy,
    IdempotencyKeyConflict,
    LeaseLost,
    TurnAttempted,
    TurnInFlight,
    TurnSuperseded,
    TurnUnusable,
)
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


def new_user(db):
    user = User()
    db.add(user)
    db.flush()
    return user.id


@pytest.fixture
def user_id(factory):
    return run(factory, new_user)


@pytest.fixture
def conversation_id(factory, user_id):
    return run(factory, lambda db: create_conversation(db, user_id, now=NOW)).id


def begin(factory, user_id, conversation_id, key="k-1", text="q1", now=NOW, lease=LEASE, **options):
    return run(
        factory,
        lambda db: turns.begin_turn(
            db, user_id, conversation_id, key, text, clock=lambda: now, lease=lease, max_context_messages=20, **options
        ),
    )


def complete(factory, claim, reply="a1", now=NOW, truncated=False):
    return run(factory, lambda db: turns.complete_turn(db, claim, reply, truncated=truncated, clock=lambda: now))


def fail(factory, claim, code="provider_error", now=NOW):
    return run(factory, lambda db: turns.fail_turn(db, claim, code, clock=lambda: now))


def turn_rows(factory):
    with factory() as db:
        return list(db.scalars(sa.select(TurnRequest).order_by(TurnRequest.user_message_id)))


def count(factory, model):
    with factory() as db:
        return db.scalar(sa.select(sa.func.count()).select_from(model))


def updated_at(factory, conversation_id):
    with factory() as db:
        return db.get(Conversation, conversation_id).updated_at


def snapshot(factory):
    """Everything a request could have touched."""
    with factory() as db:
        return (
            [(m.id, m.role, m.content) for m in db.scalars(sa.select(Message).order_by(Message.id))],
            [
                (t.idempotency_key, t.status, t.attempt_count, t.error_code, t.lease_expires_at, t.updated_at)
                for t in db.scalars(sa.select(TurnRequest).order_by(TurnRequest.user_message_id))
            ],
            [(c.id, c.updated_at) for c in db.scalars(sa.select(Conversation).order_by(Conversation.id))],
        )


# --- the first call ------------------------------------------------------------------------------------


def test_a_new_message_is_stored_with_its_request_and_claimed(factory, user_id, conversation_id):
    later = NOW + timedelta(minutes=5)

    claim = begin(factory, user_id, conversation_id, "k-1", "Что из яиц?", now=later)

    assert isinstance(claim, Claim)
    assert (claim.attempt, claim.retry, claim.conversation_id) == (1, False, conversation_id)
    assert claim.context == [{"role": "user", "content": "Что из яиц?"}]
    (turn,) = turn_rows(factory)
    assert turn.id == claim.turn_id
    assert (turn.status, turn.attempt_count, turn.idempotency_key) == ("processing", 1, "k-1")
    assert turn.processing_started_at == later and turn.lease_expires_at == later + LEASE
    assert turn.finished_at is None and turn.error_code is None and turn.result_payload is None
    assert turn.assistant_message_id is None
    with factory() as db:
        message = db.get(Message, turn.user_message_id)
    assert (message.role, message.content, message.created_at) == ("user", "Что из яиц?", later)
    assert claim.user_message_id == message.id and claim.user_message_created_at == later
    assert updated_at(factory, conversation_id) == later


def test_the_365_day_deadline_is_recorded_when_the_message_and_its_request_are_made(
    factory, user_id, conversation_id
):
    later = NOW + timedelta(hours=7)

    claim = begin(factory, user_id, conversation_id, now=later)
    complete(factory, claim, now=later + timedelta(seconds=4))

    (turn,) = turn_rows(factory)
    with factory() as db:
        user, assistant = db.scalars(sa.select(Message).order_by(Message.id)).all()
    assert turn.expires_at == turn.created_at + YEAR == later + YEAR
    assert user.expires_at == later + YEAR
    assert assistant.expires_at == later + timedelta(seconds=4) + YEAR  # from its own creation


def test_a_writer_starts_with_a_write_and_the_write_decides_ownership(engine, factory, user_id, conversation_id):
    seen = []
    sa.event.listen(
        engine,
        "before_cursor_execute",
        lambda c, cur, st, p, ctx, m: seen.append(st.lstrip().split(None, 1)[0].upper()),
    )

    claim = begin(factory, user_id, conversation_id)
    complete(factory, claim)
    claim2 = begin(factory, user_id, conversation_id, "k-2", "q2")
    fail(factory, claim2)
    with pytest.raises(ConversationNotFound):
        begin(factory, user_id, uuid4(), "k-9", "q9")

    # Each transaction: BEGIN, then an UPDATE, before any SELECT (SQLite takes the write
    # lock there and then; PostgreSQL the row lock).
    transactions = [list(group) for group in _split(seen)]
    assert len(transactions) >= 4
    for statements in transactions:
        if "UPDATE" in statements or "INSERT" in statements:
            assert statements[0] in {"UPDATE", "INSERT"}, statements


def _split(statements):
    group = []
    for statement in statements:
        if statement == "BEGIN":
            if group:
                yield group
            group = []
        elif statement != "PRAGMA":
            group.append(statement)
    if group:
        yield group


def test_a_conversation_that_is_not_the_users_or_does_not_exist_changes_nothing(factory, user_id, conversation_id):
    stranger = run(factory, new_user)
    before = snapshot(factory)

    for owner, target in ((stranger, conversation_id), (user_id, uuid4()), (stranger, uuid4())):
        with pytest.raises(ConversationNotFound):
            begin(factory, owner, target)

    assert snapshot(factory) == before


# --- the same key again ------------------------------------------------------------------------------------


def test_a_completed_request_is_returned_as_it_was_answered_and_nothing_changes(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id, "k-1", "q1")
    done = complete(factory, claim, "a1", now=NOW + timedelta(seconds=3), truncated=True)
    before = snapshot(factory)

    again = begin(factory, user_id, conversation_id, "k-1", "q1", now=NOW + timedelta(hours=1))

    assert isinstance(again, CompletedTurn) and again == done
    assert again.truncated is True
    assert (again.user_message.text, again.assistant_message.text) == ("q1", "a1")
    assert snapshot(factory) == before  # not even the activity time moved


def test_the_same_key_for_a_different_text_is_a_conflict_and_changes_nothing(factory, user_id, conversation_id):
    complete(factory, begin(factory, user_id, conversation_id, "k-1", "q1"))
    before = snapshot(factory)

    with pytest.raises(IdempotencyKeyConflict):
        begin(factory, user_id, conversation_id, "k-1", "something else", now=NOW + timedelta(minutes=1))

    assert snapshot(factory) == before


def test_the_conflict_is_decided_before_anything_about_its_state(factory, user_id, conversation_id):
    begin(factory, user_id, conversation_id, "k-1", "q1")  # being answered

    with pytest.raises(IdempotencyKeyConflict):
        begin(factory, user_id, conversation_id, "k-1", "other", now=NOW + timedelta(seconds=1))


def test_the_same_key_while_it_is_being_answered_is_in_flight(factory, user_id, conversation_id):
    begin(factory, user_id, conversation_id, "k-1", "q1")
    before = snapshot(factory)

    with pytest.raises(TurnInFlight):
        begin(factory, user_id, conversation_id, "k-1", "q1", now=NOW + timedelta(seconds=89))

    assert snapshot(factory) == before


def test_another_key_while_one_is_being_answered_is_busy_and_changes_nothing(factory, user_id, conversation_id):
    begin(factory, user_id, conversation_id, "k-1", "q1")
    before = snapshot(factory)

    with pytest.raises(ConversationBusy):
        begin(factory, user_id, conversation_id, "k-2", "q2", now=NOW + timedelta(seconds=30))

    assert snapshot(factory) == before  # no message, no request, no activity


def test_the_busy_conversation_is_free_again_when_the_request_ends_or_its_lease_does(factory, user_id, conversation_id):
    first = begin(factory, user_id, conversation_id, "k-1", "q1")
    complete(factory, first, "a1", now=NOW + timedelta(seconds=2))
    second = begin(factory, user_id, conversation_id, "k-2", "q2", now=NOW + timedelta(seconds=3))
    fail(factory, second, now=NOW + timedelta(seconds=4))
    third = begin(factory, user_id, conversation_id, "k-3", "q3", now=NOW + timedelta(seconds=5))
    assert isinstance(third, Claim)

    # the third is abandoned: busy up to the second its lease runs out
    with pytest.raises(ConversationBusy):
        begin(factory, user_id, conversation_id, "k-4", "q4", now=NOW + timedelta(seconds=5) + LEASE - timedelta(microseconds=1))
    fourth = begin(factory, user_id, conversation_id, "k-4", "q4", now=NOW + timedelta(seconds=5) + LEASE)
    assert isinstance(fourth, Claim)


def test_busy_is_per_conversation(factory, user_id, conversation_id):
    other = run(factory, lambda db: create_conversation(db, user_id, now=NOW)).id
    begin(factory, user_id, conversation_id, "k-1", "q1")

    assert isinstance(begin(factory, user_id, other, "k-1", "q1"), Claim)  # the key is per conversation too


# --- failing and trying again ----------------------------------------------------------------------------------


def test_a_failed_request_keeps_its_message_and_says_why(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)
    before_activity = updated_at(factory, conversation_id)

    assert fail(factory, claim, "provider_rate_limited", now=NOW + timedelta(seconds=2)) is True

    (turn,) = turn_rows(factory)
    assert (turn.status, turn.failure_kind, turn.error_code) == (
        TurnStatus.FAILED.value, FailureKind.GENERATION.value, "provider_rate_limited",
    )
    assert turn.finished_at == NOW + timedelta(seconds=2) and turn.result_payload is None
    assert count(factory, Message) == 1  # the user's message stays
    assert updated_at(factory, conversation_id) == before_activity  # a failure is not activity


def test_a_failed_request_is_claimed_again_by_its_key(factory, user_id, conversation_id):
    first = begin(factory, user_id, conversation_id)
    fail(factory, first, "provider_timeout", now=NOW + timedelta(seconds=2))
    later = NOW + timedelta(minutes=1)

    second = begin(factory, user_id, conversation_id, now=later)

    assert isinstance(second, Claim)
    assert (second.attempt, second.retry) == (2, True)
    assert second.turn_id == first.turn_id and second.user_message_id == first.user_message_id
    (turn,) = turn_rows(factory)
    assert (turn.status, turn.attempt_count) == ("processing", 2)
    assert (turn.failure_kind, turn.error_code, turn.finished_at) == (None, None, None)
    assert turn.lease_expires_at == later + LEASE
    assert count(factory, Message) == 1  # the same message, not a second one
    assert updated_at(factory, conversation_id) == NOW  # retrying is not new activity either


def test_a_request_whose_lease_ran_out_is_claimed_again_and_the_old_attempt_is_refused(
    factory, user_id, conversation_id
):
    first = begin(factory, user_id, conversation_id)
    after = NOW + LEASE  # the lease ends at this very instant

    second = begin(factory, user_id, conversation_id, now=after)

    assert (second.attempt, second.retry) == (2, True)
    with pytest.raises(LeaseLost):
        complete(factory, first, "late", now=after + timedelta(seconds=1))
    assert fail(factory, first, "late", now=after + timedelta(seconds=1)) is False
    assert count(factory, Message) == 1  # the late answer left nothing behind
    (turn,) = turn_rows(factory)
    assert (turn.status, turn.attempt_count) == ("processing", 2)
    complete(factory, second, "a1", now=after + timedelta(seconds=2))
    assert turn_rows(factory)[0].status == "completed"


def test_a_late_failure_does_not_undo_a_completion(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)
    complete(factory, claim)

    assert fail(factory, claim, "late") is False

    assert turn_rows(factory)[0].status == "completed"


def test_a_request_cannot_be_completed_twice(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)
    complete(factory, claim, "a1")

    with pytest.raises(LeaseLost):
        complete(factory, claim, "a1 again")

    assert count(factory, Message) == 2


def test_a_failed_request_cannot_be_completed_by_the_attempt_that_failed_it(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)
    fail(factory, claim)

    with pytest.raises(LeaseLost):
        complete(factory, claim, "a1")

    assert turn_rows(factory)[0].status == "failed" and count(factory, Message) == 1


def test_a_failed_request_cannot_be_retried_once_a_newer_one_exists(factory, user_id, conversation_id):
    first = begin(factory, user_id, conversation_id, "k-1", "q1")
    fail(factory, first)
    second = begin(factory, user_id, conversation_id, "k-2", "q2", now=NOW + timedelta(seconds=1))
    complete(factory, second, "a2", now=NOW + timedelta(seconds=2))
    before = snapshot(factory)

    with pytest.raises(TurnSuperseded):
        begin(factory, user_id, conversation_id, "k-1", "q1", now=NOW + timedelta(seconds=3))

    assert snapshot(factory) == before


def test_an_abandoned_request_cannot_be_retried_once_a_newer_one_exists(factory, user_id, conversation_id):
    begin(factory, user_id, conversation_id, "k-1", "q1")  # its worker dies
    later = NOW + LEASE + timedelta(seconds=1)
    second = begin(factory, user_id, conversation_id, "k-2", "q2", now=later)
    complete(factory, second, "a2", now=later)

    with pytest.raises(TurnSuperseded):
        begin(factory, user_id, conversation_id, "k-1", "q1", now=later + timedelta(seconds=1))


# --- an expired request is retired, and its late answer is refused --------------------------------------------------


def roles_in_order(factory):
    with factory() as db:
        return [m.role for m in db.scalars(sa.select(Message).order_by(Message.id))]


def test_an_expired_request_is_retired_by_a_new_message_and_its_late_answer_is_refused(
    factory, user_id, conversation_id
):
    """The audit's reproduction: A's lease runs out, B (another key) is admitted, A's
    late completion arrives. A must not answer; the history stays well formed."""
    first = begin(factory, user_id, conversation_id, "k-A", "qA")
    admitted = NOW + LEASE + timedelta(seconds=1)

    second = begin(factory, user_id, conversation_id, "k-B", "qB", now=admitted)

    assert isinstance(second, Claim) and (second.attempt, second.retry) == (1, False)
    a, b = turn_rows(factory)
    assert (a.status, a.failure_kind, a.error_code, a.finished_at) == (
        "failed", "generation", "interrupted", admitted,
    )
    assert (a.result_payload, a.assistant_message_id, a.attempt_count) == (None, None, 1)
    assert (b.status, b.attempt_count, b.lease_expires_at) == ("processing", 1, admitted + LEASE)
    # the late answer of A: refused, and nothing of it is left
    with pytest.raises(LeaseLost):
        complete(factory, first, "late answer of A", now=admitted + timedelta(seconds=1))
    assert fail(factory, first, "late", now=admitted + timedelta(seconds=1)) is False
    assert roles_in_order(factory) == ["user", "user"]
    assert [t.status for t in turn_rows(factory)] == ["failed", "processing"]
    # B goes on to answer, and the next context is a well-formed exchange
    complete(factory, second, "answer of B", now=admitted + timedelta(seconds=2))
    third = begin(factory, user_id, conversation_id, "k-C", "qC", now=admitted + timedelta(seconds=3))
    assert third.context == [
        {"role": "user", "content": "qB"},
        {"role": "assistant", "content": "answer of B"},
        {"role": "user", "content": "qC"},
    ]
    # ... and A, now behind B, cannot be retried under its key
    with pytest.raises(TurnSuperseded):
        begin(factory, user_id, conversation_id, "k-A", "qA", now=admitted + timedelta(seconds=4))


def test_the_retired_request_is_shown_as_failed_and_stays_out_of_every_later_context(
    factory, user_id, conversation_id
):
    from house_helper.persistence import conversations as cv

    begin(factory, user_id, conversation_id, "k-A", "qA")
    admitted = NOW + LEASE + timedelta(seconds=1)
    second = begin(factory, user_id, conversation_id, "k-B", "qB", now=admitted)
    complete(factory, second, "answer of B", now=admitted)

    page = run(factory, lambda db: cv.list_messages(db, user_id, conversation_id, now=admitted, limit=10))

    assert [(m.text, m.status, m.error_code) for m in page.messages] == [
        ("qA", "failed", "interrupted"),
        ("qB", "completed", None),
        ("answer of B", "completed", None),
    ]


def test_a_lease_is_over_at_the_instant_it_expires(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)

    with pytest.raises(LeaseLost):
        complete(factory, claim, "too late", now=NOW + LEASE)  # lease_expires_at > now is false

    assert count(factory, Message) == 1
    # Nobody replaced the attempt, so it still has its row; the same key takes it over.
    (turn,) = turn_rows(factory)
    assert (turn.status, turn.attempt_count) == ("processing", 1)
    again = begin(factory, user_id, conversation_id, now=NOW + LEASE)
    assert (again.attempt, again.retry) == (2, True)


def test_an_answer_one_instant_before_the_lease_ends_is_stored(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)

    done = complete(factory, claim, "just in time", now=NOW + LEASE - timedelta(microseconds=1))

    assert done.assistant_message.text == "just in time" and turn_rows(factory)[0].status == "completed"


def test_the_same_key_takes_an_expired_request_over_but_another_key_retires_it(
    factory, user_id, conversation_id
):
    """Same-key reclaim (attempt + 1, nothing failed) and different-key admission
    (retire, then a request of its own) stay different things."""
    begin(factory, user_id, conversation_id, "k-A", "qA")
    after = NOW + LEASE

    reclaimed = begin(factory, user_id, conversation_id, "k-A", "qA", now=after)

    assert (reclaimed.attempt, reclaimed.retry) == (2, True)
    (turn,) = turn_rows(factory)
    assert (turn.status, turn.error_code, turn.attempt_count) == ("processing", None, 2)
    assert count(factory, Message) == 1

    later = after + LEASE
    other = begin(factory, user_id, conversation_id, "k-B", "qB", now=later)

    assert (other.attempt, other.retry) == (1, False)
    a, b = turn_rows(factory)
    assert (a.status, a.error_code, a.attempt_count) == ("failed", "interrupted", 2)
    assert b.status == "processing"


def test_a_retry_retires_the_other_requests_whose_leases_ran_out_but_not_its_own(
    factory, user_id, conversation_id
):
    """Two expired requests in one conversation (a state the code no longer makes, but
    one an older version could have left): claiming one retires the other, so that one
    request is in flight."""
    from house_helper.persistence.turns import open_turn_request

    with factory() as db, db.begin():
        conversation = db.get(Conversation, conversation_id)
        for key in ("k-1", "k-2"):
            (turn, _) = open_turn_request(db, conversation, key, key, now=NOW)
            turn.status, turn.attempt_count = "processing", 1
            turn.processing_started_at, turn.lease_expires_at = NOW, NOW + LEASE
    after = NOW + LEASE + timedelta(seconds=1)

    reclaimed = begin(factory, user_id, conversation_id, "k-2", "k-2", now=after)  # the newer one

    assert (reclaimed.attempt, reclaimed.retry) == (2, True)
    by_key = {t.idempotency_key: t for t in turn_rows(factory)}
    assert (by_key["k-2"].status, by_key["k-2"].attempt_count) == ("processing", 2)
    assert (by_key["k-1"].status, by_key["k-1"].error_code) == ("failed", "interrupted")


def test_nothing_is_retired_while_a_lease_is_live_and_the_refusal_changes_nothing(
    factory, user_id, conversation_id
):
    begin(factory, user_id, conversation_id, "k-A", "qA")
    before = snapshot(factory)

    with pytest.raises(ConversationBusy):
        begin(factory, user_id, conversation_id, "k-B", "qB", now=NOW + LEASE - timedelta(microseconds=1))

    assert snapshot(factory) == before


def test_a_retirement_is_taken_back_with_the_transaction_that_made_it(factory, user_id, conversation_id):
    begin(factory, user_id, conversation_id, "k-A", "qA")
    before = snapshot(factory)

    def admit_and_fail(db):
        turns.begin_turn(
            db, user_id, conversation_id, "k-B", "qB",
            clock=lambda: NOW + LEASE + timedelta(seconds=1), lease=LEASE, max_context_messages=20,
        )
        raise RuntimeError("the connection dropped")

    with pytest.raises(RuntimeError):
        run(factory, admit_and_fail)

    assert snapshot(factory) == before  # A is as it was: still processing, still its attempt's


# --- the clock is read under the lock -------------------------------------------------------------------------------------


class RecordingClock:
    """Returns a fixed time and remembers the statements that had run when it was asked."""

    def __init__(self, engine, value):
        self.value = value
        self.statements = []
        self.seen = []
        sa.event.listen(engine, "before_cursor_execute", self._record)

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        words = statement.split()
        # "UPDATE conversations", "SELECT", "INSERT", ...: enough to see the order
        self.statements.append(" ".join(words[:2]) if words[0].upper() == "UPDATE" else words[0].upper())

    def __call__(self):
        self.seen.append([s for s in self.statements if s not in ("BEGIN", "PRAGMA")])
        return self.value


def test_begin_turn_reads_the_clock_once_and_only_after_it_holds_the_conversation(
    engine, factory, user_id, conversation_id
):
    clock = RecordingClock(engine, NOW + timedelta(seconds=100))

    claim = run(
        factory,
        lambda db: turns.begin_turn(
            db, user_id, conversation_id, "k-1", "q1", clock=clock, lease=LEASE, max_context_messages=20
        ),
    )

    assert len(clock.seen) == 1
    # the lock (an UPDATE) is the first statement of the transaction, so nothing was read
    # before it, and it had run when the clock was asked
    assert clock.seen[0][0] == "UPDATE conversations"
    # ... and the lease runs from that instant, not from any earlier one
    (turn,) = turn_rows(factory)
    assert turn.processing_started_at == NOW + timedelta(seconds=100)
    assert turn.lease_expires_at == NOW + timedelta(seconds=100) + LEASE
    assert claim.user_message_created_at == NOW + timedelta(seconds=100)


def test_complete_and_fail_read_the_clock_once_and_only_after_the_lock(engine, factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)
    clock = RecordingClock(engine, NOW + timedelta(seconds=5))
    run(factory, lambda db: turns.complete_turn(db, claim, "a1", truncated=False, clock=clock))
    assert len(clock.seen) == 1
    assert clock.seen[0] == ["UPDATE conversations"]

    second = begin(factory, user_id, conversation_id, "k-2", "q2", now=NOW + timedelta(seconds=6))
    failing = RecordingClock(engine, NOW + timedelta(seconds=7))
    run(factory, lambda db: turns.fail_turn(db, second, "x", clock=failing))
    assert len(failing.seen) == 1
    assert failing.seen[0] == ["UPDATE conversations"]


def test_a_slow_admission_does_not_shorten_the_lease(factory, user_id, conversation_id):
    """The request started at NOW, but the lock was won 80 s later: the lease is 90 s
    from the claim (one measured from NOW would have had 10 s left)."""
    claim = begin(factory, user_id, conversation_id, now=NOW + timedelta(seconds=80))

    done = complete(factory, claim, "a1", now=NOW + timedelta(seconds=80) + LEASE - timedelta(seconds=1))

    assert done.assistant_message.text == "a1"
    assert turn_rows(factory)[0].lease_expires_at == NOW + timedelta(seconds=80) + LEASE


# --- states a retry cannot use ------------------------------------------------------------------------------------


def test_a_request_that_has_expired_cannot_be_used_again(factory, user_id, conversation_id):
    complete(factory, begin(factory, user_id, conversation_id))
    before = snapshot(factory)

    with pytest.raises(TurnUnusable):
        begin(factory, user_id, conversation_id, now=NOW + YEAR)

    assert snapshot(factory) == before


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(dict(status="cancelled", finished_at=NOW), id="cancelled"),
        pytest.param(
            dict(status="failed", failure_kind="persistence", finished_at=NOW, error_code="x", result_payload={"reply": "r"}),
            id="a-reply-waits-for-its-commit",
        ),
    ],
)
def test_a_request_left_in_a_state_this_module_does_not_make_is_not_retried(factory, user_id, conversation_id, change):
    begin(factory, user_id, conversation_id)
    with factory() as db, db.begin():
        db.execute(sa.update(TurnRequest).values(**change))

    with pytest.raises(TurnUnusable):
        begin(factory, user_id, conversation_id, now=NOW + timedelta(seconds=1))


def test_a_request_left_pending_is_claimed(factory, user_id, conversation_id):
    begin(factory, user_id, conversation_id)
    with factory() as db, db.begin():
        db.execute(
            sa.update(TurnRequest).values(
                status="pending", attempt_count=0, processing_started_at=None, lease_expires_at=None
            )
        )

    claim = begin(factory, user_id, conversation_id, now=NOW + timedelta(seconds=1))

    assert (claim.attempt, claim.retry) == (1, True)


@pytest.mark.parametrize("key", ["", "has space", "я", "x" * 129, None, 5])
def test_an_invalid_key_is_refused_before_anything_is_done(factory, user_id, conversation_id, key):
    before = snapshot(factory)

    with pytest.raises(ValueError):
        begin(factory, user_id, conversation_id, key=key)

    assert snapshot(factory) == before


@pytest.mark.parametrize("text", ["", None, 5])
def test_text_is_required(factory, user_id, conversation_id, text):
    with pytest.raises(ValueError):
        begin(factory, user_id, conversation_id, text=text)


# --- completing ------------------------------------------------------------------------------------------------------


def test_completing_stores_the_answer_and_the_completion_together(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)
    done_at = NOW + timedelta(seconds=7)

    done = complete(factory, claim, "Сварите яйца.", now=done_at, truncated=True)

    (turn,) = turn_rows(factory)
    with factory() as db:
        assistant = db.get(Message, turn.assistant_message_id)
    assert (assistant.role, assistant.content, assistant.created_at) == ("assistant", "Сварите яйца.", done_at)
    assert (turn.status, turn.finished_at) == ("completed", done_at)
    assert turn.result_payload == {"reply": "Сварите яйца.", "truncated": True}
    assert done.assistant_message.id == assistant.id and done.user_message.id == claim.user_message_id
    assert done.turn_id == turn.id and done.truncated is True
    assert updated_at(factory, conversation_id) == done_at


def test_a_completion_that_cannot_finish_leaves_nothing_of_it(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)
    before = snapshot(factory)

    def explode(db):
        turns.complete_turn(db, claim, "a1", truncated=False, clock=lambda: NOW + timedelta(seconds=5))
        raise RuntimeError("the connection dropped")

    with pytest.raises(RuntimeError):
        run(factory, explode)

    assert snapshot(factory) == before  # no answer, no completion, the activity time as it was
    # ... and the attempt still owns its request
    complete(factory, claim, "a1", now=NOW + timedelta(seconds=6))
    assert turn_rows(factory)[0].status == "completed"


def test_an_empty_answer_is_refused(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)

    with pytest.raises(ValueError):
        complete(factory, claim, "")

    assert count(factory, Message) == 1


# --- competing callers (real threads, a real file) ---------------------------------------------------------------------


def race(factory, count_, work):
    """`work(session, index)` in `count_` threads at the same moment, one transaction
    each. Returns the list of results or exceptions, in thread order."""
    barrier = threading.Barrier(count_)

    def one(index):
        with factory() as db:
            barrier.wait(timeout=10)
            try:
                with db.begin():
                    return work(db, index)
            except Exception as exc:  # collected: the test says what each caller met
                return exc

    with ThreadPoolExecutor(max_workers=count_) as pool:
        return [future.result(timeout=60) for future in [pool.submit(one, i) for i in range(count_)]]


def test_of_many_simultaneous_messages_for_one_conversation_exactly_one_is_claimed(
    factory, user_id, conversation_id
):
    results = race(
        factory,
        8,
        lambda db, i: turns.begin_turn(
            db, user_id, conversation_id, f"k-{i}", f"q{i}", clock=lambda: NOW, lease=LEASE, max_context_messages=20
        ),
    )

    assert sum(isinstance(r, Claim) for r in results) == 1
    assert sum(isinstance(r, ConversationBusy) for r in results) == 7
    assert count(factory, Message) == 1 and count(factory, TurnRequest) == 1


def test_of_many_simultaneous_copies_of_one_request_exactly_one_is_claimed(factory, user_id, conversation_id):
    results = race(
        factory,
        8,
        lambda db, i: turns.begin_turn(
            db, user_id, conversation_id, "same-key", "q", clock=lambda: NOW, lease=LEASE, max_context_messages=20
        ),
    )

    assert sum(isinstance(r, Claim) for r in results) == 1
    assert sum(isinstance(r, TurnInFlight) for r in results) == 7
    assert count(factory, Message) == 1 and count(factory, TurnRequest) == 1


def test_of_many_simultaneous_completions_of_one_attempt_exactly_one_stores_its_answer(
    factory, user_id, conversation_id
):
    claim = begin(factory, user_id, conversation_id)

    results = race(
        factory,
        6,
        lambda db, i: turns.complete_turn(
            db, claim, f"answer {i}", truncated=False, clock=lambda: NOW + timedelta(seconds=1)
        ),
    )

    assert sum(isinstance(r, CompletedTurn) for r in results) == 1
    assert sum(isinstance(r, LeaseLost) for r in results) == 5
    assert count(factory, Message) == 2  # the five that lost took their answers back with them
    with factory() as db:
        winner = [r for r in results if isinstance(r, CompletedTurn)][0]
        assert db.get(Message, winner.assistant_message.id).content == winner.assistant_message.text


def test_a_completion_and_a_retry_that_meet_leave_one_answer(factory, user_id, conversation_id):
    first = begin(factory, user_id, conversation_id)
    after = NOW + LEASE + timedelta(seconds=1)

    def work(db, index):
        if index == 0:
            return turns.complete_turn(db, first, "late answer", truncated=False, clock=lambda: after)
        return turns.begin_turn(
            db, user_id, conversation_id, "k-1", "q1", clock=lambda: after, lease=LEASE, max_context_messages=20
        )

    results = race(factory, 2, work)

    (turn,) = turn_rows(factory)
    answers = count(factory, Message) - 1
    # Whichever came first: the completion (and the retry replays it) or the claim (and
    # the late completion is refused). Never both answers.
    if isinstance(results[0], CompletedTurn):
        assert isinstance(results[1], CompletedTurn) and turn.status == "completed" and answers == 1
    else:
        assert isinstance(results[0], LeaseLost) and isinstance(results[1], Claim)
        assert turn.status == "processing" and turn.attempt_count == 2 and answers == 0


def test_two_conversations_are_claimed_at_once(factory, user_id):
    ids = [run(factory, lambda db: create_conversation(db, user_id, now=NOW)).id for _ in range(4)]

    results = race(
        factory,
        4,
        lambda db, i: turns.begin_turn(
            db, user_id, ids[i], "k-1", "q", clock=lambda: NOW, lease=LEASE, max_context_messages=20
        ),
    )

    assert all(isinstance(r, Claim) for r in results)


def test_a_late_completion_and_the_admission_of_another_message_never_both_get_their_way(
    factory, user_id, conversation_id
):
    """The expired-request race with real threads. A's completion is judged at an
    instant when its lease is already over, and B is admitted at that instant too: in
    whichever order the two get the conversation, A is refused, B is claimed, and the
    history has no answer of A in it."""
    first = begin(factory, user_id, conversation_id, "k-A", "qA")
    after = NOW + LEASE + timedelta(seconds=1)

    def work(db, index):
        if index == 0:
            return turns.complete_turn(db, first, "late answer of A", truncated=False, clock=lambda: after)
        return turns.begin_turn(
            db, user_id, conversation_id, "k-B", "qB", clock=lambda: after, lease=LEASE, max_context_messages=20
        )

    late, admitted = race(factory, 2, work)

    assert isinstance(late, LeaseLost) and isinstance(admitted, Claim), (late, admitted)
    assert roles_in_order(factory) == ["user", "user"]
    assert [t.status for t in turn_rows(factory)] == ["failed", "processing"]


def test_a_completion_just_inside_its_lease_and_a_newer_message_leave_a_well_formed_history(factory, user_id):
    """The two instants differ (A's completion is read just before its lease ends, B's
    admission just after), as they can when the callers' clocks differ. Whichever gets
    the conversation first: either A answered (and B sees the exchange) or A was
    retired (and B does not). Never an answer of A after B's question."""
    for round_ in range(6):
        conversation_id = run(factory, lambda db: create_conversation(db, user_id, now=NOW)).id
        first = begin(factory, user_id, conversation_id, "k-A", "qA")

        def work(db, index):
            if index == 0:
                return turns.complete_turn(
                    db, first, "answer of A", truncated=False, clock=lambda: NOW + LEASE - timedelta(seconds=1)
                )
            return turns.begin_turn(
                db, user_id, conversation_id, "k-B", "qB",
                clock=lambda: NOW + LEASE + timedelta(seconds=1), lease=LEASE, max_context_messages=20,
            )

        completed, admitted = race(factory, 2, work)

        assert isinstance(admitted, Claim), (round_, completed, admitted)
        with factory() as db:
            ordered = [
                (m.role, m.content)
                for m in db.scalars(
                    sa.select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
                )
            ]
        if isinstance(completed, CompletedTurn):  # A got the conversation first
            assert ordered == [("user", "qA"), ("assistant", "answer of A"), ("user", "qB")]
            assert admitted.context == [
                {"role": "user", "content": "qA"},
                {"role": "assistant", "content": "answer of A"},
                {"role": "user", "content": "qB"},
            ]
        else:  # B got it first and retired A
            assert isinstance(completed, LeaseLost)
            assert ordered == [("user", "qA"), ("user", "qB")]
            assert admitted.context == [{"role": "user", "content": "qB"}]


# --- an attempt that began is not made again for a caller that did not choose to repeat it ----------------
#
# reclaim=False is what the Telegram bot passes for an update that Telegram delivers again. The
# claim of an attempt is committed before the provider is called, so attempt_count > 0 is the
# durable fact "the provider may have been asked".


def test_a_completed_request_is_replayed_whether_or_not_reclaiming_is_allowed(factory, user_id, conversation_id):
    complete(factory, begin(factory, user_id, conversation_id), "a1")
    before = snapshot(factory)

    replay = begin(factory, user_id, conversation_id, now=NOW + timedelta(seconds=5), reclaim=False)

    assert isinstance(replay, CompletedTurn) and replay.assistant_message.text == "a1"
    assert snapshot(factory) == before


def test_a_failed_attempt_is_not_made_again_when_reclaiming_is_refused_and_nothing_changes(
    factory, user_id, conversation_id
):
    first = begin(factory, user_id, conversation_id)
    fail(factory, first, "provider_timeout", now=NOW + timedelta(seconds=2))
    before = snapshot(factory)

    with pytest.raises(TurnAttempted):
        begin(factory, user_id, conversation_id, now=NOW + timedelta(minutes=1), reclaim=False)

    assert snapshot(factory) == before
    assert turn_rows(factory)[0].attempt_count == 1
    # the person's own retry (the website: reclaim allowed) still works, and counts as attempt 2
    again = begin(factory, user_id, conversation_id, now=NOW + timedelta(minutes=2))
    assert (again.attempt, again.retry) == (2, True)


def test_an_attempt_whose_lease_ran_out_is_not_made_again_when_reclaiming_is_refused(
    factory, user_id, conversation_id
):
    begin(factory, user_id, conversation_id)  # claimed, then the worker was lost
    before = snapshot(factory)

    with pytest.raises(TurnAttempted):
        begin(factory, user_id, conversation_id, now=NOW + LEASE, reclaim=False)

    assert snapshot(factory) == before
    assert turn_rows(factory)[0].status == "processing"  # not retired by the refusal either


def test_an_attempt_that_is_still_live_is_in_flight_not_attempted(factory, user_id, conversation_id):
    begin(factory, user_id, conversation_id)

    with pytest.raises(TurnInFlight):
        begin(factory, user_id, conversation_id, now=NOW + timedelta(seconds=30), reclaim=False)


def test_a_request_that_was_never_claimed_is_claimed_even_when_reclaiming_is_refused(
    factory, user_id, conversation_id
):
    begin(factory, user_id, conversation_id)
    with factory() as db, db.begin():
        db.execute(
            sa.update(TurnRequest).values(
                status="pending", attempt_count=0, processing_started_at=None, lease_expires_at=None
            )
        )

    claim = begin(factory, user_id, conversation_id, now=NOW + timedelta(seconds=1), reclaim=False)

    assert (claim.attempt, claim.retry) == (1, True)


def test_a_new_key_is_claimed_whatever_reclaiming_says(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id, "tg-1001", "Привет", reclaim=False)

    assert (claim.attempt, claim.retry) == (1, False)


def test_a_conflicting_text_is_still_a_conflict_before_the_attempt_is_considered(factory, user_id, conversation_id):
    fail(factory, begin(factory, user_id, conversation_id), "provider_error", now=NOW + timedelta(seconds=2))

    with pytest.raises(IdempotencyKeyConflict):
        begin(factory, user_id, conversation_id, text="another", now=NOW + timedelta(minutes=1), reclaim=False)


def test_the_record_that_an_attempt_began_survives_a_restart_of_the_process(engine, factory, user_id, conversation_id):
    """Nothing but the database holds it: a second session factory on the same engine, a fresh
    caller with no memory of the first, meets the same answer."""
    begin(factory, user_id, conversation_id)  # the process dies here; the claim was committed
    from house_helper.persistence.database import create_session_factory

    fresh = create_session_factory(engine)

    with pytest.raises(TurnAttempted):
        begin(fresh, user_id, conversation_id, now=NOW + LEASE + timedelta(hours=1), reclaim=False)
