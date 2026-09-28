import os

# Must run before config.py / api_client.py / bot.py are imported anywhere in
# the test session, so tests never depend on a developer's real .env file and
# never risk touching real credentials.
os.environ.setdefault("OPENAI_API_KEY", "test-api-key")
os.environ.setdefault("BOT_TOKEN", "test-bot-token")

import pytest

import bot as bot_module
import context_manager


@pytest.fixture(autouse=True)
def _reset_module_state():
    context_manager._contexts.clear()
    bot_module._user_locks.clear()
    yield
    context_manager._contexts.clear()
    bot_module._user_locks.clear()
