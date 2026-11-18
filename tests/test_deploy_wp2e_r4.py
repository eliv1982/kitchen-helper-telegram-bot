"""WP-2E R4: the three final blocking findings of the Codex review.

R4-F1  A SIGTERM that landed after the accepted tool had returned (forwarding its stdout, reading
       its status record, running or confirming the verified fallback) escaped `Rehearsal.run`'s
       `finally`, which removes only the scratch directory. The helper's one disposable container
       could therefore be left unaccounted for, and no recovery identity was printed. The whole
       helper-result/fallback phase is now one clean-up-responsibility boundary: a signal that
       lands there is deferred until the resource outcome is resolved.

R4-F2  `read_status()` tested `status` / `cleanup` for membership in a frozenset without checking
       their JSON type. `status: []` or `cleanup: {}` raised TypeError, which escaped the caller
       before the fallback ran and lost the container identity. Every malformed type now fails
       closed in `read_status()`, so the caller sees "no usable record" and runs the fallback.

R4-F3  The attachment proof was run through the pg_restore invocation contract, which accepts 0
       and 1. An uncaught Python/SQLAlchemy/connection/import/environment exception also exits 1,
       so a connection timeout was reported as `attachment_proof` (NOT RESTORABLE). The proof now
       answers with an explicit `verdict` (`ok` / `refused`) and only `refused` is artifact
       evidence; anything else is `attachment_proof_environment`, exit 4. (R5 later moved the
       deliberate refusal to its own status, 3, and framed the record with a per-run token; the
       program tests below assert the current contract.)

The R4-F1 probes drive the REAL `deploy/offsite_rehearsal.py` through a real POSIX process (native
on POSIX, through WSL on Windows). Only `rclone`, `age` and the accepted WP-2D tool are stood in
for; the parent process, its signal handlers, the real `Runner`, the status-record reader and the
scratch clean-up are the ones that ship. The signal is injected deterministically by a driver that
wraps `print` / `Path.read_bytes`, so the exact line is reproducible instead of a timing race.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from deploy.postgres import maintenance
from tests.deploy_support import REPO_ROOT
from tests.offsite_support import FIXED_NAME, fake_encrypt, load_rehearsal, offsite
from tests.test_deploy_offsite_postgres import _unconfirmed, v2_manifest, v2_world, write_v2_archive
from tests.test_deploy_postgres_rehearsal_cleanup import _linux_path, _linux_shell
from tests.test_deploy_wp2e_r3 import RUN_ID, record_bytes

rehearsal = load_rehearsal()

CONTRACT = "house-helper-maintenance-status/1"


# --- R4-F2: malformed status JSON types fail closed --------------------------------------------


def _status_reader(tmp_path: Path, text) -> object:
    path = tmp_path / "status.json"
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return rehearsal.read_status(path, "rehearse", RUN_ID)


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": []},                                   # unhashable: frozenset membership raised TypeError
        {"status": {}},                                   # unhashable
        {"status": ["passed"]},                           # a list with a valid-looking token
        {"status": {"value": "passed"}},                  # a nested object
        {"status": True},                                 # a bool is not a status token
        {"status": 1},                                    # an int is not a status token
        {"cleanup": []},                                  # unhashable
        {"cleanup": {}},                                  # unhashable
        {"cleanup": ["confirmed"]},                       # a list with a valid-looking token
        {"cleanup": {"value": "confirmed"}},              # a nested object
        {"cleanup": 1},                                   # an int is not a cleanup token
        {"status": "failed", "code": []},                 # the code type check already failed closed
        {"status": "failed", "code": {}},                 # nested code
        {"status": "failed", "code": ["archive_integrity"]},
        {"run_id": []},                                   # unexpected run id type
        {"run_id": {"value": RUN_ID}},
        {"contract": []},                                 # unexpected contract type
        {"command": []},                                  # unexpected command type
    ],
    ids=[
        "status-list", "status-object", "status-list-token", "status-nested", "status-bool",
        "status-int", "cleanup-list", "cleanup-object", "cleanup-list-token", "cleanup-nested",
        "cleanup-int", "code-list", "code-object", "code-list-token", "run-id-list", "run-id-object",
        "contract-list", "command-list",
    ],
)
def test_a_malformed_record_type_is_rejected_without_an_unhandled_type_error(tmp_path, overrides):
    value = {
        "contract": CONTRACT, "command": "rehearse", "run_id": RUN_ID,
        "status": "passed", "cleanup": "confirmed", "code": None,
    }
    value.update(overrides)
    text = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
    assert _status_reader(tmp_path, text) is None


@pytest.mark.parametrize(
    "body",
    [
        b"[]\n",                                          # the record is a JSON array
        b'"passed"\n',                                    # the record is a JSON string
        b"null\n",                                        # the record is JSON null
        b'{"contract":"house-helper-maintenance-status/1","command":"rehearse",'
        b'"run_id":"0123456789abcdef0123456789abcdef","status":[],"cleanup":"unconfirmed",'
        b'"code":null}\n',
    ],
    ids=["top-level-array", "top-level-string", "top-level-null", "status-array"],
)
def test_an_unexpected_record_shape_is_rejected(tmp_path, body):
    assert _status_reader(tmp_path, body) is None


def test_a_malformed_initial_record_still_triggers_the_verified_fallback(tmp_path):
    """R4-F2: the initial record's `status: []` used to raise TypeError before the fallback."""
    world, _ = v2_world(tmp_path)
    world.fake.restore_exit = 4
    # The fake writes the record verbatim; the callable form receives the run id the parent made.
    world.fake.restore_record = lambda run_id: record_bytes(
        run_id, status=[], cleanup="unconfirmed", code=None
    )
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED, world.text
    assert len(world.fake.cleanup_calls) == 1            # the fallback ran
    assert "unexpected TypeError" not in world.text
    assert "PASSED" not in world.text


@pytest.mark.parametrize("cleanup_value", [[], {}, ["confirmed"], 1], ids=["list", "object", "token-list", "int"])
def test_a_malformed_cleanup_value_still_triggers_the_verified_fallback(tmp_path, cleanup_value):
    world, _ = v2_world(tmp_path)
    world.fake.restore_exit = 4
    world.fake.restore_record = lambda run_id: record_bytes(
        run_id, cleanup=cleanup_value, status="failed", code="docker_infrastructure"
    )
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED, world.text
    assert len(world.fake.cleanup_calls) == 1
    assert "unexpected TypeError" not in world.text


def test_a_malformed_fallback_record_is_failed_with_the_recovery_identity(tmp_path):
    """R4-F2: the fallback's own record is read through the same type-safe reader."""
    world, _ = v2_world(tmp_path)
    _unconfirmed(world, status="passed", code=None)      # the helper's own removal is unconfirmed
    world.fake.cleanup_record = lambda run_id: record_bytes(
        run_id, command="cleanup-rehearsal", status=[], cleanup={}
    )
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED, world.text
    name = rehearsal.REHEARSAL_CONTAINER_PREFIX + world.fake.restore_run_id
    assert name in world.text and "could not be confirmed removed" in world.text
    assert "cleanup-rehearsal --run-id" in world.text
    assert "unexpected TypeError" not in world.text
    assert "NOT RESTORABLE" not in world.text and "PASSED" not in world.text


def test_a_nested_fallback_record_is_failed_with_the_recovery_identity(tmp_path):
    world, _ = v2_world(tmp_path)
    _unconfirmed(world, status="passed", code=None)
    world.fake.cleanup_record = lambda run_id: json.dumps({
        "contract": CONTRACT,
        "command": "cleanup-rehearsal",
        "run_id": run_id,
        "status": "passed",
        "cleanup": {"value": "confirmed"},
        "code": None,
    }).encode("utf-8") + b"\n"
    assert world.rehearse(expect=("messages=1",)) == rehearsal.EXIT_FAILED, world.text
    name = rehearsal.REHEARSAL_CONTAINER_PREFIX + world.fake.restore_run_id
    assert name in world.text and "could not be confirmed removed" in world.text
    assert "NOT RESTORABLE" not in world.text


# --- R4-F3: the attachment proof's own machine-readable verdict --------------------------------

STUB_DATABASE = r'''
import json
import os


class _Engine:
    def dispose(self):
        pass


def create_db_engine(*args, **kwargs):
    if os.environ.get("STUB_MODE") == "connection":
        raise RuntimeError("connection timed out")
    return _Engine()


def create_session_factory(engine):
    values = json.loads(os.environ.get("STUB_VALUES", "[0, 0, 0]"))

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def scalar(self, statement):
            return values.pop(0)

        def execute(self, statement):
            row = json.loads(os.environ.get("STUB_ROW", "{}"))

            class _Result:
                def one(self):
                    return type("Row", (), row)()

            return _Result()

    return _Session
'''

STUB_MODELS = r'''
import sqlalchemy as sa
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class MessageAttachment(Base):
    __tablename__ = "message_attachments"

    user_id = sa.Column(sa.String, primary_key=True)
    id = sa.Column(sa.String, primary_key=True)
    sha256 = sa.Column(sa.String)
    size_bytes = sa.Column(sa.Integer)
    data = sa.Column(sa.LargeBinary)
'''

STUB_ATTACHMENTS = r'''
import os


def get_for_user(session, user_id, attachment_id, now=None):
    if os.environ.get("STUB_LOADER") == "empty":
        return None
    data = b"abd" if os.environ.get("STUB_LOADER") == "bad" else b"abc"

    class _Loaded:
        id = attachment_id

    loaded = _Loaded()
    loaded.data = data
    return loaded
'''


def _write_stub_package(base: Path) -> None:
    package = base / "house_helper" / "persistence"
    package.mkdir(parents=True)
    (base / "house_helper" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "database.py").write_text(STUB_DATABASE, encoding="utf-8")
    (package / "models.py").write_text(STUB_MODELS, encoding="utf-8")
    (package / "attachments.py").write_text(STUB_ATTACHMENTS, encoding="utf-8")


PROOF_NONCE = "0123456789abcdef0123456789abcdef"


def _run_proof_program(tmp_path: Path, *, stubs: bool = True, extra_env=None, nonce=PROOF_NONCE):
    base = tmp_path / "proof-program"
    base.mkdir()
    if stubs:
        _write_stub_package(base)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    if nonce is not None:
        environment[maintenance._ATTACHMENT_PROOF_NONCE_ENV] = nonce
    environment.update(extra_env or {})
    if stubs:
        environment["PYTHONPATH"] = str(base)
    return subprocess.run(
        [sys.executable, "-c", maintenance._ATTACHMENT_PROOF],
        cwd=base, env=environment, capture_output=True, timeout=120, check=False,
    )


def _verdict(done):
    prefix = maintenance._ATTACHMENT_PROOF_PREFIX + " "
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith(prefix):
            token, separator, payload = line[len(prefix):].partition(" ")
            assert separator and token == PROOF_NONCE
            return json.loads(payload)
    return None


def test_the_proof_program_emits_ok_when_it_completes(tmp_path):
    done = _run_proof_program(tmp_path, extra_env={"STUB_VALUES": "[0, 0, 0]"})
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert _verdict(done) == {
        "verdict": "ok", "count": 0, "bytes": 0, "declared_bytes": 0, "sample": False,
    }


@pytest.mark.parametrize(
    "values, loader, reason",
    [
        ("[1, 10, 9]", None, "byte_total_mismatch"),
        ("[1, 3, 3]", "empty", "loader_empty"),
        ("[1, 3, 3]", "bad", "loader_mismatch"),
    ],
    ids=["byte-total", "loader-empty", "loader-mismatch"],
)
def test_the_proof_program_refuses_with_an_explicit_machine_readable_reason(tmp_path, values, loader, reason):
    row = {
        "user_id": "u", "id": "a", "sha256": hashlib.sha256(b"abc").hexdigest(), "size_bytes": 3,
    }
    env = {"STUB_VALUES": values, "STUB_ROW": json.dumps(row)}
    if loader is not None:
        env["STUB_LOADER"] = loader
    done = _run_proof_program(tmp_path, extra_env=env)
    assert done.returncode == 3
    assert _verdict(done) == {"verdict": "refused", "reason": reason}
    assert b"verdict" not in done.stderr


def test_a_connection_timeout_inside_the_proof_program_has_no_verdict(tmp_path):
    """The exact R4-F3 case: an uncaught SQLAlchemy/connection error is not artifact evidence."""
    done = _run_proof_program(tmp_path, extra_env={"STUB_MODE": "connection"})
    assert done.returncode != 0
    assert _verdict(done) is None
    assert b"connection timed out" in done.stderr
    assert b"verdict" not in done.stdout


def test_an_import_failure_inside_the_proof_program_has_no_verdict(tmp_path):
    done = _run_proof_program(tmp_path, stubs=False, extra_env={})
    assert done.returncode != 0
    assert _verdict(done) is None


def test_a_proof_program_without_its_result_channel_token_has_no_verdict(tmp_path):
    """R5: without the host's per-run token the program cannot produce authoritative evidence."""
    done = _run_proof_program(tmp_path, nonce=None)
    assert done.returncode == 4
    assert maintenance._ATTACHMENT_PROOF_PREFIX.encode() not in done.stdout
    assert _verdict(done) is None


def test_the_host_boundary_maps_an_uncaught_exception_to_environment_not_artifact():
    class _Tools:
        def run(self, program, **kwargs):
            return subprocess.CompletedProcess(
                program, 1, stdout=b"", stderr=b"Traceback (most recent call last):\nOperationalError: connection timed out\n"
            )

    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.attachment_proof(
            _Tools(), app_image="house-helper:local", database="house_helper_restore_x",
            role="hh_restore", passfile_value=None, network="none",
        )
    assert refused.value.code == "attachment_proof_environment"


def test_the_environment_proof_code_reaches_exit_4_and_the_parent_as_failed(monkeypatch, tmp_path, capsys):
    """A connection timeout is exit 4 with `attachment_proof_environment`, never an artifact refusal."""
    from types import SimpleNamespace

    from tests.test_deploy_postgres_rehearsal_cleanup import FakeDocker, IMAGE, _rehearse_argv

    fixture = maintenance.backup.load_object_contract_fixture(
        REPO_ROOT / "tests" / "fixtures" / "pg_object_contract.json"
    )
    point = SimpleNamespace(manifest={"alembic_revision": fixture["revision"], "tables": {}})
    real_tool_containers = maintenance.ToolContainers
    monkeypatch.setattr(maintenance, "_image_from_compose", lambda compose: IMAGE)
    monkeypatch.setattr(
        maintenance, "ToolContainers",
        lambda image: real_tool_containers(image, runner=FakeDocker()),
    )
    monkeypatch.setattr(maintenance, "validate_archive", lambda *a, **k: (point, ()))
    monkeypatch.setattr(maintenance.backup, "toc_contract_failures", lambda *a, **k: ())

    def fail(*args, **kwargs):
        raise maintenance.MaintenanceError(
            "attachment_proof_environment", "the attachment proof did not reach a verdict"
        )

    monkeypatch.setattr(maintenance, "rehearse", fail)
    status = tmp_path / "status.json"
    argv = _rehearse_argv(tmp_path) + ["--run-id", RUN_ID, "--status-file", str(status)]
    assert maintenance.main(argv) == maintenance.EX_UNAVAILABLE
    assert "refused [attachment_proof_environment]" in capsys.readouterr().err
    record = json.loads(status.read_text(encoding="utf-8"))
    assert record["status"] == "failed" and record["cleanup"] == "confirmed"
    assert record["code"] == "attachment_proof_environment"
    # The parent's truth table: a failed restore with an environment code is FAILED, never NOT
    # RESTORABLE, for both the confirmed and the fallback-confirmed cleanup states.
    for cleanup, returncode in (("confirmed", 4), ("unconfirmed", 4), ("confirmed", 3)):
        assert rehearsal.classify_status(
            {"status": "failed", "cleanup": cleanup, "code": "attachment_proof_environment"}, returncode
        ) == "failed"


# --- R4-F1: real POSIX signals during the helper-result/fallback phase -------------------------

PROBE_DRIVER = r'''
import builtins
import os
import pathlib
import runpy
import signal
import sys
import types

script = os.environ["PROBE_SCRIPT"]
mode = os.environ.get("PROBE_KILL_AT", "")

# The probe host's Python has no SQLAlchemy (nothing here installs into WSL). The frozen manifest
# check reads the model metadata only to learn the expected table set; register a stub that names
# exactly the tables the archive itself declares. Everything else runs from the real modules.
names = [name for name in os.environ.get("PROBE_TABLE_NAMES", "").split(",") if name]
models = types.ModuleType("house_helper.persistence.models")
base = types.ModuleType("house_helper.persistence.base")
base.Base = types.SimpleNamespace(metadata=types.SimpleNamespace(tables={name: None for name in names}))
sys.modules["house_helper.persistence.models"] = models
sys.modules["house_helper.persistence.base"] = base

if mode == "forwarded-line":
    original_print = builtins.print

    def hooked_print(*values, **kwargs):
        text = " ".join(str(value) for value in values)
        if kwargs.get("file") is sys.stderr and "FORWARDED-LINE" in text:
            os.kill(os.getpid(), signal.SIGTERM)
        return original_print(*values, **kwargs)

    builtins.print = hooked_print
elif mode == "cleanup-status-read":
    original_read = pathlib.Path.read_bytes

    def hooked_read(self):
        if self.name == "cleanup-status.json":
            os.kill(os.getpid(), signal.SIGTERM)
        return original_read(self)

    pathlib.Path.read_bytes = hooked_read

sys.argv = [script, *sys.argv[1:]]
runpy.run_path(script, run_name="__main__")
'''

PROBE_RCLONE = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import sys

state = pathlib.Path(os.environ["PROBE_REMOTE_DIR"])
args = sys.argv[1:]

if args[:2] == ["config", "providers"]:
    print(json.dumps([{"Name": "yandex", "Options": [{"Name": "client_id"}, {"Name": "app_folder"}]}]))
    sys.exit(0)

command, rest, index = args[0], [], 1
while index < len(args):
    value = args[index]
    if value in ("--config", "--log-level"):
        index += 2
        continue
    if value.startswith("--"):
        index += 1
        continue
    rest.append(value)
    index += 1

if command == "backend":
    print(json.dumps({"Name": "fake", "Features": {"Move": True, "Copy": True}}))
    sys.exit(0)
if command == "lsjson":
    items = []
    for path in sorted(state.iterdir()):
        if not path.is_file():
            continue
        data = path.read_bytes()
        items.append({
            "Name": path.name, "Size": len(data), "IsDir": False,
            "Hashes": {"md5": hashlib.md5(data).hexdigest()},
        })
    print(json.dumps(items))
    sys.exit(0)
if command == "cat":
    name = rest[0].split("/", 1)[1]
    sys.stdout.buffer.write((state / name).read_bytes())
    sys.exit(0)
if command == "copyto":
    source, destination = rest
    pathlib.Path(destination).write_bytes((state / source.split("/", 1)[1]).read_bytes())
    sys.exit(0)
sys.exit(99)
'''

PROBE_AGE = r'''#!/usr/bin/env python3
import os
import pathlib
import sys

sys.stdout.buffer.write(pathlib.Path(os.environ["PROBE_TAR_PATH"]).read_bytes())
'''

PROBE_HELPER = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
run_id = args[args.index("--run-id") + 1]
status = pathlib.Path(args[args.index("--status-file") + 1])
record = {
    "contract": "house-helper-maintenance-status/1",
    "command": "cleanup-rehearsal" if "cleanup-rehearsal" in args else "rehearse",
    "run_id": run_id,
}

if record["command"] == "rehearse":
    print("FORWARDED-LINE from the accepted tool", flush=True)
    record.update(status="passed", cleanup="unconfirmed", code=None)
    status.write_text(json.dumps(record, sort_keys=True) + "\n")
    sys.exit(4)

pathlib.Path(os.environ["PROBE_STATE"], "cleanup-called").write_text("called")
if os.environ.get("PROBE_CLEANUP", "confirmed") == "confirmed":
    record.update(status="passed", cleanup="confirmed", code=None)
    status.write_text(json.dumps(record, sort_keys=True) + "\n")
    sys.exit(0)
record.update(status="failed", cleanup="unconfirmed", code="rehearsal_cleanup")
status.write_text(json.dumps(record, sort_keys=True) + "\n")
# Exit 0 with an unconfirmed record is contradictory, which the parent fails closed on - and it
# forces the parent to READ this record, which is the exact moment the R4-F1 probe signals it.
sys.exit(0)
'''

RCLONE_CONF = """[fake]
type = yandex
client_id = test-client-id
client_secret = test-client-secret
token = {"access_token":"test"}
app_folder = true
"""
GOOD_KEY = "AGE-SECRET-KEY-1" + "Q" * 58


def _parent_rig(tmp_path: Path) -> dict:
    """The REAL offsite_rehearsal.py set up for one v2 point, with rclone/age/the helper stood in."""
    probe = tmp_path / "parent-probe"
    fakebin = probe / "fakebin"
    remote = probe / "remote"
    work = probe / "work"
    state = probe / "state"
    for directory in (fakebin, remote, work, state):
        directory.mkdir(parents=True)

    for name, body in (("rclone", PROBE_RCLONE), ("age", PROBE_AGE)):
        path = fakebin / name
        path.write_bytes(body.encode("utf-8"))   # LF: WSL executes the shebang
        path.chmod(0o755)

    helper = probe / "maintenance.py"
    helper.write_bytes(PROBE_HELPER.encode("utf-8"))
    driver = probe / "driver.py"
    driver.write_bytes(PROBE_DRIVER.encode("utf-8"))

    archive = probe / "recovery.tar"
    write_v2_archive(archive, dump=b"synthetic PostgreSQL dump for the R4 parent probe\n")
    sealed = fake_encrypt(archive.read_bytes())
    digests = offsite.Digests(
        len(sealed),
        hashlib.sha256(sealed).hexdigest(),
        hashlib.sha1(sealed).hexdigest(),
        hashlib.md5(sealed).hexdigest(),
    )
    (remote / FIXED_NAME).write_bytes(sealed)
    (remote / (FIXED_NAME + offsite.COMMIT_SUFFIX)).write_bytes(offsite.commit_record(FIXED_NAME, digests))

    return {
        "probe": probe, "fakebin": fakebin, "remote": remote, "work": work,
        "state": state, "helper": helper, "driver": driver,
        "table_names": ",".join(sorted(v2_manifest()["tables"])),
    }


def _run_parent(rig: dict, *, kill_at: str, cleanup: str = "confirmed") -> dict:
    p = {name: (_linux_path(value) if isinstance(value, Path) else value) for name, value in rig.items()}
    script = f'''
set -u
BASE=$(mktemp -d /tmp/hh-r4-parent.XXXXXX)
trap 'rm -rf "$BASE"' EXIT
umask 077
printf '%s\\n' '{GOOD_KEY}' > "$BASE/identity.txt"
chmod 600 "$BASE/identity.txt"
cat > "$BASE/rclone.conf" <<'CONF'
{RCLONE_CONF}CONF
chmod 600 "$BASE/rclone.conf"
cd "{p["probe"]}"
PATH="{p["fakebin"]}:$PATH" \\
PROBE_SCRIPT="{_linux_path(REPO_ROOT / "deploy" / "offsite_rehearsal.py")}" \\
PROBE_TAR_PATH="{_linux_path(rig["probe"] / "recovery.tar")}" \\
PROBE_REMOTE_DIR="{p["remote"]}" \\
PROBE_STATE="{p["state"]}" \\
PROBE_TABLE_NAMES="{rig["table_names"]}" \\
PROBE_KILL_AT="{kill_at}" \\
PROBE_CLEANUP="{cleanup}" \\
python3 "{p["driver"]}" \\
  --rclone-config "$BASE/rclone.conf" --remote fake:house-helper \\
  --identity-file "$BASE/identity.txt" --work-dir "{p["work"]}" \\
  --expect-rows messages=1 --postgres-maintenance-script "{p["helper"]}" \\
  > out.log 2> err.log
echo "EXIT=$?"
if [ -f "{p["state"]}/cleanup-called" ]; then echo CLEANUP-CALLED; else echo NO-CLEANUP; fi
if [ -n "$(ls -A "{p["work"]}" 2>/dev/null)" ]; then echo WORK-REMAINS; else echo WORK-EMPTY; fi
'''
    done = subprocess.run(_linux_shell() + [script], capture_output=True, timeout=180, check=False)
    output = done.stdout.decode("utf-8", "replace")
    errors = (rig["probe"] / "err.log").read_text(encoding="utf-8", errors="replace") if (rig["probe"] / "err.log").exists() else ""
    return {"output": output, "errors": errors, "returncode": done.returncode}


def test_a_parent_sigterm_at_the_forwarded_result_line_runs_the_fallback_first(tmp_path):
    """R4-F1's exact first case: the signal lands in the stdout-forwarding loop (line 631).

    Before R4 the raising handler escaped `Rehearsal.run` and only the scratch directory was
    removed: no fallback ran, the helper's container was unaccounted for, and the parent exited
    128+15 claiming "the clean-up ran". Now the signal is deferred, the unconfirmed removal is
    retried and confirmed, and only then is the cancellation preserved.
    """
    rig = _parent_rig(tmp_path)
    result = _run_parent(rig, kill_at="forwarded-line")
    assert "NO-BLOCK" not in result["output"], result["output"]
    assert "EXIT=143" in result["output"], result["output"] + result["errors"]
    assert "CLEANUP-CALLED" in result["output"], result["output"]
    assert "WORK-EMPTY" in result["output"], result["output"]
    assert "interrupted by signal 15" in result["errors"], result["errors"]
    assert "PASSED" not in result["errors"], result["errors"]


def test_a_parent_sigterm_during_the_fallback_status_read_is_failed_with_the_identity(tmp_path):
    """R4-F1's exact second case: the signal lands while the fallback's record is read.

    The fallback removal stayed unconfirmed. Before R4 the TypeError-free path did not exist and
    the Interrupted escaped the confirmation, so the parent said "the clean-up ran" and printed
    no recovery identity. Now the read is deferred and its result decides: FAILED (exit 1) with
    the exact container name and the recovery command.
    """
    rig = _parent_rig(tmp_path)
    result = _run_parent(rig, kill_at="cleanup-status-read", cleanup="unconfirmed")
    assert "EXIT=1" in result["output"], result["output"] + result["errors"]
    assert "CLEANUP-CALLED" in result["output"], result["output"]
    assert "WORK-EMPTY" in result["output"], result["output"]
    assert "FAILED" in result["errors"], result["errors"]
    assert rehearsal.REHEARSAL_CONTAINER_PREFIX in result["errors"], result["errors"]
    assert "could not be confirmed removed" in result["errors"], result["errors"]
    assert "cleanup-rehearsal --run-id" in result["errors"], result["errors"]
    assert "interrupted by signal" not in result["errors"], result["errors"]
    assert "PASSED" not in result["errors"], result["errors"]
