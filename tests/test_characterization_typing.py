"""Characterization of the "typing…" indicator.

These tests pin the CURRENT observable behavior so a later restructuring can
be proven behavior-preserving. The indicator is aiogram's ChatActionSender,
which sends the action from a background task; a request that finishes
before that task gets to run may legitimately send nothing, so every test
that expects an indicator holds the AI call open until one was observed.
"""

import asyncio
from unittest.mock import call

import pytest

from aiogram.exceptions import TelegramNetworkError
from aiogram.utils.chat_action import ChatActionSender

from tests import seams
from tests.helpers import FakeBot, FakeMessage, make_rate_limit_error, telegram_method


def _observe_typing(bot: FakeBot) -> asyncio.Event:
    """Return an event that is set once the bot has been asked to show typing."""
    seen = asyncio.Event()

    async def _record(*args, **kwargs):
        seen.set()

    bot.send_chat_action.side_effect = _record
    return seen


def _background_tasks() -> set:
    """Tasks still running other than the one executing the calling test."""
    return asyncio.all_tasks() - {asyncio.current_task()}


# What the indicator's background task is running; taken from the class so a
# rename in aiogram fails loudly here instead of silently matching nothing.
_TYPING_WORKER = ChatActionSender._worker.__qualname__


def _typing_workers() -> list[asyncio.Task]:
    """The indicator's own background tasks, and nothing else.

    Counting every live task is wrong on Python < 3.12: asyncio.wait_for runs
    its awaitable as a task of its own there, so while the worker waits out an
    interval it owns a second, unrelated task.
    """
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_coro().__qualname__ == _TYPING_WORKER
    ]


@pytest.mark.asyncio
async def test_typing_is_sent_to_the_originating_chat_while_the_ai_is_pending(monkeypatch):
    fake_bot = FakeBot()
    typing_seen = _observe_typing(fake_bot)
    during_ai: dict = {}

    async def ai(history, user_id):
        await asyncio.wait_for(typing_seen.wait(), timeout=2)
        during_ai["calls"] = list(fake_bot.send_chat_action.await_args_list)
        during_ai["workers"] = len(_typing_workers())
        return "ok-reply"

    seams.install_ai(monkeypatch, ai)
    group_chat = -1001234567890
    msg = FakeMessage(user_id=5, text="привет", chat_id=group_chat)

    await seams.process_text(msg, fake_bot)

    # Targets the chat (not the sender); no forum-topic thread is propagated.
    assert during_ai["calls"] == [
        call(chat_id=group_chat, action="typing", message_thread_id=None)
    ]
    assert during_ai["workers"] == 1  # exactly one indicator, running while the AI is pending
    assert not _typing_workers()  # and it is gone once the exchange is over


@pytest.mark.asyncio
async def test_typing_stops_when_the_exchange_succeeds(monkeypatch):
    fake_bot = FakeBot()
    typing_seen = _observe_typing(fake_bot)

    async def ai(history, user_id):
        await asyncio.wait_for(typing_seen.wait(), timeout=2)
        return "ok-reply"

    seams.install_ai(monkeypatch, ai)

    await seams.process_text(FakeMessage(user_id=5, text="привет"), fake_bot)

    sent_before = len(fake_bot.send_chat_action.await_args_list)
    await seams.settle()
    assert len(fake_bot.send_chat_action.await_args_list) == sent_before
    assert not _background_tasks()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_error",
    [make_rate_limit_error, lambda: RuntimeError("synthetic")],
    ids=["recognized-failure", "unexpected-failure"],
)
async def test_typing_stops_when_the_exchange_fails(monkeypatch, make_error):
    fake_bot = FakeBot()
    typing_seen = _observe_typing(fake_bot)

    async def ai(history, user_id):
        await asyncio.wait_for(typing_seen.wait(), timeout=2)
        raise make_error()

    seams.install_ai(monkeypatch, ai)
    msg = FakeMessage(user_id=5, text="привет")

    try:
        await seams.process_text(msg, fake_bot)
    except RuntimeError:
        pass  # the unexpected failure propagates; only typing is of interest here

    sent_before = len(fake_bot.send_chat_action.await_args_list)
    await seams.settle()
    assert len(fake_bot.send_chat_action.await_args_list) == sent_before
    assert not _background_tasks()


@pytest.mark.asyncio
async def test_typing_has_ended_by_the_time_the_reply_is_delivered(monkeypatch):
    fake_bot = FakeBot()
    typing_seen = _observe_typing(fake_bot)

    async def ai(history, user_id):
        await asyncio.wait_for(typing_seen.wait(), timeout=2)
        return "ok-reply"

    seams.install_ai(monkeypatch, ai)
    msg = FakeMessage(user_id=5, text="привет")
    tasks_at_delivery: list[int] = []

    async def record_delivery_state(text, **kwargs):
        tasks_at_delivery.append(len(_background_tasks()))

    msg.answer.side_effect = record_delivery_state

    await seams.process_text(msg, fake_bot)

    assert tasks_at_delivery == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("очистить контекст", id="reset-phrase"),
        pytest.param("а" * 2001, id="oversized"),
    ],
)
async def test_typing_is_not_started_for_requests_that_never_reach_the_ai(monkeypatch, text):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    fake_bot = FakeBot()

    await seams.process_text(FakeMessage(user_id=5, text=text), fake_bot)
    await seams.settle()

    fake_bot.send_chat_action.assert_not_awaited()
    assert not _background_tasks()
    assert not ai.called


@pytest.mark.asyncio
async def test_typing_indicator_failure_does_not_fail_the_exchange(monkeypatch):
    fake_bot = FakeBot()
    attempted = asyncio.Event()

    async def failing_send_chat_action(*args, **kwargs):
        attempted.set()
        raise TelegramNetworkError(telegram_method(), "typing failed")

    fake_bot.send_chat_action.side_effect = failing_send_chat_action

    async def ai(history, user_id):
        await asyncio.wait_for(attempted.wait(), timeout=2)
        return "ok-reply"

    seams.install_ai(monkeypatch, ai)
    # The indicator's task dies with an exception nobody retrieves; keep that
    # out of the test output by routing it to a no-op loop handler.
    asyncio.get_running_loop().set_exception_handler(lambda loop, context: None)
    msg = FakeMessage(user_id=5, text="привет")

    await seams.process_text(msg, fake_bot)

    assert msg.answer.await_args_list == [call("ok-reply")]
    assert seams.history(5) == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "ok-reply"},
    ]
    assert not seams.is_user_busy(5)
