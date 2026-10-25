"""Who owns the object under a partial name (deploy/offsite_backup.py, RUNBOOK 15.2 and 15.11).

The audits found, in turn, that a foreign NAME.partial could be deleted by the run that did not make it: first because the
take-back of "its own" partial was armed before the upload even started; then, after that was closed, because the sweep of
the NEXT run deleted every partial of the tool's shape, the very object the failed run had preserved; and a delete by name
after a successful upload cannot be tied to the object that was uploaded (rclone has no delete that is conditional on the
content), so it could remove an object that replaced it. The rule that closes all three, held here:

  * NO RUN EVER DELETES A `.partial`: not an earlier run's, not a foreign one, not the partial of this run's own failed upload.
    `Remote.delete` refuses the name, and there is no clean-up, sweep or take-back that could call it;
  * a `.partial` the tool sees is logged (`LEFTOVER PARTIAL`), makes every run that commits a point end with exit 6, is shown by
    `list`, and stays byte for byte through every run after it, until an operator removes it (RUNBOOK 15.11);
  * it takes no part in retention (not counted, not in the way) and draws on no budget;
  * the same holds for an object that replaced the partial before the rename and was renamed instead: the listing after the
    rename does not match, the object is NOT deleted, it has no commit record, and it is reported as UNRESOLVED;
  * `copyto` carries --error-on-no-transfer, so a skipped copy is exit 9 and a failure of the upload, never a quiet success.

The exact race of the first audit, in both modes, is tests/test_deploy_offsite_fulldisk.py (an object that appears under the
partial name before the upload); the real rclone's exit 9 is tests/test_deploy_offsite_real_rclone.py (opt-in).
"""

from datetime import timedelta

import pytest

from tests.offsite_support import FIXED_NAME, FIXED_NOW, FIXED_STAMP, Rig, offsite, point_name
from tests.test_deploy_offsite import seed
from tests.test_deploy_offsite_protocol import HISTORY, deleted_names, quarantine

PARTIAL = FIXED_NAME + ".partial"
MODES = pytest.mark.parametrize("full_disk", [True, False], ids=["full-disk-temporary", "app-folder"])
FOREIGN = b"foreign bytes: not this run's upload, and nobody but an operator may delete them"


def deleted(rig):
    return [call[-1] for call in rig.fake.rclone_calls if call[0] == "deletefile"]


def partials(rig):
    return {n: b for n, b in rig.fake.remote_files().items() if n.endswith(".partial")}


def later_runs(rig, tokens=("00000001", "00000002", "00000003")):
    """The runs after a failed one: a new day and a new name each. The faults of the first run are cleared. Yields each
    run's exit status, with the log of that run alone in rig.text."""
    rig.fake.faults.clear()
    for day, token in enumerate(tokens, start=1):
        rig.log.clear()
        rig.token = token
        rig.now = FIXED_NOW + timedelta(days=day)
        yield rig.run()


# --- Remote.upload: what rclone's exit status means ----------------------------------------------------------------------------


class Scripted(offsite.Runner):
    """A runner that answers every command with one exit status and remembers the command lines."""

    def __init__(self, code):
        self.code = code
        self.calls = []

    def run(self, argv, *, timeout, stdin_path=None, stdout_path=None, capture=False, env_extra=None):
        self.calls.append([str(a) for a in argv])
        return offsite.Result(self.code)


def uploader(tmp_path, code):
    runner = Scripted(code)
    return offsite.Remote("fake:house-helper", tmp_path / "rclone.conf", runner, "/fake/bin/rclone"), runner


def test_an_upload_that_rclone_reports_as_transferred_returns(tmp_path):
    remote, runner = uploader(tmp_path, 0)
    remote.upload(tmp_path / "sealed.bin", PARTIAL)
    assert len(runner.calls) == 1


def test_an_upload_that_rclone_skipped_is_a_failure_that_says_so_and_never_a_return(tmp_path):
    remote, runner = uploader(tmp_path, offsite.RCLONE_NOTHING_TRANSFERRED)
    assert offsite.RCLONE_NOTHING_TRANSFERRED == 9
    with pytest.raises(offsite.Failed, match=r"rclone skipped the upload \(exit 9, nothing transferred\).*did not make appeared"):
        remote.upload(tmp_path / "sealed.bin", PARTIAL)
    assert len(runner.calls) == 1                       # not retried: a second attempt would only skip again


@pytest.mark.parametrize("code", [1, 2, 3, 4, 5, 6, 7, 8, 10, 99, 143])
def test_every_other_exit_status_of_the_upload_is_a_plain_failure(tmp_path, code):
    remote, _ = uploader(tmp_path, code)
    with pytest.raises(offsite.Failed, match=rf"rclone copyto failed \(exit {code}\) while uploading") as raised:
        remote.upload(tmp_path / "sealed.bin", PARTIAL)
    assert "skipped" not in str(raised.value)


def test_the_upload_command_line_never_overwrites_and_never_lets_a_skip_pass_as_success(tmp_path):
    remote, runner = uploader(tmp_path, 0)
    remote.upload(tmp_path / "sealed.bin", PARTIAL)
    argv = runner.calls[0]
    assert argv[1] == "copyto"
    assert {"--immutable", "--ignore-existing", "--error-on-no-transfer"} <= set(argv)
    assert argv[argv.index("--retries") + 1] == "1"
    assert argv[-1] == f"fake:house-helper/{PARTIAL}"


# --- the delete function itself refuses a partial ------------------------------------------------------------------------------


def test_the_delete_function_refuses_a_partial_so_no_code_path_can_delete_one(tmp_path):
    remote, runner = uploader(tmp_path, 0)
    with pytest.raises(offsite.Failed, match=r"refusing to delete a \.partial object"):
        remote.delete(PARTIAL)
    assert runner.calls == []                                  # not even an attempt
    remote.delete(FIXED_NAME)                                  # the other shapes are still deletable (retention): one call
    remote.delete(FIXED_NAME + offsite.COMMIT_SUFFIX)
    assert [call[1] for call in runner.calls] == ["deletefile", "deletefile"]


@MODES
def test_a_confirmed_delete_of_a_partial_deletes_nothing_and_says_it_is_still_there(tmp_path, full_disk):
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.put_remote(PARTIAL, FOREIGN)
    remote = offsite.Remote(rig.remote, rig.config, rig.fake, "/fake/bin/rclone",
                            offsite.ACCESS_FULL_DISK if full_disk else offsite.ACCESS_APP_FOLDER)
    result = remote.delete_confirmed([PARTIAL])
    assert result.gone == [] and result.left == [PARTIAL] and deleted(rig) == []
    assert rig.fake.remote_files() == {PARTIAL: FOREIGN}


def test_nothing_that_could_take_back_or_sweep_a_partial_exists_any_more():
    for gone in ("take_back_partial", "drop_stale_partials", "reject"):
        assert not hasattr(offsite.Job, gone), gone


# --- a name that is taken, or an upload that did not return: nothing that carries the name is deleted ----------------------------


@MODES
def test_a_failure_of_the_upload_other_than_a_skip_does_not_delete_what_carries_the_name_either(tmp_path, full_disk):
    """The existence check of rclone itself may be what fails (exit 1, nothing written) while a foreign object is there: an exit
    status other than 9 is no proof of ownership either."""
    rig = Rig(tmp_path, full_disk=full_disk)
    real_run = rig.fake.run

    def run(argv, **kwargs):
        if argv[1] == "copyto" and str(argv[-1]).startswith("fake:") and str(argv[-1]).endswith(PARTIAL):
            rig.fake.put_remote(PARTIAL, FOREIGN)
            return offsite.Result(1)
        return real_run(argv, **kwargs)

    rig.fake.run = run
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert "rclone copyto failed (exit 1) while uploading" in rig.text and "NOT removed" in rig.text
    assert rig.fake.remote_files() == {PARTIAL: FOREIGN}
    assert deleted(rig) == [] and "moveto" not in rig.fake.rclone_commands()
    assert rig.work_leftovers() == []


# --- THE REGRESSION OF THIS REMEDIATION: what the failed run preserved, the next runs preserve too ---------------------------


@MODES
def test_a_foreign_partial_that_the_failed_run_preserved_survives_every_later_run_byte_for_byte(tmp_path, full_disk):
    """The audit's blocker: run 1 meets a foreign object under its partial name (rclone skips the copy, exit 9) and correctly
    leaves it. Run 2 used to sweep every `.partial` of the tool's shape, and deleted exactly that object."""
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.fail("upload", lambda: rig.fake.put_remote(PARTIAL, FOREIGN))          # created after the folder was listed
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert rig.fake.remote_files() == {PARTIAL: FOREIGN} and deleted(rig) == []

    for day, status in enumerate(later_runs(rig), start=1):
        assert status == offsite.EXIT_RETENTION, rig.text                          # stored, and the leftover is reported
        assert f"LEFTOVER PARTIAL {PARTIAL}: left untouched" in rig.text and "IS stored and verified" in rig.text
        assert partials(rig) == {PARTIAL: FOREIGN}, f"run {day + 1}"                # byte for byte, after every run
        assert deleted(rig) == [] and "deletefile" not in rig.fake.rclone_commands()
    assert len(rig.committed_names()) == 3                                         # the new points were made all the same


@MODES
def test_a_fragment_of_an_upload_that_stopped_is_left_by_the_run_and_by_every_run_after_it(tmp_path, full_disk):
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.fail("upload", ("exit", 99), 1)               # a fragment of 10 bytes stays under the partial name, rclone exits 1
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    fragment = rig.fake.remote_files()[PARTIAL]
    assert len(fragment) == 10 and deleted(rig) == []
    assert "NOT removed" in rig.text and "IS stored" not in rig.text
    assert rig.committed_names() == set()

    for status in later_runs(rig):
        assert status == offsite.EXIT_RETENTION, rig.text
        assert f"LEFTOVER PARTIAL {PARTIAL}: left untouched" in rig.text
        assert partials(rig) == {PARTIAL: fragment}
    assert deleted(rig) == [] and len(rig.committed_names()) == 3


@MODES
@pytest.mark.parametrize(
    "name, data",
    [
        (point_name("20200101T000000Z", "00000000") + ".partial", b""),                        # empty, and years old
        (point_name("20990101T000000Z", "ffffffff") + ".partial", b"x" * 100_000),            # large, and dated in the future
        (point_name(FIXED_STAMP, "00000001") + ".partial", b"dated like this run, another token"),
        (point_name(FIXED_STAMP, "0a1b2c3e") + ".partial", FOREIGN),                           # one digit off this run's own
    ],
    ids=["empty and old", "large and future", "dated like this run", "a name like this run's own"],
)
def test_a_partial_of_any_age_size_or_name_survives_the_run_and_the_next_ones(tmp_path, full_disk, name, data):
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.put_remote(name, data)
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert partials(rig) == {name: data}
    for status in later_runs(rig, tokens=("00000001", "00000002")):
        assert status == offsite.EXIT_RETENTION, rig.text
        assert partials(rig) == {name: data}
    assert deleted(rig) == []


# --- an upload that returned: the partial is probably this run's own, and is STILL left ---------------------------------------


@MODES
def test_once_the_upload_has_returned_a_later_failure_leaves_this_runs_partial_untouched_and_the_next_runs_report_it(tmp_path, full_disk):
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.put_remote("notes.partial", b"not in the tool's shape: never touched")
    rig.fake.fail("download", ("exit", 1), 1)              # the upload returned, the read-back fails
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert deleted(rig) == []                               # nothing is deleted, not even this run's own upload
    own = rig.fake.remote_files()[PARTIAL]
    assert len(own) > 100 and rig.fake.remote_files()["notes.partial"] == b"not in the tool's shape: never touched"
    assert f"WARNING: {PARTIAL} was uploaded by this run, which ended before the rename. It is LEFT in the folder untouched" in rig.text
    assert "removed this run's unfinished upload" not in rig.text

    for status in later_runs(rig):
        assert status == offsite.EXIT_RETENTION and f"LEFTOVER PARTIAL {PARTIAL}: left untouched" in rig.text, rig.text
        assert rig.fake.remote_files()[PARTIAL] == own and deleted(rig) == []


@MODES
@pytest.mark.parametrize(
    "kind, nth, where",
    [("lsjson", 2, PARTIAL), ("download", 1, PARTIAL), ("move", 1, FIXED_NAME)],
    ids=["replaced before the listing of the upload", "replaced before the read-back", "replaced before the rename"],
)
def test_a_partial_replaced_after_the_upload_returned_is_not_deleted_by_the_failure_clean_up(tmp_path, full_disk, kind, nth, where):
    """Item 8 of the audit. After this run's upload has returned, somebody replaces the partial with their own object. The run
    fails (the read-back, or the listing, no longer matches what it uploaded). It used to delete the object under the partial's
    name, which was now the foreign one. Before the rename that is the partial; if the replacement comes just before the rename, the
    rename carries the foreign object to the final name and the check after it fails: that object is not deleted either (it has no
    commit record, so it is UNRESOLVED). In no case is a byte of the foreign object touched."""
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.fail(kind, lambda: rig.fake.put_remote(PARTIAL, FOREIGN), nth)
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert rig.fake.remote_files() == {where: FOREIGN}
    assert deleted(rig) == [] and "IS stored" not in rig.text
    assert rig.committed_names() == set() and rig.work_leftovers() == []

    for status in later_runs(rig, tokens=("00000001", "00000002")):
        assert status == offsite.EXIT_RETENTION, rig.text
        assert rig.fake.remote_files()[where] == FOREIGN and deleted(rig) == []
        assert (f"LEFTOVER PARTIAL {PARTIAL}" if where == PARTIAL else f"UNRESOLVED {FIXED_NAME}") in rig.text


# --- retention and the budget are not touched by a partial -----------------------------------------------------------------------


@MODES
def test_a_leftover_partial_neither_holds_retention_back_nor_draws_on_its_budget(tmp_path, full_disk):
    rig = Rig(tmp_path, full_disk=full_disk)
    seed(rig, HISTORY)                                      # Oct 13 .. Oct 24: with Oct 25's point, five are due (Oct 13 .. 17)
    rig.fake.put_remote(PARTIAL.replace("0a1b2c3d", "11111111"), FOREIGN)
    assert rig.run() == offsite.EXIT_RETENTION, rig.text   # exit 6 for the partial alone: the default budget (5) is all for retention
    assert "retention holds back" not in rig.text and "more than --max-deletions" not in rig.text
    assert sorted(n[13:21] for n in deleted_names(rig) if n.endswith(".tar.age")) == ["20261013", "20261014", "20261015", "20261016", "20261017"]
    assert partials(rig) == {PARTIAL.replace("0a1b2c3d", "11111111"): FOREIGN}


@MODES
def test_list_shows_a_leftover_partial_and_changes_nothing(tmp_path, full_disk):
    rig = Rig(tmp_path, full_disk=full_disk)
    seed(rig, HISTORY[-3:])
    rig.fake.put_remote(PARTIAL, FOREIGN)
    rig.fake.put_remote("notes.txt", b"mine")
    before = rig.fake.remote_files()
    code = offsite.main(
        ["list", "--rclone-config", str(rig.config), "--remote", rig.remote,
         *(["--access-mode", offsite.ACCESS_FULL_DISK] if full_disk else [])],
        runner=rig.fake, clock=lambda: rig.now, log=rig.log.append, config_trust=rig.trust,
    )
    assert code == 0
    assert f"LEFTOVER PARTIAL  {PARTIAL}  ({len(FOREIGN)} bytes)" in rig.text and "never deletes one" in rig.text
    assert "1 other object(s) in the folder would be left alone" in rig.text           # notes.txt: the partial is not counted among them
    assert rig.fake.remote_files() == before and rig.fake.rclone_writes() == []


def test_the_operators_quarantine_of_a_partial_ends_the_report_and_keeps_the_bytes(tmp_path):
    """RUNBOOK 15.11: rename the `.partial` out of the tool's shape; the tool then leaves it alone and the exit 6 for it stops."""
    rig = Rig(tmp_path)
    rig.fake.put_remote(PARTIAL.replace("0a1b2c3d", "11111111"), FOREIGN)
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    name = PARTIAL.replace("0a1b2c3d", "11111111")
    quarantine(rig, name)                                    # what the runbook's `rclone moveto` does
    for status in later_runs(rig, tokens=("00000001",)):
        assert status == 0, rig.text
        assert "LEFTOVER" not in rig.text
        assert rig.fake.remote_files()[name + ".quarantined"] == FOREIGN
    assert deleted(rig) == []


# --- the commit record is uploaded by the same method: a skipped record is a failure and the foreign record stays ----------------


@MODES
def test_a_commit_record_name_that_is_taken_is_a_failure_and_the_foreign_object_keeps_its_bytes(tmp_path, full_disk):
    rig = Rig(tmp_path, full_disk=full_disk)
    record_name = FIXED_NAME + offsite.COMMIT_SUFFIX
    foreign = b"somebody else's object that happens to have the record's name"
    rig.fake.fail("record", lambda: rig.fake.put_remote(record_name, foreign))
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert "rclone skipped the upload (exit 9" in rig.text and "is NOT a recovery point" in rig.text
    files = rig.fake.remote_files()
    assert files[record_name] == foreign and FIXED_NAME in files          # the candidate stays, untouched, and is not a point
    assert deleted(rig) == []
    assert rig.committed_names() == set()
