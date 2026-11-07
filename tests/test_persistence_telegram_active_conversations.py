"""The Telegram conversation cursor: the selection row, its three states, and the rules a
Telegram message and a /new command follow from it.

The functions under test are persistence.telegram. Every test that uses the `engine`
fixture runs twice (tests/persistence_support.py): on a schema created from the models and
on one built by the migrations. Retention is the one exception: it needs a database at the
repository's head, so it builds a migrated one itself.

What is pinned here, in the order the product states it:

  * no row: the legacy behaviour, the latest conversation; a message makes no selection;
  * a row with a conversation: messages and /new use it, the Web-to-Telegram copy narrows
    to it, and selecting it writes no conversation activity time;
  * a row with NULL: the selected conversation was deleted (the database's ON DELETE SET
    NULL); the next message creates a new conversation and selects it, never another one;
  * a redelivered update goes to its own conversation before the cursor is consulted;
  * ownership is checked in persistence, so a cursor naming somebody else's conversation
    (possible only by raw SQL) is repaired, not followed;
  * two users' cursors are independent;
  * unlinking removes the cursor, and the retention run neither deletes it nor is confused
    by it.
"""

from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.admin.retention import purge_expired
from house_helper.persistence import conversations as cv
from house_helper.persistence import telegram as tg
from house_helper.persistence.database import (
    create_db_engine,
    create_session_factory,
    upgrade_database,
)
from house_helper.persistence.models import (
    Conversation,
    TelegramActiveConversation,
    TelegramUpdateAdmission,
    User,
)
from tests.persistence_support import (  # noqa: F401  (fixtures)
    engine,
    factory,
    new_conversation,
    new_message,
    new_turn_request,
    new_user,
    session,
    utc,
)

NOW = utc(2026, 10, 28, 12)
ACCOUNT = 555000111
OTHER_ACCOUNT = 555000222
BOT = 123456789


def run(factory, work):
    with factory() as db, db.begin():
        return work(db)


def selection_rows(factory):
    with factory() as db:
        return list(db.scalars(sa.select(TelegramActiveConversation)))


def enroll(db, **fields) -> User:
    user = new_user(db, **fields)
    # (Not needed by these functions, but a user is always provisioned in the product;
    # keeping the helper close to tests/test_persistence_telegram.py makes the intent clear.)
    return user


@pytest.fixture
def user_id(factory):
    return run(factory, lambda db: enroll(db).id)


@pytest.fixture
def other_user_id(factory):
    return run(factory, lambda db: enroll(db).id)


def make_conversation(factory, user_id, *, created=NOW, updated=NOW) -> Conversation:
    def make(db):
        return new_conversation(
            db, db.get(User, user_id), created_at=created, updated_at=updated
        )

    return run(factory, make)


# --- the three states --------------------------------------------------------------------


def test_no_row_is_the_legacy_state_and_a_plain_read_writes_nothing(factory, user_id):
    first = make_conversation(factory, user_id)
    second = make_conversation(factory, user_id, updated=NOW + timedelta(hours=1))

    assert run(factory, lambda db: tg.active_conversation(db, user_id)) is None
    # With no row, every conversation is copied to Telegram (the old behaviour).
    assert run(factory, lambda db: tg.mirrors_to_telegram(db, user_id, first.id)) is True
    assert run(factory, lambda db: tg.mirrors_to_telegram(db, user_id, second.id)) is True
    assert selection_rows(factory) == []  # the reads created nothing


def test_selecting_a_conversation_creates_the_row_and_narrows_the_copy(factory, user_id):
    first = make_conversation(factory, user_id)
    second = make_conversation(factory, user_id)

    summary = run(factory, lambda db: tg.select_conversation(db, user_id, first.id, now=NOW))

    assert summary is not None and summary.id == first.id
    [row] = selection_rows(factory)
    assert (row.user_id, row.conversation_id, row.selected_at) == (user_id, first.id, NOW)
    assert run(factory, lambda db: tg.active_conversation(db, user_id)) == tg.ActiveConversation(
        first.id, NOW
    )
    assert run(factory, lambda db: tg.mirrors_to_telegram(db, user_id, first.id)) is True
    assert run(factory, lambda db: tg.mirrors_to_telegram(db, user_id, second.id)) is False


def test_selecting_again_switches_the_cursor_and_its_time(factory, user_id):
    first = make_conversation(factory, user_id)
    second = make_conversation(factory, user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, first.id, now=NOW))

    run(
        factory,
        lambda db: tg.select_conversation(
            db, user_id, second.id, now=NOW + timedelta(minutes=5)
        ),
    )

    [row] = selection_rows(factory)
    assert row.conversation_id == second.id and row.selected_at == NOW + timedelta(minutes=5)
    assert run(factory, lambda db: tg.mirrors_to_telegram(db, user_id, first.id)) is False


def test_selecting_the_same_conversation_again_is_one_row(factory, user_id):
    conversation = make_conversation(factory, user_id)

    run(factory, lambda db: tg.select_conversation(db, user_id, conversation.id, now=NOW))
    run(
        factory,
        lambda db: tg.select_conversation(
            db, user_id, conversation.id, now=NOW + timedelta(minutes=1)
        ),
    )

    [row] = selection_rows(factory)
    assert row.conversation_id == conversation.id and row.selected_at == NOW + timedelta(minutes=1)


def test_selecting_writes_no_conversation_activity_time(factory, user_id):
    old = make_conversation(factory, user_id, created=NOW - timedelta(days=2), updated=NOW - timedelta(days=1))
    before = run(factory, lambda db: db.get(Conversation, old.id).updated_at)

    run(
        factory,
        lambda db: tg.select_conversation(db, user_id, old.id, now=NOW + timedelta(days=10)),
    )

    assert run(factory, lambda db: db.get(Conversation, old.id).updated_at) == before


def test_selecting_an_unknown_conversation_writes_nothing(factory, user_id):
    existing = make_conversation(factory, user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, existing.id, now=NOW))

    import uuid

    assert run(
        factory, lambda db: tg.select_conversation(db, user_id, uuid.uuid4(), now=NOW)
    ) is None
    [row] = selection_rows(factory)
    assert row.conversation_id == existing.id  # the valid selection is untouched


def test_a_selection_can_only_name_the_users_own_conversation(factory, user_id, other_user_id):
    theirs = make_conversation(factory, other_user_id)

    assert run(
        factory, lambda db: tg.select_conversation(db, user_id, theirs.id, now=NOW)
    ) is None
    assert selection_rows(factory) == []


def test_the_database_refuses_a_selection_of_a_conversation_that_does_not_exist(session, user_id):
    import uuid

    session.add(TelegramActiveConversation(user_id=user_id, conversation_id=uuid.uuid4(), selected_at=NOW))
    with pytest.raises(sa.exc.IntegrityError):
        session.flush()


def test_deleting_the_selected_conversation_leaves_the_null_state(factory, user_id):
    conversation = make_conversation(factory, user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, conversation.id, now=NOW))

    run(factory, lambda db: cv.delete_conversation(db, user_id, conversation.id))

    [row] = selection_rows(factory)
    assert row.conversation_id is None
    assert run(factory, lambda db: tg.active_conversation(db, user_id)) == tg.ActiveConversation(
        None, NOW
    )
    # A row that names no conversation copies nothing (the explicit "it is gone" state).
    assert run(factory, lambda db: tg.mirrors_to_telegram(db, user_id, conversation.id)) is False


def test_deleting_the_user_deletes_the_selection(factory, user_id):
    conversation = make_conversation(factory, user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, conversation.id, now=NOW))

    run(factory, lambda db: db.execute(sa.delete(User).where(User.id == user_id)))

    assert selection_rows(factory) == []


# --- the message's conversation ------------------------------------------------------------


def test_a_message_without_a_selection_follows_the_latest_and_makes_no_selection(factory, user_id):
    old = make_conversation(factory, user_id, updated=NOW)
    latest = make_conversation(factory, user_id, updated=NOW + timedelta(hours=1))

    found = run(factory, lambda db: tg.conversation_for_message(db, user_id, "tg-1", now=NOW))

    assert found.conversation_id == latest.id
    assert found.admitted is False  # a message never seen before
    assert selection_rows(factory) == []  # legacy: no cursor is created


def test_a_message_with_a_selection_goes_there_even_if_another_is_newer(factory, user_id):
    selected = make_conversation(factory, user_id, updated=NOW)
    newer = make_conversation(factory, user_id, updated=NOW + timedelta(hours=5))
    run(factory, lambda db: tg.select_conversation(db, user_id, selected.id, now=NOW))

    found = run(factory, lambda db: tg.conversation_for_message(db, user_id, "tg-1", now=NOW))
    assert found.conversation_id == selected.id and found.admitted is False
    assert selected.id != newer.id


def test_a_redelivery_resolves_to_its_own_conversation_before_the_selection(factory, user_id):
    first = make_conversation(factory, user_id, updated=NOW)
    second = make_conversation(factory, user_id, updated=NOW + timedelta(hours=1))
    run(factory, lambda db: new_turn_request(db, first, "tg-77"))
    run(factory, lambda db: tg.select_conversation(db, user_id, second.id, now=NOW))

    found = run(factory, lambda db: tg.conversation_for_message(db, user_id, "tg-77", now=NOW))

    assert found.conversation_id == first.id
    assert found.admitted is True  # a redelivery: its earlier turn request was found


def test_a_redelivery_whose_conversation_was_deleted_is_refused_by_the_ledger(factory, user_id):
    """The turn request is cascaded with the conversation. The admission ledger's marker was
    written in the claim's transaction and survives both, so the resolver refuses the
    redelivery before it creates or selects anything (the blocker KH-TG-DLG-01 closes; the
    marker is tested on its own in tests/test_persistence_telegram_admissions.py)."""
    first = make_conversation(factory, user_id)
    run(factory, lambda db: new_turn_request(db, first, "tg-77"))
    run(
        factory,
        lambda db: db.add(
            TelegramUpdateAdmission(
                bot_id=BOT,
                update_id=77,
                kind="update",
                admitted_at=NOW,
                expires_at=NOW + timedelta(hours=48),
            )
        ),
    )
    run(factory, lambda db: cv.delete_conversation(db, user_id, first.id))

    with pytest.raises(tg.UpdateAlreadyAdmitted):
        run(
            factory,
            lambda db: tg.conversation_for_message(
                db, user_id, "tg-77", now=NOW, bot_id=BOT, update_id=77
            ),
        )

    assert run(factory, lambda db: list(db.scalars(sa.select(Conversation)))) == []


def test_a_message_with_no_conversations_creates_one_and_makes_no_selection(factory, user_id):
    found = run(factory, lambda db: tg.conversation_for_message(db, user_id, "tg-1", now=NOW))

    assert run(factory, lambda db: db.get(Conversation, found.conversation_id).user_id) == user_id
    assert found.admitted is False
    assert selection_rows(factory) == []


def test_a_message_after_the_selected_conversation_was_deleted_creates_and_selects_one(factory, user_id):
    gone = make_conversation(factory, user_id)
    bystander = make_conversation(factory, user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, gone.id, now=NOW))
    run(factory, lambda db: cv.delete_conversation(db, user_id, gone.id))

    found = run(factory, lambda db: tg.conversation_for_message(db, user_id, "tg-1", now=NOW))

    assert found.conversation_id not in (gone.id, bystander.id)  # never the unrelated conversation
    [row] = selection_rows(factory)
    assert row.conversation_id == found.conversation_id  # the new conversation is selected


def test_a_cursor_naming_somebody_elses_conversation_is_repaired_not_followed(factory, user_id, other_user_id):
    mine = make_conversation(factory, user_id)
    theirs = make_conversation(factory, other_user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, mine.id, now=NOW))
    # Only raw SQL can put a foreign id in the row: the simple foreign key checks existence,
    # not ownership, which is why persistence checks it on every resolution.
    run(
        factory,
        lambda db: db.execute(
            sa.update(TelegramActiveConversation)
            .where(TelegramActiveConversation.user_id == user_id)
            .values(conversation_id=theirs.id)
        ),
    )

    found = run(factory, lambda db: tg.conversation_for_message(db, user_id, "tg-1", now=NOW))

    assert found.conversation_id != theirs.id
    [row] = selection_rows(factory)
    assert row.conversation_id == found.conversation_id


# --- /new ----------------------------------------------------------------------------------


def test_new_reuses_an_empty_selected_conversation_and_selects_it(factory, user_id):
    first = run(factory, lambda db: tg.start_conversation(db, user_id, now=NOW))
    second = run(factory, lambda db: tg.start_conversation(db, user_id, now=NOW + timedelta(minutes=1)))

    assert first == second
    assert run(factory, lambda db: len(list(db.scalars(sa.select(Conversation))))) == 1
    [row] = selection_rows(factory)
    assert row.conversation_id == first and row.selected_at == NOW  # reused, not re-selected


def test_new_creates_and_selects_a_conversation_when_the_selected_one_is_not_empty(factory, user_id):
    def make(db):
        conversation = new_conversation(db, db.get(User, user_id), created_at=NOW, updated_at=NOW)
        new_message(db, conversation, content="привет", created_at=NOW)
        tg.select_conversation(db, user_id, conversation.id, now=NOW)
        return conversation.id

    old = run(factory, make)
    created = run(factory, lambda db: tg.start_conversation(db, user_id, now=NOW + timedelta(minutes=1)))

    assert created != old
    [row] = selection_rows(factory)
    assert row.conversation_id == created


def test_new_without_a_selection_reuses_an_empty_latest_and_selects_it(factory, user_id):
    older = make_conversation(factory, user_id, updated=NOW)
    latest = make_conversation(factory, user_id, updated=NOW + timedelta(hours=1))

    created = run(factory, lambda db: tg.start_conversation(db, user_id, now=NOW + timedelta(minutes=1)))

    assert created == latest.id and created != older.id
    [row] = selection_rows(factory)
    assert row.conversation_id == latest.id


def test_new_without_a_selection_creates_one_when_the_latest_is_not_empty(factory, user_id):
    def make(db):
        conversation = new_conversation(db, db.get(User, user_id), created_at=NOW, updated_at=NOW)
        new_message(db, conversation, content="привет", created_at=NOW)
        return conversation.id

    old = run(factory, make)
    created = run(factory, lambda db: tg.start_conversation(db, user_id, now=NOW + timedelta(minutes=1)))

    assert created != old
    [row] = selection_rows(factory)
    assert row.conversation_id == created


def test_new_after_the_selected_conversation_was_deleted_creates_and_selects_one(factory, user_id):
    gone = make_conversation(factory, user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, gone.id, now=NOW))
    run(factory, lambda db: cv.delete_conversation(db, user_id, gone.id))
    assert run(factory, lambda db: tg.active_conversation(db, user_id)).conversation_id is None

    created = run(factory, lambda db: tg.start_conversation(db, user_id, now=NOW))

    [row] = selection_rows(factory)
    assert row.conversation_id == created


def test_new_writes_no_activity_time_of_the_reused_conversation(factory, user_id):
    latest = make_conversation(factory, user_id, updated=NOW)
    before = run(factory, lambda db: db.get(Conversation, latest.id).updated_at)

    run(factory, lambda db: tg.start_conversation(db, user_id, now=NOW + timedelta(days=3)))

    assert run(factory, lambda db: db.get(Conversation, latest.id).updated_at) == before


# --- two people -----------------------------------------------------------------------------


def test_two_users_cursors_are_independent(factory, user_id, other_user_id):
    mine = make_conversation(factory, user_id)
    other = make_conversation(factory, other_user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, mine.id, now=NOW))

    assert run(factory, lambda db: tg.active_conversation(db, other_user_id)) is None
    assert run(factory, lambda db: tg.mirrors_to_telegram(db, other_user_id, other.id)) is True
    found = run(factory, lambda db: tg.conversation_for_message(db, other_user_id, "tg-1", now=NOW))
    assert found.conversation_id == other.id
    assert run(factory, lambda db: tg.active_conversation(db, user_id)).conversation_id == mine.id


# --- unlinking ---------------------------------------------------------------------------------


def test_a_selection_from_an_account_that_is_not_the_users_is_refused(factory, user_id):
    from house_helper.persistence import allowlist
    from house_helper.persistence.models import TelegramLink

    first = make_conversation(factory, user_id)
    second = make_conversation(factory, user_id)

    def make(db):
        user = db.get(User, user_id)
        allowlist.allow(db, user, "telegram", "sub-1")
        db.add(TelegramLink(user_id=user_id, telegram_user_id=ACCOUNT, created_at=NOW))

    run(factory, make)

    assert run(
        factory,
        lambda db: tg.select_conversation(
            db, user_id, first.id, now=NOW, telegram_user_id=ACCOUNT
        ),
    ) is not None
    # A click authorized as another account writes nothing and switches nothing.
    assert run(
        factory,
        lambda db: tg.select_conversation(
            db, user_id, second.id, now=NOW, telegram_user_id=OTHER_ACCOUNT
        ),
    ) is None
    [row] = selection_rows(factory)
    assert row.conversation_id == first.id

    # ... and neither does one that arrives after the unlink it raced.
    run(factory, lambda db: tg.unlink(db, user_id, telegram_user_id=ACCOUNT))
    assert run(
        factory,
        lambda db: tg.select_conversation(
            db, user_id, second.id, now=NOW, telegram_user_id=ACCOUNT
        ),
    ) is None
    assert selection_rows(factory) == []


def test_a_resolution_from_an_account_that_is_not_the_users_writes_no_cursor(factory, user_id):
    """The binding is checked inside the resolution's own transaction, with the link held
    (the same rule select_conversation and start_conversation_for follow). A resolution
    that would create and select a conversation -- here because the selected one was
    deleted -- writes nothing when the account is somebody else's or when the link is
    gone; otherwise an unlink racing the resolution could leave a cursor for no link."""
    from house_helper.persistence import allowlist
    from house_helper.persistence.models import TelegramLink

    gone = make_conversation(factory, user_id)
    bystander = make_conversation(factory, user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, gone.id, now=NOW))
    run(factory, lambda db: cv.delete_conversation(db, user_id, gone.id))
    assert run(factory, lambda db: tg.active_conversation(db, user_id)).conversation_id is None

    def make(db):
        user = db.get(User, user_id)
        allowlist.allow(db, user, "telegram", "sub-1")
        db.add(TelegramLink(user_id=user_id, telegram_user_id=ACCOUNT, created_at=NOW))

    run(factory, make)

    assert run(
        factory,
        lambda db: tg.conversation_for_message(
            db, user_id, "tg-1", now=NOW, telegram_user_id=OTHER_ACCOUNT
        ),
    ) is None
    [row] = selection_rows(factory)
    assert row.conversation_id is None  # nothing was created or selected

    found = run(
        factory,
        lambda db: tg.conversation_for_message(
            db, user_id, "tg-1", now=NOW, telegram_user_id=ACCOUNT
        ),
    )
    assert found is not None and found.conversation_id not in (gone.id, bystander.id)
    [row] = selection_rows(factory)
    assert row.conversation_id == found.conversation_id

    # ... and a resolution that arrives after the unlink it raced writes nothing.
    run(factory, lambda db: tg.unlink(db, user_id, telegram_user_id=ACCOUNT))
    assert run(
        factory,
        lambda db: tg.conversation_for_message(
            db, user_id, "tg-2", now=NOW, telegram_user_id=ACCOUNT
        ),
    ) is None
    assert selection_rows(factory) == []


def test_unlinking_removes_the_cursor_with_the_link(factory, user_id, other_user_id):
    from house_helper.persistence import allowlist
    from house_helper.persistence.models import TelegramLink

    def make(db):
        user = db.get(User, user_id)
        allowlist.allow(db, user, "telegram", "sub-1")
        db.add(TelegramLink(user_id=user_id, telegram_user_id=555000111, created_at=NOW))
        conversation = new_conversation(db, user, created_at=NOW, updated_at=NOW)
        tg.select_conversation(db, user_id, conversation.id, now=NOW)
        return conversation.id

    run(factory, make)

    assert run(factory, lambda db: tg.unlink(db, user_id, telegram_user_id=555000111)) is True
    assert selection_rows(factory) == []  # a new link starts in the legacy state


def test_unlinking_another_account_keeps_the_cursor(factory, user_id):
    from house_helper.persistence import allowlist
    from house_helper.persistence.models import TelegramLink

    def make(db):
        user = db.get(User, user_id)
        allowlist.allow(db, user, "telegram", "sub-1")
        db.add(TelegramLink(user_id=user_id, telegram_user_id=555000111, created_at=NOW))
        conversation = new_conversation(db, user, created_at=NOW, updated_at=NOW)
        tg.select_conversation(db, user_id, conversation.id, now=NOW)
        return conversation.id

    conversation = run(factory, make)

    assert run(factory, lambda db: tg.unlink(db, user_id, telegram_user_id=555000222)) is False
    [row] = selection_rows(factory)
    assert row.conversation_id == conversation


def test_a_stale_cursor_is_removed_only_when_there_is_no_link(factory, user_id):
    conversation = make_conversation(factory, user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, conversation.id, now=NOW))

    assert run(factory, lambda db: tg.withdraw_codes_unless_linked(db, user_id)) is True
    assert selection_rows(factory) == []


# --- retention ---------------------------------------------------------------------------------


def test_the_retention_run_leaves_the_cursor_and_merely_nulls_a_deleted_conversation(tmp_path):
    """The new table has no deadline and is not named by the purge; deleting the old empty
    conversation it points at is the database's ON DELETE SET NULL, not a second cursor
    rule. The row stays as the explicit "selected, then deleted" state."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'retention_active.db'}")
    try:
        upgrade_database(engine)
        factory = create_session_factory(engine)
        old = NOW - timedelta(days=400)

        with factory() as db, db.begin():
            user = new_user(db, created_at=old, updated_at=old)
            conversation = new_conversation(
                db, user, created_at=old, updated_at=old
            )
            tg.select_conversation(db, user.id, conversation.id, now=old)
            user_id, conversation_id = user.id, conversation.id

        report = purge_expired(engine, now=NOW)

        assert report.deleted["conversations"] == 1
        with factory() as db:
            [row] = list(db.scalars(sa.select(TelegramActiveConversation)))
            assert row.user_id == user_id and row.conversation_id is None
            assert db.get(Conversation, conversation_id) is None
    finally:
        engine.dispose()
