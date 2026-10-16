"""ChatService and Turn, exercised on their own.

No Telegram, no aiogram, no network: a FakeBackend answers, and the
conversations live on a "web" channel to show the service is not tied to the
transport the bot happens to use. The bot's own behavior on top of the service
is pinned by the characterization suite.
"""

import asyncio

import pytest

import context_manager
from house_helper.core.backend import (
    BackendConnectionError,
    BackendEmptyResponseError,
    BackendError,
    BackendRateLimitedError,
    BackendReply,
)
from house_helper.core.chat_service import ChatService
from house_helper.core.identity import ConversationKey
from house_helper.core.outcomes import ChatOutcome, OutcomeStatus
from tests import seams
from tests.helpers import FakeBackend

WEB = ConversationKey("web", "visitor-1")
OTHER_WEB = ConversationKey("web", "visitor-2")


class HookBackend:
    """Backend whose answer is produced by `handler(messages, conversation)`,
    so a test can hold a request open or make it fail on its own terms."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[tuple[list[dict[str, str]], ConversationKey]] = []

    async def complete(self, messages, *, conversation, context_len) -> BackendReply:
        self.calls.append(([dict(turn) for turn in messages], conversation))
        return BackendReply(text=await self.handler(messages, conversation))


class Working:
    """A `while_working` factory that records when it is entered and left."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.created = 0

    def __call__(self) -> "Working":
        self.created += 1
        return self

    async def __aenter__(self) -> "Working":
        self.log.append("working-start")
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.log.append("working-stop")


async def _done(*tasks: asyncio.Task) -> None:
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)


# --- a normal exchange --------------------------------------------------------


@pytest.mark.asyncio
async def test_send_returns_the_reply_and_commits_the_exchange():
    service = ChatService(FakeBackend(reply="готово"))

    async with service.turn(WEB) as turn:
        outcome = await turn.send("что приготовить")

    assert outcome == ChatOutcome(OutcomeStatus.REPLIED, reply="готово")
    assert seams.history_of(WEB) == [
        {"role": "user", "content": "что приготовить"},
        {"role": "assistant", "content": "готово"},
    ]


@pytest.mark.asyncio
async def test_the_backend_is_asked_with_the_conversation_key_and_the_pending_history():
    backend = FakeBackend()
    seams.seed_conversation(WEB, [("user", "раньше"), ("assistant", "ответ")])
    service = ChatService(backend)

    async with service.turn(WEB) as turn:
        await turn.send("теперь")

    [(messages, conversation, context_len)] = backend.requests
    assert messages == [
        {"role": "user", "content": "раньше"},
        {"role": "assistant", "content": "ответ"},
        {"role": "user", "content": "теперь"},
    ]
    assert conversation == WEB
    assert isinstance(conversation, ConversationKey)
    assert context_len == 3  # the pending message is counted


@pytest.mark.asyncio
async def test_the_exchange_is_committed_before_the_turn_ends():
    # What a transport relies on to deliver safely: by the time send() returns
    # the exchange is already in the history, so whatever happens while the
    # answer is being delivered cannot lose it.
    service = ChatService(FakeBackend(reply="ответ"))

    async with service.turn(WEB) as turn:
        await turn.send("вопрос")

        assert seams.history_of(WEB) == [
            {"role": "user", "content": "вопрос"},
            {"role": "assistant", "content": "ответ"},
        ]


@pytest.mark.asyncio
async def test_a_failed_delivery_does_not_undo_a_successful_exchange():
    service = ChatService(FakeBackend(reply="ответ"))

    with pytest.raises(ConnectionError, match="delivery failed"):
        async with service.turn(WEB) as turn:
            await turn.send("вопрос")
            raise ConnectionError("delivery failed")  # the transport's problem

    assert seams.history_of(WEB) == [
        {"role": "user", "content": "вопрос"},
        {"role": "assistant", "content": "ответ"},
    ]
    assert not seams.lock_for(WEB).locked()


@pytest.mark.asyncio
async def test_the_text_is_used_as_given():
    backend = FakeBackend()
    service = ChatService(backend)

    async with service.turn(WEB) as turn:
        await turn.send("  с пробелами  ")

    assert backend.requests[0][0] == [{"role": "user", "content": "  с пробелами  "}]


@pytest.mark.asyncio
async def test_success_at_capacity_trims_the_oldest_pair_after_the_reply():
    original = seams.seed_conversation(
        WEB,
        [
            (role, f"{role}-{i}")
            for i in range(1, context_manager.MAX_HISTORY_MESSAGES // 2 + 1)
            for role in ("user", "assistant")
        ],
    )
    assert len(original) == context_manager.MAX_HISTORY_MESSAGES
    service = ChatService(FakeBackend(reply="новый"))

    async with service.turn(WEB) as turn:
        await turn.send("свежий")

    assert seams.history_of(WEB) == original[2:] + [
        {"role": "user", "content": "свежий"},
        {"role": "assistant", "content": "новый"},
    ]


# --- the input-length policy ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_default_limit_is_2000_characters():
    backend = FakeBackend()
    service = ChatService(backend)

    async with service.turn(WEB) as turn:
        at_limit = await turn.send("а" * 2000)
        over_limit = await turn.send("а" * 2001)

    assert at_limit.status is OutcomeStatus.REPLIED
    assert over_limit == ChatOutcome(OutcomeStatus.INPUT_TOO_LONG, max_input_length=2000)


@pytest.mark.asyncio
async def test_the_limit_is_configurable():
    service = ChatService(FakeBackend(), max_input_length=10)

    async with service.turn(WEB) as turn:
        outcome = await turn.send("а" * 11)

    assert outcome == ChatOutcome(OutcomeStatus.INPUT_TOO_LONG, max_input_length=10)


@pytest.mark.asyncio
async def test_an_oversized_message_never_reaches_the_backend_or_the_history():
    backend = FakeBackend()
    prior = seams.seed_conversation(WEB, [("user", "раньше"), ("assistant", "ответ")])
    working = Working([])
    service = ChatService(backend, max_input_length=5)

    async with service.turn(WEB) as turn:
        await turn.send("слишком длинно", while_working=working)

    assert backend.requests == []
    assert working.created == 0  # no typing indicator for a rejected message
    assert seams.history_of(WEB) == prior


# --- failures: outcome, rollback, propagation ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status"),
    [
        pytest.param(BackendRateLimitedError("x"), OutcomeStatus.RATE_LIMITED, id="rate-limited"),
        pytest.param(BackendConnectionError("x"), OutcomeStatus.CONNECTION_FAILED, id="connection"),
        pytest.param(BackendEmptyResponseError("x"), OutcomeStatus.EMPTY_RESPONSE, id="empty"),
        pytest.param(BackendError("x"), OutcomeStatus.API_ERROR, id="generic"),
    ],
)
async def test_a_backend_failure_becomes_its_outcome_and_rolls_back(error, status):
    prior = seams.seed_conversation(WEB, [("user", "раньше"), ("assistant", "ответ")])
    service = ChatService(FakeBackend(error=error))

    async with service.turn(WEB) as turn:
        outcome = await turn.send("вопрос")

    assert outcome == ChatOutcome(status)
    assert seams.history_of(WEB) == prior


@pytest.mark.asyncio
async def test_an_unexpected_exception_rolls_back_and_propagates_unchanged():
    prior = seams.seed_conversation(WEB, [("user", "раньше"), ("assistant", "ответ")])
    boom = RuntimeError("synthetic unexpected failure")
    service = ChatService(FakeBackend(error=boom))

    with pytest.raises(RuntimeError) as excinfo:
        async with service.turn(WEB) as turn:
            await turn.send("вопрос")

    assert excinfo.value is boom
    assert seams.history_of(WEB) == prior
    assert not seams.lock_for(WEB).locked()


@pytest.mark.asyncio
async def test_a_failure_at_capacity_loses_no_completed_history():
    original = seams.seed_conversation(
        WEB,
        [
            (role, f"{role}-{i}")
            for i in range(1, context_manager.MAX_HISTORY_MESSAGES // 2 + 1)
            for role in ("user", "assistant")
        ],
    )
    service = ChatService(FakeBackend(error=BackendRateLimitedError("x")))

    async with service.turn(WEB) as turn:
        await turn.send("вопрос")

    assert seams.history_of(WEB) == original


@pytest.mark.asyncio
async def test_cancellation_rolls_back_propagates_and_releases_the_lock():
    prior = seams.seed_conversation(WEB, [("user", "раньше"), ("assistant", "ответ")])
    in_backend = asyncio.Event()

    async def hang(messages, conversation):
        in_backend.set()
        await asyncio.Event().wait()

    service = ChatService(HookBackend(hang))

    async def one_turn():
        async with service.turn(WEB) as turn:
            return await turn.send("вопрос")

    task = asyncio.create_task(one_turn())
    await asyncio.wait_for(in_backend.wait(), timeout=2)
    assert seams.history_of(WEB) == prior + [{"role": "user", "content": "вопрос"}]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seams.history_of(WEB) == prior
    assert not seams.lock_for(WEB).locked()


@pytest.mark.asyncio
async def test_a_conversation_works_again_after_a_cancelled_turn():
    in_backend = asyncio.Event()
    answers = iter(["unused", "после отмены"])

    async def handler(messages, conversation):
        reply = next(answers)
        if reply == "unused":
            in_backend.set()
            await asyncio.Event().wait()
        return reply

    service = ChatService(HookBackend(handler))

    async def one_turn(text):
        async with service.turn(WEB) as turn:
            return await turn.send(text)

    task = asyncio.create_task(one_turn("первое"))
    await asyncio.wait_for(in_backend.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    outcome = await asyncio.wait_for(one_turn("второе"), timeout=2)

    assert outcome == ChatOutcome(OutcomeStatus.REPLIED, reply="после отмены")
    assert seams.history_of(WEB) == [
        {"role": "user", "content": "второе"},
        {"role": "assistant", "content": "после отмены"},
    ]


# --- while_working: active around the backend call only -----------------------


@pytest.mark.asyncio
async def test_while_working_surrounds_the_backend_call_and_nothing_else():
    log: list[str] = []

    async def handler(messages, conversation):
        log.append("backend")
        return "ответ"

    service = ChatService(HookBackend(handler))

    async with service.turn(WEB) as turn:
        log.append("before")
        outcome = await turn.send("вопрос", while_working=Working(log))
        log.append("after")

    assert outcome.status is OutcomeStatus.REPLIED
    assert log == ["before", "working-start", "backend", "working-stop", "after"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [BackendRateLimitedError("x"), RuntimeError("synthetic")],
    ids=["recognized-failure", "unexpected-failure"],
)
async def test_while_working_ends_before_a_failure_is_reported(error):
    log: list[str] = []
    service = ChatService(FakeBackend(error=error))
    working = Working(log)

    try:
        async with service.turn(WEB) as turn:
            await turn.send("вопрос", while_working=working)
            log.append("after")
    except RuntimeError:
        log.append("propagated")

    assert log[:2] == ["working-start", "working-stop"]
    assert log[2] in ("after", "propagated")


@pytest.mark.asyncio
async def test_while_working_ends_when_the_turn_is_cancelled():
    log: list[str] = []
    in_backend = asyncio.Event()

    async def hang(messages, conversation):
        in_backend.set()
        await asyncio.Event().wait()

    service = ChatService(HookBackend(hang))

    async def one_turn():
        async with service.turn(WEB) as turn:
            await turn.send("вопрос", while_working=Working(log))

    task = asyncio.create_task(one_turn())
    await asyncio.wait_for(in_backend.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert log == ["working-start", "working-stop"]


@pytest.mark.asyncio
async def test_a_failing_while_working_rolls_back_and_propagates():
    # The indicator is part of the transaction: a failure to enter it means the
    # exchange never started, exactly as a failure of the backend call itself.
    prior = seams.seed_conversation(WEB, [("user", "раньше"), ("assistant", "ответ")])
    backend = FakeBackend()
    service = ChatService(backend)

    def broken():
        raise RuntimeError("no indicator")

    with pytest.raises(RuntimeError, match="no indicator"):
        async with service.turn(WEB) as turn:
            await turn.send("вопрос", while_working=broken)

    assert backend.requests == []
    assert seams.history_of(WEB) == prior


# --- reset --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_reset_clears_the_history_and_keeps_the_lock():
    seams.seed_conversation(WEB, [("user", "раньше"), ("assistant", "ответ")])
    service = ChatService(FakeBackend())

    async with service.turn(WEB) as turn:
        outcome = await turn.reset(source="test")

        assert outcome == ChatOutcome(OutcomeStatus.CONTEXT_CLEARED)
        assert seams.history_of(WEB) == []
        assert seams.lock_for(WEB).locked()  # the confirmation can still be delivered under it


@pytest.mark.asyncio
async def test_the_service_reset_clears_the_history_and_releases_the_lock():
    seams.seed_conversation(WEB, [("user", "раньше"), ("assistant", "ответ")])
    backend = FakeBackend()
    service = ChatService(backend)

    outcome = await service.reset(WEB, source="test")

    assert outcome == ChatOutcome(OutcomeStatus.CONTEXT_CLEARED)
    assert seams.history_of(WEB) == []
    assert not seams.lock_for(WEB).locked()
    assert backend.requests == []


@pytest.mark.asyncio
async def test_a_reset_leaves_other_conversations_alone():
    same_subject_elsewhere = ConversationKey("telegram", WEB.subject)
    seams.seed_conversation(WEB, [("user", "тут")])
    elsewhere = seams.seed_conversation(same_subject_elsewhere, [("user", "там")])
    other = seams.seed_conversation(OTHER_WEB, [("user", "другой")])
    service = ChatService(FakeBackend())

    await service.reset(WEB, source="test")

    assert seams.history_of(WEB) == []
    assert seams.history_of(same_subject_elsewhere) == elsewhere
    assert seams.history_of(OTHER_WEB) == other


@pytest.mark.asyncio
async def test_a_service_reset_waits_for_the_exchange_in_flight():
    in_backend = asyncio.Event()
    release = asyncio.Event()

    async def gated(messages, conversation):
        in_backend.set()
        await release.wait()
        return "ответ"

    service = ChatService(HookBackend(gated))

    async def one_turn():
        async with service.turn(WEB) as turn:
            return await turn.send("вопрос")

    exchange = asyncio.create_task(one_turn())
    await asyncio.wait_for(in_backend.wait(), timeout=2)
    reset = asyncio.create_task(service.reset(WEB, source="test"))
    await seams.settle()

    assert not reset.done()
    assert seams.history_of(WEB) == [{"role": "user", "content": "вопрос"}]

    release.set()
    await _done(exchange, reset)

    assert seams.history_of(WEB) == []  # the reset came after, and wiped the exchange too


# --- locking ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_next_turn_waits_for_the_previous_one_to_finish_delivering():
    log: list[str] = []
    delivering = asyncio.Event()
    release_delivery = asyncio.Event()

    async def handler(messages, conversation):
        log.append(f"backend:{messages[-1]['content']}")
        return f"ответ на {messages[-1]['content']}"

    service = ChatService(HookBackend(handler))

    async def first():
        async with service.turn(WEB) as turn:
            await turn.send("one")
            log.append("deliver-start")
            delivering.set()
            await release_delivery.wait()  # a slow delivery, still inside the scope
            log.append("deliver-done")

    async def second():
        async with service.turn(WEB) as turn:
            await turn.send("two")

    first_task = asyncio.create_task(first())
    await asyncio.wait_for(delivering.wait(), timeout=2)
    second_task = asyncio.create_task(second())
    await seams.settle()

    assert log == ["backend:one", "deliver-start"]  # "two" has not reached the backend
    assert not second_task.done()
    assert seams.history_of(WEB) == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "ответ на one"},
    ]

    release_delivery.set()
    await _done(first_task, second_task)

    assert log == ["backend:one", "deliver-start", "deliver-done", "backend:two"]


@pytest.mark.asyncio
async def test_different_conversations_are_processed_concurrently():
    entered: list[ConversationKey] = []
    both_in = asyncio.Event()

    async def handler(messages, conversation):
        entered.append(conversation)
        if len(entered) == 2:
            both_in.set()
        # Neither request can finish until both have arrived, so a service that
        # serialized across conversations would time out here.
        await asyncio.wait_for(both_in.wait(), timeout=2)
        return "ответ"

    service = ChatService(HookBackend(handler))

    async def one_turn(key):
        async with service.turn(key) as turn:
            return await turn.send("привет")

    await asyncio.wait_for(
        asyncio.gather(one_turn(WEB), one_turn(OTHER_WEB)),
        timeout=3,
    )

    assert set(entered) == {WEB, OTHER_WEB}


@pytest.mark.asyncio
async def test_two_services_share_one_lock_and_one_history():
    first_service = ChatService(FakeBackend(reply="от первого"))
    second_backend = FakeBackend(reply="от второго")
    second_service = ChatService(second_backend)
    holding = asyncio.Event()
    release = asyncio.Event()

    async def hold_with_first():
        async with first_service.turn(WEB) as turn:
            await turn.send("раз")
            holding.set()
            await release.wait()

    async def use_second():
        async with second_service.turn(WEB) as turn:
            return await turn.send("два")

    holder = asyncio.create_task(hold_with_first())
    await asyncio.wait_for(holding.wait(), timeout=2)
    waiter = asyncio.create_task(use_second())
    await seams.settle()

    # The second service's turn on the same conversation has to wait, even
    # though it is a different ChatService instance...
    assert not waiter.done()
    assert second_backend.requests == []

    release.set()
    await _done(holder, waiter)

    # ...and when it runs it sees the history the first one built.
    assert second_backend.requests[0][0] == [
        {"role": "user", "content": "раз"},
        {"role": "assistant", "content": "от первого"},
        {"role": "user", "content": "два"},
    ]
    assert seams.lock_keys() == {WEB}


@pytest.mark.asyncio
async def test_two_services_do_not_block_each_others_other_conversations():
    first_service = ChatService(FakeBackend())
    second_service = ChatService(FakeBackend(reply="свободно"))

    async with first_service.turn(WEB):
        async with second_service.turn(OTHER_WEB) as turn:
            outcome = await asyncio.wait_for(turn.send("привет"), timeout=2)

    assert outcome.status is OutcomeStatus.REPLIED


@pytest.mark.asyncio
async def test_the_lock_is_held_from_entering_to_leaving_the_scope():
    service = ChatService(FakeBackend())
    lock = seams.lock_for(WEB)

    scope = service.turn(WEB)
    assert not lock.locked()  # creating the scope takes nothing

    async with scope:
        assert lock.locked()

    assert not lock.locked()


@pytest.mark.asyncio
async def test_the_lock_is_released_when_the_body_fails():
    service = ChatService(FakeBackend())

    with pytest.raises(ValueError):
        async with service.turn(WEB):
            raise ValueError("body failed")

    assert not seams.lock_for(WEB).locked()


@pytest.mark.asyncio
async def test_the_lock_is_released_when_a_waiting_turn_is_cancelled():
    service = ChatService(FakeBackend())

    async with service.turn(WEB):
        waiter = asyncio.create_task(service.reset(WEB, source="test"))
        await seams.settle()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert seams.lock_for(WEB).locked()  # still the first turn's

    assert not seams.lock_for(WEB).locked()
    await asyncio.wait_for(service.reset(WEB, source="test"), timeout=2)


# --- the turn is only valid inside its scope ----------------------------------


@pytest.mark.asyncio
async def test_a_turn_cannot_send_after_its_scope_has_exited():
    backend = FakeBackend()
    service = ChatService(backend)

    async with service.turn(WEB) as turn:
        pass

    with pytest.raises(RuntimeError, match="has ended"):
        await turn.send("слишком поздно")

    assert backend.requests == []
    assert seams.history_of(WEB) == []


@pytest.mark.asyncio
async def test_a_turn_cannot_reset_after_its_scope_has_exited():
    prior = seams.seed_conversation(WEB, [("user", "раньше"), ("assistant", "ответ")])
    service = ChatService(FakeBackend())

    async with service.turn(WEB) as turn:
        pass

    with pytest.raises(RuntimeError, match="has ended"):
        await turn.reset(source="test")

    assert seams.history_of(WEB) == prior


@pytest.mark.asyncio
async def test_a_turn_is_over_after_a_failing_scope_too():
    service = ChatService(FakeBackend())
    captured = []

    with pytest.raises(ValueError):
        async with service.turn(WEB) as turn:
            captured.append(turn)
            raise ValueError("body failed")

    with pytest.raises(RuntimeError, match="has ended"):
        await captured[0].send("слишком поздно")


@pytest.mark.asyncio
async def test_a_turn_from_an_earlier_scope_is_not_usable_in_a_later_one():
    service = ChatService(FakeBackend())

    async with service.turn(WEB) as old:
        pass
    async with service.turn(WEB):
        with pytest.raises(RuntimeError, match="has ended"):
            await old.send("чужая сессия")


@pytest.mark.asyncio
async def test_a_turn_refuses_overlapping_operations():
    in_backend = asyncio.Event()
    release = asyncio.Event()

    async def gated(messages, conversation):
        in_backend.set()
        await release.wait()
        return "ответ"

    service = ChatService(HookBackend(gated))

    async with service.turn(WEB) as turn:
        first = asyncio.create_task(turn.send("раз"))
        await asyncio.wait_for(in_backend.wait(), timeout=2)

        # Bounded, so a missing guard fails fast instead of queueing behind the
        # gated backend call and hanging the suite.
        with pytest.raises(RuntimeError, match="in progress"):
            await asyncio.wait_for(turn.send("два"), timeout=1)
        with pytest.raises(RuntimeError, match="in progress"):
            await asyncio.wait_for(turn.reset(source="test"), timeout=1)

        release.set()
        assert (await first).status is OutcomeStatus.REPLIED
        # The refused operations changed nothing, and the turn is usable again.
        assert seams.history_of(WEB) == [
            {"role": "user", "content": "раз"},
            {"role": "assistant", "content": "ответ"},
        ]
        assert (await turn.reset(source="test")).status is OutcomeStatus.CONTEXT_CLEARED
