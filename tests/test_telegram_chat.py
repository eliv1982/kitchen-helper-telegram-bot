"""The bot as a front door: who it answers, what it refuses to do for everybody else, the
commands, and what a message to the assistant costs and leaves behind.

The conversations shared with the website (the point of the feature) are
tests/test_telegram_sync.py; the pairing routes of the website are tests/test_web_telegram_link.py.
"""

import asyncio
import logging
from datetime import timedelta

import pytest

from house_helper.core.backend import BackendRateLimitedError
from house_helper.persistence.models import AllowlistEntry, Conversation, Message, User
from house_helper.web import telegram_chat as tc
from tests.chat_support import ScriptedBackend, log_text, post_headers
from tests.telegram_support import (
    BOT_TOKEN,
    MOTHER,
    SISTER,
    STRANGER,
    TelegramHarness,
    private_message,
    telegram_app,
)

pytestmark = pytest.mark.asyncio


def nothing_was_stored(t: TelegramHarness) -> bool:
    return not (t.all_messages() or t.all_turns() or t.chat.rows(Conversation))


# --- strangers -----------------------------------------------------------------------------


async def test_a_stranger_gets_one_notice_and_costs_nothing(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.deliver(STRANGER, "Привет! Что приготовить?")
        await t.deliver(STRANGER, "Эй?")
        await t.deliver(STRANGER, "/start")
        await t.deliver(STRANGER, "/new")

    assert t.bot.texts(STRANGER) == [tc.NOT_LINKED]  # once, however many times they write
    assert t.chat.backend.calls == 0
    assert nothing_was_stored(t)
    assert t.links() == [] and t.codes() == []


async def test_a_stranger_is_told_again_after_ten_minutes(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.deliver(STRANGER, "раз", tick=False)
        t.chat.clock.advance(minutes=9, seconds=59)
        await t.deliver(STRANGER, "два", tick=False)
        t.chat.clock.advance(seconds=2)
        await t.deliver(STRANGER, "три", tick=False)

    assert t.bot.texts(STRANGER) == [tc.NOT_LINKED, tc.NOT_LINKED]


async def test_the_table_of_strangers_who_were_told_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "TRACKED_SENDERS", 20)
    async with telegram_app(tmp_path) as t:
        for number in range(70):
            await t.deliver(10_000_000 + number, "привет")
        told = len(t.service._told)

    assert told == 20
    assert t.bot.texts(10_000_069) == [tc.NOT_LINKED]  # the newest are kept
    assert t.chat.backend.calls == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m["chat"].update(type="group", id=-100500),
        lambda m: m["chat"].update(type="supergroup", id=-1001234),
        lambda m: m["chat"].update(type="channel"),
        lambda m: m["from"].update(is_bot=True),
        lambda m: m["chat"].update(id=MOTHER + 1),  # a "private" chat that is not the sender's
        lambda m: m.pop("from"),
        lambda m: m.pop("chat"),
        lambda m: m["from"].update(id=0),
        lambda m: m["from"].update(id=-MOTHER),
        lambda m: m["from"].update(id=str(MOTHER)),
        lambda m: m["from"].update(id=True),
    ],
)
async def test_only_a_person_in_a_private_chat_is_answered_at_all(tmp_path, mutation):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        sent_before = len(t.bot.sent)
        update = private_message(t.next_id(), MOTHER, "Что приготовить?")
        mutation(update["message"])
        await t.deliver_update(update)

    assert len(t.bot.sent) == sent_before  # not even a notice
    assert t.chat.backend.calls == 0


@pytest.mark.parametrize(
    "update",
    [
        None,
        "text",
        [],
        {},
        {"update_id": 1},
        {"update_id": "1", "message": {}},
        {"update_id": 1, "edited_message": {}},
        {"update_id": 1, "message": "hello"},
        {"update_id": -1, "message": {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}}},
    ],
)
async def test_a_malformed_or_other_kind_of_update_is_ignored(tmp_path, update):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)
        await t.deliver_update(update)

    assert len(t.bot.sent) == before and t.chat.backend.calls == 0


# --- pairing ------------------------------------------------------------------------------------


async def test_pairing_links_the_account_to_the_person_who_asked_for_the_code(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)

    [link] = t.links()
    assert (link.user_id, link.telegram_user_id, link.display_name) == (t.user_id, MOTHER, "@mama_elena")
    assert t.bot.texts(MOTHER) == [tc.LINKED]
    assert "OpenAI" in tc.LINKED  # the person is told where the text goes


@pytest.mark.parametrize("name", ["HELP", "GREETING", "LINKED"])
async def test_what_the_bot_says_about_the_copy_the_processing_and_the_retention_is_in_russian_and_complete(name):
    text = getattr(tc, name)

    assert "OpenAI" in text and "фотографии" in text  # messages and photos go to OpenAI
    assert "365 дней" in text and "удаляется" in text  # the history expires
    assert "в избранное" in text and "пока вы сами их не уберете" in text  # favorites stay until removed


@pytest.mark.parametrize(
    ("name", "mirror"),
    [("HELP", "пересылаю сюда"), ("GREETING", "пересылаю сюда"), ("LINKED", "будут приходить и сюда")],
)
async def test_the_bot_says_that_the_website_is_copied_to_telegram_and_how_to_stop_it(name, mirror):
    text = " ".join(getattr(tc, name).split())

    assert "на сайте" in text and mirror in text
    if name != "LINKED":
        assert "/unlink" in text  # the one way to stop the copies is to disconnect


async def test_the_account_number_is_not_the_sign_in_subject_and_is_never_compared_with_it(tmp_path):
    async with telegram_app(tmp_path) as t:
        base = t.chat.base
        await t.pair(MOTHER)
        with base.factory() as db:
            subjects = [e.subject for e in db.query(AllowlistEntry).all()]

    assert str(MOTHER) not in subjects and base.subject in subjects
    assert t.links()[0].telegram_user_id == MOTHER  # linked by the code, not by a lookup of the subject


async def test_an_account_whose_number_equals_the_subject_gets_no_access_without_a_code(tmp_path):
    # If the subject of the sign-in WERE the Bot API's id, this sender would be the owner of
    # the account. They are different things, and nothing connects them but a pairing code.
    async with telegram_app(tmp_path) as t:
        subject_as_number = int(t.chat.base.subject)
        await t.deliver(subject_as_number, "Покажи историю")

    assert t.bot.texts(subject_as_number) == [tc.NOT_LINKED]
    assert t.chat.backend.calls == 0


@pytest.mark.parametrize(
    "payload",
    ["", "x", "A" * 31, "A" * 33, "A" * 32, "<script>", "а" * 32, "' OR 1=1 --"],
)
async def test_a_wrong_code_is_refused_and_changes_nothing(tmp_path, payload):
    async with telegram_app(tmp_path) as t:
        await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())
        await t.deliver(STRANGER, f"/start {payload}".strip())

    if payload:
        assert t.bot.texts(STRANGER) == [tc.BAD_CODE]
    else:
        assert t.bot.texts(STRANGER) == [tc.NOT_LINKED]
    assert t.links() == []
    assert all(row.consumed_at is None for row in t.codes())


async def test_guessing_codes_is_stopped_after_five_attempts_for_ten_minutes(tmp_path):
    async with telegram_app(tmp_path) as t:
        code = (await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())).json()["code"]
        for number in range(tc.MAX_BAD_CODES):
            await t.deliver(STRANGER, f"/start {'B' * 31}{number}", tick=False)
        await t.deliver(STRANGER, f"/start {code}", tick=False)  # the RIGHT code, but too many wrong ones came first
        refused = t.links()
        t.chat.clock.advance(minutes=10, seconds=1)  # (the first code is spent by time as well)
        fresh = (await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())).json()["code"]
        await t.deliver(STRANGER, f"/start {fresh}", tick=False)

    assert t.bot.texts(STRANGER)[:5] == [tc.BAD_CODE] * 5
    assert t.bot.texts(STRANGER)[5] == tc.TOO_MANY_CODES
    assert refused == []
    assert t.bot.texts(STRANGER)[6] == tc.LINKED and len(t.links()) == 1


async def test_a_code_cannot_be_used_twice_or_from_two_accounts(tmp_path):
    async with telegram_app(tmp_path) as t:
        code = await t.pair(MOTHER)
        await t.deliver(SISTER, f"/start {code}")

    assert t.bot.texts(SISTER) == [tc.BAD_CODE]
    assert [link.telegram_user_id for link in t.links()] == [MOTHER]


async def test_an_account_that_belongs_to_another_person_cannot_be_taken(tmp_path):
    async with telegram_app(tmp_path) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await t.pair(MOTHER, client=other)  # the second person tries to link the same account

    assert t.bot.texts(MOTHER)[-1] == tc.TELEGRAM_TAKEN
    assert [(l.user_id, l.telegram_user_id) for l in t.links()] == [(t.user_id, MOTHER)]


async def test_a_person_with_an_account_linked_cannot_pair_a_second_one_through_a_stale_code(tmp_path):
    async with telegram_app(tmp_path) as t:
        code = (await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())).json()["code"]
        await t.pair(MOTHER)  # a new code was asked for inside pair(), the first one is gone
        await t.deliver(SISTER, f"/start {code}")

    assert t.bot.texts(SISTER) == [tc.BAD_CODE]
    assert [link.telegram_user_id for link in t.links()] == [MOTHER]


async def test_a_code_that_has_expired_is_refused(tmp_path):
    async with telegram_app(tmp_path) as t:
        code = (await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())).json()["code"]
        t.chat.clock.advance(minutes=10)
        await t.deliver(MOTHER, f"/start {code}", tick=False)

    assert t.bot.texts(MOTHER) == [tc.BAD_CODE] and t.links() == []


async def test_access_withdrawn_after_the_code_was_asked_for_refuses_the_link(tmp_path):
    async with telegram_app(tmp_path) as t:
        code = (await t.chat.client.post("/api/telegram/link", json={}, headers=post_headers())).json()["code"]
        with t.chat.base.factory() as db, db.begin():
            db.get(User, t.user_id).disabled_at = t.chat.clock.current
        await t.deliver(MOTHER, f"/start {code}")

    assert t.bot.texts(MOTHER) == [tc.ACCOUNT_UNAVAILABLE] and t.links() == []


# --- the commands ----------------------------------------------------------------------------------


async def test_start_and_help_greet_a_linked_person_without_calling_the_assistant(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "/start")
        await t.deliver(MOTHER, "/help")
        await t.deliver(MOTHER, "/help@" + "house_helper_app_bot")

    assert t.bot.texts(MOTHER)[1:] == [tc.GREETING, tc.HELP, tc.HELP]
    assert t.chat.backend.calls == 0


async def test_a_command_for_another_bot_is_not_ours_and_not_the_assistants_either(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "/new@some_other_bot")
        await t.deliver(MOTHER, "/help@some_other_bot")
        await t.deliver(MOTHER, "/@")

    assert t.bot.texts(MOTHER)[1:] == [tc.UNKNOWN_COMMAND] * 3
    assert t.chat.backend.calls == 0 and nothing_was_stored(t)


async def test_an_unknown_command_gets_the_hint_and_is_never_sent_to_the_assistant(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "/delete_everything")

    assert t.bot.texts(MOTHER)[-1] == tc.UNKNOWN_COMMAND
    assert t.chat.backend.calls == 0 and nothing_was_stored(t)


async def test_new_starts_a_conversation_and_asking_twice_does_not_make_two(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Первый ответ", "Второй ответ")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Что из яиц?")
        await t.deliver(MOTHER, "/new")
        await t.deliver(MOTHER, "/new")
        await t.deliver(MOTHER, "А что из картошки?")

    first, second = t.conversations()
    assert [role for role, _ in t.stored_texts(first.id)] == ["user", "assistant"]
    assert [text for _, text in t.stored_texts(second.id)] == ["А что из картошки?", "Второй ответ"]
    assert t.chat.backend.requests[1][0] == [{"role": "user", "content": "А что из картошки?"}]  # no memory of the first
    assert len(t.conversations()) == 2


async def test_unlink_from_telegram_ends_the_link(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "/unlink")
        await t.deliver(MOTHER, "Привет")
        status = (await t.chat.client.get("/api/telegram")).json()

    assert t.bot.texts(MOTHER)[1:] == [tc.UNLINKED, tc.NOT_LINKED]
    assert t.links() == [] and t.chat.backend.calls == 0
    assert status["linked"] is False


# --- a message to the assistant ----------------------------------------------------------------------


async def test_a_message_is_answered_in_telegram_and_stored(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Сделайте омлет.")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "  У меня есть яйца и молоко  ")

    assert t.bot.texts(MOTHER)[-1] == "Сделайте омлет."
    [conversation] = t.conversations()
    assert t.stored_texts(conversation.id) == [
        ("user", "У меня есть яйца и молоко"),
        ("assistant", "Сделайте омлет."),
    ]
    assert t.chat.backend.requests[0][0] == [{"role": "user", "content": "У меня есть яйца и молоко"}]
    [turn] = t.all_turns()
    assert turn.idempotency_key.startswith("tg-") and turn.status == "completed"


async def test_the_person_sees_that_the_assistant_is_typing_while_it_works(tmp_path):
    backend = ScriptedBackend("Готово")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        arrived, release = backend.hold()
        work = asyncio.ensure_future(t.deliver(MOTHER, "Что приготовить?"))
        await asyncio.wait_for(arrived.wait(), 10)
        while not t.bot.actions:  # the first beat is a task of its own: let it run
            await asyncio.sleep(0)
        typing_while_waiting = list(t.bot.actions)
        release.set()
        await work

    assert typing_while_waiting == [{"chat_id": MOTHER, "action": "typing"}]


async def test_a_long_answer_is_sent_in_pieces_that_fit_a_telegram_message(tmp_path):
    long_answer = "\n\n".join(f"Шаг {n}. " + "очень длинный текст " * 25 for n in range(1, 30))
    assert len(long_answer) > 4096
    async with telegram_app(tmp_path, ScriptedBackend(long_answer)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Расскажи подробно")

    pieces = t.bot.texts(MOTHER)[1:]
    assert len(pieces) >= 2 and all(0 < len(piece) <= 4096 for piece in pieces)
    assert "".join(piece.replace("\n", "").replace(" ", "") for piece in pieces) == long_answer.replace("\n", "").replace(" ", "")


async def test_the_answer_is_sent_as_plain_text_without_a_parse_mode(tmp_path):
    answer = "1. Смешайте <b>яйца</b> & молоко_и_соль *не жирным*"
    async with telegram_app(tmp_path, ScriptedBackend(answer)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Рецепт")

    assert t.bot.sent[-1]["text"] == answer
    assert "parse_mode" not in t.bot.sent[-1]
    assert t.bot.sent[-1]["link_preview_options"] == {"is_disabled": True}  # no card for a link in a recipe
    assert all("disable_web_page_preview" not in call for call in t.bot.sent)  # (the deprecated field)


async def test_a_message_that_is_too_long_or_unstorable_is_refused_before_the_assistant(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "я" * 2001)
        await t.deliver(MOTHER, "привет\x00мир")
        await t.deliver(MOTHER, "   ")

    texts = t.bot.texts(MOTHER)[1:]
    assert "слишком длинное" in texts[0] and "2000" in texts[0]
    assert "недопустимые символы" in texts[1]
    assert len(texts) == 3 and "пустым" in texts[2]
    assert t.chat.backend.calls == 0 and nothing_was_stored(t)


async def test_something_that_is_not_text_gets_a_polite_note(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver_update(private_message(t.next_id(), MOTHER, sticker={"file_id": "x"}))
        await t.deliver_update(private_message(t.next_id(), MOTHER, voice={"file_id": "x", "duration": 3}))

    assert t.bot.texts(MOTHER)[1:] == [tc.UNSUPPORTED, tc.UNSUPPORTED]
    assert t.chat.backend.calls == 0


async def test_when_the_assistant_fails_the_person_is_told_in_russian_with_a_code_for_support(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend(BackendRateLimitedError("x"), "Теперь можно")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Что приготовить?")
        await t.deliver(MOTHER, "Попробую ещё раз")

    failure = t.bot.texts(MOTHER)[1]
    assert "слишком много запросов" in failure and "Код для поддержки: hh-" in failure
    assert t.bot.texts(MOTHER)[2] == "Теперь можно"
    [first, second] = t.all_turns()
    assert (first.status, first.error_code) == ("failed", "provider_rate_limited")
    # the failed message is history but not context: the retry does not show it to the assistant
    assert t.chat.backend.requests[1][0] == [{"role": "user", "content": "Попробую ещё раз"}]


async def test_without_the_assistant_configured_the_person_is_told_so(tmp_path):
    async with telegram_app(tmp_path) as t:
        t.chat.app.state.chat = None  # as if the key were not set
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Привет")

    assert "недоступен" in t.bot.texts(MOTHER)[-1] and nothing_was_stored(t)


async def test_a_busy_conversation_asks_the_person_to_wait_and_stores_nothing_more(tmp_path):
    backend = ScriptedBackend("Первый", "Второй")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        arrived, release = backend.hold()
        web_turn = asyncio.ensure_future(t.chat.send(conversation_id, "С сайта", "web-1"))
        await asyncio.wait_for(arrived.wait(), 10)

        await t.deliver(MOTHER, "А это из Telegram")  # same conversation (the latest), busy
        release.set()
        await web_turn
        await t.settle()

    assert tc.STILL_BUSY in t.bot.texts(MOTHER)
    assert [m.content for m in t.chat.messages(conversation_id) if m.role == "user"] == ["С сайта"]


async def test_a_revoked_person_is_a_stranger_at_once(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Привет")
        with t.chat.base.factory() as db, db.begin():
            for entry in db.query(AllowlistEntry).filter(AllowlistEntry.user_id == t.user_id):
                entry.revoked_at = t.chat.clock.current
        await t.deliver(MOTHER, "Ещё вопрос")

    assert t.bot.texts(MOTHER)[-1] == tc.NOT_LINKED
    assert t.chat.backend.calls == 1


# --- two people ---------------------------------------------------------------------------------------


async def test_two_people_talk_to_the_bot_without_seeing_each_other(tmp_path):
    echo = lambda messages: "Ответ для: " + messages[-1]["content"]  # noqa: E731
    backend = ScriptedBackend(echo, echo, echo, echo)
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await t.pair(SISTER, client=other)
        await t.deliver(MOTHER, "Секрет мамы: аллергия на орехи")
        await t.deliver(SISTER, "Что на ужин?")
        await t.deliver(MOTHER, "А без молока?")
        await t.deliver(SISTER, "И на завтрак?")

    other_user = t.chat.extra_users["7777777777777777777"]
    mine, theirs = t.conversations(), t.conversations(other_user)
    assert len(mine) == 1 and len(theirs) == 1
    assert "Секрет мамы" in repr(t.stored_texts(mine[0].id)) and "Секрет мамы" not in repr(t.stored_texts(theirs[0].id))
    sister_asked = [m for m, _, _ in backend.requests if m[-1]["content"] in ("Что на ужин?", "И на завтрак?")]
    mother_asked = [m for m, _, _ in backend.requests if m not in sister_asked]
    assert len(sister_asked) == len(mother_asked) == 2
    for messages in sister_asked:
        assert "аллергия" not in repr(messages) and "молока" not in repr(messages)
    for messages in mother_asked:
        assert "ужин" not in repr(messages) and "завтрак" not in repr(messages)
    assert t.bot.texts(SISTER)[-2:] == ["Ответ для: Что на ужин?", "Ответ для: И на завтрак?"]
    assert all("Секрет" not in text for text in t.bot.texts(SISTER))


# --- what the logs hold ----------------------------------------------------------------------------------


async def test_no_log_holds_the_token_an_account_number_a_name_or_a_message(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with telegram_app(tmp_path, ScriptedBackend("Ответ: секретный рецепт")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Приватный вопрос про диету")
        await t.deliver(STRANGER, "Кто тут?")
        t.bot.fail("sendMessage", 403)
        await t.deliver(MOTHER, "Ещё вопрос")
        t.chat.backend.script.append(RuntimeError("boom with Приватный"))
        await t.deliver(MOTHER, "Ломающий вопрос")

    text = log_text(caplog)
    for secret in (BOT_TOKEN, BOT_TOKEN.split(":")[1], str(MOTHER), str(STRANGER), "mama_elena", "Приватный", "секретный", "Елена"):
        assert secret not in text, secret
    assert "category=api:403" in text  # what happened is there, not who it happened to
