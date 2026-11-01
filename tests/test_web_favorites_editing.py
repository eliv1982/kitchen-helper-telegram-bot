"""Structured favorites and editing through the API: the contract old clients keep.

The assistant is a ScriptedBackend and the conversations are real ones made through the
chat API. The turn runner does not extract recipes in this stage (KH-F06B), so the tests
seed message_recipes through the supported persistence writer, from the text the answer
was stored with. Nothing reaches a provider.
"""

import json
import logging
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from house_helper.core.recipes import (
    RECIPE_END,
    RECIPE_START,
    Ingredient,
    Recipe,
    render_recipe,
)
from house_helper.persistence import favorites as favorites_module
from house_helper.persistence import message_recipes
from house_helper.persistence.models import FavoriteRecipe, Message, MessageRecipe
from house_helper.web.diagnostics import CATALOGUE
from tests.auth_support import ORIGIN, logout_as
from tests.chat_support import ScriptedBackend, chat_app, error_of, log_text, post_headers

pytestmark = pytest.mark.asyncio

BORSH = {
    "title": "Борщ",
    "servings": "4 порции",
    "ingredients": [{"name": "свёкла", "amount": "2 шт."}, {"name": "капуста", "amount": "300 г"}],
    "steps": ["Сварите бульон.", "Добавьте овощи."],
}
UKHA = {
    "title": "Уха",
    "servings": None,
    "ingredients": [{"name": "рыба", "amount": "1 кг"}],
    "steps": ["Сварите.", "Дайте настояться."],
}


def block(recipe):
    return f"{RECIPE_START}\n{json.dumps(recipe, ensure_ascii=False)}\n{RECIPE_END}"


ANSWER = "Вот два варианта.\n" + block(BORSH) + "\nИ еще один.\n" + block(UKHA)


@contextmanager
def statements_of(harness):
    seen: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        seen.append(statement.lstrip().split(None, 1)[0].upper())

    engine = harness.app.state.database._engine
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)


async def answered(chat, question="Что приготовить?", key="k-1", client=None):
    conversation = await chat.create(client)
    response = await chat.send(conversation, question, key, client)
    assert response.status_code == 201, response.text
    return conversation, response.json()["user_message"]["id"], response.json()["assistant_message"]["id"]


def stored_answer(chat, message_id):
    with chat.base.factory() as db:
        return db.get(Message, message_id).content


def seed(chat, message_id, text=None):
    """Store the recipes of the answer as the turn runner will in KH-F06C."""
    with chat.base.factory() as db, db.begin():
        message_recipes.record_recipes_for_message(
            db, message_id, text if text is not None else stored_answer(chat, message_id), now=chat.clock()
        )


async def keep(chat, conversation, message, client=None, *, recipe_index=None):
    body = {"conversation_id": conversation, "message_id": message}
    if recipe_index is not None:
        body["recipe_index"] = recipe_index
    return await (client or chat.client).post("/api/favorites", json=body, headers=post_headers())


async def keep_raw(chat, body, client=None):
    return await (client or chat.client).post("/api/favorites", json=body, headers=post_headers())


async def patch(chat, favorite_id, document, client=None):
    return await (client or chat.client).patch(
        f"/api/favorites/{favorite_id}", json=document, headers=post_headers()
    )


async def listed(chat, client=None):
    response = await (client or chat.client).get("/api/favorites")
    assert response.status_code == 200, response.text
    return response.json()["favorites"]


def rows(chat):
    return chat.rows(FavoriteRecipe)


def recipe_rows(chat, message_id):
    return chat.rows(
        MessageRecipe,
        MessageRecipe.message_id == message_id,
        order=MessageRecipe.position,
    )


async def structured_favorite(chat):
    conversation, _, answer = await answered(chat)
    seed(chat, answer)
    response = await keep(chat, conversation, answer, recipe_index=0)
    assert response.status_code == 201, response.text
    return conversation, answer, response.json()


def expected_text(title, servings, ingredients, steps, notes=None):
    body = render_recipe(
        Recipe(
            index=0,
            title=title,
            servings=servings,
            ingredients=tuple(Ingredient(item["name"], item["amount"]) for item in ingredients),
            steps=tuple(steps),
        )
    )
    return body if not notes else f"{body}\n\n{favorites_module.NOTES_HEADING}\n{notes}"


# --- saving one recipe ----------------------------------------------------------------------


async def test_one_recipe_of_a_multi_recipe_answer_is_saved(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)
        seed(chat, answer)

        response = await keep(chat, conversation, answer, recipe_index=1)

        body = response.json()
        assert response.status_code == 201
        assert set(body) == {
            "id", "title", "message_id", "created_at", "kind", "recipe_index", "version", "created",
        }
        assert body["created"] is True
        assert (body["kind"], body["recipe_index"], body["version"]) == ("structured", 1, 1)
        assert body["title"] == "Уха" and body["message_id"] == answer
        (row,) = rows(chat)
        assert row.kind == "structured" and row.source_recipe_index == 1
        assert row.ingredients == [{"name": "рыба", "amount": "1 кг"}]
        assert row.steps == ["Сварите.", "Дайте настояться."]
        assert row.content == expected_text(
            "Уха", None, [{"name": "рыба", "amount": "1 кг"}], ["Сварите.", "Дайте настояться."]
        )
        detail = (await chat.client.get(f"/api/favorites/{body['id']}")).json()
        assert detail["ingredients"] == [{"name": "рыба", "amount": "1 кг"}]
        assert detail["steps"] == ["Сварите.", "Дайте настояться."]
        assert detail["servings"] is None and detail["notes"] is None
        assert [item["id"] for item in await listed(chat)] == [body["id"]]


async def test_saving_the_same_recipe_again_is_harmless(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)
        seed(chat, answer)
        first = await keep(chat, conversation, answer, recipe_index=0)
        chat.clock.advance(hours=2)

        again = await keep(chat, conversation, answer, recipe_index=0)

        assert again.status_code == 200
        assert again.json() == {**first.json(), "created": False}
        assert len(rows(chat)) == 1


async def test_a_position_the_answer_does_not_have_is_recipe_not_found(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)
        seed(chat, answer)

        missing = await keep(chat, conversation, answer, recipe_index=2)
        unseeded, _, plain = await answered(chat, key="k-2")
        no_recipe = await keep(chat, unseeded, plain, recipe_index=0)

        for response in (missing, no_recipe):
            assert response.status_code == 404
            assert error_of(response)["detail"] == "recipe_not_found"
            assert error_of(response)["message"] == CATALOGUE["recipe_not_found"].message
        assert rows(chat) == []


@pytest.mark.parametrize("index", [-1, 5, True, "0", 1.0, None, [0]])
async def test_recipe_index_outside_the_protocol_is_a_bad_request(tmp_path, index):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)
        seed(chat, answer)

        response = await keep_raw(
            chat,
            {"conversation_id": conversation, "message_id": answer, "recipe_index": index},
        )

        assert response.status_code == 400
        assert error_of(response)["detail"] == "invalid_request"
        assert rows(chat) == []


async def test_an_old_client_body_still_saves_the_text_favorite(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)

        response = await keep(chat, conversation, answer)

        assert response.status_code == 201
        body = response.json()
        assert (body["kind"], body["recipe_index"], body["version"]) == ("text", -1, 1)
        (row,) = rows(chat)
        assert row.kind == "text" and row.content == stored_answer(chat, answer)


async def test_a_second_recipe_of_one_answer_is_a_second_favorite(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)
        seed(chat, answer)

        first = await keep(chat, conversation, answer, recipe_index=0)
        second = await keep(chat, conversation, answer, recipe_index=1)
        whole = await keep(chat, conversation, answer)

        assert (first.status_code, second.status_code, whole.status_code) == (201, 201, 201)
        assert len({first.json()["id"], second.json()["id"], whole.json()["id"]}) == 3
        assert {item["title"] for item in await listed(chat)} == {
            "Борщ", "Уха", "Вот два варианта.",
        }


# --- editing ------------------------------------------------------------------------------------


async def test_a_structured_favorite_is_edited_field_by_field(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, answer, favorite = await structured_favorite(chat)
        before_recipes = [(row.position, row.title) for row in recipe_rows(chat, answer)]
        message_before = stored_answer(chat, answer)

        response = await patch(chat, favorite["id"], {
            "expected_version": 1,
            "title": "Мой борщ",
            "servings": "2 порции",
            "ingredients": [{"name": "морковь", "amount": "1 шт."}],
            "steps": ["Готовьте иначе.", "Подавайте."],
            "notes": "Помнить про сметану.",
        })

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["version"] == 2
        assert body["title"] == "Мой борщ" and body["servings"] == "2 порции"
        assert body["ingredients"] == [{"name": "морковь", "amount": "1 шт."}]
        assert body["steps"] == ["Готовьте иначе.", "Подавайте."]
        assert body["notes"] == "Помнить про сметану."
        assert body["text"] == expected_text(
            "Мой борщ",
            "2 порции",
            [{"name": "морковь", "amount": "1 шт."}],
            ["Готовьте иначе.", "Подавайте."],
            notes="Помнить про сметану.",
        )
        assert (body["kind"], body["recipe_index"], body["created_at"]) == (
            "structured", 0, favorite["created_at"],
        )
        assert [(row.position, row.title) for row in recipe_rows(chat, answer)] == before_recipes
        assert stored_answer(chat, answer) == message_before


async def test_a_text_favorite_is_edited_as_text(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()

        response = await patch(chat, favorite["id"], {
            "expected_version": 1,
            "title": "Мой заголовок",
            "text": "  Новый текст, ровно как введен.  ",
            "notes": "Личная заметка.",
        })

        assert response.status_code == 200
        body = response.json()
        assert (body["kind"], body["version"]) == ("text", 2)
        assert body["title"] == "Мой заголовок"
        assert body["text"] == "  Новый текст, ровно как введен.  "
        assert body["notes"] == "Личная заметка."
        assert body["servings"] is None and body["ingredients"] is None and body["steps"] is None


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"title": "x"},
        {"expected_version": 1},
        {"expected_version": 0, "title": "x"},
        {"expected_version": True, "title": "x"},
        {"expected_version": "1", "title": "x"},
        {"expected_version": None, "title": "x"},
        {"expected_version": 1.5, "title": "x"},
        {"expected_version": 2**31, "title": "x"},
        {"expected_version": 10**100, "title": "x"},
        {"expected_version": 1, "kind": "text"},
        {"expected_version": 1, "message_id": 1},
        {"expected_version": 1, "recipe_index": 0},
        {"expected_version": 1, "created_at": "2026-01-01T00:00:00Z"},
        {"expected_version": 1, "user_id": "00000000-0000-0000-0000-000000000000"},
    ],
)
async def test_an_edit_needs_a_version_and_only_editable_fields(tmp_path, document):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        _, _, favorite = await structured_favorite(chat)

        response = await patch(chat, favorite["id"], document)

        assert response.status_code == 400
        assert error_of(response)["detail"] == "invalid_request"
        detail = (await chat.client.get(f"/api/favorites/{favorite['id']}")).json()
        assert detail["version"] == 1 and detail["title"] == "Борщ"


async def test_a_stale_version_is_a_conflict_and_changes_nothing(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        _, _, favorite = await structured_favorite(chat)
        first = await patch(chat, favorite["id"], {"expected_version": 1, "title": "Первый"})
        assert first.status_code == 200

        stale = await patch(chat, favorite["id"], {"expected_version": 1, "title": "Устаревший"})

        assert stale.status_code == 409
        error = error_of(stale)
        assert error["detail"] == "favorite_conflict"
        assert error["message"] == CATALOGUE["favorite_conflict"].message
        assert error["retryable"] is False
        found = (await chat.client.get(f"/api/favorites/{favorite['id']}")).json()
        assert (found["title"], found["version"]) == ("Первый", 2)


async def test_the_fields_of_the_other_kind_are_refused(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)
        seed(chat, answer)
        text = (await keep(chat, conversation, answer)).json()
        structured = (await keep(chat, conversation, answer, recipe_index=0)).json()

        for changes in (
            {"servings": None},
            {"ingredients": [{"name": "x", "amount": "1"}]},
            {"steps": ["s"]},
        ):
            response = await patch(chat, text["id"], {"expected_version": 1, **changes})
            assert response.status_code == 400, changes
        wrong = await patch(chat, structured["id"], {"expected_version": 1, "text": "нельзя"})

        assert wrong.status_code == 400
        assert (await chat.client.get(f"/api/favorites/{text['id']}")).json()["version"] == 1
        assert (await chat.client.get(f"/api/favorites/{structured['id']}")).json()["version"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"title": ""},
        {"title": "x" * 201},
        {"title": "a\nb"},
        {"text": ""},
        {"text": "   "},
        {"notes": "x" * 2001},
        {"ingredients": []},
        {"ingredients": [{"name": "x"}]},
        {"ingredients": "x"},
        {"steps": []},
        {"steps": [1]},
        {"servings": 4},
        {"servings": "x" * 101},
    ],
)
async def test_an_invalid_value_is_a_bad_request_and_writes_nothing(tmp_path, changes):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)
        seed(chat, answer)
        text = (await keep(chat, conversation, answer)).json()
        structured = (await keep(chat, conversation, answer, recipe_index=0)).json()
        target = text if any(name in changes for name in ("text",)) else structured

        response = await patch(chat, target["id"], {"expected_version": 1, **changes})

        assert response.status_code == 400, (changes, response.text)
        assert error_of(response)["detail"] == "invalid_request"
        found = (await chat.client.get(f"/api/favorites/{target['id']}")).json()
        assert found["version"] == 1


async def test_another_person_cannot_edit_or_read_the_favorite(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        other = await chat.second_user()
        _, _, favorite = await structured_favorite(chat)

        edited = await patch(chat, favorite["id"], {"expected_version": 1, "title": "чужое"}, client=other)
        read = await other.get(f"/api/favorites/{favorite['id']}")
        malformed = await patch(chat, "not-a-uuid", {"expected_version": 1, "title": "x"}, client=other)

        for response in (edited, read, malformed):
            assert response.status_code == 404
            assert error_of(response)["detail"] == "favorite_not_found"
        assert (await chat.client.get(f"/api/favorites/{favorite['id']}")).json()["title"] == "Борщ"


async def test_a_removed_structured_favorite_can_be_saved_again(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, answer, favorite = await structured_favorite(chat)
        removed = await chat.client.delete(f"/api/favorites/{favorite['id']}", headers=post_headers())
        assert removed.status_code == 204

        again = await keep(chat, conversation, answer, recipe_index=0)

        assert again.status_code == 201 and again.json()["id"] != favorite["id"]


async def test_the_limit_counts_text_and_structured_together(tmp_path, monkeypatch):
    monkeypatch.setattr(favorites_module, "FAVORITES_LIMIT", 2)
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)
        seed(chat, answer)
        assert (await keep(chat, conversation, answer)).status_code == 201
        assert (await keep(chat, conversation, answer, recipe_index=0)).status_code == 201

        full = await keep(chat, conversation, answer, recipe_index=1)
        repeated = await keep(chat, conversation, answer, recipe_index=0)

        assert full.status_code == 409 and error_of(full)["detail"] == "favorites_full"
        assert repeated.status_code == 200 and repeated.json()["created"] is False
        assert len(rows(chat)) == 2


# --- protections and observability ---------------------------------------------------------------


async def test_editing_needs_a_session(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER), sign_in_first=False) as chat:
        response = await patch(chat, "00000000-0000-0000-0000-000000000000", {"expected_version": 1, "title": "x"})

        assert response.status_code == 401
        assert error_of(response)["detail"] == "not_authenticated"


async def test_a_signed_out_session_cannot_edit(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        _, _, favorite = await structured_favorite(chat)
        out = await chat.client.post("/auth/logout", headers=post_headers(**logout_as(chat.user_id)))
        assert out.status_code == 204

        response = await patch(chat, favorite["id"], {"expected_version": 1, "title": "x"})

        assert response.status_code == 401
        assert (await chat.client.get(f"/api/favorites/{favorite['id']}")).status_code == 401


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://kitchen.example.test"])
async def test_an_edit_from_another_origin_is_refused(tmp_path, origin):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        _, _, favorite = await structured_favorite(chat)

        response = await chat.client.patch(
            f"/api/favorites/{favorite['id']}",
            json={"expected_version": 1, "title": "x"},
            headers={"Origin": origin},
        )

        assert response.status_code == 403
        assert error_of(response)["detail"] == "forbidden_origin"
        assert (await chat.client.get(f"/api/favorites/{favorite['id']}")).json()["version"] == 1


async def test_every_edit_response_is_uncacheable_and_has_a_correlation_id(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        _, _, favorite = await structured_favorite(chat)
        responses = [
            await patch(chat, favorite["id"], {"expected_version": 1, "title": "x"}),
            await patch(chat, favorite["id"], {"expected_version": 1, "title": "y"}),
            await patch(chat, favorite["id"], {"expected_version": 3, "title": ""}),
        ]

        for response in responses:
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-correlation-id"].startswith("hh-")


async def test_favorite_operations_make_no_provider_call(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, _, answer = await answered(chat)
        seed(chat, answer)
        calls = chat.backend.calls
        text = (await keep(chat, conversation, answer)).json()
        structured = (await keep(chat, conversation, answer, recipe_index=0)).json()
        await listed(chat)
        await chat.client.get(f"/api/favorites/{structured['id']}")
        await patch(chat, text["id"], {"expected_version": 1, "title": "x", "notes": "n"})
        await patch(chat, structured["id"], {"expected_version": 1, "servings": "2 порции"})
        await chat.client.delete(f"/api/favorites/{text['id']}", headers=post_headers())
        await chat.client.delete(f"/api/favorites/{structured['id']}", headers=post_headers())

        assert chat.backend.calls == calls


async def test_editing_writes_only_the_favorite(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        conversation, answer, favorite = await structured_favorite(chat)
        before_message = stored_answer(chat, answer)
        before_recipes = [(row.position, row.title, row.ingredients, row.steps) for row in recipe_rows(chat, answer)]

        with statements_of(chat) as seen:
            response = await patch(chat, favorite["id"], {"expected_version": 1, "title": "x"})

        assert response.status_code == 200
        assert {"INSERT", "DELETE"} & set(seen) == set()
        assert "UPDATE" in seen
        assert stored_answer(chat, answer) == before_message
        assert [(row.position, row.title, row.ingredients, row.steps) for row in recipe_rows(chat, answer)] == before_recipes


async def test_the_edit_of_a_recipe_is_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with chat_app(tmp_path, ScriptedBackend(default=ANSWER)) as chat:
        _, _, favorite = await structured_favorite(chat)
        await patch(chat, favorite["id"], {"expected_version": 1, "notes": "секретная заметка"})
        await patch(chat, favorite["id"], {"expected_version": 1, "notes": "еще одна"})
        await patch(chat, favorite["id"], {"expected_version": 1, "title": ""})

    logged = log_text(caplog)
    assert "секретная" not in logged and "Борщ" not in logged and "свёкла" not in logged
    assert "code=favorite_conflict" in logged and "code=invalid_request" in logged
