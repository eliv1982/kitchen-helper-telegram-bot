"""The limits and the system prompt that Telegram and the web chat share, and the
promise that sharing them changed nothing for Telegram that was not asked for.

The expected values are literals on purpose: a test that read them from the module it
tests would pass whatever the module said.
"""

import api_client
import bot
import config
import context_manager
from house_helper.core import chat_service, kitchen_prompt, limits
from house_helper.core.backend import OpenAIBackend
from house_helper.core.provider_settings import ProviderSettings
from house_helper.web import chat_api, turn_runner

import pytest

from tests import seams
from tests.chat_support import ScriptedBackend, chat_app

# --- the limits ----------------------------------------------------------------------------------


def test_the_shared_limits_have_their_values():
    assert limits.MAX_INPUT_LENGTH == 2000
    assert limits.MAX_HISTORY_MESSAGES == 40  # KH-F07A: was 20
    assert limits.DEFAULT_MAX_OUTPUT_TOKENS == 1000


def test_the_old_names_still_hold_the_shared_values():
    assert context_manager.MAX_HISTORY_MESSAGES == 40
    assert chat_service.DEFAULT_MAX_INPUT_LENGTH == 2000
    assert seams.max_input_length() == 2000  # what the bot's service really enforces
    assert seams.history_capacity() == 40


def test_the_web_chat_reads_the_same_limits_as_the_bot():
    assert chat_api.MAX_INPUT_LENGTH == limits.MAX_INPUT_LENGTH
    assert turn_runner.MAX_HISTORY_MESSAGES == limits.MAX_HISTORY_MESSAGES


def test_the_answer_cap_was_raised_conservatively_and_stays_configurable():
    env = {"BOT_TOKEN": "t", "OPENAI_API_KEY": "k"}

    assert config.load_settings(env).max_tokens == 1000  # was 600
    assert ProviderSettings(api_key="k").max_tokens == 1000
    assert config.load_settings({**env, "MAX_TOKENS": "600"}).max_tokens == 600  # an explicit cap wins
    assert config.load_settings({**env, "MAX_TOKENS": "4096"}).max_tokens == 4096
    # About three characters per Cyrillic token: the default answer still fits in one
    # Telegram message (4096 characters).
    assert 1000 * 3 < 4096


# --- the prompt ----------------------------------------------------------------------------------------


PROMPT = kitchen_prompt.KITCHEN_SYSTEM_PROMPT


def test_the_telegram_provider_layer_uses_the_shared_prompt():
    assert api_client.SYSTEM_MESSAGE is PROMPT


def test_the_prompt_is_russian_and_bounded():
    assert PROMPT == PROMPT.strip()
    # It is sent with every request: it costs tokens. 8462 characters now (the photo
    # section, KH-F07A's ingredient block and KH-MENU-01A's occasion/budget block,
    # including the three-state availability and scoped-restriction rules the Codex
    # audit required, KH-MENU-01A-FIX-01's optional-inventory, two-section shopping-list
    # and recipe/favorites workflow correction, and the Codex-FAIL remediation's removal
    # of the staple exception and separation of necessity from availability); 8550 is
    # the ceiling this test allows. The FIX-01 correction added 1089 characters
    # (+15.6%); the Codex-FAIL remediation added 413 (+5.1%).
    assert 500 < len(PROMPT) < 8550
    assert "по-русски" in PROMPT
    cyrillic = sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in PROMPT)
    assert cyrillic > 0.7 * sum(c.isalpha() for c in PROMPT)


@pytest.mark.parametrize(
    ("scenario", "clause"),
    [
        ("cooking with the ingredients a person has", "продукты, которые у него есть"),
        ("not inventing ingredients", "Не приписывай ему продукты, которых он не называл"),
        ("cooking a dish a person names", "называет блюдо"),
        ("a person who does not know what to cook", "не знает, что приготовить"),
        ("offering a few options and one question", "2–3 подходящих варианта и задай один короткий уточняющий вопрос"),
        ("different tasks do not share a frame", "рамка одной не переносится на другую"),
        ("follow-ups within the conversation", "по ходу разговора"),
        ("what was discussed counts", "с учётом того, что уже обсуждалось"),
        ("adjacent subjects: substitutions", "замена продуктов"),
        ("adjacent subjects: storage", "хранение и заморозка"),
        ("adjacent subjects: answered briefly", "Отвечай на них коротко и по делу"),
        ("a complete recipe", "Рецепт давай целиком"),
        ("with quantities", "ингредиенты с количествами"),
        ("plain text, no Markdown", "без Markdown"),
        ("unrelated: politely, briefly", "коротко и вежливо"),
        ("unrelated: not a rigid refusal", "не повторяй один и тот же отказ"),
        ("no medical or dietary advice", "Не давай медицинских советов"),
        ("see a doctor", "обратиться к врачу"),
        ("no dangerous advice", "Не давай опасных советов"),
        ("a person in distress is not refused", "не отказывай формально"),
        ("a person in distress is pointed to help", "экстренную службу"),
        ("instructions stay private", "не раскрывай"),
        ("a recipe states its portions", "число порций, ингредиенты с количествами"),
        ("times and temperatures in the steps", "с временем и температурой"),
        ("scaling portions and substituting recalculate", "пересчитай количества и время"),
        ("a substitution says what changes", "что изменится во вкусе или текстуре"),
        ("a sensible answer now beats a questionnaire", "отвечай сразу и назови допущения"),
        ("allergies and exclusions are conditions", "об аллергиях, непереносимости, исключённых продуктах"),
        ("stated constraints are not broken", "не нарушай их ни в одном варианте"),
        ("figures are estimates", "пиши «примерно»"),
        ("an estimate is not a measurement", "Не выдавай оценку"),
        ("a recipe is never called verified", "не говори, что рецепт проверен или испытан"),
        ("long-term preserving is treated with caution", "заготовки на долгий срок"),
        ("doneness of meat, poultry, fish and eggs is treated with caution", "готовность мяса, птицы, рыбы и яиц"),
        ("no invented regimes", "не придумывай режимы"),
        ("no safety guarantees", "не гарантируй безопасность"),
        ("an unsure answer points to a proven source", "посоветуй проверенный источник"),
    ],
)
def test_the_prompt_covers(scenario, clause):
    assert clause in PROMPT, scenario


def test_the_prompt_leaves_the_products_name_out_because_two_products_share_it():
    # The bot introduces itself as Kitchen Helper (tests/expected_texts.py) and the web
    # application is House Helper; one text serves both.
    assert "Kitchen Helper" not in PROMPT and "House Helper" not in PROMPT


def test_the_prompt_gives_a_recipe_room_for_its_quantities_and_still_fits_one_telegram_message():
    # "обычно до N символов" is what the model aims at; a Telegram message holds 4096.
    assert "обычно до 2500 символов" in PROMPT
    assert 2500 < 4096


def test_the_prompt_does_not_route_to_domains_that_do_not_exist():
    lowered = PROMPT.lower()
    for word in ("огород", "сад", "garden", "home", "ремонт", "растен"):
        assert word not in lowered, word


def test_the_prompt_promises_only_the_favorites_the_workflow_can_keep():
    # A photo can be attached, so the prompt says how to treat one. A voice message, a
    # reminder and a remembered preference still do not exist for the assistant to offer.
    # Favorites do exist, and KH-MENU-01A-FIX-01 sanctions exactly one mention: after a
    # list of dishes, the person is told that a full recipe can be saved to favorites.
    lowered = PROMPT.lower()
    for word in ("голос", "напомин", "запомн"):
        assert word not in lowered, word
    assert lowered.count("избранн") == 1
    assert "его можно будет сохранить в избранное" in lowered
    # The assistant itself neither saves nor keeps anything.
    for phrase in ("я сохраню", "сохраню в избранное", "сохранит в избранное"):
        assert phrase not in lowered, phrase


@pytest.mark.parametrize(
    ("scenario", "clause"),
    [
        ("a photo is described by what is visible", "Опирайся только на то, что видно"),
        ("food on a photo is the basis for options", "Продукты на фото — основа для вариантов блюд"),
        ("unclear is called unclear", "нечёткое или нечитаемое назови нечётким"),
        ("appliances: only the obvious and safe", "только очевидное и безопасное"),
        ("appliances: no guessing the model, fault or inside", "Не угадывай модель, причину поломки и устройство внутри"),
        ("appliances: no repairs, bypassing, gas or electrics", "не советуй разбирать, чинить, обходить защиту, трогать газ и электрику"),
        ("when unsure: the model, the manual or another photo", "попроси модель прибора, инструкцию или ещё одно фото"),
        ("a risk goes to a repairer", "посоветуй мастера"),
    ],
)
def test_the_prompt_tells_how_to_treat_a_photo(scenario, clause):
    assert clause in PROMPT, scenario


# --- KH-F07A: the ingredients named in the conversation ----------------------------------------------------


INGREDIENT_RULES = [
    ("a working set", "рабочий набор"),
    ("new ingredients add to the earlier ones", "добавляй новые к уже названным"),
    ("an earlier ingredient is not dropped", "не теряй прежние"),
    ("availability has three states", "подтверждено, что продукт есть; подтверждено, что его нет"),
    (
        "an unmentioned ingredient is unknown, not missing",
        "о продукте не говорили — тогда он неизвестен, не считай его ни имеющимся, ни отсутствующим",
    ),
    ("an exhaustive inventory is taken at its word", "это все продукты у меня есть"),
    (
        "the working set is scoped to cooking from what is available",
        "пока задача — готовить из имеющегося",
    ),
    (
        "only confirmed missing ingredients are definite purchases",
        "«точно купить» — только подтверждённо отсутствующее",
    ),
    (
        "required items of unknown availability are in their own check section",
        "«проверить» — нужное, но с неизвестным наличием",
    ),
    (
        "unknown items are checked, not assumed missing",
        "проверьте, есть ли у вас это; купите, если нужно",
    ),
    (
        "necessity and availability are independent",
        "Нужность и наличие — разные вещи",
    ),
    (
        "a dish may require an ingredient the person never mentioned",
        "блюдо может требовать продукт, которого человек не называл",
    ),
    (
        "unknown availability does not make a required ingredient optional",
        "неизвестное наличие не делает обязательный продукт необязательным",
    ),
    (
        "a required item of unknown availability is checked, not optional",
        "Обязательный продукт с неизвестным наличием — в «проверить»",
    ),
    (
        "only optional ingredients are offered conditionally",
        "«если есть» и «можно добавить» — только для необязательных продуктов",
    ),
    (
        "an optional item is not a definite purchase",
        "Необязательное в «точно купить» не попадает",
    ),
    (
        "basic staples are not an exception",
        "Соль, масло, вода и другие базовые запасы — не исключение",
    ),
    (
        "unnamed staples stay unknown",
        "если человек их не называл, их наличие тоже неизвестно",
    ),
    (
        "basic ingredients are not interrogated one by one",
        "Неизвестность не повод допрашивать про каждый базовый продукт",
    ),
    (
        "unknown ingredients are spoken of conditionally",
        "говори о таком условно",
    ),
    (
        "an optional item is not bought merely because it was unmentioned",
        "даже если продукт не называли",
    ),
    ("an explicit removal removes", "«Лука больше нет» — убери лук из набора"),
    ("a substitution replaces for this request", "«рис вместо картофеля» — картофель заменён рисом"),
    ("a substitution is recalculated", "количества пересчитай"),
    ("quantities are limits", "Названное количество — предел"),
    (
        "a hypothetical ingredient is not available",
        "Предполагаемое («если бы был», «что если купить») не считай имеющимся",
    ),
    ("the latest correction wins", "Последнее явное уточнение человека важнее прежних слов"),
    ("a suggested recipe consumes nothing", "не считай их съеденными оттого, что ты предложил рецепт"),
    (
        "a recipe's ingredients are not evidence of availability",
        "в рецепте — то, что нужно для блюда, а не подтверждение, что оно есть",
    ),
    ("missing information is asked for, not pretended", "а не делай вид, что помнишь"),
    (
        "preferences stated in this conversation are kept",
        "Сказанное о предпочтениях учитывай в этом разговоре",
    ),
    (
        "only persistent culinary preferences are missing",
        "постоянных кулинарных предпочтений приложение пока не хранит",
    ),
    ("no future memory is promised", "не обещай помнить это после него"),
    ("availability is never invented", "Не выдумывай наличие продуктов"),
]


@pytest.mark.parametrize(("rule", "clause"), INGREDIENT_RULES)
def test_the_prompt_covers_ingredients_named_in_the_conversation(rule, clause):
    assert clause in PROMPT, rule


def test_the_ingredient_block_appears_once_and_stays_compact():
    header = "Продукты, которые уже назывались:"
    assert PROMPT.count(header) == 1
    start = PROMPT.index(header)
    end = PROMPT.index("Праздник, гости и бюджет:", start)
    # It is sent with every request: it stays short. It grew with KH-MENU-01A's
    # three-state availability rule (R2), which replaced the unmentioned-means-missing
    # wording, with KH-MENU-01A-FIX-01's two-section shopping list, and with the
    # Codex-FAIL remediation's necessity/availability split and removal of the staple
    # exception; the bound moved from 1500 to 1700 and then to 2050.
    assert 500 < end - start < 2050


def test_the_ingredient_block_does_not_weaken_the_safety_or_recipe_rules():
    # The block is an addition; the sections that were there stay there.
    assert "Границы:" in PROMPT and "Не давай опасных советов" in PROMPT
    assert "Рецепт давай целиком" in PROMPT and "без Markdown" in PROMPT


def test_the_ingredient_rules_are_in_the_one_prompt_both_channels_send():
    from house_helper.core import openai_chat

    assert openai_chat.KITCHEN_SYSTEM_PROMPT is PROMPT  # the web backend
    assert api_client.SYSTEM_MESSAGE is PROMPT  # the Telegram adapter
    start = PROMPT.index("Продукты, которые уже назывались:")
    block = PROMPT[start : PROMPT.index("Праздник, гости и бюджет:", start)]
    assert "не теряй прежние" in block and "Лука больше нет" in block


# --- KH-MENU-01A: festive and special-occasion recommendations ---------------------------------------------


MENU_RULES = [
    (
        "a festive request is a choice of dishes, not a restriction to what was named",
        "выбор блюд, а не «только из перечисленного»",
    ),
    (
        "the named products are an optional resource, not a base or a boundary",
        "необязательный ресурс, а не основа и не граница",
    ),
    ("missing products may be suggested for purchase", "недостающее можно предложить докупить"),
    ("an explicit restriction to what is available is honoured", "только из этого"),
    ("budget requests get real dishes from inexpensive products", "реальные блюда из недорогих продуктов"),
    ("cheaper substitutes are offered", "более дешёвые замены"),
    ("no current or exact prices are invented", "Цены не выдумывай"),
    ("recommendations are 2-4 concrete different dishes", "2–4 конкретных разных блюда"),
    ("generic dish categories are ruled out", "общие категории"),
    ("a full recipe follows the choice of a dish", "Полный рецепт — когда человек выбрал блюдо"),
    ("an explicit no-shopping condition is binding", "«Ничего не покупать»"),
    ("a no-shopping restriction is limited to its stated frame", "запрет покупок в названных рамках"),
    ("a task-scoped restriction does not outlive its task", "Вне их он не действует"),
    ("a period restriction lasts until the period ends", "на срок — до его конца"),
    (
        "an indefinite restriction lasts until changed",
        "бессрочный — пока человек его не отменит или не заменит",
    ),
    (
        "a change of task does not cancel an ongoing restriction",
        "смена задачи не отменяет запрет на срок или бессрочный",
    ),
    (
        "explicit permission supersedes within its scope",
        "Разрешение докупить для текущей задачи снимает прежний запрет",
    ),
    ("materially ambiguous scope gets one short question", "Если рамки непонятны, задай один короткий вопрос"),
    (
        "a later occasion request does not turn an inventory into a restriction",
        "не сужай выбор до имеющегося",
    ),
    (
        "the non-inventory variety applies only when shopping is not prohibited",
        "Если покупки не запрещены, не сужай выбор до имеющегося",
    ),
    (
        "at least one dish is not built on the previous inventory",
        "минимум один вариант должен быть не из прежних продуктов",
    ),
    (
        "an explicit opt-out removes the inventory anchoring",
        "Если человек прямо сказал, что имеющееся использовать необязательно",
    ),
    (
        "an explicit opt-out keeps the inventory out of every dish",
        "не подставляй его в каждый вариант",
    ),
    (
        "useful available products are not artificially excluded",
        "не исключай прежние продукты нарочно",
    ),
    (
        "varied dishes are not variations of one dish",
        "не вариации одного блюда",
    ),
    (
        "the recipe and favorites offer follows the recommendations",
        "Выберите понравившееся блюдо, и я дам полный рецепт",
    ),
    (
        "the shopping-list follow-up has the two sections",
        "с разделами «точно купить» и «проверить»",
    ),
    ("a shopping-list follow-up names what is missing", "список покупок — недостающее"),
    ("a question is asked only when it is necessary", "только если без него нельзя ответить по делу"),
    ("what the person already said is not asked again", "не спрашивай то, что уже сказано"),
    (
        "only persistent preferences are missing",
        "постоянных кулинарных предпочтений приложение пока не хранит",
    ),
    ("no future memory is promised", "не обещай помнить это после него"),
    ("ordinary cooking from what is available still uses it", "предложи, что из них можно приготовить"),
]


@pytest.mark.parametrize(("rule", "clause"), MENU_RULES)
def test_the_prompt_covers_festive_and_budget_recommendations(rule, clause):
    assert clause in PROMPT, rule


def test_the_prompt_does_not_restore_the_audited_wording():
    # R1: the restriction must not be made conversation-wide again.
    assert "пока человек сам не отменит это" not in PROMPT
    assert "Явное «ничего не покупать» при этом остаётся в силе" not in PROMPT
    # R2: an unmentioned ingredient must not be called a required purchase, and the
    # purchase permission must not be duplicated in the ingredient block.
    assert "скажи, что его нужно купить" not in PROMPT
    assert "не запрещает предлагать докупить" not in PROMPT
    # R3: the account exists; only persistent culinary preferences are missing.
    assert "у тебя нет профиля" not in PROMPT
    # The Codex-FAIL remediation (R1): the staple exception must not return in any
    # form -- neither as availability nor as a stated assumption.
    assert "можно исходить" not in PROMPT
    assert "назови это допущением" not in PROMPT
    assert "допущения (порции, базовые запасы)" not in PROMPT
    # The Codex-FAIL remediation (R3): an unmentioned ingredient is not automatically
    # an optional addition, and the old necessity/availability collapse must not return.
    assert "а не обязательный ингредиент" not in PROMPT
    assert "Продукт, о котором не говорили, в предложении блюда" not in PROMPT


def test_the_live_inventory_anchoring_wording_is_gone():
    # KH-MENU-01A-FIX-01: the live failure built every festive dish on the inventory
    # because the prompt called the named products a base. The anchoring wording must
    # not return; they are an optional resource when shopping is allowed.
    assert "названные продукты — основа" not in PROMPT
    assert "названные продукты — необязательный ресурс" in PROMPT
    assert "минимум один вариант должен быть не из прежних продуктов" in PROMPT


def test_the_recipe_choice_and_favorites_offer_is_one_exact_line():
    # KH-MENU-01A-FIX-01's UX correction: after a list of dishes, the assistant invites
    # the person to choose one for a full recipe that can be saved to favorites.
    offer = (
        "Выберите понравившееся блюдо, и я дам полный рецепт. "
        "Его можно будет сохранить в избранное"
    )
    assert PROMPT.count(offer) == 1
    assert "Когда предлагаешь несколько блюд на выбор, после списка добавь" in PROMPT


def test_the_occasion_block_appears_once_and_is_in_the_one_shared_prompt():
    from house_helper.core import openai_chat

    header = "Праздник, гости и бюджет:"
    assert PROMPT.count(header) == 1
    assert openai_chat.KITCHEN_SYSTEM_PROMPT is PROMPT
    assert api_client.SYSTEM_MESSAGE is PROMPT
    start = PROMPT.index(header)
    block = PROMPT[start : PROMPT.index("Как отвечать:", start)]
    # Sent with every request: it stays compact. KH-MENU-01A-FIX-01 moved the bound
    # from 1600 to 1800 for the optional-inventory and variety correction; the
    # Codex-FAIL remediation moved it to 1950 for the new-main-ingredient rule.
    assert 500 < len(block) < 1950
    assert "2–4 конкретных разных блюда" in block
    assert "никаких текущих цен и точных сумм" in block


# --- Telegram is unchanged ---------------------------------------------------------------------------------


def test_the_bot_still_answers_through_the_openai_adapter_it_always_used():
    assert type(bot._chat_service._backend) is OpenAIBackend


def test_the_telegram_backend_reply_is_unchanged_for_every_existing_caller():
    from house_helper.core.backend import BackendReply

    reply = BackendReply(text="x", usage={"input_tokens": 1})

    assert reply.truncated is False
    assert reply == BackendReply(text="x", usage={"input_tokens": 1}, truncated=False)


@pytest.mark.asyncio
async def test_a_web_conversation_never_touches_the_telegram_state(tmp_path):
    seams.reset_state()
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "привет", "k-1")
        await chat.history(conversation)

    assert seams.stored_conversation_keys() == set()  # no history, no lock: nothing of the web
    assert context_manager._locks == {}


@pytest.mark.asyncio
async def test_a_telegram_conversation_and_a_web_conversation_do_not_share_history(tmp_path):
    from tests.helpers import FakeBackend, FakeBot, FakeMessage

    seams.reset_state()
    telegram_backend = FakeBackend(reply="ответ в телеграме")
    async with chat_app(tmp_path, ScriptedBackend("ответ в вебе")) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "вопрос в вебе", "k-1")
        mp = pytest.MonkeyPatch()
        try:
            seams.install_backend(mp, telegram_backend)
            await seams.process_text(FakeMessage(user_id=7, text="вопрос в телеграме"), FakeBot())
        finally:
            mp.undo()

        assert telegram_backend.requests[0][0] == [{"role": "user", "content": "вопрос в телеграме"}]
        assert [m.content for m in chat.messages(conversation)] == ["вопрос в вебе", "ответ в вебе"]
        assert seams.history(7) == [
            {"role": "user", "content": "вопрос в телеграме"},
            {"role": "assistant", "content": "ответ в телеграме"},
        ]
