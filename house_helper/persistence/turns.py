"""Opening a turn request: the supported way to create one.

A turn request must never exist without recoverable input, so it is not created
alone. open_turn_request stores the user's message and the request that links it
in one step (a SAVEPOINT inside the caller's transaction): either both exist or
neither does, and a request that loses the race for its idempotency key leaves no
orphan message behind. The conversation's updated_at moves in the same step. The
text is stored once, in the history; recovery reads it from the message the
request links (see TurnRequest). The retention clock of both rows starts at the
same instant, so the request never outlives the message it copies from.

The savepoint is only as atomic as the transaction around it: if the caller rolls
back, all of it goes. (database.py makes SQLite open that transaction before the
first statement, so a savepoint is never the outermost one and its release never
commits.)

This is the creation half of the turn lifecycle only. Claiming, leasing,
generating, committing and recovering a request are the turn-processing
increment (R5); see the notes at the end of TurnRequest's docstring.

Nothing here commits; the caller owns the transaction. The conversation passed
in must have been found through its owner (a lookup scoped by the session's
user): this module takes no user id and does not decide ownership.
"""

import hashlib
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.base import RETENTION, utcnow
from house_helper.persistence.identifiers import validate_idempotency_key
from house_helper.persistence.models import Conversation, Message, MessageRole, TurnRequest


class IdempotencyKeyConflict(Exception):
    """The idempotency key was used before with a different request body: a
    mistake by the client, not a retry, and never answered with the old result."""


def request_fingerprint(text: str) -> str:
    """The SHA-256 hex digest of the request body, which for now is the text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find(session: OrmSession, conversation_id, key: str) -> Optional[TurnRequest]:
    return session.scalar(
        sa.select(TurnRequest).where(
            TurnRequest.conversation_id == conversation_id, TurnRequest.idempotency_key == key
        )
    )


def _replay(existing: TurnRequest, fingerprint: str) -> tuple[TurnRequest, bool]:
    if existing.request_fingerprint != fingerprint:
        raise IdempotencyKeyConflict("this idempotency key was used with a different request")
    return existing, False


def open_turn_request(
    session: OrmSession,
    conversation: Conversation,
    idempotency_key: object,
    text: str,
    *,
    now: Optional[datetime] = None,
) -> tuple[TurnRequest, bool]:
    """Store `text` as the user's message and open a pending request for it.

    Returns (request, created). A repeated key with the same text returns the
    existing request and False, leaving everything as it was; the same key with
    different text raises IdempotencyKeyConflict. ValueError for an invalid key
    or empty text. The conversation's updated_at moves to `now`.
    """
    key = validate_idempotency_key(idempotency_key)
    if not isinstance(text, str) or not text:
        raise ValueError("a request needs text")
    fingerprint = request_fingerprint(text)

    if conversation.id is None:  # a conversation that is still pending gets its id on flush
        session.flush()
    existing = _find(session, conversation.id, key)
    if existing is not None:
        return _replay(existing, fingerprint)

    now = utcnow() if now is None else now
    try:
        with session.begin_nested():
            message = Message(
                conversation_id=conversation.id,
                role=MessageRole.USER.value,
                content=text,
                created_at=now,
                expires_at=now + RETENTION,
            )
            session.add(message)
            session.flush()
            turn = TurnRequest(
                conversation_id=conversation.id,
                idempotency_key=key,
                request_fingerprint=fingerprint,
                user_message_id=message.id,
                created_at=now,
                expires_at=now + RETENTION,
            )
            session.add(turn)
            session.flush()
            # Inside the savepoint: the message, the request and the
            # conversation's activity time stand or fall together, whatever the
            # caller does with an error from here.
            conversation.updated_at = now
            session.flush()
    except sa.exc.IntegrityError:
        # Another writer took the key between the lookup and the insert; the
        # savepoint took the message back with it. If that is not what
        # happened, the error is something else.
        existing = _find(session, conversation.id, key)
        if existing is None:
            raise
        return _replay(existing, fingerprint)

    return turn, True
