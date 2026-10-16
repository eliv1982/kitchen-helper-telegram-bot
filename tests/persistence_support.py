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
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.allowlist import allow
from house_helper.persistence.base import Base
from house_helper.persistence.database import (
    create_db_engine,
    create_session_factory,
    upgrade_database,
)
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
def engine(request, tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    if request.param == "migrated":
        upgrade_database(engine)
    else:
        Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine):
    return create_session_factory(engine)


@pytest.fixture
def session(factory):
    with factory() as session:
        yield session


@contextmanager
def rejected(session: OrmSession, constraint: str):
    """The block (plus the flush that follows it) must violate `constraint`.

    `constraint` is matched against the database's message, so a test cannot
    pass because some other rule happened to fail first. SQLite names a CHECK
    constraint, and for a UNIQUE one lists its columns.

    Runs in a SAVEPOINT, so what the test set up before the block survives the
    failure and the block can be repeated.
    """
    with pytest.raises(sa.exc.IntegrityError, match=constraint):
        with session.begin_nested():
            yield
            session.flush()


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
    now = fields.pop("created_at", datetime.now(timezone.utc))
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


def raw_insert(session: OrmSession, table: str, **values) -> None:
    """INSERT one row with plain SQL and bound parameters. No ORM, no type
    processing (UUIDs are given as hex, timestamps as stored() text), no
    listener: only the database's constraints stand in the way."""
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    session.connection().exec_driver_sql(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def raw_update(session: OrmSession, table: str, where: str, where_args: tuple, **values) -> None:
    assignments = ", ".join(f"{column} = ?" for column in values)
    session.connection().exec_driver_sql(
        f"UPDATE {table} SET {assignments} WHERE {where}", (*values.values(), *where_args)
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
