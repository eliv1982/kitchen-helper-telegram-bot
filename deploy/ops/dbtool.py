"""Database operations for a House Helper container: the checks, the backup and the
restore that deploy/RUNBOOK.md is built from, and the lock that keeps them apart from the
web service.

    python /opt/house-helper/ops/dbtool.py <command> ...

  check-config   the database URL is usable for this deployment (read only)
  check-schema   the database exists and is at the Alembic head this image expects
                 (read only)
  serve -- CMD   the web service's start: take the shared lock, check the schema, then
                 REPLACE this process with CMD (uvicorn), which keeps the lock until it ends
  exec --shared|--exclusive -- CMD
                 run any other command that uses the database (a migration, an enrollment)
                 under the lock; the ops service accepts nothing else
  backup         a consistent snapshot of the live database, safe while the web process
                 runs (SQLite's online backup API, not a copy of the file)
  verify         a backup is a sound database with this application's schema (disposable:
                 touches no database, needs no HOUSE_HELPER_DATABASE_URL)
  check-idle     nothing is using the database, so a restore may begin (read only)
  restore        install a verified backup as the database, never over an existing one
                 unless told to, and never alongside the old one's -wal/-shm files

What it is not: it applies no migration, creates no schema, and never reads or prints the
content of a user's data. The tables it reports are counted, not read. The database URL is
read from HOUSE_HELPER_DATABASE_URL, like the application; this tool additionally requires
a SQLite file at an absolute path (a relative one would silently depend on the working
directory of whoever runs it).

Why not `cp`: the database runs in WAL mode (house_helper.persistence.database), so the
committed state is the file plus its -wal file while the application is up. A copy of the
file alone can miss committed transactions or be torn by a concurrent write. The backup
here is taken through a read-only connection with the online backup API, then converted to
a self-contained single file (rollback-journal mode) and checked before it is released.
The same applies in reverse: restoring a file next to a stale -wal/-shm pair makes SQLite
replay the old log onto the new file, so a restore moves the old trio aside as a unit.

The maintenance lock. `<data directory>/.house-helper.lock` is locked with flock(2):
  * the web service holds it SHARED for as long as uvicorn runs (`serve` takes it and then
    exec()s uvicorn, which inherits the descriptor; the kernel drops it when the process
    ends, so it is never stale);
  * a command that uses the database as the application does (an enrollment) holds it
    SHARED too, so it runs beside the web service, as it always could;
  * `backup` holds it SHARED: it only reads, through SQLite;
  * a migration and `restore` hold it EXCLUSIVE: they refuse while the web service or any
    other command is running, and nothing can start while they run.
The lock is cooperative. It covers everything started through this tool, which is how the
ops service runs and the only way the runbook uses. It cannot stop a process that never asks
for it (a `docker run` with the volume and another entrypoint, the sqlite3 shell, a process of
the host), and cooperative locking does not protect against an arbitrary privileged process
that deliberately ignores it. For those `restore` adds a check inside SQLite itself: it opens
the old database, takes SQLite's write lock (refused if another connection holds it, which is
the writer that is mid-transaction), and, after closing, looks for the -wal/-shm files that
SQLite deletes when the last connection goes (still there: another connection is open). That
catches a writer or a reader that exists at that moment. What is left depends on the target:
  * an existing database: the check is made as late as possible, right before the old files
    are moved aside, so the remaining race is narrow: a process that ignores the lock and
    opens or writes to the database after the check and before the rename;
  * a target with no database file when the restore began: there is nothing to probe, and the
    target is not looked at again before the restored file is renamed into place, so the race
    with a process that ignores the lock and creates a database there spans the whole staging
    and validation of the input (longer for a larger input). Such a file is replaced unseen.
    If that process is still running, its -wal/-shm files stay beside the restored file and
    SQLite replays them onto it: `restore` reports success, yet what SQLite then reads is not
    the restored database (reproduced on Linux). The check-schema that the runbook runs
    straight after a restore refuses it (exit 4).
Either way the requirement is the one the runbook states: exclusive maintenance access, with
every managed service stopped. The tool enforces it only for what goes through the tool.

Scratch space. Backup, verify and restore stage a full copy of the database. That is on
disk, in the directory HOUSE_HELPER_STAGING_DIR names (the container's /staging, a volume of
its own: not the 64 MiB /tmp, not the data volume), in a private subdirectory this tool
creates and removes. Before it copies anything it checks the free space: the size of the
database (or MAX_DATABASE_BYTES when the size cannot be known, as for a stream) plus 64 MiB.
Restore needs the same again, once, on the data volume, for the file it installs.

Output is one line per result, `<status> [<code>]: <message>`, as house_helper.admin
prints: `ok` on stdout (on stderr when the data itself goes to stdout), `refused` and
`error` on stderr; the exit status says which:

  0  ok
  1  an unexpected failure (the class name is printed, nothing else)
  2  usage
  3  refused: the state of things is not one this command acts on
  4  the database or the backup is unusable (missing, corrupt, wrong schema, not at the head)

A backup streamed to stdout must go to a file or a pipe, never a terminal; with
`docker compose run` that means `-T`. Nothing here logs, and nothing it prints is a secret.
"""

import argparse
import contextlib
import enum
import errno
import hashlib
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Dict, Iterator, List, Mapping, Optional, Sequence

try:
    import fcntl
except ImportError:  # Windows: developers run the tests there; this tool runs in a Linux container
    fcntl = None
    import msvcrt

DATABASE_URL_ENV = "HOUSE_HELPER_DATABASE_URL"
STAGING_ENV = "HOUSE_HELPER_STAGING_DIR"

SQLITE_MAGIC = b"SQLite format 3\x00"

# A backup or restore input larger than this is refused unread: the database of two
# users is megabytes, and a stream that never ends must not fill the disk. Backup, verify
# and restore all keep to it, so a backup this tool writes is one it can restore.
MAX_DATABASE_BYTES = 1 << 30

# Free space asked for on top of a copy: SQLite's own temporary files, a journal, the rest.
STAGING_HEADROOM = 64 << 20

LOCK_FILE_NAME = ".house-helper.lock"

# How long the idle check waits for a writer that holds SQLite's write lock.
_PROBE_BUSY_SECONDS = 3

_CHUNK = 1 << 20
_REVISION_TABLE = "alembic_version"
_SIDECARS = ("-wal", "-shm", "-journal")


class ExitCode(enum.IntEnum):
    OK = 0
    INTERNAL = 1
    USAGE = 2
    REFUSED = 3
    DATABASE = 4


INTERRUPTED = 130


class ToolError(Exception):
    """A result that is not success. `status` is "refused" or "error"; the message is
    safe to print."""

    def __init__(self, status: str, code: str, message: str, exit_code: ExitCode) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.exit_code = exit_code


def _refused(code: str, message: str) -> ToolError:
    return ToolError("refused", code, message, ExitCode.REFUSED)


def _unusable(code: str, message: str) -> ToolError:
    return ToolError("error", code, message, ExitCode.DATABASE)


def _say(stream, status: str, code: str, message: str) -> None:
    print(f"{status} [{code}]: {message}", file=stream)


def _mib(value: int) -> str:
    return f"{value / (1 << 20):.0f} MiB"


# --- the configured database --------------------------------------------------------------


def database_path(environ: Mapping[str, str]) -> Path:
    """The SQLite file HOUSE_HELPER_DATABASE_URL names, or ToolError. The URL itself is
    never part of a message."""
    raw = (environ.get(DATABASE_URL_ENV) or "").strip()
    if not raw:
        raise _unusable(
            "not_configured",
            f"{DATABASE_URL_ENV} is not set. This tool never falls back to a default: the "
            "container's compose file sets it.",
        )
    from sqlalchemy.engine import make_url

    try:
        url = make_url(raw)
    except Exception:  # the parser's message can quote what it was given
        raise _unusable(
            "invalid_url", f"{DATABASE_URL_ENV} is not a valid database URL (its value is not shown)."
        ) from None
    if url.get_backend_name() != "sqlite":
        raise _refused("unsupported_backend", "this tool works on SQLite databases only.")
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        raise _refused(
            "sqlite_not_a_file", f"{DATABASE_URL_ENV} must name a plain SQLite file path."
        )
    path = Path(database)
    if not path.is_absolute():
        raise _refused(
            "path_not_absolute",
            "the SQLite path must be absolute (sqlite:////data/house_helper.db), not relative "
            "to a working directory.",
        )
    return path


def _has_sqlite_header(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def _check_directory(path: Path) -> None:
    directory = path.parent
    if not directory.is_dir():
        raise _refused("data_dir_missing", f"the directory {directory} does not exist.")
    if not os.access(directory, os.W_OK | os.X_OK):
        raise _refused(
            "data_dir_not_writable",
            f"the directory {directory} is not writable by this user (uid "
            f"{getattr(os, 'getuid', lambda: 'n/a')()}): SQLite creates its -wal and -shm "
            "files next to the database, so the directory, not only the file, must be.",
        )


def check_config(environ: Mapping[str, str]) -> str:
    """The message describing a usable configuration, or ToolError."""
    path = database_path(environ)
    _check_directory(path)
    if path.exists() and path.stat().st_size > 0:
        if not _has_sqlite_header(path):
            raise _unusable("not_a_database", f"{path} exists but is not a SQLite database.")
        state = f"present ({path.stat().st_size} bytes)"
    else:
        state = "absent or empty (the first migration creates it)"
    return f"database file {path}: {state}; its directory is writable."


def check_schema(environ: Mapping[str, str]) -> str:
    """The message saying the schema is at the head, or ToolError. Reads only."""
    database_path(environ)  # the rules of this tool first: absolute SQLite path
    from house_helper.admin.target import TargetError, open_target, repository_head

    try:
        engine = open_target(environ)
    except TargetError as error:
        raise _unusable(error.code, f"{error.message} {_HINTS.get(error.code, '')}".strip()) from None
    engine.dispose()
    return f"the database schema is at revision {repository_head()}, which this image expects."


# What the operator does about a database that is not at this image's head. The text of the
# error itself (house_helper.admin.target) is written for the README's single-machine setup.
_HINTS = {
    "database_missing": "In this deployment: the first install creates and migrates it with the "
    "house-helper-ops service (deploy/RUNBOOK.md, First install).",
    "schema_not_head": "In this deployment: a database BEHIND this image is migrated with the "
    "house-helper-ops service after a backup (deploy/RUNBOOK.md, Upgrade); a database AHEAD of "
    "it needs the image that matches it, or a backup taken with this image's version "
    "(deploy/RUNBOOK.md, Rollback). Never migrate down to fit the image.",
}


# --- the maintenance lock -------------------------------------------------------------------

_BUSY_ERRNOS = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}
if hasattr(errno, "EDEADLOCK"):
    _BUSY_ERRNOS.add(errno.EDEADLOCK)


def _try_lock(descriptor: int, exclusive: bool) -> bool:
    """True if the lock was taken, False if another holder prevents it."""
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        else:
            # msvcrt has one kind of lock: here a shared request is an exclusive one, so two
            # commands never overlap on Windows. Nothing is deployed there.
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in _BUSY_ERRNOS:
            return False
        raise
    return True


class DataDirLock:
    """The lock on the data directory (see the module docstring). Taken without waiting: a
    holder that conflicts is a refusal now, not a command that hangs for a restore."""

    def __init__(self, database: Path, *, exclusive: bool, purpose: str) -> None:
        self.path = database.with_name(LOCK_FILE_NAME)
        self.exclusive = exclusive
        self.purpose = purpose
        self.descriptor: Optional[int] = None

    def __enter__(self) -> "DataDirLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()

    def acquire(self) -> None:
        for _ in range(5):
            try:
                descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError:
                raise _refused(
                    "data_dir_not_writable",
                    f"cannot open the lock file {self.path}: the data directory must exist and be "
                    "writable by this user.",
                ) from None
            if not _try_lock(descriptor, self.exclusive):
                os.close(descriptor)
                raise self._busy()
            if fcntl is not None:
                # Someone may have deleted the file between our open and our lock; then we hold
                # a lock on a file nobody else will find. Look again.
                try:
                    same = os.fstat(descriptor).st_ino == os.stat(self.path).st_ino
                except OSError:
                    same = False
                if not same:
                    os.close(descriptor)
                    continue
            self.descriptor = descriptor
            return
        raise _refused("maintenance_lock_unstable", f"the lock file {self.path} keeps being replaced.")

    def release(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def keep_across_exec(self) -> None:
        """Let a program started by exec() inherit the lock: it then holds it until it ends."""
        assert self.descriptor is not None
        os.set_inheritable(self.descriptor, True)

    def _busy(self) -> ToolError:
        if self.exclusive:
            return _refused(
                "maintenance_lock_busy",
                f"{self.purpose} needs the data directory to itself, but the web service or another "
                "database command is running (it holds the lock "
                f"{LOCK_FILE_NAME}). Stop the web service (docker compose stop house-helper), "
                "confirm it is stopped, wait for any other command to finish, then try again.",
            )
        return _refused(
            "maintenance_lock_busy",
            f"{self.purpose} cannot start: a restore or a migration holds the data directory "
            f"exclusively (the lock {LOCK_FILE_NAME}). Wait for it to finish.",
        )


def _wal_mode(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return len(header) == 20 and header.startswith(SQLITE_MAGIC) and header[18:20] == b"\x02\x02"


def _require_idle(target: Path) -> None:
    """ToolError unless no other process is using the SQLite database at `target`.

    Two observations, both made by SQLite itself (see the module docstring for what they
    cannot see): the write lock can be taken, and, once this connection is closed, no
    -wal/-shm file remains (SQLite deletes them when the last connection closes). Opening a
    database that crashed recovers its log into the file first; nothing is lost by looking.
    A file that is not a database is not looked at: nothing live can be using it, and
    opening it would let SQLite delete the log files beside it, which belong with it in the
    directory the restore keeps.
    """
    if not target.is_file() or target.stat().st_size == 0 or not _has_sqlite_header(target):
        return
    was_wal = _wal_mode(target)
    try:
        connection = sqlite3.connect(str(target), timeout=_PROBE_BUSY_SECONDS, isolation_level=None)
    except sqlite3.Error:
        return
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                raise _refused(
                    "database_busy",
                    "another process holds SQLite's write lock on the database: something is "
                    "writing to it right now (an uncommitted transaction). Stop it first.",
                ) from None
            return  # damaged or unreadable: the restore exists for that case; the lock is the guard
        except sqlite3.DatabaseError:
            return  # not a database, or damaged: nothing live can be using it
    finally:
        connection.close()
    if was_wal and any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise _refused(
            "database_in_use",
            "another process still has the database open (SQLite left its -wal/-shm files in "
            "place when this check closed its own connection). Stop whatever uses it first.",
        )


# --- scratch space ----------------------------------------------------------------------------


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def staging_root(environ: Mapping[str, str], keep_away_from: Optional[Path] = None) -> Path:
    """The directory scratch copies go in: HOUSE_HELPER_STAGING_DIR, or the system's
    temporary directory when it is not set (development). ToolError if it is not a usable,
    separate directory."""
    raw = (environ.get(STAGING_ENV) or "").strip()
    root = Path(raw) if raw else Path(tempfile.gettempdir())
    if raw and not root.is_absolute():
        raise _refused("staging_unusable", f"{STAGING_ENV} must be an absolute path.")
    if root.is_symlink() or not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise _refused(
            "staging_unusable",
            f"the staging directory {root} is missing or not writable by this user. In the "
            "container it is a volume of its own mounted at /staging: run this command through "
            "the ops service (or `docker run -v /staging ...`), which provides it.",
        )
    if keep_away_from is not None:
        live, scratch = os.path.realpath(keep_away_from), os.path.realpath(root)
        if scratch == live or scratch.startswith(live + os.sep):
            raise _refused(
                "staging_unusable",
                "the staging directory must be separate from the data directory: scratch copies "
                "must not share it with the live database.",
            )
    return root


@contextlib.contextmanager
def staging_area(
    environ: Mapping[str, str], needed: int, purpose: str, keep_away_from: Optional[Path] = None
) -> Iterator[Path]:
    """A private (0700) scratch directory on disk with room for `needed` bytes plus the
    headroom, removed afterwards whatever happens. SQLite's own temporary files go there too."""
    root = staging_root(environ, keep_away_from)
    wanted = needed + STAGING_HEADROOM
    free = _free_bytes(root)
    if free < wanted:
        raise _refused(
            "staging_space",
            f"not enough scratch space: {root} has {_mib(free)} free, and this {purpose} needs "
            f"{_mib(wanted)} (a copy of up to {_mib(needed)} plus {_mib(STAGING_HEADROOM)}). "
            "Nothing was changed. Free the space; the limit on a database is "
            f"{_mib(MAX_DATABASE_BYTES)}.",
        )
    session = Path(tempfile.mkdtemp(prefix=f"hh-{purpose}-", dir=root))  # mode 0700
    if os.path.realpath(session.parent) != os.path.realpath(root):  # cannot happen; checked anyway
        shutil.rmtree(session, ignore_errors=True)
        raise _refused("staging_unusable", "the scratch directory is not inside the staging directory.")
    previous = os.environ.get("SQLITE_TMPDIR")
    os.environ["SQLITE_TMPDIR"] = str(session)
    try:
        yield session
    finally:
        if previous is None:
            os.environ.pop("SQLITE_TMPDIR", None)
        else:
            os.environ["SQLITE_TMPDIR"] = previous
        shutil.rmtree(session, ignore_errors=True)


def _disk_full(exc: BaseException) -> bool:
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", None)):
        return True
    return isinstance(exc, sqlite3.Error) and "full" in str(exc).lower()


def _staging_full() -> ToolError:
    return _refused(
        "staging_full",
        "the disk filled up while a copy was being written. Nothing was changed; free space "
        "(see `staging_space` for how much an operation needs) and try again.",
    )


# --- inspecting a database file -----------------------------------------------------------


@dataclass(frozen=True)
class Inspection:
    integrity_problems: tuple
    foreign_key_violations: int
    revisions: tuple
    tables: tuple  # ((name, row count), ...), sorted by name

    @property
    def sound(self) -> bool:
        return not self.integrity_problems and self.foreign_key_violations == 0


def inspect_database(path: Path) -> Inspection:
    """Integrity, foreign keys, schema revision and row counts of the SQLite file at
    `path`. Opens it read-write (the file is a scratch copy, or a snapshot being
    checked); nothing is written. Problems are returned, not raised."""
    problems: list = []
    violations = 0
    revisions: list = []
    tables: list = []
    connection = None
    try:
        connection = sqlite3.connect(str(path), timeout=30)
        problems = [str(row[0])[:200] for row in connection.execute("PRAGMA integrity_check")]
        if problems == ["ok"]:
            problems = []
            violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            names = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            for name in names:
                quoted = '"' + name.replace('"', '""') + '"'
                count = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                tables.append((name, count))
                if name == _REVISION_TABLE:
                    revisions = [
                        str(row[0]) for row in connection.execute(f"SELECT version_num FROM {quoted}")
                    ]
    except sqlite3.Error as exc:
        if _disk_full(exc):
            raise _staging_full() from None
        problems = [f"{type(exc).__name__}: {str(exc)[:150]}"]
    finally:
        if connection is not None:
            connection.close()
    return Inspection(tuple(problems[:5]), violations, tuple(revisions), tuple(tables))


def revision_relation(revision: str) -> tuple:
    """(relation, head): "head", "behind" (an older revision of this repository's
    single history) or "unknown" (not in it: a newer image's, or not this application's)."""
    from alembic.script import ScriptDirectory

    from house_helper.persistence.database import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    heads = script.get_heads()
    if len(heads) != 1:
        raise _unusable("repository_not_linear", "this image's migrations have more than one head.")
    head = heads[0]
    if revision == head:
        return "head", head
    known = {step.revision for step in script.walk_revisions()}
    return ("behind" if revision in known else "unknown"), head


# --- the schema this image's migrations produce ---------------------------------------------

_REVISION_ID = re.compile(r"[A-Za-z0-9_]{1,64}")


def _plain(value: Optional[str]) -> str:
    """A value from the database, printed only if it is a plain identifier."""
    return value if value is not None and _REVISION_ID.fullmatch(value) else "(unrecognised)"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def schema_fingerprint(path: Path) -> dict:
    """The structure of the SQLite file at `path`, read from SQLite's own catalogue: its tables
    with their columns (name, declared type, NOT NULL, primary-key position, from PRAGMA
    table_info), foreign keys and indexes, and the type and name of any trigger or view.

    Not compared, on purpose; this is a choice, not a limit of SQLite:
      * column defaults. SQLite exposes them (the dflt_value column of PRAGMA table_info and
        table_xinfo) but the comparison does not look at them;
      * CHECK constraints. SQLite has no pragma that lists them: they exist only inside the
        table's CREATE TABLE text, which sqlite_schema (also called sqlite_master) returns
        in its `sql` column, along with all the other DDL. Comparing that text exactly is
        fragile (one schema can be spelled in more than one way, for example after an ALTER
        TABLE), and comparing it by meaning would need a SQL parser; this kit has neither;
      * the text of triggers and views, and of any DDL as a whole;
      * generated or hidden columns: table_info omits them (table_xinfo lists them). The
        application's migrations create none.
    A database that differs from the reference only in those passes."""
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        tables: Dict[str, dict] = {}
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for name in names:
            columns = sorted(
                (row[1], (row[2] or "").upper(), int(row[3]), int(row[5]))
                for row in connection.execute(f"PRAGMA table_info({_quote(name)})")
            )
            groups: Dict[int, list] = {}
            for row in connection.execute(f"PRAGMA foreign_key_list({_quote(name)})"):
                groups.setdefault(row[0], []).append(row)
            foreign_keys = sorted(
                (
                    tuple(r[3] for r in sorted(rows, key=lambda r: r[1])),
                    rows[0][2],
                    tuple(str(r[4]) for r in sorted(rows, key=lambda r: r[1])),
                    rows[0][5],
                    rows[0][6],
                )
                for rows in groups.values()
            )
            indexes = []
            for _seq, index_name, unique, origin, partial in connection.execute(
                f"PRAGMA index_list({_quote(name)})"
            ):
                members = tuple(
                    str(row[2]) for row in connection.execute(f"PRAGMA index_info({_quote(index_name)})")
                )
                # An explicit CREATE INDEX has a name that matters; the automatic index behind a
                # PRIMARY KEY or UNIQUE constraint is named by SQLite and only its columns count.
                indexes.append((int(unique), members, index_name if origin == "c" else "", int(partial)))
            tables[name] = {"columns": columns, "foreign_keys": foreign_keys, "indexes": sorted(indexes)}
        others = sorted(
            (row[0], row[1])
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view')"
            )
        )
        return {"tables": tables, "others": others}
    finally:
        connection.close()


_REFERENCES: Dict[str, dict] = {}


def reference_fingerprint(revision: str, scratch: Path) -> dict:
    """The schema this image's own migrations produce at `revision`: a database built from
    them in `scratch` and read back. It is the authority; nothing here repeats the schema."""
    if revision not in _REFERENCES:
        from house_helper.persistence.database import create_db_engine, upgrade_database

        path = scratch / f"reference-{revision}.db"
        engine = create_db_engine(f"sqlite:///{path}")
        try:
            upgrade_database(engine, revision)
        finally:
            engine.dispose()
        _REFERENCES[revision] = schema_fingerprint(path)
        for leftover in scratch.glob(f"reference-{revision}.db*"):
            leftover.unlink()
    return _REFERENCES[revision]


def schema_problems(actual: dict, expected: dict) -> List[str]:
    """What differs, as short sentences. Names that come from the inspected database are
    printed only if they are plain identifiers."""
    problems: List[str] = []
    for name in sorted(set(expected["tables"]) - set(actual["tables"])):
        problems.append(f"table {name} is missing")
    for name in sorted(set(actual["tables"]) - set(expected["tables"])):
        problems.append(f"unexpected table {_plain(name)}")
    for name in sorted(set(expected["tables"]) & set(actual["tables"])):
        have, want = actual["tables"][name], expected["tables"][name]
        have_columns = {column[0]: column for column in have["columns"]}
        want_columns = {column[0]: column for column in want["columns"]}
        for column in sorted(set(want_columns) - set(have_columns)):
            problems.append(f"{name}.{column} is missing")
        for column in sorted(set(have_columns) - set(want_columns)):
            problems.append(f"unexpected column {name}.{_plain(column)}")
        for column in sorted(set(want_columns) & set(have_columns)):
            if want_columns[column] != have_columns[column]:
                problems.append(f"{name}.{column} differs in type, NOT NULL or primary key")
        if have["foreign_keys"] != want["foreign_keys"]:
            problems.append(f"the foreign keys of {name} differ")
        if have["indexes"] != want["indexes"]:
            problems.append(f"the indexes of {name} differ")
    if actual["others"] != expected["others"]:
        problems.append("triggers or views differ")
    return problems


@dataclass(frozen=True)
class Verdict:
    """What a database file is. Whether it is a sound SQLite database with this application's
    schema is decided by assess() (which raises if not); what its schema revision means to
    this image is reported here and acted on by require_usable()."""

    inspection: Inspection
    revision: Optional[str]  # None: no, or more than one, Alembic revision
    relation: str  # head, behind, unknown, or none
    head: str

    def describe(self) -> str:
        if self.relation == "head":
            where = f"schema revision {self.revision} (the head of this image)"
        elif self.relation == "behind":
            where = (
                f"schema revision {self.revision} (behind this image's head {self.head}: it "
                "needs `alembic upgrade head` before this image's application can use it)"
            )
        elif self.relation == "unknown":
            where = f"schema revision {_plain(self.revision)} (not in this image's history)"
        else:
            where = "no single Alembic schema revision"
        counts = ", ".join(f"{name}={count}" for name, count in self.inspection.tables)
        structure = "schema structure ok; " if self.relation in ("head", "behind") else ""
        return f"integrity ok; foreign keys ok; {structure}{where}; rows per table: {counts}."


def assess(path: Path, scratch: Path) -> Verdict:
    """The SQLite file at `path`, or ToolError if it is not a sound SQLite database with
    the schema this image's migrations produce for its revision (header, integrity,
    foreign keys, tables, columns, keys, indexes). A revision this image does not know
    cannot be checked and is reported, not judged: see require_usable()."""
    if not _has_sqlite_header(path):
        raise _unusable("not_a_database", "the input is not a SQLite database file.")
    inspection = inspect_database(path)
    if inspection.integrity_problems:
        raise _unusable(
            "integrity_failed",
            "SQLite's integrity check failed: " + "; ".join(inspection.integrity_problems),
        )
    if inspection.foreign_key_violations:
        raise _unusable(
            "foreign_key_violations",
            f"{inspection.foreign_key_violations} foreign-key violation(s) in the database.",
        )
    if len(inspection.revisions) != 1:
        return Verdict(inspection, None, "none", revision_relation("")[1])
    revision = inspection.revisions[0]
    relation, head = revision_relation(revision)
    if relation in ("head", "behind"):
        problems = schema_problems(schema_fingerprint(path), reference_fingerprint(revision, scratch))
        if problems:
            shown = "; ".join(problems[:5]) + (f"; and {len(problems) - 5} more" if len(problems) > 5 else "")
            raise _refused(
                "schema_mismatch",
                f"the database advertises revision {revision} but does not have this application's "
                f"schema for it ({shown}). It is incomplete, foreign or altered.",
            )
    return Verdict(inspection, revision, relation, head)


def require_usable(verdict: Verdict, *, require_head: bool = False) -> None:
    """ToolError unless this image can use the database `verdict` describes."""
    if verdict.relation == "none":
        raise _refused(
            "no_schema_version",
            "the database does not carry exactly one Alembic revision: it was not made by this "
            "application's migrations.",
        )
    if verdict.relation == "unknown":
        raise _refused(
            "revision_unknown",
            f"the schema revision {_plain(verdict.revision)} is not in this image's migration "
            "history: use the image of the application version that wrote it.",
        )
    if verdict.relation == "behind" and require_head:
        raise _refused(
            "revision_behind",
            f"the schema revision {verdict.revision} is behind this image's head {verdict.head}.",
        )


# --- moving bytes -------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_limited(source: BinaryIO, destination: Path) -> int:
    total = 0
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as out:
            for chunk in iter(lambda: source.read(_CHUNK), b""):
                total += len(chunk)
                if total > MAX_DATABASE_BYTES:
                    raise _refused("input_too_large", "the input is larger than this tool accepts.")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except OSError as exc:
        if _disk_full(exc):
            raise _staging_full() from None
        raise
    return total


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):  # Windows: not available, not needed
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --- backup -------------------------------------------------------------------------------


def snapshot_database(source: Path, destination: Path) -> None:
    """Copy the live database at `source` to a self-contained file at `destination`
    through SQLite's online backup, from a read-only connection."""
    reader = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        writer = sqlite3.connect(str(destination))
        try:
            reader.backup(writer)
            # The snapshot is one file that needs no -wal beside it.
            writer.execute("PRAGMA journal_mode=DELETE").fetchall()
        finally:
            writer.close()
    finally:
        reader.close()


def _live_bytes(source: Path) -> int:
    """The most a snapshot of `source` can take: the file and what its log may still add."""
    total = source.stat().st_size
    log = Path(str(source) + "-wal")
    if log.exists():
        total += log.stat().st_size
    return total


def backup(environ: Mapping[str, str], output: str, stdout: BinaryIO) -> str:
    source = database_path(environ)
    if not source.is_file() or source.stat().st_size == 0:
        raise _unusable("database_missing", f"there is no database at {source} to back up.")
    to_stream = output == "-"
    if to_stream and hasattr(stdout, "isatty") and stdout.isatty():
        raise ToolError(
            "refused",
            "stdout_is_a_terminal",
            "the backup is binary data: redirect stdout to a file (and use `docker compose run "
            "-T`, which does not allocate a terminal).",
            ExitCode.USAGE,
        )
    target: Optional[Path] = None if to_stream else Path(output)
    if target is not None and (target.exists() or not target.parent.is_dir()):
        raise _refused(
            "output_unusable",
            "the output file exists or its directory does not; a backup is never overwritten.",
        )
    live = _live_bytes(source)
    if live > MAX_DATABASE_BYTES:
        raise _refused(
            "database_too_large",
            f"the database ({_mib(live)} with its log) is larger than the {_mib(MAX_DATABASE_BYTES)} "
            "this tool backs up, verifies and restores. A backup it could not restore is not made.",
        )
    # Shared: the web service and the enrollment commands run beside a backup; a migration
    # or a restore does not.
    with DataDirLock(source, exclusive=False, purpose="a backup"), staging_area(
        environ, live, "backup", keep_away_from=source.parent
    ) as scratch:
        snapshot = scratch / "snapshot.db"
        try:
            snapshot_database(source, snapshot)
        except sqlite3.Error as exc:
            if _disk_full(exc):
                raise _staging_full() from None
            raise _unusable("backup_failed", f"the online backup failed ({type(exc).__name__}).") from None
        except OSError as exc:
            if _disk_full(exc):
                raise _staging_full() from None
            raise
        size = snapshot.stat().st_size
        if size > MAX_DATABASE_BYTES:
            raise _refused(
                "database_too_large",
                f"the snapshot is {_mib(size)}, larger than the {_mib(MAX_DATABASE_BYTES)} this "
                "tool restores. No backup was released.",
            )
        # A snapshot that is not sound, or not this application's schema, is not released.
        verdict = assess(snapshot, scratch)
        require_usable(verdict)
        digest = _sha256(snapshot)
        if target is None:
            with open(snapshot, "rb") as handle:
                shutil.copyfileobj(handle, stdout, _CHUNK)
            stdout.flush()
        else:
            # A named output is a second full copy, on the destination's filesystem.
            free = _free_bytes(target.parent)
            if free < size + STAGING_HEADROOM:
                raise _refused(
                    "output_space",
                    f"the filesystem of {target.parent} has {_mib(free)} free; the backup needs "
                    f"{_mib(size)} plus {_mib(STAGING_HEADROOM)}. No backup was written.",
                )
            _write_new_file(snapshot, target)
    where = "to stdout" if target is None else f"to {target}"
    return (
        f"backup written {where}: {size} bytes, sha256 {digest}; "
        f"{verdict.describe()} Keep the checksum with the file."
    )


def _write_new_file(snapshot: Path, target: Path) -> None:
    """Write `snapshot` to `target`, which must not exist: the content goes to a private
    temporary name first and is linked into place, which fails rather than replaces."""
    part = target.with_name(target.name + ".part")
    try:
        descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise _refused(
            "output_unusable",
            f"{part.name} exists: an earlier backup was interrupted, or another is running. "
            "It is left alone; remove it yourself once you know which.",
        ) from None
    try:
        with os.fdopen(descriptor, "wb") as out, open(snapshot, "rb") as handle:
            shutil.copyfileobj(handle, out, _CHUNK)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(part, target)
        except FileExistsError:
            raise _refused(
                "output_unusable", "the output file appeared meanwhile; a backup is never overwritten."
            ) from None
    except OSError as exc:
        if _disk_full(exc):
            raise _refused("output_full", "the disk of the output file filled up; no backup was released.") from None
        raise
    finally:
        part.unlink()
    _fsync_directory(target.parent)


# --- verify and restore -------------------------------------------------------------------


def _input_size(source: str, stdin: BinaryIO) -> int:
    """The size to plan scratch space for: the file's, or the largest input accepted when a
    stream's length cannot be known."""
    try:
        if source == "-":
            info = os.fstat(stdin.fileno())
        else:
            info = os.stat(source)
    except (OSError, ValueError, AttributeError):  # includes io.UnsupportedOperation
        if source != "-":
            raise _refused("input_unreadable", "the input file cannot be read.") from None
        return MAX_DATABASE_BYTES
    if not stat.S_ISREG(info.st_mode):
        return MAX_DATABASE_BYTES
    if info.st_size > MAX_DATABASE_BYTES:
        raise _refused("input_too_large", "the input is larger than this tool accepts.")
    return info.st_size


def _stage_input(source: str, stdin: BinaryIO, directory: Path) -> Path:
    staged = directory / "input.db"
    if source == "-":
        _copy_limited(stdin, staged)
    else:
        try:
            with open(source, "rb") as handle:
                _copy_limited(handle, staged)
        except OSError:
            raise _refused("input_unreadable", "the input file cannot be read.") from None
    return staged


def verify(environ: Mapping[str, str], source: str, stdin: BinaryIO, *, require_head: bool) -> str:
    with staging_area(environ, _input_size(source, stdin), "verify") as scratch:
        staged = _stage_input(source, stdin, scratch)
        verdict = assess(staged, scratch)
        require_usable(verdict, require_head=require_head)
    return f"the backup is sound: {verdict.describe()}"


def check_idle(environ: Mapping[str, str]) -> str:
    """Whether a restore may begin: nothing holds the data directory's lock and the database
    is not in use. Changes nothing but what opening a crashed WAL database recovers."""
    target = database_path(environ)
    _check_directory(target)
    with DataDirLock(target, exclusive=True, purpose="the idle check"):
        _require_idle(target)
    return (
        "no web service or other database command holds the data directory, and no other process "
        "has the database open or is writing to it."
    )


def _sidecars(target: Path) -> List[Path]:
    return [target.with_name(target.name + suffix) for suffix in _SIDECARS]


def _fresh_directory(parent: Path, stem: str) -> Path:
    for attempt in range(100):
        candidate = parent / (stem if attempt == 0 else f"{stem}-{attempt + 1}")
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return candidate
    raise _refused("restore_failed", "could not find a free name for the directory of the previous database.")


def restore(
    environ: Mapping[str, str], source: str, stdin: BinaryIO, *, replace: bool
) -> str:
    target = database_path(environ)
    directory = target.parent
    _check_directory(target)  # not check_config: restoring over a corrupt database is the point
    with DataDirLock(target, exclusive=True, purpose="a restore"):
        existing = [path for path in (target, *_sidecars(target)) if path.exists()]
        if existing and not replace:
            raise _refused(
                "database_exists",
                f"a database already exists at {target}. Restoring over it needs --replace, and "
                "the web service must be stopped; the old files are kept, not deleted.",
            )
        with staging_area(
            environ, _input_size(source, stdin), "restore", keep_away_from=directory
        ) as scratch:
            staged = _stage_input(source, stdin, scratch)
            verdict = assess(staged, scratch)
            require_usable(verdict)  # an older revision of this image's history is allowed
            size = staged.stat().st_size
            free = _free_bytes(directory)
            if free < size + STAGING_HEADROOM:
                raise _refused(
                    "data_dir_space",
                    f"the data volume has {_mib(free)} free; the restored file needs {_mib(size)} "
                    f"plus {_mib(STAGING_HEADROOM)}. Nothing was changed.",
                )
            # Into the data directory under a private name nothing opens, so the last step is
            # a rename on one filesystem.
            descriptor, name = tempfile.mkstemp(dir=directory, prefix=".restore-", suffix=".part")
            part = Path(name)
            kept: Optional[Path] = None
            try:
                try:
                    with os.fdopen(descriptor, "wb") as out, open(staged, "rb") as handle:
                        shutil.copyfileobj(handle, out, _CHUNK)
                        out.flush()
                        os.fsync(out.fileno())  # through the write handle: a read-only one fails on Windows
                except OSError as exc:
                    if _disk_full(exc):
                        raise _refused("data_dir_space", "the data volume filled up. Nothing was changed.") from None
                    raise
                if existing:
                    # The last look, as late as possible: the old database must not be written
                    # to or open anywhere. (A crashed log is recovered into the file by this
                    # look, so what is set aside below is complete.)
                    _require_idle(target)
                    existing = [path for path in (target, *_sidecars(target)) if path.exists()]
                kept = _install(part, target, existing, directory)
            finally:
                if part.exists():
                    part.unlink()
    _fsync_directory(directory)
    note = f" The previous database files were moved to {kept}." if kept else ""
    return f"restored the database at {target}.{note} {verdict.describe()}"


def _install(part: Path, target: Path, existing: List[Path], directory: Path) -> Optional[Path]:
    """Move the old database and its log files aside as a unit, then put `part` in place.
    If anything fails, the old files go back where they were. Returns the directory they
    were moved to (None if there was no old database)."""
    kept: Optional[Path] = None
    moved: List[tuple] = []
    try:
        if existing:
            # The old database and its -wal/-shm move as a unit: a new file left next to
            # the old log would have that log replayed onto it.
            kept = _fresh_directory(directory, f"pre-restore-{_timestamp()}")
            for path in existing:
                os.replace(path, kept / path.name)
                moved.append((kept / path.name, path))
        os.replace(part, target)
    except BaseException as failure:
        stranded = []
        for moved_to, original in reversed(moved):
            try:
                os.replace(moved_to, original)
            except OSError:
                stranded.append(moved_to)
        if kept is not None and not stranded:
            with contextlib.suppress(OSError):
                kept.rmdir()
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise
        if stranded:
            raise ToolError(
                "error",
                "restore_failed",
                f"the restore failed and the previous database could not be put back. Its files "
                f"are in {kept}: move them back to {directory} (all of them together) before "
                "starting the web service.",
                ExitCode.DATABASE,
            ) from None
        raise ToolError(
            "error",
            "restore_failed",
            f"the restore failed ({type(failure).__name__}); the previous database is back in "
            "place and was not changed.",
            ExitCode.DATABASE,
        ) from None
    return kept


# --- serve and exec: starting programs under the lock -------------------------------------------


def _exec(command: Sequence[str]) -> None:
    """Replace this process by `command`. A hook, so that tests can look instead of leaving."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(command[0], list(command))


def _run_under(lock: DataDirLock, command: Sequence[str]) -> None:
    lock.keep_across_exec()
    try:
        _exec(command)
    except OSError:
        raise _refused(
            "command_not_runnable", f"cannot run {command[0]!r}: not found or not executable."
        ) from None


def serve(environ: Mapping[str, str], command: Sequence[str]) -> None:
    """The web service's start. Takes the shared lock first, so that a restore cannot begin
    (and a running restore keeps this out), checks the schema, then becomes `command`, which
    holds the lock until it ends. Returns only by raising."""
    target = database_path(environ)
    lock = DataDirLock(target, exclusive=False, purpose="the web service")
    lock.acquire()
    try:
        message = check_schema(environ)
        _say(sys.stdout, "ok", "check_schema", message)
        _run_under(lock, command)
    finally:
        lock.release()  # reached only if exec() did not replace this process


def exec_locked(environ: Mapping[str, str], command: Sequence[str], *, exclusive: bool) -> None:
    """Run `command` (a migration, an enrollment) holding the data directory's lock."""
    target = database_path(environ)
    _check_directory(target)
    purpose = "a migration or other exclusive command" if exclusive else "a database command"
    lock = DataDirLock(target, exclusive=exclusive, purpose=purpose)
    lock.acquire()
    try:
        _run_under(lock, command)
    finally:
        lock.release()  # reached only if exec() did not replace this process


# --- the command line ---------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        _say(sys.stderr, "usage", "arguments", f"{message} (see --help)")
        raise SystemExit(ExitCode.USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="dbtool.py",
        description="Database checks, backup, restore and locking for a House Helper container.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("check-config", help="the database URL and directory are usable")
    commands.add_parser("check-schema", help="the schema is at this image's Alembic head")
    start = commands.add_parser(
        "serve", help="the web service's start: shared lock, schema check, then exec the command"
    )
    start.add_argument("program", nargs=argparse.REMAINDER, help="-- the program to run, e.g. uvicorn ...")
    run = commands.add_parser(
        "exec", help="run a command that uses the database, under the data directory's lock"
    )
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--shared", action="store_true", help="beside the web service (an enrollment)")
    mode.add_argument(
        "--exclusive", action="store_true", help="alone: a migration; refused while the web service runs"
    )
    run.add_argument("program", nargs=argparse.REMAINDER, help="-- the program to run")
    make = commands.add_parser("backup", help="write a consistent snapshot of the database")
    make.add_argument(
        "--output", required=True, help="a new file, or - for stdout (redirect it; never a terminal)"
    )
    check = commands.add_parser("verify", help="check a backup, touching no database")
    check.add_argument("--input", required=True, help="the backup file, or - for stdin")
    check.add_argument(
        "--require-head", action="store_true", help="refuse a backup older than this image's head"
    )
    commands.add_parser("check-idle", help="nothing is using the database: a restore may begin")
    install = commands.add_parser("restore", help="install a backup as the database")
    install.add_argument("--input", required=True, help="the backup file, or - for stdin")
    install.add_argument(
        "--replace",
        action="store_true",
        help="allow replacing an existing database (its files are moved aside, not deleted)",
    )
    return parser


def _program(args: argparse.Namespace, parser_error) -> List[str]:
    program = list(args.program)
    if program[:1] == ["--"]:
        program = program[1:]
    if not program:
        parser_error("a program to run is required after --")
    return program


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    stdin: Optional[BinaryIO] = None,
    stdout: Optional[BinaryIO] = None,
) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        program = _program(args, parser.error) if args.command in ("serve", "exec") else []
    except SystemExit as stop:  # --help (0) or a usage error (2)
        return int(stop.code or 0)
    environ = os.environ if environ is None else environ
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    # The data may go to stdout, so the report then goes to stderr.
    report = sys.stderr if args.command == "backup" and args.output == "-" else sys.stdout
    try:
        if args.command == "check-config":
            message = check_config(environ)
        elif args.command == "check-schema":
            message = check_schema(environ)
        elif args.command == "serve":
            serve(environ, program)
            return int(ExitCode.OK)  # only a hook that returned instead of exec()ing gets here
        elif args.command == "exec":
            exec_locked(environ, program, exclusive=args.exclusive)
            return int(ExitCode.OK)
        elif args.command == "backup":
            message = backup(environ, args.output, stdout)
        elif args.command == "verify":
            message = verify(environ, args.input, stdin, require_head=args.require_head)
        elif args.command == "check-idle":
            message = check_idle(environ)
        else:
            message = restore(environ, args.input, stdin, replace=args.replace)
    except ToolError as error:
        _say(sys.stderr, error.status, error.code, error.message)
        return int(error.exit_code)
    except KeyboardInterrupt:
        _say(sys.stderr, "error", "interrupted", "interrupted.")
        return INTERRUPTED
    except Exception as exc:  # an exception's text can quote paths and SQL: class only
        _say(sys.stderr, "error", "internal", f"unexpected {type(exc).__name__}.")
        return int(ExitCode.INTERNAL)
    _say(report, "ok", args.command.replace("-", "_"), message)
    return int(ExitCode.OK)


if __name__ == "__main__":
    raise SystemExit(main())
