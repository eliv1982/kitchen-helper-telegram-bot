"""The page's own script (app.js) run in Node against the shell the server sends.

A mini DOM (tests/js/fake_dom.js) holds the shell that house_helper.web.ui.render_shell makes
for a deployment with both ways of signing in; the script is run in it against a scripted
server and fake timers, and the scenarios of tests/js/dom_scenarios.js press its buttons, type
into its box and look at what it drew: that hostile text stays text, that the composer, the
retry buttons, the drawer and the sign-out do what they say.

This is NOT a browser and says nothing about how the page looks: there is no layout, no style,
no real focus or scrolling, and a real browser can differ. It is also run against the shell with
both sign-in choices only. What was tried in a real browser, and what was not, is in the README
("Проверка в браузере"). Needs Node.js; skipped without it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from house_helper.web import ui

NODE = shutil.which("node")
JS_DIR = Path(__file__).parent / "js"

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def _node(*args: str) -> dict:
    completed = subprocess.run(
        [NODE, str(JS_DIR / "run_dom.js"), *args], capture_output=True, text=True, encoding="utf-8", timeout=240
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


NAMES = _node("--list")["names"] if NODE is not None else []


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    shell = tmp_path_factory.mktemp("dom") / "shell.html"
    shell.write_text(ui.render_shell(telegram=True, email=True), encoding="utf-8")
    return _node(f"--shell={shell}")["results"]


def test_the_scenarios_were_found():
    assert len(NAMES) >= 20


@pytest.mark.parametrize("name", NAMES)
def test_scenario(results, name):
    assert results[name]["ok"], results[name].get("error")
