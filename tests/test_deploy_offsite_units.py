"""The systemd units and the runbook of the off-host backup, and how they meet the orchestrator.

Everything here READS files. Nothing installs, enables or starts a timer or a service. The command line the
service runs is parsed with the real argument parser of deploy/offsite_backup.py (so a renamed option fails
here and not at 04:15), and the commands the runbook tells the owner to type are parsed the same way.
"""

import re
import shlex
from datetime import timedelta
from pathlib import Path

import pytest

from tests.offsite_support import offsite
from tests.test_deploy_kit import DEPLOY, REPO_ROOT, RUNBOOK, read, section
from tests.test_deploy_scheduler import SERVICE as PURGE_SERVICE
from tests.test_deploy_scheduler import TIMER as PURGE_TIMER
from tests.test_deploy_scheduler import ENVIRONMENT as PURGE_ENVIRONMENT
from tests.test_deploy_scheduler import UNITS, parse_unit, values

SERVICE = UNITS / "house-helper-offsite-backup.service"
TIMER = UNITS / "house-helper-offsite-backup.timer"
ENVIRONMENT = UNITS / "house-helper-offsite.env.example"
SCRIPT = DEPLOY / "offsite_backup.py"
REHEARSAL = DEPLOY / "offsite_rehearsal.py"


def template_values(path) -> dict:
    return dict(re.findall(r"^(\w+)=(.*)$", read(path), re.MULTILINE))


# --- the files ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [SERVICE, TIMER, ENVIRONMENT, SCRIPT, REHEARSAL], ids=lambda p: p.name)
def test_the_files_are_lf_ascii_text_that_end_in_a_newline(path):
    data = path.read_bytes()
    assert b"\r" not in data and not data.startswith(b"\xef\xbb\xbf") and data.endswith(b"\n")
    data.decode("ascii")


def test_the_gitattributes_keep_the_script_and_the_units_lf_on_a_windows_checkout():
    attributes = read(DEPLOY / ".gitattributes")
    assert "systemd/* text eol=lf" in attributes and "offsite_backup.py text eol=lf" in attributes
    assert "offsite_rehearsal.py text eol=lf" in attributes


def test_the_script_and_the_units_are_not_part_of_the_image_or_its_build_context():
    for name in ("Dockerfile", ".dockerignore"):
        text = read(REPO_ROOT / name)
        assert "offsite" not in text and "systemd" not in text, name


# --- the service ---------------------------------------------------------------------------------------


def test_the_service_is_a_oneshot_that_cannot_be_enabled_on_its_own_and_does_not_restart():
    unit = parse_unit(SERVICE)
    assert set(unit) == {"Unit", "Service"}  # no [Install]: only the timer is enabled
    assert values(unit, "Service", "Type") == ["oneshot"]
    assert len(values(unit, "Service", "ExecStart")) == 1
    for forbidden in ("Restart", "ExecStartPost", "ExecStop", "ExecReload", "RemainAfterExit", "WatchdogSec", "User"):
        assert not values(unit, "Service", forbidden), forbidden
    assert values(unit, "Service", "NoNewPrivileges") == ["yes"]
    assert values(unit, "Service", "PrivateTmp") == ["yes"]
    assert values(unit, "Service", "UMask") == ["0077"]
    assert re.fullmatch(r"[0-9]+(min|h|s)", values(unit, "Service", "TimeoutStartSec")[0])


def test_the_service_reads_the_cleanups_environment_first_and_its_own_second_and_neither_is_optional():
    unit = parse_unit(SERVICE)
    # No leading "-": a missing file fails the unit at once, visibly, instead of running with half a configuration.
    assert values(unit, "Service", "EnvironmentFile") == ["/etc/house-helper/purge.env", "/etc/house-helper/offsite.env"]


def test_the_service_waits_for_docker_and_fails_at_once_if_it_is_not_up():
    unit = parse_unit(SERVICE)
    assert "docker.service" in values(unit, "Unit", "After")[0].split()
    assert values(unit, "Unit", "Requisite") == ["docker.service"]  # not Requires=: it never starts Docker
    assert not values(unit, "Unit", "Requires")


def test_a_stop_lets_the_tool_clean_up_before_anything_is_killed():
    unit = parse_unit(SERVICE)
    assert values(unit, "Service", "KillMode") == ["mixed"]  # SIGTERM to the tool only; its children are stopped by it
    stop = values(unit, "Service", "TimeoutStopSec")[0]
    assert stop.endswith("min") and int(stop[:-3]) >= 2  # long enough for the clean-up's own timeouts


def test_the_plaintext_directory_is_tmpfs_private_and_the_one_the_command_names():
    unit = parse_unit(SERVICE)
    assert values(unit, "Service", "RuntimeDirectory") == ["house-helper-offsite"]
    assert values(unit, "Service", "RuntimeDirectoryMode") == ["0700"]
    args = parsed_command()
    assert args.work_dir == Path("/run/house-helper-offsite")  # RuntimeDirectory= puts it under /run


def substitute(command: str) -> str:
    defined = {**template_values(PURGE_ENVIRONMENT), **template_values(ENVIRONMENT)}
    return re.sub(r"\$\{(\w+)\}", lambda m: defined[m.group(1)], command)


def parsed_command():
    command = values(parse_unit(SERVICE), "Service", "ExecStart")[0]
    argv = shlex.split(substitute(command))
    assert argv[0] == "/usr/bin/python3"
    assert argv[1].endswith("/deploy/offsite_backup.py")
    return offsite._build_parser().parse_args(argv[2:])


def test_the_command_line_parses_with_the_real_parser_and_names_a_run_with_every_required_option():
    command = values(parse_unit(SERVICE), "Service", "ExecStart")[0]
    args = parsed_command()
    assert args.command == "run"
    assert args.max_deletions == offsite.DEFAULT_MAX_DELETIONS
    # Everything that makes it a daily job and nothing that bends it: no shell, no override, no wide limit.
    for forbidden in ("sh -c", "bash", "--max-deletions", ";", "&&", "|", ">"):
        assert forbidden not in f" {command} ", forbidden
    # The template values are of a shape the tool accepts (so the placeholders fail at rclone, not at parsing).
    assert offsite.TAG_RE.fullmatch(args.tag) and offsite.validate_remote(args.remote)


def test_the_variables_the_service_uses_are_all_defined_by_the_two_templates_and_only_braced():
    command = values(parse_unit(SERVICE), "Service", "ExecStart")[0]
    used = set(re.findall(r"\$\{(\w+)\}", command))
    assert not re.search(r"\$(?!\{)", command), "only braced substitutions: they are not split at spaces"
    purge, offsite_env = set(template_values(PURGE_ENVIRONMENT)), set(template_values(ENVIRONMENT))
    assert used <= purge | offsite_env
    assert {"HOUSE_HELPER_CHECKOUT", "HOUSE_HELPER_IMAGE_TAG"} <= used & purge  # the tag is kept in ONE place
    assert not {"HOUSE_HELPER_CHECKOUT", "HOUSE_HELPER_IMAGE_TAG"} & offsite_env
    assert offsite_env == {
        "HOUSE_HELPER_AGE_RECIPIENTS_FILE", "HOUSE_HELPER_RCLONE_CONFIG", "HOUSE_HELPER_OFFSITE_REMOTE",
        "HOUSE_HELPER_OFFSITE_ACCESS_MODE",
    }


def test_the_access_mode_is_app_folder_in_the_template_and_reaches_the_tool_through_the_one_variable():
    # Disabled by default: the template ships the narrow mode, and the unit passes exactly the variable of the template.
    assert template_values(ENVIRONMENT)["HOUSE_HELPER_OFFSITE_ACCESS_MODE"] == offsite.ACCESS_APP_FOLDER
    command = values(parse_unit(SERVICE), "Service", "ExecStart")[0]
    assert "--access-mode ${HOUSE_HELPER_OFFSITE_ACCESS_MODE}" in command
    assert f"--access-mode {offsite.ACCESS_FULL_DISK}" not in command  # the exception is never spelled out in the unit
    assert parsed_command().access_mode == offsite.ACCESS_APP_FOLDER
    # the remote of the template is a folder the default mode accepts; the exception's folder is not the template's
    assert offsite.validate_remote(parsed_command().remote, offsite.ACCESS_APP_FOLDER)
    assert offsite.FULL_DISK_PREFIX not in template_values(ENVIRONMENT)["HOUSE_HELPER_OFFSITE_REMOTE"]
    # the template says, next to the variable, that the other value is a temporary exception and where it is explained
    text = read(ENVIRONMENT)
    assert offsite.ACCESS_FULL_DISK in text and "TEMPORARY" in text and "15.12" in text


def test_the_backup_script_gets_the_env_file_path_it_needs_from_the_cleanups_template():
    # backup.sh runs `docker compose`, which insists that the web service's env file exists (RUNBOOK section 3).
    assert "HOUSE_HELPER_ENV_FILE" in template_values(PURGE_ENVIRONMENT)
    assert "HOUSE_HELPER_ENV_FILE" in read(PURGE_ENVIRONMENT)
    assert "env_file: ${HOUSE_HELPER_ENV_FILE" in read(DEPLOY / "compose.yaml")


def test_the_environment_template_holds_no_secret_and_names_no_default_credential():
    text = read(ENVIRONMENT)
    for word in ("token =", "client_secret", "password"):
        assert word not in text, word
    assert not re.search(r"AGE-SECRET-KEY-1[0-9A-Z]{20}", text)  # the comment may name the prefix; no key is in it
    for name, value in template_values(ENVIRONMENT).items():
        assert " " not in value and not value.startswith(('"', "'")), name  # systemd reads this file itself


# --- the timer --------------------------------------------------------------------------------------------


def clock_of(unit) -> int:
    match = re.fullmatch(r"\*-\*-\* (\d\d):(\d\d):00 UTC", values(unit, "Timer", "OnCalendar")[0])
    assert match, values(unit, "Timer", "OnCalendar")
    return int(match.group(1)) * 60 + int(match.group(2))


def test_the_timer_is_daily_persistent_and_starts_the_service_only():
    unit = parse_unit(TIMER)
    assert values(unit, "Timer", "Persistent") == ["true"]
    assert values(unit, "Timer", "Unit") == ["house-helper-offsite-backup.service"] and SERVICE.is_file()
    assert values(unit, "Install", "WantedBy") == ["timers.target"]
    assert len(values(unit, "Timer", "OnCalendar")) == 1


def test_the_backup_is_scheduled_after_the_cleanup_with_room_for_both_random_delays():
    backup, purge = parse_unit(TIMER), parse_unit(PURGE_TIMER)
    delay = lambda unit: int(re.fullmatch(r"(\d+)min", values(unit, "Timer", "RandomizedDelaySec")[0]).group(1))
    assert clock_of(backup) >= clock_of(purge) + delay(purge) + 5


# --- the runbook ----------------------------------------------------------------------------------------


def test_the_runbook_has_the_section_and_names_every_file_of_the_feature():
    text = read(RUNBOOK)
    for heading in ("## 15. Encrypted off-host backup", "### 15.1 ", "### 15.3 Retention", "### 15.6 Restoring", "### 15.9 "):
        assert heading in text, heading
    for name in (
        "deploy/offsite_backup.py", "house-helper-offsite-backup.service", "house-helper-offsite-backup.timer",
        "house-helper-offsite.env.example", "dbtool.py sanitize",
    ):
        assert name in text, name


def test_the_commands_the_runbook_tells_the_owner_to_type_parse_with_the_real_parser():
    text = read(RUNBOOK)
    start = text.index("## 15.")
    end = text.find("\n## 16.", start)  # (section 16 exists since the Telegram bot: slice section 15 only)
    body = text[start:] if end == -1 else text[start:end]
    lines = [line.strip() for line in body.splitlines() if "deploy/offsite_backup.py" in line and line.lstrip().startswith(("sudo python3", "python3"))]
    assert len(lines) >= 2
    seen = set()
    for line in lines:
        argv = shlex.split(re.sub(r"^(sudo )?python3 \S+", "", line).split("#")[0])
        args = offsite._build_parser().parse_args(argv)
        seen.add(args.command)
    assert {"list", "unpack"} <= seen


def test_the_runbook_states_the_bound_on_deletion_and_that_nothing_is_installed():
    text = " ".join(read(RUNBOOK).split())
    section_15 = text[text.index("## 15. Encrypted off-host backup"):]
    assert "Nothing is installed, scheduled or deployed" in section_15
    assert "up to about 36 days" in section_15
    assert "not forensic" in section_15.replace("secure deletion", "").replace("erasure", "") or "not erasure" in section_15
    assert "--yandex-hard-delete" in section_15


# --- the script's own surface ---------------------------------------------------------------------------


def test_the_script_uses_only_the_rclone_commands_it_is_allowed_and_never_a_shell():
    source = read(SCRIPT)
    commands = set(re.findall(r"self\._call\(\s*[^,]+,\s*\"([a-z]+)\"", source))
    assert commands == {"mkdir", "lsjson", "copyto", "moveto", "deletefile", "backend", "cat"}  # backend: `features`, cat: a commit record; both read only
    assert re.findall(r"\[rclone, \"([a-z]+)\", \"([a-z]+)\"", source) == [("config", "providers")]  # the one other rclone call, offline
    for forbidden in ("shell=True", "os.system", "os.popen", '"purge"', '"cleanup"', '"sync"', "rmdirs", "--drive-use-trash"):
        assert forbidden not in source, forbidden
    assert source.count("Popen(") == 1  # one place runs commands


def test_every_delete_the_script_makes_goes_through_the_one_function_that_confirms_it_by_listing_the_folder():
    source = read(SCRIPT)
    assert source.count(".delete(") == 1                     # self.delete(...) inside Remote.delete_confirmed, nowhere else
    body = source[source.index("    def delete_confirmed"):source.index("class Deleted")]
    assert "self.delete(name, timeout)" in body and 'self.listing("confirming the deletion"' in body
    assert body.index("self.delete(name, timeout)") < body.index('self.listing("confirming the deletion"')
    assert "remote.delete(" not in source and "self.remote.delete(" not in source


def test_the_script_never_asks_age_for_a_passphrase_or_an_identity():
    source = read(SCRIPT)
    commands = re.findall(r"\[self\.tools\[\"age\"\], ([^\]]*)\]", source)
    assert commands == ['"-R", str(self.config.recipients_file)']  # the one place age is run: public keys from a file
    for forbidden in ("--decrypt", "--passphrase", "--identity", "age-keygen"):
        assert forbidden not in source, forbidden


def test_the_runbook_documents_the_commit_record_the_clock_guard_and_the_operator_review():
    text = " ".join(read(RUNBOOK).split())
    section_15 = text[text.index("## 15. Encrypted off-host backup"):]
    for needed in (
        "...tar.age.commit", "A recovery point is an object together with a valid record", "**unresolved**", "UNRESOLVED <name>: <why>",
        "never deleted by the tool", "### 15.11 Operator review", "Quarantine it (reversible)", "`SUSPECT`", "is one budget for the whole run",
        "lists the folder again", "refuses to start (exit 3, nothing done)", "There is deliberately no option that overrides the guard",
    ):
        assert needed in section_15, needed
    # the thresholds the runbook states are the ones the code uses
    assert offsite.FUTURE_TOLERANCE == timedelta(hours=1) and "more than an hour later than the host clock" in section_15
    assert offsite.MAX_FORWARD_GAP == timedelta(days=7) and "more than 7 days after the previous one" in section_15
    assert offsite.MAX_RUN_DURATION == timedelta(hours=3) and "more than 3 hours during the run" in section_15
    # every rclone command the runbook tells the owner to type for a review is one this tool's own surface allows or a plain move/delete of one object
    review = section_15[section_15.index("### 15.11 Operator review"):]
    assert "rclone moveto --config" in review and "rclone deletefile --config" in review and "--yandex-hard-delete" in review


def test_the_documented_numbers_are_the_ones_the_code_uses():
    text = " ".join(read(RUNBOOK).split())
    assert (offsite.KEEP_DAILY, offsite.KEEP_WEEKLY, offsite.DEFAULT_MAX_DELETIONS) == (7, 4, 5)
    assert "**7 most recent UTC days**" in text and "**4 most recent ISO weeks**" in text and "`--max-deletions` (default 5)" in text
    assert f"{offsite.KEEP_DAILY + offsite.KEEP_WEEKLY} points" in text
