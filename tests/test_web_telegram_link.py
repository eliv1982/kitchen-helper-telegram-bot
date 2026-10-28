"""The pairing routes under /api/telegram: who may ask for a code, what the code is, and what
the status says before and after the bot has been given it.

The other half of the pairing (the bot receiving /start <code>) is tests/test_telegram_chat.py.
"""

import logging
import re
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.persistence.authn import hash_secret
from house_helper.web.diagnostics import CATALOGUE
from tests.auth_support import ORIGIN, auth_app, sign_in
from tests.chat_support import ScriptedBackend, error_of, log_text, post_headers
from tests.telegram_support import BOT_NAME, MOTHER, SISTER, telegram_app

pytestmark = pytest.mark.asyncio


# --- without the bot ---------------------------------------------------------------------


async def test_without_the_bot_the_status_says_so_and_the_other_routes_are_unavailable(tmp_path):
    async with auth_app(tmp_path) as base:
        await sign_in(base.client, base.telegram)

        status = await base.client.get("/api/telegram")
        link = await base.client.post("/api/telegram/link", json={}, headers=post_headers())
        unlink = await base.client.delete("/api/telegram/link", headers=post_headers())

    assert status.status_code == 200 and status.json() == {"enabled": False, "linked": False}
    for refused in (link, unlink):
        assert refused.status_code == 503
        assert error_of(refused)["detail"] == "telegram_unavailable"


# --- who may ask --------------------------------------------------------------------------


async def test_every_route_needs_a_signed_in_session(tmp_path):
    async with telegram_app(tmp_path, sign_in_first=False) as t:
        client = t.chat.client
        responses = [
            await client.get("/api/telegram"),
            await client.post("/api/telegram/link", json={}, headers=post_headers()),
            await client.delete("/api/telegram/link", headers=post_headers()),
        ]

    assert [r.status_code for r in responses] == [401, 401, 401]
    assert {error_of(r)["detail"] for r in responses} == {"not_authenticated"}
    assert t.codes() == [] and t.links() == []


async def test_a_page_of_another_site_can_neither_ask_for_a_code_nor_unlink(tmp_path):
    async with telegram_app(tmp_path) as t:
        foreign = {"Origin": "https://evil.example"}
        asked = await t.chat.client.post("/api/telegram/link", json={}, headers=foreign)
        await t.pair()
        removed = await t.chat.client.delete("/api/telegram/link", headers=foreign)

    assert (asked.status_code, removed.status_code) == (403, 403)
    assert error_of(asked)["detail"] == error_of(removed)["detail"] == "forbidden_origin"
    assert len(t.links()) == 1  # still linked: the foreign delete did nothing


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"json": {"x": 1}}, 400),
        ({"content": b"not json", "headers": {"content-type": "application/json"}}, 400),
        ({"content": b"{}", "headers": {"content-type": "text/plain"}}, 415),
    ],
)
async def test_asking_for_a_code_takes_an_empty_json_object_and_nothing_else(tmp_path, kwargs, expected):
    async with telegram_app(tmp_path) as t:
        headers = {**post_headers(), **kwargs.pop("headers", {})}
        response = await t.chat.client.post("/api/telegram/link", headers=headers, **kwargs)

    assert response.status_code == expected
    assert t.codes() == []


# --- the code --------------------------------------------------------------------------------


async def test_the_code_comes_with_a_link_to_the_bot_and_a_ten_minute_life(tmp_path):
    async with telegram_app(tmp_path) as t:
        response = await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())
        body = response.json()

    assert response.status_code == 201
    assert set(body) == {"code", "link", "expires_at", "expires_in_seconds"}
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", body["code"])
    assert body["link"] == f"https://t.me/{BOT_NAME}?start={body['code']}"
    assert body["expires_in_seconds"] == 600
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-correlation-id"].startswith("hh-")
    [stored] = t.codes()
    assert stored.code_hash == hash_secret(body["code"])
    assert (stored.expires_at - stored.created_at) == timedelta(minutes=10)


async def test_a_second_request_withdraws_the_first_code(tmp_path):
    async with telegram_app(tmp_path) as t:
        first = (await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())).json()
        second = (await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())).json()
        await t.deliver(MOTHER, f"/start {first['code']}")
        refused = t.bot.texts(MOTHER)[-1]
        await t.deliver(MOTHER, f"/start {second['code']}")

    assert first["code"] != second["code"]
    assert "недействительна" in refused
    assert len(t.links()) == 1


async def test_the_code_is_in_no_log_and_in_no_table_as_itself(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with telegram_app(tmp_path) as t:
        code = await t.pair()
        with t.chat.base.factory() as db:
            dump = "\n".join(
                repr(row)
                for table in ("telegram_links", "telegram_link_codes", "users", "conversations", "messages")
                for row in db.execute(sa.text(f"SELECT * FROM {table}")).all()
            )

    assert code not in log_text(caplog)
    assert code not in dump


# --- the status ----------------------------------------------------------------------------------


async def test_the_status_before_and_after_pairing(tmp_path):
    async with telegram_app(tmp_path) as t:
        before = (await t.chat.client.get("/api/telegram")).json()
        await t.pair(MOTHER)
        after_response = await t.chat.client.get("/api/telegram")
        after = after_response.json()
        again = await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())

    assert before == {"enabled": True, "available": True, "linked": False, "bot_username": BOT_NAME}
    assert after["linked"] is True and after["enabled"] is True and after["available"] is True
    assert after["account"]["name"] == "@mama_elena"
    assert after["account"]["linked_at"].startswith("2026-10-17T12:00:")
    assert "telegram_user_id" not in after_response.text and str(MOTHER) not in after_response.text
    assert again.status_code == 409 and error_of(again)["detail"] == "telegram_already_linked"
    assert CATALOGUE["telegram_already_linked"].retryable is False


async def test_unlinking_ends_the_link_and_the_bot_stops_answering(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Что приготовить?")
        answered = t.chat.backend.calls

        first = await t.chat.client.delete("/api/telegram/link", headers=post_headers())
        second = await t.chat.client.delete("/api/telegram/link", headers=post_headers())
        status = (await t.chat.client.get("/api/telegram")).json()
        await t.deliver(MOTHER, "А теперь?")

    assert (first.status_code, first.json()) == (200, {"linked": False})
    assert second.status_code == 200  # idempotent
    assert status["linked"] is False
    assert answered == 1 and t.chat.backend.calls == 1  # the second message cost nothing
    assert "только для подключённых" in t.bot.texts(MOTHER)[-1]


async def test_after_unlinking_the_same_person_can_pair_another_account(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.chat.client.delete("/api/telegram/link", headers=post_headers())
        await t.pair(SISTER)
        link = t.links()

    assert [row.telegram_user_id for row in link] == [SISTER]


# --- two people -----------------------------------------------------------------------------------------


async def test_each_person_sees_and_changes_only_their_own_link(tmp_path):
    async with telegram_app(tmp_path) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await t.pair(SISTER, client=other)

        mine = (await t.chat.client.get("/api/telegram")).json()
        their_view = (await other.get("/api/telegram")).json()
        await t.chat.client.delete("/api/telegram/link", headers=post_headers())
        theirs_after = (await other.get("/api/telegram")).json()

    assert mine["linked"] and their_view["linked"]
    assert theirs_after["linked"] is True  # unlinking is for the one who asks
    assert [row.telegram_user_id for row in t.links()] == [SISTER]
