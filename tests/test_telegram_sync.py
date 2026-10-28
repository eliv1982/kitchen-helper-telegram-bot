"""One conversation store, two front doors: what the website and the bot share, and the
rules that keep the copying between them from echoing or repeating.

The bot's own behaviour (strangers, commands, pairing) is tests/test_telegram_chat.py.
"""

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

import pytest

from house_helper.core.backend import BackendRateLimitedError
from house_helper.persistence.base import RETENTION
from house_helper.web import telegram_chat as tc
from tests.chat_support import ScriptedBackend, error_of, log_text
from tests.telegram_support import (
    MOTHER,
    SISTER,
    STRANGER,
    TelegramHarness,
    private_message,
    telegram_app,
)

pytestmark = pytest.mark.asyncio


def roles_and_texts(messages):
    return [(m["role"], m["text"]) for m in messages]


# --- Telegram, then the website -------------------------------------------------------------


async def test_a_conversation_started_in_telegram_is_there_on_the_website_and_continues_there(tmp_path):
    backend = ScriptedBackend("Омлет из двух яиц.", "Да, можно добавить сыр.")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Что из яиц?")
        t.chat.clock.advance(seconds=5)

        listing = (await t.chat.client.get("/api/conversations")).json()
        [item] = listing["conversations"]
        history = (await t.chat.history(item["id"])).json()
        sent = await t.chat.send(item["id"], "А можно с сыром?", "web-1")
        await t.settle()
        after = (await t.chat.history(item["id"])).json()

    assert item["title"] == "Что из яиц?"  # named by the first thing she said, as the page names every conversation
    assert roles_and_texts(history["messages"]) == [("user", "Что из яиц?"), ("assistant", "Омлет из двух яиц.")]
    assert sent.status_code == 201
    # the assistant was shown the exchange that happened in Telegram
    assert backend.requests[1][0] == [
        {"role": "user", "content": "Что из яиц?"},
        {"role": "assistant", "content": "Омлет из двух яиц."},
        {"role": "user", "content": "А можно с сыром?"},
    ]
    assert roles_and_texts(after["messages"]) == [
        ("user", "Что из яиц?"),
        ("assistant", "Омлет из двух яиц."),
        ("user", "А можно с сыром?"),
        ("assistant", "Да, можно добавить сыр."),
    ]
    assert len(t.conversations()) == 1


async def test_a_telegram_message_after_a_web_conversation_continues_that_conversation(tmp_path):
    backend = ScriptedBackend("Борщ.", "Сметану подавайте отдельно.")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        await t.chat.send(conversation_id, "Что на обед?", "web-1")
        await t.settle()
        t.chat.clock.advance(seconds=5)

        await t.deliver(MOTHER, "А к нему что подать?")
        history = (await t.chat.history(conversation_id)).json()

    assert backend.requests[1][0][:2] == [
        {"role": "user", "content": "Что на обед?"},
        {"role": "assistant", "content": "Борщ."},
    ]
    assert roles_and_texts(history["messages"])[-2:] == [
        ("user", "А к нему что подать?"),
        ("assistant", "Сметану подавайте отдельно."),
    ]
    assert len(t.conversations()) == 1  # no second conversation was made


async def test_telegram_follows_the_conversation_that_was_active_last_on_either_channel(tmp_path):
    backend = ScriptedBackend("Ответ в старом", "Ответ в новом", "Ответ снова в старом")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        old = await t.chat.create()
        t.chat.clock.advance(seconds=5)
        new = await t.chat.create()
        t.chat.clock.advance(seconds=5)
        await t.chat.send(old, "В старом разговоре", "web-old")  # the older one is the active one now
        await t.settle()
        t.chat.clock.advance(seconds=5)
        await t.deliver(MOTHER, "Из Telegram")
        listing = (await t.chat.client.get("/api/conversations")).json()["conversations"]

    assert [c["id"] for c in listing][0] == old  # the website lists it first, so it opens it first
    assert [m for m in t.stored_texts(UUID(old)) if m[0] == "user"] == [("user", "В старом разговоре"), ("user", "Из Telegram")]
    assert t.stored_texts(UUID(new)) == []


# --- the website, then Telegram ------------------------------------------------------------------


async def test_what_is_said_on_the_website_arrives_in_telegram_labelled_then_the_answer(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Сварите суп.")) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        response = await t.chat.send(conversation_id, "Как варить суп?", "web-1")
        await t.settle()

    assert response.status_code == 201
    assert t.bot.texts(MOTHER) == [tc.LINKED, "Вы (на сайте):\nКак варить суп?", "Сварите суп."]
    assert t.bot.sent[-1]["chat_id"] == MOTHER


async def test_the_website_does_not_wait_for_telegram_and_is_not_changed_by_it(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        before = len(t.bot.sent)
        gate = t.bot.hold_sends()  # Telegram is slow

        response = await asyncio.wait_for(t.chat.send(conversation_id, "Привет", "web-1"), 10)
        in_telegram_while_waiting = len(t.bot.sent) - before
        gate.set()
        await t.settle()

    assert response.status_code == 201 and response.json()["assistant_message"]["text"] == "Ответ"
    assert in_telegram_while_waiting == 0
    assert t.bot.texts(MOTHER)[-2:] == ["Вы (на сайте):\nПривет", "Ответ"]


async def test_a_telegram_failure_never_reaches_the_website_and_does_not_unlink(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with telegram_app(tmp_path, ScriptedBackend("Первый", "Второй")) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        t.bot.fail("sendMessage", 403)  # "bot was blocked by the user"
        first = await t.chat.send(conversation_id, "Раз", "web-1")
        await t.settle()
        second = await t.chat.send(conversation_id, "Два", "web-2")
        await t.settle()

    assert (first.status_code, second.status_code) == (201, 201)
    assert t.bot.texts(MOTHER)[-2:] == ["Вы (на сайте):\nДва", "Второй"]  # the next turn is copied
    assert len(t.links()) == 1
    assert "category=api:403" in log_text(caplog)


async def test_a_person_with_no_telegram_linked_costs_no_call_to_telegram(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        conversation_id = await t.chat.create()
        response = await t.chat.send(conversation_id, "Привет", "web-1")
        await t.settle()

    assert response.status_code == 201
    assert t.bot.sent == [] and t.bot.actions == []


async def test_a_turn_is_copied_to_its_own_person_only(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Для мамы", "Для сестры")) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await t.pair(SISTER, client=other)
        conversation_id = await t.chat.create()
        await t.chat.send(conversation_id, "Мамин вопрос", "web-1")
        await t.settle()

    assert t.bot.texts(MOTHER)[-2:] == ["Вы (на сайте):\nМамин вопрос", "Для мамы"]
    assert all("Мамин" not in text and "Для мамы" not in text for text in t.bot.texts(SISTER))


async def test_a_failed_web_turn_is_not_copied_and_its_retry_is_copied_once(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend(BackendRateLimitedError("x"), "Теперь получилось")) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        failed = await t.chat.send(conversation_id, "Вопрос", "web-1")
        await t.settle()
        copied_after_failure = t.bot.texts(MOTHER)[1:]
        retry = await t.chat.send(conversation_id, "Вопрос", "web-1")
        await t.settle()

    assert failed.status_code == 503 and error_of(failed)["detail"] == "provider_rate_limited"
    assert copied_after_failure == []
    assert retry.status_code == 201
    assert t.bot.texts(MOTHER)[1:] == ["Вы (на сайте):\nВопрос", "Теперь получилось"]


async def test_a_retry_of_a_web_request_that_was_answered_is_not_copied_again(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        first = await t.chat.send(conversation_id, "Привет", "web-1")
        await t.settle()
        copied = list(t.bot.sent)
        again = await t.chat.send(conversation_id, "Привет", "web-1")  # the page retried
        await t.settle()

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["replayed"] is True
    assert t.bot.sent == copied
    assert t.chat.backend.calls == 1


async def test_a_web_request_whose_client_went_away_is_still_copied_once(tmp_path):
    backend = ScriptedBackend("Ответ")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        arrived, release = backend.hold()
        request = asyncio.ensure_future(t.chat.send(conversation_id, "Привет", "web-1"))
        await asyncio.wait_for(arrived.wait(), 10)
        request.cancel()  # the browser went away
        await asyncio.gather(request, return_exceptions=True)
        release.set()
        await t.settle()
        retry = await t.chat.send(conversation_id, "Привет", "web-1")
        await t.settle()

    assert retry.status_code == 200 and retry.json()["replayed"] is True
    assert t.bot.texts(MOTHER)[1:] == ["Вы (на сайте):\nПривет", "Ответ"]


# --- no echo ----------------------------------------------------------------------------------------


async def test_a_telegram_message_is_answered_once_and_never_copied_back(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Один ответ")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Привет")
        await t.settle()
        await asyncio.sleep(0.05)  # (a copy back, if there were one, would be in flight by now)
        await t.settle()

    assert t.bot.texts(MOTHER) == [tc.LINKED, "Один ответ"]
    assert t.chat.backend.calls == 1
    assert len(t.all_turns()) == 1


async def test_the_hook_ignores_a_turn_of_telegram_origin_and_a_closing_bot(tmp_path):
    from house_helper.web.turn_runner import TELEGRAM_CHANNEL, WEB_CHANNEL, TurnCompletion

    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        for origin in (TELEGRAM_CHANNEL, "anything-else"):
            t.service.on_completion(TurnCompletion(t.user_id, UUID(int=1), origin, "q", "a"))
        await t.settle()
        unchanged = len(t.bot.sent) == before
        t.service.on_completion(TurnCompletion(t.user_id, UUID(int=1), WEB_CHANNEL, "q", "a"))
        await t.settle()

    assert unchanged
    assert t.bot.texts(MOTHER)[-2:] == ["Вы (на сайте):\nq", "a"]


async def test_a_hook_that_raises_does_not_disturb_the_turn(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        def explode(completion):
            raise RuntimeError("secret detail")

        t.chat.app.state.chat.add_completion_hook(explode)
        conversation_id = await t.chat.create()
        response = await t.chat.send(conversation_id, "Привет", "web-1")
        await t.settle()

    assert response.status_code == 201
    assert "completion hook failed: category=RuntimeError" in log_text(caplog)
    assert "secret detail" not in log_text(caplog)


# --- the same update twice ---------------------------------------------------------------------------


async def test_an_update_delivered_twice_is_answered_from_the_first_run_not_run_again(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Первый и единственный")) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, "Что приготовить?")
        await t.deliver_update(update)
        await t.deliver_update(update)

    assert t.chat.backend.calls == 1
    [conversation] = t.conversations()
    assert t.stored_texts(conversation.id) == [("user", "Что приготовить?"), ("assistant", "Первый и единственный")]
    assert len(t.all_turns()) == 1
    # the person may see the answer twice (the first might not have arrived), never two answers
    assert t.bot.texts(MOTHER)[1:] == ["Первый и единственный"] * 2


async def test_a_redelivery_goes_to_the_conversation_of_its_first_delivery(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, "Первое")
        await t.deliver_update(update)
        [first] = t.conversations()
        t.chat.clock.advance(seconds=10)
        newer = await t.chat.create()  # the website starts another conversation in between
        t.chat.clock.advance(seconds=10)
        await t.deliver_update(update)

    assert t.chat.backend.calls == 1
    assert len(t.stored_texts(first.id)) == 2
    assert t.stored_texts(UUID(newer)) == []


async def test_different_updates_with_the_same_text_are_different_messages(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Раз", "Два")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Привет")
        await t.deliver(MOTHER, "Привет")

    assert t.chat.backend.calls == 2
    assert len(t.all_turns()) == 2


# --- order ---------------------------------------------------------------------------------------------------


async def test_two_messages_sent_at_once_are_answered_in_the_order_they_came(tmp_path):
    backend = ScriptedBackend("Ответ первому", "Ответ второму")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        arrived, release = backend.hold()
        first = asyncio.ensure_future(t.deliver(MOTHER, "Первое"))
        await asyncio.wait_for(arrived.wait(), 10)
        second = asyncio.ensure_future(t.deliver(MOTHER, "Второе"))
        await asyncio.sleep(0.05)  # the second is waiting its turn, not racing the first
        calls_while_waiting = backend.calls
        backend._gate = None
        release.set()
        await asyncio.gather(first, second)

    assert calls_while_waiting == 1
    [conversation] = t.conversations()
    assert t.stored_texts(conversation.id) == [
        ("user", "Первое"),
        ("assistant", "Ответ первому"),
        ("user", "Второе"),
        ("assistant", "Ответ второму"),
    ]
    assert t.bot.texts(MOTHER)[1:] == ["Ответ первому", "Ответ второму"]
    assert STRANGER not in {call["chat_id"] for call in t.bot.sent}


async def test_a_mixed_conversation_keeps_one_order_everywhere(tmp_path):
    answers = [f"Ответ {n}" for n in range(1, 5)]
    async with telegram_app(tmp_path, ScriptedBackend(*answers)) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        t.chat.clock.advance(seconds=1)
        await t.chat.send(conversation_id, "Сайт 1", "w1")
        await t.settle()
        t.chat.clock.advance(seconds=1)
        await t.deliver(MOTHER, "Телеграм 2")
        t.chat.clock.advance(seconds=1)
        await t.chat.send(conversation_id, "Сайт 3", "w3")
        await t.settle()
        t.chat.clock.advance(seconds=1)
        await t.deliver(MOTHER, "Телеграм 4")
        history = (await t.chat.history(conversation_id)).json()["messages"]

    expected = [
        ("user", "Сайт 1"), ("assistant", "Ответ 1"),
        ("user", "Телеграм 2"), ("assistant", "Ответ 2"),
        ("user", "Сайт 3"), ("assistant", "Ответ 3"),
        ("user", "Телеграм 4"), ("assistant", "Ответ 4"),
    ]
    assert roles_and_texts(history) == expected
    assert [m["id"] for m in history] == sorted(m["id"] for m in history)
    assert [(m["role"], m["content"]) for m in t.chat.backend.requests[3][0]] == expected[:-1]
    assert len(t.conversations()) == 1
    assert [turn.idempotency_key[:3] for turn in t.all_turns()].count("tg-") == 2


# --- two people --------------------------------------------------------------------------------------------------


async def test_a_conversation_made_in_telegram_belongs_to_that_person_alone(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Секретный ответ")) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Секретный вопрос")
        [conversation] = t.conversations()

        seen_by_other = await other.get("/api/conversations")
        read_by_other = await other.get(f"/api/conversations/{conversation.id}/messages")
        wrote_by_other = await other.post(
            f"/api/conversations/{conversation.id}/messages",
            json={"text": "я чужой"},
            headers={"Origin": t.chat.base.origin, "Idempotency-Key": "x-1"},
        )

    assert seen_by_other.json()["conversations"] == []
    assert read_by_other.status_code == 404 and wrote_by_other.status_code == 404
    assert "Секретн" not in seen_by_other.text + read_by_other.text + wrote_by_other.text


async def test_a_second_person_cannot_use_the_first_persons_account_by_pairing_it_again(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Приватное")
        await t.pair(MOTHER, client=other)  # refused: the account is taken
        listing = (await other.get("/api/conversations")).json()["conversations"]

    assert listing == []
    assert [link.user_id for link in t.links()] == [t.user_id]


# --- retention and persistence -------------------------------------------------------------------------------------


async def test_a_telegram_message_is_kept_and_forgotten_by_the_same_rule_as_any_other(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Привет")
        [conversation] = t.conversations()
        messages = t.chat.messages(str(conversation.id))
        recent = (await t.chat.history(str(conversation.id))).json()["messages"]
        t.chat.clock.advance(days=366)
        await t.chat.sign_in_again()  # (the session lasts 14 days)
        expired = (await t.chat.history(str(conversation.id))).json()["messages"]

    assert all(m.expires_at - m.created_at == RETENTION for m in messages)
    assert [turn.expires_at - turn.created_at for turn in t.all_turns()] == [RETENTION]
    assert len(recent) == 2 and expired == []


async def test_a_pairing_outlives_the_session_that_asked_for_it(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        t.chat.clock.advance(days=30)  # the website session (14 days) is long over
        await t.deliver(MOTHER, "Я всё ещё здесь", tick=False)

    assert t.bot.texts(MOTHER)[-1] == "Ответ"


# --- the polling loop --------------------------------------------------------------------------------------------------


async def test_the_polling_loop_finds_a_message_answers_it_and_confirms_it(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Из очереди")) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        t.bot.push(private_message(5000, MOTHER, "Через опрос"))
        texts = await t.bot.wait_for_messages(before + 1, MOTHER)
        await t.settle()
        await asyncio.sleep(0.1)  # a few more polls, which must carry the confirmation
        offsets = list(t.bot.offsets)

    assert texts[-1] == "Из очереди"
    assert 5001 in offsets  # confirmed: the next poll asked for what comes after it
    assert t.chat.backend.calls == 1


async def test_a_conflict_with_another_poller_is_waited_out_and_polling_goes_on(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with telegram_app(tmp_path, ScriptedBackend("После конфликта")) as t:
        slept = []

        async def quick_sleep(seconds):
            slept.append(seconds)
            await asyncio.sleep(0)

        t.service._sleep = quick_sleep
        await t.pair(MOTHER)
        t.bot.fail("getUpdates", 409)
        t.bot.push(private_message(6000, MOTHER, "Когда-нибудь"))
        await t.bot.wait_for_messages(2, MOTHER)

    assert slept and slept[0] == 30.0
    assert "category=api:409" in log_text(caplog)
    assert t.bot.texts(MOTHER)[-1] == "После конфликта"


async def test_a_refused_token_stops_the_bot_and_leaves_the_website_working(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with telegram_app(tmp_path) as t:
        t.bot.fail("getUpdates", 401)
        for _ in range(500):
            if t.service._poller is not None and t.service._poller.done():
                break
            await asyncio.sleep(0.01)
        poller_done = t.service._poller.done()
        website = await t.chat.client.get("/api/telegram")
        health = await t.chat.client.get("/healthz")

    assert poller_done
    assert "token was refused" in log_text(caplog)
    assert website.status_code == 200 and health.status_code == 200


async def test_stopping_the_application_stops_the_bot_cleanly(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Привет")
        service = t.service
        poller = service._poller

    assert poller.done() and service._tasks == set()
