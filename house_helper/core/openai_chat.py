"""The AI backend of the web chat: OpenAI, configured by the settings it is given.

OpenAIBackend (backend.py) is the Telegram bot's adapter: it goes through api_client,
which reads config.py, which demands BOT_TOKEN and reads the .env file when imported.
The web process has no bot token and must not load either, so this is the other
ChatBackend: the same contract (the errors and BackendReply of backend.py), the same
Kitchen prompt, but the key, the model and the limits come from a ProviderSettings that
the caller built (house_helper.core.provider_settings), and nothing global is read.

The openai SDK is imported on first use, not when this module is: starting the web
process loads no provider code. The client is created on the first request (a process
that never answers a chat message never builds one) and closed by aclose().

Logging: the request line names the model and the limits and the conversation (never
its text); a failure names the class of the exception and its HTTP status. Never the
exception's own message: the provider's text can quote the request, and an
authentication failure quotes the key. The original exception stays attached as
`__cause__` for whoever handles it in code.

What this adds to the Telegram path: the provider's finish reason is read, and an
answer that was cut off at the length cap is reported (`BackendReply.truncated`)
instead of being passed on as if it were complete.
"""

import logging
from typing import Any, Optional

from house_helper.core.backend import (
    BackendConnectionError,
    BackendEmptyResponseError,
    BackendError,
    BackendRateLimitedError,
    BackendReply,
)
from house_helper.core.identity import ConversationKey
from house_helper.core.kitchen_prompt import KITCHEN_SYSTEM_PROMPT
from house_helper.core.provider_settings import ProviderSettings

logger = logging.getLogger(__name__)

# The provider's own word for "stopped because the answer reached max_tokens".
FINISH_REASON_LENGTH = "length"


class StandaloneOpenAIBackend:
    """ChatBackend over the OpenAI chat completions API, with explicit settings.

    `client` is a seam for tests: an object shaped like AsyncOpenAI that this backend
    uses instead of building one (and does not close).
    """

    def __init__(self, settings: ProviderSettings, *, client: Optional[Any] = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI  # on first use, see the module docstring

            self._client = AsyncOpenAI(
                api_key=self._settings.api_key,
                timeout=self._settings.timeout_seconds,
                max_retries=self._settings.max_retries,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the client this backend built, if it did. Safe to call twice."""
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            await client.close()

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        conversation: ConversationKey,
        context_len: int,
    ) -> BackendReply:
        from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError

        settings = self._settings
        label = f"{conversation.channel}:{conversation.subject}"
        logger.info(
            "OpenAI request: model=%s, temperature=%s, max_tokens=%s, conversation=%s, context_len=%s",
            settings.model,
            settings.temperature,
            settings.max_tokens,
            label,
            context_len,
        )
        try:
            response = await self._get_client().chat.completions.create(
                model=settings.model,
                messages=[{"role": "system", "content": KITCHEN_SYSTEM_PROMPT}, *messages],
                temperature=settings.temperature,
                max_tokens=settings.max_tokens,
            )
        except RateLimitError as exc:
            self._log_failure("rate limit exceeded", exc, label)
            raise BackendRateLimitedError("OpenAI rate limit exceeded") from exc
        except (APITimeoutError, APIConnectionError) as exc:
            self._log_failure("could not be reached", exc, label)
            raise BackendConnectionError("OpenAI could not be reached") from exc
        except APIError as exc:
            self._log_failure("API error", exc, label)
            raise BackendError("OpenAI API error") from exc

        return self._reply_from(response, label)

    @staticmethod
    def _log_failure(what: str, exc: BaseException, label: str) -> None:
        logger.error(
            "OpenAI %s for conversation=%s (%s, status=%s)",
            what,
            label,
            type(exc).__name__,
            getattr(exc, "status_code", None),
        )

    @staticmethod
    def _reply_from(response: Any, label: str) -> BackendReply:
        choices = getattr(response, "choices", None)
        if not choices:
            logger.error("OpenAI response has no choices for conversation=%s", label)
            raise BackendEmptyResponseError("OpenAI response contains no choices")

        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message else None
        if not isinstance(content, str) or not content.strip():
            logger.error("OpenAI response has empty or invalid content for conversation=%s", label)
            raise BackendEmptyResponseError("OpenAI response content is empty or invalid")

        truncated = getattr(choices[0], "finish_reason", None) == FINISH_REASON_LENGTH
        if truncated:
            logger.warning("OpenAI answer was cut off at the length cap for conversation=%s", label)

        usage = _usage_of(response)
        if usage:
            logger.info(
                "OpenAI usage: input_tokens=%s, output_tokens=%s, total_tokens=%s",
                usage["input_tokens"],
                usage["output_tokens"],
                usage["total_tokens"],
            )
        return BackendReply(text=content, usage=usage, truncated=truncated)


def _usage_of(response: Any) -> Optional[dict[str, int]]:
    # Metadata, not part of the success condition: a missing or malformed usage
    # object never turns a valid answer into an error.
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        return {
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }
    except AttributeError:
        logger.warning("OpenAI usage metadata malformed; degrading to None")
        return None
