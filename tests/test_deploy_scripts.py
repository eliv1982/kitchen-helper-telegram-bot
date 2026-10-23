"""The operator scripts of the deployment kit: deploy/backup.sh, deploy/upgrade.sh and
deploy/new-env-file.sh. They are run for real, under bash, against a FAKE `docker` and `git`
that the shell finds first. The fake records, in order, the steps that matter (the web service
stopped, a backup taken, verified, a migration run, ...) and can be told to fail at any of them,
so these tests can say what the runbook promises:

  * a migration never runs unless a backup of the running version has been made, checked
    (size, header, SHA-256 against the one the tool reported) and verified by a disposable
    container first, and the backup is a complete, matching pair (database file + checksum file);
  * any failure before that point leaves the database untouched and publishes nothing;
  * a backup is published whole or not at all, never over an existing file, and a failed attempt
    leaves nothing a person or a script could take for a backup;
  * every failure after the migration, however it shows itself (a command that fails, an empty
    or unreadable Docker answer, an unhealthy container, a timeout, a signal), ends in one place:
    the new version is stopped (that service only), the result of stopping is stated, and the
    recovery for the exact backup and the pre-upgrade image is printed;
  * the shell's old variables (a stale $F, HOUSE_HELPER_IMAGE_TAG) are never used;
  * the secret-file script refuses to touch a file that exists.

A failure of the TEST RIG ITSELF (the fakes are not the tools the shell finds) is reported as an
ERROR at setup with the words "TEST HARNESS SETUP FAILED", never as a failing deployment-script
test: see HarnessSetupError and prove_fakes_shadow_real_tools.

The same scripts were also run against real Docker (see the remediation report); these tests
keep the ordering and the refusals true on every change without it.
"""

import hashlib
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.deploy_support import DEPLOY, REPO_ROOT, env_for, make_database, tables
from tests.deploy_support import run as run_dbtool


def find_bash() -> str:
    candidates = []
    found = shutil.which("bash")
    if found and "system32" not in found.lower():  # not the WSL launcher
        candidates.append(found)
    candidates += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            done = subprocess.run(
                [
                    candidate,
                    "-c",
                    "command -v sha256sum od head tail wc tr cut grep sed date mktemp sync ln cat dirname basename sleep stat cmp",
                ],
                capture_output=True,
            )
            if done.returncode == 0:
                return candidate
    return ""


BASH = find_bash()
needs_bash = pytest.mark.skipif(not BASH, reason="bash with GNU coreutils is not available")
posix_modes = pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and signals")

OLD, NEW = "oldtag", "newtag"
HEAD_SHA = "1234567890abcdef1234567890abcdef12345678"
CONTAINER_ID = "0123456789abcdef" * 4
OLD_IMAGE_ID = f"sha256:fakeimageid-{OLD}"
AFTER_START = ["STOP", "BACKUP", "VERIFY", "MIGRATE", "CHECK", "UP"]  # what a run that got as far as `up` did

FAKE_DOCKER = r"""#!/usr/bin/env bash
# A stand-in for docker: records what the scripts ask for, fails where a test says so.
if [ "${1-}" = --harness-identity ]; then echo fake-docker; exit 0; fi
d=$FAKE_DIR
echo "docker [tag=${HOUSE_HELPER_IMAGE_TAG-unset}] $*" >> "$d/calls.log"
event() { echo "$1" >> "$d/events"; }
flag() { [ -n "${!1-}" ]; }
args=" $* "
has() { case "$args" in *" $1 "*) return 0 ;; esac; return 1; }
cid=${FAKE_CONTAINER_ID-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef}
late=; [ -e "$d/new_version_started" ] && late=1                       # the new version was started
stopped_once=; [ -e "$d/events" ] && grep -q STOP "$d/events" && stopped_once=1
injected() { echo "Error: injected failure of: $*" >&2; exit "$1"; }

if [ "$1" = image ] && [ "$2" = inspect ]; then
    last=${*: -1}
    case " ${FAKE_MISSING_IMAGES-} " in *" $last "*) exit 1 ;; esac
    if has -f; then
        case "$*" in
            *Labels*) echo "$FAKE_IMAGE_REVISION" ;;
            *) echo "sha256:fakeimageid-${last#house-helper:}" ;;
        esac
    fi
    exit 0
fi
if [ "$1" = inspect ]; then
    last=${*: -1}
    if [ "$last" != "$cid" ]; then echo "Error: No such object: $last" >&2; exit 1; fi
    case "$*" in
        *Config.Image*) echo "${FAKE_RUNNING_IMAGE-house-helper:$OLD_TAG_FOR_FAKE}" ;;
        *)
            if flag FAKE_HEALTH_INSPECT_EXIT; then echo "Error: injected docker inspect failure" >&2; exit "$FAKE_HEALTH_INSPECT_EXIT"; fi
            polls=$(cat "$d/polls" 2> /dev/null || echo 0); polls=$((polls + 1)); echo "$polls" > "$d/polls"
            if flag FAKE_HEALTH_SEQUENCE; then   # one answer per poll, the last one repeats
                set -- $FAKE_HEALTH_SEQUENCE
                if [ "$polls" -le $# ]; then echo "${!polls}"; else echo "${!#}"; fi
            else
                echo "${FAKE_HEALTH-healthy}"
            fi ;;
    esac
    exit 0
fi
if [ "$1" = run ]; then   # the disposable verification
    cat > "$d/verified.input.$$"
    event VERIFY
    if [ -n "${FAKE_VERIFY_APPEARS-}" ]; then echo "someone else's file" > "$FAKE_VERIFY_APPEARS"; fi
    if [ -n "${FAKE_VERIFY_MODIFIES-}" ]; then echo "changed after it was checked" >> "$FAKE_VERIFY_MODIFIES"; fi
    exit "${FAKE_VERIFY_EXIT-0}"
fi
if [ "$1" = compose ]; then
    if has config; then exit "${FAKE_CONFIG_EXIT-0}"; fi
    if has ps; then
        if has --status; then
            if [ -n "$late" ] && [ -e "$d/late_stop_done" ]; then      # the check after the failed deployment was stopped
                if flag FAKE_LATE_PS_EXIT; then injected "$FAKE_LATE_PS_EXIT" ps; fi
            elif [ -n "$late" ]; then                                   # finding the new container
                if flag FAKE_LOOKUP_EXIT; then injected "$FAKE_LOOKUP_EXIT" ps; fi
                if flag FAKE_LOOKUP_EMPTY; then exit 0; fi
                if flag FAKE_LOOKUP_GARBAGE; then echo "$FAKE_LOOKUP_GARBAGE"; exit 0; fi
            elif [ -n "$stopped_once" ]; then                           # step 1, confirming the stop
                if flag FAKE_PS_EXIT_AFTER_STOP; then injected "$FAKE_PS_EXIT_AFTER_STOP" ps; fi
            else                                                        # step 0
                if flag FAKE_PS_EXIT_BEFORE_STOP; then injected "$FAKE_PS_EXIT_BEFORE_STOP" ps; fi
            fi
            [ -e "$d/running" ] && echo "$cid"; exit 0
        fi
        echo "$cid"; exit 0
    fi
    if has stop; then
        event STOP
        if [ -n "$late" ]; then   # stopping a new version that could not be verified
            touch "$d/late_stop_done"
            if flag FAKE_LATE_STOP_EXIT; then injected "$FAKE_LATE_STOP_EXIT" stop; fi
            if flag FAKE_LATE_STOP_NOOP; then exit 0; fi   # succeeds, and the service keeps running
        else
            if flag FAKE_STOP_EXIT; then injected "$FAKE_STOP_EXIT" stop; fi
            if flag FAKE_STOP_FAILS; then exit 0; fi       # succeeds, and the service keeps running
        fi
        rm -f "$d/running"; exit 0
    fi
    if has up; then
        event UP; touch "$d/running" "$d/new_version_started"
        if flag FAKE_UP_EXIT; then injected "$FAKE_UP_EXIT" up; fi
        exit 0
    fi
    if has run; then
        if has backup; then
            event BACKUP
            [ -z "${FAKE_BACKUP_EXIT-}" ] || { echo "error [backup_failed]: injected" >&2; exit "$FAKE_BACKUP_EXIT"; }
            if [ -n "${FAKE_BACKUP_APPEARS-}" ]; then echo "someone else's file" > "$FAKE_BACKUP_APPEARS"; fi
            if [ -n "${FAKE_BACKUP_SLEEP-}" ]; then sleep "$FAKE_BACKUP_SLEEP"; fi
            payload() {   # a function, not a variable: bash cannot hold the NUL byte of the header
                if flag FAKE_BACKUP_EMPTY; then :
                elif flag FAKE_BACKUP_BAD_HEADER; then printf 'not a database, 0123456789'
                elif flag FAKE_BACKUP_FROM; then cat -- "$FAKE_BACKUP_FROM"
                else printf 'SQLite format 3\0 the rest of a fake database'; fi
            }
            payload
            sum=$(payload | sha256sum | cut -d' ' -f1)
            if flag FAKE_BACKUP_WRONG_SHA; then sum=$(printf '%064d' 0); fi
            echo "ok [backup]: backup written to stdout: $(payload | wc -c | tr -d ' ') bytes, sha256 $sum; fake." >&2
            exit 0
        fi
        if has exec; then event MIGRATE; exit "${FAKE_MIGRATE_EXIT-0}"; fi
        if has check-schema; then event CHECK; exit "${FAKE_CHECK_EXIT-0}"; fi
    fi
fi
echo "fake docker: unexpected call: $*" >&2
exit 99
"""

FAKE_GIT = r"""#!/usr/bin/env bash
if [ "${1-}" = --harness-identity ]; then echo fake-git; exit 0; fi
case "$*" in
    *"rev-parse --short=12 HEAD"*) echo "${FAKE_HEAD_SHA:0:12}" ;;
    *"rev-parse HEAD"*) echo "$FAKE_HEAD_SHA" ;;
    *) exit 1 ;;
esac
"""

# `ln` is what publishes a backup (call 1: the checksum file's name, call 2: the database's, which is
# the commit). This wraps the real one so that a test can make the n-th call fail (FAKE_LN_FAIL_NTH)
# or hang forever (FAKE_LN_HOLD_NTH, to be killed in the middle of the publication), or act on call
# FAKE_LN_NTH: run the shell text FAKE_LN_BEFORE first (to swap a file for a foreign one), run the
# real link, run FAKE_LN_AFTER (to damage what it made), and then deliver the signal FAKE_LN_SIGNAL
# to the script that called it, the instant after the link EXISTS and before the script has
# recorded that it does (the window of the audit's reproduction). It is put on PATH only by the
# tests that need it.
FAKE_LN = r"""#!/usr/bin/env bash
d=$FAKE_DIR
n=$(( $(cat "$d/ln_calls" 2> /dev/null || echo 0) + 1 )); echo "$n" > "$d/ln_calls"
if [ "${FAKE_LN_FAIL_NTH-0}" = "$n" ]; then echo "ln: failed to create hard link: Operation not permitted (injected)" >&2; exit 1; fi
if [ "${FAKE_LN_HOLD_NTH-0}" = "$n" ]; then : > "$d/ln_holding"; while :; do sleep 0.2; done; fi
real=/usr/bin/ln; [ -x "$real" ] || real=/bin/ln
if [ "${FAKE_LN_NTH-0}" = "$n" ]; then
    [ -z "${FAKE_LN_BEFORE-}" ] || eval "$FAKE_LN_BEFORE"
    if [ -n "${FAKE_LN_FAIL-}" ]; then echo "ln: failed to create hard link: Operation not permitted (injected)" >&2; exit 1; fi
    "$real" "$@" || exit 1
    [ -z "${FAKE_LN_AFTER-}" ] || eval "$FAKE_LN_AFTER"
    if [ -n "${FAKE_LN_SIGNAL-}" ]; then   # KILL: the caller is gone at once
        kill -s "$FAKE_LN_SIGNAL" "$PPID"
        # Ctrl-C reaches the whole foreground group, and a shell goes on after a SIGINT that its child survived
        if [ "$FAKE_LN_SIGNAL" = INT ]; then kill -s INT $$; fi
        sleep 1
    fi
    exit 0
fi
exec "$real" "$@"
"""

# Git for Windows' bash.exe, started by a program that is not itself an MSYS program (Python,
# PowerShell, CI), puts /mingw64/bin and /usr/bin, and with them the REAL git.exe, in front of the
# PATH it was given: a fake git placed first in that PATH is then never found, and the script reads
# the repository's real HEAD. Bash runs $BASH_ENV before a non-interactive script's first command,
# so this file puts the rig's directory back in front, on every platform alike.
BASH_ENV_TEXT = """\
# written by the test rig (tests/test_deploy_scripts.py)
bin=%s
case ":$PATH:" in ":$bin:"*) ;; *) PATH="$bin:$PATH" ;; esac
export PATH
"""


RESET_SIGNALS_AND_EXEC = (
    "import os, signal, sys\n"
    "for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):\n"
    "    signal.signal(s, signal.SIG_DFL)\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)


class HarnessSetupError(Exception):
    """The test rig is broken: the fake git or docker is not what the shell finds. Raised while a
    fixture is set up, so pytest reports an ERROR at setup, never a failed deployment-script test."""


def prove_fakes_shadow_real_tools(env: dict) -> None:
    """Run, under `env`, the same kind of bash the tests use and ask it which git and docker it
    finds, who they say they are, and which HEAD git reports. Raises HarnessSetupError unless they
    are the fakes and the HEAD is the fake one (never the repository's)."""
    probe = (
        'echo "git-path=$(command -v git)"; echo "docker-path=$(command -v docker)"; '
        'echo "git-identity=$(git --harness-identity 2>&1 | head -n 1)"; '
        'echo "docker-identity=$(docker --harness-identity 2>&1 | head -n 1)"; '
        'echo "head=$(git -C "$1" rev-parse HEAD 2>&1 | head -n 1)"; echo "path=$PATH"'
    )
    done = subprocess.run(
        [BASH, "-c", probe, "_", str(REPO_ROOT)],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    seen = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    problems = []
    if seen.get("git-identity") != "fake-git":
        problems.append(f"`git` resolves to {seen.get('git-path')!r}, which is not the fake")
    if seen.get("docker-identity") != "fake-docker":
        problems.append(f"`docker` resolves to {seen.get('docker-path')!r}, which is not the fake")
    if seen.get("head") != env.get("FAKE_HEAD_SHA"):
        problems.append(f"`git rev-parse HEAD` answered {seen.get('head')!r}, not the fake HEAD")
    if problems:
        raise HarnessSetupError(
            "TEST HARNESS SETUP FAILED - this is NOT a failure of a deployment script. "
            + "; ".join(problems)
            + f". The PATH the shell sees: {seen.get('path')!r}. bash: {BASH}. stderr: {done.stderr.strip()!r}"
        )


def bash_path(path: Path) -> str:
    """`path` as the bash under test spells it (/c/Users/... for Git Bash on Windows)."""
    if os.name != "nt":
        return str(path)
    done = subprocess.run([BASH, "-c", 'cygpath -u "$1"', "_", str(path)], capture_output=True, text=True, timeout=60)
    if done.returncode != 0 or not done.stdout.strip():
        raise HarnessSetupError(f"TEST HARNESS SETUP FAILED - cannot translate {path} with cygpath: {done.stderr!r}")
    return done.stdout.strip()


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.fake_dir = tmp_path / "fake"
        self.bin = tmp_path / "bin"
        self.backups = tmp_path / "backups"
        self.deploy = DEPLOY  # a test may point this at a modified copy
        self.fake_dir.mkdir()
        self.bin.mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("git", FAKE_GIT)):
            self.add_tool(name, text)
        bash_env = tmp_path / "rig-bash-env.sh"
        bash_env.write_bytes((BASH_ENV_TEXT % shlex.quote(bash_path(self.bin))).encode("utf-8"))
        (self.fake_dir / "running").touch()  # the web service is up when the upgrade starts
        self.env = {
            **{
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("HOUSE_HELPER_", "FAKE_", "GIT_")) and k != "BASH_ENV"
            },
            "PATH": os.pathsep.join([str(self.bin), str(Path(BASH).parent), os.environ.get("PATH", "")]),
            "BASH_ENV": bash_env.as_posix(),
            "GIT_DIR": str(tmp_path / "no-such-git-dir"),  # a real git that gets in anyway fails instead of answering
            "FAKE_DIR": str(self.fake_dir),
            "FAKE_HEAD_SHA": HEAD_SHA,
            "FAKE_IMAGE_REVISION": HEAD_SHA,
            "OLD_TAG_FOR_FAKE": OLD,
            "HOME": str(tmp_path / "home"),
            "TMPDIR": str(tmp_path),
        }
        prove_fakes_shadow_real_tools(self.env)

    def add_tool(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_bytes(text.encode("utf-8"))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def copy_deploy(self) -> Path:
        """A private copy of the scripts a test may change; self.deploy points at it."""
        copy = self.root / "deploy-copy" / "deploy"
        copy.mkdir(parents=True)
        for name in ("backup.sh", "upgrade.sh", "compose.yaml"):
            (copy / name).write_bytes((DEPLOY / name).read_bytes())
        self.deploy = copy
        return copy

    def run(self, script: str, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, str(self.deploy / script), *args],
            env={**self.env, **env},
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def popen(self, script: str, *args: str, default_signals: bool = False, **env: str) -> subprocess.Popen:
        """The script as a child the test can signal. `default_signals`: start it through a tiny
        launcher that resets SIGINT/SIGTERM/SIGHUP to their defaults first, because a signal that
        was ignored when a shell started can never be trapped, and the test runner may have been
        started that way (in the background, under nohup)."""
        command = [BASH, str(self.deploy / script), *args]
        if default_signals:
            command = [sys.executable, "-c", RESET_SIGNALS_AND_EXEC, *command]
        extra = {"start_new_session": True} if os.name != "nt" else {}
        return subprocess.Popen(
            command,
            env={**self.env, **env},
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **extra,
        )

    def upgrade_args(self, *args: str) -> list:
        return ["--old-tag", OLD, "--new-tag", NEW, "--backup-dir", str(self.backups), *args]

    def upgrade(self, *args: str, **env: str):
        return self.run("upgrade.sh", *self.upgrade_args(*args), **env)

    def backup_args(self, *args: str) -> list:
        return ["--tag", OLD, "--dir", str(self.backups), *args]

    def backup(self, *args: str, **env: str):
        return self.run("backup.sh", *self.backup_args(*args), **env)

    def wait_for(self, name: str, seconds: int = 60) -> None:
        deadline = time.monotonic() + seconds
        while not (self.fake_dir / name).exists():
            assert time.monotonic() < deadline, f"the fake never reached {name}"
            time.sleep(0.05)

    @property
    def events(self) -> list:
        path = self.fake_dir / "events"
        return path.read_text().split() if path.exists() else []

    @property
    def calls(self) -> list:
        path = self.fake_dir / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def service_running(self) -> bool:
        return (self.fake_dir / "running").exists()

    def published(self) -> list:
        return sorted(p.name for p in self.backups.iterdir()) if self.backups.exists() else []


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def valid_backups(directory: Path) -> list:
    """What a person or a script may take for a backup: NAME.db with NAME.db.sha256 beside it
    and equal to the file (what `sha256sum -c` checks). Anything else is not one."""
    found = []
    for database in sorted(directory.glob("*.db")) if directory.exists() else []:
        sidecar = directory / (database.name + ".sha256")
        if sidecar.is_file() and sidecar.read_text() == f"{sha_of(database)}  {database.name}\n":
            found.append(database.name)
    return found


# --- the rig itself ----------------------------------------------------------------------------------


@needs_bash
def test_the_shell_finds_the_fake_git_and_docker_first_and_never_the_real_head(harness):
    prove_fakes_shadow_real_tools(harness.env)  # the same check that every Harness makes when it is built
    done = harness.run("upgrade.sh", "--old-tag", OLD, "--backup-dir", str(harness.backups), "--dry-run")
    assert done.returncode == 0, done.stderr
    assert f"house-helper:{HEAD_SHA[:12]}" in done.stderr
    real = (
        subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(REPO_ROOT))
        if shutil.which("git")
        else None
    )
    if real is not None and real.returncode == 0:
        assert real.stdout.strip()[:12] not in done.stderr  # the repository's own HEAD was not read


@needs_bash
def test_a_rig_whose_fakes_are_not_found_is_reported_as_a_setup_failure_not_a_script_failure(harness):
    unshadowed = {k: v for k, v in harness.env.items() if k != "BASH_ENV"}
    unshadowed["PATH"] = os.pathsep.join(p for p in unshadowed["PATH"].split(os.pathsep) if p != str(harness.bin))
    with pytest.raises(HarnessSetupError, match="TEST HARNESS SETUP FAILED"):
        prove_fakes_shadow_real_tools(unshadowed)


# --- upgrade.sh: the gate ---------------------------------------------------------------------------


@needs_bash
def test_a_complete_upgrade_runs_every_step_in_order_with_a_verified_backup_first(harness):
    done = harness.upgrade()
    assert done.returncode == 0, done.stderr
    assert harness.events == AFTER_START
    files = harness.published()
    assert len(files) == 2 and files[1] == files[0] + ".sha256"
    backup = harness.backups / files[0]
    assert f"pre-upgrade-{OLD}-to-{NEW}" in backup.name
    assert (harness.backups / files[1]).read_text().split()[0] == sha_of(backup)
    assert valid_backups(harness.backups) == [backup.name]
    assert "upgraded" in done.stderr and "RECOVERY" not in done.stderr
    assert harness.service_running


@needs_bash
def test_each_command_runs_with_its_own_image_tag_the_backup_with_the_old_one(harness):
    # The shell the operator typed into had stale values; none may be used.
    done = harness.upgrade(HOUSE_HELPER_IMAGE_TAG="stale", F="/nowhere/stale.db", OLD_TAG="stale")
    assert done.returncode == 0, done.stderr
    by_step = {}
    for line in harness.calls:
        for word in ("backup --output -", "alembic upgrade head", "check-schema", "up -d"):
            if word in line:
                by_step[word] = line
    assert "[tag=oldtag]" in by_step["backup --output -"]
    assert "[tag=newtag]" in by_step["alembic upgrade head"]
    assert "[tag=newtag]" in by_step["check-schema"] and "[tag=newtag]" in by_step["up -d"]
    verify = next(line for line in harness.calls if "verify" in line)
    assert f"house-helper:{OLD}" in verify and "--require-head" in verify  # the OLD image's head
    assert "stale" not in "\n".join(harness.calls)


@needs_bash
@pytest.mark.parametrize(
    "failure, events",
    [
        ({"FAKE_BACKUP_EXIT": "1"}, ["STOP", "BACKUP"]),
        ({"FAKE_BACKUP_EMPTY": "1"}, ["STOP", "BACKUP"]),
        ({"FAKE_BACKUP_BAD_HEADER": "1"}, ["STOP", "BACKUP"]),
        ({"FAKE_BACKUP_WRONG_SHA": "1"}, ["STOP", "BACKUP"]),
        ({"FAKE_VERIFY_EXIT": "3"}, ["STOP", "BACKUP", "VERIFY"]),
    ],
    ids=["backup fails", "empty backup", "not a database", "checksum differs", "verification fails"],
)
def test_the_migration_never_runs_after_a_backup_that_failed_any_check(harness, failure, events):
    done = harness.upgrade(**failure)
    assert done.returncode == 1
    assert harness.events == events  # no MIGRATE, no CHECK, no UP
    assert harness.published() == []  # nothing published, and no .part left either
    assert "migration was NOT run" in done.stderr
    assert "no backup was taken" in done.stderr
    assert "up -d house-helper" in done.stderr  # the way back to the old version


@needs_bash
@pytest.mark.parametrize(
    "stub_reports, files",
    [
        ("{dir}/lonely.db", {"lonely.db": b"SQLite format 3\0 data"}),  # no checksum file: not committed
        (
            "{dir}/wrong.db",
            {"wrong.db": b"SQLite format 3\0 data", "wrong.db.sha256": b"%s  wrong.db\n" % (b"0" * 64)},
        ),
        ("{dir}/empty.db", {"empty.db": b"SQLite format 3\0 data", "empty.db.sha256": b""}),
        ("{dir}/nothing.db", {}),  # reported, but not there at all
        ("", {}),  # reported nothing
        ("{dir}/a.db\n{dir}/b.db", {"a.db": b"x", "b.db": b"y"}),  # not one path
    ],
    ids=["no checksum file", "checksum differs", "empty checksum file", "no file", "no path", "two paths"],
)
def test_the_migration_never_runs_on_a_backup_that_is_not_a_complete_matching_pair(
    harness, stub_reports, files
):
    copy = harness.copy_deploy()
    (copy / "backup.sh").write_bytes(b"#!/usr/bin/env bash\nprintf '%s\\n' \"$STUB_BACKUP_REPORTS\"\n")
    harness.backups.mkdir()
    for name, content in files.items():
        (harness.backups / name).write_bytes(content)
    done = harness.upgrade(STUB_BACKUP_REPORTS=stub_reports.format(dir=harness.backups))
    assert done.returncode == 1, done.stderr
    assert harness.events == ["STOP"]  # no MIGRATE, no CHECK, no UP
    assert "cannot be trusted" in done.stderr and "migration was NOT run" in done.stderr
    assert "RECOVERY" in done.stderr and "up -d house-helper" in done.stderr
    assert "upgraded:" not in done.stderr


# The gate judges the files alone, so these tests replace backup.sh by a stub that only reports a path.
# The contract they pin (RUNBOOK section 10): the selected backup's file name matches
# NAME_PATTERN, and its checksum file is byte for byte ONE line, "<64 lower-case hex SHA-256 of the
# selected file>, two spaces, <that file's own base name>, newline". Nothing else is accepted, and
# `sha256sum -c` alone is not the gate: it follows whatever name the checksum file contains.

GATE_A = b"SQLite format 3\x00 the backup that upgrade.sh is told about"
GATE_B = b"SQLite format 3\x00 a different backup, with a perfectly good checksum of its own"
DIGEST_A, DIGEST_B = hashlib.sha256(GATE_A).hexdigest(), hashlib.sha256(GATE_B).hexdigest()
NAME_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}\.db$"
AFTER_GATE = ["STOP", "MIGRATE", "CHECK", "UP"]  # a run whose (stub) backup was accepted: the stub takes none


def record(digest: str, name: str) -> bytes:
    return f"{digest}  {name}\n".encode("utf-8")


def gate(harness, reported: str, files: dict):
    """upgrade.sh whose backup.sh reports `reported` ({dir} = the backup directory, in bash's spelling)
    and finds `files` (name -> bytes, or a function of the directory's bash spelling) in that directory."""
    copy = harness.copy_deploy()
    (copy / "backup.sh").write_bytes(b"#!/usr/bin/env bash\nprintf '%s\\n' \"$STUB_BACKUP_REPORTS\"\n")
    harness.backups.mkdir(exist_ok=True)
    where = bash_path(harness.backups)
    for name, content in files.items():
        (harness.backups / name).write_bytes(content(where) if callable(content) else content)
    return harness.upgrade(STUB_BACKUP_REPORTS=reported.format(dir=where))


def assert_gate_refused(harness, done):
    assert done.returncode == 1, done.stderr
    assert harness.events == ["STOP"]  # the stop, and then neither MIGRATE nor CHECK nor UP
    assert "cannot be trusted" in done.stderr and "migration was NOT run" in done.stderr
    assert "RECOVERY" in done.stderr and "upgraded:" not in done.stderr


@needs_bash
def test_the_gate_accepts_the_canonical_pair_and_the_upgrade_goes_on(harness):
    files = {"claimed.db": GATE_A, "claimed.db.sha256": record(DIGEST_A, "claimed.db")}
    done = gate(harness, "{dir}/claimed.db", files)
    assert done.returncode == 0, done.stderr
    assert harness.events == AFTER_GATE
    assert "gate passed" in done.stderr and "RECOVERY" not in done.stderr


def _canonical(name="claimed.db", digest=DIGEST_A):
    return record(digest, name)


# (id, the checksum file for claimed.db, further files in the directory)
BAD_CHECKSUM_FILES = [
    ("a valid checksum of ANOTHER file (the audit's reproduction)", record(DIGEST_B, "other.db"), {"other.db": GATE_B}),
    ("this file's digest, another file's name", record(DIGEST_A, "other.db"), {"other.db": GATE_A}),
    ("two records, both for this file", _canonical() * 2, {}),
    ("two records, the second for another file", _canonical() + record(DIGEST_B, "other.db"), {"other.db": GATE_B}),
    ("another record first", record(DIGEST_B, "other.db") + _canonical(), {"other.db": GATE_B}),
    ("a comment line first", b"# a comment\n" + _canonical(), {}),
    ("empty", b"", {}),
    ("only a newline", b"\n", {}),
    ("digest of 63 characters", record(DIGEST_A[:63], "claimed.db"), {}),
    ("digest of 65 characters", record(DIGEST_A + "0", "claimed.db"), {}),
    ("digest with a non-hex character", record("g" + DIGEST_A[1:], "claimed.db"), {}),
    ("upper-case digest", record(DIGEST_A.upper(), "claimed.db"), {}),
    ("digest of zeros", record("0" * 64, "claimed.db"), {}),
    ("the digest of another file under this name", record(DIGEST_B, "claimed.db"), {}),
    ("one space between digest and name", f"{DIGEST_A} claimed.db\n".encode(), {}),
    ("binary-mode marker", f"{DIGEST_A} *claimed.db\n".encode(), {}),
    ("three spaces", f"{DIGEST_A}   claimed.db\n".encode(), {}),
    ("a tab", f"{DIGEST_A}\tclaimed.db\n".encode(), {}),
    ("CRLF", f"{DIGEST_A}  claimed.db\r\n".encode(), {}),
    ("no final newline", f"{DIGEST_A}  claimed.db".encode(), {}),
    ("an extra blank line", _canonical() + b"\n", {}),
    ("a leading space", b" " + _canonical(), {}),
    ("a trailing space", f"{DIGEST_A}  claimed.db \n".encode(), {}),
    ("a NUL byte after the record", _canonical() + b"\x00", {}),
    ("a backslash-escaped record", b"\\" + _canonical(), {}),
    ("a ./ prefix", record(DIGEST_A, "./claimed.db"), {}),
    ("a sub-directory", record(DIGEST_A, "sub/claimed.db"), {}),
    ("a parent directory", record(DIGEST_A, "../backups/claimed.db"), {}),
    ("an absolute path", lambda where: record(DIGEST_A, f"{where}/claimed.db"), {}),
]


@needs_bash
@pytest.mark.parametrize("sidecar, more", [c[1:] for c in BAD_CHECKSUM_FILES], ids=[c[0] for c in BAD_CHECKSUM_FILES])
def test_the_gate_refuses_every_checksum_file_that_is_not_exactly_the_one_line_for_the_selected_file(
    harness, sidecar, more
):
    files = {"claimed.db": GATE_A, "claimed.db.sha256": sidecar, **more}
    done = gate(harness, "{dir}/claimed.db", files)
    assert_gate_refused(harness, done)
    reason = "non-empty checksum file" if sidecar == b"" else "is not exactly the one line"
    assert reason in done.stderr  # and the reason is the right one, not an accident elsewhere


@needs_bash
def test_the_audit_s_wrong_target_case_passes_sha256sum_but_not_the_gate(harness):
    # claimed.db is the alleged recovery backup; its checksum file holds a VALID checksum for other.db.
    files = {"claimed.db": GATE_A, "other.db": GATE_B, "claimed.db.sha256": record(DIGEST_B, "other.db")}
    done = gate(harness, "{dir}/claimed.db", files)
    check = subprocess.run(  # what the old gate ran: it succeeds, because it checks other.db
        [BASH, "-c", 'cd "$1" && sha256sum -c "$2"', "_", bash_path(harness.backups), "claimed.db.sha256"],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0 and "other.db: OK" in check.stdout and "claimed.db" not in check.stdout
    assert_gate_refused(harness, done)
    assert "is not exactly" in done.stderr  # and it says what the checksum file has to be


@needs_bash
@pytest.mark.parametrize("what", ["the backup", "its checksum file", "both"])
def test_the_gate_refuses_a_missing_file(harness, what):
    files = {}
    if what == "its checksum file":  # the backup is there, the checksum file is not
        files["claimed.db"] = GATE_A
    if what == "the backup":  # the checksum file is there, the backup is not
        files["claimed.db.sha256"] = record(DIGEST_A, "claimed.db")
    assert_gate_refused(harness, gate(harness, "{dir}/claimed.db", files))


# (the file name, why it is not a name backup.sh could have made)
BAD_NAMES = [
    "claimed.txt",
    "claimed.DB",
    "claimed.db.bak",
    ".hidden.db",
    "-leading-dash.db",
    "space in name.db",
    "semi;colon.db",
    "dollar$sign.db",
    "single'quote.db",
    "paren(thesis).db",
    "café.db",  # not ASCII
    "a" * 202 + ".db",  # longer than the pattern allows
]


@needs_bash
@pytest.mark.parametrize("name", BAD_NAMES, ids=[n if len(n) < 40 else "name-of-205-characters" for n in BAD_NAMES])
def test_the_gate_refuses_a_file_name_backup_sh_would_not_have_made_even_with_a_perfect_checksum_file(harness, name):
    files = {name: GATE_A, name + ".sha256": record(DIGEST_A, name)}
    try:
        done = gate(harness, "{dir}/" + name, files)
    except OSError:
        pytest.skip("this file system cannot hold such a name")
    assert_gate_refused(harness, done)


@needs_bash
def test_the_gate_refuses_a_reported_name_without_a_directory(harness):
    files = {"claimed.db": GATE_A, "claimed.db.sha256": record(DIGEST_A, "claimed.db")}
    assert_gate_refused(harness, gate(harness, "claimed.db", files))


@needs_bash
@posix_modes
@pytest.mark.parametrize("which", ["the backup", "its checksum file"])
def test_the_gate_refuses_a_symbolic_link_for_either_file(harness, which):
    stub = harness.backups
    stub.mkdir()
    (stub / "real.db").write_bytes(GATE_A)
    (stub / "real.db.sha256").write_bytes(record(DIGEST_A, "real.db"))
    if which == "the backup":
        (stub / "claimed.db").symlink_to("real.db")
        (stub / "claimed.db.sha256").write_bytes(record(DIGEST_A, "claimed.db"))
    else:
        (stub / "claimed.db").write_bytes(GATE_A)
        (stub / "claimed.db.sha256").symlink_to("real.db.sha256")
    assert_gate_refused(harness, gate(harness, "{dir}/claimed.db", {}))


@needs_bash
@posix_modes
def test_a_pair_that_was_committed_but_never_reported_passes_the_gate(harness):
    # backup.sh was signalled right after the commit: the pair is on disk, the path was never printed.
    # It is exactly the pair a normal run publishes, so an operator can hand it to the gate by hand.
    status, out, _ = signalled_backup(harness, "committed.db", 2, "TERM")
    assert status != 0 and out == ""
    only_the_pair(harness, "committed.db")
    (harness.fake_dir / "events").unlink()  # forget the signalled run's own backup/verify events
    done = gate(harness, "{dir}/committed.db", {})
    assert done.returncode == 0, done.stderr
    assert harness.events == AFTER_GATE


@needs_bash
@pytest.mark.parametrize("script, tool", [("upgrade.sh", "cmp"), ("backup.sh", "cmp"), ("backup.sh", "stat")])
def test_a_missing_comparison_tool_stops_both_scripts_before_anything_is_started(harness, script, tool):
    copy = harness.copy_deploy()
    text = (copy / script).read_bytes().decode("utf-8")
    assert text.count(f"command -v {tool} >") == 1
    (copy / script).write_bytes(text.replace(f"command -v {tool} >", "command -v no-such-tool-for-the-test >").encode("utf-8"))
    done = harness.upgrade() if script == "upgrade.sh" else harness.backup()
    assert done.returncode == 1 and tool in done.stderr and harness.events == []
    assert harness.published() == [] and harness.service_running


def test_the_name_rule_is_the_same_in_both_scripts_and_in_the_runbook():
    for text in ((DEPLOY / "backup.sh").read_text(), (DEPLOY / "upgrade.sh").read_text(), (DEPLOY / "RUNBOOK.md").read_text()):
        assert NAME_PATTERN in text
    for script in ("backup.sh", "upgrade.sh"):
        assert "printf '%s  %s\\n'" in (DEPLOY / script).read_text()  # the one line: digest, two spaces, name


@needs_bash
def test_a_web_service_that_will_not_stop_aborts_before_any_backup_or_migration(harness):
    done = harness.upgrade(FAKE_STOP_FAILS="1")
    assert done.returncode == 1
    assert harness.events == ["STOP"] and harness.published() == []
    assert "still running" in done.stderr and "RECOVERY" in done.stderr


@needs_bash
@pytest.mark.parametrize(
    "problem, message, events, service_up",
    [
        ({"FAKE_PS_EXIT_BEFORE_STOP": "3"}, "cannot ask Compose", [], True),
        ({"FAKE_STOP_EXIT": "1"}, "could not stop the web service", ["STOP"], True),
        ({"FAKE_PS_EXIT_AFTER_STOP": "3"}, "cannot confirm that the web service is stopped", ["STOP"], False),
    ],
    ids=["ps fails before the stop", "stop command fails", "ps fails after the stop"],
)
def test_a_docker_error_before_the_backup_is_reported_not_swallowed_by_set_e(
    harness, problem, message, events, service_up
):
    done = harness.upgrade(**problem)
    assert done.returncode == 1 and message in done.stderr
    assert harness.events == events and harness.published() == []
    assert harness.service_running is service_up
    assert ("RECOVERY" in done.stderr) is bool(events)


@needs_bash
def test_a_failed_migration_prints_the_recovery_for_this_exact_backup_and_never_retries(harness):
    done = harness.upgrade(FAKE_MIGRATE_EXIT="1")
    assert done.returncode == 1
    assert harness.events == ["STOP", "BACKUP", "VERIFY", "MIGRATE"]  # one attempt, no start, nothing to stop
    backup = next(p for p in harness.backups.iterdir() if p.name.endswith(".db"))
    assert backup.name in done.stderr and "do NOT retry" in done.stderr
    assert "restore --input - --replace" in done.stderr
    assert f"HOUSE_HELPER_IMAGE_TAG={OLD}" in done.stderr
    assert f"house-helper:{OLD} (image id {OLD_IMAGE_ID})" in done.stderr  # the matching pre-upgrade image
    assert "sha256sum <" in done.stderr and f"{backup.name}.sha256" in done.stderr  # the pair check, binding the name
    assert "the migration (alembic upgrade head" in done.stderr and "exit status 1" in done.stderr
    assert "never started" in done.stderr and "Nothing was restored or downgraded for you" in done.stderr
    assert valid_backups(harness.backups) == [backup.name]  # the saved database is kept


@needs_bash
def test_a_wrong_schema_after_the_migration_stops_with_the_recovery_and_starts_nothing(harness):
    checked = harness.upgrade(FAKE_CHECK_EXIT="4")
    assert checked.returncode == 1 and harness.events[-1] == "CHECK" and "UP" not in harness.events
    assert "do NOT retry" in checked.stderr and "the schema check" in checked.stderr
    assert "exit status 4" in checked.stderr and "never started" in checked.stderr


@needs_bash
def test_the_recovery_quotes_a_backup_directory_with_spaces_so_it_can_be_pasted(harness):
    spaced = harness.root / "my backups"
    done = harness.run("upgrade.sh", "--old-tag", OLD, "--new-tag", NEW, "--backup-dir", str(spaced), FAKE_MIGRATE_EXIT="1")
    assert done.returncode == 1
    restore = next(line for line in done.stderr.splitlines() if "restore --input - --replace" in line)
    assert "my\\ backups" in restore  # quoted for the shell, so the line can be pasted as it is
    assert "my\\ backups" in next(line for line in done.stderr.splitlines() if "sha256sum <" in line)


# The order of the restore, as the runbook (section 10, *Restore*) and the recovery that upgrade.sh
# prints must both give it: stop, confirm it is stopped, check-idle, restore, check-schema, start,
# look at the health. (The pair check sits between the confirmation and check-idle in the printed text.)
RESTORE_ORDER = [
    "stop house-helper",
    "ps --status running --quiet house-helper",
    "check-idle",
    "restore --input - --replace",
    "check-schema",
    "up -d house-helper",
    "ps house-helper",
]


def order_of(lines):
    """The line numbers at which RESTORE_ORDER's steps appear, each looked for after the previous."""
    found, start = [], 0
    for step in RESTORE_ORDER:
        index = next((i for i in range(start, len(lines)) if step in lines[i]), None)
        assert index is not None, f"{step!r} is missing, or comes before the step it must follow: {lines}"
        found.append(index)
        start = index + 1
    return found


def printed_commands(stderr):
    """The command lines of the printed recovery (indented four spaces), in order."""
    lines = stderr[stderr.index("RECOVERY:"):].splitlines()
    return [l.strip() for l in lines if l.startswith("    ") and ("docker compose" in l or l.strip().startswith("( cd"))]


def runbook_restore_commands():
    text = (DEPLOY / "RUNBOOK.md").read_text(encoding="utf-8")
    section = text[text.index("**Restore.** The order is mandatory"): text.index("What `restore` does and refuses")]
    block = section[section.index("```bash") + len("```bash"): section.rindex("```")]
    return [line for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")]


@needs_bash
def test_the_printed_recovery_follows_the_runbooks_restore_order_including_check_schema_and_health(harness):
    done = harness.upgrade(FAKE_MIGRATE_EXIT="1")
    assert done.returncode == 1
    commands = printed_commands(done.stderr)
    order_of(commands)  # every step is there, once the previous one has come
    assert order_of(runbook_restore_commands())  # and the runbook gives the same steps in the same order
    for command in commands:
        assert command.startswith(f"HOUSE_HELPER_IMAGE_TAG={OLD} ") or command.startswith("( cd "), command
    assert commands[0].startswith(f"HOUSE_HELPER_IMAGE_TAG={OLD} docker compose") and commands[0].endswith("stop house-helper")
    stop, restore, schema, up = (
        next(i for i, c in enumerate(commands) if step in c)
        for step in ("stop house-helper", "restore --input", "check-schema", "up -d house-helper")
    )
    assert stop == 0  # the first command is the stop: the warnings that say "stop it NOW" mean this one
    assert restore < schema < up
    assert f"HOUSE_HELPER_IMAGE_TAG={OLD} docker compose -f deploy/compose.yaml --profile ops run --rm -T house-helper-ops check-schema" in done.stderr
    assert f"house-helper:{OLD} (image id {OLD_IMAGE_ID})" in done.stderr  # the matching image is still named
    assert "Nothing was restored or downgraded for you" in done.stderr
    # no restore, no downgrade, nothing but what the migration step itself ran:
    assert not any("restore" in c or "downgrade" in c for c in harness.calls), harness.calls


@needs_bash
def test_the_printed_checksum_check_works_as_pasted_and_refuses_a_checksum_file_for_another_file(harness):
    spaced = harness.root / "my backups"  # quoting: the line must survive a directory with a space
    done = harness.run(
        "upgrade.sh", "--old-tag", OLD, "--new-tag", NEW, "--backup-dir", bash_path(spaced), FAKE_MIGRATE_EXIT="1"
    )
    assert done.returncode == 1, done.stderr
    line = next(c for c in printed_commands(done.stderr) if c.startswith("( cd "))

    def paste():  # as typed into a shell: on stdin, bytes, so that nothing re-parses the line on its way in
        return subprocess.run([BASH, "-s"], input=line.encode("utf-8") + b"\n", capture_output=True, timeout=60)

    ok = paste()
    assert ok.returncode == 0 and b"exactly the one line for" in ok.stdout, ok.stdout + ok.stderr
    database = next(p for p in spaced.iterdir() if p.name.endswith(".db"))
    sidecar = Path(str(database) + ".sha256")
    good = sidecar.read_bytes()
    other = spaced / "other.db"
    other.write_bytes(GATE_B)
    sidecar.write_bytes(record(sha_of(other), "other.db"))  # a valid checksum, of another file: sha256sum -c would pass it
    wrong = paste()
    assert wrong.returncode != 0 and b"exactly the one line" not in wrong.stdout
    sidecar.write_bytes(good * 2)  # two records, both right
    assert paste().returncode != 0
    sidecar.write_bytes(good)
    assert paste().returncode == 0  # and it is the contents that decided, nothing else


@needs_bash
def test_the_warning_that_the_new_version_may_still_run_points_at_the_stop_that_is_command_one(harness):
    done = harness.upgrade(FAKE_HEALTH="unhealthy", FAKE_LATE_STOP_NOOP="1")
    assert done.returncode == 1
    assert printed_commands(done.stderr)[0].endswith("stop house-helper")
    assert "command 1 of the recovery" in done.stderr and "Run the first command above (the stop) NOW" in done.stderr


# --- upgrade.sh: after the new version was started -----------------------------------------------------

# (injection, the operation that must be named, text that must say why)
START_FAILURES = [
    ({"FAKE_UP_EXIT": "1"}, "starting the new version", "exit status 1"),
    ({"FAKE_LOOKUP_EXIT": "7"}, "finding the new container", "exit status 7"),
    ({"FAKE_LOOKUP_EMPTY": "1"}, "finding the new container", "lists no running container"),
    ({"FAKE_LOOKUP_GARBAGE": "--all"}, "finding the new container", "exactly one container id"),
    ({"FAKE_HEALTH_INSPECT_EXIT": "9"}, "waiting for the new container's health check", "exit status 9"),
    ({"FAKE_HEALTH": "none"}, "waiting for the new container's health check", "no health status"),
    ({"FAKE_HEALTH": ""}, "waiting for the new container's health check", "no health status"),
    ({"FAKE_HEALTH": "weird"}, "waiting for the new container's health check", "does not know"),
    ({"FAKE_HEALTH": "unhealthy"}, "waiting for the new container's health check", "'unhealthy'"),
    (
        {"FAKE_HEALTH": "starting", "HOUSE_HELPER_UPGRADE_HEALTH_WAIT": "2"},
        "waiting for the new container's health check",
        "still 'starting' after 2s",
    ),
]
START_FAILURE_IDS = [
    "up fails",
    "container lookup fails",
    "container id empty",
    "container id not an id",
    "docker inspect fails (exit 9)",
    "health: none",
    "health: empty",
    "health: unknown word",
    "unhealthy",
    "health times out",
]


def assert_recovered(harness, done):
    """What every failure after the migration must have done."""
    assert done.returncode == 1, done.stderr
    assert "upgraded:" not in done.stderr and "Keep house-helper" not in done.stderr  # never reported as done
    assert "FAILED while:" in done.stderr and "did NOT complete" in done.stderr
    backups = valid_backups(harness.backups)
    assert len(backups) == 1  # the verified backup is kept, complete
    assert "RECOVERY: do NOT retry the migration" in done.stderr
    assert str(harness.backups) in done.stderr and backups[0] in done.stderr
    assert f"house-helper:{OLD} (image id {OLD_IMAGE_ID})" in done.stderr
    assert f"HOUSE_HELPER_IMAGE_TAG={OLD} docker compose -f deploy/compose.yaml stop house-helper" in done.stderr
    assert "restore --input - --replace" in done.stderr and "Nothing was restored or downgraded for you" in done.stderr
    recovery = done.stderr[done.stderr.index("RECOVERY:"):]
    assert CONTAINER_ID[:12] not in recovery  # the instructions never depend on a container id


@needs_bash
@pytest.mark.parametrize("failure, operation, why", START_FAILURES, ids=START_FAILURE_IDS)
def test_every_failure_after_the_new_version_started_ends_in_recovery_with_that_service_stopped(
    harness, failure, operation, why
):
    done = harness.upgrade(**failure)
    assert_recovered(harness, done)
    assert operation in done.stderr and why in done.stderr
    assert harness.events == AFTER_START + ["STOP"]  # the web service, once, and nothing else
    assert not harness.service_running
    assert "stopped, and confirmed" in done.stderr and "STILL BE RUNNING" not in done.stderr
    stops = [line for line in harness.calls if " stop " in line + " "]
    assert all(line.endswith("stop house-helper") for line in stops), stops  # only the web service is named
    verbs = {line.split("] ", 1)[1].split()[0] for line in harness.calls}
    assert not verbs & {"kill", "rm", "stop", "restart", "prune"}, verbs  # no direct container commands at all


@needs_bash
def test_the_exit_9_failure_of_docker_inspect_that_slipped_past_set_e_is_now_recovered(harness):
    # The audit's reproduction: stop, backup, verify, migrate, check, up all succeed; then Docker's
    # health inspection exits 9. The old script exited silently with the new service left running.
    done = harness.upgrade(FAKE_HEALTH_INSPECT_EXIT="9")
    assert done.returncode == 1
    assert harness.events == AFTER_START + ["STOP"]
    assert "RECOVERY" in done.stderr and "exit status 9" in done.stderr
    assert not harness.service_running


@needs_bash
def test_an_unhealthy_container_is_a_verdict_and_is_not_waited_out(harness):
    done = harness.upgrade(FAKE_HEALTH="unhealthy", HOUSE_HELPER_UPGRADE_HEALTH_WAIT="300")
    assert done.returncode == 1
    assert sum("State.Health" in line for line in harness.calls) == 1  # one look, no polling to the deadline


@needs_bash
def test_a_container_that_is_starting_is_waited_for_and_then_accepted_when_healthy(harness):
    done = harness.upgrade(FAKE_HEALTH_SEQUENCE="starting healthy")
    assert done.returncode == 0, done.stderr
    assert harness.events == AFTER_START  # no stop: the new version stays up
    assert sum("State.Health" in line for line in harness.calls) == 2
    assert "upgraded:" in done.stderr and "RECOVERY" not in done.stderr and harness.service_running


@needs_bash
def test_the_health_check_is_made_on_the_container_that_compose_listed(harness):
    done = harness.upgrade()
    assert done.returncode == 0
    inspects = [line for line in harness.calls if line.split("] ", 1)[1].startswith("inspect")]
    assert inspects and all(line.endswith(CONTAINER_ID) for line in inspects)


@needs_bash
@pytest.mark.parametrize(
    "stop_problem, says",
    [
        ({"FAKE_LATE_STOP_EXIT": "5"}, ["'docker compose stop house-helper' FAILED (exit status 5)", "STILL RUNNING"]),
        ({"FAKE_LATE_STOP_NOOP": "1"}, ["STILL RUNNING"]),
        ({"FAKE_LATE_PS_EXIT": "6"}, ["COULD NOT CONFIRM", "UNKNOWN", "exit status 6"]),
        (
            {"FAKE_LATE_STOP_EXIT": "5", "FAKE_LATE_PS_EXIT": "6"},
            ["FAILED (exit status 5)", "COULD NOT CONFIRM", "UNKNOWN"],
        ),
    ],
    ids=["stop fails", "stop does nothing", "cannot confirm", "stop fails and cannot confirm"],
)
def test_when_the_failed_new_version_cannot_be_stopped_or_confirmed_stopped_that_is_said_loudly(
    harness, stop_problem, says
):
    done = harness.upgrade(FAKE_HEALTH="unhealthy", **stop_problem)
    assert_recovered(harness, done)
    for text in says:
        assert text in done.stderr, text
    assert "stopped, and confirmed" not in done.stderr
    last = [line for line in done.stderr.splitlines() if line.strip()][-1]
    assert last.startswith("!! The new version may STILL BE RUNNING"), last  # the last thing the operator reads
    assert harness.events[-1] == "STOP"  # one attempt


@needs_bash
def test_a_command_that_nothing_checks_still_ends_in_the_failure_handler(harness):
    # The structural guarantee behind the audit's finding: if a command is ever added after the
    # migration without a check of its own, `set -e` ends the shell, and the EXIT trap still does
    # the whole job. Here a bare `false` is inserted after the new version started.
    copy = harness.copy_deploy()
    text = (copy / "upgrade.sh").read_bytes().decode("utf-8")
    marker = 'current_op="finding the new container'
    assert text.count(marker) == 1
    (copy / "upgrade.sh").write_bytes(text.replace(marker, "false\n" + marker).encode("utf-8"))
    done = harness.upgrade()
    assert_recovered(harness, done)
    assert "had no check of its own" in done.stderr
    assert harness.events == AFTER_START + ["STOP"] and not harness.service_running


@needs_bash
@posix_modes
@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGINT", "SIGHUP"])
def test_a_signal_while_waiting_for_health_stops_the_new_version_and_prints_the_recovery(harness, signal_name):
    proc = harness.popen(
        "upgrade.sh",
        *harness.upgrade_args(),
        default_signals=True,
        FAKE_HEALTH="starting",
        HOUSE_HELPER_UPGRADE_HEALTH_WAIT="120",
    )
    try:
        harness.wait_for("polls")
        proc.send_signal(getattr(signal, signal_name))
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 1, err
    assert "FAILED while:" in err and "RECOVERY: do NOT retry" in err and "stopped, and confirmed" in err
    assert harness.events == AFTER_START + ["STOP"] and not harness.service_running
    assert "upgraded:" not in err


@needs_bash
@pytest.mark.parametrize(
    "problem, message",
    [
        ({"FAKE_MISSING_IMAGES": f"house-helper:{OLD}"}, f"no image house-helper:{OLD}"),
        ({"FAKE_MISSING_IMAGES": f"house-helper:{NEW}"}, f"no image house-helper:{NEW}"),
        ({"FAKE_IMAGE_REVISION": "feedfacefeedface"}, "rebuild it from this commit"),
        ({"FAKE_RUNNING_IMAGE": "house-helper:something-else"}, "must name the image that is running"),
        ({"FAKE_CONFIG_EXIT": "1"}, "does not resolve"),
        ({"HOUSE_HELPER_UPGRADE_HEALTH_WAIT": "soon"}, "whole number of seconds"),
    ],
    ids=["old image missing", "new image missing", "new image from another commit", "wrong old tag", "bad compose file", "bad wait"],
)
def test_every_check_before_the_stop_changes_nothing(harness, problem, message):
    done = harness.upgrade(**problem)
    assert done.returncode == 1 and message in done.stderr
    assert harness.events == []  # not even a stop
    assert harness.published() == []
    assert (harness.fake_dir / "running").exists()  # the service is still up


@needs_bash
def test_the_same_old_and_new_tag_is_refused(harness):
    done = harness.run("upgrade.sh", "--old-tag", OLD, "--new-tag", OLD, "--backup-dir", str(harness.backups))
    assert done.returncode == 1 and "nothing to upgrade" in done.stderr and harness.events == []


@needs_bash
def test_a_dry_run_checks_everything_and_stops_nothing(harness):
    done = harness.upgrade("--dry-run")
    assert done.returncode == 0 and "dry run" in done.stderr
    assert harness.events == [] and (harness.fake_dir / "running").exists()


@needs_bash
@pytest.mark.parametrize("argv", [[], ["--old-tag"], ["--new-tag", "x"], ["--old-tag", "a b"], ["--bogus"]])
def test_upgrade_needs_the_old_tag_and_knows_no_other_options(harness, argv):
    done = harness.run("upgrade.sh", *argv)
    assert done.returncode in (1, 2) and harness.events == []
    if argv == []:
        assert done.returncode == 2 and "usage" in done.stderr


@needs_bash
def test_the_default_new_tag_is_the_commit_of_the_checkout(harness):
    done = harness.run("upgrade.sh", "--old-tag", OLD, "--backup-dir", str(harness.backups), "--dry-run")
    assert done.returncode == 0, done.stderr
    assert f"house-helper:{HEAD_SHA[:12]}" in done.stderr


# --- backup.sh: publication ---------------------------------------------------------------------------


@needs_bash
@posix_modes
def test_a_published_backup_and_its_checksum_are_private_and_agree(harness):
    done = harness.backup()
    assert done.returncode == 0, done.stderr
    path = Path(done.stdout.strip())
    assert done.stdout.count("\n") == 1 and path.parent == harness.backups  # stdout: the path, only
    assert path.read_bytes().startswith(b"SQLite format 3\x00")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(str(path) + ".sha256").stat().st_mode) == 0o600
    assert stat.S_IMODE(harness.backups.stat().st_mode) == 0o700
    assert Path(str(path) + ".sha256").read_text() == f"{sha_of(path)}  {path.name}\n"
    assert harness.published() == [path.name, path.name + ".sha256"]
    assert valid_backups(harness.backups) == [path.name]


@needs_bash
def test_a_normal_backup_publishes_one_complete_valid_pair_and_nothing_else(harness):
    done = harness.backup()
    assert done.returncode == 0, done.stderr
    path = Path(done.stdout.strip())
    assert harness.published() == [path.name, path.name + ".sha256"]  # no .part, nothing else
    assert valid_backups(harness.backups) == [path.name]
    check = subprocess.run(  # the check the runbook tells the operator to make
        [BASH, "-c", 'cd "$1" && sha256sum -c "$2"', "_", str(harness.backups), path.name + ".sha256"],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr


@needs_bash
def test_two_backups_in_the_same_second_get_different_names_and_both_are_kept(harness):
    first, second = harness.backup(), harness.backup()
    assert first.returncode == second.returncode == 0
    assert first.stdout != second.stdout
    assert len(harness.published()) == 4
    assert len(valid_backups(harness.backups)) == 2


@needs_bash
def test_a_backup_never_overwrites_a_file_that_is_already_there(harness):
    harness.backups.mkdir()
    known_good = harness.backups / "known-good.db"
    known_good.write_bytes(b"precious")
    done = harness.backup("--name", "known-good.db")
    assert done.returncode == 1 and "already exists" in done.stderr
    assert known_good.read_bytes() == b"precious"
    assert harness.published() == ["known-good.db"]  # no .part, no checksum file
    assert harness.events == []  # it did not even run the backup


@needs_bash
def test_a_checksum_file_that_is_already_there_stops_the_backup_before_it_starts(harness):
    harness.backups.mkdir()
    foreign = harness.backups / "taken.db.sha256"
    foreign.write_bytes(b"not ours\n")
    done = harness.backup("--name", "taken.db")
    assert done.returncode == 1 and "already exists" in done.stderr
    assert foreign.read_bytes() == b"not ours\n"
    assert harness.published() == ["taken.db.sha256"] and harness.events == []


@needs_bash
@pytest.mark.parametrize("when", ["FAKE_BACKUP_APPEARS", "FAKE_VERIFY_APPEARS"], ids=["during the backup", "just before publication"])
def test_a_checksum_file_that_appears_meanwhile_is_not_replaced_and_no_backup_is_left_behind(harness, when):
    # The audit's reproduction: another file takes the checksum's name after the pre-checks. The old
    # script had already published the database file by then, returned failure, and left it.
    harness.backups.mkdir()
    foreign = harness.backups / "raced.db.sha256"
    done = harness.backup("--name", "raced.db", **{when: str(foreign)})
    assert done.returncode == 1, done.stderr
    assert "already exists" in done.stderr and "nothing was published" in done.stderr
    assert foreign.read_text().strip() == "someone else's file"
    assert harness.published() == ["raced.db.sha256"]  # no raced.db, and none of our parts
    assert valid_backups(harness.backups) == []


@needs_bash
@pytest.mark.parametrize("when", ["FAKE_BACKUP_APPEARS", "FAKE_VERIFY_APPEARS"], ids=["during the backup", "just before publication"])
def test_a_database_file_that_appears_meanwhile_is_not_replaced_and_our_checksum_is_taken_back(harness, when):
    harness.backups.mkdir()
    target = harness.backups / "raced.db"
    done = harness.backup("--name", "raced.db", **{when: str(target)})
    assert done.returncode == 1, done.stderr
    assert "nothing was published" in done.stderr
    assert target.read_text().strip() == "someone else's file"
    assert harness.published() == ["raced.db"]  # our .part files and our checksum file are gone
    assert valid_backups(harness.backups) == []


@needs_bash
def test_a_backup_file_that_changes_after_it_was_verified_is_not_published(harness):
    harness.backups.mkdir()
    done = harness.backup("--name", "moved.db", FAKE_VERIFY_MODIFIES=str(harness.backups / "moved.db.part"))
    assert done.returncode == 1, done.stderr
    assert "changed after it was verified" in done.stderr and "nothing was published" in done.stderr
    assert harness.published() == []  # no pair, no checksum file, no .part


@needs_bash
def test_an_interrupted_earlier_attempt_blocks_the_name_and_is_left_alone(harness):
    harness.backups.mkdir()
    partial = harness.backups / "half.db.part"
    partial.write_bytes(b"an interrupted attempt")
    done = harness.backup("--name", "half.db")
    assert done.returncode == 1 and "already exists" in done.stderr
    assert partial.read_bytes() == b"an interrupted attempt"
    assert harness.published() == ["half.db.part"]


@needs_bash
@pytest.mark.parametrize(
    "failure",
    [{"FAKE_BACKUP_EXIT": "1"}, {"FAKE_BACKUP_EMPTY": "1"}, {"FAKE_BACKUP_WRONG_SHA": "1"}, {"FAKE_VERIFY_EXIT": "4"}],
)
def test_a_failed_backup_leaves_neither_a_published_file_nor_a_partial_one_and_spares_the_old_ones(
    harness, failure
):
    assert harness.backup().returncode == 0
    before = {p.name: p.read_bytes() for p in harness.backups.iterdir()}
    done = harness.backup(**failure)
    assert done.returncode == 1 and "nothing was published" in done.stderr
    assert {p.name: p.read_bytes() for p in harness.backups.iterdir()} == before


@needs_bash
@pytest.mark.parametrize("nth, name", [(1, "the checksum link"), (2, "the database link")])
def test_a_failure_during_publication_leaves_no_artifact_that_could_be_taken_for_a_backup(harness, nth, name):
    harness.add_tool("ln", FAKE_LN)
    assert harness.backup().returncode == 0  # an earlier backup, which must survive
    before = {p.name: p.read_bytes() for p in harness.backups.iterdir()}
    (harness.fake_dir / "ln_calls").write_text("0")  # count the next run's links from 1
    done = harness.backup("--name", "new.db", FAKE_LN_FAIL_NTH=str(nth))
    assert done.returncode == 1 and "nothing was published" in done.stderr
    assert {p.name: p.read_bytes() for p in harness.backups.iterdir()} == before  # not even a checksum file
    assert valid_backups(harness.backups) == [n for n in before if n.endswith(".db")]


@needs_bash
@posix_modes
def test_a_script_killed_between_the_two_links_leaves_a_checksum_but_no_backup(harness):
    harness.add_tool("ln", FAKE_LN)
    proc = harness.popen("backup.sh", *harness.backup_args("--name", "crash.db"), FAKE_LN_HOLD_NTH="2")
    try:
        harness.wait_for("ln_holding")  # the checksum file is linked; the database file is about to be
        os.killpg(proc.pid, signal.SIGKILL)  # the script, the held ln, everything: nothing runs cleanup
        proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not (harness.backups / "crash.db").exists()  # the commit never happened
    assert (harness.backups / "crash.db.sha256").exists()
    assert valid_backups(harness.backups) == []  # a checksum on its own is not a backup
    # The leftovers block that name only, and nothing is overwritten or half-trusted:
    again = harness.backup("--name", "crash.db")
    assert again.returncode == 1 and "already exists" in again.stderr
    other = harness.backup("--name", "other.db")
    assert other.returncode == 0, other.stderr
    assert valid_backups(harness.backups) == ["other.db"]


# --- backup.sh: a signal at the commit point ---------------------------------------------------------------
#
# The commit is the creation of the `.db` hard link. The script learns that it happened one
# statement later, so a signal can land in between: on disk the pair is complete and published, in
# the script's memory it is not. These tests deliver the signal in exactly that window (the fake
# `ln` makes the real link and then signals its caller, which is blocked waiting for it), against the
# real script and real files. POSIX only, like every signal test here.


def signalled_backup(harness, name, nth, signal_name, **env):
    """`backup.sh --name NAME` with `signal_name` delivered right after the link number `nth` exists
    (1: the checksum file, 2: the database file, the commit). Returns (status, stdout, stderr)."""
    harness.add_tool("ln", FAKE_LN)
    proc = harness.popen(
        "backup.sh",
        *harness.backup_args("--name", name),
        default_signals=True,
        FAKE_LN_NTH=str(nth),
        FAKE_LN_SIGNAL=signal_name,
        **env,
    )
    try:
        out, err = proc.communicate(timeout=60)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the fake `ln`, if it outlived the script
        except ProcessLookupError:
            pass
    return proc.returncode, out, err


def only_the_pair(harness, name):
    assert harness.published() == [name, name + ".sha256"]
    assert valid_backups(harness.backups) == [name]


@needs_bash
@posix_modes
@pytest.mark.parametrize("signal_name", ["TERM", "HUP", "INT"])
def test_a_signal_after_the_database_link_keeps_the_committed_pair_and_says_it_was_not_reported(harness, signal_name):
    # The audit's reproduction: SIGTERM after the second hard link and before `committed=1`. The old
    # clean-up believed nothing was committed and removed the checksum file, leaving the `.db` without it.
    status, out, err = signalled_backup(harness, "signalled.db", 2, signal_name)
    assert status != 0 and out == ""  # not acknowledged: the path was never printed
    only_the_pair(harness, "signalled.db")  # committed on disk, and no temporary file left
    database, sidecar = harness.backups / "signalled.db", harness.backups / "signalled.db.sha256"
    assert sidecar.read_text() == f"{sha_of(database)}  signalled.db\n"
    assert database.stat().st_nlink == 1 and sidecar.stat().st_nlink == 1  # the .part names are gone
    assert "complete" in err and "were kept" in err and str(database) in err
    assert "nothing was published" not in err


@needs_bash
@posix_modes
def test_sigkill_after_the_database_link_keeps_the_pair_and_may_leave_temporary_files(harness):
    status, out, err = signalled_backup(harness, "killed.db", 2, "KILL")
    assert status == -signal.SIGKILL and out == ""
    # Nothing could run: the committed pair is valid, the two .part names are still there, and they are
    # hard links of the same files (no second copy of the data), which `rm` can delete without harm.
    assert harness.published() == ["killed.db", "killed.db.part", "killed.db.sha256", "killed.db.sha256.part"]
    assert valid_backups(harness.backups) == ["killed.db"]
    database, part = harness.backups / "killed.db", harness.backups / "killed.db.part"
    assert os.path.samefile(database, part) and database.stat().st_nlink == 2
    again = harness.backup("--name", "killed.db")  # the leftovers block only that name
    assert again.returncode == 1 and "already exists" in again.stderr
    assert harness.published() == ["killed.db", "killed.db.part", "killed.db.sha256", "killed.db.sha256.part"]
    for leftover in ("killed.db.part", "killed.db.sha256.part"):
        (harness.backups / leftover).unlink()
    only_the_pair(harness, "killed.db")
    assert harness.backup("--name", "next.db").returncode == 0


@needs_bash
@posix_modes
@pytest.mark.parametrize("signal_name", ["TERM", "HUP", "INT"])
def test_a_signal_after_the_checksum_link_and_before_the_commit_publishes_nothing(harness, signal_name):
    status, out, err = signalled_backup(harness, "early.db", 1, signal_name)
    assert status != 0 and out == ""
    assert not (harness.backups / "early.db").exists()  # no final .db, ever
    assert harness.published() == []  # and the checksum file this run made is taken back, by identity
    assert "were kept" not in err


@needs_bash
@posix_modes
@pytest.mark.parametrize("signal_name", ["TERM", "HUP", "INT"])
def test_a_signal_while_the_backup_is_being_taken_publishes_nothing(harness, signal_name):
    proc = harness.popen(
        "backup.sh", *harness.backup_args("--name", "mid.db"), default_signals=True, FAKE_BACKUP_SLEEP="60"
    )
    try:
        harness.wait_for("events")  # the fake docker has started the backup
        if signal_name == "INT":  # Ctrl-C goes to the whole foreground group; a shell waits out a child that survives it
            os.killpg(proc.pid, signal.SIGINT)
        else:
            proc.send_signal(getattr(signal, "SIG" + signal_name))
        out, err = proc.communicate(timeout=60)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the sleeping fake docker
        except ProcessLookupError:
            pass
    assert proc.returncode != 0 and out == ""
    assert not (harness.backups / "mid.db").exists() and valid_backups(harness.backups) == []
    assert [n for n in harness.published() if not n.endswith(".part")] == []  # no .db, no checksum file


@needs_bash
@posix_modes
def test_a_signal_never_keeps_a_database_file_whose_checksum_file_is_not_its_checksum(harness):
    # Ours by identity, but damaged between the link and the signal: the pair is not valid, so the
    # script takes back both names it made. A final .db attributed to this run is never invalid.
    sidecar = bash_path(harness.backups / "damaged.db.sha256")
    status, out, err = signalled_backup(
        harness, "damaged.db", 2, "TERM", FAKE_LN_AFTER=f"echo junk > {shlex.quote(sidecar)}"
    )
    assert status != 0 and out == ""
    assert harness.published() == []
    assert "were kept" not in err


@needs_bash
@posix_modes
def test_a_run_that_lost_the_name_leaves_the_files_of_the_publisher_that_holds_it_alone(harness):
    # The winner was killed after its commit: pair + both .part names. A second run for the same name
    # must not mistake those hard links for its own when it stops.
    signalled_backup(harness, "held.db", 2, "KILL")
    before = {p.name: p.read_bytes() for p in harness.backups.iterdir()}
    assert len(before) == 4
    again = harness.backup("--name", "held.db")
    assert again.returncode == 1 and "already exists" in again.stderr
    assert {p.name: p.read_bytes() for p in harness.backups.iterdir()} == before
    assert valid_backups(harness.backups) == ["held.db"]


@needs_bash
def test_clean_up_removes_only_what_this_run_made_not_a_look_alike_under_the_same_name(harness):
    # Ownership is identity, not a name. After this run published its checksum file, another publisher
    # replaces it with its own file of the same name; then this run's database link fails. The old
    # clean-up deleted by name and removed the other publisher's file.
    harness.add_tool("ln", FAKE_LN)
    harness.backups.mkdir()
    sidecar = harness.backups / "lookalike.db.sha256"
    path = shlex.quote(bash_path(sidecar))
    done = harness.backup(
        "--name",
        "lookalike.db",
        FAKE_LN_NTH="2",
        FAKE_LN_BEFORE=f'rm -f -- {path}; echo "another publisher wrote this" > {path}',
        FAKE_LN_FAIL="1",
    )
    assert done.returncode == 1 and "nothing was published" in done.stderr
    assert sidecar.read_text().strip() == "another publisher wrote this"  # untouched
    assert harness.published() == ["lookalike.db.sha256"]  # none of this run's own files remain


@needs_bash
def test_concurrent_backups_to_one_name_have_exactly_one_publisher_and_nothing_is_overwritten(harness):
    harness.backups.mkdir()
    runs = [
        harness.popen("backup.sh", *harness.backup_args("--name", "same.db"), FAKE_BACKUP_SLEEP="3")
        for _ in range(6)
    ]
    results = []
    for proc in runs:
        out, err = proc.communicate(timeout=180)
        results.append((proc.returncode, out, err))
    winners = [r for r in results if r[0] == 0]
    assert len(winners) == 1, [(r[0], r[2]) for r in results]
    assert winners[0][1].strip().endswith("same.db")
    assert all(r[0] == 1 and r[1] == "" for r in results if r[0] != 0)
    assert harness.published() == ["same.db", "same.db.sha256"]  # one complete pair, no leftovers
    assert valid_backups(harness.backups) == ["same.db"]


@needs_bash
def test_a_published_backup_is_a_database_the_restore_accepts_and_its_checksum_verifies(harness, tmp_path, capsys):
    source = make_database(tmp_path / "source.db")
    done = harness.backup(FAKE_BACKUP_FROM=str(source))
    assert done.returncode == 0, done.stderr
    published = Path(done.stdout.strip())
    assert valid_backups(harness.backups) == [published.name]
    assert published.read_bytes() == source.read_bytes()
    data = tmp_path / "restored"
    data.mkdir()
    target = data / "house_helper.db"
    restored = run_dbtool(capsys, ["restore", "--input", str(published)], env_for(target))
    assert restored.code == 0, restored.err
    assert tables(target) == tables(source)
    assert run_dbtool(capsys, ["check-schema"], env_for(target)).code == 0


@needs_bash
def test_the_tag_has_no_default_and_odd_names_are_refused(harness):
    assert harness.run("backup.sh", "--dir", str(harness.backups)).returncode == 2
    for argv in (["--tag", "../x"], ["--tag", OLD, "--label", "a/b"], ["--tag", OLD, "--name", "x.txt"], ["--tag", OLD, "--name", "../x.db"]):
        done = harness.run("backup.sh", "--dir", str(harness.backups), *argv)
        assert done.returncode == 1 and harness.events == [], argv


@needs_bash
def test_a_missing_image_stops_the_backup_before_anything_is_written(harness):
    done = harness.backup(FAKE_MISSING_IMAGES=f"house-helper:{OLD}")
    assert done.returncode == 1 and "no image" in done.stderr
    assert harness.events == []
    assert not harness.backups.exists() or harness.published() == []


# --- new-env-file.sh --------------------------------------------------------------------------------------


@needs_bash
@posix_modes
def test_the_secret_file_is_created_private_from_the_template_and_nothing_is_printed_of_it(tmp_path):
    destination = tmp_path / "secrets" / "house-helper.env"
    template = (DEPLOY / "house-helper.env.example").read_bytes()
    done = subprocess.run([BASH, str(DEPLOY / "new-env-file.sh"), str(destination)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert destination.read_bytes() == template
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert "OPENAI_API_KEY" not in done.stdout + done.stderr  # the content is not echoed


@needs_bash
@posix_modes
def test_an_existing_secret_file_is_refused_and_left_exactly_as_it_was(tmp_path):
    destination = tmp_path / "house-helper.env"
    destination.write_text("OPENAI_API_KEY=placeholder-not-a-real-key\n")
    destination.chmod(0o640)
    before = (destination.read_bytes(), stat.S_IMODE(destination.stat().st_mode))
    done = subprocess.run([BASH, str(DEPLOY / "new-env-file.sh"), str(destination)], capture_output=True, text=True)
    assert done.returncode == 3 and "already exists" in done.stderr
    assert (destination.read_bytes(), stat.S_IMODE(destination.stat().st_mode)) == before
    assert "placeholder-not-a-real-key" not in done.stdout + done.stderr


@needs_bash
@posix_modes
def test_a_symbolic_link_in_the_way_is_refused_and_not_followed(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("do not touch")
    link = tmp_path / "house-helper.env"
    link.symlink_to(victim)
    dangling = tmp_path / "dangling.env"
    dangling.symlink_to(tmp_path / "nowhere")
    for path in (link, dangling):
        done = subprocess.run([BASH, str(DEPLOY / "new-env-file.sh"), str(path)], capture_output=True, text=True)
        assert done.returncode == 3 and "already exists" in done.stderr
    assert victim.read_text() == "do not touch" and not (tmp_path / "nowhere").exists()


@needs_bash
def test_the_secret_file_script_needs_exactly_one_path(tmp_path):
    for argv in ([], ["", ], ["a", "b"]):
        done = subprocess.run([BASH, str(DEPLOY / "new-env-file.sh"), *argv], capture_output=True, text=True)
        assert done.returncode == 2 and "usage" in done.stderr


# --- the scripts as files -------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["backup.sh", "upgrade.sh", "new-env-file.sh"])
def test_the_scripts_are_strict_bash_with_unix_line_endings(name):
    data = (DEPLOY / name).read_bytes()
    assert b"\r" not in data, "CRLF would break bash on the Linux host"
    text = data.decode("utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert "rm -rf" not in text and "mv " not in text  # nothing deletes a tree or renames over a file
    assert "umask 077" in text


def test_a_gitattributes_file_keeps_the_scripts_lf_on_windows_checkouts():
    assert "*.sh text eol=lf" in (DEPLOY / ".gitattributes").read_text()
