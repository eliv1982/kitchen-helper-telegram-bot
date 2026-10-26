"""How the AI provider is configured, for the bot and for the web process alike.

core.provider_settings is the one place that knows the variables, their defaults and
their ranges; config.py (the bot, which adds BOT_TOKEN and reads the .env file) and
web.settings (which has no bot token) both read through it. These tests pin that the
two agree, that the bot's messages are what they always were, that the web process
needs nothing of the bot's, and that a configuration error never quotes a value.
"""

import pytest

import config
from house_helper.core.provider_settings import ProviderSettings, read_provider_settings
from house_helper.web.settings import (
    WebConfigError,
    WebSettings,
    load_provider_settings,
    load_web_settings,
)

KEY = {"OPENAI_API_KEY": "sk-test-key"}


def read(env, **options):
    errors: list[str] = []
    return read_provider_settings(env, errors, **options), errors


# --- the reader -------------------------------------------------------------------------------------


def test_a_key_alone_gives_the_defaults():
    settings, errors = read(KEY)

    assert errors == []
    assert settings == ProviderSettings(
        api_key="sk-test-key",
        model="gpt-4.1-mini",
        temperature=0.7,
        max_tokens=1000,
        timeout_seconds=15.0,
        max_retries=1,
    )


def test_every_variable_can_be_set():
    settings, errors = read(
        {
            **KEY,
            "OPENAI_MODEL": "gpt-4.1",
            "TEMPERATURE": "1.5",
            "MAX_TOKENS": "2048",
            "OPENAI_TIMEOUT_SECONDS": "30",
            "OPENAI_MAX_RETRIES": "3",
        }
    )

    assert errors == []
    assert (settings.model, settings.temperature, settings.max_tokens) == ("gpt-4.1", 1.5, 2048)
    assert (settings.timeout_seconds, settings.max_retries) == (30.0, 3)


@pytest.mark.parametrize(
    ("name", "good", "bad"),
    [
        ("TEMPERATURE", ["0", "0.0", "2", "2.0", "1.25"], ["-0.1", "2.01", "abc", "", "nan"]),
        ("MAX_TOKENS", ["1", "600", "4096", "16384"], ["0", "16385", "-1", "1.5", "many", ""]),
        ("OPENAI_TIMEOUT_SECONDS", ["1", "15", "120"], ["0.99", "120.5", "soon"]),
        ("OPENAI_MAX_RETRIES", ["0", "1", "5"], ["-1", "6", "2.5", "few"]),
    ],
)
def test_the_ranges(name, good, bad):
    for value in good:
        settings, errors = read({**KEY, name: value})
        assert errors == [] and settings is not None, (name, value)
    for value in bad:
        settings, errors = read({**KEY, name: value})
        assert settings is None and len(errors) == 1 and errors[0].startswith(name), (name, value)


def test_all_the_problems_are_reported_together_after_what_the_caller_had_found():
    errors = ["BOT_TOKEN is not set"]

    settings = read_provider_settings(
        {"TEMPERATURE": "hot", "MAX_TOKENS": "0", "OPENAI_MAX_RETRIES": "9"}, errors
    )

    assert settings is None
    assert errors == [
        "BOT_TOKEN is not set",
        "OPENAI_API_KEY is not set",
        "TEMPERATURE must be a number, got 'hot'",
        "MAX_TOKENS must be between 1 and 16384, got 0",
        "OPENAI_MAX_RETRIES must be between 0 and 5, got 9",
    ]


def test_a_message_can_be_made_to_name_the_variable_and_never_the_value():
    secret = "SECRET-LOOKING-VALUE"
    env = {**KEY, "TEMPERATURE": secret, "MAX_TOKENS": "99999", "OPENAI_MAX_RETRIES": "77"}

    settings, errors = read(env, quote_values=False)

    assert settings is None
    assert errors == [
        "TEMPERATURE must be a number",
        "MAX_TOKENS must be between 1 and 16384",
        "OPENAI_MAX_RETRIES must be between 0 and 5",
    ]
    assert all(secret not in e and "99999" not in e and "77" not in e for e in errors)


def test_the_reader_reads_the_mapping_it_is_given_and_nothing_else(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key-from-the-process-environment")
    monkeypatch.setenv("MAX_TOKENS", "4000")

    settings, errors = read({})

    assert settings is None and errors == ["OPENAI_API_KEY is not set"]
    fine, _ = read(KEY)
    assert fine.max_tokens == 1000  # not the environment's 4000


def test_the_key_is_not_in_the_representation():
    settings, _ = read({"OPENAI_API_KEY": "sk-very-secret-key"})

    assert "sk-very-secret-key" not in repr(settings) and "sk-very-secret-key" not in str(settings)


# --- the bot reads through it, with BOT_TOKEN on top ------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"BOT_TOKEN": "t"},
        {"OPENAI_API_KEY": "k"},
        {"BOT_TOKEN": "t", "OPENAI_API_KEY": "k", "TEMPERATURE": "hot"},
        {"BOT_TOKEN": "t", "OPENAI_API_KEY": "k", "MAX_TOKENS": "0", "OPENAI_TIMEOUT_SECONDS": "x"},
        {"TEMPERATURE": "9", "MAX_TOKENS": "many", "OPENAI_TIMEOUT_SECONDS": "0", "OPENAI_MAX_RETRIES": "-1"},
    ],
)
def test_the_bots_messages_are_the_providers_after_its_own(env):
    expected = [] if env.get("BOT_TOKEN") else ["BOT_TOKEN is not set"]
    read_provider_settings(env, expected)

    with pytest.raises(config.ConfigError) as raised:
        config.load_settings(env)

    assert str(raised.value) == "; ".join(expected)


def test_the_bots_settings_are_the_providers():
    env = {"BOT_TOKEN": "t", **KEY, "OPENAI_MODEL": "m", "TEMPERATURE": "1", "MAX_TOKENS": "7",
           "OPENAI_TIMEOUT_SECONDS": "9", "OPENAI_MAX_RETRIES": "2"}

    bot = config.load_settings(env)
    provider, _ = read(env)

    assert (bot.openai_api_key, bot.openai_model, bot.temperature, bot.max_tokens) == (
        provider.api_key, provider.model, provider.temperature, provider.max_tokens
    )
    assert (bot.openai_timeout_seconds, bot.openai_max_retries) == (
        provider.timeout_seconds, provider.max_retries
    )
    assert bot.bot_token == "t"


# --- the web process: no bot token, no .env ------------------------------------------------------------------


def test_the_web_process_needs_no_bot_token():
    settings = load_provider_settings({"OPENAI_API_KEY": "sk-test-key"})

    assert settings == ProviderSettings(api_key="sk-test-key")


@pytest.mark.parametrize("env", [{}, {"OPENAI_API_KEY": ""}, {"BOT_TOKEN": "t"}])
def test_without_a_key_the_assistant_is_simply_not_configured(env):
    assert load_provider_settings(env) is None
    assert load_web_settings(env).provider is None


def test_without_a_key_the_other_provider_variables_are_not_looked_at():
    # (the bot's .env may hold them for the bot; test_web_settings pins the same)
    assert load_provider_settings({"TEMPERATURE": "hot", "MAX_TOKENS": "-5"}) is None


def test_with_a_key_a_malformed_companion_stops_the_start_naming_it_but_not_quoting_it():
    secret = "SECRET-LOOKING-VALUE"

    with pytest.raises(WebConfigError) as raised:
        load_provider_settings({**KEY, "TEMPERATURE": secret, "MAX_TOKENS": "99999"})

    message = str(raised.value)
    assert "TEMPERATURE must be a number" in message and "MAX_TOKENS must be between 1 and 16384" in message
    assert secret not in message and "99999" not in message and "sk-test-key" not in message


@pytest.mark.parametrize("key", ["with space", "line\nbreak", "кириллица", "x" * 513, "tab\t"])
def test_a_key_that_cannot_be_one_stops_the_start_without_being_quoted(key):
    with pytest.raises(WebConfigError) as raised:
        load_provider_settings({"OPENAI_API_KEY": key})

    assert "OPENAI_API_KEY" in str(raised.value) and key not in str(raised.value)


def test_the_web_settings_hold_the_assistants_and_hide_the_key():
    settings = load_web_settings({"OPENAI_API_KEY": "sk-very-secret-key", "MAX_TOKENS": "1500"})

    assert settings.provider.max_tokens == 1500
    assert "sk-very-secret-key" not in repr(settings)
    assert isinstance(settings, WebSettings)


def test_the_process_environment_is_the_default_source(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-environment")
    monkeypatch.setenv("MAX_TOKENS", "777")

    settings = load_web_settings()

    assert (settings.provider.api_key, settings.provider.max_tokens) == ("sk-from-the-environment", 777)
