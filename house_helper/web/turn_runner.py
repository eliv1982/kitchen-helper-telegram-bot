"""Answers one message of a web conversation, durably.

The Telegram bot keeps a conversation in memory and answers inside a scoped turn
(core.chat_service); the web chat keeps it in the database, and this module is its
turn runner: the same contract (a ChatBackend, the shared limits, the outcome
vocabulary of core.outcomes), a different store. The Telegram path is untouched.

One submit() is one pass through the lifecycle of persistence.turns, with the backend
call in the middle and NO transaction open while it runs:

  1. begin_turn (one transaction): ownership, the idempotency key, "one request per
     conversation at a time", the user's message and its request stored, the request
     claimed with a lease, and the context built. Or the answer to a repeated key.
  2. the backend call, bounded by `deadline_seconds`.
  3. complete_turn (one transaction): the assistant message and the completion
     together; or fail_turn: the request marked FAILED, which the same key retries.

The lease. A request is claimed for the deadline (60 s) and a margin (30 s): 90 s. The
lease runs from the instant of the claim, which begin_turn takes after it holds the
conversation lock, so a wait for a connection, a worker thread or the lock before the
claim does not eat into it. What the 90 s must cover is: committing step 1, the call
(60 s at most), and getting step 3 to its lock. Step 3 refuses to complete when the
lease has run out at the moment it holds the lock (LeaseLost, answered
turn_in_progress): the reply is discarded, the request stays PROCESSING for the
attempt that may have replaced it, or is retired by the next message of the
conversation, and the same key generates again. So the margin is a budget for the two
transactions around the call; when the database makes them slower than that, the
reply is lost rather than stored late. It is never stored by an attempt that no longer
holds the request.

What a failure leaves behind, by where it happens:

  * before 1 commits: nothing. The client retries.
  * between 1 and 3 (the backend failed, timed out, or the runner was stopped): the
    request is FAILED with a stable error code, the user's message stays in the
    history as "failed" and is left out of the context of every later request.
  * 3 itself fails (the database): the reply is lost, the request is marked FAILED if
    the database lets it be, and otherwise stays PROCESSING until its lease runs out,
    after which the same key claims it again. Either way a retry generates again.
  * the process dies: the request stays PROCESSING; same as the line above.

Who may retry. "A retry generates again" holds for the website, where a person chose to
send the message again. It does not hold for a caller that passes reclaim=False (the Telegram
bot, for an update that Telegram delivers again): the claim of an attempt is committed before
the backend is called, so a key with a committed attempt that did not complete (failed,
retired, or PROCESSING past its lease) is not run again; submit() raises turns.TurnAttempted
and the provider is not asked a second time. That state is in the database, so it survives
a restart. What it cannot know is whether the first request reached the provider.

Disconnects. The work of a request does not belong to the connection that asked for
it: submit() runs it as a task of its own and waits for it behind asyncio.shield, so a
client that goes away (or a server that cancels the handler) does not interrupt it. The
reply is generated, stored and completed anyway, and the client's retry with the same
key is answered from it without a second generation. Only stopping the runner
(aclose) cancels a turn that is still running.

Database units, cancellation and shutdown. A transaction runs on a worker thread and
cannot be interrupted: cancelling the task that awaits it does not stop it, and it
commits or rolls back on its own whether or not anyone is still waiting. So the runner
never ties a transaction to the task that asked for it. Every database call of the
runner is a UNIT, a task of its own that drain() counts until the call has ended, and
for each the runner records how far it got: asked for (waiting for a slot), accepted
(WebDatabase.run told the runner, in the step in which the worker pool took it: from
then on the function may run and nothing stops it) and ended (the task is done: its
result means the transaction committed, its exception that it did not). aclose() never
cancels a unit. A turn that is cancelled while it waits for a unit that is not yet
accepted withdraws it (cancelling a call that only waits for a slot means the function
never runs). One that is cancelled while its unit IS accepted hands what would have
followed to a task of its own, which waits for the unit's outcome and then does it:

  * cancelled during 1: if the unit left a claim, the request is marked FAILED
    "interrupted" (a unit that was withdrawn, refused or rolled back left nothing to
    mark);
  * cancelled during 2: the backend call is cancelled and the request marked FAILED
    "interrupted";
  * cancelled during 3: if the unit committed the completion, the reply stands and a
    retry replays it; if it did not (it was withdrawn, or the database failed), the
    request is marked FAILED "interrupted".

The guarantee, exactly. aclose() lets the turns finish for `shutdown_grace_seconds`,
cancels what is left, and waits the same time again for the cancelled turns' tasks
(and the units they wait for) to settle; then it returns, whether or not they have. It
never waits for ever. A request that was claimed, and whose interruption could be
written while the database was still open and answering within that time, is FAILED
"interrupted". One that could not (a transaction stuck behind a lock for longer than
the grace, a database already closed, a process that was killed) stays PROCESSING and
is released by its lease: the same key claims it again once the lease has run out, 90 s
after the claim, and any other message of the conversation retires it at that point
(persistence.turns). What is lost is a reply that was not stored; nothing is ever stored
twice. aclose() logs a warning when it returns with work unfinished.

Python 3.10: asyncio.timeout does not exist there, so the deadline is asyncio.wait_for.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from house_helper.core.backend import (
    BackendConnectionError,
    BackendEmptyResponseError,
    BackendError,
    BackendRateLimitedError,
    ChatBackend,
)
from house_helper.core.identity import ConversationKey
from house_helper.core.limits import MAX_HISTORY_MESSAGES
from house_helper.core.orthography import replace_yo
from house_helper.core.outcomes import OutcomeStatus
from house_helper.persistence import attachments, turns
from house_helper.persistence.conversations import ConversationNotFound
from house_helper.persistence.turns import CompletedTurn
from house_helper.web.database import DatabaseClosed, WebDatabase
from house_helper.web.diagnostics import ApiError, log_error

logger = logging.getLogger(__name__)

WEB_CHANNEL = "web"
TELEGRAM_CHANNEL = "telegram"

# How long the backend gets for one answer. The provider's own timeout (15 s, one
# retry) is shorter; this is what bounds a hung call the provider's does not.
DEFAULT_DEADLINE_SECONDS = 60.0
# A request is claimed for the deadline and this much more (the transactions before and
# after the call). After that its worker is presumed gone and the request may be claimed
# again by its key, or retired by the next message of the conversation.
LEASE_MARGIN_SECONDS = 30.0
# How long aclose() lets running turns finish, and then how long it lets the cancelled
# ones (and the database units they wait for) settle.
DEFAULT_SHUTDOWN_GRACE_SECONDS = 10.0

# What an answer cut off at the length cap ends with. It is part of the stored text, so
# the history says it too and the assistant, shown it in the next context, knows what
# "go on" refers to.
TRUNCATED_NOTE = (
    "\n\n…\n(Ответ получился слишком длинным и оборвался. "
    "Напишите «продолжи», чтобы получить остальное.)"
)

# The code of a request that was cut off by the runner stopping (the same word as
# persistence.turns uses for a request retired because its lease ran out).
INTERRUPTED = "interrupted"

# The outcome vocabulary of the chat core, spelled as the codes of the API.
_OUTCOME_CODES = {
    OutcomeStatus.RATE_LIMITED: "provider_rate_limited",
    OutcomeStatus.CONNECTION_FAILED: "provider_unreachable",
    OutcomeStatus.EMPTY_RESPONSE: "provider_empty_response",
    OutcomeStatus.API_ERROR: "provider_error",
}

_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def outcome_of(error: BaseException) -> OutcomeStatus:
    """The ChatOutcome status the core assigns to a backend failure (see
    ChatService.Turn.send, whose order of checks this repeats)."""
    if isinstance(error, BackendRateLimitedError):
        return OutcomeStatus.RATE_LIMITED
    if isinstance(error, BackendConnectionError):
        return OutcomeStatus.CONNECTION_FAILED
    if isinstance(error, BackendEmptyResponseError):
        return OutcomeStatus.EMPTY_RESPONSE
    return OutcomeStatus.API_ERROR


def clean_reply(text: str) -> str:
    """The reply as it can be stored: NUL (which PostgreSQL text cannot hold) removed,
    a lone surrogate (which UTF-8 cannot encode) replaced."""
    return _UNSTORABLE.sub(lambda found: "" if found.group() == "\x00" else "�", text)


class _Call:
    """One database call of the runner, as a task of its own. `accepted` is set (on the
    event loop) when the worker pool has taken the call: the function may run from then
    on, and cancelling `task` no longer stops it. Before that the call only waits for a
    slot, and cancelling `task` means the function never runs. `task` is done when the
    call is: its result is what the function returned, committed; its exception, that the
    transaction did not commit (or never started)."""

    __slots__ = ("task", "accepted")

    def __init__(self) -> None:
        self.task: Optional[asyncio.Task] = None
        self.accepted = False

    def accept(self) -> None:
        self.accepted = True


class TurnNotAuthorized(Exception):
    """The caller's `authorize` check, made inside the claim's own transaction, said no: the
    claim was rolled back with the rest of that transaction (nothing stored, no message, no
    request) and the backend was not called. Not an ApiError: the caller that asked for the
    check knows what its refusal means."""


@dataclass(frozen=True)
class TurnResult:
    completed: CompletedTurn
    replayed: bool  # answered from an earlier completion of the same key


@dataclass(frozen=True)
class TurnCompletion:
    """What a completion hook is told: a turn that has just been completed by this very
    call (never one replayed from an earlier completion), and the channel it came from."""

    user_id: UUID
    conversation_id: UUID
    origin: str  # WEB_CHANNEL or TELEGRAM_CHANNEL
    user_text: str
    reply_text: str


# Called, on the event loop, once for every turn that completed. It must return at once
# (start a task for anything that takes time): the turn's caller is waiting for it.
CompletionHook = Callable[[TurnCompletion], None]


class WebTurnRunner:
    """Runs the turns of the web chat for one application. Created when the
    application starts, stopped (aclose) before the database is closed."""

    def __init__(
        self,
        database: WebDatabase,
        backend: ChatBackend,
        clock: Callable[[], datetime],
        *,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        lease_margin_seconds: float = LEASE_MARGIN_SECONDS,
        shutdown_grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self._database = database
        self._backend = backend
        self._clock = clock
        self._deadline = deadline_seconds
        self._lease = timedelta(seconds=deadline_seconds + lease_margin_seconds)
        self._grace = shutdown_grace_seconds
        # The tasks of the turns: the only ones aclose() cancels.
        self._turns: set[asyncio.Task] = set()
        self._completion_hooks: list[CompletionHook] = []
        # Database units, and the tasks that settle a turn that was cancelled: waited
        # for by drain(), never cancelled by aclose() (cancelling a unit would only
        # stop waiting for a transaction that goes on).
        self._work: set[asyncio.Task] = set()
        self._closed = False

    # --- the public surface ------------------------------------------------------------

    def add_completion_hook(self, hook: CompletionHook) -> None:
        """Have `hook` told of every turn this runner completes (see TurnCompletion). A hook
        that raises is logged by the name of its exception and the turn is not affected."""
        self._completion_hooks.append(hook)

    async def submit(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: str,
        text: str,
        correlation_id: str,
        route: str,
        origin: str = WEB_CHANNEL,
        attachment_id: Optional[UUID] = None,
        reclaim: bool = True,
        authorize: Optional[Callable[[Any], bool]] = None,
    ) -> TurnResult:
        """Answer `text` in the user's conversation. Raises ApiError (always one of the
        catalogue's; its `logged` flag says whether the log has it already). `origin` is the
        channel the message came from; it is what the completion hooks are told.
        `attachment_id` names an unattached photo of the user (persistence.attachments) that the
        message takes with it; ApiError attachment_unusable if it cannot.

        `reclaim` says whether a key whose earlier attempt did not complete may be run again.
        The website's retry may (the person chose to send it again). A caller that has no such
        choice behind it, the Telegram bot handling an update that Telegram delivers a second
        time, passes False: then a key whose attempt had begun and did not complete raises
        turns.TurnAttempted, which is NOT an ApiError and is not logged here, and the provider
        is not asked again. A completed key is replayed either way.

        `authorize`, if given, is called with the session of the claim's transaction, after the
        conversation has been locked and the claim made, and must return True for the claim to
        stand; otherwise the whole transaction is rolled back and TurnNotAuthorized is raised
        (not an ApiError; the backend is not called and nothing is stored). It is how a caller
        whose right to ask can be withdrawn while it waits (the Telegram bot: the account may
        be unlinked, the person's access revoked) makes "still allowed" and "claimed" one
        atomic step instead of two with a gap."""
        if self._closed:
            error = ApiError("service_unavailable", category="RunnerClosed")
            log_error(error, correlation_id, route)
            raise error
        task = self._track(
            asyncio.ensure_future(
                self._execute(
                    user_id,
                    conversation_id,
                    idempotency_key,
                    text,
                    correlation_id,
                    route,
                    origin,
                    attachment_id,
                    reclaim,
                    authorize,
                )
            ),
            self._turns,
        )
        # Shielded: if this caller is cancelled, the turn goes on without it.
        return await asyncio.shield(task)

    @property
    def unfinished(self) -> int:
        """How many turns and database units (and tasks settling a cancelled turn) have
        not ended yet. Zero means nothing of this runner can still touch the database."""
        return len(self._turns) + len(self._work)

    async def drain(self, timeout: Optional[float] = None) -> None:
        """Wait until nothing is unfinished, what was started in the meantime included
        (for at most `timeout` seconds, if given)."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while self.unfinished:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return
            await asyncio.wait(self._turns | self._work, timeout=remaining)

    async def aclose(self) -> None:
        """Refuse new turns, let the running ones finish, cancel what is left, and
        wait for the cancelled ones to settle (each wait is bounded by the grace
        period; see the module docstring for what is guaranteed and what is not)."""
        self._closed = True
        await self.drain(self._grace)
        for task in set(self._turns):
            task.cancel()
        await self.drain(self._grace)
        if self.unfinished:
            logger.warning(
                "The chat turn runner stopped with work unfinished: %d turn task(s), "
                "%d database unit(s) or settling task(s); the requests they hold are "
                "released by their leases",
                len(self._turns),
                len(self._work),
            )

    # --- tasks -------------------------------------------------------------------------

    def _track(self, task: asyncio.Task, group: set[asyncio.Task]) -> asyncio.Task:
        group.add(task)
        task.add_done_callback(lambda done: self._forget(done, group))
        return task

    @staticmethod
    def _forget_exception(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()  # retrieved, whoever (if anyone) was waiting for it

    def _forget(self, task: asyncio.Task, group: set[asyncio.Task]) -> None:
        group.discard(task)
        self._forget_exception(task)

    def _spawn_work(self, coroutine: Any) -> asyncio.Task:
        return self._track(asyncio.ensure_future(coroutine), self._work)

    def _start_unit(self, work: Callable[[Any], Any]) -> _Call:
        """Ask for one database call (one transaction) as a task of its own. The task
        ends with the call, and an accepted call does not end with whoever waits for it."""
        call = _Call()
        call.task = self._track(
            asyncio.ensure_future(self._database_work(work, call.accept)), self._work
        )
        return call

    @staticmethod
    def _withdraw_unless_accepted(call: _Call) -> None:
        """A call that is still waiting for a slot is cancelled: it will never run. One
        that has been accepted is left alone, whatever its task is doing."""
        if not call.accepted:
            call.task.cancel()

    # --- one turn ----------------------------------------------------------------------

    async def _execute(
        self,
        user_id: UUID,
        conversation_id: UUID,
        key: str,
        text: str,
        correlation_id: str,
        route: str,
        origin: str,
        attachment_id: Optional[UUID] = None,
        reclaim: bool = True,
        authorize: Optional[Callable[[Any], bool]] = None,
    ) -> TurnResult:
        try:
            result = await self._run(
                user_id, conversation_id, key, text, correlation_id, attachment_id, reclaim, authorize
            )
        except ApiError as error:
            if not error.logged:
                log_error(error, correlation_id, route)
            raise
        except (turns.TurnAttempted, TurnNotAuthorized):
            raise  # states of their own, for the callers that asked for them (see submit)
        except Exception as exc:  # a bug: logged by class, answered like any other failure
            error = ApiError("internal_error", category=type(exc).__name__)
            log_error(error, correlation_id, route)
            raise error from None
        if not result.replayed:
            self._tell_hooks(user_id, conversation_id, origin, result)
        return result

    def _tell_hooks(
        self, user_id: UUID, conversation_id: UUID, origin: str, result: TurnResult
    ) -> None:
        done = result.completed
        completion = TurnCompletion(
            user_id=user_id,
            conversation_id=conversation_id,
            origin=origin,
            user_text=done.user_message.text,
            reply_text=done.assistant_message.text,
        )
        for hook in self._completion_hooks:
            try:
                hook(completion)
            except Exception as exc:  # a hook is not part of the turn
                logger.error("A chat turn completion hook failed: category=%s", type(exc).__name__)

    async def _database_work(
        self, work: Callable[[Any], Any], on_accepted: Optional[Callable[[], None]] = None
    ) -> Any:
        """database.run, with the database's failures turned into ApiError (by class
        name only: SQLAlchemy's messages carry statement parameters)."""
        try:
            return await self._database.run(work, on_accepted=on_accepted)
        except DatabaseClosed:
            raise ApiError("service_unavailable", category="DatabaseClosed") from None
        except SQLAlchemyError as exc:
            raise ApiError("service_unavailable", category=type(exc).__name__) from None

    async def _run(
        self,
        user_id: UUID,
        conversation_id: UUID,
        key: str,
        text: str,
        correlation_id: str,
        attachment_id: Optional[UUID] = None,
        reclaim: bool = True,
        authorize: Optional[Callable[[Any], bool]] = None,
    ) -> TurnResult:
        # 1. The claim. The clock is read by begin_turn itself, under the conversation
        # lock, so the lease is measured from the claim and not from here.
        def admit(session: Any) -> Any:
            started = turns.begin_turn(
                session,
                user_id,
                conversation_id,
                key,
                text,
                clock=self._clock,
                lease=self._lease,
                max_context_messages=MAX_HISTORY_MESSAGES,
                attachment_id=attachment_id,
                reclaim=reclaim,
            )
            if authorize is not None and not authorize(session):
                raise TurnNotAuthorized  # (the exception rolls the claim back with its transaction)
            return started

        admission = self._start_unit(admit)
        try:
            started = await asyncio.shield(admission.task)
        except asyncio.CancelledError:
            # An accepted transaction goes on without us and may commit a claim nobody
            # holds; one that still waits for a slot is withdrawn and never runs.
            self._withdraw_unless_accepted(admission)
            self._spawn_work(self._after_cancelled_admission(admission.task, correlation_id))
            raise
        except ConversationNotFound:
            raise ApiError("conversation_not_found") from None
        except turns.IdempotencyKeyConflict:
            raise ApiError("idempotency_key_reused") from None
        except turns.ConversationBusy:
            raise ApiError("conversation_busy") from None
        except turns.TurnInFlight:
            raise ApiError("turn_in_progress") from None
        except turns.TurnSuperseded:
            raise ApiError("turn_superseded") from None
        except turns.TurnUnusable:
            raise ApiError("idempotency_key_unusable") from None
        except attachments.AttachmentUnusable:
            raise ApiError("attachment_unusable") from None

        if isinstance(started, CompletedTurn):
            return TurnResult(started, replayed=True)

        claim: turns.Claim = started
        safe_context = {"conversation": conversation_id, "attempt": claim.attempt}

        # 2. The call.
        try:
            text_out, truncated = await self._generate(claim, conversation_id, correlation_id)
        except ApiError as failure:
            await self._record_failure(claim, failure.code, correlation_id)
            failure.user_message_id = claim.user_message_id
            failure.context = safe_context
            raise

        # 3. The completion. complete_turn reads the clock under the lock as well, and
        # refuses when the lease has run out by then.
        completion = self._start_unit(
            lambda session: turns.complete_turn(
                session, claim, text_out, truncated=truncated, clock=self._clock
            )
        )
        try:
            done = await asyncio.shield(completion.task)
        except asyncio.CancelledError:
            self._withdraw_unless_accepted(completion)
            self._spawn_work(
                self._after_cancelled_completion(completion.task, claim, correlation_id)
            )
            raise
        except turns.LeaseLost:
            # Another attempt owns the request now, or this one outlived its lease
            # (or was retired by a newer message): the reply is discarded.
            raise ApiError(
                "turn_in_progress",
                category="LeaseLost",
                user_message_id=claim.user_message_id,
                context=safe_context,
            ) from None
        except ApiError as failure:
            # The reply could not be stored. Say so in the request, if the database
            # lets us, so that a retry need not wait for the lease.
            await self._record_failure(claim, "storage_unavailable", correlation_id)
            failure.user_message_id = claim.user_message_id
            failure.context = safe_context
            raise
        logger.info(
            "Chat turn completed: correlation_id=%s conversation=%s attempt=%d truncated=%s retry=%s",
            correlation_id,
            conversation_id,
            claim.attempt,
            truncated,
            claim.retry,
        )
        return TurnResult(done, replayed=False)

    async def _generate(
        self, claim: turns.Claim, conversation_id: UUID, correlation_id: str
    ) -> tuple[str, bool]:
        """The reply as (text, truncated). Raises the ApiError that says why there is
        none. Cancellation is not an error to report: the request is marked
        interrupted and the cancellation goes on."""
        try:
            reply = await asyncio.wait_for(
                self._backend.complete(
                    claim.context,
                    conversation=ConversationKey(WEB_CHANNEL, str(conversation_id)),
                    context_len=len(claim.context),
                ),
                timeout=self._deadline,
            )
        except asyncio.CancelledError:
            # In a task of its own: a second cancellation must not cut the record short,
            # and aclose() must see it as unfinished until it is done.
            self._spawn_work(self._record_failure(claim, INTERRUPTED, correlation_id))
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise ApiError("provider_timeout", category="TimeoutError") from None
        except BackendError as exc:
            raise ApiError(_OUTCOME_CODES[outcome_of(exc)], category=type(exc).__name__) from None
        except Exception as exc:  # the backend's own bug, not the provider's failure
            raise ApiError("internal_error", category=type(exc).__name__) from None

        # The assistant's own words, spelled by the product's rule (house_helper.core.orthography).
        # Applied here, as the answer is produced, and nowhere on the way out: what is stored
        # (the person's messages, and the answers stored before this) is shown as it was entered.
        text = replace_yo(clean_reply(reply.text))
        if not text.strip():
            raise ApiError(
                _OUTCOME_CODES[OutcomeStatus.EMPTY_RESPONSE], category="EmptyAfterCleaning"
            )
        truncated = bool(getattr(reply, "truncated", False))
        if truncated:
            text += TRUNCATED_NOTE
        return text, truncated

    # --- what follows a turn that was cancelled while it waited for the database ------------

    async def _after_cancelled_admission(
        self, admission: asyncio.Task, correlation_id: Optional[str]
    ) -> None:
        """The turn was cancelled during step 1. Wait for the transaction to end (it was
        not stopped); if it left a claim, nobody holds it: mark it interrupted."""
        await asyncio.wait({admission})
        if admission.cancelled() or admission.exception() is not None:
            return  # refused or rolled back: nothing was claimed (or the loop is going away)
        started = admission.result()
        if isinstance(started, turns.Claim):
            await self._record_failure(started, INTERRUPTED, correlation_id)

    async def _after_cancelled_completion(
        self, completion: asyncio.Task, claim: turns.Claim, correlation_id: Optional[str]
    ) -> None:
        """The turn was cancelled during step 3. Wait for the transaction to end. If it
        committed the completion the reply stands (a retry replays it); if the
        attempt had lost its request there is nothing to do; if the database failed or
        the transaction was withdrawn before it ran, mark the request interrupted so
        that a retry need not wait for the lease."""
        await asyncio.wait({completion})
        if not completion.cancelled():  # (cancelled: withdrawn before it ran, the request is still ours)
            failure = completion.exception()
            if failure is None or isinstance(failure, turns.LeaseLost):
                return
        await self._record_failure(claim, INTERRUPTED, correlation_id)

    async def _record_failure(
        self, claim: turns.Claim, code: str, correlation_id: Optional[str]
    ) -> None:
        """Mark the attempt's request FAILED. Never raises (but cancellation of the
        caller goes through): when it cannot be done the lease is what releases the
        request. The transaction is a unit, so it is not abandoned with the caller."""
        unit = self._start_unit(
            lambda session: turns.fail_turn(session, claim, code, clock=self._clock)
        ).task
        await asyncio.wait({unit})
        if unit.cancelled():
            category = "CancelledError"
        else:
            failure = unit.exception()
            if failure is None:
                return
            category = getattr(failure, "category", None) or type(failure).__name__
        logger.error(
            "A failed chat turn could not be recorded: correlation_id=%s code=%s category=%s",
            correlation_id or "-",
            code,
            category,
        )
