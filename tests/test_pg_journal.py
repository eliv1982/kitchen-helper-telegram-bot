"""Offline tests for the WP-2C-A journal, marker and authorization record.

Frozen source: ENR-13/ENR-14 (journal shape), LCK-06..LCK-09 (separate lock, append-only,
fsync, digest chain), ENR-05 (one marker), and the WP-2C-A authority boundary (bounded,
non-secret, time-bounded Mode B authorization).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from house_helper.persistence import pg_artifacts, pg_journal


def _journal(tmp_path: Path, name: str = "cutover") -> pg_journal.Journal:
    return pg_journal.Journal(tmp_path / name)


def test_a_new_journal_is_empty_and_anchored_at_genesis(tmp_path):
    view = _journal(tmp_path).read()
    assert view.records == ()
    assert view.head == pg_journal.GENESIS
    assert view.head_seq == -1


def test_records_are_canonical_sequenced_and_digest_chained(tmp_path):
    journal = _journal(tmp_path)
    first = journal.append("cluster_enrollment", {"state": "intent", "marker": "a"})
    second = journal.append("cluster_enrollment", {"state": "roles"})
    view = journal.read()
    assert [record.seq for record in view.records] == [0, 1]
    assert first.prev == pg_journal.GENESIS
    assert second.prev == first.digest
    assert view.head == second.digest
    assert view.head_seq == 1
    assert journal.head_path.read_text(encoding="utf-8") == json.dumps(
        {"digest": second.digest, "seq": 1}, sort_keys=True, separators=(",", ":")
    )


def test_appending_fsyncs_the_file_and_the_directory(tmp_path, monkeypatch):
    journal = _journal(tmp_path)
    synced: list[int] = []
    directories: list[Path] = []
    monkeypatch.setattr(pg_journal, "_sync", lambda fd: synced.append(fd))
    monkeypatch.setattr(pg_journal, "_sync_directory", lambda path: directories.append(path))
    journal.append("cluster_enrollment", {"state": "intent"})
    # one fsync for the journal file, one for the head file; the directory twice.
    assert len(synced) >= 2
    assert len(directories) >= 2
    assert all(isinstance(fd, int) for fd in synced)


def test_a_tail_edit_is_detected_by_the_head_anchor(tmp_path):
    journal = _journal(tmp_path)
    journal.append("cluster_enrollment", {"state": "intent"})
    journal.append("cluster_enrollment", {"state": "roles"})
    lines = journal.path.read_text(encoding="utf-8").splitlines()
    edited = json.loads(lines[-1])
    edited["payload"]["state"] = "completed"
    lines[-1] = json.dumps(edited, sort_keys=True, separators=(",", ":"))
    journal.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(pg_journal.JournalError) as refused:
        journal.read()
    assert refused.value.code in ("journal_head", "journal_chain")


def test_a_truncated_journal_is_detected(tmp_path):
    journal = _journal(tmp_path)
    journal.append("cluster_enrollment", {"state": "intent"})
    journal.append("cluster_enrollment", {"state": "roles"})
    raw = journal.path.read_bytes()
    journal.path.write_bytes(raw[: raw.rfind(b"\n", 0, len(raw) - 1) + 1])
    with pytest.raises(pg_journal.JournalError) as refused:
        journal.read()
    assert refused.value.code == "journal_head"


def test_a_missing_head_anchor_with_records_is_refused(tmp_path):
    journal = _journal(tmp_path)
    journal.append("cluster_enrollment", {"state": "intent"})
    journal.head_path.unlink()
    with pytest.raises(pg_journal.JournalError) as refused:
        journal.read()
    assert refused.value.code == "journal_head"


def test_a_non_canonical_or_unsequenced_line_is_refused(tmp_path):
    journal = _journal(tmp_path)
    journal.prepare()
    journal.path.write_text(
        '{"at": "2026-11-17T00:00:00Z", "kind": "cluster_enrollment", '
        '"payload": {}, "prev": "' + pg_journal.GENESIS + '", "seq": 0, "version": 1}\n',
        encoding="utf-8",
    )
    journal.head_path.write_text(
        '{"digest":"sha256:' + "0" * 64 + '","seq":0}', encoding="utf-8"
    )
    with pytest.raises(pg_journal.JournalError) as refused:
        journal.read()
    assert refused.value.code in ("journal_not_canonical", "journal_corrupt", "journal_chain")


def test_an_unknown_record_kind_is_refused(tmp_path):
    journal = _journal(tmp_path)
    journal.append("cluster_enrollment", {"state": "intent"})
    journal.append("reconcile", {"observed_state": "marker"})
    with pytest.raises(pg_journal.JournalError) as refused:
        journal.append("not_a_kind", {})
    assert refused.value.code == "journal_kind"


def test_append_if_head_refuses_when_the_head_changed(tmp_path):
    journal = _journal(tmp_path)
    first = journal.append("cluster_enrollment", {"state": "intent"})
    view = journal.read()
    # Another writer appends after the decision observed `view`.
    journal.append("cluster_enrollment", {"state": "roles"})
    assert (
        journal.append_if_head(
            view.head, view.head_seq, "cluster_enrollment", {"state": "intent"}
        )
        is None
    )
    # The conditional append is a compare-and-append: the changed-head caller wrote
    # nothing, so there are exactly the two records that already existed.
    records = journal.read().records
    assert [record.payload["state"] for record in records] == ["intent", "roles"]
    assert records[-1].prev == first.digest


def test_the_operation_lock_is_separate_from_the_journal_append_lock(tmp_path):
    journal = _journal(tmp_path)
    with journal.operation_lock():
        # Journal appends must remain possible while the host operation interval is open.
        record = journal.append("cluster_enrollment", {"state": "intent"})
        assert record.seq == 0


def test_a_second_operation_holder_is_refused(tmp_path):
    journal = _journal(tmp_path)
    journal.prepare()
    held = journal.operation_lock()
    held.__enter__()
    try:
        with pytest.raises(pg_journal.JournalError) as refused:
            with journal.operation_lock():
                pass
        assert refused.value.code == "operation_busy"
    finally:
        held.__exit__(None, None, None)


def test_a_second_writer_is_refused_while_the_lock_is_held(tmp_path, monkeypatch):
    journal = _journal(tmp_path)
    journal.prepare()
    monkeypatch.setattr(pg_journal, "_try_lock", lambda fd, exclusive: False)
    with pytest.raises(pg_journal.JournalError) as refused:
        journal.append("cluster_enrollment", {"state": "intent"})
    assert refused.value.code == "journal_busy"


def test_the_marker_is_a_strict_non_secret_identity_record():
    marker = pg_journal.make_marker("0123456789abcdef0123456789abcdef", "726485930221")
    token, identifier = pg_journal.parse_marker(marker)
    assert token == "0123456789abcdef0123456789abcdef"
    assert identifier == "726485930221"
    assert "'" not in marker and "password" not in marker.lower()
    assert pg_journal.marker_literal(marker) == "'" + marker + "'"
    for bad in (
        "",
        "not json",
        json.dumps({"format": "other", "marker": token, "system_identifier": identifier}),
        json.dumps({"format": pg_journal.MARKER_FORMAT, "marker": "xyz", "system_identifier": identifier}),
        json.dumps({"format": pg_journal.MARKER_FORMAT, "marker": token, "system_identifier": identifier, "x": 1}),
    ):
        with pytest.raises(pg_journal.JournalError):
            pg_journal.parse_marker(bad)


def _authorization(**overrides):
    cluster = {
        "container_name": "house-helper-db",
        "container_id": "a" * 64,
        "image_digest": "postgres@sha256:" + "b" * 64,
        "volume": "house-helper-pgdata",
        "system_identifier": "726485930221",
    }
    value = {
        "format": pg_journal.AUTHORIZATION_FORMAT,
        "role": "migrate",
        "mode": "B",
        "verdict": "verified",
        "run_id": "c" * 32,
        "issued_at": pg_journal.timestamp(datetime.now(timezone.utc)),
        "not_after": pg_journal.timestamp(datetime.now(timezone.utc) + timedelta(minutes=5)),
        "journal": {"seq": 6, "head": "sha256:" + "d" * 64},
        "cluster": cluster,
        "marker": pg_journal.make_marker("e" * 32, "726485930221"),
        "database": "house_helper",
        "project_roles": list(pg_artifacts.PROJECT_ROLE_NAMES),
        "databases": ["house_helper", "postgres", "template0", "template1"],
        "predefined_roles": ["pg_monitor"],
        "artifacts": dict(pg_artifacts.manifest()),
    }
    value.update(overrides)
    return value


def test_a_valid_authorization_round_trips_without_secrets():
    raw = pg_journal.encode_authorization(_authorization())
    value = pg_journal.decode_authorization(raw)
    assert value["role"] == "migrate" and value["verdict"] == "verified"
    assert b"password" not in raw.lower()


def test_an_authorization_with_an_extra_field_is_refused():
    value = _authorization()
    value["password"] = "sentinel-never-print"
    raw = pg_journal.encode_authorization(value)
    with pytest.raises(pg_journal.JournalError) as refused:
        pg_journal.decode_authorization(raw)
    assert "sentinel" not in str(refused.value)


def test_an_expired_or_future_authorization_is_refused():
    raw = pg_journal.encode_authorization(
        _authorization(
            issued_at=pg_journal.timestamp(datetime.now(timezone.utc) - timedelta(hours=1)),
            not_after=pg_journal.timestamp(datetime.now(timezone.utc) - timedelta(minutes=30)),
        )
    )
    with pytest.raises(pg_journal.JournalError) as expired:
        pg_journal.decode_authorization(raw)
    assert expired.value.code == "authorization_expired"

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    raw = pg_journal.encode_authorization(
        _authorization(
            issued_at=pg_journal.timestamp(future),
            not_after=pg_journal.timestamp(future + timedelta(minutes=5)),
        )
    )
    with pytest.raises(pg_journal.JournalError) as early:
        pg_journal.decode_authorization(raw)
    assert early.value.code == "authorization_clock"


def test_an_authorization_that_is_not_mode_b_is_refused():
    for key, value in (("verdict", "pending"), ("mode", "A"), ("role", "hh_migrate")):
        raw = pg_journal.encode_authorization(_authorization(**{key: value}))
        with pytest.raises(pg_journal.JournalError):
            pg_journal.decode_authorization(raw)


def test_a_non_pinned_image_or_malformed_identity_is_refused():
    for field, value in (
        ("image_digest", "postgres:16"),
        ("container_id", "short"),
        ("system_identifier", "not-a-number"),
    ):
        authorization = _authorization()
        authorization["cluster"] = dict(authorization["cluster"])
        authorization["cluster"][field] = value
        with pytest.raises(pg_journal.JournalError):
            pg_journal.decode_authorization(
                pg_journal.encode_authorization(authorization)
            )


def test_an_oversized_or_empty_authorization_is_refused():
    with pytest.raises(pg_journal.JournalError):
        pg_journal.decode_authorization(b"")
    with pytest.raises(pg_journal.JournalError) as refused:
        pg_journal.decode_authorization(b"x" * (pg_journal.AUTHORIZATION_MAX_BYTES + 1))
    assert refused.value.code == "authorization_size"


def _schema_authorization(**overrides):
    cluster = {
        "container_name": "house-helper-db",
        "container_id": "a" * 64,
        "image_digest": "postgres@sha256:" + "b" * 64,
        "volume": "house-helper-pgdata",
        "system_identifier": "726485930221",
    }
    value = pg_journal.build_schema_authorization(
        cluster=cluster,
        database="house_helper",
        artifacts=dict(pg_artifacts.manifest()),
        run_id="e" * 32,
        issued=datetime.now(timezone.utc),
    )
    value.update(overrides)
    return value


def test_the_schema_record_is_bounded_and_names_only_the_schema_step():
    raw = pg_journal.encode_schema_authorization(_schema_authorization())
    value = pg_journal.decode_schema_authorization(raw)
    assert value["role"] == "schema"
    assert set(value) == pg_journal.SCHEMA_AUTHORIZATION_KEYS
    assert b"password" not in raw.lower()
    # No migration, marker, inventory or SQL text can ride on this record.
    for key in ("marker", "mode", "project_roles", "databases", "sql"):
        with pytest.raises(pg_journal.JournalError):
            pg_journal.decode_schema_authorization(
                pg_journal.encode_schema_authorization(
                    _schema_authorization(**{key: "x"})
                )
            )


def test_the_two_authorization_kinds_are_not_interchangeable():
    schema_raw = pg_journal.encode_schema_authorization(_schema_authorization())
    with pytest.raises(pg_journal.JournalError):
        pg_journal.decode_authorization(schema_raw)
    migrate_raw = pg_journal.encode_authorization(_authorization())
    with pytest.raises(pg_journal.JournalError):
        pg_journal.decode_schema_authorization(migrate_raw)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are the deploy target's contract")
def test_a_group_readable_journal_directory_is_refused(tmp_path):
    journal = _journal(tmp_path)
    journal.prepare()
    journal.directory.chmod(0o750)
    with pytest.raises(pg_journal.JournalError) as refused:
        journal.read()
    assert refused.value.code == "journal_permissions"
