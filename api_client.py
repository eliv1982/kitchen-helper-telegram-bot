import logging
from typing import Any

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError

from config import MAX_TOKENS, OPENAI_API_KEY, OPENAI_MODEL, TEMPERATURE

logger = logging.getLogger(__name__)

SYSTEM_MESSAGE = (
    "Ты Kitchen Helper — дружелюбный помощник по простым домашним блюдам. "
    "Помогай пользователю придумать, что приготовить из доступных продуктов. "
    "Отвечай кратко, практично и по-русски. "
    "Не давай медицинских, диетологических или опасных советов. "
    "Если данных мало, предложи 2-3 варианта и задай один уточняющий вопрос. "
    "Не выдумывай наличие продуктов, которых пользователь не называл."
)

_client = OpenAI(api_key=OPENAI_API_KEY)


def get_chat_response(
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
        response = _client.chat.completions.create(
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

    content = response.choices[0].message.content or ""
    usage = _extract_usage(response)
    if usage:
        logger.info(
            "OpenAI usage: input_tokens=%s, output_tokens=%s, total_tokens=%s",
            usage["input_tokens"],
            usage["output_tokens"],
            usage["total_tokens"],
        )

    return content, usage


def _extract_usage(response: Any) -> dict[str, int] | None:
    if response.usage is None:
        return None
    return {
        "input_tokens": response.usage.prompt_tokens,
        "output_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
    }
