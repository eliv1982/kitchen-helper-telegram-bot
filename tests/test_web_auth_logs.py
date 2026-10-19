"""No authentication material in the logs.

One long scenario drives every kind of sign-in (a success, a replay, an ID token
that is bad in several ways, a stranger, a provider that fails, a database that
fails, a logout) at DEBUG level, collects every secret that existed in the run (the
state, nonce and verifier of each login, each authorization code, each ID token and
access token, the session tokens, their digests, the client secret and the
Authorization header value) and requires that none of them appears in what the
application's loggers, httpx and the servers' loggers said, tracebacks included.

SQLAlchemy's engine logger is deliberately not part of the capture: with echo on it
prints the bound parameters of every statement, which here include the nonce and the
PKCE verifier (the two secrets that must be stored in the clear). The application
never turns it on; tests/test_web_auth_failures.py shows the application also does
not log SQLAlchemy's error messages, which carry the same parameters.

The second half is the access-log filter: the callback URL carries the code and the
state, and uvicorn writes the request line.
"""

import base64
import logging
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from house_helper.persistence.models import OidcState
from house_helper.persistence.models import Session as LoginSession
from house_helper.web.app import create_app
from house_helper.web.logsafety import ACCESS_LOGGER, RedactAuthQuery, install_access_log_redaction
from tests.auth_support import (
    CLIENT_ID,
    CLIENT_SECRET,
    app_log_text,
    auth_app,
    begin_sign_in,
    logout_as,
    make_rsa,
    rows,
    sign_in,
    watch_logs,
)
from tests.web_support import running, settings_for

SESSION_COOKIE = "__Host-hh_session"
LOGIN_COOKIE = "__Host-hh_login"


@pytest.mark.asyncio
async def test_nothing_secret_is_logged_whatever_happens(tmp_path, caplog):
    watch_logs(caplog)
    secrets: set[str] = {CLIENT_SECRET}
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    secrets.add(basic)
    tokens: list[str] = []

    def remember(token):
        tokens.append(token)
        return token

    async with auth_app(tmp_path) as h:
        h.telegram.token_hook = remember

        async def one(client, subject="1234123412341234123"):
            started = await begin_sign_in(client)
            secrets.add(client.cookies.get(LOGIN_COOKIE))
            callback = h.telegram.authorize(started.headers["location"], subject)
            secrets.add(callback.split("code=")[1].split("&")[0])
            return callback, await client.get(callback)

        # a success, then its replay and a logout
        callback, ok = await one(h.client)
        assert ok.status_code == 303
        secrets.add(h.client.cookies.get(SESSION_COOKIE))
        state = callback.split("state=")[1]
        h.client.cookies.set(LOGIN_COOKIE, state, domain="kitchen.example.test", path="/")
        assert (await h.client.get(callback)).status_code == 400
        assert (await h.client.get("/auth/me")).status_code == 200
        assert (await h.client.post("/auth/logout")).status_code == 400  # no X-Expected-User
        assert (await h.client.post("/auth/logout", headers=logout_as(uuid4()))).status_code == 409  # not this session's user
        assert (await h.client.post("/auth/logout", headers=logout_as(h.user_id))).status_code == 204

        # a stranger, a bad nonce, a foreign key, an unsigned token
        assert (await one(h.new_client(), "9999999999"))[1].status_code == 403
        h.telegram.claims_hook = lambda claims: {**claims, "nonce": "another"}
        assert (await one(h.new_client()))[1].status_code == 400
        h.telegram.claims_hook = None
        h.telegram.signer = make_rsa(kid="oidc-1")
        assert (await one(h.new_client()))[1].status_code == 400
        h.telegram.signer = h.telegram.keys[0]
        h.telegram.token_hook = lambda token: remember(token.rsplit(".", 1)[0] + ".")
        assert (await one(h.new_client()))[1].status_code == 400
        h.telegram.token_hook = remember

        # the provider failing, and the database failing under a login
        h.telegram.token_failure = lambda request: httpx.Response(503, text=CLIENT_SECRET)
        assert (await one(h.new_client()))[1].status_code == 502
        h.telegram.token_failure = None
        with h.factory() as db, db.begin():
            db.execute(sa.text("ALTER TABLE sessions RENAME TO sessions_gone"))
        assert (await one(h.new_client()))[1].status_code == 503
        with h.factory() as db, db.begin():
            db.execute(sa.text("ALTER TABLE sessions_gone RENAME TO sessions"))

        # everything that is stored, and everything the browsers held
        for state_row in rows(h, OidcState):
            secrets.update({state_row.state_hash, state_row.nonce, state_row.code_verifier})
        for session_row in rows(h, LoginSession):
            secrets.add(session_row.token_hash)
        secrets.update(tokens)

    logged = app_log_text(caplog)
    secrets.discard(None)
    assert len(secrets) > 25  # the scenario really did produce a crowd of secrets
    leaked = sorted(secret for secret in secrets if secret and secret in logged)
    assert leaked == []
    assert "access-token-must-never-be-stored" not in logged
    # ...while the log is not empty: the operator can follow what happened.
    assert "signed in" in logged
    assert "The ID token was rejected: wrong_nonce" in logged
    assert "not on the allowlist" in logged
    assert "HTTP 503" in logged


@pytest.mark.asyncio
async def test_what_httpx_logs_of_the_calls_to_telegram_is_only_the_endpoint_and_status(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)

    telegram_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "httpx" and "oauth.telegram.org" in record.getMessage()
    ]
    assert telegram_lines == [
        'HTTP Request: POST https://oauth.telegram.org/token "HTTP/1.1 200 OK"',
        'HTTP Request: GET https://oauth.telegram.org/.well-known/jwks.json "HTTP/1.1 200 OK"',
    ]


@pytest.mark.asyncio
async def test_the_user_id_is_logged_on_sign_in_and_nothing_personal_is(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)

    logged = app_log_text(caplog)
    assert f"User {h.user_id} signed in" in logged
    for personal in ("1234123412341234123", "Елена"):  # the Telegram sub, the display name
        assert personal not in logged


# --- the access log -------------------------------------------------------------------


def uvicorn_access_record(path: str) -> logging.LogRecord:
    """A record shaped the way uvicorn builds one:
    logger.info('%s - "%s %s HTTP/%s" %d', client, method, path_with_query, version, status)."""
    return logging.LogRecord(
        ACCESS_LOGGER, logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", path, "1.1", 303), None,
    )  # fmt: skip


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/auth/telegram/callback?code=SECRETCODE&state=SECRETSTATE", "/auth/telegram/callback?[redacted]"),
        ("/auth/telegram/callback?error=access_denied&state=SECRETSTATE", "/auth/telegram/callback?[redacted]"),
        ("/auth/telegram/login?next=SECRET", "/auth/telegram/login?[redacted]"),
        ("/auth/me?session=SECRET", "/auth/me?[redacted]"),
        ("/auth/telegram/callback?", "/auth/telegram/callback?[redacted]"),
        ("/auth/telegram/callback", "/auth/telegram/callback"),
        ("/auth/me", "/auth/me"),
        ("/healthz", "/healthz"),
        ("/healthz?probe=1", "/healthz?probe=1"),
        ("/something/else?code=visible-because-not-ours", "/something/else?code=visible-because-not-ours"),
        ("/author/x?y=1", "/author/x?y=1"),
    ],
)
def test_the_access_log_filter_hides_the_query_of_authentication_requests_only(path, expected):
    record = uvicorn_access_record(path)

    assert RedactAuthQuery().filter(record) is True  # the line is kept, not dropped
    assert record.getMessage() == f'127.0.0.1:5000 - "GET {expected} HTTP/1.1" 303'


def test_the_filter_survives_records_of_another_shape():
    for args in (None, (), ("only",), ("a", "b", 3), ({"a": 1},)):
        record = logging.LogRecord(ACCESS_LOGGER, logging.INFO, __file__, 1, "plain", args, None)
        assert RedactAuthQuery().filter(record) is True


def test_installing_the_filter_twice_installs_it_once():
    logger = logging.getLogger(ACCESS_LOGGER)
    before = [f for f in logger.filters if isinstance(f, RedactAuthQuery)]
    install_access_log_redaction()
    install_access_log_redaction()

    assert len([f for f in logger.filters if isinstance(f, RedactAuthQuery)]) == max(1, len(before))


@pytest.mark.asyncio
async def test_starting_the_application_installs_the_filter_on_the_servers_access_log(tmp_path, caplog):
    logger = logging.getLogger(ACCESS_LOGGER)
    for existing in [f for f in logger.filters if isinstance(f, RedactAuthQuery)]:
        logger.removeFilter(existing)
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    app = create_app(settings_for(tmp_path / "access.db"))
    assert not any(isinstance(f, RedactAuthQuery) for f in logger.filters)  # creating changes nothing

    async with running(app):
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:5000", "GET", "/auth/telegram/callback?code=SECRETCODE&state=SECRETSTATE", "1.1", 303,
        )  # fmt: skip

    assert "SECRETCODE" not in caplog.text and "SECRETSTATE" not in caplog.text
    assert "/auth/telegram/callback?[redacted]" in caplog.text
    assert any(isinstance(f, RedactAuthQuery) for f in logger.filters)


@pytest.mark.asyncio
async def test_the_filter_does_not_depend_on_login_being_configured(tmp_path, caplog):
    # With login off the callback still answers (503) and the server still logs its
    # query: whoever is probing it can put anything there.
    logger = logging.getLogger(ACCESS_LOGGER)
    for existing in [f for f in logger.filters if isinstance(f, RedactAuthQuery)]:
        logger.removeFilter(existing)
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    async with running(create_app(settings_for(tmp_path / "off.db"))):
        logger.info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/auth/telegram/callback?code=SECRET", "1.1", 503
        )

    assert "SECRET" not in caplog.text
