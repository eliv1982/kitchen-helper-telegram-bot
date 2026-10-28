"""Favorite recipes: what is kept, who may keep and read it, and that it outlives the history.

The functions under test are persistence.favorites. Every test runs twice
(tests/persistence_support.py): on a schema created from the models and on one built by
the migrations.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from house_helper.persistence import conversations as cv
from house_helper.persistence import favorites as fav
from house_helper.persistence.base import RETENTION
from house_helper.persistence.models import (
    Conversation,
    FavoriteRecipe,
    Message,
    MessageRole,
    User,
)
from tests.persistence_support import (  # noqa: F401  (fixtures)
    engine,
    factory,
    new_conversation,
    new_message,
    new_user,
    rejected,
    session,
    utc,
)

NOW = utc(2026, 10, 27, 12)

RECIPE = (
    "Борщ на говяжьем бульоне\n\n"
    "Порции: 4.\n"
    "Ингредиенты:\n- говядина 500 г\n- свёкла 2 шт.\n\n"
    "1. Сварите бульон (примерно 1,5 часа).\n"
    "2. Добавьте овощи.  \t Подавайте со сметаной. <b>не разметка</b> & «кавычки» 🍲\n"
)


def run(factory, work):
    with factory() as db, db.begin():
        return work(db)


@pytest.fixture
def user_id(factory):
    return run(factory, lambda db: new_user(db).id)


@pytest.fixture
def other_user_id(factory):
    return run(factory, lambda db: new_user(db).id)


def conversation_with(factory, owner, *messages, at=NOW):
    """A conversation of `owner` holding (role, text) messages; returns (conversation id,
    [message ids])."""

    def make(db):
        conversation = new_conversation(db, db.get(User, owner), created_at=at, updated_at=at)
        ids = [
            new_message(db, conversation, role, text, created_at=at, expires_at=at + RETENTION).id
            for role, text in messages
        ]
        return conversation.id, ids

    return run(factory, make)


def keep(factory, owner, conversation_id, message_id, *, now=NOW):
    return run(factory, lambda db: fav.save_favorite(db, owner, conversation_id, message_id, now=now))


def rows(factory):
    with factory() as db:
        return list(db.scalars(sa.select(FavoriteRecipe)))


# --- the title -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("Борщ", "Борщ"),
        ("Борщ на говяжьем бульоне\n\nПорции: 4.", "Борщ на говяжьем бульоне"),
        ("\n\n   \nВторая строка\nтретья", "Вторая строка"),
        ("## Борщ", "Борщ"),
        ("**Борщ классический**", "Борщ классический"),
        ("Вот рецепт борща:", "Вот рецепт борща"),
        ("- Борщ", "Борщ"),
        ("Борщ    с\t\tфасолью", "Борщ с фасолью"),
        ("***\n---\nУха", "Уха"),
        ("", "Рецепт"),
        ("   \n\t\n", "Рецепт"),
        ("***\n---\n##", "Рецепт"),
        ("x" * fav.TITLE_LENGTH, "x" * fav.TITLE_LENGTH),
        ("x" * (fav.TITLE_LENGTH + 1), "x" * fav.TITLE_LENGTH + "…"),
        ("ы" * 300, "ы" * fav.TITLE_LENGTH + "…"),
        ("слово " * 40, ("слово " * 40)[: fav.TITLE_LENGTH].rstrip() + "…"),
    ],
)
def test_the_title_is_the_first_line_without_markup(text, title):
    assert fav.favorite_title(text) == title


def test_a_title_always_fits_the_column():
    assert fav.TITLE_LENGTH + 1 <= 200
    assert len(fav.favorite_title("я" * 5000)) == fav.TITLE_LENGTH + 1


# --- keeping an answer ---------------------------------------------------------------------------


def test_the_answer_is_kept_exactly_as_it_was_stored(factory, user_id):
    conversation_id, (question, answer) = conversation_with(
        factory, user_id, ("user", "Как сварить борщ?"), ("assistant", RECIPE)
    )

    saved = keep(factory, user_id, conversation_id, answer)

    assert saved.created is True
    assert (saved.favorite.title, saved.favorite.source_message_id, saved.favorite.created_at) == (
        "Борщ на говяжьем бульоне",
        answer,
        NOW,
    )
    (row,) = rows(factory)
    assert row.content == RECIPE  # not trimmed, not rewrapped, not parsed
    assert (row.user_id, row.title, row.source_message_id) == (user_id, "Борщ на говяжьем бульоне", answer)
    detail = run(factory, lambda db: fav.get_favorite(db, user_id, row.id))
    assert detail.text == RECIPE and detail.id == saved.favorite.id


def test_keeping_the_same_answer_again_is_the_same_favorite(factory, user_id):
    conversation_id, (answer,) = conversation_with(factory, user_id, ("assistant", RECIPE))

    first = keep(factory, user_id, conversation_id, answer)
    second = keep(factory, user_id, conversation_id, answer, now=NOW + timedelta(hours=1))

    assert (first.created, second.created) == (True, False)
    assert second.favorite == first.favorite  # the first save's time, not the second's
    assert len(rows(factory)) == 1


def test_a_removed_favorite_can_be_kept_again(factory, user_id):
    conversation_id, (answer,) = conversation_with(factory, user_id, ("assistant", RECIPE))
    first = keep(factory, user_id, conversation_id, answer)
    run(factory, lambda db: fav.remove_favorite(db, user_id, first.favorite.id))

    again = keep(factory, user_id, conversation_id, answer, now=NOW + timedelta(days=1))

    assert again.created is True and again.favorite.id != first.favorite.id
    assert len(rows(factory)) == 1


@pytest.mark.parametrize("role", [MessageRole.USER.value, MessageRole.SYSTEM_NOTE.value])
def test_only_an_answer_of_the_assistant_can_be_kept(factory, user_id, role):
    conversation_id, (message,) = conversation_with(factory, user_id, (role, "что угодно"))

    with pytest.raises(fav.MessageNotFound):
        keep(factory, user_id, conversation_id, message)

    assert rows(factory) == []


def test_an_expired_answer_cannot_be_kept_but_a_kept_one_does_not_expire(factory, user_id):
    conversation_id, (answer,) = conversation_with(factory, user_id, ("assistant", RECIPE))
    keep(factory, user_id, conversation_id, answer)
    later = NOW + RETENTION + timedelta(days=1)

    assert run(factory, lambda db: fav.list_favorites(db, user_id))[0].source_message_id == answer
    with pytest.raises(fav.MessageNotFound):
        keep(factory, user_id, conversation_id, answer, now=later)  # even the same one: it is no longer there to copy
    (listed,) = run(factory, lambda db: fav.list_favorites(db, user_id))
    assert run(factory, lambda db: fav.get_favorite(db, user_id, listed.id)).text == RECIPE


def test_an_answer_is_found_only_in_the_conversation_named(factory, user_id):
    first, (answer,) = conversation_with(factory, user_id, ("assistant", "ответ"))
    second, _ = conversation_with(factory, user_id, ("assistant", "другой"))

    with pytest.raises(fav.MessageNotFound):
        keep(factory, user_id, second, answer)
    with pytest.raises(fav.MessageNotFound):
        keep(factory, user_id, first, answer + 1000)  # no such message
    assert rows(factory) == []


def test_another_persons_conversation_is_not_found_and_nothing_is_written(factory, user_id, other_user_id):
    theirs, (answer,) = conversation_with(factory, other_user_id, ("assistant", "их ответ"))

    with pytest.raises(cv.ConversationNotFound):
        keep(factory, user_id, theirs, answer)
    with pytest.raises(cv.ConversationNotFound):
        keep(factory, user_id, uuid4(), answer)  # unknown: not told apart

    assert rows(factory) == []


def test_somebody_elses_answer_cannot_be_smuggled_through_my_conversation(factory, user_id, other_user_id):
    mine, _ = conversation_with(factory, user_id, ("assistant", "мой ответ"))
    _, (theirs,) = conversation_with(factory, other_user_id, ("assistant", "их ответ"))

    with pytest.raises(fav.MessageNotFound):
        keep(factory, user_id, mine, theirs)

    assert rows(factory) == []


def test_a_user_keeps_at_most_the_limit_and_a_repeat_is_not_refused(factory, user_id, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_LIMIT", 3)
    conversation_id, answers = conversation_with(
        factory, user_id, *[("assistant", f"Рецепт {n}") for n in range(5)]
    )
    for answer in answers[:3]:
        keep(factory, user_id, conversation_id, answer)

    with pytest.raises(fav.FavoritesFull):
        keep(factory, user_id, conversation_id, answers[3])
    again = keep(factory, user_id, conversation_id, answers[0])  # already kept: nothing new

    assert again.created is False and len(rows(factory)) == 3


def test_the_limit_is_per_user(factory, user_id, other_user_id, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_LIMIT", 1)
    mine, (my_answer,) = conversation_with(factory, user_id, ("assistant", "мой"))
    theirs, (their_answer,) = conversation_with(factory, other_user_id, ("assistant", "их"))

    keep(factory, user_id, mine, my_answer)

    assert keep(factory, other_user_id, theirs, their_answer).created is True


def test_the_limit_is_a_hundred():
    assert fav.FAVORITES_LIMIT == 100


# --- listing, reading, removing ---------------------------------------------------------------


def test_favorites_are_listed_newest_first_with_a_total_order_and_without_their_text(factory, user_id):
    conversation_id, answers = conversation_with(
        factory, user_id, *[("assistant", f"Рецепт {n}\nподробности") for n in range(4)]
    )
    saved = [
        keep(factory, user_id, conversation_id, answers[number], now=NOW + timedelta(minutes=minutes))
        for number, minutes in ((0, 2), (1, 1), (2, 3), (3, 3))  # the last two at the same instant
    ]

    listed = run(factory, lambda db: fav.list_favorites(db, user_id))

    tied = sorted([saved[2].favorite, saved[3].favorite], key=lambda item: item.id.int, reverse=True)
    assert listed == [*tied, saved[0].favorite, saved[1].favorite]
    assert [item.title for item in listed if item.id == saved[0].favorite.id] == ["Рецепт 0"]
    assert not hasattr(listed[0], "text")


def test_each_person_lists_reads_and_removes_only_their_own(factory, user_id, other_user_id):
    mine, (my_answer,) = conversation_with(factory, user_id, ("assistant", "Мой борщ"))
    theirs, (their_answer,) = conversation_with(factory, other_user_id, ("assistant", "Их плов"))
    my_favorite = keep(factory, user_id, mine, my_answer).favorite
    their_favorite = keep(factory, other_user_id, theirs, their_answer).favorite

    assert [item.id for item in run(factory, lambda db: fav.list_favorites(db, user_id))] == [my_favorite.id]
    assert [item.id for item in run(factory, lambda db: fav.list_favorites(db, other_user_id))] == [their_favorite.id]
    with pytest.raises(fav.FavoriteNotFound):
        run(factory, lambda db: fav.get_favorite(db, user_id, their_favorite.id))
    with pytest.raises(fav.FavoriteNotFound):
        run(factory, lambda db: fav.remove_favorite(db, user_id, their_favorite.id))
    with pytest.raises(fav.FavoriteNotFound):
        run(factory, lambda db: fav.get_favorite(db, user_id, uuid4()))  # unknown: not told apart
    assert {row.id for row in rows(factory)} == {my_favorite.id, their_favorite.id}  # nothing was removed


def test_removing_a_favorite_removes_only_it_and_only_once(factory, user_id):
    conversation_id, (first, second) = conversation_with(
        factory, user_id, ("assistant", "Первый"), ("assistant", "Второй")
    )
    kept_first = keep(factory, user_id, conversation_id, first).favorite
    kept_second = keep(factory, user_id, conversation_id, second).favorite

    run(factory, lambda db: fav.remove_favorite(db, user_id, kept_first.id))

    assert [row.id for row in rows(factory)] == [kept_second.id]
    with pytest.raises(fav.FavoriteNotFound):
        run(factory, lambda db: fav.remove_favorite(db, user_id, kept_first.id))
    with factory() as db:  # the conversation and its messages are untouched by removing the copy
        assert db.get(Message, first) is not None and db.get(Message, second) is not None


# --- independence from the history ------------------------------------------------------------------


def test_a_favorite_survives_the_deletion_of_its_message_and_its_conversation(factory, user_id):
    conversation_id, (question, answer) = conversation_with(
        factory, user_id, ("user", "Как сварить борщ?"), ("assistant", RECIPE)
    )
    favorite = keep(factory, user_id, conversation_id, answer).favorite

    with factory() as db, db.begin():  # what the purge does to expired history, and more
        db.execute(sa.delete(Message).where(Message.conversation_id == conversation_id))
        db.execute(sa.delete(Conversation).where(Conversation.id == conversation_id))

    with factory() as db:
        assert db.get(Conversation, conversation_id) is None
        assert db.scalar(sa.select(sa.func.count()).select_from(Message)) == 0
    detail = run(factory, lambda db: fav.get_favorite(db, user_id, favorite.id))
    assert (detail.text, detail.title, detail.source_message_id) == (RECIPE, favorite.title, answer)
    assert [item.id for item in run(factory, lambda db: fav.list_favorites(db, user_id))] == [favorite.id]


def test_deleting_the_user_deletes_their_favorites_and_nobody_elses(factory, user_id, other_user_id):
    mine, (my_answer,) = conversation_with(factory, user_id, ("assistant", "Мой"))
    theirs, (their_answer,) = conversation_with(factory, other_user_id, ("assistant", "Их"))
    keep(factory, user_id, mine, my_answer)
    their_favorite = keep(factory, other_user_id, theirs, their_answer).favorite

    with factory() as db, db.begin():
        db.execute(sa.delete(User).where(User.id == user_id))

    assert [row.id for row in rows(factory)] == [their_favorite.id]


# --- the write rule and the database's own checks -----------------------------------------------------


def statements(engine, work):
    seen = []
    listener = lambda c, cur, st, p, ctx, m: seen.append(st.lstrip().split(None, 1)[0].upper())  # noqa: E731
    sa.event.listen(engine, "before_cursor_execute", listener)
    try:
        work()
    finally:
        sa.event.remove(engine, "before_cursor_execute", listener)
    return [name for name in seen if name not in ("BEGIN", "PRAGMA", "SAVEPOINT", "RELEASE")]


def test_saving_starts_with_a_write_and_removing_is_a_write(engine, factory, user_id):
    conversation_id, (answer,) = conversation_with(factory, user_id, ("assistant", RECIPE))

    saving = statements(engine, lambda: keep(factory, user_id, conversation_id, answer))
    removing = statements(
        engine,
        lambda: run(
            factory, lambda db: fav.remove_favorite(db, user_id, rows(factory)[0].id)
        ),
    )

    assert saving[0] == "UPDATE"  # the lock on the conversation, taken before anything is read
    assert saving[-1] == "INSERT"
    assert removing[-1] == "DELETE" and "DELETE" in removing[:2]


def test_reading_writes_nothing(engine, factory, user_id):
    conversation_id, (answer,) = conversation_with(factory, user_id, ("assistant", RECIPE))
    favorite = keep(factory, user_id, conversation_id, answer).favorite

    seen = statements(
        engine,
        lambda: (
            run(factory, lambda db: fav.list_favorites(db, user_id)),
            run(factory, lambda db: fav.get_favorite(db, user_id, favorite.id)),
        ),
    )

    assert set(seen) == {"SELECT"}


def test_the_database_refuses_a_second_favorite_of_the_same_answer(session):
    user = new_user(session)
    session.add(FavoriteRecipe(user_id=user.id, title="a", content="b", source_message_id=7, created_at=NOW))
    session.flush()

    with rejected(session, "favorite_recipes.user_id, favorite_recipes.source_message_id"):
        session.add(FavoriteRecipe(user_id=user.id, title="c", content="d", source_message_id=7, created_at=NOW))

    other = new_user(session)  # the same message number under another user is another favorite
    session.add(FavoriteRecipe(user_id=other.id, title="c", content="d", source_message_id=7, created_at=NOW))
    session.flush()


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"title": ""}, "ck_favorite_recipes_title_not_empty"),
        ({"title": "я" * 201}, "ck_favorite_recipes_title_max_length"),
        ({"content": ""}, "ck_favorite_recipes_content_not_empty"),
    ],
)
def test_the_database_refuses_an_empty_title_a_long_title_and_an_empty_text(session, fields, constraint):
    user = new_user(session)
    values = {"title": "ok", "content": "ok", "source_message_id": 1, "created_at": NOW, **fields}

    with rejected(session, constraint):
        session.add(FavoriteRecipe(user_id=user.id, **values))


def test_a_favorite_needs_an_owner_that_exists(session):
    with rejected(session, "FOREIGN KEY"):
        session.add(
            FavoriteRecipe(user_id=uuid4(), title="a", content="b", source_message_id=1, created_at=NOW)
        )
