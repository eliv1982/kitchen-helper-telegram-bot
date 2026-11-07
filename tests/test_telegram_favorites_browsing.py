"""Reading favorites in Telegram: the /favorites command, its pages, and one recipe.

The bot is the real TelegramChat over the real database and application; the Bot API is a
FakeBot and the assistant a scripted backend. What is pinned here:

  * /favorites opens the linked user's own list (empty state included), with one button per
    recipe title, newest first, and pages that move forward and back by the same deterministic
    order;
  * a recipe is shown as its complete current saved text -- the same content the website
    returns, including a version edited on the website -- and a long one is split into
    Telegram-sized pieces with the way back under the last piece;
  * the way back returns to the page the recipe was opened from, and a page that no longer
    exists falls back to the last one;
  * the sender is resolved from the update and the current Telegram link; callback data names
    a page or a favorite id, never an owner, so a foreign, deleted, stale or made-up id opens
    nothing, and an unlinked account gets nothing;
  * recipes saved on the website and in Telegram appear in the same list;
  * a new save sends one persistent confirmation with an "Открыть избранное" button, a
    repeated save adds no message, and a multi-recipe answer's own buttons are left intact;
  * the native command menu is registered once per start, in Russian, and its failure never
    stops polling;
  * browsing makes no provider call and writes nothing.
"""

import json
import logging
from datetime import timedelta
from typing import Optional
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from house_helper.core.backend import BackendReply
from house_helper.core.recipes import RECIPE_END, RECIPE_START, parse_reply, render_parsed_reply
from house_helper.persistence import favorites
from house_helper.persistence.models import FavoriteRecipe
from house_helper.web import telegram_chat as tc
from house_helper.web.telegram_botapi import MAX_MESSAGE_LENGTH, split_text
from tests.chat_support import ScriptedBackend, post_headers
from tests.telegram_support import (
    MOTHER,
    SISTER,
    STRANGER,
    FakeBot,
    save_buttons,
    telegram_app,
)

pytestmark = pytest.mark.asyncio

BORSH = {
    "title": "Борщ",
    "servings": "4 порции",
    "ingredients": [
        {"name": "свекла", "amount": "2 шт."},
        {"name": "капуста", "amount": "300 г"},
    ],
    "steps": ["Сварите бульон.", "Добавьте овощи."],
}
UKHA = {
    "title": "Уха",
    "servings": None,
    "ingredients": [{"name": "рыба", "amount": "1 кг"}],
    "steps": ["Сварите.", "Дайте настояться."],
}


def block(recipe) -> str:
    return f"{RECIPE_START}\n{json.dumps(recipe, ensure_ascii=False)}\n{RECIPE_END}"


RAW_ONE = "Вот рецепт.\n" + block(BORSH) + "\nПриятного аппетита."
RAW_TWO = "Вот два варианта.\n" + block(BORSH) + "\nИ еще один.\n" + block(UKHA)


def reply(raw: str) -> BackendReply:
    parsed = parse_reply(raw)
    return BackendReply(text=render_parsed_reply(parsed), recipes=parsed.recipes)


def backend_with(*raw: str) -> ScriptedBackend:
    return ScriptedBackend(*(reply(item) for item in raw))


# Seeded favorites never collide with real message ids, and each seeding gets its own range.
_SEEDED = 1_000_000


def seed_favorites(
    t,
    count: int,
    *,
    user_id: Optional[UUID] = None,
    prefix: str = "Блюдо",
    content: Optional[str] = None,
    title: Optional[str] = None,
) -> list[FavoriteRecipe]:
    """`count` text favorites of one user, created one second apart.

    A seeded row is an ordinary text favorite (the kind the website has always written); it
    is inserted directly so that the number of rows, their timestamps and their order are
    exact. Reading them goes through the real list/detail services, as the bot does.
    """
    global _SEEDED
    owner = user_id or t.user_id
    base = t.chat.clock.current
    rows = []
    with t.chat.base.factory() as db, db.begin():
        for number in range(count):
            created = base + timedelta(seconds=number)
            selected_title = title if title is not None else f"{prefix} {number + 1}"
            body = content if content is not None else (
                f"{selected_title}\n\nИнгредиенты:\n- еда — 1 шт.\n\n"
                "Приготовление:\n1. Готовьте."
            )
            row = FavoriteRecipe(
                user_id=owner,
                title=selected_title,
                content=body,
                source_message_id=_SEEDED,
                kind="text",
                source_recipe_index=-1,
                version=1,
                created_at=created,
                updated_at=created,
            )
            _SEEDED += 1
            db.add(row)
            rows.append(row)
    return rows


def item_buttons(call) -> list[dict]:
    """The title buttons of a list message, without the navigation row."""
    return [button for button in save_buttons(call) if button["text"] not in ("◀ Назад", "Далее ▶")]


def button_with_text(call, text: str) -> dict:
    return next(button for button in save_buttons(call) if button["text"] == text)


def opened_buttons(t) -> list[dict]:
    """The buttons of the last message the bot sent, in keyboard order."""
    return save_buttons(t.bot.sent[-1])


# --- the command and the empty state --------------------------------------------------------


async def test_favorites_command_shows_the_friendly_empty_state_without_controls(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "/favorites")

        assert t.bot.texts(MOTHER)[-1] == tc.FAVORITES_EMPTY
        assert "пусто" in t.bot.texts(MOTHER)[-1]
        assert "reply_markup" not in t.bot.sent[-1]
        assert t.chat.backend.calls == 0


async def test_a_favorites_command_for_another_bot_is_not_ours(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "/favorites@some_other_bot")

        assert t.bot.texts(MOTHER)[-1] == tc.UNKNOWN_COMMAND
        assert t.chat.backend.calls == 0


async def test_the_command_works_after_a_restart(tmp_path):
    async with telegram_app(tmp_path) as first:
        await first.pair(MOTHER)
        seed_favorites(first, 1, title="Из прошлого запуска")

    async with telegram_app(tmp_path, ScriptedBackend(), sign_in_first=False, seed=False) as second:
        await second.deliver(MOTHER, "/favorites")

    assert item_buttons(second.bot.sent[-1])[0]["text"] == "Из прошлого запуска"


# --- a list of titles ------------------------------------------------------------------------


async def test_favorites_lists_each_title_as_a_button_newest_first(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        seeded = seed_favorites(t, 3)
        await t.deliver(MOTHER, "/favorites")

        listed = item_buttons(t.bot.sent[-1])
        assert [button["text"] for button in listed] == ["Блюдо 3", "Блюдо 2", "Блюдо 1"]
        assert [button["callback_data"] for button in listed] == [
            f"fo:{row.id.hex}:0" for row in reversed(seeded)
        ]
        assert all(len(button["callback_data"].encode("ascii")) <= 64 for button in listed)
        assert "Ваше избранное" in t.bot.texts(MOTHER)[-1]
        assert "Нажмите на рецепт" in t.bot.texts(MOTHER)[-1]
        assert t.chat.backend.calls == 0


async def test_a_long_title_stays_inside_the_button_bound(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        seed_favorites(t, 1, title="Очень длинное название блюда " * 6)
        await t.deliver(MOTHER, "/favorites")

        [button] = item_buttons(t.bot.sent[-1])
        assert len(button["text"].encode("utf-8")) <= tc.SAVE_BUTTON_BYTES
        assert button["text"].endswith(tc.BUTTON_ELLIPSIS)


# --- one recipe ------------------------------------------------------------------------------


async def test_opening_a_recipe_shows_the_complete_saved_content_and_the_way_back(tmp_path):
    backend = backend_with(RAW_ONE)
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Как сварить борщ?")
        [save] = save_buttons(t.bot.sent[-1])
        await t.click(MOTHER, save["callback_data"])

        await t.deliver(MOTHER, "/favorites")
        [opened] = item_buttons(t.bot.sent[-1])
        await t.click(MOTHER, opened["callback_data"])

        stored = t.chat.rows(FavoriteRecipe)[0]
        assert t.bot.sent[-1]["text"] == stored.content
        shown = t.bot.texts(MOTHER)[-1]
        assert "Борщ" in shown
        assert "4 порции" in shown
        assert "свекла — 2 шт." in shown and "капуста — 300 г" in shown
        assert "Сварите бульон." in shown and "Добавьте овощи." in shown
        assert save_buttons(t.bot.sent[-1]) == [
            {"text": tc.FAVORITES_BACK, "callback_data": "fl:0"}
        ]
        assert t.bot.answered[-1]["text"] == ""  # the click is acknowledged silently
        assert backend.calls == 1  # only the answer that produced the recipe


async def test_a_version_edited_on_the_website_is_what_telegram_shows(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [save] = save_buttons(t.bot.sent[-1])
        await t.click(MOTHER, save["callback_data"])

        listing = await t.chat.client.get("/api/favorites")
        [item] = listing.json()["favorites"]
        edited = await t.chat.client.patch(
            f"/api/favorites/{item['id']}",
            json={
                "expected_version": item["version"],
                "servings": "6 порций",
                "notes": "Без соли.",
            },
            headers=post_headers(),
        )
        assert edited.status_code == 200, edited.text
        current = edited.json()

        await t.deliver(MOTHER, "/favorites")
        [opened] = item_buttons(t.bot.sent[-1])
        await t.click(MOTHER, opened["callback_data"])

        assert t.bot.sent[-1]["text"] == current["text"]
        assert "6 порций" in t.bot.sent[-1]["text"]
        assert "Мои заметки:" in t.bot.sent[-1]["text"]
        assert "Без соли." in t.bot.sent[-1]["text"]


async def test_a_long_recipe_is_split_with_the_way_back_under_the_last_piece(tmp_path):
    content = "\n\n".join(("абв " * 200).strip() for _ in range(12))
    assert len(content) > MAX_MESSAGE_LENGTH
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        seed_favorites(t, 1, title="Длинный рецепт", content=content)
        await t.deliver(MOTHER, "/favorites")
        [opened] = item_buttons(t.bot.sent[-1])

        before = len(t.bot.sent)
        await t.click(MOTHER, opened["callback_data"])
        pieces = t.bot.sent[before:]

        assert [piece["text"] for piece in pieces] == split_text(content)
        assert len(pieces) > 1
        assert all(len(piece["text"]) <= MAX_MESSAGE_LENGTH for piece in pieces)
        assert all("reply_markup" not in piece for piece in pieces[:-1])
        assert pieces[-1]["reply_markup"] == {
            "inline_keyboard": [[{"text": tc.FAVORITES_BACK, "callback_data": "fl:0"}]]
        }


# --- pagination ------------------------------------------------------------------------------


async def test_pages_move_forward_and_back_in_the_same_order(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        seeded = seed_favorites(t, tc.FAVORITES_PAGE_SIZE + 2)
        expected = [row.title for row in reversed(seeded)]

        await t.deliver(MOTHER, "/favorites")
        first = item_buttons(t.bot.sent[-1])
        assert [button["text"] for button in first] == expected[: tc.FAVORITES_PAGE_SIZE]
        assert "Страница 1 из 2" in t.bot.texts(MOTHER)[-1]
        assert "◀ Назад" not in [button["text"] for button in save_buttons(t.bot.sent[-1])]

        await t.click(MOTHER, button_with_text(t.bot.sent[-1], "Далее ▶")["callback_data"])
        second = item_buttons(t.bot.sent[-1])
        assert [button["text"] for button in second] == expected[tc.FAVORITES_PAGE_SIZE :]
        assert "Страница 2 из 2" in t.bot.texts(MOTHER)[-1]
        assert "Далее ▶" not in [button["text"] for button in save_buttons(t.bot.sent[-1])]

        await t.click(MOTHER, button_with_text(t.bot.sent[-1], "◀ Назад")["callback_data"])
        assert [button["text"] for button in item_buttons(t.bot.sent[-1])] == expected[
            : tc.FAVORITES_PAGE_SIZE
        ]


async def test_the_last_page_of_the_full_limit_is_reachable_and_final(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        seeded = seed_favorites(t, favorites.FAVORITES_LIMIT)
        pages = tc.MAX_FAVORITE_PAGES
        last = pages - 1
        remaining = len(seeded) - last * tc.FAVORITES_PAGE_SIZE
        expected = [row.title for row in reversed(seeded)]

        await t.deliver(MOTHER, "/favorites")
        await t.click(MOTHER, f"fl:{last}")

        assert len(item_buttons(t.bot.sent[-1])) == remaining
        assert [button["text"] for button in item_buttons(t.bot.sent[-1])] == expected[
            last * tc.FAVORITES_PAGE_SIZE :
        ]
        assert f"Страница {pages} из {pages}" in t.bot.texts(MOTHER)[-1]
        labels = [button["text"] for button in save_buttons(t.bot.sent[-1])]
        assert "◀ Назад" in labels and "Далее ▶" not in labels
        # The order the buttons show is exactly the service's order, tie-break included.
        with t.chat.base.factory() as db:
            canonical = [item.title for item in favorites.list_favorites(db, t.user_id)]
        assert canonical == expected


async def test_the_way_back_returns_to_the_page_the_recipe_was_opened_from(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        seeded = seed_favorites(t, tc.FAVORITES_PAGE_SIZE + 2)
        expected = [row.title for row in reversed(seeded)]

        await t.deliver(MOTHER, "/favorites")
        await t.click(MOTHER, "fl:1")
        page_two = item_buttons(t.bot.sent[-1])
        await t.click(MOTHER, page_two[0]["callback_data"])
        assert save_buttons(t.bot.sent[-1]) == [
            {"text": tc.FAVORITES_BACK, "callback_data": "fl:1"}
        ]

        await t.click(MOTHER, "fl:1")
        assert [button["text"] for button in item_buttons(t.bot.sent[-1])] == expected[
            tc.FAVORITES_PAGE_SIZE :
        ]


async def test_a_page_that_no_longer_exists_falls_back_to_the_last_page(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        seeded = seed_favorites(t, 10)

        await t.deliver(MOTHER, "/favorites")
        with t.chat.base.factory() as db, db.begin():
            db.execute(
                sa.delete(FavoriteRecipe).where(
                    FavoriteRecipe.user_id == t.user_id,
                    FavoriteRecipe.id.in_([row.id for row in seeded[:8]]),
                )
            )

        await t.click(MOTHER, "fl:1")  # the second page is gone with the deleted favorites

        assert [button["text"] for button in item_buttons(t.bot.sent[-1])] == [
            "Блюдо 10",
            "Блюдо 9",
        ]
        assert "Страница" not in t.bot.texts(MOTHER)[-1]  # one page now
        assert t.bot.answered[-1]["text"] == ""


# --- one list for the website and Telegram ---------------------------------------------------


async def test_recipes_saved_on_the_website_and_in_telegram_are_one_list(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE, RAW_TWO)) as t:
        await t.pair(MOTHER)

        # One saved with the bot's button, from a Telegram answer.
        await t.deliver(MOTHER, "Борщ?")
        [save] = save_buttons(t.bot.sent[-1])
        await t.click(MOTHER, save["callback_data"])

        # One saved through the website's own API, from the recipe of a web answer.
        t.chat.clock.advance(seconds=1)
        conversation = await t.chat.create()
        sent = await t.chat.send(conversation, "А что еще?", "web-1")
        assert sent.status_code == 201, sent.text
        await t.settle()
        assistant = [m for m in t.chat.messages(conversation) if m.role == "assistant"][-1]
        kept = await t.chat.client.post(
            "/api/favorites",
            json={
                "conversation_id": conversation,
                "message_id": assistant.id,
                "recipe_index": 1,
            },
            headers=post_headers(),
        )
        assert kept.status_code == 201, kept.text

        await t.deliver(MOTHER, "/favorites")
        titles = [button["text"] for button in item_buttons(t.bot.sent[-1])]
        assert titles == ["Уха", "Борщ"]  # the newer favorite first, whichever channel saved it


# --- identity, ownership and stale buttons ---------------------------------------------------


async def test_each_user_sees_only_their_own_favorites(tmp_path):
    async with telegram_app(tmp_path) as t:
        other = await t.chat.second_user()
        other_id = t.chat.extra_users["7777777777777777777"]
        await t.pair(MOTHER)
        await t.pair(SISTER, client=other)
        seed_favorites(t, 1, title="Блюдо мамы")
        sister_rows = seed_favorites(t, 1, user_id=other_id, title="Блюдо сестры")

        await t.deliver(MOTHER, "/favorites")
        assert [button["text"] for button in item_buttons(t.bot.sent[-1])] == ["Блюдо мамы"]

        await t.deliver(SISTER, "/favorites")
        assert [button["text"] for button in item_buttons(t.bot.sent[-1])] == ["Блюдо сестры"]

        # The sister's id, sent by the mother, is not the mother's to open.
        before = len(t.bot.sent)
        await t.click(MOTHER, f"fo:{sister_rows[0].id.hex}:0")

        assert len(t.bot.sent) == before
        assert t.bot.answered[-1]["text"] == tc.FAVORITE_UNAVAILABLE


async def test_a_deleted_or_made_up_favorite_opens_nothing(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        [row] = seed_favorites(t, 1, title="Удаляемый")
        await t.deliver(MOTHER, "/favorites")
        [button] = item_buttons(t.bot.sent[-1])

        with t.chat.base.factory() as db, db.begin():
            db.execute(
                sa.delete(FavoriteRecipe).where(FavoriteRecipe.id == row.id)
            )

        before = len(t.bot.sent)
        await t.click(MOTHER, button["callback_data"])
        assert len(t.bot.sent) == before
        assert t.bot.answered[-1]["text"] == tc.FAVORITE_UNAVAILABLE

        await t.click(MOTHER, f"fo:{uuid4().hex}:0")
        assert len(t.bot.sent) == before
        assert t.bot.answered[-1]["text"] == tc.FAVORITE_UNAVAILABLE


@pytest.mark.parametrize(
    "data",
    [
        "fl",
        "fl:",
        "fl:abc",
        "fl:-1",
        "fl:1:2",
        "fl:99",
        "fl:" + "9" * 20,
        "fo",
        "fo:",
        "fo:abc:0",
        "fo:" + "a" * 31 + ":0",
        "fo:" + "g" * 32 + ":0",
        "fo:" + "a" * 32,
        "fo:" + "a" * 32 + ":99",
    ],
)
async def test_a_malformed_favorites_callback_is_answered_and_sends_nothing(tmp_path, data):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)

        await t.click(MOTHER, data)

        assert len(t.bot.sent) == before
        if data in (tc.FAVORITES_LIST_PREFIX, tc.FAVORITES_OPEN_PREFIX):
            expected = tc.RECIPE_UNAVAILABLE  # not even a prefix + ':' this bot writes
        elif data.startswith(tc.FAVORITES_LIST_PREFIX + ":"):
            expected = tc.FAVORITES_UNAVAILABLE
        else:
            expected = tc.FAVORITE_UNAVAILABLE
        assert t.bot.answered[-1]["text"] == expected


async def test_a_stranger_cannot_open_the_list_or_a_recipe(tmp_path):
    async with telegram_app(tmp_path) as t:
        before = len(t.bot.sent)

        await t.click(STRANGER, "fl:0")
        await t.click(STRANGER, f"fo:{uuid4().hex}:0")

        assert len(t.bot.sent) == before
        assert [entry["text"] for entry in t.bot.answered] == [tc.CALLBACK_NOT_LINKED] * 2


async def test_after_unlinking_the_list_is_not_served(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        seed_favorites(t, 1)
        await t.deliver(MOTHER, "/unlink")
        await t.deliver(MOTHER, "/favorites")

        assert t.bot.texts(MOTHER)[-1] == tc.NOT_LINKED


async def test_a_favorites_callback_from_a_group_or_without_a_message_sends_nothing(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        before = len(t.bot.sent)

        await t.click(MOTHER, "fl:0", chat_type="supergroup", chat_id=-100500)
        await t.click(MOTHER, "fl:0", with_message=False)

        assert len(t.bot.sent) == before
        assert [entry["text"] for entry in t.bot.answered] == [tc.FAVORITES_UNAVAILABLE] * 2


# --- the persistent confirmation -------------------------------------------------------------


async def test_a_new_save_leaves_a_confirmation_with_a_way_to_the_list(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_TWO)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Что приготовить?")
        first_send = t.bot.sent[-1]
        assert [button["text"] for button in save_buttons(first_send)] == [
            "Сохранить «Борщ»",
            "Сохранить «Уха»",
        ]
        second = save_buttons(first_send)[1]

        await t.click(MOTHER, second["callback_data"])

        confirmation = t.bot.sent[-1]
        assert confirmation["text"] == "Рецепт «Уха» сохранен в избранное."
        assert save_buttons(confirmation) == [
            {"text": tc.FAVORITES_OPEN, "callback_data": "fl:0"}
        ]
        # The answer's own multi-recipe keyboard is untouched: both save buttons still work.
        assert [button["text"] for button in save_buttons(first_send)] == [
            "Сохранить «Борщ»",
            "Сохранить «Уха»",
        ]

        await t.click(MOTHER, "fl:0")
        assert [button["text"] for button in item_buttons(t.bot.sent[-1])] == ["Уха"]


async def test_a_repeated_save_sends_no_second_confirmation(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [save] = save_buttons(t.bot.sent[-1])

        await t.click(MOTHER, save["callback_data"])
        sent = len(t.bot.sent)
        await t.click(MOTHER, save["callback_data"])

        assert len(t.bot.sent) == sent  # the toast says "already saved"; no new message
        assert [entry["text"] for entry in t.bot.answered] == [tc.SAVED, tc.ALREADY_SAVED]


# --- the native command menu -----------------------------------------------------------------


async def test_the_bot_registers_the_russian_command_menu_in_its_fixed_order(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.settle()

    assert t.bot.commands == tc.BOT_COMMANDS
    assert [item["command"] for item in t.bot.commands] == [
        "dialogs",
        "new",
        "favorites",
        "help",
    ]
    descriptions = {item["command"]: item["description"] for item in t.bot.commands}
    assert descriptions == {
        "dialogs": "🗂 Диалоги",
        "new": "✏️ Новый диалог",
        "favorites": "📚 Избранное",
        "help": "ℹ️ Помощь",
    }
    # /start stays functional but is not in the visible menu; /unlink is in /help only.
    assert "start" not in descriptions and "unlink" not in descriptions
    for description in descriptions.values():
        assert 0 < len(description) <= 256


async def test_the_menu_is_idempotent_across_two_starts(tmp_path):
    bot = FakeBot()
    async with telegram_app(tmp_path, bot=bot) as first:
        await first.settle()
    async with telegram_app(tmp_path, bot=bot, sign_in_first=False, seed=False) as second:
        await second.settle()

    assert bot.command_calls == [tc.BOT_COMMANDS, tc.BOT_COMMANDS]
    assert bot.commands == tc.BOT_COMMANDS  # the same list, not a longer one


async def test_a_failed_menu_registration_is_one_warning_and_polling_keeps_working(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    bot = FakeBot()
    bot.fail("setMyCommands", 502)
    async with telegram_app(tmp_path, bot=bot) as t:
        await t.settle()
        failure = t.service.failure

    assert failure is None  # the bot still reads Telegram
    assert bot.commands == []  # the previous (empty) menu is untouched
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "house_helper.web.telegram" and record.levelno == logging.WARNING
    ]
    assert [line for line in warnings if "command menu was not set" in line] == [
        "The Telegram command menu was not set: category=api:502"
    ]


async def test_help_names_favorites_and_says_where_editing_is_available():
    assert "/favorites" in tc.HELP
    assert "Редактировать" in tc.HELP and "на сайте" in tc.HELP


async def test_help_documents_dialogs_new_and_unlink_and_the_mirroring_rule():
    assert "/dialogs" in tc.HELP and "/new" in tc.HELP and "/unlink" in tc.HELP
    assert "МСК" in tc.HELP  # the dialogs list's time zone is explained where commands are
    assert "не меняет" in tc.HELP and "разговор, который открывает сайт" in tc.HELP
    assert "только из выбранного разговора" in tc.HELP
