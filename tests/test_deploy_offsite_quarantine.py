"""The operator's quarantine procedure of RUNBOOK 15.11 ("Quarantining one object safely").

The blocker of the last Codex review: both manual quarantine procedures ran `rclone moveto SOURCE SOURCE.quarantined`, a
destination that is the same every time, and the pinned stable rclone 1.75.2 REPLACES an existing destination and exits 0. A second
quarantine of the same name, or any object that already carried the name, was destroyed without a word.

What is held here:

  * the RUNBOOK's text is pinned (timer stopped and the service confirmed inactive, an exact name and never a wildcard, a unique
    destination that is checked, `--ignore-existing --error-on-no-transfer`, a listing of both names afterwards, an ambiguous result
    is for the operator and not for a retry) and no fenced command of the RUNBOOK moves an object onto a fixed name;
  * the RUNBOOK's OWN shell blocks are executed, in bash, against an rclone: a stand-in that mirrors what the real 1.75.2 was seen to
    do (always on, POSIX with bash), and the REAL binary (opt-in, HOUSE_HELPER_REAL_RCLONE=/path/to/rclone, its `local` backend);
    both run the same scenarios, so a difference between them is a failure of the stand-in;
  * the scenarios include a destination that already exists, and one that appears BETWEEN the check and the move.

What is NOT shown, and the RUNBOOK says so: that `--ignore-existing` is atomic against a writer that creates the destination in the
instant before the move, and anything about Yandex Disk's own `Move`. The procedure therefore stops the backup first and accepts only
the listing taken afterwards as proof.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

import pytest

from tests.deploy_support import posix_only
from tests.offsite_support import FIXED_NAME, Rig, offsite
from tests.test_deploy_kit import RUNBOOK, read

BINARY = os.environ.get("HOUSE_HELPER_REAL_RCLONE", "")
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None or os.name == "nt", reason="the RUNBOOK's blocks are bash, run on the POSIX host")
real_only = pytest.mark.skipif(not BINARY, reason="set HOUSE_HELPER_REAL_RCLONE to the pinned stable rclone to run these")
RCLONES = [pytest.param("fake", id="stand-in"), pytest.param("real", id="real-rclone", marks=real_only)]

HEADING = "**Quarantining one object safely.**"
NEXT_HEADING = "**An `UNRESOLVED` object**"
STAMP = "20261026T091500Z"
TOKEN = "5f3a9c01"
SUFFIX = f".quarantined-{STAMP}-{TOKEN}"
OBJECT = "house-helper-20261025T041507Z-0a1b2c3d.tar.age"
PARTIAL = OBJECT + ".partial"
RECORD = OBJECT + ".commit"


def procedure_text() -> str:
    text = read(RUNBOOK)
    return text[text.index(HEADING):text.index(NEXT_HEADING)]


def blocks() -> List[str]:
    """The fenced bash blocks of the procedure, in order, as an operator would paste them (the list indentation removed)."""
    found = re.findall(r"```bash\n(.*?)```", procedure_text(), re.DOTALL)
    return [textwrap.dedent(block) for block in found]


def all_fenced_blocks() -> List[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", read(RUNBOOK), re.DOTALL)


# --- the text -------------------------------------------------------------------------------------------------------------------


def test_the_procedure_has_the_six_command_blocks_its_steps_use():
    found = blocks()
    assert len(found) == 6, len(found)                       # 1 stop+idle, 2 select+inspect, 3 destination, 4 move, 5 compare, 7 start
    stop, select, destination, move, compare, start = found
    assert "systemctl stop house-helper-offsite-backup.timer" in stop and "is-active house-helper-offsite-backup.service" in stop
    assert "is-active house-helper-offsite-backup.timer" in stop and "pgrep" in stop
    assert "exact()" in select and "show()" in select and "BEFORE=$(show" in select
    assert "secrets.token_hex(4)" in destination and "date -u +%Y%m%dT%H%M%SZ" in destination and "TAKEN" in destination
    assert "moveto" in move and "--ignore-existing" in move and "--error-on-no-transfer" in move and "idle" in move
    assert 'AFTER=$(show "$SRC" "$DST")' in compare and "AMBIGUOUS" in compare and "MOVED" in compare
    assert "systemctl start house-helper-offsite-backup.timer" in start


@needs_bash
def test_every_block_is_valid_bash_alone_and_together(tmp_path):
    for number, block in enumerate(blocks() + ["\n".join(blocks())]):
        script = tmp_path / f"block{number}.sh"
        script.write_text(block)
        done = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
        assert done.returncode == 0, (number, done.stderr)


def test_no_fenced_command_of_the_runbook_moves_an_object_onto_a_name_that_is_the_same_every_time():
    commands = [line.strip() for block in all_fenced_blocks() for line in block.splitlines() if "rclone moveto" in line]
    assert len(commands) == 1, commands                                                     # the one move there is: step 4
    only = commands[0]
    assert "--ignore-existing" in only and "--error-on-no-transfer" in only and '"$REMOTE/$SRC" "$REMOTE/$DST"' in only
    assert not re.search(r"quarantined['\"\s]*$", only)
    for block in all_fenced_blocks():
        assert not re.search(r"\.quarantined(?![-\w])", block), block                        # only ever `.quarantined-<time>-<hex>`


def test_the_text_states_the_hazard_the_remedy_the_limits_and_what_is_to_be_done_when_the_result_is_unclear():
    text = " ".join(procedure_text().split())
    for needed in (
        "a plain `rclone moveto A B` **replaces an existing `B` and exits 0**", "`--ignore-existing` makes `moveto` skip instead",
        "**exit 9**", "Nothing here shows that the skip is atomic", "**only the listing taken afterwards, never rclone's exit status**",
        "**not** against Yandex Disk", "Stop the **timer**", "waited for, not killed", "never a wildcard, a pattern or a directory",
        "`BEFORE` must be **one line**", "never used", "Do not run this step a second time", "A skipped move is **not** a success",
        "do not delete either object", "do not rename anything back", "the owner decides", "A run that meets a record without its object deletes that",
        "a missing backup costs more than an exit 6", "in one shell session",
    ):
        assert needed in text, needed
    assert text.count("atomic") == 1                           # said once, and only as the thing that is NOT shown


def test_both_quarantine_procedures_and_the_suspect_case_use_the_one_procedure():
    text = " ".join(read(RUNBOOK).split())
    review = text[text.index("### 15.11 Operator review"):]
    assert review.count("**Quarantining one object safely**") >= 4 and "steps 1 to 6 for the object" in review
    assert "with steps 2 to 6 of **Quarantining one object safely**" in review                  # the .partial
    assert "Quarantine it with **Quarantining one object safely** above (object **and** its `.commit`" in review   # the SUSPECT point
    assert "(never a fixed `.quarantined`)" in review
    assert "`systemctl is-active house-helper-offsite-backup.service` must print `inactive`" not in review          # the old, weaker check


def test_the_runbook_pins_what_was_seen_on_the_real_binary():
    text = " ".join(read(RUNBOOK).split())
    section = text[text.index("The manual quarantine of 15.11, against the same binary"):]
    section = section[:section.index("rclone's saving of a changed config")]
    for needed in ("replaced it and exited 0", "left both objects as they were and exited 0", "exited **9**", "a missing source exited 1",
                   "including a destination that appears between the check and the move", "does **not** show that `--ignore-existing` is atomic"):
        assert needed in section, needed


def test_a_quarantine_name_of_the_procedure_is_not_one_of_the_tools_shapes_and_the_tool_leaves_it_alone():
    for source in (OBJECT, PARTIAL, RECORD):
        name = source + SUFFIX
        assert offsite.parse_point_name(name) is None
        assert not offsite.PARTIAL_RE.fullmatch(name) and not offsite.COMMIT_RE.fullmatch(name) and not offsite.POINT_RE.fullmatch(name)
    entries = [offsite.Entry(OBJECT + SUFFIX, 10, {"md5": "0" * 32}), offsite.Entry(RECORD + SUFFIX, 10, {}), offsite.Entry(PARTIAL + SUFFIX, 5, {})]
    folder = offsite.classify_folder(entries, lambda name: b"")
    assert folder.committed == [] and folder.unresolved == {} and folder.stray_records == []
    assert sorted(folder.others) == sorted(e.name for e in entries)
    assert offsite.leftover_partials(entries) == []


def test_after_the_procedure_the_next_run_of_the_tool_is_healthy_and_leaves_the_quarantined_partial_alone(tmp_path):
    rig = Rig(tmp_path)
    partial = FIXED_NAME.replace("0a1b2c3d", "11111111") + ".partial"
    rig.fake.put_remote(partial, b"foreign")
    assert rig.run() == offsite.EXIT_RETENTION, rig.text
    path = rig.fake.folder() / partial
    path.rename(path.with_name(path.name + SUFFIX))                                       # what the procedure's step 4 does
    rig.log.clear()
    rig.token, rig.now = "00000002", rig.now.replace(day=26)
    assert rig.run() == offsite.EXIT_OK, rig.text
    assert rig.fake.remote_files()[partial + SUFFIX] == b"foreign" and "LEFTOVER PARTIAL" not in rig.text


# --- running the RUNBOOK's blocks -------------------------------------------------------------------------------------------------


FAKE_RCLONE = '''#!{python}
"""A stand-in for rclone on its local backend, for the two commands the procedure uses, as rclone 1.75.2 was seen to behave."""
import hashlib, json, os, sys
from pathlib import Path

args = sys.argv[1:]
command, flags, operands, skip = args[0], set(), [], False
for item in args[1:]:
    if skip:
        skip = False
    elif item == "--config":
        skip = True
    elif item.startswith("--"):
        flags.add(item)
    else:
        operands.append(item)
path = lambda operand: Path(operand.split(":", 1)[1])
if command == "lsjson":
    assert flags == {{"--files-only", "--hash"}}, flags
    folder = path(operands[0])
    if not folder.is_dir():
        sys.stderr.write("directory not found\\n")
        sys.exit(3)
    items = [{{"Path": p.name, "Name": p.name, "Size": p.stat().st_size, "IsDir": False,
              "Hashes": {{"md5": hashlib.md5(p.read_bytes()).hexdigest()}}}} for p in sorted(folder.iterdir()) if p.is_file()]
    print(json.dumps(items))
elif command == "moveto":
    source, destination = path(operands[0]), path(operands[1])
    if not source.exists():
        sys.stderr.write("no such file or directory\\n")
        sys.exit(1)
    if destination.exists() and "--ignore-existing" in flags:
        sys.exit(9 if "--error-on-no-transfer" in flags else 0)    # skipped: nothing is moved, nothing is replaced
    os.replace(source, destination)                                 # without the flag an existing destination is REPLACED, exit 0
else:
    sys.exit(2)
'''

WRAPPER = """#!/usr/bin/env bash
echo "$*" >> "$SHIM_LOG"
if [ "$1" = moveto ] && [ -n "${SHIM_RACE_FILE:-}" ] && [ ! -e "$SHIM_RACE_FILE" ]; then
  printf '%s' "${SHIM_RACE_BYTES:-RACER}" > "$SHIM_RACE_FILE"      # a writer comes in between the check and the move
fi
exec "$REAL_RCLONE" "$@"
"""

SHIMS = {
    "sudo": '#!/usr/bin/env bash\nexec "$@"\n',
    "systemctl": (
        '#!/usr/bin/env bash\necho "systemctl $*" >> "$SHIM_LOG"\n'
        'case "$1" in\n'
        '  is-active) case "$2" in *.service) echo "${SHIM_SERVICE_STATE:-inactive}";; *) echo "${SHIM_TIMER_STATE:-inactive}";; esac; exit 3;;\n'
        '  *) exit 0;;\nesac\n'
    ),
    "pgrep": '#!/usr/bin/env bash\nexit "${SHIM_PGREP:-1}"\n',
    "date": '#!/usr/bin/env bash\nif [ "$*" = "-u +%Y%m%dT%H%M%SZ" ]; then echo "$SHIM_STAMP"; else exec /bin/date "$@"; fi\n',
    "python3": '#!/usr/bin/env bash\ncase "$*" in *secrets.token_hex*) echo "$SHIM_TOKEN";; *) exec "$REAL_PYTHON" "$@";; esac\n',
}


class Outcome(NamedTuple):
    stdout: str
    stderr: str
    folder: Dict[str, bytes]
    commands: List[str]          # what the (wrapped) rclone was asked, one line each
    work: Path


def put_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_procedure(
    tmp_path: Path, rclone: str, objects: Dict[str, bytes], src: str, *, upto: int = 5, folder_exists: bool = True,
    race: Optional[str] = None, service: str = "inactive", timer: str = "inactive", pgrep: int = 1, token: str = TOKEN,
) -> Outcome:
    """Paste the first `upto` blocks of the procedure into one bash session, with `src` as the chosen name."""
    work = tmp_path / "work"
    work.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (work / "rclone.conf").write_text("[fake]\ntype = local\n")
    folder = work / "house-helper"
    if folder_exists:
        folder.mkdir()
        for name, data in objects.items():
            (folder / name).write_bytes(data)
    for name, text in SHIMS.items():
        put_executable(bin_dir / name, text)
    put_executable(bin_dir / "rclone-fake", FAKE_RCLONE.format(python=sys.executable))
    put_executable(bin_dir / "rclone", WRAPPER)

    script = "\n".join(blocks()[:upto])
    for old, new in (
        ("CONF=/etc/house-helper/rclone.conf", f"CONF={work / 'rclone.conf'}"),
        ("REMOTE='NAME:house-helper'", "REMOTE='fake:house-helper'"),
        ("SRC='house-helper-<time>-<id>.tar.age.partial'", f"SRC='{src}'"),
    ):
        assert old in script, old                                                         # the RUNBOOK still has the line this test replaces
        script = script.replace(old, new)
    script_path = tmp_path / "procedure.sh"
    script_path.write_text(script)

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path), "LC_ALL": "C",
        "SHIM_LOG": str(tmp_path / "rclone.log"), "SHIM_STAMP": STAMP, "SHIM_TOKEN": token, "REAL_PYTHON": sys.executable,
        "REAL_RCLONE": BINARY if rclone == "real" else str(bin_dir / "rclone-fake"),
        "SHIM_SERVICE_STATE": service, "SHIM_TIMER_STATE": timer, "SHIM_PGREP": str(pgrep),
    }
    if race:
        env["SHIM_RACE_FILE"] = str(folder / race)
    done = subprocess.run([BASH, str(script_path)], cwd=work, env=env, capture_output=True, text=True, timeout=120)
    log = tmp_path / "rclone.log"
    after = {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()} if folder.is_dir() else {}
    return Outcome(done.stdout, done.stderr, after, log.read_text().splitlines() if log.exists() else [], work)


def sub(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def moves(outcome: Outcome) -> List[str]:
    return [line for line in outcome.commands if line.startswith("moveto")]


ORIGINAL = b"the bytes of the object that is being quarantined"
EARLIER = b"an object that was quarantined earlier, and must never be replaced"


@posix_only
@needs_bash
@pytest.mark.parametrize("rclone", RCLONES)
class TestTheProcedureRunAsWritten:
    def test_a_clean_quarantine_moves_the_object_to_a_new_name_and_proves_it(self, tmp_path, rclone):
        out = run_procedure(tmp_path, rclone, {PARTIAL: ORIGINAL}, PARTIAL)
        assert "IDLE: nothing is scheduled or running" in out.stdout and "name ok" in out.stdout
        assert f"{PARTIAL} {len(ORIGINAL)} " in out.stdout and "free: no object of that name" in out.stdout
        assert "rclone exit: 0" in out.stdout and "MOVED:" in out.stdout and "AMBIGUOUS" not in out.stdout
        assert out.folder == {PARTIAL + SUFFIX: ORIGINAL}                                  # moved, byte for byte, to a name never used before
        [move] = moves(out)
        assert "--ignore-existing" in move and "--error-on-no-transfer" in move and move.endswith(f"fake:house-helper/{PARTIAL} fake:house-helper/{PARTIAL + SUFFIX}")
        assert "systemctl stop house-helper-offsite-backup.timer" in out.commands                # the schedule was stopped first

    def test_an_object_and_then_its_record_each_get_their_own_destination(self, tmp_path, rclone):
        record = b'{"a": "record"}'
        first = run_procedure(sub(tmp_path, "one"), rclone, {OBJECT: ORIGINAL, RECORD: record}, OBJECT)
        assert "MOVED:" in first.stdout
        second = run_procedure(sub(tmp_path, "two"), rclone, first.folder, RECORD)           # round two starts from the folder round one left
        assert "MOVED:" in second.stdout
        assert second.folder == {OBJECT + SUFFIX: ORIGINAL, RECORD + SUFFIX: record}

    def test_a_destination_that_already_exists_is_detected_before_the_move_and_nothing_is_sent_to_rclone(self, tmp_path, rclone):
        out = run_procedure(tmp_path, rclone, {PARTIAL: ORIGINAL, PARTIAL + SUFFIX: EARLIER}, PARTIAL)
        assert "TAKEN, or the listing failed: do not continue" in out.stdout and "free:" not in out.stdout
        assert "NOT RUN" in out.stdout and "AMBIGUOUS" in out.stdout and "MOVED" not in out.stdout
        assert moves(out) == []                                                           # not even attempted
        assert out.folder == {PARTIAL: ORIGINAL, PARTIAL + SUFFIX: EARLIER}                # nothing replaced, nothing moved

    def test_a_destination_that_appears_between_the_check_and_the_move_is_not_replaced_and_the_result_is_ambiguous(self, tmp_path, rclone):
        """THE collision: the check said free, then a writer created the destination, then the move ran. The real 1.75.2 skips it
        (exit 9 under --error-on-no-transfer), leaves both objects, and the listing afterwards says AMBIGUOUS: no success is claimed."""
        out = run_procedure(tmp_path, rclone, {PARTIAL: ORIGINAL}, PARTIAL, race=PARTIAL + SUFFIX)
        assert "free: no object of that name" in out.stdout                                # the check passed ...
        assert "rclone exit: 9" in out.stdout                                              # ... the move skipped, loudly ...
        assert "AMBIGUOUS: go to step 6; do not repeat the move" in out.stdout and "MOVED" not in out.stdout   # ... and is not a success
        assert out.folder == {PARTIAL: ORIGINAL, PARTIAL + SUFFIX: b"RACER"}              # the source stays; the writer's object is untouched
        assert len(moves(out)) == 1

    @pytest.mark.parametrize("state", [("service", "activating"), ("service", "active"), ("timer", "active"), ("pgrep", 0)], ids=lambda s: f"{s[0]}={s[1]}")
    def test_while_a_backup_is_scheduled_or_running_the_move_is_not_run(self, tmp_path, rclone, state):
        out = run_procedure(tmp_path, rclone, {PARTIAL: ORIGINAL}, PARTIAL, **{state[0]: state[1]})
        assert "NOT IDLE: do not continue" in out.stdout and "IDLE: nothing" not in out.stdout
        assert "NOT RUN: the schedule is not idle" in out.stdout and moves(out) == []
        assert out.folder == {PARTIAL: ORIGINAL}

    @pytest.mark.parametrize(
        "src",
        ["", "house-helper-*", "house-helper-20261025T041507Z-0a1b2c3d.tar.age.*", "*", OBJECT + ".quarantined", OBJECT + " ",
         "../" + OBJECT, "house-helper-20261025T041507Z-0A1B2C3D.tar.age", "house-helper-20261025T041507Z-0a1b2c3d.tar.age.partial.partial", "house-helper"],
        ids=["empty", "glob 1", "glob 2", "star", "a quarantined name", "trailing space", "path", "upper case", "double suffix", "the folder"],
    )
    def test_only_an_exact_name_of_the_tools_shape_is_ever_moved_never_a_wildcard(self, tmp_path, rclone, src):
        objects = {OBJECT: ORIGINAL, PARTIAL: b"p", RECORD: b"r", "house-helper-20261025T041507Z-0a1b2c3e.tar.age": b"other"}
        if src and "/" not in src:
            objects[src] = b"an object that really is called that: the listing finds it, and only the shape of its name keeps it from being moved"
        out = run_procedure(tmp_path, rclone, objects, src)
        assert "NOT an exact object name of the tool's shape: do not continue" in out.stdout and "NOT RUN" in out.stdout
        assert moves(out) == [] and out.folder == objects

    def test_a_name_that_is_not_in_the_folder_is_not_moved(self, tmp_path, rclone):
        out = run_procedure(tmp_path, rclone, {OBJECT: ORIGINAL}, PARTIAL)                 # exact, but not there
        assert "NOT RUN" in out.stdout and moves(out) == [] and out.folder == {OBJECT: ORIGINAL}

    def test_a_listing_that_fails_is_never_taken_for_a_free_destination(self, tmp_path, rclone):
        out = run_procedure(tmp_path, rclone, {}, PARTIAL, folder_exists=False)
        assert "TAKEN, or the listing failed: do not continue" in out.stdout and "NOT RUN" in out.stdout and moves(out) == []

    def test_a_second_quarantine_of_a_name_at_the_same_second_and_digits_cannot_replace_the_first(self, tmp_path, rclone):
        """The old fixed destination destroyed exactly this. Same second, same digits (a shimmed clock and token): the check sees the
        first quarantine and refuses; with a fresh token the second goes to its own name and the first is untouched."""
        first = run_procedure(sub(tmp_path, "a"), rclone, {PARTIAL: ORIGINAL}, PARTIAL)
        assert first.folder == {PARTIAL + SUFFIX: ORIGINAL}
        again = {PARTIAL: b"a new partial of the same name", PARTIAL + SUFFIX: first.folder[PARTIAL + SUFFIX]}
        refused = run_procedure(sub(tmp_path, "b"), rclone, again, PARTIAL)
        assert moves(refused) == [] and refused.folder == again and "TAKEN" in refused.stdout
        fresh = run_procedure(sub(tmp_path, "c"), rclone, again, PARTIAL, token="a1b2c3d4")
        assert fresh.folder == {PARTIAL + SUFFIX: ORIGINAL, PARTIAL + f".quarantined-{STAMP}-a1b2c3d4": b"a new partial of the same name"}


# --- the stand-in and the real binary agree on what the procedure depends on ------------------------------------------------------


def rclone_exec(tmp_path: Path, kind: str) -> str:
    if kind == "real":
        return BINARY
    path = tmp_path / "rclone-fake"
    put_executable(path, FAKE_RCLONE.format(python=sys.executable))
    return str(path)


@posix_only
@pytest.mark.parametrize("rclone", RCLONES)
class TestWhatTheProcedureDependsOn:
    """The facts of RUNBOOK 15.12.6, one by one, on the stand-in and on the real binary: the stand-in is only as good as its agreement."""

    def setup_folder(self, tmp_path: Path):
        (tmp_path / "rclone.conf").write_text("[fake]\ntype = local\n")
        folder = tmp_path / "house-helper"
        folder.mkdir()
        (folder / "source").write_bytes(b"SOURCE BYTES 123456")
        (folder / "existing").write_bytes(b"EXISTING OTHER")
        return folder

    def move(self, tmp_path: Path, kind: str, *flags: str, source: str = "source") -> int:
        return subprocess.run(
            [rclone_exec(tmp_path, kind), "moveto", "--config", str(tmp_path / "rclone.conf"), *flags, f"fake:house-helper/{source}", "fake:house-helper/existing"],
            cwd=tmp_path, capture_output=True, timeout=60,
        ).returncode

    def test_a_plain_moveto_onto_an_existing_destination_replaces_it_and_exits_0(self, tmp_path, rclone):
        folder = self.setup_folder(tmp_path)
        assert self.move(tmp_path, rclone) == 0
        assert (folder / "existing").read_bytes() == b"SOURCE BYTES 123456" and not (folder / "source").exists()   # the old runbook's command

    def test_ignore_existing_skips_and_still_exits_0(self, tmp_path, rclone):
        folder = self.setup_folder(tmp_path)
        assert self.move(tmp_path, rclone, "--ignore-existing") == 0
        assert (folder / "existing").read_bytes() == b"EXISTING OTHER" and (folder / "source").read_bytes() == b"SOURCE BYTES 123456"

    def test_ignore_existing_with_error_on_no_transfer_skips_and_exits_9(self, tmp_path, rclone):
        folder = self.setup_folder(tmp_path)
        assert self.move(tmp_path, rclone, "--ignore-existing", "--error-on-no-transfer") == 9
        assert (folder / "existing").read_bytes() == b"EXISTING OTHER" and (folder / "source").read_bytes() == b"SOURCE BYTES 123456"

    def test_the_same_flags_move_when_the_destination_is_free_and_fail_when_the_source_is_missing(self, tmp_path, rclone):
        folder = self.setup_folder(tmp_path)
        (folder / "existing").unlink()
        assert self.move(tmp_path, rclone, "--ignore-existing", "--error-on-no-transfer") == 0
        assert (folder / "existing").read_bytes() == b"SOURCE BYTES 123456" and not (folder / "source").exists()
        assert self.move(tmp_path, rclone, "--ignore-existing", "--error-on-no-transfer") == 1       # nothing left to move

    def test_a_missing_folder_is_an_error_of_the_listing_with_exit_3(self, tmp_path, rclone):
        (tmp_path / "rclone.conf").write_text("[fake]\ntype = local\n")
        done = subprocess.run([rclone_exec(tmp_path, rclone), "lsjson", "--config", str(tmp_path / "rclone.conf"), "--files-only", "--hash", "fake:nothing-here"],
                              cwd=tmp_path, capture_output=True, timeout=60)
        assert done.returncode == 3

    def test_the_listing_carries_the_name_size_and_a_lower_case_md5_the_procedure_compares(self, tmp_path, rclone):
        self.setup_folder(tmp_path)
        done = subprocess.run([rclone_exec(tmp_path, rclone), "lsjson", "--config", str(tmp_path / "rclone.conf"), "--files-only", "--hash", "fake:house-helper"],
                              cwd=tmp_path, capture_output=True, timeout=60)
        items = {i["Name"]: i for i in json.loads(done.stdout)}
        assert done.returncode == 0 and items["source"]["Size"] == 19
        assert {k.lower(): v for k, v in items["source"]["Hashes"].items()}["md5"] == hashlib.md5(b"SOURCE BYTES 123456").hexdigest()
