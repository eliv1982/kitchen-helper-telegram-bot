"""Restore rehearsal of an off-host recovery point: fetch the encrypted object, decrypt it with the owner's
PRIVATE age identity, unpack it into a scratch directory, check the archive and the database, say whether the
point is restorable, and remove the scratch directory. Runs on the OWNER'S OWN MACHINE, never on the VPS:

    python3 deploy/offsite_rehearsal.py --rclone-config F --remote NAME:DIR --identity-file F \\
        --work-dir DIR --expect-rows TABLE=N [--expect-rows ...] [--point NAME] [--access-mode M]

This is a separate program from deploy/offsite_backup.py on purpose. That one runs on the VPS, is given public
keys only, and a test pins that it never mentions an identity, a passphrase or a decryption. This one is the only
place the private identity is used, and nothing in deploy/systemd/ refers to it. It reuses the other program's
checks (the remote's folder and access mode, the commit record, the recovery-point tar) by importing it, so
there is one definition of each.

What it does, in this order, and stops at the first that fails:

  1. Refuses, before any command runs, unless: the identity file is a regular file of this user's, readable by
     no one else, holding an age secret key, and NOT inside the repository checkout; this host is not the
     production host (a /etc/house-helper directory exists there: the private identity must never be on it);
     the work directory is an existing absolute directory outside the checkout; the rclone configuration and
     the remote pass exactly the checks of the backup (the same --access-mode, the same fixed folder).
  2. Lists the folder and picks a COMMITTED point: the newest, or the one named by --point. An UNRESOLVED
     object, an object without a valid commit record, or one that is not in the folder is refused: it is not
     a recovery point and this tool does not rehearse it. A `.partial` or `.commit` name is never a point
     either: `--point` refuses one, and a leftover `.partial` in the folder is neither read, reported nor
     touched here (deploy/offsite_backup.py never deletes one and reports it; an operator removes it, RUNBOOK
     15.11), and it changes no status below.
  3. Downloads that one object into a new private directory under --work-dir and requires its size, SHA-256,
     SHA-1 and MD5 to equal the ones its commit record states (the record was written after a read-back at
     upload time, so this is the end-to-end check of the bytes that left the VPS).
  4. Decrypts it with `age -d -i IDENTITY` (the identity is passed as a path, never printed or logged).
  5. Unpacks the tar with the backup tool's own reader (exactly three files, a manifest of the known shape,
     the snapshot equal to the manifest's size and SHA-256) into a fresh directory of the scratch area, and
     checks that the manifest's time is the time in the point's name.
  6. Opens the snapshot READ ONLY and checks: SQLite's integrity check, foreign keys, no free page (every
     point is rewritten by `VACUUM INTO`), exactly one Alembic revision, and every --expect-rows TABLE=N: the
     table exists and holds at least N rows. Nothing but counts is read from the database.
  7. Removes the scratch directory, on every exit path including SIGTERM/SIGINT/SIGHUP. A directory that cannot
     be removed is a failure, never a pass: it holds a plaintext database.

What it never does: write to the remote (it lists, reads one commit record, and downloads one object; none of
the commands that create, move or delete exists in this file), write anywhere but the new directory it makes
under --work-dir, open an existing file for writing, touch a Docker volume, a live database, `restore` or the
production configuration. It does not replace the restore of RUNBOOK 15.6 and section 10 onto a scratch volume
with the image of the manifest's tag (that also checks the schema against the application's migrations); it is
the part of the rehearsal that can run anywhere, on the point that actually left the VPS.

Nothing here prints content. Lines carry the point's name, sizes, the checksum of the ENCRYPTED object, the
manifest's image tag and time, a schema revision that is a plain identifier, and the counts you asked for. The
output of age and rclone is not forwarded.

Exit status: 0 the point was retrieved, decrypted, unpacked and verified (and the scratch directory removed);
1 failed before a verdict (rclone, age or the disk failed, a signal, a scratch directory that could not be
removed); 2 usage; 3 refused before anything was done (configuration, identity, host, no committed point);
4 NOT RESTORABLE: the point was retrieved but does not verify (its bytes differ from its record, it does not
decrypt with this identity, the tar or the database is not sound, an expectation is not met); 128+N a signal.
Only 0 says the point can be restored.
"""

import argparse
import contextlib
import dataclasses
import importlib.util
import os
import re
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence


def _load_backup_tool():
    """The sibling program deploy/offsite_backup.py, loaded by path so that the way this program is started
    (a script, `python -I`, a test) does not decide which file is meant. Already loaded: that module."""
    existing = sys.modules.get("offsite_backup")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("offsite_backup", Path(__file__).resolve().with_name("offsite_backup.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["offsite_backup"] = module
    spec.loader.exec_module(module)
    return module


core = _load_backup_tool()

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED, EXIT_NOT_RESTORABLE = 0, 1, 2, 3, 4

RUN_PREFIX = "hh-rehearsal-"
IDENTITY_MARKER = b"AGE-SECRET-KEY-1"  # what every age X25519 identity starts with; compared upper-cased
MAX_IDENTITY_BYTES = 1 << 16
MAX_EXPECTATIONS = 20
# A directory whose existence means "this is the production host" (deploy/systemd/*.env.example install there).
PRODUCTION_MARKERS = (Path("/etc/house-helper"),)
TABLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
REVISION_RE = re.compile(r"[A-Za-z0-9_]{1,64}", re.ASCII)
EXPECT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]{0,63})=([0-9]{1,9})", re.ASCII)
REVISION_TABLE = "alembic_version"
SQLITE_MAGIC = b"SQLite format 3\x00"


def say(message: str) -> None:
    print(f"offsite-rehearsal: {message}", file=sys.stderr, flush=True)


class NotRestorable(core.OffsiteError):
    """The point was retrieved but does not verify. The message is safe to print."""

    exit_code = EXIT_NOT_RESTORABLE


# --- what is checked before anything runs ------------------------------------------------------


def _inside(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except (OSError, ValueError):
        return True  # cannot tell: treated as inside, which refuses


def _forbidden_places(deploy_dir: Path, markers: Sequence[Path]) -> List[Path]:
    """Where neither the private identity nor the scratch directory may be: the repository checkout (the
    directory that holds deploy/) and the production configuration."""
    return [deploy_dir.resolve().parent, *markers]


def check_production_host(markers: Sequence[Path]) -> None:
    for marker in markers:
        if core.is_there(marker):
            raise core.Refused(
                f"{marker} exists on this host, which marks it as the production host. The private age identity "
                "must never be on that host: run the rehearsal on your own machine (RUNBOOK 15.13)."
            )


def check_identity_file(path: Path, forbidden: Sequence[Path]) -> None:
    """The identity must be the owner's secret and treated as one. Reads the file; prints nothing from it."""
    if not core.is_plain_file(path):
        raise core.Refused("--identity-file must be an existing regular file (not a link).")
    if any(_inside(path, place) for place in forbidden):
        raise core.Refused(
            "--identity-file is inside the repository checkout or the production configuration. A private key "
            "must never be kept there: move it out of the tree."
        )
    try:
        size = path.stat().st_size
        if size > MAX_IDENTITY_BYTES:
            raise core.Refused("--identity-file is larger than any age identity file.")
        if os.name != "nt" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise core.Refused("--identity-file is readable by other users: make it readable by its owner only (chmod 600).")
        content = path.read_bytes()
    except OSError:
        raise core.Refused("--identity-file cannot be read.") from None
    if not any(line.strip().upper().startswith(IDENTITY_MARKER) for line in content.splitlines()):
        raise core.Refused("--identity-file holds no age secret key (a line AGE-SECRET-KEY-1...).")


def check_work_dir(work: Path, forbidden: Sequence[Path]) -> None:
    if not work.is_absolute() or work.is_symlink() or not work.is_dir() or not os.access(work, os.W_OK | os.X_OK):
        raise core.Refused("--work-dir must be an existing absolute directory (not a link) that this user can write.")
    if any(_inside(work, place) for place in forbidden):
        raise core.Refused("--work-dir is inside the repository checkout or the production configuration: use a scratch place elsewhere.")


def parse_expectation(text: str) -> tuple:
    match = EXPECT_RE.fullmatch(text)
    if match is None or int(match.group(2)) < 1:
        raise argparse.ArgumentTypeError("must be TABLE=N, a table name and a minimum row count of at least 1")
    return match.group(1), int(match.group(2))


# --- the database ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Verified:
    revision: str
    tables: int
    found: Dict[str, int]  # the expectations: table -> rows found


def verify_database(path: Path, expectations: Dict[str, int]) -> Verified:
    """Read-only checks of the unpacked snapshot. NotRestorable, with a message that quotes nothing from the
    database, unless it is sound, rewritten (no free page), at exactly one Alembic revision and holds what
    was asked for. Only counts are read."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(len(SQLITE_MAGIC))
    except OSError:
        raise core.Failed("the unpacked snapshot cannot be read.") from None
    if header != SQLITE_MAGIC:
        raise NotRestorable("the snapshot is not a SQLite database file.")
    connection = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        problems = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if problems != ["ok"]:
            raise NotRestorable(f"SQLite's integrity check of the snapshot failed ({len(problems)} problem(s)).")
        violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        if violations:
            raise NotRestorable(f"the snapshot has {violations} foreign-key violation(s).")
        if connection.execute("PRAGMA freelist_count").fetchone()[0] != 0:
            raise NotRestorable("the snapshot has free pages: it was not rewritten as every recovery point of this tool is.")
        tables = [
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        if REVISION_TABLE not in tables:
            raise NotRestorable("the snapshot has no Alembic revision table: it is not this application's database.")
        revisions = [str(row[0]) for row in connection.execute(f"SELECT version_num FROM {REVISION_TABLE}")]
        if len(revisions) != 1 or not REVISION_RE.fullmatch(revisions[0]):
            raise NotRestorable("the snapshot does not carry exactly one plain Alembic revision.")
        found: Dict[str, int] = {}
        for table, minimum in sorted(expectations.items()):
            if table not in tables or not TABLE_RE.fullmatch(table):
                raise NotRestorable(f"expected table {table} is not in the snapshot.")
            count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            found[table] = count
            if count < minimum:
                raise NotRestorable(f"expected at least {minimum} row(s) in {table}, found {count}.")
        return Verified(revisions[0], len(tables), found)
    except sqlite3.Error as failure:
        raise NotRestorable(f"SQLite could not read the snapshot ({type(failure).__name__}).") from None
    finally:
        if connection is not None:
            connection.close()


# --- the rehearsal ---------------------------------------------------------------------------


@dataclasses.dataclass
class Settings:
    remote: str
    rclone_config: Path
    identity_file: Path
    work_dir: Path
    expectations: Dict[str, int]
    point: Optional[str] = None
    access_mode: str = core.ACCESS_APP_FOLDER


def choose_point(folder, requested: Optional[str], now: datetime) -> str:
    """The committed point to rehearse: the one named, or the newest not dated in the future. Refuses anything
    that is not committed, and says why when the folder knows."""
    committed = {p.name: p for p in folder.committed}
    if requested is not None:
        if not core.POINT_RE.fullmatch(requested):
            raise core.Refused("--point is not the name of a recovery point (house-helper-<UTC time>-<8 hex>.tar.age).")
        if requested in committed:
            return requested
        if requested in folder.unresolved:
            raise core.Refused(f"{requested} is UNRESOLVED ({folder.unresolved[requested]}): not a recovery point, so not rehearsed.")
        raise core.Refused(f"{requested} is not a committed recovery point in the folder.")
    usable = [p for p in committed.values() if p.moment <= now + core.FUTURE_TOLERANCE]
    if not usable:
        raise core.Refused("the folder holds no committed recovery point (nothing with a valid commit record that matches it).")
    return max(usable, key=lambda p: (p.moment, p.name)).name


class Rehearsal:
    def __init__(
        self,
        settings: Settings,
        runner: "core.Runner",
        deploy_dir: Path,
        clock: Callable[[], datetime],
        log: Callable[[str], None] = say,
        production_markers: Sequence[Path] = PRODUCTION_MARKERS,
    ) -> None:
        self.settings, self.runner, self.deploy_dir, self.clock, self.log = settings, runner, deploy_dir, clock, log
        self.markers = tuple(production_markers)
        self.tools: Dict[str, str] = {}
        self.remote = None
        self.run_dir: Optional[Path] = None

    def preflight(self) -> None:
        settings = self.settings
        forbidden = _forbidden_places(self.deploy_dir, self.markers)
        # The same offline authorization as `run` and `list` of the backup, first; only the rule that the full-disk
        # configuration must sit where root alone can change it is left out (trust=None): it is for the VPS.
        core.authorize_remote(settings.rclone_config, settings.remote, settings.access_mode, trust=None)
        check_production_host(self.markers)
        check_identity_file(settings.identity_file, forbidden)
        check_work_dir(settings.work_dir, forbidden)
        for name in ("rclone", "age"):
            found = self.runner.which(name)
            if not found:
                raise core.Refused(f"{name} is not installed on this machine.")
            self.tools[name] = found
        self.remote, has_app_folder = core.open_remote(
            self.runner, self.tools["rclone"], settings.rclone_config, settings.remote, settings.access_mode
        )
        if settings.access_mode == core.ACCESS_FULL_DISK:
            self.log(core.full_disk_notice(settings.remote, has_app_folder))

    def run(self) -> str:
        """The verdict line of a passed rehearsal. Raises OffsiteError (and Interrupted) otherwise."""
        self.preflight()
        for entry in sorted(self.settings.work_dir.iterdir()):
            if entry.name.startswith(RUN_PREFIX) and entry.is_dir() and not entry.is_symlink():
                self.log(f"WARNING: {entry.name} in the work directory is left by an earlier rehearsal and may hold a plaintext database: remove it.")
        self.run_dir = Path(tempfile.mkdtemp(prefix=RUN_PREFIX, dir=self.settings.work_dir))
        try:
            verdict = self.rehearse()
        finally:
            removed = self.clean_up()
        if not removed:
            raise core.Failed(
                f"the scratch directory {self.run_dir.name} could not be removed and holds the plaintext snapshot: "
                "remove it by hand. This is not a pass."
            )
        return verdict

    def rehearse(self) -> str:
        settings, remote, run_dir = self.settings, self.remote, self.run_dir

        self.log("step 1/5: listing the folder and choosing a committed recovery point ...")
        folder = core.classify_folder(remote.listing(), remote.read_record)
        for name, why in folder.unresolved.items():
            self.log(f"UNRESOLVED {name}: {why}. Not a recovery point; not used.")
        now = self.clock().astimezone(timezone.utc)
        name = choose_point(folder, settings.point, now)
        record = core.read_commit_record(remote.read_record(name), name)
        if record is None:  # classify_folder just read it; a record that changed since is not trusted
            raise core.Failed("the commit record of the chosen point changed while it was being read.")

        self.log(f"step 2/5: downloading {name} ({record.size} bytes) and comparing it with its commit record ...")
        sealed = run_dir / "point.tar.age"
        remote.download(name, sealed)
        if core.digests_of(sealed) != record:
            raise NotRestorable("the downloaded object is not the one its commit record describes (size or checksum differs).")
        with open(sealed, "rb") as handle:
            if handle.read(len(core.AGE_HEADER)) != core.AGE_HEADER:
                raise NotRestorable("the downloaded object is not an age file.")

        self.log("step 3/5: decrypting with the identity file (age) ...")
        tar_path = run_dir / "recovery-point.tar"
        done = self.runner.run(
            [self.tools["age"], "-d", "-i", str(settings.identity_file)],
            timeout=core.AGE_TIMEOUT, stdin_path=sealed, stdout_path=tar_path,
        )
        if done.returncode != 0:
            raise NotRestorable(
                f"age could not decrypt the point with this identity (exit {done.returncode}): it is not a recipient of this "
                "point, or the object is damaged."
            )
        self.discard(sealed)

        self.log("step 4/5: unpacking and checking the archive ...")
        restored = run_dir / "restored"
        restored.mkdir()
        try:
            manifest = core.read_recovery_tar(tar_path, extract_to=restored)
        except OSError:
            raise core.Failed("the recovery-point tar cannot be read or the scratch files cannot be written.") from None
        except core.Failed as failure:
            raise NotRestorable(f"the decrypted archive is not a recovery point: {str(failure).rstrip('.')}.") from None
        if core.POINT_RE.fullmatch(name).group(1) != manifest["created_utc"]:
            raise NotRestorable("the time in the manifest is not the time in the point's name.")
        self.discard(tar_path)

        self.log("step 5/5: checking the database read-only ...")
        verified = verify_database(restored / core.SNAPSHOT_NAME, settings.expectations)
        wanted = ", ".join(f"{table}>={settings.expectations[table]} (found {count})" for table, count in sorted(verified.found.items()))
        return (
            f"rehearsal PASSED: {name} (sha256 of the encrypted object {record.sha256}) matched its commit record, decrypted with the "
            f"supplied identity, unpacked and verified in a scratch directory that is now removed. Made {manifest['created_utc']} by image "
            f"{manifest['image_tag']}; schema revision {verified.revision}; {verified.tables} table(s); expected rows: {wanted}. "
            "Nothing was written to the remote or to any production location."
        )

    def discard(self, path: Path) -> None:
        """Remove a plaintext or ciphertext scratch file as soon as it is not needed. Best effort: the clean-up removes the rest."""
        with contextlib.suppress(OSError):
            path.unlink()

    def clean_up(self) -> bool:
        """Remove the scratch directory, whatever happened. Ignores further signals; never raises. Whether it is gone."""
        previous = {}
        for number in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if number is not None:
                with contextlib.suppress(Exception):
                    previous[number] = signal.signal(number, signal.SIG_IGN)
        try:
            if self.run_dir is None:
                return True
            shutil.rmtree(self.run_dir, ignore_errors=True)
            gone = not core.is_there(self.run_dir)
            if not gone:
                self.log(f"WARNING: could not remove {self.run_dir}; it holds a plaintext database. Remove it by hand.")
            return gone
        finally:
            for number, handler in previous.items():
                with contextlib.suppress(Exception):
                    signal.signal(number, handler)


# --- command line ------------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        say(f"usage: {message}")
        raise SystemExit(EXIT_USAGE)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="offsite_rehearsal.py", allow_abbrev=False,
        description="Restore rehearsal of an off-host recovery point, on the owner's own machine (never the VPS).",
    )
    parser.add_argument("--rclone-config", required=True, type=Path, help="an rclone config file of the remote, mode 0600")
    parser.add_argument("--remote", required=True, help="NAME:FOLDER, as for the backup")
    parser.add_argument(
        "--access-mode", choices=core.ACCESS_MODES, default=core.ACCESS_APP_FOLDER,
        help="as for the backup: the same mode the points were made in",
    )
    parser.add_argument("--identity-file", required=True, type=Path, help="the age private identity (never on the VPS, never in the repository)")
    parser.add_argument("--work-dir", required=True, type=Path, help="an existing absolute scratch directory outside the checkout")
    parser.add_argument(
        "--expect-rows", required=True, action="append", type=parse_expectation, metavar="TABLE=N",
        help=f"the restored database must have this table with at least N rows (N >= 1); at least one, at most {MAX_EXPECTATIONS}",
    )
    parser.add_argument("--point", help="a committed point to rehearse (default: the newest)")
    return parser


def _signal_handler(signum, frame):
    raise core.Interrupted(signum)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runner: Optional["core.Runner"] = None,
    clock: Optional[Callable[[], datetime]] = None,
    deploy_dir: Optional[Path] = None,
    log: Callable[[str], None] = say,
    production_markers: Sequence[Path] = PRODUCTION_MARKERS,
) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)
    if len(args.expect_rows) > MAX_EXPECTATIONS:
        log("usage: too many --expect-rows.")
        return EXIT_USAGE
    expectations = dict(args.expect_rows)
    if len(expectations) != len(args.expect_rows):
        log("usage: a table is named twice in --expect-rows.")
        return EXIT_USAGE
    os.umask(0o077)
    installed = {}
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(Exception):
                installed[number] = signal.signal(number, _signal_handler)
    try:
        settings = Settings(
            remote=args.remote, rclone_config=args.rclone_config, identity_file=args.identity_file, work_dir=args.work_dir,
            expectations=expectations, point=args.point, access_mode=args.access_mode,
        )
        verdict = Rehearsal(
            settings, runner or core.Runner(), deploy_dir or Path(__file__).resolve().parent,
            clock or (lambda: datetime.now(timezone.utc)), log, production_markers,
        ).run()
        log(verdict)
        return EXIT_OK
    except core.OffsiteError as failure:
        label = "refused" if isinstance(failure, core.Refused) else "NOT RESTORABLE" if isinstance(failure, NotRestorable) else "FAILED"
        log(f"{label}: {failure}")
        return failure.exit_code
    except core.Interrupted as stop:
        log(f"interrupted by signal {stop.signum}; the clean-up ran.")
        return 128 + stop.signum
    except Exception as failure:  # the text of an unexpected exception can quote paths: class only
        log(f"FAILED: unexpected {type(failure).__name__}.")
        return EXIT_FAILED
    finally:
        for number, handler in installed.items():
            with contextlib.suppress(Exception):
                signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
