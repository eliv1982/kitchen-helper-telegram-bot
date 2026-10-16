"""One provisioned user, two ways in, and what an identity link may be.

The independent audit found the allowlist and the identity links unrelated: the
backup email would not resolve to the user the Telegram login does, and a link
could name any provider and subject. The rules pinned here:

  * a link exists only for an identity that was provisioned on the allowlist, for
    the very user the link names (a composite foreign key), so nothing is ever
    linked by a name, a display name or an unverified claim;
  * allowlist entries and identity links obey the same provider/subject rules;
  * revoking takes access away and keeps the rows.

Every test runs against a schema created from the models and one built by the
migrations. Rows the database must refuse are plain SQL, so the application's own
checks do not get there first.
"""

import uuid
import warnings

import pytest
import sqlalchemy as sa

from house_helper.persistence.allowlist import allow, allowed_user, is_allowed, revoke
from house_helper.persistence.models import AllowlistEntry, IdentityLink, User
from tests.persistence_support import (  # noqa: F401  (fixtures)
    count,
    engine,
    entry_row,
    factory,
    link_row,
    new_identity_link,
    new_user,
    raw_insert,
    rejected,
    session,
    utc,
)

NOW = utc(2026, 10, 16, 12)
TELEGRAM_SUB = "1234123412341234123"  # the shape of the example in Telegram's documentation


@pytest.fixture
def user(session):
    return new_user(session, display_name="Elena")


@pytest.fixture
def provisioned(session, user):
    """The single initial user with both ways in."""
    allow(session, user, "telegram", TELEGRAM_SUB, note="primary")
    allow(session, user, "email", "elena@example.com", note="backup")
    return user


# --- a link needs a provisioned identity of the same user -------------------------


def test_a_provisioned_identity_can_be_linked_to_its_user(session, provisioned):
    raw_insert(session, "identity_links", **link_row(provisioned, subject=TELEGRAM_SUB))
    raw_insert(
        session,
        "identity_links",
        **link_row(provisioned, provider="email", subject="elena@example.com"),
    )

    assert count(session, IdentityLink) == 2


def test_both_logins_link_to_the_one_user(session, provisioned):
    new_identity_link(session, provisioned, "telegram", TELEGRAM_SUB)
    new_identity_link(session, provisioned, "email", "elena@example.com")
    session.refresh(provisioned)

    assert sorted(link.provider for link in provisioned.identity_links) == ["email", "telegram"]
    assert {link.user_id for link in provisioned.identity_links} == {provisioned.id}
    assert count(session, User) == 1


def test_an_identity_that_was_never_provisioned_cannot_be_linked(session, provisioned):
    with rejected(session, "FOREIGN KEY"):
        raw_insert(session, "identity_links", **link_row(provisioned, subject="999"))


def test_an_identity_provisioned_for_another_user_cannot_be_linked_to_this_one(session, provisioned):
    intruder = new_user(session, display_name="Elena")  # even the same display name

    with rejected(session, "FOREIGN KEY"):
        raw_insert(session, "identity_links", **link_row(intruder, subject=TELEGRAM_SUB))


def test_a_link_cannot_borrow_the_entry_of_another_provider(session, provisioned):
    # The email is provisioned; a Telegram link to the same string is not the
    # same identity (and is not even a valid Telegram subject, so use a number
    # that is provisioned only for the other provider).
    allow(session, provisioned, "email", "42@example.com")

    with rejected(session, "FOREIGN KEY"):
        raw_insert(session, "identity_links", **link_row(provisioned, subject="42"))


def test_the_orm_cannot_link_an_identity_that_is_not_on_the_list_either(session, provisioned):
    with rejected(session, "FOREIGN KEY"):
        session.add(IdentityLink(user_id=provisioned.id, provider="telegram", subject="999"))


def test_provisioning_an_identity_does_not_link_it(session, provisioned):
    # Linking is the sign-in increment's job, on a successful login; allow()
    # never creates a link on its own, and nothing matches on a name.
    assert count(session, AllowlistEntry) == 2
    assert count(session, IdentityLink) == 0


def test_an_identity_belongs_to_one_user_in_both_tables(session, provisioned):
    other = new_user(session)
    new_identity_link(session, provisioned, "telegram", TELEGRAM_SUB)

    with pytest.raises(ValueError, match="another user"):
        allow(session, other, "telegram", TELEGRAM_SUB)
    with rejected(session, "allowlist_entries.provider, allowlist_entries.subject"):
        raw_insert(session, "allowlist_entries", **entry_row(other, subject=TELEGRAM_SUB))
    with rejected(session, "identity_links.provider, identity_links.subject"):
        raw_insert(session, "identity_links", **link_row(other, subject=TELEGRAM_SUB))


# --- revocation and deletion -------------------------------------------------------


def test_revoking_the_entry_refuses_the_identity_and_keeps_both_rows(session, provisioned):
    new_identity_link(session, provisioned, "telegram", TELEGRAM_SUB)

    assert revoke(session, "telegram", TELEGRAM_SUB, now=NOW)

    assert not is_allowed(session, "telegram", TELEGRAM_SUB)
    assert allowed_user(session, "telegram", TELEGRAM_SUB) is None
    assert count(session, AllowlistEntry) == 2 and count(session, IdentityLink) == 1
    # the other way in is unaffected
    assert allowed_user(session, "email", "elena@example.com").id == provisioned.id


def test_lifting_the_revocation_restores_access_through_the_same_link(session, provisioned):
    new_identity_link(session, provisioned, "telegram", TELEGRAM_SUB)
    revoke(session, "telegram", TELEGRAM_SUB, now=NOW)

    allow(session, provisioned, "telegram", TELEGRAM_SUB)

    assert allowed_user(session, "telegram", TELEGRAM_SUB).id == provisioned.id
    assert count(session, IdentityLink) == 1


def test_deleting_an_entry_deletes_the_link_that_depended_on_it(session, provisioned):
    new_identity_link(session, provisioned, "telegram", TELEGRAM_SUB)

    session.execute(sa.delete(AllowlistEntry).where(AllowlistEntry.provider == "telegram"))

    assert count(session, IdentityLink) == 0
    assert count(session, AllowlistEntry) == 1


def test_deleting_the_user_deletes_entries_and_links(session, provisioned):
    new_identity_link(session, provisioned, "telegram", TELEGRAM_SUB)
    new_identity_link(session, provisioned, "email", "elena@example.com")

    session.execute(sa.delete(User))

    assert count(session, AllowlistEntry) == 0 and count(session, IdentityLink) == 0


def test_deleting_the_user_through_the_orm_deletes_entries_and_links(session, provisioned):
    new_identity_link(session, provisioned, "telegram", TELEGRAM_SUB)
    session.commit()

    session.delete(provisioned)
    session.commit()

    assert count(session, AllowlistEntry) == 0 and count(session, IdentityLink) == 0


def test_the_orm_deletes_a_user_whose_entries_and_links_are_in_memory(session, provisioned):
    # Entries and links are deleted by the database's cascade, in whatever order
    # it likes; the ORM must not also try to delete the loaded rows (the second
    # DELETE would match nothing, and SQLAlchemy only warns about that).
    new_identity_link(session, provisioned, "telegram", TELEGRAM_SUB)
    new_identity_link(session, provisioned, "email", "elena@example.com")
    session.commit()
    assert len(provisioned.allowlist_entries) == 2 and len(provisioned.identity_links) == 2

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        session.delete(provisioned)
        session.commit()

    session.expire_all()
    assert count(session, AllowlistEntry) == 0 and count(session, IdentityLink) == 0
    assert count(session, User) == 0


def test_an_entry_that_has_a_link_cannot_be_re_keyed_or_handed_to_another_user(session, provisioned):
    other = new_user(session)
    new_identity_link(session, provisioned, "telegram", TELEGRAM_SUB)
    entry_id = session.scalar(
        sa.select(AllowlistEntry.id).where(AllowlistEntry.provider == "telegram")
    ).hex

    for column, value in (("subject", "7"), ("user_id", other.id.hex)):
        with rejected(session, "FOREIGN KEY"):
            session.connection().exec_driver_sql(
                f"UPDATE allowlist_entries SET {column} = ? WHERE id = ?", (value, entry_id)
            )


# --- the same rules for the two tables ---------------------------------------------

# (provider, subject, constraint that refuses it). Each value fails exactly one
# CHECK, so the named one is what the test proves.
REFUSED = [
    ("google", "42", "provider_valid"),
    ("", "42", "provider_valid"),
    ("Telegram", "42", "provider_valid"),
    ("telegram", "", "subject_length"),
    ("telegram", "9" * 256, "subject_length"),
    ("telegram", " 42", "subject_printable_ascii"),
    ("telegram", "4 2", "subject_printable_ascii"),
    ("telegram", "42\n", "subject_printable_ascii"),
    ("telegram", "4\x002", "subject_printable_ascii"),
    ("telegram", "4\x1f2", "subject_printable_ascii"),
    ("telegram", "4\x7f2", "subject_printable_ascii"),
    ("telegram", "4​2", "subject_printable_ascii"),
    ("telegram", "٤٢", "subject_printable_ascii"),  # Arabic-Indic digits
    ("telegram", "@name", "telegram_subject_not_email"),
    ("telegram", "a@b.c", "telegram_subject_not_email"),
    ("email", "Cook@example.com", "email_subject_normalized"),
    ("email", "cook", "email_subject_normalized"),
    ("email", "@example.com", "email_subject_normalized"),
    ("email", "cook@", "email_subject_normalized"),
    ("email", "a@b@c.d", "email_subject_normalized"),
]


@pytest.mark.parametrize("table", ["allowlist_entries", "identity_links"])
@pytest.mark.parametrize("provider, subject, constraint", REFUSED)
def test_both_tables_refuse_the_same_malformed_identities(
    session, user, table, provider, subject, constraint
):
    row = entry_row(user) if table == "allowlist_entries" else link_row(user)
    row.update(provider=provider, subject=subject)

    with rejected(session, f"ck_{table}_{constraint}"):
        raw_insert(session, table, **row)


@pytest.mark.parametrize("table", ["allowlist_entries", "identity_links"])
@pytest.mark.parametrize(
    "provider, subject",
    [("telegram", TELEGRAM_SUB), ("telegram", "~!"), ("email", "elena@example.com"), ("email", "a+b@c.d")],
)
def test_both_tables_accept_the_same_well_formed_identities(session, user, table, provider, subject):
    # An entry must exist for the link to point at.
    raw_insert(session, "allowlist_entries", **entry_row(user, provider=provider, subject=subject))
    if table == "identity_links":
        raw_insert(session, table, **link_row(user, provider=provider, subject=subject))

    assert count(session, AllowlistEntry if table == "allowlist_entries" else IdentityLink) == 1


@pytest.mark.parametrize(
    "provider, subject",
    [("telegram", "4\x002"), ("telegram", " 42"), ("email", "Cook@Example.com"), ("google", "x")],
)
def test_an_identity_link_built_by_hand_must_already_be_normalized(session, provisioned, provider, subject):
    session.add(IdentityLink(user_id=provisioned.id, provider=provider, subject=subject))

    with pytest.raises(ValueError):
        session.flush()


# --- the email snapshot on a link ---------------------------------------------------


def test_the_email_snapshot_is_optional_and_stored_as_given(session, provisioned):
    link = new_identity_link(session, provisioned, "email", "elena@example.com", email="elena@example.com")
    other = new_identity_link(session, provisioned, "telegram", TELEGRAM_SUB)

    assert (link.email, other.email) == ("elena@example.com", None)


@pytest.mark.parametrize("email", ["a b@example.com", "a\nb@example.com", "a\x00b", "а@example.com", ""])
def test_the_email_snapshot_is_printable_ascii(session, provisioned, email):
    with rejected(session, "ck_identity_links_email_printable_ascii"):
        raw_insert(session, "identity_links", **link_row(provisioned, subject=TELEGRAM_SUB, email=email))


def test_the_email_snapshot_has_the_width_the_column_declares(session, provisioned):
    ok = "a" * 320
    raw_insert(session, "identity_links", **link_row(provisioned, subject=TELEGRAM_SUB, email=ok))

    with rejected(session, "ck_identity_links_email_max_length"):
        raw_insert(
            session, "identity_links",
            **link_row(provisioned, provider="email", subject="elena@example.com", email="a" * 321),
        )


# --- it survives the transaction ---------------------------------------------------


def test_the_provisioning_is_durable(factory):
    with factory() as s:
        owner = new_user(s)
        allow(s, owner, "telegram", TELEGRAM_SUB)
        allow(s, owner, "email", "Elena@Example.com")
        new_identity_link(s, owner, "email", "elena@example.com", email="elena@example.com")
        owner_id = owner.id
        s.commit()

    with factory() as s:
        assert allowed_user(s, "telegram", TELEGRAM_SUB).id == owner_id
        assert allowed_user(s, "email", "ELENA@example.com").id == owner_id
        assert s.scalar(sa.select(IdentityLink.user_id)) == owner_id
        assert uuid.UUID(int=0) != owner_id
