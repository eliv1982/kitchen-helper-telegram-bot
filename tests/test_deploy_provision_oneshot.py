"""The P0-2 provision one-shot: two administrative commands as `hh_provision`.

No database is contacted. The success paths use a scripted engine (session, privilege and
marker rows) and a stub admin CLI; the refusal paths exercise the real wiring, passfile and
argument validation. The admin CLI's own user/invitation rules are covered by
tests/test_admin_enrollment.py and tests/test_admin_enrollment_postgres.py.
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

from tests.pg_oneshot_failures import startup_refusal, structured_denial

REPO_ROOT = Path(__file__).resolve().parent.parent
ONESHOT_PATH = REPO_ROOT / "deploy" / "ops" / "pg_oneshot.py"

SECRET = "invitation-SECRET-must-appear-once-9f31"
MARKER = pg_journal.make_marker("f" * 32, "726485930221")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


oneshot = _load("deploy_pg_oneshot_p0", ONESHOT_PATH)


def _environment(role: str = "provision") -> dict:
    login = oneshot.EXPECTED_LOGIN[role]
    environment = {
        "HOUSE_HELPER_PG_ACTIVATE": "1",
        "PGUSER": login,
        "PGHOST": "house-helper-db",
        "PGPORT": "5432",
        "PGPASSFILE": f"/run/credentials/{login}.pgpass",
        "HOUSE_HELPER_STATE_DIR": "/state",
        "HOUSE_HELPER_DATABASE_URL": (
            f"postgresql+psycopg://{login}@house-helper-db:5432/house_helper"
        ),
    }
    if role != "restore":
        environment["PGDATABASE"] = "house_helper"
    return environment


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
            "session": ("hh_provision", "hh_provision", "house_helper"),
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


def _no_passfile(monkeypatch):
    monkeypatch.setattr(pg_credentials, "validate_passfile", lambda *a, **k: None)


def _admin(stdout_lines=(), code=0, calls=None):
    def main(argv, *, environ):
        if calls is not None:
            calls.append((list(argv), dict(environ)))
        for line in stdout_lines:
            print(line)
        return code

    return main


def _run(arguments, *, admin=None, engine=None, verifier=None, environment=None, monkeypatch=None):
    if monkeypatch is not None:
        _no_passfile(monkeypatch)
    engine = engine or _Engine()
    code = oneshot.main(
        ["provision"] + list(arguments),
        environment or _environment(),
        admin_main=admin or _admin(),
        engine_factory=lambda: engine,
        lock_factory=_NullLock,
        verifier=verifier or oneshot._verify_provision_connection,
    )
    return code, engine


# --- success paths ---------------------------------------------------------------------------


def test_enroll_user_runs_as_hh_provision_and_prints_the_secret_once(monkeypatch, capsys):
    calls = []
    admin = _admin(
        ["ok [enrolled]: created the user", f"invitation code: {SECRET}"], calls=calls
    )
    engine = _Engine()
    code, _ = _run(
        ["enroll-user", "--display-name", "Owner"],
        admin=admin,
        engine=engine,
        monkeypatch=monkeypatch,
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert calls[0][0] == ["enroll-user", "--display-name", "Owner"]
    assert calls[0][1]["PGUSER"] == "hh_provision"
    assert calls[0][1]["HOUSE_HELPER_DATABASE_URL"] == (
        "postgresql+psycopg://hh_provision@house-helper-db:5432/house_helper"
    )
    assert engine.disposed is True
    # The documented output mechanism prints the secret exactly once; the one-shot adds
    # no line of its own and never repeats or forwards the value.
    assert captured.out.count(SECRET) == 1
    assert SECRET not in captured.err


def test_enrollment_status_is_forwarded_read_only(monkeypatch, capsys):
    calls = []
    admin = _admin(["ok [status]: 1 user(s)."], calls=calls)
    code, _ = _run(
        ["enrollment-status"], admin=admin, monkeypatch=monkeypatch
    )
    assert code == 0
    assert calls[0][0] == ["enrollment-status"]
    assert "ok [status]" in capsys.readouterr().out


# --- the command allowlist and the wiring ----------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["provision-email", "reissue-invitation", "revoke-invitation", "purge-expired"],
)
def test_any_other_administrative_command_is_refused_without_a_connection(
    command, monkeypatch, capsys
):
    calls = []
    engine = _Engine()
    code = oneshot.main(
        ["provision", command, "--email", "not-a-real-command"],
        _environment(),
        admin_main=_admin(calls=calls),
        engine_factory=lambda: engine,
        lock_factory=_NullLock,
        verifier=lambda _engine: pytest.fail("the verifier must not run"),
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "provision_command" in captured.err
    assert calls == []
    assert engine.disposed is False
    # The rejected argument text is never echoed.
    assert "not-a-real-command" not in captured.out + captured.err


def test_a_provision_invocation_without_a_command_is_a_usage_error(capsys):
    engine = _Engine()
    code = oneshot.main(
        ["provision"],
        _environment(),
        admin_main=_admin(),
        engine_factory=lambda: engine,
        lock_factory=_NullLock,
        verifier=lambda _engine: pytest.fail("the verifier must not run"),
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "provision_command" in captured.err
    assert engine.disposed is False


def test_another_login_is_refused_before_the_admin_command(capsys):
    environment = _environment()
    environment["PGUSER"] = "hh_app"
    environment["HOUSE_HELPER_DATABASE_URL"] = (
        "postgresql+psycopg://hh_app@house-helper-db:5432/house_helper"
    )
    code = oneshot.main(
        ["provision", "enroll-user", "--display-name", "Owner"],
        environment,
        admin_main=_admin(),
        engine_factory=lambda: _Engine(),
        lock_factory=_NullLock,
    )
    captured = capsys.readouterr()
    assert code == 78
    assert "oneshot_login" in captured.err


def test_a_second_credential_source_is_refused_without_printing_it(capsys):
    environment = _environment()
    environment["PGPASSWORD"] = SECRET
    code = oneshot.main(
        ["provision", "enroll-user", "--display-name", "Owner"],
        environment,
        admin_main=_admin(),
        engine_factory=lambda: _Engine(),
        lock_factory=_NullLock,
    )
    captured = capsys.readouterr()
    assert code == 78
    assert "oneshot_credential_source" in captured.err
    assert SECRET not in captured.out + captured.err


def test_the_real_passfile_is_validated_for_this_login(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(oneshot, "PASSFILE_DIR", str(tmp_path))
    passfile = tmp_path / "hh_provision.pgpass"
    passfile.write_text(
        f"house-helper-db:5432:house_helper:hh_provision:{SECRET}\n", encoding="utf-8"
    )
    if os.name == "posix":
        passfile.chmod(0o600)
    environment = _environment()
    environment["PGPASSFILE"] = f"{tmp_path}/hh_provision.pgpass"
    calls = []
    admin = _admin(calls=calls)
    engine = _Engine()
    code = oneshot.main(
        ["provision", "enrollment-status"],
        environment,
        admin_main=admin,
        engine_factory=lambda: engine,
        lock_factory=_NullLock,
        verifier=lambda _engine: None,
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert calls
    assert SECRET not in captured.out + captured.err


def test_a_passfile_entry_for_another_login_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(oneshot, "PASSFILE_DIR", str(tmp_path))
    passfile = tmp_path / "hh_provision.pgpass"
    passfile.write_text(
        f"house-helper-db:5432:house_helper:hh_app:{SECRET}\n", encoding="utf-8"
    )
    if os.name == "posix":
        passfile.chmod(0o600)
    environment = _environment()
    environment["PGPASSFILE"] = f"{tmp_path}/hh_provision.pgpass"
    calls = []
    code = oneshot.main(
        ["provision", "enrollment-status"],
        environment,
        admin_main=_admin(calls=calls),
        engine_factory=lambda: _Engine(),
        lock_factory=_NullLock,
        verifier=lambda _engine: pytest.fail("the verifier must not run"),
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "passfile_role" in captured.err
    assert calls == []
    assert SECRET not in captured.out + captured.err


# --- the enrolled-cluster / H2 gate ----------------------------------------------------------


def _target(**overrides):
    values = dict(
        session_user="hh_provision",
        current_user="hh_provision",
        database="house_helper",
        connect=True,
        temporary=False,
        marker=MARKER,
    )
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
def test_the_provision_target_decision(overrides, expected):
    assert oneshot.provision_target_failures(**_target(**overrides)) == expected


@pytest.mark.parametrize(
    "rows, code",
    [
        (
            {
                "session": ("hh_app", "hh_provision", "house_helper"),
                "privilege": (True, False),
                "marker": (MARKER,),
            },
            "provision_identity",
        ),
        (
            {
                "session": ("hh_provision", "hh_app", "house_helper"),
                "privilege": (True, False),
                "marker": (MARKER,),
            },
            "provision_identity",
        ),
        (
            {
                "session": ("hh_provision", "hh_provision", "postgres"),
                "privilege": (True, False),
                "marker": (MARKER,),
            },
            "provision_identity",
        ),
        (
            {
                "session": ("hh_provision", "hh_provision", "house_helper"),
                "privilege": (False, False),
                "marker": (MARKER,),
            },
            "provision_connect_handoff",
        ),
        (
            {
                "session": ("hh_provision", "hh_provision", "house_helper"),
                "privilege": (True, True),
                "marker": (MARKER,),
            },
            "provision_privilege_boundary",
        ),
        (
            {
                "session": ("hh_provision", "hh_provision", "house_helper"),
                "privilege": (True, False),
                "marker": ("",),
            },
            "provision_marker",
        ),
    ],
)
def test_the_live_gate_names_the_failed_part(rows, code):
    with pytest.raises(oneshot.ProvisionError) as refused:
        oneshot._verify_provision_connection(_Engine(rows))
    assert refused.value.code == code
    assert refused.value.exit_code == 3


def test_a_denial_the_driver_tags_with_42501_names_the_missing_h2_handoff():
    # The one shape in which 42501 is structured evidence. The pinned psycopg 3.3.6 supplies
    # no SQLSTATE for a startup refusal (F-02, and the purge suite's driver-faithful tests),
    # so it is the only shape that may be read as "the handoff is missing".
    error = sa.exc.DBAPIError("SELECT 1", {}, structured_denial())
    with pytest.raises(oneshot.ProvisionError) as refused:
        oneshot._verify_provision_connection(_Engine(error=error))
    assert refused.value.code == "provision_connect_handoff"
    assert refused.value.exit_code == 3
    assert "H2" in refused.value.message


def test_a_driver_faithful_startup_refusal_is_fail_closed_and_names_the_h2_handoff():
    # Driver-faithful (F-02): no SQLSTATE, only a finished PGconn. The one-shot reports the
    # ambiguity, names the handoff and never guesses the cause from the message text.
    error = sa.exc.DBAPIError(
        "SELECT 1", {}, startup_refusal('permission denied for database "house_helper"')
    )
    with pytest.raises(oneshot.ProvisionError) as refused:
        oneshot._verify_provision_connection(_Engine(error=error))
    assert refused.value.code == "provision_database_unavailable"
    assert refused.value.exit_code == 4
    assert "H2" in refused.value.message
    assert "enroll.py status" in refused.value.message
    assert "permission denied" not in refused.value.message


def test_any_other_connection_failure_is_a_database_refusal_without_driver_text():
    class _Unavailable(Exception):
        pass

    error = sa.exc.DBAPIError("SELECT 1", {}, _Unavailable())
    with pytest.raises(oneshot.ProvisionError) as refused:
        oneshot._verify_provision_connection(_Engine(error=error))
    assert refused.value.code == "provision_database_unavailable"
    assert refused.value.exit_code == 4
    assert "SELECT 1" not in refused.value.message


def test_a_failed_gate_prevents_the_admin_command(monkeypatch, capsys):
    _no_passfile(monkeypatch)
    calls = []
    engine = _Engine()

    def verifier(_engine):
        raise oneshot.ProvisionError("provision_marker", "no marker", 3)

    code = oneshot.main(
        ["provision", "enroll-user", "--display-name", "Owner"],
        _environment(),
        admin_main=_admin(calls=calls),
        engine_factory=lambda: engine,
        lock_factory=_NullLock,
        verifier=verifier,
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "provision_marker" in captured.err
    assert calls == []
    assert engine.disposed is True


# --- the admin CLI's failures are propagated, never amplified --------------------------------


def test_a_refusal_from_the_admin_cli_is_propagated(monkeypatch, capsys):
    _no_passfile(monkeypatch)
    admin = _admin(["refused [duplicate_display_name]: ..."], code=3)
    code, _ = _run(
        ["enroll-user", "--display-name", "Owner"],
        admin=admin,
        monkeypatch=monkeypatch,
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "duplicate_display_name" in captured.out + captured.err


def test_an_unexpected_exception_is_not_printed_with_its_text(monkeypatch, capsys):
    _no_passfile(monkeypatch)

    def admin(_argv, *, environ):
        raise RuntimeError(SECRET)

    code, _ = _run(["enroll-user", "--display-name", "Owner"], admin=admin, monkeypatch=monkeypatch)
    captured = capsys.readouterr()
    assert code == 1
    assert "RuntimeError" in captured.err
    assert SECRET not in captured.out + captured.err


def test_an_interrupted_command_is_reported_and_never_echoes_its_arguments(
    monkeypatch, capsys
):
    _no_passfile(monkeypatch)

    def admin(_argv, *, environ):
        raise KeyboardInterrupt

    code, _ = _run(
        ["enroll-user", "--display-name", "Owner"],
        admin=admin,
        monkeypatch=monkeypatch,
    )
    captured = capsys.readouterr()
    assert code == 130
    assert "interrupted" in captured.err
    assert "Traceback" not in captured.err
    assert "Owner" not in captured.out + captured.err


# --- the real CLI entrypoint (no database is reached) ----------------------------------------


def _cli_environment() -> dict:
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HOUSE_HELPER_", "PG"))
    }
    environ.update(_environment())
    return environ


def _run_cli(arguments) -> object:
    import subprocess

    return subprocess.run(
        [sys.executable, str(ONESHOT_PATH), "provision", *arguments],
        env=_cli_environment(),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_real_cli_without_a_command_is_usage():
    done = _run_cli([])
    assert done.returncode == 2, done.stderr
    assert "provision_command" in done.stderr
    assert "Traceback" not in done.stderr


def test_the_real_cli_refuses_an_unsupported_command_before_connecting():
    done = _run_cli(["provision-email", "--email", "someone@example.invalid"])
    assert done.returncode == 3, done.stderr
    assert "provision_command" in done.stderr
    assert "Traceback" not in done.stderr
    assert "someone@example.invalid" not in done.stdout + done.stderr
