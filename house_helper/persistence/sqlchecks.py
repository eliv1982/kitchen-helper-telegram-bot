"""CHECK constraint expressions that cannot be written once for both databases.

Nearly every CHECK in models.py is plain SQL that SQLite and PostgreSQL both
run (length, lower, replace, substr, comparisons). Three rules are not:

  * "the domain of an email is lower-case, its local part is as given": needs the
    position of the "@", which SQLite calls instr() and PostgreSQL strpos(); each
    dialect has only its own.
  * "only printable ASCII": SQLite has no regular expressions but has GLOB, and
    its length() stops at an embedded NUL, so a NUL must be caught separately;
    and it keeps a value in whatever storage class it arrives in, so a BLOB has
    to be refused by its type (see below). PostgreSQL has regular expressions
    and cannot store a NUL or a BLOB in a text column at all.
  * "no later than the retention period after creation": SQLite keeps timestamps
    as text and does date arithmetic with datetime(); PostgreSQL has timestamptz
    and intervals, which carry their own time zone handling.

Each is a small SQL element that renders the right text for the dialect the
table is being created in. The Alembic migration spells out both texts by hand
(a migration must not import the models); tests/test_persistence_migrations.py
compares the two, per dialect, so they cannot drift apart.

The canonical timestamp. SQLite has no timestamp type, so the retention rule
works on text, and text can only be compared and added to if there is exactly
one spelling of an instant. That spelling is what base.UtcDateTime writes and
what the ORM therefore always produces:

    YYYY-MM-DD HH:MM:SS.ffffff     26 ASCII characters, always UTC

A space between date and time, six digits of microseconds (zeros included), no
"T", no "Z", no offset, no other precision. The retention rule refuses every
other spelling of created_at and expires_at: SQLite compares text character by
character, so created_at "2026-01-01 00:00:00Z" with expires_at
"2027-01-01 00:00:00-12:00" (twelve hours past the ceiling) passed, because "-"
sorts before "Z". What it checks, each part for a reason:

  * typeof() = 'text': GLOB and length() read a BLOB as if it were text. (The
    round-trip comparison below also refuses a BLOB, because a BLOB is never
    equal to text; typeof() says so outright and does not lean on that.)
  * length(CAST(... AS BLOB)) = 26: bytes, not characters. length() and GLOB
    stop at an embedded NUL, so "2026-10-16 12:00:00.000000" followed by a NUL
    and anything else would pass both.
  * GLOB with [0-9] in each digit position and the literal separators: the shape.
  * datetime(first 19 characters, '+0 days') must give those 19 characters back:
    the value is a real moment. A bare datetime() is not enough, since SQLite
    reads "2026-02-30" as 2 March and "24:00:00" as it stands; the modifier
    forces it to compute, and anything it moves or cannot read does not
    round-trip (it returns NULL for a month 13 or a minute 60).

Only these two columns of each table are held to it, because only here does a
rule depend on the text being exact. The other timestamp columns are written by
the ORM in the same form; nothing but their writers keeps them there.

SQLite semantics worth knowing, because the texts below rely on them:

  * A CHECK passes when it evaluates to NULL. datetime() returns NULL for text
    that is not a date, so the retention rule is wrapped in COALESCE(..., 0):
    an unparseable created_at is refused instead of waved through.
  * created_at is cut to its first 19 characters ("YYYY-MM-DD HH:MM:SS") before
    datetime() adds the days, and the fractional part is appended again as text.
    Letting datetime() see the fraction would round it to milliseconds (and,
    just before midnight, change the date); this way the bound is exact to the
    microsecond for every value SQLAlchemy writes. No julianday() or strftime
    ('%s') anywhere: they return floating-point numbers that cannot hold a
    microsecond of a recent date.
"""

import sqlalchemy as sa
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ColumnElement

from house_helper.persistence.base import RETENTION_DAYS

# GLOB pattern of the canonical timestamp text, "YYYY-MM-DD HH:MM:SS.ffffff".
_UTC_TEXT_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]"


class PrintableAscii(ColumnElement):
    """True when the column holds only the characters "!" to "~" and is not
    empty: no whitespace, no control character, nothing outside ASCII."""

    inherit_cache = False  # only ever used in DDL, never in a cached query
    type = sa.Boolean()

    def __init__(self, column: str) -> None:
        self.column = column


@compiles(PrintableAscii)  # PostgreSQL, and the text shown by str()
def _printable_ascii_default(element: PrintableAscii, compiler, **kw) -> str:
    return f"{element.column} ~ '^[!-~]+$'"


@compiles(PrintableAscii, "sqlite")
def _printable_ascii_sqlite(element: PrintableAscii, compiler, **kw) -> str:
    column = element.column
    # typeof: a BLOB is refused. GLOB and length() read one as text, so "key" and
    # the BLOB x'6b6579' would both pass, and they are different values to a
    # UNIQUE constraint. 'null' stays allowed so that a nullable column
    # (identity_links.email) can be empty; NOT NULL is the column's own rule.
    # GLOB: no character outside "!".."~". The BLOB length catches what GLOB and
    # length() cannot see: after a NUL (both stop there) and, redundantly, any
    # multi-byte character.
    return (
        f"(typeof({column}) IN ('text', 'null') "
        f"AND {column} NOT GLOB '*[^!-~]*' "
        f"AND length({column}) = length(CAST({column} AS BLOB)) "
        f"AND length({column}) >= 1)"
    )


class EmailSubjectNormalized(ColumnElement):
    """The database's copy of identifiers.normalize_identity for an email subject:
    one "@" with something on both sides, and everything from the "@" on (the
    domain) lower-case. The local part is deliberately not constrained: it is kept
    exactly as the administrator approved it, because "Victim@example.com" and
    "victim@example.com" may be two mailboxes. True for any provider but email.
    (Lower-casing is ASCII-only on both databases, which is all that
    subject_printable_ascii lets through.)"""

    inherit_cache = False  # only ever used in DDL, never in a cached query
    type = sa.Boolean()

    def __init__(self, provider: str, subject: str) -> None:
        self.provider = provider
        self.subject = subject


def _email_subject_normalized(element: EmailSubjectNormalized, position: str) -> str:
    subject = element.subject
    domain = f"substr({subject}, {position}({subject}, '@'))"  # "@" and what follows it
    return (
        f"{element.provider} <> 'email' OR ({domain} = lower({domain}) "
        f"AND length({subject}) - length(replace({subject}, '@', '')) = 1 "
        f"AND substr({subject}, 1, 1) <> '@' "
        f"AND substr({subject}, length({subject}), 1) <> '@')"
    )


@compiles(EmailSubjectNormalized)  # PostgreSQL, and the text shown by str()
def _email_subject_normalized_default(element: EmailSubjectNormalized, compiler, **kw) -> str:
    return _email_subject_normalized(element, "strpos")


@compiles(EmailSubjectNormalized, "sqlite")
def _email_subject_normalized_sqlite(element: EmailSubjectNormalized, compiler, **kw) -> str:
    return _email_subject_normalized(element, "instr")


class WithinRetention(ColumnElement):
    """True when `expires` is no later than RETENTION_DAYS days after `created`.
    On SQLite also false unless both are the canonical timestamp text (see the
    module docstring); on PostgreSQL timestamptz has no spelling to get wrong."""

    inherit_cache = False  # only ever used in DDL, never in a cached query
    type = sa.Boolean()

    def __init__(self, created: str, expires: str) -> None:
        self.created = created
        self.expires = expires


@compiles(WithinRetention)  # PostgreSQL, and the text shown by str()
def _within_retention_default(element: WithinRetention, compiler, **kw) -> str:
    # Hours, not days: "days" is calendar arithmetic that follows the session's
    # time zone across a daylight-saving change; 365 * 24 hours is always the
    # same length of time.
    hours = RETENTION_DAYS * 24
    return f"{element.expires} <= {element.created} + interval '{hours} hours'"


def canonical_utc_text_sqlite(column: str) -> str:
    """`column` holds exactly "YYYY-MM-DD HH:MM:SS.ffffff", a real moment (see the
    module docstring for what each part is for)."""
    return (
        f"typeof({column}) = 'text' "
        f"AND length(CAST({column} AS BLOB)) = 26 "
        f"AND {column} GLOB '{_UTC_TEXT_GLOB}' "
        f"AND datetime(substr({column}, 1, 19), '+0 days') = substr({column}, 1, 19)"
    )


@compiles(WithinRetention, "sqlite")
def _within_retention_sqlite(element: WithinRetention, compiler, **kw) -> str:
    created, expires = element.created, element.expires
    return (
        f"COALESCE({canonical_utc_text_sqlite(created)} "
        f"AND {canonical_utc_text_sqlite(expires)} "
        f"AND {expires} <= (datetime(substr({created}, 1, 19), '+{RETENTION_DAYS} days') "
        f"|| substr({created}, 20)), 0)"
    )
