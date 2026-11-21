"""Driver-faithful connection failures for the PostgreSQL one-shot tests (C7-P3, F-02).

A test here must not pass by supplying evidence the driver never supplies. These builders
reproduce the *shape* psycopg 3.3.6 actually produces, measured with the pinned driver
(`psycopg[binary]==3.3.6`, SQLAlchemy 2.0.54) against PostgreSQL 16.15:

  * a refused CONNECT - the server's ErrorResponse did carry SQLSTATE 42501;
  * a refused password (28P01);
  * a database that does not exist (3D000);
  * a closed port.

All four arrived as `sqlalchemy.exc.OperationalError` whose `.orig` was a
`psycopg.OperationalError` (or `psycopg.errors.ConnectionTimeout` for the closed port) with
`sqlstate is None`, `diag.sqlstate is None`, no `pgcode`, and - for every server response - a
finished `PGconn` whose `error_message` held libpq's (localized) text. The SQLSTATE the
server sent is discarded at that boundary: psycopg raises the startup failure from
`PQconnectPoll` with only `pgconn=`, never `info=`.

`startup_refusal()` builds exactly that. `structured_denial()` builds the other case - a
denial the driver *does* tag with 42501, as psycopg does when it has a PGresult (or psycopg 2
through `pgcode`). `startup_timeout()` is the transport case with no `PGconn` at all.
"""

from __future__ import annotations


def _finished_pgconn(error_message: bytes):
    """The object `psycopg.errors.finish_pgconn` produces, built directly.

    `finish_pgconn` copies these fields off a live `PGconn` and finishes it; constructing the
    same class with the same fields needs no server, no socket and no libpq call.
    """
    from psycopg import errors as psycopg_errors
    from psycopg import pq

    factory = getattr(psycopg_errors, "FinishedPGconn", None)
    if factory is None:  # a driver that does not keep the finished transport record
        return None
    return factory(status=pq.ConnStatus.BAD, error_message=error_message)


def startup_refusal(message: str):
    """A startup refusal as psycopg 3.3.6 raises it: no SQLSTATE, a finished `PGconn`."""
    from psycopg import errors as psycopg_errors

    text = (
        'connection to server at "house-helper-db", port 5432 failed: '
        f"FATAL:  {message}\n"
    )
    return psycopg_errors.OperationalError(
        f"connection failed: {message}", pgconn=_finished_pgconn(text.encode("utf-8"))
    )


def startup_timeout():
    """The transport failure psycopg raises when no server answered: no SQLSTATE, no PGconn."""
    from psycopg import errors as psycopg_errors

    return psycopg_errors.ConnectionTimeout("connection timeout expired")


def structured_denial(state: str = "42501", message: str = "permission denied"):
    """A denial the driver *does* tag with a SQLSTATE, as it does when it has a PGresult."""
    from psycopg import errors as psycopg_errors
    from psycopg import pq

    return psycopg_errors.OperationalError(
        message, info={pq.DiagnosticField.SQLSTATE: state.encode("ascii")}
    )
