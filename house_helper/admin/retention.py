"""Deleting what has expired: the work of `python -m house_helper.admin purge-expired`.

Two kinds of rows are deleted, for real (a DELETE, not a filter on reads):

  * authentication artifacts past THEIR OWN expiry: sessions, OIDC login states, emailed
    sign-in links, Telegram invitations and Telegram pairing codes. The rule is one for all of them: a row goes
    when its expires_at is not after `now` (the exclusive end the application itself uses:
    at expires_at it is over). Whether it was used, withdrawn or revoked does not shorten
    or lengthen that: a used or revoked row stays until its own deadline, which is at most
    the artifact's lifetime (10 minutes to 14 days), so no new retention period exists, and
    after the row is gone every reader sees "unknown", which it already treats exactly like
    "expired". An invitation also waits until no login state is bound to it any more (the
    state's foreign key cascades, and a half-finished login must not lose its invitation
    row under it);
  * conversation history: messages, and the turn requests that hold a copy of an answer
    (result_payload), at most 365 x 24 hours after their own creation: a row is old when
    expires_at <= now OR created_at <= now - 365 days. The second clause is the policy
    ceiling spelled from the creation time, so that a row whose stored deadline is wrong
    (a restored or hand-edited database; the CHECKs would stop it on a migrated one) is
    still removed. A conversation that is empty and has seen no activity for 365 days goes
    too. A long-lived conversation keeps its recent messages: rows are judged one by one.
    The photos that came with messages (message_attachments) are history too, by the same rule:
    a photo takes the deadline of its message when it is attached, so it goes when the message
    does, and a photo that was uploaded and never sent (an hour's life) goes with the rest of
    the expired. They are deleted before the messages, so that they are counted; the foreign key
    from a photo to its message cascades, so a message can never be left holding one.

Never touched: users, allowlist entries, identity links, the Telegram links (telegram_links:
the tie between a person and their Telegram account, removed by unlinking and with the user),
anything alembic owns, and any
table that is not named here (the favourites are one: favorite_recipes holds copies of answers
that a person chose to keep, with no deadline and no key to the history, and this module does not
know it; the long-term preferences of the product requirements are not in this schema yet, and
when they arrive they will be independent tables too). A new table is therefore safe by default.

Order, which is what the foreign keys demand: sessions, login states, emailed links,
invitations (after the states bound to them), pairing codes, turn requests, photos, messages,
conversations. A
message that a turn request links cannot be deleted, so requests go first (models.TurnRequest
explains), and a message that a request still links is kept for the next run (it
is reported as held back; with the application's own writers that is only a request still in
flight under a live lease, since the application stores an answer after its request). "Still
links" is judged on the rows that exist when the statement runs, whether or not they are eligible
themselves: a request that an earlier stage skipped because another transaction held its lock is
eligible and still there, and the same holds for a login state under an invitation and for the
messages and requests of a conversation. So no stage deletes a parent, or takes a child with it
by a cascade, on the strength of a deletion that did not happen; whatever was skipped is picked up
by the next run. (A dry run has no such state to look at: _Conditions explains what it counts.) An
expired request that is still in flight is resolved before it is deleted, as models.TurnRequest
asks: it is CANCELLED, and only then deleted as the terminal row it has become; one whose lease
is still live is left alone and so are its messages.

Concurrency. This module takes no lock of its own: the operator tool runs it under the shared
maintenance lock (`exec --shared`), beside the web service, which is the lock model of
deploy/RUNBOOK.md. What makes it safe beside the web service and beside a second run is the
shape of the work:

  * every batch is ONE statement, in its own short transaction, whose first (and only)
    statement is the write: SQLite takes the write lock there and then and waits its turn
    for it (the persistence layer's rule, see persistence.database), so there is no
    read-then-write snapshot to go stale, and the lock is held for the time of at most
    `batch_size` deletions, not of the whole purge;
  * a batch is `DELETE ... WHERE id IN (SELECT id ... LIMIT n)`: it decides and deletes in
    one statement, so two runs at the same time never delete a row twice, never miss one
    that stays eligible, and never see each other half done;
  * a pause between batches lets the web service's own writers in, and the busy timeout is
    long enough to wait for them;
  * on PostgreSQL (not deployed; checked by an opt-in test) the same shape holds with its own
    means: a batch runs at READ COMMITTED whatever the database's default level is, and picks
    its rows FOR UPDATE SKIP LOCKED, so two runs, or a run and the application, take different
    rows instead of waiting for each other, failing to serialize or deadlocking over them. A
    row skipped that way is not deleted by this run, and neither is anything that depends on
    it (see the order, above), so the run finishes without an error and the next one does the rest;
  * a run that is killed leaves, at worst, the batch in progress rolled back; everything
    committed before stays, and the same command finishes the job.

What a DELETE is, and is not. Rows are gone from every table and index of the database. In
SQLite the freed pages go to the free list and are reused by later writes, the file does not
shrink, and the write-ahead log still holds earlier page images until it is overwritten;
nothing here VACUUMs, checkpoints or replaces the database, because that is not safe beside
a running service. `secure_delete` is switched on for this run's connection, so the bytes of
the pages this run frees are overwritten in the database file; that is a mitigation, not
forensic erasure, and it does not reach older backups, the log, or the disk beneath the file
(deploy/RUNBOOK.md, section 14, says so).

The clock is a parameter and the cutoff derives from it once: one run uses one `now`. The
command line has no way to give another, deliberately: a mistyped date would delete a year of
history.

Nothing printed or raised here carries a token, a hash, an address, a name or the text of a
message: the report is counts and the two timestamps.
"""

import enum
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, cast

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from house_helper.admin.target import check_schema_at_head
from house_helper.persistence import models
from house_helper.persistence.base import RETENTION
from house_helper.persistence.models import TurnStatus

# Rows per statement, and so per transaction. The write lock is held for this many
# deletions at a time; small enough that the web service's writers wait for milliseconds.
BATCH_SIZE = 500

# Between two full batches, so that a writer that is waiting for the lock gets it.
PAUSE_SECONDS = 0.05

# How long a statement of this run waits for SQLite's write lock before it gives up (the
# default of the sqlite3 driver is 5 s). A refusal then is visible, not a hang.
BUSY_TIMEOUT_MS = 30_000

_TERMINAL = (TurnStatus.COMPLETED.value, TurnStatus.FAILED.value, TurnStatus.CANCELLED.value)
_IN_FLIGHT = (TurnStatus.PENDING.value, TurnStatus.PROCESSING.value, TurnStatus.COMMITTING.value)


def _table(model: Any) -> sa.Table:
    """The Table of a mapped class (SQLAlchemy types `__table__` as a bare FromClause)."""
    return cast(sa.Table, model.__table__)


_sessions = _table(models.Session)
_oidc_states = _table(models.OidcState)
_challenges = _table(models.EmailLoginChallenge)
_invitations = _table(models.IdentityInvitation)
_link_codes = _table(models.TelegramLinkCode)
_requests = _table(models.TurnRequest)
_attachments = _table(models.MessageAttachment)
_messages = _table(models.Message)
_conversations = _table(models.Conversation)


class Category(str, enum.Enum):
    """What the report counts, in the order the rows are deleted. The value is the table."""

    SESSIONS = "sessions"
    OIDC_STATES = "oidc_states"
    EMAIL_LOGIN_CHALLENGES = "email_login_challenges"
    IDENTITY_INVITATIONS = "identity_invitations"
    TELEGRAM_LINK_CODES = "telegram_link_codes"
    TURN_REQUESTS = "turn_requests"
    MESSAGE_ATTACHMENTS = "message_attachments"
    MESSAGES = "messages"
    CONVERSATIONS = "conversations"


@dataclass(frozen=True)
class PurgeReport:
    """What a run deleted (or, for a dry run, would delete). Counts and timestamps only."""

    dry_run: bool
    now: datetime
    cutoff: datetime  # now - 365 x 24 hours: history created at or before it is gone
    deleted: Dict[str, int]  # per Category.value
    # Included in deleted["turn_requests"]: expired requests that were still in flight with
    # no live lease and were cancelled before they were deleted. (Beside another run the
    # cancelling and the deleting of one request can fall to two runs; each counts its own.)
    cancelled_in_flight: int = 0
    # Expired messages that stay for now because a request that cannot be deleted yet still
    # links them. Not included in deleted.
    held_back_messages: int = 0

    @property
    def total(self) -> int:
        return sum(self.deleted.values())


class PurgeAborted(Exception):
    """The run stopped before it finished. `done` counts what the batches that committed
    deleted; the batch that was in progress was rolled back. The original exception is the
    __cause__ (and may carry SQL text: print its class, nothing else)."""

    def __init__(self, stage: str, done: Dict[str, int], cancelled_in_flight: int) -> None:
        super().__init__(stage)
        self.stage = stage
        self.done = done
        self.cancelled_in_flight = cancelled_in_flight


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("the purge clock must be timezone-aware")
    return moment.astimezone(timezone.utc)


# --- what is eligible -----------------------------------------------------------------------


class _Conditions:
    """The SQL conditions for one run's `now`. Every method takes the table, or an alias of
    it, that the condition is about, and returns an expression over that alias only (the
    correlated subqueries name their own aliases), so the same condition serves the batch
    that deletes and the count of a dry run.

    A parent row (an invitation, a message, a conversation) may go only when nothing that
    depends on it is left, and what "left" means differs between the two uses:

      * A real run (`planned=False`) looks at the rows that EXIST when the statement runs. A
        dependent that is eligible for deletion is still there if its own stage skipped it (a
        second run or the web service holds its lock and SKIP LOCKED passed it by), and it
        blocks its parent all the same: a message under a request that links it is a foreign-key
        error, an invitation or conversation over a login state, message or request would take
        it along by a cascade that no count reports and that has to wait for the lock.
      * A dry run (`planned=True`) deletes nothing, so there is nothing to look at but one
        snapshot, and it counts as gone what the earlier stages would delete from that snapshot.
        It is a preview of a run that skips nothing and meets no concurrent change; it equals the
        real run only under those conditions (deploy/RUNBOOK.md, section 14.2)."""

    def __init__(self, now: datetime, cutoff: datetime, *, planned: bool = False) -> None:
        self.now = now
        self.cutoff = cutoff
        self.planned = planned

    def remains(self, dependent, goes: Callable) -> sa.ColumnElement:
        """`dependent`, a row that depends on the one being judged, is still there when the
        parent's stage runs. `goes` is the condition under which the dependent's own stage deletes it."""
        return ~goes(dependent) if self.planned else sa.true()

    def over(self, row) -> sa.ColumnElement:
        """The artifact's own deadline has passed (a session, a login state, a link)."""
        return row.c.expires_at <= self.now

    def invitation(self, row) -> sa.ColumnElement:
        bound = _oidc_states.alias()
        login_still_bound = (
            sa.exists().where(bound.c.invitation_id == row.c.id, self.remains(bound, self.over)).correlate(row)
        )
        return sa.and_(self.over(row), ~login_still_bound)

    def attachment(self, row) -> sa.ColumnElement:
        """A photo is as old as its message (or as an upload that was never sent): the same
        two clauses as for a message. Nothing depends on it."""
        return sa.or_(row.c.expires_at <= self.now, row.c.created_at <= self.cutoff)

    def request_expired(self, row) -> sa.ColumnElement:
        return sa.or_(row.c.expires_at <= self.now, row.c.created_at <= self.cutoff)

    def request_abandoned(self, row) -> sa.ColumnElement:
        """Still in flight, and nobody holds its lease: its worker is gone."""
        return sa.and_(
            row.c.status.in_(_IN_FLIGHT),
            sa.or_(row.c.lease_expires_at.is_(None), row.c.lease_expires_at <= self.now),
        )

    def request(self, row) -> sa.ColumnElement:
        """May be deleted: expired, and not in flight under a live lease."""
        return sa.and_(
            self.request_expired(row),
            sa.or_(row.c.status.in_(_TERMINAL), self.request_abandoned(row)),
        )

    def message_expired(self, row) -> sa.ColumnElement:
        return sa.or_(row.c.expires_at <= self.now, row.c.created_at <= self.cutoff)

    def message(self, row) -> sa.ColumnElement:
        """May be deleted: expired, and no request that links it is left (the foreign keys
        would refuse otherwise)."""
        asked, answered = _requests.alias(), _requests.alias()
        kept_by_input = (
            sa.exists()
            .where(asked.c.user_message_id == row.c.id, self.remains(asked, self.request))
            .correlate(row)
        )
        kept_by_answer = (
            sa.exists()
            .where(answered.c.assistant_message_id == row.c.id, self.remains(answered, self.request))
            .correlate(row)
        )
        return sa.and_(self.message_expired(row), ~kept_by_input, ~kept_by_answer)

    def held_back(self, row) -> sa.ColumnElement:
        """Dry run only: the real run counts the expired messages that exist when it is done."""
        return sa.and_(self.message_expired(row), ~self.message(row))

    def conversation(self, row) -> sa.ColumnElement:
        """May be deleted: no activity for 365 days and nothing left in it."""
        message, request = _messages.alias(), _requests.alias()
        has_message_left = (
            sa.exists().where(message.c.conversation_id == row.c.id, self.remains(message, self.message)).correlate(row)
        )
        has_request_left = (
            sa.exists().where(request.c.conversation_id == row.c.id, self.remains(request, self.request)).correlate(row)
        )
        return sa.and_(
            row.c.updated_at <= self.cutoff,
            row.c.created_at <= self.cutoff,
            ~has_message_left,
            ~has_request_left,
        )


def _count(connection: Connection, table, condition: sa.ColumnElement) -> int:
    return connection.execute(sa.select(sa.func.count()).select_from(table).where(condition)).scalar_one()


def _stages(conditions: _Conditions):
    """(category, table, condition-for-an-alias), in deletion order."""
    return (
        (Category.SESSIONS, _sessions, conditions.over),
        (Category.OIDC_STATES, _oidc_states, conditions.over),
        (Category.EMAIL_LOGIN_CHALLENGES, _challenges, conditions.over),
        (Category.IDENTITY_INVITATIONS, _invitations, conditions.invitation),
        (Category.TELEGRAM_LINK_CODES, _link_codes, conditions.over),
        (Category.TURN_REQUESTS, _requests, conditions.request),
        (Category.MESSAGE_ATTACHMENTS, _attachments, conditions.attachment),
        (Category.MESSAGES, _messages, conditions.message),
        (Category.CONVERSATIONS, _conversations, conditions.conversation),
    )


def _delete_batch(table, condition: Callable, size: int) -> sa.Delete:
    chosen = table.alias()
    # FOR UPDATE SKIP LOCKED: on PostgreSQL a second run takes other rows instead of waiting for
    # (and colliding with) the first one's; SQLite has no row locks, and SQLAlchemy leaves the
    # clause out of its SQL.
    ids = sa.select(chosen.c.id).where(condition(chosen)).limit(size).with_for_update(skip_locked=True)
    return sa.delete(table).where(table.c.id.in_(ids))


def _cancel_batch(conditions: _Conditions, size: int) -> sa.Update:
    chosen = _requests.alias()
    ids = (
        sa.select(chosen.c.id)
        .where(conditions.request_expired(chosen), conditions.request_abandoned(chosen))
        .limit(size)
        .with_for_update(skip_locked=True)
    )
    return (
        sa.update(_requests)
        # Compare and swap: the condition is checked again, in the write itself.
        .where(_requests.c.id.in_(ids), conditions.request_abandoned(_requests))
        .values(
            status=TurnStatus.CANCELLED.value,
            finished_at=conditions.now,
            updated_at=conditions.now,
        )
    )


# --- the connection ----------------------------------------------------------------------------


@contextmanager
def _connection_settings(connection: Connection, *, dry_run: bool) -> Iterator[None]:
    """The per-connection settings of this run, put back afterwards, before any transaction of it.

    PostgreSQL: the isolation level is set here and not left to the database's default (the
    harness of the PostgreSQL tests makes that REPEATABLE READ on purpose, where a second run's
    delete of a row the first just deleted would fail with a serialization error): READ COMMITTED
    for the batches, REPEATABLE READ for a dry run, so that its counts are one snapshot.

    SQLite: a long busy wait, and secure_delete on; refuses to run when foreign keys are not
    enforced, because the order of the deletions relies on them to refuse what must not be
    deleted."""
    if connection.dialect.name == "postgresql":
        connection.execution_options(isolation_level="REPEATABLE READ" if dry_run else "READ COMMITTED")
        yield
        return
    if connection.dialect.name != "sqlite":
        yield
        return
    raw: Any = connection.connection.driver_connection
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("the connection does not enforce foreign keys")
    before = {name: raw.execute(f"PRAGMA {name}").fetchone()[0] for name in ("busy_timeout", "secure_delete")}
    raw.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    raw.execute("PRAGMA secure_delete = ON")
    try:
        yield
    finally:
        try:
            for name, value in before.items():
                raw.execute(f"PRAGMA {name} = {int(value)}")
        except Exception:
            # The connection is gone or unusable, and the error that made it so is already on
            # its way out: do not hide it behind this one. The pool must not hand the
            # connection to anyone with this run's settings, so it is discarded.
            connection.invalidate()


def _sleep() -> None:
    time.sleep(PAUSE_SECONDS)


# --- the run -----------------------------------------------------------------------------------


def purge_expired(
    engine: Engine,
    *,
    now: datetime,
    dry_run: bool = False,
    batch_size: int = BATCH_SIZE,
    pause: Callable[[], None] = _sleep,
) -> PurgeReport:
    """Delete everything that has expired at `now` (a timezone-aware instant), or, for a
    dry run, only count it, in one read transaction that writes nothing.

    TargetError if the schema is not the repository's head (nothing is deleted).
    PurgeAborted if the run stopped half way (for any reason, an interrupt included); the
    rows of the batches that committed are gone and a second run finishes the job.
    Idempotent: nothing eligible, nothing changed.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    now = _as_utc(now)
    conditions = _Conditions(now, now - RETENTION, planned=dry_run)
    with engine.connect() as connection, _connection_settings(connection, dry_run=dry_run):
        if dry_run:
            return _preview(connection, conditions)
        with connection.begin():
            check_schema_at_head(connection)
        return _purge(connection, conditions, batch_size, pause)


def _preview(connection: Connection, conditions: _Conditions) -> PurgeReport:
    with connection.begin():
        check_schema_at_head(connection)
        deleted = {
            category.value: _count_alias(connection, table, condition)
            for category, table, condition in _stages(conditions)
        }
        cancelled = _count_alias(
            connection,
            _requests,
            lambda row: sa.and_(conditions.request_expired(row), conditions.request_abandoned(row)),
        )
        held_back = _count_alias(connection, _messages, conditions.held_back)
    return PurgeReport(True, conditions.now, conditions.cutoff, deleted, cancelled, held_back)


def _count_alias(connection: Connection, table, condition: Callable) -> int:
    row = table.alias()
    return _count(connection, row, condition(row))


def _purge(
    connection: Connection, conditions: _Conditions, batch_size: int, pause: Callable[[], None]
) -> PurgeReport:
    done = {category.value: 0 for category in Category}
    cancelled = [0]
    stage = "starting"

    def drain(make: Callable[[int], Any], tally: Callable[[int], None]) -> None:
        while True:
            with connection.begin():
                changed = connection.execute(make(batch_size)).rowcount
            tally(changed)  # after the commit: only what is certainly gone is counted
            if changed < batch_size:
                return
            pause()

    def add(category: Category) -> Callable[[int], None]:
        def tally(changed: int) -> None:
            done[category.value] += changed

        return tally

    def add_cancelled(changed: int) -> None:
        cancelled[0] += changed

    def deleting(table: sa.Table, condition: Callable) -> Callable[[int], Any]:
        return lambda size: _delete_batch(table, condition, size)

    try:
        for category, table, condition in _stages(conditions):
            stage = category.value
            if category is Category.TURN_REQUESTS:
                drain(lambda size: _cancel_batch(conditions, size), add_cancelled)
            drain(deleting(table, condition), add(category))
        stage = "report"
        with connection.begin():
            held_back = _count_alias(connection, _messages, conditions.message_expired)
    except (Exception, KeyboardInterrupt) as exc:
        raise PurgeAborted(stage, dict(done), cancelled[0]) from exc
    return PurgeReport(False, conditions.now, conditions.cutoff, done, cancelled[0], held_back)
