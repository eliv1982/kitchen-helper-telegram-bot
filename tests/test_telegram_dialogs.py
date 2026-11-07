"""Choosing the conversation in Telegram: /dialogs, its pages, and what a selection changes.

The bot is the real TelegramChat over the real database and application; the Bot API is a
FakeBot and the assistant a scripted backend. What is pinned here:

  * /dialogs lists the linked user's own conversations, newest activity first, with the
    product's Moscow time and an explicit МСК label, eight per page, and the conversation
    the chat would use now marked;
  * a button selects only one of that user's conversations (the check is in persistence);
    the confirmation is short, the history is not replayed, and the next message continues
    the selected conversation with its own context;
  * /new makes (or reuses, while it is empty) a conversation and selects it; twice on an
    empty one creates nothing extra;
  * a selected conversation that is deleted leaves the explicit NULL state: the list says
    so and the next message creates and selects a new conversation, never another one;
  * redelivered updates still go to their first conversation, whatever the cursor says;
  * the Web-to-Telegram copy follows the selection: every conversation before one is made,
    only the selected one afterwards, and nothing while the selected one is deleted;
  * switching is serialized with a message being answered and with a completed turn's
    delivery, callbacks are acknowledged first, and a conversation deleted between the
    resolution and the claim is recovered from without losing or duplicating the message;
  * photos follow the cursor exactly as text does.
"""

import asyncio
import re
import threading
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
import sqlalchemy as sa

from house_helper.persistence import conversations as cv
from house_helper.persistence import telegram as store
from house_helper.persistence.models import MessageAttachment
from house_helper.web import telegram_chat as tc
from tests.chat_support import ScriptedBackend, post_headers
from tests.photo_support import jpeg
from tests.telegram_support import (
    MOTHER,
    SISTER,
    STRANGER,
    in_line,
    photo_message,
    private_message,
    save_buttons,
    telegram_app,
    until,
)
from tests.web_support import TIMEOUT

pytestmark = pytest.mark.asyncio

MOSCOW_LABEL = re.compile(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2} МСК")


def dialog_buttons(call) -> list[dict]:
    """The conversation buttons of a dialogs message, without the navigation row."""
    return [button for button in save_buttons(call) if button["text"] not in ("◀ Назад", "Далее ▶")]


def button_with_text(call, text: str) -> dict:
    return next(button for button in save_buttons(call) if button["text"] == text)


def button_for(call, conversation_id) -> dict:
    wanted = UUID(str(conversation_id)).hex
    return next(
        button
        for button in save_buttons(call)
        if button["callback_data"].startswith(f"{tc.DIALOGS_SELECT_PREFIX}:{wanted}:")
    )


def selection(t):
    with t.chat.base.factory() as db:
        return store.active_conversation(db, t.user_id)


def stored_selection_rows(t):
    from house_helper.persistence.models import TelegramActiveConversation

    return t.chat.rows(TelegramActiveConversation)


async def delete_conversation(t, conversation_id, client=None) -> None:
    response = await (client or t.chat.client).delete(
        f"/api/conversations/{conversation_id}", headers=post_headers()
    )
    assert response.status_code == 204, response.text


async def web_turn(t, text: str, key: str) -> str:
    """A conversation started and answered on the website (it may be copied to Telegram)."""
    conversation = await t.chat.create()
    t.chat.clock.advance(seconds=1)
    response = await t.chat.send(conversation, text, key)
    assert response.status_code == 201, response.text
    await t.settle()
    return conversation


async def empty_conversation(t) -> str:
    conversation = await t.chat.create()
    t.chat.clock.advance(seconds=1)
    return conversation


async def open_dialogs(t, account=MOTHER) -> dict:
    await t.deliver(account, "/dialogs")
    return t.bot.sent[-1]


async def choose(t, conversation_id, page: int = 0, account=MOTHER) -> dict:
    await t.click(account, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(str(conversation_id)).hex}:{page}")
    return t.bot.sent[-1]


# --- the list ---------------------------------------------------------------------------------


async def test_dialogs_without_conversations_is_the_friendly_empty_state(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        before = t.chat.backend.calls
        call = await open_dialogs(t)

        assert call["text"] == tc.DIALOGS_EMPTY
        assert "reply_markup" not in call
        assert t.chat.backend.calls == before


async def test_dialogs_lists_every_conversation_newest_first_with_moscow_time(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Первый", "Второй", "Третий")) as t:
        await t.pair(MOTHER)
        first = await web_turn(t, "Первый вопрос", "w-1")
        second = await web_turn(t, "Второй вопрос", "w-2")
        third = await web_turn(t, "Третий вопрос", "w-3")
        calls_before = t.chat.backend.calls

        call = await open_dialogs(t)

        listed = dialog_buttons(call)
        assert [button["text"] for button in listed] == [
            "✓ " + tc._moscow_time(t.chat.conversation(third).updated_at) + " — Третий вопрос",
            tc._moscow_time(t.chat.conversation(second).updated_at) + " — Второй вопрос",
            tc._moscow_time(t.chat.conversation(first).updated_at) + " — Первый вопрос",
        ]
        assert [button["callback_data"] for button in listed] == [
            f"ds:{UUID(third).hex}:0",
            f"ds:{UUID(second).hex}:0",
            f"ds:{UUID(first).hex}:0",
        ]
        assert all(MOSCOW_LABEL.search(button["text"]) for button in listed)
        assert "🗂 Ваши диалоги." in call["text"] and "Страница" not in call["text"]
        assert t.chat.backend.calls == calls_before  # navigation never asks the assistant


async def test_dialogs_pages_forward_and_back_in_the_same_order(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        created = [await empty_conversation(t) for _ in range(tc.DIALOGS_PAGE_SIZE + 2)]
        newest_first = list(reversed(created))

        first_page = await open_dialogs(t)
        assert [button["callback_data"].split(":")[1] for button in dialog_buttons(first_page)] == [
            UUID(item).hex for item in newest_first[: tc.DIALOGS_PAGE_SIZE]
        ]
        assert "Страница 1 из 2" in first_page["text"]
        assert "◀ Назад" not in [button["text"] for button in save_buttons(first_page)]

        await t.click(MOTHER, button_with_text(first_page, "Далее ▶")["callback_data"])
        second_page = t.bot.sent[-1]
        assert [button["callback_data"].split(":")[1] for button in dialog_buttons(second_page)] == [
            UUID(item).hex for item in newest_first[tc.DIALOGS_PAGE_SIZE :]
        ]
        assert "Страница 2 из 2" in second_page["text"]
        assert "Далее ▶" not in [button["text"] for button in save_buttons(second_page)]

        await t.click(MOTHER, button_with_text(second_page, "◀ Назад")["callback_data"])
        back = t.bot.sent[-1]
        assert [button["callback_data"] for button in dialog_buttons(back)] == [
            button["callback_data"] for button in dialog_buttons(first_page)
        ]


async def test_a_page_that_no_longer_exists_falls_back_to_the_last_page(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        created = [await empty_conversation(t) for _ in range(tc.DIALOGS_PAGE_SIZE + 2)]
        await open_dialogs(t)
        await t.click(MOTHER, f"{tc.DIALOGS_LIST_PREFIX}:1")
        # The eight older conversations (the first page) are deleted; only two remain.
        for conversation in created[: tc.DIALOGS_PAGE_SIZE]:
            await delete_conversation(t, conversation)

        await t.click(MOTHER, f"{tc.DIALOGS_LIST_PREFIX}:1")  # a stale "page 2" button

        listed = dialog_buttons(t.bot.sent[-1])
        assert len(listed) == 2
        assert "Страница" not in t.bot.texts(MOTHER)[-1]
        assert t.bot.answered[-1]["text"] == ""


async def test_every_conversation_is_reachable_past_the_old_hundred_page_ceiling(tmp_path):
    """The list has no fixed page ceiling: with 801 conversations (101 pages of eight), the
    last conversation -- 800 rows behind the first -- is on page 101 and reachable by its
    page button. An absurd but well-formed page number is clamped to that same last page,
    not rejected by an arbitrary limit and not a reason to read more than one page."""
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        start = t.chat.clock()
        with t.chat.base.factory() as db, db.begin():
            created = [
                cv.create_conversation(
                    db, t.user_id, now=start + timedelta(seconds=index)
                ).id
                for index in range(801)
            ]

        first_page = await open_dialogs(t)
        assert "Страница 1 из 101" in first_page["text"]

        await t.click(MOTHER, f"{tc.DIALOGS_LIST_PREFIX}:100")
        last_page = t.bot.sent[-1]
        assert "Страница 101 из 101" in last_page["text"]
        [button] = dialog_buttons(last_page)  # 801 = 100 * 8 + 1
        assert button["callback_data"] == f"{tc.DIALOGS_SELECT_PREFIX}:{created[0].hex}:100"

        # A page far beyond the list settles on the last one (one read, eight rows).
        await t.click(MOTHER, f"{tc.DIALOGS_LIST_PREFIX}:999999999999")
        clamped = t.bot.sent[-1]
        assert "Страница 101 из 101" in clamped["text"]
        assert dialog_buttons(clamped)[0]["callback_data"].endswith(":100")
        assert t.bot.answered[-1]["text"] == ""  # acknowledged, not an error


async def test_the_moscow_label_is_explicit_and_the_zone_is_not_a_locale_guess():
    assert tc._moscow_time(datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)) == (
        "28.10.2026 15:00 МСК"
    )
    assert tc.MOSCOW_LABEL == "МСК"


# --- choosing ---------------------------------------------------------------------------------


async def test_choosing_an_older_conversation_restores_its_context_without_replaying_it(tmp_path):
    backend = ScriptedBackend("Борщ.", "Омлет.", "Сметану подавайте отдельно.")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        older = await web_turn(t, "Что на обед?", "w-1")
        newer = await web_turn(t, "Что на завтрак?", "w-2")
        updated_before = t.chat.conversation(older).updated_at
        call = await open_dialogs(t)
        sent_before = len(t.bot.sent)
        confirmation = await choose(t, older)

        assert confirmation["text"].startswith("🗂 Выбран диалог: «Что на обед?».")
        assert "Продолжайте писать" in confirmation["text"]
        assert save_buttons(confirmation) == [
            {"text": tc.DIALOGS_BACK, "callback_data": f"{tc.DIALOGS_LIST_PREFIX}:0"}
        ]
        assert t.bot.answered[-1]["text"] == ""  # acknowledged silently, before the lock
        assert len(t.bot.sent) == sent_before + 1  # the confirmation only: no replay
        assert t.chat.conversation(older).updated_at == updated_before  # selection is not activity
        assert selection(t).conversation_id == UUID(older)
        # The website keeps opening the conversation it opened before: the cursor is
        # Telegram's own (KH-TG-DLG-01).
        listing = (await t.chat.client.get("/api/conversations")).json()["conversations"]
        assert listing[0]["id"] == newer

        await t.deliver(MOTHER, "А к нему что подать?")
        history = (await t.chat.history(older)).json()["messages"]
        assert [(m["role"], m["text"]) for m in history][-2:] == [
            ("user", "А к нему что подать?"),
            ("assistant", "Сметану подавайте отдельно."),
        ]
        assert backend.requests[-1][0] == [
            {"role": "user", "content": "Что на обед?"},
            {"role": "assistant", "content": "Борщ."},
            {"role": "user", "content": "А к нему что подать?"},
        ]
        assert t.stored_texts(UUID(newer)) == [
            ("user", "Что на завтрак?"),
            ("assistant", "Омлет."),
        ]


async def test_the_selected_conversation_is_marked_in_the_list(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        older = await web_turn(t, "Старый вопрос", "w-1")
        newer = await web_turn(t, "Новый вопрос", "w-2")
        await open_dialogs(t)
        await choose(t, older)

        call = await open_dialogs(t)

        labels = {button["callback_data"].split(":")[1]: button["text"] for button in dialog_buttons(call)}
        assert labels[UUID(older).hex].startswith("✓ ")
        assert not labels[UUID(newer).hex].startswith("✓ ")


async def test_choosing_from_the_second_page_returns_the_way_back_there(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        created = [await empty_conversation(t) for _ in range(tc.DIALOGS_PAGE_SIZE + 1)]
        older = created[0]  # the last one on page 2

        await open_dialogs(t)
        await t.click(MOTHER, f"{tc.DIALOGS_LIST_PREFIX}:1")
        page_two = t.bot.sent[-1]
        await t.click(MOTHER, button_for(page_two, older)["callback_data"])

        assert save_buttons(t.bot.sent[-1]) == [
            {"text": tc.DIALOGS_BACK, "callback_data": f"{tc.DIALOGS_LIST_PREFIX}:1"}
        ]
        await t.click(MOTHER, f"{tc.DIALOGS_LIST_PREFIX}:1")
        assert button_for(t.bot.sent[-1], older)["callback_data"].endswith(":1")


async def test_a_conversation_deleted_before_the_click_is_one_notice_and_changes_nothing(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        kept = await empty_conversation(t)
        doomed = await empty_conversation(t)
        listing = await open_dialogs(t)
        await choose(t, kept)
        stale_button = button_for(listing, doomed)  # from the list sent before the delete
        await delete_conversation(t, doomed)
        before = len(t.bot.sent)

        await t.click(MOTHER, stale_button["callback_data"])

        assert len(t.bot.sent) == before + 1
        assert t.bot.sent[-1]["text"] == tc.DIALOG_UNAVAILABLE
        assert save_buttons(t.bot.sent[-1]) == [
            {"text": tc.DIALOGS_BACK, "callback_data": f"{tc.DIALOGS_LIST_PREFIX}:0"}
        ]
        assert selection(t).conversation_id == UUID(kept)  # nothing switched


async def test_a_callback_for_another_users_conversation_selects_nothing(tmp_path):
    async with telegram_app(tmp_path) as t:
        other = await t.chat.second_user()
        other_id = t.chat.extra_users["7777777777777777777"]
        await t.pair(MOTHER)
        await t.pair(SISTER, client=other)
        theirs = await t.chat.create(client=other)  # the sister's own conversation
        assert t.conversations(other_id)
        mine = await empty_conversation(t)
        await open_dialogs(t)
        await choose(t, mine)

        before = len(t.bot.sent)
        await t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(theirs).hex}:0")

        assert len(t.bot.sent) == before + 1
        assert t.bot.sent[-1]["text"] == tc.DIALOG_UNAVAILABLE
        assert selection(t).conversation_id == UUID(mine)


async def test_two_people_have_two_independent_cursors(tmp_path):
    backend = ScriptedBackend("Для мамы", "Для сестры")
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        other_id = t.chat.extra_users["7777777777777777777"]
        await t.pair(MOTHER)
        await t.pair(SISTER, client=other)

        await t.deliver(MOTHER, "/new")
        await t.deliver(SISTER, "/new")
        [mothers] = t.conversations()
        [sisters] = t.conversations(other_id)
        assert selection(t).conversation_id == mothers.id
        with t.chat.base.factory() as db:
            assert store.active_conversation(db, other_id).conversation_id == sisters.id

        await t.deliver(MOTHER, "Мамин вопрос")
        await t.deliver(SISTER, "Сестрин вопрос")

        assert [role for role, _ in t.stored_texts(mothers.id)] == ["user", "assistant"]
        assert [role for role, _ in t.stored_texts(sisters.id)] == ["user", "assistant"]
        assert t.stored_texts(mothers.id)[0] == ("user", "Мамин вопрос")
        assert t.stored_texts(sisters.id)[0] == ("user", "Сестрин вопрос")


async def test_a_stranger_cannot_open_or_choose_anything(tmp_path):
    async with telegram_app(tmp_path) as t:
        before = len(t.bot.sent)

        await t.click(STRANGER, f"{tc.DIALOGS_LIST_PREFIX}:0")
        await t.click(STRANGER, f"{tc.DIALOGS_SELECT_PREFIX}:{'a' * 32}:0")

        assert len(t.bot.sent) == before
        assert [entry["text"] for entry in t.bot.answered] == [tc.CALLBACK_NOT_LINKED] * 2


async def test_after_unlinking_the_cursor_is_gone_and_dialogs_are_not_served(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "/new")
        assert stored_selection_rows(t)

        await t.deliver(MOTHER, "/unlink")
        await t.deliver(MOTHER, "/dialogs")

        assert stored_selection_rows(t) == []
        assert t.bot.texts(MOTHER)[-1] == tc.NOT_LINKED


@pytest.mark.parametrize(
    "data, expected",
    [
        ("dl", tc.RECIPE_UNAVAILABLE),  # not even a prefix + ':' this bot writes
        ("dl:", tc.DIALOGS_UNAVAILABLE),
        ("dl:abc", tc.DIALOGS_UNAVAILABLE),
        ("dl:-1", tc.DIALOGS_UNAVAILABLE),
        ("dl:1:2", tc.DIALOGS_UNAVAILABLE),
        ("dl:" + "9" * 20, tc.DIALOGS_UNAVAILABLE),  # more digits than a page this bot writes
        ("ds", tc.RECIPE_UNAVAILABLE),
        ("ds:", tc.DIALOG_UNAVAILABLE),
        ("ds:abc:0", tc.DIALOG_UNAVAILABLE),
        ("ds:" + "a" * 31 + ":0", tc.DIALOG_UNAVAILABLE),
        ("ds:" + "g" * 32 + ":0", tc.DIALOG_UNAVAILABLE),
        ("ds:" + "a" * 32, tc.DIALOG_UNAVAILABLE),
        ("ds:" + "a" * 32 + ":" + "9" * 20, tc.DIALOG_UNAVAILABLE),
    ],
)
async def test_a_malformed_dialogs_callback_is_answered_and_sends_nothing(tmp_path, data, expected):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)

        await t.click(MOTHER, data)

        assert len(t.bot.sent) == before
        assert t.bot.answered[-1]["text"] == expected


async def test_a_dialogs_callback_from_a_group_or_without_a_message_sends_nothing(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)

        await t.click(MOTHER, f"{tc.DIALOGS_LIST_PREFIX}:0", chat_type="supergroup", chat_id=-100500)
        await t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{'a' * 32}:0", with_message=False)

        assert len(t.bot.sent) == before
        assert [entry["text"] for entry in t.bot.answered] == [
            tc.DIALOGS_UNAVAILABLE,
            tc.DIALOG_UNAVAILABLE,
        ]


# --- /new -------------------------------------------------------------------------------------


async def test_new_selects_a_new_conversation_and_asking_twice_on_an_empty_one_reuses_it(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)

        await t.deliver(MOTHER, "/new")
        first = selection(t).conversation_id
        await t.deliver(MOTHER, "/new")
        second = selection(t).conversation_id

        assert first == second
        assert len(t.conversations()) == 1
        assert t.bot.texts(MOTHER)[-2:] == [tc.NEW_CONVERSATION, tc.NEW_CONVERSATION]


async def test_new_from_a_nonempty_conversation_starts_and_selects_another(tmp_path):
    backend = ScriptedBackend("Ответ в старом", "Ответ в новом")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Первый вопрос")
        [old] = t.conversations()

        await t.deliver(MOTHER, "/new")
        assert selection(t).conversation_id not in (None, old.id)

        await t.deliver(MOTHER, "Второй вопрос")
        conversations = t.conversations()
        assert len(conversations) == 2
        new = next(item for item in conversations if item.id != old.id)
        assert t.stored_texts(old.id) == [
            ("user", "Первый вопрос"),
            ("assistant", "Ответ в старом"),
        ]
        assert t.stored_texts(new.id) == [
            ("user", "Второй вопрос"),
            ("assistant", "Ответ в новом"),
        ]


async def test_new_after_selecting_an_older_conversation_starts_from_that_one(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        older = await web_turn(t, "Старый вопрос", "w-1")
        await web_turn(t, "Новый вопрос", "w-2")
        await open_dialogs(t)
        await choose(t, older)

        await t.deliver(MOTHER, "/new")

        selected = selection(t).conversation_id
        assert selected not in (None, UUID(older))
        assert len(t.conversations()) == 3


# --- redelivery and idempotency ---------------------------------------------------------------


async def test_a_redelivery_goes_to_its_first_conversation_even_after_a_selection(tmp_path):
    backend = ScriptedBackend("Единственный ответ")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, "Первый вопрос")
        await t.deliver_update(update)
        [first] = t.conversations()
        t.chat.clock.advance(seconds=5)

        newer = await empty_conversation(t)
        await open_dialogs(t)
        await choose(t, newer)
        assert selection(t).conversation_id == UUID(newer)

        await t.deliver_update(update)

    assert backend.calls == 1
    assert t.bot.texts(MOTHER)[-1] == "Единственный ответ"
    assert t.stored_texts(first.id) == [
        ("user", "Первый вопрос"),
        ("assistant", "Единственный ответ"),
    ]
    assert t.stored_texts(UUID(newer)) == []
    assert selection(t).conversation_id == UUID(newer)  # the pointer was not consulted


async def test_a_redelivery_whose_own_conversation_is_deleted_is_not_run_again_elsewhere(
    tmp_path, monkeypatch
):
    """Codex KH-TG-DLG-01 BLOCKER 1, exactly: update X completes in A, the person selects B,
    Telegram redelivers X, and A is deleted after the resolution but before the claim. The
    retry must not treat X as a fresh request in B (a second provider call and a duplicate
    turn); it says, in fixed words, that the conversation is gone."""
    backend = ScriptedBackend("Первый ответ", "Второй ответ")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, "Первый вопрос")
        await t.deliver_update(update)
        [first] = t.conversations()
        t.chat.clock.advance(seconds=5)
        selected = await empty_conversation(t)
        await open_dialogs(t)
        await choose(t, selected)
        assert selection(t).conversation_id == UUID(selected)

        runner = t.chat.app.state.chat
        real_submit = runner.submit
        deleted = []

        async def submit(**kwargs):
            if not deleted:
                deleted.append(kwargs["conversation_id"])
                with t.chat.base.factory() as db, db.begin():
                    cv.delete_conversation(db, t.user_id, kwargs["conversation_id"])
            return await real_submit(**kwargs)

        monkeypatch.setattr(runner, "submit", submit)
        await t.deliver_update(update)

    assert deleted == [first.id]  # the claim really was pointed at the deleted conversation
    assert backend.calls == 1  # the assistant was not asked again
    assert t.stored_texts(UUID(selected)) == []  # nothing was written into the selected one
    assert selection(t).conversation_id == UUID(selected)  # the cursor was not used instead
    assert t.bot.texts(MOTHER)[-1] == tc.REDELIVERY_GONE


# --- from the website -------------------------------------------------------------------------


async def test_the_website_copy_follows_the_selection(tmp_path):
    backend = ScriptedBackend("Ответ сайта", "Ответ сайта", "Ответ сайта")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        first = await web_turn(t, "Сайт 1", "w-1")
        second = await web_turn(t, "Сайт 2", "w-2")
        # No selection yet: both conversations were copied (the legacy rule).
        assert "Вы (на сайте):\nСайт 1" in t.bot.texts(MOTHER)
        assert "Вы (на сайте):\nСайт 2" in t.bot.texts(MOTHER)

        await open_dialogs(t)
        await choose(t, first)
        await t.settle()
        before = len(t.bot.sent)

        await t.chat.send(first, "Сайт в выбранном", "w-3")
        await t.settle()
        copied_from_selected = t.bot.texts(MOTHER)[before:]

        await t.chat.send(second, "Сайт в другом", "w-4")
        await t.settle()

    assert copied_from_selected == ["Вы (на сайте):\nСайт в выбранном", "Ответ сайта"]
    assert t.bot.texts(MOTHER)[before + 2 :] == []  # the other conversation was not copied


async def test_nothing_is_copied_while_the_selected_conversation_is_deleted(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        selected = await web_turn(t, "Выбранный", "w-1")
        other = await web_turn(t, "Другой", "w-2")
        await open_dialogs(t)
        await choose(t, selected)
        await t.settle()
        await delete_conversation(t, selected)
        before = len(t.bot.sent)

        await t.chat.send(other, "После удаления", "w-3")
        await t.settle()

        assert t.bot.texts(MOTHER)[before:] == []
        assert selection(t).conversation_id is None
        call = await open_dialogs(t)
        assert tc.DIALOGS_DELETED_NOTE in call["text"]


# --- concurrency and deletion -----------------------------------------------------------------


async def test_switching_waits_for_a_message_being_answered_and_then_applies(tmp_path):
    backend = ScriptedBackend("Долгий ответ")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        first = await empty_conversation(t)
        second = await empty_conversation(t)
        await open_dialogs(t)
        await choose(t, first)

        arrived, release = backend.hold()
        answering = asyncio.ensure_future(t.deliver(MOTHER, "Долгий вопрос"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)

        answered_before_click = len(t.bot.answered)
        switching = asyncio.ensure_future(t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(second).hex}:0"))
        await until(in_line(t, MOTHER, 2))  # the click is waiting for the chat's turn
        # The callback was acknowledged before it waited: the button never spins for the answer.
        assert len(t.bot.answered) == answered_before_click + 1
        assert t.bot.answered[-1]["text"] == ""

        release.set()
        await asyncio.gather(answering, switching)

        assert t.stored_texts(UUID(first)) == [("user", "Долгий вопрос"), ("assistant", "Долгий ответ")]
        assert selection(t).conversation_id == UUID(second)
        # The answer was delivered first, then the selection's confirmation.
        assert t.bot.texts(MOTHER)[-2] == "Долгий ответ"
        assert "выбран" in t.bot.texts(MOTHER)[-1].lower()

        await t.deliver(MOTHER, "После переключения")
        assert t.stored_texts(UUID(second))[0] == ("user", "После переключения")


async def test_two_selections_at_once_are_serialized_and_the_last_one_wins(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        first = await empty_conversation(t)
        second = await empty_conversation(t)

        async with t.service._chat_lock(MOTHER):  # hold the turn to make both clicks queue
            earlier = asyncio.ensure_future(
                t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(first).hex}:0")
            )
            later = asyncio.ensure_future(
                t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(second).hex}:0")
            )
            await until(in_line(t, MOTHER, 3))  # the test and the two clicks

        await asyncio.gather(earlier, later)

        assert selection(t).conversation_id == UUID(second)  # both ran, in the order they came
        confirmations = [text for text in t.bot.texts(MOTHER) if text.startswith("🗂")]
        assert len(confirmations) == 2
        assert [entry["text"] for entry in t.bot.answered] == ["", ""]  # both acknowledged


async def test_a_cancelled_selection_keeps_the_chat_lock_until_its_transaction_ends(
    tmp_path, monkeypatch
):
    """Codex KH-TG-DLG-01 IMPORTANT 3: a selection accepted just before its task is
    cancelled must not release the chat lock while its transaction is still running. The
    later selection is written, and the cancelled one must not commit over it. The
    transaction is held open on the worker thread (a real accepted unit); the cancellation
    is delivered while it is open."""
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        first = await empty_conversation(t)
        second = await empty_conversation(t)

        database = t.service._database
        real_run_to_completion = database.run_to_completion
        entered, release = threading.Event(), threading.Event()

        async def paused(work):
            def hold_transaction(session):
                outcome = work(session)
                entered.set()
                release.wait(5)
                return outcome

            return await real_run_to_completion(hold_transaction)

        monkeypatch.setattr(database, "run_to_completion", paused)
        cancelled = asyncio.ensure_future(
            t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(first).hex}:0")
        )
        await until(entered.is_set)
        cancelled.cancel()
        for _ in range(20):  # let the cancellation be delivered
            await asyncio.sleep(0)
        assert not cancelled.done()  # the transaction is open, so the selected-write is not over

        later = asyncio.ensure_future(
            t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(second).hex}:0")
        )
        await until(in_line(t, MOTHER, 2))  # the cancelled holder, and the later selection
        release.set()
        results = await asyncio.gather(cancelled, later, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    assert selection(t).conversation_id == UUID(second)  # the later choice is the one kept


async def test_a_cancelled_new_cannot_overwrite_a_later_selection(tmp_path, monkeypatch):
    """The same rule for /new: its cursor write is a run_to_completion unit, so a cancelled
    /new cannot commit after (and over) a selection made while it was cancelled."""
    backend = ScriptedBackend("Ответ")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        chosen = await web_turn(t, "Вопрос", "w-1")  # a non-empty latest conversation

        database = t.service._database
        real_run_to_completion = database.run_to_completion
        entered, release = threading.Event(), threading.Event()

        async def paused(work):
            def hold_transaction(session):
                outcome = work(session)
                entered.set()
                release.wait(5)
                return outcome

            return await real_run_to_completion(hold_transaction)

        monkeypatch.setattr(database, "run_to_completion", paused)
        started = asyncio.ensure_future(t.deliver(MOTHER, "/new"))
        await until(entered.is_set)
        started.cancel()
        for _ in range(20):
            await asyncio.sleep(0)
        assert not started.done()

        later = asyncio.ensure_future(
            t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(chosen).hex}:0")
        )
        await until(in_line(t, MOTHER, 2))
        release.set()
        results = await asyncio.gather(started, later, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    assert selection(t).conversation_id == UUID(chosen)  # the later choice, not /new's


async def test_a_database_failure_during_selection_is_said_and_keeps_the_previous_selection(
    tmp_path, monkeypatch
):
    """Codex KH-TG-DLG-01 MINOR 6: a failed selection transaction reports a fixed failure
    through the ordinary binding-checked delivery, and the previous selection stays."""
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        kept = await empty_conversation(t)
        target = await empty_conversation(t)
        await open_dialogs(t)
        await choose(t, kept)

        def broken(session, user_id, conversation_id, *, now, telegram_user_id=None):
            raise sa.exc.OperationalError("SELECT 1", {}, Exception("database went away"))

        monkeypatch.setattr(store, "select_conversation", broken)
        await t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(target).hex}:0")

        assert t.bot.sent[-1]["text"] == tc.DIALOG_SELECT_FAILED
        assert save_buttons(t.bot.sent[-1]) == [
            {"text": tc.DIALOGS_BACK, "callback_data": f"{tc.DIALOGS_LIST_PREFIX}:0"}
        ]
        assert selection(t).conversation_id == UUID(kept)


async def test_a_conversation_deleted_inside_the_selection_transaction_is_unavailable(
    tmp_path, monkeypatch
):
    """The FK deletion race: the target is read as the user's, then deleted before the
    cursor write, which the foreign key refuses. That is the "no longer available" notice,
    not a success and not a generic failure; the transaction rolled back, so the previous
    selection stands."""
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        kept = await empty_conversation(t)
        target = await empty_conversation(t)
        await open_dialogs(t)
        await choose(t, kept)

        real_summary = store.conversation_summary

        def summary_then_delete(session, user_id, conversation_id, *, now):
            summary = real_summary(session, user_id, conversation_id, now=now)
            cv.delete_conversation(session, user_id, conversation_id)
            return summary

        monkeypatch.setattr(store, "conversation_summary", summary_then_delete)
        await t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(target).hex}:0")

        assert t.bot.sent[-1]["text"] == tc.DIALOG_UNAVAILABLE
        assert selection(t).conversation_id == UUID(kept)


async def test_dialogs_reads_under_the_chat_lock_and_marks_a_queued_selection(tmp_path):
    """Codex KH-TG-DLG-01 MINOR 7: a selection queued before /dialogs is reflected in the
    list. The read now happens under the same fair chat lock as the delivery, so the
    selection's committed write lands first."""
    backend = ScriptedBackend("Ответ")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        older = await web_turn(t, "Старый вопрос", "w-1")
        newer = await web_turn(t, "Новый вопрос", "w-2")

        async with t.service._chat_lock(MOTHER):
            choosing = asyncio.ensure_future(
                t.click(MOTHER, f"{tc.DIALOGS_SELECT_PREFIX}:{UUID(older).hex}:0")
            )
            await until(in_line(t, MOTHER, 2))
            listing = asyncio.ensure_future(t.deliver(MOTHER, "/dialogs"))
            await until(in_line(t, MOTHER, 3))

        await asyncio.gather(choosing, listing)

        labels = {
            button["callback_data"].split(":")[1]: button["text"]
            for button in dialog_buttons(t.bot.sent[-1])
        }
        assert labels[UUID(older).hex].startswith("✓ ")
        assert not labels[UUID(newer).hex].startswith("✓ ")


async def test_a_conversation_deleted_between_resolution_and_claim_is_recovered(tmp_path, monkeypatch):
    backend = ScriptedBackend("Ответ в новом")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        selected = await empty_conversation(t)
        await open_dialogs(t)
        await choose(t, selected)
        runner = t.chat.app.state.chat
        real_submit = runner.submit
        deleted = []

        async def submit(**kwargs):
            if not deleted:
                deleted.append(kwargs["conversation_id"])
                with t.chat.base.factory() as db, db.begin():
                    cv.delete_conversation(db, t.user_id, kwargs["conversation_id"])
            return await real_submit(**kwargs)

        monkeypatch.setattr(runner, "submit", submit)
        await t.deliver(MOTHER, "Сообщение во время удаления")

    assert deleted == [UUID(selected)]
    assert backend.calls == 1  # the same message was resolved again, not generated twice
    conversations = t.conversations()
    assert len(conversations) == 1
    [new] = conversations
    assert new.id != UUID(selected)
    assert t.stored_texts(new.id) == [
        ("user", "Сообщение во время удаления"),
        ("assistant", "Ответ в новом"),
    ]
    assert selection(t).conversation_id == new.id


async def test_a_conversation_deleted_while_the_answer_is_being_prepared_is_said_not_retried(tmp_path):
    backend = ScriptedBackend("Ответ, который не сохранится", "Ответ в новом")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        selected = await empty_conversation(t)
        await open_dialogs(t)
        await choose(t, selected)

        arrived, release = backend.hold()
        answering = asyncio.ensure_future(t.deliver(MOTHER, "Вопрос"))
        await asyncio.wait_for(arrived.wait(), TIMEOUT)
        await delete_conversation(t, selected)
        release.set()
        await asyncio.gather(answering)

        assert t.bot.texts(MOTHER)[-1] == tc.DIALOG_DELETED
        assert backend.calls == 1  # nothing was retried automatically
        assert selection(t).conversation_id is None
        call = await open_dialogs(t)
        assert tc.DIALOGS_DELETED_NOTE in call["text"]

        await t.deliver(MOTHER, "Новый вопрос")
        [new] = t.conversations()
        assert t.stored_texts(new.id)[0] == ("user", "Новый вопрос")
        assert selection(t).conversation_id == new.id


async def test_a_photo_follows_the_selected_conversation(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Вижу фото.")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "/new")
        selected = selection(t).conversation_id
        t.bot.files["photo-1"] = ("photos/photo-1.jpg", jpeg(4000, "p"))

        await t.deliver_update(photo_message(t.next_id(), MOTHER, "photo-1", caption="Что это?"))

        assert t.bot.texts(MOTHER)[-1] == "Вижу фото."
        [attachment] = t.chat.rows(MessageAttachment)
        assert attachment.message_id is not None
        assert t.stored_texts(selected)[0] == ("user", "Что это?")
        assert selection(t).conversation_id == selected


# --- the menu and the surface -----------------------------------------------------------------


async def test_a_dialogs_command_for_another_bot_is_not_ours(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "/dialogs@some_other_bot")

        assert t.bot.texts(MOTHER)[-1] == tc.UNKNOWN_COMMAND
        assert t.chat.backend.calls == 0


async def test_no_message_ever_carries_a_persistent_reply_keyboard(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "/start")
        await t.deliver(MOTHER, "/help")
        await t.deliver(MOTHER, "/dialogs")
        await t.deliver(MOTHER, "/new")

    for call in t.bot.sent:
        markup = call.get("reply_markup") or {}
        assert "keyboard" not in markup  # only inline keyboards, never a reply keyboard
