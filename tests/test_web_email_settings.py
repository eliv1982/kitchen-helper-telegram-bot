"""Email login configuration: optional as a whole, independent of Telegram login,
strict in its parts, TLS only, and never revealed.

Like Telegram's (tests/test_web_auth_settings.py): the web process starts without any of
it, refuses to start when it is configured halfway, and shows a secret in no message.
What is specific here: the two ways to sign in switch on and off independently of each
other and share only the public URL; and there is no setting that sends mail unencrypted.
"""

from datetime import timedelta

import pytest

from house_helper.persistence.database import DATABASE_URL_ENV
from house_helper.web.app import create_app
from house_helper.web.settings import (
    DEFAULT_SMTP_SECURITY,
    EMAIL_FROM_ENV,
    PUBLIC_URL_ENV,
    SMTP_HOST_ENV,
    SMTP_PASSWORD_ENV,
    SMTP_PORT_ENV,
    SMTP_SECURITY_ENV,
    SMTP_SECURITY_MODES,
    SMTP_USERNAME_ENV,
    TELEGRAM_CLIENT_ID_ENV,
    TELEGRAM_CLIENT_SECRET_ENV,
    AuthSettings,
    EmailSettings,
    SessionSettings,
    WebConfigError,
    WebSettings,
    load_auth_settings,
    load_email_settings,
    load_web_settings,
)

PASSWORD = "smtp-password-0f9e8d7c6b5a"
URL = "https://kitchen.example.com"
REQUIRED = {
    PUBLIC_URL_ENV: URL,
    SMTP_HOST_ENV: "smtp.example.com",
    EMAIL_FROM_ENV: "login@kitchen.example.com",
}
FULL = {**REQUIRED, SMTP_USERNAME_ENV: "mailer", SMTP_PASSWORD_ENV: PASSWORD}
TELEGRAM = {TELEGRAM_CLIENT_ID_ENV: "1234567890", TELEGRAM_CLIENT_SECRET_ENV: "client-secret-0f9e8d7c"}


def build(**changes) -> EmailSettings:
    values = {"origin": URL, "smtp_host": "smtp.example.com", "sender": "login@kitchen.example.com"}
    values.update(changes)
    return EmailSettings(**values)


# --- optional as a whole ------------------------------------------------------------------------


def test_with_no_email_variables_email_login_is_off_and_nothing_is_demanded():
    assert load_email_settings({}) is None
    assert load_web_settings({DATABASE_URL_ENV: "sqlite:///some.db"}).email is None


def test_empty_email_variables_count_as_unset():
    names = (PUBLIC_URL_ENV, SMTP_HOST_ENV, SMTP_PORT_ENV, SMTP_SECURITY_ENV, SMTP_USERNAME_ENV,
             SMTP_PASSWORD_ENV, EMAIL_FROM_ENV)

    assert load_email_settings({name: "" for name in names}) is None


def test_the_public_url_alone_switches_nothing_on():
    assert load_email_settings({PUBLIC_URL_ENV: URL}) is None
    assert load_auth_settings({PUBLIC_URL_ENV: URL}) is None
    settings = load_web_settings({DATABASE_URL_ENV: "sqlite:///x.db", PUBLIC_URL_ENV: URL})
    assert settings.auth is None and settings.email is None and settings.session is None


def test_the_process_environment_is_the_default_source(monkeypatch):
    for name, value in FULL.items():
        monkeypatch.setenv(name, value)

    assert load_email_settings().smtp_host == "smtp.example.com"


def test_creating_the_application_with_email_login_off_needs_no_mail_settings(monkeypatch, tmp_path):
    for name in (*FULL, SMTP_PORT_ENV, SMTP_SECURITY_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(DATABASE_URL_ENV, f"sqlite:///{tmp_path / 'x.db'}")

    assert create_app().state.settings.email is None


def test_the_bot_environment_is_not_what_email_login_is_configured_from(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    assert load_email_settings() is None


# --- all or nothing --------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", list(REQUIRED))
def test_email_login_configured_halfway_refuses_to_start_and_names_what_is_missing(missing):
    env = {name: value for name, value in FULL.items() if name != missing}

    with pytest.raises(WebConfigError) as caught:
        load_email_settings(env)

    assert missing in str(caught.value) and "partly configured" in str(caught.value)
    for value in env.values():
        assert value not in str(caught.value)  # names of what is missing, never a value


@pytest.mark.parametrize("alone", [SMTP_PORT_ENV, SMTP_SECURITY_ENV, SMTP_USERNAME_ENV, SMTP_PASSWORD_ENV])
def test_any_one_of_its_variables_alone_is_a_half_configuration(alone):
    with pytest.raises(WebConfigError, match="partly configured"):
        load_email_settings({alone: "587" if alone == SMTP_PORT_ENV else "starttls"})


def test_a_complete_configuration_loads_with_tls_and_the_standard_port():
    settings = load_email_settings(FULL)

    assert settings == EmailSettings(
        origin=URL, smtp_host="smtp.example.com", smtp_username="mailer", smtp_password=PASSWORD,
        sender="login@kitchen.example.com",
    )
    assert (settings.smtp_security, settings.smtp_port) == ("starttls", 587)
    assert settings.link_lifetime == timedelta(minutes=10)
    assert settings.request_cooldown == timedelta(minutes=1)
    assert settings.session_lifetime == timedelta(days=14)


def test_the_login_to_the_mail_server_is_optional_but_comes_as_a_pair():
    assert load_email_settings(REQUIRED).smtp_username is None
    assert load_email_settings(REQUIRED).smtp_password is None
    for only in (SMTP_USERNAME_ENV, SMTP_PASSWORD_ENV):
        with pytest.raises(WebConfigError) as caught:
            load_email_settings({**REQUIRED, only: "value-1234"})
        assert SMTP_USERNAME_ENV in str(caught.value) and SMTP_PASSWORD_ENV in str(caught.value)
        assert "value-1234" not in str(caught.value)


def test_the_web_settings_carry_the_email_settings():
    env = {DATABASE_URL_ENV: "sqlite:///x.db", **FULL}

    assert load_web_settings(env).email == load_email_settings(FULL)
    assert load_web_settings(env).auth is None


# --- independent of Telegram login --------------------------------------------------------------------


def test_email_login_needs_nothing_of_telegram_login():
    settings = load_web_settings({DATABASE_URL_ENV: "sqlite:///x.db", **FULL})

    assert settings.auth is None
    assert settings.email is not None
    assert settings.session == SessionSettings(origin=URL)


def test_telegram_login_needs_nothing_of_email_login():
    settings = load_web_settings({DATABASE_URL_ENV: "sqlite:///x.db", PUBLIC_URL_ENV: URL, **TELEGRAM})

    assert settings.email is None
    assert settings.auth is not None
    assert settings.session == SessionSettings(origin=URL)


def test_both_share_the_one_public_url():
    settings = load_web_settings({DATABASE_URL_ENV: "sqlite:///x.db", **FULL, **TELEGRAM})

    assert settings.auth.origin == settings.email.origin == URL
    assert settings.session == SessionSettings(origin=URL)


def test_half_a_telegram_configuration_does_not_stop_email_login_from_being_read_and_vice_versa():
    # each says what is wrong with itself, in its own words
    with pytest.raises(WebConfigError, match="Telegram login is only partly configured"):
        load_web_settings({DATABASE_URL_ENV: "sqlite:///x.db", **FULL, TELEGRAM_CLIENT_ID_ENV: "1234567890"})
    with pytest.raises(WebConfigError, match="Email login is only partly configured"):
        load_web_settings(
            {DATABASE_URL_ENV: "sqlite:///x.db", PUBLIC_URL_ENV: URL, **TELEGRAM, SMTP_HOST_ENV: "smtp.example.com"}
        )


def test_settings_built_by_hand_for_both_must_agree_on_the_url_and_the_session_lifetime():
    telegram = AuthSettings(origin=URL, client_id="1234567890", client_secret="client-secret-0f9e8d7c")

    WebSettings(database_url="sqlite:///x.db", auth=telegram, email=build())  # agree
    for other in (build(origin="https://other.example.com"), build(session_lifetime=timedelta(days=1))):
        with pytest.raises(WebConfigError, match="same public URL"):
            WebSettings(database_url="sqlite:///x.db", auth=telegram, email=other)


def test_the_telegram_only_parts_stay_with_telegram():
    assert not hasattr(build(), "login_cookie_name") and not hasattr(build(), "client_id")
    assert hasattr(SessionSettings(origin=URL), "session_cookie_name")
    assert not hasattr(SessionSettings(origin=URL), "redirect_uri")


# --- TLS only ------------------------------------------------------------------------------------------------


def test_there_is_no_way_to_ask_for_unencrypted_mail():
    assert set(SMTP_SECURITY_MODES) == {"starttls", "ssl"}
    assert DEFAULT_SMTP_SECURITY == "starttls"
    for mode in ("none", "plain", "off", "false", "", "tls", "STARTTLS", "SSL", "starttls ", "smtp"):
        with pytest.raises(WebConfigError, match=SMTP_SECURITY_ENV):
            build(smtp_security=mode)
        with pytest.raises(WebConfigError, match=SMTP_SECURITY_ENV):
            load_email_settings({**REQUIRED, SMTP_SECURITY_ENV: mode or " "})


@pytest.mark.parametrize("security, port", [("starttls", 587), ("ssl", 465)])
def test_the_port_defaults_to_the_standard_one_of_the_chosen_security(security, port):
    assert load_email_settings({**REQUIRED, SMTP_SECURITY_ENV: security}).smtp_port == port
    assert build(smtp_security=security).smtp_port == port


def test_a_port_can_be_chosen_and_must_be_one():
    assert load_email_settings({**REQUIRED, SMTP_PORT_ENV: "2525"}).smtp_port == 2525
    assert build(smtp_port=1).smtp_port == 1 and build(smtp_port=65535).smtp_port == 65535
    for bad in ("0", "65536", "-1", "abc", " 25", "25 ", "2.5", "", "٢٥", "1" * 6, "0x19"):
        with pytest.raises(WebConfigError, match=SMTP_PORT_ENV):
            load_email_settings({**REQUIRED, SMTP_PORT_ENV: bad or "x"})
    for bad_port in (0, 65536, -5, True, "587", 2.5):
        with pytest.raises(WebConfigError, match=SMTP_PORT_ENV):
            build(smtp_port=bad_port)


@pytest.mark.parametrize(
    "host",
    ["", " ", "smtp example.com", "smtp.example.com ", "smtp.example.com:587", "https://smtp.example.com",
     "smtp.example.com/", "-bad.example.com", "bad-.example.com", "a..b", ".example.com", "smtp@example.com",
     "smtp_host.example.com", "smtp.exämple.com", "x" * 254, "a" * 64 + ".com", "[::1]", "smtp.example.com\n"],
)
def test_a_mail_server_that_is_not_a_host_name_is_refused(host):
    with pytest.raises(WebConfigError, match=SMTP_HOST_ENV):
        build(smtp_host=host)


@pytest.mark.parametrize("host", ["smtp.example.com", "localhost", "127.0.0.1", "mail-1.example.co.uk", "a"])
def test_host_names_and_addresses_are_accepted(host):
    assert build(smtp_host=host).smtp_host == host


@pytest.mark.parametrize(
    "sender",
    ["", "login", "login@", "@example.com", "Login@Example.com", " login@example.com", "login@example.com ",
     "Kitchen <login@example.com>", "login@example", "a b@example.com", "a,b@example.com", "a@b.com,c@d.com",
     "login@example.com\nBcc: x@y.zz", "lögin@example.com", "<login@example.com>"],
)
def test_a_sender_that_is_not_a_plain_lower_case_address_is_refused(sender):
    with pytest.raises(WebConfigError, match=EMAIL_FROM_ENV):
        build(sender=sender)


# --- credentials ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("username", ["", " mailer", "mailer ", "mai ler", "mailer\n", "пользователь", "x" * 257])
def test_an_unusable_user_name_is_refused_without_being_echoed(username):
    with pytest.raises(WebConfigError) as caught:
        build(smtp_username=username, smtp_password=PASSWORD)

    assert SMTP_USERNAME_ENV in str(caught.value) and PASSWORD not in str(caught.value)


@pytest.mark.parametrize("password", ["", " secret", "secret ", "sec ret", "secret\n", "секрет", "x" * 257])
def test_an_unusable_password_is_refused_without_being_echoed(password):
    with pytest.raises(WebConfigError) as caught:
        build(smtp_username="mailer", smtp_password=password)

    assert SMTP_PASSWORD_ENV in str(caught.value)
    if password.strip():
        assert password.strip() not in str(caught.value)


def test_the_password_is_in_no_repr_or_str():
    settings = build(smtp_username="mailer", smtp_password=PASSWORD)
    web = WebSettings(database_url="sqlite:///x.db", email=settings)

    for rendered in (repr(settings), str(settings), repr(web), str(web), f"{settings!r}", f"{web}"):
        assert PASSWORD not in rendered
    assert "smtp.example.com" in repr(settings)  # the host is not a secret


def test_a_password_never_appears_in_any_configuration_error():
    attempts = [
        {**FULL, SMTP_HOST_ENV: "bad host"},
        {**FULL, SMTP_PORT_ENV: "70000"},
        {**FULL, SMTP_SECURITY_ENV: "plaintext"},
        {**FULL, EMAIL_FROM_ENV: "not an address"},
        {**FULL, PUBLIC_URL_ENV: "http://public.example.com"},
        {SMTP_HOST_ENV: "smtp.example.com", SMTP_PASSWORD_ENV: PASSWORD},
        {key: value for key, value in FULL.items() if key != SMTP_USERNAME_ENV},
    ]
    for env in attempts:
        with pytest.raises(WebConfigError) as caught:
            load_email_settings(env)
        assert PASSWORD not in str(caught.value) and PASSWORD not in repr(caught.value)


# --- the public URL, shared -----------------------------------------------------------------------------------


def test_the_public_url_is_checked_as_telegram_login_checks_it():
    assert build(origin="HTTPS://Kitchen.Example.COM/").origin == URL
    assert build(origin="http://localhost:8000").secure_cookies is False
    for bad in ("http://kitchen.example.com", "https://kitchen.example.com/app", "https://u:p@kitchen.example.com", ""):
        with pytest.raises(WebConfigError, match=PUBLIC_URL_ENV):
            build(origin=bad)


def test_a_refused_public_url_is_never_echoed():
    with pytest.raises(WebConfigError) as caught:
        build(origin="https://operator:PASSWORD-MARKER@kitchen.example.com")

    assert "PASSWORD-MARKER" not in str(caught.value) and "PASSWORD-MARKER" not in repr(caught.value)


def test_the_session_cookie_follows_the_scheme_whatever_signs_in():
    assert build().session_cookie_name == "__Host-hh_session" and build().secure_cookies is True
    local = build(origin="http://localhost:8000")
    assert local.session_cookie_name == "hh_session" and local.secure_cookies is False


# --- policy ---------------------------------------------------------------------------------------------------


def test_lifetimes_and_the_timeout_are_policy_and_must_make_sense():
    with pytest.raises(WebConfigError):
        build(link_lifetime=timedelta(0))
    with pytest.raises(WebConfigError):
        build(request_cooldown=timedelta(seconds=-1))
    with pytest.raises(WebConfigError, match="cooldown"):
        build(link_lifetime=timedelta(minutes=1), request_cooldown=timedelta(minutes=2))
    build(link_lifetime=timedelta(minutes=1), request_cooldown=timedelta(minutes=1))  # equal is fine
    for timeout in (0, -1, 121):
        with pytest.raises(WebConfigError, match="timeout"):
            build(smtp_timeout=timeout)
    with pytest.raises(WebConfigError):
        build(session_lifetime=timedelta(0))
