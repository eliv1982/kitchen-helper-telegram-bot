"""Shared helpers for the web chat tests.

Nothing leaves the process and no real provider is ever called: the assistant is a
ScriptedBackend (a ChatBackend that answers from a script and can be held at a gate
the test controls), the application is the real one on a real migrated SQLite file,
and requests go through httpx's ASGI transport. Concurrency is made deterministic with
events, never with sleeps.
"""

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import httpx
import sqlalchemy as sa

from house_helper.core.backend import BackendReply
from house_helper.core.identity import ConversationKey
from house_helper.persistence import turns
from house_helper.persistence.conversations import create_conversation
from house_helper.persistence.database import create_session_factory
from house_helper.persistence.models import Conversation, Message, TurnRequest
from house_helper.web.database import WebDatabase
from house_helper.web.turn_runner import WebTurnRunner
from tests.auth_support import ORIGIN, START, FakeClock, Harness, auth_app, seed_user, sign_in
from tests.web_support import TIMEOUT, opened_database


class ScriptedBackend:
    """An offline ChatBackend.

    `script` is a list of replies, consumed in order; an item that is an exception is
    raised instead of returned, a callable is called with the messages. When the script
    is empty, `default` answers. `requests` holds a snapshot of every call as
    (messages, conversation, context_len).

    hold() makes the next call wait at a gate: it returns (arrived, release); `arrived`
    is set when a call reaches the backend, and the call goes on once `release` is set.
    """

    def __init__(self, *script: Any, default: str = "Вот что можно приготовить.") -> None:
        self.script = list(script)
        self.default = default
        self.requests: list[tuple[list[dict[str, str]], ConversationKey, int]] = []
        self.cancelled = 0
        self._gate: Optional[tuple[asyncio.Event, asyncio.Event]] = None

    def hold(self) -> tuple[asyncio.Event, asyncio.Event]:
        self._gate = (asyncio.Event(), asyncio.Event())
        return self._gate

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def complete(self, messages, *, conversation, context_len) -> BackendReply:
        self.requests.append(([dict(item) for item in messages], conversation, context_len))
        # The call takes its place in the script on arrival, not on release, so that
        # the order of the replies is the order of the calls however the gate is used.
        item = self.script.pop(0) if self.script else self.default
        if self._gate is not None:
            arrived, release = self._gate
            arrived.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(messages)
        if isinstance(item, BackendReply):
            return item
        return BackendReply(text=item)


def post_headers(key: Optional[str] = None, **extra: str) -> dict[str, str]:
    """What a browser sends with a POST from the application's own page."""
    headers = {"Origin": ORIGIN, **extra}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@dataclass
class ChatHarness:
    base: Harness
    backend: Optional[ScriptedBackend]
    extra_users: dict[str, UUID] = field(default_factory=dict)

    @property
    def client(self) -> httpx.AsyncClient:
        return self.base.client

    @property
    def app(self):
        return self.base.app

    @property
    def clock(self):
        return self.base.clock

    @property
    def user_id(self) -> UUID:
        return self.base.user_id

    # --- requests, as the page would make them -------------------------------------------

    async def create(self, client: Optional[httpx.AsyncClient] = None) -> str:
        response = await (client or self.client).post(
            "/api/conversations", json={}, headers=post_headers()
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def send(
        self,
        conversation_id: str,
        text: str,
        key: str,
        client: Optional[httpx.AsyncClient] = None,
        **extra_headers: str,
    ) -> httpx.Response:
        return await (client or self.client).post(
            f"/api/conversations/{conversation_id}/messages",
            json={"text": text},
            headers=post_headers(key, **extra_headers),
        )

    async def history(
        self, conversation_id: str, client: Optional[httpx.AsyncClient] = None, **query: Any
    ) -> httpx.Response:
        return await (client or self.client).get(
            f"/api/conversations/{conversation_id}/messages", params=query
        )

    async def second_user(self, subject: str = "7777777777777777777") -> httpx.AsyncClient:
        """Another provisioned person, signed in from their own browser."""
        self.extra_users[subject] = seed_user(
            self.base.factory, subject=subject, display_name="Другой человек"
        )
        client = self.base.new_client()
        response = await sign_in(client, self.base.telegram, subject)
        assert response.status_code == 303, response.text
        return client

    async def sign_in_again(self) -> None:
        """A fresh session for the provisioned user (after the clock was moved far enough
        for the old one to expire)."""
        response = await sign_in(self.base.client, self.base.telegram)
        assert response.status_code == 303, response.text

    async def settle(self) -> None:
        """Wait until the turn runner has nothing running (a disconnected request's
        turn included)."""
        runner = self.app.state.chat
        if runner is not None:
            await asyncio.wait_for(runner.drain(), TIMEOUT)

    # --- the database, read by an independent session ------------------------------------

    def rows(self, model: Any, *where: Any, order: Any = None) -> list[Any]:
        with self.base.factory() as db:
            query = sa.select(model).where(*where)
            if order is not None:
                query = query.order_by(order)
            return list(db.scalars(query))

    def turns(self, conversation_id: str) -> list[TurnRequest]:
        return self.rows(
            TurnRequest,
            TurnRequest.conversation_id == UUID(conversation_id),
            order=TurnRequest.user_message_id,
        )

    def messages(self, conversation_id: str) -> list[Message]:
        return self.rows(
            Message, Message.conversation_id == UUID(conversation_id), order=Message.id
        )

    def conversation(self, conversation_id: str) -> Conversation:
        return self.rows(Conversation, Conversation.id == UUID(conversation_id))[0]


@asynccontextmanager
async def chat_app(
    tmp_path,
    backend: Optional[ScriptedBackend] = None,
    *,
    sign_in_first: bool = True,
    **options: Any,
):
    """The application with Telegram login and (if `backend` is given) the chat, started;
    the provisioned user already signed in unless sign_in_first=False."""
    async with auth_app(tmp_path, chat_backend=backend, **options) as base:
        if sign_in_first:
            response = await sign_in(base.client, base.telegram)
            assert response.status_code == 303, response.text
        yield ChatHarness(base, backend)


def error_of(response: httpx.Response) -> dict[str, Any]:
    """The body of an error response, after checking it has the whole shape."""
    body = response.json()
    assert set(body) >= {"detail", "message", "retryable", "correlation_id"}, body
    assert body["correlation_id"] == response.headers["x-correlation-id"]
    return body


def log_text(caplog) -> str:
    """Everything logged at any level, tracebacks included, as one string."""
    import logging

    formatter = logging.Formatter()
    lines = []
    for record in caplog.records:
        if record.name == "httpx":
            continue  # the test client's own request lines
        lines.append(f"{record.name} {record.levelname}: {record.getMessage()}")
        if record.exc_info:
            lines.append(formatter.formatException(record.exc_info))
    return "\n".join(lines)


def advance(harness: ChatHarness, **delta: float) -> datetime:
    harness.clock.advance(**delta)
    return harness.clock.current


# --- the turn runner on its own ---------------------------------------------------------


@dataclass
class RunnerEnv:
    runner: WebTurnRunner
    backend: ScriptedBackend
    clock: FakeClock
    factory: object
    user_id: UUID
    conversation_id: UUID
    database: WebDatabase

    async def submit(self, text="Что из яиц?", key="k-1", runner=None):
        return await (runner or self.runner).submit(
            user_id=self.user_id,
            conversation_id=self.conversation_id,
            idempotency_key=key,
            text=text,
            correlation_id="hh-0123456789abcdef",
            route="POST /test",
        )

    def turn(self) -> TurnRequest:
        with self.factory() as db:
            return db.scalars(sa.select(TurnRequest)).one()

    def count(self, model) -> int:
        with self.factory() as db:
            return db.scalar(sa.select(sa.func.count()).select_from(model))


@asynccontextmanager
async def runner_env(tmp_path, backend, *, database_options=None, **options):
    """A WebTurnRunner over a real migrated SQLite file, with a clock of the test's own;
    `options` go to the runner, `database_options` to the WebDatabase."""
    async with opened_database(tmp_path, **(database_options or {})) as (database, engine):
        factory = create_session_factory(engine)
        user_id = seed_user(factory)
        with factory() as db, db.begin():
            conversation = create_conversation(db, user_id, now=START)
        clock = FakeClock()
        runner = WebTurnRunner(database, backend, clock, **options)
        try:
            yield RunnerEnv(runner, backend, clock, factory, user_id, conversation.id, database)
        finally:
            await runner.aclose()


def gate_on(monkeypatch, name: str, *, fail: Optional[BaseException] = None):
    """Make persistence.turns.<name> wait, on its worker thread, before it does anything.

    Returns (entered, proceed), two threading.Events: `entered` is set when the call has
    started on its thread; the call goes on when the test sets `proceed`, and then either
    does its work or, with `fail`, raises that instead (before touching the database, so
    the transaction rolls back). This is how a test holds a database transaction open at
    a boundary while it does something else, with no sleeps.
    """
    entered, proceed = threading.Event(), threading.Event()
    real = getattr(turns, name)

    def gated(*args, **kwargs):
        entered.set()
        assert proceed.wait(TIMEOUT), "the test never let the call go on"
        if fail is not None:
            raise fail
        return real(*args, **kwargs)

    monkeypatch.setattr(turns, name, gated)
    return entered, proceed


async def reached(event: threading.Event) -> None:
    """Wait (without blocking the loop) until a thread has set `event`."""
    await asyncio.wait_for(
        asyncio.get_running_loop().run_in_executor(None, event.wait, TIMEOUT), TIMEOUT
    )


def cancel_turns(runner: WebTurnRunner) -> None:
    """Cancel the turn tasks of the runner, as aclose() does once its grace is over and
    as the event loop does when it is torn down. (Only the tasks of the turns: nothing
    cancels a database unit.)"""
    for task in list(runner._turns):
        task.cancel()
