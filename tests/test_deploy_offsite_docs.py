"""What the RUNBOOK and the decision register promise about the temporary full-disk mode, the age key and the restore rehearsal,
checked against the code that implements them (RUNBOOK 15.4, 15.12, 15.13; KHD-33). Everything here READS files.
"""

import re
import shlex
from pathlib import Path

import pytest

from tests.offsite_support import load_rehearsal, offsite
from tests.test_deploy_kit import DEPLOY, REPO_ROOT, RUNBOOK, read

rehearsal = load_rehearsal()
DECISIONS = REPO_ROOT / "docs" / "DECISIONS.md"


def section_15() -> str:
    text = read(RUNBOOK)
    return text[text.index("## 15. Encrypted off-host backup"):]


def part(heading: str, until: str) -> str:
    body = section_15()
    start = body.index(heading)
    return body[start:body.index(until, start + len(heading))] if until in body[start + len(heading):] else body[start:]


# --- the age key is made outside the checkout --------------------------------------------------------------------


def test_the_runbook_makes_the_private_key_outside_the_checkout_by_an_absolute_path_with_a_private_umask():
    key_step = part("1. **The key pair, made on the owner's own machine", "2. **A Yandex OAuth application")
    assert "never on the VPS and never inside a checkout" in key_step
    assert "umask 077" in key_step
    assert 'age-keygen -o "$HOME/keys/house-helper/age-key.txt"' in key_step
    assert ".gitignore" in key_step and "tests/test_repo_secrets.py" in key_step


def test_no_age_keygen_command_in_the_runbook_writes_a_bare_file_name_except_the_one_warning_against_it():
    text = read(RUNBOOK)
    for match in re.finditer(r'age-keygen -o ([^\s`]+)', text):
        target = match.group(1)
        if target.startswith(('"$HOME/', '$HOME/', "/", "~/")):
            continue
        before = " ".join(text[max(0, match.start() - 300):match.start()].split())
        assert "bare file name" in before, target                                        # only as the example of what not to do


def test_every_age_decrypt_command_of_the_runbook_names_the_key_by_an_absolute_path():
    text = read(RUNBOOK)
    commands = re.findall(r"age -d -i (\S+)", text)
    assert commands and all(c.startswith(('"$HOME/', "/", "~/")) for c in commands), commands


# --- the commands the runbook gives parse with the real parsers ------------------------------------------------------


def commands_in(text: str, script: str):
    for line in text.splitlines():
        stripped = line.strip()
        if script in stripped and stripped.startswith(("sudo python3", "python3")):
            yield shlex.split(re.sub(r"^(sudo )?python3 \S+", "", stripped).split("#")[0])


def test_the_rehearsal_commands_of_the_runbook_parse_with_the_real_rehearsal_parser():
    seen = list(commands_in(part("### 15.13 ", "\n### 15.14"), "deploy/offsite_rehearsal.py"))
    assert len(seen) >= 2
    parsed = [rehearsal._build_parser().parse_args(argv) for argv in seen]
    assert {p.access_mode for p in parsed} == {offsite.ACCESS_APP_FOLDER, offsite.ACCESS_FULL_DISK}
    assert all(p.expect_rows for p in parsed)
    full = next(p for p in parsed if p.access_mode == offsite.ACCESS_FULL_DISK)
    assert offsite.validate_remote(full.remote, offsite.ACCESS_FULL_DISK)               # the example folder is the one folder


def test_the_backup_commands_of_the_full_disk_section_parse_and_pass_the_validator():
    seen = list(commands_in(part("### 15.12 ", "### 15.13"), "deploy/offsite_backup.py"))
    assert seen
    for argv in seen:
        args = offsite._build_parser().parse_args(argv)
        assert args.access_mode == offsite.ACCESS_FULL_DISK
        assert offsite.validate_remote(args.remote, args.access_mode)


# --- the contract in the runbook is the contract in the code -------------------------------------------------------------


def test_the_runbook_states_the_authorization_contract_the_code_enforces():
    text = " ".join(part("### 15.12 ", "### 15.13").split())
    for needed in (
        f"--access-mode {offsite.ACCESS_FULL_DISK}", f"NAME:{offsite.FULL_DISK_PREFIX}", "authorize_remote", "before any command runs, rclone included",
        "app_folder = true", "contradiction", "owned by root", "not writable by group or others", "single** hard link",
        "--ignore-existing", "rclone's built-in application", "namei -l", "ReadWritePaths=/etc/house-helper",
        "writing a new file beside the old one and renaming it over it", "15.12.9", "Revoke", "TEMPORARY",
    ):
        assert needed in text, needed
    assert offsite.CONFIG_LIMIT == 1 << 20 and "at most 1 MiB" in text
    assert offsite.DEFAULT_TRUST.uid == 0 and offsite.DEFAULT_TRUST.root == Path("/")


def test_the_runbook_says_what_was_and_was_not_verified_against_a_real_rclone():
    text = " ".join(part("#### 15.12.6", "#### 15.12.7").split())
    for needed in ("rclone/rclone:1.75.2", "sha256:2687085f", "no `app_folder`", "without any network request", "--ignore-existing",
                   "Not verified: anything about Yandex Disk itself", "HOUSE_HELPER_REAL_RCLONE"):
        assert needed in text, needed


def test_the_rehearsal_exit_statuses_of_the_runbook_are_the_ones_the_code_uses():
    table = part("| Exit | Meaning |", "**What it does not replace.**")
    rows = dict(re.findall(r"^\| (\d) \| (.*) \|$", table, re.MULTILINE))
    assert set(rows) == {"0", "1", "2", "3", "4"}
    assert (rehearsal.EXIT_OK, rehearsal.EXIT_FAILED, rehearsal.EXIT_USAGE, rehearsal.EXIT_REFUSED, rehearsal.EXIT_NOT_RESTORABLE) == (0, 1, 2, 3, 4)
    assert "not restorable" in rows["4"].lower() and "only status that says the point can be restored" in rows["0"]


def test_the_monitoring_interface_names_the_unit_the_statuses_and_adds_none():
    text = " ".join(part("#### 15.12.10", "### 15.13").split())
    assert "house-helper-offsite-backup.service" in text and "SyslogIdentifier=house-helper-offsite" in text
    assert "no new code" in text and "Only 0 is success; 6 is never a success heartbeat" in text
    codes = {offsite.EXIT_OK, offsite.EXIT_FAILED, offsite.EXIT_USAGE, offsite.EXIT_REFUSED, offsite.EXIT_RETENTION, offsite.EXIT_UNCONFIRMED}
    assert codes == {0, 1, 2, 3, 6, 7}                          # the same six as before this change: nothing was added
    assert not hasattr(offsite, "EXIT_FULL_DISK")


def test_every_reference_to_a_subsection_of_15_12_points_at_a_heading_that_exists():
    headings = set(re.findall(r"^#### 15\.12\.(\d+)", read(RUNBOOK), re.MULTILINE))
    assert headings == {str(i) for i in range(1, 11)}
    mentioned = set()
    for path in (RUNBOOK, DEPLOY / "systemd" / "house-helper-offsite.env.example", DEPLOY / "systemd" / "house-helper-offsite-backup.service",
                 DEPLOY / "offsite_backup.py", DEPLOY / "offsite_rehearsal.py"):
        mentioned |= set(re.findall(r"15\.12\.(\d+)", read(path)))
    assert mentioned <= headings, mentioned - headings
    assert "### 15.13 Restore rehearsal" in read(RUNBOOK)


def test_the_status_paragraph_of_section_15_still_says_nothing_is_installed_and_names_the_real_rclone_limit():
    text = " ".join(section_15().split())
    assert "Nothing is installed, scheduled or deployed" in text
    assert "its `rclone` commands have run only offline against a real, pinned stable `rclone`" in text
    assert "No OAuth credential and no private key exists in the repository" in text


# --- the decision register -------------------------------------------------------------------------------------------------


def test_the_decision_register_records_the_exception_as_temporary_conditional_and_not_activated():
    text = " ".join(read(DECISIONS).split())
    row = text[text.index("| **KHD-33**"):]
    row = row[:row.index("### Why / alternatives / boundaries")]
    for needed in ("temporarily and only when explicitly asked for", "--access-mode full-disk-temporary", "house-helper-backup", "owner's own OAuth application",
                   "only root can change", "public keys only", "ends when a stable `rclone` with the `app_folder` option exists", "not activated"):
        assert needed in row, needed
    assert "KHD-33" not in text[:text.index("## Accepted exception")]            # a new decision, not a rewrite of an old one
    assert "decided it again" in text or "decided again by the owner" in text


def test_the_old_rule_that_the_exception_replaces_in_part_is_still_in_the_runbook_with_the_pointer():
    text = " ".join(read(RUNBOOK).split())
    assert "an rclone that cannot be made to work with the application-folder scope means this design waits, or **is decided again by the owner**" in text
    assert "The owner has decided it again, for a limited time and only under the conditions of 15.12 (KHD-33)" in text
