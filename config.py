import os
from dataclasses import dataclass

from dotenv import load_dotenv

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


def _parse_number(env, name, default, cast, low, high, errors):
    raw = env.get(name, default)
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        errors.append(f"{name} must be a number, got {raw!r}")
        return None
    if not (low <= value <= high):
        errors.append(f"{name} must be between {low} and {high}, got {value}")
        return None
    return value


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

    openai_api_key = source.get("OPENAI_API_KEY")
    if not openai_api_key:
        errors.append("OPENAI_API_KEY is not set")

    openai_model = source.get("OPENAI_MODEL", "gpt-4.1-mini")

    temperature = _parse_number(source, "TEMPERATURE", "0.7", float, 0.0, 2.0, errors)
    max_tokens = _parse_number(source, "MAX_TOKENS", "600", int, 1, 4096, errors)
    openai_timeout_seconds = _parse_number(
        source, "OPENAI_TIMEOUT_SECONDS", "15", float, 1.0, 120.0, errors
    )
    openai_max_retries = _parse_number(
        source, "OPENAI_MAX_RETRIES", "1", int, 0, 5, errors
    )

    if errors:
        raise ConfigError("; ".join(errors))

    return Settings(
        bot_token=bot_token,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        temperature=temperature,
        max_tokens=max_tokens,
        openai_timeout_seconds=openai_timeout_seconds,
        openai_max_retries=openai_max_retries,
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
