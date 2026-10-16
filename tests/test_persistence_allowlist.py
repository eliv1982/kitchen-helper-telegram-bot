"""The allowlist: one provisioned user, two ways in, uniqueness, revocation, fail-closed.

The product has one initial user, who can sign in with Telegram OIDC (primary) or
with a verified email (backup); each is an entry that belongs to that user. The
schema tests run against both a schema created from the models and one built by
the migrations (tests/persistence_support.py). Nothing here implements OIDC or
sends mail: it is the table and the functions the sign-in increment will call.

Rows the database must refuse are written as plain SQL, so that the application's
own validation does not get there first; the application-side checks have their
own tests (ValueError before anything reaches the database).
"""

import pytest
import sqlalchemy as sa

from house_helper.persistence.allowlist import (
    MAX_SUBJECT_LENGTH,
    allow,
    allowed_user,
    is_allowed,
    normalize,
    revoke,
)
from house_helper.persistence.models import AllowlistEntry, AllowlistProvider
from tests.persistence_support import (  # noqa: F401  (fixtures)
    count,
    engine,
    entry_row,
    factory,
    new_user,
    raw_insert,
    rejected,
    session,
    utc,
)

NOW = utc(2026, 10, 16, 12)


@pytest.fixture
def user(session):
    return new_user(session, display_name="Elena")


# --- the table: what the database itself refuses ----------------------------------


@pytest.mark.parametrize(
    "provider, subject",
    [("telegram", "123456789"), ("email", "cook@example.com")],
)
def test_an_identity_can_be_allowlisted_only_once(session, user, provider, subject):
    raw_insert(session, "allowlist_entries", **entry_row(user, provider=provider, subject=subject))

    with rejected(session, "allowlist_entries.provider, allowlist_entries.subject"):
        raw_insert(
            session, "allowlist_entries", **entry_row(user, provider=provider, subject=subject)
        )


def test_an_identity_belongs_to_one_user_even_if_the_second_asks(session, user):
    other = new_user(session)
    raw_insert(session, "allowlist_entries", **entry_row(user, subject="42"))

    with rejected(session, "allowlist_entries.provider, allowlist_entries.subject"):
        raw_insert(session, "allowlist_entries", **entry_row(other, subject="42"))


def test_a_telegram_subject_and_an_email_are_separate_entries(session, user):
    raw_insert(session, "allowlist_entries", **entry_row(user, provider="telegram", subject="42"))
    raw_insert(
        session,
        "allowlist_entries",
        **entry_row(user, provider="email", subject="cook@example.com"),
    )

    assert count(session, AllowlistEntry) == 2


def test_an_entry_needs_a_user_that_exists(session):
    from uuid import uuid4

    with rejected(session, "FOREIGN KEY"):
        raw_insert(
            session,
            "allowlist_entries",
            id=uuid4().hex, user_id=uuid4().hex, provider="telegram", subject="42",
            created_at="2026-10-16 12:00:00.000000",
        )


def test_an_entry_cannot_be_ownerless(session, user):
    row = entry_row(user)
    row["user_id"] = None

    with rejected(session, "NOT NULL constraint failed: allowlist_entries.user_id"):
        raw_insert(session, "allowlist_entries", **row)


@pytest.mark.parametrize("provider", ["google", "", "Telegram", "EMAIL", "phone"])
def test_only_the_known_providers_are_accepted(session, user, provider):
    with rejected(session, "ck_allowlist_entries_provider_valid"):
        raw_insert(session, "allowlist_entries", **entry_row(user, provider=provider))


def test_an_empty_subject_is_rejected(session, user):
    with rejected(session, "ck_allowlist_entries_subject_length"):
        raw_insert(session, "allowlist_entries", **entry_row(user, subject=""))


@pytest.mark.parametrize("subject", [" 42", "42 ", " ", "4 2", "42\n", "4\t2"])
def test_a_subject_cannot_contain_whitespace(session, user, subject):
    with rejected(session, "ck_allowlist_entries_subject_printable_ascii"):
        raw_insert(session, "allowlist_entries", **entry_row(user, subject=subject))


@pytest.mark.parametrize("subject", ["Cook@Example.com", "cook.example.com", "COOK@EXAMPLE.COM"])
def test_a_stored_email_must_be_lower_case_and_contain_an_at_sign(session, user, subject):
    with rejected(session, "ck_allowlist_entries_email_subject_normalized"):
        raw_insert(
            session, "allowlist_entries", **entry_row(user, provider="email", subject=subject)
        )


@pytest.mark.parametrize("subject", ["@example.com", "cook@", "a@b@example.com", "@@"])
def test_a_stored_email_has_one_at_sign_with_something_on_both_sides(session, user, subject):
    with rejected(session, "ck_allowlist_entries_email_subject_normalized"):
        raw_insert(
            session, "allowlist_entries", **entry_row(user, provider="email", subject=subject)
        )


@pytest.mark.parametrize("subject", ["@cook", "cook@example.com"])
def test_a_telegram_subject_cannot_be_an_email_or_a_handle(session, user, subject):
    with rejected(session, "ck_allowlist_entries_telegram_subject_not_email"):
        raw_insert(session, "allowlist_entries", **entry_row(user, subject=subject))


def test_an_entry_needs_a_provider_and_a_subject(session, user):
    for column in ("provider", "subject"):
        row = entry_row(user)
        row[column] = None
        with rejected(session, f"NOT NULL constraint failed: allowlist_entries.{column}"):
            raw_insert(session, "allowlist_entries", **row)


def test_revoking_an_entry_keeps_the_row(session, user):
    entry = allow(session, user, "telegram", "42")

    entry.revoked_at = NOW
    session.flush()
    session.expire_all()

    assert session.scalar(sa.select(AllowlistEntry)).revoked_at == NOW


# --- normalization ----------------------------------------------------------------


@pytest.mark.parametrize(
    "provider, subject, expected",
    [
        ("email", "cook@example.com", ("email", "cook@example.com")),
        ("email", "  Cook@Example.COM \n", ("email", "cook@example.com")),
        (AllowlistProvider.EMAIL, "Cook@Example.com", ("email", "cook@example.com")),
        ("email", "cook+tag@example.com", ("email", "cook+tag@example.com")),  # an alias stays itself
        ("telegram", "123456789", ("telegram", "123456789")),
        ("telegram", "1234123412341234123", ("telegram", "1234123412341234123")),  # Telegram's example `sub`
        (AllowlistProvider.TELEGRAM, "123456789", ("telegram", "123456789")),
    ],
)
def test_normalize_gives_the_stored_form(provider, subject, expected):
    assert normalize(provider, subject) == expected


def test_a_normalized_value_is_accepted_by_the_database(session, user):
    # normalize and the CHECKs must agree: whatever normalize returns is storable.
    for provider, subject in [("email", "  Cook@Example.COM "), ("telegram", "42")]:
        provider, subject = normalize(provider, subject)
        raw_insert(session, "allowlist_entries", **entry_row(user, provider=provider, subject=subject))

    assert count(session, AllowlistEntry) == 2


@pytest.mark.parametrize(
    "provider, subject",
    [
        ("google", "42"),  # unknown provider
        ("", "42"),
        (None, "42"),
        (42, "42"),
        (["email"], "cook@example.com"),
        ("telegram", None),  # not text
        ("telegram", 42),
        ("telegram", b"42"),
        ("telegram", ""),
        ("telegram", " "),
        ("telegram", " 42"),  # a telegram subject is compared exactly, never trimmed
        ("telegram", "42 "),
        ("telegram", "4 2"),
        ("telegram", "42\n"),
        ("telegram", "4\x002"),
        ("telegram", "4​2"),  # zero-width space
        ("telegram", "4 2"),  # no-break space
        ("telegram", "４２"),  # full-width digits
        ("telegram", "@username"),
        ("telegram", "cook@example.com"),
        ("telegram", "9" * (MAX_SUBJECT_LENGTH + 1)),
        ("email", ""),
        ("email", "   "),
        ("email", "cook"),
        ("email", "@example.com"),
        ("email", "cook@"),
        ("email", "a@b@example.com"),
        ("email", "co ok@example.com"),
        ("email", "cook@exa\tmple.com"),
        ("email", "cook@example.com\x00"),
        ("email", "a" * MAX_SUBJECT_LENGTH + "@example.com"),
        ("email", "Ünal@Bücher.example"),  # internationalized: refused, not folded (identifiers.py)
        ("email", "cook@example.com "),  # only ASCII whitespace is trimmed, so this is refused
        ("email", "cooK@example.com"),  # the Kelvin sign: str.lower() would make it "k"
        ("email", "cook＠example.com"),  # full-width "@"
    ],
)
def test_normalize_refuses_what_cannot_be_an_entry(provider, subject):
    with pytest.raises(ValueError):
        normalize(provider, subject)


def test_the_longest_accepted_subject_fits_the_column():
    subject = "9" * MAX_SUBJECT_LENGTH

    assert normalize("telegram", subject) == ("telegram", subject)


# --- is_allowed / allowed_user: fail closed ---------------------------------------


def test_nobody_is_allowed_on_an_empty_allowlist(session):
    assert not is_allowed(session, "telegram", "42")
    assert not is_allowed(session, "email", "cook@example.com")
    assert allowed_user(session, "telegram", "42") is None


def test_a_telegram_subject_is_allowed_only_exactly(session, user):
    allow(session, user, "telegram", "42")

    assert is_allowed(session, "telegram", "42")
    assert not is_allowed(session, "telegram", "420")
    assert not is_allowed(session, "telegram", "4")
    assert not is_allowed(session, "telegram", " 42")


def test_an_email_is_allowed_whatever_its_case_or_padding(session, user):
    allow(session, user, "email", "Cook@Example.com")

    assert is_allowed(session, "email", "cook@example.com")
    assert is_allowed(session, "email", " COOK@EXAMPLE.COM ")


def test_an_alias_of_an_allowed_email_is_not_allowed(session, user):
    allow(session, user, "email", "cook@example.com")

    assert not is_allowed(session, "email", "cook+anything@example.com")
    assert not is_allowed(session, "email", "c.ook@example.com")
    assert not is_allowed(session, "email", "cook@mail.example.com")


def test_a_look_alike_of_an_allowed_email_is_not_allowed(session, user):
    allow(session, user, "email", "cook@example.com")

    assert not is_allowed(session, "email", "cooK@example.com")  # Kelvin sign
    assert not is_allowed(session, "email", "соок@example.com")  # Cyrillic letters
    assert not is_allowed(session, "email", "cook@example.com​")  # zero-width space


def test_an_entry_for_one_provider_does_not_allow_another(session, user):
    allow(session, user, "telegram", "42")
    allow(session, user, "email", "cook@example.com")

    assert not is_allowed(session, "email", "42")
    assert not is_allowed(session, "telegram", "cook@example.com")


def test_a_revoked_entry_is_not_allowed(session, user):
    allow(session, user, "telegram", "42")
    allow(session, user, "email", "cook@example.com")

    assert revoke(session, "telegram", "42", now=NOW)
    assert revoke(session, "email", "Cook@Example.com", now=NOW)

    assert not is_allowed(session, "telegram", "42")
    assert not is_allowed(session, "email", "cook@example.com")


def test_a_revocation_dated_in_the_future_still_refuses(session, user):
    # Any revoked_at at all closes the door; there is no "revoked from" semantics.
    allow(session, user, "telegram", "42")
    entry = session.scalar(sa.select(AllowlistEntry))
    entry.revoked_at = utc(2999, 1, 1)
    session.flush()

    assert not is_allowed(session, "telegram", "42")


@pytest.mark.parametrize(
    "provider, subject",
    [("google", "42"), (None, None), ("telegram", None), ("telegram", 42), ("email", ""),
     ("email", "not-an-address"), ("telegram", "42\n"), (object(), object())],
)
def test_malformed_input_is_refused_without_raising(session, user, provider, subject):
    allow(session, user, "telegram", "42")  # something is on the list; none of this matches it

    assert is_allowed(session, provider, subject) is False
    assert allowed_user(session, provider, subject) is None


def test_the_answer_is_a_real_boolean(session, user):
    allow(session, user, "telegram", "42")

    assert is_allowed(session, "telegram", "42") is True
    assert is_allowed(session, "telegram", "43") is False


# --- one user, two ways in --------------------------------------------------------


def test_both_ways_in_resolve_to_the_same_user(session, user):
    allow(session, user, "telegram", "1234123412341234123", note="primary")
    allow(session, user, "email", "Elena@Example.com", note="backup")

    by_telegram = allowed_user(session, "telegram", "1234123412341234123")
    by_email = allowed_user(session, "email", "elena@example.com")

    assert by_telegram is not None and by_telegram.id == user.id
    assert by_email is not None and by_email.id == user.id


def test_each_way_in_is_revoked_on_its_own(session, user):
    allow(session, user, "telegram", "42")
    allow(session, user, "email", "elena@example.com")

    revoke(session, "telegram", "42", now=NOW)

    assert allowed_user(session, "telegram", "42") is None
    assert allowed_user(session, "email", "elena@example.com").id == user.id


def test_the_entries_of_a_user_are_reachable_from_the_user(session, user):
    allow(session, user, "telegram", "42")
    allow(session, user, "email", "elena@example.com")
    session.refresh(user)

    assert sorted(e.provider for e in user.allowlist_entries) == ["email", "telegram"]


def test_a_disabled_user_cannot_sign_in_whatever_the_entry_says(session, user):
    allow(session, user, "telegram", "42")
    user.disabled_at = NOW
    session.flush()

    assert allowed_user(session, "telegram", "42") is None
    assert not is_allowed(session, "telegram", "42")

    user.disabled_at = None
    session.flush()
    assert is_allowed(session, "telegram", "42")


def test_deleting_the_user_deletes_their_entries(session, user):
    allow(session, user, "telegram", "42")
    allow(session, user, "email", "elena@example.com")

    session.execute(sa.delete(type(user)))

    assert count(session, AllowlistEntry) == 0
    assert not is_allowed(session, "telegram", "42")


# --- allow ------------------------------------------------------------------------


def test_allow_stores_the_normalized_pair_note_and_owner(session, user):
    entry = allow(session, user, AllowlistProvider.EMAIL, " Cook@Example.com ", note="Elena, backup login")

    assert (entry.provider, entry.subject, entry.note) == (
        "email", "cook@example.com", "Elena, backup login",
    )
    assert entry.user_id == user.id
    assert entry.revoked_at is None and entry.created_at is not None


def test_allow_accepts_a_user_that_has_not_been_flushed_yet(session):
    from house_helper.persistence.models import User

    pending = User()
    session.add(pending)

    entry = allow(session, pending, "telegram", "42")

    assert entry.user_id == pending.id is not None


def test_allowing_twice_keeps_one_row(session, user):
    first = allow(session, user, "email", "cook@example.com", note="first")
    second = allow(session, user, "email", "COOK@example.com")

    assert second.id == first.id
    assert second.note == "first"  # no note given: the old one stays
    assert count(session, AllowlistEntry) == 1


def test_allowing_with_a_note_replaces_the_note(session, user):
    allow(session, user, "telegram", "42", note="old")
    allow(session, user, "telegram", "42", note="new")

    assert session.scalar(sa.select(AllowlistEntry.note)) == "new"


def test_allowing_someone_revoked_lifts_the_revocation_on_the_same_row(session, user):
    original = allow(session, user, "telegram", "42")
    revoke(session, "telegram", "42", now=NOW)

    again = allow(session, user, "telegram", "42")

    assert again.id == original.id
    assert again.revoked_at is None
    assert is_allowed(session, "telegram", "42")
    assert count(session, AllowlistEntry) == 1


def test_an_identity_on_the_list_for_one_user_is_not_moved_to_another(session, user):
    other = new_user(session)
    allow(session, user, "telegram", "42")

    with pytest.raises(ValueError, match="another user"):
        allow(session, other, "telegram", "42")

    assert allowed_user(session, "telegram", "42").id == user.id
    assert count(session, AllowlistEntry) == 1


def test_a_revoked_identity_is_not_handed_to_another_user_either(session, user):
    other = new_user(session)
    allow(session, user, "telegram", "42")
    revoke(session, "telegram", "42", now=NOW)

    with pytest.raises(ValueError, match="another user"):
        allow(session, other, "telegram", "42")

    assert allowed_user(session, "telegram", "42") is None


@pytest.mark.parametrize(
    "provider, subject", [("google", "42"), ("telegram", "@name"), ("email", "nope"), ("telegram", 42)]
)
def test_allowing_something_invalid_raises_and_stores_nothing(session, user, provider, subject):
    with pytest.raises(ValueError):
        allow(session, user, provider, subject)

    assert count(session, AllowlistEntry) == 0


# --- the ORM refuses what normalize refuses, before the database sees it -----------


@pytest.mark.parametrize(
    "provider, subject",
    [("telegram", "4\x002"), ("telegram", " 42"), ("email", "Cook@Example.com"), ("email", "nope")],
)
def test_an_entry_built_by_hand_must_already_be_normalized(session, user, provider, subject):
    session.add(AllowlistEntry(user_id=user.id, provider=provider, subject=subject))

    with pytest.raises(ValueError):
        session.flush()


# --- revoke -----------------------------------------------------------------------


def test_revoke_sets_the_time_and_keeps_the_row(session, user):
    allow(session, user, "telegram", "42")

    assert revoke(session, "telegram", "42", now=NOW) is True

    entry = session.scalar(sa.select(AllowlistEntry))
    assert entry.revoked_at == NOW
    assert count(session, AllowlistEntry) == 1


def test_revoke_without_a_time_uses_the_clock(session, user):
    allow(session, user, "telegram", "42")

    revoke(session, "telegram", "42")

    assert session.scalar(sa.select(AllowlistEntry.revoked_at)) is not None


def test_revoking_twice_keeps_the_first_time(session, user):
    allow(session, user, "telegram", "42")
    revoke(session, "telegram", "42", now=NOW)

    assert revoke(session, "telegram", "42", now=utc(2027, 1, 1)) is False
    assert session.scalar(sa.select(AllowlistEntry.revoked_at)) == NOW


def test_revoking_someone_not_on_the_list_does_nothing(session):
    assert revoke(session, "telegram", "42", now=NOW) is False
    assert count(session, AllowlistEntry) == 0


def test_revoking_something_invalid_raises(session):
    with pytest.raises(ValueError):
        revoke(session, "telegram", "@name")


def test_revoking_one_entry_leaves_the_others(session, user):
    allow(session, user, "telegram", "42")
    allow(session, user, "telegram", "43")
    allow(session, user, "email", "cook@example.com")

    revoke(session, "telegram", "42", now=NOW)

    assert not is_allowed(session, "telegram", "42")
    assert is_allowed(session, "telegram", "43")
    assert is_allowed(session, "email", "cook@example.com")


# --- it survives the transaction --------------------------------------------------


def test_entries_and_revocations_are_durable(factory):
    with factory() as s:
        owner = new_user(s)
        allow(s, owner, "telegram", "42")
        allow(s, owner, "email", "Cook@Example.com")
        s.commit()
    with factory() as s:
        assert revoke(s, "telegram", "42", now=NOW)
        s.commit()

    with factory() as s:
        assert not is_allowed(s, "telegram", "42")
        assert is_allowed(s, "email", "cook@example.com")
