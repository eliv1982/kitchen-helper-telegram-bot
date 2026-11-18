"""The frozen PostgreSQL 16 recovery-point contract (HH-DB-01 Stage 2 WP-2D).

Frozen source: HH-DB-01_Architecture_Frozen_Specification_v1.0.md §8.1-§8.11.

This module is pure where it can be: it renders the SQL that observes a snapshot, it
builds and validates the format-v2 manifest, it parses the archive TOC and the scoped
``SEQUENCE SET`` extraction, and it writes/reads the recovery-point tar. It never opens
a database connection, never runs a container and never handles a credential. The host
tool (``deploy/postgres/maintenance.py``) owns the Docker/identity/credential boundary,
exactly as ``deploy/postgres/enroll.py`` does for enrollment.

The archive contract
--------------------
A format-v2 recovery point is a tar (later wrapped in ``age`` by the offsite tool) whose
members appear in this order:

    MANIFEST.json
    snapshot.dump.sha256
    snapshot.dump

``snapshot.dump`` is ``pg_dump --format=custom --compress=zstd`` taken with the frozen
flags in ``DUMP_FLAGS`` from one exported snapshot (BAK-03/BAK-06). It is *not* expected
to be byte-deterministic (BAK-05).

The sequence pair is never read from the live database (SEQ-01). It is extracted from
exactly one ``SEQUENCE SET`` TOC entry (SEQ-03) and parsed as data under the strict
envelope of SEQ-05/SEQ-06; it is never executed. The parser below fails closed on
anything it does not recognize.

The normalized object contract is generated from the real migration chain and compared
against a checked-in fixture (BAK-08/BAK-10). A new migration deliberately regenerates
the fixture; a hand edit is a review event.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from house_helper.persistence import pg_artifacts
from house_helper.persistence import pg_contract

FORMAT_V1 = "house-helper-recovery-point/1"
FORMAT_V2 = "house-helper-recovery-point/2"
DATABASE_KIND = "postgresql"

MANIFEST_NAME = "MANIFEST.json"
CHECKSUM_NAME = "snapshot.dump.sha256"
DUMP_NAME = "snapshot.dump"
ARCHIVE_MEMBERS = (MANIFEST_NAME, CHECKSUM_NAME, DUMP_NAME)

# BAK-06: the exact flags are frozen in one constant and asserted by a test. A change
# requires a format-version decision; it is never a local edit at a call site.
DUMP_FLAGS: tuple[str, ...] = (
    "--format=custom",
    "--compress=zstd",
    "--schema=public",
    "--encoding=UTF8",
    "--no-owner",
    "--no-privileges",
    "--no-tablespaces",
    "--no-subscriptions",
    "--no-publications",
    "--no-large-objects",
)

# SEQ-05: the pinned PostgreSQL 16 client envelope. Both the variable and the value are
# exact; a different value is a different (unreviewed) client behaviour.
ALLOWED_SESSION_SETTINGS: tuple[str, ...] = (
    "SET statement_timeout = 0;",
    "SET lock_timeout = 0;",
    "SET idle_in_transaction_session_timeout = 0;",
    "SET client_encoding = 'UTF8';",
    "SET standard_conforming_strings = on;",
    "SET check_function_bodies = false;",
    "SET xmloption = content;",
    "SET client_min_messages = warning;",
    "SET row_security = off;",
)
SEARCH_PATH_SETTING = "SELECT pg_catalog.set_config('search_path', '', false);"

_EXPECTED_DUMP_SETTINGS = {
    "datestyle": "ISO, YMD",
    "timezone": "UTC",
    "bytea_output": "hex",
    "extra_float_digits": "3",
}

_MANIFEST_KEYS = frozenset(
    {
        "format",
        "database",
        "server_version_num",
        "system_identifier",
        "pg_dump_version",
        "dump_format",
        "compression",
        "alembic_revision",
        "encoding",
        "datcollate",
        "datctype",
        "roles_expected",
        "acl_contract_sha256",
        "tables",
        "sequence",
        "snapshot",
        "dump_settings",
    }
)
_SEQUENCE_KEYS = frozenset(
    {
        "name",
        "last_value",
        "is_called",
        "next_allocatable",
        "max_messages_id",
        "source",
        "unowned",
        "owner",
    }
)
_SNAPSHOT_KEYS = frozenset({"bytes", "sha256"})
_TABLE_KEYS = frozenset({"rows", "digest"})

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SYSTEM_IDENTIFIER = re.compile(r"[0-9]{1,20}")
_REVISION = re.compile(r"[A-Za-z0-9_]{1,128}")
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_TOC_LINE = re.compile(r"^(\d+);\s+(\d+)\s+(\d+)\s+(.+?)\s+(\S+)\s+(\S+)\s+(\S+)$")
_COMMENT_NAME = re.compile(
    r"^-- Name: (\S+); Type: SEQUENCE SET; Schema: (\S+); Owner: (\S+)$"
)
_RESTRICT = re.compile(r"^\\(restrict|unrestrict) (\S+)$")
_SETVAL = re.compile(
    r"^SELECT pg_catalog\.setval\('((?:[^']|'')+)'(::regclass)?, ([0-9]+), (true|false)\);$"
)

SEQUENCE_NAME = getattr(pg_contract, "MESSAGES_SEQUENCE", "messages_id_seq")
MESSAGES_TABLE = "messages"


class BackupError(Exception):
    """A fail-closed archive/manifest refusal. `code` is stable and carries no secret."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


# --- hashing and canonical JSON --------------------------------------------------------------


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise BackupError("not_canonical", "the value is not canonical JSON data") from None


def acl_contract_digest() -> str:
    """BAK-14: the checked-in role/privilege artifacts hashed into the manifest.

    ``bootstrap.sql`` is included because it is the E3 creation/maintenance-database
    contract; ``schema.sql`` and ``privileges.sql`` are included because they establish
    the schema and object ACLs a restore must reapply. ``roles.sql`` is cluster-global.
    """
    return pg_artifacts.manifest_digest()


# --- manifest --------------------------------------------------------------------------------


def build_manifest(
    *,
    server_version_num: int,
    system_identifier: str,
    pg_dump_version: str,
    alembic_revision: str,
    encoding: str,
    datcollate: str,
    datctype: str,
    tables: Mapping[str, Mapping[str, Any]],
    sequence: Mapping[str, Any],
    dump_bytes: int,
    dump_sha256: str,
) -> dict[str, Any]:
    manifest = {
        "format": FORMAT_V2,
        "database": DATABASE_KIND,
        "server_version_num": int(server_version_num),
        "system_identifier": str(system_identifier),
        "pg_dump_version": str(pg_dump_version),
        "dump_format": "custom",
        "compression": "zstd",
        "alembic_revision": str(alembic_revision),
        "encoding": str(encoding),
        "datcollate": str(datcollate),
        "datctype": str(datctype),
        "roles_expected": list(pg_contract.PROJECT_ROLE_NAMES),
        "acl_contract_sha256": acl_contract_digest(),
        "tables": {
            str(name): {"rows": int(fact["rows"]), "digest": str(fact["digest"])}
            for name, fact in tables.items()
        },
        "sequence": dict(sequence),
        "snapshot": {"bytes": int(dump_bytes), "sha256": str(dump_sha256)},
        "dump_settings": dict(_EXPECTED_DUMP_SETTINGS),
    }
    failures = manifest_failures(manifest)
    if failures:
        raise BackupError(
            "manifest_invalid",
            "the generated manifest does not satisfy the frozen contract",
            ", ".join(failures),
        )
    return manifest


def manifest_failures(manifest: object) -> tuple[str, ...]:
    """Every structural/contract failure of a manifest, in a stable order."""
    if not isinstance(manifest, Mapping):
        return ("manifest:not_an_object",)
    failures: list[str] = []
    if set(manifest) != _MANIFEST_KEYS:
        failures.append("manifest:keys")
    if manifest.get("format") != FORMAT_V2:
        failures.append("manifest:format")
    if manifest.get("database") != DATABASE_KIND:
        failures.append("manifest:database")
    version = manifest.get("server_version_num")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or not 160000 <= version <= 169999
    ):
        failures.append("manifest:server_version_num")
    if not _SYSTEM_IDENTIFIER.fullmatch(str(manifest.get("system_identifier", ""))):
        failures.append("manifest:system_identifier")
    dump_version = str(manifest.get("pg_dump_version", ""))
    if not re.fullmatch(r"16\.\d{1,2}(\.\d{1,2})?", dump_version):
        failures.append("manifest:pg_dump_version")
    if manifest.get("dump_format") != "custom":
        failures.append("manifest:dump_format")
    if manifest.get("compression") != "zstd":
        failures.append("manifest:compression")
    if not _REVISION.fullmatch(str(manifest.get("alembic_revision", ""))):
        failures.append("manifest:alembic_revision")
    if manifest.get("encoding") != pg_contract.EXPECTED_ENCODING:
        failures.append("manifest:encoding")
    for key in ("datcollate", "datctype"):
        value = manifest.get(key)
        if not isinstance(value, str) or not value or len(value) > 64:
            failures.append(f"manifest:{key}")
    if list(manifest.get("roles_expected", ())) != list(pg_contract.PROJECT_ROLE_NAMES):
        failures.append("manifest:roles_expected")
    if manifest.get("acl_contract_sha256") != acl_contract_digest():
        failures.append("manifest:acl_contract_sha256")
    tables = manifest.get("tables")
    expected_tables = sorted(pg_contract.expected_tables())
    if not isinstance(tables, Mapping):
        failures.append("manifest:tables")
    else:
        if sorted(tables) != expected_tables:
            failures.append("manifest:table_set")
        for name, fact in tables.items():
            if not _IDENTIFIER.fullmatch(str(name)) or not isinstance(fact, Mapping):
                failures.append(f"manifest:table:{name}")
                continue
            if set(fact) != _TABLE_KEYS:
                failures.append(f"manifest:table_keys:{name}")
                continue
            rows = fact.get("rows")
            if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
                failures.append(f"manifest:table_rows:{name}")
            if not _SHA256.fullmatch(str(fact.get("digest", ""))):
                failures.append(f"manifest:table_digest:{name}")
    sequence = manifest.get("sequence")
    if not isinstance(sequence, Mapping):
        failures.append("manifest:sequence")
    else:
        failures.extend(sequence_failures(sequence, max_messages_id=sequence.get("max_messages_id")))
    snapshot = manifest.get("snapshot")
    if not isinstance(snapshot, Mapping) or set(snapshot) != _SNAPSHOT_KEYS:
        failures.append("manifest:snapshot")
    else:
        size = snapshot.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            failures.append("manifest:snapshot_bytes")
        if not _SHA256.fullmatch(str(snapshot.get("sha256", ""))):
            failures.append("manifest:snapshot_sha256")
    settings = manifest.get("dump_settings")
    if not isinstance(settings, Mapping) or dict(settings) != _EXPECTED_DUMP_SETTINGS:
        failures.append("manifest:dump_settings")
    return tuple(failures)


def sequence_failures(
    sequence: Mapping[str, Any],
    *,
    max_messages_id: Any,
    seqmin: Optional[int] = None,
    seqmax: Optional[int] = None,
) -> tuple[str, ...]:
    """SEQ-07/SEQ-08 invariants over the archive-derived raw pair.

    ``seqmin``/``seqmax`` are supplied when the catalog parameters are known; the
    manifest alone can still be checked for the two exact-comparison rules and the
    ``next > max`` rule.
    """
    failures: list[str] = []
    if set(sequence) != _SEQUENCE_KEYS:
        failures.append("sequence:keys")
    if sequence.get("name") != SEQUENCE_NAME:
        failures.append("sequence:name")
    last = sequence.get("last_value")
    called = sequence.get("is_called")
    if not isinstance(last, int) or isinstance(last, bool):
        failures.append("sequence:last_value")
        return tuple(failures)
    if not isinstance(called, bool):
        failures.append("sequence:is_called")
        return tuple(failures)
    expected_next = last + (1 if called else 0)
    if sequence.get("next_allocatable") != expected_next:
        failures.append("sequence:next_allocatable")
    if sequence.get("source") != "archive:SEQUENCE SET":
        failures.append("sequence:source")
    if sequence.get("unowned") is not False:
        failures.append("sequence:unowned")
    if sequence.get("owner") != pg_contract.OWNER_ROLE:
        failures.append("sequence:owner")
    maximum = max_messages_id
    if maximum is not None:
        if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 0:
            failures.append("sequence:max_messages_id")
        elif expected_next <= maximum:
            failures.append("sequence:behind_rows")
    if seqmin is not None and last < seqmin:
        failures.append("sequence:below_min")
    if seqmax is not None and last > seqmax:
        failures.append("sequence:above_max")
    if seqmax is not None and expected_next > seqmax:
        failures.append("sequence:exhausted")
    return tuple(failures)


def compare_sequence(
    archive: Mapping[str, Any], restored: tuple[int, bool]
) -> tuple[str, ...]:
    """SEQ-07: both fields of the restored raw pair must equal the archive's."""
    failures: list[str] = []
    last, called = restored
    if archive.get("last_value") != last:
        failures.append("sequence:last_value_mismatch")
    if archive.get("is_called") is not called:
        failures.append("sequence:is_called_mismatch")
    return tuple(failures)


# --- SQL renderers ---------------------------------------------------------------------------


def digest_sql(table: object) -> str:
    """One table's ``(count, canonical digest)`` query.

    The digest is order-independent: each row is rendered by ``row_to_json`` (column
    order is the table definition's), hashed with MD5, and the sorted row hashes are
    concatenated and hashed with SHA-256. The pinned ``dump_settings`` make the text
    representation deterministic across the backup and restored instances.
    """
    name = str(table)
    if name not in pg_contract.expected_tables() or not _IDENTIFIER.fullmatch(name):
        raise BackupError("table_name", "the table is not one of the image's tables")
    quoted = f'"{name}"'
    return (
        "SELECT count(*)::text, "
        "encode(sha256(coalesce(string_agg(h, '' ORDER BY h), '')::bytea), 'hex') "
        f'FROM (SELECT md5(row_to_json(t)::text) AS h FROM public.{quoted} AS t) AS rows'
    )


def table_digest_value(count: str, digest: str) -> dict[str, Any]:
    try:
        rows = int(count)
    except (TypeError, ValueError):
        raise BackupError("digest_count", "a table row count is not an integer") from None
    value = str(digest)
    if not _SHA256.fullmatch(value):
        raise BackupError("digest_value", "a table digest is not a SHA-256 hex value")
    return {"rows": rows, "digest": value}


def session_preamble() -> str:
    return "\n".join(
        (
            "\\set ON_ERROR_STOP on",
            "\\pset tuples_only on",
            "\\pset format unaligned",
            "\\pset fieldsep '|'",
            "SET datestyle = 'ISO, YMD';",
            "SET timezone = 'UTC';",
            "SET bytea_output = 'hex';",
            "SET extra_float_digits = 3;",
        )
    )


def backup_script(*, tables: Iterable[str], staging: str = "/staging") -> str:
    """The psql program a disposable client container executes for BAK-03.

    The program opens ONE ``REPEATABLE READ`` transaction, exports a snapshot, runs
    ``pg_dump --snapshot=<id>`` synchronously from inside that transaction, computes
    every count/digest in the same transaction, writes the sequence catalog facts and
    commits. A post-dump ``count(*)`` would describe a different dataset and is never
    used.

    The ``pg_dump`` command writes the absolute dump flags of ``DUMP_FLAGS`` plus the
    exported snapshot id. The shell escape writes ``dump.ok`` only on success; the host
    tool refuses a run without it.
    """
    names = sorted(str(table) for table in tables)
    for name in names:
        if name not in pg_contract.expected_tables():
            raise BackupError("table_name", "the table set is not the image's table set")
    facts = []
    for name in names:
        facts.append(
            "SELECT 'table', '{name}', count(*)::text, "
            "encode(sha256(coalesce(string_agg(h, '' ORDER BY h), '')::bytea), 'hex') "
            "FROM (SELECT md5(row_to_json(t)::text) AS h FROM public.\"{name}\" AS t) AS rows;".format(
                name=name
            )
        )
    dump_flags = " ".join(DUMP_FLAGS)
    lines = [
        session_preamble(),
        "BEGIN ISOLATION LEVEL REPEATABLE READ;",
        "SELECT pg_export_snapshot() AS snap \\gset",
        f"\\o {staging}/snapshot.id",
        "SELECT :'snap';",
        "\\o",
        # The dump is taken INSIDE the exported snapshot's transaction. The file and
        # the success marker are written by the client container; both are checked.
        "\\! sh -c 'pg_dump "
        + dump_flags
        + " --snapshot=\"$(cat "
        + staging
        + "/snapshot.id)\" --file="
        + staging
        + "/snapshot.dump house_helper && printf ok > "
        + staging
        + "/dump.ok'",
        # BAK-06 refusals, evaluated before the facts are accepted. The dump already
        # ran; the host deletes it and reports a failure if any check fires.
        "DO $hh_backup_guard$ BEGIN "
        "IF (SELECT count(*) FROM pg_catalog.pg_largeobject_metadata) <> 0 THEN "
        "RAISE EXCEPTION 'the database has large objects; the frozen dump refuses them'; "
        "END IF; "
        "IF EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspname <> 'public' "
        "AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%') THEN "
        "RAISE EXCEPTION 'a non-system schema other than public exists'; "
        "END IF; "
        "IF (SELECT count(*) FROM pg_catalog.pg_subscription) <> 0 THEN "
        "RAISE EXCEPTION 'the database has subscriptions; the frozen dump refuses them'; "
        "END IF; "
        "END $hh_backup_guard$;",
        f"\\o {staging}/facts.txt",
        "SELECT 'server_version_num', pg_catalog.current_setting('server_version_num');",
        "SELECT 'database', pg_catalog.pg_encoding_to_char(d.encoding), d.datcollate, "
        "d.datctype FROM pg_catalog.pg_database d WHERE d.datname = pg_catalog.current_database();",
        "SELECT 'revision', version_num FROM public.alembic_version;",
        "SELECT 'max_messages_id', coalesce(max(id)::text, '') FROM public.messages;",
        "SELECT 'sequence_catalog', n.nspname, c.relname, "
        "pg_catalog.pg_get_userbyid(c.relowner), "
        "pg_catalog.pg_get_serial_sequence('public.messages', 'id'), "
        "(SELECT count(*) FROM pg_catalog.pg_depend d "
        " WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid "
        " AND d.refclassid = 'pg_catalog.pg_class'::regclass AND d.deptype = 'a'), "
        "s.seqtypid::regtype::text, s.seqstart, s.seqincrement, s.seqmax, s.seqmin, "
        "s.seqcache, s.seqcycle "
        "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_catalog.pg_sequence s ON s.seqrelid = c.oid "
        "WHERE n.nspname = 'public' AND c.relname = '" + SEQUENCE_NAME + "';",
        *facts,
        "\\o",
        "COMMIT;",
    ]
    return "\n".join(lines) + "\n"


_DATABASE_FACT = re.compile(r"^database\|UTF8\|[^|]*\|[^|]*$")


def parse_facts(text: object) -> dict[str, Any]:
    """Parse the line protocol written by ``backup_script`` without trusting it."""
    if not isinstance(text, str) or not text.strip():
        raise BackupError("facts_missing", "the backup facts file is empty")
    result: dict[str, Any] = {"tables": {}}
    seen: set[str] = set()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("|")
        kind = parts[0]
        if kind in seen and kind != "table":
            raise BackupError("facts_duplicate", "a backup fact appears twice", kind)
        if kind == "table":
            if len(parts) != 4:
                raise BackupError("facts_malformed", "a table fact is malformed")
            name = parts[1]
            if name in result["tables"] or name not in pg_contract.expected_tables():
                raise BackupError("facts_table", "a table fact names an unexpected table")
            result["tables"][name] = table_digest_value(parts[2], parts[3])
            continue
        seen.add(kind)
        if kind == "server_version_num" and len(parts) == 2 and parts[1].isdigit():
            result["server_version_num"] = int(parts[1])
        elif kind == "database" and len(parts) == 4 and _DATABASE_FACT.fullmatch(raw):
            result["encoding"], result["datcollate"], result["datctype"] = parts[1:]
        elif kind == "revision" and len(parts) == 2 and _REVISION.fullmatch(parts[1]):
            result["revision"] = parts[1]
        elif kind == "max_messages_id" and len(parts) == 2:
            value = parts[1]
            if value == "":
                result["max_messages_id"] = None
            elif value.isdigit():
                result["max_messages_id"] = int(value)
            else:
                raise BackupError("facts_malformed", "the maximum message id is not an integer")
        elif kind == "sequence_catalog" and len(parts) == 13:
            result["sequence_catalog"] = {
                "schema": parts[1],
                "name": parts[2],
                "owner": parts[3],
                "resolved": parts[4],
                "owned_by_count": int(parts[5]) if parts[5].isdigit() else -1,
                "seqtypid": parts[6],
                "seqstart": _int_fact(parts[7]),
                "seqincrement": _int_fact(parts[8]),
                "seqmax": _int_fact(parts[9]),
                "seqmin": _int_fact(parts[10]),
                "seqcache": _int_fact(parts[11]),
                "seqcycle": parts[12] == "t",
            }
        else:
            raise BackupError("facts_malformed", "a backup fact is not recognized", kind)
    required = {
        "server_version_num",
        "encoding",
        "datcollate",
        "datctype",
        "revision",
        "max_messages_id",
        "sequence_catalog",
    }
    missing = sorted(required - set(result))
    if missing:
        raise BackupError("facts_missing", "the backup facts are incomplete", ", ".join(missing))
    if sorted(result["tables"]) != sorted(pg_contract.expected_tables()):
        raise BackupError("facts_tables", "the backup facts do not cover the image's tables")
    catalog = result["sequence_catalog"]
    if (
        catalog["schema"] != "public"
        or catalog["name"] != SEQUENCE_NAME
        or catalog["resolved"] != f"public.{SEQUENCE_NAME}"
        or catalog["owned_by_count"] != 1
        or catalog["owner"] != pg_contract.OWNER_ROLE
        or catalog["seqincrement"] != 1
        or catalog["seqcycle"] is not False
        or catalog["seqstart"] < catalog["seqmin"]
        or catalog["seqmin"] < 1
        or catalog["seqmax"] > 2**63 - 1
    ):
        raise BackupError(
            "sequence_catalog", "the sequence does not satisfy the frozen catalog contract"
        )
    return result


def _int_fact(value: str) -> int:
    if not re.fullmatch(r"-?[0-9]{1,20}", value or ""):
        raise BackupError("facts_malformed", "a sequence parameter is not an integer")
    return int(value)


# --- TOC and the scoped SEQUENCE SET extraction ----------------------------------------------


@dataclass(frozen=True)
class TocEntry:
    entry_id: int
    catalog_oid: int
    object_oid: int
    object_class: str
    schema: str
    name: str
    owner: str
    table: str = ""
    raw: str = ""

    def list_line(self) -> str:
        """The exact TOC line for a `--use-list` file.

        The TOC grammar is not uniform: `CONSTRAINT` and `FK CONSTRAINT` carry the
        table before the constraint name, `DEFAULT` carries the table before the column,
        and `COMMENT` carries a sub-object class. Storing the observed line avoids
        reconstructing a shape that the pinned client did not emit.
        """
        if self.raw:
            return self.raw
        return (
            f"{self.entry_id}; {self.catalog_oid} {self.object_oid} "
            f"{self.object_class} {self.schema} {self.name} {self.owner}"
        )


# The pinned pg_dump 16 TOC classes, longest first so "FK CONSTRAINT" is not read as
# "CONSTRAINT". The comment header lines and the fixed `id; cat oid` prefix are handled
# separately.
_TOC_CLASSES = (
    "FK CONSTRAINT",
    "SEQUENCE OWNED BY",
    "MATERIALIZED VIEW DATA",
    "SEQUENCE SET",
    "TABLE DATA",
    "DEFAULT ACL",
    "CONSTRAINT",
    "SCHEMA",
    "COMMENT",
    "DEFAULT",
    "TABLE",
    "INDEX",
    "SEQUENCE",
    "ACL",
)
_TOC_PREFIX = re.compile(r"^(\d+);\s+(\d+)\s+(\d+)\s+")


def parse_toc(text: object) -> tuple[TocEntry, ...]:
    """Parse `pg_restore -l` under the pinned PostgreSQL 16 shapes.

    The classes with an extra table field are recognized explicitly; anything else is
    refused rather than silently assigned to the wrong field. `raw` keeps the original
    line for the `--use-list` file.
    """
    if not isinstance(text, str):
        raise BackupError("toc", "the archive TOC is not text")
    entries: list[TocEntry] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.startswith(";"):
            continue
        prefix = _TOC_PREFIX.match(line)
        if not prefix:
            raise BackupError("toc", "an archive TOC line does not match the pinned grammar")
        rest = line[prefix.end():]
        object_class = next(
            (name for name in _TOC_CLASSES if rest.startswith(name + " ")), ""
        )
        if not object_class:
            raise BackupError("toc", "an archive TOC line names an unpinned object class")
        fields = rest[len(object_class) + 1:].split()
        table = ""
        if object_class in ("CONSTRAINT", "FK CONSTRAINT", "DEFAULT"):
            if len(fields) != 4:
                raise BackupError("toc", "a constraint TOC line is malformed")
            schema, table, name, owner = fields
        elif object_class == "COMMENT":
            if len(fields) != 4:
                raise BackupError("toc", "a comment TOC line is malformed")
            schema, _subtype, name, owner = fields
        else:
            if len(fields) != 3:
                raise BackupError("toc", "a TOC line does not carry schema/name/owner")
            schema, name, owner = fields
        entries.append(
            TocEntry(
                entry_id=int(prefix.group(1)),
                catalog_oid=int(prefix.group(2)),
                object_oid=int(prefix.group(3)),
                object_class=object_class,
                schema=schema,
                name=name,
                owner=owner,
                table=table,
                raw=line,
            )
        )
    if not entries:
        raise BackupError("toc", "the archive TOC has no object entries")
    return tuple(entries)


def select_sequence_set(
    entries: Sequence[TocEntry], *, name: str = SEQUENCE_NAME
) -> TocEntry:
    """SEQ-03: exactly one ``SEQUENCE SET`` entry for the expected sequence."""
    matches = [
        entry
        for entry in entries
        if entry.object_class == "SEQUENCE SET"
        and entry.schema == "public"
        and entry.name == name
    ]
    if len(matches) != 1:
        raise BackupError(
            "sequence_set_selection",
            "the archive must contain exactly one SEQUENCE SET entry for the sequence",
            str(len(matches)),
        )
    entry = matches[0]
    if entry.owner != pg_contract.OWNER_ROLE:
        raise BackupError(
            "sequence_set_owner", "the SEQUENCE SET entry does not carry the owner provenance"
        )
    return entry


def select_table_data(
    entries: Sequence[TocEntry], tables: Iterable[str]
) -> tuple[TocEntry, ...]:
    """BAK-08: exactly one ``TABLE DATA`` TOC entry for every contract table.

    TOC metadata alone is not evidence that the payload is complete: a missing,
    duplicated or unexpected entry is refused here, and the caller still verifies the
    dump's own SHA-256 and the per-table logical digests after the restore.
    """
    expected = {str(name) for name in tables}
    seen: dict[str, list[TocEntry]] = {}
    unexpected: list[str] = []
    for entry in entries:
        if entry.object_class != "TABLE DATA":
            continue
        if entry.schema != "public" or entry.name not in expected:
            unexpected.append(entry.list_line())
            continue
        seen.setdefault(entry.name, []).append(entry)
    missing = sorted(expected - set(seen))
    duplicated = sorted(name for name, items in seen.items() if len(items) != 1)
    if missing or duplicated or unexpected:
        detail = []
        if missing:
            detail.append("missing " + ",".join(missing))
        if duplicated:
            detail.append("duplicated " + ",".join(duplicated))
        if unexpected:
            detail.append("unexpected " + "; ".join(unexpected))
        raise BackupError(
            "table_data_selection",
            "the archive must contain exactly one TABLE DATA entry for every table",
            " | ".join(detail),
        )
    return tuple(seen[name][0] for name in sorted(seen))


@dataclass(frozen=True)
class SequenceExtraction:
    last_value: int
    is_called: bool


def parse_sequence_extraction(
    text: object, *, name: str = SEQUENCE_NAME
) -> SequenceExtraction:
    """SEQ-05/SEQ-06: parse the scoped extraction as data, never execute it.

    Only the pinned comment forms, the exact allowlisted session settings, one matching
    ``\\restrict``/``\\unrestrict`` pair, and exactly one ``setval`` on the expected
    schema-qualified sequence are accepted. Every other byte is a refusal.
    """
    if not isinstance(text, str) or not text.strip():
        raise BackupError("sequence_extraction", "the sequence extraction is empty")
    wrapper: Optional[str] = None
    closed = False
    setval: Optional[tuple[str, int, bool]] = None
    seen_settings: set[str] = set()
    allowed_comments = {
        "--",
        "-- PostgreSQL database dump",
        "-- PostgreSQL database dump complete",
    }
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in allowed_comments:
            continue
        if stripped.startswith("-- Dumped from database version ") or stripped.startswith(
            "-- Dumped by pg_dump version "
        ):
            continue
        comment = _COMMENT_NAME.match(stripped)
        if comment:
            entry_name, schema, owner = comment.groups()
            if (schema, entry_name) != ("public", name) or owner not in (
                "-",
                pg_contract.OWNER_ROLE,
            ):
                raise BackupError(
                    "sequence_extraction_comment",
                    "a comment names an unexpected sequence entry",
                )
            continue
        restrict = _RESTRICT.match(stripped)
        if restrict:
            direction, key = restrict.groups()
            if direction == "restrict":
                if wrapper is not None:
                    raise BackupError("sequence_wrapper", "nested or duplicate \\restrict")
                wrapper = key
            else:
                if wrapper is None or closed or key != wrapper:
                    raise BackupError("sequence_wrapper", "unmatched or duplicate \\unrestrict")
                closed = True
            continue
        if stripped.startswith("\\"):
            raise BackupError("sequence_command", "an unexpected backslash command was found")
        if stripped == SEARCH_PATH_SETTING or stripped in ALLOWED_SESSION_SETTINGS:
            if stripped in seen_settings:
                raise BackupError(
                    "sequence_setting", "a session setting appears more than once"
                )
            seen_settings.add(stripped)
            continue
        match = _SETVAL.match(stripped)
        if match:
            if setval is not None:
                raise BackupError("sequence_setval", "more than one setval statement")
            literal = match.group(1).replace("''", "'")
            if literal != f"public.{name}":
                raise BackupError("sequence_setval", "setval names an unexpected sequence")
            value = int(match.group(3))
            if value > 2**63 - 1:
                raise BackupError(
                    "sequence_setval", "setval's value is outside the signed 64-bit range"
                )
            setval = (literal, value, match.group(4) == "true")
            continue
        raise BackupError("sequence_extraction", "an unrecognized line is present")
    if wrapper is not None and not closed:
        raise BackupError("sequence_wrapper", "an unbalanced \\restrict wrapper")
    if setval is None:
        raise BackupError("sequence_setval", "no setval statement was found")
    return SequenceExtraction(last_value=setval[1], is_called=setval[2])


def sequence_manifest(
    extraction: SequenceExtraction,
    *,
    max_messages_id: Optional[int],
    seqmin: int,
    seqmax: int,
) -> dict[str, Any]:
    sequence = {
        "name": SEQUENCE_NAME,
        "last_value": extraction.last_value,
        "is_called": extraction.is_called,
        "next_allocatable": extraction.last_value + (1 if extraction.is_called else 0),
        "max_messages_id": max_messages_id,
        "source": "archive:SEQUENCE SET",
        "unowned": False,
        "owner": pg_contract.OWNER_ROLE,
    }
    failures = sequence_failures(
        sequence, max_messages_id=max_messages_id, seqmin=seqmin, seqmax=seqmax
    )
    if failures:
        raise BackupError(
            "sequence_invalid",
            "the archive-derived sequence state fails the frozen invariant",
            ", ".join(failures),
        )
    return sequence


# --- the archive -----------------------------------------------------------------------------


def checksum_line(digest: str, name: str = DUMP_NAME) -> bytes:
    if not _SHA256.fullmatch(digest):
        raise BackupError("checksum", "the dump digest is not a SHA-256 hex value")
    return f"{digest}  {name}\n".encode("utf-8")


def write_archive(
    path: object,
    *,
    manifest: Mapping[str, Any],
    dump: bytes,
) -> dict[str, Any]:
    """Write the three members in the frozen order and re-read the result."""
    failures = manifest_failures(manifest)
    if failures:
        raise BackupError("manifest_invalid", "the manifest is not valid", ", ".join(failures))
    if not isinstance(dump, (bytes, bytearray)) or not dump:
        raise BackupError("dump_empty", "the dump is empty")
    digest = sha256_bytes(bytes(dump))
    snapshot = dict(manifest["snapshot"])
    if snapshot["bytes"] != len(dump) or snapshot["sha256"] != digest:
        raise BackupError("snapshot_mismatch", "the dump does not match the manifest")
    target = _path(path)
    manifest_bytes = canonical_json(manifest) + b"\n"
    with open(target, "wb") as handle:
        with tarfile.open(fileobj=handle, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            _add_member(archive, MANIFEST_NAME, manifest_bytes)
            _add_member(archive, CHECKSUM_NAME, checksum_line(digest))
            _add_member(archive, DUMP_NAME, bytes(dump))
    try:
        # The recovery point holds application rows. It is private until the offsite
        # tool encrypts it; never rely on the operator's umask.
        os.chmod(target, 0o600)
    except OSError:
        raise BackupError(
            "archive_permissions", "the recovery point could not be made private"
        ) from None
    read = read_archive(target)
    if read.manifest != dict(manifest) or read.dump != bytes(dump):
        raise BackupError("archive_roundtrip", "the written archive did not read back")
    return dict(read.manifest)


def _add_member(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    info.mode = 0o600
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(value))


def _path(value: object):
    from pathlib import Path

    if not isinstance(value, (str, bytes, Path)):
        raise BackupError("archive_path", "the archive path is not a path")
    return Path(value)


@dataclass(frozen=True)
class RecoveryPoint:
    manifest: Mapping[str, Any]
    dump: bytes
    checksum: bytes

    @property
    def format(self) -> str:
        return str(self.manifest.get("format", ""))


def read_archive(path: object) -> RecoveryPoint:
    """Read and validate a v2 archive. A v1 archive is refused here, never converted."""
    target = _path(path)
    try:
        with tarfile.open(target, mode="r:*") as archive:
            members = archive.getnames()
            if tuple(members) != ARCHIVE_MEMBERS:
                raise BackupError(
                    "archive_members",
                    "the archive members are not the frozen v2 set in the frozen order",
                )
            manifest_bytes = _member(archive, MANIFEST_NAME)
            checksum = _member(archive, CHECKSUM_NAME)
            dump = _member(archive, DUMP_NAME)
    except (OSError, tarfile.TarError):
        raise BackupError("archive_unreadable", "the recovery point cannot be read") from None
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BackupError("archive_manifest", "the manifest is not JSON") from None
    if manifest.get("format") == FORMAT_V1:
        raise BackupError("archive_v1", "a v1 recovery point is not restored by this tool")
    failures = manifest_failures(manifest)
    if failures:
        raise BackupError("archive_manifest", "the manifest is not valid", ", ".join(failures))
    digest = sha256_bytes(dump)
    expected = checksum_line(digest).strip()
    if checksum.strip() != expected:
        raise BackupError("archive_checksum", "snapshot.dump.sha256 does not match the dump")
    snapshot = dict(manifest["snapshot"])
    if snapshot["bytes"] != len(dump) or snapshot["sha256"] != digest:
        raise BackupError("archive_checksum", "the manifest does not match the dump")
    if canonical_json(manifest) + b"\n" != manifest_bytes:
        raise BackupError("archive_manifest", "the manifest is not canonical JSON")
    return RecoveryPoint(manifest=manifest, dump=dump, checksum=checksum)


def _member(archive: tarfile.TarFile, name: str) -> bytes:
    info = archive.getmember(name)
    if not info.isfile():
        raise BackupError("archive_members", "an archive member is not a regular file")
    handle = archive.extractfile(info)
    if handle is None:
        raise BackupError("archive_members", "an archive member cannot be read")
    return handle.read()


# --- the normalized object contract ----------------------------------------------------------

OBJECT_CONTRACT_FORMAT = "house-helper-pg-object-contract/1"
_OBJECT_CONTRACT_KEYS = frozenset(
    {"format", "revision", "relations", "columns", "constraints", "indexes", "sequence"}
)


def object_contract_sql() -> str:
    """The catalog observation behind the normalized object contract (BAK-08)."""
    return "\n".join(
        (
            "SELECT 'relation', c.relkind, n.nspname, c.relname, "
            "pg_catalog.pg_get_userbyid(c.relowner) "
            "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r','S','i','p') ORDER BY 2,3,4;",
            "SELECT 'column', c.relname, a.attnum, a.attname, "
            "pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "coalesce(pg_catalog.pg_get_expr(d.adbin, d.adrelid), ''), "
            "CASE WHEN a.attcollation = 0 THEN '' ELSE coalesce(coll.collname, '') END "
            "FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "LEFT JOIN pg_catalog.pg_collation coll ON coll.oid = a.attcollation "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r','p') AND a.attnum > 0 "
            "AND NOT a.attisdropped ORDER BY 2,3;",
            "SELECT 'constraint', c.relname, con.conname, con.contype, con.convalidated, "
            "pg_catalog.pg_get_constraintdef(con.oid) "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' ORDER BY 2,3;",
            "SELECT 'index', t.relname, i.relname, x.indisunique, x.indisprimary, "
            "coalesce(pg_catalog.pg_get_expr(x.indpred, x.indrelid), ''), "
            "pg_catalog.pg_get_indexdef(x.indexrelid) "
            "FROM pg_catalog.pg_index x "
            "JOIN pg_catalog.pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_catalog.pg_class t ON t.oid = x.indrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = 'public' ORDER BY 2,3;",
            "SELECT 'sequence', c.relname, s.seqtypid::regtype::text, s.seqstart, "
            "s.seqincrement, s.seqmax, s.seqmin, s.seqcache, s.seqcycle, "
            "coalesce((SELECT tn.nspname || '.' || tc.relname || '.' || a.attname "
            " FROM pg_catalog.pg_depend d "
            " JOIN pg_catalog.pg_class tc ON tc.oid = d.refobjid "
            " JOIN pg_catalog.pg_namespace tn ON tn.oid = tc.relnamespace "
            " JOIN pg_catalog.pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid "
            " WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid "
            " AND d.refclassid = 'pg_catalog.pg_class'::regclass AND d.deptype = 'a'), '') "
            "FROM pg_catalog.pg_sequence s JOIN pg_catalog.pg_class c ON c.oid = s.seqrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' ORDER BY 2;",
        )
    ) + "\n"


# BAK-08/BAK-09/RSK-04. PostgreSQL does not promise that `pg_get_constraintdef` and
# `pg_get_indexdef` return byte-identical text after a definition has been re-parsed by
# `pg_restore`. Only these deparse differences are demonstrated and permitted, and they
# are value-preserving for a string constant:
#
#   * parentheses around a single atom (a literal, number, name or quoted name) carry no
#     grouping and are dropped; function/construct call parentheses are not touched;
#   * a cast of a string literal to an unbounded character type (`text`,
#     `character varying`/`varchar` with no typmod) does not change the literal;
#   * a cast of an `ARRAY[...]` constructor of such literals to that type's array does
#     not change the elements.
#
# Everything else is compared token by token: string-literal contents (including a
# `public.` prefix or whitespace), casts on non-literals (a uuid value cast to `text` is
# a different expression), casts with a typmod (`varchar(10)` truncates), and grouping
# parentheses (a parenthesised operator expression is never folded away). That is what
# makes the old "strip `public.` and every `::text`" transform unsafe: it equated
# `'public.telegram'` with `'telegram'`, `'public.text'` with `'text'`, and
# `md5(id::text)` with `md5(id)`.
_TEXT_FAMILY_TYPES = frozenset({"text", "character varying", "varchar"})
_ATOM_KINDS = frozenset({"word", "number", "string", "quoted"})
_PUNCTUATION = "()[],.;"
_WORD_START = re.compile(r"[A-Za-z_]")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str


@dataclass(frozen=True)
class _CastType:
    base: str
    is_array: bool
    has_typmod: bool
    end: int


def _tokenize_definition(value: object) -> list[_Token]:
    """Tokens of a deparsed expression; literal contents are never interpreted."""
    text = str(value)
    tokens: list[_Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "'" or (
            char in "eEbBxXnN" and index + 1 < length and text[index + 1] == "'"
        ):
            start = index
            if char != "'":
                index += 1
            index += 1
            while index < length:
                if text[index] == "'":
                    if index + 1 < length and text[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise BackupError("definition", "an unterminated string literal")
            tokens.append(_Token("string", text[start:index]))
            continue
        if char == '"':
            start = index
            index += 1
            while index < length:
                if text[index] == '"':
                    if index + 1 < length and text[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise BackupError("definition", "an unterminated quoted identifier")
            tokens.append(_Token("quoted", text[start:index]))
            continue
        if char == ":" and index + 1 < length and text[index + 1] == ":":
            tokens.append(_Token("op", "::"))
            index += 2
            continue
        if char.isdigit() or (
            char == "." and index + 1 < length and text[index + 1].isdigit()
        ):
            match = _NUMBER.match(text, index)
            if match is None:  # pragma: no cover - the branch condition guarantees a match
                raise BackupError("definition", "a numeric literal could not be read")
            tokens.append(_Token("number", match.group(0)))
            index = match.end()
            continue
        if _WORD_START.match(char):
            match = _WORD.match(text, index)
            if match is None:  # pragma: no cover - the branch condition guarantees a match
                raise BackupError("definition", "an identifier could not be read")
            tokens.append(_Token("word", match.group(0)))
            index = match.end()
            continue
        if char in _PUNCTUATION:
            tokens.append(_Token("punct", char))
            index += 1
            continue
        tokens.append(_Token("op", char))
        index += 1
    return tokens


def _matching(tokens: Sequence[_Token], start: int, opening: str, closing: str) -> Optional[int]:
    depth = 0
    for position in range(start, len(tokens)):
        if tokens[position].text == opening:
            depth += 1
        elif tokens[position].text == closing:
            depth -= 1
            if depth == 0:
                return position
    return None


def _parse_cast_type(tokens: Sequence[_Token], cast_index: int) -> Optional[_CastType]:
    """The type a `::` cast names, or None if it is not a type this parser understands."""
    position = cast_index + 1
    if (
        position + 2 < len(tokens)
        and tokens[position].kind == "word"
        and tokens[position].text.lower() == "pg_catalog"
        and tokens[position + 1].text == "."
        and tokens[position + 2].kind in ("word", "quoted")
    ):
        position += 2
    if position >= len(tokens) or tokens[position].kind not in ("word", "quoted"):
        return None
    first = tokens[position].text.lower()
    position += 1
    if (
        first == "character"
        and position < len(tokens)
        and tokens[position].kind == "word"
        and tokens[position].text.lower() == "varying"
    ):
        base = "character varying"
        position += 1
    elif (
        first in ("timestamp", "time")
        and position + 2 < len(tokens)
        and tokens[position].kind == "word"
        and tokens[position].text.lower() in ("with", "without")
        and tokens[position + 1].text.lower() == "time"
        and tokens[position + 2].text.lower() == "zone"
    ):
        base = f"{first} {tokens[position].text.lower()} time zone"
        position += 3
    else:
        base = first
    has_typmod = False
    if position < len(tokens) and tokens[position].text == "(":
        close = _matching(tokens, position, "(", ")")
        if close is None:
            return None
        has_typmod = True
        position = close + 1
    arrays = 0
    while (
        position + 1 < len(tokens)
        and tokens[position].text == "["
        and tokens[position + 1].text == "]"
    ):
        arrays += 1
        position += 2
    return _CastType(
        base=base, is_array=arrays > 0, has_typmod=has_typmod, end=position
    )


def _is_string_literal_array(
    tokens: Sequence[_Token], open_index: int, close_index: int
) -> bool:
    """True only for `[ 'literal' [, 'literal']* ]` with at least one element.

    BAK-09/F1: only an `ARRAY[...]` constructor of string literals may lose its
    text-family cast. A constructor holding a column reference, a numeric or any other
    compound expression does not have the same value once the cast is removed; its
    parentheses are then significant and must be preserved.
    """
    index = open_index + 1
    if index >= close_index:
        return False
    while index < close_index:
        if tokens[index].kind != "string":
            return False
        index += 1
        if index < close_index:
            if tokens[index].text != ",":
                return False
            index += 1
            if index >= close_index:
                return False
    return True


def _rewrite_definitions(tokens: list[_Token]) -> list[_Token]:
    """Apply only the permitted value-preserving deparse rewrites, to a fixpoint."""
    while True:
        changed = False
        # 1. Parentheses around one atom are redundant, *unless* they are the call
        #    parentheses of a function or of a construct such as `CAST`/`IN`/`ARRAY`
        #    (the preceding token is then a name). A group containing an operator, and
        #    therefore any grouping/precedence information, is always left untouched.
        reduced: list[_Token] = []
        index = 0
        while index < len(tokens):
            if (
                tokens[index].text == "("
                and index + 2 < len(tokens)
                and tokens[index + 1].kind in _ATOM_KINDS
                and tokens[index + 2].text == ")"
                and (
                    index == 0
                    or tokens[index - 1].kind not in ("word", "quoted")
                )
            ):
                reduced.append(tokens[index + 1])
                index += 3
                changed = True
                continue
            reduced.append(tokens[index])
            index += 1
        tokens = reduced
        # 2. An unbounded text-family cast of a string literal is the literal itself.
        reduced = []
        index = 0
        while index < len(tokens):
            if (
                tokens[index].kind == "string"
                and index + 1 < len(tokens)
                and tokens[index + 1].text == "::"
            ):
                cast = _parse_cast_type(tokens, index + 1)
                if (
                    cast is not None
                    and not cast.is_array
                    and not cast.has_typmod
                    and cast.base in _TEXT_FAMILY_TYPES
                ):
                    reduced.append(tokens[index])
                    index = cast.end
                    changed = True
                    continue
            reduced.append(tokens[index])
            index += 1
        tokens = reduced
        # 3. An unbounded text-family array cast of an ARRAY constructor of such
        #    literals is the constructor itself (with or without its own parentheses).
        reduced = []
        index = 0
        while index < len(tokens):
            if (
                tokens[index].text == "ARRAY"
                and index + 1 < len(tokens)
                and tokens[index + 1].text == "["
            ):
                close = _matching(tokens, index + 1, "[", "]")
                if (
                    close is not None
                    and close + 1 < len(tokens)
                    and tokens[close + 1].text == "::"
                ):
                    cast = _parse_cast_type(tokens, close + 1)
                    if (
                        cast is not None
                        and cast.is_array
                        and not cast.has_typmod
                        and cast.base in _TEXT_FAMILY_TYPES
                        and _is_string_literal_array(tokens, index + 1, close)
                    ):
                        reduced.extend(tokens[index : close + 1])
                        index = cast.end
                        changed = True
                        continue
            if (
                tokens[index].text == "("
                and index + 2 < len(tokens)
                and tokens[index + 1].text == "ARRAY"
                and tokens[index + 2].text == "["
            ):
                close = _matching(tokens, index + 2, "[", "]")
                if (
                    close is not None
                    and close + 2 < len(tokens)
                    and tokens[close + 1].text == ")"
                    and tokens[close + 2].text == "::"
                ):
                    cast = _parse_cast_type(tokens, close + 2)
                    if (
                        cast is not None
                        and cast.is_array
                        and not cast.has_typmod
                        and cast.base in _TEXT_FAMILY_TYPES
                        and _is_string_literal_array(tokens, index + 2, close)
                    ):
                        reduced.extend(tokens[index + 1 : close + 1])
                        index = cast.end
                        changed = True
                        continue
            reduced.append(tokens[index])
            index += 1
        tokens = reduced
        if not changed:
            return tokens


def _render_tokens(tokens: Sequence[_Token]) -> str:
    parts: list[str] = []
    for index, token in enumerate(tokens):
        if index and tokens[index - 1].kind in _ATOM_KINDS and token.kind in _ATOM_KINDS:
            parts.append(" ")
        parts.append(token.text)
    return "".join(parts)


def _normalize_definition(value: str) -> str:
    """A canonical, token-aware comparison form for a deparsed catalog expression.

    Literal contents are preserved byte for byte. Only the two permitted deparse
    equivalences above are applied; see the comment on `_TEXT_FAMILY_TYPES`.
    """
    return _render_tokens(_rewrite_definitions(_tokenize_definition(value)))


def object_contract_from_rows(
    rows: Iterable[Sequence[Any]], *, revision: str
) -> dict[str, Any]:
    """Normalize the catalog rows into the checked-in fixture shape."""
    relations: list[list[Any]] = []
    columns: list[list[Any]] = []
    constraints: list[list[Any]] = []
    indexes: list[list[Any]] = []
    sequence: list[list[Any]] = []
    for row in rows:
        values = [str(value) for value in row]
        kind = values[0]
        if kind == "relation" and len(values) == 5:
            relations.append([values[1], values[2], values[3], values[4]])
        elif kind == "column" and len(values) == 8:
            columns.append(
                [
                    values[1],
                    int(values[2]),
                    values[3],
                    values[4],
                    values[5] == "t",
                    _normalize_definition(values[6]),
                    values[7],
                ]
            )
        elif kind == "constraint" and len(values) == 6:
            constraints.append(
                [values[1], values[2], values[3], values[4] == "t", _normalize_definition(values[5])]
            )
        elif kind == "index" and len(values) == 7:
            indexes.append(
                [
                    values[1],
                    values[2],
                    values[3] == "t",
                    values[4] == "t",
                    _normalize_definition(values[5]),
                    _normalize_definition(values[6]),
                ]
            )
        elif kind == "sequence" and len(values) == 10:
            sequence.append(
                [
                    values[1],
                    values[2],
                    int(values[3]),
                    int(values[4]),
                    int(values[5]),
                    int(values[6]),
                    int(values[7]),
                    values[8] == "t",
                    values[9],
                ]
            )
        else:
            raise BackupError("object_contract", "an object-contract row is malformed")
    contract = {
        "format": OBJECT_CONTRACT_FORMAT,
        "revision": str(revision),
        "relations": sorted(relations),
        "columns": sorted(columns, key=lambda row: (row[0], row[1])),
        "constraints": sorted(constraints),
        "indexes": sorted(indexes),
        "sequence": sorted(sequence),
    }
    return contract


def object_contract_digest(contract: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json(contract))


def object_contract_failures(
    contract: object, fixture: object
) -> tuple[str, ...]:
    """Compare an observed contract with the checked-in fixture (BAK-08/BAK-10)."""
    if not isinstance(contract, Mapping) or set(contract) != _OBJECT_CONTRACT_KEYS:
        return ("object_contract:shape",)
    if not isinstance(fixture, Mapping) or set(fixture) != _OBJECT_CONTRACT_KEYS:
        return ("object_contract:fixture",)
    if contract.get("format") != OBJECT_CONTRACT_FORMAT:
        return ("object_contract:format",)
    failures: list[str] = []
    if contract.get("revision") != fixture.get("revision"):
        failures.append("object_contract:revision")
    for key in ("relations", "columns", "constraints", "indexes", "sequence"):
        if contract.get(key) != fixture.get(key):
            failures.append(f"object_contract:{key}")
    return tuple(failures)


def toc_contract_failures(
    entries: Sequence[TocEntry], fixture: Mapping[str, Any]
) -> tuple[str, ...]:
    """Level-1 object-contract check: the archive TOC names the fixture's objects.

    `pg_dump` emits tables, sequences, explicit indexes, primary/unique constraints and
    foreign keys as TOC entries. CHECK constraints live inside the table definition.
    PK/UQ constraints materialize their backing index, which is in the catalog fixture
    but not a separate TOC `INDEX` entry; the remaining counts must line up.
    """
    if not isinstance(fixture, Mapping):
        return ("toc_contract:fixture",)
    try:
        relations = fixture["relations"]
        constraints = fixture["constraints"]
    except (KeyError, TypeError):
        return ("toc_contract:fixture",)
    fixture_tables = {row[2] for row in relations if row[0] == "r" and row[1] == "public"}
    fixture_sequences = {row[2] for row in relations if row[0] == "S" and row[1] == "public"}
    fixture_indexes = {row[2] for row in relations if row[0] == "i" and row[1] == "public"}
    fixture_constraints = {
        (row[0], row[1]) for row in constraints if row[2] in ("p", "u", "f")
    }
    toc_tables = {
        entry.name for entry in entries if entry.object_class == "TABLE" and entry.schema == "public"
    }
    toc_sequences = {
        entry.name
        for entry in entries
        if entry.object_class == "SEQUENCE" and entry.schema == "public"
    }
    toc_indexes = {
        entry.name for entry in entries if entry.object_class == "INDEX" and entry.schema == "public"
    }
    toc_constraints = {
        (entry.table, entry.name)
        for entry in entries
        if entry.object_class in ("CONSTRAINT", "FK CONSTRAINT") and entry.schema == "public"
    }
    failures: list[str] = []
    if toc_tables != fixture_tables:
        failures.append("toc_contract:tables")
    if toc_sequences != fixture_sequences:
        failures.append("toc_contract:sequences")
    if toc_constraints != fixture_constraints:
        failures.append("toc_contract:constraints")
    if not toc_indexes.issubset(fixture_indexes):
        failures.append("toc_contract:indexes")
    backing = sum(1 for row in constraints if row[2] in ("p", "u"))
    if len(fixture_indexes) - len(toc_indexes) != backing:
        failures.append("toc_contract:index_count")
    return tuple(failures)


def load_object_contract_fixture(path: object) -> dict[str, Any]:
    target = _path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise BackupError("fixture", "the object-contract fixture cannot be read") from None
    if not isinstance(value, dict) or set(value) != _OBJECT_CONTRACT_KEYS:
        raise BackupError("fixture", "the object-contract fixture has an unexpected shape")
    if value.get("format") != OBJECT_CONTRACT_FORMAT:
        raise BackupError("fixture", "the object-contract fixture has the wrong format")
    return value


# --- temporary database records (TMP-01..TMP-08) ---------------------------------------------

TEMP_DB_ACTIONS = (
    "authorized",
    "created_fenced",
    "restoring",
    "verified",
    "smoke_testing",
    "cleanup_pending",
    "retired",
)
TEMP_DB_PURPOSES = ("restore_verification", "smoke_clone", "recovery")
_OPERATION_ID = re.compile(r"[0-9a-f]{16,64}")


def temp_database_record(
    *,
    action: str,
    operation_id: str,
    token: str,
    database: str,
    purpose: str,
    system_identifier: str,
    connect_role: str = "hh_restore",
    archive_format: Optional[str] = None,
    archive_sha256: Optional[str] = None,
    archive_system_identifier: Optional[str] = None,
    lifecycle: Optional[str] = None,
) -> dict[str, Any]:
    """TMP-02: the exact, non-prefix operation record for one temporary database."""
    name = pg_artifacts.validate_database_name(database)
    if not name.startswith("house_helper_restore_"):
        raise BackupError("temp_database", "a temporary database must carry the restore prefix")
    if action not in TEMP_DB_ACTIONS:
        raise BackupError("temp_database_action", "the temporary database action is not frozen")
    if purpose not in TEMP_DB_PURPOSES:
        raise BackupError("temp_database_purpose", "the temporary database purpose is not frozen")
    if not _OPERATION_ID.fullmatch(operation_id) or not _OPERATION_ID.fullmatch(token):
        raise BackupError("temp_database_id", "the operation id or token is not hexadecimal")
    if not _SYSTEM_IDENTIFIER.fullmatch(system_identifier):
        raise BackupError("temp_database_identity", "the system identifier is malformed")
    if connect_role != "hh_restore":
        raise BackupError("temp_database_role", "a restore target admits hh_restore only")
    payload: dict[str, Any] = {
        "action": action,
        "operation_id": operation_id,
        "token": token,
        "database": name,
        "purpose": purpose,
        "system_identifier": system_identifier,
        "owner": pg_contract.OWNER_ROLE,
        "connect_role": connect_role,
        "lifecycle": lifecycle
        or "dropped after verification; kept only under an explicit Owner recovery decision",
    }
    if archive_format is not None:
        if archive_format != FORMAT_V2:
            raise BackupError("temp_database_archive", "the archive format is not v2")
        payload["archive"] = {
            "format": archive_format,
            "sha256": str(archive_sha256 or ""),
            "system_identifier": str(archive_system_identifier or ""),
        }
        if not _SHA256.fullmatch(payload["archive"]["sha256"]) or not _SYSTEM_IDENTIFIER.fullmatch(
            payload["archive"]["system_identifier"]
        ):
            raise BackupError("temp_database_archive", "the archive provenance is malformed")
    return payload


def active_temporary_databases(records: Iterable[Any]) -> dict[str, Mapping[str, Any]]:
    """TMP-01: exact names authorized by the newest non-retired operation record.

    A name whose newest record is ``retired`` is no longer authorized. A record with a
    malformed payload is ignored for authorization and reported by the caller's
    reconciliation; it never broadens the inventory.
    """
    latest: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for record in records:
        if getattr(record, "kind", None) != "temp_db":
            continue
        payload = getattr(record, "payload", None)
        if not isinstance(payload, Mapping):
            continue
        action = payload.get("action")
        name = payload.get("database")
        if action not in TEMP_DB_ACTIONS or not isinstance(name, str):
            continue
        if not isinstance(name, str) or not re.fullmatch(
            r"house_helper_restore_[a-z0-9]{1,32}", name
        ):
            continue
        seq = int(getattr(record, "seq", -1))
        latest[name] = (seq, payload)
    return {
        name: payload
        for name, (_, payload) in latest.items()
        if payload.get("action") != "retired"
    }
