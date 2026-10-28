"""Photographs sent to the bot: what is fetched, checked and kept, what the assistant is shown, what
the website shows and to whom, and when it is all forgotten.

The bot's other behaviour is tests/test_telegram_chat.py; the storage under it is
tests/test_persistence_attachments.py.
"""

import asyncio
import json
import logging
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.admin import retention
from house_helper.persistence import attachments as att
from house_helper.persistence.identifiers import MAX_ATTACHMENT_BYTES
from house_helper.persistence.models import MessageAttachment
from house_helper.admin.target import open_target
from house_helper.persistence.database import DATABASE_URL_ENV
from house_helper.web import telegram_chat as tc
from tests.chat_support import ScriptedBackend, error_of, log_text, post_headers
from tests.photo_support import NOT_PICTURES, jpeg, png
from tests.telegram_support import (
    BOT_TOKEN,
    MOTHER,
    SISTER,
    STRANGER,
    TelegramHarness,
    photo_message,
    private_message,
    telegram_app,
)
from tests.web_support import sqlite_url

pytestmark = pytest.mark.asyncio


def photos_of(t: TelegramHarness):
    return t.chat.rows(MessageAttachment)


def requested(t: TelegramHarness, method: str):
    return [json.loads(r.content) for r in t.bot.requests if r.url.path.endswith("/" + method)]


async def send_photo(t, account=MOTHER, data=None, *, file_id="photo-1", caption=None, tick=True, **extra):
    t.bot.files[file_id] = ("photos/" + file_id + ".jpg", data if data is not None else jpeg(4000, file_id))
    if tick:
        t.chat.clock.advance(seconds=1)
    update = photo_message(t.next_id(), account, file_id, caption=caption, **extra)
    await t.deliver_update(update)
    return update


# --- the happy path ----------------------------------------------------------------------------------


async def test_a_photo_with_a_caption_is_shown_to_the_assistant_stored_and_answered(tmp_path):
    data = jpeg(6000, "fridge")
    async with telegram_app(tmp_path, ScriptedBackend("Из этого выйдет омлет.")) as t:
        await t.pair(MOTHER)
        await send_photo(t, data=data, caption="Что приготовить из этого?")

    assert t.bot.texts(MOTHER)[-1] == "Из этого выйдет омлет."
    [stored] = photos_of(t)
    [conversation] = t.conversations()
    user_message = [m for m in t.chat.messages(str(conversation.id)) if m.role == "user"][0]
    assert stored.message_id == user_message.id and stored.user_id == t.user_id
    assert (bytes(stored.data), stored.content_type, stored.size_bytes) == (data, "image/jpeg", 6000)
    assert user_message.content == "Что приготовить из этого?"
    assert stored.expires_at == user_message.expires_at
    [shown] = [m for m, _, _ in t.chat.backend.requests]
    assert shown[-1]["content"] == [
        {"type": "text", "text": "Что приготовить из этого?"},
        {"type": "image", "content_type": "image/jpeg", "data": data},
    ]


async def test_a_photo_with_no_caption_is_a_message_too(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Вижу продукты.")) as t:
        await t.pair(MOTHER)
        await send_photo(t)

    [conversation] = t.conversations()
    assert t.stored_texts(conversation.id)[0] == ("user", att.PHOTO_PLACEHOLDER)
    content = t.chat.backend.requests[0][0][-1]["content"]
    assert content[0] == {"type": "text", "text": "Человек прислал фото без подписи."}


async def test_the_largest_size_telegram_offers_is_the_one_fetched(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await send_photo(t, file_id="big-one")

    assert [call["file_id"] for call in requested(t, "getFile")] == ["big-one"]


async def test_the_photo_is_fetched_through_the_bots_own_endpoint_and_nothing_of_it_is_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        await send_photo(t, caption="Секретная подпись")

    downloads = [r for r in t.bot.requests if r.url.path.startswith("/file/")]
    assert len(downloads) == 1 and downloads[0].url.host == "api.telegram.org"
    text = log_text(caplog)
    for secret in (BOT_TOKEN, BOT_TOKEN.split(":")[1], "photos/", "photo-1", "Секретная подпись", str(MOTHER)):
        assert secret not in text, secret


async def test_a_follow_up_question_is_asked_with_the_photo_still_in_front_of_the_assistant(tmp_path):
    data = png(3000, "label")
    async with telegram_app(tmp_path, ScriptedBackend("Это тунец.", "Откройте и слейте масло.")) as t:
        await t.pair(MOTHER)
        await send_photo(t, data=data, caption="Что в банке?")
        await t.deliver(MOTHER, "А как открыть?")

    followup = t.chat.backend.requests[1][0]
    assert [type(m["content"]).__name__ for m in followup] == ["list", "str", "str"]
    assert followup[0]["content"][1]["data"] == data


async def test_the_website_lists_the_photo_with_its_message_and_serves_it_to_its_owner_only(tmp_path):
    data = jpeg(5000, "shown")
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await send_photo(t, data=data, caption="Смотрите")
        [conversation] = t.conversations()

        history = (await t.chat.history(str(conversation.id))).json()["messages"]
        [attachment] = history[0]["attachments"]
        mine = await t.chat.client.get(attachment["url"])
        theirs = await other.get(attachment["url"])
        stranger = await t.chat.base.new_client().get(attachment["url"])

    assert history[1].get("attachments") is None
    assert set(attachment) == {"id", "content_type", "size", "url"}
    assert attachment["url"] == f"/api/attachments/{attachment['id']}" and attachment["size"] == 5000
    assert data not in json.dumps(history).encode()
    assert mine.status_code == 200 and mine.content == data
    assert mine.headers["content-type"] == "image/jpeg"
    assert mine.headers["x-content-type-options"] == "nosniff"
    assert mine.headers["cache-control"] == "no-store"
    assert mine.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert mine.headers["cross-origin-resource-policy"] == "same-origin"
    assert mine.headers["content-disposition"] == "inline"
    assert theirs.status_code == 404 and error_of(theirs)["detail"] == "attachment_not_found"
    assert stranger.status_code == 401


@pytest.mark.parametrize("path", ["/api/attachments/not-a-uuid", "/api/attachments/" + "0" * 36, "/api/attachments/00000000-0000-4000-8000-000000000000", "/api/attachments/%2e%2e"])
async def test_an_unknown_or_malformed_photo_id_is_one_answer(tmp_path, path):
    async with telegram_app(tmp_path) as t:
        response = await t.chat.client.get(path)

    assert response.status_code == 404 and error_of(response)["detail"] == "attachment_not_found"


async def test_the_photo_goes_with_the_message_after_its_year(tmp_path):
    data = jpeg(3000, "old")
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        await send_photo(t, data=data)
        [conversation] = t.conversations()
        url = (await t.chat.history(str(conversation.id))).json()["messages"][0]["attachments"][0]["url"]
        t.chat.clock.advance(days=366)
        await t.chat.sign_in_again()
        fetched = await t.chat.client.get(url)
        history = (await t.chat.history(str(conversation.id))).json()["messages"]

    assert fetched.status_code == 404 and history == []


# --- what is refused ----------------------------------------------------------------------------------------


async def test_a_strangers_photo_is_never_fetched(tmp_path):
    async with telegram_app(tmp_path) as t:
        await send_photo(t, STRANGER)

    assert t.bot.texts(STRANGER) == [tc.NOT_LINKED]
    assert requested(t, "getFile") == [] and not [r for r in t.bot.requests if r.url.path.startswith("/file/")]
    assert photos_of(t) == [] and t.chat.backend.calls == 0


@pytest.mark.parametrize("name", sorted(NOT_PICTURES))
async def test_a_file_that_is_not_a_picture_is_refused_whatever_telegram_calls_it(tmp_path, name):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await send_photo(t, data=NOT_PICTURES[name])

    assert t.bot.texts(MOTHER)[-1] == tc.PHOTO_UNSUPPORTED
    assert photos_of(t) == [] and t.all_messages() == [] and t.chat.backend.calls == 0


async def test_a_photo_over_the_limit_is_refused_before_and_while_downloading(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        # Telegram says so in the update: nothing is fetched at all
        huge = photo_message(t.next_id(), MOTHER, "huge")
        for size in huge["message"]["photo"]:
            size["file_size"] = MAX_ATTACHMENT_BYTES + 1
        await t.deliver_update(huge)
        said_beforehand = requested(t, "getFile")
        # Telegram does not say (no file_size anywhere) and the file is over the limit: the download stops
        t.bot.files["liar"] = ("photos/liar.jpg", jpeg(MAX_ATTACHMENT_BYTES + 10))
        update = photo_message(t.next_id(), MOTHER, "liar")
        for size in update["message"]["photo"]:
            size.pop("file_size")
        await t.deliver_update(update)

    assert said_beforehand == []
    assert t.bot.texts(MOTHER)[-2:] == [tc.PHOTO_TOO_BIG, tc.PHOTO_TOO_BIG]
    assert photos_of(t) == [] and t.chat.backend.calls == 0


async def test_a_photo_telegram_cannot_hand_over_is_said_to_have_failed_and_stores_nothing(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await t.deliver_update(photo_message(t.next_id(), MOTHER, "never-uploaded"))  # getFile answers 400
        t.bot.files["x"] = ("photos/x.jpg", jpeg())
        t.bot.fail("getFile", 500)
        await t.deliver_update(photo_message(t.next_id(), MOTHER, "x"))

    assert t.bot.texts(MOTHER)[-2:] == [tc.PHOTO_FAILED, tc.PHOTO_FAILED]
    assert photos_of(t) == [] and t.all_messages() == [] and t.chat.backend.calls == 0


async def test_a_caption_that_cannot_be_stored_is_refused_before_anything_is_fetched(tmp_path):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        await send_photo(t, caption="я" * 2001)
        await send_photo(t, caption="плохой\x00символ", file_id="photo-2")

    assert "слишком длинное" in t.bot.texts(MOTHER)[-2] and "недопустимые" in t.bot.texts(MOTHER)[-1]
    assert requested(t, "getFile") == [] and photos_of(t) == []


@pytest.mark.parametrize(
    "sizes",
    [
        [],
        [{}],
        ["x"],
        [{"file_id": 5, "width": 10, "height": 10}],
        [{"file_id": "a", "width": 0, "height": 10}],
        [{"file_id": "a", "width": "10", "height": 10}],
        [{"file_id": "a", "width": 10, "height": 10, "file_size": "big"}],
        [{"file_id": "", "width": 10, "height": 10}],
    ],
)
async def test_a_list_of_sizes_that_is_not_one_is_answered_politely(tmp_path, sizes):
    async with telegram_app(tmp_path) as t:
        await t.pair(MOTHER)
        update = private_message(t.next_id(), MOTHER, photo=sizes)
        await t.deliver_update(update)

    assert t.chat.backend.calls == 0 and photos_of(t) == []
    assert t.bot.texts(MOTHER)[-1] in (tc.PHOTO_TOO_BIG, tc.UNSUPPORTED)


async def test_a_refused_turn_does_not_leave_the_photo_waiting(tmp_path):
    backend = ScriptedBackend("Первый")
    async with telegram_app(tmp_path, backend) as t:
        await t.pair(MOTHER)
        conversation_id = await t.chat.create()
        arrived, release = backend.hold()
        web_turn = asyncio.ensure_future(t.chat.send(conversation_id, "С сайта", "web-1"))
        await asyncio.wait_for(arrived.wait(), 10)

        await send_photo(t)  # the conversation is busy: the turn is refused after the photo was stored
        release.set()
        await web_turn
        await t.settle()

    assert tc.STILL_BUSY in t.bot.texts(MOTHER)
    assert photos_of(t) == []


# --- the same update twice ----------------------------------------------------------------------------------------------


async def test_a_redelivered_photo_is_not_fetched_again_and_runs_once(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Единственный ответ")) as t:
        await t.pair(MOTHER)
        update = await send_photo(t, caption="Раз")
        await t.deliver_update(update)

    assert len(requested(t, "getFile")) == 1
    assert t.chat.backend.calls == 1 and len(photos_of(t)) == 1
    assert t.bot.texts(MOTHER)[-2:] == ["Единственный ответ"] * 2


async def test_an_album_is_answered_one_picture_at_a_time_and_each_keeps_its_own_photo(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Первое фото", "Второе фото")) as t:
        await t.pair(MOTHER)
        await send_photo(t, file_id="a-1", data=jpeg(2000, "1"), media_group_id="album-1")
        await send_photo(t, file_id="a-2", data=jpeg(2000, "2"), media_group_id="album-1")

    assert t.bot.texts(MOTHER)[-2:] == ["Первое фото", "Второе фото"]
    assert sorted(bytes(p.data) for p in photos_of(t)) == sorted([jpeg(2000, "1"), jpeg(2000, "2")])
    assert len({p.message_id for p in photos_of(t)}) == 2


# --- two people ---------------------------------------------------------------------------------------------------------------


async def test_two_people_keep_their_photos_apart(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Маме", "Сестре")) as t:
        other = await t.chat.second_user()
        await t.pair(MOTHER)
        await t.pair(SISTER, client=other)
        await send_photo(t, MOTHER, data=jpeg(2000, "mother"), file_id="m-1")
        await send_photo(t, SISTER, data=png(2000, "sister"), file_id="s-1")

        sister_user = t.chat.extra_users["7777777777777777777"]
        mothers = [p for p in photos_of(t) if p.user_id == t.user_id]
        sisters = [p for p in photos_of(t) if p.user_id == sister_user]
        crossed = [
            await t.chat.client.get(f"/api/attachments/{sisters[0].id}"),
            await other.get(f"/api/attachments/{mothers[0].id}"),
        ]
        own = [
            await t.chat.client.get(f"/api/attachments/{mothers[0].id}"),
            await other.get(f"/api/attachments/{sisters[0].id}"),
        ]

    assert (len(mothers), len(sisters)) == (1, 1)
    assert [c.status_code for c in crossed] == [404, 404]
    assert [c.status_code for c in own] == [200, 200]
    for messages, _, _ in t.chat.backend.requests:
        images = [p for m in messages if isinstance(m["content"], list) for p in m["content"] if p["type"] == "image"]
        assert len(images) == 1  # each assistant call was shown its own person's photo and no other


# --- forgetting ---------------------------------------------------------------------------------------------------------------------


async def test_the_expiry_job_removes_a_photo_with_its_message_and_an_unsent_upload_after_its_hour(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        await send_photo(t, data=jpeg(2000, "sent"))
        with t.chat.base.factory() as db, db.begin():
            unsent = att.store(db, t.user_id, jpeg(2000, "unsent"), now=t.chat.clock.current)
    engine = open_target({DATABASE_URL_ENV: sqlite_url(tmp_path / "auth.db")})
    try:
        soon = t.chat.clock.current + att.UPLOAD_LIFETIME
        early = retention.purge_expired(engine, now=soon, pause=lambda: None)
        kept_after_the_hour = {p.id for p in photos_of(t)}
        dry = retention.purge_expired(engine, now=t.chat.clock.current + timedelta(days=366), dry_run=True, pause=lambda: None)
        late = retention.purge_expired(engine, now=t.chat.clock.current + timedelta(days=366), pause=lambda: None)
    finally:
        engine.dispose()

    assert early.deleted["message_attachments"] == 1 and unsent.id not in kept_after_the_hour
    assert len(kept_after_the_hour) == 1  # the photo of the message is still in its year
    assert dry.deleted["message_attachments"] == 1
    assert late.deleted["message_attachments"] == 1 and late.deleted["messages"] == 2
    assert photos_of(t) == []


async def test_unlinking_telegram_keeps_the_history_and_its_photos(tmp_path):
    async with telegram_app(tmp_path, ScriptedBackend("Ответ")) as t:
        await t.pair(MOTHER)
        await send_photo(t)
        await t.deliver(MOTHER, "/unlink")

    assert len(photos_of(t)) == 1 and len(t.all_messages()) == 2
