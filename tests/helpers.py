from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from openai import RateLimitError


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


def make_rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code=429, request=request)
    return RateLimitError("Rate limited", response=response, body=None)
