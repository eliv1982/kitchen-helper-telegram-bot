"""ConversationKey: value semantics, namespacing and construction rules."""

import dataclasses

import pytest

from house_helper.core.identity import ConversationKey


def test_keys_with_the_same_parts_are_equal():
    assert ConversationKey("telegram", "42") == ConversationKey("telegram", "42")


def test_positional_and_keyword_construction_give_equal_keys():
    assert ConversationKey("telegram", "42") == ConversationKey(
        channel="telegram", subject="42"
    )


def test_parts_are_exposed_as_given():
    key = ConversationKey("telegram", "42")

    assert key.channel == "telegram"
    assert key.subject == "42"


def test_keys_differing_in_subject_are_not_equal():
    assert ConversationKey("telegram", "42") != ConversationKey("telegram", "43")


def test_keys_differing_in_channel_are_not_equal():
    assert ConversationKey("telegram", "42") != ConversationKey("web", "42")


def test_swapping_channel_and_subject_gives_a_different_key():
    assert ConversationKey("telegram", "web") != ConversationKey("web", "telegram")


def test_equal_keys_have_equal_hashes():
    assert hash(ConversationKey("telegram", "42")) == hash(
        ConversationKey("telegram", "42")
    )


def test_a_separately_built_equal_key_finds_the_same_dict_entry():
    store = {ConversationKey("telegram", "42"): "history"}

    assert store[ConversationKey("telegram", "42")] == "history"


def test_equal_keys_collapse_in_a_set_and_distinct_keys_do_not():
    keys = {
        ConversationKey("telegram", "42"),
        ConversationKey("telegram", "42"),
        ConversationKey("telegram", "43"),
        ConversationKey("web", "42"),
    }

    assert len(keys) == 3


def test_same_subject_on_two_channels_occupies_two_dict_entries():
    store = {
        ConversationKey("telegram", "42"): "from telegram",
        ConversationKey("web", "42"): "from web",
    }

    assert store[ConversationKey("telegram", "42")] == "from telegram"
    assert store[ConversationKey("web", "42")] == "from web"


def test_key_is_immutable():
    key = ConversationKey("telegram", "42")

    with pytest.raises(dataclasses.FrozenInstanceError):
        key.subject = "43"
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.channel = "web"


def test_key_is_not_interchangeable_with_a_plain_tuple_or_the_bare_id():
    key = ConversationKey("telegram", "42")

    assert key != ("telegram", "42")
    assert key != "42"
    assert key != 42
    assert {key: 1}.get(42) is None
    assert {42: 1}.get(key) is None


@pytest.mark.parametrize("subject", [42, None, b"42", 4.2])
def test_non_string_subject_is_rejected(subject):
    # Notably an int: ConversationKey("telegram", 42) would otherwise never
    # equal ConversationKey("telegram", "42") and split one conversation in two.
    with pytest.raises(TypeError):
        ConversationKey("telegram", subject)


@pytest.mark.parametrize("channel", [1, None, b"telegram"])
def test_non_string_channel_is_rejected(channel):
    with pytest.raises(TypeError):
        ConversationKey(channel, "42")


@pytest.mark.parametrize("parts", [("", "42"), ("telegram", ""), ("", "")])
def test_empty_parts_are_rejected(parts):
    with pytest.raises(ValueError):
        ConversationKey(*parts)
