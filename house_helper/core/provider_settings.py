"""How the AI provider is configured, apart from any channel.

The one place that knows the provider variables (OPENAI_API_KEY, OPENAI_MODEL,
OPENAI_REASONING_EFFORT, TEMPERATURE, MAX_TOKENS, OPENAI_TIMEOUT_SECONDS,
OPENAI_MAX_RETRIES), their defaults and their ranges. Both processes read them through
here: config.py, which adds the
Telegram bot's own BOT_TOKEN on top (and reads the .env file, as it always has), and
the web process (house_helper.web.settings), which has no bot token and must not need
one.

Importing this module reads no file and no environment variable; it does not import
the openai SDK, python-dotenv or config. Reading happens when a loader is called, from
the mapping it is given.

Errors are collected, not raised one at a time, so that a misconfigured process can
report every problem at once; the caller decides how to raise them. With
`quote_values=False` a message names the variable and the rule but never the value it
found (the web process promises that its configuration errors contain no value).

Two request shapes, chosen by one variable and never guessed from the model's name:

  * OPENAI_REASONING_EFFORT unset (the default): the request every model before GPT-6
    takes, `temperature` and `max_tokens`. Byte-for-byte what was always sent.
  * OPENAI_REASONING_EFFORT set (low, ...): a reasoning model's request,
    `reasoning_effort` and `max_completion_tokens` (which counts the reasoning tokens
    as well as the visible answer), and NO `temperature`: OpenAI's migration guide for
    GPT-6 says to remove it whenever the effort is not "none". MAX_TOKENS is the cap in
    both shapes.

The GPT-6 models reason by default (medium), so naming one without choosing an effort
would send `temperature` to a model that does not take it; that is refused at start, not
left to fail on the first message. (GPT-6 Luna supports function calling on Chat
Completions only with the effort "none"; this application uses no tools.)
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from house_helper.core.limits import DEFAULT_MAX_OUTPUT_TOKENS

API_KEY_ENV = "OPENAI_API_KEY"
MODEL_ENV = "OPENAI_MODEL"
REASONING_EFFORT_ENV = "OPENAI_REASONING_EFFORT"
TEMPERATURE_ENV = "TEMPERATURE"
MAX_TOKENS_ENV = "MAX_TOKENS"
TIMEOUT_ENV = "OPENAI_TIMEOUT_SECONDS"
MAX_RETRIES_ENV = "OPENAI_MAX_RETRIES"

DEFAULT_MODEL = "gpt-4.1-mini"

# The values of `reasoning_effort` that both the GPT-6 Luna model page and the installed
# SDK's types list. ("minimal" is not a GPT-6 value; "max" is not in the SDK's types.)
REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh")
# The model families that reason whether or not the request says so.
REASONING_BY_DEFAULT_PREFIXES = ("gpt-6",)

# MAX_TOKENS counts reasoning tokens when a reasoning effort is set, so the ceiling is
# well above the 1000 that a plain answer needs.
MAX_TOKENS_CEILING = 16384


@dataclass(frozen=True)
class ProviderSettings:
    """What is needed to call the provider. The key is kept out of the repr: a
    settings object ends up in logs and in test failure output."""

    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    temperature: float = 0.7
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    timeout_seconds: float = 15.0
    max_retries: int = 1
    reasoning_effort: Optional[str] = None  # None: not a reasoning request, see above

    def completion_params(self) -> dict[str, Any]:
        """The sampling and length parameters of one Chat Completions request (the
        model and the messages are the caller's)."""
        return completion_params(self.reasoning_effort, self.temperature, self.max_tokens)


def completion_params(
    reasoning_effort: Optional[str], temperature: float, max_tokens: int
) -> dict[str, Any]:
    """The parameters that depend on the request shape (see the module docstring):
    never `temperature` or `max_tokens` next to a reasoning effort other than "none"."""
    if reasoning_effort is None:
        return {"temperature": temperature, "max_tokens": max_tokens}
    params: dict[str, Any] = {
        "reasoning_effort": reasoning_effort,
        "max_completion_tokens": max_tokens,
    }
    if reasoning_effort == "none":
        params["temperature"] = temperature
    return params


def _parse_number(
    env: Mapping[str, str],
    name: str,
    default: str,
    cast: Callable[[str], float],
    low: float,
    high: float,
    errors: list[str],
    quote_values: bool,
):
    raw = env.get(name, default)
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        errors.append(
            f"{name} must be a number, got {raw!r}" if quote_values else f"{name} must be a number"
        )
        return None
    if not (low <= value <= high):
        errors.append(
            f"{name} must be between {low} and {high}, got {value}"
            if quote_values
            else f"{name} must be between {low} and {high}"
        )
        return None
    return value


def read_provider_settings(
    source: Mapping[str, str], errors: list[str], *, quote_values: bool = True
) -> Optional[ProviderSettings]:
    """The provider settings in `source`, or None with the problems appended to
    `errors` (only ever appended to: the caller may have put its own there first).

    A missing key is a problem here; a caller for which the provider is optional
    checks for the key itself before calling.
    """
    before = len(errors)

    api_key = source.get(API_KEY_ENV)
    if not api_key:
        errors.append(f"{API_KEY_ENV} is not set")

    model = source.get(MODEL_ENV, DEFAULT_MODEL)
    reasoning_effort = _parse_reasoning_effort(source, model, errors, quote_values)
    temperature = _parse_number(
        source, TEMPERATURE_ENV, "0.7", float, 0.0, 2.0, errors, quote_values
    )
    max_tokens = _parse_number(
        source,
        MAX_TOKENS_ENV,
        str(DEFAULT_MAX_OUTPUT_TOKENS),
        int,
        1,
        MAX_TOKENS_CEILING,
        errors,
        quote_values,
    )
    timeout_seconds = _parse_number(source, TIMEOUT_ENV, "15", float, 1.0, 120.0, errors, quote_values)
    max_retries = _parse_number(source, MAX_RETRIES_ENV, "1", int, 0, 5, errors, quote_values)

    if len(errors) > before:
        return None
    return ProviderSettings(
        api_key=api_key,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        reasoning_effort=reasoning_effort,
    )


def _parse_reasoning_effort(
    env: Mapping[str, str], model: str, errors: list[str], quote_values: bool
) -> Optional[str]:
    # An empty value counts as unset: an env file keeps a blank placeholder line.
    raw = env.get(REASONING_EFFORT_ENV) or None
    if raw is None:
        if model.startswith(REASONING_BY_DEFAULT_PREFIXES):
            errors.append(
                f"{REASONING_EFFORT_ENV} must be set for a model that reasons by default "
                f"(one of {', '.join(REASONING_EFFORTS)}; low is the usual choice)"
            )
        return None
    if raw not in REASONING_EFFORTS:
        message = f"{REASONING_EFFORT_ENV} must be one of {', '.join(REASONING_EFFORTS)}"
        errors.append(f"{message}, got {raw!r}" if quote_values else message)
        return None
    return raw
