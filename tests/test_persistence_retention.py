"""365-day retention: the default, the ceiling, and the UTC timestamps under them.

The ceiling is enforced twice, and each half has its own tests. On the ORM write
path a row that would outlive RETENTION_DAYS is refused with a ValueError before
it is sent. In the database a CHECK refuses it for any other writer: those tests
write plain SQL, so nothing but the constraint stands in the way. The CHECK is
different SQL on SQLite (text arithmetic on the stored UTC text) and PostgreSQL
(interval arithmetic); only the SQLite half can run here, and the PostgreSQL text
is compared with the migration's in test_persistence_migrations.py.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from house_helper.persistence.models import (
    MESSAGE_RETENTION,
    MESSAGE_RETENTION_DAYS,
    Message,
    TurnRequest,
)
from house_helper.persistence.sqlchecks import canonical_utc_text_sqlite
from tests.persistence_support import (  # noqa: F401  (fixtures)
    engine,
    factory,
    message_row,
    new_conversation,
    new_message,
    new_turn_request,
    new_user,
    raw_insert,
    raw_update,
    rejected,
    session,
    stored,
    turn_row,
    utc,
)


@pytest.fixture
def conversation(session):
    return new_conversation(session, new_user(session))


# --- the policy -------------------------------------------------------------------


def test_the_retention_period_is_365_days():
    assert MESSAGE_RETENTION_DAYS == 365
    assert MESSAGE_RETENTION == timedelta(days=365)


# --- messages ---------------------------------------------------------------------


def test_a_message_expires_365_days_after_it_was_created(session, conversation):
    message = new_message(session, conversation)

    assert message.expires_at - message.created_at == timedelta(days=365)


@pytest.mark.parametrize("role", ["user", "assistant", "system_note"])
def test_every_role_keeps_the_365_day_retention_including_the_reset_note(session, conversation, role):
    message = new_message(session, conversation, role=role, created_at=utc(2026, 2, 3))

    assert message.expires_at == utc(2026, 2, 3) + MESSAGE_RETENTION


def test_expiry_follows_a_created_at_that_is_given_not_the_clock(session, conversation):
    message = new_message(session, conversation, created_at=utc(2025, 3, 1, 8, 30))

    assert message.expires_at == utc(2026, 3, 1, 8, 30)


def test_a_year_is_365_days_even_across_a_leap_day(session, conversation):
    message = new_message(session, conversation, created_at=utc(2024, 1, 1))

    assert message.expires_at == utc(2024, 12, 31)  # 2024 has 366 days


def test_every_message_in_a_batch_gets_its_own_deadline(session, conversation):
    days = [utc(2026, 1, 1), utc(2026, 2, 1), utc(2026, 3, 1)]
    session.add_all(
        Message(conversation_id=conversation.id, role="user", content="x", created_at=day)
        for day in days
    )
    session.flush()

    stored = session.scalars(sa.select(Message).order_by(Message.id)).all()

    assert [m.expires_at - m.created_at for m in stored] == [MESSAGE_RETENTION] * 3
    assert [m.expires_at for m in stored] == [day + MESSAGE_RETENTION for day in days]


def test_an_explicit_expiry_is_kept(session, conversation):
    message = new_message(
        session, conversation, created_at=utc(2026, 1, 1), expires_at=utc(2026, 1, 2)
    )

    assert message.expires_at == utc(2026, 1, 2)


def test_the_stored_deadline_survives_a_round_trip(factory):
    with factory() as s:
        message = new_message(s, new_conversation(s, new_user(s)), created_at=utc(2026, 5, 5, 5))
        message_id = message.id
        s.commit()

    with factory() as s:
        loaded = s.get(Message, message_id)

        assert loaded.expires_at == utc(2027, 5, 5, 5)
        assert loaded.expires_at.tzinfo is not None


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
def test_a_message_cannot_expire_at_or_before_its_creation(session, conversation, delta):
    created = utc(2026, 1, 1)

    with rejected(session, "ck_messages_expires_after_created"):
        raw_insert(
            session, "messages",
            **message_row(conversation, created_at=stored(created), expires_at=stored(created + delta)),
        )
    with pytest.raises(ValueError, match="after created_at"):
        new_message(session, conversation, created_at=created, expires_at=created + delta)


def test_the_messages_due_for_purging_are_found_by_their_deadline(session, conversation):
    now = utc(2026, 10, 16, 12)
    expired = new_message(session, conversation, content="old", created_at=now - timedelta(days=400))
    due_now = new_message(
        session, conversation, content="exactly due",
        created_at=now - MESSAGE_RETENTION,
    )
    new_message(session, conversation, content="recent", created_at=now - timedelta(days=364))

    due = session.scalars(
        sa.select(Message).where(Message.expires_at <= now).order_by(Message.id)
    ).all()

    assert [m.id for m in due] == [expired.id, due_now.id]


# --- turn requests hold a copy of the answer, so they expire too ------------------


def test_a_turn_request_expires_365_days_after_it_was_created(session, conversation):
    turn = new_turn_request(session, conversation, created_at=utc(2026, 4, 1))

    assert turn.expires_at == utc(2027, 4, 1)


def test_a_turn_request_cannot_expire_at_or_before_its_creation(session, conversation):
    created = utc(2026, 1, 1)
    asked = new_message(session, conversation)

    with rejected(session, "ck_turn_requests_expires_after_created"):
        raw_insert(
            session, "turn_requests",
            **turn_row(conversation, asked.id, created_at=stored(created), expires_at=stored(created)),
        )
    with pytest.raises(ValueError, match="after created_at"):
        new_turn_request(session, conversation, created_at=created, expires_at=created)


# --- the ceiling: nothing is kept more than 365 days after it was created -----------

CREATED = utc(2026, 10, 16, 12)
CEILING = CREATED + MESSAGE_RETENTION
MICROSECOND = timedelta(microseconds=1)


def _message(session, conversation, **fields):
    return new_message(session, conversation, **fields)


def _turn(session, conversation, **fields):
    return new_turn_request(session, conversation, **fields)


BUILDERS = pytest.mark.parametrize("build", [_message, _turn], ids=["message", "turn request"])


@BUILDERS
@pytest.mark.parametrize(
    "lifetime",
    [
        timedelta(days=1000),
        timedelta(days=366),
        MESSAGE_RETENTION + timedelta(seconds=1),
        MESSAGE_RETENTION + MICROSECOND,
    ],
    ids=["1000 days", "366 days", "365 days and a second", "365 days and a microsecond"],
)
def test_the_orm_refuses_to_keep_a_row_longer_than_365_days(session, conversation, build, lifetime):
    with pytest.raises(ValueError, match="at most 365 days"):
        build(session, conversation, created_at=CREATED, expires_at=CREATED + lifetime)


@BUILDERS
@pytest.mark.parametrize(
    "lifetime",
    [MESSAGE_RETENTION, MESSAGE_RETENTION - MICROSECOND, timedelta(days=30), timedelta(seconds=1)],
    ids=["exactly 365 days", "365 days less a microsecond", "30 days", "a second"],
)
def test_the_orm_keeps_a_row_for_up_to_365_days(session, conversation, build, lifetime):
    row = build(session, conversation, created_at=CREATED, expires_at=CREATED + lifetime)

    assert row.expires_at - row.created_at == lifetime


@BUILDERS
def test_a_refused_insert_leaves_nothing_behind(session, conversation, build):
    with pytest.raises(ValueError):
        build(session, conversation, created_at=CREATED, expires_at=CREATED + timedelta(days=1000))

    session.rollback()
    assert session.scalar(sa.select(sa.func.count()).select_from(TurnRequest)) == 0


@BUILDERS
def test_extending_a_stored_row_past_365_days_is_refused_and_shortening_is_not(
    session, conversation, build
):
    row = build(session, conversation, created_at=CREATED)
    session.commit()

    row.expires_at = CREATED + timedelta(days=1000)
    with pytest.raises(ValueError, match="at most 365 days"):
        session.flush()
    session.rollback()

    row = session.get(type(row), row.id)
    row.expires_at = CREATED + timedelta(days=10)
    session.flush()
    assert row.expires_at == CREATED + timedelta(days=10)


@BUILDERS
def test_created_at_cannot_be_moved_to_postpone_the_deadline(session, conversation, build):
    # Otherwise the ceiling could be slid forward a year at a time.
    row = build(session, conversation, created_at=CREATED)

    row.created_at = CREATED + timedelta(days=300)
    row.expires_at = CREATED + timedelta(days=300) + MESSAGE_RETENTION
    with pytest.raises(ValueError, match="created_at cannot change once stored"):
        session.flush()


@BUILDERS
def test_a_row_without_a_creation_time_gets_one_and_the_default_deadline(session, conversation, build):
    before = datetime.now(timezone.utc)

    row = build(session, conversation)

    assert before <= row.created_at <= datetime.now(timezone.utc)
    assert row.expires_at == row.created_at + MESSAGE_RETENTION


# Rows for the database to judge, as plain SQL: (what it is, created, expires, accepted).
JUST_BEFORE_MIDNIGHT = utc(2026, 10, 16, 23, 59, 59, 999600)
LIFETIMES = [
    ("exactly 365 days", CREATED, CEILING, True),
    ("365 days less a microsecond", CREATED, CEILING - MICROSECOND, True),
    ("a day", CREATED, CREATED + timedelta(days=1), True),
    ("365 days and a microsecond", CREATED, CEILING + MICROSECOND, False),
    ("365 days and a second", CREATED, CEILING + timedelta(seconds=1), False),
    ("366 days", CREATED, CREATED + timedelta(days=366), False),
    ("1000 days", CREATED, CREATED + timedelta(days=1000), False),
    # The fraction of a second is part of the bound, not rounded away; just before
    # midnight a rounding step would also change the date.
    ("365 days, just before midnight", JUST_BEFORE_MIDNIGHT, JUST_BEFORE_MIDNIGHT + MESSAGE_RETENTION, True),
    ("365 days and a microsecond, just before midnight", JUST_BEFORE_MIDNIGHT,
     JUST_BEFORE_MIDNIGHT + MESSAGE_RETENTION + MICROSECOND, False),
    # A year is 365 days, not "the same date next year", across a leap day either way.
    ("from a leap year: Dec 31 is day 365", utc(2024, 1, 1), utc(2024, 12, 31), True),
    ("from a leap year: Jan 1 is day 366", utc(2024, 1, 1), utc(2025, 1, 1), False),
    ("across 29 Feb", utc(2024, 2, 29, 12), utc(2025, 2, 28, 12), True),
    ("across 29 Feb, a day too far", utc(2024, 2, 29, 12), utc(2025, 3, 1, 12), False),
]
LIFETIME_IDS = [name for name, *_ in LIFETIMES]


@pytest.mark.parametrize("name, created, expires, accepted", LIFETIMES, ids=LIFETIME_IDS)
def test_the_database_enforces_the_ceiling_on_messages(
    session, conversation, name, created, expires, accepted
):
    row = message_row(conversation, created_at=stored(created), expires_at=stored(expires))

    if accepted:
        raw_insert(session, "messages", **row)
    else:
        with rejected(session, "ck_messages_expires_within_retention"):
            raw_insert(session, "messages", **row)


@pytest.mark.parametrize("name, created, expires, accepted", LIFETIMES, ids=LIFETIME_IDS)
def test_the_database_enforces_the_ceiling_on_turn_requests(
    session, conversation, name, created, expires, accepted
):
    asked = new_message(session, conversation)
    row = turn_row(conversation, asked.id, created_at=stored(created), expires_at=stored(expires))

    if accepted:
        raw_insert(session, "turn_requests", **row)
    else:
        with rejected(session, "ck_turn_requests_expires_within_retention"):
            raw_insert(session, "turn_requests", **row)


def test_the_database_refuses_a_ceiling_breach_made_by_an_update(session, conversation):
    message = new_message(session, conversation, created_at=CREATED)

    with rejected(session, "ck_messages_expires_within_retention"):
        raw_update(
            session, "messages", "id = ?", (message.id,),
            expires_at=stored(CREATED + timedelta(days=1000)),
        )


def test_what_the_database_cannot_do_is_stop_both_ends_being_moved_forward_together(session, conversation):
    # A documented limit, not a guarantee. The CHECK bounds the pair of columns,
    # and a pair slid forward together is still a valid pair. Making created_at
    # immutable needs a trigger, which the schema does not use, so that part of
    # the rule is kept on the ORM path only (see the test above that refuses it).
    message = new_message(session, conversation, created_at=CREATED)
    later = CREATED + timedelta(days=300)

    raw_update(
        session, "messages", "id = ?", (message.id,),
        created_at=stored(later), expires_at=stored(later + MESSAGE_RETENTION),
    )

    session.expire_all()
    assert session.get(Message, message.id).created_at == later


@pytest.mark.parametrize(
    "created", ["0000-not a date", "2026-13-45 00:00:00.000000", "2026-02-30 00:00:00.000000", "0"]
)
def test_the_database_does_not_wave_through_a_creation_time_it_cannot_read(
    session, conversation, created
):
    # A CHECK passes when it evaluates to NULL, and SQLite's date functions
    # return NULL for text that is not a date. These sort below the expiry, so
    # expires_after_created alone would let them in.
    row = message_row(conversation, created_at=created, expires_at=stored(CEILING))

    with rejected(session, "ck_messages_expires_within_retention"):
        raw_insert(session, "messages", **row)


@pytest.mark.parametrize("how", ["orm", "sql"])
def test_a_completed_turn_cannot_keep_its_copy_of_the_answer_past_365_days(
    session, conversation, how
):
    # result_payload is the one place an answer is duplicated; it lives and dies
    # with the request, so the request is held to the same ceiling.
    finished = CREATED + timedelta(seconds=5)
    too_late = CREATED + timedelta(days=1000)

    if how == "orm":
        with pytest.raises(ValueError, match="at most 365 days"):
            new_turn_request(
                session, conversation, status="completed", result_payload={"text": "answer"},
                finished_at=finished, created_at=CREATED, expires_at=too_late,
            )
    else:
        asked = new_message(session, conversation)
        row = turn_row(
            conversation, asked.id, status="completed", result_payload='{"text": "answer"}',
            finished_at=stored(finished), created_at=stored(CREATED), expires_at=stored(too_late),
        )
        with rejected(session, "ck_turn_requests_expires_within_retention"):
            raw_insert(session, "turn_requests", **row)


def test_an_answer_and_its_copy_expire_together_when_opened_by_the_supported_path(session, conversation):
    from house_helper.persistence.turns import open_turn_request

    turn, _ = open_turn_request(session, conversation, "key-1", "hello", now=CREATED)

    assert turn.expires_at == CEILING
    assert session.get(Message, turn.user_message_id).expires_at == CEILING


# --- one spelling of an instant: the canonical UTC text ---------------------------
#
# SQLite compares and adds to timestamps as text, so the retention CHECK accepts
# exactly "YYYY-MM-DD HH:MM:SS.ffffff" (UTC, six digits of microseconds) and refuses
# every other spelling, of the same instant or of any. Everything below is plain
# SQL: nothing but the constraint stands in the way.

CANON_CREATED = "2026-01-01 00:00:00.000000"
CANON_EXPIRES = "2026-06-01 00:00:00.000000"  # 151 days later: inside the ceiling

# (created_at, expires_at). In each pair expires_at sorts after created_at as plain
# text, so expires_after_created has no reason to object and the retention rule is
# the only one that does.
NOT_THE_CANONICAL_TEXT = {
    # The reproduction from the audit: twelve hours past the ceiling, once accepted.
    "a Z and a -12:00 offset (the audit's)": ("2026-01-01 00:00:00Z", "2027-01-01 00:00:00-12:00"),
    "-12:00 on expires only": (CANON_CREATED, "2027-01-01 00:00:00.000000-12:00"),
    "+14:00 on expires, inside the ceiling as text": (CANON_CREATED, "2026-06-01 00:00:00.000000+14:00"),
    "an offset on created": ("2026-01-01 00:00:00.000000+14:00", CANON_EXPIRES),
    "the same offset on both": (
        "2026-01-01 00:00:00.000000+05:00",
        "2026-06-01 00:00:00.000000+05:00",
    ),
    "+00:00, UTC spelled as an offset": (CANON_CREATED, "2026-06-01 00:00:00.000000+00:00"),
    "a Z suffix": (CANON_CREATED, "2026-06-01 00:00:00.000000Z"),
    "a lower-case z": (CANON_CREATED, "2026-06-01 00:00:00.000000z"),
    "a T separator": ("2026-01-01T00:00:00.000000", "2026-06-01T00:00:00.000000"),
    "ISO 8601 with T and Z": ("2026-01-01T00:00:00.000000Z", "2026-06-01T00:00:00.000000Z"),
    "no fraction": ("2026-01-01 00:00:00", "2026-06-01 00:00:00"),
    "milliseconds": ("2026-01-01 00:00:00.000", "2026-06-01 00:00:00.000"),
    "five digits of fraction": (CANON_CREATED[:-1], CANON_EXPIRES[:-1]),
    "seven digits of fraction": (CANON_CREATED + "0", CANON_EXPIRES + "0"),
    "a comma as the decimal mark": ("2026-01-01 00:00:00,000000", "2026-06-01 00:00:00,000000"),
    "a date without a time": ("2026-01-01", "2026-06-01"),
    "a leading space": (" " + CANON_CREATED, " " + CANON_EXPIRES),
    "a trailing space": (CANON_CREATED + " ", CANON_EXPIRES + " "),
    "a trailing newline": (CANON_CREATED + "\n", CANON_EXPIRES + "\n"),
    "text after a NUL": (CANON_CREATED, CANON_EXPIRES + "\x00junk"),
    "a NUL inside": (CANON_CREATED, "2026-06-01 00:00:00.0000\x0000"),
    "an Arabic-Indic digit": (CANON_CREATED, "2026-06-01 00:00:00.00000٣"),
    "full-width digits": (CANON_CREATED, "２０２６-06-01 00:00:00.000000"),
    "an empty string": ("", CANON_EXPIRES),
    "garbage that sorts below a date": ("0000-not a date", CANON_EXPIRES),
    "a word": (CANON_CREATED, "soon"),
    "an integer, epoch seconds": (1767225600, 1780272000),
    "a real number": (1767225600.0, 1780272000.5),
    "a BLOB of the canonical bytes": (CANON_CREATED.encode(), CANON_EXPIRES.encode()),
    "a BLOB on one side": (CANON_CREATED, CANON_EXPIRES.encode()),
    # The canonical shape, but not a moment that exists.
    "30 February": (CANON_CREATED, "2026-02-30 00:00:00.000000"),
    "29 February in a common year": (CANON_CREATED, "2026-02-29 00:00:00.000000"),
    "month 13": (CANON_CREATED, "2026-13-01 00:00:00.000000"),
    "month 00": ("2025-12-31 00:00:00.000000", "2026-00-10 00:00:00.000000"),
    "day 00": (CANON_CREATED, "2026-03-00 00:00:00.000000"),
    "day 32": (CANON_CREATED, "2026-03-32 00:00:00.000000"),
    "hour 24": (CANON_CREATED, "2026-06-01 24:00:00.000000"),
    "hour 25": (CANON_CREATED, "2026-06-01 25:00:00.000000"),
    "minute 60": (CANON_CREATED, "2026-06-01 00:60:00.000000"),
    "second 60": (CANON_CREATED, "2026-06-01 00:00:60.000000"),
    "created_at on 30 February": ("2026-02-30 00:00:00.000000", CANON_EXPIRES),
}
TABLES_WITH_A_CEILING = ["messages", "turn_requests"]


def _insert_row(session, conversation, table, created, expires, key="key-1"):
    """One row of `table` as plain SQL, created_at and expires_at exactly as given."""
    if table == "messages":
        row = message_row(conversation, created_at=created, expires_at=expires)
    else:
        asked = new_message(session, conversation)
        row = turn_row(
            conversation, asked.id, idempotency_key=key, created_at=created, expires_at=expires
        )
    raw_insert(session, table, **row)


@pytest.mark.parametrize("table", TABLES_WITH_A_CEILING)
@pytest.mark.parametrize("name", NOT_THE_CANONICAL_TEXT)
def test_the_database_refuses_every_spelling_but_the_canonical_one(
    session, conversation, table, name
):
    created, expires = NOT_THE_CANONICAL_TEXT[name]

    with rejected(session, f"ck_{table}_expires_within_retention"):
        _insert_row(session, conversation, table, created, expires)


@pytest.mark.parametrize("table", TABLES_WITH_A_CEILING)
def test_the_canonical_text_itself_is_taken(session, conversation, table):
    _insert_row(session, conversation, table, CANON_CREATED, CANON_EXPIRES)


@pytest.mark.parametrize("table", TABLES_WITH_A_CEILING)
@pytest.mark.parametrize(
    "column, value",
    [
        ("expires_at", "2027-10-16 12:00:00.000000+00:00"),
        ("expires_at", "2027-10-16T12:00:00.000000"),
        ("expires_at", "2027-10-16 12:00:00"),
        ("expires_at", "2027-10-16 12:00:00.000000-12:00"),
        ("created_at", "2026-10-16 12:00:00.000000Z"),
        ("created_at", "2026-10-16 12:00:00"),
    ],
)
def test_an_update_to_another_spelling_is_refused_too(session, conversation, table, column, value):
    if table == "messages":
        row_id = new_message(session, conversation, created_at=CREATED).id
    else:
        row_id = new_turn_request(session, conversation, created_at=CREATED).id.hex

    with rejected(session, f"ck_{table}_expires_within_retention"):
        raw_update(session, table, "id = ?", (row_id,), **{column: value})


@pytest.mark.parametrize("table", TABLES_WITH_A_CEILING)
@pytest.mark.parametrize(
    "created, expires",
    [
        ("2026-01-01 00:00:00.000000", "9999-12-31 23:59:59.999999"),
        ("0001-01-01 00:00:00.000000", "9999-12-31 23:59:59.999999"),
        ("2026-01-01 00:00:00.000000", "2028-01-01 00:00:00.000000"),
        ("2026-01-01 00:00:00.000000", "2027-01-02 00:00:00.000000"),
    ],
    ids=["to the year 9999", "from year 1 to 9999", "two years", "366 days"],
)
def test_the_database_refuses_an_excessive_retention_in_the_canonical_text(
    session, conversation, table, created, expires
):
    with rejected(session, f"ck_{table}_expires_within_retention"):
        _insert_row(session, conversation, table, created, expires)


@pytest.mark.parametrize("table", TABLES_WITH_A_CEILING)
@pytest.mark.parametrize("microsecond", [0, 1, 500000, 999998, 999999])
def test_the_ceiling_is_exact_to_the_microsecond_at_every_fraction(
    session, conversation, table, microsecond
):
    created = datetime(2026, 10, 16, 12, 0, 0, microsecond, tzinfo=timezone.utc)
    ceiling = created + MESSAGE_RETENTION

    _insert_row(session, conversation, table, stored(created), stored(ceiling), key="exact")
    _insert_row(
        session, conversation, table, stored(created), stored(ceiling - MICROSECOND), key="under"
    )
    with rejected(session, f"ck_{table}_expires_within_retention"):
        _insert_row(
            session, conversation, table, stored(created), stored(ceiling + MICROSECOND), key="over"
        )


def test_the_retention_check_does_no_floating_point_date_arithmetic():
    # julianday() and strftime('%s') return numbers that cannot hold the microsecond
    # of a present-day date; the bound is built from text and whole days instead.
    from sqlalchemy.dialects import sqlite

    from house_helper.persistence.sqlchecks import WithinRetention

    text = str(WithinRetention("created_at", "expires_at").compile(dialect=sqlite.dialect()))

    assert not re.search(r"julianday|strftime|unixepoch|86400|CAST\([^)]* AS (REAL|FLOAT|NUMERIC)", text, re.I)
    assert "+365 days" in text


# The rule, said again in Python and without SQL: the shape, and a moment that exists.

CANONICAL_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}")


def python_says_canonical(text) -> bool:
    if not isinstance(text, str) or CANONICAL_SHAPE.fullmatch(text) is None:
        return False
    year, month, day, hour, minute, second, microsecond = map(int, re.split(r"[-: .]", text))
    try:
        datetime(year, month, day, hour, minute, second, microsecond)
    except ValueError:  # Python's calendar starts at year 1; SQLite also takes 0000
        return False
    return True


def sqlite_says_canonical(connection, value) -> bool:
    fragment = canonical_utc_text_sqlite(":value")  # the very text the CHECKs are built from
    return bool(connection.execute(f"SELECT COALESCE(({fragment}), 0)", {"value": value}).fetchone()[0])


def _spellings():
    base = "2026-10-16 12:34:56.789012"
    yield base
    for position in range(len(base)):  # one character replaced, at every position
        for char in "09a-: .TZz+/\x00٣０":
            yield base[:position] + char + base[position + 1 :]
    for position in range(len(base) + 1):  # one character added
        yield base[:position] + "0" + base[position:]
    for position in range(len(base)):  # one character removed
        yield base[:position] + base[position + 1 :]
    for year in (1900, 2000, 2024, 2026, 2100):  # the calendar, field by field
        for month in range(0, 14):
            for day in (0, 1, 28, 29, 30, 31, 32):
                yield f"{year:04d}-{month:02d}-{day:02d} 00:00:00.000000"
    for hour in range(0, 26):  # and the clock
        for minute in (0, 59, 60):
            for second in (0, 59, 60):
                yield f"2026-01-01 {hour:02d}:{minute:02d}:{second:02d}.000000"
    for fraction in ("000000", "000001", "999999"):
        yield f"2026-01-01 00:00:00.{fraction}"


def test_the_canonical_rule_gives_python_and_sqlite_the_same_verdict():
    connection = sqlite3.connect(":memory:")
    try:
        verdicts = {text: (python_says_canonical(text), sqlite_says_canonical(connection, text))
                    for text in _spellings()}
    finally:
        connection.close()

    disagreements = {text: v for text, v in verdicts.items() if v[0] != v[1]}
    assert disagreements == {}
    assert len(verdicts) > 1000
    assert {python for python, _ in verdicts.values()} == {True, False}


@pytest.mark.parametrize("value", [None, 5, 5.5, b"2026-10-16 12:34:56.789012"])
def test_a_value_that_is_not_text_is_not_a_canonical_timestamp(value):
    connection = sqlite3.connect(":memory:")
    try:
        assert sqlite_says_canonical(connection, value) is False
    finally:
        connection.close()
    assert python_says_canonical(value) is False


# What the ORM writes is exactly that text, whatever zone the datetime came in.
@pytest.mark.parametrize(
    "moment",
    [
        utc(2026, 10, 16, 12),
        datetime(2026, 10, 16, 12, 0, 0, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 16, 12, 0, 0, 999999, tzinfo=timezone.utc),
        datetime(2026, 10, 16, 15, 0, tzinfo=timezone(timedelta(hours=3))),
        datetime(2026, 10, 16, 5, 0, tzinfo=timezone(timedelta(hours=-7))),
        utc(999, 1, 1),
        utc(1, 1, 1),
    ],
    ids=["whole second", "1 us", "999999 us", "+03:00", "-07:00", "year 999", "year 1"],
)
def test_the_orm_writes_the_canonical_text(session, conversation, moment):
    message = new_message(session, conversation, created_at=moment, expires_at=moment + timedelta(days=1))
    u = moment.astimezone(timezone.utc)
    expected = (
        f"{u.year:04d}-{u.month:02d}-{u.day:02d} {u.hour:02d}:{u.minute:02d}:{u.second:02d}"
        f".{u.microsecond:06d}"
    )

    row = session.execute(
        sa.text("SELECT created_at, typeof(created_at), length(created_at) FROM messages WHERE id = :id"),
        {"id": message.id},
    ).one()

    assert tuple(row) == (expected, "text", 26)
    assert python_says_canonical(row[0])


# --- the deadline columns are indexed for the purge job ---------------------------


@pytest.mark.parametrize("table", ["messages", "turn_requests"])
def test_expires_at_is_indexed(engine, table):
    indexed = [index["column_names"] for index in sa.inspect(engine).get_indexes(table)]

    assert ["expires_at"] in indexed


# --- timestamps are timezone-aware UTC --------------------------------------------


def test_a_timestamp_in_another_zone_is_stored_as_the_same_instant_in_utc(factory):
    plus_three = timezone(timedelta(hours=3))
    given = datetime(2026, 10, 16, 15, 0, tzinfo=plus_three)

    with factory() as s:
        message = new_message(s, new_conversation(s, new_user(s)), created_at=given)
        message_id = message.id
        s.commit()

    with factory() as s:
        loaded = s.get(Message, message_id)

        assert loaded.created_at == given
        assert loaded.created_at == utc(2026, 10, 16, 12, 0)
        assert loaded.created_at.utcoffset() == timedelta(0)


def test_a_naive_timestamp_is_refused_rather_than_guessed_at(session, conversation):
    naive = datetime(2026, 10, 16, 12, 0)

    with pytest.raises(sa.exc.StatementError) as refused:
        new_message(session, conversation, created_at=naive)

    assert isinstance(refused.value.orig, ValueError)
    assert "timezone-aware" in str(refused.value.orig)


def test_timestamps_compare_correctly_in_queries_across_zones(session, conversation):
    new_message(session, conversation, content="early", created_at=utc(2026, 10, 16, 9, 0))
    new_message(session, conversation, content="late", created_at=utc(2026, 10, 16, 11, 0))
    plus_three = timezone(timedelta(hours=3))
    cutoff = datetime(2026, 10, 16, 13, 0, tzinfo=plus_three)  # 10:00 UTC

    after = session.scalars(sa.select(Message).where(Message.created_at > cutoff)).all()

    assert [m.content for m in after] == ["late"]


def test_every_timestamp_column_uses_the_utc_type():
    from house_helper.persistence.base import Base, UtcDateTime

    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.type._type_affinity is sa.DateTime and not isinstance(column.type, UtcDateTime)
    ]

    assert offenders == []


def test_creation_timestamps_are_set_when_a_row_is_stored(session, conversation):
    before = datetime.now(timezone.utc)
    turn = new_turn_request(session, conversation)

    assert before <= turn.created_at <= datetime.now(timezone.utc)
    assert isinstance(turn, TurnRequest)
