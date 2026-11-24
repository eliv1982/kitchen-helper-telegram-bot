"""C7-P4-A9-H1-FIX-01: the isolated restore's dump must be readable by the `postgres` OS user.

A9-R2 ended in `pg_restore exit 1`. The dump is copied into the disposable container with its
restrictive mode (0600) but is owned by a different OS user than the `postgres` process that runs the
non-root `pg_restore`, so that process cannot open it (H1-PROBE-01 demonstrated the access failure
with the pinned image).

`rehearse` now sets the copy's owner to `postgres:postgres` with an exact root command and verifies
that `postgres` can read it, straight after the copy and before anything is restored. These tests are
offline. The container is a fake that models only what matters here: who owns the dump, its mode, and
whether a command run as a given OS user could open it. Its `pg_restore` fails exactly as the real one
did when the dump cannot be opened, so the original failure is reproduced rather than assumed. No
Docker, no real restore, no archive and no credential is involved.
"""

from __future__ import annotations

import re
import subprocess
from types import SimpleNamespace

import pytest

from deploy.postgres import maintenance
from house_helper.persistence import pg_backup as backup
from tests.offsite_support import load_rehearsal

IMAGE = "postgres@sha256:" + "a" * 64
RUN_ID = "0123456789abcdef0123456789abcdef"
CONTAINER = "c" * 64
DUMP_PATH = f"/tmp/{backup.DUMP_NAME}"
REVISION = "0014_user_measure_overrides"
SYSTEM_IDENTIFIER = "7709166311476633635"
FOREIGN_OWNER = "1000:1000"  # an OS user other than `postgres`
TOC = (
    "1; 2615 0 SCHEMA - public hh_owner\n"
    "2; 0 0 SEQUENCE SET public messages_id_seq hh_owner\n"
)
RESTORE_LIST = b"2; 0 0 SEQUENCE SET public messages_id_seq hh_owner\n"
# A recognisable line of raw tool output. No refusal may ever repeat it.
SENTINEL = b"SECRET-TOOL-STDERR"
STOPPED = b"Error response from daemon: Container " + CONTAINER.encode() + b" is not running\n"
_DATABASE_TEXT = re.compile(r"house_helper_restore_[0-9a-f]+")
_DATABASE_BYTES = re.compile(rb"house_helper_restore_[0-9a-f]+")
_OCTAL_MODE = re.compile(r"0?[0-7]{3,4}")


class _Container:
    """The disposable container, as far as the dump's access is concerned.

    `copy_in` leaves the dump with mode 0600 and a foreign owner (the A9-R2 state). A `chown` run as
    root changes only the owner. `test -r` and `pg_restore` run as an OS user succeed only if that
    user could really open the file under the owner, group and mode bits the dump has at that moment.
    Every container command is recorded in one ordered log, together with the stand-in stages."""

    def __init__(self, *, owner=FOREIGN_OWNER, chown_result=None, readability_result=None,
                 chown_changes_owner=True):
        self.owner = owner
        self.mode = 0o600
        self.modes = [0o600]  # every mode the dump ever had
        self.chown_result = chown_result  # (status, stderr) answered instead of running chown
        self.readability_result = readability_result  # (status, stderr) answered instead of test -r
        self.chown_changes_owner = chown_changes_owner
        self.events: list[tuple] = []
        self.restore_saw = None  # (owner, mode) at the moment pg_restore ran

    def can_read(self, user: str) -> bool:
        if user == "root":
            return True
        owner, group = self.owner.split(":")
        if owner == user:
            return bool(self.mode & 0o400)
        if group == user:
            return bool(self.mode & 0o040)
        return bool(self.mode & 0o004)

    def start_detached(self, program, *, name, environment=(), network="none", tmpfs=()):
        self.events.append(("start",))
        return CONTAINER

    def copy_in(self, container, source, target):
        self.events.append(("copy_in", target))

    def pg_restore_list(self, work):
        self.events.append(("list",))
        return TOC

    def exec(self, container, program, *, stdin=None, user="postgres", timeout=300):
        program = tuple(program)
        self.events.append((
            "exec",
            user,
            tuple(_DATABASE_TEXT.sub("<database>", argument) for argument in program),
            None if stdin is None else _DATABASE_BYTES.sub(b"<database>", stdin),
        ))
        def done(status=0, stdout=b"", stderr=b""):
            return subprocess.CompletedProcess(program, status, stdout, stderr)

        if program[0] == "chown":
            if self.chown_result is not None:
                return done(self.chown_result[0], b"", self.chown_result[1])
            if user != "root":
                return done(1, b"", b"chown: changing ownership: Operation not permitted\n")
            if self.chown_changes_owner:
                self.owner = program[1]
            return done()
        if program[:2] == ("test", "-r"):
            if self.readability_result is not None:
                return done(self.readability_result[0], b"", self.readability_result[1])
            return done(0 if self.can_read(user) else 1)
        if program[0] == "chmod":
            # Only here so a future mode change would be seen: anything but an octal mode is wider.
            target = program[1]
            self.mode = int(target, 8) if _OCTAL_MODE.fullmatch(target) else 0o644
            self.modes.append(self.mode)
            return done()
        if program[0] == "pg_restore":
            self.restore_saw = (self.owner, self.mode)
            if not self.can_read(user):
                return done(1, b"", b"pg_restore: error: could not open input file: Permission denied\n")
            return done()
        if stdin and b"system_identifier" in stdin:
            return done(0, SYSTEM_IDENTIFIER.encode() + b"\n")
        if stdin and b"version_num" in stdin:
            return done(0, REVISION.encode() + b"\n")
        return done()


def _rehearse(monkeypatch, tmp_path, container, *, prepare=True):
    """Run the real `rehearse` body against the fake container.

    The stages that need a real PostgreSQL (the verification parse, the Alembic check, the attachment
    proof) are stand-ins that only record that they ran, in order, in the container's log. With
    `prepare=False` the dump preparation is a no-op, which is the flow as it was before this fix."""

    def stage(name, result):
        def run(*args, **kwargs):
            container.events.append(("stage", name))
            return result

        return run

    monkeypatch.setattr(maintenance, "_await_postgres", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "_verification_sql", lambda manifest: "SELECT 1;")
    monkeypatch.setattr(
        maintenance, "_parse_verification",
        stage("parse_verification", SimpleNamespace(failures=(), evidence={"revision": REVISION})),
    )
    monkeypatch.setattr(maintenance, "alembic_check", stage("alembic_check", None))
    monkeypatch.setattr(
        maintenance, "attachment_proof",
        stage("attachment_proof", {"count": 0, "bytes": 0, "declared_bytes": 0, "sample": False}),
    )
    if not prepare:
        monkeypatch.setattr(maintenance, "_prepare_restore_dump", lambda *a, **k: None)
    point = SimpleNamespace(
        manifest={"alembic_revision": REVISION, "datcollate": "C", "datctype": "C", "tables": {}}
    )
    return maintenance.rehearse(
        container, point=point, work=tmp_path, fixture={}, app_image=IMAGE, run_id=RUN_ID
    )


def _position(events, program_head):
    """The index of the one `exec` whose program starts with `program_head`."""
    found = [
        index for index, event in enumerate(events)
        if event[0] == "exec" and event[2][: len(program_head)] == program_head
    ]
    assert len(found) == 1, found
    return found[0]


def _refusal(monkeypatch, tmp_path, container) -> maintenance.MaintenanceError:
    with pytest.raises(maintenance.MaintenanceError) as refused:
        _rehearse(monkeypatch, tmp_path, container)
    return refused.value


# --- the ownership correction precedes the non-root restore -------------------------------------


def test_ownership_is_corrected_by_an_exact_root_command_right_after_the_copy_and_before_the_restore(
    monkeypatch, tmp_path
):
    container = _Container()
    _rehearse(monkeypatch, tmp_path, container)

    events = container.events
    copy = events.index(("copy_in", DUMP_PATH))
    chown = _position(events, ("chown",))
    check = _position(events, ("test",))
    restore = _position(events, ("pg_restore",))

    # Straight after the copy, in this order, and both before anything is restored.
    assert (chown, check) == (copy + 1, copy + 2)
    assert check < restore
    # The exact root command, then the readability check as the restoring OS user.
    assert events[chown] == ("exec", "root", ("chown", "postgres:postgres", DUMP_PATH), None)
    assert events[check] == ("exec", "postgres", ("test", "-r", DUMP_PATH), None)
    # The restore itself is still the non-root one, and it found a dump it could open.
    assert events[restore][1] == "postgres"
    assert container.restore_saw[0] == "postgres:postgres"


def test_without_the_preparation_the_modelled_dump_cannot_be_opened_by_the_restore(
    monkeypatch, tmp_path
):
    """The control: the fake reproduces A9-R2, so the tests above and below depend on the fix."""
    container = _Container()
    with pytest.raises(maintenance.MaintenanceError) as refused:
        _rehearse(monkeypatch, tmp_path, container, prepare=False)

    assert refused.value.code == "isolated_restore"
    assert container.restore_saw == (FOREIGN_OWNER, 0o600)


# --- the restrictive mode is preserved ----------------------------------------------------------


def test_the_dump_keeps_mode_0600_and_is_never_widened(monkeypatch, tmp_path):
    container = _Container()
    _rehearse(monkeypatch, tmp_path, container)

    # At the moment the restore ran the dump was postgres-owned and still exactly 0600.
    assert container.restore_saw == ("postgres:postgres", 0o600)
    assert container.mode == 0o600
    assert set(container.modes) == {0o600}


# --- a failed ownership correction prevents the restore -----------------------------------------

FAILURES = [
    pytest.param(1, b"refused: " + SENTINEL + b"\n", None, id="program-refused"),
    pytest.param(125, SENTINEL + b"\n", "docker_infrastructure", id="docker-125"),
    pytest.param(126, SENTINEL + b"\n", "docker_infrastructure", id="docker-126"),
    pytest.param(127, SENTINEL + b"\n", "docker_infrastructure", id="docker-127"),
    pytest.param(137, SENTINEL + b"\n", "docker_infrastructure", id="killed-137"),
    pytest.param(-9, SENTINEL + b"\n", "docker_infrastructure", id="docker-cli-signalled"),
    pytest.param(1, STOPPED + SENTINEL + b"\n", "docker_infrastructure", id="container-stopped"),
]


@pytest.mark.parametrize("status, stderr, infrastructure", FAILURES)
def test_a_failed_ownership_correction_prevents_the_restore(
    monkeypatch, tmp_path, status, stderr, infrastructure
):
    container = _Container(chown_result=(status, stderr))
    refusal = _refusal(monkeypatch, tmp_path, container)

    assert refusal.code == (infrastructure or "isolated_dump_owner")
    # Fail closed: the chown was the last thing the container was asked to do.
    assert container.events[-1][:3] == ("exec", "root", ("chown", "postgres:postgres", DUMP_PATH))
    assert container.restore_saw is None
    assert not [event for event in container.events if event[0] == "stage"]
    # No raw tool output, and no detail at all, reaches the refusal that is printed.
    assert refusal.detail is None
    assert SENTINEL.decode() not in str(refusal)


# --- a failed readability verification prevents the restore -------------------------------------


@pytest.mark.parametrize("status, stderr, infrastructure", FAILURES)
def test_a_failed_readability_check_prevents_the_restore(
    monkeypatch, tmp_path, status, stderr, infrastructure
):
    container = _Container(readability_result=(status, stderr))
    refusal = _refusal(monkeypatch, tmp_path, container)

    assert refusal.code == (infrastructure or "isolated_dump_access")
    assert container.events[-1][:3] == ("exec", "postgres", ("test", "-r", DUMP_PATH))
    assert container.restore_saw is None
    assert not [event for event in container.events if event[0] == "stage"]
    assert refusal.detail is None
    assert SENTINEL.decode() not in str(refusal)


def test_a_chown_that_reports_success_but_leaves_the_dump_unreadable_is_caught(monkeypatch, tmp_path):
    """The verification is its own check: it asks the `postgres` OS user, not the chown's status."""
    container = _Container(chown_changes_owner=False)
    refusal = _refusal(monkeypatch, tmp_path, container)

    assert refusal.code == "isolated_dump_access"
    assert container.owner == FOREIGN_OWNER
    assert container.restore_saw is None
    assert container.events[-1][:3] == ("exec", "postgres", ("test", "-r", DUMP_PATH))


def test_the_preparation_refusals_are_failed_outcomes_never_not_restorable():
    """Neither is evidence about the archive, so the parent must classify both as FAILED."""
    rehearsal = load_rehearsal()
    for code in ("isolated_dump_owner", "isolated_dump_access"):
        assert code not in rehearsal.ARTIFACT_REFUSALS
        record = {"status": "failed", "cleanup": "confirmed", "code": code}
        assert rehearsal.classify_status(record, rehearsal.MAINTENANCE_REFUSED) == "failed"


# --- a successful preparation changes nothing else ----------------------------------------------


def test_a_successful_preparation_leaves_the_restore_command_and_validation_sequence_unchanged(
    monkeypatch, tmp_path
):
    prepared = _Container()
    result = _rehearse(monkeypatch, tmp_path, prepared)
    assert result["system_identifier"] == SYSTEM_IDENTIFIER

    # The original restore command, unchanged, and run as the same non-root OS user.
    restore = prepared.events[_position(prepared.events, ("pg_restore",))]
    assert restore == (
        "exec", "postgres",
        (
            "pg_restore", "--exit-on-error", "--role=hh_owner", "--no-owner", "--no-privileges",
            "--use-list=/tmp/restore.list", "--dbname", "<database>", DUMP_PATH,
        ),
        None,
    )
    # The restore list it is given is unchanged, and the validation stages follow it in order.
    listed = [e for e in prepared.events if e[0] == "exec" and e[2] == ("sh", "-c", "cat > /tmp/restore.list")]
    assert [e[3] for e in listed] == [RESTORE_LIST]
    assert [e[1] for e in prepared.events if e[0] == "stage"] == [
        "parse_verification", "alembic_check", "attachment_proof",
    ]
    assert prepared.events[-1] == ("stage", "attachment_proof")

    # Nothing else moved: the flow as it was before the fix, on a dump that was already readable,
    # is this exact sequence with only the two preparation commands inserted after the copy.
    before = _Container(owner="postgres:postgres")
    _rehearse(monkeypatch, tmp_path, before, prepare=False)
    copy = before.events.index(("copy_in", DUMP_PATH))
    inserted = prepared.events[copy + 1: copy + 3]
    assert [event[2][0] for event in inserted] == ["chown", "test"]
    assert prepared.events == before.events[: copy + 1] + inserted + before.events[copy + 1:]
