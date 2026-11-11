"""KH-F01-B1: the pure adapter between the accepted B0 core and the Web presentation.

Nothing here touches a database, a provider or a page. The tests pin the three owner
corrections and the arithmetic the core was accepted with:

  * metric volumes are offered in the person's glass, and only when the 5% practical
    rounding gate passes (otherwise no hint, never an invented approximation);
  * a household conversion with no practical rounded value is still presented exactly
    (the page shows "Точно: ..."), including a fractional source capacity;
  * a source-glass clarification applies only to undeclared glass terms, so a recipe
    mixing `1 стакан` with `1 стакан 240 мл` never produces an artificial conflict and
    neither declaration nor the original parsed quantity is changed.
"""

from fractions import Fraction

import pytest

from house_helper.core.measures import (
    ConversionReason,
    ConversionStatus,
    MeasureVolumes,
    Unit,
)
from house_helper.web import measure_targets
from house_helper.web.measure_targets import (
    PRESENTATION_EXACT,
    PRESENTATION_NONE,
    PRESENTATION_ROUNDED,
    SOURCE_HOUSEHOLD,
    SOURCE_METRIC_VOLUME,
    IngredientConversion,
    measure_block,
    volumes,
)

DEFAULT = volumes()


def convert(text, target=DEFAULT, **kwargs):
    return measure_targets.convert_ingredient(0, text, target, **kwargs)


# --- target context --------------------------------------------------------------------


def test_missing_personal_values_resolve_to_the_system_defaults():
    resolved = volumes()

    assert (resolved.glass_ml, resolved.teaspoon_ml, resolved.tablespoon_ml) == (250, 5, 15)


def test_a_personal_value_is_used_for_the_target_and_nothing_else():
    target = volumes(glass_ml=240, teaspoon_ml=4, tablespoon_ml=20)

    assert (target.glass_ml, target.teaspoon_ml, target.tablespoon_ml) == (240, 4, 20)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"glass_ml": 49},
        {"glass_ml": 1001},
        {"teaspoon_ml": 0},
        {"teaspoon_ml": 21},
        {"tablespoon_ml": 4},
        {"tablespoon_ml": 51},
        {"glass_ml": 240.0},
        {"glass_ml": True},
    ],
)
def test_an_out_of_range_or_non_exact_target_is_refused_not_clamped(kwargs):
    with pytest.raises((TypeError, ValueError)):
        volumes(**kwargs)


# --- household amounts -------------------------------------------------------------------


def test_a_declared_nonstandard_source_glass_is_used_exactly():
    conversion = convert("1 стакан 200 мл")

    assert conversion.status == ConversionStatus.NOMINAL_HOUSEHOLD.value
    assert conversion.reason == ConversionReason.CONVERTED.value
    assert conversion.source_kind == SOURCE_HOUSEHOLD
    assert conversion.target_unit is Unit.GLASS
    assert conversion.exact_amount == Fraction(4, 5)
    assert conversion.rounded is None  # no practical value passes the 5% gate
    assert conversion.presentation == PRESENTATION_EXACT
    assert conversion.identical is False


def test_a_fractional_source_capacity_is_kept_exactly():
    conversion = convert("1 стакан 237,5 мл")

    assert conversion.exact_amount == Fraction(19, 20)
    assert conversion.rounded is None
    assert conversion.presentation == PRESENTATION_EXACT


def test_a_source_already_in_the_same_unit_and_size_is_not_repeated():
    conversion = convert("2 ст. л.")

    assert conversion.exact_amount == Fraction(2)
    assert conversion.identical is True
    assert conversion.presentation == PRESENTATION_NONE


def test_a_personal_tablespoon_rescales_a_declared_source_spoon():
    conversion = convert("2 ст. л.", volumes(tablespoon_ml=20))

    assert conversion.exact_amount == Fraction(3, 2)
    assert conversion.rounded.amount == Fraction(3, 2)
    assert conversion.rounded.relative_error == 0
    assert conversion.presentation == PRESENTATION_ROUNDED
    assert conversion.identical is False


def test_a_personal_teaspoon_rescales_a_declared_source_spoon():
    conversion = convert("1 ч. л.", volumes(teaspoon_ml=4))

    assert conversion.exact_amount == Fraction(5, 4)
    assert conversion.presentation == PRESENTATION_ROUNDED
    assert conversion.rounded.amount == Fraction(5, 4)


def test_two_teaspoons_at_five_millilitres_in_a_six_millilitre_spoon():
    """The pinned arithmetic: exact 5/3, and 7/4 only because it passes the 5% gate."""
    conversion = convert("2 ч. л.", volumes(teaspoon_ml=6))

    assert conversion.exact_amount == Fraction(5, 3)
    assert conversion.exact_amount != Fraction(3, 2)  # not 1½
    assert conversion.rounded.amount == Fraction(7, 4)
    assert conversion.rounded.relative_error == Fraction(1, 20)
    assert conversion.rounded.relative_error <= Fraction(1, 20)  # the gate is inclusive
    assert conversion.presentation == PRESENTATION_ROUNDED


def test_a_mixed_household_quantity_uses_the_largest_present_unit():
    conversion = convert("1 ст. л. + 1 ч. л.")

    assert conversion.target_unit is Unit.TABLESPOON
    assert conversion.exact_amount == Fraction(4, 3)
    assert conversion.presentation == PRESENTATION_EXACT  # no quarter passes 5%


def test_a_glass_anywhere_makes_the_glass_the_target_of_a_mixture():
    conversion = convert("1 стакан 200 мл + 2 ст. л.")

    assert conversion.target_unit is Unit.GLASS
    assert conversion.exact_amount == Fraction(230, 250)  # 200 + 30 mL over the 250 mL glass


# --- metric volumes (owner correction 1) ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "personal", "exact", "rounded"),
    [
        ("500 мл", None, Fraction(2), Fraction(2)),
        ("750 мл", None, Fraction(3), Fraction(3)),
        ("1 л", None, Fraction(4), Fraction(4)),
        ("480 мл", 240, Fraction(2), Fraction(2)),
        ("1 л 200 мл", None, Fraction(24, 5), Fraction(5)),
    ],
)
def test_a_metric_volume_becomes_the_persons_glass_when_it_is_practically_usable(
    text, personal, exact, rounded
):
    target = DEFAULT if personal is None else volumes(glass_ml=personal)

    conversion = convert(text, target)

    assert conversion.source_kind == SOURCE_METRIC_VOLUME
    assert conversion.target_unit is Unit.GLASS
    assert conversion.exact_amount == exact
    assert conversion.rounded.amount == rounded
    assert conversion.presentation == PRESENTATION_ROUNDED
    assert conversion.identical is False


def test_a_metric_volume_with_no_practical_glass_value_is_not_approximated():
    conversion = convert("100 мл")

    assert conversion.exact_amount == Fraction(2, 5)
    assert conversion.rounded is None
    assert conversion.presentation == PRESENTATION_NONE  # the original metric amount stays


def test_metric_volumes_are_never_turned_into_spoons():
    conversion = convert("375 мл")

    assert conversion.target_unit is Unit.GLASS


# --- what B1 does not convert ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "300 г",
        "1 кг",
        "1 кг 200 г",
        "2 ст. л. с горкой",
        "1 стакан с горкой",
        "по вкусу",
        "немного",
        "2–3 ст. л.",
        "2 шт.",
        "пачка",
        "1 чашка",
        "1 стакан 30 мл",  # outside the approved source fence: unsupported, not guessed
    ],
)
def test_mass_unsupported_ambiguous_and_qualitative_amounts_are_not_presented(text):
    assert convert(text) is None


# --- source-glass clarification (owner correction 3) ---------------------------------------


def test_an_undeclared_source_glass_asks_for_a_clarification_and_is_never_defaulted():
    conversion = convert("1 стакан")

    assert conversion.status == ConversionStatus.CLARIFICATION_REQUIRED.value
    assert conversion.reason == ConversionReason.GLASS_SIZE_REQUIRED.value
    assert conversion.exact_amount is None
    assert conversion.presentation == PRESENTATION_NONE
    block = measure_block(["1 стакан"], DEFAULT)
    assert block["source_glass_required"] is True
    assert block["source_glass_ml"] is None


def test_a_clarification_resolves_the_undeclared_glass():
    conversion = convert("1 стакан", source_glass_ml=200)

    assert conversion.status == ConversionStatus.NOMINAL_HOUSEHOLD.value
    assert conversion.exact_amount == Fraction(4, 5)
    assert conversion.presentation == PRESENTATION_EXACT


def test_a_declared_capacity_is_not_overridden_by_a_clarification():
    conversion = convert("1 стакан 240 мл", source_glass_ml=200)

    assert conversion.status == ConversionStatus.NOMINAL_HOUSEHOLD.value
    assert conversion.reason == ConversionReason.CONVERTED.value  # no artificial conflict
    assert conversion.exact_amount == Fraction(24, 25)


def test_declared_and_undeclared_terms_in_one_recipe_keep_their_own_capacities():
    amounts = ["1 стакан", "1 стакан 240 мл"]

    block = measure_block(amounts, DEFAULT, source_glass_ml=200)

    first, second = block["ingredients"]
    assert first["status"] == ConversionStatus.NOMINAL_HOUSEHOLD.value
    assert first["exact"] == {"numerator": 4, "denominator": 5}  # A uses the chosen 200
    assert second["status"] == ConversionStatus.NOMINAL_HOUSEHOLD.value
    assert second["exact"] == {"numerator": 24, "denominator": 25}  # B keeps its own 240
    assert all(item["reason"] == ConversionReason.CONVERTED.value for item in block["ingredients"])
    assert block["source_glass_required"] is True  # A still contains an undeclared glass


def test_a_compound_amount_applies_the_clarification_only_to_its_undeclared_term():
    text = "1 стакан + 1 стакан 240 мл"

    conversion = convert(text, source_glass_ml=200)

    # 200 + 240 = 440 mL; the personal 250 mL glass holds 44/25 of it.
    assert conversion.exact_amount == Fraction(44, 25)
    # The parsed source is untouched: the undeclared term is still undeclared and the
    # declared one still says 240.
    assert [part.glass_ml for part in conversion.source_quantity.parts] == [None, 240]
    assert conversion.source_quantity.text == text


def test_the_original_recipe_text_and_quantity_are_never_changed():
    text = "1 стакан + 1 стакан 240 мл"

    conversion = convert(text, source_glass_ml=200)

    assert conversion.source == text
    assert conversion.source_quantity.text == text
    assert conversion.source_quantity.parts[1].glass_ml == 240


def test_a_clarification_outside_the_approved_fence_is_refused():
    for value in (49, 1001, 0, -1, 240.0, True):
        with pytest.raises((TypeError, ValueError)):
            convert("1 стакан", source_glass_ml=value)


def test_a_personal_target_glass_never_resolves_a_source_glass():
    # The target is the person's own 400 mL glass; the source still needs a clarification.
    conversion = convert("1 стакан", volumes(glass_ml=400))

    assert conversion.status == ConversionStatus.CLARIFICATION_REQUIRED.value
    assert conversion.exact_amount is None


def test_a_clarification_equal_to_the_target_glass_makes_the_conversion_identical():
    conversion = convert("1 стакан", volumes(glass_ml=250), source_glass_ml=250)

    assert conversion.exact_amount == Fraction(1)
    assert conversion.identical is True
    assert conversion.presentation == PRESENTATION_NONE


# --- the machine-readable block ---------------------------------------------------------------


def test_the_block_carries_exact_integers_and_never_a_float():
    block = measure_block(
        ["1 стакан 237,5 мл", "2 ч. л.", "300 г"],
        volumes(teaspoon_ml=6),
        source_glass_ml=None,
    )

    assert block["target"] == {"glass_ml": 250, "teaspoon_ml": 6, "tablespoon_ml": 15}
    assert block["source_glass_ml"] is None
    assert block["source_glass_required"] is False
    assert [item["position"] for item in block["ingredients"]] == [0, 1]  # mass is absent
    for item in block["ingredients"]:
        assert item["source_kind"] in {SOURCE_HOUSEHOLD, SOURCE_METRIC_VOLUME}
        assert item["presentation"] in {
            PRESENTATION_ROUNDED,
            PRESENTATION_EXACT,
            PRESENTATION_NONE,
        }
        for value in (item["exact"], item["rounded"]):
            if value is not None:
                assert isinstance(value["numerator"], int)
                assert isinstance(value["denominator"], int)
        if item["rounded"] is not None:
            error = item["rounded"]["relative_error"]
            assert isinstance(error["numerator"], int) and isinstance(error["denominator"], int)
    # 237.5 mL in a 250 mL glass is exact 19/20, with no rounded value.
    assert block["ingredients"][0]["exact"] == {"numerator": 19, "denominator": 20}
    assert block["ingredients"][0]["rounded"] is None
    # 2 tsp at 6 mL is exact 5/3 with a practical 7/4 (1/20 error).
    assert block["ingredients"][1]["exact"] == {"numerator": 5, "denominator": 3}
    assert block["ingredients"][1]["rounded"] == {
        "numerator": 7,
        "denominator": 4,
        "relative_error": {"numerator": 1, "denominator": 20},
    }


def test_the_block_marks_which_amounts_still_need_a_source_glass():
    block = measure_block(["1 стакан", "1 стакан 240 мл", "500 мл"], DEFAULT)

    assert block["source_glass_required"] is True
    assert [item["status"] for item in block["ingredients"]] == [
        ConversionStatus.CLARIFICATION_REQUIRED.value,
        ConversionStatus.NOMINAL_HOUSEHOLD.value,
        ConversionStatus.NOMINAL_HOUSEHOLD.value,
    ]


def test_the_adapter_returns_the_accepted_core_statuses_and_reasons_unchanged():
    conversion = convert("1 стакан")

    assert isinstance(conversion, IngredientConversion)
    assert conversion.status == ConversionStatus.CLARIFICATION_REQUIRED.value
    assert conversion.reason == ConversionReason.GLASS_SIZE_REQUIRED.value


def test_a_measure_volumes_target_is_required():
    with pytest.raises(TypeError):
        measure_targets.convert_ingredient(0, "500 мл", object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        measure_block(["500 мл"], object())  # type: ignore[arg-type]


def test_the_adapter_resolves_nothing_itself():
    # The target is used exactly as resolved by resolve_target_measures; the adapter never
    # consults the system defaults on its own (that is shown by the two cases above) and
    # never writes anything.
    conversion = convert("1 стакан 200 мл", MeasureVolumes(glass_ml=250))

    assert conversion.exact_amount == Fraction(4, 5)
