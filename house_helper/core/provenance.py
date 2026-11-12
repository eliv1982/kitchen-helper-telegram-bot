"""KH-F01-B1.1-A: a purely descriptive audit of ingredient source-measure provenance.

A source measure is the measure a recipe uses for its own amounts (`1 стакан`, `1 банка`).
A glass is only a volume when its capacity is known, and a jar or package only has a
content when the text says so. This module reads the stored `amount` strings and reports,
per ingredient and per term, whether the text carries that meaning:

* ``1 стакан (200 мл)`` -- declared, capacity 200 mL;
* ``1 банка (400 г)``   -- declared package content 400 g (no volume conversion);
* ``1 стакан``          -- undeclared glass: a capacity is needed but the text gives none;
* ``1 пачка``           -- package content missing;
* ``500 мл``, ``2 ст. л.`` -- not applicable: the unit itself defines the amount;
* ``2 яйца``, ``1 луковица`` -- not applicable: a curated countable expression;
* ``2–3 ст. л.``, ``1 cup``, ``1 ст. л. с горкой`` -- an unsupported form;
* ``1 кружка``, ``1 oz`` -- a unit this audit cannot read: unsupported, never excused;
* ``кружка``, ``одна кружка``, ``по вкусу и кружка`` -- no number is not positive
  recognition: an unrecognized numberless line is unsupported, never excused as
  quantity-free.

The audit is descriptive, not a validator. It never rewrites an amount, never adds a
capacity or a content, never converts a package weight into millilitres, and never makes
a recipe invalid because a declaration is missing (OD-02). A missing declaration is
reported so that a reader can be told the truth; it is not replaced by the system glass,
the person's profile or another term's declaration (OD-03: no automatic inheritance).

B1.1 keeps three concepts apart. *Declared text* is the stored amount itself: it resolves
the capacity of the term it belongs to, and it makes no claim about where the fact came
from. *Historical provenance* would be an external-source record; B1.1 has none, so this
module never says a declaration was verified. A *temporary clarification* belongs to a
later request-time adapter and is deliberately not accepted here: an undeclared glass
stays undeclared in the audit regardless of any user-supplied capacity.

The module is pure Core: standard library plus the accepted KH-F01-B0 parser, no
database, web, Telegram, OpenAI or configuration, and no outside call. It adds no
arithmetic of its own: exact values come from `core.measures`, and the package-content
recognizer only extracts the declared text and hands it back to that parser.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from typing import Union

from house_helper.core.measures import (
    QuantityPart,
    QuantityReason,
    Unit,
    parse_quantity,
)

# Exact source values, as the core keeps them: a whole millilitre stays an int, a
# fractional declaration stays a Fraction. Nothing here is rounded.
ExactValue = Union[int, Fraction]


class ProvenanceKind(str, Enum):
    """What kind of source measure one term is."""

    GLASS = "glass"
    PACKAGE = "package"
    CUP = "cup"
    OTHER = "other"


class TermProvenanceStatus(str, Enum):
    """The audit result for one term. The values are stable for a future API adapter."""

    DECLARED = "declared"
    UNDECLARED = "undeclared"
    INVALID_CAPACITY = "invalid_capacity"
    PACKAGE_CONTENT_MISSING = "package_content_missing"
    UNSUPPORTED_OTHER = "unsupported_other"
    # Audit-internal: a metric amount, a level spoon, a countable amount or a qualitative
    # phrase needs no declaration. These entries are not part of an API provenance list.
    NOT_APPLICABLE = "not_applicable"


class CapacityOrigin(str, Enum):
    """Where a term's source capacity comes from in this audit.

    Only ``declared_text`` and ``unresolved`` are ever reported for an emitted
    provenance entry; ``not_applicable`` marks the audit-internal terms that need no
    capacity. There is deliberately no ``inherited_from_ingredient`` value (OD-03).
    """

    DECLARED_TEXT = "declared_text"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE = "not_applicable"


class ProvenanceAuditStatus(str, Enum):
    """The summary of one audit: complete when nothing is missing or unsupported."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


# Reason strings are machine-readable and descriptive; they are not Russian wording.
REASON_CAPACITY_NOT_DECLARED = "capacity_not_declared"
REASON_CAPACITY_OUT_OF_RANGE = "capacity_out_of_range"
REASON_CONTENT_NOT_DECLARED = "declared_content_missing"
REASON_PACKAGE_CONSTRUCT_UNSUPPORTED = "package_construct_unsupported"
REASON_COUNTABLE_CONSTRUCT_UNSUPPORTED = "countable_construct_unsupported"
REASON_MEASURE_CONSTRUCT_UNSUPPORTED = "measure_construct_unsupported"

# The four metric units a package net content may be written in. A package content is
# recorded in its own unit exactly as written; it is never turned into a volume.
_CONTENT_UNITS = frozenset({Unit.GRAM, Unit.KILOGRAM, Unit.MILLILITER, Unit.LITER})

# Curated package words (pack, jar, small jar, package, bag). A fixed table, not
# morphology: a form outside it is reported, never guessed at.
_PACKAGE_WORDS = frozenset(
    {
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
    }
)

# A bounded package grammar: a count, a known package word, and an optional net
# content that the accepted parser understands. The count is not converted anywhere.
_COUNT = r"[0-9]{1,9}(?:[.,][0-9]{1,9})?"
_PACKAGE_WORD_PATTERN = "|".join(
    sorted(_PACKAGE_WORDS, key=lambda word: (-len(word), word))
)
_PACKAGE_HEAD = re.compile(
    rf"(?P<count>{_COUNT})\s+(?P<word>{_PACKAGE_WORD_PATTERN})(?![а-я])"
)
# Detection and routing are derived from the same table: a spelling the table knows is
# never missed merely because its stem (пачк, банк, ...) is absent from some other regex.
_PACKAGE_MENTION = re.compile(
    rf"(?<![а-я])(?:{_PACKAGE_WORD_PATTERN})(?![а-я])"
)

# Curated countable forms: the egg, clove, head, slice and piece forms the approved
# grammar names, plus the onion the Stage A fence calls countable. A fixed table, not
# morphology: a word outside it is unsupported, never guessed at as a countable noun.
_COUNTABLE_WORDS = frozenset(
    {
        # яйцо, including the irregular genitive plural
        "яйцо",
        "яйца",
        "яиц",
        "яйцу",
        "яйцом",
        "яйце",
        "яйцам",
        "яйцами",
        "яйцах",
        # луковица
        "луковица",
        "луковицы",
        "луковицу",
        "луковицей",
        "луковице",
        "луковиц",
        "луковицам",
        "луковицами",
        "луковицах",
        # шт
        "шт",
        "штука",
        "штуки",
        "штуку",
        "штукой",
        "штуке",
        "штук",
        "штукам",
        "штуками",
        "штуках",
        # зубчик
        "зубчик",
        "зубчика",
        "зубчику",
        "зубчиком",
        "зубчике",
        "зубчики",
        "зубчиков",
        "зубчикам",
        "зубчиками",
        "зубчиках",
        # головка
        "головка",
        "головки",
        "головку",
        "головкой",
        "головке",
        "головок",
        "головкам",
        "головками",
        "головках",
        # ломтик
        "ломтик",
        "ломтика",
        "ломтику",
        "ломтиком",
        "ломтике",
        "ломтики",
        "ломтиков",
        "ломтикам",
        "ломтиками",
        "ломтиках",
    }
)
_COUNTABLE_WORD_PATTERN = "|".join(
    sorted(_COUNTABLE_WORDS, key=lambda word: (-len(word), word))
)
# The count before a countable word may be written in any form the accepted parser
# reads: a whole number, a decimal, a fraction, a mixed number or a vulgar fraction. The
# count is only recognized here; it is never converted or recorded.
_VULGAR_FRACTION_CHARS = "¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞"
_COUNTABLE_COUNT = (
    rf"[0-9]{{1,9}}(?:[.,][0-9]{{1,9}})?"
    rf"|[0-9]{{1,9}}\s+[0-9]{{1,9}}/[0-9]{{1,9}}"
    rf"|[0-9]{{1,9}}/[0-9]{{1,9}}"
    rf"|[0-9]{{1,9}}\s*[{_VULGAR_FRACTION_CHARS}]"
    rf"|[{_VULGAR_FRACTION_CHARS}]"
)
_COUNTABLE_HEAD = re.compile(
    rf"(?P<count>{_COUNTABLE_COUNT})\s*"
    rf"(?P<word>{_COUNTABLE_WORD_PATTERN})(?![а-я])\s*\.?"
)

# Terms of a package expression or a countable expression share one separator rule.
_TERM_SEPARATOR = re.compile(r"\s*(?:,|и(?![а-я])|плюс(?![а-я])|\+)\s*")
_PAREN_CONTENT = re.compile(r"\s*\(\s*(?P<content>[^()]{1,40})\s*\)")
_BARE_CONTENT = re.compile(
    r"\s*(?P<number>[0-9]{1,9}(?:[.,][0-9]{1,9})?)\s*(?P<unit>[а-я.]{1,12})"
)

# The matching form of a package expression. The package recognizer is new in B1.1, so
# it normalizes its own small input; the accepted parser normalizes its own as well.
_YO_LETTER = chr(0x0451)
_SPACE_TRANSLATION = str.maketrans({chr(0x00A0): " ", chr(0x202F): " "})


def _normalize(text: str) -> str:
    text = text.translate(_SPACE_TRANSLATION)
    text = text.lower().replace(_YO_LETTER, "е")
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class DeclaredContent:
    """A package's declared net content, exact and in its own unit.

    A weight stays a weight: `1 банка (400 г)` records 400 grams, and no millilitre value
    is derived. `unit` is one of gram, kilogram, milliliter or liter.
    """

    amount: Fraction
    unit: Unit


@dataclass(frozen=True)
class ProvenanceEntry:
    """One source term's provenance, as the audit read the stored text.

    `capacity_ml` is present only for a declared glass and `declared_content` only for a
    declared package; each keeps its exact value. `origin` says who supplied the fact in
    this audit: the stored text itself (`declared_text`), nothing (`unresolved`), or that
    no capacity is needed (`not_applicable`).
    """

    ingredient_position: int
    term_index: int
    kind: ProvenanceKind
    status: TermProvenanceStatus
    capacity_ml: ExactValue | None = None
    declared_content: DeclaredContent | None = None
    origin: CapacityOrigin = CapacityOrigin.UNRESOLVED
    reason: str | None = None


@dataclass(frozen=True)
class ProvenanceAudit:
    """The audit of one ingredient list, in stored order.

    `entries` contains every term, including the audit-internal `not_applicable` ones.
    ``declared`` and ``incomplete`` are the two groups a future API emits; an entry is in
    at most one of them. `status` is complete only when nothing is missing or unsupported
    -- a missing declaration is reported, never repaired and never made an error.
    """

    entries: tuple[ProvenanceEntry, ...]

    @property
    def declared(self) -> tuple[ProvenanceEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.status is TermProvenanceStatus.DECLARED
        )

    @property
    def incomplete(self) -> tuple[ProvenanceEntry, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.status is not TermProvenanceStatus.DECLARED
            and entry.status is not TermProvenanceStatus.NOT_APPLICABLE
        )

    @property
    def status(self) -> ProvenanceAuditStatus:
        return (
            ProvenanceAuditStatus.INCOMPLETE
            if self.incomplete
            else ProvenanceAuditStatus.COMPLETE
        )


def _entry(
    ingredient_position: int,
    term_index: int,
    kind: ProvenanceKind,
    status: TermProvenanceStatus,
    *,
    capacity_ml: ExactValue | None = None,
    declared_content: DeclaredContent | None = None,
    reason: str | None = None,
) -> ProvenanceEntry:
    """Build one entry with its origin rules in one place."""
    if status is TermProvenanceStatus.DECLARED:
        origin = CapacityOrigin.DECLARED_TEXT
    elif status is TermProvenanceStatus.NOT_APPLICABLE:
        origin = CapacityOrigin.NOT_APPLICABLE
    else:
        origin = CapacityOrigin.UNRESOLVED
    return ProvenanceEntry(
        ingredient_position=ingredient_position,
        term_index=term_index,
        kind=kind,
        status=status,
        capacity_ml=capacity_ml,
        declared_content=declared_content,
        origin=origin,
        reason=reason,
    )


def _amount_of(item: object) -> str:
    """The stored amount of one ingredient-like value.

    A plain string is an amount; an `Ingredient` (or any object with an `amount`
    attribute) and a mapping with an `amount` key are the two ingredient shapes the
    protocol and the favorite copy use. Anything else is a caller error, not data.
    """
    if isinstance(item, str):
        return item
    amount = getattr(item, "amount", None)
    if isinstance(amount, str):
        return amount
    if isinstance(item, Mapping):
        amount = item.get("amount")
        if isinstance(amount, str):
            return amount
    raise TypeError("an ingredient must be a string or carry a string amount")


def _audit_part(
    position: int, term_index: int, part: QuantityPart
) -> ProvenanceEntry:
    """One parsed term: a declared glass, an undeclared glass, or no declaration needed."""
    unit = part.unit
    if unit is Unit.GLASS and part.glass_ml is not None:
        return _entry(
            position,
            term_index,
            ProvenanceKind.GLASS,
            TermProvenanceStatus.DECLARED,
            capacity_ml=part.glass_ml,
        )
    if unit is Unit.GLASS:
        return _entry(
            position,
            term_index,
            ProvenanceKind.GLASS,
            TermProvenanceStatus.UNDECLARED,
            reason=REASON_CAPACITY_NOT_DECLARED,
        )
    # A metric amount and an ordinary level spoon carry their own defined volume.
    return _entry(
        position,
        term_index,
        ProvenanceKind.OTHER,
        TermProvenanceStatus.NOT_APPLICABLE,
    )


def _declared_content(text: str) -> DeclaredContent | None:
    """The metric net content of one package term, or None when it is not one.

    The accepted parser is the authority on the number and unit: the recognizer only
    collects the candidate text. A content that parses as a single metric mass or volume
    term is declared; anything else (a count, a household measure, prose) is not.
    """
    quantity = parse_quantity(text, allow_nonstandard_glass_sizes=True)
    if not quantity.is_quantified or len(quantity.parts) != 1:
        return None
    part = quantity.parts[0]
    if part.unit not in _CONTENT_UNITS:
        return None
    return DeclaredContent(amount=Fraction(part.amount), unit=part.unit)


def _package_entries(position: int, text: str) -> tuple[ProvenanceEntry, ...] | None:
    """Every package term of one amount, or None when the line is not that pure form.

    `text` is the normalized amount. A pure package expression is one or more
    `<count> <package word>` terms, each with an optional parenthesized or bare metric net
    content, joined by `,`, `и`, `плюс` or `+`. Anything else -- a package word mixed with
    another measure, trailing prose, a content the parser cannot read -- returns None: the
    caller then reports the line as an unsupported construct rather than salvaging part of
    it.
    """
    entries: list[ProvenanceEntry] = []
    term_index = 0
    cursor = 0
    while True:
        remainder = text[cursor:].strip()
        if remainder in ("", "."):
            break
        if term_index:
            separator = _TERM_SEPARATOR.match(text, cursor)
            if separator is None:
                return None
            cursor = separator.end()
        head = _PACKAGE_HEAD.match(text, cursor)
        if head is None:
            return None
        cursor = head.end()

        content: DeclaredContent | None = None
        paren = _PAREN_CONTENT.match(text, cursor)
        if paren is not None:
            cursor = paren.end()
            content = _declared_content(paren.group("content"))
            if content is None:
                # The text carries a content this audit cannot read (a count, a
                # household measure, prose). That is not the same as a missing one.
                return None
        else:
            bare = _BARE_CONTENT.match(text, cursor)
            if bare is not None:
                candidate = f"{bare.group('number')} {bare.group('unit')}"
                content = _declared_content(candidate)
                if content is None:
                    return None
                cursor = bare.end()

        entries.append(
            _entry(
                position,
                term_index,
                ProvenanceKind.PACKAGE,
                (
                    TermProvenanceStatus.DECLARED
                    if content is not None
                    else TermProvenanceStatus.PACKAGE_CONTENT_MISSING
                ),
                declared_content=content,
                reason=None if content is not None else REASON_CONTENT_NOT_DECLARED,
            )
        )
        term_index += 1
    return tuple(entries) if entries else None


def _countable_entries(
    position: int, text: str
) -> tuple[ProvenanceEntry, ...] | None:
    """Every countable term of one amount, or None when the line is not that pure form.

    `text` is the normalized amount. A countable expression is one or more
    `<count> <countable word>` terms joined by `,`, `и`, `плюс` or `+`, where the word is
    in the curated table. Anything else -- a countable word mixed with another measure or
    a package, a word outside the table, trailing prose -- returns None: the caller then
    reports the whole line as unsupported instead of excusing it as a bare count or
    salvaging a valid-looking term from a rejected expression.
    """
    entries: list[ProvenanceEntry] = []
    term_index = 0
    cursor = 0
    while True:
        remainder = text[cursor:].strip()
        if remainder in ("", "."):
            break
        if term_index:
            separator = _TERM_SEPARATOR.match(text, cursor)
            if separator is None:
                return None
            cursor = separator.end()
        head = _COUNTABLE_HEAD.match(text, cursor)
        if head is None:
            return None
        cursor = head.end()
        entries.append(
            _entry(
                position,
                term_index,
                ProvenanceKind.OTHER,
                TermProvenanceStatus.NOT_APPLICABLE,
                reason=QuantityReason.COUNTABLE.value,
            )
        )
        term_index += 1
    return tuple(entries) if entries else None


def _audit_amount(position: int, amount: str) -> tuple[ProvenanceEntry, ...]:
    """The provenance of one stored amount, at most one entry per recognized term.

    A term-level answer comes from the accepted parser's parts. A line the parser refuses
    as a whole yields one entry, even when one of its terms looks valid: no sub-quantity
    and no declaration is salvaged from a rejected expression.
    """
    normalized = _normalize(amount)
    quantity = parse_quantity(amount, allow_nonstandard_glass_sizes=True)

    if quantity.is_quantified:
        return tuple(
            _audit_part(position, term_index, part)
            for term_index, part in enumerate(quantity.parts)
        )

    # A package is not a unit the accepted parser can quantify, so its content is read by
    # B1.1's own bounded recognizer. Any package mention comes here before the parser's
    # reason, whose whole-line prechecks can otherwise mask the package construct.
    if _PACKAGE_MENTION.search(normalized):
        packages = _package_entries(position, normalized)
        if packages is not None:
            return packages
        return (
            _entry(
                position,
                0,
                ProvenanceKind.PACKAGE,
                TermProvenanceStatus.UNSUPPORTED_OTHER,
                reason=REASON_PACKAGE_CONSTRUCT_UNSUPPORTED,
            ),
        )

    reason = quantity.reason
    if reason is QuantityReason.INVALID_GLASS_SIZE:
        return (
            _entry(
                position,
                0,
                ProvenanceKind.GLASS,
                TermProvenanceStatus.INVALID_CAPACITY,
                reason=REASON_CAPACITY_OUT_OF_RANGE,
            ),
        )
    if reason is QuantityReason.US_CUP:
        return (
            _entry(
                position,
                0,
                ProvenanceKind.CUP,
                TermProvenanceStatus.UNSUPPORTED_OTHER,
                reason=reason.value,
            ),
        )
    if reason is QuantityReason.QUALITATIVE:
        # The parser accepts a qualitative phrase only when the whole line is one of its
        # curated phrases, so the reason is a positive recognition of the complete input.
        return (
            _entry(
                position,
                0,
                ProvenanceKind.OTHER,
                TermProvenanceStatus.NOT_APPLICABLE,
                reason=reason.value,
            ),
        )
    if reason in (QuantityReason.COUNTABLE, QuantityReason.UNKNOWN_UNIT):
        # A count of products needs no declaration (OD-13), but only a complete, curated
        # countable expression earns `not_applicable`. The parser's own COUNTABLE reason
        # is a whole-line keyword search, so it also fires on `1 стакан и 2 шт.`; nothing
        # is excused on that reason alone.
        countable = _countable_entries(position, normalized)
        if countable is not None:
            return countable
        if reason is QuantityReason.UNKNOWN_UNIT:
            # An unreadable unit is reported, never excused: `1 кружка`, `1 oz` and a
            # measure mixed with an unrecognized word all stay unsupported.
            return (
                _entry(
                    position,
                    0,
                    ProvenanceKind.OTHER,
                    TermProvenanceStatus.UNSUPPORTED_OTHER,
                    reason=reason.value,
                ),
            )
        return (
            _entry(
                position,
                0,
                ProvenanceKind.OTHER,
                TermProvenanceStatus.UNSUPPORTED_OTHER,
                reason=REASON_COUNTABLE_CONSTRUCT_UNSUPPORTED,
            ),
        )
    if reason is QuantityReason.NO_QUANTITY:
        # The parser reaches NO_QUANTITY by elimination: the line is empty or carries no
        # number token, and it is not one of its curated qualitative phrases (those return
        # QUALITATIVE before this check). Absence of a number is not positive recognition.
        # `кружка`, `ведро`, `тюбик`, `одна кружка`, `по вкусу и кружка` and any other
        # unrecognized numberless line are measures this audit cannot read, so the whole
        # line is reported: no declaration is excused and no `по вкусу` is salvaged from a
        # mixed line.
        return (
            _entry(
                position,
                0,
                ProvenanceKind.OTHER,
                TermProvenanceStatus.UNSUPPORTED_OTHER,
                reason=REASON_MEASURE_CONSTRUCT_UNSUPPORTED,
            ),
        )
    # A range, a heaped spoon, a malformed or unknown line: a form B1.1 cannot read. It is
    # reported as one unsupported term; no sub-quantity is salvaged from it.
    return (
        _entry(
            position,
            0,
            ProvenanceKind.OTHER,
            TermProvenanceStatus.UNSUPPORTED_OTHER,
            reason=reason.value,
        ),
    )


def audit_source_provenance(ingredients: Sequence[object]) -> ProvenanceAudit:
    """Audit every ingredient amount of one recipe, in stored order.

    `ingredients` is the validated ingredient list -- `Ingredient` objects, mappings with
    an `amount` key, or plain amount strings. The result is deterministic and idempotent:
    the same amounts always produce the same entries, in the same order. Nothing is
    written, stored or converted, and a missing declaration only makes the audit
    incomplete; it never raises for an amount the accepted parser refuses.

    A source glass capacity outside the approved 50--1000 mL fence is `invalid_capacity`.
    A term with no declaration stays `undeclared` even when another ingredient declares a
    capacity (OD-03): this function has no inheritance step and no recipe-level value.
    """
    if isinstance(ingredients, (str, bytes)) or not isinstance(
        ingredients, Sequence
    ):
        raise TypeError("ingredients must be a sequence of ingredients")
    amounts = [_amount_of(item) for item in ingredients]
    entries: list[ProvenanceEntry] = []
    for position, amount in enumerate(amounts):
        entries.extend(_audit_amount(position, amount))
    return ProvenanceAudit(tuple(entries))


__all__ = [
    "REASON_CAPACITY_NOT_DECLARED",
    "REASON_CAPACITY_OUT_OF_RANGE",
    "REASON_CONTENT_NOT_DECLARED",
    "REASON_COUNTABLE_CONSTRUCT_UNSUPPORTED",
    "REASON_MEASURE_CONSTRUCT_UNSUPPORTED",
    "REASON_PACKAGE_CONSTRUCT_UNSUPPORTED",
    "CapacityOrigin",
    "DeclaredContent",
    "ExactValue",
    "ProvenanceAudit",
    "ProvenanceAuditStatus",
    "ProvenanceEntry",
    "ProvenanceKind",
    "TermProvenanceStatus",
    "audit_source_provenance",
]
