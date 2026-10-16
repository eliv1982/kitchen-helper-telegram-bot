import pytest

from tests import seams
from tests.helpers import FakeBot, FakeMessage


@pytest.mark.asyncio
async def test_oversized_input_is_rejected_before_openai_call(monkeypatch):
    seams.set_max_input_length(monkeypatch, 10)

    called = False

    async def fake_ai(messages, user_id):
        nonlocal called
        called = True
        return "should not be reached"

    seams.install_ai(monkeypatch, fake_ai)

    fake_bot = FakeBot()
    msg = FakeMessage(user_id=1, text="это сообщение длиннее лимита")

    await seams.process_text(msg, fake_bot)

    assert called is False
    assert seams.history(1) == []
    msg.answer.assert_awaited_once()
    (answer_text,), _ = msg.answer.call_args
    assert "10" in answer_text


@pytest.mark.asyncio
async def test_normal_size_input_still_works(monkeypatch):
    seams.set_max_input_length(monkeypatch, 10)

    async def fake_ai(messages, user_id):
        return "ok-reply"

    seams.install_ai(monkeypatch, fake_ai)

    fake_bot = FakeBot()
    msg = FakeMessage(user_id=2, text="короткий")  # under the 10-char limit

    await seams.process_text(msg, fake_bot)

    assert seams.history(2) == [
        {"role": "user", "content": "короткий"},
        {"role": "assistant", "content": "ok-reply"},
    ]
    msg.answer.assert_awaited_with("ok-reply")


@pytest.mark.asyncio
async def test_start_message_discloses_openai_processing():
    msg = FakeMessage(user_id=3, text="/start")

    await seams.process_start_command(msg)

    msg.answer.assert_awaited_once()
    (start_text,), _ = msg.answer.call_args
    assert "OpenAI" in start_text


@pytest.mark.asyncio
async def test_input_at_exact_max_length_is_accepted(monkeypatch):
    # Uses the real MAX_INPUT_LENGTH (2000) rather than a monkeypatched
    # value, to pin the actual production boundary.
    async def fake_ai(messages, user_id):
        return "ok-reply"

    seams.install_ai(monkeypatch, fake_ai)

    fake_bot = FakeBot()
    text = "а" * seams.max_input_length()
    msg = FakeMessage(user_id=5, text=text)

    await seams.process_text(msg, fake_bot)

    assert seams.history(5) == [
        {"role": "user", "content": text},
        {"role": "assistant", "content": "ok-reply"},
    ]
    msg.answer.assert_awaited_with("ok-reply")


@pytest.mark.asyncio
async def test_oversized_input_preserves_existing_non_empty_history(monkeypatch):
    async def fake_ai(messages, user_id):
        return "first-reply"

    seams.install_ai(monkeypatch, fake_ai)

    fake_bot = FakeBot()
    user_id = 6
    await seams.process_text(FakeMessage(user_id=user_id, text="короткий"), fake_bot)
    existing = seams.history(user_id)
    assert existing != []

    called = False

    async def fake_should_not_be_called(messages, user_id):
        nonlocal called
        called = True
        return "should not be reached"

    seams.install_ai(monkeypatch, fake_should_not_be_called)

    oversized_text = "б" * (seams.max_input_length() + 1)
    oversized_msg = FakeMessage(user_id=user_id, text=oversized_text)
    await seams.process_text(oversized_msg, fake_bot)

    assert called is False
    assert seams.history(user_id) == existing
