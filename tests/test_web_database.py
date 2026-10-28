"""The web layer's use of the (synchronous) database: where the work runs, who owns
the Session, and what happens to the transaction and the connection however the
work ends."""

import asyncio
import contextvars
import inspect
import logging
import threading
import time
import uuid

import anyio
import pytest
import sqlalchemy as sa
from sqlalchemy.pool import QueuePool

from house_helper.persistence import turns
from house_helper.persistence.database import (
    create_db_engine,
    create_session_factory,
    upgrade_database,
)
from house_helper.persistence.models import Conversation, Message, TurnRequest, User
from house_helper.web.database import (
    MAX_CONCURRENT_UNITS_OF_WORK,
    DatabaseClosed,
    WebDatabase,
)
from tests.persistence_support import count
from tests.web_support import TIMEOUT, opened_database, sqlite_url

# How long a test lets a defective implementation show that it started something it
# should not have. The absence of an event cannot be waited for, only given time to
# happen: a window that is too short can hide a defect, never report a false one.
NOTHING_STARTS_WITHIN = 0.2


def add_user(session, name="Елена"):
    user = User(display_name=name)
    session.add(user)
    session.flush()
    return user.id


def users(engine) -> int:
    """How many users an independent session sees: what has been committed."""
    with create_session_factory(engine)() as witness:
        return count(witness, User)


def recorded_threads(engine):
    """Fills, as the engine's connections are checked out and back in, with the
    thread each happened on."""
    seen = {"checkout": [], "checkin": []}
    sa.event.listen(engine, "checkout", lambda *_: seen["checkout"].append(threading.get_ident()))
    sa.event.listen(engine, "checkin", lambda *_: seen["checkin"].append(threading.get_ident()))
    return seen


class Units:
    """Units of work that report that they started, wait for `release`, and count
    how many are really running at once: on their own threads, so the count is right
    however their callers ended. `peak` is the real concurrency."""

    def __init__(self, engine):
        self._engine = engine
        self._lock = threading.Lock()
        self.release = threading.Event()
        self.started = self.running = self.finished = 0
        self.peak = self.peak_checked_out = 0

    def hold(self, session):
        """Reads (a unit that writes would make the others queue on SQLite's write
        lock), then waits to be released. Returns "held"."""
        session.execute(sa.text("SELECT 1"))  # holds a connection, like real work
        with self._lock:
            self.started += 1
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.peak_checked_out = max(self.peak_checked_out, self._engine.pool.checkedout())
        try:
            assert self.release.wait(TIMEOUT)
            return "held"
        finally:
            with self._lock:
                self.running -= 1
                self.finished += 1

    def write_then_hold(self, session):
        """For the one unit of a test that shows a commit: the write comes first, so
        it holds SQLite's write lock for as long as it is held."""
        add_user(session)
        return self.hold(session)


def submit(database, work):
    return asyncio.ensure_future(database.run(work))


async def until(condition):
    async def poll():
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), TIMEOUT)


async def settle():
    """Let the tasks that were just created run to the point where they wait."""
    for _ in range(20):
        await asyncio.sleep(0)


async def cancelled_at_once(tasks):
    """Task.cancel() each, as a disconnect handler or a timeout would, and wait for
    them to end: delivered at once, whatever their work is doing."""
    for task in tasks:
        task.cancel()
    await asyncio.wait_for(asyncio.wait(tasks), TIMEOUT)
    assert all(task.cancelled() for task in tasks)


async def every_slot_is_usable(database, limit):
    """`limit` units can run together: every slot is free again. (A leaked slot
    leaves one of them waiting for ever, and the barrier breaks.)"""
    barrier = threading.Barrier(limit, timeout=TIMEOUT)

    def meet(session):
        session.execute(sa.text("SELECT 1"))
        barrier.wait()

    await asyncio.wait_for(asyncio.gather(*(database.run(meet) for _ in range(limit))), TIMEOUT)


# --- a unit of work ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_returns_what_the_work_returns_and_commits_it(tmp_path):
    async with opened_database(tmp_path) as (database, engine):
        user_id = await database.run(add_user)

        assert isinstance(user_id, uuid.UUID)
        assert users(engine) == 1  # visible to others: committed
        assert await database.run(lambda session: session.get(User, user_id).display_name) == "Елена"


@pytest.mark.asyncio
async def test_what_run_returns_stays_readable_after_the_session_is_gone(tmp_path):
    async with opened_database(tmp_path) as (database, _):
        user = await database.run(lambda session: session.merge(User(display_name="Борщ")))

        assert user.display_name == "Борщ"  # detached, not expired (see create_session_factory)


@pytest.mark.asyncio
async def test_a_failing_unit_of_work_is_rolled_back_and_its_error_propagates(tmp_path):
    class Boom(Exception):
        pass

    def work(session):
        add_user(session)
        raise Boom("no")

    async with opened_database(tmp_path) as (database, engine):
        with pytest.raises(Boom, match="no"):
            await database.run(work)

        assert users(engine) == 0
        assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_a_database_error_rolls_the_unit_back_too(tmp_path):
    def work(session):
        add_user(session, "first")
        session.add(Message(conversation_id=uuid.uuid4(), role="user", content="orphan"))
        session.flush()  # a foreign key violation

    async with opened_database(tmp_path) as (database, engine):
        with pytest.raises(sa.exc.IntegrityError):
            await database.run(work)

        assert users(engine) == 0
        assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_the_next_unit_of_work_starts_clean_after_a_failure(tmp_path):
    def failing(session):
        add_user(session, "discarded")
        raise RuntimeError

    async with opened_database(tmp_path) as (database, engine):
        with pytest.raises(RuntimeError):
            await database.run(failing)
        await database.run(lambda session: add_user(session, "kept"))

        assert users(engine) == 1


@pytest.mark.asyncio
async def test_a_savepoint_helper_of_the_persistence_layer_is_atomic_inside_a_unit_of_work(
    tmp_path,
):
    # open_turn_request looks its key up (a read) before it takes a savepoint: the
    # case R1 had to fix so that the caller's rollback takes the savepoint back.
    # The web unit of work is that caller.
    async with opened_database(tmp_path) as (database, engine):

        def setup(session):
            conversation = Conversation(user_id=add_user(session))
            session.add(conversation)
            session.flush()
            return conversation.id

        conversation_id = await database.run(setup)

        def open_then_fail(session):
            conversation = session.get(Conversation, conversation_id)
            turns.open_turn_request(session, conversation, "key-1", "что приготовить?")
            raise RuntimeError("the web request failed after the turn was opened")

        with pytest.raises(RuntimeError):
            await database.run(open_then_fail)
        with create_session_factory(engine)() as witness:
            assert (count(witness, Message), count(witness, TurnRequest)) == (0, 0)

        def open_and_keep(session):
            conversation = session.get(Conversation, conversation_id)
            return turns.open_turn_request(session, conversation, "key-1", "что приготовить?")[1]

        assert await database.run(open_and_keep) is True
        with create_session_factory(engine)() as witness:
            assert (count(witness, Message), count(witness, TurnRequest)) == (1, 1)


# --- where the work runs ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_work_runs_off_the_event_loop_thread(tmp_path):
    loop_thread = threading.get_ident()
    async with opened_database(tmp_path) as (database, _):
        worker = await database.run(lambda session: threading.get_ident())

        assert worker != loop_thread


@pytest.mark.asyncio
async def test_the_work_sees_the_context_variables_of_its_caller(tmp_path):
    # As with anyio.to_thread.run_sync, which this replaced (request-scoped context
    # such as a logging correlation id).
    variable = contextvars.ContextVar("request_id")
    variable.set("request-7")
    async with opened_database(tmp_path) as (database, _):
        assert await database.run(lambda session: variable.get()) == "request-7"


@pytest.mark.asyncio
async def test_a_session_is_opened_used_and_closed_on_one_thread(tmp_path):
    async with opened_database(tmp_path) as (database, engine):
        seen = recorded_threads(engine)

        worker = await database.run(
            lambda session: (add_user(session), threading.get_ident())[1]
        )

        assert seen["checkout"] == [worker]
        assert seen["checkin"] == [worker]
        assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_the_event_loop_keeps_running_while_the_database_is_busy(tmp_path):
    started, release = threading.Event(), threading.Event()

    def busy(session):
        session.execute(sa.text("SELECT 1"))
        started.set()
        assert release.wait(TIMEOUT)
        return "done"

    async with opened_database(tmp_path) as (database, _):
        task = asyncio.ensure_future(database.run(busy))
        try:
            while not started.is_set():
                await asyncio.sleep(0)
            for _ in range(50):  # the loop keeps turning while the worker is blocked
                await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        assert await asyncio.wait_for(task, TIMEOUT) == "done"


# --- bounded, and nothing leaks --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_limit_stays_below_the_pool_it_protects(tmp_path):
    parameters = inspect.signature(QueuePool.__init__).parameters
    capacity = parameters["pool_size"].default + parameters["max_overflow"].default

    async with opened_database(tmp_path) as (_, engine):
        assert isinstance(engine.pool, QueuePool)  # what a SQLite file gets
        assert MAX_CONCURRENT_UNITS_OF_WORK < capacity


@pytest.mark.asyncio
async def test_at_most_the_limit_runs_at_once_and_every_connection_comes_back(tmp_path):
    limit, total = 3, 12
    barrier = threading.Barrier(limit, timeout=TIMEOUT)  # proves `limit` really run together
    lock = threading.Lock()
    active = {"now": 0, "most": 0, "checked_out": 0}

    def work(session):
        session.execute(sa.text("SELECT 1"))
        with lock:
            active["now"] += 1
            active["most"] = max(active["most"], active["now"])
            active["checked_out"] = max(active["checked_out"], engine.pool.checkedout())
        barrier.wait()
        with lock:
            active["now"] -= 1

    async with opened_database(tmp_path, max_concurrent=limit) as (database, engine):
        await asyncio.wait_for(
            asyncio.gather(*(database.run(work) for _ in range(total))), TIMEOUT
        )

        assert active["most"] == limit
        assert active["checked_out"] <= limit
        assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_a_cancelled_caller_leaves_no_connection_behind(tmp_path):
    # A bare Task.cancel(), delivered at once: the worker cannot be interrupted, so
    # it runs on to the end, on its own, and takes the connection back itself.
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def busy(session):
        add_user(session)
        started.set()
        assert release.wait(TIMEOUT)
        finished.set()

    async with opened_database(tmp_path) as (database, engine):
        pool = engine.pool  # aclose() replaces it
        task = asyncio.ensure_future(database.run(busy))
        while not started.is_set():
            await asyncio.sleep(0)

        task.cancel()
        await asyncio.wait({task}, timeout=TIMEOUT)
        assert task.cancelled()
        assert pool.checkedout() == 1  # the worker is still on it

        release.set()
        await database.aclose()  # returns once the worker is done: see the closing tests

        assert finished.is_set()
        assert pool.checkedout() == 0
        assert users(engine) == 1  # the work had finished, so it commits like any other


@pytest.mark.asyncio
async def test_an_anyio_cancellation_waits_for_the_worker(tmp_path):
    # The kind Starlette delivers (a task group, a cancel scope): held back until
    # the function has finished, so the slot is not freed under a running worker.
    started, release = threading.Event(), threading.Event()
    scope = anyio.CancelScope()

    def busy(session):
        add_user(session)
        started.set()
        assert release.wait(TIMEOUT)

    async def caller(database):
        with scope:
            await database.run(busy)

    async with opened_database(tmp_path) as (database, engine):
        task = asyncio.ensure_future(caller(database))
        while not started.is_set():
            await asyncio.sleep(0)

        scope.cancel()
        for _ in range(50):
            await asyncio.sleep(0)
        assert not task.done()
        assert engine.pool.checkedout() == 1

        release.set()
        await asyncio.wait_for(task, TIMEOUT)

        assert engine.pool.checkedout() == 0
        assert users(engine) == 1


# --- the bound survives cancellation ----------------------------------------------------
# A bare Task.cancel() ends the task that awaits a unit of work, not the unit: its
# thread goes on. The bound is on units, so it has to go on counting that one.


@pytest.mark.asyncio
async def test_a_bare_cancel_does_not_free_the_slot_of_a_unit_still_running(tmp_path):
    # The audit's reproduction: the default limit, and five calls more than it allows.
    limit, extra = MAX_CONCURRENT_UNITS_OF_WORK, 5
    async with opened_database(tmp_path) as (database, engine):
        units = Units(engine)
        first = [submit(database, units.hold) for _ in range(limit)]
        await until(lambda: units.started == limit)

        await cancelled_at_once(first)
        assert engine.pool.checkedout() == limit  # the units are still on their connections

        replacements = [submit(database, units.hold) for _ in range(extra)]
        await asyncio.sleep(NOTHING_STARTS_WITHIN)
        assert (units.started, units.running) == (limit, limit)  # none of them has started

        units.release.set()
        assert await asyncio.wait_for(asyncio.gather(*replacements), TIMEOUT) == ["held"] * extra
        assert units.started == limit + extra
        assert units.peak == limit
        assert units.peak_checked_out <= limit
        await until(lambda: engine.pool.checkedout() == 0)  # the first ones end on their own
        await every_slot_is_usable(database, limit)


@pytest.mark.asyncio
async def test_repeated_cancellation_and_replacement_never_runs_more_than_the_limit(tmp_path):
    limit, rounds = 3, 5
    async with opened_database(tmp_path, max_concurrent=limit) as (database, engine):
        units = Units(engine)
        batch = [submit(database, units.hold) for _ in range(limit)]
        await until(lambda: units.started == limit)

        for _ in range(rounds):
            await cancelled_at_once(batch)
            batch = [submit(database, units.hold) for _ in range(limit)]
            await settle()
        await asyncio.sleep(NOTHING_STARTS_WITHIN)
        # The replacements cancelled while they waited never ran, and the last ones
        # still wait: only the first batch is running.
        assert (units.started, units.running, units.peak) == (limit, limit, limit)

        units.release.set()
        assert await asyncio.wait_for(asyncio.gather(*batch), TIMEOUT) == ["held"] * limit
        assert units.started == 2 * limit  # the first batch, and the last
        assert units.peak == limit
        assert units.peak_checked_out <= limit
        await until(lambda: engine.pool.checkedout() == 0)
        await every_slot_is_usable(database, limit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "holders_caller_leaves", [False, True], ids=["holder-awaited", "holder-cancelled"]
)
async def test_a_call_cancelled_while_it_waits_for_a_slot_never_runs(
    tmp_path, holders_caller_leaves
):
    # With the holder's caller gone, its unit alone keeps the slot taken: the calls
    # behind it must still be waiting for it, not already handed to a thread.
    ran = []
    scope = anyio.CancelScope()

    async def under_a_scope():
        with scope:
            await database.run(lambda session: ran.append("scope"))

    async with opened_database(tmp_path, max_concurrent=1) as (database, engine):
        units = Units(engine)
        holder = submit(database, units.hold)
        await until(lambda: units.started == 1)
        if holders_caller_leaves:
            await cancelled_at_once([holder])
        scoped = asyncio.ensure_future(under_a_scope())  # first in line
        bare = submit(database, lambda session: ran.append("bare"))
        await settle()  # both are waiting for the only slot

        scope.cancel()
        await cancelled_at_once([bare])
        await asyncio.wait_for(scoped, TIMEOUT)  # the wait is not held back, unlike the work
        assert units.running == 1  # the holder is still there

        units.release.set()
        if not holders_caller_leaves:
            assert await asyncio.wait_for(holder, TIMEOUT) == "held"
        await until(lambda: units.finished == 1)
        await every_slot_is_usable(database, 1)
        assert ran == []


@pytest.mark.asyncio
async def test_every_slot_comes_back_however_the_work_ends(tmp_path):
    class Boom(Exception):
        pass

    def raises(session):
        add_user(session)
        raise Boom

    def violates(session):
        session.add(Message(conversation_id=uuid.uuid4(), role="user", content="orphan"))
        session.flush()

    scope = anyio.CancelScope()

    async def under_a_scope():
        with scope:
            return await database.run(units.hold)

    limit = 2
    async with opened_database(tmp_path, max_concurrent=limit) as (database, engine):
        for _ in range(limit + 1):  # a slot lost to each failure would run out
            with pytest.raises(Boom):
                await asyncio.wait_for(database.run(raises), TIMEOUT)
        with pytest.raises(sa.exc.IntegrityError):
            await asyncio.wait_for(database.run(violates), TIMEOUT)

        units = Units(engine)
        bare = submit(database, units.hold)
        scoped = asyncio.ensure_future(under_a_scope())
        await until(lambda: units.started == limit)
        await cancelled_at_once([bare])
        scope.cancel()
        units.release.set()
        await asyncio.wait_for(scoped, TIMEOUT)

        await until(lambda: engine.pool.checkedout() == 0)
        assert units.finished == limit
        assert users(engine) == 0  # every failed unit was rolled back
        await every_slot_is_usable(database, limit)


@pytest.mark.asyncio
async def test_a_unit_that_cannot_be_started_gives_its_slot_back(tmp_path, monkeypatch):
    async with opened_database(tmp_path, max_concurrent=1) as (database, engine):
        with monkeypatch.context() as patch:

            def no_thread(*args, **kwargs):
                raise RuntimeError("can't start new thread")

            patch.setattr(database._pool, "submit", no_thread)
            with pytest.raises(RuntimeError, match="start new thread"):
                await database.run(add_user)

        await asyncio.wait_for(database.run(add_user), TIMEOUT)  # the only slot is free
        assert users(engine) == 1


def wait_on_this_thread(condition):
    """Poll `condition` without yielding to the event loop: for the code that fault
    injection puts inside a synchronous call."""
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.001)


def fail_the_next_pool_thread_start(monkeypatch, *, before_raising=lambda: None):
    """The first time the pool starts one of its threads, run `before_raising` and
    fail the way the operating system does when it cannot create one.

    This is the real ordering, not a stand-in for it: ThreadPoolExecutor.submit()
    puts the work item in its queue first and starts the thread second, so the error
    comes with the item already queued. Returns the list that holds the failed
    thread once it has happened."""
    real_start = threading.Thread.start
    failed = []

    def start(thread):
        if thread.name.startswith("house-helper-db") and not failed:
            failed.append(thread)
            before_raising()
            raise RuntimeError("can't start new thread")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    return failed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "a_worker_is_free", [False, True], ids=["left-in-the-queue", "taken-by-a-free-worker"]
)
async def test_a_unit_left_in_the_pool_by_a_submit_that_raised_never_runs(
    tmp_path, monkeypatch, caplog, a_worker_is_free
):
    # submit() raised "can't start new thread", but the work item was already queued
    # and the pool will run it as soon as a thread is free. The caller was told the
    # unit was not accepted, so it must do nothing: no database work, no second
    # release of the slot the caller has already given back. "taken-by-a-free-worker"
    # is the harder ordering: a thread picks the item up while submit() is still
    # raising, before run() has had the chance to say that the unit is refused.
    limit = 2 if a_worker_is_free else 1
    ran, gate = [], threading.Event()

    def ghost(session):
        ran.append("ghost")
        add_user(session, "ghost")

    async with opened_database(tmp_path, max_concurrent=limit) as (database, engine):
        units = Units(engine)
        units.release.set()  # the units that do run are not held
        queue = database._pool._work_queue
        blocker, before_raising = None, lambda: None
        if a_worker_is_free:
            blocker = database._pool.submit(gate.wait, TIMEOUT)  # starts the pool's first thread
            await until(blocker.running)

            def before_raising():
                gate.set()  # that thread is free again ...
                wait_on_this_thread(queue.empty)  # ... and takes the queued item
                time.sleep(NOTHING_STARTS_WITHIN)  # a defective unit would have run by now

        failed = fail_the_next_pool_thread_start(monkeypatch, before_raising=before_raising)

        with pytest.raises(RuntimeError, match="start new thread"):
            await asyncio.wait_for(database.run(ghost), TIMEOUT)
        assert len(failed) == 1
        if not a_worker_is_free:
            assert queue.qsize() == 1  # the item really is still queued
        assert ran == []
        assert database._slots.value == limit  # given back by the caller

        # A later, ordinary submission: the pool picks up the abandoned item first.
        assert await asyncio.wait_for(database.run(units.write_then_hold), TIMEOUT) == "held"
        if blocker:
            blocker.result(TIMEOUT)

        assert queue.empty()  # the abandoned item has been taken, and did nothing
        assert ran == []
        assert users(engine) == 1  # the replacement's; the abandoned unit wrote nothing
        assert (units.started, units.peak) == (1, 1)
        await until(lambda: engine.pool.checkedout() == 0)
        # Every permit can be taken (a leaked one leaves a call waiting for ever). The
        # calls are not required to run together, as every_slot_is_usable() requires:
        # after a failed thread start the standard library's pool can be left with
        # fewer threads than the limit (seen on 3.10 and 3.11), whatever this code does.
        await asyncio.wait_for(
            asyncio.gather(*(database.run(lambda session: None) for _ in range(limit))), TIMEOUT
        )
        await settle()
        assert database._slots.value == limit  # every permit back, none twice
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "a_worker_has_it", [True, False], ids=["taken-by-a-worker", "still-queued"]
)
async def test_a_unit_whose_completion_cannot_be_watched_is_refused_and_gives_its_slot_back(
    tmp_path, monkeypatch, caplog, a_worker_has_it
):
    # The unit is in the pool, then registering for its completion fails (the audit
    # injected exactly that). The unit is not accepted, so it never runs, even when a
    # worker has already taken it (that worker waits for the verdict), and its slot
    # is given back once, by the caller: a second release raises in a loop callback,
    # which asyncio logs.
    limit = 1
    ran, gate = [], threading.Event()

    def refuses(callback):
        raise RuntimeError("cannot register")

    def first(session):
        ran.append("first")
        return units.hold(session)

    async with opened_database(tmp_path, max_concurrent=limit) as (database, engine):
        units = Units(engine)
        units.release.set()
        pool_submit = database._pool.submit
        # The pool's only thread is busy, so the unit stays in the queue.
        blocker = None if a_worker_has_it else pool_submit(gate.wait, TIMEOUT)
        submitted = []

        def submit_and_break_the_first(*args, **kwargs):
            unit = pool_submit(*args, **kwargs)
            if not submitted:  # the replacement gets a working one
                submitted.append(unit)
                unit.add_done_callback = refuses
                if a_worker_has_it:
                    wait_on_this_thread(unit.running)
            return unit

        monkeypatch.setattr(database._pool, "submit", submit_and_break_the_first)
        with pytest.raises(RuntimeError, match="cannot register"):
            await asyncio.wait_for(database.run(first), TIMEOUT)
        assert len(submitted) == 1
        assert database._slots.value == limit  # given back at once

        gate.set()
        assert await asyncio.wait_for(database.run(units.hold), TIMEOUT) == "held"
        if blocker:
            blocker.result(TIMEOUT)
        assert ran == []  # the refused unit never ran
        assert (units.started, units.peak, units.peak_checked_out) == (1, 1, 1)
        await until(lambda: engine.pool.checkedout() == 0)
        await every_slot_is_usable(database, limit)
        await settle()
        assert database._slots.value == limit
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.asyncio
async def test_a_unit_withdrawn_from_the_queue_by_a_bare_cancel_gives_its_slot_back(tmp_path):
    # Admitted, but every pool thread is busy: the unit has not started when its
    # caller is cancelled, so the future is cancelled and the function never runs.
    ran, gate = [], threading.Event()
    async with opened_database(tmp_path, max_concurrent=1) as (database, _):
        blocker = database._pool.submit(gate.wait, TIMEOUT)
        call = submit(database, lambda session: ran.append(1))
        await settle()  # admitted: the only slot is taken, the unit waits in the pool
        await cancelled_at_once([call])

        gate.set()
        blocker.result(TIMEOUT)
        await every_slot_is_usable(database, 1)
        assert ran == []


def test_a_unit_that_outlives_the_event_loop_ends_quietly(tmp_path, caplog):
    # A server stopped while a function is still running: when it ends there is no
    # loop left to hand its slot back to, and that is not an error.
    started, release = threading.Event(), threading.Event()

    def hold(session):
        session.execute(sa.text("SELECT 1"))
        started.set()
        assert release.wait(TIMEOUT)

    async def scenario():
        engine = create_db_engine(sqlite_url(tmp_path / "web.db"))
        upgrade_database(engine)
        database = WebDatabase(engine)
        task = submit(database, hold)
        await until(started.is_set)
        await cancelled_at_once([task])
        return database, engine

    database, engine = asyncio.run(scenario())  # the loop is closed, the unit goes on
    try:
        release.set()
        database._pool.shutdown(wait=True)  # returns once the thread has ended
    finally:
        engine.dispose()

    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


# --- closing -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closing_lets_admitted_work_finish_and_refuses_what_was_queued(tmp_path, monkeypatch):
    limit = 2
    events = []
    real_dispose = sa.engine.Engine.dispose

    def recording_dispose(self, *args, **kwargs):
        events.append(("disposed", units.finished))
        return real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(sa.engine.Engine, "dispose", recording_dispose)
    ran = []

    async with opened_database(tmp_path, max_concurrent=limit) as (database, engine):
        pool = engine.pool  # aclose() replaces it
        units = Units(engine)
        admitted = [submit(database, units.write_then_hold), submit(database, units.hold)]
        await until(lambda: units.started == limit)
        queued = [submit(database, lambda session: ran.append(1)) for _ in range(3)]
        await settle()

        closing = asyncio.ensure_future(database.aclose())
        await settle()
        assert database.closed
        assert not closing.done()  # waiting for the two that run

        units.release.set()
        assert await asyncio.wait_for(asyncio.gather(*admitted), TIMEOUT) == ["held", "held"]
        await asyncio.wait_for(closing, TIMEOUT)
        refused = await asyncio.wait_for(asyncio.gather(*queued, return_exceptions=True), TIMEOUT)

        assert all(isinstance(error, DatabaseClosed) for error in refused)
        assert ran == []
        assert events == [("disposed", limit)]  # only after both had finished
        assert users(engine) == 1  # the one that wrote committed
        assert pool.checkedout() == 0


@pytest.mark.asyncio
async def test_work_queued_behind_a_unit_the_close_gave_up_on_is_refused(tmp_path, caplog):
    # aclose() does not wait for ever: it disposes the engine and shuts the pool down.
    # A call queued behind the unit it gave up on is admitted only when that unit
    # ends, after all that, and has to be refused, not handed to a pool that takes no
    # more work.
    ran = []
    async with opened_database(
        tmp_path, max_concurrent=1, drain_timeout=0.05
    ) as (database, engine):
        units = Units(engine)
        stuck = submit(database, units.hold)
        await until(lambda: units.started == 1)
        queued = submit(database, lambda session: ran.append(1))
        await settle()

        with caplog.at_level(logging.WARNING, logger="house_helper.web.database"):
            await asyncio.wait_for(database.aclose(), TIMEOUT)
        assert "still running" in caplog.text

        units.release.set()
        assert await asyncio.wait_for(stuck, TIMEOUT) == "held"
        with pytest.raises(DatabaseClosed):
            await asyncio.wait_for(queued, TIMEOUT)
        assert ran == []


@pytest.mark.asyncio
async def test_a_unit_admitted_before_the_close_but_not_yet_started_is_refused(
    tmp_path, monkeypatch
):
    # Handed to the pool, but its thread has not reached the function when the
    # database closes: aclose() sees nothing running, so it does not wait and the
    # engine goes. The unit must be refused on its own thread, under the lock.
    reached, gate, disposed = threading.Event(), threading.Event(), threading.Event()
    ran = []
    real_dispose = sa.engine.Engine.dispose

    def recording_dispose(self, *args, **kwargs):
        disposed.set()
        return real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(sa.engine.Engine, "dispose", recording_dispose)

    async with opened_database(tmp_path) as (database, _):
        real = database._run_in_session

        def late(work):
            reached.set()
            assert gate.wait(TIMEOUT)
            return real(work)

        monkeypatch.setattr(database, "_run_in_session", late)
        call = submit(database, lambda session: ran.append(1))
        await until(reached.is_set)

        await asyncio.wait_for(database.aclose(), TIMEOUT)
        assert disposed.is_set()
        gate.set()
        with pytest.raises(DatabaseClosed):
            await asyncio.wait_for(call, TIMEOUT)
        assert ran == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_first", [True, False], ids=["cancel-then-close", "close-then-cancel"]
)
async def test_cancellation_overlapping_the_close_changes_neither_the_bound_nor_the_outcome(
    tmp_path, cancel_first
):
    limit = 2
    async with opened_database(tmp_path, max_concurrent=limit) as (database, engine):
        pool = engine.pool  # aclose() replaces it
        units = Units(engine)
        writer = submit(database, units.write_then_hold)
        reader = submit(database, units.hold)
        await until(lambda: units.started == limit)
        leaves = submit(database, units.hold)  # queued, and its caller will go
        stays = submit(database, units.hold)  # queued, and will still be there at the close
        await settle()

        async def callers_leave():
            # The writer's caller is gone, but its unit still holds its slot: the
            # call behind it does not get in.
            await cancelled_at_once([writer, leaves])
            await asyncio.sleep(NOTHING_STARTS_WITHIN)

        if cancel_first:
            await callers_leave()
        closing = asyncio.ensure_future(database.aclose())
        await settle()
        if not cancel_first:
            await callers_leave()

        assert database.closed
        with pytest.raises(DatabaseClosed):  # refused at once, though every slot is taken
            await asyncio.wait_for(database.run(add_user), TIMEOUT)
        assert not closing.done()  # still waiting for the two that run
        assert (units.started, units.running, pool.checkedout()) == (limit, limit, limit)

        units.release.set()
        assert await asyncio.wait_for(reader, TIMEOUT) == "held"
        await asyncio.wait_for(closing, TIMEOUT)
        with pytest.raises(DatabaseClosed):  # its turn came after the close
            await asyncio.wait_for(stays, TIMEOUT)

        assert (units.started, units.finished, units.peak) == (limit, limit, limit)
        assert users(engine) == 1  # the writer's caller was gone; its transaction still committed
        assert pool.checkedout() == 0


@pytest.mark.asyncio
async def test_closing_ends_the_threads_the_database_started(tmp_path):
    before = set(threading.enumerate())
    async with opened_database(tmp_path, max_concurrent=3) as (database, _):
        barrier = threading.Barrier(3, timeout=TIMEOUT)  # three units, so three threads
        await asyncio.wait_for(
            asyncio.gather(*(database.run(lambda session: barrier.wait()) for _ in range(3))),
            TIMEOUT,
        )
        started = set(threading.enumerate()) - before
        ours = [thread for thread in started if thread.name.startswith("house-helper-db")]
        assert len(ours) == 3

        await database.aclose()

        await until(lambda: not any(thread.is_alive() for thread in ours))


@pytest.mark.asyncio
async def test_closing_refuses_further_work_and_disposes_the_engine_once(tmp_path, monkeypatch):
    disposed = []
    real_dispose = sa.engine.Engine.dispose

    def recording_dispose(self, *args, **kwargs):
        disposed.append(self)
        return real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(sa.engine.Engine, "dispose", recording_dispose)

    async with opened_database(tmp_path) as (database, engine):
        await database.run(add_user)
        assert not database.closed
        assert disposed == []

        await database.aclose()
        await database.aclose()  # again: nothing more to do

        assert database.closed
        assert disposed == [engine]
        with pytest.raises(DatabaseClosed):
            await database.run(add_user)
    assert disposed == [engine]  # the fixture's own aclose() was a no-op as well


@pytest.mark.asyncio
async def test_work_still_queued_for_a_slot_when_the_database_closes_is_refused(tmp_path):
    # Not yet counted as running, so the wait in aclose() does not cover it; it is
    # refused when its turn for a slot comes, so it never reaches an engine that is
    # being disposed (a disposed engine quietly builds a new pool).
    started, release = threading.Event(), threading.Event()
    ran = []

    def holds_the_only_slot(session):
        started.set()
        assert release.wait(TIMEOUT)

    async with opened_database(tmp_path, max_concurrent=1) as (database, _):
        first = asyncio.ensure_future(database.run(holds_the_only_slot))
        while not started.is_set():
            await asyncio.sleep(0)
        second = asyncio.ensure_future(database.run(lambda session: ran.append(1)))
        for _ in range(20):
            await asyncio.sleep(0)  # let it queue for the slot
        closing = asyncio.ensure_future(database.aclose())
        for _ in range(20):
            await asyncio.sleep(0)

        release.set()
        await asyncio.wait_for(asyncio.gather(first, closing), TIMEOUT)

        with pytest.raises(DatabaseClosed):
            await asyncio.wait_for(second, TIMEOUT)
        assert ran == []


@pytest.mark.asyncio
async def test_closing_waits_for_the_work_in_flight_before_it_disposes_the_engine(
    tmp_path, monkeypatch
):
    # Engine.dispose() closes only idle connections: disposing under a running
    # unit of work would leave its connection open.
    events = []
    real_dispose = sa.engine.Engine.dispose

    def recording_dispose(self, *args, **kwargs):
        events.append("disposed")
        return real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(sa.engine.Engine, "dispose", recording_dispose)
    started, release = threading.Event(), threading.Event()

    def busy(session):
        add_user(session)
        started.set()
        assert release.wait(TIMEOUT)
        events.append("work finished")
        return "finished"

    async with opened_database(tmp_path) as (database, engine):
        work = asyncio.ensure_future(database.run(busy))
        while not started.is_set():
            await asyncio.sleep(0)

        closing = asyncio.ensure_future(database.aclose())
        for _ in range(50):
            await asyncio.sleep(0)
        assert database.closed
        assert not closing.done()  # waiting for the work
        assert events == []
        with pytest.raises(DatabaseClosed):  # and meanwhile nothing new is taken on
            await database.run(add_user)

        release.set()
        await asyncio.wait_for(asyncio.gather(work, closing), TIMEOUT)

        assert work.result() == "finished"
        assert events == ["work finished", "disposed"]
        assert users(engine) == 1


@pytest.mark.asyncio
async def test_closing_does_not_wait_forever_for_work_that_never_ends(tmp_path, caplog):
    started, release = threading.Event(), threading.Event()

    def stuck(session):
        started.set()
        assert release.wait(TIMEOUT)
        return "late"

    async with opened_database(tmp_path, drain_timeout=0.05) as (database, _):
        work = asyncio.ensure_future(database.run(stuck))
        while not started.is_set():
            await asyncio.sleep(0)

        with caplog.at_level(logging.WARNING, logger="house_helper.web.database"):
            await asyncio.wait_for(database.aclose(), TIMEOUT)

        assert "still running" in caplog.text
        release.set()
        assert await asyncio.wait_for(work, TIMEOUT) == "late"  # it was not cut off


def test_a_database_can_be_built_before_any_event_loop_exists(tmp_path):
    # Nothing is bound to a loop at construction, and nothing connects.
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'never.db'}")
    try:
        database = WebDatabase(engine)

        assert not database.closed
        assert not (tmp_path / "never.db").exists()
    finally:
        engine.dispose()


# --- the unit is accepted: the caller is told when it can no longer be stopped ------------------


@pytest.mark.asyncio
async def test_on_accepted_is_called_once_when_the_pool_has_the_unit_and_before_it_ends(tmp_path):
    async with opened_database(tmp_path) as (database, engine):
        units = Units(engine)
        accepted = []

        call = asyncio.ensure_future(database.run(units.hold, on_accepted=lambda: accepted.append("yes")))
        await until(lambda: units.started == 1)

        assert accepted == ["yes"]  # told while the function is still running ...
        units.release.set()
        assert await asyncio.wait_for(call, TIMEOUT) == "held"
        assert accepted == ["yes"]  # ... and only once


@pytest.mark.asyncio
async def test_on_accepted_waits_for_the_slot_and_a_call_withdrawn_before_it_is_never_told(tmp_path):
    ran, told = [], []
    async with opened_database(tmp_path, max_concurrent=1) as (database, engine):
        units = Units(engine)
        holder = submit(database, units.hold)
        await until(lambda: units.started == 1)
        waiting = asyncio.ensure_future(
            database.run(lambda session: ran.append("waiting") or "ran", on_accepted=lambda: told.append("waiting"))
        )
        withdrawn = asyncio.ensure_future(
            database.run(lambda session: ran.append("withdrawn"), on_accepted=lambda: told.append("withdrawn"))
        )
        await settle()  # both wait for the only slot

        assert told == []  # not accepted: nothing can run yet, and the call can still be withdrawn
        await cancelled_at_once([withdrawn])
        units.release.set()
        assert await asyncio.wait_for(waiting, TIMEOUT) == "ran"
        assert await asyncio.wait_for(holder, TIMEOUT) == "held"

        assert told == ["waiting"] and ran == ["waiting"]


@pytest.mark.asyncio
async def test_on_accepted_is_not_called_for_work_the_database_refuses(tmp_path, monkeypatch):
    told = []
    async with opened_database(tmp_path) as (database, engine):
        with monkeypatch.context() as patch:

            def no_thread(*args, **kwargs):
                raise RuntimeError("can't start new thread")

            patch.setattr(database._pool, "submit", no_thread)
            with pytest.raises(RuntimeError, match="start new thread"):
                await database.run(add_user, on_accepted=lambda: told.append("accepted"))
        await database.aclose()
        with pytest.raises(DatabaseClosed):
            await database.run(add_user, on_accepted=lambda: told.append("accepted"))

    assert told == []


@pytest.mark.asyncio
async def test_a_failing_on_accepted_does_not_stop_the_unit_or_hide_its_result(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    async with opened_database(tmp_path) as (database, engine):

        def broken():
            raise ZeroDivisionError("SECRET")

        result = await asyncio.wait_for(database.run(add_user, on_accepted=broken), TIMEOUT)

        assert users(engine) == 1 and result is not None  # it ran and committed
        assert "ZeroDivisionError" in caplog.text and "SECRET" not in caplog.text


# --- run_to_completion: the caller does not leave before the unit has ended ----------------------
# For a caller that holds a lock the unit must not outlive (the Telegram bot's binding lock):
# a cancellation is raised when the unit is over, never before; a call that was still waiting for
# a slot is withdrawn at once, as with run(). The bot's use of it is tests/test_telegram_unlink_race.py.


@pytest.mark.asyncio
async def test_run_to_completion_returns_what_the_work_returns_and_commits_it(tmp_path):
    async with opened_database(tmp_path) as (database, engine):
        result = await asyncio.wait_for(database.run_to_completion(add_user), TIMEOUT)

        assert result is not None and users(engine) == 1


@pytest.mark.asyncio
async def test_run_to_completion_raises_what_the_work_raises_after_the_rollback(tmp_path):
    class Boom(Exception):
        pass

    def failing(session):
        add_user(session)
        raise Boom

    async with opened_database(tmp_path) as (database, engine):
        with pytest.raises(Boom):
            await asyncio.wait_for(database.run_to_completion(failing), TIMEOUT)

        assert users(engine) == 0
        await every_slot_is_usable(database, MAX_CONCURRENT_UNITS_OF_WORK)


@pytest.mark.asyncio
async def test_a_bare_cancel_is_held_back_until_the_accepted_unit_has_ended_and_then_raised(tmp_path):
    async with opened_database(tmp_path) as (database, engine):
        units = Units(engine)
        call = asyncio.ensure_future(database.run_to_completion(units.write_then_hold))
        await until(lambda: units.started == 1)

        call.cancel()  # delivered at once to a plain run(); here it waits for the worker
        await settle()
        assert not call.done()
        assert units.finished == 0 and users(engine) == 0  # the worker is still in its transaction

        units.release.set()
        await asyncio.wait_for(asyncio.wait({call}), TIMEOUT)
        assert call.cancelled()  # the cancellation is raised, once the unit is over
        assert units.finished == 1 and users(engine) == 1  # ... and the unit committed, as run() would have
        await until(lambda: engine.pool.checkedout() == 0)
        await every_slot_is_usable(database, MAX_CONCURRENT_UNITS_OF_WORK)


@pytest.mark.asyncio
async def test_repeated_cancellations_do_not_end_the_wait(tmp_path):
    async with opened_database(tmp_path) as (database, engine):
        units = Units(engine)
        call = asyncio.ensure_future(database.run_to_completion(units.hold))
        await until(lambda: units.started == 1)

        for _ in range(5):
            call.cancel()
            await settle()
            assert not call.done()

        units.release.set()
        await asyncio.wait_for(asyncio.wait({call}), TIMEOUT)
        assert call.cancelled() and units.finished == 1


@pytest.mark.asyncio
async def test_a_cancel_scope_that_stays_cancelled_neither_spins_nor_ends_the_wait(tmp_path):
    """The kind Starlette delivers: a scope that keeps delivering its cancellation. Inside the
    wait it must not be delivered (the loop would never yield to the worker's completion), and the
    loop must stay responsive meanwhile."""
    scope = anyio.CancelScope()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    async def caller(database, units):
        with scope:
            await database.run_to_completion(units.write_then_hold)

    async with opened_database(tmp_path) as (database, engine):
        units = Units(engine)
        beat = asyncio.ensure_future(ticker())
        task = asyncio.ensure_future(caller(database, units))
        await until(lambda: units.started == 1)

        scope.cancel()
        before = ticks
        await settle()
        assert not task.done() and units.finished == 0
        assert ticks > before  # the event loop went on turning

        units.release.set()
        await asyncio.wait_for(task, TIMEOUT)  # the scope swallows its own cancellation at its end
        beat.cancel()
        assert units.finished == 1 and users(engine) == 1
        assert engine.pool.checkedout() == 0


@pytest.mark.asyncio
async def test_a_call_cancelled_while_it_waits_for_a_slot_is_withdrawn_at_once_and_never_runs(tmp_path):
    ran = []
    async with opened_database(tmp_path, max_concurrent=1) as (database, engine):
        units = Units(engine)
        holder = submit(database, units.hold)
        await until(lambda: units.started == 1)
        waiting = asyncio.ensure_future(database.run_to_completion(lambda session: ran.append("ran")))
        await settle()  # it waits for the only slot

        waiting.cancel()
        await asyncio.wait_for(asyncio.wait({waiting}), TIMEOUT)  # nothing to wait for: it did not start
        assert waiting.cancelled()
        assert units.running == 1  # the holder is still there

        units.release.set()
        assert await asyncio.wait_for(holder, TIMEOUT) == "held"
        await until(lambda: units.finished == 1)
        await every_slot_is_usable(database, 1)
        assert ran == []


@pytest.mark.asyncio
async def test_a_call_cancelled_before_it_has_started_at_all_does_nothing(tmp_path):
    ran = []
    async with opened_database(tmp_path) as (database, engine):
        call = asyncio.ensure_future(database.run_to_completion(lambda session: ran.append("ran")))
        call.cancel()  # before the loop has run it
        await asyncio.wait_for(asyncio.wait({call}), TIMEOUT)

        assert call.cancelled() and ran == []
        await every_slot_is_usable(database, MAX_CONCURRENT_UNITS_OF_WORK)


@pytest.mark.asyncio
async def test_cancellation_beats_the_units_own_failure_and_the_failure_is_not_left_unretrieved(
    tmp_path, caplog
):
    class Boom(Exception):
        pass

    release, started = threading.Event(), threading.Event()

    def failing(session):
        add_user(session)
        started.set()
        assert release.wait(TIMEOUT)
        raise Boom("SECRET")

    caplog.set_level(logging.DEBUG)
    async with opened_database(tmp_path) as (database, engine):
        call = asyncio.ensure_future(database.run_to_completion(failing))
        while not started.is_set():
            await asyncio.sleep(0)
        call.cancel()
        await settle()
        assert not call.done()

        release.set()
        await asyncio.wait_for(asyncio.wait({call}), TIMEOUT)
        assert call.cancelled()  # the caller asked to be cancelled; that is what it gets
        assert users(engine) == 0  # the failed unit rolled back
        await settle()

    assert "never retrieved" not in caplog.text


@pytest.mark.asyncio
async def test_run_to_completion_refuses_work_once_the_database_is_closed(tmp_path):
    async with opened_database(tmp_path) as (database, engine):
        await database.aclose()

        with pytest.raises(DatabaseClosed):
            await asyncio.wait_for(database.run_to_completion(add_user), TIMEOUT)
