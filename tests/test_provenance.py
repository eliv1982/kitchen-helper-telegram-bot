"""KH-F01-B1.1-A: the source-measure provenance contract and its pure audit.

The audit reads the stored `amount` strings of a validated recipe and reports, per term,
whether the text declares the meaning of its measure. It is descriptive: a missing
declaration is reported, never repaired, and never makes the recipe invalid. These tests
pin the Stage A acceptance criteria (AC58-AC60, AC77, AC84) with the audit fixtures the
approved stage asks for: declared, undeclared, invalid, package and metric.

Two rules run through every case: the audit adds no arithmetic (exact values come from
`core.measures`), and it has no inheritance step (OD-03) -- one ingredient's declared
glass never becomes another term's capacity.
"""

import json
from fractions import Fraction

import pytest

from house_helper.core.measures import QuantityReason, Unit, parse_quantity
from house_helper.core.openai_chat import RECIPE_PROTOCOL_PROMPT
from house_helper.core.provenance import (
    REASON_CAPACITY_NOT_DECLARED,
    REASON_CAPACITY_OUT_OF_RANGE,
    REASON_CONTENT_NOT_DECLARED,
    REASON_COUNTABLE_CONSTRUCT_UNSUPPORTED,
    REASON_MEASURE_CONSTRUCT_UNSUPPORTED,
    CapacityOrigin,
    ProvenanceAuditStatus,
    ProvenanceKind,
    TermProvenanceStatus,
    audit_source_provenance,
)
from house_helper.core.recipes import (
    RECIPE_END,
    RECIPE_START,
    Ingredient,
    parse_reply,
    render_recipe,
    validate_recipe,
)


def audit(*amounts):
    """The audit of one recipe whose ingredients are the given amounts, in order."""
    return audit_source_provenance(
        [Ingredient(f"продукт {index}", amount) for index, amount in enumerate(amounts)]
    )


def only(result):
    [entry] = result.entries
    return entry


# --- AC58: the generation contract and its deterministic audit --------------------------


def test_the_generation_prompt_asks_for_an_explicit_source_capacity():
    rule = RECIPE_PROTOCOL_PROMPT

    assert "не задан самой единицей" in rule
    assert "1 стакан (200 мл)" in rule
    assert "1 банка (400 г)" in rule
    assert "5 и 15 мл" in rule  # the level spoons are a unit definition, not a declaration
    assert "не пиши «стакан» без объема" in rule
    assert "не придумывай его" in rule  # a user-transcribed recipe is never given a capacity


def test_the_generation_prompt_keeps_the_protocol_unchanged():
    # The content rule is not a schema change: the strict four-key protocol is intact.
    assert "с четырьмя ключами" in RECIPE_PROTOCOL_PROMPT
    assert '{"title": "Название", "servings": "4 порции", "ingredients": ' in (
        RECIPE_PROTOCOL_PROMPT
    )


def test_a_declared_glass_is_complete_and_keeps_its_exact_capacity():
    result = audit("1 стакан (200 мл)")

    entry = only(result)
    assert entry.kind is ProvenanceKind.GLASS
    assert entry.status is TermProvenanceStatus.DECLARED
    assert entry.capacity_ml == 200
    assert entry.declared_content is None
    assert entry.origin is CapacityOrigin.DECLARED_TEXT
    assert entry.reason is None
    assert result.status is ProvenanceAuditStatus.COMPLETE


def test_a_fractional_declared_capacity_is_kept_exactly():
    entry = only(audit("1 стакан (237,5 мл)"))

    assert entry.status is TermProvenanceStatus.DECLARED
    assert entry.capacity_ml == Fraction(475, 2)


def test_a_bare_declaration_is_read_too():
    entry = only(audit("1 стакан 200 мл"))

    assert entry.status is TermProvenanceStatus.DECLARED
    assert entry.capacity_ml == 200


def test_an_undeclared_glass_is_reported_not_repaired():
    result = audit("1 стакан")

    entry = only(result)
    assert entry.kind is ProvenanceKind.GLASS
    assert entry.status is TermProvenanceStatus.UNDECLARED
    assert entry.capacity_ml is None
    assert entry.origin is CapacityOrigin.UNRESOLVED
    assert entry.reason == REASON_CAPACITY_NOT_DECLARED
    assert result.status is ProvenanceAuditStatus.INCOMPLETE
    assert result.incomplete == (entry,)
    assert result.declared == ()


# --- AC59: a declaration is preserved by the protocol and its copy ----------------------


def _block(amount):
    return (
        f"{RECIPE_START}\n"
        + json.dumps(
            {
                "title": "Вода",
                "servings": "1 порция",
                "ingredients": [{"name": "Вода", "amount": amount}],
                "steps": ["Вскипятите воду."],
            },
            ensure_ascii=False,
        )
        + f"\n{RECIPE_END}"
    )


@pytest.mark.parametrize("amount", ["1 стакан (200 мл)", "1 банка (400 г)"])
def test_a_declaration_survives_the_protocol_parse_and_render_unchanged(amount):
    [recipe] = parse_reply(_block(amount)).recipes

    assert recipe.ingredients[0].amount == amount
    assert f"- Вода — {amount}" in render_recipe(recipe)


def test_a_declaration_survives_validation_by_the_same_rules():
    recipe = validate_recipe(
        0,
        "Вода",
        None,
        [{"name": "Вода", "amount": "1 стакан (200 мл)"}],
        ["Вскипятите воду."],
    )

    assert recipe.ingredients[0].amount == "1 стакан (200 мл)"


def test_the_protocol_still_rejects_an_extra_recipe_key():
    # The declaration lives in `amount`; no protocol key was added for it (AC49 stands).
    raw = json.loads(_block("1 стакан (200 мл)").split("\n")[1])
    raw["source_measures"] = {"glass_ml": 200}

    # Rebuild the block by hand: `_block` only writes the approved shape.
    text = (
        f"{RECIPE_START}\n{json.dumps(raw, ensure_ascii=False)}\n{RECIPE_END}"
    )
    parsed = parse_reply(text)

    assert parsed.recipes == ()
    assert parsed.has_recipes is False


# --- AC60: an existing undeclared glass receives no retroactive capacity ----------------


def test_a_legacy_undeclared_glass_stays_undeclared_and_the_text_is_untouched():
    amount = "1 стакан"
    quantity = parse_quantity(amount, allow_nonstandard_glass_sizes=True)
    entry = only(audit(amount))

    assert quantity.parts[0].glass_ml is None  # the parser invents nothing
    assert entry.status is TermProvenanceStatus.UNDECLARED
    assert entry.capacity_ml is None
    assert quantity.text == amount
    assert amount == "1 стакан"


# --- AC77: the audit is descriptive, deterministic and never invalidates a recipe -------


def test_the_audit_is_descriptive_and_idempotent():
    amounts = ["1 стакан", "500 мл", "1 пачка (400 г)", "1 cup молока"]

    first = audit(*amounts)
    second = audit(*amounts)

    assert first == second
    assert first.status is ProvenanceAuditStatus.INCOMPLETE


def test_an_incomplete_audit_never_rejects_the_recipe():
    recipe = validate_recipe(
        0,
        "Вода",
        None,
        [{"name": "Вода", "amount": "1 стакан"}],
        ["Вскипятите воду."],
    )

    assert recipe.ingredients[0].amount == "1 стакан"
    assert audit("1 стакан").status is ProvenanceAuditStatus.INCOMPLETE


# --- AC84: per-term statuses and origins, and no automatic inheritance ------------------


def test_two_declared_glass_terms_each_report_their_own_capacity():
    result = audit("1 стакан (200 мл) и 1 стакан (250 мл)")

    first, second = result.entries
    assert (first.term_index, second.term_index) == (0, 1)
    assert first.capacity_ml == 200
    assert second.capacity_ml == 250
    assert first.status is second.status is TermProvenanceStatus.DECLARED
    assert first.origin is second.origin is CapacityOrigin.DECLARED_TEXT
    assert result.status is ProvenanceAuditStatus.COMPLETE


def test_a_declared_term_sits_beside_an_undeclared_one_without_a_conflict():
    result = audit("1 стакан (200 мл) и 1 стакан")

    declared, undeclared = result.entries
    assert declared.status is TermProvenanceStatus.DECLARED
    assert declared.capacity_ml == 200
    assert undeclared.status is TermProvenanceStatus.UNDECLARED
    assert undeclared.capacity_ml is None
    assert undeclared.reason == REASON_CAPACITY_NOT_DECLARED


def test_no_capacity_is_inherited_from_another_ingredient():
    result = audit_source_provenance(
        [
            Ingredient("Вода", "1 стакан (200 мл)"),
            Ingredient("Молоко", "1 стакан"),
        ]
    )

    water, milk = result.entries
    assert water.status is TermProvenanceStatus.DECLARED
    assert water.capacity_ml == 200
    assert milk.status is TermProvenanceStatus.UNDECLARED
    assert milk.capacity_ml is None
    assert milk.origin is CapacityOrigin.UNRESOLVED


def test_the_audit_has_no_inherited_capacity_origin():
    assert "inherited_from_ingredient" not in {
        origin.value for origin in CapacityOrigin
    }


def test_entries_are_positioned_by_ingredient_and_term():
    result = audit_source_provenance(
        [
            Ingredient("Вода", "1 стакан (200 мл)"),
            Ingredient("Молоко", "500 мл"),
        ]
    )

    assert [(entry.ingredient_position, entry.term_index) for entry in result.entries] == [
        (0, 0),
        (1, 0),
    ]


# --- not applicable: no declaration is needed ------------------------------------------


def test_metric_amounts_need_no_declaration():
    result = audit("500 мл", "1,1 л")

    assert [entry.status for entry in result.entries] == [
        TermProvenanceStatus.NOT_APPLICABLE,
        TermProvenanceStatus.NOT_APPLICABLE,
    ]
    assert [entry.origin for entry in result.entries] == [
        CapacityOrigin.NOT_APPLICABLE,
        CapacityOrigin.NOT_APPLICABLE,
    ]
    assert result.status is ProvenanceAuditStatus.COMPLETE


def test_each_metric_term_of_a_compound_amount_is_reported():
    result = audit("1 кг 200 г")

    assert len(result.entries) == 2
    assert all(
        entry.status is TermProvenanceStatus.NOT_APPLICABLE for entry in result.entries
    )
    assert result.status is ProvenanceAuditStatus.COMPLETE


def test_level_spoons_need_no_declaration():
    result = audit("2 ст. л.", "1 ч. л.")

    assert all(
        entry.status is TermProvenanceStatus.NOT_APPLICABLE for entry in result.entries
    )
    assert result.status is ProvenanceAuditStatus.COMPLETE


def test_countable_pieces_need_no_declaration():
    result = audit("2 яйца", "3 зубчика", "2 шт.", "1 луковица")

    assert all(
        entry.status is TermProvenanceStatus.NOT_APPLICABLE for entry in result.entries
    )
    assert result.status is ProvenanceAuditStatus.COMPLETE


# --- KH-F01-B1.1-A: unknown units and positive countable recognition ------------------


@pytest.mark.parametrize("amount", ["1 кружка", "1 ведро", "1 тюбик", "1 oz", "7 унций"])
def test_an_unrecognized_unit_is_unsupported_not_excused(amount):
    # Following a number is not enough: an unknown noun is not made countable. The line is
    # reported, so the audit cannot be complete on a measure it cannot read.
    result = audit(amount)

    entry = only(result)
    assert entry.kind is ProvenanceKind.OTHER
    assert entry.status is TermProvenanceStatus.UNSUPPORTED_OTHER
    assert entry.capacity_ml is None
    assert entry.declared_content is None
    assert result.status is ProvenanceAuditStatus.INCOMPLETE


@pytest.mark.parametrize(
    "amount",
    [
        "2 яйца",
        "1 яйцо",
        "5 яиц",
        "1 луковица",
        "2 луковицы",
        "5 луковиц",
        "3 зубчика",
        "2 шт.",
        "1 штука",
        "1 головка",
        "2 ломтика",
        "1½ яйца",
        "1,5 яйца",
        "2 яйца и 1 луковица",
    ],
)
def test_a_curated_countable_expression_is_recognized_positively(amount):
    result = audit(amount)

    assert result.entries
    assert all(
        entry.kind is ProvenanceKind.OTHER
        and entry.status is TermProvenanceStatus.NOT_APPLICABLE
        for entry in result.entries
    )
    assert result.status is ProvenanceAuditStatus.COMPLETE


# --- KH-F01-B1.1-A R1: no number is not positive recognition --------------------------


NUMBERLESS_HOUSEHOLD_MEASURES = [
    "кружка",
    "ведро",
    "тюбик",
    "одна кружка",
    "по вкусу и кружка",
]


@pytest.mark.parametrize("amount", NUMBERLESS_HOUSEHOLD_MEASURES)
def test_a_numberless_household_measure_is_unsupported_not_excused(amount):
    # R1: a household measure the audit cannot read earns no declaration by carrying no
    # number. `одна кружка` is a word-number form, and `по вкусу и кружка` must be
    # rejected atomically: nothing is salvaged from the mixed line.
    result = audit(amount)

    entry = only(result)
    assert entry.kind is ProvenanceKind.OTHER
    assert entry.status is TermProvenanceStatus.UNSUPPORTED_OTHER
    assert entry.reason == REASON_MEASURE_CONSTRUCT_UNSUPPORTED
    assert entry.capacity_ml is None
    assert entry.declared_content is None
    assert result.declared == ()
    assert result.incomplete == (entry,)
    assert result.status is ProvenanceAuditStatus.INCOMPLETE


@pytest.mark.parametrize(
    "amount",
    [
        "соль",
        "соль и перец",
        "немножко",
        "полстакана",
        "полтора стакана",
        "две чашки",
        "ведро воды",
        "чашка",
        "бутылка",
        "ложка",
    ],
)
def test_an_unrecognized_numberless_amount_is_not_complete_by_default(amount):
    # The boundary matrix: an amount outside every positively recognized construction is
    # never complete merely because it contains no numeric token.
    result = audit(amount)

    entry = only(result)
    assert entry.status is TermProvenanceStatus.UNSUPPORTED_OTHER
    assert result.status is ProvenanceAuditStatus.INCOMPLETE


@pytest.mark.parametrize(
    "amount",
    [
        "по вкусу",
        "по вкусу.",
        "щепотка",
        "немного",
        "на глаз",
        "2 яйца",
        "1 луковица",
        "500 мл",
        "2 ст. л.",
        "1 стакан (200 мл)",
        "1 пачка (400 г)",
        "1 банка (0,5 л)",
    ],
)
def test_a_positively_recognized_standalone_construction_is_complete(amount):
    # R1 keeps every legitimate complete case: a curated qualitative phrase, a countable
    # expression, a metric or spoon term, a declared glass and a declared package.
    result = audit(amount)

    assert result.entries
    assert all(
        entry.status
        in (TermProvenanceStatus.NOT_APPLICABLE, TermProvenanceStatus.DECLARED)
        for entry in result.entries
    )
    assert result.incomplete == ()
    assert result.status is ProvenanceAuditStatus.COMPLETE


@pytest.mark.parametrize(
    "amount",
    ["по вкусу и кружка", "по вкусу и 1 кружка", "1 кружка и по вкусу"],
)
def test_a_mixed_qualitative_and_unreadable_measure_is_rejected_atomically(amount):
    result = audit(amount)

    entry = only(result)
    assert entry.status is TermProvenanceStatus.UNSUPPORTED_OTHER
    assert entry.reason != QuantityReason.QUALITATIVE.value
    assert result.declared == ()
    assert result.incomplete == (entry,)
    assert result.status is ProvenanceAuditStatus.INCOMPLETE


# --- KH-F01-B1.1-A: mixed expressions are classified atomically -----------------------


@pytest.mark.parametrize(
    "amount",
    [
        "1 стакан и 2 шт.",
        "1 стакан (200 мл) и 2 шт.",
        "стакан (мл)",
        "2 яйца и 1 стакан (200 мл)",
    ],
)
def test_a_mixed_or_malformed_measure_is_rejected_atomically(amount):
    # No declaration or quantity is salvaged from a rejected expression: one unsupported
    # entry, and the audit stays incomplete.
    result = audit(amount)

    entry = only(result)
    assert entry.status is TermProvenanceStatus.UNSUPPORTED_OTHER
    assert entry.capacity_ml is None
    assert entry.declared_content is None
    assert result.declared == ()
    assert result.incomplete == (entry,)
    assert result.status is ProvenanceAuditStatus.INCOMPLETE


def test_a_mixed_countable_construct_is_not_excused_by_the_parser_reason():
    # The parser's COUNTABLE reason is a whole-line keyword search; `1 стакан (200 мл) и
    # 2 шт.` is not a countable expression, and its declaration must not disappear.
    entry = only(audit("1 стакан (200 мл) и 2 шт."))

    assert entry.reason == REASON_COUNTABLE_CONSTRUCT_UNSUPPORTED


def test_a_numberless_measure_construct_is_not_excused_as_quantity_free():
    entry = only(audit("стакан (мл)"))

    assert entry.reason == REASON_MEASURE_CONSTRUCT_UNSUPPORTED


def test_a_qualitative_amount_needs_no_declaration():
    result = audit("по вкусу")

    assert only(result).status is TermProvenanceStatus.NOT_APPLICABLE
    assert result.status is ProvenanceAuditStatus.COMPLETE


# --- invalid and unsupported forms -----------------------------------------------------


def test_a_capacity_outside_the_approved_fence_is_invalid():
    result = audit("1 стакан (5000 мл)")

    entry = only(result)
    assert entry.kind is ProvenanceKind.GLASS
    assert entry.status is TermProvenanceStatus.INVALID_CAPACITY
    assert entry.capacity_ml is None
    assert entry.origin is CapacityOrigin.UNRESOLVED
    assert entry.reason == REASON_CAPACITY_OUT_OF_RANGE
    assert result.status is ProvenanceAuditStatus.INCOMPLETE


def test_a_us_cup_is_not_read_as_a_russian_glass():
    entry = only(audit("1 cup молока"))

    assert entry.kind is ProvenanceKind.CUP
    assert entry.status is TermProvenanceStatus.UNSUPPORTED_OTHER
    assert entry.capacity_ml is None


def test_a_range_is_rejected_as_a_whole():
    result = audit("2–3 ст. л. масла")

    entry = only(result)
    assert entry.status is TermProvenanceStatus.UNSUPPORTED_OTHER
    assert entry.capacity_ml is None
    assert result.status is ProvenanceAuditStatus.INCOMPLETE
    assert len(result.entries) == 1  # no midpoint and no salvaged sub-term


def test_a_heaped_spoon_is_unsupported():
    entry = only(audit("1 ст. л. с горкой"))

    assert entry.status is TermProvenanceStatus.UNSUPPORTED_OTHER


def test_a_line_that_names_a_measure_but_cannot_be_read_is_not_excused():
    # Trailing prose makes the whole-line parser refuse this amount. It is reported, not
    # treated as "nothing needed": an undeclared glass must not disappear this way.
    entry = only(audit("1 стакан молока"))

    assert entry.status is TermProvenanceStatus.UNSUPPORTED_OTHER
    assert entry.capacity_ml is None


# --- package content -------------------------------------------------------------------


def test_a_package_with_net_content_is_declared_in_its_own_unit():
    entry = only(audit("1 пачка (400 г)"))

    assert entry.kind is ProvenanceKind.PACKAGE
    assert entry.status is TermProvenanceStatus.DECLARED
    assert entry.capacity_ml is None  # no package-to-volume conversion
    assert entry.declared_content.amount == 400
    assert entry.declared_content.unit is Unit.GRAM
    assert entry.origin is CapacityOrigin.DECLARED_TEXT


def test_a_package_content_may_be_a_volume():
    entry = only(audit("1 банка (0,5 л)"))

    assert entry.status is TermProvenanceStatus.DECLARED
    assert entry.declared_content.amount == Fraction(1, 2)
    assert entry.declared_content.unit is Unit.LITER


def test_a_bare_package_content_is_read():
    entry = only(audit("1 пачка 400 г"))

    assert entry.status is TermProvenanceStatus.DECLARED
    assert entry.declared_content.amount == 400
    assert entry.declared_content.unit is Unit.GRAM


def test_a_package_without_content_is_incomplete():
    result = audit("2 пачки")

    entry = only(result)
    assert entry.kind is ProvenanceKind.PACKAGE
    assert entry.status is TermProvenanceStatus.PACKAGE_CONTENT_MISSING
    assert entry.declared_content is None
    assert entry.origin is CapacityOrigin.UNRESOLVED
    assert entry.reason == REASON_CONTENT_NOT_DECLARED
    assert result.status is ProvenanceAuditStatus.INCOMPLETE


def test_a_package_content_in_pieces_is_not_a_net_content():
    entry = only(audit("1 пачка (400 шт)"))

    assert entry.kind is ProvenanceKind.PACKAGE
    assert entry.status is TermProvenanceStatus.UNSUPPORTED_OTHER
    assert entry.declared_content is None


def test_several_packages_are_read_separately():
    result = audit("1 пачка (400 г) и 1 банка (500 мл)")

    assert [entry.term_index for entry in result.entries] == [0, 1]
    assert [entry.declared_content.unit for entry in result.entries] == [
        Unit.GRAM,
        Unit.MILLILITER,
    ]
    assert result.status is ProvenanceAuditStatus.COMPLETE


def test_a_bag_is_a_package():
    entry = only(audit("1 пакет (900 г)"))

    assert entry.kind is ProvenanceKind.PACKAGE
    assert entry.status is TermProvenanceStatus.DECLARED
    assert entry.declared_content.amount == 900


@pytest.mark.parametrize(
    "amount",
    ["1 пачка (400 г)", "1 банка (0,5 л)", "1 пакет 900 г"],
)
def test_a_package_never_gains_a_volume_capacity(amount):
    entry = only(audit(amount))

    assert entry.capacity_ml is None
    assert entry.declared_content is not None


# --- KH-F01-B1.1-A: every supported package spelling is detected -----------------------
#
# Detection and routing derive from one vocabulary, so every spelling below must reach the
# package recognizer. The four reproduced failures (`пачек`, `банок`, `баночек`,
# `упаковок`) are in this table; a spelling outside it is reported, never guessed at.

PACKAGE_FORMS = [
    "пачка",
    "пачки",
    "пачке",
    "пачку",
    "пачкой",
    "пачек",
    "пачкам",
    "пачками",
    "пачках",
    "банка",
    "банки",
    "банке",
    "банку",
    "банкой",
    "банок",
    "банкам",
    "банками",
    "банках",
    "баночка",
    "баночки",
    "баночке",
    "баночку",
    "баночкой",
    "баночек",
    "упаковка",
    "упаковки",
    "упаковке",
    "упаковку",
    "упаковкой",
    "упаковок",
    "пакет",
    "пакета",
    "пакету",
    "пакетом",
    "пакете",
    "пакеты",
    "пакетов",
    "пакетами",
    "пакетах",
]


@pytest.mark.parametrize("word", PACKAGE_FORMS)
def test_every_supported_package_spelling_without_content_is_incomplete(word):
    result = audit(f"5 {word}")

    entry = only(result)
    assert entry.kind is ProvenanceKind.PACKAGE
    assert entry.status is TermProvenanceStatus.PACKAGE_CONTENT_MISSING
    assert entry.declared_content is None  # no net content is invented
    assert entry.reason == REASON_CONTENT_NOT_DECLARED
    assert result.status is ProvenanceAuditStatus.INCOMPLETE


@pytest.mark.parametrize("word", PACKAGE_FORMS)
def test_every_supported_package_spelling_keeps_its_declared_content(word):
    result = audit(f"5 {word} (400 г)")

    entry = only(result)
    assert entry.kind is ProvenanceKind.PACKAGE
    assert entry.status is TermProvenanceStatus.DECLARED
    assert entry.declared_content.amount == 400
    assert entry.declared_content.unit is Unit.GRAM
    assert entry.capacity_ml is None  # weight is never turned into a volume
    assert result.status is ProvenanceAuditStatus.COMPLETE


# --- input shapes ----------------------------------------------------------------------


def test_the_audit_accepts_ingredients_mappings_and_amount_strings():
    as_ingredient = audit_source_provenance([Ingredient("Вода", "1 стакан (200 мл)")])
    as_mapping = audit_source_provenance([{"name": "Вода", "amount": "1 стакан (200 мл)"}])
    as_string = audit_source_provenance(["1 стакан (200 мл)"])

    assert as_ingredient == as_mapping == as_string


def test_a_value_that_is_not_an_ingredient_is_refused():
    with pytest.raises(TypeError):
        audit_source_provenance([42])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        audit_source_provenance("1 стакан")  # type: ignore[arg-type]
