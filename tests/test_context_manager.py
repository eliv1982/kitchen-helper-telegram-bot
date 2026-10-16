import context_manager
from house_helper.core.identity import ConversationKey


def _add_exchange(key: ConversationKey, index: int) -> None:
    """Simulate one full, successful transaction: both messages are added,
    then trimming runs once -- matching bot.py's deferred-trim contract,
    where add_message() itself never trims."""
    context_manager.add_message(key, "user", f"user-{index}")
    context_manager.add_message(key, "assistant", f"assistant-{index}")
    context_manager.trim_history(key)


def test_history_is_not_trimmed_below_the_configured_bound(monkeypatch):
    monkeypatch.setattr(context_manager, "MAX_HISTORY_MESSAGES", 10)
    key = ConversationKey("telegram", "1")

    for i in range(1, 4):  # 3 exchanges = 6 messages, under the bound of 10
        _add_exchange(key, i)

    ctx = context_manager.get_context(key)
    assert len(ctx) == 6
    assert [m["content"] for m in ctx] == [
        "user-1",
        "assistant-1",
        "user-2",
        "assistant-2",
        "user-3",
        "assistant-3",
    ]


def test_history_trims_to_the_configured_bound(monkeypatch):
    monkeypatch.setattr(context_manager, "MAX_HISTORY_MESSAGES", 4)
    key = ConversationKey("telegram", "2")

    for i in range(1, 6):  # 5 exchanges = 10 messages, over the bound of 4
        _add_exchange(key, i)

    ctx = context_manager.get_context(key)
    # Only the most recent 2 exchanges (4 messages) survive.
    assert len(ctx) == 4
    assert [m["content"] for m in ctx] == [
        "user-4",
        "assistant-4",
        "user-5",
        "assistant-5",
    ]


def test_trimming_never_splits_a_user_assistant_pair(monkeypatch):
    monkeypatch.setattr(context_manager, "MAX_HISTORY_MESSAGES", 4)
    key = ConversationKey("telegram", "3")

    for i in range(1, 8):
        _add_exchange(key, i)
        ctx = context_manager.get_context(key)
        # Every message at an even index (0-based) must be a "user" message,
        # and every odd index an "assistant" message -- the pairing is never
        # corrupted mid-trim.
        for pos, message in enumerate(ctx):
            expected_role = "user" if pos % 2 == 0 else "assistant"
            assert message["role"] == expected_role


def test_reset_clears_history_even_after_trimming(monkeypatch):
    monkeypatch.setattr(context_manager, "MAX_HISTORY_MESSAGES", 4)
    key = ConversationKey("telegram", "4")

    for i in range(1, 6):
        _add_exchange(key, i)

    assert context_manager.get_context(key) != []

    context_manager.clear_context(key)

    assert context_manager.get_context(key) == []


def test_add_message_never_trims_on_its_own(monkeypatch):
    # This is the core of the transactional contract: appending a pending
    # message -- even one that pushes a full history over the bound -- must
    # never discard anything by itself. Only trim_history() may do that, and
    # only a caller who knows the transaction succeeded should call it.
    monkeypatch.setattr(context_manager, "MAX_HISTORY_MESSAGES", 4)
    key = ConversationKey("telegram", "5")

    for i in range(1, 3):  # 2 exchanges = 4 messages, exactly at the bound
        _add_exchange(key, i)
    assert len(context_manager.get_context(key)) == 4

    context_manager.add_message(key, "user", "pending-user-msg")

    ctx = context_manager.get_context(key)
    assert len(ctx) == 5
    assert [m["content"] for m in ctx] == [
        "user-1",
        "assistant-1",
        "user-2",
        "assistant-2",
        "pending-user-msg",
    ]


def test_trim_history_is_a_no_op_under_the_bound(monkeypatch):
    monkeypatch.setattr(context_manager, "MAX_HISTORY_MESSAGES", 10)
    key = ConversationKey("telegram", "6")

    context_manager.add_message(key, "user", "hi")
    context_manager.trim_history(key)

    assert context_manager.get_context(key) == [{"role": "user", "content": "hi"}]


# --- the shared per-conversation lock registry ------------------------------


def test_a_conversation_has_one_lock():
    key = ConversationKey("telegram", "7")

    assert context_manager.get_lock(key) is context_manager.get_lock(key)


def test_an_equal_but_separately_built_key_reaches_the_same_lock():
    first = context_manager.get_lock(ConversationKey("telegram", "8"))

    assert context_manager.get_lock(ConversationKey("telegram", "8")) is first


def test_different_conversations_get_different_locks():
    telegram = ConversationKey("telegram", "9")

    assert context_manager.get_lock(telegram) is not context_manager.get_lock(
        ConversationKey("telegram", "10")
    )
    # The same subject on another channel is another conversation.
    assert context_manager.get_lock(telegram) is not context_manager.get_lock(
        ConversationKey("web", "9")
    )


def test_locks_are_created_lazily_per_key():
    assert context_manager._locks == {}

    key = ConversationKey("web", "11")
    lock = context_manager.get_lock(key)

    assert context_manager._locks == {key: lock}


def test_clearing_and_rolling_back_history_keep_the_conversations_lock():
    key = ConversationKey("telegram", "12")
    lock = context_manager.get_lock(key)
    context_manager.add_message(key, "user", "привет")

    context_manager.remove_last_message(key)
    context_manager.clear_context(key)

    assert context_manager.get_lock(key) is lock
