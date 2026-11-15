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


# The web process's Telegram and email login are configured from the environment too,
# and a developer's shell may well have them set (or half set). The tests that exercise
# configuration say what they need; none may depend on what happens to be exported.
for _name in (
    "HOUSE_HELPER_PUBLIC_URL",
    "HOUSE_HELPER_TELEGRAM_CLIENT_ID",
    "HOUSE_HELPER_TELEGRAM_CLIENT_SECRET",
    "HOUSE_HELPER_TELEGRAM_ID_TOKEN_ALG",
    "HOUSE_HELPER_SMTP_HOST",
    "HOUSE_HELPER_SMTP_PORT",
    "HOUSE_HELPER_SMTP_SECURITY",
    "HOUSE_HELPER_SMTP_USERNAME",
    "HOUSE_HELPER_SMTP_PASSWORD",
    "HOUSE_HELPER_EMAIL_FROM",
):
    os.environ.pop(_name, None)

import pytest

from tests import backend_support, plugin_check, postgres_policy, seams


def pytest_collection_modifyitems(config, items):
    plugin_check.require_asyncio_plugin(config, items)


# HH-DB-01 CI-14: one backend for the integration fixtures. With the PostgreSQL URL set
# every fixture database is PostgreSQL; locally, without it, SQLite (CI-13). A run that
# cannot honour its configuration fails before any test - never a silent fallback.
@pytest.fixture(scope="session", autouse=True)
def _integration_database_backend():
    try:
        backend_support.check_configuration()
    except backend_support.BackendConfigurationError as refusal:
        pytest.fail(str(refusal), pytrace=False)
    yield
    backend_support.close_backend()


@pytest.fixture(scope="session")
def database_backend():
    """The fixture groups' database factory (tests/backend_support.py)."""
    return backend_support.backend()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Drop the databases this test created, after its fixtures have let go of them. A test
    may still read its database after an application context has closed (the harnesses do
    not remove it themselves), but nothing survives the test."""
    result = yield
    backend_support.cleanup_databases()
    return result


# HH-DB-01 CI-07: in CI the PostgreSQL suites are required, not skipped. When
# HOUSE_HELPER_TEST_POSTGRES_URL is set, a skipped `postgres`-marked test fails the run;
# locally the variable is normally absent and the suites are opt-in (CI-13). The rule
# itself is a pure function in tests/postgres_policy.py, tested offline.
_postgres_skips = []


def pytest_runtest_logreport(report):
    if report.skipped:
        _postgres_skips.append(report)


def pytest_sessionfinish(session, exitstatus):
    skipped = postgres_policy.unexpected_postgres_skips(_postgres_skips, os.environ)
    if not skipped:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        reporter.write_line(
            f"{postgres_policy.POSTGRES_URL_ENV} is set, but these PostgreSQL tests were "
            "skipped (they are required, CI-07):",
            red=True,
        )
        for nodeid in skipped:
            reporter.write_line(f"  SKIPPED {nodeid}", red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _reset_module_state():
    seams.reset_state()
    yield
    seams.reset_state()
