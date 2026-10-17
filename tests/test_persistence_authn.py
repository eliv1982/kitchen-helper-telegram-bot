"""Authentication persistence: OIDC logins, the end of a login, and sessions.

Runs against both the schema created from the models and the migrated one
(tests/persistence_support.py). Nothing here talks to a provider or to HTTP: the
functions take the verified `sub` and the clock as arguments, which is how the
tests move time. The HTTP behaviour on top is in tests/test_web_auth.py.

The properties that matter, each pinned below: only digests of session tokens and
OIDC states are stored; a state is used once, by exactly one of any number of
concurrent callers, and not at or after its expiry; a login resolves to a user only
through the allowlist and never to another user; a session is live only while it
is neither revoked nor expired and its user still has access; validating a session
only reads; and every writer starts with a write, which is what lets concurrent
logins queue on SQLite instead of failing on a stale snapshot.
"""

import itertools
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa

from house_helper.persistence import authn
from house_helper.persistence.allowlist import allow, revoke
from house_helper.persistence.authn import (
    AuthenticatedSession,
    IssuedSession,
    LoginDenied,
    OidcLoginSecrets,
    authenticate,
    begin_oidc_login,
    complete_login,
    consume_oidc_login,
    hash_secret,
    revoke_session,
)
from house_helper.persistence.models import IdentityLink, OidcState, Session, User
from tests.persistence_support import (  # noqa: F401  (fixtures)
    count,
    engine,
    factory,
    new_oidc_state,
    new_user,
    session,
    utc,
)

NOW = utc(2026, 10, 17, 12)
SUB = "1234123412341234123"
LIFETIME = timedelta(days=14)
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")


@pytest.fixture
def user(session):
    user = new_user(session, display_name="Elena")
    allow(session, user, "telegram", SUB)
    session.commit()
    return user


def start(session, state="state-value", nonce="nonce-value", verifier="verifier-value", **options):
    options.setdefault("now", NOW)
    begin_oidc_login(session, "telegram", state, nonce, verifier, **options)


def log_in(session, subject=SUB, **options):
    options.setdefault("now", NOW)
    return complete_login(session, "telegram", subject, **options)


@contextmanager
def statements(engine):
    """The SQL the engine executes inside the block, without the transaction
    plumbing."""
    seen: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if not statement.startswith(("BEGIN", "SAVEPOINT", "RELEASE", "ROLLBACK", "PRAGMA")):
            seen.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)


def row_text(session, table: str) -> str:
    """Every value of every row of `table`, as one string, to search for a secret."""
    rows = session.execute(sa.text(f"SELECT * FROM {table}")).all()
    return "\n".join(repr(tuple(row)) for row in rows)


# --- an OIDC login: begin ---------------------------------------------------------


def test_beginning_a_login_stores_a_digest_of_the_state_and_never_the_state(session):
    start(session, state="the-state-that-goes-to-the-provider")

    row = session.scalars(sa.select(OidcState)).one()

    assert row.state_hash == hash_secret("the-state-that-goes-to-the-provider")
    assert len(row.state_hash) == 64
    assert "the-state-that-goes-to-the-provider" not in row_text(session, "oidc_states")
    assert (row.provider, row.nonce, row.code_verifier) == ("telegram", "nonce-value", "verifier-value")
    assert row.created_at == NOW and row.expires_at == NOW + authn.OIDC_LOGIN_LIFETIME
    assert row.consumed_at is None


def test_the_lifetime_of_a_login_is_the_one_given(session):
    start(session, lifetime=timedelta(seconds=90))

    assert session.scalars(sa.select(OidcState)).one().expires_at == NOW + timedelta(seconds=90)


def test_the_login_lifetime_is_ten_minutes():
    assert authn.OIDC_LOGIN_LIFETIME == timedelta(minutes=10)


def test_beginning_a_login_deletes_expired_logins_and_keeps_the_rest(session):
    # (state, minutes until expiry, already used): rows written directly, so that
    # no begin_oidc_login call of the test purges the others on the way.
    for state, minutes_left, used in [
        ("expired-long-ago", -50, False),
        ("expires-right-now", 0, False),
        ("expired-and-used", -5, True),
        ("live", 1, False),
        ("live-and-used", 5, True),
    ]:
        expires = NOW + timedelta(minutes=minutes_left)
        new_oidc_state(
            session,
            state,
            provider="telegram",
            created_at=expires - timedelta(minutes=10),
            expires_at=expires,
            consumed_at=NOW if used else None,
        )

    start(session, state="new")

    kept = {row.state_hash for row in session.scalars(sa.select(OidcState))}
    assert kept == {hash_secret(state) for state in ("live", "live-and-used", "new")}


def test_a_state_cannot_be_registered_twice(session):
    start(session, state="same")

    with pytest.raises(sa.exc.IntegrityError, match="oidc_states.state_hash"):
        start(session, state="same")


# --- an OIDC login: consume -------------------------------------------------------


def test_a_login_is_consumed_once(session):
    start(session)

    first = consume_oidc_login(session, "telegram", "state-value", now=NOW + timedelta(seconds=5))
    second = consume_oidc_login(session, "telegram", "state-value", now=NOW + timedelta(seconds=6))

    assert first == OidcLoginSecrets(nonce="nonce-value", code_verifier="verifier-value")
    assert second is None
    session.expire_all()  # the UPDATE bypassed the objects this session holds
    row = session.scalars(sa.select(OidcState)).one()
    assert row.consumed_at == NOW + timedelta(seconds=5)  # the first use, not overwritten


def test_the_secrets_of_a_login_stay_out_of_its_repr():
    secrets = OidcLoginSecrets(nonce="nonce-value", code_verifier="verifier-value")

    assert "nonce-value" not in repr(secrets) and "verifier-value" not in repr(secrets)


@pytest.mark.parametrize(
    "state",
    ["never-registered", "", "a" * 129, "has space", "../etc/passwd", "ünï", "a\nb", None, 123, b"bytes"],
)
def test_unknown_and_malformed_states_are_refused(session, state):
    start(session)

    assert consume_oidc_login(session, "telegram", state, now=NOW) is None
    session.expire_all()
    assert session.scalars(sa.select(OidcState)).one().consumed_at is None


def test_a_login_of_another_provider_cannot_be_consumed(session):
    start(session)

    assert consume_oidc_login(session, "google", "state-value", now=NOW) is None
    session.expire_all()
    assert session.scalars(sa.select(OidcState)).one().consumed_at is None


def test_a_login_is_over_at_its_expiry(session):
    start(session)
    expiry = NOW + authn.OIDC_LOGIN_LIFETIME

    assert consume_oidc_login(session, "telegram", "state-value", now=expiry) is None
    assert consume_oidc_login(session, "telegram", "state-value", now=expiry + timedelta(days=1)) is None
    session.expire_all()
    assert session.scalars(sa.select(OidcState)).one().consumed_at is None  # a refusal marks nothing
    just_before = expiry - timedelta(microseconds=1)
    assert consume_oidc_login(session, "telegram", "state-value", now=just_before) is not None


# --- the end of a login -----------------------------------------------------------


def test_the_first_login_creates_the_identity_link_and_a_session(session, user):
    issued = log_in(session)
    session.commit()

    assert isinstance(issued, IssuedSession)
    assert issued.user_id == user.id and isinstance(issued.session_id, UUID)
    assert issued.expires_at == NOW + authn.SESSION_LIFETIME
    link = session.scalars(sa.select(IdentityLink)).one()
    assert (link.user_id, link.provider, link.subject) == (user.id, "telegram", SUB)
    assert link.last_login_at == NOW and link.email is None
    stored = session.scalars(sa.select(Session)).one()
    assert (stored.id, stored.user_id) == (issued.session_id, user.id)
    assert stored.created_at == stored.last_seen_at == NOW
    assert stored.expires_at == NOW + authn.SESSION_LIFETIME and stored.revoked_at is None


def test_the_session_lifetime_is_fourteen_days_unless_given():
    assert authn.SESSION_LIFETIME == timedelta(days=14)


def test_a_given_session_lifetime_is_used(session, user):
    issued = log_in(session, lifetime=timedelta(hours=1))

    assert issued.expires_at == NOW + timedelta(hours=1)


def test_a_later_login_reuses_the_link_and_moves_last_login(session, user):
    log_in(session)
    later = NOW + timedelta(days=2)

    second = log_in(session, now=later)
    session.commit()
    session.expire_all()

    assert count(session, IdentityLink) == 1
    assert session.scalars(sa.select(IdentityLink)).one().last_login_at == later
    assert count(session, Session) == 2
    assert second.expires_at == later + authn.SESSION_LIFETIME


def test_a_session_token_is_random_urlsafe_and_stored_only_as_a_digest(session, user):
    tokens = [log_in(session).token for _ in range(5)]
    session.commit()

    assert len(set(tokens)) == 5
    assert all(TOKEN_SHAPE.fullmatch(token) for token in tokens)
    assert {row.token_hash for row in session.scalars(sa.select(Session))} == {
        hash_secret(token) for token in tokens
    }
    stored = row_text(session, "sessions")
    assert not any(token in stored for token in tokens)


def test_the_issued_session_hides_its_token_from_repr(session, user):
    issued = log_in(session)

    assert issued.token not in repr(issued)


@pytest.mark.parametrize(
    "subject",
    ["9999999999", "", "@someone", "somebody@example.com", "ünï", " " + SUB, SUB + "\n", None, 42, "x" * 256],
)
def test_an_identity_that_is_not_on_the_allowlist_gets_nothing(session, user, subject):
    users_before = count(session, User)

    with pytest.raises(LoginDenied):
        log_in(session, subject)
    session.rollback()

    assert count(session, IdentityLink) == 0 and count(session, Session) == 0
    assert count(session, User) == users_before == 1  # nobody is created by signing in


def test_a_provider_other_than_the_allowlisted_one_gets_nothing(session, user):
    with pytest.raises(LoginDenied):
        complete_login(session, "google", SUB, now=NOW)
    with pytest.raises(LoginDenied):
        complete_login(session, "email", SUB, now=NOW)  # not an address, and not listed


def test_a_revoked_identity_is_denied_and_a_lifted_revocation_signs_in_again(session, user):
    assert revoke(session, "telegram", SUB, now=NOW)
    session.commit()

    with pytest.raises(LoginDenied):
        log_in(session)
    session.rollback()
    assert count(session, Session) == 0

    allow(session, user, "telegram", SUB)  # lifts the revocation
    session.commit()
    assert log_in(session).user_id == user.id


def test_a_disabled_user_is_denied(session, user):
    user.disabled_at = NOW
    session.commit()

    with pytest.raises(LoginDenied):
        log_in(session)
    session.rollback()

    assert count(session, Session) == 0 and count(session, IdentityLink) == 0


def test_a_denied_login_leaves_the_last_login_of_a_known_identity_alone(session, user):
    log_in(session)
    session.commit()
    revoke(session, "telegram", SUB, now=NOW)
    session.commit()

    with pytest.raises(LoginDenied):
        log_in(session, now=NOW + timedelta(days=3))
    session.rollback()  # what the caller's transaction does when LoginDenied propagates

    assert session.scalars(sa.select(IdentityLink)).one().last_login_at == NOW


def test_each_identity_signs_in_as_its_own_user_and_users_are_never_merged(session, user):
    other = new_user(session, display_name="Somebody else")
    allow(session, other, "telegram", "777")
    session.commit()

    mine = log_in(session)
    theirs = log_in(session, "777")
    session.commit()

    assert (mine.user_id, theirs.user_id) == (user.id, other.id)
    links = {(link.subject, link.user_id) for link in session.scalars(sa.select(IdentityLink))}
    assert links == {(SUB, user.id), ("777", other.id)}
    assert count(session, User) == 2


def test_an_identity_is_never_matched_by_a_display_name_or_a_lookalike(session, user):
    # The user's display name and a username-like string are not identities.
    for subject in ["Elena", "elena", "@elena"]:
        with pytest.raises(LoginDenied):
            log_in(session, subject)
        session.rollback()
    assert count(session, Session) == 0


def test_a_link_that_somehow_belongs_to_another_user_is_refused(session, user, monkeypatch):
    other = new_user(session, display_name="Somebody else")
    monkeypatch.setattr(authn, "_link_owner", lambda *args: other.id)

    with pytest.raises(LoginDenied):
        log_in(session)
    session.rollback()

    assert count(session, Session) == 0


def test_losing_the_race_to_create_the_link_uses_the_winners_link(session, user, monkeypatch):
    # PostgreSQL's case: the look says "no link", the insert then meets the one a
    # concurrent login committed. Reproduced by hiding the existing link once.
    log_in(session)
    session.commit()
    real = authn._link_owner
    looks = []

    def blind_the_first_time(db, provider, subject):
        looks.append(1)
        return None if len(looks) == 1 else real(db, provider, subject)

    monkeypatch.setattr(authn, "_link_owner", blind_the_first_time)

    issued = log_in(session, now=NOW + timedelta(hours=1))
    session.commit()

    assert issued.user_id == user.id and len(looks) == 2
    assert count(session, IdentityLink) == 1 and count(session, Session) == 2


def test_losing_the_race_and_finding_no_link_afterwards_is_a_denial(session, user, monkeypatch):
    def refuse(*args):
        raise sa.exc.IntegrityError("INSERT", {}, Exception("unique"))

    monkeypatch.setattr(authn, "_link_owner", lambda *args: None)
    monkeypatch.setattr(session, "add", refuse)

    with pytest.raises(LoginDenied):
        log_in(session)
    session.rollback()

    assert count(session, Session) == 0


# --- authenticate -----------------------------------------------------------------


@pytest.fixture
def issued(session, user):
    issued = log_in(session)
    session.commit()
    return issued


def test_a_live_session_resolves_to_its_user_as_plain_values(session, user, issued):
    found = authenticate(session, issued.token, now=NOW + timedelta(days=1))

    assert found == AuthenticatedSession(
        user_id=user.id, session_id=issued.session_id, expires_at=NOW + authn.SESSION_LIFETIME
    )
    with pytest.raises(Exception):
        found.user_id = UUID(int=0)  # frozen


@pytest.mark.parametrize(
    "token",
    [None, "", "short", "a" * 42, "a" * 44, "a" * 43, "!" * 43, "é" * 43, " " + "a" * 42, 42, b"x" * 43],
)
def test_a_token_that_is_not_ours_or_not_known_resolves_to_nothing(session, issued, token):
    assert authenticate(session, token, now=NOW) is None


def test_the_digest_of_a_token_is_not_a_token(session, issued):
    # What a copy of the database holds cannot be replayed as the cookie.
    digest = hash_secret(issued.token)

    assert authenticate(session, digest, now=NOW) is None
    assert authenticate(session, digest[:43], now=NOW) is None


def test_a_revoked_session_resolves_to_nothing(session, issued):
    assert revoke_session(session, issued.token, now=NOW + timedelta(hours=1))
    session.commit()

    assert authenticate(session, issued.token, now=NOW + timedelta(hours=2)) is None


def test_a_session_is_over_at_its_expiry_not_after(session, issued):
    expiry = NOW + authn.SESSION_LIFETIME

    assert authenticate(session, issued.token, now=expiry - timedelta(microseconds=1)) is not None
    assert authenticate(session, issued.token, now=expiry) is None
    assert authenticate(session, issued.token, now=expiry + timedelta(days=365)) is None


def test_a_disabled_user_loses_the_sessions_they_had(session, user, issued):
    user.disabled_at = NOW + timedelta(minutes=1)
    session.commit()

    assert authenticate(session, issued.token, now=NOW + timedelta(minutes=2)) is None


def test_withdrawing_access_ends_the_sessions_at_once(session, issued):
    revoke(session, "telegram", SUB, now=NOW + timedelta(minutes=1))
    session.commit()

    assert authenticate(session, issued.token, now=NOW + timedelta(minutes=2)) is None


def test_lifting_a_revocation_restores_the_session_that_never_expired(session, user, issued):
    revoke(session, "telegram", SUB, now=NOW)
    session.commit()
    assert authenticate(session, issued.token, now=NOW) is None

    allow(session, user, "telegram", SUB)
    session.commit()

    assert authenticate(session, issued.token, now=NOW) is not None


def test_a_session_survives_while_the_user_keeps_another_active_identity(session, user, issued):
    # A session records no identity it came through (the schema has no column for
    # it), so access is "the user has some entry that is not revoked". Pinned so
    # that changing it is a decision.
    allow(session, user, "email", "elena@example.com")
    session.commit()
    revoke(session, "telegram", SUB, now=NOW)
    session.commit()
    assert authenticate(session, issued.token, now=NOW) is not None

    revoke(session, "email", "elena@example.com", now=NOW)
    session.commit()
    assert authenticate(session, issued.token, now=NOW) is None


def test_a_session_never_resolves_to_a_different_user(session, user, issued):
    other = new_user(session)
    allow(session, other, "telegram", "777")
    other_issued = log_in(session, "777")
    session.commit()

    assert authenticate(session, issued.token, now=NOW).user_id == user.id
    assert authenticate(session, other_issued.token, now=NOW).user_id == other.id


def test_validating_a_session_only_reads(engine, factory, user, issued):
    with factory() as reader, statements(engine) as seen:
        with reader.begin():
            assert authenticate(reader, issued.token, now=NOW) is not None
            assert authenticate(reader, "not-a-token", now=NOW) is None
            assert not (reader.new or reader.dirty or reader.deleted)

    assert seen and all(statement.lstrip().upper().startswith("SELECT") for statement in seen), seen


# --- revoke_session ---------------------------------------------------------------


def test_revoking_a_session_keeps_the_first_revocation_and_touches_nothing_else(session, user, issued):
    other = log_in(session, now=NOW)
    session.commit()

    assert revoke_session(session, issued.token, now=NOW + timedelta(hours=1)) is True
    assert revoke_session(session, issued.token, now=NOW + timedelta(hours=2)) is False
    session.commit()
    session.expire_all()

    by_id = {row.id: row for row in session.scalars(sa.select(Session))}
    assert by_id[issued.session_id].revoked_at == NOW + timedelta(hours=1)
    assert by_id[other.session_id].revoked_at is None
    assert authenticate(session, other.token, now=NOW) is not None


@pytest.mark.parametrize("token", [None, "", "x" * 43, "!" * 43, 7])
def test_revoking_what_is_not_a_session_changes_nothing(session, issued, token):
    assert revoke_session(session, token, now=NOW) is False
    session.expire_all()
    assert session.scalars(sa.select(Session)).one().revoked_at is None


# --- the order of statements: writers write first -------------------------------------


def first_statement(engine, factory, action) -> str:
    with factory() as writer, statements(engine) as seen:
        with writer.begin():
            action(writer)
    return seen[0].lstrip().split(None, 1)[0].upper()


@pytest.mark.parametrize(
    "name, action",
    [
        ("begin", lambda s: start(s, state="fresh")),
        ("consume a live state", lambda s: consume_oidc_login(s, "telegram", "state-value", now=NOW)),
        ("consume an unknown state", lambda s: consume_oidc_login(s, "telegram", "nope", now=NOW)),
        ("log in", lambda s: log_in(s)),
    ],
)
def test_the_writers_start_with_a_write(engine, factory, user, name, action):
    with factory() as setup, setup.begin():
        start(setup)

    assert first_statement(engine, factory, action) in {"UPDATE", "DELETE"}


def test_a_denied_login_has_also_started_with_a_write(engine, factory, user):
    def denied(session):
        with pytest.raises(LoginDenied):
            log_in(session, "unknown-subject")

    assert first_statement(engine, factory, denied) == "UPDATE"


def test_revoking_a_session_starts_with_a_write(engine, factory, issued):
    first = first_statement(engine, factory, lambda s: revoke_session(s, issued.token, now=NOW))

    assert first == "UPDATE"


# --- concurrency (real threads, a real file) ----------------------------------------------


def run_concurrently(factory, count_, work):
    """`work(session)` in `count_` threads at the same moment, one transaction each.
    Returns (results, errors)."""
    barrier = threading.Barrier(count_)

    def one(_):
        with factory() as db:
            barrier.wait(timeout=10)
            with db.begin():
                return work(db)

    results, errors = [], []
    with ThreadPoolExecutor(max_workers=count_) as pool:
        futures = [pool.submit(one, index) for index in range(count_)]
        for future in futures:
            try:
                results.append(future.result(timeout=60))
            except Exception as exc:  # collected so the test can say what happened
                errors.append(exc)
    return results, errors


@pytest.fixture
def slow_allowlist_reads(monkeypatch):
    """Make a login linger after its allowlist lookup, so that the others have time
    to commit between a login's first read and its first write, if it reads first. A login that writes first holds the write lock through the delay and
    the others queue behind it, which is the behaviour under test; one that reads
    first is overtaken and fails with SQLITE_BUSY_SNAPSHOT."""
    real = authn.allowed_user

    def slow(*args, **kwargs):
        found = real(*args, **kwargs)  # the read first...
        time.sleep(0.05)  # ...then the delay before whatever the login does next
        return found

    monkeypatch.setattr(authn, "allowed_user", slow)


def test_concurrent_first_logins_queue_on_the_write_lock_and_all_succeed(
    factory, user, slow_allowlist_reads
):
    results, errors = run_concurrently(factory, 8, lambda db: log_in(db))

    assert errors == []
    assert len({issued.token for issued in results}) == 8
    with factory() as db:
        assert count(db, IdentityLink) == 1  # one link, created once
        assert count(db, Session) == 8
        assert db.scalars(sa.select(IdentityLink)).one().user_id == user.id


def test_concurrent_logins_of_a_known_identity_all_succeed(factory, user, slow_allowlist_reads):
    with factory() as db, db.begin():
        log_in(db)

    results, errors = run_concurrently(factory, 8, lambda db: log_in(db, now=NOW + timedelta(hours=1)))

    assert errors == [] and len(results) == 8


def test_of_concurrent_consumers_of_one_state_exactly_one_wins(factory):
    with factory() as db, db.begin():
        start(db)

    results, errors = run_concurrently(
        factory, 8, lambda db: consume_oidc_login(db, "telegram", "state-value", now=NOW)
    )

    assert errors == []
    assert sum(result is not None for result in results) == 1


def test_revocations_racing_validations_never_error_and_end_revoked(factory, user, issued):
    turns = itertools.count()  # next() on a count is atomic

    def revoke_or_validate(db):
        if next(turns) % 2 == 0:
            return revoke_session(db, issued.token, now=NOW)
        return authenticate(db, issued.token, now=NOW)

    results, errors = run_concurrently(factory, 6, revoke_or_validate)

    assert errors == []
    assert sum(result is True for result in results) == 1  # exactly one revocation took
    with factory() as db:
        assert authenticate(db, issued.token, now=NOW) is None
