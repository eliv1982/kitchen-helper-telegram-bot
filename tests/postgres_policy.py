"""The rules that keep the PostgreSQL suites honest (HH-DB-01 CI-07, CI-13).

tests/conftest.py uses this module to FAIL a run in which a PostgreSQL-marked test is
skipped while HOUSE_HELPER_TEST_POSTGRES_URL is set: in CI the server is required, so a
skip is not a pass. Locally the variable is normally absent and the marked suites stay
opt-in.

The rule is a pure function of the reports and the environment, so it is tested offline
(tests/test_postgres_preflight.py) without a server, and the CI run that uses it needs
no extra machinery.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

POSTGRES_URL_ENV = "HOUSE_HELPER_TEST_POSTGRES_URL"
MARKER = "postgres"


def is_configured(environ: Mapping[str, str]) -> bool:
    """Whether a PostgreSQL server was named for this run."""
    return bool((environ.get(POSTGRES_URL_ENV) or "").strip())


def unexpected_postgres_skips(reports: Iterable[object], environ: Mapping[str, str]) -> list[str]:
    """The node ids of PostgreSQL-marked tests that were skipped although a server was
    configured. A report is anything with `skipped`, `keywords` and `nodeid` (pytest's
    TestReport); duplicates are collapsed and the first-seen order is kept.

    With no server configured this is empty: the local skips are the documented opt-in
    behavior (CI-13), not a failure."""
    if not is_configured(environ):
        return []
    found: list[str] = []
    for report in reports:
        if not getattr(report, "skipped", False):
            continue
        if MARKER not in getattr(report, "keywords", ()):
            continue
        nodeid = str(getattr(report, "nodeid", ""))
        if nodeid and nodeid not in found:
            found.append(nodeid)
    return found


def marker_was_declared(markers: Sequence[str]) -> bool:
    """Whether pytest.ini registered the marker (an unregistered mark only warns, and a
    typo would silently make every PostgreSQL skip invisible to the rule above)."""
    return MARKER in markers
