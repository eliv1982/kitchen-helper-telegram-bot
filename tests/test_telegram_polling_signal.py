"""A Telegram bot that cannot read Telegram must not look like one that does.

The bot polls from inside the web process (telegram_chat.py). When the token is refused, another
consumer or a webhook holds it, or Telegram cannot be reached, the website keeps working and the
bot reads nothing. These tests pin the small signal that makes that visible, and nothing more:
the first poll waits for nothing, so a start-up failure shows at once; `TelegramChat.failure`
names the category of the failure; the log has one line for the start, one ERROR for a blocked
or refused bot and one for the recovery; the pairing routes say "not available" instead of
offering to connect a bot that is not reading; /healthz stays a liveness check; and the
application never touches a webhook.
"""

import asyncio
import logging

import pytest

from house_helper.web import telegram_chat as tc
from house_helper.web.settings import (
    PUBLIC_URL_ENV,
    TELEGRAM_BOT_TOKEN_ENV,
    TELEGRAM_BOT_USERNAME_ENV,
    TELEGRAM_CLIENT_ID_ENV,
    TELEGRAM_CLIENT_SECRET_ENV,
    load_web_settings,
)
from tests.auth_support import sign_in
from tests.chat_support import ScriptedBackend, error_of, post_headers
from tests.telegram_support import BOT_NAME, BOT_TOKEN, FakeBot, MOTHER, private_message, telegram_app
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

LOGGER = "house_helper.web.telegram"


async def until(predicate) -> None:
    for _ in range(int(TIMEOUT / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition was never reached")


def lines(caplog, level=None) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and (level is None or record.levelno == level)
    ]


def held_sleep(monkeypatch):
    """The bot's pause between failed polls becomes a gate the test opens: the state in
    between is then there to be looked at. Returns (the gate, the pauses asked for, the sleep);
    a bot that is already running is given the sleep by hand (`service._sleep = sleep`)."""
    gate = asyncio.Event()
    pauses: list[float] = []

    async def sleep(seconds):
        pauses.append(seconds)
        await gate.wait()

    monkeypatch.setitem(tc.TelegramChat.__init__.__kwdefaults__, "sleep", sleep)
    return gate, pauses, sleep


async def status_of(t):
    response = await t.chat.client.get("/api/telegram")
    assert response.status_code == 200, response.text
    return response.json()


# --- start -----------------------------------------------------------------------------------


async def test_the_first_poll_waits_for_nothing_and_the_log_says_the_bot_is_polling(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    async with telegram_app(tmp_path) as t:
        await until(lambda: len(t.bot.waits) >= 2)
        status = await status_of(t)
        failure = t.service.failure

    assert t.bot.waits[0] == 0  # the start-up check is answered at once, not after a long poll
    assert t.bot.waits[1] == 25  # every later poll is a long one (BotSettings.poll_seconds)
    assert failure is None
    assert status["enabled"] is True and status["available"] is True
    assert lines(caplog, logging.INFO).count("The Telegram bot is polling") == 1
    assert lines(caplog, logging.ERROR) == []


# --- another consumer, or a webhook ----------------------------------------------------------


async def test_a_second_consumer_or_a_webhook_at_start_is_said_at_once_and_nothing_is_read_or_changed(
    tmp_path, caplog, monkeypatch
):
    caplog.set_level(logging.INFO)
    gate, pauses, _ = held_sleep(monkeypatch)
    bot = FakeBot()
    bot.fail("getUpdates", 409, 409, 409)
    bot.push(private_message(7000, MOTHER, "Прочитают только после восстановления"))
    async with telegram_app(tmp_path, ScriptedBackend("нет"), bot=bot) as t:
        await until(lambda: pauses)  # the first poll failed and the bot is waiting to try again
        blocked = {
            "failure": t.service.failure,
            "status": await status_of(t),
            "link": await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers()),
            "sent": list(bot.sent),
            "codes": t.codes(),
            "errors": lines(caplog, logging.ERROR),
        }
        gate.set()
        await until(lambda: t.service.failure is None)
        recovered = await status_of(t)

    assert pauses[0] == 30.0  # a conflict is waited out for half a minute, not hammered
    assert blocked["failure"] == "api:409"
    assert blocked["status"]["enabled"] is True and blocked["status"]["available"] is False
    assert blocked["status"]["linked"] is False
    assert blocked["link"].status_code == 503 and error_of(blocked["link"])["detail"] == "telegram_unavailable"
    assert blocked["codes"] == [] and blocked["sent"] == []  # no code issued, nothing sent
    assert len(blocked["errors"]) == 1 and "blocked" in blocked["errors"][0]
    assert "category=api:409" in blocked["errors"][0] and "webhook" in blocked["errors"][0]
    assert recovered["available"] is True

    assert len(lines(caplog, logging.ERROR)) == 1  # one error line for the whole run of failures
    assert len([m for m in lines(caplog, logging.WARNING) if "category=api:409" in m]) == 2
    assert "The Telegram bot is polling" in lines(caplog, logging.INFO)
    # The application never sets, reads or deletes a webhook, and never calls anything but getUpdates here.
    assert {request.url.path.rsplit("/", 1)[-1] for request in bot.requests} <= {"getUpdates", "sendMessage", "sendChatAction"}
    assert all("ebhook" not in request.url.path for request in bot.requests)


async def test_a_conflict_that_starts_later_is_an_error_line_again_and_clears_when_it_ends(
    tmp_path, caplog, monkeypatch
):
    caplog.set_level(logging.INFO)
    async with telegram_app(tmp_path) as t:
        await until(lambda: t.bot.waits)
        gate, pauses, sleep = held_sleep(monkeypatch)
        t.service._sleep = sleep
        t.bot.fail("getUpdates", 409)
        await until(lambda: pauses)
        during = (t.service.failure, (await status_of(t))["available"])
        gate.set()
        await until(lambda: t.service.failure is None)
        after = (await status_of(t))["available"]

    assert during == ("api:409", False) and after is True
    assert len(lines(caplog, logging.ERROR)) == 1 and "blocked" in lines(caplog, logging.ERROR)[0]
    assert "The Telegram bot is polling again" in lines(caplog, logging.INFO)


# --- the token -------------------------------------------------------------------------------


async def test_a_refused_token_is_an_error_line_and_a_bot_the_website_does_not_offer(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    bot = FakeBot()
    bot.fail("getUpdates", 401)
    async with telegram_app(tmp_path, bot=bot) as t:
        await until(lambda: t.service._poller is not None and t.service._poller.done())
        failure = t.service.failure
        status = await status_of(t)
        link = await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())
        unlink = await t.chat.client.delete("/api/telegram/link", headers=post_headers())
        health = await t.chat.client.get("/healthz")

    assert failure == "api:401"
    assert status["enabled"] is True and status["available"] is False
    assert link.status_code == 503 and error_of(link)["detail"] == "telegram_unavailable"
    assert unlink.status_code == 200  # leaving needs only the database
    assert health.status_code == 200 and health.json() == {"status": "ok"}  # liveness stays liveness
    errors = lines(caplog, logging.ERROR)
    assert len(errors) == 1 and "token was refused (api:401)" in errors[0]
    assert "The Telegram bot is polling" not in lines(caplog, logging.INFO)


# --- Telegram cannot be reached --------------------------------------------------------------


async def test_a_failing_poll_is_not_shown_as_a_working_bot_and_clears_by_itself(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        gate, pauses, sleep = held_sleep(monkeypatch)
        t.service._sleep = sleep
        t.bot.fail("getUpdates", 502)
        await until(lambda: pauses)
        during = await status_of(t)
        gate.set()
        await until(lambda: t.service.failure is None)
        after = await status_of(t)

    assert during["available"] is False
    assert during["linked"] is True and during["account"]["name"] == "@mama_elena"  # still linked, not working
    assert after["available"] is True and after["linked"] is True
    assert lines(caplog, logging.ERROR) == []  # a bad moment is a warning, not an alarm
    assert any("category=api:502" in m for m in lines(caplog, logging.WARNING))
    assert "The Telegram bot is polling again" in lines(caplog, logging.INFO)


# --- a bug in the loop itself -----------------------------------------------------------------


async def test_a_polling_loop_that_dies_of_a_bug_is_not_shown_as_a_working_bot(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)

    def broken(self, work):
        work.close()
        raise RuntimeError("a bug outside the guarded call")

    async with telegram_app(tmp_path) as t:
        await until(lambda: t.bot.waits)
        monkeypatch.setattr(tc.TelegramChat, "_spawn", broken)
        t.bot.push(private_message(8000, MOTHER, "Любое сообщение"))
        await until(lambda: t.service._poller.done())
        failure = t.service.failure
        status = await status_of(t)
        health = await t.chat.client.get("/healthz")

    assert failure == "RuntimeError"
    assert status["enabled"] is True and status["available"] is False
    assert health.status_code == 200
    errors = lines(caplog, logging.ERROR)
    assert len(errors) == 1 and "polling failed with RuntimeError" in errors[0]


# --- the sign-in does not depend on the bot --------------------------------------------------


@pytest.mark.parametrize("code", [401, 409], ids=["token refused", "token held by another consumer"])
async def test_signing_in_with_telegram_works_while_the_bot_is_refused_or_blocked(tmp_path, code):
    bot = FakeBot()
    bot.fail("getUpdates", *([code] * 50))  # the bot never gets a good answer
    async with telegram_app(tmp_path, bot=bot, sign_in_first=False) as t:
        await until(lambda: t.service.failure is not None)
        another_browser = t.chat.base.new_client()
        callback = await sign_in(another_browser, t.chat.base.telegram)
        me = await another_browser.get("/auth/me")
        conversations = await another_browser.get("/api/conversations")
        failure = t.service.failure

    assert failure == f"api:{code}"
    assert callback.status_code == 303 and me.status_code == 200 and conversations.status_code == 200


async def test_the_bots_settings_and_the_sign_in_settings_are_independent_and_use_different_credentials():
    sign_in_only = {
        PUBLIC_URL_ENV: "https://kitchen.example.test",
        TELEGRAM_CLIENT_ID_ENV: "client-id-1",
        TELEGRAM_CLIENT_SECRET_ENV: "client-secret-1",
    }
    bot_only = {TELEGRAM_BOT_TOKEN_ENV: BOT_TOKEN, TELEGRAM_BOT_USERNAME_ENV: BOT_NAME}

    first = load_web_settings(sign_in_only)
    second = load_web_settings(bot_only)
    both = load_web_settings({**sign_in_only, **bot_only})

    assert first.auth is not None and first.telegram_bot is None  # sign-in with no bot
    assert second.auth is None and second.telegram_bot is not None  # a bot with no sign-in
    assert both.auth is not None and both.telegram_bot is not None
    assert both.auth.client_secret != both.telegram_bot.token  # two different credentials
    assert BOT_TOKEN not in repr(both) and "client-secret-1" not in repr(both)
