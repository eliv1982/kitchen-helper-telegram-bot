"""The off-host backup under the conditions that go wrong on a real host, beyond the tests of
test_deploy_offsite.py:

  * the rclone and the remote must be able to do what the design assumes, or nothing is done: an rclone
    without the Yandex application-folder option, a config that is not an application-folder remote of
    the owner's own OAuth application, and a remote that cannot rename server-side all REFUSE, and none of
    them falls back to another access mode;
  * a HARD kill (SIGKILL, power) runs no handler and no clean-up. What it leaves is exactly what the
    runbook says it leaves, none of it is a recovery point, and the next run puts it right (a kill between
    the rename and the commit record leaves an UNRESOLVED object, which the next run reports and leaves alone);
  * a retention that cannot finish is reported by every run in which it cannot, never only logged;
  * the recovery point is usable: decrypted, unpacked and restored with the shipped tools it is the
    database that was backed up, minus the deleted rows.
"""

import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from tests.deploy_support import DEPLOY, REPO_ROOT, STAGING_ENV, URL_ENV, dbtool, run as run_dbtool, sha256
from tests.offsite_support import (
    DELETED_MARKER,
    FIXED_NAME,
    KEPT_MARKER,
    RCLONE_CONF,
    REMOTE,
    Rig,
    database_health,
    fake_decrypt,
    offsite,
    point_name,
    row_counts,
)
from tests.test_deploy_offsite import seed, stamps


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def refused(rig, *, words, writes_nothing=True):
    code = rig.run()
    assert code == offsite.EXIT_REFUSED, rig.text
    assert words in rig.text
    assert rig.fake.events == []  # no backup, no container, no age
    if writes_nothing:
        assert rig.fake.rclone_writes() == []
    assert rig.work_leftovers() == []


# --- fail closed: the rclone and the remote ----------------------------------------------------------------


def test_an_rclone_without_the_yandex_app_folder_option_is_refused_and_nothing_is_done(rig):
    rig.fake.app_folder_support = False  # stable 1.75.2: its Yandex backend has no such option
    refused(rig, words="no app_folder option")
    assert "1.76.0" in rig.text and rig.fake.rclone_commands() == ["config"]  # it asked, was told, and stopped


@pytest.mark.parametrize("fault", [("providers", ("exit", 1)), ("providers", "garbage")], ids=["listing fails", "listing is not json"])
def test_an_rclone_whose_capabilities_cannot_be_read_is_refused_not_assumed_good(rig, fault):
    rig.fake.fail(*fault)
    refused(rig, words="cannot be checked")


def test_list_applies_the_same_checks(rig):
    rig.fake.app_folder_support = False
    code = offsite.main(["list", "--rclone-config", str(rig.config), "--remote", REMOTE], runner=rig.fake, log=rig.log.append)
    assert code == offsite.EXIT_REFUSED and "no app_folder option" in rig.text
    assert rig.fake.rclone_commands() == ["config"]


def conf(**changes):
    values = {"type": "yandex", "client_id": "own-id", "client_secret": "own-secret", "token": '{"access_token":"t"}', "app_folder": "true"}
    values.update(changes)
    return "[fake]\n" + "".join(f"{k} = {v}\n" for k, v in values.items() if v is not None)


@pytest.mark.parametrize(
    "text, words",
    [
        (conf(app_folder=None), "app_folder = true"),                     # full-disk mode: never
        (conf(app_folder="false"), "app_folder = true"),
        (conf(app_folder=""), "app_folder = true"),
        (conf(client_id=""), "client_id and client_secret of its own"),   # rclone's built-in application is wider
        (conf(client_id=None), "client_id and client_secret of its own"),
        (conf(client_secret=None), "client_id and client_secret of its own"),
        (conf(token=None), "has no token"),
        (conf(type="drive"), "not a Yandex Disk remote"),
        (conf(type="crypt"), "not a Yandex Disk remote"),
        (conf(type=None), "not a Yandex Disk remote"),
        (conf() + "app_folder = false\n", "cannot be read"),               # a setting given twice is ambiguous: refused
        ("[other]\ntype = yandex\napp_folder = true\n", "no [fake] section"),
        ("this is not an rclone config", "cannot be read"),
        ("", "no [fake] section"),
    ],
    ids=["no app_folder", "app_folder false", "app_folder empty", "empty client_id", "no client_id", "no client_secret",
         "no token", "type drive", "type crypt", "no type", "repeated setting", "other section", "not a config", "empty file"],
)
def test_a_config_that_is_not_an_application_folder_remote_of_the_owners_own_application_is_refused(rig, text, words):
    rig.config.write_text(text)
    if os.name != "nt":
        rig.config.chmod(0o600)
    code = rig.run()
    assert code == offsite.EXIT_REFUSED and words in rig.text, rig.text
    assert rig.fake.calls == []  # refused before any command ran, not even rclone
    assert "own-secret" not in rig.text and "access_token" not in rig.text  # no value of the file is ever printed


def test_the_setting_is_accepted_in_any_letter_case_and_the_good_config_runs(rig):
    rig.config.write_text(conf(app_folder="True"))
    if os.name != "nt":
        rig.config.chmod(0o600)
    assert rig.run() == 0, rig.text


def test_a_remote_that_cannot_rename_on_the_server_is_refused_because_the_commit_would_not_be_atomic(rig):
    rig.fake.can_move = False
    refused(rig, words="cannot rename server-side")


def test_a_failure_to_ask_the_remote_about_its_features_stops_the_run_before_the_backup(rig):
    rig.fake.fail("features", ("exit", 1))
    assert rig.run() == offsite.EXIT_FAILED
    assert rig.fake.events == [] and rig.fake.rclone_writes() == []


def test_the_environment_cannot_switch_rclone_to_another_mode(rig, monkeypatch):
    # RCLONE_* variables (RCLONE_YANDEX_APP_FOLDER=false, RCLONE_CONFIG_...) are not passed to rclone at all.
    monkeypatch.setenv("RCLONE_YANDEX_APP_FOLDER", "false")
    monkeypatch.setenv("RCLONE_CONFIG", "/elsewhere.conf")
    done = offsite.Runner().run([sys.executable, "-c", "import os; print(sorted(k for k in os.environ if k.startswith('RCLONE')))"], timeout=60, capture=True)
    assert done.stdout.decode().strip() == "[]"


# --- a hard kill ------------------------------------------------------------------------------------------


def start_child(rig, stage):
    ready = rig.root / f"ready-{stage}"
    child = subprocess.Popen(
        [sys.executable, "-m", "tests.offsite_kill_child", str(rig.root), stage, str(ready)],
        cwd=str(REPO_ROOT), env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 120
    while not ready.exists():
        if child.poll() is not None:
            raise AssertionError(f"the child ended before blocking at {stage}: {child.communicate()[1].decode()[-800:]}")
        assert time.monotonic() < deadline, f"the child never reached {stage}"
        time.sleep(0.05)
    return child


def final_names(rig):
    return {n for n in rig.fake.remote_files() if offsite.parse_point_name(n)}


KILL_STAGES = ["backup", "sanitize", "age", "upload", "download", "move"]


@pytest.mark.parametrize("stage", KILL_STAGES)
def test_after_a_hard_kill_nothing_looks_like_a_recovery_point_and_the_next_run_puts_everything_right(rig, stage):
    before = seed(rig, stamps(datetime(2026, 10, 20), 4))
    child = start_child(rig, stage)
    child.kill()  # SIGKILL: no handler, no finally, no clean-up of any kind
    child.communicate()

    # What a hard kill leaves, and the runbook says so: the clean-up did not run ...
    leftovers = [n for n in rig.work_leftovers() if n.startswith(offsite.RUN_PREFIX)]
    assert len(leftovers) == 1, rig.work_leftovers()  # ... the private directory (and the plaintext in it) is still there
    # ... but no object that could be taken for a recovery point exists: the final name is only given by the
    # step before the last, and a point is an object WITH its commit record.
    assert final_names(rig) == {n for n in before if offsite.parse_point_name(n)}
    assert rig.committed_names() == final_names(rig)
    partials = {n for n in rig.fake.remote_files() if n.endswith(".partial")}
    assert partials == ({FIXED_NAME + ".partial"} if stage in ("upload", "download", "move") else set())
    assert {n: rig.fake.remote_files()[n] for n in before} == before  # and no older point was touched

    # The next run (a new random name; the lock a dead process held is gone with it) recovers by itself.
    rig.token = "feedface"
    assert rig.run() == 0, rig.text
    assert "removed the leftover work directory" in rig.text
    assert [n for n in rig.work_leftovers() if n.startswith(offsite.RUN_PREFIX)] == []
    after = rig.fake.remote_files()
    assert not any(n.endswith(".partial") for n in after)
    new = point_name("20261025T041507Z", "feedface")
    assert set(after) == set(before) | {new, new + offsite.COMMIT_SUFFIX}
    assert {n: after[n] for n in before} == before


def test_a_hard_kill_between_the_rename_and_the_commit_record_leaves_an_unresolved_object_the_next_run_reports_and_leaves_alone(rig):
    before = seed(rig, stamps(datetime(2026, 10, 20), 4))
    child = start_child(rig, "record")
    child.kill()
    child.communicate()

    # The object has its final name, was read back and listed, but no record was written: it is not a point.
    files = rig.fake.remote_files()
    assert FIXED_NAME in files and FIXED_NAME + offsite.COMMIT_SUFFIX not in files
    folder = rig.classify()
    assert folder.unresolved == {FIXED_NAME: offsite.NO_RECORD}
    assert FIXED_NAME not in rig.committed_names() and rig.committed_names() == {n for n in before if offsite.parse_point_name(n)}
    unresolved_bytes = files[FIXED_NAME]

    # The next run makes its own point, reports the candidate (exit 6) and does not touch it.
    rig.token = "feedface"
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert f"UNRESOLVED {FIXED_NAME}: {offsite.NO_RECORD}" in rig.text and "IS stored and verified" in rig.text
    after = rig.fake.remote_files()
    assert after[FIXED_NAME] == unresolved_bytes and FIXED_NAME + offsite.COMMIT_SUFFIX not in after
    assert point_name("20261025T041507Z", "feedface") in rig.committed_names()
    assert FIXED_NAME not in rig.committed_names()
    # ... and so does every run after it: the report persists until an operator acts.
    rig.token = "cafe0001"
    rig.now = rig.now.replace(day=26)
    assert rig.run() == offsite.EXIT_RETENTION
    assert rig.fake.remote_files()[FIXED_NAME] == unresolved_bytes


def test_a_hard_kill_during_the_upload_leaves_a_partial_that_is_cut_short_and_never_counted(rig):
    seed(rig, stamps(datetime(2026, 10, 20), 4))
    child = start_child(rig, "upload")
    child.kill()
    child.communicate()
    partial = rig.fake.folder() / (FIXED_NAME + ".partial")
    assert partial.stat().st_size == 10  # a transfer that stopped: a fragment
    listing = offsite.parse_listing(rig.fake._rclone_lsjson(["fake:house-helper"], ["--files-only", "--hash"]).stdout)
    assert FIXED_NAME + ".partial" in {e.name for e in listing}
    folder = rig.classify()
    assert FIXED_NAME + ".partial" not in {p.name for p in folder.committed} | set(folder.unresolved)   # retention never sees it
    assert offsite.parse_point_name(FIXED_NAME + ".partial") is None                           # nor does any reader of the names


# --- an inability to prune is reported by every run that has it --------------------------------------------


def test_a_retention_that_cannot_delete_fails_every_run_in_which_it_cannot(rig):
    seed(rig, stamps(datetime(2026, 10, 13), 12))
    rig.fake.fail("delete", ("exit", 1), 0)  # every delete fails, on every run
    codes = []
    for day in range(3):
        rig.now = datetime(2026, 10, 25 + day, 4, 15, 7, tzinfo=rig.now.tzinfo)
        rig.token = f"0000000{day}"
        rig.log.clear()
        codes.append(rig.run())
        # The first run fails on the delete itself; once the backlog is over the cap the guard refuses instead
        # (it deletes nothing), and says so. Either way the run FAILS and the stored point is not in doubt.
        assert "IS stored and verified" in rig.text
        assert "rclone deletefile failed" in rig.text or "more than --max-deletions" in rig.text
    assert codes == [offsite.EXIT_RETENTION] * 3  # never 0: the failure is not absorbed
    assert len(rig.fake.point_objects()) == 12 + 3  # and the folder visibly keeps growing, run after run


def test_leftover_partials_beyond_the_limit_are_reported_not_forgotten(rig):
    for index in range(7):
        rig.fake.put_remote(point_name("20261020T041500Z", f"{index:08x}") + ".partial", b"half")
    assert rig.run() == offsite.EXIT_RETENTION
    assert "2 more leftover partial uploads were not removed" in rig.text
    left = [n for n in rig.fake.remote_files() if n.endswith(".partial")]
    assert len(left) == 2
    rig.log.clear()
    rig.token = "feedface"
    assert rig.run() == 0, rig.text  # the next run finishes the job and is clean again
    assert not [n for n in rig.fake.remote_files() if n.endswith(".partial")]


def test_an_unexpected_error_during_retention_is_a_retention_failure_not_a_failed_backup(rig, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("a bug, with a /secret/path in its text")

    monkeypatch.setattr(offsite, "plan_retention", broken)
    assert rig.run() == offsite.EXIT_RETENTION
    assert "unexpected RuntimeError" in rig.text and "/secret/path" not in rig.text and "IS stored and verified" in rig.text
    assert FIXED_NAME in rig.fake.remote_files()


# --- the recovery point restores ---------------------------------------------------------------------------


def test_a_recovery_point_decrypts_unpacks_and_restores_with_the_shipped_tools(rig, tmp_path, capsys):
    assert rig.run() == 0, rig.text

    # The owner's machine: decrypt (the fake age), check and unpack.
    blob = rig.fake.remote_files()[FIXED_NAME]
    recovery = tmp_path / "recovery.tar"
    recovery.write_bytes(fake_decrypt(blob))
    recovered = tmp_path / "recovered"
    recovered.mkdir()
    assert offsite.main(["unpack", str(recovery), "--output-dir", str(recovered)], log=rig.log.append) == 0
    assert sorted(p.name for p in recovered.iterdir()) == ["MANIFEST.json", "snapshot.db", "snapshot.db.sha256"]

    # RUNBOOK section 10's exact check: the checksum file is the one line for that very file name.
    snapshot, line = recovered / "snapshot.db", (recovered / "snapshot.db.sha256").read_bytes()
    assert line == f"{hashlib.sha256(snapshot.read_bytes()).hexdigest()}  snapshot.db\n".encode()

    # The VPS: the shipped verification, then the shipped restore into an empty data directory.
    staging = tmp_path / "staging"
    staging.mkdir()
    verified = run_dbtool(capsys, ["verify", "--input", str(snapshot), "--require-head"], {STAGING_ENV: str(staging)})
    assert verified.code == 0, verified.err
    data = tmp_path / "restored-data"
    data.mkdir()
    env = {URL_ENV: f"sqlite:///{data / 'house_helper.db'}", STAGING_ENV: str(staging)}
    restored = run_dbtool(capsys, ["restore", "--input", str(snapshot)], env)
    assert restored.code == 0, restored.err
    assert run_dbtool(capsys, ["check-schema"], env).code == 0  # the schema is at the head of this image

    database = data / "house_helper.db"
    assert database_health(database)[0] == "ok"
    assert row_counts(database) == row_counts(rig.database)  # every live row, and only those
    content = database.read_bytes()
    assert KEPT_MARKER.encode() in content and DELETED_MARKER.encode() not in content
    manifest = offsite.read_recovery_tar(recovery)
    assert manifest["snapshot"]["sha256"] == sha256(snapshot) and manifest["image_tag"] == "abc123def456"


def test_a_damaged_encrypted_object_is_detected_by_the_tools_before_anything_is_restored(rig, tmp_path):
    assert rig.run() == 0
    blob = rig.fake.remote_files()[FIXED_NAME]
    damaged = bytearray(fake_decrypt(blob))
    damaged[len(damaged) // 3] ^= 1  # one flipped bit in the decrypted tar (age itself would refuse the file; this is the layer after it)
    recovery = tmp_path / "damaged.tar"
    recovery.write_bytes(bytes(damaged))
    out = tmp_path / "out"
    out.mkdir()
    assert offsite.main(["unpack", str(recovery), "--output-dir", str(out)], log=rig.log.append) == offsite.EXIT_FAILED
    assert list(out.iterdir()) == []
