"""Contract of the AI backend boundary (house_helper.core.backend).

Covers the OpenAI adapter on its own, the channel-qualified log label it
produces from a ConversationKey, and a fake backend standing in for it. How the
bot reacts to each failure kind is pinned end to end by the characterization
suite; the injection tests here only show that the bot depends on the
interface and nothing else.
"""

import asyncio
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import httpx
import pytest
from openai import AuthenticationError, BadRequestError, InternalServerError

import api_client
from house_helper.core.backend import (
    BackendConnectionError,
    BackendEmptyResponseError,
    BackendError,
    BackendRateLimitedError,
    BackendReply,
    ChatBackend,
    OpenAIBackend,
)
from house_helper.core.identity import ConversationKey
from tests import expected_texts, seams
from tests.helpers import (
    FakeBackend,
    FakeBot,
    FakeMessage,
    make_connection_error,
    make_generic_api_error,
    make_openai_response,
    make_rate_limit_error,
    make_status_error,
    make_timeout_error,
)

USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
MESSAGES = [{"role": "user", "content": "привет"}]
CONVERSATION = ConversationKey("telegram", "7")


class _FatalSignal(BaseException):
    """Stands in for a non-Exception BaseException (e.g. KeyboardInterrupt)."""


def _provider_raising(error):
    async def get_chat_response(messages, user_id, context_len):
        raise error

    return get_chat_response


# --- the OpenAI adapter -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [USAGE, None], ids=["with-usage", "without-usage"])
async def test_complete_returns_the_reply_and_forwards_the_call_unchanged(monkeypatch, usage):
    backend = OpenAIBackend()  # built before the patch: the lookup must be late
    calls = []

    async def get_chat_response(messages, user_id, context_len):
        calls.append((messages, user_id, context_len))
        return "готово", usage

    monkeypatch.setattr(api_client, "get_chat_response", get_chat_response)

    reply = await backend.complete(MESSAGES, conversation=CONVERSATION, context_len=1)

    assert reply == BackendReply(text="готово", usage=usage)
    assert len(calls) == 1
    assert calls[0][0] is MESSAGES  # passed through, not copied or rewritten
    assert calls[0][1:] == ("telegram:7", 1)


FAILURES = [
    pytest.param(make_rate_limit_error, BackendRateLimitedError, id="rate-limit"),
    pytest.param(make_timeout_error, BackendConnectionError, id="timeout"),
    pytest.param(make_connection_error, BackendConnectionError, id="connection"),
    pytest.param(
        lambda: api_client.InvalidResponseError("empty"),
        BackendEmptyResponseError,
        id="invalid-response",
    ),
    pytest.param(
        lambda: make_status_error(InternalServerError, 500), BackendError, id="server-500"
    ),
    pytest.param(
        lambda: make_status_error(AuthenticationError, 401), BackendError, id="auth-401"
    ),
    pytest.param(
        lambda: make_status_error(BadRequestError, 400), BackendError, id="bad-request-400"
    ),
    pytest.param(make_generic_api_error, BackendError, id="generic-api-error"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("make_error", "expected_kind"), FAILURES)
async def test_provider_failures_become_the_matching_backend_error(
    monkeypatch, make_error, expected_kind
):
    original = make_error()
    monkeypatch.setattr(api_client, "get_chat_response", _provider_raising(original))

    with pytest.raises(BackendError) as raised:
        await OpenAIBackend().complete(MESSAGES, conversation=CONVERSATION, context_len=1)

    assert type(raised.value) is expected_kind  # the exact kind, never a broader one
    assert raised.value.__cause__ is original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_error",
    [
        lambda: ValueError("synthetic"),
        lambda: KeyError("synthetic"),
        lambda: TimeoutError("synthetic"),
        lambda: httpx.ConnectError("synthetic"),
        lambda: _FatalSignal("synthetic"),
        lambda: asyncio.CancelledError(),
    ],
    ids=["value", "key", "builtin-timeout", "httpx", "base-exception", "cancelled"],
)
async def test_anything_unrecognized_propagates_untouched(monkeypatch, make_error):
    original = make_error()
    monkeypatch.setattr(api_client, "get_chat_response", _provider_raising(original))

    with pytest.raises(type(original)) as raised:
        await OpenAIBackend().complete(MESSAGES, conversation=CONVERSATION, context_len=1)

    assert raised.value is original


def test_the_specific_failure_kinds_are_distinct_backend_errors():
    kinds = [BackendRateLimitedError, BackendConnectionError, BackendEmptyResponseError]

    assert all(issubclass(kind, BackendError) for kind in kinds)
    for kind in kinds:
        assert not any(issubclass(kind, other) for other in kinds if other is not kind)


# --- the contract: a conversation key, not a Telegram user id -------------------


@pytest.mark.parametrize(
    "backend_type",
    [ChatBackend, OpenAIBackend, FakeBackend],
    ids=["protocol", "openai", "fake"],
)
def test_complete_takes_a_conversation_key_and_no_user_id(backend_type):
    parameters = inspect.signature(backend_type.complete).parameters

    assert list(parameters) == ["self", "messages", "conversation", "context_len"]
    assert parameters["conversation"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["context_len"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("backend_type", [ChatBackend, OpenAIBackend], ids=["protocol", "openai"])
def test_the_conversation_parameter_is_typed_as_a_conversation_key(backend_type):
    parameters = inspect.signature(backend_type.complete, eval_str=True).parameters

    assert parameters["conversation"].annotation is ConversationKey


# --- the channel-qualified log label --------------------------------------------


def _fake_openai_client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.mark.asyncio
async def test_request_and_success_logs_carry_the_channel_qualified_label(monkeypatch, caplog):
    create = AsyncMock(return_value=make_openai_response("Свари яйца всмятку."))
    monkeypatch.setattr(api_client, "get_client", lambda: _fake_openai_client(create))

    with caplog.at_level(logging.INFO, logger="api_client"):
        reply = await OpenAIBackend().complete(
            MESSAGES, conversation=CONVERSATION, context_len=3
        )

    assert reply == BackendReply(text="Свари яйца всмятку.", usage=USAGE)
    logged = [record.getMessage() for record in caplog.records]
    assert any("user_id=telegram:7, context_len=3" in line for line in logged)


@pytest.mark.asyncio
async def test_failure_logs_carry_the_channel_qualified_label(monkeypatch, caplog):
    create = AsyncMock(side_effect=make_rate_limit_error())
    monkeypatch.setattr(api_client, "get_client", lambda: _fake_openai_client(create))

    with caplog.at_level(logging.INFO, logger="api_client"):
        with pytest.raises(BackendRateLimitedError):
            await OpenAIBackend().complete(MESSAGES, conversation=CONVERSATION, context_len=3)

    logged = [record.getMessage() for record in caplog.records]
    assert "OpenAI rate limit exceeded for user_id=telegram:7" in logged


# --- Telegram and Web conversations with the same subject -----------------------

TELEGRAM_39 = ConversationKey("telegram", "39")
WEB_39 = ConversationKey("web", "39")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conversation", "label"),
    [
        pytest.param(TELEGRAM_39, "telegram:39", id="telegram"),
        pytest.param(WEB_39, "web:39", id="web"),
    ],
)
async def test_the_provider_layer_is_given_the_channel_qualified_label(
    monkeypatch, conversation, label
):
    labels = []

    async def get_chat_response(messages, user_id, context_len):
        labels.append(user_id)
        return "готово", None

    monkeypatch.setattr(api_client, "get_chat_response", get_chat_response)

    await OpenAIBackend().complete(MESSAGES, conversation=conversation, context_len=1)

    assert labels == [label]


@pytest.mark.asyncio
async def test_one_subject_on_two_channels_reaches_the_provider_layer_as_two_labels(monkeypatch):
    labels = []

    async def get_chat_response(messages, user_id, context_len):
        labels.append(user_id)
        return "готово", None

    monkeypatch.setattr(api_client, "get_chat_response", get_chat_response)
    backend = OpenAIBackend()

    await backend.complete(MESSAGES, conversation=TELEGRAM_39, context_len=1)
    await backend.complete(MESSAGES, conversation=WEB_39, context_len=1)

    assert labels == ["telegram:39", "web:39"]
    assert len(set(labels)) == 2


@pytest.mark.asyncio
async def test_request_logs_tell_a_same_subject_conversation_on_each_channel_apart(
    monkeypatch, caplog
):
    create = AsyncMock(return_value=make_openai_response("Свари яйца всмятку."))
    monkeypatch.setattr(api_client, "get_client", lambda: _fake_openai_client(create))
    backend = OpenAIBackend()

    with caplog.at_level(logging.INFO, logger="api_client"):
        await backend.complete(MESSAGES, conversation=TELEGRAM_39, context_len=3)
        await backend.complete(MESSAGES, conversation=WEB_39, context_len=3)

    logged = [record.getMessage() for record in caplog.records]
    telegram_line, web_line = [line for line in logged if line.startswith("OpenAI request:")]
    assert "user_id=telegram:39, context_len=3" in telegram_line
    assert "user_id=web:39, context_len=3" in web_line
    assert not any("user_id=39" in line for line in logged)  # never the bare subject


@pytest.mark.asyncio
async def test_failure_logs_tell_a_same_subject_conversation_on_each_channel_apart(
    monkeypatch, caplog
):
    create = AsyncMock(side_effect=make_rate_limit_error())
    monkeypatch.setattr(api_client, "get_client", lambda: _fake_openai_client(create))
    backend = OpenAIBackend()

    with caplog.at_level(logging.INFO, logger="api_client"):
        for conversation in (TELEGRAM_39, WEB_39):
            with pytest.raises(BackendRateLimitedError):
                await backend.complete(MESSAGES, conversation=conversation, context_len=3)

    logged = [record.getMessage() for record in caplog.records]
    assert [line for line in logged if line.startswith("OpenAI rate limit exceeded")] == [
        "OpenAI rate limit exceeded for user_id=telegram:39",
        "OpenAI rate limit exceeded for user_id=web:39",
    ]


# --- a fake backend, offline ----------------------------------------------------


@pytest.fixture
def no_provider(monkeypatch):
    """Make any use of api_client during the test a loud failure."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("api_client must not be used when a backend is injected")

    monkeypatch.setattr(api_client, "get_chat_response", forbidden)
    monkeypatch.setattr(api_client, "get_client", forbidden)


@pytest.mark.asyncio
async def test_the_bot_answers_through_an_injected_backend(monkeypatch, no_provider):
    backend = FakeBackend(reply="из фейка")
    seams.install_backend(monkeypatch, backend)
    msg = FakeMessage(user_id=31, text="что приготовить")

    await seams.process_text(msg, FakeBot())

    assert msg.answer.await_args_list == [call("из фейка")]
    assert backend.requests == [
        ([{"role": "user", "content": "что приготовить"}], seams.conversation_key(31), 1)
    ]
    assert seams.history(31) == [
        {"role": "user", "content": "что приготовить"},
        {"role": "assistant", "content": "из фейка"},
    ]


@pytest.mark.asyncio
async def test_a_same_subject_web_conversation_is_neither_sent_nor_touched(
    monkeypatch, no_provider
):
    web = ConversationKey("web", "33")
    web_history = seams.seed_conversation(
        web, [("user", "из веба"), ("assistant", "ответ в вебе")]
    )
    backend = FakeBackend(reply="из фейка")
    seams.install_backend(monkeypatch, backend)

    await seams.process_text(FakeMessage(user_id=33, text="привет"), FakeBot())

    # The backend is asked about the Telegram conversation only, with only its history.
    assert backend.requests == [
        ([{"role": "user", "content": "привет"}], ConversationKey("telegram", "33"), 1)
    ]
    assert backend.requests[0][1] != web
    assert seams.history_of(web) == web_history


BACKEND_FAILURES = [
    pytest.param(BackendRateLimitedError, expected_texts.RATE_LIMITED, id="rate-limited"),
    pytest.param(BackendConnectionError, expected_texts.CONNECTION_FAILED, id="connection"),
    pytest.param(BackendEmptyResponseError, expected_texts.EMPTY_RESPONSE, id="empty"),
    pytest.param(BackendError, expected_texts.API_ERROR, id="generic"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("error_class", "expected_text"), BACKEND_FAILURES)
async def test_an_injected_backends_failure_gets_its_message_and_a_clean_rollback(
    monkeypatch, no_provider, error_class, expected_text
):
    prior = seams.seed_messages(32, [("user", "раньше"), ("assistant", "ответ")])
    seams.install_backend(monkeypatch, FakeBackend(error=error_class("synthetic")))
    msg = FakeMessage(user_id=32, text="что приготовить")

    await seams.process_text(msg, FakeBot())

    assert msg.answer.await_args_list == [call(expected_text)]
    assert seams.history(32) == prior
    assert not seams.is_user_busy(32)
