"""The Bot API client on its own: what a failure carries (and what it never does), the limits
on a download, how long text is cut, and how the bot's settings and the log filter treat the
token.
"""

import logging

import httpx
import pytest

from house_helper.web.logsafety import RedactBotToken, install_bot_token_redaction, redact_bot_token
from house_helper.web.settings import (
    TELEGRAM_BOT_TOKEN_ENV,
    TELEGRAM_BOT_USERNAME_ENV,
    BotSettings,
    WebConfigError,
    load_bot_settings,
    load_web_settings,
)
from house_helper.web.telegram_botapi import (
    MAX_MESSAGE_LENGTH,
    TOKEN_PATTERN,
    BotApi,
    BotApiError,
    split_text,
)
from tests.telegram_support import BOT_NAME, BOT_TOKEN, FakeBot


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- failures ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_carries_numbers_and_a_word_and_nothing_else():
    def handler(request):
        return httpx.Response(
            429,
            json={"ok": False, "error_code": 429, "description": "Too Many Requests: private text",
                  "parameters": {"retry_after": 7}},
        )

    async with client_for(handler) as http:
        with pytest.raises(BotApiError) as raised:
            await BotApi(BOT_TOKEN, http).send_message(1, "секретный текст")

    error = raised.value
    assert (error.kind, error.status, error.error_code, error.retry_after) == ("api", 429, 429, 7)
    assert error.category == "api:429" and not error.permanent and not error.conflict
    assert BOT_TOKEN not in repr(error) and "секретный" not in repr(error) and "private" not in repr(error)
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.parametrize(
    "response, expected",
    [
        (httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}), ("api", True, False)),
        (httpx.Response(404, json={"ok": False, "error_code": 404, "description": "Not Found"}), ("api", True, False)),
        (httpx.Response(409, json={"ok": False, "error_code": 409, "description": "Conflict"}), ("api", False, True)),
        (httpx.Response(403, json={"ok": False, "error_code": 403, "description": "blocked"}), ("api", False, False)),
        (httpx.Response(200, content=b"not json"), ("malformed", False, False)),
        (httpx.Response(200, json=["a", "list"]), ("malformed", False, False)),
        (httpx.Response(502, content=b"<html>bad gateway</html>"), ("malformed", False, False)),
    ],
)
@pytest.mark.asyncio
async def test_every_answer_that_is_not_ok_is_classified(response, expected):
    async with client_for(lambda request: response) as http:
        with pytest.raises(BotApiError) as raised:
            await BotApi(BOT_TOKEN, http).send_chat_action(1)

    kind, permanent, conflict = expected
    assert (raised.value.kind, raised.value.permanent, raised.value.conflict) == (kind, permanent, conflict)


@pytest.mark.parametrize(
    "failure, kind",
    [(httpx.ConnectTimeout("timed out"), "timeout"), (httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("refused"), "network")],
)
@pytest.mark.asyncio
async def test_a_network_failure_is_a_kind_and_does_not_keep_the_exception_that_may_quote_the_url(failure, kind):
    def handler(request):
        raise failure

    async with client_for(handler) as http:
        with pytest.raises(BotApiError) as raised:
            await BotApi(BOT_TOKEN, http).get_updates(None, 1)

    assert raised.value.kind == kind and raised.value.__cause__ is None
    assert BOT_TOKEN not in repr(raised.value) and "api.telegram.org" not in str(raised.value)


@pytest.mark.asyncio
async def test_the_updates_request_asks_for_messages_only_and_for_what_comes_after_the_offset():
    seen = []

    def handler(request):
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": []})

    async with client_for(handler) as http:
        api = BotApi(BOT_TOKEN, http)
        await api.get_updates(None, 25)
        await api.get_updates(41, 25)

    assert seen == [
        {"timeout": 25, "allowed_updates": ["message"]},
        {"timeout": 25, "allowed_updates": ["message"], "offset": 41},
    ]


@pytest.mark.asyncio
async def test_a_message_is_plain_text_with_the_link_preview_switched_off_the_current_way():
    import json

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with client_for(handler) as http:
        await BotApi(BOT_TOKEN, http).send_message(42, "https://example.test/рецепт")

    # `link_preview_options` is the Bot API's current field; `disable_web_page_preview` is the deprecated
    # one it replaced, and there is no parse mode (a stray "_" or "<" in a recipe must not fail a delivery).
    assert seen == [
        {"chat_id": 42, "text": "https://example.test/рецепт", "link_preview_options": {"is_disabled": True}}
    ]


@pytest.mark.parametrize("token", ["", "abc", "123:short", "x" * 40, "123456789:" + "!" * 35, BOT_TOKEN + " "])
@pytest.mark.asyncio
async def test_a_string_that_is_not_a_token_is_not_accepted(token):
    async with client_for(lambda request: httpx.Response(200)) as http:
        with pytest.raises(ValueError):
            BotApi(token, http)


# --- files ------------------------------------------------------------------------------------------


BAD_PATHS = ["../secret", "photos/../../x", "photos//x", "/abs/path", "a b/c", "photos/x?y=1", "", "photos/x#frag", "a/./b/.."]


@pytest.mark.asyncio
async def test_a_file_path_from_the_server_is_checked_before_it_is_put_in_a_url():
    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": {"file_path": handler.path, "file_size": 5}})

    async with client_for(handler) as http:
        api = BotApi(BOT_TOKEN, http)
        handler.path = "photos/file_1.jpg"
        assert await api.get_file("f") == ("photos/file_1.jpg", 5)
        for path in BAD_PATHS:
            handler.path = path
            with pytest.raises(BotApiError) as raised:
                await api.get_file("f")
            assert raised.value.kind == "malformed", path


@pytest.mark.asyncio
async def test_the_download_refuses_the_same_paths_without_making_a_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"x")

    async with client_for(handler) as http:
        for path in BAD_PATHS:
            with pytest.raises(BotApiError):
                await BotApi(BOT_TOKEN, http).download(path, 10)

    assert calls == []


@pytest.mark.asyncio
async def test_a_download_stops_at_its_limit_and_is_never_read_to_the_end():
    async with client_for(lambda request: httpx.Response(200, content=b"x" * 1000)) as http:
        api = BotApi(BOT_TOKEN, http)
        assert await api.download("photos/a.jpg", 1000) == b"x" * 1000
        with pytest.raises(BotApiError) as raised:
            await api.download("photos/a.jpg", 999)

    assert raised.value.kind == "too_large"


@pytest.mark.asyncio
async def test_a_download_that_declares_too_much_is_refused_before_any_byte_is_read():
    class Endless(httpx.AsyncByteStream):
        read = 0

        async def __aiter__(self):
            while True:
                Endless.read += 1
                yield b"x" * 1024

    def handler(request):
        return httpx.Response(200, headers={"content-length": "999999999"}, stream=Endless())

    async with client_for(handler) as http:
        with pytest.raises(BotApiError) as raised:
            await BotApi(BOT_TOKEN, http).download("photos/a.jpg", 5000)

    assert raised.value.kind == "too_large" and Endless.read == 0


@pytest.mark.parametrize("status", [404, 403, 500])
@pytest.mark.asyncio
async def test_a_file_that_is_not_there_is_an_http_failure(status):
    async with client_for(lambda request: httpx.Response(status)) as http:
        with pytest.raises(BotApiError) as raised:
            await BotApi(BOT_TOKEN, http).download("photos/a.jpg", 100)

    assert (raised.value.kind, raised.value.status) == ("http", status)


# --- long text -------------------------------------------------------------------------------------------


def test_a_short_text_is_one_piece_and_nothing_is_one_none():
    assert split_text("Привет") == ["Привет"]
    assert split_text("   ") == [] and split_text("") == []


def test_a_long_text_is_cut_where_there_is_a_break_and_no_piece_is_too_long():
    paragraphs = [("абв " * 300).strip() for _ in range(8)]
    pieces = split_text("\n\n".join(paragraphs))

    assert len(pieces) >= 3 and all(0 < len(p) <= MAX_MESSAGE_LENGTH for p in pieces)
    assert all(p == p.strip() for p in pieces)
    assert "\n\n".join(pieces).split() == "\n\n".join(paragraphs).split()


def test_a_text_with_no_break_at_all_is_cut_at_the_limit():
    pieces = split_text("я" * (MAX_MESSAGE_LENGTH * 2 + 10))

    assert [len(p) for p in pieces] == [MAX_MESSAGE_LENGTH, MAX_MESSAGE_LENGTH, 10]


def test_a_small_limit_is_honoured():
    assert split_text("one two three four", limit=9) == ["one two", "three", "four"]


# --- the token in logs --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        f'HTTP Request: POST https://api.telegram.org/bot{BOT_TOKEN}/getUpdates "HTTP/1.1 200 OK"',
        f"GET https://api.telegram.org/file/bot{BOT_TOKEN}/photos/file_1.jpg",
        f"https://api.telegram.org/bot{BOT_TOKEN}",
        f"'/bot{BOT_TOKEN}/sendMessage'",
    ],
)
def test_the_token_is_hidden_wherever_a_url_carries_it(line):
    cleaned = redact_bot_token(line)

    assert BOT_TOKEN not in cleaned and BOT_TOKEN.split(":")[1] not in cleaned
    assert "bot[redacted]" in cleaned


def test_text_without_a_token_is_not_touched():
    for text in ("HTTP Request: GET https://example.com/bot/info", "/bot123/x", "plain text"):
        assert redact_bot_token(text) == text


def test_the_filter_rewrites_what_the_http_libraries_log_and_is_installed_once(caplog):
    install_bot_token_redaction()
    install_bot_token_redaction()
    logger = logging.getLogger("httpx")
    assert sum(isinstance(f, RedactBotToken) for f in logger.filters) == 1

    caplog.set_level(logging.INFO, logger="httpx")
    url = httpx.URL(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates")
    logger.info('HTTP Request: %s %s "%s %d %s"', "POST", url, "HTTP/1.1", 200, "OK")

    assert BOT_TOKEN not in caplog.text and "bot[redacted]/getUpdates" in caplog.text


def test_the_token_pattern_accepts_real_shapes_only():
    assert TOKEN_PATTERN.fullmatch(BOT_TOKEN)
    assert not TOKEN_PATTERN.fullmatch("12345:short")


# --- the settings --------------------------------------------------------------------------------------------------


GOOD = {TELEGRAM_BOT_TOKEN_ENV: BOT_TOKEN, TELEGRAM_BOT_USERNAME_ENV: "@" + BOT_NAME}


def test_both_variables_make_the_bot_and_neither_makes_none():
    settings = load_bot_settings(GOOD)

    assert (settings.username, settings.token, settings.poll_seconds) == (BOT_NAME, BOT_TOKEN, 25)
    assert settings.link_base == f"https://t.me/{BOT_NAME}"
    assert load_bot_settings({}) is None
    assert load_bot_settings({TELEGRAM_BOT_TOKEN_ENV: "", TELEGRAM_BOT_USERNAME_ENV: ""}) is None
    assert load_web_settings({"HOUSE_HELPER_DATABASE_URL": "sqlite:///x.db"}).telegram_bot is None
    assert load_web_settings({"HOUSE_HELPER_DATABASE_URL": "sqlite:///x.db", **GOOD}).telegram_bot == settings


@pytest.mark.parametrize("missing", [TELEGRAM_BOT_TOKEN_ENV, TELEGRAM_BOT_USERNAME_ENV])
def test_one_variable_without_the_other_refuses_to_start(missing):
    partial = {name: value for name, value in GOOD.items() if name != missing}

    with pytest.raises(WebConfigError, match=missing):
        load_bot_settings(partial)


@pytest.mark.parametrize(
    "username", ["", "bot", "abcd", "house_helper", "Bot Name bot", "1house_bot", "x" * 33 + "bot", "house-helper-bot", "house_helper_botx"]
)
def test_a_username_that_cannot_be_a_bots_is_refused_without_being_quoted(username):
    with pytest.raises(WebConfigError) as raised:
        BotSettings(token=BOT_TOKEN, username=username)

    # one fixed sentence for every input: nothing of the value in it
    assert str(raised.value) == (
        f"{TELEGRAM_BOT_USERNAME_ENV} must be the bot's username, like house_helper_bot"
    )


@pytest.mark.parametrize("username", ["house_helper_app_bot", "HouseHelperBot", "abc_bot", "a1234bot"])
def test_a_username_in_any_case_that_ends_in_bot_is_accepted(username):
    assert BotSettings(token=BOT_TOKEN, username=username).username == username


@pytest.mark.parametrize("token", ["", "nope", "123:abc", BOT_TOKEN + "\n"])
def test_a_token_that_is_not_one_is_refused_without_being_quoted(token):
    with pytest.raises(WebConfigError) as raised:
        BotSettings(token=token, username=BOT_NAME)

    assert str(raised.value) == f"{TELEGRAM_BOT_TOKEN_ENV} is not a bot token"


def test_the_token_does_not_appear_in_the_repr_of_the_settings():
    assert BOT_TOKEN not in repr(load_bot_settings(GOOD))
    assert BOT_TOKEN.split(":")[1] not in repr(load_web_settings({"HOUSE_HELPER_DATABASE_URL": "sqlite:///x.db", **GOOD}))


@pytest.mark.parametrize("wait", [0, 51, -1, 2.5, True])
def test_the_polling_wait_is_bounded(wait):
    with pytest.raises(WebConfigError):
        BotSettings(token=BOT_TOKEN, username=BOT_NAME, poll_seconds=wait)
