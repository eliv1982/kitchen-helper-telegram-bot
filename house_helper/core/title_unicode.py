"""The frozen character policy of a manual conversation title (PP-01-A1 D1, A1-04).

Why frozen. The rule "which characters may stand around a ZERO WIDTH JOINER", "may this
title contain any visible character at all" and "which format characters are refused"
must give one answer on the server and in the page. Asking each runtime for its own
Unicode property table does not: the supported Pythons do not agree with each other or
with Node.js. Python 3.10 ships Unicode 13.0, Python 3.12 ships Unicode 15.0, and
Node.js 24.14.0 ships Unicode 17.0 (ICU 78.2). An emoji assigned between two of them is
an emoji on one side and an unassigned symbol on the other; a format character assigned
later is refused by the runtime that knows it and accepted by one that does not. The
tables below are a frozen copy of the properties and the category that the policy uses,
shared by both validators through persistence.conversations and the generated block in
chat-logic.js (the parity is asserted by tests/test_web_ui_logic.py).

The three frozen tables and their version:

- ``Extended_Pictographic`` -- the emoji base a ZWJ may join. General category ``So``
  is NOT the test: U+2605 BLACK STAR is ``So`` and is not an emoji, so ``★``,
  U+200D, ``★`` must be refused.
- ``Default_Ignorable_Code_Point`` -- the characters that render as nothing on their
  own. A title made only of them (U+FE0F alone, U+034F, U+3164, a lone tag character)
  is blank, and is refused. Inside a meaningful title they are not refused by
  themselves: a variation selector and the ZWJ of a family emoji are legitimate parts
  of a sequence.
- ``General_Category=Cf`` -- the format characters. Every one is refused except the two
  kinds an emoji sequence may use (see "Titles" in persistence.conversations); a frozen
  table is needed because the category changed across the supported Pythons: U+0890 and
  U+0891 are unassigned in Unicode 13.0 and ``Cf`` from Unicode 14.0, and
  U+13439..U+1343F are unassigned in Unicode 13.0 and ``Cf`` from Unicode 15.0, so
  Python 3.10 would otherwise accept titles that Python 3.12 and the page refuse.

Provenance and version. The three tables were generated from the Unicode properties and
category as published in Unicode 17.0 and shipped by ICU 78.2 in Node.js 24.14.0, over
the whole range U+0000..U+10FFFF, by testing ``\\p{Extended_Pictographic}``,
``\\p{Default_Ignorable_Code_Point}`` and ``\\p{Cf}`` code point by code point and
coalescing adjacent code points into the ranges below. The exact generation is
reproducible with any runtime that exposes the same properties; the version is recorded
so a future Unicode revision is a deliberate review, not a silent change of what a
person may name a conversation.

The category checks for control, surrogate and line/paragraph separator characters
(``Cc``, ``Cs``, ``Zl``, ``Zp``) still come from each runtime's own
``unicodedata``/``\\p{...}`` table, and the rule for each is simply "forbidden". The two
kinds of guarantee differ, though. ``Cc`` and ``Cs`` rest on a formal Unicode stability
guarantee: the control characters (U+0000..U+001F, U+007F..U+009F) and the surrogate
code points (U+D800..U+DFFF) are permanently assigned to those categories, and their
sets are closed to new members, so their verdict is fixed. ``Zl`` and ``Zp`` are
singleton categories with no such guarantee -- ``Zl`` has exactly one member, U+2028
LINE SEPARATOR, and ``Zp`` exactly one, U+2029 PARAGRAPH SEPARATOR -- so their verdict
rests on those singleton definitions together with the verified agreement of the three
supported runtimes on every code point in all four categories (checked over
U+0000..U+10FFFF for Python 3.10, Python 3.12 and Node.js 24.14.0). A runtime that
disagrees, or a future revision that gives ``Zl`` or ``Zp`` another member, is a
deliberate review, like a table revision; this is recorded as a limitation of the policy.

One explicit non-property addition: U+2800 BRAILLE PATTERN BLANK is ``So``, is not
default-ignorable, and renders as an empty cell; a title of only U+2800 is blank, so it
is refused with the same "no visible character" answer.
"""

from __future__ import annotations

from typing import Iterable

# The Unicode version the three frozen tables below describe.
TITLE_UNICODE_VERSION = "17.0"

# Extended_Pictographic, Unicode 17.0 (ICU 78.2). 156 ranges.
EMOJI_BASE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x231A, 0x231B), (0x2328, 0x2328), (0x23CF, 0x23CF), (0x23E9, 0x23F3),
    (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB), (0x25B6, 0x25B6),
    (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x2604), (0x260E, 0x260E),
    (0x2611, 0x2611), (0x2614, 0x2615), (0x2618, 0x2618), (0x261D, 0x261D),
    (0x2620, 0x2620), (0x2622, 0x2623), (0x2626, 0x2626), (0x262A, 0x262A),
    (0x262E, 0x262F), (0x2638, 0x263A), (0x2640, 0x2640), (0x2642, 0x2642),
    (0x2648, 0x2653), (0x265F, 0x2660), (0x2663, 0x2663), (0x2665, 0x2666),
    (0x2668, 0x2668), (0x267B, 0x267B), (0x267E, 0x267F), (0x2692, 0x2697),
    (0x2699, 0x2699), (0x269B, 0x269C), (0x26A0, 0x26A1), (0x26A7, 0x26A7),
    (0x26AA, 0x26AB), (0x26B0, 0x26B1), (0x26BD, 0x26BE), (0x26C4, 0x26C5),
    (0x26C8, 0x26C8), (0x26CE, 0x26CF), (0x26D1, 0x26D1), (0x26D3, 0x26D4),
    (0x26E9, 0x26EA), (0x26F0, 0x26F5), (0x26F7, 0x26FA), (0x26FD, 0x26FD),
    (0x2702, 0x2702), (0x2705, 0x2705), (0x2708, 0x270D), (0x270F, 0x270F),
    (0x2712, 0x2712), (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D),
    (0x2721, 0x2721), (0x2728, 0x2728), (0x2733, 0x2734), (0x2744, 0x2744),
    (0x2747, 0x2747), (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755),
    (0x2757, 0x2757), (0x2763, 0x2764), (0x2795, 0x2797), (0x27A1, 0x27A1),
    (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2934, 0x2935), (0x2B05, 0x2B07),
    (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030),
    (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299), (0x1F004, 0x1F004),
    (0x1F02C, 0x1F02F), (0x1F094, 0x1F09F), (0x1F0AF, 0x1F0B0), (0x1F0C0, 0x1F0C0),
    (0x1F0CF, 0x1F0D0), (0x1F0F6, 0x1F0FF), (0x1F170, 0x1F171), (0x1F17E, 0x1F17F),
    (0x1F18E, 0x1F18E), (0x1F191, 0x1F19A), (0x1F1AE, 0x1F1E5), (0x1F201, 0x1F20F),
    (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F23C, 0x1F23F),
    (0x1F249, 0x1F25F), (0x1F266, 0x1F321), (0x1F324, 0x1F393), (0x1F396, 0x1F397),
    (0x1F399, 0x1F39B), (0x1F39E, 0x1F3F0), (0x1F3F3, 0x1F3F5), (0x1F3F7, 0x1F3FA),
    (0x1F400, 0x1F4FD), (0x1F4FF, 0x1F53D), (0x1F549, 0x1F54E), (0x1F550, 0x1F567),
    (0x1F56F, 0x1F570), (0x1F573, 0x1F57A), (0x1F587, 0x1F587), (0x1F58A, 0x1F58D),
    (0x1F590, 0x1F590), (0x1F595, 0x1F596), (0x1F5A4, 0x1F5A5), (0x1F5A8, 0x1F5A8),
    (0x1F5B1, 0x1F5B2), (0x1F5BC, 0x1F5BC), (0x1F5C2, 0x1F5C4), (0x1F5D1, 0x1F5D3),
    (0x1F5DC, 0x1F5DE), (0x1F5E1, 0x1F5E1), (0x1F5E3, 0x1F5E3), (0x1F5E8, 0x1F5E8),
    (0x1F5EF, 0x1F5EF), (0x1F5F3, 0x1F5F3), (0x1F5FA, 0x1F64F), (0x1F680, 0x1F6C5),
    (0x1F6CB, 0x1F6D2), (0x1F6D5, 0x1F6E5), (0x1F6E9, 0x1F6E9), (0x1F6EB, 0x1F6F0),
    (0x1F6F3, 0x1F6FF), (0x1F7DA, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F),
    (0x1F85A, 0x1F85F), (0x1F888, 0x1F88F), (0x1F8AE, 0x1F8AF), (0x1F8BC, 0x1F8BF),
    (0x1F8C2, 0x1F8CF), (0x1F8D9, 0x1F8FF), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945),
    (0x1F947, 0x1F9FF), (0x1FA58, 0x1FA5F), (0x1FA6E, 0x1FAFF), (0x1FC00, 0x1FFFD),
)

# Default_Ignorable_Code_Point, Unicode 17.0 (ICU 78.2). 17 ranges.
DEFAULT_IGNORABLE_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)

# General_Category=Cf, Unicode 17.0 (ICU 78.2). 21 ranges. U+200D ZERO WIDTH JOINER
# and the tag characters U+E0020..U+E007F are in this table: they are the two format
# kinds an emoji sequence may use, and persistence.conversations permits them only
# where the sequence needs them.
FORMAT_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD), (0x0600, 0x0605), (0x061C, 0x061C), (0x06DD, 0x06DD),
    (0x070F, 0x070F), (0x0890, 0x0891), (0x08E2, 0x08E2), (0x180E, 0x180E),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x2064), (0x2066, 0x206F),
    (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB), (0x110BD, 0x110BD), (0x110CD, 0x110CD),
    (0x13430, 0x1343F), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)

# A character that renders as nothing on its own. Beyond the default-ignorable set,
# U+2800 BRAILLE PATTERN BLANK is an ordinary symbol whose glyph is an empty cell.
BRAILLE_PATTERN_BLANK = 0x2800


def _in_ranges(point: int, ranges: Iterable[tuple[int, int]]) -> bool:
    for start, end in ranges:
        if point < start:
            return False
        if point <= end:
            return True
    return False


def is_emoji_base(point: int) -> bool:
    """True for a code point an emoji ZWJ sequence may join on either side."""
    return _in_ranges(point, EMOJI_BASE_RANGES)


def is_default_ignorable(point: int) -> bool:
    return _in_ranges(point, DEFAULT_IGNORABLE_RANGES)


def is_format(point: int) -> bool:
    """True for a General_Category=Cf code point, from the frozen Unicode 17.0 table."""
    return _in_ranges(point, FORMAT_RANGES)


def is_blank_looking(point: int) -> bool:
    """True for a code point that contributes nothing visible to a title."""
    return point == BRAILLE_PATTERN_BLANK or is_default_ignorable(point)


def _javascript_array(ranges: tuple[tuple[int, int], ...]) -> str:
    return ", ".join(f"[0x{start:04X}, 0x{end:04X}]" for start, end in ranges)


def javascript_policy_block() -> str:
    """The exact generated block chat-logic.js embeds, for the parity test.

    tests/test_web_ui_logic.py asserts this text appears in the page's script, so the
    server's frozen tables and the page's copy cannot drift apart.
    """
    return (
        "  // BEGIN GENERATED TITLE UNICODE POLICY -- do not edit by hand.\n"
        f"  // Source: house_helper/core/title_unicode.py, Unicode {TITLE_UNICODE_VERSION}\n"
        "  // (Extended_Pictographic, Default_Ignorable_Code_Point and General_Category=Cf;\n"
        "  // ICU 78.2 as shipped by Node.js 24.14.0). Both validators use these frozen\n"
        "  // tables, so the rule does not depend on either runtime's own Unicode version.\n"
        f"  var TITLE_EMOJI_BASE_RANGES = [{_javascript_array(EMOJI_BASE_RANGES)}];\n"
        f"  var TITLE_DEFAULT_IGNORABLE_RANGES = [{_javascript_array(DEFAULT_IGNORABLE_RANGES)}];\n"
        f"  var TITLE_FORMAT_RANGES = [{_javascript_array(FORMAT_RANGES)}];\n"
        "  // END GENERATED TITLE UNICODE POLICY"
    )
