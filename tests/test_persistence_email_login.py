"""Emailed sign-in links, in the persistence layer: issuing, previewing, redeeming,
withdrawing, and how revoking an identity reaches them.

Runs against both the schema created from the models and the migrated one
(tests/persistence_support.py). Nothing here sends mail or speaks HTTP: the functions
take the address, the secret and the clock as arguments, which is how the tests move
time. The HTTP behaviour on top is in tests/test_web_email_login.py.

The properties that matter, each pinned below: a link exists only for an address on the
allowlist, for the user that entry names, and never changes whose it is; the secret is
256 random bits and only its digest is stored; a link is used once, by exactly one of
any number of concurrent callers, and not at or after its expiry; looking at it uses
nothing up; a session it opens records the email identity as its provenance and is ended
with that identity; revoking the identity withdraws the links not yet used and they do
not come back; and a browser that is signed in as somebody else leaves the link usable.
"""

import base64
import itertools
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta

import pytest
import sqlalchemy as sa

from house_helper.persistence import authn, email_login
from house_helper.persistence.allowlist import _allowed_user_query, allow, revoke
from house_helper.persistence.authn import authenticate, complete_login, hash_secret
from house_helper.persistence.email_login import (
    EMAIL_LOGIN_LIFETIME,
    EMAIL_REQUEST_COOLDOWN,
    EMAIL_TOKEN_BYTES,
    IssueOutcome,
    SignInConflict,
    is_challenge_id,
    is_email_login_token,
    issue_email_login,
    mask_address,
    preview_email_login,
    redeem_email_login,
    withdraw_email_login,
)
from house_helper.persistence.models import (
    AllowlistEntry,
    EmailLoginChallenge,
    IdentityLink,
    Session,
    User,
)
from tests.persistence_support import (  # noqa: F401  (fixtures)
    count,
    engine,
    factory,
    new_user,
    raw_insert,
    raw_update,
    rejected,
    session,
    stored,
    utc,
)

NOW = utc(2026, 10, 17, 12)
ADDRESS = "elena@example.com"
OTHER_ADDRESS = "other@example.com"
TELEGRAM_SUB = "1234123412341234123"


@pytest.fixture
def user(session):
    user = new_user(session, display_name="Elena")
    allow(session, user, "email", ADDRESS)
    session.commit()
    return user


@pytest.fixture
def other(session, user):
    other = new_user(session, display_name="Other")
    allow(session, other, "email", OTHER_ADDRESS)
    session.commit()
    return other


def issue(session, address=ADDRESS, **options):
    options.setdefault("now", NOW)
    return issue_email_login(session, address, **options)


def link_for(session, address=ADDRESS, **options):
    result = issue(session, address, **options)
    assert result.outcome is IssueOutcome.ISSUED, result
    session.commit()
    return result.link


def redeem(session, link, **options):
    options.setdefault("now", NOW)
    return redeem_email_login(session, str(link.challenge_id), link.token, **options)


def challenges(session):
    session.expire_all()
    return list(session.scalars(sa.select(EmailLoginChallenge).order_by(EmailLoginChallenge.created_at)))


def row_text(session, table: str) -> str:
    rows = session.execute(sa.text(f"SELECT * FROM {table}")).all()
    return "\n".join(repr(tuple(row)) for row in rows)


@contextmanager
def statements(engine):
    """The SQL the engine executes inside the block, without the transaction plumbing."""
    seen: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if not statement.startswith(("BEGIN", "SAVEPOINT", "RELEASE", "ROLLBACK", "PRAGMA")):
            seen.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)


def first_word(statement: str) -> str:
    return statement.lstrip().split(None, 1)[0].upper()


# --- the policy -------------------------------------------------------------------------


def test_the_policy_is_ten_minutes_a_minute_and_256_bits():
    assert EMAIL_LOGIN_LIFETIME == timedelta(minutes=10)
    assert EMAIL_REQUEST_COOLDOWN == timedelta(minutes=1)
    assert EMAIL_TOKEN_BYTES * 8 >= 256
    assert EMAIL_REQUEST_COOLDOWN <= EMAIL_LOGIN_LIFETIME


# --- issuing ----------------------------------------------------------------------------


def test_an_allowlisted_address_gets_a_link_bound_to_its_user_and_address(session, user):
    result = issue(session)
    session.commit()

    assert result.outcome is IssueOutcome.ISSUED
    link = result.link
    assert link.address == ADDRESS
    assert link.expires_at == NOW + timedelta(minutes=10)
    (row,) = challenges(session)
    assert row.id == link.challenge_id
    assert (row.user_id, row.provider, row.subject) == (user.id, "email", ADDRESS)
    assert (row.created_at, row.expires_at) == (NOW, NOW + timedelta(minutes=10))
    assert row.consumed_at is None and row.revoked_at is None


def test_the_secret_is_256_random_bits_and_never_the_same_twice(session, user):
    first = link_for(session)
    second = link_for(session, now=NOW + timedelta(minutes=2))

    for link in (first, second):
        assert is_email_login_token(link.token)
        assert len(base64.urlsafe_b64decode(link.token + "=")) == 32  # 256 bits
    assert first.token != second.token
    assert first.challenge_id != second.challenge_id


def test_only_a_digest_of_the_secret_is_stored(session, user):
    link = link_for(session)

    (row,) = challenges(session)
    assert row.token_hash == hash_secret(link.token) and len(row.token_hash) == 64
    stored_text = row_text(session, "email_login_challenges")
    assert link.token not in stored_text
    assert hash_secret(link.token) in stored_text


def test_the_secret_is_in_no_repr(session, user):
    result = issue(session)

    assert result.link.token not in repr(result) and ADDRESS not in repr(result)
    assert result.link.token not in repr(result.link)


@pytest.mark.parametrize(
    "asked", [" elena@Example.COM ", "elena@EXAMPLE.COM", "elena@example.com\n", "\telena@EXAMPLE.com"]
)
def test_the_address_is_matched_the_way_the_allowlist_stores_it(session, user, asked):
    result = issue(session, asked)

    assert result.outcome is IssueOutcome.ISSUED
    assert result.link.address == ADDRESS  # the stored address, not the asker's text


@pytest.mark.parametrize(
    "asked",
    [
        "Elena@example.com",
        "ELENA@example.com",
        "eLENA@example.com",
        " ELENA@Example.COM ",  # the domain's case is free, the local part's is not
        "elenA@EXAMPLE.com",
    ],
)
def test_another_case_of_the_local_part_is_not_the_allowlisted_address(session, user, asked):
    result = issue(session, asked)

    assert result.outcome is IssueOutcome.NOT_ELIGIBLE and result.link is None
    assert challenges(session) == []  # no link, so no mail: nothing can go to that mailbox


def test_the_approved_casing_is_kept_in_the_challenge_the_link_and_the_session(session):
    approved = "Victim@example.com"
    victim = new_user(session, display_name="Victim")
    allow(session, victim, "email", approved)
    session.commit()

    result = issue(session, " Victim@EXAMPLE.COM ")  # only the domain's case is the asker's own
    session.commit()
    link = result.link

    assert result.outcome is IssueOutcome.ISSUED
    assert link.address == approved  # where the mail goes: the address as approved
    assert [c.subject for c in challenges(session)] == [approved]
    assert preview_email_login(session, str(link.challenge_id), now=NOW).masked_address == (
        "V***@example.com"
    )

    issued = redeem(session, link)
    session.commit()

    assert issued is not None and issued.user_id == victim.id
    assert session.scalar(sa.select(IdentityLink.subject)) == approved
    assert authenticate(session, issued.token, now=NOW) is not None


def test_a_mailbox_that_differs_in_case_only_cannot_sign_in_as_the_approved_identity(session):
    # The two spellings may be two mailboxes. Whoever controls the other one can ask
    # for a link, and gets it only if an administrator approved that spelling too,
    # for the user it was approved for; never the approved one's.
    first = new_user(session, display_name="First")
    allow(session, first, "email", "Victim@example.com")
    session.commit()

    assert issue(session, "victim@example.com").outcome is IssueOutcome.NOT_ELIGIBLE

    second = new_user(session, display_name="Second")
    allow(session, second, "email", "victim@example.com")
    session.commit()
    result = issue(session, "victim@example.com")
    session.commit()
    issued = redeem(session, result.link)
    session.commit()

    assert result.link.address == "victim@example.com"
    assert issued.user_id == second.id and issued.user_id != first.id
    assert session.scalar(sa.select(sa.func.count()).select_from(Session).where(Session.user_id == first.id)) == 0


@pytest.mark.parametrize(
    "asked",
    [
        "elena+tag@example.com",  # an alias is another address: nothing is folded together
        "el.ena@example.com",
        "elena@mail.example.com",
        "elena@example.com.evil.test",
        "evil.elena@example.com",
        "ELENA@EXAMPLE.COM.",
        "unknown@example.com",
    ],
)
def test_no_alias_or_lookalike_of_an_allowlisted_address_is_allowlisted(session, user, asked):
    result = issue(session, asked)

    assert result.outcome is IssueOutcome.NOT_ELIGIBLE and result.link is None
    assert challenges(session) == []


@pytest.mark.parametrize(
    "asked",
    [
        "",
        " ",
        "no-at-sign",
        "a@@example.com",
        "@example.com",
        "elena@",
        "elena @example.com",
        "elena@example.com,other@example.com",
        "élena@example.com",  # not ASCII
        "elena@exämple.com",
        "elena@example.com\x00",
        "elena@example.com" + "x" * 300,
        None,
        7,
        b"elena@example.com",
        ["elena@example.com"],
    ],
)
def test_input_that_is_not_an_address_never_raises_and_gets_no_link(session, user, asked):
    result = issue(session, asked)

    assert result.outcome is IssueOutcome.NOT_ELIGIBLE and result.link is None
    assert challenges(session) == []


def test_a_telegram_identity_is_not_an_email_address(session, user):
    allow(session, user, "telegram", TELEGRAM_SUB)
    session.commit()

    assert issue(session, TELEGRAM_SUB).outcome is IssueOutcome.NOT_ELIGIBLE


def test_a_revoked_address_gets_no_link_until_it_is_allowed_again(session, user):
    revoke(session, "email", ADDRESS, now=NOW)
    session.commit()

    assert issue(session).outcome is IssueOutcome.NOT_ELIGIBLE

    allow(session, user, "email", ADDRESS)
    session.commit()
    assert issue(session).outcome is IssueOutcome.ISSUED


def test_the_address_of_a_disabled_user_gets_no_link(session, user):
    user.disabled_at = NOW
    session.commit()

    assert issue(session).outcome is IssueOutcome.NOT_ELIGIBLE
    assert challenges(session) == []


def test_asking_again_within_the_cooldown_issues_nothing(session, user):
    link_for(session)

    soon = issue(session, now=NOW + timedelta(seconds=59, microseconds=999999))
    session.commit()

    assert soon.outcome is IssueOutcome.COOLDOWN and soon.link is None
    assert len(challenges(session)) == 1


def test_the_cooldown_ends_exactly_when_it_has_elapsed(session, user):
    link_for(session)

    later = issue(session, now=NOW + timedelta(seconds=60))
    session.commit()

    assert later.outcome is IssueOutcome.ISSUED
    assert len(challenges(session)) == 2


def test_the_cooldown_counts_links_that_were_used_or_withdrawn(session, user):
    link = link_for(session)
    assert redeem(session, link) is not None
    session.commit()
    assert issue(session, now=NOW + timedelta(seconds=10)).outcome is IssueOutcome.COOLDOWN

    again = link_for(session, now=NOW + timedelta(minutes=5))
    assert withdraw_email_login(session, again.challenge_id, now=NOW + timedelta(minutes=5))
    session.commit()
    assert issue(session, now=NOW + timedelta(minutes=5, seconds=10)).outcome is IssueOutcome.COOLDOWN


def test_the_cooldown_belongs_to_the_address_not_to_everyone(session, user, other):
    link_for(session, ADDRESS)

    assert issue(session, OTHER_ADDRESS).outcome is IssueOutcome.ISSUED


def test_issuing_removes_the_expired_links_of_that_address_only(session, user, other):
    old = link_for(session, ADDRESS)
    others_old = link_for(session, OTHER_ADDRESS)
    later = NOW + timedelta(minutes=10)  # both are expired at exactly this moment

    fresh = link_for(session, ADDRESS, now=later)

    remaining = {row.id for row in challenges(session)}
    assert remaining == {others_old.challenge_id, fresh.challenge_id}
    assert old.challenge_id not in remaining


def test_an_unknown_address_costs_no_row_and_only_a_write_lock(engine, factory, user):
    with factory() as db, statements(engine) as seen:
        with db.begin():
            assert issue_email_login(db, "nobody@example.com", now=NOW).outcome is IssueOutcome.NOT_ELIGIBLE

    assert first_word(seen[0]) == "UPDATE"
    assert not any(first_word(statement) in {"INSERT", "DELETE"} for statement in seen)
    with factory() as db:
        assert count(db, EmailLoginChallenge) == 0


@pytest.mark.parametrize(
    "action",
    [
        lambda db: issue_email_login(db, ADDRESS, now=NOW),
        lambda db: issue_email_login(db, "nobody@example.com", now=NOW),
        lambda db: issue_email_login(db, None, now=NOW),
    ],
)
def test_issuing_is_a_write_first_transaction(engine, factory, user, action):
    with factory() as db, statements(engine) as seen:
        with db.begin():
            action(db)

    assert first_word(seen[0]) == "UPDATE"


def test_issuing_asks_for_the_entrys_row_lock_in_exclusive_mode(session, user, monkeypatch):
    from sqlalchemy.dialects import postgresql, sqlite

    exclusive = _allowed_user_query("email", ADDRESS, True, True)
    shared = _allowed_user_query("email", ADDRESS, True, False)

    assert "FOR UPDATE OF allowlist_entries" in str(exclusive.compile(dialect=postgresql.dialect()))
    assert "FOR SHARE OF allowlist_entries" in str(shared.compile(dialect=postgresql.dialect()))
    assert "FOR" not in str(exclusive.compile(dialect=sqlite.dialect()))

    asked, real = [], email_login.allowed_user

    def spy(*args, **kwargs):
        asked.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(email_login, "allowed_user", spy)
    issue(session)
    link = link_for(session, now=NOW + timedelta(minutes=2))
    redeem(session, link)
    assert asked == [
        {"lock": True, "exclusive": True},
        {"lock": True, "exclusive": True},
        {"lock": True},  # redeeming: the shared lock a login takes
    ]


def test_concurrent_requests_for_one_address_issue_one_link(factory, user):
    results, errors = run_concurrently(factory, 8, lambda db: issue_email_login(db, ADDRESS, now=NOW))

    assert errors == []
    outcomes = sorted(result.outcome.value for result in results)
    assert outcomes == ["cooldown"] * 7 + ["issued"]
    with factory() as db:
        assert count(db, EmailLoginChallenge) == 1


# --- looking at a link --------------------------------------------------------------------


def test_the_preview_says_which_address_masked_and_when_it_ends(session, user):
    link = link_for(session)

    preview = preview_email_login(session, str(link.challenge_id), now=NOW)

    assert preview.masked_address == "e***@example.com"
    assert preview.expires_at == NOW + timedelta(minutes=10)
    assert link.token not in repr(preview)


def test_looking_at_a_link_uses_nothing_up(session, user):
    link = link_for(session)

    for _ in range(3):
        assert preview_email_login(session, str(link.challenge_id), now=NOW) is not None
    session.commit()

    (row,) = challenges(session)
    assert row.consumed_at is None and row.revoked_at is None
    assert redeem(session, link) is not None


def test_the_preview_only_reads(engine, factory, user):
    link = link_for_in(factory)

    with factory() as db, statements(engine) as seen:
        preview_email_login(db, str(link.challenge_id), now=NOW)

    assert [first_word(statement) for statement in seen] == ["SELECT"]


def link_for_in(factory, address=ADDRESS, **options):
    with factory() as db, db.begin():
        result = issue(db, address, **options)
    assert result.outcome is IssueOutcome.ISSUED
    return result.link


@pytest.mark.parametrize(
    "shown",
    [
        "x",
        "",
        None,
        7,
        str(uuid.uuid4()).upper(),  # the links carry the lower-case spelling only
        "{" + str(uuid.uuid4()) + "}",
        "urn:uuid:" + str(uuid.uuid4()),
        uuid.uuid4().hex,  # no hyphens
        str(uuid.uuid4()) + " ",
        str(uuid.uuid4()) + "\n",
        str(uuid.uuid4()),  # well formed, but there is no such link
        uuid.uuid4(),  # an object, not text
    ],
)
def test_the_preview_of_an_unknown_or_malformed_id_is_nothing(session, user, shown):
    link_for(session)

    assert preview_email_login(session, shown, now=NOW) is None


def test_the_preview_ends_exactly_at_the_expiry(session, user):
    link = link_for(session)
    last_moment = NOW + timedelta(minutes=10) - timedelta(microseconds=1)

    assert preview_email_login(session, str(link.challenge_id), now=last_moment) is not None
    assert preview_email_login(session, str(link.challenge_id), now=NOW + timedelta(minutes=10)) is None


@pytest.mark.parametrize("how", ["used", "withdrawn", "identity revoked", "user disabled"])
def test_a_link_that_cannot_be_used_cannot_be_previewed_either(session, user, how):
    link = link_for(session)
    if how == "used":
        assert redeem(session, link) is not None
    elif how == "withdrawn":
        assert withdraw_email_login(session, link.challenge_id, now=NOW)
    elif how == "identity revoked":
        revoke(session, "email", ADDRESS, now=NOW)
    else:
        user.disabled_at = NOW
    session.commit()

    assert preview_email_login(session, str(link.challenge_id), now=NOW) is None


def test_the_preview_asks_the_allowlist_itself_and_does_not_rely_on_the_links_being_withdrawn(session, user):
    # An entry revoked by raw SQL (unsupported, and it withdraws nothing) still stops the page.
    link = link_for(session)
    raw_update(
        session, "allowlist_entries", "provider = ? AND subject = ?", ("email", ADDRESS),
        revoked_at=stored(NOW),
    )
    session.commit()

    assert preview_email_login(session, str(link.challenge_id), now=NOW) is None
    assert challenges(session)[0].revoked_at is None  # (the link itself was not withdrawn)


@pytest.mark.parametrize(
    "address, masked",
    [
        ("elena@example.com", "e***@example.com"),
        ("el@example.com", "***@example.com"),  # too short to show a letter
        ("ele@example.com", "***@example.com"),
        ("elen@example.com", "e***@example.com"),
        ("a@b.co", "***@b.co"),
        ("someone.long+tag@mail.example.co.uk", "s***@mail.example.co.uk"),
    ],
)
def test_the_address_is_masked_but_the_domain_stays(address, masked):
    assert mask_address(address) == masked
    assert address.split("@")[0] not in masked or len(address.split("@")[0]) <= 1


def test_the_id_and_the_secret_have_their_own_shapes():
    assert is_challenge_id(str(uuid.uuid4()))
    assert is_email_login_token("A" * 43) and is_email_login_token("a-_" * 14 + "a")
    assert not is_email_login_token("A" * 42) and not is_email_login_token("A" * 44)
    assert not is_email_login_token("A" * 42 + "=") and not is_email_login_token(None)


# --- redeeming ----------------------------------------------------------------------------


def test_redeeming_signs_the_owner_in_through_the_email_identity(session, user):
    link = link_for(session)

    issued = redeem(session, link, now=NOW + timedelta(minutes=1))
    session.commit()

    assert issued is not None and issued.user_id == user.id
    email_link = session.scalars(sa.select(IdentityLink)).one()
    assert (email_link.user_id, email_link.provider, email_link.subject) == (user.id, "email", ADDRESS)
    assert email_link.last_login_at == NOW + timedelta(minutes=1)
    row = session.get(Session, issued.session_id)
    assert row.identity_link_id == email_link.id  # the provenance is exactly this identity
    assert row.user_id == user.id
    assert row.expires_at == NOW + timedelta(minutes=1) + timedelta(days=14)
    assert authenticate(session, issued.token, now=NOW + timedelta(minutes=1)) is not None
    (used,) = challenges(session)
    assert used.consumed_at == NOW + timedelta(minutes=1)


def test_a_second_sign_in_reuses_the_email_link(session, user):
    first = redeem(session, link_for(session))
    second = redeem(session, link_for(session, now=NOW + timedelta(minutes=2)), now=NOW + timedelta(minutes=2))
    session.commit()

    assert first.token != second.token
    assert count(session, IdentityLink) == 1
    ids = {row.identity_link_id for row in session.scalars(sa.select(Session))}
    assert len(ids) == 1


def test_a_link_is_used_once(session, user):
    link = link_for(session)

    assert redeem(session, link) is not None
    session.commit()

    assert redeem(session, link) is None
    assert redeem(session, link, now=NOW + timedelta(seconds=1)) is None
    session.commit()
    assert count(session, Session) == 1


def test_a_wrong_secret_gets_nothing_and_does_not_burn_the_link(session, user):
    link = link_for(session)

    for guess in ("A" * 43, "-" * 43, link.token[:-1] + ("A" if link.token[-1] != "A" else "B")):
        assert redeem_email_login(session, str(link.challenge_id), guess, now=NOW) is None
    session.commit()

    assert count(session, Session) == 0
    assert redeem(session, link) is not None  # the guesses cost the owner nothing


def test_an_id_and_a_secret_of_two_different_links_open_neither(session, user, other):
    mine = link_for(session, ADDRESS)
    theirs = link_for(session, OTHER_ADDRESS)

    assert redeem_email_login(session, str(mine.challenge_id), theirs.token, now=NOW) is None
    assert redeem_email_login(session, str(theirs.challenge_id), mine.token, now=NOW) is None
    session.commit()

    assert count(session, Session) == 0
    assert redeem(session, mine).user_id == user.id  # and neither was spent
    assert redeem(session, theirs).user_id == other.id


@pytest.mark.parametrize(
    "challenge_id, token",
    [
        (None, "A" * 43),
        ("x", "A" * 43),
        (str(uuid.uuid4()), None),
        (str(uuid.uuid4()), ""),
        (str(uuid.uuid4()), "A" * 42),
        (str(uuid.uuid4()), "A" * 44),
        (str(uuid.uuid4()), "é" * 43),
        (str(uuid.uuid4()), "A" * 43 + "\n"),
        (str(uuid.uuid4()).upper(), "A" * 43),
        (str(uuid.uuid4()), "A" * 43),  # well formed, unknown
    ],
)
def test_what_is_not_a_link_of_ours_signs_nobody_in(session, user, challenge_id, token):
    link_for(session)

    assert redeem_email_login(session, challenge_id, token, now=NOW) is None
    session.commit()
    assert count(session, Session) == 0


def test_a_malformed_id_or_secret_is_not_even_looked_up(engine, factory, user):
    with factory() as db, statements(engine) as seen:
        redeem_email_login(db, "x", "A" * 43, now=NOW)
        redeem_email_login(db, str(uuid.uuid4()), "short", now=NOW)

    assert seen == []


def test_a_link_ends_exactly_at_its_expiry(session, user):
    last_moment = NOW + timedelta(minutes=10) - timedelta(microseconds=1)
    early = link_for(session)
    later = link_for(session, now=NOW + timedelta(minutes=2))

    assert redeem_email_login(
        session, str(early.challenge_id), early.token, now=NOW + timedelta(minutes=10)
    ) is None
    assert redeem_email_login(session, str(later.challenge_id), later.token, now=last_moment) is not None


def test_revoking_the_address_withdraws_its_unused_links_for_good(session, user):
    link = link_for(session)

    assert revoke(session, "email", ADDRESS, now=NOW + timedelta(minutes=1))
    session.commit()
    assert redeem(session, link, now=NOW + timedelta(minutes=2)) is None

    allow(session, user, "email", ADDRESS)  # the address is allowed again...
    session.commit()
    assert redeem(session, link, now=NOW + timedelta(minutes=3)) is None  # ...the old link is not
    fresh = link_for(session, now=NOW + timedelta(minutes=4))  # a new one is
    assert redeem(session, fresh, now=NOW + timedelta(minutes=4)) is not None
    (first, _) = challenges(session)
    assert first.revoked_at == NOW + timedelta(minutes=1) and first.consumed_at is None


def test_revoking_ends_the_email_sessions_and_a_new_link_gives_a_new_session(session, user):
    signed_in = redeem(session, link_for(session))
    session.commit()
    assert authenticate(session, signed_in.token, now=NOW) is not None

    revoke(session, "email", ADDRESS, now=NOW + timedelta(minutes=1))
    session.commit()
    assert authenticate(session, signed_in.token, now=NOW + timedelta(minutes=1)) is None

    allow(session, user, "email", ADDRESS)
    session.commit()
    assert authenticate(session, signed_in.token, now=NOW + timedelta(minutes=2)) is None  # not revived
    again = redeem(session, link_for(session, now=NOW + timedelta(minutes=3)), now=NOW + timedelta(minutes=3))
    assert authenticate(session, again.token, now=NOW + timedelta(minutes=3)) is not None


def test_a_link_presented_after_the_user_was_disabled_is_spent_and_signs_nobody_in(session, user):
    link = link_for(session)
    user.disabled_at = NOW + timedelta(minutes=1)
    session.commit()

    assert redeem(session, link, now=NOW + timedelta(minutes=2)) is None
    session.commit()
    user.disabled_at = None  # the user is enabled again within the link's lifetime...
    session.commit()

    assert redeem(session, link, now=NOW + timedelta(minutes=3)) is None  # ...the link stays spent
    assert count(session, Session) == 0
    (row,) = challenges(session)
    assert row.consumed_at == NOW + timedelta(minutes=2)


def test_a_link_whose_entry_was_revoked_by_raw_sql_is_spent_and_signs_nobody_in(session, user):
    # Revoking by raw SQL is unsupported (it does not withdraw the links), but the
    # redemption does not depend on that: it asks the allowlist itself.
    link = link_for(session)
    raw_update(
        session, "allowlist_entries", "provider = ? AND subject = ?", ("email", ADDRESS),
        revoked_at=stored(NOW),
    )
    session.commit()

    assert redeem(session, link) is None
    session.commit()
    assert count(session, Session) == 0 and challenges(session)[0].consumed_at == NOW


def test_a_browser_signed_in_as_somebody_else_is_not_switched_and_the_link_survives(factory, user, other):
    with factory() as db, db.begin():
        link = issue(db).link

    with factory() as db:
        with pytest.raises(SignInConflict):
            with db.begin():
                redeem(db, link, signed_in_user=other.id)  # the transaction is rolled back

    with factory() as db:
        assert count(db, Session) == 0 and count(db, IdentityLink) == 0
        (row,) = db.scalars(sa.select(EmailLoginChallenge)).all()
        assert row.consumed_at is None  # not used up
    with factory() as db, db.begin():
        assert redeem(db, link) is not None  # after signing out, the same link works


def test_the_browser_of_the_same_user_gets_a_new_session(session, user):
    link = link_for(session)

    issued = redeem(session, link, signed_in_user=user.id)

    assert issued is not None and issued.user_id == user.id


def test_a_link_signs_in_its_own_user_whoever_else_is_on_the_list(session, user, other):
    mine = redeem(session, link_for(session, ADDRESS))
    theirs = redeem(session, link_for(session, OTHER_ADDRESS))
    session.commit()

    assert (mine.user_id, theirs.user_id) == (user.id, other.id)
    links = {(row.user_id, row.subject) for row in session.scalars(sa.select(IdentityLink))}
    assert links == {(user.id, ADDRESS), (other.id, OTHER_ADDRESS)}


def test_a_login_that_resolves_to_another_user_is_refused_and_rolled_back(factory, user, other, monkeypatch):
    with factory() as db, db.begin():
        link = issue(db).link

    foreign = User(id=other.id)  # what a confused allowlist would hand back
    monkeypatch.setattr(email_login, "allowed_user", lambda *args, **kwargs: foreign)
    with factory() as db:
        with db.begin():
            assert redeem(db, link) is None  # the link is spent, nobody is signed in
    with factory() as db:
        assert count(db, Session) == 0
        assert db.scalars(sa.select(EmailLoginChallenge)).one().consumed_at == NOW


def test_the_session_lifetime_is_the_callers(session, user):
    issued = redeem(session, link_for(session), lifetime=timedelta(hours=1))

    assert session.get(Session, issued.session_id).expires_at == NOW + timedelta(hours=1)


def test_telegram_and_email_sign_in_the_same_user_through_their_own_links(session, user):
    allow(session, user, "telegram", TELEGRAM_SUB)
    session.commit()

    by_telegram = complete_login(session, "telegram", TELEGRAM_SUB, now=NOW)
    by_email = redeem(session, link_for(session))
    session.commit()

    assert by_telegram.user_id == by_email.user_id == user.id
    providers = {
        row.id: row.provider for row in session.scalars(sa.select(IdentityLink))
    }
    came_through = {
        session.get(Session, issued.session_id).identity_link_id: issued
        for issued in (by_telegram, by_email)
    }
    assert {providers[link_id] for link_id in came_through} == {"telegram", "email"}


def test_revoking_one_identity_leaves_the_sessions_of_the_other(session, user):
    allow(session, user, "telegram", TELEGRAM_SUB)
    session.commit()
    by_telegram = complete_login(session, "telegram", TELEGRAM_SUB, now=NOW)
    by_email = redeem(session, link_for(session))
    session.commit()

    revoke(session, "email", ADDRESS, now=NOW)
    session.commit()
    assert authenticate(session, by_email.token, now=NOW) is None
    assert authenticate(session, by_telegram.token, now=NOW) is not None

    allow(session, user, "email", ADDRESS)
    revoke(session, "telegram", TELEGRAM_SUB, now=NOW)
    session.commit()
    assert authenticate(session, by_telegram.token, now=NOW) is None
    assert authenticate(session, by_email.token, now=NOW) is None  # revoked, and stays so


def test_no_secret_is_stored_after_a_sign_in(session, user):
    link = link_for(session)
    issued = redeem(session, link)
    session.commit()

    for table in ("email_login_challenges", "sessions", "identity_links", "allowlist_entries", "users"):
        text = row_text(session, table)
        assert link.token not in text and issued.token not in text, table


@pytest.mark.parametrize(
    "action",
    [
        lambda db, link: redeem(db, link),
        lambda db, link: redeem_email_login(db, str(link.challenge_id), "A" * 43, now=NOW),
        lambda db, link: withdraw_email_login(db, link.challenge_id, now=NOW),
    ],
)
def test_redeeming_and_withdrawing_start_with_a_write(engine, factory, user, action):
    link = link_for_in(factory)

    with factory() as db, statements(engine) as seen:
        with db.begin():
            action(db, link)

    assert first_word(seen[0]) == "UPDATE"


# --- concurrency (real threads, a real file) ------------------------------------------------


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
    """Make a redemption linger after its allowlist lookup, so that the others have
    time to commit between its first read and its first write, if it reads first."""
    real = email_login.allowed_user

    def slow(*args, **kwargs):
        found = real(*args, **kwargs)
        time.sleep(0.05)
        return found

    monkeypatch.setattr(email_login, "allowed_user", slow)


def test_of_concurrent_redeemers_of_one_link_exactly_one_wins(factory, user, slow_allowlist_reads):
    link = link_for_in(factory)

    results, errors = run_concurrently(factory, 8, lambda db: redeem(db, link))

    assert errors == []
    assert sum(result is not None for result in results) == 1
    with factory() as db:
        assert count(db, Session) == 1 and count(db, IdentityLink) == 1


def test_concurrent_redemptions_of_different_links_all_succeed_on_one_link_row(
    factory, user, slow_allowlist_reads
):
    links = [link_for_in(factory, now=NOW + timedelta(seconds=61 * n)) for n in range(6)]
    turns = itertools.count()

    results, errors = run_concurrently(
        factory, 6, lambda db: redeem(db, links[next(turns)], now=NOW + timedelta(minutes=6))
    )

    assert errors == []
    assert all(result is not None for result in results)
    with factory() as db:
        assert count(db, Session) == 6 and count(db, IdentityLink) == 1


def test_a_redemption_racing_a_revocation_never_leaves_a_live_session(factory, user):
    live = []
    for round_ in range(12):
        now = NOW + timedelta(minutes=2 * round_)
        with factory() as db, db.begin():
            allow(db, db.get(User, user.id), "email", ADDRESS)
        link = link_for_in(factory, now=now)
        turns = itertools.count()

        def work(db):
            if next(turns) % 2 == 0:
                return redeem(db, link, now=now)
            return revoke(db, "email", ADDRESS, now=now)

        results, errors = run_concurrently(factory, 2, work)

        assert errors == []
        with factory() as db:
            tokens = [r.token for r in results if isinstance(r, authn.IssuedSession)]
            live += [t for t in tokens if authenticate(db, t, now=now) is not None]
            # revoke has committed in either order: nothing it covers is alive
            assert not any(authenticate(db, t, now=now) for t in tokens)
            assert db.scalar(
                sa.select(sa.func.count()).select_from(Session).where(Session.revoked_at.is_(None))
            ) == 0
    assert live == []


# --- withdrawing -----------------------------------------------------------------------------


def test_a_link_that_could_not_be_delivered_can_be_withdrawn(session, user):
    link = link_for(session)

    assert withdraw_email_login(session, link.challenge_id, now=NOW + timedelta(seconds=5)) is True
    assert withdraw_email_login(session, link.challenge_id, now=NOW + timedelta(seconds=9)) is False
    session.commit()

    assert redeem(session, link, now=NOW + timedelta(seconds=10)) is None
    assert preview_email_login(session, str(link.challenge_id), now=NOW) is None
    (row,) = challenges(session)
    assert row.revoked_at == NOW + timedelta(seconds=5)  # the first time is kept


def test_a_used_link_and_an_unknown_one_cannot_be_withdrawn(session, user):
    link = link_for(session)
    assert redeem(session, link) is not None
    session.commit()

    assert withdraw_email_login(session, link.challenge_id, now=NOW) is False
    assert withdraw_email_login(session, uuid.uuid4(), now=NOW) is False
    assert challenges(session)[0].revoked_at is None


# --- revoking an identity reaches exactly its links -------------------------------------------


def test_revoking_withdraws_the_unused_links_of_that_identity_only(session, user, other):
    allow(session, user, "telegram", TELEGRAM_SUB)
    used = link_for(session, ADDRESS)
    assert redeem(session, used) is not None
    unused = link_for(session, ADDRESS, now=NOW + timedelta(minutes=2))
    others = link_for(session, OTHER_ADDRESS)
    session.commit()

    at = NOW + timedelta(minutes=3)
    assert revoke(session, "email", ADDRESS, now=at) is True
    assert revoke(session, "telegram", TELEGRAM_SUB, now=at + timedelta(minutes=1)) is True
    session.commit()

    by_id = {row.id: row for row in challenges(session)}
    assert by_id[unused.challenge_id].revoked_at == at
    assert by_id[used.challenge_id].revoked_at is None  # used up, not withdrawn
    assert by_id[others.challenge_id].revoked_at is None  # another address
    assert redeem(session, others, now=at).user_id == other.id


def test_revoking_twice_keeps_the_first_time_and_the_second_is_false(session, user):
    link = link_for(session)

    assert revoke(session, "email", ADDRESS, now=NOW + timedelta(minutes=1)) is True
    assert revoke(session, "email", ADDRESS, now=NOW + timedelta(minutes=5)) is False
    session.commit()

    (row,) = challenges(session)
    assert row.revoked_at == NOW + timedelta(minutes=1)
    assert link.challenge_id == row.id


def test_revoking_an_entry_that_was_revoked_another_way_still_withdraws_its_links(session, user):
    link = link_for(session)
    raw_update(
        session, "allowlist_entries", "provider = ? AND subject = ?", ("email", ADDRESS),
        revoked_at=stored(NOW),
    )
    session.commit()

    assert revoke(session, "email", ADDRESS, now=NOW + timedelta(minutes=1)) is False  # already revoked
    session.commit()

    assert challenges(session)[0].revoked_at == NOW + timedelta(minutes=1)
    allow(session, user, "email", ADDRESS)
    session.commit()
    assert redeem(session, link, now=NOW + timedelta(minutes=2)) is None


# --- what the database refuses on its own --------------------------------------------------------


def challenge_row(user, **overrides):
    return {
        "id": uuid.uuid4().hex,
        "user_id": user.id.hex,
        "provider": "email",
        "subject": ADDRESS,
        "token_hash": hash_secret("secret"),
        "created_at": stored(NOW),
        "expires_at": stored(NOW + timedelta(minutes=10)),
        **overrides,
    }


def test_a_challenge_can_exist_only_for_an_address_on_the_allowlist(session, user):
    with rejected(session, "FOREIGN KEY"):
        raw_insert(session, "email_login_challenges", **challenge_row(user, subject="nobody@example.com"))


def test_a_challenge_can_exist_only_for_the_user_the_entry_names(session, user, other):
    with rejected(session, "FOREIGN KEY"):
        raw_insert(session, "email_login_challenges", **challenge_row(other))  # ELENA's address, the other user

    with rejected(session, "FOREIGN KEY"):
        session.add(EmailLoginChallenge(
            user_id=other.id, provider="email", subject=ADDRESS, token_hash=hash_secret("x"),
            created_at=NOW, expires_at=NOW + timedelta(minutes=1),
        ))


def test_a_challenge_is_never_for_a_telegram_identity(session, user):
    allow(session, user, "telegram", TELEGRAM_SUB)
    session.commit()

    with rejected(session, "ck_email_login_challenges_provider_is_email"):
        raw_insert(
            session, "email_login_challenges",
            **challenge_row(user, provider="telegram", subject=TELEGRAM_SUB),
        )


def test_the_digest_is_unique_and_sixty_four_characters(session, user):
    raw_insert(session, "email_login_challenges", **challenge_row(user))

    with rejected(session, "email_login_challenges.token_hash"):
        raw_insert(session, "email_login_challenges", **challenge_row(user))
    with rejected(session, "ck_email_login_challenges_token_hash_length"):
        raw_insert(session, "email_login_challenges", **challenge_row(user, token_hash="short"))


def test_a_challenge_expires_after_it_was_created(session, user):
    with rejected(session, "ck_email_login_challenges_expires_after_created"):
        raw_insert(
            session, "email_login_challenges", **challenge_row(user, expires_at=stored(NOW))
        )


def test_deleting_the_entry_or_the_user_deletes_the_links(session, user, other):
    link_for(session, ADDRESS)
    link_for(session, OTHER_ADDRESS)

    session.execute(sa.delete(AllowlistEntry).where(AllowlistEntry.subject == ADDRESS))
    session.commit()
    assert [row.subject for row in challenges(session)] == [OTHER_ADDRESS]

    session.execute(sa.delete(User).where(User.id == other.id))
    session.commit()
    assert challenges(session) == []


def test_the_binding_of_a_stored_challenge_cannot_be_rewritten_through_the_orm(session, user, other):
    link = link_for(session)
    row = session.get(EmailLoginChallenge, link.challenge_id)

    for attribute, value in (
        ("token_hash", hash_secret("another")),
        ("subject", OTHER_ADDRESS),
        ("user_id", other.id),
        ("expires_at", NOW + timedelta(days=1)),
        ("created_at", NOW - timedelta(days=1)),
    ):
        setattr(row, attribute, value)
        with pytest.raises(ValueError, match="cannot change once stored"):
            session.flush()
        session.rollback()
        row = session.get(EmailLoginChallenge, link.challenge_id)

    row.consumed_at = NOW  # the two that do change
    row.revoked_at = NOW
    session.flush()


def test_an_address_that_is_not_in_normalized_form_cannot_be_stored_through_the_orm(session, user):
    session.add(EmailLoginChallenge(
        user_id=user.id, provider="email", subject="elena@EXAMPLE.com", token_hash=hash_secret("x"),
        created_at=NOW, expires_at=NOW + timedelta(minutes=1),
    ))

    with pytest.raises(ValueError, match="normalized"):
        session.flush()
