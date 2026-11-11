"""The KH-F01-A deterministic measurement core: parsing, conversion and rounding.

Everything here is arithmetic on text, with no database, web, Telegram or provider in
the picture. The tests are grouped by contract: what a line is recognized as, what one
conversion returns, how the practical rounding gate is bounded, and how the core
coexists with the existing recipe protocol.
"""

import ast
import copy
import json
from fractions import Fraction
from pathlib import Path

import pytest

import house_helper.core.measures as measures
from house_helper.core.measures import (
    GLASS_SIZES_ML,
    MAX_ROUNDING_RELATIVE_ERROR,
    ConversionReason,
    ConversionStatus,
    Dimension,
    MeasurementBasis,
    QuantityPart,
    QuantityReason,
    QuantityStatus,
    RoundedAmount,
    Unit,
    convert_quantity,
    convert_text,
    parse_quantity,
    practical_round,
)
from house_helper.core.recipes import RECIPE_END, RECIPE_START, parse_reply


NBSP = chr(0x00A0)
NARROW_NBSP = chr(0x202F)


def parts_of(text):
    return parse_quantity(text).parts


# --- recognition: metric mass and volume ---------------------------------------------


@pytest.mark.parametrize(
    ("text", "amount", "unit", "dimension"),
    [
        ("40 г", 40, Unit.GRAM, Dimension.MASS),
        ("40г", 40, Unit.GRAM, Dimension.MASS),
        ("40 гр.", 40, Unit.GRAM, Dimension.MASS),
        ("40 грамм", 40, Unit.GRAM, Dimension.MASS),
        ("40 грамма", 40, Unit.GRAM, Dimension.MASS),
        ("40 граммов", 40, Unit.GRAM, Dimension.MASS),
        ("1 кг", 1, Unit.KILOGRAM, Dimension.MASS),
        ("1,5 кг", Fraction(3, 2), Unit.KILOGRAM, Dimension.MASS),
        ("2.5 кг", Fraction(5, 2), Unit.KILOGRAM, Dimension.MASS),
        ("1 килограмм", 1, Unit.KILOGRAM, Dimension.MASS),
        ("2 килограмма", 2, Unit.KILOGRAM, Dimension.MASS),
        ("3 килограммов", 3, Unit.KILOGRAM, Dimension.MASS),
        ("200 мл", 200, Unit.MILLILITER, Dimension.VOLUME),
        ("200мл", 200, Unit.MILLILITER, Dimension.VOLUME),
        ("250 миллилитров", 250, Unit.MILLILITER, Dimension.VOLUME),
        ("1 л", 1, Unit.LITER, Dimension.VOLUME),
        ("0,25 л", Fraction(1, 4), Unit.LITER, Dimension.VOLUME),
        ("1 литр", 1, Unit.LITER, Dimension.VOLUME),
        ("2 литра", 2, Unit.LITER, Dimension.VOLUME),
        ("3 литров", 3, Unit.LITER, Dimension.VOLUME),
    ],
)
def test_metric_mass_and_volume_amounts_are_recognized(text, amount, unit, dimension):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.QUANTIFIED
    assert quantity.reason is QuantityReason.OK
    assert quantity.dimension is dimension
    assert quantity.parts == (QuantityPart(Fraction(amount), unit),)
    assert quantity.text == text


# --- recognition: household measures -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "amount", "unit"),
    [
        ("1 ч. л.", 1, Unit.TEASPOON),
        ("2 ч.л.", 2, Unit.TEASPOON),
        ("3 ч л", 3, Unit.TEASPOON),
        ("4 ч. ложки", 4, Unit.TEASPOON),
        ("5 ч. ложек", 5, Unit.TEASPOON),
        ("1 чайная ложка", 1, Unit.TEASPOON),
        ("2 чайные ложки", 2, Unit.TEASPOON),
        ("3 чайных ложек", 3, Unit.TEASPOON),
        ("1 ст. л.", 1, Unit.TABLESPOON),
        ("2 ст.л.", 2, Unit.TABLESPOON),
        ("3 ст л", 3, Unit.TABLESPOON),
        ("4 ст. ложки", 4, Unit.TABLESPOON),
        ("5 ст. ложек", 5, Unit.TABLESPOON),
        ("1 столовая ложка", 1, Unit.TABLESPOON),
        ("2 столовые ложки", 2, Unit.TABLESPOON),
        ("3 столовых ложек", 3, Unit.TABLESPOON),
        ("1 стакан", 1, Unit.GLASS),
        ("2 стакана", 2, Unit.GLASS),
        ("3 стаканов", 3, Unit.GLASS),
    ],
)
def test_household_measure_spellings_are_recognized(text, amount, unit):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.QUANTIFIED
    assert quantity.dimension is Dimension.VOLUME
    assert quantity.parts == (QuantityPart(Fraction(amount), unit),)
    assert quantity.text == text


def test_a_glass_may_declare_its_capacity_in_the_text():
    assert parts_of("1 стакан 200 мл") == (
        QuantityPart(Fraction(1), Unit.GLASS, 200),
    )
    assert parts_of("1 стакан (250 мл)") == (
        QuantityPart(Fraction(1), Unit.GLASS, 250),
    )
    assert parts_of("1 стакан ( 200 мл )") == (
        QuantityPart(Fraction(1), Unit.GLASS, 200),
    )
    assert parts_of("1 стакан на 200 мл") == (
        QuantityPart(Fraction(1), Unit.GLASS, 200),
    )
    assert parts_of("1 стакан на (250 мл)") == (
        QuantityPart(Fraction(1), Unit.GLASS, 250),
    )
    assert parts_of("2 стакана 250 мл") == (
        QuantityPart(Fraction(2), Unit.GLASS, 250),
    )


@pytest.mark.parametrize(
    "text",
    [
        "1 стакан (200 мл",
        "1 стакан 200 мл)",
        "1 стакан (200 мл))",
        "1 стакан 200 мл(",
        "1 стакан ((200 мл))",
    ],
)
def test_an_unbalanced_glass_size_delimiter_is_not_guessed_away(text):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.UNRESOLVED
    assert quantity.reason is QuantityReason.UNKNOWN_UNIT
    assert quantity.parts == ()
    assert quantity.text == text


# --- recognition: fractions and written forms ----------------------------------------


@pytest.mark.parametrize(
    ("text", "amount", "unit"),
    [
        ("¼ ч. л.", Fraction(1, 4), Unit.TEASPOON),
        ("½ стакана", Fraction(1, 2), Unit.GLASS),
        ("¾ ст. л.", Fraction(3, 4), Unit.TABLESPOON),
        ("⅓ стакана", Fraction(1, 3), Unit.GLASS),
        ("1½ ст. л.", Fraction(3, 2), Unit.TABLESPOON),
        ("2½ ч. л.", Fraction(5, 2), Unit.TEASPOON),
        ("1 ½ ч. л.", Fraction(3, 2), Unit.TEASPOON),
        ("1 1/2 стакана", Fraction(3, 2), Unit.GLASS),
        ("2/3 стакана", Fraction(2, 3), Unit.GLASS),
        ("3/4 кг", Fraction(3, 4), Unit.KILOGRAM),
    ],
)
def test_fractions_and_mixed_numbers_are_exact(text, amount, unit):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.QUANTIFIED
    assert quantity.parts == (QuantityPart(Fraction(amount), unit),)


@pytest.mark.parametrize(
    ("text", "amount", "unit"),
    [
        ("1,5 кг", Fraction(3, 2), Unit.KILOGRAM),
        ("0.5 л", Fraction(1, 2), Unit.LITER),
        ("0,125 кг", Fraction(1, 8), Unit.KILOGRAM),
        ("2,0 кг", Fraction(2), Unit.KILOGRAM),
    ],
)
def test_decimal_comma_and_point_are_read_as_exact_rationals(text, amount, unit):
    assert parts_of(text) == (QuantityPart(Fraction(amount), unit),)


# --- recognition: compounds ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "1 кг 200 г",
            ((Fraction(1), Unit.KILOGRAM), (Fraction(200), Unit.GRAM)),
        ),
        (
            "1 л 250 мл",
            ((Fraction(1), Unit.LITER), (Fraction(250), Unit.MILLILITER)),
        ),
        (
            "1 кг и 200 г",
            ((Fraction(1), Unit.KILOGRAM), (Fraction(200), Unit.GRAM)),
        ),
        (
            "1 ст. л. + 1 ч. л.",
            ((Fraction(1), Unit.TABLESPOON), (Fraction(1), Unit.TEASPOON)),
        ),
        (
            "1 стакан и 2 ст. л.",
            ((Fraction(1), Unit.GLASS), (Fraction(2), Unit.TABLESPOON)),
        ),
        (
            "2 ст. л. + 100 мл",
            ((Fraction(2), Unit.TABLESPOON), (Fraction(100), Unit.MILLILITER)),
        ),
    ],
)
def test_explicit_mixed_measurements_keep_every_term(text, expected):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.QUANTIFIED
    assert quantity.parts == tuple(
        QuantityPart(amount, unit) for amount, unit in expected
    )


# --- recognition: qualitative, ranged, unsupported, ambiguous, unresolved ------------


@pytest.mark.parametrize(
    "text",
    ["по вкусу", "По вкусу.", "щепотка", "щепотку", "щепоть", "немного", "на глаз"],
)
def test_qualitative_amounts_never_receive_a_number(text):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.QUALITATIVE
    assert quantity.reason is QuantityReason.QUALITATIVE
    assert quantity.parts == ()
    assert quantity.dimension is None
    assert quantity.text == text


@pytest.mark.parametrize(
    "text",
    [
        "2–3 ст. л.",
        "2-3",
        "2 — 3 стакана",
        "от 2 до 3 ч. л.",
        "1,5-2 кг",
    ],
)
def test_ranges_are_reported_without_inventing_a_midpoint(text):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.RANGE
    assert quantity.reason is QuantityReason.RANGE
    assert quantity.parts == ()
    assert quantity.text == text


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("2 шт.", QuantityReason.COUNTABLE),
        ("1 зубчик", QuantityReason.COUNTABLE),
        ("1 головка", QuantityReason.COUNTABLE),
        ("1 ломтик", QuantityReason.COUNTABLE),
        ("1 пачка", QuantityReason.PACKAGE),
        ("1 банка", QuantityReason.PACKAGE),
        ("1 упаковка", QuantityReason.PACKAGE),
        ("1 cup", QuantityReason.US_CUP),
        ("2 чашки", QuantityReason.US_CUP),
        ("1 ст. л. с горкой", QuantityReason.HEAPED),
        ("1 ч. л. с верхом", QuantityReason.HEAPED),
        ("1 стакан 300 мл", QuantityReason.INVALID_GLASS_SIZE),
    ],
)
def test_recognized_but_unsupported_measures_stay_unchanged(text, reason):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.UNSUPPORTED
    assert quantity.reason is reason
    assert quantity.parts == ()
    assert quantity.text == text


@pytest.mark.parametrize(
    "text",
    [
        "1 ст. л. с горкой2",
        "1 ст. л. с верхом2",
        "1 ч. л. с горкойx",
        "1 ст. л. с горкой,",
    ],
)
def test_a_malformed_heaped_qualifier_is_refused_without_raising(text):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.UNSUPPORTED
    assert quantity.reason is QuantityReason.HEAPED
    assert quantity.parts == ()
    assert quantity.text == text


@pytest.mark.parametrize(
    "text",
    ["1 ст. л. x", "1 ст. л. 2", "1 ст. л. !", "1 ст. л. без горки2"],
)
def test_unexpected_trailing_characters_refuse_the_whole_expression(text):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.UNRESOLVED
    assert quantity.reason is QuantityReason.UNKNOWN_UNIT
    assert quantity.parts == ()
    assert quantity.text == text


def test_a_malformed_qualifier_converts_to_an_unsuccessful_result():
    result = convert_text("1 ст. л. с горкой2", Unit.MILLILITER)

    assert result.status is ConversionStatus.UNSUPPORTED
    assert result.reason is ConversionReason.SOURCE_NOT_QUANTIFIED
    assert result.source.reason is QuantityReason.HEAPED
    assert result.exact_amount is None
    assert result.rounded is None
    assert result.is_success is False


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("1 кг + 200 мл", QuantityReason.MIXED_DIMENSIONS),
        ("2 ст. л. 1 ч. л.", QuantityReason.AMBIGUOUS_JUXTAPOSITION),
        ("1 стакан 2 ст. л.", QuantityReason.AMBIGUOUS_JUXTAPOSITION),
    ],
)
def test_ambiguous_compounds_are_reported_as_such(text, reason):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.AMBIGUOUS
    assert quantity.reason is reason
    assert quantity.parts == ()


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", QuantityReason.NO_QUANTITY),
        ("   ", QuantityReason.NO_QUANTITY),
        ("немножко", QuantityReason.NO_QUANTITY),
        ("соль", QuantityReason.NO_QUANTITY),
        ("-5 г", QuantityReason.NEGATIVE),
        ("−5 г", QuantityReason.NEGATIVE),
        ("1/0 г", QuantityReason.INVALID_NUMBER),
        ("2 кубика", QuantityReason.UNKNOWN_UNIT),
        ("1 кг, 200 г", QuantityReason.UNKNOWN_UNIT),
        ("1 ст. л. и", QuantityReason.UNKNOWN_UNIT),
        ("+ 1 г", QuantityReason.UNKNOWN_UNIT),
        ("2 ст. л. или 3 ст. л.", QuantityReason.UNKNOWN_UNIT),
    ],
)
def test_unresolved_lines_are_refused_with_a_reason(text, reason):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.UNRESOLVED
    assert quantity.reason is reason
    assert quantity.parts == ()
    assert quantity.text == text


def test_a_level_spoon_is_the_nominal_definition_and_is_accepted():
    quantity = parse_quantity("1 ст. л. без горки")

    assert quantity.status is QuantityStatus.QUANTIFIED
    assert quantity.parts == (QuantityPart(Fraction(1), Unit.TABLESPOON),)


def test_zero_is_a_valid_quantity_and_negative_is_not():
    zero = parse_quantity("0 г")
    negative = parse_quantity("-5 г")

    assert zero.status is QuantityStatus.QUANTIFIED
    assert zero.parts == (QuantityPart(Fraction(0), Unit.GRAM),)
    assert negative.status is QuantityStatus.UNRESOLVED


def test_parsing_keeps_the_original_text_byte_for_byte():
    text = "  2 СТ. Л.  "

    quantity = parse_quantity(text)

    assert quantity.text == text
    assert quantity.parts == (QuantityPart(Fraction(2), Unit.TABLESPOON),)


@pytest.mark.parametrize(
    ("text", "part"),
    [
        ("1" + NBSP + "кг", QuantityPart(Fraction(1), Unit.KILOGRAM)),
        (
            "2" + NARROW_NBSP + "ст." + NBSP + "л.",
            QuantityPart(Fraction(2), Unit.TABLESPOON),
        ),
        ("2  стакана", QuantityPart(Fraction(2), Unit.GLASS)),
        ("2 стакана.", QuantityPart(Fraction(2), Unit.GLASS)),
    ],
)
def test_ordinary_separators_and_sentence_punctuation_are_tolerated(text, part):
    assert parts_of(text) == (part,)


def test_is_quantified_reflects_the_status():
    assert parse_quantity("1 г").is_quantified is True
    assert parse_quantity("по вкусу").is_quantified is False


def test_parsing_refuses_a_non_string():
    with pytest.raises(TypeError):
        parse_quantity(40)


# --- conversion: exact metric --------------------------------------------------------


def test_grams_to_kilograms_is_exact():
    result = convert_text("500 г", Unit.KILOGRAM)

    assert result.status is ConversionStatus.EXACT
    assert result.reason is ConversionReason.CONVERTED
    assert result.basis is MeasurementBasis.DEFINED
    assert result.exact_amount == Fraction(1, 2)
    assert result.rounded == RoundedAmount(
        Fraction(1, 2), Unit.KILOGRAM, Fraction(0)
    )
    assert result.glass_sizes_ml == ()
    assert result.is_success is True


def test_kilograms_to_grams_is_exact_even_with_a_fraction():
    result = convert_text("1,5 кг", Unit.GRAM)

    assert result.status is ConversionStatus.EXACT
    assert result.basis is MeasurementBasis.DEFINED
    assert result.exact_amount == 1500


@pytest.mark.parametrize(
    ("text", "target", "expected"),
    [
        ("0,25 л", Unit.MILLILITER, Fraction(250)),
        ("250 мл", Unit.LITER, Fraction(1, 4)),
        ("2 л", Unit.MILLILITER, Fraction(2000)),
        ("1000 мл", Unit.LITER, Fraction(1)),
        ("0,1 кг", Unit.GRAM, Fraction(100)),
    ],
)
def test_metric_volume_and_mass_conversions_are_exact(text, target, expected):
    result = convert_text(text, target)

    assert result.status is ConversionStatus.EXACT
    assert result.reason is ConversionReason.CONVERTED
    assert result.exact_amount == expected


# --- conversion: nominal household volumes -------------------------------------------


@pytest.mark.parametrize(
    ("text", "target", "expected"),
    [
        ("2 ч. л.", Unit.MILLILITER, Fraction(10)),
        ("1 ст. л.", Unit.MILLILITER, Fraction(15)),
        ("1 ст. л.", Unit.TEASPOON, Fraction(3)),
        ("4 ч. л.", Unit.TABLESPOON, Fraction(4, 3)),
        ("1 стакан 200 мл", Unit.MILLILITER, Fraction(200)),
    ],
)
def test_household_conversions_are_nominal_and_arithmetically_exact(
    text, target, expected
):
    result = convert_text(text, target)

    assert result.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert result.reason is ConversionReason.CONVERTED
    assert result.basis is MeasurementBasis.NOMINAL_HOUSEHOLD
    assert result.exact_amount == expected


@pytest.mark.parametrize("glass_ml", GLASS_SIZES_ML)
def test_a_glass_uses_the_explicitly_selected_size(glass_ml):
    result = convert_text("1 стакан", Unit.MILLILITER, glass_ml=glass_ml)

    assert result.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert result.exact_amount == glass_ml
    assert result.glass_sizes_ml == (glass_ml,)


def test_a_glass_size_in_the_text_needs_no_caller_selection():
    result = convert_text("1 стакан 250 мл", Unit.MILLILITER)

    assert result.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert result.exact_amount == 250
    assert result.glass_sizes_ml == (250,)


def test_missing_glass_size_requires_clarification_instead_of_a_default():
    result = convert_text("1 стакан", Unit.MILLILITER)

    assert result.status is ConversionStatus.CLARIFICATION_REQUIRED
    assert result.reason is ConversionReason.GLASS_SIZE_REQUIRED
    assert result.exact_amount is None
    assert result.rounded is None
    assert result.is_success is False


def test_converting_into_glasses_also_requires_an_explicit_size():
    result = convert_text("500 мл", Unit.GLASS)

    assert result.status is ConversionStatus.CLARIFICATION_REQUIRED
    assert result.reason is ConversionReason.GLASS_SIZE_REQUIRED


def test_converting_into_glasses_uses_the_selected_size():
    result = convert_text("500 мл", Unit.GLASS, glass_ml=250)

    assert result.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert result.exact_amount == 2
    assert result.glass_sizes_ml == (250,)


def test_an_invalid_glass_size_is_refused():
    result = convert_text("1 стакан", Unit.MILLILITER, glass_ml=300)

    assert result.status is ConversionStatus.CLARIFICATION_REQUIRED
    assert result.reason is ConversionReason.GLASS_SIZE_INVALID


def test_a_conflicting_glass_size_is_not_silently_resolved():
    result = convert_text("1 стакан 250 мл", Unit.MILLILITER, glass_ml=200)

    assert result.status is ConversionStatus.CLARIFICATION_REQUIRED
    assert result.reason is ConversionReason.GLASS_SIZE_CONFLICT


@pytest.mark.parametrize(
    "text",
    [
        "0 стаканов 200 мл + 1 стакан 250 мл",
        "1 стакан 250 мл + 0 стаканов 200 мл",
        "1 стакан 200 мл + 1 стакан 250 мл",
        "1 стакан 250 мл + 1 стакан 200 мл",
        "0 стаканов 250 мл + 0 стаканов 200 мл",
    ],
)
def test_a_matching_glass_declaration_cannot_conceal_a_conflicting_one(text):
    result = convert_text(text, Unit.MILLILITER, glass_ml=200)

    assert result.status is ConversionStatus.CLARIFICATION_REQUIRED
    assert result.reason is ConversionReason.GLASS_SIZE_CONFLICT
    assert result.exact_amount is None
    assert result.rounded is None
    assert result.is_success is False


def test_a_zero_valued_glass_still_declares_its_size():
    conflicting = convert_text(
        "0 стаканов 200 мл", Unit.MILLILITER, glass_ml=250
    )
    matching = convert_text(
        "0 стаканов 200 мл + 1 стакан 200 мл", Unit.MILLILITER, glass_ml=200
    )

    assert conflicting.status is ConversionStatus.CLARIFICATION_REQUIRED
    assert conflicting.reason is ConversionReason.GLASS_SIZE_CONFLICT
    assert matching.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert matching.exact_amount == 200
    assert matching.glass_sizes_ml == (200,)


@pytest.mark.parametrize("declared", GLASS_SIZES_ML)
def test_consistent_glass_declarations_convert_in_single_and_compound_expressions(
    declared,
):
    single = convert_text(
        f"1 стакан {declared} мл", Unit.MILLILITER, glass_ml=declared
    )
    compound = convert_text(
        f"2 стакана {declared} мл + 1 стакан {declared} мл",
        Unit.MILLILITER,
        glass_ml=declared,
    )

    assert single.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert single.exact_amount == declared
    assert single.glass_sizes_ml == (declared,)
    assert compound.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert compound.exact_amount == 3 * declared
    assert compound.glass_sizes_ml == (declared,)


def test_without_a_contextual_size_each_glass_keeps_its_own_declaration():
    result = convert_text("0 стаканов 200 мл + 1 стакан 250 мл", Unit.MILLILITER)

    assert result.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert result.exact_amount == 250
    assert result.glass_sizes_ml == (200, 250)


# --- conversion: refused dimensions and refused sources ------------------------------


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("500 г", Unit.MILLILITER),
        ("500 мл", Unit.GRAM),
        ("1 кг", Unit.LITER),
        ("1 стакан 200 мл", Unit.GRAM),
    ],
)
def test_mass_and_volume_are_never_converted_into_each_other(text, target):
    result = convert_text(text, target)

    assert result.status is ConversionStatus.UNSUPPORTED
    assert result.reason is ConversionReason.MASS_VOLUME_COEFFICIENT_REQUIRED
    assert result.exact_amount is None
    assert result.basis is None


@pytest.mark.parametrize(
    ("text", "target", "status"),
    [
        ("по вкусу", Unit.GRAM, ConversionStatus.UNRESOLVED),
        ("2–3 ст. л.", Unit.MILLILITER, ConversionStatus.AMBIGUOUS),
        ("1 кг + 200 мл", Unit.GRAM, ConversionStatus.AMBIGUOUS),
        ("2 шт.", Unit.GRAM, ConversionStatus.UNSUPPORTED),
        ("2 кубика", Unit.GRAM, ConversionStatus.UNRESOLVED),
        ("-5 г", Unit.GRAM, ConversionStatus.UNRESOLVED),
    ],
)
def test_an_unquantified_source_is_reported_through_the_conversion_contract(
    text, target, status
):
    result = convert_text(text, target)

    assert result.status is status
    assert result.reason is ConversionReason.SOURCE_NOT_QUANTIFIED
    assert result.exact_amount is None
    assert result.rounded is None


# --- conversion: compounds and extreme values ----------------------------------------


def test_metric_compounds_convert_exactly():
    result = convert_text("1 кг 200 г", Unit.GRAM)

    assert result.status is ConversionStatus.EXACT
    assert result.exact_amount == 1200


def test_spoon_compounds_convert_nominally():
    result = convert_text("1 ст. л. + 1 ч. л.", Unit.MILLILITER)

    assert result.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert result.exact_amount == 20


def test_a_compound_glass_converts_with_the_selected_size():
    result = convert_text("1 стакан и 2 ст. л.", Unit.MILLILITER, glass_ml=200)

    assert result.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert result.exact_amount == 230
    assert result.glass_sizes_ml == (200,)


def test_zero_converts_to_zero():
    result = convert_text("0 г", Unit.KILOGRAM)

    assert result.status is ConversionStatus.EXACT
    assert result.exact_amount == 0
    assert result.rounded == RoundedAmount(
        Fraction(0), Unit.KILOGRAM, Fraction(0)
    )


def test_a_very_small_quantity_has_no_reliable_practical_rounding():
    result = convert_text("0,0001 г", Unit.GRAM)

    assert result.status is ConversionStatus.EXACT
    assert result.exact_amount == Fraction(1, 10_000)
    assert result.rounded is None


def test_an_unusually_large_quantity_stays_exact():
    result = convert_text("1000000 кг", Unit.GRAM)

    assert result.status is ConversionStatus.EXACT
    assert result.exact_amount == 1_000_000_000
    assert result.rounded == RoundedAmount(
        Fraction(1_000_000_000), Unit.GRAM, Fraction(0)
    )


# A digit run no kitchen amount could need, and longer than the interpreter-wide
# integer-string conversion limit, so the raw caller input is untrusted text.
_OVERSIZED_DIGITS = "9" * 5000


@pytest.mark.parametrize(
    "text",
    [
        _OVERSIZED_DIGITS + " г",
        "0," + "1" * 5000 + " кг",
        "0." + "1" * 5000 + " кг",
        "1/" + _OVERSIZED_DIGITS + " г",
        "1 " + _OVERSIZED_DIGITS + "/2 г",
        "1 стакан " + _OVERSIZED_DIGITS + " мл",
    ],
)
def test_an_oversized_number_is_classified_without_raising(text):
    quantity = parse_quantity(text)

    assert quantity.status is QuantityStatus.UNRESOLVED
    assert quantity.reason is QuantityReason.INVALID_NUMBER
    assert quantity.parts == ()
    assert quantity.text == text


def test_an_oversized_number_converts_to_an_unsuccessful_result():
    result = convert_text(_OVERSIZED_DIGITS + " г", Unit.KILOGRAM)

    assert result.status is ConversionStatus.UNRESOLVED
    assert result.reason is ConversionReason.SOURCE_NOT_QUANTIFIED
    assert result.source.reason is QuantityReason.INVALID_NUMBER
    assert result.exact_amount is None
    assert result.is_success is False


def test_a_conversion_carries_one_rounding_of_the_unrounded_target():
    result = convert_text("2,6 ст. л.", Unit.TABLESPOON)

    assert result.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert result.exact_amount == Fraction(13, 5)
    assert result.rounded == RoundedAmount(
        Fraction(5, 2), Unit.TABLESPOON, Fraction(1, 26)
    )


def test_a_us_cup_is_never_interpreted_as_a_russian_glass():
    result = convert_text("1 cup", Unit.MILLILITER)

    assert result.status is ConversionStatus.UNSUPPORTED
    assert result.reason is ConversionReason.SOURCE_NOT_QUANTIFIED
    assert result.source.reason is QuantityReason.US_CUP
    assert result.source.parts == ()


def test_conversion_is_deterministic_and_repeatable():
    first = convert_text("1,5 кг", Unit.GRAM)
    second = convert_text("1,5 кг", Unit.GRAM)

    assert first == second
    assert first.exact_amount == 1500
    assert convert_text("1500 г", Unit.KILOGRAM).exact_amount == Fraction(3, 2)


def test_compound_terms_use_exact_rational_addition():
    result = convert_text("0,1 г + 0,2 г", Unit.GRAM)

    assert result.exact_amount == Fraction(3, 10)


def test_repeated_calculations_do_not_accumulate_rounding_error():
    total = sum(
        (convert_text("0,1 г", Unit.GRAM).exact_amount for _ in range(100)),
        Fraction(0),
    )

    assert total == 10


def test_conversion_refuses_wrong_argument_types():
    with pytest.raises(TypeError):
        convert_text("1 г", "gram")
    with pytest.raises(TypeError):
        convert_quantity(parse_quantity("1 г"), "gram")
    with pytest.raises(TypeError):
        convert_quantity("1 г", Unit.GRAM)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        convert_text(
            "1 стакан", Unit.MILLILITER, glass_ml=200.0  # type: ignore[arg-type]
        )


# --- rounding ------------------------------------------------------------------------


def test_an_exact_practical_measure_needs_no_rounding_error():
    assert practical_round(Fraction(5, 2), Unit.TEASPOON) == RoundedAmount(
        Fraction(5, 2), Unit.TEASPOON, Fraction(0)
    )


def test_a_half_spoon_is_chosen_when_a_whole_spoon_misses_the_gate():
    rounded = practical_round(Fraction(13, 5), Unit.TABLESPOON)  # 2.6 tbsp

    assert rounded == RoundedAmount(
        Fraction(5, 2), Unit.TABLESPOON, Fraction(1, 26)
    )


def test_a_quarter_spoon_is_the_last_practical_resort():
    rounded = practical_round(Fraction(13, 10), Unit.TEASPOON)  # 1.3 tsp

    assert rounded == RoundedAmount(
        Fraction(5, 4), Unit.TEASPOON, Fraction(1, 26)
    )


def test_a_whole_spoon_is_preferred_when_it_already_passes():
    rounded = practical_round(Fraction(21, 10), Unit.TABLESPOON)  # 2.1 tbsp

    assert rounded == RoundedAmount(
        Fraction(2), Unit.TABLESPOON, Fraction(1, 21)
    )


def test_no_practical_representation_is_returned_when_none_passes_the_gate():
    assert practical_round(Fraction(7, 5), Unit.TEASPOON) is None  # 1.4 tsp


def test_the_five_percent_boundary_is_inclusive():
    boundary = practical_round(Fraction(20, 19), Unit.GRAM)

    assert boundary == RoundedAmount(Fraction(1), Unit.GRAM, Fraction(1, 20))
    assert boundary.relative_error == MAX_ROUNDING_RELATIVE_ERROR


def test_just_past_the_five_percent_boundary_no_representation_is_returned():
    assert practical_round(Fraction(21, 19), Unit.GRAM) is None


def test_the_five_percent_boundary_is_exact_for_epsilon_adjacent_rationals():
    epsilon = Fraction(1, 1_000_000)
    # Around the candidate 1, the boundary value is reached from both sides.
    above = Fraction(20, 19)  # the value is exactly 5% above the candidate 1
    below = Fraction(20, 21)  # the value is exactly 5% below the candidate 1

    inside_above = practical_round(above - epsilon, Unit.GRAM)
    at_above = practical_round(above, Unit.GRAM)
    outside_above = practical_round(above + epsilon, Unit.GRAM)
    inside_below = practical_round(below + epsilon, Unit.GRAM)
    at_below = practical_round(below, Unit.GRAM)
    outside_below = practical_round(below - epsilon, Unit.GRAM)

    assert inside_above == RoundedAmount(
        Fraction(1), Unit.GRAM, (above - epsilon - 1) / (above - epsilon)
    )
    assert at_above == RoundedAmount(
        Fraction(1), Unit.GRAM, MAX_ROUNDING_RELATIVE_ERROR
    )
    assert outside_above is None
    assert inside_below == RoundedAmount(
        Fraction(1), Unit.GRAM, (1 - (below + epsilon)) / (below + epsilon)
    )
    assert at_below == RoundedAmount(
        Fraction(1), Unit.GRAM, MAX_ROUNDING_RELATIVE_ERROR
    )
    assert outside_below is None
    assert inside_above.relative_error < MAX_ROUNDING_RELATIVE_ERROR
    assert inside_below.relative_error < MAX_ROUNDING_RELATIVE_ERROR


def test_every_returned_rounding_is_within_the_gate():
    for numerator in range(1, 40):
        amount = Fraction(numerator, 10)
        for unit in Unit:
            rounded = practical_round(amount, unit)
            if rounded is not None:
                assert rounded.relative_error <= MAX_ROUNDING_RELATIVE_ERROR
                assert (rounded.amount - amount) / amount == (
                    rounded.relative_error
                    if rounded.amount >= amount
                    else -rounded.relative_error
                )


def test_zero_rounds_to_zero_and_negative_amounts_are_refused():
    assert practical_round(0, Unit.GRAM) == RoundedAmount(
        Fraction(0), Unit.GRAM, Fraction(0)
    )
    with pytest.raises(ValueError):
        practical_round(Fraction(-1), Unit.GRAM)


def test_rounding_refuses_wrong_argument_types():
    with pytest.raises(TypeError):
        practical_round(1.5, Unit.GRAM)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        practical_round(Fraction(1), "gram")  # type: ignore[arg-type]


# --- partial results and the recipe data contract ------------------------------------


def test_one_bad_amount_does_not_block_the_others():
    requests = [
        ("40 г", Unit.GRAM),
        ("1 стакан", Unit.MILLILITER),
        ("по вкусу", Unit.GRAM),
        ("2 шт.", Unit.GRAM),
    ]

    results = [convert_text(text, target) for text, target in requests]

    assert results[0].status is ConversionStatus.EXACT
    assert results[0].exact_amount == 40
    assert results[1].status is ConversionStatus.CLARIFICATION_REQUIRED
    assert results[2].status is ConversionStatus.UNRESOLVED
    assert results[3].status is ConversionStatus.UNSUPPORTED


def test_an_invalid_amount_in_the_middle_does_not_block_later_valid_amounts():
    texts = ["40 мл", "1 ст. л. с горкой2", "2 ст. л."]

    results = [convert_text(text, Unit.MILLILITER) for text in texts]

    assert results[0].status is ConversionStatus.EXACT
    assert results[0].exact_amount == 40
    assert results[1].status is ConversionStatus.UNSUPPORTED
    assert results[1].source.reason is QuantityReason.HEAPED
    assert results[2].status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert results[2].exact_amount == 30


def test_amounts_from_the_recipe_protocol_convert_without_touching_the_recipe():
    body = {
        "title": "Блины",
        "servings": "2 порции",
        "ingredients": [
            {"name": "мука", "amount": "1½ ст. л."},
            {"name": "соль", "amount": "по вкусу"},
            {"name": "яйца", "amount": "2 шт."},
        ],
        "steps": ["Смешайте ингредиенты."],
    }
    answer = f"{RECIPE_START}\n{json.dumps(body, ensure_ascii=False)}\n{RECIPE_END}"
    recipe = parse_reply(answer).recipes[0]
    snapshot = copy.deepcopy(recipe)

    assert snapshot is not recipe
    assert snapshot.ingredients is not recipe.ingredients

    flour = convert_text(recipe.ingredients[0].amount, Unit.MILLILITER)
    salt = convert_text(recipe.ingredients[1].amount, Unit.GRAM)
    eggs = convert_text(recipe.ingredients[2].amount, Unit.GRAM)

    assert flour.status is ConversionStatus.NOMINAL_HOUSEHOLD
    assert flour.exact_amount == Fraction(45, 2)
    assert salt.status is ConversionStatus.UNRESOLVED
    assert eggs.status is ConversionStatus.UNSUPPORTED
    assert recipe == snapshot
    assert recipe.ingredients[0].amount == "1½ ст. л."
    assert recipe.steps == snapshot.steps == ("Смешайте ингредиенты.",)
    assert recipe.recipe_id == snapshot.recipe_id


# --- boundaries ----------------------------------------------------------------------


def test_the_module_depends_only_on_the_standard_library():
    path = Path(measures.__file__)
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)

    assert modules == {"__future__", "re", "dataclasses", "enum", "fractions"}
