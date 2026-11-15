"""The PostgreSQL test infrastructure, checked OFFLINE (HH-DB-01 CI-04..CI-07, CI-13).

No server, no Docker and no psycopg connection is needed: the URL rules are pure, the
skip policy is a pure function, and the compose file and the CI workflow are read as
text. What this proves is that the pieces which decide whether a real server is used are
themselves the agreed ones; the live behavior is exercised by the `postgres`-marked
suites when the variable is set.

The passwords here are made-up test values. No assertion shows a real credential.
"""

import configparser
import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from tests import postgres_policy, postgres_preflight
from tests.postgres_harness import (
    POSTGRES_URL_ENV,
    ROUTING_ENV,
    SERVER_VERSION_NUM_MAX,
    SERVER_VERSION_NUM_MIN,
    UnsafeTarget,
    validate_target,
)

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "not-a-real-password"
GOOD_URL = f"postgresql+psycopg://hh_test:{PASSWORD}@127.0.0.1:55432/postgres"


def _report(nodeid, *, skipped=False, marked=True, when="setup"):
    keywords = {"postgres": 1} if marked else set()
    return SimpleNamespace(nodeid=nodeid, skipped=skipped, keywords=keywords, when=when)


# --- CI-07: an unexpected skip is a failure -------------------------------------------


def test_a_skipped_postgres_test_is_unexpected_when_the_server_is_configured():
    reports = [
        _report("tests/x.py::test_ran", skipped=False),
        _report("tests/x.py::test_skipped", skipped=True),
        _report("tests/y.py::test_other_skip", skipped=True, marked=False),
        _report("tests/x.py::test_skipped", skipped=True),  # the same test, reported twice
    ]

    assert postgres_policy.unexpected_postgres_skips(reports, {POSTGRES_URL_ENV: GOOD_URL}) == [
        "tests/x.py::test_skipped"
    ]


def test_local_skips_are_not_a_failure_without_a_configured_server():
    reports = [_report("tests/x.py::test_skipped", skipped=True)]

    assert postgres_policy.unexpected_postgres_skips(reports, {}) == []
    assert postgres_policy.unexpected_postgres_skips(reports, {POSTGRES_URL_ENV: "   "}) == []


def test_pytest_registers_the_postgres_marker():
    parser = configparser.ConfigParser()
    parser.read(ROOT / "pytest.ini")
    names = [
        line.split(":", 1)[0].strip()
        for line in parser["pytest"]["markers"].splitlines()
        if line.strip()
    ]

    assert postgres_policy.marker_was_declared(names)


def test_every_live_postgres_module_carries_the_marker():
    # CI-07's collection assertion is only meaningful if the marker is on the modules.
    # tests/test_admin_postgres_harness.py is deliberately not matched: it is offline.
    modules = sorted((ROOT / "tests").glob("test_*_postgres.py"))

    assert modules  # a glob typo must not make this test vacuous
    for path in modules:
        assert "pytest.mark.postgres" in path.read_text(encoding="utf-8"), path.name


# --- CI-06: what the preflight refuses before anything is opened ----------------------


def test_a_url_with_query_parameters_is_refused():
    with pytest.raises(UnsafeTarget) as refusal:
        validate_target(GOOD_URL + "?host=elsewhere", {})

    assert refusal.value.code == "query_parameters"


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql+psycopg://hh_test:{PASSWORD}@db.example.com:55432/postgres",
        f"postgresql+psycopg://hh_test:{PASSWORD}@10.0.0.5:55432/postgres",
    ],
)
def test_a_non_loopback_host_is_refused(url):
    with pytest.raises(UnsafeTarget) as refusal:
        validate_target(url, {})

    assert refusal.value.code == "not_loopback"


def test_port_5432_is_refused():
    with pytest.raises(UnsafeTarget) as refusal:
        validate_target(f"postgresql+psycopg://hh_test:{PASSWORD}@127.0.0.1:5432/postgres", {})

    assert refusal.value.code == "default_port"


def test_a_missing_url_is_not_configured():
    with pytest.raises(UnsafeTarget) as refusal:
        validate_target(None, {})

    assert refusal.value.code == "not_configured"


@pytest.mark.parametrize("name", sorted(ROUTING_ENV))
def test_a_routing_libpq_variable_is_refused(name):
    with pytest.raises(UnsafeTarget) as refusal:
        validate_target(GOOD_URL, {name: "somewhere-else"})

    assert refusal.value.code == "libpq_environment"


def test_the_configured_url_is_accepted_and_every_field_is_fixed():
    target = validate_target(GOOD_URL, {})

    assert (target.username, target.address, target.port) == ("hh_test", "127.0.0.1", 55432)
    assert target.maintenance_database == "postgres"
    assert (SERVER_VERSION_NUM_MIN, SERVER_VERSION_NUM_MAX) == (160000, 169999)  # PostgreSQL 16


def test_the_cli_refuses_without_a_server(monkeypatch, capsys):
    for name in [POSTGRES_URL_ENV, *ROUTING_ENV]:
        monkeypatch.delenv(name, raising=False)

    assert postgres_preflight.main([]) == 1
    assert "not_configured" in capsys.readouterr().err


# --- CI-04: the driver and the pin ---------------------------------------------------


def test_psycopg_is_a_pinned_runtime_dependency():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert re.search(r"^psycopg\[binary\]==\d+\.\d+\.\d+$", requirements, re.MULTILINE)


def test_the_psycopg_3_driver_is_installed():
    assert importlib.util.find_spec("psycopg") is not None


# --- the local server: dedicated, disposable, loopback only --------------------------


def test_the_local_compose_file_is_isolated_from_every_other_stack():
    text = (ROOT / "compose.test-postgres.yaml").read_text(encoding="utf-8")

    assert "name: house-helper-test-postgres" in text  # its own compose project
    assert re.search(r"image: postgres@sha256:[0-9a-f]{64}", text)  # digest-pinned
    assert '"127.0.0.1:55432:5432"' in text  # loopback only, the harness's port
    assert "n8n_n8n_network" not in text
    assert "house-helper-data" not in text  # the Production SQLite volume
    assert re.search(r"^\s*-\s*\"?5432:5432\"?\s*$", text, re.MULTILINE) is None
    assert "0.0.0.0" not in text
    assert "house-helper-test-postgres-data:/var/lib/postgresql/data" in text  # named volume


# --- the CI workflow: both matrix legs, a real server, no silent skips ---------------


def test_ci_gives_both_python_legs_a_real_postgresql_16():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r"HOUSE_HELPER_TEST_POSTGRES_URL:\s*(\S+)", text)

    assert 'python-version: ["3.10", "3.12"]' in text
    assert re.search(r"image: postgres@sha256:[0-9a-f]{64}", text)
    assert "127.0.0.1:55432:5432" in text
    assert "pg_isready" in text
    assert match is not None  # both legs get it: the variable is job-level, not per step
    url = make_url(match.group(1))
    assert url.drivername == "postgresql+psycopg"
    assert url.port == 55432 and not url.query
    assert "python -m tests.postgres_preflight" in text
    assert "pytest --collect-only -q -m postgres" in text
    assert "python -m tests.postgres_preflight --alembic-check" in text
    assert 'pytest -q -ra -m postgres' in text  # the required PostgreSQL suites
    assert 'pytest -q -ra -m "not postgres and not sqlite"' in text  # everything else, PostgreSQL backend
    assert 'pytest -q -ra -m sqlite' in text  # the SQLite-scoped dialect tests, no URL
    assert "HOUSE_HELPER_TEST_BACKEND: sqlite" in text
    # R4-02: the service container's data volume is provisioned explicitly and run-scoped,
    # so the CI identity contract has a fact to verify instead of a guessed volume name.
    assert (
        "hh-test-postgres-${{ github.run_id }}-${{ github.run_attempt }}:/var/lib/postgresql/data"
        in text
    )
