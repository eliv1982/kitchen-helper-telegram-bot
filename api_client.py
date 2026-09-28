import logging
from typing import Any

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

import config

logger = logging.getLogger(__name__)

SYSTEM_MESSAGE = (
    "Ты Kitchen Helper — дружелюбный помощник по простым домашним блюдам. "
    "Помогай пользователю придумать, что приготовить из доступных продуктов. "
    "Отвечай кратко, практично и по-русски. "
    "Не давай медицинских, диетологических или опасных советов. "
    "Если данных мало, предложи 2-3 варианта и задай один уточняющий вопрос. "
    "Не выдумывай наличие продуктов, которых пользователь не называл."
)


class InvalidResponseError(Exception):
    """Raised when the OpenAI response is missing, empty, or otherwise unusable."""


_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Return the shared AsyncOpenAI client, creating it on first use.

    Construction is deferred (rather than happening at import time) so a
    missing or malformed API key is always caught by
    config.validate_config() at startup, instead of a client getting built
    before anyone has validated the settings it relies on.
    """
    global _client
    if _client is None:
        settings = config.get_settings()
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
        )
    return _client


async def close_client() -> None:
    """Close the shared client's underlying HTTP resources, if it exists.

    Safe to call even if a client was never created (e.g. the bot shut down
    before handling any message).
    """
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def get_chat_response(
    messages: list[dict[str, str]],
    user_id: int,
    context_len: int,
) -> tuple[str, dict[str, int] | None]:
    """
    Send messages to OpenAI and return assistant text and optional usage stats.
    """
    settings = config.get_settings()
    full_messages = [{"role": "system", "content": SYSTEM_MESSAGE}, *messages]

    logger.info(
        "OpenAI request: model=%s, temperature=%s, max_tokens=%s, user_id=%s, context_len=%s",
        settings.openai_model,
        settings.temperature,
        settings.max_tokens,
        user_id,
        context_len,
    )

    client = get_client()
    try:
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=full_messages,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
        )
    except RateLimitError:
        logger.error("OpenAI rate limit exceeded for user_id=%s", user_id)
        raise
    except APITimeoutError:
        logger.error("OpenAI request timed out for user_id=%s", user_id)
        raise
    except APIConnectionError:
        logger.error("OpenAI connection error for user_id=%s", user_id)
        raise
    except APIError as exc:
        logger.error("OpenAI API error for user_id=%s: %s", user_id, exc)
        raise

    content = _extract_content(response, user_id=user_id)
    usage = _extract_usage(response)
    if usage:
        logger.info(
            "OpenAI usage: input_tokens=%s, output_tokens=%s, total_tokens=%s",
            usage["input_tokens"],
            usage["output_tokens"],
            usage["total_tokens"],
        )

    return content, usage


def _extract_content(response: Any, user_id: int) -> str:
    choices = getattr(response, "choices", None)
    if not choices:
        logger.error("OpenAI response has no choices for user_id=%s", user_id)
        raise InvalidResponseError("OpenAI response contains no choices")

    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None) if message else None

    if not isinstance(content, str) or not content.strip():
        logger.error(
            "OpenAI response has empty or invalid content for user_id=%s", user_id
        )
        raise InvalidResponseError("OpenAI response content is empty or invalid")

    return content


def _extract_usage(response: Any) -> dict[str, int] | None:
    # Usage is metadata, not part of the success condition: a missing or
    # malformed usage object must never turn a valid answer into an error,
    # so any shape surprise here degrades to None instead of raising.
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
