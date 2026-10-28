"""What `purge-expired` deletes, what it keeps, and why that is safe.

The properties, each pinned below with synthetic rows in a migrated SQLite file:

  * a sign-in artifact (session, login state, emailed link, invitation) is deleted when its
    OWN expiry has passed, whether it was used, withdrawn or never touched, and not a moment
    before; a live one, a used-but-unexpired one and a revoked-but-unexpired one stay, and
    every credential that was refused before the purge is refused, the same way, after it;
  * history is judged row by row: a message or turn request goes at 365 x 24 hours after its
    creation (at the cutoff itself it is already over, as for every read), a recent message of
    an old conversation stays, an old empty conversation goes, and a request in flight under a
    live lease is not touched;
  * nothing else is touched: users, identities, and any table the purge does not name (the
    favourites and preferences of later increments);
  * the database is sound afterwards, a second run changes nothing, and a dry run changes
    nothing and predicts exactly what a real run deletes.

The last group compares the SQL with an independent oracle, written in Python from the rules
alone, over random worlds that include every boundary.
"""

import random
import sqlite3
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from house_helper.admin import retention
from house_helper.admin.retention import Category
from house_helper.admin.target import open_target
from house_helper.persistence import authn, email_login, invitations
from house_helper.persistence.allowlist import allow, allowed_user, revoke
from house_helper.persistence.base import RETENTION
from house_helper.persistence.conversations import list_messages
from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    create_db_engine,
    create_session_factory,
)
from tests.admin_support import db_path, db_url, migrated_template, snapshot  # noqa: F401  (fixtures)
from tests.persistence_support import stored
from tests.retention_support import (
    CUTOFF,
    MICROSECOND,
    NOW,
    Seed,
    ago,
    count,
    counts,
    dump,
    integrity,
    read,
    seeded,
)


def purge(url, *, now=NOW, dry_run=False, **options):
    """The purge, in this process, on the engine the command line itself would use."""
    engine = open_target({DATABASE_URL_ENV: url})
    try:
        return retention.purge_expired(engine, now=now, dry_run=dry_run, pause=lambda: None, **options)
    finally:
        engine.dispose()


def key(row):
    return row.id.hex if isinstance(row.id, uuid.UUID) else row.id


def present(url, table):
    return {row[0] for row in read(url, f"SELECT id FROM {table}")}


def check(url, gone, kept):
    """Every row in `gone` is absent and every row in `kept` present, table by table."""
    for table, rows in gone.items():
        assert not ({key(r) for r in rows} & present(url, table)), f"{table}: a row that had to go is still there"
    for table, rows in kept.items():
        assert {key(r) for r in rows} <= present(url, table), f"{table}: a row that had to stay is gone"


# --- authentication artifacts: what goes, what stays ------------------------------------------


def seed_artifacts(s: Seed):
    gone, kept = {}, {}

    owner = s.user()
    link = s.telegram(owner)
    gone["sessions"] = [
        s.login_session(owner, link, created=ago(days=20), expires=ago(days=6)),
        s.login_session(owner, link, created=ago(days=14), expires=NOW),  # the end is exclusive
        s.login_session(owner, link, created=ago(days=20), expires=ago(days=6), revoked_at=ago(days=7)),
    ]
    kept["sessions"] = [
        s.login_session(owner, link, created=ago(days=14), expires=NOW + MICROSECOND),
        s.login_session(owner, link, created=ago(days=1), expires=NOW + timedelta(days=13)),
        # revoked and not yet expired: the revocation stays on record until its own deadline
        s.login_session(owner, link, created=ago(days=1), expires=NOW + timedelta(days=13), revoked_at=ago(hours=1)),
    ]

    gone["oidc_states"] = [
        s.login_state(created=ago(hours=2), expires=ago(hours=1, minutes=50)),
        s.login_state(created=ago(hours=2), expires=ago(hours=1, minutes=50), consumed_at=ago(hours=1, minutes=55)),
        s.login_state(created=ago(minutes=10), expires=NOW),
    ]
    kept["oidc_states"] = [
        s.login_state(created=ago(minutes=5), expires=NOW + timedelta(minutes=5)),
        s.login_state(created=ago(minutes=5), expires=NOW + timedelta(minutes=5), consumed_at=ago(minutes=1)),
        s.login_state(created=ago(minutes=10), expires=NOW + MICROSECOND),
    ]

    address = s.email_address(owner)
    gone["email_login_challenges"] = [
        s.challenge(owner, address, created=ago(hours=1), expires=ago(minutes=50)),
        s.challenge(owner, address, created=ago(hours=1), expires=ago(minutes=50), consumed_at=ago(minutes=55)),
        s.challenge(owner, address, created=ago(hours=1), expires=ago(minutes=50), revoked_at=ago(minutes=55)),
        s.challenge(owner, address, created=ago(minutes=10), expires=NOW),
    ]
    kept["email_login_challenges"] = [
        s.challenge(owner, address, created=ago(minutes=1), expires=NOW + timedelta(minutes=9)),
        s.challenge(owner, address, created=ago(minutes=1), expires=NOW + timedelta(minutes=9), consumed_at=ago(seconds=30)),
        s.challenge(owner, address, created=ago(minutes=1), expires=NOW + timedelta(minutes=9), revoked_at=ago(seconds=30)),
    ]

    # At most one invitation per user is outstanding (neither used nor withdrawn): one user each.
    def invited(**fields):
        return s.invitation(s.user(), **fields)

    gone["identity_invitations"] = [
        invited(created=ago(hours=5), expires=ago(hours=3)),  # expired, never used
        invited(created=ago(hours=5), expires=ago(hours=3), consumed_at=ago(hours=4)),  # used, now expired
        invited(created=ago(hours=5), expires=ago(hours=3), revoked_at=ago(hours=4)),  # withdrawn, now expired
        invited(created=ago(hours=2), expires=NOW),  # the end is exclusive
    ]
    kept["identity_invitations"] = [
        invited(created=ago(minutes=30), expires=NOW + timedelta(hours=1, minutes=30)),  # active
        invited(created=ago(minutes=30), expires=NOW + timedelta(hours=1, minutes=30), consumed_at=ago(minutes=5)),
        invited(created=ago(minutes=30), expires=NOW + timedelta(hours=1, minutes=30), revoked_at=ago(minutes=5)),
        invited(created=ago(hours=2), expires=NOW + MICROSECOND),
    ]
    return gone, kept


def test_expired_authentication_artifacts_are_deleted_and_the_others_stay(db_url):
    gone, kept = seeded(db_url, seed_artifacts)

    report = purge(db_url)

    check(db_url, gone, kept)
    for table, rows in gone.items():
        assert report.deleted[table] == len(rows), table
    for table, rows in kept.items():
        assert count(db_url, table) == len(rows), table
    integrity(db_url)


def test_what_a_run_deletes_is_only_what_it_reports_and_users_and_identities_are_untouched(db_url):
    seeded(db_url, seed_artifacts)
    before = counts(db_url)

    report = purge(db_url)

    after = counts(db_url)
    for name in ("users", "allowlist_entries", "identity_links", "alembic_version"):
        assert after[name] == before[name], name
    assert before["sessions"] - after["sessions"] == report.deleted["sessions"]
    assert report.total == sum(before[t] - after[t] for t in before)


def test_a_login_state_bound_to_a_live_login_keeps_its_expired_invitation_for_the_next_run(db_url):
    def fill(s):
        user = s.user()
        # The invitation is over; the login that began just before it ended is not.
        invitation = s.invitation(user, created=ago(hours=2, minutes=5), expires=ago(minutes=1))
        live = s.login_state(created=ago(minutes=4), expires=NOW + timedelta(minutes=6), invitation=invitation)
        dead = s.login_state(created=ago(minutes=40), expires=ago(minutes=30), invitation=invitation)
        return invitation, live, dead

    invitation, live, dead = seeded(db_url, fill)

    first = purge(db_url)
    assert first.deleted["identity_invitations"] == 0
    assert first.deleted["oidc_states"] == 1
    check(db_url, {"oidc_states": [dead]}, {"identity_invitations": [invitation], "oidc_states": [live]})

    when = NOW + timedelta(minutes=7)  # the login is over, and with it the hold
    preview = purge(db_url, now=when, dry_run=True)
    later = purge(db_url, now=when)
    assert later.deleted == preview.deleted
    assert later.deleted["identity_invitations"] == 1 and later.deleted["oidc_states"] == 1
    assert count(db_url, "identity_invitations") == 0 and count(db_url, "oidc_states") == 0
    integrity(db_url)


# --- the same credential, before and after: revocation and replay prevention ------------------

T0 = ago(days=40)


def tx(db_url, work):
    engine = create_db_engine(db_url)
    try:
        with create_session_factory(engine)() as session, session.begin():
            return work(session)
    finally:
        engine.dispose()


def provisioned_user(db_url, provider, subject):
    def work(session):
        user = Seed(session).user()
        allow(session, user, provider, subject)
        return user.id

    return tx(db_url, work)


def test_a_revoked_or_expired_session_is_refused_before_and_after_the_purge_and_a_live_one_still_works(db_url):
    provisioned_user(db_url, "telegram", "5001")
    live = tx(db_url, lambda s: authn.complete_login(s, "telegram", "5001", now=T0))
    revoked = tx(db_url, lambda s: authn.complete_login(s, "telegram", "5001", now=T0))
    assert tx(db_url, lambda s: authn.revoke_session(s, revoked.token, now=T0 + timedelta(days=1)))

    def authenticated(token, now):
        return tx(db_url, lambda s: authn.authenticate(s, token, now=now)) is not None

    day2 = T0 + timedelta(days=2)
    assert authenticated(live.token, day2) and not authenticated(revoked.token, day2)
    first = purge(db_url, now=day2)
    assert first.deleted["sessions"] == 0  # a revoked session stays until its own deadline
    assert count(db_url, "sessions") == 2
    assert authenticated(live.token, day2) and not authenticated(revoked.token, day2)

    day15 = T0 + timedelta(days=15)  # both are past their 14 days
    assert not authenticated(live.token, day15) and not authenticated(revoked.token, day15)
    assert purge(db_url, now=day15).deleted["sessions"] == 2
    assert count(db_url, "sessions") == 0
    assert not authenticated(live.token, day15) and not authenticated(revoked.token, day15)
    assert tx(db_url, lambda s: authn.revoke_session(s, live.token, now=day15)) is False  # unknown: same as before


def test_a_login_state_cannot_be_replayed_before_or_after_the_purge(db_url):
    tx(db_url, lambda s: authn.begin_oidc_login(s, "telegram", "state-A", "nonce", "verifier", now=T0))

    def consume(minutes):
        return tx(db_url, lambda s: authn.consume_oidc_login(s, "telegram", "state-A", now=T0 + timedelta(minutes=minutes)))

    assert consume(1) is not None  # the legitimate return from Telegram
    assert consume(2) is None
    assert purge(db_url, now=T0 + timedelta(minutes=5)).deleted["oidc_states"] == 0  # used, not yet expired
    assert count(db_url, "oidc_states") == 1 and consume(6) is None  # still refused: it is on record as used
    assert purge(db_url, now=T0 + timedelta(minutes=11)).deleted["oidc_states"] == 1
    assert count(db_url, "oidc_states") == 0 and consume(12) is None  # unknown is refused like used


def test_a_used_invitation_cannot_be_claimed_again_before_or_after_the_purge_and_the_identity_stays(db_url):
    user_id = tx(db_url, lambda s: Seed(s).user().id)  # a user with no Telegram identity yet
    issued = tx(db_url, lambda s: invitations.issue_invitation(s, user_id, now=T0))
    invitation_id = str(issued.invitation_id)

    first = tx(
        db_url,
        lambda s: invitations.complete_invitation_login(
            s, invitation_id, "6001", now=T0 + timedelta(minutes=1)
        ),
    )
    assert first.user_id == user_id

    def claim(minutes):
        return tx(
            db_url,
            lambda s: invitations.claim_invitation(s, invitation_id, "6002", now=T0 + timedelta(minutes=minutes)),
        )

    def verify(minutes):
        return tx(
            db_url,
            lambda s: invitations.verify_invitation(s, invitation_id, issued.token, now=T0 + timedelta(minutes=minutes)),
        )

    assert claim(2) is None and verify(2) is None
    assert purge(db_url, now=T0 + timedelta(minutes=30)).deleted["identity_invitations"] == 0
    assert count(db_url, "identity_invitations") == 1 and claim(31) is None  # used and on record

    assert purge(db_url, now=T0 + timedelta(hours=3)).deleted["identity_invitations"] == 1
    assert count(db_url, "identity_invitations") == 0
    assert claim(181) is None and verify(181) is None  # unknown is refused like used
    # The identity the invitation linked is not an artifact: it is still the user's.
    assert tx(db_url, lambda s: allowed_user(s, "telegram", "6001")).id == user_id
    assert count(db_url, "allowlist_entries") == 1 and count(db_url, "identity_links") == 1


def test_an_emailed_link_cannot_be_redeemed_again_before_or_after_the_purge(db_url):
    provisioned_user(db_url, "email", "cook@example.com")
    link = tx(db_url, lambda s: email_login.issue_email_login(s, "cook@example.com", now=T0)).link
    challenge_id = str(link.challenge_id)

    def redeem(minutes):
        return tx(
            db_url,
            lambda s: email_login.redeem_email_login(s, challenge_id, link.token, now=T0 + timedelta(minutes=minutes)),
        )

    assert redeem(1) is not None
    assert redeem(2) is None
    assert purge(db_url, now=T0 + timedelta(minutes=5)).deleted["email_login_challenges"] == 0
    assert redeem(6) is None and count(db_url, "email_login_challenges") == 1
    assert purge(db_url, now=T0 + timedelta(minutes=11)).deleted["email_login_challenges"] == 1
    assert redeem(12) is None
    preview = tx(
        db_url,
        lambda s: email_login.preview_email_login(s, challenge_id, now=T0 + timedelta(minutes=12)),
    )
    assert preview is None


def test_a_withdrawn_sign_in_link_and_a_revoked_identity_stay_refused_after_the_purge(db_url):
    provisioned_user(db_url, "email", "cook@example.com")
    link = tx(db_url, lambda s: email_login.issue_email_login(s, "cook@example.com", now=T0)).link
    assert tx(db_url, lambda s: revoke(s, "email", "cook@example.com", T0 + timedelta(minutes=1)))

    def redeem(minutes):
        return tx(
            db_url,
            lambda s: email_login.redeem_email_login(
                s, str(link.challenge_id), link.token, now=T0 + timedelta(minutes=minutes)
            ),
        )

    assert redeem(2) is None
    purge(db_url, now=T0 + timedelta(hours=1))
    assert redeem(61) is None
    # Revoking an identity is the allowlist's record, not an artifact: it is never purged.
    assert read(db_url, "SELECT revoked_at IS NOT NULL FROM allowlist_entries") == [(1,)]
    assert tx(db_url, lambda s: allowed_user(s, "email", "cook@example.com")) is None


# --- conversation history ---------------------------------------------------------------------


def seed_history(s: Seed):
    gone, kept = {"messages": [], "turn_requests": [], "conversations": []}, {
        "messages": [],
        "turn_requests": [],
        "conversations": [],
    }
    user, other = s.user(), s.user()

    # A long-lived conversation: old exchanges, one exactly at the edge, recent ones.
    long_lived = s.conversation(user, created=ago(days=500), updated=ago(days=2))
    old = s.exchange(long_lived, created=ago(days=400))
    edge_old = s.exchange(long_lived, created=ago(days=365, hours=1))
    almost = s.exchange(long_lived, created=ago(days=364, hours=23))
    recent = s.exchange(long_lived, created=ago(days=10))
    newest = s.exchange(long_lived, created=ago(days=2))
    for exchange in (old, edge_old):
        gone["messages"] += [exchange.asked, exchange.answer]
        gone["turn_requests"].append(exchange.request)
    for exchange in (almost, recent, newest):
        kept["messages"] += [exchange.asked, exchange.answer]
        kept["turn_requests"].append(exchange.request)
    kept["conversations"].append(long_lived)

    # A conversation whose every message is old: it ends up empty and goes too.
    over = s.conversation(user, created=ago(days=450), updated=ago(days=380))
    done = s.exchange(over, created=ago(days=380))
    note = s.message(over, "system_note", "context reset", created=ago(days=379))
    gone["messages"] += [done.asked, done.answer, note]
    gone["turn_requests"].append(done.request)
    gone["conversations"].append(over)

    # Empty: an old one goes, one that has only just been created stays.
    gone["conversations"].append(s.conversation(user, created=ago(days=366)))
    kept["conversations"].append(s.conversation(user, created=ago(minutes=3)))

    # Someone else's recent conversation is nobody's business here.
    theirs = s.conversation(other, created=ago(days=3))
    theirs_exchange = s.exchange(theirs, created=ago(days=3))
    kept["messages"] += [theirs_exchange.asked, theirs_exchange.answer]
    kept["turn_requests"].append(theirs_exchange.request)
    kept["conversations"].append(theirs)
    return gone, kept


def test_history_older_than_365_days_is_deleted_and_newer_history_stays(db_url):
    gone, kept = seeded(db_url, seed_history)

    report = purge(db_url)

    check(db_url, gone, kept)
    for table in ("messages", "turn_requests", "conversations"):
        assert report.deleted[table] == len(gone[table]), table
        assert count(db_url, table) == len(kept[table]), table
    integrity(db_url)


def test_a_long_lived_conversation_keeps_its_recent_messages_and_reads_show_them_unchanged(db_url):
    gone, kept = seeded(db_url, seed_history)
    conversation = kept["conversations"][0]

    def shown():
        engine = create_db_engine(db_url)
        try:
            with create_session_factory(engine)() as session:
                page = list_messages(session, conversation.user_id, conversation.id, now=NOW, limit=100)
                return [(m.id, m.role, m.text, m.status) for m in page.messages]
        finally:
            engine.dispose()

    before = shown()
    purge(db_url)
    assert shown() == before
    assert [m[0] for m in before] == sorted(key(m) for m in kept["messages"] if m.conversation_id == conversation.id)
    assert count(db_url, "conversations") == len(kept["conversations"])


def test_history_at_the_cutoff_is_over_and_one_microsecond_newer_is_not(db_url):
    def fill(s):
        conversation = s.conversation(s.user(), created=ago(days=500), updated=ago(days=1))
        at = s.message(conversation, "user", "created exactly 365 x 24 hours ago", created=CUTOFF)
        after = s.message(conversation, "user", "one microsecond younger", created=CUTOFF + MICROSECOND)
        before = s.message(conversation, "user", "one microsecond older", created=CUTOFF - MICROSECOND)
        return at, after, before

    at, after, before = seeded(db_url, fill)

    purge(db_url)

    # The same instant ends a message for every read (expires_at must be after now to show).
    assert present(db_url, "messages") == {after.id}
    assert at.expires_at == NOW and before.expires_at < NOW


def test_the_stored_deadline_ends_a_row_before_the_ceiling_and_the_ceiling_ends_a_row_with_a_wrong_deadline(db_url):
    def fill(s):
        conversation = s.conversation(s.user(), created=ago(days=500), updated=ago(days=1))
        early = s.message(conversation, "user", "deadline passed", created=ago(days=3), expires=ago(days=1))
        fine = s.message(conversation, "user", "within its deadline", created=ago(days=3))
        return conversation, early, fine

    conversation, early, fine = seeded(db_url, fill)

    # A row whose stored deadline is wrong (a restored or edited database; the CHECK would stop
    # it on a migrated one, so the check is switched off for this one connection to plant it).
    connection = sqlite3.connect(db_url.removeprefix("sqlite:///"))
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at, expires_at) VALUES (?, 'user', "
            "'deadline far away', ?, ?)",
            (
                conversation.id.hex,
                (NOW - timedelta(days=400)).strftime("%Y-%m-%d %H:%M:%S.%f"),
                (NOW + timedelta(days=4000)).strftime("%Y-%m-%d %H:%M:%S.%f"),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    purge(db_url)

    assert present(db_url, "messages") == {fine.id}
    assert early.id not in present(db_url, "messages")


def test_a_request_that_expires_before_its_answer_leaves_the_answer_in_place(db_url):
    def fill(s):
        conversation = s.conversation(s.user(), created=ago(days=500), updated=ago(days=365) + timedelta(seconds=2))
        return s.exchange(conversation, created=CUTOFF - timedelta(seconds=3))

    exchange = seeded(db_url, fill)

    report = purge(db_url)

    assert report.deleted["turn_requests"] == 1 and report.deleted["messages"] == 1
    assert present(db_url, "messages") == {exchange.answer.id}  # still inside its 365 days
    assert count(db_url, "turn_requests") == 0 and count(db_url, "conversations") == 1
    integrity(db_url)


# --- an expired request that is still in flight -----------------------------------------------


def seed_in_flight(s: Seed):
    conversation = s.conversation(s.user(), created=ago(days=600), updated=ago(days=399))
    long_ago = ago(days=400)
    abandoned = {
        "pending": s.exchange(conversation, created=long_ago, status="pending"),
        "processing": s.exchange(conversation, created=long_ago, status="processing"),  # lease ran out long ago
        "committing": s.exchange(conversation, created=long_ago, status="committing"),
    }
    held = s.exchange(conversation, created=long_ago, status="processing", lease=NOW + timedelta(minutes=5))
    return abandoned, held


def test_an_abandoned_request_is_cancelled_and_then_deleted_and_one_under_a_live_lease_is_left_alone(db_url):
    abandoned, held = seeded(db_url, seed_in_flight)

    report = purge(db_url)

    assert report.cancelled_in_flight == 3
    assert report.deleted["turn_requests"] == 3 and report.deleted["messages"] == 3
    # The request with a live lease, its message, and the conversation that holds them stay.
    assert present(db_url, "turn_requests") == {held.request.id.hex}
    assert present(db_url, "messages") == {held.asked.id}
    assert report.held_back_messages == 1
    assert count(db_url, "conversations") == 1
    integrity(db_url)

    # Once the lease is over the worker is gone, and the next run finishes the job.
    later = purge(db_url, now=NOW + timedelta(minutes=6))
    assert later.cancelled_in_flight == 1 and later.deleted["turn_requests"] == 1
    assert count(db_url, "messages") == 0 and count(db_url, "conversations") == 0


def test_a_request_is_cancelled_with_the_columns_its_status_requires_before_it_is_deleted(db_url):
    abandoned, _ = seeded(db_url, seed_in_flight)
    seen = []
    engine = open_target({DATABASE_URL_ENV: db_url})

    @sa.event.listens_for(engine, "before_cursor_execute")
    def watch(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM turn_requests"):
            seen.extend(
                connection.exec_driver_sql(
                    "SELECT status, finished_at IS NOT NULL, failure_kind FROM turn_requests"
                ).fetchall()
            )

    try:
        retention.purge_expired(engine, now=NOW, pause=lambda: None)
    finally:
        engine.dispose()

    # At the moment the DELETE ran, the abandoned requests were already terminal (the CHECK
    # constraints of the table accepted them: no failure kind, a finish time).
    cancelled = [row for row in seen if row[0] == "cancelled"]
    assert len(cancelled) == 3 and all(row[1] == 1 and row[2] is None for row in cancelled)


def test_a_reply_that_is_stored_while_its_request_is_still_committing_is_kept_with_the_request(db_url):
    def fill(s):
        conversation = s.conversation(s.user(), created=ago(days=600), updated=ago(days=399))
        live = s.exchange(
            conversation, created=ago(days=400), status="committing", answered=True,
            lease=NOW + timedelta(minutes=5),
        )
        gone = s.exchange(conversation, created=ago(days=400), status="committing", answered=True)  # lease long over
        return live, gone

    live, gone = seeded(db_url, fill)

    report = purge(db_url)  # (the database refuses to delete a message a request links: a mistake would raise)

    assert report.cancelled_in_flight == 1 and report.deleted["turn_requests"] == 1
    assert present(db_url, "turn_requests") == {live.request.id.hex}
    assert present(db_url, "messages") == {live.asked.id, live.answer.id}
    assert report.held_back_messages == 2
    assert count(db_url, "conversations") == 1
    integrity(db_url)
    later = purge(db_url, now=NOW + timedelta(minutes=6))  # the worker is gone: everything can go
    assert count(db_url, "messages") == 0 and count(db_url, "conversations") == 0
    assert later.deleted["turn_requests"] == 1


# --- a parent is judged by the children that exist, not by the ones that are meant to go -------


def seed_linked_request(s: Seed):
    """An expired request that was abandoned in flight, the question it links, an old note nobody
    links, and the conversation that has been quiet for over a year: the whole chain is eligible,
    in the order request, messages, conversation."""
    conversation = s.conversation(s.user(), created=ago(days=500), updated=ago(days=380))
    stuck = s.exchange(conversation, created=ago(days=400), status="processing")  # its lease ran out long ago
    note = s.message(conversation, "user", "an old note nobody links", created=ago(days=390))
    return SimpleNamespace(conversation=conversation, asked=stuck.asked, request=stuck.request, note=note)


def seed_answered_request(s: Seed):
    """A completed request, past its deadline, with both of the messages it links."""
    conversation = s.conversation(s.user(), created=ago(days=500), updated=ago(days=380))
    return s.exchange(conversation, created=ago(days=400)), conversation


def seed_invitation_with_expired_login(s: Seed):
    """An invitation that is over, and the login state bound to it that is over too."""
    invitation = s.invitation(s.user(), created=ago(hours=2, minutes=5), expires=ago(minutes=1))
    state = s.login_state(created=ago(minutes=40), expires=ago(minutes=30), invitation=invitation)
    return SimpleNamespace(invitation=invitation, state=state)


def seed_old_conversation_with_one_note(s: Seed):
    conversation = s.conversation(s.user(), created=ago(days=500), updated=ago(days=380))
    return SimpleNamespace(
        conversation=conversation, note=s.message(conversation, "user", "an old note", created=ago(days=390))
    )


def run_one_stage(url, table, condition):
    """The statement of ONE stage, alone, in a transaction of its own: what a run does when the
    stages before it left behind a child that is eligible but still there (another run holds the
    child's lock and SKIP LOCKED passed it by). Returns how many rows it deleted."""
    engine = open_target({DATABASE_URL_ENV: url})
    try:
        conditions = retention._Conditions(NOW, CUTOFF)
        with engine.begin() as connection:
            return connection.execute(retention._delete_batch(table, getattr(conditions, condition), 100)).rowcount
    finally:
        engine.dispose()


def test_a_message_is_not_deleted_while_a_request_that_links_it_still_exists_however_expired_the_request_is(db_url):
    world = seeded(db_url, seed_linked_request)

    # The request has not been deleted (the stage before this one skipped it): the statement
    # takes the note, which nothing links, and leaves the question, which the request links.
    # (Before the correction it took both, and the foreign key refused the statement.)
    assert run_one_stage(db_url, retention._messages, "message") == 1
    assert present(db_url, "messages") == {world.asked.id}
    assert present(db_url, "turn_requests") == {world.request.id.hex}
    integrity(db_url)


def test_neither_the_question_nor_the_answer_is_deleted_while_the_request_that_links_them_exists(db_url):
    exchange, conversation = seeded(db_url, seed_answered_request)

    assert run_one_stage(db_url, retention._messages, "message") == 0
    assert present(db_url, "messages") == {exchange.asked.id, exchange.answer.id}
    assert run_one_stage(db_url, retention._conversations, "conversation") == 0
    assert present(db_url, "turn_requests") == {exchange.request.id.hex}
    assert present(db_url, "conversations") == {conversation.id.hex}
    integrity(db_url)


def test_an_invitation_is_not_deleted_while_a_login_state_bound_to_it_still_exists_however_expired_it_is(db_url):
    world = seeded(db_url, seed_invitation_with_expired_login)

    assert run_one_stage(db_url, retention._invitations, "invitation") == 0
    assert present(db_url, "identity_invitations") == {world.invitation.id.hex}
    assert present(db_url, "oidc_states") == {world.state.id.hex}  # and no cascade took it unreported
    integrity(db_url)


def test_a_conversation_is_not_deleted_while_a_message_or_request_in_it_still_exists(db_url):
    chain = seeded(db_url, seed_linked_request)
    assert run_one_stage(db_url, retention._conversations, "conversation") == 0
    assert count(db_url, "messages") == 2 and count(db_url, "turn_requests") == 1  # nothing cascaded away

    note = seeded(db_url, seed_old_conversation_with_one_note)
    assert run_one_stage(db_url, retention._conversations, "conversation") == 0
    assert count(db_url, "messages") == 3 and count(db_url, "conversations") == 2
    assert {chain.conversation.id.hex, note.conversation.id.hex} <= present(db_url, "conversations")
    integrity(db_url)


def test_a_dry_run_counts_the_children_that_its_own_earlier_stages_would_delete(db_url):
    # A dry run deletes nothing, so it cannot look at what the earlier stages left: it counts
    # as gone what they would delete from the same snapshot (the planned conditions).
    seeded(db_url, seed_linked_request)
    seeded(db_url, seed_invitation_with_expired_login)
    seeded(db_url, seed_old_conversation_with_one_note)

    preview = purge(db_url, dry_run=True)

    assert preview.deleted["turn_requests"] == 1 and preview.deleted["identity_invitations"] == 1
    assert preview.deleted["messages"] == 3 and preview.deleted["conversations"] == 2
    real = purge(db_url)
    assert real.deleted == preview.deleted
    assert count(db_url, "messages") == 0 and count(db_url, "conversations") == 0
    integrity(db_url)


# --- tables the purge does not name ------------------------------------------------------------


def test_favourites_and_preferences_survive_the_deletion_of_the_history_they_came_from(db_url):
    gone, kept = seeded(db_url, seed_history)
    owner_id = read(db_url, "SELECT user_id FROM conversations ORDER BY created_at LIMIT 1")[0][0]
    # Independent tables, as the product requirements (KHD-05) describe them: owned by a user,
    # and remembering where they came from by value, not by a key that history could cascade.
    # (The tables of the later increment do not exist yet; their foreign key is the user's.)
    engine = create_db_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE favorites (id INTEGER PRIMARY KEY, user_id CHAR(32) NOT NULL REFERENCES users (id) "
                "ON DELETE CASCADE, title TEXT NOT NULL, ingredients TEXT NOT NULL, source_message_id INTEGER)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE preferences (id INTEGER PRIMARY KEY, user_id CHAR(32) NOT NULL REFERENCES users (id) "
                "ON DELETE CASCADE, statement TEXT NOT NULL, learned_from_conversation CHAR(32))"
            )
            expired = gone["messages"][0]
            connection.exec_driver_sql(
                "INSERT INTO favorites (user_id, title, ingredients, source_message_id) VALUES (?, 'borscht', "
                "'beetroot, cabbage', ?)",
                (owner_id, expired.id),
            )
            connection.exec_driver_sql(
                "INSERT INTO preferences (user_id, statement, learned_from_conversation) VALUES (?, 'no cilantro', ?)",
                (owner_id, gone["conversations"][0].id.hex),
            )
    finally:
        engine.dispose()
    before = {"favorites": dump(db_url)["favorites"], "preferences": dump(db_url)["preferences"]}

    purge(db_url)

    check(db_url, gone, kept)
    after = dump(db_url)
    assert {"favorites": after["favorites"], "preferences": after["preferences"]} == before
    assert expired.id not in present(db_url, "messages")  # the source is gone; the favourite is not
    integrity(db_url)


def test_a_real_favorite_survives_the_expiry_and_purge_of_its_answer_its_conversation_and_its_own_age(db_url):
    gone, kept = seeded(db_url, seed_history)
    owner_id = read(db_url, "SELECT user_id FROM conversations ORDER BY created_at LIMIT 1")[0][0]
    expired = gone["messages"][0]
    saved_at = NOW - 2 * RETENTION  # kept for more than a year: its own age is no reason to delete it
    engine = create_db_engine(db_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO favorite_recipes (id, user_id, title, content, source_message_id, created_at) "
                "VALUES (?, ?, 'Борщ', 'Борщ: свёкла, капуста, говядина.', ?, ?)",
                (uuid.uuid4().hex, owner_id, expired.id, stored(saved_at)),
            )
    finally:
        engine.dispose()
    before = dump(db_url)["favorite_recipes"]
    assert len(before) == 1

    preview = purge(db_url, dry_run=True)
    real = purge(db_url)

    assert "favorite_recipes" not in preview.deleted and "favorite_recipes" not in real.deleted
    assert real.deleted["messages"] > 0  # the purge did run over the answer the favorite was copied from
    check(db_url, gone, kept)
    assert expired.id not in present(db_url, "messages")  # the source is gone; the favorite is not
    assert dump(db_url)["favorite_recipes"] == before
    integrity(db_url)


def test_the_batch_statements_take_row_locks_only_where_the_database_has_them():
    from sqlalchemy.dialects import postgresql, sqlite

    conditions = retention._Conditions(NOW, CUTOFF)
    statements = [
        retention._delete_batch(retention._messages, conditions.message, 5),
        retention._delete_batch(retention._sessions, conditions.over, 5),
        retention._cancel_batch(conditions, 5),
    ]
    for statement in statements:
        assert "FOR UPDATE" not in str(statement.compile(dialect=sqlite.dialect()))
        on_postgresql = str(statement.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE SKIP LOCKED" in on_postgresql and "LIMIT" in on_postgresql


def test_every_table_of_the_schema_has_a_retention_decision():
    # The purge deletes only from tables it names, so a table added later is safe from it by
    # default; but a table that holds conversation text, or a sign-in artifact, would then
    # silently escape the 365-day promise. This fails when a table appears that nobody has
    # decided about: name it in retention.py, or list it here with the reason it is kept.
    from house_helper.persistence.base import Base

    purged = {category.value for category in Category}
    kept = {
        "users": "the person; deleting one is not retention",
        "allowlist_entries": "who may sign in (also the record that an identity was revoked)",
        "identity_links": "the Telegram or email identity of a user",
        "favorite_recipes": "answers a person chose to keep: copies with no deadline and no link to the history",
        "telegram_links": "the tie between a person and their Telegram account: removed by unlinking and with the user",
    }
    assert set(Base.metadata.tables) == purged | set(kept), (
        "a table was added or removed: decide its retention (RUNBOOK section 14.1)"
    )


def test_the_purge_names_exactly_the_tables_it_may_delete_from():
    tables = {t.name for t in (retention._sessions, retention._oidc_states, retention._challenges,
                               retention._invitations, retention._link_codes, retention._requests,
                               retention._attachments, retention._messages, retention._conversations)}
    assert tables == {c.value for c in Category}
    untouchable = {
        "users", "allowlist_entries", "identity_links", "favorite_recipes", "telegram_links", "alembic_version",
    }
    assert not tables & untouchable


# --- repeated and dry runs --------------------------------------------------------------------


def test_a_second_run_changes_nothing(db_url):
    seeded(db_url, seed_artifacts)
    seeded(db_url, seed_history)
    first = purge(db_url)
    assert first.total > 0
    after_first = dump(db_url)

    second = purge(db_url)

    assert second.total == 0 and second.cancelled_in_flight == 0
    assert dump(db_url) == after_first
    integrity(db_url)


def test_a_dry_run_writes_nothing_not_even_a_file_and_predicts_the_real_run(db_url, db_path):
    seeded(db_url, seed_artifacts)
    seeded(db_url, seed_history)
    seeded(db_url, seed_in_flight)
    rows = dump(db_url)  # (reading opens and closes connections: take the file snapshot after it)
    files = snapshot(db_path.parent)

    preview = purge(db_url, dry_run=True)

    assert preview.dry_run and preview.total > 0
    assert snapshot(db_path.parent) == files  # no byte of any file in the directory changed
    assert dump(db_url) == rows

    real = purge(db_url)

    assert not real.dry_run
    assert real.deleted == preview.deleted
    assert (real.cancelled_in_flight, real.held_back_messages) == (
        preview.cancelled_in_flight,
        preview.held_back_messages,
    )


def test_the_clock_must_be_timezone_aware(db_url):
    with pytest.raises(ValueError):
        purge(db_url, now=NOW.replace(tzinfo=None))


def test_a_clock_in_another_zone_is_the_same_instant(db_url):
    from datetime import timezone

    seeded(db_url, seed_history)
    plus_three = timezone(timedelta(hours=3))
    preview = purge(db_url, now=NOW.astimezone(plus_three), dry_run=True)
    assert preview.now == NOW and preview.cutoff == CUTOFF
    assert preview.deleted == purge(db_url, dry_run=True).deleted


# --- the whole thing against an independent oracle ---------------------------------------------

AGES = [
    timedelta(days=-1), timedelta(0), timedelta(minutes=3), timedelta(days=10), timedelta(days=364, hours=23),
    timedelta(days=365) - MICROSECOND, timedelta(days=365), timedelta(days=365) + MICROSECOND,
    timedelta(days=400), timedelta(days=800),
]
STATUSES = ["completed", "completed", "completed", "failed", "cancelled", "pending", "processing", "committing"]


def random_world(s: Seed, rng: random.Random):
    users = [s.user() for _ in range(2)]
    for _ in range(rng.randint(2, 5)):
        user = rng.choice(users)
        created = NOW - rng.choice(AGES) - timedelta(days=rng.choice([0, 1, 30, 200]))
        updated = NOW - rng.choice(AGES)
        conversation = s.conversation(user, created=min(created, updated), updated=updated)
        for _ in range(rng.randint(0, 5)):
            at = NOW - rng.choice(AGES)
            kind = rng.choice(["exchange", "exchange", "note"])
            if kind == "note":
                s.message(conversation, "system_note", "reset", created=at)
                continue
            status = rng.choice(STATUSES)
            lease = rng.choice([None, at + timedelta(minutes=2), NOW + timedelta(hours=1), NOW, NOW - MICROSECOND])
            s.exchange(
                conversation, created=at, status=status,
                lease=lease if status in ("processing", "committing") else None,
                answered=status == "committing" and rng.random() < 0.5,
            )


def oracle(fetch, now):
    """The rules, written again in Python from the description, and applied to what the
    database holds (`fetch(sql)` returns its rows; SQLite gives timestamps as text, PostgreSQL
    as datetimes). Returns the ids of the requests, messages and conversations that must go."""
    cutoff = now - RETENTION
    requests = fetch(
        "SELECT id, conversation_id, status, lease_expires_at, expires_at, created_at, user_message_id, "
        "assistant_message_id FROM turn_requests"
    )
    messages = fetch("SELECT id, conversation_id, expires_at, created_at FROM messages")
    conversations = fetch("SELECT id, created_at, updated_at FROM conversations")

    def at(value):
        from datetime import datetime, timezone

        if value is None or not isinstance(value, str):
            return value
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)

    def request_deletable(row):
        _, _, status, lease, expires, created, _, _ = row
        expired = at(expires) <= now or at(created) <= cutoff
        terminal = status in ("completed", "failed", "cancelled")
        abandoned = status in ("pending", "processing", "committing") and (lease is None or at(lease) <= now)
        return expired and (terminal or abandoned)

    deletable_requests = {r[0] for r in requests if request_deletable(r)}
    survivors_requests = [r for r in requests if r[0] not in deletable_requests]
    linked = {i for r in survivors_requests for i in (r[6], r[7]) if i is not None}

    def message_deletable(row):
        mid, _, expires, created = row
        return (at(expires) <= now or at(created) <= cutoff) and mid not in linked

    deletable_messages = {m[0] for m in messages if message_deletable(m)}
    surviving_messages = [m for m in messages if m[0] not in deletable_messages]
    busy = {m[1] for m in surviving_messages} | {r[1] for r in survivors_requests}
    deletable_conversations = {
        c[0] for c in conversations if at(c[2]) <= cutoff and at(c[1]) <= cutoff and c[0] not in busy
    }
    return deletable_requests, deletable_messages, deletable_conversations


@pytest.mark.parametrize("seed", range(25))
def test_the_sql_agrees_with_an_independent_oracle_on_random_worlds(db_url, seed):
    rng = random.Random(seed)
    seeded(db_url, lambda s: random_world(s, rng))
    expected_requests, expected_messages, expected_conversations = oracle(lambda sql: read(db_url, sql), NOW)
    before = {t: present(db_url, t) for t in ("turn_requests", "messages", "conversations")}

    preview = purge(db_url, dry_run=True)
    report = purge(db_url, batch_size=3)  # small batches: many commits, same result

    assert before["turn_requests"] - present(db_url, "turn_requests") == expected_requests
    assert before["messages"] - present(db_url, "messages") == expected_messages
    assert before["conversations"] - present(db_url, "conversations") == expected_conversations
    assert report.deleted == preview.deleted
    assert report.deleted["turn_requests"] == len(expected_requests)
    integrity(db_url)
    assert purge(db_url).total == 0  # and nothing else is eligible
