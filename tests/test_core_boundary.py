"""house_helper.core must stay independent of the Telegram transport and must
not load provider configuration just by being imported.

The first is a static check of the core modules' own imports. It cannot look at
sys.modules instead: the test session has already imported aiogram through
bot.py, so a runtime check would prove nothing. The second needs a runtime
check, so it runs in a fresh interpreter (see the bottom of this file).
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

import house_helper.core

CORE_DIR = Path(house_helper.core.__file__).parent
REPO_ROOT = CORE_DIR.parent.parent

# The Telegram library and the Telegram entry point.
FORBIDDEN = {"aiogram", "bot"}


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_scan_sees_the_core_modules():
    names = {path.name for path in CORE_DIR.glob("*.py")}

    assert {
        "identity.py",
        "outcomes.py",
        "messages_ru.py",
        "backend.py",
        "chat_service.py",
    } <= names


def test_core_modules_do_not_import_the_telegram_transport():
    offenders = {
        path.name: sorted(_imported_roots(path) & FORBIDDEN)
        for path in CORE_DIR.glob("*.py")
        if _imported_roots(path) & FORBIDDEN
    }

    assert offenders == {}


# --- importing the core loads no provider configuration -------------------------
#
# chat_service -> backend -> api_client -> config used to run load_dotenv() on
# import. Everything below runs in a fresh interpreter (-I: no PYTHON* variables,
# nothing from the working directory), with a recording stand-in for python-dotenv
# installed first, so the real .env file is never opened -- not even if this
# boundary regresses and config is imported after all.

# Modules the chat core must not pull in just by being imported.
PROVIDER_AND_TRANSPORT = ["api_client", "config", "bot", "aiogram"]

_ISOLATED_SCRIPT = """
import asyncio, importlib, json, os, sys, types
from types import SimpleNamespace

root, forbidden, *core_modules = sys.argv[1:]
forbidden = json.loads(forbidden)
sys.path.insert(0, root)

dotenv_calls = []
dotenv = types.ModuleType("dotenv")
dotenv.load_dotenv = lambda *args, **kwargs: dotenv_calls.append(1) or False
sys.modules["dotenv"] = dotenv

# The SDK's client class is swapped for a fake before anything of ours is
# imported, so that however api_client ends up imported, nothing can reach the
# network (the offline guard does not follow into this process).
import openai

clients = []

class FakeClient:
    def __init__(self, **kwargs):
        clients.append(kwargs)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        message = SimpleNamespace(content="ok")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

openai.AsyncOpenAI = FakeClient
os.environ.update(BOT_TOKEN="x", OPENAI_API_KEY="x")

for name in core_modules:
    importlib.import_module(name)

report = {
    "loaded_after_import": [name for name in forbidden if name in sys.modules],
    "dotenv_calls_after_import": len(dotenv_calls),
    "clients_after_import": len(clients),
}

# First use of the OpenAI adapter: this is when api_client gets imported.
from house_helper.core.backend import OpenAIBackend
from house_helper.core.identity import ConversationKey

reply = asyncio.run(
    OpenAIBackend().complete(
        [{"role": "user", "content": "hi"}],
        conversation=ConversationKey("web", "1"),
        context_len=1,
    )
)
report["api_client_loaded_after_use"] = "api_client" in sys.modules
report["clients_after_use"] = len(clients)
report["reply"] = reply.text
print(json.dumps(report))
"""


@pytest.fixture(scope="module")
def isolated_report() -> dict:
    core_modules = ["house_helper.core"] + sorted(
        f"house_helper.core.{path.stem}"
        for path in CORE_DIR.glob("*.py")
        if path.stem != "__init__"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _ISOLATED_SCRIPT,
            str(REPO_ROOT),
            json.dumps(PROVIDER_AND_TRANSPORT),
            *core_modules,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.splitlines()[-1])


def test_importing_the_core_loads_no_provider_configuration_or_transport(isolated_report):
    assert isolated_report["loaded_after_import"] == []


def test_importing_the_core_never_loads_the_env_file(isolated_report):
    assert isolated_report["dotenv_calls_after_import"] == 0


def test_the_openai_adapter_loads_the_provider_layer_on_first_use(isolated_report):
    assert isolated_report["api_client_loaded_after_use"] is True
    assert isolated_report["clients_after_import"] == 0
    assert isolated_report["clients_after_use"] == 1  # created by the first call, not before
    assert isolated_report["reply"] == "ok"
