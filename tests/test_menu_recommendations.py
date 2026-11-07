"""KH-MENU-01A: festive/budget recommendations and the conversation that carries them.

What is deterministic here:

  * the shared prompt contains the occasion/budget rules, and the web backend and the
    Telegram adapter send that SAME text to the provider (their real request shapes are
    exercised, with offline clients);
  * the ordered history of an inventory -> holiday -> budget -> recipe -> shopping-list
    conversation reaches the backend on both channels, so the model is given every turn
    a change of intent depends on;
  * an explicit no-shopping message is passed through verbatim, so the application
    neither rewrites it nor lifts it;
  * the five Codex-audit scenarios (a task-scoped restriction, an ongoing restriction,
    permission after a prohibition, a partial inventory, and allergy/diet continuity)
    reach the model as ordered history, and the prompt states the rule each one turns on;
  * ordinary cooking from named ingredients keeps its own rule.

What is NOT shown here: that a real model obeys any of it. The scripted replies are
chosen by the test, not by a provider, and the prompt-clause assertions only prove that
the rule is present. Prompt compliance is model behaviour; it needs a separately
approved real-provider evaluation (the same limit KH-F07A records). These tests only
prove the contract and the inputs the model gets.
"""

from types import SimpleNamespace

import pytest

import api_client
from house_helper.core.chat_service import ChatService
from house_helper.core.identity import ConversationKey
from house_helper.core.kitchen_prompt import KITCHEN_SYSTEM_PROMPT
from house_helper.core.openai_chat import StandaloneOpenAIBackend
from house_helper.core.provider_settings import ProviderSettings
from tests.chat_support import ScriptedBackend, chat_app
from tests.helpers import FakeBackend, make_openai_response
from tests.telegram_support import MOTHER, telegram_app

pytestmark = pytest.mark.asyncio

TELEGRAM_ONE = ConversationKey("telegram", "1")

INVENTORY = "У меня есть курица, картофель, лук и морковь."
HOLIDAY = "Что приготовить на Новый год?"
BUDGET = "Хочется подешевле."
RECIPE = "Давай запечённую курицу."
SHOPPING = "Что нужно купить?"

# The five Codex-audit scenarios (R1-R3).
TASK_SCOPED_RESTRICTION = "На ужин сегодня ничего не покупать."
UNRELATED_TASK = "А что приготовить на день рождения?"
ONGOING_RESTRICTION = "На этой неделе я не могу покупать продукты."
PROHIBITION = "На день рождения ничего не покупать."
PERMISSION = "Вообще-то для дня рождения можно кое-что купить."
MENU_AFTER_PERMISSION = "Тогда предложи меню."
PARTIAL_INVENTORY = (
    "У меня есть курица и рис, но моркови точно нет. Остальное не проверял."
)
DISH_SELECTION = "Давай курицу с рисом."
ALLERGY_DIET = "У сына аллергия на орехи, и мы не едим свинину."
SUBSTITUTION = "Чем заменить сливки?"


def contents(messages) -> list[str]:
    return [message["content"] for message in messages]


async def say(service: ChatService, key: ConversationKey, text: str):
    async with service.turn(key) as turn:
        return await turn.send(text)


class RecordingClient:
    """Shaped like AsyncOpenAI for the one call each adapter makes, offline."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return make_openai_response("Ответ")

    async def close(self) -> None:  # pragma: no cover - only for completeness
        pass


# --- the shared contract, at the provider-request level -------------------------------------------


async def test_web_and_telegram_adapters_send_the_same_recommendation_rules(monkeypatch):
    web_client = RecordingClient()
    await StandaloneOpenAIBackend(
        ProviderSettings(api_key="test-key", model="test-model"), client=web_client
    ).complete(
        [{"role": "user", "content": HOLIDAY}],
        conversation=ConversationKey("web", "00000000-0000-4000-8000-000000000001"),
        context_len=1,
    )

    telegram_client = RecordingClient()
    monkeypatch.setattr(api_client, "_client", telegram_client)
    await api_client.get_chat_response(
        [{"role": "user", "content": HOLIDAY}], user_id="telegram:1", context_len=1
    )

    web_prompt = web_client.calls[0]["messages"][0]["content"]
    telegram_prompt = telegram_client.calls[0]["messages"][0]["content"]
    assert web_prompt == telegram_prompt is KITCHEN_SYSTEM_PROMPT
    assert "Праздник, гости и бюджет:" in web_prompt
    # The two rules a festive request turns on: the ingredients are a base, not a
    # boundary, and an occasion request is not a "only from this" request.
    assert "названные продукты — основа" in web_prompt
    assert "выбор блюд, а не «только из перечисленного»" in web_prompt


# --- the history the model is given ---------------------------------------------------------------


async def test_a_holiday_request_after_an_inventory_keeps_both_turns_for_the_model():
    backend = FakeBackend("На Новый год можно запечь курицу.")
    service = ChatService(backend)

    await say(service, TELEGRAM_ONE, INVENTORY)
    await say(service, TELEGRAM_ONE, HOLIDAY)

    assert contents(backend.requests[-1][0]) == [
        INVENTORY,
        "На Новый год можно запечь курицу.",
        HOLIDAY,
    ]


async def test_an_ordinary_cooking_request_is_still_about_the_named_ingredients():
    backend = FakeBackend("Омлет с луком.")
    service = ChatService(backend)

    await say(service, TELEGRAM_ONE, INVENTORY)
    await say(service, TELEGRAM_ONE, "Что приготовить из этого?")

    assert contents(backend.requests[-1][0]) == [
        INVENTORY,
        "Омлет с луком.",
        "Что приготовить из этого?",
    ]


async def test_an_explicit_no_shopping_message_reaches_the_model_verbatim():
    backend = FakeBackend("Хорошо, только из имеющегося.")
    service = ChatService(backend)
    restriction = "На Новый год, но ничего не покупать."

    await say(service, TELEGRAM_ONE, INVENTORY)
    await say(service, TELEGRAM_ONE, restriction)

    assert contents(backend.requests[-1][0]) == [
        INVENTORY,
        "Хорошо, только из имеющегося.",
        restriction,
    ]


async def test_an_explicit_no_shopping_message_reaches_the_web_model_verbatim(tmp_path):
    backend = ScriptedBackend("Хорошо, только из имеющегося.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, INVENTORY, "k-1")
        await chat.send(conversation, "На Новый год, но ничего не покупать.", "k-2")

    assert contents(backend.requests[-1][0]) == [
        INVENTORY,
        "Хорошо, только из имеющегося.",
        "На Новый год, но ничего не покупать.",
    ]


async def test_the_web_sequence_inventory_holiday_budget_recipe_shopping_reaches_the_model(tmp_path):
    backend = ScriptedBackend(
        "Праздничные варианты.",
        "Подешевле.",
        "Запеченная курица.",
        "Что купить.",
        default="Ответ.",
    )
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, INVENTORY, "k-1")
        await chat.send(conversation, HOLIDAY, "k-2")
        await chat.send(conversation, BUDGET, "k-3")
        await chat.send(conversation, RECIPE, "k-4")
        await chat.send(conversation, SHOPPING, "k-5")

    sent = backend.requests[-1][0]
    assert contents(sent) == [
        INVENTORY,
        "Праздничные варианты.",
        HOLIDAY,
        "Подешевле.",
        BUDGET,
        "Запеченная курица.",
        RECIPE,
        "Что купить.",
        SHOPPING,
    ]
    assert [message["role"] for message in sent] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert backend.requests[-1][2] == len(sent)  # the pending message is already in `sent`


async def test_the_telegram_sequence_uses_the_same_ordered_history(tmp_path):
    backend = ScriptedBackend(
        "Праздничные варианты.",
        "Подешевле.",
        "Запеченная курица.",
        "Что купить.",
        default="Ответ.",
    )
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, INVENTORY)
        await t.deliver(MOTHER, HOLIDAY)
        await t.deliver(MOTHER, BUDGET)
        await t.deliver(MOTHER, RECIPE)
        await t.deliver(MOTHER, SHOPPING)

    sent = backend.requests[-1][0]
    assert contents(sent) == [
        INVENTORY,
        "Праздничные варианты.",
        HOLIDAY,
        "Подешевле.",
        BUDGET,
        "Запеченная курица.",
        RECIPE,
        "Что купить.",
        SHOPPING,
    ]


# --- Codex FAIL remediation: R1-R3 scenario regressions --------------------------------------------
#
# Each test proves two deterministic things: the ordered turns the adapter hands the model,
# and the prompt clause that states the rule the scenario turns on. The scripted assistant
# replies are placeholders chosen by the test. None of this shows that a real model obeys
# the clause; that is model behaviour and needs a separately approved provider evaluation.


async def test_a_task_scoped_no_shopping_restriction_is_not_carried_into_an_unrelated_task():
    backend = FakeBackend("Хорошо, на ужин — только из имеющегося.")
    service = ChatService(backend)

    await say(service, TELEGRAM_ONE, TASK_SCOPED_RESTRICTION)
    await say(service, TELEGRAM_ONE, UNRELATED_TASK)

    # The application has no restriction state of its own: the two user turns and the
    # assistant's reply are all the model gets, so nothing of the application lifts or
    # extends the restriction. The scoping rule is the prompt's.
    assert contents(backend.requests[-1][0]) == [
        TASK_SCOPED_RESTRICTION,
        "Хорошо, на ужин — только из имеющегося.",
        UNRELATED_TASK,
    ]
    assert [message["role"] for message in backend.requests[-1][0]] == [
        "user",
        "assistant",
        "user",
    ]
    assert "запрет покупок в названных рамках" in KITCHEN_SYSTEM_PROMPT
    assert "Вне их он не действует" in KITCHEN_SYSTEM_PROMPT


async def test_an_ongoing_no_shopping_restriction_survives_a_change_of_task(tmp_path):
    backend = ScriptedBackend("Пока готовим из того, что есть.", default="Ответ.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, ONGOING_RESTRICTION, "k-1")
        await chat.send(conversation, HOLIDAY, "k-2")

    assert contents(backend.requests[-1][0]) == [
        ONGOING_RESTRICTION,
        "Пока готовим из того, что есть.",
        HOLIDAY,
    ]
    # A period restriction lasts until its period ends, and the new task does not cancel it.
    assert "на срок — до его конца" in KITCHEN_SYSTEM_PROMPT
    assert "смена задачи не отменяет запрет на срок или бессрочный" in KITCHEN_SYSTEM_PROMPT


async def test_explicit_permission_supersedes_a_previous_prohibition_in_its_scope():
    backend = FakeBackend("Хорошо, только из имеющегося.")
    service = ChatService(backend)

    await say(service, TELEGRAM_ONE, PROHIBITION)
    await say(service, TELEGRAM_ONE, PERMISSION)
    await say(service, TELEGRAM_ONE, MENU_AFTER_PERMISSION)

    assert contents(backend.requests[-1][0]) == [
        PROHIBITION,
        "Хорошо, только из имеющегося.",
        PERMISSION,
        "Хорошо, только из имеющегося.",
        MENU_AFTER_PERMISSION,
    ]
    assert (
        "Разрешение докупить для текущей задачи снимает прежний запрет"
        in KITCHEN_SYSTEM_PROMPT
    )


async def test_a_partial_inventory_reaches_the_model_with_missing_and_unknown_apart(tmp_path):
    backend = ScriptedBackend(
        "Запишите, что моркови нет.",
        "Вот рецепт курицы с рисом.",
        "Купите морковь.",
        default="Ответ.",
    )
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, PARTIAL_INVENTORY, "k-1")
        await chat.send(conversation, DISH_SELECTION, "k-2")
        await chat.send(conversation, SHOPPING, "k-3")

    assert contents(backend.requests[-1][0]) == [
        PARTIAL_INVENTORY,
        "Запишите, что моркови нет.",
        DISH_SELECTION,
        "Вот рецепт курицы с рисом.",
        SHOPPING,
    ]
    # R2's contract: the three states, the wording for an unknown item, and the rule that
    # only a confirmed-absent ingredient belongs on the shopping list.
    assert "подтверждено, что продукт есть; подтверждено, что его нет" in KITCHEN_SYSTEM_PROMPT
    assert "не считай его ни имеющимся, ни отсутствующим" in KITCHEN_SYSTEM_PROMPT
    assert "проверьте, есть ли у вас это; купите, если нужно" in KITCHEN_SYSTEM_PROMPT
    assert "подтверждённо отсутствующее — в список покупок" in KITCHEN_SYSTEM_PROMPT


async def test_allergy_and_diet_conditions_survive_holiday_budget_and_substitution_turns(tmp_path):
    backend = ScriptedBackend(
        "Праздничные варианты без орехов и свинины.",
        "Подешевле, без орехов и свинины.",
        "Сливки можно заменить сметаной.",
        default="Ответ.",
    )
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, ALLERGY_DIET)
        await t.deliver(MOTHER, HOLIDAY)
        await t.deliver(MOTHER, BUDGET)
        await t.deliver(MOTHER, SUBSTITUTION)

    # The condition turn is still in the history when the budget and substitution turns
    # are sent, so the model is given it on every one of them.
    assert contents(backend.requests[-1][0]) == [
        ALLERGY_DIET,
        "Праздничные варианты без орехов и свинины.",
        HOLIDAY,
        "Подешевле, без орехов и свинины.",
        BUDGET,
        "Сливки можно заменить сметаной.",
        SUBSTITUTION,
    ]
    assert "об аллергиях, непереносимости, исключённых продуктах" in KITCHEN_SYSTEM_PROMPT
    assert "не нарушай их ни в одном варианте" in KITCHEN_SYSTEM_PROMPT
