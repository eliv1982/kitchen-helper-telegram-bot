"""KH-F07A: the 40-message window and the ingredients named within it.

These tests are deterministic and deliberately narrow. With scripted backends they
show that the context the assistant is shown keeps the ingredients a person named
across turns, their corrections, substitutions and quantities, in chronological
order; that the window never exceeds 40 messages including the pending one; and
that conversations and accounts stay apart. They do **not** show that the model
obeys the prompt's ingredient rules -- that needs a real-provider evaluation
(tests/test_kitchen_prompt.py pins only that the rules are present and unchanged).
"""

import pytest

import context_manager
from house_helper.core.backend import BackendRateLimitedError
from house_helper.core.chat_service import ChatService
from house_helper.core.identity import ConversationKey
from house_helper.core.limits import MAX_HISTORY_MESSAGES
from house_helper.core.outcomes import OutcomeStatus
from tests.chat_support import ScriptedBackend, chat_app
from tests.helpers import FakeBackend
from tests.telegram_support import MOTHER, telegram_app

pytestmark = pytest.mark.asyncio

TELEGRAM_ONE = ConversationKey("telegram", "1")
TELEGRAM_TWO = ConversationKey("telegram", "2")


async def say(service: ChatService, key: ConversationKey, text: str):
    async with service.turn(key) as turn:
        return await turn.send(text)


def contents(messages) -> list[str]:
    return [message["content"] for message in messages]


# --- what the Telegram service sends -------------------------------------------------------------


async def test_an_ingredient_added_in_a_later_turn_joins_the_earlier_ones():
    backend = FakeBackend("Предложу омлет.")
    service = ChatService(backend)

    await say(service, TELEGRAM_ONE, "У меня есть картофель и лук.")
    await say(service, TELEGRAM_ONE, "Ещё есть яйца и молоко.")

    assert contents(backend.requests[1][0]) == [
        "У меня есть картофель и лук.",
        "Предложу омлет.",
        "Ещё есть яйца и молоко.",
    ]


async def test_a_correction_and_a_substitution_stay_in_the_model_context():
    backend = FakeBackend("Хорошо.")
    service = ChatService(backend)

    await say(service, TELEGRAM_ONE, "Есть картофель и лук.")
    await say(service, TELEGRAM_ONE, "Лука больше нет.")
    await say(service, TELEGRAM_ONE, "Рис вместо картофеля.")

    sent = backend.requests[2][0]
    assert contents(sent)[:5] == [
        "Есть картофель и лук.",
        "Хорошо.",
        "Лука больше нет.",
        "Хорошо.",
        "Рис вместо картофеля.",
    ]
    assert sent[-1] == {"role": "user", "content": "Рис вместо картофеля."}
    # chronological order: the latest explicit correction comes last
    assert [message["role"] for message in sent] == ["user", "assistant"] * 2 + ["user"]


async def test_a_quantity_update_stays_in_the_model_context():
    backend = FakeBackend("Понял.")
    service = ChatService(backend)

    await say(service, TELEGRAM_ONE, "У меня 500 г риса.")
    await say(service, TELEGRAM_ONE, "Риса осталось 200 г.")

    assert contents(backend.requests[1][0]) == [
        "У меня 500 г риса.",
        "Понял.",
        "Риса осталось 200 г.",
    ]


async def test_the_telegram_request_stays_within_40_messages_including_the_pending_one():
    backend = FakeBackend("Ответ")
    service = ChatService(backend)

    for index in range(25):  # 50 completed messages: well past the window
        await say(service, TELEGRAM_ONE, f"вопрос {index}")

    assert len(backend.requests) == 25
    assert max(len(request[0]) for request in backend.requests) <= MAX_HISTORY_MESSAGES == 40
    # 38 stored messages and the pending one...
    assert len(backend.requests[19][0]) == 39
    # ...then exactly 40 stored: the window drops one answer, never the question before it.
    assert len(backend.requests[20][0]) == 39
    sent = backend.requests[-1][0]
    # 40 less the leading answer the window would start with: it opens on a question.
    assert len(sent) == 39
    assert sent[0]["role"] == "user"
    assert sent[-1] == {"role": "user", "content": "вопрос 24"}
    assert [message["content"] for message in sent if message["role"] == "user"] == [
        f"вопрос {i}" for i in range(5, 25)
    ]
    # The stored history keeps its pair-trimmed bound of 40, 20 whole exchanges.
    assert len(context_manager.get_context(TELEGRAM_ONE)) == 40


async def test_a_failed_turn_never_becomes_part_of_the_later_context():
    backend = FakeBackend("Омлет.")
    service = ChatService(backend)

    await say(service, TELEGRAM_ONE, "У меня есть яйца.")
    backend.error = BackendRateLimitedError("x")
    failed = await say(service, TELEGRAM_ONE, "И сыр.")
    backend.error = None
    await say(service, TELEGRAM_ONE, "Что приготовить?")

    assert failed.status is OutcomeStatus.RATE_LIMITED
    assert contents(backend.requests[-1][0]) == [
        "У меня есть яйца.",
        "Омлет.",
        "Что приготовить?",
    ]


async def test_a_reset_starts_a_new_ingredient_context():
    backend = FakeBackend("Ответ")
    service = ChatService(backend)

    await say(service, TELEGRAM_ONE, "У меня есть яйца.")
    async with service.turn(TELEGRAM_ONE) as turn:
        await turn.reset(source="test")
    await say(service, TELEGRAM_ONE, "У меня есть рис.")

    assert backend.requests[-1][0] == [{"role": "user", "content": "У меня есть рис."}]


async def test_two_conversations_do_not_share_ingredients():
    backend = FakeBackend()
    service = ChatService(backend)

    await say(service, TELEGRAM_ONE, "У меня есть яйца.")
    await say(service, TELEGRAM_TWO, "Что приготовить?")

    assert backend.requests[1][0] == [{"role": "user", "content": "Что приготовить?"}]


async def test_two_accounts_do_not_share_ingredients():
    backend = FakeBackend()
    service = ChatService(backend)

    await say(service, ConversationKey("telegram", "100"), "Секрет: у меня есть трюфель.")
    await say(service, ConversationKey("telegram", "200"), "Что приготовить?")

    assert backend.requests[1][0] == [{"role": "user", "content": "Что приготовить?"}]


# --- what the web chat sends ---------------------------------------------------------------------


async def test_the_web_follow_up_keeps_the_ingredients_named_before(tmp_path):
    backend = ScriptedBackend("Омлет.", "Сырники.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "У меня есть яйца, молоко и мука.", "k-1")
        await chat.send(conversation, "Молоко закончилось. Что приготовить?", "k-2")

    assert contents(backend.requests[-1][0]) == [
        "У меня есть яйца, молоко и мука.",
        "Омлет.",
        "Молоко закончилось. Что приготовить?",
    ]


async def test_web_quantities_and_substitutions_reach_the_model_in_order(tmp_path):
    backend = ScriptedBackend("Взял на заметку.", "Взял на заметку.", "Взял на заметку.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "У меня 500 г риса и картофель.", "k-1")
        await chat.send(conversation, "Риса осталось 200 г.", "k-2")
        await chat.send(conversation, "И рис вместо картофеля.", "k-3")

    assert contents(backend.requests[-1][0]) == [
        "У меня 500 г риса и картофель.",
        "Взял на заметку.",
        "Риса осталось 200 г.",
        "Взял на заметку.",
        "И рис вместо картофеля.",
    ]


async def test_a_new_web_conversation_does_not_see_another_conversations_ingredients(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        first = await chat.create()
        await chat.send(first, "У меня есть яйца.", "k-1")
        second = await chat.create()
        await chat.send(second, "Что приготовить?", "k-1")

    assert backend.requests[-1][0] == [{"role": "user", "content": "Что приготовить?"}]


async def test_another_web_account_does_not_see_the_ingredients(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        other = await chat.second_user()
        mine = await chat.create()
        await chat.send(mine, "У меня есть трюфель.", "k-1")
        theirs = await chat.create(other)
        await chat.send(theirs, "Что приготовить?", "k-1", other)

    assert backend.requests[-1][0] == [{"role": "user", "content": "Что приготовить?"}]


async def test_a_failed_web_turn_never_becomes_part_of_the_later_context(tmp_path):
    backend = ScriptedBackend("Омлет.", BackendRateLimitedError("x"), "Каша.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "У меня есть яйца.", "k-1")
        failed = await chat.send(conversation, "И сыр.", "k-2")
        await chat.send(conversation, "Что приготовить?", "k-3")

    assert failed.status_code == 503
    assert contents(backend.requests[-1][0]) == [
        "У меня есть яйца.",
        "Омлет.",
        "Что приготовить?",
    ]


# --- one conversation across the two channels ----------------------------------------------------


async def test_a_conversation_shared_with_telegram_keeps_its_ingredients(tmp_path):
    backend = ScriptedBackend("Омлет.", "Сырники.")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        conversation = await t.chat.create()
        await t.chat.send(conversation, "У меня есть яйца и лук.", "web-1")
        await t.settle()
        t.chat.clock.advance(seconds=5)

        await t.deliver(MOTHER, "Лука больше нет. Что приготовить?")

    assert contents(backend.requests[-1][0]) == [
        "У меня есть яйца и лук.",
        "Омлет.",
        "Лука больше нет. Что приготовить?",
    ]
