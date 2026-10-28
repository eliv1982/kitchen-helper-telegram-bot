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
    assert limits.MAX_HISTORY_MESSAGES == 20
    assert limits.DEFAULT_MAX_OUTPUT_TOKENS == 1000


def test_the_old_names_still_hold_the_shared_values():
    assert context_manager.MAX_HISTORY_MESSAGES == 20
    assert chat_service.DEFAULT_MAX_INPUT_LENGTH == 2000
    assert seams.max_input_length() == 2000  # what the bot's service really enforces
    assert seams.history_capacity() == 20


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
    assert 500 < len(PROMPT) < 4200  # it is sent with every request: it costs tokens (4200: the photo section)
    assert "по-русски" in PROMPT
    cyrillic = sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in PROMPT)
    assert cyrillic > 0.7 * sum(c.isalpha() for c in PROMPT)


@pytest.mark.parametrize(
    ("scenario", "clause"),
    [
        ("cooking with the ingredients a person has", "продукты, которые у него есть"),
        ("not inventing ingredients", "Не выдумывай продуктов"),
        ("cooking a dish a person names", "называет блюдо"),
        ("a person who does not know what to cook", "не знает, что приготовить"),
        ("offering a few options and one question", "2–3 простых варианта и задай один уточняющий вопрос"),
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


def test_the_prompt_does_not_promise_what_the_chat_cannot_do():
    # (A photo can be attached since the Telegram bot and the photo storage: the prompt says how to treat
    # one, below. A voice message, a reminder, a favorite and a saved preference still do not exist for the
    # assistant to offer.)
    lowered = PROMPT.lower()
    for word in ("голос", "напомин", "избранн", "сохран"):
        assert word not in lowered, word


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
