"""Saving one recipe of a Telegram answer into the favorites shared with the website.

The button is made from the structured recipes the turn runner already stores beside the
rendered answer (KH-F06); a click names only (message id, position) and is resolved through
the sender's current Telegram link and that user's own conversation. The tests run the real
application, the real database and the real callback path; the Bot API is a FakeBot and the
assistant a scripted backend that returns what the real one returns after parsing the
protocol (rendered text plus validated recipes).

What is pinned here:

  * one save button under a single recipe, one per recipe when an answer carries several;
  * no control at all under an ordinary answer;
  * the saved favorite keeps the recipe's title, servings, ingredients and steps, and is
    visible through the website's own favorites API;
  * repeated clicks are one favorite, and two genuinely different recipes with the same
    title are two;
  * the sender's identity comes from the update and the link, never from the callback data:
    a click from another account on somebody else's button saves nothing;
  * invalid, foreign, stale and expired callbacks are answered in Russian and save nothing;
  * a button still works after a restart (it depends on the database, not on memory);
  * a full favorites list and an unexpected failure are answered without crashing;
  * saving makes no provider call.
"""

import json

import pytest

from house_helper.core.backend import BackendReply
from house_helper.core.recipes import RECIPE_END, RECIPE_START, parse_reply, render_parsed_reply
from house_helper.persistence import favorites
from house_helper.persistence.models import FavoriteRecipe
from house_helper.web import telegram_chat as tc
from tests.chat_support import ScriptedBackend, post_headers
from tests.telegram_support import (
    MOTHER,
    SISTER,
    STRANGER,
    callback_query,
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
SOUP_A = {
    "title": "Суп",
    "servings": "2 порции",
    "ingredients": [{"name": "морковь", "amount": "2 шт."}],
    "steps": ["Сварите."],
}
SOUP_B = {
    "title": "Суп",
    "servings": "3 порции",
    "ingredients": [{"name": "тыква", "amount": "500 г"}],
    "steps": ["Запеките.", "Пюрируйте."],
}


def block(recipe) -> str:
    return f"{RECIPE_START}\n{json.dumps(recipe, ensure_ascii=False)}\n{RECIPE_END}"


RAW_ONE = "Вот рецепт.\n" + block(BORSH) + "\nПриятного аппетита."
RAW_TWO = "Вот два варианта.\n" + block(BORSH) + "\nИ еще один.\n" + block(UKHA)
RAW_SAME_TITLE = "Два разных супа.\n" + block(SOUP_A) + "\nИ еще.\n" + block(SOUP_B)


def reply(raw: str) -> BackendReply:
    """Exactly what the real backend returns: rendered text and validated recipes."""
    parsed = parse_reply(raw)
    return BackendReply(text=render_parsed_reply(parsed), recipes=parsed.recipes)


def backend_with(*raw: str) -> ScriptedBackend:
    return ScriptedBackend(*(reply(item) for item in raw))


def buttons(t) -> list[dict]:
    return save_buttons(t.bot.sent[-1])


def favorites_rows(t, user_id=None) -> list[FavoriteRecipe]:
    where = [] if user_id is None else [FavoriteRecipe.user_id == user_id]
    return t.chat.rows(FavoriteRecipe, *where, order=FavoriteRecipe.source_recipe_index)


def last_answer(t) -> str:
    return t.bot.answered[-1]["text"]


# --- the button under an answer -------------------------------------------------------------


async def test_a_single_recipe_has_one_save_button_and_saving_keeps_the_whole_recipe(tmp_path):
    backend = backend_with(RAW_ONE)
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Как сварить борщ?")

        [button] = buttons(t)
        assert button["text"] == tc.SAVE_BUTTON
        assert len(button["callback_data"].encode("ascii")) <= 64
        [answer] = [m for m in t.all_messages() if m.role == "assistant"]
        assert button["callback_data"] == f"fav:{answer.id}:0"

        # The answer itself is plain text: the protocol never reaches the person.
        assert RECIPE_START not in " ".join(t.bot.texts(MOTHER))
        assert RECIPE_END not in " ".join(t.bot.texts(MOTHER))

        await t.click(MOTHER, button["callback_data"])

        [kept] = favorites_rows(t)
        assert (kept.user_id, kept.kind, kept.source_message_id, kept.source_recipe_index) == (
            t.user_id,
            "structured",
            answer.id,
            0,
        )
        assert kept.title == "Борщ"
        assert kept.servings == "4 порции"
        assert kept.ingredients == [
            {"name": "свекла", "amount": "2 шт."},
            {"name": "капуста", "amount": "300 г"},
        ]
        assert kept.steps == ["Сварите бульон.", "Добавьте овощи."]
        assert kept.content == (
            "Борщ\n4 порции\n\nИнгредиенты:\n- свекла — 2 шт.\n- капуста — 300 г\n\n"
            "Приготовление:\n1. Сварите бульон.\n2. Добавьте овощи."
        )
        assert last_answer(t) == tc.SAVED
        assert backend.calls == 1  # saving never asks the assistant

        # The same favorite is visible through the website's own API, for its owner.
        listed = await t.chat.client.get("/api/favorites")
        assert listed.status_code == 200, listed.text
        [item] = listed.json()["favorites"]
        detail = await t.chat.client.get(f"/api/favorites/{item['id']}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["title"] == "Борщ" and body["kind"] == "structured"
        assert body["servings"] == "4 порции"
        assert body["ingredients"] == [
            {"name": "свекла", "amount": "2 шт."},
            {"name": "капуста", "amount": "300 г"},
        ]
        assert body["steps"] == ["Сварите бульон.", "Добавьте овощи."]


async def test_several_recipes_are_saved_independently(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_TWO)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Что приготовить?")

        first, second = buttons(t)
        assert [first["text"], second["text"]] == [
            "Сохранить «Борщ»",
            "Сохранить «Уха»",
        ]

        await t.click(MOTHER, second["callback_data"])
        [kept] = favorites_rows(t)
        assert (kept.title, kept.source_recipe_index, kept.servings) == ("Уха", 1, None)
        assert kept.ingredients == [{"name": "рыба", "amount": "1 кг"}]

        await t.click(MOTHER, first["callback_data"])
        kept = favorites_rows(t)
        assert [(row.title, row.source_recipe_index) for row in kept] == [
            ("Борщ", 0),
            ("Уха", 1),
        ]


async def test_a_long_title_keeps_the_button_inside_telegrams_limits(tmp_path):
    long_title = {**BORSH, "title": "Очень длинное название блюда " * 6}
    raw = "Два варианта.\n" + block(long_title) + "\nИ еще.\n" + block(UKHA)
    async with telegram_app(tmp_path, backend_with(raw)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Что приготовить?")

        first, second = buttons(t)
        assert len(first["text"].encode("utf-8")) <= tc.SAVE_BUTTON_BYTES
        assert first["text"].endswith(tc.BUTTON_ELLIPSIS)
        assert len(first["callback_data"].encode("ascii")) <= 64
        assert second["text"] == "Сохранить «Уха»"


async def test_an_ordinary_answer_has_no_save_control(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Вот что можно приготовить.")) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Что приготовить?")

        assert "reply_markup" not in t.bot.sent[-1]
        assert "Сохранить" not in " ".join(t.bot.texts(MOTHER))


async def test_two_different_recipes_with_the_same_title_are_two_favorites(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_SAME_TITLE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Суп?")

        first, second = buttons(t)
        await t.click(MOTHER, first["callback_data"])
        await t.click(MOTHER, second["callback_data"])

        kept = favorites_rows(t)
        assert [row.title for row in kept] == ["Суп", "Суп"]
        assert [row.servings for row in kept] == ["2 порции", "3 порции"]
        assert len({row.id for row in kept}) == 2


# --- repeated clicks ------------------------------------------------------------------------


async def test_a_repeated_click_is_answered_as_already_saved_and_writes_nothing(tmp_path):
    backend = backend_with(RAW_ONE)
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Как сварить борщ?")
        [button] = buttons(t)

        await t.click(MOTHER, button["callback_data"])
        await t.click(MOTHER, button["callback_data"])

        [kept] = favorites_rows(t)
        assert kept.title == "Борщ"
        assert [entry["text"] for entry in t.bot.answered] == [tc.SAVED, tc.ALREADY_SAVED]
        assert backend.calls == 1


# --- identity and isolation -----------------------------------------------------------------


async def test_a_recipe_already_saved_on_the_website_is_answered_as_already_saved(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = buttons(t)
        [conversation] = t.conversations()

        saved = await t.chat.client.post(
            "/api/favorites",
            json={
                "conversation_id": str(conversation.id),
                "message_id": int(button["callback_data"].split(":")[1]),
                "recipe_index": 0,
            },
            headers=post_headers(),
        )
        assert saved.status_code == 201, saved.text

        await t.click(MOTHER, button["callback_data"])

        [kept] = favorites_rows(t)
        assert kept.title == "Борщ"
        assert last_answer(t) == tc.ALREADY_SAVED


async def test_another_account_cannot_save_somebody_elses_recipe(tmp_path):
    backend = backend_with(RAW_ONE, RAW_ONE)
    async with telegram_app(tmp_path, backend) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await t.pair(SISTER, client=other)

        await t.deliver(MOTHER, "Борщ для мамы")
        [mother_button] = buttons(t)
        await t.deliver(SISTER, "Борщ для сестры")
        [sister_button] = buttons(t)

        # The sister sends the mother's callback data from her own account.
        await t.click(SISTER, mother_button["callback_data"])
        assert favorites_rows(t, user_id=t.user_id) == []
        assert favorites_rows(t, user_id=t.chat.extra_users["7777777777777777777"]) == []
        assert last_answer(t) == tc.RECIPE_UNAVAILABLE
        assert (await t.chat.client.get("/api/favorites")).json()["favorites"] == []
        assert (await other.get("/api/favorites")).json()["favorites"] == []

        # Each owner can save their own; the other's rows stay untouched.
        await t.click(MOTHER, mother_button["callback_data"])
        await t.click(SISTER, sister_button["callback_data"])
        assert [row.title for row in favorites_rows(t, user_id=t.user_id)] == ["Борщ"]
        assert [
            row.title
            for row in favorites_rows(t, user_id=t.chat.extra_users["7777777777777777777"])
        ] == ["Борщ"]
        assert backend.calls == 2


async def test_a_stranger_cannot_use_a_save_button(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = buttons(t)

        await t.click(STRANGER, button["callback_data"])

        assert favorites_rows(t) == []
        assert last_answer(t) == tc.CALLBACK_NOT_LINKED


# --- invalid, stale and expired callbacks ---------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        None,
        123,
        "",
        "fav",
        "fav:abc:0",
        "fav:0:0",
        "fav:-1:0",
        "fav:1:9",
        "fav:1:0:extra",
        "other:1:0",
        "fav:" + "9" * 20 + ":0",
        "fav:1:000",
    ],
)
async def test_a_malformed_or_foreign_callback_saves_nothing_and_is_acknowledged(tmp_path, data):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        before = len(t.bot.answered)

        await t.click(MOTHER, data)

        assert favorites_rows(t) == []
        assert len(t.bot.answered) == before + 1
        assert t.bot.answered[-1]["text"] == tc.RECIPE_UNAVAILABLE


async def test_a_malformed_callback_update_is_ignored_without_crashing(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        answered = len(t.bot.answered)

        await t.deliver_update({"update_id": t.next_id(), "callback_query": "not an object"})
        await t.deliver_update(
            {
                "update_id": t.next_id(),
                "callback_query": {"id": 5, "data": "fav:1:0", "from": {"id": MOTHER}},
            }
        )
        await t.deliver_update(
            {"update_id": t.next_id(), "callback_query": {"id": "x" * 300, "data": "fav:1:0"}}
        )

        assert favorites_rows(t) == []
        assert len(t.bot.answered) == answered  # nothing with a usable id to answer


async def test_a_callback_without_its_message_or_from_a_group_saves_nothing(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = buttons(t)

        await t.deliver_update(
            callback_query(t.next_id(), MOTHER, button["callback_data"], with_message=False)
        )
        assert last_answer(t) == tc.RECIPE_UNAVAILABLE

        await t.deliver_update(
            callback_query(
                t.next_id(),
                MOTHER,
                button["callback_data"],
                chat_type="supergroup",
                chat_id=-100500,
            )
        )
        assert last_answer(t) == tc.RECIPE_UNAVAILABLE
        assert favorites_rows(t) == []


async def test_a_stale_or_expired_recipe_is_answered_as_unavailable(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = buttons(t)

        await t.click(MOTHER, "fav:999999:0")
        assert last_answer(t) == tc.RECIPE_UNAVAILABLE

        t.chat.clock.advance(days=366)
        await t.click(MOTHER, button["callback_data"])
        assert last_answer(t) == tc.RECIPE_UNAVAILABLE
        assert favorites_rows(t) == []


async def test_a_redelivered_click_after_an_ack_was_lost_is_harmless(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = buttons(t)
        update = callback_query(t.next_id(), MOTHER, button["callback_data"])

        await t.deliver_update(update)
        await t.deliver_update(update)  # Telegram repeats it because the ack was lost

        [kept] = favorites_rows(t)
        assert kept.title == "Борщ"
        assert [entry["text"] for entry in t.bot.answered] == [tc.SAVED, tc.ALREADY_SAVED]


# --- after a restart ------------------------------------------------------------------------


async def test_a_button_still_works_after_the_bot_restarts(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as first:
        await first.pair(MOTHER)
        await first.deliver(MOTHER, "Борщ?")
        [button] = buttons(first)
        data = button["callback_data"]

    # A new process: nothing in memory, the same database and the same Telegram message.
    async with telegram_app(
        tmp_path, ScriptedBackend(), sign_in_first=False, seed=False
    ) as second:
        second.chat.clock.advance(minutes=10)
        await second.click(MOTHER, data)

        [kept] = favorites_rows(second)
        assert kept.title == "Борщ"
        assert second.bot.answered[-1]["text"] == tc.SAVED
        assert second.chat.backend.calls == 0


# --- failures and a copied website answer ---------------------------------------------------


async def test_a_full_favorites_list_is_explained_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(favorites, "FAVORITES_LIMIT", 0)
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = buttons(t)

        await t.click(MOTHER, button["callback_data"])

        assert favorites_rows(t) == []
        assert last_answer(t) == tc.FAVORITES_FULL


async def test_an_unexpected_storage_failure_is_answered_without_crashing(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("storage is gone")

    monkeypatch.setattr(favorites, "save_recipe_for_telegram", explode)
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = buttons(t)

        await t.click(MOTHER, button["callback_data"])

        assert favorites_rows(t) == []
        assert last_answer(t) == tc.SAVE_FAILED


async def test_a_failed_acknowledgement_does_not_undo_the_save(tmp_path):
    async with telegram_app(tmp_path, backend_with(RAW_ONE)) as t:
        await t.pair(MOTHER)
        await t.deliver(MOTHER, "Борщ?")
        [button] = buttons(t)

        t.bot.fail("answerCallbackQuery", 403)
        await t.click(MOTHER, button["callback_data"])

        [kept] = favorites_rows(t)
        assert kept.title == "Борщ"


async def test_a_recipe_copied_from_the_website_can_be_saved_from_telegram(tmp_path):
    backend = backend_with(RAW_ONE)
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        conversation = await t.chat.create()
        sent = await t.chat.send(conversation, "Как сварить борщ?", "web-1")
        assert sent.status_code == 201, sent.text
        await t.settle()

        [button] = buttons(t)
        assert button["text"] == tc.SAVE_BUTTON
        await t.click(MOTHER, button["callback_data"])

        [kept] = favorites_rows(t)
        assert kept.title == "Борщ"
        assert backend.calls == 1
