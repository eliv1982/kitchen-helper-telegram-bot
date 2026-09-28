import asyncio

import pytest

import api_client
import bot as bot_module
import context_manager
from tests.helpers import FakeBot, FakeMessage, make_rate_limit_error


@pytest.mark.asyncio
async def test_different_users_are_processed_concurrently(monkeypatch):
    entered: list[int] = []
    release_event = asyncio.Event()

    async def fake_get_chat_response(messages, user_id, context_len):
        entered.append(user_id)
        if len(entered) < 2:
            # If per-user serialization were actually a global lock, the
            # second user's call would never reach here and this would
            # time out, failing the test.
            await asyncio.wait_for(release_event.wait(), timeout=2)
        else:
            release_event.set()
        return f"reply-{user_id}", None

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    msg_a = FakeMessage(user_id=1, text="привет")
    msg_b = FakeMessage(user_id=2, text="привет")

    await asyncio.wait_for(
        asyncio.gather(
            bot_module.handle_text(msg_a, fake_bot),
            bot_module.handle_text(msg_b, fake_bot),
        ),
        timeout=3,
    )

    assert sorted(entered) == [1, 2]
    msg_a.answer.assert_awaited_with("reply-1")
    msg_b.answer.assert_awaited_with("reply-2")


@pytest.mark.asyncio
async def test_same_user_messages_are_serialized_and_ordered(monkeypatch):
    async def fake_get_chat_response(messages, user_id, context_len):
        last_text = messages[-1]["content"]
        if last_text == "first":
            # Give the second message every chance to race ahead if the
            # per-user transaction isn't actually serialized.
            await asyncio.sleep(0.05)
        return f"reply-to-{last_text}", None

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    msg1 = FakeMessage(user_id=42, text="first")
    msg2 = FakeMessage(user_id=42, text="second")

    await asyncio.wait_for(
        asyncio.gather(
            bot_module.handle_text(msg1, fake_bot),
            bot_module.handle_text(msg2, fake_bot),
        ),
        timeout=2,
    )

    contents = [entry["content"] for entry in context_manager.get_context(42)]
    assert contents == ["first", "reply-to-first", "second", "reply-to-second"]


@pytest.mark.asyncio
async def test_failed_request_rolls_back_only_its_own_message(monkeypatch):
    async def fake_get_chat_response(messages, user_id, context_len):
        if user_id == 1:
            return "ok-reply", None
        raise make_rate_limit_error()

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    ok_msg = FakeMessage(user_id=1, text="хочу омлет")
    fail_msg = FakeMessage(user_id=2, text="что приготовить")

    await asyncio.gather(
        bot_module.handle_text(ok_msg, fake_bot),
        bot_module.handle_text(fail_msg, fake_bot),
    )

    assert context_manager.get_context(1) == [
        {"role": "user", "content": "хочу омлет"},
        {"role": "assistant", "content": "ok-reply"},
    ]
    assert context_manager.get_context(2) == []
    fail_msg.answer.assert_awaited_with(
        "Сейчас слишком много запросов к AI. Подожди немного и попробуй снова."
    )


@pytest.mark.asyncio
async def test_sequential_same_user_failure_does_not_corrupt_prior_success(monkeypatch):
    responses = iter(["ok-reply", None])

    async def fake_get_chat_response(messages, user_id, context_len):
        outcome = next(responses)
        if outcome is None:
            raise make_rate_limit_error()
        return outcome, None

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    await bot_module.handle_text(FakeMessage(user_id=7, text="первое"), fake_bot)
    await bot_module.handle_text(FakeMessage(user_id=7, text="второе"), fake_bot)

    assert context_manager.get_context(7) == [
        {"role": "user", "content": "первое"},
        {"role": "assistant", "content": "ok-reply"},
    ]


@pytest.mark.asyncio
async def test_empty_response_triggers_friendly_failure_without_crash(monkeypatch):
    async def fake_get_chat_response(messages, user_id, context_len):
        raise api_client.InvalidResponseError("empty response")

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    msg = FakeMessage(user_id=9, text="что приготовить")

    await bot_module.handle_text(msg, fake_bot)

    msg.answer.assert_awaited_with(
        "AI вернул пустой ответ. Попробуй переформулировать запрос или повторить позже."
    )
    assert context_manager.get_context(9) == []


@pytest.mark.asyncio
async def test_reset_command_waits_for_in_flight_exchange(monkeypatch):
    user_id = 101
    entered_openai = asyncio.Event()
    release_exchange = asyncio.Event()

    async def fake_get_chat_response(messages, user_id, context_len):
        entered_openai.set()
        await release_exchange.wait()
        return "reply", None

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    exchange_msg = FakeMessage(user_id=user_id, text="привет")
    reset_msg = FakeMessage(user_id=user_id, text="/reset")

    exchange_task = asyncio.create_task(bot_module.handle_text(exchange_msg, fake_bot))
    await asyncio.wait_for(entered_openai.wait(), timeout=2)

    # The exchange now holds the per-user lock with its user message already
    # added and is blocked inside the mocked OpenAI call.
    reset_task = asyncio.create_task(bot_module.cmd_reset(reset_msg))
    await asyncio.sleep(0)  # let reset_task run up to the point it blocks on the lock

    # Reset must not have been able to clear context underneath the in-flight
    # exchange: the pending user message is still there, and reset hasn't
    # finished (it's queued behind the lock the exchange is holding).
    assert context_manager.get_context(user_id) == [{"role": "user", "content": "привет"}]
    assert not reset_task.done()

    release_exchange.set()
    await asyncio.wait_for(exchange_task, timeout=2)
    await asyncio.wait_for(reset_task, timeout=2)

    # Reset ran only after the exchange fully completed, so it wiped the
    # completed turn too.
    assert context_manager.get_context(user_id) == []
    reset_msg.answer.assert_awaited_with("Контекст очищен. Можем начать с чистого листа!")

    lock = bot_module._get_user_lock(user_id)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_text_reset_phrase_waits_for_in_flight_exchange(monkeypatch):
    user_id = 102
    entered_openai = asyncio.Event()
    release_exchange = asyncio.Event()

    async def fake_get_chat_response(messages, user_id, context_len):
        entered_openai.set()
        await release_exchange.wait()
        return "reply", None

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    exchange_msg = FakeMessage(user_id=user_id, text="привет")
    reset_msg = FakeMessage(user_id=user_id, text="очистить контекст")

    exchange_task = asyncio.create_task(bot_module.handle_text(exchange_msg, fake_bot))
    await asyncio.wait_for(entered_openai.wait(), timeout=2)

    reset_task = asyncio.create_task(bot_module.handle_text(reset_msg, fake_bot))
    await asyncio.sleep(0)  # let reset_task run up to the point it blocks on the lock

    assert context_manager.get_context(user_id) == [{"role": "user", "content": "привет"}]
    assert not reset_task.done()

    release_exchange.set()
    await asyncio.wait_for(exchange_task, timeout=2)
    await asyncio.wait_for(reset_task, timeout=2)

    assert context_manager.get_context(user_id) == []
    reset_msg.answer.assert_awaited_with("Контекст очищен. Можем начать с чистого листа!")

    lock = bot_module._get_user_lock(user_id)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cancellation_during_exchange_rolls_back_pending_message(monkeypatch):
    user_id = 103
    entered_openai = asyncio.Event()
    never_released = asyncio.Event()

    async def fake_get_chat_response(messages, user_id, context_len):
        last_text = messages[-1]["content"]
        if last_text == "второе":
            entered_openai.set()
            await never_released.wait()
        return f"reply-to-{last_text}", None

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()

    # A prior, fully completed turn that must survive untouched.
    await bot_module.handle_text(FakeMessage(user_id=user_id, text="первое"), fake_bot)
    assert context_manager.get_context(user_id) == [
        {"role": "user", "content": "первое"},
        {"role": "assistant", "content": "reply-to-первое"},
    ]

    task = asyncio.create_task(
        bot_module.handle_text(FakeMessage(user_id=user_id, text="второе"), fake_bot)
    )
    await asyncio.wait_for(entered_openai.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The pending "второе" turn is rolled back; the prior completed turn is
    # untouched.
    assert context_manager.get_context(user_id) == [
        {"role": "user", "content": "первое"},
        {"role": "assistant", "content": "reply-to-первое"},
    ]

    # The lock was released normally, so a later same-user operation proceeds.
    lock = bot_module._get_user_lock(user_id)
    assert not lock.locked()

    await bot_module.handle_text(FakeMessage(user_id=user_id, text="третье"), fake_bot)
    assert context_manager.get_context(user_id) == [
        {"role": "user", "content": "первое"},
        {"role": "assistant", "content": "reply-to-первое"},
        {"role": "user", "content": "третье"},
        {"role": "assistant", "content": "reply-to-третье"},
    ]
