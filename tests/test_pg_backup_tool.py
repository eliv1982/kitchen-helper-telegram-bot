"""WP-2D: host-tool contracts that do not need a live cluster.

The real Docker/PostgreSQL rehearsal is separate evidence. These tests pin the parts
that must fail closed before any database is touched: the TOC filter, the verification
parser, the maintenance-database ACL check, the temporary-database record and the
disposable-container argv (no credential material on a command line).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from house_helper.persistence import pg_backup as backup
from house_helper.persistence import pg_contract
from house_helper.persistence import pg_credentials
from tests.test_pg_backup import _manifest

from deploy.postgres import maintenance


# --- TOC filtering ---------------------------------------------------------------------------


def test_the_restore_filter_removes_only_the_public_schema_entry():
    toc = (
        ";\n; header\n5; 2615 2200 SCHEMA - public hh_owner\n"
        "222; 1259 16502 SEQUENCE public messages_id_seq hh_owner\n"
        "3808; 0 0 SEQUENCE OWNED BY public messages_id_seq hh_owner\n"
        "3809; 0 0 SEQUENCE SET public messages_id_seq hh_owner\n"
    )
    filtered = maintenance._filtered_restore_list(toc)
    assert "SCHEMA - public" not in filtered
    assert "SEQUENCE SET public messages_id_seq" in filtered
    assert "SEQUENCE OWNED BY public messages_id_seq" in filtered


def test_the_filter_refuses_zero_or_two_public_schema_entries():
    with pytest.raises(maintenance.MaintenanceError):
        maintenance._filtered_restore_list(
            "222; 1259 16502 SEQUENCE public messages_id_seq hh_owner\n"
        )
    with pytest.raises(maintenance.MaintenanceError):
        maintenance._filtered_restore_list(
            "5; 2615 2200 SCHEMA - public hh_owner\n"
            "6; 2615 2201 SCHEMA - public hh_owner\n"
        )


# --- verification parsing --------------------------------------------------------------------


def _contract_lines(fixture):
    lines = []
    for relkind, schema, name, owner in fixture["relations"]:
        lines.append(f"relation|{relkind}|{schema}|{name}|{owner}")
    for table, ordinal, name, type_, notnull, default, collation in fixture["columns"]:
        lines.append(
            "column|{}|{}|{}|{}|{}|{}|{}".format(
                table, ordinal, name, type_, "t" if notnull else "f", default, collation
            )
        )
    for table, name, contype, validated, definition in fixture["constraints"]:
        lines.append(
            f"constraint|{table}|{name}|{contype}|{'t' if validated else 'f'}|{definition}"
        )
    for table, name, unique, primary, predicate, definition in fixture["indexes"]:
        lines.append(
            "index|{}|{}|{}|{}|{}|{}".format(
                table, name, "t" if unique else "f", "t" if primary else "f",
                predicate, definition,
            )
        )
    for (name, seqtypid, start, increment, maximum, minimum, cache, cycle, owned_by) in fixture[
        "sequence"
    ]:
        lines.append(
            "sequence|{}|{}|{}|{}|{}|{}|{}|{}|{}".format(
                name, seqtypid, start, increment, maximum, minimum, cache,
                "t" if cycle else "f", owned_by,
            )
        )
    return lines


def _verification_output(manifest, fixture):
    lines = [
        "identity|hh_restore|hh_owner",
        f"revision|{manifest['alembic_revision']}",
        "db_sequence|public.messages_id_seq|{}|{}|1|9223372036854775807|1|f|hh_owner|1".format(
            manifest["sequence"]["last_value"],
            "t" if manifest["sequence"]["is_called"] else "f",
        ),
        f"max_messages_id|{manifest['sequence']['max_messages_id']}",
        "owners|hh_owner",
        "schema|hh_owner|0",
        "not_valid|0",
    ]
    for name, fact in manifest["tables"].items():
        lines.append(f"table|{name}|{fact['rows']}|{fact['digest']}")
    for schema, objtype, grantee, privilege, grantable in pg_contract.EXPECTED_DEFAULT_ACLS:
        lines.append(
            f"hh_owner|{schema}|{objtype}|{grantee}|{privilege}|{'t' if grantable else 'f'}"
        )
    lines.extend(_contract_lines(fixture))
    return "\n".join(lines) + "\n"


def _fixture():
    return backup.load_object_contract_fixture(
        Path(__file__).parent / "fixtures" / "pg_object_contract.json"
    )


def test_a_matching_restored_catalog_has_no_verification_failures():
    manifest = _manifest()
    # The fixture's sequence is the real one; align the manifest's max row with the
    # synthetic table data used here.
    manifest["sequence"]["max_messages_id"] = 3
    output = _verification_output(manifest, _fixture())
    result = maintenance._parse_verification(output, manifest, fixture=_fixture())
    assert result.failures == []
    assert result.evidence["revision"] == "0014_user_measure_overrides"


def test_a_table_digest_mismatch_is_reported():
    manifest = _manifest()
    output = _verification_output(manifest, _fixture()).replace(
        f"table|messages|3|{manifest['tables']['messages']['digest']}",
        "table|messages|3|" + "0" * 64,
    )
    result = maintenance._parse_verification(output, manifest, fixture=_fixture())
    assert "table:mismatch:messages" in result.failures


def test_a_raw_sequence_mismatch_is_reported():
    manifest = _manifest()
    manifest["sequence"]["max_messages_id"] = 3
    output = _verification_output(manifest, _fixture()).replace(
        "db_sequence|public.messages_id_seq|5|t", "db_sequence|public.messages_id_seq|4|t"
    )
    result = maintenance._parse_verification(output, manifest, fixture=_fixture())
    assert "sequence:last_value_mismatch" in result.failures


def test_a_wrong_schema_owner_or_public_entry_is_reported():
    manifest = _manifest()
    manifest["sequence"]["max_messages_id"] = 3
    output = _verification_output(manifest, _fixture()).replace(
        "schema|hh_owner|0", "schema|postgres|1"
    )
    result = maintenance._parse_verification(output, manifest, fixture=_fixture())
    assert "schema:contract" in result.failures


def test_a_changed_constraint_contract_is_reported():
    manifest = _manifest()
    manifest["sequence"]["max_messages_id"] = 3
    fixture = _fixture()
    changed = json.loads(json.dumps(fixture))
    changed["constraints"][0][4] = "CHECK (false)"
    result = maintenance._parse_verification(
        _verification_output(manifest, changed), manifest, fixture=fixture
    )
    assert "object_contract:constraints" in result.failures


# --- the maintenance database preflight ------------------------------------------------------


GOOD_MAINTENANCE = "\n".join(
    [
        "postgres|hh_restore|hh_restore",
        "t|postgres|t|{postgres=CTc/postgres,hh_restore=c/postgres}",
        "10|postgres|CREATE|f",
        "10|postgres|TEMPORARY|f",
        "10|postgres|CONNECT|t",
        "16384|hh_restore|CONNECT|f",
        "t|f",
    ]
) + "\n"


def test_a_hardened_maintenance_database_is_accepted(monkeypatch):
    monkeypatch.setattr(maintenance, "psql", lambda *a, **k: GOOD_MAINTENANCE)
    maintenance.maintenance_preflight(
        object(),
        object(),
        maintenance_database="postgres",
        network="house-helper-pg-db",
    )


@pytest.mark.parametrize(
    "replacement",
    [
        ("postgres|hh_restore|hh_restore", "house_helper|hh_restore|hh_restore"),
        ("t|postgres|t|", "f|postgres|t|"),
        ("10|postgres|CONNECT|t", "10|postgres|CONNECT|t\n0|PUBLIC|CONNECT|f"),
        ("16384|hh_restore|CONNECT|f", "16384|hh_restore|TEMPORARY|f"),
        ("16384|hh_restore|CONNECT|f", "16384|hh_restore|CONNECT|t"),
        ("t|f", "f|f"),
        ("t|f", "t|t"),
    ],
)
def test_a_relaxed_maintenance_database_is_refused(monkeypatch, replacement):
    output = GOOD_MAINTENANCE.replace(*replacement)
    monkeypatch.setattr(maintenance, "psql", lambda *a, **k: output)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.maintenance_preflight(
            object(),
            object(),
            maintenance_database="postgres",
            network="house-helper-pg-db",
        )


# --- temporary database records --------------------------------------------------------------


def test_a_temporary_database_record_is_exact_and_bounded():
    payload = backup.temp_database_record(
        action="authorized",
        operation_id="a" * 32,
        token="b" * 16,
        database="house_helper_restore_" + "c" * 16,
        purpose="restore_verification",
        system_identifier="7709166311476633635",
        archive_format=backup.FORMAT_V2,
        archive_sha256="d" * 64,
        archive_system_identifier="7709166311476633635",
    )
    assert payload["database"].startswith("house_helper_restore_")
    assert payload["owner"] == "hh_owner"
    assert payload["connect_role"] == "hh_restore"
    with pytest.raises(backup.BackupError):
        backup.temp_database_record(
            action="authorized",
            operation_id="a" * 32,
            token="b" * 16,
            database="house_helper",
            purpose="restore_verification",
            system_identifier="1",
        )
    with pytest.raises(backup.BackupError):
        backup.temp_database_record(
            action="not-a-state",
            operation_id="a" * 32,
            token="b" * 16,
            database="house_helper_restore_" + "c" * 16,
            purpose="restore_verification",
            system_identifier="1",
        )


def test_only_the_newest_non_retired_record_authorizes_a_name():
    class Record:
        def __init__(self, seq, payload):
            self.seq = seq
            self.kind = "temp_db"
            self.payload = payload

    name = "house_helper_restore_" + "c" * 16
    base = {
        "action": "authorized",
        "operation_id": "a" * 32,
        "token": "c" * 16,
        "database": name,
        "purpose": "restore_verification",
        "system_identifier": "1",
    }
    records = [Record(0, base), Record(1, {**base, "action": "retired"})]
    assert backup.active_temporary_databases(records) == {}
    records = [Record(0, {**base, "action": "retired"}), Record(1, base)]
    assert list(backup.active_temporary_databases(records)) == [name]
    records = [Record(0, {**base, "database": "looks_temporary"})]
    assert backup.active_temporary_databases(records) == {}


# --- command construction --------------------------------------------------------------------


def test_the_disposable_argv_carries_no_password_and_only_the_role_passfile(tmp_path):
    passfile_path = tmp_path / "hh_backup.pgpass"
    passfile_path.write_text("host:5432:db:hh_backup:secret\n", encoding="utf-8")
    value = maintenance.Passfile(role="hh_backup", host_path=passfile_path)
    argv = maintenance._wrapped(value, ["psql", "-d", "house_helper", "-f", "-"])
    assert "secret" not in " ".join(argv)
    assert argv.count(value.container_path) == 1
    assert "PGPASSWORD" not in " ".join(argv)
    # The credential is copied inside the container, never passed as an environment value.
    assert "PGPASSFILE=/tmp/house-helper.pgpass" in argv[2]


def test_the_tool_refuses_a_client_image_that_is_not_the_pinned_digest():
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.ToolContainers("postgres:16")
    maintenance.ToolContainers(
        "postgres@sha256:" + "0" * 64
    )


class _RuntimeOwnedStat:
    """A passfile stat for the identity that consumes the mount (10001:10001, 0600)."""

    st_mode = stat.S_IFREG | pg_credentials.PASSFILE_MODE
    st_uid = pg_credentials.RUNTIME_UID
    st_gid = pg_credentials.RUNTIME_GID
    st_size = 64


def _report_runtime_ownership(monkeypatch, *paths):
    """Make `paths` look runtime-owned without a privileged chown and without a skip.

    The host tool validates a passfile against the container identity that will read
    it, but CI's `tmp_path` belongs to the runner. Only the named files report the
    production identity; every other `lstat` is the real one, so the POSIX ownership
    and mode comparisons still run exactly as production does.
    """
    reported = set(paths)
    real_lstat = pg_credentials.Path.lstat

    def lstat(self):
        if self in reported:
            return _RuntimeOwnedStat()
        return real_lstat(self)

    monkeypatch.setattr(pg_credentials.Path, "lstat", lstat)


def test_a_passfile_is_validated_before_it_is_mounted(tmp_path, monkeypatch):
    path = tmp_path / "hh_restore.pgpass"
    path.write_text("house-helper-db:5432:*:hh_restore:pw\n", encoding="utf-8")
    bad = tmp_path / "hh_backup.pgpass"
    bad.write_text("house-helper-db:5432:*:hh_restore:pw\n", encoding="utf-8")
    _report_runtime_ownership(monkeypatch, path, bad)
    value = maintenance.passfile(tmp_path, "hh_restore", database="*")
    assert value.role == "hh_restore"
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.passfile(tmp_path, "hh_backup", database="house_helper")


class _FakeToolRunner:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, argv, **kwargs):
        import subprocess

        stdout, returncode = self.responses.pop(0)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=b"")


def test_the_state_volume_is_verified_by_its_deployment_labels():
    import json

    good = json.dumps(
        [
            {
                "Name": maintenance.STATE_VOLUME,
                "Scope": "local",
                "Labels": {
                    "com.docker.compose.project": "house-helper-pg",
                    "com.docker.compose.volume": "state",
                },
            }
        ]
    ).encode()
    tools = maintenance.ToolContainers(
        "postgres@sha256:" + "0" * 64, runner=_FakeToolRunner([(good, 0)])
    )
    assert tools.inspect_volume(maintenance.STATE_VOLUME)["Name"] == maintenance.STATE_VOLUME
    bad = good.replace(b"house-helper-pg", b"other-project")
    tools = maintenance.ToolContainers(
        "postgres@sha256:" + "0" * 64, runner=_FakeToolRunner([(bad, 0)])
    )
    with pytest.raises(maintenance.MaintenanceError):
        tools.inspect_volume(maintenance.STATE_VOLUME)
