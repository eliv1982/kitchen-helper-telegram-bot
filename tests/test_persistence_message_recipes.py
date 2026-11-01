"""The structured recipes of an assistant answer: storing, reading, ownership and retention.

The functions under test are persistence.message_recipes; the parser they run is the
accepted KH-F06A protocol (tests/test_recipes.py). Every test runs twice
(tests/persistence_support.py): on a schema created from the models and on one built by
the migrations. Nothing here calls a provider.
"""

import json
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.core.recipes import RECIPE_END, RECIPE_START
from house_helper.persistence import message_recipes as mr
from house_helper.persistence.base import RETENTION
from house_helper.persistence.models import (
    Conversation,
    Message,
    MessageRecipe,
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

NOW = utc(2026, 11, 1, 12)


def block(title="Борщ", servings="4 порции", ingredients=None, steps=None):
    return (
        f"{RECIPE_START}\n"
        + json.dumps(
            {
                "title": title,
                "servings": servings,
                "ingredients": [{"name": "свёкла", "amount": "2 шт."}]
                if ingredients is None
                else ingredients,
                "steps": ["Сварите бульон."] if steps is None else steps,
            },
            ensure_ascii=False,
        )
        + f"\n{RECIPE_END}"
    )


TWO_RECIPES = (
    "Вот два варианта.\n"
    + block("Борщ", "4 порции")
    + "\nМежду ними обычный текст.\n"
    + block("Уха", None, [{"name": "рыба", "amount": "1 кг"}], ["Сварите.", "Дайте настояться."])
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
    def make(db):
        conversation = new_conversation(db, db.get(User, owner), created_at=at, updated_at=at)
        ids = [
            new_message(db, conversation, role, text, created_at=at, expires_at=at + RETENTION).id
            for role, text in messages
        ]
        return conversation.id, ids

    return run(factory, make)


def record(factory, message_id, text, *, now=NOW):
    return run(factory, lambda db: mr.record_recipes_for_message(db, message_id, text, now=now))


def rows(factory):
    with factory() as db:
        return list(db.scalars(sa.select(MessageRecipe).order_by(MessageRecipe.position)))


# --- storing ----------------------------------------------------------------------------------


def test_every_accepted_recipe_is_stored_in_the_order_of_the_answer(factory, user_id):
    _, (answer,) = conversation_with(factory, user_id, ("assistant", TWO_RECIPES))

    stored = record(factory, answer, TWO_RECIPES)

    assert [(item.position, item.title) for item in stored] == [(0, "Борщ"), (1, "Уха")]
    assert [row.position for row in rows(factory)] == [0, 1]
    first, second = rows(factory)
    assert (first.message_id, first.message_role) == (answer, "assistant")
    assert first.title == "Борщ" and first.servings == "4 порции"
    assert first.ingredients == [{"name": "свекла", "amount": "2 шт."}]
    assert first.steps == ["Сварите бульон."]
    assert second.title == "Уха" and second.servings is None
    assert second.ingredients == [{"name": "рыба", "amount": "1 кг"}]
    assert second.steps == ["Сварите.", "Дайте настояться."]
    assert (first.created_at, second.created_at) == (NOW, NOW)


def test_the_text_was_spelled_by_the_parser_as_every_answer_is(factory, user_id):
    text = block("Свёкла", servings=None, ingredients=[{"name": "свёкла", "amount": "2"}], steps=["Тушите свёклу."])
    _, (answer,) = conversation_with(factory, user_id, ("assistant", text))

    record(factory, answer, text)

    (row,) = rows(factory)
    assert row.title == "Свекла"
    assert row.ingredients == [{"name": "свекла", "amount": "2"}]
    assert row.steps == ["Тушите свеклу."]


def test_an_answer_without_an_accepted_recipe_stores_nothing(factory, user_id):
    _, (answer,) = conversation_with(factory, user_id, ("assistant", "Просто совет без рецепта."))

    assert record(factory, answer, "Просто совет без рецепта.") == ()
    assert rows(factory) == []


def test_invalid_blocks_are_not_stored_and_valid_ones_are(factory, user_id):
    text = "Первый.\n" + block("Хороший рецепт") + "\n" + block("Плохой", ingredients=[]) + "\nхвост"
    _, (answer,) = conversation_with(factory, user_id, ("assistant", text))

    record(factory, answer, text)

    assert [(row.position, row.title) for row in rows(factory)] == [(0, "Хороший рецепт")]


def test_recording_again_replaces_what_was_stored(factory, user_id):
    _, (answer,) = conversation_with(factory, user_id, ("assistant", TWO_RECIPES))
    record(factory, answer, TWO_RECIPES)

    again = record(factory, answer, block("Только один"), now=NOW + timedelta(minutes=1))

    assert [(item.position, item.title) for item in again] == [(0, "Только один")]
    assert [(row.position, row.title) for row in rows(factory)] == [(0, "Только один")]


def test_only_an_assistant_message_can_hold_recipes(factory, user_id):
    _, (question, note) = conversation_with(
        factory, user_id, ("user", "Как сварить борщ?"), ("system_note", "reset")
    )

    for message in (question, note, 10**9):
        with pytest.raises(mr.NotAnAnswer):
            record(factory, message, TWO_RECIPES)
    assert rows(factory) == []


def test_recording_is_a_write_first(factory, user_id, engine):
    _, (answer,) = conversation_with(factory, user_id, ("assistant", TWO_RECIPES))
    seen = []
    listener = lambda c, cur, st, p, ctx, m: seen.append(st.lstrip().split(None, 1)[0].upper())  # noqa: E731
    sa.event.listen(engine, "before_cursor_execute", listener)
    try:
        record(factory, answer, TWO_RECIPES)
    finally:
        sa.event.remove(engine, "before_cursor_execute", listener)

    names = [name for name in seen if name not in ("BEGIN", "PRAGMA", "SAVEPOINT", "RELEASE")]
    assert names[0] == "DELETE"
    assert names[-1] == "INSERT"


# --- reading ----------------------------------------------------------------------------------


def test_the_owner_reads_every_recipe_with_its_position(factory, user_id):
    _, (answer,) = conversation_with(factory, user_id, ("assistant", TWO_RECIPES))
    record(factory, answer, TWO_RECIPES)

    found = run(factory, lambda db: mr.recipes_for_message(db, user_id, answer, now=NOW))

    assert [(item.position, item.title) for item in found] == [(0, "Борщ"), (1, "Уха")]
    assert found[1].servings is None
    assert found[1].steps == ("Сварите.", "Дайте настояться.")


def test_a_message_without_recipes_answers_with_an_empty_tuple(factory, user_id):
    _, (answer,) = conversation_with(factory, user_id, ("assistant", "Совет"))

    assert run(factory, lambda db: mr.recipes_for_message(db, user_id, answer, now=NOW)) == ()


def test_somebody_elses_answer_and_an_expired_one_are_not_found(factory, user_id, other_user_id):
    theirs, (their_answer,) = conversation_with(factory, other_user_id, ("assistant", TWO_RECIPES))
    record(factory, their_answer, TWO_RECIPES)
    old, (old_answer,) = conversation_with(
        factory,
        user_id,
        ("assistant", TWO_RECIPES),
        at=NOW - RETENTION - timedelta(days=2),
    )
    record(factory, old_answer, TWO_RECIPES, now=NOW - RETENTION - timedelta(days=2))

    with pytest.raises(mr.MessageNotFound):
        run(factory, lambda db: mr.recipes_for_message(db, user_id, their_answer, now=NOW))
    with pytest.raises(mr.MessageNotFound):
        run(factory, lambda db: mr.recipes_for_message(db, user_id, old_answer, now=NOW))
    with pytest.raises(mr.MessageNotFound):
        run(factory, lambda db: mr.recipes_for_message(db, user_id, 10**9, now=NOW))
    assert len(run(factory, lambda db: mr.recipes_for_message(db, other_user_id, their_answer, now=NOW))) == 2


def test_one_recipe_is_read_by_position_and_a_missing_one_is_not_found(factory, user_id):
    _, (answer,) = conversation_with(factory, user_id, ("assistant", TWO_RECIPES))
    record(factory, answer, TWO_RECIPES)

    found = run(factory, lambda db: mr.get_recipe_for_user(db, user_id, answer, 1, now=NOW))
    with pytest.raises(mr.RecipeNotFound):
        run(factory, lambda db: mr.get_recipe_for_user(db, user_id, answer, 2, now=NOW))

    assert (found.position, found.title) == (1, "Уха")


def test_the_messages_that_have_recipes(factory, user_id):
    conversation, answers = conversation_with(
        factory, user_id, ("assistant", TWO_RECIPES), ("assistant", "без рецепта")
    )
    record(factory, answers[0], TWO_RECIPES)

    found = run(factory, lambda db: mr.message_ids_with_recipes(db, answers))

    assert found == {answers[0]}


def test_reading_writes_nothing(factory, user_id, engine):
    _, (answer,) = conversation_with(factory, user_id, ("assistant", TWO_RECIPES))
    record(factory, answer, TWO_RECIPES)
    seen = []
    listener = lambda c, cur, st, p, ctx, m: seen.append(st.lstrip().split(None, 1)[0].upper())  # noqa: E731
    sa.event.listen(engine, "before_cursor_execute", listener)
    try:
        run(factory, lambda db: mr.recipes_for_message(db, user_id, answer, now=NOW))
        run(factory, lambda db: mr.get_recipe_for_user(db, user_id, answer, 0, now=NOW))
        run(factory, lambda db: mr.message_ids_with_recipes(db, [answer]))
    finally:
        sa.event.remove(engine, "before_cursor_execute", listener)

    assert set(seen) <= {"BEGIN", "SELECT", "PRAGMA", "ROLLBACK"}


# --- the database's own rules -----------------------------------------------------------------


def test_a_second_recipe_at_one_position_is_refused(session):
    user = new_user(session)
    conversation = new_conversation(session, user)
    answer = new_message(session, conversation, role="assistant")
    session.add(
        MessageRecipe(
            message_id=answer.id,
            position=0,
            title="a",
            ingredients=[{"name": "x", "amount": "1"}],
            steps=["s"],
            created_at=NOW,
        )
    )
    session.flush()

    with rejected(session, "message_recipes.message_id, message_recipes.position"):
        session.add(
            MessageRecipe(
                message_id=answer.id,
                position=0,
                title="b",
                ingredients=[{"name": "y", "amount": "1"}],
                steps=["t"],
                created_at=NOW,
            )
        )


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"message_role": "user"}, "ck_message_recipes_message_role_is_assistant"),
        ({"position": -1}, "ck_message_recipes_position_valid"),
        ({"position": 5}, "ck_message_recipes_position_valid"),
        ({"title": ""}, "ck_message_recipes_title_not_empty"),
        ({"title": "я" * 201}, "ck_message_recipes_title_max_length"),
        ({"servings": "я" * 101}, "ck_message_recipes_servings_max_length"),
    ],
)
def test_the_database_refuses_a_recipe_the_protocol_could_not_have_produced(session, fields, constraint):
    user = new_user(session)
    conversation = new_conversation(session, user)
    answer = new_message(session, conversation, role="assistant")
    values = {
        "message_id": answer.id,
        "position": 0,
        "title": "ok",
        "ingredients": [{"name": "x", "amount": "1"}],
        "steps": ["s"],
        "created_at": NOW,
        **fields,
    }

    with rejected(session, constraint):
        session.add(MessageRecipe(**values))


def test_a_recipe_cannot_be_attached_to_a_user_message_even_by_a_direct_write(session):
    user = new_user(session)
    conversation = new_conversation(session, user)
    question = new_message(session, conversation, role="user", content="?")

    with rejected(session, "FOREIGN KEY"):
        session.add(
            MessageRecipe(
                message_id=question.id,
                position=0,
                title="a",
                ingredients=[{"name": "x", "amount": "1"}],
                steps=["s"],
                created_at=NOW,
            )
        )


def test_a_recipe_goes_with_its_message_and_with_its_user(factory, user_id):
    conversation_id, (answer,) = conversation_with(factory, user_id, ("assistant", TWO_RECIPES))
    record(factory, answer, TWO_RECIPES)

    with factory() as db, db.begin():
        db.execute(sa.delete(Message).where(Message.id == answer))

    assert rows(factory) == []

    conversation_id, (other,) = conversation_with(factory, user_id, ("assistant", TWO_RECIPES))
    record(factory, other, TWO_RECIPES)
    with factory() as db, db.begin():
        db.execute(sa.delete(User).where(User.id == user_id))
    assert rows(factory) == []
    with factory() as db:
        assert db.get(Conversation, conversation_id) is None


def test_a_hand_edited_row_is_refused_when_it_is_read_for_a_copy(factory, user_id):
    _, (answer,) = conversation_with(factory, user_id, ("assistant", TWO_RECIPES))
    record(factory, answer, TWO_RECIPES)
    with factory() as db, db.begin():
        db.execute(
            sa.update(MessageRecipe)
            .where(MessageRecipe.message_id == answer, MessageRecipe.position == 0)
            .values(ingredients=[])
        )

    with pytest.raises(ValueError):
        run(factory, lambda db: mr.recipes_for_message(db, user_id, answer, now=NOW))
