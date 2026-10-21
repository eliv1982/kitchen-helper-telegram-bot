"""Telegram invitations: issuing, withdrawing, verifying and claiming one.

Runs against both the schema created from the models and the migrated one
(tests/persistence_support.py). Nothing here talks to Telegram or to HTTP: the functions
take the verified `sub` and the clock as arguments, which is how the tests move time.

The properties that matter, each pinned below: only a digest of the secret is stored; an
invitation lives two hours, exclusive at the end; it is used once, by exactly one of any
number of concurrent claims; a revoked, expired, used or superseded invitation is never
used again; reissuing revokes the earlier one in the same transaction and the database
refuses a second outstanding one; a claim links the verified identity to the user the
invitation names and to nobody else, and never adopts or un-revokes an identity that is
already on the allowlist; a refusal leaves the invitation valid even for a caller that
commits anyway; every writer starts with a write (so concurrent callers queue on SQLite
instead of failing on a stale snapshot) and takes the user's row before the invitation's
(PostgreSQL's lock order, in the statement it renders).
"""

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from house_helper.persistence import invitations as invitations_module
from house_helper.persistence.allowlist import allow, allowed_user, revoke
from house_helper.persistence.authn import authenticate, complete_login, hash_secret
from house_helper.persistence.database import create_db_engine, create_session_factory
from house_helper.persistence.invitations import (
    INVITATION_LIFETIME,
    ClaimedInvitation,
    IdentityRefused,
    InvitationRefusal,
    InvitationRefused,
    InvitationState,
    LiveInvitation,
    claim_invitation,
    format_invitation_code,
    invitation_state,
    is_invitation_id,
    is_invitation_token,
    issue_invitation,
    parse_invitation_code,
    revoke_invitations,
    verify_invitation,
)
from house_helper.persistence.models import AllowlistEntry, IdentityInvitation, IdentityLink, User
from house_helper.persistence.models import Session as LoginSession
from tests.persistence_support import (  # noqa: F401  (fixtures)
    count,
    digest,
    engine,
    factory,
    new_user,
    raw_insert,
    rejected,
    session,
    stored,
    utc,
)
from tests.test_persistence_authn import row_text, statements
from tests.test_persistence_schema import TABLES

NOW = utc(2026, 10, 19, 12)
LATER = NOW + timedelta(minutes=5)
SUB = "1234123412341234123"
OTHER_SUB = "9876598765987659876"
TWO_HOURS = timedelta(hours=2)
ROUNDS = 3


@pytest.fixture
def user(session):
    return new_user(session, display_name="Elena")


@pytest.fixture
def other(session):
    return new_user(session, display_name="Mother")


def invitation_row(owner, **overrides) -> dict:
    """A row for plain SQL: past the ORM, so that only the database's own rules stand in the way."""
    return {
        "id": uuid.uuid4().hex,
        "user_id": owner.id.hex,
        "provider": "telegram",
        "token_hash": digest(uuid.uuid4().hex),
        "created_at": stored(NOW),
        "expires_at": stored(NOW + TWO_HOURS),
        **overrides,
    }


def columns_of(session, issued):
    """The stored columns of an invitation (selected as columns, so never a stale object)."""
    return session.execute(
        sa.select(
            IdentityInvitation.created_at,
            IdentityInvitation.expires_at,
            IdentityInvitation.consumed_at,
            IdentityInvitation.revoked_at,
        ).where(IdentityInvitation.id == issued.invitation_id)
    ).one()


def claim(session, issued, subject=SUB, now=LATER):
    return claim_invitation(session, str(issued.invitation_id), subject, now=now)


def verify(session, issued, now=LATER):
    return verify_invitation(session, str(issued.invitation_id), issued.token, now=now)


def entries(session, provider="telegram"):
    return session.scalars(
        sa.select(AllowlistEntry).where(AllowlistEntry.provider == provider).order_by(AllowlistEntry.subject)
    ).all()


# --- the schema's own rules (plain SQL, no ORM) -----------------------------------------------------------


def test_a_well_formed_row_is_accepted(session, user):
    raw_insert(session, "identity_invitations", **invitation_row(user))

    assert count(session, IdentityInvitation) == 1


def test_the_provider_must_be_telegram(session, user):
    with rejected(session, "ck_identity_invitations_provider_is_telegram"):
        raw_insert(session, "identity_invitations", **invitation_row(user, provider="email"))


@pytest.mark.parametrize("length", [0, 63, 65, 128])
def test_the_digest_must_be_64_characters(session, user, length):
    with rejected(session, "ck_identity_invitations_token_hash_length"):
        raw_insert(session, "identity_invitations", **invitation_row(user, token_hash="a" * length))


@pytest.mark.parametrize("offset", [timedelta(0), -timedelta(seconds=1)])
def test_it_must_expire_after_it_was_created(session, user, offset):
    with rejected(session, "ck_identity_invitations_expires_after_created"):
        raw_insert(session, "identity_invitations", **invitation_row(user, expires_at=stored(NOW + offset)))


def test_an_invitation_cannot_be_both_used_and_withdrawn(session, user):
    with rejected(session, "ck_identity_invitations_not_consumed_and_revoked"):
        raw_insert(
            session,
            "identity_invitations",
            **invitation_row(user, consumed_at=stored(LATER), revoked_at=stored(LATER)),
        )


def test_a_withdrawn_invitation_cannot_be_used_up_by_any_writer(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    revoke_invitations(session, user.id, now=LATER)

    with rejected(session, "ck_identity_invitations_not_consumed_and_revoked"):
        session.execute(
            sa.text("UPDATE identity_invitations SET consumed_at = :at WHERE id = :id"),
            {"at": stored(LATER), "id": issued.invitation_id.hex},
        )


def test_a_used_invitation_cannot_be_withdrawn_by_any_writer(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    claim(session, issued)

    with rejected(session, "ck_identity_invitations_not_consumed_and_revoked"):
        session.execute(
            sa.text("UPDATE identity_invitations SET revoked_at = :at WHERE id = :id"),
            {"at": stored(LATER), "id": issued.invitation_id.hex},
        )


def test_a_digest_is_unique(session, user, other):
    raw_insert(session, "identity_invitations", **invitation_row(user, token_hash=digest("same")))

    with rejected(session, "identity_invitations.token_hash"):
        raw_insert(session, "identity_invitations", **invitation_row(other, token_hash=digest("same")))


def test_a_user_has_at_most_one_outstanding_invitation(session, user, other):
    raw_insert(session, "identity_invitations", **invitation_row(user))

    with rejected(session, "identity_invitations.user_id"):
        raw_insert(session, "identity_invitations", **invitation_row(user))
    # ... which is about "outstanding" only: withdrawn and used ones are history
    raw_insert(session, "identity_invitations", **invitation_row(user, revoked_at=stored(LATER)))
    raw_insert(session, "identity_invitations", **invitation_row(user, revoked_at=stored(LATER)))
    raw_insert(session, "identity_invitations", **invitation_row(user, consumed_at=stored(LATER)))
    # ... and per user: another user has their own
    raw_insert(session, "identity_invitations", **invitation_row(other))
    assert count(session, IdentityInvitation) == 5


def test_the_owner_must_exist(session):
    with rejected(session, "FOREIGN KEY constraint failed"):
        raw_insert(
            session, "identity_invitations", **{**invitation_row(User(id=uuid.uuid4())), "user_id": uuid.uuid4().hex}
        )


def test_deleting_the_user_deletes_their_invitations_and_only_theirs(session, user, other):
    issue_invitation(session, user.id, now=NOW)
    kept = issue_invitation(session, other.id, now=NOW)

    session.execute(sa.delete(User).where(User.id == user.id))

    remaining = session.scalars(sa.select(IdentityInvitation.id)).all()
    assert remaining == [kept.invitation_id]


@pytest.mark.parametrize(
    "attribute", ["user_id", "provider", "token_hash", "created_at", "expires_at"]
)
def test_the_orm_refuses_to_rewrite_what_an_invitation_is_bound_to(factory, attribute):
    values = {
        "user_id": uuid.uuid4(),
        "provider": "email",
        "token_hash": digest("another"),
        "created_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=30),
    }
    with factory() as db:
        issued = issue_invitation(db, new_user(db).id, now=NOW)
        db.commit()
        row = db.get(IdentityInvitation, issued.invitation_id)
        setattr(row, attribute, values[attribute])

        with pytest.raises(ValueError, match="cannot change once stored"):
            db.flush()


def test_the_orm_allows_the_two_columns_that_are_meant_to_be_set(factory):
    with factory() as db:
        issued = issue_invitation(db, new_user(db).id, now=NOW)
        db.commit()
        db.get(IdentityInvitation, issued.invitation_id).revoked_at = LATER
        db.flush()  # does not raise


# --- the pieces of an invitation --------------------------------------------------------------------------


def test_the_lifetime_is_two_hours():
    assert INVITATION_LIFETIME == TWO_HOURS


def test_the_code_is_the_id_a_dot_and_the_secret_and_parses_back():
    invitation_id, token = uuid.uuid4(), "A" * 43

    code = format_invitation_code(invitation_id, token)

    assert code == f"{invitation_id}.{token}"
    assert parse_invitation_code(code) == (str(invitation_id), token)


@pytest.mark.parametrize(
    "text",
    [
        None,
        42,
        b"x",
        "",
        ".",
        f"{uuid.uuid4()}",
        f"{uuid.uuid4()}.",
        f"{uuid.uuid4()}.{'A' * 42}",
        f"{uuid.uuid4()}.{'A' * 44}",
        f"{uuid.uuid4()}.{'A' * 42}!",
        f"{uuid.uuid4()}.{'A' * 43}.extra",
        f" {uuid.uuid4()}.{'A' * 43}",
        f"{uuid.uuid4()}.{'A' * 43}\n",
        f"{str(uuid.uuid4()).upper()}.{'A' * 43}",
        f"{uuid.uuid4().hex}.{'A' * 43}",
        f"{{{uuid.uuid4()}}}.{'A' * 43}",
        f"{uuid.uuid4()}:{'A' * 43}",
    ],
)
def test_anything_that_is_not_a_code_of_ours_does_not_parse(text):
    assert parse_invitation_code(text) is None


def test_the_shapes_of_an_id_and_a_secret():
    assert is_invitation_id(str(uuid.uuid4()))
    assert not is_invitation_id(uuid.uuid4())  # a UUID object is not what a client presents
    assert not is_invitation_id(str(uuid.uuid4()).upper())
    spelled = str(uuid.UUID("abcdef01-abcd-abcd-abcd-abcdefabcdef"))
    for group in range(5):  # one group in capitals is as foreign as all of them
        parts = spelled.split("-")
        parts[group] = parts[group].upper()
        assert not is_invitation_id("-".join(parts)), group
        assert parse_invitation_code("-".join(parts) + "." + "A" * 43) is None
    assert is_invitation_token("a" * 43) and is_invitation_token("-_" * 21 + "x")
    assert not is_invitation_token("a" * 42) and not is_invitation_token("a" * 43 + "\n")
    assert not is_invitation_token(None)


@pytest.mark.parametrize(
    "consumed, revoked, expires, expected",
    [
        (None, None, NOW + timedelta(seconds=1), InvitationState.ACTIVE),
        (None, None, NOW, InvitationState.EXPIRED),  # exclusive: at expires_at it is over
        (None, None, NOW - timedelta(days=1), InvitationState.EXPIRED),
        (NOW, None, NOW + timedelta(days=1), InvitationState.USED),
        (NOW, None, NOW - timedelta(days=1), InvitationState.USED),  # used before it expired
        (None, NOW, NOW + timedelta(days=1), InvitationState.REVOKED),
        (None, NOW, NOW - timedelta(days=1), InvitationState.REVOKED),  # withdrawn, then lapsed
    ],
)
def test_the_state_of_an_invitation(consumed, revoked, expires, expected):
    state = invitation_state(expires_at=expires, consumed_at=consumed, revoked_at=revoked, now=NOW)

    assert state is expected


# --- issuing ----------------------------------------------------------------------------------------------


def test_issuing_stores_only_the_digest_and_a_two_hour_lifetime(session, user):
    issued = issue_invitation(session, user.id, now=NOW)

    assert issued.user_id == user.id and issued.superseded == 0
    assert issued.expires_at == NOW + TWO_HOURS
    row = session.get(IdentityInvitation, issued.invitation_id)
    assert row.token_hash == hash_secret(issued.token)
    assert (row.user_id, row.provider, row.created_at, row.expires_at) == (
        user.id,
        "telegram",
        NOW,
        NOW + TWO_HOURS,
    )
    assert row.consumed_at is None and row.revoked_at is None
    assert issued.code == f"{issued.invitation_id}.{issued.token}"
    assert is_invitation_token(issued.token)


def test_the_secret_is_256_random_bits_from_the_session_token_generator(session, user, monkeypatch):
    asked = []
    real = invitations_module.new_secret
    monkeypatch.setattr(invitations_module, "new_secret", lambda nbytes: asked.append(nbytes) or real(nbytes))

    issue_invitation(session, user.id, now=NOW)

    assert asked == [32]


def test_two_invitations_never_share_a_secret_or_an_id(session):
    issued = [issue_invitation(session, new_user(session).id, now=NOW) for _ in range(40)]

    assert len({item.token for item in issued}) == 40
    assert len({item.invitation_id for item in issued}) == 40


def test_the_secret_is_in_no_column_of_any_table_and_not_in_the_representation(session, user, other):
    first = issue_invitation(session, user.id, now=NOW)
    second = issue_invitation(session, other.id, now=NOW)
    claim(session, first)
    complete_login(session, "telegram", SUB, now=LATER)

    for table in TABLES:
        text = row_text(session, table)
        for secret in (first.token, second.token, first.code, second.code):
            assert secret not in text, table
    assert hash_secret(first.token) in row_text(session, "identity_invitations")  # the digest is
    for text in (repr(first), str(first), repr(claim_invitation)):
        assert first.token not in text


def test_the_secret_is_in_no_log_record_even_with_statement_logging_on(caplog, factory):
    caplog.set_level(logging.DEBUG, logger="sqlalchemy.engine")
    caplog.set_level(logging.DEBUG)
    with factory() as db, db.begin():
        owner = new_user(db, display_name="Elena")
        issued = issue_invitation(db, owner.id, now=NOW)
        assert verify(db, issued) is not None
        claim(db, issued)
        revoke_invitations(db, owner.id, now=LATER)

    assert "INSERT INTO identity_invitations" in caplog.text  # the logging really was on
    assert hash_secret(issued.token) in caplog.text  # ... and it shows the digest, which is stored
    assert issued.token not in caplog.text and issued.code not in caplog.text


def test_reissuing_revokes_the_earlier_outstanding_invitation_in_the_same_transaction(session, user):
    first = issue_invitation(session, user.id, now=NOW)
    second = issue_invitation(session, user.id, now=LATER)

    assert second.superseded == 1 and first.superseded == 0
    assert columns_of(session, first).revoked_at == LATER
    assert columns_of(session, second).revoked_at is None
    assert verify(session, first) is None and verify(session, second) is not None
    assert claim(session, first) is None  # the old secret is dead
    assert claim(session, second) is not None


def test_reissuing_leaves_the_other_users_invitation_alone(session, user, other):
    theirs = issue_invitation(session, other.id, now=NOW)
    issue_invitation(session, user.id, now=NOW)
    issue_invitation(session, user.id, now=LATER)

    assert columns_of(session, theirs).revoked_at is None
    assert verify(session, theirs) is not None


def test_reissuing_supersedes_an_expired_invitation_that_was_never_used(session, user):
    first = issue_invitation(session, user.id, now=NOW)
    later = NOW + timedelta(hours=3)

    second = issue_invitation(session, user.id, now=later)

    assert second.superseded == 1
    assert columns_of(session, first).revoked_at == later
    assert count(session, IdentityInvitation) == 2


def test_many_reissues_leave_one_outstanding_and_every_other_one_revoked(session, user):
    issued = [issue_invitation(session, user.id, now=NOW + timedelta(minutes=n)) for n in range(6)]

    outstanding = session.scalars(
        sa.select(IdentityInvitation.id).where(
            IdentityInvitation.consumed_at.is_(None), IdentityInvitation.revoked_at.is_(None)
        )
    ).all()
    assert outstanding == [issued[-1].invitation_id]
    assert [item.superseded for item in issued] == [0, 1, 1, 1, 1, 1]


def test_a_user_who_has_no_invitation_yet_gets_the_first_one(session, user):
    assert issue_invitation(session, user.id, now=NOW).superseded == 0


def test_a_user_with_only_an_email_identity_can_be_invited(session, user):
    allow(session, user, "email", "elena@example.com")

    assert issue_invitation(session, user.id, now=NOW).superseded == 0
    assert entries(session, "email")[0].revoked_at is None  # the email entry was not touched


@pytest.mark.parametrize("scenario", ["unknown", "disabled", "telegram", "telegram_revoked"])
def test_issuing_refuses_where_an_invitation_would_be_wrong_and_writes_nothing(session, user, scenario):
    wanted = {
        "unknown": InvitationRefusal.UNKNOWN_USER,
        "disabled": InvitationRefusal.USER_DISABLED,
        "telegram": InvitationRefusal.TELEGRAM_PROVISIONED,
        "telegram_revoked": InvitationRefusal.TELEGRAM_PROVISIONED,  # taken away on purpose: not a way around
    }[scenario]
    target = uuid.uuid4()
    if scenario == "disabled":
        user.disabled_at = NOW
    elif scenario in ("telegram", "telegram_revoked"):
        allow(session, user, "telegram", SUB)
        if scenario == "telegram_revoked":
            revoke(session, "telegram", SUB, now=NOW)
    if scenario != "unknown":
        target = user.id
    session.flush()

    with pytest.raises(InvitationRefused) as refused:
        issue_invitation(session, target, now=NOW)

    assert refused.value.reason is wanted
    assert count(session, IdentityInvitation) == 0


def test_a_refused_reissue_leaves_the_earlier_invitation_as_it_was(session, user):
    first = issue_invitation(session, user.id, now=NOW)
    user.disabled_at = NOW
    session.flush()

    with pytest.raises(InvitationRefused):
        issue_invitation(session, user.id, now=LATER)

    assert columns_of(session, first).revoked_at is None


# --- withdrawing ------------------------------------------------------------------------------------------


def test_revoking_withdraws_the_outstanding_invitation_for_good(session, user):
    issued = issue_invitation(session, user.id, now=NOW)

    assert revoke_invitations(session, user.id, now=LATER) == 1

    assert columns_of(session, issued).revoked_at == LATER
    assert verify(session, issued) is None and claim(session, issued) is None
    assert entries(session) == []


def test_revoking_twice_changes_nothing_and_keeps_the_first_time(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    revoke_invitations(session, user.id, now=LATER)

    assert revoke_invitations(session, user.id, now=LATER + timedelta(hours=1)) == 0

    assert columns_of(session, issued).revoked_at == LATER


def test_revoking_touches_only_that_users_invitation(session, user, other):
    issue_invitation(session, user.id, now=NOW)
    theirs = issue_invitation(session, other.id, now=NOW)

    revoke_invitations(session, user.id, now=LATER)

    assert columns_of(session, theirs).revoked_at is None


def test_revoking_does_not_touch_a_used_invitation(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    claim(session, issued)

    assert revoke_invitations(session, user.id, now=LATER) == 0

    assert columns_of(session, issued).revoked_at is None
    assert columns_of(session, issued).consumed_at == LATER


@pytest.mark.parametrize(
    "offset, revoked",
    [
        (timedelta(minutes=5), 1),
        (TWO_HOURS - timedelta(microseconds=1), 1),  # still valid for one more instant
        (TWO_HOURS, 0),  # exclusive, as verify and claim: at expires_at it is over
        (TWO_HOURS + timedelta(microseconds=1), 0),
        (timedelta(hours=30), 0),
    ],
    ids=["before", "last-instant", "boundary", "just-after", "long-after"],
)
def test_revoking_withdraws_an_invitation_only_while_it_is_valid(session, user, offset, revoked):
    issued = issue_invitation(session, user.id, now=NOW)

    assert revoke_invitations(session, user.id, now=NOW + offset) == revoked

    row = columns_of(session, issued)
    assert row.revoked_at == (NOW + offset if revoked else None)  # an expired one is not marked
    assert row.consumed_at is None
    assert verify(session, issued, now=NOW + offset) is None  # unusable either way, withdrawn or over


def test_revoking_leaves_an_expired_invitation_alone_and_reissuing_still_supersedes_it(session, user):
    # Two different questions with two different answers: an operator who withdraws an invitation
    # withdraws one that can still be used; a reissue clears the way for the new one by taking
    # out of the way whatever is outstanding (the partial unique index allows one row), expired or not.
    old = issue_invitation(session, user.id, now=NOW)
    later = NOW + timedelta(hours=3)

    assert revoke_invitations(session, user.id, now=later) == 0
    assert columns_of(session, old).revoked_at is None

    fresh = issue_invitation(session, user.id, now=later)

    assert fresh.superseded == 1
    assert columns_of(session, old).revoked_at == later
    assert columns_of(session, fresh).revoked_at is None


def test_revoking_does_not_touch_an_invitation_that_is_already_withdrawn(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    revoke_invitations(session, user.id, now=LATER)

    assert revoke_invitations(session, user.id, now=LATER + timedelta(minutes=1)) == 0

    assert columns_of(session, issued).revoked_at == LATER


def test_revoking_does_not_touch_a_used_invitation_even_after_it_expired(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    claim(session, issued)

    assert revoke_invitations(session, user.id, now=NOW + timedelta(hours=3)) == 0

    assert columns_of(session, issued).revoked_at is None


def test_revoking_for_a_user_nobody_has_revokes_nothing(session):
    assert revoke_invitations(session, uuid.uuid4(), now=NOW) == 0


def test_a_revoked_invitation_is_never_used_again_not_even_by_reissuing_and_not_by_time(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    revoke_invitations(session, user.id, now=LATER)
    fresh = issue_invitation(session, user.id, now=LATER)  # a NEW invitation; the old row stays revoked

    assert claim(session, issued) is None
    assert columns_of(session, issued).revoked_at == LATER
    assert claim(session, fresh) is not None


# --- verifying --------------------------------------------------------------------------------------------


def test_a_live_invitation_verifies_and_nothing_is_used_up(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    before = columns_of(session, issued)

    live = verify(session, issued)

    assert live == LiveInvitation(invitation_id=issued.invitation_id, expires_at=NOW + TWO_HOURS)
    assert columns_of(session, issued) == before
    assert verify(session, issued) is not None  # as often as the flow is started
    assert entries(session) == []


def test_verifying_is_one_statement_and_only_reads(engine, factory):
    with factory() as db, db.begin():
        issued = issue_invitation(db, new_user(db).id, now=NOW)
    with factory() as db, db.begin():
        with statements(engine) as seen:
            verify(db, issued)

    assert len(seen) == 1 and seen[0].lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize(
    "offset, live",
    [
        (timedelta(0), True),
        (timedelta(hours=1, minutes=59, seconds=59, microseconds=999999), True),
        (TWO_HOURS, False),  # exclusive: at expires_at it is over
        (TWO_HOURS + timedelta(microseconds=1), False),
        (timedelta(days=3), False),
    ],
)
def test_an_invitation_ends_after_two_hours_exclusive(session, user, offset, live):
    issued = issue_invitation(session, user.id, now=NOW)

    assert (verify(session, issued, now=NOW + offset) is not None) is live
    assert (claim(session, issued, now=NOW + offset) is not None) is live
    assert len(entries(session)) == (1 if live else 0)


def test_a_wrong_secret_does_not_verify(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    wrong = "A" * 43

    assert verify_invitation(session, str(issued.invitation_id), wrong, now=LATER) is None


def test_the_secret_of_another_invitation_does_not_verify_this_one(session, user, other):
    mine = issue_invitation(session, user.id, now=NOW)
    theirs = issue_invitation(session, other.id, now=NOW)

    assert verify_invitation(session, str(mine.invitation_id), theirs.token, now=LATER) is None
    assert verify_invitation(session, str(theirs.invitation_id), mine.token, now=LATER) is None
    assert verify_invitation(session, str(mine.invitation_id), mine.token, now=LATER) is not None


@pytest.mark.parametrize(
    "invitation_id",
    [None, 7, "", "not-a-uuid", str(uuid.uuid4()).upper(), uuid.uuid4().hex, str(uuid.uuid4()), uuid.uuid4()],
)
def test_a_malformed_or_unknown_id_does_not_verify(session, user, invitation_id):
    issued = issue_invitation(session, user.id, now=NOW)

    assert verify_invitation(session, invitation_id, issued.token, now=LATER) is None


@pytest.mark.parametrize("token", [None, 7, "", "short", "A" * 42, "A" * 44, "A" * 43 + "\n", "é" * 43])
def test_a_malformed_secret_does_not_verify(session, user, token):
    issued = issue_invitation(session, user.id, now=NOW)

    assert verify_invitation(session, str(issued.invitation_id), token, now=LATER) is None


# --- every reason an invitation cannot be used, for verify and for claim ----------------------------------

UNUSABLE = ["expired", "used", "revoked", "reissued", "disabled", "linked", "linked_then_revoked"]


def make_unusable(session, user, scenario):
    """An invitation that cannot be used any more, and the time at which it is looked at."""
    issued = issue_invitation(session, user.id, now=NOW)
    when = LATER
    if scenario == "expired":
        when = NOW + TWO_HOURS
    elif scenario == "used":
        claim(session, issued, subject="111")
    elif scenario == "revoked":
        revoke_invitations(session, user.id, now=LATER)
    elif scenario == "reissued":
        issue_invitation(session, user.id, now=LATER)
    elif scenario == "disabled":
        user.disabled_at = LATER
    elif scenario in ("linked", "linked_then_revoked"):
        allow(session, user, "telegram", "111")  # an operator linked the user after the invitation
        if scenario == "linked_then_revoked":
            revoke(session, "telegram", "111", now=LATER)
    session.flush()
    return issued, when


@pytest.mark.parametrize("scenario", UNUSABLE)
def test_an_unusable_invitation_does_not_verify(session, user, scenario):
    issued, when = make_unusable(session, user, scenario)

    assert verify(session, issued, now=when) is None


@pytest.mark.parametrize("scenario", UNUSABLE)
def test_an_unusable_invitation_cannot_be_claimed_and_nothing_is_written(session, user, scenario):
    issued, when = make_unusable(session, user, scenario)
    entries_before = [(e.subject, e.user_id, e.revoked_at) for e in entries(session)]
    before = columns_of(session, issued)

    assert claim(session, issued, subject=OTHER_SUB, now=when) is None

    assert [(e.subject, e.user_id, e.revoked_at) for e in entries(session)] == entries_before
    assert columns_of(session, issued) == before  # not used up by a refused claim
    assert count(session, IdentityLink) == 0 and count(session, LoginSession) == 0


def test_an_invitation_that_was_used_up_is_not_live_even_if_its_user_has_no_telegram_entry(session, user):
    # In every real flow a used invitation goes with a linked user, which is also why it is
    # refused; here the two reasons are pulled apart (a writer that marks it used and links nobody).
    issued = issue_invitation(session, user.id, now=NOW)
    session.execute(
        sa.text("UPDATE identity_invitations SET consumed_at = :at WHERE id = :id"),
        {"at": stored(LATER), "id": issued.invitation_id.hex},
    )

    assert verify(session, issued) is None
    assert claim(session, issued) is None
    assert entries(session) == []


# --- claiming ---------------------------------------------------------------------------------------------


def test_a_claim_puts_the_verified_identity_on_the_allowlist_for_the_invitations_user(session, user):
    issued = issue_invitation(session, user.id, now=NOW)

    claimed = claim(session, issued)

    assert claimed == ClaimedInvitation(invitation_id=issued.invitation_id, user_id=user.id, subject=SUB)
    (entry,) = entries(session)
    assert (entry.user_id, entry.provider, entry.subject, entry.revoked_at) == (user.id, "telegram", SUB, None)
    assert columns_of(session, issued).consumed_at == LATER
    assert allowed_user(session, "telegram", SUB).id == user.id
    # a claim links nothing and signs nobody in: that is complete_login's job
    assert count(session, IdentityLink) == 0 and count(session, LoginSession) == 0


def test_the_subject_is_stored_as_the_allowlist_stores_it(session, user):
    issued = issue_invitation(session, user.id, now=NOW)

    claimed = claim(session, issued, subject="  not-trimmed  ".strip())

    assert claimed.subject == "not-trimmed"
    assert entries(session)[0].subject == "not-trimmed"


def test_claiming_then_completing_the_login_signs_in_the_invitations_user_and_nobody_else(session, user, other):
    mine = issue_invitation(session, user.id, now=NOW)
    theirs = issue_invitation(session, other.id, now=NOW)

    claimed = claim(session, theirs, subject=OTHER_SUB)
    signed_in = complete_login(session, "telegram", claimed.subject, now=LATER)

    assert signed_in.user_id == other.id == claimed.user_id
    live = authenticate(session, signed_in.token, now=LATER)
    assert live is not None and live.user_id == other.id
    link = session.scalars(sa.select(IdentityLink)).one()
    assert (link.user_id, link.provider, link.subject) == (other.id, "telegram", OTHER_SUB)
    assert session.get(LoginSession, signed_in.session_id).identity_link_id == link.id  # provenance
    # the first user is untouched: no entry, no link, and their invitation is still theirs to use
    assert [e.user_id for e in entries(session)] == [other.id]
    assert columns_of(session, mine).consumed_at is None
    assert verify(session, mine) is not None


def test_an_invitation_is_used_once(session, user):
    issued = issue_invitation(session, user.id, now=NOW)

    assert claim(session, issued, subject=SUB) is not None
    assert claim(session, issued, subject=SUB) is None  # the same identity again
    assert claim(session, issued, subject=OTHER_SUB) is None  # another one

    assert [e.subject for e in entries(session)] == [SUB]
    assert verify(session, issued) is None


def test_two_users_are_invited_and_claimed_independently(session, user, other):
    mine = issue_invitation(session, user.id, now=NOW)
    theirs = issue_invitation(session, other.id, now=NOW)

    first = claim(session, mine, subject=SUB)
    second = claim(session, theirs, subject=OTHER_SUB)

    assert first.user_id == user.id and second.user_id == other.id and user.id != other.id
    assert {(e.user_id, e.subject) for e in entries(session)} == {(user.id, SUB), (other.id, OTHER_SUB)}
    assert allowed_user(session, "telegram", SUB).id == user.id
    assert allowed_user(session, "telegram", OTHER_SUB).id == other.id


@pytest.mark.parametrize("bad", ["", "has@at", "ключ", "x" * 256, "with space", "tab\t", 12345, None, b"123"])
def test_an_identity_that_cannot_be_stored_is_refused_and_the_invitation_stays_valid(session, user, bad):
    issued = issue_invitation(session, user.id, now=NOW)

    with pytest.raises(IdentityRefused):
        claim(session, issued, subject=bad)

    assert columns_of(session, issued).consumed_at is None
    assert entries(session) == []
    assert verify(session, issued) is not None


def test_an_identity_that_belongs_to_another_user_is_refused_and_not_taken(session, user, other):
    allow(session, other, "telegram", SUB)  # the owner's own Telegram account, say
    issued = issue_invitation(session, user.id, now=NOW)  # ... on the mother's invitation

    with pytest.raises(IdentityRefused):
        claim(session, issued, subject=SUB)

    assert [(e.user_id, e.subject) for e in entries(session)] == [(other.id, SUB)]  # still only theirs
    assert columns_of(session, issued).consumed_at is None
    assert verify(session, issued) is not None  # still valid for the right person


def test_a_revoked_identity_of_another_user_is_not_adopted_and_stays_revoked(session, user, other):
    allow(session, other, "telegram", SUB)
    revoke(session, "telegram", SUB, now=NOW)
    issued = issue_invitation(session, user.id, now=NOW)

    with pytest.raises(IdentityRefused):
        claim(session, issued, subject=SUB)

    (entry,) = entries(session)
    assert (entry.user_id, entry.revoked_at) == (other.id, NOW)  # not moved, not lifted
    assert allowed_user(session, "telegram", SUB) is None


def test_a_refused_identity_leaves_the_invitation_valid_even_for_a_caller_that_commits_anyway(factory):
    with factory() as db, db.begin():
        owner, stranger = new_user(db, display_name="Mother"), new_user(db, display_name="Elena")
        allow(db, stranger, "telegram", SUB)
        issued = issue_invitation(db, owner.id, now=NOW)

    with factory() as db:
        with pytest.raises(IdentityRefused):
            claim(db, issued, subject=SUB)
        db.commit()  # a caller that swallowed the exception and committed

    with factory() as db:
        assert verify(db, issued) is not None
        assert columns_of(db, issued).consumed_at is None
        assert [e.user_id for e in entries(db)] == [stranger.id]


def test_the_wrong_user_cannot_be_reached_through_an_invitation_id(session, user, other):
    mine = issue_invitation(session, user.id, now=NOW)
    issue_invitation(session, other.id, now=NOW)

    claimed = claim(session, mine, subject=SUB)

    assert claimed.user_id == user.id
    assert [e.user_id for e in entries(session)] == [user.id]  # the id of one cannot link the other


def test_nothing_is_matched_by_a_name_an_email_address_or_a_numeric_id(session):
    first = new_user(session, display_name="Elena")
    second = new_user(session, display_name="Elena")  # the same name: the persistence layer allows it
    allow(session, first, "email", "elena@example.com")
    issued = issue_invitation(session, second.id, now=NOW)

    claimed = claim(session, issued, subject="elena")  # neither the name nor the address' local part

    assert claimed.user_id == second.id
    assert [e.user_id for e in entries(session)] == [second.id]
    assert entries(session, "email")[0].user_id == first.id  # and the email identity did not move


@pytest.mark.parametrize(
    "invitation_id",
    [None, 7, "", "not-a-uuid", str(uuid.uuid4()).upper(), uuid.uuid4().hex, uuid.uuid4(), str(uuid.uuid4())],
)
def test_a_malformed_or_unknown_invitation_id_is_refused_without_writing(session, invitation_id):
    assert claim_invitation(session, invitation_id, SUB, now=LATER) is None

    assert entries(session) == [] and count(session, IdentityInvitation) == 0


def test_a_claim_that_fails_afterwards_rolls_back_the_use_and_the_entry(factory):
    with factory() as db, db.begin():
        owner = new_user(db, display_name="Elena")
        issued = issue_invitation(db, owner.id, now=NOW)

    with pytest.raises(RuntimeError):
        with factory() as db, db.begin():
            claim(db, issued)
            complete_login(db, "telegram", SUB, now=LATER)
            raise RuntimeError("the caller failed after the claim")

    with factory() as db:
        assert columns_of(db, issued).consumed_at is None
        assert verify(db, issued) is not None
        assert entries(db) == [] and count(db, IdentityLink) == 0 and count(db, LoginSession) == 0


def test_a_denied_login_after_a_claim_rolls_the_claim_back(factory):
    # complete_login refuses an identity that is not allowed; here, a user disabled between the two
    with factory() as db, db.begin():
        owner = new_user(db, display_name="Elena")
        issued = issue_invitation(db, owner.id, now=NOW)
    with pytest.raises(Exception) as denied:
        with factory() as db, db.begin():
            claimed = claim(db, issued)
            db.execute(sa.update(User).where(User.id == owner.id).values(disabled_at=LATER))
            complete_login(db, "telegram", claimed.subject, now=LATER)

    assert denied.type.__name__ == "LoginDenied"
    with factory() as db:
        assert columns_of(db, issued).consumed_at is None and entries(db) == []


@pytest.mark.parametrize(
    "error",
    [
        sa.exc.IntegrityError("INSERT", {"subject": SUB}, Exception("UNIQUE constraint failed")),
        ValueError("this identity is already on the allowlist for another user"),
    ],
    ids=["unique_key_lost", "another_user_got_there_first"],
)
def test_an_allowlist_insert_that_loses_a_race_is_a_refusal_and_not_a_crash(session, user, monkeypatch, error):
    issued = issue_invitation(session, user.id, now=NOW)

    def lose(*args, **kwargs):
        raise error

    monkeypatch.setattr(invitations_module, "allow", lose)

    with pytest.raises(IdentityRefused) as refused:
        claim(session, issued, subject=SUB)

    assert refused.value.__suppress_context__  # the driver's error, which quotes the identity, is not chained
    assert SUB not in str(refused.value)


# --- the first statement ----------------------------------------------------------------------------------


def first_statement(engine, factory, action):
    with factory() as db, db.begin():
        owner = new_user(db, display_name="Elena")
        issued = issue_invitation(db, owner.id, now=NOW)
    with factory() as db, db.begin():
        with statements(engine) as seen:
            action(db, owner, issued)
    return seen


def test_issuing_and_claiming_begin_with_a_write_that_changes_nothing(engine, factory):
    issuing = first_statement(engine, factory, lambda db, owner, issued: issue_invitation(db, owner.id, now=LATER))
    claiming = first_statement(engine, factory, lambda db, owner, issued: claim(db, issued))

    for seen in (issuing, claiming):
        assert seen[0].lstrip().upper().startswith("UPDATE IDENTITY_INVITATIONS")
        assert "WHERE 0 = 1" in seen[0]


def test_revoking_begins_with_its_own_conditional_write(engine, factory):
    seen = first_statement(engine, factory, lambda db, owner, issued: revoke_invitations(db, owner.id, now=LATER))

    assert seen[0].lstrip().upper().startswith("UPDATE IDENTITY_INVITATIONS")
    assert "revoked_at IS NULL" in seen[0] and "consumed_at IS NULL" in seen[0]
    assert "expires_at >" in seen[0]  # only what can still be used: the end of the two hours is exclusive
    assert len(seen) == 1


class RecordingSession:
    """Just enough of a Session to see the statement that locks the user."""

    def __init__(self):
        self.statements = []

    def scalar(self, statement):
        self.statements.append(statement)


def test_on_postgresql_the_user_is_locked_for_no_key_update_so_that_references_to_it_do_not_wait():
    recording = RecordingSession()

    invitations_module._lock_user(recording, uuid.uuid4())

    text = str(recording.statements[0].compile(dialect=postgresql.dialect()))
    assert text.rstrip().endswith("FOR NO KEY UPDATE")  # not FOR UPDATE: the FK inserts take KEY SHARE
    assert "users" in text.split("FROM", 1)[1].split("WHERE")[0]


# --- real concurrency (SQLite file, one engine and one transaction per thread) ----------------------------


def race(engine, jobs):
    """Each job(session) runs in a transaction of its own, on an engine of its own (as separate
    processes would have), all released at the same moment. Returns ("ok", value) or
    ("error", exception) per job, in order."""
    url = engine.url.render_as_string(hide_password=False)
    barrier = threading.Barrier(len(jobs))

    def run(job):
        own = create_db_engine(url)
        try:
            with create_session_factory(own)() as db:
                barrier.wait(timeout=30)
                try:
                    with db.begin():
                        return ("ok", job(db))
                except Exception as error:  # reported to the test, which says what it expected
                    return ("error", error)
        finally:
            own.dispose()

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        return [future.result(timeout=120) for future in futures]


@pytest.fixture
def two_users(factory):
    with factory() as db, db.begin():
        users = [new_user(db, display_name="Elena"), new_user(db, display_name="Mother")]
    return [user.id for user in users]


def outstanding_ids(factory, user_id):
    with factory() as db:
        return db.scalars(
            sa.select(IdentityInvitation.id).where(
                IdentityInvitation.user_id == user_id,
                IdentityInvitation.consumed_at.is_(None),
                IdentityInvitation.revoked_at.is_(None),
            )
        ).all()


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_of_many_concurrent_claims_of_one_invitation_exactly_one_succeeds(engine, factory, two_users, round_number):
    with factory() as db, db.begin():
        issued = issue_invitation(db, two_users[0], now=NOW)
    subjects = [f"{index}{SUB}" for index in range(8)]

    results = race(engine, [lambda db, s=s: claim(db, issued, subject=s) for s in subjects])

    assert [kind for kind, _ in results] == ["ok"] * 8, results  # none failed with "database is locked"
    winners = [value for _, value in results if value is not None]
    assert len(winners) == 1 and winners[0].user_id == two_users[0]
    with factory() as db:
        assert [e.subject for e in entries(db)] == [winners[0].subject]  # one identity, the winner's
        assert columns_of(db, issued).consumed_at == LATER


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_one_identity_cannot_be_linked_to_two_users_by_two_invitations_at_once(
    engine, factory, two_users, round_number
):
    with factory() as db, db.begin():
        mine = issue_invitation(db, two_users[0], now=NOW)
        theirs = issue_invitation(db, two_users[1], now=NOW)

    results = race(
        engine,
        [lambda db: claim(db, mine, subject=SUB), lambda db: claim(db, theirs, subject=SUB)],
    )

    kinds = sorted(kind if kind == "ok" else type(value).__name__ for kind, value in results)
    assert kinds == ["IdentityRefused", "ok"], results
    with factory() as db:
        (entry,) = entries(db)
        assert entry.subject == SUB
        # the loser's invitation was not used up: it is still valid for the right person
        loser = mine if entry.user_id == two_users[1] else theirs
        assert columns_of(db, loser).consumed_at is None and verify(db, loser) is not None


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_concurrent_reissues_leave_exactly_one_outstanding_invitation(engine, factory, two_users, round_number):
    jobs = [lambda db: issue_invitation(db, two_users[0], now=NOW) for _ in range(8)]

    results = race(engine, jobs)

    assert [kind for kind, _ in results] == ["ok"] * 8, results
    issued = [value for _, value in results]
    assert len({item.token for item in issued}) == 8 and len({item.invitation_id for item in issued}) == 8
    assert sorted(item.superseded for item in issued) == [0] + [1] * 7  # each revoked the one before it
    assert len(outstanding_ids(factory, two_users[0])) == 1
    with factory() as db:
        assert count(db, IdentityInvitation) == 8
        assert db.scalar(sa.select(sa.func.count()).where(IdentityInvitation.revoked_at.is_not(None))) == 7


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_a_reissue_and_a_claim_never_leave_a_linked_user_with_a_live_invitation(
    engine, factory, two_users, round_number
):
    with factory() as db, db.begin():
        old = issue_invitation(db, two_users[0], now=NOW)

    results = race(
        engine,
        [lambda db: claim(db, old, subject=SUB), lambda db: issue_invitation(db, two_users[0], now=LATER)],
    )

    (claim_kind, claimed), (issue_kind, issued) = results
    with factory() as db:
        linked = len(entries(db)) == 1
    live = outstanding_ids(factory, two_users[0])
    if claimed is not None:  # the claim came first: the user is linked, and the reissue was refused
        assert (claim_kind, issue_kind) == ("ok", "error") and linked
        assert isinstance(issued, InvitationRefused) and issued.reason is InvitationRefusal.TELEGRAM_PROVISIONED
        assert live == []
    else:  # the reissue came first: the old secret was dead when the claim looked at it
        assert (claim_kind, issue_kind) == ("ok", "ok") and not linked
        assert claimed is None and live == [issued.invitation_id]


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_a_revocation_and_a_claim_agree_on_who_won(engine, factory, two_users, round_number):
    with factory() as db, db.begin():
        issued = issue_invitation(db, two_users[0], now=NOW)

    results = race(
        engine,
        [lambda db: claim(db, issued, subject=SUB), lambda db: revoke_invitations(db, two_users[0], now=LATER)],
    )

    (_, claimed), (_, revoked) = results
    with factory() as db:
        row = columns_of(db, issued)
        assert not (row.consumed_at is not None and row.revoked_at is not None)  # never both
        if claimed is not None:
            assert revoked == 0 and row.consumed_at is not None and len(entries(db)) == 1
        else:
            assert revoked == 1 and row.revoked_at is not None and entries(db) == []
