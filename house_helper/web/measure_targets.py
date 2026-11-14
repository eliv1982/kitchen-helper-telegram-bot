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
  * mass is not converted inside the B1.1 arrays: B1 authorizes no ingredient
    coefficient there, so grams and kilograms produce no `ingredients` or `steps`
    annotation;
  * an amount identical to its source presentation is not repeated;
  * when rounding changed the exact value the presentation is "rounded" (the page marks
    it as approximate); when a household amount has no practical rounded value the exact
    rational is presented instead ("exact"); nothing else is presented.

KH-F01-B2-2 (`_mass_annotations`) adds independent ingredient-specific mass conversion
under its own key, `measures.mass_conversions` (its own `schema_version: 1`). It never
touches the released arrays: a quantified mass still yields no `ingredients` item and a
linked mass span still carries the frozen `conversion: null` / `"mass"` omission. The
engine (`core.ingredient_reference.convert_ingredient_mass`) is called with the stored
name, the parsed amount and the same resolved personal target measures as B1.1, so the
target capacities are the person's own and are never a source capacity. Only `APPROVED`
registry records produce numbers; the shipped registry approves none, so a real reference
reports its closed non-conversion reason until the owner activates it. The section is a
separate failure domain: registry acquisition, a malformed item and a section-level
failure are each confined to `mass_conversions` and never affect the released block. Each
item is checked for cross-field consistency and JSON safety, then the assembled section --
its schema version, revision, status, items and counters -- is validated and serialized
as a whole before it leaves that boundary. Every unexpected B2 failure writes one
diagnostic event (fixed stage, exception class, position, correlation id) and nothing from
the recipe or the person; a failing logging handler is confined too. Nothing here writes,
caches or calls a provider.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Optional, Sequence, Union

from house_helper.core.ingredient_reference import (
    DEFAULT_REGISTRY,
    MAX_INTERVAL_RELATIVE_WIDTH,
    CountConversion,
    CountDisplay,
    Derivation,
    MassBasis,
    MassConversionReason,
    MassConversionStatus,
    MatchStatus,
    ReferenceKind,
    ReferenceRegistry,
    VolumeConversion,
    convert_ingredient_mass,
    mass_grams,
)
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


# --- KH-F01-B2-2: the ingredient-specific mass conversion extension --------------------
#
# A separate, independently versioned key of the same block. It adds no arithmetic and no
# second parser: the accepted B2-1 gateway (`convert_ingredient_mass`) is asked once per
# quantified mass ingredient, with the stored name, the released parser's quantity and the
# person's resolved target measures, and this section only serializes the closed result.
#
# It is deliberately disjoint from `ingredients`, `steps`, `provenance` and `analysis`: the
# released client is a closed validator of `ingredients`, so a count unit or a B2 status
# there would make it reject the whole block, and a linked mass span's frozen
# `conversion: null` / `"mass"` cannot be contradicted by a parallel number. The outer
# `schema_version` stays 2 for the same reason: `versioned === 2` is an exact client check.

MASS_CONVERSIONS_SCHEMA_VERSION = 1

# The section's own outcome, mirroring the released `analysis.status` vocabulary. `failed`
# means the section could not be built at all; `incomplete` means one or more items were
# dropped (an unsafe rational, or an unexpected engine/serialization failure); no dropped
# item is ever replaced by a guessed number.
MASS_COMPLETE = "complete"
MASS_INCOMPLETE = "incomplete"
MASS_FAILED = "failed"

MASS_KIND_VOLUME = "volume"
MASS_KIND_COUNT = "count"

# The fixed diagnostic stages of an unexpected B2 failure. These words, the exception's
# class name, the ingredient position and a correlation id are the only things that ever
# reach the log; nothing from the recipe, the person or the exception's own message does.
MASS_STAGE_ITEM_CONVERSION = "mass_item_conversion"
MASS_STAGE_ITEM_SERIALIZATION = "mass_item_serialization"
MASS_STAGE_REGISTRY = "mass_registry_resolution"
MASS_STAGE_SECTION = "mass_section"

logger = logging.getLogger(__name__)

# The closed vocabularies the engine can emit. A value outside one is a malformed result,
# not something to pass through to a JSON encoder.
_MASS_STATUSES = frozenset(member.value for member in MassConversionStatus)
_MASS_REASONS = frozenset(member.value for member in MassConversionReason)
# Every closed reason has exactly one status family; any other pair is a malformed result.
_MASS_STATUS_BY_REASON = {
    MassConversionReason.CONVERTED.value: MassConversionStatus.REFERENCE_APPROXIMATE.value,
    MassConversionReason.SIZE_CLASS_REQUIRED.value: (
        MassConversionStatus.CLARIFICATION_REQUIRED.value
    ),
    MassConversionReason.GENERIC_NAME.value: MassConversionStatus.AMBIGUOUS.value,
    MassConversionReason.INGREDIENT_AMBIGUOUS.value: MassConversionStatus.AMBIGUOUS.value,
    MassConversionReason.QUALIFIER_CONFLICT.value: MassConversionStatus.AMBIGUOUS.value,
    MassConversionReason.REFERENCE_VARIANT_CONFLICT.value: MassConversionStatus.AMBIGUOUS.value,
    MassConversionReason.UNSUPPORTED_QUALIFIER.value: MassConversionStatus.UNSUPPORTED.value,
    MassConversionReason.INGREDIENT_NOT_RECOGNIZED.value: MassConversionStatus.UNSUPPORTED.value,
    MassConversionReason.STATE_MISMATCH.value: MassConversionStatus.UNSUPPORTED.value,
    MassConversionReason.ZERO_MASS.value: MassConversionStatus.UNSUPPORTED.value,
    MassConversionReason.REFERENCE_MISSING.value: MassConversionStatus.UNSUPPORTED.value,
    MassConversionReason.REFERENCE_NOT_APPROVED.value: MassConversionStatus.UNSUPPORTED.value,
    MassConversionReason.ROUNDING_GATE_FAILED.value: MassConversionStatus.UNSUPPORTED.value,
    MassConversionReason.INTERVAL_TOO_WIDE.value: MassConversionStatus.UNSUPPORTED.value,
    MassConversionReason.BELOW_USEFUL_COUNT.value: MassConversionStatus.UNSUPPORTED.value,
}
_MATCH_STATUSES = frozenset(member.value for member in MatchStatus)
_MASS_BASES = frozenset(member.value for member in MassBasis)
_DERIVATIONS = frozenset(member.value for member in Derivation)
_REFERENCE_KINDS = frozenset(member.value for member in ReferenceKind)
_DISPLAYS = frozenset(member.value for member in CountDisplay)
_VOLUME_TARGET_UNITS = frozenset(
    (Unit.GLASS.value, Unit.TEASPOON.value, Unit.TABLESPOON.value)
)

# The exact key sets of the serialized item; an unexpected extra or missing key is a
# malformed result and is dropped, never merged into a client payload.
_MASS_ITEM_KEYS = frozenset(
    {
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
)
_INGREDIENT_KEYS = frozenset({"id", "display_name", "match"})
_ASSUMPTION_KEYS = frozenset({"kind", "size_class", "weight_g", "mass_basis", "label_ru"})
_VOLUME_CONVERSION_KEYS = frozenset(
    {"kind", "target_unit", "exact", "rounded", "reference"}
)
_VOLUME_REFERENCE_KEYS = frozenset(
    {"evidence_id", "derivation", "normalization", "method"}
)
_COUNT_CONVERSION_KEYS = frozenset(
    {
        "kind",
        "piece",
        "display",
        "count_low",
        "count_high",
        "rounded_low",
        "rounded_high",
        "relative_width",
        "reference",
    }
)
_PIECE_KEYS = frozenset({"id", "size_class", "reference_kind"})
_COUNT_REFERENCE_KEYS = frozenset({"evidence_id"})

# The exact keys of the assembled section, and its own closed vocabularies. They are
# checked again as a whole (R2-01), not only item by item, so no malformed metadata can
# reach the HTTP encoder after the section was reported as built.
_MASS_SECTION_KEYS = frozenset(
    {"schema_version", "registry_revision", "status", "items", "omitted"}
)
_MASS_OMITTED_KEYS = frozenset({"unsafe_fraction", "malformed"})
_MASS_SECTION_STATUSES = frozenset({MASS_COMPLETE, MASS_INCOMPLETE, MASS_FAILED})

# The match state that must accompany each match-failure reason. A successful conversion
# must have `matched`; a refusal whose reason came from the matcher must carry the state
# the matcher reported (R2-02 A). Every other closed reason is reached after a successful
# match, so it is not constrained here.
_MATCH_BY_REFUSAL_REASON = {
    MassConversionReason.GENERIC_NAME.value: MatchStatus.GENERIC.value,
    MassConversionReason.INGREDIENT_AMBIGUOUS.value: MatchStatus.AMBIGUOUS.value,
    MassConversionReason.QUALIFIER_CONFLICT.value: MatchStatus.QUALIFIER_CONFLICT.value,
    MassConversionReason.UNSUPPORTED_QUALIFIER.value: MatchStatus.UNSUPPORTED_QUALIFIER.value,
    MassConversionReason.INGREDIENT_NOT_RECOGNIZED.value: MatchStatus.UNRECOGNIZED.value,
}


@dataclass
class _MassOmissions:
    """The items this section dropped, by reason. Nothing is clamped or re-derived."""

    unsafe_fraction: int = 0
    malformed: int = 0


class _MalformedMassResult(ValueError):
    """A B2 result that is not self-consistent or not JSON-safe, so it is dropped.

    The message is an internal code and is never logged: the diagnostic event carries only
    this class's name, a fixed stage, the position and the correlation id.
    """


def _operation_correlation_id(value: Optional[str]) -> str:
    """The request's correlation id when it is safe to log, else one local to this build.

    The id is made once per block build, so every diagnostic of the same operation can be
    associated. A value that is not a short ASCII token is not trusted as a log field: a
    client-controlled or newline-bearing string could otherwise forge a log line. When no
    usable id arrives, this makes a minimal local one -- no tracing infrastructure.
    """
    if (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and all(
            char.isascii() and (char.isalnum() or char in "-_") for char in value
        )
    ):
        return value
    return "mass-" + secrets.token_hex(8)


def _log_mass_failure(
    stage: str,
    exc: BaseException,
    correlation_id: str,
    position: Optional[int] = None,
) -> None:
    """One diagnostic event for an unexpected B2 failure, and nothing more.

    Only the fixed stage, the exception's class name, the ingredient position and the
    correlation id are written. The exception is never formatted and `exc_info` is never
    set, so no message, traceback, name, amount, profile field or credential can reach the
    log. Expected conversion refusals are values, not exceptions, and never come here.

    Logging is best effort (R2-03): a handler whose `emit` raises must not turn a confined
    B2 failure into a request failure. The diagnostic is simply lost -- it is never
    retried and never routed to a second logger -- and the caller keeps the `incomplete`
    or `failed` section it was building. Only `Exception` is caught; a process-level
    `BaseException` still propagates.
    """
    try:
        logger.warning(
            "Mass conversion failed: stage=%s exception=%s position=%s correlation_id=%s",
            stage,
            type(exc).__name__,
            "none" if position is None else position,
            correlation_id,
        )
    except Exception:
        # The logging subsystem itself failed. There is nowhere safe to report that
        # without risking the same failure again, so the event is dropped.
        pass


def _reference_registry() -> ReferenceRegistry:
    """The registry revision production converts against.

    One shipped revision is the only production source. The indirection exists so the
    API-level tests can substitute an approved synthetic fixture without a second code
    path: nothing in the application passes a registry of its own. It is called inside
    the B2 failure boundary, so a broken registry cannot blank the released block.
    """
    return DEFAULT_REGISTRY


def _ingredient_name(item: object) -> str:
    """The stored name of an ingredient object or mapping.

    The name is what B2 matches; it is serialized verbatim and never rewritten.
    """
    name = getattr(item, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(item, Mapping) and isinstance(item.get("name"), str):
        return item["name"]
    raise TypeError("every ingredient must carry a string name")


def _exact_number(value: object) -> bool:
    """An integer or an exact rational, never a bool, a float or a numeric string."""
    return isinstance(value, (int, Fraction)) and not isinstance(value, bool)


def _safe_mass_fraction(value: object) -> dict[str, int]:
    """A B2 rational as two safe integers; anything not exact is malformed, not coerced."""
    if not _exact_number(value):
        raise _MalformedMassResult("not an exact number")
    return _safe_fraction_json(value)


def _safe_mass_rounded(value: object) -> Optional[dict[str, Any]]:
    """A rounded amount, keeping the same exact-number rule as its members."""
    if value is None:
        return None
    return {
        **_safe_mass_fraction(getattr(value, "amount", None)),
        "relative_error": _safe_mass_fraction(getattr(value, "relative_error", None)),
    }


def _is_safe_integer(value: object, *, minimum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= SAFE_INTEGER_MAX
    )


def _is_fraction_json(value: object) -> bool:
    """Exactly `{"numerator", "denominator"}`, both within the safe-integer range."""
    return (
        isinstance(value, dict)
        and set(value) == {"numerator", "denominator"}
        and _is_safe_integer(value["numerator"], minimum=0)
        and _is_safe_integer(value["denominator"], minimum=1)
    )


def _is_rounded_json(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"numerator", "denominator", "relative_error"}
        and _is_fraction_json(
            {key: value[key] for key in ("numerator", "denominator")}
        )
        and _is_fraction_json(value["relative_error"])
    )


def _fraction_value(value: Mapping[str, int]) -> Fraction:
    """The exact rational of a fraction JSON object already checked by `_is_fraction_json`."""
    return Fraction(value["numerator"], value["denominator"])


def _is_text(value: object, *, optional: bool = False) -> bool:
    if value is None:
        return optional
    return isinstance(value, str)


def _is_identifier(value: object, *, optional: bool = False) -> bool:
    """A non-blank string identifier (or None when the schema allows it)."""
    if value is None:
        return optional
    return isinstance(value, str) and bool(value.strip())


def _mass_assumption_json(assumption: Any) -> dict[str, Any]:
    """The explicitly labelled size assumption behind a conditional estimate."""
    return {
        "kind": assumption.kind,
        "size_class": assumption.size_class,
        "weight_g": _safe_mass_fraction(assumption.weight_g),
        "mass_basis": assumption.mass_basis.value,
        "label_ru": assumption.label_ru,
    }


def _mass_conversion_json(conversion: Any, entry: Any) -> dict[str, Any]:
    """One successful conversion: a volume count or a count of pieces.

    Only a successful engine result reaches this function, so every field it reads is the
    one the engine filled for a number. A shape neither branch recognizes is a programming
    error and raises; the caller then drops the item instead of inventing a payload.
    """
    if isinstance(conversion, VolumeConversion):
        return {
            "kind": MASS_KIND_VOLUME,
            "target_unit": conversion.target_unit.value,
            "exact": _safe_mass_fraction(conversion.exact_count),
            "rounded": _safe_mass_rounded(conversion.rounded),
            # The registry revision travels on the section; the evidence identifier and the
            # derivation that produced this number travel with it.
            "reference": {
                "evidence_id": conversion.evidence_id,
                "derivation": conversion.derivation.value,
                "normalization": conversion.normalization,
                "method": conversion.method,
            },
        }
    if isinstance(conversion, CountConversion):
        return {
            "kind": MASS_KIND_COUNT,
            "piece": {
                "id": None if entry is None else entry.piece_id,
                "size_class": conversion.size_class,
                "reference_kind": conversion.reference_kind.value,
            },
            "display": conversion.display.value,
            "count_low": _safe_mass_fraction(conversion.count_low),
            "count_high": _safe_mass_fraction(conversion.count_high),
            "rounded_low": _safe_mass_fraction(conversion.rounded_low),
            "rounded_high": _safe_mass_fraction(conversion.rounded_high),
            "relative_width": _safe_mass_fraction(conversion.relative_width),
            "reference": {"evidence_id": conversion.evidence_id},
        }
    raise TypeError(f"unknown mass conversion result: {type(conversion).__name__}")


def _validate_mass_ingredient(item: dict[str, Any]) -> None:
    ingredient = item["ingredient"]
    if not isinstance(ingredient, dict) or set(ingredient) != _INGREDIENT_KEYS:
        raise _MalformedMassResult("ingredient shape")
    if not _is_identifier(ingredient["id"], optional=True):
        raise _MalformedMassResult("ingredient id")
    if not _is_text(ingredient["display_name"], optional=True):
        raise _MalformedMassResult("ingredient display name")
    if ingredient["match"] not in _MATCH_STATUSES:
        raise _MalformedMassResult("match status")


def _validate_mass_assumption(assumption: object) -> None:
    """The assumption behind a conditional estimate (R2-02 E).

    It must name a real size class, carry a positive exact reference weight and a
    non-empty Russian label, and use one of the closed mass-basis values. The class is
    cross-checked against the conversion's own piece by `_validate_mass_item`.
    """
    if not isinstance(assumption, dict) or set(assumption) != _ASSUMPTION_KEYS:
        raise _MalformedMassResult("assumption shape")
    if assumption["kind"] != "size_class":
        raise _MalformedMassResult("assumption kind")
    if not _is_identifier(assumption["size_class"]):
        raise _MalformedMassResult("assumption size class")
    if not _is_fraction_json(assumption["weight_g"]):
        raise _MalformedMassResult("assumption weight")
    if _fraction_value(assumption["weight_g"]) <= 0:
        raise _MalformedMassResult("assumption weight is not positive")
    if assumption["mass_basis"] not in _MASS_BASES:
        raise _MalformedMassResult("assumption mass basis")
    label = assumption["label_ru"]
    if not isinstance(label, str) or not label.strip():
        raise _MalformedMassResult("assumption label")
    if not any(chr(0x0400) <= char <= chr(0x04FF) for char in label):
        raise _MalformedMassResult("assumption label is not Russian")


def _validate_volume_conversion(conversion: dict[str, Any]) -> None:
    if set(conversion) != _VOLUME_CONVERSION_KEYS:
        raise _MalformedMassResult("volume conversion shape")
    if conversion["target_unit"] not in _VOLUME_TARGET_UNITS:
        raise _MalformedMassResult("volume target unit")
    if not _is_fraction_json(conversion["exact"]):
        raise _MalformedMassResult("volume exact count")
    if _fraction_value(conversion["exact"]) <= 0:
        raise _MalformedMassResult("volume exact count is not positive")
    rounded = conversion["rounded"]
    if rounded is not None and not _is_rounded_json(rounded):
        raise _MalformedMassResult("volume rounded count")
    if rounded is not None and _fraction_value(
        {key: rounded[key] for key in ("numerator", "denominator")}
    ) <= 0:
        raise _MalformedMassResult("volume rounded count is not positive")
    reference = conversion["reference"]
    if not isinstance(reference, dict) or set(reference) != _VOLUME_REFERENCE_KEYS:
        raise _MalformedMassResult("volume reference shape")
    if not _is_identifier(reference["evidence_id"]):
        raise _MalformedMassResult("volume evidence id")
    if reference["derivation"] not in _DERIVATIONS:
        raise _MalformedMassResult("volume derivation")
    for field in ("normalization", "method"):
        if not _is_text(reference[field], optional=True):
            raise _MalformedMassResult("volume reference text")


def _validate_count_conversion(conversion: dict[str, Any]) -> None:
    if set(conversion) != _COUNT_CONVERSION_KEYS:
        raise _MalformedMassResult("count conversion shape")
    piece = conversion["piece"]
    if not isinstance(piece, dict) or set(piece) != _PIECE_KEYS:
        raise _MalformedMassResult("piece shape")
    if not _is_identifier(piece["id"], optional=True):
        raise _MalformedMassResult("piece id")
    if not _is_identifier(piece["size_class"], optional=True):
        raise _MalformedMassResult("piece size class")
    if piece["reference_kind"] not in _REFERENCE_KINDS:
        raise _MalformedMassResult("piece reference kind")
    if conversion["display"] not in _DISPLAYS:
        raise _MalformedMassResult("count display")
    for field in (
        "count_low",
        "count_high",
        "rounded_low",
        "rounded_high",
        "relative_width",
    ):
        if not _is_fraction_json(conversion[field]):
            raise _MalformedMassResult("count fraction")
    reference = conversion["reference"]
    if not isinstance(reference, dict) or set(reference) != _COUNT_REFERENCE_KEYS:
        raise _MalformedMassResult("count reference shape")
    if not _is_identifier(reference["evidence_id"]):
        raise _MalformedMassResult("count evidence id")
    # The presentation follows the rounded bounds: a single number is exactly the case
    # where both bounds rounded to the same value, and an interval is exactly the rest.
    single = conversion["display"] == CountDisplay.SINGLE.value
    if single != (conversion["rounded_low"] == conversion["rounded_high"]):
        raise _MalformedMassResult("count display and bounds disagree")

    # Cross-field consistency (R2-02 D). The values are not recalculated; only what the
    # engine guarantees about them is checked, using exact rationals.
    low = _fraction_value(conversion["count_low"])
    high = _fraction_value(conversion["count_high"])
    rounded_low = _fraction_value(conversion["rounded_low"])
    rounded_high = _fraction_value(conversion["rounded_high"])
    width = _fraction_value(conversion["relative_width"])
    if min(low, high, rounded_low, rounded_high) <= 0:
        raise _MalformedMassResult("count is not positive")
    if low > high:
        raise _MalformedMassResult("count interval is reversed")
    if rounded_low > rounded_high:
        raise _MalformedMassResult("rounded interval is reversed")
    if width < 0:
        raise _MalformedMassResult("relative width is negative")
    if (low == high) != (width == 0):
        raise _MalformedMassResult("relative width and bounds disagree")
    if high != low and width != (high - low) / ((high + low) / 2):
        raise _MalformedMassResult("relative width is inconsistent")
    kind = piece["reference_kind"]
    if kind == ReferenceKind.POINT.value:
        # A point reference is one weight: it can never present as a measured interval.
        if low != high or rounded_low != rounded_high or width != 0:
            raise _MalformedMassResult("a point reference carries an interval")
        if not single:
            raise _MalformedMassResult("a point reference is not a single count")
    elif width > MAX_INTERVAL_RELATIVE_WIDTH:
        raise _MalformedMassResult("an interval is wider than the accepted bound")
    if kind == ReferenceKind.CALIBRATION_CLASS.value and not piece["size_class"]:
        raise _MalformedMassResult("a calibration class has no size class")


def _validate_mass_item(item: dict[str, Any]) -> None:
    """Strict shape, status and cross-field consistency of one B2 item (R2-02).

    A refusal is exactly a non-`converted` reason with `conversion: null`; a success is
    exactly a `reference_approximate`/`converted` pair with a payload. Any other
    combination is dropped, so no non-null conversion can travel with a refusal and no
    successful status can travel without one.

    Beyond the closed shape, the item must be internally believable: a success must name a
    recognized ingredient (a non-blank identity and the `matched` state, never an
    unrecognized or ambiguous one); a matcher refusal must carry the match state that
    reason belongs to; a conditional estimate must be the count conversion it describes,
    with the same size class in both places; and the zero-mass invariants must hold (a
    success needs strictly positive source mass, and `zero_mass` is exactly a zero source
    mass). Any contradiction drops the item rather than choosing which field to trust.
    """
    if set(item) != _MASS_ITEM_KEYS:
        raise _MalformedMassResult("item shape")
    if not _is_safe_integer(item["position"], minimum=0):
        raise _MalformedMassResult("position")
    if not isinstance(item["name"], str) or not isinstance(item["source"], str):
        raise _MalformedMassResult("stored text")
    if not _is_fraction_json(item["mass_g"]):
        raise _MalformedMassResult("source mass")
    _validate_mass_ingredient(item)
    if item["status"] not in _MASS_STATUSES:
        raise _MalformedMassResult("conversion status")
    if item["reason"] not in _MASS_REASONS:
        raise _MalformedMassResult("conversion reason")
    if _MASS_STATUS_BY_REASON.get(item["reason"]) != item["status"]:
        raise _MalformedMassResult("status and reason disagree")
    converted = item["reason"] == MassConversionReason.CONVERTED.value
    # Zero-mass invariants (KH-F01-B2-2-R3). `mass_g` is the parsed source mass: the
    # serializer already refused any result whose `mass_g` drifted from the amount the
    # parser read (R2-02 B), so these are exact `Fraction` comparisons on the source mass,
    # never a float approximation. A number is believable only for a strictly positive
    # mass, and `zero_mass` is meaningful only for an exactly zero mass; conversely an
    # exactly zero mass can only be that refusal.
    #
    # The reverse implication is safe only because of the accepted B2-1 precedence: the
    # gateway checks `mass == 0` and returns `ZERO_MASS` before it consults the match or
    # any reference, so no other refusal (such as `reference_not_approved`) can be
    # returned first for a zero mass. Rejecting a zero mass with a different reason
    # therefore cannot drop a legitimate engine result.
    source_mass = _fraction_value(item["mass_g"])
    if converted and source_mass <= 0:
        raise _MalformedMassResult("a success with a non-positive source mass")
    if item["reason"] == MassConversionReason.ZERO_MASS.value:
        if source_mass != 0:
            raise _MalformedMassResult("a zero-mass reason with a non-zero source mass")
    elif source_mass == 0:
        raise _MalformedMassResult("a zero source mass without the zero-mass reason")
    match = item["ingredient"]["match"]
    if converted:
        # A number is only believable for an ingredient the matcher actually recognized.
        if match != MatchStatus.MATCHED.value:
            raise _MalformedMassResult("a success with an unsuccessful match")
        if not _is_identifier(item["ingredient"]["id"]):
            raise _MalformedMassResult("a success without an ingredient identity")
        if not _is_text(item["ingredient"]["display_name"]):
            raise _MalformedMassResult("a success without a display name")
    else:
        expected_match = _MATCH_BY_REFUSAL_REASON.get(item["reason"])
        if expected_match is not None and match != expected_match:
            raise _MalformedMassResult("refusal reason and match disagree")
    conversion = item["conversion"]
    if converted != (conversion is not None):
        raise _MalformedMassResult("a refusal cannot carry a conversion")
    if not isinstance(item["conditional"], bool):
        raise _MalformedMassResult("conditional flag")
    assumption = item["assumption"]
    if (assumption is not None) != item["conditional"]:
        raise _MalformedMassResult("conditional and assumption disagree")
    if conversion is not None:
        if not isinstance(conversion, dict):
            raise _MalformedMassResult("conversion shape")
        if conversion.get("kind") == MASS_KIND_VOLUME:
            _validate_volume_conversion(conversion)
        elif conversion.get("kind") == MASS_KIND_COUNT:
            _validate_count_conversion(conversion)
        else:
            raise _MalformedMassResult("unknown conversion kind")
    if assumption is not None:
        _validate_mass_assumption(assumption)
        # An unconditional result cannot reach here (`conditional` and the assumption
        # agree), so this is exactly the conditional case: it must describe its count.
        if conversion is None or conversion.get("kind") != MASS_KIND_COUNT:
            raise _MalformedMassResult("a conditional estimate is not a count")
        piece = conversion["piece"]
        if not piece["size_class"]:
            raise _MalformedMassResult("a conditional estimate has no size class")
        if piece["size_class"] != assumption["size_class"]:
            raise _MalformedMassResult("assumption and piece size classes disagree")


def _json_ready(value: Any, label: str) -> Any:
    """Strict JSON encoding inside the B2 boundary.

    `allow_nan=False` refuses a non-finite float, and encoding the result as UTF-8 refuses
    a lone surrogate a later response encoder would choke on. Used for each item and,
    independently, for the whole assembled section (R2-01). A failure is a malformed
    result, never a 500 after the response has begun.
    """
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        raise _MalformedMassResult(label) from None
    return value


def _json_ready_mass_item(item: dict[str, Any]) -> dict[str, Any]:
    """The final item gate: strict JSON encoding, so nothing reaches the encoder later."""
    return _json_ready(item, "not JSON-safe")


def _mass_item_json(
    position: int, name: str, amount: str, conversion: Any, source_mass: Fraction
) -> dict[str, Any]:
    """One ingredient's B2 result, successful or refused, validated before it is emitted.

    The verbatim stored name and amount travel with the outcome so a client can bind the
    item to its line without trusting an index alone. `mass_g` is the parsed source mass
    (an input, never a converted result); it is checked against the exact mass the parser
    read from `amount` (R2-02 B), so a result whose serialized mass drifted from the stored
    amount is dropped rather than presented. A nested conversion's own status and reason
    must agree with the outer result (R2-02 C): the serializer never chooses which of two
    contradictory fields to trust. A refusal carries `conversion: null` and no count,
    volume or rounded value at all; `conversion` is non-null exactly when the engine's
    closed reason is `converted`. An inconsistent or non-JSON-safe result raises
    `_MalformedMassResult`, and the caller drops only that item.
    """
    entry = conversion.match.entry
    successful = conversion.conversion is not None
    if not _exact_number(conversion.mass_g) or Fraction(conversion.mass_g) != source_mass:
        raise _MalformedMassResult("source mass disagrees with the stored amount")
    if successful:
        nested = conversion.conversion
        if (
            nested.status is not conversion.status
            or nested.reason is not MassConversionReason.CONVERTED
        ):
            raise _MalformedMassResult("nested status and reason disagree")
    item = {
        "position": position,
        "name": name,
        "source": amount,
        "mass_g": _safe_mass_fraction(conversion.mass_g),
        "ingredient": {
            "id": None if entry is None else entry.ingredient_id,
            "display_name": None if entry is None else entry.display_name,
            # What the name resolved to (matched, generic, ambiguous, unrecognized, ...).
            "match": conversion.match.status.value,
        },
        "status": conversion.status.value,
        "reason": conversion.reason.value,
        "conditional": conversion.conditional,
        "assumption": (
            None
            if conversion.assumption is None
            else _mass_assumption_json(conversion.assumption)
        ),
        "conversion": (
            None if not successful else _mass_conversion_json(conversion.conversion, entry)
        ),
    }
    _validate_mass_item(item)
    return _json_ready_mass_item(item)


def _mass_annotations(
    ingredients: Sequence[object],
    target: MeasureVolumes,
    registry: ReferenceRegistry,
    omissions: _MassOmissions,
    correlation_id: str,
) -> list[dict[str, Any]]:
    """One item per quantified mass ingredient, in stored order.

    The released parser decides what a mass is: a range, a qualitative phrase, a volume, a
    count or a package yields no item at all (the B1.1 arrays own those amounts), and no
    midpoint or partial reading is salvaged from them. A quantified mass always yields one
    item, even when nothing converts.

    Failure boundary. Expected outcomes are values, not exceptions: the engine's closed
    `status`/`reason` pair covers every refusal, and an exception means the item could not
    be built -- an unsafe rational (`UnsafeFraction`) or an unexpected engine or
    serialization failure. Neither is turned into a number: the item is dropped and
    counted. The section status becomes `incomplete` when anything was dropped, so a
    defect is visible in the payload instead of being concealed behind a plausible value.
    A converter returning `None` for an amount the parser itself called a mass is such a
    defect, not a non-mass outcome. Every unexpected failure emits one safe diagnostic
    event; an expected refusal emits none.
    """
    items: list[dict[str, Any]] = []
    for position, item in enumerate(ingredients):
        try:
            name = _ingredient_name(item)
            amount = _ingredient_amount(item)
            # The same parser configuration the released B1 path uses for the same stored
            # text; there is no second parser and no re-interpretation of the amount.
            quantity = parse_quantity(amount, allow_nonstandard_glass_sizes=True)
            source_mass = mass_grams(quantity)
            conversion = convert_ingredient_mass(
                name, quantity, target=target, registry=registry
            )
        except Exception as exc:
            omissions.malformed += 1
            _log_mass_failure(MASS_STAGE_ITEM_CONVERSION, exc, correlation_id, position)
            continue
        if conversion is None:
            if source_mass is not None:
                # A quantified mass always has an outcome; `None` is a defect, and it is
                # never presented as a complete section with nothing omitted.
                omissions.malformed += 1
                _log_mass_failure(
                    MASS_STAGE_ITEM_CONVERSION,
                    _MalformedMassResult("a quantified mass produced no result"),
                    correlation_id,
                    position,
                )
            continue  # not a quantified mass: the released volume behavior owns it
        if source_mass is None:
            # A result for an amount the parser did not read as a mass is a defect too:
            # its `mass_g` could not be checked against the stored amount (R2-02 B).
            omissions.malformed += 1
            _log_mass_failure(
                MASS_STAGE_ITEM_CONVERSION,
                _MalformedMassResult("a non-mass produced a result"),
                correlation_id,
                position,
            )
            continue
        try:
            items.append(_mass_item_json(position, name, amount, conversion, source_mass))
        except UnsafeFraction:
            omissions.unsafe_fraction += 1
        except Exception as exc:
            omissions.malformed += 1
            _log_mass_failure(
                MASS_STAGE_ITEM_SERIALIZATION, exc, correlation_id, position
            )
    return items


def _failed_mass_section(revision: Optional[str]) -> dict[str, Any]:
    """An empty `failed` section; `registry_revision` is null when it was never obtained.

    This is a fixed structure that uses no value from the recipe, the person or the
    failing result, so building it cannot fail the way the thing it replaces did. It is
    the one fallback, used both for acquisition failures and for a section that does not
    pass the complete-section gate.
    """
    return {
        "schema_version": MASS_CONVERSIONS_SCHEMA_VERSION,
        "registry_revision": revision,
        "status": MASS_FAILED,
        "items": [],
        "omitted": {"unsafe_fraction": 0, "malformed": 0},
    }


def _validate_registry_revision(revision: object) -> None:
    """A revision the HTTP encoder can emit as UTF-8, or the acquisition has failed.

    A lone surrogate is a `str` but cannot be encoded to UTF-8, so checking only the type
    would let it through to the response encoder and turn a confined B2 failure into an
    HTTP 500 (R2-01).
    """
    if not isinstance(revision, str) or not revision:
        raise _MalformedMassResult("the registry revision must be a non-empty string")
    try:
        revision.encode("utf-8")
    except UnicodeEncodeError:
        raise _MalformedMassResult(
            "the registry revision is not valid UTF-8"
        ) from None


def _validated_mass_section(section: dict[str, Any]) -> dict[str, Any]:
    """The complete-section gate (R2-01), run inside the B2 failure boundary.

    Every metadata field is checked, not only each item: the schema version is this
    section's own, the revision is a non-empty UTF-8 string, the status is one of the
    closed values, `items` is a list and `omitted` is exactly the two non-negative
    counters. The counters must agree with the status (`complete` has none, `incomplete`
    has at least one, `failed` has no items). Finally the whole section is serialized with
    `allow_nan=False` and encoded as UTF-8, so nothing that a later response encoder would
    reject can ever leave this boundary as a built section.
    """
    if set(section) != _MASS_SECTION_KEYS:
        raise _MalformedMassResult("section shape")
    version = section["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != MASS_CONVERSIONS_SCHEMA_VERSION
    ):
        raise _MalformedMassResult("section schema version")
    _validate_registry_revision(section["registry_revision"])
    status = section["status"]
    if status not in _MASS_SECTION_STATUSES:
        raise _MalformedMassResult("section status")
    items = section["items"]
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise _MalformedMassResult("section items")
    # The items were validated one by one as they were built; checking them again here
    # means the section claim is about what is actually in it, not about what a builder
    # promised. An item that is not believable fails the section, never leaks through it.
    for item in items:
        _validate_mass_item(item)
    omitted = section["omitted"]
    if not isinstance(omitted, dict) or set(omitted) != _MASS_OMITTED_KEYS:
        raise _MalformedMassResult("section omissions")
    for name in _MASS_OMITTED_KEYS:
        value = omitted[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _MalformedMassResult("section omission counter")
    total = omitted["unsafe_fraction"] + omitted["malformed"]
    if status == MASS_FAILED:
        if items or total:
            raise _MalformedMassResult("a failed section carries items or omissions")
    elif status == MASS_COMPLETE:
        if total:
            raise _MalformedMassResult("a complete section has omissions")
    elif not total:
        raise _MalformedMassResult("an incomplete section has no omissions")
    _json_ready(section, "the section is not JSON-safe")
    return section


def _mass_conversions_block(
    ingredients: Sequence[object],
    target: MeasureVolumes,
    registry: Optional[ReferenceRegistry],
    correlation_id: str,
) -> dict[str, Any]:
    """The `measures.mass_conversions` extension, with its own failure domain.

    The block is built here and nowhere else. Registry acquisition happens inside this
    boundary, so a registry that cannot be resolved -- including one whose revision is not
    a UTF-8 string -- yields an empty `failed` section with a null revision instead of
    failing the released block. A section-level failure yields the same, keeping the
    revision that was actually obtained (it was validated before use). The assembled
    section is then checked as a whole and serialized strictly before it is returned
    (R2-01); a section that fails that gate becomes the same fixed `failed` fallback, with
    no revision reused from the failed metadata. Nothing here ever nulls `measures`,
    changes the HTTP status or affects the released ingredient, step, provenance or
    analysis data.
    """
    omissions = _MassOmissions()
    try:
        active = _reference_registry() if registry is None else registry
        revision = active.revision
        _validate_registry_revision(revision)
    except Exception as exc:
        _log_mass_failure(MASS_STAGE_REGISTRY, exc, correlation_id)
        return _failed_mass_section(None)
    try:
        items = _mass_annotations(ingredients, target, active, omissions, correlation_id)
    except Exception as exc:
        _log_mass_failure(MASS_STAGE_SECTION, exc, correlation_id)
        return _failed_mass_section(revision)
    section = {
        "schema_version": MASS_CONVERSIONS_SCHEMA_VERSION,
        "registry_revision": revision,
        "status": (
            MASS_INCOMPLETE
            if omissions.unsafe_fraction or omissions.malformed
            else MASS_COMPLETE
        ),
        "items": items,
        "omitted": {
            "unsafe_fraction": omissions.unsafe_fraction,
            "malformed": omissions.malformed,
        },
    }
    try:
        return _validated_mass_section(section)
    except Exception as exc:
        # The section could not be validated or serialized as a whole. Do not reuse any
        # of its metadata (the revision included): the fixed fallback is known to be safe.
        _log_mass_failure(MASS_STAGE_SECTION, exc, correlation_id)
        return _failed_mass_section(None)


def measure_block_v2(
    ingredients: Optional[Sequence[object]],
    steps: Optional[Sequence[str]],
    target: MeasureVolumes,
    *,
    source_glass_ml: Optional[Union[int, Fraction]] = None,
    favorite_version: int,
    reference_registry: Optional[ReferenceRegistry] = None,
    correlation_id: Optional[str] = None,
) -> dict[str, Any]:
    """The additive `schema_version: 2` measures block of a favorite detail answer.

    Every released B1 field keeps its name and meaning; the new data is the per-term
    `source_terms`, the step annotations, the provenance audit and the analysis summary,
    plus the independently versioned `mass_conversions` extension (KH-F01-B2-2).
    `favorite_version` is the detail's top-level `version`, so a client can reject a stale
    answer. The block is read-only: nothing here writes, caches or calls a provider, and
    the three domains fail independently (a provenance failure leaves steps and
    ingredients intact, and a step failure leaves the other two intact).

    `reference_registry` is a test seam only: production passes nothing, which is the one
    shipped registry revision. `correlation_id` is the request's diagnostic id when the
    caller has one; a safe value is reused for every B2 event of this build, and anything
    else is replaced by a local id so no client text can reach the log. The outer
    `schema_version` stays 2 -- the released client compares it exactly, and the new key
    carries its own version.

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
    if reference_registry is not None and not isinstance(
        reference_registry, ReferenceRegistry
    ):
        raise TypeError("reference_registry must be a ReferenceRegistry or None")
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
    operation_id = _operation_correlation_id(correlation_id)
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
        # The B2 extension is computed last and confined to its own section, so it cannot
        # change any released field above it. Registry acquisition happens inside it.
        "mass_conversions": _mass_conversions_block(
            items, target, reference_registry, operation_id
        ),
    }


__all__ = [
    "CAPACITY_DECLARED_TEXT",
    "CAPACITY_NOT_APPLICABLE",
    "CAPACITY_TEMPORARY_CLARIFICATION",
    "CAPACITY_UNRESOLVED",
    "MASS_COMPLETE",
    "MASS_CONVERSIONS_SCHEMA_VERSION",
    "MASS_FAILED",
    "MASS_INCOMPLETE",
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
