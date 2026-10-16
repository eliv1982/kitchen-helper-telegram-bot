"""Characterization of input handling, reset behavior and conversation identity.

These tests pin the CURRENT observable behavior so a later restructuring can
be proven behavior-preserving. They describe what the bot does today, not
necessarily what it should do; where today's behavior looks like a quirk the
test says so.

Today a conversation is identified by the Telegram sender's user id alone
(`message.from_user.id`): the chat it was sent from plays no part in which
history is used or which lock serializes the work.
"""

import asyncio
from unittest.mock import call

import pytest

from tests import expected_texts, seams
from tests.helpers import FakeBot, FakeMessage

RESET_PHRASE_VARIANTS = [
    pytest.param("очистить контекст", id="exact"),
    pytest.param("Очистить контекст", id="capitalized"),
    pytest.param("ОЧИСТИТЬ КОНТЕКСТ", id="upper-case"),
    pytest.param("  очистить контекст \n", id="surrounding-whitespace"),
    pytest.param(" очистить контекст ", id="non-breaking-spaces"),
]

NOT_A_RESET_PHRASE = [
    pytest.param("очистить контекст пожалуйста", id="trailing-words"),
    pytest.param("очистить  контекст", id="double-space-inside"),
    pytest.param("очисти контекст", id="imperative-variant"),
    pytest.param("очистить контекст!", id="trailing-punctuation"),
    pytest.param("/reset", id="command-text-bypassing-command-routing"),
]


# --- input boundaries -------------------------------------------------------


def test_input_limit_is_2000_characters():
    assert seams.max_input_length() == 2000


def test_history_capacity_is_20_messages():
    assert seams.history_capacity() == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("char", ["а", "😀"], ids=["cyrillic", "emoji"])
async def test_limit_counts_characters_not_bytes(monkeypatch, char):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)

    accepted = FakeMessage(user_id=1, text=char * 2000)
    await seams.process_text(accepted, FakeBot())
    assert accepted.answer.await_args_list == [call("ok-reply")]
    assert len(ai.calls) == 1

    rejected = FakeMessage(user_id=1, text=char * 2001)
    await seams.process_text(rejected, FakeBot())
    assert rejected.answer.await_args_list == [call(expected_texts.TOO_LONG_2000)]
    assert len(ai.calls) == 1  # the oversized one never reached the AI


@pytest.mark.asyncio
async def test_surrounding_whitespace_is_stripped_before_measuring_and_storing(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    body = "а" * 2000
    msg = FakeMessage(user_id=2, text="  \n" + body + " \t")

    await seams.process_text(msg, FakeBot())

    assert msg.answer.await_args_list == [call("ok-reply")]
    assert ai.calls[0].history == [{"role": "user", "content": body}]
    assert seams.history(2)[0] == {"role": "user", "content": body}


@pytest.mark.asyncio
async def test_oversized_text_is_rejected_even_when_padded_with_whitespace(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    msg = FakeMessage(user_id=2, text=" " + "а" * 2001 + " ")

    await seams.process_text(msg, FakeBot())

    assert msg.answer.await_args_list == [call(expected_texts.TOO_LONG_2000)]
    assert not ai.called
    assert seams.history(2) == []


@pytest.mark.asyncio
async def test_interior_whitespace_is_preserved(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    text = "первая строка\n\nвторая  строка\tс табом"

    await seams.process_text(FakeMessage(user_id=3, text="\n" + text + "\n"), FakeBot())

    assert ai.calls[0].history == [{"role": "user", "content": text}]


@pytest.mark.asyncio
async def test_whitespace_only_text_is_sent_to_the_ai_as_an_empty_user_message(monkeypatch):
    # Quirk: the "no text" guard checks the raw text, so a message that is
    # only whitespace passes it, is stripped to "" and goes to the AI anyway.
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    msg = FakeMessage(user_id=4, text="   \n ")

    await seams.process_text(msg, FakeBot())

    assert ai.calls[0].history == [{"role": "user", "content": ""}]
    assert msg.answer.await_args_list == [call("ok-reply")]
    assert seams.history(4) == [
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "ok-reply"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda m: setattr(m, "text", ""), id="empty-text"),
        pytest.param(lambda m: setattr(m, "text", None), id="no-text"),
        pytest.param(lambda m: setattr(m, "from_user", None), id="no-sender"),
    ],
)
async def test_messages_without_text_or_sender_are_silently_ignored(monkeypatch, mutate):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    fake_bot = FakeBot()
    msg = FakeMessage(user_id=5, text="привет")
    mutate(msg)

    await seams.process_text(msg, fake_bot)

    msg.answer.assert_not_awaited()
    fake_bot.send_chat_action.assert_not_awaited()
    assert not ai.called
    assert seams.history(5) == []


@pytest.mark.asyncio
async def test_oversized_input_sends_no_typing_indicator(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    fake_bot = FakeBot()

    await seams.process_text(FakeMessage(user_id=6, text="а" * 2001), fake_bot)

    fake_bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_phrase_is_recognized_before_the_length_check(monkeypatch):
    # The phrase is 17 characters; with the limit at 5 it must still reset
    # rather than being rejected as too long.
    seams.set_max_input_length(monkeypatch, 5)
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    seams.seed_messages(7, [("user", "x"), ("assistant", "y")])
    msg = FakeMessage(user_id=7, text="очистить контекст")

    await seams.process_text(msg, FakeBot())

    assert msg.answer.await_args_list == [call(expected_texts.RESET_CONFIRMATION)]
    assert seams.history(7) == []
    assert not ai.called


# --- reset behavior ---------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_command_clears_history_and_confirms(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    seams.seed_exchanges(10, pairs=3)
    msg = FakeMessage(user_id=10, text="/reset")

    await seams.process_reset_command(msg)

    assert seams.history(10) == []
    assert msg.answer.await_args_list == [call(expected_texts.RESET_CONFIRMATION)]
    assert not ai.called


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", RESET_PHRASE_VARIANTS)
async def test_reset_phrase_variants_clear_history_without_calling_ai(monkeypatch, phrase):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    fake_bot = FakeBot()
    seams.seed_exchanges(11, pairs=3)
    msg = FakeMessage(user_id=11, text=phrase)

    await seams.process_text(msg, fake_bot)

    assert seams.history(11) == []
    assert msg.answer.await_args_list == [call(expected_texts.RESET_CONFIRMATION)]
    assert not ai.called
    fake_bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", NOT_A_RESET_PHRASE)
async def test_near_miss_phrases_are_ordinary_messages_for_the_ai(monkeypatch, text):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    prior = seams.seed_exchanges(12, pairs=1)
    msg = FakeMessage(user_id=12, text=text)

    await seams.process_text(msg, FakeBot())

    assert ai.calls[0].history == prior + [{"role": "user", "content": text}]
    assert msg.answer.await_args_list == [call("ok-reply")]
    assert seams.history(12) == prior + [
        {"role": "user", "content": text},
        {"role": "assistant", "content": "ok-reply"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("via_command", [True, False], ids=["command", "phrase"])
async def test_reset_with_no_history_still_confirms(via_command):
    msg = FakeMessage(user_id=13, text="/reset" if via_command else "очистить контекст")

    if via_command:
        await seams.process_reset_command(msg)
    else:
        await seams.process_text(msg, FakeBot())

    assert seams.history(13) == []
    assert msg.answer.await_args_list == [call(expected_texts.RESET_CONFIRMATION)]


@pytest.mark.asyncio
async def test_reset_affects_only_the_requesting_user():
    seams.seed_exchanges(14, pairs=2)
    other = seams.seed_exchanges(15, pairs=2)

    await seams.process_reset_command(FakeMessage(user_id=14, text="/reset"))

    assert seams.history(14) == []
    assert seams.history(15) == other


@pytest.mark.asyncio
async def test_conversation_after_reset_starts_from_the_new_message_only(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    seams.seed_exchanges(16, pairs=3)

    await seams.process_reset_command(FakeMessage(user_id=16, text="/reset"))
    await seams.process_text(FakeMessage(user_id=16, text="новое"), FakeBot())

    assert ai.calls[0].history == [{"role": "user", "content": "новое"}]


@pytest.mark.asyncio
async def test_reset_command_without_sender_is_silently_ignored():
    seams.seed_exchanges(17, pairs=1)
    before = seams.history(17)
    msg = FakeMessage(user_id=17, text="/reset")
    msg.from_user = None

    await seams.process_reset_command(msg)

    msg.answer.assert_not_awaited()
    assert seams.history(17) == before


# --- conversation identity --------------------------------------------------


@pytest.mark.asyncio
async def test_same_user_in_different_chats_shares_one_history(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    chat_a = FakeMessage(user_id=20, text="из чата A", chat_id=100)
    chat_b = FakeMessage(user_id=20, text="из чата B", chat_id=-200)

    await seams.process_text(chat_a, FakeBot())
    await seams.process_text(chat_b, FakeBot())

    assert ai.calls[1].history == [
        {"role": "user", "content": "из чата A"},
        {"role": "assistant", "content": "ok-reply"},
        {"role": "user", "content": "из чата B"},
    ]
    # Each reply goes back through the message it answers.
    assert chat_a.answer.await_args_list == [call("ok-reply")]
    assert chat_b.answer.await_args_list == [call("ok-reply")]


@pytest.mark.asyncio
async def test_different_users_in_the_same_chat_have_separate_histories(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    group_chat = -1001234567890

    first = FakeMessage(user_id=21, text="от первого", chat_id=group_chat)
    second = FakeMessage(user_id=22, text="от второго", chat_id=group_chat)

    await seams.process_text(first, FakeBot())
    await seams.process_text(second, FakeBot())

    assert ai.calls[0].history == [{"role": "user", "content": "от первого"}]
    assert ai.calls[1].history == [{"role": "user", "content": "от второго"}]
    assert seams.history(21) != seams.history(22)


@pytest.mark.asyncio
async def test_reset_from_one_chat_clears_the_users_history_seen_from_another(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)
    await seams.process_text(FakeMessage(user_id=23, text="привет", chat_id=100), FakeBot())
    assert seams.history(23) != []

    await seams.process_reset_command(FakeMessage(user_id=23, text="/reset", chat_id=-200))

    assert seams.history(23) == []


@pytest.mark.asyncio
async def test_ai_layer_is_told_the_sender_id_not_the_chat_id(monkeypatch):
    ai = seams.RecordingAi()
    seams.install_ai(monkeypatch, ai)

    await seams.process_text(FakeMessage(user_id=24, text="привет", chat_id=-777), FakeBot())

    assert ai.calls[0].user_id == 24


@pytest.mark.asyncio
async def test_serialization_is_per_sender_not_per_chat(monkeypatch):
    release = asyncio.Event()
    entered: list[str] = []

    async def gated_ai(history, user_id):
        entered.append(history[-1]["content"])
        await release.wait()
        return "ok-reply"

    seams.install_ai(monkeypatch, gated_ai)
    fake_bot = FakeBot()
    from_chat_a = FakeMessage(user_id=25, text="первое", chat_id=100)
    from_chat_b = FakeMessage(user_id=25, text="второе", chat_id=200)

    first = asyncio.create_task(seams.process_text(from_chat_a, fake_bot))
    await seams.settle()
    second = asyncio.create_task(seams.process_text(from_chat_b, fake_bot))
    await seams.settle()

    # Same sender, different chats: still one transaction at a time.
    assert entered == ["первое"]
    assert seams.is_user_busy(25)

    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
    assert entered == ["первое", "второе"]


@pytest.mark.asyncio
async def test_different_senders_in_the_same_chat_are_not_serialized(monkeypatch):
    release = asyncio.Event()
    entered: list[int] = []

    async def gated_ai(history, user_id):
        entered.append(user_id)
        if len(entered) < 2:
            await asyncio.wait_for(release.wait(), timeout=2)
        else:
            release.set()
        return "ok-reply"

    seams.install_ai(monkeypatch, gated_ai)
    fake_bot = FakeBot()
    group_chat = -1001234567890

    await asyncio.wait_for(
        asyncio.gather(
            seams.process_text(FakeMessage(user_id=26, text="а", chat_id=group_chat), fake_bot),
            seams.process_text(FakeMessage(user_id=27, text="б", chat_id=group_chat), fake_bot),
        ),
        timeout=3,
    )

    assert sorted(entered) == [26, 27]
