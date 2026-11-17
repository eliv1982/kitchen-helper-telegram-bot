"""Read-only preflight for the HH-DB-01 WP-2B PostgreSQL deploy surface.

    python deploy/postgres/preflight.py [--compose PATH] [--env-file PATH] [--credentials-dir PATH]

The preflight answers one question: may this host be allowed to start the PostgreSQL surface
at all? It changes nothing. It creates no container, network, volume, role, database or
credential; it writes no file; it runs only these Docker commands, all of which are read-only:

    docker compose -f deploy/compose.postgres.yaml --profile pg --profile ops config --format json
    docker network ls --format {{.ID}}
    docker network inspect --format {{json .}} <id>

Network IDs are used so that a network whose name contains a space cannot be split into two
names. The inventory is read from each inspected object's Name and IPAM.Config.

Any other Docker subcommand is refused by `_docker` before it is run.

What it checks
  1. Activation: the required HOUSE_HELPER_PG_ACTIVATE interpolation resolves, the surface is
     behind Compose profiles, and the resolved database and web entrypoints are the exact
     runtime guard that refuses any value but `1`. Compose profiles do not enforce that: an
     explicit service selection activates the profile, so the guard is what stops it.
  2. The application env file holds no HOUSE_HELPER_DATABASE_URL (ARC-03) and no PG* client
     setting (the credential has exactly one source: the per-role passfile).
  3. Every role has its own passfile and the bootstrap superuser has its own password file;
     each is a regular file, mode 0600 (or stricter), owned by the container user that reads
     it, and contains exactly that role's libpq line. No value is printed.
  4. pg_hba.conf has the mandatory first-match order: local/peer for the superuser, local
     reject for everyone else, an explicit remote-superuser reject BEFORE the generic
     scram-sha-256 line, and no trust/md5 anywhere. postgresql.conf selects that file, pins
     scram-sha-256, and configures no TLS (OD-6 is open).
  5. The resolved Compose configuration: the pinned image, the dedicated internal network, no
     published ports, the PGDATA and state volumes, the network membership, the password-free
     DSN literals and the five one-shot boundaries. Every service's mount set is an exact
     allowlist of (type, resolved source, destination, read-only): an extra mount, a
     replacement source with the right basename, or a non-read-only credential mount is
     refused. The two configuration mounts must resolve to the very files checked above, and
     every one-shot must resolve to its exact operational entrypoint. Because Compose ignores
     an image's CMD when a non-null entrypoint is declared, the database and web services must
     also carry the exact documented command (the official PostgreSQL entrypoint arguments and
     the Dockerfile's WP-2A start gate respectively); an arbitrary command override is refused.
  6. The candidate database subnet does not overlap any existing Docker network. This is the
     check that makes the fixed CIDR a candidate rather than a verified Production fact.

Exit codes
  0  ok
  1  an unexpected failure (the class name is printed, nothing else)
  2  usage
  3  refused: the surface is not in a state this preflight accepts
  4  the environment cannot be inspected (Docker or Compose is missing or failed)

Nothing printed by this tool is a secret. It prints role names, file-check results, network
names, the subnet and the image digest; it never prints a credential, an env-file value or a
DSN.
"""

import argparse
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

PROJECT_NAME = "house-helper-pg"
DB_SERVICE = "house-helper-db"
APP_SERVICE = "house-helper"
DB_NETWORK = "house-helper-pg-db"
DB_SUBNET = "10.213.79.0/24"
DB_HOST = "house-helper-db"
DB_PORT = 5432
DB_NAME = "house_helper"
STATE_DIR = "/state"
CREDENTIAL_DIR = "/run/credentials"
APP_UID = APP_GID = 10001
SUPERUSER_UID = SUPERUSER_GID = 0

PINNED_POSTGRES_IMAGE = (
    "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
)

# The runtime activation guard. Compose's required interpolation and its profiles only shape
# selection: `${VAR:?}` accepts 0/false, and naming a service explicitly activates its profile.
# The database and web services therefore use this exact shell snippet as their entrypoint and
# replace themselves with the image's own entrypoint/CMD only when the value is exactly `1`.
# The one-shots use the same constant in pg_oneshot.py. The preflight compares the resolved
# entrypoint to these literals, not to a suffix or a basename.
ACTIVATION_VAR = "HOUSE_HELPER_PG_ACTIVATE"
ACTIVATION_VALUE = "1"
ACTIVATION_REFUSAL = (
    f"refused [pg_activation]: {ACTIVATION_VAR} must be exactly "
    f"{ACTIVATION_VALUE}; nothing was started."
)
ACTIVATION_GUARD = (
    f'test "${ACTIVATION_VAR}" = {ACTIVATION_VALUE} || '
    f'{{ echo "{ACTIVATION_REFUSAL}" >&2; exit 78; }};'
)
DB_ENTRYPOINT = [
    "/bin/sh",
    "-c",
    ACTIVATION_GUARD + ' exec /usr/local/bin/docker-entrypoint.sh "$@"',
    "house-helper-pg-activate",
]
APP_ENTRYPOINT = [
    "/bin/sh",
    "-c",
    ACTIVATION_GUARD + ' exec "$@"',
    "house-helper-pg-activate",
]
ONE_SHOT_ENTRYPOINT = ["python", "/opt/house-helper/ops/pg_oneshot.py"]
STAGING_DIR = "/staging"

# Compose ignores an image's default CMD when a non-null entrypoint is declared, so both
# guarded services carry their command explicitly. These are the only accepted values: the
# PostgreSQL command preserves the official entrypoint's config-file argument, and the web
# command is the Dockerfile's WP-2A dbtool start gate, verbatim.
DB_COMMAND = ["postgres", "-c", "config_file=/etc/house-helper/postgresql.conf"]
WEB_COMMAND = [
    "python",
    "/opt/house-helper/ops/dbtool.py",
    "serve",
    "--",
    "uvicorn",
    "house_helper.web.asgi:app",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
    "--no-access-log",
    "--no-server-header",
    "--log-config",
    "/opt/house-helper/logging.json",
    "--lifespan",
    "on",
    "--timeout-keep-alive",
    "120",
    "--timeout-graceful-shutdown",
    "30",
]

# The five one-shots of the Frozen Specification. The login is the role name.
ONE_SHOTS = {
    "migrate": "hh_migrate",
    "provision": "hh_provision",
    "purge": "hh_purge",
    "backup": "hh_backup",
    "restore": "hh_restore",
}
ALL_LOGINS = tuple(ONE_SHOTS.values())
APP_LOGIN = "hh_app"

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent
DEFAULT_COMPOSE = DEPLOY / "compose.postgres.yaml"
DEFAULT_ENV_FILE = DEPLOY / "house-helper.env"
DEFAULT_CREDENTIALS = HERE / "credentials"


class Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EnvironmentProblem(Exception):
    """Docker/Compose cannot be run or did not answer."""


# --- Docker, read-only -----------------------------------------------------------------------


def _allowed_docker_call(args: Sequence[str]) -> bool:
    if list(args[:1]) != ["compose"]:
        if list(args[:2]) == ["network", "ls"]:
            return list(args) == ["network", "ls", "--format", "{{.ID}}"]
        if list(args[:2]) == ["network", "inspect"]:
            return (
                len(args) == 5
                and list(args[2:4]) == ["--format", "{{json .}}"]
                and bool(args[4])
            )
        return False
    # The only Compose command this preflight may run is `config`, after the file and the two
    # profiles. Nothing that starts, stops, builds, pulls or removes a resource is allowed.
    try:
        index = list(args).index("config")
    except ValueError:
        return False
    prefix = list(args[:index])
    return (
        prefix[:2] == ["compose", "-f"]
        and len(prefix) == 7
        and prefix[3:7] == ["--profile", "pg", "--profile", "ops"]
        and list(args[index:]) == ["config", "--format", "json"]
    )


def _docker(args: Sequence[str], environ: Optional[Mapping[str, str]] = None) -> str:
    if not _allowed_docker_call(args):
        raise Refusal("docker_command", "the preflight tried a Docker command it does not allow.")
    try:
        done = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=180,
            env=dict(os.environ if environ is None else environ),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EnvironmentProblem(f"cannot run Docker ({type(error).__name__}).") from None
    if done.returncode != 0:
        # The stderr can name an env file path but never a secret; keep it out anyway.
        raise EnvironmentProblem("Docker refused a read-only inspection command.")
    return done.stdout


# --- file checks ------------------------------------------------------------------------------


def _is_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _permission_problems(path: Path, uid: int, gid: int, what: str) -> List[str]:
    problems: List[str] = []
    if os.name != "posix":
        # Windows has no POSIX ownership or mode to enforce; the deployment target is Linux.
        return problems
    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != uid or info.st_gid != gid:
        problems.append(f"{what} is not owned by the required container uid/gid")
    if mode & 0o077:
        problems.append(f"{what} is readable or writable by group/other (mode must be 0600 or stricter)")
    if not mode & 0o400:
        problems.append(f"{what} is not readable by its owner")
    return problems


def _single_line(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[0] if len(lines) == 1 else None


def passfile_problems(path: Path, login: str) -> List[str]:
    """A libpq passfile: exactly one non-comment line, that login, this host and database."""
    problems: List[str] = []
    line = _single_line(path)
    if line is None:
        return [f"the {login} passfile is not exactly one non-empty line"]
    parts = line.split(":", 4)
    if len(parts) != 5:
        return [f"the {login} passfile is not a five-field libpq line"]
    host, port, database, user, password = parts
    if host != DB_HOST:
        problems.append(f"the {login} passfile does not name {DB_HOST}")
    if port != str(DB_PORT):
        problems.append(f"the {login} passfile does not name port {DB_PORT}")
    if user != login:
        problems.append(f"the {login} passfile does not name its own login")
    if login == "hh_restore":
        if database not in (DB_NAME, "*"):
            problems.append("the restore passfile must use `*` (OD-2 is open) or the serving database")
    elif database != DB_NAME:
        problems.append(f"the {login} passfile does not name {DB_NAME}")
    if not password:
        problems.append(f"the {login} passfile has an empty password")
    return problems


def superuser_password_problems(path: Path) -> List[str]:
    if _single_line(path) is None:
        return ["the bootstrap superuser password file is not exactly one non-empty line"]
    return []


def credential_problems(directory: Path) -> List[str]:
    problems: List[str] = []
    for login in (APP_LOGIN, *ALL_LOGINS):
        path = directory / f"{login}.pgpass"
        if not _is_regular_file(path):
            problems.append(f"the {login} passfile is missing or not a regular file")
            continue
        problems += _permission_problems(path, APP_UID, APP_GID, f"the {login} passfile")
        problems += passfile_problems(path, login)
    superuser = directory / "postgres_superuser.password"
    if not _is_regular_file(superuser):
        problems.append("the bootstrap superuser password file is missing or not a regular file")
    else:
        problems += _permission_problems(
            superuser, SUPERUSER_UID, SUPERUSER_GID, "the bootstrap superuser password file"
        )
        problems += superuser_password_problems(superuser)
    return problems


# --- authentication and server configuration --------------------------------------------------


def _meaningful_lines(path: Path) -> List[List[str]]:
    lines: List[List[str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line.split())
    return lines


def hba_problems(path: Path, subnet: str = DB_SUBNET) -> List[str]:
    lines = _meaningful_lines(path)
    required = [
        ["local", "all", "postgres", "peer"],
        ["local", "all", "all", "reject"],
        ["host", "all", "postgres", subnet, "reject"],
        ["host", "all", "all", subnet, "scram-sha-256"],
    ]
    problems: List[str] = []
    if lines[:4] != required:
        problems.append(
            "pg_hba.conf does not have the mandatory first-match order "
            "(local postgres peer; local all reject; host postgres DB-CIDR reject; "
            "host all DB-CIDR scram-sha-256)"
        )
    extra = lines[4:]
    if extra and extra != [["host", "all", "all", "all", "reject"]]:
        problems.append("pg_hba.conf has an unexpected line after the mandatory block")
    for fields in lines:
        if len(fields) < 4:
            continue
        kind, _database, user = fields[0], fields[1], fields[2]
        # The method is the last field; the fields between the address and it are options.
        method = fields[-1]
        if method in ("trust", "md5", "password"):
            problems.append(f"pg_hba.conf uses a forbidden method ({method})")
        if kind == "hostssl":
            problems.append("pg_hba.conf configures TLS while OD-6 is open")
        if user == "postgres" and method == "scram-sha-256":
            problems.append("pg_hba.conf would permit a remote superuser scram login")
    return problems


def postgresql_conf_values(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def postgresql_conf_problems(path: Path) -> List[str]:
    values = postgresql_conf_values(path)
    problems: List[str] = []
    if values.get("listen_addresses") != "*":
        problems.append("postgresql.conf does not set listen_addresses = '*'")
    if values.get("password_encryption") != "scram-sha-256":
        problems.append("postgresql.conf does not pin password_encryption = scram-sha-256")
    if values.get("hba_file") != "/etc/house-helper/pg_hba.conf":
        problems.append("postgresql.conf does not select the mounted pg_hba.conf")
    if values.get("unix_socket_directories") != "/var/run/postgresql":
        problems.append("postgresql.conf does not use the image's Unix socket directory")
    if any(key == "ssl" or key.startswith("ssl_") for key in values):
        problems.append("postgresql.conf configures TLS while OD-6 is open")
    return problems


# --- DSN, env file and network ----------------------------------------------------------------


def dsn_problems(raw: str, login: str) -> List[str]:
    problems: List[str] = []
    prefix = "postgresql+psycopg://"
    if not raw.startswith(prefix):
        return [f"the {login} DSN does not use postgresql+psycopg://"]
    parts = urlsplit(raw)
    if parts.password:
        problems.append(f"the {login} DSN embeds a password")
    if parts.username != login:
        problems.append(f"the {login} DSN names a different login")
    if parts.hostname != DB_HOST:
        problems.append(f"the {login} DSN does not name {DB_HOST}")
    if parts.port != DB_PORT:
        problems.append(f"the {login} DSN does not name port {DB_PORT}")
    if parts.path != f"/{DB_NAME}":
        problems.append(f"the {login} DSN does not name {DB_NAME}")
    if parts.query or parts.fragment:
        problems.append(f"the {login} DSN carries query parameters or a fragment")
    return problems


def env_file_problems(path: Path) -> List[str]:
    problems: List[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ["the application env file is missing or unreadable"]
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, _value = line.partition("=")
        key = key.strip()
        if not separator:
            continue
        if key == "HOUSE_HELPER_DATABASE_URL":
            problems.append(
                f"the application env file line {number} sets HOUSE_HELPER_DATABASE_URL "
                "(the DSN has exactly one source: the Compose literal)"
            )
        if re.fullmatch(r"PG[A-Z_]+", key):
            problems.append(
                f"the application env file line {number} sets a PG* client setting "
                "(the credential has exactly one source: the per-role passfile)"
            )
    return problems


def overlapping_networks(
    candidate: str, networks: Mapping[str, Iterable[str]]
) -> List[str]:
    """Names whose subnets overlap the candidate. The candidate's own network is the caller's
    business; it is passed in like any other name."""
    wanted = ipaddress.ip_network(candidate, strict=False)
    problems: List[str] = []
    for name, subnets in sorted(networks.items()):
        for raw in subnets:
            try:
                found = ipaddress.ip_network(str(raw), strict=False)
            except ValueError:
                continue
            if found.version == wanted.version and found.overlaps(wanted):
                problems.append(f"network {name} ({found}) overlaps the candidate {wanted}")
    return problems


# --- resolved Compose -------------------------------------------------------------------------


def _environment(service: Mapping) -> Dict[str, str]:
    return {str(key): "" if value is None else str(value) for key, value in (service.get("environment") or {}).items()}


def _targets(service: Mapping) -> Dict[str, Mapping]:
    return {str(volume.get("target")): volume for volume in service.get("volumes") or []}


def _credential_mounts(service: Mapping, suffix: str) -> List[Mapping]:
    return [
        volume
        for volume in service.get("volumes") or []
        if str(volume.get("target", "")).startswith(CREDENTIAL_DIR + "/")
        and str(volume.get("target", "")).endswith(suffix)
    ]


def _canonical_path(value: Any) -> str:
    """The one comparison form for a mount source: absolute, symlinks resolved, and
    case-normalised on Windows. A basename match is deliberately not enough."""
    return os.path.normcase(os.path.realpath(str(value)))


def _unescape_compose(value: Any) -> str:
    # `docker compose config` re-escapes a literal dollar as `$$` so that its output can be
    # fed back to Compose. The container receives the single-dollar form, which is what the
    # entrypoint comparison and the focused shell tests must use.
    return str(value).replace("$$", "$")


def _resolved_entrypoint(service: Mapping) -> List[str]:
    return [_unescape_compose(item) for item in service.get("entrypoint") or []]


def _mount_signature(volume: Mapping) -> Tuple[str, Optional[str], str, bool]:
    kind = str(volume.get("type"))
    target = str(volume.get("target"))
    source = volume.get("source")
    if kind == "bind":
        signature: Optional[str] = _canonical_path(source)
    else:
        signature = None if source is None else str(source)
    return kind, signature, target, bool(volume.get("read_only"))


def _bind_mount(source: Path, target: str) -> Tuple[str, Optional[str], str, bool]:
    return "bind", _canonical_path(source), target, True


def _volume_mount(source: Optional[str], target: str) -> Tuple[str, Optional[str], str, bool]:
    return "volume", source, target, False


def _mount_problems(
    name: str, service: Mapping, expected: Iterable[Tuple[str, Optional[str], str, bool]]
) -> List[str]:
    observed = Counter(_mount_signature(volume) for volume in service.get("volumes") or [])
    if observed != Counter(expected):
        return [
            f"the {name} service does not mount exactly its allowed volumes "
            "(an extra mount, a replaced source path or a lost read-only flag was found)"
        ]
    return []


def compose_problems(
    resolved: Mapping, *, credentials_dir: Optional[Path] = None
) -> List[str]:
    credentials = Path(credentials_dir) if credentials_dir is not None else DEFAULT_CREDENTIALS
    problems: List[str] = []
    if resolved.get("name") != PROJECT_NAME:
        problems.append(f"the Compose project name is not {PROJECT_NAME}")
    services = resolved.get("services") or {}
    expected_services = {APP_SERVICE, DB_SERVICE, *ONE_SHOTS}
    if set(services) != expected_services:
        problems.append(
            "the service set is not exactly the web service, the database and the five one-shots "
            f"({sorted(services)})"
        )
        return problems

    networks = resolved.get("networks") or {}
    db_network = networks.get("db") or {}
    if db_network.get("name") != DB_NETWORK or db_network.get("internal") is not True:
        problems.append(f"the db network is not the internal {DB_NETWORK}")
    ipam = ((db_network.get("ipam") or {}).get("config") or [{}])[0]
    if ipam.get("subnet") != DB_SUBNET:
        problems.append(f"the db network subnet is not the documented candidate {DB_SUBNET}")
    proxy = networks.get("proxy") or {}
    if proxy.get("name") != "n8n_n8n_network" or proxy.get("external") is not True:
        problems.append("the proxy network is not the existing external n8n_n8n_network")

    volumes = resolved.get("volumes") or {}
    if (volumes.get("pgdata") or {}).get("name") != "house-helper-pgdata":
        problems.append("the PGDATA volume is not the fixed house-helper-pgdata")
    if (volumes.get("state") or {}).get("name") != "house-helper-pg-state":
        problems.append("the state volume is not the fixed house-helper-pg-state")

    for name, service in services.items():
        if service.get("ports") or service.get("expose"):
            problems.append(f"service {name} publishes or exposes a port")
        if service.get("network_mode"):
            problems.append(f"service {name} uses network_mode")
        if service.get("secrets"):
            problems.append(f"service {name} uses top-level secrets instead of a per-file mount")
        # A config is a file Compose places inside the container without an entry in
        # `volumes`, so the exact mount allowlist cannot see it: it can add a foreign passfile
        # or replace a checked file (the one-shot entrypoint) while every other check still
        # passes. `volumes_from` likewise inherits mounts the resolved `volumes` list never
        # shows. WP-2B uses neither mechanism; any declaration is refused.
        if service.get("configs"):
            problems.append(
                f"service {name} mounts a Compose config (the surface uses only explicit "
                "bind mounts, which the exact allowlist can verify)"
            )
        if service.get("volumes_from"):
            problems.append(
                f"service {name} inherits volumes from another container "
                "(the surface uses only its own exact mount set)"
            )

    db = services[DB_SERVICE]
    if db.get("image") != PINNED_POSTGRES_IMAGE:
        problems.append("the PostgreSQL image is not the pinned PostgreSQL 16 digest")
    if db.get("container_name") != DB_SERVICE:
        problems.append(f"the PostgreSQL container is not positively named {DB_SERVICE}")
    if set(db.get("networks") or {}) != {"db"}:
        problems.append("the PostgreSQL service is not on the database network only")
    if db.get("read_only") or db.get("cap_drop"):
        problems.append("the PostgreSQL service carries the application's hardening, which its entrypoint cannot use")
    db_env = _environment(db)
    if db_env.get("POSTGRES_PASSWORD_FILE") != f"{CREDENTIAL_DIR}/postgres_superuser.password":
        problems.append("the bootstrap superuser password is not read from its read-only file")
    if "POSTGRES_PASSWORD" in db_env or "POSTGRES_HOST_AUTH_METHOD" in db_env:
        problems.append("the PostgreSQL service carries a password or a host-auth method in its environment")
    if "POSTGRES_USER" in db_env or "POSTGRES_DB" in db_env:
        problems.append(
            "the PostgreSQL service names a non-default bootstrap user or database "
            "(Mode A requires the image's fresh-cluster defaults)"
        )
    command = [str(item) for item in db.get("command") or []]
    if command != DB_COMMAND:
        problems.append(
            "the PostgreSQL service does not carry the exact documented startup command "
            "(the official entrypoint with the mounted postgresql.conf)"
        )
    if any("test_cluster" in str(item) for item in db.get("command") or []):
        problems.append("the PostgreSQL service carries the disposable test-cluster marker")
    if any(str(key).startswith("traefik.") for key in (db.get("labels") or {})):
        problems.append("the PostgreSQL service carries Traefik labels")
    if (db.get("labels") or {}).get("house-helper.pg.activation") != "1":
        problems.append("the activation guard is not in force on the PostgreSQL service")
    if _resolved_entrypoint(db) != DB_ENTRYPOINT:
        problems.append(
            "the PostgreSQL service does not run the exact runtime activation guard before "
            "the image's own entrypoint"
        )
    if db_env.get(ACTIVATION_VAR) != ACTIVATION_VALUE:
        problems.append("the PostgreSQL service does not receive the exact activation value")
    problems += _mount_problems(
        DB_SERVICE,
        db,
        [
            _volume_mount("pgdata", "/var/lib/postgresql/data"),
            _bind_mount(HERE / "postgresql.conf", "/etc/house-helper/postgresql.conf"),
            _bind_mount(HERE / "pg_hba.conf", "/etc/house-helper/pg_hba.conf"),
            _bind_mount(
                credentials / "postgres_superuser.password",
                f"{CREDENTIAL_DIR}/postgres_superuser.password",
            ),
        ],
    )
    db_targets = _targets(db)
    pgdata = db_targets.get("/var/lib/postgresql/data") or {}
    if pgdata.get("type") != "volume" or pgdata.get("source") != "pgdata":
        problems.append("the PostgreSQL service does not mount the dedicated pgdata volume")
    for target, expected_name in (
        ("/etc/house-helper/postgresql.conf", "postgresql.conf"),
        ("/etc/house-helper/pg_hba.conf", "pg_hba.conf"),
    ):
        mount = db_targets.get(target) or {}
        if not mount.get("read_only") or Path(str(mount.get("source", ""))).name != expected_name:
            problems.append(f"the PostgreSQL service does not mount {target} read-only")
    superuser = _credential_mounts(db, ".password")
    if (
        len(superuser) != 1
        or not superuser[0].get("read_only")
        or Path(str(superuser[0].get("source", ""))).name != "postgres_superuser.password"
    ):
        problems.append("the PostgreSQL service does not mount exactly one read-only bootstrap credential")
    if (db.get("healthcheck") or {}).get("test") is None:
        problems.append("the PostgreSQL service has no healthcheck")

    app = services[APP_SERVICE]
    if set(app.get("networks") or {}) != {"proxy", "db"}:
        problems.append("the web service is not the only service on both proxy and db networks")
    if app.get("read_only") is not True or app.get("cap_drop") != ["ALL"]:
        problems.append("the web service lost the application hardening")
    if app.get("pull_policy") != "never":
        problems.append("the web service would pull an application image")
    app_env = _environment(app)
    if app_env.get("HOUSE_HELPER_STATE_DIR") != STATE_DIR:
        problems.append("the web service does not name the state volume")
    if app_env.get("PGPASSFILE") != f"{CREDENTIAL_DIR}/{APP_LOGIN}.pgpass":
        problems.append("the web service does not name its own passfile")
    if any(key in app_env for key in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "PGSSLMODE")):
        problems.append("the web service carries a second credential source or a TLS choice")
    if "HOUSE_HELPER_DATABASE_URL" not in app_env:
        problems.append("the web service has no DSN literal")
    problems += dsn_problems(app_env.get("HOUSE_HELPER_DATABASE_URL", ""), APP_LOGIN)
    if _resolved_entrypoint(app) != APP_ENTRYPOINT:
        problems.append(
            "the web service does not run the exact runtime activation guard before the "
            "image's own start command"
        )
    if [str(item) for item in app.get("command") or []] != WEB_COMMAND:
        problems.append(
            "the web service does not carry the exact WP-2A start command handed to the guard "
            "(Compose ignores the image CMD when an entrypoint is declared)"
        )
    if app_env.get(ACTIVATION_VAR) != ACTIVATION_VALUE:
        problems.append("the web service does not receive the exact activation value")
    problems += _mount_problems(
        APP_SERVICE,
        app,
        [
            _volume_mount("state", STATE_DIR),
            _bind_mount(credentials / f"{APP_LOGIN}.pgpass", f"{CREDENTIAL_DIR}/{APP_LOGIN}.pgpass"),
        ],
    )
    app_targets = _targets(app)
    if set(app_targets) != {STATE_DIR, f"{CREDENTIAL_DIR}/{APP_LOGIN}.pgpass"}:
        problems.append(f"the web service mounts unexpected volumes ({sorted(app_targets)})")
    if "/data" in app_targets:
        problems.append("the web service mounts the SQLite data volume (ARC-01)")
    state = app_targets.get(STATE_DIR) or {}
    if state.get("type") != "volume" or state.get("source") != "state":
        problems.append("the web service does not mount the shared state volume")
    app_passes = _credential_mounts(app, ".pgpass")
    if (
        len(app_passes) != 1
        or not app_passes[0].get("read_only")
        or Path(str(app_passes[0].get("source", ""))).name != f"{APP_LOGIN}.pgpass"
    ):
        problems.append("the web service does not mount exactly one read-only passfile")

    for name, login in ONE_SHOTS.items():
        service = services[name]
        if set(service.get("networks") or {}) != {"db"}:
            problems.append(f"the {name} one-shot is not on the database network only")
        if service.get("profiles") != ["ops"]:
            problems.append(f"the {name} one-shot is not behind the ops profile")
        if (service.get("logging") or {}).get("driver") != "none":
            problems.append(f"the {name} one-shot keeps a log driver")
        if service.get("pull_policy") != "never":
            problems.append(f"the {name} one-shot would pull an application image")
        if service.get("restart") != "no":
            problems.append(f"the {name} one-shot is not restart: no")
        if (service.get("healthcheck") or {}).get("disable") is not True:
            problems.append(f"the {name} one-shot has no disabled healthcheck")
        if any(str(key).startswith("traefik.") for key in (service.get("labels") or {})):
            problems.append(f"the {name} one-shot carries Traefik labels")
        if service.get("env_file"):
            problems.append(f"the {name} one-shot reads an environment file")
        env = _environment(service)
        if _resolved_entrypoint(service) != ONE_SHOT_ENTRYPOINT:
            problems.append(f"the {name} one-shot does not run the exact operational entrypoint")
        if env.get(ACTIVATION_VAR) != ACTIVATION_VALUE:
            problems.append(f"the {name} one-shot does not receive the exact activation value")
        expected_mounts = [_volume_mount("state", STATE_DIR)]
        if name in ("backup", "restore"):
            expected_mounts.append(_volume_mount(None, STAGING_DIR))
        expected_mounts.append(
            _bind_mount(credentials / f"{login}.pgpass", f"{CREDENTIAL_DIR}/{login}.pgpass")
        )
        problems += _mount_problems(name, service, expected_mounts)
        if env.get("HOUSE_HELPER_STATE_DIR") != STATE_DIR:
            problems.append(f"the {name} one-shot does not name the state volume")
        if any(key in env for key in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "PGSSLMODE")):
            problems.append(f"the {name} one-shot carries a second credential source or a TLS choice")
        if env.get("PGUSER") != login:
            problems.append(f"the {name} one-shot does not use its own login")
        if env.get("PGHOST") != DB_HOST or env.get("PGPORT") != str(DB_PORT):
            problems.append(f"the {name} one-shot does not name the database network's host and port")
        if env.get("PGPASSFILE") != f"{CREDENTIAL_DIR}/{login}.pgpass":
            problems.append(f"the {name} one-shot does not name its own passfile")
        problems += dsn_problems(env.get("HOUSE_HELPER_DATABASE_URL", ""), login)
        if name == "restore":
            if "PGDATABASE" in env:
                problems.append("the restore one-shot pre-selects the OD-2 maintenance database")
        elif env.get("PGDATABASE") != DB_NAME:
            problems.append(f"the {name} one-shot does not name the serving database")
        passes = _credential_mounts(service, ".pgpass")
        if (
            len(passes) != 1
            or not passes[0].get("read_only")
            or Path(str(passes[0].get("source", ""))).name != f"{login}.pgpass"
        ):
            problems.append(f"the {name} one-shot does not mount exactly one read-only passfile")
        if CREDENTIAL_DIR + "/postgres_superuser.password" in _targets(service):
            problems.append(f"the {name} one-shot can read the bootstrap superuser password")
        state = _targets(service).get(STATE_DIR) or {}
        if state.get("type") != "volume" or state.get("source") != "state":
            problems.append(f"the {name} one-shot does not mount the shared state volume")
        if service.get("command") != [name]:
            problems.append(f"the {name} one-shot does not run its own subcommand")
        if name in ("backup", "restore") and "/staging" not in _targets(service):
            problems.append(f"the {name} one-shot has no scratch volume")

    if set((services[APP_SERVICE].get("profiles") or [])) != {"pg"}:
        problems.append("the web service is not behind the pg profile")
    if set((services[DB_SERVICE].get("profiles") or [])) != {"pg"}:
        problems.append("the PostgreSQL service is not behind the pg profile")
    return problems


# --- orchestration ----------------------------------------------------------------------------


def _compose_environment(env_file: Path, credentials: Path) -> Dict[str, str]:
    environ = {key: value for key, value in os.environ.items() if not key.startswith(("HOUSE_HELPER_", "COMPOSE_"))}
    environ.update(
        {
            "HOUSE_HELPER_PG_ACTIVATE": ACTIVATION_VALUE,
            # Absolute, so the resolved mounts can be compared to the files this preflight
            # actually checked rather than to a path relative to wherever Compose ran.
            "HOUSE_HELPER_ENV_FILE": str(Path(env_file).resolve()),
            "HOUSE_HELPER_PG_CREDENTIALS_DIR": str(Path(credentials).resolve()),
        }
    )
    return environ


def _resolved_compose(compose: Path, env_file: Path, credentials: Path) -> Mapping:
    environ = _compose_environment(env_file, credentials)
    raw = _docker(
        [
            "compose",
            "-f",
            str(compose),
            "--profile",
            "pg",
            "--profile",
            "ops",
            "config",
            "--format",
            "json",
        ],
        environ,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise EnvironmentProblem("Compose returned unreadable JSON.") from None


def _network_inventory() -> Dict[str, List[str]]:
    ids = _docker(["network", "ls", "--format", "{{.ID}}"]).split()
    inventory: Dict[str, List[str]] = {}
    for identifier in ids:
        raw = _docker(["network", "inspect", "--format", "{{json .}}", identifier])
        try:
            observed = json.loads(raw)
        except json.JSONDecodeError:
            observed = {}
        name = str(observed.get("Name") or identifier)
        configs = (observed.get("IPAM") or {}).get("Config") or []
        inventory[name] = [
            entry["Subnet"]
            for entry in configs
            if isinstance(entry, dict) and entry.get("Subnet")
        ]
    return inventory


def run(compose: Path, env_file: Path, credentials: Path, *, check_networks: bool = True) -> str:
    if not compose.is_file():
        raise Refusal("compose_missing", f"the Compose file {compose} does not exist.")
    if not env_file.is_file():
        raise Refusal("env_missing", f"the application env file {env_file} does not exist.")
    if not credentials.is_dir():
        raise Refusal("credentials_missing", f"the credential directory {credentials} does not exist.")

    problems: List[str] = []
    problems += env_file_problems(env_file)
    problems += credential_problems(credentials)
    problems += hba_problems(HERE / "pg_hba.conf")
    problems += postgresql_conf_problems(HERE / "postgresql.conf")
    resolved = _resolved_compose(compose, env_file, credentials)
    problems += compose_problems(resolved, credentials_dir=credentials)
    if check_networks:
        inventory = _network_inventory()
        own = inventory.pop(DB_NETWORK, None)
        if own is not None and own != [DB_SUBNET]:
            problems.append(f"an existing network named {DB_NETWORK} has a different subnet")
        problems += overlapping_networks(DB_SUBNET, inventory)
    if problems:
        shown = "; ".join(problems[:8])
        more = f"; and {len(problems) - 8} more" if len(problems) > 8 else ""
        raise Refusal("surface_invalid", f"the PostgreSQL surface is not acceptable: {shown}{more}")

    services = ", ".join(sorted(resolved["services"]))
    file_checks = (
        "0600 ownership and format checked"
        if os.name == "posix"
        else "format and isolation checked; POSIX mode skipped"
    )
    return (
        f"project {PROJECT_NAME}; services {services}; database network {DB_NETWORK} "
        f"({DB_SUBNET}, internal, no published port); image {PINNED_POSTGRES_IMAGE}; "
        f"roles {APP_LOGIN} and {len(ONE_SHOTS)} one-shot logins have their own passfile "
        f"({file_checks}); pg_hba first-match order verified with remote superuser rejection; "
        "no TLS configured (OD-6 open); the candidate subnet does not overlap an existing "
        "Docker network."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preflight.py",
        description="Read-only preflight for the HH-DB-01 WP-2B PostgreSQL deploy surface.",
        allow_abbrev=False,
    )
    parser.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--credentials-dir", type=Path, default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    env_file = args.env_file or Path(os.environ.get("HOUSE_HELPER_ENV_FILE") or DEFAULT_ENV_FILE)
    credentials = args.credentials_dir or Path(
        os.environ.get("HOUSE_HELPER_PG_CREDENTIALS_DIR") or DEFAULT_CREDENTIALS
    )
    try:
        message = run(args.compose, env_file, credentials)
    except Refusal as refusal:
        print(f"refused [{refusal.code}]: {refusal.message}", file=sys.stderr)
        return 3
    except EnvironmentProblem as problem:
        print(f"error [environment]: {problem} Nothing was changed.", file=sys.stderr)
        return 4
    except Exception as exc:  # an exception's text can quote a path: class only
        print(f"error [internal]: unexpected {type(exc).__name__}.", file=sys.stderr)
        return 1
    print(f"ok [postgres_preflight]: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
