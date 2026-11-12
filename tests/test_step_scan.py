"""KH-F01-B1.1-B: the deterministic step scanner and its explicit ingredient binder.

These tests pin the Stage B exit criteria of the approved KH-F01 v0.6 specification:
atomic amount expressions (AC82), non-amount numbers (AC65), the four frozen local
constructions only (AC66-AC68, AC81), shared and anaphoric fallbacks (AC68), spans in
code points (AC83), determinism (AC76), bounded scanning and the span caps (AC79, AC87),
and no conversion or hint in Stage B (AC61-AC62). Stage A provenance is untouched.

The tests deliberately assert that uncertain text stays uncertain. A missing hint is the
approved outcome; a confident wrong ingredient is not.
"""

import ast
import time
from dataclasses import fields
from pathlib import Path

import pytest

import house_helper.core.step_scan as step_scan_module
from house_helper.core.provenance import (
    ProvenanceKind,
    TermProvenanceStatus,
    audit_source_provenance,
)
from house_helper.core.recipes import Ingredient
from house_helper.core.step_scan import (
    MAX_SPANS_PER_RECIPE,
    MAX_SPANS_PER_STEP,
    AmountLinkReason,
    AmountLinkStatus,
    scan_recipe_steps,
    scan_step,
)

NAMES = ("вода", "молоко", "мука", "соль", "масло", "сахар", "лук", "помидор")


def scan(text, names=NAMES):
    return scan_step(text, [Ingredient(name, "1 г") for name in names])


def only(text, names=NAMES):
    [amount] = scan(text, names).amounts
    return amount


def linked(amount):
    return amount.status is AmountLinkStatus.LINKED


# --- the result contract --------------------------------------------------------------


def test_the_scanner_imports_only_the_standard_library_and_the_measures_core():
    source = Path(step_scan_module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)

    assert imported == {
        "__future__",
        "re",
        "bisect",
        "collections.abc",
        "dataclasses",
        "enum",
        "house_helper.core.measures",
    }


def test_the_result_has_no_conversion_or_hint_field():
    exposed = {field.name for field in fields(type(scan("500 мл воды").amounts[0]))}

    assert exposed == {
        "step_index",
        "start",
        "end",
        "text",
        "status",
        "reason",
        "ingredient_position",
        "approximate",
    }


def test_a_span_is_an_exact_slice_of_the_stored_step():
    text = "Добавьте 500 мл воды."

    amount = only(text)

    assert text[amount.start : amount.end] == amount.text == "500 мл"
    assert amount.start < amount.end <= len(text)


def test_spans_are_sorted_and_non_overlapping():
    text = "Влейте 500 мл воды и добавьте 250 мл молока."
    result = scan(text)

    spans = [(amount.start, amount.end) for amount in result.amounts]

    assert spans == sorted(spans)
    assert all(left[1] <= right[0] for left, right in zip(spans, spans[1:]))


def test_code_point_offsets_survive_astral_characters():
    text = "Добавьте 🥛 500 мл воды 🥛."
    amount = only(text)

    # The emoji is one Python code point but two UTF-16 code units; a code-point span
    # must slice the original text directly.
    assert amount.text == "500 мл"
    assert text[amount.start : amount.end] == "500 мл"
    assert amount.start == text.index("500")


def test_a_non_breaking_space_is_preserved_in_the_exact_span():
    nbsp = chr(0x00A0)
    text = f"Добавьте{nbsp}500{nbsp}мл{nbsp}воды."
    [amount] = scan(text, names=("вода",)).amounts

    assert amount.text == f"500{nbsp}мл"
    assert text[amount.start : amount.end] == amount.text
    assert amount.status is AmountLinkStatus.LINKED


def test_scanning_the_same_input_twice_is_identical():
    text = "Влейте 500 мл воды и добавьте 250 мл молока."

    assert scan(text) == scan(text)


def test_scanning_does_not_change_the_inputs():
    ingredients = (Ingredient("вода", "1 стакан"), Ingredient("молоко", "500 мл"))
    steps = ("Добавьте 500 мл воды.", "Влейте 250 мл молока.")
    before = (ingredients, steps)

    scan_recipe_steps(steps, ingredients)

    assert (ingredients, steps) == before


def test_plain_names_and_mappings_are_accepted():
    from_objects = scan_step("Добавьте 500 мл воды.", [Ingredient("вода", "1 г")])
    from_strings = scan_step("Добавьте 500 мл воды.", ["вода"])
    from_mappings = scan_step("Добавьте 500 мл воды.", [{"name": "вода"}])

    assert from_objects.amounts == from_strings.amounts == from_mappings.amounts


def test_wrong_input_types_are_rejected():
    with pytest.raises(TypeError):
        scan_step(500, ["вода"])
    with pytest.raises(TypeError):
        scan_step("500 мл", "вода")
    with pytest.raises(TypeError):
        scan_step("500 мл", [object()])
    with pytest.raises(TypeError):
        scan_recipe_steps("500 мл", ["вода"])
    with pytest.raises(TypeError):
        scan_recipe_steps(["500 мл", 3], ["вода"])


def test_an_empty_recipe_is_empty():
    result = scan_recipe_steps([], [])

    assert result.steps == ()
    assert result.amounts == ()
    assert result.complete


# --- supported numeric forms ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "1 г",
        "1,5 г",
        "1.5 г",
        "1/2 стакана",
        "1 1/2 стакана",
        "½ стакана",
        "1½ стакана",
        "2 ½ стакана",
        "1 л 200 мл",
        "1 ст. л. и 1 ч. л.",
        "1 стакан 200 мл",
        "1 стакан (200 мл)",
        "1 стакан на 200 мл",
        "1 стакан (237,5 мл)",
        "500мл",
    ],
)
def test_a_supported_number_and_unit_is_one_atomic_span(text):
    result = scan(text, names=())

    [amount] = result.amounts
    assert amount.text == text
    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_MENTION


def test_a_compound_is_one_span_not_two():
    result = scan("1 л 200 мл", names=())

    [amount] = result.amounts
    assert amount.text == "1 л 200 мл"


def test_a_glass_capacity_belongs_to_the_expression():
    result = scan("1 стакан (237,5 мл) воды", names=("вода",))

    [amount] = result.amounts
    assert amount.text == "1 стакан (237,5 мл)"
    assert amount.status is AmountLinkStatus.LINKED


# --- atomic rejection and no salvage --------------------------------------------------


@pytest.mark.parametrize(
    "text, expected_text, reason",
    [
        ("2–3 ст. л. масла", "2–3 ст. л.", AmountLinkReason.RANGE),
        ("1,5–2 стакана молока", "1,5–2 стакана", AmountLinkReason.RANGE),
        ("от 100 до 150 мл", "100 до 150 мл", AmountLinkReason.RANGE),
        ("2 - 3 ст. л.", "2 - 3 ст. л.", AmountLinkReason.RANGE),
        ("1 ст. л. с горкой", "1 ст. л. с горкой", AmountLinkReason.HEAPED),
        ("2 ст. л. с горкой масла", "2 ст. л. с горкой", AmountLinkReason.HEAPED),
        ("1 cup молока", "1 cup", AmountLinkReason.US_CUP),
        ("1 чашка молока", "1 чашка", AmountLinkReason.US_CUP),
        (
            "1 пачка (400 г) сметаны",
            "1 пачка (400 г)",
            AmountLinkReason.PACKAGE,
        ),
        ("1 пачка 400 г", "1 пачка 400 г", AmountLinkReason.PACKAGE),
        ("2 зубчика чеснока", "2 зубчика", AmountLinkReason.COUNTABLE),
        ("2 яйца", "2 яйца", AmountLinkReason.COUNTABLE),
        ("1 шт.", "1 шт.", AmountLinkReason.COUNTABLE),
        (
            "1 стакан (200 мл",
            "1 стакан (200 мл",
            AmountLinkReason.MALFORMED_EXPRESSION,
        ),
        (
            "1 стакан 200",
            "1 стакан 200",
            AmountLinkReason.MALFORMED_EXPRESSION,
        ),
        (
            "1 стакан 20 мл",
            "1 стакан 20 мл",
            AmountLinkReason.UNSUPPORTED_CONSTRUCT,
        ),
        (
            "1 стакан (2000 мл)",
            "1 стакан (2000 мл)",
            AmountLinkReason.UNSUPPORTED_CONSTRUCT,
        ),
        (
            "500 мл (200 мл)",
            "500 мл (200 мл)",
            AmountLinkReason.UNSUPPORTED_CONSTRUCT,
        ),
        (
            "1 ст. л. 200 мл",
            "1 ст. л. 200 мл",
            AmountLinkReason.UNSUPPORTED_CONSTRUCT,
        ),
        (
            "1 л 200 г",
            "1 л 200 г",
            AmountLinkReason.UNSUPPORTED_CONSTRUCT,
        ),
        (
            "1 ст. л. с горкой 200 мл",
            "1 ст. л. с горкой 200 мл",
            AmountLinkReason.HEAPED,
        ),
    ],
)
def test_a_rejected_expression_is_one_span_with_no_salvage(
    text, expected_text, reason
):
    result = scan(text, names=("вода", "молоко", "морковь", "чеснок"))

    [amount] = result.amounts
    assert amount.text == expected_text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is reason
    assert amount.ingredient_position is None


def test_a_range_with_no_supported_unit_yields_no_salvaged_member():
    # The whole range is consumed; the countable word after it cannot become its own span.
    assert scan("2–3 яйца", names=("яйцо",)).amounts == ()
    assert scan("10–12 минут", names=()).amounts == ()
    assert scan("от 100 до 150", names=()).amounts == ()


@pytest.mark.parametrize(
    "text",
    [
        "1, 5 г",
        "1 / 2 стакана",
        "1 000 г",
    ],
)
def test_a_malformed_spaced_number_yields_no_salvaged_tail(text):
    assert scan(text, names=()).amounts == ()


def test_a_comma_separated_list_of_amounts_is_not_a_malformed_number():
    result = scan("1 г, 2 г", names=())

    assert [amount.text for amount in result.amounts] == ["1 г", "2 г"]


def test_an_unbalanced_capacity_does_not_yield_a_glass_or_a_volume():
    result = scan("1 стакан (200 мл воды", names=("вода",))

    [amount] = result.amounts
    assert amount.text == "1 стакан (200 мл воды"
    assert amount.reason is AmountLinkReason.MALFORMED_EXPRESSION


def test_a_rejected_package_consumes_its_declared_content():
    result = scan("1 пачка (400 г) сметаны", names=("сметана",))

    [amount] = result.amounts
    assert amount.text == "1 пачка (400 г)"
    assert "400 г" not in [span.text for span in result.amounts]


def test_a_rejected_span_consumes_a_following_range_whole():
    result = scan("1 пачка 2 – 3 стакана молока", names=("молоко",))

    [amount] = result.amounts
    assert amount.text == "1 пачка 2 – 3 стакана"
    assert amount.reason is AmountLinkReason.PACKAGE
    assert amount.ingredient_position is None


def test_a_bank_is_not_a_jar():
    assert scan("1 банк", names=()).amounts == ()

    [jar] = scan("1 банка (400 г)", names=()).amounts
    assert jar.reason is AmountLinkReason.PACKAGE


def test_a_heaped_spoon_cannot_salvage_the_following_part():
    result = scan("1 ст. л. с горкой 200 мл", names=())

    [amount] = result.amounts
    assert amount.text == "1 ст. л. с горкой 200 мл"
    assert amount.reason is AmountLinkReason.HEAPED


def test_a_spoon_followed_by_a_heaped_spoon_keeps_the_heaped_reason():
    result = scan("1 ст. л. 2 ч. л. с горкой", names=())

    [amount] = result.amounts
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.HEAPED


# --- numbers that are not ingredient amounts ------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Нагрейте до 180 °C.",
        "Нагрейте до 180°C.",
        "Доведите до 200 градусов.",
        "Готовьте 20 минут.",
        "Подождите 1,5 часа.",
        "Готовьте 10–12 минут.",
        "Рассчитано на 4 порции.",
        "Рассчитано на 2–3 порции.",
        "Диаметр 20 см.",
        "Добавьте 50%.",
        "Смешайте 1:2.",
        "Шаг 1. Добавьте воды.",
    ],
)
def test_time_temperature_servings_dimensions_and_ratios_yield_no_amount(text):
    assert scan(text).amounts == ()


def test_a_countable_form_is_reported_but_never_converted_or_linked():
    result = scan("Добавьте 2 яйца.", names=("яйцо",))

    [amount] = result.amounts
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.COUNTABLE
    assert amount.ingredient_position is None


def test_a_single_named_ingredient_never_links_an_unbound_amount():
    result = scan("Добавьте 500 мл и перемешайте.", names=("вода",))

    [amount] = result.amounts
    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_MENTION


def test_a_nearby_name_without_a_construction_does_not_link():
    result = scan(
        "Добавьте 500 мл, перемешайте с водой.", names=("вода",)
    )

    [amount] = result.amounts
    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING


def test_a_cookware_volume_is_not_an_ingredient_amount():
    text = "Возьмите кастрюлю объемом 3 л."
    [amount] = scan(text, names=("вода",)).amounts

    # The unit spelling may carry the sentence period; the slice is still exact.
    assert amount.text == text[amount.start : amount.end]
    assert amount.text.rstrip(".") == "3 л"
    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_MENTION
    assert amount.ingredient_position is None


def test_an_equipment_amount_does_not_link_a_nearby_name():
    [amount] = scan("Налейте 3 л в кастрюлю с водой.", names=("вода",)).amounts

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING


def test_a_step_number_is_not_an_amount_and_does_not_block_the_real_one():
    result = scan("Шаг 2. Добавьте 500 мл воды.", names=("вода",))

    [amount] = result.amounts
    assert amount.text == "500 мл"
    assert amount.status is AmountLinkStatus.LINKED
    assert amount.ingredient_position == 0


# --- L1: the name follows the amount -------------------------------------------------


def test_l1_links_a_name_after_the_amount():
    amount = only("Добавьте 500 мл воды.")

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0


def test_l1_links_an_inflected_name_and_an_adjective_context():
    amount = only("Добавьте 200 г пшеничной муки.", names=("пшеничная мука",))

    assert linked(amount)
    assert amount.ingredient_position == 0


def test_l1_accepts_a_verb_after_the_name():
    amount = only("Добавьте 500 мл воды и перемешайте.")

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER


def test_l1_does_not_link_a_coordinated_name_group():
    amount = only("Смешайте 500 мл воды и молока.")

    assert amount.status is AmountLinkStatus.SHARED
    assert amount.reason is AmountLinkReason.SHARED_AMOUNT
    assert amount.ingredient_position is None


def test_l1_refuses_two_uncoordinated_adjacent_names():
    amount = only("500 мл воды молока")

    assert amount.status is AmountLinkStatus.AMBIGUOUS
    assert amount.reason is AmountLinkReason.MULTIPLE_CANDIDATES


def test_l1_classifies_a_comma_separated_name_group_as_shared():
    amount = only("Смешайте 500 мл воды, молока.")

    assert amount.status is AmountLinkStatus.SHARED
    assert amount.reason is AmountLinkReason.SHARED_AMOUNT


# --- L2: the name precedes the amount through a separator -----------------------------


@pytest.mark.parametrize("separator", ["—", "–", ":", "="])
def test_l2_links_through_each_approved_separator(separator):
    amount = only(f"Вода {separator} 500 мл")

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_BEFORE_SEPARATOR
    assert amount.ingredient_position == 0


def test_l2_links_an_inflected_name():
    amount = only("муку: 200 г")

    assert linked(amount)
    assert amount.ingredient_position == 2


def test_l2_does_not_cross_a_sentence_boundary():
    amount = only("Вода. 500 мл")

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.ingredient_position is None


def test_l2_does_not_accept_a_hyphen_as_a_separator():
    amount = only("Вода - 500 мл")

    assert amount.status is AmountLinkStatus.UNRESOLVED


def test_l2_classifies_a_coordinated_name_group_as_shared():
    assert only("Вода и молоко — 500 мл").status is AmountLinkStatus.SHARED
    assert only("Вода, молоко — 500 мл").status is AmountLinkStatus.SHARED


def test_l2_refuses_two_uncoordinated_names():
    amount = only("Вода молоко — 500 мл")

    assert amount.status is AmountLinkStatus.AMBIGUOUS


def test_a_name_on_each_side_of_the_amount_is_ambiguous():
    amount = only("Вода — 500 мл молока")

    assert amount.status is AmountLinkStatus.AMBIGUOUS
    assert amount.reason is AmountLinkReason.MULTIPLE_CANDIDATES


# --- L3: the name precedes the amount through a closed verb ---------------------------


@pytest.mark.parametrize(
    "verb",
    [
        "добавьте",
        "влейте",
        "всыпьте",
        "положите",
        "возьмите",
        "нужно",
        "потребуется",
        "отмерьте",
    ],
)
def test_l3_links_through_each_closed_verb(verb):
    amount = only(f"Воду {verb} 500 мл")

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_BEFORE_COPULA
    assert amount.ingredient_position == 0


def test_l3_does_not_link_when_another_finite_verb_intervenes():
    amount = only("Воду доведите до кипения, добавьте 500 мл.")

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None


def test_l3_refuses_a_comma_between_the_name_and_the_verb():
    amount = only("Воду, добавьте 500 мл")

    assert amount.status is AmountLinkStatus.UNRESOLVED


def test_l3_refuses_the_verb_before_the_name_order():
    amount = only("Возьмите воду 500 мл")

    assert amount.status is AmountLinkStatus.UNRESOLVED


def test_l3_classifies_a_coordinated_group_as_shared():
    amount = only("Воду и соль добавьте 500 мл")

    assert amount.status is AmountLinkStatus.SHARED


def test_a_name_before_and_a_different_name_after_are_ambiguous():
    amount = only("Воду добавьте 500 мл молока")

    assert amount.status is AmountLinkStatus.AMBIGUOUS


# --- L4: independent amounts with their own names -------------------------------------


def test_l4_links_each_amount_to_its_own_name():
    result = scan("100 мл молока и 100 мл воды")

    assert [amount.status for amount in result.amounts] == [
        AmountLinkStatus.LINKED,
        AmountLinkStatus.LINKED,
    ]
    assert [amount.ingredient_position for amount in result.amounts] == [1, 0]


def test_l4_does_not_share_an_amount_between_two_names():
    result = scan("500 мл воды и 500 мл молока")

    assert [amount.ingredient_position for amount in result.amounts] == [0, 1]


def test_two_amounts_in_one_sentence_are_linked_independently():
    result = scan("Влейте 500 мл воды и добавьте 250 мл молока.")

    assert [amount.ingredient_position for amount in result.amounts] == [0, 1]
    assert [amount.status for amount in result.amounts] == [
        AmountLinkStatus.LINKED,
        AmountLinkStatus.LINKED,
    ]


def test_two_ingredients_without_a_local_construction_are_unresolved():
    result = scan("Смешайте воду и молоко, добавьте 500 мл.")

    [amount] = result.amounts
    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None


def test_a_shared_amount_has_no_per_ingredient_hint():
    amount = only("Смешайте 500 мл воды и молока.")

    assert amount.status is AmountLinkStatus.SHARED
    assert amount.ingredient_position is None


def test_an_alternative_construction_is_shared_not_linked():
    amount = only("Добавьте 500 мл воды или молока.")

    assert amount.status is AmountLinkStatus.SHARED


# --- anaphora and approximation -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "половину от 500 мл",
        "Возьмите его 500 мл",
        "Возьмите её 500 мл",
        "Возьмите их 500 мл",
        "Возьмите этого 500 мл",
        "Оставьте остаток 500 мл",
        "Возьмите столько 500 мл",
    ],
)
def test_a_portion_or_pronoun_governed_amount_is_unresolved(text):
    amount = only(text)

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.ANAPHORA
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text",
    ["Возьмите половину воды.", "оставьте его", "половину воды"],
)
def test_a_numberless_anaphoric_phrase_produces_no_span(text):
    assert scan(text).amounts == ()


@pytest.mark.parametrize("word", ["около", "примерно", "приблизительно"])
def test_an_approximation_word_is_recorded_without_a_link_change(word):
    amount = only(f"{word} 500 мл воды")

    assert linked(amount)
    assert amount.approximate is True


def test_a_plain_amount_is_not_approximate():
    assert only("Добавьте 500 мл воды.").approximate is False


# --- deterministic Russian morphology and curated aliases -----------------------------


@pytest.mark.parametrize(
    "head, form",
    [
        ("вода", "воды"),
        ("вода", "воду"),
        ("вода", "водой"),
        ("вода", "воде"),
        ("мука", "муки"),
        ("мука", "муку"),
        ("мука", "мукой"),
        ("соль", "соли"),
        ("соль", "солью"),
        ("молоко", "молока"),
        ("молоко", "молоком"),
        ("масло", "масла"),
        ("масло", "маслом"),
        ("сахар", "сахара"),
        ("сахар", "сахаром"),
        ("лук", "лука"),
        ("лук", "луком"),
        ("перец", "перца"),
        ("чай", "чая"),
    ],
)
def test_curated_inflections_link(head, form):
    amount = only(f"Добавьте 500 мл {form}", names=(head,))

    assert linked(amount)
    assert amount.ingredient_position == 0


@pytest.mark.parametrize(
    "head, form",
    [
        ("капуста", "капусты"),
        ("морковь", "моркови"),
        ("свекла", "свеклы"),
        ("курица", "курицы"),
        ("рыба", "рыбы"),
    ],
)
def test_a_guarded_stem_links(head, form):
    amount = only(f"Добавьте 200 г {form}", names=(head,))

    assert linked(amount)
    assert amount.ingredient_position == 0


def test_a_generic_word_and_a_specific_product_are_ambiguous():
    amount = only("Добавьте 5 г соли", names=("соль", "морская соль"))

    assert amount.status is AmountLinkStatus.AMBIGUOUS
    assert amount.ingredient_position is None


def test_a_specific_product_links_only_through_its_distinguishing_word():
    specific = only(
        "Добавьте 5 г морской соли", names=("соль", "морская соль")
    )

    assert linked(specific)
    assert specific.ingredient_position == 1


def test_a_specific_product_without_its_word_is_not_a_mention():
    amount = only("Добавьте 200 г крахмала", names=("картофельный крахмал",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_MENTION


def test_two_different_products_are_never_merged():
    result = scan(
        "Добавьте 200 г муки и 200 г крахмала.",
        names=("мука", "картофельный крахмал"),
    )

    assert result.amounts[0].ingredient_position == 0
    assert result.amounts[1].status is AmountLinkStatus.UNRESOLVED


def test_a_curated_alias_links_when_only_one_side_is_an_ingredient():
    amount = only("Добавьте 200 г томатов", names=("помидор",))

    assert linked(amount)
    assert amount.ingredient_position == 0


def test_an_alias_is_ambiguous_when_both_products_are_ingredients():
    amount = only("Добавьте 200 г томатов", names=("помидор", "томат"))

    assert amount.status is AmountLinkStatus.AMBIGUOUS


def test_an_unknown_word_stays_unresolved_rather_than_fuzzy_matched():
    amount = only("Добавьте 200 г мукки", names=("мука",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_MENTION


# --- sentence boundaries and abbreviations --------------------------------------------


def test_a_unit_abbreviation_does_not_split_the_sentence():
    amount = only("Добавьте 1 ст. л. соли.")

    assert linked(amount)
    assert amount.ingredient_position == 3


def test_a_capital_after_an_abbreviation_starts_a_new_sentence():
    amount = only("Добавьте 1 ст. л. Соли.")

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.ingredient_position is None


def test_a_name_in_the_next_sentence_never_links():
    result = scan("Добавьте 500 мл. Воды", names=("вода",))

    [amount] = result.amounts
    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.ingredient_position is None


# --- repeated use, determinism and caps ------------------------------------------------


def test_one_ingredient_used_in_several_steps_is_linked_independently():
    result = scan_recipe_steps(
        ["Добавьте 500 мл воды.", "Влейте 250 мл воды."],
        [Ingredient("вода", "1 л")],
    )

    assert [amount.ingredient_position for amount in result.amounts] == [0, 0]
    assert [amount.status for amount in result.amounts] == [
        AmountLinkStatus.LINKED,
        AmountLinkStatus.LINKED,
    ]


def test_the_per_step_cap_keeps_a_deterministic_prefix_and_counts_omissions():
    text = " и ".join(["1 cup"] * 40)

    result = scan_step(text, [], step_index=4)

    assert len(result.amounts) == MAX_SPANS_PER_STEP
    assert result.omitted == 40 - MAX_SPANS_PER_STEP
    assert result.text == text
    assert [amount.start for amount in result.amounts] == sorted(
        amount.start for amount in result.amounts
    )


def test_the_per_recipe_cap_is_a_prefix_in_step_and_start_order():
    steps = [" и ".join(["1 cup"] * 30) for _ in range(5)]

    result = scan_recipe_steps(steps, [])

    assert len(result.amounts) == MAX_SPANS_PER_RECIPE
    assert result.omitted == 150 - MAX_SPANS_PER_RECIPE
    assert [len(step.amounts) for step in result.steps] == [30, 30, 30, 30, 8]
    assert result.complete is False
    assert [step.text for step in result.steps] == steps


def test_a_cap_never_truncates_the_stored_step_text():
    text = " и ".join(["1 cup"] * 40)

    result = scan_step(text, [])

    assert result.text == text
    assert all(text[amount.start : amount.end] == amount.text for amount in result.amounts)


# --- bounded scanning -----------------------------------------------------------------


def test_an_adversarial_maximum_step_is_bounded_and_does_not_crash():
    text = ("500 мл " * 143)[:1000]

    started = time.perf_counter()
    result = scan_step(text, ["вода"])
    elapsed = time.perf_counter() - started

    assert result.text == text
    assert elapsed < 5.0
    assert len(result.amounts) <= MAX_SPANS_PER_STEP


def test_a_thousand_digits_are_scanned_without_a_span():
    text = "9" * 1000

    started = time.perf_counter()
    result = scan_step(text, [])
    elapsed = time.perf_counter() - started

    assert result.amounts == ()
    assert elapsed < 5.0


# --- all five outcomes and Stage A preservation ---------------------------------------


def test_all_five_outcomes_are_reachable():
    linked_amount = only("Добавьте 500 мл воды.")
    unresolved_amount = only("Добавьте 500 мл и перемешайте.")
    ambiguous_amount = only("Вода — 500 мл молока")
    shared_amount = only("Смешайте 500 мл воды и молока.")
    unsupported_amount = only("2–3 ст. л. масла")

    assert linked_amount.status is AmountLinkStatus.LINKED
    assert unresolved_amount.status is AmountLinkStatus.UNRESOLVED
    assert ambiguous_amount.status is AmountLinkStatus.AMBIGUOUS
    assert shared_amount.status is AmountLinkStatus.SHARED
    assert unsupported_amount.status is AmountLinkStatus.UNSUPPORTED
    assert all(
        amount.ingredient_position is None
        for amount in (
            unresolved_amount,
            ambiguous_amount,
            shared_amount,
            unsupported_amount,
        )
    )


def test_stage_a_provenance_is_unchanged():
    audit = audit_source_provenance([Ingredient("вода", "1 стакан (200 мл)")])

    [entry] = audit.entries
    assert entry.kind is ProvenanceKind.GLASS
    assert entry.status is TermProvenanceStatus.DECLARED
    assert entry.capacity_ml == 200


def test_scanning_a_recipe_does_not_touch_its_provenance_or_text():
    ingredients = (
        Ingredient("вода", "1 стакан (200 мл)"),
        Ingredient("мука", "500 мл"),
    )
    steps = ("Добавьте 500 мл воды.", "Добавьте 1 стакан муки.")
    audit_before = audit_source_provenance(ingredients)

    scan_recipe_steps(steps, ingredients)

    assert audit_source_provenance(ingredients) == audit_before
    assert [ingredient.amount for ingredient in ingredients] == [
        "1 стакан (200 мл)",
        "500 мл",
    ]


# --- KH-F01-B1.1-B targeted remediation R1 (H1-H7, M8-M10) ---------------------------


def test_h1_genitive_noun_still_links():
    # Positive control: the noun genitive is a real mention, not demoted as an adjective.
    amount = only("100 мл сыра", names=("сыр",))

    assert linked(amount)
    assert amount.ingredient_position == 0


def test_h1_adjective_modifier_is_not_the_cheese_ingredient():
    amount = only("100 мл сырого свежего молока", names=("сыр", "молоко"))

    assert linked(amount)
    assert amount.ingredient_position == 1
    assert amount.reason is AmountLinkReason.NAME_AFTER


def test_h1_single_ingredient_does_not_link_an_adjective_modifier():
    amount = only("100 мл сырого молока", names=("сыр",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_MENTION
    assert amount.ingredient_position is None


def test_h1_adjective_form_is_not_promoted_from_a_matching_stem():
    # ``сырного`` modifies ``продукта``; only the ingredient's own head may link.
    amount = only("100 мл сырного продукта", names=("сыр",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_MENTION


H2_REPRODUCTIONS = [
    ("1 cup + 100 мл воды", "1 cup + 100 мл", AmountLinkReason.US_CUP),
    (
        "2–3 ст. л. + 100 мл воды",
        "2–3 ст. л. + 100 мл",
        AmountLinkReason.RANGE,
    ),
    (
        "1 ст. л. с горкой 2 г 100 мл воды",
        "1 ст. л. с горкой 2 г 100 мл",
        AmountLinkReason.HEAPED,
    ),
    (
        "1 стакан на 200 л воды",
        "1 стакан на 200 л",
        AmountLinkReason.MALFORMED_EXPRESSION,
    ),
]


@pytest.mark.parametrize("text, expected_text, reason", H2_REPRODUCTIONS)
def test_h2_a_dependent_tail_never_escapes_a_rejected_expression(
    text, expected_text, reason
):
    result = scan(text, names=("вода",))

    [amount] = result.amounts
    assert amount.text == expected_text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is reason
    assert linked(amount) is False


def test_h2_an_independent_amount_after_a_sentence_boundary_still_links():
    result = scan("1 cup воды. Добавьте 100 мл воды.", names=("вода",))

    assert [amount.text for amount in result.amounts] == ["1 cup", "100 мл"]
    assert result.amounts[0].reason is AmountLinkReason.US_CUP
    assert result.amounts[1].status is AmountLinkStatus.LINKED


@pytest.mark.parametrize("text", ["1//2 мл воды", "1:2 мл воды"])
def test_h3_a_malformed_numeric_cluster_is_rejected_whole(text):
    result = scan(text, names=("вода",))

    [amount] = result.amounts
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.MALFORMED_EXPRESSION
    assert amount.text == text[: text.index("мл") + 2]
    assert linked(amount) is False


@pytest.mark.parametrize("text", ["1//2", "1:2.", "Смешайте 1:2."])
def test_h3_a_ratio_alone_yields_no_span(text):
    assert scan(text, names=("вода",)).amounts == ()


def test_h3_a_colon_separator_after_a_name_still_links():
    amount = only("вода: 500 мл", names=("вода",))

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_BEFORE_SEPARATOR


H4_REPRODUCTIONS = [
    "100 мл воды и холодного молока",
    "Вода и холодное молоко — 100 мл",
    "Вода и холодное молоко отмерьте 100 мл",
]


@pytest.mark.parametrize("text", H4_REPRODUCTIONS)
def test_h4_a_coordinated_group_with_modifiers_is_shared(text):
    amount = only(text, names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.SHARED
    assert amount.reason is AmountLinkReason.SHARED_AMOUNT
    assert amount.ingredient_position is None


def test_h4_a_comma_group_with_a_modifier_is_shared():
    amount = only("100 мл воды, холодного молока", names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.SHARED


def test_h4_independent_amounts_keep_their_own_names():
    result = scan("100 мл молока и 100 мл воды", names=("вода", "молоко"))

    assert [amount.status for amount in result.amounts] == [
        AmountLinkStatus.LINKED,
        AmountLinkStatus.LINKED,
    ]
    assert [amount.ingredient_position for amount in result.amounts] == [1, 0]


H5_REPRODUCTIONS = [
    "Сливочное молоко, 100 мл масла",
    "Сливочное молоко, масло — 100 мл",
]


@pytest.mark.parametrize("text", H5_REPRODUCTIONS)
def test_h5_a_modifier_is_not_borrowed_across_a_phrase_boundary(text):
    amount = only(text, names=("масло", "сливочное масло"))

    assert amount.status is AmountLinkStatus.AMBIGUOUS
    assert amount.reason is AmountLinkReason.MULTIPLE_CANDIDATES
    assert amount.ingredient_position is None


def test_h5_the_local_modifier_still_identifies_the_specific_product():
    amount = only("100 мл сливочного масла", names=("масло", "сливочное масло"))

    assert linked(amount)
    assert amount.ingredient_position == 1


@pytest.mark.parametrize(
    "text",
    [
        "Возьмите кастрюлю объемом 3 л для воды",
        "Возьмите чашу вместимостью 2 л для воды",
    ],
)
def test_h6_a_container_capacity_is_not_an_ingredient_amount(text):
    amount = only(text, names=("вода",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text", ["100 мл налей воды", "100 мл налейте воды", "100 мл перемешайте воды"]
)
def test_h6_an_intervening_finite_verb_blocks_l1(text):
    amount = only(text, names=("вода",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text",
    [
        "Возьмите половину от указанных 500 мл воды",
        "Возьмите половину от примерно 500 мл воды",
        "Возьмите часть от указанных 500 мл воды",
    ],
)
def test_h7_a_governing_portion_word_is_not_lost_behind_modifiers(text):
    amount = only(text, names=("вода",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.ANAPHORA
    assert amount.ingredient_position is None


def test_h7_a_plain_amount_is_not_governed_by_a_distant_portion_word():
    amount = only("Возьмите 500 мл воды", names=("вода",))

    assert linked(amount)


def test_m8_binding_is_skipped_beyond_the_per_step_cap(monkeypatch):
    calls = []
    original = step_scan_module._bind

    def counting(ctx, expression):
        calls.append(expression)
        return original(ctx, expression)

    monkeypatch.setattr(step_scan_module, "_bind", counting)
    text = "; ".join(["500 мл воды"] * 40)

    result = scan_step(text, ["вода"])

    assert len(result.amounts) == MAX_SPANS_PER_STEP
    assert result.omitted == 40 - MAX_SPANS_PER_STEP
    assert len(calls) == MAX_SPANS_PER_STEP


def test_m8_binding_budget_is_shared_across_the_recipe(monkeypatch):
    calls = []
    original = step_scan_module._bind

    def counting(ctx, expression):
        calls.append(expression)
        return original(ctx, expression)

    monkeypatch.setattr(step_scan_module, "_bind", counting)
    steps = ["; ".join(["500 мл воды"] * 30) for _ in range(5)]

    result = scan_recipe_steps(steps, ["вода"])

    assert len(result.amounts) == MAX_SPANS_PER_RECIPE
    assert result.omitted == 150 - MAX_SPANS_PER_RECIPE
    assert [len(step.amounts) for step in result.steps] == [30, 30, 30, 30, 8]
    assert len(calls) == MAX_SPANS_PER_RECIPE


def test_m8_a_period_is_not_rescanned_by_previous_word(monkeypatch):
    calls = []
    original = step_scan_module._previous_word

    def counting(ends, norms, position):
        calls.append(position)
        return original(ends, norms, position)

    monkeypatch.setattr(step_scan_module, "_previous_word", counting)
    text = "Смешайте 500 мл воды. " * 30

    scan_step(text, ["вода"])

    assert len(calls) == text.count(".")


@pytest.mark.parametrize("fraction", ["500/2", "1000/4", "250"])
def test_m9_a_fractional_capacity_matches_its_evaluated_capacity(fraction):
    text = f"1 стакан ({fraction} мл) воды"
    amount = only(text, names=("вода",))

    assert linked(amount)
    assert amount.text == f"1 стакан ({fraction} мл)"


def test_m9_a_fractional_capacity_is_the_same_span_as_a_plain_capacity():
    fractional = only("1 стакан (500/2 мл) воды", names=("вода",))
    plain = only("1 стакан (250 мл) воды", names=("вода",))

    assert fractional.status is plain.status is AmountLinkStatus.LINKED
    assert fractional.ingredient_position == plain.ingredient_position == 0


@pytest.mark.parametrize(
    "text, reason",
    [
        ("1 стакан (3000/2 мл) воды", AmountLinkReason.UNSUPPORTED_CONSTRUCT),
        ("1 стакан (149/3 мл) воды", AmountLinkReason.UNSUPPORTED_CONSTRUCT),
        ("1 стакан (3001/3 мл) воды", AmountLinkReason.UNSUPPORTED_CONSTRUCT),
        ("1 стакан (49½ мл) воды", AmountLinkReason.UNSUPPORTED_CONSTRUCT),
        ("1 стакан (1000½ мл) воды", AmountLinkReason.UNSUPPORTED_CONSTRUCT),
        ("1 стакан (500/0 мл) воды", AmountLinkReason.MALFORMED_EXPRESSION),
    ],
)
def test_m9_an_unsafe_fractional_capacity_is_rejected(text, reason):
    amount = only(text, names=("вода",))

    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is reason
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "space",
    [0x2009, 0x2003, 0x2002, 0x00A0, 0x202F, 0x3000],
)
def test_m10_unicode_horizontal_whitespace_is_supported_in_place(space):
    gap = chr(space)
    text = f"100{gap}мл{gap}воды"
    [amount] = scan(text, names=("вода",)).amounts

    assert amount.text == f"100{gap}мл"
    assert text[amount.start : amount.end] == amount.text
    assert amount.start == text.index("100")
    assert amount.status is AmountLinkStatus.LINKED


def test_m10_unicode_whitespace_keeps_offsets_around_astral_characters():
    thin = chr(0x2009)
    text = f"🥛 100{thin}мл{thin}воды"
    amount = only(text, names=("вода",))

    assert amount.text == f"100{thin}мл"
    assert text[amount.start : amount.end] == amount.text
    assert amount.status is AmountLinkStatus.LINKED


def test_remediation_preserves_the_frozen_positive_constructions():
    assert only("Добавьте 500 мл воды.").reason is AmountLinkReason.NAME_AFTER
    assert only("Вода — 500 мл").reason is AmountLinkReason.NAME_BEFORE_SEPARATOR
    assert only("Воду добавьте 500 мл").reason is AmountLinkReason.NAME_BEFORE_COPULA
    result = scan("100 мл молока и 100 мл воды", names=("вода", "молоко"))
    assert [amount.ingredient_position for amount in result.amounts] == [1, 0]


def test_remediation_keeps_spans_exact_slices_through_every_finding():
    texts = [text for text, _, _ in H2_REPRODUCTIONS] + [
        "1//2 мл воды",
        "1:2 мл воды",
        *H4_REPRODUCTIONS,
        *H5_REPRODUCTIONS,
        "Возьмите кастрюлю объемом 3 л для воды",
        "100 мл налей воды",
        "Возьмите половину от указанных 500 мл воды",
        "1 стакан (500/2 мл) воды",
        "100" + chr(0x2009) + "мл воды",
    ]
    for text in texts:
        for amount in scan(text, names=("вода", "молоко", "масло")).amounts:
            assert text[amount.start : amount.end] == amount.text


# --- KH-F01-B1.1-B targeted remediation R2 (H2, H3, H4, H6, H7, M8, M9) ----------------


H2_R2_REPRODUCTIONS = [
    ("1 cup (200 мл) + 100 мл воды", "1 cup (200 мл) + 100 мл"),
    ("1 cup + 1 cup + 100 мл воды", "1 cup + 1 cup + 100 мл"),
    ("Вода — 100 мл + 1 cup", "100 мл + 1 cup"),
    ("1 пачка + 100 мл воды", "1 пачка + 100 мл"),
]


@pytest.mark.parametrize("text, expected_text", H2_R2_REPRODUCTIONS)
def test_h2_r2_a_rejected_compound_keeps_every_dependent_numeric_term(
    text, expected_text
):
    result = scan(text, names=("вода",))

    [amount] = result.amounts
    assert amount.text == expected_text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason in {AmountLinkReason.US_CUP, AmountLinkReason.PACKAGE}
    assert amount.ingredient_position is None
    assert linked(amount) is False


@pytest.mark.parametrize(
    "text, expected_text",
    [
        ("1 cup (200 мл) + 1 cup", "1 cup (200 мл) + 1 cup"),
        ("1 пачка (400 г) + 100 мл воды", "1 пачка (400 г) + 100 мл"),
        ("2–3 ст. л. + 100 мл воды", "2–3 ст. л. + 100 мл"),
        (
            "1 cup (200 мл (300 мл)) + 100 мл воды",
            "1 cup (200 мл (300 мл)) + 100 мл",
        ),
    ],
)
def test_h2_r2_mutation_variants_stay_one_rejected_span(text, expected_text):
    [amount] = scan(text, names=("вода",)).amounts

    assert amount.text == expected_text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text, expected_texts",
    [
        ("1 cup и 100 мл воды", ["1 cup и 100 мл"]),
        ("1 cup и 1 cup", ["1 cup", "1 cup"]),
        ("1 cup и 1 пачка", ["1 cup", "1 пачка"]),
    ],
)
def test_h2_r2_the_natural_separator_follows_only_a_supported_term(
    text, expected_texts
):
    result = scan(text, names=("вода",))

    assert [amount.text for amount in result.amounts] == expected_texts
    assert all(
        amount.status is AmountLinkStatus.UNSUPPORTED
        for amount in result.amounts
    )
    assert all(amount.ingredient_position is None for amount in result.amounts)


def test_h2_r2_a_supported_compound_still_links_its_own_name():
    amount = only("100 мл + 100 мл воды", names=("вода",))

    assert amount.text == "100 мл + 100 мл"
    assert linked(amount)
    assert amount.ingredient_position == 0


def test_h2_r2_an_independent_sentence_keeps_its_own_amount():
    result = scan("1 cup воды. Добавьте 100 мл воды.", names=("вода",))

    assert [amount.text for amount in result.amounts] == ["1 cup", "100 мл"]
    assert result.amounts[0].status is AmountLinkStatus.UNSUPPORTED
    assert result.amounts[1].status is AmountLinkStatus.LINKED


H3_R2_SPACED_CLUSTERS = [
    "1// 2 мл воды",
    "1 : 2 мл воды",
    "1: 2 мл воды",
    "1 //2 мл воды",
    "1 \\ 2 мл воды",
]


@pytest.mark.parametrize("text", H3_R2_SPACED_CLUSTERS)
def test_h3_r2_a_spaced_malformed_cluster_is_consumed_whole(text):
    result = scan(text, names=("вода",))

    assert result.amounts == ()
    assert all(not linked(amount) for amount in result.amounts)


@pytest.mark.parametrize(
    "text, expected_text",
    [
        ("1 // 2 мл воды", None),
        ("1::2 мл воды", "1::2 мл"),
    ],
)
def test_h3_r2_cluster_adjacency_decides_span_but_never_a_link(
    text, expected_text
):
    result = scan(text, names=("вода",))

    if expected_text is None:
        assert result.amounts == ()
        return
    [amount] = result.amounts
    assert amount.text == expected_text
    assert amount.reason is AmountLinkReason.MALFORMED_EXPRESSION
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text",
    [
        "Смешайте в пропорции 1:2;100 мл воды добавьте позже.",
        "Смешайте в пропорции 1:2; 100 мл воды добавьте позже.",
    ],
)
def test_h3_r2_an_independent_amount_after_a_semicolon_is_not_absorbed(text):
    result = scan(text, names=("вода",))

    [amount] = result.amounts
    assert amount.text == "100 мл"
    assert linked(amount)
    assert amount.ingredient_position == 0


@pytest.mark.parametrize(
    "text, linked_text",
    [
        ("1/2 стакана молока", "1/2 стакана"),
        ("вода: 500 мл", "500 мл"),
    ],
)
def test_h3_r2_positive_controls_keep_legal_fraction_and_separator_grammar(
    text, linked_text
):
    amount = only(text, names=("вода", "молоко"))

    assert amount.text == linked_text
    assert linked(amount)


H4_R2_REPRODUCTIONS = [
    "100 мл воды и холодного, свежего молока",
    "Вода и холодное, свежее молоко — 100 мл",
    "Вода и холодное, свежее молоко отмерьте 100 мл",
    "И вода, и молоко — 100 мл",
]


@pytest.mark.parametrize("text", H4_R2_REPRODUCTIONS)
def test_h4_r2_a_coordinated_group_with_comma_modifiers_is_shared(text):
    amount = only(text, names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.SHARED
    assert amount.reason is AmountLinkReason.SHARED_AMOUNT
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text",
    [
        "100 мл воды, холодного, свежего молока",
        "Вода, и молоко — 100 мл",
        "Смешайте 100 мл воды и холодного, свежего, кипяченого молока",
    ],
)
def test_h4_r2_comma_and_repeated_coordinator_mutations_are_shared(text):
    amount = only(text, names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.SHARED
    assert amount.ingredient_position is None


def test_h4_r2_independent_quantities_keep_their_own_names():
    result = scan("100 мл молока и 100 мл воды", names=("вода", "молоко"))

    assert [amount.status for amount in result.amounts] == [
        AmountLinkStatus.LINKED,
        AmountLinkStatus.LINKED,
    ]
    assert [amount.ingredient_position for amount in result.amounts] == [1, 0]


def test_h4_r2_a_comma_before_an_unrelated_clause_is_not_shared():
    amount = only(
        "100 мл воды, перемешайте с молоком", names=("вода", "молоко")
    )

    assert linked(amount)
    assert amount.ingredient_position == 0


H6_R2_NEGATIVES = [
    "100 мл подлей воды",
    "100 мл отлей воды в чашку",
    "100 мл подогрей молока",
    "100 мл подлейте воды",
    "100 мл залейте воды",
    "Отмерьте 100 мл без воды",
    "Отмерьте 100 мл без холодной воды",
    "Возьмите кастрюлю на 3 л для воды",
    "Возьмите миску на 2 л для воды",
    "Возьмите кастрюлю объемом примерно 3 л для воды",
]


@pytest.mark.parametrize("text", H6_R2_NEGATIVES)
def test_h6_r2_an_unapproved_bridge_never_links(text):
    amount = only(text, names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text, expected_position",
    [
        ("Добавьте 500 мл воды.", 0),
        ("Добавьте 500 мл сырого молока.", 1),
        ("Влейте 300 мл молока в кастрюлю.", 1),
        ("Возьмите 3 л воды.", 0),
        ("Добавьте 200 г пшеничной муки.", 3),
    ],
)
def test_h6_r2_positive_controls_keep_approved_l1_links(
    text, expected_position
):
    amount = only(text, names=("вода", "молоко", "мука", "пшеничная мука"))

    assert linked(amount)
    assert amount.ingredient_position == expected_position


H7_R2_REPRODUCTIONS = [
    "Возьмите половину от этих же указанных примерно 500 мл воды",
    "Возьмите столько же из 500 мл воды",
    "Возьмите часть от этих же указанных примерно 500 мл воды",
    "Возьмите половину от указанных же 500 мл воды",
]


@pytest.mark.parametrize("text", H7_R2_REPRODUCTIONS)
def test_h7_r2_the_governing_chain_survives_particles_and_modifiers(text):
    amount = only(text, names=("вода",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.ANAPHORA
    assert amount.ingredient_position is None


def test_h7_r2_a_plain_amount_is_not_governed_by_a_word_that_is_not_a_portion():
    amount = only("Возьмите ровно 500 мл воды", names=("вода",))

    assert linked(amount)
    assert amount.ingredient_position == 0


@pytest.mark.parametrize("modifiers", [20, 80, 320])
def test_m8_r2_adjective_checks_stay_bounded_without_a_quantity(
    modifiers, monkeypatch
):
    calls = []
    original = step_scan_module._looks_like_adjective

    def counting(word):
        calls.append(word)
        return original(word)

    monkeypatch.setattr(step_scan_module, "_looks_like_adjective", counting)
    text = "свежего " * modifiers + "молока"

    result = scan_step(text, ["молоко"])

    assert result.amounts == ()
    # Only the ingredient's own name is classified; no per-word adjective walk runs.
    assert len(calls) <= 3


@pytest.mark.parametrize("modifiers", [20, 80, 320])
def test_m8_r2_adjective_checks_grow_linearly_with_a_quantity(
    modifiers, monkeypatch
):
    calls = []
    original = step_scan_module._looks_like_adjective

    def counting(word):
        calls.append(word)
        return original(word)

    monkeypatch.setattr(step_scan_module, "_looks_like_adjective", counting)
    text = "100 мл " + "свежего " * modifiers + "молока"

    scan_step(text, ["молоко"])

    # One call per word plus the name's head: linear in the modifier count, not quadratic.
    assert len(calls) <= modifiers + 8


def test_m8_r2_mention_construction_is_skipped_without_a_candidate(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("mentions must not be built without a quantity")

    monkeypatch.setattr(step_scan_module, "_mentions", forbidden)

    assert scan_step("свежего молока", ["молоко"]).amounts == ()


@pytest.mark.parametrize(
    "size",
    ["500/3", "1000/3", "237½", "237 1/2", "150/3", "3000/3"],
)
def test_m9_r2_an_exact_supported_fraction_capacity_links(size):
    text = f"1 стакан ({size} мл) воды"
    amount = only(text, names=("вода",))

    assert linked(amount)
    assert amount.text == text[: -len(" воды")]
    assert text[amount.start : amount.end] == amount.text
    assert amount.ingredient_position == 0


@pytest.mark.parametrize(
    "text, expected_status",
    [
        ("1 стакан (150/3 мл) воды", AmountLinkStatus.LINKED),
        ("1 стакан (149/3 мл) воды", AmountLinkStatus.UNSUPPORTED),
        ("1 стакан (3000/3 мл) воды", AmountLinkStatus.LINKED),
        ("1 стакан (3001/3 мл) воды", AmountLinkStatus.UNSUPPORTED),
        ("1 стакан (49½ мл) воды", AmountLinkStatus.UNSUPPORTED),
        ("1 стакан (50 мл) воды", AmountLinkStatus.LINKED),
        ("1 стакан (1000 мл) воды", AmountLinkStatus.LINKED),
        ("1 стакан (1000½ мл) воды", AmountLinkStatus.UNSUPPORTED),
    ],
)
def test_m9_r2_the_fence_is_exact_and_never_rounds(text, expected_status):
    amount = only(text, names=("вода",))

    assert amount.status is expected_status
    assert text[amount.start : amount.end] == amount.text


def test_m9_r2_terminating_and_non_terminating_fractions_are_both_supported():
    terminating = only("1 стакан (500/2 мл) воды", names=("вода",))
    non_terminating = only("1 стакан (500/3 мл) воды", names=("вода",))
    vulgar_mixed = only("1 стакан (237½ мл) воды", names=("вода",))

    assert terminating.status is AmountLinkStatus.LINKED
    assert non_terminating.status is AmountLinkStatus.LINKED
    assert vulgar_mixed.status is AmountLinkStatus.LINKED
    assert terminating.ingredient_position == 0
    assert non_terminating.ingredient_position == 0
    assert vulgar_mixed.ingredient_position == 0


# --- KH-F01-B1.1-B R3 mandatory corpus (Option B bounded parsing pipeline) ------------

R3_NAMES = ("вода", "молоко", "соль", "масло", "сахар")


def r3_scan(text, names=R3_NAMES):
    return scan_step(text, [Ingredient(name, "1 г") for name in names])


def r3_only(text, names=R3_NAMES):
    [amount] = r3_scan(text, names).amounts
    return amount


# The two mandatory amendments -----------------------------------------------------------------


R3_INCOMPLETE_EXPRESSIONS = [
    # The approved examples.
    ("Вода — 100 мл +", "100 мл +"),
    ("100 мл плюс", "100 мл плюс"),
    ("100 мл ++", "100 мл ++"),
    ("Вода — 100 мл + +", "100 мл + +"),
    # Adversarial mutations: a bare numeric term, a name, and a mixed sign.
    ("100 мл + 200", "100 мл + 200"),
    ("100 мл + воды", "100 мл +"),
    ("100 мл плюс +", "100 мл плюс +"),
    ("1 л 200 мл +", "1 л 200 мл +"),
    ("1 стакан (200 мл) +", "1 стакан (200 мл) +"),
]


@pytest.mark.parametrize("text, expected_text", R3_INCOMPLETE_EXPRESSIONS)
def test_r3_amendment_a_an_incomplete_explicit_continuation_rejects_the_whole(
    text, expected_text
):
    result = r3_scan(text)

    [amount] = result.amounts
    assert amount.text == expected_text
    assert text[amount.start : amount.end] == amount.text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.MALFORMED_EXPRESSION
    assert amount.ingredient_position is None
    assert linked(amount) is False


def test_r3_amendment_a_a_complete_but_rejected_term_keeps_its_own_reason():
    [amount] = r3_scan("100 мл + 1 cup").amounts

    assert amount.text == "100 мл + 1 cup"
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.US_CUP
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text, expected_text",
    [
        ("100 мл + + 200 мл воды", "100 мл + + 200 мл"),
        ("100 мл ++ 200 мл воды", "100 мл ++ 200 мл"),
        ("100 мл плюс + 200 мл воды", "100 мл плюс + 200 мл"),
    ],
)
def test_r3_amendment_a_no_interior_quantity_escapes_a_malformed_expression(
    text, expected_text
):
    result = r3_scan(text)

    [amount] = result.amounts
    assert amount.text == expected_text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.MALFORMED_EXPRESSION
    assert all(not linked(amount) for amount in result.amounts)


@pytest.mark.parametrize(
    "text",
    [
        "100 мл +. 200 мл воды",
        "100 мл +; 200 мл воды",
        "100 мл + ? 200 мл воды",
    ],
)
def test_r3_amendment_a_an_independent_expression_after_a_boundary_is_recovered(
    text,
):
    result = r3_scan(text)

    assert [amount.status for amount in result.amounts] == [
        AmountLinkStatus.UNSUPPORTED,
        AmountLinkStatus.LINKED,
    ]
    assert result.amounts[0].reason is AmountLinkReason.MALFORMED_EXPRESSION
    assert result.amounts[1].text == "200 мл"
    assert result.amounts[1].ingredient_position == 0


def test_r3_amendment_b_an_independent_clause_after_a_comma_links_the_later_amount():
    amount = r3_only("Отложите половину, 500 мл воды добавьте позже")

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0


def test_r3_amendment_b_a_continuing_portion_construction_stays_anaphoric():
    amount = r3_only("Возьмите половину, от 500 мл воды")

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.ANAPHORA
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text",
    [
        "Возьмите половину от имеющихся 500 мл воды",
        "Возьмите половину от всех указанных 500 мл воды",
        "Возьмите половину от этих же указанных примерно 500 мл воды",
        "Возьмите половину от указанных же 500 мл воды",
    ],
)
def test_r3_amendment_b_a_governing_chain_survives_quantifiers_and_participles(
    text,
):
    amount = r3_only(text)

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.ANAPHORA
    assert amount.ingredient_position is None


def test_r3_amendment_b_a_semicolon_is_an_independent_clause_boundary():
    amount = r3_only("Отложите половину; 500 мл воды добавьте позже")

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER


@pytest.mark.parametrize(
    "text",
    [
        "Возьмите половину, 500 мл воды",
        "Возьмите половину, 500 мл",
        "Возьмите половину, примерно 500 мл воды",
        "Возьмите половину, 500 мл воды для теста",
    ],
)
def test_r3_amendment_b_uncertain_portion_governance_is_conservatively_unresolved(
    text,
):
    [amount] = r3_scan(text).amounts

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.ingredient_position is None
    assert amount.reason is not AmountLinkReason.ANAPHORA


def test_r3_amendment_b_a_comma_does_not_introduce_pronoun_resolution():
    # No numeric span exists, and no ingredient is inferred from the portion word.
    assert r3_scan("Возьмите половину, его", names=("вода",)).amounts == ()


@pytest.mark.parametrize(
    "text",
    [
        "Возьмите половину, ровно 500 мл воды",
        "Возьмите половину, только 500 мл воды",
        "Возьмите половину, лишь 500 мл воды",
        "Возьмите половину, даже 500 мл воды",
        "Возьмите половину, ещё 500 мл воды",
        "Возьмите половину, и 500 мл воды",
    ],
)
def test_r3_amendment_b_a_neutral_filler_does_not_end_uncertain_governance(text):
    [amount] = r3_scan(text).amounts

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None


def test_r3_amendment_b_an_independent_predicate_still_links_through_a_filler():
    amount = r3_only("Возьмите половину, ровно 500 мл воды добавьте позже")

    assert linked(amount)
    assert amount.ingredient_position == 0


# The eight owner product decisions ------------------------------------------------------------


R3_MULTIPLE_CAPACITIES = [
    (
        "1 стакан (200 мл) (250 мл) воды",
        "1 стакан (200 мл) (250 мл)",
        AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    ),
    (
        "1 стакан (200 мл) 250 мл воды",
        "1 стакан (200 мл) 250 мл",
        AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    ),
    (
        "1 стакан на 200 мл на 250 мл воды",
        "1 стакан на 200 мл на 250 мл",
        AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    ),
    (
        "1 стакан (200 мл) на 250 мл воды",
        "1 стакан (200 мл) на 250 мл",
        AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    ),
    (
        "1 стакан (200 мл) (200 мл) воды",
        "1 стакан (200 мл) (200 мл)",
        AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    ),
    (
        "1 стакан (200 мл) (250 мл воды",
        "1 стакан (200 мл) (250 мл воды",
        AmountLinkReason.MALFORMED_EXPRESSION,
    ),
    (
        "1 стакан (200 мл) 250 воды",
        "1 стакан (200 мл) 250",
        AmountLinkReason.MALFORMED_EXPRESSION,
    ),
]


@pytest.mark.parametrize("text, expected_text, reason", R3_MULTIPLE_CAPACITIES)
def test_r3_decision_1_a_second_capacity_declaration_is_rejected_whole(
    text, expected_text, reason
):
    result = r3_scan(text)

    [amount] = result.amounts
    assert amount.text == expected_text
    assert text[amount.start : amount.end] == amount.text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is reason
    assert amount.ingredient_position is None


def test_r3_decision_1_a_single_capacity_still_links():
    amount = r3_only("1 стакан (200 мл) воды")

    assert linked(amount)
    assert amount.ingredient_position == 0


@pytest.mark.parametrize(
    "text",
    [
        "1 // 2 мл воды",
        "1 : 2 мл воды",
        "1, 2 мл воды",
    ],
)
def test_r3_decision_2_a_spaced_malformed_numeric_cluster_yields_no_span(text):
    assert r3_scan(text).amounts == ()


@pytest.mark.parametrize(
    "text, expected_text",
    [
        ("1. 100 мл воды", "100 мл"),
        ("10. 100 мл воды", "100 мл"),
        ("Шаг 1. 100 мл воды", "100 мл"),
        ("2. 200 г муки", "200 г"),
        ("1. 2 мл воды", "2 мл"),
        ("1.. 100 мл воды", "100 мл"),
        (" Возьмите 1. 100 мл воды", "100 мл"),
    ],
)
def test_r3_a_period_after_a_number_is_a_sentence_boundary(
    text, expected_text
):
    [amount] = r3_scan(text, names=("вода", "мука")).amounts

    assert amount.text == expected_text
    assert linked(amount)
    assert amount.ingredient_position is not None
    assert text[amount.start : amount.end] == amount.text


def test_r3_a_sentence_period_does_not_hide_a_second_amount():
    result = r3_scan(
        "1. 100 мл воды, 200 мл молока", names=("вода", "молоко")
    )

    assert [amount.text for amount in result.amounts] == ["100 мл", "200 мл"]
    assert [amount.status for amount in result.amounts] == [
        AmountLinkStatus.LINKED,
        AmountLinkStatus.LINKED,
    ]


@pytest.mark.parametrize(
    "text, expected_text",
    [
        ("1..2 мл воды", "1..2 мл"),
        ("1,,2 мл воды", "1,,2 мл"),
        ("1.2.3 мл воды", "1.2.3 мл"),
        ("1,2,3 мл воды", "1,2,3 мл"),
        ("1//2 мл воды", "1//2 мл"),
        ("1:2 мл воды", "1:2 мл"),
    ],
)
def test_r3_decision_2_an_adjacent_malformed_cluster_is_one_rejected_span(
    text, expected_text
):
    result = r3_scan(text)

    [amount] = result.amounts
    assert amount.text == expected_text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.MALFORMED_EXPRESSION


@pytest.mark.parametrize(
    "text, expected_text",
    [
        ("½.5 мл воды", "½.5 мл"),
        ("1½..2 мл воды", "1½..2 мл"),
    ],
)
def test_r3_a_vulgar_mixed_cluster_is_one_rejected_span(text, expected_text):
    [amount] = r3_scan(text).amounts

    assert amount.text == expected_text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.MALFORMED_EXPRESSION


@pytest.mark.parametrize(
    "text",
    [
        "Смешайте в пропорции 1:2. 100 мл воды добавьте позже.",
        "Смешайте в пропорции 1:2; 100 мл воды добавьте позже.",
        "Смешайте 1.2.3. 100 мл воды добавьте позже.",
    ],
)
def test_r3_a_malformed_numeric_token_never_absorbs_a_boundary_amount(text):
    [amount] = r3_scan(text).amounts

    assert amount.text == "100 мл"
    assert linked(amount)
    assert amount.ingredient_position == 0


R3_REPEATED_SEPARATORS = [
    "100 мл ++",
    "100 мл + +",
    "100 мл плюс плюс",
    "100 мл + и 200 мл воды",
    "100 мл и + 200 мл воды",
    "100 мл ++ 200 мл воды",
]


@pytest.mark.parametrize("text", R3_REPEATED_SEPARATORS)
def test_r3_decision_3_a_repeated_explicit_separator_run_is_malformed(text):
    result = r3_scan(text)

    [amount] = result.amounts
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.MALFORMED_EXPRESSION
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text, reason, expected_text",
    [
        ("1 cup + + 100 мл", AmountLinkReason.US_CUP, "1 cup + + 100 мл"),
        (
            "2–3 ст. л. + + 100 мл",
            AmountLinkReason.RANGE,
            "2–3 ст. л. + + 100 мл",
        ),
        (
            "1 пачка + + 100 мл",
            AmountLinkReason.PACKAGE,
            "1 пачка + + 100 мл",
        ),
    ],
)
def test_r3_decision_3_the_earliest_source_order_rejection_reason_is_kept(
    text, reason, expected_text
):
    result = r3_scan(text)

    [amount] = result.amounts
    assert amount.text == expected_text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is reason


@pytest.mark.parametrize(
    "text",
    ["500 мл воды и", "500 мл воды и сметаны", "Вода — 500 мл и"],
)
def test_r3_decision_4_inconclusive_coordination_is_unresolved(text):
    [amount] = r3_scan(text).amounts

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None


def test_r3_decision_4_a_positively_established_group_is_shared():
    amount = r3_only("500 мл воды и молока")

    assert amount.status is AmountLinkStatus.SHARED
    assert amount.reason is AmountLinkReason.SHARED_AMOUNT


def test_r3_decision_4_multiple_plausible_targets_are_ambiguous():
    amount = r3_only("Вода — 500 мл молока", names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.AMBIGUOUS
    assert amount.reason is AmountLinkReason.MULTIPLE_CANDIDATES


@pytest.mark.parametrize(
    "text",
    [
        "100 мл воды, молоко перемешайте",
        "100 мл воды, молока добавьте",
    ],
)
def test_r3_decision_5_a_comma_independent_clause_does_not_create_shared(text):
    amount = r3_only(text, names=("вода", "молоко"))

    assert linked(amount)
    assert amount.ingredient_position == 0
    assert amount.reason is AmountLinkReason.NAME_AFTER


def test_r3_decision_5_a_positive_comma_group_is_still_shared():
    amount = r3_only("100 мл воды, холодного молока", names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.SHARED


def test_r3_decision_5_uncertain_comma_structure_is_not_a_speculative_link():
    amount = r3_only("100 мл воды, сметаны", names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING


def test_r3_decision_6_an_unknown_adjective_like_word_is_not_a_modifier():
    amount = r3_only("100 мл бледного молока", names=("молоко",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None


def test_r3_decision_6_a_curated_modifier_still_links():
    amount = r3_only("100 мл свежего молока", names=("молоко",))

    assert linked(amount)
    assert amount.ingredient_position == 0


def test_r3_decision_6_an_unlisted_participle_still_extends_a_coordinated_phrase():
    # Phrase-boundary detection may use the closed adjective endings (minus `-ей`); it
    # can only produce `shared`, never a per-ingredient link.
    amount = r3_only(
        "Вода и шоколадная крошка — 100 мл",
        names=("вода", "шоколадная крошка"),
    )

    assert amount.status is AmountLinkStatus.SHARED
    assert amount.ingredient_position is None


def test_r3_decision_6_an_imperative_ending_in_ey_is_not_a_phrase_modifier():
    amount = r3_only("100 мл воды, подлей молока", names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.ingredient_position is None


def test_r3_a_distinguishing_modifier_does_not_cross_a_sentence_boundary():
    amount = r3_only(
        "Сливочное молоко. 100 мл масла",
        names=("масло", "сливочное масло"),
    )

    assert amount.status is AmountLinkStatus.AMBIGUOUS
    assert amount.reason is AmountLinkReason.MULTIPLE_CANDIDATES
    assert amount.ingredient_position is None


def test_r3_decision_7_commas_never_terminate_positive_anaphora_by_themselves():
    positive = r3_only("Возьмите половину, от 500 мл воды")
    independent = r3_only("Отложите половину, 500 мл воды добавьте позже")

    assert positive.reason is AmountLinkReason.ANAPHORA
    assert linked(independent)


def test_r3_decision_8_a_generic_specific_collision_is_ambiguous():
    amount = r3_only(
        "Добавьте 200 г муки", names=("мука", "пшеничная мука")
    )

    assert amount.status is AmountLinkStatus.AMBIGUOUS
    assert amount.reason is AmountLinkReason.MULTIPLE_CANDIDATES
    assert amount.ingredient_position is None


def test_r3_decision_8_a_local_distinguishing_modifier_resolves_the_collision():
    amount = r3_only(
        "Добавьте 200 г пшеничной муки", names=("мука", "пшеничная мука")
    )

    assert linked(amount)
    assert amount.ingredient_position == 1


# The retained positive grammar and the frozen regressions -------------------------------------


def test_r3_the_four_positive_constructions_still_link():
    assert r3_only("Добавьте 500 мл воды").reason is AmountLinkReason.NAME_AFTER
    assert (
        r3_only("Вода — 500 мл").reason
        is AmountLinkReason.NAME_BEFORE_SEPARATOR
    )
    assert (
        r3_only("Воду добавьте 500 мл").reason
        is AmountLinkReason.NAME_BEFORE_COPULA
    )
    l4 = r3_scan("100 мл молока и 100 мл воды", names=("вода", "молоко"))
    assert [amount.ingredient_position for amount in l4.amounts] == [1, 0]


def test_r3_all_five_link_outcomes_are_still_reachable():
    assert r3_only("Добавьте 500 мл воды").status is AmountLinkStatus.LINKED
    assert r3_only("Добавьте 500 мл и перемешайте").status is (
        AmountLinkStatus.UNRESOLVED
    )
    assert r3_only("Вода — 500 мл молока", names=("вода", "молоко")).status is (
        AmountLinkStatus.AMBIGUOUS
    )
    assert r3_only("Смешайте 500 мл воды и молока").status is (
        AmountLinkStatus.SHARED
    )
    assert r3_only("2–3 ст. л. масла").status is AmountLinkStatus.UNSUPPORTED


def test_r3_h1_h5_and_m10_regressions_are_preserved():
    h1 = r3_only("100 мл сырого свежего молока", names=("сыр", "молоко"))
    assert linked(h1)
    assert h1.ingredient_position == 1

    h5 = r3_only(
        "Сливочное молоко, 100 мл масла", names=("масло", "сливочное масло")
    )
    assert h5.status is AmountLinkStatus.AMBIGUOUS

    thin = chr(0x2009)
    text = f"100{thin}мл{thin}воды"
    m10 = r3_only(text, names=("вода",))
    assert m10.text == f"100{thin}мл"
    assert text[m10.start : m10.end] == m10.text
    assert linked(m10)


def test_r3_a_rejected_compound_recovers_independent_expressions():
    result = r3_scan("1 cup воды; 100 мл воды")

    assert [amount.status for amount in result.amounts] == [
        AmountLinkStatus.UNSUPPORTED,
        AmountLinkStatus.LINKED,
    ]
    assert result.amounts[0].reason is AmountLinkReason.US_CUP
    assert result.amounts[1].text == "100 мл"


def test_r3_exact_capacity_boundaries_survive_a_compound_expression():
    text = "1 стакан (500/3 мл) и 1 стакан (237½ мл) воды"
    [amount] = r3_scan(text, names=("вода",)).amounts

    assert amount.text == text[: -len(" воды")]
    assert text[amount.start : amount.end] == amount.text
    assert linked(amount)

    below = r3_only("1 стакан (49½ мл) воды", names=("вода",))
    above = r3_only("1 стакан (1000½ мл) воды", names=("вода",))
    edge_low = r3_only("1 стакан (50 мл) воды", names=("вода",))
    edge_high = r3_only("1 стакан (1000 мл) воды", names=("вода",))
    assert below.status is AmountLinkStatus.UNSUPPORTED
    assert above.status is AmountLinkStatus.UNSUPPORTED
    assert linked(edge_low)
    assert linked(edge_high)


def test_r3_unicode_spans_and_deterministic_ordering_are_exact():
    text = "🥛 100 мл воды и 250 мл молока 🥛"
    result = r3_scan(text, names=("вода", "молоко"))

    spans = [(amount.start, amount.end) for amount in result.amounts]
    assert spans == sorted(spans)
    assert all(
        left[1] <= right[0] for left, right in zip(spans, spans[1:])
    )
    assert all(
        text[amount.start : amount.end] == amount.text
        for amount in result.amounts
    )
    assert [amount.ingredient_position for amount in result.amounts] == [0, 1]
    assert result == r3_scan(text, names=("вода", "молоко"))


def test_r3_the_32_and_128_caps_keep_counts_and_complete_exact():
    steps = [" и ".join(["1 cup"] * 20) for _ in range(8)]

    result = scan_recipe_steps(steps, [])

    assert len(result.amounts) == MAX_SPANS_PER_RECIPE
    assert result.omitted == 160 - MAX_SPANS_PER_RECIPE
    assert result.complete is False
    assert [len(step.amounts) for step in result.steps] == [
        20,
        20,
        20,
        20,
        20,
        20,
        8,
        0,
    ]
    assert [step.text for step in result.steps] == steps

    small = scan_recipe_steps(["1 cup", "2 г"], [])
    assert small.complete is True
    assert small.omitted == 0


# Deterministic operation counts, not wall-clock thresholds -----------------------------------


def test_r3_expression_consumption_is_linear_in_the_term_count(monkeypatch):
    calls = []
    original = step_scan_module._match_basic_part

    def counting(text, position, sentence_end):
        calls.append(position)
        return original(text, position, sentence_end)

    monkeypatch.setattr(step_scan_module, "_match_basic_part", counting)
    for count in (20, 80, 320):
        calls.clear()
        text = "; ".join(["100 мл воды"] * count)
        result = scan_step(text, ["вода"])

        assert len(calls) <= count + 4
        assert len(result.amounts) == min(count, MAX_SPANS_PER_STEP)


def test_r3_validation_text_is_built_once_per_expression(monkeypatch):
    calls = []
    original = step_scan_module._validation_text

    def counting(text, start, end, substitutions):
        calls.append((start, end))
        return original(text, start, end, substitutions)

    monkeypatch.setattr(step_scan_module, "_validation_text", counting)
    text = " + ".join(["1 стакан (200 мл)"] * 8) + " воды"

    [amount] = scan_step(text, ["вода"]).amounts

    assert linked(amount)
    assert amount.text == text[: -len(" воды")]
    assert len(calls) == 1


def test_r3_a_long_isolated_chain_has_a_bounded_operation_count(monkeypatch):
    # Beyond the 1,000-character protocol limit; an isolated algorithmic check with a
    # deterministic count, never a CI wall-clock performance threshold.
    calls = []
    original = step_scan_module._match_basic_part

    def counting(text, position, sentence_end):
        calls.append(position)
        return original(text, position, sentence_end)

    monkeypatch.setattr(step_scan_module, "_match_basic_part", counting)
    text = " и ".join(["100 мл воды"] * 4000)

    result = scan_step(text, ["вода"])

    assert len(calls) <= 4004
    assert len(result.amounts) == MAX_SPANS_PER_STEP
    assert result.omitted == 4000 - MAX_SPANS_PER_STEP


def test_r3_a_long_alternating_supported_rejected_chain_is_bounded():
    # Pins the no-recursion path: the trailing chain is iterative in nested mode. This
    # shape (supported part, explicit sign, rejected term, repeated) used to add one
    # Python frame per repetition.
    text = "100 мл + 1 cup" + " + 100 мл + 1 cup" * 600

    result = scan_step(text, ["вода"])

    assert len(result.amounts) == 1
    assert result.omitted == 0
    [amount] = result.amounts
    assert amount.text == text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.US_CUP
    assert amount.ingredient_position is None


# --- KH-F01-B1.1-B R3-S1 focused binding stabilization (H1, H2, H3, M1) --------------


def test_s1_h1_an_adjective_form_does_not_establish_ingredient_identity():
    # Codex reproduction: the stem of `сырого` is `сыр`, but the word is the adjective,
    # not the ingredient noun.
    amount = r3_only("100 г сырого", names=("сыр",))

    assert amount.text == "100 г"
    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_MENTION
    assert amount.ingredient_position is None


def test_s1_h1_a_noun_genitive_still_links():
    amount = r3_only("100 г сыра", names=("сыр",))

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0


@pytest.mark.parametrize(
    "form", ["сыра", "сыру", "сыром", "сыре", "сыры", "сыров", "сырам"]
)
def test_s1_h1_every_noun_form_of_the_head_still_links(form):
    # Positive evidence is preserved: the ingredient's own noun forms are not demoted
    # along with the adjective forms that share their stem.
    amount = r3_only(f"100 г {form}", names=("сыр",))

    assert linked(amount)
    assert amount.ingredient_position == 0


@pytest.mark.parametrize(
    "form",
    [
        "сырого",
        "сырой",
        "сырая",
        "сырое",
        "сырым",
        "сырых",
        "сырому",
        "сырую",
    ],
)
def test_s1_h1_an_adjective_form_alone_is_never_the_head(form):
    # Every form of the closed adjective class is refused when no noun head supports it.
    # This is a class rule, not a growing adjective word list.
    amount = r3_only(f"100 г {form}", names=("сыр",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.ingredient_position is None


def test_s1_h1_the_adjective_still_modifies_its_own_noun_head():
    amount = r3_only("100 мл сырого молока", names=("сыр", "молоко"))

    assert linked(amount)
    assert amount.ingredient_position == 1


def test_s1_h1_a_specific_product_is_still_reached_through_its_modifier():
    amount = r3_only(
        "100 мл сырого свежего молока", names=("сыр", "молоко")
    )

    assert linked(amount)
    assert amount.ingredient_position == 1


def test_s1_h1_an_adjectival_head_is_still_its_own_noun():
    # `мороженое` is a noun in adjective shape. The rule compares the source word with
    # the ingredient's own head token, so such a name keeps linking through its forms.
    amount = r3_only("100 г мороженого", names=("мороженое",))

    assert linked(amount)
    assert amount.ingredient_position == 0


H2_REPRODUCTION = "Сливочное. Масло — 100 мл"


def test_s1_h2_a_modifier_does_not_cross_a_sentence_boundary():
    # Codex reproduction: the period ends the sentence, so `сливочное` never describes
    # the next sentence's `масло`.
    amount = r3_only(H2_REPRODUCTION, names=("масло", "сливочное масло"))

    assert amount.status is AmountLinkStatus.AMBIGUOUS
    assert amount.reason is AmountLinkReason.MULTIPLE_CANDIDATES
    assert amount.ingredient_position is None


def test_s1_h2_a_borrowed_modifier_never_creates_a_specific_link():
    amount = r3_only(H2_REPRODUCTION, names=("сливочное масло",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_MENTION
    assert amount.ingredient_position is None


def test_s1_h2_the_positive_phrase_still_links():
    amount = r3_only(
        "Сливочное масло — 100 мл", names=("масло", "сливочное масло")
    )

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_BEFORE_SEPARATOR
    assert amount.ingredient_position == 1


@pytest.mark.parametrize("delimiter", [".", "!", "?", ";"])
def test_s1_h2_every_sentence_delimiter_blocks_the_modifier(delimiter):
    text = f"Сливочное{delimiter} Масло — 100 мл"

    specific = r3_only(text, names=("сливочное масло",))
    collision = r3_only(text, names=("масло", "сливочное масло"))

    assert specific.status is AmountLinkStatus.UNRESOLVED
    assert specific.ingredient_position is None
    assert collision.status is AmountLinkStatus.AMBIGUOUS
    assert collision.reason is AmountLinkReason.MULTIPLE_CANDIDATES
    assert collision.ingredient_position is None


@pytest.mark.parametrize("delimiter", [".", "!", "?", ";"])
def test_s1_h2_an_adjacent_delimiter_without_whitespace_also_blocks(delimiter):
    text = f"Сливочное{delimiter}Масло — 100 мл"

    specific = r3_only(text, names=("сливочное масло",))

    assert specific.status is AmountLinkStatus.UNRESOLVED
    assert specific.ingredient_position is None


def test_s1_h2_a_clause_verb_does_not_reopen_the_crossed_phrase():
    # The same borrowed modifier must not survive the L3 order either.
    amount = r3_only(
        "Сливочное. Масло добавьте 100 мл", names=("масло", "сливочное масло")
    )

    assert amount.status is AmountLinkStatus.AMBIGUOUS
    assert amount.reason is AmountLinkReason.MULTIPLE_CANDIDATES
    assert amount.ingredient_position is None


def test_s1_h2_a_permitted_comma_gap_still_joins_the_phrase():
    # A comma is a modifier-list separator inside one sentence, not a sentence
    # delimiter; the curated phrase grammar is unchanged.
    amount = r3_only("Сливочное, масло — 100 мл", names=("сливочное масло",))

    assert linked(amount)
    assert amount.ingredient_position == 0


H3A_REPRODUCTION = "Возьмите половину, лишь 500 мл воды, молоко перемешайте"


def test_s1_h3a_a_later_unrelated_predicate_does_not_release_governance():
    # Codex reproduction: `перемешайте` belongs to the later milk clause and is not
    # evidence about the amount's own clause.
    amount = r3_only(H3A_REPRODUCTION, names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None


def test_s1_h3a_appending_an_unrelated_clause_never_creates_a_link():
    base = r3_only(
        "Возьмите половину, лишь 500 мл воды", names=("вода", "молоко")
    )
    extended = r3_only(H3A_REPRODUCTION, names=("вода", "молоко"))

    assert linked(base) is False
    assert linked(extended) is False
    assert base.status is AmountLinkStatus.UNRESOLVED
    assert extended.status is AmountLinkStatus.UNRESOLVED
    assert base.ingredient_position is None
    assert extended.ingredient_position is None


@pytest.mark.parametrize(
    "text",
    [
        "Возьмите половину, 500 мл воды",
        "Возьмите половину, лишь 500 мл воды",
        "Возьмите половину, 500 мл воды, молоко перемешайте",
        "Возьмите половину, 500 мл воды для теста",
    ],
)
def test_s1_h3a_uncertain_portion_governance_stays_unresolved(text):
    amount = r3_only(text, names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text",
    [
        "Отложите половину, 500 мл воды добавьте позже",
        "Отложите половину; 500 мл воды добавьте позже",
    ],
)
def test_s1_h3a_a_local_predicate_or_sentence_boundary_is_independent(text):
    amount = r3_only(text, names=("вода",))

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0


def test_s1_h3a_a_verified_boundary_does_not_propagate_governance():
    # The same text remains governed through a comma and becomes independent through a
    # semicolon: the boundary is the evidence, not the distance.
    governed = r3_only("Возьмите половину, 500 мл воды", names=("вода",))
    bounded = r3_only("Возьмите половину; 500 мл воды", names=("вода",))

    assert governed.status is AmountLinkStatus.UNRESOLVED
    assert governed.ingredient_position is None
    assert linked(bounded)
    assert bounded.ingredient_position == 0


def test_s1_h3b_a_curated_filler_inside_the_governing_chain_stays_anaphoric():
    # Codex reproduction: `точно` is a curated neutral filler and must behave the same
    # in non-comma and comma governance.
    amount = r3_only("Возьмите половину от точно 500 мл воды", names=("вода",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.ANAPHORA
    assert amount.ingredient_position is None


def test_s1_h3b_the_same_chain_without_the_filler_is_unchanged():
    amount = r3_only("Возьмите половину от 500 мл воды", names=("вода",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.ANAPHORA
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "filler",
    ["ровно", "лишь", "только", "еще", "и", "именно", "точно"],
)
def test_s1_h3b_adding_a_curated_filler_does_not_release_anaphora(filler):
    amount = r3_only(f"Возьмите половину {filler} 500 мл воды", names=("вода",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.ANAPHORA
    assert amount.ingredient_position is None


@pytest.mark.parametrize("filler", ["ровно", "точно"])
def test_s1_h3b_a_filler_without_a_portion_word_does_not_govern(filler):
    amount = r3_only(f"Возьмите {filler} 500 мл воды", names=("вода",))

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0


@pytest.mark.parametrize(
    "text",
    [
        "Возьмите половину от имеющихся 500 мл воды",
        "Возьмите половину от этих же указанных примерно 500 мл воды",
    ],
)
def test_s1_h3b_the_governing_chain_still_survives_quantifiers_and_particles(
    text,
):
    amount = r3_only(text, names=("вода",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.ANAPHORA
    assert amount.ingredient_position is None


def test_s1_m1_a_long_dependent_spoon_chain_completes_without_recursion():
    # Codex reproduction: the dependent-spoon chain used to add one Python frame per
    # term, so 500 terms (2,499 characters) raised RecursionError.
    text = " ".join(["1 чл"] * 500)

    result = scan_step(text, ["вода"])

    [amount] = result.amounts
    assert result.omitted == 0
    assert (amount.start, amount.end) == (0, len(text))
    assert amount.text == text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is AmountLinkReason.UNSUPPORTED_CONSTRUCT
    assert amount.ingredient_position is None


@pytest.mark.parametrize("count", [199, 500, 2000])
def test_s1_m1_the_dependent_chain_is_iterative_at_every_length(count):
    text = " ".join(["1 чл"] * count)

    result = scan_step(text, ["вода"])

    [amount] = result.amounts
    assert (amount.start, amount.end) == (0, len(text))
    assert result == scan_step(text, ["вода"])


def test_s1_m1_a_step_at_the_protocol_bound_has_one_bounded_span():
    text = " ".join(["1 чл"] * 199)
    assert len(text) <= 1000

    result = scan_step(text, ["вода"])

    [amount] = result.amounts
    assert amount.text == text
    assert amount.reason is AmountLinkReason.UNSUPPORTED_CONSTRUCT


@pytest.mark.parametrize(
    "text, reason",
    [
        ("1 ч. л. 2 ч. л.", AmountLinkReason.UNSUPPORTED_CONSTRUCT),
        ("1 ч. л. 2", AmountLinkReason.MALFORMED_EXPRESSION),
        ("1 ч. л. 2 ч. л. 3", AmountLinkReason.MALFORMED_EXPRESSION),
        ("1 ст. л. 2 ч. л. с горкой", AmountLinkReason.HEAPED),
    ],
)
def test_s1_m1_the_chain_keeps_one_rejected_span_and_the_deepest_reason(
    text, reason
):
    [amount] = scan_step(text, []).amounts

    assert amount.text == text
    assert amount.status is AmountLinkStatus.UNSUPPORTED
    assert amount.reason is reason
    assert amount.ingredient_position is None


# --- KH-F01-B1.1-B R3-S1 H3-A follow-up: contextual dash and colon boundaries --------

# A dash or a colon after the amount ends the amount's own clause. It is never a
# sentence boundary: the L2 separator before an amount and a dash inside an expression
# are untouched.
H3A_CLAUSE_SEPARATORS = [", ", " — ", " – ", " - ", ": "]


@pytest.mark.parametrize("separator", H3A_CLAUSE_SEPARATORS)
def test_s1_h3a2_an_appended_unrelated_clause_never_creates_a_link(separator):
    text = (
        f"Возьмите половину, лишь 500 мл воды{separator}молоко перемешайте"
    )

    amount = r3_only(text, names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None


@pytest.mark.parametrize("separator", H3A_CLAUSE_SEPARATORS)
def test_s1_h3a2_the_mutation_is_metamorphic_over_the_base_text(separator):
    base = r3_only(
        "Возьмите половину, лишь 500 мл воды", names=("вода", "молоко")
    )
    extended = r3_only(
        f"Возьмите половину, лишь 500 мл воды{separator}молоко перемешайте",
        names=("вода", "молоко"),
    )

    assert base.status is AmountLinkStatus.UNRESOLVED
    assert base.ingredient_position is None
    assert extended.status is base.status
    assert extended.ingredient_position is None


@pytest.mark.parametrize("separator", [" — ", ": ", " - "])
def test_s1_h3a2_a_predicate_in_another_clause_is_never_local_evidence(separator):
    # The appended clause need not name another ingredient for its predicate to be
    # foreign to the amount's clause.
    for appended in ("хорошо перемешайте", "молоко перемешайте и отложите"):
        amount = r3_only(
            f"Возьмите половину, 500 мл воды{separator}{appended}",
            names=("вода", "молоко"),
        )

        assert amount.status is AmountLinkStatus.UNRESOLVED
        assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
        assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text",
    [
        "Отложите половину, 500 мл воды добавьте позже",
        "Отложите половину, ровно 500 мл воды добавьте позже",
        "Отложите половину; 500 мл воды добавьте позже",
        "Возьмите половину, 500 мл воды добавьте позже",
    ],
)
def test_s1_h3a2_a_predicate_in_the_amounts_own_clause_still_links(text):
    amount = r3_only(text, names=("вода", "молоко"))

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0


@pytest.mark.parametrize(
    "text, reason",
    [
        ("Вода — 100 мл", AmountLinkReason.NAME_BEFORE_SEPARATOR),
        ("Вода: 100 мл", AmountLinkReason.NAME_BEFORE_SEPARATOR),
        ("Воду добавьте 500 мл", AmountLinkReason.NAME_BEFORE_COPULA),
    ],
)
def test_s1_h3a2_approved_l2_and_l3_controls_are_unchanged(text, reason):
    # A dash or a colon before the amount is an explicit L2 separator, not a clause
    # boundary: the clause-boundary search starts only after the amount.
    amount = r3_only(text, names=("вода",))

    assert linked(amount)
    assert amount.reason is reason
    assert amount.ingredient_position == 0


def test_s1_h3a2_a_dash_separator_is_not_reinterpreted_as_a_clause_boundary():
    result = r3_scan("Вода — 100 мл, молоко — 200 мл", names=("вода", "молоко"))

    assert [amount.reason for amount in result.amounts] == [
        AmountLinkReason.NAME_BEFORE_SEPARATOR,
        AmountLinkReason.NAME_BEFORE_SEPARATOR,
    ]
    assert [amount.ingredient_position for amount in result.amounts] == [0, 1]


def test_s1_h3a2_a_coordinated_group_is_not_split_by_the_clause_boundary():
    # The coordinator, not the punctuation, decides whether a second mention is part of
    # the amount's own group.
    amount = r3_only(
        "Возьмите половину, 500 мл воды и молока перемешайте",
        names=("вода", "молоко"),
    )

    assert amount.status is AmountLinkStatus.SHARED
    assert amount.reason is AmountLinkReason.SHARED_AMOUNT
    assert amount.ingredient_position is None


# --- KH-F01-B1.1-B R3-S1 MEDIUM follow-up: the lexical hyphen -------------------------

# An ASCII hyphen is part of a word only when the whole hyphenated unit is on the closed
# positive allowlist (`всё-таки`, `все-таки`, `по-прежнему`, `добавьте-ка`). Such a
# hyphen never ends the amount's own clause, so a local predicate after it is still
# local evidence. A hyphen with a blank on either side is the prose spelling of a clause
# dash and still ends the clause, and an unverified letter-joined compound
# (`воды-молоко`) does too: it may never promote a portion-governed amount to linked.
# H3-A protection is unchanged in every case.


def test_hyphen_a_letter_joined_hyphen_is_classified_as_lexical():
    # The source-level rule the clause-boundary search consults: only complete approved
    # units are word-internal; a fragment or an unlisted compound is not.
    for text in ["всё-таки", "все-таки", "по-прежнему", "добавьте-ка"]:
        hyphen = text.index("-")
        assert step_scan_module._is_lexical_hyphen(text, hyphen)

    for text in [
        "500 мл воды - молоко",
        "500 мл воды- молоко",
        "500 мл воды -молоко",
        "3-4",
        "- 500 мл",
        "а-б",
        "воды-молоко",
        "вода-молоко-сахар",
        "агар-агар",
        "лук-порей",
    ]:
        for hyphen in [index for index, char in enumerate(text) if char == "-"]:
            assert step_scan_module._is_lexical_hyphen(text, hyphen) is False


def test_hyphen_every_hyphen_of_a_unit_is_classified_consistently():
    # A multi-part unit is evaluated whole, so every hyphen it contains gets the same
    # classification: the approved word is word-internal at both of its joins, while the
    # unverified compound is a boundary at both.
    for text in ["всё-таки", "все-таки", "по-прежнему", "добавьте-ка"]:
        assert all(
            step_scan_module._is_lexical_hyphen(text, index)
            for index, char in enumerate(text)
            if char == "-"
        )
    for text in ["вода-молоко-сахар", "воды-молоко-сахар"]:
        assert not any(
            step_scan_module._is_lexical_hyphen(text, index)
            for index, char in enumerate(text)
            if char == "-"
        )


def test_hyphen_the_confirmed_unspaced_hyphen_join_stays_unresolved():
    # The Codex reproduction: the unknown compound `воды-молоко` must not let the
    # unrelated predicate `перемешайте` establish locality for the portion-governed
    # amount. The append must not promote it to linked.
    amount = r3_only(
        "Возьмите половину, лишь 500 мл воды-молоко перемешайте",
        names=("вода", "молоко"),
    )

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None
    assert amount.text == "500 мл"


UNVERIFIED_HYPHEN_JOINS = ["воды-молоко", "воды-молоко-сахар"]


@pytest.mark.parametrize("join", UNVERIFIED_HYPHEN_JOINS)
def test_hyphen_appending_an_unverified_hyphenated_join_never_promotes(join):
    # Metamorphic property: appending an unverified two-part or three-part hyphenated
    # ingredient clause to an unresolved portion-governed amount must leave the outcome
    # exactly as it was and must never promote the amount to linked.
    base = r3_only(
        "Возьмите половину, лишь 500 мл воды",
        names=("вода", "молоко", "сахар"),
    )
    appended = r3_only(
        f"Возьмите половину, лишь 500 мл воды{join} перемешайте",
        names=("вода", "молоко", "сахар"),
    )

    assert base.status is AmountLinkStatus.UNRESOLVED
    assert base.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert base.ingredient_position is None
    assert appended.status is base.status
    assert appended.reason is base.reason
    assert appended.ingredient_position is None
    assert (appended.start, appended.end, appended.text) == (
        base.start,
        base.end,
        base.text,
    )


@pytest.mark.parametrize(
    "text",
    [
        "Отложите половину, 500 мл воды всё-таки добавьте позже",
        "Отложите половину, 500 мл воды все-таки добавьте позже",
        "Отложите половину, 500 мл воды по-прежнему добавьте позже",
        "Отложите половину, 500 мл воды добавьте-ка позже",
    ],
)
def test_hyphen_the_approved_units_keep_the_local_predicate(text):
    # The approved allowlist, including both spellings of `всё-таки` and the
    # verb-particle unit `добавьте-ка`, leaves the local predicate positive.
    amount = r3_only(text, names=("вода",))

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0
    assert amount.text == "500 мл"


HYPHEN_ADVERBS = ["всё-таки", "все-таки", "по-прежнему"]


@pytest.mark.parametrize("adverb", HYPHEN_ADVERBS)
def test_hyphen_the_codex_reproduction_links_the_governed_name(adverb):
    # Codex reproduction: the lexical hyphen of the adverb made the clause look ended,
    # so the local predicate `добавьте` was discarded and the amount stayed unresolved.
    amount = r3_only(
        f"Отложите половину, 500 мл воды {adverb} добавьте позже", names=("вода",)
    )

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0
    assert amount.text == "500 мл"


@pytest.mark.parametrize(
    "text",
    [
        "Отложите половину, 500 мл воды всё-таки добавьте позже",
        "Отложите половину, 500 мл воды по-прежнему добавьте позже",
        "Отложите половину, 500 мл воды всё-таки добавьте-ка позже",
        "Отложите половину, 500 мл воды добавьте всё-таки позже",
        "Отложите половину, 500 мл воды добавьте по-прежнему позже",
    ],
)
def test_hyphen_a_lexical_hyphen_never_ends_the_amounts_own_clause(text):
    # A hyphenated word before or after the local predicate is ordinary prose, not a
    # clause boundary.
    amount = r3_only(text, names=("вода",))

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0


@pytest.mark.parametrize("separator", H3A_CLAUSE_SEPARATORS)
def test_hyphen_h3a_a_separator_still_ends_the_clause(separator):
    # The negative control of the same sentence: every real clause separator, including
    # the blank-separated ASCII hyphen, keeps `перемешайте` in the other clause.
    text = f"Возьмите половину, лишь 500 мл воды{separator}молоко перемешайте"
    amount = r3_only(text, names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None


@pytest.mark.parametrize("separator", H3A_CLAUSE_SEPARATORS)
def test_hyphen_h3a_appending_an_unrelated_clause_never_promotes_a_quantity(
    separator,
):
    # The governed form stays unresolved; adding a clause, separated by any of the
    # approved boundaries, never promotes it to `linked`.
    base = r3_only(
        "Возьмите половину, лишь 500 мл воды", names=("вода", "молоко")
    )
    appended = r3_only(
        f"Возьмите половину, лишь 500 мл воды{separator}молоко перемешайте",
        names=("вода", "молоко"),
    )

    assert base.status is AmountLinkStatus.UNRESOLVED
    assert appended.status is base.status
    assert appended.ingredient_position is None


def test_hyphen_a_comma_boundary_survives_a_following_hyphenated_word():
    # The comma, not the later lexical hyphen, ends the amount's clause.
    amount = r3_only(
        "Отложите половину, 500 мл воды, всё-таки добавьте позже", names=("вода",)
    )

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None


@pytest.mark.parametrize(
    "text",
    [
        "Отложите половину, 500 мл воды 3-4 раза перемешайте",
        "Отложите половину, 500 мл воды 1-2 раза перемешайте",
    ],
)
def test_hyphen_a_numeric_hyphen_is_not_reclassified(text):
    # A hyphen between two digit runs belongs to a numeric expression and is left
    # exactly as it was: it is never read as a lexical word-joining hyphen.
    amount = r3_only(text, names=("вода",))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None


# A plain adverb and a supported hyphenated adverb in the same slot. The hyphen alone
# must never change the ingredient-linking outcome.
HYPHEN_ADVERB_PAIRS = [
    ("уже", "всё-таки"),
    ("уже", "все-таки"),
    ("уже", "по-прежнему"),
]


@pytest.mark.parametrize("plain, hyphenated", HYPHEN_ADVERB_PAIRS)
def test_hyphen_metamorphic_replacement_before_the_predicate(plain, hyphenated):
    plain_amount = r3_only(
        f"Отложите половину, 500 мл воды {plain} добавьте позже", names=("вода",)
    )
    hyphen_amount = r3_only(
        f"Отложите половину, 500 мл воды {hyphenated} добавьте позже",
        names=("вода",),
    )

    assert linked(plain_amount)
    assert (hyphen_amount.status, hyphen_amount.reason) == (
        plain_amount.status,
        plain_amount.reason,
    )
    assert hyphen_amount.ingredient_position == plain_amount.ingredient_position == 0
    assert (hyphen_amount.start, hyphen_amount.end, hyphen_amount.text) == (
        plain_amount.start,
        plain_amount.end,
        plain_amount.text,
    )


@pytest.mark.parametrize("plain, hyphenated", HYPHEN_ADVERB_PAIRS)
def test_hyphen_metamorphic_replacement_after_the_predicate(plain, hyphenated):
    plain_amount = r3_only(
        f"Отложите половину, 500 мл воды добавьте {plain} позже", names=("вода",)
    )
    hyphen_amount = r3_only(
        f"Отложите половину, 500 мл воды добавьте {hyphenated} позже",
        names=("вода",),
    )

    assert linked(plain_amount)
    assert (hyphen_amount.status, hyphen_amount.reason) == (
        plain_amount.status,
        plain_amount.reason,
    )
    assert hyphen_amount.ingredient_position == plain_amount.ingredient_position == 0
    assert (hyphen_amount.start, hyphen_amount.end, hyphen_amount.text) == (
        plain_amount.start,
        plain_amount.end,
        plain_amount.text,
    )


@pytest.mark.parametrize("plain, hyphenated", HYPHEN_ADVERB_PAIRS)
def test_hyphen_metamorphic_replacement_never_creates_a_link(plain, hyphenated):
    # The mirror direction: with no local predicate at all, the hyphenated form must not
    # turn an unresolved amount into a link either.
    plain_amount = r3_only(
        f"Возьмите половину, лишь 500 мл воды {plain}", names=("вода", "молоко")
    )
    hyphen_amount = r3_only(
        f"Возьмите половину, лишь 500 мл воды {hyphenated}",
        names=("вода", "молоко"),
    )

    assert plain_amount.status is AmountLinkStatus.UNRESOLVED
    assert hyphen_amount.status is plain_amount.status
    assert hyphen_amount.reason is plain_amount.reason
    assert hyphen_amount.ingredient_position is None


@pytest.mark.parametrize(
    "text, reason",
    [
        ("Вода — 100 мл", AmountLinkReason.NAME_BEFORE_SEPARATOR),
        ("Вода: 100 мл", AmountLinkReason.NAME_BEFORE_SEPARATOR),
        ("Воду добавьте 500 мл", AmountLinkReason.NAME_BEFORE_COPULA),
        ("Возьмите 100 мл воды", AmountLinkReason.NAME_AFTER),
    ],
)
def test_hyphen_the_approved_constructions_are_preserved(text, reason):
    result = r3_scan(text, names=("вода", "молоко"))
    first = result.amounts[0]

    assert linked(first)
    assert first.reason is reason
    assert first.ingredient_position == 0


def test_hyphen_l4_and_shared_outcomes_are_preserved():
    independent = r3_scan(
        "Всыпьте 100 г муки, влейте 200 мл воды", names=("мука", "вода")
    )
    shared = r3_scan("Возьмите 500 мл воды и молока", names=("вода", "молоко"))

    assert [amount.ingredient_position for amount in independent.amounts] == [0, 1]
    assert [amount.reason for amount in independent.amounts] == [
        AmountLinkReason.NAME_AFTER,
        AmountLinkReason.NAME_AFTER,
    ]
    [shared_amount] = shared.amounts
    assert shared_amount.status is AmountLinkStatus.SHARED
    assert shared_amount.reason is AmountLinkReason.SHARED_AMOUNT
    assert shared_amount.ingredient_position is None


@pytest.mark.parametrize(
    "text",
    [
        "Отложите половину, 1 стакан (250 мл) воды всё-таки добавьте позже",
        "Отложите половину, 2 стакана (250 мл) воды всё-таки добавьте позже",
        "Отложите половину, 1 стакан (237½ мл) воды добавьте позже",
    ],
)
def test_hyphen_exact_capacities_and_spans_are_unchanged(text):
    amount = r3_only(text, names=("вода",))

    assert linked(amount)
    assert amount.ingredient_position == 0
    assert text[amount.start : amount.end] == amount.text
    assert amount.text.endswith(")")


# --- KH-F01-B1.1-B B1: predicate token integrity and uppercase Ё -----------------------

# A finite verb is local predicate evidence only when its complete lexical unit is valid.
# A verb fragment inside an unknown hyphenated unit -- including an approved unit
# extended by an unknown component -- is not evidence, so it can neither release a
# portion-governed amount by itself nor let a later predicate beyond the boundary its own
# unit creates release it retroactively. The same closed lexical-unit allowlist is used
# by the clause-boundary search and by the predicate index.

EMBEDDED_VERB_FRAGMENTS = [
    "добавьте-ка-молоко",
    "влейте-ка-молоко",
    "возьмите-ка-молоко",
    "налейте-ка-молоко",
    "положите-ка-молоко",
]


@pytest.mark.parametrize("fragment", EMBEDDED_VERB_FRAGMENTS)
def test_b1b1_an_embedded_verb_fragment_is_not_predicate_evidence(fragment):
    # The six Codex probes: the approved `-ка` unit extended by an unknown component. The
    # verb is still a recognised substring, but its complete unit is not approved.
    text = f"Возьмите половину, лишь 500 мл воды {fragment} перемешайте"
    amount = r3_only(text, names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None
    assert amount.text == "500 мл"
    assert (amount.start, amount.end) == (text.index("500"), text.index("500") + 6)


@pytest.mark.parametrize(
    "extension",
    [
        # A standalone verb with an unknown hyphenated suffix.
        "добавьте-молоко",
        # An approved verb-particle unit extended by an unknown component.
        "добавьте-ка-молоко",
        # An unknown hyphenated prefix in front of the verb.
        "быстро-добавьте",
        # An approved unit extended into an unknown unit that contains the verb.
        "всё-таки-добавьте",
    ],
)
def test_b1b1_an_unsupported_extension_never_releases_governance(extension):
    # Clause-boundary detection and predicate eligibility agree about the unit: the
    # nonlexical hyphen ends the amount's clause, and the verb inside it is not evidence.
    text = f"Возьмите половину, лишь 500 мл воды {extension} перемешайте"
    amount = r3_only(text, names=("вода", "молоко"))

    assert amount.status is AmountLinkStatus.UNRESOLVED
    assert amount.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert amount.ingredient_position is None
    assert amount.text == "500 мл"


def test_b1b1_the_unit_not_the_extra_predicate_decides_locality():
    # `добавьте-ка` is one complete approved unit and stays local evidence; appending an
    # unknown component turns the same verb into a fragment and the amount stays governed.
    resolved = r3_only(
        "Возьмите половину, лишь 500 мл воды добавьте-ка перемешайте",
        names=("вода", "молоко"),
    )
    fragmented = r3_only(
        "Возьмите половину, лишь 500 мл воды добавьте-ка-молоко перемешайте",
        names=("вода", "молоко"),
    )

    assert linked(resolved)
    assert resolved.reason is AmountLinkReason.NAME_AFTER
    assert resolved.ingredient_position == 0
    assert fragmented.status is AmountLinkStatus.UNRESOLVED
    assert fragmented.reason is AmountLinkReason.NO_LOCAL_BINDING
    assert fragmented.ingredient_position is None


@pytest.mark.parametrize("fragment", EMBEDDED_VERB_FRAGMENTS)
def test_b1b1_appending_an_extended_verb_never_promotes_the_amount(fragment):
    # Metamorphic property: the governed amount is unresolved before the extension and
    # must stay exactly as it was, with the same span, after it.
    base = r3_only(
        "Возьмите половину, лишь 500 мл воды", names=("вода", "молоко")
    )
    extended = r3_only(
        f"Возьмите половину, лишь 500 мл воды {fragment}",
        names=("вода", "молоко"),
    )

    assert base.status is AmountLinkStatus.UNRESOLVED
    assert extended.status is base.status
    assert extended.reason is base.reason
    assert extended.ingredient_position is None
    assert (extended.start, extended.end, extended.text) == (
        base.start,
        base.end,
        base.text,
    )


def test_b1b1_a_standalone_verb_is_still_predicate_evidence():
    # The negative controls above must not disable the ordinary standalone predicate.
    standalone = r3_only(
        "Отложите половину, 500 мл воды добавьте позже", names=("вода",)
    )
    uppercase = r3_only(
        "Отложите половину, 500 мл воды ДОБАВЬТЕ позже", names=("вода",)
    )

    assert linked(standalone)
    assert linked(uppercase)
    assert standalone.reason is AmountLinkReason.NAME_AFTER
    assert uppercase.reason is AmountLinkReason.NAME_AFTER
    assert uppercase.ingredient_position == standalone.ingredient_position == 0


@pytest.mark.parametrize(
    "unit",
    [
        "всё-таки",
        "все-таки",
        "ВСЁ-ТАКИ",
        "ВСЕ-ТАКИ",
        "Всё-таки",
        "по-прежнему",
        "ПО-ПРЕЖНЕМУ",
        "добавьте-ка",
        "ДОБАВЬТЕ-КА",
    ],
)
def test_b1b1_every_case_spelling_of_an_approved_unit_keeps_the_predicate(unit):
    # `_is_lexical_hyphen` lower-cases before the single-`е` replacement, so the uppercase
    # `Ё` spelling of an approved unit is the same unit as the lowercase one.
    text = f"Отложите половину, 500 мл воды {unit} добавьте позже"
    amount = r3_only(text, names=("вода",))

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0
    assert amount.text == "500 мл"


@pytest.mark.parametrize(
    "unit", ["добавьте-ка", "ДОБАВЬТЕ-КА", "ВЛЕЙТЕ-КА", "НАЛЕЙТЕ-КА"]
)
def test_b1b1_an_uppercase_verb_particle_unit_is_complete_evidence(unit):
    # The uppercase verb-particle unit is both a lexical hyphen (so it does not end the
    # clause) and a complete verb unit (so its verb is predicate evidence).
    amount = r3_only(
        f"Отложите половину, 500 мл воды {unit} позже", names=("вода",)
    )

    assert linked(amount)
    assert amount.reason is AmountLinkReason.NAME_AFTER
    assert amount.ingredient_position == 0


@pytest.mark.parametrize(
    "unit",
    [
        "всё-таки",
        "все-таки",
        "ВСЁ-ТАКИ",
        "ВСЕ-ТАКИ",
        "Всё-таки",
        "по-прежнему",
        "ПО-ПРЕЖНЕМУ",
        "добавьте-ка",
        "ДОБАВЬТЕ-КА",
    ],
)
def test_b1b1_every_case_spelling_of_an_approved_unit_is_lexical(unit):
    assert step_scan_module._is_lexical_hyphen(unit, unit.index("-")) is True


@pytest.mark.parametrize(
    "unit", ["ВОДЫ-МОЛОКО", "ВСЁ-ТАКИ-ТО", "всё-таки-то", "ДОБАВЬТЕ-КА-МОЛОКО"]
)
def test_b1b1_an_unapproved_uppercase_unit_is_still_a_boundary(unit):
    assert not any(
        step_scan_module._is_lexical_hyphen(unit, index)
        for index, character in enumerate(unit)
        if character == "-"
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        # A standalone finite verb is a complete unit whatever its case.
        ("добавьте", True),
        ("ДОБАВЬТЕ", True),
        # A complete approved verb-particle unit is one valid unit.
        ("добавьте-ка", True),
        ("ДОБАВЬТЕ-КА", True),
        # A fragment of an unknown unit is never evidence.
        ("добавьте-ка-молоко", False),
        ("добавьте-молоко", False),
        ("быстро-добавьте", False),
        ("всё-таки-добавьте", False),
        ("добавьте-500", False),
    ],
)
def test_b1b1_a_verb_is_evidence_only_as_a_complete_lexical_unit(text, expected):
    # Source-level check of both sides: the extension may sit before or after the verb.
    _, words = step_scan_module._tokenize(text)
    verb = next(
        word
        for word in words
        if word.norm in step_scan_module._FINITE_VERBS
    )

    assert step_scan_module._is_predicate_word(text, verb) is expected
