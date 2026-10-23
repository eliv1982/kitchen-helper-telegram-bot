"""`purge-expired` as an operator runs it: the command line, its failures, and how it lives
beside the rest of the deployment.

  * the report is counts and two timestamps, and no option chooses the time or the scope;
  * a run that is refused (schema not at the head, no database, the database stays locked) or
    fails or is interrupted leaves a sound database, says how much its committed batches had
    already deleted, exits non-zero, and the same command finishes the job;
  * the work is bounded: each batch is one write statement that comes first in its own short
    transaction, and no batch deletes more than the batch size;
  * nothing private reaches the output, the exit message of a failure, or a log record;
  * two runs at once, and a run beside the application's own writers, lose and duplicate
    nothing;
  * under the operator tool's lock model (deploy/ops/dbtool.py): `exec --shared` runs it beside
    the web service, a migration or restore (exclusive) is refused while it runs, and it is
    refused while a restore holds the directory (POSIX file locks: these run on Linux).
"""

import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.admin import cli, retention
from house_helper.admin.cli import INTERRUPTED, ExitCode
from house_helper.admin.target import open_target
from house_helper.persistence import authn
from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    create_db_engine,
    create_session_factory,
)
from house_helper.persistence.models import Message, Session
from tests.admin_support import db_path, db_url, migrated_template, run_admin  # noqa: F401  (fixtures)
from tests.deploy_support import (
    BEHIND,
    DBTOOL_PATH,
    UNCOMMITTED_WRITER,
    child_environ,
    lock_is_taken,
    posix_only,
    run_tool,
    set_revision,
    start_python,
    start_web_service,
)
from tests.retention_support import (
    NOW,
    Seed,
    ago,
    count,
    dump,
    integrity,
    seeded,
)


def clock():
    return NOW


def purge_cli(capsys, url, *argv):
    return run_admin(capsys, url, "purge-expired", *argv, clock=clock)


def purge_engine(url, listeners=None, **options):
    """The purge, in this process, on an engine a test may attach listeners to first."""
    engine = open_target({DATABASE_URL_ENV: url})
    if listeners:
        listeners(engine)
    options.setdefault("pause", lambda: None)
    try:
        return retention.purge_expired(engine, now=NOW, **options)
    finally:
        engine.dispose()


def expired_world(s: Seed, sessions=3, messages=4):
    """Some of everything that has expired, and a few things that have not."""
    user = s.user()
    link = s.telegram(user)
    for _ in range(sessions):
        s.login_session(user, link, created=ago(days=20), expires=ago(days=6))
    s.login_session(user, link, created=ago(days=1), expires=NOW + timedelta(days=13))
    s.login_state(created=ago(hours=2), expires=ago(hours=1))
    conversation = s.conversation(user, created=ago(days=500), updated=ago(days=2))
    exchange = s.exchange(conversation, created=ago(days=400))
    for index in range(messages):
        s.message(conversation, "user", f"old {index}", created=ago(days=380 + index))
    s.message(conversation, "user", "recent", created=ago(days=1))
    return exchange


def parse_report(out: str) -> dict:
    return {name: int(number) for name, number in re.findall(r"^  (\w+): (\d+)$", out, re.MULTILINE)}


# --- the command line --------------------------------------------------------------------------


def test_the_report_is_counts_and_the_two_timestamps(db_url, capsys):
    seeded(db_url, expired_world)

    run = purge_cli(capsys, db_url, "--dry-run")
    assert run.code == ExitCode.OK and run.err == ""
    first = run.out.splitlines()[0]
    assert first.startswith("ok [dry_run]: would delete ") and first.endswith("Nothing was changed.")
    assert "2025-10-23T03:30:00Z" in first and "2026-10-23T03:30:00Z" in first
    predicted = parse_report(run.out)
    assert set(predicted) == {c.value for c in retention.Category}
    assert count(db_url, "sessions") == 4  # 3 expired and 1 live: nothing was deleted

    run = purge_cli(capsys, db_url)
    assert run.code == ExitCode.OK and run.err == ""
    assert run.out.splitlines()[0].startswith("ok [purged]: deleted ")
    assert parse_report(run.out) == predicted
    assert count(db_url, "sessions") == 1

    run = purge_cli(capsys, db_url)
    assert run.code == ExitCode.OK
    assert run.out.splitlines()[0].startswith("ok [unchanged]: nothing has expired")
    assert set(parse_report(run.out).values()) == {0}


def test_no_option_chooses_the_time_or_the_scope_and_a_bad_line_is_not_echoed(db_url, capsys):
    seeded(db_url, expired_world)
    before = dump(db_url)
    for argv in (["--now", "2099-01-01"], ["--days", "1"], ["--messages"], ["--batch-size", "1"], ["2099-01-01"]):
        run = purge_cli(capsys, db_url, *argv)
        assert run.code == ExitCode.USAGE and run.out == ""
        assert re.search(r"^usage \[arguments\]: ", run.err, re.MULTILINE)
        assert all(token not in run.text for token in argv)  # what was typed is not echoed back
    assert dump(db_url) == before


def test_the_help_names_the_dry_run_and_what_is_never_touched(capsys):
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["purge-expired", "--help"])
    text = capsys.readouterr().out
    for needed in ("--dry-run", "365", "counts only", "Nothing else is touched"):
        assert needed in " ".join(text.split()), needed


def test_the_command_uses_the_clock_it_is_given_and_the_application_clock_otherwise(db_url, capsys, monkeypatch):
    seeded(db_url, expired_world)
    early = run_admin(capsys, db_url, "purge-expired", "--dry-run", clock=lambda: ago(days=10 * 365))
    assert early.code == 0 and set(parse_report(early.out).values()) == {0}  # ten years back, nothing is old
    late = run_admin(capsys, db_url, "purge-expired", "--dry-run", clock=lambda: NOW + timedelta(days=30))
    assert parse_report(late.out)["sessions"] == 4  # a month on, the live session has expired as well
    monkeypatch.setattr(cli, "utcnow", lambda: NOW)  # no clock given: the persistence layer's UTC clock
    default = run_admin(capsys, db_url, "purge-expired", "--dry-run")
    assert parse_report(default.out) == parse_report(purge_cli(capsys, db_url, "--dry-run").out)


# --- refusals: the database is not one this command acts on -----------------------------------


@pytest.mark.parametrize("argv", [[], ["--dry-run"]])
def test_a_database_that_is_not_at_the_head_is_refused_and_nothing_is_deleted(db_url, db_path, capsys, argv):
    seeded(db_url, expired_world)
    set_revision(db_path, BEHIND)
    before = dump(db_url)

    run = purge_cli(capsys, db_url, *argv)

    assert run.code == ExitCode.DATABASE and run.out == ""
    assert re.match(r"error \[schema_not_head\]: ", run.err)
    assert dump(db_url) == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_the_function_checks_the_schema_itself_not_only_the_command_line_before_it(db_url, db_path, dry_run):
    # The command line refuses a database that is not at the head before it calls the function
    # (above). The function is the second line: an engine that did not come from that check, or
    # a schema that changed in between, meets the same refusal and nothing is deleted.
    from house_helper.admin.target import TargetError

    seeded(db_url, expired_world)
    set_revision(db_path, BEHIND)
    before = dump(db_url)
    engine = create_db_engine(db_url)
    try:
        with pytest.raises(TargetError) as refused:
            retention.purge_expired(engine, now=NOW, dry_run=dry_run, pause=lambda: None)
    finally:
        engine.dispose()
    assert refused.value.code == "schema_not_head"
    assert dump(db_url) == before


def test_a_missing_database_and_a_missing_setting_are_refused_without_creating_anything(tmp_path, capsys):
    missing = tmp_path / "nowhere.db"
    run = purge_cli(capsys, f"sqlite:///{missing}")
    assert run.code == ExitCode.DATABASE and "error [database_missing]" in run.err
    assert not missing.exists()
    run = purge_cli(capsys, None)
    assert run.code == ExitCode.DATABASE and "error [not_configured]" in run.err


def test_a_connection_that_does_not_enforce_foreign_keys_is_refused_before_anything_is_written(db_url):
    seeded(db_url, expired_world)
    before = dump(db_url)
    engine = sa.create_engine(db_url)  # no persistence-layer hook: SQLite's own default, keys off
    try:
        with pytest.raises(RuntimeError):
            retention.purge_expired(engine, now=NOW)
    finally:
        engine.dispose()
    assert dump(db_url) == before


def test_a_database_that_stays_locked_is_a_refusal_not_a_hang_and_the_next_run_finishes(
    db_url, db_path, capsys, monkeypatch
):
    seeded(db_url, expired_world)
    before = dump(db_url)
    monkeypatch.setattr(retention, "BUSY_TIMEOUT_MS", 200)
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # another writer, in the middle of its transaction
    started = time.monotonic()
    try:
        run = purge_cli(capsys, db_url)
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert time.monotonic() - started < 20
    assert run.code == ExitCode.REFUSED and run.out == ""
    assert re.match(r"refused \[database_busy\]: ", run.err)
    assert "Run the same command again" in run.err and "database is locked" not in run.err
    assert dump(db_url) == before

    run = purge_cli(capsys, db_url)
    assert run.code == ExitCode.OK and count(db_url, "sessions") == 1


# --- failures and interrupts: a sound database, an honest message, a finished second run ------


def flaky_batches(monkeypatch, *, fail_on: str, error):
    """_delete_batch raises `error` for the table `fail_on`, after the stages before it ran."""
    real = retention._delete_batch

    def delete_batch(table, condition, size):
        if table.name == fail_on:
            raise error
        return real(table, condition, size)

    monkeypatch.setattr(retention, "_delete_batch", delete_batch)


def test_a_failure_in_a_later_stage_keeps_the_earlier_stages_and_a_rerun_finishes(
    db_url, db_path, tmp_path, capsys, monkeypatch
):
    control = tmp_path / "control.db"
    shutil.copyfile(db_path, control)  # the same empty, migrated database, for a run that never fails
    control_url = f"sqlite:///{control}"
    seeded(db_url, expired_world)
    seeded(control_url, expired_world)
    purge_engine(control_url)
    flaky_batches(monkeypatch, fail_on="messages", error=RuntimeError("boom CANARY-EXCEPTION-TEXT"))

    run = purge_cli(capsys, db_url)

    assert run.code == ExitCode.INTERNAL and run.out == ""
    assert re.match(r"error \[purge_failed\]: unexpected RuntimeError in stage messages: ", run.err)
    assert "CANARY-EXCEPTION-TEXT" not in run.text
    assert "had already been deleted by batches that committed" in run.err
    assert "Run the same command again" in run.err
    assert count(db_url, "sessions") == 1 and count(db_url, "turn_requests") == 0  # the earlier stages are done
    assert count(db_url, "messages") == 7  # the failed stage and the ones after it are not
    integrity(db_url)

    monkeypatch.undo()
    run = purge_cli(capsys, db_url)
    assert run.code == ExitCode.OK
    tables = ("sessions", "oidc_states", "turn_requests", "messages", "conversations")
    assert {t: count(db_url, t) for t in tables} == {t: count(control_url, t) for t in tables}
    integrity(db_url)


def test_a_statement_that_fails_after_it_ran_is_rolled_back_whole(db_url):
    seeded(db_url, expired_world)
    messages_before = count(db_url, "messages")

    def listeners(engine):
        @sa.event.listens_for(engine, "after_cursor_execute")
        def explode(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("DELETE FROM messages") and cursor.rowcount > 0:
                raise RuntimeError("the disk went away")

    with pytest.raises(retention.PurgeAborted) as aborted:
        purge_engine(db_url, listeners)

    assert aborted.value.stage == "messages"
    assert isinstance(aborted.value.__cause__, RuntimeError)
    assert aborted.value.done["messages"] == 0 and aborted.value.done["sessions"] == 3
    assert count(db_url, "messages") == messages_before  # the batch that ran was not committed
    integrity(db_url)

    assert purge_engine(db_url).deleted["messages"] == 6  # the question and answer, and four old ones
    assert count(db_url, "messages") == 1  # the recent one
    integrity(db_url)


def test_a_commit_that_fails_leaves_the_batch_uncommitted_and_uncounted(db_url):
    seeded(db_url, expired_world)
    messages_before = count(db_url, "messages")
    ran = []

    def listeners(engine):
        @sa.event.listens_for(engine, "after_cursor_execute")
        def noted(connection, cursor, statement, parameters, context, executemany):
            ran.append(statement.startswith("DELETE FROM messages"))

        @sa.event.listens_for(engine, "commit")
        def refuse(connection):
            if ran and ran[-1]:  # the transaction that deleted messages: its COMMIT fails
                raise RuntimeError("the disk went away at COMMIT")

    with pytest.raises(retention.PurgeAborted) as aborted:
        purge_engine(db_url, listeners)

    assert aborted.value.stage == "messages"
    assert aborted.value.done["messages"] == 0  # nothing is reported as deleted that was not committed
    assert aborted.value.done["sessions"] == 3
    assert count(db_url, "messages") == messages_before
    integrity(db_url)


def test_a_connection_that_dies_mid_run_reports_what_was_deleted_and_is_not_reused(db_url):
    seeded(db_url, expired_world)
    engine = open_target({DATABASE_URL_ENV: db_url})

    @sa.event.listens_for(engine, "before_cursor_execute")
    def kill(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM messages"):
            cursor.connection.close()  # the driver connection goes away under the statement

    try:
        with pytest.raises(retention.PurgeAborted) as aborted:
            retention.purge_expired(engine, now=NOW, pause=lambda: None)
        # The error that matters is the one that ended the run, not one from putting settings back.
        assert aborted.value.stage == "messages" and aborted.value.done["sessions"] == 3
        assert type(aborted.value.__cause__).__name__ in {"ProgrammingError", "OperationalError", "InterfaceError"}
        assert engine.pool.checkedout() == 0  # the dead connection was given up, not left out
    finally:
        engine.dispose()
    integrity(db_url)
    assert purge_engine(db_url).total > 0  # the next run, on a fresh connection, finishes the job
    assert count(db_url, "messages") == 1


def test_an_interrupt_between_batches_stops_cleanly_and_says_what_is_gone(db_url):
    seeded(db_url, lambda s: expired_world(s, sessions=5, messages=0))
    pauses = []

    def interrupt():
        pauses.append(1)
        raise KeyboardInterrupt

    with pytest.raises(retention.PurgeAborted) as aborted:
        purge_engine(db_url, batch_size=2, pause=interrupt)

    assert isinstance(aborted.value.__cause__, KeyboardInterrupt)
    assert aborted.value.stage == "sessions" and aborted.value.done["sessions"] == 2
    assert count(db_url, "sessions") == 4  # 5 expired + 1 live, 2 gone
    integrity(db_url)

    assert purge_engine(db_url, batch_size=2).deleted["sessions"] == 3  # the rest, in two more batches
    assert count(db_url, "sessions") == 1


def test_a_driver_error_is_called_busy_only_when_the_driver_says_so_not_because_of_the_sql_it_quotes(
    db_url, capsys, monkeypatch
):
    seeded(db_url, expired_world)
    # SQLAlchemy's text for the error quotes the statement; on PostgreSQL that is "... SKIP LOCKED".
    quoting = sa.exc.OperationalError("SELECT id FROM t FOR UPDATE SKIP LOCKED", {}, Exception("connection refused"))
    flaky_batches(monkeypatch, fail_on="sessions", error=quoting)

    run = purge_cli(capsys, db_url)

    assert run.code == ExitCode.INTERNAL and "purge_failed" in run.err and "database_busy" not in run.err
    assert "OperationalError" in run.err


def test_an_interrupt_ends_the_command_with_the_conventional_status_and_a_message(db_url, capsys, monkeypatch):
    seeded(db_url, expired_world)
    flaky_batches(monkeypatch, fail_on="messages", error=KeyboardInterrupt())

    run = purge_cli(capsys, db_url)

    assert run.code == INTERRUPTED and run.out == ""
    assert re.match(r"error \[interrupted\]: interrupted in stage messages: ", run.err)
    assert "Run the same command again" in run.err


# --- the shape of the work: short, write-first, bounded ---------------------------------------


def watch(engine, statements):
    @sa.event.listens_for(engine, "before_cursor_execute")
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))


def test_every_transaction_that_writes_starts_with_its_write_and_deletes_a_bounded_number(db_url):
    seeded(db_url, lambda s: expired_world(s, sessions=9, messages=9))
    statements, deleted = [], []

    def listeners(engine):
        watch(engine, statements)

        @sa.event.listens_for(engine, "after_cursor_execute")
        def rows(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith(("DELETE", "UPDATE")):
                deleted.append(cursor.rowcount)

    pauses = []
    purge_engine(db_url, listeners, batch_size=4, pause=lambda: pauses.append(1))

    assert max(deleted) <= 4 and sum(deleted) > 8  # many small statements, none over the batch size
    assert len(pauses) >= 3  # and a pause after each full one
    transactions, current = [], None
    for statement in statements:
        if statement == "BEGIN":
            current = []
            transactions.append(current)
        elif current is not None:
            current.append(statement)
    writing = [t for t in transactions if any(s.startswith(("DELETE", "UPDATE")) for s in t)]
    assert writing
    for transaction in writing:
        assert transaction[0].startswith(("DELETE", "UPDATE"))  # the write is the first statement
        assert len(transaction) == 1  # and the only one: a batch decides and deletes at once


def test_the_run_has_secure_delete_and_a_long_busy_wait_while_it_works_and_puts_them_back(db_url):
    seeded(db_url, expired_world)
    engine = open_target({DATABASE_URL_ENV: db_url})
    seen = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def look(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE"):
            raw = cursor.connection
            seen.append(
                (raw.execute("PRAGMA secure_delete").fetchone()[0], raw.execute("PRAGMA busy_timeout").fetchone()[0])
            )

    def settings():
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            return raw.execute("PRAGMA secure_delete").fetchone()[0], raw.execute("PRAGMA busy_timeout").fetchone()[0]

    try:
        before = settings()
        retention.purge_expired(engine, now=NOW, pause=lambda: None)
        after = settings()
    finally:
        engine.dispose()

    assert seen and set(seen) == {(1, retention.BUSY_TIMEOUT_MS)}
    assert after == before  # the pooled connection is left as it was found


# --- deletion in the file ---------------------------------------------------------------------

CANARY = "CANARY-" + uuid.uuid4().hex


def file_bytes(path) -> bytes:
    data = path.read_bytes()
    wal = path.with_name(path.name + "-wal")
    return data + (wal.read_bytes() if wal.exists() else b"")


def seed_canary(s: Seed):
    user = s.user()
    conversation = s.conversation(user, created=ago(days=500), updated=ago(days=1))
    s.message(conversation, "user", CANARY, created=ago(days=400))
    s.message(conversation, "user", "something recent that keeps the conversation", created=ago(days=1))


def test_deleted_text_is_overwritten_in_the_database_file_and_a_plain_delete_may_not_do_it(
    db_url, db_path, tmp_path, migrated_template
):
    seeded(db_url, seed_canary)
    assert CANARY.encode() in file_bytes(db_path)

    purge_engine(db_url)

    folded = sqlite3.connect(db_path)
    folded.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # what the application's own checkpoints do, in time
    folded.close()
    assert CANARY.encode() not in file_bytes(db_path)

    # The control that makes this a test: with secure_delete off, a plain DELETE of the same
    # row leaves the text in the free space of the file.
    control = tmp_path / "control.db"
    shutil.copyfile(migrated_template, control)
    seeded(f"sqlite:///{control}", seed_canary)
    plain = sqlite3.connect(control, isolation_level=None)
    plain.execute("PRAGMA secure_delete = OFF")
    plain.execute("DELETE FROM messages WHERE content LIKE 'CANARY-%'")
    plain.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    plain.close()
    assert CANARY.encode() in file_bytes(control)


# --- nothing private in the output, the failures or the logs ----------------------------------

NAME = "CANARY-NAME-" + uuid.uuid4().hex[:8]
ADDRESS = "canary.person." + uuid.uuid4().hex[:8] + "@example.com"
TEXT = "CANARY-TEXT-" + uuid.uuid4().hex


def private_world(s: Seed):
    user = s.user(NAME)
    link = s.telegram(user, "424242424")
    s.email_address(user, ADDRESS)
    s.login_session(user, link, created=ago(days=20), expires=ago(days=6))
    conversation = s.conversation(user, created=ago(days=500), updated=ago(days=380))
    s.exchange(conversation, created=ago(days=380), text=TEXT)


def test_no_name_address_message_digest_or_identifier_reaches_the_output_or_a_log(db_url, capsys, caplog, monkeypatch):
    seeded(db_url, private_world)
    secrets_ = [NAME, ADDRESS, TEXT, "424242424"]
    caplog.set_level(logging.DEBUG, logger="house_helper")  # whatever this code logged, at any level
    carrying = f"{NAME} {ADDRESS} {TEXT}"  # what an exception from a driver could quote
    shown = []

    shown.append(purge_cli(capsys, db_url, "--dry-run"))
    shown.append(purge_cli(capsys, db_url))
    shown.append(purge_cli(capsys, db_url))  # nothing left to delete
    flaky_batches(monkeypatch, fail_on="sessions", error=RuntimeError(f"failed on {carrying}"))
    shown.append(purge_cli(capsys, db_url))
    flaky_batches(  # a driver error that quotes its parameters
        monkeypatch, fail_on="sessions", error=sa.exc.OperationalError("SELECT 1", {"p": TEXT}, Exception(NAME))
    )
    shown.append(purge_cli(capsys, db_url))
    flaky_batches(  # the same kind of error, and the database is busy
        monkeypatch, fail_on="sessions",
        error=sa.exc.OperationalError("SELECT 1", {"p": TEXT}, Exception(f"database is locked {carrying}")),
    )
    shown.append(purge_cli(capsys, db_url))
    shown.append(purge_cli(capsys, None))
    shown.append(purge_cli(capsys, db_url, f"--{TEXT}"))

    codes = [run.code for run in shown]
    assert codes == [0, 0, 0, ExitCode.INTERNAL, ExitCode.INTERNAL, ExitCode.REFUSED, ExitCode.DATABASE, ExitCode.USAGE]
    everything = "\n".join(run.text for run in shown) + "\n".join(r.getMessage() for r in caplog.records)
    for secret in secrets_:
        assert secret not in everything
    assert not re.search(r"[0-9a-f]{64}", everything)  # no digest
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", everything)  # no id
    assert [r for r in caplog.records if r.name.startswith("house_helper")] == []  # this code logs nothing


# --- two runs at once, and beside the application's writers ----------------------------------


def bulk_world(url, sessions: int, messages: int):
    engine = create_db_engine(url)
    try:
        fill_bulk(engine, sessions, messages)
    finally:
        engine.dispose()


def fill_bulk(engine, sessions: int, messages: int):
    """Many expired rows, written with a few bulk statements (the ORM would take long), and a
    recent message that must stay."""
    with create_session_factory(engine)() as session, session.begin():
        seed = Seed(session)
        user = seed.user()
        link = seed.telegram(user)
        conversation = seed.conversation(user, created=ago(days=500), updated=ago(days=2))
        seed.message(conversation, "user", "recent", created=ago(days=1))
        session.flush()
        ids = (user.id, link.id, conversation.id)
    with engine.begin() as connection:
        if sessions:
            connection.execute(
                sa.insert(Session.__table__),
                [
                    {
                        "id": uuid.uuid4(), "user_id": ids[0], "identity_link_id": ids[1],
                        "token_hash": uuid.uuid4().hex * 2, "created_at": ago(days=20),
                        "last_seen_at": ago(days=20), "expires_at": ago(days=6),
                    }
                    for _ in range(sessions)
                ],
            )
        if messages:
            connection.execute(
                sa.insert(Message.__table__),
                [
                    {
                        "conversation_id": ids[2], "role": "user", "content": f"old {i}",
                        "created_at": ago(days=400), "expires_at": ago(days=35),
                    }
                    for i in range(messages)
                ],
            )


def run_in_threads(*jobs):
    results, errors = [None] * len(jobs), []
    start = threading.Barrier(len(jobs))

    def runner(index, job):
        try:
            start.wait(timeout=30)
            results[index] = job()
        except BaseException as exc:  # noqa: BLE001 (collected and re-raised by the test)
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i, job)) for i, job in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)
    assert not errors, [type(e.__cause__ or e).__name__ + ": " + str(e.__cause__ or e)[:300] for e in errors]
    return results


def test_two_runs_at_once_delete_every_row_exactly_once_between_them(db_url):
    bulk_world(db_url, sessions=500, messages=500)
    expected_sessions = 500
    expected_messages = 500

    def one_run():
        return purge_engine(db_url, batch_size=20, pause=lambda: time.sleep(0.001))

    first, second = run_in_threads(one_run, one_run)

    assert first.deleted["sessions"] + second.deleted["sessions"] == expected_sessions
    assert first.deleted["messages"] + second.deleted["messages"] == expected_messages
    assert count(db_url, "sessions") == 0 and count(db_url, "messages") == 1  # only the recent one
    integrity(db_url)
    assert purge_engine(db_url).total == 0


def test_a_run_beside_application_writers_loses_none_of_their_writes_and_none_of_its_own(db_url):
    bulk_world(db_url, sessions=300, messages=300)
    written = 60

    def writer():
        engine = create_db_engine(db_url)
        factory = create_session_factory(engine)
        try:
            for index in range(written):
                with factory() as session, session.begin():
                    authn.begin_oidc_login(session, "telegram", f"live-{index}", "n", "v", now=NOW)
                time.sleep(0.002)
        finally:
            engine.dispose()
        return written

    def purger():
        return purge_engine(db_url, batch_size=5, pause=lambda: time.sleep(0.002))

    report = run_in_threads(writer, purger)[1]

    assert report.deleted["sessions"] == 300 and report.deleted["messages"] == 300
    assert count(db_url, "oidc_states") == written  # every sign-in start the application made is there
    assert count(db_url, "sessions") == 0
    integrity(db_url)


# --- under the operator tool's lock model (POSIX file locks) ------------------------------------

PURGE_COMMAND = [sys.executable, "-m", "house_helper.admin", "purge-expired"]


def tool_purge(*extra):
    return ["exec", "--shared", "--", *PURGE_COMMAND, *extra]


@posix_only
def test_it_runs_beside_the_web_service_through_the_shared_lock(db_url, db_path):
    seeded(db_url, expired_world)
    with start_web_service(db_path):
        assert lock_is_taken(db_path)  # the web service holds the directory (shared)
        # An exclusive command (a migration, a restore) is refused while it runs...
        refused = run_tool("exec", "--exclusive", "--", sys.executable, "-c", "print(1)", database=db_path)
        assert refused.returncode == 3 and b"maintenance_lock_busy" in refused.stderr
        # ...and the cleanup is not: it takes the shared lock, as an enrollment does.
        done = run_tool(*tool_purge(), database=db_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode().startswith("ok [purged]")
    assert count(db_url, "sessions") == 1
    integrity(db_url)


@posix_only
def test_while_it_runs_a_migration_or_a_restore_is_refused_and_when_it_ends_they_are_not(db_url, db_path):
    seeded(db_url, expired_world)
    with start_python(UNCOMMITTED_WRITER, db_path) as writer:  # holds SQLite's write lock: the cleanup waits
        purge = subprocess.Popen(
            [sys.executable, str(DBTOOL_PATH), *tool_purge()],
            env=child_environ(db_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            time.sleep(1)  # the operator tool takes its lock first thing; give it the time (and no probe in the way)
            deadline = time.monotonic() + 30
            while not lock_is_taken(db_path):  # the cleanup has the directory (shared) and is waiting
                assert purge.poll() is None and time.monotonic() < deadline
                time.sleep(0.2)
            for argv in (
                ("exec", "--exclusive", "--", sys.executable, "-c", "print(1)"),  # a migration
                ("check-idle",),  # what a restore begins with
            ):
                refused = run_tool(*argv, database=db_path)
                assert refused.returncode == 3 and b"maintenance_lock_busy" in refused.stderr, argv
            assert purge.poll() is None  # still waiting, not failed
            writer.send()  # the other writer commits
            assert writer.read_line() == "COMMITTED"
            out, err = purge.communicate(timeout=60)
        finally:
            if purge.poll() is None:
                purge.kill()
                purge.communicate()
    assert purge.returncode == 0, err
    assert out.decode().startswith("ok [purged]")
    assert not lock_is_taken(db_path)  # released: the exclusive commands can run again
    assert count(db_url, "sessions") == 1


@posix_only
def test_it_is_refused_while_a_restore_or_migration_holds_the_directory(db_url, db_path):
    seeded(db_url, expired_world)
    from tests.deploy_support import dbtool

    before = dump(db_url)
    with dbtool.DataDirLock(db_path, exclusive=True, purpose="the test"):
        done = run_tool(*tool_purge(), database=db_path)
    assert done.returncode == 3 and b"maintenance_lock_busy" in done.stderr
    assert dump(db_url) == before
