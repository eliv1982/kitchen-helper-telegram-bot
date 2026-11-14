"""The favorite detail's household-measure block (KH-F01-B1).

A structured favorite is kept from a real answer of a scripted assistant, so its
ingredients are the ones the protocol accepted. The detail read is the first conversion
surface: it carries the signed-in person's resolved target capacities, every quantified
volume ingredient's exact rational, its practical rounded value and the presentation the
page should draw, plus the optional ?source_glass_ml clarification. Nothing reaches a
provider; no read writes to a favorite, a recipe version, a profile or a shopping list.

The recipes here pin the owner's three corrections at the API boundary: metric volumes
become the personal glass only where they are practically usable; a household conversion
with no practical rounded value is still carried exactly; and a clarification applies to
undeclared source glasses only, without inventing a conflict with a declared 240 mL.
"""

import hashlib
from uuid import UUID

import pytest
import sqlalchemy as sa

from house_helper.core.backend import BackendReply
from house_helper.core.recipes import Ingredient, Recipe, render_recipe
from house_helper.persistence.models import FavoriteRecipe, ShoppingList, User
from tests.chat_support import ScriptedBackend, chat_app, error_of, post_headers

pytestmark = pytest.mark.asyncio

INGREDIENTS = (
    Ingredient("мука", "1 стакан"),
    Ingredient("вода", "1 стакан 240 мл"),
    Ingredient("масло", "2 ст. л."),
    Ingredient("молоко", "500 мл"),
    Ingredient("соль", "300 г"),
    Ingredient("сахар", "1 стакан 237,5 мл"),
)
RECIPE = Recipe(
    index=0,
    title="Тесто",
    servings="2 порции",
    ingredients=INGREDIENTS,
    steps=("Смешайте.",),
)


async def kept_structured(tmp_path):
    reply = BackendReply(text=render_recipe(RECIPE), recipes=(RECIPE,))
    # Two people keep their own copy of the same recipe in the per-user test.
    backend = ScriptedBackend(reply, reply)
    return chat_app(tmp_path, backend)


async def keep_recipe(chat, client=None):
    client = client or chat.client
    conversation = await chat.create(client)
    sent = await chat.send(conversation, "Рецепт теста", "k-1", client)
    assert sent.status_code == 201, sent.text
    message_id = sent.json()["assistant_message"]["id"]
    response = await client.post(
        "/api/favorites",
        json={"conversation_id": conversation, "message_id": message_id, "recipe_index": 0},
        headers=post_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def detail(chat, favorite_id, query="", client=None):
    response = await (client or chat.client).get(f"/api/favorites/{favorite_id}{query}")
    return response


async def set_measures(chat, client=None, **measures):
    response = await (client or chat.client).patch(
        "/api/profile", json={"measures": measures}, headers=post_headers()
    )
    assert response.status_code == 200, response.text
    return response


def by_position(block):
    return {item["position"]: item for item in block["ingredients"]}


# --- the additive block ------------------------------------------------------------------


async def test_a_structured_detail_carries_the_persons_resolved_target_measures(tmp_path):
    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        response = await detail(chat, favorite)

    assert response.status_code == 200
    body = response.json()
    assert body["measures"]["target"] == {
        "glass_ml": 250,
        "teaspoon_ml": 5,
        "tablespoon_ml": 15,
    }
    assert body["measures"]["source_glass_ml"] is None
    assert body["measures"]["source_glass_required"] is True


async def test_every_quantified_volume_is_present_and_mass_is_not(tmp_path):
    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        block = (await detail(chat, favorite)).json()["measures"]

    items = by_position(block)
    assert sorted(items) == [0, 1, 2, 3, 5]  # the 300 g salt has no entry
    # The undeclared glass needs a clarification and is never defaulted.
    assert items[0]["status"] == "clarification_required"
    assert items[0]["reason"] == "glass_size_required"
    assert items[0]["exact"] is None and items[0]["presentation"] == "none"
    # A declared 240 mL is exact against the 250 mL target.
    assert items[1]["status"] == "nominal_household"
    assert items[1]["exact"] == {"numerator": 24, "denominator": 25}
    # 2 tablespoons in a 15 mL target is the identity: nothing to repeat.
    assert items[2]["exact"] == {"numerator": 2, "denominator": 1}
    assert items[2]["identical"] is True and items[2]["presentation"] == "none"
    # A metric volume: 500 mL is two 250 mL glasses, with no rounding error.
    assert items[3]["source_kind"] == "metric_volume"
    assert items[3]["exact"] == {"numerator": 2, "denominator": 1}
    assert items[3]["rounded"] == {
        "numerator": 2,
        "denominator": 1,
        "relative_error": {"numerator": 0, "denominator": 1},
    }
    assert items[3]["presentation"] == "rounded"
    # A fractional declared capacity is exact 19/20 and has no practical rounded value.
    assert items[5]["exact"] == {"numerator": 19, "denominator": 20}
    assert items[5]["rounded"] is None
    assert items[5]["presentation"] == "exact"


async def test_the_persons_own_measures_change_the_same_recipe_for_that_person_only(tmp_path):
    async with await kept_structured(tmp_path) as chat:
        other = await chat.second_user()
        favorite = await keep_recipe(chat)
        theirs_favorite = await keep_recipe(chat, other)
        await set_measures(chat, glass_ml=240, teaspoon_ml=6, tablespoon_ml=20)

        mine = (await detail(chat, favorite)).json()["measures"]
        theirs = (await detail(chat, theirs_favorite, client=other)).json()["measures"]

    assert mine["target"] == {"glass_ml": 240, "teaspoon_ml": 6, "tablespoon_ml": 20}
    assert theirs["target"] == {"glass_ml": 250, "teaspoon_ml": 5, "tablespoon_ml": 15}
    mine_items = by_position(mine)
    # AC53: the person's own 240 mL glass is a TARGET measure; the recipe's undeclared
    # glass is still unknown and is never resolved from it.
    assert mine_items[0]["status"] == "clarification_required"
    assert mine_items[0]["reason"] == "glass_size_required"
    assert mine_items[0]["exact"] is None
    # 500 mL in a 240 mL glass is 25/12; the whole 2 passes the 5% gate (4% error).
    assert mine_items[3]["exact"] == {"numerator": 25, "denominator": 12}
    assert mine_items[3]["rounded"]["numerator"] == 2
    assert mine_items[3]["rounded"]["relative_error"] == {"numerator": 1, "denominator": 25}
    # 2 source tablespoons at 20 mL each is exactly 1½ of the person's tablespoon.
    assert mine_items[2]["exact"] == {"numerator": 3, "denominator": 2}
    assert mine_items[2]["presentation"] == "rounded"
    assert mine_items[2]["identical"] is False
    # The declared 240 mL now equals the person's glass: the identity is not repeated.
    assert mine_items[1]["identical"] is True and mine_items[1]["presentation"] == "none"
    # The other person's block is still the defaults.
    assert by_position(theirs)[2]["identical"] is True


# --- the source-glass clarification ---------------------------------------------------------


async def test_the_query_parameter_resolves_only_the_undeclared_glass(tmp_path):
    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        response = await detail(chat, favorite, "?source_glass_ml=200")

    assert response.status_code == 200
    block = response.json()["measures"]
    assert block["source_glass_ml"] == 200
    items = by_position(block)
    assert items[0]["status"] == "nominal_household"
    assert items[0]["reason"] == "converted"  # no artificial conflict
    assert items[0]["exact"] == {"numerator": 4, "denominator": 5}  # 200 mL of a 250 mL glass
    # The explicitly declared 240 mL keeps its own capacity.
    assert items[1]["exact"] == {"numerator": 24, "denominator": 25}
    assert items[5]["exact"] == {"numerator": 19, "denominator": 20}


async def test_a_clarification_of_250_makes_the_undeclared_glass_an_identity(tmp_path):
    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        block = (await detail(chat, favorite, "?source_glass_ml=250")).json()["measures"]

    items = by_position(block)
    assert items[0]["exact"] == {"numerator": 1, "denominator": 1}
    assert items[0]["identical"] is True and items[0]["presentation"] == "none"


async def test_another_valid_clarification_value_is_accepted(tmp_path):
    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        block = (await detail(chat, favorite, "?source_glass_ml=237")).json()["measures"]

    assert block["source_glass_ml"] == 237
    assert by_position(block)[0]["exact"] == {"numerator": 237, "denominator": 250}


@pytest.mark.parametrize(
    "query",
    [
        "?source_glass_ml=",
        "?source_glass_ml=abc",
        "?source_glass_ml=49",
        "?source_glass_ml=1001",
        "?source_glass_ml=-5",
        "?source_glass_ml=200.0",
        "?source_glass_ml=%20200",
        "?source_glass_ml=200&source_glass_ml=250",
    ],
)
async def test_an_unusable_clarification_is_refused_and_reads_nothing(tmp_path, query):
    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        before = (await detail(chat, favorite)).json()
        response = await detail(chat, favorite, query)
        after = (await detail(chat, favorite)).json()

    assert response.status_code == 400
    assert error_of(response)["detail"] == "invalid_measure"
    assert after == before


async def test_a_clarification_is_never_persisted_in_the_favorite_or_the_profile(tmp_path):
    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        profile_before = (await chat.client.get("/api/profile")).json()
        before = (await detail(chat, favorite)).json()

        assert (await detail(chat, favorite, "?source_glass_ml=200")).status_code == 200

        after = (await detail(chat, favorite)).json()
        profile_after = (await chat.client.get("/api/profile")).json()
        with chat.base.factory() as db:
            row = db.get(FavoriteRecipe, UUID(favorite))
            stored = (row.title, row.content, row.version, row.updated_at)
            user = db.get(User, chat.user_id)
            stored_measures = (
                user.measure_glass_ml,
                user.measure_teaspoon_ml,
                user.measure_tablespoon_ml,
            )
            lists = len(db.scalars(sa.select(ShoppingList)).all())

    assert after == before  # the clarification affected that one answer only
    assert after["measures"]["source_glass_ml"] is None
    assert profile_after == profile_before
    assert stored_measures == (None, None, None)
    assert lists == 0
    assert stored[0] == "Тесто" and stored[2] == 1


async def test_a_conversion_read_writes_nothing(tmp_path):
    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        with chat.base.factory() as db:
            row = db.get(FavoriteRecipe, UUID(favorite))
            before = (row.title, row.content, row.version, row.updated_at, row.ingredients)

        for query in ("", "?source_glass_ml=200", "?source_glass_ml=250"):
            assert (await detail(chat, favorite, query)).status_code == 200

        with chat.base.factory() as db:
            row = db.get(FavoriteRecipe, UUID(favorite))
            after = (row.title, row.content, row.version, row.updated_at, row.ingredients)
            assert db.scalars(sa.select(ShoppingList)).all() == []
        # Three detail reads, and the assistant was asked once: only for the answer.
        assert chat.backend.calls == 1

    assert after == before


# --- the edit answer and failures -------------------------------------------------------------


async def test_the_edit_answer_carries_the_same_measures_block(tmp_path):
    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        response = await chat.client.patch(
            f"/api/favorites/{favorite}",
            json={"expected_version": 1, "title": "Тесто для пирога"},
            headers=post_headers(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Тесто для пирога"
    assert body["measures"]["schema_version"] == 2
    assert body["measures"]["favorite_version"] == body["version"] == 2
    assert body["measures"]["target"] == {
        "glass_ml": 250,
        "teaspoon_ml": 5,
        "tablespoon_ml": 15,
    }
    assert len(body["measures"]["ingredients"]) == 5
    assert all(item["source_terms"] for item in body["measures"]["ingredients"])


async def test_a_conversion_failure_leaves_the_recipe_readable(tmp_path, monkeypatch):
    from house_helper.web import favorites_api

    def broken(*args, **kwargs):
        raise RuntimeError("conversion failed")

    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        monkeypatch.setattr(favorites_api.measure_targets, "measure_block_v2", broken)

        response = await detail(chat, favorite)

    assert response.status_code == 200
    body = response.json()
    assert body["measures"] is None  # only the conversion section is missing
    assert body["title"] == "Тесто"
    assert [item["amount"] for item in body["ingredients"]] == [i.amount for i in INGREDIENTS]


async def test_a_corrupt_profile_measure_leaves_the_recipe_readable(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from house_helper.web import favorites_api

    corrupt = SimpleNamespace(
        measures=SimpleNamespace(glass_ml=49, teaspoon_ml=None, tablespoon_ml=None)
    )
    monkeypatch.setattr(
        favorites_api.profile_store, "get_profile", lambda session, user_id: corrupt
    )
    async with await kept_structured(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        response = await detail(chat, favorite)

    assert response.status_code == 200
    body = response.json()
    assert body["measures"] is None  # no clamped or invented target
    assert body["kind"] == "structured"
    assert [item["amount"] for item in body["ingredients"]] == [
        i.amount for i in INGREDIENTS
    ]


async def test_a_text_favorite_has_an_empty_but_valid_block(tmp_path):
    backend = ScriptedBackend("Просто текст")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        sent = await chat.send(conversation, "вопрос", "k-1")
        message_id = sent.json()["assistant_message"]["id"]
        kept = await chat.client.post(
            "/api/favorites",
            json={"conversation_id": conversation, "message_id": message_id},
            headers=post_headers(),
        )
        body = (await detail(chat, kept.json()["id"])).json()

    assert body["kind"] == "text"
    block = body["measures"]
    assert block["schema_version"] == 2
    assert block["favorite_version"] == body["version"]
    assert block["target"] == {"glass_ml": 250, "teaspoon_ml": 5, "tablespoon_ml": 15}
    assert block["source_glass_ml"] is None
    assert block["source_glass_required"] is False
    assert block["steps_source_glass_required"] is False
    assert block["ingredients"] == []
    assert block["steps"] == []
    assert block["provenance"] == {
        "status": "complete",
        "declared": [],
        "incomplete": [],
    }
    assert block["analysis"]["status"] == "complete"
    assert block["analysis"]["spans_emitted"] == 0


# --- the schema_version 2 annotation block (KH-F01-B1.1-C) -----------------------------------


STEP_INGREDIENTS = (
    Ingredient("вода", "1 л"),
    Ingredient("молоко", "1 стакан (200 мл)"),
    Ingredient("соль", "300 г"),
)
STEP_RECIPE = Recipe(
    index=0,
    title="Суп",
    servings="2 порции",
    ingredients=STEP_INGREDIENTS,
    steps=(
        "Доведите 500 мл воды до кипения.",
        "Добавьте 1 стакан молока.",
        "Добавьте 250 мл воды.",
    ),
)


async def kept_with_steps(tmp_path):
    reply = BackendReply(text=render_recipe(STEP_RECIPE), recipes=(STEP_RECIPE,))
    backend = ScriptedBackend(reply, reply)
    return chat_app(tmp_path, backend)


async def test_the_detail_carries_the_complete_typed_block_and_keeps_the_b1_fields(tmp_path):
    async with await kept_with_steps(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        body = (await detail(chat, favorite)).json()

    block = body["measures"]
    assert set(block) == {
        "schema_version",
        "favorite_version",
        "target",
        "source_glass_ml",
        "source_glass_required",
        "steps_source_glass_required",
        "ingredients",
        "steps",
        "provenance",
        "analysis",
        "mass_conversions",
    }
    assert block["schema_version"] == 2
    assert block["favorite_version"] == body["version"] == 1

    # Released B1 fields are unchanged; source_terms is the additive per-term data.
    water = by_position(block)[0]
    assert (water["position"], water["source"], water["source_kind"]) == (
        0,
        "1 л",
        "metric_volume",
    )
    assert water["exact"] == {"numerator": 4, "denominator": 1}
    assert water["source_terms"] == [
        {
            "unit": "liter",
            "amount": {"numerator": 1, "denominator": 1},
            "capacity_ml": None,
            "capacity_origin": "not_applicable",
        }
    ]
    milk = by_position(block)[1]
    assert milk["source_terms"][0]["capacity_origin"] == "declared_text"

    # Every step is annotated with its exact text, hash and code-point spans.
    assert [step["step_index"] for step in block["steps"]] == [0, 1, 2]
    for step in block["steps"]:
        text = STEP_RECIPE.steps[step["step_index"]]
        assert step["step_text"] == text
        assert step["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert step["status"] == "complete"
        for span in step["spans"]:
            assert text[span["start"] : span["end"]] == span["text"]
    # OD-03 at the API boundary: the milk ingredient declares 200 mL, the step does not
    # inherit it and asks for a clarification instead.
    milk_span = block["steps"][1]["spans"][0]
    assert milk_span["capacity_origin"] == "unresolved"
    assert milk_span["conversion"]["status"] == "clarification_required"
    assert block["steps_source_glass_required"] is True

    assert block["provenance"]["status"] == "complete"
    assert block["analysis"]["status"] == "complete"
    assert block["analysis"]["spans_emitted"] == 3


async def test_the_clarification_reaches_undeclared_step_terms_only(tmp_path):
    async with await kept_with_steps(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        body = (await detail(chat, favorite, "?source_glass_ml=200")).json()

    block = body["measures"]
    assert block["source_glass_ml"] == 200
    milk_span = block["steps"][1]["spans"][0]
    assert milk_span["capacity_origin"] == "temporary_clarification"
    assert milk_span["conversion"]["exact"] == {"numerator": 4, "denominator": 5}
    # The milk ingredient keeps its own declaration: no conflict, no override.
    milk = by_position(block)[1]
    assert milk["source_terms"][0]["capacity_origin"] == "declared_text"
    assert milk["source_terms"][0]["capacity_ml"] == {"numerator": 200, "denominator": 1}


async def test_a_patch_recomputes_the_block_at_the_new_version(tmp_path):
    async with await kept_with_steps(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        first = (await detail(chat, favorite)).json()
        response = await chat.client.patch(
            f"/api/favorites/{favorite}",
            json={
                "expected_version": 1,
                "steps": [
                    "Доведите 500 мл воды до кипения.",
                    "Добавьте 250 мл молока.",
                    "Добавьте 500 мл воды.",
                ],
            },
            headers=post_headers(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    assert body["measures"]["favorite_version"] == 2
    assert body["steps"] == [
        "Доведите 500 мл воды до кипения.",
        "Добавьте 250 мл молока.",
        "Добавьте 500 мл воды.",
    ]
    edited = body["measures"]["steps"][1]["spans"][0]
    assert edited["text"] == "250 мл"
    assert edited["link"] == "linked"
    assert edited["conversion"]["exact"] == {"numerator": 1, "denominator": 1}
    # The earlier answer stays bound to the earlier version; a client discards it as stale.
    assert first["measures"]["favorite_version"] == 1
    assert first["measures"]["steps"][1]["spans"][0]["text"] == "1 стакан"


async def test_step_hints_use_only_the_signed_in_persons_target_capacity(tmp_path):
    async with await kept_with_steps(tmp_path) as chat:
        other = await chat.second_user()
        mine = await keep_recipe(chat)
        theirs = await keep_recipe(chat, other)
        await set_measures(chat, glass_ml=200)

        mine_block = (await detail(chat, mine)).json()["measures"]
        theirs_block = (await detail(chat, theirs, client=other)).json()["measures"]

    assert mine_block["steps"][0]["spans"][0]["conversion"]["exact"] == {
        "numerator": 5,
        "denominator": 2,
    }
    assert theirs_block["steps"][0]["spans"][0]["conversion"]["exact"] == {
        "numerator": 2,
        "denominator": 1,
    }
    assert (mine_block["target"]["glass_ml"], theirs_block["target"]["glass_ml"]) == (
        200,
        250,
    )


async def test_reading_step_annotations_writes_nothing_and_keeps_protocol_v1(tmp_path):
    async with await kept_with_steps(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        with chat.base.factory() as db:
            row = db.get(FavoriteRecipe, UUID(favorite))
            before = (row.content, row.version, row.updated_at, row.ingredients, row.steps)

        for query in ("", "?source_glass_ml=200"):
            assert (await detail(chat, favorite, query)).status_code == 200

        with chat.base.factory() as db:
            row = db.get(FavoriteRecipe, UUID(favorite))
            after = (row.content, row.version, row.updated_at, row.ingredients, row.steps)
        detail_body = (await detail(chat, favorite)).json()

    assert after == before
    # The stored protocol v1 fields are echoed exactly; the block is additive only.
    assert detail_body["steps"] == list(STEP_RECIPE.steps)
    assert detail_body["ingredients"] == [
        {"name": item.name, "amount": item.amount} for item in STEP_INGREDIENTS
    ]
    assert chat.backend.calls == 1  # one answer; every detail read is derived locally
