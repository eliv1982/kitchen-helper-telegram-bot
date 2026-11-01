"""Structured recipes through the whole turn: the protocol, the transaction and the API.

No provider is called. The OpenAI client is the same fake the backend tests use; the
turn runner is exercised with a ScriptedBackend that answers with the rendered text and
the validated recipes of one protocol block, which is exactly what the real backend
returns after it has parsed the model's own output. The tests pin:

  * one provider request per turn, and the shared prompt unchanged when the feature is
    off (the default);
  * parse-before-render: the stored text has no markers or JSON, the recipes are carried
    beside it and never extracted from it;
  * one transaction: message, recipes and the completion commit together or not at all;
  * replay: the same key answers from the stored turn without a second generation;
  * the API: one Save control per recipe, identified by (message, position), saving one
    recipe independently of the others;
  * the feature switch: off by default, off sends the old prompt, on adds the protocol;
  * Telegram (the bot and the web-to-Telegram copy) receives the rendered text.

The UI's behaviour (per-recipe controls, the editor, conflicts) is in the Node scenarios.
"""

import json

import openai
import pytest
import sqlalchemy as sa

from house_helper.core.backend import BackendConnectionError, BackendReply
from house_helper.core.identity import ConversationKey
from house_helper.core.kitchen_prompt import KITCHEN_SYSTEM_PROMPT
from house_helper.core.openai_chat import RECIPE_PROTOCOL_PROMPT, StandaloneOpenAIBackend
from house_helper.core.provider_settings import ProviderSettings
from house_helper.core.recipes import (
    RECIPE_END,
    RECIPE_START,
    parse_reply,
    render_parsed_reply,
)
from house_helper.persistence import message_recipes
from house_helper.persistence.models import MessageRecipe
from house_helper.web import ui
from house_helper.web.app import create_app
from house_helper.web.settings import (
    STRUCTURED_RECIPES_ENV,
    WebConfigError,
    WebSettings,
    load_web_settings,
)
from tests.chat_support import ScriptedBackend, chat_app, error_of, post_headers
from tests.telegram_support import MOTHER, telegram_app
from tests.test_openai_chat import FakeClient, response
from tests.web_support import serving, sqlite_url

pytestmark = pytest.mark.asyncio

PROVIDER = ProviderSettings(
    api_key="sk-test-key",
    model="test-model",
    temperature=0.3,
    max_tokens=777,
    timeout_seconds=9.0,
    max_retries=1,
)
CONVERSATION = ConversationKey("web", "0f0f0f0f-0000-4000-8000-000000000001")

BORSH = {
    "title": "Борщ",
    "servings": "4 порции",
    "ingredients": [
        {"name": "свекла", "amount": "2 шт."},
        {"name": "капуста", "amount": "300 г"},
    ],
    "steps": ["Сварите бульон.", "Добавьте овощи."],
}
UKHA = {
    "title": "Уха",
    "servings": None,
    "ingredients": [{"name": "рыба", "amount": "1 кг"}],
    "steps": ["Сварите.", "Дайте настояться."],
}


def block(recipe) -> str:
    return f"{RECIPE_START}\n{json.dumps(recipe, ensure_ascii=False)}\n{RECIPE_END}"


RAW_TWO = "Вот два варианта.\n" + block(BORSH) + "\nИ еще один.\n" + block(UKHA)
RAW_ONE = "Вот рецепт.\n" + block(BORSH) + "\nПриятного аппетита."


def parsed_backend(raw: str) -> ScriptedBackend:
    """The backend the real one is after parsing: rendered text and validated recipes."""
    parsed = parse_reply(raw)
    return ScriptedBackend(
        BackendReply(text=render_parsed_reply(parsed), recipes=parsed.recipes)
    )


async def answered(chat, text="Что приготовить?", key="k-1", client=None):
    conversation = await chat.create(client)
    response_ = await chat.send(conversation, text, key, client)
    assert response_.status_code == 201, response_.text
    return conversation, response_.json()


def stored_messages(chat, conversation):
    return chat.messages(conversation)


def recipe_rows(chat, message_id):
    return chat.rows(
        MessageRecipe,
        MessageRecipe.message_id == message_id,
        order=MessageRecipe.position,
    )


# --- the backend: one call, parse before render -------------------------------------------


async def test_the_protocol_is_off_by_default_and_the_request_is_unchanged():
    client = FakeClient(response("Просто совет."))

    reply = await StandaloneOpenAIBackend(PROVIDER, client=client).complete(
        [{"role": "user", "content": "Что приготовить?"}],
        conversation=CONVERSATION,
        context_len=1,
    )

    (call,) = client.calls
    assert call["messages"][0] == {"role": "system", "content": KITCHEN_SYSTEM_PROMPT}
    assert "response_format" not in call
    assert call["max_tokens"] == PROVIDER.max_tokens
    assert reply.text == "Просто совет." and reply.recipes == ()


async def test_enabled_the_shared_prompt_gets_the_addition_and_still_one_call():
    client = FakeClient(response("Просто совет."))

    await StandaloneOpenAIBackend(
        PROVIDER, client=client, recipe_protocol=True
    ).complete(
        [{"role": "user", "content": "Что приготовить?"}],
        conversation=CONVERSATION,
        context_len=1,
    )

    (call,) = client.calls  # exactly one request
    assert call["messages"][0]["content"] == KITCHEN_SYSTEM_PROMPT + RECIPE_PROTOCOL_PROMPT
    assert call["messages"][0]["content"].startswith(KITCHEN_SYSTEM_PROMPT)
    assert "response_format" not in call
    assert call["max_tokens"] == PROVIDER.max_tokens


async def test_an_answer_with_recipes_is_rendered_and_its_recipes_are_carried():
    client = FakeClient(response(RAW_TWO))

    reply = await StandaloneOpenAIBackend(
        PROVIDER, client=client, recipe_protocol=True
    ).complete(
        [{"role": "user", "content": "Что приготовить?"}],
        conversation=CONVERSATION,
        context_len=1,
    )

    assert RECIPE_START not in reply.text and RECIPE_END not in reply.text
    assert '"title"' not in reply.text
    assert "Вот два варианта." in reply.text
    assert "И еще один." in reply.text
    assert [recipe.title for recipe in reply.recipes] == ["Борщ", "Уха"]
    assert [recipe.index for recipe in reply.recipes] == [0, 1]
    assert [item.name for item in reply.recipes[0].ingredients] == ["свекла", "капуста"]
    assert reply.recipes[1].servings is None


@pytest.mark.parametrize(
    "raw, notice",
    [
        ("Ответ.\n" + RECIPE_START + "\n{не json}\n" + RECIPE_END, "не удалось разобрать"),
        ("Ответ.\n" + RECIPE_START + '\n{"title": "Борщ"', "оборван"),
        ("Ответ.\n" + RECIPE_END + "\nКонец.", None),
    ],
    ids=["invalid", "truncated", "stray-end"],
)
async def test_bad_protocol_never_leaks_json_or_markers(raw, notice):
    reply = await StandaloneOpenAIBackend(
        PROVIDER, client=FakeClient(response(raw)), recipe_protocol=True
    ).complete(
        [{"role": "user", "content": "Что приготовить?"}],
        conversation=CONVERSATION,
        context_len=1,
    )

    assert RECIPE_START not in reply.text and RECIPE_END not in reply.text
    assert '"title"' not in reply.text
    assert reply.recipes == ()
    if notice is not None:
        assert notice in reply.text
    else:
        assert reply.text == "Ответ.\n\nКонец."


# --- the turn: one transaction ------------------------------------------------------------


async def test_a_turn_stores_the_rendered_answer_and_its_recipes(tmp_path):
    backend = parsed_backend(RAW_TWO)
    async with chat_app(tmp_path, backend) as chat:
        conversation, body = await answered(chat)
        history = (await chat.history(conversation)).json()["messages"]

    assistant = body["assistant_message"]
    message_id = assistant["id"]
    rendered = assistant["text"]
    assert RECIPE_START not in rendered and RECIPE_END not in rendered
    assert assistant["recipes"] == [
        {"position": 0, "title": "Борщ"},
        {"position": 1, "title": "Уха"},
    ]
    messages = stored_messages(chat, conversation)
    assert [(message.role, message.content) for message in messages] == [
        ("user", "Что приготовить?"),
        ("assistant", rendered),
    ]
    stored = recipe_rows(chat, message_id)
    assert [(row.position, row.title) for row in stored] == [(0, "Борщ"), (1, "Уха")]
    assert stored[0].ingredients == [
        {"name": "свекла", "amount": "2 шт."},
        {"name": "капуста", "amount": "300 г"},
    ]
    assert stored[1].servings is None
    assert backend.calls == 1

    assert history[1]["recipes"] == [
        {"position": 0, "title": "Борщ"},
        {"position": 1, "title": "Уха"},
    ]
    assert "recipes" not in history[0]


async def test_an_answer_without_recipes_keeps_the_response_shape(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend("Просто совет.")) as chat:
        conversation, body = await answered(chat)
        history = (await chat.history(conversation)).json()["messages"]

    assert "recipes" not in body["assistant_message"]
    assert "recipes" not in history[1]
    assert [row for row in chat.rows(MessageRecipe)] == []


async def test_an_invalid_block_is_not_stored_and_the_person_is_told(tmp_path):
    raw = "Вот.\n" + RECIPE_START + "\n{не json}\n" + RECIPE_END
    backend = parsed_backend(raw)
    async with chat_app(tmp_path, backend) as chat:
        _, body = await answered(chat)

    assert "recipes" not in body["assistant_message"]
    assert "не удалось разобрать" in body["assistant_message"]["text"]
    assert chat.rows(MessageRecipe) == []


async def test_a_replay_answers_from_the_stored_turn_without_generating_or_duplicating(tmp_path):
    backend = parsed_backend(RAW_TWO)
    async with chat_app(tmp_path, backend) as chat:
        conversation, first = await answered(chat, key="same")
        replay = await chat.send(conversation, "Что приготовить?", "same")

    assert replay.status_code == 200
    body = replay.json()
    assert body["replayed"] is True
    assert body["assistant_message"] == first["assistant_message"]
    assert backend.calls == 1
    assert len([m for m in stored_messages(chat, conversation) if m.role == "assistant"]) == 1
    assert len(recipe_rows(chat, first["assistant_message"]["id"])) == 2


async def test_a_retry_after_a_failure_stores_the_recipes_once(tmp_path):
    parsed = parse_reply(RAW_TWO)
    backend = ScriptedBackend(
        BackendConnectionError("the assistant is down"),
        BackendReply(text=render_parsed_reply(parsed), recipes=parsed.recipes),
    )
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        first = await chat.send(conversation, "Что приготовить?", "k-1")
        retry = await chat.send(conversation, "Что приготовить?", "k-1")

    assert first.status_code == 502
    assert retry.status_code == 201
    assert backend.calls == 2  # the failed attempt plus the retry, and nothing else
    assistant = retry.json()["assistant_message"]
    assert assistant["recipes"] == [
        {"position": 0, "title": "Борщ"},
        {"position": 1, "title": "Уха"},
    ]
    [message] = [m for m in stored_messages(chat, conversation) if m.role == "assistant"]
    assert len(recipe_rows(chat, message.id)) == 2


async def test_a_recipe_the_parser_accepted_is_never_refused_for_its_pretty_json_size(tmp_path):
    # A compact block just under the protocol's 20,000-character wire bound: its fields
    # re-spelled with the default JSON separators are over it. The parser accepted the
    # block, so committing the turn must store it, not fail the whole answer.
    large = {
        "title": "Большой рецепт",
        "servings": None,
        "ingredients": [{"name": "н", "amount": "1"}],
        "steps": ["ш" * 992] * 20,
    }
    compact = json.dumps(large, ensure_ascii=False, separators=(",", ":"))
    assert len(compact) <= 20_000 < len(json.dumps(large, ensure_ascii=False))
    raw = "Вот.\n" + RECIPE_START + "\n" + compact + "\n" + RECIPE_END

    async with chat_app(tmp_path, parsed_backend(raw)) as chat:
        _, body = await answered(chat)

    assert body["assistant_message"]["recipes"] == [{"position": 0, "title": "Большой рецепт"}]
    [stored] = recipe_rows(chat, body["assistant_message"]["id"])
    assert len(stored.steps) == 20 and len(stored.steps[0]) == 992


async def test_message_recipes_and_the_completion_commit_together(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise sa.exc.OperationalError("INSERT INTO message_recipes", {}, Exception("disk"))

    monkeypatch.setattr(message_recipes, "record_validated_recipes", explode)
    async with chat_app(tmp_path, parsed_backend(RAW_ONE)) as chat:
        conversation = await chat.create()
        response_ = await chat.send(conversation, "Что приготовить?", "k-1")

    assert response_.status_code == 503
    assert error_of(response_)["detail"] == "service_unavailable"
    messages = stored_messages(chat, conversation)
    assert [(message.role, message.content) for message in messages] == [
        ("user", "Что приготовить?")
    ]
    assert chat.rows(MessageRecipe) == []
    (turn,) = chat.turns(conversation)
    assert (turn.status, turn.assistant_message_id) == ("failed", None)


# --- the API: one Save per recipe ---------------------------------------------------------


async def test_each_recipe_of_one_answer_is_saved_on_its_own(tmp_path):
    backend = parsed_backend(RAW_TWO)
    async with chat_app(tmp_path, backend) as chat:
        conversation, body = await answered(chat)
        message_id = body["assistant_message"]["id"]
        first = await chat.client.post(
            "/api/favorites",
            json={"conversation_id": conversation, "message_id": message_id, "recipe_index": 1},
            headers=post_headers(),
        )
        second = await chat.client.post(
            "/api/favorites",
            json={"conversation_id": conversation, "message_id": message_id, "recipe_index": 0},
            headers=post_headers(),
        )
        again = await chat.client.post(
            "/api/favorites",
            json={"conversation_id": conversation, "message_id": message_id, "recipe_index": 1},
            headers=post_headers(),
        )
        listed = (await chat.client.get("/api/favorites")).json()["favorites"]
        text = await chat.client.post(
            "/api/favorites",
            json={"conversation_id": conversation, "message_id": message_id},
            headers=post_headers(),
        )

    assert (first.status_code, second.status_code) == (201, 201)
    assert (first.json()["title"], second.json()["title"]) == ("Уха", "Борщ")
    assert first.json()["kind"] == "structured" and first.json()["recipe_index"] == 1
    assert (again.status_code, again.json()["created"]) == (200, False)
    assert sorted(item["title"] for item in listed) == ["Борщ", "Уха"]
    assert text.status_code == 201 and text.json()["kind"] == "text"
    # Saving is a copy: no provider request was made for any of it.
    assert backend.calls == 1


async def test_another_person_cannot_save_a_recipe_of_my_answer(tmp_path):
    async with chat_app(tmp_path, parsed_backend(RAW_TWO)) as chat:
        other = await chat.second_user()
        conversation, body = await answered(chat)
        message_id = body["assistant_message"]["id"]

        stolen = await other.post(
            "/api/favorites",
            json={"conversation_id": conversation, "message_id": message_id, "recipe_index": 0},
            headers=post_headers(),
        )

    assert stolen.status_code == 404
    assert error_of(stolen)["detail"] == "conversation_not_found"


async def test_editing_a_structured_recipe_needs_no_provider_call(tmp_path):
    backend = parsed_backend(RAW_ONE)
    async with chat_app(tmp_path, backend) as chat:
        conversation, body = await answered(chat)
        saved = await chat.client.post(
            "/api/favorites",
            json={
                "conversation_id": conversation,
                "message_id": body["assistant_message"]["id"],
                "recipe_index": 0,
            },
            headers=post_headers(),
        )
        favorite = saved.json()
        edited = await chat.client.patch(
            f"/api/favorites/{favorite['id']}",
            json={
                "expected_version": favorite["version"],
                "title": "Борщ по-домашнему",
                "servings": "6 порций",
                "ingredients": [{"name": "свекла", "amount": "3 шт."}],
                "steps": ["Сварите бульон.", "Добавьте овощи.", "Дайте настояться."],
                "notes": "Больше укропа.",
            },
            headers=post_headers(),
        )
        conflict = await chat.client.patch(
            f"/api/favorites/{favorite['id']}",
            json={"expected_version": favorite["version"], "title": "Старая версия"},
            headers=post_headers(),
        )
        removed = await chat.client.delete(
            f"/api/favorites/{favorite['id']}", headers=post_headers()
        )

    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == favorite["version"] + 1
    assert edited.json()["title"] == "Борщ по-домашнему"
    assert conflict.status_code == 409 and error_of(conflict)["detail"] == "favorite_conflict"
    assert removed.status_code == 204
    assert backend.calls == 1


# --- the feature switch --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("off", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("ON", True),
        (" yes ", True),
    ],
)
async def test_the_switch_is_read_from_the_environment(value, expected):
    environment = {} if value is None else {STRUCTURED_RECIPES_ENV: value}

    assert load_web_settings(environment).structured_recipes is expected


async def test_a_value_nobody_meant_is_refused():
    with pytest.raises(WebConfigError, match=STRUCTURED_RECIPES_ENV):
        load_web_settings({STRUCTURED_RECIPES_ENV: "sometimes"})


async def test_the_shell_tells_the_page_whether_controls_exist():
    assert 'data-structured-recipes="0"' in ui.render_shell(telegram=True, email=True)
    assert (
        'data-structured-recipes="1"'
        in ui.render_shell(telegram=True, email=True, structured_recipes=True)
    )


async def test_the_page_served_by_an_enabled_application_says_so(tmp_path):
    settings = WebSettings(
        database_url=sqlite_url(tmp_path / "page.db"), structured_recipes=True
    )
    async with serving(create_app(settings)) as client:
        shell = (await client.get("/")).text

    assert 'data-structured-recipes="1"' in shell


async def test_with_the_switch_off_a_stored_structured_favorite_still_opens_edits_and_removes(
    tmp_path,
):
    # The flag is off (the default here): a recipe stored before it was turned off is not
    # deleted, hidden from the routes or made read-only.
    backend = parsed_backend(RAW_ONE)
    async with chat_app(tmp_path, backend) as chat:
        conversation, body = await answered(chat)
        saved = await chat.client.post(
            "/api/favorites",
            json={
                "conversation_id": conversation,
                "message_id": body["assistant_message"]["id"],
                "recipe_index": 0,
            },
            headers=post_headers(),
        )
        favorite = saved.json()
        read = await chat.client.get(f"/api/favorites/{favorite['id']}")
        edited = await chat.client.patch(
            f"/api/favorites/{favorite['id']}",
            json={"expected_version": favorite["version"], "notes": "Поправка"},
            headers=post_headers(),
        )
        removed = await chat.client.delete(
            f"/api/favorites/{favorite['id']}", headers=post_headers()
        )

    assert (saved.status_code, read.status_code, edited.status_code, removed.status_code) == (
        201,
        200,
        200,
        204,
    )
    assert read.json()["kind"] == "structured" and read.json()["ingredients"]
    assert edited.json()["notes"] == "Поправка"


async def test_the_backend_the_application_builds_obeys_the_switch(tmp_path, monkeypatch):
    built = []

    def build(**kwargs):
        client = FakeClient(response(RAW_ONE), **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", build)
    async with chat_app(
        tmp_path, None, provider=PROVIDER, structured_recipes=True
    ) as chat:
        conversation = await chat.create()
        sent = await chat.send(conversation, "Что приготовить?", "k-1")

    assert sent.status_code == 201, sent.text
    (client,) = built
    (call,) = client.calls
    assert call["messages"][0]["content"] == KITCHEN_SYSTEM_PROMPT + RECIPE_PROTOCOL_PROMPT
    assistant = sent.json()["assistant_message"]
    assert RECIPE_START not in assistant["text"]
    assert assistant["recipes"] == [{"position": 0, "title": "Борщ"}]


async def test_off_by_default_the_application_sends_the_old_prompt(tmp_path, monkeypatch):
    built = []

    def build(**kwargs):
        client = FakeClient(response("Просто совет."), **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", build)
    async with chat_app(tmp_path, None, provider=PROVIDER) as chat:
        conversation = await chat.create()
        sent = await chat.send(conversation, "Что приготовить?", "k-1")

    assert sent.status_code == 201
    (client,) = built
    assert client.calls[0]["messages"][0] == {
        "role": "system",
        "content": KITCHEN_SYSTEM_PROMPT,
    }
    assert "recipes" not in sent.json()["assistant_message"]


# --- Telegram receives the rendered text ---------------------------------------------------


async def test_a_telegram_answer_is_rendered_before_it_is_sent(tmp_path):
    backend = StandaloneOpenAIBackend(
        PROVIDER, client=FakeClient(response(RAW_ONE)), recipe_protocol=True
    )
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Как сварить борщ?")
        await t.settle()

        sent = t.bot.texts(MOTHER)
        [conversation] = t.conversations()
        rows = t.chat.rows(MessageRecipe, order=MessageRecipe.position)

    assert any("Борщ" in text and "Ингредиенты" in text for text in sent)
    assert all(RECIPE_START not in text and RECIPE_END not in text for text in sent)
    assert [row.title for row in rows] == ["Борщ"]
    assert all(RECIPE_START not in message.content for message in t.all_messages())
    assert len(t.stored_texts(conversation.id)) == 2


async def test_a_web_answer_copied_to_telegram_is_rendered_text(tmp_path):
    backend = StandaloneOpenAIBackend(
        PROVIDER, client=FakeClient(response(RAW_TWO)), recipe_protocol=True
    )
    async with telegram_app(tmp_path, backend, structured_recipes=True) as t:
        await t.pair(MOTHER)
        conversation = await t.chat.create()
        sent = await t.chat.send(conversation, "Что приготовить?", "web-1")
        await t.settle()

        copied = t.bot.texts(MOTHER)
        rows = t.chat.rows(MessageRecipe, order=MessageRecipe.position)

    assert sent.status_code == 201
    assert any("Вы (на сайте):" in text for text in copied)
    assert any("Борщ" in text and "Уха" in text for text in copied)
    assert all(RECIPE_START not in text and RECIPE_END not in text for text in copied)
    assert [row.title for row in rows] == ["Борщ", "Уха"]
