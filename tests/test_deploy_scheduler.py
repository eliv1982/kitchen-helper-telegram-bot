"""The systemd units of the cleanup (R4C-D2), and how they meet Compose, the operator tool and
the runbook.

Everything here READS files. Nothing installs, enables or starts a timer or a service, builds
an image or starts a container. The units are checked as text; the command line they run is
parsed with the real parsers of the operator tool and of the administrative command (so a
renamed command or option fails here and not at 03:30); and, where Docker Compose is installed,
the service they name is looked up in the file as Compose itself resolves it (that needs no
daemon and creates nothing).
"""

import os
import re
import shlex
import shutil
import subprocess

import pytest

from house_helper.admin import cli
from tests.deploy_support import dbtool
from tests.test_deploy_kit import (
    COMPOSE,
    DEPLOY,
    REPO_ROOT,
    RUNBOOK,
    SECRET_NAME,
    assert_first_scheduled_run_verified,
    code_lines,
    needs_compose,
    read,
    resolve,
    retention_gate_rows,
    section,
)

STATUS = REPO_ROOT / "docs" / "STATUS.md"
UNITS = DEPLOY / "systemd"
SERVICE = UNITS / "house-helper-purge.service"
TIMER = UNITS / "house-helper-purge.timer"
ENVIRONMENT = UNITS / "house-helper-purge.env.example"

EXEC_START = [
    "/usr/bin/docker", "compose", "-f", "${HOUSE_HELPER_CHECKOUT}/deploy/compose.yaml",
    "--profile", "ops", "run", "--rm", "-T", "house-helper-ops",
    "exec", "--shared", "--", "python", "-m", "house_helper.admin", "purge-expired",
]


def parse_unit(path) -> dict:
    """{section: [(key, value), ...]}: systemd allows a key to repeat, so a list. Comments and
    blank lines are dropped; any other line that is not a section or `key=value` is an error."""
    sections, current = {}, None
    for number, raw in enumerate(read(path).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            assert line.endswith("]"), f"{path.name}:{number}"
            current = sections.setdefault(line[1:-1], [])
            continue
        assert current is not None and "=" in line, f"{path.name}:{number}: {raw!r}"
        key, _, value = line.partition("=")
        current.append((key.strip(), value.strip()))
    return sections


def values(unit: dict, section_name: str, key: str) -> list:
    return [value for name, value in unit.get(section_name, []) if name == key]


# --- the files ----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [SERVICE, TIMER, ENVIRONMENT], ids=lambda p: p.name)
def test_the_files_are_lf_text_without_a_byte_order_mark_and_end_in_a_newline(path):
    data = path.read_bytes()
    assert b"\r" not in data and not data.startswith(b"\xef\xbb\xbf") and data.endswith(b"\n")
    data.decode("ascii")  # plain ASCII: nothing a terminal or systemd could mis-read


def test_the_gitattributes_keep_the_units_lf_on_a_windows_checkout():
    assert "systemd/* text eol=lf" in read(DEPLOY / ".gitattributes")


def test_the_units_are_not_part_of_the_image_or_its_build_context():
    for name in ("Dockerfile", ".dockerignore"):
        assert "systemd" not in read(REPO_ROOT / name), name


# --- the service ------------------------------------------------------------------------------------


def test_the_service_is_a_oneshot_with_one_command_and_cannot_be_enabled_on_its_own():
    unit = parse_unit(SERVICE)
    assert set(unit) == {"Unit", "Service"}  # no [Install]: only the timer is enabled
    assert values(unit, "Service", "Type") == ["oneshot"]
    assert len(values(unit, "Service", "ExecStart")) == 1
    for forbidden in ("Restart", "ExecStartPost", "ExecStop", "ExecReload", "RemainAfterExit", "WatchdogSec"):
        assert not values(unit, "Service", forbidden), forbidden  # a run that fails stays failed
    assert values(unit, "Service", "EnvironmentFile") == ["/etc/house-helper/purge.env"]  # no "-": a missing one fails
    assert values(unit, "Service", "NoNewPrivileges") == ["yes"]
    assert re.fullmatch(r"[0-9]+(min|h|s)", values(unit, "Service", "TimeoutStartSec")[0])


def test_the_service_waits_for_docker_and_fails_at_once_if_it_is_not_up():
    unit = parse_unit(SERVICE)
    assert values(unit, "Unit", "After") == ["docker.service"]
    assert values(unit, "Unit", "Requisite") == ["docker.service"]  # not Requires=: it never starts Docker
    assert not values(unit, "Unit", "Wants") and not values(unit, "Unit", "Requires")


def test_the_command_is_exactly_the_operator_one_shot_under_the_shared_lock_and_nothing_else():
    command = values(parse_unit(SERVICE), "Service", "ExecStart")[0]
    assert shlex.split(command) == EXEC_START
    # No shell, no credential, no extra mount or network, no other entrypoint, no dry run.
    for forbidden in ("sh -c", "bash", "--env", "-e ", "--env-file", "-v ", "--volume", "--network", "--entrypoint",
                      "--exclusive", "--dry-run", " up ", "-d "):
        assert forbidden not in f" {command} ", forbidden


def test_the_command_line_parses_with_the_real_parsers_of_the_operator_tool_and_of_the_command():
    tail = EXEC_START[EXEC_START.index("house-helper-ops") + 1:]
    parser = dbtool._build_parser()
    args = parser.parse_args(tail)
    assert args.command == "exec" and args.shared and not args.exclusive
    program = dbtool._program(args, parser.error)
    assert program == ["python", "-m", "house_helper.admin", "purge-expired"]
    assert os.path.isfile(REPO_ROOT / "house_helper" / "admin" / "__main__.py")  # `python -m house_helper.admin` exists
    admin = cli._build_parser().parse_args(program[3:])
    assert admin.command == "purge-expired" and admin.dry_run is False


def test_the_variables_the_service_uses_are_the_ones_the_template_defines_and_no_others():
    command = values(parse_unit(SERVICE), "Service", "ExecStart")[0]
    used = set(re.findall(r"\$\{(\w+)\}", command))
    assert used == {"HOUSE_HELPER_CHECKOUT"}
    assert not re.search(r"\$(?!\{)", command), "only braced substitutions: they are not split at spaces"
    defined = set(re.findall(r"^(\w+)=", read(ENVIRONMENT), re.MULTILINE))
    assert used <= defined


# --- the environment template -----------------------------------------------------------------------


def test_the_environment_template_names_two_paths_and_a_tag_and_holds_no_secret():
    lines = code_lines(ENVIRONMENT)
    names = [line.partition("=")[0] for line in lines]
    assert names == ["HOUSE_HELPER_CHECKOUT", "HOUSE_HELPER_ENV_FILE", "HOUSE_HELPER_IMAGE_TAG"]
    assert not [n for n in names if SECRET_NAME.search(n)]
    for line in lines:
        value = line.partition("=")[2]
        assert value and " " not in value and '"' not in value and "'" not in value, line
    flat = " ".join(read(ENVIRONMENT).split())
    assert "holds no secret" in flat and "root-owned, mode 0644" in flat
    assert "never its content" in flat  # the environment file is only named


# --- the timer ----------------------------------------------------------------------------------------


def test_the_timer_is_daily_in_utc_persistent_and_starts_only_the_service():
    unit = parse_unit(TIMER)
    assert set(unit) == {"Unit", "Timer", "Install"}
    calendar = values(unit, "Timer", "OnCalendar")
    assert len(calendar) == 1 and re.fullmatch(r"\*-\*-\* ([01][0-9]|2[0-3]):[0-5][0-9]:00 UTC", calendar[0])
    assert values(unit, "Timer", "Persistent") == ["true"]  # a missed run happens once, at the next start
    assert values(unit, "Timer", "Unit") == ["house-helper-purge.service"]
    assert values(unit, "Install", "WantedBy") == ["timers.target"]
    for event in ("OnBootSec", "OnStartupSec", "OnUnitActiveSec", "OnUnitInactiveSec", "OnActiveSec"):
        assert not values(unit, "Timer", event), event  # one schedule, not also "at every boot"
    delay = values(unit, "Timer", "RandomizedDelaySec")
    assert delay and re.fullmatch(r"[0-9]+min", delay[0]) and int(delay[0][:-3]) <= 30


def test_the_timer_says_that_timing_is_a_tolerance_and_how_to_change_it_without_editing_the_file():
    flat = " ".join(" ".join(line.lstrip("#").split()) for line in read(TIMER).splitlines())
    for needed in ("Timing is a tolerance, not a guarantee", "systemctl edit house-helper-purge.timer", "OnCalendar=",
                   "the empty line first clears the schedule", "systemd-analyze calendar"):
        assert needed in flat, needed


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze is not installed")
def test_systemd_itself_reads_the_calendar_expression():
    expression = values(parse_unit(TIMER), "Timer", "OnCalendar")[0]
    done = subprocess.run(["systemd-analyze", "calendar", expression], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "Next elapse" in done.stdout and "UTC" in done.stdout


# --- Compose: nothing starts by itself, and the service the unit names is the existing one ---------------


def test_the_compose_file_schedules_nothing_and_has_no_cleanup_service():
    text = "\n".join(code_lines(COMPOSE)).lower()
    for word in ("cron", "ofelia", "schedule", "purge", "systemd", "restart: always"):
        assert word not in text, word


@needs_compose
def test_the_unit_names_a_profile_gated_service_that_exists_and_is_unchanged(tmp_path):
    resolved = resolve(tmp_path)
    ops = resolved["services"]["house-helper-ops"]
    assert set(resolved["services"]) == {"house-helper", "house-helper-ops"}  # D2 added no service
    assert ops["profiles"] == ["ops"] and ops["network_mode"] == "none"
    assert ops["entrypoint"] == ["python", "/opt/house-helper/ops/dbtool.py"]
    assert ops["environment"] == {
        "HOUSE_HELPER_DATABASE_URL": "sqlite:////data/house_helper.db",
        "HOUSE_HELPER_STAGING_DIR": "/staging",
    }  # still no credential, and nothing for the cleanup either
    assert ops["logging"] == {"driver": "none"} and "env_file" not in ops
    # The unit's -f argument, with its variable filled in, is this repository's file.
    path = EXEC_START[EXEC_START.index("-f") + 1].replace("${HOUSE_HELPER_CHECKOUT}", str(REPO_ROOT))
    assert os.path.samefile(path, COMPOSE)
    assert EXEC_START[EXEC_START.index("--profile") + 1] == "ops" and "house-helper-ops" in EXEC_START


@needs_compose
def test_a_plain_up_still_knows_only_the_web_service(tmp_path):
    env_file = tmp_path / "e.env"
    env_file.write_text("OPENAI_API_KEY=placeholder\n", encoding="utf-8")
    environ = {**os.environ, "HOUSE_HELPER_ENV_FILE": str(env_file)}
    done = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--services"],
        env=environ, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0 and done.stdout.split() == ["house-helper"]


# --- the runbook says what the units do ------------------------------------------------------------------


def flat(text: str) -> str:
    return " ".join(text.split())


def test_section_14_exists_with_its_parts_and_says_where_the_cleanup_stands():
    text = section(14)
    headings = re.findall(r"^### (14\.\d) ", text, re.MULTILINE)
    assert headings == [f"14.{i}" for i in range(1, 10)]
    status = flat(text.split("### 14.1")[0])
    # The release record is history: it does not change when the timer is installed.
    assert status.startswith(
        "## 14. Scheduled cleanup and retention (R4C-D2) **Status: code RELEASED in Git as `489ff30` (independent audit "
        "passed, owner accepted, push-triggered CI green)."
    )
    # Released code, an installed timer and an accepted run are three different things. The installation and the
    # run are the owner's report (the repository cannot show them). The one run on the real database deleted
    # nothing, so the section says that the deletion itself has not been observed there, and that failure reporting
    # and the offsite backup are separate and still pending. The note closing the checklist says the same thing.
    assert_first_scheduled_run_verified(status, "status paragraph")
    assert "owner-reported" in status
    assert re.search(r"deleted nothing", status, re.I)
    assert re.search(r"has not been observed there", status, re.I)
    assert re.search(r"failure reporting .*? and the encrypted off-host backup .*? are separate and still pending", status, re.I)
    closing = text[text.index("Reported state"):]
    assert_first_scheduled_run_verified(closing, "closing note of 14.9")
    assert "deleted nothing" in flat(closing) and "not part of this acceptance" in flat(closing)
    # What was true before the install is not left standing beside what is true now.
    for outdated in ("Not installed, not scheduled, not deployed", "NOT SCHEDULED ON ANY HOST", "no timer exists on any host"):
        assert outdated not in text, outdated
    # The audit is over: no part of the runbook still waits for it.
    for stale in ("awaiting independent audit", "PENDING AUDIT", "Not released, not accepted"):
        assert stale not in read(RUNBOOK), stale


def test_section_14_covers_each_topic_the_stage_required():
    text = flat(section(14))
    for needed in (
        # the retention policy and its record categories
        "sessions", "oidc_states", "email_login_challenges", "identity_invitations", "turn_requests", "messages",
        "conversations", "365 x 24 hours", "Never touched:", "favourites and long-term preferences",
        # the command and the dry run
        "purge-expired --dry-run", "counts only", "Normal execution deletes; `--dry-run` is the explicit way to look",
        # scheduling: configuration, activation, deactivation, status
        "systemctl enable --now house-helper-purge.timer", "systemctl disable --now house-helper-purge.timer",
        "systemctl list-timers house-helper-purge.timer", "journalctl -u house-helper-purge.service",
        "OnCalendar=", "Persistent=true", "Changing the cadence",
        # troubleshooting, locks, recovery
        "14.5 Troubleshooting", "maintenance_lock_busy", "database_busy", "schema_not_head",
        "exec --shared", "refused", "Recovery after a failed or interrupted run", "run the same command again",
        # timing, SQLite and backups
        "Timing is a tolerance, not a guarantee", "Nothing claims that a row disappears at the instant it turns 365 days old",
        "Logical deletion is not forensic erasure", "secure_delete", "does not `VACUUM`, checkpoint or replace the database",
        "A deleted row survives in every older backup", "invents no policy",
        # the checklist
        "Before this counts as running: checklist",
    ):
        assert needed in text, needed


def test_section_14_names_the_command_and_units_the_way_the_unit_runs_them():
    text = flat(section(14))
    assert "--profile ops run --rm -T house-helper-ops exec --shared -- python -m house_helper.admin purge-expired" in text
    named = {name.rstrip(".") for name in re.findall(r"house-helper-purge[\w.@-]*", text)}
    assert named <= {"house-helper-purge.service", "house-helper-purge.timer", "house-helper-purge.env.example"}, named
    for name in named:
        assert (UNITS / name).exists(), name
    assert "/etc/house-helper/purge.env" in text and "HOUSE_HELPER_IMAGE_TAG" in text
    # The runbook's shown schedule is the timer's.
    assert "every day at 03:30 UTC" in text and "*-*-* 03:30:00 UTC" in read(TIMER)


def test_nothing_in_section_14_installs_or_starts_anything_without_an_owner_marker():
    text = section(14)
    assert "**[OWNER]**" in text.split("### 14.4")[1].split("###")[0].splitlines()[0]  # the heading of the install steps
    assert "Nothing installs, enables or starts them" in flat(text)
    assert "no scheduler runs inside the application" in flat(text)


def test_the_runbook_warns_that_a_cleanup_can_clash_with_an_upgrade_and_says_how_to_avoid_it():
    # The cleanup holds the shared lock; a migration needs it exclusively. If they meet, upgrade.sh
    # reports a failed migration for one that never started. The kit does not change the script
    # (R4C-D1's), so the precaution is a step, and it is where an operator upgrading will read it.
    text = flat(section(14))
    for needed in (
        "Before an upgrade or a restore, stop the timer, and start it again afterwards",
        "the migration is refused", "although nothing was migrated", "maintenance_lock_busy",
        "sudo systemctl stop house-helper-purge.timer", "systemctl is-active house-helper-purge.service",
        "must print: inactive", "sudo systemctl start house-helper-purge.timer",
        "R4C-D2 does not change `upgrade.sh`",
    ):
        assert needed in text, needed
    assert "stop it first (section 14.4)" in flat(section(9))
    assert "the timer stopped before an upgrade or a restore" in text  # and it is on the checklist


def test_the_gate_rows_name_the_command_and_close_on_the_first_accepted_run_only():
    gates = flat(section(12))
    for row in retention_gate_rows():
        assert "CODE RELEASED IN GIT (R4C-D2, `489ff30`)" in row, row
        # Installing the timer did not close a gate; the first run, read in the journal and accepted, did.
        assert_first_scheduled_run_verified(row, row[:60])
        assert "Closed on 2026-10-25" in row and "journal" in row, row
        assert "Open until" not in row, row
    assert "purge-expired" in gates and "section 14.9" in gates


def test_the_checklist_ticks_the_timer_for_the_first_run_and_keeps_the_separate_items_open():
    items = [line for line in section(14).split("### 14.9")[1].splitlines() if line.startswith("- [")]

    def one(prefix: str) -> str:
        found = [item for item in items if item.startswith(prefix)]
        assert len(found) == 1, (prefix, found)
        return found[0]

    # The timer item is ticked for the accepted first run and for nothing more.
    assert one("- [x] The timer:") and "2026-10-25" in one("- [x] The timer:")
    # Failure reporting and the off-host backup are separate pending work: the acceptance does not tick them.
    assert one("- [ ] Where a failed run is reported")
    assert one("- [ ] The backup retention, off-host copy and encryption")
    # What was not reported stays open too.
    for still_open in ("- [ ] A `--dry-run` was read", "- [ ] The host's clock is synchronised"):
        assert one(still_open)


# --- the documented state, as the documents give it on 2026-10-25 ----------------------------------------
# Five things that must not be confused: the technical deployment is done; the retention timer is installed
# and active and its first calendar-triggered run was verified and accepted (it deleted nothing); the offsite
# backup and the reporting of failures are separate and still pending; the Functional Alpha has not started. When
# one of them changes, the documents change with it and so do these checks (the helper they share is in test_deploy_kit.py).


def current_state() -> tuple:
    """(introduction, {item: state}) of 'Current deployment state' in docs/STATUS.md, whitespace-flattened."""
    text = read(STATUS)
    start = text.index("## Current deployment state")
    end = text.find("\n## ", start + 1)
    lines = text[start: end if end != -1 else len(text)].splitlines()
    rows = {}
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 2 and not set(cells[0]) <= set("-: "):
            rows[cells[0]] = flat(cells[1])
    return flat(" ".join(line for line in lines if not line.startswith("|"))), rows


def readme_row(name: str) -> str:
    return next(flat(line) for line in read(REPO_ROOT / "README.md").splitlines() if line.startswith(f"| {name} |"))


def test_the_documented_state_keeps_deployment_cleanup_pending_work_and_functional_alpha_apart():
    introduction, rows = current_state()
    header = flat(read(RUNBOOK).split("\n## ", 1)[0])
    # 1. The technical deployment of 2026-10-24 is the history of the kit; what runs now (REPORTED by the owner on
    # 2026-10-28) is image 66ab4ec32de3 at schema 0007, and every document that gives the state says the same. It is
    # the owner's own use and not a beta, not a public demo and not a Functional Alpha.
    assert "technical deployment" in header
    assert re.search(r"technical instance of House Helper is live", introduction)
    assert re.search(r"not a functional alpha", introduction, re.I)
    for where, text in (
        ("RUNBOOK status paragraph", header),
        ("STATUS.md, Image", rows["Image"]),
        ("README, Deployed version", readme_row("Deployed version")),
    ):
        assert "house-helper:66ab4ec32de3" in text and "REPORTED" in text.replace("reported", "REPORTED"), where
    for where, text in (
        ("RUNBOOK status paragraph", header),
        ("STATUS.md, Database", rows["Database"]),
        ("README, Database", readme_row("Database")),
    ):
        assert "`0007_favorite_recipes`" in text, where
    assert "0009_message_attachments" in header and "not deployed and applied to no database" in header  # the repository is ahead of production
    assert re.search(r"owner is enrolled and has signed in", rows["Users"])
    assert re.search(r"second person, and of any other, is not recorded", rows["Users"])  # nothing is claimed beyond the owner
    assert re.search(r"real Telegram OIDC login PASSED", rows["Sign-in and AI"]) and re.search(r"Email login is \*\*disabled", rows["Sign-in and AI"])
    # 2. The retention timer is installed and active; its first calendar-triggered run was verified and accepted,
    # and it deleted nothing. Every document that gives the state says the same.
    for where, text in (
        ("RUNBOOK status paragraph", header),
        ("STATUS.md, Retention cleanup", rows["Retention cleanup"]),
        ("README, Scheduled cleanup", readme_row("Scheduled cleanup")),
    ):
        assert_first_scheduled_run_verified(text, where)
    assert "deleted nothing" in rows["Retention cleanup"] and "does **not** show a deletion of real expired rows" in rows["Retention cleanup"]
    # 3. The scheduled backup, purge and monitoring are the owner's accepted work of 2026-10-28 (REPORTED), and no document
    # claims more than that: none of them records what exactly is installed. The older "not installed or activated"
    # statements stay only as history, marked as the state of 2026-10-25.
    assert "separate and still pending" in rows["Retention cleanup"] and "2026-10-28" in rows["Retention cleanup"]
    backups = rows["Backups, purge and monitoring"]
    assert "FINAL ACCEPT" in backups and "REPORTED" in backups and "does not record what exactly is installed" in backups
    assert re.search(r"History, state of 2026-10-25.*not installed or activated on the VPS", backups) and "pending" in backups
    assert re.search(r"final acceptance", readme_row("Backups and monitoring"), re.I)
    assert "the owner's accepted work of 2026-10-28 (REPORTED)" in header and "describe procedures, not what runs" in header
    assert re.search(r"of the items below, the owner's real Telegram sign-in and a real OpenAI text conversation are done", introduction)
    # 4. The Functional Alpha is not declared started or accepted, and nothing here says otherwise.
    assert re.search(r"not declared started or accepted", rows["Functional Alpha"], re.I)
    assert re.search(r"not a first-user beta and not a Functional Alpha", introduction)
