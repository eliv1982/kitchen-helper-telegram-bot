"""The boundary between the chat core and the AI provider.

The core talks to a ChatBackend and nothing else, so it can be exercised
offline with a fake. OpenAIBackend is the only real implementation: a thin
adapter over api_client that turns the provider's exceptions into the
backend errors below and leaves everything else alone.

api_client.py stays where it is and is still the only code that talks to
OpenAI; this module reaches it through the module attribute on every call, so
replacing api_client.get_chat_response at runtime keeps working.

api_client is imported when the adapter first runs, not when this module is
imported: it pulls in config, which reads the .env file at import time, and
merely importing the chat core (ChatService, the backend contract, the errors
below) must not load provider configuration. The same goes for the openai SDK
itself: its exception classes are imported on first use too, so that the web
process, which imports the contract and the errors from here, loads neither the
SDK nor the Telegram-side provider layer just by starting.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from house_helper.core.identity import ConversationKey


class BackendError(Exception):
    """The backend failed in a way the application recognizes.

    Raised as-is for a provider API failure with no more specific kind. The
    subclasses below are the specific kinds.
    """


class BackendRateLimitedError(BackendError):
    """The provider is rate limiting requests."""


class BackendConnectionError(BackendError):
    """The provider could not be reached: a connection failure or a timeout."""


class BackendEmptyResponseError(BackendError):
    """The provider answered, but the response is missing, empty or unusable."""


@dataclass(frozen=True)
class BackendReply:
    """What a backend returns for one request.

    `truncated` is True when the provider stopped because the answer reached the
    length cap, so `text` ends mid-thought. Only a backend that can tell sets it;
    the default (False) is what every other one means.
    """

    text: str
    usage: dict[str, int] | None = None
    truncated: bool = False


class ChatBackend(Protocol):
    """Anything that can answer a conversation.

    `messages` is the conversation so far, user and assistant turns only,
    ending with the pending user message; the system prompt is the backend's
    own business. A message's content is a string, or (the web chat only, for a
    message that came with a photo) a list of parts, {"type": "text", "text": str}
    and {"type": "image", "content_type": str, "data": bytes}: a backend that
    cannot show a picture is never given one. `conversation` names the conversation being answered, on
    whatever channel it lives; it and `context_len` are carried over from the
    current call as-is and only label the backend's log lines.

    Recognized failures are raised as BackendError subclasses. Anything else,
    cancellation included, must propagate untouched.
    """

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        conversation: ConversationKey,
        context_len: int,
    ) -> BackendReply: ...


def _log_label(conversation: ConversationKey) -> str:
    """The name a conversation is logged under, e.g. "telegram:39" or "web:39".

    Channel-qualified, so the same subject on two channels never looks like one
    conversation in the logs. A label for reading logs only: the key itself
    stays the conversation's identity.
    """
    return f"{conversation.channel}:{conversation.subject}"


class OpenAIBackend:
    """ChatBackend backed by api_client.get_chat_response.

    The failure kinds follow the order in which they were always checked, and
    the original exception stays attached as __cause__.
    """

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        conversation: ConversationKey,
        context_len: int,
    ) -> BackendReply:
        # Deferred on purpose, see the module docstring.
        import api_client
        from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError

        try:
            text, usage = await api_client.get_chat_response(
                messages,
                user_id=_log_label(conversation),
                context_len=context_len,
            )
        except RateLimitError as exc:
            raise BackendRateLimitedError("OpenAI rate limit exceeded") from exc
        except (APITimeoutError, APIConnectionError) as exc:
            raise BackendConnectionError("OpenAI could not be reached") from exc
        except api_client.InvalidResponseError as exc:
            raise BackendEmptyResponseError("OpenAI response is unusable") from exc
        except APIError as exc:
            raise BackendError("OpenAI API error") from exc
        return BackendReply(text=text, usage=usage)
