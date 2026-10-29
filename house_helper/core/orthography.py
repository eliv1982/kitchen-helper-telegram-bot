"""The product's spelling rule for Russian text: the letter "ye" (U+0435), never "yo" (U+0451).

Everything House Helper itself writes to a person (its own screens and messages, and the
assistant's answers) uses "ye" and capital "Ye" where Russian orthography allows "yo" and
capital "Yo". The fixed texts are written that way in the source. The assistant's answers
are not ours to write, so replace_yo() is applied to each new one before it is stored or
delivered.

The rule is for prose. An answer also carries text that a machine reads, and that must come
out exactly as it went in: a web address (a Unicode domain, a path, a query), an e-mail
address, the destination of a Markdown link, code. Respelled, a link leads somewhere else or
nowhere. replace_yo() leaves those spans untouched and spells everything around them, the
visible label of a link included. What counts as such a span is listed below, next to the
patterns that find it.

What it must never be applied to is what a person wrote or sent (their messages, the
titles made from them, their photos) or to anything already stored: those stay exactly as
they were entered. It is applied once, at the moment an answer is produced, and nowhere on
the way back out.

The letters are spelled as code points on purpose: the test that keeps the source free of
"yo" (tests/test_orthography.py) scans this file too.
"""

import re
from typing import Optional

SMALL_YO, CAPITAL_YO = 0x0451, 0x0401
SMALL_YE, CAPITAL_YE = 0x0435, 0x0415
COMBINING_DIAERESIS = 0x0308

_YO_TO_YE = {SMALL_YO: SMALL_YE, CAPITAL_YO: CAPITAL_YE}

# "yo" written as two characters, "ye" and a combining diaeresis, which a model may
# produce. The Latin "e" with a diaeresis is another letter and is left alone: only the
# Cyrillic "ye" is matched.
_DECOMPOSED_YO = re.compile(f"([{chr(SMALL_YE)}{chr(CAPITAL_YE)}]){chr(COMBINING_DIAERESIS)}")


# --- code that is fenced -------------------------------------------------------------------------------
#
# A fence is found line by line, before anything inside a line is looked at: a fenced block can
# hold anything, including text that looks like the spans below.

# What may stand before a fence on its line: indentation, a quotation mark, a list marker.
_BLOCK_PREFIX = r"(?:[ \t]|>|[-*+][ \t]|\d{1,9}[.)][ \t])*"
_FENCE_OPENING = re.compile(_BLOCK_PREFIX + r"(`{3,}|~{3,})([^\n]*)")
_FENCE_CLOSING = re.compile(_BLOCK_PREFIX + r"(`{3,}|~{3,})[ \t\r]*\n?")
_LINES = re.compile(r"[^\n]*\n|[^\n]+")


def _split_fences(text: str) -> list[tuple[str, bool]]:
    """`text` cut into (piece, is_fenced_code) pieces in order; joined, they are `text`.
    A fence runs from its opening line to the line that closes it (the same mark, at least
    as long), or to the end of the text when nothing closes it."""
    pieces: list[tuple[str, bool]] = []
    plain_from = fence_from = 0
    fence: Optional[tuple[str, int]] = None  # the mark and the length of the fence that is open
    for line in _LINES.finditer(text):
        if fence is None:
            opening = _FENCE_OPENING.match(line.group())
            if opening is None:
                continue
            mark = opening.group(1)
            if mark[0] == "`" and "`" in opening.group(2):
                continue  # three backticks, words and three more: a code span, not a fence
            if line.start() > plain_from:
                pieces.append((text[plain_from : line.start()], False))
            fence, fence_from = (mark[0], len(mark)), line.start()
        else:
            closing = _FENCE_CLOSING.fullmatch(line.group())
            mark = closing.group(1) if closing is not None else ""
            if mark[:1] == fence[0] and len(mark) >= fence[1]:
                pieces.append((text[fence_from : line.end()], True))
                plain_from, fence = line.end(), None
    if fence is not None:
        pieces.append((text[fence_from:], True))
    elif plain_from < len(text):
        pieces.append((text[plain_from:], False))
    return pieces


# --- spans inside a line or a paragraph ------------------------------------------------------------------
#
# One pattern of alternatives, tried from left to right: of two spans that overlap, the one that
# starts first is kept whole. A span that is found where it should not be costs only a letter left
# as it was; a span that is missed costs a link. Where in doubt the patterns therefore take more.

# Code in backticks: a run of backticks, up to the next run of the same length, in the same
# paragraph. A run with no partner is just backticks.
_CODE_SPAN = r"(?<!`)(?P<ticks>`+)(?!`)(?:(?!\n[ \t\r]*\n)[\s\S])*?(?<!`)(?P=ticks)(?!`)"

# The destination of a Markdown link or image, `](here "title")`: in angle brackets, or without
# spaces and with balanced parentheses. The label before it and the title after it are prose.
_LINK_DESTINATION = (
    r"\]\([ \t]*(?:\n[ \t]*)?"
    r"(?:<[^<>\n]*>|(?!<)(?:\\.|[^\s()\\]|\((?:\\.|[^\s()\\]|\((?:\\.|[^\s()\\])*\))*\))*)"
)

# The destination of a reference definition, `[label]: here "title"`; the label is captured. The title
# is prose. The label is not: see _BRACKETED below.
_REFERENCE_HEAD = r"^[ \t]{0,3}\[([^\]\n]*)\]:"
_REFERENCE_DEFINITION = _REFERENCE_HEAD + r"[ \t]*(?P<reference>(?:<[^<>\n]*>|[^\s<]\S*))"

# A tag of HTML with its attributes (an `href`, an `alt`). Names are ASCII, so a Russian word in
# angle brackets is not one.
_HTML_TAG = (
    r"</?[A-Za-z][A-Za-z0-9-]*"
    r"(?:\s+[A-Za-z_:][\w:.-]*(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*"
    r"\s*/?>"
)

# An e-mail address. It may start only where a word does: the pattern would otherwise run to the
# end of every long word from each of its letters.
_EMAIL = r"(?<![\w.+%-])[\w.+%-]+@[\w-]+(?:\.[\w-]+)+"

# What ends a web address in a text: a space, a quotation mark, an angle bracket, a backtick.
_ADDRESS_TAIL = r"[^\s<>\"`«»“”]"

# A web address with a scheme (`https://`, `ftp://`, `mailto:`, `tel:`), or one that begins "www.".
_URL = (
    rf"(?<![A-Za-z0-9+.-])(?:[A-Za-z][A-Za-z0-9+.-]*://|(?i:mailto|tel):){_ADDRESS_TAIL}+"
    rf"|(?<!\w)(?i:www)\.{_ADDRESS_TAIL}+"
)

# A host name written without a scheme (`site.example/page`). A full stop between two words does
# not make one: a sentence that lacks the space after its full stop looks the same. It is
# recognised in two ways, and the first needs nothing after the name:
#  * its last label is one of these top-level domains (`example.рф`, `Example.com`);
#  * a path, a query or a fragment follows it, and its last label is a word of two letters or more
#    that begins with a lower-case letter (`1000.menu/cooking`). That is no list to maintain, and
#    "3.5/4", "Слово.Слово/слово" and "т.е./т.д." are not taken for addresses.
_TOP_LEVEL_DOMAINS = (
    "рф", "рус", "москва", "онлайн", "сайт", "дети", "бел", "укр", "орг", "ком",
    "ru", "su", "com", "org", "net", "edu", "gov", "info", "biz", "io", "me", "ua", "by",
    "kz", "uz", "de", "uk", "us", "eu", "tv", "cc", "ai", "app", "dev", "xyz", "online",
    "site", "shop", "store", "club", "pro", "cloud", "tech",
)
_HOST_LABELS = r"(?<![\w.-])(?:[\w-]+\.)+"
# Cyrillic capitals are U+0400..U+042F, spelled as code points like the letters above.
_LOWER_CASE_LETTER = r"(?![A-Z\u0400-\u042F])[^\W\d_]"
_HOST = (
    rf"{_HOST_LABELS}(?i:{'|'.join(_TOP_LEVEL_DOMAINS)})(?![\w-])(?::\d+)?(?:[/?#]{_ADDRESS_TAIL}*)?"
    rf"|{_HOST_LABELS}{_LOWER_CASE_LETTER}[\w-]+(?::\d+)?[/?#]{_ADDRESS_TAIL}+"
)

_MACHINE_TEXT = re.compile(
    "|".join(
        [_CODE_SPAN, _LINK_DESTINATION, _REFERENCE_DEFINITION, _HTML_TAG, _EMAIL, _URL, _HOST]
    ),
    re.MULTILINE,
)


# --- the labels of reference links -----------------------------------------------------------------------
#
# `[text][label]`, `[label][]` and `[label]` are linked to the line `[label]: destination` by the
# label alone. It is an identifier, not prose: spelled in one place and not in the other, it would
# no longer find its definition. So a label that some definition of the answer names is kept
# exactly where it stands, in the definition and in every use of it, and the text of the link
# (`text` above) is spelled like any other prose. In the two short forms the label is also the
# text, and keeps its letter to keep the link.

_DEFINED_LABEL = re.compile(_REFERENCE_HEAD, re.MULTILINE)

# A bracketed word that may be a label. Not one that is followed by `(`, which is the text of an
# inline link, or by a non-empty `[label]`, which is the text of a reference link: the label is
# the bracket after it.
_BRACKETED = re.compile(r"\[([^\[\]\n]*)\](?!\()(?!\[[^\[\]\n]+\])")


def _label_key(label: str) -> str:
    """The label as Markdown compares labels: without regard to case or to runs of spaces."""
    return " ".join(label.split()).casefold()


def _defined_labels(prose: list[str]) -> set[str]:
    """The labels that the definitions in `prose` (the text outside fenced code) name."""
    keys = {_label_key(found.group(1)) for piece in prose for found in _DEFINED_LABEL.finditer(piece)}
    keys.discard("")
    return keys


def _spell(prose: str) -> str:
    return _DECOMPOSED_YO.sub(r"\1", prose).translate(_YO_TO_YE)


def _spell_around_spans(text: str, labels: set[str]) -> str:
    """`text` with the prose between its machine-readable spans spelled and the spans as they were.
    `labels` are those that a definition names, wherever in the answer it stands."""
    spans = []
    for found in _MACHINE_TEXT.finditer(text):
        kept = "reference" if found.group("reference") is not None else 0  # the group, or all of it
        spans.append(found.span(kept))
    spans += [
        found.span() for found in _BRACKETED.finditer(text) if _label_key(found.group(1)) in labels
    ]
    spelled: list[str] = []
    done = 0
    for start, end in sorted(spans):
        if end <= done:
            continue  # inside a span that is kept already
        spelled.append(_spell(text[done : max(start, done)]))
        spelled.append(text[max(start, done) : end])
        done = end
    spelled.append(_spell(text[done:]))
    return "".join(spelled)


def replace_yo(text: str) -> str:
    """`text` with every "yo" written as "ye" and every capital "Yo" as capital "Ye", in the
    single-character and in the decomposed form, except inside web and e-mail addresses, link
    destinations, the labels of reference links, HTML tags and code, which are returned exactly as
    they were. Anything else is returned as it is."""
    pieces = _split_fences(text)
    labels = _defined_labels([piece for piece, is_code in pieces if not is_code])
    return "".join(
        piece if is_code else _spell_around_spans(piece, labels) for piece, is_code in pieces
    )
