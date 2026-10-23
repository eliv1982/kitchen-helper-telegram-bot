"""Builders and readers for the purge tests (test_admin_retention*.py).

Everything is synthetic and lives in a SQLite file under pytest's tmp_path that was migrated to
the head (the purge refuses any other schema). Rows are written through the ORM with EXPLICIT
timestamps, so a test says how old each row is relative to NOW and nothing depends on the real
clock. The database is read back with plain SQL on a fresh engine, as tests/admin_support.py
does, so a test sees what is committed.
"""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import sqlalchemy as sa

from house_helper.persistence.allowlist import allow
from house_helper.persistence.base import RETENTION
from house_helper.persistence.database import create_db_engine, create_session_factory
from house_helper.persistence.models import (
    Conversation,
    EmailLoginChallenge,
    IdentityInvitation,
    IdentityLink,
    Message,
    OidcState,
    Session,
    TurnRequest,
    User,
)

NOW = datetime(2026, 10, 23, 3, 30, tzinfo=timezone.utc)
CUTOFF = NOW - RETENTION
MICROSECOND = timedelta(microseconds=1)


def ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Seed:
    """Writes synthetic rows into one ORM session. Every method flushes and returns the row."""

    def __init__(self, session) -> None:
        self.session = session
        self._counter = 0

    def tag(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _add(self, row):
        self.session.add(row)
        self.session.flush()
        return row

    # --- who -----------------------------------------------------------------------------

    def user(self, name: Optional[str] = None) -> User:
        return self._add(User(display_name=name or self.tag("synthetic-user")))

    def telegram(self, user: User, subject: Optional[str] = None) -> IdentityLink:
        """A provisioned Telegram identity of `user` that has signed in once."""
        subject = subject or str(1000 + self._counter + 1)
        self._counter += 1
        allow(self.session, user, "telegram", subject)
        return self._add(IdentityLink(user_id=user.id, provider="telegram", subject=subject))

    def email_address(self, user: User, address: Optional[str] = None) -> str:
        address = address or f"{self.tag('person')}@example.com"
        allow(self.session, user, "email", address)
        return address

    # --- authentication artifacts ---------------------------------------------------------

    def login_session(
        self, user: User, link: IdentityLink, *, created: datetime, expires: datetime, revoked_at=None
    ) -> Session:
        return self._add(
            Session(
                user_id=user.id,
                identity_link_id=link.id,
                token_hash=digest(self.tag("session")),
                created_at=created,
                last_seen_at=created,
                expires_at=expires,
                revoked_at=revoked_at,
            )
        )

    def login_state(
        self, *, created: datetime, expires: datetime, consumed_at=None, invitation=None
    ) -> OidcState:
        return self._add(
            OidcState(
                state_hash=digest(self.tag("state")),
                provider="telegram",
                nonce="synthetic-nonce",
                code_verifier="synthetic-verifier",
                created_at=created,
                expires_at=expires,
                consumed_at=consumed_at,
                invitation_id=None if invitation is None else invitation.id,
            )
        )

    def challenge(
        self, user: User, address: str, *, created: datetime, expires: datetime,
        consumed_at=None, revoked_at=None,
    ) -> EmailLoginChallenge:
        return self._add(
            EmailLoginChallenge(
                user_id=user.id,
                provider="email",
                subject=address,
                token_hash=digest(self.tag("challenge")),
                created_at=created,
                expires_at=expires,
                consumed_at=consumed_at,
                revoked_at=revoked_at,
            )
        )

    def invitation(
        self, user: User, *, created: datetime, expires: datetime, consumed_at=None, revoked_at=None
    ) -> IdentityInvitation:
        return self._add(
            IdentityInvitation(
                user_id=user.id,
                token_hash=digest(self.tag("invitation")),
                created_at=created,
                expires_at=expires,
                consumed_at=consumed_at,
                revoked_at=revoked_at,
            )
        )

    # --- history ------------------------------------------------------------------------------

    def conversation(self, user: User, *, created: datetime, updated: Optional[datetime] = None):
        return self._add(Conversation(user_id=user.id, created_at=created, updated_at=updated or created))

    def message(
        self, conversation: Conversation, role: str, text: str, *, created: datetime,
        expires: Optional[datetime] = None,
    ) -> Message:
        return self._add(
            Message(
                conversation_id=conversation.id,
                role=role,
                content=text,
                created_at=created,
                expires_at=expires or created + RETENTION,
            )
        )

    def exchange(
        self, conversation: Conversation, *, created: datetime, status: str = "completed",
        lease: Optional[datetime] = None, text: Optional[str] = None,
        answered_after: timedelta = timedelta(seconds=5), answered: bool = False,
    ) -> SimpleNamespace:
        """A user message and the turn request made from it, in `status`, and, for a completed
        one, the assistant message that answered it `answered_after` later (as the lifecycle
        does: the request is created with the question, the answer comes afterwards).
        `answered` gives a COMMITTING request its assistant message too: the reconciliation
        state in which the answer is stored and the status has not yet moved on."""
        key = self.tag("key")
        text = text or key
        asked = self.message(conversation, "user", f"question {text}", created=created)
        fields: dict = {}
        answer = None
        if status == "completed":
            answer = self.message(conversation, "assistant", f"answer {text}", created=created + answered_after)
            fields.update(
                assistant_message_id=answer.id,
                result_payload={"text": f"answer {text}"},
                finished_at=created + answered_after,
            )
        elif status == "failed":
            fields.update(error_code="synthetic", failure_kind="generation", finished_at=created + answered_after)
        elif status == "cancelled":
            fields.update(finished_at=created + answered_after)
        elif status == "processing":
            fields.update(processing_started_at=created, lease_expires_at=lease or created + timedelta(minutes=2))
        elif status == "committing":
            fields.update(
                result_payload={"text": f"answer {text}"},
                processing_started_at=created,
                lease_expires_at=lease or created + timedelta(minutes=2),
            )
            if answered:
                answer = self.message(conversation, "assistant", f"answer {text}", created=created + answered_after)
                fields["assistant_message_id"] = answer.id
        elif status != "pending":
            raise ValueError(status)
        request = self._add(
            TurnRequest(
                conversation_id=conversation.id,
                idempotency_key=key,
                request_fingerprint=digest(text),
                status=status,
                user_message_id=asked.id,
                created_at=created,
                updated_at=created,
                expires_at=created + RETENTION,
                **fields,
            )
        )
        return SimpleNamespace(asked=asked, answer=answer, request=request)


# --- writing and reading a database file -------------------------------------------------------


def seeded(url: str, fill) -> SimpleNamespace:
    """Open `url` with the persistence layer, call fill(Seed) in one transaction, commit.
    Returns what fill returned."""
    engine = create_db_engine(url)
    try:
        factory = create_session_factory(engine)
        with factory() as session, session.begin():
            return fill(Seed(session))
    finally:
        engine.dispose()


def read(url: str, sql: str, **params) -> list:
    engine = create_db_engine(url)
    try:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(sa.text(sql), params)]
    finally:
        engine.dispose()


def table_names(url: str) -> list:
    return [
        name
        for (name,) in read(
            url, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def dump(url: str) -> dict:
    """Every row of every table, as sorted text: equal dumps mean equal contents."""
    return {name: sorted(map(repr, read(url, f'SELECT * FROM "{name}"'))) for name in table_names(url)}


def count(url: str, table: str) -> int:
    return read(url, f'SELECT count(*) FROM "{table}"')[0][0]


def counts(url: str) -> dict:
    return {name: count(url, name) for name in table_names(url)}


def integrity(url: str) -> None:
    """The database is sound and every foreign key holds (raises AssertionError otherwise)."""
    connection = sqlite3.connect(url.removeprefix("sqlite:///"))
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
