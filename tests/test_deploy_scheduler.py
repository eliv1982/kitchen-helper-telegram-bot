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
    SECRET_NAME,
    code_lines,
    needs_compose,
    read,
    resolve,
)

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
