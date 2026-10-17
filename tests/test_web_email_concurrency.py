"""Email sign-in under concurrency, and beside Telegram sign-in.

Requests that arrive at the same moment: one link is used by one of them; one address
gets one mail; a confirmation racing a revocation never leaves a live session. And the
two ways to sign in side by side: each configured or not independently of the other,
each failing without touching the other, sessions that record which of them opened
them, and revoking one identity leaving the other's sessions alone.
"""

import asyncio
import time

import httpx
import pytest

from house_helper.persistence import email_login
from house_helper.persistence.allowlist import allow, revoke
from house_helper.persistence.models import EmailLoginChallenge, IdentityLink, User
from house_helper.persistence.models import Session as LoginSession
from tests.auth_support import ORIGIN, SUBJECT, finish_sign_in, begin_sign_in, rows, sign_in
from tests.email_support import ADDRESS, email_app

SESSION_COOKIE = "__Host-hh_session"


@pytest.fixture
def slow_allowlist_reads(monkeypatch):
    """A redemption lingers after its allowlist lookup, so the others have time to run
    into it if nothing orders them."""
    real = email_login.allowed_user

    def slow(*args, **kwargs):
        found = real(*args, **kwargs)
        time.sleep(0.05)
        return found

    monkeypatch.setattr(email_login, "allowed_user", slow)


# --- one link, many hands ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_of_eight_browsers_confirming_one_link_exactly_one_signs_in(tmp_path, slow_allowlist_reads):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()
        clients = [h.new_client() for _ in range(8)]

        responses = await asyncio.gather(*(h.confirm(link, client) for client in clients))

        statuses = sorted(response.status_code for response in responses)
        assert statuses == [303] + [400] * 7
        winners = [client for client, response in zip(clients, responses) if response.status_code == 303]
        assert (await winners[0].get("/auth/me")).status_code == 200
        for client, response in zip(clients, responses):
            if response.status_code == 400:
                assert "set-cookie" not in response.headers
                assert (await client.get("/auth/me")).status_code == 401
        assert len(rows(h, LoginSession)) == 1 and len(rows(h, IdentityLink)) == 1


@pytest.mark.asyncio
async def test_the_same_browser_pressing_the_button_twice_gets_one_session(tmp_path, slow_allowlist_reads):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()

        first, second = await asyncio.gather(h.confirm(link), h.confirm(link))

        assert sorted([first.status_code, second.status_code]) == [303, 400]
        assert len(rows(h, LoginSession)) == 1


@pytest.mark.asyncio
async def test_eight_requests_for_one_address_send_one_mail(tmp_path, slow_allowlist_reads):
    async with email_app(tmp_path) as h:
        responses = await asyncio.gather(*(h.ask() for _ in range(8)))

        assert [response.status_code for response in responses] == [202] * 8
        assert len({response.content for response in responses}) == 1  # and say the same
        assert len(h.mailer.sent) == 1 and len(rows(h, EmailLoginChallenge)) == 1


@pytest.mark.asyncio
async def test_links_of_different_people_confirmed_at_once_all_work_and_stay_their_own(tmp_path, slow_allowlist_reads):
    async with email_app(tmp_path) as h:
        users = {ADDRESS: h.user_id}
        for number in range(5):
            address = f"person{number}@example.com"
            with h.factory() as db, db.begin():
                user = User(display_name=f"Person {number}")
                db.add(user)
                db.flush()
                allow(db, user, "email", address)
                users[address] = user.id
        links = {address: await h.ask_and_get_link(address) for address in users}
        clients = {address: h.new_client() for address in users}

        responses = await asyncio.gather(*(h.confirm(links[a], clients[a]) for a in users))

        assert [response.status_code for response in responses] == [303] * len(users)
        for address, user_id in users.items():
            assert (await clients[address].get("/auth/me")).json()["user_id"] == str(user_id)
        assert len(rows(h, IdentityLink)) == len(users)


@pytest.mark.asyncio
async def test_a_confirmation_racing_a_revocation_never_leaves_a_live_session(tmp_path):
    async with email_app(tmp_path) as h:
        for round_ in range(8):
            h.clock.advance(minutes=2)
            with h.factory() as db, db.begin():
                allow(db, db.get(User, h.user_id), "email", ADDRESS)
            link = await h.ask_and_get_link()
            client = h.new_client()

            def revoke_now():
                with h.factory() as db, db.begin():
                    return revoke(db, "email", ADDRESS, now=h.clock())

            response, revoked = await asyncio.gather(h.confirm(link, client), asyncio.to_thread(revoke_now))

            assert revoked is True
            # whichever came first, nothing the revocation covers is alive afterwards
            assert (await client.get("/auth/me")).status_code == 401, round_
            live = [row for row in rows(h, LoginSession) if row.revoked_at is None]
            assert live == [], round_
            assert response.status_code in (303, 400)
            assert (await h.confirm(link, h.new_client())).status_code == 400  # and the link is gone either way


# --- two ways to sign in, side by side -----------------------------------------------------------------


def give_the_user_a_telegram_identity(h):
    with h.factory() as db, db.begin():
        allow(db, db.get(User, h.user_id), "telegram", SUBJECT)


async def both_sessions(h):
    """The user signed in by Telegram in one browser and by email in another."""
    by_telegram, by_email = h.new_client(), h.new_client()
    assert (await sign_in(by_telegram, h.telegram)).status_code == 303
    assert (await h.sign_in(by_email)).status_code == 303
    return by_telegram, by_email


@pytest.mark.asyncio
async def test_both_ways_in_open_sessions_of_the_one_user_each_recording_its_own_identity(tmp_path):
    async with email_app(tmp_path, telegram=True) as h:
        give_the_user_a_telegram_identity(h)

        by_telegram, by_email = await both_sessions(h)

        for client in (by_telegram, by_email):
            assert (await client.get("/auth/me")).json()["user_id"] == str(h.user_id)
        links = {row.id: row.provider for row in rows(h, IdentityLink)}
        assert sorted(links.values()) == ["email", "telegram"]
        came_through = sorted(links[row.identity_link_id] for row in rows(h, LoginSession))
        assert came_through == ["email", "telegram"]
        assert len(rows(h, User)) == 1  # no second user came out of it


@pytest.mark.asyncio
async def test_revoking_the_email_identity_leaves_the_telegram_session_and_the_telegram_sign_in(tmp_path):
    async with email_app(tmp_path, telegram=True) as h:
        give_the_user_a_telegram_identity(h)
        by_telegram, by_email = await both_sessions(h)

        with h.factory() as db, db.begin():
            revoke(db, "email", ADDRESS, now=h.clock())

        assert (await by_email.get("/auth/me")).status_code == 401
        assert (await by_telegram.get("/auth/me")).status_code == 200
        again = h.new_client()
        assert (await sign_in(again, h.telegram)).status_code == 303  # Telegram still signs in
        assert (await h.ask()).status_code == 202 and len(h.mailer.sent) == 1  # email does not


@pytest.mark.asyncio
async def test_revoking_the_telegram_identity_leaves_the_email_session_and_the_email_sign_in(tmp_path):
    async with email_app(tmp_path, telegram=True) as h:
        give_the_user_a_telegram_identity(h)
        by_telegram, by_email = await both_sessions(h)

        with h.factory() as db, db.begin():
            revoke(db, "telegram", SUBJECT, now=h.clock())

        assert (await by_telegram.get("/auth/me")).status_code == 401
        assert (await by_email.get("/auth/me")).status_code == 200
        h.clock.advance(minutes=2)
        assert (await h.sign_in(h.new_client())).status_code == 303  # email still signs in
        assert (await sign_in(h.new_client(), h.telegram)).status_code == 403  # Telegram does not


@pytest.mark.asyncio
async def test_logging_out_of_one_session_does_not_end_the_other(tmp_path):
    async with email_app(tmp_path, telegram=True) as h:
        give_the_user_a_telegram_identity(h)
        by_telegram, by_email = await both_sessions(h)

        assert (await by_email.post("/auth/logout", headers={"Origin": ORIGIN})).status_code == 204

        assert (await by_email.get("/auth/me")).status_code == 401
        assert (await by_telegram.get("/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_a_failing_mail_server_does_not_touch_telegram_sign_in(tmp_path):
    async with email_app(tmp_path, telegram=True) as h:
        give_the_user_a_telegram_identity(h)
        h.mailer.failure = RuntimeError("mail server down")
        await h.ask()

        assert (await sign_in(h.client, h.telegram)).status_code == 303
        assert (await h.client.get("/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_a_failing_telegram_does_not_touch_email_sign_in(tmp_path):
    async with email_app(tmp_path, telegram=True) as h:
        give_the_user_a_telegram_identity(h)
        h.telegram.token_failure = lambda request: httpx.Response(503)
        started = await begin_sign_in(h.client)
        assert (await finish_sign_in(h.client, h.telegram, started)).status_code == 502

        assert (await h.sign_in(h.new_client())).status_code == 303


@pytest.mark.asyncio
async def test_what_one_way_in_issues_cannot_be_presented_to_the_other(tmp_path):
    async with email_app(tmp_path, telegram=True) as h:
        give_the_user_a_telegram_identity(h)
        link = await h.ask_and_get_link()
        started = await begin_sign_in(h.client)
        state = h.client.cookies.get("__Host-hh_login")

        # the email secret as a Telegram state, and Telegram's state as an email secret
        callback = await h.client.get(f"/auth/telegram/callback?state={link.token}&code=x")
        h.client.cookies.set("__Host-hh_login", link.token, domain="kitchen.example.test", path="/")
        stolen_state = await h.client.get(f"/auth/telegram/callback?state={link.token}&code=x")
        confirmed = await h.confirm(link, token=state[:43].ljust(43, "A"), challenge_id=link.challenge_id)

        assert callback.status_code == 400 and stolen_state.status_code == 400
        assert confirmed.status_code == 400
        assert rows(h, LoginSession) == []
        h.client.cookies.set("__Host-hh_login", state, domain="kitchen.example.test", path="/")
        assert (await finish_sign_in(h.client, h.telegram, started)).status_code == 303  # both still work
        assert (await h.confirm(link, h.new_client())).status_code == 303


@pytest.mark.asyncio
async def test_each_way_in_is_independently_on_or_off(tmp_path):
    from tests.auth_support import auth_app

    (tmp_path / "email_only").mkdir()
    (tmp_path / "telegram_only").mkdir()
    async with email_app(tmp_path / "email_only") as h:
        assert (await h.client.get("/auth/telegram/login")).status_code == 503
        assert (await h.sign_in()).status_code == 303
        assert (await h.client.get("/auth/me")).status_code == 200
        assert (await h.client.post("/auth/logout", headers={"Origin": ORIGIN})).status_code == 204
    async with auth_app(tmp_path / "telegram_only") as h:
        assert (await h.client.post("/auth/email/request", json={"email": ADDRESS})).status_code == 503
        assert (await sign_in(h.client, h.telegram)).status_code == 303
        assert (await h.client.get("/auth/me")).status_code == 200
        assert (await h.client.post("/auth/logout", headers={"Origin": ORIGIN})).status_code == 204


@pytest.mark.asyncio
async def test_taking_email_login_out_of_the_configuration_does_not_end_the_sessions_it_opened(tmp_path):
    # Switching a way in off stops new sign-ins through it. The sessions it opened end
    # the way any session does (expiry, logout) or by revoking the identity: what the
    # README says. This is the behaviour that sentence describes.
    from house_helper.web.app import create_app
    from house_helper.web.settings import WebSettings
    from tests.auth_support import FakeTelegram, auth_settings
    from tests.web_support import running, sqlite_url

    async with email_app(tmp_path) as h:
        await h.sign_in()
        token = h.client.cookies.get(SESSION_COOKIE)
        clock = h.clock
    telegram_only = auth_settings()
    app = create_app(
        WebSettings(database_url=sqlite_url(tmp_path / "email.db"), auth=telegram_only),
        provider_transport=FakeTelegram(clock, telegram_only).transport(),
        clock=clock,
    )
    async with running(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            client.cookies.set(SESSION_COOKIE, token, domain="kitchen.example.test", path="/")

            assert (await client.get("/auth/me")).status_code == 200  # still signed in
            assert (await client.post("/auth/email/request", json={"email": ADDRESS})).status_code == 503
            assert (await client.get("/auth/email/confirm?c=" + str(h.user_id))).status_code == 503
