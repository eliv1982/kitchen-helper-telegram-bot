"""The shopping-list API's contract through the real application.

The assistant is always a ScriptedBackend: it answers with one validated recipe (the
object the real backend carries beside its rendered text), so the stored rows a list is
copied from are the rows the application writes. Nothing reaches a provider, and the
database is a real migrated SQLite file. Ownership, the Origin rule, idempotent
creation, every item write and the complete/reopen life are exercised through HTTP.
"""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from house_helper.core.backend import BackendReply
from house_helper.core.recipes import Ingredient, Recipe, render_recipe
from house_helper.persistence.models import ShoppingList, ShoppingListItem, User
from house_helper.persistence.shopping_lists import creation_fingerprint
from tests.auth_support import ORIGIN, sign_in
from tests.chat_support import ScriptedBackend, chat_app, error_of, post_headers
from tests.persistence_support import stored

pytestmark = pytest.mark.asyncio

BORSH = Recipe(
    index=0,
    title="Борщ",
    servings="4 порции",
    ingredients=(
        Ingredient("свёкла", "2 шт."),
        Ingredient("капуста", "300 г"),
        Ingredient("говядина", "500 г"),
    ),
    steps=("Сварите бульон.", "Добавьте овощи."),
)
OMELETTE = Recipe(
    index=1,
    title="Омлет",
    servings=None,
    ingredients=(Ingredient("яйца", "3 шт."),),
    steps=("Взбейте и обжарьте.",),
)


def backend() -> ScriptedBackend:
    return ScriptedBackend(
        BackendReply(text=render_recipe(BORSH), recipes=(BORSH,)),
        BackendReply(text=render_recipe(OMELETTE), recipes=(OMELETTE,)),
    )


async def answered(chat, key="k-1", client=None):
    conversation = await chat.create(client)
    response = await chat.send(conversation, "Что приготовить?", key, client)
    assert response.status_code == 201, response.text
    return conversation, response.json()["assistant_message"]["id"]


async def keep(chat, conversation, message, recipe_index=0, client=None):
    response = await (client or chat.client).post(
        "/api/favorites",
        json={
            "conversation_id": conversation,
            "message_id": message,
            "recipe_index": recipe_index,
        },
        headers=post_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def message_body(conversation, message, recipe_index=0, item_states=None) -> dict:
    body = {
        "source": "message",
        "conversation_id": conversation,
        "message_id": message,
        "recipe_index": recipe_index,
    }
    if item_states is not None:
        body["item_states"] = [
            {"position": position, "state": state}
            for position, state in sorted(item_states.items())
        ]
    return body


async def save(chat, body, *, key="list-1", client=None):
    return await (client or chat.client).post(
        "/api/shopping-lists", json=body, headers=post_headers(key)
    )


async def saved(chat, body, *, key="list-1", client=None) -> dict:
    response = await save(chat, body, key=key, client=client)
    assert response.status_code == 201, response.text
    return response.json()


def expire(chat, message_id: int) -> None:
    """Make an answer expired without moving the clock (which would end the session).

    Both timestamps move together, by raw SQL, because the ORM refuses to rewrite a
    message's created_at and the retention CHECK requires expires_at after it: the row
    becomes an ordinary answer that expired a month ago.
    """
    now = chat.clock()
    with chat.base.factory() as db, db.begin():
        db.connection().exec_driver_sql(
            "UPDATE messages SET created_at = ?, expires_at = ? WHERE id = ?",
            (stored(now - timedelta(days=400)), stored(now - timedelta(days=35)), message_id),
        )


def lists(chat) -> list[dict]:
    return chat.rows(ShoppingList)


def items(chat) -> list[ShoppingListItem]:
    return chat.rows(ShoppingListItem, order=ShoppingListItem.position)


# --- who may ask -------------------------------------------------------------------------


ROUTES = [
    pytest.param("get", "/api/shopping-lists", None, id="list"),
    pytest.param(
        "post",
        "/api/shopping-lists",
        {"source": "message", "conversation_id": str(uuid4()), "message_id": 1, "recipe_index": 0},
        id="create",
    ),
    pytest.param("get", "/api/shopping-lists/{id}", None, id="read"),
    pytest.param("patch", "/api/shopping-lists/{id}", {"status": "completed"}, id="status"),
    pytest.param("delete", "/api/shopping-lists/{id}", None, id="remove"),
    pytest.param("post", "/api/shopping-lists/{id}/items", {"name": "соль"}, id="add-item"),
    pytest.param("patch", "/api/shopping-lists/{id}/items/{item}", {"state": "at_home"}, id="edit-item"),
    pytest.param("delete", "/api/shopping-lists/{id}/items/{item}", None, id="remove-item"),
    pytest.param(
        "get",
        "/api/conversations/{id}/messages/1/recipes/0",
        None,
        id="read-recipe",
    ),
]


async def call(chat, method, path, body, *, client=None, list_id=None, item_id=None):
    path = path.format(id=list_id or str(uuid4()), item=item_id or str(uuid4()))
    client = client or chat.client
    if method == "get":
        return await client.get(path)
    if method == "delete":
        return await client.delete(path, headers=post_headers())
    if method == "patch":
        return await client.patch(path, json=body, headers=post_headers())
    return await client.post(path, json=body, headers=post_headers())


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_every_shopping_route_requires_a_session(tmp_path, method, path, body):
    async with chat_app(tmp_path, backend(), sign_in_first=False) as chat:
        response = await call(chat, method, path, body)

        assert response.status_code == 401
        assert error_of(response)["detail"] == "not_authenticated"
        assert lists(chat) == [] and items(chat) == []


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_a_disabled_user_is_refused(tmp_path, method, path, body):
    async with chat_app(tmp_path, backend()) as chat:
        with chat.base.factory() as db, db.begin():
            db.execute(sa.update(User).where(User.id == chat.user_id).values(disabled_at=chat.clock()))

        assert (await call(chat, method, path, body)).status_code == 401


async def test_a_mutation_from_another_site_is_refused(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        response = await chat.client.post(
            "/api/shopping-lists",
            json=message_body(conversation, message),
            headers={"Origin": "https://evil.example", "Idempotency-Key": "list-1"},
        )

        assert response.status_code == 403
        assert error_of(response)["detail"] == "forbidden_origin"
        assert lists(chat) == []


# --- reading a recipe for the draft --------------------------------------------------------


async def test_the_draft_reads_the_stored_recipe_the_list_will_copy(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)

        response = await chat.client.get(
            f"/api/conversations/{conversation}/messages/{message}/recipes/0"
        )

        assert response.status_code == 200
        assert response.json() == {
            "message_id": message,
            "position": 0,
            "title": "Борщ",
            "servings": "4 порции",
            "ingredients": [
                {"name": "свёкла", "amount": "2 шт."},
                {"name": "капуста", "amount": "300 г"},
                {"name": "говядина", "amount": "500 г"},
            ],
            "steps": ["Сварите бульон.", "Добавьте овощи."],
        }


async def test_the_recipe_read_is_scoped_by_conversation_user_and_expiry(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        other = await chat.second_user()
        conversation, message = await answered(chat)
        unknown = str(uuid4())

        assert (
            await other.get(f"/api/conversations/{conversation}/messages/{message}/recipes/0")
        ).status_code == 404
        assert (
            await chat.client.get(f"/api/conversations/{conversation}/messages/{message}/recipes/1")
        ).status_code == 404
        assert (
            await chat.client.get(f"/api/conversations/{unknown}/messages/{message}/recipes/0")
        ).status_code == 404
        assert (
            await chat.client.get(f"/api/conversations/{conversation}/messages/1/recipes/0")
        ).status_code == 404
        assert (
            await chat.client.get(f"/api/conversations/{conversation}/messages/{message}/recipes/9")
        ).status_code == 404

        expire(chat, message)
        expired = await chat.client.get(
            f"/api/conversations/{conversation}/messages/{message}/recipes/0"
        )
        assert expired.status_code == 404
        assert error_of(expired)["detail"] == "message_not_found"


# --- creating from a message recipe -------------------------------------------------------


async def test_a_list_copies_every_ingredient_and_the_drafts_states(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)

        body = await saved(
            chat, message_body(conversation, message, item_states={0: "at_home", 2: "purchased"})
        )

        assert body["created"] is True
        assert body["title"] == "Борщ"
        assert body["status"] == "active"
        assert body["source"] == {
            "kind": "message",
            "message_id": message,
            "recipe_index": 0,
        }
        assert [(item["position"], item["name"], item["amount"], item["state"]) for item in body["items"]] == [
            (0, "свёкла", "2 шт.", "at_home"),
            (1, "капуста", "300 г", "unchecked"),
            (2, "говядина", "500 г", "purchased"),
        ]
        assert len(lists(chat)) == 1 and len(items(chat)) == 3


async def test_a_list_with_every_ingredient_at_home_is_still_saved_whole(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)

        body = await saved(
            chat,
            message_body(
                conversation,
                message,
                item_states={0: "at_home", 1: "at_home", 2: "at_home"},
            ),
        )

        assert len(body["items"]) == 3
        assert {item["state"] for item in body["items"]} == {"at_home"}
        listed = (await chat.client.get("/api/shopping-lists")).json()["lists"][0]
        assert listed["counts"] == {
            "total": 3,
            "unchecked": 0,
            "at_home": 3,
            "purchased": 0,
        }


async def test_an_omitted_state_means_unchecked(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)

        body = await saved(chat, message_body(conversation, message))

        assert [item["state"] for item in body["items"]] == [
            "unchecked",
            "unchecked",
            "unchecked",
        ]


async def test_an_idempotent_replay_answers_the_same_list_and_writes_nothing(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = message_body(conversation, message, item_states={0: "at_home"})
        first = await saved(chat, body)

        again = await save(chat, body)

        assert again.status_code == 200
        assert again.headers["idempotent-replayed"] == "true"
        assert again.json()["id"] == first["id"]
        assert again.json()["created"] is False
        assert len(lists(chat)) == 1 and len(items(chat)) == 3


async def test_the_same_key_for_another_request_is_a_conflict(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        await saved(chat, message_body(conversation, message, item_states={0: "at_home"}))

        response = await save(
            chat, message_body(conversation, message, item_states={0: "purchased"})
        )

        assert response.status_code == 409
        assert error_of(response)["detail"] == "shopping_list_key_reused"
        assert len(lists(chat)) == 1


async def test_a_retry_after_deletion_is_a_stable_conflict_and_recreates_nothing(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = message_body(conversation, message)
        first = await saved(chat, body)
        removed = await chat.client.delete(
            f"/api/shopping-lists/{first['id']}", headers=post_headers()
        )
        assert removed.status_code == 204

        for _ in range(2):  # the same answer every time: the key stays consumed
            again = await save(chat, body)
            assert again.status_code == 409
            assert error_of(again)["detail"] == "shopping_list_deleted"
            assert "idempotent-replayed" not in again.headers

        assert lists(chat) == [] and items(chat) == []  # nothing was resurrected
        assert (await chat.client.get("/api/shopping-lists")).json() == {"lists": []}
        assert (
            await chat.client.get(f"/api/shopping-lists/{first['id']}")
        ).status_code == 404


async def test_a_changed_body_with_a_consumed_key_is_refused_after_deletion(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        first = await saved(chat, message_body(conversation, message, item_states={0: "at_home"}))
        assert (
            await chat.client.delete(
                f"/api/shopping-lists/{first['id']}", headers=post_headers()
            )
        ).status_code == 204

        response = await save(
            chat, message_body(conversation, message, item_states={0: "purchased"})
        )

        assert response.status_code == 409
        assert error_of(response)["detail"] == "shopping_list_key_reused"
        assert lists(chat) == [] and items(chat) == []


async def test_a_new_key_after_deletion_saves_a_new_list(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = message_body(conversation, message)
        first = await saved(chat, body)
        assert (
            await chat.client.delete(
                f"/api/shopping-lists/{first['id']}", headers=post_headers()
            )
        ).status_code == 204

        second = await saved(chat, body, key="list-2")

        assert second["created"] is True and second["id"] != first["id"]
        assert len(lists(chat)) == 1 and len(items(chat)) == 3
        assert (await chat.client.get("/api/shopping-lists")).json()["lists"][0]["id"] == second["id"]
        # ... and the first key is still spent
        assert (await save(chat, body)).status_code == 409


async def test_two_concurrent_retries_with_one_key_create_one_list(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = message_body(conversation, message)

        first, second = await asyncio.gather(save(chat, body), save(chat, body))

        assert sorted({first.status_code, second.status_code}) == [200, 201]
        assert len(lists(chat)) == 1 and len(items(chat)) == 3


async def test_creation_requires_an_idempotency_key(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)

        response = await chat.client.post(
            "/api/shopping-lists",
            json=message_body(conversation, message),
            headers={"Origin": ORIGIN},
        )

        assert response.status_code == 400
        assert error_of(response)["detail"] == "idempotency_key_required"
        assert lists(chat) == []


@pytest.mark.parametrize(
    "body",
    [
        {"source": "message"},
        {"source": "message", "conversation_id": "x", "message_id": 1, "recipe_index": 0},
        {"source": "favorite", "favorite_id": "x", "expected_version": 1},
        {"source": "elsewhere"},
        {"source": "message", "conversation_id": str(uuid4()), "message_id": 1, "recipe_index": 0, "extra": 1},
    ],
    ids=["missing-fields", "bad-conversation", "bad-favorite", "unknown-source", "extra-field"],
)
async def test_a_malformed_creation_is_refused(tmp_path, body):
    async with chat_app(tmp_path, backend()) as chat:
        response = await save(chat, body)

        assert response.status_code in (400, 404)
        assert lists(chat) == []


async def test_a_bad_state_or_position_is_a_bad_request(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        for states in (
            [{"position": 0, "state": "lost"}],
            [{"position": 1, "state": "at_home"}, {"position": 1, "state": "purchased"}],
            [{"position": 9, "state": "at_home"}],
        ):
            response = await save(
                chat,
                {
                    **message_body(conversation, message),
                    "item_states": states,
                },
            )
            assert response.status_code == 400, states
            assert error_of(response)["detail"] == "invalid_request"

        assert lists(chat) == []


async def test_the_message_source_must_be_the_users_unexpired_answer(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        other = await chat.second_user()
        conversation, message = await answered(chat)
        unknown = str(uuid4())

        stolen = await save(chat, message_body(conversation, message), client=other)
        unknown_conversation = await save(chat, message_body(unknown, message))
        unknown_message = await save(chat, message_body(conversation, 999999))
        unknown_recipe = await save(chat, message_body(conversation, message, recipe_index=1))

        assert (stolen.status_code, error_of(stolen)["detail"]) == (404, "conversation_not_found")
        assert (unknown_conversation.status_code, error_of(unknown_conversation)["detail"]) == (
            404,
            "conversation_not_found",
        )
        assert (unknown_message.status_code, error_of(unknown_message)["detail"]) == (
            404,
            "message_not_found",
        )
        assert (unknown_recipe.status_code, error_of(unknown_recipe)["detail"]) == (
            404,
            "recipe_not_found",
        )
        assert lists(chat) == []

        expire(chat, message)
        expired = await save(chat, message_body(conversation, message), key="list-2")
        assert expired.status_code == 404
        assert error_of(expired)["detail"] == "message_not_found"


# --- creating from a structured favorite ---------------------------------------------------


async def test_a_list_is_created_from_a_structured_favorite_at_its_version(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        favorite = await keep(chat, conversation, message)

        body = await saved(
            chat,
            {
                "source": "favorite",
                "favorite_id": favorite["id"],
                "expected_version": favorite["version"],
                "item_states": [{"position": 1, "state": "purchased"}],
            },
            key="fav-1",
        )

        assert body["title"] == "Борщ"
        assert body["source"] == {
            "kind": "favorite",
            "favorite_id": favorite["id"],
            "version": favorite["version"],
        }
        assert [(item["name"], item["state"]) for item in body["items"]] == [
            ("свёкла", "unchecked"),
            ("капуста", "purchased"),
            ("говядина", "unchecked"),
        ]


async def test_a_changed_favorite_is_a_conflict_and_never_a_silent_copy(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        favorite = await keep(chat, conversation, message)
        edited = await chat.client.patch(
            f"/api/favorites/{favorite['id']}",
            json={
                "expected_version": favorite["version"],
                "title": "Другой борщ",
                "ingredients": [{"name": "морковь", "amount": "1 шт."}],
            },
            headers=post_headers(),
        )
        assert edited.status_code == 200
        assert edited.json()["version"] == favorite["version"] + 1

        stale = await save(
            chat,
            {
                "source": "favorite",
                "favorite_id": favorite["id"],
                "expected_version": favorite["version"],
            },
            key="fav-1",
        )
        assert stale.status_code == 409
        assert error_of(stale)["detail"] == "favorite_conflict"
        assert lists(chat) == []

        current = await saved(
            chat,
            {
                "source": "favorite",
                "favorite_id": favorite["id"],
                "expected_version": favorite["version"] + 1,
            },
            key="fav-2",
        )
        assert [(item["name"], item["amount"]) for item in current["items"]] == [
            ("морковь", "1 шт.")
        ]


async def test_a_text_favorite_has_no_ingredients_to_copy(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        text = (
            await chat.client.post(
                "/api/favorites",
                json={"conversation_id": conversation, "message_id": message},
                headers=post_headers(),
            )
        ).json()

        response = await save(
            chat,
            {"source": "favorite", "favorite_id": text["id"], "expected_version": text["version"]},
            key="fav-1",
        )

        assert response.status_code == 400
        assert error_of(response)["detail"] == "invalid_request"
        assert lists(chat) == []


async def test_another_users_or_unknown_favorite_is_not_found(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        other = await chat.second_user()
        conversation, message = await answered(chat)
        favorite = await keep(chat, conversation, message)

        stolen = await save(
            chat,
            {
                "source": "favorite",
                "favorite_id": favorite["id"],
                "expected_version": favorite["version"],
            },
            client=other,
        )
        unknown = await save(
            chat,
            {"source": "favorite", "favorite_id": str(uuid4()), "expected_version": 1},
            key="fav-2",
        )

        assert stolen.status_code == 404 and error_of(stolen)["detail"] == "favorite_not_found"
        assert unknown.status_code == 404 and error_of(unknown)["detail"] == "favorite_not_found"
        assert lists(chat) == []


# --- reading, item editing and the list's life ---------------------------------------------


async def test_the_lists_are_newest_first_and_a_completed_one_stays(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        first = await saved(chat, message_body(conversation, message), key="list-1")
        chat.clock.advance(hours=1)
        second = await saved(
            chat,
            message_body(conversation, message, item_states={0: "at_home"}),
            key="list-2",
        )
        completed = await chat.client.patch(
            f"/api/shopping-lists/{first['id']}",
            json={"status": "completed"},
            headers=post_headers(),
        )
        assert completed.status_code == 200

        listing = (await chat.client.get("/api/shopping-lists")).json()["lists"]

        assert [item["id"] for item in listing] == [second["id"], first["id"]]
        assert [item["status"] for item in listing] == ["active", "completed"]
        assert (await chat.client.get(f"/api/shopping-lists/{first['id']}")).status_code == 200


async def test_the_list_and_its_items_are_read_back_in_order(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = await saved(chat, message_body(conversation, message))

        detail = await chat.client.get(f"/api/shopping-lists/{body['id']}")
        listing = await chat.client.get("/api/shopping-lists")

        assert detail.status_code == 200
        assert [item["id"] for item in detail.json()["items"]] == [
            item["id"] for item in body["items"]
        ]
        assert listing.status_code == 200
        assert listing.json()["lists"] == [
            {
                "id": body["id"],
                "title": "Борщ",
                "status": "active",
                "created_at": body["created_at"],
                "updated_at": body["updated_at"],
                "counts": {"total": 3, "unchecked": 3, "at_home": 0, "purchased": 0},
            }
        ]


async def test_an_item_is_added_edited_and_deleted_through_the_api(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = await saved(chat, message_body(conversation, message))
        list_id = body["id"]

        added = await chat.client.post(
            f"/api/shopping-lists/{list_id}/items",
            json={"name": "соль", "amount": "по вкусу", "state": "at_home"},
            headers=post_headers(),
        )
        assert added.status_code == 201
        item = added.json()
        assert (item["position"], item["state"]) == (3, "at_home")

        edited = await chat.client.patch(
            f"/api/shopping-lists/{list_id}/items/{item['id']}",
            json={"name": "морская соль", "amount": None, "state": "purchased"},
            headers=post_headers(),
        )
        assert edited.status_code == 200
        assert (edited.json()["name"], edited.json()["amount"], edited.json()["state"]) == (
            "морская соль",
            None,
            "purchased",
        )

        removed = await chat.client.delete(
            f"/api/shopping-lists/{list_id}/items/{item['id']}", headers=post_headers()
        )
        assert removed.status_code == 204
        detail = (await chat.client.get(f"/api/shopping-lists/{list_id}")).json()
        assert [entry["position"] for entry in detail["items"]] == [0, 1, 2]


async def test_a_position_above_the_item_cap_is_returned_after_deletions(tmp_path):
    """Positions are monotonic (max + 1), not indexes: a list that lost an item at the cap
    can legitimately hold position 200, and the API answers it like any other."""
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = await saved(chat, message_body(conversation, message))
        list_id = body["id"]
        assert [item["position"] for item in body["items"]] == [0, 1, 2]

        with chat.base.factory() as db, db.begin():
            for position in range(3, 200):
                db.add(
                    ShoppingListItem(
                        list_id=UUID(list_id),
                        position=position,
                        name=f"item {position}",
                        state="unchecked",
                        created_at=chat.clock(),
                        updated_at=chat.clock(),
                    )
                )

        # Free one slot below the end: the next item's position is max + 1 = 200.
        removed = await chat.client.delete(
            f"/api/shopping-lists/{list_id}/items/{body['items'][0]['id']}",
            headers=post_headers(),
        )
        assert removed.status_code == 204
        added = await chat.client.post(
            f"/api/shopping-lists/{list_id}/items",
            json={"name": "соль", "amount": "1 ч. л."},
            headers=post_headers(),
        )
        assert added.status_code == 201
        assert added.json()["position"] == 200

        detail = (await chat.client.get(f"/api/shopping-lists/{list_id}")).json()
        assert [item["position"] for item in detail["items"]] == list(range(1, 201))

        edited = await chat.client.patch(
            f"/api/shopping-lists/{list_id}/items/{added.json()['id']}",
            json={"state": "purchased"},
            headers=post_headers(),
        )
        assert edited.status_code == 200
        assert (edited.json()["position"], edited.json()["state"]) == (200, "purchased")


async def test_a_completed_list_is_read_only_and_can_be_reopened(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = await saved(chat, message_body(conversation, message))
        list_id, item_id = body["id"], body["items"][0]["id"]

        completed = await chat.client.patch(
            f"/api/shopping-lists/{list_id}",
            json={"status": "completed"},
            headers=post_headers(),
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"
        assert len(completed.json()["items"]) == 3

        for method, path, payload in (
            ("post", f"/api/shopping-lists/{list_id}/items", {"name": "соль"}),
            ("patch", f"/api/shopping-lists/{list_id}/items/{item_id}", {"state": "at_home"}),
            ("delete", f"/api/shopping-lists/{list_id}/items/{item_id}", None),
        ):
            if method == "delete":
                response = await chat.client.delete(path, headers=post_headers())
            elif method == "patch":
                response = await chat.client.patch(path, json=payload, headers=post_headers())
            else:
                response = await chat.client.post(path, json=payload, headers=post_headers())
            assert response.status_code == 409, (method, path)
            assert error_of(response)["detail"] == "shopping_list_completed"

        assert len(items(chat)) == 3

        reopened = await chat.client.patch(
            f"/api/shopping-lists/{list_id}",
            json={"status": "active"},
            headers=post_headers(),
        )
        assert reopened.status_code == 200 and reopened.json()["status"] == "active"

        edited = await chat.client.patch(
            f"/api/shopping-lists/{list_id}/items/{item_id}",
            json={"state": "at_home"},
            headers=post_headers(),
        )
        assert edited.status_code == 200 and edited.json()["state"] == "at_home"


async def test_a_list_is_deleted_with_its_items_and_nobody_elses_is_touched(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        other = await chat.second_user()
        conversation, message = await answered(chat)
        body = await saved(chat, message_body(conversation, message))
        second = await saved(chat, message_body(conversation, message), key="list-2")

        stolen = await other.delete(f"/api/shopping-lists/{body['id']}", headers=post_headers())
        removed = await chat.client.delete(
            f"/api/shopping-lists/{body['id']}", headers=post_headers()
        )

        assert stolen.status_code == 404
        assert removed.status_code == 204
        assert (await chat.client.get(f"/api/shopping-lists/{body['id']}")).status_code == 404
        assert len(chat.rows(ShoppingList)) == 1
        assert len(items(chat)) == 3
        assert (await chat.client.get(f"/api/shopping-lists/{second['id']}")).status_code == 200


async def test_another_person_cannot_read_or_change_a_list_or_its_items(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        other = await chat.second_user()
        conversation, message = await answered(chat)
        body = await saved(chat, message_body(conversation, message))
        list_id, item_id = body["id"], body["items"][0]["id"]

        assert (await other.get(f"/api/shopping-lists/{list_id}")).status_code == 404
        assert (await other.get("/api/shopping-lists")).json() == {"lists": []}
        for method, path, payload in (
            ("patch", f"/api/shopping-lists/{list_id}", {"status": "completed"}),
            ("post", f"/api/shopping-lists/{list_id}/items", {"name": "соль"}),
            ("patch", f"/api/shopping-lists/{list_id}/items/{item_id}", {"state": "at_home"}),
            ("delete", f"/api/shopping-lists/{list_id}/items/{item_id}", None),
            ("delete", f"/api/shopping-lists/{list_id}", None),
        ):
            if method == "delete":
                response = await other.delete(path, headers=post_headers())
            elif method == "patch":
                response = await other.patch(path, json=payload, headers=post_headers())
            else:
                response = await other.post(path, json=payload, headers=post_headers())
            assert response.status_code == 404, (method, path)

        detail = (await chat.client.get(f"/api/shopping-lists/{list_id}")).json()
        assert detail["status"] == "active" and len(detail["items"]) == 3


async def test_a_second_device_of_the_same_person_sees_and_changes_the_same_list(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = await saved(chat, message_body(conversation, message))

        second = chat.base.new_client()
        response = await sign_in(second, chat.base.telegram)
        assert response.status_code == 303

        assert (await second.get(f"/api/shopping-lists/{body['id']}")).status_code == 200
        added = await second.post(
            f"/api/shopping-lists/{body['id']}/items",
            json={"name": "соль", "state": "purchased"},
            headers=post_headers(),
        )
        assert added.status_code == 201

        first_view = (await chat.client.get(f"/api/shopping-lists/{body['id']}")).json()
        assert [(item["name"], item["state"]) for item in first_view["items"]][-1] == (
            "соль",
            "purchased",
        )


# --- the snapshot is independent of its source ----------------------------------------------


async def test_a_list_survives_the_conversation_it_was_copied_from(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = await saved(chat, message_body(conversation, message))

        removed = await chat.client.delete(
            f"/api/conversations/{conversation}", headers=post_headers()
        )
        assert removed.status_code == 204

        found = await chat.client.get(f"/api/shopping-lists/{body['id']}")
        assert found.status_code == 200
        assert [(item["name"], item["amount"]) for item in found.json()["items"]] == [
            ("свёкла", "2 шт."),
            ("капуста", "300 г"),
            ("говядина", "500 г"),
        ]


async def test_a_list_survives_editing_and_deleting_the_favorite_it_was_copied_from(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        favorite = await keep(chat, conversation, message)
        body = await saved(
            chat,
            {
                "source": "favorite",
                "favorite_id": favorite["id"],
                "expected_version": favorite["version"],
            },
            key="fav-1",
        )
        before = [(item["name"], item["amount"], item["state"]) for item in body["items"]]

        edited = await chat.client.patch(
            f"/api/favorites/{favorite['id']}",
            json={
                "expected_version": favorite["version"],
                "title": "Овощной борщ",
                "ingredients": [{"name": "морковь", "amount": "1 шт."}],
            },
            headers=post_headers(),
        )
        assert edited.status_code == 200
        removed = await chat.client.delete(
            f"/api/favorites/{favorite['id']}", headers=post_headers()
        )
        assert removed.status_code == 204

        found = await chat.client.get(f"/api/shopping-lists/{body['id']}")
        assert found.status_code == 200
        assert [(item["name"], item["amount"], item["state"]) for item in found.json()["items"]] == before


async def test_a_replay_still_answers_after_the_source_is_deleted(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = message_body(conversation, message)
        first = await saved(chat, body)
        assert (
            await chat.client.delete(f"/api/conversations/{conversation}", headers=post_headers())
        ).status_code == 204

        again = await save(chat, body)

        assert again.status_code == 200
        assert again.json()["id"] == first["id"]
        assert len(lists(chat)) == 1


# --- limits --------------------------------------------------------------------------------


async def test_an_item_value_that_cannot_be_stored_is_a_bad_request(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = await saved(chat, message_body(conversation, message))
        list_id, item_id = body["id"], body["items"][0]["id"]

        for method, path, payload in (
            ("post", f"/api/shopping-lists/{list_id}/items", {"name": ""}),
            ("post", f"/api/shopping-lists/{list_id}/items", {"name": "соль", "state": "lost"}),
            ("patch", f"/api/shopping-lists/{list_id}/items/{item_id}", {"name": ""}),
            ("patch", f"/api/shopping-lists/{list_id}/items/{item_id}", {"state": "lost"}),
            ("patch", f"/api/shopping-lists/{list_id}", {"status": "lost"}),
        ):
            if method == "post":
                response = await chat.client.post(
                    path, json=payload, headers=post_headers()
                )
            else:
                response = await chat.client.patch(
                    path, json=payload, headers=post_headers()
                )
            assert response.status_code == 400, (path, payload)
            assert error_of(response)["detail"] == "invalid_request"

        assert (await chat.client.get(f"/api/shopping-lists/{list_id}")).json()["items"][0][
            "name"
        ] == "свёкла"


async def test_the_item_cap_answers_the_catalogue_error(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        body = await saved(chat, message_body(conversation, message), key="first")
        # The recipe has three ingredients; fill the list to the cap directly.
        with chat.base.factory() as db, db.begin():
            for position in range(3, 200):
                db.add(
                    ShoppingListItem(
                        list_id=UUID(body["id"]),
                        position=position,
                        name=f"item {position}",
                        state="unchecked",
                        created_at=chat.clock(),
                        updated_at=chat.clock(),
                    )
                )

        response = await chat.client.post(
            f"/api/shopping-lists/{body['id']}/items",
            json={"name": "ещё"},
            headers=post_headers(),
        )

        assert response.status_code == 409
        assert error_of(response)["detail"] == "shopping_list_items_full"


async def test_the_list_cap_answers_the_catalogue_error(tmp_path):
    async with chat_app(tmp_path, backend()) as chat:
        conversation, message = await answered(chat)
        with chat.base.factory() as db, db.begin():
            for index in range(100):
                db.add(
                    ShoppingList(
                        user_id=chat.user_id,
                        title=f"Список {index}",
                        status="active",
                        source_kind="message",
                        source_message_id=message,
                        source_recipe_index=0,
                        idempotency_key=f"filler-{index}",
                        request_fingerprint=creation_fingerprint(
                            source="message",
                            conversation_id=UUID(conversation),
                            message_id=message,
                            recipe_index=0,
                        ),
                        created_at=chat.clock(),
                        updated_at=chat.clock(),
                    )
                )

        response = await save(chat, message_body(conversation, message))

        assert response.status_code == 409
        assert error_of(response)["detail"] == "shopping_lists_full"
        assert len(lists(chat)) == 100
