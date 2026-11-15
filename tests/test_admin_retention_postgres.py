"""`purge-expired` against a real PostgreSQL server. Opt-in: skipped unless configured.

The deployment is SQLite; the command is written against SQLAlchemy and does not name a
dialect (the only SQLite-specific part is a per-connection setting that other dialects skip).
This shows it on PostgreSQL 16 rather than leaving that to be believed: the same worlds as
tests/test_admin_retention.py, the same independent oracle, the foreign keys of the real
schema (NO ACTION between requests and messages is a PostgreSQL check at the end of the
statement), and two runs at once.

R2-03: the engine that runs `purge-expired` is the PURGE login's, never the application
engine. The test data is seeded and read with the application login (which may write and
read every table), the rows a run must skip are locked by the application, and the purge
runs get their own engines. The command's execution boundary therefore really measures the
purge identity's privileges.

To run, see the docstring of tests/test_admin_postgres.py (a throwaway server on THIS machine,
on a port other than 5432; these tests create and drop databases of their own on it, through
the harness of tests/postgres_harness.py that keeps them from going anywhere else):

    pytest tests/test_admin_retention_postgres.py -v -rs
"""

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa

from house_helper.admin import retention
from house_helper.persistence.database import create_session_factory
from tests.postgres_harness import POSTGRES_URL_ENV
from tests.retention_support import NOW, Seed
from tests.test_admin_postgres import pg_run, scratch, template  # noqa: F401  (fixtures)
from tests.test_admin_retention import (
    oracle,
    random_world,
    seed_answered_request,
    seed_artifacts,
    seed_history,
    seed_in_flight,
    seed_invitation_with_expired_login,
    seed_linked_request,
    seed_old_conversation_with_one_note,
)
from tests.test_admin_retention_operations import fill_bulk, run_in_threads

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_URL_ENV),
        reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL (see tests/test_admin_postgres.py)",
    ),
]

TABLES = ("sessions", "oidc_states", "email_login_challenges", "identity_invitations", "turn_requests", "messages", "conversations")


def seeded(engine, fill):
    with create_session_factory(engine)() as session, session.begin():
        return fill(Seed(session))


def fetch(engine, sql):
    with engine.connect() as connection:
        return [tuple(row) for row in connection.execute(sa.text(sql))]


def present(engine, table):
    return {row[0] for row in fetch(engine, f"SELECT id FROM {table}")}


def purge(engine, **options):
    options.setdefault("pause", lambda: None)
    return retention.purge_expired(engine, now=NOW, **options)


def check(engine, gone, kept):
    for table, rows in gone.items():
        assert not ({r.id for r in rows} & present(engine, table)), table
    for table, rows in kept.items():
        assert {r.id for r in rows} <= present(engine, table), table


def test_artifacts_and_history_go_as_they_do_on_sqlite_and_the_foreign_keys_hold(scratch):
    app, purger = scratch.app_engine(), scratch.purge_engine()
    try:
        gone_a, kept_a = seeded(app, seed_artifacts)
        gone_h, kept_h = seeded(app, seed_history)
        seeded(app, seed_in_flight)

        report = purge(purger)

        check(app, {**gone_a, **gone_h}, {**kept_a, **kept_h})
        for table, rows in {**gone_a, **gone_h}.items():
            assert report.deleted[table] >= len(rows), table
        assert report.cancelled_in_flight == 3 and report.held_back_messages == 1
        assert purge(purger).total == 0  # a second run changes nothing
        # The database itself is consistent: every foreign key is satisfied.
        assert fetch(app, "SELECT count(*) FROM turn_requests r LEFT JOIN messages m ON m.id = r.user_message_id "
                          "WHERE m.id IS NULL") == [(0,)]
    finally:
        app.dispose()
        purger.dispose()


def test_a_dry_run_changes_nothing_and_predicts_the_real_run(scratch):
    app, purger = scratch.app_engine(), scratch.purge_engine()
    try:
        seeded(app, seed_artifacts)
        seeded(app, seed_history)
        seeded(app, seed_in_flight)
        before = {t: fetch(app, f"SELECT count(*) FROM {t}") for t in TABLES}

        preview = purge(purger, dry_run=True)

        assert {t: fetch(app, f"SELECT count(*) FROM {t}") for t in TABLES} == before
        real = purge(purger)
        assert real.deleted == preview.deleted and real.total > 0
        assert (real.cancelled_in_flight, real.held_back_messages) == (
            preview.cancelled_in_flight,
            preview.held_back_messages,
        )
    finally:
        app.dispose()
        purger.dispose()


@pytest.mark.parametrize("seed", range(8))
def test_random_worlds_agree_with_the_oracle(scratch, seed):
    app, purger = scratch.app_engine(), scratch.purge_engine()
    try:
        rng = random.Random(seed)
        seeded(app, lambda s: random_world(s, rng))
        expected_requests, expected_messages, expected_conversations = oracle(lambda sql: fetch(app, sql), NOW)
        before = {t: present(app, t) for t in ("turn_requests", "messages", "conversations")}

        preview = purge(purger, dry_run=True)
        report = purge(purger, batch_size=3)

        assert before["turn_requests"] - present(app, "turn_requests") == expected_requests
        assert before["messages"] - present(app, "messages") == expected_messages
        assert before["conversations"] - present(app, "conversations") == expected_conversations
        assert report.deleted == preview.deleted
        assert purge(purger).total == 0
    finally:
        app.dispose()
        purger.dispose()


# --- a row that is locked is skipped, and what depends on it is not deleted under it ----------------------
#
# Every transaction of these runs gives up on a lock after a second instead of waiting for it. A run
# that is correct never waits for a row another transaction holds (that is what SKIP LOCKED is for), so
# a wait here is the failure, reported as such, and not a hang of the test.

LOCK_TIMEOUT = "1s"


def never_wait_for_a_lock(engine):
    @sa.event.listens_for(engine, "begin")
    def give_up_instead_of_waiting(connection):
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")


class lock_held:
    """Another transaction holds the row locks that `sql` (a SELECT ... FOR UPDATE) takes, not
    committed, until the block ends or `release()` is called: the other run, or the application, in
    the middle of a write. Held with the APPLICATION login, as the web process would."""

    def __init__(self, scratch, sql, **params):
        self.engine = scratch.app_engine()
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        try:
            assert self.connection.execute(sa.text(sql), params).all(), "the lock was taken on no row"
        except BaseException:
            self.release()
            raise

    def release(self):
        if self.transaction is not None:
            self.transaction.rollback()
            self.connection.close()
            self.engine.dispose()
            self.transaction = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.release()


def row_counts(engine):
    return {table: fetch(engine, f"SELECT count(*) FROM {table}")[0][0] for table in TABLES}


def assert_reported_is_actual(before, after, *reports):
    """Per table, the rows that are gone are exactly the rows the runs say they deleted: nothing
    went by a cascade, or by any way the reports do not count."""
    for table in TABLES:
        assert before[table] - after[table] == sum(report.deleted[table] for report in reports), table


def assert_no_orphans(engine):
    assert fetch(engine, "SELECT count(*) FROM turn_requests r WHERE NOT EXISTS "
                         "(SELECT 1 FROM messages m WHERE m.id = r.user_message_id)") == [(0,)]
    assert fetch(engine, "SELECT count(*) FROM turn_requests r WHERE r.assistant_message_id IS NOT NULL AND NOT EXISTS "
                         "(SELECT 1 FROM messages m WHERE m.id = r.assistant_message_id)") == [(0,)]
    assert fetch(engine, "SELECT count(*) FROM messages m WHERE NOT EXISTS "
                         "(SELECT 1 FROM conversations c WHERE c.id = m.conversation_id)") == [(0,)]
    assert fetch(engine, "SELECT count(*) FROM oidc_states s WHERE s.invitation_id IS NOT NULL AND NOT EXISTS "
                         "(SELECT 1 FROM identity_invitations i WHERE i.id = s.invitation_id)") == [(0,)]


def test_a_run_skips_a_request_another_run_is_cancelling_and_leaves_its_messages_and_conversation(scratch):
    # The reproduction of the audit finding: the first run holds the request (it has cancelled it
    # and not yet committed); the second skips it, and must not then delete the question the request
    # links, as if the request were already gone. Then the first run finishes, and nothing is lost.
    app = scratch.app_engine()
    first_engine, second_engine = scratch.purge_engine(), scratch.purge_engine()
    try:
        world = seeded(app, seed_linked_request)
        for engine in (first_engine, second_engine):
            never_wait_for_a_lock(engine)
        before = row_counts(app)
        cancelled, may_commit = threading.Event(), threading.Event()

        @sa.event.listens_for(first_engine, "after_cursor_execute")
        def hold_the_cancellation_open(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE turn_requests"):
                cancelled.set()  # the row is updated and locked; the transaction is not committed
                assert may_commit.wait(60), "the test never let the first run commit"

        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(purge, first_engine)
            try:
                assert cancelled.wait(60), "the first run never reached the cancellation"
                second = purge(second_engine)  # (PurgeAborted before the correction: the foreign key)
                during = row_counts(app)
                the_chain_is_intact = (
                    present(app, "turn_requests") == {world.request.id}
                    and present(app, "messages") == {world.asked.id}
                    and present(app, "conversations") == {world.conversation.id}
                )
            finally:
                may_commit.set()
            first = first.result(timeout=60)

        assert the_chain_is_intact  # the unlinked note went; the linked question, its request and the conversation did not
        assert (second.cancelled_in_flight, second.held_back_messages) == (0, 1)
        assert second.deleted["turn_requests"] == 0 and second.deleted["messages"] == 1
        assert second.deleted["conversations"] == 0
        assert (first.cancelled_in_flight, first.deleted["turn_requests"]) == (1, 1)
        assert first.deleted["messages"] == 1 and first.deleted["conversations"] == 1
        after = row_counts(app)
        assert (during["turn_requests"], during["messages"], during["conversations"]) == (1, 1, 1)
        assert (after["turn_requests"], after["messages"], after["conversations"]) == (0, 0, 0)
        assert_reported_is_actual(before, after, first, second)
        assert_no_orphans(app)
        assert purge(second_engine).total == 0 and row_counts(app) == after  # idempotent
    finally:
        app.dispose()
        first_engine.dispose()
        second_engine.dispose()


def test_what_a_run_had_to_skip_is_deleted_by_the_next_run(scratch):
    app, purger = scratch.app_engine(), scratch.purge_engine()
    try:
        world = seeded(app, seed_linked_request)
        never_wait_for_a_lock(purger)
        before = row_counts(app)

        with lock_held(scratch, "SELECT id FROM turn_requests WHERE id = :id FOR UPDATE", id=world.request.id):
            skipped = purge(purger)
            assert present(app, "turn_requests") == {world.request.id}
            assert present(app, "messages") == {world.asked.id}
            assert present(app, "conversations") == {world.conversation.id}
        assert skipped.cancelled_in_flight == 0 and skipped.deleted["turn_requests"] == 0
        assert skipped.deleted["messages"] == 1 and skipped.deleted["conversations"] == 0  # only the unlinked note

        later = purge(purger)  # the lock is gone: the request is cancelled and deleted, then what it held

        assert later.cancelled_in_flight == 1
        assert (later.deleted["turn_requests"], later.deleted["messages"], later.deleted["conversations"]) == (1, 1, 1)
        after = row_counts(app)
        assert_reported_is_actual(before, after, skipped, later)
        assert_no_orphans(app)
        assert purge(purger).total == 0 and row_counts(app) == after
    finally:
        app.dispose()
        purger.dispose()


def test_a_request_that_was_skipped_while_locked_still_keeps_its_message_when_the_lock_is_gone(scratch):
    # The symptom the audit saw: the request is skipped by its own stage (it was locked), the lock
    # is released before the messages stage, and the request, eligible but still there, is judged
    # as if it were gone. The foreign key then refused the delete of the message it links.
    app, purger = scratch.app_engine(), scratch.purge_engine()
    try:
        world = seeded(app, seed_linked_request)
        never_wait_for_a_lock(purger)
        before = row_counts(app)

        with lock_held(scratch, "SELECT id FROM turn_requests WHERE id = :id FOR UPDATE", id=world.request.id) as lock:

            @sa.event.listens_for(purger, "after_cursor_execute")
            def release_after_the_requests_stage(connection, cursor, statement, parameters, context, executemany):
                if statement.startswith("DELETE FROM turn_requests"):
                    lock.release()  # the stage has skipped the row; from here nothing holds it

            skipped = purge(purger)  # (PurgeAborted, a ForeignKeyViolation, before the correction)

        assert present(app, "turn_requests") == {world.request.id}  # eligible, and still there
        assert present(app, "messages") == {world.asked.id}
        assert present(app, "conversations") == {world.conversation.id}
        assert skipped.deleted["turn_requests"] == 0 and skipped.deleted["conversations"] == 0
        assert skipped.deleted["messages"] == 1 and skipped.held_back_messages == 1

        later = purge(purger)

        assert later.cancelled_in_flight == 1
        assert (later.deleted["turn_requests"], later.deleted["messages"], later.deleted["conversations"]) == (1, 1, 1)
        after = row_counts(app)
        assert_reported_is_actual(before, after, skipped, later)
        assert_no_orphans(app)
        assert purge(purger).total == 0 and row_counts(app) == after
    finally:
        app.dispose()
        purger.dispose()


def test_the_question_and_the_answer_of_a_locked_request_stay_with_it_and_go_with_it_later(scratch):
    app, purger = scratch.app_engine(), scratch.purge_engine()
    try:
        exchange, conversation = seeded(app, seed_answered_request)
        never_wait_for_a_lock(purger)
        before = row_counts(app)

        with lock_held(scratch, "SELECT id FROM turn_requests WHERE id = :id FOR UPDATE", id=exchange.request.id):
            skipped = purge(purger)
            assert present(app, "turn_requests") == {exchange.request.id}
            assert present(app, "messages") == {exchange.asked.id, exchange.answer.id}
            assert present(app, "conversations") == {conversation.id}
        assert skipped.total == 0 and skipped.held_back_messages == 2

        later = purge(purger)

        assert (later.deleted["turn_requests"], later.deleted["messages"], later.deleted["conversations"]) == (1, 2, 1)
        assert later.cancelled_in_flight == 0  # it was complete: deleted as it was
        after = row_counts(app)
        assert_reported_is_actual(before, after, skipped, later)
        assert_no_orphans(app)
        assert purge(purger).total == 0
    finally:
        app.dispose()
        purger.dispose()


def test_a_dry_run_is_a_snapshot_and_a_run_that_has_to_skip_a_locked_row_deletes_less(scratch):
    app, purger = scratch.app_engine(), scratch.purge_engine()
    try:
        world = seeded(app, seed_linked_request)
        never_wait_for_a_lock(purger)

        with lock_held(scratch, "SELECT id FROM turn_requests WHERE id = :id FOR UPDATE", id=world.request.id):
            preview = purge(purger, dry_run=True)  # it reads: a lock does not stop it, and it does not see one
            real = purge(purger)

        # The preview counts what the stages would delete if nothing were held; the run skipped the request.
        assert (preview.deleted["turn_requests"], preview.deleted["messages"], preview.deleted["conversations"]) == (1, 2, 1)
        assert (real.deleted["turn_requests"], real.deleted["messages"], real.deleted["conversations"]) == (0, 1, 0)
        assert purge(purger).total == preview.total - real.total  # what the next run adds makes up the difference
    finally:
        app.dispose()
        purger.dispose()


def test_an_invitation_stays_while_a_login_state_bound_to_it_is_locked_and_goes_with_it_later(scratch):
    app, purger = scratch.app_engine(), scratch.purge_engine()
    try:
        world = seeded(app, seed_invitation_with_expired_login)
        never_wait_for_a_lock(purger)
        before = row_counts(app)

        with lock_held(scratch, "SELECT id FROM oidc_states WHERE id = :id FOR UPDATE", id=world.state.id):
            skipped = purge(purger)  # (before the correction: the invitation's cascade waited for the lock)
            assert present(app, "identity_invitations") == {world.invitation.id}
            assert present(app, "oidc_states") == {world.state.id}
        assert skipped.deleted["oidc_states"] == 0 and skipped.deleted["identity_invitations"] == 0

        later = purge(purger)

        assert (later.deleted["oidc_states"], later.deleted["identity_invitations"]) == (1, 1)
        after = row_counts(app)
        assert_reported_is_actual(before, after, skipped, later)
        assert_no_orphans(app)
        assert purge(purger).total == 0
    finally:
        app.dispose()
        purger.dispose()


def test_a_conversation_stays_while_a_message_in_it_is_locked_and_goes_with_it_later(scratch):
    app, purger = scratch.app_engine(), scratch.purge_engine()
    try:
        world = seeded(app, seed_old_conversation_with_one_note)
        never_wait_for_a_lock(purger)
        before = row_counts(app)

        with lock_held(scratch, "SELECT id FROM messages WHERE id = :id FOR UPDATE", id=world.note.id):
            skipped = purge(purger)  # (before the correction: the conversation's cascade waited for the lock)
            assert present(app, "conversations") == {world.conversation.id}
            assert present(app, "messages") == {world.note.id}
        assert skipped.deleted["messages"] == 0 and skipped.deleted["conversations"] == 0

        later = purge(purger)

        assert (later.deleted["messages"], later.deleted["conversations"]) == (1, 1)
        after = row_counts(app)
        assert_reported_is_actual(before, after, skipped, later)
        assert_no_orphans(app)
        assert purge(purger).total == 0
    finally:
        app.dispose()
        purger.dispose()


def test_several_runs_at_once_delete_every_row_exactly_once_between_them(scratch):
    setup = scratch.app_engine()
    try:
        fill_bulk(setup, sessions=600, messages=600)
    finally:
        setup.dispose()

    def one_run():
        engine = scratch.purge_engine()  # a purge engine, and so connections, of its own
        try:
            return purge(engine, batch_size=7, pause=lambda: time.sleep(0.0005))
        finally:
            engine.dispose()

    reports = run_in_threads(one_run, one_run, one_run, one_run)

    assert sum(report.deleted["sessions"] for report in reports) == 600
    assert sum(report.deleted["messages"] for report in reports) == 600
    check_engine = scratch.app_engine()
    try:
        assert fetch(check_engine, "SELECT count(*) FROM sessions") == [(0,)]
        assert fetch(check_engine, "SELECT count(*) FROM messages") == [(1,)]  # the recent one
    finally:
        check_engine.dispose()
