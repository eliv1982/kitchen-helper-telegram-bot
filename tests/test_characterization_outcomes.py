"""Characterization of what the user is told, and how failures map to it.

These tests pin the CURRENT observable behavior so a later restructuring can
be proven behavior-preserving. They describe what the bot does today, not
necessarily what it should do; where today's behavior looks like a quirk the
test says so.
"""

from unittest.mock import call

import httpx
import pytest
from openai import AuthenticationError, BadRequestError, InternalServerError

from tests import expected_texts, seams
from tests.helpers import (
    FakeBot,
    FakeMessage,
    make_connection_error,
    make_generic_api_error,
    make_rate_limit_error,
    make_status_error,
    make_timeout_error,
)


class _FatalSignal(BaseException):
    """Stands in for a non-Exception BaseException (e.g. KeyboardInterrupt)."""


def _failing_ai(make_error):
    async def handler(history, user_id):
        raise make_error()

    return seams.RecordingAi(handler)


# --- fixed texts ------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_message_text_is_exact_and_sent_alone():
    msg = FakeMessage(user_id=1, text="/start")

    await seams.process_start_command(msg)

    assert msg.answer.await_args_list == [call(expected_texts.START)]


@pytest.mark.asyncio
async def test_start_does_not_touch_ai_or_history(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    msg = FakeMessage(user_id=1, text="/start")

    await seams.process_start_command(msg)

    assert not ai.called
    assert seams.history(1) == []


@pytest.mark.asyncio
async def test_reset_confirmation_text_is_exact_for_command_and_phrase():
    command = FakeMessage(user_id=5, text="/reset")
    phrase = FakeMessage(user_id=5, text="очистить контекст")

    await seams.process_reset_command(command)
    await seams.process_text(phrase, FakeBot())

    assert command.answer.await_args_list == [call(expected_texts.RESET_CONFIRMATION)]
    assert phrase.answer.await_args_list == [call(expected_texts.RESET_CONFIRMATION)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        pytest.param(None, expected_texts.TOO_LONG_2000, id="default-limit-2000"),
        pytest.param(10, expected_texts.TOO_LONG_10, id="patched-limit-10"),
    ],
)
async def test_too_long_notice_text_is_exact_and_embeds_the_current_limit(
    monkeypatch, limit, expected
):
    if limit is not None:
        seams.set_max_input_length(monkeypatch, limit)
    msg = FakeMessage(user_id=1, text="а" * (seams.max_input_length() + 1))

    await seams.process_text(msg, FakeBot())

    assert msg.answer.await_args_list == [call(expected)]


# --- AI failure -> user-facing message --------------------------------------

ERROR_MAPPING = [
    pytest.param(make_rate_limit_error, expected_texts.RATE_LIMITED, id="rate-limit"),
    pytest.param(make_timeout_error, expected_texts.CONNECTION_FAILED, id="timeout"),
    pytest.param(make_connection_error, expected_texts.CONNECTION_FAILED, id="connection"),
    pytest.param(
        lambda: seams.InvalidAiResponseError("empty"),
        expected_texts.EMPTY_RESPONSE,
        id="invalid-response",
    ),
    pytest.param(
        lambda: make_status_error(InternalServerError, 500),
        expected_texts.API_ERROR,
        id="server-error-500",
    ),
    pytest.param(
        lambda: make_status_error(AuthenticationError, 401),
        expected_texts.API_ERROR,
        id="authentication-401",
    ),
    pytest.param(
        lambda: make_status_error(BadRequestError, 400),
        expected_texts.API_ERROR,
        id="bad-request-400",
    ),
    pytest.param(make_generic_api_error, expected_texts.API_ERROR, id="generic-api-error"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_error", "expected_text"), ERROR_MAPPING)
async def test_recognized_ai_failure_gets_one_friendly_reply_and_a_clean_rollback(
    monkeypatch, make_error, expected_text
):
    user_id = 11
    prior = seams.seed_messages(user_id, [("user", "раньше"), ("assistant", "ответ")])
    ai = _failing_ai(make_error)
    seams.install_ai(monkeypatch, ai)
    msg = FakeMessage(user_id=user_id, text="что приготовить")

    await seams.process_text(msg, FakeBot())

    assert msg.answer.await_args_list == [call(expected_text)]
    # The pending user message was in the history sent to the AI, and is gone
    # again afterwards; earlier history is untouched.
    assert len(ai.calls) == 1  # no retry at this layer
    assert ai.calls[0].history == prior + [{"role": "user", "content": "что приготовить"}]
    assert seams.history(user_id) == prior
    assert not seams.is_user_busy(user_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_class",
    [ValueError, KeyError, TimeoutError, httpx.ConnectError],
    ids=lambda cls: cls.__name__,
)
async def test_unrecognized_exception_propagates_without_reply_but_rolls_back(
    monkeypatch, error_class
):
    user_id = 12
    prior = seams.seed_messages(user_id, [("user", "раньше"), ("assistant", "ответ")])
    seams.install_ai(monkeypatch, _failing_ai(lambda: error_class("synthetic")))
    msg = FakeMessage(user_id=user_id, text="что приготовить")

    with pytest.raises(error_class):
        await seams.process_text(msg, FakeBot())

    msg.answer.assert_not_awaited()
    assert seams.history(user_id) == prior
    assert not seams.is_user_busy(user_id)


@pytest.mark.asyncio
async def test_non_exception_base_exception_also_rolls_back_and_propagates(monkeypatch):
    user_id = 13
    prior = seams.seed_messages(user_id, [("user", "раньше"), ("assistant", "ответ")])
    seams.install_ai(monkeypatch, _failing_ai(lambda: _FatalSignal("synthetic")))
    msg = FakeMessage(user_id=user_id, text="что приготовить")

    with pytest.raises(_FatalSignal):
        await seams.process_text(msg, FakeBot())

    msg.answer.assert_not_awaited()
    assert seams.history(user_id) == prior
    assert not seams.is_user_busy(user_id)


# --- successful reply content -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("  padded reply \n\n", id="surrounding-whitespace"),
        pytest.param("**bold** <b>html</b> & _md_ `code`", id="markup-characters"),
        pytest.param("я" * 5000, id="longer-than-telegram-message-limit"),
    ],
)
async def test_reply_is_delivered_and_stored_verbatim_as_a_single_message(monkeypatch, reply):
    # No stripping, escaping, formatting or splitting happens in this layer.
    # (Note: a reply over Telegram's 4096-character limit is therefore sent
    # as one oversized message.)
    async def fake_ai(history, user_id):
        return reply

    seams.install_ai(monkeypatch, fake_ai)
    msg = FakeMessage(user_id=21, text="привет")

    await seams.process_text(msg, FakeBot())

    assert msg.answer.await_args_list == [call(reply)]
    assert seams.history(21) == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": reply},
    ]
