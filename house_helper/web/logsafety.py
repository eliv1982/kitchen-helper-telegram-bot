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
