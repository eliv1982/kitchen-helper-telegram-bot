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

import pytest

import api_client
import bot as bot_module
import config
import context_manager


@pytest.fixture(autouse=True)
def _reset_module_state():
    context_manager._contexts.clear()
    bot_module._user_locks.clear()
    api_client._client = None
    config._settings = None
    yield
    context_manager._contexts.clear()
    bot_module._user_locks.clear()
    api_client._client = None
    config._settings = None
