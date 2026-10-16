# The offline guard goes in before anything else -- before the environment is
# forced and before the application, or the OpenAI / aiogram / httpx stacks
# beneath it, are imported -- so no import-time code or fixture can open a
# connection. Only loopback traffic gets through (see tests/network_guard.py).
from tests import network_guard
from tests.network_guard import no_unclaimed_network_attempts  # noqa: F401  (autouse fixture)

network_guard.install()

import os

# Must be set before config.py / api_client.py / bot.py are imported anywhere
# in the test session, so tests never depend on a developer's real .env file
# (whatever it contains) and never risk touching real credentials.
#
# Uses direct assignment rather than setdefault(): setdefault() would leave
# a value the developer's shell happens to have pre-set (e.g. a real
# BOT_TOKEN or OPENAI_API_KEY exported for other work) in place, silently
# making the test run depend on whatever is in that shell. Forcing every
# value here means the suite is byte-for-byte identical regardless of the
# ambient environment it runs in.
_TEST_ENV_DEFAULTS = {
    "BOT_TOKEN": "test-bot-token",
    "OPENAI_API_KEY": "test-api-key",
    "OPENAI_MODEL": "gpt-4.1-mini",
    "TEMPERATURE": "0.7",
    "MAX_TOKENS": "600",
    "OPENAI_TIMEOUT_SECONDS": "15",
    "OPENAI_MAX_RETRIES": "1",
}
for _name, _value in _TEST_ENV_DEFAULTS.items():
    os.environ[_name] = _value

import dotenv


# config.py calls load_dotenv() when it is imported, and python-dotenv finds
# the .env next to config.py. Forcing the variables above is not enough on its
# own: the suite would still open the developer's real .env (and merge any
# extra variable it defines into os.environ, e.g. an OpenAI base URL). Make the
# loader inert before config is first imported -- config binds the name with
# `from dotenv import load_dotenv`, so replacing the attribute here is what it
# picks up. config.py itself is deliberately left untouched.
def _load_dotenv_disabled(*args, **kwargs) -> bool:
    return False


dotenv.load_dotenv = _load_dotenv_disabled

import pytest

from tests import plugin_check, seams


def pytest_collection_modifyitems(config, items):
    plugin_check.require_asyncio_plugin(config, items)


@pytest.fixture(autouse=True)
def _reset_module_state():
    seams.reset_state()
    yield
    seams.reset_state()
