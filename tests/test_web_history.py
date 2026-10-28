"""The conversation history as a person lives with it: it is there after a reload and after
signing out and in again, it can be reopened, each conversation has a name made of its first
words, each account sees only its own, and the one-year retention is as it was.

The mechanics (paging, ordering, expiry of single messages) are test_web_chat_api.py; the
persistence layer's own rules are test_persistence_conversations.py. Here: the journeys the
page makes, over the real API and a real database. The assistant is a ScriptedBackend.
"""

import pytest

from house_helper.persistence.conversations import TITLE_LENGTH, conversation_title
from tests.auth_support import logout_as
from tests.chat_support import ScriptedBackend, chat_app, post_headers

pytestmark = pytest.mark.asyncio


async def conversations_of(chat, client=None):
    response = await (client or chat.client).get("/api/conversations")
    assert response.status_code == 200, response.text
    return response.json()["conversations"]


async def say(chat, conversation, text, key, client=None):
    response = await chat.send(conversation, text, key, client)
    assert response.status_code == 201, response.text
    return response.json()


async def sign_out_and_in(chat):
    out = await chat.client.post("/auth/logout", headers=post_headers(**logout_as(chat.user_id)))
    assert out.status_code == 204
    assert (await chat.client.get("/api/conversations")).status_code == 401
    await chat.sign_in_again()


# --- it persists -------------------------------------------------------------------------------------


async def test_conversations_and_their_messages_are_there_after_signing_out_and_in(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend("Борщ: варите.", "Плов: жарьте.")) as chat:
        first = await chat.create()
        await say(chat, first, "Как сварить борщ?", "k-1")
        chat.clock.advance(minutes=5)
        second = await chat.create()
        await say(chat, second, "Как приготовить плов?", "k-2")
        before = await conversations_of(chat)
        before_history = [(await chat.history(c)).json()["messages"] for c in (first, second)]

        await sign_out_and_in(chat)

        assert await conversations_of(chat) == before  # the same conversations, names and times
        assert [c["id"] for c in before] == [second, first]
        after_history = [(await chat.history(c)).json()["messages"] for c in (first, second)]
        assert after_history == before_history
        assert [m["text"] for m in after_history[0]] == ["Как сварить борщ?", "Борщ: варите."]


async def test_a_conversation_can_be_reopened_and_continued_after_signing_in_again(tmp_path):
    backend = ScriptedBackend("Сначала бульон.", "Теперь овощи.")
    async with chat_app(tmp_path, backend) as chat:
        conversation = await chat.create()
        await say(chat, conversation, "Как сварить борщ?", "k-1")
        await sign_out_and_in(chat)

        resumed = await say(chat, conversation, "А дальше?", "k-2")

        assert resumed["conversation_id"] == conversation
        history = (await chat.history(conversation)).json()["messages"]
        assert [m["text"] for m in history] == [
            "Как сварить борщ?", "Сначала бульон.", "А дальше?", "Теперь овощи.",
        ]
        # the assistant saw the earlier exchange: the history is its memory
        sent_to_the_model = [m["content"] for m in backend.requests[-1][0]]
        assert sent_to_the_model[-3:] == ["Как сварить борщ?", "Сначала бульон.", "А дальше?"]
        assert len(await conversations_of(chat)) == 1


async def test_a_reloaded_page_reads_exactly_what_it_read_before(tmp_path):
    # (a reload is only the page asking again: nothing is kept in the browser but the session)
    async with chat_app(tmp_path, ScriptedBackend("Ответ.")) as chat:
        conversation = await chat.create()
        await say(chat, conversation, "Вопрос про суп", "k-1")

        first = (await conversations_of(chat), (await chat.history(conversation)).json())
        second = (await conversations_of(chat), (await chat.history(conversation)).json())

        assert first == second
        assert [m["text"] for m in first[1]["messages"]] == ["Вопрос про суп", "Ответ."]


# --- each account sees only its own ---------------------------------------------------------------------------


async def test_each_account_sees_only_its_own_conversations_titles_and_messages(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend("Мой ответ", "Их ответ")) as chat:
        other = await chat.second_user()
        mine = await chat.create()
        theirs = await chat.create(other)
        await say(chat, mine, "Мой секретный вопрос про борщ", "k-1")
        await say(chat, theirs, "Их секретный вопрос про плов", "k-2", other)

        my_list = await conversations_of(chat)
        their_list = await conversations_of(chat, other)

        assert [c["id"] for c in my_list] == [mine] and [c["id"] for c in their_list] == [theirs]
        assert my_list[0]["title"] == "Мой секретный вопрос про борщ"
        assert their_list[0]["title"] == "Их секретный вопрос про плов"
        assert "плов" not in str(my_list) and "борщ" not in str(their_list)
        assert (await chat.history(theirs)).status_code == 404  # not even told it exists
        assert (await chat.history(mine, other)).status_code == 404
        await sign_out_and_in(chat)  # and signing in again changes nothing about whose they are
        assert [c["id"] for c in await conversations_of(chat)] == [mine]
        assert [c["id"] for c in await conversations_of(chat, other)] == [theirs]


# --- the names ----------------------------------------------------------------------------------------------------


async def test_a_conversation_is_named_by_its_first_message_and_a_new_one_has_no_name(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend("Ответ", "Ещё ответ")) as chat:
        conversation = await chat.create()
        assert (await conversations_of(chat))[0]["title"] is None  # nothing said yet

        await say(chat, conversation, "Что приготовить из курицы и риса?", "k-1")
        await say(chat, conversation, "А если без духовки?", "k-2")

        (listed,) = await conversations_of(chat)
        assert listed["title"] == "Что приготовить из курицы и риса?"  # the first, not the latest
        assert chat.conversation(conversation).title is None  # derived on reading, never stored


async def test_a_long_or_untidy_first_message_makes_a_short_one_line_name(tmp_path):
    long_question = "Подскажите, пожалуйста, как приготовить домашнюю лапшу для куриного супа без яиц и муки высшего сорта"
    untidy = "  как   сварить\n\nгречку\t рассыпчатой  "
    async with chat_app(tmp_path, ScriptedBackend(default="Ответ")) as chat:
        a, b = await chat.create(), await chat.create()
        await say(chat, a, long_question, "k-1")
        await say(chat, b, untidy, "k-2")

        titles = {c["id"]: c["title"] for c in await conversations_of(chat)}

        assert titles[a] == conversation_title(long_question)
        assert titles[a].endswith("…") and len(titles[a]) == TITLE_LENGTH + 1
        assert long_question.startswith(titles[a][:-1])
        assert titles[b] == "как сварить гречку рассыпчатой"
        assert "\n" not in titles[a] + titles[b]


async def test_a_failed_first_message_still_names_the_conversation(tmp_path):
    from house_helper.core.backend import BackendError

    async with chat_app(tmp_path, ScriptedBackend(BackendError("provider_error"))) as chat:
        conversation = await chat.create()
        failed = await chat.send(conversation, "Как запечь рыбу?", "k-1")
        assert failed.status_code >= 500

        assert (await conversations_of(chat))[0]["title"] == "Как запечь рыбу?"


# --- retention is as it was --------------------------------------------------------------------------------------


async def test_the_name_lives_only_as_long_as_the_message_it_came_from(tmp_path):
    async with chat_app(tmp_path, ScriptedBackend("Ответ 1", "Ответ 2")) as chat:
        conversation = await chat.create()
        await say(chat, conversation, "Старый вопрос", "k-1")
        chat.clock.advance(days=300)
        await chat.sign_in_again()
        await say(chat, conversation, "Новый вопрос", "k-2")
        assert (await conversations_of(chat))[0]["title"] == "Старый вопрос"

        chat.clock.advance(days=70)  # the first exchange is 370 days old, the second 70
        await chat.sign_in_again()

        assert (await conversations_of(chat))[0]["title"] == "Новый вопрос"  # the next unexpired one
        chat.clock.advance(days=300)  # and now the second is gone as well
        await chat.sign_in_again()
        (listed,) = await conversations_of(chat)
        assert listed["title"] is None  # the page falls back to the date; no old words survive
        assert (await chat.history(conversation)).json()["messages"] == []


async def test_a_name_is_not_stored_anywhere_that_the_one_year_rule_does_not_reach(tmp_path):
    from sqlalchemy import text

    async with chat_app(tmp_path, ScriptedBackend("Ответ")) as chat:
        conversation = await chat.create()
        await say(chat, conversation, "Очень личный вопрос про бабушкин рецепт", "k-1")
        await conversations_of(chat)

        with chat.base.factory() as db:
            dump = " ".join(
                str(value)
                for table in ("conversations", "users", "favorite_recipes")
                for row in db.execute(text(f"SELECT * FROM {table}"))
                for value in row
            )

        assert "личный вопрос" not in dump  # only in messages and turn requests, which expire
