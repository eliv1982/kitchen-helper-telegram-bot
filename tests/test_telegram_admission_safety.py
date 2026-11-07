"""KH-TG-DLG-01, the final admission-safety corrections (third independent review).

Finding 1 (fail-open cutover initialization): the ledger used to be prepared best-effort, so
a failure left the cutover unknown and new messages went on to be admitted with the
pre-migration rule silently switched off. Now the ledger has an explicit readiness state
(``LedgerState``); nothing is admitted until a preparation has committed, polling reads and
acknowledges nothing meanwhile, a failed preparation leaves no half-written ledger, and the
cutover written by a retry is the process start (so messages that arrived during the failure
are not refused as pre-migration ones).

Finding 2 (expired-marker deletion race): an update the first look saw as ADMITTED_HERE must
not become a fresh message when its conversation (and turn request) is deleted before the
resolution; the provenance travels with the attempt, and the timestamp policy is asked
independently of the ledger.

Finding 3 (unbounded future timestamps): the timestamp policy has a 24 h + 5 min past bound,
a 5 min future bound, fails closed on missing/malformed dates, and is re-asked at the claim.

The genuine lost-acknowledgement polling reproduction is
tests/test_telegram_dedup.py::test_a_lost_acknowledgement_leaves_the_update_pending_and_the_restart_refuses_it.
"""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa

from house_helper.admin.retention import purge_expired
from house_helper.persistence import conversations as cv
from house_helper.persistence import telegram as store
from house_helper.persistence.models import TelegramLink, TelegramUpdateAdmission
from house_helper.web import telegram_chat as tc
from tests.chat_support import ScriptedBackend
from tests.photo_support import jpeg
from tests.telegram_support import (
    MOTHER,
    FakeBot,
    TelegramHarness,
    photo_message,
    private_message,
    telegram_app,
    until,
)

pytestmark = pytest.mark.asyncio

QUESTION = "Что приготовить из кабачков?"


# --- local helpers (the same idioms as tests/test_telegram_dedup.py) ----------------------


def delete_conversation(t: TelegramHarness, conversation_id) -> None:
    with t.chat.base.factory() as db, db.begin():
        cv.delete_conversation(db, t.user_id, conversation_id)


def select(t: TelegramHarness, conversation_id) -> None:
    with t.chat.base.factory() as db, db.begin():
        store.select_conversation(
            db, t.user_id, UUID(str(conversation_id)), now=t.chat.clock()
        )


def selection(t: TelegramHarness):
    with t.chat.base.factory() as db:
        return store.active_conversation(db, t.user_id)


def update_markers(t: TelegramHarness) -> list[TelegramUpdateAdmission]:
    return [row for row in t.chat.rows(TelegramUpdateAdmission) if row.update_id != 0]


def purge_markers(t: TelegramHarness) -> None:
    with t.chat.base.factory() as db, db.begin():
        db.execute(sa.delete(TelegramUpdateAdmission).where(TelegramUpdateAdmission.update_id != 0))


def link_account(t: TelegramHarness, account: int = MOTHER) -> None:
    """The bot's numeric account tied to the seeded user, without the pairing round trip.

    The pairing route refuses while the bot reports a failure (telegram_api.py), and these
    tests induce exactly that state; the tie itself is what a message needs."""
    with t.chat.base.factory() as db, db.begin():
        db.add(
            TelegramLink(
                user_id=t.user_id, telegram_user_id=account, created_at=t.chat.clock()
            )
        )


def age_cutover(t: TelegramHarness, moment) -> None:
    """Move the ledger's cutover before `moment`, as a process that has been running for days
    would have it. Written around the ORM (the guard is a mapper event), and mirrored into
    the bot so its next decision sees the same instant."""
    with t.chat.base.factory() as db, db.begin():
        db.execute(
            sa.update(TelegramUpdateAdmission)
            .where(TelegramUpdateAdmission.update_id == 0)
            .values(admitted_at=moment)
        )
    t.service._cutover_at = moment


# --- Finding 1: admission readiness -------------------------------------------------------


async def test_no_message_is_admitted_while_the_ledger_cannot_be_prepared(tmp_path, monkeypatch):
    """A failed preparation refuses every new message to the assistant with the fixed notice:
    no marker, conversation, message, turn or provider call. The next message retries, and a
    successful retry admits it normally."""
    real = store.initialize_update_admissions
    attempts = {"n": 0}

    def broken(*args, **kwargs):
        attempts["n"] += 1
        raise RuntimeError("the admission table is not there")

    monkeypatch.setattr(tc.store, "initialize_update_admissions", broken)
    backend = ScriptedBackend("Ответ после восстановления")
    async with telegram_app(tmp_path, backend) as t:
        link_account(t)  # the tie a message needs; pairing itself refuses while failure is set
        assert t.chat.rows(TelegramUpdateAdmission) == []  # not even the cutover row
        before = len(t.bot.sent)

        await t.deliver_update(private_message(t.next_id(), MOTHER, QUESTION))

        assert backend.calls == 0
        assert t.bot.texts(MOTHER)[before:] == [tc.TROUBLE]
        assert t.chat.rows(TelegramUpdateAdmission) == []
        assert t.conversations() == [] and t.all_turns() == [] and t.all_messages() == []
        assert t.service._ledger is tc.LedgerState.FAILED
        assert attempts["n"] >= 1

        monkeypatch.setattr(tc.store, "initialize_update_admissions", real)
        await t.deliver(MOTHER, "Сообщение после восстановления")
        assert t.service._ledger is tc.LedgerState.READY

    assert backend.calls == 1
    assert t.bot.texts(MOTHER)[-1] == "Ответ после восстановления"
    assert [turn.status for turn in t.all_turns()] == ["completed"]


async def test_polling_reads_and_acknowledges_nothing_until_the_ledger_is_ready(tmp_path, monkeypatch):
    """While preparation fails, the poller makes no getUpdates call at all. The update pushed
    to the fake server stays pending and is processed once a retry succeeds -- without the
    test re-injecting it. This is the acknowledgement-loss requirement: a failed preparation
    must not advance any offset."""
    real = store.initialize_update_admissions
    allow = {"ready": False}
    attempts = {"n": 0}

    def gated(*args, **kwargs):
        attempts["n"] += 1
        if not allow["ready"]:
            raise RuntimeError("the admission table is not there")
        return real(*args, **kwargs)

    monkeypatch.setattr(tc.store, "initialize_update_admissions", gated)
    bot = FakeBot()
    backend = ScriptedBackend("Ответ после восстановления")
    update = private_message(9401, MOTHER, "Сообщение во время сбоя")
    async with telegram_app(tmp_path, backend, bot=bot) as t:
        link_account(t)
        bot.push(update)
        await until(lambda: attempts["n"] >= 1)
        await asyncio.sleep(0.05)  # a mistaken poller would have asked by now
        assert bot.offsets == []  # nothing was read, so nothing was acknowledged
        assert [entry["update_id"] for entry in bot.pending] == [update["update_id"]]
        assert backend.calls == 0

        allow["ready"] = True
        await bot.wait_for_messages(1, MOTHER)  # the pending update's answer
        await until(
            lambda: any(
                offset is not None and offset > update["update_id"]
                for offset in bot.offsets
            )
        )

    assert backend.calls == 1
    assert bot.texts(MOTHER)[-1] == "Ответ после восстановления"


async def test_a_restart_after_a_failed_preparation_processes_the_pending_update(tmp_path, monkeypatch):
    """The process that could not prepare the ledger reads nothing; it stops; a new process on
    the same database and the same fake server prepares successfully and processes the update
    that was still pending (never acknowledged, never re-injected)."""
    real = store.initialize_update_admissions

    def broken(*args, **kwargs):
        raise RuntimeError("the admission table is not there")

    monkeypatch.setattr(tc.store, "initialize_update_admissions", broken)
    bot = FakeBot()
    update = private_message(9402, MOTHER, "Сообщение переживает перезапуск")
    backend = ScriptedBackend("Этот ответ запрашивать не следовало")
    owner = None
    async with telegram_app(tmp_path, backend, bot=bot) as first:
        owner = first.user_id
        link_account(first)
        bot.push(update)
        await asyncio.sleep(0.05)
        assert bot.offsets == [] and [entry["update_id"] for entry in bot.pending] == [
            update["update_id"]
        ]
        assert backend.calls == 0

    monkeypatch.setattr(tc.store, "initialize_update_admissions", real)
    after = ScriptedBackend("Ответ после перезапуска")
    before = len(bot.texts(MOTHER))
    async with telegram_app(tmp_path, after, bot=bot, sign_in_first=False, seed=False) as second:
        second.chat.clock.advance(minutes=10)
        await until(lambda: len(bot.texts(MOTHER)) > before)
        await second.settle()

        assert after.calls == 1
        assert second.bot.texts(MOTHER)[before:] == ["Ответ после перезапуска"]
        assert owner is not None and second.conversations(owner) and second.all_turns()
        assert [row.update_id for row in update_markers(second)] == [update["update_id"]]


async def test_the_website_keeps_serving_while_the_ledger_cannot_be_prepared(tmp_path, monkeypatch):
    """The bot's preparation is fail-closed, but the web application is not held back; the
    failure is visible through `failure` (a category, never a secret)."""

    def broken(*args, **kwargs):
        raise RuntimeError("the admission table is not there")

    monkeypatch.setattr(tc.store, "initialize_update_admissions", broken)
    async with telegram_app(tmp_path, ScriptedBackend(), sign_in_first=False) as t:
        await until(lambda: t.service.failure == "ledger")
        response = await t.chat.client.get("/healthz")

        assert response.status_code == 200
        assert "ledger" in t.service.failure  # the category only, never a secret


# --- Finding 2: provenance and the timestamp policy, together -----------------------------


async def test_the_49_hour_purge_then_deletion_race_is_refused_before_anything(tmp_path):
    """The exact review scenario: X completes, 49 hours pass, its marker is purged, the
    original turn request is retained, and X is redelivered. The status look would say
    ADMITTED_HERE and the conversation can be deleted before the resolution; the timestamp
    policy refuses the update first, so no replacement conversation and no provider call can
    happen at all."""
    backend = ScriptedBackend("Первый ответ", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        [first] = t.conversations()
        selected = await t.chat.create()
        select(t, selected)
        assert backend.calls == 1

        t.chat.clock.advance(hours=49)
        report = purge_expired(t.chat.app.state.database._engine, now=t.chat.clock())
        assert report.deleted["telegram_update_admissions"] >= 1
        assert update_markers(t) == []
        # The original request is still there (a year's retention): the first look would see it.
        [surviving] = t.all_turns()
        assert surviving.conversation_id == first.id and surviving.status == "completed"

        before = len(t.bot.sent)
        await t.deliver_update(update)

        assert backend.calls == 1  # the first delivery only
        assert t.bot.texts(MOTHER)[before:] == [tc.STALE_UPDATE]
        assert [c.id for c in t.conversations()] == [first.id, UUID(str(selected))]
        assert t.stored_texts(selected) == []  # nothing fell back to the selection
        assert selection(t).conversation_id == UUID(str(selected))


async def test_a_deletion_after_the_status_look_cannot_resurrect_an_admitted_update(
    tmp_path, monkeypatch
):
    """The first look sees the update's own turn request (ADMITTED_HERE); the conversation is
    deleted before the resolution, and the marker is already purged. The provenance is carried
    into the resolution, which refuses, so the update is not admitted into the selected
    conversation and the provider is never asked."""
    backend = ScriptedBackend("Первый ответ", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        [first] = t.conversations()
        selected = await t.chat.create()
        select(t, selected)
        purge_markers(t)  # the marker is gone; only the turn request proves the admission
        assert backend.calls == 1

        real = t.service._conversation_for

        async def delete_then_resolve(account, user_id, key, update_id, *, admitted_here=False):
            delete_conversation(t, first.id)
            return await real(
                account, user_id, key, update_id, admitted_here=admitted_here
            )

        monkeypatch.setattr(t.service, "_conversation_for", delete_then_resolve)
        before = len(t.bot.sent)
        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.ALREADY_PROCESSED]
        assert [c.id for c in t.conversations()] == [UUID(str(selected))]
        assert selection(t).conversation_id == UUID(str(selected))
        assert t.stored_texts(selected) == []
        assert t.all_turns() == []  # the request went with its conversation


async def test_a_deletion_between_the_resolution_and_the_claim_is_not_run_again(
    tmp_path, monkeypatch
):
    """The resolution found the update's own conversation (a redelivery); the conversation is
    deleted before the claim. The claim refuses, and the update is answered as a deleted
    conversation instead of being resolved again elsewhere. The marker is purged too, so only
    the resolution's provenance carries the fact."""
    backend = ScriptedBackend("Первый ответ", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        [first] = t.conversations()
        purge_markers(t)

        real = t.service._conversation_for

        async def resolve_then_delete(account, user_id, key, update_id, *, admitted_here=False):
            resolution = await real(
                account, user_id, key, update_id, admitted_here=admitted_here
            )
            if resolution is not None:
                delete_conversation(t, first.id)
            return resolution

        monkeypatch.setattr(t.service, "_conversation_for", resolve_then_delete)
        before = len(t.bot.sent)
        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.REDELIVERY_GONE]
        assert t.conversations() == []  # the original is gone; no replacement was made


async def test_after_re_pairing_the_49_hour_replay_leaves_the_new_owner_untouched(tmp_path):
    """The cross-owner half: the account changed hands after the update was admitted, its
    marker was purged and the message is outside the timestamp window. The refusal says
    nothing and the new owner gets no conversation, message, turn or provider call."""
    backend = ScriptedBackend("Первый ответ", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        second_id = next(iter(t.chat.extra_users.values()))
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        [first] = t.conversations()
        assert backend.calls == 1

        t.chat.clock.advance(hours=49)
        report = purge_expired(t.chat.app.state.database._engine, now=t.chat.clock())
        assert report.deleted["telegram_update_admissions"] >= 1
        await t.service.unlink(t.user_id)
        await t.pair(MOTHER, client=other)

        before = len(t.bot.sent)
        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.STALE_UPDATE]
        assert t.conversations(second_id) == []
        assert [turn.conversation_id for turn in t.all_turns()] == [first.id]


# --- Finding 3: the timestamp policy ------------------------------------------------------


@pytest.mark.parametrize(
    "offset, accepted",
    [
        (-(24 * 3600 + 5 * 60), True),
        (-(24 * 3600 + 5 * 60) - 1, False),
        (5 * 60, True),
        (5 * 60 + 1, False),
        (3 * 24 * 3600, False),
    ],
    ids=["past-boundary", "past-plus-one-second", "future-boundary", "future-plus-one-second", "three-days"],
)
async def test_the_timestamp_policy_boundaries(tmp_path, offset, accepted):
    """Exactly at each permitted boundary is admitted; one second beyond it is refused."""
    backend = ScriptedBackend("Ответ")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        # The startup preparation must have committed before the cutover is aged below: a
        # preparation still in flight would write its own (fresh) cutover over the test's.
        await until(lambda: t.service._ledger is tc.LedgerState.READY)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        update["message"]["date"] = int(t.chat.clock().timestamp()) + offset
        if offset < 0:
            # A ledger that has been running long enough for a day-old message to be inside
            # the timestamp window; otherwise the cutover policy would refuse it first.
            age_cutover(t, t.chat.clock() - timedelta(days=2))
        await t.deliver_update(update)

    if accepted:
        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[-1] == "Ответ"
        assert [turn.status for turn in t.all_turns()] == ["completed"]
    else:
        assert backend.calls == 0
        assert t.bot.texts(MOTHER)[-1] in (tc.STALE_UPDATE, tc.FUTURE_UPDATE)
        assert t.conversations() == [] and t.all_turns() == [] and update_markers(t) == []


@pytest.mark.parametrize(
    "date",
    [None, "yesterday", 1.5, float("inf"), float("nan"), True, 0, -5, 10**20],
    ids=["none", "text", "float", "inf", "nan", "bool", "zero", "negative", "beyond-datetime"],
)
async def test_missing_or_malformed_timestamps_fail_closed(tmp_path, date):
    backend = ScriptedBackend("Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver_update(private_message(t.next_id(), MOTHER, QUESTION, date=date))

    assert backend.calls == 0
    assert t.bot.texts(MOTHER)[-1] == tc.STALE_UPDATE
    assert t.conversations() == [] and t.all_turns() == [] and update_markers(t) == []


async def test_normal_text_and_photo_messages_are_accepted(tmp_path):
    backend = ScriptedBackend("Ответ на текст", "Ответ на фото")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        t.bot.files["photo-1"] = ("photos/photo-1.jpg", jpeg(4000, "d"))

        await t.deliver(MOTHER, "Текстовое сообщение")
        await t.deliver_update(
            photo_message(t.next_id(), MOTHER, "photo-1", caption="Что это?")
        )

    assert backend.calls == 2
    assert t.bot.texts(MOTHER)[-2:] == ["Ответ на текст", "Ответ на фото"]


async def test_a_future_dated_replay_after_the_marker_is_purged_is_refused(tmp_path):
    """The marker no longer knows the update and the conversation is gone; a future date must
    not make it admissible again once the ledger record has expired."""
    backend = ScriptedBackend("Первый ответ", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        [first] = t.conversations()
        delete_conversation(t, first.id)
        assert backend.calls == 1

        t.chat.clock.advance(hours=49)
        report = purge_expired(t.chat.app.state.database._engine, now=t.chat.clock())
        assert report.deleted["telegram_update_admissions"] >= 1
        update["message"]["date"] = int(t.chat.clock().timestamp()) + 3 * 24 * 3600

        before = len(t.bot.sent)
        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.FUTURE_UPDATE]
        assert t.conversations() == []  # no replacement was created


async def test_a_message_that_leaves_the_window_before_its_claim_is_refused(tmp_path, monkeypatch):
    """The policy is asked again in the claim's own transaction: a message that was fresh at
    the first look but stale by the time its attempt is prepared is refused, the claim rolls
    back whole and the conversation the resolution made is taken back."""
    backend = ScriptedBackend("Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        update["message"]["date"] = int(t.chat.clock().timestamp())

        real = t.service._conversation_for

        async def delayed(account, user_id, key, update_id, *, admitted_here=False):
            resolution = await real(
                account, user_id, key, update_id, admitted_here=admitted_here
            )
            t.chat.clock.advance(hours=25)  # the attempt waited past the horizon
            return resolution

        monkeypatch.setattr(t.service, "_conversation_for", delayed)
        before = len(t.bot.sent)
        await t.deliver_update(update)

        assert backend.calls == 0
        assert t.bot.texts(MOTHER)[before:] == [tc.STALE_UPDATE]
        assert t.conversations() == []  # the newly created one was taken back
        assert t.all_turns() == [] and update_markers(t) == []
