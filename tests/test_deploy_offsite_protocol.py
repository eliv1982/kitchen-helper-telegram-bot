"""The remediation of the off-host backup audit (deploy/offsite_backup.py), one section per finding:

  P1-1  private-key material is refused wherever it is in the recipients file, commented out or not;
  P1-2  plaintext that cannot be removed is never a success, and stops every later run until it is gone;
  P1-3  a delete is believed only when the folder, listed again, no longer shows the object;
  P1-4  a final-looking object is a recovery point only with a valid commit record that matches it;
        everything else is UNRESOLVED: never counted, never relied on, never deleted by the tool;
  clock an implausible clock stops every deletion by retention and cannot displace a real point;
  budget --max-deletions is ONE budget, spent on retention alone: no .partial is ever deleted, so none draws on it.

The external commands are the fakes of tests/offsite_support.py; dbtool is real.
"""

import hashlib
import json
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.offsite_support import (
    FIXED_NAME,
    FIXED_NOW,
    PRIVATE_KEY_SHAPED,
    PUBLIC_KEY,
    REMOTE,
    Rig,
    offsite,
    point_name,
)
from tests.deploy_support import DEPLOY, posix_only
from tests.test_deploy_offsite import run_refused, seed, stamps

UTC = timezone.utc
RECORD = offsite.COMMIT_SUFFIX
HISTORY = stamps(datetime(2026, 10, 13), 12)   # Oct 13 .. Oct 24: with the point of Oct 25 the plan drops Oct 13 .. 17


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def deleted_names(rig):
    return [c[-1].rsplit("/", 1)[1] for c in rig.fake.rclone_calls if c[0] == "deletefile"]


def run_list(rig):
    return offsite.main(["list", "--rclone-config", str(rig.config), "--remote", REMOTE],
                        runner=rig.fake, clock=lambda: rig.now, log=rig.log.append)


def run_with_clock(rig, *readings):
    """A run whose host clock gives these readings in turn (the start of the run, then the start of retention)."""
    sequence = iter(readings)
    return offsite.main(rig.args(), runner=rig.fake, clock=lambda: next(sequence, readings[-1]), token=lambda: rig.token,
                        deploy_dir=DEPLOY, log=rig.log.append)


def orphan_records(rig):
    files = rig.fake.remote_files()
    return sorted(n for n in files if n.endswith(RECORD) and n[: -len(RECORD)] not in files)


def quarantine(rig, name):
    """What RUNBOOK 15.7 tells the operator to do: rename the object (and its record, if any) out of the tool's shape."""
    for suffix in ("", RECORD):
        path = rig.fake.folder() / (name + suffix)
        if path.exists():
            path.rename(path.with_name(path.name + ".quarantined"))


# --- P1-1: private-key material in the recipients file ---------------------------------------------------------


PRIVATE = PRIVATE_KEY_SHAPED


@pytest.mark.parametrize(
    "content",
    [
        f"{PUBLIC_KEY}\n# {PRIVATE}\n",
        f"{PUBLIC_KEY}\n#{PRIVATE}\n",
        f"{PUBLIC_KEY}\n    {PRIVATE}\n",
        f"{PUBLIC_KEY}\n\t#\t{PRIVATE}\n",
        f"{PUBLIC_KEY} # {PRIVATE}\n",
        f"{PUBLIC_KEY}{PRIVATE}\n",
        f"{PUBLIC_KEY}\nthe old key was {PRIVATE} (rotated)\n",
        f"{PRIVATE.lower()}\n{PUBLIC_KEY}\n",
        f"{PUBLIC_KEY}\r\n# {PRIVATE}\r\n",
        f"# created: 2026-10-24T10:00:00Z\n# public key: {PUBLIC_KEY}\n{PRIVATE}\n",   # the file `age-keygen -o` writes
        f"{PUBLIC_KEY}\n{PRIVATE}",                                                    # no newline at the end
        f"{PUBLIC_KEY}\n##### {PRIVATE} #####\n",
    ],
    ids=["comment", "comment without space", "indented", "tab, comment, tab", "after a public key, as a comment",
         "glued to a public key", "inside a sentence", "lower case", "windows line ends", "age-keygen file",
         "no final newline", "banner"],
)
def test_private_key_material_is_refused_wherever_it_is_in_the_recipients_file_and_never_printed(rig, content):
    rig.recipients.write_bytes(content.encode("ascii"))
    run_refused(rig, "PRIVATE key")
    assert PRIVATE not in rig.text and PRIVATE.lower() not in rig.text and "QQQQ" not in rig.text


def test_a_private_key_beside_a_byte_that_is_not_ascii_is_still_called_a_private_key(rig):
    rig.recipients.write_bytes(b"\xff\xfe # " + PRIVATE.encode("ascii") + b"\n" + PUBLIC_KEY.encode("ascii") + b"\n")
    run_refused(rig, "PRIVATE key")
    assert "QQQQ" not in rig.text


def test_comments_that_only_talk_about_keys_are_not_mistaken_for_one(rig):
    rig.recipients.write_text(f"# the secret key stays on the laptop; this file lists the public key only\n{PUBLIC_KEY}\n")
    assert rig.run() == 0, rig.text


def test_the_private_key_check_looks_at_the_raw_file_before_any_parsing():
    source = (DEPLOY / "offsite_backup.py").read_text()
    body = source[source.index("def read_recipients"):source.index("def check_rclone_config")]
    assert body.index("PRIVATE_KEY_MARKER in content.upper()") < body.index("splitlines()") < body.index('startswith("#")')


# --- P1-2: plaintext that cannot be removed ---------------------------------------------------------------------


@pytest.fixture
def stuck(monkeypatch):
    """The removal of any work directory of the tool fails silently (as an `ignore_errors` removal would) until `release()`."""
    real = offsite.shutil.rmtree
    state = {"fail": True}

    def rmtree(path, *args, **kwargs):
        if state["fail"] and Path(path).name.startswith(offsite.RUN_PREFIX):
            return None
        return real(path, *args, **kwargs)

    monkeypatch.setattr(offsite.shutil, "rmtree", rmtree)

    class Control:
        def release(self):
            state["fail"] = False

    return Control()


def leftover_dirs(rig):
    return [n for n in rig.work_leftovers() if n.startswith(offsite.RUN_PREFIX)]


def test_plaintext_that_cannot_be_removed_ends_a_committed_run_with_status_6_and_refuses_every_later_run_until_it_is_gone(rig, stuck):
    # run 1: the point is committed, the clean-up fails: partial success, never 0
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert "WARNING: could not remove the work directory" in rig.text and "IS stored and verified" in rig.text
    assert rig.committed_names() == {FIXED_NAME}
    first = leftover_dirs(rig)
    assert len(first) == 1

    # runs 2 and 3: refused before anything is done; no new directory, no new plaintext, nothing written
    for token in ("00000001", "00000002"):
        rig.log.clear()
        rig.fake.events.clear()
        writes = len(rig.fake.rclone_writes())
        rig.token = token
        assert rig.run() == offsite.EXIT_REFUSED, rig.text
        assert first[0] in rig.text and "no new run starts" in rig.text and "Nothing was done" in rig.text
        assert rig.fake.events == [] and len(rig.fake.rclone_writes()) == writes
        assert leftover_dirs(rig) == first

    # recovery: once it can be removed the next run sweeps it and goes through
    stuck.release()
    rig.log.clear()
    rig.token = "00000003"
    assert rig.run() == 0, rig.text
    assert "removed the leftover work directory" in rig.text and leftover_dirs(rig) == []


@pytest.mark.parametrize(
    "fault, status",
    [(("age", ("exit", 1), 1), offsite.EXIT_FAILED), (("sanitize", ("exit", 4), 1), offsite.EXIT_FAILED),
     (("age", "signal", 1), 128 + signal.SIGTERM)],
    ids=["age fails", "sanitize fails", "signal"],
)
def test_plaintext_that_cannot_be_removed_after_a_failure_keeps_the_original_failure_status(rig, stuck, fault, status):
    rig.fake.fail(*fault)
    assert rig.run() == status, rig.text      # not 0, and not rewritten as 6: nothing was committed
    assert "WARNING: could not remove the work directory" in rig.text
    assert rig.committed_names() == set() and len(leftover_dirs(rig)) == 1
    rig.log.clear()
    rig.fake.faults.clear()
    assert rig.run() == offsite.EXIT_REFUSED       # and the next run does not pile more plaintext next to it


def test_the_warnings_name_the_directory_but_never_its_contents(rig, stuck):
    secret = "SYNTHETIC-PLAINTEXT-THAT-MUST-NOT-BE-LOGGED"
    old = rig.work / (offsite.RUN_PREFIX + "killed")
    (old / "pair").mkdir(parents=True)
    (old / "pair" / "leftover.db").write_text(secret)
    assert rig.run() == offsite.EXIT_REFUSED
    assert old.name in rig.text and secret not in rig.text and "leftover.db" not in rig.text
    assert rig.fake.events == [] and rig.fake.rclone_writes() == []
    assert (old / "pair" / "leftover.db").read_text() == secret       # the refusal removed and changed nothing
    stuck.release()
    rig.log.clear()
    assert rig.run() == 0 and not old.exists()


@posix_only
def test_a_leftover_directory_of_another_user_is_not_touched_and_stops_the_run(rig, monkeypatch):
    foreign = rig.work / (offsite.RUN_PREFIX + "other-user")
    foreign.mkdir()
    real_getuid = os.getuid
    monkeypatch.setattr(os, "getuid", lambda: real_getuid() + 1)      # this process is "not the owner"
    assert rig.run() == offsite.EXIT_REFUSED and foreign.exists()
    assert "other-user" in rig.text and rig.fake.events == []


# --- P1-3: a delete is believed only when the folder no longer shows the object ----------------------------------


def test_a_retention_delete_that_exits_zero_but_keeps_the_object_is_not_a_prune(rig):
    seed(rig, HISTORY)
    rig.fake.fail("delete", "noop", 0)        # every delete answers "done" and does nothing
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert "not confirmed deleted" in rig.text and "IS stored and verified" in rig.text
    assert "deleted house-helper" not in rig.text           # nothing is reported as deleted
    assert rig.committed_names() >= {point_name(s) for s in HISTORY} | {FIXED_NAME}
    assert len(rig.fake.point_objects()) == 13
    # the records of points that are still there were not deleted either: every point is whole
    assert all((rig.fake.folder() / (n + RECORD)).exists() for n in rig.fake.point_objects())
    assert deleted_names(rig) == [point_name(s) for s in HISTORY[:5]]       # one attempt each, objects only


def test_the_same_retention_succeeds_on_the_next_run_once_deletes_work(rig):
    seed(rig, HISTORY)
    rig.fake.fail("delete", "noop", 0)
    assert rig.run() == offsite.EXIT_RETENTION
    rig.fake.faults.clear()
    rig.log.clear()
    rig.token = "00000001"
    assert rig.run("--max-deletions", "10") == 0, rig.text
    # kept: 7 days (the point of this run is the older of Oct 25, so it is kept as "protected"), Oct 18 weekly
    assert len(rig.fake.point_objects()) == len(rig.committed_names()) == 9
    assert orphan_records(rig) == []


def test_a_delete_that_reports_failure_but_removed_the_object_is_a_success_because_the_listing_says_so(rig):
    seed(rig, stamps(datetime(2026, 10, 17), 8))    # Oct 17 .. 24: exactly one point (Oct 17) is dropped
    rig.fake.fail("delete", "lost", 1)             # the first delete removes the object and answers "failed"
    assert rig.run() == 0, rig.text
    assert point_name("20261017T041500Z") not in rig.fake.remote_files()
    assert point_name("20261017T041500Z") + RECORD not in rig.fake.remote_files()


def test_when_the_listing_after_the_deletes_fails_nothing_is_called_deleted_and_no_record_is_removed(rig):
    seed(rig, HISTORY)
    rig.fake.fail("lsjson", ("exit", 1), 5)         # the listing that confirms the deletes of the objects
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert "could not be confirmed" in rig.text and "deleted house-helper" not in rig.text
    assert [n for n in deleted_names(rig) if n.endswith(RECORD)] == []        # records wait for proof that their object is gone
    # the objects ARE gone on this fake, so the records are orphans; the next run finishes the job, within its budget
    assert len(orphan_records(rig)) == 5
    rig.fake.faults.clear()
    rig.log.clear()
    rig.token = "00000001"
    assert rig.run("--max-deletions", "10") == 0, rig.text
    assert orphan_records(rig) == []


def test_an_interrupted_deletion_leaves_an_orphaned_record_never_an_object_without_one_and_the_next_run_finishes_it(rig):
    seed(rig, stamps(datetime(2026, 10, 17), 8))    # one point is dropped
    rig.fake.fail("delete", ("exit", 1), 2)         # the object goes, then the delete of its record fails
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    oldest = point_name("20261017T041500Z")
    assert oldest not in rig.fake.remote_files() and oldest + RECORD in rig.fake.remote_files()
    assert rig.classify().unresolved == {} and rig.classify().stray_records == [oldest + RECORD]
    rig.fake.faults.clear()
    rig.log.clear()
    rig.now = FIXED_NOW + timedelta(days=1)
    rig.token = "00000001"
    assert rig.run() == 0, rig.text
    assert oldest + RECORD not in rig.fake.remote_files() and "an orphaned commit record" in rig.text


def test_delete_confirmed_asks_for_nothing_when_there_is_nothing_to_delete(rig):
    remote = offsite.Remote(REMOTE, rig.config, rig.fake, "/fake/bin/rclone")
    result = remote.delete_confirmed([])
    assert (result.gone, result.left, result.after) == ([], [], None) and rig.fake.calls == []


# --- P1-4: the commit record -------------------------------------------------------------------------------------


def make_entry(name, data):
    return offsite.Entry(name, len(data), {"md5": hashlib.md5(data).hexdigest()})


def digests_of_bytes(data):
    return offsite.Digests(len(data), hashlib.sha256(data).hexdigest(), hashlib.sha1(data).hexdigest(), hashlib.md5(data).hexdigest())


OBJECT = point_name("20261024T041500Z", "aaaaaaaa")
OTHER = point_name("20261023T041500Z", "bbbbbbbb")
DATA = b"an encrypted object"


def folder_of(entries, records):
    reads = []

    def read(name):
        reads.append(name)
        return records[name]

    return offsite.classify_folder(entries, read), reads


def test_the_commit_record_is_canonical_describes_one_object_and_holds_no_secret():
    record = offsite.commit_record(OBJECT, digests_of_bytes(DATA))
    assert record.endswith(b"\n") and len(record) < offsite.MAX_RECORD_BYTES
    record.decode("ascii")
    assert set(json.loads(record)) == {"format", "name", "bytes", "sha256", "sha1", "md5"}
    assert offsite.read_commit_record(record, OBJECT) == digests_of_bytes(DATA)
    assert offsite.read_commit_record(record, OTHER) is None            # a record names its own object only
    for damaged in (record[:-5], record[: len(record) // 2], record + b" ", record.replace(b"{", b"{ ", 1), b"", b"[]",
                    b"null", b'{"bytes": 1}', record.upper()):
        assert offsite.read_commit_record(damaged, OBJECT) is None, damaged
    # A well-formed record of ANOTHER size parses (it is a record, just not of this object): the listing then disagrees.
    other_size = record.replace(b'"bytes": 19', b'"bytes": 20')
    assert offsite.read_commit_record(other_size, OBJECT).size == 20


def test_only_an_object_with_a_matching_valid_record_is_committed_and_every_other_shape_of_name_is_unresolved():
    good_record = offsite.commit_record(OBJECT, digests_of_bytes(DATA))
    other_data = b"another encrypted object"
    other_record = offsite.commit_record(OTHER, digests_of_bytes(other_data))
    junk = point_name("20261022T041500Z", "cccccccc")
    no_record = point_name("20261021T041500Z", "dddddddd")
    changed = point_name("20261020T041500Z", "eeeeeeee")
    wrong_name = point_name("20261019T041500Z", "ffffffff")
    empty = point_name("20261018T041500Z", "11111111")
    oversize = point_name("20261017T041500Z", "22222222")
    entries = [
        make_entry(OBJECT, DATA), make_entry(OBJECT + RECORD, good_record),
        make_entry(OTHER, other_data), make_entry(OTHER + RECORD, other_record),
        make_entry(junk, DATA), make_entry(junk + RECORD, b"not a record at all"),
        make_entry(no_record, DATA),
        make_entry(changed, b"changed after the commit"), make_entry(changed + RECORD, offsite.commit_record(changed, digests_of_bytes(DATA))),
        make_entry(wrong_name, DATA), make_entry(wrong_name + RECORD, good_record),            # somebody else's record
        make_entry(empty, b""), make_entry(empty + RECORD, offsite.commit_record(empty, digests_of_bytes(DATA))),
        make_entry(oversize, DATA), offsite.Entry(oversize + RECORD, 5000, {}),
        make_entry(point_name("20261016T041500Z", "33333333") + RECORD, good_record),           # a record without its object
        make_entry("notes.txt", b"mine"), make_entry(OBJECT + ".partial", b"half"),
    ]
    records = {OBJECT: good_record, OTHER: other_record, junk: b"not a record at all",
               changed: offsite.commit_record(changed, digests_of_bytes(DATA)), wrong_name: good_record,
               empty: offsite.commit_record(empty, digests_of_bytes(DATA))}
    folder, reads = folder_of(entries, records)
    assert sorted(p.name for p in folder.committed) == sorted([OBJECT, OTHER])
    assert folder.unresolved == {
        junk: offsite.BAD_RECORD, no_record: offsite.NO_RECORD, changed: offsite.DIFFERS, wrong_name: offsite.BAD_RECORD,
        empty: offsite.EMPTY_OBJECT, oversize: offsite.BAD_RECORD,
    }
    assert folder.stray_records == [point_name("20261016T041500Z", "33333333") + RECORD]
    assert folder.others == sorted(["notes.txt", OBJECT + ".partial"])
    assert oversize not in reads and no_record not in reads and empty not in reads      # nothing is read that could not be a record


def test_a_record_does_not_vouch_for_an_object_whose_listed_checksum_differs_even_at_the_same_size():
    flipped = bytes([DATA[0] ^ 1]) + DATA[1:]
    entries = [make_entry(OBJECT, flipped), make_entry(OBJECT + RECORD, offsite.commit_record(OBJECT, digests_of_bytes(DATA)))]
    folder, _ = folder_of(entries, {OBJECT: offsite.commit_record(OBJECT, digests_of_bytes(DATA))})
    assert folder.committed == [] and folder.unresolved == {OBJECT: offsite.DIFFERS}


def test_a_record_that_cannot_be_read_decides_nothing_and_raises():
    def unreadable(name):
        raise offsite.Failed("rclone cat failed (exit 1) while reading a commit record.")

    entries = [make_entry(OBJECT, DATA), make_entry(OBJECT + RECORD, offsite.commit_record(OBJECT, digests_of_bytes(DATA)))]
    with pytest.raises(offsite.Failed, match="rclone cat failed"):
        offsite.classify_folder(entries, unreadable)


def test_the_shape_of_a_name_alone_is_never_a_recovery_point_in_a_run(rig):
    planted = point_name("20261024T041500Z", "deadbeef")
    rig.fake.put_remote(planted, b"looks like a point, is not one")
    seed(rig, HISTORY)
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert f"UNRESOLVED {planted}: {offsite.NO_RECORD}" in rig.text
    assert planted in rig.fake.remote_files() and planted not in rig.committed_names()
    assert planted not in deleted_names(rig)


def test_a_good_run_writes_its_record_after_the_rename_and_reads_it_back(rig):
    assert rig.run() == 0, rig.text
    assert rig.fake.events[-4:] == ["move", "record", "cat", "cat"]      # rename, record, its read-back, retention's own read
    record = (rig.fake.folder() / (FIXED_NAME + RECORD)).read_bytes()
    assert record == offsite.commit_record(FIXED_NAME, digests_of_bytes((rig.fake.folder() / FIXED_NAME).read_bytes()))
    assert f"committed {FIXED_NAME}" in rig.text


def test_post_rename_metadata_mismatch_leaves_an_object_no_later_run_counts_relies_on_or_deletes(rig):
    """The audit's reproduction: the listing right after the rename disagrees with the file that was read back. The
    object keeps its final-looking name and is NOT deleted (the rename may have damaged it, or another object may have
    been renamed instead: nothing tells the two apart). It must never become a recovery point."""
    before = seed(rig, HISTORY)
    rig.fake.fail("lsjson", "lie_size_final", 3)
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert "WARNING" in rig.text and "NOT deleted" in rig.text and "no commit record" in rig.text
    assert deleted_names(rig) == []
    files = rig.fake.remote_files()
    assert FIXED_NAME in files and FIXED_NAME + RECORD not in files and {n: files[n] for n in before} == before
    assert rig.classify().unresolved == {FIXED_NAME: offsite.NO_RECORD} and FIXED_NAME not in rig.committed_names()
    damaged = files[FIXED_NAME]

    # The next run: its own point is committed, the damaged object is reported and left alone, and retention works
    # out the same plan as if the object did not exist (if it were counted, Oct 19 would be in the plan's drop too)
    # but applies none of it: no committed point is dropped while an unresolved object exists.
    rig.fake.faults.clear()
    rig.log.clear()
    rig.now = FIXED_NOW + timedelta(days=1)
    rig.token = "00000001"
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert f"UNRESOLVED {FIXED_NAME}" in rig.text and "IS stored and verified" in rig.text
    held = ", ".join(point_name(s) for s in HISTORY[:5])                       # Oct 13 .. Oct 17
    assert f"retention holds back 5 committed point(s) it would drop ({held})" in rig.text
    assert deleted_names(rig) == []
    assert rig.fake.remote_files()[FIXED_NAME] == damaged
    committed = rig.committed_names()
    assert {point_name(s) for s in HISTORY} <= committed and FIXED_NAME not in committed

    # `list` says the same and changes nothing
    rig.log.clear()
    snapshot = rig.fake.remote_files()
    assert run_list(rig) == 0
    assert f"UNRESOLVED  {FIXED_NAME}" in rig.text and "NO valid commit record" in rig.text and rig.fake.remote_files() == snapshot


def test_an_object_the_listing_after_the_rename_does_not_match_is_not_deleted_even_though_a_delete_would_have_worked(rig):
    before = seed(rig, HISTORY[:3])
    rig.fake.fail("lsjson", "lie_size_final", 3)
    assert rig.run() == offsite.EXIT_FAILED
    files = rig.fake.remote_files()
    assert {n: files[n] for n in before} == before and FIXED_NAME in files and FIXED_NAME + RECORD not in files
    assert deleted_names(rig) == [] and "delete" not in rig.fake.events
    assert rig.committed_names() == {n for n in before if offsite.parse_point_name(n)}
    assert f"WARNING: {FIXED_NAME}, listed right after the rename, is not the file this run read back" in rig.text


@pytest.mark.parametrize(
    "fault, words",
    [
        (("record", ("exit", 1), 1), "rclone copyto failed"),
        (("record", "truncate", 1), "not the one that was written"),
        (("record", "corrupt", 1), "not the one that was written"),
        (("cat", "garbage", 1), "not the one that was written"),
        (("cat", ("exit", 1), 1), "rclone cat failed"),
    ],
    ids=["record upload fails", "record cut short", "record damaged", "read-back is garbage", "read-back fails"],
)
def test_a_commit_record_that_is_not_confirmed_fails_the_run_and_leaves_the_object_untouched_and_unresolved_or_whole(rig, fault, words):
    before = seed(rig, HISTORY[-3:])
    rig.fake.fail(*fault)
    assert rig.run() == offsite.EXIT_FAILED, rig.text
    assert words in rig.text and "NOT a recovery point" in rig.text
    assert "deletefile" not in rig.fake.rclone_commands()              # an uncertain object is never deleted by the tool
    files = rig.fake.remote_files()
    assert FIXED_NAME in files and {n: files[n] for n in before} == before
    assert rig.work_leftovers() == []
    folder = rig.classify()
    if fault[0] == "cat":
        # The record was written in full; only the read-back failed or was garbled. The evidence is durable, so later runs count it.
        assert FIXED_NAME in rig.committed_names() and folder.unresolved == {}
    else:
        assert FIXED_NAME not in rig.committed_names() and FIXED_NAME in folder.unresolved


def test_a_signal_while_the_record_is_being_written_leaves_an_unresolved_object_and_no_plaintext(rig):
    rig.fake.fail("record", "signal", 1)
    assert rig.run() == 128 + signal.SIGTERM
    assert FIXED_NAME in rig.fake.remote_files() and rig.committed_names() == set()
    assert rig.work_leftovers() == [] and rig.classify().unresolved == {FIXED_NAME: offsite.NO_RECORD}


def test_an_object_changed_after_its_commit_is_unresolved_not_counted_and_not_deleted(rig):
    seed(rig, HISTORY[-6:])
    victim = point_name(HISTORY[-3])
    path = rig.fake.folder() / victim
    path.write_bytes(path.read_bytes()[:-1] + b"X")       # same size, other bytes: the listed md5 differs from the record's
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert f"UNRESOLVED {victim}: {offsite.DIFFERS}" in rig.text
    assert victim not in rig.committed_names() and victim not in deleted_names(rig) and path.exists()
    path.write_bytes(path.read_bytes() + b"more")          # another size
    assert rig.classify().unresolved[victim] == offsite.DIFFERS


def test_a_record_that_belongs_to_another_object_does_not_make_this_one_a_point(rig):
    seed(rig, HISTORY[-3:])
    a, b = point_name(HISTORY[-1]), point_name(HISTORY[-2])
    (rig.fake.folder() / (b + RECORD)).write_bytes((rig.fake.folder() / (a + RECORD)).read_bytes())
    assert rig.classify().unresolved == {b: offsite.BAD_RECORD}


def test_orphaned_records_are_removed_by_retention_within_its_budget_and_never_counted(rig):
    seed(rig, HISTORY[-6:])
    stray = point_name("20260101T041500Z", "12345678") + RECORD
    rig.fake.put_remote(stray, offsite.commit_record(stray[: -len(RECORD)], digests_of_bytes(DATA)))
    assert rig.run() == 0, rig.text
    assert stray not in rig.fake.remote_files() and f"deleted {stray}: an orphaned commit record" in rig.text
    assert stray in deleted_names(rig)


def test_a_record_that_cannot_be_read_during_the_final_check_stops_everything_before_it_deletes_anything(rig):
    seed(rig, HISTORY)
    rig.fake.fail("cat", ("exit", 1), 2)           # the first read is the run's own read-back; this is the final check's first
    # The final check cannot be completed, so the point is not confirmed: its own status, and not the "stored and verified" of exit 6.
    assert rig.run() == offsite.EXIT_UNCONFIRMED, rig.text
    assert "rclone cat failed" in rig.text and "stored and verified" not in rig.text
    assert "deletefile" not in rig.fake.rclone_commands() and len(rig.fake.point_objects()) == 13


def test_the_operator_can_quarantine_an_unresolved_object_by_renaming_it_and_the_next_run_is_clean(rig):
    rig.fake.fail("record", ("exit", 1), 1)
    assert rig.run() == offsite.EXIT_FAILED
    rig.fake.faults.clear()
    rig.log.clear()
    rig.token = "00000001"
    assert rig.run() == offsite.EXIT_RETENTION            # reported: it is still there
    quarantine(rig, FIXED_NAME)                           # RUNBOOK 15.7: rename out of the tool's shape, delete nothing
    rig.log.clear()
    rig.token = "00000002"
    assert rig.run() == 0, rig.text
    assert (rig.fake.folder() / (FIXED_NAME + ".quarantined")).exists() and "left alone" in rig.text


def test_list_separates_committed_points_from_everything_that_needs_a_look_and_changes_nothing(rig):
    seed(rig, HISTORY[-4:])
    rig.fake.put_remote(point_name("20261020T041500Z", "deadbeef"), b"no record")
    stray = point_name("20261001T041500Z", "12345678") + RECORD
    rig.fake.put_remote(stray, offsite.commit_record(stray[: -len(RECORD)], digests_of_bytes(DATA)))
    rig.fake.put_remote("notes.txt", b"mine")
    before = rig.fake.remote_files()
    assert run_list(rig) == 0
    assert rig.fake.remote_files() == before and rig.fake.rclone_writes() == []
    assert "4 recovery points" in rig.text and "UNRESOLVED  " + point_name("20261020T041500Z", "deadbeef") in rig.text
    assert "1 orphaned commit record" in rig.text and "1 other object(s)" in rig.text


# --- the clock ---------------------------------------------------------------------------------------------------


FUTURE = point_name("20990101T000000Z", "feedface")


def points_of(*names):
    return [offsite.parse_point_name(n) for n in names]


def test_a_future_dated_point_takes_no_place_from_a_real_point_in_the_plan():
    names = [point_name(s) for s in stamps(datetime(2026, 10, 1), 25)]
    now = datetime(2026, 10, 25, 5, 0, tzinfo=UTC)
    without = offsite.plan_retention(points_of(*names), now=now)
    with_future = offsite.plan_retention(points_of(FUTURE, *names), now=now)
    assert with_future.keep == without.keep and with_future.drop == without.drop       # the real points are exactly as they were
    assert [p.name for p in with_future.suspect] == [FUTURE] and without.suspect == []
    assert FUTURE not in with_future.keep and FUTURE not in {p.name for p in with_future.drop}   # neither kept nor dropped
    # without a clock the old behaviour remains available to pure callers: the future point is just the newest
    assert FUTURE in offsite.plan_retention(points_of(FUTURE, *names)).keep


def test_a_point_is_in_the_future_only_beyond_the_tolerance():
    now = datetime(2026, 10, 25, 4, 15, 0, tzinfo=UTC)
    inside = point_name((now + offsite.FUTURE_TOLERANCE).strftime(offsite.STAMP_FORMAT), "00000001")
    beyond = point_name((now + offsite.FUTURE_TOLERANCE + timedelta(seconds=1)).strftime(offsite.STAMP_FORMAT), "00000002")
    plan = offsite.plan_retention(points_of(inside, beyond), now=now)
    assert [p.name for p in plan.suspect] == [beyond] and inside in plan.keep


def test_a_future_dated_point_stops_every_deletion_is_reported_by_every_run_and_is_itself_never_deleted(rig):
    seed(rig, HISTORY)
    rig.fake.put_point(FUTURE, b"from the future")
    for day, token in ((0, "00000001"), (1, "00000002")):
        rig.fake.rclone_calls.clear()
        rig.log.clear()
        rig.now = FIXED_NOW + timedelta(days=day)
        rig.token = token
        assert rig.run() == offsite.EXIT_RETENTION, rig.text
        assert "retention deleted nothing" in rig.text and FUTURE in rig.text and "later than the host clock" in rig.text
        assert "deletefile" not in rig.fake.rclone_commands()                        # nothing at all, not even the usual drops
        assert FUTURE in rig.fake.remote_files() and FUTURE + RECORD in rig.fake.remote_files()
        assert len(rig.fake.point_objects()) == 13 + day + 1                         # history, the future point, one new point a run


def test_list_shows_the_future_point_as_suspect_not_as_kept_or_dropped(rig):
    seed(rig, HISTORY)
    rig.fake.put_point(FUTURE, b"from the future")
    assert run_list(rig) == 0
    assert f"SUSPECT  {FUTURE}" in rig.text and "deletes NOTHING while one exists" in rig.text
    assert f"keep  {FUTURE}" not in rig.text and f"drop  {FUTURE}" not in rig.text


def test_a_clock_set_back_makes_every_newer_point_suspect_and_stops_the_deletions(rig):
    seed(rig, HISTORY)
    rig.now = datetime(2026, 10, 1, 4, 15, 7, tzinfo=UTC)         # the host clock fell back by weeks
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert "later than the host clock" in rig.text and "deletefile" not in rig.fake.rclone_commands()
    assert len(rig.fake.point_objects()) == 13 and "20261001T041507Z" in " ".join(rig.committed_names())      # the new point is kept


def test_a_clock_that_jumps_forward_between_runs_stops_the_deletions_of_that_run(rig):
    seed(rig, HISTORY)
    rig.now = datetime(2026, 12, 31, 4, 15, 7, tzinfo=UTC)          # 68 days after the newest point
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    assert "days after the previous one" in rig.text and "deletefile" not in rig.fake.rclone_commands()
    assert len(rig.fake.point_objects()) == 13


def test_a_gap_of_a_few_days_is_an_outage_and_not_a_clock_problem(rig):
    seed(rig, HISTORY)
    rig.now = FIXED_NOW + timedelta(days=offsite.MAX_FORWARD_GAP.days - 2)   # Oct 30: six days after the newest point
    assert rig.run() == 0, rig.text


def test_a_clock_that_moves_forward_during_the_run_stops_the_deletions(rig):
    seed(rig, HISTORY)
    assert run_with_clock(rig, FIXED_NOW, FIXED_NOW + timedelta(hours=5)) == offsite.EXIT_RETENTION
    assert "moved forward by 5 hours during this run" in rig.text and "deletefile" not in rig.fake.rclone_commands()


def test_a_clock_that_moves_back_during_the_run_makes_the_new_point_suspect_and_it_is_not_dropped(rig):
    seed(rig, HISTORY)
    assert run_with_clock(rig, FIXED_NOW, FIXED_NOW - timedelta(hours=3)) == offsite.EXIT_RETENTION
    assert FIXED_NAME in rig.committed_names() and "deletefile" not in rig.fake.rclone_commands()
    assert FIXED_NAME in rig.text and "later than the host clock" in rig.text


def test_a_few_minutes_of_drift_are_not_a_clock_problem(rig):
    seed(rig, HISTORY)
    rig.fake.put_point(point_name("20261025T043700Z", "00000009"), b"made by a clock 22 minutes ahead")
    assert rig.run() == 0, rig.text
    assert "deletefile" in rig.fake.rclone_commands()


def test_after_the_operator_quarantines_the_suspect_point_retention_works_again(rig):
    seed(rig, HISTORY)
    rig.fake.put_point(FUTURE, b"from the future")
    assert rig.run() == offsite.EXIT_RETENTION
    quarantine(rig, FUTURE)
    rig.log.clear()
    rig.token = "00000001"
    rig.fake.rclone_calls.clear()
    assert rig.run("--max-deletions", "10") == 0, rig.text
    assert "deletefile" in rig.fake.rclone_commands() and (rig.fake.folder() / (FUTURE + ".quarantined")).exists()


# --- one deletion budget -----------------------------------------------------------------------------------------


def add_partials(rig, count):
    for index in range(count):
        rig.fake.put_remote(point_name("20261020T041500Z", f"{index + 1:08x}") + ".partial", b"half")


def partials_in(rig):
    return {n: b for n, b in rig.fake.remote_files().items() if n.endswith(".partial")}


@pytest.mark.parametrize(
    "limit, partials, retention_runs",
    [(5, 2, True), (3, 0, True), (3, 5, True), (7, 4, True), (2, 0, False), (2, 5, False), (0, 4, False)],
)
def test_max_deletions_is_spent_on_retention_alone_and_a_leftover_partial_never_draws_on_it(rig, limit, partials, retention_runs):
    seed(rig, stamps(datetime(2026, 10, 15), 10))        # Oct 15 .. 24: with Oct 25, exactly three points are dropped
    add_partials(rig, partials)
    before = partials_in(rig)
    code = rig.run("--max-deletions", str(limit))
    assert code == (0 if retention_runs and not partials else offsite.EXIT_RETENTION), rig.text   # partials are always reported
    assert partials_in(rig) == before and len(before) == partials                                  # none of them touched, whatever the limit
    assert not [n for n in deleted_names(rig) if n.endswith(".partial")]
    assert len(deleted_names(rig)) == (6 if retention_runs else 0)                                   # three points and their three records
    assert len(rig.fake.point_objects()) == (8 if retention_runs else 11)
    if not retention_runs:
        assert "more than --max-deletions" in rig.text


def test_a_budget_of_zero_removes_nothing_and_says_so(rig):
    add_partials(rig, 2)
    assert rig.run("--max-deletions", "0") == offsite.EXIT_RETENTION
    assert "2 leftover .partial object(s) are in the folder" in rig.text and deleted_names(rig) == []
    assert len(partials_in(rig)) == 2
