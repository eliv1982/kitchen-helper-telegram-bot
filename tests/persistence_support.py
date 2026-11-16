"""Shared fixtures and builders for the persistence tests.

The `engine` fixture is parametrized: every test that uses it runs twice, once
against a schema created straight from the models and once against a schema
built by the Alembic migrations. Alembic's autogenerate comparison does not see
CHECK constraints or ON DELETE actions, so this is what proves that the
migrated database enforces the same rules the models declare.

Everything is a SQLite file under pytest's tmp_path: nothing leaves the machine
and no setting is read from the environment or from a .env file.

Two ways to write a row, because they test different things. The new_* builders
go through the ORM (its defaults, its validation listeners); raw_insert and the
*_row dictionaries go straight to SQL, past SQLAlchemy's type handling and past
every listener, so what refuses them is the database's own constraints.
"""

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.allowlist import allow
from house_helper.persistence.base import Base
from house_helper.persistence.database import create_session_factory
from house_helper.persistence.models import (
    MESSAGE_RETENTION,
    Conversation,
    IdentityLink,
    Message,
    OidcState,
    Session,
    TurnRequest,
    User,
)

SCHEMAS = ["created_from_models", "migrated"]


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def digest(text: str) -> str:
    """A 64-character hex digest, the shape of a stored token or fingerprint."""
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(params=SCHEMAS)
def engine(request, tmp_path, database_backend):
    """A schema on the configured backend (PostgreSQL when the test server is set, CI-14).

    `created_from_models` builds the DDL from Base.metadata; `migrated` runs the Alembic
    chain to the stated head. On PostgreSQL the DDL runs as the migration/owner login and
    the returned engine is the restricted application login.
    """
    database = database_backend.new_database(
        schema="models" if request.param == "created_from_models" else "migrations",
        tmp_path=tmp_path,
    )
    engine = database.app_engine()
    engine._hh_database = database  # for the DDL a test needs (a simulated outage)
    try:
        yield engine
    finally:
        engine.dispose()
        database.close()


@pytest.fixture
def factory(engine):
    return create_session_factory(engine)


@pytest.fixture
def session(factory):
    with factory() as session:
        yield session


@dataclass(frozen=True)
class Failure:
    """A driver error as plain values (HH-DB-01 R3-04). Read once, from the exception in
    hand: PostgreSQL's structured diagnostics are preferred to anything a message says,
    and a message may quote row data that a regex must never mistake for a rule."""

    message: str
    state: Optional[str]  # SQLSTATE
    constraint_name: str
    column_name: str
    table_name: str
    detail: str  # the server's DETAIL field (structured, not the row's text)
    statement: Optional[str]
    params: object


def describe(error: BaseException) -> Failure:
    orig = getattr(error, "orig", error)
    diag = getattr(orig, "diag", None)
    return Failure(
        message=str(orig),
        state=getattr(orig, "sqlstate", None),
        constraint_name=getattr(diag, "constraint_name", None) or "",
        column_name=getattr(diag, "column_name", None) or "",
        table_name=getattr(diag, "table_name", None) or "",
        detail=getattr(diag, "message_detail", None) or "",
        statement=getattr(error, "statement", None),
        params=getattr(error, "params", None),
    )


def assert_expected_rejection(dialect: str, constraint: str, failure: Failure) -> None:
    """The one decision `rejected` makes, pure and testable offline.

    SQLite names its rule in the message. PostgreSQL's message may also quote row data,
    so on PostgreSQL the structured SQLSTATE and `diag.constraint_name`/`diag.column_name`
    decide; the message is never searched for the expected name first.

    `constraint` is normally one name. It may be a narrowly bounded tuple of exact
    `ck_...` names for the one documented case where two CHECK constraints necessarily
    fail together for a single incoherent field (R3); the structured diagnostic must then
    name one of exactly those, never a third rule.

    A PostgreSQL 22001 (a type-width refusal) is never accepted here: a test that
    exercises a declared width asks `refused_width`, which makes the attribution itself
    (R4)."""
    permitted = (constraint,) if isinstance(constraint, str) else tuple(constraint)
    if dialect != "postgresql":
        for name in permitted:
            if re.search(name, failure.message):
                return
        raise AssertionError(f"the database did not name the expected rule: {failure.message}")
    if not isinstance(constraint, str):
        # R3: the row is incoherent in one way that two CHECKs both describe. Exactly the
        # identified names are permitted; a generic or third constraint is not.
        assert permitted and all(
            isinstance(name, str) and name.startswith("ck_") for name in permitted
        ), "a tuple expectation must list exact ck_... constraint names"
        assert failure.state == "23514" and failure.constraint_name in permitted, failure.message
        return
    if (
        failure.state is None
        and "printable_ascii" in constraint
        and "cannot contain NUL" in failure.message
    ):
        # The one documented exception (R2-04): psycopg refuses a NUL byte in a text value
        # before the statement reaches the server, so on PostgreSQL the printable-ASCII
        # CHECK cannot be the rule that fires for that input. The value is not storable,
        # which is what the test proves. Nothing else is excused.
        return
    if constraint == "CHECK constraint failed":
        # The test names no particular CHECK; it asserts that a CHECK of the row fired.
        assert failure.state == "23514", failure.message
    elif constraint in ("FOREIGN KEY constraint failed", "FOREIGN KEY"):
        assert failure.state == "23503", failure.message
    elif constraint.startswith("NOT NULL constraint failed:"):
        column = constraint.split(":", 1)[1].strip().split(".")[-1]
        # R3-04: the server names the column in the structured diagnostic. The message is
        # not evidence: a value of another row could contain the column's name.
        assert failure.state == "23502" and failure.column_name == column, failure.message
    elif constraint.startswith("ck_"):
        # R3-04: THE named CHECK, from `diag.constraint_name` only.
        assert failure.state == "23514" and failure.constraint_name == constraint, failure.message
    else:
        # A column list ("table.provider, table.subject") for a UNIQUE key or a foreign
        # key. The server names the constrained columns in `diag.constraint_name` or in
        # the structured DETAIL's `Key (...)` prefix - never in the free-text message,
        # which can quote the rejected row's values.
        columns = [part.strip().split(".")[-1] for part in constraint.split(",")]
        assert failure.state in ("23503", "23505") and failure.constraint_name, failure.message
        named = failure.constraint_name
        key = re.match(r"^\s*Key\s*\(([^)]*)\)", failure.detail)
        if key is not None:
            named += " " + key.group(1)
        missing = [column for column in columns if column not in named]
        assert not missing, f"{failure.message} (DETAIL: {failure.detail})"


# --- declared widths (R4) -----------------------------------------------------------------
#
# A PostgreSQL 22001 says only "value too long for type character varying(n)". Deciding
# WHICH column caused it by reading the SQL that failed is guesswork dressed as parsing: a
# literal before a positional parameter shifts the values, a cast changes the effective
# type, a JSONB or numeric conversion changes the stored representation, and VARCHAR
# accepts trailing spaces beyond n. Every attempt to make the reader cover one more real
# statement widened the ground for a false attribution.
#
# So the helper does not parse the failing statement at all. It writes the row itself: an
# UPDATE of one existing, otherwise valid row, keyed by its primary key, that assigns
# exactly one bounded text column (plus, where the model declares one, an automatic
# non-text onupdate column). The statement has no literal, no width-limiting cast or
# expression around that value and no second text value, so a 22001 has exactly one possible
# owner; and the driver's 22001 is accepted only when it reports the exact statement this
# helper compiled, with the target's declared width in the message. The value is checked in
# Python first (text, and provably over the width once PostgreSQL's trailing-space
# truncation is taken into account). Anything the helper cannot account for is refused, not
# guessed.


def declared_width(table: str, column: str) -> int:
    """The declared width of a bounded text column, from the model (never from SQL)."""
    column_type = Base.metadata.tables[table].c[column].type
    limit = getattr(column_type, "length", None)
    assert isinstance(column_type, sa.String) and limit is not None, (
        f"{table}.{column} is not a bounded text column ({column_type!r}); a 22001 cannot "
        "be its type refusal"
    )
    return limit


def text_over_width(value: object, limit: int) -> bool:
    """Whether PostgreSQL's `varchar(limit)` refuses `value` at that width.

    PostgreSQL raises 22001 only when what remains after the trailing spaces is too long:
    "If the excess characters are all spaces ... the string will be truncated to the
    maximum length". A value that is not text is never over a width in a way this check can
    predict, because the server would first convert it (a number, a JSON document)."""
    return isinstance(value, str) and len(value.rstrip(" ")) > limit


def require_over_width(table: str, column: str, value: object, limit: int) -> None:
    """`value` must be text that PostgreSQL's own rule makes over `limit`, or refuse."""
    assert isinstance(value, str), (
        f"a width probe for {table}.{column} must bind text; {type(value).__name__} would "
        "be converted by the server and the stored width could not be predicted"
    )
    assert text_over_width(value, limit), (
        f"{value!r} is not provably over {table}.{column}'s declared width of {limit}: "
        "PostgreSQL truncates trailing spaces, so only the text before them counts"
    )


def _bound_key(column, value):
    """A primary-key value as its column's type takes it (a UUID seeded as hex text is
    still a UUID: SQLAlchemy's Uuid bind expects one, not a string)."""
    if isinstance(column.type, sa.Uuid) and isinstance(value, str):
        return uuid.UUID(value)
    return value


def width_probe(table: str, column: str, value: str, key: Mapping[str, object]) -> sa.Update:
    """The one statement a 22001 may be attributed to: it writes exactly `table.column`.

    `key` must be the row's primary key (and only it), and no key column may be bounded
    text. Any automatic onupdate column may also be written, but only if it has no declared
    width. Then the statement contains exactly one bounded text value - the caller's - with
    no literal, no width-limiting cast and no expression around it to move the value onto
    another column or change its effective type."""
    model = Base.metadata.tables[table]
    primary_key = set(model.primary_key.columns.keys())
    assert set(key) == primary_key, (
        f"a width probe must identify the row by its primary key {sorted(primary_key)}, "
        f"not {sorted(key)}"
    )
    assert all(getattr(model.c[name].type, "length", None) is None for name in key), (
        "a width probe's key must not include a bounded text value"
    )
    written = {column} | {name for name, item in model.c.items() if item.onupdate is not None}
    assert all(
        name == column or getattr(model.c[name].type, "length", None) is None
        for name in written
    ), (
        f"the probe for {table}.{column} would also write another bounded text column: "
        f"{sorted(written)}"
    )
    return (
        sa.update(model)
        .where(
            *(
                model.c[name] == _bound_key(model.c[name], expected)
                for name, expected in key.items()
            )
        )
        .values({column: value})
        .execution_options(synchronize_session=False)
    )


def assert_width_refusal(table: str, column: str, failure: Failure, *, statement: str) -> None:
    """Accept a PostgreSQL 22001 only for the probe's own statement and declared width."""
    limit = declared_width(table, column)
    assert failure.state == "22001", failure.message
    assert failure.statement == statement, (
        "the width refusal was not raised by the single-column probe: expected "
        f"{statement!r}, got {failure.statement!r}"
    )
    assert f"character varying({limit})" in failure.message, failure.message


def refused_width(
    session: OrmSession,
    table: str,
    column: str,
    value: object,
    *,
    key: Mapping[str, object],
    check: Optional[str] = None,
) -> None:
    """`table.column` refuses `value`: at its declared width on PostgreSQL, by its CHECK on
    SQLite (which does not enforce VARCHAR(n)).

    The caller has written an otherwise valid row and identifies it by primary key. This
    helper writes only the one column, so no other value can be the false cause of a 22001;
    `key` and `value` are both checked before the statement is issued, and the PostgreSQL
    acceptance also requires the exact compiled statement (see `width_probe`).
    """
    check = check or f"ck_{table}_{column}_max_length"
    column_type = Base.metadata.tables[table].c[column].type
    limit = getattr(column_type, "length", None)
    assert isinstance(column_type, sa.String), (
        f"{table}.{column} is not a text column ({column_type!r}); a width probe binds text"
    )
    assert isinstance(value, str), (
        f"a width probe for {table}.{column} must bind text; {type(value).__name__} would "
        "be converted by the server and the stored value could not be predicted"
    )
    if limit is not None:
        require_over_width(table, column, value, limit)
    statement = width_probe(table, column, value, key)
    compiled = str(statement.compile(dialect=session.get_bind().dialect))
    with pytest.raises((sa.exc.IntegrityError, sa.exc.DataError)) as caught:
        with session.begin_nested():
            session.execute(statement)

    failure = describe(caught.value)
    if session.get_bind().dialect.name == "sqlite":
        assert re.search(check, failure.message), failure.message
        return
    if limit is None:
        # A TEXT column has no type width to fire first, so the named CHECK is the rule on
        # PostgreSQL too (and it must be that CHECK, not another one).
        assert failure.state == "23514" and failure.constraint_name == check, failure.message
        return
    assert_width_refusal(table, column, failure, statement=compiled)


@contextmanager
def rejected(session: OrmSession, constraint):
    """The block (plus the flush that follows it) must violate `constraint`.

    `constraint` is one rule (a named `ck_...`, a column list, or one of the generic
    phrases) or, for the one documented case where two CHECKs necessarily fire together,
    a tuple of the exact `ck_...` names that are permitted. A PostgreSQL type-width
    refusal (22001) is NOT accepted here; a test that means a declared width uses
    `refused_width`, which writes the row itself and so can attribute the refusal (R4).

    SQLite names a CHECK constraint, and for a UNIQUE one lists its columns. PostgreSQL
    phrases the same facts differently, so its structured diagnostics decide: the SQLSTATE
    plus `diag.constraint_name` / `diag.column_name`. The message is not searched for the
    expected name on PostgreSQL (R3-04: it may quote row data), and an unrelated rule can
    never satisfy the expectation.

    Runs in a SAVEPOINT, so what the test set up before the block survives the
    failure and the block can be repeated.
    """
    with pytest.raises((sa.exc.IntegrityError, sa.exc.DataError)) as caught:
        with session.begin_nested():
            yield
            session.flush()

    assert_expected_rejection(
        session.get_bind().dialect.name, constraint, describe(caught.value)
    )


def new_user(session: OrmSession, **fields) -> User:
    user = User(**fields)
    session.add(user)
    session.flush()
    return user


def new_conversation(session: OrmSession, user: User, **fields) -> Conversation:
    conversation = Conversation(user_id=user.id, **fields)
    session.add(conversation)
    session.flush()
    return conversation


def new_message(
    session: OrmSession,
    conversation: Conversation,
    role: str = "user",
    content: str = "hello",
    **fields,
) -> Message:
    message = Message(conversation_id=conversation.id, role=role, content=content, **fields)
    session.add(message)
    session.flush()
    return message


def new_turn_request(
    session: OrmSession, conversation: Conversation, key: str = "key-1", **fields
) -> TurnRequest:
    """A request in `conversation`. Unless user_message_id is given, the user
    message it is made from is created with it: a request does not exist without
    one (see TurnRequest)."""
    if "user_message_id" not in fields:
        fields["user_message_id"] = new_message(
            session, conversation, role="user", content=f"request {key}"
        ).id
    turn = TurnRequest(
        conversation_id=conversation.id,
        idempotency_key=key,
        request_fingerprint=fields.pop("request_fingerprint", digest(key)),
        **fields,
    )
    session.add(turn)
    session.flush()
    return turn


def new_login_session(session: OrmSession, user: User, token: str = "token-1", **fields) -> Session:
    """A session of `user`. Unless `identity_link_id` is given (None included) it
    is opened through a link of that user: their first one, or a new one if they
    have none. (A session needs provenance to be live; see models.Session.)"""
    now = fields.pop("created_at", datetime.now(timezone.utc))
    if "identity_link_id" not in fields:
        link = session.scalar(sa.select(IdentityLink).where(IdentityLink.user_id == user.id))
        fields["identity_link_id"] = (
            link or new_identity_link(session, user, subject=f"sub-{user.id}")
        ).id
    login = Session(
        user_id=user.id,
        token_hash=fields.pop("token_hash", digest(token)),
        created_at=now,
        expires_at=fields.pop("expires_at", now + timedelta(days=1)),
        **fields,
    )
    session.add(login)
    session.flush()
    return login


def new_identity_link(
    session: OrmSession, user: User, provider: str = "telegram", subject: str = "sub-1", **fields
) -> IdentityLink:
    """A link for `user`. The identity is put on the allowlist for that user
    first, as it has to be: a link can only exist for a provisioned identity."""
    allow(session, user, provider, subject)
    link = IdentityLink(user_id=user.id, provider=provider, subject=subject, **fields)
    session.add(link)
    session.flush()
    return link


def new_oidc_state(session: OrmSession, state: str = "state-1", **fields) -> OidcState:
    now = fields.pop("created_at", datetime.now(timezone.utc))
    row = OidcState(
        state_hash=fields.pop("state_hash", digest(state)),
        provider=fields.pop("provider", "google"),
        nonce=fields.pop("nonce", "nonce-1"),
        code_verifier=fields.pop("code_verifier", "verifier-1"),
        created_at=now,
        expires_at=fields.pop("expires_at", now + timedelta(minutes=10)),
        **fields,
    )
    session.add(row)
    session.flush()
    return row


def count(session: OrmSession, model) -> int:
    return session.scalar(sa.select(sa.func.count()).select_from(model))


# --- plain SQL ------------------------------------------------------------------

CREATED = utc(2026, 10, 16, 12)


def stored(moment: datetime) -> str:
    """A timestamp as SQLite holds one: UTC text with microseconds."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def fail_inserts(engine, table: str, *, name: str = None) -> None:
    """Make every INSERT into `table` fail (a simulated database outage). DDL runs as the
    migration/owner login on PostgreSQL; on SQLite it is the same file."""
    name = name or f"hh_fail_{table}"
    database = getattr(engine, "_hh_database", None)
    if database is None:  # a test's own SQLite engine
        ddl_engine = engine
    else:
        ddl_engine = database.migrate_engine()
    if engine.dialect.name == "sqlite":
        statement = (
            f"CREATE TRIGGER {name} BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'disk full'); END"
        )
    else:
        statement = (
            f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'disk full' USING ERRCODE = '23514'; END $$; "
            f"CREATE TRIGGER {name} BEFORE INSERT ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {name}()"
        )
    try:
        with ddl_engine.begin() as connection:
            connection.exec_driver_sql(statement)
    finally:
        if ddl_engine is not engine:
            ddl_engine.dispose()


def _mark(connection) -> str:
    """The driver's positional placeholder (SQLite `?`, psycopg `%s`)."""
    return "?" if connection.dialect.paramstyle == "qmark" else "%s"


def _bind(value):
    """A Python value as the driver takes it: UUIDs as text, JSON documents as text.
    (Plain SQL timestamps are `stored()` text, which both dialects accept.)"""
    if isinstance(value, uuid.UUID):
        return value.hex
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def raw_insert(session: OrmSession, table: str, **values) -> None:
    """INSERT one row with plain SQL and bound parameters. No ORM, no type
    processing beyond what the driver itself accepts, no listener: only the
    database's constraints stand in the way."""
    connection = session.connection()
    mark = _mark(connection)
    columns = ", ".join(values)
    marks = ", ".join(mark for _ in values)
    connection.exec_driver_sql(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})",
        tuple(_bind(value) for value in values.values()),
    )


def raw_update(session: OrmSession, table: str, where: str, where_args: tuple, **values) -> None:
    connection = session.connection()
    mark = _mark(connection)
    if mark != "?":  # the historical tests spell their conditions SQLite-style
        where = where.replace("?", mark)
    assignments = ", ".join(f"{column} = {mark}" for column in values)
    connection.exec_driver_sql(
        f"UPDATE {table} SET {assignments} WHERE {where}",
        (*[_bind(value) for value in values.values()], *[_bind(value) for value in where_args]),
    )


def _raw(value):
    """A Python value as plain SQL takes it: UUIDs as hex, datetimes as stored
    text, JSON documents as text."""
    if isinstance(value, uuid.UUID):
        return value.hex
    if isinstance(value, datetime):
        return stored(value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def user_row(**overrides) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "created_at": stored(CREATED),
        "updated_at": stored(CREATED),
        **overrides,
    }


def conversation_row(user: User, **overrides) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "user_id": user.id.hex,
        "created_at": stored(CREATED),
        "updated_at": stored(CREATED),
        **overrides,
    }


def message_row(conversation: Conversation, **overrides) -> dict:
    return {
        "conversation_id": conversation.id.hex,
        "role": "user",
        "content": "hello",
        "created_at": stored(CREATED),
        "expires_at": stored(CREATED + MESSAGE_RETENTION),
        **overrides,
    }


def entry_row(user: User, **overrides) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "user_id": user.id.hex,
        "provider": "telegram",
        "subject": "42",
        "created_at": stored(CREATED),
        **overrides,
    }


def link_row(user: User, **overrides) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "user_id": user.id.hex,
        "provider": "telegram",
        "subject": "42",
        "created_at": stored(CREATED),
        **overrides,
    }


def turn_row(conversation: Conversation, user_message_id: int, **overrides) -> dict:
    key = overrides.get("idempotency_key", "key-1")
    return {
        "id": uuid.uuid4().hex,
        "conversation_id": conversation.id.hex,
        "idempotency_key": key,
        "request_fingerprint": digest(key),
        "created_at": stored(CREATED),
        "updated_at": stored(CREATED),
        "user_message_id": user_message_id,
        "expires_at": stored(CREATED + MESSAGE_RETENTION),
        **overrides,
    }


def insert_turn(
    session: OrmSession, how: str, conversation: Conversation, user_message_id, **fields
) -> None:
    """The same request written through the ORM or as plain SQL (`how` is "orm"
    or "sql"), so that one test body can show that a rule holds on both paths.
    `fields` are column values; a UUID is converted for the SQL path."""
    key = fields.pop("idempotency_key", "key-1")
    if how == "orm":
        session.add(
            TurnRequest(
                conversation_id=fields.pop("conversation_id", conversation.id),
                idempotency_key=key,
                request_fingerprint=digest(key),
                user_message_id=user_message_id,
                **fields,
            )
        )
        session.flush()
    else:
        overrides = {name: _raw(value) for name, value in fields.items()}
        raw_insert(
            session,
            "turn_requests",
            **turn_row(conversation, user_message_id, idempotency_key=key, **overrides),
        )
