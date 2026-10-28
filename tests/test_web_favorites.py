"""The favorites API's contract: who may call it, what it accepts, what it answers, and that
what a person kept does not depend on the history it was copied from.

The assistant is always a ScriptedBackend; nothing reaches a provider. The conversations
are real ones, made through the chat API, so a favorite is kept from a real answer.
"""

import json
import logging
import re
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from house_helper.persistence import favorites as favorites_module
from house_helper.persistence.models import FavoriteRecipe, Message, User
from house_helper.web.diagnostics import CATALOGUE
from tests.auth_support import ORIGIN, logout_as
from tests.chat_support import ScriptedBackend, chat_app, error_of, log_text, post_headers

pytestmark = pytest.mark.asyncio

RECIPE = "Омлет с сыром\n\nПорции: 2.\n- 3 яйца\n- 50 г сыра\n\n1. Взбейте яйца.\n2. Обжарьте 3 минуты <i>примерно</i>."
CORRELATION_ID = re.compile(r"hh-[0-9a-f]{16}")


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


async def answered(chat, question="Что приготовить из яиц?", key="k-1", client=None, conversation=None):
    """(conversation id, the user's message id, the assistant's message id) of a real exchange."""
    conversation = conversation or await chat.create(client)
    response = await chat.send(conversation, question, key, client)
    assert response.status_code == 201, response.text
    body = response.json()
    return conversation, body["user_message"]["id"], body["assistant_message"]["id"]


async def keep(chat, conversation, message, client=None, **extra):
    return await (client or chat.client).post(
        "/api/favorites",
        json={"conversation_id": conversation, "message_id": message},
        headers=post_headers(**extra),
    )


async def listed(chat, client=None):
    response = await (client or chat.client).get("/api/favorites")
    assert response.status_code == 200, response.text
    return response.json()["favorites"]


def rows(chat):
    return chat.rows(FavoriteRecipe)


# --- who may ask ----------------------------------------------------------------------------------

ROUTES = [
    pytest.param("get", "/api/favorites", None, id="list"),
    pytest.param("post", "/api/favorites", {"conversation_id": "x", "message_id": 1}, id="keep"),
    pytest.param("get", "/api/favorites/{id}", None, id="read"),
    pytest.param("delete", "/api/favorites/{id}", None, id="remove"),
]


async def call(chat, method, path, body, *, client=None, favorite_id=None):
    path = path.format(id=favorite_id or str(uuid4()))
    client = client or chat.client
    if method == "get":
        return await client.get(path)
    if method == "delete":
        return await client.delete(path, headers=post_headers())
    return await client.post(path, json=body, headers=post_headers())


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_every_favorites_route_requires_a_session(tmp_path, method, path, body):
    async with chat_app(tmp_path, ScriptedBackend(), sign_in_first=False) as chat:
        response = await call(chat, method, path, body)

        assert response.status_code == 401
        assert error_of(response)["detail"] == "not_authenticated"
        assert rows(chat) == []


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_a_forged_or_malformed_session_cookie_is_no_session(tmp_path, method, path, body):
    async with chat_app(tmp_path, ScriptedBackend(), sign_in_first=False) as chat:
        for cookie in ("forged", "A" * 43, "x" * 5000, ""):
            chat.client.cookies.clear()
            chat.client.cookies.set("__Host-hh_session", cookie, domain="kitchen.example.test")
            assert (await call(chat, method, path, body)).status_code == 401, cookie


async def test_a_signed_out_session_cannot_read_or_change_favorites(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()["id"]
        out = await chat.client.post("/auth/logout", headers=post_headers(**logout_as(chat.user_id)))
        assert out.status_code == 204

        for method, path, body in (
            ("get", "/api/favorites", None),
            ("get", "/api/favorites/{id}", None),
            ("delete", "/api/favorites/{id}", None),
            ("post", "/api/favorites", {"conversation_id": conversation, "message_id": answer}),
        ):
            response = await call(chat, method, path, body, favorite_id=favorite)
            assert response.status_code == 401, (method, path)
        assert len(rows(chat)) == 1


async def test_a_disabled_user_is_refused(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()["id"]
        with chat.base.factory() as db, db.begin():
            db.execute(sa.update(User).where(User.id == chat.user_id).values(disabled_at=chat.clock()))

        assert (await chat.client.get("/api/favorites")).status_code == 401
        assert (await chat.client.get(f"/api/favorites/{favorite}")).status_code == 401
        assert (await keep(chat, conversation, answer)).status_code == 401


async def test_the_owner_is_the_session_never_something_the_request_says(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        other = await chat.second_user()
        conversation, _, answer = await answered(chat)
        stranger = str(chat.extra_users["7777777777777777777"])

        with_owner = await chat.client.post(
            "/api/favorites",
            json={"conversation_id": conversation, "message_id": answer, "user_id": stranger},
            headers=post_headers(**{"X-User-Id": stranger}),
        )
        listing = await chat.client.get(
            "/api/favorites", params={"user_id": stranger}, headers={"X-User-Id": stranger}
        )

        assert with_owner.status_code == 400  # an unexpected field is not read
        assert listing.status_code == 200 and listing.json() == {"favorites": []}
        assert rows(chat) == []
        assert await listed(chat, other) == []


# --- ownership between two people ------------------------------------------------------------------------


async def test_another_person_cannot_keep_read_list_or_remove_what_is_not_theirs(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        other = await chat.second_user()
        conversation, _, answer = await answered(chat)
        mine = (await keep(chat, conversation, answer)).json()["id"]

        # they cannot keep from my conversation, whatever the message id
        stolen = await keep(chat, conversation, answer, client=other)
        # nor read, nor remove my favorite; every one of those looks the same as "never existed"
        read = await other.get(f"/api/favorites/{mine}")
        removed = await other.delete(f"/api/favorites/{mine}", headers=post_headers())
        unknown_read = await other.get(f"/api/favorites/{uuid4()}")
        unknown_removed = await other.delete(f"/api/favorites/{uuid4()}", headers=post_headers())

        assert stolen.status_code == 404 and error_of(stolen)["detail"] == "conversation_not_found"
        assert (read.status_code, removed.status_code) == (404, 404)
        assert error_of(read)["detail"] == error_of(removed)["detail"] == "favorite_not_found"
        assert error_of(read)["message"] == error_of(unknown_read)["message"]
        assert error_of(removed)["message"] == error_of(unknown_removed)["message"]
        assert await listed(chat, other) == []
        assert [item["id"] for item in await listed(chat)] == [mine]
        assert len(rows(chat)) == 1  # nothing was taken or removed
        assert (await chat.client.get(f"/api/favorites/{mine}")).json()["text"] == RECIPE


async def test_each_person_keeps_their_own_even_of_the_same_words(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE, RECIPE)) as chat:
        other = await chat.second_user()
        mine = await answered(chat)
        theirs = await answered(chat, client=other)

        first = await keep(chat, mine[0], mine[2])
        second = await keep(chat, theirs[0], theirs[2], client=other)

        assert (first.status_code, second.status_code) == (201, 201)
        assert first.json()["id"] != second.json()["id"]
        assert [item["id"] for item in await listed(chat)] == [first.json()["id"]]
        assert [item["id"] for item in await listed(chat, other)] == [second.json()["id"]]
        assert {row.user_id for row in rows(chat)} == {chat.user_id, chat.extra_users["7777777777777777777"]}


async def test_an_answer_of_someone_else_cannot_be_kept_through_my_own_conversation(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE, "Мой ответ")) as chat:
        other = await chat.second_user()
        _, _, theirs = await answered(chat, client=other)
        mine, _, _ = await answered(chat, key="k-2")

        response = await keep(chat, mine, theirs)

        assert response.status_code == 404 and error_of(response)["detail"] == "message_not_found"
        assert rows(chat) == []


async def test_a_conversation_id_in_capitals_is_the_same_conversation(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)

        response = await keep(chat, conversation.upper(), answer)

        assert response.status_code == 201
        favorite = response.json()["id"]
        assert (await chat.client.get(f"/api/favorites/{favorite.upper()}")).status_code == 200


# --- keeping an answer ------------------------------------------------------------------------------------------


async def test_keeping_an_answer(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, question, answer = await answered(chat)
        chat.clock.advance(minutes=7)

        response = await keep(chat, conversation, answer)

        body = response.json()
        assert response.status_code == 201
        assert set(body) == {"id", "title", "message_id", "created_at", "created"}
        assert body["created"] is True and body["message_id"] == answer
        assert body["title"] == "Омлет с сыром"
        assert body["created_at"] == chat.clock().isoformat()
        (row,) = rows(chat)
        assert str(row.id) == body["id"] and row.user_id == chat.user_id
        assert row.content == RECIPE  # exactly the answer, markup and all


async def test_the_text_is_the_stored_answer_never_something_the_request_says(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)

        smuggled = await chat.client.post(
            "/api/favorites",
            json={"conversation_id": conversation, "message_id": answer, "text": "подделка", "title": "x"},
            headers=post_headers(),
        )

        assert smuggled.status_code == 400
        assert rows(chat) == []
        assert (await keep(chat, conversation, answer)).status_code == 201
        assert rows(chat)[0].content == RECIPE


async def test_keeping_the_same_answer_again_is_a_success_that_writes_nothing(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        first = await keep(chat, conversation, answer)
        chat.clock.advance(hours=2)

        with statements_of(chat) as seen:
            again = await keep(chat, conversation, answer)

        assert again.status_code == 200
        assert again.json() == {**first.json(), "created": False}  # the first save's time
        assert "INSERT" not in seen
        assert len(rows(chat)) == 1


async def test_only_an_answer_of_the_assistant_is_kept(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, question, answer = await answered(chat)

        asked = await keep(chat, conversation, question)
        missing = await keep(chat, conversation, answer + 99)

        assert (asked.status_code, missing.status_code) == (404, 404)
        assert error_of(asked)["detail"] == error_of(missing)["detail"] == "message_not_found"
        assert rows(chat) == []


async def test_a_conversation_that_is_unknown_or_not_a_uuid_is_not_found(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        _, _, answer = await answered(chat)

        for conversation in (str(uuid4()), "not-a-uuid", "", "1" * 36, "../../etc"):
            response = await keep(chat, conversation, answer)
            assert response.status_code == 404, conversation
            assert error_of(response)["detail"] == "conversation_not_found"
        assert rows(chat) == []


async def test_the_limit_on_favorites_is_reported_and_leaves_what_is_kept_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(favorites_module, "FAVORITES_LIMIT", 2)
    async with chat_app(tmp_path, ScriptedBackend(default=RECIPE)) as chat:
        conversation = await chat.create()
        answers = []
        for number in range(3):
            _, _, answer = await answered(chat, f"вопрос {number}", f"k-{number}", conversation=conversation)
            answers.append(answer)
        for answer in answers[:2]:
            assert (await keep(chat, conversation, answer)).status_code == 201

        full = await keep(chat, conversation, answers[2])
        repeated = await keep(chat, conversation, answers[0])

        assert full.status_code == 409
        assert error_of(full)["detail"] == "favorites_full"
        assert error_of(full)["message"] == CATALOGUE["favorites_full"].message
        assert error_of(full)["retryable"] is False
        assert repeated.status_code == 200 and repeated.json()["created"] is False
        assert len(rows(chat)) == 2
        # removing one makes room
        room = await chat.client.delete(f"/api/favorites/{repeated.json()['id']}", headers=post_headers())
        assert room.status_code == 204
        assert (await keep(chat, conversation, answers[2])).status_code == 201


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"conversation_id": "c"},
        {"message_id": 1},
        {"conversation_id": "c", "message_id": 1, "extra": 1},
        {"conversation_id": 5, "message_id": 1},
        {"conversation_id": None, "message_id": 1},
        {"conversation_id": "c", "message_id": "1"},
        {"conversation_id": "c", "message_id": 1.5},
        {"conversation_id": "c", "message_id": True},
        {"conversation_id": "c", "message_id": None},
        {"conversation_id": "c", "message_id": 0},
        {"conversation_id": "c", "message_id": -4},
        {"conversation_id": "c", "message_id": 2**62 + 1},
        {"conversation_id": "c", "message_id": [1]},
        [],
        "text",
        None,
    ],
)
async def test_a_body_that_names_anything_but_a_conversation_and_a_message_is_refused(tmp_path, document):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        if isinstance(document, dict) and document.get("conversation_id") == "c":
            document = {**document, "conversation_id": conversation}

        response = await chat.client.post(
            "/api/favorites",
            content=json.dumps(document),
            headers=post_headers(**{"Content-Type": "application/json"}),
        )

        assert response.status_code == 400
        assert error_of(response)["detail"] == "invalid_request"
        assert rows(chat) == []


@pytest.mark.parametrize(
    ("content_type", "status"),
    [("text/plain", 415), ("application/x-www-form-urlencoded", 415), ("application/json; charset=latin-1", 415)],
)
async def test_only_json_is_accepted(tmp_path, content_type, status):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)

        response = await chat.client.post(
            "/api/favorites",
            content=json.dumps({"conversation_id": conversation, "message_id": answer}),
            headers=post_headers(**{"Content-Type": content_type}),
        )

        assert response.status_code == status
        assert rows(chat) == []


async def test_a_body_that_is_not_json_or_too_large_is_refused(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        headers = post_headers(**{"Content-Type": "application/json"})

        broken = await chat.client.post("/api/favorites", content=b"{not json", headers=headers)
        huge = await chat.client.post("/api/favorites", content=b'{"a":"' + b"x" * 40_000 + b'"}', headers=headers)

        assert broken.status_code == 400 and huge.status_code == 413
        assert "{not json" not in broken.text  # nothing the client sent is quoted back


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://kitchen.example.test"])
async def test_a_mutation_from_another_origin_is_refused_with_or_without_a_session(tmp_path, origin):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()["id"]
        chat.client.cookies.clear()  # no session at all: the origin is judged first
        headers = {"Origin": origin}

        kept = await chat.client.post(
            "/api/favorites", json={"conversation_id": conversation, "message_id": answer}, headers=headers
        )
        removed = await chat.client.delete(f"/api/favorites/{favorite}", headers=headers)

        assert kept.status_code == removed.status_code == 403
        assert error_of(kept)["detail"] == error_of(removed)["detail"] == "forbidden_origin"
        assert len(rows(chat)) == 1


async def test_a_session_cookie_from_another_site_cannot_remove_a_favorite(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()["id"]

        for site in ("cross-site", "same-site"):
            response = await chat.client.delete(f"/api/favorites/{favorite}", headers={"Sec-Fetch-Site": site})
            assert response.status_code == 403, site
        assert len(rows(chat)) == 1


async def test_reads_do_not_look_at_the_origin(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        response = await chat.client.get("/api/favorites", headers={"Origin": "https://evil.example"})

        assert response.status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/favorites"), ("post", "/api/favorites"), ("options", "/api/favorites"),
     ("delete", "/api/favorites/{id}"), ("options", "/api/favorites/{id}")],
)
async def test_no_response_carries_a_cors_header(tmp_path, method, path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        headers = {
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "content-type",
        }

        response = await chat.client.request(method, path.format(id=uuid4()), headers=headers, json={} if method == "post" else None)

        assert not [name for name in response.headers if name.lower().startswith("access-control-")]


# --- reading ------------------------------------------------------------------------------------------------------------


async def test_the_list_is_newest_first_without_text_and_the_detail_has_the_text(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend("Первый рецепт\nшаги", "Второй рецепт\nшаги")) as chat:
        conversation, _, first = await answered(chat, "первый", "k-1")
        _, _, second = await answered(chat, "второй", "k-2", conversation=conversation)
        one = (await keep(chat, conversation, first)).json()
        chat.clock.advance(minutes=3)
        two = (await keep(chat, conversation, second)).json()

        shown = await listed(chat)

        assert [item["id"] for item in shown] == [two["id"], one["id"]]
        assert all(set(item) == {"id", "title", "message_id", "created_at"} for item in shown)
        assert [item["title"] for item in shown] == ["Второй рецепт", "Первый рецепт"]
        detail = (await chat.client.get(f"/api/favorites/{one['id']}")).json()
        assert set(detail) == {"id", "title", "text", "message_id", "created_at"}
        assert detail["text"] == "Первый рецепт\nшаги" and detail["message_id"] == first


async def test_an_empty_list_is_an_empty_list(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        response = await chat.client.get("/api/favorites")

        assert response.status_code == 200 and response.json() == {"favorites": []}


async def test_a_favorite_id_that_is_unknown_or_not_a_uuid_is_not_found(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        for raw in (str(uuid4()), "not-a-uuid", "1" * 36, "%2e%2e", "x" * 300):
            for response in (
                await chat.client.get(f"/api/favorites/{raw}"),
                await chat.client.delete(f"/api/favorites/{raw}", headers=post_headers()),
            ):
                assert response.status_code == 404, raw
                assert error_of(response)["detail"] == "favorite_not_found"


async def test_reading_writes_nothing(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()["id"]

        with statements_of(chat) as seen:
            for _ in range(3):
                assert (await chat.client.get("/api/favorites")).status_code == 200
                assert (await chat.client.get(f"/api/favorites/{favorite}")).status_code == 200
                assert (await chat.client.get(f"/api/favorites/{uuid4()}")).status_code == 404

        assert not {"INSERT", "UPDATE", "DELETE"} & set(seen)


# --- removing ---------------------------------------------------------------------------------------------------------------


async def test_removing_a_favorite_removes_only_the_copy(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()["id"]

        response = await chat.client.delete(f"/api/favorites/{favorite}", headers=post_headers())

        assert response.status_code == 204 and response.content == b""
        assert rows(chat) == []
        assert await listed(chat) == []
        assert (await chat.client.get(f"/api/favorites/{favorite}")).status_code == 404
        history = (await chat.history(conversation)).json()["messages"]
        assert [m["id"] for m in history][-1] == answer and history[-1]["text"] == RECIPE  # the chat is as it was
        again = await chat.client.delete(f"/api/favorites/{favorite}", headers=post_headers())
        assert again.status_code == 404 and error_of(again)["detail"] == "favorite_not_found"


async def test_a_removed_answer_can_be_kept_again(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        first = (await keep(chat, conversation, answer)).json()["id"]
        await chat.client.delete(f"/api/favorites/{first}", headers=post_headers())

        again = await keep(chat, conversation, answer)

        assert again.status_code == 201 and again.json()["id"] != first


# --- the headers of everything -------------------------------------------------------------------------------------------------


async def test_every_response_is_uncacheable_and_carries_a_server_made_correlation_id(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        kept = await keep(chat, conversation, answer)
        favorite = kept.json()["id"]
        responses = [
            kept,
            await keep(chat, conversation, answer),
            await chat.client.get("/api/favorites"),
            await chat.client.get(f"/api/favorites/{favorite}"),
            await chat.client.get(f"/api/favorites/{uuid4()}"),
            await keep(chat, str(uuid4()), answer),
            await chat.client.delete(f"/api/favorites/{favorite}", headers=post_headers()),
            await chat.client.delete(f"/api/favorites/{favorite}", headers=post_headers()),
        ]

        for response in responses:
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert CORRELATION_ID.fullmatch(response.headers["x-correlation-id"])
        assert len({response.headers["x-correlation-id"] for response in responses}) == len(responses)


async def test_a_correlation_id_sent_by_the_client_is_never_used(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        response = await chat.client.get("/api/favorites", headers={"X-Correlation-ID": "hh-aaaaaaaaaaaaaaaa"})

        assert response.headers["x-correlation-id"] != "hh-aaaaaaaaaaaaaaaa"


async def test_the_text_of_a_recipe_is_never_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()["id"]
        await chat.client.get("/api/favorites")
        await chat.client.get(f"/api/favorites/{favorite}")
        await keep(chat, conversation, answer + 99)  # an error is logged: it must not quote anything
        await chat.client.delete(f"/api/favorites/{uuid4()}", headers=post_headers())
        await chat.client.delete(f"/api/favorites/{favorite}", headers=post_headers())

    logged = log_text(caplog)
    assert "Омлет" not in logged and "яйца" not in logged and "<i>" not in logged
    assert "code=message_not_found" in logged and "code=favorite_not_found" in logged


# --- a favorite does not depend on the history ----------------------------------------------------------------------------


async def test_a_favorite_outlives_the_expiry_of_its_answer_and_its_conversation(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()
        for _ in range(3):  # more than a year, in steps shorter than a session
            chat.clock.advance(days=130)
            await chat.sign_in_again()

        history = (await chat.history(conversation)).json()["messages"]
        shown = await listed(chat)
        detail = await chat.client.get(f"/api/favorites/{favorite['id']}")

        assert history == []  # the answer it was copied from is no longer there
        assert [item["id"] for item in shown] == [favorite["id"]]
        assert shown[0]["created_at"] == favorite["created_at"]  # the day it was kept, not today
        assert detail.status_code == 200 and detail.json()["text"] == RECIPE
        # the expired answer cannot be kept again, but the favorite it already is stays
        again = await keep(chat, conversation, answer)
        assert again.status_code == 404 and error_of(again)["detail"] == "message_not_found"
        assert len(rows(chat)) == 1


async def test_a_favorite_outlives_the_physical_deletion_of_the_history(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()["id"]

        with chat.base.factory() as db, db.begin():  # the purge's end state, forced
            db.execute(sa.text("DELETE FROM turn_requests"))
            db.execute(sa.delete(Message))
            db.execute(sa.text("DELETE FROM conversations"))

        assert (await chat.client.get("/api/conversations")).json()["conversations"] == []
        assert (await chat.client.get(f"/api/favorites/{favorite}")).json()["text"] == RECIPE
        assert [item["id"] for item in await listed(chat)] == [favorite]


async def test_favorites_survive_logout_and_a_new_login(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(RECIPE)) as chat:
        conversation, _, answer = await answered(chat)
        favorite = (await keep(chat, conversation, answer)).json()["id"]
        out = await chat.client.post("/auth/logout", headers=post_headers(**logout_as(chat.user_id)))
        assert out.status_code == 204
        assert (await chat.client.get("/api/favorites")).status_code == 401

        await chat.sign_in_again()

        assert [item["id"] for item in await listed(chat)] == [favorite]
        assert (await chat.client.get(f"/api/favorites/{favorite}")).json()["text"] == RECIPE
