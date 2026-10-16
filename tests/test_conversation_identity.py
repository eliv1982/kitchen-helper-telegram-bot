"""How the application keys conversation state and serialization locks.

Telegram conversations live under ConversationKey("telegram", str(sender id)).
These tests pin that mapping and show it changes nothing about behavior:
conversations are still scoped to the sending user (not the chat), the AI layer
and logs still see the raw sender id, and a conversation from another channel
with the same subject is a different conversation.
"""

import asyncio
import logging

import pytest

import context_manager
from house_helper.core.identity import ConversationKey
from tests import seams
from tests.helpers import FakeBot, FakeMessage

OTHER_CHANNEL = "web"


# --- the Telegram mapping ---------------------------------------------------


def test_the_seam_builds_the_documented_telegram_key():
    assert seams.conversation_key(31) == ConversationKey("telegram", "31")


@pytest.mark.asyncio
async def test_a_text_message_is_stored_and_locked_under_the_namespaced_key(monkeypatch):
    seams.install_ai(monkeypatch, seams.RecordingAi())

    await seams.process_text(FakeMessage(user_id=31, text="привет"), FakeBot())

    expected = {ConversationKey("telegram", "31")}
    assert seams.stored_conversation_keys() == expected
    assert seams.lock_keys() == expected


@pytest.mark.asyncio
async def test_no_state_is_kept_under_the_bare_sender_id(monkeypatch):
    seams.install_ai(monkeypatch, seams.RecordingAi())

    await seams.process_text(FakeMessage(user_id=32, text="привет"), FakeBot())

    assert 32 not in context_manager._contexts
    assert "32" not in context_manager._contexts


@pytest.mark.asyncio
async def test_the_same_sender_in_different_chats_uses_one_key(monkeypatch):
    seams.install_ai(monkeypatch, seams.RecordingAi())

    await seams.process_text(FakeMessage(user_id=33, text="а", chat_id=100), FakeBot())
    await seams.process_text(FakeMessage(user_id=33, text="б", chat_id=-200), FakeBot())

    expected = {ConversationKey("telegram", "33")}
    assert seams.stored_conversation_keys() == expected
    assert seams.lock_keys() == expected


@pytest.mark.asyncio
async def test_different_senders_get_different_keys(monkeypatch):
    seams.install_ai(monkeypatch, seams.RecordingAi())
    group_chat = -1001234567890

    await seams.process_text(
        FakeMessage(user_id=34, text="а", chat_id=group_chat), FakeBot()
    )
    await seams.process_text(
        FakeMessage(user_id=35, text="б", chat_id=group_chat), FakeBot()
    )

    expected = {ConversationKey("telegram", "34"), ConversationKey("telegram", "35")}
    assert seams.stored_conversation_keys() == expected
    assert seams.lock_keys() == expected


@pytest.mark.asyncio
async def test_reset_command_and_reset_phrase_act_on_the_same_key(monkeypatch):
    seams.install_ai(monkeypatch, seams.RecordingAi())
    seams.seed_exchanges(36, pairs=1)
    seams.seed_exchanges(37, pairs=1)

    await seams.process_reset_command(FakeMessage(user_id=36, text="/reset"))
    await seams.process_text(FakeMessage(user_id=37, text="очистить контекст"), FakeBot())

    assert seams.history_of(ConversationKey("telegram", "36")) == []
    assert seams.history_of(ConversationKey("telegram", "37")) == []
    assert seams.lock_keys() == {
        ConversationKey("telegram", "36"),
        ConversationKey("telegram", "37"),
    }


# --- unchanged behavior around the key --------------------------------------


@pytest.mark.asyncio
async def test_the_ai_layer_still_receives_the_raw_sender_id(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)

    await seams.process_text(FakeMessage(user_id=38, text="привет"), FakeBot())

    assert ai.calls[0].user_id == 38
    assert isinstance(ai.calls[0].user_id, int)


@pytest.mark.asyncio
async def test_reset_log_line_still_reports_the_raw_sender_id(caplog):
    with caplog.at_level(logging.INFO, logger="bot"):
        await seams.process_reset_command(FakeMessage(user_id=39, text="/reset"))
        await seams.process_text(
            FakeMessage(user_id=40, text="очистить контекст"), FakeBot()
        )

    messages = [record.getMessage() for record in caplog.records]
    assert "Context cleared via /reset for user_id=39" in messages
    assert "Context cleared via reset phrase for user_id=40" in messages


# --- other channels are other conversations ---------------------------------


@pytest.mark.asyncio
async def test_a_foreign_channel_with_the_same_subject_is_not_part_of_the_history(
    monkeypatch,
):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    foreign = ConversationKey(OTHER_CHANNEL, "51")
    foreign_history = seams.seed_conversation(
        foreign, [("user", "из другого канала"), ("assistant", "ответ там")]
    )

    await seams.process_text(FakeMessage(user_id=51, text="привет"), FakeBot())

    assert ai.calls[0].history == [{"role": "user", "content": "привет"}]
    assert seams.history_of(foreign) == foreign_history


@pytest.mark.asyncio
async def test_telegram_reset_leaves_a_foreign_channel_conversation_alone():
    foreign = ConversationKey(OTHER_CHANNEL, "52")
    foreign_history = seams.seed_conversation(foreign, [("user", "там"), ("assistant", "ок")])
    seams.seed_exchanges(52, pairs=1)

    await seams.process_reset_command(FakeMessage(user_id=52, text="/reset"))

    assert seams.history(52) == []
    assert seams.history_of(foreign) == foreign_history


@pytest.mark.asyncio
async def test_a_busy_foreign_conversation_does_not_block_the_telegram_one(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    foreign_lock = seams.lock_for(ConversationKey(OTHER_CHANNEL, "53"))

    async with foreign_lock:
        await asyncio.wait_for(
            seams.process_text(FakeMessage(user_id=53, text="привет"), FakeBot()),
            timeout=2,
        )
        assert not seams.is_user_busy(53)
        assert foreign_lock.locked()

    assert ai.called


@pytest.mark.asyncio
async def test_a_busy_telegram_conversation_does_not_block_the_foreign_one():
    foreign_lock = seams.lock_for(ConversationKey(OTHER_CHANNEL, "54"))

    async with seams.lock_for(seams.conversation_key(54)):
        assert seams.is_user_busy(54)
        assert not foreign_lock.locked()
        async with foreign_lock:
            assert foreign_lock.locked()


# --- conversation state keyed by ConversationKey ----------------------------


def test_history_is_kept_separately_per_key():
    telegram = ConversationKey("telegram", "60")
    other = ConversationKey(OTHER_CHANNEL, "60")

    seams.seed_conversation(telegram, [("user", "t1"), ("assistant", "t2")])
    seams.seed_conversation(other, [("user", "o1")])

    assert [turn["content"] for turn in seams.history_of(telegram)] == ["t1", "t2"]
    assert [turn["content"] for turn in seams.history_of(other)] == ["o1"]


def test_clearing_and_rolling_back_one_key_leaves_the_other_untouched():
    telegram = ConversationKey("telegram", "61")
    other = ConversationKey(OTHER_CHANNEL, "61")
    seams.seed_conversation(telegram, [("user", "t1"), ("assistant", "t2")])
    other_history = seams.seed_conversation(other, [("user", "o1"), ("assistant", "o2")])

    context_manager.remove_last_message(telegram)
    assert [turn["content"] for turn in seams.history_of(telegram)] == ["t1"]
    assert seams.history_of(other) == other_history

    context_manager.clear_context(telegram)
    assert seams.history_of(telegram) == []
    assert seams.history_of(other) == other_history


def test_an_equal_but_separately_built_key_reaches_the_same_history():
    seams.seed_conversation(ConversationKey("telegram", "62"), [("user", "привет")])

    assert context_manager.context_length(ConversationKey("telegram", "62")) == 1
