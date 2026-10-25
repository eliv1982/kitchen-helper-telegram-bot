"""The TEMPORARY full-disk access mode of the off-host backup (deploy/offsite_backup.py, RUNBOOK 15.12).

The mode exists for a stable rclone whose Yandex backend has no `app_folder` option, with an OAuth token that the
owner has authorised to reach the whole disk. What these tests hold it to:

  * it is OFF by default, and the authorization is exact: the mode name, one fixed folder, a configuration that does
    not contradict it. A missing, empty, misspelled, malformed or contradictory authorization is refused before any
    command (rclone included) runs, and `run` and `list` give the same answer to every case;
  * it keeps the tool inside ONE folder, NAME:house-helper-backup: nothing else is accepted as the folder, no rclone
    call names the root of the remote, no bulk command exists, and nothing outside the folder (the other mode's
    folder, a sibling, the root of the account, files that are not the tool's) is ever read as a point or deleted;
  * everything that makes an upload non-destructive and a backup trustworthy holds exactly as in the default mode:
    the whole table of failures of tests/test_deploy_offsite.py, an object that already has the run's name, a commit
    record that cannot be written, a final check that fails, a signal;
  * nothing secret reaches the log, not even when rclone's own message is full of it.
"""

import os
import signal
from datetime import datetime, timedelta

import pytest

from tests.deploy_support import DEPLOY
from tests.offsite_support import (
    FIXED_NAME,
    FIXED_NOW,
    FIXED_STAMP,
    FIXED_TOKEN,
    FULL_DISK_FOLDER,
    FULL_DISK_REMOTE,
    PUBLIC_KEY,
    RCLONE_CONF_FULL_DISK,
    FakeSystem,
    Rig,
    offsite,
    point_name,
)
from tests.test_deploy_offsite import FAILURE_PARAMS, NOTHING, PARTIAL, assert_what_a_failed_run_left, seed, stamps

MODE = offsite.ACCESS_FULL_DISK


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path, full_disk=True)


def conf(**changes) -> str:
    """The configuration section of a full-disk remote: the owner's own OAuth application, and NO app_folder."""
    values = {"type": "yandex", "client_id": "own-id", "client_secret": "own-secret", "token": '{"access_token":"t"}'}
    values.update(changes)
    return "[fake]\n" + "".join(f"{k} = {v}\n" for k, v in values.items() if v is not None)


def write_conf(rig, text: str) -> None:
    rig.config.write_text(text)
    if os.name != "nt":
        rig.config.chmod(0o600)


def attempt(rig, command: str, *, mode, remote: str):
    """`run` or `list` with the given authorization: `mode` is the value of --access-mode (None: the flag is absent)."""
    option = [] if mode is None else ["--access-mode", mode]
    if command == "run":
        argv = ["run", f"--tag=abc123def456", "--recipients-file", str(rig.recipients), "--rclone-config", str(rig.config),
                "--remote", remote, "--work-dir", str(rig.work), *option]
    else:
        argv = ["list", "--rclone-config", str(rig.config), "--remote", remote, *option]
    rig.log.clear()
    code = offsite.main(argv, runner=rig.fake, clock=lambda: rig.now, token=lambda: rig.token, log=rig.log.append, config_trust=rig.trust)
    return code


# --- off by default, and the authorization is exact ------------------------------------------------------------------


def test_the_default_mode_is_the_application_folder_in_the_parser_the_config_and_the_module():
    parser = offsite._build_parser()
    common = ["--rclone-config", "c", "--remote", "n:f"]
    run = parser.parse_args(["run", "--tag", "t", "--recipients-file", "r", "--work-dir", "/w", *common])
    show = parser.parse_args(["list", *common])
    assert run.access_mode == show.access_mode == offsite.ACCESS_APP_FOLDER
    assert offsite.Config(remote="n:f", rclone_config=offsite.Path("c")).access_mode == offsite.ACCESS_APP_FOLDER
    assert offsite.validate_remote("n:f") == "n:f"  # no mode given: the default mode's rule
    assert offsite.ACCESS_MODES == (offsite.ACCESS_APP_FOLDER, offsite.ACCESS_FULL_DISK)


def test_the_environment_alone_never_selects_the_mode(rig, monkeypatch):
    # The tool does not read the variables the unit's environment file sets: only the command line decides, so a stray
    # variable (or RCLONE_YANDEX_APP_FOLDER=false) cannot switch the mode on.
    monkeypatch.setenv("HOUSE_HELPER_OFFSITE_ACCESS_MODE", MODE)
    monkeypatch.setenv("RCLONE_YANDEX_APP_FOLDER", "false")
    assert attempt(rig, "run", mode=None, remote=FULL_DISK_REMOTE) == offsite.EXIT_REFUSED
    assert rig.fake.calls == []
    assert attempt(rig, "list", mode=None, remote=FULL_DISK_REMOTE) == offsite.EXIT_REFUSED
    assert rig.fake.calls == []


# (id, mode flag, remote, rclone.conf, exit status, words in the message). Every row is tried with `run` AND `list`.
AUTHORIZATION = [
    ("flag absent, full-disk config", None, FULL_DISK_REMOTE, conf(), offsite.EXIT_REFUSED, "--access-mode full-disk-temporary"),
    ("flag app-folder, full-disk config", offsite.ACCESS_APP_FOLDER, FULL_DISK_REMOTE, conf(), offsite.EXIT_REFUSED, "app_folder = true"),
    ("flag given, config says app_folder = true", MODE, FULL_DISK_REMOTE, conf(app_folder="true"), offsite.EXIT_REFUSED, "contradicts"),
    ("flag given, config says app_folder = True", MODE, FULL_DISK_REMOTE, conf(app_folder="True"), offsite.EXIT_REFUSED, "contradicts"),
    ("flag given, config says app_folder = yes", MODE, FULL_DISK_REMOTE, conf(app_folder="yes"), offsite.EXIT_REFUSED, "contradicts"),
    ("flag given, config says app_folder = 1", MODE, FULL_DISK_REMOTE, conf(app_folder="1"), offsite.EXIT_REFUSED, "contradicts"),
    ("flag given, the app-folder remote", MODE, "fake:house-helper", conf(), offsite.EXIT_REFUSED, "exactly"),
    ("flag given, a sub-folder", MODE, f"fake:{FULL_DISK_FOLDER}/sub", conf(), offsite.EXIT_REFUSED, "exactly"),
    ("flag given, the root", MODE, "fake:", conf(), offsite.EXIT_REFUSED, "--remote must be NAME:FOLDER"),
    ("flag given, no client id", MODE, FULL_DISK_REMOTE, conf(client_id=None), offsite.EXIT_REFUSED, "client_id and client_secret of its own"),
    ("flag given, empty client secret", MODE, FULL_DISK_REMOTE, conf(client_secret=""), offsite.EXIT_REFUSED, "client_id and client_secret of its own"),
    ("flag given, no token", MODE, FULL_DISK_REMOTE, conf(token=None), offsite.EXIT_REFUSED, "has no token"),
    ("flag given, not a yandex remote", MODE, FULL_DISK_REMOTE, conf(type="drive"), offsite.EXIT_REFUSED, "not a Yandex Disk remote"),
    ("flag given, no such section", MODE, FULL_DISK_REMOTE, "[other]\ntype = yandex\n", offsite.EXIT_REFUSED, "no [fake] section"),
    ("flag given, a setting twice", MODE, FULL_DISK_REMOTE, conf() + "token = again\n", offsite.EXIT_REFUSED, "cannot be read"),
    ("flag empty", "", FULL_DISK_REMOTE, conf(), offsite.EXIT_USAGE, "--access-mode"),
    ("flag misspelled: full-disk", "full-disk", FULL_DISK_REMOTE, conf(), offsite.EXIT_USAGE, "--access-mode"),
    ("flag misspelled: upper case", "FULL-DISK-TEMPORARY", FULL_DISK_REMOTE, conf(), offsite.EXIT_USAGE, "--access-mode"),
    ("flag misspelled: underscores", "full_disk_temporary", FULL_DISK_REMOTE, conf(), offsite.EXIT_USAGE, "--access-mode"),
    ("flag with a space", f" {MODE}", FULL_DISK_REMOTE, conf(), offsite.EXIT_USAGE, "--access-mode"),
    ("flag with a suffix", f"{MODE}-forever", FULL_DISK_REMOTE, conf(), offsite.EXIT_USAGE, "--access-mode"),
    ("flag a truth value", "true", FULL_DISK_REMOTE, conf(), offsite.EXIT_USAGE, "--access-mode"),
]


@pytest.mark.parametrize("command", ["run", "list"])
@pytest.mark.parametrize("case", AUTHORIZATION, ids=[c[0] for c in AUTHORIZATION])
def test_a_missing_empty_misspelled_or_contradictory_authorization_is_refused_before_any_command_runs(rig, command, case):
    _id, mode, remote, text, status, words = case
    write_conf(rig, text)
    code = attempt(rig, command, mode=mode, remote=remote)
    assert code == status, rig.text
    if status == offsite.EXIT_REFUSED:
        assert words in rig.text, rig.text
    assert rig.fake.calls == []          # not bash, not docker, not age, and not rclone: not even `config providers`
    assert rig.fake.events == [] and rig.fake.remote_files() == {}
    assert rig.work_leftovers() == []


def test_run_and_list_give_the_same_answer_to_every_case_of_the_authorization(rig):
    for _id, mode, remote, text, status, _words in AUTHORIZATION:
        write_conf(rig, text)
        codes = {command: attempt(rig, command, mode=mode, remote=remote) for command in ("run", "list")}
        assert codes == {"run": status, "list": status}, (_id, codes)


def test_the_authorized_mode_works_for_run_and_for_list_with_an_rclone_that_has_no_app_folder_option(rig):
    assert rig.fake.app_folder_support is False                   # stable 1.75.2
    assert rig.run() == 0, rig.text
    assert rig.committed_names() == {FIXED_NAME}
    assert "NOTICE: TEMPORARY access mode full-disk-temporary" in rig.text
    assert f"touches only {FULL_DISK_REMOTE}/" in rig.text
    assert attempt(rig, "list", mode=MODE, remote=FULL_DISK_REMOTE) == 0
    assert "NOTICE: TEMPORARY access mode" in rig.text and FIXED_NAME in rig.text


@pytest.mark.parametrize("app_folder", [None, "", "false", "False"], ids=["absent", "empty", "false", "False"])
def test_the_application_folder_setting_may_be_absent_empty_or_false_in_the_full_disk_mode(rig, app_folder):
    write_conf(rig, conf(app_folder=app_folder))
    assert rig.run() == 0, rig.text


def test_an_rclone_that_does_have_the_option_is_told_to_move_to_the_narrow_mode_and_still_runs(rig):
    rig.fake.app_folder_support = True
    assert rig.run() == 0, rig.text
    assert "HAS the app_folder option" in rig.text and "--access-mode app-folder" in rig.text


def test_the_narrow_mode_still_refuses_the_rclone_without_the_option_and_names_the_exception(tmp_path):
    narrow = Rig(tmp_path)
    narrow.fake.app_folder_support = False
    assert narrow.run() == offsite.EXIT_REFUSED
    assert "no app_folder option" in narrow.text and "--access-mode full-disk-temporary" in narrow.text
    assert narrow.fake.rclone_commands() == ["config"] and narrow.fake.events == []


@pytest.mark.parametrize("fault", [("providers", ("exit", 1)), ("providers", "garbage")], ids=["listing fails", "listing is not json"])
def test_an_rclone_whose_capabilities_cannot_be_read_is_refused_in_the_full_disk_mode_too(rig, fault):
    rig.fake.fail(*fault)
    assert rig.run() == offsite.EXIT_REFUSED and "cannot be checked" in rig.text
    assert rig.fake.events == [] and rig.fake.rclone_writes() == []


def test_a_remote_that_cannot_rename_on_the_server_is_refused_in_the_full_disk_mode_too(rig):
    rig.fake.can_move = False
    assert rig.run() == offsite.EXIT_REFUSED and "cannot rename server-side" in rig.text
    assert rig.fake.events == [] and rig.fake.rclone_writes() == []


# --- the one fixed folder ----------------------------------------------------------------------------------------------


ACCEPTED = [FULL_DISK_REMOTE, f"a-b_c.d:{FULL_DISK_FOLDER}", f"x:{FULL_DISK_FOLDER}"]
REJECTED = [
    "", "fake", "fake:", ":", f":{FULL_DISK_FOLDER}", "fake:/", f"fake:/{FULL_DISK_FOLDER}", f"fake:{FULL_DISK_FOLDER}/",
    f"fake:{FULL_DISK_FOLDER}//", f"fake:{FULL_DISK_FOLDER}/.", f"fake:{FULL_DISK_FOLDER}/..", "fake:..", "fake:.",
    f"fake:../{FULL_DISK_FOLDER}", f"fake:{FULL_DISK_FOLDER}/../x", f"fake:a/../{FULL_DISK_FOLDER}", f"fake:{FULL_DISK_FOLDER}/sub",
    f"fake:sub/{FULL_DISK_FOLDER}", "fake:house-helper", "fake:house-helper/house-helper-backup", f"fake:{FULL_DISK_FOLDER}2",
    f"fake:{FULL_DISK_FOLDER}-old", f"fake:{FULL_DISK_FOLDER[:-1]}", f"fake:{FULL_DISK_FOLDER.upper()}", f"fake:{FULL_DISK_FOLDER.title()}",
    f" fake:{FULL_DISK_FOLDER}", f"fake:{FULL_DISK_FOLDER} ", f"fake:{FULL_DISK_FOLDER}\n", f"fake:{FULL_DISK_FOLDER}\x00",
    f"fake:{FULL_DISK_FOLDER}%2F..", f"fake:{FULL_DISK_FOLDER}\\..", f"fake:{FULL_DISK_FOLDER}\\sub", f"fake:~/{FULL_DISK_FOLDER}",
    f"fake:$HOME/{FULL_DISK_FOLDER}", f"fake:{FULL_DISK_FOLDER}*", f"fake:*", f"fake:{FULL_DISK_FOLDER}:x", f":backend:{FULL_DISK_FOLDER}",
    f":local:{FULL_DISK_FOLDER}", f"/etc:{FULL_DISK_FOLDER}", f"C:\\{FULL_DISK_FOLDER}", "/", "/tmp/backups", "fake:\u202ehouse-helper-backup",
    "fake:house-helper-backup\u00a0",
]


@pytest.mark.parametrize("remote", ACCEPTED)
def test_the_full_disk_folder_is_accepted_only_in_its_one_exact_form(remote):
    assert offsite.validate_remote(remote, MODE) == remote
    assert offsite.Remote(remote, offsite.Path("c"), None, "/bin/rclone", MODE).spec == remote


@pytest.mark.parametrize("remote", REJECTED, ids=[repr(r)[:40] for r in REJECTED])
def test_empty_root_malformed_escaping_and_other_folders_are_rejected_by_the_validator_the_remote_and_both_commands(rig, remote):
    with pytest.raises(offsite.Refused):
        offsite.validate_remote(remote, MODE)
    with pytest.raises(offsite.Refused):
        offsite.Remote(remote, rig.config, rig.fake, "/fake/bin/rclone", MODE)
    for command in ("run", "list"):
        assert attempt(rig, command, mode=MODE, remote=remote) == offsite.EXIT_REFUSED, (command, rig.text)
        assert rig.fake.calls == [] and rig.fake.remote_files() == {}


def test_a_folder_that_is_fine_in_the_default_mode_is_not_the_full_disk_folder():
    for remote in ("fake:house-helper", "fake:backups/house-helper", f"fake:{FULL_DISK_FOLDER}/sub"):
        assert offsite.validate_remote(remote) == remote
        with pytest.raises(offsite.Refused, match="exactly"):
            offsite.validate_remote(remote, MODE)


def test_an_unknown_mode_is_refused_by_the_validator_and_the_remote():
    with pytest.raises(offsite.Refused, match="--access-mode"):
        offsite.validate_remote(FULL_DISK_REMOTE, "full-disk")
    with pytest.raises(offsite.Refused, match="--access-mode"):
        offsite.Remote(FULL_DISK_REMOTE, offsite.Path("c"), None, "/bin/rclone", "")


REMOTE_NAMES = [
    f"house-helper-{FIXED_STAMP}-{FIXED_TOKEN}.tar.age",
    f"house-helper-{FIXED_STAMP}-{FIXED_TOKEN}.tar.age.partial",
    f"house-helper-{FIXED_STAMP}-{FIXED_TOKEN}.tar.age.commit",
]


@pytest.mark.parametrize("name", REMOTE_NAMES)
def test_the_remote_addresses_only_its_own_shapes_directly_under_the_one_folder(rig, name):
    remote = offsite.Remote(FULL_DISK_REMOTE, rig.config, rig.fake, "/fake/bin/rclone", MODE)
    assert remote._object(name) == f"{FULL_DISK_REMOTE}/{name}"


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../x", "a/b", f"../{FIXED_NAME}", f"sub/{FIXED_NAME}", f"/{FIXED_NAME}", f"{FIXED_NAME}/..", "notes.txt",
     FIXED_NAME + "x", FIXED_NAME.upper(), FIXED_NAME + "\n", "house-helper-20261025T041507Z-0A1B2C3D.tar.age", "*", "**"],
)
def test_the_remote_refuses_to_address_anything_else(rig, name):
    remote = offsite.Remote(FULL_DISK_REMOTE, rig.config, rig.fake, "/fake/bin/rclone", MODE)
    with pytest.raises(offsite.Failed, match="refusing to address"):
        remote._object(name)


# --- nothing outside the folder is ever named, read, counted or deleted --------------------------------------------------


def plant_outside_the_folder(rig) -> dict:
    """Everything an account holds that is NOT this tool's folder: the other mode's folder with a perfectly valid point,
    a sibling with a similar name, personal files, and objects of the tool's own shape at the root."""
    account = rig.fake.account
    old = point_name("20200101T000000Z")
    planted = {
        account / "Documents" / "private.txt": b"personal data",
        account / "house-helper" / old: b"a point of the application-folder mode",
        account / "house-helper" / (old + offsite.COMMIT_SUFFIX): b"its record",
        account / (FULL_DISK_FOLDER + "2") / old: b"a sibling folder",
        account / (FULL_DISK_FOLDER + "-old") / old: b"another sibling",
        account / old: b"at the root, in the tool's shape",
        account / (old + ".partial"): b"at the root, in the tool's shape",
        account / (old + offsite.COMMIT_SUFFIX): b"at the root, in the tool's shape",
    }
    for path, data in planted.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return planted


def remote_operands(call):
    return [a for a in call if a.startswith("fake:")]


def test_a_run_with_retention_and_a_list_touch_nothing_outside_the_one_folder_and_name_no_root(rig):
    planted = plant_outside_the_folder(rig)
    inside_foreign = {"notes.txt": b"mine", "house-helper-x.tar.age": b"not the tool's name", FIXED_NAME + ".bak": b"nor this"}
    for name, data in inside_foreign.items():
        rig.fake.put_remote(name, data)
    seed(rig, stamps(datetime(2026, 10, 1), 20))                      # 11 of these are due to be dropped
    assert rig.run("--max-deletions", "30") == 0, rig.text
    assert attempt(rig, "list", mode=MODE, remote=FULL_DISK_REMOTE) == 0

    deleted = [call[-1] for call in rig.fake.rclone_calls if call[0] == "deletefile"]
    assert len(deleted) >= 10                                          # retention really ran
    assert all(name.startswith(FULL_DISK_REMOTE + "/") and "/" not in name[len(FULL_DISK_REMOTE) + 1:] for name in deleted)
    for path, data in planted.items():
        assert path.read_bytes() == data, path                         # byte for byte
    files = rig.fake.remote_files()
    for name, data in inside_foreign.items():
        assert files[name] == data                                     # not of the tool's shape: left alone, inside the folder too
    # every operand that names the remote names the folder or one object directly in it; none names the root
    for call in rig.fake.rclone_calls:
        for operand in remote_operands(call):
            assert operand == FULL_DISK_REMOTE or operand.startswith(FULL_DISK_REMOTE + "/"), call
            assert operand not in ("fake:", "fake:/"), call
    assert {c[0] for c in rig.fake.rclone_calls} <= {"config", "backend", "mkdir", "lsjson", "copyto", "moveto", "deletefile", "cat"}
    backend = [c for c in rig.fake.rclone_calls if c[0] == "backend"]
    assert backend and all(c[-2:] == ["features", FULL_DISK_REMOTE] for c in backend)


def test_objects_of_the_tools_shape_outside_the_folder_are_not_points_and_not_counted(rig):
    plant_outside_the_folder(rig)
    assert rig.run() == 0, rig.text
    assert rig.committed_names() == {FIXED_NAME}                       # the planted valid-looking point of another folder is not in this one


@pytest.mark.parametrize("operand", ["fake:", "fake:/", "fake:house-helper", f"{FULL_DISK_REMOTE}2", f"{FULL_DISK_REMOTE}-old/x"])
def test_the_fake_remote_fails_the_test_when_any_command_names_the_root_or_leaves_the_folder(rig, operand):
    with pytest.raises(AssertionError, match="outside the configured folder"):
        rig.fake.run(
            ["/fake/bin/rclone", "lsjson", "--config", str(rig.config), "--log-level", "ERROR", "--files-only", "--hash", operand],
            timeout=1, capture=True,
        )
    with pytest.raises(AssertionError, match="outside the configured folder|features"):
        rig.fake.run(["/fake/bin/rclone", "backend", "--config", str(rig.config), "--log-level", "ERROR", "features", operand], timeout=1, capture=True)


@pytest.mark.parametrize("command", ["delete", "purge", "cleanup", "sync", "move", "rmdirs", "dedupe", "copy", "rmdir", "touch", "check"])
def test_no_bulk_or_unlisted_rclone_command_is_available_in_the_full_disk_mode_either(rig, command):
    with pytest.raises(AssertionError, match="forbidden rclone command"):
        rig.fake.run(["/fake/bin/rclone", command, "--config", str(rig.config), "--log-level", "ERROR", FULL_DISK_REMOTE], timeout=1)


# --- non-destructive upload, and nothing incomplete is stored ----------------------------------------------------------------


@pytest.mark.parametrize("fault, events, left", FAILURE_PARAMS)
def test_every_failure_of_the_default_mode_also_deletes_nothing_in_the_full_disk_mode(rig, fault, events, left):
    before = seed(rig, stamps(datetime(2026, 10, 15), 6))
    kind, action, nth = fault
    rig.fake.fail(kind, action, nth)
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert_what_a_failed_run_left(rig, before, left)
    assert rig.work_leftovers() == []
    assert rig.fake.events == events
    assert "IS stored" not in rig.text and f"stored {FIXED_NAME}" not in rig.text


@pytest.mark.parametrize("suffix", ["", ".partial", ".commit"])
def test_an_object_that_already_has_this_runs_name_is_never_overwritten_and_nothing_is_uploaded(rig, suffix):
    rig.fake.put_remote(FIXED_NAME + suffix, b"somebody else's bytes")
    before = rig.fake.remote_files()
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert "already exists" in rig.text
    assert rig.fake.remote_files() == before
    assert "upload" not in rig.fake.events and "delete" not in rig.fake.events


def test_a_commit_record_that_cannot_be_written_leaves_an_unresolved_object_that_is_never_called_stored(rig):
    older = seed(rig, stamps(datetime(2026, 10, 15), 5))
    rig.fake.fail("record", ("exit", 1))
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert "NOT a recovery point" in rig.text and "IS stored" not in rig.text
    folder = rig.classify()
    assert FIXED_NAME in folder.unresolved and FIXED_NAME not in rig.committed_names()
    assert {n: rig.fake.remote_files()[n] for n in older} == older
    # the next run reports it, makes its own point, and drops no committed point while the object is there
    rig.fake.faults.clear()
    rig.now, rig.token = FIXED_NOW + timedelta(days=1), "1b2c3d4e"
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert f"UNRESOLVED {FIXED_NAME}" in rig.text
    files = rig.fake.remote_files()
    assert {n: files[n] for n in older} == older and FIXED_NAME in files


def test_a_final_check_that_cannot_confirm_the_point_is_exit_7_and_deletes_nothing(rig):
    before = seed(rig, stamps(datetime(2026, 10, 1), 20))
    rig.fake.fail("lsjson", "no_hashes", 4)
    assert rig.run() == offsite.EXIT_UNCONFIRMED, rig.text
    assert "NOT reported as stored" in rig.text and "deletefile" not in rig.fake.rclone_commands()
    files = rig.fake.remote_files()
    assert {n: files[n] for n in before} == before


@pytest.mark.parametrize("kind, nth", [("upload", 1), ("lsjson", 2), ("download", 1), ("move", 1)])
def test_a_signal_before_the_commit_deletes_nothing_and_leaves_the_runs_own_partial_where_it_is(rig, kind, nth):
    before = seed(rig, stamps(datetime(2026, 10, 15), 4))
    rig.fake.fail(kind, "signal", nth)
    assert rig.run() == 128 + signal.SIGTERM, rig.text
    assert_what_a_failed_run_left(rig, before, NOTHING if kind == "upload" else PARTIAL)   # the signal at the upload comes before any write
    assert rig.work_leftovers() == []


def test_the_encrypted_object_is_all_that_reaches_the_remote_and_the_age_command_takes_public_keys_only(rig):
    assert rig.run() == 0, rig.text
    files = rig.fake.remote_files()
    assert set(files) == {FIXED_NAME, FIXED_NAME + offsite.COMMIT_SUFFIX}
    assert files[FIXED_NAME].startswith(offsite.AGE_HEADER) and b"SQLite format 3" not in files[FIXED_NAME]
    age = next(c for c in rig.fake.calls if os.path.basename(c[0]) == "age")
    assert age[1:] == ["-R", str(rig.recipients)] and PUBLIC_KEY in rig.fake.age_recipients


# --- nothing secret in the log, whatever rclone says --------------------------------------------------------------------------

LEAK_TOKEN = "SENTINEL-ACCESS-TOKEN-71bc"
LEAK_SECRET = "SENTINEL-CLIENT-SECRET-9f3a"
LEAK_PATH = "/etc/house-helper/rclone.conf"


class Loud(FakeSystem):
    """Every failing command answers with a message full of what must never be logged."""

    def run(self, argv, **kwargs):
        result = super().run(argv, **kwargs)
        if result.returncode != 0 and os.path.basename(str(argv[0])) in ("rclone", "age"):  # backup.sh and dbtool never see a token
            noise = f"error: token {LEAK_TOKEN} client_secret {LEAK_SECRET} config {LEAK_PATH} AGE-SECRET-KEY-1QQQ".encode()
            result = offsite.Result(result.returncode, result.stdout, noise)
        return result


LEAKS = (LEAK_TOKEN, LEAK_SECRET, LEAK_PATH, "AGE-SECRET-KEY", "access_token", "client_secret")
LEAK_FAULTS = [("providers", ("exit", 1)), ("features", ("exit", 1)), ("mkdir", ("exit", 1)), ("lsjson", ("exit", 1)),
               ("upload", ("exit", 1)), ("download", ("exit", 1)), ("move", ("exit", 1)), ("record", ("exit", 1)),
               ("age", ("exit", 1)), ("cat", ("exit", 1))]


@pytest.mark.parametrize("fault", LEAK_FAULTS, ids=[f[0] for f in LEAK_FAULTS])
def test_nothing_secret_reaches_the_log_when_a_command_fails_with_a_message_full_of_secrets(rig, capsys, fault):
    write_conf(rig, conf(client_secret=LEAK_SECRET, token='{"access_token":"%s"}' % LEAK_TOKEN))
    rig.fake = Loud(rig.root, rig.database, folder=FULL_DISK_FOLDER)
    rig.fake.app_folder_support = False
    seed(rig, stamps(datetime(2026, 10, 15), 2))
    rig.fake.fail(*fault)
    code = offsite.main(rig.args(), runner=rig.fake, clock=lambda: rig.now, token=lambda: rig.token, deploy_dir=DEPLOY,
                        config_trust=rig.trust)        # the real `say`: the text goes to stderr
    assert code != 0
    shown = capsys.readouterr().err
    for leak in LEAKS:
        assert leak not in shown, (fault, leak)


@pytest.mark.parametrize("command", ["run", "list"])
def test_a_refused_authorization_prints_no_value_of_the_configuration(rig, command):
    write_conf(rig, conf(client_secret=LEAK_SECRET, token='{"access_token":"%s"}' % LEAK_TOKEN, app_folder="true"))
    assert attempt(rig, command, mode=MODE, remote=FULL_DISK_REMOTE) == offsite.EXIT_REFUSED
    for leak in (LEAK_TOKEN, LEAK_SECRET, "own-id", str(rig.config)):
        assert leak not in rig.text, leak


def test_a_successful_run_and_list_print_no_value_of_the_configuration_or_a_path_of_it(rig):
    write_conf(rig, conf(client_secret=LEAK_SECRET, token='{"access_token":"%s"}' % LEAK_TOKEN))
    assert rig.run() == 0
    assert attempt(rig, "list", mode=MODE, remote=FULL_DISK_REMOTE) == 0
    for leak in (LEAK_TOKEN, LEAK_SECRET, str(rig.config), "access_token"):
        assert leak not in rig.text, leak


def test_the_log_of_a_full_disk_run_carries_names_sizes_and_checksums_only(rig):
    from tests.offsite_support import DELETED_MARKER, KEPT_MARKER

    assert rig.run() == 0
    for secret in (KEPT_MARKER, DELETED_MARKER, PUBLIC_KEY, "synthetic", "test-client", '"access_token"'):  # the notice says "OAuth token", never a value
        assert secret not in rig.text, secret


@pytest.mark.parametrize("full_disk", [True, False], ids=["full-disk-temporary", "app-folder"])
def test_an_object_that_appears_under_the_final_name_before_the_rename_is_neither_replaced_nor_judged_nor_deleted(tmp_path, full_disk):
    """`moveto` replaces an existing destination on the real rclone 1.75.2; the tool passes --ignore-existing, sees that the partial is
    still there, and fails the run: the foreign object keeps its bytes and gets no record, and nothing is deleted, not even this run's
    own partial (it stays where it is, like every .partial)."""
    rig = Rig(tmp_path, full_disk=full_disk)
    foreign = b"somebody else's object, which happens to have the final name"
    rig.fake.fail("move", lambda: rig.fake.put_remote(FIXED_NAME, foreign))
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert "the rename did not take place" in rig.text and "IS stored" not in rig.text
    files = rig.fake.remote_files()
    assert files[FIXED_NAME] == foreign and set(files) == {FIXED_NAME, FIXED_NAME + ".partial"}   # untouched, no commit record
    assert [call[-1] for call in rig.fake.rclone_calls if call[0] == "deletefile"] == []         # nothing at all was deleted
    assert rig.work_leftovers() == []


@pytest.mark.parametrize("full_disk", [True, False], ids=["full-disk-temporary", "app-folder"])
def test_an_object_that_appears_under_the_partial_name_before_the_upload_is_skipped_never_overwritten_and_never_deleted(tmp_path, full_disk):
    """The race of the final audit: after the folder was listed, somebody else creates NAME.partial. rclone skips the copy
    (--ignore-existing) and, because of --error-on-no-transfer, says so (exit 9) instead of exiting 0. The run fails, and the
    foreign object keeps every byte and is the target of no destructive call: no run deletes a .partial at all."""
    rig = Rig(tmp_path, full_disk=full_disk)
    partial = FIXED_NAME + ".partial"
    foreign = b"foreign bytes: not this run's upload, and nobody may delete them"
    rig.fake.fail("upload", lambda: rig.fake.put_remote(partial, foreign))
    assert rig.run() == offsite.EXIT_FAILED, rig.text                      # the failure status, never the success one
    assert "rclone skipped the upload (exit 9" in rig.text and "IS stored" not in rig.text
    assert f"{partial} was not confirmed as this run's own upload, so it is NOT removed" in rig.text
    assert rig.fake.remote_files() == {partial: foreign}                   # byte for byte, and nothing else: no final name, no commit record
    assert rig.fake.rclone_writes() == ["mkdir", "copyto"]                 # the skipped copy is the only write: no deletefile, no moveto, no record upload
    assert rig.fake.events[-1] == "upload" and not {"download", "move", "delete", "record"} & set(rig.fake.events)
    assert rig.committed_names() == set()
    assert f"committed {FIXED_NAME}" not in rig.text and f"stored {FIXED_NAME}" not in rig.text   # nothing that reads as a stored point
    assert rig.work_leftovers() == []
