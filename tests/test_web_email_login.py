"""Email sign-in over HTTP: asking for a link, opening it, confirming it.

The real application on a real (migrated) SQLite file, driven through its ASGI
interface; the mail goes to a recording stand-in (tests/email_support.py), so no
message is sent and no mail server is contacted.

The properties that matter, each pinned below: asking tells the asker nothing about
who is registered; a link is mailed only to an address on the allowlist, for the user
that entry names, and creates no user and no allowlist entry; opening the link uses
nothing up and shows the address masked; confirming is a POST, from this site's own
page, that uses the link up exactly once; the link works on another device than the one
that asked; a browser that is signed in as somebody else is not switched; the session
that results records the email identity and is ended with it; and the cookie is the
same secure one Telegram sign-in sets.
"""

import json
import re
from datetime import timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa

from house_helper.persistence.allowlist import allow, revoke
from house_helper.persistence.models import (
    AllowlistEntry,
    EmailLoginChallenge,
    IdentityLink,
    User,
)
from house_helper.persistence.models import Session as LoginSession
from house_helper.web import email_pages
from tests.auth_support import ORIGIN, START, cookie_attributes, rows, sign_in
from tests.email_support import ADDRESS, email_app, parse_link

SESSION_COOKIE = "__Host-hh_session"
OTHER_ADDRESS = "second@example.com"
EVIL = "https://evil.example"


def seed_second_user(h, address=OTHER_ADDRESS):
    with h.factory() as db, db.begin():
        user = User(display_name="Second")
        db.add(user)
        db.flush()
        allow(db, user, "email", address)
        return user.id


def user_count(h) -> int:
    return len(rows(h, User))


def state_of(h) -> dict:
    """The rows a sign-in could create or change, as numbers."""
    return {
        "users": len(rows(h, User)),
        "entries": len(rows(h, AllowlistEntry)),
        "links": len(rows(h, IdentityLink)),
        "sessions": len(rows(h, LoginSession)),
        "challenges": len(rows(h, EmailLoginChallenge)),
    }


def me_user(response) -> UUID:
    assert response.status_code == 200, response.text
    return UUID(response.json()["user_id"])


# --- asking for a link ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_allowlisted_address_is_mailed_a_link(tmp_path):
    async with email_app(tmp_path) as h:
        response = await h.ask()

        assert response.status_code == 202
        body = response.json()
        assert body["detail"] == "link_requested"
        assert "Если этот адрес есть в списке разрешённых" in body["message"]
        assert "Ссылка действует 10 минут" in body["message"]
        (mail,) = h.mailer.sent
        assert mail.to == ADDRESS and mail.subject == "Вход в Kitchen Helper"
        link = parse_link(h.mailer.last_url())
        assert link.url.startswith(ORIGIN + "/auth/email/confirm?c=")
        assert UUID(link.challenge_id) and re.fullmatch(r"[A-Za-z0-9_-]{43}", link.token)
        assert "Ссылка действует 10 минут" in mail.body


@pytest.mark.asyncio
async def test_the_link_is_made_from_the_configured_url_never_from_the_request(tmp_path):
    async with email_app(tmp_path) as h:
        await h.client.post(
            "/auth/email/request",
            json={"email": ADDRESS},
            headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http",
                     "Forwarded": "host=evil.example", "Origin": ORIGIN},
        )

        assert h.mailer.last_url().startswith(ORIGIN + "/auth/email/confirm?c=")
        assert "evil" not in h.mailer.sent[0].body


@pytest.mark.asyncio
async def test_the_answer_is_the_same_whoever_is_asked_about(tmp_path):
    async with email_app(tmp_path) as h:
        seed_second_user(h, "gone@example.com")
        with h.factory() as db, db.begin():
            revoke(db, "email", "gone@example.com")
        disabled = seed_second_user(h, "off@example.com")
        with h.factory() as db, db.begin():
            db.get(User, disabled).disabled_at = START
        cases = [
            ADDRESS,  # allowlisted: mailed
            ADDRESS,  # again at once: the cooldown
            " elena@Example.COM ",  # the same address, other spelling of the domain: the cooldown
            "ELENA@example.com",  # another case of the local part: another mailbox, not approved
            "Elena@Example.com",
            "nobody@example.com",  # unknown
            "elena+tag@example.com",  # an alias of an allowlisted address
            "gone@example.com",  # revoked
            "off@example.com",  # its user is disabled
            "elena@example.com.evil.test",
        ]

        answers = []
        for address in cases:
            response = await h.ask(address)
            answers.append((response.status_code, response.content, dict(response.headers)))

        assert len(set(map(repr, answers))) == 1, answers  # same status, same bytes, same headers
        assert answers[0][0] == 202
        assert len(h.mailer.sent) == 1 and h.mailer.sent[0].to == ADDRESS


@pytest.mark.asyncio
async def test_the_mail_goes_to_the_mailbox_as_approved_and_to_no_other_case_of_it(tmp_path):
    # The approved address is "Victim@example.com"; the person who controls the mailbox
    # "victim@example.com" (which may be somebody else's) asks for a link and gets the
    # same 202 as anybody and no mail, while the approved address is mailed exactly as
    # approved, whatever the case of the domain in the request.
    async with email_app(tmp_path, seed=False) as h:
        seed_second_user(h, "Victim@example.com")

        for lookalike in ("victim@example.com", "VICTIM@example.com", "vICTIM@example.com"):
            assert (await h.ask(lookalike)).status_code == 202
        assert h.mailer.sent == [] and h.mailer.attempts == 0
        assert state_of(h)["challenges"] == 0

        assert (await h.ask(" Victim@EXAMPLE.COM ")).status_code == 202
        assert [message.to for message in h.mailer.sent] == ["Victim@example.com"]
        assert state_of(h)["challenges"] == 1


@pytest.mark.asyncio
async def test_a_link_for_one_case_signs_in_that_identity_only(tmp_path):
    async with email_app(tmp_path, seed=False) as h:
        upper = seed_second_user(h, "Victim@example.com")
        lower = seed_second_user(h, "victim@example.com")

        link_lower = await h.ask_and_get_link("victim@example.com")
        assert [message.to for message in h.mailer.sent] == ["victim@example.com"]
        response = await h.confirm(link_lower)

        assert response.status_code == 303
        sessions = rows(h, LoginSession)
        assert [session.user_id for session in sessions] == [lower] and upper != lower
        assert [link.subject for link in rows(h, IdentityLink)] == ["victim@example.com"]


@pytest.mark.asyncio
async def test_asking_creates_no_user_no_allowlist_entry_and_no_link_for_a_stranger(tmp_path):
    async with email_app(tmp_path) as h:
        before = state_of(h)

        for stranger in ("nobody@example.com", "elena+tag@example.com", "ELENA@evil.test", "x@y.zz"):
            assert (await h.ask(stranger)).status_code == 202

        assert state_of(h) == before
        assert h.mailer.sent == [] and h.mailer.attempts == 0


@pytest.mark.asyncio
async def test_asking_again_within_the_cooldown_sends_nothing_and_after_it_sends_again(tmp_path):
    async with email_app(tmp_path) as h:
        first = await h.ask_and_get_link()

        h.clock.advance(seconds=59)
        assert (await h.ask()).status_code == 202
        assert len(h.mailer.sent) == 1  # said the same, sent nothing

        h.clock.advance(seconds=1)
        second = await h.ask_and_get_link()
        assert len(h.mailer.sent) == 2 and second.token != first.token
        assert (await h.confirm(first)).status_code == 303  # the first is still good (nobody was superseded)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "send, code",
    [
        (lambda c: c.post("/auth/email/request", content=b"not json", headers={"content-type": "application/json"}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", content=b"", headers={"content-type": "application/json"}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", content=b"[]", headers={"content-type": "application/json"}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", content=b'"x@y.zz"', headers={"content-type": "application/json"}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", json={}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", json={"mail": ADDRESS}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", json={"email": ADDRESS, "user_id": "1"}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", json={"email": 7}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", json={"email": None}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", json={"email": [ADDRESS]}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", content=b"[" * 5000, headers={"content-type": "application/json"}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", content=b'{"email":"' + b"a" * 3000 + b'@x.zz"}', headers={"content-type": "application/json"}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", content=f"email={ADDRESS}".encode(), headers={"content-type": "application/x-www-form-urlencoded"}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", content=json.dumps({"email": ADDRESS}).encode(), headers={"content-type": "text/plain"}), "invalid_request"),
        (lambda c: c.post("/auth/email/request", content=json.dumps({"email": ADDRESS}).encode()), "invalid_request"),
        (lambda c: c.post("/auth/email/request", json={"email": ""}), "invalid_email"),
        (lambda c: c.post("/auth/email/request", json={"email": "not an address"}), "invalid_email"),
        (lambda c: c.post("/auth/email/request", json={"email": "a@@b.co"}), "invalid_email"),
        (lambda c: c.post("/auth/email/request", json={"email": "élena@example.com"}), "invalid_email"),
        (lambda c: c.post("/auth/email/request", json={"email": ADDRESS + "\x00"}), "invalid_email"),
        (lambda c: c.post("/auth/email/request", json={"email": "x" * 400 + "@example.com"}), "invalid_email"),
    ],
)
async def test_a_request_that_is_not_an_address_in_json_is_refused_with_a_fixed_message(tmp_path, send, code):
    async with email_app(tmp_path) as h:
        response = await send(h.client)

        assert response.status_code == 400
        assert response.json()["detail"] == code
        assert response.json()["message"]  # a Russian sentence, the same for the same code
        assert re.search("[А-Яа-я]", response.json()["message"])
        assert h.mailer.sent == [] and state_of(h)["challenges"] == 0
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_a_refused_request_echoes_nothing_of_what_was_sent(tmp_path):
    async with email_app(tmp_path) as h:
        bodies = (
            {"email": "<script>alert(1)</script>"},
            {"email": "ECHO-MARKER-ADDRESS"},
            {"email": "x", "ECHO-MARKER-FIELD": "ECHO-MARKER-VALUE"},
        )
        for body in bodies:
            response = await h.client.post("/auth/email/request", json=body)

            assert response.status_code == 400
            for marker in ("script", "alert", "ECHO-MARKER", "<", "&lt;"):
                assert marker.lower() not in response.text.lower(), marker


@pytest.mark.asyncio
async def test_only_post_asks(tmp_path):
    async with email_app(tmp_path) as h:
        for method in ("get", "put", "delete", "patch", "options"):
            response = await getattr(h.client, method)("/auth/email/request")
            assert response.status_code == 405, method
        assert "access-control-allow-origin" not in (await h.client.options("/auth/email/request")).headers


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [EVIL, "null", "http://kitchen.example.test", "https://kitchen.example.test:8443",
                                    "https://kitchen.example.test.evil.example", "https://kitchen.example.test/"])
async def test_a_request_from_another_site_is_refused(tmp_path, origin):
    async with email_app(tmp_path) as h:
        response = await h.client.post("/auth/email/request", json={"email": ADDRESS}, headers={"Origin": origin})

        assert response.status_code == 403 and response.json()["detail"] == "forbidden_origin"
        assert h.mailer.sent == [] and state_of(h)["challenges"] == 0


@pytest.mark.asyncio
async def test_a_request_without_an_origin_is_not_a_browsers_and_passes(tmp_path):
    async with email_app(tmp_path) as h:
        response = await h.client.post("/auth/email/request", json={"email": ADDRESS})

        assert response.status_code == 202 and len(h.mailer.sent) == 1


@pytest.mark.asyncio
async def test_the_request_answer_is_not_cached_and_sets_no_cookie(tmp_path):
    async with email_app(tmp_path) as h:
        response = await h.ask()

        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "set-cookie" not in response.headers


# --- opening the link ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opening_the_link_shows_the_masked_address_and_a_button_and_uses_nothing_up(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()

        for _ in range(3):  # a mail scanner, a preview, the person
            page = await h.client.get(link.path)

            assert page.status_code == 200
            assert page.headers["content-type"] == "text/html; charset=utf-8"
            assert "e***@example.com" in page.text and ADDRESS not in page.text
            assert f'value="{link.challenge_id}"' in page.text
            assert link.token not in page.text  # the server never saw it
            assert "<script>" in page.text and "<form" in page.text
        assert rows(h, EmailLoginChallenge)[0].consumed_at is None
        assert state_of(h)["sessions"] == 0
        assert (await h.confirm(link)).status_code == 303  # still good


@pytest.mark.asyncio
async def test_the_page_comes_with_the_headers_that_make_it_safe(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()

        page = await h.client.get(link.path)

        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "same-origin"
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["x-frame-options"] == "DENY"
        expected = email_pages.confirm_page(link.challenge_id, "e***@example.com").headers
        assert page.headers["content-security-policy"] == expected["Content-Security-Policy"]
        assert "set-cookie" not in page.headers


class _Recorder:
    """Records every request line the application is given."""

    def __init__(self, app, seen):
        self.app, self.seen = app, seen

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            self.seen.append(scope["raw_path"].decode() + "?" + scope["query_string"].decode())
        await self.app(scope, receive, send)


@pytest.mark.asyncio
async def test_the_fragment_never_reaches_the_server(tmp_path):
    # What a browser does with "...?c=ID#SECRET": it requests the path and query only.
    seen: list[str] = []
    async with email_app(tmp_path, configure=lambda app: app.add_middleware(_Recorder, seen=seen)) as h:
        link = await h.ask_and_get_link()

        await h.client.get(link.url)  # httpx drops the fragment, as a browser does

        assert seen and all(link.token not in line and "#" not in line for line in seen)
        assert any(link.challenge_id in line for line in seen)


@pytest.mark.asyncio
async def test_every_link_that_cannot_be_used_gets_the_same_page(tmp_path):
    async with email_app(tmp_path) as h:
        seed_second_user(h, "second@example.com")
        seed_second_user(h, "third@example.com")
        unused = await h.ask_and_get_link()
        pages = {
            "unknown": await h.client.get(f"/auth/email/confirm?c={UUID(int=1)}"),
            "malformed": await h.client.get("/auth/email/confirm?c=not-an-id"),
            "missing": await h.client.get("/auth/email/confirm"),
            "empty": await h.client.get("/auth/email/confirm?c="),
            "upper case": await h.client.get(f"/auth/email/confirm?c={unused.challenge_id.upper()}"),
            "braces": await h.client.get("/auth/email/confirm?c={" + unused.challenge_id + "}"),
        }
        used = await h.ask_and_get_link("second@example.com")
        assert (await h.confirm(used, h.new_client())).status_code == 303
        pages["used"] = await h.client.get(used.path)
        disabled = await h.ask_and_get_link("third@example.com")
        with h.factory() as db, db.begin():
            db.execute(sa.update(User).where(User.display_name == "Second").values(disabled_at=START))
        pages["user disabled"] = await h.client.get(disabled.path)
        h.clock.advance(minutes=10)
        pages["expired"] = await h.client.get(unused.path)
        fresh = await h.ask_and_get_link()
        with h.factory() as db, db.begin():
            revoke(db, "email", ADDRESS, now=h.clock())
        pages["revoked"] = await h.client.get(fresh.path)
        with h.factory() as db, db.begin():
            allow(db, db.get(User, h.user_id), "email", ADDRESS)
        pages["revoked, then allowed again"] = await h.client.get(fresh.path)

        assert {name: page.status_code for name, page in pages.items()} == {name: 400 for name in pages}
        assert {page.text for page in pages.values()} == {email_pages.invalid_link_page().html}
        for page in pages.values():
            assert page.headers["content-security-policy"] == email_pages.invalid_link_page().headers["Content-Security-Policy"]
        assert "<script" not in pages["unknown"].text and "<form" not in pages["unknown"].text


@pytest.mark.asyncio
async def test_a_token_in_the_query_changes_nothing_and_is_never_echoed(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()

        page = await h.client.get(f"{link.path}&token={link.token}&c=other")

        assert link.token not in page.text
        assert state_of(h)["sessions"] == 0 and rows(h, EmailLoginChallenge)[0].consumed_at is None


@pytest.mark.asyncio
async def test_a_page_for_an_address_with_markup_in_it_is_escaped(tmp_path):
    from house_helper.persistence.email_login import issue_email_login

    async with email_app(tmp_path, seed=False) as h:
        with h.factory() as db, db.begin():
            user = User()
            db.add(user)
            db.flush()
            allow(db, user, "email", "<b>x</b>@example.com")  # printable ASCII, so the allowlist takes it
        with h.factory() as db, db.begin():
            challenge_id = issue_email_login(db, "<b>x</b>@example.com", now=START).link.challenge_id

        page = await h.client.get(f"/auth/email/confirm?c={challenge_id}")

        assert page.status_code == 200
        assert "&lt;***@example.com" in page.text and "<***@" not in page.text and "<b>" not in page.text
        h.clock.advance(minutes=2)
        assert (await h.ask("<b>x</b>@example.com")).status_code == 202
        assert h.mailer.sent == []  # an address like that is on the list, but is never mailed to


# --- confirming --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirming_signs_the_owner_in_and_redirects_to_me(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()
        before = state_of(h)

        response = await h.confirm(link)

        assert response.status_code == 303 and response.headers["location"] == "/auth/me"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        me = await h.client.get("/auth/me")
        assert me_user(me) == h.user_id
        after = state_of(h)
        assert after["users"] == before["users"] and after["entries"] == before["entries"]
        assert after["sessions"] == 1 and after["links"] == 1


@pytest.mark.asyncio
async def test_the_session_records_the_email_identity_it_came_through(tmp_path):
    async with email_app(tmp_path) as h:
        await h.sign_in()

        (session_row,) = rows(h, LoginSession)
        (link_row,) = rows(h, IdentityLink)
        assert session_row.identity_link_id == link_row.id
        assert (link_row.provider, link_row.subject, link_row.user_id) == ("email", ADDRESS, h.user_id)
        assert session_row.user_id == h.user_id
        assert link_row.last_login_at == START
        assert session_row.expires_at == START + timedelta(days=14)
        (challenge,) = rows(h, EmailLoginChallenge)
        assert challenge.consumed_at == START


@pytest.mark.asyncio
async def test_the_cookie_is_the_secure_session_cookie_and_nothing_else_is_set(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()

        response = await h.confirm(link)

        attributes = cookie_attributes(response, SESSION_COOKIE)
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", attributes["value"])
        assert attributes["httponly"] is True and attributes["secure"] is True
        assert attributes["samesite"].lower() == "lax" and attributes["path"] == "/"
        assert attributes["max-age"] == str(14 * 24 * 3600)
        assert "domain" not in attributes and "expires" not in attributes
        assert len(response.headers.get_list("set-cookie")) == 1
        assert attributes["value"] not in link.url and link.token not in attributes["value"]


@pytest.mark.asyncio
async def test_on_plain_http_for_localhost_the_cookie_loses_the_prefix_and_secure_as_telegram_does(tmp_path):
    async with email_app(tmp_path, origin="http://localhost:8000") as h:
        link = await h.ask_and_get_link()
        assert link.url.startswith("http://localhost:8000/auth/email/confirm?c=")

        response = await h.confirm(link)

        attributes = cookie_attributes(response, "hh_session")
        assert attributes["httponly"] is True and "secure" not in attributes
        assert (await h.client.get("/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_a_link_is_used_once(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()

        first = await h.confirm(link)
        again_here = await h.confirm(link)
        again_elsewhere = await h.confirm(link, h.new_client())

        assert first.status_code == 303
        for replay in (again_here, again_elsewhere):
            assert replay.status_code == 400
            assert replay.text == email_pages.invalid_link_page().html
            assert "set-cookie" not in replay.headers
        assert state_of(h)["sessions"] == 1


@pytest.mark.asyncio
async def test_a_link_ends_exactly_when_ten_minutes_have_passed(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()

        h.clock.advance(minutes=10, microseconds=-1)
        assert (await h.client.get(link.path)).status_code == 200  # the last instant
        h.clock.advance(microseconds=1)
        assert (await h.client.get(link.path)).status_code == 400
        assert (await h.confirm(link)).status_code == 400
        assert state_of(h)["sessions"] == 0


@pytest.mark.asyncio
async def test_a_wrong_secret_or_id_is_refused_and_does_not_spoil_the_link(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()
        other = seed_second_user(h)
        theirs = await h.ask_and_get_link(OTHER_ADDRESS)

        for attempt in (
            await h.confirm(link, token="A" * 43),
            await h.confirm(link, token=theirs.token),  # another link's secret
            await h.confirm(link, challenge_id=theirs.challenge_id),  # another link's id
            await h.confirm(link, challenge_id=str(UUID(int=7))),
            await h.confirm(link, token=link.token[:-1]),
            await h.confirm(link, token=""),
        ):
            assert attempt.status_code == 400 and "set-cookie" not in attempt.headers

        assert state_of(h)["sessions"] == 0
        assert (await h.confirm(link)).status_code == 303
        client = h.new_client()
        assert (await h.confirm(theirs, client)).status_code == 303
        assert me_user(await client.get("/auth/me")) == other  # and each opened its own owner's account


@pytest.mark.asyncio
async def test_the_link_works_on_another_device_than_the_one_that_asked(tmp_path):
    async with email_app(tmp_path) as h:
        phone = h.client
        laptop = h.new_client()
        await h.ask(client=phone)
        link = parse_link(h.mailer.last_url())

        page = await laptop.get(link.path)
        response = await h.confirm(link, laptop)

        assert page.status_code == 200 and response.status_code == 303
        assert me_user(await laptop.get("/auth/me")) == h.user_id
        assert (await phone.get("/auth/me")).status_code == 401  # nothing reached the asker's browser
        assert "set-cookie" not in (await h.ask(client=phone)).headers


# --- a browser that is already signed in ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_same_user_signing_in_again_gets_a_new_session_and_a_replaced_cookie(tmp_path):
    async with email_app(tmp_path) as h:
        await h.sign_in()
        old_cookie = h.client.cookies.get(SESSION_COOKIE)
        h.clock.advance(minutes=2)

        response = await h.sign_in()

        assert response.status_code == 303
        new_cookie = h.client.cookies.get(SESSION_COOKIE)
        assert new_cookie != old_cookie
        assert me_user(await h.client.get("/auth/me")) == h.user_id
        assert state_of(h)["sessions"] == 2 and state_of(h)["links"] == 1
        # the earlier session is not ended by this (as with Telegram): it is simply no
        # longer held by this browser
        other = h.new_client()
        other.cookies.set(SESSION_COOKIE, old_cookie, domain="kitchen.example.test", path="/")
        assert (await other.get("/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_a_browser_signed_in_as_somebody_else_is_not_switched_and_the_link_survives(tmp_path):
    async with email_app(tmp_path) as h:
        second = seed_second_user(h)
        await h.ask_and_get_link(OTHER_ADDRESS)  # (so that there is a second user's link to sign in with)
        second_link = parse_link(h.mailer.last_url())
        assert (await h.confirm(second_link)).status_code == 303
        assert me_user(await h.client.get("/auth/me")) == second
        h.clock.advance(minutes=2)
        elenas = await h.ask_and_get_link()

        refused = await h.confirm(elenas)

        assert refused.status_code == 409
        assert refused.text == email_pages.other_user_page().html
        assert "set-cookie" not in refused.headers
        assert me_user(await h.client.get("/auth/me")) == second  # still who they were
        for text in (refused.text, str(refused.headers)):
            assert str(second) not in text and str(h.user_id) not in text
            assert ADDRESS not in text and OTHER_ADDRESS not in text
        assert rows(h, EmailLoginChallenge)[-1].consumed_at is None  # not used up
        assert state_of(h)["sessions"] == 1

        # the explicit transition: sign out, then open the same link
        assert (await h.client.post("/auth/logout", headers={"Origin": ORIGIN})).status_code == 204
        accepted = await h.confirm(elenas)
        assert accepted.status_code == 303
        assert me_user(await h.client.get("/auth/me")) == h.user_id


@pytest.mark.asyncio
async def test_a_stale_cookie_does_not_count_as_being_signed_in(tmp_path):
    async with email_app(tmp_path, seed=True) as h:
        seed_second_user(h)
        link2 = await h.ask_and_get_link(OTHER_ADDRESS)
        assert (await h.confirm(link2)).status_code == 303
        h.clock.advance(days=15)  # the session has expired
        assert (await h.client.get("/auth/me")).status_code == 401

        elenas = await h.ask_and_get_link()

        assert (await h.confirm(elenas)).status_code == 303
        assert me_user(await h.client.get("/auth/me")) == h.user_id


@pytest.mark.asyncio
async def test_a_cookie_that_is_not_a_session_is_ignored(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()
        h.client.cookies.set(SESSION_COOKIE, "x" * 43, domain="kitchen.example.test", path="/")
        h.client.cookies.set(SESSION_COOKIE, "<script>", domain="kitchen.example.test", path="/")

        assert (await h.confirm(link)).status_code == 303


# --- confirming from the right place --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [EVIL, "null", "http://kitchen.example.test", "https://kitchen.example.test:8443"])
async def test_a_confirmation_from_another_site_is_refused_and_the_link_survives(tmp_path, origin):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()

        response = await h.confirm(link, headers={"Origin": origin})

        assert response.status_code == 403
        assert response.text == email_pages.forbidden_page().html
        assert "set-cookie" not in response.headers
        assert state_of(h)["sessions"] == 0
        assert (await h.confirm(link)).status_code == 303


@pytest.mark.asyncio
async def test_a_confirmation_without_an_origin_is_not_a_browsers_and_passes(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()

        response = await h.client.post(
            "/auth/email/confirm", data={"c": link.challenge_id, "token": link.token}
        )

        assert response.status_code == 303


FORM = "application/x-www-form-urlencoded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, content_type",
    [
        (lambda l: json.dumps({"c": l.challenge_id, "token": l.token}), "application/json"),
        (lambda l: f"c={l.challenge_id}&token={l.token}", "text/plain"),
        (lambda l: f"c={l.challenge_id}&token={l.token}", None),
        (lambda l: f"c={l.challenge_id}", FORM),
        (lambda l: f"token={l.token}", FORM),
        (lambda l: f"c={l.challenge_id}&token={l.token}&x=1", FORM),
        (lambda l: f"c={l.challenge_id}&c={l.challenge_id}&token={l.token}", FORM),
        (lambda l: f"c={l.challenge_id}&token={l.token}&token={l.token}", FORM),
        (lambda l: f"c={l.challenge_id}&token", FORM),
        (lambda l: f"c={l.challenge_id};token={l.token}", FORM),
        (lambda l: "c=\u00e9&token=\u00e9".encode("utf-8"), FORM),
        (lambda l: f"c={l.challenge_id}&token={l.token}&z=" + "9" * 2000, FORM),
        (lambda l: b"", FORM),
        (lambda l: f"c={l.challenge_id}&token={l.token}", "multipart/form-data; boundary=x"),
    ],
)
async def test_anything_but_exactly_the_two_form_fields_is_refused_and_the_link_survives(
    tmp_path, body, content_type
):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()
        headers = {"Origin": ORIGIN, **({"content-type": content_type} if content_type else {})}

        response = await h.client.post("/auth/email/confirm", content=body(link), headers=headers)

        assert response.status_code == 400 and response.text == email_pages.invalid_link_page().html
        assert "set-cookie" not in response.headers
        assert (await h.confirm(link)).status_code == 303


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["put", "delete", "patch", "options"])
async def test_confirm_answers_to_get_and_post_only(tmp_path, method):
    async with email_app(tmp_path) as h:
        assert (await getattr(h.client, method)("/auth/email/confirm")).status_code == 405


# --- revoking, disabling and signing in again ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoking_the_address_ends_its_sessions_and_its_links_and_re_allowing_brings_back_neither(tmp_path):
    async with email_app(tmp_path) as h:
        await h.sign_in()
        h.clock.advance(minutes=2)
        waiting = await h.ask_and_get_link()  # a link nobody has used yet
        assert (await h.client.get("/auth/me")).status_code == 200

        with h.factory() as db, db.begin():
            revoke(db, "email", ADDRESS, now=h.clock())

        assert (await h.client.get("/auth/me")).status_code == 401
        assert (await h.confirm(waiting, h.new_client())).status_code == 400
        assert (await h.ask()).status_code == 202 and len(h.mailer.sent) == 2  # no new mail either
        with h.factory() as db, db.begin():
            allow(db, db.get(User, h.user_id), "email", ADDRESS)
        assert (await h.client.get("/auth/me")).status_code == 401  # the session stays ended
        assert (await h.confirm(waiting, h.new_client())).status_code == 400  # and so does the link
        h.clock.advance(minutes=1)
        fresh = await h.ask_and_get_link()
        client = h.new_client()
        assert (await h.confirm(fresh, client)).status_code == 303
        assert me_user(await client.get("/auth/me")) == h.user_id


@pytest.mark.asyncio
async def test_a_user_who_is_disabled_gets_no_link_and_no_session_and_a_link_met_then_is_spent(tmp_path):
    async with email_app(tmp_path) as h:
        await h.sign_in()
        h.clock.advance(minutes=2)
        pending = await h.ask_and_get_link()
        with h.factory() as db, db.begin():
            db.get(User, h.user_id).disabled_at = h.clock()

        assert (await h.client.get("/auth/me")).status_code == 401
        assert (await h.ask()).status_code == 202 and len(h.mailer.sent) == 2  # said the same, sent nothing
        assert (await h.confirm(pending, h.new_client())).status_code == 400
        with h.factory() as db, db.begin():
            db.get(User, h.user_id).disabled_at = None
        assert (await h.confirm(pending, h.new_client())).status_code == 400  # spent by the attempt
        assert state_of(h)["sessions"] == 1


@pytest.mark.asyncio
async def test_logging_out_ends_the_session_and_leaves_the_identity_alone(tmp_path):
    async with email_app(tmp_path) as h:
        await h.sign_in()

        assert (await h.client.post("/auth/logout", headers={"Origin": ORIGIN})).status_code == 204
        assert (await h.client.get("/auth/me")).status_code == 401
        h.clock.advance(minutes=2)
        assert (await h.sign_in()).status_code == 303  # the identity is as allowed as before
        assert state_of(h)["links"] == 1


@pytest.mark.asyncio
async def test_a_session_ends_exactly_when_it_expires_and_the_next_link_makes_a_new_one(tmp_path):
    async with email_app(tmp_path) as h:
        await h.sign_in()

        h.clock.advance(days=14, microseconds=-1)
        assert (await h.client.get("/auth/me")).status_code == 200
        h.clock.advance(microseconds=1)
        assert (await h.client.get("/auth/me")).status_code == 401
        assert (await h.sign_in()).status_code == 303
        assert (await h.client.get("/auth/me")).status_code == 200


# --- the routes that exist and the ones that must not ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_sign_in_is_not_a_way_to_reach_the_telegram_routes(tmp_path):
    async with email_app(tmp_path) as h:
        for path in ("/auth/telegram/login", "/auth/telegram/callback"):
            response = await h.client.get(path)
            assert response.status_code == 503 and response.json() == {"detail": "login_unavailable"}


@pytest.mark.asyncio
async def test_without_email_configured_the_email_routes_say_unavailable_and_healthz_works(tmp_path):
    from tests.auth_support import auth_app

    async with auth_app(tmp_path) as h:  # Telegram only
        responses = [
            await h.client.post("/auth/email/request", json={"email": ADDRESS}),
            await h.client.get("/auth/email/confirm?c=" + str(UUID(int=1))),
            await h.client.post("/auth/email/confirm", data={"c": str(UUID(int=1)), "token": "A" * 43}),
        ]

        assert [r.status_code for r in responses] == [503, 503, 503]
        assert all(r.json() == {"detail": "login_unavailable"} for r in responses)
        assert "set-cookie" not in responses[2].headers
        assert (await h.client.get("/healthz")).json() == {"status": "ok"}
        assert (await sign_in(h.client, h.telegram)).status_code == 303  # and Telegram sign-in works
        assert (await h.client.get("/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_with_neither_configured_every_sign_in_route_says_unavailable(tmp_path):
    from house_helper.web.app import create_app
    from tests.web_support import serving, settings_for

    async with serving(create_app(settings_for(tmp_path / "none.db"))) as client:
        responses = [
            await client.post("/auth/email/request", json={"email": ADDRESS}),
            await client.get("/auth/email/confirm?c=" + str(UUID(int=1))),
            await client.post("/auth/email/confirm", data={"c": str(UUID(int=1)), "token": "A" * 43}),
            await client.get("/auth/me"),
        ]

        assert [r.status_code for r in responses] == [503, 503, 503, 503]
        assert (await client.get("/healthz")).status_code == 200
