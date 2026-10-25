"""The deployment kit (Dockerfile, .dockerignore, deploy/): its invariants, as tests.

These read files; they build no image and start no container. They are the part of the kit
that CI can keep true on every change:

  * the Dockerfile runs as a numeric non-root user, pins its base image by digest, copies only
    named things, bakes in no credential, keeps migrations out of the start-up command and runs
    one worker;
  * the build context is an allowlist, so a developer's real .env, a database or the tests can
    never be copied into an image, whatever the Dockerfile later says;
  * the environment template uses only variable names the application reads, and the
    application's own validator accepts it;
  * the logging configuration keeps SQL and HTTP-client detail out of the logs;
  * the runbook names no variable and no file that does not exist.

The Compose file is checked in two ways: text rules that always run (no `ports:` anywhere is the
one that must never be lost), and, where Docker Compose is installed, the file as Compose itself
resolves it (it needs no daemon and creates nothing).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from house_helper.core import provider_settings
from house_helper.web import settings as web_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
DEPLOY = REPO_ROOT / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
ENV_EXAMPLE = DEPLOY / "house-helper.env.example"
LOGGING = DEPLOY / "logging.json"
RUNBOOK = DEPLOY / "RUNBOOK.md"
GITIGNORE = REPO_ROOT / ".gitignore"

HOST = "house.elivcloud.org"
# A variable name that holds a secret: the word is a whole underscore-separated part of it
# (OPENAI_API_KEY, ..._CLIENT_SECRET, BOT_TOKEN), so MAX_TOKENS is not one.
SECRET_NAME = re.compile(r"(^|_)(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)(_|$)")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_lines(path: Path) -> list:
    """The file's lines without comments and blanks."""
    return [
        line
        for line in read(path).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def dockerfile_instructions() -> list:
    """(INSTRUCTION, arguments) with continuation lines joined and comments dropped."""
    joined, current = [], ""
    for raw in read(DOCKERFILE).splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if line.startswith("#"):
            continue
        current += (" " if current else "") + line.rstrip("\\").strip()
        if not raw.rstrip().endswith("\\"):
            joined.append(current)
            current = ""
    instructions = []
    for item in joined:
        name, _, arguments = item.partition(" ")
        instructions.append((name.upper(), arguments.strip()))
    return instructions


def known_variable_names() -> set:
    names = {
        value
        for module in (web_settings, provider_settings)
        for name, value in vars(module).items()
        if name.endswith("_ENV") and isinstance(value, str)
    }
    assert {"HOUSE_HELPER_PUBLIC_URL", "OPENAI_API_KEY", "HOUSE_HELPER_DATABASE_URL"} <= names
    return names


# Read by the operator tool (deploy/ops/dbtool.py), not by the application.
TOOL_VARIABLES = {"HOUSE_HELPER_STAGING_DIR"}

# Read by the cleanup's systemd service (deploy/systemd/, R4C-D2) from its environment file, not by
# the application, the operator tool or Compose itself.
SCHEDULER_VARIABLES = {"HOUSE_HELPER_CHECKOUT"}

COMPOSE_ONLY_VARIABLES = {
    "HOUSE_HELPER_HOST",
    "HOUSE_HELPER_ENV_FILE",
    "HOUSE_HELPER_IMAGE_TAG",
    "HOUSE_HELPER_GIT_SHA",
    "HOUSE_HELPER_HSTS_SECONDS",
}


# --- Dockerfile ---------------------------------------------------------------------------


def test_the_base_image_is_python_312_pinned_by_digest():
    text = read(DOCKERFILE)
    match = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", text, re.MULTILINE)
    assert match, "the base image is one ARG, used by both stages"
    assert re.fullmatch(r"python:3\.12-[a-z0-9.-]+@sha256:[0-9a-f]{64}", match.group(1))
    froms = [args for name, args in dockerfile_instructions() if name == "FROM"]
    assert froms and all(args.startswith("${PYTHON_IMAGE}") for args in froms)


def test_the_image_runs_as_a_numeric_non_root_user_set_after_the_files_are_copied():
    instructions = dockerfile_instructions()
    users = [i for i, (name, _) in enumerate(instructions) if name == "USER"]
    assert len(users) == 1
    user = instructions[users[0]][1]
    assert re.fullmatch(r"[1-9][0-9]*(:[1-9][0-9]*)?", user), "numeric, not root"
    last_copy = max(i for i, (name, _) in enumerate(instructions) if name == "COPY")
    assert users[0] > last_copy, "files are root-owned and not writable by the application"
    assert "install --directory --owner app --group app" in read(DOCKERFILE)  # /data


def test_the_dockerfile_copies_only_named_sources_and_never_adds_or_copies_everything():
    allowed = {"requirements.txt", "house_helper", "alembic.ini", "deploy/ops", "deploy/logging.json"}
    for name, args in dockerfile_instructions():
        assert name != "ADD"
        if name != "COPY":
            continue
        words = args.split()
        if words[0].startswith("--from="):
            continue
        sources = [w for w in words[:-1] if not w.startswith("--")]
        assert set(sources) <= allowed, sources
        assert "." not in sources and "./" not in sources


def test_no_credential_is_baked_into_the_image():
    for name, args in dockerfile_instructions():
        if name in ("ENV", "ARG"):
            for assignment in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", args):
                assert not SECRET_NAME.search(assignment.upper()), assignment
    assert ".env" not in re.sub(r"#.*", "", read(DOCKERFILE))


def test_the_start_command_takes_the_lock_and_checks_the_schema_but_never_migrates_and_runs_one_worker():
    cmd = [args for name, args in dockerfile_instructions() if name == "CMD"]
    assert len(cmd) == 1
    command = cmd[0]
    assert command.startswith("[")
    words = json.loads(command)  # exec form
    # `serve` takes the shared maintenance lock, checks the schema read only, then becomes the
    # program after `--` (uvicorn), which holds the lock for as long as it runs.
    assert words[:5] == ["python", "/opt/house-helper/ops/dbtool.py", "serve", "--", "uvicorn"]
    assert "sh" not in words and "-c" not in words, "no shell: uvicorn must be the process that holds the lock"
    for forbidden in ("alembic", "upgrade", "init_database", "--workers", "--reload"):
        assert forbidden not in command
    for required in ("--no-access-log", "--lifespan", "--log-config", "--timeout-graceful-shutdown"):
        assert required in command
    assert "house_helper.web.asgi:app" in command


def test_the_staging_directory_for_the_operator_tool_exists_owned_by_the_application_user():
    text = read(DOCKERFILE)
    assert re.search(r"install --directory --owner app --group app --mode 0700 /data /staging", text)
    env = " ".join(args for name, args in dockerfile_instructions() if name == "ENV")
    assert "HOUSE_HELPER_STAGING_DIR=/staging" in env


def test_the_healthcheck_asks_the_liveness_endpoint_and_nothing_else_is_exposed():
    instructions = dockerfile_instructions()
    health = [args for name, args in instructions if name == "HEALTHCHECK"]
    assert len(health) == 1 and "/healthz" in health[0] and "127.0.0.1:8000" in health[0]
    exposed = [args for name, args in instructions if name == "EXPOSE"]
    assert exposed == ["8000"]
    assert not any(name == "VOLUME" for name, _ in instructions)


def test_python_never_writes_bytecode_into_the_read_only_image():
    env = " ".join(args for name, args in dockerfile_instructions() if name == "ENV")
    assert "PYTHONDONTWRITEBYTECODE=1" in env and "PYTHONPATH=/app" in env


# --- .dockerignore ------------------------------------------------------------------------


def test_the_build_context_is_an_allowlist_that_never_readmits_secrets_or_data():
    lines = code_lines(DOCKERIGNORE)
    assert lines[0] == "*"
    readmitted = [line[1:] for line in lines if line.startswith("!")]
    for path in readmitted:
        assert not re.search(r"(^|/)\.env|\.db|\.sqlite|\.env\b|secret|tests|\.git\b", path), path
    copied = set()
    for name, args in dockerfile_instructions():
        if name == "COPY" and not args.startswith("--from="):
            copied.update(w for w in args.split()[:-1] if not w.startswith("--"))
    assert copied <= set(readmitted), "every COPY source is readmitted, and only those"
    assert set(readmitted) <= copied | {"requirements.txt"}


def test_a_real_env_file_and_databases_are_not_in_the_allowlisted_directories():
    # What the allowlist admits must itself hold no secret or data file today.
    for directory in (REPO_ROOT / "house_helper", DEPLOY / "ops"):
        for path in directory.rglob("*"):
            if path.is_file():
                assert not re.search(r"\.env($|\.)|\.db($|-)|\.sqlite", path.name), path


def test_the_gitignore_keeps_real_environment_files_and_backups_out_of_the_repository():
    text = read(GITIGNORE)
    assert "deploy/*.env" in text and "*.db" in text and ".env" in text
    assert "deploy/*.env" not in "".join(
        line for line in text.splitlines() if "example" in line
    )


# --- Compose: text rules that always run ----------------------------------------------------


def test_the_compose_file_publishes_no_port_and_exposes_no_debug_endpoint():
    lines = code_lines(COMPOSE)
    assert not [line for line in lines if re.match(r"\s*(ports|expose)\s*:", line)]
    text = "\n".join(lines)
    assert "api.insecure" not in text and "dashboard" not in text.lower()
    assert "network_mode: host" not in text and "privileged" not in text


def test_the_compose_file_runs_no_migration_and_holds_no_secret():
    text = "\n".join(code_lines(COMPOSE))
    assert "alembic" not in text and "upgrade" not in text
    assert not re.search(r"(?i)(api_key|secret|token|password)\s*[:=]\s*\S", text)
    assert "env_file: ${HOUSE_HELPER_ENV_FILE:-house-helper.env}" in text
    assert text.count("env_file") == 1, "only the application gets the credentials, not the ops service"


def test_the_compose_file_joins_the_existing_network_and_uses_the_existing_resolver():
    text = "\n".join(code_lines(COMPOSE))
    assert re.search(r"proxy:\s*\n\s*external: true\s*\n\s*name: n8n_n8n_network", text)
    assert text.count('tls.certresolver: "letsencrypt"') == 4
    assert text.count('entrypoints: "websecure"') == 4
    assert "entrypoints.web." not in text and ".entrypoints: web\"" not in text


# --- Compose: as Compose resolves it ----------------------------------------------------------


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


def resolve(tmp_path, **variables) -> dict:
    env_file = tmp_path / "placeholder.env"
    env_file.write_text(DECOY_ENV, encoding="utf-8")
    environ = {k: v for k, v in os.environ.items() if not k.startswith(("HOUSE_HELPER_", "COMPOSE_"))}
    environ["HOUSE_HELPER_ENV_FILE"] = str(env_file)
    environ.update(variables)
    done = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "--profile", "ops", "config", "--format", "json"],
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def labels_of(service: dict) -> dict:
    return service["labels"]


def routers(service: dict) -> dict:
    found = {}
    for key, value in labels_of(service).items():
        match = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.(.+)", key)
        if match:
            found.setdefault(match.group(1), {})[match.group(2)] = value
    return found


@needs_compose
def test_resolved_application_service_is_hardened_and_unpublished(tmp_path):
    web = resolve(tmp_path)["services"]["house-helper"]
    assert "ports" not in web
    assert web["read_only"] is True
    assert web["cap_drop"] == ["ALL"]
    assert web["security_opt"] == ["no-new-privileges:true"]
    assert web["restart"] == "unless-stopped" and web["init"] is True
    assert web["pull_policy"] == "never"
    assert set(web["networks"]) == {"proxy"}
    assert web["logging"]["driver"] == "json-file"
    assert web["logging"]["options"]["max-size"] and web["logging"]["options"]["max-file"]
    limits = web["deploy"]["resources"]["limits"]
    assert int(limits["memory"]) > 0 and float(limits["cpus"]) > 0 and int(limits["pids"]) > 0
    assert [v["target"] for v in web["volumes"]] == ["/data"]  # no /staging: that is the operator's
    assert any(t.startswith("/tmp:") and "noexec" in t and "size=64m" in t for t in web["tmpfs"])
    assert not web.get("entrypoint")  # the image's own start (dbtool serve) is what takes the lock


@needs_compose
def test_compose_decides_the_database_and_the_public_url_not_the_environment_file(tmp_path):
    resolved = resolve(tmp_path)
    environment = resolved["services"]["house-helper"]["environment"]
    assert environment["HOUSE_HELPER_DATABASE_URL"] == "sqlite:////data/house_helper.db"
    assert environment["HOUSE_HELPER_PUBLIC_URL"] == f"https://{HOST}"  # not the decoy
    assert environment["OPENAI_API_KEY"] == "placeholder-key"  # the env file does reach it
    proxy = resolved["networks"]["proxy"]
    assert (proxy["name"], proxy["external"]) == ("n8n_n8n_network", True)
    assert resolved["volumes"]["data"]["name"] == "house-helper-data"


@needs_compose
def test_the_public_url_and_every_router_follow_the_one_host_variable(tmp_path):
    resolved = resolve(tmp_path, HOUSE_HELPER_HOST="beta.example.test")
    web = resolved["services"]["house-helper"]
    assert web["environment"]["HOUSE_HELPER_PUBLIC_URL"] == "https://beta.example.test"
    for name, router in routers(web).items():
        assert router["rule"].startswith("Host(`beta.example.test`)"), name
        assert HOST not in router["rule"]


@needs_compose
def test_the_routers_have_explicit_priorities_tls_and_the_headers_first(tmp_path):
    web = resolve(tmp_path)["services"]["house-helper"]
    assert labels_of(web)["traefik.enable"] == "true"
    assert labels_of(web)["traefik.docker.network"] == "n8n_n8n_network"
    found = routers(web)
    assert set(found) == {"house-helper", "house-helper-api", "house-helper-auth", "house-helper-signin"}
    for name, router in found.items():
        assert router["entrypoints"] == "websecure", name
        assert router["tls"] == "true" and router["tls.certresolver"] == "letsencrypt", name
        assert router["service"] == "house-helper", name
        assert router["rule"].startswith(f"Host(`{HOST}`)"), name
        assert re.fullmatch(r"[0-9]+", router["priority"]), name
        assert router["middlewares"].split(",")[0] == "house-helper-headers", name
    priority = {name: int(router["priority"]) for name, router in found.items()}
    assert priority["house-helper-signin"] > priority["house-helper-auth"] > priority["house-helper"]
    assert priority["house-helper-api"] > priority["house-helper"]
    # Every rule a request can match is more specific than the catch-all, by priority.
    assert found["house-helper"]["rule"] == f"Host(`{HOST}`)"


@needs_compose
def test_the_sign_in_starting_requests_have_the_strictest_limit(tmp_path):
    web = resolve(tmp_path)["services"]["house-helper"]
    found = routers(web)
    rule = found["house-helper-signin"]["rule"]
    for path in ("/auth/telegram/login", "/auth/invite/start", "/auth/email/"):
        assert f"`{path}`" in rule
    assert "PathPrefix(`/auth/`)" in found["house-helper-auth"]["rule"]
    assert "PathPrefix(`/api/`)" in found["house-helper-api"]["rule"]

    def limit(name):
        prefix = f"traefik.http.middlewares.{name}.ratelimit."
        return {k[len(prefix):]: v for k, v in labels_of(web).items() if k.startswith(prefix)}

    strict, auth, api = (limit(f"house-helper-limit-{n}") for n in ("signin", "auth", "api"))
    for bucket in (strict, auth, api):
        assert set(bucket) == {"average", "period", "burst"}
    assert int(strict["average"]) < int(auth["average"]) and int(strict["burst"]) < int(auth["burst"])
    assert found["house-helper-signin"]["middlewares"].endswith("house-helper-limit-signin")
    assert found["house-helper-auth"]["middlewares"].endswith("house-helper-limit-auth")
    assert found["house-helper-api"]["middlewares"].endswith("house-helper-limit-api")


@needs_compose
def test_the_proxy_adds_hsts_but_never_a_csp_or_referrer_policy(tmp_path):
    web = resolve(tmp_path)["services"]["house-helper"]
    prefix = "traefik.http.middlewares.house-helper-headers.headers."
    headers = {k[len(prefix):]: v for k, v in labels_of(web).items() if k.startswith(prefix)}
    assert int(headers["stsSeconds"]) > 0
    assert "stsIncludeSubdomains" not in headers and "stsPreload" not in headers
    assert not any("policy" in key.lower() or "custom" in key.lower() for key in headers)
    assert set(headers) == {"stsSeconds", "contentTypeNosniff", "frameDeny"}


@needs_compose
def test_the_ops_service_has_no_network_credentials_or_log_driver(tmp_path):
    ops = resolve(tmp_path)["services"]["house-helper-ops"]
    assert ops["network_mode"] == "none" and "networks" not in ops
    assert ops["logging"] == {"driver": "none"}
    assert ops["environment"] == {
        "HOUSE_HELPER_DATABASE_URL": "sqlite:////data/house_helper.db",
        "HOUSE_HELPER_STAGING_DIR": "/staging",
    }
    assert ops["read_only"] is True and ops["cap_drop"] == ["ALL"]
    assert ops["restart"] == "no" and ops["healthcheck"] == {"disable": True}
    assert "ports" not in ops and "labels" not in ops
    assert ops["profiles"] == ["ops"]


@needs_compose
def test_the_ops_entrypoint_is_the_operator_tool_so_every_database_command_goes_through_its_lock(tmp_path):
    ops = resolve(tmp_path)["services"]["house-helper-ops"]
    assert ops["entrypoint"] == ["python", "/opt/house-helper/ops/dbtool.py"]
    assert ops["command"] == ["--help"]
    assert ops["init"] is True  # signals reach the program a migration exec()s


@needs_compose
def test_the_ops_service_has_a_disk_backed_private_staging_volume_of_its_own(tmp_path):
    resolved = resolve(tmp_path)
    ops = resolved["services"]["house-helper-ops"]
    staging = [v for v in ops["volumes"] if v["target"] == "/staging"]
    assert len(staging) == 1
    assert staging[0]["type"] == "volume" and not staging[0].get("source"), "anonymous: per run, removed by --rm"
    assert "bind" not in staging[0]
    data = [v for v in ops["volumes"] if v["target"] == "/data"]
    assert [v["source"] for v in data] == ["data"]
    assert not any(t.startswith("/staging") for t in ops.get("tmpfs", [])), "not a memory tmpfs"
    # The scratch space is not the 64 MB memory /tmp, which stays small and hardened.
    assert any(t.startswith("/tmp:") and "size=64m" in t and "noexec" in t for t in ops["tmpfs"])
    assert int(ops["deploy"]["resources"]["limits"]["memory"]) <= 512 * (1 << 20)
    assert set(resolved["volumes"]) == {"data"}, "no named volume for scratch copies to leak into"


@needs_compose
def test_the_ops_service_is_not_started_by_up(tmp_path):
    env_file = tmp_path / "e.env"
    env_file.write_text(DECOY_ENV, encoding="utf-8")
    environ = {**os.environ, "HOUSE_HELPER_ENV_FILE": str(env_file)}
    done = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--services"],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0 and done.stdout.split() == ["house-helper"]


# --- the environment template ---------------------------------------------------------------


def parse_env(path: Path) -> dict:
    values = {}
    for line in code_lines(path):
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


def test_the_environment_template_uses_only_names_the_application_reads():
    values = parse_env(ENV_EXAMPLE)
    assert values, "the template defines something"
    assert set(values) <= known_variable_names(), set(values) - known_variable_names()


def test_the_environment_template_holds_no_secret_and_leaves_compose_owned_names_alone():
    values = parse_env(ENV_EXAMPLE)
    for name, value in values.items():
        if SECRET_NAME.search(name):
            assert value == "", name
    for name in ("HOUSE_HELPER_DATABASE_URL", "HOUSE_HELPER_PUBLIC_URL", "BOT_TOKEN"):
        assert name not in values
    assert not [n for n in values if n.startswith(("HOUSE_HELPER_SMTP", "HOUSE_HELPER_EMAIL"))]


def test_the_template_as_it_stands_starts_the_application_with_sign_in_off():
    settings = web_settings.load_web_settings(
        {
            **parse_env(ENV_EXAMPLE),
            "HOUSE_HELPER_DATABASE_URL": "sqlite:////data/house_helper.db",
            "HOUSE_HELPER_PUBLIC_URL": f"https://{HOST}",
        }
    )
    assert settings.auth is None and settings.email is None and settings.provider is None


def test_the_template_filled_in_is_accepted_by_the_application_and_its_defaults_are_the_applications():
    values = parse_env(ENV_EXAMPLE)
    filled = {
        **values,
        "HOUSE_HELPER_TELEGRAM_CLIENT_ID": "123456789",
        "HOUSE_HELPER_TELEGRAM_CLIENT_SECRET": "placeholder-secret",
        "OPENAI_API_KEY": "placeholder-key",
        "HOUSE_HELPER_DATABASE_URL": "sqlite:////data/house_helper.db",
        "HOUSE_HELPER_PUBLIC_URL": f"https://{HOST}",
    }
    settings = web_settings.load_web_settings(filled)
    assert settings.auth is not None and settings.auth.redirect_uri == f"https://{HOST}/auth/telegram/callback"
    assert settings.email is None
    provider = settings.provider
    assert provider is not None
    default = provider_settings.ProviderSettings(api_key="x")
    assert (
        provider.model,
        provider.temperature,
        provider.max_tokens,
        provider.timeout_seconds,
        provider.max_retries,
    ) == (
        default.model,
        default.temperature,
        default.max_tokens,
        default.timeout_seconds,
        default.max_retries,
    )


# --- logging configuration --------------------------------------------------------------------


def test_the_logging_configuration_shows_application_logs_and_hides_sql_and_client_detail():
    config = json.loads(read(LOGGING))  # valid JSON
    script = (
        "import json, logging, logging.config, sys\n"
        f"logging.config.dictConfig(json.load(open({str(LOGGING)!r}, encoding='utf-8')))\n"
        "names = ['house_helper.web.app', 'uvicorn.error', 'sqlalchemy.engine', 'httpx', "
        "'httpcore.http11', 'openai._base_client', 'some.third.party']\n"
        "print(json.dumps({n: logging.getLevelName(logging.getLogger(n).getEffectiveLevel()) "
        "for n in names}))\n"
        "print(json.dumps(sorted(h.stream.name for h in logging.getLogger().handlers)))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    levels = json.loads(done.stdout.splitlines()[0])
    assert levels["house_helper.web.app"] == "INFO" and levels["uvicorn.error"] == "INFO"
    for quiet in ("sqlalchemy.engine", "httpx", "httpcore.http11", "openai._base_client", "some.third.party"):
        assert levels[quiet] == "WARNING", quiet  # SQL parameters hold the nonce and verifier
    assert "<stdout>" in json.loads(done.stdout.splitlines()[1])
    assert config["disable_existing_loggers"] is False


# --- the runbook ------------------------------------------------------------------------------


def test_the_runbook_names_no_variable_the_application_does_not_read():
    text = read(RUNBOOK)
    mentioned = set(re.findall(r"\b(?:HOUSE_HELPER_[A-Z_]+|OPENAI_[A-Z_]+|TEMPERATURE|MAX_TOKENS|BOT_TOKEN)\b", text))
    allowed = known_variable_names() | COMPOSE_ONLY_VARIABLES | TOOL_VARIABLES | SCHEDULER_VARIABLES | {"BOT_TOKEN"}
    # Prefix mentions such as HOUSE_HELPER_SMTP_* are written with a trailing wildcard.
    unknown = {
        name
        for name in mentioned
        if name not in allowed and not re.search(rf"{re.escape(name)}\*", text)
    }
    assert unknown == set(), unknown


def test_every_file_the_runbook_points_at_exists():
    text = read(RUNBOOK)
    pointed = set(re.findall(r"`((?:deploy|house_helper|tests)/[A-Za-z0-9_./-]+)`", text))
    pointed |= {"Dockerfile", ".dockerignore"} & set(re.findall(r"`([A-Za-z.]+)`", text))
    assert pointed, "the runbook references files"
    # The default environment file is git-ignored by design: the runbook names it, the
    # repository never holds it.
    generated = {"deploy/house-helper.env"}
    missing = [
        p for p in sorted(pointed - generated) if not (REPO_ROOT / p).exists() and not p.endswith("*")
    ]
    assert missing == [], missing


def test_the_runbook_keeps_the_conditions_it_promises():
    text = read(RUNBOOK)
    for needed in (
        "CODE RELEASED IN GIT (R4C-D2, `489ff30`)",  # the purge and cleanup gates name the released code; where it runs is the gate tests' business
        "RequestPath=drop",  # the verified proxy log setting
        "`RequestPath=redact`, or `queryparameters.defaultmode=redact` | **still leaks**",
        "alembic downgrade` is **not** a rollback",
        "No AAAA",
        "[OWNER]",
        "REQUIRES LIVE",
    ):
        assert needed in text, needed
    assert HOST in text and "n8n_n8n_network" in text
    assert "0006_oidc_state_invitation" in text


# --- the runbook after the audit ------------------------------------------------------------------


def section(number: int) -> str:
    """The text of '## <number>. ...' up to the next '## ' heading."""
    text = read(RUNBOOK)
    start = re.search(rf"^## {number}\. ", text, re.MULTILINE)
    following = re.search(r"^## \d+\. ", text[start.end():], re.MULTILINE)
    return text[start.start(): start.end() + following.start()] if following else text[start.start():]


def fenced_blocks(text: str) -> list:
    return re.findall(r"```(?:bash)?\n(.*?)```", text, re.DOTALL)


def retention_gate_rows() -> list:
    """The two retention rows of the gate table in section 12, whitespace-flattened."""
    names = ("Scheduled cleanup", "365-day physical deletion")
    rows = [
        " ".join(line.split())
        for line in section(12).splitlines()
        if line.startswith(tuple(f"| **{name}**" for name in names))
    ]
    assert len(rows) == len(names), rows
    return rows


FIRST_RUN = r"first (?:automatic |calendar-triggered |scheduled )*(?:run|execution)\b"


def assert_first_scheduled_run_verified(text: str, where: str) -> None:
    """The cleanup as the documents report it on 2026-10-25: the timer is installed and active, and its first
    calendar-triggered run (2026-10-25) was verified and accepted by the owner. Those are two facts and the first
    never implies the second, so each has its own check, and the wording of 2026-10-24 ("not yet verified") must
    not be left standing beside the new one. The wording around them is free."""
    flat_text = " ".join(text.split())
    assert re.search(r"installed and active", flat_text, re.I), where
    assert re.search(FIRST_RUN + r"[^.|]*?\bverified and accepted", flat_text, re.I), where
    assert not re.search(FIRST_RUN + r"[^.|]*?\bnot (?:yet )?(?:been )?verified", flat_text, re.I), where


def test_no_unsafe_command_of_the_first_version_is_left_in_the_runbook():
    text = read(RUNBOOK)
    for gone in (
        '> "$F.part"',  # a redirect that clobbers
        'mv "$F.part"',  # a rename that replaces
        "install -m 600 deploy/house-helper.env.example",  # overwrites a live secret file
        "hhops python",  # the ops entrypoint is the tool now
        "hhops alembic",  # a migration without the lock
        "$DBTOOL",
        "export OLD_TAG",  # a tag carried in shell state
    ):
        assert gone not in text, gone
    for block in fenced_blocks(text):
        for line in block.splitlines():
            code = line.split("#")[0]
            assert not re.search(r"\balembic (upgrade|downgrade)\b", code) or "exec --exclusive --" in code, line


def test_the_upgrade_is_the_script_and_no_block_in_its_section_runs_a_migration_by_hand():
    text = section(9)
    assert "deploy/upgrade.sh --old-tag" in text and "--dry-run" in text
    assert "refuses to migrate unless a backup" in text
    for block in fenced_blocks(text):
        assert "alembic" not in block and "hhops" not in block, block
    for needed in ("set -euo pipefail", "--require-head", "do not retry", "docker image prune -a"):
        assert needed in text, needed


def test_every_hhops_command_in_the_runbook_is_a_command_of_the_tool():
    import importlib.util

    spec = importlib.util.spec_from_file_location("deploy_dbtool_kit", DEPLOY / "ops" / "dbtool.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    commands = set(tool._build_parser()._subparsers._group_actions[0].choices)
    assert {"serve", "exec", "backup", "verify", "restore", "check-idle", "check-schema"} <= commands
    used = set()
    for block in fenced_blocks(read(RUNBOOK)):
        for line in block.splitlines():
            code = line.split("#")[0]
            for match in re.finditer(r"\bhhops (?:--\S+ )?([a-z][a-z-]*)(.*)", code):
                used.add(match.group(1))
                if match.group(1) == "exec":
                    assert re.match(r"\s+--(shared|exclusive) -- \S", match.group(2)), line
    assert used, "the runbook shows hhops commands"
    assert used <= commands, used - commands


def test_every_option_the_runbook_gives_a_script_exists_in_that_script():
    for script in ("backup.sh", "upgrade.sh"):
        source = read(DEPLOY / script)
        for line in read(RUNBOOK).splitlines():
            if f"deploy/{script}" in line and "bash" in line:
                for option in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", line.split("#")[0]):
                    assert f"{option})" in source, (script, option)


def test_the_retention_requirements_are_gates_that_nothing_in_the_kit_waives():
    text = read(RUNBOOK)
    assert "time-box" not in text.lower()
    gates = section(12)
    assert "Pre-live gate 1: required before real use" in gates
    assert "Pre-live gate 2: required before any real user data is stored; not waived" in gates
    # R4C-D2 built both and its code is released. They are stated as exactly that (code in Git, timer
    # installed, first calendar-triggered run verified and accepted on 2026-10-25), and as closed on that run
    # alone: not as missing, not as waived, and not any longer as code that runs nowhere or waits for its first run.
    for row in retention_gate_rows():
        assert "**CODE RELEASED IN GIT (R4C-D2, `489ff30`);" in row, row
        assert_first_scheduled_run_verified(row, row[:60])
        assert "NOT SCHEDULED ON ANY HOST" not in row and "NOT DEPLOYED" not in row, row
    assert gates.count("**Closed on 2026-10-25") == 2
    assert "**Open until**" not in gates
    # The closure covers the activation only: the run deleted nothing, and the backup row stays a separate blocker.
    assert "deleted nothing" in " ".join(gates.split()) and "has not been observed" in " ".join(gates.split())
    backup = next(line for line in gates.splitlines() if line.startswith("| **Backup retention and off-host**"))
    assert "BLOCKER before real data" in backup and "undecided and not installed" in backup
    assert "NOT IMPLEMENTED" not in gates
    assert "substitutes for either of them" in gates
    assert "outside the kit R4C-D1 released" in " ".join(gates.split())  # the code is a change to the application
    assert "STATUS.md" in gates


def test_the_runbook_separates_a_spend_alert_from_an_enforced_hard_limit():
    text = read(RUNBOOK)
    assert "https://developers.openai.com/api/docs/guides/spend-limits" in text
    spend = text[text.index("### 4.2 OpenAI spend limits"): text.index("## 5. First install")]
    flat = " ".join(spend.split())
    for needed in (
        "**it does not stop API traffic**",
        "Enforce a hard limit",
        "project_spend_limit_exceeded",
        "not instantaneous",
        "lower thresholds",
        "dedicated OpenAI project",
        "Setting an amount without turning the switch on gives alerts only",
    ):
        assert needed in flat, needed
    assert not re.search(r"\$\s?\d", spend), "no monetary budget is invented"


def test_the_traefik_preflight_covers_every_source_and_every_sink_with_a_canary():
    text = read(RUNBOOK)
    preflight = text[text.index("### 4.1 Traefik access-log preflight"): text.index("### 4.2 OpenAI spend limits")]
    for needed in (
        "read-only",
        "Command-line arguments",
        "Environment",
        "TRAEFIK_",
        "Mounted files",
        "/etc/traefik",
        "$HOME/.config/traefik.yml",
        "--configFile",
        "standard output",
        "docker logs",
        "accessLog.filePath",
        "OTLP",
        "tracing",
        "RequestPath=drop",
        "queryparameters.defaultmode=drop",
        "headers.defaultmode",
        "canary",
        "control",
        "leaked",
        "a file **without** an access log, plus the flag `--accesslog=true` | **no access log**",
    ):
        assert needed in preflight, needed
    # The procedure only reads: nothing in it restarts, removes or writes to Traefik.
    for block in fenced_blocks(preflight):
        for line in block.splitlines():
            code = line.split("#")[0]
            assert not re.search(r"docker (restart|stop|kill|rm|run|compose)\b", code), line
            assert not re.search(r'docker exec "\$T" sh -c .*(\brm\b|\btee\b|sed -i|\bmv\b|\bcp\b|touch|>\s*/)', code), line


def test_the_secret_file_is_created_without_overwriting_and_changed_by_a_separate_procedure():
    text = read(RUNBOOK)
    assert "bash deploy/new-env-file.sh" in text
    changing = text[text.index("**Change** a value"): text.index("### Shell helpers")]
    for needed in ("stat -c '%a %U %n'", "600 <you>", "dc config -q", "--force-recreate", "No `>` redirect"):
        assert needed in changing, needed


def test_the_maintenance_lock_section_states_what_it_does_not_cover():
    text = read(RUNBOOK)
    lock = text[text.index("### The maintenance lock"): text.index("## 3. Configuration contract")]
    for needed in ("cooperative", "does not cover", "database_busy", "database_in_use", "exclusive", "shared"):
        assert needed in lock, needed
    assert "stop the web service, confirm it is stopped" in lock


def test_the_non_cooperating_writer_limits_are_stated_for_each_target_without_overstating():
    text = read(RUNBOOK)
    lock = text[text.index("### The maintenance lock"): text.index("## 3. Configuration contract")]
    flat = " ".join(lock.split())
    for needed in (
        "does not protect against a process that deliberately ignores it",
        "any privileged (root) process",
        "Replacing an existing database",
        "narrow race",
        "Restoring into an empty target",
        "nothing to probe",
        "spans the whole staging and validation of the input",
        "replaced unseen",
        "`check-schema`",
        "exclusive maintenance access, with every managed service stopped",
    ):
        assert needed in flat, needed
    # the old, overstated sentence is gone: it called the whole race "the milliseconds before the rename"
    assert "milliseconds before the rename" not in text
    source = read(DEPLOY / "ops" / "dbtool.py")
    for needed in (
        "an existing database",
        "a target with no database file when the restore began",
        "spans the whole staging",
        "arbitrary privileged process",
    ):
        assert needed in " ".join(source.split()), needed


def test_the_schema_comparison_is_described_accurately_in_the_runbook_and_the_tool():
    runbook = " ".join(read(RUNBOOK).split())
    source = " ".join(read(DEPLOY / "ops" / "dbtool.py").split())
    for text in (runbook, source):
        assert "no catalogue" not in text and "offers no catalogue" not in text
        for needed in ("table_info", "table_xinfo", "sqlite_schema", "CHECK"):
            assert needed in text, needed
    assert "no pragma that lists CHECK constraints" in runbook
    assert "SQLite has no pragma that lists them" in source
    assert "a choice, not a limit of SQLite" in runbook
    assert "Deliberately not compared" in runbook
    gate = read(RUNBOOK)[read(RUNBOOK).index("| **Schema validation** |"):]
    assert "(SQLite has no catalogue for them)" not in gate.splitlines()[0]
    assert "Deliberately not compared" in gate.splitlines()[0]


def test_the_runbook_defines_the_completed_backup_as_a_pair_published_checksum_first():
    ten = section(10)
    flat = " ".join(ten.split())
    for needed in (
        "A completed backup is a pair",
        "`<name>.db` and `<name>.db.sha256`",
        "the checksum file first, the `.db` last",
        "The appearance of the `.db` is the commit",
        "A file is a backup only if",
        "sha256sum -c",
        "That is **not** a backup",
        "delete the `.db` first and its `.sha256` after",
    ):
        assert needed in flat, needed
    nine = " ".join(section(9).split())
    for needed in ("complete pair", "Every failure after step 3 takes one path", "that one service", "STILL RUNNING", "UNKNOWN", "stopped, and confirmed"):
        assert needed in nine, needed
    script = read(DEPLOY / "backup.sh")
    assert "THE FORMAT OF A COMPLETED BACKUP" in script and "LAST. Its appearance is the commit" in script


def test_the_runbook_and_both_scripts_state_the_same_checksum_contract_and_the_commit_versus_report_difference():
    def prose(text):  # a script's comments as running text
        return " ".join(line.lstrip().lstrip("#").strip() for line in text.splitlines())

    ten = " ".join(section(10).split())
    backup = prose(read(DEPLOY / "backup.sh"))
    upgrade = prose(read(DEPLOY / "upgrade.sh"))
    for text in (ten, backup):
        for needed in (
            r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}\.db$",
            "two spaces",
            "a newline",
        ):
            assert needed in text, needed
    for needed in (  # the runbook: what the operator is promised and told
        "exactly one line, byte for byte",
        "committed is not the same as reported",
        "keeps the complete pair",
        "ownership is identity, not a name",
        "`SIGKILL` and a power cut run no clean-up, so they can leave temporary files **even when the committed pair is valid**",
        "`sha256sum -c` alone is **not** that check",
        "cmp -",
        "a valid checksum for `other.db` inside `claimed.db.sha256` passes it for `claimed.db`",
    ):
        assert needed in ten, needed
    for needed in (  # backup.sh: the same facts at the place the code is
        "COMMITTED IS NOT THE SAME AS REPORTED",
        "OWNERSHIP IS IDENTITY",
        "SIGKILL and a power cut run nothing, so they can leave temporary files even when the committed pair is valid",
    ):
        assert needed in backup, needed
    for needed in ("does not use `sha256sum -c`", "compared exactly", "ONE line"):  # upgrade.sh
        assert needed in upgrade, needed
    assert "sha256sum -c" not in " ".join(line for line in read(DEPLOY / "upgrade.sh").splitlines() if not line.lstrip().startswith("#"))


def test_the_restore_steps_are_in_the_same_order_in_the_runbook_and_in_what_upgrade_prints():
    block = section(10)
    restore = block[block.index("**Restore.** The order is mandatory"): block.index("What `restore` does and refuses")]
    steps = ["stop house-helper", "ps --status running --quiet house-helper", "check-idle", "restore --input - --replace", "check-schema", "up -d house-helper", "ps house-helper"]
    at = 0
    for step in steps:
        at = restore.index(step, at) + len(step)
    printed = read(DEPLOY / "upgrade.sh")
    printed = printed[printed.index("Restore the backup with that image"): printed.index("Nothing was restored or downgraded for you")]
    at = 0
    for step in steps:
        step = step.replace("house-helper", "$SERVICE")  # the script spells the one service as a variable
        at = printed.index(step, at) + len(step)
