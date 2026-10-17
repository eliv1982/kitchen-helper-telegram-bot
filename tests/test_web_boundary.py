"""Where the web package may be imported from, what it may import, and what
starting it does.

house_helper.core and house_helper.persistence stay free of FastAPI and of the web
package. The web package stays free of the Telegram transport, of provider
configuration and of anything that changes the schema. The Telegram process does
not load the web stack.

The static checks read the modules' import statements (the test session has
already imported most of these libraries, so sys.modules would prove nothing). The
runtime checks run in a fresh `python -I` interpreter, without BOT_TOKEN or
OPENAI_API_KEY, in an empty working directory, with a recording stand-in for
python-dotenv so the real .env file is never opened.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import house_helper.web
from house_helper.persistence.database import DATABASE_URL_ENV
from tests.test_persistence_boundary import (
    CORE_DIR,
    PERSISTENCE_DIR,
    REPO_ROOT,
    _imported_names,
    _is_under,
    _names_imported_by,
    _python_files,
)

WEB_DIR = Path(house_helper.web.__file__).parent

WEB_LIBRARIES = {"fastapi", "starlette", "uvicorn"}
WEB_PACKAGE = "house_helper.web"

# What the web package must not import: the Telegram transport and its state, the
# provider layer, and configuration (config reads the .env file when imported).
TRANSPORT_AND_PROVIDER = {
    "aiogram",
    "bot",
    "config",
    "api_client",
    "context_manager",
    "openai",
    "dotenv",
}

# The Telegram process: its own modules, which must not reach for the web stack.
TELEGRAM_MODULES = ["bot.py", "api_client.py", "config.py", "context_manager.py"]

# Only Alembic, run by an operator, changes the schema. The web package must not
# even name a way to do it.
SCHEMA_CHANGERS = {"init_database", "upgrade_database", "downgrade_database", "create_all", "drop_all"}


def _identifiers(source: str) -> set[str]:
    """Every name `source` uses or imports: variables, attributes, imported names."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.name.split(".")[-1] for alias in node.names)
    return names


def _offenders(directory: Path, forbidden_roots=(), forbidden_package=None) -> dict[str, list[str]]:
    result = {}
    for path in _python_files(directory):
        names = _imported_names(path)
        bad = sorted(
            n
            for n in names
            if n.split(".")[0] in forbidden_roots
            or (forbidden_package and _is_under(n, forbidden_package))
        )
        if bad:
            result[str(path.relative_to(directory))] = bad
    return result


# --- static -------------------------------------------------------------------------------------


def test_the_scan_sees_the_files_it_is_meant_to_scan():
    assert {"app.py", "settings.py", "database.py", "asgi.py", "health.py"} <= {
        p.name for p in _python_files(WEB_DIR)
    }
    assert all((REPO_ROOT / name).is_file() for name in TELEGRAM_MODULES)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("from house_helper.persistence.database import init_database", {"init_database"}),
        ("database.upgrade_database(engine)", {"upgrade_database"}),
        ("Base.metadata.create_all(engine)", {"create_all"}),
        ("from x import create_all as make", {"create_all"}),
    ],
)
def test_the_identifier_scanner_sees_every_spelling(source, expected):
    assert expected <= _identifiers(source)


def test_the_core_does_not_import_the_web_stack_or_package():
    assert _offenders(CORE_DIR, WEB_LIBRARIES, WEB_PACKAGE) == {}


def test_persistence_does_not_import_the_web_stack_or_package():
    assert _offenders(PERSISTENCE_DIR, WEB_LIBRARIES, WEB_PACKAGE) == {}


@pytest.mark.parametrize("module", TELEGRAM_MODULES)
def test_the_telegram_modules_do_not_import_the_web_stack_or_package(module):
    names = _names_imported_by((REPO_ROOT / module).read_text(encoding="utf-8"), "")

    assert sorted(
        n for n in names if n.split(".")[0] in WEB_LIBRARIES or _is_under(n, WEB_PACKAGE)
    ) == []


def test_the_web_package_does_not_import_the_telegram_transport_or_provider_layer():
    assert _offenders(WEB_DIR, TRANSPORT_AND_PROVIDER) == {}


def test_the_web_package_does_not_import_alembic():
    assert _offenders(WEB_DIR, {"alembic"}) == {}


def test_the_web_package_names_no_way_to_change_the_schema():
    offenders = {
        str(path.relative_to(WEB_DIR)): sorted(
            _identifiers(path.read_text(encoding="utf-8")) & SCHEMA_CHANGERS
        )
        for path in _python_files(WEB_DIR)
        if _identifiers(path.read_text(encoding="utf-8")) & SCHEMA_CHANGERS
    }

    assert offenders == {}


# --- runtime: a fresh interpreter ------------------------------------------------------------------

# Not dotenv: the probe's own stand-in for it is in sys.modules from the start, so
# what counts is whether anything calls it (dotenv_calls_*), not whether it is there.
WATCHED = sorted(TRANSPORT_AND_PROVIDER - {"dotenv"})

_PROBE = """
import asyncio, importlib, json, os, sys, types

root, watched, *modules = sys.argv[1:]
watched = json.loads(watched)
sys.path.insert(0, root)

# A recording stand-in: the real .env is never opened, even if the boundary breaks.
dotenv_calls = []
dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda *args, **kwargs: dotenv_calls.append(1) or False
sys.modules["dotenv"] = dotenv

import sqlalchemy as sa

engines = []
real_create_engine = sa.create_engine
def counting_create_engine(*args, **kwargs):
    engines.append(1)
    return real_create_engine(*args, **kwargs)
sa.create_engine = counting_create_engine

def loaded():
    return sorted(
        name for name in sys.modules
        if any(name == w or name.startswith(w + ".") for w in watched)
    )

for name in modules:
    importlib.import_module(name)

report = {
    "bot_token_in_environment": "BOT_TOKEN" in os.environ,
    "openai_key_in_environment": "OPENAI_API_KEY" in os.environ,
    "loaded_after_import": loaded(),
    "dotenv_calls_after_import": len(dotenv_calls),
    "engines_after_import": len(engines),
    "files_after_import": sorted(os.listdir(".")),
}

if "house_helper.web.asgi" in modules:
    import httpx
    from house_helper.web.asgi import app

    report["app_class"] = type(app).__name__

    async def serve():
        async with app.router.lifespan_context(app):
            report["engines_after_startup"] = len(engines)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://web.test") as client:
                response = await client.get("/healthz")
            report["health"] = [response.status_code, response.json()]
        report["closed_after_shutdown"] = app.state.database.closed

    asyncio.run(serve())
    report["loaded_after_run"] = loaded()
    report["dotenv_calls_after_run"] = len(dotenv_calls)
    report["files_after_run"] = sorted(os.listdir("."))

print(json.dumps(report))
"""


def _run_probe(tmp_path, modules, *, extra_env=None, check=True):
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"BOT_TOKEN", "OPENAI_API_KEY", DATABASE_URL_ENV}
    }
    env.update(extra_env or {})
    work_dir = tmp_path / "cwd"
    work_dir.mkdir()
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(REPO_ROOT), json.dumps(WATCHED), *modules],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=work_dir,
        env=env,
    )
    if check:
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout.splitlines()[-1])
    return completed


def _web_modules(*, with_asgi: bool) -> list[str]:
    names = ["house_helper.web"] + sorted(
        f"house_helper.web.{path.stem}"
        for path in WEB_DIR.glob("*.py")
        if path.stem != "__init__" and (with_asgi or path.stem != "asgi")
    )
    return names


@pytest.fixture(scope="module")
def imported_report(tmp_path_factory):
    return _run_probe(tmp_path_factory.mktemp("import"), _web_modules(with_asgi=False))


@pytest.fixture(scope="module")
def started_report(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("start")
    database_file = tmp_path / "never_created.db"
    report = _run_probe(
        tmp_path,
        _web_modules(with_asgi=True),
        extra_env={DATABASE_URL_ENV: f"sqlite:///{database_file}"},
    )
    report["database_file_exists"] = database_file.exists()
    return report


def test_the_probe_runs_without_the_bot_environment(imported_report):
    assert imported_report["bot_token_in_environment"] is False
    assert imported_report["openai_key_in_environment"] is False


def test_importing_the_web_modules_loads_no_telegram_provider_or_env_loading(imported_report):
    assert imported_report["loaded_after_import"] == []
    assert imported_report["dotenv_calls_after_import"] == 0


def test_importing_the_web_modules_creates_no_engine_and_no_file(imported_report):
    assert imported_report["engines_after_import"] == 0
    assert imported_report["files_after_import"] == []


def test_the_asgi_module_builds_the_application_and_nothing_else(started_report):
    assert started_report["app_class"] == "FastAPI"
    assert started_report["engines_after_import"] == 0
    assert started_report["files_after_import"] == []
    assert started_report["loaded_after_import"] == []
    assert started_report["dotenv_calls_after_import"] == 0


def test_the_application_starts_serves_and_stops_without_the_bot_environment(started_report):
    assert started_report["engines_after_startup"] == 1  # created by the lifespan, once
    assert started_report["health"] == [200, {"status": "ok"}]
    assert started_report["closed_after_shutdown"] is True


def test_running_the_application_loads_no_telegram_or_provider_code_and_creates_no_file(
    started_report,
):
    assert started_report["loaded_after_run"] == []
    assert started_report["dotenv_calls_after_run"] == 0
    assert started_report["files_after_run"] == []
    assert started_report["database_file_exists"] is False  # nothing connected, so nothing made


def test_the_asgi_module_with_no_database_variable_uses_the_default_and_still_creates_nothing(
    tmp_path,
):
    report = _run_probe(tmp_path, ["house_helper.web.asgi"])

    assert report["health"] == [200, {"status": "ok"}]
    assert report["files_after_run"] == []  # the default is a file in the working directory


def test_the_asgi_module_refuses_to_import_with_unusable_configuration(tmp_path):
    completed = _run_probe(
        tmp_path,
        ["house_helper.web.asgi"],
        extra_env={DATABASE_URL_ENV: "sqlite:///:memory:"},
        check=False,
    )

    assert completed.returncode != 0
    assert "WebConfigError" in completed.stderr


def test_the_telegram_process_loads_no_part_of_the_web_stack(tmp_path):
    script = """
import json, sys, types
root = sys.argv[1]
sys.path.insert(0, root)
dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda *args, **kwargs: False
sys.modules["dotenv"] = dotenv
import bot
print(json.dumps(sorted(
    name for name in sys.modules
    if name.split(".")[0] in {"fastapi", "starlette", "uvicorn"} or name.startswith("house_helper.web")
)))
"""
    env = {**os.environ, "BOT_TOKEN": "x", "OPENAI_API_KEY": "x"}
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1]) == []
