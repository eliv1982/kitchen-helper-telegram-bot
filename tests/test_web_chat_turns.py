"""What happens to a message once the chat accepts it.

Through the HTTP API with a ScriptedBackend: the context the assistant is shown,
idempotent replay, one turn at a time per conversation, failed turns and their retry,
a client that disconnects, a database that fails half-way, a reply cut off at the
length cap. Directly on the runner (with a database and a clock of the test's own):
the deadline, the lease, stopping the application with a turn still running.

Concurrency is made deterministic with the backend's gate and with events; the only
waiting is for something the test has already arranged to happen.
"""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa

from house_helper.core.backend import (
    BackendConnectionError,
    BackendEmptyResponseError,
    BackendError,
    BackendRateLimitedError,
    BackendReply,
)
from house_helper.core.limits import MAX_HISTORY_MESSAGES
from house_helper.persistence import turns
from house_helper.persistence.models import Message
from house_helper.web.diagnostics import ApiError
from house_helper.web.turn_runner import TRUNCATED_NOTE
from tests.auth_support import logout_as
from tests.chat_support import ScriptedBackend, chat_app, error_of, gate_on, reached, runner_env
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio


async def until_done(tasks, count):
    """Wait until `count` of the tasks have finished (the others may be held)."""
    pending = set(tasks)
    while sum(task.done() for task in tasks) < count:
        done, pending = await asyncio.wait(pending, timeout=TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        assert done, "nothing finished"


def roles(messages):
    return [(m["role"], m["content"]) for m in messages]


# --- the context the assistant is shown -------------------------------------------------------


async def test_a_follow_up_is_answered_with_the_conversation_so_far(tmp_path):
    backend = ScriptedBackend("Омлет.", "Без молока: на воде.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()

        await chat.send(conversation, "Что из яиц?", "k-1")
        await chat.send(conversation, "А без молока?", "k-2")

        first, second = backend.requests
        assert roles(first[0]) == [("user", "Что из яиц?")]
        assert roles(second[0]) == [
            ("user", "Что из яиц?"),
            ("assistant", "Омлет."),
            ("user", "А без молока?"),
        ]
        assert [call[1].channel for call in backend.requests] == ["web", "web"]
        assert [call[1].subject for call in backend.requests] == [conversation, conversation]
        assert [call[2] for call in backend.requests] == [len(first[0]), len(second[0])]


async def test_the_context_is_bounded_to_20_including_the_current_message(tmp_path):
    backend = ScriptedBackend(default="ответ")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        for index in range(15):
            assert (await chat.send(conversation, f"вопрос {index}", f"k-{index}")).status_code == 201

        sizes = [len(request[0]) for request in backend.requests]
        last = backend.requests[-1][0]

        assert max(sizes) <= MAX_HISTORY_MESSAGES == 20
        assert sizes[:10] == [1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
        assert sizes[10:] == [19] * 5  # whole exchanges: the window never starts on an answer
        assert last[0]["role"] == "user" and last[-1] == {"role": "user", "content": "вопрос 14"}
        # the newest nine exchanges and the question
        assert [m["content"] for m in last if m["role"] == "user"] == [f"вопрос {i}" for i in range(5, 15)]
        assert [m["role"] for m in last] == ["user", "assistant"] * 9 + ["user"]
        # the history itself is not bounded
        history = (await chat.history(conversation, limit=100)).json()["messages"]
        assert len(history) == 30


async def test_what_is_sent_to_the_assistant_is_user_and_assistant_turns_only(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "раз", "k-1")
        await chat.send(conversation, "два", "k-2")

        for messages, *_ in backend.requests:
            assert {m["role"] for m in messages} <= {"user", "assistant"}
            assert all(set(m) == {"role", "content"} for m in messages)


async def test_a_failed_message_is_not_part_of_what_the_assistant_is_shown_later(tmp_path):
    backend = ScriptedBackend("Омлет.", BackendRateLimitedError("x"), "Каша.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "Что из яиц?", "k-1")
        failed = await chat.send(conversation, "Что из риса?", "k-2")
        assert failed.status_code == 503

        await chat.send(conversation, "А из овсянки?", "k-3")

        assert roles(backend.requests[2][0]) == [
            ("user", "Что из яиц?"),
            ("assistant", "Омлет."),
            ("user", "А из овсянки?"),
        ]
        # ... while the history still shows it, as failed, for the person to see.
        shown = (await chat.history(conversation)).json()["messages"]
        assert [(m["role"], m["text"], m["status"]) for m in shown] == [
            ("user", "Что из яиц?", "completed"),
            ("assistant", "Омлет.", "completed"),
            ("user", "Что из риса?", "failed"),
            ("user", "А из овсянки?", "completed"),
            ("assistant", "Каша.", "completed"),
        ]


async def test_conversations_never_see_each_other(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        first, second = await chat.create(), await chat.create()
        await chat.send(first, "про борщ", "k-1")
        await chat.send(second, "про плов", "k-1")  # the same key: keys are per conversation

        assert roles(backend.requests[1][0]) == [("user", "про плов")]
        assert [m.content for m in chat.messages(second)][0] == "про плов"


async def test_messages_that_have_expired_are_not_shown_to_the_assistant(tmp_path):
    backend = ScriptedBackend("старый ответ", "новый ответ")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "старый вопрос", "k-1")
        chat.clock.advance(days=366)  # the exchange has expired; its rows are still there
        await chat.sign_in_again()

        await chat.send(conversation, "новый вопрос", "k-2")

        assert roles(backend.requests[1][0]) == [("user", "новый вопрос")]
        assert len(chat.messages(conversation)) == 4  # nothing was deleted: reads leave them be


async def test_a_reset_marker_cuts_the_context_there(tmp_path):
    backend = ScriptedBackend("раз", "два")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "до", "k-1")
        with chat.base.factory() as db, db.begin():
            db.add(
                Message(
                    conversation_id=UUID(conversation),
                    role="system_note",
                    content="context reset",
                    created_at=chat.clock(),
                )
            )

        await chat.send(conversation, "после", "k-2")

        assert roles(backend.requests[1][0]) == [("user", "после")]
        shown = (await chat.history(conversation)).json()["messages"]
        assert "context reset" not in str(shown)  # a note is for the application, not the person


# --- idempotent replay -------------------------------------------------------------------------


async def test_the_same_key_and_text_again_is_answered_from_the_first_time(tmp_path):
    backend = ScriptedBackend("Омлет.", "не должно быть показано")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        first = await chat.send(conversation, "Что из яиц?", "k-1")
        before = chat.conversation(conversation).updated_at
        chat.clock.advance(minutes=10)

        replay = await chat.send(conversation, "  Что из яиц?  ", "k-1")  # trimmed: the same text

        assert first.status_code == 201 and replay.status_code == 200
        assert replay.headers["idempotent-replayed"] == "true"
        assert replay.json() == {**first.json(), "replayed": True}
        assert backend.calls == 1
        assert len(chat.messages(conversation)) == 2 and len(chat.turns(conversation)) == 1
        assert chat.conversation(conversation).updated_at == before  # a replay is not activity


async def test_a_key_is_used_up_by_a_different_text_not_taken_over(tmp_path):
    backend = ScriptedBackend("Омлет.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        first = await chat.send(conversation, "Что из яиц?", "k-1")
        chat.clock.advance(minutes=1)

        reuse = await chat.send(conversation, "Что из риса?", "k-1")

        assert reuse.status_code == 409
        body = error_of(reuse)
        assert (body["detail"], body["retryable"]) == ("idempotency_key_reused", False)
        assert backend.calls == 1 and len(chat.messages(conversation)) == 2
        assert (await chat.send(conversation, "Что из яиц?", "k-1")).json()["assistant_message"] == (
            first.json()["assistant_message"]
        )


async def test_a_replay_survives_a_new_session(tmp_path):
    backend = ScriptedBackend("Омлет.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        first = await chat.send(conversation, "Что из яиц?", "k-1")
        await chat.base.client.post(
            "/auth/logout", headers=logout_as(chat.user_id, Origin="https://kitchen.example.test")
        )
        await chat.sign_in_again()

        replay = await chat.send(conversation, "Что из яиц?", "k-1")

        assert replay.status_code == 200 and backend.calls == 1
        assert replay.json()["assistant_message"] == first.json()["assistant_message"]


# --- one turn at a time per conversation ----------------------------------------------------------


async def test_a_second_message_while_one_is_being_answered_is_refused_and_retryable(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        first = asyncio.ensure_future(chat.send(conversation, "Что из яиц?", "k-1"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        second = await chat.send(conversation, "А из риса?", "k-2")

        assert second.status_code == 409
        body = error_of(second)
        assert (body["detail"], body["retryable"]) == ("conversation_busy", True)
        assert body["retry_after_seconds"] == 5 and second.headers["retry-after"] == "5"
        assert "Подождите" in body["message"]
        assert backend.calls == 1
        assert [m.content for m in chat.messages(conversation)] == ["Что из яиц?"]  # nothing of the second
        assert len(chat.turns(conversation)) == 1

        release.set()
        assert (await first).status_code == 201
        retried = await chat.send(conversation, "А из риса?", "k-2")  # the same request, now
        assert retried.status_code == 201
        assert roles(backend.requests[1][0])[:2] == [("user", "Что из яиц?"), ("assistant", backend.default)]


async def test_the_same_request_while_it_is_being_answered_is_in_progress_not_run_twice(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        first = asyncio.ensure_future(chat.send(conversation, "Что из яиц?", "k-1"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        again = await chat.send(conversation, "Что из яиц?", "k-1")

        assert again.status_code == 409
        body = error_of(again)
        assert (body["detail"], body["retryable"]) == ("turn_in_progress", True)
        assert again.headers["retry-after"] == "5"

        release.set()
        original = await first
        assert original.status_code == 201 and backend.calls == 1
        replay = await chat.send(conversation, "Что из яиц?", "k-1")
        assert replay.status_code == 200 and replay.json()["assistant_message"] == original.json()["assistant_message"]


async def test_many_simultaneous_messages_one_is_answered_and_the_rest_are_told_to_wait(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        tasks = [
            asyncio.ensure_future(chat.send(conversation, f"вопрос {index}", f"k-{index}"))
            for index in range(8)
        ]
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        await until_done(tasks, 7)  # everyone but the one that is being answered

        release.set()
        responses = await asyncio.gather(*tasks)

        assert sorted(r.status_code for r in responses) == [201] + [409] * 7
        assert {error_of(r)["detail"] for r in responses if r.status_code == 409} == {"conversation_busy"}
        assert backend.calls == 1
        assert len(chat.turns(conversation)) == 1 and len(chat.messages(conversation)) == 2


async def test_other_conversations_are_not_held_up(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        slow, quick = await chat.create(), await chat.create()
        arrived, release = backend.hold()
        first = asyncio.ensure_future(chat.send(slow, "долгий вопрос", "k-1"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        backend._gate = None  # the next call is not held

        other = await chat.send(quick, "быстрый вопрос", "k-1")

        assert other.status_code == 201
        release.set()
        assert (await first).status_code == 201


async def test_two_people_are_independent_of_each_other(tmp_path):
    backend = ScriptedBackend()
    async with chat_app(tmp_path, backend) as chat:
        other = await chat.second_user()
        mine, theirs = await chat.create(), await chat.create(other)
        arrived, release = backend.hold()
        first = asyncio.ensure_future(chat.send(mine, "мой вопрос", "k-1"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        backend._gate = None

        answered = await chat.send(theirs, "их вопрос", "k-1", other)

        assert answered.status_code == 201
        release.set()
        await first


# --- a failed turn, and trying again --------------------------------------------------------------

FAILURES = [
    pytest.param(BackendRateLimitedError("x"), 503, "provider_rate_limited", True, id="rate-limited"),
    pytest.param(BackendConnectionError("x"), 502, "provider_unreachable", True, id="unreachable"),
    pytest.param(BackendEmptyResponseError("x"), 502, "provider_empty_response", True, id="empty"),
    pytest.param(BackendError("x"), 502, "provider_error", True, id="provider-error"),
    pytest.param(TimeoutError("x"), 504, "provider_timeout", True, id="timeout"),
    pytest.param(asyncio.TimeoutError(), 504, "provider_timeout", True, id="asyncio-timeout"),
    pytest.param(ValueError("x"), 500, "internal_error", True, id="bug"),
]


@pytest.mark.parametrize(("failure", "status", "code", "retryable"), FAILURES)
async def test_a_failed_turn_is_kept_as_failed_and_the_same_key_tries_again(
    tmp_path, failure, status, code, retryable
):
    backend = ScriptedBackend(failure, "Омлет.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()

        failed = await chat.send(conversation, "Что из яиц?", "k-1")

        assert failed.status_code == status
        body = error_of(failed)
        assert (body["detail"], body["retryable"]) == (code, retryable)
        stored = chat.messages(conversation)
        (turn,) = chat.turns(conversation)
        assert [m.role for m in stored] == ["user"] and body["user_message_id"] == stored[0].id
        assert (turn.status, turn.failure_kind, turn.error_code) == ("failed", "generation", code)
        assert turn.finished_at is not None and turn.result_payload is None
        shown = (await chat.history(conversation)).json()["messages"]
        assert shown == [
            {
                "id": stored[0].id,
                "role": "user",
                "text": "Что из яиц?",
                "created_at": chat.clock().isoformat(),
                "status": "failed",
                "idempotency_key": "k-1",
                "error_code": code,
            }
        ]

        retried = await chat.send(conversation, "Что из яиц?", "k-1")

        assert retried.status_code == 201 and retried.json()["replayed"] is False
        assert retried.json()["user_message"]["id"] == stored[0].id  # the same message, not a second one
        (turn,) = chat.turns(conversation)
        assert (turn.status, turn.attempt_count) == ("completed", 2)
        assert (turn.failure_kind, turn.error_code) == (None, None)
        assert len(chat.messages(conversation)) == 2
        assert backend.calls == 2
        assert roles(backend.requests[1][0]) == [("user", "Что из яиц?")]
        shown = (await chat.history(conversation)).json()["messages"]
        assert [m["status"] for m in shown] == ["completed", "completed"]
        assert all("idempotency_key" not in m and "error_code" not in m for m in shown)


async def test_a_rate_limited_answer_says_how_long_to_wait(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(BackendRateLimitedError("x"))) as chat:
        conversation = await chat.create()

        failed = await chat.send(conversation, "привет", "k")

        assert failed.headers["retry-after"] == "30"
        assert error_of(failed)["retry_after_seconds"] == 30


async def test_a_failure_leaves_the_conversation_free_for_another_message(tmp_path):
    backend = ScriptedBackend(BackendError("x"), "Каша.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        assert (await chat.send(conversation, "Что из яиц?", "k-1")).status_code == 502

        other = await chat.send(conversation, "Что из риса?", "k-2")

        assert other.status_code == 201


async def test_a_failed_message_cannot_be_retried_once_the_conversation_has_moved_on(tmp_path):
    backend = ScriptedBackend(BackendError("x"), "Каша.", "Омлет.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "Что из яиц?", "k-1")  # fails
        await chat.send(conversation, "Что из риса?", "k-2")  # answered

        late = await chat.send(conversation, "Что из яиц?", "k-1")

        assert late.status_code == 409
        body = error_of(late)
        assert (body["detail"], body["retryable"]) == ("turn_superseded", False)
        assert backend.calls == 2
        (first, second) = chat.turns(conversation)
        assert (first.status, second.status) == ("failed", "completed")
        # it is sent again as a new message instead, with a new key
        assert (await chat.send(conversation, "Что из яиц?", "k-3")).status_code == 201


async def test_a_retry_with_other_text_is_not_a_retry(tmp_path):
    backend = ScriptedBackend(BackendError("x"))
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await chat.send(conversation, "Что из яиц?", "k-1")

        other = await chat.send(conversation, "Что-то другое?", "k-1")

        assert other.status_code == 409 and backend.calls == 1


@pytest.mark.parametrize("reply", ["", "   \n", "\x00", "\x00 \x00", "\ud800"])
async def test_a_reply_with_nothing_in_it_is_a_failure_not_an_empty_message(tmp_path, reply):
    backend = ScriptedBackend(reply)
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()

        failed = await chat.send(conversation, "привет", "k")

        # A lone surrogate is replaced, not dropped, so that one is a (strange) reply.
        if reply == "\ud800":
            assert failed.status_code == 201
            assert failed.json()["assistant_message"]["text"] == "�"
        else:
            assert failed.status_code == 502
            assert error_of(failed)["detail"] == "provider_empty_response"
            assert [m.role for m in chat.messages(conversation)] == ["user"]


async def test_a_reply_that_cannot_be_stored_as_it_is_is_cleaned_not_refused(tmp_path):
    backend = ScriptedBackend("Сварите\x00 яйца \ud83d.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()

        sent = await chat.send(conversation, "привет", "k")

        assert sent.status_code == 201
        assert sent.json()["assistant_message"]["text"] == "Сварите яйца �."


# --- an answer cut off at the length cap ------------------------------------------------------------


async def test_an_answer_that_was_cut_off_says_so_everywhere(tmp_path):
    cut = BackendReply(text="Омлет. Ингредиенты: яйца, молоко, со", truncated=True)
    backend = ScriptedBackend(cut, "Соль и масло.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()

        sent = await chat.send(conversation, "Расскажи рецепт омлета", "k-1")

        body = sent.json()
        assert sent.status_code == 201 and body["truncated"] is True
        assert body["assistant_message"]["text"] == "Омлет. Ингредиенты: яйца, молоко, со" + TRUNCATED_NOTE
        assert "продолжи" in TRUNCATED_NOTE and "оборвался" in TRUNCATED_NOTE
        (turn,) = chat.turns(conversation)
        assert turn.result_payload["truncated"] is True
        replay = await chat.send(conversation, "Расскажи рецепт омлета", "k-1")
        assert replay.json()["truncated"] is True and replay.json()["assistant_message"] == body["assistant_message"]
        # the next question shows the assistant that its own answer ended there
        await chat.send(conversation, "продолжи", "k-2")
        assert backend.requests[1][0][1]["content"].endswith(TRUNCATED_NOTE)
        shown = (await chat.history(conversation)).json()["messages"]
        assert shown[1]["text"].endswith("(Ответ получился слишком длинным и оборвался. "
                                         "Напишите «продолжи», чтобы получить остальное.)")


async def test_a_complete_answer_is_not_marked(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend(BackendReply(text="Всё.", truncated=False))) as chat:
        conversation = await chat.create()

        sent = await chat.send(conversation, "привет", "k")

        assert sent.json()["truncated"] is False
        assert sent.json()["assistant_message"]["text"] == "Всё."


# --- a client that goes away ----------------------------------------------------------------------------


async def test_a_disconnect_during_generation_does_not_lose_the_answer(tmp_path):
    backend = ScriptedBackend("Омлет.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        request = asyncio.ensure_future(chat.send(conversation, "Что из яиц?", "k-1"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        assert [t.status for t in chat.turns(conversation)] == ["processing"]

        request.cancel()  # the browser goes away
        with pytest.raises(asyncio.CancelledError):
            await request

        assert backend.cancelled == 0  # the assistant was not interrupted
        assert [t.status for t in chat.turns(conversation)] == ["processing"]
        release.set()
        await chat.settle()

        (turn,) = chat.turns(conversation)
        assert (turn.status, turn.attempt_count) == ("completed", 1)
        assert [m.content for m in chat.messages(conversation)] == ["Что из яиц?", "Омлет."]
        # the person comes back, sends the same thing again, and gets that answer
        replay = await chat.send(conversation, "Что из яиц?", "k-1")
        assert replay.status_code == 200 and replay.json()["assistant_message"]["text"] == "Омлет."
        assert backend.calls == 1


async def test_a_retry_while_the_abandoned_turn_is_still_running_waits_for_it(tmp_path):
    backend = ScriptedBackend("Омлет.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        request = asyncio.ensure_future(chat.send(conversation, "Что из яиц?", "k-1"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        impatient = await chat.send(conversation, "Что из яиц?", "k-1")

        assert impatient.status_code == 409 and error_of(impatient)["detail"] == "turn_in_progress"
        release.set()
        await chat.settle()
        assert (await chat.send(conversation, "Что из яиц?", "k-1")).status_code == 200


async def test_a_disconnect_during_a_failing_generation_still_records_the_failure(tmp_path):
    backend = ScriptedBackend(BackendError("x"), "Омлет.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        request = asyncio.ensure_future(chat.send(conversation, "Что из яиц?", "k-1"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        release.set()
        await chat.settle()

        (turn,) = chat.turns(conversation)
        assert (turn.status, turn.error_code) == ("failed", "provider_error")
        assert (await chat.send(conversation, "Что из яиц?", "k-1")).status_code == 201


async def test_a_disconnect_while_the_message_is_being_registered_leaves_nothing_half_done(
    tmp_path, monkeypatch
):
    import threading

    backend = ScriptedBackend("Омлет.")
    entered, proceed = threading.Event(), threading.Event()
    real = turns.begin_turn

    def slow_begin(*args, **kwargs):
        entered.set()
        assert proceed.wait(TIMEOUT)
        return real(*args, **kwargs)

    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        monkeypatch.setattr(turns, "begin_turn", slow_begin)
        request = asyncio.ensure_future(chat.send(conversation, "Что из яиц?", "k-1"))
        await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, entered.wait, TIMEOUT), TIMEOUT)

        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        proceed.set()
        await chat.settle()

        (turn,) = chat.turns(conversation)
        assert turn.status == "completed"  # the turn it started is the turn it finished
        assert len(chat.messages(conversation)) == 2 and backend.calls == 1


# --- the database fails ------------------------------------------------------------------------------------


def operational_error(detail: str = "disk I/O error"):
    return sa.exc.OperationalError("UPDATE secret_table SET x = ?", {"x": "SECRET"}, Exception(detail))


async def test_a_failure_to_store_the_reply_is_recorded_and_the_retry_generates_again(
    tmp_path, monkeypatch
):
    harness = []

    def slow_answer(messages):
        harness[0].clock.advance(seconds=3)  # so the completion happens later than the registration
        return "первый ответ"

    backend = ScriptedBackend(slow_answer, "второй ответ")
    real = turns.complete_turn
    attempts = []

    def failing_once(session, claim, reply, **kwargs):
        attempts.append(claim.attempt)
        if len(attempts) == 1:
            real(session, claim, reply, **kwargs)  # everything is written ...
            raise operational_error()  # ... and then the transaction fails
        return real(session, claim, reply, **kwargs)

    async with chat_app(tmp_path, backend) as chat:
        harness.append(chat)
        conversation = await chat.create()
        monkeypatch.setattr(turns, "complete_turn", failing_once)
        chat.clock.advance(minutes=5)
        registered = chat.clock()

        failed = await chat.send(conversation, "Что из яиц?", "k-1")

        assert chat.clock() == registered + timedelta(seconds=3)
        assert failed.status_code == 503
        body = error_of(failed)
        assert (body["detail"], body["retryable"]) == ("service_unavailable", True)
        assert "user_message_id" in body
        # The transaction rolled back as a whole: no assistant message, no completion,
        # and the conversation's activity time is where registration left it.
        assert [m.role for m in chat.messages(conversation)] == ["user"]
        (turn,) = chat.turns(conversation)
        assert (turn.status, turn.error_code) == ("failed", "storage_unavailable")
        assert chat.conversation(conversation).updated_at == registered

        chat.clock.advance(minutes=1)
        retried = await chat.send(conversation, "Что из яиц?", "k-1")

        assert retried.status_code == 201
        assert retried.json()["assistant_message"]["text"] == "второй ответ"
        (turn,) = chat.turns(conversation)
        assert (turn.status, turn.attempt_count) == ("completed", 2)
        assert [m.role for m in chat.messages(conversation)] == ["user", "assistant"]
        assert attempts == [1, 2]


async def test_when_even_the_failure_cannot_be_recorded_the_lease_releases_the_turn(
    tmp_path, monkeypatch
):
    backend = ScriptedBackend("первый ответ", "второй ответ")

    def down(*args, **kwargs):
        raise operational_error()

    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        with monkeypatch.context() as broken:
            broken.setattr(turns, "complete_turn", down)
            broken.setattr(turns, "fail_turn", down)
            failed = await chat.send(conversation, "Что из яиц?", "k-1")
        assert failed.status_code == 503
        (turn,) = chat.turns(conversation)
        assert turn.status == "processing"  # nobody could say otherwise

        too_soon = await chat.send(conversation, "Что из яиц?", "k-1")
        also_blocked = await chat.send(conversation, "Что из риса?", "k-2")
        assert too_soon.status_code == 409 and error_of(too_soon)["detail"] == "turn_in_progress"
        assert also_blocked.status_code == 409 and error_of(also_blocked)["detail"] == "conversation_busy"
        shown = (await chat.history(conversation)).json()["messages"]
        assert shown[0]["status"] == "in_progress"

        chat.clock.advance(seconds=91)  # past the lease: 60 s to answer and 30 s of margin
        shown = (await chat.history(conversation)).json()["messages"]
        assert shown[0]["status"] == "failed" and shown[0]["idempotency_key"] == "k-1"
        retried = await chat.send(conversation, "Что из яиц?", "k-1")

        assert retried.status_code == 201
        (turn,) = chat.turns(conversation)
        assert (turn.status, turn.attempt_count) == ("completed", 2)
        assert [m.role for m in chat.messages(conversation)] == ["user", "assistant"]


async def test_a_failure_to_register_the_message_stores_nothing_and_the_retry_works(
    tmp_path, monkeypatch
):
    backend = ScriptedBackend("Омлет.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()

        def down(*args, **kwargs):
            raise operational_error()

        with monkeypatch.context() as broken:
            broken.setattr(turns, "begin_turn", down)
            failed = await chat.send(conversation, "Что из яиц?", "k-1")

        assert failed.status_code == 503 and "user_message_id" not in error_of(failed)
        assert chat.messages(conversation) == [] and chat.turns(conversation) == []
        assert backend.calls == 0
        assert (await chat.send(conversation, "Что из яиц?", "k-1")).status_code == 201


async def test_a_late_answer_from_an_attempt_that_lost_its_lease_is_discarded(tmp_path):
    backend = ScriptedBackend("ответ первой попытки", "ответ второй попытки")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        first = asyncio.ensure_future(chat.send(conversation, "Что из яиц?", "k-1"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        backend._gate = None
        chat.clock.advance(seconds=91)  # the first attempt has outlived its lease

        second = await chat.send(conversation, "Что из яиц?", "k-1")  # claims it again
        assert second.status_code == 201
        release.set()
        late = await first

        assert late.status_code == 409 and error_of(late)["detail"] == "turn_in_progress"
        assert backend.calls == 2
        (turn,) = chat.turns(conversation)
        assert (turn.status, turn.attempt_count) == ("completed", 2)
        # the answer that stands is the one of the attempt that held the lease
        assert [m.content for m in chat.messages(conversation)] == [
            "Что из яиц?", "ответ второй попытки"
        ]


async def test_an_expired_turn_is_retired_by_the_next_message_and_its_late_answer_is_discarded(tmp_path):
    """The audit's reproduction, through the API: A is being answered when its lease runs
    out, B (another key) is admitted, then A's provider answers. A's answer must not be
    stored, and the next context must be well formed."""
    backend = ScriptedBackend("ответ на A", "ответ на B", "ответ на C")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        first = asyncio.ensure_future(chat.send(conversation, "вопрос A", "k-A"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        backend._gate = None  # the other calls are not held
        chat.clock.advance(seconds=91)  # A has outlived its lease

        second = await chat.send(conversation, "вопрос B", "k-B")  # another key: not a retry of A
        assert second.status_code == 201 and second.json()["assistant_message"]["text"] == "ответ на B"
        release.set()
        late = await first

        assert late.status_code == 409
        body = error_of(late)
        assert (body["detail"], body["retryable"]) == ("turn_in_progress", True)
        assert backend.calls == 2
        a, b = chat.turns(conversation)
        assert (a.status, a.failure_kind, a.error_code, a.assistant_message_id) == (
            "failed", "generation", "interrupted", None,
        )
        assert (b.status, b.attempt_count) == ("completed", 1)
        # one answer, and it is B's: A's late answer left nothing in the history
        assert [(m.role, m.content) for m in chat.messages(conversation)] == [
            ("user", "вопрос A"), ("user", "вопрос B"), ("assistant", "ответ на B"),
        ]
        assert roles(backend.requests[1][0]) == [("user", "вопрос B")]  # A was never B's context
        shown = (await chat.history(conversation)).json()["messages"]
        assert [(m["text"], m["status"]) for m in shown] == [
            ("вопрос A", "failed"), ("вопрос B", "completed"), ("ответ на B", "completed"),
        ]
        # the next message is shown the exchange that happened, and only that
        third = await chat.send(conversation, "вопрос C", "k-C")
        assert third.status_code == 201
        assert roles(backend.requests[2][0]) == [
            ("user", "вопрос B"), ("assistant", "ответ на B"), ("user", "вопрос C"),
        ]
        # A cannot be sent again under its key: it is behind B now
        again = await chat.send(conversation, "вопрос A", "k-A")
        assert again.status_code == 409 and error_of(again)["detail"] == "turn_superseded"


async def test_the_same_key_after_the_lease_is_a_retry_while_another_key_is_a_new_message(tmp_path):
    backend = ScriptedBackend("первый", "второй")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        arrived, release = backend.hold()
        first = asyncio.ensure_future(chat.send(conversation, "вопрос A", "k-A"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        backend._gate = None
        chat.clock.advance(seconds=91)

        retried = await chat.send(conversation, "вопрос A", "k-A")  # the same key: claims it again

        assert retried.status_code == 201
        (turn,) = chat.turns(conversation)
        assert (turn.status, turn.attempt_count, turn.error_code) == ("completed", 2, None)
        assert len(chat.messages(conversation)) == 2  # one question, one answer
        release.set()
        assert (await first).status_code == 409  # the first attempt's answer is the one refused
        assert len(chat.messages(conversation)) == 2


# --- the runner on its own: the deadline, stopping, the lease ------------------------------------------------


async def test_a_backend_that_does_not_answer_is_cut_off_at_the_deadline(tmp_path):
    backend = ScriptedBackend()
    backend.hold()  # never released
    async with runner_env(tmp_path, backend, deadline_seconds=0.05) as env:
        with pytest.raises(ApiError) as raised:
            await asyncio.wait_for(env.submit(), TIMEOUT)  # (a missing deadline fails, not hangs)

        assert raised.value.code == "provider_timeout"
        assert backend.cancelled == 1  # the call itself was cancelled, not abandoned
        turn = env.turn()
        assert (turn.status, turn.error_code) == ("failed", "provider_timeout")
        assert env.count(Message) == 1


async def test_a_turn_still_running_when_the_application_stops_is_marked_interrupted(tmp_path):
    backend = ScriptedBackend()
    arrived, _ = backend.hold()  # never released
    async with runner_env(tmp_path, backend, shutdown_grace_seconds=0.5) as env:
        request = asyncio.ensure_future(env.submit())
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        await env.runner.aclose()

        with pytest.raises(asyncio.CancelledError):
            await request
        turn = env.turn()
        assert (turn.status, turn.error_code) == ("failed", "interrupted")
        assert backend.cancelled == 1
        with pytest.raises(ApiError) as refused:  # and it takes no more
            await env.submit(key="k-2")
        assert refused.value.code == "service_unavailable"


async def test_stopping_the_application_lets_a_turn_that_is_nearly_done_finish(tmp_path):
    backend = ScriptedBackend("Омлет.")
    arrived, release = backend.hold()
    async with runner_env(tmp_path, backend, shutdown_grace_seconds=5.0) as env:
        request = asyncio.ensure_future(env.submit())
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        closing = asyncio.ensure_future(env.runner.aclose())
        await asyncio.sleep(0)  # let it start waiting

        release.set()
        await asyncio.wait_for(closing, TIMEOUT)

        result = await request
        assert result.completed.assistant_message.text == "Омлет."
        assert env.turn().status == "completed"


async def test_a_turn_abandoned_by_a_dead_worker_is_not_taken_until_its_lease_runs_out(tmp_path):
    backend = ScriptedBackend("Омлет.")
    async with runner_env(tmp_path, backend) as env:
        # a worker that claimed the request and then died: it never answers
        with env.factory() as db, db.begin():
            turns.begin_turn(
                db,
                env.user_id,
                env.conversation_id,
                "k-1",
                "Что из яиц?",
                clock=env.clock,
                lease=timedelta(seconds=90),
                max_context_messages=20,
            )

        with pytest.raises(ApiError) as waiting:
            await env.submit()
        assert waiting.value.code == "turn_in_progress"
        assert backend.calls == 0

        env.clock.advance(seconds=89)
        with pytest.raises(ApiError):
            await env.submit()

        env.clock.advance(seconds=2)
        result = await env.submit()

        assert result.replayed is False and backend.calls == 1
        assert env.turn().attempt_count == 2


async def test_the_lease_is_the_deadline_and_a_margin(tmp_path):
    backend = ScriptedBackend("Омлет.")
    async with runner_env(tmp_path, backend, deadline_seconds=10, lease_margin_seconds=5) as env:
        arrived, release = backend.hold()
        request = asyncio.ensure_future(env.submit())
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        turn = env.turn()
        assert turn.lease_expires_at - turn.processing_started_at == timedelta(seconds=15)
        release.set()
        await request


async def test_a_bug_in_the_backend_is_an_internal_error_and_the_turn_is_released(tmp_path):
    backend = ScriptedBackend(RuntimeError("something with SECRET in it"))
    async with runner_env(tmp_path, backend) as env:
        with pytest.raises(ApiError) as raised:
            await env.submit()

        assert raised.value.code == "internal_error"
        assert raised.value.category == "RuntimeError"
        assert "SECRET" not in str(raised.value) and "SECRET" not in repr(raised.value.__dict__)
        assert env.turn().error_code == "internal_error"


async def test_the_lease_runs_from_the_claim_not_from_the_start_of_the_request(tmp_path, monkeypatch):
    """The request waits for the database before it is claimed (here: a hundred seconds,
    longer than the whole lease). A lease measured from the start of the request would
    be over before the backend was asked, and the answer would be refused."""
    backend = ScriptedBackend("Омлет.")
    entered, proceed = gate_on(monkeypatch, "begin_turn")
    async with runner_env(tmp_path, backend) as env:
        request = asyncio.ensure_future(env.submit())
        await reached(entered)
        env.clock.advance(seconds=100)  # the time the request spent waiting to be claimed
        claimed_at = env.clock()
        proceed.set()

        result = await asyncio.wait_for(request, TIMEOUT)

        turn = env.turn()
        assert turn.processing_started_at == claimed_at
        assert turn.lease_expires_at == claimed_at + timedelta(seconds=90)
        assert result.completed.assistant_message.text == "Омлет." and turn.status == "completed"


async def test_an_answer_that_comes_after_the_lease_is_not_stored_even_if_nobody_replaced_it(tmp_path):
    backend = ScriptedBackend("первый ответ", "второй ответ")
    async with runner_env(tmp_path, backend) as env:
        arrived, release = backend.hold()
        request = asyncio.ensure_future(env.submit())
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        backend._gate = None
        env.clock.advance(seconds=90)  # the lease is over at this very instant
        release.set()

        with pytest.raises(ApiError) as lost:
            await asyncio.wait_for(request, TIMEOUT)

        assert (lost.value.code, lost.value.category) == ("turn_in_progress", "LeaseLost")
        assert lost.value.user_message_id is not None
        turn = env.turn()
        assert (turn.status, turn.attempt_count, turn.assistant_message_id) == ("processing", 1, None)
        assert env.count(Message) == 1  # the late answer was not kept
        # the same key claims it (its lease is over) and answers
        again = await env.submit()
        assert again.completed.assistant_message.text == "второй ответ"
        assert env.turn().attempt_count == 2


async def test_the_documented_numbers_are_the_ones_in_force(tmp_path):
    backend = ScriptedBackend("Омлет.")
    async with runner_env(tmp_path, backend) as env:
        arrived, release = backend.hold()
        request = asyncio.ensure_future(env.submit())
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        turn = env.turn()
        assert turn.lease_expires_at - turn.processing_started_at == timedelta(seconds=90)
        assert env.runner._deadline == 60  # (the provider's deadline is shorter than the lease by 30 s)
        release.set()
        await request
