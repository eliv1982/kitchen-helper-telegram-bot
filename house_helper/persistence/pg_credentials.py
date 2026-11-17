"""Per-role libpq `PGPASSFILE` handling for the PostgreSQL deploy surface (HH-DB-01 WP-2C-A).

Frozen source: ARC-03, AUTH-02..AUTH-04, OD-8 (as approved for WP-2B: one `PGPASSFILE`
per login, mode `0600`, owned by the container's running uid, mounted read-only into
exactly one service) and §4.1 of the WP-2C-A brief.

The module NEVER returns, prints, formats or logs a password. It reads a passfile only to
answer "is this file the one this role may use?"; a refusal names the path and the login,
never a field value. `PGPASSWORD`, `PGSERVICE`, `PGSERVICEFILE` and `PGSSLMODE` are
rejected as additional credential or TLS sources (the last because OD-6 is still open).

Production credentials are not generated or populated here. OD-8 remains the Owner's open
decision; population and rotation are operational acts outside this work package.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping, Optional

PASSFILE_MODE = 0o600
FORBIDDEN_SECRET_VARIABLES = ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "PGSSLMODE")

# The application image's runtime identity (Dockerfile `USER 10001:10001`). The HOST
# validates a per-role passfile against this identity because the file is bind-mounted
# into a container that runs as 10001:10001; comparing it with host root's uid is wrong
# (F11). Inside a container the same validation uses the current process identity, so
# there is no second, weaker rule. Journal (`root`) and bootstrap-credential (`0:0`)
# ownership are separate contracts and are never checked with these values.
RUNTIME_UID = 10001
RUNTIME_GID = 10001
_POSIX = os.name == "posix"


def _current_uid() -> int:
    return os.getuid()


def _current_gid() -> int:
    return os.getgid()


class PassfileError(Exception):
    """A refused credential source. `code` is stable; no field value is ever included."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


def environment_failures(environ: Mapping[str, str]) -> tuple[str, ...]:
    """A second credential source or an OD-6 TLS choice must not be present."""
    return tuple(name for name in FORBIDDEN_SECRET_VARIABLES if name in environ)


def _fields(line: str) -> list[str]:
    """Split one libpq passfile line on unescaped colons.

    The files this deployment uses contain no escapes, but a colon or backslash in a
    password is legal in the format; refusing to misparse it is better than silently
    accepting a line that does not mean what it looks like.
    """
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if escaped:
        raise PassfileError("passfile_format", "a passfile line ends inside an escape")
    fields.append("".join(current))
    return fields


def validate_passfile(
    path: object,
    *,
    login: str,
    host: str,
    port: str,
    database: str,
    require_private: bool = True,
    require_owner: bool = True,
    owner_uid: Optional[int] = None,
    owner_gid: Optional[int] = None,
) -> None:
    """Validate the file's kind, permissions, ownership and matching entry.

    A symlink is refused: a per-role credential mount is a regular file, and a symlink
    would make the "mounted into exactly one service" property unverifiable. On Windows
    (no POSIX mode/uid) the content checks still run.

    `owner_uid`/`owner_gid` default to the current process identity. The host tool passes
    `RUNTIME_UID`/`RUNTIME_GID`: the file belongs to the container's runtime user, not to
    the root process that reads the host directory.
    """
    if not isinstance(path, (str, os.PathLike)) or not str(path):
        raise PassfileError("passfile_path", "the passfile path is missing")
    candidate = Path(path)
    try:
        info = candidate.lstat()
    except OSError:
        raise PassfileError("passfile_missing", "the passfile does not exist") from None
    if stat.S_ISLNK(info.st_mode):
        raise PassfileError("passfile_symlink", "the passfile is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise PassfileError("passfile_kind", "the passfile is not a regular file")
    if info.st_size > 64 * 1024:
        raise PassfileError("passfile_size", "the passfile is larger than a per-role entry needs")
    if _POSIX:
        if require_private and stat.S_IMODE(info.st_mode) != PASSFILE_MODE:
            raise PassfileError("passfile_mode", "the passfile is not mode 0600")
        if require_owner:
            expected_uid = _current_uid() if owner_uid is None else int(owner_uid)
            expected_gid = _current_gid() if owner_gid is None else int(owner_gid)
            if info.st_uid != expected_uid:
                raise PassfileError(
                    "passfile_owner", "the passfile is not owned by the runtime user"
                )
            if info.st_gid != expected_gid:
                raise PassfileError(
                    "passfile_group", "the passfile is not in the runtime group"
                )
    try:
        text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise PassfileError("passfile_unreadable", "the passfile cannot be read") from None
    matches = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = _fields(line)
        if len(fields) != 5:
            raise PassfileError("passfile_format", "a passfile line does not have five fields")
        entry_host, entry_port, entry_database, entry_user, entry_password = fields
        if entry_user != login:
            raise PassfileError(
                "passfile_role", "the passfile holds an entry for a different login"
            )
        if not entry_password:
            raise PassfileError("passfile_empty", "the passfile entry has no password")
        # The host and port are exact: a wildcard would let this credential be offered to
        # another endpoint. The database is exact unless the caller explicitly expects the
        # OD-2 wildcard (the restore passfile).
        if entry_host != host or entry_port != port:
            continue
        if entry_database not in ((database,) if database != "*" else ("*",)):
            continue
        matches += 1
    if matches == 0:
        raise PassfileError(
            "passfile_entry", "the passfile has no entry for this service's host/database"
        )
    if matches > 1:
        raise PassfileError(
            "passfile_duplicate", "the passfile holds more than one matching entry"
        )


def provider(
    environ: Mapping[str, str],
    *,
    login: str,
    host: str,
    port: str,
    database: str,
    validate: bool = True,
    owner_uid: Optional[int] = None,
    owner_gid: Optional[int] = None,
) -> str:
    """The configured `PGPASSFILE` path, validated; no secret is returned.

    `validate=False` is for a wiring-only preflight that deliberately does not read the
    file (for example the host-side `check-credentials` summary, which reports booleans).
    """
    failures = environment_failures(environ)
    if failures:
        raise PassfileError(
            "credential_source",
            "a second credential source or a TLS choice is present",
            failures[0],
        )
    path = environ.get("PGPASSFILE")
    if not path:
        raise PassfileError("passfile_path", "PGPASSFILE is not set")
    if validate:
        validate_passfile(
            path,
            login=login,
            host=host,
            port=port,
            database=database,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
    return path


def summary(
    path: object,
    *,
    login: str,
    host: str,
    port: str,
    database: str,
    owner_uid: Optional[int] = None,
    owner_gid: Optional[int] = None,
) -> Mapping[str, bool]:
    """Non-secret booleans for an operator preflight. Never raises for a bad file."""
    result = {"exists": False, "private": False, "owned": False, "entry": False}
    candidate = Path(path) if path else None
    if candidate is None:
        return result
    try:
        info = candidate.lstat()
    except OSError:
        return result
    result["exists"] = stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    if _POSIX:
        expected_uid = _current_uid() if owner_uid is None else int(owner_uid)
        expected_gid = _current_gid() if owner_gid is None else int(owner_gid)
        result["private"] = stat.S_IMODE(info.st_mode) == PASSFILE_MODE
        result["owned"] = info.st_uid == expected_uid and info.st_gid == expected_gid
    else:
        result["private"] = True
        result["owned"] = True
    try:
        validate_passfile(
            candidate,
            login=login,
            host=host,
            port=port,
            database=database,
            require_private=_POSIX,
            require_owner=_POSIX,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
    except PassfileError:
        result["entry"] = False
    else:
        result["entry"] = True
    return result
