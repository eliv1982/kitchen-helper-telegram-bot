"""The durable Telegram update admission ledger: bot-scoped markers that survive the
deletion of everything the update produced.

Why it exists: the turn request of a Telegram turn is cascaded away with its conversation,
so after a conversation deletion a redelivery had no durable record and looked new. The
ledger keeps (bot_id, update_id) and timestamps only -- no user, conversation, message,
photo, answer or credential -- and has no foreign key to any of them, so nothing a person
deletes can erase the fact that Telegram already delivered the update.

Every test runs twice (tests/persistence_support.py): on a schema created from the models
and on one built by the migrations. The migration's populated upgrade/downgrade cycle is in
tests/test_persistence_telegram_migration.py; the end-to-end bot behaviour is
tests/test_telegram_dedup.py.

What is pinned here:

  * the identity is the bot's (bot_id, update_id), enforced by the primary key;
  * admission is one function: the binding check, the "not another owner's" check and the
    marker write, so a refused claim leaves no marker;
  * a marker survives the deletion of its conversation and of the user, and it refuses a
    resolution before a replacement conversation is created;
  * the cutover row is written once per bot and the backfill recovers only what the turn
    requests can prove (recent, "tg-<id>" keys, earliest admission);
  * the shape CHECKs hold for rows that did not come through the ORM.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from house_helper.persistence import conversations as cv
from house_helper.persistence import telegram as tg
from house_helper.persistence.allowlist import allow
from house_helper.persistence.models import (
    TELEGRAM_ADMISSION_RETENTION,
    TELEGRAM_CUTOVER_UPDATE_ID,
    Conversation,
    TelegramActiveConversation,
    TelegramLink,
    TelegramUpdateAdmission,
    TelegramUpdateAdmissionKind,
    TurnRequest,
    User,
)
from tests.persistence_support import (  # noqa: F401  (fixtures)
    engine,
    factory,
    new_conversation,
    new_turn_request,
    new_user,
    rejected,
    session,
    utc,
)

NOW = utc(2026, 11, 7, 12)
BOT = 123456789
OTHER_BOT = 987654321
ACCOUNT = 555000111
OTHER_ACCOUNT = 555000222


def run(factory, work):
    with factory() as db, db.begin():
        return work(db)


def enroll(db) -> User:
    return new_user(db)


def link_user(factory, user_id, account=ACCOUNT) -> None:
    """The bot's numeric account tied to the user, with a provisioned sign-in identity.

    The OIDC subject is random and unrelated to the numeric id (models.TelegramLink says
    they are different values); random per call so the same account can be re-paired to
    another person after unlinking without colliding on the allowlist."""
    def make(db):
        user = db.get(User, user_id)
        allow(db, user, "telegram", f"sub-{uuid4().hex}")
        db.add(TelegramLink(user_id=user_id, telegram_user_id=account, created_at=NOW))

    run(factory, make)


def unlink(factory, user_id, account=ACCOUNT) -> bool:
    return run(factory, lambda db: tg.unlink(db, user_id, telegram_user_id=account))


def make_conversation(factory, user_id):
    return run(
        factory,
        lambda db: new_conversation(
            db, db.get(User, user_id), created_at=NOW, updated_at=NOW
        ),
    )


def admit(factory, user_id, update_id, *, bot=BOT, existing=False, account=ACCOUNT):
    return run(
        factory,
        lambda db: tg.admit_update(
            db,
            account,
            user_id,
            update_id,
            bot_id=bot,
            now=NOW,
            existing=existing,
        ),
    )


def marker(factory, update_id, bot=BOT):
    with factory() as db:
        return db.get(TelegramUpdateAdmission, (bot, update_id))


def markers(factory):
    with factory() as db:
        return list(db.scalars(sa.select(TelegramUpdateAdmission)))


@pytest.fixture
def user_id(factory):
    return run(factory, lambda db: enroll(db).id)


@pytest.fixture
def other_user_id(factory):
    return run(factory, lambda db: enroll(db).id)


# --- admission --------------------------------------------------------------------------------


def test_an_admission_writes_one_marker_for_this_bot_and_update(factory, user_id):
    link_user(factory, user_id)

    assert admit(factory, user_id, 1001) is tg.AdmissionOutcome.ADMITTED

    [row] = markers(factory)
    assert (row.bot_id, row.update_id, row.kind) == (
        BOT,
        1001,
        TelegramUpdateAdmissionKind.UPDATE.value,
    )
    assert row.admitted_at == NOW
    assert row.expires_at == NOW + TELEGRAM_ADMISSION_RETENTION
    assert run(factory, lambda db: tg.update_admission_exists(db, BOT, 1001)) is True
    assert run(factory, lambda db: tg.update_admission_exists(db, OTHER_BOT, 1001)) is False


def test_the_identity_is_scoped_to_the_bot_not_to_a_user_or_conversation(factory, user_id, other_user_id):
    """The same update id under two bots is two markers; under one bot it is one, whoever
    asks and whatever conversation they have."""
    link_user(factory, user_id)

    assert admit(factory, user_id, 1001) is tg.AdmissionOutcome.ADMITTED
    assert admit(factory, user_id, 1001, bot=OTHER_BOT) is tg.AdmissionOutcome.ADMITTED

    assert {(row.bot_id, row.update_id) for row in markers(factory)} == {
        (BOT, 1001),
        (OTHER_BOT, 1001),
    }


def test_a_new_claim_that_meets_a_marker_is_refused_and_writes_nothing(factory, user_id):
    link_user(factory, user_id)
    admit(factory, user_id, 1001)

    assert admit(factory, user_id, 1001) is tg.AdmissionOutcome.ALREADY_ADMITTED

    [row] = markers(factory)  # one row, the first admission, not rewritten
    assert (row.bot_id, row.update_id, row.admitted_at) == (BOT, 1001, NOW)
    assert run(factory, lambda db: list(db.scalars(sa.select(TurnRequest)))) == []


def test_a_surviving_claim_stands_when_its_marker_is_already_there(factory, user_id):
    """A replay or a retry of a turn request that still exists: the marker is expected and
    must not refuse the claim. It also fills a marker that a pre-migration database lacks."""
    link_user(factory, user_id)
    conversation = make_conversation(factory, user_id)
    run(factory, lambda db: new_turn_request(db, conversation, tg.update_key(1001)))

    assert admit(factory, user_id, 1001, existing=True) is tg.AdmissionOutcome.ADMITTED
    assert marker(factory, 1001) is not None

    # ... and the second replay finds the marker and still stands.
    assert admit(factory, user_id, 1001, existing=True) is tg.AdmissionOutcome.ADMITTED
    assert len(markers(factory)) == 1


def test_a_refused_binding_leaves_no_marker_that_would_block_a_legitimate_first_attempt(
    factory, user_id, other_user_id
):
    """A claim for an account that is not the user's is refused before the marker, so the
    person who does own the account can still have the update admitted later."""
    link_user(factory, user_id)

    assert admit(factory, user_id, 1001, account=OTHER_ACCOUNT) is tg.AdmissionOutcome.BINDING_CHANGED
    assert markers(factory) == []

    assert admit(factory, user_id, 1001) is tg.AdmissionOutcome.ADMITTED
    assert marker(factory, 1001) is not None


def test_an_update_admitted_for_another_owner_is_refused_without_a_second_marker(factory, user_id, other_user_id):
    link_user(factory, user_id)
    link_user(factory, other_user_id, account=OTHER_ACCOUNT)
    conversation = make_conversation(factory, user_id)
    run(factory, lambda db: new_turn_request(db, conversation, tg.update_key(1001)))

    assert admit(factory, other_user_id, 1001, account=OTHER_ACCOUNT) is (
        tg.AdmissionOutcome.BINDING_CHANGED
    )
    assert markers(factory) == []


def test_the_database_refuses_a_second_row_for_the_same_bot_and_update(session, user_id):
    session.add(
        TelegramUpdateAdmission(
            bot_id=BOT,
            update_id=1001,
            kind=TelegramUpdateAdmissionKind.UPDATE.value,
            admitted_at=NOW,
            expires_at=NOW + TELEGRAM_ADMISSION_RETENTION,
        )
    )
    session.flush()

    session.add(
        TelegramUpdateAdmission(
            bot_id=BOT,
            update_id=1001,
            kind=TelegramUpdateAdmissionKind.UPDATE.value,
            admitted_at=NOW,
            expires_at=NOW + TELEGRAM_ADMISSION_RETENTION,
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        session.flush()


@pytest.mark.parametrize(
    "fields, constraint",
    [
        (
            dict(
                update_id=0,
                kind="update",
                expires_at=NOW + TELEGRAM_ADMISSION_RETENTION,
            ),
            "ck_telegram_update_admissions_shape_valid",
        ),
        (
            dict(
                update_id=1001,
                kind="update",
                expires_at=None,
            ),
            "ck_telegram_update_admissions_shape_valid",
        ),
        (
            dict(
                update_id=1001,
                kind="cutover",
                expires_at=None,
            ),
            "ck_telegram_update_admissions_shape_valid",
        ),
        (
            dict(
                update_id=TELEGRAM_CUTOVER_UPDATE_ID + 1,
                kind="something_else",
                expires_at=NOW + TELEGRAM_ADMISSION_RETENTION,
            ),
            "ck_telegram_update_admissions_kind_valid",
        ),
        (
            dict(
                update_id=1001,
                kind="update",
                expires_at=NOW,  # not after admitted_at
            ),
            "ck_telegram_update_admissions_shape_valid",
        ),
    ],
)
def test_the_shape_checks_hold_for_rows_that_did_not_come_through_the_orm(
    session, fields, constraint
):
    row = {
        "bot_id": BOT,
        "admitted_at": NOW,
        **fields,
    }
    with rejected(session, constraint):
        session.add(TelegramUpdateAdmission(**row))


def test_the_orm_refuses_to_rewrite_a_marker(session, user_id):
    session.add(
        TelegramUpdateAdmission(
            bot_id=BOT,
            update_id=1001,
            kind=TelegramUpdateAdmissionKind.UPDATE.value,
            admitted_at=NOW,
            expires_at=NOW + TELEGRAM_ADMISSION_RETENTION,
        )
    )
    session.flush()
    row = session.get(TelegramUpdateAdmission, (BOT, 1001))

    row.expires_at = NOW + timedelta(hours=1)
    with pytest.raises(ValueError):
        session.flush()


# --- what survives ------------------------------------------------------------------------


def test_a_marker_survives_the_deletion_of_its_conversation_and_of_its_user(factory, user_id):
    link_user(factory, user_id)
    conversation = make_conversation(factory, user_id)
    run(factory, lambda db: new_turn_request(db, conversation, tg.update_key(1001)))
    admit(factory, user_id, 1001, existing=True)

    run(factory, lambda db: cv.delete_conversation(db, user_id, conversation.id))
    assert marker(factory, 1001) is not None

    run(factory, lambda db: db.execute(sa.delete(User).where(User.id == user_id)))
    assert marker(factory, 1001) is not None  # no foreign key: nothing a person deletes erases it


def test_the_status_follows_the_owner_request_then_the_marker(factory, user_id, other_user_id):
    link_user(factory, user_id)
    link_user(factory, other_user_id, account=OTHER_ACCOUNT)
    conversation = make_conversation(factory, user_id)
    run(factory, lambda db: new_turn_request(db, conversation, tg.update_key(1001)))
    admit(factory, user_id, 1001, existing=True)
    key = tg.update_key(1001)

    def status(db, owner):
        return tg.update_admission_status(db, BOT, owner, 1001, key)

    assert run(factory, lambda db: status(db, user_id)) is tg.AdmissionStatus.ADMITTED_HERE
    assert run(factory, lambda db: status(db, other_user_id)) is tg.AdmissionStatus.ADMITTED_ELSEWHERE

    run(factory, lambda db: cv.delete_conversation(db, user_id, conversation.id))

    assert run(factory, lambda db: status(db, other_user_id)) is tg.AdmissionStatus.ADMITTED_GONE
    assert run(factory, lambda db: status(db, user_id)) is tg.AdmissionStatus.ADMITTED_GONE


def test_a_new_update_is_new_and_a_marker_for_another_bot_does_not_count(factory, user_id):
    key = tg.update_key(1001)
    assert (
        run(factory, lambda db: tg.update_admission_status(db, BOT, user_id, 1001, key))
        is tg.AdmissionStatus.NEW
    )

    link_user(factory, user_id)
    admit(factory, user_id, 1001, bot=OTHER_BOT)
    assert (
        run(factory, lambda db: tg.update_admission_status(db, BOT, user_id, 1001, key))
        is tg.AdmissionStatus.NEW
    )


def test_a_resolution_with_a_marker_and_no_surviving_request_refuses_to_create_anything(
    factory, user_id, other_user_id
):
    """The exact hole: the conversation (and the turn request in it) is gone, the account was
    re-paired, and the update comes again. The ledger refuses BEFORE any conversation is made
    or selected."""
    link_user(factory, user_id)
    conversation = make_conversation(factory, user_id)
    run(factory, lambda db: new_turn_request(db, conversation, tg.update_key(1001)))
    admit(factory, user_id, 1001, existing=True)
    run(factory, lambda db: cv.delete_conversation(db, user_id, conversation.id))

    # The same owner, after re-pairing.
    assert unlink(factory, user_id)
    link_user(factory, user_id, account=OTHER_ACCOUNT)
    with pytest.raises(tg.UpdateAlreadyAdmitted):
        run(
            factory,
            lambda db: tg.conversation_for_message(
                db,
                user_id,
                tg.update_key(1001),
                now=NOW,
                telegram_user_id=OTHER_ACCOUNT,
                bot_id=BOT,
                update_id=1001,
            ),
        )

    # Another owner, after a handover of the original account.
    assert unlink(factory, user_id, account=OTHER_ACCOUNT)
    link_user(factory, other_user_id, account=ACCOUNT)
    with pytest.raises(tg.UpdateAlreadyAdmitted):
        run(
            factory,
            lambda db: tg.conversation_for_message(
                db,
                other_user_id,
                tg.update_key(1001),
                now=NOW,
                telegram_user_id=ACCOUNT,
                bot_id=BOT,
                update_id=1001,
            ),
        )

    assert run(factory, lambda db: list(db.scalars(sa.select(Conversation)))) == []
    assert run(factory, lambda db: list(db.scalars(sa.select(TelegramActiveConversation)))) == []


def test_a_resolution_without_the_ledger_identity_behaves_as_before(factory, user_id):
    """The optional arguments are what keep every existing caller working."""
    link_user(factory, user_id)
    conversation = make_conversation(factory, user_id)
    run(factory, lambda db: new_turn_request(db, conversation, tg.update_key(1001)))
    admit(factory, user_id, 1001, existing=True)
    run(factory, lambda db: cv.delete_conversation(db, user_id, conversation.id))

    found = run(
        factory,
        lambda db: tg.conversation_for_message(
            db, user_id, tg.update_key(1001), now=NOW, telegram_user_id=ACCOUNT
        ),
    )

    assert found is not None and found.admitted is False  # the old resolver, unchanged


def test_a_resolution_told_the_update_was_admitted_never_resolves_it_afresh(factory, user_id):
    """The caller's first look saw the update's own turn request; it has disappeared since
    (its conversation was deleted), and the marker has been purged. Without the provenance
    the resolver would fall back to the selection and create a conversation for an update
    that was already admitted -- the 49-hour race. With it, nothing is created or selected."""
    link_user(factory, user_id)
    selected = make_conversation(factory, user_id)
    run(factory, lambda db: tg.select_conversation(db, user_id, selected.id, now=NOW))
    conversations_before = run(
        factory, lambda db: [row.id for row in db.scalars(sa.select(Conversation))]
    )

    with pytest.raises(tg.UpdateAlreadyAdmitted):
        run(
            factory,
            lambda db: tg.conversation_for_message(
                db,
                user_id,
                tg.update_key(1001),
                now=NOW,
                telegram_user_id=ACCOUNT,
                bot_id=BOT,
                update_id=1001,
                admitted_here=True,  # no turn request, no marker: only the caller's look
            ),
        )

    assert run(
        factory, lambda db: [row.id for row in db.scalars(sa.select(Conversation))]
    ) == conversations_before
    [selection] = run(
        factory, lambda db: list(db.scalars(sa.select(TelegramActiveConversation)))
    )
    assert selection.conversation_id == selected.id


# --- cutover and backfill ------------------------------------------------------------------


def test_the_cutover_row_is_written_once_per_bot_and_never_replaced(factory):
    def initialize(db, now):
        return tg.initialize_update_admissions(db, BOT, now=now)

    first = run(factory, lambda db: initialize(db, NOW))
    later = run(factory, lambda db: initialize(db, NOW + timedelta(days=1)))

    assert first.cutover_at == NOW
    assert later.cutover_at == NOW  # read back, not rewritten
    [row] = markers(factory)
    assert (row.bot_id, row.update_id, row.kind, row.expires_at) == (
        BOT,
        TELEGRAM_CUTOVER_UPDATE_ID,
        TelegramUpdateAdmissionKind.CUTOVER.value,
        None,
    )

    run(factory, lambda db: initialize(db, NOW))
    other = run(
        factory, lambda db: tg.initialize_update_admissions(db, OTHER_BOT, now=NOW)
    )
    assert other.cutover_at == NOW  # another bot has its own cutover row
    assert len(markers(factory)) == 2


def test_the_backfill_recovers_recent_update_requests_and_nothing_else(factory, user_id):
    conversation = make_conversation(factory, user_id)

    def seed(db):
        new_turn_request(db, conversation, tg.update_key(1001), created_at=NOW - timedelta(hours=1))
        new_turn_request(db, conversation, tg.update_key(1002), created_at=NOW - timedelta(hours=47))
        new_turn_request(db, conversation, tg.update_key(1003), created_at=NOW - timedelta(hours=49))
        new_turn_request(db, conversation, "web-1", created_at=NOW - timedelta(hours=1))
        new_turn_request(db, conversation, "tg-01004", created_at=NOW - timedelta(hours=1))
        new_turn_request(db, conversation, "tg-", created_at=NOW - timedelta(hours=1))

    run(factory, seed)

    ledger = run(factory, lambda db: tg.initialize_update_admissions(db, BOT, now=NOW))

    assert ledger.backfilled == 2
    assert {row.update_id for row in markers(factory) if row.update_id != 0} == {1001, 1002}
    assert marker(factory, 1003) is None  # beyond the window
    assert marker(factory, 4) is None  # a key this module would not write


def test_the_backfill_dates_a_marker_from_the_earliest_surviving_request(factory, user_id):
    """One update once admitted twice (the hole the ledger closes) is still one marker, dated
    to the first admission. The key is unique per conversation, so the two live apart."""
    first = make_conversation(factory, user_id)
    second = make_conversation(factory, user_id)

    def seed(db):
        new_turn_request(db, first, tg.update_key(1001), created_at=NOW - timedelta(hours=5))
        new_turn_request(db, second, tg.update_key(1001), created_at=NOW - timedelta(hours=2))

    run(factory, seed)
    run(factory, lambda db: tg.initialize_update_admissions(db, BOT, now=NOW))

    [row] = [marker for marker in markers(factory) if marker.update_id == 1001]
    assert row.admitted_at == NOW - timedelta(hours=5)
    assert row.expires_at == NOW - timedelta(hours=5) + TELEGRAM_ADMISSION_RETENTION


def test_a_backfill_never_rewrites_a_marker_that_already_exists(factory, user_id):
    link_user(factory, user_id)
    admit(factory, user_id, 1001)
    before = marker(factory, 1001)
    assert before is not None and before.admitted_at == NOW
    conversation = make_conversation(factory, user_id)
    run(
        factory,
        lambda db: new_turn_request(
            db, conversation, tg.update_key(1001), created_at=NOW - timedelta(hours=30)
        ),
    )

    run(factory, lambda db: tg.initialize_update_admissions(db, BOT, now=NOW))

    after = marker(factory, 1001)
    assert after is not None and after.admitted_at == NOW  # the first admission stands


def test_a_skewed_future_turn_request_is_still_recovered(factory, user_id):
    """A request dated in the future (a wrong server clock at the time) proves what it
    proves; the marker is written with that time and the purge's ceiling judges it then."""
    conversation = make_conversation(factory, user_id)
    run(
        factory,
        lambda db: new_turn_request(
            db, conversation, tg.update_key(1001), created_at=NOW + timedelta(hours=1)
        ),
    )

    ledger = run(factory, lambda db: tg.initialize_update_admissions(db, BOT, now=NOW))

    assert ledger.backfilled == 1
    assert marker(factory, 1001).admitted_at == NOW + timedelta(hours=1)


@pytest.mark.parametrize("fail_on", [1, 2], ids=["before-any-marker", "halfway"])
def test_a_preparation_that_fails_part_way_commits_nothing_and_can_be_retried(
    factory, user_id, monkeypatch, fail_on
):
    """The cutover row and the backfilled markers are one transaction. A failure before the
    first recovered marker or halfway through them leaves NO row behind (not even the cutover),
    so a retry starts from a clean ledger; the retry writes the pinned cutover, not its own
    instant."""
    first = make_conversation(factory, user_id)
    second = make_conversation(factory, user_id)
    run(
        factory,
        lambda db: new_turn_request(
            db, first, tg.update_key(1001), created_at=NOW - timedelta(hours=2)
        ),
    )
    run(
        factory,
        lambda db: new_turn_request(
            db, second, tg.update_key(1002), created_at=NOW - timedelta(hours=1)
        ),
    )

    real = tg._insert_update_marker
    made = {"markers": 0}

    def failing(session, bot_id, update_id, admitted_at):
        made["markers"] += 1
        if made["markers"] == fail_on:
            raise RuntimeError("the storage went away")
        return real(session, bot_id, update_id, admitted_at)

    pinned = NOW - timedelta(hours=3)
    monkeypatch.setattr(tg, "_insert_update_marker", failing)
    with pytest.raises(RuntimeError):
        run(
            factory,
            lambda db: tg.initialize_update_admissions(
                db, BOT, now=NOW, cutover_at=pinned
            ),
        )
    assert markers(factory) == []  # the cutover row rolled back with the recovered markers

    monkeypatch.setattr(tg, "_insert_update_marker", real)
    ledger = run(
        factory,
        lambda db: tg.initialize_update_admissions(db, BOT, now=NOW, cutover_at=pinned),
    )

    assert ledger.cutover_at == pinned  # the process start, not the retry's instant
    assert ledger.backfilled == 2
    assert {row.update_id for row in markers(factory)} == {
        TELEGRAM_CUTOVER_UPDATE_ID,
        1001,
        1002,
    }


def test_a_pinned_cutover_is_used_once_and_then_read_back(factory):
    pinned = NOW - timedelta(minutes=5)
    first = run(
        factory,
        lambda db: tg.initialize_update_admissions(
            db, BOT, now=NOW, cutover_at=pinned
        ),
    )
    later = run(
        factory,
        lambda db: tg.initialize_update_admissions(
            db, BOT, now=NOW + timedelta(days=1), cutover_at=NOW
        ),
    )

    assert first.cutover_at == pinned
    assert later.cutover_at == pinned  # the existing row always wins


def test_update_id_from_key_accepts_only_what_update_key_writes():
    assert tg.update_id_from_key("tg-1") == 1
    assert tg.update_id_from_key("tg-9223372036854775807") == 9223372036854775807
    assert tg.update_id_from_key("tg-9223372036854775808") is None  # past a signed BIGINT
    assert tg.update_id_from_key("tg-0") is None
    assert tg.update_id_from_key("tg-01") is None
    assert tg.update_id_from_key("tg-") is None
    assert tg.update_id_from_key("tg--1") is None
    assert tg.update_id_from_key("tg-1a") is None
    assert tg.update_id_from_key("web-1") is None
    assert tg.update_id_from_key("tg-9" * 20) is None
    assert tg.update_id_from_key(None) is None
