"""Shared helpers for the Telegram bot tests.

Nothing leaves the process. The Bot API is a FakeBot: an httpx.MockTransport that answers the
calls the bot makes (getUpdates, sendMessage, sendChatAction, getFile and the file download),
records what was sent to whom, can be told to fail a call, and holds updates until the bot's
poll asks for them. The application is the real one, on a real migrated SQLite file, with the
assistant a ScriptedBackend (tests/chat_support.py); the website is reached through httpx's
ASGI transport, signed in as the provisioned user through the fake Telegram login.

Most tests hand an update straight to TelegramChat.handle_update (it runs the whole flow and
returns when the answer has been sent) and then wait for the copies to Telegram that a web
turn starts (settle). A few push the update through the fake's queue and let the polling loop
find it.
"""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID

import httpx

from house_helper.persistence.models import Conversation, Message, TelegramLink, TelegramLinkCode, TurnRequest
from house_helper.web.settings import BotSettings
from tests.chat_support import ChatHarness, ScriptedBackend, post_headers
from tests.auth_support import auth_app, sign_in
from tests.web_support import TIMEOUT

# Shaped like a real token (the library checks the shape) and nothing else.
BOT_TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
BOT_NAME = "house_helper_app_bot"

MOTHER = 555000111  # what the Bot API says a sender's id is; never equal to a sign-in `sub`
SISTER = 555000222
STRANGER = 777000999


def bot_settings(**options: Any) -> BotSettings:
    return BotSettings(token=BOT_TOKEN, username=BOT_NAME, **options)


class FakeBot:
    """The Bot API, as the bot sees it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []  # sendMessage calls, in order
        self.actions: list[dict[str, Any]] = []  # sendChatAction calls
        self.requests: list[httpx.Request] = []  # every call, URLs included
        self.files: dict[str, tuple[str, bytes]] = {}  # file_id -> (file_path, content)
        self.failures: dict[str, list[Any]] = {}  # method -> error codes to answer, one per call
        self.offsets: list[Optional[int]] = []
        self.waits: list[Optional[int]] = []  # the `timeout` each getUpdates asked for
        self._pending: list[dict[str, Any]] = []
        self._changed = asyncio.Event()
        self._sent_changed = asyncio.Event()
        self.poll_wait = 0.02  # how long a getUpdates with nothing to return waits
        self._send_gate: Optional[asyncio.Event] = None

    # --- the test's side -------------------------------------------------------------------

    def push(self, update: dict[str, Any]) -> None:
        self._pending.append(update)
        self._changed.set()

    def hold_sends(self) -> asyncio.Event:
        """From now on a sendMessage waits until the returned event is set."""
        self._send_gate = asyncio.Event()
        return self._send_gate

    def fail(self, method: str, *codes: int) -> None:
        self.failures.setdefault(method, []).extend(codes)

    def texts(self, chat_id: Optional[int] = None) -> list[str]:
        return [call["text"] for call in self.sent if chat_id is None or call["chat_id"] == chat_id]

    async def wait_for_messages(self, count: int, chat_id: Optional[int] = None) -> list[str]:
        async def reached() -> None:
            while len(self.texts(chat_id)) < count:
                self._sent_changed.clear()
                await self._sent_changed.wait()

        await asyncio.wait_for(reached(), TIMEOUT)
        return self.texts(chat_id)

    # --- the network -----------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @staticmethod
    def _error(code: int, description: str) -> httpx.Response:
        return httpx.Response(
            code if code != 429 else 429,
            json={"ok": False, "error_code": code, "description": description},
        )

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        file_match = re.fullmatch(r"/file/bot([^/]+)/(.+)", path)
        if file_match:
            if file_match.group(1) != BOT_TOKEN:
                return httpx.Response(401)
            wanted = file_match.group(2)
            for file_path, content in self.files.values():
                if file_path == wanted:
                    return httpx.Response(200, content=content)
            return httpx.Response(404)
        match = re.fullmatch(r"/bot([^/]+)/([A-Za-z]+)", path)
        if not match:
            return httpx.Response(404)
        if match.group(1) != BOT_TOKEN:
            return self._error(401, "Unauthorized")
        method = match.group(2)
        body = json.loads(request.content or b"{}")
        queued = self.failures.get(method)
        if queued:
            code = queued.pop(0)
            if code:
                return self._error(code, "Forbidden: bot was blocked by the user")
        if method == "getUpdates":
            return await self._get_updates(body)
        if method == "sendMessage":
            if self._send_gate is not None:
                await self._send_gate.wait()
            self.sent.append(body)
            self._sent_changed.set()
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        if method == "sendChatAction":
            self.actions.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "getFile":
            entry = self.files.get(body.get("file_id"))
            if entry is None:
                return self._error(400, "Bad Request: wrong file_id")
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_id": body["file_id"], "file_size": len(entry[1]), "file_path": entry[0]}},
            )
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 123456789, "is_bot": True, "username": BOT_NAME}})
        return self._error(404, "Not Found")

    async def _get_updates(self, body: dict[str, Any]) -> httpx.Response:
        offset = body.get("offset")
        self.offsets.append(offset)
        self.waits.append(body.get("timeout"))
        if offset is not None:
            self._pending = [u for u in self._pending if u["update_id"] >= offset]
        if not self._pending:
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), self.poll_wait)
            except asyncio.TimeoutError:
                pass
        result = [u for u in self._pending if offset is None or u["update_id"] >= offset]
        return httpx.Response(200, json={"ok": True, "result": result})


# --- updates, as Telegram sends them ------------------------------------------------------


def private_message(
    update_id: int,
    account: int,
    text: Optional[str] = None,
    *,
    first_name: str = "Елена",
    username: Optional[str] = "mama_elena",
    **extra: Any,
) -> dict[str, Any]:
    sender: dict[str, Any] = {"id": account, "is_bot": False, "first_name": first_name}
    if username is not None:
        sender["username"] = username
    message: dict[str, Any] = {
        "message_id": update_id * 10,
        "from": sender,
        "chat": {"id": account, "type": "private", "first_name": first_name},
        "date": 1790000000,
    }
    if text is not None:
        message["text"] = text
    message.update(extra)
    return {"update_id": update_id, "message": message}


def photo_message(
    update_id: int,
    account: int,
    file_id: str = "photo-1",
    *,
    caption: Optional[str] = None,
    size: int = 100_000,
    **more: Any,
) -> dict[str, Any]:
    extra: dict[str, Any] = {
        **more,
        "photo": [
            {"file_id": file_id + "-small", "file_unique_id": "s", "width": 90, "height": 60, "file_size": 1500},
            {"file_id": file_id, "file_unique_id": "l", "width": 1280, "height": 960, "file_size": size},
        ]
    }
    if caption is not None:
        extra["caption"] = caption
    return private_message(update_id, account, **extra)


# --- exact interleavings -------------------------------------------------------------------


async def until(predicate) -> None:
    """Let the event loop run until `predicate()` holds. The condition, not a delay, is what
    a test waits for (TIMEOUT only turns a hang into a failure)."""

    async def spin() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(spin(), TIMEOUT)


def in_line(t: "TelegramHarness", account: int, users: int, table: str = "_chat_locks"):
    """A predicate: `users` things hold or wait for the lock of the chat of `account`. The count
    is taken in the same step that asks for the lock, so it is exact: a test that holds the
    lock itself (`async with t.service._chat_lock(account)`) and sees 2 knows that something
    else is really waiting behind it. `table` is "_binding_locks" for the second lock."""

    def reached() -> bool:
        entry = getattr(t.service, table).get(account)
        return entry is not None and entry[1] >= users

    return reached


# --- the harness ----------------------------------------------------------------------------


@dataclass
class TelegramHarness:
    chat: ChatHarness
    bot: FakeBot
    counter: int = 1000
    mirror_errors: list[str] = field(default_factory=list)

    @property
    def service(self):
        return self.chat.app.state.telegram

    @property
    def user_id(self) -> UUID:
        return self.chat.user_id

    def next_id(self) -> int:
        self.counter += 1
        return self.counter

    async def deliver(
        self, account: int, text: Optional[str] = None, *, tick: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        """Give the bot one private message and wait for it to be handled completely. The
        clock moves a second first (tick=False: not), so that the activity times of
        successive messages differ, as they do in life, and "the latest conversation" has
        one answer."""
        if tick:
            self.chat.clock.advance(seconds=1)
        update = private_message(self.next_id(), account, text, **kwargs)
        await self.service.handle_update(update)
        return update

    async def deliver_update(self, update: dict[str, Any]) -> None:
        await self.service.handle_update(update)

    async def pair(self, account: int = MOTHER, *, client: Optional[httpx.AsyncClient] = None) -> str:
        """The honest pairing: ask the website for a code, present it to the bot from
        `account`. Returns the code."""
        response = await (client or self.chat.client).post("/api/telegram/link", json={}, headers=post_headers())
        assert response.status_code == 201, response.text
        code = response.json()["code"]
        await self.deliver(account, f"/start {code}")
        return code

    async def settle(self) -> None:
        """Wait until the bot has no handler or copy to Telegram in hand, and the turn
        runner nothing running."""
        for _ in range(200):
            tasks = set(self.service._tasks)
            if tasks:
                await asyncio.wait_for(asyncio.wait(tasks), TIMEOUT)
            await self.chat.settle()
            if not self.service._tasks:
                return
        raise AssertionError("the bot never settled")

    # --- the database ----------------------------------------------------------------------

    def links(self) -> list[TelegramLink]:
        return self.chat.rows(TelegramLink)

    def codes(self) -> list[TelegramLinkCode]:
        return self.chat.rows(TelegramLinkCode)

    def conversations(self, user_id: Optional[UUID] = None) -> list[Conversation]:
        where = [Conversation.user_id == (user_id or self.user_id)]
        return self.chat.rows(Conversation, *where, order=Conversation.created_at)

    def stored_texts(self, conversation_id) -> list[tuple[str, str]]:
        return [(m.role, m.content) for m in self.chat.messages(str(conversation_id))]

    def all_messages(self) -> list[Message]:
        return self.chat.rows(Message, order=Message.id)

    def all_turns(self) -> list[TurnRequest]:
        return self.chat.rows(TurnRequest)


@asynccontextmanager
async def telegram_app(
    tmp_path,
    backend: Optional[ScriptedBackend] = None,
    *,
    bot: Optional[FakeBot] = None,
    sign_in_first: bool = True,
    **options: Any,
):
    """The application with Telegram login, the chat and the bot, started; the provisioned
    user signed in on the website unless sign_in_first=False."""
    bot = bot or FakeBot()
    backend = backend if backend is not None else ScriptedBackend()
    async with auth_app(
        tmp_path,
        chat_backend=backend,
        bot_settings=bot_settings(),
        bot_transport=bot.transport(),
        **options,
    ) as base:
        if sign_in_first:
            response = await sign_in(base.client, base.telegram)
            assert response.status_code == 303, response.text
        yield TelegramHarness(ChatHarness(base, backend), bot)
