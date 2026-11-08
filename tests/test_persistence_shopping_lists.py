"""Saved shopping lists: the complete snapshot, its states, its life and its owners.

Every test runs against both schemas (built from the models and built by the
migrations), because a rule that only one of them enforces is a rule the deployed
database does not have. The recipes are real validated protocol recipes, stored through
message_recipes, so a list is made from the same rows the application reads; no provider
is called and nothing leaves the machine.
"""

import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.core.recipes import MAX_RECIPES_PER_REPLY, Ingredient, Recipe
from house_helper.persistence import favorites as fav
from house_helper.persistence import message_recipes as mr
from house_helper.persistence import shopping_lists as sl
from house_helper.persistence.models import (
    Conversation,
    FavoriteRecipe,
    Message,
    ShoppingList,
    ShoppingListItem,
    ShoppingListRequest,
    User,
)
from tests.persistence_support import (  # noqa: F401  (fixtures)
    digest,
    engine,
    factory,
    new_conversation,
    new_message,
    new_user,
    raw_insert,
    rejected,
    session,
    stored,
    utc,
)

NOW = utc(2026, 11, 8, 12)
BORSH = Recipe(
    index=0,
    title="Борщ",
    servings="4 порции",
    ingredients=(
        Ingredient("свёкла", "2 шт."),
        Ingredient("капуста", "300 г"),
        Ingredient("говядина", "500 г"),
    ),
    steps=("Сварите бульон.", "Добавьте овощи."),
)
OMELETTE = Recipe(
    index=1,
    title="Омлет",
    servings=None,
    ingredients=(Ingredient("яйца", "3 шт."),),
    steps=("Взбейте и обжарьте.",),
)


def answered(db, user, recipe: Recipe = BORSH):
    """A conversation of `user` with one assistant answer carrying `recipe`."""
    conversation = new_conversation(db, user, created_at=NOW, updated_at=NOW)
    answer = new_message(
        db,
        conversation,
        role="assistant",
        content="Вот рецепт.",
        created_at=NOW,
        expires_at=NOW + timedelta(days=365),
    )
    mr.record_validated_recipes(db, answer.id, [recipe], now=NOW)
    return conversation, answer


def create_message_list(
    db,
    user,
    conversation,
    answer,
    *,
    key="list-1",
    recipe_index=0,
    item_states=None,
    now=NOW,
):
    fingerprint = sl.creation_fingerprint(
        source="message",
        conversation_id=conversation.id,
        message_id=answer.id,
        recipe_index=recipe_index,
        item_states=item_states,
    )
    return sl.create_list(
        db,
        user.id,
        source="message",
        conversation_id=conversation.id,
        message_id=answer.id,
        recipe_index=recipe_index,
        item_states=item_states,
        idempotency_key=key,
        fingerprint=fingerprint,
        now=now,
    )


def structured_favorite(db, user, conversation, answer, recipe_index=0, now=NOW):
    return fav.save_favorite(
        db, user.id, conversation.id, answer.id, now=now, recipe_index=recipe_index
    ).favorite


def counts(db) -> tuple[int, int]:
    return (
        db.scalar(sa.select(sa.func.count()).select_from(ShoppingList)),
        db.scalar(sa.select(sa.func.count()).select_from(ShoppingListItem)),
    )


def requests(db) -> list[ShoppingListRequest]:
    return db.scalars(sa.select(ShoppingListRequest).order_by(ShoppingListRequest.id)).all()


# --- the complete snapshot --------------------------------------------------------------


def test_a_saved_list_copies_every_ingredient_in_order_with_its_quantities(session):
    user = new_user(session)
    conversation, answer = answered(session, user)

    made = create_message_list(session, user, conversation, answer)

    assert made.created is True
    found = made.shopping_list
    assert found.title == "Борщ"
    assert [item.position for item in found.items] == [0, 1, 2]
    assert [(item.name, item.amount) for item in found.items] == [
        ("свёкла", "2 шт."),
        ("капуста", "300 г"),
        ("говядина", "500 г"),
    ]
    assert [item.state for item in found.items] == ["unchecked", "unchecked", "unchecked"]


def test_the_draft_states_are_kept_and_every_other_ingredient_is_unchecked(session):
    user = new_user(session)
    conversation, answer = answered(session, user)

    made = create_message_list(
        session, user, conversation, answer, item_states={0: "at_home", 2: "purchased"}
    )

    assert [item.state for item in made.shopping_list.items] == [
        "at_home",
        "unchecked",
        "purchased",
    ]


def test_a_list_can_be_saved_when_every_ingredient_is_at_home(session):
    user = new_user(session)
    conversation, answer = answered(session, user)

    made = create_message_list(
        session, user, conversation, answer, item_states={0: "at_home", 1: "at_home", 2: "at_home"}
    )

    found = made.shopping_list
    assert len(found.items) == 3  # nothing was filtered out
    assert [item.state for item in found.items] == ["at_home", "at_home", "at_home"]
    summary = sl.list_lists(session, user.id)[0]
    assert (summary.item_count, summary.unchecked_count, summary.at_home_count, summary.purchased_count) == (
        3,
        0,
        3,
        0,
    )


def test_a_recipe_with_no_available_ingredients_still_has_its_items(session):
    # "Empty to buy" is a view of the states, not an empty list: the snapshot stays whole.
    user = new_user(session)
    conversation, answer = answered(session, user, OMELETTE)

    made = create_message_list(session, user, conversation, answer)

    assert [(item.name, item.state) for item in made.shopping_list.items] == [
        ("яйца", "unchecked")
    ]


# --- states and item editing ------------------------------------------------------------


def test_an_item_moves_between_all_three_states_and_at_home_never_deletes_it(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    made = create_message_list(session, user, conversation, answer)
    item_id = made.shopping_list.items[0].id

    seen = []
    for state in ("at_home", "purchased", "unchecked", "purchased", "at_home"):
        item = sl.update_item(
            session, user.id, made.shopping_list.id, item_id, changes={"state": state}, now=NOW
        )
        seen.append(item.state)
        assert counts(session)[1] == 3  # no state ever removed a row

    assert seen == ["at_home", "purchased", "unchecked", "purchased", "at_home"]


def test_a_manual_item_is_appended_and_its_fields_are_edited_and_deleted(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    made = create_message_list(session, user, conversation, answer)
    list_id = made.shopping_list.id

    added = sl.add_item(
        session, user.id, list_id, name="  соль  ", amount=" по вкусу ", state="at_home", now=NOW
    )
    assert (added.position, added.name, added.amount, added.state) == (
        3,
        "соль",
        "по вкусу",
        "at_home",
    )

    edited = sl.update_item(
        session,
        user.id,
        list_id,
        added.id,
        changes={"name": "морская соль", "amount": None, "state": "purchased"},
        now=NOW,
    )
    assert (edited.name, edited.amount, edited.state) == (
        "морская соль",
        None,
        "purchased",
    )

    # An absent field is left as it is; a null amount clears only the amount.
    partial = sl.update_item(
        session, user.id, list_id, added.id, changes={"name": "соль"}, now=NOW
    )
    assert (partial.name, partial.amount, partial.state) == ("соль", None, "purchased")

    stored_item = sl.get_list(session, user.id, list_id).items[-1]
    assert (stored_item.name, stored_item.amount, stored_item.state) == (
        "соль",
        None,
        "purchased",
    )

    sl.delete_item(session, user.id, list_id, added.id, now=NOW)
    assert [item.position for item in sl.get_list(session, user.id, list_id).items] == [0, 1, 2]


def test_a_completed_list_is_read_only_until_it_is_reopened(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    made = create_message_list(session, user, conversation, answer)
    list_id = made.shopping_list.id
    item_id = made.shopping_list.items[0].id

    completed = sl.set_status(session, user.id, list_id, status="completed", now=NOW)
    assert completed.status == "completed" and len(completed.items) == 3

    with pytest.raises(sl.ShoppingListCompleted):
        sl.add_item(session, user.id, list_id, name="соль", now=NOW)
    with pytest.raises(sl.ShoppingListCompleted):
        sl.update_item(
            session, user.id, list_id, item_id, changes={"state": "at_home"}, now=NOW
        )
    with pytest.raises(sl.ShoppingListCompleted):
        sl.delete_item(session, user.id, list_id, item_id, now=NOW)
    assert len(sl.get_list(session, user.id, list_id).items) == 3

    reopened = sl.set_status(session, user.id, list_id, status="active", now=NOW)
    assert reopened.status == "active" and len(reopened.items) == 3

    edited = sl.update_item(
        session, user.id, list_id, item_id, changes={"state": "at_home"}, now=NOW
    )
    assert edited.state == "at_home"


def test_setting_the_status_it_already_has_writes_nothing(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    made = create_message_list(session, user, conversation, answer)
    list_id = made.shopping_list.id

    same = sl.set_status(session, user.id, list_id, status="active", now=NOW + timedelta(hours=1))

    assert same.status == "active"
    assert same.updated_at == NOW  # not moved by a no-op


# --- the two sources --------------------------------------------------------------------


def test_a_list_is_created_from_a_structured_favorite_at_the_version_the_person_saw(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    favorite = structured_favorite(session, user, conversation, answer)

    fingerprint = sl.creation_fingerprint(
        source="favorite", favorite_id=favorite.id, expected_version=favorite.version
    )
    made = sl.create_list(
        session,
        user.id,
        source="favorite",
        favorite_id=favorite.id,
        expected_version=favorite.version,
        idempotency_key="fav-1",
        fingerprint=fingerprint,
        now=NOW,
    )

    found = made.shopping_list
    assert found.title == "Борщ"
    assert found.source_kind == "favorite"
    assert (found.source_favorite_id, found.source_favorite_version) == (
        favorite.id,
        favorite.version,
    )
    assert (found.source_message_id, found.source_recipe_index) == (None, None)
    assert [(item.name, item.amount) for item in found.items] == [
        ("свёкла", "2 шт."),
        ("капуста", "300 г"),
        ("говядина", "500 г"),
    ]


def test_a_changed_favorite_is_a_conflict_and_nothing_is_saved(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    favorite = structured_favorite(session, user, conversation, answer)
    fav.update_favorite(
        session,
        user.id,
        favorite.id,
        expected_version=favorite.version,
        changes={"title": "Другой борщ"},
        now=NOW,
    )

    fingerprint = sl.creation_fingerprint(
        source="favorite", favorite_id=favorite.id, expected_version=favorite.version
    )
    with pytest.raises(fav.FavoriteConflict):
        sl.create_list(
            session,
            user.id,
            source="favorite",
            favorite_id=favorite.id,
            expected_version=favorite.version,
            idempotency_key="fav-1",
            fingerprint=fingerprint,
            now=NOW,
        )

    assert counts(session) == (0, 0)


def test_a_text_favorite_cannot_be_copied_as_a_list(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    text = fav.save_favorite(session, user.id, conversation.id, answer.id, now=NOW).favorite

    fingerprint = sl.creation_fingerprint(
        source="favorite", favorite_id=text.id, expected_version=text.version
    )
    with pytest.raises(fav.InvalidFavorite):
        sl.create_list(
            session,
            user.id,
            source="favorite",
            favorite_id=text.id,
            expected_version=text.version,
            idempotency_key="fav-1",
            fingerprint=fingerprint,
            now=NOW,
        )

    assert counts(session) == (0, 0)


# --- idempotent creation ----------------------------------------------------------------


def test_creation_is_idempotent_for_the_same_key_and_request(session):
    user = new_user(session)
    conversation, answer = answered(session, user)

    first = create_message_list(session, user, conversation, answer)
    again = create_message_list(session, user, conversation, answer)

    assert first.created is True and again.created is False
    assert again.shopping_list.id == first.shopping_list.id
    assert counts(session) == (1, 3)


def test_the_same_key_with_another_request_is_refused(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    create_message_list(session, user, conversation, answer, item_states={0: "at_home"})

    with pytest.raises(sl.IdempotencyKeyReused):
        create_message_list(
            session, user, conversation, answer, item_states={0: "purchased"}
        )

    assert counts(session) == (1, 3)
    assert sl.get_list(session, user.id, sl.list_lists(session, user.id)[0].id).items[
        0
    ].state == "at_home"


def test_a_replay_answers_the_saved_list_after_its_source_is_gone(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    first = create_message_list(session, user, conversation, answer)

    session.execute(sa.delete(Message).where(Message.id == answer.id))
    session.execute(sa.delete(Conversation).where(Conversation.id == conversation.id))

    again = create_message_list(session, user, conversation, answer)

    assert again.created is False
    assert again.shopping_list.id == first.shopping_list.id
    assert len(again.shopping_list.items) == 3


# --- the key outlives the list ------------------------------------------------------------


def test_a_retry_after_deletion_is_a_stable_conflict_and_recreates_nothing(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    made = create_message_list(session, user, conversation, answer)

    sl.delete_list(session, user.id, made.shopping_list.id)

    assert counts(session) == (0, 0)  # the list and every item are gone
    assert sl.list_lists(session, user.id) == []
    for _ in range(2):  # stable: the same answer every time, not a one-off
        with pytest.raises(sl.ShoppingListDeleted):
            create_message_list(session, user, conversation, answer)
    assert counts(session) == (0, 0)  # nothing was resurrected

    (tombstone,) = requests(session)
    assert tombstone.idempotency_key == "list-1"
    assert tombstone.list_id is None
    assert tombstone.request_fingerprint == sl.creation_fingerprint(
        source="message",
        conversation_id=conversation.id,
        message_id=answer.id,
        recipe_index=0,
    )
    # The tombstone keeps only technical metadata: no title, no ingredients, no state.
    assert [column.name for column in tombstone.__table__.columns] == [
        "id",
        "user_id",
        "idempotency_key",
        "request_fingerprint",
        "list_id",
        "created_at",
    ]


def test_a_changed_request_with_a_consumed_key_is_refused_after_deletion(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    made = create_message_list(session, user, conversation, answer)

    sl.delete_list(session, user.id, made.shopping_list.id)

    with pytest.raises(sl.IdempotencyKeyReused):
        create_message_list(
            session, user, conversation, answer, item_states={0: "at_home"}
        )
    assert counts(session) == (0, 0)
    assert [row.list_id for row in requests(session)] == [None]


def test_a_new_key_creates_a_new_list_after_the_first_was_deleted(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    first = create_message_list(session, user, conversation, answer)
    sl.delete_list(session, user.id, first.shopping_list.id)

    second = create_message_list(
        session, user, conversation, answer, key="list-2"
    )

    assert second.created is True
    assert second.shopping_list.id != first.shopping_list.id
    assert counts(session) == (1, 3)
    assert len(sl.list_lists(session, user.id)) == 1
    # ... and the first key is still spent
    with pytest.raises(sl.ShoppingListDeleted):
        create_message_list(session, user, conversation, answer)


def test_two_users_may_consume_the_same_key_independently(session):
    first, second = new_user(session, display_name="A"), new_user(session, display_name="B")
    first_conversation, first_answer = answered(session, first)
    second_conversation, second_answer = answered(session, second)

    made_first = create_message_list(session, first, first_conversation, first_answer)
    made_second = create_message_list(session, second, second_conversation, second_answer)

    assert made_first.created is True and made_second.created is True
    assert made_first.shopping_list.id != made_second.shopping_list.id

    sl.delete_list(session, first.id, made_first.shopping_list.id)

    with pytest.raises(sl.ShoppingListDeleted):
        create_message_list(session, first, first_conversation, first_answer)
    replay = create_message_list(session, second, second_conversation, second_answer)
    assert replay.created is False
    assert replay.shopping_list.id == made_second.shopping_list.id


def test_a_rolled_back_creation_leaves_no_key_reservation(factory):
    with factory() as db:
        user = new_user(db)
        conversation, answer = answered(db, user)
        db.commit()
        user_id, conversation_id, answer_id = user.id, conversation.id, answer.id

    def retry(db):
        return create_message_list(
            db,
            db.get(User, user_id),
            db.get(Conversation, conversation_id),
            db.get(Message, answer_id),
        )

    with factory() as db:
        assert retry(db).created is True
        db.rollback()  # the transaction fails after the insert, as WebDatabase.run does

    with factory() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingList)) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(ShoppingListRequest)) == 0
        again = retry(db)
        assert again.created is True  # the key was never consumed
        db.commit()

    with factory() as db:
        assert counts(db) == (1, 3)
        assert len(requests(db)) == 1


def test_deleting_the_user_deletes_the_requests_including_tombstones(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    create_message_list(session, user, conversation, answer, key="kept")
    removed = create_message_list(session, user, conversation, answer, key="removed")
    sl.delete_list(session, user.id, removed.shopping_list.id)
    assert len(requests(session)) == 2  # one live reference and one tombstone

    session.execute(sa.delete(User).where(User.id == user.id))

    assert counts(session) == (0, 0)
    assert requests(session) == []


# --- independence from the source --------------------------------------------------------


def test_a_list_survives_deletion_of_the_conversation_it_was_copied_from(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    made = create_message_list(session, user, conversation, answer)

    session.execute(sa.delete(Message).where(Message.id == answer.id))
    session.execute(sa.delete(Conversation).where(Conversation.id == conversation.id))

    found = sl.get_list(session, user.id, made.shopping_list.id)
    assert [(item.name, item.amount) for item in found.items] == [
        ("свёкла", "2 шт."),
        ("капуста", "300 г"),
        ("говядина", "500 г"),
    ]
    assert found.source_message_id == answer.id  # a remembered value, not a key to a row


def test_a_list_survives_editing_and_deleting_its_source_favorite(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    favorite = structured_favorite(session, user, conversation, answer)
    fingerprint = sl.creation_fingerprint(
        source="favorite", favorite_id=favorite.id, expected_version=favorite.version
    )
    made = sl.create_list(
        session,
        user.id,
        source="favorite",
        favorite_id=favorite.id,
        expected_version=favorite.version,
        idempotency_key="fav-1",
        fingerprint=fingerprint,
        now=NOW,
    )
    before = [(item.name, item.amount) for item in made.shopping_list.items]

    fav.update_favorite(
        session,
        user.id,
        favorite.id,
        expected_version=favorite.version,
        changes={
            "title": "Овощной борщ",
            "ingredients": [{"name": "морковь", "amount": "1 шт."}],
        },
        now=NOW,
    )
    assert [
        (item.name, item.amount) for item in sl.get_list(session, user.id, made.shopping_list.id).items
    ] == before

    session.execute(sa.delete(FavoriteRecipe).where(FavoriteRecipe.id == favorite.id))
    found = sl.get_list(session, user.id, made.shopping_list.id)
    assert [(item.name, item.amount) for item in found.items] == before
    assert (found.source_favorite_id, found.source_favorite_version) == (
        favorite.id,
        favorite.version,
    )


# --- ownership and deletion -------------------------------------------------------------


def test_another_user_cannot_read_or_change_a_list_or_its_items(session):
    owner, other = new_user(session, display_name="A"), new_user(session, display_name="B")
    conversation, answer = answered(session, owner)
    made = create_message_list(session, owner, conversation, answer)
    list_id = made.shopping_list.id
    item_id = made.shopping_list.items[0].id

    with pytest.raises(sl.ShoppingListNotFound):
        sl.get_list(session, other.id, list_id)
    with pytest.raises(sl.ShoppingListNotFound):
        sl.add_item(session, other.id, list_id, name="соль", now=NOW)
    with pytest.raises(sl.ShoppingListNotFound):
        sl.update_item(
            session, other.id, list_id, item_id, changes={"state": "at_home"}, now=NOW
        )
    with pytest.raises(sl.ShoppingListNotFound):
        sl.delete_item(session, other.id, list_id, item_id, now=NOW)
    with pytest.raises(sl.ShoppingListNotFound):
        sl.set_status(session, other.id, list_id, status="completed", now=NOW)
    with pytest.raises(sl.ShoppingListNotFound):
        sl.delete_list(session, other.id, list_id)

    assert sl.list_lists(session, other.id) == []
    assert (sl.get_list(session, owner.id, list_id).status, counts(session)) == (
        "active",
        (1, 3),
    )


def test_an_item_of_another_list_is_not_found(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    first = create_message_list(session, user, conversation, answer, key="one")
    second = create_message_list(session, user, conversation, answer, key="two")
    item_id = first.shopping_list.items[0].id

    with pytest.raises(sl.ShoppingListItemNotFound):
        sl.update_item(
            session,
            user.id,
            second.shopping_list.id,
            item_id,
            changes={"state": "at_home"},
            now=NOW,
        )
    with pytest.raises(sl.ShoppingListItemNotFound):
        sl.delete_item(session, user.id, second.shopping_list.id, item_id, now=NOW)


def test_deleting_a_list_deletes_only_its_own_items(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    first = create_message_list(session, user, conversation, answer, key="one")
    second = create_message_list(session, user, conversation, answer, key="two")

    sl.delete_list(session, user.id, first.shopping_list.id)

    assert counts(session) == (1, 3)
    found = sl.get_list(session, user.id, second.shopping_list.id)
    assert found.id == second.shopping_list.id and len(found.items) == 3


def test_deleting_the_user_deletes_the_lists_and_their_items(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    create_message_list(session, user, conversation, answer)
    assert counts(session) == (1, 3)

    session.execute(sa.delete(User).where(User.id == user.id))

    assert counts(session) == (0, 0)


# --- limits ------------------------------------------------------------------------------


def _fill_lists(session, user, conversation, answer, number: int) -> None:
    for index in range(number):
        session.add(
            ShoppingList(
                user_id=user.id,
                title=f"Список {index}",
                status="active",
                source_kind="message",
                source_message_id=answer.id,
                source_recipe_index=0,
                idempotency_key=f"filler-{index}",
                request_fingerprint=digest(f"filler-{index}"),
                created_at=NOW,
                updated_at=NOW,
            )
        )
    session.flush()


def test_the_list_cap_is_one_hundred(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    _fill_lists(session, user, conversation, answer, 100)

    with pytest.raises(sl.ShoppingListsFull):
        create_message_list(session, user, conversation, answer)

    assert counts(session)[0] == 100


def test_a_deleted_list_frees_the_cap_but_not_its_key(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    made = create_message_list(session, user, conversation, answer)
    _fill_lists(session, user, conversation, answer, 99)  # 100 in total
    with pytest.raises(sl.ShoppingListsFull):
        create_message_list(session, user, conversation, answer, key="list-2")

    sl.delete_list(session, user.id, made.shopping_list.id)  # 99: the tombstone is not a list

    made_again = create_message_list(session, user, conversation, answer, key="list-2")
    assert made_again.created is True
    assert len(sl.list_lists(session, user.id)) == 100
    with pytest.raises(sl.ShoppingListDeleted):
        create_message_list(session, user, conversation, answer)  # "list-1" is still spent


def test_the_item_cap_is_two_hundred(session):
    user = new_user(session)
    conversation, answer = answered(session, user, OMELETTE)
    made = create_message_list(session, user, conversation, answer)
    list_id = made.shopping_list.id
    for position in range(1, 200):
        raw_insert(
            session,
            "shopping_list_items",
            id=uuid.uuid4().hex,
            list_id=list_id.hex,
            position=position,
            name=f"item {position}",
            state="unchecked",
            created_at=stored(NOW),
            updated_at=stored(NOW),
        )
    assert counts(session)[1] == 200

    with pytest.raises(sl.ShoppingListItemsFull):
        sl.add_item(session, user.id, list_id, name="ещё", now=NOW)


# --- validation and the database's own rules ---------------------------------------------


@pytest.mark.parametrize(
    "procedure",
    [
        lambda db, user, conversation, answer: create_message_list(
            db, user, conversation, answer, item_states={3: "at_home"}
        ),
        lambda db, user, conversation, answer: create_message_list(
            db, user, conversation, answer, item_states={0: "lost"}
        ),
        lambda db, user, conversation, answer: create_message_list(
            db, user, conversation, answer, recipe_index=MAX_RECIPES_PER_REPLY
        ),
        lambda db, user, conversation, answer: create_message_list(
            db, user, conversation, answer, key="not printable"
        ),
    ],
    ids=["position-out-of-range", "unknown-state", "unknown-recipe", "bad-key"],
)
def test_a_malformed_creation_is_refused_and_writes_nothing(
    session, procedure
):
    user = new_user(session)
    conversation, answer = answered(session, user)

    with pytest.raises(sl.InvalidShoppingList):
        procedure(session, user, conversation, answer)

    assert counts(session) == (0, 0)


def test_the_database_refuses_an_unknown_state_an_empty_name_and_duplicate_positions(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    made = create_message_list(session, user, conversation, answer)
    list_id = made.shopping_list.id

    with rejected(session, "ck_shopping_list_items_state_valid"):
        session.add(
            ShoppingListItem(
                list_id=list_id,
                position=10,
                name="соль",
                state="lost",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with rejected(session, "ck_shopping_list_items_name_not_empty"):
        session.add(
            ShoppingListItem(
                list_id=list_id,
                position=10,
                name="",
                state="unchecked",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with rejected(session, "shopping_list_items.list_id, shopping_list_items.position"):
        session.add(
            ShoppingListItem(
                list_id=list_id,
                position=0,
                name="соль",
                state="unchecked",
                created_at=NOW,
                updated_at=NOW,
            )
        )


def test_the_database_refuses_an_inconsistent_source_and_an_empty_title(session):
    user = new_user(session)
    with rejected(session, "ck_shopping_lists_source_consistent"):
        session.add(
            ShoppingList(
                user_id=user.id,
                title="Борщ",
                status="active",
                source_kind="message",
                source_favorite_id=user.id,
                source_favorite_version=1,
                idempotency_key="k-1",
                request_fingerprint=digest("k-1"),
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with rejected(session, "ck_shopping_lists_title_not_empty"):
        session.add(
            ShoppingList(
                user_id=user.id,
                title="",
                status="active",
                source_kind="message",
                source_message_id=1,
                source_recipe_index=0,
                idempotency_key="k-2",
                request_fingerprint=digest("k-2"),
                created_at=NOW,
                updated_at=NOW,
            )
        )


def test_the_database_refuses_a_second_reservation_of_one_key_for_one_user(session):
    user = new_user(session)
    conversation, answer = answered(session, user)
    create_message_list(session, user, conversation, answer)

    with rejected(
        session, "shopping_list_requests.user_id, shopping_list_requests.idempotency_key"
    ):
        session.add(
            ShoppingListRequest(
                user_id=user.id,
                idempotency_key="list-1",
                request_fingerprint=digest("another"),
                created_at=NOW,
            )
        )

    with rejected(session, "ck_shopping_list_requests_idempotency_key_printable_ascii"):
        session.add(
            ShoppingListRequest(
                user_id=user.id,
                idempotency_key="not printable",
                request_fingerprint=digest("another"),
                created_at=NOW,
            )
        )


def test_a_consumed_key_cannot_be_rewritten_through_the_orm(factory):
    with factory() as db, db.begin():
        user = new_user(db)
        conversation, answer = answered(db, user)
        made = create_message_list(db, user, conversation, answer)
        list_id = made.shopping_list.id
        stranger_id = new_user(db, display_name="Другой").id

    for field, value in (
        ("idempotency_key", "another"),
        ("request_fingerprint", digest("another")),
        ("user_id", stranger_id),
        ("list_id", None),
        ("created_at", NOW + timedelta(days=1)),
    ):
        with factory() as db:
            row = db.scalars(sa.select(ShoppingListRequest)).one()
            setattr(row, field, value)
            with pytest.raises(ValueError, match="cannot change"):
                db.flush()
            db.rollback()

    with factory() as db:
        row = db.scalars(sa.select(ShoppingListRequest)).one()
        assert (row.idempotency_key, row.list_id) == ("list-1", list_id)


def test_the_identity_of_a_saved_list_cannot_be_rewritten_through_the_orm(factory):
    with factory() as db, db.begin():
        user = new_user(db)
        stranger = new_user(db, display_name="Другой")
        conversation, answer = answered(db, user)
        made = create_message_list(db, user, conversation, answer)
        list_id = made.shopping_list.id
        stranger_id = stranger.id

    for field, value in (
        ("user_id", stranger_id),
        ("title", "Другое"),
        ("source_kind", "favorite"),
        ("idempotency_key", "another"),
        ("request_fingerprint", digest("another")),
        ("created_at", NOW + timedelta(days=1)),
    ):
        with factory() as db:
            row = db.get(ShoppingList, list_id)
            setattr(row, field, value)
            with pytest.raises(ValueError, match="cannot change"):
                db.flush()
            db.rollback()

    with factory() as db:
        assert db.get(ShoppingList, list_id).title == "Борщ"


def test_an_items_place_and_list_cannot_be_rewritten_through_the_orm(factory):
    with factory() as db, db.begin():
        user = new_user(db)
        conversation, answer = answered(db, user)
        first = create_message_list(db, user, conversation, answer, key="one")
        second = create_message_list(db, user, conversation, answer, key="two")
        item_id = first.shopping_list.items[0].id
        second_id = second.shopping_list.id

    for field, value in (("position", 99), ("list_id", second_id)):
        with factory() as db:
            item = db.get(ShoppingListItem, item_id)
            setattr(item, field, value)
            with pytest.raises(ValueError, match="cannot change"):
                db.flush()
            db.rollback()

    with factory() as db:
        assert db.get(ShoppingListItem, item_id).position == 0
