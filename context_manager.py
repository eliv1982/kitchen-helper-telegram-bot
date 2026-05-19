from typing import TypedDict

MessageDict = TypedDict("MessageDict", {"role": str, "content": str})

# user_id -> list of chat messages (user/assistant only)
_contexts: dict[int, list[MessageDict]] = {}


def get_context(user_id: int) -> list[MessageDict]:
    if user_id not in _contexts:
        _contexts[user_id] = []
    return _contexts[user_id]


def add_message(user_id: int, role: str, content: str) -> None:
    get_context(user_id).append({"role": role, "content": content})


def clear_context(user_id: int) -> None:
    _contexts[user_id] = []


def context_length(user_id: int) -> int:
    return len(get_context(user_id))
