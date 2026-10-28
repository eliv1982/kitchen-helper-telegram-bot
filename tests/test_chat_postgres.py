"""The web chat's persistence and turn runner against a real PostgreSQL server. Opt-in:
skipped unless configured, exactly like tests/test_admin_postgres.py (read its docstring
for how to start a throwaway server and what keeps these tests from going anywhere
else; tests/postgres_harness.py is the safety harness, and CI does not run this).

What the SQLite tests cannot show, and this does: that the conversation row lock is
what serialises writers on PostgreSQL (SQLite has one writer; here there are several),
that "write first" holds without a snapshot to trip over, that the JSONB payload, the
timestamptz comparisons (leases, expiry), the self-assigning UPDATE and the subquery in
the context all behave as on SQLite, that a reader is never blocked by a writer, and that
a reply with a NUL (which PostgreSQL text cannot hold) is cleaned before it is stored.
Everything here goes through the same functions and the same runner as the web chat.

Without the variable every test here is skipped, which is not a pass: `pytest -rs`
lists the skip.
"""

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.core.backend import BackendError
from house_helper.persistence import conversations as cv
from house_helper.persistence import turns
from house_helper.persistence.base import utcnow
from house_helper.persistence.database import create_session_factory, upgrade_database
from house_helper.persistence.models import Conversation, Message, TurnRequest, User
from house_helper.web.database import WebDatabase
from house_helper.web.diagnostics import ApiError
from house_helper.web.turn_runner import WebTurnRunner
from tests.chat_support import ScriptedBackend, gate_on, reached
from tests.postgres_harness import POSTGRES_URL_ENV, Scratch, run_from_environment

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL (see tests/test_admin_postgres.py)",
)

NOW = utcnow().replace(microsecond=0)
LEASE = timedelta(seconds=90)


@pytest.fixture(scope="module")
def pg_run():
    run = run_from_environment()
    try:
        yield run
    finally:
        run.close()


@pytest.fixture(scope="module")
def template(pg_run):
    name = pg_run.create_database()
    engine = pg_run.engine(name)
    try:
        upgrade_database(engine)
    finally:
        engine.dispose()
    return name


@pytest.fixture
def scratch(pg_run, template):
    name = pg_run.create_database(template=template)
    try:
        yield Scratch(pg_run, name)
    finally:
        pg_run.drop_database(name)


@pytest.fixture
def engine(scratch):
    engine = scratch.engine()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def factory(engine):
    return create_session_factory(engine)


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
def conversation_id(factory, user_id):
    return run(factory, lambda db: cv.create_conversation(db, user_id, now=NOW)).id


def begin(factory, user_id, conversation_id, key="k-1", text="q1", now=NOW):
    return run(
        factory,
        lambda db: turns.begin_turn(
            db, user_id, conversation_id, key, text, clock=lambda: now, lease=LEASE, max_context_messages=20
        ),
    )


def count(factory, model):
    with factory() as db:
        return db.scalar(sa.select(sa.func.count()).select_from(model))


# --- the lifecycle, on PostgreSQL --------------------------------------------------------------------------


def test_a_turn_runs_through_its_lifecycle_and_is_replayed(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id, "k-1", "Что из яиц? 🍳")
    done = run(
        factory,
        lambda db: turns.complete_turn(
            db, claim, "Омлет 🥚", truncated=True, clock=lambda: NOW + timedelta(seconds=3)
        ),
    )

    again = begin(factory, user_id, conversation_id, "k-1", "Что из яиц? 🍳", now=NOW + timedelta(hours=1))

    assert again == done and again.truncated is True
    assert (again.user_message.text, again.assistant_message.text) == ("Что из яиц? 🍳", "Омлет 🥚")
    with factory() as db:
        turn = db.scalars(sa.select(TurnRequest)).one()
        assert turn.result_payload == {"reply": "Омлет 🥚", "truncated": True}  # JSONB, round trip
        assert (turn.status, turn.lease_expires_at, turn.expires_at - turn.created_at) == (
            "completed", NOW + LEASE, timedelta(days=365),
        )


def test_failure_retry_and_the_lease_work_with_timestamptz(factory, user_id, conversation_id):
    first = begin(factory, user_id, conversation_id)
    assert run(factory, lambda db: turns.fail_turn(db, first, "provider_timeout", clock=lambda: NOW + timedelta(seconds=1)))
    second = begin(factory, user_id, conversation_id, now=NOW + timedelta(seconds=2))
    assert (second.attempt, second.retry) == (2, True)

    with pytest.raises(turns.TurnInFlight):
        begin(factory, user_id, conversation_id, now=NOW + timedelta(seconds=2) + LEASE - timedelta(microseconds=1))
    third = begin(factory, user_id, conversation_id, now=NOW + timedelta(seconds=2) + LEASE)
    assert third.attempt == 3
    with pytest.raises(turns.LeaseLost):
        run(factory, lambda db: turns.complete_turn(db, second, "late", truncated=False, clock=lambda: NOW + timedelta(minutes=5)))
    assert count(factory, Message) == 1


def test_the_context_and_the_history_follow_the_same_rules(factory, user_id, conversation_id):
    for number in range(1, 4):
        claim = begin(factory, user_id, conversation_id, f"k-{number}", f"q{number}", now=NOW + timedelta(seconds=number))
        if number == 2:
            run(factory, lambda db: turns.fail_turn(db, claim, "provider_error", clock=lambda: NOW + timedelta(seconds=number)))
        else:
            run(factory, lambda db: turns.complete_turn(db, claim, f"a{number}", truncated=False, clock=lambda: NOW + timedelta(seconds=number)))

    claim = begin(factory, user_id, conversation_id, "k-4", "q4", now=NOW + timedelta(seconds=10))
    page = run(
        factory,
        lambda db: cv.list_messages(db, user_id, conversation_id, now=NOW + timedelta(seconds=10), limit=50),
    )

    assert [(m["role"], m["content"]) for m in claim.context] == [
        ("user", "q1"), ("assistant", "a1"), ("user", "q3"), ("assistant", "a3"), ("user", "q4")
    ]
    assert [(m.text, m.status) for m in page.messages] == [
        ("q1", "completed"), ("a1", "completed"), ("q2", "failed"), ("q3", "completed"),
        ("a3", "completed"), ("q4", "in_progress"),
    ]
    far = NOW + timedelta(days=366)
    assert run(factory, lambda db: cv.list_messages(db, user_id, conversation_id, now=far, limit=50)).messages == []


def test_the_activity_time_moves_only_for_what_adds_a_message(factory, user_id, conversation_id):
    def activity():
        with factory() as db:
            return db.get(Conversation, conversation_id).updated_at

    claim = begin(factory, user_id, conversation_id, now=NOW + timedelta(minutes=1))
    assert activity() == NOW + timedelta(minutes=1)
    with pytest.raises(turns.ConversationBusy):
        begin(factory, user_id, conversation_id, "k-2", "q2", now=NOW + timedelta(minutes=2))
    run(factory, lambda db: cv.list_messages(db, user_id, conversation_id, now=NOW + timedelta(minutes=3), limit=5))
    run(factory, lambda db: turns.fail_turn(db, claim, "x", clock=lambda: NOW + timedelta(minutes=4)))
    begin(factory, user_id, conversation_id, now=NOW + timedelta(minutes=5))  # the retry
    assert activity() == NOW + timedelta(minutes=1)


def test_ownership_is_decided_by_the_first_write(factory, user_id, conversation_id):
    stranger = run(factory, lambda db: (lambda u: (db.add(u), db.flush(), u.id)[2])(User()))

    with pytest.raises(cv.ConversationNotFound):
        begin(factory, stranger, conversation_id)
    assert count(factory, Message) == 0


# --- competing connections -------------------------------------------------------------------------------------


def race(factory, count_, work):
    barrier = threading.Barrier(count_)

    def one(index):
        with factory() as db:
            barrier.wait(timeout=10)
            try:
                with db.begin():
                    return work(db, index)
            except Exception as exc:
                return exc

    with ThreadPoolExecutor(max_workers=count_) as pool:
        return [f.result(timeout=120) for f in [pool.submit(one, i) for i in range(count_)]]


def test_of_many_connections_asking_at_once_exactly_one_conversation_turn_is_claimed(
    factory, user_id, conversation_id
):
    results = race(
        factory,
        8,
        lambda db, i: turns.begin_turn(
            db, user_id, conversation_id, f"k-{i}", f"q{i}", clock=lambda: NOW, lease=LEASE, max_context_messages=20
        ),
    )

    assert sum(isinstance(r, turns.Claim) for r in results) == 1, results
    assert sum(isinstance(r, turns.ConversationBusy) for r in results) == 7, results
    assert count(factory, Message) == 1 and count(factory, TurnRequest) == 1


def test_of_many_copies_of_one_request_exactly_one_is_claimed(factory, user_id, conversation_id):
    results = race(
        factory,
        8,
        lambda db, i: turns.begin_turn(
            db, user_id, conversation_id, "same", "q", clock=lambda: NOW, lease=LEASE, max_context_messages=20
        ),
    )

    assert sum(isinstance(r, turns.Claim) for r in results) == 1, results
    assert sum(isinstance(r, turns.TurnInFlight) for r in results) == 7, results


def test_of_many_completions_of_one_attempt_exactly_one_stores_its_answer(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)

    results = race(
        factory,
        6,
        lambda db, i: turns.complete_turn(db, claim, f"answer {i}", truncated=False, clock=lambda: NOW + timedelta(seconds=1)),
    )

    assert sum(isinstance(r, turns.CompletedTurn) for r in results) == 1, results
    assert sum(isinstance(r, turns.LeaseLost) for r in results) == 5, results
    assert count(factory, Message) == 2


def test_a_late_completion_and_a_retry_that_meet_do_not_deadlock_in_the_orders_tried(factory, user_id):
    after = NOW + LEASE + timedelta(seconds=1)
    for _ in range(8):  # (a lock-order mistake shows in some interleavings only)
        conversation_id = run(factory, lambda db: cv.create_conversation(db, user_id, now=NOW)).id
        first = begin(factory, user_id, conversation_id)

        def work(db, index):
            if index == 0:
                return turns.complete_turn(db, first, "late", truncated=False, clock=lambda: after)
            return turns.begin_turn(
                db, user_id, conversation_id, "k-1", "q1", clock=lambda: after, lease=LEASE, max_context_messages=20
            )

        results = race(factory, 2, work)

        # Both lock the conversation before the request (the one lock order of
        # persistence.turns), so neither waits for the other's second lock: no DBAPIError
        # (deadlock detected), only the two outcomes of the order. Eight rounds of an
        # interleaving are evidence for this order, not a proof for every possible writer.
        assert not any(isinstance(r, sa.exc.DBAPIError) for r in results), results
        assert all(isinstance(r, (turns.CompletedTurn, turns.LeaseLost, turns.Claim)) for r in results), results
        answers = len([m for m in run(factory, lambda db: db.scalars(
            sa.select(Message).where(Message.conversation_id == conversation_id, Message.role == "assistant")).all())])
        assert answers <= 1


# --- an expired request, a newer message, and a late answer ------------------------------------------------------


def roles_and_texts(factory, conversation_id):
    with factory() as db:
        return [
            (m.role, m.content)
            for m in db.scalars(
                sa.select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
            )
        ]


def test_an_expired_request_is_retired_by_a_new_message_and_its_late_answer_is_refused(
    factory, user_id, conversation_id
):
    first = begin(factory, user_id, conversation_id, "k-A", "qA")
    admitted = NOW + LEASE + timedelta(seconds=1)

    second = begin(factory, user_id, conversation_id, "k-B", "qB", now=admitted)

    assert isinstance(second, turns.Claim)
    with pytest.raises(turns.LeaseLost):
        run(
            factory,
            lambda db: turns.complete_turn(
                db, first, "late answer of A", truncated=False, clock=lambda: admitted + timedelta(seconds=1)
            ),
        )
    assert roles_and_texts(factory, conversation_id) == [("user", "qA"), ("user", "qB")]
    with factory() as db:
        a, b = db.scalars(sa.select(TurnRequest).order_by(TurnRequest.user_message_id)).all()
        assert (a.status, a.failure_kind, a.error_code, a.finished_at) == (
            "failed", "generation", "interrupted", admitted,
        )
        assert (b.status, b.lease_expires_at) == ("processing", admitted + LEASE)
    run(factory, lambda db: turns.complete_turn(db, second, "answer of B", truncated=False, clock=lambda: admitted))
    third = begin(factory, user_id, conversation_id, "k-C", "qC", now=admitted + timedelta(seconds=1))
    assert [(m["role"], m["content"]) for m in third.context] == [
        ("user", "qB"), ("assistant", "answer of B"), ("user", "qC")
    ]
    with pytest.raises(turns.TurnSuperseded):
        begin(factory, user_id, conversation_id, "k-A", "qA", now=admitted + timedelta(seconds=2))


def test_a_lease_is_over_at_the_instant_it_expires_with_timestamptz(factory, user_id, conversation_id):
    claim = begin(factory, user_id, conversation_id)

    with pytest.raises(turns.LeaseLost):
        run(factory, lambda db: turns.complete_turn(db, claim, "late", truncated=False, clock=lambda: NOW + LEASE))
    done = run(
        factory,
        lambda db: turns.complete_turn(
            db, claim, "in time", truncated=False, clock=lambda: NOW + LEASE - timedelta(microseconds=1)
        ),
    )

    assert done.assistant_message.text == "in time"


def test_a_late_completion_and_the_admission_of_another_message_never_both_get_their_way(
    factory, user_id
):
    """The audit's race on PostgreSQL, real connections, the lock decides the order.
    Eight rounds, each with a fresh conversation."""
    after = NOW + LEASE + timedelta(seconds=1)
    for round_ in range(8):
        conversation_id = run(factory, lambda db: cv.create_conversation(db, user_id, now=NOW)).id
        first = begin(factory, user_id, conversation_id, "k-A", "qA")

        def work(db, index):
            if index == 0:
                return turns.complete_turn(db, first, "late answer of A", truncated=False, clock=lambda: after)
            return turns.begin_turn(
                db, user_id, conversation_id, "k-B", "qB", clock=lambda: after, lease=LEASE, max_context_messages=20
            )

        late, admitted = race(factory, 2, work)

        assert isinstance(late, turns.LeaseLost) and isinstance(admitted, turns.Claim), (round_, late, admitted)
        assert roles_and_texts(factory, conversation_id) == [("user", "qA"), ("user", "qB")]


def test_a_completion_just_inside_its_lease_and_a_newer_message_leave_a_well_formed_history(
    factory, user_id
):
    for round_ in range(8):
        conversation_id = run(factory, lambda db: cv.create_conversation(db, user_id, now=NOW)).id
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

        assert isinstance(admitted, turns.Claim), (round_, completed, admitted)
        ordered = roles_and_texts(factory, conversation_id)
        if isinstance(completed, turns.CompletedTurn):  # A got the conversation first
            assert ordered == [("user", "qA"), ("assistant", "answer of A"), ("user", "qB")]
            assert [m["role"] for m in admitted.context] == ["user", "assistant", "user"]
        else:  # B got it first and retired A
            assert isinstance(completed, turns.LeaseLost), completed
            assert ordered == [("user", "qA"), ("user", "qB")]
            assert [m["role"] for m in admitted.context] == ["user"]


def test_a_reader_is_not_held_up_by_a_writer_that_has_not_committed(factory, user_id, conversation_id):
    begin(factory, user_id, conversation_id)
    writer = factory()
    writer.begin()
    try:
        writer.execute(sa.update(Conversation).where(Conversation.id == conversation_id).values(updated_at=NOW))
        # the row is locked by an open transaction: reads go on
        page = run(factory, lambda db: cv.list_messages(db, user_id, conversation_id, now=NOW, limit=5))
        listed = run(factory, lambda db: cv.list_conversations(db, user_id, now=NOW, limit=5))
        assert len(page.messages) == 1 and len(listed.conversations) == 1
    finally:
        writer.rollback()
        writer.close()


# --- the runner, on PostgreSQL ------------------------------------------------------------------------------------


@pytest.fixture
def web_database(engine):
    database = WebDatabase(engine)  # (it disposes the engine when closed; so does the fixture)
    yield database
    asyncio.run(database.aclose())


def submit(runner, user_id, conversation_id, key, text="Что из яиц?"):
    return runner.submit(
        user_id=user_id,
        conversation_id=conversation_id,
        idempotency_key=key,
        text=text,
        correlation_id="hh-0123456789abcdef",
        route="POST /test",
    )


def test_the_runner_answers_retries_and_cleans_what_postgresql_cannot_hold(web_database, factory, user_id, conversation_id):
    backend = ScriptedBackend(BackendError("x"), "Сварите\x00 яйца 🍳.")

    async def scenario():
        runner = WebTurnRunner(web_database, backend, lambda: utcnow())
        try:
            with pytest.raises(ApiError) as failed:
                await submit(runner, user_id, conversation_id, "k-1")
            assert failed.value.code == "provider_error"
            done = await submit(runner, user_id, conversation_id, "k-1")
            replay = await submit(runner, user_id, conversation_id, "k-1")
            return done, replay
        finally:
            await runner.aclose()

    done, replay = asyncio.run(scenario())

    assert done.completed.assistant_message.text == "Сварите яйца 🍳."  # no NUL: PostgreSQL would refuse it
    assert replay.replayed is True and backend.calls == 2
    assert count(factory, Message) == 2


def test_two_simultaneous_messages_one_is_answered_and_the_other_is_told_to_wait(
    web_database, factory, user_id, conversation_id
):
    backend = ScriptedBackend()

    async def scenario():
        runner = WebTurnRunner(web_database, backend, lambda: utcnow())
        arrived, release = backend.hold()
        try:
            first = asyncio.ensure_future(submit(runner, user_id, conversation_id, "k-1", "один"))
            await asyncio.wait_for(arrived.wait(), 30)
            with pytest.raises(ApiError) as busy:
                await submit(runner, user_id, conversation_id, "k-2", "два")
            release.set()
            return busy.value.code, await first
        finally:
            release.set()
            await runner.aclose()

    code, first = asyncio.run(scenario())

    assert code == "conversation_busy" and first.replayed is False
    assert count(factory, Message) == 2 and count(factory, TurnRequest) == 1


def test_a_claim_that_commits_after_its_turn_was_cancelled_is_marked_interrupted(
    web_database, factory, user_id, conversation_id, monkeypatch
):
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "begin_turn")

    async def scenario():
        runner = WebTurnRunner(web_database, backend, lambda: utcnow(), shutdown_grace_seconds=0.05)
        request = asyncio.ensure_future(submit(runner, user_id, conversation_id, "k-1"))
        try:
            await reached(entered)
            await asyncio.wait_for(runner.aclose(), 30)  # bounded, with the claim still to commit
            with pytest.raises(asyncio.CancelledError):
                await request
            unfinished = runner.unfinished
            proceed.set()
            await asyncio.wait_for(runner.drain(), 30)
            return unfinished, runner.unfinished
        finally:
            proceed.set()
            await runner.aclose()

    unfinished_then, unfinished_after = asyncio.run(scenario())

    assert unfinished_then > 0 and unfinished_after == 0
    with factory() as db:
        turn = db.scalars(sa.select(TurnRequest)).one()
    assert (turn.status, turn.failure_kind, turn.error_code) == ("failed", "generation", "interrupted")
    assert backend.calls == 0 and count(factory, Message) == 1
