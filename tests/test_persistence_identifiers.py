"""Identifiers that come from outside: the application's rules and the database's.

SQLite does not enforce the width of a VARCHAR, and a string column takes any
bytes, so the length and character-set rules of idempotency keys and identity
subjects are CHECK constraints too. The risk of having both is that they drift:
the application accepts what the database then refuses (a user-visible error
instead of a clean one), or the other way round (the database waves through what
the application would have refused). So the same values go through both, and the
verdicts must be identical.

Every value is written as plain SQL for the database's verdict, so the
application's own checks (the ORM listeners) are not what refuses it. The ORM
path is tested separately, at the end.
"""

import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from house_helper.persistence import identifiers
from house_helper.persistence.identifiers import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_SUBJECT_LENGTH,
    is_printable_ascii,
    normalize_identity,
    validate_idempotency_key,
)
from house_helper.persistence.models import AllowlistEntry, TurnRequest
from house_helper.persistence.sqlchecks import PrintableAscii
from tests.persistence_support import (  # noqa: F401  (fixtures)
    conversation_row,
    count,
    digest,
    engine,
    entry_row,
    factory,
    link_row,
    new_conversation,
    new_message,
    new_user,
    new_turn_request,
    raw_insert,
    rejected,
    session,
    stored,
    turn_row,
    user_row,
    utc,
)

CREATED = utc(2026, 10, 16, 12)


def accepted_by_database(session, write) -> bool:
    """Run `write`, a plain-SQL insert, and report whether the database took it.
    Nothing is kept either way."""
    savepoint = session.begin_nested()
    try:
        write()
    except sa.exc.IntegrityError:
        savepoint.rollback()
        return False
    savepoint.rollback()
    return True


def python_accepts(check, *args) -> bool:
    try:
        check(*args)
    except ValueError:
        return False
    return True


@pytest.fixture
def user(session):
    return new_user(session)


@pytest.fixture
def conversation(session, user):
    return new_conversation(session, user)


# --- idempotency keys -------------------------------------------------------------


def _key_is_in_the_stored_form(key) -> bool:
    return python_accepts(validate_idempotency_key, key)


def _database_takes_key(session, conversation, asked, key) -> bool:
    return accepted_by_database(
        session,
        lambda: raw_insert(
            session, "turn_requests", **turn_row(conversation, asked.id, idempotency_key=key)
        ),
    )


KEYS = {
    "one character": "a",
    "a uuid": "3f2b8c1e-0d4a-4e55-9b0c-7a1d2e3f4a5b",
    "punctuation": "!~#$%&'()*+,-./:;<=>?@[\\]^_`{|}",
    "the longest": "k" * MAX_IDEMPOTENCY_KEY_LENGTH,
    "one too long": "k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1),
    "empty": "",
    "a space": " ",
    "padded": " key",
    "with a space": "a b",
    "cyrillic": "ключ",
    "latin-1 letter": "café",
    "zero-width space": "a​b",
    "no-break space": "a b",
    "line separator": "a b",
    "full-width letter": "ａ",
    "emoji": "a\U0001f372",
    "combining accent": "é",
}


@pytest.mark.parametrize("name", KEYS)
def test_keys_get_the_same_verdict_from_the_application_and_the_database(
    session, conversation, name
):
    asked = new_message(session, conversation)
    key = KEYS[name]

    assert _database_takes_key(session, conversation, asked, key) is _key_is_in_the_stored_form(key)


def test_the_corpus_of_keys_has_both_verdicts():
    verdicts = {_key_is_in_the_stored_form(key) for key in KEYS.values()}

    assert verdicts == {True, False}  # a corpus that only passed or only failed proves nothing


def test_every_character_up_to_latin_1_gets_the_same_verdict_inside_a_key(session, conversation):
    # NUL, the other control characters and DEL, the space, every printable ASCII
    # character, and the Latin-1 supplement, each between two letters.
    asked = new_message(session, conversation)
    disagreements, accepted = [], []
    for code in range(256):
        key = f"a{chr(code)}b"
        by_database = _database_takes_key(session, conversation, asked, key)
        if by_database is not _key_is_in_the_stored_form(key):
            disagreements.append(code)
        if by_database:
            accepted.append(code)

    assert disagreements == []
    assert accepted == list(range(0x21, 0x7F))  # exactly "!" to "~"


def test_a_key_made_of_every_printable_character_is_accepted_by_both(session, conversation):
    asked = new_message(session, conversation)
    key = "".join(chr(code) for code in range(0x21, 0x7F))

    assert _key_is_in_the_stored_form(key)
    assert _database_takes_key(session, conversation, asked, key)


@pytest.mark.parametrize("length", [1, 127, 128])
def test_keys_up_to_the_declared_width_are_accepted(session, conversation, length):
    asked = new_message(session, conversation)

    assert _database_takes_key(session, conversation, asked, "k" * length)


@pytest.mark.parametrize("length", [0, 129, 1000])
def test_keys_beyond_the_declared_width_are_refused_by_the_database(session, conversation, length):
    asked = new_message(session, conversation)
    row = turn_row(conversation, asked.id, idempotency_key="k" * length)

    with rejected(session, "ck_turn_requests_idempotency_key_length"):
        raw_insert(session, "turn_requests", **row)


@pytest.mark.parametrize("key", [None, 5, b"key", ["key"]])
def test_a_key_that_is_not_text_is_refused_by_the_application(key):
    with pytest.raises(ValueError, match="must be text"):
        validate_idempotency_key(key)


def test_a_trailing_newline_does_not_slip_through(session, conversation):
    # `$` in a regular expression would let "key\n" through; fullmatch does not.
    asked = new_message(session, conversation)

    assert not _key_is_in_the_stored_form("key\n")
    assert not _database_takes_key(session, conversation, asked, "key\n")


def test_the_orm_refuses_a_bad_key_before_anything_is_sent(session, conversation):
    with pytest.raises(ValueError):
        new_turn_request(session, conversation, key="bad key")


# --- identity subjects ------------------------------------------------------------


def _identity_is_in_the_stored_form(provider, subject) -> bool:
    try:
        return normalize_identity(provider, subject) == (provider, subject)
    except ValueError:
        return False


def _database_takes_identity(session, user, provider, subject) -> bool:
    return accepted_by_database(
        session,
        lambda: raw_insert(
            session, "allowlist_entries", **entry_row(user, provider=provider, subject=subject)
        ),
    )


@pytest.mark.parametrize(
    "provider, template",
    [("telegram", "4{}2"), ("email", "a{}b@example.com"), ("email", "a@exam{}ple.com")],
    ids=["telegram", "email local part", "email domain"],
)
def test_every_character_up_to_latin_1_gets_the_same_verdict_inside_a_subject(
    session, user, provider, template
):
    disagreements, accepted = [], []
    for code in range(256):
        subject = template.format(chr(code))
        by_database = _database_takes_identity(session, user, provider, subject)
        if by_database is not _identity_is_in_the_stored_form(provider, subject):
            disagreements.append(code)
        if by_database:
            accepted.append(code)

    assert disagreements == []
    # What is left is printable ASCII, minus what each provider rules out ("@" in a
    # Telegram subject; in an email, "@" as the extra one and every capital letter).
    expected = [c for c in range(0x21, 0x7F) if chr(c) != "@"]
    if provider == "email":
        expected = [c for c in expected if not chr(c).isupper()]
    assert accepted == expected


SUBJECTS = {
    "telegram: documented example": ("telegram", "1234123412341234123"),
    "telegram: letters and symbols": ("telegram", "abc_DEF-123.~!"),
    "telegram: the longest": ("telegram", "9" * MAX_SUBJECT_LENGTH),
    "telegram: one too long": ("telegram", "9" * (MAX_SUBJECT_LENGTH + 1)),
    "telegram: empty": ("telegram", ""),
    "telegram: a handle": ("telegram", "@name"),
    "telegram: an address": ("telegram", "a@b.c"),
    "telegram: padded": ("telegram", " 42"),
    "telegram: arabic-indic digits": ("telegram", "٤٢"),
    "telegram: full-width digits": ("telegram", "４２"),
    "telegram: cyrillic": ("telegram", "аб"),
    "email: plain": ("email", "cook@example.com"),
    "email: with an alias": ("email", "cook+tag@example.com"),
    "email: dots and digits": ("email", "c.o.o.k1@sub.example.co.uk"),
    "email: upper case": ("email", "Cook@example.com"),
    "email: all upper case": ("email", "COOK@EXAMPLE.COM"),
    "email: the longest": ("email", "a" * (MAX_SUBJECT_LENGTH - len("@b.c")) + "@b.c"),
    "email: one too long": ("email", "a" * (MAX_SUBJECT_LENGTH - len("@b.c") + 1) + "@b.c"),
    "email: no at sign": ("email", "cook.example.com"),
    "email: two at signs": ("email", "a@b@example.com"),
    "email: nothing before the at sign": ("email", "@example.com"),
    "email: nothing after the at sign": ("email", "cook@"),
    "email: only an at sign": ("email", "@"),
    "email: kelvin sign": ("email", "cooK@example.com"),
    "email: cyrillic look-alike": ("email", "сook@example.com"),
    "email: internationalized": ("email", "ünal@bücher.example"),
    "email: full-width at sign": ("email", "cook＠example.com"),
    "unknown provider": ("google", "42"),
    "empty provider": ("", "42"),
    "provider in the wrong case": ("Telegram", "42"),
}


@pytest.mark.parametrize("name", SUBJECTS)
def test_subjects_get_the_same_verdict_from_the_application_and_the_database(session, user, name):
    provider, subject = SUBJECTS[name]

    assert _database_takes_identity(session, user, provider, subject) is _identity_is_in_the_stored_form(
        provider, subject
    )


def test_the_corpus_of_subjects_has_both_verdicts():
    verdicts = {_identity_is_in_the_stored_form(p, s) for p, s in SUBJECTS.values()}

    assert verdicts == {True, False}


@pytest.mark.parametrize("subject", ["4\x002", "4\x00", "\x00", "a\x00@example.com"])
def test_a_nul_inside_a_subject_is_caught_although_sqlite_text_functions_stop_at_it(session, user, subject):
    # length() and GLOB see only what precedes the NUL; the BLOB length does not.
    provider = "email" if "@" in subject else "telegram"

    assert not _identity_is_in_the_stored_form(provider, subject)
    assert not _database_takes_identity(session, user, provider, subject)


def test_a_nul_inside_a_key_is_caught_too(session, conversation):
    asked = new_message(session, conversation)

    assert not _database_takes_key(session, conversation, asked, "ke\x00y")


# --- the storage class is part of the value: a BLOB is never an identifier -----------
#
# SQLite stores whatever it is given, in whatever class it arrives. GLOB and length()
# read a BLOB as text, so the BLOB x'6b6579' passed every character rule as "key"
# does, and, being a different value to a UNIQUE constraint, could sit beside it.
# The CHECKs now also require typeof() = 'text'. Python's bytes is what SQLite
# stores as a BLOB, so these rows are plain SQL with bytes parameters.


def _printable_ascii_rule(connection, value):
    """The rule on its own, the very text the CHECKs are built from: 1, 0, or None
    (NULL, which a CHECK lets through)."""
    fragment = str(PrintableAscii(":value").compile(dialect=sqlite.dialect()))
    return connection.execute(f"SELECT ({fragment})", {"value": value}).fetchone()[0]


def test_the_printable_ascii_rule_refuses_every_blob_however_it_reads_as_text():
    connection = sqlite3.connect(":memory:")
    try:
        accepted = [
            code
            for code in range(256)
            if _printable_ascii_rule(connection, b"a" + bytes([code]) + b"b") != 0
        ]
        whole_values = [b"key", b"!~", b"x" * 128, b"x" * 129, b"", b"\x00", b"a\x00b", b"k\x00"]
        accepted_whole = [v for v in whole_values if _printable_ascii_rule(connection, v) != 0]

        assert accepted == []  # not even the bytes that are printable ASCII
        assert accepted_whole == []
        # The same characters as TEXT are fine, so it is the class, not the content.
        assert _printable_ascii_rule(connection, "key") == 1
        assert _printable_ascii_rule(connection, "!~") == 1
        # NULL passes, as it always did: identity_links.email may be empty.
        assert _printable_ascii_rule(connection, None) is None
    finally:
        connection.close()


def test_the_printable_ascii_rule_agrees_with_python_on_text_with_nul_and_controls():
    connection = sqlite3.connect(":memory:")
    try:
        values = [f"a{chr(code)}b" for code in range(0x80)] + ["a\x00", "\x00a", "a\x00b\x00c", "ke\x00y"]
        disagreements = [
            v for v in values if bool(_printable_ascii_rule(connection, v)) is not is_printable_ascii(v)
        ]
    finally:
        connection.close()

    assert disagreements == []


BLOB_KEYS_REFUSED_ONLY_BY_THE_CHARACTER_RULE = [
    b"key-1", b"ke\x00y", b"\x00", b"\x00" * 5, b"k" * 128, b"a b", b"ke\ty", b"ke\x7fy",
    b"caf\xc3\xa9", b"\xff",
]
BLOB_KEYS_ALSO_THE_WRONG_LENGTH = [b"", b"k" * 129, b"k" * 1000]


def _turn_row_with_key(conversation, asked, key):
    row = turn_row(conversation, asked.id)  # (the helper fingerprints a str key only)
    row["idempotency_key"] = key
    return row


@pytest.mark.parametrize("key", BLOB_KEYS_REFUSED_ONLY_BY_THE_CHARACTER_RULE, ids=repr)
def test_a_blob_idempotency_key_is_refused_by_the_character_rule(session, conversation, key):
    asked = new_message(session, conversation)

    with rejected(session, "ck_turn_requests_idempotency_key_printable_ascii"):
        raw_insert(session, "turn_requests", **_turn_row_with_key(conversation, asked, key))


@pytest.mark.parametrize("key", BLOB_KEYS_ALSO_THE_WRONG_LENGTH, ids=lambda key: f"{len(key)} bytes")
def test_a_blob_idempotency_key_of_the_wrong_length_is_refused(session, conversation, key):
    asked = new_message(session, conversation)

    with rejected(session, "CHECK constraint failed"):
        raw_insert(session, "turn_requests", **_turn_row_with_key(conversation, asked, key))


@pytest.mark.parametrize(
    "key", BLOB_KEYS_REFUSED_ONLY_BY_THE_CHARACTER_RULE + BLOB_KEYS_ALSO_THE_WRONG_LENGTH, ids=repr
)
def test_the_application_refuses_the_bytes_the_database_refuses_as_a_blob(key):
    with pytest.raises(ValueError, match="must be text"):
        validate_idempotency_key(key)


def test_a_blob_key_cannot_sit_beside_the_text_key_it_spells(session, conversation):
    # The point of it all: x'6b6579' and "key" are two values to UNIQUE, so before
    # this a client could hold the same key twice in one conversation.
    first, second = new_message(session, conversation), new_message(session, conversation)
    raw_insert(session, "turn_requests", **_turn_row_with_key(conversation, first, "key-1"))

    with rejected(session, "ck_turn_requests_idempotency_key_printable_ascii"):
        raw_insert(session, "turn_requests", **_turn_row_with_key(conversation, second, b"key-1"))

    assert count(session, TurnRequest) == 1


def test_the_same_key_as_text_is_taken_so_it_is_the_class_that_decides(session, conversation):
    asked = new_message(session, conversation)

    assert _database_takes_key(session, conversation, asked, "key-1")
    assert not accepted_by_database(
        session,
        lambda: raw_insert(session, "turn_requests", **_turn_row_with_key(conversation, asked, b"key-1")),
    )


BLOB_SUBJECTS = [
    ("telegram", b"4242"),
    ("telegram", b"42\x0042"),
    ("telegram", b"\x00"),
    ("telegram", b"42\x0a"),
    ("telegram", b"9" * 255),
    ("telegram", b"9" * 256),
    ("telegram", b""),
    ("telegram", b"@name"),
    ("email", b"cook@example.com"),
    ("email", b"a\x00@example.com"),
    ("email", b"cook@example.com\x00"),
    ("email", b"Cook@Example.com"),
    ("email", b"cook"),
    ("email", b"a" * 252 + b"@b.c"),
    ("email", b""),
]


@pytest.mark.parametrize("provider, subject", BLOB_SUBJECTS, ids=repr)
def test_a_blob_subject_is_refused_on_the_allowlist(session, user, provider, subject):
    with rejected(session, "CHECK constraint failed"):
        raw_insert(session, "allowlist_entries", **entry_row(user, provider=provider, subject=subject))


@pytest.mark.parametrize("provider, subject", BLOB_SUBJECTS, ids=repr)
def test_a_blob_subject_is_refused_on_an_identity_link_before_anything_else(
    session, user, provider, subject
):
    with rejected(session, "CHECK constraint failed"):
        raw_insert(session, "identity_links", **link_row(user, provider=provider, subject=subject))


@pytest.mark.parametrize("provider, subject", BLOB_SUBJECTS, ids=repr)
def test_the_application_refuses_a_subject_given_as_bytes(provider, subject):
    with pytest.raises(ValueError, match="must be text"):
        normalize_identity(provider, subject)


def test_a_blob_subject_cannot_sit_beside_the_text_subject_it_spells(session, user):
    raw_insert(session, "allowlist_entries", **entry_row(user, subject="4242"))

    with rejected(session, "CHECK constraint failed"):
        raw_insert(session, "allowlist_entries", **entry_row(user, subject=b"4242"))

    assert session.scalar(sa.select(sa.func.count()).select_from(AllowlistEntry)) == 1


@pytest.mark.parametrize("table, build", [("allowlist_entries", entry_row), ("identity_links", link_row)])
@pytest.mark.parametrize("provider", [b"telegram", b"email", b"google", b""], ids=repr)
def test_a_blob_provider_is_refused(session, user, table, build, provider):
    with rejected(session, f"ck_{table}_provider_valid"):
        raw_insert(session, table, **build(user, provider=provider, subject="4242"))
    with pytest.raises(ValueError, match="unknown identity provider"):
        normalize_identity(provider, "4242")


@pytest.mark.parametrize(
    "email", [b"cook@example.com", b"a\x00b@example.com", b"", b"\x00", b"x" * 320], ids=repr
)
def test_a_blob_email_on_an_identity_link_is_refused(session, user, email):
    raw_insert(session, "allowlist_entries", **entry_row(user, subject="4242"))

    with rejected(session, "ck_identity_links_email_printable_ascii"):
        raw_insert(session, "identity_links", **link_row(user, subject="4242", email=email))


@pytest.mark.parametrize("email", ["cook@example.com", None], ids=repr)
def test_a_text_email_and_no_email_at_all_are_still_taken(session, user, email):
    raw_insert(session, "allowlist_entries", **entry_row(user, subject="4242"))

    raw_insert(session, "identity_links", **link_row(user, subject="4242", email=email))

    assert session.scalar(sa.text("SELECT count(*) FROM identity_links")) == 1


@pytest.mark.parametrize("subject", ["4\x002", "\x00", "42\x00", "4\x1f2", "4\x7f2", "4\t2", "4\n2"])
def test_a_nul_or_a_control_character_in_a_text_subject_is_refused_on_an_identity_link(
    session, user, subject
):
    with rejected(session, "CHECK constraint failed"):
        raw_insert(session, "identity_links", **link_row(user, subject=subject))


@pytest.mark.parametrize("email", ["a\x00b@example.com", "a\tb@example.com", "a b@example.com", "é@example.com"])
def test_a_nul_or_a_control_character_in_a_text_email_is_refused(session, user, email):
    raw_insert(session, "allowlist_entries", **entry_row(user, subject="4242"))

    with rejected(session, "ck_identity_links_email_printable_ascii"):
        raw_insert(session, "identity_links", **link_row(user, subject="4242", email=email))


@pytest.mark.parametrize("subject", ["", "9" * 256])
def test_a_text_subject_of_the_wrong_length_is_refused_on_an_identity_link(session, user, subject):
    with rejected(session, "CHECK constraint failed"):
        raw_insert(session, "identity_links", **link_row(user, subject=subject))


# --- normalization is what makes an email comparable ------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Cook@Example.COM", "cook@example.com"),
        ("  cook@example.com\n", "cook@example.com"),
        ("\tCOOK+Tag@Example.com \r\n", "cook+tag@example.com"),
    ],
)
def test_an_email_is_trimmed_and_lower_cased_into_its_stored_form(session, user, raw, expected):
    provider, subject = normalize_identity("email", raw)

    assert (provider, subject) == ("email", expected)
    assert _identity_is_in_the_stored_form(provider, subject)
    assert _database_takes_identity(session, user, provider, subject)


def test_the_kelvin_sign_is_refused_before_lower_casing_not_after():
    # "K".lower() == "k": checking for ASCII after lower-casing would turn
    # an attacker's mailbox into the allowlisted one.
    assert "cooK@example.com".lower() == "cook@example.com"

    with pytest.raises(ValueError, match="printable ASCII"):
        normalize_identity("email", "cooK@example.com")


def test_only_ascii_whitespace_is_trimmed_from_an_email():
    with pytest.raises(ValueError):
        normalize_identity("email", " cook@example.com")
    with pytest.raises(ValueError):
        normalize_identity("email", "cook@example.com ")


def test_a_telegram_subject_is_never_trimmed_or_folded():
    assert normalize_identity("telegram", "AbC") == ("telegram", "AbC")  # case is kept
    with pytest.raises(ValueError):
        normalize_identity("telegram", "AbC ")


def test_is_printable_ascii():
    assert is_printable_ascii("!~09azAZ")
    assert not any(is_printable_ascii(x) for x in ["", " ", "a b", "\n", "é", None, 5, b"a"])


# --- every bounded text column has a limit the database enforces -------------------

# (table, column, limit, a function that builds a valid row with that column overridden)


def _oidc_row(**overrides):
    return {
        "id": user_row()["id"],
        "state_hash": digest("state"),
        "provider": "telegram",
        "nonce": "n",
        "code_verifier": "v",
        "created_at": stored(CREATED),
        "expires_at": stored(CREATED + timedelta(minutes=10)),
        **overrides,
    }


WIDTHS = [
    ("users", "display_name", 200, lambda ctx, v: user_row(display_name=v)),
    ("conversations", "title", 200, lambda ctx, v: conversation_row(ctx.user, title=v)),
    ("allowlist_entries", "note", 500, lambda ctx, v: entry_row(ctx.user, note=v)),
    ("oidc_states", "provider", 64, lambda ctx, v: _oidc_row(provider=v)),
    ("oidc_states", "nonce", 128, lambda ctx, v: _oidc_row(nonce=v)),
    ("oidc_states", "code_verifier", 128, lambda ctx, v: _oidc_row(code_verifier=v)),
    (
        "turn_requests", "error_code", 64,
        lambda ctx, v: turn_row(ctx.conversation, ctx.asked.id, error_code=v),
    ),
]


@pytest.fixture
def ctx(session, user, conversation):
    return SimpleNamespace(user=user, conversation=conversation, asked=new_message(session, conversation))


@pytest.mark.parametrize("table, column, limit, build", WIDTHS, ids=[f"{t}.{c}" for t, c, *_ in WIDTHS])
def test_a_text_column_takes_exactly_the_width_it_declares(session, ctx, table, column, limit, build):
    raw_insert(session, table, **build(ctx, "x" * limit))
    session.rollback()

    with rejected(session, f"ck_{table}_{column}_max_length"):
        raw_insert(session, table, **build(ctx, "x" * (limit + 1)))


@pytest.mark.parametrize("table, column, limit, build", WIDTHS, ids=[f"{t}.{c}" for t, c, *_ in WIDTHS])
def test_the_declared_width_is_the_width_of_the_check(table, column, limit, build):
    from house_helper.persistence.base import Base

    declared = Base.metadata.tables[table].c[column].type.length

    assert declared == limit


def test_the_application_constants_are_the_widths_of_the_columns():
    allowlist_subject = AllowlistEntry.__table__.c.subject.type.length
    key = TurnRequest.__table__.c.idempotency_key.type.length

    assert allowlist_subject == MAX_SUBJECT_LENGTH == identifiers.MAX_SUBJECT_LENGTH
    assert key == MAX_IDEMPOTENCY_KEY_LENGTH
