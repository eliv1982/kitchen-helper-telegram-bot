"""house_helper.core must stay independent of the Telegram transport.

A static check of the core modules' own imports. It cannot look at
sys.modules instead: the test session has already imported aiogram through
bot.py, so a runtime check would prove nothing.
"""

import ast
from pathlib import Path

import house_helper.core

CORE_DIR = Path(house_helper.core.__file__).parent

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

    assert {"identity.py", "outcomes.py", "messages_ru.py"} <= names


def test_core_modules_do_not_import_the_telegram_transport():
    offenders = {
        path.name: sorted(_imported_roots(path) & FORBIDDEN)
        for path in CORE_DIR.glob("*.py")
        if _imported_roots(path) & FORBIDDEN
    }

    assert offenders == {}
