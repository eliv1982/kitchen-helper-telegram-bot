"""The Russian message catalog, checked against independent literals.

tests/expected_texts.py is deliberately not derived from production code, so
this is a comparison against the wording as originally shipped, not a catalog
checking itself.
"""

import pytest

from house_helper.core import messages_ru
from house_helper.core.outcomes import ChatOutcome, OutcomeStatus
from tests import expected_texts

GOLDEN = [
    pytest.param(
        ChatOutcome(OutcomeStatus.CONTEXT_CLEARED),
        expected_texts.RESET_CONFIRMATION,
        id="context-cleared",
    ),
    pytest.param(
        ChatOutcome(OutcomeStatus.INPUT_TOO_LONG, max_input_length=2000),
        expected_texts.TOO_LONG_2000,
        id="input-too-long-2000",
    ),
    pytest.param(
        ChatOutcome(OutcomeStatus.INPUT_TOO_LONG, max_input_length=10),
        expected_texts.TOO_LONG_10,
        id="input-too-long-10",
    ),
    pytest.param(
        ChatOutcome(OutcomeStatus.RATE_LIMITED),
        expected_texts.RATE_LIMITED,
        id="rate-limited",
    ),
    pytest.param(
        ChatOutcome(OutcomeStatus.CONNECTION_FAILED),
        expected_texts.CONNECTION_FAILED,
        id="connection-failed",
    ),
    pytest.param(
        ChatOutcome(OutcomeStatus.EMPTY_RESPONSE),
        expected_texts.EMPTY_RESPONSE,
        id="empty-response",
    ),
    pytest.param(
        ChatOutcome(OutcomeStatus.API_ERROR),
        expected_texts.API_ERROR,
        id="api-error",
    ),
]


@pytest.mark.parametrize(("outcome", "expected"), GOLDEN)
def test_catalog_text_is_exactly_the_shipped_wording(outcome, expected):
    assert messages_ru.render(outcome) == expected


def test_every_status_is_either_golden_checked_or_the_reply_passthrough():
    # Adding a status without deciding its wording (and adding a golden text
    # for it) must fail here rather than at runtime.
    checked = {param.values[0].status for param in GOLDEN}

    assert checked | {OutcomeStatus.REPLIED} == set(OutcomeStatus)


@pytest.mark.parametrize(
    "reply",
    ["  padded reply \n\n", "**bold** <b>html</b> & _md_ `code`", "я" * 5000],
)
def test_a_reply_is_rendered_verbatim(reply):
    assert messages_ru.render(ChatOutcome(OutcomeStatus.REPLIED, reply=reply)) == reply
