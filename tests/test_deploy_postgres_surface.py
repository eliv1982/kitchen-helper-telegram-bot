"""HH-DB-01 WP-2B: the PostgreSQL deploy surface, as tests.

Everything here READS files or runs Docker Compose's read-only `config` command. No image is
built, no container, network or volume is created, no database is reached and no credential is
generated. The tests pin the properties the WP-2B brief requires:

  * a separate Compose project with a digest-pinned PostgreSQL 16 service, an internal-only
    database network, no published port and dedicated PGDATA/state volumes;
  * the activation guard: an ordinary Compose invocation without the explicit variable fails
    before any service is selected;
  * exactly one service on both the proxy and database networks, and only the web service;
  * per-role DSNs with no password, per-role read-only passfile mounts, and five one-shot
    boundaries that refuse every execution;
  * the mandatory pg_hba first-match order, including remote-superuser rejection;
  * no TLS configuration while OD-6 is open, and no OD-7 tuning;
  * the SQLite deployment surface is not redirected to the PostgreSQL file.
"""

import copy
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "deploy"
POSTGRES = DEPLOY / "postgres"
COMPOSE_PG = DEPLOY / "compose.postgres.yaml"
COMPOSE_SQLITE = DEPLOY / "compose.yaml"
PG_HBA = POSTGRES / "pg_hba.conf"
PG_CONF = POSTGRES / "postgresql.conf"
CREDENTIALS = POSTGRES / "credentials"
PREFLIGHT_PATH = POSTGRES / "preflight.py"
ONESHOT_PATH = DEPLOY / "ops" / "pg_oneshot.py"

DB_HOST = "house-helper-db"
DB_PORT = 5432
DB_NAME = "house_helper"
DB_SUBNET = "10.213.79.0/24"
APP_LOGIN = "hh_app"
ONE_SHOTS = {
    "migrate": "hh_migrate",
    "provision": "hh_provision",
    "purge": "hh_purge",
    "backup": "hh_backup",
    "restore": "hh_restore",
}
PINNED_IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_lines(path: Path) -> list:
    return [
        line
        for line in read(path).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = load(PREFLIGHT_PATH, "deploy_postgres_preflight")
oneshot = load(ONESHOT_PATH, "deploy_pg_oneshot")


def dockerfile_cmd() -> list:
    """The Dockerfile's exec-form CMD, line continuations removed."""
    lines = read(REPO_ROOT / "Dockerfile").splitlines()
    payload = []
    started = False
    for line in lines:
        if not started:
            if not line.startswith("CMD "):
                continue
            line = line[len("CMD "):]
            started = True
        payload.append(line.rstrip().rstrip("\\").strip())
        if line.rstrip().endswith("]"):
            break
    return json.loads(" ".join(payload))


# --- static rules: always run ----------------------------------------------------------------


def test_the_surface_is_a_separate_compose_project_named_house_helper_pg():
    text = read(COMPOSE_PG)
    assert re.search(r"^name:\s*house-helper-pg\s*$", text, re.MULTILINE)
    assert "container_name: house-helper-db" in text
    assert "name: house-helper-pg-db" in text
    assert "name: house-helper-pgdata" in text
    assert "name: house-helper-pg-state" in text


def test_the_postgres_image_is_pinned_by_digest_and_is_the_tested_pg16_image():
    text = read(COMPOSE_PG)
    found = re.findall(r"image:\s*(postgres@sha256:[0-9a-f]{64})", text)
    assert found == [PINNED_IMAGE]
    assert preflight.PINNED_POSTGRES_IMAGE == PINNED_IMAGE
    # A moving tag must not appear as the service image.
    assert not re.search(r"^\s+image:\s*postgres:16\S*\s*$", text, re.MULTILINE)


def test_no_service_publishes_a_port_and_the_database_network_is_internal():
    text = "\n".join(code_lines(COMPOSE_PG))
    assert not re.search(r"(?m)^\s*(ports|expose)\s*:", text)
    assert re.search(r"internal:\s*true", text)
    assert DB_SUBNET in text


def test_the_activation_guard_is_required_and_every_service_is_behind_a_profile():
    text = read(COMPOSE_PG)
    assert "HOUSE_HELPER_PG_ACTIVATE:?" in text
    assert 'profiles: ["pg"]' in text
    assert 'profiles: ["ops"]' in text
    # Four services behind pg (database + web) is not what the text says; exactly two pg
    # services and five ops services must be declared.
    assert text.count('profiles: ["pg"]') == 2
    assert text.count('profiles: ["ops"]') == 5


def test_the_explicit_start_commands_are_the_verified_intended_ones():
    # Compose drops an image CMD when a non-null entrypoint is declared, so these must be
    # explicit and exact: the official PostgreSQL entrypoint arguments and the Dockerfile's
    # WP-2A start gate, verbatim.
    assert preflight.DB_COMMAND == [
        "postgres",
        "-c",
        "config_file=/etc/house-helper/postgresql.conf",
    ]
    assert preflight.WEB_COMMAND == dockerfile_cmd()
    assert preflight.WEB_COMMAND[:6] == [
        "python",
        "/opt/house-helper/ops/dbtool.py",
        "serve",
        "--",
        "uvicorn",
        "house_helper.web.asgi:app",
    ]


def test_the_exact_runtime_guard_is_wired_into_the_compose_file():
    # Compose re-escapes a literal dollar as `$$`; the container receives `$`.
    text = read(COMPOSE_PG).replace("$$", "$")
    # Exactly the database and the web service get the shell guard. The one-shots use the
    # same exact check inside pg_oneshot.py, and the preflight requires the same entrypoints.
    assert text.count(preflight.ACTIVATION_GUARD) == 2
    assert text.count("HOUSE_HELPER_PG_ACTIVATE: ${HOUSE_HELPER_PG_ACTIVATE:?") == 7


def test_pg_hba_has_the_mandatory_first_match_order_and_no_weak_method():
    problems = preflight.hba_problems(PG_HBA)
    assert problems == []
    lines = [
        line.split()
        for line in read(PG_HBA).splitlines()
        if line.split("#", 1)[0].strip()
    ]
    assert lines[:4] == [
        ["local", "all", "postgres", "peer"],
        ["local", "all", "all", "reject"],
        ["host", "all", "postgres", DB_SUBNET, "reject"],
        ["host", "all", "all", DB_SUBNET, "scram-sha-256"],
    ]
    text = "\n".join(code_lines(PG_HBA))
    assert not re.search(r"(?m)^\s*\S+\s+\S+\s+\S+\s+\S+\s+(trust|md5|password)\s*$", text)
    assert "hostssl" not in text


def test_the_hba_cidr_and_the_compose_subnet_are_the_same_static_value():
    compose_subnet = re.search(r"subnet:\s*(\S+)", read(COMPOSE_PG)).group(1)
    hba_subnet = next(
        line.split()[3]
        for line in read(PG_HBA).splitlines()
        if line.startswith("host ") and "scram-sha-256" in line
    )
    assert compose_subnet == hba_subnet == DB_SUBNET


def test_postgresql_conf_is_minimal_pins_scram_and_configures_no_tls():
    problems = preflight.postgresql_conf_problems(PG_CONF)
    assert problems == []
    values = preflight.postgresql_conf_values(PG_CONF)
    assert set(values) == {
        "listen_addresses",
        "password_encryption",
        "hba_file",
        "unix_socket_directories",
    }
    assert not any(key == "ssl" or key.startswith("ssl_") for key in values)


def test_the_credential_directory_is_gitignored_and_tracks_no_credential():
    text = read(REPO_ROOT / ".gitignore")
    assert "deploy/postgres/credentials/*.pgpass" in text
    assert "deploy/postgres/credentials/*.password" in text
    git = shutil.which("git")
    if not git or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    done = subprocess.run(
        [git, "-C", str(REPO_ROOT), "ls-files", "--", "deploy/postgres/credentials"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    tracked = done.stdout.split()
    assert (CREDENTIALS / "README.md").is_file()
    assert not [path for path in tracked if path.endswith((".pgpass", ".password"))], tracked


def test_the_image_creates_the_state_volume_with_the_application_owner():
    text = read(REPO_ROOT / "Dockerfile")
    assert "install --directory --owner app --group app --mode 0700 /data /staging /state" in text
    assert "HOUSE_HELPER_STATE_DIR=/state" in text


def test_the_sqlite_surface_is_not_redirected_to_the_postgres_file():
    for path in (
        COMPOSE_SQLITE,
        DEPLOY / "backup.sh",
        DEPLOY / "upgrade.sh",
        *sorted(path for path in (DEPLOY / "systemd").glob("*") if path.is_file()),
    ):
        text = read(path)
        assert "compose.postgres" not in text, path
        assert "house-helper-pg" not in text, path
    sqlite = read(COMPOSE_SQLITE)
    assert "network_mode: none" in sqlite
    assert "sqlite:////data/house_helper.db" in sqlite
    assert "postgres" not in sqlite.lower()


def test_the_one_shot_is_a_fail_closed_boundary_with_no_execution_path():
    text = read(ONESHOT_PATH)
    for forbidden in (
        "import subprocess",
        "import socket",
        "os.exec",
        "import psycopg",
        "import sqlalchemy",
        "connect(",
        "alembic.command",
        "sys.environ",
    ):
        assert forbidden not in text, forbidden
    assert "os.environ" in text
    assert "EX_CONFIG = 78" in text


def test_only_the_web_service_joins_both_networks_and_postgres_has_no_proxy():
    services = read(COMPOSE_PG).split("services:", 1)[1]
    app_block = services.split("  house-helper:\n", 1)[1].split("\n  migrate:", 1)[0]
    db_block = services.split("  house-helper-db:\n", 1)[1].split("\n  house-helper:\n", 1)[0]
    assert "      - proxy\n      - db\n" in app_block
    assert "      - db\n" in db_block
    assert "proxy" not in db_block
    assert "traefik." not in db_block


# --- the preflight's pure checks ---------------------------------------------------------------


def test_the_preflight_refuses_an_application_env_file_with_a_dsn(tmp_path):
    env = tmp_path / "app.env"
    env.write_text("OPENAI_API_KEY=placeholder\nHOUSE_HELPER_DATABASE_URL=sqlite:///decoy.db\n")
    problems = preflight.env_file_problems(env)
    assert any("HOUSE_HELPER_DATABASE_URL" in problem for problem in problems)
    env.write_text("export HOUSE_HELPER_DATABASE_URL=sqlite:///decoy.db\n")
    assert preflight.env_file_problems(env)


def test_the_preflight_refuses_a_pg_client_setting_in_the_application_env_file(tmp_path):
    env = tmp_path / "app.env"
    env.write_text("PGPASSWORD=placeholder\n")
    assert preflight.env_file_problems(env)


def test_the_preflight_passfile_parser_accepts_only_the_role_line(tmp_path):
    good = tmp_path / "hh_app.pgpass"
    good.write_text(f"{DB_HOST}:{DB_PORT}:{DB_NAME}:{APP_LOGIN}:placeholder\n")
    assert preflight.passfile_problems(good, APP_LOGIN) == []
    good.write_text(f"{DB_HOST}:{DB_PORT}:{DB_NAME}:hh_other:placeholder\n")
    assert preflight.passfile_problems(good, APP_LOGIN)
    good.write_text(f"{DB_HOST}:{DB_PORT}:{DB_NAME}:{APP_LOGIN}:\n")
    assert preflight.passfile_problems(good, APP_LOGIN)


def test_the_preflight_accepts_only_the_wildcard_or_serving_database_for_restore(tmp_path):
    restore = tmp_path / "hh_restore.pgpass"
    restore.write_text(f"{DB_HOST}:{DB_PORT}:*:hh_restore:placeholder\n")
    assert preflight.passfile_problems(restore, "hh_restore") == []
    restore.write_text(f"{DB_HOST}:{DB_PORT}:other:hh_restore:placeholder\n")
    assert preflight.passfile_problems(restore, "hh_restore")


def test_the_preflight_refuses_a_group_readable_passfile(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX ownership and mode")
    monkeypatch.setattr(preflight, "APP_UID", os.getuid())
    monkeypatch.setattr(preflight, "APP_GID", os.getgid())
    monkeypatch.setattr(preflight, "SUPERUSER_UID", os.getuid())
    monkeypatch.setattr(preflight, "SUPERUSER_GID", os.getgid())
    write_credentials(tmp_path)
    (tmp_path / "hh_secret.pgpass").write_text("x")
    (tmp_path / "hh_app.pgpass").chmod(0o644)
    problems = preflight.credential_problems(tmp_path)
    assert any("group/other" in problem for problem in problems), problems


def test_the_preflight_refuses_an_overlapping_candidate_subnet():
    assert preflight.overlapping_networks(DB_SUBNET, {"other": ["10.213.79.0/25"]})
    assert preflight.overlapping_networks(DB_SUBNET, {"other": ["10.0.0.0/24"]}) == []


def test_the_preflight_refuses_a_wrong_hba_order(tmp_path):
    hba = tmp_path / "pg_hba.conf"
    hba.write_text(
        "local all postgres peer\n"
        "local all all reject\n"
        f"host all all {DB_SUBNET} scram-sha-256\n"
        f"host all postgres {DB_SUBNET} reject\n"
    )
    assert preflight.hba_problems(hba)


def test_the_preflight_dsn_check_requires_a_password_free_literal():
    good = f"postgresql+psycopg://{APP_LOGIN}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    assert preflight.dsn_problems(good, APP_LOGIN) == []
    assert preflight.dsn_problems(good.replace(f"@{DB_HOST}", ":pw@" + DB_HOST), APP_LOGIN)
    assert preflight.dsn_problems(good.replace(APP_LOGIN, "hh_other"), APP_LOGIN)
    assert preflight.dsn_problems("postgresql://" + good.split("://", 1)[1], APP_LOGIN)
    assert preflight.dsn_problems(good + "?sslmode=disable", APP_LOGIN)


def test_the_preflight_allows_only_read_only_docker_commands():
    compose = [
        "compose",
        "-f",
        str(COMPOSE_PG),
        "--profile",
        "pg",
        "--profile",
        "ops",
        "config",
        "--format",
        "json",
    ]
    assert preflight._allowed_docker_call(compose)
    for forbidden in (
        ["compose", "-f", str(COMPOSE_PG), "up"],
        ["compose", "-f", str(COMPOSE_PG), "run", "migrate"],
        ["compose", "-f", str(COMPOSE_PG), "down"],
        ["compose", "-f", str(COMPOSE_PG), "build"],
        ["compose", "-f", str(COMPOSE_PG), "pull"],
        ["network", "create", "x"],
        ["volume", "rm", "x"],
        ["network", "inspect", "x"],
    ):
        assert not preflight._allowed_docker_call(forbidden), forbidden


# --- the runtime activation guard ----------------------------------------------------------------


def _posix_shell():
    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("a POSIX shell is not installed on this host")
    return shell


def _guard_environment(value):
    environ = {
        key: item
        for key, item in os.environ.items()
        if key != preflight.ACTIVATION_VAR
    }
    if value is not None:
        environ[preflight.ACTIVATION_VAR] = value
    return environ


@pytest.mark.parametrize(
    "value", [None, "", "0", "false", "False", "true", "01", " 1", "1 "]
)
def test_the_runtime_guard_refuses_anything_but_the_exact_activation_value(value):
    done = subprocess.run(
        [
            _posix_shell(),
            "-c",
            preflight.ACTIVATION_GUARD + ' exec "$@"',
            "house-helper-pg-activate",
            sys.executable,
            "-c",
            "print(12345)",
        ],
        env=_guard_environment(value),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 78, (value, done.stderr)
    assert "pg_activation" in done.stderr
    assert "12345" not in done.stdout


def test_the_runtime_guard_allows_the_normal_start_path_only_for_the_exact_value():
    done = subprocess.run(
        [
            _posix_shell(),
            "-c",
            preflight.ACTIVATION_GUARD + ' exec "$@"',
            "house-helper-pg-activate",
            sys.executable,
            "-c",
            "print(12345)",
        ],
        env=_guard_environment("1"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The guard replaced itself with the next command; no container was started.
    assert done.returncode == 0, done.stderr
    assert "12345" in done.stdout


# --- the one-shot's refusal paths ---------------------------------------------------------------


def oneshot_environment(role: str) -> dict:
    login = ONE_SHOTS[role]
    environment = {
        "HOUSE_HELPER_PG_ACTIVATE": "1",
        "PGUSER": login,
        "PGHOST": DB_HOST,
        "PGPORT": str(DB_PORT),
        "PGPASSFILE": f"/run/credentials/{login}.pgpass",
        "HOUSE_HELPER_STATE_DIR": "/state",
        "HOUSE_HELPER_DATABASE_URL": f"postgresql+psycopg://{login}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
    }
    if role != "restore":
        environment["PGDATABASE"] = DB_NAME
    return environment


@pytest.mark.parametrize("role", sorted(set(ONE_SHOTS) - {"migrate"}))
def test_every_unimplemented_one_shot_refuses_before_it_connects(role, capsys):
    code = oneshot.main([role], oneshot_environment(role))
    captured = capsys.readouterr()
    assert code == 78
    assert "oneshot_not_implemented" in captured.err


def test_a_one_shot_with_a_second_credential_source_is_refused_without_printing_it(capsys):
    environment = oneshot_environment("migrate")
    environment["PGPASSWORD"] = "sentinel-must-never-be-printed"
    code = oneshot.main(["migrate"], environment)
    captured = capsys.readouterr()
    assert code == 78
    assert "oneshot_credential_source" in captured.err
    assert "sentinel-must-never-be-printed" not in captured.out + captured.err


def test_a_one_shot_does_not_select_a_tls_mode_while_od6_is_open(capsys):
    environment = oneshot_environment("migrate")
    environment["PGSSLMODE"] = "require"
    code = oneshot.main(["migrate"], environment)
    captured = capsys.readouterr()
    assert code == 78
    assert "oneshot_credential_source" in captured.err


def test_a_one_shot_with_another_role_s_wiring_is_refused(capsys):
    environment = oneshot_environment("migrate")
    environment["PGUSER"] = "hh_app"
    code = oneshot.main(["migrate"], environment)
    captured = capsys.readouterr()
    assert code == 78
    assert "oneshot_login" in captured.err


def test_the_restore_one_shot_does_not_select_the_od2_database(capsys):
    environment = oneshot_environment("restore")
    environment["PGDATABASE"] = "postgres"
    code = oneshot.main(["restore"], environment)
    captured = capsys.readouterr()
    assert code == 78
    assert "oneshot_database" in captured.err


def test_an_unknown_one_shot_argument_is_a_usage_error(capsys):
    assert oneshot.main(["deploy"], {}) == 2
    assert "usage" in capsys.readouterr().err


def cli_environment(role: str) -> dict:
    """A real process environment: the ambient PG*/HOUSE_HELPER_* settings must not decide this."""
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HOUSE_HELPER_", "PG"))
    }
    environ.update(oneshot_environment(role))
    return environ


def run_oneshot_cli(role: str, **override) -> subprocess.CompletedProcess:
    environ = cli_environment(role)
    environ.update(override)
    return subprocess.run(
        [sys.executable, str(ONESHOT_PATH), role],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("role", sorted(set(ONE_SHOTS) - {"migrate"}))
def test_every_unimplemented_real_one_shot_cli_enters_its_validation_path(role):
    # Real invocation with `environ is None`, which is the path that used to raise
    # AttributeError on the nonexistent `sys.environ`.
    done = run_oneshot_cli(role)
    assert done.returncode == 78, done.stderr
    assert "oneshot_not_implemented" in done.stderr
    assert role in done.stderr
    assert "Traceback" not in done.stderr


def test_the_real_migrate_cli_refuses_without_the_authorization_channel():
    # WP-2C-A implements migrate, but only through the host tool's explicit stdin
    # channel. A direct `docker compose run migrate` exits 3 before it reads or connects.
    done = run_oneshot_cli("migrate")
    assert done.returncode == 3, done.stderr
    assert "authorization_channel" in done.stderr
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize("role", sorted(ONE_SHOTS))
@pytest.mark.parametrize("value", [None, "", "0", "false"])
def test_every_real_one_shot_cli_refuses_a_non_exact_activation_value(role, value):
    environ = cli_environment(role)
    environ.pop(preflight.ACTIVATION_VAR, None)
    if value is not None:
        environ[preflight.ACTIVATION_VAR] = value
    done = subprocess.run(
        [sys.executable, str(ONESHOT_PATH), role],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 78, done.stderr
    assert "oneshot_activation" in done.stderr
    assert "Traceback" not in done.stderr


def test_a_real_one_shot_cli_reports_a_wiring_refusal_without_a_traceback():
    done = run_oneshot_cli("migrate", PGUSER="hh_app")
    assert done.returncode == 78, done.stderr
    assert "oneshot_login" in done.stderr
    assert "Traceback" not in done.stderr


def test_a_real_one_shot_cli_without_a_role_is_a_usage_error():
    environ = cli_environment("migrate")
    done = subprocess.run(
        [sys.executable, str(ONESHOT_PATH)],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2
    assert "usage" in done.stderr
    assert "Traceback" not in done.stderr


# --- Compose as Compose resolves it ------------------------------------------------------------


def _compose_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        done = subprocess.run(
            [docker, "compose", "version"], capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


needs_compose = pytest.mark.skipif(not _compose_available(), reason="Docker Compose is not installed")

DECOY_ENV = (
    "HOUSE_HELPER_TELEGRAM_CLIENT_ID=placeholder-id\n"
    "HOUSE_HELPER_TELEGRAM_CLIENT_SECRET=placeholder-secret\n"
    "OPENAI_API_KEY=placeholder-key\n"
    "HOUSE_HELPER_PUBLIC_URL=https://decoy.example\n"
    "HOUSE_HELPER_DATABASE_URL=sqlite:///decoy.db\n"
)


def write_credentials(directory: Path, password: str = "placeholder-not-a-secret") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for login in (APP_LOGIN, *ONE_SHOTS.values()):
        database = "*" if login == "hh_restore" else DB_NAME
        path = directory / f"{login}.pgpass"
        path.write_text(f"{DB_HOST}:{DB_PORT}:{database}:{login}:{password}\n")
        if os.name == "posix":
            path.chmod(0o600)
    superuser = directory / "postgres_superuser.password"
    superuser.write_text(password + "\n")
    if os.name == "posix":
        superuser.chmod(0o600)


def base_environment(tmp_path: Path, **extra) -> dict:
    env_file = tmp_path / "placeholder.env"
    env_file.write_text(DECOY_ENV, encoding="utf-8")
    credentials = tmp_path / "credentials"
    write_credentials(credentials)
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HOUSE_HELPER_", "COMPOSE_"))
    }
    environ.update(
        {
            "HOUSE_HELPER_PG_ACTIVATE": "1",
            "HOUSE_HELPER_ENV_FILE": str(env_file),
            "HOUSE_HELPER_PG_CREDENTIALS_DIR": str(credentials),
        }
    )
    environ.update(extra)
    return environ


def resolve(tmp_path: Path, compose: Path = COMPOSE_PG, **extra) -> dict:
    environ = base_environment(tmp_path, **extra)
    profiles = ["--profile", "pg", "--profile", "ops"] if compose == COMPOSE_PG else ["--profile", "ops"]
    done = subprocess.run(
        ["docker", "compose", "-f", str(compose), *profiles, "config", "--format", "json"],
        env=environ,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_compose
def test_resolved_topology_is_isolated_and_unpublished(tmp_path):
    resolved = resolve(tmp_path)
    assert resolved["name"] == "house-helper-pg"
    assert set(resolved["services"]) == {"house-helper", "house-helper-db", *ONE_SHOTS}
    assert resolved["networks"]["db"]["name"] == "house-helper-pg-db"
    assert resolved["networks"]["db"]["internal"] is True
    assert resolved["networks"]["db"]["ipam"]["config"][0]["subnet"] == DB_SUBNET
    assert resolved["networks"]["proxy"]["name"] == "n8n_n8n_network"
    assert resolved["volumes"]["pgdata"]["name"] == "house-helper-pgdata"
    assert resolved["volumes"]["state"]["name"] == "house-helper-pg-state"
    for name, service in resolved["services"].items():
        assert not service.get("ports") and not service.get("expose"), name


@needs_compose
def test_resolved_database_is_on_the_database_network_only(tmp_path):
    db = resolve(tmp_path)["services"]["house-helper-db"]
    assert set(db["networks"]) == {"db"}
    assert db["image"] == PINNED_IMAGE
    assert db["container_name"] == DB_HOST
    assert db["profiles"] == ["pg"]
    assert not any(key.startswith("traefik.") for key in (db.get("labels") or {}))
    assert (db.get("labels") or {})["house-helper.pg.activation"] == "1"
    assert db["environment"]["POSTGRES_PASSWORD_FILE"] == "/run/credentials/postgres_superuser.password"
    assert "POSTGRES_PASSWORD" not in db["environment"]
    assert "POSTGRES_HOST_AUTH_METHOD" not in db["environment"]
    # Mode A's fresh-cluster inventory is the image's default postgres user/database.
    assert "POSTGRES_USER" not in db["environment"]
    assert "POSTGRES_DB" not in db["environment"]
    assert "config_file=/etc/house-helper/postgresql.conf" in db["command"]
    targets = {volume["target"] for volume in db["volumes"]}
    assert targets == {
        "/var/lib/postgresql/data",
        "/etc/house-helper/postgresql.conf",
        "/etc/house-helper/pg_hba.conf",
        "/run/credentials/postgres_superuser.password",
    }
    assert "read_only" not in db or db["read_only"] is not True


@needs_compose
def test_resolved_application_is_wired_to_postgres_and_not_to_sqlite(tmp_path):
    app = resolve(tmp_path)["services"]["house-helper"]
    assert set(app["networks"]) == {"proxy", "db"}
    assert app["profiles"] == ["pg"]
    assert app["environment"]["HOUSE_HELPER_DATABASE_URL"] == (
        f"postgresql+psycopg://{APP_LOGIN}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )
    assert app["environment"]["HOUSE_HELPER_STATE_DIR"] == "/state"
    assert app["environment"]["PGPASSFILE"] == f"/run/credentials/{APP_LOGIN}.pgpass"
    assert "password" not in app["environment"]["HOUSE_HELPER_DATABASE_URL"]
    targets = {volume["target"] for volume in app["volumes"]}
    assert targets == {"/state", f"/run/credentials/{APP_LOGIN}.pgpass"}
    assert "/data" not in targets
    assert app["read_only"] is True and app["cap_drop"] == ["ALL"]
    assert app["pull_policy"] == "never"


@needs_compose
def test_resolved_one_shots_have_exactly_one_credential_each(tmp_path):
    resolved = resolve(tmp_path)
    for role, login in ONE_SHOTS.items():
        service = resolved["services"][role]
        assert set(service["networks"]) == {"db"}, role
        assert service["profiles"] == ["ops"], role
        assert service["logging"] == {"driver": "none"}, role
        assert service["pull_policy"] == "never", role
        assert not service.get("env_file"), role
        assert not any(key.startswith("traefik.") for key in (service.get("labels") or {})), role
        bind_mounts = [
            volume
            for volume in service["volumes"]
            if volume["type"] == "bind" and volume["target"].startswith("/run/credentials/")
        ]
        assert [volume["target"] for volume in bind_mounts] == [
            f"/run/credentials/{login}.pgpass"
        ], role
        assert bind_mounts[0]["read_only"] is True
        assert service["environment"]["PGUSER"] == login
        assert service["environment"]["PGPASSFILE"] == f"/run/credentials/{login}.pgpass"
        assert service["environment"]["HOUSE_HELPER_STATE_DIR"] == "/state"
        assert service["entrypoint"] == ["python", "/opt/house-helper/ops/pg_oneshot.py"]
        if role == "restore":
            assert "PGDATABASE" not in service["environment"]
        else:
            assert service["environment"]["PGDATABASE"] == DB_NAME
    assert "postgres_superuser.password" not in {
        volume["target"] for volume in resolved["services"]["migrate"]["volumes"]
    }


@needs_compose
def test_the_web_traefik_labels_do_not_drift_from_the_sqlite_surface(tmp_path):
    sqlite = resolve(tmp_path, compose=COMPOSE_SQLITE)
    postgres = resolve(tmp_path)
    assert (
        sqlite["services"]["house-helper"]["labels"]
        == postgres["services"]["house-helper"]["labels"]
    ), "the deliberate cutover must not change the public routing shape"


@needs_compose
def test_an_ordinary_compose_invocation_without_the_guard_fails(tmp_path):
    environ = base_environment(tmp_path)
    del environ["HOUSE_HELPER_PG_ACTIVATE"]
    done = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_PG), "config", "-q"],
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode != 0
    assert "HOUSE_HELPER_PG_ACTIVATE" in done.stderr


@needs_compose
def test_the_resolved_surface_runs_the_tested_runtime_guard(tmp_path):
    resolved = resolve(tmp_path)
    assert preflight._resolved_entrypoint(
        resolved["services"][preflight.DB_SERVICE]
    ) == preflight.DB_ENTRYPOINT
    assert preflight._resolved_entrypoint(
        resolved["services"][preflight.APP_SERVICE]
    ) == preflight.APP_ENTRYPOINT
    # Compose ignores the image CMD when a non-null entrypoint is declared, so the web service
    # must carry the WP-2A start gate explicitly as the guard's argument.
    assert (
        resolved["services"][preflight.APP_SERVICE]["command"] == preflight.WEB_COMMAND
    )
    assert resolved["services"][preflight.DB_SERVICE]["command"] == preflight.DB_COMMAND
    for role in ONE_SHOTS:
        assert preflight._resolved_entrypoint(
            resolved["services"][role]
        ) == preflight.ONE_SHOT_ENTRYPOINT
    for name in (preflight.DB_SERVICE, preflight.APP_SERVICE, *ONE_SHOTS):
        assert (
            resolved["services"][name]["environment"][preflight.ACTIVATION_VAR]
            == preflight.ACTIVATION_VALUE
        )


@needs_compose
def test_explicit_service_selection_cannot_bypass_the_runtime_guard(tmp_path):
    # `0` passes Compose's required interpolation and activates the profile, so the file still
    # resolves. The service's own entrypoint is the layer that refuses it; naming the service
    # explicitly only selects that entrypoint. The full resolved argv (entrypoint + command)
    # is used, exactly as Compose hands it to the container.
    resolved = resolve(tmp_path, HOUSE_HELPER_PG_ACTIVATE="0")
    base = base_environment(tmp_path, HOUSE_HELPER_PG_ACTIVATE="0")
    for name in (preflight.DB_SERVICE, preflight.APP_SERVICE):
        service = resolved["services"][name]
        entrypoint = preflight._resolved_entrypoint(service)
        command = [str(item) for item in service.get("command") or []]
        for value in (None, "", "0", "false"):
            environ = dict(base)
            environ.pop(preflight.ACTIVATION_VAR, None)
            if value is not None:
                environ[preflight.ACTIVATION_VAR] = value
            done = subprocess.run(
                [_posix_shell(), *entrypoint[1:], *command],
                env=environ,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert done.returncode == 78, (name, value, done.stderr)
            assert "pg_activation" in done.stderr
            assert done.stdout == ""


@needs_compose
def test_activation_one_hands_the_complete_database_command_to_the_official_entrypoint(
    tmp_path,
):
    resolved = resolve(tmp_path)
    db = resolved["services"][preflight.DB_SERVICE]
    assert db["command"] == preflight.DB_COMMAND
    stub = tmp_path / "docker-entrypoint-stub.sh"
    stub.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8", newline="\n"
    )
    entrypoint = preflight._resolved_entrypoint(db)
    script = entrypoint[2].replace(
        "/usr/local/bin/docker-entrypoint.sh",
        '/bin/sh "%s"' % str(stub).replace("\\", "/"),
    )
    done = subprocess.run(
        [_posix_shell(), "-c", script, entrypoint[3], *preflight.DB_COMMAND],
        env=_guard_environment("1"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == preflight.DB_COMMAND


@needs_compose
def test_activation_one_hands_the_complete_web_command_to_the_start_gate(tmp_path):
    resolved = resolve(tmp_path)
    app = resolved["services"][preflight.APP_SERVICE]
    assert app["command"] == preflight.WEB_COMMAND
    # Replace only the `python` binary, via PATH: the guard must forward the rest of the exact
    # resolved command (the dbtool start gate and every uvicorn argument) unchanged.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "python"
    stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8", newline="\n")
    os.chmod(stub, 0o755)
    environ = _guard_environment("1")
    environ["PATH"] = str(bin_dir) + os.pathsep + environ.get("PATH", "")
    entrypoint = preflight._resolved_entrypoint(app)
    done = subprocess.run(
        [_posix_shell(), "-c", entrypoint[2], entrypoint[3], *preflight.WEB_COMMAND],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == preflight.WEB_COMMAND[1:]


@needs_compose
def test_an_application_env_file_cannot_override_the_activation_value(tmp_path):
    env_file = tmp_path / "placeholder.env"
    env_file.write_text(
        DECOY_ENV + "HOUSE_HELPER_PG_ACTIVATE=0\n", encoding="utf-8"
    )
    credentials = tmp_path / "credentials"
    write_credentials(credentials)
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HOUSE_HELPER_", "COMPOSE_"))
    }
    environ.update(
        {
            "HOUSE_HELPER_PG_ACTIVATE": "1",
            "HOUSE_HELPER_ENV_FILE": str(env_file),
            "HOUSE_HELPER_PG_CREDENTIALS_DIR": str(credentials),
        }
    )
    done = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_PG),
            "--profile",
            "pg",
            "--profile",
            "ops",
            "config",
            "--format",
            "json",
        ],
        env=environ,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.returncode == 0, done.stderr
    resolved = json.loads(done.stdout)
    # The explicit environment entry takes precedence over env_file.
    assert (
        resolved["services"][preflight.APP_SERVICE]["environment"][
            preflight.ACTIVATION_VAR
        ]
        == "1"
    )


@needs_compose
def test_the_preflight_accepts_the_exact_resolved_mount_sets(tmp_path):
    resolved = resolve(tmp_path)
    assert preflight.compose_problems(
        resolved, credentials_dir=tmp_path / "credentials"
    ) == []


@needs_compose
def test_the_preflight_refuses_an_extra_credential_mount_in_a_one_shot(tmp_path):
    resolved = resolve(tmp_path)
    credentials = tmp_path / "credentials"
    resolved["services"]["migrate"]["volumes"].append(
        {
            "type": "bind",
            "source": str(credentials / "hh_provision.pgpass"),
            "target": "/run/credentials/hh_provision.pgpass",
            "read_only": True,
        }
    )
    problems = preflight.compose_problems(resolved, credentials_dir=credentials)
    assert any("migrate" in problem and "allowed volumes" in problem for problem in problems)


@needs_compose
def test_the_preflight_refuses_a_replacement_one_shot_entrypoint(tmp_path):
    resolved = resolve(tmp_path)
    resolved["services"]["migrate"]["entrypoint"] = ["/bin/sh", "-c", "true"]
    problems = preflight.compose_problems(
        resolved, credentials_dir=tmp_path / "credentials"
    )
    assert any("operational entrypoint" in problem for problem in problems)


@needs_compose
def test_the_preflight_refuses_an_hba_source_that_only_shares_the_basename(tmp_path):
    resolved = resolve(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "pg_hba.conf").write_text("local all all trust\n", encoding="utf-8")
    for volume in resolved["services"][preflight.DB_SERVICE]["volumes"]:
        if volume.get("target") == "/etc/house-helper/pg_hba.conf":
            volume["source"] = str(decoy / "pg_hba.conf")
    problems = preflight.compose_problems(
        resolved, credentials_dir=tmp_path / "credentials"
    )
    assert any(
        preflight.DB_SERVICE in problem and "allowed volumes" in problem
        for problem in problems
    )


@needs_compose
def test_the_preflight_refuses_a_replacement_database_start_command(tmp_path):
    resolved = resolve(tmp_path)
    resolved["services"][preflight.DB_SERVICE]["command"] = ["postgres"]
    problems = preflight.compose_problems(
        resolved, credentials_dir=tmp_path / "credentials"
    )
    assert any("documented startup command" in problem for problem in problems)


@needs_compose
def test_the_preflight_refuses_a_replacement_web_start_command(tmp_path):
    resolved = resolve(tmp_path)
    resolved["services"][preflight.APP_SERVICE]["command"] = ["/bin/sh"]
    problems = preflight.compose_problems(
        resolved, credentials_dir=tmp_path / "credentials"
    )
    assert any("WP-2A start command" in problem for problem in problems)


def resolve_with_overlay(tmp_path: Path, overlay: str) -> dict:
    """Resolve the accepted surface merged with an overlay, exactly as Compose would run it."""
    environ = base_environment(tmp_path)
    overlay_path = tmp_path / "override.yaml"
    overlay_path.write_text(overlay, encoding="utf-8")
    done = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_PG),
            "-f",
            str(overlay_path),
            "--profile",
            "pg",
            "--profile",
            "ops",
            "config",
            "--format",
            "json",
        ],
        env=environ,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_compose
def test_the_preflight_refuses_a_configs_mounted_foreign_passfile_in_a_one_shot(tmp_path):
    # Codex bypass 1: Compose `configs` (not `volumes`) places hh_provision's passfile inside
    # the migrate one-shot. The resolved volume list is unchanged, so the exact mount
    # allowlist alone accepts it.
    passfile = tmp_path / "credentials" / "hh_provision.pgpass"
    resolved = resolve_with_overlay(
        tmp_path,
        "services:\n"
        "  migrate:\n"
        "    configs:\n"
        "      - source: provision_pass\n"
        "        target: /run/credentials/hh_provision.pgpass\n"
        "configs:\n"
        "  provision_pass:\n"
        f"    file: {passfile.as_posix()}\n",
    )
    migrate = resolved["services"]["migrate"]
    assert [entry["target"] for entry in migrate["configs"]] == [
        "/run/credentials/hh_provision.pgpass"
    ]
    # The injected file never appears in the mount set the allowlist compares.
    assert "/run/credentials/hh_provision.pgpass" not in {
        volume["target"] for volume in migrate["volumes"]
    }
    problems = preflight.compose_problems(
        resolved, credentials_dir=tmp_path / "credentials"
    )
    assert any(
        "migrate" in problem and "Compose config" in problem for problem in problems
    ), problems


@needs_compose
def test_the_preflight_refuses_a_configs_override_of_the_one_shot_entrypoint(tmp_path):
    # Codex bypass 2: a config replaces /opt/house-helper/ops/pg_oneshot.py with an existing
    # file while the checked entrypoint array stays exactly intact.
    resolved = resolve_with_overlay(
        tmp_path,
        "services:\n"
        "  migrate:\n"
        "    configs:\n"
        "      - source: oneshot_code\n"
        "        target: /opt/house-helper/ops/pg_oneshot.py\n"
        "configs:\n"
        "  oneshot_code:\n"
        f"    file: {(DEPLOY / 'ops' / 'dbtool.py').as_posix()}\n",
    )
    migrate = resolved["services"]["migrate"]
    assert preflight._resolved_entrypoint(migrate) == preflight.ONE_SHOT_ENTRYPOINT
    assert [entry["target"] for entry in migrate["configs"]] == [
        "/opt/house-helper/ops/pg_oneshot.py"
    ]
    problems = preflight.compose_problems(
        resolved, credentials_dir=tmp_path / "credentials"
    )
    assert any(
        "migrate" in problem and "Compose config" in problem for problem in problems
    ), problems


@needs_compose
def test_every_service_refuses_configs_secrets_and_volumes_from(tmp_path):
    # None of the seven services uses these Compose-managed file channels; each is refused
    # everywhere, so no service is a weaker boundary than another.
    resolved = resolve(tmp_path)
    credentials = tmp_path / "credentials"
    assert preflight.compose_problems(resolved, credentials_dir=credentials) == []
    expected = {
        "configs": "Compose config",
        "secrets": "top-level secrets",
        "volumes_from": "inherits volumes",
    }
    for name in (preflight.DB_SERVICE, preflight.APP_SERVICE, *ONE_SHOTS):
        for channel, marker in expected.items():
            mutated = copy.deepcopy(resolved)
            mutated["services"][name][channel] = ["placeholder"]
            problems = preflight.compose_problems(mutated, credentials_dir=credentials)
            assert len(problems) == 1, (name, channel, problems)
            assert name in problems[0] and marker in problems[0], (
                name,
                channel,
                problems,
            )


@needs_compose
def test_the_preflight_refuses_a_non_read_only_credential_mount(tmp_path):
    resolved = resolve(tmp_path)
    app = resolved["services"][preflight.APP_SERVICE]
    for volume in app["volumes"]:
        if volume.get("target") == f"/run/credentials/{APP_LOGIN}.pgpass":
            volume["read_only"] = False
    problems = preflight.compose_problems(
        resolved, credentials_dir=tmp_path / "credentials"
    )
    assert any(
        preflight.APP_SERVICE in problem and "allowed volumes" in problem
        for problem in problems
    )
