"""The chat API's contract: who may call it, what it accepts, what it answers.

What happens to a turn once it is accepted (follow-ups, retries, concurrency, failures)
is test_web_chat_turns.py; what the errors and the log say is
test_web_chat_diagnostics.py. Here: authentication, ownership, validation of every
input, the shape of every success, and the headers of all of it. The assistant is
always a ScriptedBackend; nothing reaches a provider.
"""

import asyncio
import json
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from house_helper.persistence import conversations
from house_helper.persistence.models import Conversation, Message, TurnRequest, User
from house_helper.web import chat_api
from tests.auth_support import ORIGIN, logout_as
from tests.chat_support import ScriptedBackend, chat_app, error_of, post_headers, reached
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

CORRELATION_ID = re.compile(r"hh-[0-9a-f]{16}")
JSON = {"Content-Type": "application/json"}

# PP-01-A1-R5: the strict client contract is a JavaScript function; real route answers are
# fed to it by tests/js/crosscheck.js (the same Node harness the Web logic tests use).
NODE = shutil.which("node")
CROSSCHECK = Path(__file__).parent / "js" / "crosscheck.js"


def title_mutation_outcomes(cases):
    request = json.dumps({"task": "title_mutation", "cases": cases})
    completed = subprocess.run(
        [NODE, str(CROSSCHECK)],
        input=request,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)["outcomes"]


@contextmanager
def statements_of(harness):
    """The SQL the application's engine runs inside the block."""
    seen: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        seen.append(statement.lstrip().split(None, 1)[0].upper())

    engine = harness.app.state.database._engine
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)


def count(harness, model) -> int:
    return len(harness.rows(model))


# --- who may ask --------------------------------------------------------------------------

ROUTES = [
    pytest.param("get", "/api/conversations", None, id="list"),
    pytest.param("post", "/api/conversations", {}, id="create"),
    pytest.param("patch", "/api/conversations/{id}", {"title": "Борщ"}, id="rename"),
    pytest.param("delete", "/api/conversations/{id}", None, id="delete"),
    pytest.param("get", "/api/conversations/{id}/messages", None, id="history"),
    pytest.param("post", "/api/conversations/{id}/messages", {"text": "привет"}, id="send"),
]


async def call(harness, method, path, body, *, client=None, conversation_id=None, **headers):
    path = path.format(id=conversation_id or str(uuid4()))
    client = client or harness.client
    if method == "get":
        return await client.get(path, headers=headers)
    if method == "delete":
        return await client.delete(path, headers=headers)
    if method == "patch":
        return await client.patch(path, json=body, headers=post_headers(**headers))
    return await client.post(path, json=body, headers=post_headers("key-1", **headers))


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_every_chat_route_requires_a_session(tmp_path, method, path, body):
    async with chat_app(tmp_path, ScriptedBackend(), sign_in_first=False) as chat:
        response = await call(chat, method, path, body)

        assert response.status_code == 401
        assert error_of(response)["detail"] == "not_authenticated"
        assert count(chat, Conversation) == 0
        assert chat.backend.calls == 0


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_a_forged_or_malformed_session_cookie_is_no_session(tmp_path, method, path, body):
    async with chat_app(tmp_path, ScriptedBackend(), sign_in_first=False) as chat:
        for cookie in ("forged", "A" * 43, "x" * 5000, ""):
            chat.client.cookies.clear()
            chat.client.cookies.set("__Host-hh_session", cookie, domain="kitchen.example.test")
            response = await call(chat, method, path, body)
            assert response.status_code == 401, cookie


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_a_signed_out_session_stops_working(tmp_path, method, path, body):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        out = await chat.client.post("/auth/logout", headers=post_headers(**logout_as(chat.user_id)))
        assert out.status_code == 204

        response = await call(chat, method, path, body, conversation_id=conversation)

        assert response.status_code == 401


async def test_a_disabled_user_is_refused(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        with chat.base.factory() as db, db.begin():
            db.execute(
                sa.update(User).where(User.id == chat.user_id).values(disabled_at=chat.clock())
            )

        assert (await chat.history(conversation)).status_code == 401
        assert (await chat.send(conversation, "привет", "k")).status_code == 401


async def test_the_user_is_the_session_never_something_the_request_says(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        other = await chat.second_user()
        theirs = await chat.create(other)
        stranger = str(chat.extra_users["7777777777777777777"])

        # A user id in the query, a header or the body is not read: it is refused as an
        # unexpected field or ignored, and never lets one person into another's.
        listing = await chat.client.get(
            "/api/conversations", params={"user_id": stranger}, headers={"X-User-Id": stranger}
        )
        created = await chat.client.post(
            "/api/conversations", json={"user_id": stranger}, headers=post_headers()
        )
        sent = await chat.client.post(
            f"/api/conversations/{theirs}/messages",
            json={"text": "привет", "user_id": stranger},
            headers=post_headers("k", **{"X-User-Id": stranger}),
        )
        peeked = await chat.client.get(
            f"/api/conversations/{theirs}/messages", params={"user_id": stranger}
        )

        assert listing.status_code == 200 and listing.json()["conversations"] == []
        assert created.status_code == 400
        assert sent.status_code == 400
        assert peeked.status_code == 404
        assert count(chat, Message) == 0


# --- ownership: one answer for "not yours", "not there" and "not an id" ---------------------


async def test_another_persons_conversation_cannot_be_read_written_or_listed(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        mine = await chat.create()
        assert (await chat.send(mine, "мой секрет", "k-1")).status_code == 201
        other = await chat.second_user()

        read = await chat.history(mine, other)
        write = await chat.send(mine, "чужое сообщение", "k-2", other)
        listing = await other.get("/api/conversations")

        assert read.status_code == write.status_code == 404
        assert error_of(read)["detail"] == error_of(write)["detail"] == "conversation_not_found"
        assert listing.json()["conversations"] == []
        assert backend.calls == 1  # the stranger's message never reached the assistant
        assert [m.content for m in chat.messages(mine)][0] == "мой секрет"
        assert len(chat.messages(mine)) == 2  # nothing was added to it


async def test_a_stranger_gets_the_same_answer_whether_it_is_unknown_foreign_or_malformed(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        mine = await chat.create()
        other = await chat.second_user()
        ids = {
            "foreign": mine,
            "unknown": str(uuid4()),
            "malformed": "not-a-uuid",
            "unhyphenated": uuid4().hex,
            "braced": "{" + str(uuid4()) + "}",
            "urn": "urn:uuid:" + str(uuid4()),
            "too long": "a" * 300,
            "nul": "%00",
            "number": "1",
            "unicode digits": "١٢٣٤٥٦٧٨-١٢٣٤-١٢٣٤-١٢٣٤-١٢٣٤٥٦٧٨٩٠١٢",
        }

        answers = {}
        for name, conversation in ids.items():
            read = await other.get(f"/api/conversations/{conversation}/messages")
            write = await other.post(
                f"/api/conversations/{conversation}/messages",
                json={"text": "привет"},
                headers=post_headers("k"),
            )
            answers[name] = (read.status_code, write.status_code, error_of(read)["message"])

        assert set(answers.values()) == {(404, 404, "Диалог не найден.")}


async def test_a_conversation_id_in_capitals_is_the_same_conversation(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.history(conversation.upper())

        assert response.status_code == 200
        assert response.json()["conversation_id"] == conversation


async def test_each_person_lists_only_their_own_conversations(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        other = await chat.second_user()
        mine = [await chat.create() for _ in range(2)]
        theirs = [await chat.create(other) for _ in range(3)]

        mine_listed = (await chat.client.get("/api/conversations")).json()["conversations"]
        theirs_listed = (await other.get("/api/conversations")).json()["conversations"]

        assert {c["id"] for c in mine_listed} == set(mine)
        assert {c["id"] for c in theirs_listed} == set(theirs)


# --- a request must come from our page --------------------------------------------------------


@pytest.mark.parametrize("signed_in", [True, False], ids=["with-session", "without-session"])
@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "null", "http://kitchen.example.test", ORIGIN + ".evil.example", ""],
)
async def test_a_mutation_from_another_origin_is_refused_with_or_without_a_session(
    tmp_path, origin, signed_in
):
    async with chat_app(tmp_path, ScriptedBackend(), sign_in_first=signed_in) as chat:
        conversation = str(uuid4())
        headers = {"Origin": origin, "Idempotency-Key": "k"}

        created = await chat.client.post("/api/conversations", json={}, headers=headers)
        sent = await chat.client.post(
            f"/api/conversations/{conversation}/messages", json={"text": "привет"}, headers=headers
        )

        assert created.status_code == sent.status_code == 403
        assert error_of(created)["detail"] == "forbidden_origin"
        assert count(chat, Conversation) == 0
        assert chat.backend.calls == 0


async def test_two_origin_headers_are_refused(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        response = await chat.client.post(
            "/api/conversations", json={}, headers=[("Origin", ORIGIN), ("Origin", ORIGIN)]
        )

        assert response.status_code == 403


@pytest.mark.parametrize(
    ("site", "allowed"),
    [("same-origin", True), ("none", True), ("same-site", False), ("cross-site", False)],
)
async def test_without_an_origin_the_fetch_metadata_decides(tmp_path, site, allowed):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        response = await chat.client.post(
            "/api/conversations", json={}, headers={"Sec-Fetch-Site": site}
        )

        assert (response.status_code == 201) is allowed
        if not allowed:
            assert error_of(response)["detail"] == "forbidden_origin"


async def test_a_request_with_no_browser_headers_is_allowed_like_logout(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        response = await chat.client.post("/api/conversations", json={})

        assert response.status_code == 201


async def test_reads_do_not_look_at_the_origin(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        response = await chat.client.get(
            "/api/conversations", headers={"Origin": "https://evil.example"}
        )

        assert response.status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/conversations"),
        ("post", "/api/conversations"),
        ("options", "/api/conversations"),
        ("get", "/api/conversations/{id}/messages"),
        ("options", "/api/conversations/{id}/messages"),
    ],
)
async def test_no_response_carries_a_cors_header(tmp_path, method, path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        headers = {
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        }

        response = await chat.client.request(
            method.upper(), path.format(id=conversation), headers=headers
        )

        assert not [name for name in response.headers if name.lower().startswith("access-control")]
        if method == "options":
            assert response.status_code == 405  # a preflight is not answered: the browser blocks it


# --- the shape of every response -----------------------------------------------------------------


async def test_every_response_is_uncacheable_and_carries_a_server_made_correlation_id(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        responses = [
            await chat.client.get("/api/conversations"),
            await chat.history(conversation),
            await chat.send(conversation, "привет", "k"),
            await chat.send(conversation, "привет", "k"),  # a replay
            await chat.history(str(uuid4())),  # an error
            await chat.send(conversation, "", "k2"),  # another
        ]

        seen = set()
        for response in responses:
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert CORRELATION_ID.fullmatch(response.headers["x-correlation-id"])
            seen.add(response.headers["x-correlation-id"])
        assert len(seen) == len(responses)  # a new one for every request


async def test_a_correlation_id_sent_by_the_client_is_never_used(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        forged = "hh-0000000000000000"

        ok = await chat.client.get("/api/conversations", headers={"X-Correlation-ID": forged})
        bad = await chat.client.get(
            f"/api/conversations/{uuid4()}/messages", headers={"X-Correlation-ID": forged}
        )

        assert ok.headers["x-correlation-id"] != forged
        assert bad.headers["x-correlation-id"] != forged
        assert error_of(bad)["correlation_id"] != forged


async def test_creating_a_conversation(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        response = await chat.client.post("/api/conversations", json={}, headers=post_headers())

        body = response.json()
        assert response.status_code == 201
        assert set(body) == {
            "id",
            "title",
            "title_origin",
            "can_reset_title",
            "created_at",
            "updated_at",
            "last_message_at",
        }
        assert body["title"] is None  # nothing the user said is kept outside the 365 days
        assert body["title_origin"] == "none" and body["can_reset_title"] is False
        assert body["created_at"] == body["updated_at"] == chat.clock().isoformat()
        assert body["last_message_at"] is None
        stored = chat.conversation(body["id"])
        assert stored.user_id == chat.user_id  # the session's user, not a parameter
        assert stored.title is None


async def test_conversations_are_listed_by_latest_activity(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        first = await chat.create()
        chat.clock.advance(minutes=1)
        second = await chat.create()
        chat.clock.advance(minutes=1)
        third = await chat.create()

        listed = (await chat.client.get("/api/conversations")).json()
        assert [c["id"] for c in listed["conversations"]] == [third, second, first]

        chat.clock.advance(minutes=1)
        assert (await chat.send(first, "борщ", "k")).status_code == 201

        listed = (await chat.client.get("/api/conversations")).json()
        assert [c["id"] for c in listed["conversations"]] == [first, third, second]
        assert listed["conversations"][0]["last_message_at"] == chat.clock().isoformat()


# --- PP-01-A1: the conversation title and its two dates ------------------------------------


def title_headers():
    return {**JSON, "Origin": ORIGIN}


async def test_renaming_a_conversation_persists_and_is_the_same_title_telegram_reads(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend("Борщ.")) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "Как сварить борщ?", "k-1")
        before = chat.conversation(conversation)

        response = await chat.client.patch(
            f"/api/conversations/{conversation}",
            json={"title": "  Мой борщ  "},
            headers=title_headers(),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Мой борщ"  # trimmed, stored, not truncated
        assert body["title_origin"] == "manual" and body["can_reset_title"] is True
        assert body["created_at"] == before.created_at.isoformat()
        assert body["updated_at"] == before.updated_at.isoformat()  # not conversation activity
        assert body["last_message_at"] is not None
        stored = chat.conversation(conversation)
        assert stored.title == "Мой борщ"
        assert stored.updated_at == before.updated_at  # the row itself did not move
        listed = (await chat.client.get("/api/conversations")).json()["conversations"]
        assert listed[0]["title"] == "Мой борщ" and listed[0]["title_origin"] == "manual"


async def test_reset_restores_the_derived_title_and_is_safe_to_repeat(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "Как сварить борщ?", "k-1")

        renamed = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Своё"}, headers=title_headers()
        )
        assert renamed.json()["title_origin"] == "manual"

        reset = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": None}, headers=title_headers()
        )
        assert reset.status_code == 200
        assert reset.json()["title"] == "Как сварить борщ?"  # derived from the first message
        assert reset.json()["title_origin"] == "derived" and reset.json()["can_reset_title"] is False
        assert chat.conversation(conversation).title is None

        again = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": None}, headers=title_headers()
        )
        assert again.status_code == 200 and again.json()["title"] == "Как сварить борщ?"
        assert again.json()["can_reset_title"] is False


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
async def test_the_real_title_patch_shape_is_accepted_by_the_strict_client_contract(tmp_path):
    """PP-01-A1-R5 (OD-2): the client applies a title answer only when it obeys the strict
    summary contract. The real PATCH route's rename and reset answers are fed to that same
    client code (tests/js/crosscheck.js) and are classified as confirmed."""
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "Как сварить борщ?", "k-1")

        renamed = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Мой борщ"}, headers=title_headers()
        )
        reset = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": None}, headers=title_headers()
        )
        assert renamed.status_code == 200 and reset.status_code == 200

        outcomes = title_mutation_outcomes(
            [
                {
                    "status": renamed.status_code,
                    "body": renamed.json(),
                    "request": {"title": "Мой борщ"},
                    "id": conversation,
                },
                {
                    "status": reset.status_code,
                    "body": reset.json(),
                    "request": {"title": None},
                    "id": conversation,
                },
            ]
        )

        assert outcomes == [{"type": "confirmed"}, {"type": "confirmed"}]


@pytest.mark.parametrize(
    ("title", "code"),
    [
        ("", "empty_title"),
        ("   ", "empty_title"),
        ("Борщ\nс грибами", "invalid_title"),
        ("Борщ\tс грибами", "invalid_title"),
        ("a\u0000b", "invalid_title"),
        ("a\u200bb", "invalid_title"),  # an invisible format character, not an emoji joiner
        ("x" * 201, "title_too_long"),
    ],
)
async def test_a_title_that_cannot_be_stored_is_refused_and_nothing_changes(tmp_path, title, code):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        before = chat.conversation(conversation)

        response = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": title}, headers=title_headers()
        )

        assert response.status_code == 400
        assert error_of(response)["detail"] == code
        after = chat.conversation(conversation)
        assert (after.title, after.updated_at) == (before.title, before.updated_at)


async def test_every_kind_of_valid_title_is_kept_exactly(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        titles = [
            "Борщ",
            "Borscht",
            "Борщ 🍲",
            "Семья 👨\u200d👩\u200d👧",
            "Флаг 🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
            "я" * 200,
            "Плов",
            "Плов",  # duplicates are allowed
        ]
        for title in titles:
            response = await chat.client.patch(
                f"/api/conversations/{conversation}", json={"title": title}, headers=title_headers()
            )
            assert response.status_code == 200, response.text
            assert response.json()["title"] == title


@pytest.mark.parametrize("body", [{}, {"title": 5}, {"title": "x", "other": 1}, {"name": "x"}])
async def test_the_rename_body_names_only_one_title_of_the_right_kind(tmp_path, body):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.client.patch(
            f"/api/conversations/{conversation}", json=body, headers=title_headers()
        )

        assert response.status_code == 400
        assert error_of(response)["detail"] == "invalid_request"


async def test_a_rename_cannot_reach_another_account_or_a_conversation_that_is_not_there(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        theirs = await chat.create(await chat.second_user())
        before = chat.conversation(theirs)

        for conversation in (theirs, str(uuid4())):
            response = await chat.client.patch(
                f"/api/conversations/{conversation}", json={"title": "Чужое"}, headers=title_headers()
            )
            assert response.status_code == 404
            assert error_of(response)["detail"] == "conversation_not_found"

        after = chat.conversation(theirs)
        assert (after.title, after.updated_at) == (before.title, before.updated_at)


async def test_a_rename_is_refused_without_the_pages_origin(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.client.patch(
            f"/api/conversations/{conversation}",
            json={"title": "Борщ"},
            headers={**JSON, "Origin": "https://evil.example"},
        )

        assert response.status_code == 403
        assert error_of(response)["detail"] == "forbidden_origin"
        assert chat.conversation(conversation).title is None


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Content-Type": "application/json"}, id="missing"),
        pytest.param(
            {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"},
            id="missing-with-same-origin-fetch-metadata",
        ),
        pytest.param(
            {"Content-Type": "application/json", "Sec-Fetch-Site": "none"},
            id="missing-with-address-bar-fetch-metadata",
        ),
        pytest.param({"Content-Type": "application/json", "Origin": "null"}, id="null"),
        pytest.param(
            {"Content-Type": "application/json", "Origin": ORIGIN + ".evil.example"},
            id="prefix-foreign",
        ),
    ],
)
async def test_a_rename_requires_exactly_one_matching_origin(tmp_path, headers):
    """PP-01-A1's A1-02: the PATCH is refused without the application's own Origin, and
    Fetch Metadata is never accepted in its place (the other mutations keep their rule)."""
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Борщ"}, headers=headers
        )

        assert response.status_code == 403
        assert error_of(response)["detail"] == "forbidden_origin"
        assert chat.conversation(conversation).title is None


@pytest.mark.parametrize(
    "origins",
    [
        pytest.param((ORIGIN, ORIGIN), id="duplicate-matching"),
        pytest.param((ORIGIN, "https://evil.example"), id="conflicting"),
        pytest.param(("https://evil.example", ORIGIN), id="conflicting-reversed"),
    ],
)
async def test_a_rename_refuses_repeated_origin_headers(tmp_path, origins):
    """The header is read with getlist, so repeated values are seen rather than collapsed
    by a single-value accessor: two Origins are not "one matching Origin"."""
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.client.patch(
            f"/api/conversations/{conversation}",
            json={"title": "Борщ"},
            headers=[("Content-Type", "application/json"), *(("Origin", value) for value in origins)],
        )

        assert response.status_code == 403
        assert error_of(response)["detail"] == "forbidden_origin"
        assert chat.conversation(conversation).title is None


async def test_a_rename_with_the_matching_origin_is_accepted(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.client.patch(
            f"/api/conversations/{conversation}",
            json={"title": "Борщ"},
            headers={"Content-Type": "application/json", "Origin": ORIGIN},
        )

        assert response.status_code == 200
        assert response.json()["title"] == "Борщ"
        assert chat.conversation(conversation).title == "Борщ"


async def test_a_rename_neither_reorders_the_list_nor_touches_anything_else(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend("Ответ.")) as chat:
        first = await chat.create()
        chat.clock.advance(minutes=1)
        second = await chat.create()
        await chat.send(first, "Первый", "k-1")
        await chat.send(second, "Второй", "k-2")
        before = [c["id"] for c in (await chat.client.get("/api/conversations")).json()["conversations"]]
        messages_before = (await chat.history(first)).json()["messages"]

        response = await chat.client.patch(
            f"/api/conversations/{first}", json={"title": "Своё"}, headers=title_headers()
        )

        assert response.status_code == 200
        after = [c["id"] for c in (await chat.client.get("/api/conversations")).json()["conversations"]]
        assert after == before  # a rename is not activity
        assert (await chat.history(first)).json()["messages"] == messages_before
        assert count(chat, Message) == 4 and count(chat, TurnRequest) == 2


@contextmanager
def hold_title_mutation(monkeypatch, name):
    """Hold persistence.conversations.<name> before it touches the database.

    The real PATCH handler runs the function on a database worker in its own transaction;
    this makes that transaction start after the test has issued another title write, so
    the audited inversion (an older write committing last) is reproduced through the real
    route and the real persistence path, with no sleeps."""
    entered, proceed = threading.Event(), threading.Event()
    real = getattr(conversations, name)

    def gated(*args, **kwargs):
        entered.set()
        assert proceed.wait(TIMEOUT), "the test never let the call go on"
        return real(*args, **kwargs)

    monkeypatch.setattr(conversations, name, gated)
    try:
        yield entered, proceed
    finally:
        monkeypatch.setattr(conversations, name, real)


async def test_an_overlapping_reset_can_commit_after_a_newer_rename_through_the_real_route(
    tmp_path, monkeypatch
):
    """A1-01 (A1-01-R2): the audited commit inversion, on the real handler and database.

    A reset is held before its transaction starts; a newer rename is accepted and
    committed; the reset then runs and is the last write, clearing the title. This is
    persisted state, not response ordering, and it is exactly what the page's serialized
    title queue exists to prevent: the page does not send the rename until the reset's
    definitive answer. The test is engine-agnostic and runs on SQLite and PostgreSQL."""
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        first = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Борщ"}, headers=title_headers()
        )
        assert first.status_code == 200

        with hold_title_mutation(monkeypatch, "reset_conversation_title") as (entered, proceed):
            resetting = asyncio.create_task(
                chat.client.patch(
                    f"/api/conversations/{conversation}",
                    json={"title": None},
                    headers=title_headers(),
                )
            )
            await reached(entered)
            renamed = await chat.client.patch(
                f"/api/conversations/{conversation}",
                json={"title": "Новое имя"},
                headers=title_headers(),
            )
            assert renamed.status_code == 200
            assert chat.conversation(conversation).title == "Новое имя"

            proceed.set()
            reset = await resetting
            assert reset.status_code == 200

        # The older reset committed last: the persisted title is cleared.
        assert chat.conversation(conversation).title is None


async def test_the_pages_order_reset_then_rename_leaves_the_rename_as_the_persisted_title(
    tmp_path,
):
    """A1-01 (A1-01-R2): when each write's definitive answer is known before the next is
    sent -- what the page's queue enforces -- the later rename is the persisted title,
    whatever the reset answered. The reverse order resets the title."""
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        renamed = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Своё"}, headers=title_headers()
        )
        assert renamed.status_code == 200
        reset = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": None}, headers=title_headers()
        )
        assert reset.status_code == 200 and reset.json()["title"] is None
        assert chat.conversation(conversation).title is None

        again = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Позже"}, headers=title_headers()
        )
        assert again.status_code == 200
        assert chat.conversation(conversation).title == "Позже"


async def test_a_failure_while_building_the_answer_comes_after_the_commit_and_the_title_is_stored(
    tmp_path, monkeypatch
):
    """A1-01c (A1-01-R3): the PATCH handler commits the title before it builds its
    JSONResponse. An exception while formatting that answer becomes the application's own
    500 ``internal_error`` -- but the title has already been stored. A client must treat
    any 5xx as an indeterminate outcome, never as proof that the transaction rolled back.
    The persisted title is checked independently of the HTTP result; the test is
    engine-agnostic (SQLite and PostgreSQL)."""
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        first = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Старое"}, headers=title_headers()
        )
        assert first.status_code == 200

        formatted = []

        def lose_the_answer(summary):
            # Called after database.run returned: its session block has committed.
            formatted.append(summary.title)
            raise RuntimeError("the answer could not be built")

        monkeypatch.setattr(chat_api, "_conversation_json", lose_the_answer)
        response = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Новое имя"}, headers=title_headers()
        )

        assert response.status_code == 500
        assert error_of(response)["detail"] == "internal_error"
        assert formatted == ["Новое имя"], "the answer was built from the committed summary"
        # The HTTP result is a failure; the database says the write was stored.
        assert chat.conversation(conversation).title == "Новое имя"


async def test_a_database_failure_before_the_commit_is_a_definitive_refusal(
    tmp_path, monkeypatch
):
    """A1-01c (A1-01-R3): a failure raised by the mutation itself, before the session block
    commits, rolls back and is answered by the application as a 503. Unlike a 5xx written
    after the commit, this one proves the title was not stored; the page may continue after
    it. The two cases are indistinguishable from a 5xx body alone, which is why the page
    treats every 5xx as indeterminate."""
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        first = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Старое"}, headers=title_headers()
        )
        assert first.status_code == 200

        def refuse(*args, **kwargs):
            raise sa.exc.OperationalError(
                "UPDATE conversations", {}, Exception("connection lost")
            )

        monkeypatch.setattr(conversations, "set_conversation_title", refuse)
        response = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Новое имя"}, headers=title_headers()
        )

        assert response.status_code == 503
        assert error_of(response)["detail"] == "service_unavailable"
        assert chat.conversation(conversation).title == "Старое"


# The page's closed set of definitive title-PATCH refusals (chat-logic.js
# TITLE_REFUSAL_STATUSES). Each case is issued through the real route; the stored title and
# its updated_at prove that the refusal was made before any write. A 4xx outside this set is
# not needed to prove the negative -- the page treats it as indeterminate -- but every code
# here must keep answering exactly the status the page's table expects.
DEFINITIVE_TITLE_REFUSALS = [
    pytest.param("json", {"title": ""}, title_headers(), 400, "empty_title", id="empty"),
    pytest.param("json", {"title": "x" * 201}, title_headers(), 400, "title_too_long", id="too-long"),
    pytest.param("json", {"title": "a\nb"}, title_headers(), 400, "invalid_title", id="characters"),
    pytest.param("json", {"other": 1}, title_headers(), 400, "invalid_request", id="shape"),
    pytest.param(
        "content",
        b'{"title": "x"}',
        {"Origin": ORIGIN, "Content-Type": "text/plain"},
        415,
        "unsupported_media_type",
        id="media-type",
    ),
    pytest.param(
        "json", {"title": "x"}, {"Content-Type": "application/json"}, 403, "forbidden_origin", id="origin"
    ),
    pytest.param(
        "content",
        b'{"title": "' + b"x" * (40 * 1024) + b'"}',
        title_headers(),
        413,
        "request_too_large",
        id="too-large",
    ),
]


@pytest.mark.parametrize(("kind", "body", "headers", "status", "code"), DEFINITIVE_TITLE_REFUSALS)
async def test_every_definitive_title_refusal_is_answered_before_any_write(
    tmp_path, kind, body, headers, status, code
):
    """PP-01-A1-R4 (R4-03): the client may release a conversation's title queue only after
    one of these answers. The real route proves each is raised before the transaction
    touches the row: the stored title and updated_at are exactly what they were."""
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        stored = await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Старое"}, headers=title_headers()
        )
        assert stored.status_code == 200
        before = chat.conversation(conversation)

        if kind == "json":
            response = await chat.client.patch(
                f"/api/conversations/{conversation}", json=body, headers=headers
            )
        else:
            response = await chat.client.patch(
                f"/api/conversations/{conversation}", content=body, headers=headers
            )

        assert response.status_code == status
        assert error_of(response)["detail"] == code
        after = chat.conversation(conversation)
        assert (after.title, after.updated_at) == (before.title, before.updated_at)


async def test_a_conversation_not_found_refusal_is_also_before_any_write(tmp_path):
    """PP-01-A1-R4 (R4-03): the other definitive answer, the application's own 404. The
    client treats it as "gone" (the conversation is removed) rather than retrying it."""
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        response = await chat.client.patch(
            f"/api/conversations/{uuid4()}", json={"title": "Борщ"}, headers=title_headers()
        )

        assert response.status_code == 404
        assert error_of(response)["detail"] == "conversation_not_found"


async def test_deleting_the_conversation_deletes_its_stored_title_with_it(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        await chat.client.patch(
            f"/api/conversations/{conversation}", json={"title": "Своё"}, headers=title_headers()
        )

        assert (await chat.client.delete(f"/api/conversations/{conversation}", headers=title_headers())).status_code == 204

        assert chat.rows(Conversation, Conversation.id == UUID(conversation)) == []


async def test_the_list_is_bounded_and_says_when_there_is_more(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        for _ in range(5):
            await chat.create()
            chat.clock.advance(seconds=1)

        two = (await chat.client.get("/api/conversations", params={"limit": 2})).json()
        everything = (await chat.client.get("/api/conversations", params={"limit": 100})).json()

        assert len(two["conversations"]) == 2 and two["has_more"] is True
        assert len(everything["conversations"]) == 5 and everything["has_more"] is False
        assert two["conversations"] == everything["conversations"][:2]


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", "1.5", "", "٣", " 5", "5 ", "1e1"])
async def test_a_bad_limit_is_refused_without_being_quoted(tmp_path, limit):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        for path in ("/api/conversations", f"/api/conversations/{conversation}/messages"):
            response = await chat.client.get(path, params={"limit": limit})

            assert response.status_code == 400, (path, limit)
            assert error_of(response)["detail"] == "invalid_query"


@pytest.mark.parametrize("query", ["limit=1&limit=2", "before=1&before=2", "before=0", "before=-5", "before=x"])
async def test_repeated_or_bad_query_parameters_are_refused(tmp_path, query):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.client.get(f"/api/conversations/{conversation}/messages?{query}")

        assert response.status_code == 400
        assert error_of(response)["detail"] == "invalid_query"


# --- the history ---------------------------------------------------------------------------------


async def test_the_history_of_an_empty_conversation(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        body = (await chat.history(conversation)).json()

        assert body == {
            "conversation_id": conversation,
            "messages": [],
            "has_more": False,
            "next_before": None,
        }


async def test_the_history_is_oldest_first_and_pages_backwards(tmp_path):
    backend = ScriptedBackend(default="ответ")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        for index in range(4):  # 8 messages
            assert (await chat.send(conversation, f"вопрос {index}", f"k-{index}")).status_code == 201

        everything = (await chat.history(conversation)).json()
        newest = (await chat.history(conversation, limit=3)).json()
        older = (await chat.history(conversation, limit=3, before=newest["next_before"])).json()
        oldest = (await chat.history(conversation, limit=3, before=older["next_before"])).json()

        ids = [m["id"] for m in everything["messages"]]
        assert ids == sorted(ids) and len(ids) == 8
        assert [m["role"] for m in everything["messages"]] == ["user", "assistant"] * 4
        assert [m["id"] for m in newest["messages"]] == ids[5:] and newest["has_more"] is True
        assert [m["id"] for m in older["messages"]] == ids[2:5] and older["has_more"] is True
        assert [m["id"] for m in oldest["messages"]] == ids[:2]
        assert oldest["has_more"] is False and oldest["next_before"] is None
        assert newest["next_before"] == ids[5] and older["next_before"] == ids[2]


async def test_the_history_shows_what_was_said_exactly(tmp_path):
    text = "Что приготовить из «яиц» и 🍅?\n\tСтрока 2 \r\nи ещё  пробелы внутри"
    backend = ScriptedBackend("Сварите яйца 🥚.\n1. Вода\n2. Соль")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, f"   {text}  \n", "k")

        messages = (await chat.history(conversation)).json()["messages"]

        assert [m["text"] for m in messages] == [text, "Сварите яйца 🥚.\n1. Вода\n2. Соль"]
        assert [m["status"] for m in messages] == ["completed", "completed"]
        assert all("idempotency_key" not in m and "error_code" not in m for m in messages)
        assert backend.requests[0][0] == [{"role": "user", "content": text}]  # trimmed, nothing else


async def test_reading_writes_nothing_and_never_moves_the_activity_time(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "привет", "k")
        chat.clock.advance(hours=3)
        before = chat.conversation(conversation).updated_at

        with statements_of(chat) as seen:
            for _ in range(3):
                assert (await chat.history(conversation)).status_code == 200
                assert (await chat.client.get("/api/conversations")).status_code == 200
                assert (await chat.history(str(uuid4()))).status_code == 404

        assert not {"INSERT", "UPDATE", "DELETE"} & set(seen)
        assert chat.conversation(conversation).updated_at == before


async def test_expired_messages_are_not_shown(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "старое", "k-1")
        chat.clock.advance(days=364)
        await chat.sign_in_again()  # a session lasts 14 days
        await chat.send(conversation, "новое", "k-2")
        assert [m["text"] for m in (await chat.history(conversation)).json()["messages"]][:1] == ["старое"]

        chat.clock.advance(days=2)  # the first exchange is now 366 days old; the second is not
        await chat.sign_in_again()

        shown = (await chat.history(conversation)).json()["messages"]
        assert [m["text"] for m in shown if m["role"] == "user"] == ["новое"]
        assert len(shown) == 2


# --- sending: the success --------------------------------------------------------------------------


async def test_sending_a_message(tmp_path):
    backend = ScriptedBackend("Омлет: взбейте яйца, посолите, обжарьте.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        chat.clock.advance(minutes=5)

        response = await chat.send(conversation, "У меня есть яйца", "send-1")

        body = response.json()
        assert response.status_code == 201
        assert set(body) == {
            "conversation_id", "user_message", "assistant_message", "truncated", "replayed"
        }
        assert body["conversation_id"] == conversation
        assert body["truncated"] is False and body["replayed"] is False
        assert "idempotent-replayed" not in response.headers
        user, assistant = body["user_message"], body["assistant_message"]
        assert (user["role"], user["text"]) == ("user", "У меня есть яйца")
        assert (assistant["role"], assistant["text"]) == (
            "assistant", "Омлет: взбейте яйца, посолите, обжарьте."
        )
        assert user["id"] < assistant["id"]
        assert user["created_at"] == assistant["created_at"] == chat.clock().isoformat()
        assert chat.conversation(conversation).updated_at == chat.clock()
        (turn,) = chat.turns(conversation)
        assert (turn.status, turn.attempt_count, turn.idempotency_key) == ("completed", 1, "send-1")
        assert turn.result_payload == {
            "reply": "Омлет: взбейте яйца, посолите, обжарьте.", "truncated": False
        }


async def test_the_365_day_deadline_is_recorded_on_everything_that_is_kept(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        chat.clock.advance(minutes=5)
        await chat.send(conversation, "привет", "k")

        year = timedelta(days=365)
        messages = chat.messages(conversation)
        (turn,) = chat.turns(conversation)
        assert len(messages) == 2
        for kept in (*messages, turn):
            assert kept.created_at == chat.clock()
            assert kept.expires_at == kept.created_at + year


# --- sending: what is refused before anything is stored -----------------------------------------------


async def refused(chat, conversation, *, content, key="k", headers=None):
    sent = {**JSON, **(headers or {})}
    if key is not None:
        sent["Idempotency-Key"] = key
    response = await chat.client.post(
        f"/api/conversations/{conversation}/messages",
        content=content,
        headers={"Origin": ORIGIN, **sent},
    )
    assert response.status_code >= 400, response.text
    assert count(chat, Message) == 0 and count(chat, TurnRequest) == 0
    assert chat.backend.calls == 0
    return response


@pytest.mark.parametrize(
    ("content_type", "status"),
    [
        (None, 415),
        ("text/plain", 415),
        ("application/x-www-form-urlencoded", 415),
        ("multipart/form-data; boundary=x", 415),
        ("application/json; charset=latin-1", 415),
        ("application/jsonp", 415),
        ("text/json", 415),
    ],
)
async def test_only_json_is_accepted(tmp_path, content_type, status):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        headers = {"Content-Type": content_type} if content_type else {}

        for path, key in (("/api/conversations", None), (f"/api/conversations/{conversation}/messages", "k")):
            sent = {"Origin": ORIGIN, **headers}
            if key:
                sent["Idempotency-Key"] = key
            response = await chat.client.post(path, content=b'{"text": "a"}' if key else b"{}", headers=sent)
            assert response.status_code == status, (path, content_type)
            assert error_of(response)["detail"] == "unsupported_media_type"


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", "APPLICATION/JSON;charset=UTF-8"],
)
async def test_json_with_utf8_is_accepted(tmp_path, content_type):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        response = await chat.client.post(
            "/api/conversations",
            content=b"{}",
            headers={"Origin": ORIGIN, "Content-Type": content_type},
        )

        assert response.status_code == 201


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"null",
        b"[]",
        b'"text"',
        b"123",
        b"{",
        b'{"text": }',
        b'{"text": "a",}',
        b'{"text": "a"} trailing',
        b"\xef\xbb\xbf" + '{"text": "привет"}'.encode(),  # a byte order mark
        b'{"text": "\xff\xfe"}',  # not UTF-8
        '{"text": "привет"}'.encode("utf-16"),
        b'{"text": "a", "text": "b"}',  # a repeated key
        b'{"text": NaN}',
        b"[" * 10000 + b"]" * 10000,  # nested past what the parser takes
    ],
)
async def test_a_body_that_is_not_a_json_object_with_the_right_fields_is_refused(tmp_path, content):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await refused(chat, conversation, content=content)

        assert response.status_code == 400
        assert error_of(response)["detail"] == "invalid_request"


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"message": "привет"},
        {"text": "привет", "role": "assistant"},
        {"text": "привет", "conversation_id": "x"},
        {"Text": "привет"},
        {"text": None},
        {"text": 5},
        {"text": ["привет"]},
        {"text": {"a": 1}},
        {"text": True},
    ],
)
async def test_the_message_must_be_exactly_a_text(tmp_path, document):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await refused(chat, conversation, content=json.dumps(document).encode())

        assert response.status_code == 400
        assert error_of(response)["detail"] == "invalid_request"


@pytest.mark.parametrize("text", ["", " ", "\n\t \r\n", " "])
async def test_an_empty_message_is_refused(tmp_path, text):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.send(conversation, text, "k")

        # str.strip() trims a no-break space too, so it is as empty as the rest.
        assert response.status_code == 400
        assert error_of(response)["detail"] == "empty_message"
        assert chat.backend.calls == 0 and count(chat, Message) == 0


@pytest.mark.parametrize(
    "escaped",
    [
        r"\u0000",
        r"привет\u0000мир",
        r"\u0001",
        r"\u0008",
        r"\u000b",
        r"\u000c",
        r"\u001b[31m",
        r"\u007f",
        r"\u0085",
        r"\u009f",
        r"\ud800",  # a lone high surrogate: it parses, and cannot be encoded as UTF-8
        r"\udfff",
        r"привет \ude00 мир",
        r"\ud83d",  # half of an emoji
        r"\ud83d\ud83d",
    ],
)
async def test_characters_that_cannot_be_stored_are_refused_not_a_server_error(tmp_path, escaped):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        # A letter on each side: str.strip() would take U+0085, U+000B and the like off
        # the ends of a text made of nothing else, and call it empty.
        response = await refused(
            chat, conversation, content=('{"text": "ы' + escaped + 'ы"}').encode("utf-8")
        )

        assert response.status_code == 400
        assert error_of(response)["detail"] == "invalid_message_text"


@pytest.mark.parametrize(
    "text",
    ["a\tb", "a\nb", "a\r\nb", "🍅", "\U0001f468\u200d\U0001f373", "é", "   ", "﻿x", "x￾"],
)
async def test_ordinary_unusual_text_is_kept_as_it_is(tmp_path, text):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.send(conversation, "ы" + text + "ы", "k")

        assert response.status_code == 201
        assert response.json()["user_message"]["text"] == "ы" + text + "ы"
        assert chat.messages(conversation)[0].content == "ы" + text + "ы"


async def test_the_length_limit_is_2000_characters_after_trimming(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()

        at_limit = await chat.send(conversation, "я" * 2000, "k-1")
        padded = await chat.send(conversation, "  \n" + "я" * 2000 + "\t ", "k-2")
        over = await chat.send(conversation, "я" * 2001, "k-3")
        astral_at_limit = await chat.send(conversation, "🍅" * 2000, "k-4")  # 2000 characters, 8000 bytes
        astral_over = await chat.send(conversation, "🍅" * 2001, "k-5")

        assert [r.status_code for r in (at_limit, padded, over, astral_at_limit, astral_over)] == [
            201, 201, 400, 201, 400,
        ]
        body = error_of(over)
        assert body["detail"] == "message_too_long"
        assert "2000" in body["message"] and body["retryable"] is False
        assert backend.calls == 3
        assert len(chat.turns(conversation)) == 3  # the refused ones left nothing behind


async def test_the_body_size_is_bounded_whether_it_is_declared_or_streamed(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        url = f"/api/conversations/{conversation}/messages"
        headers = {"Origin": ORIGIN, "Idempotency-Key": "k", **JSON}
        big = b'{"text": "' + b"a" * (40 * 1024) + b'"}'

        declared = await chat.client.post(url, content=big, headers=headers)

        async def streamed_body():
            for _ in range(80):
                yield b"a" * 1024

        streamed = await chat.client.post(url, content=streamed_body(), headers=headers)
        created = await chat.client.post(
            "/api/conversations", content=b"{" + b" " * (40 * 1024) + b"}", headers={"Origin": ORIGIN, **JSON}
        )

        assert declared.status_code == streamed.status_code == created.status_code == 413
        assert error_of(declared)["detail"] == "request_too_large"
        assert chat.backend.calls == 0 and count(chat, Message) == 0


async def test_the_largest_legitimate_body_fits(tmp_path):
    # 2000 characters outside the basic plane, written as JSON escapes (12 bytes each).
    text = "🍅" * 2000
    body = '{"text": "' + "\\ud83c\\udf45" * 2000 + '"}'
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.client.post(
            f"/api/conversations/{conversation}/messages",
            content=body.encode("ascii"),
            headers={"Origin": ORIGIN, "Idempotency-Key": "k", **JSON},
        )

        assert response.status_code == 201
        assert response.json()["user_message"]["text"] == text


@pytest.mark.parametrize(
    "key",
    ["", "a" * 129, "has space", "tab\t", "new\nline", "кириллица", "emoji🍅", "x\x00y", "\x7f"],
)
async def test_an_idempotency_key_must_be_1_to_128_printable_ascii(tmp_path, key):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        response = await chat.client.post(
            f"/api/conversations/{conversation}/messages",
            json={"text": "привет"},
            headers=[
                ("Origin", ORIGIN),
                ("Idempotency-Key", key.encode("utf-8", "surrogatepass")),
            ],
        )

        assert response.status_code == 400
        assert error_of(response)["detail"] in {"invalid_idempotency_key", "idempotency_key_required"}
        assert chat.backend.calls == 0 and count(chat, Message) == 0


@pytest.mark.parametrize("key", ["k", "a" * 128, "550e8400-e29b-41d4-a716-446655440000", "!~#$%&'*+-.^_`|\"<>"])
async def test_a_valid_idempotency_key_is_accepted(tmp_path, key):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()

        assert (await chat.send(conversation, "привет", key)).status_code == 201


async def test_a_missing_or_repeated_idempotency_key_is_refused(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        url = f"/api/conversations/{conversation}/messages"

        missing = await chat.client.post(url, json={"text": "привет"}, headers=post_headers())
        repeated = await chat.client.post(
            url,
            json={"text": "привет"},
            headers=[("Origin", ORIGIN), ("Idempotency-Key", "a"), ("Idempotency-Key", "b")],
        )

        assert missing.status_code == repeated.status_code == 400
        assert error_of(missing)["detail"] == "idempotency_key_required"
        assert error_of(repeated)["detail"] == "invalid_idempotency_key"
        assert chat.backend.calls == 0


async def test_a_refusal_does_not_quote_the_request(tmp_path):
    marker = "ZQXMARKER7731"
    async with chat_app(tmp_path, ScriptedBackend()) as chat:
        conversation = await chat.create()
        url = f"/api/conversations/{conversation}/messages"
        attempts = [
            chat.client.post(url, json={"text": marker + "\u0000"}, headers=post_headers("k")),
            chat.client.post(url, json={"text": "я" * 2001, marker: 1}, headers=post_headers("k")),
            chat.client.post(url, json={marker: marker}, headers=post_headers("k")),
            chat.client.post(url, content=("{" + marker).encode(), headers=post_headers("k", **JSON)),
            # (the space makes it not a key)
            chat.client.post(url, json={"text": marker}, headers=post_headers(marker + " x")),
            chat.client.get(f"/api/conversations/{marker}/messages"),
            chat.client.get(url, params={"limit": marker, "before": marker}),
            chat.client.post(url, json={"text": marker}, headers={"Origin": marker, "Idempotency-Key": "k"}),
        ]
        for attempt in attempts:
            response = await attempt
            assert response.status_code >= 400
            assert marker not in response.text
            assert marker not in str(dict(response.headers))


# --- the assistant is not configured --------------------------------------------------------------------


async def test_without_an_assistant_a_message_is_answered_503_and_everything_else_works(tmp_path):
    async with chat_app(tmp_path, None) as chat:
        conversation = await chat.create()

        sent = await chat.send(conversation, "привет", "k")
        history = await chat.history(conversation)
        listing = await chat.client.get("/api/conversations")

        assert sent.status_code == 503
        body = error_of(sent)
        assert (body["detail"], body["retryable"]) == ("chat_unavailable", False)
        assert body["message"] == "Помощник сейчас недоступен."
        assert history.status_code == listing.status_code == 200
        assert count(chat, Message) == 0 and count(chat, TurnRequest) == 0  # nothing stored


async def test_without_an_assistant_a_stranger_still_learns_nothing(tmp_path):
    async with chat_app(tmp_path, None, sign_in_first=False) as chat:
        response = await chat.send(str(uuid4()), "привет", "k")

        assert response.status_code == 401  # the login comes before the configuration


async def test_without_an_assistant_a_refused_request_is_refused_first(tmp_path):
    async with chat_app(tmp_path, None) as chat:
        conversation = await chat.create()

        response = await chat.client.post(
            f"/api/conversations/{conversation}/messages",
            json={"text": "привет"},
            headers={"Origin": "https://evil.example", "Idempotency-Key": "k"},
        )

        assert response.status_code == 403
