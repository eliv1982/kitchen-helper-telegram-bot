"""Shared helpers of the deployment-kit tests (test_deploy_dbtool.py, test_deploy_safety.py,
test_deploy_scripts.py): the operator tool loaded by path, disposable SQLite databases, and
small child processes that stand in for the things the tool must stay clear of (the web
service, a writer in the middle of a transaction, a process that crashed). Everything
lives under pytest's tmp_path; nothing leaves the machine.
"""

import hashlib
import importlib.util
import io
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

import pytest

from house_helper.persistence.database import create_db_engine, create_session_factory, upgrade_database

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "deploy"
DBTOOL_PATH = DEPLOY / "ops" / "dbtool.py"
URL_ENV = "HOUSE_HELPER_DATABASE_URL"
STAGING_ENV = "HOUSE_HELPER_STAGING_DIR"
HEAD = "0006_oidc_state_invitation"
BEHIND = "0005_identity_invitations"

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX file locks and file modes")


def load_dbtool():
    spec = importlib.util.spec_from_file_location("deploy_dbtool", DBTOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_dbtool"] = module
    spec.loader.exec_module(module)
    return module


dbtool = load_dbtool()
LOCK = dbtool.LOCK_FILE_NAME


# --- databases ------------------------------------------------------------------------------


def make_database(path: Path, revision: str = "head") -> Path:
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        upgrade_database(engine, revision)
    finally:
        engine.dispose()
    return path


def make_big_database(path: Path, mebibytes: int) -> Path:
    """A migrated database of at least `mebibytes` MiB, holding ordinary message rows."""
    from house_helper.persistence.models import Conversation, Message, User

    make_database(path)
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        factory = create_session_factory(engine)
        with factory() as session, session.begin():
            user = User(display_name="ballast")
            conversation = Conversation(user=user)
            session.add_all([user, conversation])
            for index in range(mebibytes):
                session.add(
                    Message(conversation=conversation, role="user", content=chr(97 + index % 26) * (1 << 20))
                )
    finally:
        engine.dispose()
    return path


def env_for(path: Path, **extra: str) -> dict:
    return {URL_ENV: f"sqlite:///{path}", **extra}


def tables(path: Path) -> dict:
    connection = sqlite3.connect(str(path))
    try:
        names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: connection.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        connection.close()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def set_revision(path: Path, revision: str) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("UPDATE alembic_version SET version_num = ?", (revision,))
        connection.commit()
    finally:
        connection.close()


def alter(path: Path, *statements: str) -> None:
    """Run SQL against a database file (to damage its schema on purpose)."""
    connection = sqlite3.connect(str(path))
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def listing(directory: Path) -> list:
    """Names in a data directory, without the lock file the tool leaves there and without
    SQLite's own -wal/-shm files (it creates them when a database is opened and deletes them
    when the last connection closes, so their presence says nothing about what a command did)."""
    return sorted(
        p.name for p in directory.iterdir() if p.name != LOCK and not p.name.endswith(("-wal", "-shm"))
    )


def staging_files(directory: Path) -> list:
    return sorted(p.name for p in directory.iterdir())


# --- running the tool in this process ----------------------------------------------------------


class Run:
    def __init__(self, code: int, data: bytes, out: str, err: str) -> None:
        self.code = code
        self.data = data  # what went to the binary stdout
        self.out = out
        self.err = err


def run(capsys, argv, env, stdin: bytes = b"", stdout=None) -> Run:
    stdout = io.BytesIO() if stdout is None else stdout
    code = dbtool.main(argv, environ=env, stdin=io.BytesIO(stdin), stdout=stdout)
    captured = capsys.readouterr()
    return Run(code, stdout.getvalue(), captured.out, captured.err)


# --- child processes ----------------------------------------------------------------------------

# Each prints READY on its own line once it is in the state the test needs.
STAND_IN_PROGRAM = "import os, time; print('READY', os.getpid(), flush=True); time.sleep(600)"

UNCOMMITTED_WRITER = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=1)
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('BEGIN IMMEDIATE')
connection.execute('CREATE TABLE zz_inflight (x)')
connection.execute("INSERT INTO zz_inflight VALUES ('written mid-transaction')")
print('READY', flush=True)
sys.stdin.readline()
connection.execute('COMMIT')
print('COMMITTED', flush=True)
sys.stdin.readline()
"""

IDLE_CONNECTION = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=1)
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('SELECT count(*) FROM sqlite_master').fetchall()
print('READY', flush=True)
sys.stdin.readline()
"""

CRASHED_WRITER = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute('CREATE TABLE zz_crash (x)')
connection.execute("INSERT INTO zz_crash VALUES ('committed, and only in the log')")
os._exit(0)
"""


def child_environ(database: Optional[Path] = None, **extra: str) -> dict:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("HOUSE_HELPER_")}
    environ["PYTHONPATH"] = str(REPO_ROOT)
    if database is not None:
        environ[URL_ENV] = f"sqlite:///{database}"
    environ.update(extra)
    return environ


class Child:
    """A started child process. wait_ready() returns once it printed READY."""

    def __init__(self, argv: List[str], environ: dict) -> None:
        self.process = subprocess.Popen(
            argv,
            env=environ,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._watchdog = threading.Timer(120, self.process.kill)
        self._watchdog.daemon = True
        self._watchdog.start()
        self.lines: List[str] = []

    def wait_ready(self) -> "Child":
        while True:
            line = self.process.stdout.readline()
            if not line:
                err = self.process.stderr.read()
                raise AssertionError(f"the child ended before READY. Output: {self.lines}\n{err}")
            self.lines.append(line.strip())
            if line.startswith("READY"):
                return self

    def send(self, text: str = "go\n") -> None:
        self.process.stdin.write(text)
        self.process.stdin.flush()

    def read_line(self) -> str:
        return self.process.stdout.readline().strip()

    def stop(self) -> None:
        self._watchdog.cancel()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=30)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()

    def __enter__(self) -> "Child":
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


def start_web_service(database: Path, **extra: str) -> Child:
    """The web service's start-up, as the image runs it: `dbtool serve -- <program>`, the
    program standing in for uvicorn. Ready once the program is running."""
    return Child(
        [sys.executable, str(DBTOOL_PATH), "serve", "--", sys.executable, "-c", STAND_IN_PROGRAM],
        child_environ(database, **extra),
    ).wait_ready()


def start_python(code: str, database: Path) -> Child:
    return Child([sys.executable, "-c", code, str(database)], child_environ(database)).wait_ready()


def run_tool(*argv: str, database: Optional[Path] = None, stdin: Optional[bytes] = None, **extra: str):
    """The tool as a separate process, as the container runs it."""
    return subprocess.run(
        [sys.executable, str(DBTOOL_PATH), *argv],
        env=child_environ(database, **extra),
        input=stdin,
        capture_output=True,
        timeout=180,
    )


def lock_is_taken(database: Path) -> bool:
    """True if somebody holds the data directory's lock, so that an exclusive one cannot be taken."""
    lock = dbtool.DataDirLock(database, exclusive=True, purpose="the test")
    try:
        lock.acquire()
    except dbtool.ToolError:
        return True
    lock.release()
    return False
