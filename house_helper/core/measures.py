"""Deterministic kitchen measurement arithmetic: recognition, conversion, rounding.

This is the KH-F01-A foundation, not a user-facing feature. It is pure Core: standard
library only, no database, web, Telegram, OpenAI or configuration, and no outside call.
The arithmetic is exact -- rational numbers from `fractions` -- so nothing accumulates
floating-point error, and presentation rounding is applied at most once, at the end.

What a quantity is
------------------
A text amount is recognized only when the whole line is an explicit, unambiguous
measurement. The original text is always kept in `Quantity.text`, exactly as it came in;
nothing here rewrites a recipe or a stored amount.

Supported measures:

* mass -- g, kg (`г`, `гр`, `грамм`, `кг`, ...);
* volume -- ml, l (`мл`, `литр`, ...);
* household volumes -- teaspoon (`ч. л.`), tablespoon (`ст. л.`) and glass (`стакан`);
* one or several explicit terms joined by `+` or `и` (and metric terms may simply stand
  next to each other, as in `1 кг 200 г`), when all terms share one dimension;
* a glass whose capacity is written in the text itself (`1 стакан 200 мл`);
* decimal comma and point (`1,5`, `1.5`), mixed numbers (`1½`, `1 1/2`) and the common
  vulgar fractions (`¼`, `½`, `¾`, ...).

Anything else is reported, never guessed. An explicit range (`2–3 ст. л.`) is a range --
no midpoint is invented. Qualitative amounts (`по вкусу`, `щепотка`, `немного`) stay
qualitative. Countable and package units (`шт.`, `зубчик`, `пачка`), heaped spoons and
US cups are recognized only in order to be reported as unsupported; a US cup is never
silently read as a Russian glass. A malformed or unknown line is unresolved.

Conversion rules
----------------
* 1 kg = 1000 g and 1 L = 1000 mL exactly.
* 1 tsp = 5 mL and 1 tbsp = 15 mL are *nominal household volumes*: they define a
  conventional volume, not the physical accuracy of a real spoon. A source glass is 200
  or 250 mL only when that size is explicit (in the text, in reliable source metadata or
  in the caller's legacy `glass_ml`); without it the result asks for clarification and no
  default is assumed. An explicitly declared source capacity outside 200/250 mL,
  including a fractional one, is accepted only in the opt-in parser mode
  (`allow_nonstandard_glass_sizes=True`) and must stay within the approved 50--1000 mL
  range.
* Mass and volume are never converted into each other here. That needs an approved
  ingredient-specific coefficient, and F01-A authorizes none.
* A conversion is EXACT when every unit involved is a metric mass or volume unit;
  otherwise it is NOMINAL_HOUSEHOLD, because a household measure was involved.

Household measure contexts (KH-F01-B0)
--------------------------------------
`MeasureVolumes` is an immutable set of household capacities. A *source* context records
what is reliably established about the source recipe itself; a *target* context records
the household the converted result is presented in. The two sides are independent:
target capacities never resolve or reinterpret a source measure, and an undeclared
source glass stays unknown even when the target glass is known. Missing target
capacities resolve to the system defaults (250/5/15 mL) through
`resolve_target_measures`; a source spoon without reliable metadata stays conventional
at 5/15 mL, and a source glass without a declaration or metadata is never defaulted.
The legacy `glass_ml` argument keeps its old meaning and cannot be combined with either
context.

Practical rounding
------------------
`practical_round` chooses the coarsest practical step (whole measures first, then
halves, then quarters where applicable) whose additional error against the *unrounded*
target is at most 5% (`MAX_ROUNDING_RELATIVE_ERROR`). If no step passes, it returns
None: no reliable rounded equivalent exists, and the caller should keep the unrounded
value. Rounding never runs on top of an earlier rounding.

Outcomes and context
--------------------
`ConversionResult.status` is the stable family a future Web or Telegram layer maps to
Russian wording; `reason`, `basis`, the source `Quantity` (with every parsed part) and
`glass_sizes_ml` are machine-readable context, so no caller has to guess why a
conversion succeeded or failed. Wording is deliberately not produced here.

Partial results are natural: every amount is parsed and converted on its own, so one
ambiguous or unsupported ingredient cannot prevent another from converting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

# The only glass capacities the legacy Russian kitchen definition allows, in millilitres.
# Extended explicit source declarations are fenced by GLASS_ML_MIN..GLASS_ML_MAX below.
GLASS_SIZES_ML: tuple[int, ...] = (200, 250)

# System defaults for a target household (D18/D19). They are product constants, not
# personal values: no account is seeded from them, and they never resolve a source glass.
SYSTEM_GLASS_ML = Fraction(250)
SYSTEM_TEASPOON_ML = Fraction(5)
SYSTEM_TABLESPOON_ML = Fraction(15)

# Approved personal ranges, inclusive (D18/D19). The glass fence also governs explicitly
# declared source capacities in the opt-in parser mode.
GLASS_ML_MIN = Fraction(50)
GLASS_ML_MAX = Fraction(1000)
TEASPOON_ML_MIN = Fraction(1)
TEASPOON_ML_MAX = Fraction(20)
TABLESPOON_ML_MIN = Fraction(5)
TABLESPOON_ML_MAX = Fraction(50)

# The additional error a practical representation may introduce, relative to the
# unrounded target: 5% (D07).
MAX_ROUNDING_RELATIVE_ERROR = Fraction(1, 20)


class Dimension(str, Enum):
    """The physical dimension of a quantity. Mass and volume never mix here."""

    MASS = "mass"
    VOLUME = "volume"


class Unit(str, Enum):
    """A supported source or target unit. The values are stable for callers."""

    GRAM = "gram"
    KILOGRAM = "kilogram"
    MILLILITER = "milliliter"
    LITER = "liter"
    TEASPOON = "teaspoon"
    TABLESPOON = "tablespoon"
    GLASS = "glass"

    @property
    def dimension(self) -> Dimension:
        return _UNIT_DIMENSIONS[self]


_UNIT_DIMENSIONS = {
    Unit.GRAM: Dimension.MASS,
    Unit.KILOGRAM: Dimension.MASS,
    Unit.MILLILITER: Dimension.VOLUME,
    Unit.LITER: Dimension.VOLUME,
    Unit.TEASPOON: Dimension.VOLUME,
    Unit.TABLESPOON: Dimension.VOLUME,
    Unit.GLASS: Dimension.VOLUME,
}

# Units whose size is a nominal household definition rather than a metric definition.
_HOUSEHOLD_UNITS = frozenset({Unit.TEASPOON, Unit.TABLESPOON, Unit.GLASS})
# Units with a purely decimal relation to their base unit.
_METRIC_UNITS = frozenset({Unit.GRAM, Unit.KILOGRAM, Unit.MILLILITER, Unit.LITER})

# The practical presentation grids, coarsest first. A whole measure is preferred; a
# half is used when the whole misses the 5% gate; quarters follow only where a quarter
# is a real kitchen measure (D07/D16).
_PRACTICAL_STEPS: dict[Unit, tuple[Fraction, ...]] = {
    Unit.GRAM: (Fraction(1), Fraction(1, 2)),
    Unit.KILOGRAM: (Fraction(1), Fraction(1, 2), Fraction(1, 10)),
    Unit.MILLILITER: (Fraction(1), Fraction(1, 2)),
    Unit.LITER: (Fraction(1), Fraction(1, 2), Fraction(1, 10)),
    Unit.TEASPOON: (Fraction(1), Fraction(1, 2), Fraction(1, 4)),
    Unit.TABLESPOON: (Fraction(1), Fraction(1, 2), Fraction(1, 4)),
    Unit.GLASS: (Fraction(1), Fraction(1, 2), Fraction(1, 4)),
}


class QuantityStatus(str, Enum):
    """What kind of quantity a line turned out to hold."""

    QUANTIFIED = "quantified"
    QUALITATIVE = "qualitative"
    RANGE = "range"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    UNRESOLVED = "unresolved"


class QuantityReason(str, Enum):
    """The precise machine-readable reason behind a `QuantityStatus`."""

    OK = "ok"
    QUALITATIVE = "qualitative"
    RANGE = "range"
    NEGATIVE = "negative"
    INVALID_NUMBER = "invalid_number"
    UNKNOWN_UNIT = "unknown_unit"
    NO_QUANTITY = "no_quantity"
    COUNTABLE = "countable"
    PACKAGE = "package"
    US_CUP = "us_cup"
    HEAPED = "heaped"
    QUALIFIER_UNSUPPORTED = "qualifier_unsupported"
    INVALID_GLASS_SIZE = "invalid_glass_size"
    MIXED_DIMENSIONS = "mixed_dimensions"
    AMBIGUOUS_JUXTAPOSITION = "ambiguous_juxtaposition"


class ConversionStatus(str, Enum):
    """The outcome family of one conversion, for a future Russian UX layer."""

    EXACT = "exact"
    NOMINAL_HOUSEHOLD = "nominal_household"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class ConversionReason(str, Enum):
    """The precise machine-readable reason behind a `ConversionStatus`."""

    CONVERTED = "converted"
    SOURCE_NOT_QUANTIFIED = "source_not_quantified"
    GLASS_SIZE_REQUIRED = "glass_size_required"
    GLASS_SIZE_INVALID = "glass_size_invalid"
    GLASS_SIZE_CONFLICT = "glass_size_conflict"
    MASS_VOLUME_COEFFICIENT_REQUIRED = "mass_volume_coefficient_required"


class MeasurementBasis(str, Enum):
    """Whether a conversion rests on defined metric relations or on nominal volumes.

    DEFINED is an exact unit relation (1 kg = 1000 g). NOMINAL_HOUSEHOLD says a
    household volume is involved: the number is arithmetically exact for the declared,
    conventional or configured capacities, but that is not a claim that a real spoonful
    or glass holds exactly that much.
    """

    DEFINED = "defined"
    NOMINAL_HOUSEHOLD = "nominal_household"


@dataclass(frozen=True)
class QuantityPart:
    """One parsed term: an amount, its unit, and a glass size when the text declared it.

    A declared glass capacity is kept exactly: a whole millilitre stays an `int`, and a
    fractional declaration such as `237,5 ml` is kept as a `Fraction` without rounding.
    """

    amount: Fraction
    unit: Unit
    glass_ml: int | Fraction | None = None


@dataclass(frozen=True)
class Quantity:
    """One ingredient amount as recognized, with the original text untouched.

    `parts` is empty unless `status` is QUANTIFIED. A quantified quantity has at least
    one part; every part shares `dimension`. A glass part may carry its declared size;
    an undeclared glass size is resolved only at conversion time, never here.
    """

    text: str
    status: QuantityStatus
    reason: QuantityReason
    dimension: Dimension | None = None
    parts: tuple[QuantityPart, ...] = ()

    @property
    def is_quantified(self) -> bool:
        return self.status is QuantityStatus.QUANTIFIED


@dataclass(frozen=True)
class RoundedAmount:
    """A practical presentation of an unrounded target, with its exact relative error."""

    amount: Fraction
    unit: Unit
    relative_error: Fraction


def _measure_volume(
    name: str,
    value: int | Fraction | None,
    minimum: Fraction,
    maximum: Fraction,
) -> int | Fraction | None:
    """Validate one household capacity and keep it exact.

    Only `int` and `Fraction` are exact enough to be a capacity; `bool` is refused even
    though it is an `int` subclass, and floats, decimals and strings are refused rather
    than silently rounded. A value outside its approved inclusive range is refused, never
    clamped to the nearest bound. A whole millilitre stays an `int`; only a genuinely
    fractional capacity is kept as a `Fraction`.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, Fraction)):
        raise TypeError(f"{name} must be an int, a Fraction or None")
    volume = Fraction(value)
    if not minimum <= volume <= maximum:
        raise ValueError(f"{name} must be within {minimum} and {maximum} ml")
    return int(volume) if volume.denominator == 1 else volume


@dataclass(frozen=True)
class MeasureVolumes:
    """Household capacities for one measurement context, in millilitres.

    The same value type describes both sides of a conversion, but the sides never borrow
    from each other. On the source side a `None` field means "not reliably established":
    an ordinary level spoon still uses the conventional 5/15 mL, while an unstated glass
    capacity stays unknown and is never filled from the target side. On the target side
    `resolve_target_measures` replaces every `None` with the system default.

    Values are exact rationals within the approved inclusive ranges (D18/D19); the
    context is immutable and no cross-field rule relates the three capacities.
    """

    glass_ml: int | Fraction | None = None
    teaspoon_ml: int | Fraction | None = None
    tablespoon_ml: int | Fraction | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "glass_ml",
            _measure_volume("glass_ml", self.glass_ml, GLASS_ML_MIN, GLASS_ML_MAX),
        )
        object.__setattr__(
            self,
            "teaspoon_ml",
            _measure_volume(
                "teaspoon_ml", self.teaspoon_ml, TEASPOON_ML_MIN, TEASPOON_ML_MAX
            ),
        )
        object.__setattr__(
            self,
            "tablespoon_ml",
            _measure_volume(
                "tablespoon_ml",
                self.tablespoon_ml,
                TABLESPOON_ML_MIN,
                TABLESPOON_ML_MAX,
            ),
        )


def resolve_target_measures(
    overrides: MeasureVolumes | None = None,
) -> MeasureVolumes:
    """Target household capacities with the system defaults filled in.

    Only target presentation uses this resolver. It is deliberately never applied to a
    source recipe: a missing source glass capacity must stay unknown and require
    clarification instead of being resolved from a default or a personal setting.
    """
    if overrides is None:
        return MeasureVolumes(
            SYSTEM_GLASS_ML, SYSTEM_TEASPOON_ML, SYSTEM_TABLESPOON_ML
        )
    if not isinstance(overrides, MeasureVolumes):
        raise TypeError("overrides must be a MeasureVolumes or None")
    return MeasureVolumes(
        SYSTEM_GLASS_ML if overrides.glass_ml is None else overrides.glass_ml,
        SYSTEM_TEASPOON_ML
        if overrides.teaspoon_ml is None
        else overrides.teaspoon_ml,
        SYSTEM_TABLESPOON_ML
        if overrides.tablespoon_ml is None
        else overrides.tablespoon_ml,
    )


@dataclass(frozen=True)
class ConversionResult:
    """One conversion attempt: its outcome family, context and values.

    On success `exact_amount` is the unrounded target and `rounded` is its practical
    presentation, or None when no representation passed the 5% gate. `glass_sizes_ml`
    records the glass capacity or capacities actually used, exactly: whole millilitres
    stay `int`, and only a fractional capacity appears as a `Fraction`. On failure every
    value is None and `reason` says why; the source Quantity keeps its own reason too.
    """

    status: ConversionStatus
    reason: ConversionReason
    source: Quantity
    target_unit: Unit
    basis: MeasurementBasis | None = None
    exact_amount: Fraction | None = None
    rounded: RoundedAmount | None = None
    glass_sizes_ml: tuple[int | Fraction, ...] = ()

    @property
    def is_success(self) -> bool:
        return self.status in (
            ConversionStatus.EXACT,
            ConversionStatus.NOMINAL_HOUSEHOLD,
        )


# --- numbers -------------------------------------------------------------------------

# The common vulgar fractions, as exact rationals. They are never read as floats.
_VULGAR_FRACTIONS = {
    "¼": Fraction(1, 4),  # ¼
    "½": Fraction(1, 2),  # ½
    "¾": Fraction(3, 4),  # ¾
    "⅐": Fraction(1, 7),  # ⅐
    "⅑": Fraction(1, 9),  # ⅑
    "⅒": Fraction(1, 10),  # ⅒
    "⅓": Fraction(1, 3),  # ⅓
    "⅔": Fraction(2, 3),  # ⅔
    "⅕": Fraction(1, 5),  # ⅕
    "⅖": Fraction(2, 5),  # ⅖
    "⅗": Fraction(3, 5),  # ⅗
    "⅘": Fraction(4, 5),  # ⅘
    "⅙": Fraction(1, 6),  # ⅙
    "⅚": Fraction(5, 6),  # ⅚
    "⅛": Fraction(1, 8),  # ⅛
    "⅜": Fraction(3, 8),  # ⅜
    "⅝": Fraction(5, 8),  # ⅝
    "⅞": Fraction(7, 8),  # ⅞
}
_VULGAR_CHARS = "".join(_VULGAR_FRACTIONS)

_INTEGER = r"[0-9]+"

# Our own bound on one matched numeric token. It is far beyond any kitchen amount or
# practical precision; its purpose is that an oversized line is classified by this
# module rather than by the interpreter-wide integer-string conversion limit, which
# differs between Python versions.
_MAX_NUMBER_DIGITS = 64

_NUMBER_PATTERN = (
    rf"(?:"
    rf"(?P<mixed_whole>{_INTEGER})\s+"
    rf"(?P<mixed_numerator>{_INTEGER})/(?P<mixed_denominator>{_INTEGER})"
    rf"|(?P<vulgar_whole>{_INTEGER})\s*(?P<vulgar_mixed>[{_VULGAR_CHARS}])"
    rf"|(?P<vulgar_only>[{_VULGAR_CHARS}])"
    rf"|(?P<fraction_numerator>{_INTEGER})/(?P<fraction_denominator>{_INTEGER})"
    rf"|(?P<decimal>{_INTEGER}[.,]{_INTEGER})"
    rf"|(?P<integer>{_INTEGER})"
    rf")"
)

# Russian unit spellings. Longer forms stand first so a word is never cut short when a
# complete form follows; the whole line must still be consumed for a quantity to parse.
_UNIT_PATTERN = (
    r"(?:"
    r"грамм(?:ов|а)?|гр\.?|г\.?"
    r"|килограмм(?:ов|а)?|кг\.?"
    r"|миллилитр(?:ов|а)?|мл\.?"
    r"|литр(?:ов|а)?|л\.?"
    r"|ч\.?\s*(?:лож(?:к(?:а|и|у)|ек)|л\.?)"
    r"|чайная\s+ложка|чайной\s+ложки|чайную\s+ложку"
    r"|чайные\s+ложки|чайных\s+ложек"
    r"|ст\.?\s*(?:лож(?:к(?:а|и|у)|ек)|л\.?)"
    r"|столовая\s+ложка|столовой\s+ложки|столовую\s+ложку"
    r"|столовые\s+ложки|столовых\s+ложек"
    r"|стакан(?:ов|а)?"
    r")"
)

_TERM_HEAD = re.compile(
    rf"\s*(?P<number>{_NUMBER_PATTERN})\s*(?P<unit_text>{_UNIT_PATTERN})",
    re.IGNORECASE,
)

# A glass may state its own capacity: "1 стакан 200 мл", "1 стакан (250 мл)",
# "1 стакан на 200 мл". Legacy parsing accepts only the two defined sizes; the opt-in
# mode accepts any exact capacity within the approved 50--1000 mL fence. Parentheses
# must be balanced: a declaration is either wholly parenthesized or wholly bare, so a
# stray delimiter is left unaccounted and refuses the line instead of being guessed away.
_GLASS_SIZE_AMOUNT = (
    r"(?P<size>[0-9]+(?:[.,][0-9]+)?)\s*(?:мл\.?|миллилитр(?:а|ов)?)"
)
_GLASS_SIZE_PARENTHESIZED = re.compile(
    rf"\s*(?:на\s+)?\(\s*{_GLASS_SIZE_AMOUNT}\s*\)",
    re.IGNORECASE,
)
_GLASS_SIZE_BARE = re.compile(
    rf"\s*(?:на\s+)?{_GLASS_SIZE_AMOUNT}",
    re.IGNORECASE,
)

# A level spoon is the nominal definition; heaped is not evidenced and is reported.
_QUALIFIER = re.compile(
    r"\s*(?P<qualifier>без\s+горки|с\s+горкой|с\s+верхом)",
    re.IGNORECASE,
)

_AND = re.compile(r"(?:и|плюс)(?![\w])", re.IGNORECASE)

_NEGATIVE = re.compile(r"^[-−]\s*[0-9]")
_RANGE = re.compile(
    r"^(?:от\s+)?[0-9]+(?:[.,][0-9]+)?\s*"
    r"(?:до|[–—−-])\s*[0-9]+(?:[.,][0-9]+)?",
    re.IGNORECASE,
)
_HAS_NUMBER = re.compile(rf"[0-9{_VULGAR_CHARS}]")
_HEAPED = re.compile(r"\bс\s+(?:горкой|верхом)\b", re.IGNORECASE)
_US_CUP = re.compile(r"\bcups?\b|чашк", re.IGNORECASE)
_COUNTABLE = re.compile(
    r"\bшт\b|штук|зубчик|головк|ломтик",
    re.IGNORECASE,
)
_PACKAGE = re.compile(r"пачк|банк|баночк|упаковк", re.IGNORECASE)

_QUALITATIVE = frozenset(
    {"по вкусу", "щепотка", "щепотку", "щепоть", "немного", "на глаз"}
)

# Non-breaking spaces are ordinary separators in prose and in pasted recipe text.
_SPACE_TRANSLATION = str.maketrans({chr(0x00A0): " ", chr(0x202F): " "})

# Matching treats the two Russian spellings of "e" alike. The letter is addressed by
# code point because no literal yo belongs in a source file that the orthography scan
# reads (tests/test_orthography.py); this module shows no text to anyone.
_YO_LETTER = chr(0x0451)

# Normalized spellings that map to one unit. `_unit_key` removes spaces and dots, so
# "ч. л.", "ч.л." and "ч л" all arrive here as "чл".
_UNIT_KEYS = {
    "г": Unit.GRAM,
    "гр": Unit.GRAM,
    "грамм": Unit.GRAM,
    "грамма": Unit.GRAM,
    "граммов": Unit.GRAM,
    "кг": Unit.KILOGRAM,
    "килограмм": Unit.KILOGRAM,
    "килограмма": Unit.KILOGRAM,
    "килограммов": Unit.KILOGRAM,
    "мл": Unit.MILLILITER,
    "миллилитр": Unit.MILLILITER,
    "миллилитра": Unit.MILLILITER,
    "миллилитров": Unit.MILLILITER,
    "л": Unit.LITER,
    "литр": Unit.LITER,
    "литра": Unit.LITER,
    "литров": Unit.LITER,
    "чл": Unit.TEASPOON,
    "чложка": Unit.TEASPOON,
    "чложки": Unit.TEASPOON,
    "чложек": Unit.TEASPOON,
    "чложку": Unit.TEASPOON,
    "чайнаяложка": Unit.TEASPOON,
    "чайнойложки": Unit.TEASPOON,
    "чайнуюложку": Unit.TEASPOON,
    "чайныеложки": Unit.TEASPOON,
    "чайныхложек": Unit.TEASPOON,
    "стл": Unit.TABLESPOON,
    "стложка": Unit.TABLESPOON,
    "стложки": Unit.TABLESPOON,
    "стложек": Unit.TABLESPOON,
    "стложку": Unit.TABLESPOON,
    "столоваяложка": Unit.TABLESPOON,
    "столовойложки": Unit.TABLESPOON,
    "столовуюложку": Unit.TABLESPOON,
    "столовыеложки": Unit.TABLESPOON,
    "столовыхложек": Unit.TABLESPOON,
    "стакан": Unit.GLASS,
    "стакана": Unit.GLASS,
    "стаканов": Unit.GLASS,
}

# Every reason a term scan can report, mapped to its outcome family. The map is total
# over the scan's failures: an unlisted reason still yields an unsuccessful result
# (below), so no malformed line can turn a parser detail into an exception.
_FAILURE_STATUS = {
    QuantityReason.UNKNOWN_UNIT: QuantityStatus.UNRESOLVED,
    QuantityReason.INVALID_NUMBER: QuantityStatus.UNRESOLVED,
    QuantityReason.NO_QUANTITY: QuantityStatus.UNRESOLVED,
    QuantityReason.QUALIFIER_UNSUPPORTED: QuantityStatus.UNRESOLVED,
    QuantityReason.HEAPED: QuantityStatus.UNSUPPORTED,
    QuantityReason.INVALID_GLASS_SIZE: QuantityStatus.UNSUPPORTED,
    QuantityReason.MIXED_DIMENSIONS: QuantityStatus.AMBIGUOUS,
    QuantityReason.AMBIGUOUS_JUXTAPOSITION: QuantityStatus.AMBIGUOUS,
}


class _InvalidNumber(ValueError):
    pass


def _bounded_int(digits: str) -> int:
    """An integer from a matched digit run, within this module's own length bound.

    The bound is local on purpose: an oversized token is a malformed amount, which must
    be refused the same way on every supported Python version, rather than raising the
    interpreter's integer-string conversion error.
    """
    if len(digits) > _MAX_NUMBER_DIGITS:
        raise _InvalidNumber("a number has too many digits")
    return int(digits)


def _normalize(text: str) -> str:
    """The matching form of an amount: lower case, single spaces, no non-breaking space.

    Only matching uses this; the original string is what a Quantity keeps.
    """
    text = text.translate(_SPACE_TRANSLATION)
    text = text.lower().replace(_YO_LETTER, "е")
    return re.sub(r"\s+", " ", text).strip()


def _unit_key(text: str) -> str:
    return re.sub(r"\s+", "", text.lower().replace(_YO_LETTER, "е")).replace(".", "")


def _decimal_fraction(text: str) -> Fraction:
    """An exact rational from a decimal written with a comma or a point."""
    whole, _, fractional = text.replace(",", ".").partition(".")
    numerator = _bounded_int(whole + fractional)
    return Fraction(numerator, 10 ** len(fractional))


def _number_value(groups: dict[str, str | None]) -> Fraction:
    """The exact rational behind one matched number, whatever its written form."""
    if groups["mixed_whole"] is not None:
        denominator = _bounded_int(groups["mixed_denominator"] or "0")
        if denominator == 0:
            raise _InvalidNumber("a fraction cannot have a zero denominator")
        return Fraction(_bounded_int(groups["mixed_whole"])) + Fraction(
            _bounded_int(groups["mixed_numerator"] or "0"), denominator
        )
    if groups["vulgar_whole"] is not None:
        return Fraction(_bounded_int(groups["vulgar_whole"])) + _VULGAR_FRACTIONS[
            groups["vulgar_mixed"] or ""
        ]
    if groups["vulgar_only"] is not None:
        return _VULGAR_FRACTIONS[groups["vulgar_only"] or ""]
    if groups["fraction_numerator"] is not None:
        denominator = _bounded_int(groups["fraction_denominator"] or "0")
        if denominator == 0:
            raise _InvalidNumber("a fraction cannot have a zero denominator")
        return Fraction(_bounded_int(groups["fraction_numerator"]), denominator)
    if groups["decimal"] is not None:
        return _decimal_fraction(groups["decimal"])
    if groups["integer"] is not None:
        return Fraction(_bounded_int(groups["integer"]))
    raise _InvalidNumber("no number was matched")


def _read_term(
    text: str,
    position: int,
    allow_nonstandard_glass_sizes: bool,
) -> tuple[QuantityPart | None, int, QuantityReason | None]:
    """Read one `number + unit` term at `position`.

    Returns the part and the position after it, or None and the reason the term could
    not be accepted. `text` is already normalized. A declared glass capacity is kept
    exactly: a whole millilitre as an `int`, a fractional one as a `Fraction`. The
    legacy 200/250 fence applies unless the caller opted in to nonstandard explicit
    sizes.
    """
    match = _TERM_HEAD.match(text, position)
    if match is None:
        return None, position, QuantityReason.UNKNOWN_UNIT

    unit = _UNIT_KEYS.get(_unit_key(match.group("unit_text")))
    if unit is None:
        return None, position, QuantityReason.UNKNOWN_UNIT

    try:
        amount = _number_value(match.groupdict())
    except _InvalidNumber:
        return None, position, QuantityReason.INVALID_NUMBER

    end = match.end()
    glass_ml = None
    if unit is Unit.GLASS:
        size_match = _GLASS_SIZE_PARENTHESIZED.match(text, end)
        if size_match is None:
            size_match = _GLASS_SIZE_BARE.match(text, end)
        if size_match is not None:
            try:
                size = _decimal_fraction(size_match.group("size"))
            except _InvalidNumber:
                return None, position, QuantityReason.INVALID_NUMBER
            if allow_nonstandard_glass_sizes:
                if not GLASS_ML_MIN <= size <= GLASS_ML_MAX:
                    return None, position, QuantityReason.INVALID_GLASS_SIZE
            elif size not in GLASS_SIZES_ML:
                return None, position, QuantityReason.INVALID_GLASS_SIZE
            glass_ml = int(size) if size.denominator == 1 else size
            end = size_match.end()

    qualifier = _QUALIFIER.match(text, end)
    if qualifier is not None:
        if qualifier.group("qualifier") in {"с горкой", "с верхом"}:
            return None, position, QuantityReason.HEAPED
        if unit not in (Unit.TEASPOON, Unit.TABLESPOON):
            return None, position, QuantityReason.QUALIFIER_UNSUPPORTED
        end = qualifier.end()

    # One trailing sentence period belongs to ordinary prose, not to the unit.
    if text.startswith(".", end):
        end += 1

    return QuantityPart(amount=amount, unit=unit, glass_ml=glass_ml), end, None


def _scan_terms(
    text: str, allow_nonstandard_glass_sizes: bool
) -> tuple[tuple[QuantityPart, ...], QuantityReason | None, Dimension | None]:
    """Read a whole quantity expression, one term at a time.

    Terms may be joined by `+` or `и`; two metric terms may also stand next to each
    other ("1 кг 200 г"). Every term must share one dimension. Nothing is inferred from
    a partial match: if any character is not accounted for, the expression is refused.
    """
    parts: list[QuantityPart] = []
    dimension: Dimension | None = None
    position = 0
    previous_metric = False
    pending_separator = False

    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            if pending_separator:
                return (), QuantityReason.UNKNOWN_UNIT, None
            break

        implicit = False
        if parts:
            and_match = _AND.match(text, position)
            if text.startswith("+", position):
                position += 1
                pending_separator = True
            elif and_match is not None:
                position = and_match.end()
                pending_separator = True
            elif text[position].isdigit():
                implicit = True
            else:
                return (), QuantityReason.UNKNOWN_UNIT, None
            while position < len(text) and text[position].isspace():
                position += 1
            if position >= len(text):
                return (), QuantityReason.UNKNOWN_UNIT, None

        part, end, failure = _read_term(
            text, position, allow_nonstandard_glass_sizes
        )
        if failure is not None or part is None:
            return (), failure or QuantityReason.UNKNOWN_UNIT, None

        if dimension is None:
            dimension = part.unit.dimension
        elif part.unit.dimension is not dimension:
            return (), QuantityReason.MIXED_DIMENSIONS, None

        if implicit and not (previous_metric and part.unit in _METRIC_UNITS):
            return (), QuantityReason.AMBIGUOUS_JUXTAPOSITION, None

        parts.append(part)
        previous_metric = part.unit in _METRIC_UNITS
        pending_separator = False
        position = end

    if not parts:
        return (), QuantityReason.NO_QUANTITY, None
    return tuple(parts), None, dimension


def parse_quantity(
    text: str, *, allow_nonstandard_glass_sizes: bool = False
) -> Quantity:
    """Recognize one amount, keeping its original text exactly.

    A line is quantified only when it is entirely an explicit measurement. A range is
    never averaged, a qualitative phrase is never given a number, and an unsupported
    or ambiguous line is left as it was with a reason that says what was wrong.

    With the default `False`, a declared glass capacity is still restricted to the
    legacy 200/250 mL sizes and every other behavior is unchanged. With `True`, an
    explicitly declared capacity within the approved 50--1000 mL range is accepted
    exactly, including a fractional one; a bare `1 стакан` still has no capacity.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")

    normalized = _normalize(text)
    if not normalized:
        return Quantity(text, QuantityStatus.UNRESOLVED, QuantityReason.NO_QUANTITY)
    if normalized.rstrip(" .") in _QUALITATIVE:
        return Quantity(text, QuantityStatus.QUALITATIVE, QuantityReason.QUALITATIVE)
    if _HAS_NUMBER.search(normalized) is None:
        return Quantity(text, QuantityStatus.UNRESOLVED, QuantityReason.NO_QUANTITY)
    if _NEGATIVE.match(normalized):
        return Quantity(text, QuantityStatus.UNRESOLVED, QuantityReason.NEGATIVE)
    if _RANGE.match(normalized):
        return Quantity(text, QuantityStatus.RANGE, QuantityReason.RANGE)
    if _HEAPED.search(normalized):
        return Quantity(text, QuantityStatus.UNSUPPORTED, QuantityReason.HEAPED)
    if _US_CUP.search(normalized):
        return Quantity(text, QuantityStatus.UNSUPPORTED, QuantityReason.US_CUP)
    if _COUNTABLE.search(normalized):
        return Quantity(text, QuantityStatus.UNSUPPORTED, QuantityReason.COUNTABLE)
    if _PACKAGE.search(normalized):
        return Quantity(text, QuantityStatus.UNSUPPORTED, QuantityReason.PACKAGE)

    parts, failure, dimension = _scan_terms(
        normalized, allow_nonstandard_glass_sizes
    )
    if failure is not None:
        # A scan failure always yields an unsuccessful Quantity; an unlisted reason is
        # still reported as unresolved instead of raising, and the reason is preserved.
        status = _FAILURE_STATUS.get(failure, QuantityStatus.UNRESOLVED)
        return Quantity(text, status, failure)
    return Quantity(
        text, QuantityStatus.QUANTIFIED, QuantityReason.OK, dimension, parts
    )


# --- rounding ------------------------------------------------------------------------


def practical_round(amount: Fraction | int, unit: Unit) -> RoundedAmount | None:
    """The coarsest practical presentation within the 5% additional-error gate.

    Returns None when no practical step passes: the caller should then keep the
    unrounded value rather than show a misleading hint. Zero rounds to zero; a negative
    amount is not a measurement and is refused.
    """
    if not isinstance(amount, (int, Fraction)):
        raise TypeError("amount must be an int or a Fraction")
    if not isinstance(unit, Unit):
        raise TypeError("unit must be a Unit")
    value = Fraction(amount)
    if value < 0:
        raise ValueError("a quantity cannot be negative")
    if value == 0:
        return RoundedAmount(Fraction(0), unit, Fraction(0))

    for step in _PRACTICAL_STEPS[unit]:
        # Nearest multiple of the step, halves rounded up; every value is non-negative.
        multiplier = int(value / step + Fraction(1, 2))
        candidate = step * multiplier
        error = abs(candidate - value) / value
        if error <= MAX_ROUNDING_RELATIVE_ERROR:
            return RoundedAmount(candidate, unit, error)
    return None


# --- conversion ----------------------------------------------------------------------


def _failure(
    quantity: Quantity,
    target_unit: Unit,
    status: ConversionStatus,
    reason: ConversionReason,
) -> ConversionResult:
    return ConversionResult(
        status=status,
        reason=reason,
        source=quantity,
        target_unit=target_unit,
    )


_SOURCE_FAILURE_STATUS = {
    QuantityStatus.QUALITATIVE: ConversionStatus.UNRESOLVED,
    QuantityStatus.RANGE: ConversionStatus.AMBIGUOUS,
    QuantityStatus.AMBIGUOUS: ConversionStatus.AMBIGUOUS,
    QuantityStatus.UNSUPPORTED: ConversionStatus.UNSUPPORTED,
    QuantityStatus.UNRESOLVED: ConversionStatus.UNRESOLVED,
}


def convert_quantity(
    quantity: Quantity,
    target_unit: Unit,
    *,
    glass_ml: int | None = None,
    source_measures: MeasureVolumes | None = None,
    target_measures: MeasureVolumes | None = None,
) -> ConversionResult:
    """Convert one parsed quantity into `target_unit`.

    Exact metric conversions are EXACT; any conversion that involves a household
    measure is NOMINAL_HOUSEHOLD.

    The legacy `glass_ml` argument keeps the KH-F01-A contract: it is the single glass
    capacity for this call, it must be 200 or 250 mL when a glass is involved, and it
    cannot be combined with `source_measures` or `target_measures`. With the contexts,
    `source_measures` describes what is reliably established about the source recipe and
    `target_measures` the household the result is presented in. Missing target
    capacities resolve to their system defaults (250/5/15 mL); missing source capacities
    never do. A source glass without a declaration or reliable metadata still asks for
    clarification even when a target glass is known, and target measures never
    reinterpret a source spoon, a declared glass or an unknown capacity.

    A size written in the text wins for that glass; a context that disagrees with any
    declared size is reported as a conflict rather than silently overriding either
    value, so one matching declaration cannot hide another that contradicts the context.
    Mass and volume are not converted into each other -- no ingredient coefficient is
    approved in this increment. Failures never prevent other quantities from converting;
    this function has no state and mutates nothing.
    """
    if not isinstance(quantity, Quantity):
        raise TypeError("quantity must be a Quantity")
    if not isinstance(target_unit, Unit):
        raise TypeError("target_unit must be a Unit")
    if glass_ml is not None and not isinstance(glass_ml, int):
        raise TypeError("glass_ml must be an int or None")
    if source_measures is not None and not isinstance(source_measures, MeasureVolumes):
        raise TypeError("source_measures must be a MeasureVolumes or None")
    if target_measures is not None and not isinstance(target_measures, MeasureVolumes):
        raise TypeError("target_measures must be a MeasureVolumes or None")
    if glass_ml is not None and (
        source_measures is not None or target_measures is not None
    ):
        raise ValueError(
            "glass_ml cannot be combined with source_measures or target_measures"
        )

    if (
        quantity.status is not QuantityStatus.QUANTIFIED
        or quantity.dimension is None
        or not quantity.parts
    ):
        return _failure(
            quantity,
            target_unit,
            _SOURCE_FAILURE_STATUS.get(
                quantity.status, ConversionStatus.UNRESOLVED
            ),
            ConversionReason.SOURCE_NOT_QUANTIFIED,
        )

    if target_unit.dimension is not quantity.dimension:
        return _failure(
            quantity,
            target_unit,
            ConversionStatus.UNSUPPORTED,
            ConversionReason.MASS_VOLUME_COEFFICIENT_REQUIRED,
        )

    declared_sizes = {
        part.glass_ml
        for part in quantity.parts
        if part.unit is Unit.GLASS and part.glass_ml is not None
    }
    undeclared_glass = any(
        part.unit is Unit.GLASS and part.glass_ml is None
        for part in quantity.parts
    )
    target_is_glass = target_unit is Unit.GLASS

    # The target side is resolved explicitly, and only there: the system defaults fill
    # missing target capacities but never a source one.
    resolved_target = (
        resolve_target_measures(target_measures)
        if target_measures is not None
        else None
    )
    source_glass_ml = (
        source_measures.glass_ml if source_measures is not None else None
    )

    if glass_ml is not None:
        # The legacy KH-F01-A branch, unchanged: its single capacity must be one of the
        # two defined glass sizes and must agree with every declaration in the text.
        if undeclared_glass or target_is_glass:
            if glass_ml not in GLASS_SIZES_ML:
                return _failure(
                    quantity,
                    target_unit,
                    ConversionStatus.CLARIFICATION_REQUIRED,
                    ConversionReason.GLASS_SIZE_INVALID,
                )
        # The contextual size must agree with every declared capacity, not merely with
        # one of them: a matching declaration must not conceal a conflicting one.
        # Declarations are compared as a set, so order and duplicates do not matter,
        # and a zero-valued term still declares its glass.
        if declared_sizes - {glass_ml}:
            return _failure(
                quantity,
                target_unit,
                ConversionStatus.CLARIFICATION_REQUIRED,
                ConversionReason.GLASS_SIZE_CONFLICT,
            )
    else:
        # A declared capacity must agree with reliable source metadata. An undeclared
        # source glass is never filled from the target side or from the system default.
        if source_glass_ml is not None and (declared_sizes - {source_glass_ml}):
            return _failure(
                quantity,
                target_unit,
                ConversionStatus.CLARIFICATION_REQUIRED,
                ConversionReason.GLASS_SIZE_CONFLICT,
            )
        if undeclared_glass and source_glass_ml is None:
            return _failure(
                quantity,
                target_unit,
                ConversionStatus.CLARIFICATION_REQUIRED,
                ConversionReason.GLASS_SIZE_REQUIRED,
            )
        if target_is_glass and resolved_target is None:
            return _failure(
                quantity,
                target_unit,
                ConversionStatus.CLARIFICATION_REQUIRED,
                ConversionReason.GLASS_SIZE_REQUIRED,
            )

    source_teaspoon_ml = Fraction(5)
    source_tablespoon_ml = Fraction(15)
    if source_measures is not None:
        if source_measures.teaspoon_ml is not None:
            source_teaspoon_ml = source_measures.teaspoon_ml
        if source_measures.tablespoon_ml is not None:
            source_tablespoon_ml = source_measures.tablespoon_ml

    sizes_used: set[int | Fraction] = set()

    def source_factor(part: QuantityPart) -> Fraction:
        unit = part.unit
        if unit is Unit.GRAM or unit is Unit.MILLILITER:
            return Fraction(1)
        if unit is Unit.KILOGRAM or unit is Unit.LITER:
            return Fraction(1000)
        if unit is Unit.TEASPOON:
            return source_teaspoon_ml
        if unit is Unit.TABLESPOON:
            return source_tablespoon_ml
        if unit is Unit.GLASS:
            if part.glass_ml is not None:
                size = part.glass_ml
            elif source_glass_ml is not None:
                size = source_glass_ml
            elif glass_ml is not None:
                size = glass_ml
            else:
                raise RuntimeError("a glass size must be resolved before conversion")
            sizes_used.add(size)
            return Fraction(size)
        raise RuntimeError(f"unhandled unit {unit!r}")

    def target_factor(unit: Unit) -> Fraction:
        if unit is Unit.GRAM or unit is Unit.MILLILITER:
            return Fraction(1)
        if unit is Unit.KILOGRAM or unit is Unit.LITER:
            return Fraction(1000)
        if unit is Unit.TEASPOON:
            return (
                resolved_target.teaspoon_ml
                if resolved_target is not None
                else Fraction(5)
            )
        if unit is Unit.TABLESPOON:
            return (
                resolved_target.tablespoon_ml
                if resolved_target is not None
                else Fraction(15)
            )
        if unit is Unit.GLASS:
            if resolved_target is not None:
                size = resolved_target.glass_ml
                if size is None:
                    raise RuntimeError("a target glass size must be resolved")
            elif glass_ml is not None:
                size = glass_ml
            else:
                raise RuntimeError("a glass size must be resolved before conversion")
            sizes_used.add(size)
            return Fraction(size)
        raise RuntimeError(f"unhandled unit {unit!r}")

    base_amount = sum(
        (part.amount * source_factor(part) for part in quantity.parts),
        Fraction(0),
    )
    exact_amount = base_amount / target_factor(target_unit)

    nominal = target_unit in _HOUSEHOLD_UNITS or any(
        part.unit in _HOUSEHOLD_UNITS for part in quantity.parts
    )
    return ConversionResult(
        status=(
            ConversionStatus.NOMINAL_HOUSEHOLD
            if nominal
            else ConversionStatus.EXACT
        ),
        reason=ConversionReason.CONVERTED,
        source=quantity,
        target_unit=target_unit,
        basis=(
            MeasurementBasis.NOMINAL_HOUSEHOLD
            if nominal
            else MeasurementBasis.DEFINED
        ),
        exact_amount=exact_amount,
        rounded=practical_round(exact_amount, target_unit),
        glass_sizes_ml=tuple(sorted(sizes_used)),
    )


def convert_text(
    text: str,
    target_unit: Unit,
    *,
    glass_ml: int | None = None,
    source_measures: MeasureVolumes | None = None,
    target_measures: MeasureVolumes | None = None,
    allow_nonstandard_glass_sizes: bool = False,
) -> ConversionResult:
    """Parse one amount and convert it, in one call. The text is never modified.

    `allow_nonstandard_glass_sizes` is the parser opt-in; it changes only how an
    explicitly declared source glass capacity is read and defaults to legacy behavior.
    """
    return convert_quantity(
        parse_quantity(
            text, allow_nonstandard_glass_sizes=allow_nonstandard_glass_sizes
        ),
        target_unit,
        glass_ml=glass_ml,
        source_measures=source_measures,
        target_measures=target_measures,
    )


__all__ = [
    "GLASS_ML_MAX",
    "GLASS_ML_MIN",
    "GLASS_SIZES_ML",
    "MAX_ROUNDING_RELATIVE_ERROR",
    "SYSTEM_GLASS_ML",
    "SYSTEM_TABLESPOON_ML",
    "SYSTEM_TEASPOON_ML",
    "TABLESPOON_ML_MAX",
    "TABLESPOON_ML_MIN",
    "TEASPOON_ML_MAX",
    "TEASPOON_ML_MIN",
    "ConversionReason",
    "ConversionResult",
    "ConversionStatus",
    "Dimension",
    "MeasurementBasis",
    "MeasureVolumes",
    "Quantity",
    "QuantityPart",
    "QuantityReason",
    "QuantityStatus",
    "RoundedAmount",
    "Unit",
    "convert_quantity",
    "convert_text",
    "parse_quantity",
    "practical_round",
    "resolve_target_measures",
]
