"""KH-F01-B1 / B1.1-C: present one recipe's amounts in a person's own household measures.

This is the adapter between the accepted KH-F01-B0 calculation core (core.measures) and
the Web layer. It is pure: it reads no database, calls no provider, mutates nothing, and
returns plain values. The core's arithmetic is used exactly as accepted -- exact
Fractions, the 5% practical-rounding gate, the core statuses and reasons -- and this
module adds no arithmetic of its own.

KH-F01-B1.1-C (`measure_block_v2`) composes the accepted stage-A provenance audit and
stage-B step scanner into the additive `schema_version: 2` block of 15.8.2. It adds no
second scanner, binder, morphology or arithmetic engine: it calls the accepted components
and only serializes and confines their results. Capacity is resolved per source term with
no inheritance (OD-03): a declaration in the term's own text wins, a temporary
clarification applies only to terms that lack one, and everything else is `unresolved`.
The v2 block also adds step annotations bound to `favorite_version`, per-term
`capacity_origin`, safe-rational serialization and independent failure domains.

The stage-B scanner now records the exact recognized approximation word
(`около`, `примерно`, `приблизительно`) and the exact validated quantity of a span
(including a declared capacity such as `237½` or `500/3`). The adapter serializes that
word and converts that quantity directly; it never re-parses the span text, which a
narrower parser would misread.

Target context. Only the signed-in person's capacities are used, and only as target
measures: they never resolve, reinterpret or override a source measure. A missing
personal value was already resolved to the system default (250/5/15 mL) by
resolve_target_measures; this module never reads an override from anywhere else.

Source context. A glass capacity written in the recipe is used exactly, including a
nonstandard or fractional one (the parser's opt-in mode is always enabled here). An
undeclared source glass is never defaulted: without a clarification the ingredient is
reported as clarification_required, and with one the clarification is applied ONLY to the
undeclared glass terms of that amount, as a temporary immutable Quantity handed to the
core. Declared terms keep their own capacity, so a recipe that mixes `1 стакан` with
`1 стакан 240 мл` cannot produce an artificial conflict; the original parsed quantity and
the ingredient text are never changed. (Passing one blanket source_measures context would
turn a declared 240 mL into a conflict with a 200 mL clarification, which is exactly the
behavior the owner rejected; this adapter therefore passes none and rewrites only the
terms that lack a capacity.)

What is presented:
  * a household amount (glass, teaspoon, tablespoon, or a mixture of them) is converted
    into its largest present unit -- glass before tablespoon before teaspoon -- with the
    person's capacity for that unit;
  * a metric volume (mL or L) is offered as the person's glass only when the practical
    rounding gate passes; otherwise no hint is presented at all, and the original metric
    amount is left as it is;
  * mass is not converted: B1 authorizes no ingredient coefficient, so grams and
    kilograms are not turned into spoons or glasses;
  * an amount identical to its source presentation is not repeated;
  * when rounding changed the exact value the presentation is "rounded" (the page marks
    it as approximate); when a household amount has no practical rounded value the exact
    rational is presented instead ("exact"); nothing else is presented.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Optional, Sequence, Union

from house_helper.core.measures import (
    ConversionResult,
    Dimension,
    MeasureVolumes,
    Quantity,
    QuantityPart,
    RoundedAmount,
    Unit,
    convert_quantity,
    parse_quantity,
    resolve_target_measures,
)
from house_helper.core.provenance import (
    ProvenanceEntry,
    TermProvenanceStatus,
    audit_source_provenance,
)
from house_helper.core.recipes import MAX_STEP_LENGTH, MAX_STEPS
from house_helper.core.step_scan import (
    MAX_SPANS_PER_RECIPE,
    MAX_SPANS_PER_STEP,
    AmountLinkStatus,
    StepAmount,
    scan_recipe_steps,
)

# The presentation the page should draw for one ingredient's conversion.
PRESENTATION_ROUNDED = "rounded"  # the practical rounded value
PRESENTATION_EXACT = "exact"  # the exact rational (no practical value passed the gate)
PRESENTATION_NONE = "none"  # nothing is shown for this ingredient

# Which source family the amount belongs to. The distinction matters for the metric
# presentation rule (a metric hint is shown only when it is practically usable).
SOURCE_HOUSEHOLD = "household"
SOURCE_METRIC_VOLUME = "metric_volume"

_HOUSEHOLD_UNITS = frozenset({Unit.GLASS, Unit.TEASPOON, Unit.TABLESPOON})


def volumes(
    glass_ml: Optional[int] = None,
    teaspoon_ml: Optional[int] = None,
    tablespoon_ml: Optional[int] = None,
) -> MeasureVolumes:
    """A target context from the person's stored overrides, defaults resolved.

    The stored values are whole millilitres (the profile API validates them); an
    out-of-range value is refused by MeasureVolumes rather than clamped.
    """
    return resolve_target_measures(
        MeasureVolumes(
            glass_ml=glass_ml, teaspoon_ml=teaspoon_ml, tablespoon_ml=tablespoon_ml
        )
    )


@dataclass(frozen=True)
class IngredientConversion:
    """One ingredient amount, as the page may present it.

    `exact_amount` is the core's unrounded Fraction and `rounded` its practical
    presentation (or None when none passed the 5% gate). `identical` says the converted
    amount is exactly the source amount in the same unit, so there is nothing new to
    show. `presentation` is the page's instruction, computed here so the metric rule
    lives in one place.
    """

    position: int
    source: str
    source_kind: str
    target_unit: Unit
    status: str
    reason: str
    exact_amount: Optional[Fraction]
    rounded: Optional[RoundedAmount]
    identical: bool
    presentation: str
    source_quantity: Quantity


def _source_kind(units: set[Unit]) -> str:
    if units & _HOUSEHOLD_UNITS:
        return SOURCE_HOUSEHOLD
    return SOURCE_METRIC_VOLUME


def _target_unit(units: set[Unit]) -> Unit:
    """The largest household unit present, or the glass for a purely metric amount."""
    if Unit.GLASS in units:
        return Unit.GLASS
    if Unit.TABLESPOON in units:
        return Unit.TABLESPOON
    if Unit.TEASPOON in units:
        return Unit.TEASPOON
    return Unit.GLASS


def _has_undeclared_glass(quantity: Quantity) -> bool:
    return any(
        part.unit is Unit.GLASS and part.glass_ml is None for part in quantity.parts
    )


def _with_source_glass(
    quantity: Quantity, source_glass_ml: Union[int, Fraction]
) -> Quantity:
    """A temporary copy with the clarification filled into the undeclared glass terms.

    Declared terms keep their own capacity. The original Quantity is never changed: the
    caller keeps the parsed source exactly as the parser returned it.
    """
    parts = tuple(
        QuantityPart(
            amount=part.amount,
            unit=part.unit,
            glass_ml=(
                source_glass_ml
                if part.unit is Unit.GLASS and part.glass_ml is None
                else part.glass_ml
            ),
        )
        for part in quantity.parts
    )
    return Quantity(
        text=quantity.text,
        status=quantity.status,
        reason=quantity.reason,
        dimension=quantity.dimension,
        parts=parts,
    )


def _checked_source_glass(value: Union[int, Fraction]) -> Union[int, Fraction]:
    """The clarification as an exact capacity, validated by the core's own fence."""
    return MeasureVolumes(glass_ml=value).glass_ml


def _failure_conversion(
    position: int, quantity: Quantity, result: ConversionResult, source_kind: str
) -> IngredientConversion:
    unit = _target_unit({part.unit for part in quantity.parts})
    return IngredientConversion(
        position=position,
        source=quantity.text,
        source_kind=source_kind,
        target_unit=unit,
        status=result.status.value,
        reason=result.reason.value,
        exact_amount=None,
        rounded=None,
        identical=False,
        presentation=PRESENTATION_NONE,
        source_quantity=quantity,
    )


def convert_ingredient(
    position: int,
    amount: str,
    target: MeasureVolumes,
    *,
    source_glass_ml: Optional[Union[int, Fraction]] = None,
) -> Optional[IngredientConversion]:
    """One ingredient amount in the person's measures, or None when none applies.

    None means the amount is not a quantified volume this increment presents: a
    qualitative, ranged, ambiguous, unsupported or malformed amount, or a mass. A
    quantified household or metric volume always yields a conversion (a successful one,
    or one whose status says a source glass capacity is still required).

    `source_glass_ml` applies to undeclared glass terms only. It is the temporary
    calculation context, never a personal target value and never persisted.
    """
    if not isinstance(target, MeasureVolumes):
        raise TypeError("target must be a MeasureVolumes")
    quantity = parse_quantity(amount, allow_nonstandard_glass_sizes=True)
    return _conversion_from_quantity(
        position, quantity, target, source_glass_ml=source_glass_ml
    )


def _conversion_from_quantity(
    position: int,
    quantity: Quantity,
    target: MeasureVolumes,
    *,
    source_glass_ml: Optional[Union[int, Fraction]] = None,
) -> Optional[IngredientConversion]:
    """Convert one already-parsed quantity, never re-parsing its text.

    A step span carries the exact quantity the accepted scanner validated, including a
    declared capacity the narrower B1 parser cannot spell (`237½`, `500/3`). Re-parsing
    the span text here would lose that capacity, so the adapter converts the scanner's
    quantity directly. An ingredient amount is parsed by `convert_ingredient` first.
    """
    if not quantity.is_quantified or quantity.dimension is None:
        return None
    if quantity.dimension is not Dimension.VOLUME:
        return None
    units = {part.unit for part in quantity.parts}
    source_kind = _source_kind(units)
    target_unit = _target_unit(units)

    calculation = quantity
    if source_glass_ml is not None:
        calculation = _with_source_glass(quantity, _checked_source_glass(source_glass_ml))

    result = convert_quantity(calculation, target_unit, target_measures=target)
    if not result.is_success:
        return _failure_conversion(position, quantity, result, source_kind)

    exact = result.exact_amount
    if exact is None:  # (not reachable for a successful conversion)
        raise RuntimeError("a successful conversion must carry an exact amount")
    identical = (
        len(quantity.parts) == 1
        and quantity.parts[0].unit is target_unit
        and exact == quantity.parts[0].amount
    )
    if identical:
        presentation = PRESENTATION_NONE
    elif result.rounded is not None:
        presentation = PRESENTATION_ROUNDED
    elif source_kind == SOURCE_HOUSEHOLD:
        presentation = PRESENTATION_EXACT
    else:
        presentation = PRESENTATION_NONE
    return IngredientConversion(
        position=position,
        source=quantity.text,
        source_kind=source_kind,
        target_unit=target_unit,
        status=result.status.value,
        reason=result.reason.value,
        exact_amount=exact,
        rounded=result.rounded,
        identical=identical,
        presentation=presentation,
        source_quantity=quantity,
    )


def _fraction_json(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def conversion_json(conversion: IngredientConversion) -> dict[str, Any]:
    """One conversion as machine-readable JSON: exact integers, never floats."""
    rounded: Optional[dict[str, Any]] = None
    if conversion.rounded is not None:
        rounded = {
            **_fraction_json(conversion.rounded.amount),
            "relative_error": _fraction_json(conversion.rounded.relative_error),
        }
    return {
        "position": conversion.position,
        "source": conversion.source,
        "source_kind": conversion.source_kind,
        "target_unit": conversion.target_unit.value,
        "status": conversion.status,
        "reason": conversion.reason,
        "exact": (
            None
            if conversion.exact_amount is None
            else _fraction_json(conversion.exact_amount)
        ),
        "rounded": rounded,
        "identical": conversion.identical,
        "presentation": conversion.presentation,
    }


def measure_block(
    amounts: Sequence[str],
    target: MeasureVolumes,
    *,
    source_glass_ml: Optional[Union[int, Fraction]] = None,
) -> dict[str, Any]:
    """The additive `measures` block of a favorite detail answer.

    `amounts` are the ingredient amounts in their stored order; positions are their
    indexes. Only quantified volume amounts appear. Escaping the API as integers keeps
    the exact rational readable to a client without a float ever standing for it.
    """
    if not isinstance(target, MeasureVolumes):
        raise TypeError("target must be a MeasureVolumes")
    checked_glass = (
        None if source_glass_ml is None else _checked_source_glass(source_glass_ml)
    )
    conversions: list[IngredientConversion] = []
    for position, amount in enumerate(amounts):
        conversion = convert_ingredient(
            position, amount, target, source_glass_ml=checked_glass
        )
        if conversion is not None:
            conversions.append(conversion)
    return {
        "target": {
            "glass_ml": _whole(target.glass_ml),
            "teaspoon_ml": _whole(target.teaspoon_ml),
            "tablespoon_ml": _whole(target.tablespoon_ml),
        },
        "source_glass_ml": None if checked_glass is None else _whole(checked_glass),
        "source_glass_required": any(
            _has_undeclared_glass(conversion.source_quantity)
            for conversion in conversions
        ),
        "ingredients": [conversion_json(item) for item in conversions],
    }


def _whole(value: Union[int, Fraction, None]) -> int:
    if value is None:  # (a resolved target never has an empty field)
        raise ValueError("a resolved target measure must have a value")
    fraction = Fraction(value)
    if fraction.denominator != 1:
        raise ValueError("a target measure must be a whole number of millilitres")
    return int(fraction)


# --- KH-F01-B1.1-C: the typed schema_version 2 annotation block ------------------------
#
# The block is a composition of already accepted components: the stage-A provenance audit,
# the stage-B step scanner and binder, and the B0/B1 parse/convert path used above. This
# section serializes their results, resolves each source term's capacity origin with no
# inheritance (OD-03), keeps every fraction exact and confined to JavaScript's safe
# integer range, and isolates failures so one broken domain never blanks another.

SCHEMA_VERSION = 2

# The capacity origins of 15.8.2. `inherited_from_ingredient` deliberately does not exist.
CAPACITY_DECLARED_TEXT = "declared_text"
CAPACITY_TEMPORARY_CLARIFICATION = "temporary_clarification"
CAPACITY_NOT_APPLICABLE = "not_applicable"
CAPACITY_UNRESOLVED = "unresolved"

STEP_COMPLETE = "complete"
STEP_INCOMPLETE = "incomplete"
STEP_FAILED = "failed"

PROVENANCE_INCOMPLETE = "incomplete"
PROVENANCE_FAILED = "failed"

ANALYSIS_COMPLETE = "complete"
ANALYSIS_INCOMPLETE = "incomplete"
ANALYSIS_FAILED = "failed"

# Why a span carries no `conversion`; the closed enum of 15.8.2.
OMITTED_MASS = "mass"
OMITTED_UNSAFE_FRACTION = "unsafe_fraction"
OMITTED_CONVERSION_FAILED = "conversion_failed"

# JavaScript's exact-integer bound: 0 .. 2**53 - 1. A fraction outside it is omitted,
# never rounded into a representable one.
SAFE_INTEGER_MAX = 2**53 - 1

# Deterministic preflight bound on step ingredient-mention matching, in word-comparison
# units: the per-word head lookups plus the nested modifier comparisons a specific
# ingredient's distinguishing words cause. An ordinary recipe -- a handful of steps and
# tens of ingredients -- is many times below it, so its hints are always kept; a
# pathological but protocol-valid recipe degrades to counted omissions instead of delaying
# the favorite. The outcome depends only on the stored text and the ingredient list, never
# on a clock.
MENTION_WORK_BUDGET = 10_000


class UnsafeFraction(ValueError):
    """A rational cannot be serialized as two exact JavaScript-safe integers."""


def _safe_fraction_json(value: Union[int, Fraction]) -> dict[str, int]:
    """A reduced fraction with a positive denominator inside the safe-integer bound.

    `Fraction` already normalizes the sign and reduces, so `denominator` is always
    positive and the two values are coprime. A negative numerator or either component
    above 2**53 - 1 is refused; no float or rounded value ever stands in for a quantity.
    """
    fraction = Fraction(value)
    numerator, denominator = fraction.numerator, fraction.denominator
    if not 0 <= numerator <= SAFE_INTEGER_MAX or not 0 <= denominator <= SAFE_INTEGER_MAX:
        raise UnsafeFraction("a rational outside the safe-integer range")
    return {"numerator": numerator, "denominator": denominator}


def _safe_rounded_json(rounded: Optional[RoundedAmount]) -> Optional[dict[str, Any]]:
    if rounded is None:
        return None
    return {
        **_safe_fraction_json(rounded.amount),
        "relative_error": _safe_fraction_json(rounded.relative_error),
    }


def _conversion_values_json(conversion: IngredientConversion) -> dict[str, Any]:
    """The `ConversionJson` fields shared by an ingredient and a step span (15.8.2)."""
    return {
        "target_unit": conversion.target_unit.value,
        "status": conversion.status,
        "reason": conversion.reason,
        "exact": (
            None
            if conversion.exact_amount is None
            else _safe_fraction_json(conversion.exact_amount)
        ),
        "rounded": _safe_rounded_json(conversion.rounded),
        "identical": conversion.identical,
        "presentation": conversion.presentation,
    }


def _ingredient_conversion_v2(
    conversion: IngredientConversion, clarification: Optional[Union[int, Fraction]]
) -> dict[str, Any]:
    """One released B1 ingredient object, extended with its required per-term data.

    `source_terms` comes from the original parsed quantity, never from a clarified copy:
    a term that declares its own capacity keeps `declared_text`, an undeclared glass term
    reports `temporary_clarification` when one was supplied for this request and
    `unresolved` otherwise, and a spoon or metric term is `not_applicable`.
    """
    terms: list[dict[str, Any]] = []
    for part in conversion.source_quantity.parts:
        term: dict[str, Any] = {
            "unit": part.unit.value,
            "amount": _safe_fraction_json(part.amount),
            "capacity_ml": None,
            "capacity_origin": CAPACITY_NOT_APPLICABLE,
        }
        if part.unit is Unit.GLASS:
            if part.glass_ml is not None:
                term["capacity_ml"] = _safe_fraction_json(part.glass_ml)
                term["capacity_origin"] = CAPACITY_DECLARED_TEXT
            elif clarification is not None:
                term["capacity_ml"] = _safe_fraction_json(clarification)
                term["capacity_origin"] = CAPACITY_TEMPORARY_CLARIFICATION
            else:
                term["capacity_origin"] = CAPACITY_UNRESOLVED
        terms.append(term)
    return {
        "position": conversion.position,
        "source": conversion.source,
        "source_kind": conversion.source_kind,
        "target_unit": conversion.target_unit.value,
        "status": conversion.status,
        "reason": conversion.reason,
        "exact": (
            None
            if conversion.exact_amount is None
            else _safe_fraction_json(conversion.exact_amount)
        ),
        "rounded": _safe_rounded_json(conversion.rounded),
        "identical": conversion.identical,
        "presentation": conversion.presentation,
        "source_terms": terms,
    }


def _provenance_entry_json(entry: ProvenanceEntry) -> dict[str, Any]:
    content: Optional[dict[str, Any]] = None
    if entry.declared_content is not None:
        content = {
            "amount": _safe_fraction_json(entry.declared_content.amount),
            "unit": entry.declared_content.unit.value,
        }
    return {
        "ingredient_position": entry.ingredient_position,
        "term_index": entry.term_index,
        "kind": entry.kind.value,
        "status": entry.status.value,
        "capacity_ml": (
            None if entry.capacity_ml is None else _safe_fraction_json(entry.capacity_ml)
        ),
        "declared_content": content,
        "origin": entry.origin.value,
        "reason": entry.reason,
    }


@dataclass
class _Omissions:
    """Counters for the three `omitted_reasons` of 15.8.2."""

    limit_exceeded: int = 0
    malformed: int = 0
    unsafe_fraction: int = 0


def _ingredient_amount(item: object) -> str:
    """The stored amount of an ingredient object, mapping or plain string."""
    if isinstance(item, str):
        return item
    amount = getattr(item, "amount", None)
    if isinstance(amount, str):
        return amount
    if isinstance(item, Mapping) and isinstance(item.get("amount"), str):
        return item["amount"]
    raise TypeError("every ingredient must carry a string amount")


def _ingredient_annotations(
    ingredients: Sequence[object],
    target: MeasureVolumes,
    clarification: Optional[Union[int, Fraction]],
    omissions: _Omissions,
) -> tuple[list[dict[str, Any]], bool]:
    """Every quantified volume ingredient, in stored order, and the B1 glass flag.

    The flag keeps its released meaning: it is evaluated on the original parsed amounts,
    before the temporary clarification, and includes an ingredient whose conversion is
    later omitted as unsafe. A conversion that raises unexpectedly propagates: `measures`
    may then be null, exactly as in B1 (15.8.2, 15.6.4).
    """
    conversions: list[IngredientConversion] = []
    for position, item in enumerate(ingredients):
        conversion = convert_ingredient(
            position,
            _ingredient_amount(item),
            target,
            source_glass_ml=clarification,
        )
        if conversion is not None:
            conversions.append(conversion)
    required = any(
        _has_undeclared_glass(conversion.source_quantity)
        for conversion in conversions
    )
    payload: list[dict[str, Any]] = []
    for conversion in conversions:
        try:
            payload.append(_ingredient_conversion_v2(conversion, clarification))
        except UnsafeFraction:
            # The ingredient is omitted; the source amount and every other hint stay.
            omissions.unsafe_fraction += 1
    return payload, required


def _provenance_block(
    ingredients: Sequence[object], omissions: _Omissions
) -> dict[str, Any]:
    """The stage-A audit, split into `declared` and `incomplete`, or a failed shell.

    Audit-internal `not_applicable` terms are never emitted, so they never make the status
    incomplete. An audit exception is confined here: only this section is lost. A
    serialization exception is confined the same way: an unsafe fraction omits only its
    own entry, and any other unexpected serialization error marks the section failed
    without touching the ingredient or step domains.
    """
    try:
        audit = audit_source_provenance(ingredients)
    except Exception:
        return {
            "status": PROVENANCE_FAILED,
            "declared": [],
            "incomplete": [],
        }
    declared: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    unsafe = 0
    try:
        for entry in audit.entries:
            if entry.status is TermProvenanceStatus.NOT_APPLICABLE:
                continue
            try:
                payload = _provenance_entry_json(entry)
            except UnsafeFraction:
                unsafe += 1
                continue
            if entry.status is TermProvenanceStatus.DECLARED:
                declared.append(payload)
            else:
                incomplete.append(payload)
    except Exception:
        # A serialization failure is a provenance-domain failure: keep the other domains
        # intact rather than letting it collapse the whole measures block.
        return {
            "status": PROVENANCE_FAILED,
            "declared": [],
            "incomplete": [],
        }
    omissions.unsafe_fraction += unsafe
    return {
        "status": audit.status.value,
        "declared": declared,
        "incomplete": incomplete,
    }


def _step_capacity_origin(
    quantity: Quantity, clarification: Optional[Union[int, Fraction]]
) -> str:
    """The per-term origin of a step span (15.8.2, OD-03).

    A spoon or metric term needs no glass capacity. A glass term reports `declared_text`
    when it declares its own capacity, `temporary_clarification` when the request supplied
    one for an undeclared term, and `unresolved` otherwise. No linked ingredient is ever
    consulted.
    """
    if not quantity.is_quantified or quantity.dimension is not Dimension.VOLUME:
        return CAPACITY_NOT_APPLICABLE
    glasses = [part for part in quantity.parts if part.unit is Unit.GLASS]
    if not glasses:
        return CAPACITY_NOT_APPLICABLE
    if any(part.glass_ml is None for part in glasses):
        return (
            CAPACITY_TEMPORARY_CLARIFICATION
            if clarification is not None
            else CAPACITY_UNRESOLVED
        )
    return CAPACITY_DECLARED_TEXT


def _span_capacity_origin(
    span: StepAmount, clarification: Optional[Union[int, Fraction]]
) -> str:
    """The capacity origin a span reports, from the scanner's own quantity (OD-03).

    It is shared by the normal serializer and by the failure fallback, so a span whose
    conversion fails keeps the origin the scanner established: `declared_text` when the
    source term declares its own capacity, `temporary_clarification` when the request
    supplied one, `unresolved` otherwise, and `not_applicable` for a rejected construct or
    a metric/spoon term. It never consults a linked ingredient and never guesses.
    """
    if span.status is AmountLinkStatus.UNSUPPORTED or span.quantity is None:
        return CAPACITY_NOT_APPLICABLE
    return _step_capacity_origin(span.quantity, clarification)


def _linked_span_conversion(
    quantity: Optional[Quantity],
    target: MeasureVolumes,
    clarification: Optional[Union[int, Fraction]],
    omissions: _Omissions,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """The `conversion` and `conversion_omitted_reason` of a linked span.

    A handled failure (for example an undeclared source glass) is still a ConversionJson;
    only a mass, an unsafe fraction or an unexpected exception yields null. A mass amount
    is linked but has no B1.1 conversion: B1.1 performs no mass-to-volume conversion (the
    ingredient coefficient is B2's).
    """
    if quantity is None:
        return None, OMITTED_CONVERSION_FAILED
    if quantity.dimension is Dimension.MASS:
        return None, OMITTED_MASS
    if not quantity.is_quantified or quantity.dimension is not Dimension.VOLUME:
        return None, OMITTED_CONVERSION_FAILED
    try:
        # The span's exact scanner quantity is converted directly; re-parsing its text
        # would lose a declared capacity the narrower B1 parser cannot spell.
        conversion = _conversion_from_quantity(
            0, quantity, target, source_glass_ml=clarification
        )
    except Exception:
        return None, OMITTED_CONVERSION_FAILED
    if conversion is None:
        return None, OMITTED_CONVERSION_FAILED
    try:
        return _conversion_values_json(conversion), None
    except UnsafeFraction:
        omissions.unsafe_fraction += 1
        return None, OMITTED_UNSAFE_FRACTION


def _span_json(
    span: StepAmount,
    target: MeasureVolumes,
    clarification: Optional[Union[int, Fraction]],
    omissions: _Omissions,
) -> dict[str, Any]:
    """One `SpanAnnotation`.

    Only a supported span is converted: a rejected construct (range, US cup, package,
    countable, heaped, malformed) never is, and neither is an unresolved, ambiguous or
    shared amount (no number without a link). The glass flag is evaluated by the scanner
    over every expression, including omitted ones, so this serializer does not compute it.
    """
    # The scanner already validated this expression; its exact quantity is used as is, so
    # a declared fractional capacity (`237½`, `500/3`) survives the B1 boundary.
    quantity = span.quantity

    conversion: Optional[dict[str, Any]] = None
    omitted: Optional[str] = None
    origin = _span_capacity_origin(span, clarification)
    if span.status is not AmountLinkStatus.UNSUPPORTED:
        if span.status is AmountLinkStatus.LINKED:
            conversion, omitted = _linked_span_conversion(
                quantity, target, clarification, omissions
            )
    return {
        "start": span.start,
        "end": span.end,
        "text": span.text,
        "ingredient_position": span.ingredient_position,
        "link": span.status.value,
        "link_reason": span.reason.value,
        "conversion": conversion,
        "conversion_omitted_reason": omitted,
        "capacity_origin": origin,
        # The scanner records the exact recognized word (`около`/`примерно`/
        # `приблизительно`); the adapter only serializes it.
        "source_approximation": {
            "approximate": span.approximate_word is not None,
            "word": span.approximate_word,
        },
    }


def _failed_span_json(
    span: StepAmount, clarification: Optional[Union[int, Fraction]]
) -> dict[str, Any]:
    """A span whose conversion raised unexpectedly: no number, everything else intact.

    The scanner's own per-term capacity metadata survives the failure: the origin is
    computed by the same `_span_capacity_origin` the normal serializer uses, so a declared
    (`1 стакан 200 мл`), clarified or unresolved glass keeps the origin it established
    rather than being flattened to `not_applicable`. Nothing is invented: if that metadata
    cannot be built, this function raises and the caller fails the step instead.
    """
    return {
        "start": span.start,
        "end": span.end,
        "text": span.text,
        "ingredient_position": span.ingredient_position,
        "link": span.status.value,
        "link_reason": span.reason.value,
        "conversion": None,
        "conversion_omitted_reason": OMITTED_CONVERSION_FAILED,
        "capacity_origin": _span_capacity_origin(span, clarification),
        "source_approximation": {
            "approximate": span.approximate_word is not None,
            "word": span.approximate_word,
        },
    }


def _step_json(
    step_index: int, text: str, spans: list[dict[str, Any]], status: str
) -> dict[str, Any]:
    return {
        "step_index": step_index,
        "step_text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "status": status,
        "spans": spans,
    }


def _failed_step_json(step_index: int, text: str) -> dict[str, Any]:
    """A step whose serialization raised: exact text and hash, no annotations.

    This builder deliberately does not call `_step_json`, so a failure in that serializer
    cannot take the step, or the whole `measures` block, down with it.
    """
    return {
        "step_index": step_index,
        "step_text": text,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "status": STEP_FAILED,
        "spans": [],
    }


def _scan_steps(
    steps: Sequence[str],
    ingredients: Sequence[object],
    omissions: _Omissions,
) -> Optional[tuple[list[tuple[int, str, tuple[StepAmount, ...], int]], bool]]:
    """Scan the bounded steps with the accepted whole-recipe scanner.

    The scanner applies both span caps as a deterministic step/start prefix and, through
    `MENTION_WORK_BUDGET`, a deterministic upper bound on ingredient mention matching
    (head lookups and nested modifier comparisons); a step it declines is counted as an
    omitted span. A step that is not a
    string, is longer than the protocol bound or lies beyond the protocol's step count is
    omitted and counted as malformed: protocol validation bounds them already, so only a
    corrupt row can reach that branch.

    If the whole-recipe scan raises, the failure is the step domain's failure: the result
    is ``None`` and the caller emits ``steps: []`` with ``analysis.status = "failed"``.
    There is deliberately no per-step retry, which would repeat expensive work and could
    hide the original failure behind a partial "complete" pass.

    The result is ``(entries, glass_required)``; each entry is
    ``(original_step_index, text, amounts, omitted)``. The glass flag comes from the
    scanner's whole-expression evaluation, so it survives the caps and the work guard.
    """
    bounded: list[tuple[int, str]] = []
    for index, text in enumerate(steps):
        if (
            index >= MAX_STEPS
            or not isinstance(text, str)
            or len(text) > MAX_STEP_LENGTH
        ):
            omissions.malformed += 1
            continue
        bounded.append((index, text))
    if not bounded:
        return [], False
    texts = tuple(text for _, text in bounded)
    try:
        scanned = scan_recipe_steps(
            texts, ingredients, max_mention_work=MENTION_WORK_BUDGET
        )
    except Exception:
        return None
    entries: list[tuple[int, str, tuple[StepAmount, ...], int]] = []
    for (index, text), step in zip(bounded, scanned.steps):
        omissions.limit_exceeded += step.omitted
        entries.append((index, text, step.amounts, step.omitted))
    return entries, scanned.glass_required


def _step_annotations(
    scanned: Sequence[tuple[int, str, tuple[StepAmount, ...], int]],
    target: MeasureVolumes,
    clarification: Optional[Union[int, Fraction]],
    omissions: _Omissions,
) -> tuple[list[dict[str, Any]], int, bool]:
    """The `steps` array, its emitted span count and whether any step failed.

    A step with at least one span is present; a step with no spans is omitted (the client
    treats an absent step as "no hints"). Serialization is isolated at both levels: a
    span annotation that raises becomes a null-conversion span for that span only (with
    the scanner's capacity origin preserved), and a step that raises becomes a `failed`
    step for that step only. If even that fallback cannot build trustworthy metadata, the
    step fails as a whole rather than carrying an invented annotation. None of these can
    take the other steps, the provenance section or the recipe down with it.
    """
    steps: list[dict[str, Any]] = []
    spans_emitted = 0
    any_failed = False
    for index, text, amounts, omitted in scanned:
        spans: list[dict[str, Any]] = []
        unrepresentable = False
        for amount in amounts:
            try:
                annotation = _span_json(
                    amount, target, clarification, omissions
                )
            except Exception:
                try:
                    annotation = _failed_span_json(amount, clarification)
                except Exception:
                    # Trustworthy metadata could not be built either (for example the
                    # capacity origin). Fail the whole step rather than emit an invented
                    # annotation; the other steps, provenance and ingredients survive.
                    unrepresentable = True
                    break
            spans.append(annotation)
        if unrepresentable:
            any_failed = True
            try:
                steps.append(_failed_step_json(index, text))
            except Exception:
                continue
            continue
        if not spans:
            # The invariant: a step appears only if it has a span.
            continue
        try:
            payload = _step_json(
                index,
                text,
                spans,
                STEP_INCOMPLETE if omitted else STEP_COMPLETE,
            )
        except Exception:
            any_failed = True
            try:
                payload = _failed_step_json(index, text)
            except Exception:
                continue
        steps.append(payload)
        spans_emitted += len(payload["spans"])
    return steps, spans_emitted, any_failed


def _analysis_block(
    provenance_status: str,
    steps_failed: bool,
    spans_emitted: int,
    omissions: _Omissions,
) -> dict[str, Any]:
    """The overall derived-block summary (15.8.2).

    `failed` wins: a step pass or audit failure means the block is not complete data.
    Otherwise any cap, malformed step, unsafe-fraction omission or incomplete provenance
    makes it `incomplete`. `spans_omitted` counts spans suppressed by a cap or by the
    deterministic mention-work budget; an unsafe fraction omits a conversion, not a span,
    and is counted in its own reason.
    """
    reasons = {
        "limit_exceeded": omissions.limit_exceeded,
        "malformed": omissions.malformed,
        "unsafe_fraction": omissions.unsafe_fraction,
    }
    if provenance_status == PROVENANCE_FAILED or steps_failed:
        status = ANALYSIS_FAILED
    elif any(reasons.values()) or provenance_status == PROVENANCE_INCOMPLETE:
        status = ANALYSIS_INCOMPLETE
    else:
        status = ANALYSIS_COMPLETE
    return {
        "status": status,
        "spans_emitted": spans_emitted,
        "spans_omitted": omissions.limit_exceeded,
        "omitted_reasons": reasons,
        "limits": {
            "max_spans_per_step": MAX_SPANS_PER_STEP,
            "max_spans_per_recipe": MAX_SPANS_PER_RECIPE,
            "max_step_chars": MAX_STEP_LENGTH,
            "max_steps": MAX_STEPS,
        },
    }


def measure_block_v2(
    ingredients: Optional[Sequence[object]],
    steps: Optional[Sequence[str]],
    target: MeasureVolumes,
    *,
    source_glass_ml: Optional[Union[int, Fraction]] = None,
    favorite_version: int,
) -> dict[str, Any]:
    """The additive `schema_version: 2` measures block of a favorite detail answer.

    Every released B1 field keeps its name and meaning; the new data is the per-term
    `source_terms`, the step annotations, the provenance audit and the analysis summary.
    `favorite_version` is the detail's top-level `version`, so a client can reject a stale
    answer. The block is read-only: nothing here writes, caches or calls a provider, and
    the three domains fail independently (a provenance failure leaves steps and
    ingredients intact, and a step failure leaves the other two intact).

    Step analysis is bounded by `MENTION_WORK_BUDGET`, a deterministic upper bound on the
    mention-matching comparisons of each step (head lookups plus the nested modifier
    comparisons): a step whose bound exceeds the remaining budget contributes no span.
    Those spans are counted in `omitted_reasons.limit_exceeded`, so `analysis.status` is
    `incomplete` and never a false `complete`; ingredient hints, provenance, the recipe and
    the HTTP response are untouched.
    """
    if not isinstance(target, MeasureVolumes):
        raise TypeError("target must be a MeasureVolumes")
    if (
        isinstance(favorite_version, bool)
        or not isinstance(favorite_version, int)
        or favorite_version < 1
    ):
        raise TypeError("favorite_version must be a positive integer")
    for name, value in (("ingredients", ingredients), ("steps", steps)):
        if value is not None and (
            isinstance(value, (str, bytes)) or not isinstance(value, Sequence)
        ):
            raise TypeError(f"{name} must be a sequence or None")
    items = tuple(ingredients or ())
    texts = tuple(steps or ())
    clarification = (
        None if source_glass_ml is None else _checked_source_glass(source_glass_ml)
    )

    omissions = _Omissions()
    ingredient_payload, source_glass_required = _ingredient_annotations(
        items, target, clarification, omissions
    )
    provenance = _provenance_block(items, omissions)
    scanned = _scan_steps(texts, items, omissions)
    if scanned is None:
        # The whole step pass failed: nothing is annotated, and no per-step retry is
        # attempted. Ingredient conversions and provenance are unaffected (15.6.4).
        step_payload: list[dict[str, Any]] = []
        spans_emitted = 0
        steps_failed = True
        steps_source_glass_required = False
    else:
        entries, steps_source_glass_required = scanned
        step_payload, spans_emitted, steps_failed = _step_annotations(
            entries, target, clarification, omissions
        )
    analysis = _analysis_block(
        provenance["status"], steps_failed, spans_emitted, omissions
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "favorite_version": favorite_version,
        "target": {
            "glass_ml": _whole(target.glass_ml),
            "teaspoon_ml": _whole(target.teaspoon_ml),
            "tablespoon_ml": _whole(target.tablespoon_ml),
        },
        "source_glass_ml": None if clarification is None else _whole(clarification),
        "source_glass_required": source_glass_required,
        "steps_source_glass_required": steps_source_glass_required,
        "ingredients": ingredient_payload,
        "steps": step_payload,
        "provenance": provenance,
        "analysis": analysis,
    }


__all__ = [
    "CAPACITY_DECLARED_TEXT",
    "CAPACITY_NOT_APPLICABLE",
    "CAPACITY_TEMPORARY_CLARIFICATION",
    "CAPACITY_UNRESOLVED",
    "PRESENTATION_EXACT",
    "PRESENTATION_NONE",
    "PRESENTATION_ROUNDED",
    "SAFE_INTEGER_MAX",
    "SCHEMA_VERSION",
    "SOURCE_HOUSEHOLD",
    "SOURCE_METRIC_VOLUME",
    "UnsafeFraction",
    "IngredientConversion",
    "conversion_json",
    "convert_ingredient",
    "measure_block",
    "measure_block_v2",
    "volumes",
]
