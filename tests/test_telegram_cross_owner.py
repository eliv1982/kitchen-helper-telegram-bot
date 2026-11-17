"""A Telegram update belongs to the bot, not to whoever the sending account is tied to this week.

The finding of the second independent review: chat T belongs to user A; update U reaches the
assistant and the attempt times out; T is unlinked from A and linked to B; Telegram brings U
again. The old look was through the CURRENT owner's conversations only, so U looked new, the
assistant ran again, and A's words were stored in B's history.

Now the update's id is looked up across owners, in the claim's own transaction as well as before
anything is fetched, and an update that was admitted for somebody else is dropped: the person
behind the account gets the fixed "the connection changed" notice, and nothing of the first
owner's message, photo or answer is read, fetched, stored, sent to the assistant or sent on. What
the same owner gets (the replay of a completed turn, the refusal of an attempt that began and did
not complete) and what the website does with its own retry are unchanged (tests/
test_telegram_redelivery.py, which also shows them).

"No row exists for an update that never reached a claim" is the one thing this cannot see: such
an update is a new message for whoever the account is tied to when it comes. The tests say so.
"""

import asyncio
import logging
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import sqlalchemy as sa

from house_helper.persistence import telegram as store
from house_helper.persistence import turns
from house_helper.persistence.allowlist import allow
from house_helper.persistence.database import create_db_engine, create_session_factory
from house_helper.persistence.models import Conversation, Message, MessageAttachment, TelegramLink
from house_helper.web import telegram_chat as tc
from house_helper.web.turn_runner import TELEGRAM_CHANNEL, TurnNotAuthorized
from tests.backend_support import sqlite_only
from tests.chat_support import ScriptedBackend, error_of, log_text, reached
from tests.persistence_support import new_user, utc
from tests.photo_support import jpeg
from tests.postgres_harness import (
    HELD_UNCONFIRMED_WORKER_EXIT,
    POSTGRES_URL_ENV,
    RELEASED_SPAWN_NEVER_ATTEMPTED,
    RELEASED_WORKER_EXIT_CONFIRMED,
    Expected,
    Run,
    Scratch,
    UnsafeTarget,
    guard_engine,
    validate_target,
)
from tests.telegram_support import MOTHER, TelegramHarness, photo_message, private_message, telegram_app
from tests.test_admin_enrollment_postgres import (  # noqa: F401  (fixtures)
    monitor_rc,
    run_on,
    scratch_rc,
)
from tests.test_admin_postgres import pg_run, scalar, template  # noqa: F401  (fixtures, helper)
# R7: the offline server the harness itself is tested against, so the held database's
# protection is proved against a real `Run` rather than a stand-in.
from tests.test_admin_postgres_harness import BASE, TOKEN, FakeServer, cluster_identity
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

QUESTION = "Что приготовить из кабачков? (секрет первой: аллергия на орехи)"
UPDATE = 7345
NOTHING_STARTS_WITHIN = 0.2


@contextmanager
def unrecorded_failure(monkeypatch):
    """The process goes away before it can write that the attempt failed: the request stays as
    its claim left it (PROCESSING, with a lease)."""
    with monkeypatch.context() as patch:
        patch.setattr(turns, "fail_turn", lambda *args, **kwargs: False)
        yield


def second_person(t: TelegramHarness):
    return next(iter(t.chat.extra_users.values()))


async def hand_over(t: TelegramHarness, other) -> None:
    """The account is unlinked from the first person and linked to the second."""
    await t.service.unlink(t.user_id)
    await t.pair(MOTHER, client=other)
    assert [link.user_id for link in t.links()] == [second_person(t)]


def nothing_of_the_first_owner_reached_the_second(t: TelegramHarness) -> None:
    """The second person has no conversation, message, request or photo, and the first person's
    history is as it was."""
    second = second_person(t)
    assert t.conversations(second) == []
    owned = {c.id for c in t.chat.rows(Conversation)}
    assert owned == {c.id for c in t.conversations(t.user_id)}
    assert all(row.user_id != second for row in t.chat.rows(MessageAttachment))


def fetches(t: TelegramHarness) -> int:
    return len([r for r in t.bot.requests if r.url.path.endswith("/getFile") or r.url.path.startswith("/file/")])


# --- the review's scenario, in each state the first attempt can have left ------------------------------------------


async def test_a_completed_update_redelivered_after_the_account_changed_hands_is_dropped(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    backend = ScriptedBackend("Икра из кабачков.", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        update = private_message(UPDATE, MOTHER, QUESTION)
        await t.deliver_update(update)
        [conversation] = t.conversations()
        history = t.stored_texts(conversation.id)
        await hand_over(t, other)
        before = len(t.bot.sent)

        await t.deliver_update(update)
        await t.settle()

        assert backend.calls == 1  # no provider call
        assert t.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]  # fixed words; not the answer, not the question
        assert t.stored_texts(conversation.id) == history  # the first owner's history is as it was
        [turn] = t.all_turns()
        assert turn.status == "completed" and turn.conversation_id == conversation.id  # ... and its owner is unchanged
        nothing_of_the_first_owner_reached_the_second(t)
        assert t.service._chat_locks == {} and t.service._binding_locks == {}

    log = log_text(caplog)
    assert "admitted for another account" in log
    assert "кабачк" not in log and "орех" not in log and str(UPDATE) not in log


async def test_a_timed_out_update_redelivered_after_the_account_changed_hands_is_dropped_not_reported_as_not_run_again(
    tmp_path,
):
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        update = private_message(UPDATE, MOTHER, QUESTION)
        await t.deliver_update(update)  # the first owner's attempt fails
        [turn_before] = t.all_turns()
        assert (turn_before.status, turn_before.attempt_count) == ("failed", 1)
        await hand_over(t, other)
        before = len(t.bot.sent)

        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]  # neither NOT_RUN_AGAIN nor a failure notice
        [turn] = t.all_turns()
        assert (turn.id, turn.status, turn.attempt_count, turn.error_code) == (
            turn_before.id,
            "failed",
            1,
            "provider_timeout",
        )
        assert [m.role for m in t.all_messages()] == ["user"]  # the first owner's own, nothing added or invented
        nothing_of_the_first_owner_reached_the_second(t)


@pytest.mark.parametrize("seconds", [30, 200], ids=["lease-live", "lease-expired"])
async def test_an_update_left_in_flight_by_a_crash_is_dropped_for_the_new_owner_whatever_the_lease_says(
    tmp_path, monkeypatch, seconds
):
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        update = private_message(UPDATE, MOTHER, QUESTION)
        with unrecorded_failure(monkeypatch):
            await t.deliver_update(update)
        [turn_before] = t.all_turns()
        assert (turn_before.status, turn_before.attempt_count) == ("processing", 1)
        await hand_over(t, other)
        t.chat.clock.advance(seconds=seconds)
        before = len(t.bot.sent)

        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]  # neither STILL_BUSY nor NOT_RUN_AGAIN
        [turn] = t.all_turns()
        assert (turn.id, turn.status, turn.attempt_count) == (turn_before.id, "processing", 1)  # not touched
        nothing_of_the_first_owner_reached_the_second(t)


async def test_a_photo_update_redelivered_after_the_account_changed_hands_is_neither_fetched_nor_stored_again(tmp_path):
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        t.bot.files["photo-1"] = ("photos/photo-1.jpg", jpeg(4000, "o"))
        update = photo_message(UPDATE, MOTHER, "photo-1", caption="Что это?")
        await t.deliver_update(update)
        assert fetches(t) == 2 and len(t.chat.rows(MessageAttachment)) == 1
        await hand_over(t, other)
        before = len(t.bot.sent)

        await t.deliver_update(update)

        assert fetches(t) == 2  # not a second getFile, not a second download
        assert [row.user_id for row in t.chat.rows(MessageAttachment)] == [t.user_id]  # the first owner's, still
        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]
        nothing_of_the_first_owner_reached_the_second(t)


# --- what is unchanged -----------------------------------------------------------------------------------------


async def test_the_same_owner_linked_again_still_gets_the_replay_of_a_completed_update(tmp_path):
    backend = ScriptedBackend("Икра из кабачков.", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(UPDATE, MOTHER, QUESTION)
        await t.deliver_update(update)
        await t.service.unlink(t.user_id)
        await t.pair(MOTHER)  # the same person, the same account, a new link

        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[-1] == "Икра из кабачков."  # answered from the first completion, as before
        assert [turn.status for turn in t.all_turns()] == ["completed"]


async def test_the_same_owner_linked_again_still_gets_the_refusal_of_an_attempt_that_did_not_complete(tmp_path):
    backend = ScriptedBackend(TimeoutError(), "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(UPDATE, MOTHER, QUESTION)
        await t.deliver_update(update)
        await t.service.unlink(t.user_id)
        await t.pair(MOTHER)

        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[-1] == tc.NOT_RUN_AGAIN
        assert [(turn.status, turn.attempt_count) for turn in t.all_turns()] == [("failed", 1)]


async def test_an_update_that_never_reached_a_claim_is_a_new_message_for_whoever_the_account_is_tied_to_then(tmp_path):
    """The honest limit: nothing was written for it, so nothing can be said about it. (A dropped
    update is a new message; the first person's words were never kept, so nothing of theirs is
    in the second person's history that was not typed to the second person's account.)"""
    backend = ScriptedBackend("Ответ второму")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        update = private_message(UPDATE, MOTHER, "Привет")
        await t.deliver_update(update)  # the account is nobody's: the stranger's notice, no claim
        assert t.all_turns() == []
        await t.pair(MOTHER, client=other)

        await t.deliver_update(update)

        assert backend.calls == 1 and t.bot.texts(MOTHER)[-1] == "Ответ второму"
        assert [turn.conversation_id for turn in t.all_turns()] == [t.conversations(second_person(t))[0].id]


async def test_the_websites_own_retry_is_unchanged_and_its_keys_do_not_clash_with_telegrams(tmp_path):
    backend = ScriptedBackend(TimeoutError(), "Ответ после повтора", "Ответ второму", "Ответ из Telegram")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        conversation = await t.chat.create()
        failed = await t.chat.send(conversation, QUESTION, "web-1")
        retry = await t.chat.send(conversation, QUESTION, "web-1")  # the person pressed "send again"
        assert failed.status_code == 504 and retry.status_code == 201
        assert retry.json()["assistant_message"]["text"] == "Ответ после повтора"

        # The second person types the same text on the page, with their own key: nothing to do with updates.
        theirs = await t.chat.create(client=other)
        again = await t.chat.send(theirs, QUESTION, "web-1", client=other)
        assert again.status_code == 201
        await t.settle()

        await t.deliver_update(private_message(UPDATE, MOTHER, "Новое сообщение"))  # and Telegram still works
        assert backend.calls == 4 and t.bot.texts(MOTHER)[-1] == "Ответ из Telegram"


# --- the website cannot make up a key that stands for somebody else's update ----------------------------------------


async def test_the_website_refuses_the_telegram_prefix_in_an_idempotency_key(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        conversation = await t.chat.create()

        refused = await t.chat.send(conversation, "Привет", f"tg-{UPDATE}")
        allowed = await t.chat.send(conversation, "Привет", "web-ok")

        assert refused.status_code == 400 and error_of(refused)["detail"] == "invalid_idempotency_key"
        assert allowed.status_code == 201
        assert t.all_turns()[0].idempotency_key == "web-ok" and len(t.all_turns()) == 1


async def test_a_key_made_up_on_the_website_cannot_make_the_bot_refuse_a_real_update(tmp_path):
    backend = ScriptedBackend("Ответ первой")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        theirs = await t.chat.create(client=other)
        poisoned = await t.chat.send(theirs, "Привет", f"tg-{UPDATE}", client=other)  # the second person tries
        await t.pair(MOTHER)

        await t.deliver_update(private_message(UPDATE, MOTHER, QUESTION))

        assert poisoned.status_code == 400
        assert backend.calls == 1 and t.bot.texts(MOTHER)[-1] == "Ответ первой"


# --- the claim alone is enough --------------------------------------------------------------------------------------


async def test_the_claim_refuses_an_update_of_another_owner_even_when_the_look_before_it_was_wrong(tmp_path, monkeypatch):
    """The look before the claim is for the common case. What decides is the claim's own transaction,
    so even a look that finds nothing (here: one that is made blind) leaves the assistant unasked and
    the first owner's words out of the second person's history."""
    backend = ScriptedBackend("Икра из кабачков.", "Этот ответ запрашивать не следовало")

    class BlindLook:
        """persistence.telegram, except that the look before the claim sees nothing."""

        def __init__(self, real):
            self._real = real

        def update_admitted_for_another(self, *args, **kwargs):
            return False

        def __getattr__(self, name):
            return getattr(self._real, name)

    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        t.bot.files["photo-1"] = ("photos/photo-1.jpg", jpeg(4000, "p"))
        update = photo_message(UPDATE, MOTHER, "photo-1", caption="Что это?")
        await t.deliver_update(update)
        await hand_over(t, other)
        before = len(t.bot.sent)
        monkeypatch.setattr(tc, "store", BlindLook(store))

        await t.deliver_update(update)
        await t.settle()

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]
        assert len(t.all_turns()) == 1  # the second person's claim was rolled back with its message
        second = second_person(t)
        assert [m for m in t.all_messages() if m.conversation_id in {c.id for c in t.conversations(second)}] == []
        # The photo the blind look let the bot fetch for the second person was taken off again.
        assert [row.user_id for row in t.chat.rows(MessageAttachment)] == [t.user_id]


@pytest.mark.sqlite
@sqlite_only
async def test_two_claims_for_one_update_by_two_owners_are_serialised_and_the_second_is_refused(tmp_path):
    """SQLite-scoped: the concurrency of the claim itself, below the bot, relies on SQLite's
    global writer lock (the claim's first statement is the conversation write). PostgreSQL's
    equivalent guarantee is the share lock on the link plus the ledger's primary key, whose
    cross-owner case is the separate PostgreSQL test below."""
    backend = ScriptedBackend("Ответ первой", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        runner = t.chat.app.state.chat
        first_conversation = await t.chat.create()
        second_conversation = await t.chat.create(client=other)
        second = second_person(t)
        entered, proceed = threading.Event(), threading.Event()

        def hold_the_claim_open(session, started):
            entered.set()
            assert proceed.wait(TIMEOUT), "the test never let the first claim go on"
            return True

        def submit(user_id, conversation_id, authorize):
            return asyncio.ensure_future(
                runner.submit(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    idempotency_key=store.update_key(UPDATE),
                    text="Одно и то же сообщение",
                    correlation_id="hh-0123456789abcdef",
                    route="test",
                    origin=TELEGRAM_CHANNEL,
                    reclaim=False,
                    authorize=authorize,
                )
            )

        try:
            first = submit(t.user_id, uuid_of(first_conversation), hold_the_claim_open)
            await reached(entered)
            rival = submit(
                second,
                uuid_of(second_conversation),
                lambda session, started: not store.update_admitted_for_another(
                    session, second, UPDATE
                ),
            )
            await asyncio.sleep(NOTHING_STARTS_WITHIN)  # (the absence of an event can only be given time)
            assert not rival.done()  # it cannot even claim while the first claim is open
        finally:
            proceed.set()

        assert (await asyncio.wait_for(first, TIMEOUT)).completed.assistant_message.text == "Ответ первой"
        with pytest.raises(TurnNotAuthorized):
            await asyncio.wait_for(rival, TIMEOUT)

        assert backend.calls == 1
        assert [turn.idempotency_key for turn in t.all_turns()] == [store.update_key(UPDATE)]  # one request, the first owner's
        assert [m.conversation_id for m in t.all_messages()] == [uuid_of(first_conversation)] * 2


def uuid_of(text: str) -> UUID:
    return UUID(text)


# --- PostgreSQL: the ledger marker is the bot's, not the owner's -----------------------------------

NOW = utc(2026, 11, 15, 12)
CROSS_OWNER_BOT = 123456789
CROSS_OWNER_ACCOUNT = 555000111
CROSS_OWNER_UPDATE = 8801


def _owner_with_link(scratch, *, subject):
    def work(db):
        user = new_user(db, display_name="Owner")
        allow(db, user, "telegram", subject)
        db.add(
            TelegramLink(
                user_id=user.id,
                telegram_user_id=CROSS_OWNER_ACCOUNT,
                created_at=NOW,
            )
        )
        return user.id

    return run_on(scratch, work)


def _hand_the_account_over(scratch, previous_owner, *, subject):
    """Unlink the account from its first owner and link it to a second (one transaction)."""

    def work(db):
        link = db.scalar(
            sa.select(TelegramLink).where(TelegramLink.user_id == previous_owner)
        )
        db.delete(link)
        db.flush()  # the DELETE is issued before the new link's INSERT
        user = new_user(db, display_name="Second owner")
        allow(db, user, "telegram", subject)
        db.add(
            TelegramLink(
                user_id=user.id,
                telegram_user_id=CROSS_OWNER_ACCOUNT,
                created_at=NOW,
            )
        )
        db.flush()
        return user.id

    return run_on(scratch, work)


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL "
    "(see tests/test_admin_postgres.py)",
)
async def test_the_shared_admission_marker_refuses_the_same_update_for_a_second_owner(
    scratch_rc, monitor_rc
):
    """Cross-owner isolation through the real ledger, on a real server. Owner A admits update
    U and commits a marker, but no turn request survives (its conversation is gone). The
    account is handed to owner B. B asks `store.admit_update` in its own transaction: the
    marker is the bot's, not A's, so B is refused ALREADY_ADMITTED and can never process the
    update. The same-owner PostgreSQL concurrency test proves deduplication only; this one
    proves the marker is shared across owners."""
    first_owner = _owner_with_link(scratch_rc, subject="sub-first")
    admitted = run_on(
        scratch_rc,
        lambda db: store.admit_update(
            db,
            CROSS_OWNER_ACCOUNT,
            first_owner,
            CROSS_OWNER_UPDATE,
            bot_id=CROSS_OWNER_BOT,
            now=NOW,
            existing=False,
        ),
    )
    assert admitted is store.AdmissionOutcome.ADMITTED

    second_owner = _hand_the_account_over(scratch_rc, first_owner, subject="sub-second")
    # Nothing of A's request survives: only the marker speaks for the update now.
    status = run_on(
        scratch_rc,
        lambda db: store.update_admission_status(
            db,
            CROSS_OWNER_BOT,
            second_owner,
            CROSS_OWNER_UPDATE,
            store.update_key(CROSS_OWNER_UPDATE),
        ),
    )
    assert status is store.AdmissionStatus.ADMITTED_GONE

    rival = run_on(
        scratch_rc,
        lambda db: store.admit_update(
            db,
            CROSS_OWNER_ACCOUNT,
            second_owner,
            CROSS_OWNER_UPDATE,
            bot_id=CROSS_OWNER_BOT,
            now=NOW,
            existing=False,
        ),
    )
    assert rival is store.AdmissionOutcome.ALREADY_ADMITTED

    assert (
        scalar(
            monitor_rc,
            "SELECT count(*) FROM telegram_update_admissions "
            "WHERE bot_id = :bot AND update_id = :update",
            bot=CROSS_OWNER_BOT,
            update=CROSS_OWNER_UPDATE,
        )
        == 1
    )
    assert scalar(monitor_rc, "SELECT count(*) FROM turn_requests") == 0  # neither owner processed it


def _owner_without_link(scratch, *, subject):
    """A second person who is enrolled for Telegram but whose account is not linked yet: the
    state a handover starts from."""

    def work(db):
        user = new_user(db, display_name="Second owner")
        allow(db, user, "telegram", subject)
        return user.id

    return run_on(scratch, work)


def _wait_until(condition, *, timeout=60, what="the condition"):
    """Deterministic-enough polling with a bounded deadline: the only unbounded thing is the
    assertion that names what never happened."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"{what} did not happen within {timeout}s")


def _waiting_on_a_lock(engine, pid):
    """True when the backend `pid` is blocked: either lock manager waiting (pg_locks records
    a not-granted request) or the distinct row-lock wait reported by pg_stat_activity."""
    blocked = scalar(
        engine, "SELECT count(*) FROM pg_locks WHERE pid = :pid AND NOT granted", pid=pid
    )
    if blocked:
        return True
    return (
        scalar(
            engine,
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid",
            pid=pid,
        )
        == "Lock"
    )


# A budget below PostgreSQL's own limits: the application login's role settings are
# lock_timeout = 5s, statement_timeout = 30s and idle_in_transaction_session_timeout = 60s
# (tests/postgres_harness.py). The handover gives itself a longer lock_timeout so that the
# test, not the role's 5 s, decides when the wait ends; every wait below is shorter than the
# lock_timeout it depends on, and the whole test stays well inside the idle limit.
OBSERVE_WITHIN = 10  # seconds to see the handover's DELETE blocked
HANDOVER_WITHIN = 30  # seconds for the handover to begin and to complete after the release
HANDOVER_LOCK_TIMEOUT = "20s"
HANDOVER_STATEMENT_TIMEOUT = "30s"


def _lock_wait_event(engine, pid):
    """The pg_stat_activity wait event of `pid`, or None when it is not waiting. A row-lock
    conflict with another transaction waits on that transaction's id: `transactionid`."""
    return scalar(
        engine, "SELECT wait_event FROM pg_stat_activity WHERE pid = :pid", pid=pid
    )


# --- the handover worker's lifecycle, in one place ---------------------------------------------------------------
#
# The worker's connection must be gone before the `scratch_rc` fixture drops the database.
# A reported failure does not stop pytest teardown: `pg_run.drop_database()` runs after a
# failing test exactly as after a passing one, and it ends in `DROP DATABASE ... WITH
# (FORCE)`. A thread cannot be killed, so a worker that will not finish inside this process
# could still be holding a connection when that statement runs. The worker therefore runs in
# a process of its own, whose exit this test waits for -- terminating it and confirming the
# exit when it has to -- before anything it used is cleaned up.

logger = logging.getLogger(__name__)

# A wait that keeps failing is not an exit. Each wait phase is given a fixed number of
# attempts, so a broken or always-raising wait is reported instead of being swallowed
# forever: the cleanup path has to stay finite and auditable.
HANDOVER_WAIT_ATTEMPTS = 3


class HandoverProcessNotStopped(RuntimeError):
    """The worker's process could not be confirmed exited. Nothing it used may be cleaned up,
    and the call must not return as though the exit had happened: a process may still be
    writing its files or holding its connection.

    `worker_pid` is the process id when the raiser still knew it -- including the post-spawn
    path, where no wrapper is returned -- so the retained database's record can name it."""

    def __init__(self, message, *, interruption=None, worker_pid=None):
        super().__init__(message)
        self.interruption = interruption
        self.worker_pid = worker_pid


def _say(level, message, *args, exc_info=None) -> None:
    """A diagnostic that decides nothing: one that raises is dropped. Terminating the worker,
    waiting for its exit and the order of the cleanup must never depend on a logger."""
    try:
        logger.log(level, message, *args, exc_info=exc_info)
    except BaseException:  # noqa: BLE001 - logging is never part of the lifecycle
        pass


def _report_secondary(what, failure) -> None:
    """A failure met while cleaning up is reported and never raised: the failure the test is
    already reporting has to stay the one that explains the run. pytest shows this line in a
    failing test's captured log."""
    _say(logging.WARNING, "%s: %s", what, failure, exc_info=failure)


def _release_the_admission(holder) -> None:
    """Publish A's marker and release its share lock on the link: the commit is the release,
    which is what lets the handover's DELETE proceed. The rollback after a commit is a no-op
    and the second chance after a failed one; closing the session ends the transaction either
    way.

    Every step is attempted whatever the previous one did, and the FIRST failure is the one
    raised: a rollback or a close that also fails is reported, never raised in its place, so
    the run is explained by what went wrong first rather than by whatever failed last."""
    first = None
    try:
        holder.commit()
    except BaseException as problem:  # noqa: BLE001 - raised below, as the first failure
        first = problem
    for what, cleanup in (
        ("the rollback after the release", holder.rollback),
        ("closing the admission session", holder.close),
    ):
        try:
            cleanup()
        except BaseException as problem:  # noqa: BLE001 - reported, never over the first
            if first is None:
                first = problem
            else:
                _report_secondary(f"{what} failed too", problem)
    if first is not None:
        raise first


class _Admission:
    """The first owner's admission transaction, released at most once.

    Its release is the handover worker's precondition: the DELETE waits on the share lock
    until this transaction is gone."""

    def __init__(self, holder):
        self._holder = holder
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        _release_the_admission(self._holder)
        self.released = True

    def release_if_the_test_is_already_failing(self) -> None:
        """For a path where a failure is already being reported: a release that fails here
        must not become the diagnostic."""
        if self.released:
            return
        try:
            self.release()
        except BaseException as problem:  # noqa: BLE001 - reported, deliberately not raised
            _report_secondary("the admission transaction could not be released", problem)


def _dispose_quietly(engine) -> None:
    """Closing the engine after the worker has been joined. Its failure is reported for the
    same reason as any other cleanup failure: it is not the test's diagnostic."""
    try:
        engine.dispose()
    except BaseException as problem:  # noqa: BLE001 - reported, deliberately not raised
        _report_secondary("the admission engine could not be disposed", problem)


class _HandoverProcess:
    """The worker's process, and the exit that has to be confirmed before anything it used
    is cleaned up. `process` is a `subprocess.Popen` in the real test and a stub where an
    injection needs one; the lifecycle only asks it to wait, to be killed and whether it is
    still running."""

    def __init__(self, process, *, directory, pidfile=None):
        self.process = process
        self.directory = directory
        self.pidfile = pidfile

    def wait(self, *, timeout=None):
        return self.process.wait(timeout=timeout)

    def kill(self) -> None:
        self.process.kill()

    def exited(self) -> bool:
        return self.process.poll() is not None

    def output(self) -> str:
        """What the worker's process wrote: the diagnostic when it failed."""
        try:
            return (self.directory / "handover.log").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return ""

    def close(self) -> None:
        """Its temporary files -- only after `_stop_the_handover` confirmed the exit."""
        shutil.rmtree(self.directory, ignore_errors=True)


def _end_the_process(process) -> int:
    """Terminate a process nothing else owns, and return its exit status only once the OS
    reports that it ended.

    Nothing is logged before the exit is known: a diagnostic must not be able to decide
    whether the termination or the wait happens. A kill that fails is followed by the wait
    all the same, and an interruption of a wait is never taken for an ending. When the exit
    still cannot be confirmed this raises `HandoverProcessNotStopped` -- it never returns as
    though the process were gone."""
    diagnostics = []
    try:
        if process.poll() is None:
            process.kill()
    except BaseException as problem:  # noqa: BLE001 - the wait below is the evidence
        diagnostics.append(("the handover process could not be signalled", problem))

    exit_code = None
    interruption = None
    for _ in range(HANDOVER_WAIT_ATTEMPTS):
        try:
            exit_code = process.wait()
            break
        except BaseException as problem:  # noqa: BLE001 - a failed wait is not an exit
            if interruption is None:
                interruption = problem
            diagnostics.append(
                ("waiting for the terminated process was interrupted", problem)
            )

    for what, problem in diagnostics:  # deferred until the outcome is known
        _report_secondary(what, problem)

    if exit_code is None:
        raise HandoverProcessNotStopped(
            f"the handover process did not confirm its exit within "
            f"{HANDOVER_WAIT_ATTEMPTS} waits",
            interruption=interruption,
            worker_pid=getattr(process, "pid", None),
        )
    return exit_code


class _Spawned:
    """The raw process between `Popen` and a `_HandoverProcess`, and whose job it is to end
    it.

    `mine` is the whole ownership state: while it is true this -- not a wrapper that may not
    exist yet -- has to end the process. `hand_over` is the one place it stops being ours,
    and it is the last thing done on the success path, so no failure before it (closing the
    log, building the wrapper, the transfer itself) can lose the process or let its files be
    removed under it."""

    def __init__(self, process):
        self.process = process
        self.mine = True

    def hand_over(self) -> None:
        self.mine = False

    def end(self) -> int:
        return _end_the_process(self.process)


def _open_the_worker_log(directory):
    """The worker's output file. A seam of its own: a close that fails after `Popen` has
    already created a process is exercised through it."""
    return open(directory / "handover.log", "wb")


def _close_the_log_without_a_process(log) -> None:
    """After a `Popen` that failed, no child exists: the log is ours alone to close, and a
    failure to close it is secondary to the start's failure."""
    try:
        log.close()
    except BaseException as problem:  # noqa: BLE001 - reported, never over the start's failure
        _report_secondary("closing the worker's log after a failed start", problem)


class _SpawnAttempt:
    """What the lifecycle positively knows about the spawn path, recorded before the attempt
    and only ever added to.

    `hold` is the `_Deferral` that owns the pre-armed database (or None for a bare record).
    Every change goes THROUGH the run first: `record()` asks the run to record the attempt
    before `Popen`, and `spawned(process)` asks it to associate the actual handle before any
    fallible post-spawn step. Only when the run accepted the evidence is it mirrored here, so
    a caller's local claim can never get ahead of the state the run will verify.

    `attempted` is set immediately BEFORE `Popen` is called: the call can raise after the
    operating system has already created the process, and then there is no handle to wait
    for -- so "the handle is missing" proves nothing, while "the attempt was recorded" is
    evidence that a worker may exist. `exit_code` is set only when a process this module
    owned was positively confirmed exited (the wait returned its status); an exception, a
    timeout or a failed wrapper is not a confirmation."""

    def __init__(self, hold=None):
        self._hold = hold
        self.attempted = False
        self.exit_code = None
        self.attempt_failure = None
        self.association_failure = None

    def record(self) -> None:
        if self._hold is not None:
            try:
                self._hold.record_attempt()
            except BaseException as problem:  # noqa: BLE001 - the spawn path stops here
                self.attempt_failure = problem
                raise
        self.attempted = True

    def spawned(self, process) -> None:
        """Bind the actual `Popen` handle to the held database, before the fallible steps
        that follow can lose ownership of it. The run is the owner of the handle; this
        record only mirrors that the binding was accepted."""
        if self._hold is not None:
            try:
                self._hold.record_process(process)
            except BaseException as problem:  # noqa: BLE001 - the run refused the evidence
                self.association_failure = problem
                raise

    def record_exit(self, exit_code) -> None:
        self.attempted = True
        self.exit_code = exit_code

    @property
    def exit_confirmed(self) -> bool:
        return self.exit_code is not None


def _spawn(command, *, directory, cwd=None, pidfile=None, attempt=None) -> _HandoverProcess:
    """Start the worker and hand its process to a `_HandoverProcess`, or leave nothing
    running.

    Ownership is explicit (`_Spawned`): from the moment `Popen` returns, this function is
    responsible for ending the process until `_HandoverProcess` owns it. The first thing done
    with the returned handle is to bind it to the held database (`attempt.spawned`), before
    closing the log, building the wrapper or transferring ownership -- each of which can
    fail, and none of which may leave the run unable to prove the process's exit. Any failure
    after the process exists terminates it and confirms its exit before the error propagates;
    the confirmed status is recorded on `attempt`, so the caller can release the pre-armed
    hold even though it never received a wrapper. `attempt.record()` runs BEFORE `Popen`: a
    `Popen` that raises may still have started a worker, and then the caller must keep the
    database held.

    Nothing here removes a file: the caller removes the directory only once this call
    returned a wrapper or raised with the exit confirmed (`HandoverProcessNotStopped` is the
    one exception, and it is the signal not to)."""
    log = _open_the_worker_log(directory)
    try:
        if attempt is not None:
            attempt.record()  # BEFORE the call: a raised Popen may have started a worker
        process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
    except BaseException:  # noqa: BLE001 - no handle exists; whether a worker does is unknown
        _close_the_log_without_a_process(log)
        raise

    spawned = _Spawned(process)  # the raw process is ours from this line
    failure = None
    try:
        if attempt is not None:
            # R7-01: the run gets the actual handle FIRST, so nothing below can lose it.
            attempt.spawned(process)
        log.close()
        child = _HandoverProcess(process, directory=directory, pidfile=pidfile)
        spawned.hand_over()  # the wrapper owns it now; the caller cleans up through it
        return child
    except BaseException as problem:  # noqa: BLE001 - the report, unless the exit is unconfirmed
        failure = problem
        raise
    finally:
        if spawned.mine:
            try:
                exit_code = spawned.end()
            except HandoverProcessNotStopped as not_stopped:
                # The caller must not remove the files of a process that may still be alive;
                # the start's own failure stays attached to this report.
                _report_secondary(
                    "the spawned worker may still be running; its files are left in place",
                    not_stopped,
                )
                raise not_stopped from failure
            if attempt is not None:
                # R7: the exit WAS confirmed (the wait returned its status) even though no
                # wrapper was handed over; the pre-armed hold may be released on it.
                attempt.record_exit(exit_code)


def _expected_for(scratch) -> Expected:
    """The connection expectation the harness builds for its own engines, so the worker's
    process is verified -- database, marker, role, cluster -- before its first statement,
    like every other connection of a run (R4-01)."""
    run = scratch.run
    cluster = run.verified_cluster().system_identifier
    if run.roles is None:
        return Expected(
            run.target, scratch.name, run.marker, cluster, None, run.target.username
        )
    return Expected(
        run.target,
        scratch.name,
        run.marker,
        cluster,
        run.identity("app"),
        run.identity("owner").name,
    )


# The child's whole program: put this checkout first, then hand the pickled configuration
# (the scratch database's URL and expectation, both of them this run's) to the handover.
_HANDOVER_CHILD = (
    "import pickle, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from tests.test_telegram_cross_owner import _hand_over_the_account\n"
    "with open(sys.argv[2], 'rb') as configuration:\n"
    "    _hand_over_the_account(pickle.load(configuration))\n"
)

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def _publish_the_pid(pidfile, pid) -> None:
    """Publish the backend pid atomically, so the observing test can never read the pidfile
    while it is still empty.

    `Path.write_text` creates (or truncates) the file first and writes into it second; an
    observer that looks for the file between those two steps can see it while it is still
    empty and parse an empty string (the observed CI flake). A sibling temporary file
    renamed into place publishes the name and the complete contents together: the pidfile
    exists only once it holds the full pid."""
    pidfile = Path(pidfile)
    partial = pidfile.with_name(pidfile.name + ".partial")
    partial.write_text(str(pid), encoding="ascii")
    os.replace(partial, pidfile)


def _published_pid(pidfile):
    """The worker's backend pid once its publication is complete, or None while it is not:
    the file may be absent, or (on Windows) briefly unopenable while the rename into place
    is still in flight. A pidfile that exists but does not carry a complete pid is not a
    publication, so the observer waits for this value instead of for the file's existence."""
    try:
        return int(Path(pidfile).read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


def _hand_over_the_account(configuration) -> None:
    """The worker's own process: the transaction the test used to run in a thread -- the
    DELETE that waits on A's share lock, the new link, the commit -- on an engine guarded
    exactly as the harness guards every other connection, with the transaction budgets the
    role would otherwise decide."""
    engine = guard_engine(create_db_engine(configuration["url"]), configuration["expected"])
    try:
        with create_session_factory(engine)() as db, db.begin():
            pid = db.connection().exec_driver_sql("SELECT pg_backend_pid()").scalar()
            _publish_the_pid(configuration["pidfile"], pid)
            # Budgets of this transaction, below the observation and result deadlines.
            db.execute(sa.text(f"SET LOCAL lock_timeout = '{HANDOVER_LOCK_TIMEOUT}'"))
            db.execute(
                sa.text(f"SET LOCAL statement_timeout = '{HANDOVER_STATEMENT_TIMEOUT}'")
            )
            link = db.scalar(
                sa.select(TelegramLink).where(
                    TelegramLink.user_id == UUID(configuration["first_owner"])
                )
            )
            db.delete(link)
            db.flush()  # the DELETE must wait on A's share lock
            db.add(
                TelegramLink(
                    user_id=UUID(configuration["second_owner"]),
                    telegram_user_id=CROSS_OWNER_ACCOUNT,
                    created_at=NOW,
                )
            )
            db.flush()
    finally:
        engine.dispose()


def _remove_the_worker_directory(directory) -> None:
    """Remove the worker's temporary files, and let no failure of the removal replace the
    failure that brought us here. The decision to call this has already been made from the
    lifecycle evidence; this function cannot change it."""
    try:
        shutil.rmtree(directory, ignore_errors=True)
    except BaseException as problem:  # noqa: BLE001 - reported, never the report itself
        _report_secondary(
            "the worker's temporary directory could not be removed", problem
        )


def _start_the_handover(scratch, *, first_owner, second_owner, attempt=None) -> _HandoverProcess:
    """Start the worker's process. Its configuration is a file only this user can read: it
    carries the run's password, which is why it is not an argument nor an environment
    entry. `attempt` is the lifecycle's record of the spawn path: it is marked inside
    `_spawn` immediately before `Popen`, so a failure to write the configuration first is
    positively a failure with no worker behind it.

    R7-01 (P2): the directory carries the run's password, so it is removed only when the
    failure is positively BEFORE any spawn attempt (the attempt record is unset) or when the
    process's exit was positively confirmed (`_spawn` records that status only after a
    completed wait). An attempt that may have created a process without returning a handle
    keeps the directory: an absent handle is not an absent process. The decision is computed
    from the attempt record before any diagnostic is emitted, and neither a logger nor a
    removal failure can change it or displace the original exception."""
    evidence = attempt if attempt is not None else _SpawnAttempt()
    directory = Path(tempfile.mkdtemp(prefix="house-helper-handover-"))
    try:
        pidfile = directory / "backend.pid"
        configuration_path = directory / "handover.configuration"
        with open(configuration_path, "wb") as configuration:
            pickle.dump(
                {
                    "url": scratch.url_text,
                    "expected": _expected_for(scratch),
                    "first_owner": str(first_owner),
                    "second_owner": str(second_owner),
                    "pidfile": str(pidfile),
                },
                configuration,
            )
        os.chmod(configuration_path, 0o600)
        return _spawn(
            [
                sys.executable,
                "-c",
                _HANDOVER_CHILD,
                str(_REPOSITORY_ROOT),
                str(configuration_path),
            ],
            directory=directory,
            cwd=_REPOSITORY_ROOT,
            pidfile=pidfile,
            attempt=evidence,
        )
    except HandoverProcessNotStopped:  # a process may still use these files
        # `_spawn` confirms any process it started before it raises; this is the one report
        # that says it could not. The directory carries the run's password, so it stays.
        raise
    except BaseException:  # noqa: BLE001 - decide from the evidence, not from the failure
        if evidence.attempted and not evidence.exit_confirmed:
            _say(
                logging.CRITICAL,
                "the worker's temporary directory is retained at %s: a spawn attempt was "
                "recorded and no process exit was confirmed",
                directory,
            )
            raise  # the original exception, over a directory a process may still be using
        _remove_the_worker_directory(directory)
        raise


class HandoverProcessFailed(RuntimeError):
    """The worker's process ended on its own with a non-zero status. Its output is the
    diagnostic, captured when the failure is recognised so that it survives the temporary
    files being removed."""

    def __init__(self, exit_code, output):
        super().__init__(f"the handover process exited with status {exit_code}")
        self.exit_code = exit_code
        self.output = output


def _wait_for_the_handover(child, *, budget) -> None:
    """The handover finished by itself: its process exited zero. A worker that overran is not
    waited for here -- `_finish_handover` terminates it once this raises."""
    exit_code = child.wait(timeout=budget)  # `TimeoutExpired` when it overran the budget
    if exit_code != 0:
        raise HandoverProcessFailed(exit_code, child.output())


class _Stopped:
    """How the worker's process ended: its status, whether this test had to end it, and the
    first interruption met while waiting (reported, never dropped)."""

    def __init__(self, exit_code=None, *, terminated=False, interruption=None):
        self.exit_code = exit_code
        self.terminated = terminated
        self.interruption = interruption


def _worker_pid(child, unconfirmed):
    """The worker's process id, when the lifecycle still knows it: from the wrapper, or from
    the unconfirmed exit a start raised after `Popen` had already created the process.

    R7: recorded with the held database so a recovery can name the process that may still be
    holding the connection. None when nothing knows it -- a guess is not recorded."""
    if child is not None:
        return getattr(child.process, "pid", None)
    if isinstance(unconfirmed, HandoverProcessNotStopped):
        return unconfirmed.worker_pid
    return None


def _stop_the_handover(child, *, budget) -> _Stopped:
    """Return only once the worker's process has exited, and keep the first interruption met
    on the way.

    The wait for the process's own exit is bounded by `budget`. The wait for a process this
    function has terminated blocks until the exit -- it is NOT bounded on purpose: a test
    that returned while the worker was alive would let the fixture drop the scratch database
    under it. Diagnostics are collected, never emitted, until the outcome is known, so
    neither logging nor an interruption of a wait can skip the termination: an interruption
    is recorded and the wait is made again (a fixed number of times -- a wait that keeps
    failing is not an ending), and even a failed termination is followed by the wait. When
    the exit still cannot be confirmed this raises `HandoverProcessNotStopped` instead of
    returning: the caller must not clean up anything the process may still be using."""
    if child is None:  # nothing was started: the failure happened before the process existed
        return _Stopped()

    interruption = None
    diagnostics = []

    def remember(what, problem):
        nonlocal interruption
        if interruption is None:
            interruption = problem
        diagnostics.append((what, problem))

    def report():
        for what, problem in diagnostics:  # deferred until the outcome is known
            _report_secondary(what, problem)
        diagnostics.clear()

    # 1. The process's own exit, within the budget. Nothing is logged here: a diagnostic
    #    must not decide whether the termination below happens.
    deadline = time.monotonic() + budget
    for _ in range(HANDOVER_WAIT_ATTEMPTS):
        try:
            exit_code = child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            break
        except BaseException as problem:  # noqa: BLE001 - recorded; never an ending
            remember("waiting for the handover process was interrupted", problem)
            if time.monotonic() >= deadline:
                break
        else:
            report()
            return _Stopped(exit_code, interruption=interruption)

    # 2. It did not end by itself in time, or the wait could not be trusted: end it. A kill
    #    that fails is followed by the wait all the same.
    try:
        child.kill()
    except BaseException as problem:  # noqa: BLE001 - the wait below is the guarantee
        remember("the handover process could not be signalled", problem)

    # 3. The wait that confirms it, with a bound on how often a failing wait is retried.
    exit_code = None
    for _ in range(HANDOVER_WAIT_ATTEMPTS):
        try:
            exit_code = child.wait()
        except BaseException as problem:  # noqa: BLE001 - a failed wait is not an exit
            remember("waiting for the terminated process was interrupted", problem)
        else:
            break

    report()

    if exit_code is None:
        raise HandoverProcessNotStopped(
            f"the handover process did not confirm its exit within "
            f"{HANDOVER_WAIT_ATTEMPTS} waits; nothing it used is cleaned up",
            interruption=interruption,
            worker_pid=_worker_pid(child, None),
        )

    _say(
        logging.CRITICAL,
        "the handover process did not exit within %ss; terminating it and waiting for its "
        "exit before anything it used is cleaned up",
        budget,
    )
    _say(logging.CRITICAL, "the handover process ended (exit status %s)", exit_code)
    return _Stopped(exit_code, terminated=True, interruption=interruption)


# --- R7: the database a worker that would not exit leaves behind ------------------------------------------------
#
# `scratch_rc`'s teardown drops the database after a failing test exactly as after a passing
# one, and the session's `pg_run` finalizer drops what is left as well. That is safe only
# while the worker's process is confirmed gone. When its exit cannot be confirmed at all, the
# database it may still be using is HELD instead (tests/postgres_harness.py): registered with
# the very `Run` that owns it, so both the function-level fixture and the session finalizer
# refuse to drop it, its roles and its grants.


class HoldRegistrationFailed(RuntimeError):
    """The hold could not be armed before the worker's process was started. The spawn path is
    therefore never entered -- there is no worker to protect the database from -- and the run
    that owns it cleans it up as usual."""


class HoldReleaseFailed(RuntimeError):
    """A hold that was positively safe to release could not be released. The database stays
    held (the run refuses it, and the fixture refuses its drop), and the failure is
    reported: the protection is kept, never traded for a claim that it was removed."""


class _Deferral:
    """The scratch database the worker uses, HELD from before the spawn until its exit is
    positively confirmed.

    `arm()` registers the hold with the Run instance (`pg_run`) that created the database
    BEFORE the spawn path is entered: the protection exists before `Popen` can run, so no
    failure afterwards has to remember to register it. The session finalizer -- `Run.close()`
    -- and the fixture's `drop_database()` both refuse while the hold exists.

    `release()` is the only way the hold ends, and it requires evidence the Run itself
    verifies: the actual process handle it was given reports its exit, or the Run recorded no
    spawn attempt at all. The lifecycle's `record_attempt()` and `record_process()` are the
    two moments that evidence is handed over, in that order and before the matching spawn
    step. `must_drop()` is true only after such a release, or when the hold was never armed
    -- in which case `arm()` refused and the spawn path was never entered, so no worker
    exists."""

    def __init__(self, *, run, name):
        self.run = run
        self.name = name
        self.armed = False
        self.released = False
        self.record = None
        self.attempt_record = None
        self.arm_failure = None
        self.attempt_failure = None
        self.association_failure = None
        self.release_failure = None

    def arm(self):
        """Register HELD with the run that owns the database, once, before any spawn attempt.

        A failure is remembered (`arm_failure`) and raised as `HoldRegistrationFailed`; the
        caller must not enter the spawn path after that, and the run cleans the database up
        normally because no worker was ever attempted."""
        if self.armed or self.released:
            return self.record
        try:
            record = self.run.hold_database(
                self.name, reason=HELD_UNCONFIRMED_WORKER_EXIT
            )
        except BaseException as problem:  # noqa: BLE001 - reported, never a safe state
            self.arm_failure = problem
            raise HoldRegistrationFailed(
                f"the scratch database {self.name} could not be held before the worker was "
                "started; the worker was not started"
            ) from problem
        self.armed = True
        self.record = record
        return record

    def record_attempt(self):
        """Record with the Run, BEFORE `Popen`, that a spawn attempt is being made.

        A failure is reported to `_SpawnAttempt`, which does not enter `Popen` after it: the
        evidence is what authorizes the spawn path, not the other way round. The hold itself
        is unchanged -- it was armed before this call."""
        if not self.armed or self.released:
            raise HoldRegistrationFailed(
                f"the scratch database {self.name} is not held; no spawn attempt is recorded"
            )
        try:
            self.attempt_record = self.run.note_spawn_attempt(self.name)
        except BaseException as problem:  # noqa: BLE001 - the caller must not spawn
            self.attempt_failure = problem
            raise
        return self.attempt_record

    def record_process(self, process):
        """Associate the actual `Popen` handle with the hold, before any fallible step.

        A failure is returned to the caller by raising; the hold stays, and the Run has no
        evidence with which to release it, so the database remains protected."""
        if not self.armed or self.released:
            raise HoldRegistrationFailed(
                f"the scratch database {self.name} is not held; no process is associated"
            )
        try:
            self.record = self.run.note_held_process(self.name, process=process)
        except BaseException as problem:  # noqa: BLE001 - reported, never unholding
            self.association_failure = problem
            raise
        return self.record

    def note_worker(self, worker_pid):
        """Associate the process id that became known after arming. The run updates the hold
        in place -- it is never removed, not even for an instant. A failure is returned for
        the caller to report; the protection itself is unchanged (the hold already exists)."""
        if not self.armed or self.released or worker_pid is None:
            return None
        try:
            self.record = self.run.note_held_worker(self.name, worker_pid=worker_pid)
        except BaseException as problem:  # noqa: BLE001 - reported, never unholding
            self.association_failure = problem
            return problem
        return None

    def release(self, *, reason):
        """Release the hold on the one reason the lifecycle's evidence supports, and only
        then.

        `reason` selects what the lifecycle believes: an exit it confirmed through the
        process it held, or a spawn attempt it never made. The Run verifies that claim
        against its own state -- the associated handle's `poll()` for the first, the absence
        of a recorded attempt for the second -- and refuses anything it cannot establish. Its
        refusal is kept (`release_failure`) and raised as `HoldReleaseFailed`: the database
        is still protected, which is exactly what `must_drop()` must keep reporting."""
        if not self.armed:
            raise HoldReleaseFailed(
                f"the scratch database {self.name} was never held; there is nothing to release"
            )
        if self.released:
            return self.record
        try:
            released = self.run.release_hold(self.name, reason=reason)
        except BaseException as problem:  # noqa: BLE001 - the hold is still there
            self.release_failure = problem
            raise HoldReleaseFailed(
                f"the scratch database {self.name} remains held: the hold could not be released"
            ) from problem
        self.released = True
        self.record = released
        return released

    def must_drop(self) -> bool:
        """Whether pytest's own teardown may drop the database: only after a release this
        test positively established, or when arming was refused (no worker was attempted)."""
        return self.released or not self.armed


@dataclass(frozen=True)
class _HandoverScratch(Scratch):
    """The R7 handover test's scratch database, with the deferral that holds it from before
    the worker is started. Everything else is `Scratch`."""

    deferral: "_Deferral"


def _release_the_hold_after_the_exit(defer, attempt, child, stopped):
    """Release the pre-armed hold, and only on evidence the Run can verify itself.

    The lifecycle names the one reason its own evidence supports: an exit it confirmed
    through the handle it held (`_stop_the_handover`'s `_Stopped`, or the status `_spawn`
    confirmed while it owned the raw process), or a spawn attempt that was positively never
    recorded. It supplies no status: the Run asks the associated process handle itself. A
    missing `_HandoverProcess` wrapper, a generic exception, a failed log close or an
    unconfirmed wait is none of those: the hold is kept and the caller reports the failure.

    Returns the failure when the release itself failed (the database stays protected), and
    None when there was nothing to release or the release succeeded."""
    if defer is None or not defer.armed or defer.released:
        return None  # nothing was held (or it is already released)
    if child is not None and stopped is not None and stopped.exit_code is not None:
        reason = RELEASED_WORKER_EXIT_CONFIRMED  # the wait for the worker returned its status
    elif child is None and attempt is not None and attempt.exit_confirmed:
        reason = RELEASED_WORKER_EXIT_CONFIRMED  # `_spawn` ended the child it owned
    elif child is None and attempt is not None and not attempt.attempted:
        reason = RELEASED_SPAWN_NEVER_ATTEMPTED  # positively no spawn: no worker exists
    else:
        return None  # ambiguous: a worker may exist; the hold stays
    try:
        defer.release(reason=reason)
    except BaseException as problem:  # noqa: BLE001 - returned, reported by the caller
        return problem
    return None


def _finish_handover(
    *, child, admission, engine, budget, failure, defer=None, attempt=None
) -> None:
    """What may not be left to the fixtures: A's admission released (the worker can only
    make progress after it), the worker's process ended and its exit confirmed, the engine
    disposed -- in that order, on every path.

    The release is in a `finally` of its own, and stopping the worker is in an *outer* one,
    so a release that fails, or a failure before the worker was even started, cannot skip
    it. Disposing only after the exit is confirmed is what keeps a worker from racing a
    connection this test closed; an interruption that was recovered from is raised when
    nothing else failed, because a test that was interrupted is not a test that passed. When
    the exit cannot be confirmed, the worker's directory and the engine are left untouched
    and the unconfirmed exit is reported: the run is never told that it ended (`raise
    HandoverProcessNotStopped` from `_stop_the_handover`).

    R7: the scratch database was already HELD before the worker was started (`_Deferral.
    arm`), so neither the function-level fixture nor the session finalizer can drop it while
    a process may still be alive. The hold is released here -- after a confirmed exit or a
    positively never-made attempt, never on the absence of a handle -- and stays in place on
    every path that cannot establish either. A failed release is reported with the test's own
    failure and never over it."""
    try:
        try:
            admission.release()
        except BaseException as problem:  # noqa: BLE001 - never instead of the test's own
            _report_secondary("the admission transaction could not be released", problem)
    finally:
        unconfirmed = None
        stopped = None
        try:
            stopped = _stop_the_handover(child, budget=budget)
        except HandoverProcessNotStopped as not_stopped:
            unconfirmed = not_stopped

        release_failure = None
        if unconfirmed is None:
            release_failure = _release_the_hold_after_the_exit(defer, attempt, child, stopped)
        else:
            _say(
                logging.CRITICAL,
                "the handover process was not confirmed stopped: %s",
                unconfirmed,
            )

        if unconfirmed is not None:
            # No exit was confirmed: a process may still be writing the files and holding
            # the connection, so neither its directory nor the engine is touched, and the
            # hold stays (it was armed before the spawn). The test's own failure, if there is
            # one, is still the report; otherwise the unconfirmed exit is -- the run must
            # never be told that the worker ended.
            if failure is not None:
                return  # the test's own failure keeps propagating
            raise unconfirmed

        output = child.output() if child is not None else ""
        try:
            if (
                failure is not None
                and not stopped.terminated
                and stopped.exit_code not in (0, None)
            ):
                _report_secondary(
                    "the handover process failed too",
                    HandoverProcessFailed(stopped.exit_code, output),
                )
        finally:
            if child is not None:
                child.close()  # its exit is confirmed; the files it used can go
            _dispose_quietly(engine)
        if release_failure is not None:
            # R7: the exit was confirmed, but the run refused to clear the hold. The
            # database is still protected; the fixture's drop refuses it too.
            if failure is not None:
                _report_secondary(
                    "the hold could not be released; the scratch database stays protected",
                    release_failure,
                )
                return  # the test's own failure stays the one that explains the run
            raise release_failure
        if failure is not None:
            return  # the test's own failure stays the one that explains the run
        if stopped.exit_code not in (0, None):
            raise HandoverProcessFailed(stopped.exit_code, output)
        if stopped.interruption is not None:
            raise stopped.interruption


def _release_around_the_observation(observe, child, *, admission) -> None:
    """`observe(child)` -- the assertions made while A's transaction is open -- and then the
    release of that transaction, whatever the observation did.

    On the observation's failure the release is attempted too, and a release that fails is
    reported rather than raised over the observation; `_finish_handover` tries again."""
    try:
        observe(child)
    except BaseException:
        admission.release_if_the_test_is_already_failing()
        raise
    admission.release()


def _hand_over_under_review(prepare, start, observe, *, admission, engine, budget, defer=None):
    """The whole lifecycle of the PostgreSQL handover test, in one function so that the
    offline test at the end of this module can drive it without a server.

    `prepare` makes the facts A's open transaction rests on; `start(attempt)` starts the
    worker's process (recording on `attempt` that `Popen` was reached); `observe(child)`
    asserts what is visible while A is still open. A's transaction is then released and the
    worker's exit confirmed before this call can return. The one path on which the worker may
    outlive it is an exit that cannot be confirmed at all: then nothing it used is cleaned up,
    the database it used stays held (`defer`, R7) and `HandoverProcessNotStopped` is raised
    instead (see `_finish_handover`).

    R7: the hold is armed BEFORE `start(attempt)` is called. An arming failure stops the
    lifecycle there, with no worker started, so `Popen` is not reachable while the database
    is unprotected and the run cleans it up normally."""
    child = None
    failure = None
    attempt = _SpawnAttempt(hold=defer)
    try:
        prepared = prepare()
        if defer is not None:
            defer.arm()  # HELD before the spawn path: no `Popen` without the hold
        child = start(attempt)
        if defer is not None:
            # The process id is only known now; it is added to the existing hold, which is
            # never removed to do it. A failure is a diagnostic: the hold already exists.
            association = defer.note_worker(_worker_pid(child, None))
            if association is not None:
                _report_secondary(
                    "the worker's process id could not be recorded with the hold", association
                )
        _release_around_the_observation(observe, child, admission=admission)
        _wait_for_the_handover(child, budget=budget)  # it exited; the account is B's
        return prepared
    except BaseException as problem:
        failure = problem
        raise
    finally:
        _finish_handover(
            child=child,
            admission=admission,
            engine=engine,
            budget=budget,
            failure=failure,
            defer=defer,
            attempt=attempt,
        )


def _release_the_handover_scratch(run, name, deferral) -> None:
    """The R7 fixture's teardown decision, in one place so the offline tests can drive it.

    The database is dropped only after a release this test positively established (an
    OS-confirmed exit, or a spawn attempt that never happened) or when the hold could never
    be armed -- in which case the spawn path was never entered. A database that is still held
    is never dropped here: a worker may still be using it, and the run that owns it refuses
    the drop itself, at this level and in `Run.close()`."""
    if deferral.must_drop():
        run.drop_database(name)
        return
    _say(
        logging.CRITICAL,
        "the scratch database %s is retained: %s",
        name,
        "held before the worker was started, and no confirmed exit released it",
    )


@pytest.fixture
def handover_scratch_rc(pg_run, template):
    """A database of its own for the R7 handover test -- `scratch_rc` plus the deferral that
    holds it from before the worker is started.

    The hold is registered with `pg_run` itself, the session's Run that created the database,
    BEFORE the spawn path is entered, so the teardown below AND the session finalizer
    (`Run.close()`) refuse until this test positively establishes that no worker is left. A
    confirmed exit (the ordinary path, and a worker this test had to terminate) releases the
    hold and the database is dropped as any other scratch fixture's is."""
    name = pg_run.create_database(template=template)
    deferral = _Deferral(run=pg_run, name=name)
    try:
        yield _HandoverScratch(pg_run, name, deferral)
    finally:
        _release_the_handover_scratch(pg_run, name, deferral)


@pytest.fixture
def handover_monitor_rc(handover_scratch_rc):
    """The R7 test's `monitor_rc`: the same scratch database, not a second one."""
    engine = handover_scratch_rc.engine()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL "
    "(see tests/test_admin_postgres.py)",
)
def test_the_binding_handover_waits_for_an_open_admission_and_the_marker_refuses_the_new_owner(
    handover_scratch_rc, handover_monitor_rc
):
    """Binding-handover serialization plus marker-based replay protection.

    The real constraint: an account has ONE link at a time, so a claim for owner B can only
    happen after an unlink from A and a link to B. This test separates the two facts that
    protect the update, and does not pretend there are ever two simultaneously eligible
    owners:

      * A's admission transaction stays OPEN. Its marker is uncommitted (invisible to every
        other connection) and its `SELECT ... FOR SHARE` on the link row is held. B's
        attempt, made while A is open, looks at the same (still A's) link under READ COMMITTED
        and is refused BINDING_CHANGED. PostgreSQL does not make that look wait: a second
        FOR SHARE is compatible with the first.
      * The handover's DELETE of that link conflicts with A's share lock. It is started in
        a process of its own (see the lifecycle note below) and is DETERMINISTICALLY shown to
        be waiting on A's transaction (`pg_locks`/`pg_stat_activity`, wait event
        `transactionid`).
      * Releasing A lets the DELETE and the new link commit. A fresh B admission then sees
        B's link but is refused ALREADY_ADMITTED by A's committed marker: the ledger is the
        bot's, not the owner's.

    At most one admission succeeds, one update marker survives, one link exists (B's), and
    nothing of a turn -- message, request, attachment or conversation -- is retained for
    either owner. Nothing here reaches a turn runner or a provider (`turn_requests` is 0,
    which is the durable proof no turn, and so no provider call, began)."""
    scratch_rc = handover_scratch_rc  # the same scratch database, plus its R7 deferral
    monitor_rc = handover_monitor_rc
    first_owner = _owner_with_link(scratch_rc, subject="sub-first")
    second_owner = _owner_without_link(scratch_rc, subject="sub-second")
    marker_sql = (
        "SELECT count(*) FROM telegram_update_admissions "
        "WHERE bot_id = :bot AND update_id = :update"
    )
    marker = {"bot": CROSS_OWNER_BOT, "update": CROSS_OWNER_UPDATE}

    holder_engine = scratch_rc.engine()
    holder = create_session_factory(holder_engine)()
    holder.begin()
    admission = _Admission(holder)

    def start_the_handover(attempt):
        return _start_the_handover(
            scratch_rc,
            first_owner=first_owner,
            second_owner=second_owner,
            attempt=attempt,
        )

    def prepare():
        assert (
            store.admit_update(
                holder,
                CROSS_OWNER_ACCOUNT,
                first_owner,
                CROSS_OWNER_UPDATE,
                bot_id=CROSS_OWNER_BOT,
                now=NOW,
                existing=False,
            )
            is store.AdmissionOutcome.ADMITTED
        )
        # The marker is written, but only inside the open transaction: no other connection
        # can see it, which is why it cannot be what refuses B yet.
        assert scalar(monitor_rc, marker_sql, **marker) == 0

        # B is a second eligible owner, but the account is still A's. Its FOR SHARE is
        # compatible with A's, so this returns instead of queueing; the binding, not the
        # marker (still invisible), is what refuses it.
        competitor_outcome = run_on(
            scratch_rc,
            lambda db: store.admit_update(
                db,
                CROSS_OWNER_ACCOUNT,
                second_owner,
                CROSS_OWNER_UPDATE,
                bot_id=CROSS_OWNER_BOT,
                now=NOW,
                existing=False,
            ),
        )
        assert competitor_outcome is store.AdmissionOutcome.BINDING_CHANGED
        assert holder.in_transaction(), "A's admission transaction must still be open"
        assert scalar(monitor_rc, marker_sql, **marker) == 0
        return competitor_outcome

    def observe(child):
        _wait_until(
            lambda: _published_pid(child.pidfile) is not None or child.exited(),
            timeout=HANDOVER_WITHIN,
            what="the handover's transaction to open on its own connection",
        )
        pid = _published_pid(child.pidfile)
        assert pid is not None, (
            "the handover process ended before its transaction opened:\n" + child.output()
        )
        _wait_until(
            lambda: _waiting_on_a_lock(monitor_rc, pid) or child.exited(),
            timeout=OBSERVE_WITHIN,
            what="the handover's DELETE waiting on the link's share lock",
        )
        assert not child.exited(), (
            "the handover process ended before its DELETE was seen waiting:\n" + child.output()
        )
        # A row-lock conflict is a wait on the holder's transaction id.
        assert _lock_wait_event(monitor_rc, pid) == "transactionid"
        assert scalar(monitor_rc, marker_sql, **marker) == 0  # A's marker still uncommitted

    # A's admission is open for all of this: it is released on every path, and the worker's
    # process is stopped and its exit confirmed before the test can return (see
    # `_hand_over_under_review`).
    competitor_outcome = _hand_over_under_review(
        prepare,
        start_the_handover,
        observe,
        admission=admission,
        engine=holder_engine,
        budget=HANDOVER_WITHIN,
        defer=scratch_rc.deferral,  # R7: an unconfirmed exit holds the database
    )

    # The account really changed hands; that is why B's fresh attempt is meaningful.
    assert (
        run_on(
            scratch_rc,
            lambda db: store.enrolled_user_for_telegram(db, CROSS_OWNER_ACCOUNT),
        )
        == second_owner
    )
    # The attempt made while A was open never admitted the update: it was a different owner.
    assert competitor_outcome is store.AdmissionOutcome.BINDING_CHANGED
    # A fresh attempt after the handover is refused by the first owner's committed marker.
    fresh = run_on(
        scratch_rc,
        lambda db: store.admit_update(
            db,
            CROSS_OWNER_ACCOUNT,
            second_owner,
            CROSS_OWNER_UPDATE,
            bot_id=CROSS_OWNER_BOT,
            now=NOW,
            existing=False,
        ),
    )
    assert fresh is store.AdmissionOutcome.ALREADY_ADMITTED

    # Exactly one update was admitted and exactly one marker survives; exactly one link
    # exists, and it is B's. Nothing of a turn was retained for either owner.
    assert scalar(monitor_rc, marker_sql, **marker) == 1
    assert (
        scalar(
            monitor_rc,
            "SELECT count(*) FROM telegram_update_admissions "
            "WHERE bot_id = :bot AND update_id = :update AND kind = 'update'",
            **marker,
        )
        == 1
    )
    assert scalar(monitor_rc, "SELECT count(*) FROM telegram_links") == 1
    assert scalar(monitor_rc, "SELECT count(*) FROM turn_requests") == 0
    assert scalar(monitor_rc, "SELECT count(*) FROM messages") == 0
    assert scalar(monitor_rc, "SELECT count(*) FROM message_attachments") == 0
    assert scalar(monitor_rc, "SELECT count(*) FROM conversations") == 0


# --- the same lifecycle, without a server ------------------------------------------------------------------------
#
# The test above is opt-in and needs a real server, so what it promises about its worker is
# proved here instead: the same `_hand_over_under_review`, the same helpers beneath it, and
# a stub admission transaction whose `commit` is the release -- the role releasing A's share
# lock plays there, and the only thing it has to imitate. The worker is a real process here
# too: `_spawn` starts one, and `_stop_the_handover` ends it.


WORKER_BOUND = 0.5  # how long a wait is given for a process this test expects to exit
SHORT_BUDGET = 0.05  # a budget a process that does not exit on its own cannot meet
SLEEPING_CHILD = [sys.executable, "-c", "import time; time.sleep(30)"]
QUICK_CHILD = [sys.executable, "-c", "pass"]
FAILING_CHILD = [
    sys.executable,
    "-c",
    "import sys; sys.stderr.write('the handover could not complete\\n'); sys.exit(3)",
]


def _offline_child(command, *, attempt=None) -> _HandoverProcess:
    """A real process on a directory of its own, started exactly as the worker's is."""
    return _spawn(
        command,
        directory=Path(tempfile.mkdtemp(prefix="house-helper-handover-offline-")),
        attempt=attempt,
    )


class StubAdmissionTransaction:
    """A's open admission transaction, as far as the release is concerned: `commit` is the
    release. Every call is recorded; each step can be made to fail on its own, because the
    release must survive every combination."""

    def __init__(self, *, failure=None, rollback_failure=None, close_failure=None):
        self.calls: list[str] = []
        self.failure = failure
        self.rollback_failure = rollback_failure
        self.close_failure = close_failure

    def commit(self):
        self.calls.append("commit")
        if self.failure is not None:
            raise self.failure

    def rollback(self):
        self.calls.append("rollback")
        if self.rollback_failure is not None:
            raise self.rollback_failure

    def close(self):
        self.calls.append("close")
        if self.close_failure is not None:
            raise self.close_failure


class StubEngine:
    """The admission engine, and the evidence for the one ordering that matters: at the
    moment it is disposed, no process this test started may still be alive."""

    def __init__(self, *, children):
        self.children = children
        self.disposed = False
        self.live_at_dispose = None

    def dispose(self):
        self.live_at_dispose = [child for child in self.children if not child.exited()]
        self.disposed = True


class ObservationFailed(Exception):
    """The test's own failure, injected where the lock observation is."""


class ReleaseFailed(Exception):
    """A cleanup failure, injected where the release of the admission is."""


class StartFailed(Exception):
    """A failure to start the worker's process, before anything of it exists."""


class SimulatedInterrupt(Exception):
    """An interruption of a wait for the worker's process, injected where a real
    `KeyboardInterrupt` lands: after the worker was told to stop and before it has ended."""


def _teardown_stand_in(child):
    """Where `pg_run.drop_database()` runs. `DROP DATABASE ... WITH (FORCE)` may destroy the
    scratch database now -- after a failing test as much as after a passing one -- so no
    process this test started may still be alive. That is the assertion the fixture itself
    cannot make, and the reason the worker is a process."""
    assert child is None or child.exited(), "fixture teardown would race a live worker process"
    return child is None or child.process.wait(timeout=5)


@pytest.mark.parametrize(
    "scenario",
    [
        "normal",
        "observation-fails",
        "cleanup-fails",
        "release-fails",
        "worker-fails",
        "worker-overruns",
        "prepare-fails",
        "start-fails",
    ],
)
def test_the_handover_process_cannot_outlive_the_test(scenario, caplog):
    """Every exit of the handover: the admission is released, the worker's process ended and
    its exit confirmed, the engine disposed -- and the test's own failure is the one that is
    reported.

    `StubEngine` checks the half of the ordering that protects the database: at the moment
    it is disposed, no process this test started is alive. `_teardown_stand_in` then does
    what the fixture would: it may proceed only over a dead worker."""
    caplog.set_level(logging.WARNING)
    releasing_fails = scenario in {"cleanup-fails", "release-fails"}
    observing_fails = scenario in {"observation-fails", "cleanup-fails"}
    session = StubAdmissionTransaction(
        failure=ReleaseFailed("the release failed") if releasing_fails else None
    )
    admission = _Admission(session)
    children = []
    engine = StubEngine(children=children)
    budget = SHORT_BUDGET if scenario == "worker-overruns" else WORKER_BOUND

    def start(attempt):
        if scenario == "start-fails":
            raise StartFailed("the handover process could not be started")
        if scenario == "worker-fails":
            child = _offline_child(FAILING_CHILD, attempt=attempt)
        elif scenario in {
            "observation-fails",
            "cleanup-fails",
            "release-fails",
            "worker-overruns",
        }:
            child = _offline_child(SLEEPING_CHILD, attempt=attempt)  # still running when it has to be stopped
        else:
            child = _offline_child(QUICK_CHILD, attempt=attempt)
        children.append(child)
        return child

    def observe(worker):
        if observing_fails:
            raise ObservationFailed("the lock observation failed")

    def prepare():
        if scenario == "prepare-fails":
            raise ObservationFailed("the setup failed before the worker was started")

    def run():
        return _hand_over_under_review(
            prepare, start, observe, admission=admission, engine=engine, budget=budget
        )

    if scenario in {"observation-fails", "cleanup-fails", "prepare-fails"}:
        with pytest.raises(ObservationFailed):  # the test's own failure, still the report
            run()
    elif scenario == "release-fails":
        with pytest.raises(ReleaseFailed):  # nothing precedes it, so it IS the failure
            run()
    elif scenario == "start-fails":
        with pytest.raises(StartFailed):  # the failure before the process existed
            run()
    elif scenario == "worker-fails":
        with pytest.raises(HandoverProcessFailed) as reported:
            run()
        assert "the handover could not complete" in reported.value.output
    elif scenario == "worker-overruns":
        with pytest.raises(subprocess.TimeoutExpired):
            run()
    else:
        run()

    # Nothing this test started outlived it, and the teardown stand-in can prove it.
    assert all(child.exited() for child in children), "a handover process outlived the test"
    assert engine.disposed is True
    assert engine.live_at_dispose == [], "the engine was disposed over a live worker process"
    _teardown_stand_in(children[-1] if children else None)
    assert session.calls[0] == "commit" and session.calls[-1] == "close"

    if scenario == "normal":
        assert admission.released is True
        assert session.calls == ["commit", "rollback", "close"]
        assert "could not be released" not in caplog.text  # nothing to report
        assert "terminating it" not in caplog.text  # nothing had to be killed
    elif scenario == "observation-fails":
        assert admission.released is True  # the observation's failure released it all the same
        assert "could not be released" not in caplog.text
        assert "terminating it" in caplog.text  # the worker was still running
        assert "the handover process ended (exit status" in caplog.text
    elif scenario == "cleanup-fails":
        assert admission.released is False
        assert "the admission transaction could not be released" in caplog.text
        assert "terminating it" in caplog.text
    elif scenario == "release-fails":
        assert admission.released is False
        assert "the admission transaction could not be released" in caplog.text
        assert "terminating it" in caplog.text
    elif scenario == "worker-fails":
        assert admission.released is True
        assert "terminating it" not in caplog.text  # it ended on its own, with status 3
    elif scenario == "worker-overruns":
        assert admission.released is True
        assert "terminating it" in caplog.text  # the overrun was ended, not waited out
    elif scenario == "start-fails":
        assert admission.released is True  # the start's failure took the cleanup path
        assert session.calls == ["commit", "rollback", "close"]
    else:  # prepare-fails
        assert admission.released is True  # the setup's failure still released the transaction
        assert session.calls == ["commit", "rollback", "close"]


# --- the termination that makes the fixture's drop safe ------------------------------------------------------------


def test_a_worker_that_would_not_exit_cannot_race_the_fixture_teardown(caplog):
    """The residual risk this closes: a worker that never finishes used to leave a live
    thread holding a connection while `scratch_rc`'s teardown ran `DROP DATABASE ... WITH
    (FORCE)` -- which happens after a failing test too. The worker is now a process: when it
    overruns it is terminated, its exit is confirmed, and only then is anything it used
    touched, so the fixture's drop cannot race it."""
    caplog.set_level(logging.CRITICAL)
    session = StubAdmissionTransaction()
    admission = _Admission(session)
    children = []
    engine = StubEngine(children=children)
    child = _offline_child(SLEEPING_CHILD)
    children.append(child)

    with pytest.raises(subprocess.TimeoutExpired):
        _hand_over_under_review(
            lambda: None,
            lambda attempt: child,
            lambda worker: None,
            admission=admission,
            engine=engine,
            budget=SHORT_BUDGET,  # the process does not exit on its own
        )

    # The forced termination is explicit in the log and confirmed by the process itself.
    assert "terminating it" in caplog.text
    assert "the handover process ended (exit status" in caplog.text
    assert child.exited() and child.process.returncode is not None
    assert engine.live_at_dispose == []  # disposed only after the exit
    assert engine.disposed is True
    assert _teardown_stand_in(child) is not None  # the fixture's drop may now proceed


def test_an_interrupted_wait_is_retried_until_the_process_is_confirmed_gone(caplog):
    """An exception out of the wait is not evidence that the worker ended: the first
    interruption is kept, the wait is made again, and the termination still completes before
    anything is disposed."""
    caplog.set_level(logging.WARNING)
    session = StubAdmissionTransaction()
    admission = _Admission(session)
    children = []
    engine = StubEngine(children=children)
    child = _offline_child(SLEEPING_CHILD)
    children.append(child)
    real_wait = child.wait
    waits = []

    def interrupted_wait(*, timeout=None):
        waits.append(timeout)
        if len(waits) == 1:
            raise SimulatedInterrupt("waiting for the handover process was interrupted")
        return real_wait(timeout=timeout)

    child.wait = interrupted_wait

    def observe(worker):
        raise ObservationFailed("the lock observation failed")

    with pytest.raises(ObservationFailed):  # the test's own failure stays the report
        _hand_over_under_review(
            lambda: None,
            lambda attempt: child,
            observe,
            admission=admission,
            engine=engine,
            budget=SHORT_BUDGET,
        )

    assert len(waits) >= 3  # interrupted, retried, and confirmed after the termination
    assert "waiting for the handover process was interrupted" in caplog.text
    assert child.exited()
    assert engine.live_at_dispose == []
    assert _teardown_stand_in(child) is not None


def test_an_interruption_that_nothing_else_explains_is_raised_after_the_exit_is_confirmed():
    """A test that was interrupted is not a test that passed: once the worker's exit is
    confirmed and everything is cleaned up, the first interruption becomes the report."""
    session = StubAdmissionTransaction()
    admission = _Admission(session)
    children = []
    engine = StubEngine(children=children)
    child = _offline_child(QUICK_CHILD)
    children.append(child)
    real_wait = child.wait
    waits = []

    def interrupted_after_the_body(*, timeout=None):
        waits.append(timeout)
        if len(waits) == 2:  # the body's own wait is the first
            raise SimulatedInterrupt("the finish's wait was interrupted")
        return real_wait(timeout=timeout)

    child.wait = interrupted_after_the_body

    with pytest.raises(SimulatedInterrupt) as reported:
        _hand_over_under_review(
            lambda: None,
            lambda attempt: child,
            lambda worker: None,
            admission=admission,
            engine=engine,
            budget=WORKER_BOUND,
        )

    assert reported.value.args == ("the finish's wait was interrupted",)
    assert child.exited()  # it had already ended on its own
    assert engine.disposed is True
    assert engine.live_at_dispose == []


# --- the release's exception priority ----------------------------------------------------------------------------

SECONDARY_REPORT = {
    "rollback": "the rollback after the release failed too",
    "close": "closing the admission session failed too",
}


@pytest.mark.parametrize(
    "commit_fails, rollback_fails, close_fails, first, reported_secondaries",
    [
        (True, False, False, "commit", ()),
        (True, True, False, "commit", ("rollback",)),
        (True, False, True, "commit", ("close",)),
        (True, True, True, "commit", ("rollback", "close")),
        (False, True, False, "rollback", ()),
        (False, False, True, "close", ()),
    ],
)
def test_the_first_release_failure_stays_the_report(
    commit_fails, rollback_fails, close_fails, first, reported_secondaries, caplog
):
    """Every combination of the three release steps failing: every step is attempted, the
    FIRST failure is the one raised (the same object), and a later one is logged with its
    traceback instead of replacing it."""
    caplog.set_level(logging.WARNING)
    failures = {
        "commit": ReleaseFailed("the commit failed"),
        "rollback": ReleaseFailed("the rollback failed"),
        "close": ReleaseFailed("the close failed"),
    }
    session = StubAdmissionTransaction(
        failure=failures["commit"] if commit_fails else None,
        rollback_failure=failures["rollback"] if rollback_fails else None,
        close_failure=failures["close"] if close_fails else None,
    )
    admission = _Admission(session)

    with pytest.raises(ReleaseFailed) as reported:
        admission.release()

    assert reported.value is failures[first]  # the first failure, not the last one seen
    assert session.calls == ["commit", "rollback", "close"]  # every cleanup was attempted
    assert admission.released is False
    for operation in reported_secondaries:
        assert SECONDARY_REPORT[operation] in caplog.text
    for operation in {"rollback", "close"} - {first} - set(reported_secondaries):
        assert SECONDARY_REPORT[operation] not in caplog.text


def test_a_failing_release_cannot_skip_stopping_the_worker(caplog):
    """The seventh combination, where the failure that must stay primary is not the
    release's: an observation failure is already active when every step of the release
    fails. The observation stays the report, all three steps are attempted on both release
    attempts, the secondary failures are logged, and the worker's process is still stopped
    before the engine is disposed."""
    caplog.set_level(logging.WARNING)
    session = StubAdmissionTransaction(
        failure=ReleaseFailed("the commit failed"),
        rollback_failure=ReleaseFailed("the rollback failed"),
        close_failure=ReleaseFailed("the close failed"),
    )
    admission = _Admission(session)
    children = []
    engine = StubEngine(children=children)
    child = _offline_child(SLEEPING_CHILD)
    children.append(child)

    def observe(worker):
        raise ObservationFailed("the lock observation failed")

    with pytest.raises(ObservationFailed):
        _hand_over_under_review(
            lambda: None,
            lambda attempt: child,
            observe,
            admission=admission,
            engine=engine,
            budget=WORKER_BOUND,
        )

    # Both release attempts -- the observation's and the finish's -- tried all three steps.
    assert session.calls == ["commit", "rollback", "close"] * 2
    assert caplog.text.count("the admission transaction could not be released") == 2
    assert SECONDARY_REPORT["rollback"] in caplog.text
    assert SECONDARY_REPORT["close"] in caplog.text
    assert "terminating it" in caplog.text  # the worker was still running; stopped, not skipped
    assert child.exited()
    assert engine.disposed is True
    assert engine.live_at_dispose == []
    assert _teardown_stand_in(child) is not None


# --- a process that already exists when starting it fails --------------------------------------------------------
#
# Codex R7 P1: `Popen` can succeed and then `log.close()` or `_HandoverProcess(...)` can fail.
# Before this correction the handle was lost at that point, the caller removed the temporary
# directory holding the run's password, and the fixture teardown ran while the worker was
# still alive. The tests below start real local processes (no database is contacted) and make
# each post-spawn step fail in turn.

BEATING_WITHIN = 0.2  # how long the heartbeat file is watched for a sign of life

# A child whose work is observable: it writes until it is gone. No signal and no
# platform-specific process table are needed to see that it ended.
HEARTBEAT_CHILD = (
    "import sys, time\n"
    "with open(sys.argv[1], 'ab', buffering=0) as beat:\n"
    "    while True:\n"
    "        beat.write(b'x')\n"
    "        time.sleep(0.02)\n"
)

# The same, for `_start_the_handover`, whose child gets (repository root, configuration
# path): the heartbeat lands next to the configuration.
HANDOVER_HEARTBEAT = (
    "import sys, time\n"
    "with open(sys.argv[2] + '.beat', 'ab', buffering=0) as beat:\n"
    "    while True:\n"
    "        beat.write(b'x')\n"
    "        time.sleep(0.02)\n"
)


def _heartbeat(directory):
    """A real child that keeps writing, and the file it writes."""
    beat = directory / "heartbeat"
    return [sys.executable, "-c", HEARTBEAT_CHILD, str(beat)], beat


def _beat_has_stopped(beat) -> bool:
    before = beat.stat().st_size if beat.exists() else 0
    time.sleep(BEATING_WITHIN)
    after = beat.stat().st_size if beat.exists() else 0
    return before == after


@pytest.fixture
def recorded_processes(monkeypatch):
    """Every real process the test starts. `Popen` is only wrapped so the test can ask the
    operating system itself whether a child is still running."""
    started = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    return started


class LogCloseFailed(Exception):
    """Closing the worker's log failed after its process had already started."""


class WrapperConstructionFailed(Exception):
    """Building the worker's wrapper failed after its process had already started."""


class OwnershipTransferFailed(Exception):
    """The transfer of the process to the wrapper failed."""


class KillFailed(Exception):
    """The process could not be signalled."""


class WaitFailed(Exception):
    """The wait could not confirm the process's exit."""


class LoggingFailed(Exception):
    """Raised by a logger a diagnostic is written to."""


class CleanupFailed(Exception):
    """Removing the worker's temporary files failed."""


class _LogWhoseCloseFails:
    """A real file for `Popen` whose close raises once the process exists."""

    def __init__(self, real, failure):
        self._real = real
        self._failure = failure

    def fileno(self):
        return self._real.fileno()

    def write(self, data):
        return self._real.write(data)

    def flush(self):
        return self._real.flush()

    def close(self):
        self._real.close()
        raise self._failure


def _a_directory_with_a_password(tmp_path) -> tuple[Path, Path]:
    """The worker's directory as the real start leaves it, with the file that carries the
    run's password."""
    directory = tmp_path / "handover"
    directory.mkdir()
    credentials = directory / "handover.configuration"
    credentials.write_bytes(b"the run's password")
    return directory, credentials


def test_a_process_that_already_started_is_ended_before_a_failing_log_close_propagates(
    tmp_path, monkeypatch, recorded_processes
):
    """`log.close()` raised after `Popen` returned. The original failure is the report, the
    child's exit is confirmed before it propagates, and no file is removed: removing the
    password's directory is the caller's step and only happens after a confirmed exit."""
    directory, credentials = _a_directory_with_a_password(tmp_path)
    command, beat = _heartbeat(directory)
    failure = LogCloseFailed("closing the worker's log failed")

    def open_a_log_that_cannot_be_closed(path):
        return _LogWhoseCloseFails(open(path / "handover.log", "wb"), failure)

    monkeypatch.setitem(globals(), "_open_the_worker_log", open_a_log_that_cannot_be_closed)

    with pytest.raises(LogCloseFailed) as reported:
        _spawn(command, directory=directory)

    assert reported.value is failure  # the original exception, not a cleanup's
    [process] = recorded_processes
    assert process.poll() is not None  # the OS says it ended before the error left `_spawn`
    assert _beat_has_stopped(beat)  # the child's own work stopped too
    assert credentials.read_bytes() == b"the run's password"  # nothing was removed


def test_a_process_that_already_started_is_ended_before_a_failing_wrapper_propagates(
    tmp_path, monkeypatch, recorded_processes
):
    """`_HandoverProcess` could not be built after `Popen` returned. Cleanup cannot rely on
    the wrapper: it must use the raw process, and that is what is proved here."""
    directory, credentials = _a_directory_with_a_password(tmp_path)
    command, beat = _heartbeat(directory)
    failure = WrapperConstructionFailed("the wrapper could not be built")

    class Exploding:
        def __init__(self, *args, **kwargs):
            raise failure

    monkeypatch.setitem(globals(), "_HandoverProcess", Exploding)

    with pytest.raises(WrapperConstructionFailed) as reported:
        _spawn(command, directory=directory)

    assert reported.value is failure
    [process] = recorded_processes
    assert process.poll() is not None
    assert _beat_has_stopped(beat)
    assert credentials.exists()


def test_a_process_that_already_started_is_ended_when_ownership_cannot_be_transferred(
    tmp_path, monkeypatch, recorded_processes
):
    """The wrapper was built but the explicit transfer of the process to it failed. The
    process is still ours at that point, and is ended before the error propagates."""
    directory, credentials = _a_directory_with_a_password(tmp_path)
    command, beat = _heartbeat(directory)
    failure = OwnershipTransferFailed("the process could not be handed over")

    def hand_over_fails(self):
        raise failure

    monkeypatch.setattr(_Spawned, "hand_over", hand_over_fails)

    with pytest.raises(OwnershipTransferFailed) as reported:
        _spawn(command, directory=directory)

    assert reported.value is failure
    [process] = recorded_processes
    assert process.poll() is not None
    assert _beat_has_stopped(beat)
    assert credentials.exists()


def _scratch_for_starting():
    """As much of the harness's scratch database as `_start_the_handover` needs to write its
    configuration. Nothing here connects anywhere: the failures under test happen after the
    process exists and before it could use the configuration."""
    run = SimpleNamespace(
        roles=None,
        target=SimpleNamespace(username="app"),
        marker="marker",
        verified_cluster=lambda: SimpleNamespace(system_identifier="1"),
    )
    return SimpleNamespace(run=run, name="scratch", url_text="postgresql+psycopg://unused")


def test_the_workers_files_are_removed_only_after_its_process_has_ended(
    monkeypatch, recorded_processes
):
    """The caller's cleanup -- removing the directory that carries the run's password -- is
    checked against the operating system's own answer about the process at the moment of
    every removal it performs."""
    removals = []
    real_rmtree = shutil.rmtree

    def watched_rmtree(path, *args, **kwargs):
        removals.append([process.poll() for process in recorded_processes])
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", watched_rmtree)
    monkeypatch.setitem(globals(), "_HANDOVER_CHILD", HANDOVER_HEARTBEAT)

    failure = WrapperConstructionFailed("the wrapper could not be built")

    class Exploding:
        def __init__(self, *args, **kwargs):
            raise failure

    monkeypatch.setitem(globals(), "_HandoverProcess", Exploding)

    with pytest.raises(WrapperConstructionFailed) as reported:
        _start_the_handover(
            _scratch_for_starting(),
            first_owner=UUID(int=1),
            second_owner=UUID(int=2),
        )

    assert reported.value is failure
    assert removals, "the caller never removed the worker's directory"
    assert all(
        code is not None for answer in removals for code in answer
    ), "the worker's files were removed while its process was still alive"
    [process] = recorded_processes
    assert process.poll() is not None


def test_the_workers_files_are_left_in_place_when_the_exit_cannot_be_confirmed(
    monkeypatch, recorded_processes
):
    """The start could not confirm the exit. There is no safe cleanup then: the directory --
    and the password in it -- stays, and the caller is told so by the exception."""
    made = []
    real_mkdtemp = tempfile.mkdtemp
    real_rmtree = shutil.rmtree

    def recording_mkdtemp(*args, **kwargs):
        directory = real_mkdtemp(*args, **kwargs)
        made.append(Path(directory))
        return directory

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)
    monkeypatch.setitem(globals(), "_HANDOVER_CHILD", HANDOVER_HEARTBEAT)

    failure = WrapperConstructionFailed("the wrapper could not be built")

    class Exploding:
        def __init__(self, *args, **kwargs):
            raise failure

    monkeypatch.setitem(globals(), "_HandoverProcess", Exploding)

    def end_without_confirmation(self):
        raise HandoverProcessNotStopped("the exit could not be confirmed")

    monkeypatch.setattr(_Spawned, "end", end_without_confirmation)

    with pytest.raises(HandoverProcessNotStopped):
        _start_the_handover(
            _scratch_for_starting(),
            first_owner=UUID(int=1),
            second_owner=UUID(int=2),
        )

    [directory] = made
    assert directory.exists()  # nothing was removed
    assert (directory / "handover.configuration").exists()
    [process] = recorded_processes
    assert process.poll() is None  # it really was left running, with its files
    # The test ends the process it deliberately left to the caller's judgement.
    process.kill()
    process.wait(timeout=10)
    real_rmtree(directory, ignore_errors=True)


# --- R7-01 (P2): an ambiguous spawn attempt keeps the credential-bearing directory -------------------------------
#
# Codex R7 P2: `_start_the_handover` removed the directory after `Popen` raised without
# returning a handle. An attempt can outlive its handle. The directory holds the run's
# password, so it is now removed only on positively confirmed absence (no attempt recorded)
# or a confirmed exit -- never because the exception looked like the others.


def _recorded_mkdtemp(monkeypatch):
    """Every directory `_start_the_handover` creates, so the test can say what survived."""
    made = []
    real = tempfile.mkdtemp

    def recording(*args, **kwargs):
        directory = real(*args, **kwargs)
        made.append(Path(directory))
        return directory

    monkeypatch.setattr(tempfile, "mkdtemp", recording)
    return made


def test_a_popen_that_raises_after_the_attempt_keeps_the_credentials_directory(monkeypatch):
    """The Codex reproduction: `Popen` was reached and raised, no handle came back. The
    attempt is recorded, so a process may exist; the password's directory stays and the
    original exception is the report."""
    made = _recorded_mkdtemp(monkeypatch)
    failure = PopenFailed("the process could not be started")

    def popen_that_loses_the_handle(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "Popen", popen_that_loses_the_handle)
    attempt = _SpawnAttempt()

    with pytest.raises(PopenFailed) as reported:
        _start_the_handover(
            _scratch_for_starting(),
            first_owner=UUID(int=1),
            second_owner=UUID(int=2),
            attempt=attempt,
        )

    assert reported.value is failure
    [directory] = made
    assert directory.exists()  # nothing was removed
    assert (directory / "handover.configuration").exists()
    assert attempt.attempted is True and attempt.exit_confirmed is False
    shutil.rmtree(directory, ignore_errors=True)  # the test owns what it retained


def test_a_pre_popen_failure_permits_ordinary_cleanup(monkeypatch):
    """The positive counterpart: the log could not be opened, which is before the attempt is
    recorded and before `Popen`, so no worker can exist and the directory is removed."""
    made = _recorded_mkdtemp(monkeypatch)
    failure = StartFailed("the worker's log could not be opened")

    def log_that_cannot_be_opened(path):
        raise failure

    monkeypatch.setitem(globals(), "_open_the_worker_log", log_that_cannot_be_opened)
    attempt = _SpawnAttempt()

    with pytest.raises(StartFailed) as reported:
        _start_the_handover(
            _scratch_for_starting(),
            first_owner=UUID(int=1),
            second_owner=UUID(int=2),
            attempt=attempt,
        )

    assert reported.value is failure
    [directory] = made
    assert not directory.exists()  # ordinary cleanup: positively no spawn attempt
    assert attempt.attempted is False and attempt.exit_confirmed is False


@pytest.mark.parametrize(
    "spawn_was_attempted", [True, False], ids=["attempted", "never-attempted"]
)
def test_a_failing_logger_cannot_change_the_retention_decision(monkeypatch, spawn_was_attempted):
    """A logger that raises is not part of the decision: the retention check is computed
    first, the report is attempted afterwards, and the directory either survives or not for
    the evidence's sake alone."""
    made = _recorded_mkdtemp(monkeypatch)
    logger_that_fails = _LoggerThatFails(LoggingFailed("logging failed"))
    monkeypatch.setitem(globals(), "logger", logger_that_fails)
    failure = StartFailed("the worker could not be prepared")

    if spawn_was_attempted:
        def popen_that_loses_the_handle(*args, **kwargs):
            raise failure

        monkeypatch.setattr(subprocess, "Popen", popen_that_loses_the_handle)
        expected_retained = True
    else:
        def log_that_cannot_be_opened(path):
            raise failure

        monkeypatch.setitem(globals(), "_open_the_worker_log", log_that_cannot_be_opened)
        expected_retained = False

    with pytest.raises(StartFailed) as reported:
        _start_the_handover(
            _scratch_for_starting(),
            first_owner=UUID(int=1),
            second_owner=UUID(int=2),
            attempt=_SpawnAttempt(),
        )

    assert reported.value is failure  # the original failure, not the logger's
    [directory] = made
    assert directory.exists() is expected_retained
    assert logger_that_fails.attempts == (1 if expected_retained else 0)
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


def test_a_failing_removal_cannot_replace_the_original_failure(monkeypatch, caplog):
    """The removal itself failed after a pre-`Popen` failure. The decision (remove) stands;
    the removal's failure is reported, and the start's own failure stays the report."""
    caplog.set_level(logging.WARNING)
    made = _recorded_mkdtemp(monkeypatch)
    real_rmtree = shutil.rmtree
    failure = StartFailed("the worker's log could not be opened")
    removal_failure = CleanupFailed("the directory could not be removed")

    def rmtree_that_fails(*args, **kwargs):
        raise removal_failure

    def log_that_cannot_be_opened(path):
        raise failure

    monkeypatch.setattr(shutil, "rmtree", rmtree_that_fails)
    monkeypatch.setitem(globals(), "_open_the_worker_log", log_that_cannot_be_opened)

    with pytest.raises(StartFailed) as reported:
        _start_the_handover(
            _scratch_for_starting(),
            first_owner=UUID(int=1),
            second_owner=UUID(int=2),
            attempt=_SpawnAttempt(),
        )

    assert reported.value is failure
    assert "could not be removed" in caplog.text  # reported, not raised in its place
    [directory] = made
    real_rmtree(directory, ignore_errors=True)


def test_a_failed_association_does_not_authorize_the_database_to_be_dropped(
    monkeypatch, tmp_path
):
    """The run refused the actual process handle. The worker was ended and its exit
    confirmed by the start, but the run has no handle of its own, so it cannot establish the
    exit: the database stays held and neither teardown may drop it."""
    run, server = _fake_server_run()
    name = run.create_database()
    deferral = _Deferral(run=run, name=name)
    failure = TrackingBroken("the process could not be associated")
    offered = []

    def association_fails(name, *, process):
        offered.append(process)
        raise failure

    monkeypatch.setattr(run, "note_held_process", association_fails)
    directory = tmp_path / "worker"
    directory.mkdir()

    def start(attempt):
        return _spawn(QUICK_CHILD, directory=directory, attempt=attempt)

    with pytest.raises(TrackingBroken) as reported:
        _review(child_factory=start, deferral=deferral)

    assert reported.value is failure
    assert offered  # the actual handle really reached the run
    assert deferral.association_failure is failure
    assert deferral.must_drop() is False

    _release_the_handover_scratch(run, name, deferral)  # the function-level teardown
    assert name in server.databases
    with pytest.raises(UnsafeTarget) as closed:  # the session finalizer
        run.close()
    assert closed.value.code == "held_database"
    [record] = run.pending_cleanup()["held"]
    assert record.spawn_attempted is True and record.worker_pid is None
    shutil.rmtree(directory, ignore_errors=True)


# --- diagnostics that fail, when the process has to be stopped ---------------------------------------------------
#
# Codex R7 P2: `_stop_the_handover` used to log `terminating it` before killing and to retry
# a failing wait forever. A logger that raised (or an interruption of the log) skipped both
# the kill and the wait, and the fixture teardown then ran under a live worker. Diagnostics
# are now collected and reported only after the outcome is known; the mandatory steps do not
# consult a logger at all.

SLOW_EXITING_CHILD = [sys.executable, "-c", "import time; time.sleep(0.4)"]


class _LoggerThatFails:
    """Every diagnostic raises. Whether it is an error or an interruption, the termination
    and the wait must not depend on it."""

    def __init__(self, failure):
        self.failure = failure
        self.attempts = 0

    def log(self, level, message, *args, **kwargs):
        self.attempts += 1
        raise self.failure


@pytest.mark.parametrize(
    "failure",
    [LoggingFailed("logging failed"), KeyboardInterrupt()],
    ids=["logging-raises", "logging-is-interrupted"],
)
def test_failing_diagnostics_cannot_skip_the_termination_of_the_worker(monkeypatch, failure):
    """Reporting timed out, and the report itself failed. The worker is still killed, its exit
    confirmed, and only then is the engine disposed."""
    logger_that_fails = _LoggerThatFails(failure)
    monkeypatch.setitem(globals(), "logger", logger_that_fails)
    session = StubAdmissionTransaction()
    admission = _Admission(session)
    children = []
    engine = StubEngine(children=children)
    child = _offline_child(SLEEPING_CHILD)
    children.append(child)

    def run():
        return _hand_over_under_review(
            lambda: None,
            lambda attempt: child,
            lambda worker: None,
            admission=admission,
            engine=engine,
            budget=SHORT_BUDGET,  # the worker does not exit by itself
        )

    # The lifecycle's outcome is the timeout it always was: the failing logger changes how it
    # is reported, never what happened to the worker.
    with pytest.raises(subprocess.TimeoutExpired):
        run()

    assert logger_that_fails.attempts > 0  # a diagnostic was attempted, and raised
    assert child.exited()  # the termination did not depend on it
    assert engine.disposed is True
    assert engine.live_at_dispose == []
    assert _teardown_stand_in(child) is not None


def test_a_failed_kill_is_followed_by_the_wait_that_confirms_the_exit(caplog):
    """A kill that raises is not evidence that the worker ended -- nor that it did not: the
    exit-confirming wait still runs, and the failure is reported alongside."""
    caplog.set_level(logging.WARNING)
    session = StubAdmissionTransaction()
    admission = _Admission(session)
    children = []
    engine = StubEngine(children=children)
    child = _offline_child(SLOW_EXITING_CHILD)  # ends by itself, after the budget
    children.append(child)
    failure = KillFailed("the process could not be signalled")
    kills = []

    def kill_fails():
        kills.append(time.monotonic())
        raise failure

    child.kill = kill_fails

    def run():
        return _hand_over_under_review(
            lambda: None,
            lambda attempt: child,
            lambda worker: None,
            admission=admission,
            engine=engine,
            budget=SHORT_BUDGET,
        )

    with pytest.raises(subprocess.TimeoutExpired):
        run()

    assert kills  # the termination was attempted
    assert "the handover process could not be signalled" in caplog.text
    assert child.exited()  # the wait after the failed kill confirmed it
    assert engine.disposed is True
    assert engine.live_at_dispose == []
    assert _teardown_stand_in(child) is not None


def test_an_unconfirmed_exit_is_never_treated_as_a_stop(monkeypatch, caplog):
    """The exit cannot be confirmed. The call must not return into the fixture's teardown as
    though the worker had ended: the engine is not disposed, the worker's directory is not
    removed, and the fixture's stand-in refuses to proceed over the live process."""
    caplog.set_level(logging.CRITICAL)
    session = StubAdmissionTransaction()
    admission = _Admission(session)
    children = []
    engine = StubEngine(children=children)
    child = _offline_child(SLEEPING_CHILD)
    children.append(child)
    real_wait, real_kill = child.wait, child.kill
    waits = []

    def the_exit_is_never_confirmed(*, timeout=None):
        waits.append(timeout)
        if len(waits) == 1:
            return 0  # the body believes it finished; it is in fact still running
        raise WaitFailed("the wait could not confirm the exit")

    def the_signal_fails():
        raise KillFailed("the process could not be signalled")

    child.wait = the_exit_is_never_confirmed
    child.kill = the_signal_fails

    def run():
        return _hand_over_under_review(
            lambda: None,
            lambda attempt: child,
            lambda worker: None,
            admission=admission,
            engine=engine,
            budget=SHORT_BUDGET,
        )

    with pytest.raises(HandoverProcessNotStopped):
        run()

    assert child.exited() is False  # it really is still running
    assert engine.disposed is False and engine.live_at_dispose is None
    assert child.directory.exists()  # its files were not removed
    with pytest.raises(AssertionError):  # the fixture's drop would have raced the worker
        _teardown_stand_in(child)
    assert "was not confirmed stopped" in caplog.text

    # The test itself ends the process it deliberately left running.
    child.wait = real_wait
    child.kill = real_kill
    real_kill()
    real_wait(timeout=10)
    shutil.rmtree(child.directory, ignore_errors=True)


def test_an_unconfirmed_exit_does_not_displace_the_tests_own_failure(monkeypatch, caplog):
    """The test was already failing when the exit could not be confirmed. Its failure stays
    the report, the unconfirmed exit is reported next to it, and nothing is cleaned up over
    the live process."""
    caplog.set_level(logging.CRITICAL)
    session = StubAdmissionTransaction()
    admission = _Admission(session)
    children = []
    engine = StubEngine(children=children)
    child = _offline_child(SLEEPING_CHILD)
    children.append(child)
    real_wait, real_kill = child.wait, child.kill

    def the_exit_is_never_confirmed(*, timeout=None):
        raise WaitFailed("the wait could not confirm the exit")

    def the_signal_fails():
        raise KillFailed("the process could not be signalled")

    child.wait = the_exit_is_never_confirmed
    child.kill = the_signal_fails

    def observe(worker):
        raise ObservationFailed("the lock observation failed")

    def run():
        return _hand_over_under_review(
            lambda: None,
            lambda attempt: child,
            observe,
            admission=admission,
            engine=engine,
            budget=WORKER_BOUND,
        )

    with pytest.raises(ObservationFailed):
        run()

    assert engine.disposed is False and engine.live_at_dispose is None
    assert child.directory.exists()
    with pytest.raises(AssertionError):
        _teardown_stand_in(child)
    assert "was not confirmed stopped" in caplog.text

    child.wait = real_wait
    child.kill = real_kill
    real_kill()
    real_wait(timeout=10)
    shutil.rmtree(child.directory, ignore_errors=True)


# --- R7: the database a worker that would not exit leaves protected ---------------------------------------------
#
# The fixture above and the session's `pg_run` finalizer both drop scratch databases after a
# failing test. When the worker's exit cannot be confirmed, the database it may still be
# using is HELD instead: registered with the very Run that owns it, before anything is
# logged, on every path -- including the one where the test's own failure is already the
# report, and the post-spawn path where no `_HandoverProcess` wrapper exists.

HOLD_NAME = "house_helper_test_" + "a" * 32 + "_1"


class TrackingBroken(Exception):
    """The run's records cannot be trusted, so a hold cannot be armed (or released)."""


class PopenFailed(OSError):
    """`Popen` raised AFTER the operating system had started the process: no handle came
    back, and that is not evidence that no worker exists."""


class _RecordingRun:
    """What the deferral asks of the session's Run: arm the hold, record the spawn attempt
    before `Popen`, associate the actual process and the worker that becomes known
    afterwards, release it on the evidence the Run accepts. Every call is recorded, and
    arming, association, attempt recording and releasing can each be made to fail like a run
    whose tracking cannot be trusted."""

    def __init__(self, *, failure=None, release_failure=None, association_failure=None,
                 attempt_failure=None):
        self.failure = failure
        self.release_failure = release_failure
        self.association_failure = association_failure
        self.attempt_failure = attempt_failure
        self.holds = []  # (name, reason, worker_pid)
        self.attempts = []  # name
        self.processes = []  # (name, process)
        self.associations = []  # (name, worker_pid)
        self.releases = []  # (name, reason, worker_exit)
        self.dropped = []  # the fixture's cleanup, when it is allowed to run

    def hold_database(self, name, *, reason, worker_pid=None):
        self.holds.append((name, reason, worker_pid))
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(name=name, reason=reason, worker_pid=worker_pid)

    def note_spawn_attempt(self, name):
        self.attempts.append(name)
        if self.attempt_failure is not None:
            raise self.attempt_failure
        return SimpleNamespace(
            name=name, reason=HELD_UNCONFIRMED_WORKER_EXIT, worker_pid=None
        )

    def note_held_process(self, name, *, process):
        self.processes.append((name, process))
        if self.association_failure is not None:
            raise self.association_failure
        return SimpleNamespace(
            name=name,
            reason=HELD_UNCONFIRMED_WORKER_EXIT,
            worker_pid=getattr(process, "pid", None),
        )

    def note_held_worker(self, name, *, worker_pid):
        self.associations.append((name, worker_pid))
        return SimpleNamespace(
            name=name, reason=HELD_UNCONFIRMED_WORKER_EXIT, worker_pid=worker_pid
        )

    def release_hold(self, name, *, reason, worker_exit=None):
        self.releases.append((name, reason, worker_exit))
        if self.release_failure is not None:
            raise self.release_failure
        return SimpleNamespace(
            name=name, reason=HELD_UNCONFIRMED_WORKER_EXIT, worker_pid=None
        )

    def drop_database(self, name):
        self.dropped.append(name)


def _deferral_for(
    failure=None, release_failure=None, association_failure=None, attempt_failure=None
):
    run = _RecordingRun(
        failure=failure,
        release_failure=release_failure,
        association_failure=association_failure,
        attempt_failure=attempt_failure,
    )
    return _Deferral(run=run, name=HOLD_NAME), run


def _unconfirmed_child(*, the_body_finishes=True, attempt=None):
    """A real worker whose exit the lifecycle cannot confirm. The caller ends it.

    With `the_body_finishes` (the default), the body's own wait (`_wait_for_the_handover`)
    believes it finished -- it reports 0 -- while the process is still running; the waits
    that would confirm the exit, and the kill, all fail. When the body never reaches that
    wait (a failure before it), every wait fails. `attempt` is the lifecycle's record: when
    it is supplied, the real `_spawn` records the attempt and associates the process with the
    held database before this function sabotages the wrapper's waits."""
    child = _offline_child(SLEEPING_CHILD, attempt=attempt)
    real_wait, real_kill = child.wait, child.kill
    waits = []

    def the_exit_is_never_confirmed(*, timeout=None):
        waits.append(timeout)
        if the_body_finishes and len(waits) == 1:
            return 0  # the body believes it finished; it is in fact still running
        raise WaitFailed("the wait could not confirm the exit")

    def the_signal_fails():
        raise KillFailed("the process could not be signalled")

    child.wait = the_exit_is_never_confirmed
    child.kill = the_signal_fails
    return child, (real_wait, real_kill)


def _end_the_leftover(child, real) -> None:
    """The test ends the process it deliberately left to the fixture's judgement."""
    real_wait, real_kill = real
    child.wait, child.kill = real_wait, real_kill
    real_kill()
    real_wait(timeout=10)
    shutil.rmtree(child.directory, ignore_errors=True)


def _review(*, child_factory, deferral, observe=None, engine=None, budget=WORKER_BOUND):
    return _hand_over_under_review(
        lambda: None,
        child_factory,
        observe or (lambda worker: None),
        admission=_Admission(StubAdmissionTransaction()),
        engine=engine if engine is not None else StubEngine(children=[]),
        budget=budget,
        defer=deferral,
    )


def _fake_server_run():
    """A real `Run` over the offline server the harness is tested against, so the held
    database's protection is exercised by the actual `Run` state machine -- not by a stub of
    it -- and the teardown functions below can be driven against the same instance."""
    target = validate_target(BASE, {})
    server = FakeServer(target)
    run = Run(
        target,
        connect=server.connect,
        token=TOKEN,
        cluster=cluster_identity(),
        docker=server.docker,
        environ={},
    )
    return run, server


def test_the_deferral_holds_the_database_with_the_very_run_that_owns_it():
    deferral, run = _deferral_for()

    assert deferral.run is run  # the session's Run, not a copy
    assert deferral.must_drop() is True  # nothing was armed: the spawn path is not entered

    deferral.arm()

    assert run.holds == [(HOLD_NAME, HELD_UNCONFIRMED_WORKER_EXIT, None)]
    assert deferral.armed is True and deferral.must_drop() is False

    deferral.record_attempt()  # recorded with the run BEFORE `Popen`
    assert run.attempts == [HOLD_NAME]

    deferral.note_worker(4321)  # the PID that became known after arming

    assert run.associations == [(HOLD_NAME, 4321)]
    assert deferral.must_drop() is False  # associating a worker never removes protection

    deferral.release(reason=RELEASED_WORKER_EXIT_CONFIRMED)

    assert run.releases == [(HOLD_NAME, RELEASED_WORKER_EXIT_CONFIRMED, None)]
    assert deferral.released is True and deferral.must_drop() is True


def test_the_hold_exists_before_popen_is_reached():
    """The pre-armed state, observed from inside the start thunk: the run already holds the
    database, and no spawn attempt has been recorded yet, before the worker can exist."""
    deferral, run = _deferral_for()
    observed = []

    def start(attempt):
        observed.append((deferral.armed, list(run.holds), attempt.attempted))
        return _offline_child(QUICK_CHILD, attempt=attempt)

    _review(child_factory=start, deferral=deferral)

    [(armed, holds, attempted)] = observed
    assert armed is True  # HELD before the spawn path
    assert holds == [(HOLD_NAME, HELD_UNCONFIRMED_WORKER_EXIT, None)]
    assert attempted is False  # `_spawn` records the attempt immediately before `Popen`
    assert run.releases == [(HOLD_NAME, RELEASED_WORKER_EXIT_CONFIRMED, None)]


def test_the_attempt_is_recorded_with_the_run_before_popen_is_called(monkeypatch):
    """Observed from inside `Popen` itself: the run already holds the spawn attempt when the
    call that may create the process is made."""
    deferral, run = _deferral_for()
    observed = []
    real_popen = subprocess.Popen

    def watching_popen(*args, **kwargs):
        observed.append((list(run.attempts), deferral.attempt_record is not None))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", watching_popen)

    def start(attempt):
        return _offline_child(QUICK_CHILD, attempt=attempt)

    _review(child_factory=start, deferral=deferral)

    [(attempts, recorded)] = observed
    assert attempts == [HOLD_NAME] and recorded is True
    assert run.releases == [(HOLD_NAME, RELEASED_WORKER_EXIT_CONFIRMED, None)]


def test_a_hold_that_cannot_be_armed_stops_the_worker_from_being_started(recorded_processes):
    """The first defect, closed: registration can no longer fail after a worker exists,
    because it happens BEFORE the spawn path. A refused arming stops the lifecycle there --
    `start` is never called, so `Popen` is never reached -- and no worker is left unheld."""
    deferral, run = _deferral_for(failure=TrackingBroken("the run's records are unusable"))
    invocations = []

    def start(attempt):
        invocations.append(attempt)
        return _offline_child(QUICK_CHILD, attempt=attempt)

    with pytest.raises(HoldRegistrationFailed) as reported:
        _review(child_factory=start, deferral=deferral)

    assert reported.value.__cause__ is run.failure
    assert invocations == []  # the spawn path was not entered
    assert recorded_processes == []  # `Popen` was never invoked
    assert run.holds == [(HOLD_NAME, HELD_UNCONFIRMED_WORKER_EXIT, None)]
    assert deferral.armed is False and deferral.arm_failure is run.failure
    # No worker was attempted, so the run that owns the database cleans it up as usual.
    assert deferral.must_drop() is True


def test_a_confirmed_exit_releases_the_hold_and_leaves_the_database_droppable():
    deferral, run = _deferral_for()
    child = _offline_child(QUICK_CHILD)

    _review(child_factory=lambda attempt: child, deferral=deferral)

    assert run.holds == [(HOLD_NAME, HELD_UNCONFIRMED_WORKER_EXIT, None)]
    assert run.releases == [(HOLD_NAME, RELEASED_WORKER_EXIT_CONFIRMED, None)]
    assert deferral.released is True and deferral.must_drop() is True


def test_a_failure_before_the_spawn_is_a_release_with_positive_evidence():
    """No worker was ever attempted: `_spawn` was not reached, so the attempt record stays
    false and the hold is released through that separately verified path."""
    deferral, run = _deferral_for()

    def start(attempt):
        raise StartFailed("the worker's configuration could not be written")

    with pytest.raises(StartFailed):
        _review(child_factory=start, deferral=deferral)

    assert run.associations == []  # nothing was started, so no PID became known
    assert run.releases == [(HOLD_NAME, RELEASED_SPAWN_NEVER_ATTEMPTED, None)]
    assert deferral.released is True and deferral.must_drop() is True


def test_a_spawn_preparation_that_fails_before_popen_releases_on_the_attempt_record(
    tmp_path, monkeypatch
):
    """`_spawn` itself failed before the call: its log could not be opened, and that is
    positively before `Popen` -- the attempt record stays false, so the release is the
    separately verified "never attempted" one."""
    deferral, run = _deferral_for()
    directory = tmp_path / "worker"
    directory.mkdir()
    failure = StartFailed("the worker's log could not be opened")

    def log_that_cannot_be_opened(path):
        raise failure

    monkeypatch.setitem(globals(), "_open_the_worker_log", log_that_cannot_be_opened)

    def start(attempt):
        return _spawn(QUICK_CHILD, directory=directory, attempt=attempt)

    with pytest.raises(StartFailed) as reported:
        _review(child_factory=start, deferral=deferral)

    assert reported.value is failure
    assert run.releases == [(HOLD_NAME, RELEASED_SPAWN_NEVER_ATTEMPTED, None)]
    assert deferral.must_drop() is True


def test_a_popen_that_raises_without_a_handle_keeps_the_database_held(tmp_path, monkeypatch):
    """The second defect, closed: `Popen` was invoked and raised, and no handle came back --
    which is NOT evidence that no worker exists. The hold stays, and neither teardown may
    drop the database."""
    deferral, run = _deferral_for()
    failure = PopenFailed("the process could not be started")
    directory = tmp_path / "worker"
    directory.mkdir()

    def popen_that_loses_the_handle(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "Popen", popen_that_loses_the_handle)

    def start(attempt):
        return _spawn([sys.executable, "-c", "pass"], directory=directory, attempt=attempt)

    with pytest.raises(PopenFailed) as reported:
        _review(child_factory=start, deferral=deferral)

    assert reported.value is failure
    assert run.releases == []  # nothing establishes that no worker exists
    assert run.holds == [(HOLD_NAME, HELD_UNCONFIRMED_WORKER_EXIT, None)]
    assert deferral.armed is True and deferral.released is False
    assert deferral.must_drop() is False


def test_a_normal_confirmed_exit_removes_the_temporary_files():
    """The ordinary path: the worker ended by itself, its exit was confirmed, and only then
    is the directory that carried the run's password removed."""
    deferral, run = _deferral_for()
    made = {}

    def start(attempt):
        made["child"] = _offline_child(QUICK_CHILD, attempt=attempt)
        return made["child"]

    _review(child_factory=start, deferral=deferral)

    child = made["child"]
    assert child.exited()
    assert not child.directory.exists()  # removed by the wrapper after the confirmed exit
    assert deferral.released is True and deferral.must_drop() is True


def test_a_spawn_is_not_entered_when_the_attempt_cannot_be_recorded(recorded_processes):
    """The evidence must exist before the spawn, not after it: if the run refuses to record
    the attempt, `Popen` is never called. The run recorded nothing, so the safe release is
    the one for a spawn that never happened -- and the directory is ordinary cleanup."""
    deferral, run = _deferral_for(attempt_failure=TrackingBroken("the run refused the attempt"))

    def start(attempt):
        return _start_the_handover(
            _scratch_for_starting(),
            first_owner=UUID(int=1),
            second_owner=UUID(int=2),
            attempt=attempt,
        )

    with pytest.raises(TrackingBroken) as reported:
        _review(child_factory=start, deferral=deferral)

    assert reported.value is run.attempt_failure
    assert run.attempts == [HOLD_NAME]  # the recording was asked for
    assert recorded_processes == []  # `Popen` was never invoked
    assert deferral.attempt_failure is run.attempt_failure
    assert run.releases == [(HOLD_NAME, RELEASED_SPAWN_NEVER_ATTEMPTED, None)]
    assert deferral.must_drop() is True


def test_the_process_is_associated_before_a_failing_log_close(
    tmp_path, monkeypatch, recorded_processes
):
    """The handle reaches the run BEFORE the first fallible post-spawn step. The log close
    fails, the process is ended and its exit confirmed, and the run already holds the handle
    that proves it."""
    deferral, run = _deferral_for()
    deferral.arm()
    attempt = _SpawnAttempt(hold=deferral)
    directory, credentials = _a_directory_with_a_password(tmp_path)
    command, beat = _heartbeat(directory)
    failure = LogCloseFailed("closing the worker's log failed")

    def open_a_log_that_cannot_be_closed(path):
        return _LogWhoseCloseFails(open(path / "handover.log", "wb"), failure)

    monkeypatch.setitem(globals(), "_open_the_worker_log", open_a_log_that_cannot_be_closed)

    with pytest.raises(LogCloseFailed) as reported:
        _spawn(command, directory=directory, attempt=attempt)

    assert reported.value is failure
    assert run.attempts == [HOLD_NAME]  # recorded before `Popen`
    [process] = recorded_processes
    assert run.processes == [(HOLD_NAME, process)]  # associated before the log close
    assert process.poll() is not None and _beat_has_stopped(beat)
    assert credentials.exists()
    shutil.rmtree(directory, ignore_errors=True)


def test_a_failure_after_the_spawn_releases_only_after_the_exit_is_confirmed(
    tmp_path, monkeypatch, recorded_processes
):
    """`Popen` returned a child and the wrapper could not be built. No wrapper ever reaches
    the lifecycle -- but `_spawn` ended the process it owned and waited for it, so the exit
    IS positively confirmed and the hold may be released on that status."""
    deferral, run = _deferral_for()
    failure = WrapperConstructionFailed("the wrapper could not be built")
    directory = tmp_path / "worker"
    directory.mkdir()

    class Exploding:
        def __init__(self, *args, **kwargs):
            raise failure

    monkeypatch.setitem(globals(), "_HandoverProcess", Exploding)

    def start(attempt):
        return _spawn(QUICK_CHILD, directory=directory, attempt=attempt)

    with pytest.raises(WrapperConstructionFailed) as reported:
        _review(child_factory=start, deferral=deferral)

    assert reported.value is failure
    [process] = recorded_processes
    assert process.poll() is not None  # the operating system says it ended
    assert run.releases == [
        (HOLD_NAME, RELEASED_WORKER_EXIT_CONFIRMED, None)
    ]
    assert deferral.must_drop() is True


def test_a_spawn_whose_exit_cannot_be_confirmed_keeps_the_database_held(
    tmp_path, monkeypatch, recorded_processes
):
    """The wrapper could not be built AND the raw process it belonged to could not be
    confirmed ended. No exit status is recorded, so the hold stays: the one thing that would
    have released it is exactly what is missing."""
    deferral, run = _deferral_for()
    directory = tmp_path / "worker"
    directory.mkdir()

    class Exploding:
        def __init__(self, *args, **kwargs):
            raise WrapperConstructionFailed("the wrapper could not be built")

    monkeypatch.setitem(globals(), "_HandoverProcess", Exploding)

    def end_without_confirmation(self):
        raise HandoverProcessNotStopped("the exit could not be confirmed", worker_pid=4321)

    monkeypatch.setattr(_Spawned, "end", end_without_confirmation)

    def start(attempt):
        return _spawn(QUICK_CHILD, directory=directory, attempt=attempt)

    with pytest.raises(HandoverProcessNotStopped) as reported:
        _review(child_factory=start, deferral=deferral)

    assert reported.value.worker_pid == 4321
    assert run.releases == []
    assert run.holds == [(HOLD_NAME, HELD_UNCONFIRMED_WORKER_EXIT, None)]
    assert deferral.armed is True and deferral.must_drop() is False

    [process] = recorded_processes  # reap the child the simulated failure left behind
    process.wait(timeout=10)


def test_a_terminated_overrun_leaves_the_scratch_database_droppable():
    deferral, run = _deferral_for()
    child = _offline_child(SLEEPING_CHILD)

    with pytest.raises(subprocess.TimeoutExpired):
        _review(child_factory=lambda attempt: child, deferral=deferral, budget=SHORT_BUDGET)

    assert child.exited()  # the exit was confirmed after the termination
    assert run.releases == [
        (HOLD_NAME, RELEASED_WORKER_EXIT_CONFIRMED, None)
    ]
    assert deferral.released is True and deferral.must_drop() is True


def test_a_failing_test_still_releases_the_hold_after_the_confirmed_exit():
    """The test's own assertion failed; the worker had exited. The database is no longer in
    use, so the hold is released and the fixture's normal cleanup proceeds."""
    deferral, run = _deferral_for()
    child = _offline_child(QUICK_CHILD)

    def observe(worker):
        raise ObservationFailed("the lock observation failed")

    with pytest.raises(ObservationFailed):  # the test's own failure stays the report
        _review(child_factory=lambda attempt: child, deferral=deferral, observe=observe)

    assert child.exited()
    assert run.releases == [(HOLD_NAME, RELEASED_WORKER_EXIT_CONFIRMED, None)]
    assert deferral.must_drop() is True


def test_an_unconfirmed_exit_keeps_the_hold_and_names_the_worker_process():
    deferral, run = _deferral_for()
    child, real = _unconfirmed_child()

    with pytest.raises(HandoverProcessNotStopped):
        _review(child_factory=lambda attempt: child, deferral=deferral, budget=SHORT_BUDGET)

    assert run.holds == [(HOLD_NAME, HELD_UNCONFIRMED_WORKER_EXIT, None)]
    assert run.associations == [(HOLD_NAME, child.process.pid)]
    assert run.releases == []  # no exit was confirmed: nothing released it
    assert deferral.armed is True and deferral.must_drop() is False

    _end_the_leftover(child, real)


def test_the_tests_own_failure_does_not_release_the_hold():
    deferral, run = _deferral_for()

    def observe(worker):
        raise ObservationFailed("the lock observation failed")

    child, real = _unconfirmed_child(the_body_finishes=False)

    with pytest.raises(ObservationFailed):  # the test's own failure stays the report
        _review(
            child_factory=lambda attempt: child,
            deferral=deferral,
            observe=observe,
            budget=SHORT_BUDGET,
        )

    assert run.releases == []
    assert deferral.armed is True and deferral.must_drop() is False
    assert run.associations == [(HOLD_NAME, child.process.pid)]

    _end_the_leftover(child, real)


def test_a_start_time_unconfirmed_exit_also_keeps_the_hold():
    """The post-spawn path: `Popen` was reached, no `_HandoverProcess` was returned, and the
    start's own `HandoverProcessNotStopped` is the only evidence. The absent wrapper proves
    nothing, so the hold stays."""
    deferral, run = _deferral_for()
    unconfirmed = HandoverProcessNotStopped("the exit could not be confirmed", worker_pid=4321)

    def start(attempt):
        attempt.record()  # what `_spawn` does immediately before `Popen`
        raise unconfirmed

    with pytest.raises(HandoverProcessNotStopped) as reported:
        _review(child_factory=start, deferral=deferral)

    assert reported.value is unconfirmed
    assert run.releases == []
    assert deferral.armed is True and deferral.must_drop() is False


def test_an_unconfirmed_start_names_the_worker_process_it_could_not_end():
    """The raw process the start could not end is the one whose PID the record must carry:
    the wrapper that would normally know it does not exist on that path."""

    class _NeverEnds:
        pid = 4321

        def poll(self):
            return None

        def kill(self):
            pass

        def wait(self):
            raise WaitFailed("the wait could not confirm the exit")

    with pytest.raises(HandoverProcessNotStopped) as reported:
        _Spawned(_NeverEnds()).end()

    assert reported.value.worker_pid == 4321


def test_failing_diagnostics_cannot_release_the_hold_prematurely(monkeypatch):
    """A logger that raises must not decide anything: the hold was armed before the spawn,
    and no diagnostic can stand in for a confirmed exit."""
    logger_that_fails = _LoggerThatFails(LoggingFailed("logging failed"))
    monkeypatch.setitem(globals(), "logger", logger_that_fails)
    deferral, run = _deferral_for()
    child, real = _unconfirmed_child()

    with pytest.raises(HandoverProcessNotStopped):
        _review(child_factory=lambda attempt: child, deferral=deferral, budget=SHORT_BUDGET)

    assert logger_that_fails.attempts > 0  # a diagnostic was attempted, and raised
    assert run.releases == []  # nothing released it
    assert deferral.armed is True and deferral.must_drop() is False

    _end_the_leftover(child, real)


def test_a_hold_that_cannot_be_released_stays_held_and_is_reported():
    """The exit was confirmed and the run refused the release. The database is still
    protected -- the fixture's drop refuses it, and so does the session's `close()` -- and
    the refusal is the report."""
    deferral, run = _deferral_for(release_failure=TrackingBroken("the run refused the release"))
    child = _offline_child(QUICK_CHILD)

    with pytest.raises(HoldReleaseFailed) as reported:
        _review(child_factory=lambda attempt: child, deferral=deferral)

    assert reported.value.__cause__ is run.release_failure
    assert run.releases == [(HOLD_NAME, RELEASED_WORKER_EXIT_CONFIRMED, None)]
    assert deferral.release_failure is run.release_failure
    assert deferral.armed is True and deferral.released is False
    assert deferral.must_drop() is False  # never dropped on a claim that it is protected


def test_a_failed_release_does_not_displace_the_tests_own_failure(caplog):
    caplog.set_level(logging.WARNING)
    deferral, run = _deferral_for(release_failure=TrackingBroken("the run refused the release"))
    child = _offline_child(QUICK_CHILD)

    def observe(worker):
        raise ObservationFailed("the lock observation failed")

    with pytest.raises(ObservationFailed):  # the test's own failure stays the report
        _review(child_factory=lambda attempt: child, deferral=deferral, observe=observe)

    assert deferral.must_drop() is False
    assert "the hold could not be released" in caplog.text  # the refusal is reported


def test_the_fixture_teardown_drops_only_after_a_release():
    """The fixture's decision, driven offline: a held database is never passed to
    `drop_database`; a released one is; and an arming that was refused -- before any spawn
    attempt -- leaves nothing to protect."""

    class _RunThatDrops:
        def __init__(self):
            self.dropped = []

        def hold_database(self, name, *, reason, worker_pid=None):
            return SimpleNamespace(name=name, reason=reason, worker_pid=worker_pid)

        def note_spawn_attempt(self, name):
            return SimpleNamespace(
                name=name, reason=HELD_UNCONFIRMED_WORKER_EXIT, worker_pid=None
            )

        def note_held_worker(self, name, *, worker_pid):
            return SimpleNamespace(
                name=name, reason=HELD_UNCONFIRMED_WORKER_EXIT, worker_pid=worker_pid
            )

        def release_hold(self, name, *, reason, worker_exit=None):
            return SimpleNamespace(name=name, reason=reason, worker_pid=None)

        def drop_database(self, name):
            self.dropped.append(name)

    released = _Deferral(run=_RunThatDrops(), name=HOLD_NAME)
    released.arm()
    released.release(reason=RELEASED_WORKER_EXIT_CONFIRMED)
    _release_the_handover_scratch(released.run, HOLD_NAME, released)
    assert released.run.dropped == [HOLD_NAME]  # the ordinary path still cleans up

    never_attempted = _Deferral(run=_RunThatDrops(), name=HOLD_NAME)
    never_attempted.arm()
    never_attempted.release(reason=RELEASED_SPAWN_NEVER_ATTEMPTED)
    _release_the_handover_scratch(never_attempted.run, HOLD_NAME, never_attempted)
    assert never_attempted.run.dropped == [HOLD_NAME]

    held = _Deferral(run=_RunThatDrops(), name=HOLD_NAME)
    held.arm()
    _release_the_handover_scratch(held.run, HOLD_NAME, held)
    assert held.run.dropped == []  # never while the hold exists

    refused = _Deferral(run=_RecordingRun(failure=TrackingBroken("unusable")), name=HOLD_NAME)
    with pytest.raises(HoldRegistrationFailed):
        refused.arm()
    _release_the_handover_scratch(refused.run, HOLD_NAME, refused)
    # The spawn path was never entered, so no worker can be using it.
    assert refused.must_drop() is True


# --- R7: the held database against the REAL `Run` state machine ---------------------------------------------------
#
# The tests above drive the lifecycle with a recording stand-in for the run. These drive the
# actual `Run` that the session's `pg_run` fixture provides -- the same instance the
# function-level teardown and the session finalizer share -- over the offline FakeServer the
# harness itself is tested against. Nothing here connects anywhere: the database's protection
# is a state machine, not a server.


def test_a_held_database_is_refused_by_every_teardown_path_until_it_is_released():
    run, server = _fake_server_run()
    name = run.create_database()
    deferral = _Deferral(run=run, name=name)
    deferral.arm()  # before any worker: this is the pre-armed state

    # The function-level teardown (`handover_scratch_rc`'s `finally`) refuses.
    _release_the_handover_scratch(run, name, deferral)
    assert name in server.databases

    # A direct drop, the generic close and the failed-setup cleanup all refuse.
    with pytest.raises(UnsafeTarget) as dropped:
        run.drop_database(name)
    assert dropped.value.code == "held_database"
    with pytest.raises(UnsafeTarget) as closed:
        run.close()
    assert closed.value.code == "held_database"
    with pytest.raises(UnsafeTarget) as setup:
        run._cleanup_after_failed_setup()
    assert setup.value.code == "held_database"
    assert name in server.databases
    assert [record.name for record in run.pending_cleanup()["held"]] == [name]

    # Only the explicit release, on the Run's own evidence, re-enables cleanup.
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    deferral.record_attempt()  # before `Popen`: the run records the attempt
    deferral.record_process(process)  # the actual handle, not a caller's status
    assert run.pending_cleanup()["held"][0].spawn_attempted is True
    deferral.release(reason=RELEASED_WORKER_EXIT_CONFIRMED)
    _release_the_handover_scratch(run, name, deferral)
    assert name not in server.databases
    run.close()  # nothing left: no refusal


def test_an_unconfirmed_exit_keeps_both_the_fixture_and_the_session_from_dropping():
    run, server = _fake_server_run()
    name = run.create_database()
    deferral = _Deferral(run=run, name=name)
    children = []
    engine = StubEngine(children=children)
    made = {}

    def start(attempt):
        child, real = _unconfirmed_child(attempt=attempt)
        made["child"], made["real"] = child, real
        children.append(child)
        return child

    with pytest.raises(HandoverProcessNotStopped):
        _hand_over_under_review(
            lambda: None,
            start,
            lambda worker: None,
            admission=_Admission(StubAdmissionTransaction()),
            engine=engine,
            budget=SHORT_BUDGET,
            defer=deferral,
        )

    child, real = made["child"], made["real"]
    _release_the_handover_scratch(run, name, deferral)  # the function-level teardown
    assert name in server.databases
    with pytest.raises(UnsafeTarget) as closed:  # the session finalizer
        run.close()
    assert closed.value.code == "held_database"
    assert name in server.databases
    assert engine.disposed is False
    [record] = run.pending_cleanup()["held"]
    assert record.worker_pid == child.process.pid  # the process that may still be alive
    assert record.spawn_attempted is True

    _end_the_leftover(child, real)


# --- a restart ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["completed", "crashed"])
async def test_a_restart_on_the_same_database_does_not_open_the_update_to_the_new_owner(tmp_path, monkeypatch, how):
    update = private_message(UPDATE, MOTHER, QUESTION)

    async with telegram_app(tmp_path, ScriptedBackend("Икра из кабачков." if how == "completed" else TimeoutError())) as first:
        other = await first.chat.second_user()
        await first.pair(MOTHER)
        if how == "crashed":
            with unrecorded_failure(monkeypatch):
                await first.deliver_update(update)
        else:
            await first.deliver_update(update)
        second_id = second_person(first)
        await hand_over(first, other)

    # A new process: nothing in memory, the same database. Telegram brings the update again.
    after = ScriptedBackend("Этот ответ запрашивать не следовало")
    async with telegram_app(
        tmp_path, after, sign_in_first=False, seed=False, database=first.database
    ) as second:
        second.chat.clock.advance(minutes=10)
        before = len(second.bot.sent)

        await second.deliver_update(update)
        await second.settle()

        assert after.calls == 0  # no provider call
        assert second.bot.texts(MOTHER)[before:] == [tc.BINDING_CHANGED]
        assert second.conversations(second_id) == []
        assert [turn.idempotency_key for turn in second.all_turns()] == [store.update_key(UPDATE)]
        assert len(second.chat.rows(Message)) == (2 if how == "completed" else 1)  # the first owner's, nothing more
