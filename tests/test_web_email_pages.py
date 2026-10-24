"""The pages of email sign-in: self-contained, locked down, and the script that
carries the secret from the address bar into the form.

The link's secret is in the URL fragment. These tests pin what makes that safe to
open: no resource from anywhere else, a Content-Security-Policy that allows exactly
the page's own script and style (by hash, which the test recomputes from the served
page instead of trusting the module), no way for the script to send anything, headers
that keep the page out of caches, frames and other sites' referrers.

The script itself is run, when Node.js is installed, against a stand-in for the few
browser objects it touches, so that a typo in it cannot go unnoticed. (No browser is
involved, and none is claimed to have been tested.)
"""

import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from uuid import uuid4

import pytest

from house_helper.web import email_pages

CHALLENGE_ID = str(uuid4())
TOKEN = "Zx9-_" + "AbCdEfGhIj" * 3 + "KlMnOpQr"  # 43 characters of the URL-safe alphabet
assert len(TOKEN) == 43


def policy_of(page) -> dict[str, str]:
    directives = [d.strip() for d in page.headers["Content-Security-Policy"].split(";") if d.strip()]
    return {d.split(" ", 1)[0]: d.split(" ", 1)[1] if " " in d else "" for d in directives}


def inline(page, tag: str) -> str:
    found = re.findall(rf"<{tag}>(.*?)</{tag}>", page.html, re.DOTALL)
    assert len(found) == 1, f"{len(found)} inline <{tag}> elements"
    return found[0]


def sha256_source(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


def confirm():
    return email_pages.confirm_page(CHALLENGE_ID, "e***@example.com")


ALL_PAGES = {
    "confirm": confirm(),
    "invalid": email_pages.invalid_link_page(),
    "other user": email_pages.other_user_page(),
    "forbidden": email_pages.forbidden_page(),
    "unavailable": email_pages.unavailable_page(),
    "error": email_pages.error_page(),
}


def test_the_confirmation_page_is_branded_house_helper():
    page = confirm()

    assert "<title>Вход в House Helper</title>" in page.html and "<h1>Вход в House Helper</h1>" in page.html
    assert "Kitchen" not in page.html and "Кухонн" not in page.html


# --- the policy ---------------------------------------------------------------------------


def test_the_policy_allows_the_pages_own_script_and_style_by_their_hashes_and_nothing_else():
    page = confirm()

    assert policy_of(page) == {
        "default-src": "'none'",
        "script-src": sha256_source(inline(page, "script")),  # recomputed from what is served
        "style-src": sha256_source(inline(page, "style")),
        "form-action": "'self'",
        "base-uri": "'none'",
        "frame-ancestors": "'none'",
    }


def test_the_policy_names_no_way_to_run_or_load_anything_else():
    for name, page in ALL_PAGES.items():
        policy = page.headers["Content-Security-Policy"]
        for forbidden in ("unsafe-inline", "unsafe-eval", "unsafe-hashes", "*", "http:", "https:", "data:", "blob:", "strict-dynamic"):
            assert forbidden not in policy, (name, forbidden)
        assert "connect-src" not in policy  # default-src 'none' covers fetch, XHR, WebSocket, beacons
        assert policy_of(page)["default-src"] == "'none'", name


def test_a_page_without_a_script_may_not_run_one_or_submit_anywhere():
    for name, page in ALL_PAGES.items():
        if name == "confirm":
            continue
        assert "<script" not in page.html, name
        assert "script-src" not in policy_of(page), name
        assert policy_of(page)["form-action"] == "'none'", name
        assert "<form" not in page.html, name


def test_a_change_to_the_script_or_the_style_would_not_pass_the_policy():
    page = confirm()
    changed = page.html.replace("button.disabled = false;", "button.disabled = false; steal();")

    assert changed != page.html
    assert sha256_source(re.findall(r"<script>(.*?)</script>", changed, re.DOTALL)[0]) != policy_of(page)["script-src"]


def test_every_page_is_private_unframable_and_not_sniffed():
    for name, page in ALL_PAGES.items():
        assert page.headers["Cache-Control"] == "no-store", name
        assert page.headers["Referrer-Policy"] == "same-origin", name  # see the module's docstring
        assert page.headers["X-Content-Type-Options"] == "nosniff", name
        assert page.headers["X-Frame-Options"] == "DENY", name
        assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"], name
        assert '<meta name="robots" content="noindex">' in page.html, name


def test_the_statuses():
    assert {name: page.status_code for name, page in ALL_PAGES.items()} == {
        "confirm": 200, "invalid": 400, "other user": 409, "forbidden": 403, "unavailable": 503, "error": 500,
    }


# --- nothing from anywhere else ------------------------------------------------------------------


def test_the_page_loads_nothing_and_links_nowhere():
    for name, page in ALL_PAGES.items():
        text = page.html
        assert "http://" not in text and "https://" not in text and "//" not in re.sub(r"<!doctype[^>]*>", "", text).replace("*/", ""), name
        for pattern in (r"\bsrc\s*=", r"\bhref\s*=", r"<link\b", r"<img\b", r"<iframe\b", r"<object\b", r"<embed\b",
                        r"<audio\b", r"<video\b", r"<base\b", r"@import", r"url\(", r"javascript:", r"\bon[a-z]+\s*="):
            assert re.search(pattern, text, re.IGNORECASE) is None, (name, pattern)
        assert not re.search(r"<meta[^>]+http-equiv", text, re.IGNORECASE), name


def test_the_only_form_posts_back_to_this_site_with_two_fields_and_no_get():
    page = confirm()

    forms = re.findall(r"<form\b[^>]*>", page.html)
    assert len(forms) == 1
    assert 'method="post"' in forms[0] and 'action="/auth/email/confirm"' in forms[0]
    names = re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', page.html)
    assert names == [("c", CHALLENGE_ID), ("token", "")]  # the secret is not in the markup
    assert page.html.count("<button") == 1 and 'type="submit"' in page.html


def test_the_secret_is_never_in_the_markup_the_server_sends():
    # The server cannot put it there: it never receives it (the fragment stays in the
    # browser). Pinned from the other side: the page is built from the id and the
    # masked address alone.
    import inspect

    assert list(inspect.signature(email_pages.confirm_page).parameters) == ["challenge_id", "masked_address"]


def test_what_goes_into_the_page_is_escaped():
    page = email_pages.confirm_page('"><script>alert(1)</script>', 'a"<b>&@x.test')

    assert "<script>alert(1)</script>" not in page.html
    assert html.escape('a"<b>&@x.test') in page.html and html.escape('"><script>alert(1)</script>', quote=True) in page.html
    assert page.html.count("<script>") == 1  # the page's own


def test_the_page_names_the_masked_address_and_never_a_full_one():
    page = email_pages.confirm_page(CHALLENGE_ID, "e***@example.com")

    assert "e***@example.com" in page.html and "elena" not in page.html.lower()
    assert 'lang="ru"' in page.html and "Войти" in page.html


# --- the script -------------------------------------------------------------------------------------


def test_the_script_reads_the_fragment_removes_it_and_cannot_send_anything():
    script = inline(confirm(), "script")

    assert "window.location.hash" in script
    assert "history.replaceState" in script
    assert 'form.elements["token"].value = token' in script
    for banned in ("fetch", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "importScripts",
                   "eval", "Function(", "document.write", "innerHTML", "outerHTML", "insertAdjacent",
                   "localStorage", "sessionStorage", "indexedDB", "document.cookie", "location.href =",
                   "location.assign", "location.replace", "window.open", "postMessage", "import(",
                   "<script", "src ="):
        assert banned not in script, banned


def test_the_script_accepts_a_secret_of_exactly_the_shape_the_server_issues():
    script = inline(confirm(), "script")

    assert "/^[A-Za-z0-9_-]{43}$/" in script  # persistence.email_login: token_urlsafe(32)


NODE = shutil.which("node")
DRIVER = r"""
const script = require("fs").readFileSync(0, "utf8");
const fragment = process.env.FRAGMENT;
const calls = [];
const form = {
  elements: { token: { value: "" } },
  addEventListener(type, handler) { calls.push(["listen", type]); form.submit = handler; },
};
const button = { disabled: true };
const problem = { hidden: true };
const nodes = { login: form, go: button, problem };
const window = {
  location: { hash: fragment, pathname: "/auth/email/confirm", search: "?c=11111111-2222-3333-4444-555555555555" },
  history: { replaceState(...args) { calls.push(["replaceState", ...args]); } },
};
const document = { getElementById: (id) => nodes[id] };
new Function("window", "document", script)(window, document);
const before = button.disabled;
if (form.submit) form.submit();
console.log(JSON.stringify({
  token: form.elements.token.value, enabled: !before, problemHidden: problem.hidden,
  disabledAfterSubmit: button.disabled, calls,
}));
"""


def run_script(fragment: str) -> dict:
    completed = subprocess.run(
        [NODE, "-e", DRIVER],
        input=inline(confirm(), "script"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "FRAGMENT": fragment},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_script_puts_a_good_secret_in_the_form_enables_the_button_and_clears_the_address_bar():
    result = run_script("#" + TOKEN)

    assert result["token"] == TOKEN and result["enabled"] is True and result["problemHidden"] is True
    assert ["replaceState", None, "", "/auth/email/confirm?c=11111111-2222-3333-4444-555555555555"] in result["calls"]
    assert TOKEN not in json.dumps([c for c in result["calls"] if c[0] == "replaceState"])
    assert result["disabledAfterSubmit"] is True  # a double click cannot send it twice


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
@pytest.mark.parametrize(
    "fragment",
    ["", "#", "#" + TOKEN[:-1], "#" + TOKEN + "A", "#" + TOKEN[:-1] + "=", "#" + TOKEN[:-1] + " ",
     "#" + TOKEN[:-1] + "%", "#<script>", "#" + "é" * 43, "##" + TOKEN, TOKEN],
)
def test_the_script_refuses_anything_else_and_shows_the_problem(fragment):
    result = run_script(fragment)

    assert result["token"] == "" and result["enabled"] is False and result["problemHidden"] is False
    assert any(call[0] == "replaceState" for call in result["calls"])  # even a bad one leaves the bar


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_script_survives_a_browser_that_refuses_to_rewrite_the_address():
    script = inline(confirm(), "script")
    refusing = DRIVER.replace(
        'history: { replaceState(...args) { calls.push(["replaceState", ...args]); } }',
        'history: { replaceState() { throw new Error("SecurityError"); } }',
    )
    assert refusing != DRIVER

    completed = subprocess.run(
        [NODE, "-e", refusing], input=script, capture_output=True, text=True,
        encoding="utf-8", timeout=60, env={**os.environ, "FRAGMENT": "#" + TOKEN},
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["token"] == TOKEN
