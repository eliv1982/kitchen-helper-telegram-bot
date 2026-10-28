"""Linking a Telegram account to a user: pairing codes, the link, and the conversation a
Telegram message belongs to.

The functions under test are persistence.telegram. Every test runs twice
(tests/persistence_support.py): on a schema created from the models and on one built by the
migrations.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from house_helper.persistence import allowlist
from house_helper.persistence import telegram as tg
from house_helper.persistence.authn import hash_secret
from house_helper.persistence.identifiers import TELEGRAM_UPDATE_KEY_PREFIX
from house_helper.persistence.models import (
    AllowlistEntry,
    Conversation,
    TelegramLink,
    TelegramLinkCode,
    User,
)
from tests.persistence_support import (  # noqa: F401  (fixtures)
    engine,
    factory,
    new_conversation,
    new_message,
    new_turn_request,
    new_user,
    rejected,
    session,
    utc,
)

NOW = utc(2026, 10, 28, 12)
ACCOUNT = 987654321  # what the Bot API says a sender's id is: nothing like a sign-in `sub`
OTHER_ACCOUNT = 123456789


def run(factory, work):
    with factory() as db, db.begin():
        return work(db)


def enroll(db, **fields) -> User:
    """A provisioned user: one way in, not revoked."""
    user = new_user(db, **fields)
    allowlist.allow(db, user, "telegram", f"sub-{uuid4().hex[:12]}")
    return user


@pytest.fixture
def user_id(factory):
    return run(factory, lambda db: enroll(db).id)


@pytest.fixture
def other_user_id(factory):
    return run(factory, lambda db: enroll(db).id)


def issue(factory, user, *, now=NOW):
    return run(factory, lambda db: tg.issue_link_code(db, user, now=now))


def present(factory, code, account=ACCOUNT, name="@mama", *, now=NOW):
    return run(factory, lambda db: tg.consume_link_code(db, code, account, name, now=now))


def link_of(factory, user):
    return run(factory, lambda db: tg.get_link(db, user))


def rows(factory, model):
    with factory() as db:
        return list(db.scalars(sa.select(model)))


# --- the code ---------------------------------------------------------------------------


def test_a_code_is_random_url_safe_and_fits_the_start_payload():
    codes = {tg.new_code() for _ in range(50)}

    assert len(codes) == 50
    assert all(tg.is_code(code) and len(code) == 32 for code in codes)
    assert len(next(iter(codes))) <= 64  # the longest /start payload Telegram allows


@pytest.mark.parametrize(
    "value",
    [None, "", "short", "x" * 31, "x" * 33, "x" * 31 + " ", "x" * 31 + "\n", "é" * 32, "x" * 31 + "+", 12, b"x" * 32],
)
def test_anything_that_is_not_the_shape_of_a_code_is_not_one(value):
    assert tg.is_code(value) is False


def test_the_secret_is_stored_only_as_its_digest(factory, user_id):
    issued = issue(factory, user_id)

    [row] = rows(factory, TelegramLinkCode)
    assert row.code_hash == hash_secret(issued.code)
    assert issued.code not in repr((row.code_hash, row.user_id, row.created_at, row.expires_at))
    with factory() as db:
        dump = repr(db.execute(sa.text("SELECT * FROM telegram_link_codes")).all())
    assert issued.code not in dump


def test_a_code_lives_ten_minutes(factory, user_id):
    issued = issue(factory, user_id)

    assert issued.expires_at == NOW + timedelta(minutes=10)
    assert tg.CODE_LIFETIME == timedelta(minutes=10)


# --- linking ----------------------------------------------------------------------------


def test_presenting_the_code_links_the_account_to_the_user_it_was_issued_for(factory, user_id):
    issued = issue(factory, user_id)

    result = present(factory, issued.code)

    assert result == tg.LinkResult(tg.LinkOutcome.LINKED, user_id)
    link = link_of(factory, user_id)
    assert (link.telegram_user_id, link.display_name, link.created_at) == (ACCOUNT, "@mama", NOW)
    assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, ACCOUNT)) == user_id
    assert run(factory, lambda db: tg.chat_of_user(db, user_id)) == ACCOUNT


def test_the_account_never_says_whose_it_is_so_the_code_alone_decides_the_user(
    factory, user_id, other_user_id
):
    mine = issue(factory, user_id)
    issue(factory, other_user_id)

    result = present(factory, mine.code)

    assert result.user_id == user_id
    assert link_of(factory, other_user_id) is None


def test_a_code_works_once(factory, user_id):
    issued = issue(factory, user_id)
    assert present(factory, issued.code).outcome is tg.LinkOutcome.LINKED
    run(factory, lambda db: tg.unlink(db, user_id))

    again = present(factory, issued.code)

    assert again == tg.LinkResult(tg.LinkOutcome.INVALID_CODE)
    assert link_of(factory, user_id) is None


def test_two_accounts_racing_for_one_code_get_one_link(factory, user_id):
    issued = issue(factory, user_id)

    first = present(factory, issued.code, ACCOUNT)
    second = present(factory, issued.code, OTHER_ACCOUNT)

    assert (first.outcome, second.outcome) == (tg.LinkOutcome.LINKED, tg.LinkOutcome.INVALID_CODE)
    assert link_of(factory, user_id).telegram_user_id == ACCOUNT


def test_a_code_is_dead_at_its_deadline_and_alive_a_moment_before(factory, user_id, other_user_id):
    late = issue(factory, user_id)
    punctual = issue(factory, other_user_id)

    at_deadline = present(factory, late.code, ACCOUNT, now=late.expires_at)
    just_before = present(
        factory, punctual.code, OTHER_ACCOUNT, now=punctual.expires_at - timedelta(microseconds=1)
    )

    assert at_deadline.outcome is tg.LinkOutcome.INVALID_CODE
    assert just_before.outcome is tg.LinkOutcome.LINKED
    assert link_of(factory, user_id) is None


@pytest.mark.parametrize("code", [None, "", "nope", "A" * 32, "A" * 31, 5])
def test_an_unknown_or_malformed_code_changes_nothing(factory, user_id, code):
    issue(factory, user_id)
    before = [(r.code_hash, r.consumed_at) for r in rows(factory, TelegramLinkCode)]

    result = present(factory, code)

    assert result == tg.LinkResult(tg.LinkOutcome.INVALID_CODE)
    assert [(r.code_hash, r.consumed_at) for r in rows(factory, TelegramLinkCode)] == before
    assert rows(factory, TelegramLink) == []


@pytest.mark.parametrize("account", [0, -1, -1001234567890, "7", None, 1.5, True])
def test_only_a_positive_integer_id_can_be_linked(factory, user_id, account):
    issued = issue(factory, user_id)

    result = present(factory, issued.code, account)

    assert result == tg.LinkResult(tg.LinkOutcome.INVALID_CODE)
    assert rows(factory, TelegramLink) == []
    [row] = rows(factory, TelegramLinkCode)
    assert row.consumed_at is None  # a request that was never a person's does not spend the code


def test_asking_for_a_new_code_withdraws_the_old_one(factory, user_id):
    old = issue(factory, user_id)
    new = issue(factory, user_id, now=NOW + timedelta(minutes=1))

    assert present(factory, old.code, now=NOW + timedelta(minutes=2)).outcome is tg.LinkOutcome.INVALID_CODE
    assert present(factory, new.code, now=NOW + timedelta(minutes=2)).outcome is tg.LinkOutcome.LINKED
    assert len(rows(factory, TelegramLinkCode)) == 1


def test_a_user_with_an_account_linked_gets_no_code_until_they_unlink(factory, user_id):
    present(factory, issue(factory, user_id).code)

    with pytest.raises(tg.AlreadyLinked):
        issue(factory, user_id)
    assert run(factory, lambda db: tg.unlink(db, user_id)) is True
    assert issue(factory, user_id).code


def test_a_refused_request_for_a_code_takes_nothing_back(factory, user_id):
    old = issue(factory, user_id)
    present(factory, old.code)  # linked now
    spare = run(factory, lambda db: tg.new_code())
    run(
        factory,
        lambda db: db.add(
            TelegramLinkCode(
                user_id=user_id, code_hash=hash_secret(spare), created_at=NOW, expires_at=NOW + tg.CODE_LIFETIME
            )
        ),
    )

    with pytest.raises(tg.AlreadyLinked):
        issue(factory, user_id)

    # the transaction rolled back, the withdrawal of the outstanding code with it
    assert hash_secret(spare) in {r.code_hash for r in rows(factory, TelegramLinkCode)}


def test_a_disabled_user_or_one_with_no_way_in_gets_no_code(factory):
    disabled = run(factory, lambda db: enroll(db, disabled_at=NOW).id)
    locked_out = run(factory, lambda db: enroll(db).id)
    run(
        factory,
        lambda db: [setattr(e, "revoked_at", NOW) for e in db.scalars(sa.select(AllowlistEntry).where(AllowlistEntry.user_id == locked_out))],
    )
    stranger = run(factory, lambda db: new_user(db).id)  # provisioned with nothing

    for user in (disabled, locked_out, stranger, uuid4()):
        with pytest.raises(tg.UserUnavailable):
            issue(factory, user)
    assert rows(factory, TelegramLinkCode) == []


def test_a_user_who_lost_access_after_asking_cannot_complete_the_link_and_the_code_is_spent(
    factory, user_id
):
    issued = issue(factory, user_id)
    run(factory, lambda db: setattr(db.get(User, user_id), "disabled_at", NOW))

    result = present(factory, issued.code)

    assert result == tg.LinkResult(tg.LinkOutcome.ACCOUNT_UNAVAILABLE)
    assert rows(factory, TelegramLink) == []
    assert rows(factory, TelegramLinkCode)[0].consumed_at == NOW


def test_an_account_that_belongs_to_somebody_else_cannot_be_taken_and_the_code_is_spent(
    factory, user_id, other_user_id
):
    present(factory, issue(factory, other_user_id).code, ACCOUNT)
    mine = issue(factory, user_id)

    result = present(factory, mine.code, ACCOUNT)

    assert result == tg.LinkResult(tg.LinkOutcome.TELEGRAM_TAKEN)
    assert link_of(factory, user_id) is None
    assert link_of(factory, other_user_id).telegram_user_id == ACCOUNT
    assert present(factory, mine.code, OTHER_ACCOUNT).outcome is tg.LinkOutcome.INVALID_CODE


def test_a_second_account_cannot_replace_the_first_without_unlinking(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)
    spare = tg.new_code()
    run(
        factory,
        lambda db: db.add(
            TelegramLinkCode(
                user_id=user_id, code_hash=hash_secret(spare), created_at=NOW, expires_at=NOW + tg.CODE_LIFETIME
            )
        ),
    )

    result = present(factory, spare, OTHER_ACCOUNT)

    assert result == tg.LinkResult(tg.LinkOutcome.USER_HAS_ANOTHER)
    assert link_of(factory, user_id).telegram_user_id == ACCOUNT


def test_presenting_a_code_from_the_account_that_is_already_linked_is_not_an_error(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)
    spare = tg.new_code()
    run(
        factory,
        lambda db: db.add(
            TelegramLinkCode(
                user_id=user_id, code_hash=hash_secret(spare), created_at=NOW, expires_at=NOW + tg.CODE_LIFETIME
            )
        ),
    )

    assert present(factory, spare, ACCOUNT) == tg.LinkResult(tg.LinkOutcome.ALREADY_LINKED, user_id)
    assert len(rows(factory, TelegramLink)) == 1


# --- what the name may be ----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("@mama", "@mama"),
        ("  Елена \t Петровна\n", "Елена Петровна"),
        ("a\x00b\x07c", "a b c"),
        ("‮⁦evil", "evil"),
        ("", None),
        ("   \n ", None),
        (None, None),
        (42, None),
        ("я" * 300, "я" * 100),
    ],
)
def test_a_display_name_is_made_fit_to_keep_and_to_show(raw, expected):
    cleaned = tg.clean_display_name(raw)

    assert cleaned == expected
    assert cleaned is None or (len(cleaned) <= 100 and cleaned.isprintable())


def test_a_name_is_stored_cleaned(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT, "  Елена\n\x00Петровна ")

    assert link_of(factory, user_id).display_name == "Елена Петровна"


# --- who the bot serves --------------------------------------------------------------------


def test_an_unknown_sender_is_nobody(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)

    for sender in (OTHER_ACCOUNT, 0, -ACCOUNT, "987654321", None, 1.0, True):
        assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, sender)) is None


def test_every_sender_is_resolved_to_their_own_user_only(factory, user_id, other_user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)
    present(factory, issue(factory, other_user_id).code, OTHER_ACCOUNT)

    assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, ACCOUNT)) == user_id
    assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, OTHER_ACCOUNT)) == other_user_id
    assert run(factory, lambda db: tg.chat_of_user(db, user_id)) == ACCOUNT
    assert run(factory, lambda db: tg.chat_of_user(db, other_user_id)) == OTHER_ACCOUNT


def test_withdrawing_a_persons_access_ends_their_telegram_access_without_unlinking(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)
    assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, ACCOUNT)) == user_id

    run(
        factory,
        lambda db: [setattr(e, "revoked_at", NOW) for e in db.scalars(sa.select(AllowlistEntry).where(AllowlistEntry.user_id == user_id))],
    )
    assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, ACCOUNT)) is None
    assert run(factory, lambda db: tg.chat_of_user(db, user_id)) is None
    assert link_of(factory, user_id) is not None  # the row is still there

    run(
        factory,
        lambda db: [setattr(e, "revoked_at", None) for e in db.scalars(sa.select(AllowlistEntry).where(AllowlistEntry.user_id == user_id))],
    )
    assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, ACCOUNT)) == user_id

    run(factory, lambda db: setattr(db.get(User, user_id), "disabled_at", NOW))
    assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, ACCOUNT)) is None


def test_unlinking_removes_the_link_and_the_codes_and_only_that_users(factory, user_id, other_user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)
    present(factory, issue(factory, other_user_id).code, OTHER_ACCOUNT)

    assert run(factory, lambda db: tg.unlink(db, user_id)) is True
    assert run(factory, lambda db: tg.unlink(db, user_id)) is False

    assert link_of(factory, user_id) is None
    assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, ACCOUNT)) is None
    assert link_of(factory, other_user_id).telegram_user_id == OTHER_ACCOUNT
    assert {r.user_id for r in rows(factory, TelegramLinkCode)} == {other_user_id}


def test_unlinking_one_account_leaves_a_link_to_another_account_and_the_codes_alone(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)
    held = issue_unlinked_code(factory, user_id)

    assert run(factory, lambda db: tg.unlink(db, user_id, telegram_user_id=OTHER_ACCOUNT)) is False

    assert link_of(factory, user_id).telegram_user_id == ACCOUNT
    assert held in {r.id for r in rows(factory, TelegramLinkCode) if r.user_id == user_id}


def test_unlinking_the_named_account_removes_the_link_and_the_codes(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)

    assert run(factory, lambda db: tg.unlink(db, user_id, telegram_user_id=ACCOUNT)) is True

    assert link_of(factory, user_id) is None
    assert [r for r in rows(factory, TelegramLinkCode) if r.user_id == user_id] == []


def issue_unlinked_code(factory, user_id):
    """A code the user holds now, for an account that already has its link: put there directly
    (issue_link_code refuses a user who is linked). Returns the code row's id."""
    row = TelegramLinkCode(user_id=user_id, code_hash=hash_secret("x" * 32), created_at=NOW, expires_at=NOW + tg.CODE_LIFETIME)
    run(factory, lambda db: db.add(row))
    return row.id


def test_a_new_conversation_is_started_only_for_the_account_that_is_still_the_users(factory, user_id, other_user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)

    made = run(factory, lambda db: tg.start_conversation_for(db, ACCOUNT, user_id, now=NOW))
    wrong_user = run(factory, lambda db: tg.start_conversation_for(db, ACCOUNT, other_user_id, now=NOW))
    wrong_account = run(factory, lambda db: tg.start_conversation_for(db, OTHER_ACCOUNT, user_id, now=NOW))

    assert made is not None and wrong_user is None and wrong_account is None
    assert len(rows(factory, Conversation)) == 1


def test_no_conversation_is_started_for_a_user_whose_access_was_withdrawn_or_who_was_unlinked(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)
    run(factory, lambda db: tg.unlink(db, user_id))

    assert run(factory, lambda db: tg.start_conversation_for(db, ACCOUNT, user_id, now=NOW)) is None

    present(factory, issue(factory, user_id).code, ACCOUNT)
    run(factory, lambda db: [setattr(e, "revoked_at", NOW) for e in db.scalars(sa.select(AllowlistEntry).where(AllowlistEntry.user_id == user_id))])
    assert run(factory, lambda db: tg.start_conversation_for(db, ACCOUNT, user_id, now=NOW)) is None
    assert rows(factory, Conversation) == []


# --- what the database refuses on its own ------------------------------------------------------


def test_the_database_allows_one_account_per_user_and_one_user_per_account(session):
    first, second = enroll(session), enroll(session)
    session.add(TelegramLink(user_id=first.id, telegram_user_id=ACCOUNT, created_at=NOW))
    session.flush()

    with rejected(session, "telegram_links.telegram_user_id"):
        session.add(TelegramLink(user_id=second.id, telegram_user_id=ACCOUNT, created_at=NOW))
    with rejected(session, "telegram_links.user_id"):
        session.add(TelegramLink(user_id=first.id, telegram_user_id=OTHER_ACCOUNT, created_at=NOW))


@pytest.mark.parametrize("account", [0, -5, -1001234567890])
def test_the_database_refuses_a_group_id_or_zero(session, account):
    user = enroll(session)

    with rejected(session, "telegram_user_id_positive"):
        session.add(TelegramLink(user_id=user.id, telegram_user_id=account, created_at=NOW))


def test_the_database_bounds_the_name_and_the_code_digest(session):
    user = enroll(session)

    with rejected(session, "display_name_max_length"):
        session.add(
            TelegramLink(user_id=user.id, telegram_user_id=ACCOUNT, display_name="x" * 101, created_at=NOW)
        )
    with rejected(session, "code_hash_length"):
        session.add(
            TelegramLinkCode(user_id=user.id, code_hash="abc", created_at=NOW, expires_at=NOW + tg.CODE_LIFETIME)
        )
    with rejected(session, "expires_after_created"):
        session.add(TelegramLinkCode(user_id=user.id, code_hash="a" * 64, created_at=NOW, expires_at=NOW))


def test_a_stored_link_cannot_be_moved_to_another_user_or_account(session):
    first, second = enroll(session), enroll(session)
    link = TelegramLink(user_id=first.id, telegram_user_id=ACCOUNT, created_at=NOW)
    session.add(link)
    session.flush()

    link.user_id = second.id
    with pytest.raises(ValueError, match="cannot change once stored"):
        session.flush()
    session.rollback()


def test_deleting_a_user_deletes_the_link_and_the_codes(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)
    issue_more = TelegramLinkCode(
        user_id=user_id, code_hash=hash_secret(tg.new_code()), created_at=NOW, expires_at=NOW + tg.CODE_LIFETIME
    )
    run(factory, lambda db: db.add(issue_more))

    run(factory, lambda db: db.execute(sa.delete(User).where(User.id == user_id)))

    assert rows(factory, TelegramLink) == [] and rows(factory, TelegramLinkCode) == []


# --- conversations --------------------------------------------------------------------------------


def test_the_latest_conversation_is_the_one_the_website_lists_first(factory, user_id, other_user_id):
    def make(db):
        user, other = db.get(User, user_id), db.get(User, other_user_id)
        old = new_conversation(db, user, created_at=NOW, updated_at=NOW)
        newest = new_conversation(db, user, created_at=NOW, updated_at=NOW + timedelta(hours=3))
        new_conversation(db, other, created_at=NOW, updated_at=NOW + timedelta(days=30))
        return old.id, newest.id

    old, newest = run(factory, make)

    assert run(factory, lambda db: tg.latest_conversation_id(db, user_id)) == newest
    assert run(factory, lambda db: tg.latest_conversation_id(db, uuid4())) is None
    assert old != newest


def test_a_user_with_no_conversation_has_no_latest(factory, user_id):
    assert run(factory, lambda db: tg.latest_conversation_id(db, user_id)) is None


def test_the_conversation_of_a_request_is_found_through_the_users_own_conversations_only(
    factory, user_id, other_user_id
):
    def make(db):
        mine = new_conversation(db, db.get(User, user_id), created_at=NOW, updated_at=NOW)
        theirs = new_conversation(db, db.get(User, other_user_id), created_at=NOW, updated_at=NOW)
        for conversation, key in ((mine, "tg-100"), (theirs, "tg-200")):
            message = new_message(db, conversation, "user", "привет", created_at=NOW, expires_at=NOW + timedelta(days=1))
            new_turn_request(
                db, conversation, key, user_message_id=message.id, created_at=NOW, expires_at=NOW + timedelta(days=1)
            )
        return mine.id

    mine = run(factory, make)

    assert run(factory, lambda db: tg.conversation_of_request(db, user_id, "tg-100")) == mine
    assert run(factory, lambda db: tg.conversation_of_request(db, user_id, "tg-200")) is None
    assert run(factory, lambda db: tg.conversation_of_request(db, user_id, "tg-999")) is None


# --- withdrawing the codes of a user who has no link ---------------------------------------------


def test_the_codes_of_a_user_without_a_link_are_withdrawn_and_only_theirs(factory, user_id, other_user_id):
    issue(factory, user_id)
    issue(factory, other_user_id)

    assert run(factory, lambda db: tg.withdraw_codes_unless_linked(db, user_id)) is True

    assert {r.user_id for r in rows(factory, TelegramLinkCode)} == {other_user_id}


def test_nothing_is_removed_for_a_user_who_has_a_link_neither_the_link_nor_the_codes(factory, user_id):
    """The "nothing to unlink" operation is not an unlink: a link that exists was not what the
    caller looked at, and it is left alone, with whatever the user holds."""
    present(factory, issue(factory, user_id).code, ACCOUNT)
    held = issue_unlinked_code(factory, user_id)

    assert run(factory, lambda db: tg.withdraw_codes_unless_linked(db, user_id)) is False

    assert link_of(factory, user_id).telegram_user_id == ACCOUNT
    assert held in {r.id for r in rows(factory, TelegramLinkCode) if r.user_id == user_id}


# --- the identity of an update --------------------------------------------------------------------


def admit_for(factory, user_id, update_id, *, text="request", conversation=None):
    """The claim of a Telegram update as the bot records it: a turn request with the update's key
    in a conversation of the user's."""

    def make(db):
        found = db.get(Conversation, conversation) if conversation else new_conversation(db, db.get(User, user_id))
        new_turn_request(db, found, tg.update_key(update_id))
        return found.id

    return run(factory, make)


def test_an_update_has_one_key_made_of_its_id_and_a_prefix_the_website_does_not_use():
    assert tg.update_key(1001) == "tg-1001"
    assert "tg-".startswith(TELEGRAM_UPDATE_KEY_PREFIX) and tg.update_key(7).startswith(TELEGRAM_UPDATE_KEY_PREFIX)


def test_an_update_nobody_admitted_belongs_to_nobody(factory, user_id):
    assert run(factory, lambda db: tg.update_admitted_for_another(db, user_id, 1001)) is False


def test_an_update_admitted_for_the_user_is_not_another_ones(factory, user_id):
    admit_for(factory, user_id, 1001)

    assert run(factory, lambda db: tg.update_admitted_for_another(db, user_id, 1001)) is False


def test_an_update_admitted_for_somebody_else_is_theirs_whoever_asks_now(factory, user_id, other_user_id):
    admit_for(factory, user_id, 1001)

    assert run(factory, lambda db: tg.update_admitted_for_another(db, other_user_id, 1001)) is True
    assert run(factory, lambda db: tg.update_admitted_for_another(db, user_id, 1001)) is False


def test_the_look_is_by_the_updates_own_id_only(factory, user_id, other_user_id):
    admit_for(factory, user_id, 1001)

    assert run(factory, lambda db: tg.update_admitted_for_another(db, other_user_id, 1002)) is False
    assert run(factory, lambda db: tg.update_admitted_for_another(db, other_user_id, 100)) is False
    assert run(factory, lambda db: tg.update_admitted_for_another(db, other_user_id, 10010)) is False


def test_the_look_finds_the_update_in_any_conversation_of_its_owner_and_in_any_state(factory, user_id, other_user_id):
    def make(db):
        first = new_conversation(db, db.get(User, user_id))
        second = new_conversation(db, db.get(User, user_id))
        new_turn_request(db, first, tg.update_key(1001), status="failed", failure_kind="generation", error_code="x", finished_at=NOW)
        new_turn_request(db, second, tg.update_key(1002))

    run(factory, make)

    assert run(factory, lambda db: tg.update_admitted_for_another(db, other_user_id, 1001)) is True
    assert run(factory, lambda db: tg.update_admitted_for_another(db, other_user_id, 1002)) is True


def test_a_claim_is_admitted_for_the_account_that_is_still_the_users_and_a_fresh_update(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)

    assert run(factory, lambda db: tg.admit_update(db, ACCOUNT, user_id, 1001)) is True


def test_a_claim_is_refused_for_an_update_admitted_for_somebody_else(factory, user_id, other_user_id):
    present(factory, issue(factory, other_user_id).code, ACCOUNT)
    admit_for(factory, user_id, 1001)  # admitted while the account was the first person's

    assert run(factory, lambda db: tg.admit_update(db, ACCOUNT, other_user_id, 1001)) is False
    assert run(factory, lambda db: tg.admit_update(db, ACCOUNT, other_user_id, 1002)) is True


def test_a_claim_is_refused_when_the_account_is_not_the_users_or_the_user_is_not_enrolled(factory, user_id, other_user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)

    assert run(factory, lambda db: tg.admit_update(db, ACCOUNT, other_user_id, 1001)) is False  # another user's account
    assert run(factory, lambda db: tg.admit_update(db, OTHER_ACCOUNT, user_id, 1001)) is False  # an account with no link
    run(factory, lambda db: setattr(db.get(User, user_id), "disabled_at", NOW))
    assert run(factory, lambda db: tg.admit_update(db, ACCOUNT, user_id, 1001)) is False


def test_holding_the_link_asks_postgresql_for_a_share_lock_on_the_link_row_and_sqlite_for_nothing():
    """The lock that keeps an unlink away until the claim has committed. SQLite has no row locks
    (its writer lock does the work), so the clause must simply not appear there."""
    from sqlalchemy.dialects import postgresql, sqlite

    query = sa.select(TelegramLink.user_id).where(TelegramLink.telegram_user_id == ACCOUNT).with_for_update(read=True, of=TelegramLink)

    assert "FOR SHARE OF telegram_links" in str(query.compile(dialect=postgresql.dialect()))
    assert "FOR" not in str(query.compile(dialect=sqlite.dialect()))


def test_holding_the_link_changes_nothing_in_the_answer(factory, user_id):
    present(factory, issue(factory, user_id).code, ACCOUNT)

    assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, ACCOUNT, hold=True)) == user_id
    assert run(factory, lambda db: tg.enrolled_user_for_telegram(db, OTHER_ACCOUNT, hold=True)) is None
