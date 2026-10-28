"""The web application's access to the database.

The engine is synchronous (see house_helper.persistence.database), and a
synchronous call on the event loop would stall every request. So all database
work goes through WebDatabase.run(), which hands a function to a worker thread.

Session ownership: a Session is created, used, committed or rolled back, and
closed inside one call to run(), on one worker thread. A Session is never handed
to another thread, never outlives the call, and never reaches a request handler:
the handler passes in the work and gets back its result. (A FastAPI dependency
that yields a Session was rejected: its setup and teardown run as separate calls
that may land on different threads, and when the teardown runs relative to the
response depends on the FastAPI version.)

Transaction boundary: run() is one transaction. It commits when the function
returns and rolls back when it raises, so a function must not commit itself. The
persistence helpers (house_helper.persistence.turns, ...) work inside the
caller's transaction, which is exactly what this provides.

Cancellation: a worker thread cannot be interrupted, so a function that has
started always runs to its end, and its transaction commits or rolls back as
usual, whether or not anyone is still waiting for the result (a client that
disconnects, a timeout). Because the whole Session lifecycle is inside that
function, a cancelled caller never leaves a connection checked out. Where the
cancellation comes from anyio (a task group, a cancel scope: what Starlette
uses) it is held back until the function has finished; a bare Task.cancel() is
delivered at once and leaves the function running on its own. A request that
must not repeat its effect when retried needs an idempotency key (see
house_helper.persistence.turns). A caller that must know how a unit ended, whoever
is still waiting, therefore does not call run() from the task that may be cancelled:
the web turn runner runs every unit as a task of its own that nothing cancels, and
settles a cancelled turn from the unit's outcome (web.turn_runner). A caller that holds a
lock around the unit, and must not release it while the unit can still change what the
lock protects (the Telegram bot's binding lock), calls run_to_completion() instead: the
same unit, but the caller does not leave until the unit has ended, and a cancellation is
raised then. From outside,
"waiting for a slot", "queued" and "running" are not told apart; a cancelled call is
withdrawn only in the first two, so "the awaiting task was cancelled" never means
"the function did not run".

The bound: a slot belongs to the unit of work, not to the task awaiting it. It is
taken before the function is handed to the pool and given back when the function
has finished on its thread (or was withdrawn before it started), however the
caller ended. Holding the slot in the awaiting task, as anyio's thread limiter
does, is not enough: a bare Task.cancel() frees that slot while the thread is
still running, and the replacement requests then start more workers than the
limit. The functions also run on a pool of exactly as many threads as there are
slots, so no more than that many can run at once even apart from the slots. A call
cancelled while it waits for a slot never runs.

Submission: ThreadPoolExecutor.submit() queues the work item first and starts a
thread second. When the thread cannot be started it raises ("can't start new
thread") with the item still queued, and the pool runs it later, when a thread is
free. A raised submit() therefore does not mean that nothing will run. So a unit
may not start until run() has settled whether it was accepted (submit() returned
and the completion callback is registered): the worker waits for that verdict, and
a unit that was not accepted does nothing when a worker finds it. The slot follows
the verdict. Before it, the slot is run()'s. A refused unit's slot is given back by
run(), and by nobody else, since the unit never runs. An accepted unit's slot is
given back by the unit when its function has finished on its thread, or by the
completion callback if it was withdrawn before it started; one of the two, never
both (see the comments in run()). (The pool's own bookkeeping goes slightly wrong
after such a failure, and on some Python versions it then keeps fewer threads than
slots, so units queue behind fewer threads: slower, never wrong.)

Shutdown: aclose() refuses new work, waits (a bounded time) for the functions
still running, then disposes the engine. Disposing first would leave the
connection of a running function open, because Engine.dispose() closes only
idle connections. The running functions are counted on their own threads, so the
count is right however their callers ended. A call still waiting for a slot is
refused when its turn comes: it never reaches the engine or the pool.
"""

import asyncio
import contextvars
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

import anyio
import anyio.to_thread
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession

from house_helper.persistence.database import create_session_factory

logger = logging.getLogger(__name__)

T = TypeVar("T")

# How many units of work may run at once (the slots, and the threads of the pool
# they run on). Kept below the engine's default pool (5 connections + 10
# overflow), so a call waits for a slot here, in the event loop, and never inside
# the engine's pool for a connection. It also bounds SQLite's writers queueing on
# its file lock. tests/test_web_database.py pins the relation.
MAX_CONCURRENT_UNITS_OF_WORK = 10

_CLOSED_MESSAGE = "the database is not available: the application is not running"

# How long aclose() waits for running functions before disposing the engine anyway.
DRAIN_TIMEOUT_SECONDS = 10.0


class DatabaseClosed(RuntimeError):
    """Work was submitted after the application stopped (or before it started)."""


class _Unit:
    """A call to run(), as the worker thread that will execute it sees it: the
    function, the loop its slot goes back to, and whether the pool accepted it.

    run() settles that exactly once, right after it has handed the unit to the pool,
    in the same step on the loop (no await in between, so the wait below is short).
    """

    def __init__(self, work: Callable[[OrmSession], Any], loop: asyncio.AbstractEventLoop) -> None:
        self.work = work
        self.loop = loop
        self._settled = threading.Event()
        self._accepted = False

    def settle(self, accepted: bool) -> None:
        self._accepted = accepted
        self._settled.set()

    def wait_until_accepted(self) -> bool:
        """Called on the worker thread: waits for the verdict, then returns it."""
        self._settled.wait()
        return self._accepted


class WebDatabase:
    """Runs functions against the database on worker threads.

    Owns `engine`: aclose() disposes it. Create it when the application starts
    and close it when the application stops. run() and aclose() are called on the
    event loop; the functions given to run() execute on worker threads.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        max_concurrent: int = MAX_CONCURRENT_UNITS_OF_WORK,
        drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self._engine = engine
        self._session_factory = create_session_factory(engine)
        # The slots (admission, on the event loop) and the threads they run on: the
        # same number, so an admitted unit never waits for a thread. A Semaphore, not
        # a CapacityLimiter: a slot is released by the unit, not by the task that took
        # it, and CapacityLimiter's acquire_on_behalf_of() loses the token (and turns
        # the CancelledError into a RuntimeError) when a bare Task.cancel() lands in it.
        self._slots = anyio.Semaphore(max_concurrent, max_value=max_concurrent)
        self._pool = ThreadPoolExecutor(
            max_workers=max_concurrent, thread_name_prefix="house-helper-db"
        )
        self._drain_timeout = drain_timeout
        # Guards _closed (when written) and _running. Held only for a counter
        # update, except inside wait_for, which releases it while it waits.
        self._state = threading.Condition()
        self._closed = False
        self._running = 0  # functions between starting and finishing, on their threads

    @property
    def closed(self) -> bool:
        return self._closed

    async def run(
        self,
        work: Callable[[OrmSession], T],
        *,
        on_accepted: Optional[Callable[[], None]] = None,
    ) -> T:
        """Call `work(session)` on a worker thread, in one transaction, and
        return what it returns. Whatever it raises is raised here, after the
        transaction has been rolled back. Raises DatabaseClosed once closed.

        `on_accepted`, if given, is called once on the event loop, in the very step in
        which the pool takes the unit (no await between). From then on the function
        may run whatever happens to this call: cancelling it no longer promises that
        the function does not run (it is withdrawn only while it has not started).
        Before it, the call is only waiting for a slot, and cancelling it means the
        function never runs. So a caller that must know which of the two it is in has
        its answer here. It is not called for a call that is refused (DatabaseClosed,
        a pool that would not take the unit) or cancelled while it waits for a slot.
        It must be quick and must not raise: an exception is logged by class name and
        ignored, because by then the unit is already on its way."""
        loop = asyncio.get_running_loop()
        self._refuse_if_closed()  # at once, even if every slot is taken
        await self._slots.acquire()  # cancelled while waiting: holds nothing
        unit = _Unit(work, loop)
        accepted = False
        try:
            # Again after the wait, which may have outlasted the close: the pool
            # refuses work once aclose() has shut it down. Everything from here to
            # the end of the try is one step on the event loop (no await), so
            # nothing can cancel or close in between.
            self._refuse_if_closed()
            # submit() can raise with the unit already queued (see the module
            # docstring), so it is only accepted once nothing after the submit has
            # failed either. `accepted` is set by the last statement, and the finally
            # acts on that one value for both the worker's verdict and the slot,
            # whatever interrupts this step.
            future = self._pool.submit(contextvars.copy_context().run, self._run_unit, unit)
            # An accepted unit's slot is given back by whichever of these two
            # happens, never both: a unit that runs gives it back itself when it
            # ends (_run_unit, on its own thread, however its caller ended); a unit
            # cancelled before it started never runs, and this callback gives it
            # back. cancel() and the start are mutually exclusive under the
            # future's own lock.
            future.add_done_callback(lambda done: done.cancelled() and self._give_back(loop))
            accepted = True
        finally:
            unit.settle(accepted)
            if not accepted:
                # Refused: whatever the pool still holds of it will do nothing and
                # give nothing back, and nothing ever cancels it, so the callback
                # above cannot give it back either. The slot is given back here, once.
                self._slots.release()
        if on_accepted is not None:
            try:
                on_accepted()
            except Exception as exc:  # the unit is accepted: nothing may now fail the call
                logger.error("An on_accepted callback failed: category=%s", type(exc).__name__)
        # What anyio's thread limiter did: a cancel scope waits for the function to
        # finish. (A bare Task.cancel() still gets through, and the slot stays taken.)
        with anyio.CancelScope(shield=True):
            return await asyncio.wrap_future(future, loop=loop)

    async def run_to_completion(self, work: Callable[[OrmSession], T]) -> T:
        """run(), for a caller that holds something the function must not outlive: a lock whose
        meaning is "nothing of this change is still going on".

        run() lets a bare Task.cancel() through at once and leaves an accepted function running
        on its thread, so a caller that releases its lock on the way out releases it with the
        function still able to change what the lock protects. This one does not leave until the
        function has ended, committed or rolled back: the cancellation (any number of them, of
        either kind) is noted, the wait goes on, and the cancellation is raised only then. A
        call that is cancelled before the pool has taken the function (it waits for a slot) is
        withdrawn at once and never runs, as with run(), so there is nothing to wait for.

        The wait is shielded from anyio's cancellation the way run()'s is, so a cancel scope
        that stays cancelled cannot make this loop spin without ever yielding to the thread's
        completion callback. It is bounded by the function itself: nothing here waits for
        anything else, and the function's own end (or the database's failure to run it) is
        what returns. A function that never ended would hold the caller's lock for good, which
        is the safe way for that to fail."""
        loop = asyncio.get_running_loop()
        accepted: asyncio.Future = loop.create_future()

        def mark_accepted() -> None:
            if not accepted.done():
                accepted.set_result(None)

        unit = asyncio.ensure_future(self.run(work, on_accepted=mark_accepted))
        cancelled: Optional[asyncio.CancelledError] = None
        try:
            # Until the pool has taken the function, the call can still be withdrawn.
            await asyncio.wait({unit, accepted}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError as exc:
            cancelled = exc
            if not accepted.done():
                unit.cancel()  # still waiting for a slot: it never runs
        with anyio.CancelScope(shield=True):
            while not unit.done():
                try:
                    await asyncio.wait({unit})
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
        if not unit.cancelled():
            unit.exception()  # retrieved, whoever the outcome is for
        if cancelled is not None:
            raise cancelled
        return unit.result()

    def _refuse_if_closed(self) -> None:
        if self._closed:
            raise DatabaseClosed(_CLOSED_MESSAGE)

    def _give_back(self, loop: asyncio.AbstractEventLoop) -> None:
        """Release a slot. Called on the thread that ran the unit, or on the one that
        cancelled it before it started; the semaphore belongs to the event loop."""
        try:
            loop.call_soon_threadsafe(self._slots.release)
        except RuntimeError:
            pass  # the loop is closed: nobody is left to wait for the slot

    def _run_unit(self, unit: _Unit) -> Any:
        """What the pool runs: an accepted unit, then its slot back, whatever the
        unit did (including being refused because the database closed). A unit that
        was not accepted is left alone: it neither runs nor gives anything back."""
        if not unit.wait_until_accepted():
            return None
        try:
            return self._run_in_session(unit.work)
        finally:
            self._give_back(unit.loop)

    def _run_in_session(self, work: Callable[[OrmSession], T]) -> T:
        with self._state:
            # The check that makes the wait in aclose() sufficient: it is made under
            # the same lock aclose() takes to close and to wait, so work is either
            # counted before the wait or refused (a unit admitted just before the
            # close may start after it).
            self._refuse_if_closed()
            self._running += 1
        try:
            # Everything about the Session happens here, in this one call.
            with self._session_factory() as session:
                with session.begin():
                    return work(session)
        finally:
            with self._state:
                self._running -= 1
                if self._running == 0:
                    self._state.notify_all()

    async def aclose(self) -> None:
        """Refuse further work, let the running functions finish (for at most the
        drain timeout), dispose the engine. Safe to call twice. The waiting and the
        disposal run on a worker thread, like all other database work, and go on to
        the end if this coroutine is cancelled."""
        with self._state:
            if self._closed:
                return
            self._closed = True
        try:
            await anyio.to_thread.run_sync(self._drain_and_dispose)
        finally:
            # run() submits nothing once _closed is set, so this is the last word
            # to the pool. Units still in it run (and are refused), and a thread
            # still on a function ends with it.
            self._pool.shutdown(wait=False)
        logger.info("Database engine disposed")

    def _drain_and_dispose(self) -> None:
        with self._state:
            self._state.wait_for(lambda: self._running == 0, self._drain_timeout)
            still_running = self._running
        if still_running:
            logger.warning(
                "Closing the database with %d unit(s) of work still running", still_running
            )
        self._engine.dispose()
