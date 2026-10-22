"""The two units of the Telegram invitation sign-in, and the column that joins them.

begin_invitation_login is the START (the secret is verified and the login's state is stored
with the invitation's id), complete_invitation_login the END (the id is read back from that
state; claim, session and the check of whose it is, in one transaction). Runs against both the
schema created from the models and the migrated one (tests/persistence_support.py); nothing here
talks to Telegram or to HTTP.

The properties that matter, each pinned below: the id a login claims is the one verified at its
start and nothing a caller can choose at the end; nothing is stored for an invitation that is
not live, and nothing is used up by starting; a revocation, a reissue or a link made by another
route between the two wins; the end is all or nothing (an exception anywhere, in the claim, the
session or the check of whose it is, leaves the invitation valid, nothing on the allowlist and no
identity link or session); of any number of concurrent ends exactly one succeeds; an identity
that is already on the allowlist is never adopted or un-revoked; the column is a foreign key
that cascades, and its value never changes.
"""

import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.persistence import authn
from house_helper.persistence import invitations as invitations_module
from house_helper.persistence.allowlist import allow, revoke
from house_helper.persistence.authn import (
    SESSION_LIFETIME,
    IssuedSession,
    LoginDenied,
    OidcLoginSecrets,
    authenticate,
    begin_oidc_login,
    consume_oidc_login,
    hash_secret,
    is_invitation_login,
)
from house_helper.persistence.database import create_db_engine, create_session_factory
from house_helper.persistence.invitations import (
    IdentityRefused,
    InvitationNotUsable,
    SignedInAsAnotherUser,
    begin_invitation_login,
    complete_invitation_login,
    issue_invitation,
    revoke_invitations,
)
from house_helper.persistence.models import (
    AllowlistEntry,
    IdentityInvitation,
    IdentityLink,
    OidcState,
    User,
)
from house_helper.persistence.models import Session as LoginSession
from tests.persistence_support import (  # noqa: F401  (fixtures)
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
from tests.test_persistence_invitations import (
    LATER,
    NOW,
    OTHER_SUB,
    ROUNDS,
    SUB,
    TWO_HOURS,
    race,
)

STATE = "state-of-an-invitation-login"
NONCE = "nonce-value"
VERIFIER = "verifier-value"


@pytest.fixture
def user(session):
    return new_user(session, display_name="Mother")


@pytest.fixture
def other(session):
    return new_user(session, display_name="Elena")


def begin(session, issued, *, state=STATE, token=None, now=LATER, **options):
    return begin_invitation_login(
        session,
        str(issued.invitation_id),
        issued.token if token is None else token,
        state,
        NONCE,
        VERIFIER,
        now=now,
        **options,
    )


def complete(session, issued, subject=SUB, *, now=LATER, **options):
    return complete_invitation_login(session, str(issued.invitation_id), subject, now=now, **options)


def counts(factory) -> dict[str, int]:
    with factory() as db:
        return {
            model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model))
            for model in (AllowlistEntry, IdentityLink, LoginSession, OidcState)
        }


def invitation_columns(factory, issued):
    with factory() as db:
        return db.execute(
            sa.select(IdentityInvitation.consumed_at, IdentityInvitation.revoked_at).where(
                IdentityInvitation.id == issued.invitation_id
            )
        ).one()


def is_unused(factory, issued) -> bool:
    row = invitation_columns(factory, issued)
    return row.consumed_at is None and row.revoked_at is None


def state_row(session):
    return session.execute(
        sa.select(OidcState.invitation_id, OidcState.state_hash, OidcState.provider, OidcState.expires_at)
    ).one()


# --- the start ----------------------------------------------------------------------------------


def test_a_started_login_remembers_the_invitation_that_was_verified_and_nothing_secret(session, user):
    issued = issue_invitation(session, user.id, now=NOW)

    assert begin(session, issued) is True

    row = state_row(session)
    assert row.invitation_id == issued.invitation_id
    assert row.state_hash == hash_secret(STATE) and row.provider == "telegram"
    assert row.expires_at == LATER + authn.OIDC_LOGIN_LIFETIME
    text = row_text(session, "oidc_states")
    assert issued.token not in text and STATE not in text  # the digest of the state, and the id
    secrets = consume_oidc_login(session, "telegram", STATE, now=LATER)
    assert secrets == OidcLoginSecrets(nonce=NONCE, code_verifier=VERIFIER, invitation_id=issued.invitation_id)


def test_starting_uses_nothing_up_and_may_be_done_again(session, user):
    issued = issue_invitation(session, user.id, now=NOW)

    assert begin(session, issued, state="first-state") is True
    assert begin(session, issued, state="second-state") is True

    row = session.execute(
        sa.select(IdentityInvitation.consumed_at, IdentityInvitation.revoked_at)
    ).one()
    assert (row.consumed_at, row.revoked_at) == (None, None)
    assert session.scalar(sa.select(sa.func.count()).select_from(OidcState)) == 2


def test_an_ordinary_login_has_no_invitation(session):
    begin_oidc_login(session, "telegram", STATE, NONCE, VERIFIER, now=NOW)

    secrets = consume_oidc_login(session, "telegram", STATE, now=LATER)

    assert secrets.invitation_id is None
    assert session.scalar(sa.select(OidcState.invitation_id)) is None


def test_the_start_does_not_take_an_invitation_id_from_the_caller_and_verifies_the_secret(session, user, other):
    mine = issue_invitation(session, user.id, now=NOW)
    theirs = issue_invitation(session, other.id, now=NOW)

    # their id with my secret, and the reverse: not the same invitation, so neither starts
    assert begin_invitation_login(
        session, str(theirs.invitation_id), mine.token, STATE, NONCE, VERIFIER, now=LATER
    ) is False
    assert begin_invitation_login(
        session, str(mine.invitation_id), theirs.token, STATE, NONCE, VERIFIER, now=LATER
    ) is False
    assert session.scalar(sa.select(sa.func.count()).select_from(OidcState)) == 0


def not_live_cases(session, user, other):
    """name -> (invitation, token to present, time): invitations that cannot start a login."""
    cases = {}
    issued = issue_invitation(session, user.id, now=NOW)
    cases["wrong secret"] = (issued, "A" * 43, LATER)
    cases["malformed secret"] = (issued, "short", LATER)
    cases["empty secret"] = (issued, "", LATER)
    cases["expired at the very end"] = (issued, issued.token, NOW + TWO_HOURS)
    cases["expired long ago"] = (issued, issued.token, NOW + TWO_HOURS + timedelta(days=3))

    used = issue_invitation(session, other.id, now=NOW)
    session.execute(
        sa.update(IdentityInvitation)
        .where(IdentityInvitation.id == used.invitation_id)
        .values(consumed_at=LATER)
    )
    cases["used"] = (used, used.token, LATER)
    return cases


def test_nothing_is_stored_for_an_invitation_that_is_not_live(session, user, other):
    for name, (issued, token, moment) in not_live_cases(session, user, other).items():
        assert begin(session, issued, token=token, now=moment) is False, name

    assert session.scalar(sa.select(sa.func.count()).select_from(OidcState)) == 0


def test_nothing_is_stored_for_a_revoked_or_superseded_invitation(session, user):
    first = issue_invitation(session, user.id, now=NOW)
    second = issue_invitation(session, user.id, now=NOW + timedelta(minutes=1))  # first is superseded
    assert begin(session, first) is False
    assert begin(session, second) is True
    revoke_invitations(session, user.id, now=LATER)

    assert begin(session, second, state="another-state") is False
    assert session.scalar(sa.select(sa.func.count()).select_from(OidcState)) == 1


def test_nothing_is_stored_for_a_user_who_is_disabled_or_already_linked(session, user, other):
    disabled = issue_invitation(session, user.id, now=NOW)
    linked = issue_invitation(session, other.id, now=NOW)
    user.disabled_at = NOW
    session.flush()
    allow(session, other, "telegram", SUB)  # linked by some other route after the invitation was issued

    assert begin(session, disabled) is False
    assert begin(session, linked) is False
    assert session.scalar(sa.select(sa.func.count()).select_from(OidcState)) == 0


def test_the_start_verifies_before_it_stores_and_writes_first(engine, session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    session.commit()

    with statements(engine) as seen:
        assert begin(session, issued) is True
        session.commit()

    assert seen[0].startswith("UPDATE identity_invitations")  # the write that takes SQLite's lock
    verify_at = next(i for i, text in enumerate(seen) if text.startswith("SELECT") and "token_hash" in text)
    purge_at = next(i for i, text in enumerate(seen) if text.startswith("DELETE FROM oidc_states"))
    insert_at = next(i for i, text in enumerate(seen) if text.startswith("INSERT INTO oidc_states"))
    assert 0 < verify_at < purge_at < insert_at  # verified first, stored last


def test_a_refused_start_writes_nothing_but_the_lock(engine, session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    session.commit()

    with statements(engine) as seen:
        assert begin(session, issued, token="A" * 43) is False
        session.rollback()

    assert not any(text.startswith(("INSERT", "DELETE")) for text in seen), seen


def test_expired_logins_are_swept_when_an_invitation_login_begins(session, user):
    begin_oidc_login(session, "telegram", "stale", NONCE, VERIFIER, now=NOW - timedelta(hours=1))
    issued = issue_invitation(session, user.id, now=NOW)

    assert begin(session, issued) is True

    assert consume_oidc_login(session, "telegram", "stale", now=NOW) is None
    assert session.scalar(sa.select(sa.func.count()).select_from(OidcState)) == 1


def test_a_browser_signed_in_as_another_user_cannot_start_and_the_same_user_can(session, user, other):
    issued = issue_invitation(session, user.id, now=NOW)

    with pytest.raises(SignedInAsAnotherUser):
        begin(session, issued, signed_in_user=other.id)
    assert session.scalar(sa.select(sa.func.count()).select_from(OidcState)) == 0

    assert begin(session, issued, signed_in_user=user.id) is True
    assert begin(session, issued, state="other-state", signed_in_user=None) is True


def test_the_other_user_is_only_told_about_after_the_secret_was_found_to_be_live(session, user, other):
    issued = issue_invitation(session, user.id, now=NOW)

    # a stranger's session and a wrong secret: just "not live", no hint of whose it is
    assert begin(session, issued, token="A" * 43, signed_in_user=other.id) is False


def test_the_login_that_began_with_an_invitation_is_known_by_its_state_whatever_its_age(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    begin(session, issued, state="bound-state")
    begin_oidc_login(session, "telegram", "plain-state", NONCE, VERIFIER, now=LATER)
    consume_oidc_login(session, "telegram", "bound-state", now=LATER)  # used up: still known

    assert is_invitation_login(session, "telegram", "bound-state") is True
    assert is_invitation_login(session, "telegram", "plain-state") is False
    assert is_invitation_login(session, "telegram", "no-such-state") is False
    assert is_invitation_login(session, "telegram", "not a state!") is False
    assert is_invitation_login(session, "google", "bound-state") is False


# --- the column -----------------------------------------------------------------------------


def state_row_for_sql(session, **overrides):
    return {
        "id": uuid.uuid4().hex,
        "state_hash": digest(uuid.uuid4().hex),
        "provider": "telegram",
        "nonce": NONCE,
        "code_verifier": VERIFIER,
        "created_at": stored(NOW),
        "expires_at": stored(NOW + timedelta(minutes=10)),
        **overrides,
    }


def test_a_login_can_only_name_an_invitation_that_exists(session):
    with rejected(session, "FOREIGN KEY"):
        raw_insert(session, "oidc_states", **state_row_for_sql(session, invitation_id=uuid.uuid4().hex))


def test_deleting_an_invitation_deletes_the_logins_bound_to_it_and_no_other(session, user):
    issued = issue_invitation(session, user.id, now=NOW)
    begin(session, issued, state="bound-state")
    begin_oidc_login(session, "telegram", "plain-state", NONCE, VERIFIER, now=NOW)

    session.execute(sa.delete(IdentityInvitation).where(IdentityInvitation.id == issued.invitation_id))

    remaining = session.execute(sa.select(OidcState.state_hash)).scalars().all()
    assert remaining == [hash_secret("plain-state")]  # a bound login does not become an ordinary one


def test_deleting_a_user_deletes_their_invitations_and_the_logins_bound_to_them(session, user, other):
    mine = issue_invitation(session, user.id, now=NOW)
    theirs = issue_invitation(session, other.id, now=NOW)
    begin(session, mine, state="mine")
    begin(session, theirs, state="theirs")

    session.execute(sa.delete(User).where(User.id == user.id))

    remaining = session.execute(sa.select(OidcState.invitation_id)).scalars().all()
    assert remaining == [theirs.invitation_id]


def test_the_invitation_a_login_names_cannot_be_changed_once_stored(session, user, other):
    mine = issue_invitation(session, user.id, now=NOW)
    theirs = issue_invitation(session, other.id, now=NOW)
    begin(session, mine)
    stored_state = session.scalars(sa.select(OidcState)).one()

    stored_state.invitation_id = theirs.invitation_id
    with pytest.raises(ValueError, match="invitation_id cannot change once stored"):
        session.flush()
    session.rollback()


# --- the end ---------------------------------------------------------------------------------


def test_the_end_links_the_verified_identity_to_the_invited_user_and_opens_their_session(session, user):
    issued = issue_invitation(session, user.id, now=NOW)

    result = complete(session, issued)

    assert isinstance(result, IssuedSession) and result.user_id == user.id
    assert result.expires_at == LATER + SESSION_LIFETIME  # the existing policy, not a new one
    entry = session.scalars(sa.select(AllowlistEntry)).one()
    assert (entry.user_id, entry.provider, entry.subject, entry.revoked_at) == (user.id, "telegram", SUB, None)
    link = session.scalars(sa.select(IdentityLink)).one()
    assert (link.user_id, link.provider, link.subject) == (user.id, "telegram", SUB)
    opened = session.scalars(sa.select(LoginSession)).one()
    assert opened.user_id == user.id and opened.identity_link_id == link.id  # provenance
    assert session.scalar(
        sa.select(IdentityInvitation.consumed_at).where(IdentityInvitation.id == issued.invitation_id)
    ) == LATER
    live = authenticate(session, result.token, now=LATER)
    assert live is not None and live.user_id == user.id


def test_the_session_lasts_as_long_as_the_caller_says_and_the_default_is_fourteen_days(session, user, other):
    short = issue_invitation(session, user.id, now=NOW)
    normal = issue_invitation(session, other.id, now=NOW)

    assert complete(session, short, lifetime=timedelta(hours=1)).expires_at == LATER + timedelta(hours=1)
    assert complete(session, normal, subject=OTHER_SUB).expires_at == LATER + timedelta(days=14)


def test_two_invited_users_link_two_different_identities_and_nothing_crosses(session, user, other):
    mother = issue_invitation(session, user.id, now=NOW)
    owner = issue_invitation(session, other.id, now=NOW)

    mother_session = complete(session, mother, subject=SUB)
    owner_session = complete(session, owner, subject=OTHER_SUB)

    assert (mother_session.user_id, owner_session.user_id) == (user.id, other.id)
    by_subject = {
        e.subject: e.user_id for e in session.scalars(sa.select(AllowlistEntry)).all()
    }
    assert by_subject == {SUB: user.id, OTHER_SUB: other.id}
    assert authenticate(session, mother_session.token, now=LATER).user_id == user.id
    assert authenticate(session, owner_session.token, now=LATER).user_id == other.id


def test_the_end_reads_the_invitation_from_the_state_so_a_state_can_not_claim_another(session, user, other):
    mine = issue_invitation(session, user.id, now=NOW)
    theirs = issue_invitation(session, other.id, now=NOW)
    begin(session, mine, state="my-state")
    secrets = consume_oidc_login(session, "telegram", "my-state", now=LATER)

    result = complete_invitation_login(session, str(secrets.invitation_id), SUB, now=LATER)

    assert result.user_id == user.id  # the invitation the state named, whoever else has one
    assert session.scalar(
        sa.select(IdentityInvitation.consumed_at).where(IdentityInvitation.id == theirs.invitation_id)
    ) is None


NOT_USABLE = ["unknown", "malformed", "used", "revoked", "expired", "superseded", "disabled", "linked"]


@pytest.mark.parametrize("why", NOT_USABLE)
def test_an_invitation_that_cannot_be_used_ends_nothing_and_writes_nothing(factory, why):
    with factory() as db, db.begin():
        invited = new_user(db, display_name="Mother")
        issued = issue_invitation(db, invited.id, now=NOW)
        moment = LATER
        target = str(issued.invitation_id)
        if why == "unknown":
            target = str(uuid.uuid4())
        elif why == "malformed":
            target = "not-an-id"
        elif why == "used":
            db.execute(sa.update(IdentityInvitation).values(consumed_at=LATER))
        elif why == "revoked":
            revoke_invitations(db, invited.id, now=LATER)
        elif why == "expired":
            moment = NOW + TWO_HOURS
        elif why == "superseded":
            issue_invitation(db, invited.id, now=NOW + timedelta(minutes=1))
        elif why == "disabled":
            invited.disabled_at = LATER
        elif why == "linked":
            allow(db, invited, "telegram", OTHER_SUB)  # linked by another route since it was issued
    before = counts(factory)

    with pytest.raises(InvitationNotUsable):
        with factory() as db, db.begin():
            complete_invitation_login(db, target, SUB, now=moment)

    assert counts(factory) == before


@pytest.mark.parametrize(
    "subject",
    [None, "", "   ", "has@sign", "x" * 500, "ünï", "tab\tbed", 12345],
    ids=["none", "empty", "blank", "at-sign", "too-long", "non-ascii", "control", "not-text"],
)
def test_a_subject_that_cannot_be_an_identity_is_refused_and_the_invitation_stays_valid(factory, subject):
    with factory() as db, db.begin():
        issued = issue_invitation(db, new_user(db, display_name="Mother").id, now=NOW)
    before = counts(factory)

    with pytest.raises(IdentityRefused):
        with factory() as db, db.begin():
            complete(db, issued, subject=subject)

    assert counts(factory) == before and is_unused(factory, issued)


def test_an_identity_that_belongs_to_another_user_is_refused_and_stays_theirs(factory):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        owner = new_user(db, display_name="Elena")
        allow(db, owner, "telegram", SUB)
        issued = issue_invitation(db, mine.id, now=NOW)
    before = counts(factory)

    with pytest.raises(IdentityRefused):
        with factory() as db, db.begin():
            complete(db, issued, subject=SUB)

    assert counts(factory) == before and is_unused(factory, issued)
    with factory() as db:
        entry = db.scalars(sa.select(AllowlistEntry)).one()
        assert (entry.user_id, entry.subject) == (owner.id, SUB)


def test_a_revoked_identity_is_never_lifted_by_an_invitation(factory):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        owner = new_user(db, display_name="Elena")
        allow(db, owner, "telegram", SUB)
        revoke(db, "telegram", SUB, now=NOW)
        issued = issue_invitation(db, mine.id, now=NOW)

    with pytest.raises(IdentityRefused):
        with factory() as db, db.begin():
            complete(db, issued, subject=SUB)

    with factory() as db:
        entry = db.scalars(sa.select(AllowlistEntry)).one()
        assert entry.user_id == owner.id and entry.revoked_at is not None  # still revoked, still theirs
    assert is_unused(factory, issued)


def test_a_browser_signed_in_as_another_user_is_refused_with_the_invitation_left_valid(factory):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        owner = new_user(db, display_name="Elena")
        issued = issue_invitation(db, mine.id, now=NOW)
    before = counts(factory)

    with pytest.raises(SignedInAsAnotherUser):
        with factory() as db, db.begin():
            complete(db, issued, signed_in_user=owner.id)

    assert counts(factory) == before and is_unused(factory, issued)
    with factory() as db, db.begin():  # and it still works, for the browser that is signed in as nobody
        assert complete(db, issued, signed_in_user=None).user_id == mine.id


def test_the_same_user_may_link_a_telegram_identity_from_a_browser_signed_in_as_them(factory):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        issued = issue_invitation(db, mine.id, now=NOW)

    with factory() as db, db.begin():
        assert complete(db, issued, signed_in_user=mine.id).user_id == mine.id


# --- all or nothing --------------------------------------------------------------------------


def test_a_failure_while_the_session_is_issued_undoes_the_claim_the_entry_and_the_link(factory, monkeypatch):
    with factory() as db, db.begin():
        issued = issue_invitation(db, new_user(db, display_name="Mother").id, now=NOW)
    before = counts(factory)
    real = authn.complete_login

    def boom(session, provider, subject, **options):
        real(session, provider, subject, **options)  # the link and the session really were written
        raise RuntimeError("the session could not be issued")

    monkeypatch.setattr(invitations_module.authn, "complete_login", boom)

    with pytest.raises(RuntimeError):
        with factory() as db, db.begin():
            complete(db, issued)

    assert counts(factory) == before and is_unused(factory, issued)
    monkeypatch.undo()
    with factory() as db, db.begin():  # the invitation was never touched: the person can try again
        assert complete(db, issued).user_id is not None


def test_a_database_failure_at_the_session_insert_undoes_everything_before_it(engine, factory):
    with factory() as db, db.begin():
        issued = issue_invitation(db, new_user(db, display_name="Mother").id, now=NOW)
    before = counts(factory)
    with engine.begin() as connection:  # the claim, the entry and the link succeed; the session cannot
        connection.exec_driver_sql(
            "CREATE TRIGGER refuse_sessions BEFORE INSERT ON sessions "
            "BEGIN SELECT RAISE(ABORT, 'no sessions today'); END"
        )

    with pytest.raises(sa.exc.DBAPIError):
        with factory() as db, db.begin():
            complete(db, issued)

    assert counts(factory) == before and is_unused(factory, issued)


def test_a_session_of_somebody_else_is_refused_and_everything_is_undone(factory, monkeypatch):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        owner = new_user(db, display_name="Elena")
        issued = issue_invitation(db, mine.id, now=NOW)
    before = counts(factory)

    def somebody_elses(session, provider, subject, **options):
        return IssuedSession(token="t" * 43, user_id=owner.id, session_id=uuid.uuid4(), expires_at=LATER)

    monkeypatch.setattr(invitations_module.authn, "complete_login", somebody_elses)

    with pytest.raises(LoginDenied):
        with factory() as db, db.begin():
            complete(db, issued)

    assert counts(factory) == before and is_unused(factory, issued)


def test_a_refusal_leaves_the_invitation_valid_even_for_a_caller_that_commits_anyway(factory):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        allow(db, new_user(db, display_name="Elena"), "telegram", SUB)
        issued = issue_invitation(db, mine.id, now=NOW)

    with factory() as db, db.begin():  # swallows the refusal and commits
        with pytest.raises(IdentityRefused):
            complete(db, issued, subject=SUB)

    assert is_unused(factory, issued)


# --- what happens between the start and the end -----------------------------------------------


def started(factory, state=STATE):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        issued = issue_invitation(db, mine.id, now=NOW)
        assert begin(db, issued, state=state) is True
    return mine.id, issued


def end_of(factory, state=STATE, subject=SUB):
    """What the callback does: the invitation the state names, claimed for the verified `sub`."""
    with factory() as db, db.begin():
        secrets = consume_oidc_login(db, "telegram", state, now=LATER)
        return complete_invitation_login(db, str(secrets.invitation_id), subject, now=LATER)


def test_a_revocation_between_the_start_and_the_end_wins(factory):
    user_id, issued = started(factory)
    with factory() as db, db.begin():
        assert revoke_invitations(db, user_id, now=LATER) == 1

    with pytest.raises(InvitationNotUsable):
        end_of(factory)

    assert counts(factory)["allowlist_entries"] == 0 and counts(factory)["sessions"] == 0


def test_a_reissue_between_the_start_and_the_end_wins(factory):
    user_id, issued = started(factory)
    with factory() as db, db.begin():
        issue_invitation(db, user_id, now=LATER)  # the old one is superseded

    with pytest.raises(InvitationNotUsable):
        end_of(factory)

    assert counts(factory)["allowlist_entries"] == 0


def test_a_link_made_by_another_route_between_the_start_and_the_end_wins(factory):
    user_id, issued = started(factory)
    with factory() as db, db.begin():
        allow(db, db.get(User, user_id), "telegram", OTHER_SUB)

    with pytest.raises(InvitationNotUsable):
        end_of(factory)

    with factory() as db:
        assert [e.subject for e in db.scalars(sa.select(AllowlistEntry))] == [OTHER_SUB]


def test_the_end_after_the_invitation_expired_between_the_two_is_refused(factory):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        issued = issue_invitation(db, mine.id, now=NOW)
        assert begin(db, issued, now=NOW + TWO_HOURS - timedelta(minutes=1)) is True

    with pytest.raises(InvitationNotUsable):
        with factory() as db, db.begin():
            secrets = consume_oidc_login(db, "telegram", STATE, now=NOW + TWO_HOURS - timedelta(seconds=30))
            complete_invitation_login(db, str(secrets.invitation_id), SUB, now=NOW + TWO_HOURS)

    assert is_unused(factory, issued)  # expired is not used up, and not withdrawn either
    assert counts(factory)["allowlist_entries"] == 0 and counts(factory)["sessions"] == 0


def test_the_used_up_invitation_cannot_be_used_by_a_second_login_that_started_before(factory):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        issued = issue_invitation(db, mine.id, now=NOW)
        assert begin(db, issued, state="first-state") is True
        assert begin(db, issued, state="second-state") is True  # two browsers, one invitation

    assert end_of(factory, "first-state").user_id == mine.id
    with pytest.raises(InvitationNotUsable):
        end_of(factory, "second-state", subject=OTHER_SUB)

    with factory() as db:
        assert [e.subject for e in db.scalars(sa.select(AllowlistEntry))] == [SUB]


# --- real concurrency (SQLite file, one engine and one transaction per thread) -----------------


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_of_many_concurrent_ends_of_one_invitation_exactly_one_succeeds(engine, factory, round_number):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        issued = issue_invitation(db, mine.id, now=NOW)
    subjects = [f"{index}{SUB}" for index in range(6)]

    results = race(engine, [lambda db, s=s: complete(db, issued, subject=s) for s in subjects])

    outcomes = sorted(kind if kind == "ok" else type(value).__name__ for kind, value in results)
    assert outcomes == ["InvitationNotUsable"] * 5 + ["ok"], results  # none failed with "database is locked"
    with factory() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(AllowlistEntry)) == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(IdentityLink)) == 1
        assert db.scalar(sa.select(sa.func.count()).select_from(LoginSession)) == 1


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_one_identity_cannot_be_linked_to_two_users_by_two_invitation_logins_at_once(
    engine, factory, round_number
):
    with factory() as db, db.begin():
        mother = issue_invitation(db, new_user(db, display_name="Mother").id, now=NOW)
        owner = issue_invitation(db, new_user(db, display_name="Elena").id, now=NOW)

    results = race(
        engine,
        [lambda db: complete(db, mother, subject=SUB), lambda db: complete(db, owner, subject=SUB)],
    )

    outcomes = sorted(kind if kind == "ok" else type(value).__name__ for kind, value in results)
    assert outcomes == ["IdentityRefused", "ok"], results
    with factory() as db:
        (entry,) = db.scalars(sa.select(AllowlistEntry)).all()
        assert db.scalar(sa.select(sa.func.count()).select_from(LoginSession)) == 1
    loser = mother if entry.user_id == owner_user_id(factory, owner) else owner
    assert is_unused(factory, loser)  # the loser's invitation is still valid for the right person


def owner_user_id(factory, issued):
    with factory() as db:
        return db.scalar(
            sa.select(IdentityInvitation.user_id).where(IdentityInvitation.id == issued.invitation_id)
        )


@pytest.mark.parametrize("round_number", range(ROUNDS))
def test_a_start_and_a_revocation_at_once_leave_a_state_the_end_then_refuses(engine, factory, round_number):
    with factory() as db, db.begin():
        mine = new_user(db, display_name="Mother")
        issued = issue_invitation(db, mine.id, now=NOW)

    results = race(
        engine,
        [
            lambda db: begin(db, issued),
            lambda db: revoke_invitations(db, mine.id, now=LATER),
        ],
    )

    assert [kind for kind, _ in results] == ["ok", "ok"], results
    # Whichever came first, nobody gets in: if the revocation was first the start stored
    # nothing; if the start was first its state is there, and the end refuses it.
    if counts(factory)["oidc_states"]:
        assert results[0] == ("ok", True)
        with pytest.raises(InvitationNotUsable):
            end_of(factory)
    else:
        assert results[0] == ("ok", False)
    assert counts(factory)["allowlist_entries"] == 0 and counts(factory)["sessions"] == 0
