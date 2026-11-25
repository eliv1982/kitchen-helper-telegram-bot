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
from house_helper.web import greeting
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
GREETING_PARTS = (
    "Привет! Я Федя — твой домашний помощник.",
    "Помогу придумать, что приготовить, подобрать рецепт, заменить ингредиенты, "
    "пересчитать порции и составить список покупок по рецепту.",
    "Расскажи, что тебе нужно, — разберемся вместе.",
)
LOGIN_LEAD = " ".join(GREETING_PARTS)
HELP_BODY = (
    "Спроси, что приготовить из имеющихся продуктов или как приготовить блюдо. "
    "Могу предложить идеи блюд: выбери понравившееся — дам полный рецепт, "
    "который можно сохранить в избранное кнопкой рядом с рецептом."
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


def test_the_greeting_module_holds_the_approved_sentences():
    # The expected values above are independent literals; the module must equal them.
    assert greeting.GREETING_PARTS == GREETING_PARTS
    assert greeting.GREETING_TEXT == "\n\n".join(GREETING_PARTS)
    assert greeting.GREETING_TEXT.count(NAME) == 1
    assert "ё" not in " ".join(GREETING_PARTS).lower()


def test_the_shared_prompt_opens_with_the_name_and_carries_the_persona():
    prompt = kitchen_prompt.KITCHEN_SYSTEM_PROMPT

    assert prompt.startswith(
        "Ты — Федя, домашний помощник. Сейчас помогаешь с кухней и готовкой. "
        "Всегда отвечай по-русски, без лишних вступлений."
    )
    assert prompt.count(NAME) == 1
    assert prompt.count(PERSONA) == 1
    assert prompt.index(NAME) < prompt.index(PERSONA)
    # The obsolete formal-address instruction is gone, not inverted.
    assert "на «вы», пока человек сам не перейдёт на «ты»" not in prompt


# --- Telegram ------------------------------------------------------------------------------


def test_the_telegram_greeting_is_the_canonical_greeting_and_keeps_help():
    assert tc.GREETING == "\n\n".join(GREETING_PARTS) + "\n\n" + tc.HELP
    assert tc.GREETING.count(NAME) == 1
    assert tc.HELP in tc.GREETING
    assert "Здравствуйте" not in tc.GREETING


def test_the_telegram_help_speaks_informationally_and_keeps_every_condition():
    assert "напиши, какие продукты есть дома" in tc.HELP
    assert "Если ты не знаешь, что приготовить" in tc.HELP
    assert "выбери понравившееся, и я дам полный рецепт" in tc.HELP
    assert "кнопкой под моим ответом, когда она есть" in tc.HELP
    # FIX-01: lists are edited on the site too, so the sentence says so.
    assert "Редактировать рецепты, заметки и списки покупок можно только на сайте House Helper." in tc.HELP
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

    # The JS source wraps the sentences across string concatenation, so its fragments are
    # checked as they are written; the same words are checked whole through node tests.
    assert '"Привет! Я Федя — твой домашний помощник.",' in logic
    assert '"Помогу придумать, что приготовить, подобрать рецепт, заменить ингредиенты, " +' in logic
    assert '"пересчитать порции и составить список покупок по рецепту.",' in logic
    assert '"Расскажи, что тебе нужно, — разберемся вместе.",' in logic
    assert '"Спроси, что приготовить из имеющихся продуктов или как приготовить блюдо. " +' in logic
    assert '"Могу предложить идеи блюд: выбери понравившееся — дам полный рецепт, " +' in logic
    assert '"который можно сохранить в избранное кнопкой рядом с рецептом.";' in logic
    assert 'replied: "Федя ответил."' in logic
    assert 'preparing: "Федя готовит ответ…"' in logic
    assert 'helpTitle: "Как пользоваться помощником"' in logic  # D-5: unchanged
    # FIX-01: the empty conversation shows the greeting; the help panel shows the help.
    assert "emptyThread: GREETING_PARTS" in logic and "help: HELP_BODY" in logic
    assert "emptyThread: HELP_EXPLANATION" not in logic
    assert "HELP_EXPLANATION" not in logic
    assert logic.count('hint = "Дождись ответа помощника.";') == 2
    assert 'нажми «В избранное»' in logic and 'нажми «Сохранить» рядом с рецептом' in logic
    # The first suggestion is the Owner's replacement; the other two are kept.
    assert '"Что приготовить на ужин?",' in app
    assert '"Как приготовить борщ?",' in app
    assert '"Чем заменить сметану в рецепте?",' in app
    assert "Что приготовить из курицы и риса?" not in app
    assert 'text: mine ? "Вы:" : "Федя, помощник:"' in app
    assert '"Помощник:"' not in app


def test_the_greeting_and_the_functional_help_are_separate_texts():
    logic = _static("chat-logic.js")
    app = _static("app.js")

    # The help body carries the workflow, not the greeting: no «Привет», no introduction.
    assert HELP_BODY.startswith("Спроси, что приготовить из имеющихся продуктов")
    assert "Привет" not in HELP_BODY and "Я Федя" not in HELP_BODY
    assert "идеи блюд" in HELP_BODY and "полный рецепт" in HELP_BODY and "избранное" in HELP_BODY
    # The empty conversation draws every greeting part; the help panel draws TEXT.help.
    assert "Logic.TEXT.emptyThread.forEach" in app
    assert 'h("p", { class: "lead", text: Logic.TEXT.emptyThread })' not in app
    assert "els.helpText.textContent = Logic.TEXT.help;" in app
    # The two strings cannot be the same one any more.
    assert "help: GREETING_PARTS" not in logic


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
