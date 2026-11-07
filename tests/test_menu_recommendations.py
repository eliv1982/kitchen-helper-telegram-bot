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
  * ordinary cooking from named ingredients keeps its own rule;
  * KH-MENU-01A-FIX-01's live smoke failures: an inventory followed by an open festive
    request (and by an explicit opt-out) reaches both models in order, unknown
    ingredients are not definite purchases, and the ideas -> recipe -> favorites
    workflow is in the shared prompt and in both help surfaces.
  * the Codex-FAIL remediation: staples get no availability exception, necessity and
    availability are separate dimensions (a required but unmentioned main ingredient is
    not made optional), and confirmed-missing and unknown required items go to different
    shopping-list sections. The Web help entry's reachability in an active conversation
    is behaviourally exercised by tests/js/dom_scenarios.js (tests/test_web_ui_dom.py);
    these Python tests pin that the same workflow text is in the shared prompt.

What is NOT shown here: that a real model obeys any of it. The scripted replies are
chosen by the test, not by a provider, and the prompt-clause assertions only prove that
the rule is present. Prompt compliance is model behaviour; it needs a separately
approved real-provider evaluation (the same limit KH-F07A records). These tests only
prove the contract and the inputs the model gets.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import api_client
from house_helper.core.chat_service import ChatService
from house_helper.core.identity import ConversationKey
from house_helper.core.kitchen_prompt import KITCHEN_SYSTEM_PROMPT
from house_helper.core.openai_chat import StandaloneOpenAIBackend
from house_helper.core.provider_settings import ProviderSettings
from house_helper.web import telegram_chat as tc
from house_helper.web import ui
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
    # The rules a festive request turns on: the ingredients are an optional resource,
    # not a base or a boundary; at least one dish is not built on them; and an occasion
    # request is not a "only from this" request (KH-MENU-01A-FIX-01).
    assert "необязательный ресурс, а не основа и не граница" in web_prompt
    assert "минимум один вариант должен быть не из прежних продуктов" in web_prompt
    assert "выбор блюд, а не «только из перечисленного»" in web_prompt
    assert "«точно купить» — только подтверждённо отсутствующее" in web_prompt


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
    # only a confirmed-absent ingredient belongs on the shopping list. The Codex-FAIL
    # remediation (R3) separates necessity from availability: a required item confirmed
    # missing is a definite purchase; a required item of unknown availability is checked;
    # only an optional addition is conditional.
    assert "подтверждено, что продукт есть; подтверждено, что его нет" in KITCHEN_SYSTEM_PROMPT
    assert "не считай его ни имеющимся, ни отсутствующим" in KITCHEN_SYSTEM_PROMPT
    assert "проверьте, есть ли у вас это; купите, если нужно" in KITCHEN_SYSTEM_PROMPT
    assert "«точно купить» — только подтверждённо отсутствующее" in KITCHEN_SYSTEM_PROMPT
    assert "«проверить» — нужное, но с неизвестным наличием" in KITCHEN_SYSTEM_PROMPT
    assert "Необязательное в «точно купить» не попадает" in KITCHEN_SYSTEM_PROMPT


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


# --- KH-MENU-01A-FIX-01: the live smoke remediation ------------------------------------------------
#
# The live smoke test (2026-11-07) found three things a real person saw:
#
#   1. after an inventory turn, an open New Year request that allowed shopping still
#      produced dishes built on that inventory, and kept doing so after she said using
#      it was optional;
#   2. salt, water and oil were silently assumed, and unmentioned eggs, cucumbers and
#      mayonnaise appeared as definite purchases;
#   3. the ideas -> chosen dish -> full recipe -> save to favorites workflow was not
#      explained where a person would look.
#
# The application's history assembly was already correct: both channels pass the ordered
# turns. The correction is in the one shared prompt and in the two help surfaces. As in
# the rest of this file, these tests pin the contract, the help wording and the ordered
# history, not a real model's compliance.

LIVE_INVENTORY = "У меня есть курица, картошка, лук и морковь. Что можно приготовить?"
LIVE_FESTIVE = (
    "А на Новый год хочется что-нибудь особенное и праздничное, но недорогое. "
    "Могу кое-что докупить. Что посоветуешь?"
)
LIVE_OPT_OUT = (
    "Использовать продукты, которые у меня есть, необязательно: предложи праздничные "
    "блюда, часть продуктов можно докупить."
)
LIVE_UNKNOWN = "У меня есть курица и картошка. Больше ничего не проверяла."
DISH_CHOSEN = "Давай картофельные гнезда с грибами."
RECIPE_OFFER = (
    "Выберите понравившееся блюдо, и я дам полный рецепт. "
    "Его можно будет сохранить в избранное"
)


async def test_the_live_inventory_then_festive_request_reaches_the_web_model_in_order(tmp_path):
    backend = ScriptedBackend("Праздничные варианты.", default="Ответ.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, LIVE_INVENTORY, "k-1")
        await chat.send(conversation, LIVE_FESTIVE, "k-2")

    # The model is given the inventory turn and the festive turn, in that order. The
    # application does not drop the inventory (KH-F07A) and does not rewrite the request.
    assert contents(backend.requests[-1][0]) == [
        LIVE_INVENTORY,
        "Праздничные варианты.",
        LIVE_FESTIVE,
    ]
    # What the prompt must say for that history: the inventory became an optional
    # resource, at least one dish is not built on it, and useful items are not excluded.
    assert "перешёл от готовки из имеющегося к открытому выбору блюд" in KITCHEN_SYSTEM_PROMPT
    assert "необязательный ресурс, а не основа и не граница" in KITCHEN_SYSTEM_PROMPT
    assert "минимум один вариант должен быть не из прежних продуктов" in KITCHEN_SYSTEM_PROMPT
    assert "не исключай прежние продукты нарочно" in KITCHEN_SYSTEM_PROMPT
    # Codex FAIL, R3: an open menu may require new main ingredients; they are neither
    # replaced by the inventory nor made optional.
    assert "Открытое меню может включать блюда с новыми основными продуктами" in KITCHEN_SYSTEM_PROMPT
    assert "не подменяй их имеющимися" in KITCHEN_SYSTEM_PROMPT
    # The audited anchoring wording must not return.
    assert "названные продукты — основа" not in KITCHEN_SYSTEM_PROMPT


async def test_the_explicit_inventory_opt_out_reaches_the_telegram_model_verbatim(tmp_path):
    backend = ScriptedBackend(
        "Праздничные варианты.",
        "Ответ.",
        default="Ответ.",
    )
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, LIVE_INVENTORY)
        await t.deliver(MOTHER, LIVE_FESTIVE)
        await t.deliver(MOTHER, LIVE_OPT_OUT)

    # The opt-out is passed through as the person wrote it, after the two earlier turns.
    assert contents(backend.requests[-1][0]) == [
        LIVE_INVENTORY,
        "Праздничные варианты.",
        LIVE_FESTIVE,
        "Ответ.",
        LIVE_OPT_OUT,
    ]
    # The prompt treats an explicit opt-out as removing the anchoring, not as a hint.
    assert "Если человек прямо сказал, что имеющееся использовать необязательно" in KITCHEN_SYSTEM_PROMPT
    assert "не опирайся на него как на основу" in KITCHEN_SYSTEM_PROMPT
    assert "не подставляй его в каждый вариант" in KITCHEN_SYSTEM_PROMPT


async def test_unknown_ingredients_are_check_items_not_definite_purchases(tmp_path):
    backend = ScriptedBackend(
        "Записала: курица и картошка.",
        "Вот рецепт картофельных гнезд.",
        default="Ответ.",
    )
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, LIVE_UNKNOWN, "k-1")
        await chat.send(conversation, DISH_CHOSEN, "k-2")
        await chat.send(conversation, SHOPPING, "k-3")

    assert contents(backend.requests[-1][0]) == [
        LIVE_UNKNOWN,
        "Записала: курица и картошка.",
        DISH_CHOSEN,
        "Вот рецепт картофельных гнезд.",
        SHOPPING,
    ]
    # Unknown stays a third state. A needed ingredient whose availability is unknown is
    # a check item, not a definite purchase; only what the dish does not require may be
    # offered as an optional addition (Codex FAIL, R3).
    assert "«точно купить» — только подтверждённо отсутствующее" in KITCHEN_SYSTEM_PROMPT
    assert "«проверить» — нужное, но с неизвестным наличием" in KITCHEN_SYSTEM_PROMPT
    assert "Необязательное в «точно купить» не попадает" in KITCHEN_SYSTEM_PROMPT
    assert "«если есть» и «можно добавить» — только для необязательных продуктов" in KITCHEN_SYSTEM_PROMPT
    # Codex FAIL, R1: salt, water and oil get no availability exception -- not even as a
    # stated assumption.
    assert "Соль, масло, вода и другие базовые запасы — не исключение" in KITCHEN_SYSTEM_PROMPT
    assert "если человек их не называл, их наличие тоже неизвестно" in KITCHEN_SYSTEM_PROMPT
    assert "назови это допущением одной строкой" not in KITCHEN_SYSTEM_PROMPT
    assert "допущения (порции, базовые запасы)" not in KITCHEN_SYSTEM_PROMPT
    # The wording that made every needed item a purchase is gone.
    assert "Нужное и подтверждённо отсутствующее — в список покупок" not in KITCHEN_SYSTEM_PROMPT


async def test_unknown_salt_oil_and_water_are_never_assumed_available(tmp_path):
    # Codex FAIL, R1: the earlier wording let salt, oil and water be assumed and then
    # asked for the assumption to be stated; that is still invented availability. The
    # conversation where the person named only chicken and potatoes must reach the model
    # unchanged, and the prompt must say the staples are unknown too.
    backend = ScriptedBackend("Уточните, пожалуйста, что есть дома.", default="Ответ.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "У меня есть курица и картошка. Больше ничего не проверяла.", "k-1")

    assert contents(backend.requests[-1][0]) == [
        "У меня есть курица и картошка. Больше ничего не проверяла.",
    ]
    assert "Соль, масло, вода и другие базовые запасы — не исключение" in KITCHEN_SYSTEM_PROMPT
    assert "если человек их не называл, их наличие тоже неизвестно" in KITCHEN_SYSTEM_PROMPT
    assert "не считай их имеющимися по умолчанию" in KITCHEN_SYSTEM_PROMPT
    assert "Неизвестность не повод допрашивать про каждый базовый продукт" in KITCHEN_SYSTEM_PROMPT
    assert "говори о таком условно" in KITCHEN_SYSTEM_PROMPT
    assert "назови это допущением" not in KITCHEN_SYSTEM_PROMPT


async def test_a_required_but_unmentioned_main_ingredient_is_not_made_optional(tmp_path):
    # Codex FAIL, R3: necessity and availability are independent dimensions. An open
    # recommendation may name a dish that requires a main ingredient the person never
    # mentioned; unknown availability makes it a check-before-buying item, not an
    # optional addition and not a reason to substitute something already at home.
    backend = ScriptedBackend("Праздничные варианты.", default="Ответ.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, LIVE_INVENTORY, "k-1")
        await chat.send(conversation, LIVE_FESTIVE, "k-2")

    assert contents(backend.requests[-1][0]) == [
        LIVE_INVENTORY,
        "Праздничные варианты.",
        LIVE_FESTIVE,
    ]
    assert "Нужность и наличие — разные вещи" in KITCHEN_SYSTEM_PROMPT
    assert "блюдо может требовать продукт, которого человек не называл" in KITCHEN_SYSTEM_PROMPT
    assert "неизвестное наличие не делает обязательный продукт необязательным" in KITCHEN_SYSTEM_PROMPT
    assert "Обязательный продукт с неизвестным наличием — в «проверить»" in KITCHEN_SYSTEM_PROMPT
    assert "«если есть» и «можно добавить» — только для необязательных продуктов" in KITCHEN_SYSTEM_PROMPT
    assert "Открытое меню может включать блюда с новыми основными продуктами" in KITCHEN_SYSTEM_PROMPT
    # The collapsed rule ("an unmentioned ingredient in a suggestion is an optional
    # addition, not a required ingredient") must not return.
    assert "а не обязательный ингредиент" not in KITCHEN_SYSTEM_PROMPT
    assert "Продукт, о котором не говорили, в предложении блюда" not in KITCHEN_SYSTEM_PROMPT


async def test_confirmed_missing_and_unknown_required_items_go_to_different_sections(tmp_path):
    # Codex FAIL, R3: the partial inventory names carrot as confirmed missing and the
    # rest as unchecked; the shopping-list follow-up must separate the definite purchase
    # from the check-before-buying item at the clause level the model is given.
    backend = ScriptedBackend(
        "Запишите, что моркови нет.",
        "Вот рецепт курицы с рисом.",
        "Купите морковь; остальное проверьте.",
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
    # Confirmed-missing and required: a definite purchase.
    assert "«точно купить» — только подтверждённо отсутствующее" in KITCHEN_SYSTEM_PROMPT
    # Required but unknown: checked, not bought blind, and not called optional.
    assert "«проверить» — нужное, но с неизвестным наличием" in KITCHEN_SYSTEM_PROMPT
    assert "проверьте, есть ли у вас это; купите, если нужно" in KITCHEN_SYSTEM_PROMPT
    # Optional additions are the only conditional ones.
    assert "«если есть» и «можно добавить» — только для необязательных продуктов" in KITCHEN_SYSTEM_PROMPT


async def test_the_ideas_then_recipe_then_favorites_workflow_is_stated_in_both_channels(tmp_path):
    backend = ScriptedBackend("Идеи блюд.", "Полный рецепт.", default="Ответ.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, LIVE_FESTIVE, "k-1")
        await chat.send(conversation, DISH_CHOSEN, "k-2")

    # The recommendation turn and the choice turn are both in the model's history: the
    # full recipe is generated for the chosen dish, not inside the recommendation.
    assert contents(backend.requests[-1][0]) == [
        LIVE_FESTIVE,
        "Идеи блюд.",
        DISH_CHOSEN,
    ]
    # The shared prompt asks for the natural-language offer after a list of dishes.
    assert KITCHEN_SYSTEM_PROMPT.count(RECIPE_OFFER) == 1
    assert "после списка добавь" in KITCHEN_SYSTEM_PROMPT
    # Telegram's help: the workflow and its limits (the save button is not always
    # there; editing is only on the site).
    assert "Если вы не знаете, что приготовить, я предложу идеи блюд" in tc.HELP
    assert "выберите понравившееся, и я дам полный рецепт" in tc.HELP
    assert "кнопкой под ответом помощника, когда она есть" in tc.HELP
    assert "Редактировать" in tc.HELP and "только на сайте" in tc.HELP
    assert tc.HELP in tc.GREETING
    # Web's help: the same workflow in the empty-conversation hint.
    logic = (Path(ui.STATIC_DIR) / "chat-logic.js").read_text(encoding="utf-8")
    assert "Помощник может предложить идеи блюд" in logic
    assert "он даст полный рецепт" in logic
    assert "который можно сохранить в избранное" in logic
