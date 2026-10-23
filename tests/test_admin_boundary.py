"""Where the administrative package may be imported from, and what it may import.

It is an operator's tool, run by hand: the web application, the Telegram bot, the chat
core and the persistence layer neither import it nor depend on it, and it imports
neither the web stack, the Telegram transport, the provider layer, configuration (which
reads the .env file) nor the sign-in libraries. It also names no way to change the
schema: only Alembic, run by an operator, does that.

The static checks read the modules' import statements; the runtime check imports every
module, and runs the command, in a fresh `python -I` interpreter with a recording
stand-in for python-dotenv, and looks at what got loaded, what was created, and whether
a .env file would have been read.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import house_helper.admin
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
from tests.test_web_boundary import (
    SCHEMA_CHANGERS,
    SIGN_IN_LIBRARIES,
    TELEGRAM_MODULES,
    TRANSPORT_AND_PROVIDER,
    WEB_DIR,
    WEB_LIBRARIES,
    _identifiers,
)

ADMIN_DIR = Path(house_helper.admin.__file__).parent
ADMIN_PACKAGE = "house_helper.admin"
OTHER_PACKAGES = ("house_helper.web", "house_helper.core")


def test_the_scan_sees_the_files_it_is_meant_to_scan():
    assert {"__main__.py", "cli.py", "enrollment.py", "provisioning.py", "retention.py", "target.py"} <= {
        p.name for p in _python_files(ADMIN_DIR)
    }


def test_the_admin_package_imports_no_web_telegram_provider_or_sign_in_code():
    forbidden_roots = WEB_LIBRARIES | TRANSPORT_AND_PROVIDER | SIGN_IN_LIBRARIES
    offenders = {}
    for path in _python_files(ADMIN_DIR):
        bad = sorted(
            name
            for name in _imported_names(path)
            if name.split(".")[0] in forbidden_roots
            or any(_is_under(name, package) for package in OTHER_PACKAGES)
        )
        if bad:
            offenders[path.name] = bad

    assert offenders == {}


def test_the_admin_package_imports_alembic_only_to_read_and_names_no_schema_changer():
    imported = {name for path in _python_files(ADMIN_DIR) for name in _imported_names(path)}
    named = {name for path in _python_files(ADMIN_DIR) for name in _identifiers(path.read_text("utf-8"))}

    assert "alembic.command" not in imported
    assert not any(_is_under(name, "alembic.command") for name in imported)
    assert named & SCHEMA_CHANGERS == set()
    assert {"stamp", "upgrade", "downgrade"} & named == set()


def test_nothing_else_imports_the_admin_package():
    importers = {}
    for directory in (CORE_DIR, PERSISTENCE_DIR, WEB_DIR):
        for path in _python_files(directory):
            if any(_is_under(name, ADMIN_PACKAGE) for name in _imported_names(path)):
                importers[str(path.relative_to(REPO_ROOT))] = ADMIN_PACKAGE
    for module in TELEGRAM_MODULES:
        names = _names_imported_by((REPO_ROOT / module).read_text(encoding="utf-8"), "")
        if any(_is_under(name, ADMIN_PACKAGE) for name in names):
            importers[module] = ADMIN_PACKAGE

    assert importers == {}


# --- runtime: a fresh interpreter ---------------------------------------------------------------

WATCHED = sorted(
    (TRANSPORT_AND_PROVIDER - {"dotenv"}) | WEB_LIBRARIES | SIGN_IN_LIBRARIES
) + ["house_helper.web", "house_helper.core"]

_PROBE = """
import importlib, json, os, sys, types

root, watched, *modules = sys.argv[1:]
watched = json.loads(watched)
sys.path.insert(0, root)

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
    "loaded_after_import": loaded(),
    "dotenv_calls_after_import": len(dotenv_calls),
    "engines_after_import": len(engines),
    "files_after_import": sorted(os.listdir(".")),
}

# And a real invocation that is refused before it reaches any database.
from house_helper.admin.cli import main
report["exit_status"] = main(
    ["provision-email", "--email", "a@example.com"], environ={}
)
report["loaded_after_run"] = loaded()
report["dotenv_calls_after_run"] = len(dotenv_calls)
report["engines_after_run"] = len(engines)
report["files_after_run"] = sorted(os.listdir("."))

print(json.dumps(report))
"""


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    work_dir = tmp_path_factory.mktemp("probe")
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"BOT_TOKEN", "OPENAI_API_KEY", DATABASE_URL_ENV}
        and not name.startswith("HOUSE_HELPER_")
    }
    modules = [ADMIN_PACKAGE] + sorted(
        f"{ADMIN_PACKAGE}.{path.stem}" for path in ADMIN_DIR.glob("*.py") if path.stem != "__init__"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(REPO_ROOT), json.dumps(WATCHED), *modules],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=work_dir,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.splitlines()[-1])


def test_the_probe_runs_without_the_bot_environment(report):
    assert report["bot_token_in_environment"] is False


def test_importing_every_admin_module_loads_nothing_it_must_not_and_has_no_effect(report):
    assert report["loaded_after_import"] == []
    assert report["dotenv_calls_after_import"] == 0
    assert report["engines_after_import"] == 0
    assert report["files_after_import"] == []  # nothing created; __main__ ran nothing


def test_running_the_command_loads_no_web_telegram_or_provider_code_and_reads_no_env_file(report):
    assert report["exit_status"] == 4  # no database configured: refused before any engine
    assert report["loaded_after_run"] == []
    assert report["dotenv_calls_after_run"] == 0
    assert report["engines_after_run"] == 0
    assert report["files_after_run"] == []
