"""KH-F01-B2-2: the ingredient-specific mass conversion extension of a favorite detail.

The extension lives in its own key, `measures.mass_conversions`, with its own
`schema_version: 1`. It is checked at both levels that matter: the adapter block
(`measure_targets.measure_block_v2`) for its exact shape and its failure boundaries, and the
real HTTP detail (`GET`/`PATCH /api/favorites/{id}`) for serialization, personal measures
and account isolation.

The shipped registry approves no reference, so every number asserted here comes from an
approved synthetic fixture built in memory: fixture aliases say "test", fixture evidence
says it is not food data, and no value in this file claims to be a real measurement.
Real-registry cases assert identities, statuses, reasons and the registry revision only.

The released B1.1 portion of the block is compared before and after the extension and must
stay identical, including the frozen `conversion_omitted_reason: "mass"` of a linked step
span.
"""

import json
import logging
import re
from contextlib import contextmanager
from dataclasses import fields, replace
from fractions import Fraction

import pytest

import house_helper.web.measure_targets as measure_targets
from house_helper.core.backend import BackendReply
from house_helper.core.ingredient_reference import (
    REGISTRY_REVISION,
    Derivation,
    MassBasis,
    MassConversionReason,
    MassConversionStatus,
    MatchStatus,
    MeasurementEvidence,
    PieceState,
    ReferenceKind,
    VolumeReference,
    convert_ingredient_mass,
)
from house_helper.core.measures import parse_quantity
from house_helper.core.recipes import Ingredient, Recipe, render_recipe
from house_helper.persistence.models import FavoriteRecipe, ShoppingList
from house_helper.web.measure_targets import measure_block_v2
from tests.chat_support import ScriptedBackend, chat_app, post_headers
from tests.test_ingredient_reference import (
    APPROVED,
    build_registry,
    make_entry,
    piece,
    piece_reference,
    potato_registry,
)

DEFAULT = measure_targets.volumes()

# The released fields of the block. Everything the extension adds must stay outside them.
RELEASED_KEYS = (
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
)

INGREDIENTS = (
    Ingredient("тестовая мука", "125 г"),
    Ingredient("вода", "1 стакан 200 мл"),
    Ingredient("соль", "300 г"),
    Ingredient("мука пшеничная", "40 г"),
    Ingredient("мука", "1 кг"),
)
STEPS = (
    "Влейте 1 стакан 200 мл воды.",
    "Добавьте 100 г муки.",
)
RECIPE = Recipe(
    index=0,
    title="Тесто",
    servings="2 порции",
    ingredients=INGREDIENTS,
    steps=STEPS,
)


# --- synthetic registries -------------------------------------------------------------
#
# Every record is synthetic; no coefficient below is a real ingredient measurement.


def cup_registry():
    """One approved mass-to-volume reference for a fixture flour (1 cup = 120 g, 240 mL)."""
    entry = make_entry("test_flour", "тестовая мука", "мука")
    evidence = MeasurementEvidence(
        evidence_id="fixture.cup.volume",
        source_id="fixture.source",
        quoted="SYNTHETIC TEST FIXTURE, not food reference evidence",
        original_amount=Fraction(1),
        original_unit="us_cup",
        original_volume_ml=Fraction(240),
        mass_g=Fraction(120),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="test fixture only",
        limitations="synthetic; never a real coefficient",
        normalization="US cup = 240 mL (test fixture)",
    )
    volume = VolumeReference(
        ingredient_id="test_flour",
        evidence_id="fixture.cup.volume",
        grams_per_ml=Fraction(1, 2),
        derivation=Derivation.SOURCE_CUP_NORMALIZED,
        status=APPROVED,
    )
    return build_registry([entry], volumes=[volume], evidence=(evidence,))


def point_registry(weight_g=200):
    """One approved individual-piece weight: a point reference, no interval."""
    entry = make_entry(
        "test_root",
        "тестовый корень",
        state=PieceState.RAW,
        basis=MassBasis.AS_PURCHASED,
        piece_id="test_root",
    )
    return build_registry(
        [entry],
        pieces=[piece("test_root")],
        piece_references=[
            piece_reference("test_root", "test_root", weight_min_g=weight_g)
        ],
    )


def interval_registry(low_g=150, high_g=170):
    """One approved measured interval for a single piece (a source-stated range)."""
    entry = make_entry(
        "test_root",
        "тестовый корень",
        state=PieceState.RAW,
        basis=MassBasis.AS_PURCHASED,
        piece_id="test_root",
    )
    return build_registry(
        [entry],
        pieces=[piece("test_root")],
        piece_references=[
            piece_reference(
                "test_root",
                "test_root",
                kind=ReferenceKind.MEASURED_INTERVAL,
                weight_min_g=low_g,
                weight_max_g=high_g,
            )
        ],
    )


def calibration_registry(*, default=False):
    """One supplier size grade: sizing evidence, with or without an owner default class."""
    entry = make_entry(
        "test_fillet",
        "тестовое филе",
        state=PieceState.RAW,
        basis=MassBasis.EDIBLE_PORTION,
        piece_id="test_fillet",
    )
    return build_registry(
        [entry],
        pieces=[piece("test_fillet", basis=MassBasis.EDIBLE_PORTION)],
        piece_references=[
            piece_reference(
                "test_fillet",
                "test_fillet",
                kind=ReferenceKind.CALIBRATION_CLASS,
                size_class="medium",
                weight_min_g=150,
                weight_max_g=170,
                default=default,
                label="если филе среднее" if default else None,
            )
        ],
    )


# --- helpers --------------------------------------------------------------------------


def block(ingredients, steps=(), target=DEFAULT, registry=None, **kwargs):
    return measure_block_v2(
        ingredients,
        steps,
        target,
        favorite_version=1,
        reference_registry=registry,
        **kwargs,
    )


def section(result):
    return result["mass_conversions"]


def items(result):
    return {item["position"]: item for item in section(result)["items"]}


def released(result):
    return {key: result[key] for key in RELEASED_KEYS}


# --- the successful shapes -----------------------------------------------------------


def test_an_approved_volume_reference_is_serialized_with_its_evidence():
    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    (item,) = section(result)["items"]
    # 125 g at 1/2 g per mL is 250 mL, exactly the person's 250 mL glass.
    assert item == {
        "position": 0,
        "name": "тестовая мука",
        "source": "125 г",
        "mass_g": {"numerator": 125, "denominator": 1},
        "ingredient": {
            "id": "test_flour",
            "display_name": "test_flour",
            "match": "matched",
        },
        "status": "reference_approximate",
        "reason": "converted",
        "conditional": False,
        "assumption": None,
        "conversion": {
            "kind": "volume",
            "target_unit": "glass",
            "exact": {"numerator": 1, "denominator": 1},
            "rounded": {
                "numerator": 1,
                "denominator": 1,
                "relative_error": {"numerator": 0, "denominator": 1},
            },
            "reference": {
                "evidence_id": "fixture.cup.volume",
                "derivation": "source_cup_normalized",
                "normalization": "US cup = 240 mL (test fixture)",
                "method": "not_stated",
            },
        },
    }


def test_an_approved_point_reference_is_one_count_and_never_an_interval():
    result = block((Ingredient("тестовый корень", "300 г"),), registry=point_registry(200))

    conversion = items(result)[0]["conversion"]
    assert conversion["kind"] == "count"
    assert conversion["display"] == "single"
    # 300 g / 200 g is 3/2 pieces; a point weight never fabricates a second bound.
    assert conversion["count_low"] == {"numerator": 3, "denominator": 2}
    assert conversion["count_high"] == {"numerator": 3, "denominator": 2}
    assert conversion["rounded_low"] == {"numerator": 3, "denominator": 2}
    assert conversion["rounded_high"] == {"numerator": 3, "denominator": 2}
    assert conversion["relative_width"] == {"numerator": 0, "denominator": 1}
    assert conversion["piece"] == {
        "id": "test_root",
        "size_class": None,
        "reference_kind": "point",
    }
    assert conversion["reference"] == {"evidence_id": "fixture.piece"}


def test_an_approved_measured_interval_keeps_its_source_bounds():
    result = block((Ingredient("тестовый корень", "500 г"),), registry=interval_registry())

    conversion = items(result)[0]["conversion"]
    assert conversion["display"] == "interval"
    # 500 g against 150--170 g: [500/170, 500/150], a source-stated range of one piece.
    assert conversion["count_low"] == {"numerator": 50, "denominator": 17}
    assert conversion["count_high"] == {"numerator": 10, "denominator": 3}
    assert conversion["rounded_low"] == {"numerator": 3, "denominator": 1}
    assert conversion["rounded_high"] == {"numerator": 7, "denominator": 2}
    # (high - low) / midpoint = 1/8, inside the 1/4 usability bound.
    assert conversion["relative_width"] == {"numerator": 1, "denominator": 8}
    assert conversion["piece"]["reference_kind"] == "measured_interval"


def test_an_approved_calibration_class_with_a_default_is_an_explicit_assumption():
    result = block(
        (Ingredient("тестовое филе", "500 г"),),
        registry=calibration_registry(default=True),
    )

    (item,) = section(result)["items"]
    assert item["status"] == "reference_approximate"
    # The estimate is conditional and names what it assumed; the engine never selects a
    # class silently, so the payload must carry the assumption the person has to see.
    assert item["conditional"] is True
    assert item["assumption"] == {
        "kind": "size_class",
        "size_class": "medium",
        "weight_g": {"numerator": 150, "denominator": 1},
        "mass_basis": "edible_portion",
        "label_ru": "если филе среднее",
    }
    assert item["conversion"]["piece"] == {
        "id": "test_fillet",
        "size_class": "medium",
        "reference_kind": "calibration_class",
    }


def test_two_people_with_different_glasses_get_different_counts_of_the_same_mass():
    ingredients = (Ingredient("тестовая мука", "125 г"),)

    fifty = items(
        block(ingredients, target=measure_targets.volumes(glass_ml=50), registry=cup_registry())
    )[0]
    two_hundred = items(
        block(ingredients, target=measure_targets.volumes(glass_ml=200), registry=cup_registry())
    )[0]

    assert fifty["conversion"]["target_unit"] == "glass"
    assert fifty["conversion"]["exact"] == {"numerator": 5, "denominator": 1}
    assert two_hundred["conversion"]["target_unit"] == "glass"
    assert two_hundred["conversion"]["exact"] == {"numerator": 5, "denominator": 4}
    # 250 mL is not a "cup" constant here: it is each person's own glass capacity.
    assert fifty["source"] == two_hundred["source"] == "125 г"


def test_the_registry_revision_and_the_evidence_identifier_travel_with_the_result():
    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["schema_version"] == 1
    assert section(result)["registry_revision"] == "test-fixture"
    assert section(result)["items"][0]["conversion"]["reference"]["evidence_id"] == (
        "fixture.cup.volume"
    )


def test_the_shipped_registry_reports_its_own_revision_and_no_number():
    result = block((Ingredient("мука пшеничная", "40 г"),))

    (item,) = section(result)["items"]
    assert section(result)["registry_revision"] == REGISTRY_REVISION
    assert item["status"] == "unsupported"
    assert item["reason"] == "reference_not_approved"
    assert item["conversion"] is None


# --- refusals -------------------------------------------------------------------------


REFUSALS = [
    # An unapproved record is never activated by being recognized or structurally valid.
    (
        (Ingredient("мука пшеничная", "40 г"),),
        None,
        "matched",
        "unsupported",
        "reference_not_approved",
    ),
    (
        (Ingredient("нечто неизвестное", "100 г"),),
        None,
        "unrecognized",
        "unsupported",
        "ingredient_not_recognized",
    ),
    ((Ingredient("соль", "300 г"),), None, "generic", "ambiguous", "generic_name"),
    (
        (Ingredient("тестовая мука без кожи с кожей", "100 г"),),
        cup_registry(),
        "qualifier_conflict",
        "ambiguous",
        "qualifier_conflict",
    ),
]


@pytest.mark.parametrize("ingredients, registry, match, status, reason", REFUSALS)
def test_a_refusal_names_its_closed_reason_and_carries_no_payload(
    ingredients, registry, match, status, reason
):
    result = block(ingredients, registry=registry)

    (item,) = section(result)["items"]
    assert item["ingredient"]["match"] == match
    assert (item["status"], item["reason"]) == (status, reason)
    assert item["conversion"] is None
    assert item["conditional"] is False and item["assumption"] is None
    # The verbatim name and amount are still there; the item is exactly the closed shape,
    # so no count, volume or rounded value can have leaked with the refusal.
    assert (item["name"], item["source"]) == (ingredients[0].name, ingredients[0].amount)
    assert set(item) == {
        "position",
        "name",
        "source",
        "mass_g",
        "ingredient",
        "status",
        "reason",
        "conditional",
        "assumption",
        "conversion",
    }


def test_a_recognized_but_unapproved_entry_keeps_its_identity_and_no_number():
    result = block((Ingredient("мука пшеничная", "40 г"),))

    (item,) = section(result)["items"]
    assert item["ingredient"] == {
        "id": "wheat_flour_all_purpose",
        "display_name": "мука пшеничная",
        "match": "matched",
    }
    assert item["conversion"] is None


def test_an_as_purchased_mass_never_uses_an_edible_portion_reference():
    # The fixture's reference weighs the edible root; the stored name states no edible basis.
    result = block(
        (Ingredient("тестовый корень", "600 г"), Ingredient("тестовый корень очищенный", "600 г")),
        registry=potato_registry(default=True),
    )

    unqualified, edible = items(result)[0], items(result)[1]
    assert (unqualified["status"], unqualified["reason"]) == ("unsupported", "state_mismatch")
    assert unqualified["conversion"] is None
    # The explicit edible phrase matches, and the result is conditional on the size class.
    assert edible["status"] == "reference_approximate"
    assert edible["conditional"] is True
    assert edible["assumption"]["label_ru"] == "если корни средние"


def test_a_class_with_no_default_requires_a_clarification_instead_of_a_guess():
    result = block((Ingredient("тестовое филе", "500 г"),), registry=calibration_registry())

    (item,) = section(result)["items"]
    assert (item["status"], item["reason"]) == (
        "clarification_required",
        "size_class_required",
    )
    assert item["conversion"] is None and item["assumption"] is None


def test_a_broad_interval_is_refused_rather_than_presented():
    result = block((Ingredient("тестовый корень", "500 г"),), registry=interval_registry(100, 200))

    (item,) = section(result)["items"]
    assert (item["status"], item["reason"]) == ("unsupported", "interval_too_wide")
    assert item["conversion"] is None


# --- what qualifies for an item at all ------------------------------------------------


def test_only_a_quantified_mass_gets_an_item():
    result = block(
        (
            Ingredient("вода", "1 стакан 200 мл"),
            Ingredient("масло", "2 ст. л."),
            Ingredient("соль", "по вкусу"),
            Ingredient("яйца", "2 яйца"),
            Ingredient("мука", "1 кг 200 г"),
        ),
        registry=cup_registry(),
    )

    # Only the compound mass qualifies; the volume, the qualitative phrase and the count
    # keep the released behavior and produce no B2 item.
    assert [item["position"] for item in section(result)["items"]] == [4]
    assert section(result)["items"][0]["mass_g"] == {"numerator": 1200, "denominator": 1}


def test_an_ingredient_with_no_mass_makes_the_section_complete_and_empty():
    result = block((Ingredient("вода", "500 мл"),))

    assert section(result) == {
        "schema_version": 1,
        "registry_revision": REGISTRY_REVISION,
        "status": "complete",
        "items": [],
        "omitted": {"unsafe_fraction": 0, "malformed": 0},
    }


def test_a_mass_equal_to_zero_is_refused_by_its_own_reason():
    result = block((Ingredient("мука пшеничная", "0 г"),))

    (item,) = section(result)["items"]
    assert (item["status"], item["reason"]) == ("unsupported", "zero_mass")


# --- failure boundaries ---------------------------------------------------------------


def test_an_unsafe_rational_omits_only_its_own_item_and_is_counted():
    huge = "9007199254740993 г"  # 2**53 + 1 grams: not a JavaScript-safe integer

    result = block(
        (Ingredient("мука пшеничная", huge), Ingredient("тестовая мука", "125 г")),
        registry=cup_registry(),
    )

    assert [item["position"] for item in section(result)["items"]] == [1]
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 1, "malformed": 0}
    # The dropped item is not counted against the released analysis block.
    assert result["analysis"]["omitted_reasons"]["unsafe_fraction"] == 0


def test_an_unexpected_engine_failure_drops_the_item_and_never_invents_a_result(monkeypatch):
    def broken(name, quantity, **kwargs):
        raise RuntimeError("a programming defect, not a conversion outcome")

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", broken)
    result = block((Ingredient("тестовая мука", "125 г"), Ingredient("вода", "1 стакан")), registry=cup_registry())

    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 2}
    assert section(result)["items"] == []
    # The released block is untouched by the B2 failure.
    assert [item["position"] for item in result["ingredients"]] == [1]


def test_a_section_level_failure_yields_an_empty_failed_section(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("the section itself failed")

    monkeypatch.setattr(measure_targets, "_mass_annotations", broken)
    result = block((Ingredient("тестовая мука", "125 г"), Ingredient("вода", "500 мл")), registry=cup_registry())

    assert section(result) == {
        "schema_version": 1,
        "registry_revision": "test-fixture",
        "status": "failed",
        "items": [],
        "omitted": {"unsafe_fraction": 0, "malformed": 0},
    }
    assert [item["position"] for item in result["ingredients"]] == [1]
    assert result["analysis"]["status"] == "complete"


def test_an_invalid_source_glass_configuration_still_nulls_the_whole_block():
    # The released behavior is unchanged: an invalid personal/declared capacity raises from
    # the B1 path before B2 runs, so the caller serves the recipe with `measures: null`.
    with pytest.raises(ValueError):
        block((Ingredient("тестовая мука", "125 г"),), source_glass_ml=40, registry=cup_registry())  # type: ignore[call-arg]


def test_the_registry_argument_must_be_a_registry():
    with pytest.raises(TypeError):
        measure_block_v2((), (), DEFAULT, favorite_version=1, reference_registry={})  # type: ignore[arg-type]


# --- the released block is unchanged --------------------------------------------------


def test_the_released_portion_is_identical_with_and_without_the_extension():
    ingredients = (Ingredient("тестовая мука", "125 г"), Ingredient("вода", "1 стакан 200 мл"))
    steps = ("Влейте 1 стакан 200 мл воды и добавьте 125 г муки.",)

    with_fixture = block(ingredients, steps, registry=cup_registry())
    with_shipped = block(ingredients, steps)

    assert released(with_fixture) == released(with_shipped)
    assert set(with_fixture) == set(RELEASED_KEYS) | {"mass_conversions"}
    # The outer version is the released one; only the extension has its own.
    assert with_fixture["schema_version"] == 2
    assert section(with_fixture)["schema_version"] == 1


def test_a_mass_ingredient_still_has_no_released_ingredient_annotation():
    result = block((Ingredient("тестовая мука", "125 г"), Ingredient("вода", "500 мл")), registry=cup_registry())

    assert [item["position"] for item in result["ingredients"]] == [1]
    assert items(result)[0]["status"] == "reference_approximate"


def test_a_linked_mass_step_span_keeps_the_frozen_mass_omission():
    result = block((Ingredient("мука", "1 кг"),), ("Добавьте 100 г муки.",), registry=cup_registry())

    span = result["steps"][0]["spans"][0]
    assert span["link"] == "linked"
    assert span["conversion"] is None
    assert span["conversion_omitted_reason"] == "mass"
    # The same stored mass converts in the B2 extension, and only there.
    (item,) = section(result)["items"]
    assert item["conversion"]["target_unit"] == "glass"


def test_the_stored_name_and_amount_are_preserved_verbatim():
    stored = (Ingredient("тестовая  мука", "125 г"),)  # the doubled space is the stored text
    result = block(stored, registry=cup_registry())

    (item,) = section(result)["items"]
    assert item["name"] == "тестовая  мука"
    assert item["source"] == "125 г"
    assert result["mass_conversions"]["items"][0]["name"] == stored[0].name


def test_the_recipe_protocol_stays_v1():
    assert [field.name for field in fields(Ingredient)] == ["name", "amount"]
    assert [field.name for field in fields(Recipe)] == [
        "index",
        "title",
        "servings",
        "ingredients",
        "steps",
    ]
    text = render_recipe(RECIPE)
    assert text.splitlines()[:3] == ["Тесто", "2 порции", ""]
    assert "- тестовая мука — 125 г" in text


def test_the_extension_is_deterministic():
    ingredients = (Ingredient("тестовая мука", "125 г"), Ingredient("соль", "300 г"))
    assert block(ingredients, STEPS, registry=cup_registry()) == block(
        ingredients, STEPS, registry=cup_registry()
    )


# --- the HTTP detail ------------------------------------------------------------------


async def kept_recipe(tmp_path):
    reply = BackendReply(text=render_recipe(RECIPE), recipes=(RECIPE,))
    # Two people keep their own copy of the same recipe in the per-account test.
    return chat_app(tmp_path, ScriptedBackend(reply, reply))


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


async def detail(chat, favorite_id, client=None):
    response = await (client or chat.client).get(f"/api/favorites/{favorite_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def set_measures(chat, client=None, **measures):
    response = await (client or chat.client).patch(
        "/api/profile", json={"measures": measures}, headers=post_headers()
    )
    assert response.status_code == 200, response.text
    return response


def section_by_position(block_value):
    return {
        item["position"]: item
        for item in block_value["mass_conversions"]["items"]
    }


@pytest.mark.asyncio
async def test_the_detail_serializes_an_approved_conversion_and_its_companions(tmp_path, monkeypatch):
    monkeypatch.setattr(measure_targets, "_reference_registry", lambda: cup_registry())
    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        body = await detail(chat, favorite)

    measures = body["measures"]
    assert measures["schema_version"] == 2
    assert measures["mass_conversions"]["schema_version"] == 1
    assert measures["mass_conversions"]["registry_revision"] == "test-fixture"

    found = section_by_position(measures)
    # Every quantified mass gets one item; the volume at position 1 gets none. The fixture
    # converts the two masses of its own flour and knows nothing of the real names.
    assert sorted(found) == [0, 2, 3, 4]
    assert found[0]["conversion"]["kind"] == "volume"
    assert found[0]["conversion"]["target_unit"] == "glass"
    assert found[0]["conversion"]["exact"] == {"numerator": 1, "denominator": 1}
    assert found[4]["conversion"]["exact"] == {"numerator": 8, "denominator": 1}
    assert (found[2]["status"], found[2]["reason"]) == ("unsupported", "ingredient_not_recognized")
    assert found[2]["conversion"] is None
    assert (found[3]["status"], found[3]["reason"]) == ("unsupported", "ingredient_not_recognized")
    assert found[3]["conversion"] is None
    # The stored recipe and the released arrays are untouched.
    assert body["ingredients"] == [
        {"name": item.name, "amount": item.amount} for item in INGREDIENTS
    ]
    assert [item["position"] for item in measures["ingredients"]] == [1]
    assert measures["steps"][0]["step_text"] == STEPS[0]


@pytest.mark.asyncio
async def test_the_shipped_registry_serves_reasons_and_never_a_number(tmp_path):
    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        body = await detail(chat, favorite)

    measures = body["measures"]
    assert measures["mass_conversions"]["registry_revision"] == REGISTRY_REVISION
    found = section_by_position(measures)
    assert sorted(found) == [0, 2, 3, 4]
    assert (found[0]["status"], found[0]["reason"]) == ("unsupported", "ingredient_not_recognized")
    assert (found[2]["status"], found[2]["reason"]) == ("ambiguous", "generic_name")
    assert (found[3]["status"], found[3]["reason"]) == ("unsupported", "reference_not_approved")
    assert (found[4]["status"], found[4]["reason"]) == ("ambiguous", "generic_name")
    for item in found.values():
        assert item["conversion"] is None
    # No count, volume or rounded value anywhere in the section.
    assert "count_low" not in str(measures["mass_conversions"])


@pytest.mark.asyncio
async def test_each_account_sees_its_own_glass_and_never_the_others(tmp_path, monkeypatch):
    monkeypatch.setattr(measure_targets, "_reference_registry", lambda: cup_registry())
    async with await kept_recipe(tmp_path) as chat:
        other = await chat.second_user()
        mine = await keep_recipe(chat)
        theirs = await keep_recipe(chat, other)
        await set_measures(chat, glass_ml=50)
        await set_measures(chat, client=other, glass_ml=200)

        mine_body = await detail(chat, mine)
        theirs_body = await detail(chat, theirs, client=other)

    my_item = section_by_position(mine_body["measures"])[0]
    their_item = section_by_position(theirs_body["measures"])[0]
    assert my_item["conversion"]["exact"] == {"numerator": 5, "denominator": 1}
    assert their_item["conversion"]["exact"] == {"numerator": 5, "denominator": 4}
    assert mine_body["measures"]["target"]["glass_ml"] == 50
    assert theirs_body["measures"]["target"]["glass_ml"] == 200
    assert mine_body["id"] != theirs_body["id"]


@pytest.mark.asyncio
async def test_the_edit_answer_recomputes_the_extension_at_the_new_version(tmp_path, monkeypatch):
    monkeypatch.setattr(measure_targets, "_reference_registry", lambda: cup_registry())
    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        response = await chat.client.patch(
            f"/api/favorites/{favorite}",
            json={"expected_version": 1, "title": "Тесто для пирога"},
            headers=post_headers(),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 2
    found = section_by_position(body["measures"])
    assert found[0]["conversion"]["exact"] == {"numerator": 1, "denominator": 1}
    # Editing the title never edits a stored ingredient or amount.
    assert body["ingredients"] == [
        {"name": item.name, "amount": item.amount} for item in INGREDIENTS
    ]


@pytest.mark.asyncio
async def test_reading_a_favorite_writes_nothing_and_calls_no_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(measure_targets, "_reference_registry", lambda: cup_registry())
    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        stored_before = [(row.id, row.content, row.version) for row in chat.rows(FavoriteRecipe)]
        calls_before = chat.backend.calls
        lists_before = chat.rows(ShoppingList)

        await detail(chat, favorite)

        assert [
            (row.id, row.content, row.version) for row in chat.rows(FavoriteRecipe)
        ] == stored_before
        assert chat.rows(ShoppingList) == lists_before
        assert chat.backend.calls == calls_before


@pytest.mark.asyncio
async def test_a_corrupt_profile_still_serves_the_recipe_without_measures(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from house_helper.web import favorites_api

    monkeypatch.setattr(measure_targets, "_reference_registry", lambda: cup_registry())
    corrupt = SimpleNamespace(
        display_name=None,
        measures=SimpleNamespace(glass_ml=40, teaspoon_ml=5, tablespoon_ml=15),
    )
    monkeypatch.setattr(
        favorites_api.profile_store, "get_profile", lambda session, user_id: corrupt
    )
    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        body = await detail(chat, favorite)

    # The released rule wins: an invalid B1.1 target is not rescued by, or blamed on, B2.
    assert body["measures"] is None
    assert body["ingredients"] == [
        {"name": item.name, "amount": item.amount} for item in INGREDIENTS
    ]


# --- KH-F01-B2-2-R1: F1 -- malformed results at the HTTP boundary ---------------------
#
# The engine is synthetic here too: every corruption below is a fixture, and the point is
# that the adapter -- not the JSON encoder -- is where it is confined.


def real_conversion(name="тестовая мука", amount="125 г", registry=None):
    """One real engine result for the fixture registry, before any corruption."""
    return convert_ingredient_mass(
        name,
        parse_quantity(amount, allow_nonstandard_glass_sizes=True),
        target=DEFAULT,
        registry=cup_registry() if registry is None else registry,
    )


def corrupt_successful_results(monkeypatch, corrupt):
    """Route every successful engine result through `corrupt`, leaving refusals alone."""
    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        result = real(name, quantity, **kwargs)
        if result is None or result.conversion is None:
            return result
        return corrupt(result)

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    return patched


def test_a_malformed_evidence_identifier_drops_only_its_item(monkeypatch):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result, conversion=replace(result.conversion, evidence_id=object())
        ),
    )

    result = block(
        (Ingredient("тестовая мука", "125 г"), Ingredient("вода", "1 стакан 200 мл")),
        registry=cup_registry(),
    )

    assert section(result)["items"] == []
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}
    # The released B1.1 annotation of the volume ingredient is untouched.
    assert [item["position"] for item in result["ingredients"]] == [1]


def test_a_refusal_that_carries_a_conversion_is_dropped(monkeypatch):
    successful = real_conversion()
    contradictory = replace(
        successful,
        status=MassConversionStatus.UNSUPPORTED,
        reason=MassConversionReason.REFERENCE_NOT_APPROVED,
    )
    # The real engine never does this; the fixture proves the adapter refuses it.
    assert contradictory.conversion is not None
    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        if name == "тестовая мука":
            return contradictory
        return real(name, quantity, **kwargs)

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    result = block(
        (Ingredient("тестовая мука", "125 г"), Ingredient("соль", "300 г")),
        registry=cup_registry(),
    )

    # The valid refusal survives; the contradictory item never reaches the payload.
    assert [item["position"] for item in section(result)["items"]] == [1]
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_converter_returning_none_for_a_quantified_mass_is_malformed(monkeypatch):
    monkeypatch.setattr(
        measure_targets, "convert_ingredient_mass", lambda *args, **kwargs: None
    )

    result = block(
        (Ingredient("тестовая мука", "125 г"), Ingredient("вода", "500 мл")),
        registry=cup_registry(),
    )

    # `None` is right for the volume, but not for the mass: it is counted, and the section
    # is not a misleading `complete` with nothing omitted.
    assert section(result)["items"] == []
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}
    assert [item["position"] for item in result["ingredients"]] == [1]


def test_a_malformed_item_does_not_remove_an_unrelated_valid_item(monkeypatch):
    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        result = real(name, quantity, **kwargs)
        if result is not None and result.conversion is not None and result.mass_g == 125:
            return replace(
                result, conversion=replace(result.conversion, evidence_id=object())
            )
        return result

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    result = block(
        (Ingredient("тестовая мука", "125 г"), Ingredient("тестовая мука", "100 г")),
        registry=cup_registry(),
    )

    assert [item["position"] for item in section(result)["items"]] == [1]
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}
    assert section(result)["status"] == "incomplete"


@pytest.mark.parametrize(
    "field",
    ["evidence_id", "normalization", "method", "target_unit"],
)
def test_a_nested_value_of_the_wrong_type_is_malformed(monkeypatch, field):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result, conversion=replace(result.conversion, **{field: object()})
        ),
    )

    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.5, "1/2"])
def test_a_non_exact_conversion_number_is_malformed_not_coerced(monkeypatch, value):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result, conversion=replace(result.conversion, exact_count=value)
        ),
    )

    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_successful_status_with_a_refusal_reason_is_malformed(monkeypatch):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result, reason=MassConversionReason.REFERENCE_NOT_APPROVED
        ),
    )

    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_refusal_reason_with_the_wrong_status_is_malformed(monkeypatch):
    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        result = real(name, quantity, **kwargs)
        if result is None or result.conversion is not None:
            return result
        # "generic_name" belongs to the ambiguous family, never to unsupported.
        return replace(result, status=MassConversionStatus.UNSUPPORTED)

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    # The shipped registry calls "соль" generic, which is an ambiguous outcome.
    result = block((Ingredient("соль", "300 г"),))

    assert section(result)["items"] == []
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}
    assert (result["mass_conversions"]["registry_revision"]) == REGISTRY_REVISION


def test_a_conditional_flag_without_an_assumption_is_malformed(monkeypatch):
    corrupt_successful_results(
        monkeypatch, lambda result: replace(result, conditional=True, assumption=None)
    )

    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_the_json_gate_refuses_a_non_finite_value():
    with pytest.raises(measure_targets._MalformedMassResult):
        measure_targets._json_ready_mass_item({"value": float("inf")})


@pytest.mark.asyncio
async def test_a_malformed_evidence_identifier_cannot_make_the_detail_get_fail(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(measure_targets, "_reference_registry", lambda: cup_registry())
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result, conversion=replace(result.conversion, evidence_id=object())
        ),
    )

    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        response = await chat.client.get(f"/api/favorites/{favorite}")

    assert response.status_code == 200, response.text
    body = response.json()
    measures = body["measures"]
    assert measures["schema_version"] == 2
    # The two conversions were dropped; the two genuine refusals are still there.
    assert sorted(section_by_position(measures)) == [2, 3]
    assert measures["mass_conversions"]["status"] == "incomplete"
    assert measures["mass_conversions"]["omitted"] == {
        "unsafe_fraction": 0,
        "malformed": 2,
    }
    # The released arrays and the stored recipe are untouched.
    assert [item["position"] for item in measures["ingredients"]] == [1]
    assert measures["steps"][0]["step_text"] == STEPS[0]
    assert body["ingredients"] == [
        {"name": item.name, "amount": item.amount} for item in INGREDIENTS
    ]


@pytest.mark.asyncio
async def test_a_malformed_result_cannot_make_a_successful_patch_fail(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(measure_targets, "_reference_registry", lambda: cup_registry())
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result, conversion=replace(result.conversion, evidence_id=object())
        ),
    )

    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        response = await chat.client.patch(
            f"/api/favorites/{favorite}",
            json={"expected_version": 1, "title": "Тесто для пирога"},
            headers=post_headers(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # The write happened, and the answer describes that same stored version.
        assert body["version"] == 2
        assert body["title"] == "Тесто для пирога"
        assert body["measures"]["mass_conversions"]["omitted"] == {
            "unsafe_fraction": 0,
            "malformed": 2,
        }
        assert sorted(section_by_position(body["measures"])) == [2, 3]
        reread = await detail(chat, favorite)

    assert (reread["version"], reread["title"]) == (body["version"], body["title"])
    assert reread["measures"]["mass_conversions"] == (
        body["measures"]["mass_conversions"]
    )


def test_an_invalid_assumption_value_is_dropped_not_serialized(monkeypatch):
    from house_helper.core.ingredient_reference import SizeAssumption

    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        result = real(name, quantity, **kwargs)
        if result is None or result.conversion is None:
            return result
        return replace(
            result,
            conditional=True,
            assumption=SizeAssumption(
                kind="size_class",
                size_class=object(),
                weight_g=result.mass_g,
                mass_basis=MassBasis.NOT_APPLICABLE,
                label_ru=object(),
            ),
        )

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


# --- KH-F01-B2-2-R1: F2 -- registry acquisition inside the boundary -------------------


def test_a_registry_failure_is_an_isolated_failed_section(monkeypatch):
    def broken():
        raise RuntimeError("the registry could not be built")

    monkeypatch.setattr(measure_targets, "_reference_registry", broken)
    result = block(
        (Ingredient("тестовая мука", "125 г"), Ingredient("вода", "1 стакан 200 мл"))
    )

    assert section(result) == {
        "schema_version": 1,
        "registry_revision": None,
        "status": "failed",
        "items": [],
        "omitted": {"unsafe_fraction": 0, "malformed": 0},
    }
    # No revision is claimed for a registry that was never obtained.
    assert section(result)["registry_revision"] is None
    # The released fields, including the B1.1 volume annotation, are unchanged.
    assert result["schema_version"] == 2
    assert [item["position"] for item in result["ingredients"]] == [1]


@pytest.mark.asyncio
async def test_a_registry_failure_keeps_the_detail_get_successful(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("the registry could not be built")

    monkeypatch.setattr(measure_targets, "_reference_registry", broken)
    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        response = await chat.client.get(f"/api/favorites/{favorite}")

    assert response.status_code == 200, response.text
    body = response.json()
    measures = body["measures"]
    assert measures is not None
    assert measures["schema_version"] == 2
    assert measures["mass_conversions"] == {
        "schema_version": 1,
        "registry_revision": None,
        "status": "failed",
        "items": [],
        "omitted": {"unsafe_fraction": 0, "malformed": 0},
    }
    assert measures["mass_conversions"]["registry_revision"] != REGISTRY_REVISION
    # B1.1, the steps and the stored recipe survive the B2 domain failing.
    assert [item["position"] for item in measures["ingredients"]] == [1]
    assert measures["steps"][0]["step_text"] == STEPS[0]
    assert body["ingredients"] == [
        {"name": item.name, "amount": item.amount} for item in INGREDIENTS
    ]


# --- KH-F01-B2-2-R1: F3 -- safe operational diagnostics -------------------------------


MASS_LOGGER = "house_helper.web.measure_targets"
CORRELATION = "hh-0123456789abcdef"


def mass_records(caplog):
    return [record for record in caplog.records if record.name == MASS_LOGGER]


def test_an_unexpected_item_failure_logs_one_safe_event(monkeypatch, caplog):
    def broken(name, quantity, **kwargs):
        raise RuntimeError("SECRET ingredient тестовая мука 125 г")

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", broken)
    with caplog.at_level(logging.WARNING, logger=MASS_LOGGER):
        block((Ingredient("тестовая мука", "125 г"),), correlation_id=CORRELATION)

    (record,) = mass_records(caplog)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "stage=mass_item_conversion" in message
    assert "exception=RuntimeError" in message
    assert "position=0" in message
    assert f"correlation_id={CORRELATION}" in message
    for forbidden in ("SECRET", "тестовая", "125", "Traceback", "RuntimeError:"):
        assert forbidden not in message


@pytest.mark.asyncio
async def test_the_detail_reuses_the_request_correlation_id_in_diagnostics(
    tmp_path, monkeypatch, caplog
):
    def broken(name, quantity, **kwargs):
        raise RuntimeError("hidden")

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", broken)
    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        with caplog.at_level(logging.WARNING, logger=MASS_LOGGER):
            response = await chat.client.get(f"/api/favorites/{favorite}")

    assert response.status_code == 200, response.text
    # The id the server already put on the answer is the one in every B2 event.
    header = response.headers["X-Correlation-ID"]
    records = mass_records(caplog)
    assert records  # the four quantified masses each failed in the fixture
    for record in records:
        assert f"correlation_id={header}" in record.getMessage()


def test_an_unsafe_correlation_id_is_replaced_before_logging(monkeypatch, caplog):
    def broken(name, quantity, **kwargs):
        raise RuntimeError("hidden")

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", broken)
    with caplog.at_level(logging.WARNING, logger=MASS_LOGGER):
        block((Ingredient("тестовая мука", "125 г"),), correlation_id="evil\ninjected")

    (record,) = mass_records(caplog)
    message = record.getMessage()
    assert "evil" not in message and "\n" not in message
    assert re.search(r"correlation_id=mass-[0-9a-f]{16}", message)


def test_a_malformed_result_logs_the_serialization_stage(monkeypatch, caplog):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result, conversion=replace(result.conversion, evidence_id=object())
        ),
    )
    with caplog.at_level(logging.WARNING, logger=MASS_LOGGER):
        block(
            (Ingredient("тестовая мука", "125 г"),),
            registry=cup_registry(),
            correlation_id=CORRELATION,
        )

    (record,) = mass_records(caplog)
    message = record.getMessage()
    assert "stage=mass_item_serialization" in message
    assert "exception=_MalformedMassResult" in message
    assert "position=0" in message
    assert f"correlation_id={CORRELATION}" in message


def test_an_unexpected_section_failure_logs_one_safe_event(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("SECRET section text")

    monkeypatch.setattr(measure_targets, "_mass_annotations", broken)
    with caplog.at_level(logging.WARNING, logger=MASS_LOGGER):
        block(
            (Ingredient("тестовая мука", "125 г"),),
            registry=cup_registry(),
            correlation_id=CORRELATION,
        )

    (record,) = mass_records(caplog)
    message = record.getMessage()
    assert "stage=mass_section" in message
    assert "exception=RuntimeError" in message
    assert "position=none" in message
    assert f"correlation_id={CORRELATION}" in message
    assert "SECRET" not in message and "Traceback" not in message


def test_a_registry_failure_logs_one_safe_event(monkeypatch, caplog):
    def broken():
        raise RuntimeError("SECRET registry text")

    monkeypatch.setattr(measure_targets, "_reference_registry", broken)
    with caplog.at_level(logging.WARNING, logger=MASS_LOGGER):
        block((Ingredient("тестовая мука", "125 г"),), correlation_id=CORRELATION)

    (record,) = mass_records(caplog)
    message = record.getMessage()
    assert "stage=mass_registry_resolution" in message
    assert "exception=RuntimeError" in message
    assert "position=none" in message
    assert f"correlation_id={CORRELATION}" in message
    assert "SECRET" not in message and "Traceback" not in message


def test_one_operation_reuses_its_local_correlation_id(monkeypatch, caplog):
    def broken(name, quantity, **kwargs):
        raise RuntimeError("hidden")

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", broken)
    with caplog.at_level(logging.WARNING, logger=MASS_LOGGER):
        block((Ingredient("мука", "100 г"), Ingredient("соль", "200 г")))

    records = mass_records(caplog)
    assert len(records) == 2
    ids = {
        re.search(r"correlation_id=(\S+)", record.getMessage()).group(1)
        for record in records
    }
    assert len(ids) == 1
    (local,) = ids
    assert re.fullmatch(r"mass-[0-9a-f]{16}", local)


def test_expected_refusals_do_not_emit_diagnostics(caplog):
    with caplog.at_level(logging.WARNING, logger=MASS_LOGGER):
        result = block(
            (Ingredient("соль", "300 г"), Ingredient("мука пшеничная", "40 г"))
        )

    assert mass_records(caplog) == []
    assert section(result)["status"] == "complete"
    assert all(item["conversion"] is None for item in section(result)["items"])
    assert "count_low" not in str(section(result))


# --- KH-F01-B2-2-R2: R2-01 -- complete-section validation ----------------------------
#
# The registry revision is data too. A lone surrogate is a `str` but not UTF-8, so it must
# be refused while the section is still being built, never left to the response encoder.


FAILED_SECTION = {
    "schema_version": 1,
    "registry_revision": None,
    "status": "failed",
    "items": [],
    "omitted": {"unsafe_fraction": 0, "malformed": 0},
}


def test_a_lone_surrogate_registry_revision_never_reaches_the_section():
    bad = replace(cup_registry(), revision="\ud800")

    result = block(
        (Ingredient("тестовая мука", "125 г"), Ingredient("вода", "1 стакан 200 мл")),
        registry=bad,
    )

    assert section(result) == FAILED_SECTION
    # The invalid revision is not reused in the fallback.
    assert section(result)["registry_revision"] is None
    # The released block is exactly what an intact registry would produce.
    assert result["schema_version"] == 2
    assert [item["position"] for item in result["ingredients"]] == [1]


@pytest.mark.parametrize("revision", [42, None, "", object(), b"bytes"])
def test_an_invalid_registry_revision_type_is_a_failed_section(revision):
    bad = replace(cup_registry(), revision=revision)

    result = block((Ingredient("тестовая мука", "125 г"),), registry=bad)

    assert section(result) == FAILED_SECTION


@pytest.mark.parametrize("counter", [-1, "1", 1.5, True])
def test_invalid_section_metadata_falls_back_to_the_fixed_failed_section(
    monkeypatch, counter
):
    def corrupt(ingredients, target, registry, omissions, correlation_id):
        omissions.malformed = counter
        return []

    monkeypatch.setattr(measure_targets, "_mass_annotations", corrupt)
    result = block(
        (Ingredient("тестовая мука", "125 г"), Ingredient("вода", "1 стакан 200 мл")),
        registry=cup_registry(),
    )

    assert section(result) == FAILED_SECTION
    assert [item["position"] for item in result["ingredients"]] == [1]


def test_the_failed_fallback_is_itself_json_safe():
    fallback = measure_targets._failed_mass_section(None)

    assert fallback == FAILED_SECTION
    # The fixed fallback must survive the same gate the built section must pass.
    json.dumps(fallback, allow_nan=False, ensure_ascii=False).encode("utf-8")


def test_the_complete_section_gate_rejects_a_surrogate_revision_directly():
    section_value = dict(FAILED_SECTION, registry_revision="\ud800", status="complete")

    with pytest.raises(measure_targets._MalformedMassResult):
        measure_targets._validated_mass_section(section_value)


def test_the_complete_section_gate_revalidates_its_items():
    section_value = dict(FAILED_SECTION, registry_revision="test-fixture", status="complete")
    section_value["items"] = [{"not": "a mass item"}]

    with pytest.raises(measure_targets._MalformedMassResult):
        measure_targets._validated_mass_section(section_value)


@pytest.mark.asyncio
async def test_an_invalid_revision_cannot_break_get_or_patch_and_persists(
    tmp_path, monkeypatch
):
    bad = replace(cup_registry(), revision="\ud800")
    monkeypatch.setattr(measure_targets, "_reference_registry", lambda: bad)
    expected_v1 = measure_block_v2(INGREDIENTS, STEPS, DEFAULT, favorite_version=1)
    expected_v2 = measure_block_v2(INGREDIENTS, STEPS, DEFAULT, favorite_version=2)

    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        got = await chat.client.get(f"/api/favorites/{favorite}")
        patched = await chat.client.patch(
            f"/api/favorites/{favorite}",
            json={"expected_version": 1, "title": "Тесто для пирога"},
            headers=post_headers(),
        )
        reread = await chat.client.get(f"/api/favorites/{favorite}")

    assert got.status_code == 200, got.text
    body = got.json()
    assert body["measures"]["mass_conversions"] == FAILED_SECTION
    assert released(body["measures"]) == released(expected_v1)
    assert "\ud800" not in got.text

    assert patched.status_code == 200, patched.text
    changed = patched.json()
    assert changed["version"] == 2
    assert changed["measures"]["mass_conversions"] == FAILED_SECTION
    assert released(changed["measures"]) == released(expected_v2)

    # The write persisted, and the later answer describes the same stored version.
    assert reread.status_code == 200, reread.text
    reread_body = reread.json()
    assert (reread_body["version"], reread_body["title"]) == (
        changed["version"],
        changed["title"],
    )
    assert reread_body["measures"] == changed["measures"]


# --- KH-F01-B2-2-R2: R2-02 -- semantic item validation -------------------------------


def test_a_success_with_an_unrecognized_match_is_dropped(monkeypatch):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result,
            match=replace(
                result.match,
                status=MatchStatus.UNRECOGNIZED,
                entry=None,
            ),
        ),
    )

    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_success_without_an_ingredient_identity_is_dropped(monkeypatch):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(result, match=replace(result.match, entry=None)),
    )

    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_reversed_count_interval_is_dropped(monkeypatch):
    def corrupt(result):
        conversion = result.conversion
        return replace(
            result,
            conversion=replace(
                conversion,
                count_low=conversion.count_high,
                count_high=conversion.count_low,
            ),
        )

    corrupt_successful_results(monkeypatch, corrupt)
    result = block((Ingredient("тестовый корень", "500 г"),), registry=interval_registry())

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_reversed_rounded_bounds_are_dropped(monkeypatch):
    def corrupt(result):
        conversion = result.conversion
        return replace(
            result,
            conversion=replace(
                conversion,
                rounded_low=conversion.rounded_high,
                rounded_high=conversion.rounded_low,
            ),
        )

    corrupt_successful_results(monkeypatch, corrupt)
    result = block((Ingredient("тестовый корень", "500 г"),), registry=interval_registry())

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_point_reference_cannot_masquerade_as_an_interval(monkeypatch):
    def corrupt(result):
        conversion = result.conversion
        return replace(
            result,
            conversion=replace(conversion, count_high=conversion.count_low + 1),
        )

    corrupt_successful_results(monkeypatch, corrupt)
    result = block((Ingredient("тестовый корень", "300 г"),), registry=point_registry(200))

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_zero_calibration_assumption_weight_is_dropped(monkeypatch):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result, assumption=replace(result.assumption, weight_g=Fraction(0))
        ),
    )

    result = block(
        (Ingredient("тестовое филе", "500 г"),),
        registry=calibration_registry(default=True),
    )

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


@pytest.mark.parametrize("label", ["", "   ", "if the fillet is medium"])
def test_an_invalid_assumption_label_is_dropped(monkeypatch, label):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result, assumption=replace(result.assumption, label_ru=label)
        ),
    )

    result = block(
        (Ingredient("тестовое филе", "500 г"),),
        registry=calibration_registry(default=True),
    )

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_source_mass_drifting_from_the_stored_amount_is_dropped(monkeypatch):
    corrupt_successful_results(
        monkeypatch, lambda result: replace(result, mass_g=Fraction(999))
    )

    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}
    # The stored amount is never replaced by a newly calculated one.
    assert result["ingredients"] == []


def test_a_nested_conversion_status_disagreeing_with_its_result_is_dropped(monkeypatch):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(
            result,
            conversion=replace(
                result.conversion,
                status=MassConversionStatus.UNSUPPORTED,
                reason=MassConversionReason.REFERENCE_NOT_APPROVED,
            ),
        ),
    )

    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_malformed_semantic_item_does_not_remove_its_valid_sibling(monkeypatch):
    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        result = real(name, quantity, **kwargs)
        if result is not None and result.conversion is not None and result.mass_g == 125:
            return replace(result, mass_g=Fraction(999))
        return result

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    result = block(
        (Ingredient("тестовая мука", "125 г"), Ingredient("тестовая мука", "100 г")),
        registry=cup_registry(),
    )

    assert [item["position"] for item in section(result)["items"]] == [1]
    assert section(result)["items"][0]["mass_g"] == {"numerator": 100, "denominator": 1}
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_the_omission_counters_match_the_dropped_semantic_items(monkeypatch):
    corrupt_successful_results(
        monkeypatch,
        lambda result: replace(result, match=replace(result.match, entry=None)),
    )

    result = block(
        (
            Ingredient("тестовая мука", "125 г"),
            Ingredient("тестовая мука", "100 г"),
            Ingredient("вода", "1 стакан 200 мл"),
        ),
        registry=cup_registry(),
    )

    assert section(result)["items"] == []
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 2}
    # The released B1.1 volume annotation survives both dropped mass items.
    assert [item["position"] for item in result["ingredients"]] == [2]


# --- KH-F01-B2-2-R2: R2-03 -- a failing logging handler stays confined ---------------


class _RaisingHandler(logging.Handler):
    """A real handler whose `emit` raises, as the Codex reproduction did."""

    def emit(self, record):  # noqa: D102 - deliberately raises
        raise OSError("the logging handler is broken")


@contextmanager
def broken_logging():
    target = logging.getLogger(MASS_LOGGER)
    handler = _RaisingHandler()
    old_level, old_propagate = target.level, target.propagate
    target.addHandler(handler)
    target.setLevel(logging.WARNING)
    try:
        yield
    finally:
        target.removeHandler(handler)
        target.setLevel(old_level)
        target.propagate = old_propagate


def test_a_broken_logging_handler_cannot_escape_a_registry_failure(monkeypatch):
    def broken():
        raise RuntimeError("hidden")

    monkeypatch.setattr(measure_targets, "_reference_registry", broken)
    with broken_logging():
        result = block(
            (Ingredient("тестовая мука", "125 г"), Ingredient("вода", "1 стакан 200 мл"))
        )

    assert section(result) == FAILED_SECTION
    assert [item["position"] for item in result["ingredients"]] == [1]


def test_a_broken_logging_handler_cannot_escape_an_item_failure(monkeypatch):
    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        if name == "тестовая мука":
            raise RuntimeError("hidden")
        return real(name, quantity, **kwargs)

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    with broken_logging():
        result = block(
            (Ingredient("тестовая мука", "125 г"), Ingredient("вода", "1 стакан 200 мл")),
            registry=cup_registry(),
        )

    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}
    assert [item["position"] for item in result["ingredients"]] == [1]


@pytest.mark.asyncio
async def test_a_broken_logger_keeps_the_detail_get_successful(
    tmp_path, monkeypatch
):
    def broken():
        raise RuntimeError("hidden")

    monkeypatch.setattr(measure_targets, "_reference_registry", broken)
    expected = measure_block_v2(INGREDIENTS, STEPS, DEFAULT, favorite_version=1)
    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        with broken_logging():
            response = await chat.client.get(f"/api/favorites/{favorite}")

    assert response.status_code == 200, response.text
    measures = response.json()["measures"]
    assert measures["mass_conversions"] == FAILED_SECTION
    assert released(measures) == released(expected)


@pytest.mark.asyncio
async def test_a_broken_logger_keeps_a_successful_patch_and_b1_1_intact(
    tmp_path, monkeypatch
):
    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        if name in {"тестовая мука", "мука пшеничная"}:
            raise RuntimeError("hidden")
        return real(name, quantity, **kwargs)

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    expected = measure_block_v2(INGREDIENTS, STEPS, DEFAULT, favorite_version=2)
    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        with broken_logging():
            response = await chat.client.patch(
                f"/api/favorites/{favorite}",
                json={"expected_version": 1, "title": "Тесто для пирога"},
                headers=post_headers(),
            )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 2
    measures = body["measures"]
    assert measures["mass_conversions"]["status"] == "incomplete"
    # The two dropped fixture masses are counted; the two refusals remain.
    assert measures["mass_conversions"]["omitted"] == {
        "unsafe_fraction": 0,
        "malformed": 2,
    }
    assert released(measures) == released(expected)


def test_a_working_handler_still_receives_one_safe_correlated_event(monkeypatch):
    records = []

    class _CollectingHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    target = logging.getLogger(MASS_LOGGER)
    handler = _CollectingHandler()
    old_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.WARNING)

    def broken(name, quantity, **kwargs):
        raise RuntimeError("SECRET ingredient тестовая мука 125 г")

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", broken)
    try:
        block((Ingredient("тестовая мука", "125 г"),), correlation_id=CORRELATION)
    finally:
        target.removeHandler(handler)
        target.setLevel(old_level)

    (record,) = records
    message = record.getMessage()
    assert "stage=mass_item_conversion" in message
    assert "exception=RuntimeError" in message
    assert "position=0" in message
    assert f"correlation_id={CORRELATION}" in message
    for forbidden in ("SECRET", "тестовая", "125", "Traceback"):
        assert forbidden not in message


# --- KH-F01-B2-2-R3: zero-mass semantic invariants -----------------------------------
#
# The adapter must never present a number for a zero-mass ingredient, and it must never
# mislabel a non-zero mass as `zero_mass`. Both corruptions below are fabricated by
# wrapping the real result; the engine never produces either shape.


def test_a_fabricated_successful_conversion_of_zero_grams_is_dropped(monkeypatch):
    # The engine returns a refusal for `0 г`; this fixture relabels a real successful
    # result as a zero-mass one, keeping its nested conversion. Codex reproduction 1.
    zero_success = replace(real_conversion(), mass_g=Fraction(0))
    assert zero_success.conversion is not None  # the fabricated "successful" shape

    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        if quantity.text == "0 г":
            return zero_success
        return real(name, quantity, **kwargs)

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    result = block((Ingredient("тестовая мука", "0 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_nonzero_mass_labelled_zero_mass_is_dropped(monkeypatch):
    # Codex reproduction 2: the parsed source mass is 125 g, but the result claims the
    # `zero_mass` refusal. The two fields cannot both be believed.
    successful = real_conversion()
    contradictory = replace(
        successful,
        status=MassConversionStatus.UNSUPPORTED,
        reason=MassConversionReason.ZERO_MASS,
        conversion=None,
    )
    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        if quantity.text == "125 г":
            return contradictory
        return real(name, quantity, **kwargs)

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    assert section(result)["items"] == []
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}


def test_a_legitimate_zero_mass_refusal_stays_accepted():
    # The reverse invariant must not over-reject: the engine's own `0 г` refusal is valid.
    result = block((Ingredient("мука пшеничная", "0 г"),))

    (item,) = section(result)["items"]
    assert (item["status"], item["reason"]) == ("unsupported", "zero_mass")
    assert item["mass_g"] == {"numerator": 0, "denominator": 1}
    assert item["conversion"] is None
    assert section(result)["status"] == "complete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 0}


def test_a_legitimate_positive_mass_conversion_stays_accepted():
    result = block((Ingredient("тестовая мука", "125 г"),), registry=cup_registry())

    (item,) = section(result)["items"]
    assert (item["status"], item["reason"]) == ("reference_approximate", "converted")
    assert item["mass_g"] == {"numerator": 125, "denominator": 1}
    assert item["conversion"] is not None
    assert section(result)["status"] == "complete"


def test_an_exact_positive_fractional_mass_is_accepted():
    # 125,5 g at the fixture's 1/2 g/mL is 251 mL, exactly 251/250 of the 250 mL glass.
    # The mass and the conversion stay exact integers, never a rounded float.
    result = block((Ingredient("тестовая мука", "125,5 г"),), registry=cup_registry())

    (item,) = section(result)["items"]
    assert item["mass_g"] == {"numerator": 251, "denominator": 2}
    assert item["status"] == "reference_approximate"
    assert item["conversion"]["exact"] == {"numerator": 251, "denominator": 250}
    assert section(result)["status"] == "complete"


def test_a_zero_mass_contradiction_drops_only_itself_and_keeps_siblings_and_b1_1(
    monkeypatch,
):
    zero_success = replace(real_conversion(), mass_g=Fraction(0))
    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        if quantity.text == "0 г":
            return zero_success
        return real(name, quantity, **kwargs)

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    ingredients = (
        Ingredient("тестовая мука", "0 г"),
        Ingredient("тестовая мука", "100 г"),
        Ingredient("вода", "1 стакан 200 мл"),
    )
    result = block(ingredients, registry=cup_registry())

    # Only the contradictory item is dropped; its valid B2 sibling survives.
    assert [item["position"] for item in section(result)["items"]] == [1]
    assert section(result)["items"][0]["mass_g"] == {"numerator": 100, "denominator": 1}
    assert section(result)["status"] == "incomplete"
    assert section(result)["omitted"] == {"unsafe_fraction": 0, "malformed": 1}
    # The released B1.1 block is exactly what it was without the contradiction.
    assert released(result) == released(block(ingredients, registry=cup_registry()))
    assert [item["position"] for item in result["ingredients"]] == [2]


@pytest.mark.asyncio
async def test_a_zero_mass_contradiction_keeps_get_patch_and_reread_consistent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(measure_targets, "_reference_registry", lambda: cup_registry())
    real = measure_targets.convert_ingredient_mass

    def patched(name, quantity, **kwargs):
        result = real(name, quantity, **kwargs)
        if (
            name == "тестовая мука"
            and quantity.text == "125 г"
            and result is not None
            and result.conversion is not None
        ):
            return replace(
                result,
                status=MassConversionStatus.UNSUPPORTED,
                reason=MassConversionReason.ZERO_MASS,
                conversion=None,
            )
        return result

    monkeypatch.setattr(measure_targets, "convert_ingredient_mass", patched)
    async with await kept_recipe(tmp_path) as chat:
        favorite = await keep_recipe(chat)
        got = await chat.client.get(f"/api/favorites/{favorite}")
        assert got.status_code == 200, got.text
        body = got.json()
        measures = body["measures"]
        assert measures["mass_conversions"]["status"] == "incomplete"
        assert measures["mass_conversions"]["omitted"] == {
            "unsafe_fraction": 0,
            "malformed": 1,
        }
        # Position 0 was the contradictory 125 g; the refusals 2, 3 and the conversion 4
        # survive, and the released B1.1 volume annotation at position 1 is untouched.
        assert sorted(section_by_position(measures)) == [2, 3, 4]
        assert [item["position"] for item in measures["ingredients"]] == [1]
        assert body["ingredients"] == [
            {"name": item.name, "amount": item.amount} for item in INGREDIENTS
        ]

        patched_response = await chat.client.patch(
            f"/api/favorites/{favorite}",
            json={"expected_version": 1, "title": "Тесто для пирога"},
            headers=post_headers(),
        )
        assert patched_response.status_code == 200, patched_response.text
        changed = patched_response.json()
        assert changed["version"] == 2
        assert changed["measures"]["mass_conversions"]["omitted"] == {
            "unsafe_fraction": 0,
            "malformed": 1,
        }
        assert sorted(section_by_position(changed["measures"])) == [2, 3, 4]

        reread = await chat.client.get(f"/api/favorites/{favorite}")
        assert reread.status_code == 200, reread.text
        reread_body = reread.json()

    # The write persisted, and the later answer describes the same stored version.
    assert (reread_body["version"], reread_body["title"]) == (
        changed["version"],
        changed["title"],
    )
    assert reread_body["measures"] == changed["measures"]
