"""Structured favorites: one recipe of an answer saved, edited and kept independently.

The functions under test are persistence.favorites and persistence.message_recipes. The
message recipes are seeded through the supported writer; no provider is called anywhere.
Every test runs twice (tests/persistence_support.py): on a schema created from the models
and on one built by the migrations.
"""

import json
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.core.recipes import (
    RECIPE_END,
    RECIPE_START,
    Ingredient,
    Recipe,
    render_recipe,
)
from house_helper.persistence import favorites as fav
from house_helper.persistence import message_recipes as mr
from house_helper.persistence.base import RETENTION
from house_helper.persistence.models import (
    MAX_FAVORITE_NOTES_LENGTH,
    Conversation,
    FavoriteRecipe,
    Message,
    MessageRecipe,
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
LATER = NOW + timedelta(hours=1)

BORSH = {
    "title": "Борщ",
    "servings": "4 порции",
    "ingredients": [{"name": "свёкла", "amount": "2 шт."}, {"name": "капуста", "amount": "300 г"}],
    "steps": ["Сварите бульон.", "Добавьте овощи."],
}
UKHA = {
    "title": "Уха",
    "servings": None,
    "ingredients": [{"name": "рыба", "amount": "1 кг"}],
    "steps": ["Сварите.", "Дайте настояться."],
}


def block(recipe):
    return f"{RECIPE_START}\n{json.dumps(recipe, ensure_ascii=False)}\n{RECIPE_END}"


TWO_RECIPES = "Вот два варианта.\n" + block(BORSH) + "\nИ еще.\n" + block(UKHA)


def run(factory, work):
    with factory() as db, db.begin():
        return work(db)


@pytest.fixture
def user_id(factory):
    return run(factory, lambda db: new_user(db).id)


@pytest.fixture
def other_user_id(factory):
    return run(factory, lambda db: new_user(db).id)


def answer_with(factory, owner, text=TWO_RECIPES, *, at=NOW):
    """(conversation id, message id) of an assistant answer whose recipes are stored."""

    def make(db):
        conversation = new_conversation(db, db.get(User, owner), created_at=at, updated_at=at)
        message = new_message(
            db,
            conversation,
            "assistant",
            text,
            created_at=at,
            expires_at=at + RETENTION,
        )
        mr.record_recipes_for_message(db, message.id, text, now=at)
        return conversation.id, message.id

    return run(factory, make)


def save(factory, owner, conversation_id, message_id, index=None, *, now=NOW):
    return run(
        factory,
        lambda db: fav.save_favorite(
            db, owner, conversation_id, message_id, now=now, recipe_index=index
        ),
    )


def edit(factory, owner, favorite_id, expected_version, changes, *, now=LATER):
    return run(
        factory,
        lambda db: fav.update_favorite(
            db,
            owner,
            favorite_id,
            expected_version=expected_version,
            changes=changes,
            now=now,
        ),
    )


def detail(factory, owner, favorite_id):
    return run(factory, lambda db: fav.get_favorite(db, owner, favorite_id))


def favorites(factory):
    with factory() as db:
        return list(db.scalars(sa.select(FavoriteRecipe).order_by(FavoriteRecipe.created_at)))


def message_recipe_rows(factory, message_id):
    with factory() as db:
        return list(
            db.scalars(
                sa.select(MessageRecipe)
                .where(MessageRecipe.message_id == message_id)
                .order_by(MessageRecipe.position)
            )
        )


def message_recipe_values(factory, message_id):
    return [
        (row.position, row.title, row.servings, row.ingredients, row.steps)
        for row in message_recipe_rows(factory, message_id)
    ]


def rendered(title, servings, ingredients, steps, notes=None):
    body = render_recipe(
        Recipe(
            index=0,
            title=title,
            servings=servings,
            ingredients=tuple(Ingredient(*item) for item in ingredients),
            steps=tuple(steps),
        )
    )
    return body if not notes else f"{body}\n\n{fav.NOTES_HEADING}\n{notes}"


# --- saving -----------------------------------------------------------------------------------


def test_each_recipe_of_one_answer_is_a_separate_favorite(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)

    first = save(factory, user_id, conversation_id, answer, 0)
    second = save(factory, user_id, conversation_id, answer, 1)

    assert first.created is True and second.created is True
    assert first.favorite.id != second.favorite.id
    assert (first.favorite.title, second.favorite.title) == ("Борщ", "Уха")
    assert first.favorite.kind == second.favorite.kind == "structured"
    assert (first.favorite.source_recipe_index, second.favorite.source_recipe_index) == (0, 1)
    assert first.favorite.version == second.favorite.version == 1
    assert len(favorites(factory)) == 2


def test_the_saved_copy_holds_the_fields_and_its_rendered_text(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)

    saved = save(factory, user_id, conversation_id, answer, 0)
    found = detail(factory, user_id, saved.favorite.id)

    assert found.kind == "structured" and found.source_recipe_index == 0
    assert found.title == "Борщ" and found.servings == "4 порции"
    assert found.ingredients == (
        Ingredient("свекла", "2 шт."),
        Ingredient("капуста", "300 г"),
    )
    assert found.steps == ("Сварите бульон.", "Добавьте овощи.")
    assert found.notes is None
    assert found.text == rendered(
        "Борщ",
        "4 порции",
        [("свекла", "2 шт."), ("капуста", "300 г")],
        ["Сварите бульон.", "Добавьте овощи."],
    )


def test_a_recipe_without_servings_saves_without_them(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)

    found = detail(factory, user_id, save(factory, user_id, conversation_id, answer, 1).favorite.id)

    assert found.servings is None
    assert found.text.split("\n")[:3] == ["Уха", "", "Ингредиенты:"]


def test_saving_the_same_recipe_again_is_the_same_favorite(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    first = save(factory, user_id, conversation_id, answer, 0)

    again = save(factory, user_id, conversation_id, answer, 0, now=LATER)

    assert again.created is False and again.favorite == first.favorite
    assert len(favorites(factory)) == 1


def test_the_text_and_a_structured_recipe_of_one_answer_coexist(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)

    whole = save(factory, user_id, conversation_id, answer)
    one = save(factory, user_id, conversation_id, answer, 0)

    assert whole.created is True and one.created is True
    assert {row.kind for row in favorites(factory)} == {"text", "structured"}
    assert (whole.favorite.source_recipe_index, one.favorite.source_recipe_index) == (-1, 0)


def test_a_position_that_was_not_accepted_is_not_found(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)

    with pytest.raises(mr.RecipeNotFound):
        save(factory, user_id, conversation_id, answer, 2)

    assert favorites(factory) == []


@pytest.mark.parametrize("index", [-1, 5, True, "0", 1.0])
def test_an_index_outside_the_protocol_is_invalid_and_writes_nothing(factory, user_id, index):
    conversation_id, answer = answer_with(factory, user_id)

    with pytest.raises(fav.InvalidFavorite):
        save(factory, user_id, conversation_id, answer, index)

    assert favorites(factory) == []


def test_a_structured_save_is_idempotent_under_the_limit(factory, user_id, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_LIMIT", 1)
    conversation_id, answer = answer_with(factory, user_id)
    save(factory, user_id, conversation_id, answer, 0)

    with pytest.raises(fav.FavoritesFull):
        save(factory, user_id, conversation_id, answer, 1)
    again = save(factory, user_id, conversation_id, answer, 0)

    assert again.created is False and len(favorites(factory)) == 1


# --- independence -----------------------------------------------------------------------------


def test_the_favorite_is_a_copy_of_the_message_recipe(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite
    before = detail(factory, user_id, favorite.id)

    # The source recipe changes and the message is deleted entirely.
    with factory() as db, db.begin():
        db.execute(
            sa.update(MessageRecipe)
            .where(MessageRecipe.message_id == answer)
            .values(title="Другой борщ")
        )
        db.execute(sa.delete(Message).where(Message.id == answer))

    after = detail(factory, user_id, favorite.id)

    assert after == before
    assert (after.title, after.source_message_id) == ("Борщ", answer)
    assert message_recipe_rows(factory, answer) == []


def test_editing_a_favorite_does_not_touch_the_message_or_its_recipes(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite
    recipe_before = message_recipe_values(factory, answer)
    with factory() as db:
        message_before = db.get(Message, answer).content

    edit(factory, user_id, favorite.id, 1, {
        "title": "Мой борщ",
        "servings": "2 порции",
        "ingredients": [{"name": "морковь", "amount": "1 шт."}],
        "steps": ["Готовьте иначе."],
        "notes": "так вкуснее",
    })

    assert message_recipe_values(factory, answer) == recipe_before
    with factory() as db:
        assert db.get(Message, answer).content == message_before


def test_removing_a_structured_favorite_leaves_the_source_alone(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite

    run(factory, lambda db: fav.remove_favorite(db, user_id, favorite.id))

    assert favorites(factory) == []
    assert len(message_recipe_rows(factory, answer)) == 2
    with factory() as db:
        assert db.get(Message, answer) is not None


def test_a_structured_favorite_outlives_the_expiry_of_its_answer(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite
    later = NOW + RETENTION + timedelta(days=1)

    # The answer is over the retention line, so it is no longer there to copy ...
    with pytest.raises(fav.MessageNotFound):
        save(factory, user_id, conversation_id, answer, 0, now=later)
    # ... but the copy made before it expired is still the person's.
    found = run(factory, lambda db: fav.get_favorite(db, user_id, favorite.id))
    assert found.title == "Борщ" and found.source_message_id == answer
    assert [item.id for item in run(factory, lambda db: fav.list_favorites(db, user_id))] == [
        favorite.id
    ]


def test_a_structured_favorite_survives_the_deletion_of_its_message_and_conversation(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite

    with factory() as db, db.begin():
        db.execute(sa.delete(Message).where(Message.conversation_id == conversation_id))
        db.execute(sa.delete(Conversation).where(Conversation.id == conversation_id))

    assert [item.id for item in run(factory, lambda db: fav.list_favorites(db, user_id))] == [
        favorite.id
    ]
    assert detail(factory, user_id, favorite.id).title == "Борщ"
    with factory() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(MessageRecipe)) == 0


# --- editing ----------------------------------------------------------------------------------


def test_editing_a_structured_favorite_renders_the_new_fields(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite

    updated = edit(factory, user_id, favorite.id, 1, {
        "title": "Мой борщ",
        "servings": "2 порции",
        "ingredients": [{"name": "морковь", "amount": "1 шт."}],
        "steps": ["Готовьте иначе.", "Подавайте."],
        "notes": "Помнить про сметану.",
    })

    assert updated.version == 2
    assert updated.title == "Мой борщ" and updated.servings == "2 порции"
    assert updated.ingredients == (Ingredient("морковь", "1 шт."),)
    assert updated.steps == ("Готовьте иначе.", "Подавайте.")
    assert updated.notes == "Помнить про сметану."
    assert updated.text == rendered(
        "Мой борщ",
        "2 порции",
        [("морковь", "1 шт.")],
        ["Готовьте иначе.", "Подавайте."],
        notes="Помнить про сметану.",
    )
    assert updated.created_at == NOW and updated.updated_at == LATER


def test_fields_that_are_not_named_are_left_as_they_were(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite

    updated = edit(factory, user_id, favorite.id, 1, {"title": "Новое имя"})

    assert updated.title == "Новое имя"
    assert updated.servings == "4 порции"
    assert updated.ingredients == (Ingredient("свекла", "2 шт."), Ingredient("капуста", "300 г"))
    assert updated.steps == ("Сварите бульон.", "Добавьте овощи.")


def test_servings_and_notes_can_be_cleared(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite
    edit(factory, user_id, favorite.id, 1, {"servings": None, "notes": "заметка"})

    updated = edit(factory, user_id, favorite.id, 2, {"servings": None, "notes": None})

    assert updated.servings is None and updated.notes is None
    assert fav.NOTES_HEADING not in updated.text
    assert updated.text == rendered(
        "Борщ", None, [("свекла", "2 шт."), ("капуста", "300 г")], ["Сварите бульон.", "Добавьте овощи."]
    )


def test_a_successful_edit_increments_the_version_and_moves_only_updated_at(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite

    first = edit(factory, user_id, favorite.id, 1, {"title": "Раз"}, now=LATER)
    second = edit(factory, user_id, favorite.id, 2, {"title": "Два"}, now=LATER + timedelta(hours=1))

    assert (first.version, second.version) == (2, 3)
    assert second.created_at == NOW and second.updated_at == LATER + timedelta(hours=1)
    assert detail(factory, user_id, favorite.id).version == 3


def test_a_stale_edit_conflicts_and_changes_nothing(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite
    edit(factory, user_id, favorite.id, 1, {"title": "Первый правка"})

    with pytest.raises(fav.FavoriteConflict):
        edit(factory, user_id, favorite.id, 1, {"title": "Устаревшая правка"})

    found = detail(factory, user_id, favorite.id)
    assert found.title == "Первый правка" and found.version == 2


def test_editing_a_text_favorite_keeps_its_text_exactly_and_notes_out_of_it(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer).favorite

    updated = edit(factory, user_id, favorite.id, 1, {
        "title": "Мой заголовок",
        "text": "  Новый текст, ровно как введен.  ",
        "notes": "Личная заметка.",
    })

    assert updated.kind == "text" and updated.source_recipe_index == -1
    assert updated.title == "Мой заголовок"
    assert updated.text == "  Новый текст, ровно как введен.  "
    assert updated.notes == "Личная заметка."
    assert updated.version == 2


def test_a_title_edit_does_not_rewrite_a_text_favorite(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer).favorite
    before = detail(factory, user_id, favorite.id)

    updated = edit(factory, user_id, favorite.id, 1, {"title": "Другое имя"})

    assert updated.text == before.text and updated.title == "Другое имя"


def test_a_text_favorite_has_no_structured_fields_and_a_structured_one_has_no_text(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    text = save(factory, user_id, conversation_id, answer).favorite
    structured = save(factory, user_id, conversation_id, answer, 0).favorite

    for changes in ({"servings": None}, {"ingredients": [{"name": "x", "amount": "1"}]}, {"steps": ["s"]}):
        with pytest.raises(fav.InvalidFavorite):
            edit(factory, user_id, text.id, text.version, changes)
    with pytest.raises(fav.InvalidFavorite):
        edit(factory, user_id, structured.id, structured.version, {"text": "нельзя"})
    assert detail(factory, user_id, text.id).version == 1
    assert detail(factory, user_id, structured.id).version == 1


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, fav.InvalidFavorite),
        ({"unknown": "x"}, fav.InvalidFavorite),
        ({"title": ""}, fav.InvalidFavorite),
        ({"title": "x" * 201}, fav.InvalidFavorite),
        ({"title": "a\nb"}, fav.InvalidFavorite),
        ({"title": f"{RECIPE_START} x"}, fav.InvalidFavorite),
        ({"text": ""}, fav.InvalidFavorite),
        ({"text": "   "}, fav.InvalidFavorite),
        ({"text": "x" * (fav.MAX_FAVORITE_TEXT_LENGTH + 1)}, fav.InvalidFavorite),
        ({"notes": "x" * (MAX_FAVORITE_NOTES_LENGTH + 1)}, fav.InvalidFavorite),
    ],
)
def test_an_invalid_edit_is_refused_and_writes_nothing(factory, user_id, changes, expected):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer).favorite

    with pytest.raises(expected):
        edit(factory, user_id, favorite.id, 1, changes)

    assert detail(factory, user_id, favorite.id).version == 1


def test_the_largest_acceptable_version_is_a_conflict_not_an_overflow(factory, user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer).favorite

    with pytest.raises(fav.FavoriteConflict):
        edit(factory, user_id, favorite.id, fav.MAX_FAVORITE_VERSION, {"title": "x"})

    assert fav.MAX_FAVORITE_VERSION + 1 <= 2**31 - 1  # its successor fits the column


@pytest.mark.parametrize("version", [0, -1, True, "1", None, 1.0, 2**31, 10**100])
def test_an_invalid_expected_version_is_refused(factory, user_id, version):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer).favorite

    with pytest.raises(fav.InvalidFavorite):
        edit(factory, user_id, favorite.id, version, {"title": "x"})


@pytest.mark.parametrize(
    "changes",
    [
        {"ingredients": []},
        {"ingredients": [{"name": "x"}]},
        {"ingredients": [{"name": "", "amount": "1"}]},
        {"steps": []},
        {"steps": [1]},
        {"servings": 4},
        {"servings": "x" * 101},
        {"title": ""},
    ],
)
def test_invalid_structured_fields_are_refused(factory, user_id, changes):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite

    with pytest.raises(fav.InvalidFavorite):
        edit(factory, user_id, favorite.id, 1, changes)

    assert detail(factory, user_id, favorite.id).version == 1


def test_an_edit_is_a_write_first(factory, user_id, engine):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite
    seen = []
    listener = lambda c, cur, st, p, ctx, m: seen.append(st.lstrip().split(None, 1)[0].upper())  # noqa: E731
    sa.event.listen(engine, "before_cursor_execute", listener)
    try:
        edit(factory, user_id, favorite.id, 1, {"title": "x"})
    finally:
        sa.event.remove(engine, "before_cursor_execute", listener)

    names = [name for name in seen if name not in ("BEGIN", "PRAGMA", "SAVEPOINT", "RELEASE")]
    assert names[0] == "UPDATE"


# --- ownership --------------------------------------------------------------------------------


def test_another_person_cannot_read_edit_or_remove_a_structured_favorite(factory, user_id, other_user_id):
    conversation_id, answer = answer_with(factory, user_id)
    favorite = save(factory, user_id, conversation_id, answer, 0).favorite

    with pytest.raises(fav.FavoriteNotFound):
        detail(factory, other_user_id, favorite.id)
    with pytest.raises(fav.FavoriteNotFound):
        edit(factory, other_user_id, favorite.id, 1, {"title": "чужое"})
    with pytest.raises(fav.FavoriteNotFound):
        run(factory, lambda db: fav.remove_favorite(db, other_user_id, favorite.id))

    assert detail(factory, user_id, favorite.id).title == "Борщ"


def test_the_limit_counts_both_kinds_together(factory, user_id, monkeypatch):
    monkeypatch.setattr(fav, "FAVORITES_LIMIT", 2)
    conversation_id, answer = answer_with(factory, user_id)
    save(factory, user_id, conversation_id, answer)  # text
    save(factory, user_id, conversation_id, answer, 0)  # structured

    with pytest.raises(fav.FavoritesFull):
        save(factory, user_id, conversation_id, answer, 1)

    assert len(favorites(factory)) == 2


# --- the database's own rules -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "constraint"),
    [
        ({"kind": "other"}, "ck_favorite_recipes_kind_valid"),
        ({"source_recipe_index": 5}, "ck_favorite_recipes_source_recipe_index_valid"),
        ({"version": 0}, "ck_favorite_recipes_version_valid"),
        ({"notes": "x" * 2001}, "ck_favorite_recipes_notes_max_length"),
        (
            {
                "kind": "structured",
                "source_recipe_index": 0,
                "servings": "x" * 101,
                "ingredients": [{"name": "x", "amount": "1"}],
                "steps": ["s"],
            },
            "ck_favorite_recipes_servings_max_length",
        ),
        (
            {"kind": "text", "servings": "4"},
            "ck_favorite_recipes_kind_consistent",
        ),
        (
            {"kind": "structured", "source_recipe_index": -1},
            "ck_favorite_recipes_kind_consistent",
        ),
        (
            {"kind": "structured", "ingredients": None},
            "ck_favorite_recipes_kind_consistent",
        ),
    ],
)
def test_the_database_refuses_an_inconsistent_favorite(session, fields, constraint):
    user = new_user(session)
    values = {
        "user_id": user.id,
        "title": "ok",
        "content": "ok",
        "source_message_id": 1,
        "created_at": NOW,
        "updated_at": NOW,
        **fields,
    }

    with rejected(session, constraint):
        session.add(FavoriteRecipe(**values))


def test_two_recipes_of_one_message_can_be_favorites_but_the_same_position_cannot(session):
    user = new_user(session)
    first = FavoriteRecipe(
        user_id=user.id,
        title="a",
        content="b",
        source_message_id=7,
        kind="structured",
        source_recipe_index=0,
        ingredients=[{"name": "x", "amount": "1"}],
        steps=["s"],
        created_at=NOW,
        updated_at=NOW,
    )
    second = FavoriteRecipe(
        user_id=user.id,
        title="c",
        content="d",
        source_message_id=7,
        kind="structured",
        source_recipe_index=1,
        ingredients=[{"name": "y", "amount": "1"}],
        steps=["t"],
        created_at=NOW,
        updated_at=NOW,
    )
    session.add_all([first, second])
    session.flush()

    with rejected(
        session,
        "favorite_recipes.user_id, favorite_recipes.source_message_id, favorite_recipes.source_recipe_index",
    ):
        session.add(
            FavoriteRecipe(
                user_id=user.id,
                title="e",
                content="f",
                source_message_id=7,
                kind="structured",
                source_recipe_index=0,
                ingredients=[{"name": "z", "amount": "1"}],
                steps=["u"],
                created_at=NOW,
                updated_at=NOW,
            )
        )


def test_identity_kind_and_creation_time_cannot_be_rewritten_on_the_orm_path(session):
    user = new_user(session)
    row = FavoriteRecipe(
        user_id=user.id,
        title="a",
        content="b",
        source_message_id=1,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(row)
    session.flush()

    row.kind = "structured"
    with pytest.raises(ValueError, match="cannot change once stored"):
        session.flush()
