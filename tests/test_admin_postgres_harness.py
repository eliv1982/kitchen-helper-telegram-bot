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
from tests import postgres_identity as identity
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

# R3-01: what the container's control file says, and the container the harness inspects.
CLUSTER_IDENTIFIER = "7000000000000000001"
OTHER_IDENTIFIER = "7000000000000000002"
CONTAINER = "a" * 64


class _IdentityTarget:
    """The two fields the identity contract reads from a harness Target."""

    port = 55432
    address = "127.0.0.1"


def cluster_identity() -> identity.ClusterIdentity:
    """A genuinely established identity (R4-01 seals it at creation). The fake Docker
    answers the documented Compose facts, so the run treats this exactly as it treats the
    identity of a real, verified cluster."""
    return identity.establish(_IdentityTarget(), environ={}, docker=FakeDocker())


class FakeDocker:
    """Stands in for the Docker CLI. `inspect` answers with the Compose facts; every
    disagreement the identity contract can refuse is a constructor argument, so an
    adversarial test says exactly what lies. The facts are the fields Docker actually
    returns (R4-04): `Id`, `Config.Image`, `State`, `Mounts`, `NetworkSettings.Ports` -
    a container inspection has no `RepoDigests` field, so none is faked."""

    def __init__(
        self,
        *,
        compose=None,
        identifier=CLUSTER_IDENTIFIER,
        image=identity.PINNED_IMAGE,
        volume=identity.DATA_VOLUME,
        volume_writable=True,
        labels=None,
        host_ip="127.0.0.1",
        port=55432,
        running=True,
        inspect_fails=False,
        control_fails=False,
        inspected_id=None,
        volume_labels=None,  # None: derive from the container's Compose provenance
        shared_with=(),
        volume_name=None,  # override the volume's own Name (the mount names something else)
    ):
        self.compose = [CONTAINER] if compose is None else list(compose)
        self.identifier = identifier
        self.image = image
        self.data_volume = volume
        self.volume_writable = volume_writable
        self.labels = (
            {
                "com.docker.compose.project": identity.COMPOSE_PROJECT,
                "com.docker.compose.service": identity.COMPOSE_SERVICE,
                "com.docker.compose.oneoff": "False",
            }
            if labels is None
            else dict(labels)
        )
        self.host_ip = host_ip
        self.port = port
        self.running = running
        self.inspect_fails = inspect_fails
        self.control_fails = control_fails
        self.inspected_id = CONTAINER if inspected_id is None else inspected_id
        self.inspections = []
        # R5-04: the volume's own record. `None` derives Compose provenance from the
        # container labels (which is what a real Compose-created volume carries).
        project = self.labels.get(identity.COMPOSE_PROJECT_LABEL)
        self.volume_labels = (
            (
                {
                    identity.COMPOSE_PROJECT_LABEL: project,
                    identity.COMPOSE_VOLUME_LABEL: self.data_volume,
                }
                if project
                else {}
            )
            if volume_labels is None
            else dict(volume_labels)
        )
        self.shared_with = list(shared_with)
        self.volume_name = volume_name

    def compose_container_ids(self):
        return list(self.compose)

    def inspect(self, container_id):
        self.inspections.append(container_id)
        if self.inspect_fails:
            raise identity.IdentityError(
                "cluster_identity_unavailable", "simulated: the container cannot be inspected."
            )
        if container_id not in self.compose:
            raise identity.IdentityError(
                "cluster_identity_unavailable", "simulated: no such container."
            )
        return {
            "Id": self.inspected_id,
            "State": {"Running": self.running, "Status": "running" if self.running else "exited"},
            "Config": {"Image": self.image, "Labels": dict(self.labels)},
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": self.data_volume,
                    "Destination": identity.DATA_PATH,
                    "RW": self.volume_writable,
                }
            ],
            "NetworkSettings": {
                "Ports": {
                    identity.DATABASE_PORT: [
                        {"HostIp": self.host_ip, "HostPort": str(self.port)}
                    ]
                }
            },
        }

    def volume(self, name):
        """R5-04: the volume's own record, with the provenance labels a real Compose
        project's volume carries (or none for a manually created / CI volume)."""
        if self.inspect_fails:
            raise identity.IdentityError(
                "cluster_identity_unavailable", "simulated: the volume cannot be inspected."
            )
        return {
            "Name": name if self.volume_name is None else self.volume_name,
            "Driver": "local",
            "Scope": "local",
            "Labels": dict(self.volume_labels),
            "Mountpoint": f"/var/lib/docker/volumes/{name}/_data",
        }

    def volume_containers(self, name):
        if self.inspect_fails:
            raise identity.IdentityError(
                "cluster_identity_unavailable", "simulated: the volume cannot be inspected."
            )
        return [self.inspected_id, *self.shared_with]

    def system_identifier(self, container_id):
        if self.control_fails:
            raise identity.IdentityError(
                "cluster_identity_unavailable",
                "simulated: the control file cannot be read.",
            )
        if container_id not in self.compose:
            raise identity.IdentityError(
                "cluster_identity_unavailable", "simulated: no such container."
            )
        return self.identifier


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
        check_connect_params(Expected(target, "house_helper_test_x", run.marker, CLUSTER_IDENTIFIER), params)
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
        check_connect_params(Expected(target, "house_helper_test_x", "m", CLUSTER_IDENTIFIER), _dialect_params(tampered))

    assert refused.value.code == "connect_parameters"
    assert PASSWORD not in str(refused.value) and REMOTE not in str(refused.value)


def test_check_connect_params_catches_another_password():
    target = validate_target(BASE, {})
    params = target.connect_params("x") | {"password": "another"}

    with pytest.raises(UnsafeTarget):
        check_connect_params(Expected(target, "x", None, CLUSTER_IDENTIFIER), params)


# --- a stand-in server ----------------------------------------------------------------------------------------


class Rows:
    def __init__(self, *row):
        self.row = row or None

    def fetchone(self):
        return self.row

    def __iter__(self):
        return iter(() if self.row is None else (self.row,))


class Result:
    """What a query that returns several rows answers in the fake (only `fetchall`)."""

    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class SimulatedDatabaseError(RuntimeError):
    """A failure shaped like a psycopg error: the harness reads `sqlstate`, exactly as it
    would from `psycopg.errors.DuplicateDatabase` (R5-05)."""

    def __init__(self, message, sqlstate):
        super().__init__(message)
        self.sqlstate = sqlstate


class FakeConnection:
    def __init__(self, server, params):
        self.server = server
        self.params = params
        self.user = params.get("user")
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
        if server.should_fail(sql):
            raise RuntimeError("simulated failure")
        databases = server.databases
        # --- the cluster's persistent identifier (R3-01) ------------------------------
        if sql == identity.SYSTEM_IDENTIFIER_SQL:
            if server.identifier_unavailable:
                raise RuntimeError("simulated: the control file cannot be read")
            return Rows(server.system_identifier)
        # --- identity and ownership (R2-06) -------------------------------------------
        if sql == harness._IDENTITY_SQL:
            reached = server.reported_database or self.database
            return Rows(reached, databases[reached], server.owner(reached))
        if sql == harness._COMMENT_AND_OWNER_SQL:
            name = params[0]
            return Rows(databases[name], server.owner(name)) if name in databases else Rows()
        if sql == harness._ROLE_SQL:
            return Rows(self.user, server.current_user(self.user))
        # --- roles (R2-05/R2-06) ------------------------------------------------------
        if sql == "SELECT rolsuper FROM pg_roles WHERE rolname = current_user":
            return Rows(True)
        if sql.startswith("SELECT 1 FROM pg_roles WHERE rolname = %s"):
            return Rows(1) if params[0] in server.roles else Rows()
        if match := re.fullmatch(r'CREATE ROLE "([^"]+)"(?: LOGIN PASSWORD \'[^\']*\')?(?: NOLOGIN)?(.*)', sql):
            server.roles[match.group(1)] = match.group(2)
        elif match := re.fullmatch(r'GRANT "([^"]+)" TO "([^"]+)" WITH SET TRUE, INHERIT FALSE', sql):
            server.memberships.append((match.group(2), match.group(1)))
        elif match := re.fullmatch(r'ALTER ROLE "[^"]+" SET .+', sql):
            pass
        elif match := re.fullmatch(r'DROP ROLE "([^"]+)"', sql):
            name = match.group(1)
            if name not in server.roles:  # only a role this server has was dropped
                raise RuntimeError("simulated: no such role")
            # R4-06: PostgreSQL refuses DROP ROLE while privileges are still granted TO the
            # role (pg_shdepend). The maintenance CONNECT grant is exactly that, and it is
            # why cleanup revokes grants before dropping roles.
            if any(role == name for role, _database in server.maintenance_grants):
                raise RuntimeError(
                    "simulated: role cannot be dropped because privileges depend on it"
                )
            del server.roles[name]
            server.dropped_roles.append(name)
        # --- the dedicated-cluster evidence (R2-06) -----------------------------------
        elif sql.startswith("SELECT current_user, (SELECT rolsuper"):
            return Rows(self.user, True, server.owner(self.database), None)
        elif sql.startswith(
            "SELECT datname, shobj_description(oid, 'pg_database'), pg_get_userbyid(datdba) "
            "FROM pg_database WHERE datname NOT IN"
        ):
            # R5-04 item 8: the inventory check now sees the actual owner and marker.
            return Result(
                [
                    (name, comment, server.owner(name))
                    for name, comment in databases.items()
                    if name not in ("postgres", "template0", "template1")
                ]
            )
        # --- the maintenance-database fence (R2-06) -----------------------------------
        elif re.fullmatch(r'REVOKE ALL ON DATABASE "[^"]+" FROM PUBLIC', sql):
            pass
        elif match := re.fullmatch(r'GRANT CONNECT ON DATABASE "([^"]+)" TO "([^"]+)"', sql):
            server.maintenance_grants.append((match.group(2), match.group(1)))
        elif match := re.fullmatch(r'REVOKE CONNECT ON DATABASE "([^"]+)" FROM "([^"]+)"', sql):
            grant = (match.group(2), match.group(1))
            if grant not in server.maintenance_grants:
                raise RuntimeError("simulated: no such grant")
            server.maintenance_grants.remove(grant)
        elif sql.startswith("SELECT count(*) FROM aclexplode("):
            return Rows(0)
        elif sql.startswith("SELECT rolname, has_database_privilege"):
            names = params[1]
            return Result([(name, server.maintenance_connect(name)) for name in names])
        # --- databases ----------------------------------------------------------------
        elif match := re.fullmatch(r'CREATE DATABASE "([^"]+)"(?: TEMPLATE "([^"]+)")?(?: OWNER "([^"]+)")?(?: ALLOW_CONNECTIONS false)?', sql):
            name, template, owner = match.groups()
            if name in databases:
                # R5-05: a real psycopg `DuplicateDatabase` carries SQLSTATE 42P04; that is
                # the one failure proving nothing was created.
                raise SimulatedDatabaseError("simulated: duplicate_database", "42P04")
            if template is not None and template not in databases:
                raise SimulatedDatabaseError("simulated: no such template", "3D000")
            databases[name] = None
            if owner is not None:
                server.owners[name] = owner
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
        self.target = target
        # R4-02: a dedicated, disposable cluster holds nothing but the maintenance database
        # (and this harness's own scratch databases, which the server adds as they appear).
        self.databases = {target.maintenance_database: None}
        self.owners: dict[str, str] = {}  # datname -> pg_database.datdba (default: the bootstrap user)
        self.roles: dict[str, str] = {}
        self.memberships: list[tuple[str, str]] = []
        self.dropped_roles: list[str] = []
        self.statements = []  # (database the statement ran in, text)
        self.connections = []
        self.peer = None  # what the driver says the peer address is (None: the one asked for)
        self.port = None
        self.peer_unknown = False
        self.redirect = {}  # database asked for -> database really reached
        self.reported_database = None  # what current_database() says
        self.fail_on = None  # a statement containing this raises
        self.fail_when = None  # a callable(sql) -> bool (R3-02 failure injection)
        # R3-01: what the SQL connection reports, and the grants cleanup must revoke.
        self.system_identifier = CLUSTER_IDENTIFIER
        self.identifier_unavailable = False
        self.maintenance_grants: list[tuple[str, str]] = []  # (role, database)
        self.docker = FakeDocker()

    def should_fail(self, sql) -> bool:
        if self.fail_on and self.fail_on in sql:
            return True
        return bool(self.fail_when and self.fail_when(sql))

    def owner(self, database):
        return self.owners.get(database, self.target.username)

    @staticmethod
    def current_user(user):
        """`current_user` after the harness's role settings: the migration login assumes
        the NOLOGIN owner, every other login is itself."""
        return user[: -len("migrate")] + "owner" if user.endswith("_migrate") else user

    @staticmethod
    def maintenance_connect(role):
        """Only the migration and restore logins are handed CONNECT on the maintenance database."""
        return role.endswith(("_migrate", "_restore"))

    def connect(self, params):
        connection = FakeConnection(self, params)
        self.connections.append(connection)
        return connection

    def destructive(self):
        return [s for _, s in self.statements if s.split(" ", 1)[0] in ("CREATE", "COMMENT", "ALTER", "DROP")]

    def mutating(self):
        """Every statement that changes the cluster: databases, roles, ACLs (R3-01)."""
        return [
            s
            for _, s in self.statements
            if s.split(" ", 1)[0] in ("CREATE", "COMMENT", "ALTER", "DROP", "GRANT", "REVOKE")
        ]


@pytest.fixture
def target():
    return validate_target(BASE, {})


@pytest.fixture
def server(target):
    return FakeServer(target)


@pytest.fixture
def run(target, server):
    # R3-01: the fixture's run has already been given the independently verified identity
    # the fake Docker and the fake server agree on. A test that wants to see the *refusal*
    # builds its own Run without one (or with a lying FakeDocker).
    return Run(
        target,
        connect=server.connect,
        token=TOKEN,
        cluster=cluster_identity(),
        docker=server.docker,
    )


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
    assert server.databases == {"postgres": None}  # nothing is left, nothing else touched


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
    # Somebody else's data, in a database this harness must never touch (the refusal
    # below always precedes any inventory or creation statement).
    server.databases["persistent"] = "somebody else's data"
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

    verify_connection(connection, Expected(target, "postgres", None, CLUSTER_IDENTIFIER))  # (no exception)


@pytest.mark.parametrize("tamper", [lambda d, n, m: d.update({n: "somebody else's"}), lambda d, n, m: d.update({n: None})], ids=["foreign-token", "no-token"])
def test_a_scratch_database_that_is_not_this_runs_is_refused_before_a_migration(tamper, run, server, target):
    name = run.create_database()
    tamper(server.databases, name, run.marker)
    dialect = SimpleNamespace(connect=lambda **params: server.connect(params))
    connect = guarded_connect(Expected(target, name, run.marker, CLUSTER_IDENTIFIER))

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
        guarded_connect(Expected(target, name, run.marker, CLUSTER_IDENTIFIER))(dialect, None, [], target.connect_params(name))

    assert refused.value.code == "wrong_database"
    assert server.connections[-1].closed and server.connections[-1].statements == []


def test_the_guard_refuses_parameters_that_were_not_validated_before_the_driver_is_called(run, target):
    called = Tripwire()
    dialect = SimpleNamespace(connect=called)
    params = target.connect_params("postgres") | {"hostaddr": REMOTE}

    with pytest.raises(UnsafeTarget):
        guarded_connect(Expected(target, "postgres", None, CLUSTER_IDENTIFIER))(dialect, None, [], params)

    assert called.calls == []


def test_a_guarded_engine_checks_its_connections_before_it_runs_a_statement(target):
    engine = sa.create_engine("sqlite://")  # not a PostgreSQL URL: its parameters are not the validated ones
    ran = []
    sa.event.listen(engine, "before_cursor_execute", lambda *args: ran.append(args[2]))
    guard_engine(engine, Expected(target, "postgres", None, CLUSTER_IDENTIFIER))

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

    assert set(server.databases) == {"postgres", "house_helper_test_" + "f" * 32 + "_1"}
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

    assert name not in server.databases and set(server.databases) == {"postgres"}


def test_a_database_that_already_existed_under_the_name_is_not_ours_to_drop(run, server):
    """R5-04 item 8 strengthens this: a scratch-LOOKING name with no marker is now refused
    by the inventory check before CREATE is even attempted, and the pre-existing database
    is untouched either way."""
    existing = f"house_helper_test_{TOKEN}_1"
    server.databases[existing] = None  # the name the run is about to generate, taken by someone else
    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()  # refused: no marker + no per-run owner

    assert refused.value.code == "not_dedicated"
    run.close()

    assert existing in server.databases
    assert not [s for s in server.destructive() if s.startswith(("DROP", "COMMENT"))]


def test_a_failed_create_leaves_nothing_registered(run, server):
    server.fail_on = "CREATE DATABASE"
    with pytest.raises(RuntimeError):
        run.create_database()
    server.fail_on = None

    run.close()

    assert set(server.databases) == {"postgres"}
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
    server.databases["persistent"] = "somebody else's data"  # (created after this run's own database)
    server.redirect[name] = "persistent"  # the scratch connection lands in another database; the control one does not
    dialect = SimpleNamespace(connect=lambda **params: server.connect(params))
    with pytest.raises(UnsafeTarget) as refused:
        guarded_connect(Expected(target, name, run.marker, CLUSTER_IDENTIFIER))(dialect, None, [], target.connect_params(name))
    assert refused.value.code == "wrong_database"

    run.close()

    assert name not in server.databases and server.databases["persistent"] == "somebody else's data"


# --- the ownership fence, and partial preparation (R2-05, R2-06) ----------------------------------------------


def test_a_database_that_carries_the_token_but_not_the_owner_is_refused(run, server, target):
    """R2-06: the COMMENT is not ownership. `pg_database.datdba` must still name the role
    the database was created for, or the connection (and the cleanup) refuses."""
    name = run.create_database()
    server.owners[name] = "somebody-else"
    dialect = SimpleNamespace(connect=lambda **params: server.connect(params))

    with pytest.raises(UnsafeTarget) as refused:
        guarded_connect(Expected(target, name, run.marker, CLUSTER_IDENTIFIER))(dialect, None, [], target.connect_params(name))
    assert refused.value.code == "wrong_owner"

    with pytest.raises(UnsafeTarget) as dropped:
        run.drop_database(name)
    assert dropped.value.code == "not_this_run"
    assert name in server.databases  # nothing was destroyed


def test_a_partially_prepared_role_set_cannot_escape_cleanup(target, server):
    """R2-05: the membership grant fails after two roles exist. The failure itself must run
    the ownership-verified cleanup: nothing this run created may survive it."""
    server.databases.pop("persistent", None)  # the dedicated-cluster evidence sees only this run's databases
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    server.fail_on = 'GRANT "'

    with pytest.raises(RuntimeError):
        run.prepare_roles()

    assert server.roles == {}  # the two roles created before the failure are gone
    assert sorted(server.dropped_roles) == sorted(
        f"hh_t_{TOKEN}_{key}" for key in ("owner", "migrate")
    )
    assert all(harness.ROLE_NAME.fullmatch(name) for name in server.dropped_roles)


def test_cluster_wide_role_preparation_refuses_a_server_that_is_not_dedicated(target, server):
    server.databases["house_helper"] = None  # a database this harness never created
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)

    with pytest.raises(UnsafeTarget) as refused:
        run.prepare_roles()

    assert refused.value.code == "not_dedicated"
    assert server.roles == {} and server.destructive() == []


# --- R3-01: nothing destructive without an independently verified cluster -----------------------------


def test_no_database_is_created_without_an_independently_verified_cluster(target, server):
    """The old evidence (superuser, ownership, an empty inventory, the optional setting)
    is all present in this fake server; the identity is not. Nothing destructive runs."""
    server.databases.pop("persistent", None)  # a pristine-looking, empty local cluster
    run = Run(target, connect=server.connect, token=TOKEN, docker=FakeDocker(compose=()), environ={})

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()

    assert refused.value.code == "cluster_identity_missing"
    assert server.statements == [] and server.mutating() == []
    with pytest.raises(UnsafeTarget):  # the same before any role is created
        run.prepare_roles()
    assert server.statements == [] and server.roles == {}


def test_a_direct_create_database_call_cannot_bypass_the_identity_gate(target, server):
    """`create_database()` is public; there is no unguarded path left to it."""
    run = Run(target, connect=server.connect, token=TOKEN, docker=FakeDocker(compose=[]), environ={})

    for attempt in (lambda: run.create_database(), lambda: run.prepare_roles()):
        with pytest.raises(UnsafeTarget) as refused:
            attempt()

        assert refused.value.code == "cluster_identity_missing"
    assert server.mutating() == [] and server.connections == []


@pytest.mark.parametrize(
    "lie, code",
    [
        pytest.param({"compose": []}, "cluster_identity_missing", id="no-container"),
        pytest.param({"compose": [CONTAINER, "b" * 64]}, "cluster_identity_ambiguous", id="two-containers"),
        pytest.param({"inspect_fails": True}, "cluster_identity_unavailable", id="inspect-fails"),
        pytest.param({"control_fails": True}, "cluster_identity_unavailable", id="control-file-unreadable"),
        pytest.param({"identifier": "not-a-number"}, "cluster_identity_unavailable", id="identifier-not-a-number"),
        pytest.param({"running": False}, "cluster_identity_unavailable", id="container-stopped"),
        pytest.param({"image": "postgres@sha256:" + "0" * 64}, "cluster_identity_mismatch", id="other-image"),
        pytest.param({"volume": "someone-elses-volume"}, "cluster_identity_mismatch", id="other-volume"),
        pytest.param(
            {"labels": {"com.docker.compose.project": "another-project"}},
            "cluster_identity_mismatch",
            id="other-compose-project",
        ),
        pytest.param(
            {
                "labels": {
                    "com.docker.compose.project": identity.COMPOSE_PROJECT,
                    "com.docker.compose.service": "web",
                }
            },
            "cluster_identity_mismatch",
            id="other-compose-service",
        ),
        pytest.param(
            {
                "labels": {
                    "com.docker.compose.project": identity.COMPOSE_PROJECT,
                    "com.docker.compose.service": identity.COMPOSE_SERVICE,
                    "com.docker.compose.oneoff": "True",
                }
            },
            "cluster_identity_mismatch",
            id="one-off-container",
        ),
        pytest.param({"host_ip": "0.0.0.0"}, "cluster_identity_mismatch", id="published-to-the-world"),
        pytest.param({"port": 55433}, "cluster_identity_mismatch", id="another-host-port"),
    ],
)
def test_a_container_that_disagrees_with_the_contract_blocks_every_destructive_call(
    lie, code, target, server
):
    run = Run(target, connect=server.connect, token=TOKEN, docker=FakeDocker(**lie), environ={})

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()

    assert refused.value.code == code
    assert server.mutating() == [] and server.connections == []


def test_ci_must_name_the_runner_supplied_service_container(target, server):
    run = Run(
        target,
        connect=server.connect,
        token=TOKEN,
        docker=FakeDocker(),
        environ={identity.GITHUB_ACTIONS_ENV: "true"},
    )

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()

    assert refused.value.code == "cluster_identity_missing"
    assert server.mutating() == []


CI_IDENTITY_ENVIRON = {
    identity.GITHUB_ACTIONS_ENV: "true",
    identity.CONTAINER_ID_ENV: CONTAINER,
    identity.RUN_ID_ENV: "1234567890",
    identity.RUN_ATTEMPT_ENV: "2",
}


def test_the_runner_supplied_service_container_is_accepted_through_the_same_contract(target, server):
    environ = dict(CI_IDENTITY_ENVIRON)
    volume = identity.ci_volume(environ)  # R4-02: the run-scoped volume the workflow mounts
    docker = FakeDocker(labels={}, volume=volume)  # a service container has no Compose labels
    run = Run(
        target,
        connect=server.connect,
        token=TOKEN,
        docker=docker,
        environ=environ,
    )

    name = run.create_database()
    run.drop_database(name)

    assert docker.inspections[0] == CONTAINER  # established once, from the runner-named container
    mutations = server.mutating()
    assert mutations[0] == f'CREATE DATABASE "{name}"'
    assert mutations[-1] == f'DROP DATABASE "{name}" WITH (FORCE)'
    assert [s for s in mutations if s.startswith(("GRANT", "REVOKE", "CREATE ROLE", "DROP ROLE"))] == []


def test_a_container_id_alone_does_not_select_the_ci_contract(target, server):
    """R4-02: locally, the id is conflicting evidence, never a weaker contract."""
    inspected = FakeDocker()

    run = Run(
        target,
        connect=server.connect,
        token=TOKEN,
        docker=inspected,
        environ={identity.CONTAINER_ID_ENV: CONTAINER},
    )

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()

    assert refused.value.code == "cluster_identity_conflict"
    assert inspected.inspections == [] and server.mutating() == [] and server.connections == []


def test_a_sql_connection_to_another_cluster_is_refused_before_any_statement_of_consequence(target, server):
    """The container is the verified one, but the connection reaches a different cluster:
    the independently established identifier wins and nothing destructive is sent."""
    server.system_identifier = OTHER_IDENTIFIER
    run = Run(target, connect=server.connect, token=TOKEN, docker=FakeDocker(), environ={})

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()

    assert refused.value.code == "cluster_identity_changed"
    # Only the two read-only identity questions were asked; nothing of consequence ran.
    assert {sql for _, sql in server.statements} == {
        harness._IDENTITY_SQL,
        identity.SYSTEM_IDENTIFIER_SQL,
    }
    assert server.mutating() == []


def test_an_identifier_the_connection_cannot_answer_is_refused(target, server):
    server.identifier_unavailable = True
    run = Run(target, connect=server.connect, token=TOKEN, docker=FakeDocker(), environ={})

    with pytest.raises(RuntimeError):  # the fake's read fails; nothing is destroyed or created
        run.create_database()

    assert server.mutating() == []


def test_cleanup_refuses_state_created_without_a_verified_cluster(target, server):
    """The unguarded-cleanup bypass: recorded state whose identity was never proved is
    not dropped on a guess."""
    run = Run(target, connect=server.connect, token=TOKEN, docker=FakeDocker(compose=()), environ={})
    run._databases["house_helper_test_" + TOKEN + "_1"] = "marked"  # planted record

    with pytest.raises(UnsafeTarget) as refused:
        run.close()

    assert refused.value.code == "cluster_identity_missing"
    assert server.statements == [] and server.mutating() == []
    assert run.pending_cleanup()["databases"] == ("house_helper_test_" + TOKEN + "_1",)


def test_a_cluster_change_between_setup_and_cleanup_refuses_the_drop_and_keeps_the_record(run, server):
    name = run.create_database()
    server.system_identifier = OTHER_IDENTIFIER  # the port now reaches another cluster

    with pytest.raises(UnsafeTarget) as refused:
        run.close()

    assert refused.value.code == "cluster_identity_changed"
    assert not [s for s in server.mutating() if s.startswith("DROP")]
    assert name in server.databases and name in run.pending_cleanup()["databases"]
    server.system_identifier = CLUSTER_IDENTIFIER  # the same cluster again: the retry works
    run.close()
    assert name not in server.databases


def test_a_replaced_container_between_setup_and_cleanup_refuses_the_drop(run, server):
    name = run.create_database()
    server.docker.identifier = OTHER_IDENTIFIER  # `pg_controldata` now names another cluster

    with pytest.raises(UnsafeTarget) as refused:
        run.close()

    assert refused.value.code == "cluster_identity_changed"
    assert not [s for s in server.mutating() if s.startswith("DROP")]
    assert name in run.pending_cleanup()["databases"]
    server.docker.identifier = CLUSTER_IDENTIFIER
    run.close()
    assert name not in server.databases


# --- R4-01: the one privileged-operation boundary ------------------------------------------------


def test_control_before_verification_hands_out_nothing(target, server):
    """The demonstrated bypass: `Run.control()` used to open a bootstrap connection before
    any identity existed. It now refuses, and no connection is opened at all."""
    run = Run(target, connect=server.connect, token=TOKEN, docker=FakeDocker(compose=()), environ={})

    with pytest.raises(UnsafeTarget) as refused:
        with run.control():
            pytest.fail("a privileged connection was handed out before verification")

    assert refused.value.code == "cluster_identity_missing"
    assert server.connections == [] and server.statements == []


@pytest.mark.parametrize("attempt", ["control", "create", "roles", "restore-probe"])
def test_an_unavailable_container_produces_zero_mutations(attempt, target, server):
    run = Run(target, connect=server.connect, token=TOKEN, docker=FakeDocker(inspect_fails=True), environ={})

    with pytest.raises(UnsafeTarget) as refused:
        if attempt == "control":
            with run.control():
                pytest.fail("a privileged connection was handed out")
        elif attempt == "create":
            run.create_database()
        elif attempt == "roles":
            run.prepare_roles()
        else:
            run.create_database_as("restore")

    assert refused.value.code == "cluster_identity_unavailable"
    assert server.connections == [] and server.mutating() == []


def test_a_hand_built_identity_cannot_open_a_privileged_connection(target, server):
    """R4-01: copying the facts of a verified identity does not authorize anything; the
    module's per-instance seal is required."""
    forged = identity.ClusterIdentity(
        system_identifier=CLUSTER_IDENTIFIER,
        container_id=CONTAINER,
        image=identity.PINNED_IMAGE,
        volume=identity.DATA_VOLUME,
        mode="compose",
        port=55432,
    )
    assert identity.issued(forged) is False
    run = Run(target, connect=server.connect, token=TOKEN, cluster=forged, docker=server.docker)

    with pytest.raises(UnsafeTarget) as refused:
        with run.control():
            pytest.fail("a hand-built identity opened a privileged connection")

    assert refused.value.code == "cluster_identity_missing"
    assert server.connections == [] and server.statements == []


def test_a_verified_cluster_still_supports_the_permitted_operations(run, server):
    name = run.create_database()

    with run.control() as control:  # a privileged connection, verified before it is yielded
        assert control.execute(harness._IDENTITY_SQL).fetchone()[0] == "postgres"

    run.drop_database(name)
    assert name not in server.databases


def test_privileged_grants_refuse_an_external_engine(target, server):
    """R4-01 item 4: an arbitrary SQLAlchemy engine is not evidence of a verified cluster."""
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    run.prepare_roles()
    before = len(server.mutating())
    external = sa.create_engine("sqlite://")
    try:
        with pytest.raises(UnsafeTarget) as refused:
            run.apply_privileges(external)
    finally:
        external.dispose()

    assert refused.value.code == "unverified_engine"
    assert server.mutating()[before:] == []  # not one GRANT, REVOKE or ALTER DEFAULT left the run


def test_privileged_grants_refuse_an_engine_that_does_not_reach_the_verified_cluster(run, server, monkeypatch):
    """R4-01 item 5: even an engine this run created is refused when the connection it
    actually opens is not the verified cluster (here: the engine factory is swapped for
    SQLite after registration, so its connection parameters are not the validated ones)."""
    name = run.create_database()
    run.prepare_roles()  # apply_privileges needs the prepared identities before its engine gate
    monkeypatch.setattr(harness, "create_db_engine", lambda url_text: sa.create_engine("sqlite://"))
    engine = run.engine(name)  # registered with the run, but its connections are elsewhere
    before = len(server.mutating())
    try:
        with pytest.raises(UnsafeTarget) as refused:
            run.apply_privileges(engine)
    finally:
        engine.dispose()

    assert refused.value.code in ("connect_parameters", "peer_unknown")
    assert server.mutating()[before:] == []


class EngineConnection:
    """The parts of a SQLAlchemy `Connection` that `verify_engine_connection` reads."""

    def __init__(self, server, *, peer, database="postgres"):
        self.server = server
        self.database = database
        self.connection = SimpleNamespace(
            dbapi_connection=SimpleNamespace(
                info=SimpleNamespace(hostaddr=peer, port=55432, dbname=database)
            )
        )

    def exec_driver_sql(self, sql):
        if sql == harness._IDENTITY_SQL:
            return Rows(
                self.database,
                self.server.databases[self.database],
                self.server.owner(self.database),
            )
        if sql == harness._ROLE_SQL:
            return Rows("postgres", "postgres")
        if sql == identity.SYSTEM_IDENTIFIER_SQL:
            return Rows(self.server.system_identifier)
        raise AssertionError(f"a statement the verification should not send: {sql}")


def test_the_actual_engine_connection_is_what_gets_verified(target, server):
    """R4-01 item 5, directly: `verify_engine_connection` reads the peer from the driver's
    own connection, so a foreign destination is refused even when the Run itself is the
    verified one."""
    expected = Expected(target, "postgres", None, CLUSTER_IDENTIFIER)

    harness.verify_engine_connection(EngineConnection(server, peer=target.address), expected)

    with pytest.raises(UnsafeTarget) as refused:
        harness.verify_engine_connection(EngineConnection(server, peer=REMOTE), expected)
    assert refused.value.code == "wrong_peer"

    server.system_identifier = OTHER_IDENTIFIER
    with pytest.raises(UnsafeTarget) as refused:
        harness.verify_engine_connection(EngineConnection(server, peer=target.address), expected)
    assert refused.value.code == "cluster_identity_changed"


# --- R4-01 item 8: the restore-identity lifecycle ------------------------------------------------


def test_the_restore_lifecycle_creates_tracks_and_drops_a_disabled_database(target, server):
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    run.prepare_roles()

    name = run.create_database_as("restore")  # created by the restore login, disabled

    assert name in run.pending_cleanup()["databases"]
    assert server.databases[name] == run.marker
    assert server.owners[name] == run.roles["owner"].name
    assert any("ALLOW_CONNECTIONS false" in sql for _, sql in server.statements)
    assert any("CREATE DATABASE" in sql for sql in (s for _, s in server.statements))

    run.close()

    assert name not in server.databases and server.roles == {}


def test_the_restore_lifecycle_cannot_create_an_untracked_database(target, server):
    run = Run(target, connect=server.connect, token=TOKEN, docker=FakeDocker(inspect_fails=True), environ={})

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database_as("restore")

    assert refused.value.code == "cluster_identity_unavailable"
    assert server.mutating() == [] and server.connections == []
    assert run.pending_cleanup()["databases"] == ()


# --- R4-03: every cleanup entry point revalidates -------------------------------------------------


def test_a_direct_drop_revalidates_and_refuses_when_docker_is_unavailable(run, server):
    """The demonstrated bypass: `drop_database()` accepted the cached identity. Now the
    Docker evidence is re-established first, and nothing is dropped without it."""
    name = run.create_database()
    server.docker.inspect_fails = True

    with pytest.raises(UnsafeTarget) as refused:
        run.drop_database(name)

    assert refused.value.code == "cluster_identity_unavailable"
    assert name in server.databases and name in run.pending_cleanup()["databases"]
    assert not [s for s in server.mutating() if s.startswith("DROP")]

    server.docker.inspect_fails = False  # the correct environment comes back
    run.drop_database(name)  # a later safe retry completes
    assert name not in server.databases and name not in run.pending_cleanup()["databases"]


def test_a_direct_drop_refuses_a_substituted_container(run, server):
    name = run.create_database()
    server.docker.data_volume = "another-volume"  # the same port, a different cluster's data

    with pytest.raises(UnsafeTarget) as refused:
        run.drop_database(name)

    assert refused.value.code == "cluster_identity_mismatch"
    assert name in server.databases and name in run.pending_cleanup()["databases"]
    assert not [s for s in server.mutating() if s.startswith("DROP")]

    server.docker.data_volume = identity.DATA_VOLUME
    run.drop_database(name)
    assert name not in server.databases


def test_a_direct_drop_refuses_a_changed_system_identifier(run, server):
    name = run.create_database()
    server.docker.identifier = OTHER_IDENTIFIER

    with pytest.raises(UnsafeTarget) as refused:
        run.drop_database(name)

    assert refused.value.code == "cluster_identity_changed"
    assert name in server.databases and not [s for s in server.mutating() if s.startswith("DROP")]

    server.docker.identifier = CLUSTER_IDENTIFIER
    run.drop_database(name)
    assert name not in server.databases


# --- R4-06: the fake enforces the DROP ROLE dependency ------------------------------------------


def test_dropping_roles_before_revoking_their_grants_fails_and_keeps_the_records(target, server):
    """Negative control: the wrong cleanup order (DROP ROLE before REVOKE) fails the way
    PostgreSQL fails it - a role that still holds a database privilege cannot be dropped -
    and nothing is falsely recorded as cleaned."""
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    run.prepare_roles()

    problems = run._drop_roles()  # the wrong order: the maintenance CONNECT grants still exist

    assert problems
    assert server.roles  # the grant-holding roles survived
    assert run.pending_cleanup()["maintenance_grants"]  # and their grants are still tracked
    assert run.pending_cleanup()["roles"]

    run.close()  # the correct order - revoke first, then drop - completes

    assert server.roles == {} and server.maintenance_grants == []
    assert run.pending_cleanup()["roles"] == () and run.pending_cleanup()["maintenance_grants"] == ()


# --- R3-02: cleanup tracking and failure injection ----------------------------------------------


def fail_on_nth(pattern, n):
    seen = {"count": 0}

    def predicate(sql):
        if pattern in sql:
            seen["count"] += 1
            return seen["count"] == n
        return False

    return predicate


def test_a_failure_after_the_first_maintenance_grant_revokes_it_and_drops_the_roles(target, server):
    server.databases.pop("persistent", None)
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    server.fail_when = fail_on_nth('GRANT CONNECT ON DATABASE "postgres"', 2)

    with pytest.raises(RuntimeError):
        run.prepare_roles()

    assert server.maintenance_grants == []  # the first grant was revoked
    assert server.roles == {}  # and both roles created before the failure are gone
    assert run.pending_cleanup()["roles"] == ()
    assert run.pending_cleanup()["maintenance_grants"] == ()


def test_a_failure_after_the_second_maintenance_grant_revokes_both(target, server):
    server.databases.pop("persistent", None)
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    server.fail_when = fail_on_nth("SELECT count(*) FROM aclexplode(", 1)  # right after both grants

    with pytest.raises(RuntimeError):
        run.prepare_roles()

    assert server.maintenance_grants == []
    assert server.roles == {}
    assert run.pending_cleanup()["maintenance_grants"] == ()


def test_a_revocation_failure_keeps_the_grant_and_the_roles_and_never_hides_the_setup_error(target, server):
    server.databases.pop("persistent", None)
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    seen = {"grants": 0}

    def predicate(sql):
        if 'GRANT CONNECT ON DATABASE "postgres"' in sql:
            seen["grants"] += 1
            return seen["grants"] == 2
        return "REVOKE CONNECT ON DATABASE" in sql  # every revocation fails too

    server.fail_when = predicate
    with pytest.raises(RuntimeError) as failed:
        run.prepare_roles()

    # The setup failure is what the caller sees; the cleanup failure is its cause, not lost.
    assert failed.value.__cause__ is not None
    assert len(server.maintenance_grants) == 1  # the record whose revocation failed is kept
    assert set(server.roles)  # nothing was falsely reported as dropped
    pending = run.pending_cleanup()
    assert pending["maintenance_grants"] and pending["roles"] and pending["failures"]
    assert len(server.dropped_roles) == 0

    server.fail_when = None  # the operator (or a retry) can finish it
    run.close()
    assert server.maintenance_grants == [] and server.roles == {}
    assert run.pending_cleanup()["roles"] == () and run.pending_cleanup()["maintenance_grants"] == ()


def test_a_role_drop_failure_keeps_only_the_failed_role_record_for_the_retry(target, server):
    server.databases.pop("persistent", None)
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    run.prepare_roles()
    server.fail_when = fail_on_nth('DROP ROLE "', 1)  # the first drop (reverse order) fails

    with pytest.raises(RuntimeError):
        run.close()

    survivor = run.pending_cleanup()["roles"]
    assert len(survivor) == 1 and harness.ROLE_NAME.fullmatch(survivor[0])
    assert list(server.roles) == list(survivor)  # exactly the failed one remains
    assert run.pending_cleanup()["failures"]

    server.fail_when = None
    run.close()
    assert server.roles == {} and run.pending_cleanup()["roles"] == ()


# --- R5-02: no privileged expectation without a verified cluster identifier --------------------


def test_the_old_optional_identity_shape_no_longer_constructs(target):
    """The exact Codex shape `Expected(target, "postgres", None)` is structurally gone:
    the identifier is a required field, not an optional omission."""
    with pytest.raises(TypeError):
        Expected(target, "postgres", None)


@pytest.mark.parametrize("identifier", [None, "", "not-a-number"])
def test_an_expectation_without_a_verified_cluster_identifier_cannot_be_built(target, identifier):
    with pytest.raises(UnsafeTarget) as refused:
        Expected(target, "postgres", None, identifier)

    assert refused.value.code == "cluster_identity_missing"


def test_guarded_connect_refuses_a_missing_identity_before_the_driver(target):
    """The exact Codex shape: an expectation that omitted the identifier. The common gate
    refuses it before the dialect is called, so zero statements and zero connections."""
    called = Tripwire()
    dialect = SimpleNamespace(connect=called)
    expected = SimpleNamespace(cluster_identifier=None)  # a lean future/foreign object

    with pytest.raises(UnsafeTarget) as refused:
        harness.guarded_connect(expected)(dialect, None, [], target.connect_params("postgres"))

    assert refused.value.code == "cluster_identity_missing"
    assert called.calls == []


def test_an_injected_unissued_identity_cannot_build_a_privileged_expectation(target, server):
    forged = identity.ClusterIdentity(
        system_identifier=CLUSTER_IDENTIFIER,
        container_id=CONTAINER,
        image=identity.PINNED_IMAGE,
        volume=identity.DATA_VOLUME,
        mode="compose",
        port=55432,
    )
    run = Run(target, connect=server.connect, token=TOKEN, cluster=forged, docker=server.docker)

    with pytest.raises(UnsafeTarget) as refused:
        run._expected("postgres", None)
    assert refused.value.code == "cluster_identity_missing"

    with pytest.raises(UnsafeTarget) as refused:
        with run.control():
            pytest.fail("an unissued identity opened a privileged connection")
    assert refused.value.code == "cluster_identity_missing"
    assert server.connections == [] and server.mutating() == []


def test_an_altered_copy_of_a_verified_identity_authorizes_nothing(target, server):
    import dataclasses

    altered = dataclasses.replace(cluster_identity(), system_identifier=OTHER_IDENTIFIER)
    assert identity.issued(altered) is False
    run = Run(target, connect=server.connect, token=TOKEN, cluster=altered, docker=server.docker)

    with pytest.raises(UnsafeTarget) as refused:
        with run.control():
            pytest.fail("a copied proof on altered facts opened a privileged connection")

    assert refused.value.code == "cluster_identity_missing"
    assert server.connections == [] and server.mutating() == []


# --- R5-03: cleanup revalidates the disposable identity at every mutation boundary ---------------


def _limit_identity_inspections(server, allowed):
    """Make the Docker identity evidence disappear after `allowed` successful inspections,
    so a failure can be injected at a precise point inside cleanup."""
    original = server.docker.inspect
    seen = {"count": 0}

    def inspect(container_id):
        seen["count"] += 1
        if seen["count"] > allowed:
            raise identity.IdentityError(
                "cluster_identity_unavailable", "simulated: Docker became unavailable"
            )
        return original(container_id)

    server.docker.inspect = inspect
    return original


def test_docker_unavailable_before_revocation_blocks_every_mutation(target, server):
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    run.prepare_roles()
    before = len(server.mutating())
    server.docker.inspect_fails = True

    with pytest.raises(UnsafeTarget) as refused:
        run.close()

    assert refused.value.code == "cluster_identity_unavailable"
    assert len(server.mutating()) == before  # nothing revoked, nothing dropped
    assert server.maintenance_grants and server.roles  # records kept for a safe retry

    server.docker.inspect_fails = False  # the original environment comes back
    run.close()
    assert server.maintenance_grants == [] and server.roles == {}


def test_docker_loss_between_revocation_and_role_deletion_blocks_role_drops(target, server):
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    run.prepare_roles()
    original = _limit_identity_inspections(server, allowed=3)  # close + 2 revocations

    with pytest.raises(UnsafeTarget) as refused:
        run.close()

    assert refused.value.code == "cluster_identity_unavailable"
    assert server.maintenance_grants == []  # both revocations did run
    assert len(server.roles) == 7  # not one role was dropped
    assert not [s for s in server.mutating() if s.startswith("DROP ROLE")]
    assert run.pending_cleanup()["roles"]  # still tracked for the retry

    server.docker.inspect = original
    run.close()
    assert server.roles == {}


def test_a_changed_cluster_identifier_during_cleanup_blocks_every_mutation(target, server):
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    run.prepare_roles()
    before = len(server.mutating())
    server.docker.identifier = OTHER_IDENTIFIER  # the control file now names another cluster

    with pytest.raises(UnsafeTarget) as refused:
        run.close()

    assert refused.value.code == "cluster_identity_changed"
    assert len(server.mutating()) == before
    assert server.maintenance_grants and server.roles

    server.docker.identifier = CLUSTER_IDENTIFIER
    run.close()
    assert server.maintenance_grants == [] and server.roles == {}


# --- R5-05: an attempted CREATE is never evidence of ownership ---------------------------------


def _run_with_roles(target, server):
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)
    run.prepare_roles()
    return run


def test_a_failed_create_against_a_preexisting_database_is_never_adopted_or_dropped(
    target, server, monkeypatch
):
    """The exact reproduction: the expected name is taken by a database with the expected
    owner and NO marker. CREATE fails with duplicate_database; close() must leave it."""
    run = _run_with_roles(target, server)
    existing = f"house_helper_test_{TOKEN}_1"
    server.databases[existing] = None  # no marker at all
    server.owners[existing] = run.identity("owner").name
    # The inventory gate now refuses this database on its own (tested separately); the
    # CREATE lifecycle is the subject here, so reach it directly.
    monkeypatch.setattr(run, "_require_clean_inventory", lambda control: None)

    with pytest.raises(SimulatedDatabaseError) as failed:
        run.create_database_as("restore")

    assert failed.value.sqlstate == "42P04"
    assert run.pending_cleanup()["databases"] == ()  # not recorded as owned
    assert run.pending_cleanup()["ambiguous"] == ()

    run.close()

    assert existing in server.databases  # the pre-existing database survived
    assert not [s for s in server.mutating() if s.startswith("DROP DATABASE")]
    assert run.pending_cleanup()["databases"] == ()


def test_an_ambiguous_creation_is_preserved_and_reported(target, server, monkeypatch):
    run = _run_with_roles(target, server)
    existing = f"house_helper_test_{TOKEN}_1"
    server.owners[existing] = run.identity("owner").name
    monkeypatch.setattr(run, "_require_clean_inventory", lambda control: None)
    server.fail_on = "CREATE DATABASE"

    with pytest.raises(RuntimeError):
        run.create_database_as("restore")
    server.fail_on = None
    # Model a statement that did take effect before its answer was lost.
    server.databases[existing] = None

    assert run.pending_cleanup()["databases"] == ()
    assert run.pending_cleanup()["ambiguous"] == (existing,)

    with pytest.raises(UnsafeTarget) as refused:
        run.close()

    assert refused.value.code == "ambiguous_create"
    assert existing in server.databases
    assert not [s for s in server.mutating() if s.startswith("DROP DATABASE")]
    assert run.pending_cleanup()["ambiguous"] == (existing,)


def test_an_ambiguous_creation_that_never_took_effect_is_forgotten(target, server, monkeypatch):
    run = _run_with_roles(target, server)
    monkeypatch.setattr(run, "_require_clean_inventory", lambda control: None)
    server.fail_on = "CREATE DATABASE"

    with pytest.raises(RuntimeError):
        run.create_database_as("restore")
    server.fail_on = None

    assert run.pending_cleanup()["ambiguous"]  # recorded while unresolved
    run.close()

    assert run.pending_cleanup()["ambiguous"] == ()
    assert server.maintenance_grants == [] and server.roles == {}


# --- R5-04: volume provenance and runner evidence at the harness level -------------------------


def test_a_foreign_volume_with_the_expected_name_blocks_every_destructive_call(target, server):
    run = Run(
        target,
        connect=server.connect,
        token=TOKEN,
        docker=FakeDocker(volume_labels={}),  # right name, no Compose provenance
        environ={},
    )

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()

    assert refused.value.code == "cluster_identity_mismatch"
    assert server.mutating() == [] and server.connections == []


def test_a_data_volume_shared_with_another_container_blocks_the_run(target, server):
    run = Run(
        target,
        connect=server.connect,
        token=TOKEN,
        docker=FakeDocker(shared_with=["b" * 64]),
        environ={},
    )

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()

    assert refused.value.code == "cluster_identity_mismatch"
    assert server.mutating() == [] and server.connections == []


def test_runner_scoped_evidence_in_local_mode_blocks_the_run(target, server):
    run = Run(
        target,
        connect=server.connect,
        token=TOKEN,
        docker=FakeDocker(),
        environ={identity.RUN_ID_ENV: "1234567890"},
    )

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()

    assert refused.value.code == "cluster_identity_conflict"
    assert server.mutating() == [] and server.connections == []


def test_another_compose_projects_container_cannot_use_the_ci_contract(target, server):
    volume = identity.ci_volume(CI_IDENTITY_ENVIRON)
    docker = FakeDocker(
        labels={
            identity.COMPOSE_PROJECT_LABEL: "another-project",
            identity.COMPOSE_SERVICE_LABEL: "postgres",
            identity.COMPOSE_ONEOFF_LABEL: "False",
        },
        volume=volume,
        volume_labels={},
    )
    run = Run(target, connect=server.connect, token=TOKEN, docker=docker, environ=CI_IDENTITY_ENVIRON)

    with pytest.raises(UnsafeTarget) as refused:
        run.create_database()

    assert refused.value.code == "cluster_identity_conflict"
    assert server.mutating() == [] and server.connections == []


def test_a_scratch_looking_foreign_database_blocks_role_preparation(target, server):
    """R5-04 item 8: the inventory check reads the actual owner and marker, not the shape
    of the name."""
    server.databases[f"house_helper_test_{'f' * 32}_1"] = None  # scratch-looking, no marker
    run = Run(target, connect=server.connect, token=TOKEN, cluster=cluster_identity(), docker=server.docker)

    with pytest.raises(UnsafeTarget) as refused:
        run.prepare_roles()

    assert refused.value.code == "not_dedicated"
    assert server.roles == {} and server.mutating() == []


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
