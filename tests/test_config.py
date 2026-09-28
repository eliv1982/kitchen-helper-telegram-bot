import pytest

import config

VALID_ENV = {
    "BOT_TOKEN": "token",
    "OPENAI_API_KEY": "key",
}


def test_missing_bot_token_reports_error():
    with pytest.raises(config.ConfigError) as exc_info:
        config.load_settings(env={"OPENAI_API_KEY": "key"})

    assert "BOT_TOKEN" in str(exc_info.value)


def test_missing_openai_api_key_reports_error():
    with pytest.raises(config.ConfigError) as exc_info:
        config.load_settings(env={"BOT_TOKEN": "token"})

    assert "OPENAI_API_KEY" in str(exc_info.value)


def test_missing_both_required_settings_reports_both_errors():
    with pytest.raises(config.ConfigError) as exc_info:
        config.load_settings(env={})

    message = str(exc_info.value)
    assert "BOT_TOKEN" in message
    assert "OPENAI_API_KEY" in message


@pytest.mark.parametrize(
    "field,value",
    [
        ("TEMPERATURE", "not-a-number"),
        ("MAX_TOKENS", "many"),
        ("OPENAI_TIMEOUT_SECONDS", "soon"),
        ("OPENAI_MAX_RETRIES", "a-few"),
    ],
)
def test_malformed_numeric_setting_does_not_raise_uncontrolled_value_error(field, value):
    env = {**VALID_ENV, field: value}

    # Must raise our own ConfigError (a concise, controlled startup error),
    # never a bare ValueError bubbling up from int()/float().
    with pytest.raises(config.ConfigError) as exc_info:
        config.load_settings(env=env)

    assert field in str(exc_info.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("TEMPERATURE", "-0.1"),
        ("TEMPERATURE", "2.1"),
        ("MAX_TOKENS", "0"),
        ("MAX_TOKENS", "5000"),
        ("OPENAI_TIMEOUT_SECONDS", "0"),
        ("OPENAI_TIMEOUT_SECONDS", "121"),
        ("OPENAI_MAX_RETRIES", "-1"),
        ("OPENAI_MAX_RETRIES", "6"),
    ],
)
def test_out_of_range_numeric_setting_is_rejected(field, value):
    env = {**VALID_ENV, field: value}

    with pytest.raises(config.ConfigError) as exc_info:
        config.load_settings(env=env)

    assert field in str(exc_info.value)


def test_valid_env_produces_settings_with_expected_defaults():
    settings = config.load_settings(env=VALID_ENV)

    assert settings.bot_token == "token"
    assert settings.openai_api_key == "key"
    assert settings.openai_model == "gpt-4.1-mini"
    assert settings.temperature == 0.7
    assert settings.max_tokens == 600
    assert settings.openai_timeout_seconds == 15.0
    assert settings.openai_max_retries == 1


def test_valid_env_can_override_defaults():
    env = {
        **VALID_ENV,
        "OPENAI_MODEL": "gpt-4.1",
        "TEMPERATURE": "1.2",
        "MAX_TOKENS": "300",
        "OPENAI_TIMEOUT_SECONDS": "20",
        "OPENAI_MAX_RETRIES": "2",
    }

    settings = config.load_settings(env=env)

    assert settings.openai_model == "gpt-4.1"
    assert settings.temperature == 1.2
    assert settings.max_tokens == 300
    assert settings.openai_timeout_seconds == 20.0
    assert settings.openai_max_retries == 2


def test_validate_config_returns_empty_list_when_valid(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OPENAI_API_KEY", "key")

    assert config.validate_config() == []
    # A successful validation primes the cache used by get_settings().
    assert config.get_settings().bot_token == "token"


def test_validate_config_returns_concise_errors_when_invalid(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "key")

    errors = config.validate_config()

    assert any("BOT_TOKEN" in err for err in errors)


def test_get_settings_caches_after_first_successful_call(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OPENAI_API_KEY", "key")

    first = config.get_settings()
    # Even if the environment changes afterwards, the cached settings object
    # is reused rather than being silently re-parsed mid-process.
    monkeypatch.setenv("OPENAI_API_KEY", "changed")
    second = config.get_settings()

    assert first is second
