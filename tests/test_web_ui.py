"""The page and what it relies on: the shell, the static files, the headers, and the real
API's answers in the shapes the page expects.

Requests go through httpx's ASGI transport into the real application (see
tests/web_support.py); no socket, no browser, nothing real is called. The page's own logic
is tested against a scripted server in tests/test_web_ui_logic.py (Node.js), against a real
server in tests/test_web_ui_integration.py, and its files are scanned in
tests/test_web_ui_assets.py. None of that is a browser: see the README for what was and was
not tried in one.

Here: what the server sends for "/" and "/static/..." (and what it never sends), the
sign-in and sign-out journey that ends on the page, the email request the page makes, and the
chat API as the page uses it -- creating, reading, paging, sending, replaying a key, and each
way a send can answer (busy, in progress, failed, superseded, unavailable, not signed in).
"""

import asyncio
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from house_helper.core.backend import BackendConnectionError
from house_helper.core.limits import MAX_INPUT_LENGTH
from house_helper.web import ui
from house_helper.web.app import create_app
from house_helper.web.settings import WebSettings
from tests.auth_support import CLIENT_ID, CLIENT_SECRET, ORIGIN, auth_app, logout_as, sign_in
from tests.chat_support import ScriptedBackend, advance, chat_app, error_of, post_headers
from tests.email_support import ADDRESS, FakeMailer, SENDER, email_app
from tests.web_support import TIMEOUT, serving, settings_for

STATIC = Path(ui.STATIC_DIR)
CORS = ("access-control-allow-origin", "access-control-allow-credentials", "access-control-allow-methods")


# --- reading the shell -----------------------------------------------------------------------------


class Shell(HTMLParser):
    """The facts about a page that matter here: what runs, what loads, what is typed into."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.inline_scripts: list[str] = []
        self.inline_styles: list[str] = []
        self.text: list[str] = []
        self._open: list[str] = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._open.append(tag)
        if tag == "script" and "src" not in dict(attrs):
            self.inline_scripts.append("")
        if tag == "style":
            self.inline_styles.append("")

    def handle_endtag(self, tag):
        if tag in self._open:
            while self._open and self._open.pop() != tag:
                pass

    def handle_data(self, data):
        if self._open and self._open[-1] == "script" and self.inline_scripts and "src" not in self.tags[-1][1]:
            self.inline_scripts[-1] += data
        elif self._open and self._open[-1] == "style":
            self.inline_styles[-1] += data
        elif data.strip():
            self.text.append(data.strip())

    def elements(self, name):
        return [attrs for tag, attrs in self.tags if tag == name]

    @property
    def ids(self) -> set[str]:
        return {attrs["id"] for _, attrs in self.tags if "id" in attrs}

    def by_id(self, identifier: str) -> dict:
        return next(attrs for _, attrs in self.tags if attrs.get("id") == identifier)


def csp(header: str) -> dict[str, list[str]]:
    directives = {}
    for part in header.split(";"):
        name, *sources = part.split()
        directives[name] = sources
    return directives


def shell_for(**flags) -> Shell:
    return Shell(ui.render_shell(**flags))


# --- the shell: what is in it -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_shell_is_a_russian_html_page_made_for_this_request(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await h.client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-language"] == "ru"
    page = Shell(response.text)
    assert page.by_id("screen-app") is not None
    html = response.text
    assert html.startswith("<!doctype html>") and '<html lang="ru">' in html
    assert '<meta charset="utf-8">' in html and "width=device-width" in html
    assert "<title>House Helper</title>" in html
    assert 'name="robots" content="noindex"' in html


@pytest.mark.parametrize(
    "telegram, email",
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_only_the_ways_of_signing_in_that_exist_are_drawn(telegram, email):
    page = shell_for(telegram=telegram, email=email)
    body = page.by_id("screen-login") and next(a for t, a in page.tags if t == "body")

    assert body["data-telegram"] == ("1" if telegram else "0")
    assert body["data-email"] == ("1" if email else "0")
    assert ("login-telegram" in page.ids) is telegram
    assert ("email-form" in page.ids and "email-input" in page.ids and "email-submit" in page.ids) is email
    assert ("login-none" in page.ids) is (not telegram and not email)
    if telegram:
        assert page.by_id("login-telegram")["href"] == "/auth/telegram/login"  # the existing full-page route


@pytest.mark.parametrize(
    "telegram, email",
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_the_first_visit_hint_is_drawn_beside_telegram_and_only_there(telegram, email):
    # R4C-Auth A3: a person who has not linked their Telegram yet is pointed at their invitation
    # link, which is the only way in for them; the hint is plain text (the page never builds or
    # knows an invitation) and exists only where the Telegram button does.
    html = ui.render_shell(telegram=telegram, email=email)

    hint = "Откройте ссылку-приглашение"
    assert (hint in html) is telegram
    assert "/auth/invite" not in html and "invitation" not in html.lower()  # no link, no code, no route
    if telegram:
        assert html.index("login-telegram") < html.index(hint)  # under the button, not above it


@pytest.mark.asyncio
async def test_the_flags_follow_what_the_running_application_has(tmp_path):
    (tmp_path / "telegram").mkdir()
    async with auth_app(tmp_path / "telegram") as h:
        telegram_only = Shell((await h.client.get("/")).text)
    (tmp_path / "email").mkdir()
    async with email_app(tmp_path / "email") as h:
        email_only = Shell((await h.client.get("/")).text)
    (tmp_path / "both").mkdir()
    async with email_app(tmp_path / "both", telegram=True) as h:
        both = Shell((await h.client.get("/")).text)
    app = create_app(settings_for(tmp_path / "none.db"))
    async with serving(app) as client:
        neither = Shell((await client.get("/")).text)

    flags = lambda page: (page.ids >= {"login-telegram"}, page.ids >= {"email-form"}, page.ids >= {"login-none"})
    assert flags(telegram_only) == (True, False, False)
    assert flags(email_only) == (False, True, False)
    assert flags(both) == (True, True, False)
    assert flags(neither) == (False, False, True)


@pytest.mark.asyncio
async def test_the_shell_holds_no_secret_and_no_deployment_detail(tmp_path):
    (tmp_path / "x").mkdir()
    async with email_app(tmp_path / "x", telegram=True, smtp_username="mailer", smtp_password="smtp-secret-9f8e7d") as h:
        text = (await h.client.get("/")).text

    for forbidden in (
        CLIENT_SECRET, CLIENT_ID, "smtp-secret-9f8e7d", "mailer", "smtp.example.test", SENDER, ADDRESS,
        "kitchen.example.test", "sqlite", "tmp", "OPENAI", "sk-", "hh_session", "client_secret",
    ):
        assert forbidden not in text, forbidden


def test_the_length_limit_comes_from_the_servers_setting():
    page = shell_for(telegram=True, email=False)
    body = next(a for t, a in page.tags if t == "body")

    assert body["data-max-length"] == str(MAX_INPUT_LENGTH) == "2000"
    assert next(a for t, a in shell_for(telegram=True, email=False, max_length=15).tags if t == "body")["data-max-length"] == "15"


def test_the_shell_has_no_inline_script_or_style_and_no_handler_attribute():
    for flags in ({"telegram": True, "email": True}, {"telegram": False, "email": False}):
        page = shell_for(**flags)
        html = ui.render_shell(**flags)

        assert page.inline_scripts == [] and page.inline_styles == []
        assert [a["src"] for a in page.elements("script")] == ["/static/chat-logic.js", "/static/app.js"]
        assert [a["href"] for a in page.elements("link")] == ["/static/icon.svg", "/static/app.css"]
        handlers = [(tag, name) for tag, attrs in page.tags for name in attrs if name.startswith("on") or name == "style"]
        assert handlers == []
        assert "javascript:" not in html.lower() and "<iframe" not in html.lower() and "<base" not in html.lower()


def test_the_shell_names_nothing_outside_this_site_but_the_telegram_route_it_starts():
    page = shell_for(telegram=True, email=True)

    references = [
        attrs[name] for _, attrs in page.tags for name in ("src", "href", "action", "formaction") if name in attrs
    ]
    assert all(reference.startswith("/") and not reference.startswith("//") for reference in references), references
    assert "http://" not in ui.render_shell(telegram=True, email=True)
    assert "https://" not in ui.render_shell(telegram=True, email=True)
    # A form the browser could submit by itself would put what was typed into an address.
    assert all("action" not in attrs and "method" not in attrs for attrs in page.elements("form"))


def test_every_field_has_a_label_every_button_a_type_and_the_live_regions_exist():
    page = shell_for(telegram=True, email=True)
    labelled = {attrs.get("for") for attrs in page.elements("label")}

    for attrs in page.elements("input") + page.elements("textarea"):
        assert attrs["id"] in labelled, attrs
    assert all(attrs.get("type") in ("button", "submit") for attrs in page.elements("button"))
    assert page.by_id("announcer")["aria-live"] == "polite" and page.by_id("announcer")["role"] == "status"
    assert page.by_id("alerter")["role"] == "alert"
    assert page.by_id("composer-error")["role"] == "alert"
    assert page.by_id("messages")["role"] == "log"
    assert page.by_id("drawer-toggle")["aria-controls"] == "sidebar" and page.by_id("drawer-toggle")["aria-expanded"] == "false"
    assert page.by_id("email-input")["type"] == "email" and page.by_id("email-input")["autocomplete"] == "email"
    headings = [attrs.get("id") for tag, attrs in page.tags if tag == "h1"]  # (the <noscript> one has none)
    assert {"login-title", "unavailable-title", "chat-title"} <= set(headings)


def test_all_the_text_a_person_reads_is_russian():
    import re

    page = shell_for(telegram=True, email=True)
    words = {word for line in page.text for word in re.findall(r"[A-Za-z][A-Za-z+]*", line)}

    assert words <= {"Telegram", "Ctrl+Enter", "JavaScript", "House", "Helper"}, words  # names, written as everyone writes them
    assert any("House Helper" in line for line in page.text)
    for tag, attrs in page.tags:
        for name in ("placeholder", "aria-label", "title"):
            if name in attrs:
                assert not re.search(r"[A-Za-z]{3,}", attrs[name]), (name, attrs[name])


SETUP_TEXT = "Вход пока не подключен. Мы готовим тестовую версию House Helper."


@pytest.mark.parametrize(
    "telegram, email",
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_the_product_is_house_helper_everywhere_the_page_names_itself_and_the_kitchen_is_only_described(telegram, email):
    import re

    html = ui.render_shell(telegram=telegram, email=email)

    headings = [h.strip() for h in re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.S) if h.strip()]
    assert headings and set(headings) == {"House Helper"}, headings  # <noscript>, failure, setup and login screens
    assert "<title>House Helper</title>" in html
    assert '<p class="brand">House Helper</p>' in html
    assert "Кухонный помощник" not in html and "Кухонного помощника" not in html and "Kitchen Helper" not in html
    # What the assistant does today is still said: the kitchen is the first module, not the name of the product.
    assert "Помощник по домашней кухне" in html


def test_the_icon_is_named_for_the_product():
    icon = (STATIC / "icon.svg").read_text(encoding="utf-8")

    assert 'aria-label="House Helper"' in icon and "Кухонный" not in icon


def test_the_setup_screen_is_a_calm_hidden_notice_with_nothing_to_press():
    import re

    html = ui.render_shell(telegram=False, email=False)
    page = Shell(html)

    assert "hidden" in page.by_id("screen-setup")  # the script shows it; nothing else does
    assert page.by_id("screen-setup")["aria-labelledby"] == "setup-title"
    assert page.by_id("setup-title").get("tabindex") == "-1"  # (the script moves focus to it, as for the other screens)
    assert page.by_id("setup-text")["class"] == "notice"  # not "notice-error"
    assert "role" not in page.by_id("setup-text")  # not an alert: nothing is wrong
    assert SETUP_TEXT in page.text
    section = re.search(r'<section id="screen-setup".*?</section>', html, re.S).group(0)
    assert "<button" not in section and "<a " not in section and "<form" not in section
    # Said in Russian, in the words the product asked for, and with no code, address or detail of the deployment.
    for forbidden in ("login_unavailable", "503", "Telegram", "SMTP", "OIDC", "client", "secret", "OPENAI", "env"):
        assert forbidden not in section, forbidden


@pytest.mark.parametrize(
    "telegram, email",
    [(True, False), (False, True), (True, True), (False, False)],
)
def test_the_setup_screen_does_not_depend_on_which_sign_in_choices_are_drawn(telegram, email):
    page = shell_for(telegram=telegram, email=email)

    assert "screen-setup" in page.ids and "setup-title" in page.ids and "setup-text" in page.ids
    assert "hidden" in page.by_id("screen-setup")


@pytest.mark.asyncio
async def test_a_deployment_without_login_serves_the_page_with_no_choice_and_answers_the_page_503_login_unavailable(tmp_path):
    app = create_app(settings_for(tmp_path / "off.db"))
    async with serving(app) as client:
        page = await client.get("/")
        me = await client.get("/auth/me")

    shell = Shell(page.text)
    body = next(attrs for tag, attrs in shell.tags if tag == "body")
    assert page.status_code == 200 and SETUP_TEXT in shell.text
    assert (body["data-telegram"], body["data-email"]) == ("0", "0")
    assert "login-telegram" not in shell.ids and "email-form" not in shell.ids  # no button leads to a 503
    # The one answer the page reads as "login is not connected" -- and it gives nobody a session.
    assert me.status_code == 503 and me.json() == {"detail": "login_unavailable"}
    assert "set-cookie" not in me.headers
    assert "login_unavailable" not in page.text  # the code is for the script, never for a person


# --- headers ------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_page_is_served_under_a_restrictive_policy_and_may_not_be_framed_or_cached(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await h.client.get("/")

    policy = csp(response.headers["content-security-policy"])
    assert policy == {
        "default-src": ["'none'"],
        "script-src": ["'self'"],
        "style-src": ["'self'"],
        "img-src": ["'self'"],
        "connect-src": ["'self'"],
        "form-action": ["'none'"],
        "base-uri": ["'none'"],
        "frame-ancestors": ["'none'"],
    }
    assert "'unsafe-inline'" not in response.headers["content-security-policy"]
    assert "'unsafe-eval'" not in response.headers["content-security-policy"]
    assert "data:" not in response.headers["content-security-policy"] and "http" not in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert "microphone=()" in response.headers["permissions-policy"]


@pytest.mark.asyncio
async def test_no_cors_header_is_ever_sent_whoever_asks(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        foreign = {"Origin": "https://evil.example.test"}
        responses = [
            await chat.client.get("/", headers=foreign),
            await chat.client.get("/static/app.js", headers=foreign),
            await chat.client.get("/auth/me", headers=foreign),
            await chat.client.get("/api/conversations", headers=foreign),
            await chat.client.options("/api/conversations", headers={**foreign, "Access-Control-Request-Method": "POST"}),
            await chat.client.options("/", headers={**foreign, "Access-Control-Request-Method": "GET"}),
            await chat.client.post("/api/conversations", json={}, headers=foreign),
        ]

    for response in responses:
        assert not any(name in response.headers for name in CORS), response.request.url
    assert responses[-1].status_code == 403  # the foreign Origin is still refused: nothing was relaxed
    assert error_of(responses[-1])["detail"] == "forbidden_origin"


@pytest.mark.asyncio
async def test_the_page_and_its_files_are_public_and_the_same_for_everyone(tmp_path):
    async with auth_app(tmp_path) as h:
        anonymous = await h.client.get("/")
        assert "set-cookie" not in anonymous.headers and "www-authenticate" not in anonymous.headers
        for name in ui.ASSETS:
            assert (await h.client.get(f"/static/{name}")).status_code == 200
        await sign_in(h.client, h.telegram)
        signed_in = await h.client.get("/")

    assert signed_in.text == anonymous.text  # nothing of a person's is in it


# --- the static files ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, media",
    [
        ("app.css", "text/css; charset=utf-8"),
        ("chat-logic.js", "text/javascript; charset=utf-8"),
        ("app.js", "text/javascript; charset=utf-8"),
        ("icon.svg", "image/svg+xml"),
    ],
)
async def test_each_static_file_is_served_as_the_file_with_its_type_and_an_etag(tmp_path, name, media):
    async with auth_app(tmp_path) as h:
        response = await h.client.get(f"/static/{name}")
        etag = response.headers["etag"]
        unchanged = await h.client.get(f"/static/{name}", headers={"If-None-Match": etag})
        weak = await h.client.get(f"/static/{name}", headers={"If-None-Match": f"W/{etag}"})
        listed = await h.client.get(f"/static/{name}", headers={"If-None-Match": f'"other", {etag}'})
        stale = await h.client.get(f"/static/{name}", headers={"If-None-Match": '"stale"'})

    assert response.status_code == 200
    assert response.headers["content-type"] == media
    assert response.content == (STATIC / name).read_bytes()
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert [r.status_code for r in (unchanged, weak, listed, stale)] == [304, 304, 304, 200]
    assert unchanged.content == b"" and unchanged.headers["etag"] == etag
    assert not any(name in response.headers for name in CORS)


@pytest.mark.asyncio
async def test_only_the_listed_static_files_exist_and_no_path_leads_anywhere_else(tmp_path):
    paths = [
        "/static/nope.js", "/static/", "/static", "/static//app.js", "/static/APP.JS", "/static/app.js.map",
        "/static/.env", "/static/__init__.py", "/static/..%2fauth.py", "/static/%2e%2e/auth.py", "/static/..%5cauth.py",
        "/static/app.js%00", "/static/app.js%2f..%2f..%2fauth.py", "/static/..%2f..%2fREADME.md",
        "/static/%2e%2e%2f%2e%2e%2f.env", "/static/chat-logic.js?x=../../auth.py",
    ]
    async with auth_app(tmp_path) as h:
        for path in paths:
            response = await h.client.get(path)
            if path.startswith("/static/chat-logic.js?"):
                assert response.content == (STATIC / "chat-logic.js").read_bytes()  # a query changes nothing
            else:
                assert response.status_code == 404, path
                assert b"Kitchen" not in response.content and b"import " not in response.content, path

        # A trailing slash is turned into the same file by the framework, or refused; it leads nowhere else.
        slashed = await h.client.get("/static/app.js/")
        assert slashed.status_code == 404 or (
            slashed.status_code == 307 and slashed.headers["location"].endswith("/static/app.js") and slashed.content == b""
        )

    assert set(ui.ASSETS) == {path.name for path in STATIC.iterdir()}  # nothing else sits in the folder


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("path", ["/", "/static/app.js"])
async def test_the_page_and_its_files_only_answer_get(tmp_path, method, path):
    async with auth_app(tmp_path) as h:
        response = await getattr(h.client, method)(path, headers={"Origin": ORIGIN})

    assert response.status_code == 405


# --- signing in and out, ending on the page ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signing_in_by_telegram_lands_on_the_page_which_then_finds_the_user(tmp_path):
    async with auth_app(tmp_path) as h:
        before = await h.client.get("/auth/me")
        callback = await sign_in(h.client, h.telegram)
        page = await h.client.get(callback.headers["location"])
        me = await h.client.get("/auth/me")

    assert before.status_code == 401
    assert callback.status_code == 303 and callback.headers["location"] == "/"
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert me.status_code == 200 and me.json()["user_id"] == str(h.user_id)
    assert set(me.json()) == {"user_id", "session_expires_at"}


@pytest.mark.asyncio
async def test_signing_in_by_email_lands_on_the_page_too(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()
        confirmed = await h.confirm(link)
        page = await h.client.get(confirmed.headers["location"])
        me = await h.client.get("/auth/me")

    assert confirmed.status_code == 303 and confirmed.headers["location"] == "/"
    assert page.status_code == 200 and me.status_code == 200


@pytest.mark.asyncio
async def test_logout_ends_the_session_and_the_page_stays_public(tmp_path):
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        assert (await h.client.get("/auth/me")).status_code == 200

        out = await h.client.post("/auth/logout", headers=logout_as(h.user_id, Origin=ORIGIN))
        after = await h.client.get("/auth/me")
        page = await h.client.get("/")

    assert out.status_code == 204
    assert after.status_code == 401
    assert page.status_code == 200


@pytest.mark.asyncio
async def test_logout_from_another_site_is_still_refused(tmp_path):
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        refused = await h.client.post("/auth/logout", headers=logout_as(h.user_id, Origin="https://evil.example.test"))
        null_origin = await h.client.post("/auth/logout", headers=logout_as(h.user_id, Origin="null"))

        assert refused.status_code == 403 and refused.json() == {"detail": "forbidden_origin"}
        assert null_origin.status_code == 403
        assert (await h.client.get("/auth/me")).status_code == 200


# --- the answers the page tells apart ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_signed_in_is_a_401_with_a_code_and_the_auth_routes_have_no_correlation_id(tmp_path):
    async with auth_app(tmp_path) as h:
        me = await h.client.get("/auth/me")
        listing = await h.client.get("/api/conversations")

    assert me.status_code == 401 and me.json() == {"detail": "not_authenticated"}
    assert "x-correlation-id" not in me.headers  # the page shows none for /auth/*: there is none to show
    assert listing.status_code == 401
    body = error_of(listing)  # the API's errors have one, in the body and the header
    assert body["detail"] == "not_authenticated" and body["correlation_id"].startswith("hh-")


@pytest.mark.asyncio
async def test_no_login_configured_is_a_503_the_page_does_not_take_for_signed_out(tmp_path):
    app = create_app(settings_for(tmp_path / "off.db"))
    async with serving(app) as client:
        me = await client.get("/auth/me")

    assert me.status_code == 503 and me.json() == {"detail": "login_unavailable"}


# --- the email request the page makes -------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_email_request_keeps_the_local_part_and_answers_every_address_alike(tmp_path):
    mailer = FakeMailer()
    async with email_app(tmp_path, mailer=mailer) as h:
        from tests.email_support import seed_email_user

        seed_email_user(h.factory, address="Cook@example.com")  # case matters in a local part
        known = await h.ask("Cook@EXAMPLE.com")  # the page sends what was typed; the server lower-cases the domain only
        lowered = await h.ask("cook@example.com")  # what a page that lower-cased everything would have sent
        stranger = await h.ask("stranger@example.org")

    assert [r.status_code for r in (known, lowered, stranger)] == [202, 202, 202]
    assert known.json() == lowered.json() == stranger.json()  # nothing tells the allowlist apart
    assert known.json()["detail"] == "link_requested"
    assert "в списке разрешённых" in known.json()["message"] and "Если" in known.json()["message"]  # neutral wording
    assert [m.to for m in mailer.sent] == ["Cook@example.com"]  # only the exact local part was admitted
    for response in (known, lowered, stranger):
        assert "x-correlation-id" not in response.headers and "correlation_id" not in response.json()


@pytest.mark.asyncio
async def test_the_email_request_errors_carry_a_code_and_a_message_and_no_correlation_id(tmp_path):
    async with email_app(tmp_path) as h:
        invalid = await h.client.post("/auth/email/request", json={"email": "not an address"}, headers={"Origin": ORIGIN})
        malformed = await h.client.post("/auth/email/request", json={"mail": "a@b.co"}, headers={"Origin": ORIGIN})
        foreign = await h.client.post("/auth/email/request", json={"email": ADDRESS}, headers={"Origin": "https://evil.example.test"})

    assert (invalid.status_code, invalid.json()["detail"]) == (400, "invalid_email")
    assert (malformed.status_code, malformed.json()["detail"]) == (400, "invalid_request")
    assert (foreign.status_code, foreign.json()["detail"]) == (403, "forbidden_origin")
    for response in (invalid, malformed, foreign):
        assert set(response.json()) == {"detail", "message"}
        assert "x-correlation-id" not in response.headers


@pytest.mark.asyncio
async def test_without_email_login_the_request_is_a_503_with_a_code_only(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await h.client.post("/auth/email/request", json={"email": ADDRESS}, headers={"Origin": ORIGIN})

    assert response.status_code == 503 and response.json() == {"detail": "login_unavailable"}


# --- the chat API as the page uses it ---------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_journey_of_the_page_create_send_list_read(tmp_path):
    backend = ScriptedBackend("Борщ варят так.")
    async with chat_app(tmp_path, backend) as chat:
        empty = await chat.client.get("/api/conversations")
        created = await chat.client.post("/api/conversations", json={}, headers=post_headers())
        conversation = created.json()["id"]
        sent = await chat.send(conversation, "Как варить борщ?", "web-0123456789abcdef0123456789abcdef")
        listing = await chat.client.get("/api/conversations")
        history = await chat.history(conversation)

    assert empty.json() == {"conversations": [], "has_more": False}
    assert created.status_code == 201
    assert set(created.json()) == {"id", "title", "created_at", "updated_at"} and created.json()["title"] is None
    assert sent.status_code == 201
    body = sent.json()
    assert set(body) == {"conversation_id", "user_message", "assistant_message", "truncated", "replayed"}
    assert body["replayed"] is False and body["truncated"] is False and body["conversation_id"] == conversation
    assert listing.json()["conversations"][0]["id"] == conversation and listing.json()["conversations"][0]["title"] is None
    page = history.json()
    assert page["conversation_id"] == conversation and page["has_more"] is False and page["next_before"] is None
    assert [(m["role"], m["text"], m["status"]) for m in page["messages"]] == [
        ("user", "Как варить борщ?", "completed"), ("assistant", "Борщ варят так.", "completed"),
    ]
    assert [m["id"] for m in page["messages"]] == [body["user_message"]["id"], body["assistant_message"]["id"]]
    assert all(set(m) == {"id", "role", "text", "created_at", "status"} for m in page["messages"])  # no key once done
    assert all(m["created_at"].endswith("+00:00") for m in page["messages"])  # a time with its offset


@pytest.mark.asyncio
async def test_paging_walks_back_through_a_history_without_a_gap_or_a_repeat(tmp_path):
    backend = ScriptedBackend(default="ответ")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        for index in range(6):  # twelve messages
            assert (await chat.send(conversation, f"вопрос {index}", f"k-{index}")).status_code == 201

        newest = (await chat.history(conversation, limit=5)).json()
        pages = [newest]
        while pages[-1]["has_more"]:
            before = pages[-1]["next_before"]
            assert before == pages[-1]["messages"][0]["id"]  # the cursor is the oldest id shown
            pages.append((await chat.history(conversation, limit=5, before=before)).json())
        everything = (await chat.history(conversation, limit=100)).json()
        exact = (await chat.history(conversation, limit=12)).json()
        invalid = await chat.history(conversation, before="abc")

    ids = [m["id"] for page in reversed(pages) for m in page["messages"]]
    assert ids == [m["id"] for m in everything["messages"]] and ids == sorted(set(ids)) and len(ids) == 12
    assert [len(p["messages"]) for p in pages] == [5, 5, 2]
    assert [p["has_more"] for p in pages] == [True, True, False] and pages[-1]["next_before"] is None
    assert everything["has_more"] is False
    assert exact["has_more"] is False and len(exact["messages"]) == 12  # exactly a page is not "more"
    assert invalid.status_code == 400 and error_of(invalid)["detail"] == "invalid_query"


@pytest.mark.asyncio
async def test_the_same_key_and_text_again_is_a_replay_never_a_second_answer(tmp_path):
    backend = ScriptedBackend("Первый ответ.", "Второй ответ.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        first = await chat.send(conversation, "Что из яиц?", "k-1")
        lost = await chat.history(conversation)  # a client that lost the answer reads the history: both are there
        replay = await chat.send(conversation, "Что из яиц?", "k-1")
        different = await chat.send(conversation, "А из риса?", "k-1")

    assert first.status_code == 201 and backend.calls == 1
    assert [m["role"] for m in lost.json()["messages"]] == ["user", "assistant"]
    assert replay.status_code == 200 and replay.headers["idempotent-replayed"] == "true"
    assert replay.json() == {**first.json(), "replayed": True}
    assert different.status_code == 409
    assert error_of(different)["detail"] == "idempotency_key_reused" and error_of(different)["retryable"] is False
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_the_limit_counts_code_points_and_the_page_may_send_what_javascript_calls_4000(tmp_path):
    backend = ScriptedBackend(default="ок")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        emoji = "😀" * MAX_INPUT_LENGTH  # 2000 code points, 4000 UTF-16 units, 8000 bytes
        accepted = await chat.send(conversation, emoji, "k-1")
        too_long = await chat.send(conversation, emoji + "😀", "k-2")
        # The same text as JSON escapes of surrogate pairs (what some clients send): still 2000
        # code points, under a new key so it is a new message and not a replay.
        escaped = await chat.client.post(
            f"/api/conversations/{conversation}/messages",
            content='{"text": "' + "\\ud83d\\ude00" * MAX_INPUT_LENGTH + '"}',
            headers={**post_headers("k-3"), "Content-Type": "application/json"},
        )
        lone = await chat.client.post(
            f"/api/conversations/{conversation}/messages",
            content='{"text": "a\\ud83db"}',
            headers={**post_headers("k-4"), "Content-Type": "application/json"},
        )
        history = (await chat.history(conversation)).json()

    assert accepted.status_code == 201
    assert history["messages"][0]["text"] == emoji  # it comes back exactly
    assert too_long.status_code == 400 and error_of(too_long)["detail"] == "message_too_long"
    assert "2000" in error_of(too_long)["message"]
    assert escaped.status_code == 201
    assert lone.status_code == 400 and error_of(lone)["detail"] == "invalid_message_text"
    assert backend.calls == 2  # the refused ones never reached the assistant


@pytest.mark.asyncio
async def test_a_message_that_is_being_answered_is_visible_with_its_key_and_blocks_others_with_retry_after(tmp_path):
    backend = ScriptedBackend("Ответ.", "Другой ответ.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        first = asyncio.ensure_future(chat.send(conversation, "Медленный вопрос", "k-slow"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        in_progress = (await chat.history(conversation)).json()["messages"]
        busy = await chat.send(conversation, "Другой вопрос", "k-other")
        own = await chat.send(conversation, "Медленный вопрос", "k-slow")
        release.set()
        original = await first
        again = await chat.send(conversation, "Другой вопрос", "k-other")

    assert [(m["role"], m["status"]) for m in in_progress] == [("user", "in_progress")]
    assert in_progress[0]["idempotency_key"] == "k-slow"  # what the page needs to recognize its own message
    assert busy.status_code == 409 and busy.headers["retry-after"] == "5"
    assert (error_of(busy)["detail"], error_of(busy)["retryable"], error_of(busy)["retry_after_seconds"]) == ("conversation_busy", True, 5)
    assert own.status_code == 409 and error_of(own)["detail"] == "turn_in_progress" and own.headers["retry-after"] == "5"
    assert original.status_code == 201 and again.status_code == 201


@pytest.mark.asyncio
async def test_a_failed_turn_is_kept_with_its_key_and_code_and_the_same_key_tries_again(tmp_path):
    backend = ScriptedBackend(BackendConnectionError("down"), "Теперь ответ.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        failed = await chat.send(conversation, "Что из яиц?", "k-1")
        history = (await chat.history(conversation)).json()["messages"]
        retried = await chat.send(conversation, "Что из яиц?", "k-1")
        after = (await chat.history(conversation)).json()["messages"]

    assert failed.status_code == 502
    body = error_of(failed)
    assert (body["detail"], body["retryable"]) == ("provider_unreachable", True)
    assert isinstance(body["user_message_id"], int) and body["correlation_id"] == failed.headers["x-correlation-id"]
    assert [(m["role"], m["status"]) for m in history] == [("user", "failed")]
    assert (history[0]["idempotency_key"], history[0]["error_code"]) == ("k-1", "provider_unreachable")
    assert retried.status_code == 201 and backend.calls == 2
    assert [(m["role"], m["status"]) for m in after] == [("user", "completed"), ("assistant", "completed")]
    assert all("idempotency_key" not in m and "error_code" not in m for m in after)


@pytest.mark.asyncio
async def test_a_failed_message_cannot_be_retried_once_the_conversation_has_moved_on(tmp_path):
    backend = ScriptedBackend(BackendConnectionError("down"), "Ответ на новое.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        assert (await chat.send(conversation, "Старое", "k-old")).status_code == 502
        assert (await chat.send(conversation, "Новое", "k-new")).status_code == 201

        stale = await chat.send(conversation, "Старое", "k-old")
        as_new = await chat.send(conversation, "Старое", "k-fresh")

    assert stale.status_code == 409
    assert (error_of(stale)["detail"], error_of(stale)["retryable"]) == ("turn_superseded", False)
    assert as_new.status_code == 201  # a fresh key is a new message: the person's choice, never the page's


@pytest.mark.asyncio
async def test_without_an_assistant_conversations_still_work_and_a_message_is_a_503_that_stores_nothing(tmp_path):
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        created = await h.client.post("/api/conversations", json={}, headers=post_headers())
        conversation = created.json()["id"]
        sent = await h.client.post(
            f"/api/conversations/{conversation}/messages", json={"text": "Привет"}, headers=post_headers("k-1")
        )
        history = await h.client.get(f"/api/conversations/{conversation}/messages")

    assert created.status_code == 201
    assert sent.status_code == 503
    body = error_of(sent)
    assert (body["detail"], body["retryable"]) == ("chat_unavailable", False)
    assert history.json()["messages"] == []


@pytest.mark.asyncio
async def test_a_conversation_that_is_not_the_users_is_not_found_whoever_asks(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        mine = await chat.create()
        other = await chat.second_user()
        theirs = await other.get(f"/api/conversations/{mine}/messages")
        unknown = await chat.history("00000000-0000-4000-8000-000000000000")
        malformed = await chat.history("not-a-uuid")
        sent = await other.post(
            f"/api/conversations/{mine}/messages", json={"text": "Привет"}, headers=post_headers("k-1")
        )

    for response in (theirs, unknown, malformed, sent):
        assert response.status_code == 404 and error_of(response)["detail"] == "conversation_not_found"
    assert backend.calls == 0


@pytest.mark.asyncio
async def test_the_list_is_the_users_own_newest_activity_first(tmp_path):
    backend = ScriptedBackend(default="ответ")
    async with chat_app(tmp_path, backend) as chat:
        # (The clock is the test's own and does not move by itself: equal times would be ordered
        # by a random id, so each step is a moment later than the one before.)
        first = await chat.create()
        advance(chat, seconds=1)
        second = await chat.create()
        advance(chat, seconds=1)
        await chat.send(first, "Раньше", "k-1")  # moves the older conversation to the top
        other = await chat.second_user()
        theirs = await other.get("/api/conversations")
        mine = (await chat.client.get("/api/conversations")).json()

    assert [c["id"] for c in mine["conversations"]] == [first, second]
    assert theirs.json() == {"conversations": [], "has_more": False}
