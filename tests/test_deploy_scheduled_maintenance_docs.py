"""The C7-P3 owner procedure, checked against the code it describes (F-01, F-02, F-03).

Everything here READS files; the one Docker test runs `docker compose config`, which creates
no container and writes nothing. The three corrections of 2026-11-21 are pinned:

* **F-01** - section 6 stops and disables the automated maintenance. It does not install,
  enable or re-arm the legacy SQLite units: those are historical artefacts until a separate
  written Owner-approved data-rollback procedure exists (RBK-01/RBK-06).
* **F-02** - the documented statuses agree with the one-shot: a startup refusal is exit 4
  (`purge_database_unavailable`, no SQLSTATE from the pinned driver), and
  `purge_connect_handoff` is exit 3, reported only from structured evidence.
* **F-03** - every manual Compose command names `--env-file /etc/house-helper/purge.env`,
  every host enrollment command that resolves the Compose surface takes
  `--credentials-dir "$CREDENTIALS_DIR"` (an ordinary `sudo` carries no environment), the
  document says why, and the non-mutating preflight and the recorded dry run come before
  any real purge. The regression asserts the resolved credential *mount source*, not just
  that `config --quiet` exits 0.
* **HOF-03 item 4** - the lock handoff is a real journaled `handoff:lock` observation
  (`enroll.py lock-handoff`), not an observation substituted for the frozen journal event.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from tests.pg_oneshot_failures import startup_refusal, structured_denial
from tests.test_deploy_kit import read
from tests.test_deploy_postgres_surface import needs_compose, write_credentials

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "deploy" / "postgres" / "scheduled_maintenance.md"
LEGACY = REPO_ROOT / "deploy" / "systemd" / "legacy-sqlite" / "README.md"
TEMPLATE = REPO_ROOT / "deploy" / "systemd" / "house-helper-purge.env.example"
ONESHOT_PATH = REPO_ROOT / "deploy" / "ops" / "pg_oneshot.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


oneshot = _load("deploy_pg_oneshot_maintenance_docs", ONESHOT_PATH)


def section(number: str) -> str:
    """The text of the '#.. <number> ...' heading up to the next heading of the same level.

    Headings inside fenced code blocks are ignored: the procedure's shell comments start with
    `#`, and a comment is not a heading.
    """
    lines = read(DOC).splitlines(keepends=True)
    opening = re.compile(rf"^(#+) {re.escape(number)}(?:\.| )")
    start = level = None
    fenced = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = opening.match(line)
        if match:
            start, level = index, len(match.group(1))
            break
    assert start is not None, number
    fenced = False
    for index in range(start + 1, len(lines)):
        if lines[index].lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = re.match(r"^(#+) ", lines[index])
        if heading and len(heading.group(1)) <= level:
            return "".join(lines[start:index])
    return "".join(lines[start:])


def flat(text: str) -> str:
    return " ".join(text.split())


def plain(text: str) -> str:
    """Whitespace-flattened, without Markdown emphasis, for phrase checks."""
    return flat(text).replace("`", "").replace("**", "")


def fenced(text: str) -> list:
    return re.findall(r"```(?:bash)?\n(.*?)```", text, re.DOTALL)


# --- F-03: the manual-command environment ------------------------------------------------------


def test_every_manual_compose_command_names_the_units_environment_file():
    commands = [
        line.strip()
        for block in fenced(read(DOC))
        for line in block.replace("\\\r\n", " ").replace("\\\n", " ").splitlines()
        if line.strip().startswith("sudo docker compose")
    ]
    runs = [command for command in commands if " run " in command]
    assert runs, "the procedure must show the manual Compose commands"
    for command in runs:
        assert "--env-file /etc/house-helper/purge.env" in command, command
        assert "run --rm -T purge" in command, command
    configs = [command for command in commands if " config " in command]
    assert configs and all("--env-file" in command for command in configs)


def test_the_document_says_why_an_exported_variable_is_not_enough():
    text = plain(read(DOC))
    for needed in (
        "does not carry the invoking shell's environment",
        "sudo -E",
        "error while interpolating services.<service>.environment.HOUSE_HELPER_PG_ACTIVATE",
        "--env-file is not optional",
        "must never contain a credential",
    ):
        assert needed in text, needed


def test_the_host_tools_get_the_canonical_journal_and_credential_paths():
    # `flat` preserves a line-continuation backslash as its own token; the checks below
    # describe the command as the shell reads it, so remove those tokens first.
    text = flat(section("3")).replace(" \\ ", " ")
    for needed in (
        '--journal-dir "$JOURNAL_DIR" status',
        '--journal-dir "$JOURNAL_DIR" --credentials-dir "$CREDENTIALS_DIR" verify',
        '--credentials-dir "$CREDENTIALS_DIR" lock-handoff',
        "--credentials-dir \"$CREDENTIALS_DIR\" handoff --role hh_purge",
        "check-credentials --credentials-dir",
        "s/^HOUSE_HELPER_PG_JOURNAL_DIR=//p",
        "s/^HOUSE_HELPER_PG_CREDENTIALS_DIR=//p",
        "The approved value is the one in the same file the units read",
    ):
        assert needed in text, needed


def test_every_enrollment_command_that_resolves_compose_names_the_credential_directory():
    # F-03: `status` and `check-credentials` do not resolve the Compose surface (the latter
    # has its own flag above); every other host enrollment command must carry the approved
    # directory explicitly, because an ordinary `sudo` loses the environment and the
    # repository default would make the mount-identity check refuse.
    commands = [
        line.strip()
        for block in fenced(read(DOC))
        for line in block.replace("\\\r\n", " ").replace("\\\n", " ").splitlines()
        if "deploy/postgres/enroll.py" in line
    ]
    assert commands
    for command in commands:
        if command.endswith(" status") or "check-credentials" in command:
            continue
        assert '--credentials-dir "$CREDENTIALS_DIR"' in command, command


def test_the_lock_handoff_is_a_journaled_event_not_a_substituted_observation():
    text = plain(section("3.9"))
    assert "lock-handoff --approved-by" in text
    assert "handoff_lock" in text and "handoff:lock" in text
    assert "lock handoff UNKNOWN" in text
    assert "no exclusive holder remains" in text
    # The withdrawn claim must not come back.
    assert "is not a journal event" not in text
    assert "Do not invent a journal line" not in text


def test_a_non_mutating_preflight_and_a_recorded_dry_run_precede_the_real_purge():
    text = read(DOC)
    preflight = text.index("--profile ops config --quiet")
    dry_run = text.index("purge purge --dry-run")
    record = text.index("Record the dry run's output")
    real = text.index("sudo systemctl start house-helper-purge.service")
    assert preflight < dry_run < record < real
    assert "No row is counted or deleted" in flat(section("3.7"))
    assert "refused [purge_database_unavailable]" in plain(section("3.7"))
    assert "preflight.py" in text and "check-credentials" in text


def test_the_gates_are_in_the_order_that_observes_before_it_acts():
    text = read(DOC)
    steps = [
        "### 3.1 Record the timer state",          # before-values first
        "### 3.2 Stop the legacy schedule first",  # the legacy pair is last used here
        "### 3.3 Install the PostgreSQL units",
        "### 3.4 Prove the installed image",
        "### 3.6 Non-mutating preflight",
        "### 3.7 Observe the refusal",             # failure propagation, before the handoff
        "### 3.8 Journal the H3",                  # the authority change
        "### 3.9 Observe a manual dry run",        # authorization and lock evidence
        "### 3.10 Observe a manual real purge",
        "### 3.11 Observe a manual offsite backup",
        "### 3.12 Monitoring",
        "### 3.13 Re-arm the timers sequentially",
    ]
    positions = [text.index(step) for step in steps]
    assert positions == sorted(positions), steps


def installed_env(template: str, replacements: dict) -> str:
    """The shipped template with named entries substituted, `KEY=` prefixes preserved.

    The earlier fixture returned only the replacement value for a named key, silently
    turning `HOUSE_HELPER_PG_CREDENTIALS_DIR=/approved` into a bare path line. Compose then
    fell back to its default directory and the test could not see the difference.
    """
    lines = []
    for line in template.splitlines():
        if line.startswith("#") or "=" not in line:
            lines.append(line)
            continue
        key, value = line.split("=", 1)
        lines.append(f"{key}={replacements.get(key, value)}")
    return "\n".join(lines)


def _compose_run(environ, *extra, profile="ops", config_args=()):
    compose = REPO_ROOT / "deploy" / "compose.postgres.yaml"
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose),
            *extra,
            "--profile",
            profile,
            "config",
            *config_args,
        ],
        env=environ,
        capture_output=True,
        text=True,
        timeout=180,
    )


@needs_compose
def test_the_documented_env_file_resolves_the_compose_surface(tmp_path):
    # The mechanism section 3.5 relies on, exercised with the shipped template: with
    # `--env-file` the project resolves AND the credential mount resolves to the approved
    # external directory; without it, the activation guard fails exactly as the document
    # says. No container is created.
    credentials = tmp_path / "approved-credentials"
    write_credentials(credentials)
    app_env = tmp_path / "app.env"
    app_env.write_text("OPENAI_API_KEY=placeholder\n", encoding="utf-8")
    env_file = tmp_path / "purge.env"
    env_file.write_text(
        installed_env(
            read(TEMPLATE),
            {
                "HOUSE_HELPER_CHECKOUT": str(REPO_ROOT),
                "HOUSE_HELPER_ENV_FILE": str(app_env),
                "HOUSE_HELPER_PG_CREDENTIALS_DIR": str(credentials),
            },
        ),
        encoding="utf-8",
    )
    environ = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HOUSE_HELPER_", "COMPOSE_"))
    }

    resolved = _compose_run(
        environ, "--env-file", str(env_file), config_args=("--quiet",)
    )
    assert resolved.returncode == 0, resolved.stderr
    guarded = _compose_run(environ, config_args=("--quiet",))
    assert guarded.returncode != 0
    assert "required variable HOUSE_HELPER_PG_ACTIVATE is missing a value" in guarded.stderr

    # Requirement F-03.6: assert the resolved mount SOURCE, not merely a zero exit.
    # `--profile pg` is the service that carries the bootstrap credential bind.
    model = _compose_run(
        environ,
        "--env-file",
        str(env_file),
        profile="pg",
        config_args=("--format", "json"),
    )
    assert model.returncode == 0, model.stderr
    services = json.loads(model.stdout)["services"]
    mounts = {
        entry["target"]: entry["source"]
        for entry in services["house-helper-db"]["volumes"]
        if "source" in entry
    }
    expected = credentials / "postgres_superuser.password"
    assert os.path.normcase(os.path.realpath(mounts[
        "/run/credentials/postgres_superuser.password"
    ])) == os.path.normcase(os.path.realpath(expected))
    # The web service's own passfile resolves from the same directory.
    web_mounts = {
        entry["target"]: entry["source"]
        for entry in services["house-helper"]["volumes"]
        if "source" in entry
    }
    assert os.path.normcase(os.path.realpath(web_mounts[
        "/run/credentials/hh_app.pgpass"
    ])) == os.path.normcase(os.path.realpath(credentials / "hh_app.pgpass"))


# --- F-01: stop, do not reactivate -------------------------------------------------------------


def test_rollback_stops_maintenance_and_refuses_to_reactivate_sqlite():
    text = section("6")
    flattened = flat(text)
    for needed in (
        "stop and disable the automated maintenance",
        "disable --now house-helper-purge.timer house-helper-offsite-backup.timer",
        "prohibited",
        "historical",
        "separate, written, Owner-approved data-rollback procedure",
        "RBK-01",
        "RBK-06",
        "newly authorized reverse project",
        "mutate preserved SQLite data",
        "shared offsite destination",
        "displace PostgreSQL recovery points",
    ):
        assert needed in flattened, needed
    # No step of the recovery section installs or enables anything, and the legacy path is
    # named only to forbid it.
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("sudo systemctl enable"), stripped
        assert "install -m 0644 deploy/systemd/legacy-sqlite" not in stripped, stripped
    assert "must not be copied to `/etc` or enabled without the authorization below" in flattened


def test_the_rollback_requires_all_six_decisions():
    text = flat(section("6"))
    for decision in (
        "Which database is authoritative",
        "accepted loss of PostgreSQL data",
        "connections are drained",
        "recovery asset is preserved",
        "separately protected",
        "before any schedule is re-armed",
    ):
        assert decision in text, decision


def test_the_legacy_units_are_assets_not_a_rollback_command():
    text = flat(read(LEGACY))
    assert "preserved, not installed" in text
    assert "not a rollback procedure" in text
    assert "separate, written, Owner-approved data-rollback procedure" in text
    assert "RBK-01" in text and "RBK-06" in text
    # The withdrawn instruction must not come back.
    assert "file replacement plus" not in text


# --- F-02: the documented statuses are the implemented ones ------------------------------------


def test_the_documented_connection_statuses_are_the_implemented_ones():
    documented = plain(section("1.1")).lower()
    assert "purge_database_unavailable" in documented and "exit 4" in documented
    assert "purge_connect_handoff" in documented and "exit 3" in documented

    startup = oneshot._connection_failure(
        _wrapped(startup_refusal('permission denied for database "house_helper"')),
        role="purge", login="hh_purge", handoff="H3",
        handoff_command="enroll.py handoff --role hh_purge",
    )
    assert startup[0] == "purge_database_unavailable" and startup[2] == 4

    structured = oneshot._connection_failure(
        _wrapped(structured_denial()),
        role="purge", login="hh_purge", handoff="H3",
        handoff_command="enroll.py handoff --role hh_purge",
    )
    assert structured[0] == "purge_connect_handoff" and structured[2] == 3


def test_the_status_table_names_the_category_the_emitter_uses_for_four():
    # The procedure must not claim exit 4 is only one thing: the emitter calls it
    # `schema_not_head`, and the journal line carries the real reason.
    text = flat(section("1.1"))
    assert "schema_not_head" in text and "superset" in text
    assert "refused [purge_database_unavailable]" in text


def _wrapped(orig):
    import sqlalchemy as sa

    return sa.exc.DBAPIError("SELECT 1", {}, orig)
