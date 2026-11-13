"""KH-F01-B2-1: the deterministic ingredient reference registry and mass conversion.

The real registry ships no approved reference, so its tests assert identities, statuses
and reasons only -- never a coefficient. Every number here comes from a synthetic fixture
built in memory: fixture aliases say "test", fixture evidence says it is not food data,
and nothing in this file claims to be a real ingredient measurement.
"""

import ast
import hashlib
import os
import subprocess
import sys
from dataclasses import fields, replace
from fractions import Fraction
from pathlib import Path

import pytest

import house_helper.core.ingredient_reference as reference
from house_helper.core.ingredient_reference import (
    DEFAULT_REGISTRY,
    MAX_INTERVAL_RELATIVE_WIDTH,
    QUARTER_PIECE,
    REGISTRY_REVISION,
    US_CUP_ML,
    Alias,
    CountDisplay,
    Derivation,
    GenericWord,
    IngredientEntry,
    MassBasis,
    MassConversionReason,
    MassConversionStatus,
    MatchStatus,
    MeasurementEvidence,
    PieceIdentity,
    PieceReference,
    PieceState,
    RecordStatus,
    ReferenceKind,
    ReferenceRegistry,
    SourceRef,
    VolumeReference,
    canonical_key,
    convert_ingredient_mass,
    mass_grams,
    match_ingredient,
    name_tokens,
    normalize_name,
    registry_digest,
    validate_registry,
)
from house_helper.core.measures import MeasureVolumes, Unit, parse_quantity

APPROVED = RecordStatus.APPROVED
UNDER_REVIEW = RecordStatus.UNDER_REVIEW
YO = chr(0x0451)
DIAERESIS = chr(0x0308)

TEST_SOURCE = SourceRef(
    source_id="fixture.source",
    publisher="Synthetic test data",
    url="about:blank",
    accessed="2026-11-13",
    licence="none: test fixture",
)


# --- fixture construction -------------------------------------------------------------
#
# Every record below is synthetic. The quoted text says so, and no number is presented as
# a real ingredient measurement.


def _evidence(
    evidence_id,
    *,
    mass_g=120,
    volume_ml=240,
    normalization=None,
    method="not_stated",
    mass_basis=MassBasis.NOT_APPLICABLE,
):
    return MeasurementEvidence(
        evidence_id=evidence_id,
        source_id="fixture.source",
        quoted="SYNTHETIC TEST FIXTURE, not food reference evidence",
        original_amount=Fraction(1),
        original_unit="us_cup" if volume_ml is not None else "piece",
        original_volume_ml=None if volume_ml is None else Fraction(volume_ml),
        mass_g=Fraction(mass_g),
        mass_basis=mass_basis,
        method=method,
        conditions="test fixture only",
        limitations="synthetic; never a real coefficient",
        normalization=normalization,
    )


def build_registry(
    entries=(),
    *,
    volumes=(),
    pieces=(),
    piece_references=(),
    generic_words=(),
    sources=None,
    evidence=None,
):
    if evidence is None:
        # A fixture's auto-built evidence describes the same piece state as the piece
        # identity it backs, so the metadata is consistent unless a test says otherwise.
        piece_basis = {item.piece_id: item.mass_basis for item in pieces}
        rows = {}
        for volume in volumes:
            rows[volume.evidence_id] = _evidence(
                volume.evidence_id,
                mass_g=volume.grams_per_ml * 240,
                volume_ml=240,
            )
        for reference_row in piece_references:
            rows.setdefault(
                reference_row.evidence_id,
                _evidence(
                    reference_row.evidence_id,
                    mass_g=reference_row.weight_min_g,
                    volume_ml=None,
                    mass_basis=piece_basis.get(
                        reference_row.piece_id, MassBasis.AS_PURCHASED
                    ),
                ),
            )
        evidence = tuple(rows[key] for key in sorted(rows))
    return ReferenceRegistry(
        revision="test-fixture",
        sources=(TEST_SOURCE,) if sources is None else sources,
        evidence=evidence,
        ingredients=tuple(entries),
        generic_words=tuple(generic_words),
        pieces=tuple(pieces),
        volume_references=tuple(volumes),
        piece_references=tuple(piece_references),
    )


def make_entry(
    ingredient_id,
    *aliases,
    status=APPROVED,
    state=PieceState.UNSPECIFIED,
    basis=MassBasis.NOT_APPLICABLE,
    piece_id=None,
    accepted=(),
    required=(),
    forbidden=(),
):
    return IngredientEntry(
        ingredient_id=ingredient_id,
        display_name=ingredient_id,
        category="test",
        form="test fixture",
        state=state,
        mass_basis=basis,
        piece_id=piece_id,
        aliases=tuple(Alias(text, "test fixture alias") for text in aliases),
        accepted_qualifiers=tuple(accepted),
        qualifiers_required=tuple(required),
        qualifiers_forbidden=tuple(forbidden),
        status=status,
    )


def volume_reference(ingredient_id, grams_per_ml, *, evidence_id="fixture.volume", status=APPROVED):
    return VolumeReference(
        ingredient_id=ingredient_id,
        evidence_id=evidence_id,
        grams_per_ml=Fraction(grams_per_ml),
        derivation=Derivation.DIRECT_TARGET,
        status=status,
    )


def piece_reference(
    ingredient_id,
    piece_id,
    *,
    kind=ReferenceKind.POINT,
    size_class=None,
    weight_min_g=200,
    weight_max_g=None,
    default=False,
    label=None,
    evidence_id="fixture.piece",
    status=APPROVED,
):
    return PieceReference(
        ingredient_id=ingredient_id,
        piece_id=piece_id,
        evidence_id=evidence_id,
        reference_kind=kind,
        size_class=size_class,
        weight_min_g=Fraction(weight_min_g),
        weight_max_g=Fraction(weight_min_g if weight_max_g is None else weight_max_g),
        size_class_default=default,
        assumption_label_ru=label,
        status=status,
    )


def piece(piece_id, *, state=PieceState.RAW, basis=MassBasis.AS_PURCHASED, bone="none", skin="none"):
    return PieceIdentity(
        piece_id=piece_id,
        display_name=piece_id,
        piece_kind="test",
        state=state,
        bone=bone,
        skin=skin,
        mass_basis=basis,
        whole_or_edible="test fixture",
    )


def flour_registry(grams_per_ml, *, target_units=("glass", "tablespoon", "teaspoon")):
    entry = make_entry("test_flour", "тестовая мука")
    volume = VolumeReference(
        ingredient_id="test_flour",
        evidence_id="fixture.volume",
        grams_per_ml=Fraction(grams_per_ml),
        derivation=Derivation.SOURCE_CUP_NORMALIZED,
        status=APPROVED,
        applies_to=target_units,
    )
    return build_registry([entry], volumes=[volume])


def potato_registry(*, default=False):
    entry = make_entry(
        "test_root",
        "тестовый корень",
        state=PieceState.RAW,
        basis=MassBasis.AS_PURCHASED,
        piece_id="test_root",
        accepted=("basis:edible", "size:small", "size:medium"),
    )
    shape = piece("test_root", basis=MassBasis.EDIBLE_PORTION)
    references = [
        piece_reference(
            "test_root",
            "test_root",
            size_class="small",
            weight_min_g=170,
            default=False,
        ),
        piece_reference(
            "test_root",
            "test_root",
            size_class="medium",
            weight_min_g=213,
            default=default,
            label="если корни средние" if default else None,
        ),
    ]
    return build_registry([entry], pieces=[shape], piece_references=references)


# --- normalization -------------------------------------------------------------------


def test_a_name_is_lowercased_and_its_punctuation_becomes_spaces():
    assert normalize_name("  МУКА,  Пшеничная!  ") == "мука пшеничная"


def test_the_yo_letter_is_written_as_ye_in_the_single_and_decomposed_form():
    assert normalize_name("т" + YO + "плый") == "теплый"
    assert normalize_name("т" + "е" + DIAERESIS + "плый") == "теплый"


def test_a_hyphen_is_part_of_a_token_and_a_digit_is_kept():
    assert normalize_name("Какао-порошок") == "какао-порошок"
    assert normalize_name("сорт 1") == "сорт 1"


def test_word_order_is_not_part_of_the_key_but_is_kept_in_the_tokens():
    assert canonical_key("мука пшеничная") == canonical_key("пшеничная мука")
    assert name_tokens("пшеничная мука") == ("пшеничная", "мука")


def test_an_empty_name_has_no_tokens():
    assert name_tokens("   ") == ()
    assert canonical_key("") == ()


def test_a_non_string_name_is_refused():
    with pytest.raises(TypeError):
        normalize_name(None)


# --- matching: aliases, generic words, refusals --------------------------------------


def test_an_exact_alias_matches_its_entry():
    match = match_ingredient("мука пшеничная")

    assert match.status is MatchStatus.MATCHED
    assert match.entry.ingredient_id == "wheat_flour_all_purpose"


def test_both_word_orders_of_one_alias_are_the_same_key():
    first = match_ingredient("мука пшеничная")
    second = match_ingredient("пшеничная мука")

    assert first.status is second.status is MatchStatus.MATCHED
    assert first.entry.ingredient_id == second.entry.ingredient_id
    assert first.canonical_key == second.canonical_key


@pytest.mark.parametrize("name", ["мука", "крупа", "гречка", "соль", "овсянка", "овсяные хлопья"])
def test_a_generic_word_is_ambiguous_and_never_resolves(name):
    match = match_ingredient(name)

    assert match.status is MatchStatus.GENERIC
    assert match.entry is None
    assert len(match.entries) >= 2
    assert all(entry.ingredient_id for entry in match.entries)


@pytest.mark.parametrize(
    ("name", "published"),
    [("сахар", "sugar_granulated"), ("рис", "rice_white_long_grain_dry")],
)
def test_a_generic_word_keeps_its_meaning_with_one_known_product(name, published):
    # R1-07: generic-name classification does not depend on how complete the catalogue
    # currently is. One known product is still an ambiguity, never a silent answer.
    match = match_ingredient(name)

    assert match.status is MatchStatus.GENERIC
    assert match.entry is None
    assert [entry.ingredient_id for entry in match.entries] == [published]


def test_a_generic_word_with_no_known_product_is_still_generic():
    # A generic term whose products are all still uncatalogued keeps the same semantics:
    # it names an ambiguity and resolves to nothing.
    word = GenericWord("тестовый общий термин", ())
    registry = build_registry(
        [make_entry("test_only", "единственный продукт")], generic_words=[word]
    )

    match = match_ingredient("тестовый общий термин", registry=registry)

    assert match.status is MatchStatus.GENERIC
    assert match.entry is None
    assert match.entries == ()
    assert validate_registry(registry) == ()


def test_a_generic_word_with_an_unresolved_id_is_a_structural_problem():
    # Referential integrity survives the removal of the "two products" rule.
    registry = build_registry(
        [make_entry("test_only", "единственный продукт")],
        generic_words=[GenericWord("тестовый общий термин", ("test_only", "test_ghost"))],
    )

    assert any(
        "unknown ingredient test_ghost" in problem for problem in validate_registry(registry)
    )


def test_a_generic_word_never_resolves_through_a_size_qualifier():
    fine = match_ingredient("соль мелкая")
    coarse = match_ingredient("соль крупная")

    assert fine.status is coarse.status is MatchStatus.GENERIC
    assert fine.qualifiers == ("size:small",)
    assert coarse.qualifiers == ("size:large",)
    assert fine.entry is None and coarse.entry is None


def test_a_generic_word_never_resolves_through_a_state_qualifier():
    dry = match_ingredient("гречка сухая")
    cooked = match_ingredient("гречка вареная")

    assert dry.status is cooked.status is MatchStatus.GENERIC
    assert dry.qualifiers == ("state:dry",)
    assert cooked.qualifiers == ("state:cooked",)
    assert dry.entry is None and cooked.entry is None


def test_an_unknown_name_is_not_recognized():
    match = match_ingredient("сметана")

    assert match.status is MatchStatus.UNRECOGNIZED
    assert match.entry is None
    assert match.entries == ()


def test_the_bare_word_for_a_single_product_is_not_an_alias():
    # "грудка" could be a bone-in breast or a fillet; only the explicit fillet resolves.
    assert match_ingredient("грудка").status is MatchStatus.UNRECOGNIZED
    assert match_ingredient("филе грудки").entry.ingredient_id == "chicken_breast_fillet"


def test_a_rejected_product_is_kept_out_of_matching():
    assert match_ingredient("соль кошерная").status is MatchStatus.UNRECOGNIZED


# --- matching: the required distinctions ---------------------------------------------


def test_bone_in_and_boneless_are_different_entries_and_pieces():
    bone_in = match_ingredient("бедро на кости с кожей")
    boneless = match_ingredient("бедро без кости")

    assert bone_in.entry.ingredient_id != boneless.entry.ingredient_id
    assert bone_in.entry.piece_id != boneless.entry.piece_id


def test_skin_on_and_skinless_are_different_entries_and_pieces():
    skin_on = match_ingredient("бедро на кости с кожей")
    skinless = match_ingredient("бедро на кости без кожи")

    assert skin_on.entry.ingredient_id != skinless.entry.ingredient_id
    assert skin_on.entry.piece_id != skinless.entry.piece_id


def test_with_skin_and_without_skin_stay_distinguishable_when_both_are_generic():
    with_skin = match_ingredient("бедро с кожей")
    without_skin = match_ingredient("бедро без кожи")

    assert with_skin.status is without_skin.status is MatchStatus.GENERIC
    assert with_skin.qualifiers == ("skin:on",)
    assert without_skin.qualifiers == ("skin:off",)


def test_a_fillet_of_the_breast_and_a_fillet_of_the_thigh_are_different_products():
    breast = match_ingredient("филе грудки")
    thigh = match_ingredient("филе бедра")

    assert breast.entry.ingredient_id == "chicken_breast_fillet"
    assert thigh.entry.ingredient_id == "chicken_thigh_boneless"


def test_the_groats_and_the_flour_of_one_grain_are_different_entries():
    groats = match_ingredient("крупа гречневая")
    flour = match_ingredient("мука гречневая")

    assert groats.entry.ingredient_id != flour.entry.ingredient_id


def test_the_two_oat_products_stay_separate():
    thick = match_ingredient("геркулес")

    assert thick.entry.ingredient_id == "oats_rolled_thick"
    assert match_ingredient("овсяные хлопья").status is MatchStatus.GENERIC


def test_a_contradictory_name_is_reported_rather_than_resolved():
    match = match_ingredient("бедро без кожи с кожей")

    assert match.status is MatchStatus.QUALIFIER_CONFLICT
    assert set(match.conflicting_qualifiers) == {"skin:on", "skin:off"}


def test_a_contradictory_bone_name_is_reported():
    match = match_ingredient("бедро без кости на кости")

    assert match.status is MatchStatus.QUALIFIER_CONFLICT
    assert set(match.conflicting_qualifiers) == {"bone:in", "bone:out"}


def test_a_state_word_the_matched_entry_does_not_model_is_unsupported():
    match = match_ingredient("куриное филе вареное")

    assert match.status is MatchStatus.UNSUPPORTED_QUALIFIER
    assert match.entry.ingredient_id == "chicken_breast_fillet"
    assert match.qualifiers == ("state:cooked",)


def test_a_size_word_the_matched_entry_does_not_accept_is_unsupported():
    match = match_ingredient("мука пшеничная мелкая")

    assert match.status is MatchStatus.UNSUPPORTED_QUALIFIER
    assert match.qualifiers == ("size:small",)


def test_the_edible_basis_phrase_selects_the_same_product_with_a_stated_basis():
    whole = match_ingredient("картофель")
    peeled = match_ingredient("очищенный картофель")

    assert whole.entry.ingredient_id == peeled.entry.ingredient_id
    assert whole.qualifiers == ()
    assert peeled.qualifiers == ("basis:edible",)


# --- matching: ambiguity and qualifier-driven selection ------------------------------


def test_two_entries_that_match_one_name_are_ambiguous_without_a_tie_break():
    first = make_entry("test_a", "двойной продукт")
    second = make_entry("test_b", "двойной продукт")
    registry = build_registry([first, second])

    match = match_ingredient("двойной продукт", registry=registry)

    assert match.status is MatchStatus.AMBIGUOUS
    assert [entry.ingredient_id for entry in match.entries] == ["test_a", "test_b"]
    assert match.entry is None


def test_required_qualifiers_select_between_entries_sharing_one_alias():
    raw = make_entry(
        "test_raw", "тестовый продукт", state=PieceState.RAW, required=("state:raw",)
    )
    cooked = make_entry(
        "test_cooked", "тестовый продукт", state=PieceState.COOKED, required=("state:cooked",)
    )
    registry = build_registry([raw, cooked])

    assert match_ingredient("тестовый продукт сырой", registry=registry).entry.ingredient_id == "test_raw"
    assert (
        match_ingredient("тестовый продукт вареный", registry=registry).entry.ingredient_id
        == "test_cooked"
    )
    assert match_ingredient("тестовый продукт", registry=registry).status is MatchStatus.UNRECOGNIZED


def test_a_forbidden_qualifier_drops_an_entry():
    plain = make_entry("test_plain", "тестовый продукт", forbidden=("state:smoked",))
    registry = build_registry([plain])

    assert match_ingredient("тестовый продукт", registry=registry).status is MatchStatus.MATCHED
    assert (
        match_ingredient("тестовый продукт копченый", registry=registry).status
        is MatchStatus.UNRECOGNIZED
    )


# --- mass normalization --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("300 г", 300),
        ("0,3 кг", 300),
        ("1 кг", 1000),
        ("1 кг 200 г", 1200),
        ("1,5 кг", 1500),
    ],
)
def test_a_quantified_mass_is_summed_in_grams(text, expected):
    assert mass_grams(parse_quantity(text)) == expected


@pytest.mark.parametrize(
    "text",
    ["2-3 г", "по вкусу", "200 мл", "2 шт.", "1 пачка", "1 стакан", "с горкой"],
)
def test_an_amount_that_is_not_a_mass_yields_none(text):
    assert mass_grams(parse_quantity(text)) is None


def test_mass_grams_refuses_a_non_quantity():
    with pytest.raises(TypeError):
        mass_grams("300 г")


# --- mass to volume ------------------------------------------------------------------


def test_a_cup_derived_coefficient_converts_a_tablespoon_count():
    # B2E-01: 40 g at 1/2 g/mL is 80 mL, which is 16/3 tablespoons.
    registry = flour_registry(Fraction(1, 2))

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("40 г"), target=MeasureVolumes(), registry=registry
    )

    assert result.status is MassConversionStatus.REFERENCE_APPROXIMATE
    assert result.reason is MassConversionReason.CONVERTED
    conversion = result.conversion
    assert conversion.target_unit is Unit.TABLESPOON
    assert conversion.exact_count == Fraction(16, 3)
    assert conversion.rounded.amount == Fraction(11, 2)
    assert conversion.rounded.relative_error == Fraction(1, 32)


def test_a_direct_density_falls_through_to_the_quarter_step():
    # B2E-02: 40 g at 1 g/mL is 8/3 tablespoons; whole 3 and half 5/2 both miss the gate.
    registry = flour_registry(Fraction(1))

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("40 г"), target=MeasureVolumes(), registry=registry
    )

    conversion = result.conversion
    assert conversion.exact_count == Fraction(8, 3)
    assert conversion.rounded.amount == Fraction(11, 4)


def test_the_glass_is_used_when_its_rounded_count_reaches_one():
    # B2E-03: 198 g at 33/40 g/mL is 240 mL, 24/25 of a 250 mL glass.
    registry = flour_registry(Fraction(33, 40))

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("198 г"), target=MeasureVolumes(), registry=registry
    )

    conversion = result.conversion
    assert conversion.target_unit is Unit.GLASS
    assert conversion.exact_count == Fraction(24, 25)
    assert conversion.rounded.amount == 1
    assert conversion.rounded.relative_error == Fraction(1, 24)


def test_an_exact_household_count_has_no_rounding_error():
    # B2E-04: 18 g at 6/5 g/mL is exactly one tablespoon.
    registry = flour_registry(Fraction(6, 5))

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("18 г"), target=MeasureVolumes(), registry=registry
    )

    conversion = result.conversion
    assert conversion.target_unit is Unit.TABLESPOON
    assert conversion.exact_count == 1
    assert conversion.rounded.amount == 1
    assert conversion.rounded.relative_error == 0


def test_the_target_unit_preference_is_glass_then_tablespoon_then_teaspoon():
    registry = flour_registry(Fraction(1))

    glass = convert_ingredient_mass(
        "тестовая мука", parse_quantity("800 г"), target=MeasureVolumes(), registry=registry
    ).conversion
    spoon = convert_ingredient_mass(
        "тестовая мука", parse_quantity("60 г"), target=MeasureVolumes(), registry=registry
    ).conversion
    teaspoon = convert_ingredient_mass(
        "тестовая мука", parse_quantity("20 г"), target=MeasureVolumes(), registry=registry
    ).conversion

    assert glass.target_unit is Unit.GLASS
    assert spoon.target_unit is Unit.TABLESPOON
    assert teaspoon.target_unit is Unit.TEASPOON


def test_a_unit_the_reference_excludes_is_never_chosen():
    registry = flour_registry(Fraction(1), target_units=("tablespoon",))

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("400 г"), target=MeasureVolumes(), registry=registry
    ).conversion

    assert result.target_unit is Unit.TABLESPOON


def test_an_explicit_target_unit_is_honoured():
    registry = flour_registry(Fraction(33, 40), target_units=("glass",))

    conversion = reference._convert_mass_to_volume(
        Fraction(198),
        registry.volume_references[0],
        MeasureVolumes(),
        target_unit=Unit.GLASS,
        registry=registry,
    )

    assert conversion.exact_count == Fraction(24, 25)


def test_a_target_unit_the_reference_does_not_list_is_a_programming_error():
    registry = flour_registry(Fraction(1), target_units=("tablespoon",))

    with pytest.raises(ValueError):
        reference._convert_mass_to_volume(
            Fraction(40),
            registry.volume_references[0],
            MeasureVolumes(),
            target_unit=Unit.GLASS,
            registry=registry,
        )


def test_personal_capacities_are_used_and_the_personal_glass_is_not_fixed():
    reference_row = flour_registry(Fraction(1)).volume_references[0]

    large = reference._convert_mass_to_volume(
        Fraction(250),
        reference_row,
        MeasureVolumes(glass_ml=500),
        target_unit=Unit.GLASS,
        registry=flour_registry(Fraction(1)),
    )
    small = reference._convert_mass_to_volume(
        Fraction(250),
        reference_row,
        MeasureVolumes(glass_ml=200),
        target_unit=Unit.GLASS,
        registry=flour_registry(Fraction(1)),
    )

    assert large.exact_count == Fraction(1, 2)
    assert small.exact_count == Fraction(5, 4)


def test_a_missing_personal_capacity_resolves_to_the_system_default():
    registry = flour_registry(Fraction(1))

    resolved = convert_ingredient_mass(
        "тестовая мука", parse_quantity("400 г"), target=None, registry=registry
    )
    explicit = convert_ingredient_mass(
        "тестовая мука",
        parse_quantity("400 г"),
        target=MeasureVolumes(glass_ml=250, teaspoon_ml=5, tablespoon_ml=15),
        registry=registry,
    )

    assert resolved == explicit


def test_an_invalid_personal_capacity_is_refused_and_never_clamped():
    with pytest.raises(ValueError):
        MeasureVolumes(glass_ml=5)


def test_the_source_cup_normalization_travels_with_the_result():
    entry = make_entry("test_cup", "тестовая чашка")
    volume = VolumeReference(
        ingredient_id="test_cup",
        evidence_id="fixture.cup",
        grams_per_ml=Fraction(1, 2),
        derivation=Derivation.SOURCE_CUP_NORMALIZED,
        status=APPROVED,
    )
    registry = build_registry(
        [entry],
        volumes=[volume],
        evidence=(
            _evidence(
                "fixture.cup",
                mass_g=120,
                volume_ml=240,
                normalization="US cup = 240 mL (NIST)",
                method="not_stated",
            ),
        ),
    )

    conversion = convert_ingredient_mass(
        "тестовая чашка", parse_quantity("40 г"), target=MeasureVolumes(), registry=registry
    ).conversion

    assert conversion.derivation is Derivation.SOURCE_CUP_NORMALIZED
    assert conversion.normalization == "US cup = 240 mL (NIST)"
    assert conversion.method == "not_stated"


def test_a_count_with_no_practical_step_is_refused():
    # 9.5 g at 1 g/mL is 9.5 mL: 0.038 glass, 0.633 tablespoon, 1.9 teaspoons,
    # and no whole, half or quarter step is within 5% of 1.9.
    registry = flour_registry(Fraction(1))

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("9,5 г"), target=MeasureVolumes(), registry=registry
    )

    assert result.status is MassConversionStatus.UNSUPPORTED
    assert result.reason is MassConversionReason.ROUNDING_GATE_FAILED


def test_the_conversion_carries_the_original_amount_untouched():
    quantity = parse_quantity("40 г")
    snapshot = parse_quantity("40 г")

    convert_ingredient_mass(
        "тестовая мука", quantity, target=MeasureVolumes(), registry=flour_registry(Fraction(1))
    )

    assert quantity == snapshot
    assert quantity.text == "40 г"


# --- mass to count: points and intervals ---------------------------------------------


def point_registry(weight_g, *, mass_basis=MassBasis.AS_PURCHASED, state=PieceState.RAW):
    entry = make_entry(
        "test_piece",
        "тестовый кусок",
        state=state,
        basis=mass_basis,
        piece_id="test_piece",
    )
    shape = piece("test_piece", state=state, basis=mass_basis)
    reference_row = piece_reference("test_piece", "test_piece", weight_min_g=weight_g)
    return build_registry([entry], pieces=[shape], piece_references=[reference_row])


def interval_registry(low_g, high_g):
    entry = make_entry("test_piece", "тестовый кусок", piece_id="test_piece")
    shape = piece("test_piece")
    reference_row = piece_reference(
        "test_piece",
        "test_piece",
        kind=ReferenceKind.MEASURED_INTERVAL,
        weight_min_g=low_g,
        weight_max_g=high_g,
    )
    return build_registry([entry], pieces=[shape], piece_references=[reference_row])


def test_a_point_reference_yields_one_count_and_never_an_interval():
    # B2E-07: 600 g of a 213 g piece is 200/71, presented as 11/4 with no range.
    result = convert_ingredient_mass(
        "тестовый кусок", parse_quantity("600 г"), target=MeasureVolumes(), registry=point_registry(213)
    )

    conversion = result.conversion
    assert result.conditional is False
    assert conversion.reference_kind is ReferenceKind.POINT
    assert conversion.count_low == conversion.count_high == Fraction(200, 71)
    assert conversion.rounded_low == conversion.rounded_high == Fraction(11, 4)
    assert conversion.display is CountDisplay.SINGLE
    assert conversion.relative_width == 0


def test_a_point_reference_can_present_an_exact_half():
    # B2E-08: 300 g of a 200 g piece is exactly one and a half.
    result = convert_ingredient_mass(
        "тестовый кусок", parse_quantity("300 г"), target=MeasureVolumes(), registry=point_registry(200)
    )

    conversion = result.conversion
    assert conversion.count_low == Fraction(3, 2)
    assert conversion.rounded_low == Fraction(3, 2)
    assert conversion.display is CountDisplay.SINGLE


def test_a_measured_interval_produces_real_bounds():
    # B2E-09: 150-170 g pieces, 500 g.
    conversion = reference._convert_mass_to_count(
        Fraction(500),
        interval_registry(150, 170).piece_references[0],
    )

    assert conversion.status is MassConversionStatus.REFERENCE_APPROXIMATE
    assert conversion.count_low == Fraction(50, 17)
    assert conversion.count_high == Fraction(10, 3)
    assert conversion.rounded_low == 3
    assert conversion.rounded_high == Fraction(7, 2)
    assert conversion.display is CountDisplay.INTERVAL
    assert conversion.relative_width == Fraction(1, 8)


def test_a_second_interval_is_usable():
    # B2E-10: 110-130 g pieces, 1000 g.
    conversion = reference._convert_mass_to_count(
        Fraction(1000),
        interval_registry(110, 130).piece_references[0],
    )

    assert conversion.count_low == Fraction(100, 13)
    assert conversion.count_high == Fraction(100, 11)
    assert conversion.rounded_low == 8
    assert conversion.rounded_high == 9
    assert conversion.relative_width == Fraction(1, 6)
    assert conversion.display is CountDisplay.INTERVAL


@pytest.mark.parametrize(
    ("low_g", "high_g", "mass"),
    [(100, 200, 500), (80, 250, 1000)],
)
def test_a_broad_interval_is_refused(low_g, high_g, mass):
    conversion = reference._convert_mass_to_count(
        Fraction(mass),
        interval_registry(low_g, high_g).piece_references[0],
    )

    assert conversion.status is MassConversionStatus.UNSUPPORTED
    assert conversion.reason is MassConversionReason.INTERVAL_TOO_WIDE
    assert conversion.relative_width > MAX_INTERVAL_RELATIVE_WIDTH


def test_the_interval_width_bound_is_inclusive():
    # Weights in the ratio 9:7 make (high - low) / midpoint exactly 1/4.
    reference_row = interval_registry(140, 180).piece_references[0]

    assert reference._convert_mass_to_count(Fraction(1000), reference_row).relative_width == Fraction(1, 4)
    assert reference._convert_mass_to_count(Fraction(1000), reference_row).status is (
        MassConversionStatus.REFERENCE_APPROXIMATE
    )


def test_a_point_never_fabricates_an_uncertainty_interval():
    conversion = reference._convert_mass_to_count(
        Fraction(600), point_registry(213).piece_references[0]
    )

    assert conversion.count_low == conversion.count_high
    assert conversion.display is CountDisplay.SINGLE
    assert not (
        {field.name for field in fields(type(conversion))}
        & {"tolerance", "uncertainty", "plus_minus", "margin"}
    )


def test_a_quarter_piece_is_presented_against_a_200_gram_reference():
    # R1-08: 50 g against a 200 g point is exactly a quarter, and a quarter is presentable.
    conversion = reference._convert_mass_to_count(
        Fraction(50), point_registry(200).piece_references[0]
    )

    assert conversion.status is MassConversionStatus.REFERENCE_APPROXIMATE
    assert conversion.reason is MassConversionReason.CONVERTED
    assert conversion.count_low == Fraction(1, 4)
    assert conversion.rounded_low == Fraction(1, 4)


def test_a_quarter_piece_is_presented_against_a_201_gram_reference():
    # R1-08: the same quarter must not depend on whether the reference weighs 200 or
    # 201 g. There is no product-weight threshold anywhere in the presentation rule.
    conversion = reference._convert_mass_to_count(
        Fraction(50), point_registry(201).piece_references[0]
    )

    assert conversion.status is MassConversionStatus.REFERENCE_APPROXIMATE
    assert conversion.count_low == Fraction(50, 201)
    assert conversion.rounded_low == Fraction(1, 4)


def test_the_mathematical_count_is_kept_apart_from_its_presentation():
    # R1-08: the exact rational count is reported as it is; only the rounded field is a
    # grid value. A quarter of presentation never rewrites the mathematics.
    conversion = reference._convert_mass_to_count(
        Fraction(75), point_registry(300).piece_references[0]
    )

    assert conversion.count_low == Fraction(1, 4)
    assert conversion.rounded_low == Fraction(1, 4)

    inexact = reference._convert_mass_to_count(
        Fraction(220), point_registry(300).piece_references[0]
    )
    assert inexact.count_low == Fraction(11, 15)
    assert inexact.rounded_low == Fraction(3, 4)


def test_the_presentation_floor_is_a_quarter_piece_everywhere():
    # The grid bottoms out at a quarter for every reference, whatever its weight.
    assert QUARTER_PIECE == Fraction(1, 4)
    for weight in (20, 200, 201, 1000):
        conversion = reference._convert_mass_to_count(
            Fraction(weight, 4), point_registry(weight).piece_references[0]
        )
        assert conversion.rounded_low == QUARTER_PIECE, weight


def test_a_count_that_no_quarter_can_express_is_refused():
    # R1-08: below the grid there is no number at all -- never zero, never a smaller
    # fraction. 40 g of a 200 g piece is 0.2, which no grid step reaches within 5%.
    conversion = reference._convert_mass_to_count(
        Fraction(40), point_registry(200).piece_references[0]
    )

    assert conversion.status is MassConversionStatus.UNSUPPORTED
    assert conversion.reason is MassConversionReason.ROUNDING_GATE_FAILED


# --- count: size classes, conditional estimates and basis ----------------------------


def calibration_registry():
    entry = make_entry(
        "test_fillet",
        "тестовое филе",
        basis=MassBasis.AS_PURCHASED,
        piece_id="test_fillet",
    )
    shape = piece("test_fillet")
    references = [
        piece_reference(
            "test_fillet",
            "test_fillet",
            kind=ReferenceKind.CALIBRATION_CLASS,
            size_class="110-130",
            weight_min_g=110,
            weight_max_g=130,
        ),
        piece_reference(
            "test_fillet",
            "test_fillet",
            kind=ReferenceKind.CALIBRATION_CLASS,
            size_class="150-170",
            weight_min_g=150,
            weight_max_g=170,
        ),
        piece_reference(
            "test_fillet",
            "test_fillet",
            kind=ReferenceKind.CALIBRATION_CLASS,
            size_class="170-200",
            weight_min_g=170,
            weight_max_g=200,
        ),
    ]
    return build_registry([entry], pieces=[shape], piece_references=references)


def test_a_named_calibration_class_uses_its_own_bounds_only():
    result = convert_ingredient_mass(
        "тестовое филе",
        parse_quantity("500 г"),
        target=MeasureVolumes(),
        size_class="150-170",
        registry=calibration_registry(),
    )

    conversion = result.conversion
    assert conversion.reference_kind is ReferenceKind.CALIBRATION_CLASS
    assert conversion.size_class == "150-170"
    assert conversion.count_low == Fraction(50, 17)
    assert conversion.count_high == Fraction(10, 3)
    # Never the span of the neighbouring classes: that would be 2.5 .. 500/110.
    assert conversion.count_low > Fraction(5, 2)
    assert conversion.display is CountDisplay.INTERVAL


def test_calibration_classes_are_never_combined_without_a_named_class():
    result = convert_ingredient_mass(
        "тестовое филе", parse_quantity("500 г"), target=MeasureVolumes(),
        registry=calibration_registry(),
    )

    assert result.status is MassConversionStatus.CLARIFICATION_REQUIRED
    assert result.reason is MassConversionReason.SIZE_CLASS_REQUIRED


def test_a_named_class_selects_its_reference():
    registry = potato_registry()

    result = convert_ingredient_mass(
        "очищенный тестовый корень",
        parse_quantity("400 г"),
        target=MeasureVolumes(),
        size_class="small",
        registry=registry,
    )

    assert result.conversion.status is MassConversionStatus.REFERENCE_APPROXIMATE
    assert result.conversion.size_class == "small"
    assert result.conditional is False


def test_without_a_default_class_the_size_class_is_required():
    # B2E-16: class-specific records exist, no default and no class named.
    result = convert_ingredient_mass(
        "очищенный тестовый корень", parse_quantity("600 г"), target=MeasureVolumes(),
        registry=potato_registry(),
    )

    assert result.status is MassConversionStatus.CLARIFICATION_REQUIRED
    assert result.reason is MassConversionReason.SIZE_CLASS_REQUIRED


def test_a_default_class_produces_an_explicitly_labelled_conditional_estimate():
    # B2E-14.
    result = convert_ingredient_mass(
        "очищенный тестовый корень", parse_quantity("600 г"), target=MeasureVolumes(),
        registry=potato_registry(default=True),
    )

    assert result.status is MassConversionStatus.REFERENCE_APPROXIMATE
    assert result.conditional is True
    assert result.assumption.kind == "size_class"
    assert result.assumption.size_class == "medium"
    assert result.assumption.weight_g == 213
    assert result.assumption.mass_basis is MassBasis.EDIBLE_PORTION
    assert result.assumption.label_ru == "если корни средние"


def test_a_conditional_estimate_is_never_offered_across_a_basis_mismatch():
    # B2E-15: the name states no edible basis, the reference weight excludes peel.
    result = convert_ingredient_mass(
        "тестовый корень", parse_quantity("600 г"), target=MeasureVolumes(),
        registry=potato_registry(default=True),
    )

    assert result.status is MassConversionStatus.UNSUPPORTED
    assert result.reason is MassConversionReason.STATE_MISMATCH
    assert result.assumption is None


def test_an_edible_reference_is_never_used_for_an_as_purchased_name():
    # B2E-17: no yield factor exists.
    result = convert_ingredient_mass(
        "тестовый корень", parse_quantity("600 г"), target=MeasureVolumes(),
        registry=potato_registry(default=True),
    )

    assert result.reason is MassConversionReason.STATE_MISMATCH


def test_a_raw_name_and_a_cooked_reference_are_a_state_mismatch():
    # B2E-18: the entry is raw, the only reference was measured cooked.
    entry = make_entry(
        "test_piece", "тестовый кусок", state=PieceState.RAW, piece_id="test_piece"
    )
    shape = piece("test_piece", state=PieceState.COOKED)
    registry = build_registry(
        [entry], pieces=[shape], piece_references=[piece_reference("test_piece", "test_piece")]
    )

    result = convert_ingredient_mass(
        "тестовый кусок", parse_quantity("600 г"), target=MeasureVolumes(), registry=registry
    )

    assert result.status is MassConversionStatus.UNSUPPORTED
    assert result.reason is MassConversionReason.STATE_MISMATCH


def test_a_size_class_the_caller_names_must_exist_among_approved_records():
    result = convert_ingredient_mass(
        "очищенный тестовый корень", parse_quantity("600 г"), target=MeasureVolumes(),
        size_class="large", registry=potato_registry(default=True),
    )

    assert result.status is MassConversionStatus.CLARIFICATION_REQUIRED
    assert result.reason is MassConversionReason.SIZE_CLASS_REQUIRED


# --- orchestration: approval, eligibility and refusal mapping ------------------------


def test_a_mass_quantity_that_is_not_a_mass_emits_no_item():
    # B2E-27.
    for text in ("2-3 г", "по вкусу", "200 мл", "2 шт.", "1 пачка"):
        assert (
            convert_ingredient_mass(
                "тестовая мука", parse_quantity(text), registry=flour_registry(1)
            )
            is None
        )


def test_zero_mass_is_refused():
    # B2E-26.
    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("0 г"), registry=flour_registry(1)
    )

    assert result.status is MassConversionStatus.UNSUPPORTED
    assert result.reason is MassConversionReason.ZERO_MASS


def test_an_unapproved_reference_produces_no_number():
    # B2E-25.
    entry = make_entry("test_flour", "тестовая мука")
    registry = build_registry(
        [entry], volumes=[volume_reference("test_flour", 1, status=UNDER_REVIEW)]
    )

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("40 г"), target=MeasureVolumes(), registry=registry
    )

    assert result.status is MassConversionStatus.UNSUPPORTED
    assert result.reason is MassConversionReason.REFERENCE_NOT_APPROVED
    assert result.conversion is None


def test_an_unapproved_entry_produces_no_number():
    entry = make_entry("test_flour", "тестовая мука", status=UNDER_REVIEW)
    registry = build_registry([entry], volumes=[volume_reference("test_flour", 1)])

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("40 г"), target=MeasureVolumes(), registry=registry
    )

    assert result.reason is MassConversionReason.REFERENCE_NOT_APPROVED


def test_an_entry_without_any_reference_is_reported_as_missing():
    entry = make_entry("test_bare", "тестовый продукт")
    registry = build_registry([entry])

    result = convert_ingredient_mass(
        "тестовый продукт", parse_quantity("40 г"), target=MeasureVolumes(), registry=registry
    )

    assert result.reason is MassConversionReason.REFERENCE_MISSING


def test_two_approved_volume_references_are_a_registry_conflict():
    entry = make_entry("test_flour", "тестовая мука")
    registry = build_registry(
        [entry],
        volumes=[volume_reference("test_flour", 1), volume_reference("test_flour", 1)],
    )

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("40 г"), target=MeasureVolumes(), registry=registry
    )

    assert result.status is MassConversionStatus.AMBIGUOUS
    assert result.reason is MassConversionReason.REFERENCE_VARIANT_CONFLICT


@pytest.mark.parametrize(
    ("name", "status", "reason"),
    [
        ("мука", MassConversionStatus.AMBIGUOUS, MassConversionReason.GENERIC_NAME),
        ("сахар", MassConversionStatus.AMBIGUOUS, MassConversionReason.GENERIC_NAME),
        ("рис", MassConversionStatus.AMBIGUOUS, MassConversionReason.GENERIC_NAME),
        ("сметана", MassConversionStatus.UNSUPPORTED, MassConversionReason.INGREDIENT_NOT_RECOGNIZED),
        (
            "бедро без кожи с кожей",
            MassConversionStatus.AMBIGUOUS,
            MassConversionReason.QUALIFIER_CONFLICT,
        ),
        (
            "куриное филе вареное",
            MassConversionStatus.UNSUPPORTED,
            MassConversionReason.UNSUPPORTED_QUALIFIER,
        ),
    ],
)
def test_every_match_refusal_maps_to_a_closed_reason(name, status, reason):
    result = convert_ingredient_mass(
        name, parse_quantity("40 г"), target=MeasureVolumes(), registry=DEFAULT_REGISTRY
    )

    assert result.status is status
    assert result.reason is reason
    assert result.conversion is None


def test_the_real_registry_ships_no_approved_record_and_converts_nothing():
    assert all(entry.status is not APPROVED for entry in DEFAULT_REGISTRY.ingredients)
    assert all(
        reference_row.status is not APPROVED
        for reference_row in DEFAULT_REGISTRY.volume_references
    )
    assert all(
        reference_row.status is not APPROVED
        for reference_row in DEFAULT_REGISTRY.piece_references
    )

    result = convert_ingredient_mass(
        "мука пшеничная", parse_quantity("40 г"), target=MeasureVolumes()
    )

    assert result.reason is MassConversionReason.REFERENCE_NOT_APPROVED


def test_a_real_entry_without_reference_data_reports_it_missing():
    result = convert_ingredient_mass(
        "куриная грудка на кости", parse_quantity("600 г"), target=MeasureVolumes()
    )

    assert result.reason is MassConversionReason.REFERENCE_MISSING


# --- determinism, safety and boundaries ----------------------------------------------


def test_the_same_inputs_produce_equal_structures():
    # B2E-30.
    registry = potato_registry(default=True)
    first = convert_ingredient_mass(
        "очищенный тестовый корень", parse_quantity("600 г"), target=MeasureVolumes(),
        registry=registry,
    )
    second = convert_ingredient_mass(
        "очищенный тестовый корень", parse_quantity("600 г"), target=MeasureVolumes(),
        registry=registry,
    )

    assert first == second
    assert match_ingredient("тестовый корень", registry=registry) == match_ingredient(
        "тестовый корень", registry=registry
    )


def test_a_gram_amount_and_a_kilogram_amount_are_the_same_conversion():
    # B2E-05.
    registry = flour_registry(Fraction(1, 2))
    grams = convert_ingredient_mass(
        "тестовая мука", parse_quantity("300 г"), target=MeasureVolumes(), registry=registry
    )
    kilograms = convert_ingredient_mass(
        "тестовая мука", parse_quantity("0,3 кг"), target=MeasureVolumes(), registry=registry
    )

    assert grams.mass_g == kilograms.mass_g == 300
    assert grams == kilograms


def test_the_real_registry_is_structurally_coherent():
    # B2E-29.
    assert validate_registry(DEFAULT_REGISTRY) == ()


def test_the_real_registry_digest_is_pinned():
    # The canonical form is now typed field-delimited JSON (B2-1-R2); the digest changed
    # for that reason alone, not because a shipped measurement changed.
    assert registry_digest(DEFAULT_REGISTRY) == (
        "2333dbb6b5150522a6db753bf47a9fb48471a6d480f0b87ba9314c61025c9091"
    )
    assert DEFAULT_REGISTRY.revision == REGISTRY_REVISION


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda records: replace(
                records,
                generic_words=(GenericWord("мука", ("wheat_flour_all_purpose", "test_ghost")),),
            ),
            "unknown ingredient test_ghost",
        ),
        (
            lambda records: replace(
                records,
                ingredients=(make_entry("test_bad", "мука  пшеничная"),),
            ),
            "alias not normalized",
        ),
        (
            lambda records: replace(
                records,
                ingredients=(make_entry("test_bad", "тестовый продукт"),),
                generic_words=(GenericWord("тестовый продукт", ("test_bad", "test_other")),),
            ),
            "also an alias",
        ),
        (
            lambda records: replace(
                records,
                generic_words=(GenericWord("мука", ()), GenericWord("мука", ())),
            ),
            "duplicate generic word",
        ),
    ],
)
def test_structural_problems_are_reported(mutate, message):
    base = build_registry([make_entry("test_a", "тестовый продукт")])
    broken = mutate(base)

    problems = validate_registry(broken)

    assert any(message in problem for problem in problems), problems


def test_a_point_reference_with_a_range_is_a_structural_problem():
    entry = make_entry("test_piece", "тестовый кусок", piece_id="test_piece")
    registry = build_registry(
        [entry],
        pieces=[piece("test_piece")],
        piece_references=[
            piece_reference("test_piece", "test_piece", weight_min_g=100, weight_max_g=200)
        ],
    )

    assert any("point with a range" in problem for problem in validate_registry(registry))


def test_an_unresolved_evidence_reference_is_a_structural_problem():
    entry = make_entry("test_flour", "тестовая мука")
    registry = ReferenceRegistry(
        revision="test-fixture",
        sources=(TEST_SOURCE,),
        evidence=(),
        ingredients=(entry,),
        generic_words=(),
        pieces=(),
        volume_references=(volume_reference("test_flour", 1),),
        piece_references=(),
    )

    assert any("unresolved evidence" in problem for problem in validate_registry(registry))


def test_a_default_class_without_a_label_is_a_structural_problem():
    entry = make_entry("test_piece", "тестовый кусок", piece_id="test_piece")
    registry = build_registry(
        [entry],
        pieces=[piece("test_piece")],
        piece_references=[
            piece_reference("test_piece", "test_piece", size_class="medium", default=True)
        ],
    )

    assert any("default class without a label" in problem for problem in validate_registry(registry))


def test_the_registry_wording_and_fixtures_are_free_of_the_yo_letter():
    for entry in DEFAULT_REGISTRY.ingredients:
        assert YO not in entry.display_name
        for alias in entry.aliases:
            assert YO not in alias.text
            assert chr(0x0308) not in alias.text


def test_the_module_depends_only_on_the_standard_library_and_the_measurement_core():
    # B2E-31.
    modules = set()
    for node in ast.walk(ast.parse(Path(reference.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)

    assert modules == {
        "__future__",
        "hashlib",
        "json",
        "unicodedata",
        "dataclasses",
        "enum",
        "fractions",
        "house_helper.core.measures",
    }


def test_the_registry_is_immutable():
    with pytest.raises(Exception):
        DEFAULT_REGISTRY.revision = "changed"
    with pytest.raises(Exception):
        DEFAULT_REGISTRY.ingredients[0].display_name = "changed"


def test_a_source_cup_row_keeps_its_original_measure_and_its_normalized_volume():
    row = next(
        item
        for item in DEFAULT_REGISTRY.evidence
        if item.evidence_id == "kab.rice_long_grain_dry.2026-11-13"
    )
    reference_row = next(
        item for item in DEFAULT_REGISTRY.volume_references if item.evidence_id == row.evidence_id
    )

    assert row.original_amount == Fraction(1, 2)
    assert row.original_unit == "us_cup"
    assert row.original_volume_ml == Fraction(120)
    assert row.mass_g == 99
    assert row.quoted == "1/2 cup = 99 g"
    assert row.normalization is not None
    assert reference_row.grams_per_ml == Fraction(33, 40)


def test_the_source_cup_normalization_constant_is_the_one_the_owner_accepted():
    assert US_CUP_ML == 240
    for row in DEFAULT_REGISTRY.evidence:
        if row.original_unit == "us_cup":
            assert row.original_volume_ml == US_CUP_ML * row.original_amount


def test_a_non_positive_mass_or_density_is_a_programming_error():
    registry = flour_registry(1)
    with pytest.raises(ValueError):
        reference._convert_mass_to_volume(Fraction(0), registry.volume_references[0], MeasureVolumes())
    with pytest.raises(ValueError):
        reference._convert_mass_to_count(Fraction(0), point_registry(200).piece_references[0])
    with pytest.raises(TypeError):
        reference._convert_mass_to_volume("40", registry.volume_references[0], MeasureVolumes())


# --- R1-01: one supported conversion gateway, approval enforced -----------------------


def test_the_public_api_exposes_one_conversion_gateway():
    assert "convert_ingredient_mass" in reference.__all__
    for removed in ("convert_mass_to_volume", "convert_mass_to_count"):
        assert removed not in reference.__all__
        assert not hasattr(reference, removed), removed
    for removed in ("HALF_PIECE", "QUARTER_PIECE_MIN_WEIGHT_G"):
        assert removed not in reference.__all__
        assert not hasattr(reference, removed), removed


def test_a_private_arithmetic_helper_refuses_an_unapproved_reference():
    # The underscore is not the boundary: the helper itself refuses to compute.
    volume = volume_reference("test_flour", 1, status=UNDER_REVIEW)
    converted = reference._convert_mass_to_volume(Fraction(40), volume, MeasureVolumes())

    assert converted.reason is MassConversionReason.REFERENCE_NOT_APPROVED
    assert converted.status is MassConversionStatus.UNSUPPORTED
    assert converted.exact_count is None
    assert converted.volume_ml is None
    assert converted.rounded is None
    assert converted.target_unit is None

    count = piece_reference("test_piece", "test_piece", status=UNDER_REVIEW)
    counted = reference._convert_mass_to_count(Fraction(600), count)

    assert counted.reason is MassConversionReason.REFERENCE_NOT_APPROVED
    assert counted.count_low is None
    assert counted.count_high is None
    assert counted.rounded_low is None
    assert counted.rounded_high is None


def test_an_approved_synthetic_fixture_is_still_testable_through_the_helper():
    # Arithmetic stays verifiable with explicitly approved synthetic records.
    volume = volume_reference("test_flour", 1)
    converted = reference._convert_mass_to_volume(Fraction(18), volume, MeasureVolumes())

    assert converted.reason is MassConversionReason.CONVERTED
    assert converted.exact_count == Fraction(6, 5)


def test_no_real_record_produces_a_number_through_the_public_gateway():
    # R1-01: every shipped record is inactive, so no supported public path may return a
    # numeric conversion for any of them.
    for entry in DEFAULT_REGISTRY.ingredients:
        for size_class in (None, "100-count"):
            result = convert_ingredient_mass(
                entry.aliases[0].text,
                parse_quantity("600 г"),
                target=MeasureVolumes(),
                size_class=size_class,
                registry=DEFAULT_REGISTRY,
            )
            assert result is not None
            assert result.conversion is None, (entry.ingredient_id, size_class)
            assert result.status is not MassConversionStatus.REFERENCE_APPROXIMATE


# --- R1-02: the selected measurement's mass basis governs ----------------------------


def commercial_potato_registry(
    *, evidence_basis=MassBasis.AS_PURCHASED, piece_basis=MassBasis.AS_PURCHASED
):
    """A synthetic 100-count commercial potato grade, approved, as-purchased evidence."""
    entry = make_entry(
        "test_potato",
        "тестовый корень",
        state=PieceState.RAW,
        basis=MassBasis.AS_PURCHASED,
        piece_id="test_potato",
        accepted=("basis:edible", "size:small", "size:medium"),
    )
    shape = piece("test_potato", basis=piece_basis)
    count_weight = Fraction(8) * Fraction(45359237, 1600000)
    reference_row = piece_reference(
        "test_potato",
        "test_potato",
        kind=ReferenceKind.CALIBRATION_CLASS,
        size_class="100-count",
        weight_min_g=count_weight,
        evidence_id="fixture.commercial",
    )
    evidence = (
        _evidence(
            "fixture.commercial",
            mass_g=count_weight,
            volume_ml=None,
            mass_basis=evidence_basis,
        ),
    )
    return build_registry(
        [entry], pieces=[shape], piece_references=[reference_row], evidence=evidence
    )


def test_an_as_purchased_commercial_grade_never_converts_peeled_edible_mass():
    # R1-02: the reference's own evidence is as-purchased, so an explicitly peeled mass
    # must not borrow it, even when `size_class` names the grade.
    result = convert_ingredient_mass(
        "очищенный тестовый корень",
        parse_quantity("600 г"),
        target=MeasureVolumes(),
        size_class="100-count",
        registry=commercial_potato_registry(),
    )

    assert result.conversion is None
    assert result.reason is MassConversionReason.STATE_MISMATCH
    assert result.status is MassConversionStatus.UNSUPPORTED


def test_the_same_as_purchased_reference_converts_an_as_purchased_mass():
    # The positive control: matching bases still convert, so the refusal above is about
    # the basis and not about the reference being unusable.
    result = convert_ingredient_mass(
        "тестовый корень",
        parse_quantity("600 г"),
        target=MeasureVolumes(),
        size_class="100-count",
        registry=commercial_potato_registry(),
    )

    assert result.reason is MassConversionReason.CONVERTED
    assert result.conversion is not None


def test_evidence_that_contradicts_its_piece_metadata_is_reported_not_converted():
    # R1-02: a reference whose evidence basis differs from its piece identity is a
    # registry defect. The runtime reports it and the validator names it.
    registry = commercial_potato_registry(
        evidence_basis=MassBasis.AS_PURCHASED, piece_basis=MassBasis.EDIBLE_PORTION
    )

    result = convert_ingredient_mass(
        "очищенный тестовый корень",
        parse_quantity("600 г"),
        target=MeasureVolumes(),
        size_class="100-count",
        registry=registry,
    )

    assert result.conversion is None
    assert result.status is MassConversionStatus.AMBIGUOUS
    assert result.reason is MassConversionReason.REFERENCE_VARIANT_CONFLICT
    assert any(
        "evidence basis differs from its piece" in problem
        for problem in validate_registry(registry)
    )


# --- R1-03: an explicit size argument never overrides a named size -------------------


def test_a_size_argument_that_contradicts_the_name_is_a_qualifier_conflict():
    result = convert_ingredient_mass(
        "очищенный тестовый корень средний",
        parse_quantity("600 г"),
        target=MeasureVolumes(),
        size_class="small",
        registry=potato_registry(),
    )

    assert result.conversion is None
    assert result.status is MassConversionStatus.AMBIGUOUS
    assert result.reason is MassConversionReason.QUALIFIER_CONFLICT


def test_a_size_argument_that_agrees_with_the_name_proceeds():
    result = convert_ingredient_mass(
        "очищенный тестовый корень средний",
        parse_quantity("600 г"),
        target=MeasureVolumes(),
        size_class="medium",
        registry=potato_registry(),
    )

    assert result.reason is MassConversionReason.CONVERTED
    assert result.conversion.size_class == "medium"
    assert result.conditional is False


def test_a_conditional_default_never_overrides_a_named_size():
    # The registry carries an approved default class; an explicit size word still wins.
    result = convert_ingredient_mass(
        "очищенный тестовый корень мелкий",
        parse_quantity("600 г"),
        target=MeasureVolumes(),
        registry=potato_registry(default=True),
    )

    assert result.reason is MassConversionReason.CONVERTED
    assert result.conversion.size_class == "small"
    assert result.conditional is False
    assert result.assumption is None


# --- R1-04: fractional teaspoon fallback ---------------------------------------------


def test_the_terminal_teaspoon_fallback_accepts_half_a_teaspoon():
    # Density 6/5: 3 g is 2.5 mL, exactly half a 5 mL teaspoon.
    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("3 г"), target=MeasureVolumes(),
        registry=flour_registry(Fraction(6, 5)),
    )

    assert result.reason is MassConversionReason.CONVERTED
    conversion = result.conversion
    assert conversion.target_unit is Unit.TEASPOON
    assert conversion.exact_count == Fraction(1, 2)
    assert conversion.rounded.amount == Fraction(1, 2)


def test_the_terminal_teaspoon_fallback_accepts_a_quarter_teaspoon():
    # Density 6/5: 1.5 g is 1.25 mL, exactly a quarter teaspoon.
    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("1,5 г"), target=MeasureVolumes(),
        registry=flour_registry(Fraction(6, 5)),
    )

    assert result.reason is MassConversionReason.CONVERTED
    conversion = result.conversion
    assert conversion.target_unit is Unit.TEASPOON
    assert conversion.exact_count == Fraction(1, 4)
    assert conversion.rounded.amount == Fraction(1, 4)


def test_a_fractional_teaspoon_still_obeys_the_five_percent_gate():
    # 3.1 g at 6/5 is 2.583 mL: 0.5167 tsp rounds to 1/2 within 5%.
    accepted = convert_ingredient_mass(
        "тестовая мука", parse_quantity("3,1 г"), target=MeasureVolumes(),
        registry=flour_registry(Fraction(6, 5)),
    )
    # 4 g at 6/5 is 3.333 mL: 0.6667 tsp has no step within 5%.
    refused = convert_ingredient_mass(
        "тестовая мука", parse_quantity("4 г"), target=MeasureVolumes(),
        registry=flour_registry(Fraction(6, 5)),
    )

    assert accepted.conversion.rounded.amount == Fraction(1, 2)
    assert refused.reason is MassConversionReason.ROUNDING_GATE_FAILED
    assert refused.conversion is None


def test_glass_and_tablespoon_still_require_one_whole_unit():
    # 7.5 g at 1 g/mL is exactly half a tablespoon, which is below one and skipped; the
    # teaspoon fallback then expresses 1.5.
    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("7,5 г"), target=MeasureVolumes(),
        registry=flour_registry(Fraction(1)),
    )

    conversion = result.conversion
    assert conversion.target_unit is Unit.TEASPOON
    assert conversion.exact_count == Fraction(3, 2)


def test_a_fractional_result_is_not_promoted_to_a_unit_the_reference_excludes():
    # Without a teaspoon fallback there is no half-tablespoon answer either.
    registry = flour_registry(Fraction(1), target_units=("tablespoon",))

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("7,5 г"), target=MeasureVolumes(), registry=registry
    )

    assert result.status is MassConversionStatus.UNSUPPORTED
    assert result.reason is MassConversionReason.ROUNDING_GATE_FAILED
    assert result.conversion is None


# --- R1-05: a refusal never carries a conversion object ------------------------------


@pytest.mark.parametrize(
    "case",
    [
        "volume_rounding",
        "count_rounding",
        "interval_too_wide",
        "unapproved",
        "state_mismatch",
    ],
)
def test_every_unsuccessful_conversion_has_no_conversion_payload(case):
    if case == "volume_rounding":
        result = convert_ingredient_mass(
            "тестовая мука", parse_quantity("9,5 г"), target=MeasureVolumes(),
            registry=flour_registry(Fraction(1)),
        )
    elif case == "count_rounding":
        result = convert_ingredient_mass(
            "тестовый кусок", parse_quantity("40 г"), target=MeasureVolumes(),
            registry=point_registry(200),
        )
    elif case == "interval_too_wide":
        result = convert_ingredient_mass(
            "тестовый кусок", parse_quantity("500 г"), target=MeasureVolumes(),
            registry=interval_registry(100, 200),
        )
    elif case == "unapproved":
        result = convert_ingredient_mass(
            "тестовая мука", parse_quantity("40 г"), target=MeasureVolumes(),
            registry=build_registry(
                [make_entry("test_flour", "тестовая мука")],
                volumes=[volume_reference("test_flour", 1, status=UNDER_REVIEW)],
            ),
        )
    else:
        result = convert_ingredient_mass(
            "тестовый корень", parse_quantity("600 г"), target=MeasureVolumes(),
            registry=potato_registry(default=True),
        )

    assert result.status is not MassConversionStatus.REFERENCE_APPROXIMATE
    assert result.reason is not MassConversionReason.CONVERTED
    assert result.conversion is None


# --- R1-06: evidence and reference validation ----------------------------------------


def _probe_registry(
    *,
    evidence,
    weight_min_g=200,
    weight_max_g=None,
    kind=ReferenceKind.POINT,
    size_class=None,
):
    entry = make_entry(
        "test_piece", "тестовый кусок", basis=MassBasis.AS_PURCHASED, piece_id="test_piece"
    )
    shape = piece("test_piece", basis=MassBasis.AS_PURCHASED)
    reference_row = piece_reference(
        "test_piece",
        "test_piece",
        kind=kind,
        size_class=size_class,
        weight_min_g=weight_min_g,
        weight_max_g=weight_max_g,
        evidence_id=evidence.evidence_id,
    )
    return build_registry(
        [entry], pieces=[shape], piece_references=[reference_row], evidence=(evidence,)
    )


def _probe_evidence(**overrides):
    row = _evidence(
        "fixture.probe", mass_g=200, volume_ml=None, mass_basis=MassBasis.AS_PURCHASED
    )
    return replace(row, **overrides)


def test_a_point_weight_that_differs_from_its_evidence_is_a_structural_problem():
    registry = _probe_registry(evidence=_probe_evidence(mass_g=213), weight_min_g=170)

    assert any(
        "point weight differs from its own evidence" in problem
        for problem in validate_registry(registry)
    )


def test_a_calibration_point_class_that_differs_from_its_evidence_is_a_structural_problem():
    registry = _probe_registry(
        evidence=_probe_evidence(mass_g=213),
        weight_min_g=170,
        kind=ReferenceKind.CALIBRATION_CLASS,
        size_class="100-count",
    )

    assert any(
        "point class differs from its own evidence" in problem
        for problem in validate_registry(registry)
    )


def test_a_measured_interval_without_a_range_is_a_structural_problem():
    registry = _probe_registry(
        evidence=_probe_evidence(mass_g=200),
        weight_min_g=200,
        weight_max_g=200,
        kind=ReferenceKind.MEASURED_INTERVAL,
    )

    assert any(
        "interval without a range" in problem for problem in validate_registry(registry)
    )


def test_an_inverted_or_non_positive_weight_is_a_structural_problem():
    inverted = _probe_registry(
        evidence=_probe_evidence(mass_g=200), weight_min_g=300, weight_max_g=100
    )
    zero = _probe_registry(evidence=_probe_evidence(mass_g=0), weight_min_g=0)

    assert any("inverted range" in problem for problem in validate_registry(inverted))
    assert any(
        "minimum weight is non-positive" in problem for problem in validate_registry(zero)
    )


def test_a_piece_reference_must_draw_on_a_piece_measurement():
    registry = _probe_registry(
        evidence=_probe_evidence(mass_g=200, original_volume_ml=Fraction(240))
    )

    assert any(
        "evidence is a volume measurement" in problem
        for problem in validate_registry(registry)
    )


def test_an_unsupported_numeric_type_is_reported_instead_of_raising():
    for value in (0.5, "200", True, float("nan"), float("inf")):
        registry = _probe_registry(evidence=_probe_evidence(mass_g=value))

        problems = validate_registry(registry)

        assert any("not an exact number" in problem for problem in problems), value


def test_a_zero_source_volume_is_reported_without_dividing():
    entry = make_entry("test_flour", "тестовая мука")
    volume = volume_reference("test_flour", 1, evidence_id="fixture.vol")
    evidence = replace(
        _evidence("fixture.vol", mass_g=120, volume_ml=1),
        original_volume_ml=Fraction(0),
    )
    registry = build_registry([entry], volumes=[volume], evidence=(evidence,))

    problems = validate_registry(registry)

    assert any("volume is non-positive" in problem for problem in problems)
    assert any("evidence volume is not usable" in problem for problem in problems)


def test_an_approved_coefficient_without_evidence_produces_no_number():
    # An approved record whose evidence does not resolve is incomplete data, never a
    # coefficient: the volume path refuses it before any arithmetic runs.
    entry = make_entry("test_flour", "тестовая мука")
    registry = ReferenceRegistry(
        revision="test-fixture",
        sources=(TEST_SOURCE,),
        evidence=(),
        ingredients=(entry,),
        generic_words=(),
        pieces=(),
        volume_references=(volume_reference("test_flour", 1, evidence_id="fixture.missing"),),
        piece_references=(),
    )

    result = convert_ingredient_mass(
        "тестовая мука", parse_quantity("40 г"), target=MeasureVolumes(), registry=registry
    )

    assert result.reason is MassConversionReason.REFERENCE_MISSING
    assert result.conversion is None


def test_numerical_evidence_is_never_read_from_the_quoted_text():
    # The quote says 999 g; the structured mass is 200 g, and only that is used.
    evidence = _probe_evidence(mass_g=200, quoted="the source says 999 g per piece")
    registry = _probe_registry(evidence=evidence)

    assert validate_registry(registry) == ()
    conversion = reference._convert_mass_to_count(
        Fraction(200), registry.piece_references[0]
    )
    assert conversion.count_low == 1


def test_a_single_product_generic_word_is_structurally_coherent():
    # R1-06/R1-07: catalogue completeness is not a structural requirement.
    entry = make_entry("test_only", "единственный продукт")
    registry = build_registry(
        [entry], generic_words=[GenericWord("единственный термин", ("test_only",))]
    )

    assert validate_registry(registry) == ()


# --- R1-09: canonical candidate order and digest ------------------------------------


def test_candidate_order_does_not_depend_on_registry_order():
    registry = build_registry(
        [make_entry("test_b", "двойной продукт"), make_entry("test_a", "двойной продукт")]
    )

    match = match_ingredient("двойной продукт", registry=registry)

    assert [entry.ingredient_id for entry in match.entries] == ["test_a", "test_b"]


def test_generic_candidates_are_ordered_by_identity():
    match = match_ingredient("мука")

    assert [entry.ingredient_id for entry in match.entries] == [
        "buckwheat_flour",
        "wheat_flour_all_purpose",
    ]


def test_permuting_equivalent_records_does_not_change_the_digest():
    entry_a = make_entry("test_a", "альфа продукт", "тест а")
    entry_b = make_entry("test_b", "бета продукт")
    reordered_a = make_entry("test_a", "тест а", "альфа продукт")
    volume_a = volume_reference("test_a", 1, evidence_id="fixture.va")
    volume_b = volume_reference("test_b", Fraction(3, 2), evidence_id="fixture.vb")

    first = build_registry([entry_a, entry_b], volumes=[volume_a, volume_b])
    second = build_registry([entry_b, reordered_a], volumes=[volume_b, volume_a])

    assert registry_digest(first) == registry_digest(second)


def test_the_digest_changes_when_a_value_changes():
    base = build_registry([make_entry("test_a", "альфа продукт")])
    changed = build_registry([make_entry("test_a", "альфа продукт", status=UNDER_REVIEW)])

    assert registry_digest(base) != registry_digest(changed)


# --- B2-1-R2: exact evidence arithmetic and canonical serialization -----------------


def test_a_third_grams_per_ml_coefficient_is_accepted_exactly():
    # D1 case A: 1 g / 3 mL is exactly Fraction(1, 3). The operands stay `int` on
    # purpose: an integer division would produce a float and falsely report a mismatch.
    entry = make_entry("test_flour", "тестовая мука")
    volume = volume_reference("test_flour", Fraction(1, 3))
    evidence = replace(
        _evidence("fixture.volume", mass_g=1, volume_ml=3),
        mass_g=1,
        original_volume_ml=3,
    )
    registry = build_registry([entry], volumes=[volume], evidence=(evidence,))

    assert validate_registry(registry) == ()


def test_an_integral_coefficient_from_integer_evidence_is_accepted():
    entry = make_entry("test_flour", "тестовая мука")
    volume = volume_reference("test_flour", 240)
    evidence = replace(
        _evidence("fixture.volume", mass_g=240, volume_ml=1),
        mass_g=240,
        original_volume_ml=1,
    )
    registry = build_registry([entry], volumes=[volume], evidence=(evidence,))

    assert validate_registry(registry) == ()


def test_a_huge_integer_evidence_ratio_is_compared_without_overflow():
    # D1 case B: 10**400 / 1 is an exact integer ratio. A float division overflows, so
    # the comparison must run on Fractions; the mismatch is still found deterministically.
    entry = make_entry("test_flour", "тестовая мука")
    volume = volume_reference("test_flour", 1)
    evidence = replace(
        _evidence("fixture.volume", mass_g=1, volume_ml=1),
        mass_g=10 ** 400,
        original_volume_ml=1,
    )
    registry = build_registry([entry], volumes=[volume], evidence=(evidence,))

    assert validate_registry(registry) == (
        "volume reference test_flour: coefficient differs from its own evidence",
    )


def test_evidence_that_really_contradicts_its_coefficient_is_reported():
    entry = make_entry("test_flour", "тестовая мука")
    volume = volume_reference("test_flour", 2)
    evidence = replace(
        _evidence("fixture.volume", mass_g=3, volume_ml=1),
        mass_g=3,
        original_volume_ml=1,
    )
    registry = build_registry([entry], volumes=[volume], evidence=(evidence,))

    assert any(
        "coefficient differs from its own evidence" in problem
        for problem in validate_registry(registry)
    )


def test_volume_reference_permutations_have_one_digest():
    # Both references share ingredient, evidence and derivation; only the status
    # differs. A partial sort key would leave them in input order, so the digest would
    # depend on how the tuple happened to be written.
    entry = make_entry("test_a", "альфа продукт")
    approved = volume_reference("test_a", Fraction(1, 2))
    under_review = replace(approved, status=UNDER_REVIEW)
    first = build_registry([entry], volumes=[approved, under_review])
    second = build_registry([entry], volumes=[under_review, approved])

    assert validate_registry(first) == ()
    assert registry_digest(first) == registry_digest(second)


def test_piece_reference_permutations_have_one_digest():
    entry = make_entry(
        "test_piece", "тестовый кусок", piece_id="test_piece", basis=MassBasis.AS_PURCHASED
    )
    shape = piece("test_piece", basis=MassBasis.AS_PURCHASED)
    approved = piece_reference("test_piece", "test_piece", weight_min_g=200)
    under_review = replace(approved, status=UNDER_REVIEW)
    first = build_registry(
        [entry], pieces=[shape], piece_references=[approved, under_review]
    )
    second = build_registry(
        [entry], pieces=[shape], piece_references=[under_review, approved]
    )

    assert validate_registry(first) == ()
    assert registry_digest(first) == registry_digest(second)


def test_a_volume_approval_status_change_changes_the_digest():
    entry = make_entry("test_a", "альфа продукт")
    approved = volume_reference("test_a", Fraction(1, 2))
    base = build_registry([entry], volumes=[approved])
    changed = build_registry([entry], volumes=[replace(approved, status=UNDER_REVIEW)])

    assert registry_digest(base) != registry_digest(changed)


def test_a_piece_approval_status_change_changes_the_digest():
    entry = make_entry(
        "test_piece", "тестовый кусок", piece_id="test_piece", basis=MassBasis.AS_PURCHASED
    )
    shape = piece("test_piece", basis=MassBasis.AS_PURCHASED)
    approved = piece_reference("test_piece", "test_piece", weight_min_g=200)
    base = build_registry([entry], pieces=[shape], piece_references=[approved])
    changed = build_registry(
        [entry],
        pieces=[shape],
        piece_references=[replace(approved, status=UNDER_REVIEW)],
    )

    assert registry_digest(base) != registry_digest(changed)


def _digest_of_method_and_conditions(method, conditions):
    evidence = replace(
        _evidence("fixture.volume", mass_g=120, volume_ml=240),
        method=method,
        conditions=conditions,
    )
    entry = make_entry("test_flour", "тестовая мука")
    registry = build_registry(
        [entry],
        volumes=[volume_reference("test_flour", Fraction(1, 2))],
        evidence=(evidence,),
    )
    assert validate_registry(registry) == ()
    return registry_digest(registry)


def test_the_delimiter_collision_now_yields_distinct_digests():
    # These two records once concatenated to the same "|"-joined text:
    # "level|packed" + "dry" and "level" + "packed|dry".
    one = _digest_of_method_and_conditions("level|packed", "dry")
    other = _digest_of_method_and_conditions("level", "packed|dry")

    assert one != other


def test_separators_empty_strings_and_unicode_stay_distinct():
    variants = [
        ("a,b", "c"),
        ("a", "b,c"),
        ('a"b', "c"),
        ("a\\b", "c"),
        ("a\nb", "c"),
        ("", ""),
        ("|", ""),
        ("", "|"),
        ("склад|упаковано", "сухо"),
        ("склад", "упаковано|сухо"),
    ]

    digests = {
        _digest_of_method_and_conditions(method, conditions)
        for method, conditions in variants
    }

    assert len(digests) == len(variants)


def test_the_canonical_text_is_the_hand_written_typed_json():
    # The expected text is written by hand, not produced by the serializer under test:
    # one JSON record per line, an explicit record-type tag first, exact numbers encoded
    # as ["num", numerator, denominator], and Russian text kept as text.
    evidence = MeasurementEvidence(
        evidence_id="probe.evidence",
        source_id="fixture.source",
        quoted="1/3 стакана",
        original_amount=Fraction(1, 3),
        original_unit="us_cup",
        original_volume_ml=Fraction(240),
        mass_g=Fraction(80),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="level",
        conditions="dry",
        limitations="none",
    )
    registry = ReferenceRegistry(
        revision="test-fixture",
        sources=(TEST_SOURCE,),
        evidence=(evidence,),
        ingredients=(),
        generic_words=(),
        pieces=(),
        volume_references=(),
        piece_references=(),
    )
    expected = "\n".join(
        [
            '["evidence","probe.evidence","fixture.source","1/3 стакана",'
            '["num","1","3"],"us_cup",["num","240","1"],["num","80","1"],'
            '"not_applicable","level","dry","none",null]',
            '["revision","test-fixture"]',
            '["source","fixture.source","Synthetic test data","about:blank","2026-11-13",'
            '"none: test fixture",null,null]',
        ]
    )

    assert reference._canonical_lines(registry) == expected.split("\n")
    assert registry_digest(registry) == hashlib.sha256(expected.encode("utf-8")).hexdigest()


def test_the_digest_is_stable_on_repeated_execution():
    assert registry_digest(DEFAULT_REGISTRY) == registry_digest(DEFAULT_REGISTRY)


def _digest_in_a_subprocess(hash_seed):
    root = Path(reference.__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = hash_seed
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(root), environment.get("PYTHONPATH", "")]
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from house_helper.core.ingredient_reference import registry_digest;"
            " print(registry_digest())",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
        timeout=300,
    )
    return completed.stdout.strip()


def test_the_digest_does_not_depend_on_process_hash_randomization():
    expected = registry_digest(DEFAULT_REGISTRY)

    assert _digest_in_a_subprocess("0") == expected
    assert _digest_in_a_subprocess("1") == expected
