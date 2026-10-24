"""The final remediation of the second off-host backup audit (deploy/offsite_backup.py), one section per finding:

  Fix 1  a commit record is bound to the remote object by its size AND its MD5: a listing that gives no MD5
         for the object, or another one, leaves it UNRESOLVED whatever its size. An unresolved object takes
         no retention slot and justifies no deletion; if the point just made cannot be bound, nothing is deleted;
  Fix 2  the end of a run says exactly what was established: exit 0 (committed, final check passed, nothing
         else to look at), exit 6 (committed, final check passed, housekeeping needs attention: "stored and
         verified"), exit 7 (the final check failed or does not show the point as committed: never "stored");
  Fix 3  the strict policy: while ANY unresolved object is in the folder retention drops no committed point
         (new points are still made and verified, the run exits 6 and names the object, stale partial uploads
         and orphaned records are still tidied), and `list` says so.

The external commands are the fakes of tests/offsite_support.py; dbtool is real.
"""

import hashlib
import json
import re
from datetime import timedelta

import pytest

from tests.offsite_support import FIXED_NAME, FIXED_NOW, OFFSITE_PATH, Rig, offsite, point_name
from tests.test_deploy_kit import RUNBOOK, read
from tests.test_deploy_offsite import seed
from tests.test_deploy_offsite_protocol import (
    DATA,
    HISTORY,
    OBJECT,
    RECORD,
    deleted_names,
    digests_of_bytes,
    quarantine,
    run_list,
)

DESCRIBED = digests_of_bytes(DATA)
THE_RECORD = offsite.commit_record(OBJECT, DESCRIBED)
ALTERED = bytes([DATA[0] ^ 1]) + DATA[1:]            # the same size, other bytes
assert len(ALTERED) == len(DATA) and ALTERED != DATA


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def classify_object(entry):
    """The folder of one object and its (valid) record, the listing reporting `entry` for the object."""
    record_entry = offsite.Entry(OBJECT + RECORD, len(THE_RECORD), {})
    return offsite.classify_folder([entry, record_entry], lambda name: THE_RECORD)


def md5_of(data):
    return hashlib.md5(data).hexdigest()


# --- Fix 1: the record is bound to the object by its MD5 --------------------------------------------------------


@pytest.mark.parametrize(
    "hashes",
    [{}, {"md5": ""}, {"sha256": DESCRIBED.sha256}, {"sha1": DESCRIBED.sha1, "sha256": DESCRIBED.sha256}],
    ids=["no hashes", "empty md5", "only a matching sha256", "matching sha1 and sha256, no md5"],
)
def test_same_size_content_whose_listing_has_no_md5_is_unresolved_whether_altered_or_not(hashes):
    for content in (ALTERED, DATA):                     # the tool cannot tell these apart without an MD5: neither is accepted
        folder = classify_object(offsite.Entry(OBJECT, len(content), dict(hashes)))
        assert folder.committed == [] and folder.unresolved == {OBJECT: offsite.NO_MD5}


@pytest.mark.parametrize("listed_md5", [md5_of(ALTERED), "0" * 32, "f" * 32], ids=["md5 of the altered bytes", "zeros", "ones"])
def test_same_size_altered_content_with_an_incorrect_md5_is_unresolved(listed_md5):
    folder = classify_object(offsite.Entry(OBJECT, len(DATA), {"md5": listed_md5}))
    assert folder.committed == [] and folder.unresolved == {OBJECT: offsite.DIFFERS}


def test_correct_size_and_a_matching_md5_is_a_committed_point_however_the_remote_spells_it():
    raw = json.dumps([
        {"Name": OBJECT, "Size": len(DATA), "IsDir": False, "Hashes": {"MD5": DESCRIBED.md5.upper()}},
        {"Name": OBJECT + RECORD, "Size": len(THE_RECORD), "IsDir": False, "Hashes": {}},
    ]).encode()
    folder = offsite.classify_folder(offsite.parse_listing(raw), lambda name: THE_RECORD)
    assert [p.name for p in folder.committed] == [OBJECT] and folder.unresolved == {}


def test_a_matching_md5_does_not_rescue_another_size():
    folder = classify_object(offsite.Entry(OBJECT, len(DATA) + 1, {"md5": DESCRIBED.md5}))
    assert folder.committed == [] and folder.unresolved == {OBJECT: offsite.DIFFERS}


def test_check_entry_requires_the_md5_and_compares_every_other_checksum_the_listing_offers():
    good = offsite.Entry(OBJECT, len(DATA), {"md5": DESCRIBED.md5})
    assert offsite.check_entry(good, DESCRIBED) == ["md5"]
    assert set(offsite.check_entry(offsite.Entry(OBJECT, len(DATA), {"md5": DESCRIBED.md5, "sha1": DESCRIBED.sha1}), DESCRIBED)) == {"md5", "sha1"}
    with pytest.raises(offsite.Unverifiable, match="no MD5"):
        offsite.check_entry(offsite.Entry(OBJECT, len(DATA), {}), DESCRIBED)
    assert issubclass(offsite.Unverifiable, offsite.Failed)                    # it is a failure to every caller that does not tell them apart
    with pytest.raises(offsite.Failed, match="sha1"):
        offsite.check_entry(offsite.Entry(OBJECT, len(DATA), {"md5": DESCRIBED.md5, "sha1": "0" * 40}), DESCRIBED)
    with pytest.raises(offsite.Failed, match="different size"):
        offsite.check_entry(offsite.Entry(OBJECT, len(DATA) + 1, {"md5": DESCRIBED.md5}), DESCRIBED)


def alter_in_place(rig, name):
    path = rig.fake.folder() / name
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])


@pytest.mark.parametrize(
    "how, reason",
    [
        ("no listed hashes, altered bytes", offsite.NO_MD5),
        ("no listed hashes, intact bytes", offsite.NO_MD5),
        ("altered bytes, wrong md5 listed", offsite.DIFFERS),
    ],
)
def test_unresolved_points_take_no_slot_and_all_pruning_of_older_committed_points_is_held_back(rig, how, reason):
    """The audit's scenario. The two newest points of the history cannot be tied to their records. If they counted,
    they would fill two daily places and push Oct 17, a valid point, out of the plan. They must not (the plan is made
    from the committed points alone); and under the strict policy, while they exist, no committed point is dropped."""
    seed(rig, HISTORY)                                                       # Oct 13 .. Oct 24
    unverifiable = [point_name(s) for s in HISTORY[-2:]]                     # Oct 23 and Oct 24
    for name in unverifiable:
        if "no listed hashes" in how:
            rig.fake.hashless.add(name)
        if "altered" in how:
            alter_in_place(rig, name)
    everything = [point_name(s) for s in HISTORY] + [FIXED_NAME]
    plan = lambda names: offsite.plan_retention([offsite.parse_point_name(n) for n in names])
    counted, honest = plan(everything), plan([n for n in everything if n not in unverifiable])
    displaced = {p.name for p in counted.drop} - {p.name for p in honest.drop}
    assert displaced == {point_name(HISTORY[4])}                             # Oct 17: dropped only if the unverifiable ones counted
    before = rig.fake.remote_files()

    assert rig.run() == offsite.EXIT_RETENTION, rig.text                     # the new point is confirmed; the rest needs a look
    for name in unverifiable:
        assert f"UNRESOLVED {name}: {reason}" in rig.text
    held = ", ".join(p.name for p in honest.drop)                            # what the honest plan would drop is exactly what is held back
    assert f"retention holds back {len(honest.drop)} committed point(s) it would drop ({held})" in rig.text
    assert deleted_names(rig) == []
    files = rig.fake.remote_files()
    assert {n: files[n] for n in before} == before                          # every older object and record: same name, same bytes
    assert point_name(HISTORY[4]) in files and point_name(HISTORY[4]) + RECORD in files      # the older valid point is still there
    assert not set(unverifiable) & rig.committed_names() and point_name(HISTORY[4]) in rig.committed_names()


def test_when_the_point_just_made_cannot_be_bound_to_its_record_nothing_is_deleted_although_retention_has_work(rig):
    before = seed(rig, HISTORY)                                              # five points are due to be dropped
    rig.fake.fail("lsjson", "no_hashes", 4)                                  # the final listing carries no MD5 at all
    assert rig.run() == offsite.EXIT_UNCONFIRMED, rig.text
    assert offsite.NO_MD5 in rig.text and "deletefile" not in rig.fake.rclone_commands()
    after = rig.fake.remote_files()
    assert {n: after[n] for n in before} == before and FIXED_NAME in after and FIXED_NAME + RECORD in after

    # A listing that is whole again the next day: the earlier point is a committed one, and retention proceeds as planned.
    rig.fake.faults.clear()
    rig.log.clear()
    rig.now = FIXED_NOW + timedelta(days=1)
    rig.token = "00000001"
    assert rig.run("--max-deletions", "10") == 0, rig.text
    assert FIXED_NAME in rig.committed_names() and "deletefile" in rig.fake.rclone_commands()


def test_a_listing_without_an_md5_right_after_the_rename_writes_no_record_and_deletes_nothing(rig):
    """Not proven damaged, so not deleted; but nothing can be tied to a record, so none is written."""
    before = seed(rig, HISTORY[-3:])
    rig.fake.fail("lsjson", "no_hashes", 3)                                  # "confirming the rename"
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert "nothing can tie the renamed object to a commit record" in rig.text
    files = rig.fake.remote_files()
    assert FIXED_NAME in files and FIXED_NAME + RECORD not in files and {n: files[n] for n in before} == before
    assert "deletefile" not in rig.fake.rclone_commands() and rig.work_leftovers() == []
    assert rig.classify().unresolved == {FIXED_NAME: offsite.NO_RECORD}


def test_the_sha256_read_back_still_decides_when_the_listing_md5_agrees(rig):
    rig.fake.fail("download", "corrupt", 1)
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert "not the file that was uploaded" in rig.text and "move" not in rig.fake.events
    assert rig.fake.remote_files() == {}


def test_list_shows_an_object_without_a_listed_md5_as_unresolved_and_changes_nothing(rig):
    seed(rig, HISTORY[-3:])
    victim = point_name(HISTORY[-2])
    rig.fake.hashless.add(victim)
    before = rig.fake.remote_files()
    assert run_list(rig) == 0
    assert f"UNRESOLVED  {victim}  ({offsite.NO_MD5})" in rig.text and "2 recovery points" in rig.text
    assert rig.fake.remote_files() == before and rig.fake.rclone_writes() == []


# --- Fix 2: what the end of a run says --------------------------------------------------------------------------


def the_commit_record_goes_missing(rig):
    return lambda: (rig.fake.folder() / (FIXED_NAME + RECORD)).unlink()


def the_commit_record_is_replaced_by_garbage_of_the_same_size(rig):
    def damage():
        path = rig.fake.folder() / (FIXED_NAME + RECORD)
        path.write_bytes(b"x" * path.stat().st_size)
    return damage


def the_commit_record_is_cut_short(rig):
    def damage():
        path = rig.fake.folder() / (FIXED_NAME + RECORD)
        path.write_bytes(path.read_bytes()[:30])
    return damage


def the_commit_record_is_emptied(rig):
    return lambda: (rig.fake.folder() / (FIXED_NAME + RECORD)).write_bytes(b"")


def the_commit_record_describes_other_bytes(rig):
    def damage():
        (rig.fake.folder() / (FIXED_NAME + RECORD)).write_bytes(offsite.commit_record(FIXED_NAME, DESCRIBED))
    return damage


def the_object_is_altered_at_the_same_size(rig):
    return lambda: alter_in_place(rig, FIXED_NAME)


def the_object_disappears(rig):
    return lambda: (rig.fake.folder() / FIXED_NAME).unlink()


DAMAGE = [
    (the_commit_record_goes_missing, offsite.NO_RECORD),
    (the_commit_record_is_replaced_by_garbage_of_the_same_size, offsite.BAD_RECORD),
    (the_commit_record_is_cut_short, offsite.BAD_RECORD),
    (the_commit_record_is_emptied, offsite.BAD_RECORD),
    (the_commit_record_describes_other_bytes, offsite.DIFFERS),
    (the_object_is_altered_at_the_same_size, offsite.DIFFERS),
    (the_object_disappears, "it is not in the folder listing"),
]


@pytest.mark.parametrize("damage, words", DAMAGE, ids=[d.__name__ for d, _ in DAMAGE])
def test_damage_between_the_commit_and_the_final_listing_is_not_confirmed_and_never_called_stored(rig, damage, words):
    """The record (or the object) was published and, for the record, read back; then it is gone or wrong before the
    final listing. The run cannot establish that it made a recovery point: its own status, no claim, no deletion."""
    before = seed(rig, HISTORY)
    rig.fake.fail("lsjson", damage(rig), 4)                                  # the 4th listing of a clean run is the final check's
    assert rig.run() == offsite.EXIT_UNCONFIRMED, rig.text
    assert words in rig.text and "NOT reported as stored" in rig.text and "Nothing was deleted" in rig.text
    assert "stored and verified" not in rig.text and f"stored {FIXED_NAME}" not in rig.text
    assert "deletefile" not in rig.fake.rclone_commands()
    files = rig.fake.remote_files()
    assert {n: files[n] for n in before} == before                          # nothing older was touched
    assert rig.work_leftovers() == []


def test_a_missing_record_is_reported_by_the_next_run_as_unresolved_and_that_run_is_exit_6_for_its_own_point(rig):
    seed(rig, HISTORY[-3:])
    rig.fake.fail("lsjson", the_commit_record_goes_missing(rig), 4)
    assert rig.run() == offsite.EXIT_UNCONFIRMED
    rig.fake.faults.clear()
    rig.log.clear()
    rig.token = "00000001"
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert f"UNRESOLVED {FIXED_NAME}: {offsite.NO_RECORD}" in rig.text
    assert "00000001.tar.age IS stored and verified" in rig.text            # THIS run's point was confirmed, and only it


@pytest.mark.parametrize(
    "fault",
    # cat 1 is the run's own read-back; the final check then reads the 13 records oldest first, so cat 14 is the new point's own
    [("lsjson", ("exit", 1), 4), ("lsjson", "garbage", 4), ("cat", ("exit", 1), 2), ("cat", "garbage", 14)],
    ids=["final listing fails", "final listing is garbage", "an older record cannot be read", "the new point's own record reads as garbage"],
)
def test_a_final_check_that_cannot_be_completed_is_not_confirmed_either(rig, fault):
    before = seed(rig, HISTORY)
    rig.fake.fail(*fault)
    assert rig.run() == offsite.EXIT_UNCONFIRMED, rig.text
    assert "NOT reported as stored" in rig.text and "stored and verified" not in rig.text
    assert "deletefile" not in rig.fake.rclone_commands()
    files = rig.fake.remote_files()
    assert {n: files[n] for n in before} == before and FIXED_NAME in files


def test_what_else_was_wrong_is_not_lost_when_the_final_check_fails(rig):
    stale = point_name("20261020T041500Z", "11111111") + ".partial"
    rig.fake.put_remote(stale, b"half")
    rig.fake.fail("delete", "noop", 0)                                       # the leftover cannot be removed
    rig.fake.fail("lsjson", the_commit_record_goes_missing(rig), 5)          # one more listing: the one that confirms the delete
    assert rig.run() == offsite.EXIT_UNCONFIRMED, rig.text
    assert f"Also: could not remove the leftover partial upload {stale}" in rig.text


def maintenance_succeeds(rig):
    seed(rig, HISTORY[-3:])


def maintenance_fails(rig):
    seed(rig, HISTORY)
    rig.fake.fail("delete", ("exit", 1), 1)


def final_check_fails(rig):
    seed(rig, HISTORY)
    rig.fake.fail("lsjson", the_commit_record_goes_missing(rig), 4)


@pytest.mark.parametrize(
    "setup, code, said, not_said",
    [
        (maintenance_succeeds, offsite.EXIT_OK, [f"stored {FIXED_NAME} (the final check matched its size and MD5"], ["IS stored", "NOT reported"]),
        (maintenance_fails, offsite.EXIT_RETENTION, [f"{FIXED_NAME} IS stored and verified", "SHA-256 was checked by the read-back",
                                                       "matched its size and MD5 to its commit record"], ["NOT reported"]),
        (final_check_fails, offsite.EXIT_UNCONFIRMED, ["NOT reported as stored"], ["stored and verified", f"stored {FIXED_NAME} ("]),
    ],
    ids=["committed, maintenance successful", "committed, maintenance failed", "final check failed"],
)
def test_the_three_outcomes_have_three_statuses_and_each_says_only_what_was_established(rig, setup, code, said, not_said):
    setup(rig)
    assert rig.run() == code, rig.text
    for words in said:
        assert words in rig.text, words
    for words in not_said:
        assert words not in rig.text, words
    # Nothing after the final check claims more than size and MD5: the SHA-256 is said to be checked before the commit only.
    assert "re-verified" not in rig.text and "reverified" not in rig.text


def test_the_exit_statuses_are_distinct_and_the_unconfirmed_one_is_a_failure_of_its_own():
    codes = [offsite.EXIT_OK, offsite.EXIT_FAILED, offsite.EXIT_USAGE, offsite.EXIT_REFUSED, offsite.EXIT_RETENTION, offsite.EXIT_UNCONFIRMED]
    assert len(set(codes)) == len(codes) and offsite.EXIT_UNCONFIRMED not in (0, 1, 2, 3, 6) and offsite.EXIT_UNCONFIRMED < 128
    assert offsite.Unconfirmed.exit_code == offsite.EXIT_UNCONFIRMED and not issubclass(offsite.Unconfirmed, offsite.Failed)
    assert offsite.RetentionIncomplete.exit_code == offsite.EXIT_RETENTION


def test_only_the_confirmed_path_can_say_stored_and_verified():
    source = read(OFFSITE_PATH)
    assert source.count("IS stored") == 1                                  # RetentionIncomplete's message, after the final check passed
    body = source[source.index("    def confirm_commit"):source.index("    def report_unresolved")]
    assert "stored and verified" not in body.replace("NOT reported as stored", "")
    assert source.index("folder = self.confirm_commit()") < source.index("IS stored")


def test_the_runbook_states_the_exit_statuses_the_code_uses_and_the_final_check():
    text = " ".join(read(RUNBOOK).split())
    start = text.index("Exit status: **0**")
    status = text[start:text.index("| What you see |", start)]
    for code in (offsite.EXIT_OK, offsite.EXIT_FAILED, offsite.EXIT_USAGE, offsite.EXIT_REFUSED, offsite.EXIT_RETENTION, offsite.EXIT_UNCONFIRMED):
        assert f"**{code}**" in status, code
    assert re.search(r"\*\*7\*\* .*not confirmed.*never described as stored", status, re.IGNORECASE)
    assert "**6** the new point **is stored and verified**" in status and "final check" in status
    section_15 = text[text.index("## 15. Encrypted off-host backup"):]
    for needed in (
        "whose size and MD5 are the size and MD5 the listing reports for it", "gives no MD5", "no retention slot",
        "does not re-read points after they were committed", "the final check of the folder did not confirm", "`exit 7 FAILED",
        "ends with **exit 7**", "no later run reads the object again",
    ):
        assert needed in section_15, needed


# --- Fix 3: an unresolved object stops all pruning of committed points ----------------------------------------------


PLANTED = point_name("20261001T041500Z", "deadbeef")            # final-looking, older than every point, no commit record


def plant_unresolved(rig):
    rig.fake.put_remote(PLANTED, b"looks like a point, has no record")


def test_an_unresolved_object_leaves_every_older_committed_point_and_record_untouched(rig):
    """The regression for the strict policy. Five points are due to be dropped (Oct 13 .. Oct 17); an unresolved object
    is in the folder; so none is: not one delete of a point or of a record, and every older object keeps its bytes."""
    seed(rig, HISTORY)
    plant_unresolved(rig)
    before = rig.fake.remote_files()
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert f"UNRESOLVED {PLANTED}: {offsite.NO_RECORD}" in rig.text
    assert "retention holds back 5 committed point(s)" in rig.text and "no committed point is dropped" in rig.text
    assert deleted_names(rig) == [] and "deletefile" not in rig.fake.rclone_commands()
    files = rig.fake.remote_files()
    assert {n: files[n] for n in before} == before                          # the unresolved object too: neither deleted nor trusted
    assert [point_name(s) for s in HISTORY] == sorted(n for n in rig.committed_names() if n != FIXED_NAME)
    assert PLANTED not in rig.committed_names()
    # the new point is still made, verified and confirmed
    assert FIXED_NAME in rig.committed_names() and f"{FIXED_NAME} IS stored and verified" in rig.text


def test_an_unresolved_object_does_not_stop_the_tidying_of_leftover_partials_and_orphaned_records(rig):
    seed(rig, HISTORY)
    plant_unresolved(rig)
    stale = point_name("20261020T041500Z", "11111111") + ".partial"
    stray = point_name("20260101T041500Z", "12345678") + RECORD
    rig.fake.put_remote(stale, b"half")
    rig.fake.put_remote(stray, offsite.commit_record(stray[: -len(RECORD)], DESCRIBED))
    before = rig.fake.remote_files()
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert sorted(deleted_names(rig)) == sorted([stale, stray])             # these two, and nothing else
    files = rig.fake.remote_files()
    assert stale not in files and stray not in files and f"deleted {stray}: an orphaned commit record" in rig.text
    assert {n: files[n] for n in before if n not in (stale, stray)} == {n: b for n, b in before.items() if n not in (stale, stray)}
    assert PLANTED in files and "retention holds back 5 committed point(s)" in rig.text


def test_the_hold_ends_when_the_operator_has_dealt_with_the_object_and_the_next_run_prunes_as_planned(rig):
    seed(rig, HISTORY)
    plant_unresolved(rig)
    assert rig.run() == offsite.EXIT_RETENTION and deleted_names(rig) == []
    quarantine(rig, PLANTED)                                                # RUNBOOK 15.11: rename out of the tool's shape
    rig.fake.rclone_calls.clear()
    rig.log.clear()
    rig.now = FIXED_NOW + timedelta(days=1)
    rig.token = "00000001"
    assert rig.run("--max-deletions", "10") == 0, rig.text                  # the held-back five, and Oct 19, which the next day's point displaces
    assert "holds back" not in rig.text
    assert sorted(n[13:21] for n in deleted_names(rig) if n.endswith(".tar.age")) == [
        "20261013", "20261014", "20261015", "20261016", "20261017", "20261019",
    ]


def test_list_says_hold_and_not_drop_while_an_unresolved_object_exists_and_changes_nothing(rig):
    seed(rig, HISTORY)
    plant_unresolved(rig)
    before = rig.fake.remote_files()
    assert run_list(rig) == 0
    assert "a run now would delete none of them, though retention alone would drop 5" in rig.text
    assert f"hold  {point_name(HISTORY[0])}" in rig.text and "drop  " not in rig.text and "would keep" not in rig.text
    assert f"UNRESOLVED  {PLANTED}  ({offsite.NO_RECORD})" in rig.text
    assert rig.fake.remote_files() == before and rig.fake.rclone_writes() == []


def test_the_runbook_states_the_strict_policy():
    text = " ".join(read(RUNBOOK).split())
    section_15 = text[text.index("## 15. Encrypted off-host backup"):]
    for needed in (
        "retention drops no committed point at all", "The strict rule: no pruning while an unresolved object exists.",
        "`retention holds back N committed point(s)`", "still removes orphaned commit records and leftover `.partial` uploads",
        "`hold` instead of `drop`", "While it is there the tool prunes no committed point",
    ):
        assert needed in section_15, needed
