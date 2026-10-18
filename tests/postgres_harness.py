"""The safety harness of the opt-in PostgreSQL tests (tests/test_admin_postgres.py).

Those tests CREATE and DROP databases on a server, so the question this module answers is
"can an operator-supplied URL send them anywhere but a throwaway server on this machine,
or make them touch a database they did not create?". The answer is built in three layers,
none of which trusts the one before it:

  1. BEFORE any connection (`validate_target`, pure, nothing is opened):
       * the URL must name the psycopg 3 driver, a loopback host (localhost, 127.0.0.1 or
         ::1: exactly those, no name is looked up), a port (not 5432: that is where a
         persistent installation runs, a throwaway server is mapped to another one), a
         user and a plain maintenance database name, and carry NO query parameters at
         all. The parameters are what makes `url.host` worthless as evidence:
         SQLAlchemy's psycopg dialect lets `?host=`, `?hostaddr=`, `?dbname=`,
         `?service=`, `?port=`, `?user=`, ... override the fields of the URL
         (tests/test_admin_postgres_harness.py shows it with the real dialect), and libpq
         lets PGHOSTADDR take a connection to another address whatever the host says
         (tests/test_admin_postgres.py shows it with the real driver);
       * the libpq environment variables that can route a connection are refused when they
         are set (ROUTING_ENV), because libpq reads them behind the URL's back;
       * HOUSE_HELPER_DATABASE_URL is never read, here or by anything in this module.
  2. The connection parameters are BUILT from the validated fields (`Target`), never
     copied from the input: host, hostaddr (both the literal IP, so there is no name
     resolution and PGHOSTADDR / a service file cannot take over: an explicit parameter
     beats both), port, user and dbname are always set, and the scratch database name is
     generated. `check_connect_params` compares what the dialect is about to pass to the
     driver with them.
  3. AFTER each connection and BEFORE its first statement of consequence
     (`verify_connection`): the driver is asked where it really connected (client-side:
     the peer IP and port libpq used, the database name) and the server which database
     this is (`current_database()`) and whose it is: every scratch database carries a
     COMMENT with a random token that only this invocation knows. A connection that fails
     any of it is closed and nothing is run on it, not a migration, not a CREATE.

Only a database this `Run` created is ever dropped, and only while its comment is still
this run's token (`Run.drop_database`). Each test gets a FRESH database cloned from a
migrated template, so no data is ever reset or truncated.

Messages are fixed text (`UnsafeTarget`): never the URL, never a password, never a value
that came from the URL or the server.

What this cannot know: that a loopback address is a server of THIS machine (a Docker port
mapping, an SSH tunnel or a WSL relay all answer on 127.0.0.1) or that it is disposable.
The blast radius is limited instead: the only statements the run sends are CREATE, COMMENT,
ALTER and DROP of databases under names it generated (and drops only those it created),
plus one read-only identity question per connection. The command line
test (`main`) opens its own engine from the pinned URL text, so that engine is not
guarded per connection; the database it uses was verified when it was created.
"""

from __future__ import annotations

import ipaddress
import os
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Optional

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url

from house_helper.persistence.database import create_db_engine

POSTGRES_URL_ENV = "HOUSE_HELPER_TEST_POSTGRES_URL"
DRIVER = "postgresql+psycopg"
CONNECT_TIMEOUT = 15  # seconds: a connection that stalls fails the test instead of hanging the run
DEFAULT_PORT = 5432  # refused: a persistent installation lives there; a throwaway server is mapped elsewhere

# The only hosts accepted, and the literal address each one is pinned to. "localhost" is
# NOT looked up: it means 127.0.0.1 (a server that listens on ::1 only must be named "::1").
LOOPBACK_ADDRESS = {"localhost": "127.0.0.1", "127.0.0.1": "127.0.0.1", "::1": "::1"}

# libpq reads these when the connection parameters do not say. They are refused when set:
# PGHOSTADDR and PGSERVICE (a service file may carry hostaddr=...) are the ones the
# explicit parameters of layer 2 do not have to be trusted against, the others are
# refused as well so that nothing ambient decides where the tests go.
ROUTING_ENV = frozenset(
    {
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGDATABASE",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSYSCONFDIR",
        "PGTARGETSESSIONATTRS",
        "PGLOADBALANCEHOSTS",
    }
)

MARKER_PREFIX = "house_helper-test-run:"
ISOLATION_LEVELS = ("read committed", "repeatable read", "serializable")

_MAINTENANCE_DATABASE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_TOKEN = re.compile(r"[0-9a-f]{32}")
_SCRATCH_NAME = re.compile(r"house_helper_test_[0-9a-f]{32}_[0-9]{1,3}")

# What the dialect may hand to the driver for a URL built by Target.url (checked by
# check_connect_params). "context" is the psycopg dialect's own (its type adapters).
_ALLOWED_PARAMS = frozenset(
    {"user", "password", "host", "hostaddr", "port", "dbname", "connect_timeout", "context"}
)
_PINNED_PARAMS = ("user", "host", "hostaddr", "port", "dbname", "connect_timeout")

_IDENTITY_SQL = (
    "SELECT current_database(), shobj_description(oid, 'pg_database') "
    "FROM pg_database WHERE datname = current_database()"
)
_COMMENT_SQL = "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s"


class UnsafeTarget(Exception):
    """The tests refuse to go on. `code` is stable (the tests assert it); `message` is fixed
    text that never contains the URL, a password or a value taken from the URL or server."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- layer 1 and 2: the target, validated before anything is opened ---------------------------------------


@dataclass(frozen=True)
class Target:
    """A server the tests may use: loopback only, every routing field fixed."""

    username: str
    address: str  # a literal IP (127.0.0.1 or ::1)
    port: int
    maintenance_database: str  # the database to connect to in order to create and drop the others
    password: Optional[str] = field(default=None, repr=False)  # None: the URL has none (or an empty one)

    def connect_params(self, database: str) -> dict[str, Any]:
        """The keyword arguments of `psycopg.connect`, every routing one explicit."""
        params: dict[str, Any] = {
            "user": self.username,
            "host": self.address,
            "hostaddr": self.address,
            "port": self.port,
            "dbname": database,
            "connect_timeout": CONNECT_TIMEOUT,
        }
        if self.password is not None:
            params["password"] = self.password
        return params

    def url(self, database: str) -> URL:
        """A SQLAlchemy URL built from validated fields only. `hostaddr` rides in the query
        so that an engine made from its TEXT (as the command line does) is pinned as well."""
        return URL.create(
            DRIVER,
            username=self.username,
            password=self.password,
            host=self.address,
            port=self.port,
            database=database,
            query={"hostaddr": self.address, "connect_timeout": str(CONNECT_TIMEOUT)},
        )


def check_environment(environ: Mapping[str, str]) -> None:
    set_routing = sorted(name for name in environ if name.upper() in ROUTING_ENV)
    if set_routing:
        raise UnsafeTarget(
            "libpq_environment",
            "libpq variables that can send a connection elsewhere are set: "
            f"{', '.join(set_routing)} (names only). Unset them to run these tests.",
        )


def validate_target(raw: Optional[str], environ: Mapping[str, str]) -> Target:
    """The `Target` for `raw` (the value of POSTGRES_URL_ENV), or UnsafeTarget. Pure: no
    connection, no name resolution, no file is opened."""
    if not (raw or "").strip():
        raise UnsafeTarget("not_configured", f"{POSTGRES_URL_ENV} is not set.")
    try:
        url = make_url(raw.strip())
    except Exception:  # (the parser's message can quote the text, password included)
        raise UnsafeTarget(
            "invalid_url", f"{POSTGRES_URL_ENV} is not a valid database URL (its value is not shown)."
        ) from None
    if url.drivername != DRIVER:
        raise UnsafeTarget(
            "wrong_driver",
            f"{POSTGRES_URL_ENV} must use the psycopg 3 driver: "
            "postgresql+psycopg://USER:PASSWORD@HOST:PORT/DATABASE",
        )
    if url.query:
        raise UnsafeTarget(
            "query_parameters",
            f"{POSTGRES_URL_ENV} must not carry query parameters: ?host=, ?hostaddr=, ?dbname=, "
            "?service= and the like override the host and database of the URL.",
        )
    address = LOOPBACK_ADDRESS.get((url.host or "").lower())
    if address is None:
        raise UnsafeTarget(
            "not_loopback",
            f"{POSTGRES_URL_ENV} must name this machine ({', '.join(LOOPBACK_ADDRESS)}): "
            "these tests create and drop databases.",
        )
    if url.port is None or not 0 < url.port < 65536:
        raise UnsafeTarget("port", f"{POSTGRES_URL_ENV} must name the server's port explicitly.")
    if url.port == DEFAULT_PORT:
        raise UnsafeTarget(
            "default_port",
            f"{POSTGRES_URL_ENV} names port {DEFAULT_PORT}, where a long-lived PostgreSQL usually runs. "
            "Map a throwaway server to another port (see the README).",
        )
    if not url.username:
        raise UnsafeTarget("username", f"{POSTGRES_URL_ENV} must name the user explicitly.")
    if not url.database or not _MAINTENANCE_DATABASE.fullmatch(url.database):
        raise UnsafeTarget(
            "database",
            f"{POSTGRES_URL_ENV} must name a maintenance database to connect to (letters, digits "
            "and underscores): the tests create their own databases and never use this one's data.",
        )
    check_environment(environ)
    return Target(
        username=url.username,
        address=address,
        port=url.port,
        maintenance_database=url.database,
        password=url.password or None,  # '' and None are the same: no password
    )


def target_from_environment(environ: Optional[Mapping[str, str]] = None) -> Target:
    """The target named by POSTGRES_URL_ENV and by nothing else (HOUSE_HELPER_DATABASE_URL
    is not read). `environ` is os.environ unless a test passes its own."""
    environ = os.environ if environ is None else environ
    return validate_target(environ.get(POSTGRES_URL_ENV), environ)


def run_from_environment(environ: Optional[Mapping[str, str]] = None) -> "Run":
    """For fixtures: the `Run` for the configured server. An unsafe configuration FAILS the
    test (an error at setup) before anything is opened, with a message free of values."""
    try:
        target = target_from_environment(environ)
    except UnsafeTarget as refusal:
        pytest.fail(f"{POSTGRES_URL_ENV} refused [{refusal.code}]: {refusal.message}", pytrace=False)
    return Run(target)


# --- layer 2 and 3: what a connection is checked against --------------------------------------------------


@dataclass(frozen=True)
class Expected:
    target: Target
    database: str
    marker: Optional[str]  # the token a scratch database must carry; None: the maintenance database


def check_connect_params(expected: Expected, cparams: Mapping[str, Any]) -> None:
    """The parameters the dialect is about to give the driver must be the validated ones."""
    if set(cparams) - _ALLOWED_PARAMS:
        raise UnsafeTarget(
            "connect_parameters",
            "the driver would be given connection parameters that were not validated (names withheld).",
        )
    wanted = expected.target.connect_params(expected.database)
    for key in _PINNED_PARAMS:
        if str(cparams.get(key)) != str(wanted[key]):
            raise UnsafeTarget(
                "connect_parameters",
                f"the connection parameter '{key}' is not the validated one.",
            )
    if (cparams.get("password") or None) != expected.target.password:
        raise UnsafeTarget("connect_parameters", "the connection parameter 'password' is not the validated one.")


def _is_the_peer(found: Any, address: str) -> bool:
    try:
        ip = ipaddress.ip_address(found)
    except ValueError:  # '' (a Unix socket), None, a host name
        return False
    return ip.is_loopback and ip == ipaddress.ip_address(address)


def verify_connection(connection: Any, expected: Expected) -> None:
    """`connection` (a psycopg-style connection that was just opened) must really be on the
    validated server and in the expected database, and a scratch database must be this run's.
    Client-side facts come first (they need no statement); the server is then asked one
    read-only question. UnsafeTarget otherwise; the caller closes the connection."""
    try:
        peer, port, database = connection.info.hostaddr, connection.info.port, connection.info.dbname
    except Exception:  # (NotSupportedError: a libpq older than 12 cannot name the peer)
        raise UnsafeTarget(
            "peer_unknown", "the driver cannot say which address the connection went to."
        ) from None
    if not _is_the_peer(peer, expected.target.address):
        raise UnsafeTarget("wrong_peer", "the connection went to an address other than the validated one.")
    if port != expected.target.port:
        raise UnsafeTarget("wrong_peer", "the connection went to a port other than the validated one.")
    if database != expected.database:
        raise UnsafeTarget("wrong_database", "the connection is to a database other than the expected one.")
    row = connection.execute(_IDENTITY_SQL).fetchone()
    connection.rollback()
    if row is None or row[0] != expected.database:
        raise UnsafeTarget("wrong_database", "the server says this is a database other than the expected one.")
    if expected.marker is not None and row[1] != expected.marker:
        raise UnsafeTarget("not_this_run", "the database does not belong to this test run.")


def guarded_connect(expected: Expected) -> Callable[..., Any]:
    """A SQLAlchemy `do_connect` hook: the parameters are checked before the driver is
    called, the connection before it is handed to the pool (so before a migration, a
    transaction or a statement of the tests can use it)."""

    def do_connect(dialect, connection_record, cargs, cparams):
        check_connect_params(expected, cparams)
        connection = dialect.connect(*cargs, **cparams)
        try:
            verify_connection(connection, expected)
        except BaseException:
            connection.close()
            raise
        return connection

    return do_connect


def guard_engine(engine: sa.Engine, expected: Expected) -> sa.Engine:
    sa.event.listen(engine, "do_connect", guarded_connect(expected))
    return engine


# --- the run: what it created, and the only things it may drop ------------------------------------------------


def _psycopg_connect(params: Mapping[str, Any]) -> Any:
    try:
        import psycopg
    except ImportError:
        raise RuntimeError(
            'the psycopg 3 driver is not installed: pip install "psycopg[binary]"'
        ) from None
    return psycopg.connect(autocommit=True, **params)


class Run:
    """One invocation of the PostgreSQL tests. Everything it creates carries its token;
    it creates through a connection verified to be on the validated server, and drops only
    what it created and still finds marked with its token. Creating a `Run` opens nothing."""

    def __init__(
        self,
        target: Target,
        *,
        connect: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        token: Optional[str] = None,
    ) -> None:
        self.target = target
        self.token = token or uuid.uuid4().hex
        if not _TOKEN.fullmatch(self.token):
            raise ValueError("a run token is 32 lower-case hex digits")
        self._connect = connect or _psycopg_connect
        self._databases: dict[str, str] = {}  # name -> "created" (not yet marked) | "marked"
        self._counter = 0

    @property
    def marker(self) -> str:
        return MARKER_PREFIX + self.token

    def _state(self, name: str, *states: str) -> str:
        state = self._databases.get(name)
        if state is None or state not in states or not _SCRATCH_NAME.fullmatch(name):
            raise UnsafeTarget("not_created_by_this_run", "that database was not created by this test run.")
        return state

    def _open(self, database: str, marker: Optional[str]) -> Any:
        connection = self._connect(self.target.connect_params(database))
        try:
            verify_connection(connection, Expected(self.target, database, marker))
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def control(self) -> Iterator[Any]:
        """An autocommit connection to the maintenance database, verified before use."""
        connection = self._open(self.target.maintenance_database, marker=None)
        try:
            yield connection
        finally:
            connection.close()

    def create_database(self, *, template: Optional[str] = None, isolation: Optional[str] = None) -> str:
        """A new database of this run's, named by the run, marked with its token; a copy of
        `template` (one this run made) when given. Returns the name."""
        if isolation is not None and isolation not in ISOLATION_LEVELS:
            raise ValueError("unknown isolation level")
        if template is not None:
            self._state(template, "marked")
        self._counter += 1
        name = f"house_helper_test_{self.token}_{self._counter}"
        if not _SCRATCH_NAME.fullmatch(name):
            raise UnsafeTarget("unsafe_name", "the generated database name is not the expected form.")
        clause = f' TEMPLATE "{template}"' if template is not None else ""
        with self.control() as control:
            control.execute(f'CREATE DATABASE "{name}"{clause}')
            self._databases[name] = "created"
            control.execute(f"COMMENT ON DATABASE \"{name}\" IS '{self.marker}'")
            self._databases[name] = "marked"
            if isolation is not None:
                control.execute(f"ALTER DATABASE \"{name}\" SET default_transaction_isolation = '{isolation}'")
        return name

    def drop_database(self, name: str) -> None:
        state = self._state(name, "created", "marked")
        with self.control() as control:
            row = control.execute(_COMMENT_SQL, (name,)).fetchone()
            if row is None:  # already gone
                del self._databases[name]
                return
            comment = row[0]
            # Ours: still carries this run's token, or was created here and never got to be
            # marked (the COMMENT statement failed; a name that exists was not free to take).
            if not (comment == self.marker or (state == "created" and comment is None)):
                raise UnsafeTarget(
                    "not_this_run", "a database that does not carry this run's token is not dropped."
                )
            control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        del self._databases[name]

    def close(self) -> None:
        """Drop everything this run still owns, whatever else failed. The first problem is
        raised once every database has been tried."""
        problems = []
        for name in reversed(list(self._databases)):
            try:
                self.drop_database(name)
            except Exception as problem:
                problems.append(problem)
        if problems:
            raise problems[0]

    def url(self, name: str) -> URL:
        self._state(name, "marked")
        return self.target.url(name)

    def engine(self, name: str) -> sa.Engine:
        """An engine for one of this run's databases whose every connection is verified
        (layer 3) before the first statement runs on it."""
        url_text = self.url(name).render_as_string(hide_password=False)
        return guard_engine(create_db_engine(url_text), Expected(self.target, name, self.marker))


@dataclass(frozen=True)
class Scratch:
    """One database of a run, for a test."""

    run: Run
    name: str

    @property
    def url(self) -> URL:
        return self.run.url(self.name)

    @property
    def url_text(self) -> str:
        return self.url.render_as_string(hide_password=False)

    def engine(self) -> sa.Engine:
        return self.run.engine(self.name)


# --- output that must not show a secret --------------------------------------------------------------------------


def leaked(url: URL, text: str) -> list[str]:
    """The NAMES of the secrets of `url` that `text` contains (never their values, so an
    assertion on the result shows nothing sensitive). No password, or an empty one, is
    nothing to look for."""
    secrets = {
        "password": url.password,
        "database": url.database,
        "url": url.render_as_string(hide_password=False),
    }
    return [name for name, value in secrets.items() if value and value in text]
