"""A Telegram update belongs to the bot, not to whoever the sending account is tied to this week.

The finding of the second independent review: chat T belongs to user A; update U reaches the
assistant and the attempt times out; T is unlinked from A and linked to B; Telegram brings U
again. The old look was through the CURRENT owner's conversations only, so U looked new, the
assistant ran again, and A's words were stored in B's history.

Now the update's id is looked up across owners, in the claim's own transaction as well as before
anything is fetched, and an update that was admitted for somebody else is dropped: the person
behind the account gets the fixed "the connection changed" notice, and nothing of the first
owner's message, photo or answer is read, fetched, stored, sent to the assistant or sent on. What
the same owner gets (the replay of a completed turn, the refusal of an attempt that began and did
not complete) and what the website does with its own retry are unchanged (tests/
test_telegram_redelivery.py, which also shows them).

"No row exists for an update that never reached a claim" is the one thing this cannot see: such
an update is a new message for whoever the account is tied to when it comes. The tests say so.
"""

import asyncio
import logging
import threading
from contextlib import contextmanager
from uuid import UUID

import pytest

from house_helper.persistence import telegram as store
from house_helper.persistence import turns
from house_helper.persistence.models import Conversation, Message, MessageAttachment
from house_helper.web import telegram_chat as tc
from house_helper.web.turn_runner import TELEGRAM_CHANNEL, TurnNotAuthorized
from tests.chat_support import ScriptedBackend, error_of, log_text, reached
from tests.photo_support import jpeg
from tests.telegram_support import MOTHER, TelegramHarness, photo_message, private_message, telegram_app
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

QUESTION = "Что приготовить из кабачков? (секрет первой: аллергия на орехи)"
UPDATE = 7345
NOTHING_STARTS_WITHIN = 0.2


@contextmanager
def unrecorded_failure(monkeypatch):
    """The process goes away before it can write that the attempt failed: the request stays as
    its claim left it (PROCESSING, with a lease)."""
    with monkeypatch.context() as patch:
        patch.setattr(turns, "fail_turn", lambda *args, **kwargs: False)
        yield


def second_person(t: TelegramHarness):
    return next(iter(t.chat.extra_users.values()))


async def hand_over(t: TelegramHarness, other) -> None:
    """The account is unlinked from the first person and linked to the second."""
    await t.service.unlink(t.user_id)
    await t.pair(MOTHER, client=other)
    assert [link.user_id for link in t.links()] == [second_person(t)]


def nothing_of_the_first_owner_reached_the_second(t: TelegramHarness) -> None:
    """The second person has no conversation, message, request or photo, and the first person's
    history is as it was."""
    second = second_person(t)
    assert t.conversations(second) == []
    owned = {c.id for c in t.chat.rows(Conversation)}
    assert owned == {c.id for c in t.conversations(t.user_id)}
    assert all(row.user_id != second for row in t.chat.rows(MessageAttachment))


def fetches(t: TelegramHarness) -> int:
    return len([r for r in t.bot.requests if r.url.path.endswith("/getFile") or r.url.path.startswith("/file/")])


# --- the review's scenario, in each state the first attempt can have left ------------------------------------------


async def test_a_completed_update_redelivered_after_the_account_changed_hands_is_dropped(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    backend = ScriptedBackend("Икра из кабачков.", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        update = private_message(UPDATE, MOTHER, QUESTION)
        await t.deliver_update(update)
        [conversation] = t.conversations()
        history = t.stored_texts(conversation.id)
        await hand_over(t, other)
        before = len(t.bot.sent)

        await t.deliver_update(update)
        await t.settle()

        assert backend.calls == 1  # no provider call
        assert t.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]  # fixed words; not the answer, not the question
        assert t.stored_texts(conversation.id) == history  # the first owner's history is as it was
        [turn] = t.all_turns()
        assert turn.status == "completed" and turn.conversation_id == conversation.id  # ... and its owner is unchanged
        nothing_of_the_first_owner_reached_the_second(t)
        assert t.service._chat_locks == {} and t.service._binding_locks == {}

    log = log_text(caplog)
    assert "admitted for another account" in log
    assert "кабачк" not in log and "орех" not in log and str(UPDATE) not in log


async def test_a_timed_out_update_redelivered_after_the_account_changed_hands_is_dropped_not_reported_as_not_run_again(
    tmp_path,
):
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        update = private_message(UPDATE, MOTHER, QUESTION)
        await t.deliver_update(update)  # the first owner's attempt fails
        [turn_before] = t.all_turns()
        assert (turn_before.status, turn_before.attempt_count) == ("failed", 1)
        await hand_over(t, other)
        before = len(t.bot.sent)

        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]  # neither NOT_RUN_AGAIN nor a failure notice
        [turn] = t.all_turns()
        assert (turn.id, turn.status, turn.attempt_count, turn.error_code) == (
            turn_before.id,
            "failed",
            1,
            "provider_timeout",
        )
        assert [m.role for m in t.all_messages()] == ["user"]  # the first owner's own, nothing added or invented
        nothing_of_the_first_owner_reached_the_second(t)


@pytest.mark.parametrize("seconds", [30, 200], ids=["lease-live", "lease-expired"])
async def test_an_update_left_in_flight_by_a_crash_is_dropped_for_the_new_owner_whatever_the_lease_says(
    tmp_path, monkeypatch, seconds
):
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        update = private_message(UPDATE, MOTHER, QUESTION)
        with unrecorded_failure(monkeypatch):
            await t.deliver_update(update)
        [turn_before] = t.all_turns()
        assert (turn_before.status, turn_before.attempt_count) == ("processing", 1)
        await hand_over(t, other)
        t.chat.clock.advance(seconds=seconds)
        before = len(t.bot.sent)

        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]  # neither STILL_BUSY nor NOT_RUN_AGAIN
        [turn] = t.all_turns()
        assert (turn.id, turn.status, turn.attempt_count) == (turn_before.id, "processing", 1)  # not touched
        nothing_of_the_first_owner_reached_the_second(t)


async def test_a_photo_update_redelivered_after_the_account_changed_hands_is_neither_fetched_nor_stored_again(tmp_path):
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        t.bot.files["photo-1"] = ("photos/photo-1.jpg", jpeg(4000, "o"))
        update = photo_message(UPDATE, MOTHER, "photo-1", caption="Что это?")
        await t.deliver_update(update)
        assert fetches(t) == 2 and len(t.chat.rows(MessageAttachment)) == 1
        await hand_over(t, other)
        before = len(t.bot.sent)

        await t.deliver_update(update)

        assert fetches(t) == 2  # not a second getFile, not a second download
        assert [row.user_id for row in t.chat.rows(MessageAttachment)] == [t.user_id]  # the first owner's, still
        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]
        nothing_of_the_first_owner_reached_the_second(t)


# --- what is unchanged -----------------------------------------------------------------------------------------


async def test_the_same_owner_linked_again_still_gets_the_replay_of_a_completed_update(tmp_path):
    backend = ScriptedBackend("Икра из кабачков.", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(UPDATE, MOTHER, QUESTION)
        await t.deliver_update(update)
        await t.service.unlink(t.user_id)
        await t.pair(MOTHER)  # the same person, the same account, a new link

        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[-1] == "Икра из кабачков."  # answered from the first completion, as before
        assert [turn.status for turn in t.all_turns()] == ["completed"]


async def test_the_same_owner_linked_again_still_gets_the_refusal_of_an_attempt_that_did_not_complete(tmp_path):
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(UPDATE, MOTHER, QUESTION)
        await t.deliver_update(update)
        await t.service.unlink(t.user_id)
        await t.pair(MOTHER)

        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[-1] == tc.NOT_RUN_AGAIN
        assert [(turn.status, turn.attempt_count) for turn in t.all_turns()] == [("failed", 1)]


async def test_an_update_that_never_reached_a_claim_is_a_new_message_for_whoever_the_account_is_tied_to_then(tmp_path):
    """The honest limit: nothing was written for it, so nothing can be said about it. (A dropped
    update is a new message; the first person's words were never kept, so nothing of theirs is
    in the second person's history that was not typed to the second person's account.)"""
    backend = ScriptedBackend("Ответ второму")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        update = private_message(UPDATE, MOTHER, "Привет")
        await t.deliver_update(update)  # the account is nobody's: the stranger's notice, no claim
        assert t.all_turns() == []
        await t.pair(MOTHER, client=other)

        await t.deliver_update(update)

        assert backend.calls == 1 and t.bot.texts(MOTHER)[-1] == "Ответ второму"
        assert [turn.conversation_id for turn in t.all_turns()] == [t.conversations(second_person(t))[0].id]


async def test_the_websites_own_retry_is_unchanged_and_its_keys_do_not_clash_with_telegrams(tmp_path):
    backend = ScriptedBackend(TimeoutError(), "Ответ после повтора", "Ответ второму", "Ответ из Telegram")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        conversation = await t.chat.create()
        failed = await t.chat.send(conversation, QUESTION, "web-1")
        retry = await t.chat.send(conversation, QUESTION, "web-1")  # the person pressed "send again"
        assert failed.status_code == 504 and retry.status_code == 201
        assert retry.json()["assistant_message"]["text"] == "Ответ после повтора"

        # The second person types the same text on the page, with their own key: nothing to do with updates.
        theirs = await t.chat.create(client=other)
        again = await t.chat.send(theirs, QUESTION, "web-1", client=other)
        assert again.status_code == 201
        await t.settle()

        await t.deliver_update(private_message(UPDATE, MOTHER, "Новое сообщение"))  # and Telegram still works
        assert backend.calls == 4 and t.bot.texts(MOTHER)[-1] == "Ответ из Telegram"


# --- the website cannot make up a key that stands for somebody else's update ----------------------------------------


async def test_the_website_refuses_the_telegram_prefix_in_an_idempotency_key(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        conversation = await t.chat.create()

        refused = await t.chat.send(conversation, "Привет", f"tg-{UPDATE}")
        allowed = await t.chat.send(conversation, "Привет", "web-ok")

        assert refused.status_code == 400 and error_of(refused)["detail"] == "invalid_idempotency_key"
        assert allowed.status_code == 201
        assert t.all_turns()[0].idempotency_key == "web-ok" and len(t.all_turns()) == 1


async def test_a_key_made_up_on_the_website_cannot_make_the_bot_refuse_a_real_update(tmp_path):
    backend = ScriptedBackend("Ответ первой")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        theirs = await t.chat.create(client=other)
        poisoned = await t.chat.send(theirs, "Привет", f"tg-{UPDATE}", client=other)  # the second person tries
        await t.pair(MOTHER)

        await t.deliver_update(private_message(UPDATE, MOTHER, QUESTION))

        assert poisoned.status_code == 400
        assert backend.calls == 1 and t.bot.texts(MOTHER)[-1] == "Ответ первой"


# --- the claim alone is enough --------------------------------------------------------------------------------------


async def test_the_claim_refuses_an_update_of_another_owner_even_when_the_look_before_it_was_wrong(tmp_path, monkeypatch):
    """The look before the claim is for the common case. What decides is the claim's own transaction,
    so even a look that finds nothing (here: one that is made blind) leaves the assistant unasked and
    the first owner's words out of the second person's history."""
    backend = ScriptedBackend("Икра из кабачков.", "Этот ответ запрашивать не следовало")

    class BlindLook:
        """persistence.telegram, except that the look before the claim sees nothing."""

        def __init__(self, real):
            self._real = real

        def update_admitted_for_another(self, *args, **kwargs):
            return False

        def __getattr__(self, name):
            return getattr(self._real, name)

    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        t.bot.files["photo-1"] = ("photos/photo-1.jpg", jpeg(4000, "p"))
        update = photo_message(UPDATE, MOTHER, "photo-1", caption="Что это?")
        await t.deliver_update(update)
        await hand_over(t, other)
        before = len(t.bot.sent)
        monkeypatch.setattr(tc, "store", BlindLook(store))

        await t.deliver_update(update)
        await t.settle()

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]
        assert len(t.all_turns()) == 1  # the second person's claim was rolled back with its message
        second = second_person(t)
        assert [m for m in t.all_messages() if m.conversation_id in {c.id for c in t.conversations(second)}] == []
        # The photo the blind look let the bot fetch for the second person was taken off again.
        assert [row.user_id for row in t.chat.rows(MessageAttachment)] == [t.user_id]


async def test_two_claims_for_one_update_by_two_owners_are_serialised_and_the_second_is_refused(tmp_path):
    """The concurrency of the claim itself, below the bot: the first owner's claim is held open
    inside its transaction; the second owner's claim for the same update cannot look until it has
    committed (SQLite's writer lock; PostgreSQL's row lock on the link is the reasoning in
    persistence.telegram.admit_update), and then finds it."""
    backend = ScriptedBackend("Ответ первой", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        runner = t.chat.app.state.chat
        first_conversation = await t.chat.create()
        second_conversation = await t.chat.create(client=other)
        second = second_person(t)
        entered, proceed = threading.Event(), threading.Event()

        def hold_the_claim_open(session):
            entered.set()
            assert proceed.wait(TIMEOUT), "the test never let the first claim go on"
            return True

        def submit(user_id, conversation_id, authorize):
            return asyncio.ensure_future(
                runner.submit(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=store.update_key(UPDATE),
                    text="Одно и то же сообщение",
                    correlation_id="hh-0123456789abcdef",
                    route="test",
                    origin=TELEGRAM_CHANNEL,
                    reclaim=False,
                    authorize=authorize,
                )
            )

        try:
            first = submit(t.user_id, uuid_of(first_conversation), hold_the_claim_open)
            await reached(entered)
            rival = submit(second, uuid_of(second_conversation), lambda session: not store.update_admitted_for_another(session, second, UPDATE))
            await asyncio.sleep(NOTHING_STARTS_WITHIN)  # (the absence of an event can only be given time)
            assert not rival.done()  # it cannot even claim while the first claim is open
        finally:
            proceed.set()

        assert (await asyncio.wait_for(first, TIMEOUT)).completed.assistant_message.text == "Ответ первой"
        with pytest.raises(TurnNotAuthorized):
            await asyncio.wait_for(rival, TIMEOUT)

        assert backend.calls == 1
        assert [turn.idempotency_key for turn in t.all_turns()] == [store.update_key(UPDATE)]  # one request, the first owner's
        assert [m.conversation_id for m in t.all_messages()] == [uuid_of(first_conversation)] * 2


def uuid_of(text: str) -> UUID:
    return UUID(text)


# --- a restart ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["completed", "crashed"])
async def test_a_restart_on_the_same_database_does_not_open_the_update_to_the_new_owner(tmp_path, monkeypatch, how):
    update = private_message(UPDATE, MOTHER, QUESTION)

    async with telegram_app(tmp_path, ScriptedBackend("Икра из кабачков." if how == "completed" else TimeoutError())) as first:
        other = await first.chat.second_user()
        await first.pair(MOTHER)
        if how == "crashed":
            with unrecorded_failure(monkeypatch):
                await first.deliver_update(update)
        else:
            await first.deliver_update(update)
        second_id = second_person(first)
        await hand_over(first, other)

    # A new process: nothing in memory, the same database. Telegram brings the update again.
    after = ScriptedBackend("Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, after, sign_in_first=False, seed=False) as second:
        second.chat.clock.advance(minutes=10)
        before = len(second.bot.sent)

        await second.deliver_update(update)
        await second.settle()

        assert after.calls == 0  # no provider call
        assert second.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]
        assert second.conversations(second_id) == []
        assert [turn.idempotency_key for turn in second.all_turns()] == [store.update_key(UPDATE)]
        assert len(second.chat.rows(Message)) == (2 if how == "completed" else 1)  # the first owner's, nothing more
