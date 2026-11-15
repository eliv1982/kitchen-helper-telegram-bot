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
from typing import Optional

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
    decide; the message is never searched for the expected name first."""
    if dialect != "postgresql":
        if re.search(constraint, failure.message):
            return
        raise AssertionError(f"the database did not name the expected rule: {failure.message}")
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


_INSERT = re.compile(
    r"^\s*INSERT\s+INTO\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?\"?([A-Za-z_][A-Za-z0-9_]*)\"?\s*"
    r"\(([^)]*)\)\s*VALUES\s*\((.*)\)\s*(?:RETURNING\s+.*)?$",
    re.IGNORECASE | re.DOTALL,
)
_NAMED_PARAM = re.compile(r"%\(([^)]+)\)s")


def _split_top_level(text: str) -> list[str]:
    """Split a VALUES/SET list on commas that are not inside single quotes."""
    parts, current, quoted = [], [], False
    for character in text:
        if character == "'":
            quoted = not quoted
        if character == "," and not quoted:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    parts.append("".join(current).strip())
    return parts


def overlong_columns(statement: Optional[str], params: object, table: str) -> Optional[set[str]]:
    """The columns of `table` whose BOUND value exceeds the column's declared width.

    R3-04: the isolated-input proof for a 22001. `None` means the statement could not be
    read, which is a refusal (the helper must not accept a width error it cannot attribute
    to the expected column). A different column of the same declared width is named, so a
    same-width sibling can never satisfy the assertion."""
    if not statement or table not in Base.metadata.tables:
        return None
    match = _INSERT.match(statement)
    if match is None or match.group(1).lower() != table:
        return None
    columns = [part.strip().strip('"').split(".")[-1].strip('"') for part in match.group(2).split(",")]
    values = _split_top_level(match.group(3))
    if len(columns) != len(values):
        return None
    names = _NAMED_PARAM.search(match.group(3)) is not None
    if names:
        # One dict per row; SQLAlchemy may also hand a tuple of dicts for executemany.
        entries: list[object] = list(params) if isinstance(params, list) else [params]
    elif isinstance(params, list):
        entries = list(params)
    else:
        entries = [params]
    offending: set[str] = set()
    for entry in entries:
        for index, placeholder in enumerate(values):
            column = columns[index]
            declared = Base.metadata.tables[table].c.get(column)
            limit = None if declared is None else getattr(declared.type, "length", None)
            if limit is None:
                continue
            if names:
                if not isinstance(entry, dict):
                    continue
                name = _NAMED_PARAM.search(placeholder)
                if name is None:
                    continue
                value = entry.get(name.group(1))
            else:
                if not isinstance(entry, (tuple, list)):
                    continue
                value = entry[index] if index < len(entry) else None
            if isinstance(value, str) and len(value) > limit:
                offending.add(column)
    return offending


def assert_expected_width_rejection(table: str, column: str, failure: Failure) -> None:
    """The one decision `rejected_width` makes for PostgreSQL, pure and testable."""
    limit = getattr(Base.metadata.tables[table].c[column].type, "length", None)
    constraint = f"ck_{table}_{column}_max_length"
    if limit is None:
        # A TEXT column: there is no type width to fire first, so the named CHECK is the
        # rule on PostgreSQL too (and it must be that CHECK, not another one).
        assert failure.state == "23514" and failure.constraint_name == constraint, failure.message
        return
    assert failure.state == "22001", failure.message
    assert f"character varying({limit})" in failure.message, failure.message
    # R3-04: the width alone does not name the column; another column of the same width
    # could have overflowed. The bound statement must show THIS column as the only one
    # over its declared width.
    offending = overlong_columns(failure.statement, failure.params, table)
    assert offending is not None, (
        "the width refusal could not be attributed to a column: the statement that "
        f"failed was not readable ({failure.statement!r})"
    )
    assert offending == {column}, (
        f"the width refusal names {sorted(offending)} and not only {column!r}"
    )


@contextmanager
def rejected(session: OrmSession, constraint: str):
    """The block (plus the flush that follows it) must violate `constraint`.

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


@contextmanager
def rejected_width(session: OrmSession, table: str, column: str):
    """The block must be refused because `table.column`'s declared width is exceeded.

    SQLite does not enforce `VARCHAR(n)`, so its `ck_<table>_<column>_max_length` CHECK
    fires. PostgreSQL refuses the value at the type's width before the CHECK can run
    (SQLSTATE 22001, "value too long for type character varying(n)"). That is accepted
    ONLY here, with the exact column's declared limit read from the model, and - R3-04 -
    only when the failing statement shows this column (and no other column of the same
    declared width) as the one whose bound value is over its limit.
    """
    constraint = f"ck_{table}_{column}_max_length"
    with pytest.raises((sa.exc.IntegrityError, sa.exc.DataError)) as caught:
        with session.begin_nested():
            yield
            session.flush()

    failure = describe(caught.value)
    if session.get_bind().dialect.name == "sqlite":
        assert re.search(constraint, failure.message), failure.message
        return
    assert_expected_width_rejection(table, column, failure)


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
