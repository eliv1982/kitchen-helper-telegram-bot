import asyncio
from typing import TypedDict

from house_helper.core.identity import ConversationKey
from house_helper.core.limits import MAX_HISTORY_MESSAGES

MessageDict = TypedDict("MessageDict", {"role": str, "content": str})

# Keep only the most recent completed exchanges so a long-running
# conversation doesn't grow memory or per-request context size without
# bound. Must stay even: trimming always drops a whole user/assistant pair
# from the front, so the role sequence is never split (a lone assistant
# message can never become the oldest entry).
#
# Trimming is deferred (see trim_history()) rather than happening inside
# add_message(): a pending user message added on top of a full history must
# be free to sit there un-trimmed while its request is in flight, so a
# failed or cancelled request can roll it back without having already
# discarded a previously completed pair to make room for it.
#
# MAX_HISTORY_MESSAGES (20) is defined in house_helper.core.limits, shared with the web
# chat, and imported above. It stays a name of this module: trim_history() reads it at
# call time, which is what lets a test replace it.

# conversation key -> list of chat messages (user/assistant only)
_contexts: dict[ConversationKey, list[MessageDict]] = {}

# conversation key -> the lock serializing that conversation, created lazily.
# Guarantees that the whole "user message -> history update -> backend request
# -> assistant response / rollback" transaction never overlaps for the same
# conversation, while different conversations are still processed
# concurrently.
#
# The lock lives next to the history it protects, and there is exactly one
# registry per process: however many services or transports operate on a
# conversation, they all contend on the same lock over the same history.
_locks: dict[ConversationKey, asyncio.Lock] = {}


def get_lock(key: ConversationKey) -> asyncio.Lock:
    """The lock serializing a conversation (the same object for equal keys).

    Never replaced or dropped when a conversation is cleared: a reset runs
    under this lock, so swapping it out would let the next turn slip past a
    queued one.
    """
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def get_context(key: ConversationKey) -> list[MessageDict]:
    if key not in _contexts:
        _contexts[key] = []
    return _contexts[key]


def add_message(key: ConversationKey, role: str, content: str) -> None:
    """Append a message to a conversation's history.

    Does not trim. Retention is enforced separately by trim_history(),
    which callers must invoke only once a transaction has fully succeeded.
    """
    ctx = get_context(key)
    ctx.append({"role": role, "content": content})


def trim_history(key: ConversationKey) -> None:
    """Drop the oldest completed user/assistant pair(s) down to the bound.

    Call this only after a turn has fully completed (the assistant reply
    is already appended) so a failed or cancelled request -- which never
    calls this -- can't lose a previously completed exchange.
    """
    ctx = get_context(key)
    while len(ctx) > MAX_HISTORY_MESSAGES:
        del ctx[0:2]


def clear_context(key: ConversationKey) -> None:
    _contexts[key] = []


def remove_last_message(key: ConversationKey) -> None:
    """Remove the most recently added message, if any (used for rollback)."""
    ctx = get_context(key)
    if ctx:
        ctx.pop()


def context_length(key: ConversationKey) -> int:
    return len(get_context(key))
