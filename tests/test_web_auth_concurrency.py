"""Concurrent authentication attempts, against the real worker threads and a real
SQLite file.

What is at stake: the same callback sent twice at once must exchange one code and
open one session; many people (or one person with many tabs) signing in at the same
moment must each get a session, and the identity link must be created exactly once;
and none of it may fail with "database is locked" or an unhandled error, which is
what a transaction that reads before it writes does on SQLite when another writer
commits in between (persistence/authn.py explains how the writers avoid it).

Each test repeats its race a few times, on fresh databases where the first login
matters: a race that is won by luck once is not a pass.
"""

import asyncio
import itertools
import logging

import httpx
import pytest

from house_helper.persistence.allowlist import allow
from house_helper.persistence.models import IdentityLink, OidcState, User
from house_helper.persistence.models import Session as LoginSession
from house_helper.web.app import create_app
from house_helper.web.settings import WebSettings
from tests.auth_support import (
    SUBJECT,
    FakeTelegram,
    app_log_text,
    auth_app,
    begin_sign_in,
    counts,
    finish_sign_in,
    rows,
    sign_in,
    watch_logs,
)
from tests.web_support import TIMEOUT, running, sqlite_url

SESSION_COOKIE = "__Host-hh_session"
LOGIN_COOKIE = "__Host-hh_login"
ROUNDS = 3


def round_dir(tmp_path, number):
    directory = tmp_path / f"round{number}"
    directory.mkdir()
    return directory


async def together(*awaitables):
    return await asyncio.wait_for(asyncio.gather(*awaitables), TIMEOUT * 3)


def no_errors_logged(caplog):
    problems = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert problems == [], [r.getMessage() for r in problems]
    assert "database is locked" not in app_log_text(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("copies", [2, 6])
async def test_the_same_callback_sent_at_once_exchanges_one_code_and_opens_one_session(
    tmp_path, caplog, copies
):
    watch_logs(caplog)
    for round_ in range(ROUNDS):
        async with auth_app(round_dir(tmp_path, round_)) as h:
            started = await begin_sign_in(h.client)
            callback = h.telegram.authorize(started.headers["location"])

            responses = await together(*(h.client.get(callback) for _ in range(copies)))

            statuses = sorted(response.status_code for response in responses)
            assert statuses == [303] + [400] * (copies - 1), statuses
            assert h.telegram.token_calls == 1  # the code was exchanged once
            assert h.telegram.jwks_calls == 1
            assert counts(h)["sessions"] == 1 and counts(h)["identity_links"] == 1
            assert h.telegram.protocol_errors == []
            (state,) = rows(h, OidcState)
            assert state.consumed_at is not None
    no_errors_logged(caplog)


@pytest.mark.asyncio
async def test_many_browsers_signing_in_at_once_each_get_a_session_and_one_link_is_made(tmp_path, caplog):
    watch_logs(caplog)
    browsers = 8
    for round_ in range(ROUNDS):
        async with auth_app(round_dir(tmp_path, round_)) as h:
            clients = [h.client] + [h.new_client() for _ in range(browsers - 1)]
            started = [await begin_sign_in(client) for client in clients]

            responses = await together(
                *(finish_sign_in(client, h.telegram, start) for client, start in zip(clients, started))
            )

            assert [response.status_code for response in responses] == [303] * browsers
            tokens = {client.cookies.get(SESSION_COOKIE) for client in clients}
            assert len(tokens) == browsers and None not in tokens
            assert counts(h)["sessions"] == browsers
            assert counts(h)["identity_links"] == 1  # created once, by whichever came first
            assert rows(h, IdentityLink)[0].user_id == h.user_id
            assert counts(h)["users"] == 1
            for client in clients:
                assert (await client.get("/auth/me")).json()["user_id"] == str(h.user_id)
            assert h.telegram.token_calls == browsers and h.telegram.protocol_errors == []
    no_errors_logged(caplog)


@pytest.mark.asyncio
async def test_sign_ins_of_two_people_at_once_stay_separate(tmp_path, caplog):
    watch_logs(caplog)
    for round_ in range(ROUNDS):
        async with auth_app(round_dir(tmp_path, round_)) as h:
            with h.factory() as db, db.begin():
                second = User(display_name="Somebody else")
                db.add(second)
                db.flush()
                allow(db, second, "telegram", "777")
                second_id = second.id
            mine = [h.client] + [h.new_client() for _ in range(3)]
            theirs = [h.new_client() for _ in range(4)]
            flows = [(client, SUBJECT) for client in mine] + [(client, "777") for client in theirs]
            started = [await begin_sign_in(client) for client, _ in flows]

            responses = await together(
                *(
                    finish_sign_in(client, h.telegram, start, subject)
                    for (client, subject), start in zip(flows, started)
                )
            )

            assert [response.status_code for response in responses] == [303] * 8
            for client in mine:
                assert (await client.get("/auth/me")).json()["user_id"] == str(h.user_id)
            for client in theirs:
                assert (await client.get("/auth/me")).json()["user_id"] == str(second_id)
            assert counts(h)["identity_links"] == 2 and counts(h)["users"] == 2
    no_errors_logged(caplog)


@pytest.mark.asyncio
async def test_many_logins_starting_at_once_each_store_their_own_state(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        clients = [h.client] + [h.new_client() for _ in range(11)]

        responses = await together(*(client.get("/auth/telegram/login") for client in clients))

        assert [response.status_code for response in responses] == [303] * 12
        states = {client.cookies.get(LOGIN_COOKIE) for client in clients}
        assert len(states) == 12 and len(rows(h, OidcState)) == 12
    no_errors_logged(caplog)


@pytest.mark.asyncio
async def test_signing_in_while_others_are_reading_never_fails(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)
        newcomers = [h.new_client() for _ in range(4)]
        started = [await begin_sign_in(client) for client in newcomers]

        readers = [h.client.get("/auth/me") for _ in range(20)]
        writers = [finish_sign_in(c, h.telegram, s) for c, s in zip(newcomers, started)]
        responses = await together(*readers, *writers)

        assert [r.status_code for r in responses[:20]] == [200] * 20
        assert [r.status_code for r in responses[20:]] == [303] * 4
        assert counts(h)["sessions"] == 5
    no_errors_logged(caplog)


@pytest.mark.asyncio
async def test_logging_out_while_the_same_session_is_in_use_never_fails(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        await sign_in(h.client, h.telegram)

        responses = await together(
            *(h.client.post("/auth/logout") if i % 3 == 0 else h.client.get("/auth/me") for i in range(12))
        )

        assert {r.status_code for r in responses} <= {200, 204, 401}
        assert (await h.client.get("/auth/me")).status_code == 401
        assert [row.revoked_at is not None for row in rows(h, LoginSession)] == [True]
    no_errors_logged(caplog)


@pytest.mark.asyncio
async def test_a_login_in_progress_survives_other_logins_finishing_around_it(tmp_path):
    async with auth_app(tmp_path) as h:
        slow = h.new_client()
        slow_start = await begin_sign_in(slow)
        others = [h.new_client() for _ in range(3)]
        for other in others:
            await sign_in(other, h.telegram)

        response = await finish_sign_in(slow, h.telegram, slow_start)

        assert response.status_code == 303
        assert counts(h)["sessions"] == 4 and counts(h)["identity_links"] == 1


@pytest.mark.asyncio
async def test_concurrent_refusals_and_successes_do_not_disturb_each_other(tmp_path, caplog):
    watch_logs(caplog)
    async with auth_app(tmp_path) as h:
        clients = [h.new_client() for _ in range(6)]
        started = [await begin_sign_in(client) for client in clients]
        subjects = itertools.cycle([SUBJECT, "9999999999"])  # every second person is a stranger
        plan = list(zip(clients, started, [next(subjects) for _ in clients]))

        responses = await together(
            *(finish_sign_in(client, h.telegram, start, subject) for client, start, subject in plan)
        )

        assert [r.status_code for r in responses] == [303, 403, 303, 403, 303, 403]
        assert counts(h)["sessions"] == 3 and counts(h)["identity_links"] == 1
        assert [c.cookies.get(SESSION_COOKIE) is not None for c in clients] == [True, False] * 3
    no_errors_logged(caplog)


@pytest.mark.asyncio
async def test_the_database_is_the_arbiter_not_the_process(tmp_path):
    # Two application instances (two processes in production) on one database file:
    # the same callback delivered to both is still honoured once.
    async with auth_app(tmp_path) as first:
        second_telegram = FakeTelegram(first.clock, first.settings)
        second_telegram.grants = first.telegram.grants  # one provider, two relying instances
        second_app = create_app(
            WebSettings(database_url=sqlite_url(tmp_path / "auth.db"), auth=first.settings),
            provider_transport=second_telegram.transport(),
            clock=first.clock,
        )
        async with running(second_app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second_app), base_url=first.origin
            ) as second_client:
                started = await begin_sign_in(first.client)
                callback = first.telegram.authorize(started.headers["location"])
                second_client.cookies.set(
                    LOGIN_COOKIE, first.client.cookies.get(LOGIN_COOKIE), domain="kitchen.example.test", path="/"
                )

                responses = await together(first.client.get(callback), second_client.get(callback))

        assert sorted(r.status_code for r in responses) == [303, 400]
        assert counts(first)["sessions"] == 1
        assert first.telegram.token_calls + second_telegram.token_calls == 1
