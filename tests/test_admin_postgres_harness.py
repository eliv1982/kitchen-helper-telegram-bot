"""The safety harness of the opt-in PostgreSQL tests (tests/postgres_harness.py), tested OFFLINE.

No server and no psycopg are needed, so unlike tests/test_admin_postgres.py these run in
CI. What they stand in for a server is `FakeServer`, which records every statement and
can be told to lie about where a connection went; what they use of SQLAlchemy is the
real psycopg dialect (which can be asked for its connection parameters without the
driver installed) and an in-memory SQLite engine to see that the guard is wired.

The passwords here are made-up. Assertions still avoid showing values where they can: a
secret is looked for with `leaked` (names, not values).
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
from sqlalchemy.engine import make_url

from house_helper.persistence.database import DATABASE_URL_ENV
from tests import network_guard
from tests import postgres_harness as harness
from tests.postgres_harness import (
    POSTGRES_URL_ENV,
    ROUTING_ENV,
    Expected,
    Run,
    Target,
    UnsafeTarget,
    check_connect_params,
    guard_engine,
    guarded_connect,
    leaked,
    run_from_environment,
    target_from_environment,
    validate_target,
    verify_connection,
)

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "not-a-real-password"
REMOTE = "203.0.113.10"  # (documentation range: nothing is there)
BASE = f"postgresql+psycopg://postgres:{PASSWORD}@localhost:55432/postgres"
TOKEN = "0123456789abcdef0123456789abcdef"


# --- nothing may be opened: tripwires ---------------------------------------------------------------------


class Tripwire:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(1)
        raise AssertionError("a connection (or an engine) was attempted")


@pytest.fixture
def no_connections(monkeypatch):
    """Anything that could open a connection, or an engine to open one with, raises and is
    counted: the engine factory, the driver call, a pooled connect and every socket connect
    (the offline guard of tests/network_guard.py forwards to what it is given here)."""
    tripwire = Tripwire()
    monkeypatch.setattr(sa, "create_engine", tripwire)
    monkeypatch.setattr(sa.engine.Engine, "connect", tripwire)
    monkeypatch.setattr(sa.engine.default.DefaultDialect, "connect", tripwire)
    monkeypatch.setattr(harness, "create_db_engine", tripwire)
    monkeypatch.setattr(harness, "_psycopg_connect", tripwire)
    for key in network_guard.originals:
        if "connect" in key:
            monkeypatch.setitem(network_guard.originals, key, tripwire)
    return tripwire


# --- the URL is validated before anything is opened -------------------------------------------------------

VALID_HOSTS = [
    pytest.param("localhost", "127.0.0.1", id="localhost"),
    pytest.param("LocalHost", "127.0.0.1", id="localhost-any-case"),
    pytest.param("127.0.0.1", "127.0.0.1", id="ipv4"),
    pytest.param("[::1]", "::1", id="ipv6"),
]


@pytest.mark.parametrize("host, address", VALID_HOSTS)
def test_a_loopback_url_gives_a_target_pinned_to_the_literal_address(host, address, no_connections):
    target = validate_target(f"postgresql+psycopg://postgres:{PASSWORD}@{host}:55432/postgres", {})

    assert (target.username, target.address, target.port) == ("postgres", address, 55432)
    assert target.maintenance_database == "postgres"
    assert target.password is not None
    assert leaked(target.url("house_helper_test_x"), repr(target)) == []  # its text hides the password
    assert no_connections.calls == []


@pytest.mark.parametrize("userinfo", ["postgres", "postgres:"], ids=["no-password", "empty-password"])
def test_a_passwordless_url_is_valid_and_an_empty_password_means_none(userinfo, no_connections):
    target = validate_target(f"postgresql+psycopg://{userinfo}@127.0.0.1:55432/postgres", {})

    assert target.password is None  # '' and None are one thing
    url = target.url("house_helper_test_x")
    assert url.password is None
    assert "password" not in target.connect_params("x")
    assert make_url(url.render_as_string(hide_password=False)) == url
    assert no_connections.calls == []


UNSAFE = [
    # a query parameter wins over the host and database of the URL (see the dialect test below)
    pytest.param(f"{BASE}?host=remote.example.com", "query_parameters", id="query-host"),
    pytest.param(f"{BASE}?hostaddr={REMOTE}", "query_parameters", id="query-hostaddr"),
    pytest.param(f"{BASE}?dbname=persistent", "query_parameters", id="query-dbname"),
    pytest.param(f"{BASE}?database=persistent", "query_parameters", id="query-database"),
    pytest.param(f"{BASE}?service=production", "query_parameters", id="query-service"),
    pytest.param(f"{BASE}?port=5432", "query_parameters", id="query-port"),
    pytest.param(f"{BASE}?user=other", "query_parameters", id="query-user"),
    pytest.param(f"{BASE}?options=-c%20search_path%3Dx", "query_parameters", id="query-options"),
    pytest.param(f"{BASE}?passfile=/somewhere", "query_parameters", id="query-passfile"),
    pytest.param(f"{BASE}?target_session_attrs=read-write", "query_parameters", id="query-target"),
    pytest.param(f"{BASE}?load_balance_hosts=random", "query_parameters", id="query-balance"),
    pytest.param(f"{BASE}?host=a&host=b", "query_parameters", id="query-two-hosts"),
    pytest.param(f"{BASE}?HOST=remote.example.com", "query_parameters", id="query-upper-case"),
    pytest.param(f"{BASE}?sslmode=disable", "query_parameters", id="query-harmless-still-refused"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@/postgres?host=/var/run", "query_parameters", id="query-socket"),
    # not this machine
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@db.example.com:5432/postgres", "not_loopback", id="remote-name"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@{REMOTE}:5432/postgres", "not_loopback", id="remote-ip"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@0.0.0.0:5432/postgres", "not_loopback", id="any-address"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@localhost.example.com:5432/postgres", "not_loopback", id="localhost-lookalike"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@127.0.0.1.example.com:5432/postgres", "not_loopback", id="ip-lookalike"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@127.0.0.2:5432/postgres", "not_loopback", id="other-loopback-address"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@[::ffff:127.0.0.1]:5432/postgres", "not_loopback", id="mapped-ipv6"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@localhost.:5432/postgres", "not_loopback", id="trailing-dot"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@/postgres", "not_loopback", id="no-host-unix-socket"),
    # the rest of the URL
    pytest.param(f"postgresql://postgres:{PASSWORD}@localhost:55432/postgres", "wrong_driver", id="driver-default"),
    pytest.param(f"postgresql+psycopg2://postgres:{PASSWORD}@localhost:55432/postgres", "wrong_driver", id="driver-psycopg2"),
    pytest.param("sqlite:///house_helper.db", "wrong_driver", id="driver-sqlite"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@localhost/postgres", "port", id="port-missing"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@localhost:0/postgres", "port", id="port-zero"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@localhost:70000/postgres", "port", id="port-too-big"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@localhost:5432/postgres", "default_port", id="port-default"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@127.0.0.1:5432/postgres", "default_port", id="port-default-ipv4"),
    pytest.param(f"postgresql+psycopg://:{PASSWORD}@localhost:55432/postgres", "username", id="user-missing"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@localhost:55432", "database", id="database-missing"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@localhost:55432/post%67res", "database", id="database-escaped"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@localhost:55432/a-b", "database", id="database-hyphen"),
    pytest.param(f"postgresql+psycopg://postgres:{PASSWORD}@localhost:55432/9db", "database", id="database-digit-first"),
    pytest.param("this is not a url", "invalid_url", id="not-a-url"),
    pytest.param("", "not_configured", id="empty"),
    pytest.param("   ", "not_configured", id="blank"),
    pytest.param(None, "not_configured", id="unset"),
]


@pytest.mark.parametrize("raw, code", UNSAFE)
def test_an_unsafe_url_is_refused_before_any_connection_and_the_message_shows_no_value(
    raw, code, no_connections
):
    with pytest.raises(UnsafeTarget) as refused:
        validate_target(raw, {})

    assert refused.value.code == code
    message = str(refused.value)
    for value in (PASSWORD, REMOTE, "remote.example.com", "persistent", "production", "55432"):
        assert value not in message  # (the made-up secrets and the values of the rejected input)
    assert no_connections.calls == []


@pytest.mark.parametrize("name", sorted(ROUTING_ENV))
@pytest.mark.parametrize("spelling", [str.upper, str.lower], ids=["upper", "lower"])
def test_a_libpq_routing_variable_is_refused_before_any_connection(name, spelling, no_connections):
    environ = {POSTGRES_URL_ENV: BASE, spelling(name): REMOTE}

    with pytest.raises(UnsafeTarget) as refused:
        target_from_environment(environ)

    assert refused.value.code == "libpq_environment"
    assert spelling(name) in str(refused.value)  # which variable, so the operator can unset it ...
    assert REMOTE not in str(refused.value)  # ... never its value
    assert no_connections.calls == []


def test_the_process_environment_is_what_target_from_environment_reads(monkeypatch, no_connections):
    for name in list(os.environ):  # (whatever the machine running this has set)
        if name.upper() in ROUTING_ENV:
            monkeypatch.delenv(name)
    monkeypatch.setenv(POSTGRES_URL_ENV, BASE)
    monkeypatch.setenv("PGHOSTADDR", REMOTE)  # libpq would connect here although the URL says localhost
    with pytest.raises(UnsafeTarget) as refused:
        target_from_environment()
    assert refused.value.code == "libpq_environment"

    monkeypatch.delenv("PGHOSTADDR")
    assert target_from_environment().address == "127.0.0.1"
    assert no_connections.calls == []


@pytest.mark.parametrize("name", ["PGPASSWORD", "PGPASSFILE", "PGDATA"])
def test_variables_that_cannot_route_a_connection_are_not_refused(name):
    assert validate_target(BASE, {name: "x"}).address == "127.0.0.1"


def test_the_application_database_variable_is_never_the_test_target(no_connections):
    environ = {DATABASE_URL_ENV: BASE}  # a perfectly good PostgreSQL URL, in the WRONG variable

    with pytest.raises(UnsafeTarget) as refused:
        target_from_environment(environ)

    assert refused.value.code == "not_configured"
    assert no_connections.calls == []


def test_the_harness_never_names_the_application_database_variable():
    tree = ast.parse((ROOT / "tests" / "postgres_harness.py").read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]
    assert not [s for s in strings if "HOUSE_HELPER_DATABASE_URL" in s]
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not imported & {"DATABASE_URL_ENV", "database_url_from_environment"}


def test_run_from_environment_fails_the_test_without_opening_anything(no_connections):
    with pytest.raises(pytest.fail.Exception) as failed:
        run_from_environment({POSTGRES_URL_ENV: f"{BASE}?hostaddr={REMOTE}"})

    assert "refused [query_parameters]" in str(failed.value)
    assert PASSWORD not in str(failed.value) and REMOTE not in str(failed.value)
    assert run_from_environment({POSTGRES_URL_ENV: BASE}).target.address == "127.0.0.1"  # (this one is fine)
    assert no_connections.calls == []


# --- why `url.host` proves nothing, and what the driver is really given --------------------------------


def _dialect_params(url):
    return PGDialect_psycopg().create_connect_args(url)[1]


@pytest.mark.parametrize(
    "query, key, routed_to",
    [
        ("host=remote.example.com", "host", "remote.example.com"),
        (f"hostaddr={REMOTE}", "hostaddr", REMOTE),
        ("dbname=persistent", "dbname", "persistent"),
        ("service=production", "service", "production"),
        ("port=1", "port", "1"),
        ("user=other", "user", "other"),
    ],
)
def test_root_cause_the_dialect_lets_a_query_parameter_override_the_url(query, key, routed_to):
    url = make_url(f"{BASE}?{query}")

    assert url.host == "localhost" and url.database == "postgres"  # what a check of the URL's fields sees ...
    assert _dialect_params(url)[key] == routed_to  # ... and what the driver would be given


@pytest.mark.parametrize("host, address", VALID_HOSTS)
@pytest.mark.parametrize("password", [PASSWORD, None], ids=["password", "no-password"])
def test_a_generated_url_gives_the_driver_exactly_the_validated_parameters(host, address, password):
    credentials = f"postgres:{password}" if password else "postgres"
    target = validate_target(f"postgresql+psycopg://{credentials}@{host}:55432/postgres", {})
    run = Run(target, token=TOKEN)

    for text in (False, True):  # the URL object, and its text as the command line receives it
        url = target.url("house_helper_test_x")
        if text:
            url = make_url(url.render_as_string(hide_password=False))
        params = _dialect_params(url)
        check_connect_params(Expected(target, "house_helper_test_x", run.marker), params)
        assert (params["host"], params["hostaddr"], params["port"], params["dbname"]) == (
            address,
            address,
            55432,
            "house_helper_test_x",
        )
        assert params.get("password") == password


@pytest.mark.parametrize(
    "query",
    ["host=remote.example.com", f"hostaddr={REMOTE}", "dbname=persistent", "service=production", "port=1", "user=other", "options=-c%20x%3Dy"],
)
def test_check_connect_params_catches_a_tampered_url_even_if_validation_were_bypassed(query):
    target = validate_target(BASE, {})
    tampered = make_url(target.url("house_helper_test_x").render_as_string(hide_password=False) + "&" + query)

    with pytest.raises(UnsafeTarget) as refused:
        check_connect_params(Expected(target, "house_helper_test_x", "m"), _dialect_params(tampered))

    assert refused.value.code == "connect_parameters"
    assert PASSWORD not in str(refused.value) and REMOTE not in str(refused.value)


def test_check_connect_params_catches_another_password():
    target = validate_target(BASE, {})
    params = target.connect_params("x") | {"password": "another"}

    with pytest.raises(UnsafeTarget):
        check_connect_params(Expected(target, "x", None), params)


# --- a stand-in server ----------------------------------------------------------------------------------------


class Rows:
    def __init__(self, *row):
        self.row = row or None

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, server, params):
        self.server = server
        self.params = params
        self.closed = False
        self.statements = []
        self.database = server.redirect.get(params["dbname"], params["dbname"])

    @property
    def info(self):
        if self.server.peer_unknown:
            raise RuntimeError("the libpq in use cannot say (NotSupportedError)")
        return SimpleNamespace(
            hostaddr=self.params["hostaddr"] if self.server.peer is None else self.server.peer,
            port=self.params["port"] if self.server.port is None else self.server.port,
            dbname=self.database,
        )

    def execute(self, sql, params=None):
        server = self.server
        self.statements.append(sql)
        server.statements.append((self.database, sql))
        if server.fail_on and server.fail_on in sql:
            raise RuntimeError("simulated failure")
        databases = server.databases
        if sql == harness._IDENTITY_SQL:
            return Rows(server.reported_database or self.database, databases[self.database])
        if sql == harness._COMMENT_SQL:
            return Rows(databases[params[0]]) if params[0] in databases else Rows()
        if match := re.fullmatch(r'CREATE DATABASE "([^"]+)"(?: TEMPLATE "([^"]+)")?', sql):
            name, template = match.groups()
            if name in databases or (template is not None and template not in databases):
                raise RuntimeError("simulated: duplicate_database / no such template")
            databases[name] = None
        elif match := re.fullmatch(r"COMMENT ON DATABASE \"([^\"]+)\" IS '([^']*)'", sql):
            databases[match.group(1)] = match.group(2)
        elif match := re.fullmatch(r'ALTER DATABASE "([^"]+)" SET default_transaction_isolation = \'[a-z ]+\'', sql):
            assert match.group(1) in databases
        elif match := re.fullmatch(r'DROP DATABASE "([^"]+)" WITH \(FORCE\)', sql):
            del databases[match.group(1)]
        else:
            raise AssertionError("a statement the harness should not send")
        return Rows()

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class FakeServer:
    """What the harness can see of a PostgreSQL server, and the ways it can be made to lie."""

    def __init__(self, target):
        self.databases = {target.maintenance_database: None, "persistent": "somebody else's data"}
        self.statements = []  # (database the statement ran in, text)
        self.connections = []
        self.peer = None  # what the driver says the peer address is (None: the one asked for)
        self.port = None
        self.peer_unknown = False
        self.redirect = {}  # database asked for -> database really reached
        self.reported_database = None  # what current_database() says
        self.fail_on = None  # a statement containing this raises

    def connect(self, params):
        connection = FakeConnection(self, params)
        self.connections.append(connection)
        return connection

    def destructive(self):
        return [s for _, s in self.statements if s.split(" ", 1)[0] in ("CREATE", "COMMENT", "ALTER", "DROP")]


@pytest.fixture
def target():
    return validate_target(BASE, {})


@pytest.fixture
def server(target):
    return FakeServer(target)


@pytest.fixture
def run(target, server):
    return Run(target, connect=server.connect, token=TOKEN)


def test_a_run_opens_nothing_until_it_is_asked_to(target):
    tripwire = Tripwire()

    run = Run(target, connect=tripwire, token=TOKEN)

    assert tripwire.calls == [] and run.marker == harness.MARKER_PREFIX + TOKEN
    for ask in (
        lambda: run.url("house_helper_test_nothing_1"),
        lambda: run.engine("house_helper_test_nothing_1"),
        lambda: run.create_database(template="persistent"),
        lambda: run.create_database(isolation="whatever"),
        lambda: run.drop_database("persistent"),
    ):
        with pytest.raises((UnsafeTarget, ValueError)):
            ask()
    run.close()
    assert tripwire.calls == []


# --- a connection is verified before anything is run on it -------------------------------------------------


def test_every_connection_checks_where_it_is_before_it_runs_anything_else(run, server):
    template = run.create_database()
    clone = run.create_database(template=template, isolation="repeatable read")
    run.drop_database(clone)
    run.close()

    assert server.connections and all(c.closed for c in server.connections)
    assert all(c.statements[0] == harness._IDENTITY_SQL for c in server.connections)
    assert server.databases == {"postgres": None, "persistent": "somebody else's data"}  # nothing is left, nothing else touched


def test_the_databases_a_run_makes_are_named_and_marked_by_the_run(run, server):
    template = run.create_database()
    clone = run.create_database(template=template, isolation="repeatable read")

    assert re.fullmatch(rf"house_helper_test_{TOKEN}_[0-9]+", template)
    assert clone != template
    assert server.databases[template] == server.databases[clone] == run.marker
    assert [s for s in server.destructive() if s.startswith("CREATE")] == [
        f'CREATE DATABASE "{template}"',
        f'CREATE DATABASE "{clone}" TEMPLATE "{template}"',
    ]
    assert Run(validate_target(BASE, {}), connect=server.connect).marker != run.marker  # another run, another token


@pytest.mark.parametrize(
    "lie, code, statements_run",
    [
        pytest.param({"peer": REMOTE}, "wrong_peer", 0, id="remote-peer"),
        pytest.param({"peer": ""}, "wrong_peer", 0, id="unix-socket-peer"),
        pytest.param({"peer": "127.0.0.2"}, "wrong_peer", 0, id="other-loopback-peer"),
        pytest.param({"peer": "::1"}, "wrong_peer", 0, id="other-family-peer"),
        pytest.param({"port": 5432}, "wrong_peer", 0, id="other-port"),
        pytest.param({"peer_unknown": True}, "peer_unknown", 0, id="peer-cannot-be-named"),
        pytest.param({"redirect": {"postgres": "persistent"}}, "wrong_database", 0, id="driver-says-another-database"),
        pytest.param({"reported_database": "persistent"}, "wrong_database", 1, id="server-says-another-database"),
    ],
)
def test_a_connection_that_is_not_where_it_should_be_is_closed_and_nothing_destructive_runs(
    lie, code, statements_run, run, server
):
    for attribute, value in lie.items():
        setattr(server, attribute, value)

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()

    assert refused.value.code == code
    assert len(server.statements) == statements_run  # at most the one read-only question
    assert server.destructive() == []
    assert server.connections and all(c.closed for c in server.connections)
    assert set(server.databases) == {"postgres", "persistent"}
    run.close()  # (the same refusal, or nothing to do) and still nothing destroyed
    assert server.destructive() == [] and server.databases["persistent"] == "somebody else's data"


def test_verification_accepts_the_right_connection(run, server, target):
    connection = server.connect(target.connect_params("postgres"))

    verify_connection(connection, Expected(target, "postgres", None))  # (no exception)


@pytest.mark.parametrize("tamper", [lambda d, n, m: d.update({n: "somebody else's"}), lambda d, n, m: d.update({n: None})], ids=["foreign-token", "no-token"])
def test_a_scratch_database_that_is_not_this_runs_is_refused_before_a_migration(tamper, run, server, target):
    name = run.create_database()
    tamper(server.databases, name, run.marker)
    dialect = SimpleNamespace(connect=lambda **params: server.connect(params))
    connect = guarded_connect(Expected(target, name, run.marker))

    with pytest.raises(UnsafeTarget) as refused:
        connect(dialect, None, [], target.connect_params(name))  # what upgrade_database's connection goes through

    assert refused.value.code == "not_this_run"
    connection = server.connections[-1]
    assert connection.closed and connection.statements == [harness._IDENTITY_SQL]  # no migration, no DDL


def test_a_scratch_name_that_resolves_to_another_database_is_refused_before_a_migration(run, server, target):
    name = run.create_database()
    server.redirect[name] = "persistent"  # e.g. a service file or a parameter the checks missed
    dialect = SimpleNamespace(connect=lambda **params: server.connect(params))

    with pytest.raises(UnsafeTarget) as refused:
        guarded_connect(Expected(target, name, run.marker))(dialect, None, [], target.connect_params(name))

    assert refused.value.code == "wrong_database"
    assert server.connections[-1].closed and server.connections[-1].statements == []


def test_the_guard_refuses_parameters_that_were_not_validated_before_the_driver_is_called(run, target):
    called = Tripwire()
    dialect = SimpleNamespace(connect=called)
    params = target.connect_params("postgres") | {"hostaddr": REMOTE}

    with pytest.raises(UnsafeTarget):
        guarded_connect(Expected(target, "postgres", None))(dialect, None, [], params)

    assert called.calls == []


def test_a_guarded_engine_checks_its_connections_before_it_runs_a_statement(target):
    engine = sa.create_engine("sqlite://")  # not a PostgreSQL URL: its parameters are not the validated ones
    ran = []
    sa.event.listen(engine, "before_cursor_execute", lambda *args: ran.append(args[2]))
    guard_engine(engine, Expected(target, "postgres", None))

    with pytest.raises(UnsafeTarget) as refused:
        engine.connect()

    assert refused.value.code == "connect_parameters" and ran == []


def test_the_engine_of_a_run_is_guarded_and_made_from_the_pinned_url(run, monkeypatch):
    name = run.create_database()
    made = []

    def sqlite_instead(url_text):
        made.append(url_text)
        return sa.create_engine("sqlite://")

    monkeypatch.setattr(harness, "create_db_engine", sqlite_instead)

    engine = run.engine(name)

    assert make_url(made[0]) == run.target.url(name)  # built from validated fields only
    assert "hostaddr=127.0.0.1" in made[0] and f"/{name}?" in made[0]
    with pytest.raises(UnsafeTarget):  # the guard is on it (SQLite's parameters are not the validated ones)
        engine.connect()


# --- cleanup: only what this run created, and still marked with its token ---------------------------------


def test_close_drops_what_the_run_made_and_nothing_else(run, server):
    server.databases["house_helper_test_" + "f" * 32 + "_1"] = harness.MARKER_PREFIX + "f" * 32  # another run's
    template = run.create_database()
    clones = [run.create_database(template=template) for _ in range(2)]

    run.close()

    assert set(server.databases) == {"postgres", "persistent", "house_helper_test_" + "f" * 32 + "_1"}
    assert sorted(s for s in server.destructive() if s.startswith("DROP")) == sorted(
        f'DROP DATABASE "{name}" WITH (FORCE)' for name in [template, *clones]
    )
    run.close()  # idempotent


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("persistent", id="an-existing-application-database"),
        pytest.param("postgres", id="the-maintenance-database"),
        pytest.param("house_helper_test_" + "f" * 32 + "_1", id="another-runs-scratch-database"),
        pytest.param(f"house_helper_test_{TOKEN}_7", id="a-name-of-this-run-that-it-never-created"),
        pytest.param('x"; DROP DATABASE persistent; --', id="injection"),
    ],
)
def test_a_database_this_run_did_not_create_is_never_dropped(name, run, server):
    server.databases[name] = harness.MARKER_PREFIX + TOKEN  # even one that carries this run's token
    before = dict(server.databases)

    with pytest.raises(UnsafeTarget) as refused:
        run.drop_database(name)

    assert refused.value.code == "not_created_by_this_run"
    assert server.databases == before and server.statements == []


def test_a_database_whose_token_was_replaced_is_not_dropped_and_the_others_still_are(run, server):
    first, second = run.create_database(), run.create_database()
    server.databases[first] = "somebody else's now"

    with pytest.raises(UnsafeTarget) as refused:
        run.close()

    assert refused.value.code == "not_this_run"
    assert first in server.databases and second not in server.databases  # the other one went


def test_a_database_that_failed_to_be_marked_is_still_cleaned_up(run, server):
    server.fail_on = "COMMENT ON"  # created, then the marking statement fails
    with pytest.raises(RuntimeError):
        run.create_database()
    name = next(n for n in server.databases if n.startswith("house_helper_test_"))
    assert server.databases[name] is None
    server.fail_on = None

    run.close()

    assert name not in server.databases and set(server.databases) == {"postgres", "persistent"}


def test_a_database_that_already_existed_under_the_name_is_not_ours_to_drop(run, server):
    existing = f"house_helper_test_{TOKEN}_1"
    server.databases[existing] = None  # the name the run is about to generate, taken by someone else
    with pytest.raises(RuntimeError):
        run.create_database()  # CREATE DATABASE fails: it exists

    run.close()

    assert existing in server.databases
    assert not [s for s in server.destructive() if s.startswith(("DROP", "COMMENT"))]


def test_a_failed_create_leaves_nothing_registered(run, server):
    server.fail_on = "CREATE DATABASE"
    with pytest.raises(RuntimeError):
        run.create_database()
    server.fail_on = None

    run.close()

    assert set(server.databases) == {"postgres", "persistent"}
    assert [s for s in server.destructive() if not s.startswith("CREATE")] == []  # (the failed attempt itself aside)


def test_cleanup_refuses_to_drop_through_a_connection_that_is_not_where_it_should_be(run, server):
    name = run.create_database()
    server.peer = REMOTE  # from now on the connection goes somewhere else

    with pytest.raises(UnsafeTarget) as refused:
        run.close()

    assert refused.value.code == "wrong_peer"
    assert not [s for s in server.destructive() if s.startswith("DROP")]
    assert name in server.databases


def test_a_fixture_whose_work_fails_verification_still_gets_its_database_dropped(run, server, target):
    """The shape of the module's `template` fixture: create the database, then its first
    connection (the migrations') is refused. Cleanup goes through its own verified
    connection to the maintenance database, so the database is still dropped."""
    name = run.create_database()
    server.redirect[name] = "persistent"  # the scratch connection lands in another database; the control one does not
    dialect = SimpleNamespace(connect=lambda **params: server.connect(params))
    with pytest.raises(UnsafeTarget) as refused:
        guarded_connect(Expected(target, name, run.marker))(dialect, None, [], target.connect_params(name))
    assert refused.value.code == "wrong_database"

    run.close()

    assert name not in server.databases and server.databases["persistent"] == "somebody else's data"


# --- output that must not show a secret ------------------------------------------------------------------------


def test_leaked_names_a_password_that_appears_and_only_names_it():
    url = make_url(f"postgresql+psycopg://postgres:{PASSWORD}@127.0.0.1:55432/scratch_db")

    assert leaked(url, f"could not connect with {PASSWORD}") == ["password"]
    assert leaked(url, "the database scratch_db is gone") == ["database"]
    assert leaked(url, url.render_as_string(hide_password=False)) == ["password", "database", "url"]
    assert leaked(url, "nothing to see") == []


@pytest.mark.parametrize("userinfo", ["postgres", "postgres:"], ids=["no-password", "empty-password"])
def test_leaked_works_for_a_url_without_a_password(userinfo):
    url = make_url(f"postgresql+psycopg://{userinfo}@127.0.0.1:55432/scratch_db")

    assert leaked(url, "any output, even with a colon: and an empty string in it") == []  # no TypeError, no false alarm
    assert leaked(url, "scratch_db") == ["database"]
    assert url.password in (None, "")


def test_nothing_shown_of_a_target_or_a_refusal_contains_the_password(target):
    shown = " ".join([repr(target), str(target.url("x")), repr(target.url("x")), repr(UnsafeTarget("c", "m"))])

    assert PASSWORD not in shown


# --- end to end: the module itself, with an unsafe configuration -------------------------------------------


def _environment_without_postgres_settings():
    return {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("PG", "HOUSE_HELPER_"))
    }


@pytest.mark.parametrize(
    "extra, refusal",
    [
        pytest.param({POSTGRES_URL_ENV: f"{BASE}?hostaddr={REMOTE}"}, "refused [query_parameters]", id="query-parameter"),
        pytest.param({POSTGRES_URL_ENV: BASE, "PGHOSTADDR": REMOTE}, "refused [libpq_environment]", id="libpq-variable"),
    ],
)
def test_the_postgres_test_module_refuses_an_unsafe_configuration_at_setup(extra, refusal):
    environment = _environment_without_postgres_settings() | extra

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_admin_postgres.py", "-q", "-p", "no:cacheprovider", "--tb=short"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )

    output = done.stdout + done.stderr
    assert done.returncode != 0
    assert refusal in output
    assert " passed" not in output  # no test ran against anything
    for value in (PASSWORD, REMOTE):
        assert value not in output  # no password, no value of the rejected input
    assert "psycopg" not in output  # the driver was never even asked to connect
