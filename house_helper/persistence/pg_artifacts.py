"""The production loader and renderer for the frozen `db/*.sql` artifacts (HH-DB-01 WP-2C-A).

The artifacts are the single source of the role, schema and privilege contract. This
module does not restate their SQL: it reads the files from the image's `db/` directory,
validates every substituted value against a fixed allowlist BEFORE any SQL text is built,
and returns the exact text that `deploy/ops/pg_oneshot.py` executes.

It is deliberately separate from `tests/postgres_sql.py`. The test runner remains the
independent implementation behind the accepted WP-1B/WP-1C suites; production code must
not import `tests`, and the test suite must not be the only thing that exercises the
artifact boundaries. Both load the same checked-in files.

Boundary facts (unchanged from WP-1B/WP-1C):

* `db/roles.sql` takes no parameter and must run as the bootstrap superuser.
* `db/bootstrap.sql` is phase-separated (`-- @phase: create|harden|enable`) and the
  `create`/`enable` phases are one statement each, sent alone on an autocommit connection.
* `db/schema.sql` and `db/privileges.sql` each contain a transaction guard and a
  transaction-local ROL-06 contract; the caller supplies ONE transaction.
* Only `house_helper` (grantee `hh_migrate`) and `house_helper_restore_<token>`
  (grantee `hh_restore`) are valid targets. Production WP-2C-A initializes and upgrades
  the serving database only.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Optional

# `house_helper/persistence/pg_artifacts.py` -> repository root (or /app in the image).
DB_DIRECTORY = Path(__file__).resolve().parents[2] / "db"

SERVING_DATABASE = "house_helper"
MAINTENANCE_DATABASES = frozenset({"postgres", "house_helper_admin"})
GRANTEES = frozenset({"hh_migrate", "hh_restore"})
PROJECT_ROLE_NAMES = (
    "hh_owner",
    "hh_migrate",
    "hh_app",
    "hh_purge",
    "hh_backup",
    "hh_provision",
    "hh_restore",
)
LOGIN_ROLE_NAMES = tuple(name for name in PROJECT_ROLE_NAMES if name != "hh_owner")

ARTIFACT_FILES = ("roles.sql", "bootstrap.sql", "schema.sql", "privileges.sql")

_RESTORE_DATABASE = re.compile(r"house_helper_restore_[a-z0-9]{1,32}")
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_PHASE = re.compile(r"^--\s*@phase:\s*(create|harden|enable)\s*$")


class ArtifactError(Exception):
    """A refused artifact operation. `code` is stable; no value is ever a credential."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


def directory() -> Path:
    """The directory the three frozen artifacts are read from."""
    if not DB_DIRECTORY.is_dir():
        raise ArtifactError("artifact_directory", "the db/ artifact directory is missing")
    return DB_DIRECTORY


def load(filename: str) -> str:
    if filename not in ARTIFACT_FILES:
        raise ArtifactError("artifact_name", "the artifact is not one of the frozen SQL files")
    path = directory() / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise ArtifactError("artifact_missing", f"{filename} is missing") from None
    if not text.strip():
        raise ArtifactError("empty_artifact", f"{filename} is empty")
    return text


def sha256_file(filename: str) -> str:
    return hashlib.sha256(load(filename).encode("utf-8")).hexdigest()


def manifest() -> Mapping[str, str]:
    """The SHA-256 of every frozen artifact, in a fixed order."""
    return {name: sha256_file(name) for name in ARTIFACT_FILES}


def manifest_digest() -> str:
    """One digest over the complete artifact set (canonical JSON, sorted keys)."""
    payload = json.dumps(manifest(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_database_name(name: object) -> str:
    if not isinstance(name, str):
        raise ArtifactError("database_name", "a database name must be text")
    if name == SERVING_DATABASE or _RESTORE_DATABASE.fullmatch(name):
        return name
    raise ArtifactError("database_name", "the database name is not an allowed one")


def validate_maintenance_database(name: object) -> str:
    if isinstance(name, str) and name in MAINTENANCE_DATABASES:
        return name
    raise ArtifactError("maintenance_database", "the maintenance database is not an allowed one")


def validate_grantee(name: object) -> str:
    if isinstance(name, str) and name in GRANTEES:
        return name
    raise ArtifactError("grantee", "the CONNECT grantee is not hh_migrate or hh_restore")


def validate_database_grantee(database: object, grantee: object) -> tuple[str, str]:
    name = validate_database_name(database)
    role = validate_grantee(grantee)
    expected = "hh_migrate" if name == SERVING_DATABASE else "hh_restore"
    if role != expected:
        raise ArtifactError(
            "grantee_database",
            "the CONNECT grantee does not match the database kind",
            f"{name}/{role}",
        )
    return name, role


def validate_role_name(name: object) -> str:
    if isinstance(name, str) and name in PROJECT_ROLE_NAMES:
        return name
    raise ArtifactError("role_name", "the role is not one of the seven project roles")


def _quote(validated: str) -> str:
    if not _IDENTIFIER.fullmatch(validated):  # defense in depth; the allowlists are the control
        raise ArtifactError("identifier", "an identifier that did not pass its allowlist")
    return f'"{validated}"'


def render_roles() -> str:
    return load("roles.sql")


def bootstrap_phases(sql: Optional[str] = None) -> Mapping[str, str]:
    text = load("bootstrap.sql") if sql is None else sql
    order: list[str] = []
    content: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        marker = _PHASE.match(line.strip())
        if marker:
            current = marker.group(1)
            if current in content:
                raise ArtifactError("bootstrap_phase", f"phase {current} appears twice")
            order.append(current)
            content[current] = []
        elif current is not None:
            content[current].append(line)
    if order != ["create", "harden", "enable"]:
        raise ArtifactError(
            "bootstrap_phase", "bootstrap.sql does not declare create, harden, enable in order"
        )
    phases = {name: "\n".join(lines).strip() for name, lines in content.items()}
    for name in ("create", "enable"):
        if _statement_count(phases[name]) != 1:
            raise ArtifactError(
                "bootstrap_phase",
                f"the {name} phase must be exactly one statement",
            )
    return phases


def _statement_count(sql: str) -> int:
    stripped = "\n".join(
        line for line in sql.splitlines() if line.strip() and not line.strip().startswith("--")
    ).strip()
    if not stripped.endswith(";"):
        raise ArtifactError("bootstrap_phase", "a bootstrap phase statement is not terminated")
    return stripped.count(";")


def render_bootstrap(
    database: object, grantee: object, *, phases: Optional[Mapping[str, str]] = None
) -> Mapping[str, str]:
    name, role = validate_database_grantee(database, grantee)
    source = bootstrap_phases() if phases is None else phases
    rendered = {}
    for phase, text in source.items():
        rendered[phase] = text.replace("{{database}}", _quote(name)).replace(
            "{{grantee}}", _quote(role)
        )
        if "{{" in rendered[phase]:
            raise ArtifactError("placeholder", "a bootstrap placeholder was not substituted")
    return rendered


def render_schema(database: object, *, sql: Optional[str] = None) -> str:
    name = validate_database_name(database)
    text = load("schema.sql") if sql is None else sql
    rendered = text.replace("{{database}}", name)
    if "{{" in rendered:
        raise ArtifactError("placeholder", "a schema placeholder was not substituted")
    return rendered


def render_privileges(database: object, *, sql: Optional[str] = None) -> str:
    name = validate_database_name(database)
    text = load("privileges.sql") if sql is None else sql
    rendered = text.replace("{{database}}", name)
    if "{{" in rendered:
        raise ArtifactError("placeholder", "a privileges placeholder was not substituted")
    return rendered


def require_serving(name: object, grantee: object) -> tuple[str, str]:
    """Production WP-2C-A drives the serving lifecycle only.

    A restore database follows RST-04 (a different ordered sequence) and belongs to the
    restore work package; it is refused here rather than run through the wrong lifecycle.
    """
    database, role = validate_database_grantee(name, grantee)
    if database != SERVING_DATABASE:
        raise ArtifactError(
            "restore_database",
            "the production executor initializes and upgrades house_helper only",
            database,
        )
    return database, role
