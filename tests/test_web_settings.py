"""The web configuration: isolated from the bot's, faithful to R1's database rule,
and silent about secrets."""

import pytest

from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    DEFAULT_DATABASE_URL,
    database_url_from_environment,
)
from house_helper.web.settings import WebConfigError, WebSettings, load_web_settings

SECRET = "s3cr3t-pa55word"


# --- where the database is: the same rule as the persistence layer --------------


@pytest.mark.parametrize("environment", [{}, {DATABASE_URL_ENV: ""}], ids=["unset", "empty"])
def test_the_default_is_the_persistence_layers_local_file(environment):
    assert load_web_settings(environment).database_url == DEFAULT_DATABASE_URL


def test_the_variable_names_the_database():
    settings = load_web_settings({DATABASE_URL_ENV: "sqlite:///elsewhere.db"})

    assert settings.database_url == "sqlite:///elsewhere.db"


@pytest.mark.parametrize("value", [None, "", "sqlite:///from_env.db"], ids=["unset", "empty", "set"])
def test_the_web_and_the_persistence_layer_resolve_the_url_identically(monkeypatch, value):
    # load_web_settings repeats database_url_from_environment's one-line rule
    # (that function cannot take a mapping); this pins that they stay in step.
    if value is None:
        monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    else:
        monkeypatch.setenv(DATABASE_URL_ENV, value)

    assert load_web_settings().database_url == database_url_from_environment()


def test_the_process_environment_is_the_default_source(monkeypatch):
    monkeypatch.setenv(DATABASE_URL_ENV, "sqlite:///process.db")

    assert load_web_settings().database_url == "sqlite:///process.db"


# --- isolation from the Telegram bot's configuration ------------------------------


def test_no_telegram_or_provider_setting_is_required():
    # config.load_settings would refuse this: no BOT_TOKEN, no OPENAI_API_KEY.
    assert load_web_settings({}).database_url == DEFAULT_DATABASE_URL


def test_malformed_bot_settings_do_not_break_the_web_process():
    bot_only = {"BOT_TOKEN": "", "OPENAI_API_KEY": "", "TEMPERATURE": "hot", "MAX_TOKENS": "-5"}

    assert load_web_settings(bot_only).database_url == DEFAULT_DATABASE_URL


# --- validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["", "not a url", "://nothing", "sqlite", "postgresql://kitchen@db.internal:notaport/kitchen"],
)
def test_an_unparseable_url_is_refused(url):
    with pytest.raises(WebConfigError, match=DATABASE_URL_ENV):
        WebSettings(database_url=url)


def test_a_url_that_is_not_text_is_refused():
    with pytest.raises(WebConfigError, match=DATABASE_URL_ENV):
        WebSettings(database_url=None)  # type: ignore[arg-type]


def test_the_loader_validates_too():
    with pytest.raises(WebConfigError):
        load_web_settings({DATABASE_URL_ENV: "not a url"})


@pytest.mark.parametrize(
    "url",
    ["sqlite://", "sqlite:///:memory:", "sqlite:///", "sqlite+pysqlite://"],
)
def test_a_private_in_memory_sqlite_database_is_refused(url):
    # SQLAlchemy gives every thread its own connection to such a database, so the
    # web process's worker threads would each see a different, empty one.
    with pytest.raises(WebConfigError, match="in-memory"):
        WebSettings(database_url=url)


@pytest.mark.parametrize(
    "url, backend",
    [
        ("sqlite:///relative.db", "sqlite"),
        ("sqlite:////var/lib/house_helper/app.db", "sqlite"),
        ("sqlite+pysqlite:///relative.db", "sqlite"),
        # Accepted without a connection and without its driver installed: the
        # settings only describe the database.
        (f"postgresql://kitchen:{SECRET}@db.internal:5432/kitchen", "postgresql"),
    ],
)
def test_a_usable_url_is_accepted(url, backend):
    assert WebSettings(database_url=url).database_backend == backend


# --- no secret in anything that can be logged ------------------------------------------


def test_the_representation_of_settings_does_not_show_the_url():
    settings = WebSettings(database_url=f"postgresql://kitchen:{SECRET}@db.internal/kitchen")

    assert SECRET not in repr(settings)
    assert SECRET not in str(settings)
    assert "db.internal" not in repr(settings)


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql://kitchen:{SECRET}@db.internal:notaport/kitchen",
        f"{SECRET} is not a url",
        f"sqlite://kitchen:{SECRET}@",
    ],
)
def test_a_refusal_never_quotes_the_url(url):
    with pytest.raises(WebConfigError) as raised:
        WebSettings(database_url=url)

    error = raised.value
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    # Nor does the traceback carry SQLAlchemy's own message, which can quote it.
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


def test_the_backend_name_is_all_that_is_offered_for_logging():
    settings = WebSettings(database_url=f"postgresql://kitchen:{SECRET}@db.internal/kitchen")

    assert settings.database_backend == "postgresql"
    assert SECRET not in settings.database_backend
