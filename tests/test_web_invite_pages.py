"""The pages of the Telegram invitation sign-in: self-contained, locked down, and the script
that carries the secret from the address bar to the start request.

The invitation's secret is in the URL fragment. These tests pin what makes that safe to open:
no resource from anywhere else, a Content-Security-Policy that allows exactly the page's own
script and style (by hash, which the test recomputes from the served page instead of trusting
the module) and lets the script call this origin and nothing else, headers that keep the page out
of caches, frames and other sites' referrers, and Russian text throughout.

The script itself is run, when Node.js is installed, against a stand-in for the few browser
objects it touches (no browser is involved, and none is claimed to have been tested): it must
take the code out of the address bar before it does anything else, send nothing until the button
is pressed, send the code only in the body of one POST to this origin, never touch storage or
cookies, follow only an address at Telegram, and handle every kind of answer.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from uuid import uuid4

import pytest

from house_helper.web import invite_pages

INVITATION_ID = str(uuid4())
SECRET = "Zx9-_" + "AbCdEfGhIj" * 3 + "KlMnOpQr"  # 43 characters of the URL-safe alphabet
assert len(SECRET) == 43
CODE = f"{INVITATION_ID}.{SECRET}"
TELEGRAM_URL = "https://oauth.telegram.org/auth?response_type=code&state=abc"


def policy_of(page) -> dict[str, str]:
    directives = [d.strip() for d in page.headers["Content-Security-Policy"].split(";") if d.strip()]
    return {d.split(" ", 1)[0]: d.split(" ", 1)[1] if " " in d else "" for d in directives}


def inline(page, tag: str) -> str:
    found = re.findall(rf"<{tag}>(.*?)</{tag}>", page.html, re.DOTALL)
    assert len(found) == 1, f"{len(found)} inline <{tag}> elements"
    return found[0]


def sha256_source(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


def visible_text(page) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<script>.*?</script>|<style>.*?</style>|<[^>]+>", " ", page.html, flags=re.DOTALL))


INVITATION = invite_pages.invitation_page()
ALL_PAGES = {
    "invitation": INVITATION,
    "done": invite_pages.done_page(),
    "not configured": invite_pages.not_configured_page(),
    "unavailable": invite_pages.unavailable_page(),
    "provider unavailable": invite_pages.provider_unavailable_page(),
    "invalid invitation": invite_pages.invalid_invitation_page(),
    "identity": invite_pages.identity_page(),
    "other user": invite_pages.other_user_page(),
    "login other user": invite_pages.login_other_user_page(),
    "not completed": invite_pages.not_completed_page(),
    "stale": invite_pages.stale_page(),
    "error": invite_pages.error_page(),
}


# --- the policy ---------------------------------------------------------------------------


def test_the_policy_allows_the_pages_own_script_and_style_by_their_hashes_and_this_origin_to_call():
    policy = policy_of(INVITATION)

    assert policy["script-src"] == sha256_source(inline(INVITATION, "script"))
    assert policy["style-src"] == sha256_source(inline(INVITATION, "style"))
    assert policy["connect-src"] == "'self'"  # the start request, and nowhere else
    assert policy["default-src"] == "'none'"
    assert policy["form-action"] == "'none'" and policy["base-uri"] == "'none'"
    assert policy["frame-ancestors"] == "'none'"


def test_the_policy_names_no_way_to_run_or_load_anything_else():
    for name, page in ALL_PAGES.items():
        text = page.headers["Content-Security-Policy"]
        assert "unsafe-inline" not in text and "unsafe-eval" not in text, name
        assert "http:" not in text and "*" not in text and "data:" not in text, name
        assert "navigate-to" not in text, name


def test_a_page_without_a_script_may_not_run_one_or_call_anywhere():
    for name, page in ALL_PAGES.items():
        if name == "invitation":
            continue
        policy = policy_of(page)
        assert "script-src" not in policy and "connect-src" not in policy, name
        assert "<script" not in page.html, name


def test_a_change_to_the_script_or_the_style_would_not_pass_the_policy():
    policy = policy_of(INVITATION)

    assert sha256_source(inline(INVITATION, "script") + " ") != policy["script-src"]
    assert sha256_source(inline(INVITATION, "style") + " ") != policy["style-src"]


def test_every_page_is_private_unframable_and_not_sniffed():
    for name, page in ALL_PAGES.items():
        assert page.headers["Cache-Control"] == "no-store", name
        assert page.headers["Referrer-Policy"] == "same-origin", name
        assert page.headers["X-Content-Type-Options"] == "nosniff", name
        assert page.headers["X-Frame-Options"] == "DENY", name


def test_the_statuses():
    assert {name: page.status_code for name, page in ALL_PAGES.items()} == {
        "invitation": 200,
        "done": 200,
        "not configured": 503,
        "unavailable": 503,
        "provider unavailable": 502,
        "invalid invitation": 400,
        "identity": 409,
        "other user": 409,
        "login other user": 409,
        "not completed": 400,
        "stale": 400,
        "error": 500,
    }


def test_the_page_loads_nothing_and_has_no_form_to_submit():
    for name, page in ALL_PAGES.items():
        markup = re.sub(r"<script>.*?</script>|<style>.*?</style>", "", page.html, flags=re.DOTALL)
        assert not re.search(r"<(form|img|iframe|link|object|embed|audio|video|base)\b", markup), name
        assert not re.search(r"\b(src|srcset|action|formaction|data)\s*=", markup), name
        assert "http://" not in page.html, name
        assert "https://" not in markup, name  # (the script names Telegram's address, once)
        # the only link a page may carry goes to this site's own application
        assert re.findall(r"href=\"([^\"]*)\"", page.html) in ([], ["/"]), name


def test_every_page_is_in_russian_and_declares_it():
    for name, page in ALL_PAGES.items():
        assert '<html lang="ru">' in page.html, name
        assert re.search(r"[А-Яа-яЁё]{4,}", visible_text(page)), name
        assert not re.search(r"\b(invalid|error|failed|expired|invitation|unauthorized)\b", visible_text(page), re.I), name


def test_the_texts_name_no_user_no_identity_and_no_reason_behind_a_refusal():
    for name, page in ALL_PAGES.items():
        text = visible_text(page).lower()
        for leak in ("user_id", "allowlist", "subject", "белый список", "sub "):
            assert leak not in text, (name, leak)
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", page.html), name  # no id of anything


def test_what_a_page_says_about_the_invitation_is_the_same_whatever_is_behind_it():
    # one page for a used, a revoked, a superseded, an expired and an unknown invitation
    assert invite_pages.failure_page("invitation_not_usable").html == invite_pages.invalid_invitation_page().html
    assert "использован" in visible_text(invite_pages.invalid_invitation_page())
    assert invite_pages.failure_page("something we did not foresee").html == invite_pages.error_page().html


def test_each_failure_code_has_its_own_page_and_the_status_of_the_page():
    expected = {
        "invalid_login_request": "stale",
        "login_failed": "not completed",
        "provider_unavailable": "provider unavailable",
        "temporarily_unavailable": "unavailable",
        "invitation_not_usable": "invalid invitation",
        "identity_not_linkable": "identity",
        "signed_in_as_another_user": "other user",
        "server_error": "error",
    }
    for code, name in expected.items():
        assert invite_pages.failure_page(code).html == ALL_PAGES[name].html, code


def test_the_invitation_is_not_known_to_the_page_markup():
    # the same markup for everybody: nothing of an invitation is put into it
    assert invite_pages.invitation_page().html == INVITATION.html
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", INVITATION.html.split("<script>")[0])
    assert 'id="go"' in INVITATION.html and "disabled" in INVITATION.html  # nothing to press until the script says


# --- the script -------------------------------------------------------------------------------------


def test_the_script_reads_the_fragment_removes_it_and_sends_only_the_one_request():
    script = inline(INVITATION, "script")

    assert script.index("window.history.replaceState") < script.index("window.fetch")  # the bar first
    assert "window.location.hash" in script
    assert script.count("window.fetch(") == 1 and '"/auth/invite/start"' in script
    assert 'method: "POST"' in script and "JSON.stringify({ code: code })" in script
    assert 'credentials: "same-origin"' in script and 'redirect: "error"' in script
    for banned in ("XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "importScripts",
                   "eval", "Function(", "document.write", "innerHTML", "outerHTML", "insertAdjacent",
                   "localStorage", "sessionStorage", "indexedDB", "document.cookie", "location.href",
                   "location.replace", "window.open", "postMessage", "import(", "<script", "src =",
                   "mode:", "referrerPolicy", "?code", "&code", "+ code", "code +", "`"):
        assert banned not in script, banned
    # the one place the script navigates, and to what
    assert script.count("window.location.assign(") == 1
    assert 'var TELEGRAM = "https://oauth.telegram.org/auth?";' in script


def test_the_script_accepts_a_code_of_exactly_the_shape_the_server_issues():
    script = inline(INVITATION, "script")

    assert (
        r'/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[A-Za-z0-9_-]{43}$/' in script
    )  # persistence.invitations.parse_invitation_code: str(uuid4()) "." token_urlsafe(32)


NODE = shutil.which("node")
DRIVER = r"""
const script = require("fs").readFileSync(0, "utf8");
const spec = JSON.parse(process.env.SPEC);
const log = [];
const touched = [];
const button = {
  disabled: !spec.startsEnabled,
  listeners: {},
  addEventListener(type, handler) { this.listeners[type] = handler; },
};
const status = { hidden: true, textContent: "", className: "" };
const nodes = { go: button, status };
const answers = (spec.answers || []).slice();
const window = {
  location: {
    hash: spec.fragment, pathname: "/auth/invite", search: spec.search || "",
    assign(url) { log.push(["assign", url]); },
  },
  history: {
    replaceState(...args) {
      if (spec.replaceFails) { throw new Error("SecurityError"); }
      log.push(["replaceState", ...args]);
      if (!spec.replaceIgnored) { window.location.hash = ""; }  // what a browser does to the address
    },
  },
  fetch(url, init) {
    log.push(["fetch", url, init]);
    const answer = answers.shift();
    if (answer === undefined || answer.network) { return Promise.reject(new TypeError("network")); }
    return Promise.resolve({
      status: answer.status,
      json: () => (answer.notJson ? Promise.reject(new SyntaxError("not json")) : Promise.resolve(answer.body)),
    });
  },
  addEventListener(type, handler) { this["on_" + type] = handler; },
};
for (const name of ["localStorage", "sessionStorage", "indexedDB"]) {
  Object.defineProperty(window, name, { get() { touched.push(name); throw new Error(name + " touched"); } });
}
const document = {
  getElementById: (id) => nodes[id],
  get cookie() { touched.push("cookie"); throw new Error("cookie touched"); },
  set cookie(value) { touched.push("cookie"); throw new Error("cookie touched"); },
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 5));
(async () => {
  new Function("window", "document", script)(window, document);
  const snapshot = () => ({ disabled: button.disabled, text: status.textContent, hidden: status.hidden, cls: status.className });
  const frames = { start: snapshot(), logAfterLoad: log.slice() };
  for (const step of spec.steps || []) {
    if (step === "click") {
      if (button.listeners.click) { button.listeners.click(); }
      frames.duringRequest = snapshot();
      await settle();
    } else if (step === "click-twice") {
      button.listeners.click(); button.listeners.click();
      await settle();
    } else if (step === "pageshow") {
      if (window.on_pageshow) { window.on_pageshow({ persisted: true }); }
    } else if (step === "pageshow-plain") {
      if (window.on_pageshow) { window.on_pageshow({ persisted: false }); }
    }
    frames["after_" + step] = snapshot();
  }
  const listening = Object.keys(button.listeners).concat(
    Object.keys(window).filter((name) => name.startsWith("on_")));
  console.log(JSON.stringify({ end: snapshot(), frames, log, touched, listening, hash: window.location.hash }));
})();
"""


def run_script(**spec) -> dict:
    completed = subprocess.run(
        [NODE, "-e", DRIVER],
        input=inline(INVITATION, "script"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "SPEC": json.dumps(spec)},
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def fetches(result) -> list:
    return [entry for entry in result["log"] if entry[0] == "fetch"]


needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


@needs_node
def test_a_good_code_enables_the_button_clears_the_address_bar_and_sends_nothing_yet():
    result = run_script(fragment="#" + CODE, search="")

    assert result["frames"]["start"]["disabled"] is False
    assert result["frames"]["start"]["hidden"] is True
    assert ["replaceState", None, "", "/auth/invite"] in result["log"]
    assert CODE not in json.dumps([e for e in result["log"] if e[0] == "replaceState"]) and SECRET not in json.dumps(result["log"])
    assert fetches(result) == []  # opening the page uses nothing up and sends nothing
    assert result["touched"] == []


@needs_node
def test_the_query_of_the_page_is_kept_and_the_fragment_is_the_only_thing_removed():
    result = run_script(fragment="#" + CODE, search="?from=message")

    assert ["replaceState", None, "", "/auth/invite?from=message"] in result["log"]


@needs_node
def test_pressing_the_button_posts_the_code_in_a_json_body_to_this_origin_and_nowhere_else():
    result = run_script(
        fragment="#" + CODE,
        steps=["click"],
        answers=[{"status": 200, "body": {"authorization_url": TELEGRAM_URL}}],
    )

    (_, url, init), = fetches(result)
    assert url == "/auth/invite/start"  # relative: this origin, no query, no fragment
    assert init["method"] == "POST" and init["credentials"] == "same-origin"
    assert init["headers"] == {"Content-Type": "application/json"}
    assert json.loads(init["body"]) == {"code": CODE}
    assert init["redirect"] == "error" and init["cache"] == "no-store"
    assert set(init) == {"method", "headers", "body", "credentials", "cache", "redirect"}
    assert CODE not in url and SECRET not in url and "?" not in url and "#" not in url
    # the bar was cleared before the request, and then the page goes to Telegram
    order = [entry[0] for entry in result["log"]]
    assert order == ["replaceState", "fetch", "assign"]
    assert ["assign", TELEGRAM_URL] in result["log"]
    assert result["frames"]["duringRequest"]["disabled"] is True  # a double click cannot send it twice
    assert result["touched"] == []


@needs_node
def test_a_second_click_while_the_first_is_in_flight_sends_nothing():
    result = run_script(
        fragment="#" + CODE,
        steps=["click-twice"],
        answers=[{"status": 200, "body": {"authorization_url": TELEGRAM_URL}}],
    )

    assert len(fetches(result)) == 1


@needs_node
@pytest.mark.parametrize(
    "address",
    [
        "https://evil.example/auth?response_type=code",
        "http://oauth.telegram.org/auth?x=1",
        "https://oauth.telegram.org.evil.example/auth?x=1",
        "https://oauth.telegram.org/token?x=1",
        "javascript:alert(1)",
        "//oauth.telegram.org/auth?x=1",
        "",
        None,
        17,
    ],
)
def test_the_page_follows_only_an_address_at_telegram(address):
    result = run_script(
        fragment="#" + CODE,
        steps=["click"],
        answers=[{"status": 200, "body": {"authorization_url": address}}],
    )

    assert [entry for entry in result["log"] if entry[0] == "assign"] == []
    assert result["end"]["hidden"] is False and result["end"]["cls"] == "problem"
    assert result["end"]["disabled"] is False  # and the person may try again


@needs_node
@pytest.mark.parametrize("code", [400, 403])
def test_a_refusal_is_final_the_message_is_shown_and_the_code_is_forgotten(code):
    message = "Приглашение недействительно, уже использовано или его срок истёк. Попросите выдать новое."
    result = run_script(
        fragment="#" + CODE,
        steps=["click", "click"],
        answers=[{"status": code, "body": {"detail": "invitation_invalid", "message": message}}],
    )

    assert len(fetches(result)) == 1  # the second click has nothing to send
    assert result["end"]["text"] == message and result["end"]["cls"] == "problem"
    assert result["end"]["disabled"] is True


@needs_node
@pytest.mark.parametrize("code", [409, 500, 502, 503])
def test_a_trouble_that_may_pass_is_shown_and_the_person_may_press_again_with_the_same_code(code):
    result = run_script(
        fragment="#" + CODE,
        steps=["click", "click"],
        answers=[
            {"status": code, "body": {"detail": "x", "message": "Сервис временно недоступен."}},
            {"status": 200, "body": {"authorization_url": TELEGRAM_URL}},
        ],
    )

    first, second = fetches(result)
    assert first[2]["body"] == second[2]["body"] and json.loads(second[2]["body"]) == {"code": CODE}
    assert result["frames"]["after_click"]["text"] != ""
    assert ["assign", TELEGRAM_URL] in result["log"]


@needs_node
def test_a_lost_connection_is_said_in_russian_and_may_be_retried():
    result = run_script(fragment="#" + CODE, steps=["click"], answers=[{"network": True}])

    assert "Нет связи" in result["end"]["text"] and result["end"]["disabled"] is False


@needs_node
@pytest.mark.parametrize(
    "answer",
    [
        {"status": 502, "notJson": True},
        {"status": 500, "body": None},
        {"status": 500, "body": {"message": 12}},
        {"status": 500, "body": {"message": "x" * 501}},
        {"status": 500, "body": ["not", "an", "object"]},
    ],
)
def test_an_answer_that_is_not_ours_gets_a_fixed_russian_sentence(answer):
    result = run_script(fragment="#" + CODE, steps=["click"], answers=[answer])

    assert result["end"]["text"] == "Не получилось начать вход. Попробуйте ещё раз чуть позже."


@needs_node
def test_coming_back_from_telegram_by_the_back_button_lets_the_person_start_again():
    result = run_script(
        fragment="#" + CODE,
        steps=["click", "pageshow-plain", "pageshow", "click"],
        answers=[
            {"status": 200, "body": {"authorization_url": TELEGRAM_URL}},
            {"status": 200, "body": {"authorization_url": TELEGRAM_URL}},
        ],
    )

    assert result["frames"]["after_pageshow-plain"]["disabled"] is True  # a plain show changes nothing
    assert result["frames"]["after_pageshow"]["disabled"] is False  # a page restored from the cache does
    assert "не завершён" in result["frames"]["after_pageshow"]["text"]
    assert len(fetches(result)) == 2


@needs_node
@pytest.mark.parametrize(
    "fragment",
    [
        "",
        "#",
        "#" + INVITATION_ID,
        "#" + SECRET,
        "#" + CODE[:-1],
        "#" + CODE + "A",
        "#" + CODE + " ",
        "#" + CODE.upper(),
        "#" + INVITATION_ID.replace("-", "") + "." + SECRET,
        "#" + INVITATION_ID + "_" + SECRET,
        "#" + INVITATION_ID + "." + SECRET[:-1] + "=",
        "#<script>",
        "#" + "é" * 80,
        "##" + CODE,
        CODE,
    ],
)
def test_the_script_refuses_anything_else_and_shows_the_problem_and_the_button_stays_off(fragment):
    result = run_script(fragment=fragment, steps=["click"])

    assert result["frames"]["start"]["disabled"] is True and result["frames"]["start"]["hidden"] is False
    assert result["frames"]["start"]["cls"] == "problem" and "неполная или повреждена" in result["frames"]["start"]["text"]
    assert any(entry[0] == "replaceState" for entry in result["log"])  # even a bad one leaves the bar
    assert fetches(result) == []


REFUSAL = "Эта страница не может безопасно обработать приглашение"


def assert_closed(result) -> None:
    """The page is shut: nothing was sent, nowhere was navigated, nothing was kept, the button is off
    and stays off, nobody listens for it to be pressed or for the page to be shown again, and the
    person is told in Russian that the page cannot process the invitation safely."""
    assert fetches(result) == []
    assert [entry for entry in result["log"] if entry[0] == "assign"] == []
    assert result["touched"] == []  # no storage, no cookie
    assert result["listening"] == []  # no click handler and no pageshow handler were ever registered
    frames = [frame for name, frame in result["frames"].items() if name != "logAfterLoad"]
    for frame in [result["end"], *frames]:
        assert frame["disabled"] is True
        assert frame["hidden"] is False and frame["cls"] == "problem" and REFUSAL in frame["text"]
    # (everything the page did and showed; `hash` is the stand-in's address, which still holds the
    # fragment the browser would not let the page remove)
    said = json.dumps({name: value for name, value in result.items() if name != "hash"})
    assert SECRET not in said and CODE not in said


@needs_node
def test_a_browser_that_refuses_to_rewrite_the_address_gets_a_closed_page_and_nothing_is_sent():
    result = run_script(
        fragment="#" + CODE,
        replaceFails=True,
        steps=["click", "pageshow-plain", "pageshow", "click"],
        answers=[{"status": 200, "body": {"authorization_url": TELEGRAM_URL}}] * 2,
    )

    assert_closed(result)
    assert not any(entry[0] == "replaceState" for entry in result["log"])  # it threw before it logged


@needs_node
def test_a_browser_that_ignores_the_rewrite_and_leaves_the_fragment_gets_the_same_closed_page():
    result = run_script(
        fragment="#" + CODE,
        replaceIgnored=True,
        steps=["click", "pageshow", "click"],
        answers=[{"status": 200, "body": {"authorization_url": TELEGRAM_URL}}] * 2,
    )

    assert result["hash"] == "#" + CODE  # (the stand-in left it in the address, as the case says)
    assert any(entry[0] == "replaceState" for entry in result["log"])  # it did try
    assert_closed(result)


@needs_node
@pytest.mark.parametrize("fragment", ["#" + CODE, "#" + CODE + "A", "#nonsense"])
@pytest.mark.parametrize("failure", ["replaceFails", "replaceIgnored"])
def test_whatever_the_fragment_a_page_that_cannot_clear_it_is_closed(fragment, failure):
    # (a bad fragment too: a page that cannot be sure the address is clean takes no part)
    result = run_script(**{failure: True}, fragment=fragment, steps=["click", "pageshow"])

    assert_closed(result)


@needs_node
def test_a_page_that_cannot_rewrite_the_address_is_closed_even_when_there_was_nothing_in_it():
    result = run_script(replaceFails=True, fragment="", steps=["click"])

    assert_closed(result)


@needs_node
def test_a_page_restored_from_the_cache_after_a_failed_rewrite_stays_shut():
    # The page-cache restoration (pageshow with persisted) re-enables the button of a page that went to
    # Telegram and came back; a page that never got as far, because it could not clear the address, has
    # no such handler, so a restoration of it changes nothing and a press sends nothing.
    result = run_script(
        fragment="#" + CODE,
        replaceFails=True,
        steps=["pageshow", "click", "pageshow", "click"],
        answers=[{"status": 200, "body": {"authorization_url": TELEGRAM_URL}}] * 4,
    )

    assert_closed(result)


@needs_node
@pytest.mark.parametrize("failure", ["replaceFails", "replaceIgnored"])
def test_the_closed_page_switches_the_button_off_itself_and_not_only_by_its_markup(failure):
    # (the stand-in's button starts ENABLED here, as it would if the markup were ever changed)
    result = run_script(**{failure: True}, fragment="#" + CODE, startsEnabled=True, steps=["click"])

    assert_closed(result)
    assert result["frames"]["start"]["disabled"] is True


def test_the_script_checks_the_address_before_it_enables_listens_or_sends_anything():
    script = inline(INVITATION, "script")

    check = script.index("if (!removed)")
    assert script.index("window.history.replaceState") < script.index("window.location.hash === \"\"") < check
    assert check < script.index("PATTERN.test(code)") < script.index("button.disabled = false;")
    assert check < script.index("addEventListener") < script.index("window.fetch(")
    assert script.count("addEventListener(") == 2  # click and pageshow, both after the check
    assert 'removed = window.location.hash === "";' in script
    # the closed page forgets the code, keeps the button off, says why and stops
    closed = script[check : script.index("if (!PATTERN.test(code))")]
    assert 'code = "";' in closed and "button.disabled = true;" in closed and "return;" in closed
    assert "безопасно обработать" in closed
