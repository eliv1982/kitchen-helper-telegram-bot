from typing import TypedDict

from house_helper.core.identity import ConversationKey

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

# conversation key -> list of chat messages (user/assistant only)
_contexts: dict[ConversationKey, list[MessageDict]] = {}


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
