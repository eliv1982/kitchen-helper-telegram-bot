import logging
from typing import Any

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI, RateLimitError

from config import (
    MAX_TOKENS,
    OPENAI_API_KEY,
    OPENAI_MAX_RETRIES,
    OPENAI_MODEL,
    OPENAI_TIMEOUT_SECONDS,
    TEMPERATURE,
)

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


_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    timeout=OPENAI_TIMEOUT_SECONDS,
    max_retries=OPENAI_MAX_RETRIES,
)


async def get_chat_response(
    messages: list[dict[str, str]],
    user_id: int,
    context_len: int,
) -> tuple[str, dict[str, int] | None]:
    """
    Send messages to OpenAI and return assistant text and optional usage stats.
    """
    full_messages = [{"role": "system", "content": SYSTEM_MESSAGE}, *messages]

    logger.info(
        "OpenAI request: model=%s, temperature=%s, max_tokens=%s, user_id=%s, context_len=%s",
        OPENAI_MODEL,
        TEMPERATURE,
        MAX_TOKENS,
        user_id,
        context_len,
    )

    try:
        response = await _client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=full_messages,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
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
