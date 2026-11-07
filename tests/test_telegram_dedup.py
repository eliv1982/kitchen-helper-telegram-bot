"""KH-TG-DLG-01, the remaining BLOCKER: an update Telegram delivers again after the
conversation (and its turn request) is gone must not be admitted again -- not in another
conversation of the same person, not in another person's history, not after a restart.

The durable record is persistence.telegram's admission ledger (telegram_update_admissions),
written in the transaction that claims the turn. These tests are the review's reproductions,
end to end through the real bot:

  * same owner: A answered X, B selected, A deleted, X redelivered;
  * cross owner: A answered X, A deleted, the account unlinked and linked to B, X redelivered;
  * the actual polling loop: the acknowledgement is lost, the conversation deleted, the
    process restarted, and the update is pushed through getUpdates again;
  * a photo: the redelivery is neither downloaded nor run;
  * the claim-level race: a look that (as if racing a deletion) sees nothing, a new
    conversation made, and the claim's own check refusing it and taking that conversation back;
  * two admissions for one update at once: one stands, the other is refused;
  * after the marker's window: the purge removes it and the timestamp policy still refuses
    the old update instead of admitting it as new;
  * the timestamp policy does not depend on the ledger: a message outside its window is
    refused even while its own completed turn survives;
  * the cutover policy: a pre-migration update with no provenance is refused.
"""

import asyncio
import threading
from datetime import timedelta
from uuid import UUID

import pytest

from house_helper.admin.retention import purge_expired
from house_helper.persistence import conversations as cv
from house_helper.persistence import telegram as store
from house_helper.persistence import turns
from house_helper.persistence.models import (
    Conversation,
    MessageAttachment,
    TelegramActiveConversation,
    TelegramUpdateAdmission,
)
from house_helper.persistence.base import RETENTION
from house_helper.web import telegram_chat as tc
from house_helper.web.turn_runner import TELEGRAM_CHANNEL, TurnNotAuthorized
from tests.chat_support import ScriptedBackend, reached
from tests.photo_support import jpeg
from tests.persistence_support import new_message, new_turn_request
from tests.telegram_support import (
    MOTHER,
    FakeBot,
    TelegramHarness,
    photo_message,
    private_message,
    telegram_app,
    until,
)
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

QUESTION = "Что приготовить из кабачков?"


def delete_conversation(t: TelegramHarness, conversation_id, user_id=None) -> None:
    with t.chat.base.factory() as db, db.begin():
        cv.delete_conversation(db, user_id or t.user_id, conversation_id)


def select(t: TelegramHarness, conversation_id) -> None:
    with t.chat.base.factory() as db, db.begin():
        store.select_conversation(
            db, t.user_id, UUID(str(conversation_id)), now=t.chat.clock()
        )


def selection(t: TelegramHarness):
    with t.chat.base.factory() as db:
        return store.active_conversation(db, t.user_id)


def admissions(t: TelegramHarness) -> list[TelegramUpdateAdmission]:
    return t.chat.rows(TelegramUpdateAdmission)


def update_markers(t: TelegramHarness) -> list[TelegramUpdateAdmission]:
    return [row for row in admissions(t) if row.update_id != 0]


def fetches(t: TelegramHarness) -> int:
    return len(
        [
            request
            for request in t.bot.requests
            if request.url.path.endswith("/getFile") or request.url.path.startswith("/file/")
        ]
    )


def dated(update: dict, moment) -> dict:
    update["message"]["date"] = int(moment.timestamp())
    return update


# --- the review's reproductions -----------------------------------------------------------------


async def test_a_redelivery_after_the_original_conversation_was_deleted_before_resolution(
    tmp_path,
):
    """The blocker exactly: X completed in A, B was selected, A was deleted BEFORE the
    redelivery was resolved. The old code found no turn request, treated X as new, and ran
    it in B. The ledger refuses it and no conversation is touched."""
    backend = ScriptedBackend("Первый ответ", "Второй ответ не должен быть запрошен")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        [first] = t.conversations()
        t.chat.clock.advance(seconds=5)

        selected = await t.chat.create()
        select(t, selected)
        delete_conversation(t, first.id)
        before = len(t.bot.sent)

        await t.deliver_update(update)

        assert backend.calls == 1  # the assistant was asked once, ever
        assert t.bot.texts(MOTHER)[before:] == [tc.ALREADY_PROCESSED]
        assert t.stored_texts(selected) == []  # nothing was written into the selected one
        assert selection(t).conversation_id == UUID(selected)  # the cursor was not used instead
        assert [c.id for c in t.conversations()] == [UUID(selected)]  # no conversation was created
        assert t.all_turns() == []  # the request went with the conversation; the marker stayed
        assert [row.update_id for row in admissions(t) if row.update_id] == [
            update["update_id"]
        ]


async def test_a_redelivery_after_the_account_changed_hands_and_the_conversation_was_deleted(
    tmp_path,
):
    """The other half of the review: A's conversation was deleted, the account was unlinked
    from A and linked to B, and X came again. B must get nothing of A's -- not the message,
    not a provider call, not a turn, not a conversation."""
    backend = ScriptedBackend("Первый ответ", "Ответ второму не должен запрашиваться")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, "Секрет первой: аллергия на орехи")
        await t.deliver_update(update)
        [first] = t.conversations()
        secret = t.stored_texts(first.id)

        delete_conversation(t, first.id)
        assert await t.service.unlink(t.user_id) is True
        # The same Telegram account, now tied to the second person.
        await t.pair(MOTHER, client=other)
        second_id = next(iter(t.chat.extra_users.values()))
        before = len(t.bot.sent)

        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.ALREADY_PROCESSED]
        assert "орех" not in tc.ALREADY_PROCESSED and "аллерг" not in tc.ALREADY_PROCESSED
        assert t.conversations(second_id) == []
        assert [
            message for message in t.all_messages() if message.conversation_id in {
                conversation.id for conversation in t.conversations(second_id)
            }
        ] == []
        assert t.all_turns() == []  # nothing was written for the second person
        # A's own history is gone with its conversation; the secret is nowhere.
        assert secret and "орех" in repr(secret)


@pytest.mark.parametrize("owner", ["same", "re-paired"])
async def test_a_lost_acknowledgement_leaves_the_update_pending_and_the_restart_refuses_it(
    tmp_path, owner
):
    """The review's reproduction, faithfully. The update is in the fake server's pending queue
    and the bot processes it; the getUpdates that would acknowledge it is blocked BEFORE the
    server applies the higher offset, so that acknowledgement is lost; the conversation is
    deleted (and, for the re-paired case, the account is linked to somebody else); the process
    stops and starts again against the same server and the same pending queue -- the update is
    not re-injected. Its next getUpdates returns it again, and the restart refuses it before
    any download, provider call, turn or conversation."""
    update_id = 9301
    backend = ScriptedBackend("Ответ", "Этот ответ запрашивать не следовало")
    bot = FakeBot()
    second_id = None
    async with telegram_app(tmp_path, backend, bot=bot) as first:
        other = await first.chat.second_user() if owner == "re-paired" else None
        if other is not None:
            second_id = next(iter(first.chat.extra_users.values()))
        await first.pair(MOTHER)
        bot.files["photo-1"] = ("photos/photo-1.jpg", jpeg(4000, "d"))
        update = photo_message(update_id, MOTHER, "photo-1", caption="Что это?")
        gate = bot.hold_acknowledgements()
        bot.push(update)
        await bot.wait_for_messages(2, MOTHER)  # the pairing's confirmation, then the answer
        [conversation] = first.conversations()
        assert backend.calls == 1 and fetches(first) == 2
        delete_conversation(first, conversation.id)
        if owner == "re-paired":
            await first.service.unlink(first.user_id)
            await first.pair(MOTHER, client=other)
        # The acknowledging call is at the server and has not been applied: the update it
        # would have dropped is still pending, exactly as a lost getUpdates leaves it.
        await until(
            lambda: any(
                offset is not None and offset > update["update_id"]
                for offset in bot.offsets
            )
        )
        assert [entry["update_id"] for entry in bot.pending] == [update["update_id"]]
        assert not gate.is_set()

    # A new process, the same fake server: nothing is re-injected. Its first getUpdates
    # carries no offset and returns the update the lost acknowledgement never dropped.
    bot.release_acknowledgements()
    before = len(bot.texts(MOTHER))
    after = ScriptedBackend("Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, after, bot=bot, sign_in_first=False, seed=False) as second:
        second.chat.clock.advance(minutes=10)
        await until(lambda: len(bot.texts(MOTHER)) > before)
        await second.settle()

    assert after.calls == 0
    assert second.bot.texts(MOTHER)[before:] == [tc.ALREADY_PROCESSED]
    assert fetches(second) == 2  # no second getFile, no second download
    if owner == "re-paired":
        assert second_id is not None and second.conversations(second_id) == []
    else:
        assert second.conversations() == []
    assert second.all_turns() == []  # refused before anything was stored


async def test_a_photo_redelivery_after_the_conversation_was_deleted_is_not_downloaded_again(
    tmp_path,
):
    backend = ScriptedBackend("Ответ", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        t.bot.files["photo-1"] = ("photos/photo-1.jpg", jpeg(4000, "d"))
        update = photo_message(t.next_id(), MOTHER, "photo-1", caption="Что это?")
        await t.deliver_update(update)
        [first] = t.conversations()
        assert fetches(t) == 2 and backend.calls == 1
        delete_conversation(t, first.id)
        before = len(t.bot.sent)

        await t.deliver_update(update)

        assert fetches(t) == 2  # no second getFile, no second download
        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.ALREADY_PROCESSED]
        assert t.chat.rows(MessageAttachment) == []  # went with the deleted conversation
        assert t.conversations() == []


async def test_the_claim_refuses_a_duplicate_and_takes_back_the_conversation_it_created(
    tmp_path, monkeypatch
):
    """Both early looks are blinded, as a deletion racing them would leave them. The resolution
    makes a new conversation; the claim's own transaction is what refuses the update, and the
    bot removes the empty conversation again (the person never asked for one)."""
    backend = ScriptedBackend("Первый ответ", "Этот ответ запрашивать не следовало")

    class BlindLedger:
        def __init__(self, real):
            self._real = real

        def update_admission_status(self, *args, **kwargs):
            return store.AdmissionStatus.NEW

        def __getattr__(self, name):
            return getattr(self._real, name)

    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        [first] = t.conversations()
        delete_conversation(t, first.id)
        before = len(t.bot.sent)

        monkeypatch.setattr(tc, "store", BlindLedger(store))

        async def blind_resolution(account, user_id, key, update_id, *, admitted_here=False):
            # The resolver as it was before the ledger: it cannot see that the update was
            # already admitted. (The early look was blinded too, so `admitted_here` is False
            # here; the point of this test is the claim's own check.)
            now = t.chat.clock()
            return await t.service._database.run_to_completion(
                lambda session: store.conversation_for_message(
                    session, user_id, key, now=now, telegram_user_id=account
                )
            )

        monkeypatch.setattr(t.service, "_conversation_for", blind_resolution)
        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.ALREADY_PROCESSED]
        assert t.conversations() == []  # the conversation the blind resolution made is gone
        assert t.chat.rows(TelegramActiveConversation) == []


async def test_two_admissions_for_one_update_at_once_leave_one_claim(tmp_path):
    """Two claims of the same update (same owner, two conversations) cannot both stand: the
    database's primary key is what decides, and the loser is rolled back whole."""
    backend = ScriptedBackend("Ответ первой", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        runner = t.chat.app.state.chat
        first_conversation = await t.chat.create()
        second_conversation = await t.chat.create()
        entered, proceed = threading.Event(), threading.Event()
        update_id = 9501

        def authorize(hold: bool):
            def check(session, started):
                existing = isinstance(started, turns.CompletedTurn) or bool(
                    getattr(started, "retry", False)
                )
                decision = store.admit_update(
                    session,
                    MOTHER,
                    t.user_id,
                    update_id,
                    bot_id=t.service._settings.bot_id,
                    now=t.chat.clock(),
                    existing=existing,
                )
                if hold:
                    entered.set()
                    assert proceed.wait(TIMEOUT), "the test never let the first claim go on"
                return decision is store.AdmissionOutcome.ADMITTED

            return check

        def submit(conversation_id, check):
            return asyncio.ensure_future(
                runner.submit(
                    user_id=t.user_id,
                    conversation_id=UUID(conversation_id),
                    idempotency_key=store.update_key(update_id),
                    text="Одно и то же сообщение",
                    correlation_id="hh-0123456789abcdef",
                    route="test",
                    origin=TELEGRAM_CHANNEL,
                    reclaim=False,
                    authorize=check,
                )
            )

        try:
            first = submit(first_conversation, authorize(hold=True))
            await reached(entered)
            rival = submit(second_conversation, authorize(hold=False))
            await asyncio.sleep(0.2)  # the absence of an event can only be given time
            assert not rival.done()  # it waits for the first transaction
        finally:
            proceed.set()

        assert (await asyncio.wait_for(first, TIMEOUT)).completed.assistant_message.text == (
            "Ответ первой"
        )
        with pytest.raises(TurnNotAuthorized):
            await asyncio.wait_for(rival, TIMEOUT)

        assert backend.calls == 1
        assert [row.update_id for row in admissions(t) if row.update_id] == [update_id]
        assert [turn.conversation_id for turn in t.all_turns()] == [UUID(first_conversation)]


# --- after the marker's window -------------------------------------------------------------------


async def test_after_the_marker_is_purged_an_old_update_is_refused_as_stale(tmp_path):
    """The marker lives 48 hours; the purge removes it; the message is then older than the
    24-hour replay horizon, and the stale policy refuses it instead of admitting it anew."""
    backend = ScriptedBackend("Первый ответ", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        [first] = t.conversations()
        delete_conversation(t, first.id)
        assert [row.update_id for row in admissions(t) if row.update_id] == [
            update["update_id"]
        ]

        t.chat.clock.advance(hours=49)
        report = purge_expired(t.chat.app.state.database._engine, now=t.chat.clock())
        assert report.deleted["telegram_update_admissions"] >= 1
        assert [row for row in admissions(t) if row.update_id] == []

        before = len(t.bot.sent)
        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[before:] == [tc.STALE_UPDATE]
        assert t.conversations() == []
        assert [row for row in admissions(t) if row.update_id] == []  # nothing was admitted


@pytest.mark.parametrize("older", [25, 48], ids=["just-over-24h", "two-days"])
async def test_an_update_older_than_the_replay_horizon_is_refused_before_any_download_or_call(
    tmp_path, older
):
    backend = ScriptedBackend("Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = dated(
            private_message(t.next_id(), MOTHER, QUESTION),
            t.chat.clock() - timedelta(hours=older),
        )

        await t.deliver_update(update)

    assert backend.calls == 0
    assert t.bot.texts(MOTHER)[-1] == tc.STALE_UPDATE
    assert t.conversations() == []
    assert t.all_turns() == []
    assert update_markers(t) == []


async def test_a_message_without_a_usable_date_is_refused(tmp_path):
    backend = ScriptedBackend("Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver_update(private_message(t.next_id(), MOTHER, QUESTION, date=None))

    assert backend.calls == 0
    assert t.bot.texts(MOTHER)[-1] == tc.STALE_UPDATE
    assert t.all_turns() == [] and update_markers(t) == []


async def test_an_admitted_update_older_than_the_horizon_is_refused_even_if_its_turn_survives(
    tmp_path,
):
    """The timestamp policy is asked of every message to the assistant, independently of the
    turn requests and markers (KHD-42 remediation). A message outside the window is refused
    even when its own completed turn survives, so the policy does not depend on which ledger
    record happens to still exist. No provider call is made either way."""
    backend = ScriptedBackend("Икра из кабачков.", "Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, QUESTION)
        await t.deliver_update(update)
        [conversation] = t.conversations()
        t.chat.clock.advance(hours=25)

        await t.deliver_update(update)

        assert backend.calls == 1
        assert t.bot.texts(MOTHER)[-1] == tc.STALE_UPDATE
        assert [turn.status for turn in t.all_turns()] == ["completed"]
        assert [c.id for c in t.conversations()] == [conversation.id]  # nothing new
        assert len(t.stored_texts(conversation.id)) == 2  # nothing was written again


async def test_a_pre_migration_update_without_provenance_is_refused_by_the_cutover(tmp_path):
    """The ledger's cutover is the first start of this code. A message dated before it, with
    no turn request and no marker (its conversation was deleted before the upgrade), cannot
    be told from a new one -- so it is refused, not guessed at."""
    backend = ScriptedBackend("Ответ на свежее сообщение")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        old = dated(
            private_message(t.next_id(), MOTHER, QUESTION),
            t.chat.clock() - timedelta(seconds=60),
        )

        await t.deliver_update(old)
        refused = t.bot.texts(MOTHER)[before:]
        await t.deliver(MOTHER, "Свежее сообщение")

    assert refused == [tc.PRE_MIGRATION_UPDATE]
    assert backend.calls == 1  # only the fresh message
    assert t.bot.texts(MOTHER)[-1] == "Ответ на свежее сообщение"


async def test_a_pre_migration_update_whose_turn_request_survives_is_replayed(tmp_path):
    """The turn request (and the backfill) proves what became of a pre-cutover update, so it
    is not refused: its stored answer is replayed and the provider is never asked."""
    backend = ScriptedBackend("Этот ответ запрашивать не следовало")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        # A completed turn as the previous code left it, dated before the cutover, with no
        # marker (the startup backfill's window is 48 hours, so this one is inside it).
        now = t.chat.clock() - timedelta(minutes=1)
        with t.chat.base.factory() as db, db.begin():
            created = cv.create_conversation(db, t.user_id, now=now)
            row = db.get(Conversation, created.id)
            question = new_message(
                db, row, role="user", content="Вопрос до обновления", created_at=now
            )
            answer = new_message(
                db, row, role="assistant", content="Ответ до обновления", created_at=now
            )
            new_turn_request(
                db,
                row,
                store.update_key(9601),
                status="completed",
                request_fingerprint=turns.request_fingerprint("Вопрос до обновления"),
                user_message_id=question.id,
                assistant_message_id=answer.id,
                result_payload={"reply": "Ответ до обновления", "truncated": False},
                created_at=now,
                expires_at=now + RETENTION,
                finished_at=now,
            )
        update = dated(
            private_message(9601, MOTHER, "Вопрос до обновления"),
            now,
        )
        await t.deliver_update(update)

    assert backend.calls == 0  # replayed from the stored answer
    assert t.bot.texts(MOTHER)[-1] == "Ответ до обновления"
