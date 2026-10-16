"""The conversation transaction, independent of any transport.

A transport (Telegram today) answers one incoming message by opening a *turn*
on its conversation and doing everything for that message inside the scope:

    async with service.turn(key) as turn:
        outcome = await turn.send(text, while_working=...)
        ...deliver the outcome to the user...

The turn holds the conversation's lock from entering the scope to leaving it.
That is the point of a scoped turn rather than a single call: the transport's
delivery of the answer happens while the lock is still held, so the next
message from the same conversation cannot start before this one has been
delivered. Different conversations never wait for each other.

The service owns the transaction around the AI call: the input-length policy,
appending the user message, the backend request, rolling the pending message
back when the request does not complete, appending the answer and trimming
the history. It knows nothing about how messages arrive or are delivered, and
it never produces text for the user: it returns a ChatOutcome and the
transport renders it (see messages_ru).

History and locks live in context_manager and are shared by every service in
the process, so two services over the same conversation still take turns.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    asynccontextmanager,
    contextmanager,
    nullcontext,
)
from typing import Any

import context_manager
from house_helper.core.backend import (
    BackendConnectionError,
    BackendEmptyResponseError,
    BackendError,
    BackendRateLimitedError,
    ChatBackend,
)
from house_helper.core.identity import ConversationKey
from house_helper.core.outcomes import ChatOutcome, OutcomeStatus

logger = logging.getLogger(__name__)

# Application-level cap on a single incoming message, independent of any
# transport's own limit (Telegram's is ~4096 characters). Keeps a single
# request's cost and the history it adds predictable, regardless of how large
# a message a user sends.
DEFAULT_MAX_INPUT_LENGTH = 2000

# Something to keep open for exactly as long as the backend call is in flight
# (a transport's "typing..." indicator). A factory rather than an instance, so
# it is only created if the backend is actually called.
WhileWorking = Callable[[], AbstractAsyncContextManager[Any]]


class ChatService:
    """Answers conversations through a ChatBackend, one turn at a time."""

    def __init__(
        self,
        backend: ChatBackend,
        *,
        max_input_length: int = DEFAULT_MAX_INPUT_LENGTH,
    ) -> None:
        self._backend = backend
        self._max_input_length = max_input_length

    def turn(self, key: ConversationKey) -> AbstractAsyncContextManager["Turn"]:
        """Open a turn on a conversation.

        Entering waits for the conversation's lock and holds it until the
        scope exits, however it exits (normally, by an exception, or by
        cancellation). The Turn is only valid inside the scope.
        """
        return self._open_turn(key)

    @asynccontextmanager
    async def _open_turn(self, key: ConversationKey) -> AsyncIterator["Turn"]:
        async with context_manager.get_lock(key):
            turn = Turn(key, self._backend, self._max_input_length)
            try:
                yield turn
            finally:
                turn._close()

    async def reset(self, key: ConversationKey, *, source: str) -> ChatOutcome:
        """Clear a conversation's history under its lock and release the lock.

        For a transport that confirms the reset after the lock is released.
        One that confirms it while still holding the lock resets through its
        open turn instead (Turn.reset).
        """
        async with self.turn(key) as turn:
            return await turn.reset(source=source)


class Turn:
    """One conversation's locked stretch of work; obtained from
    ChatService.turn() and valid only inside that scope.

    Operations are awaited one at a time. Using the turn after its scope has
    exited, or starting an operation while another is still running, raises
    RuntimeError: either would touch the history outside the lock or
    interleave two transactions.
    """

    def __init__(
        self,
        key: ConversationKey,
        backend: ChatBackend,
        max_input_length: int,
    ) -> None:
        self._key = key
        self._backend = backend
        self._max_input_length = max_input_length
        self._open = True
        self._busy = False

    def _close(self) -> None:
        self._open = False

    @contextmanager
    def _operation(self) -> Iterator[None]:
        if not self._open:
            raise RuntimeError("this turn has ended: its scope has been exited")
        if self._busy:
            raise RuntimeError("this turn already has an operation in progress")
        self._busy = True
        try:
            yield
        finally:
            self._busy = False

    async def send(
        self,
        text: str,
        *,
        while_working: WhileWorking | None = None,
    ) -> ChatOutcome:
        """Answer one user message.

        `text` is used as given (the transport normalizes it). Returns REPLIED
        with the answer once the exchange is committed to the history -- before
        the caller delivers it, so a failed delivery never undoes an exchange
        that happened. Returns INPUT_TOO_LONG, RATE_LIMITED, CONNECTION_FAILED,
        EMPTY_RESPONSE or API_ERROR when the exchange did not happen; the
        history is then exactly as it was. Anything else, cancellation
        included, propagates after the same rollback.

        `while_working`, if given, is entered right before the backend call
        and left right after it, so it covers neither the input check nor
        anything the caller does with the outcome.
        """
        with self._operation():
            key = self._key

            if len(text) > self._max_input_length:
                return ChatOutcome(
                    OutcomeStatus.INPUT_TOO_LONG,
                    max_input_length=self._max_input_length,
                )

            context_manager.add_message(key, "user", text)
            ctx_len = context_manager.context_length(key)

            try:
                # Single rollback point for the whole transaction: any
                # exception at all -- a recognized backend failure,
                # cancellation, or a completely unexpected one -- means the
                # turn never completed, so the pending user message always
                # comes back out here before the exception is re-raised for
                # the except clauses below (or the caller) to interpret.
                try:
                    working = while_working() if while_working is not None else nullcontext()
                    async with working:
                        backend_reply = await self._backend.complete(
                            context_manager.get_context(key),
                            conversation=key,
                            context_len=ctx_len,
                        )
                    reply = backend_reply.text
                except BaseException:
                    context_manager.remove_last_message(key)
                    raise
            except asyncio.CancelledError:
                # Never convert cancellation into an application outcome --
                # just let it propagate untouched now that the rollback above
                # has run.
                raise
            except BackendRateLimitedError:
                return ChatOutcome(OutcomeStatus.RATE_LIMITED)
            except BackendConnectionError:
                return ChatOutcome(OutcomeStatus.CONNECTION_FAILED)
            except BackendEmptyResponseError:
                return ChatOutcome(OutcomeStatus.EMPTY_RESPONSE)
            except BackendError:
                return ChatOutcome(OutcomeStatus.API_ERROR)
            # Any other exception (not one of the recognized failure types
            # above) is unexpected: it propagates unchanged, never swallowed or
            # turned into an outcome. The rollback already happened above.

            context_manager.add_message(key, "assistant", reply)
            context_manager.trim_history(key)
            return ChatOutcome(OutcomeStatus.REPLIED, reply=reply)

    async def reset(self, *, source: str) -> ChatOutcome:
        """Clear the conversation's history; `source` names what asked for it."""
        with self._operation():
            context_manager.clear_context(self._key)
            logger.debug(
                "Context cleared via %s for conversation %s:%s",
                source,
                self._key.channel,
                self._key.subject,
            )
            return ChatOutcome(OutcomeStatus.CONTEXT_CLEARED)
