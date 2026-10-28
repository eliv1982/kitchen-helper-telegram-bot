"""The Telegram invitation sign-in against a real PostgreSQL server. Opt-in: skipped unless configured.

What the SQLite tests cannot show: migration 0006 applies, reverses and applies again on PostgreSQL
(a plain column, key and index, no table rebuild), the foreign key from a login to its invitation
refuses an unknown invitation and cascades, any number of ends of one invitation at once make exactly
one session, one identity cannot be linked to two users by two invitations at once (PostgreSQL's unique
key is what decides, where SQLite's write lock does), and the start's insert of a login that names an
invitation (a FOR KEY SHARE on the invitation's row) neither waits for nor holds up the claim, the
reissue or the revocation, which lock the same row FOR NO KEY UPDATE.

To run, see tests/test_admin_postgres.py: the same throwaway server, the same variable
(HOUSE_HELPER_TEST_POSTGRES_URL), the same guards (tests/postgres_harness.py: loopback only, a fresh
database per test, only databases of this run are dropped). These tests never read
HOUSE_HELPER_DATABASE_URL.

    pytest tests/test_invite_login_postgres.py -v -rs

Without the variable every test here is skipped, which is not a pass: `pytest -rs` lists the skip. CI
does not set it.
"""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.persistence import invitations as invitations_module
from house_helper.persistence.authn import consume_oidc_login, hash_secret
from house_helper.persistence.database import (
    create_session_factory,
    downgrade_database,
    upgrade_database,
)
from house_helper.persistence.invitations import (
    IdentityRefused,
    InvitationNotUsable,
    begin_invitation_login,
    complete_invitation_login,
    issue_invitation,
    revoke_invitations,
)
from house_helper.persistence.models import AllowlistEntry, IdentityLink, OidcState, User
from house_helper.persistence.models import Session as LoginSession
from tests.persistence_support import utc
from tests.postgres_harness import POSTGRES_URL_ENV
from tests.test_admin_enrollment_postgres import monitor_rc, run_on, scratch_rc  # noqa: F401  (fixtures)
from tests.test_admin_postgres import pg_run, scalar, template  # noqa: F401  (fixtures and helpers)

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL (see tests/test_admin_postgres.py)",
)

NOW = utc(2026, 10, 21, 12)
LATER = NOW + timedelta(minutes=5)
SUB = "1234123412341234123"
OTHER_SUB = "9876598765987659876"
PAUSE = 1.5  # how long a transaction that must be waiting is given to prove it is not finishing
HEAD = "0009_message_attachments"
INVITATIONS = "0005_identity_invitations"


def two_invited(scratch):
    def work(db):
        users = [User(display_name="Mother"), User(display_name="Elena")]
        db.add_all(users)
        db.flush()
        return [(user.id, issue_invitation(db, user.id, now=NOW)) for user in users]

    return run_on(scratch, work)


def begin(db, issued, state, **options):
    return begin_invitation_login(
        db, str(issued.invitation_id), issued.token, state, "nonce", "verifier", now=LATER, **options
    )


def complete(db, issued, subject=SUB):
    return complete_invitation_login(db, str(issued.invitation_id), subject, now=LATER)


def race(scratch, jobs):
    """Each job(session) runs in a transaction of its own, on an engine of its own, all released at the
    same moment: ("ok", value) or ("error", exception) per job, in order."""
    barrier = threading.Barrier(len(jobs))

    def run(job):
        engine = scratch.engine()
        try:
            with create_session_factory(engine)() as db:
                barrier.wait(timeout=30)
                try:
                    with db.begin():
                        return ("ok", job(db))
                except Exception as error:  # reported to the test, which says what it expected
                    return ("error", error)
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return [future.result(timeout=120) for future in [pool.submit(run, job) for job in jobs]]


# --- the migration on a real server ---------------------------------------------------------------------


def test_the_migrations_end_at_0006_and_the_column_key_and_index_are_what_the_models_say(scratch_rc, monitor_rc):
    assert scalar(monitor_rc, "SELECT version_num FROM alembic_version") == HEAD
    column = monitor_rc.connect().execute(
        sa.text(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'oidc_states' AND column_name = 'invitation_id'"
        )
    ).one()
    assert tuple(column) == ("uuid", "YES")
    definition = scalar(
        monitor_rc,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'fk_oidc_states_invitation_id_identity_invitations'",
    )
    assert definition == "FOREIGN KEY (invitation_id) REFERENCES identity_invitations(id) ON DELETE CASCADE"
    index = scalar(monitor_rc, "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_oidc_states_invitation_id'")
    assert "(invitation_id)" in index and "UNIQUE" not in index


def test_the_revision_downgrades_deleting_bound_logins_and_upgrades_again(scratch_rc, monitor_rc):
    ((user_id, issued), _) = two_invited(scratch_rc)

    def logins(db):
        assert begin(db, issued, "bound-state") is True
        db.execute(sa.text(
            "INSERT INTO oidc_states (id, state_hash, provider, nonce, code_verifier, created_at, expires_at) "
            "VALUES (gen_random_uuid(), :hash, 'telegram', 'n', 'v', :created, :expires)"
        ), {"hash": hash_secret("plain-state"), "created": LATER, "expires": LATER + timedelta(minutes=10)})

    run_on(scratch_rc, logins)
    assert scalar(monitor_rc, "SELECT count(*) FROM oidc_states") == 2
    engine = scratch_rc.engine()
    try:
        downgrade_database(engine, INVITATIONS)
        assert scalar(monitor_rc, "SELECT version_num FROM alembic_version") == INVITATIONS
        assert scalar(monitor_rc, "SELECT count(*) FROM oidc_states") == 1  # the bound one did not survive as ordinary
        assert scalar(monitor_rc, "SELECT state_hash FROM oidc_states") == hash_secret("plain-state")
        assert scalar(monitor_rc, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'oidc_states' AND column_name = 'invitation_id'") == 0
        assert scalar(monitor_rc, "SELECT count(*) FROM identity_invitations") == 2  # nothing else was touched
        upgrade_database(engine)
    finally:
        engine.dispose()

    assert scalar(monitor_rc, "SELECT version_num FROM alembic_version") == HEAD
    assert scalar(monitor_rc, "SELECT count(*) FROM oidc_states WHERE invitation_id IS NULL") == 1


def test_the_foreign_key_refuses_an_unknown_invitation_and_cascades(scratch_rc, monitor_rc):
    ((user_id, issued), (other_id, other_issued)) = two_invited(scratch_rc)
    insert = (
        "INSERT INTO oidc_states (id, state_hash, provider, nonce, code_verifier, created_at, expires_at, invitation_id) "
        "VALUES (gen_random_uuid(), :hash, 'telegram', 'n', 'v', :created, :expires, :invitation)"
    )
    base = {"created": LATER, "expires": LATER + timedelta(minutes=10)}
    engine = scratch_rc.engine()
    try:
        with pytest.raises(sa.exc.IntegrityError, match="fk_oidc_states_invitation_id_identity_invitations"):
            with engine.begin() as connection:
                connection.execute(sa.text(insert), {**base, "hash": hash_secret("x"), "invitation": uuid.uuid4()})
    finally:
        engine.dispose()

    def bound(db):
        assert begin(db, issued, "mine") is True and begin(db, other_issued, "theirs") is True

    run_on(scratch_rc, bound)
    run_on(scratch_rc, lambda db: db.execute(sa.delete(User).where(User.id == user_id)))  # the user, so their invitation

    assert scalar(monitor_rc, "SELECT count(*) FROM oidc_states") == 1
    assert scalar(monitor_rc, "SELECT invitation_id FROM oidc_states") == other_issued.invitation_id


# --- the units ----------------------------------------------------------------------------------------


def test_a_whole_sign_in_on_postgresql(scratch_rc, monitor_rc):
    ((user_id, issued), _) = two_invited(scratch_rc)

    def start(db):
        assert begin(db, issued, "state-1") is True

    def end(db):
        secrets = consume_oidc_login(db, "telegram", "state-1", now=LATER)
        return complete_invitation_login(db, str(secrets.invitation_id), SUB, now=LATER)

    run_on(scratch_rc, start)
    assert scalar(monitor_rc, "SELECT consumed_at FROM identity_invitations WHERE id = :id", id=issued.invitation_id) is None
    result = run_on(scratch_rc, end)

    assert result.user_id == user_id
    assert scalar(monitor_rc, "SELECT count(*) FROM allowlist_entries") == 1
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_links") == 1
    assert scalar(monitor_rc, "SELECT count(*) FROM sessions") == 1
    assert scalar(
        monitor_rc, "SELECT consumed_at IS NOT NULL FROM identity_invitations WHERE id = :id", id=issued.invitation_id
    ) is True


@pytest.mark.parametrize("round_number", range(3))
def test_of_many_concurrent_ends_of_one_invitation_exactly_one_succeeds(scratch_rc, monitor_rc, round_number):
    ((user_id, issued), _) = two_invited(scratch_rc)

    results = race(scratch_rc, [lambda db, index=index: complete(db, issued, f"{index}{SUB}") for index in range(6)])

    outcomes = sorted(kind if kind == "ok" else type(value).__name__ for kind, value in results)
    assert outcomes == ["InvitationNotUsable"] * 5 + ["ok"], results
    assert scalar(monitor_rc, "SELECT count(*) FROM allowlist_entries") == 1
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_links") == 1
    assert scalar(monitor_rc, "SELECT count(*) FROM sessions") == 1


@pytest.mark.parametrize("round_number", range(3))
def test_one_identity_cannot_be_linked_to_two_users_by_two_invitations_at_once(scratch_rc, monitor_rc, round_number):
    ((mother_id, mother), (owner_id, owner)) = two_invited(scratch_rc)

    results = race(scratch_rc, [lambda db: complete(db, mother, SUB), lambda db: complete(db, owner, SUB)])

    outcomes = sorted(kind if kind == "ok" else type(value).__name__ for kind, value in results)
    assert outcomes == ["IdentityRefused", "ok"], results
    assert scalar(monitor_rc, "SELECT count(*) FROM allowlist_entries") == 1
    assert scalar(monitor_rc, "SELECT count(*) FROM sessions") == 1
    # the loser's invitation was rolled back to unused: it is still valid for the right person
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_invitations WHERE consumed_at IS NULL") == 1


# --- the locks ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("job", ["claim", "revocation", "reissue"])
def test_a_start_that_has_not_committed_does_not_hold_up_a_claim_a_reissue_or_a_revocation(scratch_rc, monitor_rc, job):
    ((user_id, issued), _) = two_invited(scratch_rc)
    started, release = threading.Event(), threading.Event()

    def holding_start():
        engine = scratch_rc.engine()
        try:
            with create_session_factory(engine)() as db, db.begin():
                assert begin(db, issued, "held") is True  # the insert holds FOR KEY SHARE on the invitation
                started.set()
                release.wait(30)
        finally:
            engine.dispose()

    work = {
        "claim": lambda db: complete(db, issued, SUB),
        "revocation": lambda db: revoke_invitations(db, user_id, now=LATER),
        "reissue": lambda db: issue_invitation(db, user_id, now=LATER),
    }[job]
    holder = threading.Thread(target=holding_start)
    holder.start()
    assert started.wait(30)
    try:
        # all three lock the invitation's row FOR NO KEY UPDATE, which does not conflict with the
        # start's FOR KEY SHARE: none of them waits for it
        began = time.monotonic()
        run_on(scratch_rc, work)
        assert time.monotonic() - began < PAUSE
    finally:
        release.set()
        holder.join(30)
    assert scalar(monitor_rc, "SELECT count(*) FROM oidc_states") == 1  # and the start did commit afterwards


def test_a_claim_that_has_not_committed_does_not_hold_up_a_start(scratch_rc, monitor_rc):
    ((user_id, issued), (other_id, other_issued)) = two_invited(scratch_rc)
    claimed, release = threading.Event(), threading.Event()
    finished = {}

    def holding_claim():
        engine = scratch_rc.engine()
        try:
            with create_session_factory(engine)() as db, db.begin():
                complete(db, issued, SUB)  # the user's row FOR NO KEY UPDATE, the invitation's, everything
                claimed.set()
                release.wait(30)
        finally:
            engine.dispose()

    holder = threading.Thread(target=holding_claim)
    holder.start()
    assert claimed.wait(30)
    try:
        began = time.monotonic()
        # the other user's invitation: no reason to wait at all; and a start that names THIS user's
        # invitation (now consumed in the open transaction) sees it still live until the claim commits
        run_on(scratch_rc, lambda db: begin(db, other_issued, "other-state"))
        finished["other"] = time.monotonic() - began
        began = time.monotonic()
        run_on(scratch_rc, lambda db: begin(db, issued, "same-state"))
        finished["same"] = time.monotonic() - began
        assert finished["other"] < PAUSE and finished["same"] < PAUSE
    finally:
        release.set()
        holder.join(30)

    # the start that raced the claim stored a state, and the end refuses it: the invitation is used up
    secrets = run_on(scratch_rc, lambda db: consume_oidc_login(db, "telegram", "same-state", now=LATER))
    assert secrets is not None and secrets.invitation_id == issued.invitation_id
    with pytest.raises(InvitationNotUsable):
        run_on(scratch_rc, lambda db: complete_invitation_login(db, str(secrets.invitation_id), OTHER_SUB, now=LATER))
    assert scalar(monitor_rc, "SELECT count(*) FROM sessions") == 1  # the claim's, and nobody else's


def test_a_revocation_waits_for_nothing_the_start_holds_and_wins(scratch_rc, monitor_rc):
    ((user_id, issued), _) = two_invited(scratch_rc)
    run_on(scratch_rc, lambda db: begin(db, issued, "state-1"))

    assert run_on(scratch_rc, lambda db: revoke_invitations(db, user_id, now=LATER)) == 1

    with pytest.raises(InvitationNotUsable):
        run_on(scratch_rc, lambda db: (consume_oidc_login(db, "telegram", "state-1", now=LATER), complete(db, issued))[1])
    assert scalar(monitor_rc, "SELECT count(*) FROM allowlist_entries") == 0
