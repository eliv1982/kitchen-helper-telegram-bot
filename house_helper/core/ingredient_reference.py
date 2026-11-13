"""KH-F01-B2-1: ingredient-specific reference data and deterministic mass conversion.

Mass and volume are never converted into each other by the measurement core; the
coefficient is a property of one ingredient in one form. This module is that
coefficient, made explicit: a versioned registry, an exact-name matcher, two arithmetic
paths, and a closed set of reasons for every refusal.

Two paths
---------
* **Mass to volume.** A mass becomes a count of the person's own glass, tablespoon or
  teaspoon through an ingredient-specific reference density (`grams_per_ml`). A published
  source cup is normalized to 240 mL (NIST) and the normalization travels with the
  record; the original source amount, unit and volume are kept in the evidence. The
  arithmetic is exact and `core.measures.practical_round` applies the accepted 5% gate
  once, at presentation. Glass and tablespoon are offered only at one or more; the
  terminal teaspoon fallback may present a half or a quarter, because no larger unit can
  express them. The original recipe text and stored amount are never touched.
* **Mass to count.** A mass becomes a number of individual products through a piece
  reference. Three kinds stay apart: `POINT` (one measured or source-representative
  weight -- a single count, never an interval), `MEASURED_INTERVAL` (a source-stated
  range of one piece -- a real interval), and `CALIBRATION_CLASS` (a supplier's size
  grade -- sizing evidence, used only when the grade is named or explicitly assumed, and
  never combined into a distribution).

What is never done
------------------
No product state is inferred. There are no lexical defaults: a bare generic word is a
`GenericWord`, reported as ambiguous, and never resolves to the ordinary product. No
yield factor turns an edible-portion weight into an as-purchased one. No uncertainty is
fabricated around a point estimate. No similarity, stemming, transliteration or model
chooses a candidate: matching is an exact lookup on a normalized token key, and a tie is
reported, never broken.

Approval
--------
Every record carries a `RecordStatus`, and only `APPROVED` records produce numbers.
`convert_ingredient_mass` is the single supported public conversion gateway: it matches
the name, selects one approved reference, and only then runs the arithmetic. The
arithmetic helpers are private (`_convert_mass_to_volume`, `_convert_mass_to_count`) and
refuse an unapproved reference themselves, so an underscore is not the only thing
standing between an inactive record and a number. This release ships the researched
catalogue with no approved reference at all, so nothing in production converts yet:
activation is an owner decision, and changing a record means a new id plus
`DEPRECATED`/`superseded_by`, never an in-place edit. Tests build their own registries
from synthetic, clearly test-only fixtures.

A refusal is total
------------------
For an unsuccessful conversion `MassConversion.conversion` is always `None`: no exact
count, volume or rounded value travels with a refusal. The reason alone tells the caller
what happened, so raw diagnostics can never be mistaken for an approved result.

Small counts
------------
The presentation grid for pieces bottoms out at a quarter. The exact count is kept
separately (`count_low`/`count_high`) and the presentation lives only in
`rounded_low`/`rounded_high`; there is no weight-dependent exception, so a quarter piece
is presentable against a 200 g reference exactly as against a 201 g one. An exact count
that no grid step can express within the 5% gate is refused (`rounding_gate_failed`),
never shown as zero.

The module is pure Core: standard library plus `core.measures`, no database, web,
Telegram, provider, network or clock. Identical inputs on one registry revision produce
identical results.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction

from house_helper.core.measures import (
    MAX_ROUNDING_RELATIVE_ERROR,
    Dimension,
    MeasureVolumes,
    Quantity,
    RoundedAmount,
    Unit,
    practical_round,
    resolve_target_measures,
)

# The registry revision. It changes whenever any record changes; the canonical digest
# test makes such a change deliberate rather than accidental. The canonical form is part
# of the fingerprinted artifact too: revision ".4" changed the serialization only (a
# typed, field-delimited form), not any shipped measurement.
REGISTRY_REVISION = "2026-11-13.4"

# A published US cup is normalized to 240 mL (NIST, "approximately"), the owner's
# decision OD-B2-1. The normalization is recorded per evidence row.
US_CUP_ML = Fraction(240)

# An interval is shown only when its relative width stays within this bound (OD-B2-5).
MAX_INTERVAL_RELATIVE_WIDTH = Fraction(1, 4)

# The local presentation grid for counts: whole pieces first, then halves, quarters.
# `core.measures.Unit` is deliberately not extended (OD-B2-6a). The quarter is the
# smallest presentable piece for every reference: the floor is a presentation rule, not
# a function of the reference's own weight.
_PIECE_STEPS = (Fraction(1), Fraction(1, 2), Fraction(1, 4))
QUARTER_PIECE = Fraction(1, 4)

# Mass-to-volume target preference (OD-B2-3): the first household unit whose rounded
# count reaches one and passes the 5% gate, otherwise the teaspoon. The teaspoon is the
# terminal fallback and is the one unit allowed to present a half or a quarter.
_PREFERRED_TARGET_UNITS = (Unit.GLASS, Unit.TABLESPOON, Unit.TEASPOON)

# One avoirdupois ounce is 1/16 of 0.45359237 kg exactly. A source stated in ounces is
# converted exactly here and the source amount is kept in its own unit.
_OUNCE_G = Fraction(45359237, 1600000)

UE = chr(0x0451)  # the Russian "yo" letter, spelled as a code point on purpose
_YE = chr(0x0435)


class RecordStatus(str, Enum):
    """Whether a record may produce a number. Only APPROVED does (OD-B2-20)."""

    APPROVED = "approved"
    UNDER_REVIEW = "under_review"
    PENDING_EVIDENCE = "pending_evidence"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"


class MassBasis(str, Enum):
    """What the weight includes."""

    EDIBLE_PORTION = "edible_portion"  # meat only, peeled, cored, drained
    AS_PURCHASED = "as_purchased"  # bone, skin, peel, core or stone included
    # The distinction carries no meaning for a dry pantry product (flour, salt, sugar).
    NOT_APPLICABLE = "not_applicable"


class PieceState(str, Enum):
    """The state the reference weight was measured in."""

    RAW = "raw"
    COOKED = "cooked"
    UNSPECIFIED = "unspecified"


class ReferenceKind(str, Enum):
    """Which claim the evidence supports. The kind decides whether an interval exists."""

    POINT = "point"  # one measured or representative weight
    MEASURED_INTERVAL = "measured_interval"  # a source-stated range of one piece
    CALIBRATION_CLASS = "calibration_class"  # a supplier size grade


class Derivation(str, Enum):
    """How a `grams_per_ml` coefficient came to be."""

    DIRECT_TARGET = "direct_target"
    SOURCE_CUP_NORMALIZED = "source_cup_normalized"


class MatchStatus(str, Enum):
    """What an ingredient name resolved to."""

    MATCHED = "matched"
    GENERIC = "generic"
    AMBIGUOUS = "ambiguous"
    QUALIFIER_CONFLICT = "qualifier_conflict"
    UNSUPPORTED_QUALIFIER = "unsupported_qualifier"
    UNRECOGNIZED = "unrecognized"


class MassConversionStatus(str, Enum):
    """The outcome family of one mass conversion, for the B2-2 payload."""

    REFERENCE_APPROXIMATE = "reference_approximate"
    CLARIFICATION_REQUIRED = "clarification_required"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


class MassConversionReason(str, Enum):
    """The closed machine-readable reason behind a `MassConversionStatus`."""

    CONVERTED = "converted"
    SIZE_CLASS_REQUIRED = "size_class_required"
    GENERIC_NAME = "generic_name"
    INGREDIENT_AMBIGUOUS = "ingredient_ambiguous"
    QUALIFIER_CONFLICT = "qualifier_conflict"
    REFERENCE_VARIANT_CONFLICT = "reference_variant_conflict"
    INGREDIENT_NOT_RECOGNIZED = "ingredient_not_recognized"
    UNSUPPORTED_QUALIFIER = "unsupported_qualifier"
    STATE_MISMATCH = "state_mismatch"
    ZERO_MASS = "zero_mass"
    REFERENCE_MISSING = "reference_missing"
    REFERENCE_NOT_APPROVED = "reference_not_approved"
    ROUNDING_GATE_FAILED = "rounding_gate_failed"
    INTERVAL_TOO_WIDE = "interval_too_wide"
    BELOW_USEFUL_COUNT = "below_useful_count"


class CountDisplay(str, Enum):
    """One number, or a source-stated interval."""

    SINGLE = "single"
    INTERVAL = "interval"


# --- reference records ---------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """One external source. `accessed` is the date the source was read."""

    source_id: str
    publisher: str
    url: str
    accessed: str
    licence: str
    record_id: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class MeasurementEvidence:
    """One verbatim source measurement, in the source's own unit and volume.

    `original_volume_ml` is the volume the source's measure denotes after the accepted
    normalization (a US cup is 240 mL); it is None for a piece weight. The quoted text
    stays as written so a reviewer can check the number against the source without
    trusting this module.
    """

    evidence_id: str
    source_id: str
    quoted: str
    original_amount: Fraction
    original_unit: str
    original_volume_ml: Fraction | None
    mass_g: Fraction
    mass_basis: MassBasis
    method: str
    conditions: str
    limitations: str
    normalization: str | None = None


@dataclass(frozen=True)
class Alias:
    """One exact name, pre-normalized, with the reason it denotes this entry.

    The applicability is mandatory: an alias without a documented reason is a silent
    assumption with extra steps.
    """

    text: str
    applicability: str


@dataclass(frozen=True)
class GenericWord:
    """A known under-specified word. It never resolves; it only names the ambiguity."""

    text: str
    ambiguous_between: tuple[str, ...]


@dataclass(frozen=True)
class IngredientEntry:
    """One product identity: its names, its form, and the reference it may use.

    `accepted_qualifiers` is the closed set of qualifier keys this product may carry
    beyond the words of a matched alias (a size selector, or the edible-basis phrase).
    A qualifier outside that set is reported, never ignored.
    """

    ingredient_id: str
    display_name: str
    category: str
    form: str
    state: PieceState
    mass_basis: MassBasis
    piece_id: str | None
    aliases: tuple[Alias, ...]
    accepted_qualifiers: tuple[str, ...] = ()
    qualifiers_required: tuple[str, ...] = ()
    qualifiers_forbidden: tuple[str, ...] = ()
    status: RecordStatus = RecordStatus.UNDER_REVIEW
    superseded_by: str | None = None


@dataclass(frozen=True)
class VolumeReference:
    """An approved `grams_per_ml` coefficient for one ingredient and one derivation."""

    ingredient_id: str
    evidence_id: str
    grams_per_ml: Fraction
    derivation: Derivation
    status: RecordStatus = RecordStatus.UNDER_REVIEW
    approved_on: str | None = None
    applies_to: tuple[str, ...] = ("glass", "tablespoon", "teaspoon")


@dataclass(frozen=True)
class PieceIdentity:
    """What one individual piece is, as the evidence describes it."""

    piece_id: str
    display_name: str
    piece_kind: str
    state: PieceState
    bone: str
    skin: str
    mass_basis: MassBasis
    whole_or_edible: str


@dataclass(frozen=True)
class PieceReference:
    """One weight or weight range for one piece identity.

    `weight_min_g == weight_max_g` is the definition of a POINT reference. A calibration
    class keeps its source boundaries; `size_class_default` is an owner decision and is
    false everywhere in this release.
    """

    ingredient_id: str
    piece_id: str
    evidence_id: str
    reference_kind: ReferenceKind
    size_class: str | None
    weight_min_g: Fraction
    weight_max_g: Fraction
    size_class_default: bool = False
    assumption_label_ru: str | None = None
    status: RecordStatus = RecordStatus.UNDER_REVIEW


@dataclass(frozen=True)
class ReferenceRegistry:
    """One immutable registry revision: data plus identifiers, no behaviour."""

    revision: str
    sources: tuple[SourceRef, ...]
    evidence: tuple[MeasurementEvidence, ...]
    ingredients: tuple[IngredientEntry, ...]
    generic_words: tuple[GenericWord, ...]
    pieces: tuple[PieceIdentity, ...]
    volume_references: tuple[VolumeReference, ...]
    piece_references: tuple[PieceReference, ...]


# --- results -------------------------------------------------------------------------


@dataclass(frozen=True)
class IngredientMatch:
    """The outcome of matching one name against one registry revision.

    `qualifiers` lists every recognized qualifier key in the name, so "with skin" and
    "skinless" stay distinguishable even when both are refused. `entries` lists the
    candidates behind an ambiguity or a generic word, in registry order.
    """

    status: MatchStatus
    canonical_key: str
    tokens: tuple[str, ...]
    qualifiers: tuple[str, ...]
    entry: IngredientEntry | None = None
    entries: tuple[IngredientEntry, ...] = ()
    conflicting_qualifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SizeAssumption:
    """The explicitly labelled size assumption behind a conditional estimate."""

    kind: str
    size_class: str
    weight_g: Fraction
    mass_basis: MassBasis
    label_ru: str


@dataclass(frozen=True)
class VolumeConversion:
    """One mass-to-volume result: the exact count, its rounding, and its provenance."""

    status: MassConversionStatus
    reason: MassConversionReason
    target_unit: Unit | None = None
    exact_count: Fraction | None = None
    volume_ml: Fraction | None = None
    rounded: RoundedAmount | None = None
    evidence_id: str | None = None
    derivation: Derivation | None = None
    normalization: str | None = None
    method: str | None = None


@dataclass(frozen=True)
class CountConversion:
    """One mass-to-count result. A POINT reference never fills `count_high` with a range."""

    status: MassConversionStatus
    reason: MassConversionReason
    display: CountDisplay | None = None
    count_low: Fraction | None = None
    count_high: Fraction | None = None
    rounded_low: Fraction | None = None
    rounded_high: Fraction | None = None
    relative_width: Fraction | None = None
    reference_kind: ReferenceKind | None = None
    size_class: str | None = None
    evidence_id: str | None = None


@dataclass(frozen=True)
class MassConversion:
    """One ingredient's mass conversion attempt, successful or refused.

    A refusal keeps the matched entry and the original mass so a later layer can explain
    it without guessing; `conversion` is None exactly when `reason` is not `CONVERTED`,
    so no exact count, volume or rounded value ever travels with a refusal.
    """

    status: MassConversionStatus
    reason: MassConversionReason
    mass_g: Fraction
    match: IngredientMatch
    conversion: VolumeConversion | CountConversion | None = None
    conditional: bool = False
    assumption: SizeAssumption | None = None


# --- normalization -------------------------------------------------------------------
#
# Matching is an exact lookup on a normalized token key. Nothing here measures
# similarity: two names are the same when their normalized token sets are the same.

# The qualifier lexicon: whole tokens and short phrases only, each with its canonical
# key. Forms are listed explicitly -- this is a fixed table, not morphology, so a word
# that is not here is not a qualifier and never silently narrows or widens a match.
_QUALIFIER_TABLE: tuple[tuple[str, str], ...] = (
    # bone and skin -- two-token phrases first, so they are consumed as one qualifier
    ("без кости", "bone:out"),
    ("без косточки", "bone:out"),
    ("на кости", "bone:in"),
    ("с костью", "bone:in"),
    ("с кожей", "skin:on"),
    ("без кожи", "skin:off"),
    # preparation state
    ("сырой", "state:raw"),
    ("сырая", "state:raw"),
    ("сырое", "state:raw"),
    ("сырые", "state:raw"),
    ("готовый", "state:cooked"),
    ("готовая", "state:cooked"),
    ("готовое", "state:cooked"),
    ("готовые", "state:cooked"),
    ("вареный", "state:cooked"),
    ("вареная", "state:cooked"),
    ("вареное", "state:cooked"),
    ("вареные", "state:cooked"),
    ("отварной", "state:cooked"),
    ("отварная", "state:cooked"),
    ("отварное", "state:cooked"),
    ("жареный", "state:fried"),
    ("жареная", "state:fried"),
    ("жареное", "state:fried"),
    ("жареные", "state:fried"),
    ("сухой", "state:dry"),
    ("сухая", "state:dry"),
    ("сухое", "state:dry"),
    ("сухие", "state:dry"),
    ("копченый", "state:smoked"),
    ("копченая", "state:smoked"),
    ("копченое", "state:smoked"),
    ("копченые", "state:smoked"),
    # edible basis: the only phrase that establishes that a recipe mass is peeled
    ("очищенный", "basis:edible"),
    ("очищенная", "basis:edible"),
    ("очищенное", "basis:edible"),
    ("очищенные", "basis:edible"),
    # size, as a selector, never as a default
    ("мелкий", "size:small"),
    ("мелкая", "size:small"),
    ("мелкое", "size:small"),
    ("средний", "size:medium"),
    ("средняя", "size:medium"),
    ("среднее", "size:medium"),
    ("крупный", "size:large"),
    ("крупная", "size:large"),
    ("крупное", "size:large"),
    ("большой", "size:large"),
    ("большая", "size:large"),
    ("большое", "size:large"),
)

# Longest phrase first, table order otherwise: the scan is deterministic.
_QUALIFIER_PHRASES: tuple[tuple[tuple[str, ...], str], ...] = tuple(
    sorted(
        ((tuple(text.split()), key) for text, key in _QUALIFIER_TABLE),
        key=lambda item: -len(item[0]),
    )
)

# The household word a size qualifier selects, when a reference carries that class.
_SIZE_CLASS_BY_QUALIFIER = {
    "size:small": "small",
    "size:medium": "medium",
    "size:large": "large",
}

# Qualifier families: two different values of one family in one name contradict.
_FAMILIES = ("bone", "skin", "state", "basis", "size")

# The closed vocabulary of `PieceIdentity.bone` and `.skin`.
_BONE_VALUES = frozenset({"in", "out", "none"})
_SKIN_VALUES = frozenset({"on", "off", "none"})


def normalize_name(text: str) -> str:
    """One name in canonical text form: NFC, lower case, "ye" for "yo", single spaces.

    Characters other than letters, digits and `-` become spaces, so punctuation around a
    name is not part of it. The result keeps the word order: it is what the qualifier
    scan reads.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    folded = unicodedata.normalize("NFC", text).lower().replace(UE, _YE)
    kept = [character if (character.isalnum() or character == "-") else " " for character in folded]
    return " ".join("".join(kept).split())


def name_tokens(text: str) -> tuple[str, ...]:
    """The normalized tokens of one name, in the order it was written."""
    return tuple(normalize_name(text).split())


def canonical_key(text: str) -> tuple[str, ...]:
    """The order-insensitive matching key: normalized tokens, sorted.

    Sorting is a normalization, not a similarity measure: it only makes word order
    irrelevant, so `мука пшеничная` and `пшеничная мука` are one key.
    """
    return tuple(sorted(name_tokens(text)))


def _scan_qualifiers(tokens: tuple[str, ...]) -> tuple[tuple[str, int, int], ...]:
    """Every recognized qualifier phrase in `tokens` as (key, start, end), left to right.

    Recognition is greedy on the phrase table: a two-token phrase is preferred over its
    first token alone, which is why "с кожей" cannot be read as two unrelated words.
    """
    found: list[tuple[str, int, int]] = []
    position = 0
    while position < len(tokens):
        match: tuple[str, int, int] | None = None
        for phrase, key in _QUALIFIER_PHRASES:
            if tokens[position : position + len(phrase)] == phrase:
                match = (key, position, position + len(phrase))
                break
        if match is None:
            position += 1
        else:
            found.append(match)
            position = match[2]
    return tuple(found)


def _qualifier_conflict(keys: tuple[str, ...]) -> tuple[str, ...]:
    """The conflicting qualifier keys when one family carries two values, else ().

    `без кожи с кожей` is not a product: the name contradicts itself and is reported
    rather than resolved toward either word.
    """
    conflicts: list[str] = []
    for family in _FAMILIES:
        values = sorted({key for key in keys if key.split(":", 1)[0] == family})
        if len(values) > 1:
            conflicts.extend(values)
    return tuple(conflicts)


# --- matching ------------------------------------------------------------------------


def _alias_index(
    registry: ReferenceRegistry,
) -> dict[tuple[str, ...], list[tuple[IngredientEntry, Alias]]]:
    """Key -> every (entry, alias) registered under it, in registry order.

    Deprecated and rejected entries are kept out of the index: they exist for the audit
    trail, not to answer a name.
    """
    index: dict[tuple[str, ...], list[tuple[IngredientEntry, Alias]]] = {}
    for entry in registry.ingredients:
        if entry.status in (RecordStatus.DEPRECATED, RecordStatus.REJECTED):
            continue
        for alias in entry.aliases:
            index.setdefault(canonical_key(alias.text), []).append((entry, alias))
    return index


def _generic_index(registry: ReferenceRegistry) -> dict[tuple[str, ...], GenericWord]:
    return {canonical_key(word.text): word for word in registry.generic_words}


def _declared_qualifiers(
    entry: IngredientEntry, aliases: tuple[Alias, ...]
) -> frozenset[str]:
    """Every qualifier key the entry accounts for: its alias words, accepted, required."""
    declared = set(entry.accepted_qualifiers) | set(entry.qualifiers_required)
    for alias in aliases:
        declared.update(key for key, _, _ in _scan_qualifiers(name_tokens(alias.text)))
    return frozenset(declared)


def _admissible(
    entry: IngredientEntry, detected: frozenset[str]
) -> bool:
    if not set(entry.qualifiers_required) <= detected:
        return False
    return not (set(entry.qualifiers_forbidden) & detected)


def _entry_order(entry: IngredientEntry) -> str:
    """The stable identity that canonicalizes candidate order.

    Registry order is an artefact of how the catalogue was written; the ingredient id is
    the identity. Sorting by it means permuting equivalent records changes nothing a
    caller can observe.
    """
    return entry.ingredient_id


def match_ingredient(
    name: str, *, registry: ReferenceRegistry | None = None
) -> IngredientMatch:
    """Resolve one ingredient name against the registry, exactly and without guessing.

    The lookup is a dictionary hit on the sorted normalized token key; there is no
    similarity, stemming or ordering of candidates. A name that is not an alias and not a
    generic word is unrecognized. A name whose recognized qualifiers are not all
    accounted for by the matched entry is `unsupported_qualifier` -- never quietly
    dropped. An unqualified generic word never resolves to a specific product.
    """
    active = _active_registry(registry)
    tokens = name_tokens(name)
    if not tokens:
        return IngredientMatch(
            status=MatchStatus.UNRECOGNIZED,
            canonical_key="",
            tokens=(),
            qualifiers=(),
        )

    found = _scan_qualifiers(tokens)
    detected = tuple(sorted({key for key, _, _ in found}))
    covered = {index for _, start, end in found for index in range(start, end)}
    base_tokens = tuple(token for index, token in enumerate(tokens) if index not in covered)
    full_key = tuple(sorted(tokens))
    base_key = tuple(sorted(base_tokens))
    detected_set = frozenset(detected)
    key_text = " ".join(full_key)

    conflicts = _qualifier_conflict(detected)
    if conflicts:
        return IngredientMatch(
            status=MatchStatus.QUALIFIER_CONFLICT,
            canonical_key=key_text,
            tokens=tokens,
            qualifiers=detected,
            conflicting_qualifiers=conflicts,
        )

    index = _alias_index(active)
    hits = index.get(full_key)
    if not hits:
        hits = index.get(base_key) if base_key != full_key else None
    # Two word orders of one name are one key, so one entry may appear twice here: the
    # candidates are unique entries, never two votes for the same product. The order is
    # canonical (ingredient id), so permuting equivalent registry records cannot change
    # what a caller observes.
    candidates = (
        tuple(sorted(dict.fromkeys(entry for entry, _ in hits), key=_entry_order))
        if hits
        else ()
    )
    admissible = tuple(entry for entry in candidates if _admissible(entry, detected_set))

    if len(admissible) == 1:
        entry = admissible[0]
        aliases = tuple(
            alias for candidate, alias in hits or () if candidate.ingredient_id == entry.ingredient_id
        )
        declared = _declared_qualifiers(entry, aliases)
        if not detected_set <= declared:
            return IngredientMatch(
                status=MatchStatus.UNSUPPORTED_QUALIFIER,
                canonical_key=key_text,
                tokens=tokens,
                qualifiers=detected,
                entry=entry,
                entries=(entry,),
            )
        return IngredientMatch(
            status=MatchStatus.MATCHED,
            canonical_key=key_text,
            tokens=tokens,
            qualifiers=detected,
            entry=entry,
            entries=(entry,),
        )

    if len(admissible) > 1:
        return IngredientMatch(
            status=MatchStatus.AMBIGUOUS,
            canonical_key=key_text,
            tokens=tokens,
            qualifiers=detected,
            entries=admissible,
        )

    generic = _generic_index(active)
    for key in (full_key, base_key):
        word = generic.get(key)
        if word is not None:
            entries = tuple(
                sorted(
                    (
                        entry
                        for entry in active.ingredients
                        if entry.ingredient_id in word.ambiguous_between
                    ),
                    key=_entry_order,
                )
            )
            return IngredientMatch(
                status=MatchStatus.GENERIC,
                canonical_key=" ".join(key),
                tokens=tokens,
                qualifiers=detected,
                entries=entries,
            )

    return IngredientMatch(
        status=MatchStatus.UNRECOGNIZED,
        canonical_key=key_text,
        tokens=tokens,
        qualifiers=detected,
    )


# --- mass ----------------------------------------------------------------------------


def mass_grams(quantity: Quantity) -> Fraction | None:
    """The total mass of a parsed amount, in grams, or None when it is not a mass.

    Every parsed part shares one dimension, so a quantified mass is exactly the sum of
    its parts: `1 кг 200 г` is 1200 g, `300 г` and `0,3 кг` are the same mass. A range,
    a qualitative phrase, a volume or a count is not a mass and yields None -- no
    midpoint is invented and nothing is salvaged from partial text.
    """
    if not isinstance(quantity, Quantity):
        raise TypeError("quantity must be a Quantity")
    if not quantity.is_quantified or quantity.dimension is not Dimension.MASS:
        return None
    total = Fraction(0)
    for part in quantity.parts:
        if part.unit is Unit.KILOGRAM:
            total += Fraction(part.amount) * 1000
        elif part.unit is Unit.GRAM:
            total += Fraction(part.amount)
        else:  # Dimension.MASS admits only these two units
            raise ValueError(f"unexpected mass unit {part.unit!r}")
    return total


def _capacity(target: MeasureVolumes, unit: Unit) -> Fraction:
    if unit is Unit.GLASS:
        value = target.glass_ml
    elif unit is Unit.TABLESPOON:
        value = target.tablespoon_ml
    elif unit is Unit.TEASPOON:
        value = target.teaspoon_ml
    else:
        raise ValueError(f"{unit!r} is not a household unit")
    if value is None:  # resolve_target_measures fills every target capacity
        raise ValueError("target measures must be resolved before conversion")
    return Fraction(value)


def _evidence(registry: ReferenceRegistry, evidence_id: str) -> MeasurementEvidence | None:
    for record in registry.evidence:
        if record.evidence_id == evidence_id:
            return record
    return None


def _exact_number(value: object) -> bool:
    """Whether a value is an exact, finite number this module may compute with.

    `int` and `Fraction` are exact. `bool` is refused even though it subclasses `int`,
    and a float, a Decimal or a string is refused rather than coerced -- a float can also
    be NaN or infinite, which neither `int` nor `Fraction` can be.
    """
    return isinstance(value, (int, Fraction)) and not isinstance(value, bool)


# --- mass to volume ------------------------------------------------------------------


def _convert_mass_to_volume(
    mass_g: Fraction | int,
    reference: VolumeReference,
    target: MeasureVolumes | None,
    *,
    target_unit: Unit | None = None,
    registry: ReferenceRegistry | None = None,
) -> VolumeConversion:
    """Convert one mass into a count of household measures for one reference.

    Private arithmetic helper. `convert_ingredient_mass` is the supported public
    gateway; this function still refuses an unapproved reference itself, so a direct
    call cannot turn an inactive record into a number either.

    `target` holds the person's own capacities (or the system defaults through
    `resolve_target_measures`); a source capacity is never consulted. With no
    `target_unit`, the preference is glass, then tablespoon, then teaspoon: the first
    unit whose rounded count reaches one and passes the 5% gate, and only the terminal
    teaspoon fallback may be a half or a quarter.

    The volume is exact (`mass / grams_per_ml`); presentation rounding happens once and
    the result is always approximate -- a density is a reference, not a measurement of
    this spoonful.
    """
    if not isinstance(mass_g, (int, Fraction)):
        raise TypeError("mass_g must be an int or a Fraction")
    mass = Fraction(mass_g)
    if mass <= 0:
        raise ValueError("mass_g must be positive")
    if not isinstance(reference, VolumeReference):
        raise TypeError("reference must be a VolumeReference")
    if not _exact_number(reference.grams_per_ml) or reference.grams_per_ml <= 0:
        raise ValueError("grams_per_ml must be a positive exact number")
    if reference.status is not RecordStatus.APPROVED:
        return VolumeConversion(
            status=MassConversionStatus.UNSUPPORTED,
            reason=MassConversionReason.REFERENCE_NOT_APPROVED,
            evidence_id=reference.evidence_id,
        )

    measures = resolve_target_measures(target)
    evidence = _evidence(_active_registry(registry), reference.evidence_id)
    volume_ml = mass / reference.grams_per_ml
    allowed = tuple(
        Unit(value) for value in reference.applies_to if value in {unit.value for unit in Unit}
    )

    if target_unit is not None:
        if target_unit not in allowed:
            raise ValueError(f"{target_unit!r} is not one of the reference's units")
        units = (target_unit,)
    else:
        units = tuple(unit for unit in _PREFERRED_TARGET_UNITS if unit in allowed)

    for unit in units:
        exact = volume_ml / _capacity(measures, unit)
        rounded = practical_round(exact, unit)
        if rounded is None:
            continue
        # Glass and tablespoon are offered only at one or more. The terminal teaspoon
        # fallback is exempt: half and a quarter teaspoon are real kitchen measures, and
        # no larger unit can express them.
        if target_unit is None and unit is not Unit.TEASPOON and rounded.amount < 1:
            continue
        return VolumeConversion(
            status=MassConversionStatus.REFERENCE_APPROXIMATE,
            reason=MassConversionReason.CONVERTED,
            target_unit=unit,
            exact_count=exact,
            volume_ml=volume_ml,
            rounded=rounded,
            evidence_id=reference.evidence_id,
            derivation=reference.derivation,
            normalization=None if evidence is None else evidence.normalization,
            method=None if evidence is None else evidence.method,
        )

    return VolumeConversion(
        status=MassConversionStatus.UNSUPPORTED,
        reason=MassConversionReason.ROUNDING_GATE_FAILED,
        volume_ml=volume_ml,
        evidence_id=reference.evidence_id,
        derivation=reference.derivation,
    )


# --- mass to count -------------------------------------------------------------------


def _round_pieces(value: Fraction) -> Fraction | None:
    """The coarsest whole/half/quarter piece within the 5% gate, or None."""
    for step in _PIECE_STEPS:
        multiplier = int(value / step + Fraction(1, 2))
        candidate = step * multiplier
        if candidate == 0:
            continue
        if abs(candidate - value) / value <= MAX_ROUNDING_RELATIVE_ERROR:
            return candidate
    return None


def _convert_mass_to_count(
    mass_g: Fraction | int, reference: PieceReference
) -> CountConversion:
    """Convert one mass into a count of pieces for one approved piece reference.

    Private arithmetic helper. `convert_ingredient_mass` is the supported public
    gateway; this function still refuses an unapproved reference itself.

    A `POINT` reference yields one count and no interval. A `MEASURED_INTERVAL` or a
    `CALIBRATION_CLASS` yields `[mass / max_weight, mass / min_weight]`, shown only when
    its relative width `(high - low) / midpoint` is at most 1/4. The exact bounds are
    kept apart from the presentation: `count_low`/`count_high` are mathematical, and
    `rounded_low`/`rounded_high` are the whole/half/quarter presentation. The bounds are
    exact: they come from the source's own weights, never from a manufactured
    uncertainty, and no reference weight changes which fractions may be presented.
    """
    if not isinstance(mass_g, (int, Fraction)):
        raise TypeError("mass_g must be an int or a Fraction")
    mass = Fraction(mass_g)
    if mass <= 0:
        raise ValueError("mass_g must be positive")
    if not isinstance(reference, PieceReference):
        raise TypeError("reference must be a PieceReference")
    if not _exact_number(reference.weight_min_g) or not _exact_number(
        reference.weight_max_g
    ):
        raise ValueError("piece weights must be exact numbers")
    if reference.weight_min_g <= 0 or reference.weight_max_g < reference.weight_min_g:
        raise ValueError("piece weights must be positive and ordered")
    if reference.status is not RecordStatus.APPROVED:
        return CountConversion(
            status=MassConversionStatus.UNSUPPORTED,
            reason=MassConversionReason.REFERENCE_NOT_APPROVED,
            reference_kind=reference.reference_kind,
            size_class=reference.size_class,
            evidence_id=reference.evidence_id,
        )

    low = mass / reference.weight_max_g
    high = mass / reference.weight_min_g
    width = Fraction(0) if high == low else (high - low) / ((high + low) / 2)
    base = dict(
        reference_kind=reference.reference_kind,
        size_class=reference.size_class,
        evidence_id=reference.evidence_id,
        count_low=low,
        count_high=high,
        relative_width=width,
    )

    if reference.reference_kind is not ReferenceKind.POINT and width > MAX_INTERVAL_RELATIVE_WIDTH:
        return CountConversion(
            status=MassConversionStatus.UNSUPPORTED,
            reason=MassConversionReason.INTERVAL_TOO_WIDE,
            **base,
        )

    rounded_low = _round_pieces(low)
    rounded_high = None if reference.reference_kind is ReferenceKind.POINT else _round_pieces(high)
    if rounded_low is None or (
        reference.reference_kind is not ReferenceKind.POINT and rounded_high is None
    ):
        return CountConversion(
            status=MassConversionStatus.UNSUPPORTED,
            reason=MassConversionReason.ROUNDING_GATE_FAILED,
            **base,
        )
    if rounded_high is None:
        rounded_high = rounded_low

    # The grid itself stops at a quarter, so this guard states the invariant rather than
    # branching on the reference's weight: there is no product-weight threshold.
    if rounded_low < QUARTER_PIECE:
        return CountConversion(
            status=MassConversionStatus.UNSUPPORTED,
            reason=MassConversionReason.BELOW_USEFUL_COUNT,
            **base,
        )

    display = (
        CountDisplay.SINGLE if rounded_low == rounded_high else CountDisplay.INTERVAL
    )
    return CountConversion(
        status=MassConversionStatus.REFERENCE_APPROXIMATE,
        reason=MassConversionReason.CONVERTED,
        display=display,
        rounded_low=rounded_low,
        rounded_high=rounded_high,
        **base,
    )


# --- selection and orchestration -----------------------------------------------------


@dataclass(frozen=True)
class _PieceSelection:
    reference: PieceReference | None
    reason: MassConversionReason | None
    conditional: bool


def _named_size_class(match: IngredientMatch) -> str | None:
    """The class a household size word selects, when the name carries exactly one."""
    named = sorted(
        {
            _SIZE_CLASS_BY_QUALIFIER[key]
            for key in match.qualifiers
            if key in _SIZE_CLASS_BY_QUALIFIER
        }
    )
    if len(named) > 1:  # already impossible: two size values are a qualifier conflict
        return None
    return named[0] if named else None


def _select_piece_reference(
    approved: tuple[PieceReference, ...],
    match: IngredientMatch,
    size_class: str | None,
) -> _PieceSelection:
    """Pick the one reference this call may use, or the reason there is none.

    An explicit size word in the name and an explicit `size_class` argument must agree:
    neither silently overrides the other, and a disagreement is a qualifier conflict.

    A named class (the caller's, or the name's own size word) must exist. Without one,
    a reference that describes no class is used directly; otherwise a single
    owner-designated default class produces a conditional estimate; otherwise the size
    class is required. A conditional default never overrides explicit size information.
    Two references for the same choice are a registry defect and are reported, not
    ordered.
    """
    named = _named_size_class(match)
    if size_class is not None and named is not None and size_class != named:
        return _PieceSelection(None, MassConversionReason.QUALIFIER_CONFLICT, False)
    wanted = size_class if size_class is not None else named

    if wanted is not None:
        matching = tuple(ref for ref in approved if ref.size_class == wanted)
        if not matching:
            return _PieceSelection(None, MassConversionReason.SIZE_CLASS_REQUIRED, False)
        if len(matching) > 1:
            return _PieceSelection(None, MassConversionReason.REFERENCE_VARIANT_CONFLICT, False)
        return _PieceSelection(matching[0], None, False)

    plain = tuple(ref for ref in approved if ref.size_class is None)
    if len(plain) > 1:
        return _PieceSelection(None, MassConversionReason.REFERENCE_VARIANT_CONFLICT, False)
    if plain:
        return _PieceSelection(plain[0], None, False)

    defaults = tuple(ref for ref in approved if ref.size_class_default)
    if len(defaults) > 1:
        return _PieceSelection(None, MassConversionReason.REFERENCE_VARIANT_CONFLICT, False)
    if defaults:
        return _PieceSelection(defaults[0], None, True)

    return _PieceSelection(None, MassConversionReason.SIZE_CLASS_REQUIRED, False)


def _match_failure(match: IngredientMatch, mass: Fraction) -> MassConversion:
    status, reason = {
        MatchStatus.GENERIC: (
            MassConversionStatus.AMBIGUOUS,
            MassConversionReason.GENERIC_NAME,
        ),
        MatchStatus.AMBIGUOUS: (
            MassConversionStatus.AMBIGUOUS,
            MassConversionReason.INGREDIENT_AMBIGUOUS,
        ),
        MatchStatus.QUALIFIER_CONFLICT: (
            MassConversionStatus.AMBIGUOUS,
            MassConversionReason.QUALIFIER_CONFLICT,
        ),
        MatchStatus.UNSUPPORTED_QUALIFIER: (
            MassConversionStatus.UNSUPPORTED,
            MassConversionReason.UNSUPPORTED_QUALIFIER,
        ),
        MatchStatus.UNRECOGNIZED: (
            MassConversionStatus.UNSUPPORTED,
            MassConversionReason.INGREDIENT_NOT_RECOGNIZED,
        ),
    }[match.status]
    return MassConversion(
        status=status, reason=reason, mass_g=mass, match=match
    )


def _refusal(
    match: IngredientMatch,
    mass: Fraction,
    reason: MassConversionReason,
    status: MassConversionStatus = MassConversionStatus.UNSUPPORTED,
) -> MassConversion:
    return MassConversion(
        status=status, reason=reason, mass_g=mass, match=match
    )


def _volume_path(
    match: IngredientMatch,
    mass: Fraction,
    references: tuple[VolumeReference, ...],
    target: MeasureVolumes | None,
    registry: ReferenceRegistry,
) -> MassConversion:
    approved = tuple(ref for ref in references if ref.status is RecordStatus.APPROVED)
    if not approved:
        return _refusal(match, mass, MassConversionReason.REFERENCE_NOT_APPROVED)
    if len(approved) > 1:
        return _refusal(
            match,
            mass,
            MassConversionReason.REFERENCE_VARIANT_CONFLICT,
            MassConversionStatus.AMBIGUOUS,
        )
    # An approved coefficient whose evidence row does not resolve is incomplete data,
    # not a number: the count path applies the same rule to its own evidence.
    if _evidence(registry, approved[0].evidence_id) is None:
        return _refusal(match, mass, MassConversionReason.REFERENCE_MISSING)
    conversion = _convert_mass_to_volume(
        mass, approved[0], target, registry=registry
    )
    if conversion.reason is not MassConversionReason.CONVERTED:
        return _refusal(match, mass, conversion.reason, conversion.status)
    return MassConversion(
        status=conversion.status,
        reason=conversion.reason,
        mass_g=mass,
        match=match,
        conversion=conversion,
    )


def _count_path(
    match: IngredientMatch,
    mass: Fraction,
    entry: IngredientEntry,
    size_class: str | None,
    registry: ReferenceRegistry,
) -> MassConversion:
    piece = next(
        (item for item in registry.pieces if item.piece_id == entry.piece_id), None
    )
    if piece is None:
        return _refusal(match, mass, MassConversionReason.REFERENCE_MISSING)

    references = tuple(
        ref
        for ref in registry.piece_references
        if ref.ingredient_id == entry.ingredient_id and ref.piece_id == entry.piece_id
    )
    if not references:
        return _refusal(match, mass, MassConversionReason.REFERENCE_MISSING)

    approved = tuple(ref for ref in references if ref.status is RecordStatus.APPROVED)
    if not approved:
        return _refusal(match, mass, MassConversionReason.REFERENCE_NOT_APPROVED)

    selection = _select_piece_reference(approved, match, size_class)
    if selection.reference is None:
        reason = selection.reason or MassConversionReason.REFERENCE_VARIANT_CONFLICT
        status = (
            MassConversionStatus.AMBIGUOUS
            if reason
            in (
                MassConversionReason.REFERENCE_VARIANT_CONFLICT,
                MassConversionReason.QUALIFIER_CONFLICT,
            )
            else MassConversionStatus.CLARIFICATION_REQUIRED
        )
        return _refusal(match, mass, reason, status)

    reference = selection.reference
    evidence = _evidence(registry, reference.evidence_id)
    if evidence is None:
        return _refusal(match, mass, MassConversionReason.REFERENCE_MISSING)
    # The selected measurement's own mass basis governs eligibility; the shared
    # `PieceIdentity.mass_basis` is not enough. A reference whose evidence contradicts
    # its piece metadata is a registry defect: it is reported, never converted.
    if evidence.mass_basis is not piece.mass_basis:
        return _refusal(
            match,
            mass,
            MassConversionReason.REFERENCE_VARIANT_CONFLICT,
            MassConversionStatus.AMBIGUOUS,
        )

    # The recipe's mass basis comes only from what the name states. Without an explicit
    # edible phrase the mass is taken as purchased; the two are never mixed.
    basis = (
        MassBasis.EDIBLE_PORTION
        if "basis:edible" in match.qualifiers
        else entry.mass_basis
    )
    if basis is not evidence.mass_basis:
        return _refusal(match, mass, MassConversionReason.STATE_MISMATCH)
    if (
        entry.state is not PieceState.UNSPECIFIED
        and piece.state is not PieceState.UNSPECIFIED
        and entry.state is not piece.state
    ):
        return _refusal(match, mass, MassConversionReason.STATE_MISMATCH)

    conversion = _convert_mass_to_count(mass, reference)
    if conversion.reason is not MassConversionReason.CONVERTED:
        return _refusal(match, mass, conversion.reason, conversion.status)

    assumption: SizeAssumption | None = None
    if selection.conditional:
        label = reference.assumption_label_ru
        if label is None:  # a default class without a label is a registry defect
            return _refusal(
                match,
                mass,
                MassConversionReason.REFERENCE_VARIANT_CONFLICT,
                MassConversionStatus.AMBIGUOUS,
            )
        assumption = SizeAssumption(
            kind="size_class",
            size_class=reference.size_class or "",
            weight_g=reference.weight_min_g,
            mass_basis=evidence.mass_basis,
            label_ru=label,
        )
    return MassConversion(
        status=conversion.status,
        reason=conversion.reason,
        mass_g=mass,
        match=match,
        conversion=conversion,
        conditional=selection.conditional,
        assumption=assumption,
    )


def convert_ingredient_mass(
    name: str,
    quantity: Quantity,
    *,
    target: MeasureVolumes | None = None,
    size_class: str | None = None,
    registry: ReferenceRegistry | None = None,
) -> MassConversion | None:
    """Convert one ingredient's parsed amount, or None when the amount is not a mass.

    This is the single supported public conversion entry point, the function B2-2 will
    call: the name, the stored amount and the person's resolved target capacities in;
    one item out. Approval is enforced here and only here -- a name is matched, the one
    approved reference is selected, and the private arithmetic helpers run afterwards --
    and the helpers refuse an unapproved record as well. A quantified mass always
    produces an item, even when nothing converts; any other amount (a range, a phrase, a
    volume, a count, a package) produces none and keeps the released behavior.

    Volume is preferred when the entry has a volume reference; otherwise the count path
    runs. Every refusal names a closed reason and carries `conversion=None`, and the
    original text is never touched.
    """
    active = _active_registry(registry)
    mass = mass_grams(quantity)
    if mass is None:
        return None

    match = match_ingredient(name, registry=active)
    if mass == 0:
        return _refusal(match, mass, MassConversionReason.ZERO_MASS)
    if match.status is not MatchStatus.MATCHED or match.entry is None:
        return _match_failure(match, mass)

    entry = match.entry
    volumes = tuple(
        ref for ref in active.volume_references if ref.ingredient_id == entry.ingredient_id
    )
    if volumes:
        if entry.status is not RecordStatus.APPROVED:
            return _refusal(match, mass, MassConversionReason.REFERENCE_NOT_APPROVED)
        return _volume_path(match, mass, volumes, target, active)

    if entry.piece_id is not None:
        # A product with no reference record at all is reported as missing even when the
        # catalogue entry itself is still under review: there is nothing to review yet.
        references = tuple(
            ref
            for ref in active.piece_references
            if ref.ingredient_id == entry.ingredient_id and ref.piece_id == entry.piece_id
        )
        if not references:
            return _refusal(match, mass, MassConversionReason.REFERENCE_MISSING)
        if entry.status is not RecordStatus.APPROVED:
            return _refusal(match, mass, MassConversionReason.REFERENCE_NOT_APPROVED)
        return _count_path(match, mass, entry, size_class, active)

    return _refusal(match, mass, MassConversionReason.REFERENCE_MISSING)


# --- registry validation -------------------------------------------------------------


def _number_problem(value: object, label: str) -> str | None:
    """Why one numeric field is unusable, or None when it is a positive exact number.

    The type is checked before any comparison, so a float, a string or a bool produces a
    message instead of a `TypeError`; a NaN or an infinity can never be an `int` or a
    `Fraction`, so rejecting unsupported types rejects them as well.
    """
    if not _exact_number(value):
        return f"{label} is not an exact number"
    if value <= 0:
        return f"{label} is non-positive"
    return None


def _evidence_problems(record: MeasurementEvidence) -> tuple[str, ...]:
    """Numeric and textual problems of one evidence row."""
    prefix = f"evidence {record.evidence_id}"
    problems: list[str] = []
    for label, value in (
        ("original amount", record.original_amount),
        ("mass", record.mass_g),
    ):
        message = _number_problem(value, label)
        if message is not None:
            problems.append(f"{prefix}: {message}")
    if record.original_volume_ml is not None:
        message = _number_problem(record.original_volume_ml, "volume")
        if message is not None:
            problems.append(f"{prefix}: {message}")
    if not isinstance(record.original_unit, str) or not record.original_unit.strip():
        problems.append(f"{prefix}: empty unit")
    return tuple(problems)


def _volume_reference_problems(
    reference: VolumeReference,
    evidence_by_id: dict[str, MeasurementEvidence],
    known_ingredients: set[str],
) -> list[str]:
    """Structural and numerical problems of one `VolumeReference`."""
    prefix = f"volume reference {reference.ingredient_id}"
    problems: list[str] = []
    if reference.ingredient_id not in known_ingredients:
        problems.append(f"volume reference to unknown ingredient {reference.ingredient_id}")
    evidence = evidence_by_id.get(reference.evidence_id)
    if evidence is None:
        problems.append(f"volume reference {reference.evidence_id}: unresolved evidence")

    density = _number_problem(reference.grams_per_ml, "density")
    if density is not None:
        problems.append(f"{prefix}: {density}")
    if not reference.applies_to:
        problems.append(f"{prefix}: no unit")
    for unit in reference.applies_to:
        if unit not in {member.value for member in Unit}:
            problems.append(f"{prefix}: unknown unit {unit}")
    if not isinstance(reference.derivation, Derivation):
        problems.append(f"{prefix}: unknown derivation")

    if evidence is not None:
        volume = evidence.original_volume_ml
        if volume is None:
            problems.append(f"{prefix}: evidence has no volume")
        elif _number_problem(volume, "volume") is not None:
            problems.append(f"{prefix}: evidence volume is not usable")
        elif (
            density is None
            and _number_problem(evidence.mass_g, "mass") is None
            # Both operands are known positive exact numbers. They are converted before
            # dividing: an integer `int / int` would be a float (1/3 is not exactly
            # Fraction(1, 3)) and a huge integer ratio would raise OverflowError, while
            # `Fraction / Fraction` is exact for every size and every denominator.
            and Fraction(evidence.mass_g) / Fraction(volume)
            != Fraction(reference.grams_per_ml)
        ):
            problems.append(f"{prefix}: coefficient differs from its own evidence")

    if (
        reference.derivation is Derivation.SOURCE_CUP_NORMALIZED
        and evidence is not None
        and (evidence.original_volume_ml is None or evidence.normalization is None)
    ):
        problems.append(f"{prefix}: unnormalized cup")
    return problems


def _piece_reference_problems(
    reference: PieceReference,
    evidence_by_id: dict[str, MeasurementEvidence],
    piece_by_id: dict[str, PieceIdentity],
    known_ingredients: set[str],
) -> list[str]:
    """Structural and numerical problems of one `PieceReference`.

    The numerical check is per kind: a point weight must equal its own evidence mass; a
    measured interval must actually be a range; a calibration class may be a range or a
    single-point commercial grade, and equality of bounds then needs the matching
    evidence. Free text is never read for a number.
    """
    prefix = f"piece reference {reference.ingredient_id}"
    problems: list[str] = []

    if reference.ingredient_id not in known_ingredients:
        problems.append(f"piece reference to unknown ingredient {reference.ingredient_id}")
    piece_identity = piece_by_id.get(reference.piece_id)
    if piece_identity is None:
        problems.append(f"piece reference to unknown piece {reference.piece_id}")
    evidence = evidence_by_id.get(reference.evidence_id)
    if evidence is None:
        problems.append(f"piece reference {reference.evidence_id}: unresolved evidence")

    kind = reference.reference_kind
    if not isinstance(kind, ReferenceKind):
        problems.append(f"{prefix}: unknown reference kind")

    minimum = _number_problem(reference.weight_min_g, "minimum weight")
    maximum = _number_problem(reference.weight_max_g, "maximum weight")
    if minimum is not None:
        problems.append(f"{prefix}: {minimum}")
    if maximum is not None:
        problems.append(f"{prefix}: {maximum}")
    usable = minimum is None and maximum is None
    if usable and reference.weight_max_g < reference.weight_min_g:
        problems.append(f"{prefix}: inverted range")
        usable = False

    if usable and kind is ReferenceKind.POINT:
        if reference.weight_min_g != reference.weight_max_g:
            problems.append(f"{prefix}: point with a range")
        elif (
            evidence is not None
            and _number_problem(evidence.mass_g, "mass") is None
            and evidence.mass_g != reference.weight_min_g
        ):
            problems.append(f"{prefix}: point weight differs from its own evidence")
    elif usable and kind is ReferenceKind.MEASURED_INTERVAL:
        if reference.weight_min_g == reference.weight_max_g:
            problems.append(f"{prefix}: interval without a range")
    elif usable and kind is ReferenceKind.CALIBRATION_CLASS:
        if reference.weight_min_g == reference.weight_max_g and evidence is not None:
            if (
                _number_problem(evidence.mass_g, "mass") is None
                and evidence.mass_g != reference.weight_min_g
            ):
                problems.append(f"{prefix}: point class differs from its own evidence")

    if kind is ReferenceKind.CALIBRATION_CLASS and reference.size_class is None:
        problems.append(f"{prefix}: class without a name")
    if reference.size_class_default and reference.size_class is None:
        problems.append(f"{prefix}: default without a class")
    if reference.size_class_default and not reference.assumption_label_ru:
        problems.append(f"{prefix}: default class without a label")

    if evidence is not None:
        if evidence.original_volume_ml is not None:
            problems.append(f"{prefix}: evidence is a volume measurement")
        if (
            piece_identity is not None
            and evidence.mass_basis is not piece_identity.mass_basis
        ):
            problems.append(f"{prefix}: evidence basis differs from its piece")
    return problems


def _registry_problems(registry: ReferenceRegistry) -> tuple[str, ...]:
    """Every structural problem of one registry, as stable message strings.

    Malformed numeric data -- an unsupported type, a zero or negative value, a zero
    denominator -- becomes a message instead of an arithmetic exception: every comparison
    runs only after its operands are known to be exact positive numbers. Free-text
    `quoted` descriptions are never read for a number; every value compared here is a
    structured field.
    """
    problems: list[str] = []
    if not registry.revision:
        problems.append("revision is empty")

    source_ids = [source.source_id for source in registry.sources]
    if len(set(source_ids)) != len(source_ids):
        problems.append("duplicate source id")
    known_sources = set(source_ids)

    evidence_ids = [record.evidence_id for record in registry.evidence]
    if len(set(evidence_ids)) != len(evidence_ids):
        problems.append("duplicate evidence id")
    evidence_by_id = {record.evidence_id: record for record in registry.evidence}
    for record in registry.evidence:
        if record.source_id not in known_sources:
            problems.append(f"evidence {record.evidence_id}: unknown source")
        problems.extend(_evidence_problems(record))

    ingredient_ids = [entry.ingredient_id for entry in registry.ingredients]
    if len(set(ingredient_ids)) != len(ingredient_ids):
        problems.append("duplicate ingredient id")
    known_ingredients = set(ingredient_ids)
    vocabulary = {key for _, key in _QUALIFIER_TABLE}

    piece_ids = [piece.piece_id for piece in registry.pieces]
    if len(set(piece_ids)) != len(piece_ids):
        problems.append("duplicate piece id")
    piece_by_id = {piece.piece_id: piece for piece in registry.pieces}
    for piece in registry.pieces:
        if piece.bone not in _BONE_VALUES:
            problems.append(f"piece {piece.piece_id}: unknown bone value")
        if piece.skin not in _SKIN_VALUES:
            problems.append(f"piece {piece.piece_id}: unknown skin value")

    for entry in registry.ingredients:
        if not entry.aliases:
            problems.append(f"ingredient {entry.ingredient_id}: no alias")
        for alias in entry.aliases:
            if normalize_name(alias.text) != alias.text:
                problems.append(
                    f"ingredient {entry.ingredient_id}: alias not normalized: {alias.text}"
                )
            if not alias.applicability.strip():
                problems.append(
                    f"ingredient {entry.ingredient_id}: alias without applicability"
                )
        for qualifier in (
            set(entry.accepted_qualifiers)
            | set(entry.qualifiers_required)
            | set(entry.qualifiers_forbidden)
        ):
            if qualifier not in vocabulary:
                problems.append(
                    f"ingredient {entry.ingredient_id}: unknown qualifier {qualifier}"
                )
        if entry.piece_id is not None and entry.piece_id not in piece_by_id:
            problems.append(f"ingredient {entry.ingredient_id}: unknown piece")
        if entry.superseded_by is not None and entry.superseded_by not in known_ingredients:
            problems.append(f"ingredient {entry.ingredient_id}: superseded_by unresolved")
        if (
            entry.status is RecordStatus.DEPRECATED and entry.superseded_by is None
        ):
            problems.append(f"ingredient {entry.ingredient_id}: deprecated without successor")

    alias_keys = {
        canonical_key(alias.text)
        for entry in registry.ingredients
        if entry.status not in (RecordStatus.DEPRECATED, RecordStatus.REJECTED)
        for alias in entry.aliases
    }
    generic_keys = [canonical_key(word.text) for word in registry.generic_words]
    if len(set(generic_keys)) != len(generic_keys):
        problems.append("duplicate generic word")
    for word in registry.generic_words:
        # A generic word names an ambiguity, not a catalogue size: one known product, or
        # none, keeps the same semantics. Only referential integrity is required.
        if canonical_key(word.text) in alias_keys:
            problems.append(f"generic word {word.text}: also an alias")
        for ingredient_id in word.ambiguous_between:
            if ingredient_id not in known_ingredients:
                problems.append(
                    f"generic word {word.text}: unknown ingredient {ingredient_id}"
                )

    approved_volume: dict[str, int] = {}
    for reference in registry.volume_references:
        problems.extend(
            _volume_reference_problems(reference, evidence_by_id, known_ingredients)
        )
        if reference.status is RecordStatus.APPROVED:
            approved_volume[reference.ingredient_id] = (
                approved_volume.get(reference.ingredient_id, 0) + 1
            )
    for ingredient_id, count in approved_volume.items():
        if count > 1:
            problems.append(f"ingredient {ingredient_id}: two approved volume references")

    approved_pieces: dict[tuple[str, str | None], int] = {}
    for reference in registry.piece_references:
        problems.extend(
            _piece_reference_problems(
                reference, evidence_by_id, piece_by_id, known_ingredients
            )
        )
        if reference.status is RecordStatus.APPROVED:
            slot = (reference.ingredient_id, reference.size_class)
            approved_pieces[slot] = approved_pieces.get(slot, 0) + 1
    for slot, count in approved_pieces.items():
        if count > 1:
            problems.append(f"piece reference {slot[0]}/{slot[1]}: two approved references")

    return tuple(problems)


def validate_registry(registry: ReferenceRegistry | None = None) -> tuple[str, ...]:
    """The registry's structural problems, empty when it is coherent.

    This is a data check, not a runtime guard: it runs in tests, never on a request.
    """
    return _registry_problems(_active_registry(registry))


def _canonical_value(value: object) -> object:
    """One record field as a JSON-ready value with an explicit type tag.

    A string stays a string and `None` stays JSON null. An exact number becomes a
    `["num", numerator, denominator]` triple -- `int` and an equal `Fraction` therefore
    encode identically, and no value is ever converted through a float. A `bool` and a
    tuple keep their own tags, so no two differently typed fields can share a
    representation. Only the types registry records use are accepted: an unsupported
    value raises rather than falling back to `repr`, whose memory address is not stable.
    """
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, Fraction):
        return ["num", str(value.numerator), str(value.denominator)]
    if isinstance(value, int):
        return ["num", str(value), "1"]
    if isinstance(value, str):
        return value
    if isinstance(value, tuple):
        return ["list", [_canonical_value(item) for item in value]]
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _canonical_lines(registry: ReferenceRegistry) -> list[str]:
    """One canonical JSON record per line, sorted as a whole.

    Each record is a JSON array: an explicit record-type tag first, then every
    semantically relevant field in declaration order. JSON quotes its strings, so a
    field boundary can never be forged by content (`"level|packed"` + `"dry"` and
    `"level"` + `"packed|dry"` stay distinct), and the field values are type-tagged
    (`_canonical_value`) so names, numbers and lists cannot impersonate each other. The
    output is UTF-8 text with Russian preserved, no float, no set iteration and no
    insertion order.

    Sorting the complete serialized records -- not a partial key -- makes the digest
    independent of the order records were written in: equivalent registries produce the
    same multiset of lines, so they produce the same sorted list. Any change to a field,
    including an approval status, changes at least one line and therefore the digest.
    The digest is a change detector, not a proof that the evidence behind a record is
    authentic.
    """
    def record(kind: str, *values: object) -> str:
        return json.dumps(
            [kind, *(_canonical_value(value) for value in values)],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    lines = [record("revision", registry.revision)]
    lines += [
        record("source", source.source_id, source.publisher, source.url, source.accessed,
               source.licence, source.record_id, source.version)
        for source in registry.sources
    ]
    lines += [
        record("evidence", row.evidence_id, row.source_id, row.quoted, row.original_amount,
               row.original_unit, row.original_volume_ml, row.mass_g, row.mass_basis,
               row.method, row.conditions, row.limitations, row.normalization)
        for row in registry.evidence
    ]
    for entry in registry.ingredients:
        lines.append(
            record("ingredient", entry.ingredient_id, entry.display_name, entry.category,
                   entry.form, entry.state, entry.mass_basis, entry.piece_id, entry.status,
                   entry.superseded_by, tuple(sorted(entry.accepted_qualifiers)),
                   tuple(sorted(entry.qualifiers_required)),
                   tuple(sorted(entry.qualifiers_forbidden)))
        )
        lines += [
            record("alias", entry.ingredient_id, alias.text, alias.applicability)
            for alias in entry.aliases
        ]
    lines += [
        record("generic", word.text, tuple(sorted(word.ambiguous_between)))
        for word in registry.generic_words
    ]
    lines += [
        record("piece", piece.piece_id, piece.display_name, piece.piece_kind, piece.state,
               piece.bone, piece.skin, piece.mass_basis, piece.whole_or_edible)
        for piece in registry.pieces
    ]
    lines += [
        record("volume", ref.ingredient_id, ref.evidence_id, ref.grams_per_ml,
               ref.derivation, tuple(sorted(ref.applies_to)), ref.status, ref.approved_on)
        for ref in registry.volume_references
    ]
    lines += [
        record("count", ref.ingredient_id, ref.piece_id, ref.evidence_id,
               ref.reference_kind, ref.size_class, ref.weight_min_g, ref.weight_max_g,
               ref.size_class_default, ref.assumption_label_ru, ref.status)
        for ref in registry.piece_references
    ]
    lines.sort()
    return lines


def registry_digest(registry: ReferenceRegistry | None = None) -> str:
    """A canonical digest of one registry revision.

    The input is the sorted list of complete canonical records (`_canonical_lines`):
    typed, JSON-quoted fields with no float and no delimiter concatenation. The digest is
    therefore independent of the order records were written in and of any field content
    that could imitate a boundary. Any edit -- a coefficient, a status, an alias, a note
    -- changes the digest, so a change to shipped data is visible in a diff and deliberate
    in review. It is an integrity fingerprint, not evidence of source authenticity.
    """
    text = "\n".join(_canonical_lines(_active_registry(registry)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _active_registry(registry: ReferenceRegistry | None) -> ReferenceRegistry:
    return DEFAULT_REGISTRY if registry is None else registry


# --- shipped research catalogue ------------------------------------------------------
#
# No record below is approved: this release ships the researched catalogue so the engine
# is exercised against real identities, and nothing converts until the owner approves a
# record. The USDA Food Buying Guide rows were supplied by the owner's B2-1 brief; this
# implementation did not re-read those PDFs, and the note says so in each record.

_NIST_SOURCE = SourceRef(
    source_id="nist_metric_kitchen",
    publisher="NIST, Office of Weights and Measures",
    url=(
        "https://www.nist.gov/pml/owm/metric-si/metric-kitchen/"
        "metric-kitchen-cooking-measurement-equivalencies"
    ),
    accessed="2026-11-13",
    licence="not stated",
)

_KAB_SOURCE = SourceRef(
    source_id="kab_weight_chart",
    publisher="King Arthur Baking",
    url="https://www.kingarthurbaking.com/learn/ingredient-weight-chart",
    accessed="2026-11-13",
    licence="not stated",
)

_FDC_SOURCE = SourceRef(
    source_id="usda_fdc",
    publisher="USDA FoodData Central (SR Legacy)",
    url="https://fdc.nal.usda.gov/",
    accessed="2026-11-13",
    licence="public domain (US government work)",
)

_POULTRY_EU_SOURCE = SourceRef(
    source_id="poultry_eu",
    publisher="poultry.eu",
    url="https://poultry.eu/products/raw-meat-products/chilled/",
    accessed="2026-11-13",
    licence="not stated",
)

_FBG_MEATS_SOURCE = SourceRef(
    source_id="usda_fbg_meats",
    publisher="USDA Food Buying Guide for Child Nutrition Programs, section 1",
    url=(
        "https://foodbuyingguide.fns.usda.gov/files/Reports/"
        "USDA_FBG_Section1_MeatsAndMeatAlternates_YieldTable.pdf"
    ),
    accessed="2026-11-13 (supplied by the owner's B2-1 brief; not re-read here)",
    licence="public domain (US government work)",
)

_FBG_VEGETABLES_SOURCE = SourceRef(
    source_id="usda_fbg_vegetables",
    publisher="USDA Food Buying Guide for Child Nutrition Programs, section 2",
    url=(
        "https://foodbuyingguide.fns.usda.gov/files/Reports/"
        "USDA_FBG_Section2_Vegetables_YieldTable.pdf"
    ),
    accessed="2026-11-13 (supplied by the owner's B2-1 brief; not re-read here)",
    licence="public domain (US government work)",
)

_CUP_NORMALIZATION = "US cup = 240 mL (NIST, approximate)"
_CUP_LIMITATION = "the source does not define its cup in millilitres; normalized to 240 mL"

_EVIDENCE = (
    MeasurementEvidence(
        evidence_id="kab.flour_ap.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1 cup = 120 g",
        original_amount=Fraction(1),
        original_unit="us_cup",
        original_volume_ml=US_CUP_ML,
        mass_g=Fraction(120),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="fluff, sprinkle, scrape (supporting page, not required per row)",
        conditions="all-purpose wheat flour, dry",
        limitations=_CUP_LIMITATION,
        normalization=_CUP_NORMALIZATION,
    ),
    MeasurementEvidence(
        evidence_id="kab.sugar_granulated.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1 cup = 198 g",
        original_amount=Fraction(1),
        original_unit="us_cup",
        original_volume_ml=US_CUP_ML,
        mass_g=Fraction(198),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="white granulated sugar, dry",
        limitations=_CUP_LIMITATION,
        normalization=_CUP_NORMALIZATION,
    ),
    MeasurementEvidence(
        evidence_id="kab.rice_long_grain_dry.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1/2 cup = 99 g",
        original_amount=Fraction(1, 2),
        original_unit="us_cup",
        original_volume_ml=Fraction(120),
        mass_g=Fraction(99),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="long-grain white rice, dry",
        limitations=_CUP_LIMITATION,
        normalization=_CUP_NORMALIZATION,
    ),
    MeasurementEvidence(
        evidence_id="kab.salt_table.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1 tbsp = 18 g",
        original_amount=Fraction(1),
        original_unit="tablespoon",
        original_volume_ml=Fraction(15),
        mass_g=Fraction(18),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="table salt, fine grind; no brand stated",
        limitations="no level or heaped condition is stated by the source",
    ),
    MeasurementEvidence(
        evidence_id="kab.salt_kosher_dc.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1 tbsp = 8 g",
        original_amount=Fraction(1),
        original_unit="tablespoon",
        original_volume_ml=Fraction(15),
        mass_g=Fraction(8),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="kosher salt, Diamond Crystal: a different product, never a substitute",
        limitations="the crystal shape differs; the number is not a coarse-salt coefficient",
    ),
    MeasurementEvidence(
        evidence_id="kab.salt_kosher_morton.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1 tbsp = 16 g",
        original_amount=Fraction(1),
        original_unit="tablespoon",
        original_volume_ml=Fraction(15),
        mass_g=Fraction(16),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="kosher salt, Morton: a different product, never a substitute",
        limitations="8, 16 and 18 g per tablespoon are three products, not three trials",
    ),
    MeasurementEvidence(
        evidence_id="kab.buckwheat_whole.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1 cup = 170 g",
        original_amount=Fraction(1),
        original_unit="us_cup",
        original_volume_ml=US_CUP_ML,
        mass_g=Fraction(170),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="whole buckwheat groats; the source does not state raw or roasted",
        limitations="roasting changes bulk density; no reference is attached to this row",
        normalization=_CUP_NORMALIZATION,
    ),
    MeasurementEvidence(
        evidence_id="kab.buckwheat_flour.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1 cup = 120 g",
        original_amount=Fraction(1),
        original_unit="us_cup",
        original_volume_ml=US_CUP_ML,
        mass_g=Fraction(120),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="buckwheat flour, dry",
        limitations=_CUP_LIMITATION,
        normalization=_CUP_NORMALIZATION,
    ),
    MeasurementEvidence(
        evidence_id="kab.cocoa.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1/2 cup = 42 g",
        original_amount=Fraction(1, 2),
        original_unit="us_cup",
        original_volume_ml=Fraction(120),
        mass_g=Fraction(42),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="unsweetened cocoa powder, dry",
        limitations=_CUP_LIMITATION,
        normalization=_CUP_NORMALIZATION,
    ),
    MeasurementEvidence(
        evidence_id="kab.oats_old_fashioned.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1 cup = 89 g",
        original_amount=Fraction(1),
        original_unit="us_cup",
        original_volume_ml=US_CUP_ML,
        mass_g=Fraction(89),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="rolled oats, old-fashioned or quick-cooking: one distinct product",
        limitations="not interchangeable with thick rolled oats; not averaged",
        normalization=_CUP_NORMALIZATION,
    ),
    MeasurementEvidence(
        evidence_id="kab.oats_ka_rolled.2026-11-13",
        source_id="kab_weight_chart",
        quoted="1 cup = 113 g",
        original_amount=Fraction(1),
        original_unit="us_cup",
        original_volume_ml=US_CUP_ML,
        mass_g=Fraction(113),
        mass_basis=MassBasis.NOT_APPLICABLE,
        method="not_stated",
        conditions="thick rolled oats (King Arthur Rolled): a second distinct product",
        limitations="not interchangeable with standard rolled oats; not averaged",
        normalization=_CUP_NORMALIZATION,
    ),
    # One row per source portion: a point reference must draw on the measurement it
    # actually restates, never on a row whose `mass_g` is a different size.
    MeasurementEvidence(
        evidence_id="fdc.170026.potato.small.2026-11-13",
        source_id="usda_fdc",
        quoted='small (1-3/4"-2-1/2" dia) = 170 g',
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(170),
        mass_basis=MassBasis.EDIBLE_PORTION,
        method="not_stated",
        conditions="potatoes, flesh and skin, raw; the source's small household portion",
        limitations=(
            "FDC portions are edible material without refuse; the weight excludes peel, "
            "so it does not count whole as-purchased tubers"
        ),
    ),
    MeasurementEvidence(
        evidence_id="fdc.170026.potato.medium.2026-11-13",
        source_id="usda_fdc",
        quoted='medium (2-1/4"-3-1/4") = 213 g',
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(213),
        mass_basis=MassBasis.EDIBLE_PORTION,
        method="not_stated",
        conditions="potatoes, flesh and skin, raw; the source's medium household portion",
        limitations=(
            "FDC portions are edible material without refuse; the weight excludes peel, "
            "so it does not count whole as-purchased tubers"
        ),
    ),
    MeasurementEvidence(
        evidence_id="fdc.170026.potato.large.2026-11-13",
        source_id="usda_fdc",
        quoted='large (3"-4-1/4") = 369 g',
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(369),
        mass_basis=MassBasis.EDIBLE_PORTION,
        method="not_stated",
        conditions="potatoes, flesh and skin, raw; the source's large household portion",
        limitations=(
            "FDC portions are edible material without refuse; the weight excludes peel, "
            "so it does not count whole as-purchased tubers"
        ),
    ),
    MeasurementEvidence(
        evidence_id="fdc.170000.onion.small.2026-11-13",
        source_id="usda_fdc",
        quoted="small = 70 g",
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(70),
        mass_basis=MassBasis.EDIBLE_PORTION,
        method="not_stated",
        conditions="onions, raw; the source's small household portion",
        limitations="edible portion (peeled); not an as-purchased bulb weight",
    ),
    MeasurementEvidence(
        evidence_id="fdc.170000.onion.medium.2026-11-13",
        source_id="usda_fdc",
        quoted='medium (2-1/2" dia) = 110 g',
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(110),
        mass_basis=MassBasis.EDIBLE_PORTION,
        method="not_stated",
        conditions="onions, raw; the source's medium household portion",
        limitations="edible portion (peeled); not an as-purchased bulb weight",
    ),
    MeasurementEvidence(
        evidence_id="fdc.170000.onion.large.2026-11-13",
        source_id="usda_fdc",
        quoted="large = 150 g",
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(150),
        mass_basis=MassBasis.EDIBLE_PORTION,
        method="not_stated",
        conditions="onions, raw; the source's large household portion",
        limitations="edible portion (peeled); not an as-purchased bulb weight",
    ),
    MeasurementEvidence(
        evidence_id="fdc.170393.carrot.small.2026-11-13",
        source_id="usda_fdc",
        quoted='small (5-1/2" long) = 50 g',
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(50),
        mass_basis=MassBasis.EDIBLE_PORTION,
        method="not_stated",
        conditions="carrots, raw; the source's small household portion",
        limitations="edible portion (peeled); not an as-purchased root weight",
    ),
    MeasurementEvidence(
        evidence_id="fdc.170393.carrot.medium.2026-11-13",
        source_id="usda_fdc",
        quoted="medium = 61 g",
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(61),
        mass_basis=MassBasis.EDIBLE_PORTION,
        method="not_stated",
        conditions="carrots, raw; the source's medium household portion",
        limitations="edible portion (peeled); not an as-purchased root weight",
    ),
    MeasurementEvidence(
        evidence_id="fdc.170393.carrot.large.2026-11-13",
        source_id="usda_fdc",
        quoted='large (7-1/4"-8-1/2") = 72 g',
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(72),
        mass_basis=MassBasis.EDIBLE_PORTION,
        method="not_stated",
        conditions="carrots, raw; the source's large household portion",
        limitations="edible portion (peeled); not an as-purchased root weight",
    ),
    MeasurementEvidence(
        evidence_id="fdc.171688.apple.2026-11-13",
        source_id="usda_fdc",
        quoted='medium (3" dia) = 182 g',
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(182),
        mass_basis=MassBasis.EDIBLE_PORTION,
        method="not_stated",
        conditions="apples, raw, with skin",
        limitations=(
            "not read directly (API quota): reproduced from several pages citing this "
            "record; edible portion, core and stem excluded"
        ),
    ),
    MeasurementEvidence(
        evidence_id="poultry_eu.breast_fillet.calibration.2026-11-13",
        source_id="poultry_eu",
        quoted="Calibration 110-130g / 130-150g / 150-170g / 170-200g / 200g+",
        original_amount=Fraction(1),
        original_unit="piece",
        original_volume_ml=None,
        mass_g=Fraction(150),
        mass_basis=MassBasis.AS_PURCHASED,
        method="not_stated",
        conditions="chicken breast fillet, skin off, raw; supplier size grades",
        limitations=(
            "a grade is sizing evidence, not a population distribution; the open 200g+ "
            "grade cannot bound a count and has no reference"
        ),
    ),
    MeasurementEvidence(
        evidence_id="usda_fbg.chicken_thigh_bone_in_skin_on.2026-11-13",
        source_id="usda_fbg_meats",
        quoted="Chicken thigh, bone-in, skin-on: approximately 4.0 oz each",
        original_amount=Fraction(4),
        original_unit="ounce",
        original_volume_ml=None,
        mass_g=Fraction(4) * _OUNCE_G,
        mass_basis=MassBasis.AS_PURCHASED,
        method="not_stated",
        conditions="chicken thigh, bone-in, skin-on, raw",
        limitations=(
            "the source says approximately; the ounce figure is converted exactly and "
            "kept as one representative weight, never as a universal piece weight"
        ),
    ),
    MeasurementEvidence(
        evidence_id="usda_fbg.chicken_thigh_bone_in_skinless.2026-11-13",
        source_id="usda_fbg_meats",
        quoted="Chicken thigh, bone-in, skinless: approximately 2.8 oz each",
        original_amount=Fraction(28, 10),
        original_unit="ounce",
        original_volume_ml=None,
        mass_g=Fraction(28, 10) * _OUNCE_G,
        mass_basis=MassBasis.AS_PURCHASED,
        method="not_stated",
        conditions="chicken thigh, bone-in, skinless, raw",
        limitations=(
            "a different product from the skin-on thigh; never substituted for it"
        ),
    ),
    MeasurementEvidence(
        evidence_id="usda_fbg.chicken_thigh_with_back.2026-11-13",
        source_id="usda_fbg_meats",
        quoted="Chicken thigh with back, bone-in, skin-on: approximately 8.7 oz each",
        original_amount=Fraction(87, 10),
        original_unit="ounce",
        original_volume_ml=None,
        mass_g=Fraction(87, 10) * _OUNCE_G,
        mass_basis=MassBasis.AS_PURCHASED,
        method="not_stated",
        conditions="chicken thigh with back, bone-in, skin-on, raw",
        limitations=(
            "a third, distinct cut; the back adds mass, so this weight is never used for "
            "a plain thigh"
        ),
    ),
    MeasurementEvidence(
        evidence_id="usda_fbg.potato_120_count.2026-11-13",
        source_id="usda_fbg_vegetables",
        quoted="Fresh whole White/Russet potatoes, 120-count: approximately 6 oz each",
        original_amount=Fraction(6),
        original_unit="ounce",
        original_volume_ml=None,
        mass_g=Fraction(6) * _OUNCE_G,
        mass_basis=MassBasis.AS_PURCHASED,
        method="not_stated",
        conditions="commercial size grade: 120 potatoes per unit, whole and unpeeled",
        limitations=(
            "a commercial sizing category, not a household small/medium/large word; the "
            "number says nothing about an individually measured tuber"
        ),
    ),
    MeasurementEvidence(
        evidence_id="usda_fbg.potato_100_count.2026-11-13",
        source_id="usda_fbg_vegetables",
        quoted="Fresh whole White/Russet potatoes, 100-count: approximately 8 oz each",
        original_amount=Fraction(8),
        original_unit="ounce",
        original_volume_ml=None,
        mass_g=Fraction(8) * _OUNCE_G,
        mass_basis=MassBasis.AS_PURCHASED,
        method="not_stated",
        conditions="commercial size grade: 100 potatoes per unit, whole and unpeeled",
        limitations="a commercial sizing category; never a universal medium potato",
    ),
    MeasurementEvidence(
        evidence_id="usda_fbg.potato_80_count.2026-11-13",
        source_id="usda_fbg_vegetables",
        quoted="Fresh whole White/Russet potatoes, 80-count: approximately 10 oz each",
        original_amount=Fraction(10),
        original_unit="ounce",
        original_volume_ml=None,
        mass_g=Fraction(10) * _OUNCE_G,
        mass_basis=MassBasis.AS_PURCHASED,
        method="not_stated",
        conditions="commercial size grade: 80 potatoes per unit, whole and unpeeled",
        limitations="a commercial sizing category; never a universal large potato",
    ),
)

_ENTRIES = (
    IngredientEntry(
        ingredient_id="wheat_flour_all_purpose",
        display_name="мука пшеничная",
        category="grain",
        form="dry all-purpose wheat flour",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias("мука пшеничная", "the ordinary named wheat flour; both Russian word orders"),
            Alias("пшеничная мука", "the same product, the other word order"),
        ),
    ),
    IngredientEntry(
        ingredient_id="buckwheat_groats_raw",
        display_name="крупа гречневая",
        category="grain",
        form="dry whole buckwheat groats; raw or roasted is not stated by the source",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias("крупа гречневая", "whole groats named by the grain; state unverified"),
            Alias("гречневая крупа", "the same, the other word order"),
        ),
    ),
    IngredientEntry(
        ingredient_id="buckwheat_groats_roasted",
        display_name="крупа гречневая жареная",
        category="grain",
        form="dry whole roasted buckwheat groats",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias("крупа гречневая жареная", "the name states roasting; no evidence exists yet"),
            Alias("ядрица", "the market word for roasted whole groats"),
        ),
    ),
    IngredientEntry(
        ingredient_id="buckwheat_flour",
        display_name="мука гречневая",
        category="grain",
        form="buckwheat flour, dry",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias("мука гречневая", "flour of buckwheat, not the groats"),
            Alias("гречневая мука", "the same, the other word order"),
        ),
    ),
    IngredientEntry(
        ingredient_id="rice_white_long_grain_dry",
        display_name="рис длиннозерный",
        category="grain",
        form="dry white long-grain rice",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias("рис длиннозерный", "long-grain white rice; the form is named"),
            Alias("длиннозерный рис", "the same, the other word order"),
        ),
    ),
    IngredientEntry(
        ingredient_id="oats_rolled_standard",
        display_name="овсяные хлопья быстрого приготовления",
        category="grain",
        form="dry rolled oats, old-fashioned or quick-cooking",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias(
                "овсяные хлопья быстрого приготовления",
                "the standard rolled product; the bare words stay a generic word",
            ),
            Alias("хлопья быстрого приготовления", "the same product by its use"),
        ),
    ),
    IngredientEntry(
        ingredient_id="oats_rolled_thick",
        display_name="овсяные хлопья толстые",
        category="grain",
        form="dry thick rolled oats",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias("овсяные хлопья толстые", "the thick product is named as thick"),
            Alias("геркулес", "the market word for thick rolled oats"),
        ),
    ),
    IngredientEntry(
        ingredient_id="sugar_granulated",
        display_name="сахар песок",
        category="sweetener",
        form="dry white granulated sugar",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias("сахар песок", "granulated sugar; the form is named"),
            Alias("сахарный песок", "the same, the other word order"),
        ),
    ),
    IngredientEntry(
        ingredient_id="salt_table_fine",
        display_name="соль поваренная",
        category="seasoning",
        form="dry fine table salt",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias("соль поваренная", "fine table salt; the bare word stays a generic word"),
            Alias("поваренная соль", "the same, the other word order"),
        ),
    ),
    IngredientEntry(
        ingredient_id="salt_kosher",
        display_name="соль кошерная",
        category="seasoning",
        form="kosher salt, coarse crystals",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias("соль кошерная", "a different product; kept to explain the generic word"),
        ),
        status=RecordStatus.REJECTED,
    ),
    IngredientEntry(
        ingredient_id="cocoa_unsweetened",
        display_name="какао-порошок",
        category="powder",
        form="unsweetened cocoa powder, dry",
        state=PieceState.UNSPECIFIED,
        mass_basis=MassBasis.NOT_APPLICABLE,
        piece_id=None,
        aliases=(
            Alias("какао", "cocoa powder is the only product this catalogue holds"),
            Alias("какао-порошок", "the explicit form of the same product"),
        ),
    ),
    IngredientEntry(
        ingredient_id="chicken_breast_fillet",
        display_name="куриное филе",
        category="poultry",
        form="raw boneless skinless breast fillet",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="chicken_breast_fillet",
        aliases=(
            Alias("куриное филе", "the boneless skinless breast fillet"),
            Alias("филе куриное", "the same, the other word order"),
            Alias("филе грудки", "the fillet of the breast, boneless and skinless"),
            Alias("куриная грудка без кости", "the same cut named by what it lacks"),
        ),
    ),
    IngredientEntry(
        ingredient_id="chicken_inner_fillet",
        display_name="куриное внутреннее филе",
        category="poultry",
        form="raw boneless skinless inner fillet (tenderloin)",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="chicken_inner_fillet",
        aliases=(
            Alias("внутреннее филе", "the tenderloin, a different piece from the breast fillet"),
            Alias("малое филе", "the market word for the same piece"),
        ),
    ),
    IngredientEntry(
        ingredient_id="chicken_breast_bone_in_skin_on",
        display_name="куриная грудка на кости с кожей",
        category="poultry",
        form="raw bone-in skin-on breast",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="chicken_breast_bone_in_skin_on",
        aliases=(
            Alias("куриная грудка на кости", "the bone-in breast; no piece weight exists yet"),
            Alias("грудка на кости", "the same cut, shorter form"),
        ),
    ),
    IngredientEntry(
        ingredient_id="chicken_thigh_bone_in_skin_on",
        display_name="куриное бедро на кости с кожей",
        category="poultry",
        form="raw bone-in skin-on thigh",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="chicken_thigh_bone_in_skin_on",
        aliases=(
            Alias("бедро на кости с кожей", "bone-in and skin-on are both named"),
            Alias("куриное бедро на кости с кожей", "the same, with the bird named"),
        ),
    ),
    IngredientEntry(
        ingredient_id="chicken_thigh_bone_in_skinless",
        display_name="куриное бедро на кости без кожи",
        category="poultry",
        form="raw bone-in skinless thigh",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="chicken_thigh_bone_in_skinless",
        aliases=(
            Alias("бедро на кости без кожи", "bone-in and skinless are both named"),
            Alias("куриное бедро на кости без кожи", "the same, with the bird named"),
        ),
    ),
    IngredientEntry(
        ingredient_id="chicken_thigh_boneless",
        display_name="куриное бедро без кости",
        category="poultry",
        form="raw boneless thigh",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="chicken_thigh_boneless",
        aliases=(
            Alias("бедро без кости", "the boneless thigh; skin is not stated, so neither is assumed"),
            Alias("филе бедра", "the market word for the boneless thigh"),
            Alias("куриное бедро без кости", "the same, with the bird named"),
        ),
    ),
    IngredientEntry(
        ingredient_id="chicken_thigh_with_back_bone_in_skin_on",
        display_name="бедро на кости с кожей с частью спинки",
        category="poultry",
        form="raw bone-in skin-on thigh with a portion of back",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="chicken_thigh_with_back_bone_in_skin_on",
        aliases=(
            Alias(
                "бедро на кости с кожей с частью спинки",
                "the USDA 'thigh with back' cut; B2-1 wording, not owner-approved",
            ),
            Alias("бедро с частью спинки", "the same cut, shorter form"),
        ),
    ),
    IngredientEntry(
        ingredient_id="potato_whole_raw",
        display_name="картофель",
        category="vegetable",
        form="raw whole potato",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="potato_whole",
        aliases=(
            Alias("картофель", "the whole raw tuber; peel is part of the name's product"),
            Alias("картофелина", "one whole tuber, the same product"),
            Alias("картошка", "the everyday word for the same product"),
        ),
        accepted_qualifiers=("basis:edible", "size:small", "size:medium", "size:large"),
    ),
    IngredientEntry(
        ingredient_id="onion_whole_raw",
        display_name="лук репчатый",
        category="vegetable",
        form="raw whole onion bulb",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="onion_whole",
        aliases=(
            Alias("лук репчатый", "the bulb, not a green onion or a leek"),
            Alias("луковица", "one whole bulb of the same product"),
        ),
        accepted_qualifiers=("basis:edible", "size:small", "size:medium", "size:large"),
    ),
    IngredientEntry(
        ingredient_id="carrot_whole_raw",
        display_name="морковь",
        category="vegetable",
        form="raw whole carrot root",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="carrot_whole",
        aliases=(
            Alias("морковь", "the whole raw root"),
            Alias("морковка", "the everyday word for the same product"),
        ),
        accepted_qualifiers=("basis:edible", "size:small", "size:medium", "size:large"),
    ),
    IngredientEntry(
        ingredient_id="apple_whole_raw",
        display_name="яблоко",
        category="fruit",
        form="raw whole apple",
        state=PieceState.RAW,
        mass_basis=MassBasis.AS_PURCHASED,
        piece_id="apple_whole",
        aliases=(
            Alias("яблоко", "one whole raw fruit"),
            Alias("яблоки", "the same product in the plural"),
        ),
        accepted_qualifiers=("basis:edible", "size:medium"),
    ),
)

_GENERIC_WORDS = (
    GenericWord("мука", ("wheat_flour_all_purpose", "buckwheat_flour")),
    GenericWord("крупа", ("buckwheat_groats_raw", "buckwheat_groats_roasted")),
    GenericWord("гречка", ("buckwheat_groats_raw", "buckwheat_groats_roasted")),
    GenericWord("соль", ("salt_table_fine", "salt_kosher")),
    # A generic word names an ambiguity; how many products the catalogue currently holds
    # is not part of that meaning. These two are generic even though only one product of
    # each is known, and they never resolve to that product.
    GenericWord("сахар", ("sugar_granulated",)),
    GenericWord("рис", ("rice_white_long_grain_dry",)),
    GenericWord("овсянка", ("oats_rolled_standard", "oats_rolled_thick")),
    GenericWord("овсяные хлопья", ("oats_rolled_standard", "oats_rolled_thick")),
    GenericWord(
        "курица",
        (
            "chicken_breast_fillet",
            "chicken_inner_fillet",
            "chicken_breast_bone_in_skin_on",
            "chicken_thigh_bone_in_skin_on",
            "chicken_thigh_bone_in_skinless",
            "chicken_thigh_boneless",
            "chicken_thigh_with_back_bone_in_skin_on",
        ),
    ),
    GenericWord(
        "бедро",
        (
            "chicken_thigh_bone_in_skin_on",
            "chicken_thigh_bone_in_skinless",
            "chicken_thigh_boneless",
            "chicken_thigh_with_back_bone_in_skin_on",
        ),
    ),
    GenericWord(
        "куриное бедро",
        (
            "chicken_thigh_bone_in_skin_on",
            "chicken_thigh_bone_in_skinless",
            "chicken_thigh_boneless",
            "chicken_thigh_with_back_bone_in_skin_on",
        ),
    ),
    GenericWord(
        "филе",
        (
            "chicken_breast_fillet",
            "chicken_inner_fillet",
            "chicken_thigh_boneless",
        ),
    ),
)

_PIECES = (
    PieceIdentity(
        piece_id="chicken_breast_fillet",
        display_name="филе грудки",
        piece_kind="fillet",
        state=PieceState.RAW,
        bone="none",
        skin="none",
        mass_basis=MassBasis.AS_PURCHASED,
        whole_or_edible="the fillet as sold; no refuse is removed before weighing",
    ),
    PieceIdentity(
        piece_id="chicken_inner_fillet",
        display_name="внутреннее филе",
        piece_kind="inner_fillet",
        state=PieceState.RAW,
        bone="none",
        skin="none",
        mass_basis=MassBasis.AS_PURCHASED,
        whole_or_edible="the tenderloin as sold",
    ),
    PieceIdentity(
        piece_id="chicken_breast_bone_in_skin_on",
        display_name="грудка на кости",
        piece_kind="breast",
        state=PieceState.RAW,
        bone="in",
        skin="on",
        mass_basis=MassBasis.AS_PURCHASED,
        whole_or_edible="a whole bone-in breast, not a fillet",
    ),
    PieceIdentity(
        piece_id="chicken_thigh_bone_in_skin_on",
        display_name="бедро на кости с кожей",
        piece_kind="thigh",
        state=PieceState.RAW,
        bone="in",
        skin="on",
        mass_basis=MassBasis.AS_PURCHASED,
        whole_or_edible="one thigh with its bone and skin",
    ),
    PieceIdentity(
        piece_id="chicken_thigh_bone_in_skinless",
        display_name="бедро на кости без кожи",
        piece_kind="thigh",
        state=PieceState.RAW,
        bone="in",
        skin="off",
        mass_basis=MassBasis.AS_PURCHASED,
        whole_or_edible="one thigh with its bone, skin removed",
    ),
    PieceIdentity(
        piece_id="chicken_thigh_boneless",
        display_name="бедро без кости",
        piece_kind="thigh",
        state=PieceState.RAW,
        bone="out",
        skin="none",
        mass_basis=MassBasis.AS_PURCHASED,
        whole_or_edible="one boneless thigh; the skin state is not established",
    ),
    PieceIdentity(
        piece_id="chicken_thigh_with_back_bone_in_skin_on",
        display_name="бедро с частью спинки",
        piece_kind="thigh_with_back",
        state=PieceState.RAW,
        bone="in",
        skin="on",
        mass_basis=MassBasis.AS_PURCHASED,
        whole_or_edible="one thigh together with a portion of back",
    ),
    PieceIdentity(
        piece_id="potato_whole",
        display_name="картофель",
        piece_kind="tuber",
        state=PieceState.RAW,
        bone="none",
        skin="none",
        mass_basis=MassBasis.EDIBLE_PORTION,
        whole_or_edible="one tuber; the FDC weight is edible flesh, peel excluded",
    ),
    PieceIdentity(
        piece_id="potato_whole_as_purchased",
        display_name="картофель (товарный калибр)",
        piece_kind="tuber",
        state=PieceState.RAW,
        bone="none",
        skin="none",
        mass_basis=MassBasis.AS_PURCHASED,
        whole_or_edible=(
            "one whole unpeeled tuber of a commercial count grade; a separate product "
            "state from the edible-portion tuber, never substituted for it"
        ),
    ),
    PieceIdentity(
        piece_id="onion_whole",
        display_name="лук репчатый",
        piece_kind="bulb",
        state=PieceState.RAW,
        bone="none",
        skin="none",
        mass_basis=MassBasis.EDIBLE_PORTION,
        whole_or_edible="one bulb; the FDC weight is peeled",
    ),
    PieceIdentity(
        piece_id="carrot_whole",
        display_name="морковь",
        piece_kind="root",
        state=PieceState.RAW,
        bone="none",
        skin="none",
        mass_basis=MassBasis.EDIBLE_PORTION,
        whole_or_edible="one root; the FDC weight is peeled",
    ),
    PieceIdentity(
        piece_id="apple_whole",
        display_name="яблоко",
        piece_kind="fruit",
        state=PieceState.RAW,
        bone="none",
        skin="none",
        mass_basis=MassBasis.EDIBLE_PORTION,
        whole_or_edible="one fruit; the FDC weight excludes core and stem",
    ),
)

_VOLUME_REFERENCES = (
    VolumeReference(
        ingredient_id="wheat_flour_all_purpose",
        evidence_id="kab.flour_ap.2026-11-13",
        grams_per_ml=Fraction(1, 2),
        derivation=Derivation.SOURCE_CUP_NORMALIZED,
    ),
    VolumeReference(
        ingredient_id="buckwheat_flour",
        evidence_id="kab.buckwheat_flour.2026-11-13",
        grams_per_ml=Fraction(1, 2),
        derivation=Derivation.SOURCE_CUP_NORMALIZED,
    ),
    VolumeReference(
        ingredient_id="rice_white_long_grain_dry",
        evidence_id="kab.rice_long_grain_dry.2026-11-13",
        grams_per_ml=Fraction(33, 40),
        derivation=Derivation.SOURCE_CUP_NORMALIZED,
    ),
    VolumeReference(
        ingredient_id="oats_rolled_standard",
        evidence_id="kab.oats_old_fashioned.2026-11-13",
        grams_per_ml=Fraction(89, 240),
        derivation=Derivation.SOURCE_CUP_NORMALIZED,
    ),
    VolumeReference(
        ingredient_id="oats_rolled_thick",
        evidence_id="kab.oats_ka_rolled.2026-11-13",
        grams_per_ml=Fraction(113, 240),
        derivation=Derivation.SOURCE_CUP_NORMALIZED,
    ),
    VolumeReference(
        ingredient_id="sugar_granulated",
        evidence_id="kab.sugar_granulated.2026-11-13",
        grams_per_ml=Fraction(33, 40),
        derivation=Derivation.SOURCE_CUP_NORMALIZED,
    ),
    VolumeReference(
        ingredient_id="salt_table_fine",
        evidence_id="kab.salt_table.2026-11-13",
        grams_per_ml=Fraction(6, 5),
        derivation=Derivation.DIRECT_TARGET,
    ),
    VolumeReference(
        ingredient_id="cocoa_unsweetened",
        evidence_id="kab.cocoa.2026-11-13",
        grams_per_ml=Fraction(7, 20),
        derivation=Derivation.SOURCE_CUP_NORMALIZED,
    ),
)

_PIECE_REFERENCES = (
    # Poultry: the fillet's supplier classes, kept per class and never combined.
    PieceReference(
        ingredient_id="chicken_breast_fillet",
        piece_id="chicken_breast_fillet",
        evidence_id="poultry_eu.breast_fillet.calibration.2026-11-13",
        reference_kind=ReferenceKind.CALIBRATION_CLASS,
        size_class="110-130",
        weight_min_g=Fraction(110),
        weight_max_g=Fraction(130),
    ),
    PieceReference(
        ingredient_id="chicken_breast_fillet",
        piece_id="chicken_breast_fillet",
        evidence_id="poultry_eu.breast_fillet.calibration.2026-11-13",
        reference_kind=ReferenceKind.CALIBRATION_CLASS,
        size_class="130-150",
        weight_min_g=Fraction(130),
        weight_max_g=Fraction(150),
    ),
    PieceReference(
        ingredient_id="chicken_breast_fillet",
        piece_id="chicken_breast_fillet",
        evidence_id="poultry_eu.breast_fillet.calibration.2026-11-13",
        reference_kind=ReferenceKind.CALIBRATION_CLASS,
        size_class="150-170",
        weight_min_g=Fraction(150),
        weight_max_g=Fraction(170),
    ),
    PieceReference(
        ingredient_id="chicken_breast_fillet",
        piece_id="chicken_breast_fillet",
        evidence_id="poultry_eu.breast_fillet.calibration.2026-11-13",
        reference_kind=ReferenceKind.CALIBRATION_CLASS,
        size_class="170-200",
        weight_min_g=Fraction(170),
        weight_max_g=Fraction(200),
    ),
    # Poultry: newly identified Food Buying Guide representative weights.
    PieceReference(
        ingredient_id="chicken_thigh_bone_in_skin_on",
        piece_id="chicken_thigh_bone_in_skin_on",
        evidence_id="usda_fbg.chicken_thigh_bone_in_skin_on.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class=None,
        weight_min_g=Fraction(4) * _OUNCE_G,
        weight_max_g=Fraction(4) * _OUNCE_G,
    ),
    PieceReference(
        ingredient_id="chicken_thigh_bone_in_skinless",
        piece_id="chicken_thigh_bone_in_skinless",
        evidence_id="usda_fbg.chicken_thigh_bone_in_skinless.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class=None,
        weight_min_g=Fraction(28, 10) * _OUNCE_G,
        weight_max_g=Fraction(28, 10) * _OUNCE_G,
    ),
    PieceReference(
        ingredient_id="chicken_thigh_with_back_bone_in_skin_on",
        piece_id="chicken_thigh_with_back_bone_in_skin_on",
        evidence_id="usda_fbg.chicken_thigh_with_back.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class=None,
        weight_min_g=Fraction(87, 10) * _OUNCE_G,
        weight_max_g=Fraction(87, 10) * _OUNCE_G,
    ),
    # Produce: FDC household sizes, edible-portion weights. Each reference draws on the
    # evidence row for its own size.
    PieceReference(
        ingredient_id="potato_whole_raw",
        piece_id="potato_whole",
        evidence_id="fdc.170026.potato.small.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class="small",
        weight_min_g=Fraction(170),
        weight_max_g=Fraction(170),
    ),
    PieceReference(
        ingredient_id="potato_whole_raw",
        piece_id="potato_whole",
        evidence_id="fdc.170026.potato.medium.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class="medium",
        weight_min_g=Fraction(213),
        weight_max_g=Fraction(213),
    ),
    PieceReference(
        ingredient_id="potato_whole_raw",
        piece_id="potato_whole",
        evidence_id="fdc.170026.potato.large.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class="large",
        weight_min_g=Fraction(369),
        weight_max_g=Fraction(369),
    ),
    # Produce: commercial potato count grades. These are as-purchased, unpeeled sizing
    # categories, so they belong to their own piece identity and never to the
    # edible-portion tuber. No ingredient entry selects that identity yet, so the records
    # document the grades without being able to produce a number.
    PieceReference(
        ingredient_id="potato_whole_raw",
        piece_id="potato_whole_as_purchased",
        evidence_id="usda_fbg.potato_120_count.2026-11-13",
        reference_kind=ReferenceKind.CALIBRATION_CLASS,
        size_class="120-count",
        weight_min_g=Fraction(6) * _OUNCE_G,
        weight_max_g=Fraction(6) * _OUNCE_G,
    ),
    PieceReference(
        ingredient_id="potato_whole_raw",
        piece_id="potato_whole_as_purchased",
        evidence_id="usda_fbg.potato_100_count.2026-11-13",
        reference_kind=ReferenceKind.CALIBRATION_CLASS,
        size_class="100-count",
        weight_min_g=Fraction(8) * _OUNCE_G,
        weight_max_g=Fraction(8) * _OUNCE_G,
    ),
    PieceReference(
        ingredient_id="potato_whole_raw",
        piece_id="potato_whole_as_purchased",
        evidence_id="usda_fbg.potato_80_count.2026-11-13",
        reference_kind=ReferenceKind.CALIBRATION_CLASS,
        size_class="80-count",
        weight_min_g=Fraction(10) * _OUNCE_G,
        weight_max_g=Fraction(10) * _OUNCE_G,
    ),
    PieceReference(
        ingredient_id="onion_whole_raw",
        piece_id="onion_whole",
        evidence_id="fdc.170000.onion.small.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class="small",
        weight_min_g=Fraction(70),
        weight_max_g=Fraction(70),
    ),
    PieceReference(
        ingredient_id="onion_whole_raw",
        piece_id="onion_whole",
        evidence_id="fdc.170000.onion.medium.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class="medium",
        weight_min_g=Fraction(110),
        weight_max_g=Fraction(110),
    ),
    PieceReference(
        ingredient_id="onion_whole_raw",
        piece_id="onion_whole",
        evidence_id="fdc.170000.onion.large.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class="large",
        weight_min_g=Fraction(150),
        weight_max_g=Fraction(150),
    ),
    PieceReference(
        ingredient_id="carrot_whole_raw",
        piece_id="carrot_whole",
        evidence_id="fdc.170393.carrot.small.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class="small",
        weight_min_g=Fraction(50),
        weight_max_g=Fraction(50),
    ),
    PieceReference(
        ingredient_id="carrot_whole_raw",
        piece_id="carrot_whole",
        evidence_id="fdc.170393.carrot.medium.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class="medium",
        weight_min_g=Fraction(61),
        weight_max_g=Fraction(61),
    ),
    PieceReference(
        ingredient_id="carrot_whole_raw",
        piece_id="carrot_whole",
        evidence_id="fdc.170393.carrot.large.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class="large",
        weight_min_g=Fraction(72),
        weight_max_g=Fraction(72),
    ),
    PieceReference(
        ingredient_id="apple_whole_raw",
        piece_id="apple_whole",
        evidence_id="fdc.171688.apple.2026-11-13",
        reference_kind=ReferenceKind.POINT,
        size_class="medium",
        weight_min_g=Fraction(182),
        weight_max_g=Fraction(182),
    ),
)

DEFAULT_REGISTRY = ReferenceRegistry(
    revision=REGISTRY_REVISION,
    sources=(
        _NIST_SOURCE,
        _KAB_SOURCE,
        _FDC_SOURCE,
        _POULTRY_EU_SOURCE,
        _FBG_MEATS_SOURCE,
        _FBG_VEGETABLES_SOURCE,
    ),
    evidence=_EVIDENCE,
    ingredients=_ENTRIES,
    generic_words=_GENERIC_WORDS,
    pieces=_PIECES,
    volume_references=_VOLUME_REFERENCES,
    piece_references=_PIECE_REFERENCES,
)

__all__ = [
    "DEFAULT_REGISTRY",
    "MAX_INTERVAL_RELATIVE_WIDTH",
    "QUARTER_PIECE",
    "REGISTRY_REVISION",
    "US_CUP_ML",
    "Alias",
    "CountConversion",
    "CountDisplay",
    "Derivation",
    "GenericWord",
    "IngredientEntry",
    "IngredientMatch",
    "MassBasis",
    "MassConversion",
    "MassConversionReason",
    "MassConversionStatus",
    "MatchStatus",
    "MeasurementEvidence",
    "PieceIdentity",
    "PieceReference",
    "PieceState",
    "RecordStatus",
    "ReferenceKind",
    "ReferenceRegistry",
    "SizeAssumption",
    "SourceRef",
    "VolumeConversion",
    "VolumeReference",
    "canonical_key",
    "convert_ingredient_mass",
    "mass_grams",
    "match_ingredient",
    "name_tokens",
    "normalize_name",
    "registry_digest",
    "validate_registry",
]
