"""Contract of the outcome model (house_helper.core.outcomes)."""

import dataclasses

import pytest

from house_helper.core.outcomes import ChatOutcome, OutcomeStatus


def test_statuses_are_stable_plain_strings():
    # The values may end up in logs or a future API, so they are pinned.
    assert {status.name: status.value for status in OutcomeStatus} == {
        "REPLIED": "replied",
        "CONTEXT_CLEARED": "context_cleared",
        "INPUT_TOO_LONG": "input_too_long",
        "RATE_LIMITED": "rate_limited",
        "CONNECTION_FAILED": "connection_failed",
        "EMPTY_RESPONSE": "empty_response",
        "API_ERROR": "api_error",
    }
    assert all(isinstance(status, str) for status in OutcomeStatus)
    assert OutcomeStatus.RATE_LIMITED == "rate_limited"


def test_outcomes_without_payload_carry_nothing():
    outcome = ChatOutcome(OutcomeStatus.RATE_LIMITED)

    assert outcome.reply is None
    assert outcome.max_input_length is None


def test_outcomes_compare_by_value():
    assert ChatOutcome(OutcomeStatus.REPLIED, reply="a") == ChatOutcome(
        OutcomeStatus.REPLIED, reply="a"
    )
    assert ChatOutcome(OutcomeStatus.REPLIED, reply="a") != ChatOutcome(
        OutcomeStatus.REPLIED, reply="b"
    )


def test_outcomes_are_immutable():
    outcome = ChatOutcome(OutcomeStatus.REPLIED, reply="a")

    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.reply = "b"


def test_replied_requires_a_reply():
    with pytest.raises(ValueError):
        ChatOutcome(OutcomeStatus.REPLIED)


def test_input_too_long_requires_the_limit():
    with pytest.raises(ValueError):
        ChatOutcome(OutcomeStatus.INPUT_TOO_LONG)
