"""How the AI provider is configured, apart from any channel.

The one place that knows the provider variables (OPENAI_API_KEY, OPENAI_MODEL,
TEMPERATURE, MAX_TOKENS, OPENAI_TIMEOUT_SECONDS, OPENAI_MAX_RETRIES), their defaults
and their ranges. Both processes read them through here: config.py, which adds the
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
"""

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

from house_helper.core.limits import DEFAULT_MAX_OUTPUT_TOKENS

API_KEY_ENV = "OPENAI_API_KEY"
MODEL_ENV = "OPENAI_MODEL"
TEMPERATURE_ENV = "TEMPERATURE"
MAX_TOKENS_ENV = "MAX_TOKENS"
TIMEOUT_ENV = "OPENAI_TIMEOUT_SECONDS"
MAX_RETRIES_ENV = "OPENAI_MAX_RETRIES"

DEFAULT_MODEL = "gpt-4.1-mini"


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
    temperature = _parse_number(
        source, TEMPERATURE_ENV, "0.7", float, 0.0, 2.0, errors, quote_values
    )
    max_tokens = _parse_number(
        source, MAX_TOKENS_ENV, str(DEFAULT_MAX_OUTPUT_TOKENS), int, 1, 4096, errors, quote_values
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
    )
