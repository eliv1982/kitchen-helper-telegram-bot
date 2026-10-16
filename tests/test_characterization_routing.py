"""Characterization of update routing and outgoing Telegram requests.

Unlike the other characterization tests these drive real aiogram `Update`
objects through the real Dispatcher, so the handler filters and the shape of
the requests sent back to Telegram are exercised for real. Only the HTTP
layer is replaced (see seams.WireTelegram), so nothing leaves the process.

These tests pin the CURRENT observable behavior; where it looks like a quirk
the test name and comments say so.
"""

import pytest
import pytest_asyncio

from tests import expected_texts, seams


@pytest_asyncio.fixture
async def wire():
    harness = seams.WireTelegram()
    yield harness
    await harness.close()


@pytest.mark.asyncio
async def test_start_command_gets_the_start_text_in_the_originating_chat(wire):
    await wire.send("/start", user_id=7, chat_id=-100500)

    assert wire.sent_messages == [(-100500, expected_texts.START)]


@pytest.mark.asyncio
async def test_start_command_with_a_payload_still_gets_the_start_text(wire):
    await wire.send("/start some-deep-link-payload", user_id=7, chat_id=7)

    assert wire.sent_messages == [(7, expected_texts.START)]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/reset", "/reset now please"])
async def test_reset_command_routes_to_reset_not_to_the_ai(monkeypatch, wire, text):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    seams.seed_exchanges(7, pairs=2)

    await wire.send(text, user_id=7, chat_id=-100500)

    assert wire.sent_messages == [(-100500, expected_texts.RESET_CONFIRMATION)]
    assert seams.history(7) == []
    assert not ai.called


@pytest.mark.asyncio
async def test_plain_text_goes_to_the_ai_and_the_reply_to_the_originating_chat(monkeypatch, wire):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)

    await wire.send("что приготовить?", user_id=7, chat_id=-100500)

    assert ai.calls[0].history == [{"role": "user", "content": "что приготовить?"}]
    assert ai.calls[0].user_id == 7
    assert wire.sent_messages == [(-100500, "ok-reply")]


@pytest.mark.asyncio
async def test_commands_addressed_to_this_bot_by_name_are_recognized(monkeypatch, wire):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)

    await wire.send(f"/start@{wire.BOT_USERNAME}", user_id=7)
    await wire.send(f"/reset@{wire.BOT_USERNAME}", user_id=7)

    assert wire.sent_messages == [
        (7, expected_texts.START),
        (7, expected_texts.RESET_CONFIRMATION),
    ]
    assert not ai.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["/help", "/menu", "/start@someotherbot", "/reset@someotherbot"]
)
async def test_other_slash_commands_are_forwarded_to_the_ai_as_plain_text(
    monkeypatch, wire, text
):
    # Quirk: only /start and /reset (for this bot) are recognized commands;
    # any other text, including unknown commands and commands addressed to a
    # different bot, is treated as an ordinary user message.
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)

    await wire.send(text, user_id=7)

    assert ai.calls[0].history == [{"role": "user", "content": text}]
    assert wire.sent_messages == [(7, "ok-reply")]


@pytest.mark.asyncio
async def test_updates_without_text_are_ignored(monkeypatch, wire):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)

    await wire.send(None, user_id=7)

    assert wire.requests == []
    assert not ai.called


@pytest.mark.asyncio
async def test_edited_messages_are_ignored(monkeypatch, wire):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)

    await wire.send_edited("исправленный текст", user_id=7)

    assert wire.requests == []
    assert not ai.called
