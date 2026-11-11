"""KH-F01-B1: present one recipe's amounts in a person's own household measures.

This is the adapter between the accepted KH-F01-B0 calculation core (core.measures) and
the Web layer. It is pure: it reads no database, calls no provider, mutates nothing, and
returns plain values. The core's arithmetic is used exactly as accepted -- exact
Fractions, the 5% practical-rounding gate, the core statuses and reasons -- and this
module adds no arithmetic of its own.

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


__all__ = [
    "PRESENTATION_EXACT",
    "PRESENTATION_NONE",
    "PRESENTATION_ROUNDED",
    "SOURCE_HOUSEHOLD",
    "SOURCE_METRIC_VOLUME",
    "IngredientConversion",
    "conversion_json",
    "convert_ingredient",
    "measure_block",
    "volumes",
]
