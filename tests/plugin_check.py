"""Stop a test run that would drown in unrelated failures.

The async tests need pytest-asyncio. Pytest finds it through an entry point, and
a run that turns plugin autoloading off (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, which
hermetic sandboxes do) never loads it: every async test then fails with "async def
functions are not natively supported", hundreds of failures with nothing to do with
the code under test. This turns that into one clear message at collection time.

It checks only when an async test is actually collected, so a run of the
synchronous files (the persistence tests, for one) is not affected. It does not
load the plugin for you: pytest refuses to register one plugin under two names, so
loading it by hand would break the normal run, where autoload has done it already.
"""

from __future__ import annotations

import pytest

PLUGIN_MODULE = "pytest_asyncio.plugin"


def asyncio_plugin_is_active(config: pytest.Config) -> bool:
    """By module, not by name: autoload registers it as "asyncio", and
    `-p pytest_asyncio.plugin` registers the same module under its own name."""
    return any(
        getattr(plugin, "__name__", None) == PLUGIN_MODULE
        for plugin in config.pluginmanager.get_plugins()
    )


def require_asyncio_plugin(config: pytest.Config, items: list[pytest.Item]) -> None:
    """UsageError if an async test was collected and pytest-asyncio is not active."""
    if asyncio_plugin_is_active(config):
        return
    asynchronous = [item for item in items if item.get_closest_marker("asyncio")]
    if not asynchronous:
        return
    raise pytest.UsageError(
        f"{len(asynchronous)} collected tests are async, but pytest-asyncio is not active, "
        "so each would fail on its own with an unrelated error. If plugin autoloading is "
        "disabled (PYTEST_DISABLE_PLUGIN_AUTOLOAD), name the plugin: "
        "pytest -p pytest_asyncio.plugin. Otherwise check that requirements-dev.txt is "
        "installed and that the run does not pass -p no:asyncio."
    )
