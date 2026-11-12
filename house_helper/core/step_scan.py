"""KH-F01-B1.1-B: the deterministic step scanner and its explicit ingredient binder.

This module is Stage B only. It reads a stored preparation step and reports every
quantity expression it can classify, with exact half-open code-point offsets into the
stored text, and it links an expression to a structured ingredient only through one of
the four frozen local constructions of the approved KH-F01 v0.6 specification (15.5.6):

* L1 -- the ingredient name follows the amount (``500 мл воды``);
* L2 -- the name precedes the amount after one explicit separator (``Вода — 500 мл``);
* L3 -- the name precedes the amount through one closed copular verb (``Воду добавьте
  500 мл``);
* L4 -- a coordinated list whose every amount carries its own following name.

Nothing else links. A nearby name, the fact that the sentence names exactly one
ingredient, a plausible culinary assumption or a pronoun never links an amount. When an
expression cannot be linked defensibly the result is ``unresolved``, ``ambiguous`` or
``shared``; a construct outside the approved grammar is ``unsupported``. No such result
is ever promoted to ``linked``.

The scanner is atomic. A candidate is expanded to its maximal expression and classified
as a whole: a range, a heaped spoon, a US cup, a package, a countable form, an unbalanced
capacity or an unknown construct yields one rejected span and never a salvaged
sub-quantity. The original text is never normalized or rewritten; span text is always the
exact slice of the stored step.

Internally the scanner is a bounded parsing pipeline. Numeric tokens, parts, capacity
declarations and trailing continuations are consumed exactly once, left to right; a
candidate is expanded to its maximal supported-or-rejected extent before any binding;
and the core parser's validation text is assembled in one forward pass over that
expression. A dependent explicit continuation that is incomplete (`100 мл +`), a repeated
explicit separator run (`100 мл ++`) or a second capacity declaration rejects the whole
expression, never a supported prefix. Binding then requires positive evidence from the
approved L1--L4 grammar; an inconclusive or uncertain coordination is `unresolved`, never
a speculative link or a shared group.

Stage B adds no arithmetic and no conversion. A linked amount is eligible for a future
step hint, but no hint, capacity, equivalent or persistence is produced here. The result
structures are internal to this stage; the typed public API, the Web layer and Telegram
presentation are later, separately authorized stages.

The module is pure Core: standard library and the accepted KH-F01-B0 parser, no database,
web, Telegram, OpenAI or configuration, and no outside call. It is deterministic and
idempotent: the same step and the same ingredient names produce the same spans in the
same order, and a dictionary is never used to break a tie.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from house_helper.core.measures import (
    GLASS_ML_MAX,
    GLASS_ML_MIN,
    QuantityReason,
    Unit,
    parse_quantity,
)

# The approved analysis limits (AC87 / 15.14). They bound derived spans, never the stored
# text: an omitted span is counted, and the step is still returned with its exact text.
MAX_SPANS_PER_STEP = 32
MAX_SPANS_PER_RECIPE = 128


class AmountLinkStatus(str, Enum):
    """The closed outcome of one amount expression. The values are stable for callers."""

    LINKED = "linked"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    SHARED = "shared"
    UNSUPPORTED = "unsupported"


class AmountLinkReason(str, Enum):
    """The closed machine-readable reason of one outcome (15.5.6, 15.5.8)."""

    NAME_AFTER = "name_after"
    NAME_BEFORE_SEPARATOR = "name_before_separator"
    NAME_BEFORE_COPULA = "name_before_copula"
    SHARED_AMOUNT = "shared_amount"
    MULTIPLE_CANDIDATES = "multiple_candidates"
    ANAPHORA = "anaphora"
    NO_LOCAL_BINDING = "no_local_binding"
    NO_MENTION = "no_mention"
    RANGE = "range"
    US_CUP = "us_cup"
    PACKAGE = "package"
    COUNTABLE = "countable"
    HEAPED = "heaped"
    UNSUPPORTED_CONSTRUCT = "unsupported_construct"
    MALFORMED_EXPRESSION = "malformed_expression"


@dataclass(frozen=True)
class StepAmount:
    """One classified amount expression of one step.

    ``start`` and ``end`` are half-open offsets in Unicode code points of the exact stored
    step, so ``step_text[start:end]`` is exactly ``text``. ``ingredient_position`` is the
    zero-based position in the validated ingredient list and is present only for a
    ``linked`` span; every other status keeps it ``None``. ``approximate`` records a
    source word (``около``, ``примерно``, ``приблизительно``) for honest presentation by a
    later stage; it changes no arithmetic and is never a conversion.
    """

    step_index: int
    start: int
    end: int
    text: str
    status: AmountLinkStatus
    reason: AmountLinkReason
    ingredient_position: int | None = None
    approximate: bool = False


@dataclass(frozen=True)
class StepAmountScan:
    """The spans of one step, in start order.

    ``text`` is the step exactly as stored. ``omitted`` counts spans left out by the
    per-step or per-recipe cap; the stored text is never truncated or rewritten.
    """

    step_index: int
    text: str
    amounts: tuple[StepAmount, ...]
    omitted: int = 0


@dataclass(frozen=True)
class RecipeStepScan:
    """The spans of every step of one recipe, grouped by step and kept in step order."""

    steps: tuple[StepAmountScan, ...]
    omitted: int = 0

    @property
    def complete(self) -> bool:
        """True when no span was omitted by a cap."""
        return self.omitted == 0

    @property
    def amounts(self) -> tuple[StepAmount, ...]:
        """Every kept span of the recipe, flattened in step and start order."""
        return tuple(amount for step in self.steps for amount in step.amounts)


# --- text and numeric grammar ---------------------------------------------------------

_YO_LETTER = chr(0x0451)
# Step text is protocol-bounded to one line, so vertical whitespace (a newline or a
# carriage return) is deliberately not a separation character: a newline rule could
# shift an offset. These horizontal Unicode space separators are the only blank
# characters the grammar accepts. Each one is matched in place, so a thin or em space
# never changes the exact code-point span.
_BLANK_CODES = (
    0x0009,  # tab
    0x0020,  # space
    0x00A0,  # no-break space
    0x1680,  # ogham space mark
    *(range(0x2000, 0x200B)),  # en quad .. hair space
    0x202F,  # narrow no-break space
    0x205F,  # medium mathematical space
    0x3000,  # ideographic space
)
_BLANK_CHARS = "".join(chr(code) for code in _BLANK_CODES)
_WS = "[" + "".join(re.escape(chr(code)) for code in _BLANK_CODES) + "]"

_VULGAR_CHARS = "¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞"
# The exact rational behind each vulgar fraction, as numerator and denominator. A glass
# capacity may be written with one (`237½ мл`); the value is never a float and never a
# rounded decimal.
_VULGAR_VALUES = {
    "¼": (1, 4),
    "½": (1, 2),
    "¾": (3, 4),
    "⅐": (1, 7),
    "⅑": (1, 9),
    "⅒": (1, 10),
    "⅓": (1, 3),
    "⅔": (2, 3),
    "⅕": (1, 5),
    "⅖": (2, 5),
    "⅗": (3, 5),
    "⅘": (4, 5),
    "⅙": (1, 6),
    "⅚": (5, 6),
    "⅛": (1, 8),
    "⅜": (3, 8),
    "⅝": (5, 8),
    "⅞": (7, 8),
}
_INT = r"[0-9]+"

# The same numeric forms the accepted B0 parser reads. The alternatives are ordered so a
# mixed number is never read as a bare integer, and every form is bounded by a digit run.
_NUMBER = (
    rf"(?:"
    rf"{_INT}{_WS}+{_INT}/{_INT}"
    rf"|{_INT}{_WS}*[{_VULGAR_CHARS}]"
    rf"|[{_VULGAR_CHARS}]"
    rf"|{_INT}/{_INT}"
    rf"|{_INT}[.,]{_INT}"
    rf"|{_INT}"
    rf")"
)
_NUMBER_RE = re.compile(_NUMBER)

# The accepted parser's Russian unit spellings, with a whole-word boundary that the
# whole-line parser does not need. Without the boundary ``180 градусов`` would begin with
# a gram ``г`` and ``1 ложка`` with a litre ``л``; the boundary is what keeps those words
# out of the grammar.
_UNIT = (
    rf"(?:"
    rf"грамм(?:ов|а)?|гр\.?|г\.?"
    rf"|килограмм(?:ов|а)?|кг\.?"
    rf"|миллилитр(?:ов|а)?|мл\.?"
    rf"|литр(?:ов|а)?|л\.?"
    rf"|ч\.?{_WS}*(?:лож(?:к(?:а|и|у)|ек)|л\.?)"
    rf"|чайная{_WS}+ложка|чайной{_WS}+ложки|чайную{_WS}+ложку"
    rf"|чайные{_WS}+ложки|чайных{_WS}+ложек"
    rf"|ст\.?{_WS}*(?:лож(?:к(?:а|и|у)|ек)|л\.?)"
    rf"|столовая{_WS}+ложка|столовой{_WS}+ложки|столовую{_WS}+ложку"
    rf"|столовые{_WS}+ложки|столовых{_WS}+ложек"
    rf"|стакан(?:ов|а)?"
    rf")"
    rf"(?![а-я{_YO_LETTER}a-z])"
)
_UNIT_RE = re.compile(rf"(?P<unit>{_UNIT})", re.IGNORECASE)
_HEAD_PART_RE = re.compile(
    rf"(?P<number>{_NUMBER}){_WS}*(?P<unit>{_UNIT})", re.IGNORECASE
)

# The millilitre spellings a glass capacity may be written in.
_ML_UNIT = rf"(?:мл\.?|миллилитр(?:а|ов)?)(?![а-я{_YO_LETTER}a-z])"
_CAPACITY_PAREN_RE = re.compile(
    rf"{_WS}*\({_WS}*(?P<size>{_NUMBER}){_WS}*(?P<unit>{_ML_UNIT}){_WS}*\)",
    re.IGNORECASE,
)
_CAPACITY_BARE_RE = re.compile(
    rf"{_WS}*(?:на{_WS}+)?(?P<size>{_NUMBER}){_WS}*(?P<unit>{_ML_UNIT})",
    re.IGNORECASE,
)
# A capacity introduced by "на" whose unit is not a millilitre is a malformed
# declaration (`1 стакан на 200 л`); it must be consumed, not split into a tail amount.
_CAPACITY_NA_PREFIX_RE = re.compile(rf"{_WS}*на{_WS}+", re.IGNORECASE)
# A local bound on a capacity fraction's digit runs, so an oversized token is a
# deterministic malformed expression rather than an interpreter integer-limit error.
_MAX_CAPACITY_NUMBER_DIGITS = 64
_QUALIFIER_RE = re.compile(
    rf"{_WS}*(?P<qualifier>"
    rf"без{_WS}+горки|с{_WS}+горкой|с{_WS}+верхом"
    rf")(?![а-я{_YO_LETTER}a-z])",
    re.IGNORECASE,
)
_SEPARATOR_RE = re.compile(
    rf"{_WS}*(?P<separator>\+|и(?![а-я{_YO_LETTER}a-z])"
    rf"|плюс(?![а-я{_YO_LETTER}a-z])){_WS}*",
    re.IGNORECASE,
)
# Only an explicit arithmetic sign continues a rejected branch into a following term.
# The natural-language "и" is left alone there: two rejected forms joined by "и"
# (``1 cup и 1 cup``) are independent spans, not one expression.
_PLUS_SEPARATOR_RE = re.compile(
    rf"{_WS}*(?P<separator>\+|плюс(?![а-я{_YO_LETTER}a-z])){_WS}*",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(
    rf"(?P<first>{_NUMBER}){_WS}*"
    rf"(?:до(?![а-я{_YO_LETTER}a-z])|–|—|-){_WS}*"
    rf"(?P<second>{_NUMBER})",
    re.IGNORECASE,
)

_US_CUP_WORD = (
    rf"(?:cups?|чашк(?:а|и|у|ой|е|ам|ами|ах)|чашек)"
    rf"(?![а-я{_YO_LETTER}a-z])"
)
_PACKAGE_WORD = (
    rf"(?:"
    rf"пачк(?:а|и|у|ой|е|ам|ами|ах)|пачек"
    rf"|банк(?:а|и|у|ой|е|ам|ами|ах)|банок"
    rf"|баночк(?:а|и|у|ой|е|ам|ами|ах)|баночек"
    rf"|упаковк(?:а|и|у|ой|е|ам|ами|ах)|упаковок"
    rf"|пакет(?:а|у|ом|е|ы|ов|ам|ами|ах)?"
    rf")(?![а-я{_YO_LETTER}a-z])"
)
_COUNTABLE_WORD = (
    rf"(?:"
    rf"яйц(?:о|а|у|ом|е|ам|ами|ах)|яиц"
    rf"|луковиц(?:а|ы|у|ей|е|ам|ами|ах)?"
    rf"|шт\.?|штук(?:а|и|у|ой|е|ам|ами|ах)?"
    rf"|зубчик(?:а|у|ом|е|и|ов|ам|ами|ах)?"
    rf"|головк(?:а|и|у|ой|е|ам|ами|ах)|головок"
    rf"|ломтик(?:а|у|ом|е|и|ов|ам|ами|ах)?"
    rf")(?![а-я{_YO_LETTER}a-z])"
)
_REJECTED_RE = re.compile(
    rf"(?P<number>{_NUMBER}){_WS}*(?:"
    rf"(?P<us_cup>{_US_CUP_WORD})"
    rf"|(?P<package>{_PACKAGE_WORD})"
    rf"|(?P<countable>{_COUNTABLE_WORD})"
    rf")",
    re.IGNORECASE,
)

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

_METRIC_UNITS = frozenset(
    {Unit.GRAM, Unit.KILOGRAM, Unit.MILLILITER, Unit.LITER}
)
_SPOON_UNITS = frozenset({Unit.TEASPOON, Unit.TABLESPOON})

_SEPARATORS = frozenset({"—", "–", ":", "="})
_COORDINATORS = frozenset({"и", "плюс", "или", "либо"})
_FUNCTION_WORDS = frozenset({"и", "из", "от", "с", "со", "до", "по", "для", "без"})
# The closed positive bridge set for L1: the only function words that may sit between an
# amount and its governed head noun. A negating or excluding word (`без воды`) is not a
# bridge, so an amount it excludes never links.
_L1_BRIDGE_WORDS = frozenset({"из", "от", "с", "со"})
_COPULA_VERBS = frozenset(
    {
        "добавьте",
        "влейте",
        "всыпьте",
        "положите",
        "возьмите",
        "нужно",
        "потребуется",
        "отмерьте",
    }
)
_APPROXIMATE_WORDS = frozenset({"около", "примерно", "приблизительно"})
_ANAPHORA_WORDS = frozenset(
    {
        "его",
        "ее",
        "их",
        "этого",
        "этой",
        "этот",
        "эту",
        "этим",
        "этом",
        "половина",
        "половину",
        "половины",
        "половиной",
        "часть",
        "части",
        "частью",
        "остаток",
        "остатка",
        "остатки",
        "столько",
        "эти",
        "этих",
        "тех",
    }
)
# Permitted particles in a governing anaphora chain (`этих же указанных 500 мл`). They
# carry no amount; they are skipped so that the governing portion word is still found.
_GOVERNING_PARTICLES = frozenset({"же", "именно", "ведь"})
# Words that may sit between a governing portion/anaphora word and its numeric span
# without breaking the governance link (`половину от указанных 500 мл`). This is a
# closed local grammar, not pronoun resolution: any other word stops the scan.
_ANAPHORA_LINKERS = frozenset({"от", "из", "с", "со", "по", "до", "о", "об"})
# A closed list of quantifier/determiner and reflexively marked participle words that may
# sit inside a governing anaphora chain even though they are not ordinary adjectives
# (`от всех указанных 500 мл`, `от имеющихся 500 мл`). Every form is listed explicitly:
# this is product data, never a POS tagger, and an unlisted word stops the chain.
_GOVERNING_WORDS = frozenset(
    {
        "все",
        "всех",
        "всей",
        "всего",
        "всем",
        "всеми",
        "обоих",
        "обеих",
        "имеющегося",
        "имеющемуся",
        "имеющимся",
        "имеющимися",
        "имеющиеся",
        "имеющихся",
        "имевшимся",
        "оставшегося",
        "оставшемуся",
        "оставшимися",
        "оставшиеся",
        "оставшихся",
    }
)
# Neutral fillers that may sit between a portion word and an amount without ending the
# portion relation (`половину, ровно 500 мл`, `половину, и 500 мл`). The same curated set
# is consulted by both governance scans, so a filler behaves identically whether a comma
# separates the portion word from the amount or not. They never make a link positive:
# they can only keep a possible portion relation alive, which yields `unresolved`.
_PORTION_FILLER_WORDS = frozenset(
    {
        "ровно",
        "только",
        "лишь",
        "даже",
        "также",
        "точно",
        "еще",
        "уже",
        "всего",
        "целых",
        "полных",
        "просто",
        "именно",
        "и",
        "или",
        "либо",
    }
)
# A closed list of container-capacity words. An amount governed by one is a cookware
# capacity, never an ingredient quantity (`кастрюлю объемом 3 л`).
_CAPACITY_CONTEXT_WORDS = frozenset(
    {
        "объем",
        "объема",
        "объемом",
        "объеме",
        "объемы",
        "объемов",
        "вместимость",
        "вместимости",
        "вместимостью",
        "емкость",
        "емкости",
        "емкостью",
    }
)
# A closed list of cookware noun stems. `кастрюлю на 3 л` is a container capacity, not an
# ingredient quantity; it is recognised only through the bounded pattern
# `cookware + на + amount`, so a bare `на` never hides a real ingredient amount.
_COOKWARE_STEMS = (
    "кастрюл",
    "чаш",
    "миск",
    "сковород",
    "казан",
    "сотейник",
    "ковш",
    "чугун",
    "форм",
    "противен",
    "жаровн",
    "котел",
)
# A closed list of common recipe finite verbs. It exists only so the suffix heuristic
# cannot read a verb such as `налей` (ending `-ей`) as an adjective modifier; it is not
# a general morphological analyser.
_FINITE_VERBS = frozenset(
    {
        "налей",
        "налейте",
        "залей",
        "залейте",
        "пролей",
        "пролейте",
        "разогрей",
        "разогрейте",
        "согрей",
        "согрейте",
        "открой",
        "откройте",
        "закрой",
        "закройте",
        "накрой",
        "накройте",
        "промой",
        "промойте",
        "слей",
        "слейте",
        "добавь",
        "добавьте",
        "влей",
        "влейте",
        "всыпь",
        "всыпьте",
        "положи",
        "положите",
        "возьми",
        "возьмите",
        "смешай",
        "смешайте",
        "перемешай",
        "перемешайте",
        "доведи",
        "доведите",
        "вари",
        "варите",
        "свари",
        "сварите",
        "готовь",
        "готовьте",
        "оставь",
        "оставьте",
        "отмерь",
        "отмерьте",
        "процеди",
        "процедите",
        "взбей",
        "взбейте",
        "нагрей",
        "нагрейте",
        "обжарь",
        "обжарьте",
        "туши",
        "тушите",
        "посыпь",
        "посыпьте",
        "смажь",
        "смажьте",
        "нарежь",
        "нарежьте",
        "измельчи",
        "измельчите",
        "соедини",
        "соедините",
        "выложи",
        "выложите",
        "переложи",
        "переложите",
        "сними",
        "снимите",
        "мешая",
        "помешивая",
        "перемешивая",
        "взбивая",
        "подливая",
        "убавляя",
    }
)

# A closed list of sentence-ending abbreviations. The period of a unit abbreviation
# belongs to the abbreviation; a period that starts a new sentence (a capital letter or a
# digit follows) is still a boundary, so a name in the next sentence never links.
_ABBREVIATIONS = frozenset(
    {
        "ст",
        "ч",
        "г",
        "гр",
        "кг",
        "мг",
        "мл",
        "л",
        "шт",
        "др",
        "т",
        "д",
        "п",
        "им",
        "ул",
        "рис",
        "мин",
        "сек",
        "см",
        "мм",
        "руб",
        "коп",
        "проц",
        "прим",
    }
)

# The curated inflection groups. They are product data, versioned with the code, and
# contain no user data. The approved guarded stem rule covers the rest of a noun's or an
# adjective's ordinary endings; anything outside both stays unresolved rather than being
# merged with an unrelated product.
_CURATED_GROUPS = (
    frozenset(
        {
            "яйцо",
            "яйца",
            "яйцу",
            "яйцом",
            "яйце",
            "яйцам",
            "яйцами",
            "яйцах",
            "яиц",
        }
    ),
    frozenset({"молоко", "молока", "молоку", "молоком", "молоке"}),
    frozenset({"мука", "муки", "муке", "муку", "мукой"}),
    frozenset({"вода", "воды", "воде", "воду", "водой", "водою"}),
    frozenset({"соль", "соли", "солью"}),
    frozenset({"масло", "масла", "маслу", "маслом", "масле"}),
    frozenset({"сахар", "сахара", "сахару", "сахаром", "сахаре"}),
    frozenset({"лук", "лука", "луку", "луком", "луке"}),
    # Words whose stem loses a fleeting vowel; the guarded stem rule cannot pair them.
    frozenset(
        {
            "перец",
            "перца",
            "перцу",
            "перцем",
            "перце",
            "перцы",
            "перцев",
            "перцам",
            "перцами",
            "перцах",
        }
    ),
    frozenset({"чай", "чая", "чаю", "чаем", "чае"}),
)
_FORM_KEYS: dict[str, str] = {}
for _group in _CURATED_GROUPS:
    _key = min(_group)
    for _form in _group:
        _FORM_KEYS[_form] = _key

# Curated aliases, not a general thesaurus. Two names in one group are the same product
# only when the recipe does not contain both; if it contains both, both are candidates and
# the mention is ambiguous rather than silently preferring one.
_ALIAS_GROUPS = (
    frozenset({"помидор", "томат"}),
    frozenset({"картофель", "картошка"}),
)
_ALIAS_KEYS: dict[str, str] = {}
for _group in _ALIAS_GROUPS:
    _key = min(_group)
    for _form in _group:
        _ALIAS_KEYS[_form] = _key

# The closed adjective/participle endings used only to recognise context words between an
# amount and its name. They are not a morphological analyser and never identify an
# ingredient by themselves.
_ADJECTIVE_ENDINGS = tuple(
    sorted(
        {
            "ыми",
            "ими",
            "ого",
            "его",
            "ому",
            "ему",
            "ых",
            "их",
            "ым",
            "им",
            "ые",
            "ие",
            "ый",
            "ий",
            "ой",
            "ая",
            "яя",
            "ое",
            "ее",
            "ую",
            "юю",
            "ей",
        },
        key=len,
        reverse=True,
    )
)

# The phrase-boundary modifier endings. They are the adjective/participle endings minus
# `-ей`, which is also the ordinary imperative ending (`подлей`, `отлей`). A coordinated
# name group may be extended leftward over these endings even when the word's stem is not
# in the curated lexicon: doing so can only produce `shared`/`unresolved`, never a link.
_PHRASE_MODIFIER_ENDINGS = tuple(
    ending for ending in _ADJECTIVE_ENDINGS if ending != "ей"
)

# The closed positive lexicon of step-text modifier stems used by the L1 bridge and by a
# governing anaphora chain. Positive evidence is lexical: a word is a modifier only when
# its stem is one of these curated cooking adjectives/participles *and* its remainder is
# one of the closed adjective endings above. An unknown word is never promoted merely
# because it looks like an adjective (`бледного` is not `свежего`). This is product data,
# versioned with the code, and never a general morphological analyser.
_MODIFIER_STEMS = frozenset(
    {
        "апельсинов",
        "бальзамическ",
        "барань",
        "бел",
        "бланширован",
        "варен",
        "взбит",
        "винн",
        "газирован",
        "говяжь",
        "горчичн",
        "горяч",
        "готов",
        "гречнев",
        "густ",
        "домашн",
        "дроблен",
        "желт",
        "жарен",
        "жидк",
        "заморожен",
        "запечен",
        "зелен",
        "измельчен",
        "картофельн",
        "квашен",
        "кипячен",
        "кисл",
        "копчен",
        "консервирован",
        "красн",
        "крупн",
        "крупнорублен",
        "кукурузн",
        "курин",
        "лимонн",
        "манн",
        "маринован",
        "мелк",
        "мелкорублен",
        "минеральн",
        "молочн",
        "молот",
        "морожен",
        "морск",
        "мясн",
        "мягк",
        "нарезан",
        "нашинкован",
        "натуральн",
        "негазирован",
        "обезжирен",
        "обжарен",
        "овсян",
        "оливков",
        "остр",
        "отварен",
        "очищен",
        "охлажден",
        "пастеризован",
        "перлов",
        "печен",
        "питьев",
        "плавлен",
        "поджарен",
        "подсолнечн",
        "полукопчен",
        "полутверд",
        "порублен",
        "потушен",
        "приготовлен",
        "прожарен",
        "проварен",
        "пропарен",
        "просеян",
        "протерт",
        "пшенн",
        "пшеничн",
        "размягчен",
        "расплавлен",
        "растительн",
        "растерт",
        "растоплен",
        "рафинирован",
        "репчат",
        "ржан",
        "рисов",
        "рублен",
        "рыбн",
        "сварен",
        "свеж",
        "свежевыжат",
        "свежеморожен",
        "свежекопчен",
        "свин",
        "сгущен",
        "сливочн",
        "смешан",
        "сметанн",
        "соев",
        "солен",
        "стерилизован",
        "столов",
        "сушен",
        "сыр",
        "сырн",
        "тверд",
        "творожн",
        "тепл",
        "терт",
        "толчен",
        "томатн",
        "тонкорублен",
        "тушен",
        "указанн",
        "ультрапастеризован",
        "фильтрован",
        "холодн",
        "цельн",
        "черн",
        "шинкован",
        "яблочн",
        "ячменн",
    }
)

# The approved guarded stem endings. Longest first so ``ой`` is tried before ``й`` and
# ``ами`` before ``и``; a strip is allowed only when at least three characters remain.
_STEM_ENDINGS = tuple(
    sorted(
        {
            "ами",
            "ями",
            "ах",
            "ях",
            "ов",
            "ев",
            "ей",
            "ой",
            "ом",
            "ем",
            "ам",
            "ям",
            "ою",
            "ею",
            "ью",
            "ы",
            "и",
            "а",
            "я",
            "у",
            "ю",
            "е",
            "о",
            "ь",
            "й",
        }
        | set(_ADJECTIVE_ENDINGS),
        key=len,
        reverse=True,
    )
)


def _norm(text: str) -> str:
    """The matching form of one word or phrase: lower case with one e spelling."""
    return text.replace(_YO_LETTER, "е").lower()


def _skip_ws(text: str, position: int) -> int:
    while position < len(text) and text[position] in _BLANK_CHARS:
        position += 1
    return position


def _unit_key(text: str) -> str:
    return _norm(re.sub(r"\s+", "", text)).replace(".", "")


def _topic(text: str) -> str:
    """A whole-line matching form, used only for qualifier words."""
    return re.sub(r"\s+", " ", _norm(text)).strip()


# --- sentence segmentation and words --------------------------------------------------


@dataclass(frozen=True)
class _Word:
    start: int
    end: int
    norm: str
    sentence: int


def _previous_word(ends: Sequence[int], norms: Sequence[str], position: int) -> str:
    """The normalized word ending at or before ``position``, via a binary search."""
    index = bisect_right(ends, position) - 1
    return norms[index] if index >= 0 else ""


def _dot_ends_sentence(
    text: str, position: int, ends: Sequence[int], norms: Sequence[str]
) -> bool:
    """Whether the period at ``position`` starts a new sentence.

    A decimal point and an abbreviation's own period do not. An abbreviation's period
    still ends a sentence when a capital letter or a digit follows, so a name in the next
    sentence is never reached across a boundary.
    """
    if (
        position > 0
        and position + 1 < len(text)
        and text[position - 1].isdigit()
        and text[position + 1].isdigit()
    ):
        return False
    previous = _previous_word(ends, norms, position)
    next_position = _skip_ws(text, position + 1)
    if previous in _ABBREVIATIONS or len(previous) == 1:
        if next_position >= len(text):
            return True
        return text[next_position].isupper() or text[next_position].isdigit()
    return True


def _sentence_of(bounds: Sequence[int], position: int) -> int:
    return max(0, bisect_right(bounds, position) - 1)


def _tokenize(text: str) -> tuple[list[int], list[_Word]]:
    """One left-to-right pass: sentence bounds and word tokens with exact offsets.

    Words are collected first, then the sentence boundaries are found using a binary
    search over the word ends, so a period never triggers a backward rescan and the whole
    pass stays linear in the step length.
    """
    raw: list[tuple[int, int, str]] = []
    position = 0
    length = len(text)
    while position < length:
        if not text[position].isalpha():
            position += 1
            continue
        end = position + 1
        while end < length and text[end].isalpha():
            end += 1
        raw.append((position, end, _norm(text[position:end])))
        position = end
    ends = tuple(item[1] for item in raw)
    norms = tuple(item[2] for item in raw)
    bounds = [0]
    for match in re.finditer(r"[.!?;]", text):
        dot = match.start()
        if text[dot] == "." and not _dot_ends_sentence(text, dot, ends, norms):
            continue
        bounds.append(dot + 1)
    words = [
        _Word(
            start=start,
            end=end,
            norm=norm,
            sentence=_sentence_of(bounds, start),
        )
        for start, end, norm in raw
    ]
    return bounds, words


# --- ingredient identities ------------------------------------------------------------


@dataclass(frozen=True)
class _IngredientInfo:
    position: int
    head: str
    modifiers: tuple[str, ...]


def _name_of(item: object) -> str:
    if isinstance(item, str):
        return item
    name = getattr(item, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(item, Mapping):
        name = item.get("name")
        if isinstance(name, str):
            return name
    raise TypeError("an ingredient must be a string or carry a string name")


def _looks_like_adjective(word: str) -> bool:
    return any(
        word.endswith(ending) and len(word) > len(ending)
        for ending in _ADJECTIVE_ENDINGS
    )


def _ends_with_modifier_ending(word: str) -> bool:
    """The positive modifier test used by the L1 bridge and an anaphora chain.

    A word is a permitted modifier only through the curated positive stem lexicon and
    one of the closed adjective endings. An unknown token is never promoted because it
    merely looks like an adjective or ends like an imperative.
    """
    for ending in _ADJECTIVE_ENDINGS:
        if not word.endswith(ending) or len(word) <= len(ending):
            continue
        stem = word[: -len(ending)]
        if stem in _MODIFIER_STEMS:
            return True
        # The regular doubled-n adjective/participle suffix (`нарезанный`,
        # `измельченный`) keeps the lexical root one character shorter.
        if stem.endswith("н") and stem[:-1] in _MODIFIER_STEMS:
            return True
    return False


def _ends_with_phrase_modifier_ending(word: str) -> bool:
    """The looser modifier test used only for phrase and coordinated-group boundaries.

    A curated modifier qualifies. Otherwise a closed adjective ending other than `-ей`
    qualifies, so an unlisted participle such as `шоколадного` still extends a noun
    phrase. The result of this test never links an amount by itself: it only lets a
    known mention be reached from an explicit coordinator or comma boundary.
    """
    if _ends_with_modifier_ending(word):
        return True
    return any(
        word.endswith(ending) and len(word) > len(ending)
        for ending in _PHRASE_MODIFIER_ENDINGS
    )


def _split_name(name: str) -> tuple[str, ...]:
    tokens = []
    position = 0
    length = len(name)
    while position < length:
        if not name[position].isalpha():
            position += 1
            continue
        end = position + 1
        while end < length and name[end].isalpha():
            end += 1
        tokens.append(_norm(name[position:end]))
        position = end
    return tuple(tokens)


def _head_of(tokens: tuple[str, ...]) -> int:
    """The head noun's index: the last token that is not adjective-like."""
    for index in range(len(tokens) - 1, -1, -1):
        if not _looks_like_adjective(tokens[index]):
            return index
    return len(tokens) - 1


def _ingredient_infos(names: Sequence[str]) -> tuple[_IngredientInfo, ...]:
    infos: list[_IngredientInfo] = []
    for position, name in enumerate(names):
        tokens = _split_name(name)
        if not tokens:
            continue
        head_index = _head_of(tokens)
        infos.append(
            _IngredientInfo(
                position=position,
                head=tokens[head_index],
                modifiers=tuple(
                    token
                    for index, token in enumerate(tokens)
                    if index != head_index
                ),
            )
        )
    return tuple(infos)


def _stem(word: str) -> str:
    for ending in _STEM_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


# Sorted construction makes a hypothetical stem collision resolve the same way on every
# run, even though a frozenset's own iteration order is not part of the contract.
_FORM_STEM_KEYS = {
    _stem(form): key for form, key in sorted(_FORM_KEYS.items())
}
_ALIAS_STEM_KEYS = {
    _stem(form): key for form, key in sorted(_ALIAS_KEYS.items())
}


def _curated_key(word: str) -> str | None:
    key = _FORM_KEYS.get(word)
    if key is not None:
        return key
    return _FORM_STEM_KEYS.get(_stem(word))


def _alias_key(word: str) -> str | None:
    key = _ALIAS_KEYS.get(word)
    if key is not None:
        return key
    return _ALIAS_STEM_KEYS.get(_stem(word))


def _same_curated(left: str, right: str) -> bool:
    """Curated product identity: one inflection group or one curated alias group.

    This is product data, not a morphological guess, so it settles identity by itself.
    """
    left_key = _curated_key(left)
    if left_key is not None and left_key == _curated_key(right):
        return True
    left_alias = _alias_key(left)
    return left_alias is not None and left_alias == _alias_key(right)


def _same_word(left: str, right: str) -> bool:
    """A deterministic word identity: exact, curated group, curated alias or guarded stem."""
    if left == right or _same_curated(left, right):
        return True
    left_stem = _stem(left)
    right_stem = _stem(right)
    return (
        len(left_stem) >= 3
        and len(right_stem) >= 3
        and left_stem == right_stem
    )


def _same_head(word: str, head: str) -> bool:
    """Whether a source word can be a form of an ingredient's head noun.

    Exact identity and curated product identity always qualify. A guarded stem match
    additionally requires both sides to belong to the same closed adjective class: an
    adjectival source form never becomes a noun by stem similarity alone, so `сырого`
    does not establish the ingredient `сыр`. The head token of an ingredient name is a
    noun by construction, so an adjectival word can only be a form of an adjectival head
    (`мороженого` of `мороженое`). The test is a class rule over the closed adjective
    ending table, never an adjective word list.
    """
    if word == head or _same_curated(word, head):
        return True
    if _looks_like_adjective(word) and not _looks_like_adjective(head):
        return False
    return _same_word(word, head)


def _whitespace_gap(text: str, left_end: int, right_start: int) -> bool:
    """Whether only whitespace sits between two adjacent words."""
    return text[left_end:right_start].strip() == ""


def _modifier_gap(text: str, left_end: int, right_start: int) -> bool:
    """Whether a gap may join modifiers inside one noun phrase: blanks or one comma."""
    return text[left_end:right_start].strip(_BLANK_CHARS) in ("", ",")


@dataclass(frozen=True)
class _WordIndex:
    """Per-word adjective facts, computed in one pass over the step.

    The scanner used to re-traverse an adjective suffix list for every word and every
    following word, which made a long modifier run quadratic. Every fact the binder
    needs is now derived from one left-to-right and one right-to-left pass.
    """

    adjectives: tuple[bool, ...]
    modifying: tuple[bool, ...]
    phrase_modifying: tuple[bool, ...]
    modifies_following: tuple[bool, ...]
    adjacent_start: tuple[int, ...]


def _build_word_index(
    text: str, words: Sequence[_Word]
) -> _WordIndex:
    nouns = len(words)
    adjectives = tuple(_looks_like_adjective(word.norm) for word in words)
    modifying = tuple(
        _ends_with_modifier_ending(word.norm) for word in words
    )
    phrase_modifying = tuple(
        _ends_with_phrase_modifier_ending(word.norm) for word in words
    )
    # `following_ok[i]` mirrors the old forward walk from word `i`: the adjective run
    # that follows it (whitespace-joined, same sentence) must end at a noun phrase.
    following_ok = [False] * nouns
    for index in range(nouns - 1, -1, -1):
        following = index + 1
        if following >= nouns:
            continue
        if words[following].sentence != words[index].sentence:
            continue
        if not _whitespace_gap(
            text, words[index].end, words[following].start
        ):
            continue
        if adjectives[following]:
            following_ok[index] = following_ok[following]
            continue
        norm = words[following].norm
        following_ok[index] = not (
            norm in _FUNCTION_WORDS
            or norm in _COORDINATORS
            or norm in _COPULA_VERBS
            or norm in _FINITE_VERBS
        )
    modifies_following = tuple(
        adjectives[index] and following_ok[index]
        for index in range(nouns)
    )
    # The first word of the whitespace-joined adjective run ending at this word. A
    # punctuation or sentence boundary ends the run, as a fixed look-back window must not
    # borrow a modifier from the previous phrase.
    adjacent_start = [0] * nouns
    for index in range(nouns):
        if not adjectives[index]:
            adjacent_start[index] = index
            continue
        if (
            index > 0
            and adjectives[index - 1]
            and words[index - 1].sentence == words[index].sentence
            and _whitespace_gap(
                text, words[index - 1].end, words[index].start
            )
        ):
            adjacent_start[index] = adjacent_start[index - 1]
        else:
            adjacent_start[index] = index
    return _WordIndex(
        adjectives=adjectives,
        modifying=modifying,
        phrase_modifying=phrase_modifying,
        modifies_following=modifies_following,
        adjacent_start=tuple(adjacent_start),
    )


def _modifiers_present(
    text: str,
    info: _IngredientInfo,
    words: Sequence[_Word],
    head_index: int,
    word_index: _WordIndex,
) -> bool:
    """Whether a specific name's distinguishing words sit in its own noun phrase.

    Only the contiguous run of adjective-like words immediately before the head, joined
    by whitespace with no punctuation or other head noun, counts. A fixed look-back
    window would let ``Сливочное молоко, 100 мл масла`` borrow ``сливочное`` from the
    previous phrase; a phrase boundary stops the run.

    The run and the head must also belong to one sentence and be joined by a permitted
    phrase gap. The run's own start is indexed with the same sentence rule, but the
    junction between the last modifier and the head is checked here: without it a
    sentence period (``Сливочное. Масло``) would let the modifier of the previous
    sentence describe this head, and a modifier run would reach a name across a
    sentence boundary.
    """
    if not info.modifiers:
        return True
    if head_index <= 0:
        return False
    modifier_index = head_index - 1
    if not word_index.adjectives[modifier_index]:
        return False
    if words[modifier_index].sentence != words[head_index].sentence:
        return False
    if not _modifier_gap(
        text, words[modifier_index].end, words[head_index].start
    ):
        return False
    start = word_index.adjacent_start[modifier_index]
    collected = tuple(word.norm for word in words[start:head_index])
    return all(
        any(_same_word(modifier, candidate) for candidate in collected)
        for modifier in info.modifiers
    )


@dataclass(frozen=True)
class _Mention:
    index: int
    positions: tuple[int, ...]


def _mention_at(
    text: str,
    index: int,
    words: Sequence[_Word],
    infos: Sequence[_IngredientInfo],
    word_index: _WordIndex,
) -> _Mention | None:
    word = words[index]
    if word_index.modifies_following[index]:
        return None
    matches = [info for info in infos if _same_head(word.norm, info.head)]
    if not matches:
        return None
    general = [info for info in matches if not info.modifiers]
    specific = [info for info in matches if info.modifiers]
    present = [
        info
        for info in specific
        if _modifiers_present(text, info, words, index, word_index)
    ]
    if present:
        return _Mention(index, tuple(sorted(info.position for info in present)))
    if general and specific:
        # A general word and a specific product share the head; without the specific
        # name's distinguishing words the text does not choose between them.
        return _Mention(
            index,
            tuple(sorted(info.position for info in general + specific)),
        )
    if general:
        return _Mention(index, tuple(sorted(info.position for info in general)))
    # A specific name needs its distinguishing words; its bare head is not a mention.
    return None


def _mentions(
    text: str,
    words: Sequence[_Word],
    infos: Sequence[_IngredientInfo],
    word_index: _WordIndex,
) -> dict[int, _Mention]:
    mentions: dict[int, _Mention] = {}
    for index in range(len(words)):
        mention = _mention_at(text, index, words, infos, word_index)
        if mention is not None:
            mentions[index] = mention
    return mentions


# --- expression scanning --------------------------------------------------------------


@dataclass(frozen=True)
class _Expression:
    start: int
    end: int
    supported: bool
    reason: AmountLinkReason | None


@dataclass(frozen=True)
class _Part:
    """One consumed part of an amount expression.

    ``substitutions`` are the parse-only replacements of exact capacity declarations, in
    source order. They never alter the reported span or the stored step text; they are
    accumulated while the part is consumed so the validation text is assembled in one
    forward pass.
    """

    unit: Unit
    end: int
    reason: AmountLinkReason | None
    substitutions: tuple[tuple[int, int, str], ...] = ()


@dataclass(frozen=True)
class _SeparatorRun:
    """One consumed run of measure separators.

    ``end`` is where the run ends including trailing blanks; ``span_end`` is the same
    position with trailing blanks trimmed, used when a dangling or repeated explicit
    separator belongs to a rejected span. ``count`` and ``explicit`` make a repeated
    explicit run detectable without re-reading the text.
    """

    end: int
    span_end: int
    count: int
    explicit: int


_MALFORMED = AmountLinkReason.MALFORMED_EXPRESSION
_UNSUPPORTED = AmountLinkReason.UNSUPPORTED_CONSTRUCT
_HEAPED = AmountLinkReason.HEAPED


def _can_start_number(text: str, position: int, floor: int = 0) -> bool:
    """Whether a number token may begin here, rather than inside a larger token.

    A directly attached sign or a space-separated thousands group is not a new number.
    A dash separated by whitespace is ordinary punctuation (an L2 separator or a
    subtraction); the only minus sign always refused there is U+2212, which is not in the
    approved separator set and must not salvage a number from a malformed range.

    ``floor`` is the first code point of the current sentence. The backwards guard never
    looks across it, so a sentence period cannot make an independent amount in the next
    sentence look like the tail of the previous one.
    """
    if position <= floor:
        return True
    previous = text[position - 1]
    if previous.isalnum() or previous == "_":
        return False
    if (
        previous in ",./"
        and position - 2 >= floor
        and text[position - 2].isdigit()
    ):
        return False
    if previous in "-−–—":
        return False
    if previous in _BLANK_CHARS:
        before = position - 1
        while before >= floor and text[before] in _BLANK_CHARS:
            before -= 1
        if before >= floor:
            if text[before].isdigit():
                return False
            if text[before] == "−":
                return False
            if text[before] in ",./":
                earlier = before - 1
                while earlier >= floor and text[earlier] in _BLANK_CHARS:
                    earlier -= 1
                if earlier >= floor and text[earlier].isdigit():
                    return False
    return True


def _starts_number(text: str, position: int, floor: int = 0) -> bool:
    if position >= len(text):
        return False
    if not (text[position].isdigit() or text[position] in _VULGAR_CHARS):
        return False
    return _can_start_number(text, position, floor)


def _consume_number(text: str, position: int) -> int:
    match = _NUMBER_RE.match(text, position)
    if match is None:
        return position + 1
    return match.end()


def _consume_to_paren(text: str, position: int, sentence_end: int) -> int:
    """Consume a balanced parenthetical region, or the rest of the sentence.

    Balancing keeps a malformed nested declaration from leaving an inner part behind to
    be scanned as an independent amount.
    """
    depth = 0
    cursor = position
    while cursor < sentence_end:
        character = text[cursor]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return sentence_end


def _match_until(
    pattern: re.Pattern[str], text: str, position: int, limit: int
) -> re.Match[str] | None:
    """A regex match that never extends past ``limit`` (a sentence boundary)."""
    match = pattern.match(text, position)
    if match is None or match.end() > limit:
        return None
    return match


def _consume_number_and_unit(
    text: str, position: int, limit: int
) -> int:
    match = _match_until(_HEAD_PART_RE, text, position, limit)
    if match is not None:
        return match.end()
    return _consume_number(text, position)


# The core's whole-line parser reads a glass-capacity declaration only as a decimal, so
# a fraction, a vulgar fraction or a vulgar mixed number there (`500/3 мл`, `237½ мл`) is
# evaluated here as an exact numerator/denominator pair. Nothing is rounded: a terminating
# value is handed to the core as its exact decimal spelling; a non-terminating value (for
# example 500/3) is validated exactly here and only the capacity declaration is left out
# of the core's parse, whose result is a validity verdict rather than a capacity value.
def _gcd(left: int, right: int) -> int:
    while right:
        left, right = right, left % right
    return left


def _bounded_capacity_digits(*groups: str) -> bool:
    return all(len(group) <= _MAX_CAPACITY_NUMBER_DIGITS for group in groups)


def _capacity_fraction(size_text: str) -> tuple[int, int] | None:
    """The exact numerator and denominator of a capacity declaration.

    Every numeric form the approved grammar permits for a capacity is read here: an
    integer, a decimal, a fraction, a mixed number, a vulgar fraction and a vulgar mixed
    number. The pair is never reduced to a float or a rounded decimal.
    """
    text = size_text.strip(_BLANK_CHARS)
    mixed = re.fullmatch(rf"([0-9]+){_WS}+([0-9]+)/([0-9]+)", text)
    if mixed is not None:
        whole, numerator, denominator = (
            mixed.group(1),
            mixed.group(2),
            mixed.group(3),
        )
    else:
        plain = re.fullmatch(r"([0-9]+)/([0-9]+)", text)
        if plain is not None:
            whole, numerator, denominator = "0", plain.group(1), plain.group(2)
        else:
            vulgar_mixed = re.fullmatch(
                rf"([0-9]+){_WS}*([{_VULGAR_CHARS}])", text
            )
            if vulgar_mixed is not None:
                whole = vulgar_mixed.group(1)
                numerator_value, denominator_value = _VULGAR_VALUES[
                    vulgar_mixed.group(2)
                ]
                if not _bounded_capacity_digits(whole):
                    return None
                return (
                    int(whole) * denominator_value + numerator_value,
                    denominator_value,
                )
            vulgar_only = re.fullmatch(rf"[{_VULGAR_CHARS}]", text)
            if vulgar_only is not None:
                return _VULGAR_VALUES[text]
            decimal = re.fullmatch(r"([0-9]+)[.,]([0-9]+)", text)
            if decimal is not None:
                whole, fractional = decimal.group(1), decimal.group(2)
                if not _bounded_capacity_digits(whole, fractional):
                    return None
                return (
                    int(whole + fractional),
                    10 ** len(fractional),
                )
            integer = re.fullmatch(r"[0-9]+", text)
            if integer is None or not _bounded_capacity_digits(text):
                return None
            return int(text), 1
    if not _bounded_capacity_digits(whole, numerator, denominator):
        return None
    denominator_value = int(denominator)
    if denominator_value == 0:
        return None
    return int(whole) * denominator_value + int(numerator), denominator_value


def _within_glass_fence(numerator: int, denominator: int) -> bool:
    """Whether ``numerator / denominator`` is inside the accepted 50--1000 mL fence."""
    return (
        numerator * GLASS_ML_MIN.denominator >= GLASS_ML_MIN.numerator * denominator
        and numerator * GLASS_ML_MAX.denominator
        <= GLASS_ML_MAX.numerator * denominator
    )


def _exact_decimal(numerator: int, denominator: int) -> str | None:
    """An exact finite decimal spelling, or None when the fraction does not terminate."""
    common = _gcd(numerator, denominator)
    numerator //= common
    denominator //= common
    if denominator == 1:
        return str(numerator)
    twos = fives = 0
    remainder = denominator
    while remainder % 2 == 0:
        twos += 1
        remainder //= 2
    while remainder % 5 == 0:
        fives += 1
        remainder //= 5
    if remainder != 1:
        return None
    places = max(twos, fives)
    scaled = numerator * 2 ** (places - twos) * 5 ** (places - fives)
    digits = str(scaled)
    if len(digits) <= places:
        digits = "0" * (places - len(digits) + 1) + digits
    whole, fractional = digits[:-places], digits[-places:].rstrip("0")
    return whole if fractional == "" else f"{whole}.{fractional}"


def _capacity_override(
    text: str, match: re.Match[str]
) -> tuple[tuple[int, int, str] | None, AmountLinkReason | None]:
    """Evaluate an exact glass capacity, returning a parse substitution if needed.

    A terminating value is substituted as its exact decimal. A non-terminating value is
    checked exactly against the 50--1000 mL fence and the whole declaration is left out
    of the core's parse text; the span always keeps the untouched original text.
    """
    size = text[match.start("size") : match.end("size")]
    if "/" not in size and not any(
        character in _VULGAR_VALUES for character in size
    ):
        return None, None
    fraction = _capacity_fraction(size)
    if fraction is None:
        return None, _MALFORMED
    numerator, denominator = fraction
    if not _within_glass_fence(numerator, denominator):
        return None, _UNSUPPORTED
    decimal = _exact_decimal(numerator, denominator)
    if decimal is None:
        return (match.start(), match.end(), ""), None
    return (match.start("size"), match.end("size"), decimal), None


def _read_suffix(
    text: str,
    end: int,
    unit: Unit,
    sentence_end: int,
    *,
    probe_dependent: bool = True,
) -> tuple[int, AmountLinkReason | None, tuple[tuple[int, int, str], ...]]:
    """Read every capacity declaration and qualifier after one part.

    The result is the new end, the first rejection reason (if any) and the parse
    substitutions accumulated in source order: the exact decimal of a terminating
    fractional or vulgar capacity, or the removal of a non-terminating declaration that
    was validated exactly here. A malformed capacity consumes the malformed text so no
    smaller amount can be read out of it.

    The declaration loop is what makes a second declaration safe. The approved grammar
    has at most one capacity per part: a well-formed second declaration is an
    unsupported conflicting construct and an unbalanced or unit-less second declaration
    is malformed. Either way the whole expression is rejected and every declaration is
    consumed, so a later capacity number can never be rescanned as its own amount.

    ``probe_dependent`` is true for every ordinary read. It is false only for the single
    step of the iterative dependent-spoon chain, where the caller owns the following
    number for the same spoon unit.
    """
    substitutions: list[tuple[int, int, str]] = []
    declarations = 0
    while True:
        paren_position = _skip_ws(text, end)
        if paren_position < sentence_end and text[paren_position] == "(":
            capacity = _match_until(
                _CAPACITY_PAREN_RE, text, end, sentence_end
            )
            if capacity is None:
                return (
                    _consume_to_paren(text, paren_position, sentence_end),
                    _MALFORMED,
                    (),
                )
            declarations += 1
            if declarations > 1:
                return capacity.end(), _UNSUPPORTED, ()
            if unit is not Unit.GLASS:
                return capacity.end(), _UNSUPPORTED, ()
            override, reason = _capacity_override(text, capacity)
            if reason is not None:
                return capacity.end(), reason, ()
            if override is not None:
                substitutions.append(override)
            end = capacity.end()
            continue
        if unit is Unit.GLASS:
            capacity = _match_until(
                _CAPACITY_BARE_RE, text, end, sentence_end
            )
            if capacity is not None:
                declarations += 1
                if declarations > 1:
                    return capacity.end(), _UNSUPPORTED, ()
                override, reason = _capacity_override(text, capacity)
                if reason is not None:
                    return capacity.end(), reason, ()
                if override is not None:
                    substitutions.append(override)
                end = capacity.end()
                continue
            na_prefix = _match_until(
                _CAPACITY_NA_PREFIX_RE, text, end, sentence_end
            )
            if na_prefix is not None:
                declared = _match_until(
                    _HEAD_PART_RE, text, na_prefix.end(), sentence_end
                )
                if declared is not None:
                    # "1 стакан на 200 л": a capacity with a non-millilitre unit is
                    # malformed and is consumed whole, never salvaged as "200 л".
                    return declared.end(), _MALFORMED, ()
            number_position = _skip_ws(text, end)
            if number_position < sentence_end and _starts_number(
                text, number_position
            ):
                # A bare number after a household unit is a malformed capacity
                # declaration: the whole expression is rejected, not just the number.
                return (
                    _consume_number_and_unit(
                        text, number_position, sentence_end
                    ),
                    _MALFORMED,
                    (),
                )
        elif unit in _SPOON_UNITS and probe_dependent:
            number_position = _skip_ws(text, end)
            if _starts_number(text, number_position):
                # A spoon followed by a number is already outside the approved grammar
                # and is rejected whole. The dependent term is read against the whole
                # remaining text, because the tokenizer can mistake an abbreviation's
                # period for a sentence end when a digit follows (`1 ст. л. 2 ч. л.`).
                # The dependent chain is walked iteratively: each probe reads one part
                # without probing its own dependent, so a long `1 ч. л. 1 ч. л. ...` run
                # costs one iteration per term instead of one Python frame per term. The
                # visible outcome is unchanged: one rejected span, the reason of the
                # deepest part, and `unsupported` when every part is well-formed.
                while True:
                    following = _match_probe_part(text, number_position)
                    if following is None:
                        return (
                            _consume_number(text, number_position),
                            _MALFORMED,
                            (),
                        )
                    if following.reason is not None:
                        return following.end, following.reason, ()
                    next_position = _skip_ws(text, following.end)
                    if not _starts_number(text, next_position):
                        return following.end, _UNSUPPORTED, ()
                    number_position = next_position
        break

    qualifiers: list[re.Match[str]] = []
    while True:
        qualifier = _match_until(_QUALIFIER_RE, text, end, sentence_end)
        if qualifier is None:
            break
        qualifiers.append(qualifier)
        end = qualifier.end()
    if len(qualifiers) > 1:
        return end, _UNSUPPORTED, ()
    if qualifiers:
        word = _topic(qualifiers[0].group("qualifier"))
        if word in {"с горкой", "с верхом"}:
            return end, _HEAPED, ()
        if unit not in _SPOON_UNITS:
            return end, _UNSUPPORTED, ()
    return end, None, tuple(substitutions)


def _validation_text(
    text: str,
    start: int,
    end: int,
    substitutions: Sequence[tuple[int, int, str]],
) -> str:
    """Build the core parser's input in one forward pass over the expression.

    Substitutions arrive in source order from the left-to-right expression consumer, so
    each one is appended once and no string is rebuilt per capacity. The original span
    and the stored text are never modified.
    """
    if not substitutions:
        return text[start:end]
    pieces: list[str] = []
    cursor = start
    for replacement_start, replacement_end, replacement in substitutions:
        if replacement_start < cursor or replacement_end > end:
            continue
        pieces.append(text[cursor:replacement_start])
        pieces.append(replacement)
        cursor = replacement_end
    pieces.append(text[cursor:end])
    return "".join(pieces)


def _read_part(
    text: str, position: int, sentence_end: int, probe_dependent: bool
) -> _Part | None:
    head = _HEAD_PART_RE.match(text, position)
    if head is None:
        return None
    # A unit may end exactly at the tokenizer's sentence bound: an abbreviation period
    # followed by a digit is a boundary to the tokenizer but can still be a dependent
    # spoon term (`1 ст. л. 200 мл`). The suffix helpers bound the parts they read, so
    # the head itself is not cut here.
    unit = _UNIT_KEYS.get(_unit_key(head.group("unit")))
    if unit is None:
        return None
    end, reason, substitutions = _read_suffix(
        text, head.end(), unit, sentence_end, probe_dependent=probe_dependent
    )
    return _Part(
        unit=unit, end=end, reason=reason, substitutions=substitutions
    )


def _match_basic_part(text: str, position: int, sentence_end: int) -> _Part | None:
    return _read_part(text, position, sentence_end, True)


def _match_probe_part(text: str, position: int) -> _Part | None:
    """One step of the iterative dependent-spoon chain in `_read_suffix`.

    The same head, unit and suffix grammar as `_match_basic_part`, except that a spoon
    part's own following number is left to the caller's loop. That is what keeps a long
    `1 ч. л. 1 ч. л. ...` run from adding one Python stack frame per term.
    """
    return _read_part(text, position, len(text), False)


def _is_explicit_separator(match: re.Match[str]) -> bool:
    """Whether a matched measure separator is an arithmetic sign (`+`, `плюс`)."""
    return match.group("separator").casefold() in {"+", "плюс"}


def _separator_run(text: str, position: int) -> _SeparatorRun | None:
    """Consume the whole run of measure separators starting at ``position``.

    A run of one is the ordinary grammar. A run of more than one is actionable: any
    explicit arithmetic sign in it makes the expression malformed, while a run of only
    the natural-language ``и`` is not a separator at all and ends the expression before
    it. ``span_end`` is trimmed so a dangling sign never drags trailing blanks into a
    reported span.
    """
    cursor = position
    count = 0
    explicit = 0
    span_end = position
    while True:
        separator = _SEPARATOR_RE.match(text, cursor)
        if separator is None:
            break
        count += 1
        if _is_explicit_separator(separator):
            explicit += 1
        cursor = separator.end()
        trimmed = cursor
        while trimmed > position and text[trimmed - 1] in _BLANK_CHARS:
            trimmed -= 1
        span_end = trimmed
    if count == 0:
        return None
    return _SeparatorRun(
        end=cursor, span_end=span_end, count=count, explicit=explicit
    )


def _match_rejection_term(
    text: str, position: int, sentence_end: int
) -> tuple[int, AmountLinkReason] | None:
    """A term that can only continue an expression as a rejection.

    It is a range with a unit, a US cup, a package or a countable form, or a malformed
    numeric cluster followed by a unit. A bare number is not a term of the approved
    grammar, so it never extends a supported expression here.
    """
    ranged = _match_range(text, position, sentence_end)
    if ranged is not None:
        ranged_end, ranged_reason = ranged
        if ranged_reason is not None:
            return ranged_end, ranged_reason
        return None
    rejected = _match_rejected(text, position)
    if rejected is not None:
        return rejected
    cluster_end, _ = _consume_malformed_number_cluster(text, position)
    if cluster_end > position:
        unit_match = _match_until(
            _UNIT_RE, text, _skip_ws(text, cluster_end), sentence_end
        )
        if unit_match is not None:
            return unit_match.end(), _MALFORMED
    return None


def _match_part(
    text: str, position: int, sentence_end: int, *, nested: bool = False
) -> _Expression | None:
    """Consume one supported-or-rejected part expression, atomically.

    The consumer is a single left-to-right pass. Every explicit continuation is decided
    as a unit: when ``+``/``плюс`` (or a run of them) has no valid following term, the
    whole expression is malformed and the sign is retained in the reported span. A
    supported prefix is never returned as ``linked`` in that case, and no interior
    quantity is salvaged for a later scan.

    ``nested`` is used only by `_consume_single_term` inside `_consume_trailing_chain`:
    the outer chain already owns the trailing continuation loop, so a nested part returns
    its own end and the chain continues iteratively. This keeps a long
    `+`/rejected-term chain from consuming one Python stack frame per term.
    """
    first = _match_basic_part(text, position, sentence_end)
    if first is None:
        return None
    if first.reason is not None:
        end = (
            first.end
            if nested
            else _consume_trailing_chain(text, first.end, sentence_end)
        )
        return _Expression(position, end, False, first.reason)

    end = first.end
    unit = first.unit
    substitutions = list(first.substitutions)
    while True:
        if end >= sentence_end:
            break
        run = _separator_run(text, end)
        if run is not None:
            if run.count > 1:
                if run.explicit:
                    # A repeated explicit separator run is malformed even when a valid
                    # term follows: the grammar permits exactly one separator between
                    # parts, and the whole expression is rejected without salvage.
                    malformed_end = (
                        run.span_end
                        if nested
                        else _consume_trailing_chain(
                            text, run.span_end, sentence_end
                        )
                    )
                    return _Expression(position, malformed_end, False, _MALFORMED)
                # A run of only the natural-language `и` is not a measure separator.
                break
            next_position = _skip_ws(text, run.end)
            following = _match_basic_part(text, next_position, sentence_end)
            if following is not None:
                if following.reason is not None:
                    end = (
                        following.end
                        if nested
                        else _consume_trailing_chain(
                            text, following.end, sentence_end
                        )
                    )
                    return _Expression(position, end, False, following.reason)
                end = following.end
                unit = following.unit
                substitutions.extend(following.substitutions)
                continue
            # An explicit arithmetic sign joins a following rejected term into the same
            # maximal expression: the whole expression is rejected, never split so the
            # unsupported tail could be scanned again as an independent amount. A name
            # after the sign (`500 мл воды и молока`) still ends the expression, because
            # the natural-language `и` is a measure separator only before a supported
            # number+unit term.
            if run.explicit:
                rejection = _match_rejection_term(
                    text, next_position, sentence_end
                )
                if rejection is not None:
                    rejection_end, reason = rejection
                    end = (
                        rejection_end
                        if nested
                        else _consume_trailing_chain(
                            text, rejection_end, sentence_end
                        )
                    )
                    return _Expression(position, end, False, reason)
                # The explicit continuation is incomplete: `100 мл +`, `100 мл плюс`,
                # `100 мл ++` or `100 мл + 200`. The whole expression is malformed, the
                # abandoned sign run is part of its span, and nothing inside it may be
                # re-scanned as a fresh amount.
                malformed_end = (
                    run.span_end
                    if nested
                    else _consume_trailing_chain(
                        text, run.span_end, sentence_end
                    )
                )
                return _Expression(position, malformed_end, False, _MALFORMED)
            break

        next_position = _skip_ws(text, end)
        if not _starts_number(text, next_position):
            break
        following = _match_basic_part(text, next_position, sentence_end)
        if following is None:
            break
        if following.reason is not None:
            end = (
                following.end
                if nested
                else _consume_trailing_chain(
                    text, following.end, sentence_end
                )
            )
            return _Expression(position, end, False, following.reason)
        if unit in _METRIC_UNITS and following.unit in _METRIC_UNITS:
            end = following.end
            unit = following.unit
            substitutions.extend(following.substitutions)
            continue
        # A household part followed by another part without an explicit separator is
        # ambiguous juxtaposition; it is rejected whole, never split.
        end = (
            following.end
            if nested
            else _consume_trailing_chain(text, following.end, sentence_end)
        )
        return _Expression(position, end, False, _UNSUPPORTED)

    parse_text = _validation_text(text, position, end, substitutions)
    quantity = parse_quantity(parse_text, allow_nonstandard_glass_sizes=True)
    if quantity.is_quantified:
        return _Expression(position, end, True, None)
    reason = _PARSE_REASONS.get(quantity.reason, _UNSUPPORTED)
    if not nested:
        end = _consume_trailing_chain(text, end, sentence_end)
    return _Expression(position, end, False, reason)


_PARSE_REASONS = {
    QuantityReason.HEAPED: AmountLinkReason.HEAPED,
    QuantityReason.RANGE: AmountLinkReason.RANGE,
    QuantityReason.US_CUP: AmountLinkReason.US_CUP,
    QuantityReason.PACKAGE: AmountLinkReason.PACKAGE,
    QuantityReason.COUNTABLE: AmountLinkReason.COUNTABLE,
    QuantityReason.INVALID_GLASS_SIZE: AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    QuantityReason.MIXED_DIMENSIONS: AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    QuantityReason.AMBIGUOUS_JUXTAPOSITION: AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    QuantityReason.QUALIFIER_UNSUPPORTED: AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    QuantityReason.UNKNOWN_UNIT: AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    QuantityReason.NEGATIVE: AmountLinkReason.UNSUPPORTED_CONSTRUCT,
    QuantityReason.INVALID_NUMBER: AmountLinkReason.MALFORMED_EXPRESSION,
    QuantityReason.NO_QUANTITY: AmountLinkReason.MALFORMED_EXPRESSION,
    QuantityReason.QUALITATIVE: AmountLinkReason.UNSUPPORTED_CONSTRUCT,
}


def _match_range(
    text: str, position: int, sentence_end: int
) -> tuple[int, AmountLinkReason | None] | None:
    """Match a range form.

    The result is the consumed end and the rejection reason, or ``None`` when ``position``
    does not start a range. A range without a supported unit is consumed but yields no
    span (its end, ``None``); every number inside it is consumed whole.
    """
    if position >= sentence_end:
        return None
    match = _RANGE_RE.match(text, position)
    if match is None or match.end() > sentence_end:
        return None
    end = match.end()
    unit_position = _skip_ws(text, end)
    if unit_position >= sentence_end:
        return end, None
    unit_match = _match_until(_UNIT_RE, text, unit_position, sentence_end)
    if unit_match is None:
        return end, None
    unit = _UNIT_KEYS.get(_unit_key(unit_match.group("unit")))
    if unit is None:
        return end, None
    suffix_end, reason, _ = _read_suffix(
        text, unit_match.end(), unit, sentence_end
    )
    if reason is _MALFORMED:
        return suffix_end, _MALFORMED
    return suffix_end, AmountLinkReason.RANGE


def _consume_single_term(text: str, position: int, sentence_end: int) -> int:
    """Consume one term at ``position`` without its own trailing `+` chain.

    The term is the same maximal unit the main scan would recognise: a range, a part, a
    rejected form, a malformed numeric cluster with its unit, or a bare number. This is
    the single continuation rule shared by supported and rejected branches.
    """
    ranged = _match_range(text, position, sentence_end)
    if ranged is not None:
        return ranged[0]
    part = _match_part(text, position, sentence_end, nested=True)
    if part is not None:
        return part.end
    rejected = _match_rejected(text, position)
    if rejected is not None:
        return rejected[0]
    cluster_end, _ = _consume_malformed_number_cluster(text, position)
    if cluster_end > position:
        unit_match = _match_until(
            _UNIT_RE, text, _skip_ws(text, cluster_end), sentence_end
        )
        if unit_match is not None:
            return unit_match.end()
        return cluster_end
    return _consume_number(text, position)


def _consume_trailing_chain(text: str, end: int, sentence_end: int) -> int:
    """Consume everything that still belongs to one maximal expression after ``end``.

    This is the no-salvage guard for a rejected branch: a capacity parenthesis, an
    explicit `+`/`плюс` separator and its term, or a directly following term are consumed,
    so none can be rescanned as a separate amount. A separator followed by a name rather
    than a number (``500 мл воды и молока``) is not part of the amount expression and is
    left for the binder. A dangling explicit sign still belongs to the rejected
    expression and is consumed through its whole run, so it can never be separated from
    the expression it abandoned. The loop is iterative, so a long `+` chain cannot deepen
    the interpreter stack.
    """
    while True:
        if end >= sentence_end:
            return end
        position = _skip_ws(text, end)
        if position < sentence_end and text[position] == "(":
            capacity_end = _consume_to_paren(text, position, sentence_end)
            if capacity_end <= end:
                return end
            end = capacity_end
            continue
        separator = _PLUS_SEPARATOR_RE.match(text, end)
        if separator is not None:
            next_position = _skip_ws(text, separator.end())
            if next_position < sentence_end and _starts_number(
                text, next_position
            ):
                term_end = _consume_single_term(
                    text, next_position, sentence_end
                )
                if term_end > next_position:
                    end = term_end
                    continue
            run = _separator_run(text, end)
            if run is not None and run.explicit and run.span_end > end:
                end = run.span_end
                continue
            return end
        # The natural-language `и` continues a rejected expression only when the next
        # token starts a supported number+unit term, exactly as it does for a supported
        # part. `1 cup и 1 cup` stays two rejected spans; `1 cup и 100 мл` is one
        # rejected compound.
        natural = _SEPARATOR_RE.match(text, end)
        if natural is not None and not _is_explicit_separator(natural):
            following = _match_basic_part(
                text, _skip_ws(text, natural.end()), sentence_end
            )
            if following is not None:
                end = following.end
                continue
        if position < sentence_end and _starts_number(text, position):
            term_end = _consume_single_term(text, position, sentence_end)
            if term_end > position:
                end = term_end
                continue
        return end


def _match_rejected(
    text: str, position: int
) -> tuple[int, AmountLinkReason] | None:
    """Match a US cup, package or countable rejected form, without its tail."""
    match = _REJECTED_RE.match(text, position)
    if match is None:
        return None
    if match.group("us_cup") is not None:
        reason = AmountLinkReason.US_CUP
    elif match.group("package") is not None:
        reason = AmountLinkReason.PACKAGE
    else:
        reason = AmountLinkReason.COUNTABLE
    return match.end(), reason


# Punctuation that can wrongly join two digit runs into one malformed token
# (``1//2``, ``1:2``, ``1..2``, ``1,,2``, ``1 : 2``). A legal decimal, fraction, mixed
# number or range never reaches this helper because the expression branches consume it
# first. An invisible join across a sentence boundary - a ratio followed by an
# independent amount - is never consumed. The decimal comma and dot belong here because
# their repeated form is exactly the malformed token whose tail must not be salvaged.
_CLUSTER_SEPARATORS = ":/\\.,"


def _consume_malformed_number_cluster(
    text: str, position: int
) -> tuple[int, bool]:
    """Consume a digit cluster joined by ratio/fraction punctuation.

    Returns the consumed end and whether the punctuation was directly adjacent to both
    numeric runs. ``1//2`` and ``1:2`` are one malformed token and become one rejected
    span; a spaced ``1 : 2`` is the malformed spaced-number form that the approved
    behaviour consumes without a span. Either way the cluster is consumed once and its
    interior can never be rescanned as a fresh number.

    A completed adjacent malformed token never consumes across a following sentence
    period: ``1:2. 100 мл`` keeps the ratio as its own (no-span) token and leaves the
    independent amount in the next sentence. A spaced cluster is still consumed whole,
    as the approved conservative rule requires, so its internals never become spans.
    """
    length = len(text)
    if position >= length or not (
        text[position].isdigit() or text[position] in _VULGAR_CHARS
    ):
        # A cluster always begins with a digit or a vulgar fraction. Starting at
        # punctuation would let a dangling sign or a sentence period swallow an
        # unrelated following number.
        return position, False
    end = position
    while end < length and (
        text[end].isdigit() or text[end] in _VULGAR_CHARS
    ):
        end += 1
    joined = False
    adjacent = True
    while True:
        probe = _skip_ws(text, end)
        if probe >= length or text[probe] not in _CLUSTER_SEPARATORS:
            break
        led_by_ws = probe != end
        run_has_period = False
        # A run such as `..`, `//`, `. .` or `. : .` is one connector: blanks between
        # its punctuation characters keep the whole token malformed but make it spaced.
        while probe < length:
            if text[probe] in _CLUSTER_SEPARATORS:
                if text[probe] == ".":
                    run_has_period = True
                probe += 1
                continue
            next_probe = _skip_ws(text, probe)
            if (
                next_probe > probe
                and next_probe < length
                and text[next_probe] in _CLUSTER_SEPARATORS
            ):
                adjacent = False
                probe = next_probe
                continue
            break
        after = _skip_ws(text, probe)
        if after >= length or not (
            text[after].isdigit() or text[after] in _VULGAR_CHARS
        ):
            break
        if run_has_period and (led_by_ws or after != probe):
            # A period with whitespace on either side is sentence punctuation, not a
            # numeric connector: `1. 100 мл`, `1 . . 2`, `1:2. 100`. Stop before it; the
            # next sentence's amount is independent and must not be swallowed. Adjacent
            # periods (`1..2`, `1.2.3`) remain one malformed numeric token.
            break
        if led_by_ws or after != probe:
            adjacent = False
        end = after
        while end < length and (
            text[end].isdigit() or text[end] in _VULGAR_CHARS
        ):
            end += 1
        joined = True
    return (end, adjacent) if joined else (position, False)


def _scan_expressions(
    text: str, bounds: Sequence[int]
) -> tuple[_Expression, ...]:
    expressions: list[_Expression] = []
    position = 0
    length = len(text)
    sentence_index = 0
    while position < length:
        # The scan is a single forward pass, so the sentence index is advanced, never
        # re-searched with a bisect. The sentence start is also the lower bound of the
        # number-start guard, so a sentence period cannot make the next sentence's
        # amount look like the tail of the previous one.
        while (
            sentence_index + 1 < len(bounds)
            and bounds[sentence_index + 1] <= position
        ):
            sentence_index += 1
        sentence_start = bounds[sentence_index] if bounds else 0
        sentence_end = (
            bounds[sentence_index + 1]
            if sentence_index + 1 < len(bounds)
            else length
        )
        if not _starts_number(text, position, sentence_start):
            position += 1
            continue
        ranged = _match_range(text, position, sentence_end)
        if ranged is not None:
            range_end, range_reason = ranged
            if range_reason is None:
                position = max(range_end, position + 1)
                continue
            end = _consume_trailing_chain(text, range_end, sentence_end)
            expressions.append(_Expression(position, end, False, range_reason))
            position = max(end, position + 1)
            continue
        expression = _match_part(text, position, sentence_end)
        if expression is not None:
            expressions.append(expression)
            position = max(expression.end, position + 1)
            continue
        rejected = _match_rejected(text, position)
        if rejected is not None:
            rejected_end, reason = rejected
            end = _consume_trailing_chain(text, rejected_end, sentence_end)
            expressions.append(_Expression(position, end, False, reason))
            position = max(end, position + 1)
            continue
        cluster_end, adjacent = _consume_malformed_number_cluster(
            text, position
        )
        if cluster_end > position:
            if not adjacent:
                # The approved malformed spaced-number form is consumed without a span,
                # exactly like `1 / 2`; only its whole cluster is removed from scanning.
                position = max(cluster_end, position + 1)
                continue
            unit_match = _UNIT_RE.match(text, _skip_ws(text, cluster_end))
            if unit_match is not None:
                end = _consume_trailing_chain(
                    text, unit_match.end(), sentence_end
                )
                expressions.append(_Expression(position, end, False, _MALFORMED))
                position = max(end, position + 1)
            else:
                position = max(cluster_end, position + 1)
            continue
        position += 1
    return tuple(expressions)


# --- explicit local binding -----------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    positions: tuple[int, ...]
    reason: AmountLinkReason


@dataclass(frozen=True)
class _Binding:
    status: AmountLinkStatus
    reason: AmountLinkReason
    position: int | None


_SHARED = object()
_MULTIPLE = object()
_INCONCLUSIVE = object()
_COORDINATED = object()
_After = _Candidate | object | None


@dataclass(frozen=True)
class _StepContext:
    """Indexed word, phrase and sentence context, built once per step.

    Word tokens are contiguous and in text order, so the previous and next word in a
    sentence are ``index - 1`` and ``index + 1``; no helper rescans the whole word list
    for each amount. ``sentence_mentions`` is the per-sentence mention index and
    ``phrase_starts`` is the precomputed first token of each modifier run, so neither a
    phrase boundary nor a sentence boundary is ever rediscovered by walking a prefix of
    the step for every expression.
    """

    text: str
    bounds: tuple[int, ...]
    words: tuple[_Word, ...]
    starts: tuple[int, ...]
    ends: tuple[int, ...]
    mentions: Mapping[int, _Mention]
    sentence_mentions: Mapping[int, tuple[int, ...]]
    sentence_verbs: Mapping[int, tuple[int, ...]]
    adjectives: tuple[bool, ...]
    modifying: tuple[bool, ...]
    phrase_modifying: tuple[bool, ...]
    phrase_starts: tuple[int, ...]


def _word_before(ctx: _StepContext, position: int) -> int:
    """Index of the last word whose end is at or before ``position``, or -1."""
    return bisect_right(ctx.ends, position) - 1


def _word_from(ctx: _StepContext, position: int) -> int:
    """Index of the first word whose start is at or after ``position``."""
    return bisect_left(ctx.starts, position)


def _in_sentence(ctx: _StepContext, index: int, sentence: int) -> bool:
    return 0 <= index < len(ctx.words) and ctx.words[index].sentence == sentence


def _nearby_words(
    ctx: _StepContext, position: int, sentence: int, limit: int
) -> list[_Word]:
    """Words immediately before ``position`` with only whitespace between them."""
    result: list[_Word] = []
    cursor = position
    index = _word_before(ctx, position)
    while (
        index >= 0
        and len(result) < limit
        and ctx.words[index].sentence == sentence
    ):
        word = ctx.words[index]
        if ctx.text[word.end : cursor].strip() != "":
            break
        result.append(word)
        cursor = word.start
        index -= 1
    return result


def _previous_contiguous(
    ctx: _StepContext, index: int, sentence: int
) -> int:
    """The word index immediately before ``index`` joined by whitespace, or -1."""
    prior = index - 1
    if not _in_sentence(ctx, prior, sentence):
        return -1
    if not _whitespace_gap(
        ctx.text, ctx.words[prior].end, ctx.words[index].start
    ):
        return -1
    return prior


# The closed positive allowlist of approved lexical hyphenated units (KH-F01-B1.1-B).
# Russian prose writes the same ASCII hyphen both inside a word (`всё-таки`,
# `по-прежнему`, `добавьте-ка`) and as a clause dash (`500 мл воды - молоко
# перемешайте`). Only a complete word listed here is word-internal. The entries are
# stored in the matching form (`_norm`: lower case with one `е` spelling), so
# `всё-таки` and `все-таки` are the same unit. Two closed groups are listed: adverbs
# and the particle `-ка` attached to the approved imperative verbs. A compound outside
# the allowlist (`воды-молоко`, `агар-агар`, `лук-порей`) is never classified as
# word-internal, so its hyphen stays a conservative clause boundary. This is product
# data, versioned with the code: it is not a dictionary lookup, a fuzzy match or a
# morphological guess, and a new unit is added deliberately as a whole word.
_LEXICAL_HYPHEN_WORDS = frozenset({"все-таки", "по-прежнему"})
_KA_PARTICLE_VERBS = frozenset(
    {"добавьте", "налейте", "возьмите", "положите", "влейте"}
)
_LEXICAL_HYPHEN_UNITS = _LEXICAL_HYPHEN_WORDS | frozenset(
    f"{verb}-ка" for verb in _KA_PARTICLE_VERBS
)
_MAX_LEXICAL_HYPHEN_UNIT = max(len(unit) for unit in _LEXICAL_HYPHEN_UNITS)

_GOVERNING_SOFT_PUNCTUATION = frozenset({",", ":", "—", "–"})
# The punctuation that can end the amount's own clause: the module's soft clause
# punctuation plus the ASCII hyphen that prose uses for the same dash. It is never a
# sentence boundary and it is never a global rule: `_clause_end_after` searches only
# after the amount's own end, so the L2 separator before an amount (`Вода — 100 мл`), a
# range inside an expression and a hyphen inside a number stay untouched. A hyphen that
# belongs to a complete approved word is classified by `_is_lexical_hyphen` and is not
# a boundary; any other letter-joined hyphen still is.
_CLAUSE_BOUNDARY_CHARS = _GOVERNING_SOFT_PUNCTUATION | {"-"}
_CLAUSE_BOUNDARY_RE = re.compile(
    "["
    + "".join(re.escape(character) for character in sorted(_CLAUSE_BOUNDARY_CHARS))
    + "]"
)


def _lexical_norm(text: str) -> str:
    """The matching form of one hyphenated unit: case first, then one ``е`` spelling.

    `_norm` replaces ``ё`` before lower-casing, so an uppercase ``Ё`` survives into the
    matching form (``ВСЁ-ТАКИ`` became ``всё-таки`` and was not recognised). The
    lexical-hyphen path needs every case spelling of a unit to be the same stored form,
    so it lower-cases first and replaces afterwards. This is deliberately not a change
    to the global `_norm`, whose behaviour the rest of the scanner already depends on.
    """
    return text.lower().replace(_YO_LETTER, "е")


def _lexical_run(text: str, left: int, right: int) -> tuple[int, int] | None:
    """The maximal run of letters and ASCII hyphens containing ``text[left:right]``.

    ``left`` and ``right`` bound a span already known to sit inside the run (one word or
    one hyphen). The scan is bounded by the longest approved unit. ``None`` means the run
    is longer than that, so it can never be a complete unit: a fragment of a longer run
    is never positive evidence, and a bounded recognition never mistakes one for a word.

    This is the single source of lexical-unit boundaries. The clause-boundary search and
    the predicate-token check both read a unit through it, so they cannot disagree about
    where a hyphenated unit starts or ends.
    """
    length = len(text)
    start = left
    while (
        start > 0
        and left - start < _MAX_LEXICAL_HYPHEN_UNIT
        and (text[start - 1].isalpha() or text[start - 1] == "-")
    ):
        start -= 1
    if start > 0 and (text[start - 1].isalpha() or text[start - 1] == "-"):
        return None
    end = right
    while (
        end < length
        and end - right < _MAX_LEXICAL_HYPHEN_UNIT
        and (text[end].isalpha() or text[end] == "-")
    ):
        end += 1
    if end < length and (text[end].isalpha() or text[end] == "-"):
        return None
    if end - start > _MAX_LEXICAL_HYPHEN_UNIT:
        return None
    return start, end


def _is_lexical_hyphen(text: str, position: int) -> bool:
    """Whether the ASCII hyphen at ``position`` belongs to one approved word.

    Russian prose writes the same ASCII hyphen both inside a word (``всё-таки``,
    ``по-прежнему``, ``добавьте-ка``) and for a clause dash (``500 мл воды - молоко
    перемешайте``). Only the first is a lexical hyphen, and only for a complete unit in
    the closed allowlist: the maximal run of letters and hyphens around ``position`` is
    read whole by `_lexical_run` and must be one listed word. A fragment is never
    evidence, so ``воды-молоко`` and ``вода-молоко-сахар`` are not word-internal and an
    unknown or unverified letter-joined hyphen stays a conservative clause boundary.

    Everything else keeps its existing meaning. A numeric hyphen (``3-4``) is adjacent
    to digits, not letters, and a hyphen with a blank on either side is clause
    punctuation; both still end the clause. The run scan is bounded by the longest
    listed unit, so the classification stays constant-time per hyphen and the clause
    pass stays bounded by the number of boundary characters, not by a rescan of the
    text. The matching form is `_lexical_norm`, so every case spelling of an approved
    unit -- including ``ВСЁ-ТАКИ`` -- is recognised as the same unit.
    """
    if not (
        0 < position < len(text) - 1
        and text[position - 1].isalpha()
        and text[position + 1].isalpha()
    ):
        return False
    span = _lexical_run(text, position, position + 1)
    if span is None:
        return False
    return _lexical_norm(text[span[0] : span[1]]) in _LEXICAL_HYPHEN_UNITS


def _is_predicate_word(text: str, word: _Word) -> bool:
    """Whether a finite verb is a complete lexical unit, not a fragment.

    A finite verb is local predicate evidence only when its complete lexical unit is
    valid: a standalone word, or one complete approved hyphenated unit
    (``добавьте-ка``). A verb that is a fragment of a longer run -- inside an unknown
    compound (``добавьте-ка-молоко``, ``молоко-добавьте``) or inside an approved unit
    extended by an unknown part (``всё-таки-добавьте``) -- is not evidence. Both sides of
    the candidate are checked, because the extension may sit before or after it.

    The check is the same bounded lexical-unit recognition the clause-boundary search
    uses (`_lexical_run` and the closed allowlist), so eligibility and clause boundaries
    cannot disagree about the unit a verb belongs to.
    """
    if word.norm not in _FINITE_VERBS:
        return False
    left = word.start - 1
    right = word.end
    joined_left = left >= 0 and (text[left].isalpha() or text[left] == "-")
    joined_right = right < len(text) and (
        text[right].isalpha() or text[right] == "-"
    )
    if not joined_left and not joined_right:
        # A standalone word is a complete unit by itself, whatever its length.
        return True
    span = _lexical_run(text, word.start, word.end)
    if span is None:
        return False
    return _lexical_norm(text[span[0] : span[1]]) in _LEXICAL_HYPHEN_UNITS


def _governing_gap_kind(text: str, left_end: int, right_start: int) -> str:
    """Classify the punctuation a governing chain would cross.

    ``contiguous`` is blanks only; ``soft`` is a colon or dash; ``comma`` is a comma;
    ``hard`` is any other character. A comma is the boundary an independent clause
    creates, so it is the only kind the chain may cross, and only behind a governing
    preposition.
    """
    gap = text[left_end:right_start]
    punctuation = "".join(
        character for character in gap if character not in _BLANK_CHARS
    )
    if punctuation == "":
        return "contiguous"
    if all(
        character in _GOVERNING_SOFT_PUNCTUATION
        for character in punctuation
    ):
        return "comma" if "," in punctuation else "soft"
    return "hard"


def _governed_by_anaphora(
    ctx: _StepContext, expression: _Expression, sentence: int
) -> bool:
    """Whether a portion/anaphora word governs the span, across supported modifiers.

    The scan walks the whole governing chain leftward, skipping only a closed set of
    linkers, particles, neutral fillers, approximation words, curated
    quantifiers/participles and positive lexical modifiers, until it finds an anaphora
    word (governed) or any other word (not governed). It is not a fixed-width window and
    stops at a sentence
    boundary or a finite verb, so
    ``половину от этих же указанных примерно 500 мл`` stays governed, and a curated
    filler such as ``точно`` behaves the same with or without a comma.

    A comma is not automatically a boundary and not automatically transparent. It is an
    independent-clause boundary (``Отложите половину, 500 мл воды``) unless the word
    immediately to its right is a governing preposition, which carries the portion
    relation across it (``Возьмите половину, от 500 мл воды``). Semicolons and periods
    are sentence boundaries before this scan ever runs.
    """
    index = _word_before(ctx, expression.start)
    cursor = expression.start
    bridge = False
    while index >= 0 and ctx.words[index].sentence == sentence:
        word = ctx.words[index]
        gap = _governing_gap_kind(ctx.text, word.end, cursor)
        if gap == "hard":
            return False
        if gap == "comma" and not bridge:
            return False
        norm = word.norm
        if norm in _ANAPHORA_WORDS:
            return True
        if norm in _FINITE_VERBS:
            return False
        if (
            norm in _APPROXIMATE_WORDS
            or norm in _ANAPHORA_LINKERS
            or norm in _GOVERNING_PARTICLES
            or norm in _GOVERNING_WORDS
            or norm in _PORTION_FILLER_WORDS
            or ctx.phrase_modifying[index]
        ):
            bridge = norm in _ANAPHORA_LINKERS
            cursor = word.start
            index -= 1
            continue
        return False
    return False


def _clause_end_after(
    ctx: _StepContext, expression: _Expression, sentence: int
) -> int:
    """The end of the amount's own clause.

    The clause ends at the next clause punctuation (a comma, a colon or a dash) or at
    the sentence end. The punctuation is contextual, not a sentence boundary and not a
    global rule: the search starts at the amount's own end, so the L2 separator before an
    amount (`Вода — 100 мл`), a range separator inside an expression and a hyphen inside
    a number are never crossed or reinterpreted. A semicolon or a period already split
    the sentence before this scan runs.

    An ASCII hyphen is classified by `_is_lexical_hyphen`: the hyphen of a complete
    approved word (`воды всё-таки добавьте`) is word-internal and is skipped, while any
    other hyphen -- a clause dash, a numeric hyphen or an unverified letter-joined
    compound (`воды-молоко`) -- ends the clause. The skip is a search over the
    punctuation positions only, so the pass stays bounded by the number of boundary
    characters, not by a rescan of the text.
    """
    limit = (
        ctx.bounds[sentence + 1]
        if sentence + 1 < len(ctx.bounds)
        else len(ctx.text)
    )
    position = expression.end
    while position < limit:
        boundary = _CLAUSE_BOUNDARY_RE.search(ctx.text, position, limit)
        if boundary is None:
            return limit
        found = boundary.start()
        if ctx.text[found] == "-" and _is_lexical_hyphen(ctx.text, found):
            position = found + 1
            continue
        return found
    return limit


def _clause_has_predicate(
    ctx: _StepContext, expression: _Expression, sentence: int
) -> bool:
    """Whether the amount's own clause carries a finite predicate after the amount.

    Only the amount's local clause is evidence. A finite verb in a later clause
    (``500 мл воды, молоко перемешайте``, ``500 мл воды — молоко перемешайте``) belongs
    to another ingredient, so it never proves that the amount's clause is independent.
    The amount's clause ends at the next comma, colon or dash at the latest; the first
    finite verb after the amount must start before that boundary.

    The index consulted here holds only eligible predicate tokens (`_is_predicate_word`):
    a verb that is a fragment of an unknown hyphenated unit is absent, so it can neither
    establish locality by its own position nor push the answer onto a later predicate
    beyond the boundary the same unit creates.
    """
    verbs = ctx.sentence_verbs.get(sentence, ())
    first = bisect_left(verbs, _word_from(ctx, expression.end))
    if first >= len(verbs):
        return False
    return ctx.words[verbs[first]].start < _clause_end_after(
        ctx, expression, sentence
    )


def _uncertain_portion_governance(
    ctx: _StepContext, expression: _Expression, sentence: int
) -> bool:
    """Whether a comma leaves a portion relation possible but not positive.

    ``_governed_by_anaphora`` only returns true with positive governance (a governing
    preposition or no comma at all). This helper finds the complementary case: an
    anaphora/portion word sits to the left of a comma, and the amount's own clause
    carries no finite predicate. ``Возьмите половину, 500 мл воды`` is such a case; the
    approved fallback is conservative ``unresolved``, never a link. When the amount's
    clause does carry a finite predicate, the comma is positively an independent clause
    boundary (``Отложите половину, 500 мл воды добавьте позже``) and this returns false.
    A predicate in a later clause about another ingredient is not local evidence.
    """
    index = _word_before(ctx, expression.start)
    cursor = expression.start
    crossed_comma = False
    while index >= 0 and ctx.words[index].sentence == sentence:
        word = ctx.words[index]
        gap = _governing_gap_kind(ctx.text, word.end, cursor)
        if gap == "hard":
            return False
        if gap == "comma":
            crossed_comma = True
        norm = word.norm
        if norm in _ANAPHORA_WORDS:
            if not crossed_comma:
                return False
            return not _clause_has_predicate(ctx, expression, sentence)
        if norm in _FINITE_VERBS:
            return False
        if (
            norm in _APPROXIMATE_WORDS
            or norm in _ANAPHORA_LINKERS
            or norm in _GOVERNING_PARTICLES
            or norm in _GOVERNING_WORDS
            or norm in _PORTION_FILLER_WORDS
            or ctx.phrase_modifying[index]
        ):
            cursor = word.start
            index -= 1
            continue
        return False
    return False


def _is_cookware(word: str) -> bool:
    return any(word.startswith(stem) for stem in _COOKWARE_STEMS)


def _preceded_by_capacity_context(
    ctx: _StepContext, expression: _Expression, sentence: int
) -> bool:
    """Whether the amount is a container capacity, not an ingredient quantity.

    The context is bounded and syntactic: the amount is immediately preceded by a
    capacity noun (``объемом``, ``вместимостью``), optionally through an approximation
    word, or by `cookware + на` (``кастрюлю на 3 л``). No broader window is used.
    """
    index = _word_before(ctx, expression.start)
    if not _in_sentence(ctx, index, sentence):
        return False
    if not _whitespace_gap(ctx.text, ctx.words[index].end, expression.start):
        return False
    while ctx.words[index].norm in _APPROXIMATE_WORDS:
        index = _previous_contiguous(ctx, index, sentence)
        if index < 0:
            return False
    norm = ctx.words[index].norm
    if norm in _CAPACITY_CONTEXT_WORDS:
        return True
    if norm == "на":
        prior = _previous_contiguous(ctx, index, sentence)
        return prior >= 0 and _is_cookware(ctx.words[prior].norm)
    return False


def _sentence_has_mention(
    ctx: _StepContext, expression: _Expression, sentence: int
) -> bool:
    for index in ctx.sentence_mentions.get(sentence, ()):
        word = ctx.words[index]
        if word.start >= expression.start and word.end <= expression.end:
            continue
        return True
    return False


def _phrase_starts(
    text: str,
    words: Sequence[_Word],
    modifying: Sequence[bool],
    mentions: Mapping[int, _Mention],
) -> tuple[int, ...]:
    """Index the first token of the modifier run ending at every word, once per step.

    The run is joined by whitespace or a comma (``холодного, свежего молока``), so a
    comma-separated modifier sequence stays one noun phrase. A mention or any other word
    ends the run; it never crosses a sentence boundary. Building the index once removes
    the repeated backwards walk from every coordinated-group check.
    """
    starts = [0] * len(words)
    for index in range(len(words)):
        left = index - 1
        if (
            left >= 0
            and words[left].sentence == words[index].sentence
            and modifying[left]
            and left not in mentions
            and _modifier_gap(text, words[left].end, words[index].start)
        ):
            starts[index] = starts[left]
        else:
            starts[index] = index
    return tuple(starts)


def _coordinated_group_ending_at(
    ctx: _StepContext, index: int, sentence: int
) -> list[int]:
    """The mentions in the coordinated group whose last head is ``index``.

    A group member is reached through a modifier run (``холодного, свежего молока``), a
    coordinator (``воды и молока``) or a comma (``воды, молока``). Repeated coordinators
    (``И вода, и молоко``) are supported through the optional comma before the
    coordinator. Anything else ends the group, so an unrelated mention is never merged.
    """
    group = [index] if index in ctx.mentions else []
    current = index
    while group:
        start = ctx.phrase_starts[current]
        prior = start - 1
        if prior < 0 or ctx.words[prior].sentence != sentence:
            break
        between = ctx.text[ctx.words[prior].end : ctx.words[start].start]
        if ctx.words[prior].norm in _COORDINATORS and _modifier_gap(
            ctx.text, ctx.words[prior].end, ctx.words[start].start
        ):
            name = prior - 1
            if (
                _in_sentence(ctx, name, sentence)
                and name in ctx.mentions
                and _modifier_gap(
                    ctx.text, ctx.words[name].end, ctx.words[prior].start
                )
            ):
                group.append(name)
                current = name
                continue
            break
        if (
            "," in between
            and _modifier_gap(
                ctx.text, ctx.words[prior].end, ctx.words[start].start
            )
            and prior in ctx.mentions
        ):
            group.append(prior)
            current = prior
            continue
        break
    return group


def _has_uncoordinated_prior_mention(
    ctx: _StepContext, index: int, sentence: int
) -> bool:
    prior = index - 1
    if not _in_sentence(ctx, prior, sentence):
        return False
    if prior not in ctx.mentions:
        return False
    between = ctx.text[ctx.words[prior].end : ctx.words[index].start]
    return "," not in between and ctx.words[prior].norm not in _COORDINATORS


def _mention_opens_clause(
    ctx: _StepContext, index: int, sentence: int
) -> bool:
    """Whether a comma-joined mention is the subject/object of a new finite clause.

    ``100 мл воды, молоко перемешайте`` is two independent clauses, not one shared
    ingredient group. Only a closed-set finite verb counts as positive evidence; an
    uncertain continuation never creates a shared link.
    """
    following = index + 1
    cursor = ctx.words[index].end
    while (
        _in_sentence(ctx, following, sentence)
        and _modifier_gap(
            ctx.text, cursor, ctx.words[following].start
        )
        and ctx.phrase_modifying[following]
        and ctx.words[following].norm not in _FINITE_VERBS
    ):
        cursor = ctx.words[following].end
        following += 1
    if not _in_sentence(ctx, following, sentence):
        return False
    return (
        _modifier_gap(ctx.text, cursor, ctx.words[following].start)
        and ctx.words[following].norm in _FINITE_VERBS
    )


_NON_NOUN_WORDS = _FUNCTION_WORDS | _COORDINATORS | _FINITE_VERBS | (
    _APPROXIMATE_WORDS
    | _GOVERNING_PARTICLES
    | _ANAPHORA_LINKERS
    | frozenset(
        {
            "на",
            "в",
            "во",
            "к",
            "ко",
            "у",
            "не",
            "ни",
            "как",
            "что",
            "чтобы",
            "при",
            "под",
            "над",
            "между",
            "через",
            "после",
            "перед",
            "за",
            "про",
        }
    )
)


def _could_start_noun_phrase(norm: str) -> bool:
    """Whether an unclassified follower may begin a coordinated noun phrase."""
    return norm not in _NON_NOUN_WORDS


def _coordinated_forward(
    ctx: _StepContext, head_index: int, sentence: int
) -> object | None:
    """Whether a second mention continues the noun phrase after ``head_index``.

    The continuation is a coordinator or a comma, then a modifier run (modifiers may be
    comma-separated) ending at another mention. A number panel, a finite verb or any
    other word ends it, so ``100 мл молока и 100 мл воды`` never shares the first amount.

    Three outcomes are distinguished:

    * ``_COORDINATED`` -- positive evidence of a shared noun group;
    * ``_INCONCLUSIVE`` -- a coordinator/comma opens a noun-like continuation that is
      not an ingredient mention; the amount may or may not be shared, so a link is not
      positive;
    * ``None`` -- no coordination evidence (including a comma that opens an independent
      finite clause), so the ordinary L1 result stands.
    """
    index = head_index + 1
    if not _in_sentence(ctx, index, sentence):
        return None
    previous_end = ctx.words[head_index].end
    gap = ctx.text[previous_end : ctx.words[index].start]
    if not _modifier_gap(ctx.text, previous_end, ctx.words[index].start):
        return None
    explicit = False
    if ctx.words[index].norm in _COORDINATORS:
        explicit = True
        previous_end = ctx.words[index].end
        index += 1
    elif "," in gap:
        previous_end = ctx.words[index].start
    else:
        return None
    while _in_sentence(ctx, index, sentence):
        word = ctx.words[index]
        gap = ctx.text[previous_end : word.start]
        if not _modifier_gap(ctx.text, previous_end, word.start):
            return None
        if index in ctx.mentions:
            if not explicit and _mention_opens_clause(ctx, index, sentence):
                return None
            return _COORDINATED
        if word.norm in _FINITE_VERBS:
            return None
        if ctx.phrase_modifying[index]:
            previous_end = word.end
            index += 1
            continue
        if word.norm in _COORDINATORS and "," in gap:
            explicit = True
            previous_end = word.end
            index += 1
            continue
        if _could_start_noun_phrase(word.norm):
            return _INCONCLUSIVE
        return None
    if explicit:
        return _INCONCLUSIVE
    return None


def _after_binding(
    ctx: _StepContext, expression: _Expression, sentence: int
) -> _After:
    cursor = expression.end
    gap = 0
    function_words = 0
    head_index: int | None = None
    index = _word_from(ctx, expression.end)
    if (
        _in_sentence(ctx, index, sentence)
        and _whitespace_gap(ctx.text, expression.end, ctx.words[index].start)
        and ctx.words[index].norm in _COORDINATORS
    ):
        # The expression consumer leaves a coordinator it did not consume (the next token
        # is not a supported amount term). A coordinator with no established group is
        # inconclusive, never a licence to link by proximity.
        return _INCONCLUSIVE
    while index < len(ctx.words) and ctx.words[index].sentence == sentence:
        word = ctx.words[index]
        if not _whitespace_gap(ctx.text, cursor, word.start):
            break
        if index in ctx.mentions:
            head_index = index
            break
        if word.norm in _FINITE_VERBS:
            return None
        if word.norm in _L1_BRIDGE_WORDS:
            function_words += 1
            if function_words > 1:
                return None
            gap += 1
        elif ctx.modifying[index]:
            gap += 1
        else:
            return None
        if gap > 3:
            return None
        cursor = word.end
        index += 1
    if head_index is None:
        return None

    coordination = _coordinated_forward(ctx, head_index, sentence)
    if coordination is _COORDINATED:
        return _SHARED
    if coordination is _INCONCLUSIVE:
        return _INCONCLUSIVE
    next_index = head_index + 1
    if (
        _in_sentence(ctx, next_index, sentence)
        and next_index in ctx.mentions
        and _whitespace_gap(
            ctx.text, ctx.words[head_index].end, ctx.words[next_index].start
        )
    ):
        return _MULTIPLE

    mention = ctx.mentions[head_index]
    return _Candidate(mention.positions, AmountLinkReason.NAME_AFTER)


def _separator_binding(
    ctx: _StepContext, expression: _Expression, sentence: int
) -> _After:
    index = _word_before(ctx, expression.start)
    if not _in_sentence(ctx, index, sentence):
        return None
    between = ctx.text[ctx.words[index].end : expression.start]
    if between.strip() not in _SEPARATORS:
        return None
    if index not in ctx.mentions:
        return None
    group = _coordinated_group_ending_at(ctx, index, sentence)
    if len(group) > 1:
        return _SHARED
    if _has_uncoordinated_prior_mention(ctx, index, sentence):
        return _MULTIPLE
    mention = ctx.mentions[index]
    return _Candidate(mention.positions, AmountLinkReason.NAME_BEFORE_SEPARATOR)


def _copula_binding(
    ctx: _StepContext, expression: _Expression, sentence: int
) -> _After:
    verb_index = _word_before(ctx, expression.start)
    if verb_index < 1 or ctx.words[verb_index].sentence != sentence:
        return None
    if ctx.words[verb_index].norm not in _COPULA_VERBS:
        return None
    if ctx.text[ctx.words[verb_index].end : expression.start].strip() != "":
        return None
    name_index = verb_index - 1
    if ctx.words[name_index].sentence != sentence:
        return None
    if ctx.text[ctx.words[name_index].end : ctx.words[verb_index].start].strip() != "":
        return None
    if name_index not in ctx.mentions:
        return None
    group = _coordinated_group_ending_at(ctx, name_index, sentence)
    if len(group) > 1:
        return _SHARED
    if _has_uncoordinated_prior_mention(ctx, name_index, sentence):
        return _MULTIPLE
    mention = ctx.mentions[name_index]
    return _Candidate(mention.positions, AmountLinkReason.NAME_BEFORE_COPULA)


def _bind(ctx: _StepContext, expression: _Expression) -> _Binding:
    sentence = _sentence_of(ctx.bounds, expression.start)
    if _preceded_by_capacity_context(ctx, expression, sentence):
        reason = (
            AmountLinkReason.NO_LOCAL_BINDING
            if _sentence_has_mention(ctx, expression, sentence)
            else AmountLinkReason.NO_MENTION
        )
        return _Binding(AmountLinkStatus.UNRESOLVED, reason, None)
    if _governed_by_anaphora(ctx, expression, sentence):
        return _Binding(AmountLinkStatus.UNRESOLVED, AmountLinkReason.ANAPHORA, None)
    if _uncertain_portion_governance(ctx, expression, sentence):
        # The portion relation is possible but not positively established, and no
        # independent predicate separates it from the amount: conservative unresolved.
        return _Binding(
            AmountLinkStatus.UNRESOLVED,
            AmountLinkReason.NO_LOCAL_BINDING,
            None,
        )

    after = _after_binding(ctx, expression, sentence)
    separator = _separator_binding(ctx, expression, sentence)
    copula = _copula_binding(ctx, expression, sentence)

    if after is _SHARED or separator is _SHARED or copula is _SHARED:
        return _Binding(
            AmountLinkStatus.SHARED, AmountLinkReason.SHARED_AMOUNT, None
        )
    if after is _MULTIPLE or separator is _MULTIPLE or copula is _MULTIPLE:
        return _Binding(
            AmountLinkStatus.AMBIGUOUS,
            AmountLinkReason.MULTIPLE_CANDIDATES,
            None,
        )
    if (
        after is _INCONCLUSIVE
        or separator is _INCONCLUSIVE
        or copula is _INCONCLUSIVE
    ):
        # A coordination the grammar cannot establish positively is never promoted to a
        # link or a shared group: the approved fallback is unresolved without a hint. A
        # sentence with no ingredient mention at all keeps the more precise `no_mention`
        # reason; `no_local_binding` means a possible target existed but was not bound.
        reason = (
            AmountLinkReason.NO_LOCAL_BINDING
            if _sentence_has_mention(ctx, expression, sentence)
            else AmountLinkReason.NO_MENTION
        )
        return _Binding(AmountLinkStatus.UNRESOLVED, reason, None)

    candidates = [
        candidate
        for candidate in (after, separator, copula)
        if isinstance(candidate, _Candidate)
    ]
    if not candidates:
        reason = (
            AmountLinkReason.NO_LOCAL_BINDING
            if _sentence_has_mention(ctx, expression, sentence)
            else AmountLinkReason.NO_MENTION
        )
        return _Binding(AmountLinkStatus.UNRESOLVED, reason, None)
    if any(len(candidate.positions) != 1 for candidate in candidates):
        return _Binding(
            AmountLinkStatus.AMBIGUOUS,
            AmountLinkReason.MULTIPLE_CANDIDATES,
            None,
        )
    positions = {candidate.positions[0] for candidate in candidates}
    if len(positions) != 1:
        return _Binding(
            AmountLinkStatus.AMBIGUOUS,
            AmountLinkReason.MULTIPLE_CANDIDATES,
            None,
        )
    return _Binding(
        AmountLinkStatus.LINKED, candidates[0].reason, positions.pop()
    )


# --- one step and one recipe ----------------------------------------------------------


def _scan_step(
    step_index: int, text: str, infos: Sequence[_IngredientInfo], limit: int
) -> tuple[tuple[StepAmount, ...], int]:
    """Scan one step, binding at most ``limit`` spans and counting every expression.

    Expressions are found in a single left-to-right pass. Only the kept prefix is bound,
    so a step or recipe that exceeds the span cap does no binding work for the spans it
    omits, while the omission count and the cap behaviour stay exact.
    """
    bounds, words = _tokenize(text)
    if limit <= 0:
        # The recipe budget is already spent: only the count is needed, so no word
        # context or ingredient matching is built for a step whose spans are all omitted.
        return (), len(_scan_expressions(text, bounds))
    expressions = _scan_expressions(text, bounds)
    if not expressions:
        # No candidate quantity exists, so no mention or binding context is built at all.
        return (), 0
    words_tuple = tuple(words)
    word_index = _build_word_index(text, words_tuple)
    mentions = _mentions(text, words_tuple, infos, word_index)
    starts = tuple(word.start for word in words_tuple)
    ends = tuple(word.end for word in words_tuple)
    sentence_mentions: dict[int, list[int]] = {}
    sentence_verbs: dict[int, list[int]] = {}
    for index, word in enumerate(words_tuple):
        # Only a verb whose complete lexical unit is valid is local predicate evidence:
        # a fragment inside an unknown hyphenated unit (`добавьте-ка-молоко`) is not.
        if _is_predicate_word(text, word):
            sentence_verbs.setdefault(word.sentence, []).append(index)
    for index in mentions:
        sentence_mentions.setdefault(
            words_tuple[index].sentence, []
        ).append(index)
    ctx = _StepContext(
        text=text,
        bounds=tuple(bounds),
        words=words_tuple,
        starts=starts,
        ends=ends,
        mentions=mentions,
        sentence_mentions={
            sentence: tuple(indices)
            for sentence, indices in sentence_mentions.items()
        },
        sentence_verbs={
            sentence: tuple(indices)
            for sentence, indices in sentence_verbs.items()
        },
        adjectives=word_index.adjectives,
        modifying=word_index.modifying,
        phrase_modifying=word_index.phrase_modifying,
        phrase_starts=_phrase_starts(
            text, words_tuple, word_index.phrase_modifying, mentions
        ),
    )
    amounts: list[StepAmount] = []
    for expression in expressions[: max(0, limit)]:
        sentence = _sentence_of(bounds, expression.start)
        nearby = _nearby_words(ctx, expression.start, sentence, 2)
        approximate = any(
            word.norm in _APPROXIMATE_WORDS for word in nearby
        )
        if not expression.supported:
            reason = expression.reason or _UNSUPPORTED
            amounts.append(
                StepAmount(
                    step_index=step_index,
                    start=expression.start,
                    end=expression.end,
                    text=text[expression.start : expression.end],
                    status=AmountLinkStatus.UNSUPPORTED,
                    reason=reason,
                    approximate=approximate,
                )
            )
            continue
        binding = _bind(ctx, expression)
        amounts.append(
            StepAmount(
                step_index=step_index,
                start=expression.start,
                end=expression.end,
                text=text[expression.start : expression.end],
                status=binding.status,
                reason=binding.reason,
                ingredient_position=binding.position,
                approximate=approximate,
            )
        )
    return tuple(amounts), len(expressions)


def _require_names(ingredients: object) -> list[str]:
    if isinstance(ingredients, (str, bytes)) or not isinstance(
        ingredients, Sequence
    ):
        raise TypeError("ingredients must be a sequence of ingredients")
    return [_name_of(item) for item in ingredients]


def _require_steps(steps: object) -> list[str]:
    if isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence):
        raise TypeError("steps must be a sequence of strings")
    result: list[str] = []
    for step in steps:
        if not isinstance(step, str):
            raise TypeError("every step must be a string")
        result.append(step)
    return result


def scan_step(
    text: str, ingredients: Sequence[object], *, step_index: int = 0
) -> StepAmountScan:
    """Scan one stored step and bind its amounts to the given ingredients.

    ``ingredients`` is the validated ingredient list -- `Ingredient` objects, mappings
    with a ``name`` key, or plain names. The result is deterministic and idempotent: the
    same text and names always produce the same spans in the same order. The step text is
    echoed exactly; nothing is written, converted or persisted.

    The per-step limit applies. A recipe-level caller can use `scan_recipe_steps`, which
    also applies the per-recipe limit.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    names = _require_names(ingredients)
    infos = _ingredient_infos(names)
    amounts, total = _scan_step(
        step_index, text, infos, MAX_SPANS_PER_STEP
    )
    return StepAmountScan(
        step_index=step_index,
        text=text,
        amounts=amounts,
        omitted=total - len(amounts),
    )


def scan_recipe_steps(
    steps: Sequence[str], ingredients: Sequence[object]
) -> RecipeStepScan:
    """Scan every step of one recipe, in stored order.

    The per-step limit (32 spans) and the per-recipe limit (128 spans) are applied as a
    deterministic prefix in step and start order. An omitted span is counted; the stored
    step text is returned unchanged and is never truncated, rewritten or reduced.
    """
    texts = _require_steps(steps)
    names = _require_names(ingredients)
    infos = _ingredient_infos(names)
    scanned: list[StepAmountScan] = []
    total = 0
    omitted_total = 0
    for index, text in enumerate(texts):
        remaining = max(0, MAX_SPANS_PER_RECIPE - total)
        limit = min(MAX_SPANS_PER_STEP, remaining)
        amounts, count = _scan_step(index, text, infos, limit)
        omitted = count - len(amounts)
        omitted_total += omitted
        total += len(amounts)
        scanned.append(
            StepAmountScan(
                step_index=index,
                text=text,
                amounts=amounts,
                omitted=omitted,
            )
        )
    return RecipeStepScan(tuple(scanned), omitted=omitted_total)


__all__ = [
    "MAX_SPANS_PER_RECIPE",
    "MAX_SPANS_PER_STEP",
    "AmountLinkReason",
    "AmountLinkStatus",
    "RecipeStepScan",
    "StepAmount",
    "StepAmountScan",
    "scan_recipe_steps",
    "scan_step",
]
