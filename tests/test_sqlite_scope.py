"""An offline guard against the SQLite bypass (HH-DB-01 R3-03).

The integration fixtures are backend-parameterized (CI-14): in CI they create PostgreSQL
databases. A test that constructs its own SQLite file on the side still passes in that
step - and reports PostgreSQL-first compliance it did not exercise. Four such paths were
found by review (tests/test_web_database.py, tests/test_web_app.py,
tests/test_admin_provisioning.py, tests/test_admin_enrollment.py). This module scans the
test suite for the known patterns and fails when a test uses one without being either

  * scoped to SQLite explicitly (`pytest.mark.sqlite`, `sqlite_only`, or a module-level
    `pytestmark` saying `sqlite`), or
  * in the documented allowlist below - unit tests whose SQLite engine is the subject
    (the harness/engine units) or settings objects that never open a database.

The scan is deliberately narrow: it looks at database-constructing calls and at the
SQLite-only fixtures, not at every string that happens to contain "sqlite:".
"""

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent

CONSTRUCTORS = {
    "create_db_engine",
    "create_engine",
    "open_target",
    "init_database",
    "settings_for",
    "WebSettings",
    "open_factory",
    "open_admin_factory",
}
SQLITE_FIXTURES = {"sqlite_db_url", "migrated_template", "db_path"}

# (module name, test name, why this one is allowed to build SQLite on the side)
ALLOWLIST = {
    ("test_admin_postgres_harness.py", "test_a_guarded_engine_checks_its_connections_before_it_runs_a_statement"): (
        "unit test of the guard: a non-PostgreSQL engine is the point (nothing connects)"
    ),
    ("test_admin_postgres_harness.py", "test_the_engine_of_a_run_is_guarded_and_made_from_the_pinned_url"): (
        "unit test of the guard: SQLite stands in for an unverified engine (nothing connects)"
    ),
    ("test_admin_postgres_harness.py", "test_privileged_grants_refuse_an_external_engine"): (
        "unit test of the privileged boundary: a non-PostgreSQL engine is the point (nothing connects)"
    ),
    ("test_admin_postgres_harness.py", "test_privileged_grants_refuse_an_engine_that_does_not_reach_the_verified_cluster"): (
        "unit test of the privileged boundary: SQLite stands in for an engine that reaches another database"
    ),
    ("test_web_database.py", "test_a_database_can_be_built_before_any_event_loop_exists"): (
        "asserts that constructing WebDatabase binds no loop and opens nothing"
    ),
    ("test_web_auth_settings.py", "test_a_hand_built_web_settings_without_login_is_valid"): (
        "settings validation only; no database is opened"
    ),
    ("test_web_auth_settings.py", "test_the_secret_is_in_no_repr_or_str"): (
        "settings validation only; no database is opened"
    ),
    ("test_web_email_settings.py", "test_settings_built_by_hand_for_both_must_agree_on_the_url_and_the_session_lifetime"): (
        "settings validation only; no database is opened"
    ),
    ("test_web_email_settings.py", "test_the_password_is_in_no_repr_or_str"): (
        "settings validation only; no database is opened"
    ),
}


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_sqlite_value(node) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith("sqlite:")
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(part, ast.Constant) and isinstance(part.value, str) and "sqlite:" in part.value
            for part in node.values
        )
    if isinstance(node, ast.Call):
        return _dotted(node.func).split(".")[-1] in ("sqlite_url", "_sqlite_url")
    return False


def _is_sqlite_construction(node) -> bool:
    if isinstance(node, ast.Call):
        return _dotted(node.func).split(".")[-1] in CONSTRUCTORS and any(
            _is_sqlite_value(argument)
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
        )
    return False


def _decorators_mark_sqlite(function) -> bool:
    for decorator in function.decorator_list:
        dotted = _dotted(decorator.func if isinstance(decorator, ast.Call) else decorator)
        if "sqlite" in dotted:
            return True
        if isinstance(decorator, ast.Call) and any(
            isinstance(argument, ast.Constant) and argument.value == "sqlite"
            for argument in decorator.args
        ):
            return True
    return False


def _module_marks_sqlite(tree) -> bool:
    for node in tree.body:
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        ):
            value = node.value
            if any(
                "sqlite" in _dotted(part)
                for part in ast.walk(value)
                if isinstance(part, ast.Attribute)
            ):
                return True
    return False


def sqlite_using_tests() -> list[tuple[str, str]]:
    """Every test function that constructs SQLite (or uses a SQLite-only fixture) without
    being marked for it - allowed or not."""
    found = []
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _module_marks_sqlite(tree):
            continue
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not function.name.startswith("test_") or _decorators_mark_sqlite(function):
                continue
            uses_sqlite = False
            for node in ast.walk(function):
                if _is_sqlite_construction(node):
                    uses_sqlite = True
                if isinstance(node, ast.Name) and node.id in SQLITE_FIXTURES:
                    uses_sqlite = True
                if isinstance(node, ast.arg) and node.arg in SQLITE_FIXTURES:
                    uses_sqlite = True
            if uses_sqlite:
                found.append((path.name, function.name))
    return found


def unmarked_sqlite_tests() -> list[tuple[str, str]]:
    return [entry for entry in sqlite_using_tests() if entry not in ALLOWLIST]


def test_no_unmarked_test_constructs_sqlite_on_the_side():
    found = unmarked_sqlite_tests()

    assert found == [], (
        "these tests use a SQLite database (or a SQLite-only fixture) but are not marked "
        "`sqlite`/`sqlite_only` and are not in the documented allowlist: "
        f"{found}"
    )


def test_the_allowlist_has_no_stale_entries():
    stale = {entry for entry in ALLOWLIST if entry not in set(sqlite_using_tests())}

    assert stale == set(), (
        "these allowlist entries no longer match a SQLite-using test; remove them: "
        f"{sorted(stale)}"
    )
