"""Offline credential and one-shot boundary tests (HH-DB-01 WP-2B / WP-2C-A)."""

from __future__ import annotations

import importlib.util
import io
import os
import stat
import sys
from pathlib import Path

import pytest

from house_helper.persistence import pg_credentials
from house_helper.persistence import pg_executor
from house_helper.persistence import pg_journal

REPO_ROOT = Path(__file__).resolve().parent.parent
ONESHOT_PATH = REPO_ROOT / "deploy" / "ops" / "pg_oneshot.py"

SECRET = "sentinel-must-never-be-printed-3f9a"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


oneshot = _load("deploy_pg_oneshot_wp2c", ONESHOT_PATH)


def _passfile(tmp_path: Path, content: str, *, mode: int = 0o600) -> Path:
    path = tmp_path / "hh_migrate.pgpass"
    path.write_text(content, encoding="utf-8")
    if os.name == "posix":
        path.chmod(mode)
    return path


def _validate(path, **overrides):
    values = dict(
        login="hh_migrate",
        host="house-helper-db",
        port="5432",
        database="house_helper",
        require_private=os.name == "posix",
        require_owner=os.name == "posix",
    )
    values.update(overrides)
    pg_credentials.validate_passfile(path, **values)


def test_a_matching_private_passfile_is_accepted_without_returning_the_password(tmp_path):
    path = _passfile(tmp_path, f"house-helper-db:5432:house_helper:hh_migrate:{SECRET}\n")
    assert _validate(path) is None


def test_a_missing_file_or_an_entry_for_another_login_is_refused_without_the_secret(tmp_path):
    with pytest.raises(pg_credentials.PassfileError) as missing:
        _validate(tmp_path / "absent.pgpass")
    assert missing.value.code == "passfile_missing"

    path = _passfile(tmp_path, f"house-helper-db:5432:house_helper:hh_app:{SECRET}\n")
    with pytest.raises(pg_credentials.PassfileError) as wrong:
        _validate(path)
    assert wrong.value.code == "passfile_role"
    assert SECRET not in str(wrong.value)


def test_a_line_for_another_database_or_a_malformed_line_is_refused(tmp_path):
    path = _passfile(tmp_path, f"house-helper-db:5432:postgres:hh_migrate:{SECRET}\n")
    with pytest.raises(pg_credentials.PassfileError) as wrong:
        _validate(path)
    assert wrong.value.code == "passfile_entry"

    path = _passfile(tmp_path, f"house-helper-db:5432:house_helper:hh_migrate\n")
    with pytest.raises(pg_credentials.PassfileError) as malformed:
        _validate(path)
    assert malformed.value.code == "passfile_format"


def test_a_symlink_is_refused(tmp_path):
    target = _passfile(tmp_path, f"house-helper-db:5432:house_helper:hh_migrate:{SECRET}\n")
    link = tmp_path / "link.pgpass"
    if os.name == "posix":
        link.symlink_to(target)
    else:  # pragma: no cover - Windows symlinks need a privilege
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks are unavailable")
    with pytest.raises(pg_credentials.PassfileError) as refused:
        _validate(link)
    assert refused.value.code == "passfile_symlink"


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are the deploy contract")
def test_a_group_readable_passfile_is_refused(tmp_path):
    path = _passfile(
        tmp_path,
        f"house-helper-db:5432:house_helper:hh_migrate:{SECRET}\n",
        mode=0o644,
    )
    with pytest.raises(pg_credentials.PassfileError) as refused:
        _validate(path)
    assert refused.value.code == "passfile_mode"


class _Stat:
    def __init__(self, *, uid: int, gid: int, mode: int = 0o600):
        self.st_mode = stat.S_IFREG | mode
        self.st_uid = uid
        self.st_gid = gid
        self.st_size = 64


def _owned_passfile(tmp_path, monkeypatch, *, uid: int, gid: int):
    path = _passfile(
        tmp_path, f"house-helper-db:5432:house_helper:hh_migrate:{SECRET}\n"
    )
    monkeypatch.setattr(pg_credentials, "_POSIX", True)
    monkeypatch.setattr(
        pg_credentials.Path, "lstat", lambda self: _Stat(uid=uid, gid=gid)
    )
    return path


def test_the_host_requires_the_container_runtime_10001_owner(tmp_path, monkeypatch):
    path = _owned_passfile(tmp_path, monkeypatch, uid=0, gid=0)
    # Host root must NOT accept a passfile that happens to be root-owned: the file is
    # consumed by a container running as 10001:10001.
    with pytest.raises(pg_credentials.PassfileError) as refused:
        pg_credentials.validate_passfile(
            path,
            login="hh_migrate",
            host="house-helper-db",
            port="5432",
            database="house_helper",
            owner_uid=pg_credentials.RUNTIME_UID,
            owner_gid=pg_credentials.RUNTIME_GID,
        )
    assert refused.value.code == "passfile_owner"


def test_the_host_accepts_exactly_the_runtime_10001_ownership(tmp_path, monkeypatch):
    path = _owned_passfile(tmp_path, monkeypatch, uid=10001, gid=10001)
    assert (
        pg_credentials.validate_passfile(
            path,
            login="hh_migrate",
            host="house-helper-db",
            port="5432",
            database="house_helper",
            owner_uid=pg_credentials.RUNTIME_UID,
            owner_gid=pg_credentials.RUNTIME_GID,
        )
        is None
    )


def test_the_host_requires_the_runtime_group_not_only_the_uid(tmp_path, monkeypatch):
    path = _owned_passfile(tmp_path, monkeypatch, uid=10001, gid=0)
    with pytest.raises(pg_credentials.PassfileError) as refused:
        pg_credentials.validate_passfile(
            path,
            login="hh_migrate",
            host="house-helper-db",
            port="5432",
            database="house_helper",
            owner_uid=pg_credentials.RUNTIME_UID,
            owner_gid=pg_credentials.RUNTIME_GID,
        )
    assert refused.value.code == "passfile_group"


def test_inside_the_container_the_current_process_identity_is_used(tmp_path, monkeypatch):
    path = _owned_passfile(tmp_path, monkeypatch, uid=10001, gid=10001)
    monkeypatch.setattr(pg_credentials, "_current_uid", lambda: 10001)
    monkeypatch.setattr(pg_credentials, "_current_gid", lambda: 10001)
    assert (
        pg_credentials.validate_passfile(
            path,
            login="hh_migrate",
            host="house-helper-db",
            port="5432",
            database="house_helper",
        )
        is None
    )
    monkeypatch.setattr(pg_credentials, "_current_uid", lambda: 0)
    monkeypatch.setattr(pg_credentials, "_current_gid", lambda: 0)
    with pytest.raises(pg_credentials.PassfileError) as refused:
        pg_credentials.validate_passfile(
            path,
            login="hh_migrate",
            host="house-helper-db",
            port="5432",
            database="house_helper",
        )
    assert refused.value.code == "passfile_owner"


def test_the_host_summary_reports_the_runtime_owner(tmp_path, monkeypatch):
    path = _owned_passfile(tmp_path, monkeypatch, uid=10001, gid=10001)
    result = pg_credentials.summary(
        path,
        login="hh_migrate",
        host="house-helper-db",
        port="5432",
        database="house_helper",
        owner_uid=pg_credentials.RUNTIME_UID,
        owner_gid=pg_credentials.RUNTIME_GID,
    )
    assert result == {"exists": True, "private": True, "owned": True, "entry": True}
    zero = _owned_passfile(tmp_path, monkeypatch, uid=0, gid=0)
    assert pg_credentials.summary(
        zero,
        login="hh_migrate",
        host="house-helper-db",
        port="5432",
        database="house_helper",
        owner_uid=pg_credentials.RUNTIME_UID,
        owner_gid=pg_credentials.RUNTIME_GID,
    )["owned"] is False


def test_a_second_credential_source_or_a_tls_choice_is_refused():
    for name in pg_credentials.FORBIDDEN_SECRET_VARIABLES:
        assert pg_credentials.environment_failures({name: SECRET}) == (name,)
    with pytest.raises(pg_credentials.PassfileError) as refused:
        pg_credentials.provider(
            {"PGPASSWORD": SECRET},
            login="hh_migrate",
            host="house-helper-db",
            port="5432",
            database="house_helper",
            validate=False,
        )
    assert SECRET not in str(refused.value)


def test_the_summary_reports_booleans_and_never_a_value(tmp_path):
    path = _passfile(tmp_path, f"house-helper-db:5432:house_helper:hh_migrate:{SECRET}\n")
    result = pg_credentials.summary(
        path, login="hh_migrate", host="house-helper-db", port="5432", database="house_helper"
    )
    assert result == {"exists": True, "private": True, "owned": True, "entry": True}
    assert SECRET not in repr(result)


# --- the migrate one-shot boundary ------------------------------------------------------------


def _environment(role: str = "migrate") -> dict:
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


def test_a_direct_migrate_invocation_without_the_authorization_channel_is_refused(capsys):
    code = oneshot.main(["migrate"], _environment(), stdin=io.BytesIO(b"{}"))
    captured = capsys.readouterr()
    assert code == 3
    assert "authorization_channel" in captured.err


def test_an_opened_channel_with_no_record_is_refused(capsys):
    environment = _environment()
    environment[oneshot.AUTHORIZATION_CHANNEL_VAR] = "1"
    code = oneshot.main(["migrate"], environment, stdin=io.BytesIO(b""))
    captured = capsys.readouterr()
    assert code == 3
    assert "authorization" in captured.err


def test_an_opened_channel_with_a_tty_is_refused(capsys, monkeypatch):
    environment = _environment()
    environment[oneshot.AUTHORIZATION_CHANNEL_VAR] = "1"

    class Tty(io.BytesIO):
        def isatty(self):
            return True

    code = oneshot.main(["migrate"], environment, stdin=Tty(b"{}"))
    captured = capsys.readouterr()
    assert code == 3
    assert "authorization" in captured.err


def test_an_oversized_record_is_refused(capsys):
    environment = _environment()
    environment[oneshot.AUTHORIZATION_CHANNEL_VAR] = "1"
    code = oneshot.main(
        ["migrate"],
        environment,
        stdin=io.BytesIO(b"x" * (pg_journal.AUTHORIZATION_MAX_BYTES + 1)),
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "authorization" in captured.err


class _FakeExecutor:
    def __init__(self):
        self.payload = None

    def run(self, payload):
        self.payload = payload
        return pg_executor.ExecutionReport(
            plan="fresh_initialization",
            revision="0014_user_measure_overrides",
            head="0014_user_measure_overrides",
            actions=("authorization",),
        )


def test_an_authorized_run_reaches_the_executor_with_the_record(capsys, monkeypatch):
    from house_helper.persistence import pg_credentials as credentials

    monkeypatch.setattr(credentials, "validate_passfile", lambda *a, **k: None)
    environment = _environment()
    environment[oneshot.AUTHORIZATION_CHANNEL_VAR] = "1"
    fake = _FakeExecutor()
    code = oneshot.main(
        ["migrate"], environment, stdin=io.BytesIO(b'{"sentinel":"record"}'), executor=fake
    )
    captured = capsys.readouterr()
    assert code == 0
    assert fake.payload == b'{"sentinel":"record"}'
    assert "ok [migrate]" in captured.out


# --- the enrollment schema one-shot (F8) ------------------------------------------------------


def _schema_record(**overrides) -> bytes:
    from house_helper.persistence import pg_artifacts

    value = pg_journal.build_schema_authorization(
        cluster={
            "container_name": "house-helper-db",
            "container_id": "b" * 64,
            "image_digest": "postgres@sha256:" + "c" * 64,
            "volume": "house-helper-pgdata",
            "system_identifier": "726485930221",
        },
        database="house_helper",
        artifacts=dict(pg_artifacts.manifest()),
        run_id="d" * 32,
        issued=pg_journal.utcnow(),
    )
    value.update(overrides)
    return pg_journal.encode_schema_authorization(value)


class _NullLock:
    def __init__(self, purpose: str) -> None:
        self.purpose = purpose

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_the_schema_one_shot_requires_the_explicit_channel(capsys):
    code = oneshot.main(["schema"], _environment("schema"), stdin=io.BytesIO(b"{}"))
    assert code == 3
    assert "authorization_channel" in capsys.readouterr().err


def test_the_schema_one_shot_refuses_a_migrate_authorization(capsys, monkeypatch):
    from house_helper.persistence import pg_credentials as credentials

    monkeypatch.setattr(credentials, "validate_passfile", lambda *a, **k: None)
    environment = _environment("schema")
    environment[oneshot.AUTHORIZATION_CHANNEL_VAR] = "1"
    code = oneshot.main(
        ["schema"],
        environment,
        stdin=io.BytesIO(_schema_record(role="migrate")),
    )
    assert code == 3
    assert "authorization" in capsys.readouterr().err


def test_an_authorized_schema_run_reaches_only_the_frozen_schema_step(capsys, monkeypatch):
    from house_helper.persistence import pg_credentials as credentials
    from house_helper.persistence import pg_executor

    monkeypatch.setattr(credentials, "validate_passfile", lambda *a, **k: None)
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        pg_executor,
        "apply_schema_contract",
        lambda engine, authorization: calls.append((engine, authorization)),
    )
    engine = _FakeEngine()
    environment = _environment("schema")
    environment[oneshot.AUTHORIZATION_CHANNEL_VAR] = "1"
    code = oneshot.main(
        ["schema"],
        environment,
        stdin=io.BytesIO(_schema_record()),
        engine_factory=lambda: engine,
        lock_factory=_NullLock,
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert len(calls) == 1 and calls[0][0] is engine
    # F8: the decoded record reaches the step; it is consumed, not discarded.
    assert calls[0][1]["role"] == "schema"
    assert calls[0][1]["cluster"]["container_name"] == "house-helper-db"
    assert engine.disposed is True
    assert "ok [schema]" in captured.out


def test_the_schema_one_shot_uses_the_migrate_login_only(capsys):
    environment = _environment("schema")
    environment["PGUSER"] = "hh_app"
    code = oneshot.main(["schema"], environment, stdin=io.BytesIO(b"{}"))
    assert code == 78
    assert "oneshot_login" in capsys.readouterr().err


def test_the_unimplemented_one_shots_keep_their_refusal(capsys):
    for role in ("provision", "purge", "backup", "restore"):
        code = oneshot.main([role], _environment(role))
        captured = capsys.readouterr()
        assert code == 78
        assert "oneshot_not_implemented" in captured.err


def test_the_bootstrap_password_retirement_is_a_designed_plan_only():
    from house_helper.persistence import pg_enrollment

    plan = pg_enrollment.bootstrap_password_retirement_plan()
    assert len(plan) == 6
    joined = " ".join(plan)
    assert "ALTER ROLE postgres PASSWORD NULL" in joined
    assert "POSTGRES_PASSWORD_FILE" in joined
    assert "Mode B" in joined
    # The function returns text; it has no engine, connection, Docker or subprocess handle.
    assert all(isinstance(step, str) for step in plan)


def test_a_second_credential_source_is_refused_before_anything_is_executed(capsys):
    environment = _environment()
    environment["PGPASSWORD"] = SECRET
    code = oneshot.main(["migrate"], environment, stdin=io.BytesIO(b"{}"))
    captured = capsys.readouterr()
    assert code == 78
    assert "oneshot_credential_source" in captured.err
    assert SECRET not in captured.out + captured.err
