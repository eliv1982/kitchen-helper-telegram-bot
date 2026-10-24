"""Shared helpers of the off-host backup tests (test_deploy_offsite*.py, test_deploy_sanitize.py): the
orchestrator loaded by path, synthetic databases with deleted rows, and FakeSystem, a stand-in for the
four external commands the orchestrator runs (bash deploy/backup.sh, docker, age, rclone).

What is real and what is not:
  * dbtool is REAL: the fake `backup.sh` takes its snapshot with `dbtool backup`, and the fake `docker`
    runs `dbtool sanitize` and `dbtool verify` on the bytes it is given. So the rewrite that these tests
    check is the one that ships, on a real SQLite file.
  * age is a reversible fake (a header and an XOR). It proves what the orchestrator hands to age and
    that the plaintext does not reach the remote; it proves nothing about age's cryptography.
  * rclone is a directory on disk standing in for the remote. It accepts ONLY the commands the
    orchestrator is allowed to use and fails the test on any other (delete, purge, sync, cleanup ...)
    or on any path outside the one configured folder. `cat` is the one read of a commit record.
Everything lives under pytest's tmp_path. No credential, no network, no real user data.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tests.deploy_support import DEPLOY, URL_ENV, STAGING_ENV, dbtool, make_database

OFFSITE_PATH = DEPLOY / "offsite_backup.py"


def load_offsite():
    spec = importlib.util.spec_from_file_location("deploy_offsite_backup", OFFSITE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_offsite_backup"] = module
    spec.loader.exec_module(module)
    return module


offsite = load_offsite()

# A public key from age's own documentation. The orchestrator only checks its shape; the fake age
# does not use it.
PUBLIC_KEY = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
PRIVATE_KEY_SHAPED = "AGE-SECRET-KEY-1" + "Q" * 58  # looks like a private key; is not one

TAG = "abc123def456"
FOLDER = "house-helper"
REMOTE = f"fake:{FOLDER}"
FIXED_NOW = datetime(2026, 10, 25, 4, 15, 7, tzinfo=timezone.utc)
FIXED_STAMP = "20261025T041507Z"
FIXED_TOKEN = "0a1b2c3d"
FIXED_NAME = f"house-helper-{FIXED_STAMP}-{FIXED_TOKEN}.tar.age"

KEPT_MARKER = "KEPTROW-MARKER"
DELETED_MARKER = "DELETEDROW-MARKER"


# --- synthetic databases -----------------------------------------------------------------------


def make_database_with_deleted_rows(path: Path, kept: int = 12, deleted: int = 40) -> Path:
    """A migrated database with `kept` messages that stay and `deleted` that were deleted afterwards by a
    plain DELETE, which leaves the bytes in the file. Text is made up."""
    from house_helper.persistence.database import create_db_engine, create_session_factory
    from house_helper.persistence.models import Conversation, Message, User

    make_database(path)
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        factory = create_session_factory(engine)
        with factory() as session, session.begin():
            user = User(display_name="synthetic")
            keep, gone = Conversation(user=user), Conversation(user=user)
            session.add_all([user, keep, gone])
            for index in range(kept):
                session.add(Message(conversation=keep, role="user", content=f"{KEPT_MARKER}-{index:03d} " + "k" * 200))
            for index in range(deleted):
                session.add(Message(conversation=gone, role="user", content=f"{DELETED_MARKER}-{index:03d} " + "d" * 200))
    finally:
        engine.dispose()
    # The deletion, with secure_delete explicitly OFF on its own connection: some SQLite builds (Debian's,
    # Ubuntu's: the production image's) are compiled with it on and zero the bytes themselves, which would
    # leave nothing for these tests to find. A database whose rows were deleted by a build or a time without
    # it is exactly the case the rewrite exists for.
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA secure_delete = OFF")
        connection.execute("DELETE FROM messages WHERE content LIKE ?", (f"{DELETED_MARKER}%",))
        connection.commit()
    finally:
        connection.close()
    return path


def row_counts(path: Path) -> dict:
    connection = sqlite3.connect(str(path))
    try:
        names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: connection.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in sorted(names)}
    finally:
        connection.close()


def database_health(path: Path) -> Tuple[str, int]:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute("PRAGMA integrity_check").fetchone()[0], connection.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        connection.close()


# --- the fake age ------------------------------------------------------------------------------

FAKE_AGE_BODY = b"fake-age-ciphertext\n"


def fake_encrypt(data: bytes) -> bytes:
    return offsite.AGE_HEADER + FAKE_AGE_BODY + bytes(b ^ 0xA5 for b in data)


def fake_decrypt(blob: bytes) -> bytes:
    prefix = offsite.AGE_HEADER + FAKE_AGE_BODY
    assert blob.startswith(prefix), "not the fake age format"
    return bytes(b ^ 0xA5 for b in blob[len(prefix):])


# --- the fake system ---------------------------------------------------------------------------

ALLOWED_RCLONE = {"mkdir", "lsjson", "copyto", "moveto", "deletefile", "backend", "config", "cat"}
WRITING_RCLONE = {"mkdir", "copyto", "moveto", "deletefile"}  # what changes the remote

RCLONE_CONF = '''[fake]
type = yandex
client_id = test-client-id
client_secret = test-client-secret
token = {"access_token":"test"}
app_folder = true
'''


class FakeSystem(offsite.Runner):
    """bash/docker/age/rclone, as far as the orchestrator uses them. `faults` maps (kind, nth) to an
    action; nth 0 means every call of that kind. Kinds: backup, sanitize, verify, age, mkdir, lsjson,
    upload, record (the upload of a commit record), cat (the read of one), download, move, delete.
    Actions: ("exit", code), "garbage" (lsjson, cat), "corrupt" / "truncate" (upload, record, cat,
    download, sanitize), "lie_size" / "lie_md5" (lsjson: of a .partial), "lie_size_final" /
    "lie_md5_final" (lsjson: of a final-named object), "no_hashes" (lsjson: this one listing carries no
    checksum at all), "noop" (move; delete: reports success and deletes nothing), "lost" (delete: deletes
    and reports failure), "signal" (deliver SIGTERM to this process at the start of the call), "bad_pair"
    (backup). An action may also be a function with no arguments: it runs at the start of that call (to
    change the remote between two steps of a run) and the call then behaves normally.
    `hashless` is a set of object names whose listing never carries a checksum."""

    def __init__(self, root: Path, database: Path, remote_account: Optional[Path] = None) -> None:
        self.root = root
        self.database = database
        self.account = remote_account or root / "remote-account"
        self.staging = root / "fake-staging"
        self.staging.mkdir(exist_ok=True)
        self.account.mkdir(exist_ok=True)
        self.missing: set = set()
        self.faults: Dict[Tuple[str, int], object] = {}
        self.counts: Dict[str, int] = {}
        self.calls: List[List[str]] = []
        self.events: List[str] = []
        self.rclone_calls: List[List[str]] = []
        self.docker_calls: List[dict] = []
        self.env_extras: List[dict] = []
        self.age_recipients: List[str] = []
        self.plaintext_paths: List[Path] = []
        self.remote_hashes = True
        self.hashless: set = set()
        self.app_folder_support = True   # False: an rclone like 1.75.2, whose Yandex backend has no app_folder option
        self.can_move = True
        self.hang_at: Optional[Tuple[str, Path]] = None  # (kind, ready file): block there, to be killed by a test

    # helpers for tests -----------------------------------------------------------------------

    def folder(self) -> Path:
        return self.account / FOLDER

    def remote_files(self) -> Dict[str, bytes]:
        folder = self.folder()
        return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()} if folder.is_dir() else {}

    def put_remote(self, name: str, data: bytes = b"an older recovery point") -> None:
        self.folder().mkdir(parents=True, exist_ok=True)
        (self.folder() / name).write_bytes(data)

    def put_point(self, name: str, data: bytes = b"an older recovery point") -> None:
        """A COMMITTED recovery point: the object and the valid commit record the tool would have written."""
        self.put_remote(name, data)
        digests = offsite.Digests(len(data), hashlib.sha256(data).hexdigest(), hashlib.sha1(data).hexdigest(), hashlib.md5(data).hexdigest())
        self.put_remote(name + offsite.COMMIT_SUFFIX, offsite.commit_record(name, digests))

    def point_objects(self) -> List[str]:
        """The final-named objects in the folder, committed or not (no records, no .partial)."""
        return [n for n in self.remote_files() if offsite.parse_point_name(n)]

    def fail(self, kind: str, action: object = ("exit", 1), nth: int = 1) -> "FakeSystem":
        self.faults[(kind, nth)] = action
        return self

    def rclone_commands(self) -> List[str]:
        return [call[0] for call in self.rclone_calls]

    def rclone_writes(self) -> List[str]:
        """The rclone commands that changed (or could change) the remote."""
        return [c for c in self.rclone_commands() if c in WRITING_RCLONE]

    def which(self, name: str) -> Optional[str]:
        return None if name in self.missing else f"/fake/bin/{name}"

    def _hang(self, kind: str) -> None:
        if self.hang_at is not None and self.hang_at[0] == kind:
            self.hang_at[1].write_text("blocked")
            time.sleep(600)  # the test kills this process; nothing after this line runs

    def _fault(self, kind: str):
        if kind != "upload":  # an upload blocks after it has written part of its object
            self._hang(kind)
        self.counts[kind] = self.counts.get(kind, 0) + 1
        action = self.faults.get((kind, self.counts[kind]), self.faults.get((kind, 0)))
        if action == "signal":
            signal.raise_signal(signal.SIGTERM)  # the handler main() installed raises Interrupted here
        if callable(action):
            action()
            return None
        return action

    # the runner ------------------------------------------------------------------------------

    def run(self, argv, *, timeout, stdin_path=None, stdout_path=None, capture=False, env_extra=None):
        self.calls.append([str(a) for a in argv])
        self.env_extras.append(dict(env_extra or {}))
        tool = Path(argv[0]).name
        return getattr(self, "_" + tool)([str(a) for a in argv], stdin_path, stdout_path, dict(env_extra or {}))

    # bash deploy/backup.sh ------------------------------------------------------------------

    def _bash(self, argv, stdin_path, stdout_path, env):
        assert Path(argv[1]).name == "backup.sh", argv
        options = dict(zip(argv[2::2], argv[3::2]))
        assert set(options) == {"--tag", "--dir", "--label"}, options  # never --name, never the default dir
        self.events.append("backup")
        action = self._fault("backup")
        if isinstance(action, tuple) and action[0] == "exit":
            return offsite.Result(action[1], b"", b"error [backup_failed]: injected\n")
        directory = Path(options["--dir"])
        directory.mkdir(mode=0o700, exist_ok=True)
        buffer = io.BytesIO()
        report = io.StringIO()
        with contextlib.redirect_stderr(report):
            code = dbtool.main(
                ["backup", "--output", "-"],
                environ={URL_ENV: f"sqlite:///{self.database}", STAGING_ENV: str(self.staging)},
                stdin=io.BytesIO(),
                stdout=buffer,
            )
        assert code == 0, report.getvalue()
        data = buffer.getvalue()
        name = f"house-helper-20261025T041500Z-feedbeef-{options['--label']}.db"
        path = directory / name
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        line = f"{digest}  {name}\n"
        if action == "bad_pair":
            line = f"{'0' * 64}  {name}\n"
        (directory / (name + ".sha256")).write_bytes(line.encode("ascii"))
        self.plaintext_paths.append(path)
        return offsite.Result(0, f"{path}\n".encode(), f"published {path} ({len(data)} bytes, sha256 {digest}), verified.\n".encode())

    # docker run ... dbtool.py sanitize|verify -----------------------------------------------

    def _docker(self, argv, stdin_path, stdout_path, env):
        assert argv[1] == "run"
        image_index = next(i for i, a in enumerate(argv) if a.startswith("house-helper:"))
        flags = argv[2:image_index]
        assert argv[image_index + 1:image_index + 3] == ["python", offsite.DBTOOL], argv
        args = argv[image_index + 3:]
        kind = args[0]
        self.docker_calls.append({"image": argv[image_index], "flags": flags, "args": args, "env": env})
        self.events.append(kind)
        action = self._fault(kind)
        if isinstance(action, tuple) and action[0] == "exit":
            return offsite.Result(action[1], b"", f"refused [injected]: injected\n".encode())
        assert stdin_path is not None
        out, err = io.BytesIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = dbtool.main(
                args,
                environ={STAGING_ENV: str(self.staging)},  # NO database URL: the container has no volume of the data
                stdin=io.BytesIO(stdin_path.read_bytes()),
                stdout=out,
            )
        data = out.getvalue()
        if stdout_path is not None:
            if action == "corrupt" and data:
                data = data[:100] + bytes([data[100] ^ 0xFF]) + data[101:]
            if action == "truncate":
                data = data[: len(data) // 2]
            with offsite.open_exclusive(stdout_path) as handle:
                handle.write(data)
            self.plaintext_paths.append(stdout_path)
        return offsite.Result(code, b"", err.getvalue().encode())

    # age -R recipients ----------------------------------------------------------------------

    def _age(self, argv, stdin_path, stdout_path, env):
        assert argv[1] == "-R" and len(argv) == 3, argv  # recipients file only: never -p, -i or -r
        self.events.append("age")
        self.age_recipients = Path(argv[2]).read_text().splitlines()
        action = self._fault("age")
        if isinstance(action, tuple) and action[0] == "exit":
            return offsite.Result(action[1], b"", b"age: error: injected\n")
        blob = fake_encrypt(stdin_path.read_bytes())
        if action == "plain":
            blob = stdin_path.read_bytes()
        with offsite.open_exclusive(stdout_path) as handle:
            handle.write(blob)
        return offsite.Result(0)

    # rclone ---------------------------------------------------------------------------------

    def _rclone(self, argv, stdin_path, stdout_path, env):
        assert env == {}, "rclone gets no extra environment"
        command = argv[1]
        assert command in ALLOWED_RCLONE, f"forbidden rclone command: {command}"
        self.rclone_calls.append(argv[1:])
        if command == "config":  # `rclone config providers --config FILE`: offline, lists what this rclone can do
            assert argv[2] == "providers" and argv[3] == "--config" and len(argv) == 5, argv
            action = self._fault("providers")
            if isinstance(action, tuple) and action[0] == "exit":
                return offsite.Result(action[1])
            if action == "garbage":
                return offsite.Result(0, b"not json")
            options = [{"Name": "client_id"}, {"Name": "hard_delete"}] + ([{"Name": "app_folder"}] if self.app_folder_support else [])
            return offsite.Result(0, json.dumps([{"Name": "local", "Options": []}, {"Name": "yandex", "Options": options}]).encode())
        assert argv[2] == "--config" and argv[4:6] == ["--log-level", "ERROR"], argv
        rest = argv[6:]
        flags = [a for a in rest if a.startswith("--")]
        values = {"--retries"}
        operands = []
        skip = False
        for item in rest:
            if skip:
                skip = False
                continue
            if item in values:
                skip = True
                continue
            if item.startswith("--"):
                continue
            operands.append(item)
        for operand in operands:
            if operand.startswith("fake:"):
                read_only_root = command == "backend" and operand == "fake:"  # `backend features` asks about the remote itself
                assert read_only_root or operand == REMOTE or operand.startswith(REMOTE + "/"), f"outside the configured folder: {operand}"
        return getattr(self, "_rclone_" + command)(operands, flags)

    def _rclone_backend(self, operands, flags):
        assert operands == ["features", "fake:"], operands  # read only, and of the remote's root, not of the folder
        action = self._fault("features")
        if isinstance(action, tuple) and action[0] == "exit":
            return offsite.Result(action[1])
        if action == "garbage":
            return offsite.Result(0, b"{}")
        return offsite.Result(0, json.dumps({"Name": "fake", "Features": {"Move": self.can_move, "Copy": True}}).encode())

    def _remote_path(self, operand: str) -> Path:
        return self.account / operand.split(":", 1)[1]

    def _rclone_mkdir(self, operands, flags):
        action = self._fault("mkdir")
        if isinstance(action, tuple) and action[0] == "exit":
            return offsite.Result(action[1])
        self._remote_path(operands[0]).mkdir(parents=True, exist_ok=True)
        return offsite.Result(0)

    def _rclone_lsjson(self, operands, flags):
        action = self._fault("lsjson")
        if isinstance(action, tuple) and action[0] == "exit":
            return offsite.Result(action[1])
        if action == "garbage":
            return offsite.Result(0, b"<html>not json</html>")
        assert flags == ["--files-only", "--hash"], flags
        folder = self._remote_path(operands[0])
        if not folder.is_dir():
            return offsite.Result(3)
        items = []
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                items.append({"Path": path.name, "Name": path.name, "Size": -1, "IsDir": True})
                continue
            data = path.read_bytes()
            size = len(data) + (1 if action == "lie_size" and path.name.endswith(".partial") else 0)
            size += 1 if action == "lie_size_final" and path.name.endswith(".tar.age") else 0
            hashes = {"md5": hashlib.md5(data).hexdigest()} if self.remote_hashes and path.name not in self.hashless else {}
            if action == "no_hashes":
                hashes = {}
            if action == "lie_md5" and hashes and path.name.endswith(".partial"):
                hashes = {"MD5": "0" * 32}
            if action == "lie_md5_final" and hashes and path.name.endswith(".tar.age"):
                hashes = {"MD5": "0" * 32}
            items.append({"Path": path.name, "Name": path.name, "Size": size, "ModTime": "2026-10-25T04:16:00Z", "IsDir": False, "Hashes": hashes})
        return offsite.Result(0, json.dumps(items).encode())

    def _rclone_copyto(self, operands, flags):
        source, destination = operands
        if source.startswith("fake:"):  # a download
            self.events.append("download")
            action = self._fault("download")
            if isinstance(action, tuple) and action[0] == "exit":
                return offsite.Result(action[1])
            data = self._remote_path(source).read_bytes()
            if action == "corrupt":
                data = data[:50] + bytes([data[50] ^ 0xFF]) + data[51:]
            with offsite.open_exclusive(Path(destination)) as handle:
                handle.write(data)
            return offsite.Result(0)
        kind = "record" if destination.endswith(offsite.COMMIT_SUFFIX) else "upload"  # an upload
        self.events.append(kind)
        assert "--immutable" in flags, "an upload must never overwrite"
        assert self.rclone_calls[-1][self.rclone_calls[-1].index("--retries") + 1] == "1", "one attempt of the whole command"
        action = self._fault(kind)
        target = self._remote_path(destination)
        if isinstance(action, tuple) and action[0] == "exit":
            if action[1] == 99:  # fails after writing part of the object
                target.write_bytes(Path(source).read_bytes()[:10])
            return offsite.Result(action[1] if action[1] != 99 else 1)
        if target.exists():
            return offsite.Result(1)
        data = Path(source).read_bytes()
        if action == "corrupt":
            data = data[:50] + bytes([data[50] ^ 0xFF]) + data[51:]
        if action == "truncate":
            data = data[: len(data) // 2]
        if kind == "upload" and self.hang_at is not None and self.hang_at[0] == "upload":
            target.write_bytes(data[:10])  # a transfer in progress: part of the object is there
            self._hang("upload")
        target.write_bytes(data)
        return offsite.Result(0)

    def _rclone_moveto(self, operands, flags):
        self.events.append("move")
        action = self._fault("move")
        if isinstance(action, tuple) and action[0] == "exit":
            return offsite.Result(action[1])
        if action == "noop":
            return offsite.Result(0)
        source, destination = self._remote_path(operands[0]), self._remote_path(operands[1])
        source.replace(destination)
        if action == "corrupt":
            data = destination.read_bytes()
            destination.write_bytes(data[:50] + bytes([data[50] ^ 0xFF]) + data[51:])
        return offsite.Result(0)

    def _rclone_deletefile(self, operands, flags):
        assert flags == ["--yandex-hard-delete"], flags
        self.events.append("delete")
        action = self._fault("delete")
        if isinstance(action, tuple) and action[0] == "exit":
            return offsite.Result(action[1])
        if action == "noop":  # accepts the delete, reports success, keeps the object
            return offsite.Result(0)
        path = self._remote_path(operands[0])
        if not path.is_file():
            return offsite.Result(4)
        path.unlink()
        return offsite.Result(1 if action == "lost" else 0)  # "lost": the object is gone, the answer says it failed

    def _rclone_cat(self, operands, flags):
        assert flags == [] and len(operands) == 1 and operands[0].endswith(offsite.COMMIT_SUFFIX), (operands, flags)  # a commit record only
        self.events.append("cat")
        action = self._fault("cat")
        if isinstance(action, tuple) and action[0] == "exit":
            return offsite.Result(action[1])
        path = self._remote_path(operands[0])
        if not path.is_file():
            return offsite.Result(3)
        data = path.read_bytes()
        if action == "garbage":
            data = b"not a commit record"
        if action == "truncate":
            data = data[: len(data) // 2]
        return offsite.Result(0, data)


# --- running the orchestrator ------------------------------------------------------------------


class Rig:
    """The orchestrator wired to a FakeSystem under tmp_path, with a valid configuration."""

    def __init__(self, tmp_path: Path, *, deleted_rows: bool = True, fresh: bool = True) -> None:
        """`fresh=False` attaches to the directories of a Rig that already exists (a child process of a test)."""
        self.root = tmp_path
        data_dir = tmp_path / "data"  # the staging directory must not be inside the data directory
        self.work = tmp_path / "work"
        self.recipients = tmp_path / "recipients.txt"
        self.config = tmp_path / "rclone.conf"
        if fresh:
            data_dir.mkdir()
            (make_database_with_deleted_rows if deleted_rows else make_database)(data_dir / "live.db")
            self.work.mkdir()
            self.recipients.write_text(f"# the owner's public key{chr(10)}{PUBLIC_KEY}{chr(10)}")
            self.config.write_text(RCLONE_CONF)
            if os.name != "nt":
                self.config.chmod(0o600)
        self.database = data_dir / "live.db"
        self.fake = FakeSystem(tmp_path, self.database)
        self.log: List[str] = []
        self.now = FIXED_NOW
        self.token = FIXED_TOKEN

    def args(self, *extra: str, tag: str = TAG) -> List[str]:
        return [
            "run", f"--tag={tag}", "--recipients-file", str(self.recipients), "--rclone-config", str(self.config),
            "--remote", REMOTE, "--work-dir", str(self.work), *extra,
        ]

    def run(self, *extra: str, argv: Optional[List[str]] = None) -> int:
        return offsite.main(
            argv if argv is not None else self.args(*extra),
            runner=self.fake,
            clock=lambda: self.now,
            token=lambda: self.token,
            deploy_dir=DEPLOY,
            log=self.log.append,
        )

    @property
    def text(self) -> str:
        return "\n".join(self.log)

    def classify(self):
        """What the folder holds by what the tool can prove (offsite.classify_folder), read through a second
        FakeSystem on the same remote, so that the counters and events of the run under test stay as they were."""
        if not self.fake.folder().is_dir():  # nothing was ever uploaded
            return offsite.Folder([], {}, [], [])
        reader = FakeSystem(self.root, self.database, remote_account=self.fake.account)
        reader.remote_hashes, reader.hashless = self.fake.remote_hashes, self.fake.hashless   # the remote lists what it lists
        remote = offsite.Remote(REMOTE, self.config, reader, "/fake/bin/rclone")
        return offsite.classify_folder(remote.listing(), remote.read_record)

    def committed_names(self) -> set:
        return {p.name for p in self.classify().committed}

    def work_leftovers(self) -> List[str]:
        return sorted(p.name for p in self.work.iterdir() if p.name != offsite.LOCK_NAME)

    def decrypted_snapshot(self, name: str, into: Path) -> Path:
        """Decrypt a remote object with the fake age, check the tar and extract the snapshot."""
        into.mkdir(exist_ok=True)
        tar_path = into / "recovery.tar"
        tar_path.write_bytes(fake_decrypt(self.fake.remote_files()[name]))
        out = into / "extracted"
        out.mkdir()
        offsite.read_recovery_tar(tar_path, extract_to=out)
        return out / offsite.SNAPSHOT_NAME


def point_name(stamp: str, token: str = "00000000") -> str:
    return f"house-helper-{stamp}-{token}.tar.age"
