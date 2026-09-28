import pytest

import api_client
import bot as bot_module
import context_manager
from tests.helpers import FakeBot, FakeMessage


@pytest.mark.asyncio
async def test_oversized_input_is_rejected_before_openai_call(monkeypatch):
    monkeypatch.setattr(bot_module, "MAX_INPUT_LENGTH", 10)

    called = False

    async def fake_get_chat_response(messages, user_id, context_len):
        nonlocal called
        called = True
        return "should not be reached", None

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    msg = FakeMessage(user_id=1, text="это сообщение длиннее лимита")

    await bot_module.handle_text(msg, fake_bot)

    assert called is False
    assert context_manager.get_context(1) == []
    msg.answer.assert_awaited_once()
    (answer_text,), _ = msg.answer.call_args
    assert "10" in answer_text


@pytest.mark.asyncio
async def test_normal_size_input_still_works(monkeypatch):
    monkeypatch.setattr(bot_module, "MAX_INPUT_LENGTH", 10)

    async def fake_get_chat_response(messages, user_id, context_len):
        return "ok-reply", None

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    msg = FakeMessage(user_id=2, text="короткий")  # under the 10-char limit

    await bot_module.handle_text(msg, fake_bot)

    assert context_manager.get_context(2) == [
        {"role": "user", "content": "короткий"},
        {"role": "assistant", "content": "ok-reply"},
    ]
    msg.answer.assert_awaited_with("ok-reply")


@pytest.mark.asyncio
async def test_start_message_discloses_openai_processing():
    msg = FakeMessage(user_id=3, text="/start")

    await bot_module.cmd_start(msg)

    msg.answer.assert_awaited_once()
    (start_text,), _ = msg.answer.call_args
    assert "OpenAI" in start_text


@pytest.mark.asyncio
async def test_input_at_exact_max_length_is_accepted(monkeypatch):
    # Uses the real MAX_INPUT_LENGTH (2000) rather than a monkeypatched
    # value, to pin the actual production boundary.
    async def fake_get_chat_response(messages, user_id, context_len):
        return "ok-reply", None

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    text = "а" * bot_module.MAX_INPUT_LENGTH
    msg = FakeMessage(user_id=5, text=text)

    await bot_module.handle_text(msg, fake_bot)

    assert context_manager.get_context(5) == [
        {"role": "user", "content": text},
        {"role": "assistant", "content": "ok-reply"},
    ]
    msg.answer.assert_awaited_with("ok-reply")


@pytest.mark.asyncio
async def test_oversized_input_preserves_existing_non_empty_history(monkeypatch):
    async def fake_get_chat_response(messages, user_id, context_len):
        return "first-reply", None

    monkeypatch.setattr(api_client, "get_chat_response", fake_get_chat_response)

    fake_bot = FakeBot()
    user_id = 6
    await bot_module.handle_text(FakeMessage(user_id=user_id, text="короткий"), fake_bot)
    existing = list(context_manager.get_context(user_id))
    assert existing != []

    called = False

    async def fake_should_not_be_called(messages, user_id, context_len):
        nonlocal called
        called = True
        return "should not be reached", None

    monkeypatch.setattr(api_client, "get_chat_response", fake_should_not_be_called)

    oversized_text = "б" * (bot_module.MAX_INPUT_LENGTH + 1)
    oversized_msg = FakeMessage(user_id=user_id, text=oversized_text)
    await bot_module.handle_text(oversized_msg, fake_bot)

    assert called is False
    assert context_manager.get_context(user_id) == existing
