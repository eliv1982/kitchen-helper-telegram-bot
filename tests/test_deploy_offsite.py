"""The off-host backup orchestrator (deploy/offsite_backup.py): what it does on a good day, and what it
refuses to do on every bad one.

The external commands are fakes (tests/offsite_support.py) but dbtool is REAL, on synthetic SQLite: the
rewrite that these tests inspect is the one that ships. The promises under test:

  * a run makes ONE recovery point (snapshot + checksum + manifest in a tar), encrypted for a public key,
    uploaded, read back, renamed to its final name and only then COMMITTED by a record that matches the
    object; a deleted row is not in it;
  * a run that fails anywhere before the commit deletes NOTHING on the remote (no older recovery point, and no .partial,
    not even its own: it cannot be told from a replaced one), and leaves no commit record; what it made stays, reported;
  * retention keeps 7 daily and 4 weekly COMMITTED points, runs only after a commit, deletes only names of
    the tool's own shape in the one configured folder, one object at a time, confirms every delete by
    listing the folder, spends --max-deletions on points and orphaned records only, and refuses a mass
    deletion;
  * temporary plaintext is removed on success, on failure and on a signal, and a failure to remove it is
    never a success (tests/test_deploy_offsite_protocol.py has the commit protocol, the unresolved objects,
    the clock and the budget).
"""

import hashlib
import io
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.deploy_support import DEPLOY, dbtool, posix_only, sha256


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

from tests.offsite_support import (
    DELETED_MARKER,
    FIXED_NAME,
    FIXED_NOW,
    FIXED_STAMP,
    FIXED_TOKEN,
    FOLDER,
    KEPT_MARKER,
    PRIVATE_KEY_SHAPED,
    PUBLIC_KEY,
    REMOTE,
    TAG,
    Rig,
    database_health,
    fake_decrypt,
    offsite,
    point_name,
    row_counts,
)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def stamps(start: datetime, days: int, hour_minute=(4, 15)):
    return [
        (start + timedelta(days=i)).replace(hour=hour_minute[0], minute=hour_minute[1], second=0).strftime(offsite.STAMP_FORMAT)
        for i in range(days)
    ]


def seed(rig, stamp_list):
    """Committed points (object + valid commit record) for the given times; returns the whole folder."""
    for stamp in stamp_list:
        rig.fake.put_point(point_name(stamp), f"point {stamp}".encode())
    return rig.fake.remote_files()


def points(*names):
    return [offsite.parse_point_name(n) for n in names]


# --- retention: the pure plan ----------------------------------------------------------------------------


def kept_dates(plan):
    return sorted(re.search(r"house-helper-(\d{8})T", name).group(1) for name in plan.keep)


def test_sixty_daily_points_keep_the_last_seven_days_and_the_four_sundays_before_them():
    # 2026-10-25 is a Sunday: the last seven days are Mon 19 .. Sun 25 (all of ISO week 43); the newest
    # point of each earlier week is its Sunday.
    series = [offsite.parse_point_name(point_name(s)) for s in stamps(datetime(2026, 8, 27), 60)]
    plan = offsite.plan_retention(series)
    assert kept_dates(plan) == [
        "20260927", "20261004", "20261011", "20261018",
        "20261019", "20261020", "20261021", "20261022", "20261023", "20261024", "20261025",
    ]
    assert len(plan.drop) == 60 - 11
    assert [p.moment for p in plan.drop] == sorted(p.moment for p in plan.drop)  # oldest first


def test_in_mid_week_the_weekly_points_reach_one_week_further_back():
    # Wed 2026-10-21: seven days are Thu 15 .. Wed 21; last Sunday (18) is among them, so it is not
    # counted again as a weekly point and the four weekly points are Oct 11, Oct 4, Sep 27, Sep 20.
    series = [offsite.parse_point_name(point_name(s)) for s in stamps(datetime(2026, 8, 27), 56)]  # to Oct 21
    plan = offsite.plan_retention(series)
    assert kept_dates(plan) == [
        "20260920", "20260927", "20261004", "20261011",
        "20261015", "20261016", "20261017", "20261018", "20261019", "20261020", "20261021",
    ]


def test_only_the_newest_point_of_a_day_is_kept_and_two_runs_a_day_do_not_cost_a_day():
    names = [point_name(f"20261025T{h:02d}1500Z", f"{h:08x}") for h in (3, 9, 15)] + [point_name("20261024T031500Z")]
    plan = offsite.plan_retention(points(*names))
    assert sorted(plan.keep) == sorted([names[2], names[3]])
    assert {p.name for p in plan.drop} == {names[0], names[1]}


def test_few_points_are_all_kept_and_an_empty_folder_has_nothing_to_do():
    few = points(*(point_name(s) for s in stamps(datetime(2026, 10, 20), 3)))
    assert offsite.plan_retention(few).drop == []
    assert offsite.plan_retention([]).keep == {} and offsite.plan_retention([]).drop == []


def test_a_week_that_spans_the_new_year_is_one_week():
    # 2026-12-31 (Thu) and 2027-01-03 (Sun) are both ISO week 53 of 2026; 2027-01-04 starts week 1.
    names = [point_name(s) for s in ("20261231T041500Z", "20270103T041500Z", "20270104T041500Z")]
    plan = offsite.plan_retention(points(*names), daily=1, weekly=4)
    assert plan.keep[names[2]] == "daily"          # the newest day
    assert plan.keep[names[1]] == "weekly"         # the newest point of week 53
    assert names[0] in {p.name for p in plan.drop}  # the same week's older point is not a second weekly point


def test_the_protected_point_survives_even_if_a_wrong_clock_made_it_look_old():
    names = [point_name(s) for s in stamps(datetime(2030, 1, 1), 40)]
    old = point_name("20200101T000000Z", "deadbeef")
    plan = offsite.plan_retention(points(old, *names), protect={old})
    assert plan.keep[old] == "protected" and old not in {p.name for p in plan.drop}
    assert offsite.plan_retention(points(old, *names)).keep.get(old) is None  # without protection it would go


def test_the_plan_does_not_depend_on_the_clock_a_future_point_costs_one_slot_and_deletes_nothing_recent():
    names = [point_name(s) for s in stamps(datetime(2026, 10, 1), 25)]
    future = point_name("20990101T000000Z", "feedface")
    with_future = offsite.plan_retention(points(future, *names))
    assert future in with_future.keep
    newest_real = names[-1]
    assert newest_real in with_future.keep  # the most recent real points are still kept


@pytest.mark.parametrize("seed_value", range(25))
def test_properties_of_the_plan_on_random_histories(seed_value):
    generator = random.Random(seed_value)
    moments = sorted(
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=generator.randrange(0, 60 * 24 * 200))
        for _ in range(generator.randrange(0, 80))
    )
    found = [offsite.parse_point_name(point_name(m.strftime(offsite.STAMP_FORMAT), f"{i:08x}")) for i, m in enumerate(moments)]
    plan = offsite.plan_retention(found)
    names = {p.name for p in found}
    assert set(plan.keep) <= names and {p.name for p in plan.drop} == names - set(plan.keep)
    assert len(plan.keep) <= offsite.KEEP_DAILY + offsite.KEEP_WEEKLY
    if found:
        assert max(found, key=lambda p: (p.moment, p.name)).name in plan.keep  # the newest point is never dropped
        assert plan.keep, "something is always kept"
    kept = [p for p in found if p.name in plan.keep]
    daily = [p for p in kept if plan.keep[p.name] == "daily"]
    assert len({p.moment.date() for p in daily}) == len(daily) <= offsite.KEEP_DAILY
    # Applying the plan and planning again changes nothing.
    again = offsite.plan_retention(kept)
    assert again.drop == [] and set(again.keep) == set(plan.keep)


@pytest.mark.parametrize(
    "name, valid",
    [
        (FIXED_NAME, True),
        ("house-helper-20261025T041507Z-0a1b2c3d.tar.age.partial", False),
        ("house-helper-20261025T041507Z-0A1B2C3D.tar.age", False),
        ("house-helper-20261025T041507Z-0a1b2c3.tar.age", False),
        ("house-helper-20261340T041507Z-0a1b2c3d.tar.age", False),   # not a date
        ("house-helper-20261025T251507Z-0a1b2c3d.tar.age", False),   # not a time
        ("house-helper-20261025T041507Z-0a1b2c3d.tar.age ", False),
        ("../house-helper-20261025T041507Z-0a1b2c3d.tar.age", False),
        ("house-helper-20261025T041507Z-0a1b2c3d.tar.age\n", False),
        ("notes.txt", False),
    ],
)
def test_only_the_tools_own_final_names_are_recovery_points(name, valid):
    assert (offsite.parse_point_name(name) is not None) is valid


# --- configuration is checked before anything is done ----------------------------------------------------


def run_refused(rig, expected_text, argv=None):
    assert rig.run(argv=argv) == offsite.EXIT_REFUSED, rig.text
    assert expected_text in rig.text
    assert rig.fake.calls == []  # nothing was run: not the backup, not the container, not rclone
    assert rig.work_leftovers() == []


def test_a_recipients_file_with_a_private_key_is_refused_and_the_key_is_not_printed(rig):
    rig.recipients.write_text(f"{PUBLIC_KEY}\n{PRIVATE_KEY_SHAPED}\n")
    run_refused(rig, "PRIVATE key")
    assert PRIVATE_KEY_SHAPED not in rig.text and "QQQQ" not in rig.text


@pytest.mark.parametrize(
    "content",
    [
        "",
        "# only a comment\n",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample owner\n",
        "age1plugin1abc\n",
        PUBLIC_KEY + "x\n",
        PUBLIC_KEY.upper() + "\n",
        "\n".join([PUBLIC_KEY] * 9) + "\n",
        "AGE-PLUGIN-YUBIKEY-1ABC\n",
    ],
    ids=["empty", "comment only", "ssh key", "plugin recipient", "too long", "upper case", "nine keys", "plugin identity"],
)
def test_only_x25519_public_keys_are_accepted_as_recipients(rig, content):
    rig.recipients.write_text(content)
    run_refused(rig, "--recipients-file")


def test_several_recipients_and_comments_and_windows_line_ends_are_fine(rig):
    rig.recipients.write_bytes(f"# primary\r\n{PUBLIC_KEY}\r\n\r\n# spare\r\n{PUBLIC_KEY}\r\n".encode())
    assert rig.run() == 0, rig.text


@pytest.mark.parametrize(
    "remote",
    ["fake:", "fake", "/tmp/backups", "C:\\backups", ":local:/x", "fake:/abs", "fake:a/../b", "fake:a b", "fake:a//b",
     "fake:.hidden", "fake:a/b/c/d/e", "fa ke:x", "fake:x\n"],
)
def test_the_remote_must_be_a_folder_of_its_own_on_a_named_remote(rig, remote):
    argv = rig.args()
    argv[argv.index("--remote") + 1] = remote
    run_refused(rig, "--remote", argv)


def test_a_nested_folder_name_is_accepted():
    assert offsite.validate_remote("yandex-app:backups/house-helper") == "yandex-app:backups/house-helper"


@pytest.mark.parametrize("tool", ["bash", "docker", "age", "rclone"])
def test_a_missing_tool_is_refused_before_anything_runs(rig, tool):
    rig.fake.missing.add(tool)
    run_refused(rig, f"{tool} is not installed")


@posix_only
def test_an_rclone_config_that_others_can_read_is_refused(rig):
    rig.config.chmod(0o640)
    run_refused(rig, "--rclone-config")


@pytest.mark.parametrize("bad", ["", "-x", "a b", "a/b", "x" * 200])
def test_the_image_tag_must_look_like_one(rig, bad):
    run_refused(rig, "--tag", rig.args(tag=bad))


def test_the_work_directory_must_exist_and_be_absolute(rig):
    argv = rig.args()
    argv[argv.index("--work-dir") + 1] = "relative/work"
    run_refused(rig, "--work-dir", argv)
    argv[argv.index("--work-dir") + 1] = str(rig.root / "does-not-exist")
    rig.log.clear()
    run_refused(rig, "--work-dir", argv)


def test_the_arguments_are_all_required_and_a_usage_error_is_status_2(rig, capsys):
    assert offsite.main(["run", "--tag", TAG], runner=rig.fake, log=rig.log.append) == offsite.EXIT_USAGE
    assert offsite.main([], runner=rig.fake, log=rig.log.append) == offsite.EXIT_USAGE
    assert rig.fake.calls == []
    assert offsite.main(["--help"], runner=rig.fake, log=rig.log.append) == 0


def test_a_negative_deletion_limit_is_refused(rig):
    run_refused(rig, "--max-deletions", argv=rig.args("--max-deletions", "-1"))


# --- the recovery point: one tar, exactly three files ---------------------------------------------------


@pytest.fixture
def made_tar(tmp_path):
    snapshot = tmp_path / "snap.db"
    snapshot.write_bytes(b"SQLite format 3\x00" + b"x" * 5000)
    tar_path = tmp_path / "point.tar"
    digests = offsite.build_recovery_tar(tar_path, snapshot, FIXED_NOW, TAG)
    return snapshot, tar_path, digests


def test_the_tar_holds_a_manifest_the_one_line_checksum_file_and_the_snapshot(made_tar, tmp_path):
    snapshot, tar_path, digests = made_tar
    with tarfile.open(tar_path) as archive:
        assert [m.name for m in archive.getmembers()] == ["MANIFEST.json", "snapshot.db.sha256", "snapshot.db"]
        assert all(m.isreg() and m.uid == 0 and m.gid == 0 and m.mode == 0o600 for m in archive.getmembers())
        assert archive.extractfile("snapshot.db.sha256").read() == f"{digests.sha256}  snapshot.db\n".encode()
    manifest = offsite.read_recovery_tar(tar_path)
    assert manifest == {
        "format": "house-helper-recovery-point/1",
        "created_utc": FIXED_STAMP,
        "image_tag": TAG,
        "sanitized": "vacuum-into",
        "snapshot": {"name": "snapshot.db", "bytes": digests.size, "sha256": sha256(snapshot)},
    }


def test_the_tar_can_be_extracted_and_the_checksum_file_passes_the_runbooks_exact_check(made_tar, tmp_path):
    snapshot, tar_path, digests = made_tar
    out = tmp_path / "out"
    out.mkdir()
    offsite.read_recovery_tar(tar_path, extract_to=out)
    assert (out / "snapshot.db").read_bytes() == snapshot.read_bytes()
    assert (out / "snapshot.db.sha256").read_bytes() == f"{sha256(out / 'snapshot.db')}  snapshot.db\n".encode()


def rewrite_tar(source, destination, transform):
    with tarfile.open(source) as old, tarfile.open(destination, "w", format=tarfile.USTAR_FORMAT) as new:
        members = [(m, old.extractfile(m).read()) for m in old.getmembers()]
        for member, data in transform(members):
            member.size = len(data)
            new.addfile(member, io.BytesIO(data))


def flip(members):
    out = []
    for member, data in members:
        if member.name == "snapshot.db":
            data = data[:20] + bytes([data[20] ^ 1]) + data[21:]
        out.append((member, data))
    return out


def extra_member(members):
    extra = tarfile.TarInfo("extra.txt")
    return [*members, (extra, b"surprise")]


def traversal(members):
    out = []
    for member, data in members:
        if member.name == "snapshot.db":
            member.name = "../snapshot.db"
        out.append((member, data))
    return out


def wrong_checksum_line(members):
    return [(m, (b"0" * 64 + b"  snapshot.db\n") if m.name == "snapshot.db.sha256" else d) for m, d in members]


def manifest_with_extra_key(members):
    import json

    out = []
    for member, data in members:
        if member.name == "MANIFEST.json":
            document = json.loads(data)
            document["host"] = "eliv-server"
            data = json.dumps(document).encode()
        out.append((member, data))
    return out


def missing_snapshot(members):
    return [(m, d) for m, d in members if m.name != "snapshot.db"]


def link_instead_of_file(members):
    out = []
    for member, data in members:
        if member.name == "snapshot.db":
            member.type, data = tarfile.SYMTYPE, b""
            member.linkname = "/etc/passwd"
        out.append((member, data))
    return out


@pytest.mark.parametrize(
    "transform",
    [flip, extra_member, traversal, wrong_checksum_line, manifest_with_extra_key, missing_snapshot, link_instead_of_file],
    ids=["flipped bit", "extra member", "path traversal", "wrong checksum line", "extra manifest key", "no snapshot", "symlink member"],
)
def test_a_tar_that_is_not_exactly_what_the_tool_writes_is_rejected_and_extracts_nothing(made_tar, tmp_path, transform):
    _snapshot, tar_path, _ = made_tar
    bad = tmp_path / "bad.tar"
    rewrite_tar(tar_path, bad, transform)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(offsite.Failed):
        offsite.read_recovery_tar(bad, extract_to=out)
    code = offsite.main(["unpack", str(bad), "--output-dir", str(out)], log=lambda m: None)
    assert code == offsite.EXIT_FAILED
    assert list(out.iterdir()) == []  # nothing is left that could be taken for a backup


def test_unpack_extracts_a_good_point_and_never_replaces_a_file(made_tar, tmp_path):
    _snapshot, tar_path, digests = made_tar
    out = tmp_path / "restore"
    out.mkdir()
    messages = []
    assert offsite.main(["unpack", str(tar_path), "--output-dir", str(out)], log=messages.append) == 0
    assert sorted(p.name for p in out.iterdir()) == ["MANIFEST.json", "snapshot.db", "snapshot.db.sha256"]
    assert digests.sha256 in " ".join(messages)
    before = (out / "snapshot.db").read_bytes()
    assert offsite.main(["unpack", str(tar_path), "--output-dir", str(out)], log=lambda m: None) == offsite.EXIT_REFUSED
    assert (out / "snapshot.db").read_bytes() == before


def test_a_file_that_is_not_a_tar_is_rejected(tmp_path):
    junk = tmp_path / "junk.tar"
    junk.write_bytes(b"not a tar" * 1000)
    with pytest.raises(offsite.Failed):
        offsite.read_recovery_tar(junk)


# --- a good run -----------------------------------------------------------------------------------------


def test_a_run_stores_one_verified_encrypted_recovery_point_without_the_deleted_rows(rig, tmp_path):
    live_before = sha256(rig.database)
    assert rig.run() == 0, rig.text

    files = rig.fake.remote_files()
    assert sorted(files) == [FIXED_NAME, FIXED_NAME + ".commit"]  # one point: the object and its record; no .partial left behind
    assert rig.committed_names() == {FIXED_NAME}
    blob = files[FIXED_NAME]
    record = offsite.read_commit_record(files[FIXED_NAME + ".commit"], FIXED_NAME)
    assert record == offsite.Digests(len(blob), sha256_bytes(blob), hashlib.sha1(blob).hexdigest(), hashlib.md5(blob).hexdigest())
    assert blob.startswith(offsite.AGE_HEADER)
    assert b"SQLite format 3" not in blob and KEPT_MARKER.encode() not in blob  # the plaintext never reaches the remote

    snapshot = rig.decrypted_snapshot(FIXED_NAME, tmp_path / "dec")
    data = snapshot.read_bytes()
    assert DELETED_MARKER.encode() not in data  # the rewrite kept the deleted rows out
    assert data.count(KEPT_MARKER.encode()) >= 12
    assert row_counts(snapshot) == row_counts(rig.database)
    assert database_health(snapshot) == ("ok", 0)
    manifest = offsite.read_recovery_tar(tmp_path / "dec" / "recovery.tar")
    assert manifest["image_tag"] == TAG and manifest["created_utc"] == FIXED_STAMP

    assert sha256(rig.database) == live_before  # the live database was not changed
    assert rig.work_leftovers() == []
    assert all(not p.exists() for p in rig.fake.plaintext_paths)
    assert "step 6/6" in rig.text and FIXED_NAME in rig.text


def test_a_tar_that_does_not_read_back_clean_is_never_encrypted(rig, monkeypatch):
    real = offsite.build_recovery_tar

    def damaging(destination, snapshot, created, tag):
        digests = real(destination, snapshot, created, tag)
        data = destination.read_bytes()
        middle = len(data) // 3  # inside the snapshot, not in a block of padding
        destination.write_bytes(data[:middle] + bytes([data[middle] ^ 1]) + data[middle + 1:])
        return digests

    monkeypatch.setattr(offsite, "build_recovery_tar", damaging)
    assert rig.run() == offsite.EXIT_FAILED
    assert rig.fake.events == ["backup", "sanitize", "verify"]  # no age, nothing written to the remote
    assert rig.fake.rclone_writes() == [] and "lsjson" not in rig.fake.rclone_commands() and rig.work_leftovers() == []


def test_the_steps_run_in_this_order_and_each_isolated_container_has_no_network_and_no_database(rig):
    assert rig.run() == 0, rig.text
    # ... the rename, then the commit record (written, read back), then retention reads the record of the point it just made
    assert rig.fake.events == ["backup", "sanitize", "verify", "age", "upload", "download", "move", "record", "cat", "cat"]
    sanitize, verify = rig.fake.docker_calls
    assert sanitize["args"] == ["sanitize", "--input", "-", "--output", "-"]
    assert verify["args"] == ["verify", "--input", "-", "--require-head"]
    for call in (sanitize, verify):
        assert call["image"] == f"house-helper:{TAG}"
        assert call["flags"] == list(offsite.DOCKER_ISOLATION[1:])
        assert call["env"] == {}  # no HOUSE_HELPER_DATABASE_URL, no volume of the data: the live database is out of reach
    flags = " ".join(sanitize["flags"])
    for needed in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--log-driver none", "-v /staging"):
        assert needed in flags


def test_the_isolation_flags_and_the_backup_name_rule_are_the_ones_backup_sh_uses():
    script = (DEPLOY / "backup.sh").read_text()
    verify_call = script[script.index("docker run --rm -i"):]
    verify_call = verify_call[: verify_call.index('"house-helper:$tag"')].replace("\\\n", " ")
    assert shlex.split(verify_call)[1:] == list(offsite.DOCKER_ISOLATION)
    pattern = re.search(r"NAME_PATTERN='(.*)'", script).group(1)
    assert pattern == "^" + offsite.BACKUP_NAME_RE.pattern.replace("\\.db", "\\.db") + "$"
    assert offsite.MAX_DATABASE_BYTES == dbtool.MAX_DATABASE_BYTES
    assert f"DBTOOL={offsite.DBTOOL}" in script


def test_the_backup_script_is_called_with_a_private_directory_and_a_label_never_a_default_or_a_name(rig):
    assert rig.run() == 0
    index = next(i for i, c in enumerate(rig.fake.calls) if Path(c[0]).name == "bash")
    command = rig.fake.calls[index]
    assert Path(command[1]).name == "backup.sh" and Path(command[1]).parent == DEPLOY
    options = dict(zip(command[2::2], command[3::2]))
    assert options["--tag"] == TAG and options["--label"] == "offsite"
    assert Path(options["--dir"]).parent.name.startswith(offsite.RUN_PREFIX)  # inside this run's private directory
    assert rig.fake.env_extras[index]["TMPDIR"] == str(Path(options["--dir"]).parent)


def test_age_gets_the_recipients_file_and_never_a_passphrase_or_an_identity(rig):
    assert rig.run() == 0
    age = next(c for c in rig.fake.calls if Path(c[0]).name == "age")
    assert age[1:] == ["-R", str(rig.recipients)]
    assert PUBLIC_KEY in rig.fake.age_recipients


def test_every_rclone_call_names_the_config_the_one_folder_and_only_allowed_commands(rig):
    rig.fake.put_point(point_name("20260101T000000Z"), b"old")  # something to delete, so every command is used
    for stamp in stamps(datetime(2026, 10, 15), 10):  # to the day before the run: a daily history
        rig.fake.put_point(point_name(stamp), b"x")
    assert rig.run() == 0, rig.text
    commands = rig.fake.rclone_commands()
    assert set(commands) <= {"config", "backend", "mkdir", "lsjson", "copyto", "moveto", "deletefile", "cat"}
    assert "deletefile" in commands
    for call in rig.fake.rclone_calls:
        assert call[call.index("--config") + 1] == str(rig.config)
    # the order of the writes: upload, read back, rename, commit record; retention strictly after the commit
    order = [c for c in commands if c in ("copyto", "moveto", "deletefile")]
    assert order[:4] == ["copyto", "copyto", "moveto", "copyto"] and set(order[4:]) == {"deletefile"}
    assert commands.index("deletefile") > commands.index("moveto")


def test_the_run_prints_names_sizes_and_checksums_but_no_content_key_or_credential(rig):
    assert rig.run() == 0
    text = rig.text
    for secret in (KEPT_MARKER, DELETED_MARKER, PUBLIC_KEY, "token", "type = fake", "synthetic"):
        assert secret not in text, secret
    assert str(rig.config) not in text  # not even the path of the file that holds the token


def test_plaintext_is_removed_as_soon_as_the_next_stage_has_what_it_needs(rig):
    seen = {}
    real_run = rig.fake.run

    def spy(argv, **kwargs):
        tool = Path(argv[0]).name
        step = None
        if tool == "docker":
            step = argv[argv.index("python") + 2]  # sanitize or verify
        elif tool == "age":
            step = "age"
        elif tool == "rclone" and argv[1] == "copyto":
            step = ("record" if argv[-1].endswith(offsite.COMMIT_SUFFIX) else "upload") if "--immutable" in argv else "download"
        if step:
            run_dir = next(p for p in rig.work.iterdir() if p.name.startswith(offsite.RUN_PREFIX))
            seen[step] = sorted(os.listdir(run_dir))
        return real_run(argv, **kwargs)

    rig.fake.run = spy
    assert rig.run() == 0, rig.text
    assert seen == {
        "sanitize": ["pair"],                           # the backup pair, nothing else yet
        "verify": ["sanitized.db"],                     # the unsanitized pair is gone before anything else reads the copy
        "age": ["recovery-point.tar"],                  # the sanitized copy is gone once it is packed
        "upload": ["recovery-point.tar.age"],           # the plaintext tar is gone once it is encrypted
        "download": ["recovery-point.tar.age"],
        "record": ["commit-record.json", "recovery-point.tar.age"],   # the record holds checksums of the ENCRYPTED object only
    }


# --- a failed run changes nothing that was not its own ----------------------------------------------------


NOTHING, PARTIAL, FINAL = "nothing", "its own .partial", "the unmatched final-named object"

FAILURES = [
    # id, (kind, action, nth), events that happened, what the failed run leaves on the remote besides the older points
    ("backup fails", ("backup", ("exit", 1), 1), ["backup"], NOTHING),
    ("backup pair is wrong", ("backup", "bad_pair", 1), ["backup"], NOTHING),
    ("sanitize fails", ("sanitize", ("exit", 4), 1), ["backup", "sanitize"], NOTHING),
    ("sanitize stream damaged", ("sanitize", "corrupt", 1), ["backup", "sanitize"], NOTHING),
    ("sanitize stream cut", ("sanitize", "truncate", 1), ["backup", "sanitize"], NOTHING),
    ("verify fails", ("verify", ("exit", 4), 1), ["backup", "sanitize", "verify"], NOTHING),
    ("age fails", ("age", ("exit", 1), 1), ["backup", "sanitize", "verify", "age"], NOTHING),
    ("age output is not age", ("age", "plain", 1), ["backup", "sanitize", "verify", "age"], NOTHING),
    ("mkdir fails", ("mkdir", ("exit", 1), 1), ["backup", "sanitize", "verify", "age"], NOTHING),
    ("first listing fails", ("lsjson", ("exit", 1), 1), ["backup", "sanitize", "verify", "age"], NOTHING),
    ("first listing is garbage", ("lsjson", "garbage", 1), ["backup", "sanitize", "verify", "age"], NOTHING),
    ("upload fails", ("upload", ("exit", 1), 1), ["backup", "sanitize", "verify", "age", "upload"], NOTHING),
    # "upload fails half way" is not here: an upload that did not return proves nothing about what carries the partial's
    # name, so the run leaves it alone (tests/test_deploy_offsite_ownership.py). Every failure below happens AFTER the upload
    # returned. The partial is then probably this run's own, but it is left where it is all the same: no run deletes a
    # .partial (a delete by name cannot be tied to the content, so it could remove an object that replaced it).
    ("upload damaged", ("upload", "corrupt", 1), ["backup", "sanitize", "verify", "age", "upload"], PARTIAL),
    ("upload cut short", ("upload", "truncate", 1), ["backup", "sanitize", "verify", "age", "upload"], PARTIAL),
    ("remote reports another size", ("lsjson", "lie_size", 2), ["backup", "sanitize", "verify", "age", "upload"], PARTIAL),
    ("remote reports another md5", ("lsjson", "lie_md5", 2), ["backup", "sanitize", "verify", "age", "upload"], PARTIAL),
    ("listing after upload fails", ("lsjson", ("exit", 1), 2), ["backup", "sanitize", "verify", "age", "upload"], PARTIAL),
    ("read-back fails", ("download", ("exit", 1), 1), ["backup", "sanitize", "verify", "age", "upload", "download"], PARTIAL),
    ("read-back differs", ("download", "corrupt", 1), ["backup", "sanitize", "verify", "age", "upload", "download"], PARTIAL),
    ("commit fails", ("move", ("exit", 1), 1), ["backup", "sanitize", "verify", "age", "upload", "download", "move"], PARTIAL),
    ("commit does nothing", ("move", "noop", 1), ["backup", "sanitize", "verify", "age", "upload", "download", "move"], PARTIAL),
    ("committed object is damaged", ("move", "corrupt", 1), ["backup", "sanitize", "verify", "age", "upload", "download", "move"], FINAL),
]
FAILURE_PARAMS = [pytest.param(f, e, left, id=name) for name, f, e, left in FAILURES]


def assert_what_a_failed_run_left(rig, before, left):
    """No older point is touched and NOTHING on the remote is deleted. What the run itself made stays where it is: its own
    .partial (an upload that returned), or the object under the final name that the listing after the rename did not match;
    neither has a commit record, so neither is a recovery point (the next run reports it)."""
    files = rig.fake.remote_files()
    assert {n: files[n] for n in before} == before
    extra = set(files) - set(before)
    assert extra == {NOTHING: set(), PARTIAL: {FIXED_NAME + ".partial"}, FINAL: {FIXED_NAME}}[left]
    assert [c for c in rig.fake.rclone_calls if c[0] == "deletefile"] == []
    if rig.fake.remote_hashes:  # a remote that lists no MD5 binds no point to its record
        assert rig.committed_names() == {n for n in before if offsite.parse_point_name(n)}


@pytest.mark.parametrize("fault, events, left", FAILURE_PARAMS)
def test_a_failed_run_deletes_nothing_leaves_no_older_point_touched_and_no_plaintext(rig, fault, events, left):
    before = seed(rig, stamps(datetime(2026, 10, 15), 6))
    kind, action, nth = fault
    rig.fake.fail(kind, action, nth)
    code = rig.run()
    assert code == offsite.EXIT_FAILED, rig.text
    assert_what_a_failed_run_left(rig, before, left)
    assert rig.work_leftovers() == []
    assert all(not p.exists() for p in rig.fake.plaintext_paths)
    assert rig.fake.events == events  # it stopped where the fault was; nothing later ran
    assert "FAILED" in rig.text and DELETED_MARKER not in rig.text
    if left == PARTIAL:
        assert f"{FIXED_NAME}.partial was uploaded by this run, which ended before the rename. It is LEFT in the folder" in rig.text


def test_a_remote_whose_backend_reports_no_checksum_is_refused_before_the_rename_not_accepted_on_size_and_the_read_back(rig):
    """The MD5 is what ties an object to its commit record in every later run, so a remote that does not list one is
    found out at the upload, while the object is still a partial: nothing is renamed and no record is written (the partial
    stays where it is, like every .partial)."""
    before = seed(rig, stamps(datetime(2026, 10, 15), 3))
    rig.fake.remote_hashes = False
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert "gives no MD5" in rig.text
    assert_what_a_failed_run_left(rig, before, PARTIAL)
    assert "download" not in rig.fake.events and "move" not in rig.fake.events
    assert rig.work_leftovers() == []


def test_a_remote_that_reports_a_checksum_has_it_compared(rig):
    assert rig.run() == 0, rig.text
    # a mismatch is the "remote reports another md5" case above; the read-back is a SHA-256 comparison and says so
    assert "its SHA-256 equals the file's (the remote's own md5 agreed too)" in rig.text


def test_if_the_listing_after_the_commit_fails_the_verified_object_is_kept_and_the_run_still_fails(rig):
    # The object was read back byte for byte before it was renamed, so it is a good point even though
    # this run could not confirm the rename. The run reports failure; nothing is deleted.
    before = seed(rig, stamps(datetime(2026, 10, 15), 3))
    rig.fake.fail("lsjson", ("exit", 1), 3)
    assert rig.run() == offsite.EXIT_FAILED
    files = rig.fake.remote_files()
    assert set(files) == set(before) | {FIXED_NAME}
    assert "deletefile" not in rig.fake.rclone_commands()
    assert fake_decrypt(files[FIXED_NAME])  # and it decrypts: it is a whole point


def test_an_older_point_is_never_deleted_by_a_run_that_fails_even_when_retention_would_want_it(rig):
    before = seed(rig, stamps(datetime(2026, 8, 1), 40))  # far more than retention keeps
    rig.fake.fail("age", ("exit", 1))
    assert rig.run() == offsite.EXIT_FAILED
    assert rig.fake.remote_files() == before


# --- retention in a run ---------------------------------------------------------------------------------


def test_a_run_drops_exactly_the_points_the_plan_drops_and_nothing_else(rig):
    seed(rig, stamps(datetime(2026, 10, 13), 12))   # Oct 13 .. Oct 24; with today's point the plan drops Oct 13..17
    assert rig.run() == 0, rig.text
    remaining = sorted(re.search(r"-(\d{8})T", n).group(1) for n in rig.fake.point_objects())
    assert remaining == ["20261018", "20261019", "20261020", "20261021", "20261022", "20261023", "20261024", "20261025"]
    deleted_calls = [c[-1].rsplit("/", 1)[1] for c in rig.fake.rclone_calls if c[0] == "deletefile"]
    assert sorted(n[13:21] for n in deleted_calls if n.endswith(".tar.age")) == ["20261013", "20261014", "20261015", "20261016", "20261017"]
    # a point is its object AND its record: both go, the object first, and no record is left without its object
    assert sorted(n[13:21] for n in deleted_calls if n.endswith(".commit")) == ["20261013", "20261014", "20261015", "20261016", "20261017"]
    assert all(n.endswith(".tar.age") for n in deleted_calls[:5]) and all(n.endswith(".commit") for n in deleted_calls[5:])
    assert len(rig.fake.remote_files()) == 2 * len(remaining)


def test_the_steady_state_run_adds_one_point_and_drops_the_one_that_aged_out(rig):
    seed(rig, stamps(datetime(2026, 9, 20), 35))      # Sep 20 .. Oct 24: already rotated by earlier runs
    first = rig.run("--max-deletions", "50")           # bring it to steady state (a one-off, owner-approved cleanup)
    assert first == 0, rig.text
    files = set(rig.fake.point_objects())
    assert len(files) == 11 and FIXED_NAME in files
    rig.fake.rclone_calls.clear()
    rig.now = FIXED_NOW + timedelta(days=1)
    rig.token = "0f0f0f0f"
    assert rig.run() == 0, rig.text                    # the next day, with the default cap
    deleted = [c[-1] for c in rig.fake.rclone_calls if c[0] == "deletefile" and c[-1].endswith(".tar.age")]
    assert 1 <= len(deleted) <= 2
    assert len(rig.fake.point_objects()) == 11         # +1 -1: the history does not grow


def test_retention_refuses_a_mass_deletion_and_deletes_nothing_but_the_new_point_stays(rig):
    before = seed(rig, stamps(datetime(2026, 7, 17), 100))   # Jul 17 .. Oct 24
    assert rig.run() == offsite.EXIT_RETENTION
    assert "more than --max-deletions" in rig.text and "IS stored and verified" in rig.text
    files = rig.fake.remote_files()
    assert files.keys() == before.keys() | {FIXED_NAME, FIXED_NAME + ".commit"}
    assert "deletefile" not in rig.fake.rclone_commands()


def test_a_zero_limit_turns_retention_off_without_failing_the_backup_itself(rig):
    seed(rig, stamps(datetime(2026, 9, 1), 54))
    assert rig.run("--max-deletions", "0") == offsite.EXIT_RETENTION
    assert FIXED_NAME in rig.fake.remote_files()


@pytest.mark.parametrize(
    "fault, deleted_something",
    [
        (("delete", ("exit", 1), 1), False),
        (("delete", ("exit", 1), 2), True),
        (("lsjson", ("exit", 1), 5), True),
    ],
    # A listing before retention that fails or is garbage is NOT here: the final check cannot then confirm the point, which is
    # its own status (exit 7, never "stored and verified"): see tests/test_deploy_offsite_final.py.
    ids=["first delete fails", "second delete fails", "check after retention fails"],
)
def test_a_retention_problem_is_its_own_status_and_never_loses_the_new_point(rig, fault, deleted_something):
    seed(rig, stamps(datetime(2026, 10, 13), 12))  # Oct 13 .. Oct 24: needs five deletions, within the cap
    rig.fake.fail(*fault)
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert FIXED_NAME in rig.fake.remote_files()
    assert ("deletefile" in rig.fake.rclone_commands()) is (fault[0] == "delete" or deleted_something)
    assert rig.work_leftovers() == []


def test_names_that_are_not_the_tools_own_are_never_touched(rig):
    foreign = {
        "notes.txt": b"the owner's notes",
        "house-helper-20260101T000000Z-ZZZZZZZZ.tar.age": b"bad token",
        "house-helper-20260101T000000Z-00000000.tar.age.bak": b"a copy",
        "house-helper-20261340T000000Z-00000003.tar.age": b"not a date",
        "house-helper-20261340T000000Z-00000003.tar.age.commit": b"not a date either",
        "20260101T000000Z.tar.age": b"another tool",
    }
    for name, data in foreign.items():
        rig.fake.put_remote(name, data)
    (rig.fake.folder() / "a-subfolder").mkdir()
    (rig.fake.folder() / "a-subfolder" / "inside.age").write_bytes(b"inside")
    seed(rig, stamps(datetime(2026, 10, 10), 12))
    assert rig.run() == 0, rig.text
    files = rig.fake.remote_files()
    for name, data in foreign.items():
        assert files[name] == data
    assert (rig.fake.folder() / "a-subfolder" / "inside.age").read_bytes() == b"inside"
    assert "left alone" in rig.text


def test_a_leftover_partial_upload_is_never_deleted_does_not_stop_the_backup_and_fails_the_run_visibly(rig):
    """A .partial of the tool's shape that was already there (a killed run, a stopped upload, somebody else) is not
    deleted: nothing proves whose it is. The backup is made and stored all the same, and the leftover is REPORTED (exit 6),
    not just warned about; an object that is not of the tool's shape is not even looked at."""
    stale = point_name("20261020T041500Z", "11111111") + ".partial"
    rig.fake.put_remote(stale, b"half")
    rig.fake.put_remote("notes.partial", b"not ours")
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert f"LEFTOVER PARTIAL {stale}: left untouched" in rig.text and "IS stored and verified" in rig.text
    assert "notes.partial" not in rig.text
    files = rig.fake.remote_files()
    assert files[stale] == b"half" and files["notes.partial"] == b"not ours" and FIXED_NAME in files
    assert "deletefile" not in rig.fake.rclone_commands()
    assert rig.committed_names() == {FIXED_NAME}


def test_only_this_tools_shapes_can_be_addressed_on_the_remote(rig):
    remote = offsite.Remote(REMOTE, rig.config, rig.fake, "/fake/bin/rclone")
    for name in ("", "..", "../x", "notes.txt", "a/b", FIXED_NAME + "/", FIXED_NAME + " ", "house-helper-bad.tar.age", FIXED_NAME + ".bak"):
        with pytest.raises(offsite.Failed):
            remote.delete(name)
    assert rig.fake.calls == []
    remote.ensure_folder()  # the folder itself may be made; nothing outside it
    assert rig.fake.rclone_commands() == ["mkdir"]


@pytest.mark.parametrize("command", ["delete", "purge", "cleanup", "sync", "move", "rmdirs", "dedupe", "copy"])
def test_the_fake_remote_would_fail_the_test_on_any_forbidden_rclone_command(rig, command):
    with pytest.raises(AssertionError, match="forbidden"):
        rig.fake.run(["/fake/bin/rclone", command, "--config", "x"], timeout=1)


# --- clean-up on every exit path ------------------------------------------------------------------------


SIGNALS = [("backup", 1), ("sanitize", 1), ("verify", 1), ("age", 1), ("mkdir", 1), ("lsjson", 1),
           ("upload", 1), ("lsjson", 2), ("download", 1), ("move", 1)]


@pytest.mark.parametrize("kind, nth", SIGNALS, ids=[f"{k}#{n}" for k, n in SIGNALS])
def test_a_signal_at_any_step_before_the_commit_removes_plaintext_and_deletes_nothing_on_the_remote(rig, kind, nth):
    before = seed(rig, stamps(datetime(2026, 10, 15), 4))
    rig.fake.fail(kind, "signal", nth)
    code = rig.run()
    assert code == 128 + signal.SIGTERM, rig.text
    assert rig.work_leftovers() == []
    assert all(not p.exists() for p in rig.fake.plaintext_paths)
    # The signal at the upload arrives before anything is written. After the upload had returned, this run's own .partial stays
    # (it cannot be told from a replaced one, so nothing deletes it) and the log says so; no point is committed either way.
    assert_what_a_failed_run_left(rig, before, PARTIAL if (kind, nth) in (("lsjson", 2), ("download", 1), ("move", 1)) else NOTHING)


def test_a_signal_while_confirming_the_commit_keeps_the_point_that_was_read_back(rig):
    before = seed(rig, stamps(datetime(2026, 10, 15), 4))
    rig.fake.fail("lsjson", "signal", 3)
    assert rig.run() == 128 + signal.SIGTERM
    assert set(rig.fake.remote_files()) == set(before) | {FIXED_NAME}
    assert rig.work_leftovers() == []


def test_a_signal_during_retention_ends_the_run_with_the_point_stored(rig):
    seed(rig, stamps(datetime(2026, 10, 13), 12))
    rig.fake.fail("delete", "signal", 1)
    assert rig.run() == 128 + signal.SIGTERM
    assert FIXED_NAME in rig.fake.remote_files()
    assert rig.work_leftovers() == []


def test_the_signal_handlers_are_put_back_when_the_run_ends(rig):
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    rig.run()
    assert {s: signal.getsignal(s) for s in before} == before


def test_a_directory_left_by_a_killed_run_is_swept_at_the_next_start_and_only_that(rig):
    stale = rig.work / (offsite.RUN_PREFIX + "killed")
    (stale / "pair").mkdir(parents=True)
    (stale / "pair" / "leftover.db").write_bytes(b"SQLite format 3\x00 old plaintext")
    bystander_dir, bystander_file = rig.work / "someone-elses", rig.work / (offsite.RUN_PREFIX + "file")
    bystander_dir.mkdir()
    bystander_file.write_text("not a directory")
    assert rig.run() == 0
    assert not stale.exists()
    assert bystander_dir.exists() and bystander_file.exists()
    assert "removed the leftover work directory" in rig.text


def test_a_failure_to_remove_the_work_directory_after_a_commit_is_reported_loudly_and_is_not_a_success(rig, monkeypatch):
    real = offsite.shutil.rmtree

    def refuse(path, *args, **kwargs):
        if Path(path).name.startswith(offsite.RUN_PREFIX):
            return None
        return real(path, *args, **kwargs)

    monkeypatch.setattr(offsite.shutil, "rmtree", refuse)
    assert rig.run() == offsite.EXIT_RETENTION, rig.text  # the backup is committed: partial success, never 0
    assert "WARNING: could not remove the work directory" in rig.text and "plaintext" in rig.text
    assert "IS stored and verified" in rig.text and FIXED_NAME in rig.fake.remote_files()


@posix_only
def test_a_second_run_at_the_same_time_does_nothing(rig):
    import fcntl

    descriptor = os.open(rig.work / offsite.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rig.run() == offsite.EXIT_FAILED
        assert "another off-host backup is running" in rig.text
        # The read-only checks of rclone ran before the lock (they change nothing); no backup, no container,
        # no age, nothing written to the remote.
        assert rig.fake.events == [] and rig.fake.rclone_writes() == []
    finally:
        os.close(descriptor)
    rig.log.clear()
    assert rig.run() == 0  # once the lock is released the next run goes through


def test_not_enough_room_for_the_copies_stops_the_run_before_any_is_made(rig, monkeypatch):
    monkeypatch.setattr(offsite, "free_bytes", lambda path: 1000)
    assert rig.run() == offsite.EXIT_FAILED
    assert "not enough room" in rig.text
    assert rig.fake.events == ["backup"] and rig.fake.rclone_writes() == []
    assert rig.work_leftovers() == []


# --- list ------------------------------------------------------------------------------------------------


def test_list_shows_what_retention_would_do_and_changes_nothing(rig):
    before = seed(rig, stamps(datetime(2026, 9, 1), 54))
    assert offsite.main(["list", "--rclone-config", str(rig.config), "--remote", REMOTE], runner=rig.fake, log=rig.log.append) == 0
    assert rig.fake.remote_files() == before
    # the checks, one listing, and one read of the commit record of each of the 54 points; nothing written
    assert rig.fake.rclone_commands() == ["config", "backend", "lsjson"] + ["cat"] * 54
    assert "54 recovery points" in rig.text and "would keep 11 and delete 43" in rig.text
    assert rig.fake.events == ["cat"] * 54  # no backup, no container, no age; only the reads of the records


# --- the real command runner ---------------------------------------------------------------------------


def python(code):
    return [sys.executable, "-c", code]


def test_the_runner_hands_a_file_to_stdin_writes_stdout_to_a_new_private_file_and_bounds_stderr(tmp_path):
    source = tmp_path / "in.bin"
    source.write_bytes(b"binary \x00\xff data")
    target = tmp_path / "out.bin"
    code = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.write('e' * 200000)"
    done = offsite.Runner().run(python(code), timeout=60, stdin_path=source, stdout_path=target)
    assert done.returncode == 0 and target.read_bytes() == source.read_bytes()
    assert len(done.stderr) == offsite.MAX_STDERR_BYTES
    if os.name != "nt":
        assert (target.stat().st_mode & 0o777) == 0o600
    with pytest.raises(FileExistsError):  # an existing file is never written over
        offsite.Runner().run(python("pass"), timeout=60, stdout_path=target)
    assert target.read_bytes() == source.read_bytes()


def test_the_runner_returns_stdout_when_asked_and_drops_it_otherwise():
    assert offsite.Runner().run(python("print('hello')"), timeout=60, capture=True).stdout.strip() == b"hello"
    assert offsite.Runner().run(python("print('hello')"), timeout=60).stdout == b""


def test_the_runner_does_not_pass_rclone_settings_from_the_environment_but_passes_what_it_is_given(monkeypatch):
    monkeypatch.setenv("RCLONE_CONFIG", "/somewhere/else.conf")
    monkeypatch.setenv("RCLONE_YANDEX_TOKEN", "should-not-be-seen")
    monkeypatch.setenv("KEEP_ME", "yes")
    code = "import os; print(sorted(k for k in os.environ if k.startswith(('RCLONE', 'KEEP', 'EXTRA'))))"
    done = offsite.Runner().run(python(code), timeout=60, capture=True, env_extra={"EXTRA": "1"})
    assert done.stdout.decode().strip() == "['EXTRA', 'KEEP_ME']"


def test_a_command_that_runs_too_long_is_stopped():
    started = time.monotonic()
    with pytest.raises(offsite.Failed, match="did not finish"):
        offsite.Runner().run(python("import time; time.sleep(120)"), timeout=1)
    assert time.monotonic() - started < 60


def test_a_command_is_stopped_when_a_signal_interrupts_the_wait(monkeypatch):
    started = []
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)

        def communicate(self, *args, **kwargs):
            raise offsite.Interrupted(signal.SIGTERM)

    monkeypatch.setattr(offsite.subprocess, "Popen", Spy)
    with pytest.raises(offsite.Interrupted):
        offsite.Runner().run(python("import time; time.sleep(120)"), timeout=120)
    assert started and started[0].poll() is not None  # the child was asked to stop and is gone


def test_the_script_runs_as_a_program_and_reports_usage_errors_with_status_2():
    done = subprocess.run([sys.executable, "-I", str(DEPLOY / "offsite_backup.py"), "run"], capture_output=True, timeout=60)
    assert done.returncode == 2
    done = subprocess.run([sys.executable, "-I", str(DEPLOY / "offsite_backup.py"), "--help"], capture_output=True, timeout=60)
    assert done.returncode == 0 and b"unpack" in done.stdout
