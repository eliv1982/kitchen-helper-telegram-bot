"""An update that Telegram delivers again is never run through the assistant a second time once an
attempt at it has begun.

The idempotency key of a Telegram turn is made of the update id ("tg-<id>"). What a repeat of the
update finds in the database decides what happens:

  * the turn completed          -> its answer is replayed (nothing is generated);
  * an attempt began and did not complete (failed, timed out, its lease ran out, the process
    went away)                  -> NOT run again: the person is told, in fixed words, to write the
                                   message anew; nothing is fabricated;
  * an attempt is still running -> the repeat waits for its turn in the chat and then finds the
                                   first of these.

The website's own retry (the person pressing "send again") is unchanged: it may run a failed
request again. What differs is who chose: nobody chose a redelivery.

The guarantee is about the assistant, not "exactly once" delivery: a completed answer can reach
the chat twice (test_telegram_sync.py shows it), and an acknowledged message that was being
handled when the process was killed gets no answer. The rules of who is served are
tests/test_telegram_binding.py; the lifecycle under them is
tests/test_persistence_turn_lifecycle.py.
"""

import asyncio
import logging
from contextlib import contextmanager

import pytest

from house_helper.persistence import turns
from house_helper.persistence.models import MessageAttachment
from house_helper.web import telegram_chat as tc
from house_helper.web.diagnostics import CATALOGUE
from tests.chat_support import ScriptedBackend, error_of, log_text
from tests.photo_support import jpeg
from tests.telegram_support import MOTHER, SISTER, in_line, photo_message, private_message, telegram_app, until
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

QUESTION = "Что приготовить из кабачков?"


def statuses(t):
    return [(turn.status, turn.attempt_count, turn.error_code) for turn in t.all_turns()]


@contextmanager
def unrecorded_failure(monkeypatch):
    """The process goes away before it can write that the attempt failed: the request stays as
    its claim left it (PROCESSING, with a lease). A `with` block: failures are recorded again
    afterwards, as they are in the next process."""
    with monkeypatch.context() as patch:
        patch.setattr(turns, "fail_turn", lambda *args, **kwargs: False)
        yield


# --- a provider failure, then the same update again ----------------------------------------------------


async def test_a_provider_timeout_followed_by_a_redelivery_does_not_ask_the_provider_again(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)

        await t.deliver_update(update)
        told_first = t.bot.texts(MOTHER)[-1]
        await t.deliver_update(update)  # Telegram brings it again

    assert backend.calls == 1  # the provider was asked once, ever
    assert told_first.startswith(CATALOGUE["provider_timeout"].message)
    assert t.bot.texts(MOTHER)[-1] == tc.NOT_RUN_AGAIN  # a state of its own, not the failure again
    assert statuses(t) == [("failed", 1, "provider_timeout")]  # nothing was claimed, changed or invented
    assert [m.role for m in t.all_messages()] == ["user"]  # no assistant message was fabricated
    log = log_text(caplog)
    assert "an attempt had begun" in log and "internal_error" not in log
    assert QUESTION not in log


async def test_the_person_can_still_send_the_message_again_as_a_new_one(tmp_path):
    backend = ScriptedBackend(TimeoutError(), "Запеканка из кабачков.")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        await t.deliver_update(update)

        await t.deliver(MOTHER, QUESTION)  # their own choice: a new message, a new update

    assert backend.calls == 2
    assert t.bot.texts(MOTHER)[-1] == "Запеканка из кабачков."
    assert [s[0] for s in statuses(t)] == ["failed", "completed"]


async def test_a_failure_to_store_the_answer_does_not_make_the_update_runnable_again(tmp_path, monkeypatch):
    """The claim is committed before the provider is called, in a transaction of its own: a
    later transaction that fails (here: the one that stores the answer) cannot take it back."""
    from sqlalchemy.exc import OperationalError

    def broken(*args, **kwargs):
        raise OperationalError("UPDATE", {}, Exception("disk I/O error"))

    backend = ScriptedBackend("Ответ, который не удалось сохранить", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        with monkeypatch.context() as patch:
            patch.setattr(turns, "complete_turn", broken)
            await t.deliver_update(update)
        assert statuses(t) == [("failed", 1, "storage_unavailable")]

        await t.deliver_update(update)

    assert backend.calls == 1
    assert t.bot.texts(MOTHER)[-1] == tc.NOT_RUN_AGAIN
    assert statuses(t) == [("failed", 1, "storage_unavailable")]


# --- the process goes away ---------------------------------------------------------------------------------------


async def test_while_the_lease_is_live_the_repeat_is_busy_and_once_it_ran_out_it_is_not_run(tmp_path, monkeypatch):
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        with unrecorded_failure(monkeypatch):
            await t.deliver_update(update)  # the claim stands, the failure was never written
        assert statuses(t) == [("processing", 1, None)]

        t.chat.clock.advance(seconds=30)  # inside the 90-second lease
        await t.deliver_update(update)
        busy = t.bot.texts(MOTHER)[-1]

        t.chat.clock.advance(seconds=61)  # the lease has run out
        await t.deliver_update(update)

    assert busy == tc.STILL_BUSY
    assert t.bot.texts(MOTHER)[-1] == tc.NOT_RUN_AGAIN
    assert backend.calls == 1
    assert statuses(t) == [("processing", 1, None)]  # the refusal changed nothing: no second claim


async def test_a_crash_after_the_attempt_began_then_a_restart_and_the_same_update_is_not_run_again(
    tmp_path, monkeypatch
):
    update = private_message(2500, MOTHER, QUESTION)

    async with telegram_app(tmp_path, ScriptedBackend(TimeoutError())) as first:
        await first.pair(MOTHER)
        with unrecorded_failure(monkeypatch):
            await first.deliver_update(update)  # claimed; the process "dies" with the failure unwritten
        assert statuses(first) == [("processing", 1, None)]

    # A new process: nothing in memory, the same database. Telegram brings the update again.
    after = ScriptedBackend("Ответ на новое сообщение")
    async with telegram_app(tmp_path, after, sign_in_first=False, seed=False) as second:
        second.chat.clock.advance(minutes=10)
        await second.deliver_update(update)
        told = second.bot.texts(MOTHER)[-1]
        calls_for_the_repeat = after.calls
        await second.deliver(MOTHER, "Напишу заново: " + QUESTION)  # the person's own new message

    assert told == tc.NOT_RUN_AGAIN and calls_for_the_repeat == 0
    assert after.calls == 1  # only the person's new message reached the assistant
    assert [s[0] for s in statuses(second)] == ["failed", "completed"]  # the old one retired by the newer message
    assert second.bot.texts(MOTHER)[-1] == "Ответ на новое сообщение"


async def test_the_claim_is_in_the_database_before_the_provider_is_asked(tmp_path):
    seen = []
    holder = {}

    def peek(messages):
        seen.append(statuses(holder["t"]))  # read through another connection, from inside the call
        return "Ответ"

    async with telegram_app(tmp_path, ScriptedBackend(peek)) as t:
        holder["t"] = t
        await t.pair(MOTHER)
        await t.deliver(MOTHER, QUESTION)

    assert seen == [[("processing", 1, None)]]


# --- a completed turn is replayed --------------------------------------------------------------------------------


async def test_a_completed_result_is_replayed_however_late_the_update_comes_back(tmp_path):
    backend = ScriptedBackend("Икра из кабачков.", "Суп из тыквы.")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        await t.deliver(MOTHER, "А на завтра?")  # life goes on, in the same conversation
        t.chat.clock.advance(hours=2)  # long after the lease of the first

        await t.deliver_update(update)

    assert backend.calls == 2
    assert t.bot.texts(MOTHER)[-1] == "Икра из кабачков."  # the answer to THIS update, as it was
    assert [s[0] for s in statuses(t)] == ["completed", "completed"]
    assert tc.NOT_RUN_AGAIN not in t.bot.texts(MOTHER)


async def test_a_repeat_that_comes_while_the_first_is_still_being_answered_waits_and_is_replayed(tmp_path):
    backend = ScriptedBackend("Единственный ответ")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        arrived, release = backend.hold()
        first = asyncio.ensure_future(t.deliver_update(update))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        second = asyncio.ensure_future(t.deliver_update(update))
        await until(in_line(t, MOTHER, 2))  # the repeat is waiting for its turn
        assert backend.calls == 1
        backend._gate = None
        release.set()
        await asyncio.gather(first, second)

    assert backend.calls == 1
    assert t.bot.texts(MOTHER)[-2:] == ["Единственный ответ"] * 2  # may be seen twice, run once
    assert statuses(t) == [("completed", 1, None)]


# --- a photograph ------------------------------------------------------------------------------------------------


async def test_a_redelivered_photo_whose_attempt_failed_is_neither_fetched_nor_run_again(tmp_path):
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        t.bot.files["photo-1"] = ("photos/photo-1.jpg", jpeg(4000, "r"))
        update = photo_message(t.next_id(), MOTHER, "photo-1", caption="Что это?")

        await t.deliver_update(update)
        await t.deliver_update(update)

    fetches = [r for r in t.bot.requests if r.url.path.endswith("/getFile") or r.url.path.startswith("/file/")]
    assert backend.calls == 1
    assert len(fetches) == 2  # one getFile and one download, for the first delivery only
    assert len(t.chat.rows(MessageAttachment)) == 1  # kept with the failed message, not stored twice
    assert t.bot.texts(MOTHER)[-1] == tc.NOT_RUN_AGAIN


# --- two people ---------------------------------------------------------------------------------------------------


async def test_a_refused_repeat_tells_nobody_else_anything_and_exposes_nothing(tmp_path):
    echo = lambda messages: "Ответ для: " + messages[-1]["content"]  # noqa: E731
    backend = ScriptedBackend(TimeoutError(), echo)
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await t.pair(SISTER, client=other)
        secret = private_message(t.next_id(), MOTHER, "Секрет мамы: аллергия на орехи")
        await t.deliver_update(secret)  # fails
        await t.deliver(SISTER, "Что на ужин?")  # the other person is served meanwhile
        sister_before = list(t.bot.texts(SISTER))

        await t.deliver_update(secret)  # the first person's update comes again

    assert backend.calls == 2  # the secret once, the sister's question once
    assert t.bot.texts(MOTHER)[-1] == tc.NOT_RUN_AGAIN
    assert "аллергия" not in tc.NOT_RUN_AGAIN and "орех" not in tc.NOT_RUN_AGAIN
    assert t.bot.texts(SISTER) == sister_before  # not a word reached the other person's chat
    other_user = t.chat.extra_users["7777777777777777777"]
    assert all("орех" not in repr(t.stored_texts(c.id)) for c in t.conversations(other_user))
    assert all("орех" not in repr(request[0]) for request in backend.requests[1:])


# --- the website's own retry is unchanged -------------------------------------------------------------------------


async def test_the_websites_own_retry_of_a_failed_message_still_generates_again(tmp_path):
    backend = ScriptedBackend(TimeoutError(), "Ответ после повтора")
    async with telegram_app(tmp_path, backend) as t:
        conversation_id = await t.chat.create()
        failed = await t.chat.send(conversation_id, QUESTION, "web-1")
        retry = await t.chat.send(conversation_id, QUESTION, "web-1")  # the person pressed "send again"

    assert failed.status_code == 504 and error_of(failed)["detail"] == "provider_timeout"
    assert retry.status_code == 201 and retry.json()["assistant_message"]["text"] == "Ответ после повтора"
    assert backend.calls == 2
    assert statuses(t) == [("completed", 2, None)]


async def test_a_retry_the_person_chose_may_run_a_failed_update_while_a_redelivery_may_not(tmp_path):
    """The same failed key: the runner refuses the automatic repeat (reclaim=False, what the bot
    passes for a redelivery) and accepts the chosen one (the default, what the website passes)."""
    backend = ScriptedBackend(TimeoutError(), "Ответ по выбору человека")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        await t.deliver_update(update)
        assert backend.calls == 1

        [conversation] = t.conversations()
        done = await t.chat.app.state.chat.submit(
            user_id=t.user_id,
            conversation_id=conversation.id,
            idempotency_key=f"tg-{update['update_id']}",
            text=QUESTION,
            correlation_id="hh-test",
            route="test",
        )

    assert backend.calls == 2 and done.completed.assistant_message.text == "Ответ по выбору человека"
    assert statuses(t) == [("completed", 2, None)]
