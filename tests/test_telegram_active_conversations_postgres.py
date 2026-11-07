"""Real PostgreSQL: the 0011 selection migration and the cursor's binding race. Opt-in.

What the SQLite tests cannot show:

  * `alembic_version.version_num` is VARCHAR(32) on PostgreSQL, so a revision id longer
    than 32 characters fails the upgrade with SQLSTATE 22001 (it did: the first draft was
    `0011_telegram_active_conversations`, 34 characters). Here a populated database at
    `0010_structured_recipe_favorites` is upgraded on a real server, every existing row is
    checked unchanged, the revision is checked, and the downgrade / re-upgrade cycle is
    run too.
  * `conversation_for_message` now holds the Telegram link (`SELECT ... FOR SHARE`) in the
    transaction that may create and select a conversation. That share lock is what stops
    a concurrent unlink from committing between the resolution's read and its cursor
    write: the unlink's DELETE waits for the resolution, and then removes the cursor with
    the link. Without it the resolution commits a cursor for a link that is already gone.

To run, see tests/test_admin_postgres.py (the same throwaway server, variable and guards):

    pytest tests/test_telegram_active_conversations_postgres.py -v -rs

Without HOUSE_HELPER_TEST_POSTGRES_URL every test here is skipped, which is not a pass:
`pytest -rs` lists the skip. CI does not set it.
"""

import os
import threading
import time
from datetime import timedelta

import pytest

from house_helper.admin.retention import purge_expired
from house_helper.persistence import conversations as cv
from house_helper.persistence import telegram as tg
from house_helper.persistence.allowlist import allow
from house_helper.persistence.database import (
    create_session_factory,
    downgrade_database,
    upgrade_database,
)
from house_helper.persistence.models import (
    TELEGRAM_CUTOVER_UPDATE_ID,
    FavoriteRecipe,
    TelegramLink,
    TelegramUpdateAdmission,
)
from tests.persistence_support import (
    new_conversation,
    new_message,
    new_turn_request,
    new_user,
    utc,
)
from tests.postgres_harness import POSTGRES_URL_ENV, Scratch
from tests.test_admin_enrollment_postgres import (  # noqa: F401  (fixtures)
    monitor_rc,
    run_on,
    scratch_rc,
)
from tests.test_admin_postgres import (  # noqa: F401  (fixtures and helpers)
    pg_run,
    scalar,
    template,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL "
    "(see tests/test_admin_postgres.py)",
)

NOW = utc(2026, 11, 7, 12)
PREVIOUS = "0010_structured_recipe_favorites"
HEAD = "0011_telegram_active_selection"
ACCOUNT = 555000111
BOT = 123456789


def test_the_selection_revision_fits_the_version_column():
    assert len(HEAD) <= 32  # alembic_version.version_num is VARCHAR(32)


# --- the migration on a real server ---------------------------------------------------------------


def test_a_populated_0010_database_upgrades_downgrades_and_upgrades_again(pg_run):
    name = pg_run.create_database()
    scratch = Scratch(pg_run, name)
    try:
        engine = scratch.engine()
        try:
            upgrade_database(engine, PREVIOUS)
            factory = create_session_factory(engine)
            with factory() as db, db.begin():
                user = new_user(db, display_name="Elena")
                conversation = new_conversation(
                    db, user, created_at=NOW, updated_at=NOW
                )
                message = new_message(
                    db, conversation, role="user", content="Борщ?", created_at=NOW
                )
                new_turn_request(db, conversation, "w-1", user_message_id=message.id)
                db.add(
                    TelegramLink(
                        user_id=user.id, telegram_user_id=ACCOUNT, created_at=NOW
                    )
                )
                db.add(
                    FavoriteRecipe(
                        user_id=user.id,
                        title="Борщ",
                        content="Свёкла, капуста.",
                        source_message_id=message.id,
                    )
                )

            def snapshot():
                return {
                    "users": scalar(engine, "SELECT count(*) FROM users"),
                    "conversations": scalar(
                        engine, "SELECT count(*) FROM conversations"
                    ),
                    "messages": scalar(engine, "SELECT count(*) FROM messages"),
                    "turn_requests": scalar(
                        engine, "SELECT count(*) FROM turn_requests"
                    ),
                    "telegram_links": scalar(
                        engine, "SELECT count(*) FROM telegram_links"
                    ),
                    "favorite_recipes": scalar(
                        engine, "SELECT count(*) FROM favorite_recipes"
                    ),
                }

            before = snapshot()
            assert scalar(engine, "SELECT version_num FROM alembic_version") == PREVIOUS

            upgrade_database(engine)  # the fix: the identifier fits VARCHAR(32)

            assert scalar(engine, "SELECT version_num FROM alembic_version") == HEAD
            assert snapshot() == before  # every old row is unchanged
            for table in ("telegram_active_conversations", "telegram_update_admissions"):
                assert scalar(engine, f"SELECT count(*) FROM {table}") == 0
                assert scalar(
                    engine, f"SELECT to_regclass('{table}') IS NOT NULL"
                )
            assert (
                scalar(
                    engine,
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'telegram_update_admissions'",
                )
                == 5
            )

            downgrade_database(engine, PREVIOUS)

            assert scalar(engine, "SELECT version_num FROM alembic_version") == PREVIOUS
            for table in ("telegram_active_conversations", "telegram_update_admissions"):
                assert scalar(engine, f"SELECT to_regclass('{table}') IS NULL") is True
            assert snapshot() == before

            upgrade_database(engine)  # and forward again: empty tables, as they began

            assert scalar(engine, "SELECT version_num FROM alembic_version") == HEAD
            assert snapshot() == before
            for table in ("telegram_active_conversations", "telegram_update_admissions"):
                assert scalar(engine, f"SELECT count(*) FROM {table}") == 0
        finally:
            engine.dispose()
    finally:
        pg_run.drop_database(name)


# --- the binding race ------------------------------------------------------------------------------


def _cursor_waiting_to_be_recreated(scratch):
    """A user, a link, and a selection row whose conversation was deleted: resolving a
    message now creates a conversation and writes the cursor, which is what the unlink
    race is about."""

    def work(db):
        user = new_user(db, display_name="Elena")
        allow(db, user, "telegram", "sub-1")
        db.add(
            TelegramLink(user_id=user.id, telegram_user_id=ACCOUNT, created_at=NOW)
        )
        selected = new_conversation(db, user, created_at=NOW, updated_at=NOW)
        tg.select_conversation(db, user.id, selected.id, now=NOW)
        cv.delete_conversation(db, user.id, selected.id)
        return user.id

    return run_on(scratch, work)


def _wait_until_locked_out_or_done(
    engine, unlink_done: threading.Event, deadline_seconds: float = 30.0
) -> bool:
    """True when a session is really waiting on a lock; False when the unlink finished
    first (which is the pre-fix outcome this test must catch)."""
    sql = (
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock'"
    )
    deadline = time.monotonic() + deadline_seconds
    while True:
        if unlink_done.is_set():
            return False
        if scalar(engine, sql) >= 1:
            return True
        assert time.monotonic() < deadline, "the unlink neither finished nor waited"
        time.sleep(0.05)


def test_resolution_holds_the_link_and_cannot_recreate_the_cursor_after_an_unlink(
    scratch_rc, monitor_rc, monkeypatch
):
    """The exact race: the resolution has read the NULL selection and is about to write
    the cursor; the unlink starts. With the link held (`SELECT ... FOR SHARE`) in the
    resolution's transaction, the unlink's DELETE waits for it, then removes the cursor
    with the link. Without the hold, the unlink commits during the pause and the
    resolution writes a cursor for a link that is already gone."""
    user_id = _cursor_waiting_to_be_recreated(scratch_rc)
    engine = scratch_rc.engine()
    resolved = {}
    unlinked = {}
    entered = threading.Event()
    release = threading.Event()
    unlink_done = threading.Event()
    real_write = tg._write_selection

    def paused_write(session, owner, conversation_id, *, now):
        entered.set()
        release.wait(10)  # the selection was read; the cursor has not been written yet
        return real_write(session, owner, conversation_id, now=now)

    monkeypatch.setattr(tg, "_write_selection", paused_write)
    try:
        factory = create_session_factory(engine)

        def resolving():
            with factory() as db, db.begin():
                resolved["found"] = tg.conversation_for_message(
                    db, user_id, "tg-1", now=NOW, telegram_user_id=ACCOUNT
                )

        def unlinking():
            with factory() as db, db.begin():
                unlinked["removed"] = tg.unlink(db, user_id, telegram_user_id=ACCOUNT)
            unlink_done.set()

        first = threading.Thread(target=resolving)
        first.start()
        try:
            assert entered.wait(10)
            second = threading.Thread(target=unlinking)
            second.start()
            try:
                blocked = _wait_until_locked_out_or_done(monitor_rc, unlink_done)
                assert blocked is True  # the unlink waits for the resolution's link lock
                assert unlinked == {}  # ... and has committed nothing
            finally:
                release.set()
                first.join(30)
                second.join(30)
            assert not first.is_alive() and not second.is_alive()
        finally:
            release.set()
            first.join(30)

        assert resolved["found"] is not None  # the resolution created and selected one
        assert unlinked["removed"] is True
    finally:
        engine.dispose()

    # The unlink ran second and took the cursor with the link: the stale cursor
    # (written by the resolution) is gone, and nothing was left behind for no link.
    assert (
        scalar(monitor_rc, "SELECT count(*) FROM telegram_active_conversations") == 0
    )
    assert scalar(monitor_rc, "SELECT count(*) FROM telegram_links") == 0


def test_an_unlink_that_committed_before_resolution_leaves_no_cursor(scratch_rc, monitor_rc):
    """The other order: the link is already gone when the resolution opens its
    transaction, so the binding check refuses it and no cursor is written."""
    user_id = _cursor_waiting_to_be_recreated(scratch_rc)

    assert run_on(
        scratch_rc,
        lambda db: tg.unlink(db, user_id, telegram_user_id=ACCOUNT),
    ) is True

    assert run_on(
        scratch_rc,
        lambda db: tg.conversation_for_message(
            db, user_id, "tg-1", now=NOW, telegram_user_id=ACCOUNT
        ),
    ) is None
    assert (
        scalar(monitor_rc, "SELECT count(*) FROM telegram_active_conversations") == 0
    )
    assert scalar(monitor_rc, "SELECT count(*) FROM telegram_links") == 0


# --- the update admission ledger on a real server -------------------------------------------------


def _user_with_link(scratch, account=ACCOUNT):
    def work(db):
        user = new_user(db, display_name="Elena")
        allow(db, user, "telegram", f"sub-{account}")
        db.add(
            TelegramLink(user_id=user.id, telegram_user_id=account, created_at=NOW)
        )
        return user.id

    return run_on(scratch, work)


def _admitted_update(scratch, user_id, update_id):
    """A conversation, the turn request of one update, and its marker, in one transaction."""
    from house_helper.persistence.models import User

    def work(db):
        conversation = new_conversation(
            db, db.get(User, user_id), created_at=NOW, updated_at=NOW
        )
        new_turn_request(db, conversation, tg.update_key(update_id), created_at=NOW)
        outcome = tg.admit_update(
            db, ACCOUNT, user_id, update_id, bot_id=BOT, now=NOW, existing=True
        )
        return conversation.id, outcome

    return run_on(scratch, work)


def test_the_marker_survives_the_deletion_and_refuses_a_resolution_on_postgresql(scratch_rc, monitor_rc):
    user_id = _user_with_link(scratch_rc)
    conversation_id, outcome = _admitted_update(scratch_rc, user_id, 3101)
    assert outcome is tg.AdmissionOutcome.ADMITTED

    run_on(scratch_rc, lambda db: cv.delete_conversation(db, user_id, conversation_id))

    assert scalar(
        monitor_rc,
        "SELECT count(*) FROM telegram_update_admissions WHERE bot_id = :bot AND update_id = 3101",
        bot=BOT,
    ) == 1
    assert run_on(
        scratch_rc,
        lambda db: tg.update_admission_status(
            db, BOT, user_id, 3101, tg.update_key(3101)
        ),
    ) is tg.AdmissionStatus.ADMITTED_GONE
    with pytest.raises(tg.UpdateAlreadyAdmitted):
        run_on(
            scratch_rc,
            lambda db: tg.conversation_for_message(
                db,
                user_id,
                tg.update_key(3101),
                now=NOW,
                bot_id=BOT,
                update_id=3101,
            ),
        )
    assert scalar(monitor_rc, "SELECT count(*) FROM conversations") == 0


def test_two_concurrent_admissions_on_postgresql_leave_exactly_one(scratch_rc, monitor_rc):
    """The database's primary key is what serializes two admissions of one update: the second
    INSERT waits on the first transaction's uncommitted row, and after the commit it is a
    unique violation, which admit_update reports as ALREADY_ADMITTED (inside its SAVEPOINT)."""
    user_id = _user_with_link(scratch_rc)
    engine = scratch_rc.engine()
    entered, proceed, second_started = threading.Event(), threading.Event(), threading.Event()
    outcomes = {}
    done = threading.Event()
    real = tg._insert_update_marker

    def paused_insert(session, bot_id, update_id, admitted_at):
        first = real(session, bot_id, update_id, admitted_at)
        if threading.current_thread().name == "first-admission":
            entered.set()
            proceed.wait(10)
        return first

    try:
        factory = create_session_factory(engine)

        def first():
            with factory() as db, db.begin():
                outcomes["first"] = tg.admit_update(
                    db, ACCOUNT, user_id, 3102, bot_id=BOT, now=NOW, existing=False
                )

        def second():
            second_started.set()
            with factory() as db, db.begin():
                outcomes["second"] = tg.admit_update(
                    db, ACCOUNT, user_id, 3102, bot_id=BOT, now=NOW, existing=False
                )
            done.set()

        import house_helper.persistence.telegram as module

        original = module._insert_update_marker
        module._insert_update_marker = paused_insert
        try:
            one = threading.Thread(target=first, name="first-admission")
            one.start()
            assert entered.wait(10)
            two = threading.Thread(target=second, name="second-admission")
            two.start()
            assert second_started.wait(10)
            assert _wait_until_locked_out_or_done(monitor_rc, done) is True  # the INSERT waits
            proceed.set()
            one.join(30)
            two.join(30)
            assert not one.is_alive() and not two.is_alive()
        finally:
            module._insert_update_marker = original
    finally:
        engine.dispose()

    assert outcomes["first"] is tg.AdmissionOutcome.ADMITTED
    assert outcomes["second"] is tg.AdmissionOutcome.ALREADY_ADMITTED
    assert scalar(
        monitor_rc,
        "SELECT count(*) FROM telegram_update_admissions WHERE update_id = 3102",
    ) == 1


def test_the_cutover_and_backfill_on_postgresql(scratch_rc, monitor_rc):
    from house_helper.persistence.models import User

    def seed(db):
        user = new_user(db)
        conversation = new_conversation(db, user, created_at=NOW, updated_at=NOW)
        new_turn_request(
            db, conversation, tg.update_key(3201), created_at=NOW - timedelta(hours=1)
        )
        new_turn_request(
            db, conversation, tg.update_key(3202), created_at=NOW - timedelta(hours=49)
        )

    run_on(scratch_rc, seed)

    first = run_on(
        scratch_rc, lambda db: tg.initialize_update_admissions(db, BOT, now=NOW)
    )
    later = run_on(
        scratch_rc,
        lambda db: tg.initialize_update_admissions(db, BOT, now=NOW + timedelta(hours=1)),
    )

    assert first.cutover_at == NOW and later.cutover_at == NOW
    assert first.backfilled == 1 and later.backfilled == 0
    assert scalar(
        monitor_rc,
        "SELECT count(*) FROM telegram_update_admissions WHERE kind = 'update'",
    ) == 1
    assert scalar(
        monitor_rc,
        "SELECT count(*) FROM telegram_update_admissions "
        "WHERE kind = 'cutover' AND update_id = 0 AND expires_at IS NULL",
    ) == 1


def test_the_purge_deletes_aged_markers_and_keeps_the_cutover_row_on_postgresql(scratch_rc, monitor_rc):
    def seed(db):
        db.add(
            TelegramUpdateAdmission(
                bot_id=BOT,
                update_id=3301,
                kind="update",
                admitted_at=NOW - timedelta(hours=49),
                expires_at=NOW - timedelta(hours=1),
            )
        )
        db.add(
            TelegramUpdateAdmission(
                bot_id=BOT,
                update_id=TELEGRAM_CUTOVER_UPDATE_ID,
                kind="cutover",
                admitted_at=NOW - timedelta(days=30),
                expires_at=None,
            )
        )

    run_on(scratch_rc, seed)

    engine = scratch_rc.engine()
    try:
        report = purge_expired(engine, now=NOW, pause=lambda: None)
    finally:
        engine.dispose()

    assert report.deleted["telegram_update_admissions"] == 1
    assert scalar(
        monitor_rc, "SELECT count(*) FROM telegram_update_admissions"
    ) == 1  # the cutover row


def test_a_preparation_that_fails_part_way_commits_nothing_and_can_be_retried_on_postgresql(
    scratch_rc, monitor_rc
):
    """The cutover row and the backfilled markers are one transaction on a real server too:
    a failure halfway leaves no row (not even the cutover), and the retry writes the pinned
    process start rather than its own instant."""
    from house_helper.persistence.models import User

    def seed(db):
        user = new_user(db)
        first = new_conversation(db, user, created_at=NOW, updated_at=NOW)
        second = new_conversation(db, user, created_at=NOW, updated_at=NOW)
        new_turn_request(
            db, first, tg.update_key(3401), created_at=NOW - timedelta(hours=2)
        )
        new_turn_request(
            db, second, tg.update_key(3402), created_at=NOW - timedelta(hours=1)
        )

    run_on(scratch_rc, seed)

    real = tg._insert_update_marker
    made = {"markers": 0}

    def failing(session, bot_id, update_id, admitted_at):
        made["markers"] += 1
        if made["markers"] == 2:
            raise RuntimeError("the storage went away")
        return real(session, bot_id, update_id, admitted_at)

    pinned = NOW - timedelta(hours=3)
    tg._insert_update_marker = failing
    try:
        with pytest.raises(RuntimeError):
            run_on(
                scratch_rc,
                lambda db: tg.initialize_update_admissions(
                    db, BOT, now=NOW, cutover_at=pinned
                ),
            )
    finally:
        tg._insert_update_marker = real

    assert scalar(monitor_rc, "SELECT count(*) FROM telegram_update_admissions") == 0

    ledger = run_on(
        scratch_rc,
        lambda db: tg.initialize_update_admissions(db, BOT, now=NOW, cutover_at=pinned),
    )

    assert ledger.cutover_at == pinned and ledger.backfilled == 2
    assert scalar(monitor_rc, "SELECT count(*) FROM telegram_update_admissions") == 3
    assert scalar(
        monitor_rc,
        "SELECT admitted_at FROM telegram_update_admissions "
        "WHERE kind = 'cutover' AND update_id = 0",
    ) == pinned


def test_a_resolution_told_the_update_was_admitted_refuses_before_creating_on_postgresql(
    scratch_rc, monitor_rc
):
    """The provenance parameter alone (no marker, no turn request) refuses a resolution on
    the real server: nothing is created and the selection is untouched."""
    from house_helper.persistence.models import User

    user_id = _user_with_link(scratch_rc)

    def seed(db):
        selected = new_conversation(
            db, db.get(User, user_id), created_at=NOW, updated_at=NOW
        )
        tg.select_conversation(db, user_id, selected.id, now=NOW)
        return selected.id

    selected_id = run_on(scratch_rc, seed)
    before = scalar(monitor_rc, "SELECT count(*) FROM conversations")

    with pytest.raises(tg.UpdateAlreadyAdmitted):
        run_on(
            scratch_rc,
            lambda db: tg.conversation_for_message(
                db,
                user_id,
                tg.update_key(3403),
                now=NOW,
                telegram_user_id=ACCOUNT,
                bot_id=BOT,
                update_id=3403,
                admitted_here=True,
            ),
        )

    assert scalar(monitor_rc, "SELECT count(*) FROM conversations") == before
    assert scalar(
        monitor_rc,
        "SELECT conversation_id FROM telegram_active_conversations WHERE user_id = :user",
        user=user_id,
    ) == selected_id
