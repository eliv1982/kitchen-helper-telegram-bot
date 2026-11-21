"""Native verification of the tagged CONNECT handoff wrapper (HH-DB-01 P0 R2).

Run from the repository root:

    python tests/native_handoff_wrapper.py

Requires Docker and the pinned PostgreSQL image from `deploy/compose.postgres.yaml`. It
creates ONE disposable `docker run --rm` container from that exact digest with
`--network none`, no mounts, no credentials and no persistent data. It never contacts,
starts or modifies the enrolled PostgreSQL cluster.

What is exercised, in the real pinned Alpine/BusyBox runtime:

  * the actual `_HANDOFF_GRANT_SCRIPT` and `_HANDOFF_PROBE_SCRIPT` from
    `deploy/postgres/enroll.py`, exec'ed exactly as the host tool does (`-u postgres`);
  * a `psql` shim earlier on PATH that records the argv and the exact `-f` file bytes, so
    the SQL delivered to the child can be compared byte-for-byte;
  * completion markers, exit status, BusyBox timeout status (143, not 124);
  * an orphaned `psql` after the wrapper is SIGKILLed (the probe must report it alive);
  * immutable container-id binding: after a same-name replacement, the old id no longer
    answers while the name does.

This script is deliberately not named `test_*.py`: the normal pytest suite stays offline.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from deploy.postgres import enroll  # noqa: E402  (after sys.path setup)

COMPOSE = REPOSITORY / "deploy" / "compose.postgres.yaml"

SHIM = """\
#!/bin/sh
cap=/tmp/cap
tag="${PGAPPNAME:-}"
file=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-f" ]; then file="$a"; fi
  case "$a" in hh_handoff_tag=*) tag="${a##*=}" ;; esac
  prev="$a"
done
if [ -z "$tag" ]; then
  sql=$(cat 2>/dev/null)
  case "$sql" in
    *pg_stat_activity*) printf '0\\n'; exit 0 ;;
  esac
  exit 0
fi
mkdir -p "$cap/$tag" 2>/dev/null
printf '%s\\n' "$@" > "$cap/$tag.argv" 2>/dev/null
if [ -n "$file" ]; then cp "$file" "$cap/$tag.sql" 2>/dev/null; fi
if [ -f "$cap/$tag.control" ]; then . "$cap/$tag.control"; fi
[ -n "${shim_stdout:-}" ] && printf '%s\\n' "$shim_stdout"
[ -n "${shim_stderr:-}" ] && printf '%s\\n' "$shim_stderr" >&2
sleep "${shim_sleep:-0}"
exit "${shim_exit:-0}"
"""


def image_reference() -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(r"^\s*image:\s*(postgres@sha256:[0-9a-f]{64})\s*$", text, re.M)
    if match is None:
        raise SystemExit("the pinned postgres image digest is not in the Compose file")
    return match.group(1)


class Harness:
    def __init__(self) -> None:
        self.image = image_reference()
        self.name = "hh-native-handoff-" + uuid.uuid4().hex[:12]
        self.container_id = ""
        self.failures: list[str] = []
        self.checks: list[str] = []

    # -- docker helpers ----------------------------------------------------------------

    def run(self, argv, *, stdin=None, timeout=180):
        return subprocess.run(
            list(argv),
            input=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def sh(self, script: str, *, stdin=None, user="root", timeout=180):
        argv = ["docker", "exec"]
        if stdin is not None:
            argv.append("-i")
        if user:
            argv += ["-u", user]
        argv += [self.name, "sh", "-c", script]
        return self.run(argv, stdin=stdin, timeout=timeout)

    def read(self, path: str) -> bytes:
        done = self.sh(f"cat {path}", user="postgres")
        if done.returncode != 0:
            raise AssertionError(f"could not read {path}")
        return done.stdout

    def write(self, path: str, data: bytes, *, user: str = "postgres") -> None:
        done = self.sh(f"cat > {path}", stdin=data, user=user)
        if done.returncode != 0:
            raise AssertionError(f"could not write {path}")

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.checks.append(label)
            print(f"PASS  {label}")
        else:
            self.failures.append(label + (f": {detail}" if detail else ""))
            print(f"FAIL  {label}{(': ' + detail) if detail else ''}")

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        inspect = self.run(
            ["docker", "image", "inspect", self.image, "--format", "{{.Id}}"]
        )
        if inspect.returncode != 0:
            raise SystemExit(
                f"the pinned image {self.image} is not present locally; nothing was run"
            )
        started = self.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                self.name,
                "--network",
                "none",
                "-e",
                "PATH=/tmp/shim:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "--entrypoint",
                "sh",
                self.image,
                "-c",
                "sleep 900",
            ]
        )
        if started.returncode != 0:
            raise SystemExit(started.stderr.decode("utf-8", "replace"))
        self.container_id = self.run(
            ["docker", "inspect", "--type", "container", self.name, "--format", "{{.Id}}"]
        ).stdout.decode().strip()
        self.sh("mkdir -p /tmp/shim /tmp/cap")
        self.write("/tmp/shim/psql", SHIM.encode(), user="root")
        self.sh(
            "chmod 0755 /tmp/shim/psql; chown -R postgres:postgres /tmp/cap; "
            "chmod 0700 /tmp/cap"
        )
        self.check(
            "container is the pinned digest",
            self.run(
                ["docker", "inspect", "--type", "container", self.container_id,
                 "--format", "{{.Config.Image}}"]
            ).stdout.decode().strip() == self.image,
        )

    def stop(self) -> None:
        self.run(["docker", "rm", "-f", self.name], timeout=60)
        # A replacement container is also removed by name (idempotent).
        self.run(["docker", "rm", "-f", self.name], timeout=60)

    def control(
        self,
        tag: str,
        *,
        exit_code: int = 0,
        sleep: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.write(
            f"/tmp/cap/{tag}.control",
            (
                f"shim_exit={exit_code}\nshim_sleep={sleep}\n"
                f"shim_stdout={stdout}\nshim_stderr={stderr}\n"
            ).encode(),
        )

    def kill_tagged(self, tag: str) -> None:
        """Kill every process whose cmdline carries the tag; the tag is env, not argv."""
        script = (
            'tag="${KILL_TAG:?}"; '
            "for p in /proc/[0-9]*; do "
            "pid=${p#/proc/}; "
            '[ "$pid" = "$$" ] && continue; '
            'cmd=$(tr "\\000" "\\n" < "$p/cmdline" 2>/dev/null) || continue; '
            'case "$cmd" in *"$tag"*) kill -9 "$pid" 2>/dev/null ;; esac; '
            "done"
        )
        self.run(
            ["docker", "exec", "-e", f"KILL_TAG={tag}", "-u", "root", self.name,
             "sh", "-c", script],
            timeout=30,
        )

    def docker_helper(self):
        spec = enroll.ClusterSpec(
            project="house-helper-pg",
            container_name=self.name,
            image=self.image,
            volume="unused-native",
            network="unused-native",
            credentials_directory="/unused/credentials",
            state_volume="unused-native-state",
            postgresql_conf_source="/unused/postgresql.conf",
            pg_hba_source="/unused/pg_hba.conf",
            bootstrap_credential_source="/unused/password",
        )
        return enroll.Docker(spec, runner=subprocess.run)


def wait_for(harness: Harness, path: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if harness.sh(f"test -e {path}", user="postgres").returncode == 0:
            return True
        time.sleep(0.05)
    return False


def scenario_delivery(harness: Harness, docker) -> None:
    print("\n== scenario: SQL stdin delivery, lifecycle and evidence ==")
    run_id = uuid.uuid4().hex
    tag = enroll.enrollment.handoff_application_name(run_id)
    harness.control(tag, exit_code=0, sleep=0)
    attempt = enroll._grant_connect_transport(
        docker, "hh_app", run_id, container_id=harness.container_id
    )
    sql = f'GRANT CONNECT ON DATABASE "house_helper" TO "hh_app";\n'.encode()
    captured = harness.read(f"/tmp/cap/{tag}.sql")
    harness.check("client returned normally", attempt.returned, repr(attempt))
    harness.check("client status is 0", attempt.returncode == 0, repr(attempt.returncode))
    harness.check(
        "psql received the SQL bytes unchanged", captured == sql, repr(captured)
    )
    evidence = enroll._handoff_transport(
        docker, run_id, container_id=harness.container_id
    )
    harness.check("probe observed the operation", evidence.observed, repr(evidence))
    harness.check("wrapper start marker", evidence.started, repr(evidence))
    harness.check("wrapper stop marker", evidence.stopped, repr(evidence))
    harness.check("wrapper recorded psql exit 0", evidence.psql_exit == 0, repr(evidence))
    harness.check("wrapper is gone", not evidence.client_alive, repr(evidence))
    harness.check("tagged session is gone", not evidence.session_alive, repr(evidence))
    harness.check(
        "termination is positively established",
        enroll.enrollment.handoff_termination_failures(evidence) == (),
        repr(evidence),
    )
    harness.check(
        "no SQL file is left behind",
        harness.sh(f"test ! -e /tmp/{tag}.sql", user="postgres").returncode == 0,
    )

    # The wrapper must not swallow or redirect the child's streams or status.
    stream_tag = enroll.enrollment.handoff_application_name(uuid.uuid4().hex)
    harness.control(
        stream_tag, exit_code=0, stdout="handoff-out", stderr="handoff-err"
    )
    done = harness.run(
        [
            "docker",
            "exec",
            "-i",
            "-u",
            "postgres",
            harness.container_id,
            "sh",
            "-c",
            enroll._HANDOFF_GRANT_SCRIPT,
            enroll.HANDOFF_GRANT_WRAPPER_NAME,
            stream_tag,
        ],
        stdin=sql,
    )
    harness.check("child stdout is forwarded", b"handoff-out" in done.stdout, repr(done.stdout))
    harness.check("child stderr is forwarded", b"handoff-err" in done.stderr, repr(done.stderr))
    harness.check("child exit status is forwarded", done.returncode == 0, repr(done.returncode))


def scenario_busybox_timeout(harness: Harness, docker) -> None:
    print("\n== scenario: BusyBox timeout and ambiguous status ==")
    direct = harness.sh("timeout 1 sleep 5; echo $?", user="postgres")
    harness.check(
        "pinned BusyBox timeout exits 143 (not 124)",
        direct.stdout.decode().strip() == "143",
        direct.stdout.decode().strip(),
    )

    run_id = uuid.uuid4().hex
    tag = enroll.enrollment.handoff_application_name(run_id)
    harness.control(tag, exit_code=0, sleep=30)
    original = enroll._HANDOFF_GRANT_SCRIPT
    enroll._HANDOFF_GRANT_SCRIPT = original.replace(
        f"timeout {enroll._HANDOFF_CONTAINER_TIMEOUT} ", "timeout 1 "
    )
    try:
        attempt = enroll._grant_connect_transport(
            docker, "hh_app", run_id, container_id=harness.container_id
        )
    finally:
        enroll._HANDOFF_GRANT_SCRIPT = original
    evidence = enroll._handoff_transport(
        docker, run_id, container_id=harness.container_id
    )
    harness.check(
        "the timed-out wrapper reports 143 to the host",
        attempt.returncode == 143,
        repr(attempt.returncode),
    )
    harness.check(
        "the container recorded the same ambiguous 143",
        evidence.psql_exit == 143,
        repr(evidence.psql_exit),
    )
    harness.check(
        "143 is neither success nor a definitive refusal",
        evidence.psql_exit not in (enroll.enrollment.GRANT_SUCCEEDED,
                                   enroll.enrollment.GRANT_SERVER_REFUSED),
        repr(evidence.psql_exit),
    )
    harness.check(
        "termination is still positively established",
        enroll.enrollment.handoff_termination_failures(evidence) == (),
        repr(evidence),
    )


def scenario_orphan(harness: Harness, docker) -> None:
    print("\n== scenario: orphaned psql after the wrapper is killed ==")
    run_id = uuid.uuid4().hex
    tag = enroll.enrollment.handoff_application_name(run_id)
    harness.control(tag, exit_code=0, sleep=30)
    result: dict[str, object] = {}

    def launch() -> None:
        result["attempt"] = enroll._grant_connect_transport(
            docker, "hh_app", run_id, container_id=harness.container_id
        )

    thread = threading.Thread(target=launch)
    thread.start()
    if not wait_for(harness, f"/tmp/{tag}.start"):
        harness.check("wrapper started", False, "no start marker")
        return
    pid_file: list[str] = []
    deadline = time.time() + 10
    while time.time() < deadline:
        pid_file = harness.read(f"/tmp/{tag}.pid").decode().split()
        if len(pid_file) >= 2:
            break
        time.sleep(0.05)
    wrapper_pid = pid_file[0] if pid_file else ""
    child_pid = pid_file[1] if len(pid_file) > 1 else ""
    harness.check("the wrapper published its pid and child", len(pid_file) >= 2, repr(pid_file))
    harness.sh(f"kill -9 {wrapper_pid}", user="root")
    thread.join(timeout=30)
    if thread.is_alive():
        harness.check("orphan client returned", False, "docker exec did not return")
        return
    evidence = enroll._handoff_transport(
        docker, run_id, container_id=harness.container_id
    )
    harness.check(
        "the orphaned tagged client is reported alive",
        evidence.client_alive is True,
        repr(evidence),
    )
    harness.check(
        "termination is refused while the orphan lives",
        enroll.enrollment.handoff_termination_failures(evidence)
        == ("transport:client_alive",),
        repr(enroll.enrollment.handoff_termination_failures(evidence)),
    )
    harness.check(
        "the wrapper never recorded a completion status",
        evidence.psql_exit is None,
        repr(evidence.psql_exit),
    )
    if child_pid:
        harness.sh(f"kill -9 {child_pid}", user="root")
    # The BusyBox `timeout` may have forked `env`/psql; close every tagged survivor.
    harness.kill_tagged(tag)
    deadline = time.time() + 10
    while time.time() < deadline:
        evidence = enroll._handoff_transport(
            docker, run_id, container_id=harness.container_id
        )
        if not evidence.client_alive:
            break
        time.sleep(0.1)
    harness.check(
        "termination becomes positive only after the orphan is gone",
        enroll.enrollment.handoff_termination_failures(evidence) == (),
        repr(evidence),
    )


def scenario_identity(harness: Harness, docker) -> None:
    print("\n== scenario: immutable container-id binding ==")
    old_id = harness.container_id
    alive = harness.run(
        ["docker", "exec", "-u", "postgres", old_id, "true"], timeout=30
    )
    harness.check("the verified id answers", alive.returncode == 0, repr(alive))
    # Replace the container under the same name.
    harness.run(["docker", "rm", "-f", harness.name], timeout=60)
    started = harness.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            harness.name,
            "--network",
            "none",
            "--entrypoint",
            "sh",
            harness.image,
            "-c",
            "sleep 900",
        ]
    )
    harness.check("replacement container started", started.returncode == 0)
    old = harness.run(
        ["docker", "exec", "-u", "postgres", old_id, "true"], timeout=30
    )
    by_name = harness.run(
        ["docker", "exec", "-u", "postgres", harness.name, "true"], timeout=30
    )
    harness.check("the old immutable id no longer answers", old.returncode != 0)
    harness.check("the mutable name now resolves to the replacement", by_name.returncode == 0)
    run_id = uuid.uuid4().hex
    evidence = enroll._handoff_transport(docker, run_id, container_id=old_id)
    harness.check(
        "a probe bound to the old id is unobserved",
        evidence.observed is False,
        repr(evidence),
    )
    harness.check(
        "an unobserved probe is never termination",
        enroll.enrollment.handoff_termination_failures(evidence)
        == ("transport:unobserved",),
        repr(enroll.enrollment.handoff_termination_failures(evidence)),
    )
    try:
        docker.verify_handoff_container(old_id)
    except enroll.DockerError as error:
        harness.check(
            "identity re-verification refuses the replacement",
            error.code in ("container_identity", "container_replacement", "docker_inspect"),
            error.code,
        )
    else:
        harness.check("identity re-verification refuses the replacement", False, "accepted")


def main() -> int:
    harness = Harness()
    print(f"image: {harness.image}")
    print(f"container: {harness.name}")
    try:
        harness.start()
        docker = harness.docker_helper()
        scenario_delivery(harness, docker)
        scenario_busybox_timeout(harness, docker)
        scenario_orphan(harness, docker)
        scenario_identity(harness, docker)
    finally:
        harness.stop()
    print()
    if harness.failures:
        print(f"FAILED: {len(harness.failures)} of {len(harness.checks) + len(harness.failures)} checks")
        for failure in harness.failures:
            print(f"  - {failure}")
        return 1
    print(f"OK: {len(harness.checks)} native checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
