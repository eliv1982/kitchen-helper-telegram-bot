"""Characterization of Telegram delivery semantics and same-user ordering.

These tests pin the CURRENT observable behavior so a later restructuring can
be proven behavior-preserving. They describe what the bot does today, not
necessarily what it should do; where today's behavior is an asymmetry or a
quirk the test name and comments say so.

The central fact being pinned: for a text message, the per-user lock is held
from the moment the message is picked up until its reply (or error notice, or
reset confirmation) has been *delivered* to Telegram. A slow Telegram
delivery therefore delays that user's next message, which keeps replies
arriving in the order the messages were sent.
"""

import asyncio
from unittest.mock import call

import pytest
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from tests import expected_texts, seams
from tests.helpers import (
    DelayedDelivery,
    FakeBot,
    FakeMessage,
    make_rate_limit_error,
    record_delivery,
    telegram_method,
)

TELEGRAM_ERRORS = [
    pytest.param(lambda: TelegramNetworkError(telegram_method(), "net"), id="network"),
    pytest.param(lambda: TelegramBadRequest(telegram_method(), "bad"), id="bad-request"),
    pytest.param(lambda: TelegramForbiddenError(telegram_method(), "blocked"), id="forbidden"),
    pytest.param(lambda: TelegramServerError(telegram_method(), "5xx"), id="server"),
    pytest.param(lambda: TelegramRetryAfter(telegram_method(), "flood", 30), id="retry-after"),
    pytest.param(lambda: TelegramAPIError(telegram_method(), "generic"), id="generic-api"),
]


def _ai_replying_to_last_message(log=None):
    async def ai(history, user_id):
        text = history[-1]["content"]
        if log is not None:
            log.append(("ai", text))
        return f"reply-to-{text}"

    return ai


async def _finish(*tasks, timeout=2):
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)


# --- what is delivered, and when relative to the stored history -------------


@pytest.mark.asyncio
async def test_reply_is_delivered_after_history_is_committed(monkeypatch):
    seams.install_ai(monkeypatch, _ai_replying_to_last_message())
    msg = FakeMessage(user_id=1, text="привет")
    seen_at_delivery: list = []

    async def record(text, **kwargs):
        seen_at_delivery.append(seams.history(1))

    msg.answer.side_effect = record

    await seams.process_text(msg, FakeBot())

    # By the time Telegram is asked to deliver, the exchange is already stored.
    assert seen_at_delivery == [
        [
            {"role": "user", "content": "привет"},
            {"role": "assistant", "content": "reply-to-привет"},
        ]
    ]


@pytest.mark.asyncio
async def test_success_trims_history_before_delivery_too(monkeypatch):
    seams.install_ai(monkeypatch, _ai_replying_to_last_message())
    seams.seed_exchanges(2)
    msg = FakeMessage(user_id=2, text="новое")
    length_at_delivery: list[int] = []

    async def record(text, **kwargs):
        length_at_delivery.append(len(seams.history(2)))

    msg.answer.side_effect = record

    await seams.process_text(msg, FakeBot())

    assert length_at_delivery == [seams.history_capacity()]


@pytest.mark.asyncio
@pytest.mark.parametrize("make_error", TELEGRAM_ERRORS)
async def test_failed_reply_delivery_keeps_the_exchange_and_sends_one_notice(
    monkeypatch, make_error
):
    seams.install_ai(monkeypatch, _ai_replying_to_last_message())
    msg = FakeMessage(user_id=3, text="привет")
    msg.answer.side_effect = [make_error(), None]

    await seams.process_text(msg, FakeBot())

    # No retry of the reply, no waiting for retry_after: straight to the notice.
    assert msg.answer.await_args_list == [
        call("reply-to-привет"),
        call(expected_texts.DELIVERY_FAILED),
    ]
    # The exchange stays in history even though the user never saw the reply.
    assert seams.history(3) == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "reply-to-привет"},
    ]
    assert not seams.is_user_busy(3)


@pytest.mark.asyncio
async def test_failed_notice_after_failed_reply_propagates_and_keeps_the_exchange(monkeypatch):
    seams.install_ai(monkeypatch, _ai_replying_to_last_message())
    msg = FakeMessage(user_id=4, text="привет")
    msg.answer.side_effect = [
        TelegramNetworkError(telegram_method(), "reply failed"),
        TelegramNetworkError(telegram_method(), "notice failed"),
    ]

    with pytest.raises(TelegramNetworkError, match="notice failed"):
        await seams.process_text(msg, FakeBot())

    assert len(msg.answer.await_args_list) == 2
    assert seams.history(4) == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "reply-to-привет"},
    ]
    assert not seams.is_user_busy(4)


@pytest.mark.asyncio
async def test_non_telegram_error_while_delivering_the_reply_propagates_without_notice(
    monkeypatch,
):
    seams.install_ai(monkeypatch, _ai_replying_to_last_message())
    msg = FakeMessage(user_id=5, text="привет")
    msg.answer.side_effect = RuntimeError("synthetic")

    with pytest.raises(RuntimeError, match="synthetic"):
        await seams.process_text(msg, FakeBot())

    assert msg.answer.await_args_list == [call("reply-to-привет")]
    assert len(seams.history(5)) == 2
    assert not seams.is_user_busy(5)


@pytest.mark.asyncio
async def test_failed_error_notice_propagates_after_rollback(monkeypatch):
    # Notices other than the reply have no fallback: a Telegram failure while
    # delivering them is not caught anywhere in this layer.
    async def failing_ai(history, user_id):
        raise make_rate_limit_error()

    seams.install_ai(monkeypatch, failing_ai)
    prior = seams.seed_exchanges(6, pairs=1)
    msg = FakeMessage(user_id=6, text="привет")
    msg.answer.side_effect = TelegramNetworkError(telegram_method(), "notice failed")

    with pytest.raises(TelegramNetworkError):
        await seams.process_text(msg, FakeBot())

    assert msg.answer.await_args_list == [call(expected_texts.RATE_LIMITED)]
    assert seams.history(6) == prior
    assert not seams.is_user_busy(6)


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["phrase", "command", "start", "too-long"])
async def test_failed_delivery_of_other_notices_propagates_unhandled(via):
    texts = {
        "phrase": "очистить контекст",
        "command": "/reset",
        "start": "/start",
        "too-long": "а" * 2001,
    }
    msg = FakeMessage(user_id=7, text=texts[via])
    msg.answer.side_effect = TelegramNetworkError(telegram_method(), "notice failed")
    seams.seed_exchanges(7, pairs=1)

    with pytest.raises(TelegramNetworkError):
        if via == "command":
            await seams.process_reset_command(msg)
        elif via == "start":
            await seams.process_start_command(msg)
        else:
            await seams.process_text(msg, FakeBot())

    assert len(msg.answer.await_args_list) == 1  # no fallback attempted
    assert not seams.is_user_busy(7)
    # A reset is performed before its confirmation is attempted; the other
    # notices never touch the history.
    expected_len = 0 if via in ("phrase", "command") else 2
    assert len(seams.history(7)) == expected_len


@pytest.mark.asyncio
async def test_cancellation_while_delivering_keeps_the_committed_exchange(monkeypatch):
    seams.install_ai(monkeypatch, _ai_replying_to_last_message())
    log: list = []
    msg = FakeMessage(user_id=8, text="привет")
    stuck = DelayedDelivery(msg, log)

    task = asyncio.create_task(seams.process_text(msg, FakeBot()))
    await asyncio.wait_for(stuck.started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Cancellation is not a delivery failure: no notice is attempted, and
    # nothing is rolled back because the exchange had already completed.
    assert msg.answer.await_args_list == [call("reply-to-привет")]
    assert seams.history(8) == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "reply-to-привет"},
    ]
    assert not seams.is_user_busy(8)


@pytest.mark.asyncio
async def test_start_message_is_not_serialized_with_an_exchange_in_flight(monkeypatch):
    release = asyncio.Event()
    entered = asyncio.Event()

    async def gated_ai(history, user_id):
        entered.set()
        await release.wait()
        return "ok-reply"

    seams.install_ai(monkeypatch, gated_ai)
    exchange = asyncio.create_task(
        seams.process_text(FakeMessage(user_id=9, text="привет"), FakeBot())
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    start = FakeMessage(user_id=9, text="/start")

    # Must return while the exchange still holds the lock (bounded, so a
    # regression fails fast instead of hanging).
    await asyncio.wait_for(seams.process_start_command(start), timeout=2)

    assert start.answer.await_args_list == [call(expected_texts.START)]
    assert seams.is_user_busy(9)
    assert seams.history(9) == [{"role": "user", "content": "привет"}]

    release.set()
    await _finish(exchange)


# --- same-user ordering when delivery is delayed ----------------------------


@pytest.mark.asyncio
async def test_second_message_waits_for_the_first_replys_delayed_delivery(monkeypatch):
    user_id = 42
    log: list[tuple[str, str]] = []
    seams.install_ai(monkeypatch, _ai_replying_to_last_message(log))
    fake_bot = FakeBot()
    first = FakeMessage(user_id=user_id, text="first")
    second = FakeMessage(user_id=user_id, text="second")
    first_delivery = DelayedDelivery(first, log)
    record_delivery(second, log)

    first_task = asyncio.create_task(seams.process_text(first, fake_bot))
    await asyncio.wait_for(first_delivery.started.wait(), timeout=2)
    second_task = asyncio.create_task(seams.process_text(second, fake_bot))
    await seams.settle()

    # First reply is mid-delivery: the second message has not even reached the
    # AI, and its text is not in the history yet.
    assert log == [("ai", "first"), ("deliver-start", "reply-to-first")]
    assert not second_task.done()
    assert seams.is_user_busy(user_id)
    assert seams.history(user_id) == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply-to-first"},
    ]

    first_delivery.release()
    await _finish(first_task, second_task)

    assert log == [
        ("ai", "first"),
        ("deliver-start", "reply-to-first"),
        ("deliver-done", "reply-to-first"),
        ("ai", "second"),
        ("deliver", "reply-to-second"),
    ]
    assert seams.history(user_id) == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply-to-first"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply-to-second"},
    ]
    assert not seams.is_user_busy(user_id)


@pytest.mark.asyncio
async def test_queued_messages_are_processed_and_delivered_in_arrival_order(monkeypatch):
    user_id = 43
    log: list[tuple[str, str]] = []
    ai = seams.RecordingAi(_ai_replying_to_last_message(log))
    seams.install_ai(monkeypatch, ai)
    fake_bot = FakeBot()
    messages = [FakeMessage(user_id=user_id, text=name) for name in ("one", "two", "three")]
    first_delivery = DelayedDelivery(messages[0], log)
    record_delivery(messages[1], log)
    record_delivery(messages[2], log)

    tasks = []
    for message in messages:
        tasks.append(asyncio.create_task(seams.process_text(message, fake_bot)))
        await seams.settle()  # make the arrival order unambiguous
    await asyncio.wait_for(first_delivery.started.wait(), timeout=2)
    assert [entry for entry in log if entry[0] == "ai"] == [("ai", "one")]

    first_delivery.release()
    await _finish(*tasks)

    assert log == [
        ("ai", "one"),
        ("deliver-start", "reply-to-one"),
        ("deliver-done", "reply-to-one"),
        ("ai", "two"),
        ("deliver", "reply-to-two"),
        ("ai", "three"),
        ("deliver", "reply-to-three"),
    ]
    # The third request saw the first two exchanges, in order.
    assert ai.calls[2].history == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "reply-to-one"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "reply-to-two"},
        {"role": "user", "content": "three"},
    ]


@pytest.mark.asyncio
async def test_other_users_are_not_held_up_by_a_users_delayed_delivery(monkeypatch):
    log: list[tuple[str, str]] = []
    seams.install_ai(monkeypatch, _ai_replying_to_last_message(log))
    fake_bot = FakeBot()
    slow = FakeMessage(user_id=50, text="slow")
    other = FakeMessage(user_id=51, text="other")
    slow_delivery = DelayedDelivery(slow, log)
    record_delivery(other, log)

    slow_task = asyncio.create_task(seams.process_text(slow, fake_bot))
    await asyncio.wait_for(slow_delivery.started.wait(), timeout=2)

    await asyncio.wait_for(seams.process_text(other, fake_bot), timeout=2)

    # The other user's whole exchange completed while the first was stuck.
    assert log == [
        ("ai", "slow"),
        ("deliver-start", "reply-to-slow"),
        ("ai", "other"),
        ("deliver", "reply-to-other"),
    ]
    assert seams.is_user_busy(50)
    assert not seams.is_user_busy(51)

    slow_delivery.release()
    await _finish(slow_task)


@pytest.mark.asyncio
async def test_delayed_delivery_of_an_error_notice_also_holds_the_user_lock(monkeypatch):
    user_id = 44
    log: list[tuple[str, str]] = []
    outcomes = iter(["fail", "ok"])

    async def ai(history, uid):
        text = history[-1]["content"]
        log.append(("ai", text))
        if next(outcomes) == "fail":
            raise make_rate_limit_error()
        return f"reply-to-{text}"

    seams.install_ai(monkeypatch, ai)
    fake_bot = FakeBot()
    first = FakeMessage(user_id=user_id, text="first")
    second = FakeMessage(user_id=user_id, text="second")
    notice_delivery = DelayedDelivery(first, log)
    record_delivery(second, log)

    first_task = asyncio.create_task(seams.process_text(first, fake_bot))
    await asyncio.wait_for(notice_delivery.started.wait(), timeout=2)
    second_task = asyncio.create_task(seams.process_text(second, fake_bot))
    await seams.settle()

    # The failed turn was already rolled back, but the user is still locked
    # until the notice itself has been delivered.
    assert log == [("ai", "first"), ("deliver-start", expected_texts.RATE_LIMITED)]
    assert seams.history(user_id) == []
    assert not second_task.done()

    notice_delivery.release()
    await _finish(first_task, second_task)

    assert log[2:] == [
        ("deliver-done", expected_texts.RATE_LIMITED),
        ("ai", "second"),
        ("deliver", "reply-to-second"),
    ]


@pytest.mark.asyncio
async def test_delayed_delivery_of_the_fallback_notice_also_holds_the_user_lock(monkeypatch):
    user_id = 45
    log: list[tuple[str, str]] = []
    seams.install_ai(monkeypatch, _ai_replying_to_last_message(log))
    fake_bot = FakeBot()
    first = FakeMessage(user_id=user_id, text="first")
    second = FakeMessage(user_id=user_id, text="second")
    record_delivery(second, log)
    fallback_started = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def fail_then_stall(text, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TelegramNetworkError(telegram_method(), "reply failed")
        log.append(("deliver-start", text))
        fallback_started.set()
        await release.wait()
        log.append(("deliver-done", text))

    first.answer.side_effect = fail_then_stall

    first_task = asyncio.create_task(seams.process_text(first, fake_bot))
    await asyncio.wait_for(fallback_started.wait(), timeout=2)
    second_task = asyncio.create_task(seams.process_text(second, fake_bot))
    await seams.settle()

    assert log == [("ai", "first"), ("deliver-start", expected_texts.DELIVERY_FAILED)]
    assert not second_task.done()

    release.set()
    await _finish(first_task, second_task)

    assert log[2:] == [
        ("deliver-done", expected_texts.DELIVERY_FAILED),
        ("ai", "second"),
        ("deliver", "reply-to-second"),
    ]


@pytest.mark.asyncio
async def test_delayed_reset_phrase_confirmation_holds_the_user_lock(monkeypatch):
    user_id = 46
    log: list[tuple[str, str]] = []
    seams.install_ai(monkeypatch, _ai_replying_to_last_message(log))
    seams.seed_exchanges(user_id, pairs=2)
    fake_bot = FakeBot()
    reset = FakeMessage(user_id=user_id, text="очистить контекст")
    after = FakeMessage(user_id=user_id, text="после")
    confirmation = DelayedDelivery(reset, log)
    record_delivery(after, log)

    reset_task = asyncio.create_task(seams.process_text(reset, fake_bot))
    await asyncio.wait_for(confirmation.started.wait(), timeout=2)
    after_task = asyncio.create_task(seams.process_text(after, fake_bot))
    await seams.settle()

    assert log == [("deliver-start", expected_texts.RESET_CONFIRMATION)]
    assert seams.is_user_busy(user_id)
    assert not after_task.done()

    confirmation.release()
    await _finish(reset_task, after_task)

    assert log[1:] == [
        ("deliver-done", expected_texts.RESET_CONFIRMATION),
        ("ai", "после"),
        ("deliver", "reply-to-после"),
    ]


@pytest.mark.asyncio
async def test_reset_command_confirmation_is_delivered_outside_the_user_lock():
    # Asymmetry: the /reset *command* releases the lock before replying, while
    # the "очистить контекст" *phrase* replies while still holding it.
    busy_while_delivering: dict[str, bool] = {}
    command = FakeMessage(user_id=47, text="/reset")
    phrase = FakeMessage(user_id=47, text="очистить контекст")

    async def record_command(text, **kwargs):
        busy_while_delivering["command"] = seams.is_user_busy(47)

    async def record_phrase(text, **kwargs):
        busy_while_delivering["phrase"] = seams.is_user_busy(47)

    command.answer.side_effect = record_command
    phrase.answer.side_effect = record_phrase

    await seams.process_reset_command(command)
    await seams.process_text(phrase, FakeBot())

    assert busy_while_delivering == {"command": False, "phrase": True}


@pytest.mark.asyncio
async def test_next_message_can_overtake_a_delayed_reset_command_confirmation(monkeypatch):
    # Consequence of the asymmetry above: because /reset does not hold the
    # lock while its confirmation is delivered, a following message completes
    # (and is delivered) before a slow confirmation reaches Telegram.
    user_id = 48
    log: list[tuple[str, str]] = []
    seams.install_ai(monkeypatch, _ai_replying_to_last_message(log))
    fake_bot = FakeBot()
    reset = FakeMessage(user_id=user_id, text="/reset")
    after = FakeMessage(user_id=user_id, text="после")
    confirmation = DelayedDelivery(reset, log)
    record_delivery(after, log)

    reset_task = asyncio.create_task(seams.process_reset_command(reset))
    await asyncio.wait_for(confirmation.started.wait(), timeout=2)

    await asyncio.wait_for(seams.process_text(after, fake_bot), timeout=2)

    assert log == [
        ("deliver-start", expected_texts.RESET_CONFIRMATION),
        ("ai", "после"),
        ("deliver", "reply-to-после"),
    ]

    confirmation.release()
    await _finish(reset_task)
    assert log[-1] == ("deliver-done", expected_texts.RESET_CONFIRMATION)
