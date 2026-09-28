import context_manager


def _add_exchange(user_id: int, index: int) -> None:
    """Simulate one full, successful transaction: both messages are added,
    then trimming runs once -- matching bot.py's deferred-trim contract,
    where add_message() itself never trims."""
    context_manager.add_message(user_id, "user", f"user-{index}")
    context_manager.add_message(user_id, "assistant", f"assistant-{index}")
    context_manager.trim_history(user_id)


def test_history_is_not_trimmed_below_the_configured_bound(monkeypatch):
    monkeypatch.setattr(context_manager, "MAX_HISTORY_MESSAGES", 10)
    user_id = 1

    for i in range(1, 4):  # 3 exchanges = 6 messages, under the bound of 10
        _add_exchange(user_id, i)

    ctx = context_manager.get_context(user_id)
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
    user_id = 2

    for i in range(1, 6):  # 5 exchanges = 10 messages, over the bound of 4
        _add_exchange(user_id, i)

    ctx = context_manager.get_context(user_id)
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
    user_id = 3

    for i in range(1, 8):
        _add_exchange(user_id, i)
        ctx = context_manager.get_context(user_id)
        # Every message at an even index (0-based) must be a "user" message,
        # and every odd index an "assistant" message -- the pairing is never
        # corrupted mid-trim.
        for pos, message in enumerate(ctx):
            expected_role = "user" if pos % 2 == 0 else "assistant"
            assert message["role"] == expected_role


def test_reset_clears_history_even_after_trimming(monkeypatch):
    monkeypatch.setattr(context_manager, "MAX_HISTORY_MESSAGES", 4)
    user_id = 4

    for i in range(1, 6):
        _add_exchange(user_id, i)

    assert context_manager.get_context(user_id) != []

    context_manager.clear_context(user_id)

    assert context_manager.get_context(user_id) == []


def test_add_message_never_trims_on_its_own(monkeypatch):
    # This is the core of the transactional contract: appending a pending
    # message -- even one that pushes a full history over the bound -- must
    # never discard anything by itself. Only trim_history() may do that, and
    # only a caller who knows the transaction succeeded should call it.
    monkeypatch.setattr(context_manager, "MAX_HISTORY_MESSAGES", 4)
    user_id = 5

    for i in range(1, 3):  # 2 exchanges = 4 messages, exactly at the bound
        _add_exchange(user_id, i)
    assert len(context_manager.get_context(user_id)) == 4

    context_manager.add_message(user_id, "user", "pending-user-msg")

    ctx = context_manager.get_context(user_id)
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
    user_id = 6

    context_manager.add_message(user_id, "user", "hi")
    context_manager.trim_history(user_id)

    assert context_manager.get_context(user_id) == [{"role": "user", "content": "hi"}]
