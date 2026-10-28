"""Keeps authentication material out of the server's access log.

The browser comes back from the provider to /auth/telegram/callback?code=...&state=...
and uvicorn's access log records the request line, query string and all. The code
and the state are single-use and the code cannot be redeemed without the PKCE
verifier, but a log is read by more people and kept longer than a login, and
"no sensitive authentication material in logs" is not a place for exceptions.

install_access_log_redaction() puts a filter on the `uvicorn.access` logger that
replaces the query string of every /auth/ request line with a marker. It cannot be
done from a route: the line is written by the server, after the response. A server
started with --no-access-log needs nothing, and nothing else here is affected. It is
idempotent, and the lifespan calls it (see app.py).
"""

import logging
import re

ACCESS_LOGGER = "uvicorn.access"
_PREFIX = "/auth/"
_MARKER = "?[redacted]"


class RedactAuthQuery(logging.Filter):
    """uvicorn logs `'%s - "%s %s HTTP/%s" %d'` with (client, method, path+query,
    http version, status); the path is the third argument."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            target = args[2]
            if target.startswith(_PREFIX) and "?" in target:
                record.args = (*args[:2], target.split("?", 1)[0] + _MARKER, *args[3:])
        return True


def install_access_log_redaction() -> None:
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(existing, RedactAuthQuery) for existing in logger.filters):
        logger.addFilter(RedactAuthQuery())


# --- the Telegram bot's token -----------------------------------------------------------
#
# The Bot API is addressed as https://api.telegram.org/bot<token>/<method>, so the token is
# in the URL of every request, and httpx writes that URL to the "httpx" logger at INFO for
# each one. The deployed logging configuration keeps that logger at WARNING, but a level is
# one setting away from a leak, and "no secret in logs" is not a place for exceptions: the
# filter replaces the token with a marker in whatever those loggers write. telegram_botapi
# never puts the URL in an exception or a log line of its own.

HTTP_LOGGERS = ("httpx", "httpcore")
_BOT_TOKEN_IN_URL = re.compile(r"/(file/)?bot[0-9]{5,16}:[A-Za-z0-9_-]{30,64}(?=[/\s\"'?]|$)")


def _hide_token(found: "re.Match[str]") -> str:
    return "/" + (found.group(1) or "") + "bot[redacted]"


def redact_bot_token(text: str) -> str:
    return _BOT_TOKEN_IN_URL.sub(_hide_token, text)


class RedactBotToken(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a record that cannot be formatted is not ours to repair
            return True
        redacted = redact_bot_token(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_bot_token_redaction() -> None:
    for name in HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(existing, RedactBotToken) for existing in logger.filters):
            logger.addFilter(RedactBotToken())
