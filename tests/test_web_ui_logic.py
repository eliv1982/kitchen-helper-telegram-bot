"""The page's logic (house_helper/web/static/chat-logic.js), run in Node.js.

The logic is a plain script with no DOM: it decides what to send, what an answer means and
how to recover from an unclear one, and describes what the page should draw. Here it runs
against a scripted server (tests/js/harness.js: a fetch whose answers the scenario writes in
the shapes of the real API, a clock and timers the scenario moves, a storage it can break),
and each scenario of tests/js/logic_scenarios.js is one test below. Nothing real is called.

These tests need Node.js and are SKIPPED when `node` is not found. A skip is not a pass: the
CI image has Node, and `pytest -rs` lists what was skipped.

The server's side of the same agreements is checked where the server is: the text rules
(trimming, the length in code points, which characters are refused) are compared with the
server's own function over a fixed set of generated texts, and the wording of every error
code with the server's catalogue; tests/test_web_ui.py checks the real API's answers in the
shapes the scenarios assume, and tests/test_web_ui_integration.py runs this script against a
real server.

These tests do not run a browser: the drawing (app.js) is checked statically
(tests/test_web_ui_assets.py) and by hand (README, "Проверка в браузере").
"""

import json
import math
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from house_helper.core.limits import MAX_INPUT_LENGTH
from house_helper.web.chat_api import DEFAULT_LIST_LIMIT, _message_text
from house_helper.web.diagnostics import CATALOGUE, ApiError

NODE = shutil.which("node")
JS_DIR = Path(__file__).parent / "js"

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

ZONES = ["UTC", "Europe/Moscow", "America/New_York", "Asia/Kolkata"]


def _node(script: str, *args: str, stdin: str = "", timeout: int = 240) -> str:
    completed = subprocess.run(
        [NODE, str(JS_DIR / script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return completed.stdout


def _scenario_names() -> list[str]:
    if NODE is None:
        return []
    return json.loads(_node("run_scenarios.js", "--list"))["names"]


SCENARIOS = _scenario_names()
PLAIN = [name for name in SCENARIOS if not name.startswith("time_")]
TIMED = [name for name in SCENARIOS if name.startswith("time_")]

_runs: dict = {}


def _results(zone: str) -> dict:
    """Every scenario run once per time zone (the whole suite in UTC, and the scenarios that
    name times in the others)."""
    if zone not in _runs:
        only = [] if zone == "UTC" else ["--only=time_"]
        output = _node("run_scenarios.js", f"--tz={zone}", *only)
        _runs[zone] = json.loads(output.strip().splitlines()[-1])["results"]
    return _runs[zone]


def _crosscheck(task: str, **request) -> dict:
    return json.loads(_node("crosscheck.js", stdin=json.dumps({"task": task, **request})))


def test_the_scenarios_were_found():
    assert len(SCENARIOS) >= 70
    assert TIMED, "the scenarios that depend on the time zone"


@pytest.mark.parametrize("name", PLAIN)
def test_scenario(name):
    result = _results("UTC")[name]

    assert result["ok"], result.get("error")


@pytest.mark.parametrize("zone", ZONES)
@pytest.mark.parametrize("name", TIMED)
def test_scenario_in_time_zone(name, zone):
    result = _results(zone)[name]

    assert result["ok"], result.get("error")


# --- the page and the server agree about the text ---------------------------------------------


def test_the_client_strips_exactly_the_characters_the_server_strips():
    # Not JavaScript's trim(): it also strips U+FEFF and keeps U+001C..U+001F and U+0085.
    # The text kept for a retry must be the text the server stored.
    python = {code for code in range(0x110000) if not chr(code).strip()}

    javascript = set(_crosscheck("whitespace")["stripped"])

    assert javascript == python
    assert 0xFEFF not in javascript and 0x85 in javascript and 0x1C in javascript


SAMPLES = [
    "", "a", "я", "😀", "😀😀", "a😀b", "e\u0301", "\u0301", "\U0001f468\u200d\U0001f469\u200d👧", "\ud83d", "\ude00", "a\ud83db", "\ud83d\ud83d", "\ude00\ud83d",
    "x" * 2000, "😀" * 2000, "😀" * 2001, "я" * 1999 + "😀", "\U0010ffff" * 3, "\u0000", "a\u0000b",
]


def test_the_client_counts_code_points_as_python_does():
    counts = _crosscheck("count", texts=SAMPLES)["counts"]

    assert counts == [len(text) for text in SAMPLES]
    assert counts[SAMPLES.index("😀" * 2000)] == 2000  # 4000 UTF-16 units


def _generated_texts() -> list[str]:
    """A fixed, seeded set of texts built from the characters that matter: every kind of
    whitespace, controls, surrogates, astral characters, combining marks, and lengths at
    the limit."""
    generator = random.Random(20261018)
    spaces = [chr(code) for code in range(0x110000) if chr(code).isspace()]
    controls = [chr(code) for code in list(range(0, 32)) + list(range(0x7F, 0xA0))]
    odd = ["\ufeff", "\u200b", "᠎", "⁠", "\ud800", "\udbff", "\udc00", "\udfff", "�"]
    letters = ["a", "Z", "я", "Ё", "7", "!", "😀", "🍲", "\U0001f468", "\u0301", "\u200d", "é"]
    pool = spaces * 2 + controls + odd + letters * 4
    texts = []
    for _ in range(900):
        texts.append("".join(generator.choice(pool) for _ in range(generator.randint(0, 10))))
    for length in (1998, 1999, 2000, 2001, 2002):
        for _ in range(40):
            body = "".join(generator.choice(letters[:9]) for _ in range(length))
            texts.append(generator.choice(["", " ", "\n", "\u3000", "\u0085", "\u001f"]) + body + generator.choice(["", " ", "\r\n", "\u2028", "\u00a0"]))
    for _ in range(60):
        padding = "".join(generator.choice(spaces) for _ in range(generator.randint(1, 300)))
        texts.append(padding + "😀" * 2000 + padding)
        texts.append(padding + "x" + padding)
    return texts


def _server_verdict(text: str):
    try:
        return "ok", _message_text({"text": text})
    except ApiError as error:
        return error.code, None


def test_the_client_accepts_and_refuses_exactly_what_the_server_does():
    texts = _generated_texts()
    reasons = {"empty": "empty_message", "characters": "invalid_message_text", "too_long": "message_too_long"}

    results = _crosscheck("prepare", texts=texts, max=MAX_INPUT_LENGTH)["results"]

    assert len(results) == len(texts) >= 1200
    kinds = set()
    for text, result in zip(texts, results):
        code, stored = _server_verdict(text)
        expected = (code, stored)
        actual = ("ok", result["text"]) if result["ok"] else (reasons[result["reason"]], None)
        assert actual == expected, repr(text[:40])
        kinds.add(code)
    assert kinds == {"ok", "empty_message", "invalid_message_text", "message_too_long"}, "the texts cover every verdict"


def _titled_texts() -> list[str]:
    """Texts as they are STORED (the server's verdict "ok", already trimmed) plus the shapes that
    matter for a name: every kind of whitespace inside, astral characters at the cut, lengths
    around the limit."""
    stored = [text for verdict, text in map(_server_verdict, _generated_texts()) if verdict == "ok"]
    generator = random.Random(20261027)
    spaces = [chr(code) for code in range(0x110000) if chr(code).isspace()]
    pieces = ["слово", "а", "борщ", "🍲", "😀😀", "x" * 7, "é", "\u0301"]
    for _ in range(1500):
        text = ""
        for _ in range(generator.randint(1, 30)):
            text += generator.choice(pieces) + "".join(
                generator.choice(spaces) for _ in range(generator.randint(0, 3))
            )
        stored.append(text)  # (the rule is the same for any text; the server only ever gets the storable ones)
    for length in (58, 59, 60, 61, 62):
        for filler in ("x", "я", "😀", "x ", " я"):
            stored.append(("a" + filler * length)[: length + 1].strip() or "a")
            stored.append("x" * (length - 1) + " " + "y" * 5)
    return stored


def test_the_page_names_a_conversation_exactly_as_the_server_does():
    from house_helper.persistence.conversations import TITLE_LENGTH, conversation_title

    texts = _titled_texts()

    titles = _crosscheck("title", texts=texts)["titles"]

    assert len(texts) >= 1000
    expected = [conversation_title(text) for text in texts]
    assert titles == expected
    assert any(title and title.endswith("…") for title in titles), "the texts reach the cut"
    assert any(title and len(title) == TITLE_LENGTH for title in titles), "and stop exactly at the limit"
    # ... and what is not a name is not one on either side
    blanks = ["", " ", "\n\t ", "\u3000", "\u0085\u001f"]
    assert _crosscheck("title", texts=blanks)["titles"] == [conversation_title(text) for text in blanks] == [None] * 5


def _title_texts() -> list[str]:
    """The shapes a conversation title can take: every kind of whitespace at the ends,
    ordinary letters and punctuation, emoji sequences that need format characters, and
    the characters that must be refused (controls, line separators, bidi marks, the BOM,
    unpaired surrogates) -- single and inside a title, lengths around 200."""
    bodies = [
        "", "a", "я", "Борщ", "Borscht", "Борщ 🍲", "Семья \U0001f468\u200d\U0001f469\u200d👧",
        "\U0001f469\U0001f3fd\u200d\U0001f373", "❤️", "1️⃣", "🇷🇺", "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
        "e\u0301", "a\u200db", "a\u200cb", "a\u200bb", "a\ufeffb", "a\u00adb",
        "a\nb", "a\rb", "a\tb", "a\u0000b", "a\u001fb", "a\u007fb", "a\u0085b",
        "a\u2028b", "a\u2029b", "a\u202ab", "a\u2066b", "a\ud800b", "a\ude00b",
        " x ", "  ", "\u00a0\u2003",
        # A1-04: a forbidden line separator or control at an end must be refused before
        # trimming; whitespace that is not forbidden is still trimmed.
        "\u0411\u043e\u0440\u0449\n", "\n\u0411\u043e\u0440\u0449", "\u0411\u043e\u0440\u0449\r", "\r\u0411\u043e\u0440\u0449", "\u2028\u0411\u043e\u0440\u0449", "\u0411\u043e\u0440\u0449\u2028", "\u0411\u043e\u0440\u0449\u2029",
        "\u00a0\u0411\u043e\u0440\u0449\u3000",
        # The emoji format characters are allowed only inside the sequence that needs them.
        "\u200d", "\u200d\U0001f468", "\U0001f468\u200d", "\U0001f468\u200d\u200d\U0001f469",
        "\U000e0067\U000e0062\U000e007f", "\U000e0067", "a\U000e0067b",
        "\U0001f3f4\U000e0067\U000e0062", "\U0001f3f4\U000e007f", "\u0411\u043e\u0440\u0449 \U0001f3f4",
        "\u041f\u043e\u0432\u0430\u0440 \U0001f469\U0001f3fd\u200d\U0001f373", "\u0412\u0440\u0430\u0447 \U0001f468\u200d\u2695\ufe0f", "\u0421\u0435\u0440\u0434\u0446\u0435 \u2764\ufe0f\u200d\U0001f525",
        # A1-04: the independent audit's counterexamples. Each must be refused by BOTH
        # validators, from the same frozen character policy (core.title_unicode).
        "\ufe0f",  # standalone variation selector
        "\u034f",  # combining grapheme joiner
        "\u3164",  # Hangul filler
        "\u2605\u200d\u2605",  # So is not an emoji base
        "\U0001f3f4\U000e0020\U000e007f",  # tag space is not a flag letter
        "\U0001fae9",  # an emoji base newer than Python 3.12's Unicode 15.0
        "\U0001fae9\u200d\U0001fae9",  # and the same on both sides of a joiner
        # Blank-looking classes and the tag alphabet's edges.
        "\u2800", "\ufe0f\ufe0e", "\u115f\u1160", "\uffa0",
        "\U0001f3f4\U000e0031\U000e007f",  # digit tag
        "\U0001f3f4\U000e0061\U000e007f",  # one-letter subdivision flag (valid)
        "\U0001f3f4\U000e0061\U000e007a\U000e007f",  # a-z tags (valid)
        "\u0411\u043e\u0440\u0449 \u3164",  # a filler inside meaningful text is not blank
    ]
    for length in (199, 200, 201):
        body = "я" * length
        bodies.append(body)
        bodies.append("  " + body + "  ")
        bodies.append(body[:-1] + "\U0001f468\u200d\U0001f469\u200d👧")  # 200+ code points: the joiner counts
    # The start and end of every Unicode format (Cf) range, alone and in a title: these are
    # the characters the rule must treat one by one, not a blanket category.
    for start, end in (
        (0xAD, 0xAD), (0x600, 0x605), (0x61C, 0x61C), (0x6DD, 0x6DD), (0x70F, 0x70F),
        (0x890, 0x891), (0x8E2, 0x8E2), (0x180E, 0x180E), (0x200B, 0x200F),
        (0x202A, 0x202E), (0x2060, 0x2064), (0x2066, 0x206F), (0xFEFF, 0xFEFF),
        (0xFFF9, 0xFFFB), (0x110BD, 0x110BD), (0x110CD, 0x110CD), (0x13430, 0x1343F),
        (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0001, 0xE0001), (0xE0020, 0xE007F),
    ):
        for point in (start, end):
            character = chr(point)
            bodies.extend([character, "a" + character + "b"])
    return bodies


def test_the_page_validates_a_conversation_title_exactly_as_the_server_does():
    from house_helper.persistence.conversations import (
        InvalidConversationTitle,
        normalize_conversation_title,
    )

    texts = _title_texts()
    results = _crosscheck("conversation_title", texts=texts)["results"]

    assert len(results) == len(texts)
    for text, result in zip(texts, results):
        try:
            stored = normalize_conversation_title(text)
            expected = ("ok", None, stored)
        except InvalidConversationTitle as error:
            expected = ("error", error.reason, None)
        actual = ("ok", None, result["text"]) if result["ok"] else ("error", result["reason"], None)
        assert actual == expected, repr(text[:40])


def _policy_boundary_texts() -> list[str]:
    """Titles built from every boundary of the frozen character policy: each range's
    start, end, and the code points just outside them, alone, around a ZWJ, and inside
    ordinary text -- plus every tag character after the flag base. The server and the page
    must give the same verdict for all of them, not just the strings the audit named."""
    from house_helper.core.title_unicode import DEFAULT_IGNORABLE_RANGES, EMOJI_BASE_RANGES

    def boundary_points(ranges) -> list[int]:
        points = set()
        for start, end in ranges:
            for point in (start - 1, start, end, end + 1):
                if 0 <= point <= 0x10FFFF and not 0xD800 <= point <= 0xDFFF:
                    points.add(point)
        return sorted(points)

    texts: list[str] = []
    for point in boundary_points(EMOJI_BASE_RANGES):
        character = chr(point)
        texts.extend((character, character + "‍" + character, "Борщ " + character))
    for point in boundary_points(DEFAULT_IGNORABLE_RANGES):
        character = chr(point)
        texts.extend((character, "Борщ" + character))
    for point in range(0xE0020, 0xE0080):
        texts.append("\U0001f3f4" + chr(point) + "\U000e007f")
    return texts


def test_the_pages_emoji_and_blank_policy_agrees_with_the_server_at_every_frozen_boundary():
    from house_helper.persistence.conversations import (
        InvalidConversationTitle,
        normalize_conversation_title,
    )

    texts = _policy_boundary_texts()
    results = _crosscheck("conversation_title", texts=texts)["results"]

    assert len(results) == len(texts) > 1000
    for text, result in zip(texts, results):
        try:
            stored = normalize_conversation_title(text)
            expected = ("ok", None, stored)
        except InvalidConversationTitle as error:
            expected = ("error", error.reason, None)
        actual = ("ok", None, result["text"]) if result["ok"] else ("error", result["reason"], None)
        assert actual == expected, repr(text)


def test_the_pages_frozen_title_character_tables_are_the_servers():
    """A1-04: the page embeds the same Unicode policy data the server uses. The block is
    generated from house_helper/core/title_unicode.py; if either copy is edited alone,
    this fails rather than letting the two validators drift."""
    from house_helper.core.title_unicode import TITLE_UNICODE_VERSION, javascript_policy_block

    source = (Path(__file__).parent.parent / "house_helper" / "web" / "static" / "chat-logic.js").read_text(
        encoding="utf-8"
    )
    assert javascript_policy_block() in source
    assert f"Unicode {TITLE_UNICODE_VERSION}" in source
    # The equality is over the whole frozen table, not a sample.
    assert source.count("BEGIN GENERATED TITLE UNICODE POLICY") == 1
    assert source.count("END GENERATED TITLE UNICODE POLICY") == 1


def test_the_pages_title_error_words_are_the_servers():
    words = _crosscheck("texts")["TEXT"]
    from house_helper.web.diagnostics import CATALOGUE

    assert words["titleEmpty"] == CATALOGUE["empty_title"].message
    assert words["titleTooLong"] == CATALOGUE["title_too_long"].message
    assert words["titleCharacters"] == CATALOGUE["invalid_title"].message


def test_the_pages_limits_are_the_servers():
    from house_helper.persistence.profile import (
        MAX_DISPLAY_NAME_LENGTH,
        MEASURE_DEFAULTS,
        MEASURE_RANGES,
    )

    facts = _crosscheck("texts")

    assert facts["MAX"] == MAX_INPUT_LENGTH == 2000
    assert facts["MAX_PROFILE_NAME"] == MAX_DISPLAY_NAME_LENGTH == 200
    assert facts["PAGE_SIZE"] == DEFAULT_LIST_LIMIT
    # KH-F01-B1: the page checks the profile read against these, and the form against them
    # before a PATCH; the server's own tables come from core.measures.
    assert facts["MEASURE_LIMITS"] == {
        name: {
            "min": MEASURE_RANGES[name][0],
            "max": MEASURE_RANGES[name][1],
            "def": MEASURE_DEFAULTS[name],
        }
        for name in ("glass_ml", "teaspoon_ml", "tablespoon_ml")
    }


# --- KH-F01-B1: the page's fractions and Russian unit forms -----------------------------------

_VULGAR = {
    2: {1: "½"},
    3: {1: "⅓", 2: "⅔"},
    4: {1: "¼", 2: "½", 3: "¾"},
    6: {1: "⅙", 5: "⅚"},
    8: {1: "⅛", 3: "⅜", 5: "⅝", 7: "⅞"},
}
_HUNDREDTHS = {5: 20, 10: 10, 20: 5, 25: 4, 50: 2, 100: 1}
_UNIT_FORMS = {
    "glass": {
        "one": "ваш стакан",
        "few": "ваших стакана",
        "many": "ваших стаканов",
        "fraction": "вашего стакана",
    },
    "teaspoon": {
        "one": "ваша чайная ложка",
        "few": "ваших чайных ложки",
        "many": "ваших чайных ложек",
        "fraction": "вашей чайной ложки",
    },
    "tablespoon": {
        "one": "ваша столовая ложка",
        "few": "ваших столовых ложки",
        "many": "ваших столовых ложек",
        "fraction": "вашей столовой ложки",
    },
}


def _reference_measure(numerator: int, denominator: int, unit: str) -> str:
    """The formatter written independently of chat-logic.js, for the crosscheck."""
    if denominator < 1 or numerator < 0:
        return None
    divisor = math.gcd(numerator, denominator) or 1
    n, d = numerator // divisor, denominator // divisor
    if d == 1:
        amount = str(n)
    else:
        whole, rest = divmod(n, d)
        if rest in _VULGAR.get(d, {}):
            amount = (str(whole) if whole else "") + _VULGAR[d][rest]
        elif d in _HUNDREDTHS:
            hundredths = n * _HUNDREDTHS[d]
            integer, tail = divmod(hundredths, 100)
            if tail == 0:
                amount = str(integer)
            else:
                digits = f"{tail:02d}".rstrip("0")
                amount = f"{integer},{digits}"
        else:
            amount = f"{n}/{d}"
    if d != 1:
        form = "fraction"
    else:
        last_two = n % 100
        if 11 <= last_two <= 14:
            form = "many"
        elif n % 10 == 1:
            form = "one"
        elif 2 <= n % 10 <= 4:
            form = "few"
        else:
            form = "many"
    return f"{amount} {_UNIT_FORMS[unit][form]}"


def test_the_page_formats_every_practical_fraction_and_unit_as_the_server_side_reference_does():
    values = [
        [numerator, denominator, unit]
        for unit in ("glass", "teaspoon", "tablespoon")
        for denominator in list(range(1, 13)) + [20, 25, 50, 100]
        for numerator in range(0, 13)
    ]
    formatted = _crosscheck("fractions", values=values)["formatted"]

    assert len(formatted) == len(values) == 3 * 16 * 13
    expected = [_reference_measure(*value) for value in values]
    assert formatted == expected
    # The owner's examples are in the set and are what the page words.
    assert _reference_measure(4, 5, "glass") == "0,8 вашего стакана"
    assert _reference_measure(3, 2, "tablespoon") == "1½ вашей столовой ложки"
    assert _reference_measure(5, 4, "teaspoon") == "1¼ вашей чайной ложки"
    assert _reference_measure(19, 20, "glass") == "0,95 вашего стакана"


def test_every_error_the_server_can_name_has_the_same_words_in_the_page():
    words = _crosscheck("texts")["CODE_MESSAGES"]

    for code, spec in CATALOGUE.items():
        assert words.get(code) == spec.message, code
    # What the page adds is for the /auth endpoints and the code of a turn that failed while its
    # reply was stored: never a second wording of a catalogue code.
    assert set(words) - set(CATALOGUE) == {
        "temporarily_unavailable", "login_unavailable", "invalid_email", "server_error", "storage_unavailable",
    }


def test_the_profile_data_note_explains_the_retention_the_purge_applies():
    from house_helper.persistence.base import RETENTION_DAYS

    text = _crosscheck("texts")["TEXT"]
    messages, recipes, titles = (
        text["profileDataMessages"],
        text["profileDataRecipes"],
        text["profileDataTitles"],
    )

    # tests/test_admin_retention.py verifies the lifecycle these sentences describe: messages
    # and their photos expire one by one RETENTION_DAYS after their own creation, an old
    # conversation with a newer message survives, and a conversation itself goes only when it
    # is empty and has seen no activity for RETENTION_DAYS.
    day = f"{RETENTION_DAYS} дней"
    assert f"Сообщения и фотографии к ним хранятся {day} с момента создания" in messages
    assert "Диалог не удаляется только из-за возраста" in messages
    assert "есть более новые сообщения, остается" in messages
    assert f"не осталось сообщений и им не пользовались {day}" in messages
    assert not messages.startswith("Диалоги,"), "the old claim that conversations expire by age"

    # A recipe reply is an assistant message: it goes on the message's own deadline while the
    # conversation may stay. A favorite is a copy with no deadline.
    assert "с рецептами — это сообщения" in recipes
    assert f"каждый хранится {day} с момента создания" in recipes
    assert "даже если сам диалог остается" in recipes
    assert "Рецепты, сохраненные в избранное, хранятся, пока вы сами их не удалите." in recipes

    # PP-01-A1 (A1-08): a stored title is navigation metadata; it survives individual
    # message expiry, does not extend retention and is deleted with its conversation.
    assert "автоматическое название" in titles and "или ваше собственное" in titles
    assert "переименовать" in titles and "сбросить" in titles
    assert "истечение отдельных сообщений его не удаляет" in titles
    assert "Переименование не продлевает срок хранения" in titles
    assert "При удалении диалога его название удаляется вместе с ним" in titles
