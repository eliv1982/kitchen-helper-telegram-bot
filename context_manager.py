from typing import TypedDict

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
MAX_HISTORY_MESSAGES = 20

# user_id -> list of chat messages (user/assistant only)
_contexts: dict[int, list[MessageDict]] = {}


def get_context(user_id: int) -> list[MessageDict]:
    if user_id not in _contexts:
        _contexts[user_id] = []
    return _contexts[user_id]


def add_message(user_id: int, role: str, content: str) -> None:
    """Append a message to a user's history.

    Does not trim. Retention is enforced separately by trim_history(),
    which callers must invoke only once a transaction has fully succeeded.
    """
    ctx = get_context(user_id)
    ctx.append({"role": role, "content": content})


def trim_history(user_id: int) -> None:
    """Drop the oldest completed user/assistant pair(s) down to the bound.

    Call this only after a turn has fully completed (the assistant reply
    is already appended) so a failed or cancelled request -- which never
    calls this -- can't lose a previously completed exchange.
    """
    ctx = get_context(user_id)
    while len(ctx) > MAX_HISTORY_MESSAGES:
        del ctx[0:2]


def clear_context(user_id: int) -> None:
    _contexts[user_id] = []


def remove_last_message(user_id: int) -> None:
    """Remove the most recently added message, if any (used for rollback)."""
    ctx = get_context(user_id)
    if ctx:
        ctx.pop()


def context_length(user_id: int) -> int:
    return len(get_context(user_id))
