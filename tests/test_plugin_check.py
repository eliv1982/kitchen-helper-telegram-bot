"""A run without pytest-asyncio stops with one clear message instead of
hundreds of failures. Each case is a real nested pytest run in a temporary
directory, because the thing under test is how pytest starts.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

CONFTEST = """
from tests import plugin_check


def pytest_collection_modifyitems(config, items):
    plugin_check.require_asyncio_plugin(config, items)
"""

ASYNC_TEST = """
import pytest


@pytest.mark.asyncio
async def test_asynchronous():
    assert True
"""

SYNC_TEST = """
def test_synchronous():
    assert True
"""


def run_pytest(tmp_path, *arguments, autoload: bool):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT)}
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    if not autoload:
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
            "--rootdir", str(tmp_path), "--confcutdir", str(tmp_path), *arguments, str(tmp_path),
        ],  # fmt: skip
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


@pytest.fixture
def project(tmp_path):
    (tmp_path / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (tmp_path / "test_async.py").write_text(ASYNC_TEST, encoding="utf-8")
    return tmp_path


def test_async_tests_without_the_plugin_stop_the_run_with_one_clear_message(project):
    result = run_pytest(project, autoload=False)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "pytest-asyncio is not active" in output
    assert "-p pytest_asyncio.plugin" in output
    assert "1 collected tests are async" in output
    assert "failed" not in result.stdout  # no pile of unrelated failures


def test_naming_the_plugin_explicitly_makes_the_same_run_pass(project):
    result = run_pytest(project, "-p", "pytest_asyncio.plugin", autoload=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_the_normal_run_with_autoload_is_unaffected(project):
    result = run_pytest(project, autoload=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_synchronous_tests_still_run_without_the_plugin(project):
    (project / "test_async.py").unlink()
    (project / "test_sync.py").write_text(SYNC_TEST, encoding="utf-8")

    result = run_pytest(project, autoload=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_disabling_the_plugin_by_name_is_caught_too(project):
    result = run_pytest(project, "-p", "no:asyncio", autoload=True)

    assert result.returncode != 0
    assert "pytest-asyncio is not active" in result.stdout + result.stderr
