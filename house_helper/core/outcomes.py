from dataclasses import dataclass
from enum import Enum


class OutcomeStatus(str, Enum):
    """How one user turn ended, as far as the application is concerned.

    Only application-level results belong here. A reply that could not be
    delivered is a property of the transport, not of the turn, so the transport
    reports that on its own and it has no status. An unexpected failure is not
    an outcome either: it propagates as an exception.

    The members compare equal to their string values, but do not rely on str()
    or f-string formatting of a member to produce the value: they do not, and
    the formatting rules of mixed-in enums have changed between Python
    versions. Use `.value`.
    """

    REPLIED = "replied"
    CONTEXT_CLEARED = "context_cleared"
    INPUT_TOO_LONG = "input_too_long"
    RATE_LIMITED = "rate_limited"
    CONNECTION_FAILED = "connection_failed"
    EMPTY_RESPONSE = "empty_response"
    API_ERROR = "api_error"


@dataclass(frozen=True)
class ChatOutcome:
    """The result of one user turn: a status plus the data that status needs.

    `reply` is required for REPLIED and `max_input_length` for INPUT_TOO_LONG;
    every other status carries nothing. Wording is not decided here -- turning
    an outcome into text is the job of a message catalog.
    """

    status: OutcomeStatus
    reply: str | None = None
    max_input_length: int | None = None

    def __post_init__(self) -> None:
        if self.status is OutcomeStatus.REPLIED and self.reply is None:
            raise ValueError("a REPLIED outcome requires a reply")
        if self.status is OutcomeStatus.INPUT_TOO_LONG and self.max_input_length is None:
            raise ValueError("an INPUT_TOO_LONG outcome requires max_input_length")
