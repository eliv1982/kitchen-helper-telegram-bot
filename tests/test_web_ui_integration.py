"""The page's logic against the real application, over real HTTP.

The application runs for real, on a loopback port (uvicorn in a thread of this process), on a
real migrated SQLite file, with a fake assistant and nothing else faked: no ASGI shortcut, no
scripted answers. Node.js runs the very script the page loads (chat-logic.js) against it, with a
small fetch wrapper for the browser's part (the session cookie, the Origin a browser puts on a
POST, Sec-Fetch-Site) and, where a journey says so, a lost request or a lost answer.

What this adds to tests/test_web_ui_logic.py, which scripts the server: the real answers (their
shapes, statuses, Retry-After, keys and correlation ids) are what the script is made to understand,
and a message that is sent twice because the network lost something is answered, stored and given
to the assistant once. After each journey the database and the assistant's calls are counted here.

Needs Node.js; skipped without it. It is NOT a browser: nothing here draws a page.
"""

import asyncio
import json
import shutil
import socket
import subprocess
import threading
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import sqlalchemy as sa
import uvicorn
from sqlalchemy.exc import SQLAlchemyError

from house_helper.core.backend import BackendConnectionError, BackendReply
from house_helper.persistence import authn
from house_helper.persistence.base import utcnow
from house_helper.persistence.database import create_db_engine, create_session_factory, upgrade_database
from house_helper.persistence.models import Conversation, Message
from house_helper.persistence.models import Session as LoginSession
from house_helper.web.app import create_app
from house_helper.web.settings import WebSettings
from tests.auth_support import auth_settings, seed_user

NODE = shutil.which("node")
JOURNEYS = Path(__file__).parent / "js" / "integration_journeys.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


class LiveBackend:
    """The assistant of the live server: instant, except for a message that starts with "slow"
    (most of a second), and a message that starts with "break once" (fails the first time) or
    "break always" (fails every time). Everything it was asked is kept, to be counted."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self._failed_once: set[str] = set()
        self._lock = threading.Lock()

    async def complete(self, messages, *, conversation, context_len):
        text = messages[-1]["content"]
        with self._lock:
            self.asked.append(text)
            first_time = text not in self._failed_once
            if text.startswith("break once"):
                self._failed_once.add(text)
        if text.startswith("slow"):
            await asyncio.sleep(0.8)
        if text.startswith("break always") or (text.startswith("break once") and first_time):
            raise BackendConnectionError("the assistant is down")
        return BackendReply(text="Ответ на: " + text)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LiveServer:
    def __init__(self, directory: Path) -> None:
        self.port = _free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        url = f"sqlite:///{directory / 'live.db'}"
        self.engine = create_db_engine(url)
        upgrade_database(self.engine)
        self.factory = create_session_factory(self.engine)
        self.backend = LiveBackend()
        self.lost_acknowledgements: set[str] = set()
        app = create_app(
            WebSettings(database_url=url, auth=auth_settings(self.origin)),
            chat_backend=self.backend,
        )
        self.app = app
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False, lifespan="on"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.users: dict[str, object] = {}

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            assert self.thread.is_alive() and time.monotonic() < deadline, "the server did not start"
            time.sleep(0.05)
        self._lose_acknowledgements()

    def _lose_acknowledgements(self) -> None:
        """A fault of the live server, in the real route: for a message that starts with "ack lost",
        the turn is run and COMPLETED (stored, by the real runner), and then, the first time that key
        is seen, the route fails the way a lost commit acknowledgement does (a SQLAlchemyError, which
        ChatRoute answers 503 "service_unavailable" with a correlation id). The same key again is
        answered by the runner's own replay (200)."""
        runner = self.app.state.chat
        submit = runner.submit

        async def submit_then_lose_the_acknowledgement(**kwargs):
            result = await submit(**kwargs)
            key = kwargs["idempotency_key"]
            if kwargs["text"].startswith("ack lost") and key not in self.lost_acknowledgements:
                self.lost_acknowledgements.add(key)
                raise SQLAlchemyError("the acknowledgement of the commit was lost")
            return result

        runner.submit = submit_then_lose_the_acknowledgement

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(20)
        self.engine.dispose()

    def sign_in_new_user(self, name: str) -> str:
        """A person of their own, signed in: the Cookie header their browser would send."""
        subject = f"{9_000_000_000_000_000_000 + len(self.users)}"  # a distinct 19-digit "sub" per person
        user_id = seed_user(self.factory, subject=subject, display_name=name)
        with self.factory() as session, session.begin():
            issued = authn.complete_login(
                session, "telegram", subject, now=utcnow(), lifetime=timedelta(hours=1)
            )
        self.users[name] = user_id
        return f"hh_session={issued.token}"

    def revocations(self, name: str) -> list:
        """When each session of this person was revoked (None: still open), read from the database."""
        with self.factory() as session:
            return list(
                session.scalars(
                    sa.select(LoginSession.revoked_at).where(LoginSession.user_id == self.users[name])
                )
            )

    def counts(self, name: str) -> tuple[int, int]:
        """(user messages, assistant messages) stored for this person."""
        with self.factory() as session:
            rows = session.execute(
                sa.select(Message.role, sa.func.count())
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Conversation.user_id == self.users[name])
                .group_by(Message.role)
            ).all()
        by_role = dict(rows)
        return by_role.get("user", 0), by_role.get("assistant", 0)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    server = LiveServer(tmp_path_factory.mktemp("live"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _names() -> list[str]:
    if NODE is None:
        return []
    completed = subprocess.run(
        [NODE, str(JOURNEYS), "--list"], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)["names"]


NAMES = _names()

# What the assistant must have been asked, and what must be stored, when a journey is over: a
# message that the network made the page send twice is asked once and stored once.
EXPECTED = {
    "first_conversation": (Counter({"Как варить борщ?": 1}), (1, 1)),
    "reload_restores_the_history_and_the_list": (Counter({"Первый вопрос": 1, "Второй вопрос": 1}), (2, 2)),
    "paging_through_a_long_history": (Counter({f"вопрос {i}": 1 for i in range(1, 31)}), (30, 30)),
    "a_lost_answer_is_seen_in_the_history_and_confirmed_by_the_same_key_without_a_second_run": (
        Counter({"потерянный ответ": 1}), (1, 1)
    ),
    "a_lost_request_is_retried_with_the_same_key_and_runs_once": (Counter({"потерянный запрос": 1}), (1, 1)),
    "a_replay_after_an_unseen_completion_is_one_answer_not_two": (Counter({"нет связи после ответа": 1}), (1, 1)),
    "a_busy_conversation_waits_and_then_sends_the_same_key": (
        Counter({"начало": 1, "slow первый": 1, "второй вопрос": 1}), (3, 3)
    ),
    "a_provider_failure_is_known_only_through_the_history_and_the_same_key_tries_again": (
        Counter({"break once пожалуйста": 2}), (1, 1)
    ),
    "a_503_after_a_committed_completion_is_one_turn_whatever_is_tried": (Counter({"ack lost вопрос": 1}), (1, 1)),
    "a_message_passed_over_can_only_be_sent_again_as_a_new_one": (
        Counter({"break always первый": 2, "дальше без сбоя": 1}), (3, 1)
    ),
    "the_limit_is_in_code_points_end_to_end": (Counter({"😀" * 2000: 1}), (1, 1)),
    "the_session_ending_is_noticed_and_the_unsent_message_is_kept_for_next_time": (Counter({"пока вхожу": 1}), (1, 1)),
    "a_lost_answer_to_sign_out_cannot_end_the_session_of_whoever_signed_in_since": (Counter({"секрет Анны": 1}), (1, 1)),
    "a_sign_out_whose_answer_was_lost_is_repeated_by_the_same_person_and_closes_nothing_twice": (
        Counter({"пока выхожу": 1}), (1, 1)
    ),
    "a_sign_out_whose_request_was_lost_is_repeated_and_ends_the_session": (Counter({"пока не вышла": 1}), (1, 1)),
    "a_long_first_message_is_named_the_same_by_the_page_and_by_the_server": (
        Counter({"Подскажите,   пожалуйста,\nкак приготовить домашнюю лапшу 🍜 для куриного супа без яиц и без муки высшего сорта": 1}),
        (1, 1),
    ),
    "favorites_are_kept_reopened_after_a_reload_and_removed": (Counter({"Как варить борщ?": 1}), (1, 1)),
    "favorites_belong_to_the_person_who_kept_them": (Counter({"секрет рецепт Анны": 1}), (1, 1)),
}

# The database after a journey, for the ones about signing out: whose session is revoked and whose is not.
SECOND = " (second)"  # the name of the other person a journey is given, signed in as well


def _after_cross_principal_retry(live, name):
    assert all(when is not None for when in live.revocations(name)), "her session was ended by her own sign-out"
    assert live.revocations(name + SECOND) == [None], "his session was not touched by her repeat"


def _after_same_person_repeat(live, name):
    assert len(live.revocations(name)) == 1 and live.revocations(name)[0] is not None
    assert live.revocations(name + SECOND) == [None]


AFTER = {
    "a_lost_answer_to_sign_out_cannot_end_the_session_of_whoever_signed_in_since": _after_cross_principal_retry,
    "a_sign_out_whose_answer_was_lost_is_repeated_by_the_same_person_and_closes_nothing_twice": _after_same_person_repeat,
    "a_sign_out_whose_request_was_lost_is_repeated_and_ends_the_session": _after_same_person_repeat,
}


def test_every_journey_has_its_expectations():
    assert len(NAMES) >= 15
    assert set(NAMES) == set(EXPECTED)
    assert set(AFTER) <= set(EXPECTED)


@pytest.mark.parametrize("name", NAMES)
def test_journey(live, name):
    cookie = live.sign_in_new_user(name)
    other = live.sign_in_new_user(name + SECOND)  # (a second person, for the journeys that have one sign in too)
    asked_before = len(live.backend.asked)

    completed = subprocess.run(
        [NODE, str(JOURNEYS), name, live.origin, cookie, other],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["ok"], result.get("error")
    asked, stored = EXPECTED[name]
    assert Counter(live.backend.asked[asked_before:]) == asked
    assert live.counts(name) == stored
    if name in AFTER:
        AFTER[name](live, name)
    else:
        assert live.revocations(name + SECOND) == [None]  # nothing a journey does to one person touches the other's session


# --- the real server, over a real socket: what a browser would be sent -----------------------------------


def _get(live, path: str, headers=None):
    request = Request(live.origin + path, headers=headers or {})
    with urlopen(request, timeout=20) as response:  # a loopback address: the network guard lets it pass
        return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()


def test_the_shell_and_the_static_files_over_real_http(live):
    status, headers, body = _get(live, "/")
    assert status == 200 and headers["content-type"] == "text/html; charset=utf-8"
    assert "script-src 'self'" in headers["content-security-policy"]
    assert b'data-telegram="1"' in body and b'data-email="0"' in body

    for name, media in (("app.js", "text/javascript; charset=utf-8"), ("chat-logic.js", "text/javascript; charset=utf-8"), ("app.css", "text/css; charset=utf-8")):
        status, headers, content = _get(live, f"/static/{name}")
        assert status == 200 and headers["content-type"] == media and headers["x-content-type-options"] == "nosniff"
        assert content == (Path(__file__).parent.parent / "house_helper" / "web" / "static" / name).read_bytes()
        assert not any(h.startswith("access-control-") for h in headers)


def test_a_browser_style_post_from_this_origin_works_and_one_from_another_is_refused(live):
    cookie = live.sign_in_new_user("origin check")

    def post(origin):
        request = Request(
            live.origin + "/api/conversations",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "Cookie": cookie, "Origin": origin},
        )
        for _ in range(5):
            try:
                with urlopen(request, timeout=20) as response:
                    return response.status
            except HTTPError as error:  # urllib raises for 4xx
                return error.code
            except ConnectionResetError:
                # The answer to a refused request is sent before its body is read; on some systems
                # the reset of the connection then overtakes it. The status is still what is asserted.
                continue
        raise AssertionError("the server never answered")

    assert post(live.origin) == 201
    assert post("https://evil.example.test") == 403
    assert post("null") == 403  # an Origin of "null" is refused, which is why the page never changes the fetch mode
