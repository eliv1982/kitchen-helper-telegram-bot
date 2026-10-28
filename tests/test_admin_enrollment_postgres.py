"""Enrollment and invitations against a real PostgreSQL server. Opt-in: skipped unless configured.

What the SQLite tests cannot show: the enrollment commands run at READ COMMITTED under the
advisory lock that provision-email takes (one lock for every command that creates users), so
the count behind the enrollment limit is exact under concurrency; migrations 0005 and 0006 apply,
reverses and applies again on PostgreSQL, with its partial unique index; and the row locks that
order a reissue against a web claim (the user's row first, FOR NO KEY UPDATE, then the
invitation's) really do: whichever comes second waits for the first and then sees it. Also the
one race that exists on PostgreSQL only: two invitations, two users, ONE Telegram identity.

To run, see tests/test_admin_postgres.py: the same throwaway server, the same variable
(HOUSE_HELPER_TEST_POSTGRES_URL), the same guards (tests/postgres_harness.py: loopback only, a
fresh database per test, only databases of this run are dropped). These tests never read
HOUSE_HELPER_DATABASE_URL.

    pytest tests/test_admin_enrollment_postgres.py -v -rs

Without the variable every test here is skipped, which is not a pass: `pytest -rs` lists the
skip. CI does not set it.
"""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.admin import enrollment, provisioning
from house_helper.admin.cli import main
from house_helper.admin.enrollment import EnrollmentRefusal, EnrollmentRefusalReason
from house_helper.admin.provisioning import Outcome, Refusal, prepare_request, provision_email
from house_helper.persistence import invitations as invitations_module
from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    create_session_factory,
    downgrade_database,
    upgrade_database,
)
from house_helper.persistence.invitations import (
    InvitationRefusal,
    InvitationRefused,
    InvitationState,
    claim_invitation,
    issue_invitation,
    parse_invitation_code,
    revoke_invitations,
    verify_invitation,
)
from house_helper.persistence.models import User
from tests.persistence_support import utc
from tests.postgres_harness import POSTGRES_URL_ENV, leaked
from tests.test_admin_postgres import (  # noqa: F401  (fixtures and helpers)
    DATABASE_DEFAULT,
    _KEY,
    _LOCKS,
    assert_the_lock_is_free,
    monitor,
    pg_run,
    scalar,
    scratch,
    template,
    waiters,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL (see tests/test_admin_postgres.py)",
)

NOW = utc(2026, 10, 19, 12)
LATER = NOW + timedelta(minutes=5)
SUB = "1234123412341234123"
OTHER_SUB = "9876598765987659876"
PAUSE = 1.5  # how long a transaction that must be waiting is given to prove it is not finishing


@pytest.fixture
def scratch_rc(pg_run, template):
    """A database of its own whose default isolation level is the server's (READ COMMITTED),
    which is what a deployment normally has; `scratch` has REPEATABLE READ."""
    from tests.postgres_harness import Scratch

    name = pg_run.create_database(template=template)
    try:
        yield Scratch(pg_run, name)
    finally:
        pg_run.drop_database(name)


@pytest.fixture
def monitor_rc(scratch_rc):
    engine = scratch_rc.engine()
    try:
        yield engine
    finally:
        engine.dispose()


def run_on(scratch, work):
    """work(session) in a transaction of its own, on an engine of its own."""
    engine = scratch.engine()
    try:
        with create_session_factory(engine)() as db, db.begin():
            return work(db)
    finally:
        engine.dispose()


def enroll_on(scratch, name):
    engine = scratch.engine()
    try:
        return enrollment.enroll_user(create_session_factory(engine), name, now=NOW)
    finally:
        engine.dispose()


def make_invited_user(scratch, name="Elena"):
    def work(db):
        user = User(display_name=name)
        db.add(user)
        db.flush()
        return user.id, issue_invitation(db, user.id, now=NOW)

    return run_on(scratch, work)


def entries(monitor):
    return scalar(monitor, "SELECT count(*) FROM allowlist_entries WHERE provider = 'telegram'")


# --- the migration on a real server ---------------------------------------------------------------------


def test_the_migrations_end_at_the_head_and_the_table_and_its_partial_index_are_what_the_models_say(
    scratch_rc, monitor_rc
):
    assert scalar(monitor_rc, "SELECT version_num FROM alembic_version") == "0009_message_attachments"
    definition = scalar(
        monitor_rc,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_identity_invitations_one_outstanding'",
    )
    assert "CREATE UNIQUE INDEX" in definition and "(user_id)" in definition
    assert "consumed_at IS NULL" in definition and "revoked_at IS NULL" in definition


def test_the_invitation_revision_downgrades_and_upgrades_again(scratch_rc, monitor_rc):
    user_id, issued = make_invited_user(scratch_rc)
    engine = scratch_rc.engine()
    try:
        downgrade_database(engine, "0004_email_identity_case")
        assert scalar(monitor_rc, "SELECT to_regclass('identity_invitations') IS NULL") is True
        assert scalar(monitor_rc, "SELECT count(*) FROM users") == 1  # nothing else was touched
        upgrade_database(engine)
    finally:
        engine.dispose()

    assert scalar(monitor_rc, "SELECT version_num FROM alembic_version") == "0009_message_attachments"
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_invitations") == 0  # an empty table again


def test_the_databases_own_rules_hold_on_postgresql(scratch_rc, monitor_rc):
    user_id, issued = make_invited_user(scratch_rc)
    insert = (
        "INSERT INTO identity_invitations (id, user_id, provider, token_hash, created_at, expires_at, "
        "consumed_at, revoked_at) VALUES (gen_random_uuid(), :user, :provider, :digest, :created, "
        ":expires, :consumed, :revoked)"
    )
    base = {
        "user": user_id,
        "provider": "telegram",
        "created": NOW,
        "expires": NOW + timedelta(hours=2),
        "consumed": None,
        "revoked": None,
    }

    def refused(**changes):
        engine = scratch_rc.engine()
        try:
            with pytest.raises(sa.exc.IntegrityError) as error:
                with engine.begin() as connection:
                    connection.execute(sa.text(insert), {**base, "digest": uuid.uuid4().hex * 2, **changes})
            return str(error.value.orig)
        finally:
            engine.dispose()

    assert "uq_identity_invitations_one_outstanding" in refused()  # a second outstanding invitation
    assert "ck_identity_invitations_provider_is_telegram" in refused(provider="email", revoked=NOW)
    assert "ck_identity_invitations_not_consumed_and_revoked" in refused(consumed=LATER, revoked=LATER)
    assert "ck_identity_invitations_expires_after_created" in refused(expires=NOW, revoked=NOW)
    assert "ck_identity_invitations_token_hash_length" in refused(digest="short", revoked=NOW)


# --- the lock enrollment shares with provision-email --------------------------------------------------


def test_an_enrollment_runs_at_read_committed_holding_provision_emails_advisory_lock(scratch, monitor, monkeypatch):
    # The premise: an ordinary transaction on this database IS repeatable read.
    assert scalar(monitor, "SHOW default_transaction_isolation") == DATABASE_DEFAULT
    seen = {}
    real = enrollment.check_schema_at_head

    def look_inside_the_transaction(connection):
        seen["isolation"] = connection.exec_driver_sql("SHOW transaction_isolation").scalar()
        seen["advisory_locks_held_by_this_backend"] = connection.exec_driver_sql(
            _LOCKS + " AND pid = pg_backend_pid() AND granted"
        ).scalar()
        real(connection)

    monkeypatch.setattr(enrollment, "check_schema_at_head", look_inside_the_transaction)

    issued = enroll_on(scratch, "Elena")

    assert issued.superseded == 0
    # the key is provision-email's: _LOCKS counts exactly provisioning.ADVISORY_LOCK_KEY
    assert seen == {"isolation": "read committed", "advisory_locks_held_by_this_backend": 1}
    assert provisioning.ADVISORY_LOCK_KEY == 815883989575170308
    assert_the_lock_is_free(monitor)


def hold_the_lock_and_run(monitor, jobs):
    """Every job is held at the advisory lock, before it reads anything, until all of them are
    queued; then the lock is let go. Returns the results and the errors."""
    results, errors = [], []
    with monitor.connect().execution_options(isolation_level="AUTOCOMMIT") as holder:
        holder.execute(sa.select(sa.func.pg_advisory_lock(_KEY)))
        try:
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = [pool.submit(job) for job in jobs]
                try:
                    deadline = time.monotonic() + 60
                    while waiters(monitor) < len(jobs):
                        failed = [f.exception() for f in futures if f.done()]
                        assert not failed, f"a run ended before the lock was released: {failed}"
                        assert time.monotonic() < deadline, "the runs never queued behind the lock"
                        time.sleep(0.05)
                finally:
                    holder.execute(sa.select(sa.func.pg_advisory_unlock(_KEY)))
                for future in futures:
                    try:
                        results.append(future.result(timeout=60))
                    except Exception as error:
                        errors.append(error)
        finally:
            holder.execute(sa.select(sa.func.pg_advisory_unlock_all()))
    return results, errors


def test_concurrent_enrollments_queued_at_the_lock_create_exactly_two_users(scratch, monitor):
    """Without READ COMMITTED a run that had taken its snapshot at the lock statement (under
    REPEATABLE READ) would count the users before the runs ahead of it committed, and make a
    third; without the lock they would all see an empty table."""
    names = [f"User number {number}" for number in range(6)]

    results, errors = hold_the_lock_and_run(monitor, [lambda n=n: enroll_on(scratch, n) for n in names])

    assert len(results) == 2 and len(errors) == 4
    assert all(isinstance(e, EnrollmentRefusal) and e.reason is EnrollmentRefusalReason.ENROLLMENT_FULL for e in errors)
    assert scalar(monitor, "SELECT count(*) FROM users") == 2
    assert scalar(monitor, "SELECT count(*) FROM identity_invitations") == 2
    assert_the_lock_is_free(monitor)


def test_concurrent_enrollments_of_one_name_create_one_user(scratch, monitor):
    results, errors = hold_the_lock_and_run(monitor, [lambda: enroll_on(scratch, "Мама") for _ in range(5)])

    assert len(results) == 1 and len(errors) == 4
    assert all(e.reason is EnrollmentRefusalReason.DISPLAY_NAME_IN_USE for e in errors)
    assert scalar(monitor, "SELECT count(*) FROM users") == 1


def test_enrollment_and_provision_email_wait_on_the_same_lock_so_they_cannot_both_create_the_first_user(
    scratch, monitor
):
    def provision():
        engine = scratch.engine()
        try:
            return provision_email(create_session_factory(engine), prepare_request("Elena@example.com"))
        finally:
            engine.dispose()

    results, errors = hold_the_lock_and_run(
        monitor, [provision, lambda: enroll_on(scratch, "Alpha"), lambda: enroll_on(scratch, "Beta")]
    )

    # all three queued on ONE key (hold_the_lock_and_run waited for three waiters on it); then
    # they ran one at a time: at most two users, whoever was first
    assert all(isinstance(e, (Refusal, EnrollmentRefusal)) for e in errors), errors
    created = [r for r in results if getattr(r, "outcome", None) is Outcome.CREATED] + [
        r for r in results if not hasattr(r, "outcome")
    ]
    assert scalar(monitor, "SELECT count(*) FROM users") == len(created) <= 2
    assert_the_lock_is_free(monitor)


def test_a_refusal_for_a_full_house_changes_nothing_and_releases_the_lock(scratch, monitor):
    enroll_on(scratch, "Elena")
    enroll_on(scratch, "Мама")
    engine = scratch.engine()
    try:
        factory = create_session_factory(engine)
        with pytest.raises(EnrollmentRefusal) as refused:
            enrollment.enroll_user(factory, "Third", now=NOW)

        assert refused.value.reason is EnrollmentRefusalReason.ENROLLMENT_FULL
        assert engine.pool.checkedin() == 1  # the connection is open, in the pool
        assert scalar(monitor, "SELECT count(*) FROM users") == 2
        assert_the_lock_is_free(monitor)  # let go by the end of the transaction, not by closing
    finally:
        engine.dispose()


def test_a_failure_after_the_writes_rolls_the_user_and_the_invitation_back_and_releases_the_lock(
    scratch, monitor, monkeypatch
):
    real = enrollment.invitations.issue_invitation

    def failing(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("boom")

    monkeypatch.setattr(enrollment.invitations, "issue_invitation", failing)
    engine = scratch.engine()
    try:
        with pytest.raises(RuntimeError):
            enrollment.enroll_user(create_session_factory(engine), "Elena", now=NOW)

        assert engine.pool.checkedin() == 1
        assert scalar(monitor, "SELECT count(*) FROM users") == 0
        assert scalar(monitor, "SELECT count(*) FROM identity_invitations") == 0
        assert_the_lock_is_free(monitor)
    finally:
        engine.dispose()


# --- invitations under real row locks (READ COMMITTED) --------------------------------------------------


def race(scratch, jobs, *, timeout=60):
    """Each job(session) in a transaction of its own, on an engine of its own, released together."""
    barrier = threading.Barrier(len(jobs))

    def run(job):
        engine = scratch.engine()
        try:
            with create_session_factory(engine)() as db:
                barrier.wait(timeout=30)
                try:
                    with db.begin():
                        return ("ok", job(db))
                except Exception as error:
                    return ("error", error)
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return [f.result(timeout=timeout) for f in [pool.submit(run, job) for job in jobs]]


@pytest.mark.parametrize("round_number", range(3))
def test_of_many_concurrent_claims_of_one_invitation_exactly_one_succeeds(scratch_rc, monitor_rc, round_number):
    user_id, issued = make_invited_user(scratch_rc)
    subjects = [f"{index}{SUB}" for index in range(8)]

    results = race(
        scratch_rc,
        [lambda db, s=s: claim_invitation(db, str(issued.invitation_id), s, now=LATER) for s in subjects],
    )

    assert [kind for kind, _ in results] == ["ok"] * 8, results
    winners = [value for _, value in results if value is not None]
    assert len(winners) == 1 and winners[0].user_id == user_id
    assert entries(monitor_rc) == 1
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_invitations WHERE consumed_at IS NOT NULL") == 1


def test_under_repeatable_read_the_losers_of_a_claim_race_fail_safely_and_never_two_win(scratch, monitor):
    # The database default here is REPEATABLE READ: a loser may get a serialisation failure
    # instead of waiting and finding the invitation used. Either is a refusal; two winners is not.
    user_id, issued = make_invited_user(scratch)
    subjects = [f"{index}{SUB}" for index in range(8)]

    results = race(
        scratch,
        [lambda db, s=s: claim_invitation(db, str(issued.invitation_id), s, now=LATER) for s in subjects],
    )

    winners = [value for kind, value in results if kind == "ok" and value is not None]
    assert len(winners) == 1
    for kind, value in results:
        assert kind == "ok" or isinstance(value, sa.exc.DBAPIError), results  # a clean database error
    assert entries(monitor) == 1
    assert scalar(monitor, "SELECT count(*) FROM identity_invitations WHERE consumed_at IS NOT NULL") == 1


def pause_inside(monkeypatch, name):
    """Wrap invitations.<name> so that the FIRST call pauses after the real work, until released.
    Returns (inside, release): `inside` is set when the call is paused."""
    inside, release = threading.Event(), threading.Event()
    real = getattr(invitations_module, name)
    calls = []

    def wrapper(*args, **kwargs):
        value = real(*args, **kwargs)
        if not calls:
            calls.append(1)
            inside.set()
            assert release.wait(timeout=60)
        return value

    monkeypatch.setattr(invitations_module, name, wrapper)
    return inside, release


def test_two_invitations_for_two_users_cannot_link_one_identity_and_the_loser_stays_valid(
    scratch_rc, monitor_rc, monkeypatch
):
    """The race that exists on PostgreSQL only. Two claims (two users, so two different user rows
    and no common lock) both pass the check that the identity is free and both use their
    invitation up; both then insert the allowlist entry, and the unique (provider, subject) key
    lets one through. The other is refused as IdentityRefused and its transaction, rolled back,
    un-uses its invitation. (The barrier holds both claims between the check and the insert.)"""
    first_user, first = make_invited_user(scratch_rc, "Elena")
    second_user, second = make_invited_user(scratch_rc, "Мама")
    both_checked = threading.Barrier(2)
    real_allow = invitations_module.allow

    def allow_after_both_have_checked(*args, **kwargs):
        both_checked.wait(timeout=30)
        return real_allow(*args, **kwargs)

    monkeypatch.setattr(invitations_module, "allow", allow_after_both_have_checked)

    results = race(
        scratch_rc,
        [
            lambda db: claim_invitation(db, str(first.invitation_id), SUB, now=LATER),
            lambda db: claim_invitation(db, str(second.invitation_id), SUB, now=LATER),
        ],
    )

    kinds = sorted(kind if kind == "ok" else type(value).__name__ for kind, value in results)
    assert kinds == ["IdentityRefused", "ok"], results
    assert entries(monitor_rc) == 1
    owner = scalar(monitor_rc, "SELECT user_id FROM allowlist_entries WHERE provider = 'telegram'")
    loser = second if owner == first_user else first
    consumed = scalar(
        monitor_rc,
        "SELECT consumed_at IS NOT NULL FROM identity_invitations WHERE id = :id",
        id=loser.invitation_id,
    )
    assert consumed is False  # rolled back with the refused claim: still valid for the right person
    assert run_on(scratch_rc, lambda db: verify_invitation(db, str(loser.invitation_id), loser.token, now=LATER)) is not None


def test_a_reissue_waits_for_a_claim_in_flight_and_is_then_refused_because_the_user_is_linked(
    scratch_rc, monitor_rc, monkeypatch
):
    user_id, old = make_invited_user(scratch_rc)
    inside, release = pause_inside(monkeypatch, "allow")  # a claim, after it used the invitation up
    with ThreadPoolExecutor(max_workers=2) as pool:
        claiming = pool.submit(
            run_on, scratch_rc, lambda db: claim_invitation(db, str(old.invitation_id), SUB, now=LATER)
        )
        assert inside.wait(timeout=60)
        reissuing = pool.submit(run_on, scratch_rc, lambda db: issue_invitation(db, user_id, now=LATER))

        time.sleep(PAUSE)
        assert not reissuing.done()  # it waits for the user's row, which the claim holds
        release.set()

        assert claiming.result(timeout=60) is not None
        with pytest.raises(InvitationRefused) as refused:
            reissuing.result(timeout=60)

    assert refused.value.reason is InvitationRefusal.TELEGRAM_PROVISIONED
    assert entries(monitor_rc) == 1
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_invitations WHERE revoked_at IS NULL AND consumed_at IS NULL") == 0


def test_a_claim_waits_for_a_reissue_in_flight_and_finds_its_invitation_revoked(
    scratch_rc, monitor_rc, monkeypatch
):
    user_id, old = make_invited_user(scratch_rc)
    inside, release = pause_inside(monkeypatch, "hash_secret")  # a reissue, after it revoked the old one
    with ThreadPoolExecutor(max_workers=2) as pool:
        reissuing = pool.submit(run_on, scratch_rc, lambda db: issue_invitation(db, user_id, now=LATER))
        assert inside.wait(timeout=60)
        claiming = pool.submit(
            run_on, scratch_rc, lambda db: claim_invitation(db, str(old.invitation_id), SUB, now=LATER)
        )

        time.sleep(PAUSE)
        assert not claiming.done()  # it waits for the user's row, which the reissue holds
        release.set()

        new = reissuing.result(timeout=60)
        assert claiming.result(timeout=60) is None  # the old secret was dead when it looked

    assert entries(monitor_rc) == 0
    assert scalar(monitor_rc, "SELECT id FROM identity_invitations WHERE revoked_at IS NULL AND consumed_at IS NULL") == new.invitation_id


def test_a_revocation_waits_for_a_claim_in_flight_and_changes_nothing(scratch_rc, monitor_rc, monkeypatch):
    user_id, old = make_invited_user(scratch_rc)
    inside, release = pause_inside(monkeypatch, "allow")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claiming = pool.submit(
            run_on, scratch_rc, lambda db: claim_invitation(db, str(old.invitation_id), SUB, now=LATER)
        )
        assert inside.wait(timeout=60)
        revoking = pool.submit(run_on, scratch_rc, lambda db: revoke_invitations(db, user_id, now=LATER))

        time.sleep(PAUSE)
        assert not revoking.done()  # it contends for the very row the claim has updated
        release.set()

        assert claiming.result(timeout=60) is not None
        assert revoking.result(timeout=60) == 0  # re-evaluated after the claim committed: nothing to revoke

    assert scalar(monitor_rc, "SELECT count(*) FROM identity_invitations WHERE revoked_at IS NOT NULL") == 0
    assert entries(monitor_rc) == 1


def test_a_claim_does_not_block_the_inserts_that_merely_reference_the_users_row(
    scratch_rc, monitor_rc, monkeypatch
):
    # FOR NO KEY UPDATE, not FOR UPDATE: a conversation (a foreign key to the user) can be
    # created while a claim of that user is in flight.
    user_id, old = make_invited_user(scratch_rc)
    inside, release = pause_inside(monkeypatch, "allow")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claiming = pool.submit(
            run_on, scratch_rc, lambda db: claim_invitation(db, str(old.invitation_id), SUB, now=LATER)
        )
        assert inside.wait(timeout=60)
        from house_helper.persistence.conversations import create_conversation

        chatting = pool.submit(run_on, scratch_rc, lambda db: create_conversation(db, user_id, now=LATER))
        chatting.result(timeout=PAUSE * 10)  # finishes while the claim is still paused
        assert not claiming.done()
        release.set()
        assert claiming.result(timeout=60) is not None

    assert scalar(monitor_rc, "SELECT count(*) FROM conversations") == 1


# --- an expired invitation, the report, and the clock behind the lock ----------------------------------


def test_revoking_leaves_an_expired_invitation_alone_and_reissuing_supersedes_it(scratch_rc, monitor_rc):
    user_id, old = make_invited_user(scratch_rc)  # issued at NOW, over at NOW + 2 hours (exclusive)
    over = NOW + timedelta(hours=2)

    def revoke_at(moment):
        return run_on(scratch_rc, lambda db: revoke_invitations(db, user_id, now=moment))

    assert revoke_at(over) == 0 and revoke_at(over + timedelta(hours=1)) == 0
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_invitations WHERE revoked_at IS NOT NULL") == 0
    # the unique index would refuse the new row if this one were still in the way: only a reissue takes it out
    fresh = run_on(scratch_rc, lambda db: issue_invitation(db, user_id, now=over + timedelta(hours=1)))
    assert fresh.superseded == 1
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_invitations WHERE revoked_at IS NOT NULL") == 1
    assert revoke_at(fresh.expires_at - timedelta(microseconds=1)) == 1  # its last instant is still valid


def test_the_report_names_the_usable_invitation_when_the_newest_one_carries_the_earliest_time(scratch_rc):
    user_id, _ = make_invited_user(scratch_rc)  # issued at NOW
    engine = scratch_rc.engine()
    try:
        factory = create_session_factory(engine)
        enrollment.reissue_invitation(factory, user_id, now=NOW + timedelta(minutes=10))
        last = enrollment.reissue_invitation(factory, user_id, now=NOW + timedelta(minutes=5))
        (user,) = enrollment.enrollment_status(factory, now=NOW + timedelta(minutes=15)).users
    finally:
        engine.dispose()

    assert user.invitation.state is InvitationState.ACTIVE and user.invitation.at == last.expires_at


def test_a_run_queued_behind_the_lock_reads_the_clock_only_once_it_holds_the_lock(scratch, monitor):
    user_id, _ = make_invited_user(scratch)
    read = threading.Event()

    def clock():
        read.set()
        return LATER

    def reissue():
        engine = scratch.engine()
        try:
            return enrollment.reissue_invitation(create_session_factory(engine), user_id, now=clock)
        finally:
            engine.dispose()

    with monitor.connect().execution_options(isolation_level="AUTOCOMMIT") as holder:
        holder.execute(sa.select(sa.func.pg_advisory_lock(_KEY)))
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                running = pool.submit(reissue)
                try:
                    deadline = time.monotonic() + 60
                    while waiters(monitor) < 1:
                        assert not running.done(), running.exception()
                        assert time.monotonic() < deadline, "the run never queued behind the lock"
                        time.sleep(0.05)
                    time.sleep(PAUSE / 3)
                    assert not read.is_set()  # queued, so it has not dated its invitation yet
                finally:
                    holder.execute(sa.select(sa.func.pg_advisory_unlock(_KEY)))
                issued = running.result(timeout=60)
        finally:
            holder.execute(sa.select(sa.func.pg_advisory_unlock_all()))

    assert read.is_set() and issued.superseded == 1
    assert_the_lock_is_free(monitor)


# --- the command line, end to end ---------------------------------------------------------------------


def test_the_command_line_enrolls_reissues_revokes_and_reports(scratch, monitor, capsys):
    environ = {DATABASE_URL_ENV: scratch.url_text}

    assert main(["enroll-user", "--display-name", "Elena"], environ=environ) == 0
    enrolled = capsys.readouterr()
    user_id = scalar(monitor, "SELECT id FROM users")
    assert main(["reissue-invitation", "--user-id", str(user_id)], environ=environ) == 0
    reissued = capsys.readouterr()
    assert main(["revoke-invitation", "--user-id", str(user_id)], environ=environ) == 0
    revoked = capsys.readouterr()
    assert main(["enrollment-status"], environ=environ) == 0
    reported = capsys.readouterr()
    assert main(["enroll-user", "--display-name", "elena"], environ=environ) == 3
    refused = capsys.readouterr()

    assert enrolled.out.startswith("ok [enrolled]: ") and reissued.out.startswith("ok [reissued]: ")
    codes = [out.out.splitlines()[1][len("invitation code: "):] for out in (enrolled, reissued)]
    assert all(parse_invitation_code(code) is not None for code in codes) and codes[0] != codes[1]
    assert revoked.out.startswith("ok [revoked]: ")
    assert 'name "Elena"' in reported.out and "invitation: revoked" in reported.out
    assert refused.err.startswith("refused [display_name_in_use]: ")
    for output in (enrolled, reissued, revoked, reported, refused):
        assert leaked(scratch.url, output.out + output.err) == []  # the URL is never printed
        for code in codes:
            if output not in (enrolled, reissued):
                assert code not in output.out + output.err  # and the secret only where it is issued
    assert scalar(monitor, "SELECT count(*) FROM identity_invitations") == 2
    assert_the_lock_is_free(monitor)
