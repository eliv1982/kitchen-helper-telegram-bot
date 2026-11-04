"""Deleting one conversation from the web chat API.

The preflight for this route, and what these tests pin:

  * the ownership check is the DELETE's own WHERE, and an unknown, somebody else's,
    already-deleted or malformed id is the same 404;
  * the database's cascades remove the conversation's messages, turn requests, stored
    message recipes and attachments, and nothing of another person's;
  * favourite recipes are copies with no foreign key to a conversation or a message, so
    both a whole-answer favourite and a structured one survive the deletion unchanged;
  * a turn that is still being answered when its conversation is deleted stores nothing
    when it completes.

No provider is called. The conversations are real ones made through the chat API, and
the assistant is a ScriptedBackend.
"""

import asyncio
import re
from uuid import UUID, uuid4

import pytest

from house_helper.core.backend import BackendReply
from house_helper.core.recipes import Ingredient, Recipe
from house_helper.persistence.models import (
    Conversation,
    FavoriteRecipe,
    Message,
    MessageRecipe,
    TurnRequest,
)
from tests.chat_support import ScriptedBackend, chat_app, error_of, post_headers
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

CORRELATION_ID = re.compile(r"hh-[0-9a-f]{16}")
RECIPE_TEXT = "Борщ\n\n1. Сварите бульон."


def structured_reply() -> BackendReply:
    """The answer the real backend produces after parsing one recipe block."""
    return BackendReply(
        text=RECIPE_TEXT,
        recipes=(
            Recipe(
                index=0,
                title="Борщ",
                servings="4 порции",
                ingredients=(Ingredient("свекла", "2 шт."),),
                steps=("Сварите бульон.",),
            ),
        ),
    )


async def delete(chat, conversation, client=None):
    return await (client or chat.client).delete(
        f"/api/conversations/{conversation}", headers=post_headers()
    )


async def answered(chat, text="Что приготовить?", key="k-1", client=None):
    conversation = await chat.create(client)
    response = await chat.send(conversation, text, key, client)
    assert response.status_code == 201, response.text
    return conversation, response.json()


def count(chat, model) -> int:
    return len(chat.rows(model))


# --- who may ask -----------------------------------------------------------------------------


async def test_deleting_a_conversation_requires_a_session(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(), sign_in_first=False) as chat:
        response = await chat.client.delete(
            f"/api/conversations/{uuid4()}", headers=post_headers()
        )

    assert response.status_code == 401
    assert error_of(response)["detail"] == "not_authenticated"


async def test_a_delete_from_another_origin_is_refused_and_deletes_nothing(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.client.delete(
            f"/api/conversations/{conversation}",
            headers={"Origin": "https://evil.example"},
        )

        assert response.status_code == 403
        assert error_of(response)["detail"] == "forbidden_origin"
        assert chat.conversation(conversation).id is not None


# --- the answer, and what is deleted ---------------------------------------------------------


async def test_deleting_the_users_conversation_removes_its_history_and_answers_204(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(structured_reply())) as chat:
        conversation, body = await answered(chat)
        user_message = body["user_message"]["id"]
        assistant_message = body["assistant_message"]["id"]
        assert count(chat, MessageRecipe) == 1

        response = await delete(chat, conversation)

        assert response.status_code == 204
        assert response.content == b""
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert CORRELATION_ID.fullmatch(response.headers["x-correlation-id"])
        assert chat.rows(Conversation, Conversation.id == UUID(conversation)) == []
        assert chat.messages(conversation) == []
        assert chat.turns(conversation) == []
        assert count(chat, Message) == 0
        assert count(chat, TurnRequest) == 0
        assert count(chat, MessageRecipe) == 0
        assert chat.rows(
            MessageRecipe,
            MessageRecipe.message_id.in_((user_message, assistant_message)),
        ) == []


async def test_deleting_one_conversation_leaves_the_others_whole(tmp_path):
    backend = ScriptedBackend(default="ответ")
    async with chat_app(tmp_path, backend) as chat:
        first, _ = await answered(chat, "Первый вопрос", "k-1")
        chat.clock.advance(minutes=1)
        second, _ = await answered(chat, "Второй вопрос", "k-2")

        response = await delete(chat, second)

        assert response.status_code == 204
        assert [c["id"] for c in (await chat.client.get("/api/conversations")).json()["conversations"]] == [first]
        assert (await chat.history(second)).status_code == 404
        assert (await chat.history(first)).status_code == 200
        assert [m["text"] for m in (await chat.history(first)).json()["messages"]] == [
            "Первый вопрос", "ответ",
        ]


async def test_the_same_delete_twice_is_a_plain_404_and_changes_nothing_more(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation, _ = await answered(chat)
        assert (await delete(chat, conversation)).status_code == 204

        again = await delete(chat, conversation)

        assert again.status_code == 404
        assert error_of(again)["detail"] == "conversation_not_found"
        assert chat.rows(Conversation, Conversation.id == UUID(conversation)) == []


# --- ownership and account isolation ---------------------------------------------------------


async def test_unknown_foreign_and_malformed_ids_are_the_same_safe_404(tmp_path):
    backend = ScriptedBackend(default="ответ")
    async with chat_app(tmp_path, backend) as chat:
        mine, _ = await answered(chat)
        other = await chat.second_user()
        theirs, _ = await answered(chat, "Их вопрос", "their-key", client=other)

        answers = {}
        for name, conversation, client in (
            ("unknown", str(uuid4()), chat.client),
            ("malformed", "not-a-uuid", chat.client),
            ("unhyphenated", uuid4().hex, chat.client),
            ("foreign", theirs, chat.client),
        ):
            response = await delete(chat, conversation, client)
            answers[name] = (response.status_code, error_of(response)["detail"])

        assert set(answers.values()) == {(404, "conversation_not_found")}
        assert chat.conversation(mine).id is not None
        assert chat.conversation(theirs).id is not None
        assert len(chat.messages(theirs)) == 2
        assert len(chat.turns(theirs)) == 1


async def test_deleting_my_conversation_leaves_another_persons_conversation_whole(tmp_path):
    backend = ScriptedBackend(default="ответ")
    async with chat_app(tmp_path, backend) as chat:
        mine, _ = await answered(chat, "Мой секрет", "my-key")
        other = await chat.second_user()
        theirs, _ = await answered(chat, "Их секрет", "their-key", client=other)

        assert (await delete(chat, mine)).status_code == 204

        assert chat.rows(Conversation, Conversation.id == UUID(mine)) == []
        assert chat.conversation(theirs).id is not None
        assert [m.content for m in chat.messages(theirs)] == ["Их секрет", "ответ"]
        assert len(chat.turns(theirs)) == 1
        history = await chat.history(theirs, client=other)
        assert history.status_code == 200
        assert [m["text"] for m in history.json()["messages"]] == ["Их секрет", "ответ"]
        assert (await other.get("/api/conversations")).json()["conversations"][0]["id"] == theirs


# --- what is preserved ------------------------------------------------------------------------


async def test_text_and_structured_favorites_survive_the_deletion_of_their_conversation(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(structured_reply())) as chat:
        conversation, body = await answered(chat)
        answer = body["assistant_message"]["id"]

        text_kept = await chat.client.post(
            "/api/favorites",
            json={"conversation_id": conversation, "message_id": answer},
            headers=post_headers(),
        )
        structured_kept = await chat.client.post(
            "/api/favorites",
            json={"conversation_id": conversation, "message_id": answer, "recipe_index": 0},
            headers=post_headers(),
        )
        assert (text_kept.status_code, structured_kept.status_code) == (201, 201)
        text_id = text_kept.json()["id"]
        structured_id = structured_kept.json()["id"]

        assert (await delete(chat, conversation)).status_code == 204

        assert count(chat, FavoriteRecipe) == 2
        listed = (await chat.client.get("/api/favorites")).json()["favorites"]
        assert {item["id"] for item in listed} == {text_id, structured_id}
        assert count(chat, Message) == count(chat, MessageRecipe) == 0

        text = await chat.client.get(f"/api/favorites/{text_id}")
        structured = await chat.client.get(f"/api/favorites/{structured_id}")
        assert text.status_code == structured.status_code == 200
        assert text.json()["text"] == RECIPE_TEXT
        assert text.json()["message_id"] == answer
        assert structured.json()["title"] == "Борщ"
        assert structured.json()["kind"] == "structured"
        assert structured.json()["message_id"] == answer
        assert structured.json()["ingredients"] == [{"name": "свекла", "amount": "2 шт."}]
        assert structured.json()["steps"] == ["Сварите бульон."]


async def test_deleting_one_persons_conversation_leaves_another_persons_favorites(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(default="ответ")) as chat:
        mine, _ = await answered(chat, "Мой", "my-key")
        my_answer = (await chat.history(mine)).json()["messages"][-1]["id"]
        mine_kept = await chat.client.post(
            "/api/favorites",
            json={"conversation_id": mine, "message_id": my_answer},
            headers=post_headers(),
        )
        other = await chat.second_user()
        theirs, _ = await answered(chat, "Их", "their-key", client=other)
        their_answer = (await chat.history(theirs, client=other)).json()["messages"][-1]["id"]
        their_kept = await other.post(
            "/api/favorites",
            json={"conversation_id": theirs, "message_id": their_answer},
            headers=post_headers(),
        )
        assert (mine_kept.status_code, their_kept.status_code) == (201, 201)

        assert (await delete(chat, mine)).status_code == 204

        assert [item["id"] for item in (await other.get("/api/favorites")).json()["favorites"]] == [
            their_kept.json()["id"]
        ]
        mine_listed = await chat.client.get("/api/favorites")
        assert mine_listed.status_code == 200
        assert [item["id"] for item in mine_listed.json()["favorites"]] == [mine_kept.json()["id"]]


# --- a reply still being prepared -------------------------------------------------------------


async def test_a_turn_still_being_answered_when_its_conversation_is_deleted_stores_nothing(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        sending = asyncio.ensure_future(chat.send(conversation, "Готовь", "k-1"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        assert (await delete(chat, conversation)).status_code == 204
        release.set()
        response = await sending

        assert response.status_code == 409
        assert error_of(response)["detail"] == "turn_in_progress"
        assert chat.rows(Conversation, Conversation.id == UUID(conversation)) == []
        assert chat.messages(conversation) == []
        assert chat.turns(conversation) == []
        assert count(chat, MessageRecipe) == 0
        assert backend.calls == 1
