"""The restore rehearsal (deploy/offsite_rehearsal.py, RUNBOOK 15.13): fetch a committed point from the remote, decrypt it
with the owner's PRIVATE identity, unpack it into a scratch directory, check the archive and the database, remove the
scratch directory.

The points it rehearses are made by the real backup (tests/offsite_support.py: real dbtool, fake rclone/age/docker); the
broken ones are built here, with the real tar writer, and put on the fake remote with a valid commit record, so that they
are exactly what a damaged-but-committed point would be. The age identity is a synthetic string of the right shape that
only the fake age accepts; nothing is written inside the repository, and no real key exists anywhere in these tests.

What is held:
  * it passes only for a COMMITTED point whose downloaded bytes equal its commit record, that decrypts, whose tar is
    exactly what the tool writes, whose database is sound, rewritten, at one revision and holds what was expected;
  * every other outcome is a distinct non-zero status and prints no PASSED, and in EVERY outcome the scratch directory
    is gone and nothing else on disk changed (the production database, the remote, the rest of the tree);
  * it refuses to run where the private identity would be at risk (inside the checkout, readable by others, on the
    production host), and the production script still knows no identity.
"""

import hashlib
import os
import re
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.deploy_support import DEPLOY, HEAD, REPO_ROOT, posix_only
from tests.offsite_support import (
    FIXED_NAME,
    FIXED_NOW,
    FakeSystem,
    Rig,
    fake_decrypt,
    fake_encrypt,
    load_rehearsal,
    make_database_with_deleted_rows,
    offsite,
    point_name,
)
from tests.test_deploy_offsite_fulldisk import LEAK_PATH, LEAK_SECRET, LEAK_TOKEN, conf, write_conf

rehearsal = load_rehearsal()

GOOD_KEY = "AGE-SECRET-KEY-1" + "Q" * 58   # synthetic: the fake age accepts exactly this one
OTHER_KEY = "AGE-SECRET-KEY-1" + "Z" * 58   # a well-shaped identity that is not a recipient of anything
TAG = "abc123def456"
LATER = FIXED_NOW + timedelta(days=2)


class RehearsalSystem(FakeSystem):
    """FakeSystem plus `age -d -i IDENTITY`: it decrypts only for GOOD_KEY, and answers a wrong key like age does."""

    noise = b""

    def _age(self, argv, stdin_path, stdout_path, env):
        assert argv[1:3] == ["-d", "-i"] and len(argv) == 4, argv           # the identity is a path, never a value
        self.events.append("decrypt")
        action = self._fault("decrypt")
        if isinstance(action, tuple) and action[0] == "exit":
            return offsite.Result(action[1], b"", self.noise or b"age: error: injected\n")
        if GOOD_KEY not in Path(argv[3]).read_text():
            return offsite.Result(1, b"", self.noise or b"age: error: no identity matched any of the recipients\n")
        try:
            plain = fake_decrypt(stdin_path.read_bytes())
        except AssertionError:
            return offsite.Result(1, b"", self.noise or b"age: error: not an age file\n")
        with offsite.open_exclusive(stdout_path) as handle:
            handle.write(plain)
        return offsite.Result(0)


def tree(root: Path) -> dict:
    files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}
    folders = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir())
    return {"files": files, "folders": folders}


class World:
    """A backup rig that has made one committed point, a rehearsal system on the same remote, an identity outside every
    checkout, an empty scratch directory."""

    def __init__(self, tmp_path: Path, *, full_disk: bool = False, make_point: bool = True) -> None:
        self.tmp = tmp_path
        self.rig = Rig(tmp_path, full_disk=full_disk)
        self.full_disk = full_disk
        if make_point:
            assert self.rig.run() == 0, self.rig.text
        self.fake = RehearsalSystem(tmp_path, self.rig.database, remote_account=self.rig.fake.account, folder=self.rig.fake.folder_name)
        self.fake.app_folder_support = not full_disk
        self.keys = tmp_path / "keys"
        self.keys.mkdir()
        self.identity = self.write_identity(GOOD_KEY)
        self.scratch = tmp_path / "scratch"
        self.scratch.mkdir()
        self.markers = (tmp_path / "not-the-production-host",)
        self.log: list = []

    def write_identity(self, key: str, name: str = "identity.txt") -> Path:
        path = self.keys / name
        path.write_text(f"# created: 2026-10-25T00:00:00Z\n# public key: age1synthetic\n{key}\n")
        if os.name != "nt":
            path.chmod(0o600)
        return path

    def rehearse(self, *extra, expect=("messages=12", "users=1"), identity=None, work=None, deploy_dir=None, markers=None, mode=True) -> int:
        argv = [
            "--rclone-config", str(self.rig.config), "--remote", self.rig.remote,
            "--identity-file", str(identity or self.identity), "--work-dir", str(work or self.scratch),
        ]
        for item in expect:
            argv += ["--expect-rows", item]
        if self.full_disk and mode:
            argv += ["--access-mode", offsite.ACCESS_FULL_DISK]
        self.log.clear()
        return rehearsal.main(
            [*argv, *extra], runner=self.fake, clock=lambda: LATER, deploy_dir=deploy_dir or DEPLOY, log=self.log.append,
            production_markers=self.markers if markers is None else markers,
        )

    @property
    def text(self) -> str:
        return "\n".join(self.log)

    def plant(self, name: str, snapshot: Path, *, created: datetime = None, mutate=None) -> str:
        """A COMMITTED point (object and valid record) made from `snapshot`, however bad the snapshot is."""
        moment = created or offsite.parse_point_name(name).moment
        tar = self.tmp / f"plant-{len(list(self.tmp.glob('plant-*')))}.tar"
        offsite.build_recovery_tar(tar, snapshot, moment, TAG)
        data = tar.read_bytes()
        self.rig.fake.put_point(name, fake_encrypt(mutate(data) if mutate else data))
        return name

    def scratch_is_empty(self) -> bool:
        return list(self.scratch.iterdir()) == []


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def small_database(path: Path, *, version_rows=(HEAD,), version_table=True, violation=False, rows=2) -> Path:
    """A tiny, clean (no free page) SQLite file with a parent table, optionally a broken foreign key or odd revisions."""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))")
        connection.executemany("INSERT INTO parent (id) VALUES (?)", [(i,) for i in range(1, rows + 1)])
        if violation:
            connection.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")
        if version_table:
            connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            connection.executemany("INSERT INTO alembic_version VALUES (?)", [(v,) for v in version_rows])
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    return path


# --- a good point -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("full_disk", [False, True], ids=["app-folder", "full-disk-temporary"])
def test_a_committed_point_is_retrieved_decrypted_unpacked_and_verified_and_nothing_is_left_or_changed(tmp_path, full_disk):
    world = World(tmp_path, full_disk=full_disk)
    before = tree(tmp_path)
    assert world.rehearse() == 0, world.text
    assert "rehearsal PASSED" in world.text and FIXED_NAME in world.text and "messages>=12 (found 12)" in world.text
    assert "Nothing was written to the remote or to any production location" in world.text
    assert world.scratch_is_empty()
    assert tree(tmp_path) == before                                   # the production database, the remote, everything: unchanged
    assert world.fake.rclone_writes() == ["copyto"] and "upload" not in world.fake.events         # the one copyto is the download to the scratch directory
    assert not {"backup", "sanitize", "verify", "age"} & set(world.fake.events)       # no backup, no container, no encryption
    assert world.fake.events.count("download") == 1 and world.fake.events.count("decrypt") == 1
    assert set(world.fake.rclone_commands()) <= {"config", "backend", "lsjson", "cat", "copyto"}
    downloads = [c for c in world.fake.rclone_calls if c[0] == "copyto"]
    assert len(downloads) == 1 and downloads[0][-2].startswith(world.rig.remote + "/")                        # one object, from the one folder, to a local path
    assert ("NOTICE: TEMPORARY access mode" in world.text) == full_disk


def test_the_verdict_names_the_point_the_encrypted_checksum_the_image_and_the_counts_but_no_content_and_no_key(world):
    assert world.rehearse() == 0
    digest = hashlib.sha256(world.rig.fake.remote_files()[FIXED_NAME]).hexdigest()
    assert digest in world.text and "image abc123def456" in world.text and f"schema revision {HEAD}" in world.text
    for forbidden in (GOOD_KEY, "Q" * 20, str(world.identity), "KEPTROW", "DELETEDROW", "synthetic"):
        assert forbidden not in world.text, forbidden


def test_the_decrypted_snapshot_is_the_database_that_was_backed_up_minus_the_deleted_rows(world):
    # The rehearsal's own reading of the database agrees with the rows the rig put there: 12 kept messages, 1 user.
    assert world.rehearse(expect=("messages=12", "users=1", "conversations=2")) == 0, world.text
    assert world.rehearse(expect=("messages=13",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert "expected at least 13 row(s) in messages, found 12" in world.text


# --- which point ---------------------------------------------------------------------------------------------------


def make_second_point(world) -> str:
    world.rig.now, world.rig.token = FIXED_NOW + timedelta(days=1), "1b2c3d4e"
    assert world.rig.run() == 0, world.rig.text
    return "house-helper-20261026T041507Z-1b2c3d4e.tar.age"


def test_the_newest_committed_point_is_the_default_and_point_selects_another(world):
    newer = make_second_point(world)
    assert world.rehearse() == 0 and newer in world.text and FIXED_NAME not in world.text
    assert world.rehearse("--point", FIXED_NAME) == 0 and FIXED_NAME in world.text


def test_an_unresolved_object_is_never_rehearsed_whatever_its_name_says(world):
    ghost = point_name("20261026T000000Z", "deadbeef")
    world.rig.fake.put_remote(ghost, fake_encrypt(b"no commit record"))
    assert world.rehearse() == 0 and FIXED_NAME in world.text and f"UNRESOLVED {ghost}" in world.text    # the committed one is used
    assert world.rehearse("--point", ghost) == rehearsal.EXIT_REFUSED
    assert "UNRESOLVED" in world.text and "not a recovery point" in world.text
    assert world.fake.events.count("download") == 1                       # only the first rehearsal downloaded anything
    assert world.scratch_is_empty()


def test_a_leftover_partial_in_the_folder_is_ignored_untouched_and_changes_no_status(world):
    """The backup tool never deletes a `.partial` and reports it (RUNBOOK 15.11); the rehearsal neither reads, reports nor touches
    one, refuses it as a --point, and its statuses (the docstring of deploy/offsite_rehearsal.py) are the same with one there."""
    partial = point_name("20261026T000000Z", "deadbeef") + ".partial"
    world.rig.fake.put_remote(partial, b"somebody's half-written object")
    before = world.rig.fake.remote_files()
    assert world.rehearse() == 0, world.text
    assert FIXED_NAME in world.text and "LEFTOVER" not in world.text and partial not in world.text
    assert world.rehearse("--point", partial) == rehearsal.EXIT_REFUSED, world.text
    assert world.rig.fake.remote_files() == before and world.scratch_is_empty()
    assert world.fake.events.count("download") == 1                       # only the first rehearsal downloaded anything


@pytest.mark.parametrize(
    "point", ["../x", f"../{FIXED_NAME}", "house-helper-x.tar.age", FIXED_NAME + ".commit", FIXED_NAME + ".partial", FIXED_NAME.upper(),
              "house-helper-20261099T000000Z-0a1b2c3d.tar.age", f"sub/{FIXED_NAME}", "", "*"],
)
def test_a_point_that_is_not_a_name_of_the_tools_or_not_in_the_folder_is_refused_before_a_download(world, point):
    assert world.rehearse("--point", point) == rehearsal.EXIT_REFUSED, world.text
    assert "download" not in world.fake.events and world.scratch_is_empty()


def test_a_point_that_changed_after_its_commit_is_unresolved_and_therefore_not_rehearsed(world):
    path = world.rig.fake.folder() / FIXED_NAME
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])                    # the listing's MD5 no longer equals the record's
    assert world.rehearse() == rehearsal.EXIT_REFUSED, world.text
    assert "no committed recovery point" in world.text and "download" not in world.fake.events
    assert world.rehearse("--point", FIXED_NAME) == rehearsal.EXIT_REFUSED and "UNRESOLVED" in world.text


def test_an_empty_folder_has_nothing_to_rehearse(tmp_path):
    world = World(tmp_path, make_point=False)
    world.rig.fake.folder().mkdir(parents=True)
    assert world.rehearse() == rehearsal.EXIT_REFUSED and "no committed recovery point" in world.text


# --- the transfer and the decryption ------------------------------------------------------------------------------------


def test_bytes_that_differ_from_the_commit_record_after_the_download_are_not_restorable(world):
    world.fake.fail("download", "corrupt")
    assert world.rehearse() == rehearsal.EXIT_NOT_RESTORABLE, world.text
    assert "not the one its commit record describes" in world.text and "PASSED" not in world.text
    assert "decrypt" not in world.fake.events and world.scratch_is_empty()


def test_a_failed_download_is_a_failure_not_a_verdict(world):
    world.fake.fail("download", ("exit", 1))
    assert world.rehearse() == rehearsal.EXIT_FAILED, world.text
    assert "rclone copyto failed" in world.text and "PASSED" not in world.text and world.scratch_is_empty()


def test_an_identity_that_is_not_a_recipient_cannot_decrypt_and_the_point_is_not_restorable_with_it(world):
    other = world.write_identity(OTHER_KEY, "other.txt")
    assert world.rehearse(identity=other) == rehearsal.EXIT_NOT_RESTORABLE, world.text
    assert "could not decrypt" in world.text and "PASSED" not in world.text
    assert OTHER_KEY not in world.text and world.scratch_is_empty()


def test_age_failing_is_not_restorable_and_its_message_is_not_forwarded(world):
    world.fake.noise = f"age: {LEAK_TOKEN} {GOOD_KEY}".encode()
    world.fake.fail("decrypt", ("exit", 1))
    assert world.rehearse() == rehearsal.EXIT_NOT_RESTORABLE
    assert LEAK_TOKEN not in world.text and GOOD_KEY not in world.text and world.scratch_is_empty()


# --- a committed point that is not restorable --------------------------------------------------------------------------------


def good_snapshot(world) -> Path:
    path = world.tmp / "good.db"
    small_database(path)
    return path


def garbage_database(path: Path) -> Path:
    path.write_bytes(b"SQLite format 3\x00" + os.urandom(8000))
    return path


def not_a_database(path: Path) -> Path:
    path.write_bytes(b"hello, this is not a database")
    return path


def unsanitized_database(path: Path) -> Path:
    """The real schema with rows deleted by a plain DELETE (free pages), in rollback-journal mode as every point's file is."""
    make_database_with_deleted_rows(path)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
    finally:
        connection.close()
    return path


def flip_inside_the_snapshot(data: bytes) -> bytes:
    at = data.index(b"SQLite format 3\x00") + 200
    return data[:at] + bytes([data[at] ^ 0xFF]) + data[at + 1:]


BAD_POINTS = [
    # id, builder(world) -> snapshot path, words in the message
    ("garbage with a sqlite header", lambda w: garbage_database(w.tmp / "g.db"), r"integrity check|could not read the snapshot"),
    ("not sqlite at all", lambda w: not_a_database(w.tmp / "n.db"), "not a SQLite database"),
    ("free pages: not rewritten", lambda w: unsanitized_database(w.tmp / "f.db"), "free pages"),
    ("no revision table", lambda w: small_database(w.tmp / "r.db", version_table=False), "no Alembic revision table"),
    ("two revisions", lambda w: small_database(w.tmp / "t.db", version_rows=("0005_a", "0006_b")), "exactly one plain Alembic revision"),
    ("no revision", lambda w: small_database(w.tmp / "z.db", version_rows=()), "exactly one plain Alembic revision"),
    ("revision that is not an identifier", lambda w: small_database(w.tmp / "i.db", version_rows=("0006 or 1=1; --",)), "exactly one plain Alembic revision"),
    ("foreign-key violation", lambda w: small_database(w.tmp / "k.db", violation=True), "foreign-key violation"),
]


@pytest.mark.parametrize("build, words", [(b, w) for _, b, w in BAD_POINTS], ids=[i for i, _, _ in BAD_POINTS])
def test_a_committed_point_whose_database_is_not_sound_is_not_restorable_and_leaves_nothing(world, build, words):
    name = world.plant(point_name("20261026T020000Z", "badbad01"), build(world))
    before = tree(world.tmp)
    assert world.rehearse("--point", name, expect=("parent=1",)) == rehearsal.EXIT_NOT_RESTORABLE, world.text
    assert re.search(words, world.text), world.text
    assert "PASSED" not in world.text and world.scratch_is_empty()
    assert tree(world.tmp) == before


@pytest.mark.parametrize(
    "expect, words",
    [
        (("parent=3",), "expected at least 3 row\\(s\\) in parent, found 2"),
        (("nosuchtable=1",), "expected table nosuchtable is not in the snapshot"),
        (("parent=1", "child=1"), "expected at least 1 row\\(s\\) in child, found 0"),
    ],
)
def test_an_expectation_that_the_restored_database_does_not_meet_is_not_restorable(world, expect, words):
    name = world.plant(point_name("20261026T020000Z", "feed0001"), good_snapshot(world))
    assert world.rehearse("--point", name, expect=expect) == rehearsal.EXIT_NOT_RESTORABLE, world.text
    assert re.search(words, world.text) and "PASSED" not in world.text and world.scratch_is_empty()


def test_the_same_planted_point_passes_when_what_is_expected_is_there(world):
    name = world.plant(point_name("20261026T020000Z", "feed0002"), good_snapshot(world))
    assert world.rehearse("--point", name, expect=("parent=2",)) == 0, world.text
    assert "parent>=2 (found 2)" in world.text


@pytest.mark.parametrize(
    "mutate, words",
    [
        (flip_inside_the_snapshot, "does not match its manifest"),
        (lambda data: data[: len(data) // 2], "not a recovery point"),
        (lambda data: b"this is not a tar archive" * 50, "not a recovery point"),
        (lambda data: b"", "not a recovery point|empty"),
    ],
    ids=["a flipped byte inside the snapshot", "cut in half", "not a tar", "empty"],
)
def test_a_decrypted_archive_that_is_not_exactly_what_the_tool_writes_is_not_restorable(world, mutate, words):
    name = world.plant(point_name("20261026T020000Z", "feed0003"), good_snapshot(world), mutate=mutate)
    assert world.rehearse("--point", name, expect=("parent=1",)) == rehearsal.EXIT_NOT_RESTORABLE, world.text
    assert re.search(words, world.text) and "PASSED" not in world.text and world.scratch_is_empty()
    assert not (world.scratch / "restored").exists()


def test_a_manifest_whose_time_is_not_the_time_in_the_points_name_is_not_restorable(world):
    name = world.plant(point_name("20261026T020000Z", "feed0004"), good_snapshot(world), created=datetime(2026, 10, 20, 2, 0, 0, tzinfo=timezone.utc))
    assert world.rehearse("--point", name, expect=("parent=1",)) == rehearsal.EXIT_NOT_RESTORABLE
    assert "time in the manifest" in world.text and world.scratch_is_empty()


# --- where the private identity may be ---------------------------------------------------------------------------------------


def refused(world, **kwargs):
    assert world.rehearse(**kwargs) == rehearsal.EXIT_REFUSED, world.text
    assert world.fake.calls == [] and "PASSED" not in world.text and world.scratch_is_empty()
    return world.text


def test_an_identity_inside_the_checkout_is_refused_and_so_is_a_scratch_directory_inside_it(world, tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "secrets").mkdir()
    inside = checkout / "secrets" / "key.txt"
    inside.write_text(GOOD_KEY + "\n")
    if os.name != "nt":
        inside.chmod(0o600)
    assert "inside the repository checkout" in refused(world, identity=inside, deploy_dir=checkout / "deploy")
    scratch_inside = checkout / "scratch"
    scratch_inside.mkdir()
    assert "--work-dir is inside the repository checkout" in refused(world, work=scratch_inside, deploy_dir=checkout / "deploy")
    assert list(scratch_inside.iterdir()) == []


def test_the_real_checkout_of_this_repository_is_recognised_as_forbidden_for_the_identity_and_the_scratch_directory():
    forbidden = rehearsal._forbidden_places(DEPLOY, ())
    assert forbidden == [REPO_ROOT.resolve()]
    with pytest.raises(offsite.Refused, match="inside the repository checkout"):
        rehearsal.check_identity_file(REPO_ROOT / "deploy" / "RUNBOOK.md", forbidden)   # (refused for where it is, before a byte is read)
    with pytest.raises(offsite.Refused, match="inside the repository checkout"):
        rehearsal.check_work_dir(REPO_ROOT / "tests", forbidden)


def test_this_host_being_the_production_host_refuses_the_rehearsal(world, tmp_path):
    marker = tmp_path / "etc-house-helper"
    marker.mkdir()
    assert "production host" in refused(world, markers=(marker,))


def test_the_production_marker_is_the_directory_the_unit_templates_install_into():
    assert rehearsal.PRODUCTION_MARKERS == (Path("/etc/house-helper"),)
    assert "/etc/house-helper/" in (DEPLOY / "systemd" / "house-helper-offsite.env.example").read_text()


def test_an_identity_that_holds_no_secret_key_is_refused(world):
    public_only = world.keys / "recipients.txt"
    public_only.write_text("# only a public key\nage1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p\n")
    if os.name != "nt":
        public_only.chmod(0o600)
    assert "holds no age secret key" in refused(world, identity=public_only)


def test_a_missing_empty_or_oversized_identity_is_refused(world):
    assert "regular file" in refused(world, identity=world.keys / "missing.txt")
    empty = world.keys / "empty.txt"
    empty.write_text("")
    if os.name != "nt":
        empty.chmod(0o600)
    assert "holds no age secret key" in refused(world, identity=empty)
    big = world.keys / "big.txt"
    big.write_text(GOOD_KEY + "\n" + "#" * (rehearsal.MAX_IDENTITY_BYTES + 1))
    if os.name != "nt":
        big.chmod(0o600)
    assert "larger than any age identity file" in refused(world, identity=big)


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o666, 0o660])
def test_an_identity_that_other_users_can_read_is_refused(world, mode):
    world.identity.chmod(mode)
    assert "readable by other users" in refused(world)


@posix_only
def test_an_identity_that_is_a_symbolic_link_is_refused(world):
    link = world.keys / "link.txt"
    link.symlink_to(world.identity)
    assert "regular file" in refused(world, identity=link)


def test_the_identity_is_passed_to_age_as_a_path_and_the_key_text_is_in_no_argument_and_no_log(world):
    assert world.rehearse() == 0
    age = next(c for c in world.fake.calls if os.path.basename(c[0]) == "age")
    assert age[1:] == ["-d", "-i", str(world.identity)]
    assert not any(GOOD_KEY in part for call in world.fake.calls for part in call)
    assert GOOD_KEY not in world.text


def test_a_scratch_directory_that_is_missing_relative_or_a_link_is_refused(world, tmp_path):
    assert "existing absolute directory" in refused(world, work=tmp_path / "nope")
    assert "existing absolute directory" in refused(world, work=Path("scratch"))
    if os.name != "nt":
        link = tmp_path / "scratch-link"
        link.symlink_to(world.scratch, target_is_directory=True)
        assert "existing absolute directory" in refused(world, work=link)


def test_the_remote_and_the_mode_pass_exactly_the_checks_of_the_backup_and_nothing_runs_before_they_do(tmp_path):
    world = World(tmp_path, full_disk=True)
    assert world.rehearse(mode=False) == rehearsal.EXIT_REFUSED                         # a full-disk config without the flag
    assert "--access-mode full-disk-temporary" in world.text and world.fake.calls == []
    write_conf(world.rig, conf(app_folder="true"))
    assert world.rehearse() == rehearsal.EXIT_REFUSED and "contradicts" in world.text and world.fake.calls == []
    write_conf(world.rig, conf())
    assert world.rehearse("--point", FIXED_NAME) == 0, world.text
    other = ["--rclone-config", str(world.rig.config), "--remote", "fake:house-helper", "--identity-file", str(world.identity),
             "--work-dir", str(world.scratch), "--expect-rows", "users=1", "--access-mode", offsite.ACCESS_FULL_DISK]
    assert rehearsal.main(other, runner=world.fake, log=world.log.append, production_markers=world.markers) == rehearsal.EXIT_REFUSED
    assert "exactly" in "\n".join(world.log)


@pytest.mark.parametrize("tool", ["rclone", "age"])
def test_a_missing_tool_is_refused_before_anything_runs(world, tool):
    world.fake.missing.add(tool)
    assert f"{tool} is not installed" in refused(world)


# --- usage --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [["--expect-rows", "users"], ["--expect-rows", "users=0"], ["--expect-rows", "users=-1"], ["--expect-rows", "1users=1"],
     ["--expect-rows", "users=x"], ["--expect-rows", "users=1;drop"], ["--expect-rows", 'us"ers=1'], ["--expect-rows", "users=1", "--expect-rows", "users=2"]],
)
def test_a_malformed_or_repeated_expectation_is_a_usage_error_before_anything_runs(world, extra):
    code = rehearsal.main(
        ["--rclone-config", str(world.rig.config), "--remote", world.rig.remote, "--identity-file", str(world.identity),
         "--work-dir", str(world.scratch), *extra], runner=world.fake, log=world.log.append, production_markers=world.markers)
    assert code == rehearsal.EXIT_USAGE and world.fake.calls == [] and world.scratch_is_empty()


def test_at_least_one_expectation_is_required_and_there_is_a_bound(world):
    base = ["--rclone-config", str(world.rig.config), "--remote", world.rig.remote, "--identity-file", str(world.identity),
            "--work-dir", str(world.scratch)]
    assert rehearsal.main(base, runner=world.fake, log=world.log.append) == rehearsal.EXIT_USAGE
    many = [x for i in range(rehearsal.MAX_EXPECTATIONS + 1) for x in ("--expect-rows", f"t{i}=1")]
    assert rehearsal.main([*base, *many], runner=world.fake, log=world.log.append) == rehearsal.EXIT_USAGE
    assert world.fake.calls == []


def test_the_script_runs_as_a_program_in_isolated_mode_and_reports_usage_errors_with_status_2():
    script = str(DEPLOY / "offsite_rehearsal.py")
    helped = subprocess.run([sys.executable, "-I", script, "--help"], capture_output=True, text=True, timeout=120)
    assert helped.returncode == 0 and "--identity-file" in helped.stdout and "--expect-rows" in helped.stdout
    bad = subprocess.run([sys.executable, "-I", script], capture_output=True, text=True, timeout=120)
    assert bad.returncode == 2 and "usage" in bad.stderr


# --- clean-up on every exit path ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["download", "decrypt"])
def test_a_signal_during_the_rehearsal_removes_the_scratch_directory(world, kind):
    before = tree(world.tmp)
    world.fake.fail(kind, "signal")
    assert world.rehearse() == 128 + signal.SIGTERM, world.text
    assert world.scratch_is_empty() and tree(world.tmp) == before and "PASSED" not in world.text


def test_a_scratch_directory_that_cannot_be_removed_is_never_a_pass(world, monkeypatch):
    real = rehearsal.shutil.rmtree
    monkeypatch.setattr(rehearsal.shutil, "rmtree", lambda *a, **k: None)          # the removal "succeeds" and removes nothing
    assert world.rehearse() == rehearsal.EXIT_FAILED, world.text
    assert "could not be removed" in world.text and "This is not a pass" in world.text and "PASSED" not in world.text
    left = list(world.scratch.iterdir())
    assert len(left) == 1 and left[0].name.startswith(rehearsal.RUN_PREFIX)
    real(left[0])


def test_a_leftover_of_an_earlier_rehearsal_is_warned_about_not_deleted(world):
    leftover = world.scratch / (rehearsal.RUN_PREFIX + "killed")
    leftover.mkdir()
    (leftover / "restored.db").write_bytes(b"SQLite format 3\x00 plaintext")
    assert world.rehearse() == 0
    assert "left by an earlier rehearsal" in world.text and (leftover / "restored.db").exists()


def test_the_signal_handlers_are_put_back_when_the_rehearsal_ends(world):
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    world.rehearse()
    assert {s: signal.getsignal(s) for s in before} == before


# --- nothing secret is printed, whatever the tools say --------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["providers", "features", "lsjson", "cat", "download"])
def test_rclone_messages_full_of_secrets_are_not_printed(tmp_path, kind, capsys):
    class Loud(RehearsalSystem):
        def run(self, argv, **kwargs):
            result = super().run(argv, **kwargs)
            if result.returncode != 0 and os.path.basename(str(argv[0])) == "rclone":
                result = offsite.Result(result.returncode, result.stdout, f"token {LEAK_TOKEN} secret {LEAK_SECRET} {LEAK_PATH}".encode())
            return result

    world = World(tmp_path, full_disk=True)
    write_conf(world.rig, conf(client_secret=LEAK_SECRET, token='{"access_token":"%s"}' % LEAK_TOKEN))
    world.fake = Loud(tmp_path, world.rig.database, remote_account=world.rig.fake.account, folder=world.rig.fake.folder_name)
    world.fake.app_folder_support = False
    world.fake.fail(kind, ("exit", 1))
    code = rehearsal.main(
        ["--rclone-config", str(world.rig.config), "--remote", world.rig.remote, "--identity-file", str(world.identity),
         "--work-dir", str(world.scratch), "--expect-rows", "users=1", "--access-mode", offsite.ACCESS_FULL_DISK],
        runner=world.fake, clock=lambda: LATER, deploy_dir=DEPLOY, production_markers=world.markers,
    )
    assert code != 0
    shown = capsys.readouterr().err
    for leak in (LEAK_TOKEN, LEAK_SECRET, LEAK_PATH, GOOD_KEY, "access_token"):
        assert leak not in shown, (kind, leak)
    assert world.scratch_is_empty()


# --- what the program is, and what the production script is not ---------------------------------------------------------------------


def test_the_rehearsal_never_calls_anything_that_writes_to_the_remote_or_runs_a_container_or_a_shell():
    source = (DEPLOY / "offsite_rehearsal.py").read_text()
    assert set(re.findall(r"remote\.(\w+)\(", source)) == {"listing", "read_record", "download"}
    for forbidden in ("upload", "delete", "move(", "ensure_folder", "mkdir\"", "deletefile", "moveto", "docker", "bash", "shell=True",
                      "os.system", "Popen(", "restore(", "dbtool", "backup.sh", "--yandex-hard-delete"):
        assert forbidden not in re.sub(r'""".*?"""', "", source, flags=re.S), forbidden       # the docstring explains; the code does not do
    assert source.count("self.runner.run(") == 1                                             # one command: age -d -i


def test_the_production_script_and_the_units_know_nothing_of_the_rehearsal_or_an_identity():
    for path in [DEPLOY / "systemd" / name for name in os.listdir(DEPLOY / "systemd")] + [REPO_ROOT / "Dockerfile"]:
        text = path.read_text()
        assert "offsite_rehearsal" not in text and "identity" not in text.lower().replace("syslogidentifier", ""), path.name
    script = (DEPLOY / "offsite_backup.py").read_text()
    for forbidden in ("--decrypt", "--passphrase", "--identity", "age-keygen", "-d\", \"-i\""):
        assert forbidden not in script, forbidden
    assert re.findall(r"\[self\.tools\[\"age\"\], ([^\]]*)\]", script) == ['"-R", str(self.config.recipients_file)']
