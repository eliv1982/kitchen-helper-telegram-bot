"""How the application gets its assistant: from the settings, from a test's backend, or
not at all; and what that does to the web process's boundaries.

The real OpenAI backend is used here with the SDK's client replaced by a fake, so the
whole path (HTTP request, runner, backend, request to "OpenAI", reply) is exercised
without a socket. What the process loads while it starts and serves is checked in a
fresh interpreter (tests/test_web_boundary.py has the probe).
"""

import logging

import openai
import pytest

from house_helper.core.kitchen_prompt import KITCHEN_SYSTEM_PROMPT
from house_helper.core.openai_chat import StandaloneOpenAIBackend
from house_helper.core.provider_settings import ProviderSettings
from house_helper.web.turn_runner import WebTurnRunner
from tests.chat_support import ScriptedBackend, chat_app, error_of
from tests.test_openai_chat import FakeClient, response
from tests.test_web_boundary import _run_probe

PROVIDER = ProviderSettings(
    api_key="sk-test-key", model="test-model", temperature=0.2, max_tokens=1234, timeout_seconds=7.0, max_retries=0
)


@pytest.fixture
def sdk(monkeypatch):
    """The SDK's client class, replaced: every client built is kept."""
    built = []

    def build(**kwargs):
        client = FakeClient(response("Омлет: взбейте яйца."), **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", build)
    return built


@pytest.mark.asyncio
async def test_with_the_assistants_settings_a_message_goes_through_the_real_backend(tmp_path, sdk):
    async with chat_app(tmp_path, None, provider=PROVIDER) as chat:
        assert isinstance(chat.app.state.chat, WebTurnRunner)
        assert sdk == []  # starting the application built no client
        conversation = await chat.create()

        first = await chat.send(conversation, "Что из яиц?", "k-1")
        second = await chat.send(conversation, "А без молока?", "k-2")

        assert first.status_code == second.status_code == 201
        assert first.json()["assistant_message"]["text"] == "Омлет: взбейте яйца."
        (client,) = sdk  # one client for the life of the application, built by the first message
        assert client.init == {"api_key": "sk-test-key", "timeout": 7.0, "max_retries": 0}
        one, two = client.calls
        assert one == {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": KITCHEN_SYSTEM_PROMPT},
                {"role": "user", "content": "Что из яиц?"},
            ],
            "temperature": 0.2,
            "max_tokens": 1234,
        }
        assert two["messages"][1:] == [
            {"role": "user", "content": "Что из яиц?"},
            {"role": "assistant", "content": "Омлет: взбейте яйца."},
            {"role": "user", "content": "А без молока?"},
        ]
    assert [c.closed for c in sdk] == [1]  # and closed with the application, once


@pytest.mark.asyncio
async def test_a_provider_failure_through_the_real_backend_is_the_catalogues_error(tmp_path, sdk, caplog):
    caplog.set_level(logging.DEBUG)
    async with chat_app(tmp_path, None, provider=PROVIDER) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "Что из яиц?", "k-0")
        sdk[0].outcome = openai.AuthenticationError(
            "Incorrect API key provided: sk-proj-SECRETKEY123",
            response=__import__("httpx").Response(401, request=__import__("httpx").Request("POST", "https://x.test")),
            body=None,
        )

        failed = await chat.send(conversation, "А без молока?", "k-1")

        assert failed.status_code == 502
        assert error_of(failed)["detail"] == "provider_error"
        everything = failed.text + "\n".join(r.getMessage() for r in caplog.records)
        assert "SECRETKEY" not in everything and "sk-proj" not in everything


@pytest.mark.asyncio
async def test_an_injected_backend_wins_and_is_not_closed_by_the_application(tmp_path, sdk):
    backend = ScriptedBackend("Омлет.")
    closed = []
    backend.aclose = lambda: closed.append(1)  # (would be awaited if the application owned it)
    async with chat_app(tmp_path, backend, provider=PROVIDER) as chat:
        conversation = await chat.create()
        sent = await chat.send(conversation, "Что из яиц?", "k-1")

        assert sent.json()["assistant_message"]["text"] == "Омлет."
        assert chat.app.state.chat._backend is backend

    assert closed == [] and sdk == []


@pytest.mark.asyncio
async def test_without_settings_or_a_backend_there_is_no_runner_and_no_client(tmp_path, sdk):
    async with chat_app(tmp_path, None) as chat:
        assert chat.app.state.chat is None
        conversation = await chat.create()

        assert (await chat.send(conversation, "привет", "k")).status_code == 503

    assert sdk == []


@pytest.mark.asyncio
async def test_the_backend_built_from_settings_is_the_standalone_one(tmp_path, sdk):
    async with chat_app(tmp_path, None, provider=PROVIDER) as chat:
        backend = chat.app.state.chat._backend

        assert type(backend) is StandaloneOpenAIBackend


@pytest.mark.asyncio
async def test_the_runner_is_gone_after_the_application_stops(tmp_path, sdk):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        app = chat.app
        assert app.state.chat is not None

    assert app.state.chat is None and app.state.auth is None


@pytest.mark.asyncio
async def test_the_start_says_whether_the_assistant_is_on(tmp_path, sdk, caplog):
    caplog.set_level(logging.INFO)

    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    async with chat_app(tmp_path / "on", None, provider=PROVIDER):
        pass
    async with chat_app(tmp_path / "off", None):
        pass

    started = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Web application started")]
    assert [line.rsplit("assistant: ", 1)[1].rstrip(")") for line in started] == ["on", "off"]
    assert all("sk-test-key" not in line for line in started)


# --- the process boundaries -------------------------------------------------------------------------------------


def test_a_process_with_a_key_and_no_bot_token_starts_and_serves_loading_nothing_of_the_bot(tmp_path):
    report = _run_probe(
        tmp_path,
        ["house_helper.web.asgi"],
        extra_env={"OPENAI_API_KEY": "sk-test-key-not-real", "MAX_TOKENS": "1500"},
    )

    assert report["bot_token_in_environment"] is False
    assert report["openai_key_in_environment"] is True
    assert report["health"] == [200, {"status": "ok"}]
    assert report["loaded_after_run"] == []  # no openai SDK, no api_client, no config, no aiogram
    assert report["dotenv_calls_after_run"] == 0
    assert report["files_after_run"] == []


def test_a_process_with_a_key_and_a_malformed_companion_refuses_to_start_without_quoting_it(tmp_path):
    completed = _run_probe(
        tmp_path,
        ["house_helper.web.asgi"],
        extra_env={"OPENAI_API_KEY": "sk-test-key-not-real", "TEMPERATURE": "SECRET-LOOKING-VALUE"},
        check=False,
    )

    assert completed.returncode != 0
    assert "WebConfigError" in completed.stderr and "TEMPERATURE must be a number" in completed.stderr
    assert "SECRET-LOOKING-VALUE" not in completed.stderr and "sk-test-key-not-real" not in completed.stderr


def test_a_process_without_a_key_still_starts(tmp_path):
    report = _run_probe(tmp_path, ["house_helper.web.asgi"], extra_env={"TEMPERATURE": "hot"})

    assert report["health"] == [200, {"status": "ok"}]
    assert report["loaded_after_run"] == []
