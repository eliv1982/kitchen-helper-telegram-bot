"""The page's files, read as text: what they must never contain, and what they must agree on.

No browser and no Node here, so these run everywhere. They are the part of the checking that a
scripted server cannot do for the drawing code (app.js): that nothing a person or the assistant
wrote can ever be interpreted as markup or script, that nothing is loaded from anywhere else,
that the ids the script looks for are in the shell the server sends, and that the style keeps
the contrast and touch-target promises its header makes.

These are scans, not proofs: they catch the way code of this kind usually goes wrong (innerHTML,
eval, a remote script), not every way. The behaviour is checked in tests/test_web_ui_logic.py,
tests/test_web_ui_integration.py and, by hand, in a browser (README).
"""

import re
from pathlib import Path

import pytest

from house_helper.web import ui

STATIC = Path(ui.STATIC_DIR)
APP = (STATIC / "app.js").read_text(encoding="utf-8")
LOGIC = (STATIC / "chat-logic.js").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
SCRIPTS = {"app.js": APP, "chat-logic.js": LOGIC}


def code_of(source: str) -> str:
    """The source without its comments and string literals' contents left in place, for scans
    that must not trip over a sentence in a comment that names what the code avoids."""
    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[;{}),])\s*//.*$", "", without_blocks)


# --- nothing is ever interpreted as markup or script -----------------------------------------------------------

BANNED = [
    r"\.innerHTML", r"\.outerHTML", r"insertAdjacentHTML", r"document\.write", r"createContextualFragment",
    r"\bDOMParser\b", r"\.srcdoc", r"\beval\s*\(", r"\bnew\s+Function\b", r"(?<![\w.])Function\s*\(",
    r"set(?:Timeout|Interval)\s*\(\s*[\"'`]", r"javascript:", r"<script", r"\bimportScripts\b", r"\bimport\s*\(",
    r"\bnew\s+(?:Shared)?Worker\b", r"\bXMLHttpRequest\b", r"\bWebSocket\b", r"\bEventSource\b", r"sendBeacon",
    r"\bpostMessage\b", r"window\.open", r"\blocation\s*(?:\.href|\.assign|\.replace)?\s*=(?!=)", r"document\.cookie",
    r"\blocalStorage\b", r"\bindexedDB\b", r"Math\.random", r"\.setAttribute\(\s*[\"']on", r"\.setAttribute\(\s*[\"']style",
    r"\.onclick\s*=", r"\.on[a-z]+\s*=(?!=)", r"\bwith\s*\(",
]


@pytest.mark.parametrize("name", sorted(SCRIPTS))
@pytest.mark.parametrize("pattern", BANNED)
def test_the_scripts_contain_no_way_to_turn_text_into_code_or_markup(name, pattern):
    assert re.search(pattern, code_of(SCRIPTS[name])) is None, f"{name}: {pattern}"


def test_text_reaches_the_page_only_as_text_nodes():
    code = code_of(APP)

    assert code.count(".textContent") >= 20 and "createTextNode" in code
    # The one helper that builds elements refuses attributes that run code or style.
    assert re.search(r"/\^\(on\|style\$\)/i\.test\(name\)", code) is not None
    # Nothing builds a string of HTML and hands it over.
    assert not re.search(r"[\"'`]\s*<[a-zA-Z]", code), "a string that starts a tag"


def test_the_only_storage_is_session_storage_and_only_the_page_touches_it_and_the_logic_is_given_it():
    assert "sessionStorage" in code_of(APP)
    assert "sessionStorage" not in code_of(LOGIC)  # the logic only uses the storage it is handed
    assert "hh.pending.v1" in LOGIC
    # What the record may hold is the key's five short names (the scenarios check the values).
    assert 'c: record.conversationId, k: record.key, t: record.text, b: record.baseline, at: record.at' in LOGIC


def test_the_page_calls_only_its_own_origin_and_names_no_other_address():
    for name, source in SCRIPTS.items():
        code = code_of(source)
        assert not re.search(r"https?://", code), name
        assert "//cdn" not in code and "unpkg" not in code and "jsdelivr" not in code, name
    paths = re.findall(r'http\(\s*"(GET|POST)",\s*("[^"]*")', LOGIC) + re.findall(r'http\(\s*"(GET|POST)",\s*(\S+?),', LOGIC)
    assert paths, "the requests of the logic were not found"
    for method, path in paths:
        assert path.startswith('"/api/') or path.startswith('"/auth/'), path
    assert {method for method, _ in paths} == {"GET", "POST"}
    # Every request goes through one of two places, and each asks for the cookie of this site only: the
    # conversations' http() and the Telegram panel's call() (a controller of its own, which talks to
    # /api/telegram and nothing else).
    assert LOGIC.count("deps.fetch(") == 2
    assert LOGIC.count('credentials: "same-origin"') == 2
    telegram_paths = re.findall(r'call\(\s*"(GET|POST|DELETE)",\s*"([^"]*)"', LOGIC)
    assert telegram_paths and all(path.startswith("/api/telegram") for _, path in telegram_paths)
    assert 'credentials: "same-origin"' in LOGIC and 'redirect: "error"' in LOGIC
    # The fetch mode and the referrer policy are the browser's own (see ui.py: Origin).
    assert not re.search(r"\bmode\s*:", LOGIC) and not re.search(r"referrerPolicy", LOGIC)
    assert not re.search(r"\bmode\s*:", code_of(APP)) and "referrerPolicy" not in APP


def test_the_user_id_is_kept_to_tell_whose_page_it_is_and_is_never_sent():
    code = code_of(LOGIC)

    assert not re.search(r"\bX-User\b|Authorization|document\.cookie", code)
    # The server's /auth/me names the person (user_id), and only classifyMe reads that field...
    me_start = code.index("function classifyMe(")
    me_end = code.index("function classifyRead(")
    assert code.count("user_id") == code[me_start:me_end].count("user_id") > 0
    # ...the page keeps the id (the principal, and the owner written in each record of an unresolved message)
    # only to know whose page and whose record it is. It is sent nowhere: no request line mentions it, the
    # bodies are the message text, nothing, the conversation and message of an answer to keep, and the e-mail
    # address, and the one header is the idempotency key.
    for line in code.splitlines():
        if re.search(r"http\(|json:|headers|\.body|Idempotency", line):
            assert not re.search(r"principal|userId|owner|user_id", line), line
    assert re.findall(r"json:\s*(\{[^}]*\})", code) == [
        "{ text: turn.text }",
        "{}",
        "{ conversation_id: conversationId, message_id: messageId }",
        "{ email: checked.address }",
    ]
    assert "Idempotency-Key" in code and code.count('"Idempotency-Key"') == 1


# --- the shell and the script agree about the ids -----------------------------------------------------------------


def _shell_ids() -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', ui.render_shell(telegram=True, email=True)))


def test_every_element_the_script_looks_for_is_in_the_shell():
    wanted = set(re.findall(r'byId\("([^"]+)"\)', APP))
    ids = _shell_ids()

    assert wanted and wanted <= ids, wanted - ids
    # The ones a deployment may lack are looked at only when they exist.
    for identifier in ("email-form", "email-input", "email-submit", "email-result"):
        without = set(re.findall(r'\bid="([^"]+)"', ui.render_shell(telegram=True, email=False)))
        assert identifier in wanted and identifier not in without
    assert "if (els.emailForm)" in APP
    shell_without_telegram = set(re.findall(r'\bid="([^"]+)"', ui.render_shell(telegram=False, email=True)))
    assert "login-telegram" not in wanted and "login-telegram" not in shell_without_telegram


def test_the_script_and_the_style_use_the_same_breakpoint_and_the_script_looks_only_for_ids_that_exist_in_the_style():
    assert '"(max-width: 52rem)"' in APP and "@media (max-width: 52rem)" in CSS
    classes_in_css = set(re.findall(r"\.([a-zA-Z][\w-]*)", CSS))
    used = set(re.findall(r'class:\s*"([^"]+)"', APP))
    for group in used:
        for name in group.split():
            if name.startswith(("btn", "msg", "conv", "notice", "chip", "hint", "loading", "spinner", "empty", "older", "banner", "counter")):
                assert name in classes_in_css, f"app.js uses .{name}, which the style does not define"


def test_the_viewport_allows_zoom_and_the_markup_asks_for_no_fixed_sizes():
    html = ui.render_shell(telegram=True, email=True)
    viewport = re.search(r'name="viewport" content="([^"]+)"', html).group(1)

    assert "width=device-width" in viewport and "initial-scale=1" in viewport
    assert "user-scalable" not in viewport and "maximum-scale" not in viewport
    # An on-screen keyboard shrinks the page (the header and the composer stay in view above it) instead of covering it.
    assert "interactive-widget=resizes-content" in viewport
    assert 'style="' not in html


# --- the style ------------------------------------------------------------------------------------------------------


def _tokens() -> dict[str, str]:
    root = re.search(r":root\s*\{(.*?)\}", CSS, re.S).group(1)
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})\s*;", root))


def _luminance(colour: str) -> float:
    red, green, blue = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    linear = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)


def contrast(foreground: str, background: str) -> float:
    tokens = _tokens()
    a, b = _luminance(tokens[foreground]), _luminance(tokens[background])
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


TEXT_PAIRS = [
    ("text", "bg"), ("text", "surface"), ("text", "pale"), ("muted", "bg"), ("muted", "surface"), ("muted", "pale"),
    ("on-color", "blue"), ("on-color", "blue-dark"), ("on-color", "green"), ("on-color", "green-dark"),
    ("blue", "surface"), ("blue", "pale"), ("blue", "bg"), ("green-dark", "surface"), ("green-dark", "green-pale"),
    ("green", "surface"), ("green", "green-pale"), ("red-dark", "red-pale"), ("red-dark", "surface"), ("red-dark", "bg"),
    ("red", "surface"), ("red", "red-pale"),
]
EDGE_PAIRS = [("field-edge", "surface"), ("field-edge", "bg"), ("blue", "surface"), ("green", "surface"), ("red", "surface"), ("on-color", "blue")]


def test_the_palette_is_the_approved_family_and_the_tokens_are_what_the_rules_use():
    tokens = _tokens()

    # Royal blue, deep green, pale blue surfaces, a light neutral ground, red for errors.
    assert _luminance(tokens["blue"]) < 0.1 and tokens["blue"][3:5] > tokens["blue"][1:3] and int(tokens["blue"][5:7], 16) > int(tokens["blue"][1:3], 16)
    assert int(tokens["green"][3:5], 16) > int(tokens["green"][1:3], 16) and int(tokens["green"][3:5], 16) > int(tokens["green"][5:7], 16)
    assert _luminance(tokens["pale"]) > 0.8 and _luminance(tokens["bg"]) > 0.9
    assert int(tokens["red"][1:3], 16) > 150 and int(tokens["red"][3:5], 16) < 70
    assert "color-scheme: light" in CSS
    # Colours are written once, as tokens.
    outside = re.sub(r":root\s*\{.*?\}", "", CSS, flags=re.S)
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b", outside) == []


@pytest.mark.parametrize("foreground, background", TEXT_PAIRS)
def test_text_has_at_least_4_5_to_1_on_its_ground(foreground, background):
    assert contrast(foreground, background) >= 4.5, (foreground, background, contrast(foreground, background))


@pytest.mark.parametrize("foreground, background", EDGE_PAIRS)
def test_the_edge_of_a_control_has_at_least_3_to_1_on_its_ground(foreground, background):
    assert contrast(foreground, background) >= 3.0, (foreground, background, contrast(foreground, background))


def _rules() -> list[tuple[str, dict[str, str]]]:
    outside_root = re.sub(r":root\s*\{.*?\}", "", CSS, flags=re.S)
    stripped = re.sub(r"/\*.*?\*/", "", outside_root, flags=re.S)
    rules = []
    for selector, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", stripped):
        declarations = dict(
            (name.strip(), value.strip()) for name, value in (d.split(":", 1) for d in body.split(";") if ":" in d)
        )
        rules.append((" ".join(selector.split()), declarations))
    return rules


def _merged_rules() -> dict[str, dict[str, str]]:
    """The declarations of each selector, those of a media query laid over the plain ones."""
    merged: dict[str, dict[str, str]] = {}
    for selector, declarations in _rules():
        merged.setdefault(selector, {}).update(declarations)
    return merged


def test_every_rule_that_sets_both_a_colour_and_a_background_keeps_the_contrast():
    checked = 0
    for selector, declarations in _rules():
        colour = re.fullmatch(r"var\(--([a-z-]+)\)", declarations.get("color", ""))
        background = re.fullmatch(r"var\(--([a-z-]+)\)", declarations.get("background", ""))
        if colour and background:
            checked += 1
            assert contrast(colour.group(1), background.group(1)) >= 4.5, selector
    assert checked >= 8


def test_touch_targets_are_at_least_44_css_pixels():
    rules = _merged_rules()
    root_size = 16  # rem: nothing here changes the browser's default

    assert not re.search(r"html\s*\{[^}]*font-size", CSS)
    for selector in (".btn", ".chip", "input[type=\"email\"], textarea"):
        height = rules[selector]["min-height"]
        assert height.endswith("rem") and float(height[:-3]) * root_size >= 44, selector
    assert float(rules[".conv-item"]["min-height"][:-3]) * root_size >= 44
    assert float(rules[".telegram-more summary"]["min-height"][:-3]) * root_size >= 44
    assert float(rules[".btn"]["min-width"][:-3]) * root_size >= 44


def test_keyboard_focus_is_visible_and_motion_and_hidden_are_handled():
    rules = _merged_rules()

    assert ":focus-visible" in rules and re.match(r"([3-9]|\d\d)px solid var\(--", rules[":focus-visible"]["outline"])
    assert ".topbar :focus-visible" in rules  # on the blue bar the ring is the light colour
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert rules["[hidden]"]["display"] == "none !important"
    assert "outline: none" not in re.sub(r"h1:focus\s*\{[^}]*\}", "", CSS)  # the one heading that takes focus by script only


def test_the_style_loads_nothing_and_uses_system_fonts():
    assert "@import" not in CSS and "url(" not in CSS and "@font-face" not in CSS
    stack = re.search(r"font:\s*[^;]*?([^;]*system-ui[^;]*);", CSS)
    assert stack is not None and "Segoe UI" in stack.group(1) and "sans-serif" in stack.group(1)


def test_the_icon_is_a_plain_image_with_no_script_or_reference():
    icon = (STATIC / "icon.svg").read_text(encoding="utf-8")

    assert icon.lstrip().startswith("<svg") and "<script" not in icon and "onload" not in icon
    assert "href" not in icon and "xlink" not in icon and "<image" not in icon and "<foreignObject" not in icon.lower()


# --- the layout: what keeps the page from scrolling, and where the parts of the side column stay ------------------------


def test_the_page_cannot_be_made_taller_than_the_window_by_what_is_in_the_messages():
    rules = _merged_rules()

    # Each message holds a visually-hidden "who wrote this" label, which is positioned (out of the flow).
    assert rules[".visually-hidden"]["position"] == "absolute !important"
    # With no positioned box above it, the containing block of such a label is the page, and the label, at its
    # place in a long conversation, stretched the page by the length of the whole list (found in a real browser:
    # a blank page below the application). The scrolled list is therefore the containing block of its labels...
    assert rules[".messages"]["position"] == "relative" and rules[".messages"]["overflow-y"] == "auto"
    # ...and the application, exactly as tall as the window, holds whatever else sticks out.
    assert rules[".app"]["position"] == "relative" and rules[".app"]["overflow"] == "clip"
    assert rules[".app"]["height"] == "100dvh"


def test_the_side_column_keeps_its_top_and_its_foot_and_scrolls_the_list_between_them():
    rules = _merged_rules()

    assert rules[".sidebar"]["display"] == "flex" and rules[".sidebar"]["flex-direction"] == "column"
    assert rules[".conv-list"]["overflow-y"] == "auto" and rules[".conv-list"]["min-height"] == "0"
    assert rules[".telegram"]["flex"] == "none" and rules[".tabs"]["flex"] == "none"
    html = ui.render_shell(telegram=True, email=True)
    # The tabs, the button for a new conversation and the list are in this order, and Telegram comes after them.
    order = [html.index(marker) for marker in ('id="tab-chats"', 'id="new-conversation"', 'id="conversation-list"', 'id="telegram"')]
    assert order == sorted(order)


def test_the_long_telegram_text_is_in_a_details_and_what_matters_at_a_glance_is_not():
    html = ui.render_shell(telegram=True, email=True)
    block = re.search(r'<details id="telegram-more"([^>]*)>(.*?)</details>', html, re.S)

    assert block is not None
    # The shell does not decide whether it is open: the script does, by the state of the account (below).
    assert not re.search(r"\bopen\b", block.group(1))
    assert re.search(r"<summary>[^<]+</summary>", block.group(2))
    assert 'id="telegram-privacy"' in block.group(2)
    for glance in ("telegram-title", "telegram-text", "telegram-notice", "telegram-connect", "telegram-disconnect", "telegram-confirm"):
        assert f'id="{glance}"' in html and f'id="{glance}"' not in block.group(2), glance
    # The script shows the expander only when the logic has something to put in it, and empties and closes it with the panel.
    assert "els.telegramMore.hidden = !panel.privacy" in APP
    assert "els.telegramMore.open = false" in APP


def test_the_long_telegram_text_is_open_until_the_account_is_linked_and_the_readers_choice_is_left_alone():
    # Open by default unless the panel says "linked"; set only when that default changes, so a redraw of the
    # panel (a code, a poll, the question about disconnecting) does not undo what the reader opened or closed.
    assert 'var openByDefault = panel.phase !== "linked";' in APP
    assert "if (openByDefault !== explanationOpen) {" in APP
    assert "els.telegramMore.open = openByDefault;" in APP
    assert "explanationOpen = null;" in APP
    # The text itself is the same as before: it is not shortened, nor made a separate consent.
    assert "Что происходит с данными" in ui.render_shell(telegram=True, email=True)
