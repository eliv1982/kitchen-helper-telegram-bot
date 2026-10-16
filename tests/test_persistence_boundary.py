"""Where persistence may be imported from, and what it may import.

house_helper.core (the chat core) stays free of SQLAlchemy, Alembic and the
persistence package. The persistence package stays free of the Telegram
transport and of provider configuration, so importing it never loads a .env
file, an API key or the bot.

The static checks read the modules' import statements (the test session has
already imported most of these libraries, so sys.modules would prove nothing).
The runtime checks import in a fresh `python -I` interpreter and look at what
got loaded.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

import house_helper.core
import house_helper.persistence

CORE_DIR = Path(house_helper.core.__file__).parent
PERSISTENCE_DIR = Path(house_helper.persistence.__file__).parent
REPO_ROOT = CORE_DIR.parent.parent

# Top-level names the core must not import, and the package it must not reach.
PERSISTENCE_LIBRARIES = {"sqlalchemy", "alembic"}
PERSISTENCE_PACKAGE = "house_helper.persistence"

# What the persistence package must not import: the Telegram transport, the
# provider layer and configuration (config reads the .env file when imported).
TRANSPORT_AND_PROVIDER = {"aiogram", "bot", "config", "api_client", "openai", "dotenv"}


def _names_imported_by(source: str, package: str) -> set[str]:
    """Every dotted module name `source` imports, as absolute names, however the
    import is spelled. `package` is the package the source lives in, for
    resolving relative imports."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join(parts + ([node.module] if node.module else []))
            names.add(base)
            # `from house_helper import persistence` imports house_helper.persistence
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _imported_names(path: Path) -> set[str]:
    package = ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts[:-1])
    return _names_imported_by(path.read_text(encoding="utf-8"), package)


def _python_files(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.py"))


def _is_under(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


# --- static: the core does not import persistence ---------------------------------


def test_the_scan_sees_the_files_it_is_meant_to_scan():
    assert {"chat_service.py", "identity.py"} <= {p.name for p in _python_files(CORE_DIR)}
    assert {"models.py", "database.py", "env.py"} <= {p.name for p in _python_files(PERSISTENCE_DIR)}


@pytest.mark.parametrize(
    "source, expected",
    [
        ("import sqlalchemy", {"sqlalchemy"}),
        ("from sqlalchemy.orm import Session", {"sqlalchemy.orm", "sqlalchemy.orm.Session"}),
        ("from house_helper import persistence", {"house_helper", "house_helper.persistence"}),
        ("from house_helper.persistence import models", {"house_helper.persistence", "house_helper.persistence.models"}),
        ("from .. import persistence", {"house_helper", "house_helper.persistence"}),
        ("from ..persistence import models", {"house_helper.persistence", "house_helper.persistence.models"}),
    ],
)
def test_the_import_scanner_resolves_every_spelling(source, expected):
    # A scanner that missed `from house_helper import persistence` or a relative
    # import would let the boundary tests below pass over a violation. The
    # relative cases are written as they would appear in house_helper/core/.
    assert expected <= _names_imported_by(source, "house_helper.core")


def test_core_modules_do_not_import_the_persistence_libraries():
    offenders = {
        path.name: sorted(n for n in names if n.split(".")[0] in PERSISTENCE_LIBRARIES)
        for path in _python_files(CORE_DIR)
        if (names := _imported_names(path))
        and any(n.split(".")[0] in PERSISTENCE_LIBRARIES for n in names)
    }

    assert offenders == {}


def test_core_modules_do_not_import_the_persistence_package():
    offenders = {
        path.name: sorted(n for n in names if _is_under(n, PERSISTENCE_PACKAGE))
        for path in _python_files(CORE_DIR)
        if (names := _imported_names(path)) and any(_is_under(n, PERSISTENCE_PACKAGE) for n in names)
    }

    assert offenders == {}


# --- static: persistence does not import the transport or provider layer ----------


def test_persistence_modules_do_not_import_the_transport_or_provider_layer():
    offenders = {
        str(path.relative_to(PERSISTENCE_DIR)): sorted(
            n for n in names if n.split(".")[0] in TRANSPORT_AND_PROVIDER
        )
        for path in _python_files(PERSISTENCE_DIR)
        if (names := _imported_names(path))
        and any(n.split(".")[0] in TRANSPORT_AND_PROVIDER for n in names)
    }

    assert offenders == {}


# --- runtime ----------------------------------------------------------------------

_IMPORT_PROBE = """
import importlib, json, sys

root, modules, watched = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
sys.path.insert(0, root)
for name in modules:
    importlib.import_module(name)
loaded = sorted(
    name for name in sys.modules
    if any(name == w or name.startswith(w + ".") for w in watched)
)
print(json.dumps(loaded))
"""


def _loaded_after_importing(modules: list[str], watched: list[str]) -> list[str]:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _IMPORT_PROBE,
            str(REPO_ROOT),
            json.dumps(modules),
            json.dumps(watched),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.splitlines()[-1])


def _all_modules(directory: Path, package: str) -> list[str]:
    return [package] + sorted(
        f"{package}.{path.stem}" for path in directory.glob("*.py") if path.stem != "__init__"
    )


def test_importing_the_core_loads_no_persistence_code():
    loaded = _loaded_after_importing(
        _all_modules(CORE_DIR, "house_helper.core"),
        ["sqlalchemy", "alembic", "house_helper.persistence"],
    )

    assert loaded == []


def test_importing_persistence_loads_no_transport_provider_or_env_loading():
    loaded = _loaded_after_importing(
        _all_modules(PERSISTENCE_DIR, "house_helper.persistence"),
        ["aiogram", "bot", "config", "api_client", "openai", "dotenv"],
    )

    assert loaded == []
