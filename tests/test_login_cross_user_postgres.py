"""A login in a browser that holds a session of somebody else, against a real PostgreSQL server.
Opt-in: skipped unless configured.

What the SQLite tests cannot show: on PostgreSQL (READ COMMITTED, no write lock taken first) the
comparison that authn.complete_login makes, in the transaction that would issue the session, still
refuses a live session of another user and leaves nothing behind (not a session, not the identity
link a first login creates); and a login that races the end of the browser's own session either
signs in (it saw the session ended) or is refused (it saw it live), never fails or leaves a
half-written login.

To run, see tests/test_admin_postgres.py: the same throwaway server, the same variable
(HOUSE_HELPER_TEST_POSTGRES_URL), the same guards (tests/postgres_harness.py: loopback only, a fresh
database per test, only databases of this run are dropped). These tests never read
HOUSE_HELPER_DATABASE_URL.

    pytest tests/test_login_cross_user_postgres.py -v -rs

Without the variable every test here is skipped, which is not a pass: `pytest -rs` lists the skip. CI
does not set it.
"""

import os
from datetime import timedelta

import pytest

from house_helper.persistence.allowlist import allow
from house_helper.persistence.authn import SignedInAsAnotherUser, complete_login, revoke_session
from house_helper.persistence.models import User
from tests.persistence_support import utc
from tests.postgres_harness import POSTGRES_URL_ENV
from tests.test_admin_enrollment_postgres import monitor_rc, run_on, scratch_rc  # noqa: F401  (fixtures)
from tests.test_admin_postgres import pg_run, scalar, template  # noqa: F401  (fixtures and helpers)
from tests.test_invite_login_postgres import race

pytestmark = pytest.mark.skipif(
    not os.environ.get(POSTGRES_URL_ENV),
    reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL (see tests/test_admin_postgres.py)",
)

NOW = utc(2026, 10, 21, 12)
LATER = NOW + timedelta(minutes=5)
SUB = "1234123412341234123"
OTHER_SUB = "9876598765987659876"


def two_users(scratch):
    """Elena, signed in (her session's token is the browser's), and Boris, who has never signed in."""

    def work(db):
        elena, boris = User(display_name="Elena"), User(display_name="Boris")
        db.add_all([elena, boris])
        db.flush()
        allow(db, elena, "telegram", SUB)
        allow(db, boris, "telegram", OTHER_SUB)
        first = complete_login(db, "telegram", SUB, now=NOW)
        return elena.id, boris.id, first.token

    return run_on(scratch, work)


def boris_in_elenas_browser(token):
    return lambda db: complete_login(db, "telegram", OTHER_SUB, now=LATER, browser_session_token=token)


def test_a_live_session_of_another_user_refuses_the_login_and_nothing_is_left_on_postgresql(scratch_rc, monitor_rc):
    elena_id, boris_id, token = two_users(scratch_rc)

    with pytest.raises(SignedInAsAnotherUser):
        run_on(scratch_rc, boris_in_elenas_browser(token))

    assert scalar(monitor_rc, "SELECT count(*) FROM sessions") == 1
    assert scalar(monitor_rc, "SELECT count(*) FROM sessions WHERE revoked_at IS NULL") == 1  # hers, untouched
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_links") == 1  # hers; none was made for Boris
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_links WHERE user_id = :id", id=boris_id) == 0

    # the same user signing in again is no obstacle, and the old session stays
    again = run_on(
        scratch_rc,
        lambda db: complete_login(db, "telegram", SUB, now=LATER, browser_session_token=token),
    )
    assert again.user_id == elena_id
    assert scalar(monitor_rc, "SELECT count(*) FROM sessions WHERE revoked_at IS NULL") == 2


@pytest.mark.parametrize("round_number", range(4))
def test_a_login_racing_the_end_of_the_browsers_session_is_refused_or_signs_in_and_never_fails(
    scratch_rc, monitor_rc, round_number
):
    _, boris_id, token = two_users(scratch_rc)

    login, revocation = race(
        scratch_rc,
        [boris_in_elenas_browser(token), lambda db: revoke_session(db, token, now=LATER)],
    )

    assert revocation == ("ok", True), revocation
    signed_in = login[0] == "ok"
    assert signed_in or isinstance(login[1], SignedInAsAnotherUser), login
    assert scalar(monitor_rc, "SELECT count(*) FROM sessions") == (2 if signed_in else 1)
    assert scalar(monitor_rc, "SELECT count(*) FROM sessions WHERE revoked_at IS NULL") == (1 if signed_in else 0)
    assert scalar(monitor_rc, "SELECT count(*) FROM identity_links WHERE user_id = :id", id=boris_id) == (
        1 if signed_in else 0
    )
