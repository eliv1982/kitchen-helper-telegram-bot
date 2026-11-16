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
     this is (`current_database()`), whose it is (`pg_database.datdba`; R2-06: a matching
     COMMENT is not ownership) and, when a restricted login was intended, which role is
     actually session_user/current_user. Every scratch database carries a COMMENT with a
     random token that only this invocation knows. A connection that fails any of it is
     closed and nothing is run on it, not a migration, not a CREATE.

Only a database this `Run` created is ever dropped, and only while both its comment and
its owner are still what this run recorded (`Run.drop_database`). Each test gets a FRESH
database cloned from a migrated template, so no data is ever reset or truncated.

R2-06 - the test-only security fence. Once `prepare_roles()` is called, the scratch
databases are created with `ALLOW_CONNECTIONS false`, PUBLIC is revoked, the migration
login is granted and verified, connections are enabled, the `public` schema is hardened
as the owner (ownership, PUBLIC revocation, explicit USAGE, default ACLs without purge),
the runtime handoffs are observed, and the maintenance database itself is restricted to
the migration and restore logins.

R3-01/R4-01 - the independent cluster identity and its one privileged boundary. No role
is created or dropped, no database is created or dropped, and no maintenance ACL is
changed unless `tests/postgres_identity.py` has positively identified the cluster: the
Compose container (or, only on the GitHub Actions runner, the runner's run-scoped service
container) must be running the digest-pinned image, publish exactly the validated loopback
host port, keep PGDATA on the verified volume, and - read through `pg_controldata` inside
that container, never through SQL - report a persistent `system_identifier`. Every
privileged connection is acquired through `PrivilegedBoundary`, which cannot exist until
that identity has been established and which verifies the connection it issues against the
identifier, through `pg_control_system()`, before handing it out. Cleanup re-inspects the
container and re-reads the identifier before every destructive step. A SQL fact (the
bootstrap superuser, the database owner, an empty inventory, the optional
`house_helper.test_cluster` setting) can make the contract stricter, but it never replaces
the independent evidence. The identifier is never learned from the connection it is used
to authorize.

R4-01 - the privileged-operation boundary. `Run.control()` is the only way a bootstrap
connection is acquired, and it delegates to `PrivilegedBoundary`: no privileged connection
is exposed before verification succeeds, and the expectation it is verified against always
carries the independently established system identifier (never an optional omission).
`apply_privileges()` refuses any engine this run did not create and verify, and then
verifies the ACTUAL connection it is about to mutate ACLs on. The restore-identity probes
of the live suites create and drop their databases through `create_database_as()` /
`drop_database()`, so no test can create an untracked disposable database with raw SQL.

R3-02 - cleanup tracking. Every CONNECT grant this run hands to a maintenance login is
recorded with the role and the database; cleanup revokes exactly those, before dropping a
role, and keeps any record whose revocation or drop failed so a later `close()` (or the
operator) can see and retry it. Ownership and identity are verified again for each
destructive cleanup action, and cleanup errors are surfaced without discarding the setup
failure that preceded them.

R5 - the remaining bypasses. (1) A privileged connection cannot even be described without
the independently established cluster identifier: `Expected` refuses one, `guarded_connect`
refuses before the driver is called, and every verification asks `pg_control_system()`.
(2) Cleanup re-establishes the container/storage/control-file identity immediately before
EVERY revocation and role drop - not once at `close()` entry - so evidence that disappears
mid-cleanup stops the remaining mutations and keeps their records. (3) An attempted CREATE
is never tracked as an owned database: only a confirmed success is (R5-05), a duplicate
failure is never adopted, and an unresolved attempt is preserved and reported. (4) The
database inventory requires the actual marker and owner, not a scratch-looking name.

R7 - a HELD scratch database, and the one reason for it. A test can retain a database it
created (`hold_database`) when the process that used it could not be confirmed exited: that
process may still write its files or hold its connection, so nothing it used may be cleaned
up. A held database stays in the ordinary tracked inventory - its ownership record is not
removed - but `drop_database()` refuses it before any Docker inspection or connection, and
`close()` refuses ALL cleanup rather than partly destroying the run and leaving a
half-cleaned ownership state. Roles, grants and the other databases of the run are kept for
recovery; `pending_cleanup()` reports them with the database, the reason and the worker's
PID when it is known.

A hold is never released automatically: nothing in teardown, `drop_database()`,
`close()` or a failed setup calls `release_hold()`. That one explicit operation is the only
way a hold ends, it applies only to a hold this run recorded, and the evidence is bound to
this run's own lifecycle state, not to a caller's assertion. A hold records, BEFORE `Popen`
is called, that a spawn attempt is being made (`note_spawn_attempt`); the attempt is what
makes `spawn_never_attempted` refusable later even when `Popen` raised without returning a
handle. When `Popen` returns, the actual process handle is associated before any fallible
post-spawn step (`note_held_process`), and a confirmed-exit release asks that handle itself
(`poll()`, cross-checked against its `returncode`) rather than trusting a supplied number.
A nonzero status is a valid exit; a missing handle, a failed `poll()`, a running process or
contradictory evidence is not evidence of anything and releases nothing. An absent handle,
a failed wrapper or a generic exception is not evidence either. A worker PID that becomes
known after the hold was recorded is associated with `note_held_worker()`, which updates the
record in place: the protection is never removed, not even for an instant.

Messages are fixed text (`UnsafeTarget`): never the URL, never a password, never a value
that came from the URL or the server.

What this cannot know: that a loopback address is a server of THIS machine (a Docker port
mapping, an SSH tunnel or a WSL relay all answer on 127.0.0.1) or that it is disposable.
The blast radius is limited instead: the only statements the run sends are CREATE, COMMENT,
ALTER and DROP of databases under names it generated (and drops only those it created),
plus the read-only identity questions of every guarded connection. Every engine a `Run`
hands out is guarded per connection and registered with the run (R4-01); the preflight's
single bootstrap connection is verified the same way before it asks anything, and
`postgres_preflight` reuses `Run` for its `--alembic-check` step.
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL, make_url

from house_helper.persistence.database import create_db_engine
from tests import postgres_identity as identity

POSTGRES_URL_ENV = "HOUSE_HELPER_TEST_POSTGRES_URL"
DRIVER = "postgresql+psycopg"
CONNECT_TIMEOUT = 15  # seconds: a connection that stalls fails the test instead of hanging the run
DEFAULT_PORT = 5432  # refused: a persistent installation lives there; a throwaway server is mapped elsewhere
# HH-DB-01 CI-06: the test server IS PostgreSQL 16, not a nearby major version.
SERVER_VERSION_NUM_MIN = 160000
SERVER_VERSION_NUM_MAX = 169999

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
# HH-DB-01 R7: the ONE reason a scratch database may be retained instead of dropped. The
# process that used it could not be confirmed exited, so it may still be writing its files or
# holding its connection; nothing it used may be cleaned up. There is no other reason and no
# automatic release: a database left this way is a resource for the operator to recover.
HELD_UNCONFIRMED_WORKER_EXIT = "unconfirmed_worker_exit"
HELD_REASONS = frozenset({HELD_UNCONFIRMED_WORKER_EXIT})
# HH-DB-01 R7/R7-01: the only conditions under which a hold may be released. Each is a
# POSITIVE establishment by this run's own lifecycle state, never a caller-supplied number:
# the associated process handle itself reports its exit (and the status is cross-checked
# against `returncode`), or this run recorded no spawn attempt at all. Nothing in teardown
# releases a hold.
RELEASED_WORKER_EXIT_CONFIRMED = "worker_exit_confirmed"
RELEASED_SPAWN_NEVER_ATTEMPTED = "spawn_never_attempted"
RELEASE_REASONS = frozenset({RELEASED_WORKER_EXIT_CONFIRMED, RELEASED_SPAWN_NEVER_ATTEMPTED})

_MAINTENANCE_DATABASE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_TOKEN = re.compile(r"[0-9a-f]{32}")
# The counter is not capped at three digits: a full PostgreSQL run creates one scratch
# database per test (more than a thousand of them), and a four-digit suffix must remain a
# name this run generated rather than an "unsafe name" that stops the run.
_SCRATCH_NAME = re.compile(r"house_helper_test_[0-9a-f]{32}_[0-9]{1,6}")
# R5-04 item 8: the COMMENT a scratch database of this harness carries, and the owner a
# per-run prepared identity gives it. A name alone is never disposal evidence.
_SCRATCH_MARKER = re.compile(re.escape(MARKER_PREFIX) + r"[0-9a-f]{32}")
_SCRATCH_OWNER = re.compile(r"hh_t_[0-9a-f]{32}_owner")

# What the dialect may hand to the driver for a URL built by Target.url (checked by
# check_connect_params). "context" is the psycopg dialect's own (its type adapters).
_ALLOWED_PARAMS = frozenset(
    {"user", "password", "host", "hostaddr", "port", "dbname", "connect_timeout", "context"}
)
_PINNED_PARAMS = ("user", "host", "hostaddr", "port", "dbname", "connect_timeout")

# HH-DB-01 R2-06: the identity of a connection is the database, this run's token and its
# OWNER (`pg_database.datdba`). A matching COMMENT or a name/port alone is not ownership:
# the owner is the independent fact the fence, every connection and the cleanup guard check.
_IDENTITY_SQL = (
    "SELECT current_database(), shobj_description(oid, 'pg_database'), "
    "pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
)
_COMMENT_AND_OWNER_SQL = (
    "SELECT shobj_description(oid, 'pg_database'), pg_get_userbyid(datdba) "
    "FROM pg_database WHERE datname = %s"
)
_ROLE_SQL = "SELECT session_user, current_user"
# The marker a dedicated disposable cluster may declare for itself. It strengthens, but
# never replaces, the owner/inventory evidence below: a matching COMMENT is insufficient.
DEDICATED_SETTING = "house_helper.test_cluster"
DEDICATED_VALUE = "disposable"
MAINTENANCE_CONNECT_ROLES = ("migrate", "restore")

# --- HH-DB-01 CI-16 / CI-17: per-run restricted identities ---------------------------------
#
# The frozen role matrix (§3.2) is the contract; the names are per run because roles are
# cluster-wide (CI-17). One bootstrap superuser connection creates the roles and the
# databases; every other connection is a restricted login, verified on opening.
ROLE_KEYS = ("owner", "migrate", "app", "purge", "backup", "provision", "restore")
ROLE_NAME = re.compile(r"hh_t_[0-9a-f]{32}_(?:owner|migrate|app|purge|backup|provision|restore)")
# The ten retention tables of ROL-11 (the purge command's whole grant).
PURGE_TABLES = (
    "sessions",
    "oidc_states",
    "email_login_challenges",
    "identity_invitations",
    "telegram_link_codes",
    "telegram_update_admissions",
    "message_attachments",
    "messages",
    "conversations",
    "turn_requests",
)
PURGE_COLUMN_UPDATES = {
    "sessions": ("expires_at",),
    "oidc_states": ("expires_at",),
    "email_login_challenges": ("expires_at",),
    "identity_invitations": ("expires_at",),
    "telegram_link_codes": ("expires_at",),
    "telegram_update_admissions": ("expires_at",),
    "message_attachments": ("expires_at",),
    "messages": ("expires_at",),
    "conversations": ("updated_at",),
    "turn_requests": ("status", "finished_at", "updated_at"),
}
PROVISION_TABLES = ("users", "allowlist_entries", "identity_invitations")


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

    def connect_params(self, database: str, identity: Optional["RoleIdentity"] = None) -> dict[str, Any]:
        """The keyword arguments of `psycopg.connect`, every routing one explicit."""
        params: dict[str, Any] = {
            "user": self.username if identity is None else identity.name,
            "host": self.address,
            "hostaddr": self.address,
            "port": self.port,
            "dbname": database,
            "connect_timeout": CONNECT_TIMEOUT,
        }
        password = self.password if identity is None else identity.password
        if password is not None:
            params["password"] = password
        return params

    def url(self, database: str, identity: Optional["RoleIdentity"] = None) -> URL:
        """A SQLAlchemy URL built from validated fields only. `hostaddr` rides in the query
        so that an engine made from its TEXT (as the command line does) is pinned as well."""
        return URL.create(
            DRIVER,
            username=self.username if identity is None else identity.name,
            password=self.password if identity is None else identity.password,
            host=self.address,
            port=self.port,
            database=database,
            query={"hostaddr": self.address, "connect_timeout": str(CONNECT_TIMEOUT)},
        )


@dataclass(frozen=True)
class RoleIdentity:
    """One per-run restricted login (CI-16/CI-17). The password is generated for this run
    and never printed: its repr hides it, and nothing in this module puts it in a message."""

    key: str
    name: str
    password: Optional[str] = field(repr=False)
    session_user: str
    current_user: str


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


def verified_cluster_identity(
    target: Target,
    *,
    environ: Optional[Mapping[str, str]] = None,
    docker: Optional[Any] = None,
) -> identity.ClusterIdentity:
    """R3-01: the independently established identity of the disposable test cluster, as
    an `UnsafeTarget` refusal when it cannot be established. Convenience for the
    preflight; `Run` uses the same contract."""
    environ = os.environ if environ is None else environ
    try:
        return identity.establish(target, environ=environ, docker=docker)
    except identity.IdentityError as refusal:
        raise UnsafeTarget(refusal.code, refusal.message) from None


def preflight(
    target: Target,
    *,
    environ: Optional[Mapping[str, str]] = None,
    docker: Optional[Any] = None,
) -> int:
    """CI-06: the cluster is independently identified (R3-01), then one verified
    connection confirms the driver is installed, the server answers, it is PostgreSQL 16,
    the configured role can connect and the connection reaches the identified cluster.
    Returns `server_version_num`. Every refusal is an `UnsafeTarget` with fixed text (no
    URL, no password)."""
    try:
        import psycopg  # noqa: F401  (the driver itself does the connecting)
    except ImportError:
        raise UnsafeTarget(
            "driver", 'the psycopg 3 driver is not installed: pip install "psycopg[binary]"'
        ) from None
    cluster = verified_cluster_identity(target, environ=environ, docker=docker)
    # R4-01: even the preflight's single privileged connection is acquired through the one
    # boundary, so it is verified (peer, port, database, owner and identifier) before the
    # first statement, and a connection that reaches another cluster is closed untouched.
    boundary = PrivilegedBoundary(target=target, cluster=cluster, connect=_psycopg_connect)
    with boundary.connection(
        target.maintenance_database, marker=None, owner=target.username
    ) as connection:
        row = connection.execute("SHOW server_version_num").fetchone()
        connection.rollback()
        try:
            number = int(row[0])
        except (TypeError, ValueError, IndexError):
            raise UnsafeTarget(
                "server_version", "the server did not name its version as a number."
            ) from None
        if not SERVER_VERSION_NUM_MIN <= number <= SERVER_VERSION_NUM_MAX:
            raise UnsafeTarget(
                "server_version",
                f"the server is not PostgreSQL 16 (server_version_num {number}; "
                f"expected {SERVER_VERSION_NUM_MIN}-{SERVER_VERSION_NUM_MAX}).",
            )
        connection.execute("SELECT 1").fetchone()
        connection.rollback()
        return number


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
    # R5-02: NOT optional. Every connection expectation carries the independently
    # established cluster identifier, and every verification compares it against
    # `pg_control_system()`. There is no "omit it and the check is skipped" shape left.
    cluster_identifier: str
    identity: Optional[RoleIdentity] = None  # None: the bootstrap connection of the URL
    database_owner: Optional[str] = None  # None: the bootstrap user of the URL

    def __post_init__(self) -> None:
        if not isinstance(self.cluster_identifier, str) or not self.cluster_identifier.isdigit():
            raise UnsafeTarget(
                "cluster_identity_missing",
                "a connection expectation without the independently verified cluster "
                "identifier is refused; no privileged connection is opened on a guess.",
            )


def check_connect_params(expected: Expected, cparams: Mapping[str, Any]) -> None:
    """The parameters the dialect is about to give the driver must be the validated ones."""
    if set(cparams) - _ALLOWED_PARAMS:
        raise UnsafeTarget(
            "connect_parameters",
            "the driver would be given connection parameters that were not validated (names withheld).",
        )
    wanted = expected.target.connect_params(expected.database, expected.identity)
    for key in _PINNED_PARAMS:
        if str(cparams.get(key)) != str(wanted[key]):
            raise UnsafeTarget(
                "connect_parameters",
                f"the connection parameter '{key}' is not the validated one.",
            )
    if (cparams.get("password") or None) != wanted.get("password"):
        raise UnsafeTarget("connect_parameters", "the connection parameter 'password' is not the validated one.")


def _is_the_peer(found: Any, address: str) -> bool:
    try:
        ip = ipaddress.ip_address(found)
    except ValueError:  # '' (a Unix socket), None, a host name
        return False
    return ip.is_loopback and ip == ipaddress.ip_address(address)


def _verify(info: tuple[Any, Any, Any], fetch: Callable[[str], Any], expected: Expected) -> None:
    """The checks of one connection, against the facts the caller supplies: the driver's
    client-side `info` (peer address, port, database) and a `fetch(sql)` that returns one
    row through the connection actually being used. No connection is opened here."""
    peer, port, database = info
    if not _is_the_peer(peer, expected.target.address):
        raise UnsafeTarget("wrong_peer", "the connection went to an address other than the validated one.")
    if port != expected.target.port:
        raise UnsafeTarget("wrong_peer", "the connection went to a port other than the validated one.")
    if database != expected.database:
        raise UnsafeTarget("wrong_database", "the connection is to a database other than the expected one.")
    row = fetch(_IDENTITY_SQL)
    if row is None or row[0] != expected.database:
        raise UnsafeTarget("wrong_database", "the server says this is a database other than the expected one.")
    if expected.marker is not None and row[1] != expected.marker:
        raise UnsafeTarget("not_this_run", "the database does not belong to this test run.")
    # R2-06: `pg_database.datdba`, not the COMMENT and not the name, says whose it is.
    owner = expected.database_owner or expected.target.username
    if len(row) < 3 or row[2] != owner:
        raise UnsafeTarget(
            "wrong_owner", "the database is not owned by the identity it was created for."
        )
    if expected.identity is not None:
        roles = fetch(_ROLE_SQL)
        if roles is None or tuple(roles) != (expected.identity.session_user, expected.identity.current_user):
            raise UnsafeTarget(
                "wrong_identity", "the connection is not the restricted identity it was made for."
            )
    # R3-01/R5-02: the same persistent identifier the container's control file named. It is
    # never optional: every expectation carries it (the `Expected` constructor refuses one
    # that does not), and it is read before the first statement of consequence, never
    # learned from this connection.
    if not isinstance(expected.cluster_identifier, str) or not expected.cluster_identifier.isdigit():
        raise UnsafeTarget(
            "cluster_identity_missing",
            "a connection without an independently verified cluster identifier is refused; "
            "nothing is run on it.",
        )
    row = fetch(identity.SYSTEM_IDENTIFIER_SQL)
    if row is None or str(row[0]) != expected.cluster_identifier:
        raise UnsafeTarget(
            "cluster_identity_changed",
            "the connection reports a database system identifier other than the independently "
            "verified test cluster's; nothing is run on it.",
        )


def verify_connection(connection: Any, expected: Expected) -> None:
    """`connection` (a psycopg-style connection that was just opened) must really be on the
    validated server and in the expected database, and a scratch database must be this run's.
    Client-side facts come first (they need no statement); the server is then asked one
    read-only question. UnsafeTarget otherwise; the caller closes the connection."""
    try:
        info = (connection.info.hostaddr, connection.info.port, connection.info.dbname)
    except Exception:  # (NotSupportedError: a libpq older than 12 cannot name the peer)
        raise UnsafeTarget(
            "peer_unknown", "the driver cannot say which address the connection went to."
        ) from None

    def fetch(sql: str) -> Any:
        row = connection.execute(sql).fetchone()
        connection.rollback()
        return row

    _verify(info, fetch, expected)


def verify_engine_connection(connection: sa.Connection, expected: Expected) -> None:
    """R4-01: verify the ACTUAL connection a SQLAlchemy engine is about to use - not the
    Run object a caller handed the engine from. The driver's own facts are read from the
    DBAPI connection and the server's from the engine's connection, inside the caller's
    transaction (no rollback here)."""
    try:
        raw = connection.connection.dbapi_connection
        info = (raw.info.hostaddr, raw.info.port, raw.info.dbname)
    except Exception:
        raise UnsafeTarget(
            "peer_unknown", "the engine's driver cannot say which address the connection went to."
        ) from None

    def fetch(sql: str) -> Any:
        return connection.exec_driver_sql(sql).fetchone()

    _verify(info, fetch, expected)


class PrivilegedBoundary:
    """HH-DB-01 R4-01: the one boundary through which a privileged (bootstrap) connection
    is acquired.

    It cannot be constructed without a verified `ClusterIdentity`, and every connection it
    issues is verified - client-side peer/port/database, the server's `current_database()`
    and owner, and the control file's `system_identifier` - BEFORE it is handed to the
    caller. There is no other way in this module to obtain a bootstrap connection, so a
    caller cannot acquire one "to use later" ahead of verification, and a connection that
    really reaches another cluster is closed without a statement of consequence."""

    def __init__(self, *, target: "Target", cluster: identity.ClusterIdentity, connect: Callable[..., Any]) -> None:
        # R4-01: only an identity this process actually established (Docker facts + control
        # file) is accepted; a hand-built one is not a way in.
        if not identity.issued(cluster) or not cluster.system_identifier:
            raise UnsafeTarget(
                "cluster_identity_missing",
                "a privileged connection needs an independently verified cluster identity.",
            )
        self._target = target
        self._cluster = cluster
        self._connect = connect

    def expectation(self, database: str, *, marker: Optional[str], owner: str) -> Expected:
        # R5-01 item 3: the identity's facts are re-checked here, not only when the
        # boundary was built. A fact changed (or a proof copied onto altered facts) after
        # construction therefore cannot be used to authorize this connection.
        if not identity.issued(self._cluster):
            raise UnsafeTarget(
                "cluster_identity_missing",
                "the cluster identity's facts are no longer the independently established "
                "ones; no privileged connection is verified on it.",
            )
        return Expected(
            self._target,
            database,
            marker,
            self._cluster.system_identifier,
            identity=None,
            database_owner=owner,
        )

    @contextmanager
    def connection(self, database: str, *, marker: Optional[str], owner: str) -> Iterator[Any]:
        expected = self.expectation(database, marker=marker, owner=owner)
        connection = self._connect(self._target.connect_params(database))
        try:
            verify_connection(connection, expected)
            yield connection
        finally:
            connection.close()


def _require_expected_identity(expected: Expected) -> None:
    """R5-02: the common gate every privileged connection passes before the driver is
    called. An expectation with no verified cluster identifier cannot reach a connection -
    not even a read-only one - so there is no path that opens first and checks later."""
    if not isinstance(expected.cluster_identifier, str) or not expected.cluster_identifier.isdigit():
        raise UnsafeTarget(
            "cluster_identity_missing",
            "a connection expectation without the independently verified cluster "
            "identifier is refused; no connection is opened on a guess.",
        )


def guarded_connect(expected: Expected) -> Callable[..., Any]:
    """A SQLAlchemy `do_connect` hook: the identity gate and the parameters are checked
    before the driver is called, the connection before it is handed to the pool (so before
    a migration, a transaction or a statement of the tests can use it)."""

    def do_connect(dialect, connection_record, cargs, cparams):
        _require_expected_identity(expected)
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


def _is_duplicate_database(error: BaseException) -> bool:
    """R5-05: PostgreSQL's `duplicate_database` (SQLSTATE 42P04) is the one CREATE failure
    that definitively proves nothing was created: the name was already taken. Every other
    failure is ambiguous and must not be treated as proof of anything."""
    return (
        getattr(error, "sqlstate", None) == "42P04"
        or getattr(error, "pgcode", None) == "42P04"
    )


def _psycopg_connect(params: Mapping[str, Any]) -> Any:
    try:
        import psycopg
    except ImportError:
        raise RuntimeError(
            'the psycopg 3 driver is not installed: pip install "psycopg[binary]"'
        ) from None
    return psycopg.connect(autocommit=True, **params)


@dataclass(frozen=True)
class HeldDatabase:
    """HH-DB-01 R7: a scratch database retained because the process that used it could not
    be confirmed exited.

    The record is what a recovery needs and nothing more: WHICH database, WHY it was kept,
    the worker's process id when the lifecycle still knew it, whether a spawn attempt was
    recorded before `Popen`, the identity of the run that owns it, and the resources
    (`retained`) that `close()` would otherwise have destroyed. It carries no connection, no
    credential, no live process object and no instruction: a future cleanup must go through
    the same ownership-, token- and identity-verified path as every other drop."""

    name: str
    reason: str
    worker_pid: Optional[int]
    test_run: str  # the run token: the inventory record that still owns the database
    retained: Mapping[str, Any]  # what this run still tracks (databases, roles, grants)
    # R7-01: recorded in the run state BEFORE `Popen` is called. It is part of the reported
    # record because it is what makes `spawn_never_attempted` refusable even on a path where
    # no process handle ever came back.
    spawn_attempted: bool = False


@dataclass
class _HeldWorker:
    """R7-01: the transient, in-memory half of a hold.

    It lives only inside the `Run` that created the hold and is never part of
    `HeldDatabase`: the actual process object is what proves termination, and a live
    process is neither serializable nor meaningful outside the process that owns it.
    `spawn_attempted` is set before `Popen`; `process` is the handle `Popen` returned,
    associated before any fallible post-spawn step. An absent handle is not evidence that
    no process exists."""

    spawn_attempted: bool = False
    process: Optional[Any] = None


class Run:
    """One invocation of the PostgreSQL tests. Everything it creates carries its token;
    it creates through a connection verified to be on the validated server, and drops only
    what it created and still finds marked with its token. Creating a `Run` opens nothing.

    HH-DB-01 CI-17: `prepare_roles()` creates the per-run restricted identities of the frozen
    matrix (§3.2) - a NOLOGIN owner, a migration login that assumes it, and separate
    application, purge, backup, provisioning and restore logins - with a credential of this
    run each. The bootstrap user named by the URL is used only to create and drop them and
    the scratch databases; every test connection is a restricted login, verified on opening
    (`session_user`, `current_user`).

    HH-DB-01 R3-01/R4-01: creating or dropping a role or a database, and changing a
    maintenance ACL, first asks `tests/postgres_identity.py` who the cluster is. Without a
    verified identity - or when the container and the SQL connection disagree, or when the
    identity changed between setup and cleanup - the operation is refused and no destructive
    statement is sent. A bootstrap connection is acquired only through
    `PrivilegedBoundary`, which accepts only an identity the identity module sealed and
    verifies every connection it issues against that cluster's system identifier. `cluster`
    and `docker` are injectable for the offline tests; in a real run they are established on
    first demand from the environment."""

    def __init__(
        self,
        target: Target,
        *,
        connect: Optional[Callable[[Mapping[str, Any]], Any]] = None,
        token: Optional[str] = None,
        cluster: Optional[identity.ClusterIdentity] = None,
        docker: Optional[Any] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.target = target
        self.token = token or uuid.uuid4().hex
        if not _TOKEN.fullmatch(self.token):
            raise ValueError("a run token is 32 lower-case hex digits")
        self._connect = connect or _psycopg_connect
        self._databases: dict[str, str] = {}  # name -> "created" (not yet marked) | "marked"
        self._database_owners: dict[str, str] = {}  # name -> the role pg_database.datdba names
        # R5-05: an attempted CREATE whose outcome could not be confirmed. These are
        # PRESERVED and reported, never adopted and never dropped without proof.
        self._ambiguous: dict[str, str] = {}  # name -> the owner it was attempted for
        # R7: databases of this run that are HELD - the process that used them could not be
        # confirmed exited. They stay in `_databases` (the ownership record is not removed);
        # this only says that no cleanup may destroy them or anything around them.
        self._held: dict[str, HeldDatabase] = {}
        # R7-01: the evidence behind each hold, kept OUT of the reported record: whether a
        # spawn was attempted and the actual process handle `Popen` returned. A handle is a
        # live object, so it is transient state of this run and is never serialized.
        self._held_workers: dict[str, _HeldWorker] = {}
        self._counter = 0
        self._roles: Optional[dict[str, RoleIdentity]] = None
        self._role_order: list[str] = []
        self._prepared_complete = False
        # R3-02: each maintenance CONNECT handed out, by role and database, so cleanup
        # can revoke exactly those (a CONNECT grant blocks DROP ROLE).
        self._maintenance_grants: list[tuple[str, str, str]] = []  # (key, role, database)
        # R3-02: cleanup problems stay here after they are raised, so nothing is falsely
        # recorded as cleaned and an operator (or a retrying close()) can see them.
        self.cleanup_failures: list[BaseException] = []
        self._cluster = cluster
        self._docker = docker
        self._environ = environ
        # R4-01: the engines this run created and verified. `apply_privileges()` accepts
        # only those, and then verifies the connection it is about to mutate ACLs on.
        self._guarded_engines: "weakref.WeakKeyDictionary[sa.Engine, Expected]" = (
            weakref.WeakKeyDictionary()
        )

    @property
    def marker(self) -> str:
        return MARKER_PREFIX + self.token

    # --- HH-DB-01 R3-01: the independently verified cluster ------------------------------

    def verified_cluster(self) -> identity.ClusterIdentity:
        """The disposable cluster's identity, established once from the container (never
        from SQL) and cached. Refuses with `UnsafeTarget` when it cannot be established.

        R5-01/R5-02: a cached identity is only returned while the identity module still
        accepts its facts as independently established. A hand-built or altered identity
        injected into the run is refused here, before any privileged expectation exists."""
        if self._cluster is None:
            docker = self._docker if self._docker is not None else identity.Docker()
            environ = os.environ if self._environ is None else self._environ
            try:
                self._cluster = identity.establish(self.target, environ=environ, docker=docker)
            except identity.IdentityError as refusal:
                raise UnsafeTarget(refusal.code, refusal.message) from None
            self._docker = docker
        if not identity.issued(self._cluster):
            raise UnsafeTarget(
                "cluster_identity_missing",
                "the run's cluster facts were not independently established (or no longer "
                "match them); refusing cluster-wide work.",
            )
        return self._cluster

    def _require_verified_cluster(self) -> identity.ClusterIdentity:
        """Every privileged entry point calls this before its first statement (R4-01)."""
        return self.verified_cluster()

    def _has_state(self) -> bool:
        return bool(
            self._databases or self._ambiguous or self._roles or self._maintenance_grants
        )

    def _require_cleanup_identity(self) -> None:
        """R3-01/R3-02/R5-03: cleanup destroys only what the *same* verified cluster
        created. Re-runs the initial eligibility checks (container, volume provenance,
        control file) immediately before the operation that needs them, so a check that
        succeeded earlier never authorizes a later mutation after the evidence is gone."""
        if not self._has_state():
            return
        cluster = self._cluster
        if cluster is None:
            raise UnsafeTarget(
                "cluster_identity_missing",
                "cleanup was asked for state this run cannot prove it created on a verified "
                "cluster; nothing is destroyed.",
            )
        environ = os.environ if self._environ is None else self._environ
        docker = self._docker if self._docker is not None else identity.Docker()
        try:
            # R5-01/R5-03: re-running the initial eligibility checks, not merely comparing
            # a cached value, is what makes cleanup's identity proof fresh.
            self._cluster = cluster.revalidate(docker, self.target, environ=environ)
        except identity.IdentityError as refusal:
            raise UnsafeTarget(refusal.code, refusal.message) from None
        self._docker = docker

    def pending_cleanup(self) -> dict[str, Any]:
        """R3-02: what this run still has to clean (unsuccessful records are retained).

        R7: `held` names the databases that must NOT be cleaned - each with the reason the
        hold exists, the worker's PID when it is known, the owning run and the resources
        kept around it. Nothing here releases a hold; it is a record for the operator."""
        return {
            "databases": tuple(self._databases),
            "ambiguous": tuple(self._ambiguous),
            "roles": tuple(self._role_name(key) for key in self._role_order),
            "maintenance_grants": tuple(self._maintenance_grants),
            "failures": tuple(self.cleanup_failures),
            "held": tuple(self._held.values()),
        }

    def _retained_resources(self) -> Mapping[str, Any]:
        """What this run still tracks, recorded when a database is held so that a recovery
        does not depend on the run object outliving the failure."""
        return {
            "databases": tuple(self._databases),
            "roles": tuple(self._role_name(key) for key in self._role_order),
            "maintenance_grants": tuple(self._maintenance_grants),
        }

    def _require_held(self, name: str, what: str) -> HeldDatabase:
        """The hold record this run created, or a refusal that changes nothing.

        Only a database this run still tracks can be held or operated on, and the record's
        token must still be this run's. Nothing is opened and nothing is altered on the
        refusal path, so an unknown name can never be used to reach the evidence state."""
        held = self._held.get(name)
        if held is None:
            raise UnsafeTarget(
                "not_held", f"that database is not held by this run; {what}."
            )
        if held.test_run != self.token or name not in self._databases:
            raise UnsafeTarget(
                "not_held",
                f"the hold record does not belong to this run's inventory; {what}.",
            )
        return held

    def hold_database(
        self,
        name: str,
        *,
        reason: str = HELD_UNCONFIRMED_WORKER_EXIT,
        worker_pid: Optional[int] = None,
    ) -> HeldDatabase:
        """R7: retain one of this run's scratch databases instead of ever dropping it.

        The database must already be tracked (a name this run created); holding anything
        else is refused, so this can never be used to protect a foreign database from the
        checks that govern it. The call is idempotent and deterministic and performs NO SQL
        and NO Docker inspection: it must work on the path where a process may still be
        alive, exactly when those operations are unsafe or unavailable.

        The ownership record is KEPT - the database stays in the inventory - and there is no
        automatic release: only an explicit `release_hold()` with positive evidence ends a
        hold. `drop_database()` and `close()` refuse while the hold exists."""
        if reason not in HELD_REASONS:
            raise UnsafeTarget(
                "held_reason",
                "the only retained state this harness records is an unconfirmed worker exit.",
            )
        # Refuses a name this run did not create (and one outside the generated form); no
        # connection, no Docker call, nothing destroyed. An unknown name is never "held".
        self._state(name, "created", "marked")
        existing = self._held.get(name)
        if existing is not None:  # idempotent: the first record stands, nothing is cleared
            return existing
        held = HeldDatabase(
            name=name,
            reason=reason,
            worker_pid=worker_pid,
            test_run=self.token,
            retained=self._retained_resources(),
        )
        self._held[name] = held
        return held

    def note_spawn_attempt(self, name: str) -> HeldDatabase:
        """R7-01: record, BEFORE `Popen` is called, that this run is about to attempt a spawn.

        This is the evidence the release decision rests on: a `Popen` call can raise AFTER
        the operating system has created the process, and then no handle ever comes back -
        so "no handle" proves nothing. A recorded attempt is what makes
        `RELEASED_SPAWN_NEVER_ATTEMPTED` refusable on that path, even though the record names
        no worker.

        The call opens nothing - no SQL, no Docker inspection, no connection - so it can be
        made exactly where the attempt is about to happen. The record is updated in place and
        the call is idempotent. A caller that receives an exception must not enter `Popen`:
        the evidence is what authorizes the spawn path, not the other way round."""
        held = self._require_held(name, "no spawn attempt is recorded")
        worker = self._held_workers.setdefault(name, _HeldWorker())
        if held.spawn_attempted:
            worker.spawn_attempted = True
            return held
        updated = replace(held, spawn_attempted=True)
        worker.spawn_attempted = True
        self._held[name] = updated  # in place: no instant without the hold
        return updated

    def note_held_process(self, name: str, *, process: Any) -> HeldDatabase:
        """R7-01: associate the ACTUAL process `Popen` returned with a hold that already
        recorded its spawn attempt.

        The handle is the only thing that can prove the worker's exit later (`poll()` or a
        completed `wait()`): a caller-supplied integer is not. It is therefore associated as
        soon as `Popen` returns, before any fallible post-spawn step (closing the log,
        building a wrapper, a transfer) can lose ownership. The handle is kept only in this
        run's transient state; the reported record stays plain data (database, reason, pid,
        token, retained resources).

        Only a hold this run recorded can be associated, and only after its spawn attempt
        was recorded. A process without a usable integer pid or without the `poll()` evidence
        this run relies on is refused, and a different process - or a pid that contradicts
        the one already recorded - is refused too. Every refusal leaves the hold exactly as
        it was."""
        held = self._require_held(name, "no process is associated with it")
        worker = self._held_workers.get(name)
        if worker is None or not worker.spawn_attempted:
            raise UnsafeTarget(
                "held_spawn",
                "no spawn attempt was recorded for that hold; a process cannot be associated "
                "with a spawn this run never recorded.",
            )
        pid = getattr(process, "pid", None)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise UnsafeTarget(
                "held_worker", "the associated process must name a positive integer pid."
            )
        if not callable(getattr(process, "poll", None)):
            raise UnsafeTarget(
                "held_worker",
                "the associated process must answer whether it has exited; its exit could "
                "not otherwise be established.",
            )
        if worker.process is not None and worker.process is not process:
            raise UnsafeTarget(
                "held_worker",
                "the hold already names a different process object; the record is kept as it is.",
            )
        if held.worker_pid is not None and held.worker_pid != pid:
            raise UnsafeTarget(
                "held_worker",
                "the hold already names a different worker process; the record is kept as it is.",
            )
        if worker.process is process and held.worker_pid == pid:
            return held  # idempotent: nothing to change, nothing removed
        updated = replace(held, worker_pid=pid)
        worker.process = process
        self._held[name] = updated  # in place: no instant without the hold
        return updated

    def note_held_worker(self, name: str, *, worker_pid: int) -> HeldDatabase:
        """R7: associate the worker's process id with a hold that ALREADY exists.

        This is the pre-armed lifecycle's second half: the hold is registered before the
        worker is started (so a spawn that cannot be confirmed never leaves the database
        unprotected), and the process id only becomes known afterwards. The record is
        replaced in place - there is no moment at which the database is not held - and
        nothing is opened: no SQL, no Docker inspection, no connection.

        Only a hold this run recorded can be associated. A different pid on a record that
        already names one is refused (the protection stays exactly as it was): a record
        that names the wrong process is worse evidence for a recovery than one that names
        none."""
        held = self._held.get(name)
        if held is None:
            raise UnsafeTarget(
                "not_held",
                "that database is not held by this run; no worker is associated with it.",
            )
        if held.test_run != self.token or name not in self._databases:
            raise UnsafeTarget(
                "not_held",
                "the hold record does not belong to this run's inventory; it is not altered.",
            )
        if isinstance(worker_pid, bool) or not isinstance(worker_pid, int):
            raise UnsafeTarget(
                "held_worker", "a worker process id must be an integer."
            )
        if held.worker_pid is not None and held.worker_pid != worker_pid:
            raise UnsafeTarget(
                "held_worker",
                "the hold already names a different worker process; the record is kept as it is.",
            )
        if held.worker_pid == worker_pid:
            return held  # idempotent: nothing to change, nothing removed
        updated = replace(held, worker_pid=worker_pid)
        self._held[name] = updated  # in place: no instant without the hold
        return updated

    def release_hold(
        self,
        name: str,
        *,
        reason: str,
        worker_exit: Optional[int] = None,
    ) -> HeldDatabase:
        """R7-01: the ONE explicit way a hold ends, and only on evidence this run holds.

        `reason` is `RELEASED_WORKER_EXIT_CONFIRMED` or `RELEASED_SPAWN_NEVER_ATTEMPTED`;
        anything else is refused. The decision is NOT the caller's assertion:

        * a confirmed exit requires the process handle `note_held_process()` associated, and
          the handle itself must report that it exited (`poll()` returns a status). A missing
          handle, an exception from `poll()`, a process that is still running, or a status
          that contradicts the handle is a refusal. `worker_exit`, when supplied, is only a
          cross-check against the status the handle actually reported - a bare integer never
          releases anything. A nonzero status is a valid exit.
        * a spawn that never happened requires that this run recorded no spawn attempt and
          that no worker pid or process was associated. A recorded attempt keeps the hold
          even when no handle came back: `Popen` can raise after the operating system has
          already created the process.

        This operation opens nothing - no SQL, no Docker, no connection - so it works on the
        path where a process may still be alive, and it does not weaken any check:
        `drop_database()` still revalidates the container, the control file and the database's
        marker and owner before it destroys anything. Only a database this run still tracks
        can be released, and every refusal leaves the hold (and its transient evidence)
        exactly as it was. Every ownership record is preserved: the database stays in the
        inventory until it is dropped, and a release never removes a role or a grant."""
        if reason not in RELEASE_REASONS:
            raise UnsafeTarget(
                "release_reason",
                "a hold is released only for a worker exit confirmed by the operating system "
                "or a spawn attempt positively shown never to have happened.",
            )
        held = self._require_held(name, "there is nothing to release")
        worker = self._held_workers.get(name)

        if reason == RELEASED_WORKER_EXIT_CONFIRMED:
            exit_status = self._confirmed_exit_status(worker)
            if worker_exit is not None:
                if (
                    isinstance(worker_exit, bool)
                    or not isinstance(worker_exit, int)
                    or worker_exit != exit_status
                ):
                    raise UnsafeTarget(
                        "release_evidence",
                        "the supplied exit status does not match the status the associated "
                        "process reported; the hold is kept.",
                    )
        else:
            if worker_exit is not None:
                raise UnsafeTarget(
                    "release_evidence",
                    "a spawn attempt that never happened has no exit status.",
                )
            if held.spawn_attempted or (worker is not None and worker.spawn_attempted):
                raise UnsafeTarget(
                    "release_evidence",
                    "a spawn attempt was recorded for this hold, so it cannot be shown that "
                    "no worker exists; the hold is kept.",
                )
            if held.worker_pid is not None or (
                worker is not None and worker.process is not None
            ):
                raise UnsafeTarget(
                    "release_evidence",
                    "a worker process is associated with this hold, so it cannot be shown "
                    "that no worker exists; the hold is kept.",
                )

        del self._held[name]
        self._held_workers.pop(name, None)
        return held

    def _confirmed_exit_status(self, worker: Optional["_HeldWorker"]) -> int:
        """The status the ACTUAL associated process reported, or a refusal.

        The evidence is the process's own answer, never a number the caller supplied: an
        absent handle cannot prove an exit (the process could exist without one), `poll()`
        is the operating-system-backed question, and a status that contradicts the handle's
        `returncode` is not evidence either. Nothing is changed on the refusal path."""
        if worker is None or worker.process is None:
            raise UnsafeTarget(
                "release_evidence",
                "no process handle is associated with this hold; its exit cannot be "
                "established and the hold is kept.",
            )
        process = worker.process
        try:
            observed = process.poll()
        except BaseException:
            raise UnsafeTarget(
                "release_evidence",
                "the associated process did not report whether it has exited; the hold is kept.",
            ) from None
        if observed is None:
            raise UnsafeTarget(
                "release_evidence",
                "the associated process is still running; the hold is kept.",
            )
        if isinstance(observed, bool) or not isinstance(observed, int):
            raise UnsafeTarget(
                "release_evidence",
                "the associated process reported no usable exit status; the hold is kept.",
            )
        reported = getattr(process, "returncode", observed)
        if reported is not None and reported != observed:
            raise UnsafeTarget(
                "release_evidence",
                "the associated process reported contradictory exit evidence; the hold is kept.",
            )
        return observed

    def _require_no_holds(self) -> None:
        """R7: cleanup may not run at all while a database is held.

        A held database keeps the roles, the maintenance grants and the other databases that
        belong to it; destroying any of those would leave the ownership state of this run
        half-cleaned around a database that must survive. Refusing BEFORE the first step is
        the fail-closed choice: no role is dropped, no grant is revoked, nothing is opened."""
        if self._held:
            raise UnsafeTarget(
                "held_database",
                "cleanup was asked to run while a scratch database is held after an "
                "unconfirmed worker exit; nothing is dropped, no role is dropped and no "
                "grant is revoked.",
            )

    def _expected(
        self,
        database: str,
        marker: Optional[str],
        role: Optional[RoleIdentity] = None,
    ) -> "Expected":
        """The expectation for one connection (R4-01/R5-02).

        EVERY expectation carries the independently established identifier: the bootstrap
        (`role is None`) and the maintenance logins run privileged statements, and even the
        runtime logins are verified against the same cluster so a connection can never be
        to a different one. `_require_verified_cluster()` refuses a missing or unissued
        identity here - the identifier is not an optional omission - and the returned
        `Expected` cannot be built without it."""
        cluster = self._require_verified_cluster()
        return Expected(
            self.target,
            database,
            marker,
            cluster.system_identifier,
            identity=role,
            database_owner=self._owner_of(database),
        )

    # --- HH-DB-01 CI-16/CI-17: the restricted identities ---------------------------------

    @property
    def roles(self) -> Optional[Mapping[str, RoleIdentity]]:
        """The per-run identities, or None until `prepare_roles()` has succeeded."""
        return self._roles

    def _role_name(self, key: str) -> str:
        return f"hh_t_{self.token}_{key}"

    def identity(self, key: str) -> RoleIdentity:
        if self._roles is None or key not in self._roles:
            raise UnsafeTarget("no_identity", "the run has no prepared restricted identity.")
        return self._roles[key]

    def prepare_roles(self) -> Mapping[str, RoleIdentity]:
        """Create the seven project identities of this run (idempotent). The bootstrap
        superuser is required: it creates the roles and databases and does nothing else.

        R2-05/R3-02: a failure anywhere in the preparation - after one role, after a
        membership grant, after a maintenance grant, before the maintenance handoff - runs
        the same ownership- and identity-verified cleanup as `close()` before it
        propagates. A cleanup failure never hides the setup failure (it is raised with it
        as its cause) and never clears its own tracking: a partial set cannot escape into
        a later test unrecorded."""
        if self._prepared_complete:
            return self._roles  # type: ignore[return-value]
        # R3-01: no role, grant or database before the cluster is independently verified.
        self._require_verified_cluster()
        created: dict[str, RoleIdentity] = self._roles if self._roles is not None else {}
        self._roles = created  # close() sees each role as soon as it exists
        try:
            with self.control() as control:
                # R2-06: roles are cluster-wide and the maintenance ACL is a cluster-level
                # change. Both are refused unless the server is independently identified
                # as this harness's dedicated disposable test cluster.
                self._require_clean_inventory(control)
                row = control.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
                if row is None or row[0] is not True:
                    raise UnsafeTarget(
                        "bootstrap_role",
                        "creating the per-run test roles needs a superuser in the test URL; "
                        "use the throwaway server of compose.test-postgres.yaml.",
                    )
                names = {key: self._role_name(key) for key in ROLE_KEYS}
                for name in names.values():
                    if control.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone():
                        raise UnsafeTarget("role_exists", "a per-run test role already exists; refusing to adopt it.")
                passwords = {key: secrets.token_hex(24) for key in ROLE_KEYS if key != "owner"}

                def role(key: str, attributes: str) -> None:
                    password = passwords.get(key)
                    login = f" LOGIN PASSWORD '{password}'" if password is not None else " NOLOGIN"
                    control.execute(f'CREATE ROLE "{names[key]}"{login} {attributes}')
                    created[key] = RoleIdentity(
                        key=key,
                        name=names[key],
                        password=password,
                        session_user=names[key],
                        current_user=names[key],
                    )
                    if key not in self._role_order:  # (a retried preparation is idempotent)
                        self._role_order.append(key)

                base = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS"
                role("owner", base)
                role("migrate", base)
                control.execute(f'GRANT "{names["owner"]}" TO "{names["migrate"]}" WITH SET TRUE, INHERIT FALSE')
                control.execute(f'ALTER ROLE "{names["migrate"]}" SET role = \'{names["owner"]}\'')
                role("app", base)
                control.execute(f'ALTER ROLE "{names["app"]}" SET search_path = \'public\'')
                control.execute(f'ALTER ROLE "{names["app"]}" SET statement_timeout = \'30s\'')
                control.execute(f'ALTER ROLE "{names["app"]}" SET lock_timeout = \'5s\'')
                control.execute(f'ALTER ROLE "{names["app"]}" SET idle_in_transaction_session_timeout = \'60s\'')
                role("purge", base)
                role("backup", base)
                role("provision", base)
                role("restore", "NOSUPERUSER CREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS")
                control.execute(f'GRANT "{names["owner"]}" TO "{names["restore"]}" WITH SET TRUE, INHERIT FALSE')
                for key, application in (
                    ("migrate", "hh_migrate"),
                    ("app", "hh_web"),
                    ("purge", "hh_purge"),
                    ("backup", "hh_backup"),
                    ("provision", "hh_provision"),
                ):
                    control.execute(f'ALTER ROLE "{names[key]}" SET application_name = \'{application}\'')
                # R2-06/R2-07: the maintenance database is not reachable by the application
                # identities; only the migration and restore logins may connect to it.
                self._restrict_maintenance_database(control, names)
            # The migration login's session user is itself; its current user is the owner.
            created["migrate"] = RoleIdentity(
                key="migrate",
                name=names["migrate"],
                password=passwords["migrate"],
                session_user=names["migrate"],
                current_user=names["owner"],
            )
            self._prepared_complete = True
        except BaseException as setup_error:
            # R2-05/R3-02: nothing a partial preparation created may outlive the failure,
            # but a cleanup failure is reported with the setup failure, never instead of
            # it and never swallowed.
            try:
                self._cleanup_after_failed_setup()
            except BaseException as cleanup_error:
                raise setup_error from cleanup_error
            raise
        return created

    def _cleanup_after_failed_setup(self) -> None:
        """The same order `close()` uses: maintenance grants first (they block DROP ROLE),
        then only the roles this run created. Failed records are kept."""
        if not self._has_state():
            return
        self._require_no_holds()  # R7: the same all-or-nothing refusal `close()` makes
        self._require_cleanup_identity()
        problems = self._resolve_ambiguous_creations()
        if problems:
            self.cleanup_failures.extend(problems)
            raise problems[0]
        for name in reversed(list(self._databases)):
            try:
                self.drop_database(name)
            except BaseException as problem:
                problems.append(problem)
        if problems:
            self.cleanup_failures.extend(problems)
            raise problems[0]
        problems.extend(self._revoke_maintenance_grants())
        if problems:
            self.cleanup_failures.extend(problems)
            raise problems[0]
        problems.extend(self._drop_roles())
        if problems:
            self.cleanup_failures.extend(problems)
            raise problems[0]

    def _require_clean_inventory(self, control: Any) -> None:
        """Additional, SQL-side evidence that this is a dedicated instance: the bootstrap
        role is a superuser and owns the maintenance database, the cluster holds no
        database other than the initial ones and this harness's per-run scratch databases,
        and a cluster that declares itself with `house_helper.test_cluster` must say
        `disposable`. None of this is the identity proof - R3-01's independent contract is
        checked first and cannot be replaced by any of it - it can only refuse more.
        dedicated disposable test cluster (R2-06). Evidence, none of it a URL, a name
        prefix or a port: the bootstrap role is a superuser and owns the maintenance
        database (`pg_database.datdba`); the cluster holds no databases other than the
        initial ones and this harness's per-run scratch databases; and a cluster that
        declares itself with the `house_helper.test_cluster` setting must say `disposable`.
        A COMMENT and a database name are never enough."""
        row = control.execute(
            "SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
            "(SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()), "
            f"current_setting('{DEDICATED_SETTING}', true)"
        ).fetchone()
        if row is None:
            raise UnsafeTarget("not_dedicated", "the server did not answer the cluster identity question.")
        user, superuser, owner, declared = tuple(row)
        if declared is not None and declared != DEDICATED_VALUE:
            raise UnsafeTarget(
                "not_dedicated",
                "the server declares itself as something other than the disposable test cluster; "
                "refusing cluster-wide changes.",
            )
        if superuser is not True or user != owner:
            raise UnsafeTarget(
                "not_dedicated",
                "the maintenance database is not owned by the superuser of the test URL: this is not "
                "the dedicated disposable test cluster, so no cluster-wide change is made.",
            )
        # R5-04 item 8: a scratch-LOOKING name is not evidence. A database this harness
        # created carries its marker (the run token in the COMMENT) and is owned either by
        # the bootstrap user or by a per-run `hh_t_<token>_owner` role. A foreign or
        # persistent database named like a scratch one has neither, and is refused.
        strangers = [
            name
            for name, comment, owner in control.execute(
                "SELECT datname, shobj_description(oid, 'pg_database'), pg_get_userbyid(datdba) "
                "FROM pg_database WHERE datname NOT IN ('postgres', 'template0', 'template1')"
            ).fetchall()
            if not (
                _SCRATCH_NAME.fullmatch(name)
                and isinstance(comment, str)
                and _SCRATCH_MARKER.fullmatch(comment)
                and (
                    owner == self.target.username
                    or (isinstance(owner, str) and bool(_SCRATCH_OWNER.fullmatch(owner)))
                )
            )
        ]
        if strangers:
            raise UnsafeTarget(
                "not_dedicated",
                "the server holds databases that this harness did not create; refusing cluster-wide changes "
                "on a shared instance.",
            )

    def _restrict_maintenance_database(self, control: Any, names: Mapping[str, str]) -> None:
        """ROL-16/R2-06: `PUBLIC` loses `CONNECT` on the maintenance database; only the
        migration and restore logins are handed it. Verified before anything else uses it."""
        maintenance = self.target.maintenance_database
        control.execute(f'REVOKE ALL ON DATABASE "{maintenance}" FROM PUBLIC')
        for key in MAINTENANCE_CONNECT_ROLES:
            role = names[key]
            control.execute(f'GRANT CONNECT ON DATABASE "{maintenance}" TO "{role}"')
            # R3-02: track the exact grant the moment it exists. Cleanup revokes it before
            # DROP ROLE, and a failed revocation keeps its record for the next attempt.
            self._maintenance_grants.append((key, role, maintenance))
        public_rows = control.execute(
            "SELECT count(*) FROM aclexplode("
            "(SELECT datacl FROM pg_database WHERE datname = %s)) a "
            "WHERE a.grantee = 0 AND a.privilege_type IN ('CONNECT', 'TEMPORARY')",
            (maintenance,),
        ).fetchone()
        if public_rows is None or public_rows[0] != 0:
            raise UnsafeTarget("fence", "the maintenance database still grants PUBLIC a database privilege.")
        granted = {
            row[0]: row[1]
            for row in control.execute(
                "SELECT rolname, has_database_privilege(rolname, %s, 'CONNECT') FROM pg_roles "
                "WHERE rolname = ANY(%s)",
                (maintenance, [names[key] for key in ("app", "purge", "backup", "provision")] + [
                    names[key] for key in MAINTENANCE_CONNECT_ROLES
                ]),
            ).fetchall()
        }
        for key in ("app", "purge", "backup", "provision"):
            if granted.get(names[key]) is not False:
                raise UnsafeTarget(
                    "fence", "a runtime login can still connect to the maintenance database."
                )
        for key in MAINTENANCE_CONNECT_ROLES:
            if granted.get(names[key]) is not True:
                raise UnsafeTarget(
                    "fence", "the maintenance login was not granted CONNECT on the maintenance database."
                )


    def _state(self, name: str, *states: str) -> str:
        state = self._databases.get(name)
        if state is None or state not in states or not _SCRATCH_NAME.fullmatch(name):
            raise UnsafeTarget("not_created_by_this_run", "that database was not created by this test run.")
        return state

    def _owner_of(self, database: str) -> str:
        """The role `pg_database.datdba` must name for `database` (R2-06)."""
        return self._database_owners.get(database, self.target.username)

    def _marker_of(self, database: str) -> Optional[str]:
        return None if database == self.target.maintenance_database else self.marker

    def _privileged_boundary(self) -> PrivilegedBoundary:
        """R4-01: the single acquisition point of a bootstrap connection. It is built from
        the independently verified identity; there is no path to `self._connect` for a
        privileged database that skips it."""
        return PrivilegedBoundary(
            target=self.target, cluster=self.verified_cluster(), connect=self._connect
        )

    @contextmanager
    def control(self) -> Iterator[Any]:
        """An autocommit connection to the maintenance database, acquired through the one
        privileged boundary and verified - peer, port, database, owner and the cluster's
        system identifier - before it is yielded (R4-01). A run whose cluster has not been
        verified refuses here; nothing is opened on a guess."""
        with self._privileged_boundary().connection(
            self.target.maintenance_database, marker=None, owner=self.target.username
        ) as connection:
            yield connection

    @contextmanager
    def connect_as(self, database: str, key: str) -> Iterator[Any]:
        """A verified connection to one of this run's databases as a prepared identity
        (`session_user` and `current_user` are checked on opening, R2-03/R2-06)."""
        role = self.identity(key)
        connection = self._connect(self.target.connect_params(database, role))
        try:
            verify_connection(connection, self._expected(database, self._marker_of(database), role))
            yield connection
        finally:
            connection.close()

    def create_database(
        self,
        *,
        template: Optional[str] = None,
        isolation: Optional[str] = None,
        owner: Optional[str] = None,
        runtime_roles: Optional[Iterable[str]] = None,
    ) -> str:
        """A new database of this run's, named by the run, marked with its token; a copy of
        `template` (one this run made) when given. Returns the name.

        With `prepare_roles()` done, the database is owned by the run's NOLOGIN owner and
        created behind the frozen fence (R2-06, ROL-22): `ALLOW_CONNECTIONS false` in the
        CREATE itself, `PUBLIC` revoked, `CONNECT` granted to the migration login only,
        the disabled ACL verified, and only then enabled. The `public` schema is then
        hardened as the migration/owner login, and the four runtime logins receive their
        `CONNECT` handoff afterwards (pass `runtime_roles=()` for a template that is cloned
        before any of them may reach it)."""
        if isolation is not None and isolation not in ISOLATION_LEVELS:
            raise ValueError("unknown isolation level")
        if template is not None:
            self._state(template, "marked")
        # R3-01/R4-02: not one CREATE DATABASE before the cluster is independently
        # verified AND shown to be a dedicated disposable instance by the same eligibility
        # evidence role preparation requires (R4-02 item 7). The inventory check is defense
        # in depth; it never substitutes for the independent identity.
        self._require_verified_cluster()
        runtime = None
        if self._roles is not None:
            owner = owner or self.identity("owner").name
            runtime = ("app", "provision", "purge", "backup") if runtime_roles is None else tuple(runtime_roles)
        self._counter += 1
        name = f"house_helper_test_{self.token}_{self._counter}"
        if not _SCRATCH_NAME.fullmatch(name):
            raise UnsafeTarget("unsafe_name", "the generated database name is not the expected form.")
        clause = f' TEMPLATE "{template}"' if template is not None else ""
        if owner is not None:
            clause += f' OWNER "{owner}"'
        if self._roles is not None:
            clause += " ALLOW_CONNECTIONS false"
        with self.control() as control:
            self._require_clean_inventory(control)
            control.execute(f'CREATE DATABASE "{name}"{clause}')
            self._databases[name] = "created"
            self._database_owners[name] = owner or self.target.username
            control.execute(f"COMMENT ON DATABASE \"{name}\" IS '{self.marker}'")
            self._databases[name] = "marked"
            if isolation is not None:
                control.execute(f"ALTER DATABASE \"{name}\" SET default_transaction_isolation = '{isolation}'")
        if runtime is not None:
            self._fence_database(name, runtime)
        return name

    def create_database_as(self, key: str, *, owner_key: str = "owner") -> str:
        """R4-01/R5-05: a scratch database created BY one of the prepared logins, through a
        verified connection, tracked exactly like `create_database()` and dropped only by
        `drop_database()`.

        The restore-identity probes (RST-01/RST-08) must use this: the restore login's own
        `CREATEDB` creates the database (disabled), the run records its name and the owner
        it was made for, and cleanup goes through the same ownership-, token- and
        identity-verified path. A raw `CREATE DATABASE` in a test can therefore never leave
        a disposable database the run does not know about.

        R5-05: an ATTEMPTED CREATE is not evidence of creation or ownership. Nothing is
        recorded as this run's before the statement returns successfully. A duplicate-name
        failure is definitively not ours (the pre-existing database is never adopted); any
        other failure is recorded as AMBIGUOUS - preserved, reported by `close()`, and
        never dropped without the marker proof."""
        # The same eligibility gate as create_database() and prepare_roles(), first: no
        # connection is attempted, and no name is generated, on an unverified cluster.
        self._require_verified_cluster()
        self.identity(key)  # the creating login must exist before a name is generated
        owner = self.identity(owner_key).name
        self._counter += 1
        name = f"house_helper_test_{self.token}_{self._counter}"
        if not _SCRATCH_NAME.fullmatch(name):
            raise UnsafeTarget("unsafe_name", "the generated database name is not the expected form.")
        with self.control() as control:
            self._require_clean_inventory(control)
        with self.connect_as(self.target.maintenance_database, key) as connection:
            try:
                connection.execute(
                    f'CREATE DATABASE "{name}" OWNER "{owner}" ALLOW_CONNECTIONS false'
                )
            except BaseException as creation_error:
                if not _is_duplicate_database(creation_error):
                    # The statement may or may not have taken effect. Keep the record for
                    # reporting, but it authorizes nothing: `drop_database()` never sees it
                    # and `close()` refuses to guess.
                    self._ambiguous[name] = owner
                raise
            # Only a CONFIRMED successful creation is tracked as this run's, immediately
            # after the statement returns. From here the database is provably ours (CREATE
            # is atomic and the name did not pre-exist), so a later failure - even the
            # connection's own close - is safely cleaned up by name and owner.
            self._databases[name] = "created"
            self._database_owners[name] = owner
        with self.control() as control:
            control.execute(f"COMMENT ON DATABASE \"{name}\" IS '{self.marker}'")
        self._databases[name] = "marked"
        return name

    def _resolve_ambiguous_creations(self) -> list[BaseException]:
        """R5-05: settle attempted creations whose outcome was unknown. A name that does
        not exist was never created and is forgotten; a name that exists cannot be shown to
        be this run's, so it is PRESERVED and reported. Nothing here drops anything."""
        if not self._ambiguous:
            return []
        problems: list[BaseException] = []
        kept: dict[str, str] = {}
        with self.control() as control:
            for name, owner in self._ambiguous.items():
                self._require_cleanup_identity()
                row = control.execute(_COMMENT_AND_OWNER_SQL, (name,)).fetchone()
                if row is None:
                    continue  # definitively not created
                kept[name] = owner
                problems.append(
                    UnsafeTarget(
                        "ambiguous_create",
                        "an attempted database creation did not resolve; the database is "
                        "preserved and reported instead of being dropped on a guess.",
                    )
                )
        self._ambiguous = kept
        return problems

    def _fence_database(self, name: str, runtime_roles: Iterable[str]) -> None:
        """BOOT-01/E3 shape for a scratch database (R2-06): created disabled, `PUBLIC`
        revoked, the migration login granted and verified, enabled, the `public` schema
        hardened and the default ACLs installed as the owner, then the H1/H2/H3 handoffs -
        each observed, not assumed."""
        migrate = self.identity("migrate").name
        owner = self.identity("owner").name
        with self.control() as control:
            control.execute(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC')
            control.execute(f'GRANT CONNECT ON DATABASE "{name}" TO "{migrate}"')
            row = control.execute(
                "SELECT datallowconn, has_database_privilege(%s, %s, 'CONNECT'), "
                "pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
                (migrate, name, name),
            ).fetchone()
            public = control.execute(
                "SELECT d.datacl IS NOT NULL, "
                "(SELECT count(*) FROM aclexplode(d.datacl) a WHERE a.grantee = 0 "
                "AND a.privilege_type IN ('CONNECT', 'TEMPORARY')) "
                "FROM pg_database d WHERE d.datname = %s",
                (name,),
            ).fetchone()
            if (
                row is None
                or row[0] is not False
                or row[1] is not True
                or row[2] != owner
                or public is None
                or public[0] is not True  # the ACL is materialized, so the absence below is evidence
                or public[1] != 0
            ):
                raise UnsafeTarget("fence", "the scratch database did not reach the verified disabled state.")
            control.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
        self._harden_schema(name)
        with self.control() as control:
            for key in runtime_roles:
                control.execute(f'GRANT CONNECT ON DATABASE "{name}" TO "{self.identity(key).name}"')
            granted = {
                row[0]: row[1]
                for row in control.execute(
                    "SELECT rolname, has_database_privilege(rolname, %s, 'CONNECT') FROM pg_roles "
                    "WHERE rolname = ANY(%s)",
                    (name, [self.identity(key).name for key in runtime_roles]),
                ).fetchall()
            }
            if any(granted.get(self.identity(key).name) is not True for key in runtime_roles):
                raise UnsafeTarget("fence", "a runtime login did not receive its CONNECT handoff.")
            restore = self.identity("restore").name
            if control.execute(
                "SELECT has_database_privilege(%s, %s, 'CONNECT')", (restore, name)
            ).fetchone()[0] is not False:
                raise UnsafeTarget("fence", "the restore login can reach an application database.")

    def _harden_schema(self, name: str) -> None:
        """ROL-09/ROL-14 as the migration login (which assumes the owner): the `public`
        schema belongs to the owner, `PUBLIC` keeps no privilege, the six logins get
        `USAGE` and no `CREATE`, and the default ACLs for future objects carry no purge
        grant. Observed before the runtime handoffs (R2-06 step 7/8)."""
        owner = self.identity("owner").name
        users = ", ".join(
            f'"{self.identity(key).name}"'
            for key in ("app", "purge", "backup", "provision", "migrate", "restore")
        )
        with self.connect_as(name, "migrate") as connection:
            connection.execute(f'ALTER SCHEMA public OWNER TO "{owner}"')
            connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            connection.execute(f"GRANT USAGE ON SCHEMA public TO {users}")
            app = self.identity("app").name
            backup = self.identity("backup").name
            for statement in (
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{app}"',
                f'GRANT USAGE ON SEQUENCES TO "{app}"',
                f'GRANT SELECT ON TABLES TO "{backup}"',
                f'GRANT SELECT ON SEQUENCES TO "{backup}"',
            ):
                connection.execute(
                    f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public {statement}'
                )
            schema = connection.execute(
                "SELECT n.nspowner::regrole::text, "
                "(SELECT count(*) FROM aclexplode(n.nspacl) a WHERE a.grantee = 0) "
                "FROM pg_namespace n WHERE n.nspname = 'public'"
            ).fetchone()
            if schema is None or schema[0] != owner or schema[1] != 0:
                raise UnsafeTarget("fence", "the public schema did not reach the verified hardened state.")
            for key in ("app", "purge", "backup", "provision", "migrate", "restore"):
                usage, create = connection.execute(
                    "SELECT has_schema_privilege(%s, 'public', 'USAGE'), "
                    "has_schema_privilege(%s, 'public', 'CREATE')",
                    (self.identity(key).name, self.identity(key).name),
                ).fetchone()
                if usage is not True or create is not False:
                    raise UnsafeTarget("fence", "a runtime login does not have exactly USAGE on the public schema.")
            purge_default = connection.execute(
                "SELECT count(*) FROM pg_default_acl d "
                "JOIN pg_roles defacl_owner ON defacl_owner.oid = d.defaclrole "
                "JOIN LATERAL aclexplode(d.defaclacl) a ON true "
                "JOIN pg_roles acl_grantee ON acl_grantee.oid = a.grantee "
                "WHERE defacl_owner.rolname = %s AND acl_grantee.rolname = %s",
                (owner, self.identity("purge").name),
            ).fetchone()
            if purge_default is None or purge_default[0] != 0:
                raise UnsafeTarget("fence", "the purge login inherits a default privilege, which ROL-14 forbids.")

    def apply_privileges(self, engine: sa.Engine) -> None:
        """ROL-10..ROL-14 for one of this run's databases, executed as its owner through the
        migration login. Idempotent, and safe on a partially migrated schema: the explicit
        lists are intersected with the tables that exist (a revision before them has nothing
        of theirs to grant).

        R4-01: an ACL mutation is privileged. The engine must be one this run created and
        verified - an arbitrary external engine is refused outright - and the ACTUAL
        connection the transaction uses is verified (peer, port, database, owner, identity
        and the independently established system identifier) before the first GRANT."""
        cluster = self._require_verified_cluster()
        roles = self._roles
        if roles is None:
            raise UnsafeTarget("no_identity", "no prepared identities: no ACL contract to apply.")
        expected = self._guarded_engines.get(engine)
        if expected is None or expected.cluster_identifier != cluster.system_identifier:
            raise UnsafeTarget(
                "unverified_engine",
                "privileged ACL changes are applied only through an engine this run created "
                "and verified; an external engine is refused.",
            )
        app, purge, backup, provision = (roles[key].name for key in ("app", "purge", "backup", "provision"))
        owner = roles["owner"].name
        with engine.begin() as connection:
            verify_engine_connection(connection, expected)
            existing = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
            # ROL-10 - application DML, sequence usage, and then the alembic_version
            # exception, in this order: ALL TABLES has just granted DML on it too.
            connection.exec_driver_sql(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{app}"')
            connection.exec_driver_sql(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO "{app}"')
            if "alembic_version" in existing:
                connection.exec_driver_sql(f'REVOKE ALL ON alembic_version FROM "{app}"')
                connection.exec_driver_sql(f'GRANT SELECT ON alembic_version TO "{app}"')
                connection.exec_driver_sql(f'GRANT SELECT ON alembic_version TO "{purge}"')
                connection.exec_driver_sql(f'GRANT SELECT ON alembic_version TO "{backup}"')
                connection.exec_driver_sql(f'GRANT SELECT ON alembic_version TO "{provision}"')
            # ROL-11 - exactly the ten retention tables and the columns the purge locks/writes.
            purge_tables = [table for table in PURGE_TABLES if table in existing]
            if purge_tables:
                tables = ", ".join(f'"{table}"' for table in purge_tables)
                connection.exec_driver_sql(f'GRANT SELECT, DELETE ON {tables} TO "{purge}"')
                for table, columns in PURGE_COLUMN_UPDATES.items():
                    if table in existing:
                        names = ", ".join(columns)
                        connection.exec_driver_sql(f'GRANT UPDATE ({names}) ON "{table}" TO "{purge}"')
            # ROL-12 - backup reads every table and sequence.
            connection.exec_driver_sql(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{backup}"')
            connection.exec_driver_sql(f'GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "{backup}"')
            # ROL-13 - provisioning: no DELETE, no sessions.
            provision_tables = [table for table in PROVISION_TABLES if table in existing]
            if provision_tables:
                tables = ", ".join(f'"{table}"' for table in provision_tables)
                connection.exec_driver_sql(f'GRANT SELECT, INSERT, UPDATE ON {tables} TO "{provision}"')
            if "identity_links" in existing:
                connection.exec_driver_sql(f'GRANT SELECT ON identity_links TO "{provision}"')
            # ROL-14 - default privileges for future objects created by the owner.
            for statement in (
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES",
                "GRANT USAGE ON SEQUENCES",
            ):
                connection.exec_driver_sql(f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public {statement} TO "{app}"')
            for statement in ("GRANT SELECT ON TABLES", "GRANT SELECT ON SEQUENCES"):
                connection.exec_driver_sql(f'ALTER DEFAULT PRIVILEGES FOR ROLE "{owner}" IN SCHEMA public {statement} TO "{backup}"')


    def drop_database(self, name: str) -> None:
        # R7: a held database is refused by name BEFORE any Docker inspection or connection
        # (both of which `_require_cleanup_identity` and `control()` would make), and before
        # any statement. Its tracking record is never cleared.
        if name in self._held:
            raise UnsafeTarget(
                "held_database",
                "that database is held after an unconfirmed worker exit; nothing is dropped.",
            )
        state = self._state(name, "created", "marked")
        # R4-03: every cleanup entry point revalidates the container, its storage and the
        # control file's identifier against the identity established at setup - not against
        # a cached value - and keeps the record when that evidence is unavailable or
        # changed. `control()` then verifies the connection it opens against the same
        # identifier.
        self._require_cleanup_identity()
        with self.control() as control:
            row = control.execute(_COMMENT_AND_OWNER_SQL, (name,)).fetchone()
            if row is None:  # already gone
                del self._databases[name]
                return
            comment, owner = row[0], row[1]
            # Ours: still carries this run's token, or was created here and never got to be
            # marked (the COMMENT statement failed; a name that exists was not free to take);
            # and its `pg_database.datdba` is still the owner it was created for (R2-06).
            ours = comment == self.marker or (state == "created" and comment is None)
            if not ours or owner != self._owner_of(name):
                raise UnsafeTarget(
                    "not_this_run",
                    "a database that is not both marked and owned by this run is not dropped.",
                )
            control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        del self._databases[name]
        self._database_owners.pop(name, None)

    def close(self) -> None:
        """Drop everything this run still owns, whatever else failed: the container/control
        identity is re-verified first (R3-01), then its databases (they hold every object
        the roles own), then the maintenance grants it handed out, then its roles (R3-02).
        Every step is attempted in order; the first problem is raised once the later steps
        that are still safe have been tried, and every unsuccessful record is kept for a
        retry or for the operator.

        R7: if any database is held (an unconfirmed worker exit), this refuses before its
        first step instead of cleaning around it: the roles and grants that belong to the
        held database must survive with it, and a partially cleaned run is worse than a
        retained one."""
        self._require_no_holds()  # R7: before the identity check and before any step
        self._require_cleanup_identity()  # nothing destroyed if this refuses
        if not self._has_state():
            return
        # R5-05: settle attempted creations before anything is dropped. A name that cannot
        # be shown to be this run's is preserved and reported; cleanup then stops rather
        # than destroying evidence around it.
        problems = self._resolve_ambiguous_creations()
        if problems:
            self.cleanup_failures.extend(problems)
            raise problems[0]
        for name in reversed(list(self._databases)):
            try:
                self.drop_database(name)
            except BaseException as problem:
                problems.append(problem)
        if problems:
            # The roles still own those databases; dropping them now would fail and could
            # leave a half-cleaned record. Keep everything and report.
            self.cleanup_failures.extend(problems)
            raise problems[0]
        problems.extend(self._revoke_maintenance_grants())
        if problems:
            self.cleanup_failures.extend(problems)
            raise problems[0]
        problems.extend(self._drop_roles())
        if problems:
            self.cleanup_failures.extend(problems)
            raise problems[0]

    def _revoke_maintenance_grants(self) -> list[BaseException]:
        """R3-02/R5-03: revoke exactly the CONNECT grants recorded for this run, before
        any role is dropped. A grant whose revocation fails stays recorded; nothing is
        cleared on a guess.

        R5-03: the container/storage/control-file identity is re-established immediately
        before EVERY revocation, not once at `close()` entry. If the Docker evidence becomes
        unavailable while cleanup is in progress, the next mutation is refused and every
        remaining grant is kept for a later safe retry."""
        if not self._maintenance_grants:
            return []
        problems: list[BaseException] = []
        kept: list[tuple[str, str, str]] = []
        with self.control() as control:  # identity-verified; raises before any revocation
            for key, role, database in list(self._maintenance_grants):
                self._require_cleanup_identity()  # R5-03: fresh evidence per mutation
                if role != self._role_name(key) or database != self.target.maintenance_database:
                    problems.append(
                        UnsafeTarget(
                            "not_this_run",
                            "a recorded maintenance grant does not name this run's role and "
                            "the maintenance database; it is not revoked.",
                        )
                    )
                    kept.append((key, role, database))
                    continue
                try:
                    control.execute(f'REVOKE CONNECT ON DATABASE "{database}" FROM "{role}"')
                except BaseException as problem:
                    problems.append(problem)
                    kept.append((key, role, database))
        self._maintenance_grants = kept
        return problems

    def _drop_roles(self) -> list[BaseException]:
        """Drop only the roles this run created under its own token (reverse dependency
        order). A name outside the generated pattern is never dropped. A role whose drop
        failed stays recorded (R3-02): the tracking is narrowed, never falsely cleared.

        R5-03: the independently verified disposable-cluster identity is re-established
        immediately before EVERY role drop, so a successful earlier check does not
        authorize later mutations after the evidence has gone."""
        if not self._roles:
            return []
        problems: list[BaseException] = []
        survivors: list[str] = []
        with self.control() as control:
            for key in reversed(self._role_order):
                self._require_cleanup_identity()  # R5-03: fresh evidence per mutation
                name = self._role_name(key)
                if not ROLE_NAME.fullmatch(name):
                    problems.append(UnsafeTarget("unsafe_role", "a role name that is not a per-run test role."))
                    survivors.append(key)
                    continue
                try:
                    row = control.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
                    if row is not None:
                        control.execute(f'DROP ROLE "{name}"')
                except BaseException as problem:
                    problems.append(problem)
                    survivors.append(key)
        remaining = set(survivors)
        self._role_order = [key for key in self._role_order if key in remaining]
        self._roles = {
            key: role for key, role in self._roles.items() if key in remaining
        } or None
        if not remaining:
            self._prepared_complete = False
        return problems

    def url(self, name: str, role: Optional[str] = None) -> URL:
        self._state(name, "marked")
        if role is None:
            return self.target.url(name)
        return self.target.url(name, self.identity(role))

    def engine(self, name: str, role: Optional[str] = None) -> sa.Engine:
        """An engine for one of this run's databases whose every connection is verified
        (layer 3) before the first statement runs on it. `role` names a prepared identity;
        without one the engine is the bootstrap connection of the URL (legacy tests).

        R4-01: the engine is registered with this run. `apply_privileges()` accepts only a
        registered engine and still verifies the connection it actually uses."""
        role_identity = None if role is None else self.identity(role)
        url_text = self.url(name, role).render_as_string(hide_password=False)
        expected = self._expected(name, self.marker, role_identity)
        engine = guard_engine(create_db_engine(url_text), expected)
        self._guarded_engines[engine] = expected
        return engine

    def migrate_engine(self, name: str) -> sa.Engine:
        return self.engine(name, "migrate")

    def engine_as(self, database: str, role: str) -> sa.Engine:
        """A guarded engine for ANY database (including the maintenance one) as a prepared
        identity: for the negative tests that a runtime login cannot connect there."""
        role_identity = self.identity(role)
        url_text = self.target.url(database, role_identity).render_as_string(hide_password=False)
        expected = self._expected(database, self._marker_of(database), role_identity)
        engine = guard_engine(create_db_engine(url_text), expected)
        self._guarded_engines[engine] = expected
        return engine


@dataclass(frozen=True)
class Scratch:
    """One database of a run, for a test.

    With prepared identities, `engine()` is the restricted application login and the other
    accessors are the frozen matrix's logins; a test that needs DDL asks for `migrate_engine`
    explicitly (CI-16)."""

    run: Run
    name: str

    def _prepared(self) -> bool:
        return self.run.roles is not None

    def url_for(self, role: str = "app") -> URL:
        return self.run.url(self.name, role if self._prepared() else None)

    @property
    def url(self) -> URL:
        return self.url_for("app")

    @property
    def url_text(self) -> str:
        return self.url.render_as_string(hide_password=False)

    def url_text_for(self, role: str) -> str:
        return self.url_for(role).render_as_string(hide_password=False)

    def engine(self) -> sa.Engine:
        return self.run.engine(self.name, "app" if self._prepared() else None)

    def app_engine(self) -> sa.Engine:
        return self.run.engine(self.name, "app")

    def migrate_engine(self) -> sa.Engine:
        return self.run.engine(self.name, "migrate")

    def provision_engine(self) -> sa.Engine:
        return self.run.engine(self.name, "provision")

    def purge_engine(self) -> sa.Engine:
        return self.run.engine(self.name, "purge")

    def backup_engine(self) -> sa.Engine:
        return self.run.engine(self.name, "backup")



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
