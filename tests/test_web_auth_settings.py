"""Telegram login configuration: optional as a whole, strict in its parts, and
never revealed.

The web process must start without credentials when login is not configured (R2's
boundary), refuse to start when it is configured halfway, and derive everything
that touches security (redirect URI, accepted Origin, cookie attributes) from one
configured public URL and never from a request.
"""

from datetime import timedelta

import pytest

from house_helper.persistence.database import DATABASE_URL_ENV
from house_helper.web.app import create_app
from house_helper.web.settings import (
    ID_TOKEN_ALGORITHMS,
    PUBLIC_URL_ENV,
    TELEGRAM_CLIENT_ID_ENV,
    TELEGRAM_CLIENT_SECRET_ENV,
    TELEGRAM_ID_TOKEN_ALG_ENV,
    AuthSettings,
    WebConfigError,
    WebSettings,
    load_auth_settings,
    load_web_settings,
)

SECRET = "client-secret-0f9e8d7c6b5a4938"
FULL = {
    PUBLIC_URL_ENV: "https://kitchen.example.com",
    TELEGRAM_CLIENT_ID_ENV: "1234567890",
    TELEGRAM_CLIENT_SECRET_ENV: SECRET,
}


def build(**changes) -> AuthSettings:
    values = {"origin": "https://kitchen.example.com", "client_id": "1234567890", "client_secret": SECRET}
    values.update(changes)
    return AuthSettings(**values)


# --- optional as a whole --------------------------------------------------------------


def test_with_no_login_variables_login_is_off_and_nothing_is_demanded():
    assert load_auth_settings({}) is None
    settings = load_web_settings({DATABASE_URL_ENV: "sqlite:///some.db"})
    assert settings.auth is None


def test_empty_login_variables_count_as_unset():
    assert load_auth_settings({name: "" for name in FULL}) is None
    assert load_auth_settings({**{name: "" for name in FULL}, TELEGRAM_ID_TOKEN_ALG_ENV: ""}) is None


def test_the_process_environment_is_the_default_source(monkeypatch):
    for name, value in FULL.items():
        monkeypatch.setenv(name, value)

    assert load_auth_settings().client_id == "1234567890"


def test_creating_the_application_with_login_off_needs_no_credentials(monkeypatch, tmp_path):
    for name in (*FULL, TELEGRAM_ID_TOKEN_ALG_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(DATABASE_URL_ENV, f"sqlite:///{tmp_path / 'x.db'}")

    assert create_app().state.settings.auth is None


def test_the_bot_environment_is_not_what_login_is_configured_from(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    assert load_auth_settings() is None


# --- all or nothing ------------------------------------------------------------------------


@pytest.mark.parametrize("missing", list(FULL))
def test_login_configured_halfway_refuses_to_start_and_names_what_is_missing(missing):
    env = {name: value for name, value in FULL.items() if name != missing}

    with pytest.raises(WebConfigError) as caught:
        load_auth_settings(env)

    assert missing in str(caught.value)
    assert "partly configured" in str(caught.value)  # said so, not merely a bad value
    for value in env.values():
        assert value not in str(caught.value)  # never a value, only names


def test_an_algorithm_alone_is_a_half_configuration():
    with pytest.raises(WebConfigError, match=PUBLIC_URL_ENV):
        load_auth_settings({TELEGRAM_ID_TOKEN_ALG_ENV: "ES256"})


def test_a_complete_configuration_loads():
    settings = load_auth_settings(FULL)

    assert settings == AuthSettings(
        origin="https://kitchen.example.com", client_id="1234567890", client_secret=SECRET
    )
    assert settings.id_token_algorithm == "RS256"


def test_the_web_settings_carry_the_login_settings():
    env = {DATABASE_URL_ENV: "sqlite:///x.db", **FULL}

    assert load_web_settings(env).auth == load_auth_settings(FULL)


def test_a_hand_built_web_settings_without_login_is_valid():
    assert WebSettings(database_url="sqlite:///x.db").auth is None


# --- what is derived from the public URL -----------------------------------------------------


def test_the_redirect_uri_is_the_public_url_plus_the_callback_path():
    assert build().redirect_uri == "https://kitchen.example.com/auth/telegram/callback"


@pytest.mark.parametrize(
    "given, origin",
    [
        ("https://kitchen.example.com", "https://kitchen.example.com"),
        ("https://kitchen.example.com/", "https://kitchen.example.com"),
        ("HTTPS://Kitchen.Example.COM", "https://kitchen.example.com"),
        ("https://kitchen.example.com:443", "https://kitchen.example.com"),  # the Origin a browser sends
        ("https://kitchen.example.com:8443", "https://kitchen.example.com:8443"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://localhost", "http://localhost"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("https://localhost:8443", "https://localhost:8443"),
    ],
)
def test_the_public_url_is_normalized_to_the_origin_a_browser_sends(given, origin):
    assert build(origin=given).origin == origin


@pytest.mark.parametrize(
    "given",
    [
        "http://kitchen.example.com",  # plain http outside localhost
        "http://10.0.0.5:8000",
        "http://localhost.evil.test",
        "http://127.0.0.1.evil.test",
        "ftp://kitchen.example.com",
        "kitchen.example.com",
        "//kitchen.example.com",
        "https://",
        "https:///path",
        "https://kitchen.example.com/app",  # a path: the cookie and redirect would disagree
        "https://kitchen.example.com//",
        "https://kitchen.example.com/?next=/",
        "https://kitchen.example.com?",
        "https://kitchen.example.com#",
        "https://kitchen.example.com/#x",
        "https://user@kitchen.example.com",  # credentials in a URL end up in logs
        "https://user:pass@kitchen.example.com",
        "https://kitchen.example.com\\@evil.test",
        "https://kitchen.example.com:notaport",
        "https://kitchen.example.com:99999",
        "https://kitchen.example.com ",
        " https://kitchen.example.com",
        "https://kitchen.example.com\n",
        "https://kitchen.exämple.com",  # IDN: give the punycode
        "",
    ],
)
def test_a_public_url_that_is_not_a_plain_origin_is_refused(given):
    with pytest.raises(WebConfigError, match=PUBLIC_URL_ENV):
        build(origin=given)


@pytest.mark.parametrize(
    "given",
    [
        "https://operator:PASSWORD-MARKER@kitchen.example.com",
        "http://PASSWORD-MARKER.example.com",
        "https://kitchen.example.com/PASSWORD-MARKER",
        "https://kitchen.example.com/?token=PASSWORD-MARKER",
        "https://kitchen.example.com:PASSWORD-MARKER",
    ],
)
def test_a_refused_public_url_is_never_echoed(given):
    # A URL typed into the wrong variable may carry a password; the message is the
    # same fixed text whatever was given.
    with pytest.raises(WebConfigError) as caught:
        build(origin=given)

    assert "PASSWORD-MARKER" not in str(caught.value)
    assert "PASSWORD-MARKER" not in repr(caught.value)


def test_https_gives_secure_prefixed_cookies():
    settings = build()

    assert settings.secure_cookies is True
    assert settings.session_cookie_name == "__Host-hh_session"
    assert settings.login_cookie_name == "__Host-hh_login"


def test_the_plain_http_allowed_for_localhost_gives_cookies_a_browser_will_store():
    settings = build(origin="http://localhost:8000")

    assert settings.secure_cookies is False
    assert settings.session_cookie_name == "hh_session"
    assert settings.login_cookie_name == "hh_login"
    assert settings.redirect_uri == "http://localhost:8000/auth/telegram/callback"


def test_insecure_cookies_cannot_be_configured_for_a_public_host():
    # There is no switch: the scheme decides, and http is accepted for loopback only.
    for origin in ("http://kitchen.example.com", "http://192.168.1.10"):
        with pytest.raises(WebConfigError):
            build(origin=origin)


# --- credentials ----------------------------------------------------------------------------


@pytest.mark.parametrize("client_id", ["", " 123", "123 ", "12 3", "12:3", "12\n3", "идентификатор", "1" * 257])
def test_an_unusable_client_id_is_refused_without_being_echoed(client_id):
    with pytest.raises(WebConfigError) as caught:
        build(client_id=client_id)

    assert TELEGRAM_CLIENT_ID_ENV in str(caught.value)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("secret", ["", " secret", "secret ", "sec ret", "secret\n", "секрет", "x" * 257])
def test_an_unusable_client_secret_is_refused_without_being_echoed(secret):
    with pytest.raises(WebConfigError) as caught:
        build(client_secret=secret)

    assert TELEGRAM_CLIENT_SECRET_ENV in str(caught.value)
    if secret.strip():
        assert secret.strip() not in str(caught.value)


def test_a_secret_may_contain_a_colon_a_client_id_may_not():
    assert build(client_secret="a:b").client_secret == "a:b"
    with pytest.raises(WebConfigError):
        build(client_id="a:b")


def test_the_secret_is_in_no_repr_or_str():
    settings = build()
    web = WebSettings(database_url="sqlite:///x.db", auth=settings)

    for rendered in (repr(settings), str(settings), repr(web), str(web), f"{settings!r}", f"{web}"):
        assert SECRET not in rendered
    assert "1234567890" in repr(settings)  # the client id is not a secret


def test_a_secret_never_appears_in_any_configuration_error():
    attempts = [
        {**FULL, PUBLIC_URL_ENV: "http://public.example.com"},
        {**FULL, TELEGRAM_CLIENT_ID_ENV: "bad id"},
        {**FULL, TELEGRAM_ID_TOKEN_ALG_ENV: "HS256"},
        {PUBLIC_URL_ENV: FULL[PUBLIC_URL_ENV], TELEGRAM_CLIENT_SECRET_ENV: SECRET},
    ]
    for env in attempts:
        with pytest.raises(WebConfigError) as caught:
            load_auth_settings(env)
        assert SECRET not in str(caught.value)


# --- the algorithm ----------------------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ID_TOKEN_ALGORITHMS)
def test_the_algorithms_botfather_offers_and_this_accepts(algorithm):
    assert load_auth_settings({**FULL, TELEGRAM_ID_TOKEN_ALG_ENV: algorithm}).id_token_algorithm == algorithm


@pytest.mark.parametrize(
    "algorithm", ["HS256", "HS512", "none", "None", "rs256", "RS512", "PS256", "ES256K", "ES384", "EdDSA ", "es256", "*"]
)
def test_no_other_algorithm_is_accepted(algorithm):
    with pytest.raises(WebConfigError, match=TELEGRAM_ID_TOKEN_ALG_ENV):
        load_auth_settings({**FULL, TELEGRAM_ID_TOKEN_ALG_ENV: algorithm})


def test_exactly_the_three_algorithms_are_known():
    assert ID_TOKEN_ALGORITHMS == ("RS256", "ES256", "EdDSA")


# --- policy ------------------------------------------------------------------------------------


def test_lifetimes_default_to_the_persistence_policy_and_must_be_positive():
    settings = build()

    assert settings.session_lifetime == timedelta(days=14)
    assert settings.login_lifetime == timedelta(minutes=10)
    for options in ({"session_lifetime": timedelta(0)}, {"login_lifetime": timedelta(seconds=-1)}):
        with pytest.raises(WebConfigError):
            build(**options)
