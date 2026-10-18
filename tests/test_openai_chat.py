"""The web chat's own OpenAI backend (core.openai_chat.StandaloneOpenAIBackend).

No real client is ever built or called: the SDK's client is a fake that records what it
is asked and answers (or fails) as the test says. What is pinned: the request it makes,
how every provider failure becomes a backend error, that an answer cut off at the length
cap is reported, that nothing of the provider's own text or the conversation's reaches a
log, that the client is built late and closed once, and that this module never goes
through api_client or config (the Telegram side).
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import httpx
import openai
import pytest
from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from house_helper.core.backend import (
    BackendConnectionError,
    BackendEmptyResponseError,
    BackendError,
    BackendRateLimitedError,
    BackendReply,
)
from house_helper.core.identity import ConversationKey
from house_helper.core.kitchen_prompt import KITCHEN_SYSTEM_PROMPT
from house_helper.core.openai_chat import StandaloneOpenAIBackend
from house_helper.core.provider_settings import ProviderSettings
from tests.test_persistence_boundary import REPO_ROOT

SETTINGS = ProviderSettings(
    api_key="sk-test-key", model="test-model", temperature=0.3, max_tokens=777, timeout_seconds=9.0, max_retries=2
)
CONVERSATION = ConversationKey("web", "0f0f0f0f-0000-4000-8000-000000000001")
MESSAGES = [{"role": "user", "content": "что из яиц?"}]


def response(content="Сварите яйца.", finish_reason="stop", usage=(10, 5, 15), choices=True):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage_object = None
    if usage is not None:
        usage_object = SimpleNamespace(
            prompt_tokens=usage[0], completion_tokens=usage[1], total_tokens=usage[2]
        )
    return SimpleNamespace(choices=[choice] if choices else [], usage=usage_object)


class FakeClient:
    """Shaped like AsyncOpenAI for what the backend uses of it."""

    def __init__(self, outcome=None, **kwargs):
        self.init = kwargs
        self.outcome = outcome if outcome is not None else response()
        self.calls = []
        self.closed = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def close(self):
        self.closed += 1


def request():
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def status_error(error_class, status, message="status error"):
    return error_class(message, response=httpx.Response(status, request=request()), body=None)


async def complete(backend, messages=MESSAGES):
    return await backend.complete(messages, conversation=CONVERSATION, context_len=len(messages))


# --- the request ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_request_has_the_shared_prompt_the_history_and_the_configured_limits():
    client = FakeClient()

    reply = await complete(StandaloneOpenAIBackend(SETTINGS, client=client))

    assert reply == BackendReply(
        text="Сварите яйца.", usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}, truncated=False
    )
    (call,) = client.calls
    assert call == {
        "model": "test-model",
        "messages": [{"role": "system", "content": KITCHEN_SYSTEM_PROMPT}, *MESSAGES],
        "temperature": 0.3,
        "max_tokens": 777,
    }
    assert call["messages"][0]["content"] is KITCHEN_SYSTEM_PROMPT
    assert MESSAGES == [{"role": "user", "content": "что из яиц?"}]  # the caller's list is not touched


@pytest.mark.asyncio
async def test_the_history_is_passed_in_order():
    client = FakeClient()
    history = [
        {"role": "user", "content": "один"},
        {"role": "assistant", "content": "два"},
        {"role": "user", "content": "три"},
    ]

    await complete(StandaloneOpenAIBackend(SETTINGS, client=client), history)

    assert client.calls[0]["messages"][1:] == history


@pytest.mark.asyncio
async def test_the_answer_without_usage_is_still_an_answer():
    reply = await complete(StandaloneOpenAIBackend(SETTINGS, client=FakeClient(response(usage=None))))

    assert reply.text == "Сварите яйца." and reply.usage is None


@pytest.mark.asyncio
async def test_malformed_usage_never_turns_an_answer_into_a_failure():
    outcome = response()
    outcome.usage = SimpleNamespace(prompt_tokens=1)  # no completion_tokens

    reply = await complete(StandaloneOpenAIBackend(SETTINGS, client=FakeClient(outcome)))

    assert reply.text == "Сварите яйца." and reply.usage is None


@pytest.mark.parametrize(
    ("finish_reason", "truncated"),
    [("length", True), ("stop", False), ("content_filter", False), (None, False), ("tool_calls", False)],
)
@pytest.mark.asyncio
async def test_an_answer_cut_off_at_the_length_cap_is_reported(finish_reason, truncated):
    reply = await complete(
        StandaloneOpenAIBackend(SETTINGS, client=FakeClient(response("Омлет: яйца, мол", finish_reason)))
    )

    assert reply.truncated is truncated
    assert reply.text == "Омлет: яйца, мол"  # the text itself is passed on as it came


# --- failures -----------------------------------------------------------------------------------------------------

FAILURES = [
    pytest.param(lambda: status_error(RateLimitError, 429), BackendRateLimitedError, id="rate-limit"),
    pytest.param(lambda: APITimeoutError(request=request()), BackendConnectionError, id="timeout"),
    pytest.param(lambda: APIConnectionError(request=request()), BackendConnectionError, id="connection"),
    pytest.param(lambda: status_error(InternalServerError, 500), BackendError, id="server-500"),
    pytest.param(lambda: status_error(AuthenticationError, 401), BackendError, id="auth-401"),
    pytest.param(lambda: status_error(BadRequestError, 400), BackendError, id="bad-request-400"),
    pytest.param(lambda: APIError("generic", request(), body=None), BackendError, id="generic"),
]


@pytest.mark.parametrize(("make_error", "expected"), FAILURES)
@pytest.mark.asyncio
async def test_provider_failures_become_the_matching_backend_error(make_error, expected):
    original = make_error()

    with pytest.raises(BackendError) as raised:
        await complete(StandaloneOpenAIBackend(SETTINGS, client=FakeClient(original)))

    assert type(raised.value) is expected  # the exact kind, never a broader one
    assert raised.value.__cause__ is original


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(response(choices=False), id="no-choices"),
        pytest.param(response(content=None), id="no-content"),
        pytest.param(response(content=""), id="empty"),
        pytest.param(response(content="  \n\t"), id="blank"),
        pytest.param(response(content=["not", "text"]), id="not-text"),
        pytest.param(SimpleNamespace(choices=[SimpleNamespace(message=None, finish_reason="stop")], usage=None), id="no-message"),
        pytest.param(SimpleNamespace(usage=None), id="no-choices-attribute"),
    ],
)
@pytest.mark.asyncio
async def test_an_unusable_response_is_an_empty_response_error(outcome):
    with pytest.raises(BackendEmptyResponseError):
        await complete(StandaloneOpenAIBackend(SETTINGS, client=FakeClient(outcome)))


class _Fatal(BaseException):
    pass


@pytest.mark.parametrize(
    "error",
    [ValueError("synthetic"), KeyError("synthetic"), TimeoutError("synthetic"), httpx.ConnectError("synthetic"), _Fatal("x"), asyncio.CancelledError()],
    ids=["value", "key", "builtin-timeout", "httpx", "base-exception", "cancelled"],
)
@pytest.mark.asyncio
async def test_anything_unrecognized_propagates_untouched(error):
    with pytest.raises(type(error)) as raised:
        await complete(StandaloneOpenAIBackend(SETTINGS, client=FakeClient(error)))

    assert raised.value is error


# --- what is logged ---------------------------------------------------------------------------------------------------


def log_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "house_helper.core.openai_chat"]


@pytest.mark.asyncio
async def test_the_request_is_logged_without_the_conversation_text_or_the_key(caplog):
    caplog.set_level(logging.DEBUG)

    await complete(StandaloneOpenAIBackend(SETTINGS, client=FakeClient()), [{"role": "user", "content": "ЛИЧНЫЙ ВОПРОС"}])

    lines = log_lines(caplog)
    assert (
        "OpenAI request: model=test-model, temperature=0.3, max_tokens=777, "
        f"conversation=web:{CONVERSATION.subject}, context_len=1"
    ) in lines
    assert "OpenAI usage: input_tokens=10, output_tokens=5, total_tokens=15" in lines
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert "ЛИЧНЫЙ" not in everything and "sk-test-key" not in everything and "Сварите" not in everything


@pytest.mark.parametrize(
    ("error_class", "status"),
    [(AuthenticationError, 401), (BadRequestError, 400), (InternalServerError, 500), (RateLimitError, 429)],
)
@pytest.mark.asyncio
async def test_a_failure_is_logged_by_class_and_status_never_by_the_providers_own_text(
    caplog, error_class, status
):
    caplog.set_level(logging.DEBUG)
    leaky = "Incorrect API key provided: sk-proj-SECRETKEY123. Request: 'ЛИЧНЫЙ ВОПРОС'"

    with pytest.raises(BackendError):
        await complete(
            StandaloneOpenAIBackend(SETTINGS, client=FakeClient(status_error(error_class, status, leaky))),
            [{"role": "user", "content": "ЛИЧНЫЙ ВОПРОС"}],
        )

    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRETKEY" not in everything and "sk-proj" not in everything and "ЛИЧНЫЙ" not in everything
    assert any(error_class.__name__ in line and f"status={status}" in line for line in log_lines(caplog))
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.asyncio
async def test_a_connection_failure_is_logged_without_a_status(caplog):
    caplog.set_level(logging.DEBUG)

    with pytest.raises(BackendConnectionError):
        await complete(StandaloneOpenAIBackend(SETTINGS, client=FakeClient(APITimeoutError(request=request()))))

    assert any("APITimeoutError, status=None" in line for line in log_lines(caplog))


@pytest.mark.asyncio
async def test_a_cut_off_answer_is_logged_as_a_warning(caplog):
    caplog.set_level(logging.DEBUG)

    await complete(StandaloneOpenAIBackend(SETTINGS, client=FakeClient(response(finish_reason="length"))))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [f"OpenAI answer was cut off at the length cap for conversation=web:{CONVERSATION.subject}"]


# --- the client: late, once, closed ---------------------------------------------------------------------------------------


@pytest.fixture
def fake_sdk(monkeypatch):
    built = []

    def build(**kwargs):
        client = FakeClient(**kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", build)
    return built


@pytest.mark.asyncio
async def test_no_client_exists_until_the_first_request_and_then_exactly_one(fake_sdk):
    backend = StandaloneOpenAIBackend(SETTINGS)
    assert fake_sdk == []

    await complete(backend)
    await complete(backend)

    assert len(fake_sdk) == 1
    assert fake_sdk[0].init == {"api_key": "sk-test-key", "timeout": 9.0, "max_retries": 2}
    assert len(fake_sdk[0].calls) == 2


@pytest.mark.asyncio
async def test_closing_closes_the_client_it_built_once_and_a_later_request_builds_a_new_one(fake_sdk):
    backend = StandaloneOpenAIBackend(SETTINGS)
    await complete(backend)

    await backend.aclose()
    await backend.aclose()

    assert [client.closed for client in fake_sdk] == [1]
    await complete(backend)
    assert len(fake_sdk) == 2


@pytest.mark.asyncio
async def test_closing_before_any_request_builds_nothing(fake_sdk):
    await StandaloneOpenAIBackend(SETTINGS).aclose()

    assert fake_sdk == []


@pytest.mark.asyncio
async def test_a_client_that_was_handed_in_is_not_closed_by_the_backend():
    client = FakeClient()
    backend = StandaloneOpenAIBackend(SETTINGS, client=client)
    await complete(backend)

    await backend.aclose()

    assert client.closed == 0


# --- it is not the Telegram path ---------------------------------------------------------------------------------------------

_PROBE = """
import asyncio, json, sys, types
root = sys.argv[1]
sys.path.insert(0, root)
dotenv = types.ModuleType("dotenv")
calls = []
dotenv.load_dotenv = lambda *a, **k: calls.append(1) or False
sys.modules["dotenv"] = dotenv

from types import SimpleNamespace
from house_helper.core.identity import ConversationKey
from house_helper.core.openai_chat import StandaloneOpenAIBackend
from house_helper.core.provider_settings import ProviderSettings

watched = ["api_client", "config", "bot", "aiogram", "context_manager", "openai"]
after_import = [name for name in watched if name in sys.modules]

class Client:
    def __init__(self):
        async def create(**kwargs):
            message = SimpleNamespace(content="ok")
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

backend = StandaloneOpenAIBackend(ProviderSettings(api_key="k"), client=Client())
reply = asyncio.run(backend.complete([{"role": "user", "content": "hi"}], conversation=ConversationKey("web", "1"), context_len=1))
print(json.dumps({
    "after_import": after_import,
    "after_use": [name for name in ["api_client", "config", "bot", "aiogram", "context_manager"] if name in sys.modules],
    "dotenv_calls": len(calls),
    "reply": reply.text,
}))
"""


def test_the_backend_never_loads_the_telegram_side_or_reads_an_env_file():
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            name: value
            for name, value in os.environ.items()
            if name not in {"BOT_TOKEN", "OPENAI_API_KEY"}  # nothing of the bot's
        },
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.splitlines()[-1])
    assert report["after_import"] == []  # not even the SDK is loaded by importing it
    assert report["after_use"] == []  # ... and using it loads no Telegram code, api_client or config
    assert report["dotenv_calls"] == 0
    assert report["reply"] == "ok"
