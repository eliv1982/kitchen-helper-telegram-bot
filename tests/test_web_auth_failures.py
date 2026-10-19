"""Sign-in that must not succeed: a callback that is not ours, a token that is not
good, an identity that is not allowed, a provider or a database that fails.

Every case ends the same way, and each test asserts it: no session, no identity
link, no user created, and the login's state spent (a callback is never retried, the
person starts again). The answers are fixed codes that echo nothing of the request,
and the logs name a reason and never a secret.
"""

import asyncio

import httpx
import jwt
import pytest
import sqlalchemy as sa

from house_helper.persistence import authn
from house_helper.persistence.allowlist import allow
from house_helper.persistence.models import AllowlistEntry, IdentityLink, OidcState, User
from house_helper.persistence.models import Session as LoginSession
from tests.auth_support import (
    CLIENT_SECRET,
    SUBJECT,
    START,
    app_log_text,
    auth_app,
    begin_sign_in,
    cookie_attributes,
    counts,
    database_text,
    finish_sign_in,
    hmac_variant,
    jwks_of,
    key_for,
    logout_as,
    make_rsa,
    rows,
    sign_in,
    tampered_variant,
    unsigned_variant,
    watch_logs,
)
from tests.web_support import TIMEOUT

LOGIN_COOKIE = "__Host-hh_login"
SESSION_COOKIE = "__Host-hh_session"


def assert_refused(response, status, code):
    assert response.status_code == status, response.text
    assert response.json() == {"detail": code}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    cookies = response.headers.get_list("set-cookie")
    assert not any(cookie.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" not in cookie for cookie in cookies)
    assert cookie_attributes(response, LOGIN_COOKIE)["max-age"] == "0"  # the attempt's cookie is spent


def assert_nothing_granted(h, users=1):
    assert counts(h)["sessions"] == 0
    assert counts(h)["identity_links"] == 0
    assert counts(h)["users"] == users
    assert h.client.cookies.get(SESSION_COOKIE) is None


def state_rows(h):
    return rows(h, OidcState)


# --- callbacks that are not ours ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_callback_with_nothing_in_it_is_refused(tmp_path):
    async with auth_app(tmp_path) as h:
        response = await h.client.get("/auth/telegram/callback")

        assert_refused(response, 400, "invalid_login_request")
        assert_nothing_granted(h)
        assert h.telegram.requests == []  # Telegram was never asked anything


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["", " ", "a" * 129, "has space", "../../x", "<script>alert(1)</script>", "é" * 43, "a\x00b"])
async def test_a_state_that_is_not_one_of_ours_is_refused_without_an_echo(tmp_path, state):
    async with auth_app(tmp_path) as h:
        started = await begin_sign_in(h.client)

        response = await h.client.get(
            "/auth/telegram/callback", params={"state": state, "code": "<img src=x onerror=alert(1)>"}
        )

        assert_refused(response, 400, "invalid_login_request")
        assert "script" not in response.text and "<img" not in response.text
        assert state_rows(h)[0].consumed_at is None  # the real login is still pending
        assert h.telegram.requests == []
        assert started  # the browser's own login was not disturbed


@pytest.mark.asyncio
async def test_a_callback_from_a_browser_that_did_not_start_the_login_is_refused(tmp_path):
    # Login CSRF: the callback address of somebody else's login, opened in a browser
    # that never started it.
    async with auth_app(tmp_path) as h:
        attackers_browser = h.new_client()
        started = await begin_sign_in(attackers_browser)
        callback = h.telegram.authorize(started.headers["location"])

        response = await h.client.get(callback)  # the victim's browser: no login cookie

        assert_refused(response, 400, "invalid_login_request")
        assert_nothing_granted(h)
        assert h.telegram.token_calls == 0
        assert state_rows(h)[0].consumed_at is None  # not burnt by the stranger either

        # ...and the rightful browser can still finish it.
        assert (await attackers_browser.get(callback)).status_code == 303


@pytest.mark.asyncio
async def test_a_callback_whose_state_is_not_the_one_in_the_browsers_cookie_is_refused(tmp_path):
    async with auth_app(tmp_path) as h:
        other = h.new_client()
        theirs = await begin_sign_in(other)
        mine = await begin_sign_in(h.client)  # this browser has a login of its own
        callback = h.telegram.authorize(theirs.headers["location"])

        response = await h.client.get(callback)

        assert_refused(response, 400, "invalid_login_request")
        assert h.telegram.token_calls == 0
        assert all(row.consumed_at is None for row in state_rows(h))
        assert mine and counts(h)["sessions"] == 0


@pytest.mark.asyncio
async def test_a_state_nobody_started_is_refused_even_with_a_matching_cookie(tmp_path):
    async with auth_app(tmp_path) as h:
        forged = "f" * 43
        h.client.cookies.set(LOGIN_COOKIE, forged, domain="kitchen.example.test", path="/")

        response = await h.client.get("/auth/telegram/callback", params={"state": forged, "code": "c"})

        assert_refused(response, 400, "invalid_login_request")
        assert h.telegram.requests == []


@pytest.mark.asyncio
async def test_a_callback_cannot_be_replayed(tmp_path):
    async with auth_app(tmp_path) as h:
        started = await begin_sign_in(h.client)
        callback = h.telegram.authorize(started.headers["location"])
        state = h.client.cookies.get(LOGIN_COOKIE)
        first = await h.client.get(callback)
        assert first.status_code == 303
        h.client.cookies.set(LOGIN_COOKIE, state, domain="kitchen.example.test", path="/")  # the attacker has it too

        replay = await h.client.get(callback)

        assert_refused(replay, 400, "invalid_login_request")
        assert h.telegram.token_calls == 1  # the code was exchanged once
        assert counts(h)["sessions"] == 1 and counts(h)["identity_links"] == 1


@pytest.mark.asyncio
async def test_a_login_expires_after_ten_minutes_exactly(tmp_path):
    async with auth_app(tmp_path) as h:
        started = await begin_sign_in(h.client)
        callback = h.telegram.authorize(started.headers["location"])
        h.clock.advance(minutes=10)  # at the instant it expires

        response = await h.client.get(callback)

        assert_refused(response, 400, "invalid_login_request")
        assert_nothing_granted(h)
        assert h.telegram.token_calls == 0
        assert state_rows(h)[0].consumed_at is None  # a refusal marks nothing


@pytest.mark.asyncio
async def test_a_login_finished_just_inside_its_ten_minutes_succeeds(tmp_path):
    async with auth_app(tmp_path) as h:
        started = await begin_sign_in(h.client)
        callback = h.telegram.authorize(started.headers["location"])
        h.clock.advance(minutes=9, seconds=59)

        assert (await h.client.get(callback)).status_code == 303


@pytest.mark.asyncio
async def test_the_cookie_that_binds_a_login_expires_with_it(tmp_path):
    async with auth_app(tmp_path) as h:
        started = await begin_sign_in(h.client)

        assert cookie_attributes(started, LOGIN_COOKIE)["max-age"] == "600"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        {"error": "access_denied"},
        {"error": "access_denied", "error_description": "<b>user said no</b>"},
        {"error": "server_error"},
        {"error": ""},
        {"error": "access_denied", "code": "c-looks-fine"},
    ],
)
async def test_an_error_from_the_provider_ends_the_login_and_spends_its_state(tmp_path, query):
    async with auth_app(tmp_path) as h:
        started = await begin_sign_in(h.client)
        state = h.client.cookies.get(LOGIN_COOKIE)

        response = await h.client.get("/auth/telegram/callback", params={**query, "state": state})

        assert_refused(response, 400, "invalid_login_request")
        assert "denied" not in response.text and "user said no" not in response.text
        assert h.telegram.token_calls == 0
        assert state_rows(h)[0].consumed_at == START
        # The same login cannot be picked up again with a code afterwards.
        h.client.cookies.set(LOGIN_COOKIE, state, domain="kitchen.example.test", path="/")
        replay = await h.client.get(h.telegram.authorize(started.headers["location"]))
        assert_refused(replay, 400, "invalid_login_request")
        assert_nothing_granted(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [None, "", "has space", "tab\there", "x" * 2049, "é", "a\nb"])
async def test_a_callback_without_a_usable_code_spends_the_login_and_asks_nothing(tmp_path, code):
    async with auth_app(tmp_path) as h:
        await begin_sign_in(h.client)
        state = h.client.cookies.get(LOGIN_COOKIE)
        params = {"state": state} if code is None else {"state": state, "code": code}

        response = await h.client.get("/auth/telegram/callback", params=params)

        assert_refused(response, 400, "invalid_login_request")
        assert h.telegram.requests == []
        assert state_rows(h)[0].consumed_at == START
        assert_nothing_granted(h)


# --- an ID token that is not good -------------------------------------------------------------------------


def with_claims(**changes):
    def hook(claims):
        for name, value in changes.items():
            if value is None:
                claims.pop(name, None)
            else:
                claims[name] = value
        return claims

    return hook


def use_signer(telegram, key):
    telegram.signer = key


NOW = int(START.timestamp())


def publish_weak_key(telegram):
    """Telegram (somehow) publishes a 1024-bit RSA key and signs with it."""
    weak = make_rsa(kid="oidc-1", bits=1024)
    telegram.signer = weak
    telegram.jwks_document = jwks_of(weak)

BAD_TOKENS = [
    ("nonce of another login", lambda t: setattr(t, "claims_hook", with_claims(nonce="another")), "wrong_nonce"),
    ("no nonce", lambda t: setattr(t, "claims_hook", with_claims(nonce=None)), "wrong_nonce"),
    ("another issuer", lambda t: setattr(t, "claims_hook", with_claims(iss="https://evil.test")), "wrong_issuer"),
    ("issuer with a slash", lambda t: setattr(t, "claims_hook", with_claims(iss="https://oauth.telegram.org/")), "wrong_issuer"),
    ("another audience", lambda t: setattr(t, "claims_hook", with_claims(aud="999")), "wrong_audience"),
    ("two audiences", lambda t: setattr(t, "claims_hook", with_claims(aud=["1234567890", "999"])), "wrong_audience"),
    ("expired", lambda t: setattr(t, "claims_hook", with_claims(exp=NOW - 1)), "expired"),
    ("expiring right now", lambda t: setattr(t, "claims_hook", with_claims(exp=NOW)), "expired"),
    ("no expiry", lambda t: setattr(t, "claims_hook", with_claims(exp=None)), "missing_expiry"),
    ("issued in the future", lambda t: setattr(t, "claims_hook", with_claims(iat=NOW + 3600)), "issued_in_the_future"),
    ("no subject", lambda t: setattr(t, "claims_hook", with_claims(sub=None)), "missing_subject"),
    ("numeric subject", lambda t: setattr(t, "claims_hook", with_claims(sub=1234123412341234123)), "missing_subject"),
    ("signed by another key with our kid", lambda t: use_signer(t, make_rsa(kid="oidc-1")), "bad_signature"),
    ("signed by a key that is not published", lambda t: use_signer(t, make_rsa(kid="oidc-42")), "unknown_key"),
    ("a published key that is too small", lambda t: publish_weak_key(t), "invalid_token"),
    ("unsigned (alg none)", lambda t: setattr(t, "token_hook", unsigned_variant), "algorithm_not_allowed"),
    ("alg none, any case", lambda t: setattr(t, "token_hook", lambda tok: unsigned_variant(tok, alg="NoNe")), "algorithm_not_allowed"),
    (
        "HMAC with the public key as the secret",
        lambda t: setattr(
            t, "token_hook", lambda tok: hmac_variant(tok, key_for("RS256").jwk["n"].encode())
        ),
        "algorithm_not_allowed",
    ),
    ("another algorithm", lambda t: use_signer(t, key_for("ES256")), "algorithm_not_allowed"),
    ("payload changed after signing", lambda t: setattr(t, "token_hook", lambda tok: tampered_variant(tok, sub="1")), "bad_signature"),
    ("not a token at all", lambda t: setattr(t, "token_hook", lambda tok: "this-is-not-a-jwt"), "malformed"),
    ("embedded jwk", lambda t: setattr(t, "header_hook", lambda h: {**h, "jwk": key_for("RS256").jwk}), "unsupported_header"),
    ("jku", lambda t: setattr(t, "header_hook", lambda h: {**h, "jku": "https://evil.test/jwks"}), "unsupported_header"),
]


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:The RSA key is 1024 bits")  # one case signs with such a key
@pytest.mark.parametrize("name, damage, reason", BAD_TOKENS, ids=[case[0] for case in BAD_TOKENS])
async def test_a_login_with_a_bad_id_token_grants_nothing(tmp_path, caplog, name, damage, reason):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        damage(h.telegram)
        tokens = []
        real_hook = h.telegram.token_hook
        h.telegram.token_hook = lambda tok: tokens.append(tok) or (real_hook(tok) if real_hook else tok)

        response = await sign_in(h.client, h.telegram)

        assert_refused(response, 400, "login_failed")
        assert_nothing_granted(h)
        assert state_rows(h)[0].consumed_at == START
        assert h.telegram.token_calls == 1 and h.telegram.jwks_calls == 1
        logged = app_log_text(caplog)
        assert f"The ID token was rejected: {reason}" in logged
        assert all(token not in logged and token not in response.text for token in tokens)


@pytest.mark.asyncio
async def test_a_token_that_is_not_valid_json_in_its_claims_grants_nothing(tmp_path):
    async with auth_app(tmp_path) as h:
        key = key_for("RS256")
        h.telegram.token_hook = lambda tok: jwt.PyJWS().encode(
            b"not json", key.private, algorithm="RS256", headers={"kid": key.kid}
        )

        assert_refused(await sign_in(h.client, h.telegram), 400, "login_failed")
        assert_nothing_granted(h)


# --- an identity that is not allowed -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verified_identity_that_is_not_on_the_allowlist_gets_nothing(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        response = await sign_in(h.client, h.telegram, subject="9999999999")

        assert_refused(response, 403, "not_allowed")
        assert_nothing_granted(h)
        assert state_rows(h)[0].consumed_at == START
        logged = app_log_text(caplog)
        assert "9999999999" not in logged and "9999999999" not in response.text
        assert "not on the allowlist" in logged


@pytest.mark.asyncio
async def test_the_username_or_name_of_an_allowed_person_does_not_admit_anyone(tmp_path):
    # A token for a stranger whose profile claims say they are the allowed person.
    async with auth_app(tmp_path) as h:
        h.telegram.claims_hook = with_claims(
            sub="5555555555",
            id=int(SUBJECT),
            preferred_username=SUBJECT,
            name="Елена",
            email="elena@example.com",
            email_verified=True,
            phone_number="971577777777",
        )

        response = await sign_in(h.client, h.telegram, subject="5555555555")

        assert_refused(response, 403, "not_allowed")
        assert_nothing_granted(h)


@pytest.mark.asyncio
async def test_a_revoked_identity_gets_nothing(tmp_path):
    async with auth_app(tmp_path) as h:
        with h.factory() as db, db.begin():
            db.execute(sa.update(AllowlistEntry).values(revoked_at=START))

        assert_refused(await sign_in(h.client, h.telegram), 403, "not_allowed")
        assert_nothing_granted(h)


@pytest.mark.asyncio
async def test_a_disabled_user_gets_nothing(tmp_path):
    async with auth_app(tmp_path) as h:
        with h.factory() as db, db.begin():
            db.execute(sa.update(User).values(disabled_at=START))

        assert_refused(await sign_in(h.client, h.telegram), 403, "not_allowed")
        assert_nothing_granted(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["@someone", "someone@example.com", "é" * 5, "x" * 300, "has space", "tab\t"])
async def test_a_subject_that_cannot_be_an_identity_gets_nothing(tmp_path, subject):
    async with auth_app(tmp_path) as h:
        assert_refused(await sign_in(h.client, h.telegram, subject=subject), 403, "not_allowed")
        assert_nothing_granted(h)


@pytest.mark.asyncio
async def test_each_allowed_person_signs_in_as_themselves_and_nobody_is_merged(tmp_path):
    async with auth_app(tmp_path) as h:
        with h.factory() as db, db.begin():
            second = User(display_name="Somebody else")
            db.add(second)
            db.flush()
            allow(db, second, "telegram", "777")
            second_id = second.id
        other_browser = h.new_client()

        await sign_in(h.client, h.telegram)
        await sign_in(other_browser, h.telegram, subject="777")

        mine = (await h.client.get("/auth/me")).json()["user_id"]
        theirs = (await other_browser.get("/auth/me")).json()["user_id"]
        assert (mine, theirs) == (str(h.user_id), str(second_id))
        assert counts(h)["users"] == 2 and counts(h)["identity_links"] == 2
        links = {(link.subject, link.user_id) for link in rows(h, IdentityLink)}
        assert links == {(SUBJECT, h.user_id), ("777", second_id)}


@pytest.mark.asyncio
async def test_a_refused_login_does_not_touch_the_last_login_of_a_known_identity(tmp_path):
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        with h.factory() as db, db.begin():
            db.execute(sa.update(AllowlistEntry).values(revoked_at=START))
        h.clock.advance(days=1)

        response = await sign_in(h.new_client(), h.telegram)

        assert response.status_code == 403
        assert rows(h, IdentityLink)[0].last_login_at == START


# --- Telegram failing ---------------------------------------------------------------------------------------------


def status(code, **kwargs):
    return lambda request: httpx.Response(code, **kwargs)


def raising(exception):
    def raiser(request):
        raise exception

    return raiser


PROVIDER_OUTAGES = [
    ("token endpoint 500", "token_failure", status(500, text="oops")),
    ("token endpoint 503", "token_failure", status(503)),
    ("token endpoint timeout", "token_failure", raising(httpx.ReadTimeout("slow"))),
    ("token endpoint unreachable", "token_failure", raising(httpx.ConnectError("no route"))),
    ("token endpoint redirects", "token_failure", status(307, headers={"location": "https://evil.test/"})),
    ("token endpoint answers HTML", "token_failure", status(200, text="<html>")),
    ("token response without id_token", "token_failure", status(200, json={"access_token": "a"})),
    ("key set 500", "jwks_failure", status(500)),
    ("key set timeout", "jwks_failure", raising(httpx.ConnectTimeout("slow"))),
    ("key set garbage", "jwks_failure", status(200, text="{")),
    ("key set redirects", "jwks_failure", status(302, headers={"location": "https://evil.test/jwks"})),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name, hook, behaviour", PROVIDER_OUTAGES, ids=[case[0] for case in PROVIDER_OUTAGES])
async def test_a_provider_that_fails_grants_nothing_and_says_so(tmp_path, caplog, name, hook, behaviour):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        setattr(h.telegram, hook, behaviour)

        response = await sign_in(h.client, h.telegram)

        assert_refused(response, 502, "provider_unavailable")
        assert_nothing_granted(h)
        assert state_rows(h)[0].consumed_at == START
        assert "evil.test" not in [url for _, url in h.telegram.requests]  # no redirect was followed
        logged = app_log_text(caplog)
        assert CLIENT_SECRET not in logged
        assert SUBJECT not in logged


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 401, 403])
async def test_a_provider_that_refuses_the_code_fails_the_login_without_detail(tmp_path, caplog, code):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        h.telegram.token_failure = status(
            code, json={"error": "invalid_client", "error_description": "description-marker"}
        )

        response = await sign_in(h.client, h.telegram)

        assert_refused(response, 400, "login_failed")
        assert "description-marker" not in response.text + app_log_text(caplog)
        assert f"HTTP {code}" in app_log_text(caplog)  # what an operator needs to find a bad secret
        assert_nothing_granted(h)


@pytest.mark.asyncio
async def test_after_a_provider_outage_the_person_starts_again_and_it_works(tmp_path):
    async with auth_app(tmp_path) as h:
        h.telegram.token_failure = status(503)
        started = await begin_sign_in(h.client)
        callback = h.telegram.authorize(started.headers["location"])
        state = h.client.cookies.get(LOGIN_COOKIE)
        assert_refused(await h.client.get(callback), 502, "provider_unavailable")

        # The same callback address is dead (its state is spent, its code is not
        # to be exchanged twice)...
        h.client.cookies.set(LOGIN_COOKIE, state, domain="kitchen.example.test", path="/")
        assert_refused(await h.client.get(callback), 400, "invalid_login_request")

        # ...and a fresh login, with the provider back, is fine.
        h.telegram.token_failure = None
        assert (await sign_in(h.client, h.telegram)).status_code == 303
        assert counts(h)["sessions"] == 1


@pytest.mark.asyncio
async def test_a_callback_waiting_for_telegram_holds_no_database_resource(tmp_path):
    async with auth_app(tmp_path) as h:
        arrived, release = h.telegram.hold_token_requests()
        started = await begin_sign_in(h.client)
        waiting = asyncio.ensure_future(finish_sign_in(h.client, h.telegram, started))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        # Telegram is "slow". Meanwhile the database is free for everyone: a write
        # (another login starting) and a read (an unauthenticated lookup) go through.
        busy = h.new_client()
        assert (await asyncio.wait_for(begin_sign_in(busy), TIMEOUT)).status_code == 303
        assert (await asyncio.wait_for(busy.get("/auth/me"), TIMEOUT)).status_code == 401
        assert not waiting.done()

        release.set()
        response = await asyncio.wait_for(waiting, TIMEOUT)
        assert response.status_code == 303


# --- the database failing ----------------------------------------------------------------------------------------------


def make_session_inserts_fail(exception):
    """Fail the INSERT of a login session, after the identity link was flushed in
    the same transaction. Returns a function that removes the failure."""

    def boom(mapper, connection, target):
        raise exception

    sa.event.listen(LoginSession, "before_insert", boom)
    return lambda: sa.event.remove(LoginSession, "before_insert", boom)


@pytest.mark.asyncio
async def test_a_database_error_while_completing_a_login_rolls_everything_back(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        remove = make_session_inserts_fail(
            sa.exc.OperationalError("INSERT INTO sessions", {"token_hash": "SECRET-PARAMETER"}, Exception("disk I/O error"))
        )
        try:
            response = await sign_in(h.client, h.telegram)
        finally:
            remove()

        assert_refused(response, 503, "temporarily_unavailable")
        assert_nothing_granted(h)  # in particular: the link that was flushed first is gone
        assert state_rows(h)[0].consumed_at == START
        logged = app_log_text(caplog)
        assert "OperationalError" in logged and "SECRET-PARAMETER" not in logged
        assert "INSERT INTO" not in logged

        # The system recovers: a new login after the fault works.
        assert (await sign_in(h.client, h.telegram)).status_code == 303
        assert counts(h)["sessions"] == 1 and counts(h)["identity_links"] == 1


@pytest.mark.asyncio
async def test_an_unexpected_error_while_completing_a_login_rolls_back_and_leaks_nothing(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        remove = make_session_inserts_fail(RuntimeError("secret-detail-of-a-bug"))
        try:
            response = await sign_in(h.client, h.telegram)
        finally:
            remove()

        assert_refused(response, 500, "server_error")
        assert_nothing_granted(h)
        logged = app_log_text(caplog)
        assert "RuntimeError" in logged
        assert "secret-detail-of-a-bug" not in logged + response.text


@pytest.mark.asyncio
async def test_a_failure_to_spend_the_state_stops_the_login_before_telegram_is_asked(tmp_path, monkeypatch):
    async with auth_app(tmp_path) as h:
        def broken(session, provider, state, *, now):
            raise sa.exc.OperationalError("UPDATE oidc_states", {"state_hash": "x"}, Exception("database is locked"))

        monkeypatch.setattr(authn, "consume_oidc_login", broken)

        response = await sign_in(h.client, h.telegram)

        assert_refused(response, 503, "temporarily_unavailable")
        assert h.telegram.requests == []
        assert_nothing_granted(h)


@pytest.mark.asyncio
async def test_a_database_error_when_starting_a_login_leaks_no_secret(tmp_path, caplog, monkeypatch):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        with h.factory() as db, db.begin():
            db.execute(
                sa.text(
                    "CREATE TRIGGER refuse_logins BEFORE INSERT ON oidc_states "
                    "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
                )
            )
        seen = {}
        real = authn.begin_oidc_login

        def recording(session, provider, state, nonce, code_verifier, **options):
            seen.update(state=state, nonce=nonce, code_verifier=code_verifier)
            try:
                return real(session, provider, state, nonce, code_verifier, **options)
            except Exception as exc:
                seen["message"] = str(exc)  # what SQLAlchemy would have put in a log
                raise

        monkeypatch.setattr(authn, "begin_oidc_login", recording)

        response = await h.client.get("/auth/telegram/login")

        assert response.status_code == 503
        assert response.json() == {"detail": "temporarily_unavailable"}
        assert "set-cookie" not in response.headers  # no login cookie for a login that was not stored
        # The point of the test: SQLAlchemy's own message carries the secrets...
        assert seen["nonce"] in seen["message"] and seen["code_verifier"] in seen["message"]
        # ...and the application's logs do not.
        logged = app_log_text(caplog)
        assert "IntegrityError" in logged
        for secret in (seen["state"], seen["nonce"], seen["code_verifier"]):
            assert secret not in logged and secret not in response.text
        assert counts(h)["oidc_states"] == 0


@pytest.mark.asyncio
async def test_with_the_database_closed_every_route_that_needs_it_says_unavailable(tmp_path):
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        started = await begin_sign_in(h.new_client())
        await h.app.state.database.aclose()

        login = await h.client.get("/auth/telegram/login")
        me = await h.client.get("/auth/me")
        logout = await h.client.post("/auth/logout", headers=logout_as(h.user_id))

        assert (login.status_code, me.status_code, logout.status_code) == (503, 503, 503)
        assert login.json() == me.json() == logout.json() == {"detail": "temporarily_unavailable"}
        assert "set-cookie" not in logout.headers  # the session could not be checked: the cookie is not cleared either
        assert started.status_code == 303


@pytest.mark.asyncio
async def test_a_logout_that_cannot_check_the_owner_of_the_session_ends_nothing_and_clears_nothing(tmp_path):
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        with h.factory() as db, db.begin():
            db.execute(sa.text("ALTER TABLE sessions RENAME TO sessions_gone"))

        response = await h.client.post("/auth/logout", headers=logout_as(h.user_id))

        assert response.status_code == 503
        assert response.json() == {"detail": "temporarily_unavailable"}
        assert "set-cookie" not in response.headers
        with h.factory() as db, db.begin():
            db.execute(sa.text("ALTER TABLE sessions_gone RENAME TO sessions"))
        assert [row.revoked_at for row in rows(h, LoginSession)] == [None]  # not ended "to be safe"


@pytest.mark.asyncio
async def test_a_session_is_not_reported_as_logged_out_when_the_database_is_down(tmp_path):
    # 503, not 401: a client must not take an outage for "your session ended".
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        with h.factory() as db, db.begin():
            db.execute(sa.text("ALTER TABLE sessions RENAME TO sessions_gone"))

        response = await h.client.get("/auth/me")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_the_stored_nonce_and_verifier_are_the_only_secrets_kept_in_the_clear(tmp_path):
    async with auth_app(tmp_path) as h:
        started = await begin_sign_in(h.client)
        callback = h.telegram.authorize(started.headers["location"])
        await h.client.get(callback)
        text = database_text(h)
        code = callback.split("code=")[1].split("&")[0]
        token = h.client.cookies.get(SESSION_COOKIE)

        assert code not in text  # the authorization code is never stored
        assert token not in text
        assert h.client.cookies.get(LOGIN_COOKIE) is None
        assert CLIENT_SECRET not in text
        assert "access-token-must-never-be-stored" not in text
