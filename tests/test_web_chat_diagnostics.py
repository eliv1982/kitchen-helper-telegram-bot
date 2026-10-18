"""What the chat API tells, and what it must never tell.

Every error has a server-made correlation id in the body, the header and the log line
that describes it, so that a screenshot can be matched to the server's side. And
nothing that would not be safe to paste into a ticket gets into either: not the text
of an exception (the provider's quotes the request and the key, SQLAlchemy's quotes the
statement's parameters), not a traceback, not what the person wrote or the assistant
answered, not the cookie, not the idempotency key.
"""

import asyncio
import logging
import re
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter
from openai import AuthenticationError, BadRequestError, InternalServerError

import api_client
from house_helper.core.backend import (
    BackendConnectionError,
    BackendError,
    BackendRateLimitedError,
    OpenAIBackend,
)
from house_helper.core.identity import ConversationKey
from house_helper.persistence import conversations, turns
from house_helper.web.chat_api import ChatRoute
from tests.auth_support import ORIGIN
from tests.chat_support import ScriptedBackend, chat_app, error_of, log_text, post_headers
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

CHAT_LOGGERS = ("house_helper.web.chat", "house_helper.web.turn_runner", "house_helper.web.chat_api")
FAILURE_LINE = re.compile(
    r"Chat request failed: correlation_id=(?P<id>hh-[0-9a-f]{16}) code=(?P<code>\w+) "
    r"status=(?P<status>\d+) route=(?P<route>.+?) category=(?P<category>\S+)"
)


def chat_records(caplog):
    return [r for r in caplog.records if r.name in CHAT_LOGGERS]


def failure_lines(caplog):
    return [m.groupdict() for r in chat_records(caplog) if (m := FAILURE_LINE.search(r.getMessage()))]


def chat_log_text(caplog) -> str:
    return "\n".join(f"{r.name} {r.levelname} {r.getMessage()}" for r in chat_records(caplog))


@pytest.fixture(autouse=True)
def watch(caplog):
    caplog.set_level(logging.DEBUG)


def operational_error():
    return sa.exc.OperationalError(
        "INSERT INTO messages (content) VALUES (?)", {"content": "SECRET-SQL-PARAMETER"}, Exception("SECRET-DRIVER-TEXT")
    )


# --- one id, in the body, the header and the log ---------------------------------------------------------

SCENARIOS = [
    pytest.param("signed_out", 401, "not_authenticated", "-", id="not-signed-in"),
    pytest.param("foreign_origin", 403, "forbidden_origin", "-", id="foreign-origin"),
    pytest.param("unknown_conversation", 404, "conversation_not_found", "-", id="unknown-conversation"),
    pytest.param("bad_json", 400, "invalid_request", "MalformedJson", id="bad-json"),
    pytest.param("empty", 400, "empty_message", "-", id="empty-message"),
    pytest.param("rate_limited", 503, "provider_rate_limited", "BackendRateLimitedError", id="rate-limited"),
    pytest.param("unreachable", 502, "provider_unreachable", "BackendConnectionError", id="unreachable"),
    pytest.param("provider_error", 502, "provider_error", "BackendError", id="provider-error"),
    pytest.param("bug", 500, "internal_error", "RuntimeError", id="bug-in-the-backend"),
    pytest.param("database", 503, "service_unavailable", "OperationalError", id="database"),
    pytest.param("route_bug", 500, "internal_error", "RuntimeError", id="bug-in-a-route"),
]


@pytest.mark.parametrize(("scenario", "status", "code", "category"), SCENARIOS)
async def test_every_error_has_one_id_in_the_body_the_header_and_the_log(
    tmp_path, caplog, monkeypatch, scenario, status, code, category
):
    script = {
        "rate_limited": [BackendRateLimitedError("SECRET-PROVIDER-TEXT")],
        "unreachable": [BackendConnectionError("SECRET-PROVIDER-TEXT")],
        "provider_error": [BackendError("SECRET-PROVIDER-TEXT")],
        "bug": [RuntimeError("SECRET-BUG-TEXT")],
    }.get(scenario, [])
    async with chat_app(tmp_path, ScriptedBackend(*script), sign_in_first=scenario != "signed_out") as chat:
        conversation = await chat.create() if scenario != "signed_out" else str(uuid4())
        url = f"/api/conversations/{conversation}/messages"
        if scenario == "signed_out":
            response = await chat.client.get("/api/conversations")
        elif scenario == "foreign_origin":
            response = await chat.client.post("/api/conversations", json={}, headers={"Origin": "https://evil.example"})
        elif scenario == "unknown_conversation":
            response = await chat.history(str(uuid4()))
        elif scenario == "bad_json":
            response = await chat.client.post(
                url, content=b"{", headers=post_headers("k", **{"Content-Type": "application/json"})
            )
        elif scenario == "empty":
            response = await chat.send(conversation, " ", "k")
        elif scenario == "database":
            with monkeypatch.context() as broken:
                broken.setattr(turns, "begin_turn", lambda *a, **k: (_ for _ in ()).throw(operational_error()))
                response = await chat.send(conversation, "привет", "k")
        elif scenario == "route_bug":
            with monkeypatch.context() as broken:
                broken.setattr(
                    conversations, "list_conversations",
                    lambda *a, **k: (_ for _ in ()).throw(RuntimeError("SECRET-ROUTE-TEXT")),
                )
                response = await chat.client.get("/api/conversations")
        else:
            response = await chat.send(conversation, "привет", "k")

        body = error_of(response)
        assert (response.status_code, body["detail"]) == (status, code)
        identifier = body["correlation_id"]
        assert re.fullmatch(r"hh-[0-9a-f]{16}", identifier)
        lines = [line for line in failure_lines(caplog) if line["id"] == identifier]
        assert len(lines) == 1, "one line for one failure"  # logged once, not once per layer
        assert (lines[0]["code"], lines[0]["status"], lines[0]["category"]) == (code, str(status), category)
        assert lines[0]["route"].startswith(("GET /api/", "POST /api/"))
        text = log_text(caplog) + response.text
        for leak in (
            "SECRET-PROVIDER-TEXT",
            "SECRET-BUG-TEXT",
            "SECRET-ROUTE-TEXT",
            "SECRET-SQL-PARAMETER",
            "SECRET-DRIVER-TEXT",
        ):
            assert leak not in text, leak


async def test_the_failure_log_line_names_the_route_by_its_template_not_its_url(tmp_path, caplog):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = str(uuid4())

        await chat.history(conversation)

        (line,) = failure_lines(caplog)
        assert line["route"] == "GET /api/conversations/{conversation_id}/messages"
        assert conversation not in chat_log_text(caplog)


async def test_a_completed_turn_is_logged_with_the_id_of_its_response(tmp_path, caplog):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.send(conversation, "привет", "k")

        completed = [r.getMessage() for r in chat_records(caplog) if "Chat turn completed" in r.getMessage()]
        assert len(completed) == 1
        assert f"correlation_id={response.headers['x-correlation-id']}" in completed[0]
        assert f"conversation={conversation}" in completed[0] and "attempt=1" in completed[0]


async def test_the_assistant_not_being_configured_is_logged_for_the_operator(tmp_path, caplog):
    async with chat_app(tmp_path, None) as chat:
        conversation = await chat.create()

        response = await chat.send(conversation, "привет", "k")

        (line,) = [l for l in failure_lines(caplog) if l["id"] == response.headers["x-correlation-id"]]
        assert (line["code"], line["category"]) == ("chat_unavailable", "NotConfigured")
        assert [r.levelname for r in chat_records(caplog) if "chat_unavailable" in r.getMessage()] == ["WARNING"]


async def test_a_failure_after_the_client_has_gone_is_still_logged_with_an_id(tmp_path, caplog):
    backend = ScriptedBackend(BackendError("SECRET-PROVIDER-TEXT"))
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        request = asyncio.ensure_future(chat.send(conversation, "привет", "k"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        release.set()
        await chat.settle()

        (line,) = failure_lines(caplog)  # nobody was left to be answered; the log has it all the same
        assert (line["code"], line["status"]) == ("provider_error", "502")
        assert "SECRET-PROVIDER-TEXT" not in log_text(caplog)


# --- nothing from the request, the exception or the conversation gets into a log ------------------------------


async def test_no_log_line_holds_what_was_said_or_the_credentials_of_the_request(tmp_path, caplog):
    said = "ОЧЕНЬ-ЛИЧНЫЙ-ВОПРОС-про-борщ"
    answered = "ОЧЕНЬ-ЛИЧНЫЙ-ОТВЕТ-про-борщ"
    key = "idempotency-key-9F3A7B"
    backend = ScriptedBackend(answered, BackendError("x"), RuntimeError("y"))
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        session_cookie = [c.value for c in chat.client.cookies.jar if c.name.endswith("hh_session")][0]
        await chat.send(conversation, said, key)  # answered
        await chat.send(conversation, said + "2", key + "-2")  # fails
        await chat.send(conversation, said + "3", key + "-3")  # fails
        await chat.send(conversation, said, key)  # replayed
        await chat.send(conversation, "\x00" + said, key + "-4")  # refused
        await chat.history(conversation)

        text = log_text(caplog)
        for secret in (said, answered, key, session_cookie, "борщ"):
            assert secret not in text, secret


async def test_the_text_of_a_provider_exception_reaches_neither_the_log_nor_the_client(tmp_path, caplog):
    leaky = "Incorrect API key provided: sk-proj-SECRETKEY123. Your request: 'ЛИЧНЫЙ ВОПРОС'"
    cause = BackendError(leaky)
    cause.__cause__ = RuntimeError(leaky)
    async with chat_app(tmp_path, ScriptedBackend(cause)) as chat:
        conversation = await chat.create()

        response = await chat.send(conversation, "привет", "k")

        assert response.status_code == 502
        for text in (response.text, log_text(caplog)):
            assert "sk-proj" not in text and "SECRETKEY" not in text and "ЛИЧНЫЙ" not in text


async def test_a_database_error_is_logged_by_its_class_not_its_statement(tmp_path, caplog, monkeypatch):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        monkeypatch.setattr(turns, "begin_turn", lambda *a, **k: (_ for _ in ()).throw(operational_error()))

        response = await chat.send(conversation, "привет", "k")

        assert response.status_code == 503
        text = log_text(caplog) + response.text
        for leak in ("SECRET-SQL-PARAMETER", "SECRET-DRIVER-TEXT", "INSERT INTO", "VALUES"):
            assert leak not in text, leak
        assert "OperationalError" in chat_log_text(caplog)
        assert "Traceback" not in text


async def test_an_unexpected_error_is_a_catalogue_error_with_no_traceback_anywhere(
    tmp_path, caplog, monkeypatch
):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        monkeypatch.setattr(
            conversations, "list_conversations", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("SECRET-ROUTE-TEXT"))
        )

        response = await chat.client.get("/api/conversations")

        assert response.status_code == 500
        body = error_of(response)
        assert body == {
            "detail": "internal_error",
            "message": "Что-то пошло не так. Попробуйте ещё раз чуть позже.",
            "retryable": True,
            "correlation_id": response.headers["x-correlation-id"],
        }
        for text in (response.text, log_text(caplog)):
            assert "Traceback" not in text and "SECRET-ROUTE-TEXT" not in text
            assert "File \"" not in text and "RuntimeError" not in response.text
        assert all(record.exc_info is None for record in caplog.records if record.name in CHAT_LOGGERS)
        assert "RuntimeError" in chat_log_text(caplog)  # the class: a category an operator can use


async def test_the_server_never_answers_with_an_exception_page(tmp_path, monkeypatch):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        monkeypatch.setattr(
            conversations, "list_messages", lambda *a, **k: (_ for _ in ()).throw(KeyError("SECRET"))
        )

        response = await chat.history(conversation)

        assert response.headers["content-type"].startswith("application/json")
        assert "SECRET" not in response.text and "KeyError" not in response.text


# --- FastAPI's own validation can never quote the input -------------------------------------------------------


async def test_a_validation_error_of_the_framework_is_the_catalogues_and_does_not_echo(tmp_path, caplog):
    probe = APIRouter(prefix="/api", route_class=ChatRoute)

    @probe.get("/_probe")
    async def probe_route(number: int) -> dict:  # a declared parameter: FastAPI validates it
        return {"number": number}

    def configure(app):
        app.include_router(probe)

    async with chat_app(tmp_path, ScriptedBackend(), configure=configure) as chat:
        response = await chat.client.get("/api/_probe", params={"number": "ZQX-NOT-A-NUMBER"})

        body = error_of(response)
        assert (response.status_code, body["detail"]) == (400, "invalid_request")
        assert "ZQX" not in response.text and "ZQX" not in log_text(caplog)
        assert "int_parsing" not in response.text and '"loc"' not in response.text
        (line,) = failure_lines(caplog)
        assert line["category"] == "RequestValidationError"


# --- the provider's own logging -----------------------------------------------------------------------------------


def status_error(error_class, status, message):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return error_class(message, response=httpx.Response(status, request=request), body=None)


class RaisingClient:
    def __init__(self, error):
        async def create(**kwargs):
            raise error

        self.chat = type("Chat", (), {"completions": type("Completions", (), {"create": staticmethod(create)})()})()


@pytest.mark.parametrize(
    ("error_class", "status"),
    [(AuthenticationError, 401), (BadRequestError, 400), (InternalServerError, 500)],
)
async def test_the_telegram_provider_path_logs_the_class_and_status_not_the_message(
    monkeypatch, caplog, error_class, status
):
    message = "Incorrect API key provided: sk-proj-SECRETKEY123. You sent: 'ЛИЧНОЕ СООБЩЕНИЕ'"
    monkeypatch.setattr(
        api_client, "get_client", lambda: RaisingClient(status_error(error_class, status, message))
    )

    with pytest.raises(BackendError):
        await OpenAIBackend().complete(
            [{"role": "user", "content": "ЛИЧНОЕ СООБЩЕНИЕ"}],
            conversation=ConversationKey("telegram", "7"),
            context_len=1,
        )

    text = log_text(caplog)
    assert "sk-proj" not in text and "SECRETKEY" not in text and "ЛИЧНОЕ" not in text
    assert f"OpenAI API error for user_id=telegram:7 ({error_class.__name__}, status={status})" in text


async def test_the_telegram_provider_path_still_logs_the_other_failures_as_it_did(monkeypatch, caplog):
    from tests.helpers import make_connection_error, make_rate_limit_error, make_timeout_error

    for error in (make_rate_limit_error(), make_timeout_error(), make_connection_error()):
        monkeypatch.setattr(api_client, "get_client", lambda error=error: RaisingClient(error))
        with pytest.raises(BackendError):
            await OpenAIBackend().complete(
                [{"role": "user", "content": "привет"}],
                conversation=ConversationKey("telegram", "7"),
                context_len=1,
            )

    assert [r.getMessage() for r in caplog.records if r.name == "api_client" and r.levelname == "ERROR"] == [
        "OpenAI rate limit exceeded for user_id=telegram:7",
        "OpenAI request timed out for user_id=telegram:7",
        "OpenAI connection error for user_id=telegram:7",
    ]


# --- Origin of a log line: the request lines of the server --------------------------------------------------------


async def test_the_chat_does_not_log_the_identity_of_the_person(tmp_path, caplog):
    async with chat_app(tmp_path, ScriptedBackend(BackendError("x"))) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "привет", "k")
        await chat.history(str(uuid4()))

        assert str(chat.user_id) not in chat_log_text(caplog)
        assert ORIGIN not in chat_log_text(caplog)
