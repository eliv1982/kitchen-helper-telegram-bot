"""The closing listing (deploy/offsite_backup.py, Job.closing_listing; RUNBOOK 15.7 and 15.11).

The blocker of the last Codex review: a foreign `.partial` created AFTER the first listing of the folder, while the run was in
the middle of the rename (or of any later step), was in no listing the run looked at, so a run that committed its point ended
with exit 0 and, on a monitored host, sent its success heartbeat over a folder that held an object nobody had looked at.

The rule held here:

  * the LAST remote operation of a run that committed a point is a fresh listing of the folder, taken after the commit and after
    the retention; the status is decided from it for partials;
  * every `.partial` of the tool's shape in it is logged (`LEFTOVER PARTIAL`, with its size and MD5) and makes the status 6, whether
    it was there at the start or appeared since;
  * a closing listing that cannot be taken (rclone fails, garbage, an unexpected error) makes the status 6 as well, never 0;
  * nothing is deleted because of it (no `.partial` is ever deleted), and the committed point stays committed;
  * only exit 0 is a success heartbeat (RUNBOOK 15.10), so a degraded run sends none.

What this is NOT: a guarantee against a writer that comes after the closing listing. It is an observation at one point in time.
"""

import pytest

from tests.offsite_support import FIXED_NAME, FIXED_STAMP, Rig, offsite, point_name
from tests.test_deploy_kit import DEPLOY, RUNBOOK, read
from tests.test_deploy_offsite import seed
from tests.test_deploy_offsite_ownership import FOREIGN, MODES, PARTIAL, deleted, later_runs, partials
from tests.test_deploy_offsite_protocol import HISTORY, deleted_names

LATE = point_name(FIXED_STAMP, "0badf00d") + ".partial"     # not this run's name: somebody else's object
LATE_BYTES = b"a foreign object that appeared while the run was renaming"
SUCCESS_LINE = "points kept"                                  # only the log of a run that is healthy says this


def inject(rig, name=LATE, data=LATE_BYTES):
    return lambda: rig.fake.put_remote(name, data)


def assert_committed_and_degraded(rig, status, name=LATE, data=LATE_BYTES):
    """The shape of every correct outcome below: the point IS committed, the partial IS reported, the status is 6, and no success."""
    assert status == offsite.EXIT_RETENTION, rig.text                           # 6, not 0: the status a monitor treats as no heartbeat
    assert status != offsite.EXIT_OK
    assert rig.committed_names() >= {FIXED_NAME}                                # committed-point semantics: object + valid record
    assert FIXED_NAME + offsite.COMMIT_SUFFIX in rig.fake.remote_files()
    assert f"LEFTOVER PARTIAL {name}: left untouched" in rig.text               # detected and logged
    assert f"1 leftover .partial object(s) are in the folder ({name})" in rig.text
    assert "IS stored and verified" in rig.text                                  # the point is reported as stored, with the problem beside it
    assert SUCCESS_LINE not in rig.text and "closing listing showed no leftover" not in rig.text   # the healthy log line was NOT written
    assert partials(rig).get(name) == data and deleted(rig) == []               # untouched, byte for byte; no deletefile at all


# --- the regression: a partial that appears during the rename -----------------------------------------------------------------


@MODES
def test_a_partial_that_appears_during_the_rename_is_found_by_the_closing_listing(tmp_path, full_disk):
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.fail("move", inject(rig))                                          # created at the start of `moveto`: after every listing so far
    assert_committed_and_degraded(rig, rig.run())

    # it really was invisible to the earlier steps: no LEFTOVER line before the commit, one after it
    committed_at = next(i for i, line in enumerate(rig.log) if line.startswith(f"committed {FIXED_NAME}"))
    leftovers = [i for i, line in enumerate(rig.log) if "LEFTOVER PARTIAL" in line]
    assert leftovers and min(leftovers) > committed_at
    assert "NOT in the listing taken before this run's upload" in rig.text and f"{len(LATE_BYTES)} bytes" in rig.text


@MODES
def test_the_late_partial_keeps_every_later_run_at_exit_6_until_an_operator_has_dealt_with_it(tmp_path, full_disk):
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.fail("move", inject(rig))
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    for status in later_runs(rig):
        assert status == offsite.EXIT_RETENTION and f"LEFTOVER PARTIAL {LATE}" in rig.text
        assert "listed before this run's upload too" in rig.text                  # now the first listing shows it as well
        assert partials(rig) == {LATE: LATE_BYTES} and deleted(rig) == []
    assert len(rig.committed_names()) == 4


@MODES
@pytest.mark.parametrize("kind", ["mkdir", "upload", "download", "move", "record", "cat", "delete"])
def test_a_partial_created_at_the_start_of_any_remote_command_of_the_run_is_found(tmp_path, full_disk, kind):
    """Every command of the run in turn is the moment the writer comes in. History is seeded so that `cat` (the read of a record)
    and `deletefile` happen too. The closing listing is what makes the later ones visible; the first listing, the early ones."""
    rig = Rig(tmp_path, full_disk=full_disk)
    seed(rig, HISTORY)
    rig.fake.fail(kind, inject(rig))
    status = rig.run()
    assert status == offsite.EXIT_RETENTION, rig.text
    assert f"LEFTOVER PARTIAL {LATE}: left untouched" in rig.text and "IS stored and verified" in rig.text
    assert partials(rig) == {LATE: LATE_BYTES}
    assert FIXED_NAME in rig.committed_names() and SUCCESS_LINE not in rig.text
    assert all(not n.endswith(".partial") for n in deleted_names(rig))           # retention ran as planned, and touched no partial


@MODES
def test_a_partial_that_appears_after_the_listing_that_confirmed_the_point_is_found_only_because_of_the_closing_listing(tmp_path, full_disk):
    """The first `cat` of a run is the read of the new point's record by the final check: it happens right AFTER the listing
    before retention and BEFORE anything else. Without a listing taken after it, nothing would look at the folder again."""
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.fail("cat", inject(rig), 1)
    assert_committed_and_degraded(rig, rig.run())
    assert rig.fake.rclone_commands()[-1] == "lsjson"                            # the last remote command is the closing listing
    assert rig.fake.rclone_commands().count("lsjson") == 5                       # before upload, upload, rename, final check, closing


@MODES
def test_the_last_remote_command_of_a_healthy_run_is_the_closing_listing_and_the_run_says_so(tmp_path, full_disk):
    rig = Rig(tmp_path, full_disk=full_disk)
    seed(rig, HISTORY)
    assert rig.run() == offsite.EXIT_OK, rig.text
    commands = rig.fake.rclone_commands()
    assert commands[-1] == "lsjson"
    last_write = max(i for i, c in enumerate(commands) if c in ("mkdir", "copyto", "moveto", "deletefile"))
    assert last_write < len(commands) - 1                                        # a listing after the last thing the run changed
    assert "closing listing showed no leftover .partial" in rig.text and SUCCESS_LINE in rig.text


# --- a closing listing that cannot be taken is never a healthy run -------------------------------------------------------------


def closing_index(tmp_path, full_disk, history=None):
    """The number of the closing listing among the `lsjson` calls of a run that commits: the last one of an identical clean run."""
    (tmp_path / "baseline").mkdir()
    baseline = Rig(tmp_path / "baseline", full_disk=full_disk)
    if history:
        seed(baseline, history)
    assert baseline.run() == offsite.EXIT_OK, baseline.text
    assert baseline.fake.rclone_commands()[-1] == "lsjson"
    return baseline.fake.counts["lsjson"]


@MODES
@pytest.mark.parametrize("history", [None, HISTORY], ids=["no history", "retention deletes"])
@pytest.mark.parametrize("how", [("exit", 1), ("exit", 3), "garbage"], ids=["rclone fails", "folder reported missing", "not JSON"])
def test_a_failed_closing_listing_ends_the_run_at_6_and_never_at_0(tmp_path, full_disk, history, how):
    n = closing_index(tmp_path, full_disk, history)
    rig = Rig(tmp_path, full_disk=full_disk)
    if history:
        seed(rig, history)
    rig.fake.fail("lsjson", how, n)
    status = rig.run()
    assert status == offsite.EXIT_RETENTION, rig.text
    assert "the closing listing of the folder could not be taken" in rig.text and "is NOT known" in rig.text
    assert "IS stored and verified" in rig.text and SUCCESS_LINE not in rig.text and "closing listing showed no leftover" not in rig.text
    assert FIXED_NAME in rig.committed_names()                                   # the point is a point all the same
    assert rig.fake.counts["lsjson"] == n                                        # it was the last command, and it was tried once
    assert not any(n_.endswith(".partial") for n_ in deleted_names(rig))         # only retention deletes, and never a partial


@MODES
def test_an_unexpected_error_of_the_closing_listing_is_also_6_and_names_only_its_class(tmp_path, full_disk):
    n = closing_index(tmp_path, full_disk)
    rig = Rig(tmp_path, full_disk=full_disk)

    def explode():
        raise RuntimeError("secret detail: /root/private/path")

    rig.fake.fail("lsjson", explode, n)
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert "unexpected RuntimeError" in rig.text and "secret detail" not in rig.text and "/root/private" not in rig.text
    assert SUCCESS_LINE not in rig.text and FIXED_NAME in rig.committed_names()


@MODES
def test_a_failed_closing_listing_still_reports_the_partials_the_first_listing_showed(tmp_path, full_disk):
    early = point_name("20200101T000000Z", "00000000") + ".partial"
    n = closing_index(tmp_path, full_disk)
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.put_remote(early, FOREIGN)
    rig.fake.fail("lsjson", ("exit", 1), n)
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert f"1 leftover .partial object(s) are in the folder ({early})" in rig.text           # not known to be gone
    assert "the closing listing of the folder could not be taken" in rig.text
    assert partials(rig) == {early: FOREIGN}


@MODES
def test_the_closing_listing_does_not_replace_the_unconfirmed_status_of_a_point_the_final_check_could_not_confirm(tmp_path, full_disk):
    """Exit 7 is decided by the final check, before any closing listing; the partials of the first listing are still named in it."""
    early = point_name("20200101T000000Z", "00000000") + ".partial"
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.put_remote(early, FOREIGN)
    rig.fake.fail("lsjson", ("exit", 1), 4)                                      # the listing before retention (the final check)
    assert rig.run() == offsite.EXIT_UNCONFIRMED, rig.text
    assert f"Also: 1 leftover .partial object(s) are in the folder ({early})" in rig.text
    assert rig.fake.counts["lsjson"] == 4                                        # no closing listing was tried: the status was already decided


# --- the closing listing is the authority, in both directions ------------------------------------------------------------------


@MODES
def test_a_partial_that_an_operator_removed_during_the_run_is_not_reported_after_it_is_gone(tmp_path, full_disk):
    early = point_name("20200101T000000Z", "00000000") + ".partial"
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.put_remote(early, FOREIGN)
    rig.fake.fail("upload", lambda: (rig.fake.folder() / early).unlink(), 1)    # the operator quarantines it while the run uploads
    assert rig.run() == offsite.EXIT_OK, rig.text
    assert f"LEFTOVER PARTIAL {early}: left untouched" in rig.text               # it was reported when it was seen ...
    assert f"is not in the closing listing any more: {early}" in rig.text         # ... and the closing listing says it is gone
    assert SUCCESS_LINE in rig.text


@MODES
def test_only_partials_of_the_tools_shape_count_in_the_closing_listing(tmp_path, full_disk):
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.fail("move", lambda: [rig.fake.put_remote(n, b"x") for n in ("notes.partial", FIXED_NAME + ".partial.quarantined", "readme.txt")])
    assert rig.run() == offsite.EXIT_OK, rig.text
    assert "LEFTOVER PARTIAL" not in rig.text


@MODES
def test_several_late_partials_are_all_named_and_counted(tmp_path, full_disk):
    names = [point_name(FIXED_STAMP, f"0000000{i}") + ".partial" for i in range(1, 4)]
    rig = Rig(tmp_path, full_disk=full_disk)
    rig.fake.fail("move", lambda: [rig.fake.put_remote(n, FOREIGN) for n in names])
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    for name in names:
        assert f"LEFTOVER PARTIAL {name}: left untouched" in rig.text
    assert "3 leftover .partial object(s) are in the folder" in rig.text and partials(rig) == {n: FOREIGN for n in names}
    assert deleted(rig) == []


# --- the source and the RUNBOOK say the same ------------------------------------------------------------------------------------


def test_in_the_source_the_closing_listing_comes_after_retention_and_before_the_status_is_decided():
    source = read(DEPLOY / "offsite_backup.py")
    body = source[source.index("    def retain"):source.index("    def confirm_commit")]
    assert body.index("self.prune(") < body.index("self.closing_listing()") < body.index("raise RetentionIncomplete(")
    assert source.count("self.closing_listing()") == 1 and source.count('"the closing listing of the folder"') == 1
    closing = source[source.index("    def closing_listing"):source.index("    @staticmethod")]
    assert ".delete" not in closing and "deletefile" not in closing                 # it observes; it deletes nothing


def test_the_runbook_describes_the_closing_listing_its_exit_status_and_its_limit():
    text = " ".join(read(RUNBOOK).split())
    for needed in (
        "**The closing listing.**", "as the very last thing it does with the remote", "makes the exit status **6**, **even though the new point was committed",
        "which no earlier listing shows", "never 0, says so, and still names the partials the first listing had shown",
        "It is an observation at one moment, not a guarantee: an object that appears after it is seen by the next run",
        "the **closing listing** that ends every run finds it", "`the closing listing of the folder could not be taken` (exit 6, with `IS stored and verified`)",
        "NOT in the listing taken before this run's upload: it appeared while the run was in progress", "taken and showing no leftover `.partial`",
    ):
        assert needed in text, needed
