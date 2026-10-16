"""Test seams between the test suite and the current implementation.

Bot-level tests go through this module instead of reaching into bot.py,
context_manager.py or api_client.py directly. It is the single place that
knows how the application is currently wired: which module owns conversation
state, where the per-user lock lives, which function performs the AI call, how
much history is retained and how the Telegram handlers are invoked.

When the implementation is restructured, this is the only test-support file
that should need to change; the tests themselves keep describing behavior.

Nothing here touches the network: the AI call is replaced by a stub, and the
wire harness replaces aiogram's HTTP layer with an offline session.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, SendChatAction, SendMessage
from aiogram.types import Chat, Message, MessageEntity, Update, User

import api_client
import bot as bot_module
import config
import context_manager
from house_helper.core.identity import ConversationKey

ChatTurn = dict[str, str]
AiHandler = Callable[[list[ChatTurn], int], Awaitable[str]]

# The error the AI layer raises for a missing/empty/unusable completion.
InvalidAiResponseError = api_client.InvalidResponseError


# --- state ------------------------------------------------------------------


def reset_state() -> None:
    """Drop all process-wide state so every test starts from a clean slate."""
    context_manager._contexts.clear()
    bot_module._user_locks.clear()
    api_client._client = None
    config._settings = None


def conversation_key(user_id: int) -> ConversationKey:
    """The key under which a Telegram sender's conversation is stored.

    Spelled out here rather than taken from bot.py on purpose: this is the
    contract the rest of the suite relies on, so a change to how the
    application namespaces Telegram conversations has to show up as a failing
    test instead of being silently mirrored.
    """
    return ConversationKey("telegram", str(user_id))


def history_of(key: ConversationKey) -> list[ChatTurn]:
    """Snapshot of a conversation's stored history, addressed by key."""
    return [dict(turn) for turn in context_manager.get_context(key)]


def history(user_id: int) -> list[ChatTurn]:
    """Snapshot of a user's stored conversation history."""
    return history_of(conversation_key(user_id))


def seed_conversation(
    key: ConversationKey, messages: list[tuple[str, str]]
) -> list[ChatTurn]:
    """Directly store (role, content) turns under any conversation key.
    Returns a snapshot of the resulting history for later comparison."""
    for role, content in messages:
        context_manager.add_message(key, role, content)
    return history_of(key)


def seed_messages(user_id: int, messages: list[tuple[str, str]]) -> list[ChatTurn]:
    """Directly store (role, content) turns as already-completed history.
    Returns a snapshot of the resulting history for later comparison."""
    return seed_conversation(conversation_key(user_id), messages)


def stored_conversation_keys() -> set[ConversationKey]:
    """Every key that currently has conversation state (even if empty --
    merely reading a history creates its entry)."""
    return set(context_manager._contexts)


def seed_exchanges(user_id: int, pairs: int = 10) -> list[ChatTurn]:
    """Directly seed `pairs` completed user/assistant exchanges (2*pairs
    messages), landing exactly at the history capacity (20) by default.
    Returns a snapshot of the seeded history for later comparison."""
    messages: list[tuple[str, str]] = []
    for i in range(1, pairs + 1):
        messages.append(("user", f"seed-user-{i}"))
        messages.append(("assistant", f"seed-assistant-{i}"))
    return seed_messages(user_id, messages)


def history_capacity() -> int:
    """Maximum number of stored history messages per user."""
    return context_manager.MAX_HISTORY_MESSAGES


# --- input limit ------------------------------------------------------------


def max_input_length() -> int:
    """Maximum accepted length (in characters) of one incoming message."""
    return bot_module.MAX_INPUT_LENGTH


def set_max_input_length(monkeypatch, value: int) -> None:
    monkeypatch.setattr(bot_module, "MAX_INPUT_LENGTH", value)


# --- per-user serialization -------------------------------------------------


def lock_for(key: ConversationKey) -> asyncio.Lock:
    """The serialization lock guarding one conversation."""
    return bot_module._get_user_lock(key)


def lock_keys() -> set[ConversationKey]:
    """Every key for which a serialization lock has been created."""
    return set(bot_module._user_locks)


def is_user_busy(user_id: int) -> bool:
    """True while some operation holds this user's serialization lock."""
    return lock_for(conversation_key(user_id)).locked()


async def settle(turns: int = 25) -> None:
    """Let every already-runnable task make progress, then return.

    Used to assert that something is *still* blocked ("has not happened yet")
    without sleeping on a real clock: it only yields to the event loop, so it
    is deterministic as long as nothing under test waits on a timer.
    """
    for _ in range(turns):
        await asyncio.sleep(0)


# --- AI stand-in ------------------------------------------------------------


@dataclass
class AiCall:
    history: list[ChatTurn]  # what was sent; ends with the pending user message
    user_id: int


@dataclass
class RecordingAi:
    """Async AI stand-in that records every call it receives.

    `handler(history, user_id)` produces the reply text (or raises, to
    simulate a failure). Without a handler it replies "ok-reply".
    """

    handler: AiHandler | None = None
    calls: list[AiCall] = field(default_factory=list)

    @property
    def called(self) -> bool:
        return bool(self.calls)

    async def __call__(self, history: list[ChatTurn], user_id: int) -> str:
        self.calls.append(AiCall(history=history, user_id=user_id))
        if self.handler is None:
            return "ok-reply"
        return await self.handler(history, user_id)


def install_ai(monkeypatch, handler: AiHandler) -> None:
    """Route the application's AI call to `handler(history, user_id) -> reply`.

    The handler receives a snapshot of the history that would be sent to the
    model (excluding the system prompt). Anything it raises reaches the
    application exactly as the real AI layer's failure would.
    """

    async def _adapter(messages, user_id, context_len):
        return await handler([dict(turn) for turn in messages], user_id), None

    monkeypatch.setattr(api_client, "get_chat_response", _adapter)


def install_backend(monkeypatch, backend) -> None:
    """Answer every conversation with `backend` (any ChatBackend) instead of
    the OpenAI adapter. Unlike install_ai, nothing of api_client is involved."""
    monkeypatch.setattr(bot_module, "_backend", backend)


# --- Telegram handlers ------------------------------------------------------


async def process_text(message, bot) -> None:
    """Deliver an incoming plain-text message to the application."""
    await bot_module.handle_text(message, bot)


async def process_reset_command(message) -> None:
    """Deliver an incoming /reset command to the application."""
    await bot_module.cmd_reset(message)


async def process_start_command(message) -> None:
    """Deliver an incoming /start command to the application."""
    await bot_module.cmd_start(message)


# --- wire-level harness -----------------------------------------------------


class _OfflineSession(BaseSession):
    """aiogram session that records requests instead of sending them.

    It has no HTTP client at all, so nothing can leave the process even if a
    test mistakenly triggers a real Telegram call.
    """

    def __init__(self, on_request: Callable[[Any], Any]) -> None:
        super().__init__()
        self._on_request = on_request

    async def close(self) -> None:
        return None

    async def make_request(self, bot, method, timeout=None):
        return self._on_request(method)

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):
        raise RuntimeError("offline test session cannot stream content")
        yield b""  # pragma: no cover - marks this as an async generator


class WireTelegram:
    """Feeds real aiogram Updates through the real Dispatcher.

    Routing (which handler an update reaches) and the shape of the outgoing
    Telegram requests are exercised for real; only the HTTP layer is replaced
    by an offline recording session. Every request is answered with `True`,
    except getMe, which reports the bot's own identity (aiogram asks for it
    when a command carries an @mention).
    """

    BOT_USERNAME = "kitchen_helper_bot"

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.bot = Bot(token="123456:TEST-TOKEN", session=_OfflineSession(self._answer))
        self._next_update_id = 1

    def _answer(self, method):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return User(
                id=123456, is_bot=True, first_name="Kitchen", username=self.BOT_USERNAME
            )
        return True

    @property
    def sent_messages(self) -> list[tuple[int, str]]:
        return [
            (request.chat_id, request.text)
            for request in self.requests
            if isinstance(request, SendMessage)
        ]

    @property
    def chat_actions(self) -> list[tuple[int, str]]:
        return [
            (request.chat_id, request.action)
            for request in self.requests
            if isinstance(request, SendChatAction)
        ]

    def _message(self, text: str | None, user_id: int, chat_id: int) -> Message:
        entities = None
        if text and text.startswith("/"):
            entities = [
                MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))
            ]
        message_id = self._next_update_id
        return Message(
            message_id=message_id,
            date=datetime.datetime(2026, 1, 1),
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
            from_user=User(id=user_id, is_bot=False, first_name="Tester"),
            text=text,
            entities=entities,
        )

    async def send(self, text: str | None, *, user_id: int, chat_id: int | None = None):
        """Feed an incoming message update; returns the Dispatcher's result."""
        update = Update(
            update_id=self._next_update_id,
            message=self._message(text, user_id, chat_id if chat_id is not None else user_id),
        )
        self._next_update_id += 1
        return await bot_module.dp.feed_update(self.bot, update)

    async def send_edited(self, text: str, *, user_id: int, chat_id: int | None = None):
        """Feed an edited-message update (which the application does not handle)."""
        update = Update(
            update_id=self._next_update_id,
            edited_message=self._message(
                text, user_id, chat_id if chat_id is not None else user_id
            ),
        )
        self._next_update_id += 1
        return await bot_module.dp.feed_update(self.bot, update)

    async def close(self) -> None:
        await self.bot.session.close()
