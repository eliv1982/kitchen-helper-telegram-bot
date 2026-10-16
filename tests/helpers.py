import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from aiogram.methods import SendMessage
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)

from house_helper.core.backend import BackendReply
from house_helper.core.identity import ConversationKey


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    """Stand-in for aiogram's Message, exposing only what the handlers use."""

    def __init__(self, user_id: int, text: str, chat_id: int | None = None) -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
        self.chat = FakeChat(chat_id if chat_id is not None else user_id)
        self.answer = AsyncMock()


class FakeBot:
    """Stand-in for aiogram's Bot, only used for the typing indicator."""

    def __init__(self) -> None:
        self.id = 1
        self.send_chat_action = AsyncMock()


def make_openai_response(content: str | None, usage: tuple[int, int, int] | None = (10, 5, 15)):
    """Build a lightweight object shaped like an OpenAI ChatCompletion."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    usage_obj = None
    if usage is not None:
        prompt, completion, total = usage
        usage_obj = SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=total
        )
    return SimpleNamespace(choices=[choice], usage=usage_obj)


def _openai_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def make_rate_limit_error() -> RateLimitError:
    response = httpx.Response(status_code=429, request=_openai_request())
    return RateLimitError("Rate limited", response=response, body=None)


def make_status_error(error_class: type[APIStatusError], status_code: int) -> APIStatusError:
    """Build an OpenAI HTTP-status error (e.g. InternalServerError, 500)."""
    response = httpx.Response(status_code=status_code, request=_openai_request())
    return error_class("status error", response=response, body=None)


def make_timeout_error() -> APITimeoutError:
    return APITimeoutError(request=_openai_request())


def make_connection_error() -> APIConnectionError:
    return APIConnectionError(request=_openai_request())


def make_generic_api_error() -> APIError:
    return APIError("generic api error", _openai_request(), body=None)


class FakeBackend:
    """Offline ChatBackend: records each request and answers without a network.

    Replies `reply`, or raises `error` if one is given. `requests` holds a
    snapshot of every call as (messages, conversation, context_len).
    """

    def __init__(self, reply: str = "fake-reply", error: BaseException | None = None) -> None:
        self.reply = reply
        self.error = error
        self.requests: list[tuple[list[dict[str, str]], ConversationKey, int]] = []

    async def complete(self, messages, *, conversation, context_len) -> BackendReply:
        self.requests.append(([dict(turn) for turn in messages], conversation, context_len))
        if self.error is not None:
            raise self.error
        return BackendReply(text=self.reply)


def telegram_method() -> SendMessage:
    """A throwaway Telegram method, as aiogram's API errors require one."""
    return SendMessage(chat_id=1, text="x")


class DelayedDelivery:
    """Makes `message.answer` block until released, logging into `log`.

    Records ("deliver-start", text) when delivery begins and
    ("deliver-done", text) when it completes, so tests can assert exactly how
    deliveries and other events interleave.
    """

    def __init__(self, message: FakeMessage, log: list[tuple[str, str]]) -> None:
        self.started = asyncio.Event()
        self._gate = asyncio.Event()
        self._log = log
        message.answer.side_effect = self._answer

    async def _answer(self, text, **kwargs) -> None:
        self._log.append(("deliver-start", text))
        self.started.set()
        await self._gate.wait()
        self._log.append(("deliver-done", text))

    def release(self) -> None:
        self._gate.set()


def record_delivery(message: FakeMessage, log: list[tuple[str, str]]) -> None:
    """Log ("deliver", text) whenever `message.answer` is awaited."""

    async def _answer(text, **kwargs) -> None:
        log.append(("deliver", text))

    message.answer.side_effect = _answer
