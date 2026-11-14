"""KH-F01-B1.1-C: the typed schema_version 2 annotation block.

These tests pin the adapter, not the already accepted stage-A audit or stage-B scanner.
They check the frozen §15.8.2 contract: the complete shape, per-term capacity origins with
no inheritance (OD-03), linked step conversions through the released B0/B1 arithmetic,
safe rationals, Unicode code-point spans, favorite-version binding, failure confinement,
the 32/128 caps and deterministic ordering.
"""

import hashlib
import json
from contextlib import contextmanager
from fractions import Fraction

import pytest

import house_helper.core.step_scan as step_scan_module
from house_helper.core.recipes import (
    MAX_RECIPE_BLOCK_LENGTH,
    MAX_STEP_LENGTH,
    RECIPE_END,
    RECIPE_START,
    Ingredient,
    parse_reply,
)
from house_helper.web import measure_targets as measure_targets
from house_helper.web.measure_targets import (
    SAFE_INTEGER_MAX,
    UnsafeFraction,
    measure_block_v2,
)

DEFAULT = measure_targets.volumes()


def block(ingredients, steps=(), target=DEFAULT, **kwargs):
    kwargs.setdefault("favorite_version", 1)
    return measure_block_v2(ingredients, steps, target, **kwargs)


def by_position(items):
    return {item["position"]: item for item in items}


@contextmanager
def _counting_same_word():
    """Count every real `_same_word` comparison made inside the block."""
    calls = []
    real = step_scan_module._same_word

    def counting(left, right):
        calls.append((left, right))
        return real(left, right)

    step_scan_module._same_word = counting
    try:
        yield calls
    finally:
        step_scan_module._same_word = real


# --- the complete frozen shape ---------------------------------------------------------------


def test_the_block_carries_every_frozen_v2_field_and_keeps_the_b1_fields():
    result = block(
        (Ingredient("вода", "1 стакан (200 мл)"), Ingredient("соль", "300 г")),
        ("Добавьте 500 мл воды.",),
        favorite_version=4,
    )

    assert set(result) == {
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
    assert result["schema_version"] == 2
    assert result["favorite_version"] == 4
    assert result["target"] == {"glass_ml": 250, "teaspoon_ml": 5, "tablespoon_ml": 15}
    assert result["source_glass_ml"] is None

    # Every released B1 ingredient field is still there, plus the required source_terms.
    conversion = result["ingredients"][0]
    assert set(conversion) == {
        "position",
        "source",
        "source_kind",
        "target_unit",
        "status",
        "reason",
        "exact",
        "rounded",
        "identical",
        "presentation",
        "source_terms",
    }
    assert conversion["position"] == 0
    assert conversion["source"] == "1 стакан (200 мл)"
    assert conversion["source_kind"] == "household"
    assert conversion["target_unit"] == "glass"
    assert conversion["status"] == "nominal_household"
    assert conversion["reason"] == "converted"
    assert conversion["exact"] == {"numerator": 4, "denominator": 5}
    assert conversion["presentation"] == "exact"

    assert set(result["provenance"]) == {"status", "declared", "incomplete"}
    assert set(result["analysis"]) == {
        "status",
        "spans_emitted",
        "spans_omitted",
        "omitted_reasons",
        "limits",
    }
    assert result["analysis"]["limits"] == {
        "max_spans_per_step": 32,
        "max_spans_per_recipe": 128,
        "max_step_chars": 1000,
        "max_steps": 50,
    }


def test_text_only_and_missing_lists_produce_valid_empty_arrays():
    result = block(None, None)

    assert result["ingredients"] == []
    assert result["steps"] == []
    assert result["provenance"] == {
        "status": "complete",
        "declared": [],
        "incomplete": [],
    }
    assert result["analysis"]["status"] == "complete"
    assert result["analysis"]["spans_emitted"] == 0
    assert result["source_glass_required"] is False
    assert result["steps_source_glass_required"] is False


def test_favorite_version_must_be_a_positive_integer():
    for value in (0, -1, True, "1", 1.0, None):
        with pytest.raises(TypeError):
            measure_block_v2((), (), DEFAULT, favorite_version=value)  # type: ignore[arg-type]


# --- per-term capacity origins (OD-03) --------------------------------------------------------


def test_a_declared_capacity_is_declared_text_and_a_spoon_is_not_applicable():
    result = block(
        (Ingredient("вода", "1 стакан (200 мл)"), Ingredient("масло", "2 ст. л."))
    )
    water, oil = block_ingredients(result)

    assert water["source_terms"] == [
        {
            "unit": "glass",
            "amount": {"numerator": 1, "denominator": 1},
            "capacity_ml": {"numerator": 200, "denominator": 1},
            "capacity_origin": "declared_text",
        }
    ]
    assert oil["source_terms"] == [
        {
            "unit": "tablespoon",
            "amount": {"numerator": 2, "denominator": 1},
            "capacity_ml": None,
            "capacity_origin": "not_applicable",
        }
    ]


def test_an_undeclared_glass_is_unresolved_and_asks_for_a_clarification():
    result = block((Ingredient("вода", "1 стакан"),))
    water = block_ingredients(result)[0]

    assert water["status"] == "clarification_required"
    assert water["reason"] == "glass_size_required"
    assert water["exact"] is None
    assert water["source_terms"][0]["capacity_ml"] is None
    assert water["source_terms"][0]["capacity_origin"] == "unresolved"
    assert result["source_glass_required"] is True


def test_a_clarification_resolves_only_the_undeclared_term():
    result = block((Ingredient("вода", "1 стакан"),), source_glass_ml=200)
    water = block_ingredients(result)[0]

    assert water["exact"] == {"numerator": 4, "denominator": 5}
    term = water["source_terms"][0]
    assert term["capacity_ml"] == {"numerator": 200, "denominator": 1}
    assert term["capacity_origin"] == "temporary_clarification"
    assert result["source_glass_ml"] == 200


def test_a_declaration_always_wins_over_a_clarification():
    result = block((Ingredient("вода", "1 стакан 240 мл"),), source_glass_ml=200)
    water = block_ingredients(result)[0]

    term = water["source_terms"][0]
    assert term["capacity_ml"] == {"numerator": 240, "denominator": 1}
    assert term["capacity_origin"] == "declared_text"
    assert water["exact"] == {"numerator": 24, "denominator": 25}


def test_two_terms_in_one_amount_report_independent_origins():
    result = block(
        (Ingredient("вода", "1 стакан (200 мл) + 1 стакан"),),
        source_glass_ml=250,
    )
    terms = block_ingredients(result)[0]["source_terms"]

    assert [term["capacity_origin"] for term in terms] == [
        "declared_text",
        "temporary_clarification",
    ]
    assert [term["capacity_ml"] for term in terms] == [
        {"numerator": 200, "denominator": 1},
        {"numerator": 250, "denominator": 1},
    ]
    # The 200 + 250 mL total is exact; no artificial conflict is created.
    assert block_ingredients(result)[0]["exact"] == {"numerator": 9, "denominator": 5}


# --- no automatic inheritance for steps -------------------------------------------------------


def test_a_step_never_inherits_a_linked_ingredients_declared_capacity():
    result = block(
        (Ingredient("вода", "1 стакан (200 мл)"),),
        ("Добавьте 1 стакан воды.",),
    )
    span = result["steps"][0]["spans"][0]

    assert span["link"] == "linked"
    assert span["ingredient_position"] == 0
    # OD-03: the ingredient's 200 mL does not reach the step term.
    assert span["capacity_origin"] == "unresolved"
    assert span["conversion"]["status"] == "clarification_required"
    assert span["conversion"]["exact"] is None
    assert result["steps_source_glass_required"] is True
    # The ingredient keeps its own declaration in the same answer.
    assert block_ingredients(result)[0]["source_terms"][0]["capacity_origin"] == (
        "declared_text"
    )


def test_tc_b11_41_step_terms_never_borrow_an_ingredient_declaration():
    ingredients = (
        Ingredient("вода", "1 стакан (200 мл)"),
        Ingredient("молоко", "1 стакан"),
    )
    steps = ("Добавьте 1 стакан воды.", "Добавьте 1 стакан молока.")
    result = block(ingredients, steps, source_glass_ml=250)

    water, milk = block_ingredients(result)
    # The water ingredient uses its own 200: 0.8 of the 250 mL target.
    assert water["source_terms"][0]["capacity_origin"] == "declared_text"
    assert water["exact"] == {"numerator": 4, "denominator": 5}
    # The water step does not inherit it; the temporary 250 applies (identity, no hint).
    water_span = result["steps"][0]["spans"][0]
    assert water_span["capacity_origin"] == "temporary_clarification"
    assert water_span["conversion"]["identical"] is True
    assert water_span["conversion"]["presentation"] == "none"
    # The milk ingredient and milk step both use the temporary 250.
    assert milk["source_terms"][0]["capacity_origin"] == "temporary_clarification"
    milk_span = result["steps"][1]["spans"][0]
    assert milk_span["capacity_origin"] == "temporary_clarification"
    assert milk_span["conversion"]["identical"] is True
    # No inheritance origin exists anywhere in the payload (OD-03).
    assert "inherited_from_ingredient" not in json.dumps(result)


def test_the_clarification_reaches_an_undeclared_step_term_without_persisting():
    result = block(
        (Ingredient("вода", "1 стакан (200 мл)"),),
        ("Добавьте 1 стакан воды.",),
        source_glass_ml=200,
    )
    span = result["steps"][0]["spans"][0]

    assert span["capacity_origin"] == "temporary_clarification"
    assert span["conversion"]["exact"] == {"numerator": 4, "denominator": 5}
    assert span["conversion"]["presentation"] == "exact"
    # The stored step text is echoed exactly and the flag is evaluated pre-clarification.
    assert span["text"] == "1 стакан"
    assert result["steps_source_glass_required"] is True


# --- linked conversions, failures, approximation and mass -------------------------------------


def test_linked_metric_spans_convert_independently_and_stay_sorted():
    result = block(
        (Ingredient("вода", "1 л"), Ingredient("молоко", "1 л")),
        ("Влейте 500 мл воды и добавьте 250 мл молока.",),
    )
    spans = result["steps"][0]["spans"]

    assert [(span["start"], span["end"]) for span in spans] == [(7, 13), (30, 36)]
    assert all(span["link"] == "linked" for span in spans)
    assert all(span["link_reason"] == "name_after" for span in spans)
    assert [span["ingredient_position"] for span in spans] == [0, 1]
    assert spans[0]["conversion"]["exact"] == {"numerator": 2, "denominator": 1}
    assert spans[1]["conversion"]["exact"] == {"numerator": 1, "denominator": 1}
    assert spans[0]["capacity_origin"] == "not_applicable"
    # Sorted and non-overlapping, as the contract requires.
    assert spans[0]["end"] <= spans[1]["start"]


def test_an_approximate_source_word_is_reported_exactly():
    for word in ("около", "примерно", "приблизительно"):
        result = block(
            (Ingredient("вода", "1 л"),),
            (f"Добавьте {word} 500 мл воды.",),
        )
        span = result["steps"][0]["spans"][0]

        assert span["source_approximation"] == {"approximate": True, "word": word}
        # Approximation changes the presentation marker, never the arithmetic.
        assert span["conversion"]["exact"] == {"numerator": 2, "denominator": 1}


def test_a_span_without_an_approximation_word_reports_a_null_word():
    result = block((Ingredient("вода", "1 л"),), ("Добавьте 500 мл воды.",))

    assert result["steps"][0]["spans"][0]["source_approximation"] == {
        "approximate": False,
        "word": None,
    }


def test_a_linked_mass_span_has_no_b1_1_conversion():
    result = block((Ingredient("мука", "1 кг"),), ("Добавьте 200 г муки.",))
    span = result["steps"][0]["spans"][0]

    assert span["link"] == "linked"
    assert span["link_reason"] == "name_after"
    assert span["conversion"] is None
    assert span["conversion_omitted_reason"] == "mass"


def test_a_rejected_construct_is_unsupported_and_never_converted():
    result = block((Ingredient("масло", "1 л"),), ("Добавьте 2–3 ст. л. масла.",))
    span = result["steps"][0]["spans"][0]

    assert span["link"] == "unsupported"
    assert span["link_reason"] == "range"
    assert span["conversion"] is None
    assert span["conversion_omitted_reason"] is None
    assert span["capacity_origin"] == "not_applicable"


def test_an_unresolved_amount_has_no_conversion_but_keeps_its_origin():
    result = block((Ingredient("вода", "1 л"),), ("Добавьте 1 стакан и перемешайте.",))
    span = result["steps"][0]["spans"][0]

    assert span["link"] == "unresolved"
    assert span["conversion"] is None
    assert span["conversion_omitted_reason"] is None
    # A supported household term reports its origin even without a local link.
    assert span["capacity_origin"] == "unresolved"


# --- exact capacity across the stage B -> C boundary -------------------------------------------


@pytest.mark.parametrize(
    ("amount", "numerator", "denominator"),
    (
        ("1 стакан (237½ мл)", 19, 20),
        ("1 стакан (237 1/2 мл)", 19, 20),
        ("1 стакан (500/3 мл)", 2, 3),
    ),
)
def test_a_step_declared_exact_capacity_survives_the_b1_parser(amount, numerator, denominator):
    result = block(
        (Ingredient("вода", "1 л"),),
        (f"Влейте {amount} воды.",),
        target=measure_targets.volumes(glass_ml=250),
    )
    span = result["steps"][0]["spans"][0]

    assert span["link"] == "linked"
    assert span["capacity_origin"] == "declared_text"
    assert span["conversion"]["status"] == "nominal_household"
    assert span["conversion"]["exact"] == {
        "numerator": numerator,
        "denominator": denominator,
    }
    # The span text is the untouched source, never the parser's substituted text.
    assert span["text"] == amount


# --- provenance -------------------------------------------------------------------------------


def test_provenance_records_a_package_content_without_inventing_a_volume():
    result = block((Ingredient("мука", "1 пачка (400 г)"),))

    entry = result["provenance"]["declared"][0]
    assert entry == {
        "ingredient_position": 0,
        "term_index": 0,
        "kind": "package",
        "status": "declared",
        "capacity_ml": None,
        "declared_content": {
            "amount": {"numerator": 400, "denominator": 1},
            "unit": "gram",
        },
        "origin": "declared_text",
        "reason": None,
    }
    assert result["ingredients"] == []  # no package-to-volume conversion
    assert result["provenance"]["status"] == "complete"


def test_not_applicable_terms_are_not_emitted_and_never_make_provenance_incomplete():
    result = block(
        (Ingredient("соль", "300 г"), Ingredient("яйца", "2 яйца")),
    )

    assert result["provenance"] == {
        "status": "complete",
        "declared": [],
        "incomplete": [],
    }


def test_an_undeclared_glass_is_reported_incomplete_without_an_error():
    result = block((Ingredient("вода", "1 стакан"),))

    entry = result["provenance"]["incomplete"][0]
    assert entry["status"] == "undeclared"
    assert entry["capacity_ml"] is None
    assert entry["origin"] == "unresolved"
    assert entry["reason"] == "capacity_not_declared"
    assert result["provenance"]["status"] == "incomplete"
    assert result["analysis"]["status"] == "incomplete"


# --- rational serialization -------------------------------------------------------------------


def test_fractions_are_reduced_exact_integers_with_a_relative_error_fraction():
    result = block(
        (Ingredient("молоко", "500 мл"),),
        target=measure_targets.volumes(glass_ml=240),
    )
    conversion = block_ingredients(result)[0]

    assert conversion["exact"] == {"numerator": 25, "denominator": 12}
    assert conversion["rounded"] == {
        "numerator": 2,
        "denominator": 1,
        "relative_error": {"numerator": 1, "denominator": 25},
    }
    for value in (conversion["exact"], conversion["rounded"]):
        assert isinstance(value["numerator"], int)
        assert isinstance(value["denominator"], int)


def test_the_serializer_refuses_unsafe_or_negative_integers_and_reduces():
    assert measure_targets._safe_fraction_json(Fraction(-2, -4)) == {
        "numerator": 1,
        "denominator": 2,
    }
    with pytest.raises(UnsafeFraction):
        measure_targets._safe_fraction_json(Fraction(SAFE_INTEGER_MAX + 1))
    with pytest.raises(UnsafeFraction):
        measure_targets._safe_fraction_json(Fraction(-1))


def test_an_unsafe_ingredient_conversion_is_omitted_and_counted_not_rounded():
    result = block((Ingredient("вода", "99999999999999999 мл"),))

    assert result["ingredients"] == []
    assert result["analysis"]["omitted_reasons"]["unsafe_fraction"] == 1
    assert result["analysis"]["status"] == "incomplete"


def test_an_unsafe_span_conversion_keeps_the_span_and_the_other_hints():
    result = block(
        (Ingredient("вода", "1 л"),),
        ("Влейте 99999999999999999 мл воды и добавьте 250 мл воды.",),
    )
    spans = result["steps"][0]["spans"]

    assert len(spans) == 2
    assert spans[0]["conversion"] is None
    assert spans[0]["conversion_omitted_reason"] == "unsafe_fraction"
    assert spans[0]["text"] == "99999999999999999 мл"
    assert spans[1]["conversion"]["exact"] == {"numerator": 1, "denominator": 1}
    assert result["analysis"]["omitted_reasons"]["unsafe_fraction"] == 1


# --- Unicode, echoes and hashes ---------------------------------------------------------------


def test_span_offsets_are_code_points_and_the_echo_and_hash_are_exact():
    text = "Добавьте 🥛 500 мл воды 🥛."
    result = block((Ingredient("вода", "1 л"),), (text,))
    step = result["steps"][0]
    span = step["spans"][0]

    assert step["step_text"] == text
    assert step["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert text[span["start"] : span["end"]] == span["text"] == "500 мл"
    # Two astral characters each occupy one code point but two UTF-16 units.
    assert span["start"] == 11
    assert len(text) < len(text.encode("utf-16-le")) // 2


# --- failure confinement ----------------------------------------------------------------------


def test_a_provenance_failure_leaves_ingredient_and_step_hints_intact(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(measure_targets, "audit_source_provenance", broken)
    result = block(
        (Ingredient("вода", "1 стакан (200 мл)"),),
        ("Добавьте 1 стакан 200 мл воды.",),
    )

    assert result["provenance"] == {
        "status": "failed",
        "declared": [],
        "incomplete": [],
    }
    assert result["analysis"]["status"] == "failed"
    assert block_ingredients(result)[0]["exact"] == {"numerator": 4, "denominator": 5}
    assert result["steps"][0]["spans"][0]["conversion"]["exact"] == {
        "numerator": 4,
        "denominator": 5,
    }


def test_a_provenance_serialization_failure_is_confined(monkeypatch):
    def broken(entry):
        raise RuntimeError("provenance serialization failed")

    monkeypatch.setattr(measure_targets, "_provenance_entry_json", broken)
    result = block(
        (Ingredient("вода", "1 стакан (200 мл)"),),
        ("Добавьте 500 мл воды.",),
    )

    assert result["provenance"] == {
        "status": "failed",
        "declared": [],
        "incomplete": [],
    }
    assert result["analysis"]["status"] == "failed"
    # The other domains survive intact.
    assert block_ingredients(result)[0]["exact"] == {"numerator": 4, "denominator": 5}
    assert result["steps"][0]["spans"][0]["conversion"] is not None


def test_a_whole_step_pass_failure_yields_no_steps_and_does_not_retry(monkeypatch):
    calls = []

    def broken(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("scan failed")

    # Only the whole pass is replaced: there is no per-step fallback to call.
    monkeypatch.setattr(measure_targets, "scan_recipe_steps", broken)
    result = block(
        (Ingredient("вода", "1 стакан (200 мл)"),),
        ("Добавьте 500 мл воды.",),
    )

    assert calls == [True]  # one whole pass, never a second per-step pass
    assert result["steps"] == []
    assert result["steps_source_glass_required"] is False
    assert result["analysis"]["status"] == "failed"
    assert result["provenance"]["status"] == "complete"
    assert block_ingredients(result)[0]["status"] == "nominal_household"


def test_one_step_serialization_failure_does_not_suppress_the_other_steps(monkeypatch):
    real = measure_targets._step_json

    def selective(step_index, text, spans, status):
        if step_index == 1:
            raise RuntimeError("this step failed to serialize")
        return real(step_index, text, spans, status)

    monkeypatch.setattr(measure_targets, "_step_json", selective)
    result = block(
        (Ingredient("вода", "1 л"),),
        (
            "Добавьте 500 мл воды.",
            "Добавьте 250 мл воды.",
            "Добавьте 100 мл воды.",
        ),
    )

    steps = {step["step_index"]: step for step in result["steps"]}
    assert steps[0]["status"] == "complete"
    assert steps[1] == {
        "step_index": 1,
        "step_text": "Добавьте 250 мл воды.",
        "text_sha256": hashlib.sha256("Добавьте 250 мл воды.".encode("utf-8")).hexdigest(),
        "status": "failed",
        "spans": [],
    }
    assert steps[2]["status"] == "complete"
    assert result["analysis"]["status"] == "failed"
    # The independent ingredient and provenance domains are untouched.
    assert block_ingredients(result)[0]["status"] == "nominal_household"
    assert result["provenance"]["status"] == "complete"


def test_one_failing_span_conversion_does_not_suppress_the_other_spans(monkeypatch):
    real = measure_targets._conversion_from_quantity

    def selective(position, quantity, target, *, source_glass_ml=None):
        if quantity.text == "500 мл":
            raise RuntimeError("this span failed")
        return real(
            position, quantity, target, source_glass_ml=source_glass_ml
        )

    monkeypatch.setattr(measure_targets, "_conversion_from_quantity", selective)
    result = block(
        (Ingredient("вода", "1 л"),),
        ("Влейте 500 мл воды и добавьте 250 мл воды.",),
    )
    spans = result["steps"][0]["spans"]

    assert len(spans) == 2
    assert spans[0]["conversion"] is None
    assert spans[0]["conversion_omitted_reason"] == "conversion_failed"
    assert spans[0]["link"] == "linked"
    assert spans[1]["conversion"]["exact"] == {"numerator": 1, "denominator": 1}


def _broken_span_serializer(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("injected span serialization failure")

    monkeypatch.setattr(measure_targets, "_span_json", broken)


def test_a_failed_span_keeps_a_declared_capacity_origin(monkeypatch):
    _broken_span_serializer(monkeypatch)
    result = block((Ingredient("вода", "1 л"),), ("Влейте 1 стакан 200 мл воды.",))

    [span] = result["steps"][0]["spans"]
    assert span["text"] == "1 стакан 200 мл"
    assert span["link"] == "linked"
    assert span["link_reason"] == "name_after"
    assert span["ingredient_position"] == 0
    assert span["conversion"] is None
    assert span["conversion_omitted_reason"] == "conversion_failed"
    # The scanner declared this capacity; the fallback must not erase it.
    assert span["capacity_origin"] == "declared_text"


def test_a_failed_span_keeps_a_clarified_capacity_origin(monkeypatch):
    _broken_span_serializer(monkeypatch)
    result = block(
        (Ingredient("вода", "1 л"),),
        ("Добавьте 1 стакан воды.",),
        source_glass_ml=250,
    )

    [span] = result["steps"][0]["spans"]
    assert span["capacity_origin"] == "temporary_clarification"
    assert span["conversion"] is None
    assert span["conversion_omitted_reason"] == "conversion_failed"


def test_a_failed_span_keeps_an_unresolved_capacity_origin(monkeypatch):
    _broken_span_serializer(monkeypatch)
    result = block((Ingredient("вода", "1 л"),), ("Добавьте 1 стакан воды.",))

    [span] = result["steps"][0]["spans"]
    # No declaration and no clarification: the origin is the honest `unresolved`, never a
    # fabricated `declared_text` or `temporary_clarification`.
    assert span["capacity_origin"] == "unresolved"
    assert result["steps_source_glass_required"] is True


def test_a_failed_span_keeps_not_applicable_for_metric_and_spoon_terms(monkeypatch):
    _broken_span_serializer(monkeypatch)
    result = block(
        (Ingredient("вода", "1 л"), Ingredient("соль", "1 ч. л.")),
        ("Добавьте 500 мл воды и 1 ч. л. соли.", "Добавьте 1-2 стакана воды."),
    )

    metric, spoon = result["steps"][0]["spans"]
    assert metric["capacity_origin"] == "not_applicable"
    assert spoon["capacity_origin"] == "not_applicable"
    [unsupported] = result["steps"][1]["spans"]
    assert unsupported["link"] == "unsupported"
    assert unsupported["capacity_origin"] == "not_applicable"


def test_a_failed_span_keeps_the_other_spans_steps_and_domains(monkeypatch):
    _broken_span_serializer(monkeypatch)
    result = block(
        (Ingredient("вода", "1 стакан (200 мл)"),),
        ("Влейте 1 стакан 200 мл воды.", "Добавьте 500 мл воды."),
    )

    declared = result["steps"][0]["spans"][0]
    metric = result["steps"][1]["spans"][0]
    assert declared["capacity_origin"] == "declared_text"
    assert metric["capacity_origin"] == "not_applicable"
    assert result["analysis"]["spans_emitted"] == 2
    # A conversion failure is not a cap or unsafe-fraction omission; the established
    # isolation semantics leave the status unchanged.
    assert result["analysis"]["status"] == "complete"
    assert block_ingredients(result)[0]["source_terms"][0]["capacity_origin"] == (
        "declared_text"
    )
    assert result["provenance"]["status"] == "complete"


def test_a_failed_span_whose_origin_cannot_be_built_fails_the_step_conservatively(
    monkeypatch,
):
    def broken_span(*args, **kwargs):
        raise RuntimeError("injected span serialization failure")

    def broken_origin(span, clarification):
        raise RuntimeError("capacity origin unavailable")

    monkeypatch.setattr(measure_targets, "_span_json", broken_span)
    monkeypatch.setattr(measure_targets, "_step_capacity_origin", broken_origin)
    result = block(
        (Ingredient("вода", "1 л"),),
        ("Добавьте 1 стакан воды.", "Добавьте 500 мл воды."),
    )

    assert [
        (step["step_index"], step["status"], step["spans"])
        for step in result["steps"]
    ] == [(0, "failed", []), (1, "failed", [])]
    # The exact stored text survives the failure.
    assert [step["step_text"] for step in result["steps"]] == [
        "Добавьте 1 стакан воды.",
        "Добавьте 500 мл воды.",
    ]
    assert result["analysis"]["status"] == "failed"
    assert block_ingredients(result)[0]["exact"] == {"numerator": 4, "denominator": 1}
    assert result["provenance"]["status"] == "complete"


# --- caps and determinism ---------------------------------------------------------------------


def _many_amount_step(count=39, text_unit="мл"):
    return "Добавьте " + ", ".join(f"{index} {text_unit} воды" for index in range(1, count + 1)) + "."


def test_the_per_step_cap_keeps_32_spans_and_counts_the_omissions():
    result = block((Ingredient("вода", "1 л"),), (_many_amount_step(),))
    step = result["steps"][0]

    assert len(step["spans"]) == 32
    assert step["status"] == "incomplete"
    assert result["analysis"]["spans_emitted"] == 32
    assert result["analysis"]["spans_omitted"] == 7
    assert result["analysis"]["omitted_reasons"]["limit_exceeded"] == 7
    assert result["analysis"]["status"] == "incomplete"
    # The kept prefix is the first 32 amounts, in start order.
    assert [span["text"] for span in step["spans"][:2]] == ["1 мл", "2 мл"]
    assert step["spans"][-1]["text"] == "32 мл"


def test_the_per_recipe_cap_keeps_a_deterministic_prefix_across_steps():
    step = _many_amount_step()
    result = block((Ingredient("вода", "1 л"),), (step,) * 5)

    assert [len(item["spans"]) for item in result["steps"]] == [32, 32, 32, 32]
    assert result["analysis"]["spans_emitted"] == 128
    assert result["analysis"]["omitted_reasons"]["limit_exceeded"] == 67
    assert result["analysis"]["status"] == "incomplete"


def test_the_glass_flag_sees_a_term_hidden_past_the_span_cap():
    # 32 metric amounts fill the per-step cap; the undeclared glass is the 33rd.
    text = "1 мл; " * 32 + "1 стакан воды."
    result = block((Ingredient("вода", "1 л"),), (text,))

    assert len(result["steps"][0]["spans"]) == 32
    assert result["steps"][0]["spans"][-1]["text"] == "1 мл"
    assert result["analysis"]["omitted_reasons"]["limit_exceeded"] == 1
    assert result["steps_source_glass_required"] is True


def test_the_glass_flag_ignores_a_declared_capacity_past_the_span_cap():
    text = "1 мл; " * 32 + "1 стакан 200 мл воды."
    result = block((Ingredient("вода", "1 л"),), (text,))

    assert result["steps_source_glass_required"] is False


def _compact_recipe(recipe):
    """The protocol's own compact wire form of an accepted recipe."""
    return json.dumps(
        {
            "title": recipe.title,
            "servings": recipe.servings,
            "ingredients": [
                {"name": item.name, "amount": item.amount}
                for item in recipe.ingredients
            ],
            "steps": list(recipe.steps),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _large_protocol_fixture(target_chars=19_979):
    """A complete protocol-v1 recipe whose compact wire form is exactly ``target_chars``.

    100 ingredients x 16 steps, each step a single line under the 1000-character bound.
    The whole object -- title, servings, ingredients and steps -- is validated through the
    real `parse_reply` path, so no field escapes the protocol's input bounds; the accepted
    `Recipe` is what the guarded and unguarded comparisons measure. Its mention-work bound
    is far above ``MENTION_WORK_BUDGET``, so the adapter must degrade deterministically.

    Internal padding (spaces before the final period, never at a field edge) reaches the
    exact size without changing the token stream the scanner sees.
    """
    names = [f"ингредиент{index}" for index in range(100)]
    payload = {
        "title": "Большой рецепт",
        "servings": "8 порций",
        "ingredients": [{"name": name, "amount": "1 ч. л."} for name in names],
        "steps": [
            "; ".join(
                f"хорошо перемешайте и добавьте 1 ч. л. {names[(step * 7 + slot * 3) % 100]}"
                for slot in range(16)
            )
            + "."
            for step in range(16)
        ],
    }

    def compact():
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    remaining = target_chars - compact()
    assert remaining >= 0
    for index in range(len(payload["steps"])):
        step = payload["steps"][index]
        room = MAX_STEP_LENGTH - len(step)
        take = min(room, remaining)
        if take:
            payload["steps"][index] = step[:-1] + " " * take + "."
        remaining -= take
        if remaining == 0:
            break
    block_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert remaining == 0 and len(block_text) == target_chars

    parsed = parse_reply(RECIPE_START + block_text + RECIPE_END)
    assert parsed.issues == ()
    assert len(parsed.recipes) == 1
    # The accepted recipe is the measured object: nothing was stripped or rewritten.
    assert len(_compact_recipe(parsed.recipes[0])) == target_chars
    return parsed.recipes[0], block_text


def test_a_pathological_protocol_valid_recipe_degrades_deterministically():
    import time

    recipe, block_text = _large_protocol_fixture()
    assert len(block_text) == 19_979
    assert len(block_text) <= MAX_RECIPE_BLOCK_LENGTH
    # The fixture is exactly what the protocol accepts, not a manually shaped object.
    parsed = parse_reply(RECIPE_START + block_text + RECIPE_END)
    assert parsed.issues == ()
    assert parsed.recipes == (recipe,)
    assert measure_targets.MENTION_WORK_BUDGET == 10_000

    with _counting_same_word() as guarded_calls:
        started = time.perf_counter()
        first = block(recipe.ingredients, recipe.steps)
        elapsed = time.perf_counter() - started
    # The degraded step does no mention matching at all, not even a partial pass.
    assert guarded_calls == []

    # The primary assertion is the deterministic degraded shape, not a clock reading.
    assert first["steps"] == []
    assert first["analysis"]["status"] == "incomplete"
    assert first["analysis"]["spans_emitted"] == 0
    assert first["analysis"]["spans_omitted"] > 0
    assert (
        first["analysis"]["omitted_reasons"]["limit_exceeded"]
        == first["analysis"]["spans_omitted"]
    )
    # Ingredient hints, provenance and the original data survive.
    assert len(first["ingredients"]) == 100
    assert first["provenance"]["status"] == "complete"
    assert first["analysis"]["omitted_reasons"]["malformed"] == 0
    # A very generous secondary bound: the unguarded scanner took seconds here.
    assert elapsed < 2.0

    assert block(recipe.ingredients, recipe.steps) == first

    # The same accepted recipe, unguarded: the bound exists because the real matching
    # cost exceeds it, not to make a benchmark pass.
    with _counting_same_word() as unguarded_calls:
        unguarded = step_scan_module.scan_recipe_steps(
            recipe.steps, recipe.ingredients
        )
    assert len(unguarded.steps) == 16
    # The unguarded pass binds the whole recipe up to the 128-span cap; the cost measured
    # here is the matching it does, not the spans it later drops.
    assert sum(len(step.amounts) for step in unguarded.steps) == (
        step_scan_module.MAX_SPANS_PER_RECIPE
    )
    assert len(unguarded_calls) > measure_targets.MENTION_WORK_BUDGET


# Codex's second-audit F1 reproduction, kept verbatim in structure: twenty ingredients
# whose thirty-two indistinguishable modifiers all share one head, then four steps with a
# hundred-word modifier run immediately before that head.
_CODEX_F1_NAMES = tuple("сухой " * 32 + "вода" for _ in range(20))
_CODEX_F1_STEPS = tuple("горячей " * 100 + "сухой вода, 1 мл." for _ in range(4))


def _codex_f1_recipe():
    """The F1 fixture as a complete protocol-v1 object, accepted by the real parser."""
    payload = {
        "title": "Т",
        "servings": "2 порции",
        "ingredients": [
            {"name": name, "amount": "1 л"} for name in _CODEX_F1_NAMES
        ],
        "steps": list(_CODEX_F1_STEPS),
    }
    block_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    parsed = parse_reply(RECIPE_START + block_text + RECIPE_END)
    assert parsed.issues == ()
    assert len(parsed.recipes) == 1
    return parsed.recipes[0], block_text


def _mention_work_total(ingredients, steps):
    infos = step_scan_module._ingredient_infos([item.name for item in ingredients])
    total = 0
    for text in steps:
        _, words = step_scan_module._tokenize(text)
        total += step_scan_module._mention_work(text, tuple(words), infos)
    return total


def _word_ingredient_estimate(ingredients, steps):
    """The guard the second audit rejected: words x ingredients only."""
    total = 0
    for text in steps:
        _, words = step_scan_module._tokenize(text)
        total += len(words) * len(ingredients)
    return total


def test_the_nested_modifier_fixture_is_charged_and_degrades_honestly():
    recipe, block_text = _codex_f1_recipe()
    # The audit reported 7,799 characters; the two-character difference is only in fields
    # it did not state (title and servings). The adversarial structure and every cost it
    # causes are reproduced exactly: 8,240 old-estimate units and 258,640 comparisons.
    assert len(block_text) == 7_801
    ingredients, steps = recipe.ingredients, recipe.steps

    # The rejected guard admitted this recipe: 8,240 < 10,000, so it reported `complete`.
    assert _word_ingredient_estimate(ingredients, steps) == 8_240
    assert _word_ingredient_estimate(ingredients, steps) < (
        measure_targets.MENTION_WORK_BUDGET
    )
    # The real cost is the nested modifier comparison, and the new bound covers it.
    with _counting_same_word() as unguarded_calls:
        unguarded = step_scan_module.scan_recipe_steps(steps, ingredients)
    assert len(unguarded.steps) == 4 and unguarded.omitted == 0
    assert len(unguarded_calls) == 258_640
    assert _mention_work_total(ingredients, steps) >= len(unguarded_calls)
    assert _mention_work_total(ingredients, steps) > (
        measure_targets.MENTION_WORK_BUDGET
    )

    with _counting_same_word() as guarded_calls:
        result = block(ingredients, steps)
    # No matching is performed once the bound is known to be over budget.
    assert guarded_calls == []

    assert result["steps"] == []
    assert result["analysis"]["status"] == "incomplete"
    assert result["analysis"]["spans_emitted"] == 0
    assert result["analysis"]["spans_omitted"] == 4
    assert result["analysis"]["omitted_reasons"] == {
        "limit_exceeded": 4,
        "malformed": 0,
        "unsafe_fraction": 0,
    }
    # The recipe, its ingredient conversions and its provenance are untouched.
    assert len(result["ingredients"]) == 20
    assert result["ingredients"][0]["exact"] == {"numerator": 4, "denominator": 1}
    assert result["provenance"]["status"] == "complete"
    assert block(ingredients, steps) == result


def test_the_glass_flag_survives_the_work_guard():
    ingredients = tuple(
        Ingredient(f"ингредиент{index}", "1 л") for index in range(100)
    )
    # 125 words x 100 ingredients = 12,500 head comparisons, over the 10,000 budget.
    step = "мешаем " * 120 + "добавьте 1 стакан воды."
    result = block(ingredients, (step,))

    # The mention work is over budget, so no span is emitted ...
    assert result["steps"] == []
    assert result["analysis"]["status"] == "incomplete"
    # ... but the lightweight expression pass still sees the undeclared glass.
    assert result["steps_source_glass_required"] is True


def test_the_work_guard_keeps_an_ordinary_recipe_fully_annotated():
    ingredients = tuple(
        Ingredient(name, "1 л")
        for name in ("вода", "молоко", "мука", "сахар", "соль", "масло")
    )
    steps = (
        "Влейте 500 мл воды и добавьте 200 мл молока.",
        "Добавьте 100 г муки, 2 ст. л. сахара и 1 ч. л. соли.",
        "Растопите 50 г масла и перемешайте.",
    )
    result = block(ingredients, steps)

    assert result["analysis"]["status"] == "complete"
    assert result["analysis"]["omitted_reasons"]["limit_exceeded"] == 0
    assert result["analysis"]["spans_emitted"] == 6


def test_an_oversized_or_non_string_step_is_omitted_without_losing_the_others():
    good = "Добавьте 500 мл воды."
    oversized = "1 мл " * 300  # more than the protocol's 1000-character bound
    result = block((Ingredient("вода", "1 л"),), (good, oversized, 42))

    assert [step["step_index"] for step in result["steps"]] == [0]
    assert result["steps"][0]["spans"][0]["conversion"] is not None
    assert result["analysis"]["omitted_reasons"]["malformed"] == 2
    assert result["analysis"]["status"] == "incomplete"


def test_repeated_identical_requests_produce_an_identical_block():
    ingredients = (Ingredient("вода", "1 стакан (200 мл)"), Ingredient("молоко", "1 стакан"))
    steps = ("Добавьте 1 стакан воды.", "Влейте 500 мл молока.")

    first = block(ingredients, steps, source_glass_ml=250)
    second = block(ingredients, steps, source_glass_ml=250)

    assert first == second


# --- helpers ----------------------------------------------------------------------------------


def block_ingredients(result):
    return [result["ingredients"][index] for index in sorted(by_position(result["ingredients"]))]
