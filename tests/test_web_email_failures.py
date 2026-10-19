"""Email sign-in when something goes wrong, and what it says about it.

The mail cannot be sent; the database fails under a request; a bug raises; the
database is closed. For each: the answer is the one a person can act on and the
asker learns nothing about who is registered; no link is left alive for a message
nobody got; a failed confirmation spends nothing; no session is left half made; and
nothing secret (the address, the link, its secret, the session token) reaches a log
or an answer.
"""

import asyncio
import logging

import pytest
import sqlalchemy as sa

from house_helper.persistence import email_login
from house_helper.persistence.allowlist import allow
from house_helper.persistence.models import EmailLoginChallenge, IdentityLink, User
from house_helper.persistence.models import Session as LoginSession
from house_helper.web import email_pages
from house_helper.web.logsafety import ACCESS_LOGGER, RedactAuthQuery
from house_helper.web.mailer import EmailDeliveryError
from tests.auth_support import ORIGIN, START, app_log_text, logout_as, rows, watch_logs
from tests.email_support import ADDRESS, email_app, parse_link

SESSION_COOKIE = "__Host-hh_session"
NEUTRAL = {"detail": "link_requested"}


def challenge_rows(h):
    return rows(h, EmailLoginChallenge)


def attempted_link(h):
    return parse_link(h.mailer.last_attempted_url())


# --- the mail cannot be sent ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_when_the_mail_cannot_be_sent_the_answer_is_the_same_and_the_link_is_withdrawn(tmp_path, caplog):
    watch_logs(caplog)
    async with email_app(tmp_path) as h:
        baseline = await h.ask("nobody@example.com")  # what a stranger is told
        h.mailer.failure = EmailDeliveryError("SMTPServerDisconnected")

        response = await h.ask()

        assert (response.status_code, response.content, dict(response.headers)) == (
            baseline.status_code, baseline.content, dict(baseline.headers)
        )
        assert h.mailer.attempts == 1 and h.mailer.sent == []
        (row,) = challenge_rows(h)
        assert row.revoked_at == START and row.consumed_at is None  # no live link for a lost message
        link = attempted_link(h)
        assert (await h.client.get(link.path)).status_code == 400
        assert (await h.confirm(link)).status_code == 400
        assert rows(h, LoginSession) == []
        logged = app_log_text(caplog)
        assert "A sign-in email could not be sent (SMTPServerDisconnected)" in logged
        for secret in (ADDRESS, "elena", link.token, link.challenge_id, link.url):
            assert secret not in logged


@pytest.mark.asyncio
async def test_after_a_failed_send_the_cooldown_still_holds_and_a_later_request_works(tmp_path):
    async with email_app(tmp_path) as h:
        h.mailer.failure = EmailDeliveryError("SMTPServerDisconnected")
        await h.ask()
        h.mailer.failure = None

        h.clock.advance(seconds=30)
        assert (await h.ask()).status_code == 202
        assert h.mailer.attempts == 1  # the attempt that failed still counts against the cooldown

        h.clock.advance(seconds=30)
        link = await h.ask_and_get_link()
        assert (await h.confirm(link)).status_code == 303
        assert len(challenge_rows(h)) == 2


@pytest.mark.asyncio
async def test_an_error_of_any_kind_in_the_sender_is_the_same_and_its_message_is_not_logged(tmp_path, caplog):
    watch_logs(caplog)
    async with email_app(tmp_path) as h:
        h.mailer.failure = RuntimeError(f"relay said: {ADDRESS} secret-relay-detail")

        response = await h.ask()

        assert response.status_code == 202 and response.json()["detail"] == "link_requested"
        assert challenge_rows(h)[0].revoked_at is not None
        logged = app_log_text(caplog)
        assert "A sign-in email could not be sent (RuntimeError)" in logged
        assert "secret-relay-detail" not in logged and ADDRESS not in logged
        assert "Traceback" not in logged  # no stack, whose frames would carry the arguments


@pytest.mark.asyncio
async def test_an_allowlisted_address_that_cannot_be_mailed_to_gets_no_attempt_and_no_live_link(tmp_path, caplog):
    watch_logs(caplog)
    async with email_app(tmp_path, seed=False) as h:
        with h.factory() as db, db.begin():
            user = User()
            db.add(user)
            db.flush()
            allow(db, user, "email", "a,b@example.com")  # on the list; but a comma is a second recipient

        response = await h.ask("a,b@example.com")

        assert response.status_code == 202
        assert h.mailer.attempts == 0  # not even handed to the sender
        assert challenge_rows(h)[0].revoked_at is not None
        assert "could not be sent (ValueError)" in app_log_text(caplog)


@pytest.mark.asyncio
async def test_the_answer_goes_out_before_the_mail_does(tmp_path):
    # So that how long it takes to answer cannot say whether a mail was sent. Driven at
    # the ASGI level, because an HTTP client hands over the response only when the
    # application has finished, background work included.
    async with email_app(tmp_path) as h:
        arrived, release = h.mailer.hold()
        body = b'{"email": "elena@example.com"}'
        sent: list[dict] = []
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "https", "path": "/auth/email/request", "raw_path": b"/auth/email/request",
            "query_string": b"", "root_path": "", "client": ("127.0.0.1", 5000),
            "server": ("kitchen.example.test", 443),
            "headers": [
                (b"host", b"kitchen.example.test"), (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()), (b"origin", ORIGIN.encode()),
            ],
        }
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)

        async def send(message):
            sent.append(message)

        task = asyncio.ensure_future(h.app(scope, receive, send))
        await asyncio.wait_for(arrived.wait(), 10)

        assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
        assert sent[0]["status"] == 202 and not sent[1].get("more_body")
        assert h.mailer.sent == []  # the answer is out and the mail is not yet

        release.set()
        await asyncio.wait_for(task, 10)
        assert len(h.mailer.sent) == 1


@pytest.mark.asyncio
async def test_a_link_that_cannot_be_withdrawn_is_reported_and_harms_nobody(tmp_path, caplog, monkeypatch):
    watch_logs(caplog)
    async with email_app(tmp_path) as h:
        h.mailer.failure = EmailDeliveryError("SMTPServerDisconnected")

        def broken(session, challenge_id, *, now):
            raise sa.exc.OperationalError("UPDATE email_login_challenges", {"id": "x"}, Exception("database is locked"))

        monkeypatch.setattr(email_login, "withdraw_email_login", broken)

        response = await h.ask()

        assert response.status_code == 202
        logged = app_log_text(caplog)
        assert "A database error stopped an authentication request (OperationalError)" in logged
        assert "A sign-in link could not be withdrawn: the database failed" in logged
        assert "UPDATE email_login_challenges" not in logged and "database is locked" not in logged
        # Nobody has its secret (the message was not sent), so what stays on record is inert.
        assert challenge_rows(h)[0].revoked_at is None and h.mailer.sent == []


# --- the database fails ---------------------------------------------------------------------------------


def break_table(h, table="email_login_challenges"):
    with h.factory() as db, db.begin():
        db.execute(sa.text(f"ALTER TABLE {table} RENAME TO {table}_gone"))


def repair_table(h, table="email_login_challenges"):
    with h.factory() as db, db.begin():
        db.execute(sa.text(f"ALTER TABLE {table}_gone RENAME TO {table}"))


@pytest.mark.asyncio
async def test_a_database_failure_when_asking_is_a_503_that_sends_nothing_and_recovers(tmp_path, caplog):
    watch_logs(caplog)
    async with email_app(tmp_path) as h:
        break_table(h)

        response = await h.ask()

        assert response.status_code == 503
        assert response.json()["detail"] == "temporarily_unavailable" and "Сервис" in response.json()["message"]
        assert h.mailer.attempts == 0
        logged = app_log_text(caplog)
        assert "OperationalError" in logged and "no such table" not in logged and ADDRESS not in logged
        assert "SELECT" not in logged and "UPDATE" not in logged
        repair_table(h)
        assert (await h.ask()).status_code == 202 and len(h.mailer.sent) == 1


@pytest.mark.asyncio
async def test_a_database_failure_when_opening_the_link_is_an_unavailable_page_not_an_invalid_one(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()
        break_table(h)

        page = await h.client.get(link.path)

        assert page.status_code == 503 and page.text == email_pages.unavailable_page().html
        repair_table(h)
        assert (await h.client.get(link.path)).status_code == 200  # the link is as it was


def refuse_sessions(h):
    """A database that takes everything up to the session, then refuses it."""
    with h.factory() as db, db.begin():
        db.execute(sa.text(
            "CREATE TRIGGER refuse_sessions BEFORE INSERT ON sessions "
            "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        ))


def accept_sessions(h):
    with h.factory() as db, db.begin():
        db.execute(sa.text("DROP TRIGGER refuse_sessions"))


@pytest.mark.asyncio
async def test_a_failure_while_the_session_is_made_spends_nothing_and_leaves_nothing(tmp_path, caplog):
    watch_logs(caplog)
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()
        refuse_sessions(h)

        response = await h.confirm(link)

        assert response.status_code == 503 and response.text == email_pages.unavailable_page().html
        assert "set-cookie" not in response.headers
        (row,) = challenge_rows(h)
        assert row.consumed_at is None  # the claim was rolled back with everything else
        assert rows(h, LoginSession) == [] and rows(h, IdentityLink) == []  # not even the link row
        logged = app_log_text(caplog)
        assert "IntegrityError" in logged and "disk full" not in logged and "INSERT INTO" not in logged
        assert link.token not in logged

        accept_sessions(h)
        assert (await h.confirm(link)).status_code == 303  # the same link, now that the fault is gone
        assert len(rows(h, LoginSession)) == 1


@pytest.mark.asyncio
async def test_a_bug_while_confirming_is_a_500_page_that_leaks_nothing_and_spends_nothing(tmp_path, caplog, monkeypatch):
    watch_logs(caplog)
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()
        real = email_login.redeem_email_login

        def broken(session, challenge_id, token, **options):
            real(session, challenge_id, token, **options)  # (it did its work, then...)
            raise RuntimeError(f"secret-detail-of-a-bug {token}")

        monkeypatch.setattr(email_login, "redeem_email_login", broken)

        response = await h.confirm(link)

        assert response.status_code == 500 and response.text == email_pages.error_page().html
        assert "set-cookie" not in response.headers
        assert challenge_rows(h)[0].consumed_at is None and rows(h, LoginSession) == []
        logged = app_log_text(caplog)
        assert "RuntimeError" in logged and "secret-detail-of-a-bug" not in logged and link.token not in logged
        monkeypatch.undo()
        assert (await h.confirm(link)).status_code == 303


@pytest.mark.asyncio
async def test_a_bug_while_asking_is_a_500_that_sends_nothing(tmp_path, caplog, monkeypatch):
    watch_logs(caplog)
    async with email_app(tmp_path) as h:
        def broken(session, address, **options):
            raise RuntimeError(f"secret-detail-of-a-bug {address}")

        monkeypatch.setattr(email_login, "issue_email_login", broken)

        response = await h.ask()

        assert response.status_code == 500 and response.json()["detail"] == "server_error"
        assert "secret-detail" not in response.text + app_log_text(caplog) and ADDRESS not in app_log_text(caplog)
        assert h.mailer.attempts == 0


@pytest.mark.asyncio
async def test_a_bug_while_looking_at_a_link_is_a_500_page(tmp_path, caplog, monkeypatch):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()

        def broken(session, challenge_id, *, now):
            raise RuntimeError("secret-detail-of-a-bug")

        monkeypatch.setattr(email_login, "preview_email_login", broken)
        page = await h.client.get(link.path)

        assert page.status_code == 500 and page.text == email_pages.error_page().html
        assert "secret-detail" not in page.text


@pytest.mark.asyncio
async def test_with_the_database_closed_every_email_route_says_unavailable(tmp_path):
    async with email_app(tmp_path) as h:
        link = await h.ask_and_get_link()
        await h.app.state.database.aclose()

        asked = await h.ask()
        opened = await h.client.get(link.path)
        confirmed = await h.confirm(link)

        assert asked.status_code == 503 and asked.json()["detail"] == "temporarily_unavailable"
        assert opened.status_code == 503 and confirmed.status_code == 503
        assert "set-cookie" not in confirmed.headers


# --- nothing secret is logged ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_secret_is_logged_whatever_happens(tmp_path, caplog):
    watch_logs(caplog)
    secrets: set[str] = set()
    async with email_app(tmp_path) as h:
        second_id = None
        with h.factory() as db, db.begin():
            other = User(display_name="Second")
            db.add(other)
            db.flush()
            allow(db, other, "email", "second@example.com")
            second_id = other.id

        def remember(link):
            secrets.update({link.token, link.challenge_id, link.url, link.token[:20]})
            return link

        # a success, its replay, a logout
        first = remember(await h.ask_and_get_link())
        await h.client.get(first.path)
        assert (await h.confirm(first)).status_code == 303
        secrets.add(h.client.cookies.get(SESSION_COOKIE))
        assert (await h.confirm(first)).status_code == 400
        assert (await h.client.post("/auth/logout", headers=logout_as(h.user_id, Origin=ORIGIN))).status_code == 204

        # wrong and malformed secrets
        h.clock.advance(minutes=2)
        second = remember(await h.ask_and_get_link())
        await h.confirm(second, token="B" * 43)
        await h.confirm(second, token="not-a-token")
        await h.confirm(second, headers={"Origin": "https://evil.example"})

        # a browser signed in as somebody else
        h.clock.advance(minutes=2)
        theirs = remember(await h.ask_and_get_link("second@example.com"))
        client = h.new_client()
        assert (await h.confirm(theirs, client)).status_code == 303
        secrets.add(client.cookies.get(SESSION_COOKIE))
        mine = remember(await h.ask_and_get_link())
        assert (await h.confirm(mine, client)).status_code == 409
        await h.ask()  # at once again: the cooldown

        # strangers, an alias, a revoked address, the mail failing, the database failing
        await h.ask("nobody@example.com")
        await h.ask("elena+tag@example.com")
        h.clock.advance(minutes=2)
        h.mailer.failure = RuntimeError(f"relay: {ADDRESS} secret-relay-detail")
        await h.ask()
        secrets.add(h.mailer.last_attempted_url())
        secrets.add("secret-relay-detail")
        h.mailer.failure = None
        break_table(h)
        await h.ask()
        await h.client.get(mine.path)
        repair_table(h)

        # everything that is stored
        for row in challenge_rows(h):
            secrets.add(row.token_hash)
        for row in rows(h, LoginSession):
            secrets.add(row.token_hash)
        assert second_id is not None

    logged = app_log_text(caplog)
    secrets.discard(None)
    assert len(secrets) > 20  # the scenario really did produce a crowd of secrets
    assert sorted(secret for secret in secrets if secret in logged) == []
    for personal in (ADDRESS, "elena", "second@example.com", "Елена", "nobody@example.com", "evil.example"):
        assert personal not in logged, personal
    # ...while the log is not empty: the operator can follow what happened
    assert "signed in by email" in logged
    assert "A sign-in link was requested (issued)" in logged
    assert "A sign-in link was requested (not_eligible)" in logged
    assert "A sign-in link was requested (cooldown)" in logged
    assert "A sign-in link was not valid" in logged
    assert "signed in as another user" in logged
    assert "could not be sent (RuntimeError)" in logged


@pytest.mark.asyncio
async def test_the_user_id_is_logged_on_sign_in_and_nothing_personal_is(tmp_path, caplog):
    watch_logs(caplog)
    async with email_app(tmp_path) as h:
        await h.sign_in()

    logged = app_log_text(caplog)
    assert f"User {h.user_id} signed in by email" in logged
    for personal in (ADDRESS, "elena", "Елена"):
        assert personal not in logged


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/auth/email/confirm?c=SECRET-ID", "/auth/email/confirm?[redacted]"),
        ("/auth/email/confirm?c=SECRET-ID&token=SECRET", "/auth/email/confirm?[redacted]"),
        ("/auth/email/confirm", "/auth/email/confirm"),
        ("/auth/email/request?x=SECRET", "/auth/email/request?[redacted]"),
    ],
)
def test_the_access_log_filter_hides_the_query_of_the_email_routes_too(path, expected):
    record = logging.LogRecord(
        ACCESS_LOGGER, logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", path, "1.1", 200), None,
    )  # fmt: skip

    assert RedactAuthQuery().filter(record) is True
    assert record.getMessage() == f'127.0.0.1:5000 - "GET {expected} HTTP/1.1" 200'
