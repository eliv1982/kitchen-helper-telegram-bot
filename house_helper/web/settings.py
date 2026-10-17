"""Configuration of the web process.

Deliberately separate from the root config.py, which belongs to the Telegram bot:
that module demands BOT_TOKEN and OPENAI_API_KEY and reads the .env file when it
is imported. The web process needs neither, so nothing here imports it, and
nothing here reads a .env file (to use one, start the server with
`uvicorn --env-file .env`).

The only setting so far is the database, and its meaning is the persistence
layer's: HOUSE_HELPER_DATABASE_URL, falling back to a local SQLite file when it is
unset or empty. Problems are reported as WebConfigError, and a message never
contains the URL (a PostgreSQL URL carries a password).
"""

import os
from dataclasses import dataclass, field
from typing import Mapping, Optional

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from house_helper.persistence.database import DATABASE_URL_ENV, DEFAULT_DATABASE_URL


class WebConfigError(Exception):
    """The web configuration is unusable. The message never includes the URL."""


@dataclass(frozen=True)
class WebSettings:
    # repr=False: a settings object ends up in logs and test failure output, and
    # the URL may hold a password.
    database_url: str = field(repr=False)

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


def load_web_settings(env: Optional[Mapping[str, str]] = None) -> WebSettings:
    """Settings from `env` (os.environ by default). Raises WebConfigError."""
    source = os.environ if env is None else env
    return WebSettings(database_url=source.get(DATABASE_URL_ENV) or DEFAULT_DATABASE_URL)
