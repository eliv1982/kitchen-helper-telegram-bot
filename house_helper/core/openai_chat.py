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
its text); a failure names the class of the exception, its HTTP status and, when the
provider sent one that is a plain identifier (insufficient_quota, model_not_found,
unsupported_parameter, and the parameter's name), that code. Never the exception's own
message: the provider's text can quote the request, and an authentication failure
quotes the key. The original exception stays attached as `__cause__` for whoever
handles it in code.

What this adds to the Telegram path: the provider's finish reason is read, and an
answer that was cut off at the length cap is reported (`BackendReply.truncated`)
instead of being passed on as if it were complete.

Reasoning models (ProviderSettings.reasoning_effort set, see provider_settings): the
length cap counts the reasoning tokens too, so an answer can end at the cap having
produced no visible text at all. That is an empty response, as before, but the log says
why (and how many tokens the reasoning used, which is also logged for every answer).
"""

import base64
import logging
import re
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
from house_helper.core.recipes import parse_reply, render_parsed_reply

logger = logging.getLogger(__name__)

# The provider's own word for "stopped because the answer reached the token cap".
FINISH_REASON_LENGTH = "length"

# Appended to the shared prompt only when structured recipes are switched on
# (house_helper.web.settings, HOUSE_HELPER_STRUCTURED_RECIPES). The shared text is
# never changed: this is a formatting addition, and the words about what to answer
# stay the ones both channels already use. The block is defined by the sentinels of
# house_helper.core.recipes; the model is told not to repeat it in prose and not to
# wrap it in Markdown. Nothing here asks for response_format or for a second call.
RECIPE_PROTOCOL_PROMPT = (
    "\n\n"
    "ДОПОЛНЕНИЕ К ОФОРМЛЕНИЮ РЕЦЕПТОВ (служебное).\n"
    "Правила содержания ответа не меняются. Когда в ответе есть готовый рецепт с полным "
    "списком ингредиентов и шагами приготовления, оформи сам рецепт отдельным блоком, а в "
    "обычном тексте оставь разговорную часть: короткое пояснение до или после, уточнения, "
    "советы. Не повторяй в обычном тексте то, что уже вошло в блок, и не упоминай саму "
    "разметку.\n"
    "Для каждого рецепта — один блок ровно такого вида:\n"
    "<<<RECIPE>>>\n"
    '{"title": "Название", "servings": "4 порции", "ingredients": '
    '[{"name": "Свекла", "amount": "2 шт."}], "steps": ["Первый шаг.", "Второй шаг."]}\n'
    "<<<END_RECIPE>>>\n"
    "В блоке строго один JSON-объект с четырьмя ключами:\n"
    "- title — короткое название, строка;\n"
    "- servings — число порций словами или null, если не знаешь;\n"
    "- ingredients — непустой список объектов ровно с ключами name и amount (строки);\n"
    "- steps — непустой список строк, по одному шагу в каждом.\n"
    "Не добавляй другие ключи, комментарии или текст внутри блока. Не оборачивай блок в "
    "Markdown или код. Метки <<<RECIPE>>> и <<<END_RECIPE>>> используй только как границы "
    "блоков и больше нигде.\n"
    "Если рецептов несколько, дай отдельный блок для каждого, в том же порядке, не больше "
    "пяти. Если готового рецепта в ответе нет (обычный разговор, совет, замена продукта, "
    "короткий ответ), не добавляй ни одного блока."
)

# What of the provider's error may be logged besides its class and status: its `code` and
# `param`, when they are plain identifiers ("insufficient_quota", "max_tokens"). They are
# fixed words of the API, not text it echoes; anything else is left out.
_IDENTIFIER = re.compile(r"[a-z0-9_.\-]{1,64}")


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation in the shape the Chat Completions API takes. A message whose content is
    a string is passed on as it is; one made of parts has its pictures turned into `image_url`
    parts holding a data URL (the bytes, base64), the only way to give the API a picture that
    is not on the internet. Anything in a part that is not one of the two kinds is dropped:
    the parts are made by persistence.conversations, but this is the door to a provider."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            converted.append({"role": message["role"], "content": content})
            continue
        parts: list[dict[str, Any]] = []
        for part in content:
            if part.get("type") == "text":
                parts.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image":
                url = f"data:{part['content_type']};base64,{base64.b64encode(part['data']).decode('ascii')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
        converted.append({"role": message["role"], "content": parts})
    return converted


class StandaloneOpenAIBackend:
    """ChatBackend over the OpenAI chat completions API, with explicit settings.

    `client` is a seam for tests: an object shaped like AsyncOpenAI that this backend
    uses instead of building one (and does not close).

    `recipe_protocol` switches the structured-recipe mode on: the shared prompt gets
    RECIPE_PROTOCOL_PROMPT appended, and the answer is parsed by
    house_helper.core.recipes *before* it is rendered. `BackendReply.text` is the
    plain text a person reads (never a marker or JSON block), and `BackendReply.recipes`
    carries the validated recipes the same answer produced. One provider call, the
    same token limits, no response_format. With the switch off, nothing of the protocol
    is sent and the answer is returned exactly as before.
    """

    def __init__(
        self,
        settings: ProviderSettings,
        *,
        client: Optional[Any] = None,
        recipe_protocol: bool = False,
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._recipe_protocol = recipe_protocol

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
        messages: list[dict[str, Any]],
        *,
        conversation: ConversationKey,
        context_len: int,
    ) -> BackendReply:
        from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError

        settings = self._settings
        label = f"{conversation.channel}:{conversation.subject}"
        if settings.reasoning_effort is None:
            logger.info(
                "OpenAI request: model=%s, temperature=%s, max_tokens=%s, conversation=%s, context_len=%s",
                settings.model,
                settings.temperature,
                settings.max_tokens,
                label,
                context_len,
            )
        else:
            logger.info(
                "OpenAI request: model=%s, reasoning_effort=%s, max_completion_tokens=%s, "
                "conversation=%s, context_len=%s",
                settings.model,
                settings.reasoning_effort,
                settings.max_tokens,
                label,
                context_len,
            )
        system_prompt = KITCHEN_SYSTEM_PROMPT
        if self._recipe_protocol:
            # The shared prompt itself is untouched: the protocol is an addition to its
            # formatting rules, sent only when structured recipes are switched on.
            system_prompt = KITCHEN_SYSTEM_PROMPT + RECIPE_PROTOCOL_PROMPT
        try:
            response = await self._get_client().chat.completions.create(
                model=settings.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    *to_openai_messages(messages),
                ],
                **settings.completion_params(),
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

        return self._reply_from(response, label, self._recipe_protocol)

    @staticmethod
    def _log_failure(what: str, exc: BaseException, label: str) -> None:
        logger.error(
            "OpenAI %s for conversation=%s (%s, status=%s%s)",
            what,
            label,
            type(exc).__name__,
            getattr(exc, "status_code", None),
            _error_identifiers(exc),
        )

    @staticmethod
    def _reply_from(
        response: Any, label: str, recipe_protocol: bool = False
    ) -> BackendReply:
        choices = getattr(response, "choices", None)
        if not choices:
            logger.error("OpenAI response has no choices for conversation=%s", label)
            raise BackendEmptyResponseError("OpenAI response contains no choices")

        usage = _usage_of(response)
        truncated = getattr(choices[0], "finish_reason", None) == FINISH_REASON_LENGTH
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message else None
        if not isinstance(content, str) or not content.strip():
            if truncated:
                logger.error(
                    "OpenAI answer is empty and hit the length cap for conversation=%s "
                    "(the cap is spent before the first word when the model reasons; reasoning_tokens=%s)",
                    label,
                    (usage or {}).get("reasoning_tokens"),
                )
            else:
                logger.error(
                    "OpenAI response has empty or invalid content for conversation=%s", label
                )
            raise BackendEmptyResponseError("OpenAI response content is empty or invalid")

        if truncated:
            logger.warning("OpenAI answer was cut off at the length cap for conversation=%s", label)

        if usage:
            logger.info(
                "OpenAI usage: input_tokens=%s, output_tokens=%s, total_tokens=%s%s",
                usage["input_tokens"],
                usage["output_tokens"],
                usage["total_tokens"],
                f", reasoning_tokens={usage['reasoning_tokens']}" if "reasoning_tokens" in usage else "",
            )
        if not recipe_protocol:
            return BackendReply(text=content, usage=usage, truncated=truncated)

        # The provider's own answer is parsed before anything is rendered: the blocks
        # are the only source of the structured recipes, and `text` is built from the
        # same parse, so a recipe can never be read back out of the rendered text.
        parsed = parse_reply(content)
        return BackendReply(
            text=render_parsed_reply(parsed),
            usage=usage,
            truncated=truncated,
            recipes=parsed.recipes,
        )


def _error_identifiers(exc: BaseException) -> str:
    """", code=insufficient_quota, param=max_tokens": the provider's machine-readable
    code (and parameter), when it sent plain identifiers; "" otherwise."""
    parts = []
    for name in ("code", "param"):
        value = getattr(exc, name, None)
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            parts.append(f", {name}={value}")
    return "".join(parts)


def _usage_of(response: Any) -> Optional[dict[str, int]]:
    # Metadata, not part of the success condition: a missing or malformed usage
    # object never turns a valid answer into an error.
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        counts = {
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        }
    except AttributeError:
        logger.warning("OpenAI usage metadata malformed; degrading to None")
        return None
    # Only a reasoning model reports it; counted inside output_tokens, listed apart.
    reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
    if isinstance(reasoning, int) and not isinstance(reasoning, bool):
        counts["reasoning_tokens"] = reasoning
    return counts
