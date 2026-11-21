"""The C7-P3 PostgreSQL purge one-shot: the retention command as `hh_purge`.

No database is contacted. The success paths use a scripted engine (session, privilege and
marker rows) and a stub admin CLI; the refusal paths exercise the real wiring, passfile,
argument, lock and handoff gates. Retention itself is covered by
tests/test_admin_retention*.py and is deliberately not reimplemented here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from house_helper.persistence import pg_credentials
from house_helper.persistence import pg_journal

from tests.pg_oneshot_failures import startup_refusal, startup_timeout, structured_denial

REPO_ROOT = Path(__file__).resolve().parent.parent
ONESHOT_PATH = REPO_ROOT / "deploy" / "ops" / "pg_oneshot.py"

MARKER = pg_journal.make_marker("e" * 32, "726485930221")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


oneshot = _load("deploy_pg_oneshot_purge", ONESHOT_PATH)


def _environment(role: str = "purge") -> dict:
    login = oneshot.EXPECTED_LOGIN[role]
    return {
        "HOUSE_HELPER_PG_ACTIVATE": "1",
        "PGUSER": login,
        "PGHOST": "house-helper-db",
        "PGPORT": "5432",
        "PGDATABASE": "house_helper",
        "PGPASSFILE": f"/run/credentials/{login}.pgpass",
        "HOUSE_HELPER_STATE_DIR": "/state",
        "HOUSE_HELPER_DATABASE_URL": (
            f"postgresql+psycopg://{login}@house-helper-db:5432/house_helper"
        ),
    }


class _NullLock:
    def __init__(self, purpose: str) -> None:
        self.purpose = purpose

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _Result:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, rows) -> None:
        self.rows = rows

    def exec_driver_sql(self, sql: str):
        if "session_user" in sql:
            return _Result(self.rows["session"])
        if "has_database_privilege" in sql:
            return _Result(self.rows["privilege"])
        if "shobj_description" in sql:
            return _Result(self.rows["marker"])
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _Engine:
    def __init__(self, rows=None, *, error=None) -> None:
        self.rows = rows or {
            "session": ("hh_purge", "hh_purge", "house_helper"),
            "privilege": (True, False),
            "marker": (MARKER,),
        }
        self.error = error
        self.disposed = False

    def connect(self):
        if self.error is not None:
            raise self.error
        return _Connection(self.rows)

    def dispose(self) -> None:
        self.disposed = True


class _LockBusy(Exception):
    code = "maintenance_lock_busy"
    message = "the migration or restore holds the state volume"
    exit_code = 3


def _no_passfile(monkeypatch):
    monkeypatch.setattr(pg_credentials, "validate_passfile", lambda *a, **k: None)


def _admin(code=0, calls=None):
    def main(argv, *, environ):
        if calls is not None:
            calls.append((list(argv), dict(environ)))
        return code

    return main


def _run(
    arguments,
    *,
    admin=None,
    engine=None,
    verifier=None,
    environment=None,
    lock_factory=_NullLock,
    monkeypatch=None,
):
    if monkeypatch is not None:
        _no_passfile(monkeypatch)
    engine = engine or _Engine()
    code = oneshot.main(
        ["purge"] + list(arguments),
        environment or _environment(),
        admin_main=admin or _admin(),
        engine_factory=lambda: engine,
        lock_factory=lock_factory,
        purge_verifier=verifier or oneshot._verify_purge_connection,
    )
    return code, engine


# --- success paths ---------------------------------------------------------------------------


def test_a_real_run_forwards_only_the_retention_command_and_returns_its_status(
    monkeypatch, capsys
):
    calls = []
    engine = _Engine()
    code, _ = _run(
        [], admin=_admin(code=0, calls=calls), engine=engine, monkeypatch=monkeypatch
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert calls[0][0] == ["purge-expired"]
    assert calls[0][1]["PGUSER"] == "hh_purge"
    assert calls[0][1]["HOUSE_HELPER_DATABASE_URL"] == (
        "postgresql+psycopg://hh_purge@house-helper-db:5432/house_helper"
    )
    assert engine.disposed is True


def test_a_dry_run_is_forwarded_verbatim(monkeypatch, capsys):
    calls = []
    code, _ = _run(
        ["--dry-run"],
        admin=_admin(code=0, calls=calls),
        monkeypatch=monkeypatch,
    )
    assert code == 0
    assert calls[0][0] == ["purge-expired", "--dry-run"]


@pytest.mark.parametrize("status", [1, 3, 4, 130])
def test_a_partial_or_refused_admin_run_keeps_its_nonzero_status(
    status, monkeypatch, capsys
):
    # The one-shot adds no success line of its own: a degraded run stays degraded, so the
    # systemd unit and its heartbeat cannot record a success that did not happen.
    code, _ = _run([], admin=_admin(code=status), monkeypatch=monkeypatch)
    captured = capsys.readouterr()
    assert code == status
    assert "ok [" not in captured.out


# --- the bounded argument surface ------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [["--delete"], ["--dry-run", "extra"], ["purge-expired"], ["--dry-run=false"]],
)
def test_any_other_argument_is_a_usage_refusal_without_a_connection(arguments, capsys):
    engine = _Engine()
    code = oneshot.main(
        ["purge"] + arguments,
        _environment(),
        admin_main=_admin(),
        engine_factory=lambda: engine,
        lock_factory=_NullLock,
        purge_verifier=lambda _engine: pytest.fail("the verifier must not run"),
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "purge_arguments" in captured.err
    assert engine.disposed is False


# --- the wiring, passfile and lock gates -----------------------------------------------------


def test_another_login_is_refused_before_the_admin_command(capsys):
    environment = _environment()
    environment["PGUSER"] = "hh_app"
    environment["HOUSE_HELPER_DATABASE_URL"] = (
        "postgresql+psycopg://hh_app@house-helper-db:5432/house_helper"
    )
    code = oneshot.main(
        ["purge"],
        environment,
        admin_main=_admin(),
        engine_factory=lambda: _Engine(),
        lock_factory=_NullLock,
    )
    captured = capsys.readouterr()
    assert code == 78
    assert "oneshot_login" in captured.err


def test_the_real_passfile_is_validated_for_this_login(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(oneshot, "PASSFILE_DIR", str(tmp_path))
    passfile = tmp_path / "hh_purge.pgpass"
    passfile.write_text(
        "house-helper-db:5432:house_helper:hh_purge:not-a-real-secret\n", encoding="utf-8"
    )
    if os.name == "posix":
        passfile.chmod(0o600)
    environment = _environment()
    environment["PGPASSFILE"] = f"{tmp_path}/hh_purge.pgpass"
    calls = []
    code, _ = _run(
        [],
        admin=_admin(calls=calls),
        engine=_Engine(),
        environment=environment,
        verifier=lambda _engine: None,
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert calls


def test_a_passfile_entry_for_another_login_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(oneshot, "PASSFILE_DIR", str(tmp_path))
    passfile = tmp_path / "hh_purge.pgpass"
    passfile.write_text(
        "house-helper-db:5432:house_helper:hh_app:not-a-real-secret\n", encoding="utf-8"
    )
    if os.name == "posix":
        passfile.chmod(0o600)
    environment = _environment()
    environment["PGPASSFILE"] = f"{tmp_path}/hh_purge.pgpass"
    calls = []
    code = oneshot.main(
        ["purge"],
        environment,
        admin_main=_admin(calls=calls),
        engine_factory=lambda: _Engine(),
        lock_factory=_NullLock,
        purge_verifier=lambda _engine: pytest.fail("the verifier must not run"),
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "passfile_role" in captured.err
    assert calls == []


def test_the_shared_state_volume_lock_is_taken_and_a_busy_volume_refuses(
    monkeypatch, capsys
):
    _no_passfile(monkeypatch)
    seen = []

    def busy(purpose):
        seen.append(purpose)
        raise _LockBusy()

    calls = []
    engine = _Engine()
    code = oneshot.main(
        ["purge"],
        _environment(),
        admin_main=_admin(calls=calls),
        engine_factory=lambda: engine,
        lock_factory=busy,
        purge_verifier=lambda _engine: pytest.fail("the verifier must not run"),
    )
    captured = capsys.readouterr()
    assert seen == ["the PostgreSQL purge command"]
    assert code == 3
    assert "maintenance_lock_busy" in captured.err
    assert calls == []
    assert engine.disposed is False


# --- the enrolled-cluster / H3 gate ----------------------------------------------------------


def _target(**overrides):
    values = {
        "session_user": "hh_purge",
        "current_user": "hh_purge",
        "database": "house_helper",
        "connect": True,
        "temporary": False,
        "marker": MARKER,
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({}, ()),
        ({"session_user": "hh_app"}, ("session_user",)),
        ({"current_user": "hh_app"}, ("current_user",)),
        ({"database": "postgres"}, ("database",)),
        ({"connect": False}, ("connect_handoff",)),
        ({"temporary": True}, ("temporary_privilege",)),
        ({"marker": ""}, ("marker",)),
        ({"marker": "{}"}, ("marker",)),
    ],
)
def test_the_purge_target_decision(overrides, expected):
    assert oneshot.purge_target_failures(**_target(**overrides)) == expected


@pytest.mark.parametrize(
    "rows, code",
    [
        (
            {
                "session": ("hh_app", "hh_app", "house_helper"),
                "privilege": (True, False),
                "marker": (MARKER,),
            },
            "purge_identity",
        ),
        (
            {
                "session": ("hh_purge", "hh_purge", "postgres"),
                "privilege": (True, False),
                "marker": (MARKER,),
            },
            "purge_identity",
        ),
        (
            {
                "session": ("hh_purge", "hh_purge", "house_helper"),
                "privilege": (False, False),
                "marker": (MARKER,),
            },
            "purge_connect_handoff",
        ),
        (
            {
                "session": ("hh_purge", "hh_purge", "house_helper"),
                "privilege": (True, True),
                "marker": (MARKER,),
            },
            "purge_privilege_boundary",
        ),
        (
            {
                "session": ("hh_purge", "hh_purge", "house_helper"),
                "privilege": (True, False),
                "marker": ("not a marker",),
            },
            "purge_marker",
        ),
    ],
)
def test_a_failed_target_gate_prevents_the_admin_command(rows, code, monkeypatch, capsys):
    _no_passfile(monkeypatch)
    calls = []
    engine = _Engine(rows=rows)
    result, _ = _run([], admin=_admin(calls=calls), engine=engine)
    captured = capsys.readouterr()
    assert result == 3
    assert code in captured.err
    assert calls == []
    assert engine.disposed is True


def test_a_denial_the_driver_tags_with_42501_names_the_missing_h3_handoff():
    # The one case in which 42501 is structured evidence: the driver supplied a SQLSTATE
    # (psycopg with a PGresult, psycopg 2 through `pgcode`, or a denial raised after the
    # session exists). The pinned driver does NOT supply one for a startup refusal - see the
    # next tests - so this is the only shape that may be read as "the handoff is missing".
    error = sa.exc.DBAPIError("SELECT 1", {}, structured_denial())
    with pytest.raises(oneshot.PurgeError) as refused:
        oneshot._verify_purge_connection(_Engine(error=error))
    assert refused.value.code == "purge_connect_handoff"
    assert refused.value.exit_code == 3
    assert "H3" in refused.value.message


@pytest.mark.parametrize(
    "message",
    [
        'permission denied for database "house_helper"',  # the server's 42501 text
        'password authentication failed for user "hh_purge"',  # 28P01
        'database "house_helper" does not exist',  # 3D000
    ],
)
def test_a_startup_refusal_without_a_sqlstate_is_one_fail_closed_refusal(message):
    # Driver-faithful (F-02): psycopg 3.3.6 raises the startup failure with `pgconn=` only, so
    # `sqlstate`, `diag.sqlstate` and `pgcode` are all absent even when the server sent 42501.
    # The message text is libpq's and localized, so it is never read as authorization
    # evidence: every startup refusal is one `purge_database_unavailable` (exit 4) that names
    # the ambiguity and the handoff to verify - never a guessed `purge_connect_handoff`.
    error = sa.exc.DBAPIError("SELECT 1", {}, startup_refusal(message))
    with pytest.raises(oneshot.PurgeError) as refused:
        oneshot._verify_purge_connection(_Engine(error=error))
    assert refused.value.code == "purge_database_unavailable"
    assert refused.value.exit_code == 4
    assert "H3" in refused.value.message
    assert "enroll.py status" in refused.value.message
    # Neither the statement nor the driver's text is repeated back.
    assert "SELECT 1" not in refused.value.message
    assert message not in refused.value.message


def test_the_statement_text_of_a_startup_refusal_is_not_classified_and_not_leaked():
    # A sharper negative control: a statement and driver text that both mention the handoff.
    error = sa.exc.DBAPIError(
        'GRANT CONNECT ON DATABASE "house_helper" TO "hh_purge"',
        {},
        startup_refusal('permission denied for database "house_helper"'),
    )
    with pytest.raises(oneshot.PurgeError) as refused:
        oneshot._verify_purge_connection(_Engine(error=error))
    assert refused.value.code == "purge_database_unavailable"
    assert "GRANT" not in refused.value.message


def test_a_transport_timeout_is_the_same_fail_closed_database_refusal():
    # The closed-port variant: psycopg raises ConnectionTimeout, with no PGconn at all.
    error = sa.exc.DBAPIError(None, {}, startup_timeout())
    with pytest.raises(oneshot.PurgeError) as refused:
        oneshot._verify_purge_connection(_Engine(error=error))
    assert refused.value.code == "purge_database_unavailable"
    assert refused.value.exit_code == 4
    assert "connection timeout" not in refused.value.message


def test_a_startup_refusal_prevents_the_admin_command_and_exits_four(monkeypatch, capsys):
    _no_passfile(monkeypatch)
    calls = []
    code, engine = _run(
        [],
        admin=_admin(calls=calls),
        engine=_Engine(
            error=sa.exc.DBAPIError(
                "SELECT 1",
                {},
                startup_refusal('permission denied for database "house_helper"'),
            )
        ),
        monkeypatch=monkeypatch,
    )
    captured = capsys.readouterr()
    assert code == 4
    assert "purge_database_unavailable" in captured.err
    assert "purge_connect_handoff" not in captured.err
    assert calls == []
    assert engine.disposed is True


def test_any_other_connection_failure_is_a_database_refusal_without_driver_text():
    class _Unavailable(Exception):
        pass

    error = sa.exc.DBAPIError("SELECT 1", {}, _Unavailable())
    with pytest.raises(oneshot.PurgeError) as refused:
        oneshot._verify_purge_connection(_Engine(error=error))
    assert refused.value.code == "purge_database_unavailable"
    assert refused.value.exit_code == 4
    assert "SELECT 1" not in refused.value.message


def test_a_failed_gate_prevents_the_admin_command(monkeypatch, capsys):
    _no_passfile(monkeypatch)
    calls = []
    code, engine = _run(
        [],
        admin=_admin(calls=calls),
        engine=_Engine(
            rows={
                "session": ("hh_purge", "hh_purge", "house_helper"),
                "privilege": (False, False),
                "marker": (MARKER,),
            }
        ),
        monkeypatch=monkeypatch,
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "purge_connect_handoff" in captured.err
    assert calls == []
    assert engine.disposed is True
