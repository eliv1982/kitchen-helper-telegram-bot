"""Unlinking never touches a tie it did not lock, and never lets go of the binding lock while its
database unit can still change the tie.

Two findings of the second independent review, reproduced here as exact interleavings (no sleeps
to arrange an order; the only waits are for conditions):

  * An unlink that found NO link used to fall back to an unconditional, unlocked delete of
    "whatever link the user has now". A link made in the gap (the chat linked to the user a
    moment after the look) was cut outside the binding lock, the chat was handed to somebody
    else, and the answer a delivery had already checked was sent to the new owner.
  * An unlink that was cancelled (a bare Task.cancel(): the page closed, a timeout) left its
    `async with binding` while its database unit was still running on the worker thread. The
    lock that says "the tie is not being taken apart" was free while it was.

The persistence helpers are tests/test_persistence_telegram.py; the primitive that holds the
caller until the unit is over is tests/test_web_database.py; the redelivery rules are
tests/test_telegram_cross_owner.py and tests/test_telegram_redelivery.py.
"""

import asyncio
import json
import threading
from contextlib import contextmanager

import pytest

from house_helper.persistence import telegram as store
from house_helper.persistence.models import TelegramLink
from house_helper.web import telegram_chat as tc
from house_helper.web.diagnostics import ApiError
from tests.chat_support import ScriptedBackend, post_headers, reached
from tests.telegram_support import MOTHER, TelegramHarness, in_line, telegram_app, until
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

OTHER_ACCOUNT = 555000333


class Seam:
    """The bot's database with a seam in it: the test can stop one named task before its Nth
    database call is made, and run something right after one. A task is told apart by its name,
    so the calls of the unlink under test are counted and nobody else's are."""

    def __init__(self, real) -> None:
        self._real = real
        self._calls: dict[str, int] = {}
        self._holds: dict[tuple[str, int], tuple[asyncio.Event, asyncio.Event]] = {}
        self._after: dict[tuple[str, int], object] = {}

    def hold_before(self, task: str, nth: int) -> tuple[asyncio.Event, asyncio.Event]:
        """(arrived, release): the Nth call of `task` waits for `release` before it starts."""
        self._holds[(task, nth)] = (asyncio.Event(), asyncio.Event())
        return self._holds[(task, nth)]

    def after(self, task: str, nth: int, action) -> None:
        self._after[(task, nth)] = action

    async def _through(self, method: str, work, **options):
        name = asyncio.current_task().get_name()
        number = self._calls[name] = self._calls.get(name, 0) + 1
        gate = self._holds.get((name, number))
        if gate is not None:
            gate[0].set()
            await gate[1].wait()
        result = await getattr(self._real, method)(work, **options)
        action = self._after.get((name, number))
        if action is not None:
            action()
        return result

    async def run(self, work, **options):
        return await self._through("run", work, **options)

    async def run_to_completion(self, work, **options):
        return await self._through("run_to_completion", work, **options)

    def __getattr__(self, name):
        return getattr(self._real, name)


def interpose(t: TelegramHarness) -> Seam:
    seam = Seam(t.service._database)
    t.service._database = seam
    return seam


def sending(t: TelegramHarness, text: str):
    """True when a sendMessage with exactly this text has reached the (held) Bot API."""

    def reached_bot() -> bool:
        return any(
            request.url.path.endswith("/sendMessage") and json.loads(request.content)["text"] == text
            for request in t.bot.requests
        )

    return reached_bot


def assert_idle(t: TelegramHarness) -> None:
    assert t.service._chat_locks == {} and t.service._binding_locks == {}


async def yield_to_the_loop(rounds: int = 30) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def gate_store_unlink(monkeypatch):
    """persistence.telegram.unlink waits, on its worker thread, before it does anything. Returns
    (entered, proceed): `entered` is set when the worker is inside the call, and the call goes
    on when the test sets `proceed`."""
    entered, proceed = threading.Event(), threading.Event()
    real = store.unlink

    def gated(session, user_id, **options):
        entered.set()
        assert proceed.wait(TIMEOUT), "the test never let the unlink go on"
        return real(session, user_id, **options)

    monkeypatch.setattr(store, "unlink", gated)
    return entered, proceed


def move_link(t: TelegramHarness, account: int):
    """Make the first person's link a link to `account` instead, straight in the database: the tie
    changes hands in the gap between a look and a lock. (A stored link cannot be edited, so it is
    replaced.)"""

    def move() -> None:
        with t.chat.base.factory() as db, db.begin():
            link = db.query(TelegramLink).filter(TelegramLink.user_id == t.user_id).one()
            made = link.created_at
            db.delete(link)
            db.flush()
            db.add(TelegramLink(user_id=t.user_id, telegram_user_id=account, created_at=made))

    return move


@contextmanager
def always_released(held: list):
    """Whatever the test does, every gate in `held` (more may be added while it runs) is opened on
    the way out: a test that fails with a worker or a send still held would otherwise hang the
    shutdown and hide its own failure under a time-out."""
    try:
        yield held
    finally:
        for gate in held:
            gate.set()


# --- finding 1: an unlink that found no link cannot cut a link made after it looked -------------------------


async def test_an_unlink_that_found_no_link_does_not_cut_a_link_made_after_it_looked(tmp_path):
    """The exact interleaving of the review. (1) The first person's unlink looks: no link. (2) The
    chat is linked to them. (3) A website turn starts to be copied to that chat, under the binding
    lock. (4) The unlink goes on to its second step. (5) The chat is offered to the second person.
    (6) The copy is sent. The first person's link must have survived (4), the chat must not have
    been handed over at (5), and the copy goes to the chat while it is still theirs."""
    async with telegram_app(tmp_path, ScriptedBackend("Ответ первой")) as t:
        other = await t.chat.second_user()
        seam = interpose(t)
        arrived, release = seam.hold_before("unlink-first", 2)
        with always_released([release]) as held:
            unlinking = asyncio.create_task(t.service.unlink(t.user_id), name="unlink-first")
            await asyncio.wait_for(arrived.wait(), TIMEOUT)  # (1) it has looked; its next step is held
            assert t.links() == []

            await t.pair(MOTHER)  # (2)
            assert [link.user_id for link in t.links()] == [t.user_id]

            conversation = await t.chat.create()
            held.append(gate := t.bot.hold_sends())
            await t.chat.send(conversation, "Что на ужин?", "web-1")  # (3)
            await until(sending(t, tc.FROM_WEBSITE + "Что на ужин?"))
            assert t.service._binding_locks[MOTHER][0].locked()

            release.set()  # (4)
            await asyncio.wait_for(unlinking, TIMEOUT)
            assert unlinking.result() is False  # there was no link when it looked: nothing was removed
            assert [link.user_id for link in t.links()] == [t.user_id]  # ... and nothing was removed after

            response = await other.post("/api/telegram/link", json={}, headers=post_headers())  # (5)
            code = response.json()["code"]
            offered = asyncio.ensure_future(t.deliver(MOTHER, f"/start {code}"))
            await until(lambda: any(c.consumed_at is not None for c in t.codes() if c.user_id != t.user_id))
            [link] = t.links()
            assert link.user_id == t.user_id and link.telegram_user_id == MOTHER  # still the first person's

            gate.set()  # (6)
            await offered
            await t.settle()
            assert_idle(t)

    [link] = t.links()
    assert link.user_id == t.user_id and link.telegram_user_id == MOTHER
    # (The copy and the notice both left the held Bot API at once; their order between them is not the point.)
    assert set(t.bot.texts(MOTHER)[-3:]) == {tc.FROM_WEBSITE + "Что на ужин?", "Ответ первой", tc.TELEGRAM_TAKEN}


async def test_an_unlink_with_no_link_still_withdraws_the_pairing_codes_the_person_holds(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        response = await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())
        code = response.json()["code"]
        assert len(t.codes()) == 1

        removed = await t.service.unlink(t.user_id)  # no link yet: the code is what there is to cancel

        assert removed is False and t.codes() == []
        await t.deliver(MOTHER, f"/start {code}")  # the code that was shown is no longer a way in
        assert t.links() == [] and t.bot.texts(MOTHER)[-1] == tc.BAD_CODE
        assert_idle(t)


async def test_the_other_persons_codes_are_left_alone_by_an_unlink_with_no_link(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        other = await t.chat.second_user()
        await other.post("/api/telegram/link", json={}, headers=post_headers())
        [theirs] = t.codes()

        await t.service.unlink(t.user_id)

        assert [code.id for code in t.codes()] == [theirs.id]


async def test_a_tie_that_changed_between_the_look_and_the_lock_is_looked_up_again_and_removed_under_its_own_lock(
    tmp_path,
):
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        await t.pair(MOTHER)
        seam = interpose(t)
        # After the look (call 1) the tie moves to another account; the locked removal of the
        # account that was seen (call 2) finds nothing to remove and must not remove anything.
        seam.after("unlink-first", 1, move_link(t, OTHER_ACCOUNT))
        locks_taken: list[int] = []
        real = t.service._binding

        def spy(account):
            locks_taken.append(account)
            return real(account)

        t.service._binding = spy

        removed = await asyncio.create_task(t.service.unlink(t.user_id), name="unlink-first")

        assert removed is True
        assert locks_taken == [MOTHER, OTHER_ACCOUNT]  # each removal under the lock of ITS account
        assert t.links() == []
        assert_idle(t)


async def test_a_tie_that_keeps_changing_is_never_removed_without_its_lock_and_the_caller_is_told(tmp_path):
    """The old code deleted whatever link the user had, unlocked, after the attempts ran out. Now
    the attempts run out in an error, and the tie that was last seen is still there."""
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        await t.pair(MOTHER)
        seam = interpose(t)
        # A look is every other call: 1, 3, 5, ... Each time the tie moves right after it.
        for number, account in zip(range(1, 2 * tc.UNLINK_ATTEMPTS, 2), range(OTHER_ACCOUNT, OTHER_ACCOUNT + 10)):
            seam.after("unlink-first", number, move_link(t, account))

        [outcome] = await asyncio.gather(
            asyncio.create_task(t.service.unlink(t.user_id), name="unlink-first"), return_exceptions=True
        )

        assert isinstance(outcome, ApiError) and outcome.code == "service_unavailable"
        [link] = t.links()  # not removed: nobody held the lock of the account it points at now
        assert link.user_id == t.user_id
        assert_idle(t)


# --- finding 2: cancelling the unlink does not release the binding lock under its database unit ----------------


async def test_cancelling_an_unlink_does_not_release_the_binding_lock_while_its_unit_can_still_run(
    tmp_path, monkeypatch
):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ первой")) as t:
        await t.pair(MOTHER)
        entered, proceed = gate_store_unlink(monkeypatch)
        with always_released([proceed]):
            unlinking = asyncio.create_task(t.service.unlink(t.user_id), name="unlink-first")
            await reached(entered)  # the worker is inside the removal, with the binding lock held

            unlinking.cancel()  # a bare Task.cancel(): the page went away, a timeout
            await yield_to_the_loop()
            assert not unlinking.done()  # it does not leave while the worker can still change the tie
            assert t.service._binding_locks[MOTHER][0].locked()

            conversation = await t.chat.create()
            await t.chat.send(conversation, "Что на ужин?", "web-1")  # a copy wants to check the tie and send
            await until(in_line(t, MOTHER, 2, "_binding_locks"))  # ... and waits for the lock
            before = len(t.bot.sent)
            assert len(t.links()) == 1  # the removal has not committed: the copy must not be checked against this

            proceed.set()
            await asyncio.wait_for(asyncio.wait({unlinking}), TIMEOUT)
            assert unlinking.cancelled()  # the cancellation is delivered, once the unit is over
            await t.settle()

            assert t.links() == []  # the unit that was running when it was cancelled did commit
            assert t.bot.texts(MOTHER)[before:] == []  # the copy found the tie gone: dropped, not sent
            assert_idle(t)


async def test_a_second_and_a_third_cancellation_change_nothing(tmp_path, monkeypatch):
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        await t.pair(MOTHER)
        entered, proceed = gate_store_unlink(monkeypatch)
        with always_released([proceed]):
            unlinking = asyncio.create_task(t.service.unlink(t.user_id), name="unlink-first")
            await reached(entered)

            for _ in range(3):
                unlinking.cancel()
                await yield_to_the_loop()
                assert not unlinking.done() and t.service._binding_locks[MOTHER][0].locked()

            proceed.set()
            await asyncio.wait_for(asyncio.wait({unlinking}), TIMEOUT)
            assert unlinking.cancelled() and t.links() == []
            assert_idle(t)


async def test_cancellation_of_the_unlink_command_is_held_back_the_same_way(tmp_path, monkeypatch):
    """/unlink is a handler of its own (a task of the bot); it takes the same path."""
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        await t.pair(MOTHER)
        entered, proceed = gate_store_unlink(monkeypatch)
        with always_released([proceed]):
            command = asyncio.create_task(t.deliver(MOTHER, "/unlink"))
            await reached(entered)

            command.cancel()
            await yield_to_the_loop()
            assert not command.done() and t.service._binding_locks[MOTHER][0].locked()

            proceed.set()
            await asyncio.wait_for(asyncio.wait({command}), TIMEOUT)
            assert command.cancelled() and t.links() == []
            await t.settle()
            assert_idle(t)


async def test_an_unlink_cancelled_while_it_waits_for_the_lock_runs_nothing_and_leaks_nothing(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ первой")) as t:
        await t.pair(MOTHER)
        gate = t.bot.hold_sends()
        with always_released([gate]):
            conversation = await t.chat.create()
            await t.chat.send(conversation, "Что на ужин?", "web-1")
            await until(sending(t, tc.FROM_WEBSITE + "Что на ужин?"))  # a copy holds the binding lock
            unlinking = asyncio.create_task(t.service.unlink(t.user_id), name="unlink-first")
            await until(in_line(t, MOTHER, 2, "_binding_locks"))  # it waits for the lock: nothing of it runs yet

            unlinking.cancel()
            await asyncio.wait_for(asyncio.wait({unlinking}), TIMEOUT)
            assert unlinking.cancelled()
            assert len(t.links()) == 1  # it never ran: the tie is whole
            assert t.service._binding_locks[MOTHER][1] == 1  # only the copy is left in line

            gate.set()
            await t.settle()
            assert_idle(t)
            assert len(t.links()) == 1
            assert t.bot.texts(MOTHER)[-2:] == [tc.FROM_WEBSITE + "Что на ужин?", "Ответ первой"]


async def test_after_a_cancelled_unlink_the_next_one_and_the_next_delivery_work(tmp_path, monkeypatch):
    """No deadlock and no stuck lock: after the cancelled unit has ended, everything goes on."""
    async with telegram_app(tmp_path, ScriptedBackend("Ответ первой", "Ответ второй")) as t:
        await t.pair(MOTHER)
        entered, proceed = gate_store_unlink(monkeypatch)
        with always_released([proceed]):
            unlinking = asyncio.create_task(t.service.unlink(t.user_id), name="unlink-first")
            await reached(entered)
            unlinking.cancel()
            proceed.set()
            await asyncio.wait_for(asyncio.wait({unlinking}), TIMEOUT)
            assert unlinking.cancelled() and t.links() == []

            await t.pair(MOTHER)  # linked again, as if nothing had happened
            assert await asyncio.wait_for(t.service.unlink(t.user_id), TIMEOUT) is True
            assert t.links() == []
            assert_idle(t)
            conversation = await t.chat.create()  # (and the runner and the database are still serving)
            assert (await t.chat.send(conversation, "Ещё вопрос", "web-2")).status_code == 201
