"""The ordinary Telegram sign-in in a browser that is already signed in as somebody else.

The browser has one session cookie. If it holds a live session of user A and the callback of an
ordinary Telegram login verifies the identity of user B, answering with B's cookie would silently
sign A out of that browser (A's session row would stay valid but unreachable) and sign B in on
top of whatever A had on the screen. So the callback refuses: 409 signed_in_as_another_user, a
page in Russian, A's cookie and session untouched, nothing issued for B, nothing written for B
(not even the identity link that a first login creates). The invitation sign-in and the emailed
link have always refused the same way; this is the same rule for the plain button.

The check is made in the transaction that issues the session (persistence.authn.complete_login
reads the browser's cookie there), not before the provider is called: a session that ends while
the provider is being asked is not in the way, and one that is still live at the moment of issuing
is. What no server can see is a cookie the browser did not send with the request; that one case
is pinned at the end, so that it is a known limit and not a surprise.

Only fake providers are used.
"""

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest
import sqlalchemy as sa

from house_helper.persistence import authn
from house_helper.persistence.allowlist import revoke
from house_helper.persistence.models import IdentityLink, User
from house_helper.persistence.models import Session as LoginSession
from house_helper.web import invite_pages
from tests.auth_support import (
    ORIGIN,
    SUBJECT,
    app_log_text,
    auth_app,
    begin_sign_in,
    cookie_attributes,
    counts,
    finish_sign_in,
    logout_as,
    rows,
    seed_user,
    sign_in,
    watch_logs,
)
from tests.invite_support import HOST, OWNER_SUB, enroll, follow, sign_in_directly, start
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

LOGIN_COOKIE = "__Host-hh_login"
SESSION_COOKIE = "__Host-hh_session"
CODE = "signed_in_as_another_user"


def second_user(h) -> object:
    """Boris: another provisioned user with a Telegram identity of his own."""
    return seed_user(h.factory, subject=OWNER_SUB, display_name="Борис")


def snapshot(h) -> dict:
    """Every row of every table a sign-in writes (the spent login states left out)."""
    with h.factory() as db:
        return {
            table: sorted(repr(tuple(row)) for row in db.execute(sa.text(f"SELECT * FROM {table}")).all())
            for table in ("users", "allowlist_entries", "identity_links", "sessions")
        }


def assert_conflict(response) -> None:
    """The answer is the page that says the browser is signed in as somebody else, and it
    sets no session cookie; the login attempt's own cookie is spent."""
    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] in ("no-referrer", "same-origin")
    assert f'data-code="{CODE}"' in response.text
    assert "учётную запись" in response.text and "выйдите" in response.text
    assert "script" not in response.text.lower()
    cookies = response.headers.get_list("set-cookie")
    assert not any(cookie.startswith(f"{SESSION_COOKIE}=") for cookie in cookies), cookies
    assert cookie_attributes(response, LOGIN_COOKIE)["max-age"] == "0"


async def me(client) -> str:
    response = await client.get("/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["user_id"]


def callback_state(callback: str) -> str:
    return parse_qs(urlsplit(callback).query)["state"][0]


# --- the three kinds of browser ---------------------------------------------------------------------


async def test_a_browser_with_no_session_signs_in_as_always(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await sign_in(h.client, h.telegram)

        assert response.status_code == 303 and response.headers["location"] == "/"
        assert cookie_attributes(response, SESSION_COOKIE)["max-age"] == str(14 * 24 * 3600)
        assert await me(h.client) == str(h.user_id)
        assert counts(h)["sessions"] == 1


@pytest.mark.parametrize("cookie", ["x" * 43, "not a token", "x" * 500])
async def test_a_cookie_that_names_no_session_is_no_obstacle(tmp_path, cookie):
    async with auth_app(tmp_path) as h:
        h.client.cookies.set(SESSION_COOKIE, cookie, domain=HOST, path="/")

        response = await sign_in(h.client, h.telegram)

        assert response.status_code == 303
        assert await me(h.client) == str(h.user_id)


async def test_the_same_user_signing_in_again_gets_a_new_session_and_keeps_the_old_one(tmp_path):
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        first = h.client.cookies.get(SESSION_COOKIE)

        response = await sign_in(h.client, h.telegram)

        assert response.status_code == 303 and response.headers["location"] == "/"
        assert h.client.cookies.get(SESSION_COOKIE) != first
        assert await me(h.client) == str(h.user_id)
        sessions = rows(h, LoginSession)
        links = rows(h, IdentityLink)
        assert len(sessions) == 2 and len(links) == 1
        assert all(s.revoked_at is None and s.user_id == h.user_id for s in sessions)
        assert {s.identity_link_id for s in sessions} == {links[0].id}  # the way in is recorded as ever
        assert all(s.expires_at - s.created_at == authn.SESSION_LIFETIME for s in sessions)  # 14 days, as ever


# --- a browser that is signed in as somebody else ---------------------------------------------------------


async def test_a_browser_signed_in_as_another_user_is_refused_and_keeps_its_session(tmp_path):
    async with auth_app(tmp_path) as h:
        second_user(h)
        await sign_in(h.client, h.telegram)  # the first user's session
        token = h.client.cookies.get(SESSION_COOKIE)
        before = snapshot(h)

        response = await sign_in(h.client, h.telegram, OWNER_SUB)  # Boris's account, approved at Telegram

        assert_conflict(response)
        assert h.client.cookies.get(SESSION_COOKIE) == token  # the cookie is the same one
        assert await me(h.client) == str(h.user_id)  # and still the first user's
        assert snapshot(h) == before  # no session for Boris, no link for him, nothing of the first user touched
        assert all(s.revoked_at is None for s in rows(h, LoginSession))


async def test_the_refusal_page_is_a_page_of_its_own_and_says_nothing_about_anybody(tmp_path):
    async with auth_app(tmp_path) as h:
        boris = second_user(h)
        await sign_in(h.client, h.telegram)

        response = await sign_in(h.client, h.telegram, OWNER_SUB)

        assert response.text == invite_pages.login_other_user_page().html
        assert response.text != invite_pages.other_user_page().html  # not the invitation's: no invitation here
        assert "приглашени" not in response.text.lower()
        for private in (SUBJECT, OWNER_SUB, str(h.user_id), str(boris), "Борис", "Елена"):
            assert private not in response.text
            assert private not in "".join(f"{k}: {v}" for k, v in response.headers.items())


async def test_the_refusal_is_logged_without_naming_anybody(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        boris = second_user(h)
        await sign_in(h.client, h.telegram)
        token = h.client.cookies.get(SESSION_COOKIE)
        caplog.clear()  # (the first user's own sign-in, before, logs their id as it always did)

        response = await sign_in(h.client, h.telegram, OWNER_SUB)

        logged = app_log_text(caplog)
        assert response.status_code == 409
        assert "signed in as another user" in logged
        for private in (SUBJECT, OWNER_SUB, str(h.user_id), str(boris), token):
            assert private not in logged


async def test_after_a_refusal_the_person_signs_out_and_then_signs_in_as_the_other_user(tmp_path):
    async with auth_app(tmp_path) as h:
        boris = second_user(h)
        await sign_in(h.client, h.telegram)
        assert (await sign_in(h.client, h.telegram, OWNER_SUB)).status_code == 409

        signed_out = await h.client.post("/auth/logout", headers=logout_as(h.user_id, origin=ORIGIN))
        response = await sign_in(h.client, h.telegram, OWNER_SUB)

        assert signed_out.status_code == 204
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert await me(h.client) == str(boris)


async def test_a_refused_login_that_is_replayed_issues_nothing_either(tmp_path):
    async with auth_app(tmp_path) as h:
        second_user(h)
        await sign_in(h.client, h.telegram)
        token = h.client.cookies.get(SESSION_COOKIE)
        started = await begin_sign_in(h.client)
        callback = h.telegram.authorize(started.headers["location"], OWNER_SUB)
        assert (await h.client.get(callback)).status_code == 409
        before, exchanged = snapshot(h), h.telegram.token_calls

        replay = await h.client.get(callback)  # the login cookie was spent by the refusal
        h.client.cookies.set(LOGIN_COOKIE, callback_state(callback), domain=HOST, path="/")
        with_cookie = await h.client.get(callback)  # and with it put back: the state was used up

        for answer in (replay, with_cookie):
            assert answer.status_code == 400 and answer.json() == {"detail": "invalid_login_request"}
            assert not any(c.startswith(f"{SESSION_COOKIE}=") for c in answer.headers.get_list("set-cookie"))
        assert h.telegram.token_calls == exchanged  # no code was exchanged a second time
        assert snapshot(h) == before and h.client.cookies.get(SESSION_COOKIE) == token


async def test_a_login_that_succeeded_cannot_be_replayed_into_a_second_session(tmp_path):
    async with auth_app(tmp_path) as h:
        started = await begin_sign_in(h.client)
        callback = h.telegram.authorize(started.headers["location"])
        assert (await h.client.get(callback)).status_code == 303
        before = snapshot(h)

        replay = await h.client.get(callback)

        assert replay.status_code == 400 and snapshot(h) == before


# --- a session that is not live does not count ------------------------------------------------------------


def expire(h, token):
    h.clock.advance(days=15)


def withdraw(h, token):
    with h.factory() as db, db.begin():
        assert authn.revoke_session(db, token, now=h.clock())


def remove_identity(h, token):
    with h.factory() as db, db.begin():
        assert revoke(db, "telegram", SUBJECT, now=h.clock())


def disable_user(h, token):
    with h.factory() as db, db.begin():
        db.get(User, h.user_id).disabled_at = h.clock()


@pytest.mark.parametrize("end", [expire, withdraw, remove_identity, disable_user], ids=lambda f: f.__name__)
async def test_a_session_that_is_over_does_not_stand_in_the_way(tmp_path, end):
    async with auth_app(tmp_path) as h:
        boris = second_user(h)
        await sign_in(h.client, h.telegram)
        token = h.client.cookies.get(SESSION_COOKIE)
        end(h, token)  # (the cookie is still in the browser)
        assert (await h.client.get("/auth/me")).status_code == 401

        response = await sign_in(h.client, h.telegram, OWNER_SUB)

        assert response.status_code == 303
        assert h.client.cookies.get(SESSION_COOKIE) != token
        assert await me(h.client) == str(boris)


# --- tabs and a change of who is signed in while the login is under way -------------------------------------


async def test_another_tab_that_signed_in_as_somebody_else_in_between_is_refused_at_the_end(tmp_path):
    async with auth_app(tmp_path) as h:
        second_user(h)
        started = await begin_sign_in(h.client)  # tab 1: nobody is signed in yet
        token = sign_in_directly(h, h.client, h.user_id, "tab2@example.com")  # tab 2: the first user is
        before = snapshot(h)

        response = await finish_sign_in(h.client, h.telegram, started, OWNER_SUB)

        assert_conflict(response)
        assert h.client.cookies.get(SESSION_COOKIE) == token
        assert await me(h.client) == str(h.user_id)
        assert snapshot(h) == before


async def test_another_tab_that_went_through_telegram_makes_the_first_tab_stale_and_harms_nobody(tmp_path):
    async with auth_app(tmp_path) as h:
        second_user(h)
        first_tab = await begin_sign_in(h.client)
        await sign_in(h.client, h.telegram)  # tab 2 signs the first user in; its login cookie replaced tab 1's
        token = h.client.cookies.get(SESSION_COOKIE)
        before = snapshot(h)

        response = await finish_sign_in(h.client, h.telegram, first_tab, OWNER_SUB)

        assert response.status_code == 400 and response.json() == {"detail": "invalid_login_request"}
        assert h.client.cookies.get(SESSION_COOKIE) == token and snapshot(h) == before


async def test_a_person_who_signed_out_in_another_tab_while_the_login_was_under_way_signs_in(tmp_path):
    async with auth_app(tmp_path) as h:
        boris = second_user(h)
        await sign_in(h.client, h.telegram)
        started = await begin_sign_in(h.client)  # begun while the first user was signed in
        signed_out = await h.client.post("/auth/logout", headers=logout_as(h.user_id, origin=ORIGIN))
        assert signed_out.status_code == 204

        response = await finish_sign_in(h.client, h.telegram, started, OWNER_SUB)

        assert response.status_code == 303 and await me(h.client) == str(boris)


async def test_the_check_is_made_when_the_session_is_issued_not_when_the_request_came(tmp_path):
    async with auth_app(tmp_path) as h:
        boris = second_user(h)
        await sign_in(h.client, h.telegram)
        token = h.client.cookies.get(SESSION_COOKIE)
        started = await begin_sign_in(h.client)
        arrived, release = h.telegram.hold_token_requests()
        waiting = asyncio.ensure_future(finish_sign_in(h.client, h.telegram, started, OWNER_SUB))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)  # the callback carried the first user's cookie...

        withdraw(h, token)  # ...whose session ends while Telegram is being asked
        release.set()
        response = await asyncio.wait_for(waiting, TIMEOUT)

        assert response.status_code == 303 and await me(h.client) == str(boris)


async def test_a_session_still_live_at_the_moment_of_issuing_is_in_the_way(tmp_path):
    async with auth_app(tmp_path) as h:
        second_user(h)
        await sign_in(h.client, h.telegram)
        token = h.client.cookies.get(SESSION_COOKIE)
        started = await begin_sign_in(h.client)
        arrived, release = h.telegram.hold_token_requests()
        waiting = asyncio.ensure_future(finish_sign_in(h.client, h.telegram, started, OWNER_SUB))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        before = snapshot(h)

        release.set()
        response = await asyncio.wait_for(waiting, TIMEOUT)

        assert_conflict(response)
        assert h.client.cookies.get(SESSION_COOKIE) == token and snapshot(h) == before


async def test_a_cookie_the_browser_did_not_send_cannot_be_seen_and_the_known_limit_is_this(tmp_path):
    # The one gap, and a narrow one: another tab signs the first user in AFTER this callback's request
    # was sent (so its cookie is not in the request), while Telegram is still being asked. The server
    # cannot see what the browser did not send. The login then succeeds for Boris; the first user's
    # session is not touched (it stays valid in the database), only the browser's cookie is replaced.
    async with auth_app(tmp_path) as h:
        boris = second_user(h)
        started = await begin_sign_in(h.client)
        arrived, release = h.telegram.hold_token_requests()
        waiting = asyncio.ensure_future(finish_sign_in(h.client, h.telegram, started, OWNER_SUB))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        sign_in_directly(h, h.client, h.user_id, "tab2@example.com")

        release.set()
        response = await asyncio.wait_for(waiting, TIMEOUT)

        assert response.status_code == 303 and await me(h.client) == str(boris)
        first_users = [s for s in rows(h, LoginSession) if s.user_id == h.user_id]
        assert len(first_users) == 1 and first_users[0].revoked_at is None


# --- what was already right stays right ---------------------------------------------------------------------


async def test_the_invitation_sign_in_and_the_guarded_logout_are_as_they_were(tmp_path):
    async with auth_app(tmp_path) as h:
        mother = enroll(h, "Мама")
        await sign_in(h.client, h.telegram)  # the first user's browser
        token = h.client.cookies.get(SESSION_COOKIE)

        refused = await start(h.client, mother.code)  # her link, opened in his browser
        wrong = await h.client.post("/auth/logout", headers=logout_as(mother.user_id, origin=ORIGIN))
        right = await h.client.post("/auth/logout", headers=logout_as(h.user_id, origin=ORIGIN))

        assert refused.status_code == 409 and refused.json()["detail"] == CODE
        assert wrong.status_code == 409 and wrong.json() == {"detail": "session_mismatch"}
        assert wrong.headers.get_list("set-cookie") == []  # nothing revoked, no cookie touched
        assert right.status_code == 204
        assert (await h.client.get("/auth/me")).status_code == 401
        assert h.client.cookies.get(SESSION_COOKIE) is None
        # and the invitation, which the refusal left valid, works once the browser is nobody's
        done = await follow(h.client, h.telegram, mother, "5550001112223334445")
        assert done.status_code == 303 and done.headers["location"] == "/auth/invite/done"
        assert await me(h.client) == str(mother.user_id)
        assert token != h.client.cookies.get(SESSION_COOKIE)
