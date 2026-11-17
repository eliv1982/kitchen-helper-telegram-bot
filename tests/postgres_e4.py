"""The E4 state classifier (HH-DB-01 Stage 2, WP-1C-B).

Frozen source: §5.2 BOOT-07/BOOT-08 (the allowed-state matrix and its observation
rule), §5.1 BOOT-01/BOOT-06 and states E3-a/E3-b/E3-c (the connection fence),
§4.3 ENR-10/ENR-11 (recognized partial state and the E4 row of the interrupted-enrollment
reconciliation), the erratum FE-1 (default `PUBLIC` access is a recognized intermediate
state only while `datallowconn = false`) and ROL-23 (`PUBLIC` `CONNECT` after enablement
is fail-closed).

What this module answers
------------------------
Given the OBSERVED cluster, which row of the BOOT-07 matrix is this database in, and
what is the one action the specification allows next? Nothing else. It never repairs,
migrates, initializes, grants, revokes, enables or alters anything: every statement it
issues is a read of a `pg_catalog` relation, a `pg_catalog` function, or a setting the
server itself reports. It writes no journal record and infers nothing from the absence of
one (BOOT-08: a missing acknowledgment is never proof that an operation did not occur),
which is why the classifier reads only the cluster.

The five E4 states
------------------
* `ABSENT` - BOOT-07 row 1: no such database. Action: creation may proceed under the
  matching enrollment intent (BOOT-01 Steps 1-5). Ownership, locale, ACL and sessions
  have nothing to report.
* `DISABLED_UNHARDENED` - BOOT-07 row 2: connections are disabled and the database
  ACL is a state `REVOKE ALL ... FROM PUBLIC` + `GRANT CONNECT ... TO <grantee>` can
  converge to E3-b. Action: finish database-level hardening from the maintenance
  connection; do not connect (BOOT-06).
* `DISABLED_HARDENED` - BOOT-07 row 3: connections are disabled and the ACL is exactly
  E3-b. Action: enable connections only after verification.
* `ENABLED_MAINTENANCE_ONLY` - BOOT-07 row 4: connections are enabled, the ACL is
  exactly E3-c, and only the one maintenance login (`hh_migrate` for the serving
  database, `hh_restore` for a restore database) can connect. The action depends on
  the target half of the observation: `verify_target_objects` when the database has
  not been looked into yet, `initialize_schema` when it has and holds no application
  object.
* `FAIL_CLOSED` - BOOT-07 row 5: conflicting ownership or locale, unexpected `PUBLIC`
  access, an unrecognized or non-hardened ACL while enabled, an explicit grant outside
  the accepted shapes, an unauthorized session, an application object, a subscription
  replicating into the database, an observation that cannot be trusted, or any other
  unexpected state. Action: `remediate`; never "fix forward" by enabling access or
  adopting objects.

Why the target half exists
-------------------------
E4 must also hold that the database holds **nothing but PostgreSQL's own objects**
(§4.3 ENR-10/ENR-11 E4 row: "no application tables"; BOOT-04: `public` is the only
non-system schema; BOOT-09: an interrupted chain leaves zero application tables and
`alembic_version` absent). A PostgreSQL catalog is per-database, so another database's
relations cannot be read from the maintenance connection, and §5.1 BOOT-06 forbids
connecting at all while connections are disabled. The classifier therefore never opens a
connection itself:

1. `classify(control, database=..., grantee=...)` reads the maintenance-observable
   facts. If they are not the E3-c shape, the verdict is final. If they are, the verdict
   is `ENABLED_MAINTENANCE_ONLY` with `action = verify_target_objects` and
   `tables_checked = False`.
2. WP-1C-C then opens the ONE authorized maintenance-login connection and calls
   `classify(control, database=..., grantee=..., target=connection)`. The supplied
   connection must be to the same database and must be the grantee's session
   (BOOT-06 / row 4); only then is its catalog read. A clean database returns
   `action = initialize_schema`, which is the only verdict whose
   `may_initialize_schema` property is true. Any unexpected object returns
   `FAIL_CLOSED` with `unexpected_tables`.

The pre-initialization inventory is a baseline difference
---------------------------------------------------------
ENR-10 says every existing object must be in the authorized inventory and nothing
outside it may exist. Before BOOT-04, that inventory is **empty**: the only authorized
content of the database is the intrinsic PostgreSQL baseline produced by
`CREATE DATABASE ... TEMPLATE template0` (BOOT-01). Enumerating a blacklist of "forbidden"
object classes is not a contract - Codex R2 demonstrated twelve classes (collations,
conversions, text-search objects, large objects, operators, publications, casts, foreign
data wrappers, ...) that a blacklist missed, several creatable by the ordinary
`hh_owner` role. The classifier therefore decides the inventory by a **baseline
difference** instead:

* PostgreSQL's `pg_class.h` defines `FirstNormalObjectId` (16384). Every object `initdb`
  creates - the catalogs and their relations, the built-in types, operators, casts,
  operator classes and families, collations, conversions, text-search objects, the
  `public` schema and the `plpgsql` extension - has an object identifier **below** that
  value. Every object created after `initdb`, by any role including the bootstrap
  superuser, is allocated an identifier **at or above** it. A pristine
  `TEMPLATE template0` database is therefore exactly the set of catalog rows whose OID is
  below 16384; it was measured row-for-row on the pinned PostgreSQL 16.15 image and
  contains none above.
* A threshold alone is not enough for a class whose object can be created with an
  EXPLICITLY assigned identifier. `pg_catalog.lo_create(<oid>)` gives a large object the
  OID the caller names - measured on the same 16.15 image, OID 1000 is accepted - and such
  a row sits below the boundary. The classes `initdb` leaves EMPTY (large objects,
  publications, event triggers, foreign data wrappers and servers, default ACLs and
  transforms) therefore reject any row whatever its OID: they have no intrinsic instance
  to be a difference from, so every instance is created work. The OID boundary is kept
  only for the classes `initdb` really populates.
* The observation iterates every catalog that can hold an independently created object -
  not only `pg_class`, `pg_proc`, `pg_type` and `pg_namespace`, but also `pg_collation`,
  `pg_conversion`, `pg_operator`, `pg_opclass`, `pg_opfamily`, `pg_am`, `pg_cast`,
  `pg_ts_config`, `pg_ts_dict`, `pg_ts_parser`, `pg_ts_template`, `pg_extension`,
  `pg_publication`, `pg_event_trigger`, `pg_foreign_data_wrapper`, `pg_foreign_server`,
  `pg_largeobject_metadata`, `pg_language`, `pg_default_acl` and `pg_transform` - and
  reports every row that is not part of the baseline: at or above the OID boundary, or in
  an empty-baseline class at any OID.
* Objects that cannot exist without one already in the inventory are not enumerated a
  second time: a relation's row type and an array type are derived from the relation or
  the element type (`typrelid`/`typelem`); indexes, constraints, triggers, rules, policies,
  sequences of a table, publication members and user mappings all derive from a root that
  is reported. A session's `pg_temp_N` namespace is PostgreSQL's own and is not itself an
  unexpected schema, but **every object inside it is reported** - a temporary
  `alembic_version` is not the pre-initialization state.
* `pg_user_mapping` has no `PUBLIC` `SELECT`; a user mapping is reachable only through a
  foreign server (which is reported). `pg_subscription` is cluster-level state and must
  not be enumerated through the target session: it has no `PUBLIC` `SELECT` either (it
  holds a connection string), so a non-superuser read is refused by the server. What the
  observation reads is the credential-free `(oid, subname, subdbid)` triple, on the
  maintenance session - the bootstrap superuser, already behind the trust and freshness
  gate - and it reports every subscription whose `subdbid` is the target database. A
  database that is the target of a logical-replication subscription has been adopted by
  something other than BOOT-04, so it fails closed exactly like an application table.
  The connection string (`subconninfo`) is never selected, so no credential is ever read,
  carried in evidence or printed.

The inventory is read by **one statement**, so every row comes from one read-committed
snapshot: a table committed between two separate catalog scans cannot be half-seen and
authorize initialization. An object whose namespace OID the catalog does not name is
reported under the never-allowed name `oid:<oid>`, so an unclassifiable row fails closed;
the whole read stays behind the same trusted-environment and freshness gate as the rest
of the observation (F1/F3). All catalog reads are qualified and predicate-free and every
decision is made in Python (see below). The subscription triple is the one fact that is
not in that statement: it is cluster-level, so it is read separately from the maintenance
session and filtered to this database's OID in Python.

Only the connection actually used for this observation counts as an authorized session;
any other client backend on the database - including a second maintenance-login session
- is an "unauthorized session" of row 5. Autovacuum and background workers are not
sessions and are ignored. The maintenance connection must be the bootstrap superuser
from an allowed maintenance database (BOOT-01 Step 1, ENR-22): refuse rather than
classify from a vantage that cannot see the whole cluster.

The observation is trusted, and it is fresh
-------------------------------------------
The two Codex R1 BLOCKERs found here were name resolution and staleness. Both are
answered by construction, not by after-the-fact assertion:

* **Name resolution (F1).** Every observation statement names its relations and
  functions with an explicit `pg_catalog.` qualifier, carries no `WHERE`/`JOIN` predicate
  and performs no cast, so a session's `search_path` cannot make an application-controlled
  relation, view, function, operator or type participate in the observation. Every row is
  fetched and every decision is made in Python. `observe()` additionally refuses to read
  anything from a session whose effective schema path does not put `pg_catalog` first
  (`current_schemas(true)`): with `search_path = public, pg_catalog` even the `=` operator
  is user-definable, and PostgreSQL 16 was measured doing exactly that. That refusal is
  `FAIL_UNTRUSTED_ENVIRONMENT`, and the environment is never "fixed" by a `SET`.
* **Freshness (F3).** The freshness contract is FRESH-1..FRESH-4 below. A verdict that
  cannot be shown fresh is never an authorizing one.

FRESH-1 - both the maintenance and the target session must be in `read committed` (or its
   accepted spelling `read uncommitted`), the only isolated levels where each statement
   takes a new snapshot. Under `repeatable read`/`serializable` a table committed after
   the caller's first statement is invisible (measured on PostgreSQL 16), so the catalog
   read cannot authorize anything: `FAIL_STALE_OBSERVATION`.
FRESH-2 - the session inventory is read after `pg_catalog.pg_stat_clear_snapshot()`,
   because the statistics snapshot is otherwise cached for the whole caller transaction
   and a second maintenance connection that appears inside it stays invisible (measured
   on PostgreSQL 16). The call discards a session-local read cache; it changes no setting,
   no data and no transaction state.
FRESH-3 - an authorizing verdict that read the target catalog requires positive evidence
   that the read was live: the supplied probe session must appear in the inventory as the
   grantee's client backend. If it does not, the inventory is not trustworthy.
FRESH-4 - the observation issues no `BEGIN`/`COMMIT`/`ROLLBACK`/`SET`. The caller's
   transaction, isolation level and settings are left exactly as they were; a caller in an
   unsupported state is refused, never adjusted.

The E4 verdict is also **self-consistent**: the server's own `has_database_privilege`
readings are compared against the observed ACL under an explicit PostgreSQL 16 privilege
model (`database_privilege`), and a disagreement fails closed. That is what makes an
impossible privilege combination unreachable rather than merely untested, and it is how a
hidden membership that confers `CONNECT` is caught (F4).

Pure versus observed
--------------------
`classify_facts()` is the whole decision rule and is pure: every state, every failure
code and every boundary can be tested with a hand-built `DatabaseFacts`. `observe()` is
the only database code, and `classify()` is their composition. Failure codes are stable
strings and are asserted by the tests; evidence carries non-secret catalog facts only
(names, flags, pids), never a credential.

This module implements classification only. It does not complete hardening, enable
connections, initialize the schema or reconcile the enrollment journal; those belong to
WP-1C-C and the stages that own them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, Tuple

from tests.postgres_sql import (
    ArtifactError,
    MAINTENANCE_DATABASES,
    PROJECT_ROLE_NAMES,
    validate_database_grantee,
)

OWNER_ROLE = "hh_owner"
EXPECTED_ENCODING = "UTF8"
EXPECTED_LOCALE = ("C", "C", "c")  # BOOT-01: UTF8, C/C, libc provider (WP-1A evidence)

# --- the five states and the one action each allows ------------------------------------------

E4_ABSENT = "absent"
E4_DISABLED_UNHARDENED = "disabled_unhardened"
E4_DISABLED_HARDENED = "disabled_hardened"
E4_ENABLED_MAINTENANCE_ONLY = "enabled_maintenance_only"
E4_FAIL_CLOSED = "fail_closed"

ACTION_CREATE_DATABASE = "create_database"
ACTION_HARDEN_DATABASE_ACL = "harden_database_acl"
ACTION_ENABLE_CONNECTIONS = "enable_connections"
ACTION_VERIFY_TARGET_OBJECTS = "verify_target_objects"
ACTION_INITIALIZE_SCHEMA = "initialize_schema"
ACTION_REMEDIATE = "remediate"

E4_STATES = (
    E4_ABSENT,
    E4_DISABLED_UNHARDENED,
    E4_DISABLED_HARDENED,
    E4_ENABLED_MAINTENANCE_ONLY,
    E4_FAIL_CLOSED,
)
E4_ACTIONS = (
    ACTION_CREATE_DATABASE,
    ACTION_HARDEN_DATABASE_ACL,
    ACTION_ENABLE_CONNECTIONS,
    ACTION_VERIFY_TARGET_OBJECTS,
    ACTION_INITIALIZE_SCHEMA,
    ACTION_REMEDIATE,
)

# --- stable failure codes --------------------------------------------------------------------

FAIL_OWNERSHIP = "ownership_conflict"
FAIL_ENCODING = "encoding_mismatch"
FAIL_LOCALE = "locale_mismatch"
FAIL_PUBLIC_ACCESS = "unexpected_public_access"
FAIL_UNEXPECTED_GRANTS = "unexpected_database_grants"
FAIL_ACL_NOT_HARDENED = "database_acl_not_hardened"
FAIL_ACL_UNRECOGNIZED = "unrecognized_database_acl"
FAIL_SESSIONS = "unauthorized_sessions"
FAIL_TABLES = "unexpected_tables"
FAIL_SUBSCRIPTIONS = "unexpected_subscription"
FAIL_CONNECTION_STATE = "unrecognized_connection_state"
# Codex R1 F1/F3/F4: an observation that cannot be trusted is not a verdict about the
# database. The three codes are appended after the original set so the established order
# of the earlier codes is unchanged for every reader that pinned it.
FAIL_UNTRUSTED_ENVIRONMENT = "untrusted_observation_environment"
FAIL_STALE_OBSERVATION = "stale_observation"
FAIL_INCONSISTENT_PRIVILEGE = "inconsistent_privilege_observation"

# Deterministic order: the verdict lists every failure it observed, in this order, so a
# test and an operator read the same sequence twice.
FAILURE_ORDER = (
    FAIL_OWNERSHIP,
    FAIL_ENCODING,
    FAIL_LOCALE,
    FAIL_PUBLIC_ACCESS,
    FAIL_UNEXPECTED_GRANTS,
    FAIL_ACL_NOT_HARDENED,
    FAIL_ACL_UNRECOGNIZED,
    FAIL_SESSIONS,
    FAIL_TABLES,
    FAIL_SUBSCRIPTIONS,
    FAIL_CONNECTION_STATE,
    FAIL_UNTRUSTED_ENVIRONMENT,
    FAIL_STALE_OBSERVATION,
    FAIL_INCONSISTENT_PRIVILEGE,
)

# The isolation levels whose statements each take their own snapshot (FRESH-1). PostgreSQL
# accepts `read uncommitted` and treats it as `read committed`; it reports the spelling it
# was given, so both are accepted.
FRESH_ISOLATION = ("read committed", "read uncommitted")

# `PUBLIC` may hold exactly the built-in default pair while connections are disabled
# (FE-1); the grantee may hold exactly `CONNECT`.
_DEFAULT_PUBLIC = frozenset({("CONNECT", False), ("TEMPORARY", False)})
# The implicit ACL of a NULL `datacl` (`acldefault(OBJECT_DATABASE, datdba)`): the owner
# holds CREATE/CONNECT/TEMPORARY, EVERY role holds CONNECT/TEMPORARY through PUBLIC.
_DEFAULT_ACL = (
    (OWNER_ROLE, "CONNECT", False),
    (OWNER_ROLE, "CREATE", False),
    (OWNER_ROLE, "TEMPORARY", False),
    ("PUBLIC", "CONNECT", False),
    ("PUBLIC", "TEMPORARY", False),
)
_DEFAULT_GRANTS = frozenset({"CONNECT", "TEMPORARY"})
# `pg_class.h`'s FirstNormalObjectId. Every object `initdb` creates has an OID below this
# value; every object created afterwards - by `hh_owner`, by the bootstrap superuser or by
# an extension - is allocated one at or above it. That boundary is what makes the
# pre-initialization inventory a baseline difference instead of a blacklist of classes.
FIRST_NORMAL_OBJECT_ID = 16384

# The inventory classes `initdb` leaves EMPTY (measured on the pinned 16.15 image: a
# pristine `template0` database has zero rows in each). They have no intrinsic instance a
# threshold could be a difference from, and a caller can name the identifier explicitly -
# `lo_create(1000)` really does create a large object with OID 1000 - so any row in one of
# them is created work whatever its OID.
_EMPTY_BASELINE_CLASSES = frozenset(
    {
        "large object",
        "publication",
        "event trigger",
        "foreign data wrapper",
        "foreign server",
        "default acl",
        "transform",
    }
)

# The only namespaces PostgreSQL creates for a session and its storage. Neither is an
# "unexpected schema" in its own right (the server owns it), and the `pg_` prefix is
# reserved, so an application cannot forge one. Objects *inside* them are still reported.
_SESSION_SCHEMA_PREFIXES = ("pg_temp_", "pg_toast_temp_")


# --- facts and verdict ----------------------------------------------------------------------


@dataclass(frozen=True)
class TargetInventory:
    """Every object of the target database that is not PostgreSQL's intrinsic baseline.

    ENR-10 defines "recognized partial state" as a SUBSET of the authorized inventory and
    nothing outside it. Before BOOT-04 has run, the authorized inventory is empty: the
    only authorized content is what `CREATE DATABASE ... TEMPLATE template0` produces
    (every object of it has an OID below `FIRST_NORMAL_OBJECT_ID`, and the classes
    `_EMPTY_BASELINE_CLASSES` names hold nothing at all). An empty inventory is therefore
    the only clean one, and it is distinct from `None` ("the catalog was not read").

    `objects` holds one `(class, schema, name)` triple per reported object, sorted:
    `class` is `schema` or the catalog class (`relation`, `routine`, `type`, `collation`,
    `conversion`, `operator`, `operator class`, `operator family`, `access method`,
    `cast`, `text search configuration`, `text search dictionary`, `text search parser`,
    `text search template`, `extension`, `publication`, `event trigger`,
    `foreign data wrapper`, `foreign server`, `large object`, `language`, `default acl`,
    `transform`); `schema` is the namespace name or `""` for a database-level class; and
    `name` is the object's name, or `oid:<oid>` for a class whose object has no name of
    its own (a cast, a large object, a default ACL, a transform). The kind letter of a
    relation/routine/type (`relkind`, `prokind`, `typtype`) is not part of the evidence:
    the class plus the name is what identifies an object outside the baseline."""

    objects: Tuple[Tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        # Sorted by construction: an operator reading the evidence, a test asserting it
        # and a verdict produced from the same objects see the same order.
        object.__setattr__(self, "objects", tuple(sorted(self.objects)))

    @property
    def entries(self) -> Tuple[Tuple[str, str, str], ...]:
        """Flattened `(class, schema, name)` evidence; retained for WP-1C-C."""
        return self.objects

    def __bool__(self) -> bool:
        return bool(self.objects)


@dataclass(frozen=True)
class DatabaseFacts:
    """Everything E4 classifies, as observed (never as assumed).

    `exists=False` makes every other field meaningless. `acl_entries` holds the EXPLICIT
    entries of a materialized `datacl` as `(grantee-or-PUBLIC, privilege, grantable)`;
    when `acl_materialized` is false the catalog default applies instead (owner all,
    `PUBLIC` `CONNECT`/`TEMPORARY`) and that default is reconstructed by the classifier.
    `connect` and `temporary` are the EFFECTIVE `has_database_privilege` results per
    project role, because `PUBLIC` grants are included in them; they are cross-checked
    against `acl_entries` and a disagreement fails closed. `sessions` is every client
    backend on the database as `(pid, usename)`; `probe_pid` names the one connection the
    caller supplied for this observation, which is the only authorized one.
    `target_inventory` is `None` when the catalog was not read (a disabled database, or a
    target connection that is not the grantee's) and otherwise every object of the
    database that is not PostgreSQL's own. `subscriptions` holds the credential-free
    `(oid, subname)` of every subscription whose `subdbid` is this database; it is read
    from the maintenance session, so it is available even while no connection to the
    database is permitted.

    `observation` carries `(failure code, non-secret reason)` pairs for an observation
    whose environment or snapshot could not be trusted (F1/F3); any pair makes the verdict
    fail closed whatever the other facts say, and no catalog object is read from such a
    session in the first place."""

    exists: bool
    owner: Optional[str] = None
    encoding: Optional[str] = None
    collate: Optional[str] = None
    ctype: Optional[str] = None
    provider: Optional[str] = None
    allowconn: Optional[bool] = None
    acl_materialized: bool = False
    acl_entries: Tuple[Tuple[str, str, bool], ...] = ()
    connect: Mapping[str, bool] = field(default_factory=dict)
    temporary: Mapping[str, bool] = field(default_factory=dict)
    sessions: Tuple[Tuple[int, str], ...] = ()
    probe_pid: Optional[int] = None
    target_inventory: Optional[TargetInventory] = None
    subscriptions: Tuple[Tuple[int, str], ...] = ()
    observation: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class E4Verdict:
    """The classification, the one allowed action, and the observed evidence."""

    state: str
    action: str
    database: str
    grantee: str
    failures: Tuple[str, ...] = ()
    tables_checked: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fail_closed(self) -> bool:
        return self.state == E4_FAIL_CLOSED

    @property
    def may_initialize_schema(self) -> bool:
        """The ONLY proceed gate: an enabled, hardened, maintenance-only database whose
        catalog was read and holds no object that is not PostgreSQL's own. WP-1C-C must
        not initialize on any other verdict - in particular not on
        `verify_target_objects`, and never when `observation` reported a problem."""
        return self.action == ACTION_INITIALIZE_SCHEMA


# --- the pure privilege model ---------------------------------------------------------------


def database_privilege(
    acl_materialized: bool,
    acl_entries: Tuple[Tuple[str, str, bool], ...],
    role: str,
    privilege: str,
) -> bool:
    """The effective database privilege of `role`, from the ACL alone (PostgreSQL 16).

    Measured against the server (tests/test_postgres_e4.py::TestPrivilegeModel):

    * `datacl IS NULL` means the catalog default applies: the owner holds
      CREATE/CONNECT/TEMPORARY and `PUBLIC` holds CONNECT/TEMPORARY, so every role holds
      CONNECT and TEMPORARY and the owner holds CREATE too.
    * A materialized ACL grants exactly what its entries say. A database OWNER has NO
      implicit privilege: PostgreSQL 16 answers `has_database_privilege(<owner>,
      ..., 'CONNECT')` with false once the owner's entry is gone and no `PUBLIC` entry
      covers it. That is the fact Codex R1 F4 turned into a lost-`CONNECT` database, and
      it is why the convergence check below projects the hardening instead of assuming
      the owner keeps its rights.
    * Membership is NOT modelled. The frozen matrix's two edges are `set_option` only,
      every project role is NOINHERIT, and `has_database_privilege` therefore inherits
      nothing from them; an unexpected membership that does confer a privilege makes the
      server disagree with this model, which fails closed (F4)."""
    if not acl_materialized:
        return role == OWNER_ROLE or privilege in _DEFAULT_GRANTS
    return any(
        grantee in (role, "PUBLIC") and name == privilege
        for grantee, name, _grantable in acl_entries
    )


# --- the pure decision rule ------------------------------------------------------------------


def _ordered(codes: Tuple[str, ...]) -> Tuple[str, ...]:
    seen = set(codes)
    return tuple(code for code in FAILURE_ORDER if code in seen)


def _public_entries(facts: DatabaseFacts) -> Tuple[Tuple[str, bool], ...]:
    """The effective `PUBLIC` database privileges. A NULL `datacl` is the catalog default:
    `PUBLIC` holds `CONNECT` and `TEMPORARY`, and that is exactly the state FE-1 calls a
    recognized intermediate state while, and only while, connections are disabled."""
    if not facts.acl_materialized:
        return tuple(sorted(_DEFAULT_PUBLIC))
    return tuple(
        (privilege, grantable)
        for grantee, privilege, grantable in facts.acl_entries
        if grantee == "PUBLIC"
    )


def _grantee_entries(facts: DatabaseFacts, grantee: str) -> Tuple[Tuple[str, bool], ...]:
    return tuple(
        (privilege, grantable)
        for named, privilege, grantable in facts.acl_entries
        if named == grantee
    )


def _foreign_entries(facts: DatabaseFacts, grantee: str) -> Tuple[Tuple[str, str, bool], ...]:
    """Explicit ACL entries naming anyone but the owner, the grantee or `PUBLIC`. None of
    them is removable by the E3 hardening (`REVOKE` `PUBLIC` + `GRANT` the grantee), so
    none of them is a recognized intermediate state."""
    return tuple(
        entry
        for entry in facts.acl_entries
        if entry[0] not in (OWNER_ROLE, grantee, "PUBLIC")
    )


def _connect_is_exact(facts: DatabaseFacts, grantee: str) -> bool:
    """E3-b/E3-c: effective `CONNECT` exactly for the owner and the one maintenance login,
    and for no other project role."""
    if facts.connect.get(OWNER_ROLE) is not True or facts.connect.get(grantee) is not True:
        return False
    return all(
        facts.connect.get(role) is False
        for role in PROJECT_ROLE_NAMES
        if role not in (OWNER_ROLE, grantee)
    )


def _acl_hardened(facts: DatabaseFacts, grantee: str) -> bool:
    """Exactly the E3-b/E3-c ACL acceptance state (BOOT-01 Steps 2-4): materialized, no
    `PUBLIC` entry, `CONNECT` exactly for the owner and the grantee, no grant option and
    no other privilege for the grantee, and no `TEMPORARY` for the grantee."""
    if not facts.acl_materialized:
        return False
    if _public_entries(facts):
        return False
    if _foreign_entries(facts, grantee):
        return False
    if set(_grantee_entries(facts, grantee)) != {("CONNECT", False)}:
        return False
    if not _connect_is_exact(facts, grantee):
        return False
    return facts.temporary.get(grantee) is False


def _hardening_projection(facts: DatabaseFacts, grantee: str) -> Tuple[Tuple[str, str, bool], ...]:
    """The ACL the approved E3-b operation produces from the observed one.

    That operation is exactly `REVOKE ALL ON DATABASE <db> FROM PUBLIC` followed by
    `GRANT CONNECT ON DATABASE <db> TO <grantee>` (BOOT-01 Steps 2-3): every `PUBLIC` entry
    disappears, the grantee ends up with a plain non-grantable `CONNECT`, and every other
    entry - including the owner's - is left exactly as it was. A NULL `datacl` is first
    materialized by the `REVOKE`, which writes the owner default entries with it."""
    entries = _DEFAULT_ACL if not facts.acl_materialized else tuple(facts.acl_entries)
    kept = tuple(
        entry
        for entry in entries
        if entry[0] != "PUBLIC" and entry != (grantee, "CONNECT", False)
    )
    return kept + ((grantee, "CONNECT", False),)


def _projected_facts(facts: DatabaseFacts, grantee: str) -> DatabaseFacts:
    """A `DatabaseFacts` holding the projected ACL and the privileges that model implies."""
    entries = _hardening_projection(facts, grantee)
    return replace(
        facts,
        exists=True,
        acl_materialized=True,
        acl_entries=entries,
        connect={
            role: database_privilege(True, entries, role, "CONNECT")
            for role in PROJECT_ROLE_NAMES
        },
        temporary={
            role: database_privilege(True, entries, role, "TEMPORARY")
            for role in PROJECT_ROLE_NAMES
        },
        sessions=(),
        probe_pid=None,
        target_inventory=None,
        subscriptions=(),
        observation=(),
    )


def _acl_convergent(facts: DatabaseFacts, grantee: str) -> bool:
    """A disabled ACL that the approved E3 hardening PROVABLY converges to E3-b - and
    nothing beyond that.

    The accepted rules stay: no explicit grant outside {owner, grantee, `PUBLIC`-defaults}
    (the hardening never removes one), no `PUBLIC` privilege outside the built-in default
    pair (FE-1 tolerates the default pair only, and an unexpected `PUBLIC` privilege is a
    row-5 condition), and no grantee privilege other than a plain `CONNECT`.

    The convergence itself is then decided on the projection, not on an assumption: the
    projected ACL must BE the E3-b acceptance state. The case Codex R1 F4 demonstrated -
    a disabled database whose owner `CONNECT` comes only from `PUBLIC`, so the prescribed
    revoke takes it away - fails here, because the projection has no owner entry the
    revoke can leave behind."""
    if _foreign_entries(facts, grantee):
        return False
    if any(entry not in _DEFAULT_PUBLIC for entry in _public_entries(facts)):
        return False
    if any(entry != ("CONNECT", False) for entry in _grantee_entries(facts, grantee)):
        return False
    return _acl_hardened(_projected_facts(facts, grantee), grantee)


def _unauthorized_sessions(facts: DatabaseFacts, grantee: str) -> Tuple[Tuple[int, str], ...]:
    """Row 5: any client backend on the database other than the one connection the caller
    supplied for this observation. That connection must itself be the grantee's session;
    a second maintenance-login session, an application session or a superuser session all
    fail closed."""
    authorized = facts.probe_pid
    return tuple(
        (pid, usename)
        for pid, usename in facts.sessions
        if not (authorized is not None and pid == authorized and usename == grantee)
    )


def _acl_failures(facts: DatabaseFacts, grantee: str) -> Tuple[str, ...]:
    """The ACL half of row 5, with the two arity-appropriate readings of BOOT-07.

    While DISABLED, an ACL is either convergent (row 2), hardened (row 3), or it is an
    unrecognized state that fails closed. While ENABLED, only the hardened ACL is
    accepted; "`datallowconn = true` while the ACL is not hardened" is fail-closed and is
    never repaired forward (FE-1/ROL-23)."""
    public = _public_entries(facts)
    foreign = _foreign_entries(facts, grantee)
    grantee_entries = set(_grantee_entries(facts, grantee))
    public_default_only = all(entry in _DEFAULT_PUBLIC for entry in public)
    grantee_convergent = grantee_entries <= {("CONNECT", False)}

    if facts.allowconn is False:
        if _acl_hardened(facts, grantee) or _acl_convergent(facts, grantee):
            return ()
        codes = []
        if not public_default_only:
            codes.append(FAIL_PUBLIC_ACCESS)
        if foreign:
            codes.append(FAIL_UNEXPECTED_GRANTS)
        codes.append(FAIL_ACL_UNRECOGNIZED)
        return tuple(codes)

    if facts.allowconn is True:
        if _acl_hardened(facts, grantee):
            return ()
        codes = [FAIL_ACL_NOT_HARDENED]
        if public:
            codes.append(FAIL_PUBLIC_ACCESS)
        if foreign:
            codes.append(FAIL_UNEXPECTED_GRANTS)
        if not grantee_convergent:
            codes.append(FAIL_ACL_UNRECOGNIZED)
        return tuple(codes)

    return ()


def _observation_problems(
    facts: DatabaseFacts, grantee: str
) -> Tuple[Tuple[str, str], ...]:
    """Everything that makes these facts unusable as the basis of an authorizing verdict.

    `facts.observation` already holds what `observe()` found about the session
    environment and its snapshot (F1/F3). Two more conditions are properties of the facts
    themselves and are therefore checked here, where they are testable without a server:

    * the probe session must be visible in the session inventory (FRESH-3). A live
      session that the statistics read does not show means the read was not live;
    * the server's effective `CONNECT`/`TEMPORARY` readings must agree with the observed
      ACL under `database_privilege` (F4). A disagreement is an ACL the classifier cannot
      explain - an unexpected membership, a resolver the observer did not account for - and
      it is never allowed to authorize anything. The readings are only checked when they
      were taken at all (an early fail-closed observation takes none)."""
    problems = list(facts.observation)
    if facts.probe_pid is not None and not any(
        pid == facts.probe_pid and usename == grantee
        for pid, usename in facts.sessions
    ):
        problems.append(
            (FAIL_STALE_OBSERVATION, "the supplied probe session is not in the session inventory")
        )
    # A complete observation carries one effective-privilege reading per project role. No
    # reading at all is only possible on the early fail-closed path above, whose facts were
    # never read; a partial or contradictory set is never allowed to authorize anything.
    if facts.exists and not facts.observation:
        for privilege, observed in (
            ("CONNECT", facts.connect),
            ("TEMPORARY", facts.temporary),
        ):
            for role in PROJECT_ROLE_NAMES:
                value = observed.get(role)
                if not isinstance(value, bool):
                    problems.append(
                        (FAIL_INCONSISTENT_PRIVILEGE, f"the effective {privilege} of {role} was not observed")
                    )
                elif value is not database_privilege(
                    facts.acl_materialized, tuple(facts.acl_entries), role, privilege
                ):
                    problems.append(
                        (
                            FAIL_INCONSISTENT_PRIVILEGE,
                            f"the effective {privilege} of {role} disagrees with the database ACL",
                        )
                    )
    return tuple(problems)


def classify_facts(facts: DatabaseFacts, *, database: str, grantee: str) -> E4Verdict:
    """The whole E4 decision rule, pure and deterministic.

    `database`/`grantee` are re-validated against the WP-1B binding (a serving database
    takes `hh_migrate`, a restore database takes `hh_restore`, and nothing else); the
    classifier can therefore never be asked to expect the wrong security state."""
    name, role = validate_database_grantee(database, grantee)

    def verdict(
        state: str,
        action: str,
        failures: Tuple[str, ...] = (),
        observation: Tuple[Tuple[str, str], ...] = (),
    ) -> E4Verdict:
        inventory = facts.target_inventory
        return E4Verdict(
            state=state,
            action=action,
            database=name,
            grantee=role,
            failures=_ordered(failures),
            tables_checked=inventory is not None,
            evidence={
                "owner": facts.owner,
                "encoding": facts.encoding,
                "collate": facts.collate,
                "ctype": facts.ctype,
                "provider": facts.provider,
                "allowconn": facts.allowconn,
                "acl_materialized": facts.acl_materialized,
                "acl_entries": facts.acl_entries,
                "public_entries": _public_entries(facts),
                "connect": dict(facts.connect),
                "temporary": dict(facts.temporary),
                "sessions": facts.sessions,
                "probe_pid": facts.probe_pid,
                "target_inventory": (None if inventory is None else inventory.entries),
                "subscriptions": facts.subscriptions,
                "observation": observation,
            },
        )

    # F1/F3/F4: an observation that cannot be trusted is not a verdict about the database.
    problems = _observation_problems(facts, role)
    if problems:
        return verdict(
            E4_FAIL_CLOSED, ACTION_REMEDIATE, tuple(code for code, _ in problems), problems
        )

    if not facts.exists:
        return verdict(E4_ABSENT, ACTION_CREATE_DATABASE)

    failures = []
    if facts.owner != OWNER_ROLE:
        failures.append(FAIL_OWNERSHIP)
    if facts.encoding != EXPECTED_ENCODING:
        failures.append(FAIL_ENCODING)
    if (facts.collate, facts.ctype, facts.provider) != EXPECTED_LOCALE:
        failures.append(FAIL_LOCALE)
    if _unauthorized_sessions(facts, role):
        failures.append(FAIL_SESSIONS)
    failures.extend(_acl_failures(facts, role))
    if facts.allowconn not in (True, False):
        failures.append(FAIL_CONNECTION_STATE)
    if facts.target_inventory:
        failures.append(FAIL_TABLES)
    if facts.subscriptions:
        failures.append(FAIL_SUBSCRIPTIONS)

    if failures:
        return verdict(E4_FAIL_CLOSED, ACTION_REMEDIATE, tuple(failures))

    if facts.allowconn is False:
        state = (
            E4_DISABLED_HARDENED
            if _acl_hardened(facts, role)
            else E4_DISABLED_UNHARDENED
        )
        action = (
            ACTION_ENABLE_CONNECTIONS
            if state == E4_DISABLED_HARDENED
            else ACTION_HARDEN_DATABASE_ACL
        )
        return verdict(state, action)

    # Enabled, hardened and maintenance-only: the maintenance half of E4 is satisfied.
    if facts.target_inventory is None:
        return verdict(E4_ENABLED_MAINTENANCE_ONLY, ACTION_VERIFY_TARGET_OBJECTS)
    return verdict(E4_ENABLED_MAINTENANCE_ONLY, ACTION_INITIALIZE_SCHEMA)


# --- the observation ------------------------------------------------------------------------
#
# Every statement below names its relations and functions with an explicit `pg_catalog.`
# qualifier, carries no `WHERE`/`JOIN` predicate over application-visible names and
# performs no cast, so no application-controlled object can take part in the observation
# (F1). Rows are fetched whole and every decision is made in Python. The audit test
# `test_every_observation_statement_is_qualified_and_predicate_free` reads this tuple, so a
# new statement cannot be added without passing it.

_TRANSACTION = (
    "SELECT pg_catalog.current_setting('transaction_isolation'), "
    "pg_catalog.current_setting('search_path'), "
    "pg_catalog.current_schemas(true)"
)
_MAINTENANCE_IDENTITY = (
    "SELECT pg_catalog.current_database(), pg_catalog.current_setting('is_superuser')"
)
_CLEAR_STATISTICS = "SELECT pg_catalog.pg_stat_clear_snapshot()"
_CURRENT_DATABASE = "SELECT pg_catalog.current_database()"
_BACKEND_PID = "SELECT pg_catalog.pg_backend_pid()"
# `session_user` and `current_user` are SQL keywords, not catalog names: there is nothing
# to qualify and nothing an application can shadow. This is the one observation statement
# that names no catalog object at all.
_SESSION_IDENTITY = "SELECT session_user, current_user"
_DATABASES = (
    "SELECT d.datname, d.datallowconn, d.datdba, "
    "pg_catalog.pg_encoding_to_char(d.encoding), d.datcollate, d.datctype, "
    "d.datlocprovider, d.datacl IS NOT NULL, d.oid FROM pg_catalog.pg_database d"
)
# Every subscription of the cluster, as the credential-free triple only: the name, the
# OID and the database it replicates into. `subconninfo` is never selected. The catalog
# has no `PUBLIC` `SELECT` (it holds that connection string), which is why this is read
# from the maintenance session - the one session the observation already requires to be
# the bootstrap superuser - and never through the grantee's target connection.
_SUBSCRIPTIONS = (
    "SELECT s.oid, s.subname, s.subdbid FROM pg_catalog.pg_subscription s"
)
_DATABASE_ACLS = (
    "SELECT d.datname, a.grantee, a.privilege_type, a.is_grantable "
    "FROM pg_catalog.pg_database d CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) a"
)
_ROLES = "SELECT r.oid, r.rolname FROM pg_catalog.pg_roles r"
# Row 5's "unauthorized sessions": client backends only. Autovacuum and background
# workers are not sessions and must not make a clean database look occupied.
_SESSIONS = (
    "SELECT a.datname, a.pid, a.usename, a.backend_type FROM pg_catalog.pg_stat_activity a"
)
# The pre-initialization inventory in ONE statement: one read-committed snapshot, so a
# table committed between two catalog scans cannot be half-seen. Every row is
# (class, namespace-oid, name, oid, typtype/relkind/prokind, typrelid, typelem); the
# class-specific columns are NULL in the branches that do not use them. The type branch
# is first so the UNION's oid/char columns are typed before the NULL-only branches. Every
# name is qualified and no branch carries a predicate, cast or operator (F1); the
# namespace OIDs are resolved to names in Python from the `schema` rows of the same
# snapshot.
_INVENTORY = " UNION ALL ".join(
    (
        # `all` is named in OBSERVATION_STATEMENTS via this joined text; each branch keeps
        # its catalog explicitly qualified.
        "SELECT 'type', t.typnamespace, t.typname, t.oid, t.typtype, t.typrelid, t.typelem "
        "FROM pg_catalog.pg_type t",
        "SELECT 'schema', NULL, n.nspname, n.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_namespace n",
        "SELECT 'relation', c.relnamespace, c.relname, c.oid, c.relkind, NULL, NULL "
        "FROM pg_catalog.pg_class c",
        "SELECT 'routine', p.pronamespace, p.proname, p.oid, p.prokind, NULL, NULL "
        "FROM pg_catalog.pg_proc p",
        "SELECT 'collation', c.collnamespace, c.collname, c.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_collation c",
        "SELECT 'conversion', c.connamespace, c.conname, c.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_conversion c",
        "SELECT 'operator', o.oprnamespace, o.oprname, o.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_operator o",
        "SELECT 'operator class', c.opcnamespace, c.opcname, c.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_opclass c",
        "SELECT 'operator family', f.opfnamespace, f.opfname, f.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_opfamily f",
        "SELECT 'access method', NULL, a.amname, a.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_am a",
        "SELECT 'cast', NULL, NULL, c.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_cast c",
        "SELECT 'text search configuration', c.cfgnamespace, c.cfgname, c.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_ts_config c",
        "SELECT 'text search dictionary', d.dictnamespace, d.dictname, d.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_ts_dict d",
        "SELECT 'text search parser', p.prsnamespace, p.prsname, p.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_ts_parser p",
        "SELECT 'text search template', t.tmplnamespace, t.tmplname, t.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_ts_template t",
        "SELECT 'extension', NULL, e.extname, e.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_extension e",
        "SELECT 'publication', NULL, p.pubname, p.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_publication p",
        "SELECT 'event trigger', NULL, e.evtname, e.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_event_trigger e",
        "SELECT 'foreign data wrapper', NULL, f.fdwname, f.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_foreign_data_wrapper f",
        "SELECT 'foreign server', NULL, s.srvname, s.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_foreign_server s",
        "SELECT 'large object', NULL, NULL, m.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_largeobject_metadata m",
        "SELECT 'language', NULL, l.lanname, l.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_language l",
        "SELECT 'default acl', NULL, NULL, d.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_default_acl d",
        "SELECT 'transform', NULL, NULL, t.oid, NULL, NULL, NULL "
        "FROM pg_catalog.pg_transform t",
    )
)
_HAS_CONNECT = "SELECT pg_catalog.has_database_privilege(%s, %s, 'CONNECT')"
_HAS_TEMPORARY = "SELECT pg_catalog.has_database_privilege(%s, %s, 'TEMPORARY')"

OBSERVATION_STATEMENTS = (
    _TRANSACTION,
    _MAINTENANCE_IDENTITY,
    _CLEAR_STATISTICS,
    _CURRENT_DATABASE,
    _BACKEND_PID,
    _SESSION_IDENTITY,
    _DATABASES,
    _DATABASE_ACLS,
    _SUBSCRIPTIONS,
    _ROLES,
    _SESSIONS,
    _INVENTORY,
    _HAS_CONNECT,
    _HAS_TEMPORARY,
)


def _inventory(connection: Any) -> TargetInventory:
    """Every object of the target database that is not PostgreSQL's intrinsic baseline
    (BOOT-04/BOOT-09, ENR-10). Read from the target connection only - a PostgreSQL catalog
    is per-database - and in one statement, so the result is one snapshot.

    An object of a class `initdb` populates is outside the baseline exactly when its OID
    is at or above `FIRST_NORMAL_OBJECT_ID` (the boundary `initdb` leaves behind). An
    object of an `_EMPTY_BASELINE_CLASSES` class is outside it whatever its OID, because
    that class has no intrinsic instance and an explicitly assigned OID (a large object's)
    can be below the boundary. The only name-based exceptions are the session namespaces,
    which PostgreSQL itself creates with an OID above the boundary: the namespace is not
    reported as an unexpected schema, but every object inside it is. Nothing is filtered
    by system-schema name, so an object created by the bootstrap superuser in
    `pg_catalog` or `pg_toast` is reported too."""
    rows = connection.execute(_INVENTORY).fetchall()
    namespaces = {
        int(oid): str(name)
        for class_, _schema, name, oid, _kind, _typrelid, _typelem in rows
        if str(class_) == "schema"
    }
    objects = []
    for class_, namespace_oid, name, oid, _kind, typrelid, typelem in rows:
        number = int(oid)
        class_ = str(class_)
        if class_ not in _EMPTY_BASELINE_CLASSES and number < FIRST_NORMAL_OBJECT_ID:
            continue  # an object of the intrinsic baseline
        if class_ == "schema":
            schema_name = str(name)
            if schema_name.startswith(_SESSION_SCHEMA_PREFIXES):
                continue
            # `pg_catalog`, `information_schema`, `pg_toast` and `public` are below the
            # boundary and were already skipped above; a *re-created* `public` has an OID
            # above it and is reported like any other schema work.
            objects.append(("schema", schema_name, schema_name))
            continue
        if class_ == "type" and (int(typrelid or 0) or int(typelem or 0)):
            # A relation's row type, or an array type: derived from an object that is
            # itself reported (the relation, or the element type).
            continue
        if namespace_oid is None:
            schema = ""
        else:
            # A namespace OID the snapshot does not name is reported under a name that is
            # never allowed, so such an object fails closed.
            schema = namespaces.get(int(namespace_oid), f"oid:{int(namespace_oid)}")
        objects.append((class_, schema, "oid:" + str(number) if name is None else str(name)))
    return TargetInventory(objects=tuple(sorted(objects)))


def _current_database(connection: Any) -> str:
    return str(connection.execute(_CURRENT_DATABASE).fetchone()[0])


def _grantee_name(role_names: Mapping[Any, str], grantee: Any) -> str:
    """An ACL grantee: OID 0 is `PUBLIC`; any other OID is the role the catalog names."""
    if int(grantee) == 0:
        return "PUBLIC"
    return str(role_names.get(grantee, grantee))


def _trusted_environment(schemas: Any) -> bool:
    """FRESH/trust: is `pg_catalog` first in the effective schema path?

    `current_schemas(true)` reports the path the server actually searches, including the
    implicit schemas. A temporary namespace may precede `pg_catalog` (PostgreSQL searches
    it first for relations and our statements are qualified anyway); anything else before
    `pg_catalog` means a user schema can define the operators and functions our SQL uses -
    measured on PostgreSQL 16 with `search_path = public, pg_catalog`, where `'a' = 'b'`
    became true."""
    path = [str(schema) for schema in schemas]
    if path and path[0].startswith("pg_temp_"):
        path = path[1:]
    return bool(path) and path[0] == "pg_catalog"


def _environment_problems(connection: Any, where: str) -> Tuple[Tuple[str, str], ...]:
    """The session's freshness and trust contract, read without touching its settings.

    FRESH-1: the isolation level must be one whose statements each take a new snapshot.
    FRESH-4: nothing here writes - both settings are only read."""
    isolation, search_path, schemas = connection.execute(_TRANSACTION).fetchone()
    problems = []
    if str(isolation).lower() not in FRESH_ISOLATION:
        problems.append(
            (FAIL_STALE_OBSERVATION, f"{where} transaction isolation is {isolation}")
        )
    if not _trusted_environment(schemas):
        problems.append(
            (FAIL_UNTRUSTED_ENVIRONMENT, f"{where} search_path is {search_path}")
        )
    return tuple(problems)


def _require_maintenance_connection(control: Any) -> None:
    """BOOT-01 Step 1 / ENR-22: E4 is classified from the local cluster administrator's
    maintenance connection. A session that is not the bootstrap superuser, or that is not
    on an allowed maintenance database, is refused before any fact is read - it cannot
    promise the complete session inventory E4's row 5 depends on. Both values are read by
    `pg_catalog` functions and compared here, so no identifier resolution is involved."""
    row = control.execute(_MAINTENANCE_IDENTITY).fetchone()
    if row is None:
        raise ArtifactError("maintenance_database", "the maintenance connection named no database")
    database, superuser = str(row[0]), str(row[1])
    if database not in MAINTENANCE_DATABASES:
        raise ArtifactError(
            "maintenance_database",
            "E4 classification must run from an allowed maintenance database",
            database,
        )
    if superuser != "on":
        raise ArtifactError(
            "bootstrap_superuser",
            "E4 classification must run as the bootstrap superuser",
        )


def observe(
    control: Any,
    *,
    database: object,
    grantee: object,
    target: Optional[Any] = None,
) -> DatabaseFacts:
    """Read the facts of `database` from the maintenance connection, and - only when
    connections are enabled, the session is trusted and the caller supplied this
    database's own maintenance-login connection - the target catalog.

    Issues no `BEGIN`/`COMMIT`/`SET`: it is safe inside a caller's transaction and leaves
    the session contract untouched (FRESH-4). The only non-read it performs is
    `pg_stat_clear_snapshot()`, which discards this session's cached statistics snapshot
    so the session inventory is live (FRESH-2); it changes no setting and no transaction
    state. A session whose environment or snapshot cannot be trusted is not read at all:
    the facts carry the reason and the verdict fails closed (F1/F3)."""
    name, role = validate_database_grantee(database, grantee)
    _require_maintenance_connection(control)

    observation = _environment_problems(control, "maintenance")
    if observation:
        # Nothing is read from a session where an application object could answer for a
        # catalog object; the verdict only has to fail closed.
        return DatabaseFacts(exists=True, observation=observation)

    role_names = {
        row[0]: str(row[1]) for row in control.execute(_ROLES).fetchall()
    }
    row = next(
        (
            database_row
            for database_row in control.execute(_DATABASES).fetchall()
            if str(database_row[0]) == name
        ),
        None,
    )
    if row is None:
        if target is not None:
            raise ArtifactError(
                "target_not_permitted",
                "no connection may be used for a database that does not exist",
                name,
            )
        return DatabaseFacts(exists=False)

    allowconn = row[1]
    owner = str(role_names.get(row[2], row[2]))
    encoding = str(row[3])
    entries = tuple(
        (_grantee_name(role_names, grantee_oid), str(privilege), bool(grantable))
        for database_name, grantee_oid, privilege, grantable in control.execute(
            _DATABASE_ACLS
        ).fetchall()
        if str(database_name) == name
    )
    connect = {
        role_name: control.execute(_HAS_CONNECT, (role_name, name)).fetchone()[0]
        for role_name in PROJECT_ROLE_NAMES
    }
    temporary = {
        role_name: control.execute(_HAS_TEMPORARY, (role_name, name)).fetchone()[0]
        for role_name in PROJECT_ROLE_NAMES
    }
    # A subscription replicates INTO the database whose OID is its `subdbid`; the catalog
    # is cluster-wide, so the rows of other databases are read and discarded here, and
    # only the credential-free triple was ever selected.
    database_oid = int(row[8])
    subscriptions = tuple(
        sorted(
            (int(subscription_oid), str(subname))
            for subscription_oid, subname, subdbid in control.execute(
                _SUBSCRIPTIONS
            ).fetchall()
            if int(subdbid) == database_oid
        )
    )

    # FRESH-2: the statistics snapshot is cached for the whole caller transaction, so a
    # second backend that connected inside it stays invisible until it is discarded.
    control.execute(_CLEAR_STATISTICS)
    sessions = tuple(
        (int(pid), "" if usename is None else str(usename))
        for datname, pid, usename, backend_type in control.execute(_SESSIONS).fetchall()
        if str(datname) == name and str(backend_type) == "client backend"
    )

    probe_pid: Optional[int] = None
    inventory: Optional[TargetInventory] = None
    if target is not None:
        # BOOT-06 / FE-1: nothing is ever read through a connection while the database
        # is not accepting connections - and such a connection cannot legitimately exist.
        if allowconn is not True:
            raise ArtifactError(
                "target_not_permitted",
                "no connection may be used while the database is not accepting "
                "connections (BOOT-06)",
                name,
            )
        if _current_database(target) != name:
            raise ArtifactError(
                "target_database",
                "the supplied connection is not to the database under classification",
                _current_database(target),
            )
        # FRESH-1/FRESH-3: the target's snapshot must be able to see a commit that
        # happened after the caller's first statement, and its environment must be one
        # where an application object cannot answer for `pg_catalog`.
        target_problems = _environment_problems(target, "target")
        if target_problems:
            observation += target_problems
        else:
            session_user, _current_user = target.execute(_SESSION_IDENTITY).fetchone()
            pid = int(target.execute(_BACKEND_PID).fetchone()[0])
            if session_user == role:
                # Only the one authorized maintenance login may contribute target facts.
                # Any other session is visible in `sessions` and fails row 5 instead.
                probe_pid = pid
                inventory = _inventory(target)

    return DatabaseFacts(
        exists=True,
        owner=owner,
        encoding=encoding,
        collate=str(row[4]),
        ctype=str(row[5]),
        provider=str(row[6]),
        allowconn=allowconn,
        acl_materialized=bool(row[7]),
        acl_entries=entries,
        connect=connect,
        temporary=temporary,
        sessions=sessions,
        probe_pid=probe_pid,
        target_inventory=inventory,
        subscriptions=subscriptions,
        observation=observation,
    )


def classify(
    control: Any,
    *,
    database: object,
    grantee: object,
    target: Optional[Any] = None,
) -> E4Verdict:
    """Observe, then classify. `target` is the caller's own already-open, read-only
    connection to the same database as the maintenance login; it is used only when the
    maintenance facts show connections are enabled and the target session is trusted, and
    only its own catalog is read."""
    name, role = validate_database_grantee(database, grantee)
    return classify_facts(
        observe(control, database=name, grantee=role, target=target),
        database=name,
        grantee=role,
    )
