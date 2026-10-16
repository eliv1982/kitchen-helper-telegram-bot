"""The test suite never reads a developer's real .env file.

tests/conftest.py makes python-dotenv's loader inert before config.py is first
imported. These tests guard that arrangement without ever touching the real
.env: one inspects the loader config.py actually holds, the other runs
config.py's import-time code next to a throwaway .env.
"""

import builtins
import importlib.util
import os
import shutil
from pathlib import Path

import config

SENTINEL = "HOUSE_HELPER_TEST_DOTENV_SENTINEL"


def test_loader_held_by_config_never_opens_a_file(monkeypatch):
    opened = []

    def refuse_open(file, *args, **kwargs):
        opened.append(file)
        raise AssertionError(f"unexpected file open: {file!r}")

    # Scoped tightly around the call so pytest's own file access is unaffected.
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "open", refuse_open)
        loaded = config.load_dotenv()

    assert loaded is False
    assert opened == []


def test_dotenv_next_to_config_py_is_not_loaded(tmp_path):
    # A copy of the real config.py beside a .env that defines a sentinel
    # variable: were the loader live, executing the copy would merge the
    # sentinel into os.environ.
    shutil.copy(Path(config.__file__), tmp_path / "config.py")
    (tmp_path / ".env").write_text(f"{SENTINEL}=leaked\n", encoding="utf-8")
    assert os.environ.get(SENTINEL) is None

    spec = importlib.util.spec_from_file_location(
        "config_dotenv_probe", tmp_path / "config.py"
    )
    probe = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(probe)
        assert os.environ.get(SENTINEL) is None
    finally:
        os.environ.pop(SENTINEL, None)
