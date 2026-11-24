"""The assistant's identity: one name, one persona, and the literals that must agree.

The expected values are literals on purpose: a test that read them from the module it
tests would pass whatever the module said. The Web files ship no Python, so their
literals are cross-checked here against the same approved contract.
"""

import ast
import inspect
import re
from pathlib import Path

from house_helper.core import assistant_identity, kitchen_prompt
from house_helper.web import telegram_chat as tc
from house_helper.web import ui

ROOT = Path(__file__).resolve().parent.parent
HOUSE_HELPER = ROOT / "house_helper"
STATIC = HOUSE_HELPER / "web" / "static"

NAME = "Федя"
PERSONA = (
    "Общайся на «ты»: дружелюбно, компетентно и по делу. Будь терпеливым и "
    "практичным. Допускается легкий ситуативный юмор. Не выдумывай факты. "
    "Не представляйся без необходимости, не говори о себе в третьем лице. "
    "Соблюдай все функциональные требования и правила безопасности независимо "
    "от стиля общения."
)
LOGIN_LEAD = (
    "Привет! Я Федя — твой помощник по домашней кухне. "
    "Подскажу, что приготовить из того, что есть дома, и расскажу, как приготовить блюдо."
)


def _static(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# --- the module ----------------------------------------------------------------------------


def test_the_name_is_the_approved_one():
    assert assistant_identity.ASSISTANT_NAME == NAME
    assert NAME == NAME.strip()
    assert all(character.isalpha() for character in NAME)  # one Russian word, not a label
    assert "ё" not in NAME.lower()


def test_the_persona_is_the_approved_text_verbatim():
    assert assistant_identity.PERSONA == PERSONA
    assert "ё" not in PERSONA.lower()
    assert PERSONA.strip() == PERSONA


def test_the_identity_module_is_domain_neutral_and_has_no_imports():
    source = (HOUSE_HELPER / "core" / "assistant_identity.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree))
    # A future Equipment, Shopping or Budget prompt composes the same two constants.
    public = {name for name in vars(assistant_identity) if not name.startswith("_")}
    assert public == {"ASSISTANT_NAME", "PERSONA"}


def test_the_persona_is_defined_in_exactly_one_module():
    hits = []
    for path in HOUSE_HELPER.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(target, ast.Name) and target.id == "PERSONA" for target in node.targets):
                hits.append(path.relative_to(ROOT).as_posix())

    assert hits == ["house_helper/core/assistant_identity.py"]


# --- the shared prompt ---------------------------------------------------------------------


def test_the_shared_prompt_opens_with_the_name_and_carries_the_persona():
    prompt = kitchen_prompt.KITCHEN_SYSTEM_PROMPT

    assert prompt.startswith(
        "Ты — Федя, помощник по домашней кухне. "
        "Всегда отвечай по-русски, без лишних вступлений."
    )
    assert prompt.count(NAME) == 1
    assert prompt.count(PERSONA) == 1
    assert prompt.index(NAME) < prompt.index(PERSONA)
    # The obsolete formal-address instruction is gone, not inverted.
    assert "на «вы», пока человек сам не перейдёт на «ты»" not in prompt


# --- Telegram ------------------------------------------------------------------------------


def test_the_telegram_greeting_introduces_the_name_once_and_keeps_help():
    assert tc.GREETING == "Привет! Я Федя — твой помощник в House Helper.\n\n" + tc.HELP
    assert tc.GREETING.count(NAME) == 1
    assert tc.HELP in tc.GREETING
    assert "Здравствуйте" not in tc.GREETING


def test_the_telegram_help_speaks_informationally_and_keeps_every_condition():
    assert "напиши, какие продукты есть дома" in tc.HELP
    assert "Если ты не знаешь, что приготовить" in tc.HELP
    assert "выбери понравившееся, и я дам полный рецепт" in tc.HELP
    assert "кнопкой под моим ответом, когда она есть" in tc.HELP
    # The relay keeps both conditions: before a selection, everything; after, only it.
    assert (
        "Пока в Telegram не выбран разговор, я отвечаю в самом свежем, и все, что ты "
        "пишешь мне на сайте, вместе с моим ответом пересылается сюда; после выбора — "
        "только из выбранного разговора."
    ) in tc.HELP
    assert "/dialogs показывает все твои разговоры" in tc.HELP
    # The command list and the privacy appendix are untouched and still composed in.
    assert tc.HELP.endswith(tc.PRIVACY)
    for command in ("/dialogs — выбрать разговор", "/new — начать новый разговор",
                    "/favorites — избранное", "/unlink — отключить Telegram от аккаунта",
                    "/help — эта подсказка"):
        assert command in tc.HELP
    # No formal address remains in the assistant's own help body. The privacy appendix
    # is deliberately formal (Tier B) and is composed after it untouched.
    body = tc.HELP[: -len(tc.PRIVACY)]
    for old in ("вы не знаете", "выберите", "напишите", "отключите", "ваши", "вам"):
        assert old not in body, old


def test_the_telegram_recipe_and_dialog_copy_match_the_approved_words():
    assert tc.RECIPE_UNAVAILABLE == "Этот рецепт больше недоступен. Попроси меня показать его снова."
    assert tc.UNSUPPORTED == (
        "Пока я понимаю текст и фотографии. Напиши, что хочешь приготовить, или пришли фото."
    )
    assert tc.DIALOGS_HEADER == "🗂 Твои диалоги."
    assert tc.DIALOGS_EMPTY == "Диалогов пока нет. Напиши сообщение, и я начну первый."
    assert tc.DIALOGS_HINT == (
        "Нажми на диалог, чтобы продолжить его в Telegram. "
        "Время — последнего сообщения, по Москве (МСК)."
    )
    sentence = "Продолжай писать — я учту контекст этого разговора."
    assert tc.DIALOG_SELECTED.endswith(sentence)
    assert tc.DIALOG_SELECTED_UNTITLED.endswith(sentence)
    assert "Продолжайте" not in tc.DIALOG_SELECTED + tc.DIALOG_SELECTED_UNTITLED


# --- Web -----------------------------------------------------------------------------------


def test_the_login_lead_names_the_assistant_and_the_platform_stays_house_helper():
    html = ui.render_shell(telegram=True, email=True)

    assert LOGIN_LEAD in html
    assert "Здесь впервые? Открой ссылку-приглашение, которую тебе прислали: она нужна только для первого входа." in html
    assert "<title>House Helper</title>" in html
    assert '<p class="brand">House Helper</p>' in html
    headings = {h.strip() for h in re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.S) if h.strip()}
    assert headings == {"House Helper"}


def test_the_browser_literals_agree_with_the_approved_contract():
    logic = _static("chat-logic.js")
    app = _static("app.js")

    # The JS source wraps the sentence across string concatenation, so its fragments are
    # checked as they are written; the same words are checked whole through node tests.
    assert '"Я Федя. Спроси, что приготовить из имеющихся продуктов или как приготовить блюдо. " +' in logic
    assert '"Могу предложить идеи блюд: выбери понравившееся — дам полный рецепт, " +' in logic
    assert '"который можно сохранить в избранное кнопкой рядом с рецептом.";' in logic
    assert 'replied: "Федя ответил."' in logic
    assert 'preparing: "Федя готовит ответ…"' in logic
    assert 'helpTitle: "Как пользоваться помощником"' in logic  # D-5: unchanged
    assert "emptyThread: HELP_EXPLANATION" in logic and "help: HELP_EXPLANATION" in logic
    assert logic.count('hint = "Дождись ответа помощника.";') == 2
    assert 'нажми «В избранное»' in logic and 'нажми «Сохранить» рядом с рецептом' in logic
    assert 'text: mine ? "Вы:" : "Федя, помощник:"' in app
    assert '"Помощник:"' not in app


def test_the_accessibility_attribution_is_unchanged_except_for_the_name():
    html = ui.render_shell(telegram=True, email=True)
    app = _static("app.js")

    assert 'role="log"' in html  # the message log itself is untouched
    assert 'h("p", { class: "visually-hidden", text: mine ? "Вы:" : "Федя, помощник:" })' in app


def test_the_greeting_is_presentation_and_not_a_stored_message():
    # render_shell draws the shell from flags only; it has no database, conversation or
    # message to write. The greeting exists in the markup, not in anyone's history.
    parameters = list(inspect.signature(ui.render_shell).parameters)
    assert parameters == ["telegram", "email", "max_length", "structured_recipes"]
    assert LOGIN_LEAD in ui.render_shell(telegram=False, email=True)
