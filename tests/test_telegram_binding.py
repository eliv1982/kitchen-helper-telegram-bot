"""Who an account is, at the moment it is served: a message or a copy that waited in line for its
turn is served only if the Telegram account is STILL the enrolled sender of the user it belonged
to, and is dropped, never redirected, when it is not.

The interleavings are made exact, not hoped for. The line of a chat is a lock (telegram_chat.py:
`_chat_lock`); a test takes that lock itself to stand for "an earlier message is being answered",
puts a message behind it, waits (on the lock's count of users, not on the clock) until the message
is really waiting, changes the world, and lets go. The second lock, the one that keeps the tie
from being cut between the check and the send (`_binding`), is shown by holding the Bot API at
the send. Nothing here sleeps to arrange an order.

The redelivery rules are tests/test_telegram_redelivery.py; the bot's other behaviour is
tests/test_telegram_chat.py.
"""

import asyncio
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from house_helper.persistence import telegram as store
from house_helper.persistence.models import AllowlistEntry, MessageAttachment
from house_helper.web import telegram_chat as tc
from house_helper.web.turn_runner import TurnNotAuthorized
from tests.chat_support import ScriptedBackend, post_headers
from tests.photo_support import jpeg
from tests.telegram_support import (
    BOT_TOKEN,
    FakeBot,
    MOTHER,
    SISTER,
    TelegramHarness,
    in_line,
    photo_message,
    sending,
    telegram_app,
    until,
)
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

SECRET = "Секрет первой: аллергия на орехи"


def revoke(t: TelegramHarness) -> None:
    """The operator withdraws the first person's access (the allowlist entries are revoked)."""
    with t.chat.base.factory() as db, db.begin():
        for entry in db.query(AllowlistEntry).filter(AllowlistEntry.user_id == t.user_id):
            entry.revoked_at = t.chat.clock.current


async def website_unlink(t: TelegramHarness):
    response = await asyncio.wait_for(t.chat.client.delete("/api/telegram/link", headers=post_headers()), TIMEOUT)
    assert response.status_code == 200, response.text
    return response


def assert_idle(t: TelegramHarness) -> None:
    """Both tables of locks are empty: nothing holds or waits, and no entry was leaked."""
    assert t.service._chat_locks == {} and t.service._binding_locks == {}


def second_person(t: TelegramHarness):
    return next(iter(t.chat.extra_users.values()))


# --- the send predicate, on its own -------------------------------------------------------------


class _WriteOnlyJournal(list):
    """The general request journal, made read-hostile: appends stay allowed (the transport
    still records every call), but any read fails the test. It is what proves that the send
    predicate never scans the journal that every poll grows."""

    @staticmethod
    def _refuse():
        raise AssertionError("sending() must not read FakeBot.requests")

    def __iter__(self):
        self._refuse()

    def __len__(self):
        self._refuse()

    def __getitem__(self, index):
        self._refuse()


async def test_the_send_predicate_sees_arrivals_without_reading_the_poll_journal():
    """The predicate is the arrival side of a held send, on its own: it sees a send that is
    still held at the gate (not completed), keeps the exact text match, ignores polling
    calls, records each arrival once, and never touches the growing journal."""
    bot = FakeBot()
    bot.requests = _WriteOnlyJournal(bot.requests)
    stand_in = SimpleNamespace(bot=bot)
    gate = bot.hold_sends()
    transport = bot.transport()

    def call(method: str, **payload):
        return httpx.Request("POST", f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload)

    held = asyncio.ensure_future(
        transport.handle_async_request(call("sendMessage", chat_id=MOTHER, text="Ответ первой"))
    )
    await until(sending(stand_in, "Ответ первой"))

    assert not held.done() and bot.sent == []  # arrived, not completed: the gate still holds it
    assert not sending(stand_in, "Ответ первой!")()  # the match is the whole text, exactly
    assert not sending(stand_in, "Что приготовить?")()

    # A poll reaches the same transport and is journalled like every call, but it is not a send.
    await transport.handle_async_request(call("getUpdates"))
    assert sending(stand_in, "Ответ первой")()  # still the one held send
    assert list.__len__(bot.requests) == 2  # ... and both calls really are in the journal

    polling_only = FakeBot()
    polling_only.requests = _WriteOnlyJournal(polling_only.requests)
    await polling_only.transport().handle_async_request(call("getUpdates"))
    assert not sending(SimpleNamespace(bot=polling_only), "Ответ первой")()

    other = FakeBot()  # per instance: nothing is shared between scenarios
    assert not sending(SimpleNamespace(bot=other), "Ответ первой")()

    gate.set()
    assert (await held).status_code == 200
    assert bot.sent == [{"chat_id": MOTHER, "text": "Ответ первой"}]
    assert bot.send_attempts == [{"chat_id": MOTHER, "text": "Ответ первой"}]  # recorded once

    await transport.handle_async_request(call("sendMessage", chat_id=MOTHER, text="Ответ второй"))
    assert [attempt["text"] for attempt in bot.send_attempts] == ["Ответ первой", "Ответ второй"]


async def test_a_send_to_another_bots_token_does_not_satisfy_the_predicate():
    """The approved boundary: an arrival must pass FakeBot's token check. The call is still
    in the general journal (the fake records every call) but it is not this bot's send."""
    bot = FakeBot()
    stand_in = SimpleNamespace(bot=bot)
    wrong_token = "999999999:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    request = httpx.Request(
        "POST",
        f"https://api.telegram.org/bot{wrong_token}/sendMessage",
        json={"chat_id": MOTHER, "text": "Ответ первой"},
    )
    response = await bot.transport().handle_async_request(request)

    assert response.status_code == 401 and response.json()["ok"] is False
    assert bot.requests == [request]  # the journal still records the call, as before
    assert bot.send_attempts == [] and bot.sent == []
    assert not sending(stand_in, "Ответ первой")()


async def test_a_send_refused_by_an_injected_failure_is_an_arrival_but_not_a_completion():
    """A correctly addressed send that the fake then refuses is still an arrival: the token
    check and decoding passed, and the failure comes after the recorded arrival. It is
    recorded once, and it is never a completed send."""
    bot = FakeBot()
    stand_in = SimpleNamespace(bot=bot)
    bot.fail("sendMessage", 403)  # "bot was blocked by the user"
    request = httpx.Request(
        "POST",
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": MOTHER, "text": "Ответ первой"},
    )
    response = await bot.transport().handle_async_request(request)

    assert response.status_code == 403 and response.json()["ok"] is False
    assert bot.requests == [request]  # the journal keeps its record too
    assert bot.send_attempts == [{"chat_id": MOTHER, "text": "Ответ первой"}]  # exactly once
    assert bot.sent == []  # refused: never a completed send
    assert sending(stand_in, "Ответ первой")()


# --- the person's access is withdrawn while the message waits ---------------------------------------


async def test_access_withdrawn_while_a_message_waits_for_its_turn_stops_it_before_the_assistant(tmp_path):
    backend = ScriptedBackend("Этот ответ не должен быть запрошен")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        async with t.service._chat_lock(MOTHER):  # an earlier message is being answered
            queued = asyncio.ensure_future(t.deliver(MOTHER, SECRET))
            await until(in_line(t, MOTHER, 2))
            revoke(t)
        await queued
        await t.settle()
        assert_idle(t)

    assert backend.calls == 0
    assert t.all_turns() == [] and t.all_messages() == []  # not even the message was kept
    assert t.bot.texts(MOTHER)[before:] == [tc.NOT_LINKED]  # fixed words, nothing of the message


async def test_access_withdrawn_while_the_assistant_works_keeps_the_answer_from_the_chat(tmp_path):
    backend = ScriptedBackend("Ответ, который уже нельзя отправлять")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        arrived, release = backend.hold()
        turn = asyncio.ensure_future(t.deliver(MOTHER, "Что приготовить?"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        revoke(t)
        release.set()
        await turn

    assert backend.calls == 1  # it had begun; it cannot be taken back
    assert t.bot.texts(MOTHER)[before:] == [tc.NOT_LINKED]
    [conversation] = t.conversations()  # the answer is the person's own history, not the chat's
    assert ("assistant", "Ответ, который уже нельзя отправлять") in t.stored_texts(conversation.id)


async def test_a_queued_photo_is_not_downloaded_for_a_person_whose_access_was_withdrawn(tmp_path):
    backend = ScriptedBackend("Не должно быть вызвано")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        t.bot.files["photo-1"] = ("photos/photo-1.jpg", jpeg(4000, "q"))
        async with t.service._chat_lock(MOTHER):
            queued = asyncio.ensure_future(t.deliver_update(photo_message(t.next_id(), MOTHER, "photo-1", caption="Что это?")))
            await until(in_line(t, MOTHER, 2))
            revoke(t)
        await queued

    assert backend.calls == 0
    assert not [r for r in t.bot.requests if r.url.path.endswith("/getFile") or r.url.path.startswith("/file/")]
    assert t.chat.rows(MessageAttachment) == []
    assert t.all_turns() == []


async def test_access_withdrawn_between_the_check_and_the_claim_still_stops_the_assistant(tmp_path):
    """The check after the lock and the claim are separated by a few awaits. The claim asks again
    in its own transaction, so a tie that went away in that gap costs no request to the assistant
    and leaves nothing behind. (The gap is made exact: the bot is held between the two.)"""
    backend = ScriptedBackend("Не должно быть вызвано")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        checked, proceed = asyncio.Event(), asyncio.Event()
        original = t.service._conversation_for

        async def held(account, user_id, key, update_id, *, admitted_here=False):
            found = await original(
                account, user_id, key, update_id, admitted_here=admitted_here
            )
            checked.set()
            await proceed.wait()  # the check above has passed; the claim has not been made
            return found

        t.service._conversation_for = held
        turn = asyncio.ensure_future(t.deliver(MOTHER, SECRET))
        await asyncio.wait_for(checked.wait(), TIMEOUT)
        revoke(t)
        proceed.set()
        await turn
        assert_idle(t)

    assert backend.calls == 0
    assert t.all_turns() == [] and t.all_messages() == []  # the claim was rolled back whole
    assert t.bot.texts(MOTHER)[before:] == [tc.NOT_LINKED]


async def test_the_runner_makes_the_claim_conditional_when_asked_to_and_stores_nothing_otherwise(tmp_path):
    backend = ScriptedBackend("Ответ", "Второй ответ")
    async with telegram_app(tmp_path, backend) as t:
        runner = t.chat.app.state.chat
        conversation_id = await t.chat.create()

        async def submit(key, authorize):
            return await runner.submit(
                user_id=t.user_id,
                conversation_id=UUID(conversation_id),
                idempotency_key=key,
                text="Привет",
                correlation_id="hh-test",
                route="test",
                authorize=authorize,
            )

        with pytest.raises(TurnNotAuthorized):
            await submit("k-1", lambda session, started: False)
        refused = (backend.calls, len(t.all_turns()), len(t.all_messages()))
        # the same key is still free: nothing was kept
        done = await submit("k-1", lambda session, started: True)

    assert refused == (0, 0, 0)
    assert done.completed.assistant_message.text == "Ответ" and backend.calls == 1


# --- the account is unlinked while the message waits --------------------------------------------------


async def test_unlinking_while_a_message_waits_stops_it_before_the_assistant(tmp_path):
    backend = ScriptedBackend("Не должно быть вызвано")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        async with t.service._chat_lock(MOTHER):
            queued = asyncio.ensure_future(t.deliver(MOTHER, SECRET))
            await until(in_line(t, MOTHER, 2))
            await website_unlink(t)
        await queued

    assert backend.calls == 0 and t.all_turns() == []
    assert t.bot.texts(MOTHER)[before:] == [tc.NOT_LINKED]


async def test_unlinking_does_not_wait_for_an_answer_that_is_being_prepared_and_the_answer_is_withheld(tmp_path):
    backend = ScriptedBackend("Ответ, который не должен уйти")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        arrived, release = backend.hold()
        turn = asyncio.ensure_future(t.deliver(MOTHER, "Что приготовить?"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        await website_unlink(t)  # returns while the assistant is still working: it holds no lock for that
        assert not turn.done()
        release.set()
        await turn

    assert t.bot.texts(MOTHER)[before:] == [tc.NOT_LINKED]
    assert t.links() == []


async def test_unlinking_waits_for_a_send_that_was_already_checked(tmp_path):
    """The tie cannot be cut between the check and the send: an unlink that arrives while the
    send is under way waits for it (and so the account cannot have been given to somebody else
    by the time the checked text leaves)."""
    async with telegram_app(tmp_path, ScriptedBackend("Ответ первой")) as t:
        await t.pair(MOTHER)
        gate = t.bot.hold_sends()
        turn = asyncio.ensure_future(t.deliver(MOTHER, "Что приготовить?"))
        await until(sending(t, "Ответ первой"))  # checked, and now at the Bot API

        unlinking = asyncio.ensure_future(website_unlink(t))
        await until(in_line(t, MOTHER, 2, "_binding_locks"))  # it is waiting for the lock
        assert not unlinking.done()
        assert len(t.links()) == 1  # nothing has been cut

        gate.set()
        await turn
        await unlinking
        assert_idle(t)

    assert t.bot.texts(MOTHER)[-1] == "Ответ первой"  # sent while the tie was whole
    assert t.links() == []


# --- the account is given to somebody else ------------------------------------------------------------------


async def test_a_message_that_waited_is_not_processed_as_the_person_the_account_belongs_to_next(tmp_path):
    backend = ScriptedBackend("Не должно быть вызвано")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        async with t.service._chat_lock(MOTHER):
            queued = asyncio.ensure_future(t.deliver(MOTHER, SECRET))
            await until(in_line(t, MOTHER, 2))
            await website_unlink(t)
            await t.pair(MOTHER, client=other)  # the SAME Telegram account is linked to the other person
        await queued

    assert backend.calls == 0
    assert t.all_turns() == [] and t.conversations(second_person(t)) == []
    assert t.bot.texts(MOTHER)[before:] == [tc.LINKED, tc.BINDING_CHANGED]  # told, in fixed words
    assert all(SECRET not in text for text in t.bot.texts(MOTHER))


async def test_an_answer_prepared_for_one_person_is_not_sent_to_the_next_owner_of_the_account(tmp_path):
    backend = ScriptedBackend("Ответ для первой: орехи нельзя")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        arrived, release = backend.hold()
        turn = asyncio.ensure_future(t.deliver(MOTHER, SECRET))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        await website_unlink(t)
        await t.pair(MOTHER, client=other)
        release.set()
        await turn

    assert backend.calls == 1
    assert t.bot.texts(MOTHER)[before:] == [tc.LINKED, tc.BINDING_CHANGED]
    assert all("орехи" not in text for text in t.bot.texts(MOTHER))
    [conversation] = t.conversations()  # the answer is stored where it belongs: the first person's history
    assert ("assistant", "Ответ для первой: орехи нельзя") in t.stored_texts(conversation.id)
    assert t.conversations(second_person(t)) == []


# --- the copy of a website turn waits ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "new_owner",
    [pytest.param("other_person", id="the-account-goes-to-the-other-person"), pytest.param("other_account", id="the-person-links-another-account")],
)
async def test_a_copy_of_a_website_turn_that_waited_is_dropped_when_the_tie_changed(tmp_path, new_owner):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ первой: орехи нельзя")) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        before = len(t.bot.sent)
        async with t.service._chat_lock(MOTHER):
            response = await t.chat.send(conversation_id, SECRET, "web-1")
            assert response.status_code == 201  # the website is not held up by the line
            await until(in_line(t, MOTHER, 2))  # the copy is waiting for its turn
            await website_unlink(t)
            if new_owner == "other_person":
                await t.pair(MOTHER, client=other)
            else:
                await t.pair(SISTER)
        await t.settle()
        assert_idle(t)

    assert [call["text"] for call in t.bot.sent[before:]] == [tc.LINKED]  # the pairing's own reply, and nothing else
    assert all("орехи" not in call["text"] and SECRET not in call["text"] for call in t.bot.sent[before:])


async def test_a_copy_is_still_sent_when_nothing_changed_while_it_waited(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ первой")) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        async with t.service._chat_lock(MOTHER):
            await t.chat.send(conversation_id, "Как варить суп?", "web-1")
            await until(in_line(t, MOTHER, 2))
        await t.settle()

    assert t.bot.texts(MOTHER)[-2:] == ["Вы (на сайте):\nКак варить суп?", "Ответ первой"]


async def test_the_other_person_still_gets_their_own_copies_while_the_first_is_dropped(tmp_path):
    echo = lambda messages: "Ответ для: " + messages[-1]["content"]  # noqa: E731
    async with telegram_app(tmp_path, ScriptedBackend(echo, echo)) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await t.pair(SISTER, client=other)
        first_conversation = await t.chat.create()
        second_conversation = await t.chat.create(other)
        async with t.service._chat_lock(MOTHER):
            await t.chat.send(first_conversation, SECRET, "web-1")
            await until(in_line(t, MOTHER, 2))
            revoke(t)
            await t.chat.send(second_conversation, "Что на ужин?", "web-2", client=other)
            await until(lambda: "Ответ для: Что на ужин?" in t.bot.texts(SISTER))
        await t.settle()

    assert t.bot.texts(SISTER)[-2:] == ["Вы (на сайте):\nЧто на ужин?", "Ответ для: Что на ужин?"]
    assert all(SECRET not in text for text in t.bot.texts(MOTHER)) and all(SECRET not in text for text in t.bot.texts(SISTER))


# --- the commands of an account whose tie has moved ---------------------------------------------------------------


async def test_the_unlink_command_of_a_stale_tie_does_not_cut_the_new_one(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        first = t.user_id
        await website_unlink(t)
        await t.pair(MOTHER, client=other)

        removed = await t.service.unlink(first, account=MOTHER)  # what /unlink does with the user it saw

        assert removed is False
        [link] = t.links()
        assert link.user_id == second_person(t) and link.telegram_user_id == MOTHER


async def test_the_unlink_command_ends_its_own_tie_and_says_so(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        await t.pair(MOTHER)

        await t.deliver(MOTHER, "/unlink")

    assert t.links() == [] and t.bot.texts(MOTHER)[-1] == tc.UNLINKED


async def test_the_new_command_does_nothing_for_a_tie_that_is_gone(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend()) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        user = t.user_id
        await website_unlink(t)
        await t.pair(MOTHER, client=other)
        now = t.chat.clock.current

        started = await t.service._database.run(lambda session: store.start_conversation_for(session, MOTHER, user, now=now))

    assert started is None
    assert t.conversations() == [] and t.conversations(second_person(t)) == []


async def test_a_message_from_an_account_with_no_waiting_still_reaches_the_assistant(tmp_path):
    """The checks cost the ordinary case nothing it needs: one message, answered once."""
    backend = ScriptedBackend("Омлет.")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Что из яиц?")
        assert_idle(t)

    assert backend.calls == 1 and t.bot.texts(MOTHER)[-1] == "Омлет."
