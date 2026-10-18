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

open_turn_request is the creation half of the turn lifecycle. The rest of this module
(begin_turn, complete_turn, fail_turn) is the smallest lifecycle the web chat needs:
one process, one attempt at a time, a lease that says how long an attempt may take.
It is deliberately NOT the turn-processing increment (R5) described at the end of
TurnRequest's docstring: there is no COMMITTING stage (the reply is generated outside
any transaction and stored together with the completion in one), no recovery of a
reply that was generated and not stored (the next attempt generates again), no
purge, and nothing for several workers competing for a queue. What it does hold to
from that list: every transition is one compare-and-swap UPDATE (status and
attempt_count in the WHERE, a row count of exactly 1 or the attempt has lost its
request), attempt_count is the fencing token, and the assistant message and the move
to COMPLETED are one transaction.

The lifecycle, per request: begin_turn stores the user's message and the request and
claims it (PENDING -> PROCESSING, attempt 1, a lease), or recognises a retry of a
request with the same idempotency key; the caller then generates the reply with no
transaction open; complete_turn stores the assistant message and moves to COMPLETED,
or fail_turn moves to FAILED (failure_kind generation). A FAILED request, and a
PROCESSING one whose lease has run out (its worker is gone), is claimed again by the
same key: attempt_count + 1, the earlier attempt's late answer is refused.

The lease. A lease is the exclusive right to complete the request until
lease_expires_at, and it is measured from the instant of the claim: begin_turn reads
the clock only after it holds the conversation lock (`clock` is a callable for that
reason; a time read before the lock would count the wait for the lock against the
lease). complete_turn checks it as well, against a clock read after ITS lock: an
attempt whose lease has run out cannot complete, whether or not anyone has claimed the
request since, and its answer is discarded (LeaseLost).

Expired attempts are retired, not left to linger. Under the conversation lock, an
admission (begin_turn, for a new key or a retry) first moves every other PROCESSING
request of the conversation whose lease has run out to FAILED (error_code
"interrupted", failure_kind generation, as if its worker had reported the failure). So
a request that has been left behind by a newer one can never complete afterwards: its
status is no longer PROCESSING and the compare-and-swap of its late completion finds
nothing. Together with "ConversationBusy while a lease is live" this is what keeps one
request in flight per conversation, and a conversation's history in the order
user, assistant, user, assistant: an assistant message is stored only by a completion
whose lease was live and whose request was not retired, under the lock, in the same
transaction. The retired request keeps its user message, shown as "failed" and left
out of every later context; it cannot be retried once something newer exists
(TurnSuperseded), like any other failed request.

One request per conversation is in flight at a time (ConversationBusy for another),
and a failed request can be retried only while it is the newest in its conversation
(TurnSuperseded): a retry appends the answer at the end of the history, so an older
request answered late would put it after messages that came later.

Locking, and the deadlock guarantee. Write first, like every writer in this package:
begin_turn, complete_turn and fail_turn start with an UPDATE of the conversation row
(scoped to its owner in begin_turn, which is also the ownership check). On SQLite that
takes the write lock before anything is read, so concurrent callers queue instead of
failing with "database is locked"; on PostgreSQL it takes the row lock, and the
statements that follow (the messages, the requests) see what the previous holder
committed. The UPDATE assigns updated_at to itself: only what really adds a message
moves the conversation's activity time.

The order of locks among the functions of this module is one: the conversation row
first, then the rows of its messages and requests. begin_turn, complete_turn and
fail_turn all follow it, and open_turn_request runs inside begin_turn, after the row
is locked. That consistent order is what these functions rely on to wait for one
another instead of deadlocking; it is a property of the order, not a general proof of
freedom from deadlock. Any other code that locks rows of a conversation (a
purge job, a reset) must take the conversation row first too, and open_turn_request
called on its own, by a caller that has not locked the conversation itself, is outside
the order: on PostgreSQL its INSERTs take only the shared key lock that the foreign key
needs, and its UPDATE of the conversation comes last. Production code does not call it
that way (only begin_turn calls it); tests/test_chat_postgres.py runs the orders that
can meet against a real server, which is evidence for those orders and no more.

Nothing here commits; the caller owns the transaction. begin_turn takes the user id of
the session and finds the conversation only through it; open_turn_request takes a
conversation already found through its owner and does not decide ownership.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Union
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.base import RETENTION, utcnow
from house_helper.persistence.conversations import ConversationNotFound, build_context
from house_helper.persistence.identifiers import validate_idempotency_key
from house_helper.persistence.models import (
    Conversation,
    FailureKind,
    Message,
    MessageRole,
    TurnRequest,
    TurnStatus,
)


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


# --- the lifecycle of the web chat ------------------------------------------------------


class ConversationBusy(Exception):
    """Another request of the conversation is being answered right now."""


class TurnInFlight(Exception):
    """This request is being answered right now, by the attempt that holds its lease."""


class TurnSuperseded(Exception):
    """This request can no longer be retried: the conversation has moved on since."""


class TurnUnusable(Exception):
    """The request of this key is in a state no retry can use: it has expired, was
    cancelled, or holds a reply that was never committed (not produced by this module)."""


class LeaseLost(Exception):
    """The attempt no longer owns its request: another attempt claimed it after the
    lease ran out, or it already ended. The caller's work is discarded; the
    transaction is rolled back by the exception."""


@dataclass(frozen=True)
class Claim:
    """An attempt that owns a request until its lease runs out."""

    turn_id: UUID
    conversation_id: UUID
    attempt: int
    retry: bool  # an earlier attempt failed or was abandoned
    user_message_id: int
    user_message_created_at: datetime
    context: list[dict[str, str]]  # what to send to the AI, ending with the user's message


@dataclass(frozen=True)
class StoredMessage:
    id: int
    text: str
    created_at: datetime


@dataclass(frozen=True)
class CompletedTurn:
    turn_id: UUID
    conversation_id: UUID
    user_message: StoredMessage
    assistant_message: StoredMessage
    truncated: bool


def _lock_conversation(session: OrmSession, user_id: UUID, conversation_id: UUID) -> Conversation:
    """The user's conversation, locked for writing (see the module docstring); the
    statement that decides ownership is a write. ConversationNotFound otherwise."""
    locked = session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
        .values(updated_at=Conversation.updated_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if locked != 1:
        raise ConversationNotFound
    return session.scalars(sa.select(Conversation).where(Conversation.id == conversation_id)).one()


def _refuse_if_busy(
    session: OrmSession, conversation_id: UUID, now: datetime, except_turn: Optional[UUID]
) -> None:
    query = sa.select(TurnRequest.id).where(
        TurnRequest.conversation_id == conversation_id,
        TurnRequest.status.in_((TurnStatus.PROCESSING.value, TurnStatus.COMMITTING.value)),
        TurnRequest.lease_expires_at > now,
    )
    if except_turn is not None:
        query = query.where(TurnRequest.id != except_turn)
    if session.scalar(query.limit(1)) is not None:
        raise ConversationBusy


# What a request retired for an expired lease says it failed with: the code the web chat
# uses for work that was cut off, because that is what it was (its worker is gone or too
# slow to be waited for).
RETIRED_ERROR_CODE = "interrupted"


def _retire_expired(
    session: OrmSession, conversation_id: UUID, now: datetime, except_turn: Optional[UUID]
) -> None:
    """Move every PROCESSING request of the conversation whose lease has run out (at
    `now`, read under the conversation lock) to FAILED, except `except_turn`, the one
    the caller is about to claim. After this, no such request can complete: its
    compare-and-swap needs status PROCESSING. COMMITTING is not touched: this module
    never makes it, and a request that holds a reply is not this module's to fail."""
    query = sa.update(TurnRequest).where(
        TurnRequest.conversation_id == conversation_id,
        TurnRequest.status == TurnStatus.PROCESSING.value,
        TurnRequest.lease_expires_at <= now,
    )
    if except_turn is not None:
        query = query.where(TurnRequest.id != except_turn)
    session.execute(
        query.values(
            status=TurnStatus.FAILED.value,
            failure_kind=FailureKind.GENERATION.value,
            error_code=RETIRED_ERROR_CODE,
            result_payload=None,
            finished_at=now,
            updated_at=now,
        ).execution_options(synchronize_session=False)
    )


def _stored(message: Message) -> StoredMessage:
    return StoredMessage(message.id, message.content, message.created_at)


def _completed(session: OrmSession, turn: TurnRequest) -> CompletedTurn:
    if turn.assistant_message_id is None:
        raise TurnUnusable
    user = session.get(Message, turn.user_message_id)
    assistant = session.get(Message, turn.assistant_message_id)
    payload = turn.result_payload or {}
    return CompletedTurn(
        turn_id=turn.id,
        conversation_id=turn.conversation_id,
        user_message=_stored(user),
        assistant_message=_stored(assistant),
        truncated=bool(payload.get("truncated")),
    )


def _claim(
    session: OrmSession,
    turn: TurnRequest,
    *,
    now: datetime,
    lease: timedelta,
    max_context_messages: int,
    retry: bool,
) -> Claim:
    attempt = turn.attempt_count + 1
    moved = session.execute(
        sa.update(TurnRequest)
        .where(
            TurnRequest.id == turn.id,
            TurnRequest.status == turn.status,
            TurnRequest.attempt_count == turn.attempt_count,
        )
        .values(
            status=TurnStatus.PROCESSING.value,
            attempt_count=attempt,
            processing_started_at=now,
            lease_expires_at=now + lease,
            finished_at=None,
            failure_kind=None,
            error_code=None,
            result_payload=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if moved != 1:  # cannot happen while the conversation is locked; the swap says so anyway
        raise TurnInFlight
    message = session.get(Message, turn.user_message_id)
    return Claim(
        turn_id=turn.id,
        conversation_id=turn.conversation_id,
        attempt=attempt,
        retry=retry,
        user_message_id=message.id,
        user_message_created_at=message.created_at,
        context=build_context(
            session,
            turn.conversation_id,
            message.id,
            now=now,
            max_messages=max_context_messages,
        ),
    )


def begin_turn(
    session: OrmSession,
    user_id: UUID,
    conversation_id: UUID,
    idempotency_key: object,
    text: str,
    *,
    clock: Callable[[], datetime],
    lease: timedelta,
    max_context_messages: int,
) -> Union[Claim, CompletedTurn]:
    """Accept a user's message for answering.

    `clock` returns the current time and is called once, after the conversation has
    been locked: that instant is the moment of the claim (the lease runs from it, the
    message and the request are made at it) and the one by which every other lease of
    the conversation is judged. A time taken before the lock would include the wait
    for it.

    A new key: any other request of the conversation whose lease has run out is retired
    (FAILED, "interrupted"), then the message and its request are stored
    (open_turn_request) and claimed; returns the Claim, whose `context` is what to send
    to the AI. The same key again: a COMPLETED request is returned as it was answered
    (CompletedTurn, nothing is run), a FAILED one or one whose lease has run out is
    claimed again (Claim, retry=True; the others whose leases have run out are retired
    as above), one that is being answered raises TurnInFlight.

    Raises ConversationNotFound (not the user's), IdempotencyKeyConflict (the key was
    used for different text), ConversationBusy (another request is being answered),
    TurnInFlight, TurnSuperseded and TurnUnusable (see their docstrings), ValueError
    for a key or text that is not one. Nothing is written for any of them: the
    caller's transaction rolls back with the exception.
    """
    key = validate_idempotency_key(idempotency_key)
    if not isinstance(text, str) or not text:
        raise ValueError("a request needs text")
    fingerprint = request_fingerprint(text)

    conversation = _lock_conversation(session, user_id, conversation_id)
    now = clock()  # the moment of the claim: after the lock, never before it
    existing = _find(session, conversation_id, key)

    if existing is None:
        _retire_expired(session, conversation_id, now, except_turn=None)
        _refuse_if_busy(session, conversation_id, now, except_turn=None)
        turn, _ = open_turn_request(session, conversation, key, text, now=now)
        return _claim(
            session, turn, now=now, lease=lease, max_context_messages=max_context_messages, retry=False
        )

    if existing.request_fingerprint != fingerprint:
        raise IdempotencyKeyConflict("this idempotency key was used with a different request")
    if existing.expires_at <= now:
        raise TurnUnusable
    status = existing.status
    if status == TurnStatus.COMPLETED.value:
        return _completed(session, existing)
    if status in (TurnStatus.PROCESSING.value, TurnStatus.COMMITTING.value):
        if existing.lease_expires_at is not None and existing.lease_expires_at > now:
            raise TurnInFlight
    elif status == TurnStatus.FAILED.value:
        if existing.failure_kind != FailureKind.GENERATION.value:
            raise TurnUnusable  # a reply is stored and waits for a commit: R5's to finish
    elif status != TurnStatus.PENDING.value:
        raise TurnUnusable  # cancelled

    # A retry: of a failure, of an abandoned attempt, or of a request left pending.
    newer = session.scalar(
        sa.select(TurnRequest.id)
        .where(
            TurnRequest.conversation_id == conversation_id,
            TurnRequest.user_message_id > existing.user_message_id,
        )
        .limit(1)
    )
    if newer is not None:
        raise TurnSuperseded
    _retire_expired(session, conversation_id, now, except_turn=existing.id)
    _refuse_if_busy(session, conversation_id, now, except_turn=existing.id)
    return _claim(
        session, existing, now=now, lease=lease, max_context_messages=max_context_messages, retry=True
    )


def complete_turn(
    session: OrmSession,
    claim: Claim,
    reply: str,
    *,
    truncated: bool,
    clock: Callable[[], datetime],
) -> CompletedTurn:
    """Store the assistant's reply and complete the request, as one transaction.

    `clock` is called once, after the conversation has been locked (see begin_turn).
    The conversation's activity time moves to that instant, the reply is stored as an
    assistant message that expires RETENTION after it, and the request moves
    PROCESSING -> COMPLETED with the reply in result_payload. LeaseLost if the attempt
    no longer owns the request: it ended or was retired, another attempt claimed it, or
    this attempt's own lease has run out at that instant (the exception rolls
    everything back, the message included).
    """
    if not isinstance(reply, str) or not reply:
        raise ValueError("a reply needs text")
    # The conversation is locked first. Whatever the answer below, only the transaction
    # that holds the lock reads the clock and decides who owns the request.
    locked = session.execute(
        sa.update(Conversation)
        .where(Conversation.id == claim.conversation_id)
        .values(updated_at=Conversation.updated_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if locked != 1:
        raise LeaseLost  # the conversation is gone, and the request with it
    now = clock()
    message = Message(
        conversation_id=claim.conversation_id,
        role=MessageRole.ASSISTANT.value,
        content=reply,
        created_at=now,
        expires_at=now + RETENTION,
    )
    session.add(message)
    session.flush()
    moved = session.execute(
        sa.update(TurnRequest)
        .where(
            TurnRequest.id == claim.turn_id,
            TurnRequest.status == TurnStatus.PROCESSING.value,
            TurnRequest.attempt_count == claim.attempt,
            TurnRequest.lease_expires_at > now,
        )
        .values(
            status=TurnStatus.COMPLETED.value,
            assistant_message_id=message.id,
            result_payload={"reply": reply, "truncated": truncated},
            finished_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if moved != 1:
        raise LeaseLost
    session.execute(
        sa.update(Conversation)
        .where(Conversation.id == claim.conversation_id)
        .values(updated_at=now)
        .execution_options(synchronize_session=False)
    )
    user = session.get(Message, claim.user_message_id)
    return CompletedTurn(
        turn_id=claim.turn_id,
        conversation_id=claim.conversation_id,
        user_message=_stored(user),
        assistant_message=_stored(message),
        truncated=truncated,
    )


def fail_turn(
    session: OrmSession, claim: Claim, error_code: str, *, clock: Callable[[], datetime]
) -> bool:
    """Move the attempt's request PROCESSING -> FAILED (failure_kind generation, so a
    retry generates a new reply). `error_code` is stored as given (at most 64
    characters). `clock` is called once, after the conversation has been locked. False,
    with nothing changed, if the attempt no longer owns the request (it ended, was
    retired, or another attempt claimed it): whoever does decides what happens to it.
    An attempt whose lease has run out but that nobody has replaced may still fail its
    request: that only lets the same key retry at once instead of waiting."""
    locked = session.execute(
        sa.update(Conversation)
        .where(Conversation.id == claim.conversation_id)
        .values(updated_at=Conversation.updated_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if locked != 1:
        return False
    now = clock()
    moved = session.execute(
        sa.update(TurnRequest)
        .where(
            TurnRequest.id == claim.turn_id,
            TurnRequest.status == TurnStatus.PROCESSING.value,
            TurnRequest.attempt_count == claim.attempt,
        )
        .values(
            status=TurnStatus.FAILED.value,
            failure_kind=FailureKind.GENERATION.value,
            error_code=error_code,
            result_payload=None,
            finished_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    return moved == 1
