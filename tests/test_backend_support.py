"""The integration-test backend switch (HH-DB-01 CI-01, CI-13, CI-14), tested OFFLINE.

No server is touched: the rule is a pure function of the environment, and an unsafe
configuration is refused before anything is opened. The tests that use the resulting
databases live in the suites themselves; this module pins the choice.
"""

import pytest

from tests import backend_support
from tests.postgres_harness import POSTGRES_URL_ENV

BASE = "postgresql+psycopg://someone:secret-value@127.0.0.1:55432/postgres"


def test_without_a_server_a_local_run_uses_sqlite():
    assert backend_support.backend_name({}) == backend_support.SQLITE


def test_a_configured_server_selects_postgresql():
    assert backend_support.backend_name({POSTGRES_URL_ENV: BASE}) == backend_support.POSTGRES


def test_in_ci_an_unconfigured_run_is_an_error_not_a_sqlite_fallback():
    with pytest.raises(backend_support.BackendConfigurationError) as refused:
        backend_support.backend_name({backend_support.CI_ENV: "true"})
    assert POSTGRES_URL_ENV in str(refused.value)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_every_usual_ci_value_requires_the_server(value):
    with pytest.raises(backend_support.BackendConfigurationError):
        backend_support.backend_name({backend_support.CI_ENV: value})


def test_asking_for_postgresql_without_a_server_is_refused():
    with pytest.raises(backend_support.BackendConfigurationError):
        backend_support.backend_name({backend_support.BACKEND_ENV: backend_support.POSTGRES})


def test_asking_for_an_unknown_backend_is_refused_without_echoing_it():
    with pytest.raises(backend_support.BackendConfigurationError) as refused:
        backend_support.backend_name({backend_support.BACKEND_ENV: "oracle"})
    assert "oracle" not in str(refused.value)


def test_an_unsafe_server_is_refused_with_no_url_or_password_in_the_message():
    hostile = "postgresql+psycopg://someone:secret-value@db.example.com:55432/postgres"
    with pytest.raises(backend_support.BackendConfigurationError) as refused:
        backend_support.check_configuration({POSTGRES_URL_ENV: hostile})
    message = str(refused.value)
    assert "secret-value" not in message and "db.example.com" not in message
    assert "[not_loopback]" in message


def test_a_valid_configuration_names_postgresql():
    assert backend_support.check_configuration({POSTGRES_URL_ENV: BASE}) == backend_support.POSTGRES


def test_sqlite_can_be_asked_for_explicitly_without_a_server():
    assert (
        backend_support.backend_name({backend_support.BACKEND_ENV: backend_support.SQLITE})
        == backend_support.SQLITE
    )
