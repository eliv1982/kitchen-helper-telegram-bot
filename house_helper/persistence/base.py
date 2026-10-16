"""Declarative base, constraint naming and the column types shared by all models."""

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase

# Every constraint and index gets a deterministic name. Alembic needs names to
# drop or alter anything later (SQLite in particular cannot refer to an unnamed
# constraint), and PostgreSQL truncates identifiers longer than 63 characters,
# which tests/test_persistence_models.py checks for.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


# The data policy: nothing the user said, and nothing derived from it, is kept
# longer than this after it was created. models.py applies it to messages and to
# the turn requests that hold a copy of an answer; sqlchecks.py spells it as SQL.
RETENTION_DAYS = 365
RETENTION = timedelta(days=RETENTION_DAYS)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(sa.types.TypeDecorator):
    """A timestamp that is always timezone-aware UTC in Python.

    PostgreSQL stores it as timestamptz. SQLite has no timezone-aware type and
    would hand back naive datetimes, so the value is converted to UTC on the way
    in and tagged as UTC on the way out. A naive datetime is rejected rather
    than guessed at: it would silently be read back as UTC whatever zone it was
    meant in.

    The canonical SQLite text, which is what this type writes and the only
    spelling the retention CHECKs accept (see sqlchecks.py):

        YYYY-MM-DD HH:MM:SS.ffffff

    UTC, 26 ASCII characters, a space between date and time, six digits of
    microseconds always (".000000" included), no "T", no "Z", no offset. SQLite
    compares and adds to text character by character, so there must be exactly
    one spelling of an instant.
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"timestamps must be timezone-aware, got naive {value!r}")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        datetime: UtcDateTime,
        uuid.UUID: sa.Uuid,
    }
