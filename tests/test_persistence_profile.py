"""The profile's persistence: its two settings and the identities that have signed in.

The same tests run against a schema built from the models and one built by the
migrations (tests/persistence_support.py). Nothing leaves the process.
"""

import uuid

import pytest
import sqlalchemy as sa

from house_helper.persistence import profile
from house_helper.persistence.allowlist import revoke
from house_helper.persistence.models import TextSize, User
from house_helper.persistence.profile import (
    LOGIN_ACTIVE,
    LOGIN_REVOKED,
    InvalidDisplayName,
    InvalidTextSize,
    ProfileNotFound,
    get_profile,
    normalize_display_name,
    normalize_text_size,
    update_profile,
)
from tests.persistence_support import (  # noqa: F401  (fixtures)
    engine,
    factory,
    new_identity_link,
    new_user,
    session,
    utc,
)

NOW = utc(2026, 11, 9, 10)


# --- validation ---------------------------------------------------------------------


def test_the_profile_name_limit_is_the_display_name_columns_width():
    assert profile.MAX_DISPLAY_NAME_LENGTH == User.__table__.c.display_name.type.length == 200


def test_a_name_is_trimmed_and_bounded_at_two_hundred_code_points():
    assert normalize_display_name("  Елена  ") == "Елена"
    assert normalize_display_name("Елена Кухня") == "Елена Кухня"
    assert normalize_display_name("😀" * 200) == "😀" * 200  # 200 code points, 400 UTF-16 units
    assert normalize_display_name("Наталья́") == "Наталья́"  # a combining mark is text
    with pytest.raises(InvalidDisplayName):
        normalize_display_name("😀" * 201)
    with pytest.raises(InvalidDisplayName):
        normalize_display_name("x" * 201)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "\n\t",
        "a\nb",
        "a\tb",
        "a\x00b",
        "a\x07b",
        "a\u007fb",
        "a\u0085b",
        "a​b",  # zero-width space (Cf)
        "a‎b",  # left-to-right mark (Cf)
        "a‮b",  # right-to-left override (Cf)
        "a﻿b",  # byte order mark (Cf)
        "a\ud800b",  # lone surrogate (Cs)
        "ab",  # private use (Co)
        "a͸b",  # unassigned (Cn)
    ],
)
def test_a_name_with_an_empty_or_invisible_or_control_character_is_refused(value):
    with pytest.raises(InvalidDisplayName):
        normalize_display_name(value)


@pytest.mark.parametrize("value", ["normal", "large"])
def test_a_text_size_is_one_of_the_two_values(value):
    assert normalize_text_size(value) == value


@pytest.mark.parametrize("value", ["Normal", "huge", "", " normal", None, 1, True, ["normal"]])
def test_any_other_text_size_is_refused(value):
    with pytest.raises(InvalidTextSize):
        normalize_text_size(value)


# --- reading and writing -------------------------------------------------------------


def test_a_new_user_has_the_normal_text_size(session):
    user = new_user(session, display_name="Елена")

    assert session.refresh(user) is None
    assert user.text_size == TextSize.NORMAL.value
    assert user.display_name == "Елена"


def test_the_profile_is_the_users_own_name_size_and_sign_in_methods(session):
    elena = new_user(session, display_name="Елена")
    other = new_user(session, display_name="Другой")
    new_identity_link(session, elena, "telegram", "42", created_at=utc(2026, 11, 9, 8))
    new_identity_link(
        session, elena, "email", "elena@example.com", created_at=utc(2026, 11, 9, 9)
    )
    new_identity_link(session, other, "telegram", "77", created_at=utc(2026, 11, 9, 10))

    mine = get_profile(session, elena.id)
    theirs = get_profile(session, other.id)

    assert (mine.display_name, mine.text_size) == ("Елена", "normal")
    assert [(method.provider, method.address, method.status) for method in mine.login_methods] == [
        ("telegram", None, LOGIN_ACTIVE),
        ("email", "elena@example.com", LOGIN_ACTIVE),
    ]
    assert [(method.provider, method.address) for method in theirs.login_methods] == [
        ("telegram", None)
    ]
    assert "42" not in repr(mine.login_methods)  # the Telegram subject is not part of the answer


def test_a_revoked_identity_is_reported_as_revoked_and_its_link_and_address_stay(session):
    elena = new_user(session, display_name="Елена")
    new_identity_link(
        session, elena, "email", "elena@example.com", created_at=utc(2026, 11, 9, 8)
    )
    new_identity_link(session, elena, "telegram", "42", created_at=utc(2026, 11, 9, 9))
    revoke(session, "telegram", "42", now=utc(2026, 11, 9, 12))

    methods = get_profile(session, elena.id).login_methods

    assert [(method.provider, method.status) for method in methods] == [
        ("email", LOGIN_ACTIVE),
        ("telegram", LOGIN_REVOKED),
    ]
    assert methods[0].address == "elena@example.com"
    assert methods[1].address is None


def test_a_change_is_written_trimmed_and_both_fields_persist(session):
    elena = new_user(session, display_name="Елена")

    changed = update_profile(
        session, elena.id, display_name="  Иван  ", text_size="large", now=NOW
    )

    assert (changed.display_name, changed.text_size) == ("Иван", "large")
    assert get_profile(session, elena.id) == changed
    assert session.get(User, elena.id).display_name == "Иван"
    assert session.get(User, elena.id).text_size == "large"
    assert session.get(User, elena.id).updated_at == NOW


def test_a_change_that_names_one_field_leaves_the_other_as_it_was(session):
    elena = new_user(session, display_name="Елена")
    update_profile(session, elena.id, text_size="large", now=NOW)

    renamed = update_profile(session, elena.id, display_name="Иван", now=NOW)
    resized = update_profile(session, elena.id, text_size="normal", now=NOW)

    assert renamed.text_size == "large" and resized.display_name == "Иван"
    assert get_profile(session, elena.id).text_size == "normal"


def test_duplicate_names_are_allowed_and_never_link_two_accounts(session):
    first = new_user(session, display_name="Елена")
    second = new_user(session, display_name="Другой")

    update_profile(session, first.id, display_name="Одинаковое", now=NOW)
    update_profile(session, second.id, display_name="Одинаковое", now=NOW)

    assert get_profile(session, first.id).display_name == "Одинаковое"
    assert get_profile(session, second.id).display_name == "Одинаковое"
    assert first.id != second.id


def test_an_empty_change_is_a_programming_error_not_a_write(session):
    elena = new_user(session, display_name="Елена")

    with pytest.raises(ValueError):
        update_profile(session, elena.id, now=NOW)


def test_a_user_that_does_not_exist_is_never_guessed(session):
    with pytest.raises(ProfileNotFound):
        get_profile(session, uuid.uuid4())
    with pytest.raises(ProfileNotFound):
        update_profile(session, uuid.uuid4(), display_name="Иван", now=NOW)


def test_an_invalid_value_is_refused_before_the_write(session):
    elena = new_user(session, display_name="Елена")

    with pytest.raises(InvalidDisplayName):
        update_profile(session, elena.id, display_name="​", now=NOW)
    with pytest.raises(InvalidTextSize):
        update_profile(session, elena.id, text_size="huge", now=NOW)

    assert get_profile(session, elena.id).display_name == "Елена"
    assert get_profile(session, elena.id).text_size == "normal"


def test_the_database_refuses_a_text_size_outside_the_closed_list(session):
    elena = new_user(session, display_name="Елена")

    with pytest.raises(sa.exc.IntegrityError, match="ck_users_text_size_valid"):
        with session.begin_nested():
            session.execute(
                sa.text("UPDATE users SET text_size = 'huge' WHERE id = :id"),
                {"id": elena.id.hex},
            )
