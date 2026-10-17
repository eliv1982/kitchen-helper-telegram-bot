"""Configuration of the web process.

Deliberately separate from the root config.py, which belongs to the Telegram bot:
that module demands BOT_TOKEN and OPENAI_API_KEY and reads the .env file when it
is imported. The web process needs neither, so nothing here imports it, and
nothing here reads a .env file (to use one, start the server with
`uvicorn --env-file .env`).

The database is the persistence layer's own setting: HOUSE_HELPER_DATABASE_URL,
falling back to a local SQLite file when it is unset or empty.

Telegram login is optional, and all or nothing. With none of its variables set the
process starts and serves /healthz as before, and the /auth routes answer 503. With
any of them set, the three required ones must all be set, or the process refuses to
start:

  HOUSE_HELPER_PUBLIC_URL             where users reach this application:
                                      https://host[:port], nothing after it (plain
                                      http only for localhost, for development)
  HOUSE_HELPER_TELEGRAM_CLIENT_ID     the Client ID of the bot's Login Widget
  HOUSE_HELPER_TELEGRAM_CLIENT_SECRET its Client Secret
  HOUSE_HELPER_TELEGRAM_ID_TOKEN_ALG  optional: RS256 (default), ES256 or EdDSA; it
                                      must be what @BotFather is set to

The public URL is configuration, never read from a request: the redirect URI the
provider is given, the Origin accepted on logout and the cookie attributes all
derive from it, so a forged Host header changes none of them. The cookie attributes
follow its scheme: https gives Secure cookies with the __Host- prefix; the plain
http that is allowed for localhost gives the same cookies without both, because a
browser would not store them.

Problems are reported as WebConfigError, and a message never contains a URL, an
identifier or a secret (a database URL carries a password, and so does the public
URL if someone typed one into it).
"""

import ipaddress
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Mapping, Optional
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from house_helper.persistence.authn import OIDC_LOGIN_LIFETIME, SESSION_LIFETIME
from house_helper.persistence.database import DATABASE_URL_ENV, DEFAULT_DATABASE_URL
from house_helper.persistence.identifiers import is_printable_ascii

PUBLIC_URL_ENV = "HOUSE_HELPER_PUBLIC_URL"
TELEGRAM_CLIENT_ID_ENV = "HOUSE_HELPER_TELEGRAM_CLIENT_ID"
TELEGRAM_CLIENT_SECRET_ENV = "HOUSE_HELPER_TELEGRAM_CLIENT_SECRET"
TELEGRAM_ID_TOKEN_ALG_ENV = "HOUSE_HELPER_TELEGRAM_ID_TOKEN_ALG"

TELEGRAM_CALLBACK_PATH = "/auth/telegram/callback"

# The signing algorithms @BotFather offers that this application accepts. ES256K is
# left out: it is limited to the `openid` scope and needs secp256k1 support that
# the other three do not. Exactly one is accepted at a time (see AuthSettings).
ID_TOKEN_ALGORITHMS = ("RS256", "ES256", "EdDSA")
DEFAULT_ID_TOKEN_ALGORITHM = "RS256"

_MAX_CREDENTIAL_LENGTH = 256


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
class AuthSettings:
    """Telegram login, fully configured. Validated on construction, so a value
    built by hand (a test, a launcher) cannot skip it."""

    origin: str
    client_id: str
    # repr=False: a settings object ends up in logs and test failure output.
    client_secret: str = field(repr=False)
    id_token_algorithm: str = DEFAULT_ID_TOKEN_ALGORITHM
    # Policy, not configuration: no environment variable (yet).
    session_lifetime: timedelta = SESSION_LIFETIME
    login_lifetime: timedelta = OIDC_LOGIN_LIFETIME

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", _normalized_origin(self.origin))
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
        if self.session_lifetime <= timedelta(0) or self.login_lifetime <= timedelta(0):
            raise WebConfigError("session and login lifetimes must be positive")

    @property
    def redirect_uri(self) -> str:
        """What is registered with @BotFather and sent to the provider."""
        return self.origin + TELEGRAM_CALLBACK_PATH

    @property
    def secure_cookies(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def session_cookie_name(self) -> str:
        return ("__Host-" if self.secure_cookies else "") + "hh_session"

    @property
    def login_cookie_name(self) -> str:
        """Ties a started login to the browser that started it."""
        return ("__Host-" if self.secure_cookies else "") + "hh_login"


@dataclass(frozen=True)
class WebSettings:
    # repr=False: a settings object ends up in logs and test failure output, and
    # the URL may hold a password.
    database_url: str = field(repr=False)
    auth: Optional[AuthSettings] = None

    def __post_init__(self) -> None:
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


def load_auth_settings(env: Optional[Mapping[str, str]] = None) -> Optional[AuthSettings]:
    """Telegram login settings from `env` (os.environ by default): None when none
    of its variables is set (an empty one counts as unset), AuthSettings when all
    the required ones are, WebConfigError for anything in between."""
    source = os.environ if env is None else env
    values = {
        name: source.get(name) or None
        for name in (
            PUBLIC_URL_ENV,
            TELEGRAM_CLIENT_ID_ENV,
            TELEGRAM_CLIENT_SECRET_ENV,
            TELEGRAM_ID_TOKEN_ALG_ENV,
        )
    }
    if all(value is None for value in values.values()):
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


def load_web_settings(env: Optional[Mapping[str, str]] = None) -> WebSettings:
    """Settings from `env` (os.environ by default). Raises WebConfigError."""
    source = os.environ if env is None else env
    return WebSettings(
        database_url=source.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL,
        auth=load_auth_settings(source),
    )
