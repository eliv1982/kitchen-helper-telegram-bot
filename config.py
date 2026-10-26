import os
from dataclasses import dataclass

from dotenv import load_dotenv

from house_helper.core.provider_settings import read_provider_settings

load_dotenv()


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    bot_token: str
    openai_api_key: str
    openai_model: str
    temperature: float
    max_tokens: int
    openai_timeout_seconds: float
    openai_max_retries: int
    # None: the request every model before GPT-6 takes (see core.provider_settings).
    openai_reasoning_effort: str | None = None


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """
    Build and validate Settings from environment variables (os.environ by
    default, or an explicit mapping for tests). Raises ConfigError describing
    every problem found if anything required is missing or out of range, so
    there is exactly one place malformed configuration can surface -- never
    an uncontrolled crash at import time.
    """
    source = env if env is not None else os.environ
    errors: list[str] = []

    bot_token = source.get("BOT_TOKEN")
    if not bot_token:
        errors.append("BOT_TOKEN is not set")

    # The provider's own variables (key, model, temperature, limits), their defaults
    # and ranges are read in one place shared with the web process, which has no bot
    # token: house_helper.core.provider_settings. It appends to `errors`, so the
    # messages and their order are what they have always been.
    provider = read_provider_settings(source, errors)

    if errors:
        raise ConfigError("; ".join(errors))

    return Settings(
        bot_token=bot_token,
        openai_api_key=provider.api_key,
        openai_model=provider.model,
        temperature=provider.temperature,
        max_tokens=provider.max_tokens,
        openai_timeout_seconds=provider.timeout_seconds,
        openai_max_retries=provider.max_retries,
        openai_reasoning_effort=provider.reasoning_effort,
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached, validated settings, loading them on first call."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def validate_config() -> list[str]:
    """Return a concise list of configuration problems (empty means OK).

    Also primes the cache used by get_settings() on success, so a valid
    startup check means the rest of the process never re-validates.
    """
    global _settings
    try:
        _settings = load_settings()
    except ConfigError as exc:
        return str(exc).split("; ")
    return []
