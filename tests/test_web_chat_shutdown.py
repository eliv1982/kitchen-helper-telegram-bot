"""Stopping the turn runner while the database still holds its transactions.

A transaction runs on a worker thread and cannot be interrupted, so a turn that is
cancelled while it waits for one has not stopped it: the transaction goes on and commits
or rolls back unseen. What is pinned here is that the runner does not lose track of such
a transaction at any of the three boundaries of a turn (the claim, the call to the
assistant, the completion), that "drained" means nothing of the runner can still touch
the database, that stopping stays bounded, and what is recorded in each case.

Every transaction is held at a boundary by a threading.Event the test controls
(chat_support.gate_on), so what happens before, during and after is arranged, not
waited for: the only real time that passes is a grace period of a few hundredths of a
second that has to elapse once.
"""

import asyncio
import logging
import threading

import pytest
import sqlalchemy as sa

from house_helper.persistence import turns
from house_helper.persistence.database import create_db_engine, create_session_factory
from house_helper.persistence.models import Message, TurnRequest
from house_helper.web.database import WebDatabase
from house_helper.web.diagnostics import ApiError
from house_helper.web.turn_runner import WebTurnRunner
from tests.chat_support import ScriptedBackend, cancel_turns, gate_on, reached, runner_env
from tests.web_support import TIMEOUT, sqlite_url

pytestmark = pytest.mark.asyncio

GRACE = 0.05


def operational_error():
    return sa.exc.OperationalError("UPDATE secret_table SET x = ?", {"x": "SECRET"}, Exception("disk I/O error"))


async def settled(env):
    await asyncio.wait_for(env.runner.drain(), TIMEOUT)
    assert env.runner.unfinished == 0


async def cancelled(request):
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, TIMEOUT)


# --- during the claim (step 1) ------------------------------------------------------------------------


async def test_a_claim_that_commits_after_the_turn_was_cancelled_is_marked_interrupted(tmp_path, monkeypatch):
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "begin_turn")
    async with runner_env(tmp_path, backend) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)

        cancel_turns(env.runner)  # the turn is cancelled while its transaction is still to run
        await cancelled(request)

        assert env.runner.unfinished > 0  # not drained: the transaction has not ended
        assert env.count(TurnRequest) == 0
        proceed.set()  # ... and now it commits a claim
        await settled(env)

        turn = env.turn()
        assert (turn.status, turn.failure_kind, turn.error_code) == ("failed", "generation", "interrupted")
        assert turn.finished_at is not None
        assert backend.calls == 0  # the assistant was never asked
        assert env.count(Message) == 1  # the person's message stays, as a failed one
        # nothing is stuck: the same key claims the request again at once
        retry = await env.submit()
        assert retry.replayed is False and retry.completed.assistant_message.text == "Омлет."
        assert (env.turn().status, env.turn().attempt_count) == ("completed", 2)


async def test_a_claim_that_is_refused_after_the_turn_was_cancelled_leaves_nothing_and_no_noise(
    tmp_path, monkeypatch, caplog
):
    backend = ScriptedBackend()
    entered, proceed = gate_on(monkeypatch, "begin_turn", fail=operational_error())
    caplog.set_level(logging.DEBUG)
    async with runner_env(tmp_path, backend) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)
        cancel_turns(env.runner)
        await cancelled(request)

        proceed.set()
        await settled(env)

        assert env.count(TurnRequest) == 0 and env.count(Message) == 0
        assert "could not be recorded" not in caplog.text and "SECRET" not in caplog.text


async def test_a_replay_that_was_cancelled_while_it_was_read_changes_nothing(tmp_path, monkeypatch):
    backend = ScriptedBackend("Омлет.")
    async with runner_env(tmp_path, backend) as env:
        await env.submit()
        entered, proceed = gate_on(monkeypatch, "begin_turn")
        request = asyncio.ensure_future(env.submit())  # the same key: a replay
        await reached(entered)
        cancel_turns(env.runner)
        await cancelled(request)

        proceed.set()
        await settled(env)

        turn = env.turn()
        assert (turn.status, turn.attempt_count) == ("completed", 1)  # a completed request is left alone
        assert backend.calls == 1


# --- during the call to the assistant (step 2) --------------------------------------------------------------


async def test_a_turn_cancelled_during_the_call_is_marked_interrupted_and_drains(tmp_path):
    backend = ScriptedBackend()
    arrived, _ = backend.hold()  # never released
    async with runner_env(tmp_path, backend) as env:
        request = asyncio.ensure_future(env.submit())
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        cancel_turns(env.runner)
        await cancelled(request)

        assert env.runner.unfinished > 0  # the record of the interruption is still to be written
        await settled(env)

        turn = env.turn()
        assert (turn.status, turn.error_code) == ("failed", "interrupted")
        assert backend.cancelled == 1  # the call itself is cancelled (nothing synchronous in it)
        assert env.count(Message) == 1


# --- during the completion (step 3) ---------------------------------------------------------------------------


async def test_a_completion_that_commits_after_the_turn_was_cancelled_stands_and_is_replayed(
    tmp_path, monkeypatch
):
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "complete_turn")
    async with runner_env(tmp_path, backend) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)  # answered; the transaction that stores the answer is about to run
        cancel_turns(env.runner)
        await cancelled(request)

        assert env.runner.unfinished > 0
        assert env.turn().status == "processing"
        proceed.set()
        await settled(env)

        turn = env.turn()
        assert (turn.status, turn.error_code, turn.attempt_count) == ("completed", None, 1)  # not "interrupted"
        assert env.count(Message) == 2
        replay = await env.submit()
        assert replay.replayed is True and replay.completed.assistant_message.text == "Омлет."
        assert backend.calls == 1


async def test_a_completion_that_fails_after_the_turn_was_cancelled_is_marked_interrupted(
    tmp_path, monkeypatch
):
    backend = ScriptedBackend("первый ответ", "второй ответ")
    entered, proceed = gate_on(monkeypatch, "complete_turn", fail=operational_error())
    async with runner_env(tmp_path, backend) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)
        cancel_turns(env.runner)
        await cancelled(request)

        proceed.set()
        await settled(env)

        turn = env.turn()
        assert (turn.status, turn.error_code) == ("failed", "interrupted")  # a retry need not wait
        assert env.count(Message) == 1
        monkeypatch.undo()
        retry = await env.submit()
        assert retry.completed.assistant_message.text == "второй ответ" and env.turn().attempt_count == 2


async def test_a_completion_refused_for_a_lost_lease_after_the_turn_was_cancelled_changes_nothing_more(
    tmp_path, monkeypatch
):
    backend = ScriptedBackend("первый ответ")
    entered, proceed = gate_on(monkeypatch, "complete_turn")
    async with runner_env(tmp_path, backend) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)
        cancel_turns(env.runner)
        await cancelled(request)
        env.clock.advance(seconds=91)  # the lease ends while the transaction waits for its turn

        proceed.set()
        await settled(env)

        turn = env.turn()
        assert (turn.status, turn.attempt_count) == ("processing", 1)  # LeaseLost: nothing recorded over it
        assert env.count(Message) == 1


# --- stopping the runner (aclose) ---------------------------------------------------------------------------------


async def test_stopping_with_a_claim_still_to_commit_returns_in_bounded_time_and_the_claim_is_recorded_later(
    tmp_path, monkeypatch, caplog
):
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "begin_turn")
    caplog.set_level(logging.WARNING)
    async with runner_env(tmp_path, backend, shutdown_grace_seconds=GRACE) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)

        await asyncio.wait_for(env.runner.aclose(), TIMEOUT)  # returns although the claim is still to be made

        await cancelled(request)
        assert env.runner.unfinished > 0  # and the runner says so ...
        assert "stopped with work unfinished" in caplog.text  # ... and logs it
        assert env.count(TurnRequest) == 0
        proceed.set()  # the claim commits after aclose() has returned
        await settled(env)

        turn = env.turn()
        assert (turn.status, turn.error_code) == ("failed", "interrupted")
        assert backend.calls == 0
        # the application is stopping, so a new runner stands in for the next process
        second = WebTurnRunner(env.database, backend, env.clock)
        try:
            retry = await env.submit(runner=second)
        finally:
            await second.aclose()
        assert retry.completed.assistant_message.text == "Омлет." and env.turn().attempt_count == 2


async def test_stopping_with_a_completion_still_to_commit_keeps_the_answer(tmp_path, monkeypatch):
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "complete_turn")
    async with runner_env(tmp_path, backend, shutdown_grace_seconds=GRACE) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)

        await asyncio.wait_for(env.runner.aclose(), TIMEOUT)
        await cancelled(request)
        proceed.set()
        await settled(env)

        assert (env.turn().status, env.count(Message)) == ("completed", 2)


async def test_a_unit_the_runner_waits_for_is_never_cancelled_by_stopping_it(tmp_path, monkeypatch):
    """aclose() cancels turns, not transactions. A cancelled unit would leave the claim
    unrecorded: the settling task could not tell what the transaction did."""
    backend = ScriptedBackend()
    entered, proceed = gate_on(monkeypatch, "begin_turn")
    async with runner_env(tmp_path, backend, shutdown_grace_seconds=GRACE) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)
        units = set(env.runner._work)
        assert units  # the claim is a unit

        await env.runner.aclose()
        await cancelled(request)

        assert not any(unit.cancelled() for unit in units)
        proceed.set()
        await settled(env)
        assert env.turn().error_code == "interrupted"


async def test_stopping_lets_a_slow_claim_finish_and_the_turn_go_on_when_it_is_within_the_grace(
    tmp_path, monkeypatch
):
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "begin_turn")
    async with runner_env(tmp_path, backend, shutdown_grace_seconds=5.0) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)
        closing = asyncio.ensure_future(env.runner.aclose())
        await asyncio.sleep(0)  # let it start waiting

        proceed.set()
        await asyncio.wait_for(closing, TIMEOUT)

        result = await request
        assert result.completed.assistant_message.text == "Омлет." and env.turn().status == "completed"
        assert env.runner.unfinished == 0


async def test_when_the_database_has_closed_before_the_late_claim_ends_the_lease_is_the_recovery(
    tmp_path, monkeypatch, caplog
):
    """The one case where the interruption cannot be written: the database was closed
    (its own grace expired) while a claim was still to commit. The claim stays PROCESSING,
    nothing raises, the failure to record is logged by class name, and once the lease is
    over the same key claims the request again."""
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "begin_turn")
    caplog.set_level(logging.ERROR)
    async with runner_env(
        tmp_path, backend, shutdown_grace_seconds=GRACE, database_options={"drain_timeout": GRACE}
    ) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)
        await asyncio.wait_for(env.runner.aclose(), TIMEOUT)
        await cancelled(request)
        await asyncio.wait_for(env.database.aclose(), TIMEOUT)  # as the application does next

        proceed.set()  # the claim commits into a database that is closed to the runner
        await settled(env)

        turn = env.turn()
        assert (turn.status, turn.attempt_count) == ("processing", 1)
        assert "A failed chat turn could not be recorded" in caplog.text
        assert "DatabaseClosed" in caplog.text and "SECRET" not in caplog.text
        assert env.count(Message) == 1

    # the next process: a new engine over the same file, and the lease has run out
    engine = create_db_engine(sqlite_url(tmp_path / "web.db"))
    database = WebDatabase(engine)
    try:
        runner = WebTurnRunner(database, backend, env.clock)
        try:
            with pytest.raises(ApiError) as early:
                await env.submit(runner=runner)  # still within the lease
            assert early.value.code == "turn_in_progress"
            env.clock.advance(seconds=91)
            retry = await env.submit(runner=runner)
        finally:
            await runner.aclose()
        with create_session_factory(engine)() as db:
            row = db.scalars(sa.select(TurnRequest)).one()
    finally:
        await database.aclose()
    assert retry.completed.assistant_message.text == "Омлет."
    assert (row.status, row.attempt_count) == ("completed", 2)


async def test_drain_waits_for_a_unit_that_is_still_running_even_after_its_turn_is_gone(tmp_path, monkeypatch):
    backend = ScriptedBackend()
    entered, proceed = gate_on(monkeypatch, "begin_turn")
    async with runner_env(tmp_path, backend) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)
        cancel_turns(env.runner)
        await cancelled(request)
        assert not env.runner._turns  # no turn is left ...

        await env.runner.drain(0.05)  # ... but the transaction is, so this times out

        assert env.runner.unfinished > 0
        proceed.set()
        await settled(env)


# --- a call that has not been accepted yet is withdrawn, one that has is waited for ---------------------


async def hold_the_only_slot(env):
    """Occupy the only slot of the database with a unit that waits until released.
    Returns (the call, a release function)."""
    held, release = threading.Event(), threading.Event()

    def hold(session):
        held.set()
        assert release.wait(TIMEOUT)

    blocker = asyncio.ensure_future(env.database.run(hold))
    await reached(held)
    return blocker, release.set


async def let_waiting_calls_wait():
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_claim_still_waiting_for_a_slot_when_its_turn_is_cancelled_never_runs(tmp_path, monkeypatch):
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "begin_turn")
    proceed.set()  # (the function would go straight through, if it ever ran)
    async with runner_env(tmp_path, backend, database_options={"max_concurrent": 1}) as env:
        blocker, free_the_slot = await hold_the_only_slot(env)
        request = asyncio.ensure_future(env.submit())
        await let_waiting_calls_wait()  # the claim waits for the slot, not yet accepted
        assert env.runner.unfinished > 0

        cancel_turns(env.runner)
        await cancelled(request)
        free_the_slot()
        await asyncio.wait_for(blocker, TIMEOUT)
        await settled(env)

        assert not entered.is_set()  # begin_turn never started
        assert env.count(TurnRequest) == 0 and env.count(Message) == 0 and backend.calls == 0


async def test_a_completion_still_waiting_for_a_slot_when_its_turn_is_cancelled_is_withdrawn_and_marked(
    tmp_path, monkeypatch
):
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "complete_turn")
    proceed.set()
    async with runner_env(tmp_path, backend, database_options={"max_concurrent": 1}) as env:
        arrived, release_backend = backend.hold()
        request = asyncio.ensure_future(env.submit())
        await asyncio.wait_for(arrived.wait(), TIMEOUT)  # claimed; the backend is working
        blocker, free_the_slot = await hold_the_only_slot(env)
        release_backend.set()
        await let_waiting_calls_wait()  # the completion waits for the slot, not yet accepted

        cancel_turns(env.runner)
        await cancelled(request)
        free_the_slot()
        await asyncio.wait_for(blocker, TIMEOUT)
        await settled(env)

        assert not entered.is_set()  # complete_turn never started: the reply is discarded ...
        turn = env.turn()
        assert (turn.status, turn.error_code) == ("failed", "interrupted")  # ... and the request released
        assert env.count(Message) == 1


async def test_a_call_that_was_accepted_is_not_withdrawn_when_its_turn_is_cancelled(tmp_path, monkeypatch):
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "begin_turn")
    async with runner_env(tmp_path, backend, database_options={"max_concurrent": 1}) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)  # accepted, and running on its thread

        cancel_turns(env.runner)
        await cancelled(request)
        proceed.set()
        await settled(env)

        assert env.turn().error_code == "interrupted"  # it did run: the claim was made, then released
