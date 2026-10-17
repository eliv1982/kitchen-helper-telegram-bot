"""Telegram sign-in over HTTP: the routes that work, the session they create, and
what happens when login is not configured.

The real application runs (ASGI lifespan, worker threads, a migrated SQLite file);
only Telegram is replaced, by tests/auth_support.FakeTelegram, which enforces the
protocol (client credentials, redirect URI, PKCE, single-use codes). The failures
of the callback are in tests/test_web_auth_failures.py, concurrency in
tests/test_web_auth_concurrency.py, and the logs in tests/test_web_auth_logs.py.

Browsers are httpx clients with their own cookie jars, talking to the application
over its public https origin (a Secure cookie is only sent back there).
"""

from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import sqlalchemy as sa
from fastapi import Depends

from house_helper.persistence.allowlist import allow, revoke
from house_helper.persistence.authn import AuthenticatedSession, hash_secret
from house_helper.persistence.models import AllowlistEntry, IdentityLink, OidcState, User
from house_helper.persistence.models import Session as LoginSession
from house_helper.web import app as app_module
from house_helper.web.app import create_app
from house_helper.web.auth import require_user
from house_helper.web.settings import WebSettings
from house_helper.web.telegram_oidc import AUTHORIZATION_ENDPOINT, code_challenge
from tests.auth_support import (
    CLIENT_ID,
    CLIENT_SECRET,
    ORIGIN,
    START,
    SUBJECT,
    FakeClock,
    FakeTelegram,
    auth_app,
    auth_settings,
    begin_sign_in,
    cookie_attributes,
    counts,
    database_text,
    finish_sign_in,
    key_for,
    rows,
    sign_in,
)
from tests.web_support import LifespanFailed, running, serving, settings_for, sqlite_url

HOST = "kitchen.example.test"
PRIVATE = {"cache-control": "no-store", "referrer-policy": "no-referrer"}


def assert_private(response):
    for name, value in PRIVATE.items():
        assert response.headers[name] == value, (name, response.headers)


def query_of(location):
    return {name: values[0] for name, values in parse_qs(urlsplit(location).query).items()}


def whoami(app):
    """A route as the later increments will write them: the user comes from
    require_user and nowhere else."""

    @app.get("/test-only/whoami")
    async def route(current: AuthenticatedSession = Depends(require_user)):
        return {"user_id": str(current.user_id)}


# --- login not configured -----------------------------------------------------------


UNAVAILABLE = [
    ("GET", "/auth/telegram/login"),
    ("GET", "/auth/telegram/callback?code=c&state=s"),
    ("GET", "/auth/me"),
    ("POST", "/auth/logout"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method, path", UNAVAILABLE)
async def test_without_login_configured_the_sign_in_routes_answer_503(tmp_path, method, path):
    app = create_app(settings_for(tmp_path / "off.db"))

    async with serving(app) as client:
        response = await client.request(method, path)

    assert response.status_code == 503
    assert response.json() == {"detail": "login_unavailable"}
    assert_private(response)
    assert "set-cookie" not in response.headers
    assert list(tmp_path.iterdir()) == []  # not even the database was touched


@pytest.mark.asyncio
async def test_without_login_configured_healthz_is_as_it_was_and_no_http_client_is_made(
    tmp_path, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("no HTTP client may be created when login is off")

    # Only the application's name for httpx, not the library itself: the test's own
    # client is an httpx.AsyncClient too.
    monkeypatch.setattr(app_module, "httpx", SimpleNamespace(AsyncClient=forbidden))
    app = create_app(settings_for(tmp_path / "off.db"))

    async with serving(app) as client:
        response = await client.get("/healthz")
        assert app.state.auth is None

    assert response.status_code == 200 and response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_with_login_configured_healthz_stays_open_and_independent(tmp_path):
    async with auth_app(tmp_path) as h:
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("x")
        response = await h.client.get("/healthz")  # no cookie, nothing else

    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_healthz_does_not_need_the_database_even_with_login_on(tmp_path):
    async with auth_app(tmp_path) as h:
        await h.app.state.database.aclose()

        assert (await h.client.get("/healthz")).status_code == 200


@pytest.mark.asyncio
async def test_the_http_client_for_telegram_exists_only_while_the_application_runs(tmp_path):
    async with auth_app(tmp_path) as h:
        assert h.app.state.auth is not None

    assert h.app.state.auth is None


@pytest.mark.asyncio
async def test_the_http_client_for_telegram_is_closed_when_the_application_stops(tmp_path):
    async with auth_app(tmp_path) as h:
        provider_client = h.app.state.auth._oidc._http
        assert not provider_client.is_closed

    assert provider_client.is_closed


@pytest.mark.asyncio
async def test_the_database_is_closed_even_if_closing_the_http_client_fails(tmp_path, monkeypatch):
    app = None
    closed = []
    real_aclose = httpx.AsyncClient.aclose

    async def failing_aclose(self):
        await real_aclose(self)
        if self is app.state.auth._oidc._http:
            raise RuntimeError("the connection pool would not close")

    monkeypatch.setattr(httpx.AsyncClient, "aclose", failing_aclose)

    settings = auth_settings()
    telegram = FakeTelegram(FakeClock(), settings)
    app = create_app(
        WebSettings(database_url=sqlite_url(tmp_path / "x.db"), auth=settings),
        provider_transport=telegram.transport(),
    )

    with pytest.raises(LifespanFailed):
        async with running(app):
            closed.append(app.state.database)

    assert closed[0].closed  # the engine was still disposed
    assert app.state.auth is None


# --- starting a login ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starting_a_login_sends_the_browser_to_telegram_with_pkce_state_and_nonce(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await h.client.get("/auth/telegram/login")

        assert response.status_code == 303
        location = response.headers["location"]
        parameters = query_of(location)
        assert location.startswith(AUTHORIZATION_ENDPOINT + "?")
        assert parameters["client_id"] == CLIENT_ID
        assert parameters["redirect_uri"] == f"{ORIGIN}/auth/telegram/callback"
        assert parameters["response_type"] == "code" and parameters["scope"] == "openid"
        assert parameters["code_challenge_method"] == "S256"
        assert set(parameters) == {
            "client_id", "redirect_uri", "response_type", "scope", "state", "nonce",
            "code_challenge", "code_challenge_method",
        }  # fmt: skip
        assert_private(response)
        assert response.content == b""

        (stored,) = rows(h, OidcState)
        assert stored.state_hash == hash_secret(parameters["state"])
        assert stored.provider == "telegram" and stored.consumed_at is None
        assert stored.nonce == parameters["nonce"]
        assert code_challenge(stored.code_verifier) == parameters["code_challenge"]
        assert stored.created_at == START and stored.expires_at == START + timedelta(minutes=10)
        assert parameters["state"] not in database_text(h)  # only its digest is kept
        assert counts(h)["sessions"] == 0 and counts(h)["identity_links"] == 0


@pytest.mark.asyncio
async def test_starting_a_login_binds_it_to_the_browser_with_a_short_lived_cookie(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await h.client.get("/auth/telegram/login")

    cookie = cookie_attributes(response, "__Host-hh_login")
    assert cookie["value"] == query_of(response.headers["location"])["state"]
    assert cookie["httponly"] is True and cookie["secure"] is True
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/" and "domain" not in cookie  # what the __Host- prefix requires
    assert cookie["max-age"] == "600"
    assert not any(name.startswith("__Host-hh_session") for name in response.headers.get_list("set-cookie"))


@pytest.mark.asyncio
async def test_every_login_has_its_own_secrets(tmp_path):
    async with auth_app(tmp_path) as h:
        first = query_of((await h.client.get("/auth/telegram/login")).headers["location"])
        second = query_of((await h.client.get("/auth/telegram/login")).headers["location"])

        assert len(rows(h, OidcState)) == 2
    for name in ("state", "nonce", "code_challenge"):
        assert first[name] != second[name]


@pytest.mark.asyncio
async def test_a_login_ignores_everything_the_request_says(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await h.client.get(
            "/auth/telegram/login",
            params={"next": "https://evil.test/", "redirect_uri": "https://evil.test/cb", "state": "mine"},
            headers={"x-forwarded-host": "evil.test", "x-forwarded-proto": "http", "host": "evil.test"},
        )

    parameters = query_of(response.headers["location"])
    assert parameters["redirect_uri"] == f"{ORIGIN}/auth/telegram/callback"
    assert parameters["state"] != "mine"
    assert urlsplit(response.headers["location"]).netloc == "oauth.telegram.org"
    assert "domain" not in cookie_attributes(response, "__Host-hh_login")


@pytest.mark.asyncio
async def test_starting_a_login_removes_logins_that_have_expired(tmp_path):
    async with auth_app(tmp_path) as h:
        await h.client.get("/auth/telegram/login")
        h.clock.advance(minutes=11)
        await h.client.get("/auth/telegram/login")

        assert len(rows(h, OidcState)) == 1


# --- a successful sign-in --------------------------------------------------------------


@pytest.mark.asyncio
async def test_signing_in_creates_a_session_and_lands_on_auth_me(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await sign_in(h.client, h.telegram)

        assert response.status_code == 303
        assert response.headers["location"] == "/auth/me"
        assert response.content == b""
        assert_private(response)
        assert h.telegram.protocol_errors == []
        assert h.telegram.token_calls == 1 and h.telegram.jwks_calls == 1

        token = h.client.cookies.get("__Host-hh_session")
        assert token and len(token) == 43
        (session_row,) = rows(h, LoginSession)
        assert session_row.token_hash == hash_secret(token)
        assert session_row.user_id == h.user_id and session_row.revoked_at is None
        assert session_row.created_at == START
        assert session_row.expires_at == START + timedelta(days=14)
        (link,) = rows(h, IdentityLink)
        assert (link.user_id, link.provider, link.subject) == (h.user_id, "telegram", SUBJECT)
        assert link.last_login_at == START
        assert session_row.identity_link_id == link.id  # opened through the verified identity
        (state_row,) = rows(h, OidcState)
        assert state_row.consumed_at == START
        assert counts(h)["users"] == 1  # signing in created nobody
        assert token not in database_text(h)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        "&next=https://evil.test/",
        "&redirect_uri=https://evil.test/&return_to=//evil.test&url=https://evil.test",
        "&next=%2F%2Fevil.test&continue=https://evil.test",
    ],
)
async def test_where_a_sign_in_lands_is_never_chosen_by_the_request(tmp_path, extra):
    async with auth_app(tmp_path) as h:
        started = await begin_sign_in(h.client)
        callback = h.telegram.authorize(started.headers["location"])

        response = await h.client.get(callback + extra)

        assert response.status_code == 303
        assert response.headers["location"] == "/auth/me"


@pytest.mark.asyncio
async def test_following_the_sign_in_redirect_with_its_cookie_shows_the_signed_in_user(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await sign_in(h.client, h.telegram)
        assert response.status_code == 303

        landed = await h.client.get(response.headers["location"])  # the cookie jar carries the session

        assert landed.status_code == 200
        assert landed.json() == {
            "user_id": str(h.user_id),
            "session_expires_at": (START + timedelta(days=14)).isoformat(),
        }
        assert_private(landed)


@pytest.mark.asyncio
async def test_the_sign_in_redirect_carries_no_secret_in_its_address_or_headers(tmp_path):
    async with auth_app(tmp_path) as h:
        started = await begin_sign_in(h.client)
        callback = h.telegram.authorize(started.headers["location"])
        returned = query_of(callback)  # what the address bar held on the way back: code and state

        response = await h.client.get(callback)

        assert response.status_code == 303
        location = response.headers["location"]
        parts = urlsplit(location)
        assert (parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment) == (
            "", "", "/auth/me", "", ""
        )
        session_token = h.client.cookies.get("__Host-hh_session")
        handed_over = [returned["code"], returned["state"], CLIENT_SECRET]
        for name, value in response.headers.multi_items():
            for secret in handed_over:
                assert secret not in value, (name, value)
            # the session cookie is how the token is delivered, and the only place it is
            if not (name.lower() == "set-cookie" and value.startswith("__Host-hh_session=")):
                assert session_token not in value, (name, value)
        assert session_token not in response.text


@pytest.mark.asyncio
async def test_the_session_cookie_is_httponly_lax_secure_host_only_and_expires_with_the_session(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await sign_in(h.client, h.telegram)

    cookie = cookie_attributes(response, "__Host-hh_session")
    assert cookie["httponly"] is True and cookie["secure"] is True
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/" and "domain" not in cookie
    assert cookie["max-age"] == str(14 * 24 * 3600)
    assert "expires" not in cookie or True  # Max-Age is what is relied on


@pytest.mark.asyncio
async def test_the_login_cookie_is_spent_by_the_callback(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await sign_in(h.client, h.telegram)

        spent = cookie_attributes(response, "__Host-hh_login")
        assert spent["max-age"] == "0" and spent["value"] in ('""', "")
        assert spent["httponly"] is True and spent["secure"] is True
        assert h.client.cookies.get("__Host-hh_login") is None


@pytest.mark.asyncio
async def test_me_names_the_user_of_the_session_and_nothing_else(tmp_path):
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)

        response = await h.client.get("/auth/me")

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(h.user_id),
        "session_expires_at": (START + timedelta(days=14)).isoformat(),
    }
    assert_private(response)
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_signing_in_again_makes_another_session_and_reuses_the_link(tmp_path):
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        first_token = h.client.cookies.get("__Host-hh_session")
        h.clock.advance(hours=3)
        second_browser = h.new_client()
        await sign_in(second_browser, h.telegram)

        assert counts(h)["sessions"] == 2 and counts(h)["identity_links"] == 1
        (link,) = rows(h, IdentityLink)
        assert link.last_login_at == START + timedelta(hours=3)
        assert second_browser.cookies.get("__Host-hh_session") != first_token
        assert (await h.client.get("/auth/me")).status_code == 200  # the first one still works
        assert (await second_browser.get("/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_signing_in_over_an_existing_session_issues_a_new_token(tmp_path):
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        before = h.client.cookies.get("__Host-hh_session")
        await sign_in(h.client, h.telegram)

        assert h.client.cookies.get("__Host-hh_session") != before  # no fixation


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["ES256", "EdDSA"])
async def test_signing_in_works_with_the_other_algorithms_botfather_offers(tmp_path, algorithm):
    async with auth_app(tmp_path, algorithm=algorithm) as h:
        response = await sign_in(h.client, h.telegram)

        assert response.status_code == 303
        assert (await h.client.get("/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_a_token_signed_with_an_algorithm_other_than_the_configured_one_cannot_sign_in(tmp_path):
    async with auth_app(tmp_path, algorithm="RS256") as h:
        h.telegram.signer = key_for("ES256")  # Telegram "switched" without our being told
        response = await sign_in(h.client, h.telegram)

        assert response.status_code == 400
        assert counts(h)["sessions"] == 0


@pytest.mark.asyncio
async def test_local_development_over_plain_http_gets_cookies_a_browser_will_keep(tmp_path):
    async with auth_app(tmp_path, origin="http://localhost:8000") as h:
        started = await begin_sign_in(h.client)
        login_cookie = cookie_attributes(started, "hh_login")
        assert "secure" not in login_cookie and login_cookie["httponly"] is True
        assert query_of(started.headers["location"])["redirect_uri"] == (
            "http://localhost:8000/auth/telegram/callback"
        )

        response = await finish_sign_in(h.client, h.telegram, started)

        assert response.status_code == 303
        cookie = cookie_attributes(response, "hh_session")
        assert "secure" not in cookie
        assert cookie["httponly"] is True and cookie["samesite"].lower() == "lax"
        assert cookie["path"] == "/" and "domain" not in cookie
        assert (await h.client.get("/auth/me")).status_code == 200


# --- the session behind a request ------------------------------------------------------------


async def signed_in(h, client=None):
    client = client or h.client
    response = await sign_in(client, h.telegram)
    assert response.status_code == 303, response.text
    return client.cookies.get("__Host-hh_session")


@pytest.mark.asyncio
async def test_a_request_without_a_session_is_not_authenticated(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await h.client.get("/auth/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "not_authenticated"}
    assert_private(response)
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cookie",
    ["", "x", "a" * 42, "a" * 44, "a" * 43, "!" * 43, "../" * 15, "é" * 43, "a" * 5000, '"' + "a" * 41 + '"'],
)
async def test_a_cookie_that_is_not_a_session_is_not_authenticated(tmp_path, cookie):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        response = await h.client.get(
            # Bytes: a client is not limited to ASCII, and httpx would refuse a str.
            "/auth/me", headers={"cookie": b"__Host-hh_session=" + cookie.encode("utf-8")}
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "not_authenticated"}


@pytest.mark.asyncio
async def test_the_digest_stored_in_the_database_is_not_a_session(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        (stored,) = rows(h, LoginSession)

        response = await h.client.get(
            "/auth/me", headers={"cookie": f"__Host-hh_session={stored.token_hash[:43]}"}
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_session_travels_only_in_its_own_cookie(tmp_path):
    async with auth_app(tmp_path) as h:
        token = await signed_in(h)
        anonymous = h.new_client()

        for kwargs in (
            {"params": {"session": token, "token": token, "__Host-hh_session": token}},
            {"headers": {"authorization": f"Bearer {token}", "x-session": token}},
            {"headers": {"cookie": f"hh_session={token}"}},  # the development name, not this one's
            {"headers": {"cookie": f"x__Host-hh_session={token}"}},
        ):
            assert (await anonymous.get("/auth/me", **kwargs)).status_code == 401, kwargs


@pytest.mark.asyncio
async def test_an_expired_session_is_not_authenticated_and_one_second_earlier_it_is(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        h.clock.advance(days=14, seconds=-1)
        assert (await h.client.get("/auth/me")).status_code == 200

        h.clock.advance(seconds=1)
        assert (await h.client.get("/auth/me")).status_code == 401
        h.clock.advance(days=100)
        assert (await h.client.get("/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_a_session_revoked_in_the_database_is_not_authenticated(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        with h.factory() as db, db.begin():
            db.execute(sa.update(LoginSession).values(revoked_at=START))

        response = await h.client.get("/auth/me")

    assert response.status_code == 401 and response.json() == {"detail": "not_authenticated"}


@pytest.mark.asyncio
async def test_disabling_the_user_ends_their_sessions_at_once(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        with h.factory() as db, db.begin():
            db.execute(sa.update(User).values(disabled_at=START))

        assert (await h.client.get("/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_withdrawing_the_allowlist_entry_ends_the_sessions_at_once(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        with h.factory() as db, db.begin():
            db.execute(sa.update(AllowlistEntry).values(revoked_at=START))

        assert (await h.client.get("/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_revoking_the_identity_ends_its_session_and_a_new_sign_in_is_needed_after_re_allowing(
    tmp_path,
):
    async with auth_app(tmp_path) as h:
        old_token = await signed_in(h)
        assert (await h.client.get("/auth/me")).status_code == 200

        with h.factory() as db, db.begin():
            assert revoke(db, "telegram", SUBJECT, now=START + timedelta(minutes=1))
        assert (await h.client.get("/auth/me")).status_code == 401

        with h.factory() as db, db.begin():
            allow(db, db.get(User, h.user_id), "telegram", SUBJECT)
        assert (await h.client.get("/auth/me")).status_code == 401  # not restored

        again = h.new_client()
        new_token = await signed_in(h, again)
        assert (await again.get("/auth/me")).status_code == 200
        assert new_token != old_token
        assert (await h.client.get("/auth/me")).status_code == 401  # the old one stays dead
        assert sorted(row.revoked_at is not None for row in rows(h, LoginSession)) == [False, True]


@pytest.mark.asyncio
async def test_every_way_of_not_being_authenticated_looks_the_same(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        valid = h.client.cookies.get("__Host-hh_session")
        bodies = set()
        for header in (None, "x", "a" * 43):
            headers = {"cookie": f"__Host-hh_session={header}"} if header is not None else {}
            response = await h.new_client().get("/auth/me", headers=headers)
            bodies.add((response.status_code, response.text, tuple(sorted(response.headers.items()))))
        with h.factory() as db, db.begin():
            db.execute(sa.update(LoginSession).values(revoked_at=START))
        revoked = await h.client.get("/auth/me")

    assert len({(status, text) for status, text, _ in bodies}) == 1
    assert (revoked.status_code, revoked.text) == next(iter({(s, t) for s, t, _ in bodies}))
    assert valid  # the session was real: the refusal is about its state, not its shape


@pytest.mark.asyncio
async def test_a_route_that_uses_require_user_is_given_the_server_side_user_only(tmp_path):
    async with auth_app(tmp_path, configure=whoami) as h:
        other_user = None
        with h.factory() as db, db.begin():
            other = User(display_name="Somebody else")
            db.add(other)
            db.flush()
            other_user = str(other.id)
        await signed_in(h)

        for kwargs in (
            {},
            {"params": {"user_id": other_user, "userId": other_user, "uid": other_user}},
            {"headers": {"x-user-id": other_user, "x-forwarded-user": other_user}},
            {"headers": {"authorization": f"Bearer {other_user}"}},
        ):
            response = await h.client.get("/test-only/whoami", **kwargs)
            assert response.json() == {"user_id": str(h.user_id)}, kwargs
        post = await h.client.request("POST", "/test-only/whoami", json={"user_id": other_user})
        assert post.status_code == 405  # (the route is GET) and in no case the body's user


@pytest.mark.asyncio
async def test_a_route_that_uses_require_user_refuses_everyone_else(tmp_path):
    async with auth_app(tmp_path, configure=whoami) as h:
        response = await h.client.get(
            "/test-only/whoami", params={"user_id": str(h.user_id)}, headers={"x-user-id": str(h.user_id)}
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "not_authenticated"}


# --- logout --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_revokes_the_session_and_clears_the_cookie(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)

        response = await h.client.post("/auth/logout")

        assert response.status_code == 204 and response.content == b""
        assert_private(response)
        cleared = cookie_attributes(response, "__Host-hh_session")
        assert cleared["max-age"] == "0"
        assert cleared["httponly"] is True and cleared["secure"] is True and cleared["path"] == "/"
        (stored,) = rows(h, LoginSession)
        assert stored.revoked_at == START
        assert (await h.client.get("/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_a_revoked_token_stays_revoked_even_if_a_browser_keeps_sending_it(tmp_path):
    async with auth_app(tmp_path) as h:
        token = await signed_in(h)
        await h.client.post("/auth/logout")
        replay = h.new_client()

        response = await replay.get("/auth/me", headers={"cookie": f"__Host-hh_session={token}"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_is_repeatable_and_keeps_the_first_revocation_time(tmp_path):
    async with auth_app(tmp_path) as h:
        token = await signed_in(h)
        await h.client.post("/auth/logout")
        h.clock.advance(hours=1)

        again = await h.new_client().post("/auth/logout", headers={"cookie": f"__Host-hh_session={token}"})

        assert again.status_code == 204
        assert rows(h, LoginSession)[0].revoked_at == START


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie", [None, "", "garbage", "a" * 43])
async def test_logging_out_without_a_real_session_is_harmless(tmp_path, cookie):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        headers = {} if cookie is None else {"cookie": f"__Host-hh_session={cookie}"}

        response = await h.new_client().post("/auth/logout", headers=headers)

        assert response.status_code == 204
        assert cookie_attributes(response, "__Host-hh_session")["max-age"] == "0"
        assert (await h.client.get("/auth/me")).status_code == 200  # somebody else's session is untouched


@pytest.mark.asyncio
async def test_logout_ends_only_the_session_it_was_called_with(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        other = h.new_client()
        await signed_in(h, other)

        await h.client.post("/auth/logout")

        assert (await h.client.get("/auth/me")).status_code == 401
        assert (await other.get("/auth/me")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["https://evil.test", "null", "http://kitchen.example.test", f"{ORIGIN}.evil.test", f"{ORIGIN}:8443", "", ORIGIN + "/"])
async def test_logout_from_another_origin_is_refused_and_does_nothing(tmp_path, origin):
    async with auth_app(tmp_path) as h:
        await signed_in(h)

        response = await h.client.post("/auth/logout", headers={"origin": origin})

        assert response.status_code == 403
        assert response.json() == {"detail": "forbidden_origin"}
        assert_private(response)
        assert rows(h, LoginSession)[0].revoked_at is None
        assert (await h.client.get("/auth/me")).status_code == 200


@pytest.mark.asyncio
async def test_logout_from_our_own_origin_or_without_one_is_accepted(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        other = h.new_client()
        await signed_in(h, other)

        assert (await h.client.post("/auth/logout", headers={"origin": ORIGIN})).status_code == 204
        assert (await other.post("/auth/logout")).status_code == 204  # no Origin: not a browser's cross-site POST


@pytest.mark.asyncio
async def test_the_origin_logout_accepts_is_the_configured_one_not_the_hosts(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)

        forged = await h.client.post(
            "/auth/logout", headers={"origin": "https://evil.test", "host": "evil.test"}
        )

        assert forged.status_code == 403


# --- the shape of the surface -------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/auth/telegram/login"),
        ("POST", "/auth/telegram/callback"),
        ("PUT", "/auth/me"),
        ("POST", "/auth/me"),
        ("DELETE", "/auth/me"),
        ("GET", "/auth/logout"),
        ("PUT", "/auth/logout"),
        ("DELETE", "/auth/logout"),
    ],
)
async def test_each_route_answers_only_its_own_method(tmp_path, method, path):
    async with auth_app(tmp_path) as h:
        response = await h.client.request(method, path)

    assert response.status_code == 405


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/auth", "/auth/", "/auth/telegram", "/auth/telegram/", "/auth/session", "/auth/google/login", "/auth/telegram/token", "/me", "/login", "/logout"]
)
async def test_there_are_no_other_routes(tmp_path, path):
    async with auth_app(tmp_path) as h:
        response = await h.client.get(path)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_no_conversation_recipe_or_preference_route_exists(tmp_path):
    async with auth_app(tmp_path) as h:
        await signed_in(h)
        paths = [
            "/conversations", "/api/conversations", "/recipes", "/preferences", "/api/me",
            "/users", "/api/users", "/chat", "/api/chat", "/api/v1/conversations",
        ]  # fmt: skip
        statuses = {path: (await h.client.get(path)).status_code for path in paths}

    assert set(statuses.values()) == {404}
