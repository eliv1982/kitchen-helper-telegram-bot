"""Configuration of the web process.

Deliberately separate from the root config.py, which belongs to the Telegram bot:
that module demands BOT_TOKEN and OPENAI_API_KEY and reads the .env file when it
is imported. The web process needs neither, so nothing here imports it, and
nothing here reads a .env file (to use one, start the server with
`uvicorn --env-file .env`).

The database is the persistence layer's own setting: HOUSE_HELPER_DATABASE_URL,
falling back to a local SQLite file when it is unset or empty.

Two ways to sign in, each optional and independent of the other. With neither
configured the process starts and serves /healthz as before, and the /auth routes
answer 503. Each is all or nothing: with any of its variables set, the required ones
must all be set, or the process refuses to start. They share one setting, the public
URL:

  HOUSE_HELPER_PUBLIC_URL             where users reach this application:
                                      https://host[:port], nothing after it (plain
                                      http only for localhost, for development).
                                      Needed by both; on its own it enables nothing.

Telegram login (OpenID Connect):

  HOUSE_HELPER_TELEGRAM_CLIENT_ID     the Client ID of the bot's Login Widget
  HOUSE_HELPER_TELEGRAM_CLIENT_SECRET its Client Secret
  HOUSE_HELPER_TELEGRAM_ID_TOKEN_ALG  optional: RS256 (default), ES256 or EdDSA; it
                                      must be what @BotFather is set to

Email login (a link mailed over SMTP; needs no Telegram variable):

  HOUSE_HELPER_SMTP_HOST              the mail server
  HOUSE_HELPER_EMAIL_FROM             the address the mail comes from
  HOUSE_HELPER_SMTP_SECURITY          optional: starttls (default, port 587) or ssl
                                      (implicit TLS, port 465). There is no plaintext
                                      setting: a mail server that cannot do TLS cannot
                                      be used
  HOUSE_HELPER_SMTP_PORT              optional: the port, if not the default of the above
  HOUSE_HELPER_SMTP_USERNAME          optional, together with the password: the login
  HOUSE_HELPER_SMTP_PASSWORD            to the mail server

The assistant (the chat's AI provider) is configured by the same variables as the
Telegram bot's, read through house_helper.core.provider_settings: OPENAI_API_KEY (the
only required one), OPENAI_MODEL, TEMPERATURE, MAX_TOKENS, OPENAI_TIMEOUT_SECONDS and
OPENAI_MAX_RETRIES. Not BOT_TOKEN: the web process has none and asks for none. Without
OPENAI_API_KEY the process starts, signs people in, creates and shows conversations,
and answers a message with 503 "chat_unavailable"; with it, every other provider
variable is validated at start (a malformed one refuses to start, naming the variable
and never its value). A key with nothing else set uses the defaults.

The public URL is configuration, never read from a request: the redirect URI the
provider is given, the address in a mailed link, the Origin accepted on logout and
the cookie attributes all derive from it, so a forged Host header changes none of
them. The cookie attributes follow its scheme: https gives Secure cookies with the
__Host- prefix; the plain http that is allowed for localhost gives the same cookies
without both, because a browser would not store them. When both ways to sign in are
configured they must agree on the URL and on how long a session lives (they are
one setting in the environment; WebSettings checks it for settings built by hand).

Problems are reported as WebConfigError, and a message never contains a URL, an
identifier or a secret (a database URL carries a password, and so does the public
URL if someone typed one into it).
"""

import ipaddress
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Mapping, Optional
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from house_helper.core.provider_settings import (
    API_KEY_ENV,
    ProviderSettings,
    read_provider_settings,
)
from house_helper.persistence.authn import OIDC_LOGIN_LIFETIME, SESSION_LIFETIME
from house_helper.persistence.database import DATABASE_URL_ENV, DEFAULT_DATABASE_URL
from house_helper.persistence.email_login import EMAIL_LOGIN_LIFETIME, EMAIL_REQUEST_COOLDOWN
from house_helper.persistence.identifiers import (
    is_mailable_address,
    is_printable_ascii,
    normalize_identity,
)

PUBLIC_URL_ENV = "HOUSE_HELPER_PUBLIC_URL"
TELEGRAM_CLIENT_ID_ENV = "HOUSE_HELPER_TELEGRAM_CLIENT_ID"
TELEGRAM_CLIENT_SECRET_ENV = "HOUSE_HELPER_TELEGRAM_CLIENT_SECRET"
TELEGRAM_ID_TOKEN_ALG_ENV = "HOUSE_HELPER_TELEGRAM_ID_TOKEN_ALG"
SMTP_HOST_ENV = "HOUSE_HELPER_SMTP_HOST"
SMTP_PORT_ENV = "HOUSE_HELPER_SMTP_PORT"
SMTP_SECURITY_ENV = "HOUSE_HELPER_SMTP_SECURITY"
SMTP_USERNAME_ENV = "HOUSE_HELPER_SMTP_USERNAME"
SMTP_PASSWORD_ENV = "HOUSE_HELPER_SMTP_PASSWORD"
EMAIL_FROM_ENV = "HOUSE_HELPER_EMAIL_FROM"

TELEGRAM_CALLBACK_PATH = "/auth/telegram/callback"

# How the connection to the mail server is protected. Both are TLS with the server's
# certificate and name checked; there is no value for "none". (The default port of
# each is the standard one for mail submission.)
SMTP_SECURITY_MODES = {"starttls": 587, "ssl": 465}
DEFAULT_SMTP_SECURITY = "starttls"

# The signing algorithms @BotFather offers that this application accepts. ES256K is
# left out: it is limited to the `openid` scope and needs secp256k1 support that
# the other three do not. Exactly one is accepted at a time (see AuthSettings).
ID_TOKEN_ALGORITHMS = ("RS256", "ES256", "EdDSA")
DEFAULT_ID_TOKEN_ALGORITHM = "RS256"

_MAX_CREDENTIAL_LENGTH = 256
_MAX_API_KEY_LENGTH = 512


class WebConfigError(Exception):
    """The web configuration is unusable. The message never includes the URL."""


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalized_origin(value: str) -> str:
    """`value` as scheme://host[:port], or WebConfigError. Lower-case host, the
    scheme's default port dropped: this is the form a browser sends in Origin."""
    invalid = WebConfigError(
        f"{PUBLIC_URL_ENV} must be an origin like https://kitchen.example.com "
        "(https, no path, no credentials; plain http only for localhost)"
    )
    if not value or any(not 32 < ord(char) < 127 for char in value):
        raise invalid
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise invalid from None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme not in ("http", "https")
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
    ):
        raise invalid
    if parts.scheme == "http" and not _is_loopback(host):
        raise invalid
    default_port = 443 if parts.scheme == "https" else 80
    shown_host = f"[{host}]" if ":" in host else host
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parts.scheme}://{shown_host}{suffix}"


def _credential_problem(name: str, value: str, *, forbid_colon: bool) -> Optional[WebConfigError]:
    # Printable ASCII only: a stray space or newline from a pasted .env line would
    # otherwise surface as an unexplained 401 from the provider. A client id is
    # also the user name of HTTP Basic authentication, which cannot hold a colon.
    if (
        not is_printable_ascii(value)
        or len(value) > _MAX_CREDENTIAL_LENGTH
        or (forbid_colon and ":" in value)
    ):
        return WebConfigError(f"{name} is not valid (printable ASCII without spaces, not empty)")
    return None


@dataclass(frozen=True)
class SessionSettings:
    """What every way of signing in shares: the origin the application is reached
    at, and how long a session lasts. Validated on construction, so a value built by
    hand (a test, a launcher) cannot skip it."""

    origin: str
    # Policy, not configuration: no environment variable (yet).
    session_lifetime: timedelta = SESSION_LIFETIME

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", _normalized_origin(self.origin))
        if self.session_lifetime <= timedelta(0):
            raise WebConfigError("session and login lifetimes must be positive")

    @property
    def secure_cookies(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def session_cookie_name(self) -> str:
        return ("__Host-" if self.secure_cookies else "") + "hh_session"

    def shared(self) -> "SessionSettings":
        """Only the part that every way of signing in shares."""
        return SessionSettings(origin=self.origin, session_lifetime=self.session_lifetime)


@dataclass(frozen=True, kw_only=True)
class AuthSettings(SessionSettings):
    """Telegram login, fully configured."""

    client_id: str
    # repr=False: a settings object ends up in logs and test failure output.
    client_secret: str = field(repr=False)
    id_token_algorithm: str = DEFAULT_ID_TOKEN_ALGORITHM
    login_lifetime: timedelta = OIDC_LOGIN_LIFETIME

    def __post_init__(self) -> None:
        super().__post_init__()
        for name, value, forbid_colon in (
            (TELEGRAM_CLIENT_ID_ENV, self.client_id, True),
            (TELEGRAM_CLIENT_SECRET_ENV, self.client_secret, False),
        ):
            problem = _credential_problem(name, value, forbid_colon=forbid_colon)
            if problem is not None:
                raise problem
        if self.id_token_algorithm not in ID_TOKEN_ALGORITHMS:
            raise WebConfigError(
                f"{TELEGRAM_ID_TOKEN_ALG_ENV} must be one of {', '.join(ID_TOKEN_ALGORITHMS)}"
            )
        if self.login_lifetime <= timedelta(0):
            raise WebConfigError("session and login lifetimes must be positive")

    @property
    def redirect_uri(self) -> str:
        """What is registered with @BotFather and sent to the provider."""
        return self.origin + TELEGRAM_CALLBACK_PATH

    @property
    def login_cookie_name(self) -> str:
        """Ties a started login to the browser that started it."""
        return ("__Host-" if self.secure_cookies else "") + "hh_login"


_HOSTNAME_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_SMTP_HOST = re.compile(rf"{_HOSTNAME_LABEL}(?:\.{_HOSTNAME_LABEL})*")
_MAX_SMTP_HOST_LENGTH = 253


@dataclass(frozen=True, kw_only=True)
class EmailSettings(SessionSettings):
    """Email login, fully configured: where the mail goes out and what it says it
    is from. The server is always reached over TLS (see SMTP_SECURITY_MODES)."""

    smtp_host: str
    smtp_security: str = DEFAULT_SMTP_SECURITY
    smtp_port: Optional[int] = None  # None: the standard port of smtp_security
    smtp_username: Optional[str] = None
    # repr=False: a settings object ends up in logs and test failure output.
    smtp_password: Optional[str] = field(default=None, repr=False)
    sender: str
    # Policy, not configuration: no environment variable (yet).
    link_lifetime: timedelta = EMAIL_LOGIN_LIFETIME
    request_cooldown: timedelta = EMAIL_REQUEST_COOLDOWN
    # Per socket operation, as smtplib counts it: a connection that stalls is dropped
    # after this long, however the server behaves.
    smtp_timeout: float = 15.0

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            not isinstance(self.smtp_host, str)
            or len(self.smtp_host) > _MAX_SMTP_HOST_LENGTH
            or _SMTP_HOST.fullmatch(self.smtp_host) is None
        ):
            raise WebConfigError(f"{SMTP_HOST_ENV} is not a valid host name")
        if self.smtp_security not in SMTP_SECURITY_MODES:
            raise WebConfigError(f"{SMTP_SECURITY_ENV} must be one of {', '.join(SMTP_SECURITY_MODES)}")
        if self.smtp_port is None:
            object.__setattr__(self, "smtp_port", SMTP_SECURITY_MODES[self.smtp_security])
        elif (
            not isinstance(self.smtp_port, int)
            or isinstance(self.smtp_port, bool)
            or not 0 < self.smtp_port < 65536
        ):
            raise WebConfigError(f"{SMTP_PORT_ENV} must be a port number")
        if (self.smtp_username is None) != (self.smtp_password is None):
            raise WebConfigError(f"{SMTP_USERNAME_ENV} and {SMTP_PASSWORD_ENV} go together")
        for name, value in (
            (SMTP_USERNAME_ENV, self.smtp_username),
            (SMTP_PASSWORD_ENV, self.smtp_password),
        ):
            if value is not None:
                problem = _credential_problem(name, value, forbid_colon=False)
                if problem is not None:
                    raise problem
        try:
            normalized = normalize_identity("email", self.sender)[1]
        except ValueError:
            normalized = None
        if normalized is None or normalized != self.sender or not is_mailable_address(normalized):
            raise WebConfigError(
                f"{EMAIL_FROM_ENV} must be a plain address with a lower-case domain like login@example.com"
            )
        if self.link_lifetime <= timedelta(0) or self.request_cooldown <= timedelta(0):
            raise WebConfigError("the link lifetime and the request cooldown must be positive")
        if self.request_cooldown > self.link_lifetime:
            raise WebConfigError("the request cooldown must not exceed the link lifetime")
        if not 0 < self.smtp_timeout <= 120:
            raise WebConfigError("the mail server timeout must be between 0 and 120 seconds")


@dataclass(frozen=True)
class WebSettings:
    # repr=False: a settings object ends up in logs and test failure output, and
    # the URL may hold a password.
    database_url: str = field(repr=False)
    auth: Optional[AuthSettings] = None
    email: Optional[EmailSettings] = None
    # The assistant. None: not configured, and the chat answers 503. (Its repr hides
    # the key.)
    provider: Optional[ProviderSettings] = None

    @property
    def session(self) -> Optional[SessionSettings]:
        """The part both ways to sign in share (None when neither is configured):
        what the session cookie and the redirects are made from."""
        for configured in (self.auth, self.email):
            if configured is not None:
                return configured.shared()
        return None

    def __post_init__(self) -> None:
        if (
            self.auth is not None
            and self.email is not None
            and self.auth.shared() != self.email.shared()
        ):
            # One cookie, one origin: two answers to "where is this application" would
            # make the cookie of one login method not the other's.
            raise WebConfigError(
                "Telegram login and email login must use the same public URL and session lifetime"
            )
        # Checked here, not only in load_web_settings, so a WebSettings built by
        # hand (a test, a future launcher) cannot skip it.
        try:
            url = make_url(self.database_url)
        except (ArgumentError, ValueError, TypeError):
            # ValueError: a port that is not a number. `from None`, and no URL in
            # the message: SQLAlchemy's own error can quote what it failed to parse.
            raise WebConfigError(f"{DATABASE_URL_ENV} is not a valid database URL") from None
        if url.get_backend_name() == "sqlite" and url.database in (None, "", ":memory:"):
            # The engine is used from worker threads (see web/database.py). A
            # private in-memory SQLite database is per connection and SQLAlchemy
            # gives each thread its own, so every thread would see a different,
            # empty database. A file is what the web process needs.
            raise WebConfigError(
                f"{DATABASE_URL_ENV} must name a SQLite file: an in-memory SQLite "
                "database is not shared between the web process's worker threads"
            )

    @property
    def database_backend(self) -> str:
        """"sqlite", "postgresql", ...: what is safe to log about the database."""
        return make_url(self.database_url).get_backend_name()


def _values(source: Mapping[str, str], names: tuple[str, ...]) -> dict[str, Optional[str]]:
    """The named variables; an empty one counts as unset."""
    return {name: source.get(name) or None for name in names}


def load_auth_settings(env: Optional[Mapping[str, str]] = None) -> Optional[AuthSettings]:
    """Telegram login settings from `env` (os.environ by default): None when none
    of its own variables (the client id and secret, the algorithm) is set -- the
    public URL alone enables nothing, it is shared with email login -- AuthSettings
    when the required ones are all set, WebConfigError for anything in between."""
    source = os.environ if env is None else env
    own = (TELEGRAM_CLIENT_ID_ENV, TELEGRAM_CLIENT_SECRET_ENV, TELEGRAM_ID_TOKEN_ALG_ENV)
    values = _values(source, (PUBLIC_URL_ENV, *own))
    if all(values[name] is None for name in own):
        return None
    required = (PUBLIC_URL_ENV, TELEGRAM_CLIENT_ID_ENV, TELEGRAM_CLIENT_SECRET_ENV)
    missing = [name for name in required if values[name] is None]
    if missing:
        raise WebConfigError(
            "Telegram login is only partly configured; also set: " + ", ".join(missing)
        )
    return AuthSettings(
        origin=values[PUBLIC_URL_ENV],
        client_id=values[TELEGRAM_CLIENT_ID_ENV],
        client_secret=values[TELEGRAM_CLIENT_SECRET_ENV],
        id_token_algorithm=values[TELEGRAM_ID_TOKEN_ALG_ENV] or DEFAULT_ID_TOKEN_ALGORITHM,
    )


def load_email_settings(env: Optional[Mapping[str, str]] = None) -> Optional[EmailSettings]:
    """Email login settings from `env` (os.environ by default): None when none of its
    own variables is set (the public URL alone enables nothing), EmailSettings when
    the required ones are all set, WebConfigError for anything in between. A value is
    never quoted in the error."""
    source = os.environ if env is None else env
    own = (
        SMTP_HOST_ENV,
        SMTP_PORT_ENV,
        SMTP_SECURITY_ENV,
        SMTP_USERNAME_ENV,
        SMTP_PASSWORD_ENV,
        EMAIL_FROM_ENV,
    )
    values = _values(source, (PUBLIC_URL_ENV, *own))
    if all(values[name] is None for name in own):
        return None
    required = (PUBLIC_URL_ENV, SMTP_HOST_ENV, EMAIL_FROM_ENV)
    missing = [name for name in required if values[name] is None]
    if missing:
        raise WebConfigError(
            "Email login is only partly configured; also set: " + ", ".join(missing)
        )
    port = None
    if values[SMTP_PORT_ENV] is not None:
        if re.fullmatch(r"[0-9]{1,5}", values[SMTP_PORT_ENV]) is None:
            raise WebConfigError(f"{SMTP_PORT_ENV} must be a port number")
        port = int(values[SMTP_PORT_ENV])
    return EmailSettings(
        origin=values[PUBLIC_URL_ENV],
        smtp_host=values[SMTP_HOST_ENV],
        smtp_security=values[SMTP_SECURITY_ENV] or DEFAULT_SMTP_SECURITY,
        smtp_port=port,
        smtp_username=values[SMTP_USERNAME_ENV],
        smtp_password=values[SMTP_PASSWORD_ENV],
        sender=values[EMAIL_FROM_ENV],
    )


def load_provider_settings(env: Optional[Mapping[str, str]] = None) -> Optional[ProviderSettings]:
    """The assistant's settings from `env` (os.environ by default): None when there
    is no OPENAI_API_KEY (the chat is then unavailable, nothing else is), the settings
    when there is, WebConfigError for a key with a malformed companion variable. The
    message names the variable and the rule, never the value found."""
    source = os.environ if env is None else env
    key = source.get(API_KEY_ENV)
    if not key:
        return None
    # A stray space or newline from a pasted .env line would otherwise surface as a
    # failure of the first chat request, not of the start.
    if not is_printable_ascii(key) or len(key) > _MAX_API_KEY_LENGTH:
        raise WebConfigError(f"{API_KEY_ENV} is not valid (printable ASCII without spaces)")
    problems: list[str] = []
    settings = read_provider_settings(source, problems, quote_values=False)
    if settings is None:
        raise WebConfigError("The assistant is not configured correctly: " + "; ".join(problems))
    return settings


def load_web_settings(env: Optional[Mapping[str, str]] = None) -> WebSettings:
    """Settings from `env` (os.environ by default). Raises WebConfigError."""
    source = os.environ if env is None else env
    return WebSettings(
        database_url=source.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL,
        auth=load_auth_settings(source),
        email=load_email_settings(source),
        provider=load_provider_settings(source),
    )
