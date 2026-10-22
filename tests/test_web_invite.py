"""The Telegram invitation sign-in over HTTP: the page, the start, and the flows that work.

The real application runs (ASGI lifespan, worker threads, a migrated SQLite file); only
Telegram is replaced, by tests/auth_support.FakeTelegram, which enforces the protocol (client
credentials, redirect URI, PKCE, single-use codes) and signs real tokens. What goes wrong is in
tests/test_web_invite_failures.py, and what must stay secret in tests/test_web_invite_secrecy.py.

Browsers are httpx clients with their own cookie jars, talking to the application over its public
https origin (a Secure cookie is only sent back there). An invitation is issued the way the operator
command issues it (tests/invite_support.enroll), and followed the way the page's script does: a
POST of the code to /auth/invite/start, then to Telegram, then back to the ordinary callback.
"""

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
import sqlalchemy as sa

from house_helper.persistence import authn
from house_helper.persistence.allowlist import allow, revoke
from house_helper.persistence.invitations import issue_invitation, revoke_invitations
from house_helper.persistence.models import IdentityInvitation, IdentityLink, OidcState, User
from house_helper.persistence.models import Session as LoginSession
from house_helper.web import invite_pages
from tests.auth_support import (
    ORIGIN,
    START,
    begin_sign_in,
    auth_app,
    cookie_attributes,
    finish_sign_in,
    logout_as,
    rows,
)
from tests.invite_support import (
    MOTHER_SUB,
    OWNER_SUB,
    THIRD_SUB,
    come_back,
    enroll,
    entries_of,
    follow,
    is_unused,
    invitation_row,
    sessions_of,
    shape,
    sign_in_directly,
    start,
    states,
    tables,
)

pytestmark = pytest.mark.asyncio

PRIVATE = {"cache-control": "no-store", "referrer-policy": "no-referrer"}
LOGIN_COOKIE = "__Host-hh_login"
SESSION_COOKIE = "__Host-hh_session"


def query_of(location):
    return {name: values[0] for name, values in parse_qs(urlsplit(location).query).items()}


# --- the page ---------------------------------------------------------------------------------------


async def test_the_page_is_the_same_for_everybody_and_touches_nothing(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        before = tables(h)

        plain = await h.client.get("/auth/invite")
        # what a link with a fragment, or with a query that somebody added, brings to the server
        with_query = await h.client.get("/auth/invite", params={"code": mother.code, "c": mother.token})

        assert plain.status_code == 200 and plain.text == invite_pages.invitation_page().html
        assert with_query.text == plain.text  # nothing of the request is echoed
        assert mother.token not in with_query.text and mother.code not in with_query.text
        assert plain.headers["content-type"].startswith("text/html")
        assert plain.headers["content-security-policy"] == invite_pages.invitation_page().headers[
            "Content-Security-Policy"
        ]
        assert plain.headers["cache-control"] == "no-store"
        assert tables(h) == before and is_unused(h, mother)  # looking uses nothing up and stores nothing
        assert "set-cookie" not in plain.headers


async def test_the_page_says_nothing_about_whether_an_invitation_is_valid(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        valid = await h.client.get("/auth/invite")
        h.clock.advance(hours=3)  # long expired
        expired = await h.client.get("/auth/invite")

        assert shape(valid) == shape(expired) and is_unused(h, mother)


# --- the start ----------------------------------------------------------------------------------------


async def test_a_live_invitation_starts_a_login_that_is_bound_to_this_browser_and_to_the_invitation(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        response = await start(h.client, mother.code)

        assert response.status_code == 200
        assert response.json() == {"authorization_url": response.json()["authorization_url"]}
        for name, value in PRIVATE.items():
            assert response.headers[name] == value
        url = response.json()["authorization_url"]
        assert url.startswith("https://oauth.telegram.org/auth?")
        parameters = query_of(url)
        assert set(parameters) == {
            "response_type", "client_id", "redirect_uri", "scope", "state", "nonce",
            "code_challenge", "code_challenge_method",
        }  # exactly an ordinary login's: nothing of the invitation goes to the provider
        assert parameters["redirect_uri"] == h.settings.redirect_uri == ORIGIN + "/auth/telegram/callback"
        assert parameters["scope"] == "openid" and parameters["code_challenge_method"] == "S256"
        cookie = cookie_attributes(response, LOGIN_COOKIE)
        assert cookie["value"] == parameters["state"]
        assert cookie["httponly"] is True and cookie["secure"] is True
        assert cookie["samesite"].lower() == "lax" and cookie["path"] == "/" and "domain" not in cookie
        assert int(cookie["max-age"]) == 600
        with h.factory() as db:
            (state,) = db.execute(sa.select(OidcState.invitation_id, OidcState.state_hash)).all()
        assert state.invitation_id == mother.invitation_id
        assert state.state_hash == authn.hash_secret(parameters["state"])
        assert is_unused(h, mother)  # starting uses nothing up
        assert tables(h)["allowlist_entries"] == 0 and tables(h)["sessions"] == 0


async def test_a_login_may_be_started_again_and_the_invitation_is_still_there(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        first = await start(h.client, mother.code)
        second = await start(h.client, mother.code)

        assert first.status_code == second.status_code == 200
        assert first.json() != second.json()  # a new state and nonce each time
        assert len(states(h)) == 2 and is_unused(h, mother)


async def test_every_invitation_that_cannot_start_a_login_gets_the_same_answer(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        live = enroll(h, "Живая")
        wrong_secret = f"{live.invitation_id}.{'A' * 43}"
        unknown = f"{uuid4()}.{live.token}"
        used = enroll(h, "Использованная")
        revoked = enroll(h, "Отозванная")
        superseded = enroll(h, "Заменённая")
        expiring = enroll(h, "Просроченная", now=h.clock() - timedelta(hours=2, seconds=1))
        disabled = enroll(h, "Отключённая")
        linked = enroll(h, "Привязанная")
        with h.factory() as db, db.begin():
            db.execute(
                sa.update(IdentityInvitation)
                .where(IdentityInvitation.id == used.invitation_id)
                .values(consumed_at=h.clock())
            )
            revoke_invitations(db, revoked.user_id, now=h.clock())
            issue_invitation(db, superseded.user_id, now=h.clock())  # the first one is superseded
            db.get(User, disabled.user_id).disabled_at = h.clock()
            allow(db, db.get(User, linked.user_id), "telegram", THIRD_SUB)
        candidates = {
            "not a code": "hello",
            "empty": "",
            "id only": str(live.invitation_id),
            "secret only": live.token,
            "wrong secret": wrong_secret,
            "unknown id": unknown,
            "used": used.code,
            "revoked": revoked.code,
            "superseded": superseded.code,
            "expired": expiring.code,
            "user disabled": disabled.code,
            "user already linked": linked.code,
            "upper case": live.code.upper(),
            "padded": " " + live.code,
            "extra part": live.code + ".x",
        }

        answers = {name: await start(h.client, code) for name, code in candidates.items()}

        assert {name: response.status_code for name, response in answers.items()} == {
            name: 400 for name in candidates
        }
        assert len({shape(response) for response in answers.values()}) == 1  # one answer, whatever the reason
        sample = answers["unknown id"]
        assert sample.json()["detail"] == "invitation_invalid"
        assert "set-cookie" not in sample.headers
        assert tables(h)["oidc_states"] == 0  # nothing was stored for any of them
        assert is_unused(h, live)
        # and the one that is live still starts (the others did not use anything up)
        assert (await start(h.client, live.code)).status_code == 200


@pytest.mark.parametrize(
    "send",
    [
        pytest.param(lambda c, code: c.post("/auth/invite/start", content=b"{}", headers={"content-type": "text/plain"}), id="text"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", data={"code": code}), id="form"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", content=b'{"code": "x"}'), id="no content type"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", content=b"not json", headers={"content-type": "application/json"}), id="not json"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", json=[code]), id="array"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", json=code), id="string"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", json={}), id="empty object"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", json={"code": code, "extra": 1}), id="extra field"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", json={"kod": code}), id="other field"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", json={"code": 12345}), id="number"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", json={"code": None}), id="null"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", json={"code": [code]}), id="list"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", json={"code": code, "x": "y" * 600}), id="too long"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", content=b"[" * 5000, headers={"content-type": "application/json"}), id="nested"),
        pytest.param(lambda c, code: c.post("/auth/invite/start", params={"code": code}), id="in the query, no body"),
    ],
)
async def test_a_request_that_is_not_exactly_a_json_object_with_a_code_is_refused_and_stores_nothing(tmp_path, send):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        response = await send(h.client, mother.code)

        assert response.status_code == 400 and response.json()["detail"] == "invalid_request"
        assert tables(h)["oidc_states"] == 0 and is_unused(h, mother)
        assert "set-cookie" not in response.headers
        assert mother.token not in response.text


async def test_a_body_over_the_limit_is_refused_though_it_is_a_valid_request_in_every_other_way(tmp_path):
    async def chunks(payload):
        for index in range(0, len(payload), 100):
            yield payload[index : index + 100]

    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        valid = json.dumps({"code": mother.code}).encode()
        padded = valid + b" " * 600  # trailing white space is legal JSON: only the size is wrong
        assert len(valid) < 512 < len(padded)
        headers = {"origin": ORIGIN, "content-type": "application/json"}

        declared = await h.client.post("/auth/invite/start", content=padded, headers=headers)
        streamed = await h.client.post("/auth/invite/start", content=chunks(padded), headers=headers)  # no length declared
        unpadded = await h.client.post("/auth/invite/start", content=valid, headers=headers)

        assert declared.status_code == streamed.status_code == 400
        assert declared.json()["detail"] == streamed.json()["detail"] == "invalid_request"
        assert tables(h)["oidc_states"] == 0 or unpadded.status_code == 200
        assert unpadded.status_code == 200 and tables(h)["oidc_states"] == 1  # the same request, small enough


async def test_the_code_in_a_query_string_is_never_read(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        # a body that would be fine, and a code in the query that is the live one: only the body counts
        refused = await h.client.post(
            "/auth/invite/start", json={"code": f"{mother.invitation_id}.{'B' * 43}"}, params={"code": mother.code},
            headers={"origin": ORIGIN},
        )

        assert refused.status_code == 400 and tables(h)["oidc_states"] == 0


async def test_the_start_is_a_post_and_nothing_else(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        for method in ("GET", "PUT", "PATCH", "DELETE"):
            response = await h.client.request(method, "/auth/invite/start")
            assert response.status_code == 405, method


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"origin": "http://kitchen.example.test"},
        {"origin": ORIGIN + ".evil.example"},
        {"origin": ""},
        {"origin": ORIGIN + "/"},
        {"origin": ORIGIN, "sec-fetch-site": "cross-site"},
    ],
    ids=["foreign", "null", "http", "suffix", "empty", "slash", "second opinion"],
)
async def test_a_start_from_another_site_is_refused_even_with_a_live_invitation(tmp_path, headers):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        response = await h.client.post("/auth/invite/start", json={"code": mother.code}, headers=headers)

        if headers.get("origin") == ORIGIN:  # the Origin is ours: the browser's own word is enough
            assert response.status_code == 200
            return
        assert response.status_code == 403 and response.json()["detail"] == "forbidden_origin"
        assert tables(h)["oidc_states"] == 0 and is_unused(h, mother)
        assert "set-cookie" not in response.headers


async def test_two_origin_headers_are_refused(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        response = await h.client.post(
            "/auth/invite/start", json={"code": mother.code}, headers=[("origin", ORIGIN), ("origin", ORIGIN)]
        )

        assert response.status_code == 403 and tables(h)["oidc_states"] == 0


@pytest.mark.parametrize(
    ("site", "allowed"),
    [("same-origin", True), ("none", True), ("same-site", False), ("cross-site", False), ("", False)],
)
async def test_without_an_origin_the_fetch_metadata_decides(tmp_path, site, allowed):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        response = await start(h.client, mother.code, origin=None, **{"sec-fetch-site": site})

        assert (response.status_code == 200) is allowed, response.text
        assert (tables(h)["oidc_states"] == 1) is allowed


async def test_a_request_with_no_browser_headers_at_all_is_allowed_to_start(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        response = await start(h.client, mother.code, origin=None)

        assert response.status_code == 200  # not a browser: no cookie it did not choose to send


# --- the flow ---------------------------------------------------------------------------------------


async def test_a_valid_invitation_and_a_verified_telegram_identity_link_the_user_and_sign_them_in(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)

        started = await start(h.client, mother.code)
        callback_url = h.telegram.authorize(started.json()["authorization_url"], MOTHER_SUB)
        assert is_unused(h, mother)  # approved at Telegram, not yet back: still nothing used up
        assert entries_of(h, mother.user_id) == []
        response = await h.client.get(callback_url)

        assert response.status_code == 303 and response.headers["location"] == "/auth/invite/done"
        for name, value in PRIVATE.items():
            assert response.headers[name] == value
        session_cookie = cookie_attributes(response, SESSION_COOKIE)
        assert session_cookie["httponly"] is True and session_cookie["secure"] is True
        assert session_cookie["samesite"].lower() == "lax" and session_cookie["path"] == "/"
        assert int(session_cookie["max-age"]) == 14 * 24 * 3600  # the existing policy, unchanged
        spent = cookie_attributes(response, LOGIN_COOKIE)
        assert spent["value"] == "" or int(spent["max-age"]) == 0  # the login cookie is spent
        me = await h.client.get("/auth/me")
        assert me.json() == {
            "user_id": str(mother.user_id),
            "session_expires_at": (START + timedelta(days=14)).isoformat(),
        }
        done = await h.client.get("/auth/invite/done")
        assert done.status_code == 200 and done.text == invite_pages.done_page().html
        assert h.telegram.protocol_errors == []
        # the database: the invitation used up exactly once, the identity linked to exactly the invited user
        assert invitation_row(h, mother).consumed_at == START
        assert entries_of(h, mother.user_id) == [("telegram", MOTHER_SUB, False)]
        with h.factory() as db:
            link = db.scalars(sa.select(IdentityLink)).one()
            (opened,) = db.scalars(sa.select(LoginSession)).all()
        assert (link.user_id, link.provider, link.subject) == (mother.user_id, "telegram", MOTHER_SUB)
        assert opened.user_id == mother.user_id and opened.identity_link_id == link.id  # provenance
        assert tables(h) == {
            "users": 1, "allowlist_entries": 1, "identity_links": 1, "sessions": 1,
            "oidc_states": 1, "identity_invitations": 1,
        }


async def test_the_page_after_the_link_is_for_a_signed_in_browser_only(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        stranger = await h.client.get("/auth/invite/done")

        assert stranger.status_code == 303 and stranger.headers["location"] == "/"
        assert "Готово" not in stranger.text


async def test_the_owner_and_mother_link_two_different_telegram_accounts_to_two_users(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        owner = enroll(h, "Елена")
        mother = enroll(h, "Мама")
        owners_browser, mothers_browser = h.client, h.new_client()

        assert (await follow(owners_browser, h.telegram, owner, OWNER_SUB)).status_code == 303
        assert (await follow(mothers_browser, h.telegram, mother, MOTHER_SUB)).status_code == 303

        assert (await owners_browser.get("/auth/me")).json()["user_id"] == str(owner.user_id)
        assert (await mothers_browser.get("/auth/me")).json()["user_id"] == str(mother.user_id)
        assert entries_of(h, owner.user_id) == [("telegram", OWNER_SUB, False)]
        assert entries_of(h, mother.user_id) == [("telegram", MOTHER_SUB, False)]
        assert is_unused(h, owner) is False and is_unused(h, mother) is False  # each used once
        assert h.telegram.protocol_errors == []


async def test_after_linking_each_person_sees_only_their_own_conversations(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        owner, mother = enroll(h, "Елена"), enroll(h, "Мама")
        owners_browser, mothers_browser = h.client, h.new_client()
        await follow(owners_browser, h.telegram, owner, OWNER_SUB)
        await follow(mothers_browser, h.telegram, mother, MOTHER_SUB)
        headers = {"origin": ORIGIN}

        mine = (await owners_browser.post("/api/conversations", json={}, headers=headers)).json()["id"]
        theirs = [
            (await mothers_browser.post("/api/conversations", json={}, headers=headers)).json()["id"]
            for _ in range(2)
        ]

        listed = lambda response: {c["id"] for c in response.json()["conversations"]}  # noqa: E731
        assert listed(await owners_browser.get("/api/conversations")) == {mine}
        assert listed(await mothers_browser.get("/api/conversations")) == set(theirs)
        # nobody reads the other's: the same answer as for a conversation that does not exist
        foreign = await mothers_browser.get(f"/api/conversations/{mine}/messages")
        unknown = await mothers_browser.get(f"/api/conversations/{uuid4()}/messages")
        assert foreign.status_code == unknown.status_code == 404
        without_id = lambda response: {k: v for k, v in response.json().items() if k != "correlation_id"}  # noqa: E731
        assert without_id(foreign) == without_id(unknown)
        assert (await owners_browser.get(f"/api/conversations/{theirs[0]}/messages")).status_code == 404


async def test_after_linking_the_person_signs_in_with_telegram_alone_from_another_device(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        phone = h.client
        await follow(phone, h.telegram, mother, MOTHER_SUB)
        tablet = h.new_client()
        before = invitation_row(h, mother)

        started = await begin_sign_in(tablet)  # the ordinary login: no invitation anywhere
        response = await finish_sign_in(tablet, h.telegram, started, MOTHER_SUB)

        assert response.status_code == 303 and response.headers["location"] == "/"
        assert (await tablet.get("/auth/me")).json()["user_id"] == str(mother.user_id)
        assert (await phone.get("/auth/me")).json()["user_id"] == str(mother.user_id)  # both are signed in
        assert len(sessions_of(h, mother.user_id)) == 2
        assert invitation_row(h, mother) == before  # the invitation was not touched again
        assert [s.invitation_id for s in states(h)] == [mother.invitation_id, None]
        assert entries_of(h, mother.user_id) == [("telegram", MOTHER_SUB, False)]  # still one identity
        assert h.telegram.protocol_errors == []


async def test_signing_out_on_one_device_leaves_the_other_signed_in(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        phone, tablet = h.client, h.new_client()
        await follow(phone, h.telegram, mother, MOTHER_SUB)
        await finish_sign_in(tablet, h.telegram, await begin_sign_in(tablet), MOTHER_SUB)

        out = await phone.post("/auth/logout", headers=logout_as(mother.user_id, origin=ORIGIN))

        assert out.status_code == 204
        assert (await phone.get("/auth/me")).status_code == 401
        assert (await tablet.get("/auth/me")).status_code == 200


async def test_a_person_without_an_invitation_cannot_sign_in_with_telegram(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        enroll(h)  # an invitation exists, but this person does not have it

        started = await begin_sign_in(h.client)
        response = await finish_sign_in(h.client, h.telegram, started, THIRD_SUB)

        assert response.status_code == 403 and response.json() == {"detail": "not_allowed"}  # as ever
        assert tables(h)["allowlist_entries"] == 0 and tables(h)["sessions"] == 0


async def test_the_invitation_can_be_used_once_only(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        await follow(h.client, h.telegram, mother, MOTHER_SUB)
        other_browser = h.new_client()

        again = await start(other_browser, mother.code)

        assert again.status_code == 400 and again.json()["detail"] == "invitation_invalid"
        assert tables(h)["oidc_states"] == 1  # the one that was used; nothing new
        assert len(entries_of(h, mother.user_id)) == 1


async def test_the_same_telegram_account_cannot_be_linked_to_a_second_user_by_a_second_invitation(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        owner, mother = enroll(h, "Елена"), enroll(h, "Мама")
        await follow(h.client, h.telegram, owner, OWNER_SUB)
        mothers_browser = h.new_client()

        response = await follow(mothers_browser, h.telegram, mother, OWNER_SUB)  # her phone, his account

        assert response.status_code == 409 and "text/html" in response.headers["content-type"]
        assert response.text == invite_pages.identity_page().html
        assert entries_of(h, mother.user_id) == []
        assert entries_of(h, owner.user_id) == [("telegram", OWNER_SUB, False)]
        assert is_unused(h, mother)  # still valid: she may use another Telegram account
        assert (await mothers_browser.get("/auth/me")).status_code == 401
        # and with the right account it works
        assert (await follow(mothers_browser, h.telegram, mother, MOTHER_SUB)).status_code == 303


async def test_a_person_already_signed_in_as_the_invited_user_may_link_telegram_and_gets_a_new_session(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        old_token = sign_in_directly(h, h.client, mother.user_id, "mother@example.com")

        response = await follow(h.client, h.telegram, mother, MOTHER_SUB)

        assert response.status_code == 303 and response.headers["location"] == "/auth/invite/done"
        assert (await h.client.get("/auth/me")).json()["user_id"] == str(mother.user_id)
        assert h.client.cookies.get(SESSION_COOKIE) != old_token  # the cookie was replaced
        with h.factory() as db:
            assert authn.authenticate(db, old_token, now=h.clock()) is not None  # the old one is not ended
        assert len(sessions_of(h, mother.user_id)) == 2
        assert sorted(entries_of(h, mother.user_id)) == [
            ("email", "mother@example.com", False),
            ("telegram", MOTHER_SUB, False),
        ]


async def test_the_session_made_by_an_invitation_ends_when_the_telegram_identity_is_revoked(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        await follow(h.client, h.telegram, mother, MOTHER_SUB)
        assert (await h.client.get("/auth/me")).status_code == 200

        with h.factory() as db, db.begin():
            assert revoke(db, "telegram", MOTHER_SUB, now=h.clock()) is True

        assert (await h.client.get("/auth/me")).status_code == 401  # provenance: the identity it came in by
        started = await begin_sign_in(h.client)
        denied = await finish_sign_in(h.client, h.telegram, started, MOTHER_SUB)
        assert denied.status_code == 403
        assert (await start(h.client, mother.code)).status_code == 400  # and the invitation is not a way back


async def test_the_session_made_by_an_invitation_ends_at_the_same_time_as_any_other(tmp_path):
    async with auth_app(tmp_path, seed=False) as h:
        mother = enroll(h)
        await follow(h.client, h.telegram, mother, MOTHER_SUB)

        h.clock.advance(days=13, hours=23)
        assert (await h.client.get("/auth/me")).status_code == 200
        h.clock.advance(hours=1)  # fourteen days after the sign-in: over, to the instant
        assert (await h.client.get("/auth/me")).status_code == 401
