"""Mode A enrollment, Mode B verification and the E0-E7 resume dispatch (HH-DB-01 WP-2C-A).

Frozen source: §4.1-§4.6 (ENR-01..ENR-22), §7.2 (LCK-06..LCK-09), §4.4 (ENR-13/ENR-14).

The host tool (`deploy/postgres/enroll.py`) owns the Docker and `psql` side. This module is
the decision and orchestration layer and is deliberately free of `subprocess`, sockets and
SQL text construction: the only collaborator it accepts is a `Cluster` adapter.

Authority flow
--------------
Mode A is a one-time trusted enrollment. Before the intent exists, the cluster must be
observed pristine; the intent is then appended BEFORE the first mutation. Every later run
resumes exactly that intent: a different `system_identifier`, an extra object, a role with
drifted attributes or a foreign container/volume/image is a refusal, never an adoption.

Both operations run under the host-side `operation.lock` in the journal directory. It is a
separate lock from the journal append lock and from the worker's state-volume maintenance
lock: the host must not take the worker's non-reentrant flock, and journal appends must
remain possible inside the interval. A migrate whose client times out after launch is
UNKNOWN, never `not_started`; it is resolved only by an observation.

Mode B is the authoritative later check. It requires the completed enrollment record,
compares the recorded container/volume/image/identifier and the live marker, role and
database inventories, and never falls back to Mode A. The host tool calls it immediately
before it writes a migrate authorization; if any item differs, it appends a
`cluster_identity:mismatch` record naming the item and returns a refusal.

The in-container executor re-verifies the database-observable half of the same evidence
(marker, identifier embedded in it, role/database inventory, schema state) before it runs
anything. See `house_helper/persistence/pg_executor.py` and the authority-flow document.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

from house_helper.persistence import pg_contract as contract
from house_helper.persistence.pg_journal import (
    GENESIS,
    Journal,
    JournalError,
    JournalView,
    Record,
    make_marker,
    parse_marker,
    timestamp,
    utcnow,
)

# --- E0-E7 durable states --------------------------------------------------------------------


E0 = "E0"
E1 = "E1"
E2 = "E2"
E3 = "E3"
E4 = "E4"
E5 = "E5"
E6 = "E6"
E7 = "E7"
FAIL = "FAIL"

ENROLLMENT_STATES = (E0, E1, E2, E3, E4, E5, E6, E7, FAIL)
_RANK = {E0: 0, E1: 1, E2: 2, E3: 3, E4: 4, E5: 5, E6: 6, E7: 7}

# The journal's milestone payload states, and the observed state they record.
_RECORD_RANK = {
    "intent": 1,
    "roles": 3,
    "database": 4,
    "hardened": 5,
    "marker": 6,
    "completed": 7,
}

ACTION_BEGIN = "begin_enrollment"
ACTION_ROLES = "create_roles"
ACTION_RESUME_ROLES = "resume_roles"
ACTION_CREATE_DATABASE = "create_database"
ACTION_FINISH_DATABASE = "finish_database_hardening"
# F8: after E3-c, the ENR-04 schema/default-ACL half (the unchanged db/schema.sql) must
# complete before the ENR-05 marker. It is a separate action so an interrupted enrollment
# resumes exactly the outstanding half, never the already-completed database-level one.
ACTION_INITIALIZE_SCHEMA = "initialize_schema"
ACTION_MARKER = "write_marker"
ACTION_COMPLETE = "complete_enrollment"
ACTION_MODE_B = "mode_b_only"
ACTION_REMEDIATE = "remediate"

STATE_ACTIONS = {
    E0: ACTION_BEGIN,
    E1: ACTION_ROLES,
    E2: ACTION_RESUME_ROLES,
    E3: ACTION_CREATE_DATABASE,
    E4: ACTION_FINISH_DATABASE,
    E5: ACTION_MARKER,
    E6: ACTION_COMPLETE,
    E7: ACTION_MODE_B,
    FAIL: ACTION_REMEDIATE,
}


class EnrollmentError(Exception):
    """A fail-closed enrollment/verification refusal. No value is ever a credential."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message if detail is None else f"{message} ({detail})")
        self.code = code
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class ClusterIdentity:
    container_name: str
    container_id: str
    image_digest: str
    volume: str
    system_identifier: str

    def as_journal(self) -> Mapping[str, str]:
        return {
            "container_name": self.container_name,
            "container_id": self.container_id,
            "image_digest": self.image_digest,
            "volume": self.volume,
            "system_identifier": self.system_identifier,
        }

    @classmethod
    def from_journal(cls, value: Mapping[str, Any]) -> "ClusterIdentity":
        try:
            return cls(
                container_name=str(value["container_name"]),
                container_id=str(value["container_id"]),
                image_digest=str(value["image_digest"]),
                volume=str(value["volume"]),
                system_identifier=str(value["system_identifier"]),
            )
        except (KeyError, TypeError):
            raise EnrollmentError("identity_record", "the recorded cluster identity is incomplete") from None


@dataclass(frozen=True)
class Observation:
    """One read-only view of the cluster, gathered through the adapter."""

    identity: ClusterIdentity
    databases: Sequence[str]
    roles: Mapping[str, contract.RoleFacts]
    memberships: Sequence[contract.MembershipEdge]
    predefined_roles: Sequence[str]
    database: Optional[contract.DatabaseFacts]
    marker: str
    target_inventory: Optional[contract.TargetInventory]
    effective_membership: Sequence[tuple[str, str, str, bool]] = ()
    schema_versions: Sequence[str] = ()
    public_tables: Sequence[str] = ()
    default_acls: Sequence[tuple[str, str, str, str, str, bool]] = ()
    # F8: the `public` schema ownership/PUBLIC-count/USAGE+CREATE matrix, observed on the
    # target. None means "not observed", which is never treated as verified.
    schema_acl: Optional[tuple[Any, ...]] = None

    @property
    def predefined_memberships(self) -> tuple[contract.MembershipEdge, ...]:
        return contract.predefined_membership_edges(
            self.memberships, self.predefined_roles
        )


@dataclass(frozen=True)
class Plan:
    state: str
    action: str
    failures: tuple[str, ...] = ()
    recorded_rank: int = 0
    observed_rank: int = 0
    recorded_completed: bool = False
    intent: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class EnrollmentReport:
    state: str
    actions: tuple[str, ...]
    marker: str
    system_identifier: str
    records_written: tuple[int, ...]


@dataclass(frozen=True)
class ModeBVerdict:
    ok: bool
    mismatches: tuple[str, ...]
    completed: Optional[Mapping[str, Any]]
    observed: Observation
    journal_seq: int
    journal_head: str = GENESIS


class Cluster(Protocol):
    """The host-side operations Mode A needs. Production: Docker + local-peer `psql`."""

    def identity(self) -> ClusterIdentity: ...

    def observe(self, *, read_target: bool = False) -> Observation: ...

    def apply_roles(self) -> None: ...

    def create_database(self) -> None: ...

    def harden_database(self) -> None: ...

    def enable_database(self) -> None: ...

    def harden_schema(self, expected: ClusterIdentity) -> None: ...

    def write_marker(self, marker: str) -> None: ...


# --- journal helpers -------------------------------------------------------------------------


def _enrollment_records(records: Iterable[Record]) -> tuple[Record, ...]:
    return tuple(
        record
        for record in records
        if record.kind == "cluster_enrollment"
        and isinstance(record.payload.get("state"), str)
    )


def _recorded_rank(records: Iterable[Record]) -> tuple[int, bool, Optional[Mapping[str, Any]]]:
    rank = 0
    completed = False
    intent: Optional[Mapping[str, Any]] = None
    for record in records:
        if record.kind == "cluster_enrollment":
            state = record.payload.get("state")
            rank = max(rank, _RECORD_RANK.get(str(state), 0))
            if state == "completed":
                completed = True
            if state == "intent":
                intent = record.payload
        elif record.kind == "reconcile":
            observed = record.payload.get("observed_state")
            rank = max(rank, _RECORD_RANK.get(str(observed), 0))
    return rank, completed, intent


def _completed_records(records: Iterable[Record]) -> tuple[Record, ...]:
    return tuple(
        record
        for record in records
        if record.kind == "cluster_enrollment" and record.payload.get("state") == "completed"
    )


# --- §7.3 permission handoffs -----------------------------------------------------------------
#
# HOF-01/ROL-16 make each runtime CONNECT grant a separately authorized handoff, with an
# intent and an observed outcome in the journal. The authorized surface is three handoffs
# covering four logins: the serving app (H1), enrollment (H2) and the scheduled operations
# (H3). H3 covers BOTH scheduled logins - `hh_purge` and `hh_backup` - in the frozen
# specification's order; each role still gets its own single-role GRANT and its own
# intent/outcome pair (HOF-02: one statement per role), and both carry the frozen
# `handoff:scheduled` event. `hh_purge` is appended here for C7-P3, when its timer is
# re-armed; the earlier records for H1, H2 and the H3 `hh_backup` login are untouched.
HANDOFF_ROLES = ("hh_app", "hh_provision", "hh_purge", "hh_backup")
HANDOFF_EVENTS = {
    "hh_app": "app",
    "hh_provision": "provision",
    "hh_purge": "scheduled",
    "hh_backup": "scheduled",
}
# An outcome is a *decision*. `unknown` is deliberately not a value: an unresolved
# operation has no outcome at all (LCK-08), so it cannot be mistaken for a resolution.
HANDOFF_OUTCOMES = frozenset({"observed", "failed", "reconciled"})
_HANDOFF_RUN_ID = re.compile(r"[0-9a-f]{32}")
_HANDOFF_APPLICATION_PREFIX = "hh-handoff-"
_HANDOFF_INTENT_KEYS = frozenset(
    {
        "state",
        "action",
        "handoff",
        "role",
        "database",
        "operator",
        "run_id",
        "application_name",
        "before",
    }
)
_HANDOFF_OUTCOME_KEYS = frozenset(
    {
        "state",
        "action",
        "handoff",
        "role",
        "database",
        "run_id",
        "operator",
        "outcome",
        "evidence",
    }
)

# The complete database-level privilege vocabulary. `CREATE`/`TEMPORARY` are never
# authorized by a handoff; they exist here so a collateral grant is detected rather than
# ignored.
DATABASE_PRIVILEGES = contract.DATABASE_PRIVILEGES
# The enrolled baseline already authorizes exactly these roles to CONNECT: the NOLOGIN
# owner (inherent rights) and the pre-handoff maintenance login (db/bootstrap.sql). Every
# other login's CONNECT must have provenance: a journaled successful handoff.
PRE_HANDOFF_CONNECT_ROLES = frozenset({contract.OWNER_ROLE, "hh_migrate"})

# psql exit codes. With `ON_ERROR_STOP=1` and a script whose only statement is the GRANT,
# 3 is a definitive server-side refusal of that statement; 0 is a completed execution.
# Everything else (connection loss 2, BusyBox `timeout` 143, client/docker failures,
# signals) is
# ambiguous and must never be recorded as "failed".
GRANT_SUCCEEDED = 0
GRANT_SERVER_REFUSED = 3


def handoff_application_name(run_id: str) -> str:
    """The one `application_name` a handoff operation's psql session carries.

    It is derived from the bounded run id, so the journal, the session evidence and the
    container-side markers all name the same operation without storing a secret.
    """
    if not isinstance(run_id, str) or not _HANDOFF_RUN_ID.fullmatch(run_id):
        raise EnrollmentError("handoff_journal", "the handoff run id is malformed")
    return _HANDOFF_APPLICATION_PREFIX + run_id


@dataclass(frozen=True)
class DatabaseAclState:
    """The complete serving-database ACL, exactly as observed.

    `entries` are the explicit `datacl` items as `(grantee, privilege, grantable)`;
    `effective` is the full `has_database_privilege` matrix for the seven project roles
    across the three database privileges. Both the pre-state recorded in an intent and the
    post-state read after an attempt use this one shape, so the comparison is total.
    """

    acl_materialized: bool
    entries: tuple[tuple[str, str, bool], ...] = ()
    effective: tuple[tuple[str, str, bool], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(sorted(self.entries)))
        object.__setattr__(self, "effective", tuple(sorted(self.effective)))

    def privilege(self, role: str, privilege: str) -> Optional[bool]:
        for observed_role, observed_privilege, value in self.effective:
            if observed_role == role and observed_privilege == privilege:
                return value
        return None

    def entries_for(self, grantee: str) -> tuple[tuple[str, str, bool], ...]:
        return tuple(entry for entry in self.entries if entry[0] == grantee)

    def as_journal(self) -> Mapping[str, Any]:
        return {
            "acl_materialized": self.acl_materialized,
            "entries": [list(entry) for entry in self.entries],
            "effective": [list(entry) for entry in self.effective],
        }

    @classmethod
    def from_journal(
        cls, value: Any, *, where: str = ""
    ) -> "DatabaseAclState":
        suffix = f" ({where})" if where else ""
        if not isinstance(value, Mapping) or set(value) != {
            "acl_materialized",
            "entries",
            "effective",
        }:
            raise EnrollmentError(
                "handoff_journal", "the handoff pre-state is malformed" + suffix
            )
        if not isinstance(value["acl_materialized"], bool):
            raise EnrollmentError(
                "handoff_journal", "the handoff pre-state is malformed" + suffix
            )
        for key in ("entries", "effective"):
            if not isinstance(value[key], list):
                raise EnrollmentError(
                    "handoff_journal", "the handoff pre-state is malformed" + suffix
                )
        entries: list[tuple[str, str, bool]] = []
        for item in value["entries"]:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 3
                or not isinstance(item[0], str)
                or not item[0]
                or len(item[0]) > 128
                or item[1] not in DATABASE_PRIVILEGES
                or not isinstance(item[2], bool)
            ):
                raise EnrollmentError(
                    "handoff_journal", "the handoff pre-state is malformed" + suffix
                )
            entries.append((item[0], item[1], item[2]))
        effective: list[tuple[str, str, bool]] = []
        for item in value["effective"]:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 3
                or item[0] not in contract.PROJECT_ROLE_NAMES
                or item[1] not in DATABASE_PRIVILEGES
                or not isinstance(item[2], bool)
            ):
                raise EnrollmentError(
                    "handoff_journal", "the handoff pre-state is malformed" + suffix
                )
            effective.append((item[0], item[1], item[2]))
        pairs = {(role, privilege) for role, privilege, _ in effective}
        expected_pairs = len(contract.PROJECT_ROLE_NAMES) * len(DATABASE_PRIVILEGES)
        if len(effective) != expected_pairs or len(pairs) != expected_pairs:
            raise EnrollmentError(
                "handoff_journal",
                "the handoff pre-state does not cover the full privilege matrix" + suffix,
            )
        return cls(
            acl_materialized=value["acl_materialized"],
            entries=tuple(entries),
            effective=tuple(effective),
        )


_HANDOFF_EVIDENCE_BOOLEANS = (
    "has_database_privilege",
    "has_temporary",
    "has_create",
    "changed",
    "reconciled",
    "applied",
    "privileges_unchanged",
)


def _validate_handoff_evidence(
    payload: Mapping[str, Any], intent: Mapping[str, Any], *, where: str
) -> None:
    """The evidence of one outcome must be structurally and semantically consistent.

    A structurally valid outcome that never had a matching intent, or whose evidence
    contradicts its own recorded pre-state, is journal corruption. Provenance is derived
    only from a validated history: an orphan or self-contradictory `observed` line can
    never authorize a CONNECT.
    """
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        raise EnrollmentError(
            "handoff_journal", "a handoff outcome has no evidence", where
        )
    for key in _HANDOFF_EVIDENCE_BOOLEANS:
        if key in evidence and not isinstance(evidence[key], bool):
            raise EnrollmentError(
                "handoff_journal", "a handoff outcome evidence field is malformed", where
            )
    if "psql_exit" in evidence:
        value = evidence["psql_exit"]
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise EnrollmentError(
                "handoff_journal", "a handoff psql status is malformed", where
            )
    if "items" in evidence and (
        not isinstance(evidence["items"], list)
        or not all(isinstance(item, str) for item in evidence["items"])
    ):
        raise EnrollmentError(
            "handoff_journal", "a handoff evidence item list is malformed", where
        )
    if "code" in evidence and not isinstance(evidence["code"], str):
        raise EnrollmentError(
            "handoff_journal", "a handoff evidence code is malformed", where
        )
    if "transport" in evidence and not isinstance(evidence["transport"], str):
        raise EnrollmentError(
            "handoff_journal", "a handoff transport label is malformed", where
        )

    role = payload["role"]
    outcome = payload["outcome"]
    before = DatabaseAclState.from_journal(intent.get("before"), where=where)
    already_connected = before.privilege(role, "CONNECT") is True

    if "changed" in evidence:
        if evidence["changed"] is True and already_connected:
            raise EnrollmentError(
                "handoff_journal",
                "a handoff outcome claims a change that its pre-state already contained",
                where,
            )
        if evidence["changed"] is False and not already_connected:
            raise EnrollmentError(
                "handoff_journal",
                "a handoff outcome claims no change from an absent privilege",
                where,
            )

    if outcome == "failed":
        if evidence.get("has_database_privilege") is True:
            raise EnrollmentError(
                "handoff_journal",
                "a failed handoff outcome cannot establish database privilege",
                where,
            )
        if not isinstance(evidence.get("code"), str) and not isinstance(
            evidence.get("items"), list
        ):
            raise EnrollmentError(
                "handoff_journal",
                "a failed handoff outcome names neither a code nor its failing items",
                where,
            )
    elif outcome == "observed":
        if "changed" not in evidence or evidence.get("has_database_privilege") is not True:
            raise EnrollmentError(
                "handoff_journal",
                "an observed handoff outcome does not record the observed privilege",
                where,
            )
    elif outcome == "reconciled":
        if evidence.get("reconciled") is not True:
            raise EnrollmentError(
                "handoff_journal",
                "a reconciled handoff outcome is missing its reconciliation evidence",
                where,
            )
        applied = evidence.get("applied") is True
        unchanged = evidence.get("privileges_unchanged") is True
        if applied == unchanged:
            raise EnrollmentError(
                "handoff_journal",
                "a reconciled handoff outcome is neither applied nor unchanged",
                where,
            )
        if applied and already_connected:
            raise EnrollmentError(
                "handoff_journal",
                "a reconciled handoff outcome claims an application over an existing grant",
                where,
            )
        # "Unchanged" means the recorded pre-state survived. For a no-op intent (the role
        # already held CONNECT) that is still True; for a real attempt it is False.
        expected_privilege = True if applied else already_connected
        if evidence.get("has_database_privilege") is not expected_privilege:
            raise EnrollmentError(
                "handoff_journal",
                "a reconciled handoff outcome contradicts its observed privilege",
                where,
            )


def handoff_events(records: Iterable[Record]) -> tuple[Record, ...]:
    """The valid `handoff` records, in journal order, as one paired history.

    Every outcome must follow exactly one intent with the same run id, role, action,
    handoff event and database. Duplicate intents, duplicate outcomes, orphan outcomes and
    cross-role pairings are journal corruption, never a future extension to ignore: an
    outcome that does not pair with a prior intent could otherwise authorize a CONNECT
    that this tool never recorded making.
    """
    found = tuple(record for record in records if record.kind == "handoff")
    intents: dict[str, Record] = {}
    outcomes: dict[str, Record] = {}
    for record in found:
        payload = record.payload
        where = f"seq {record.seq}"
        state = payload.get("state")
        role = payload.get("role")
        if state not in ("intent", "outcome"):
            raise EnrollmentError(
                "handoff_journal", "a handoff record has an unknown state", where
            )
        expected_keys = (
            _HANDOFF_INTENT_KEYS if state == "intent" else _HANDOFF_OUTCOME_KEYS
        )
        if set(payload) != expected_keys:
            raise EnrollmentError(
                "handoff_journal", "a handoff record has unexpected fields", where
            )
        if role not in HANDOFF_ROLES:
            raise EnrollmentError(
                "handoff_journal", "a handoff record names an unauthorized role", where
            )
        if payload.get("database") != contract.SERVING_DATABASE:
            raise EnrollmentError(
                "handoff_journal", "a handoff record names a foreign database", where
            )
        if payload.get("action") != f"grant_connect:{role}":
            raise EnrollmentError(
                "handoff_journal", "a handoff record names a foreign action", where
            )
        if payload.get("handoff") != HANDOFF_EVENTS[role]:
            raise EnrollmentError(
                "handoff_journal", "a handoff record names a foreign event", where
            )
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not _HANDOFF_RUN_ID.fullmatch(run_id):
            raise EnrollmentError(
                "handoff_journal", "a handoff record has no run id", where
            )
        operator = payload.get("operator")
        if not isinstance(operator, str) or not operator:
            raise EnrollmentError(
                "handoff_journal", "a handoff record has no operator", where
            )
        if state == "intent":
            if run_id in intents or run_id in outcomes:
                raise EnrollmentError(
                    "handoff_journal",
                    "a handoff operation id appears more than once",
                    where,
                )
            if payload.get("application_name") != handoff_application_name(run_id):
                raise EnrollmentError(
                    "handoff_journal",
                    "a handoff intent does not name its operation session",
                    where,
                )
            DatabaseAclState.from_journal(payload.get("before"), where=where)
            intents[run_id] = record
            continue

        # The type gate comes BEFORE the membership test: `HANDOFF_OUTCOMES` is a set, so
        # an unhashable value (a list, a dict) would raise TypeError from the lookup itself
        # and escape as an internal error instead of the controlled `handoff_journal`
        # refusal. Only a known string token is ever an outcome; everything else - a list,
        # a mapping, null, a number, a boolean or an unrecognized string - is corruption,
        # and never CONNECT provenance.
        outcome = payload.get("outcome")
        if not isinstance(outcome, str) or outcome not in HANDOFF_OUTCOMES:
            raise EnrollmentError(
                "handoff_journal",
                "a handoff outcome is not recognized",
                f"{where}, {type(outcome).__name__}",
            )
        intent = intents.get(run_id)
        if intent is None:
            raise EnrollmentError(
                "handoff_journal",
                "a handoff outcome has no matching prior intent",
                where,
            )
        if run_id in outcomes:
            raise EnrollmentError(
                "handoff_journal",
                "a handoff operation has more than one outcome",
                where,
            )
        for key in ("role", "action", "handoff", "database"):
            if payload.get(key) != intent.payload.get(key):
                raise EnrollmentError(
                    "handoff_journal",
                    "a handoff outcome does not match its intent",
                    where,
                )
        _validate_handoff_evidence(payload, intent.payload, where=where)
        outcomes[run_id] = record
    return found


def pending_handoff_intents(records: Iterable[Record]) -> tuple[Record, ...]:
    """LCK-08: handoff intents with no outcome record for the same run id, oldest first.

    An intent without an outcome is UNKNOWN. It is never treated as "did not happen"; the
    next handoff invocation must (1) prove the original operation terminated and (2) compare
    the complete ACL with the intent's recorded pre-state before it may append a resolving
    outcome. Until then no other handoff transition is attempted.
    """
    events = handoff_events(records)
    resolved = {
        (record.payload["run_id"], record.payload["role"])
        for record in events
        if record.payload["state"] == "outcome"
    }
    return tuple(
        record
        for record in events
        if record.payload["state"] == "intent"
        and (record.payload["run_id"], record.payload["role"]) not in resolved
    )


def successful_handoff_roles(records: Iterable[Record]) -> frozenset[str]:
    """The roles whose CONNECT has journaled provenance.

    Only an `observed` outcome that *made* the change, or a `reconciled` outcome that
    observed it applied, is proof. A `failed` outcome, a reconciled "not applied", or the
    mere presence of a grant in the catalog is not. A no-op outcome (the role already held
    CONNECT) only confirms provenance created earlier in the same history; a no-op that has
    no such predecessor is semantically inconsistent and is refused rather than allowed to
    launder an unjournaled CONNECT.

    The whole lock-handoff history is validated first: a caller that reads only grant
    events must not silently ignore a corrupted or unresolved lock record (LCK-08).
    """
    records = tuple(records)
    # A pending lock intent is an unresolved UNKNOWN (LCK-08). A reader that pairs grant
    # records must not silently hand an authorization decision to its caller while that
    # state exists; validating the pairing alone would let a pending intent through.
    pending_lock = pending_lock_handoff_refusal(records)
    if pending_lock is not None:
        raise EnrollmentError("lock_handoff_unknown", pending_lock)
    roles: set[str] = set()
    for record in handoff_events(records):
        payload = record.payload
        if payload["state"] != "outcome":
            continue
        evidence = payload["evidence"]
        if evidence.get("has_database_privilege") is not True:
            continue
        if evidence.get("changed") is True or evidence.get("applied") is True:
            roles.add(payload["role"])
            continue
        if payload["role"] not in roles:
            raise EnrollmentError(
                "handoff_journal",
                "a no-op handoff outcome precedes any successful handoff for its role",
                f"seq {record.seq}",
            )
    return frozenset(roles)


# --- §7.4 HOF-03 item 4: the journaled lock handoff ------------------------------------------
#
# HOF-03 makes the lock handoff an *observed* re-arm condition: the web process holds the
# shared state-volume lock (`dbtool check-idle` refuses) and no exclusive holder remains,
# and the journal records `handoff:lock`. The event is its own journal kind: it grants
# nothing, names no role or database, and must never be folded into the grant records the
# handoff parser pairs and authorizes CONNECT from. Like every LCK-07 observation it is
# recorded as an intent followed by one outcome, and an intent without an outcome is
# UNKNOWN (LCK-08), resolved by re-running the same command. The evidence is the kernel's
# own per-descriptor FLOCK record for the validated web process, not a successful probe.
LOCK_HANDOFF_KIND = "handoff_lock"
LOCK_HANDOFF_ACTION = "observe_lock_handoff"
LOCK_HANDOFF_EVENT = "lock"
LOCK_EXCLUSIVE_REFUSAL_CODE = "maintenance_lock_busy"
# An outcome is a decision. `unknown` is deliberately absent: an unresolved observation has
# no outcome at all and stays pending.
LOCK_HANDOFF_OUTCOMES = frozenset({"observed", "failed"})
_LOCK_INTENT_KEYS = frozenset({"state", "action", "handoff", "operator", "run_id"})
_LOCK_OUTCOME_KEYS = frozenset(
    {"state", "action", "handoff", "operator", "run_id", "outcome", "evidence"}
)
# The lock proof established by `deploy/postgres/lock_owner.py`: the kernel's lock table
# shows a FLOCK READ held by the validated web process's own descriptor, bracketed by two
# identical snapshots. `check-idle` must also be refused with the lock-held code, but the
# refusal alone is never the evidence (a probe's own temporary lock must not count).
LOCK_HANDOFF_OBSERVATIONS = 2
LOCK_ACCESS_READ = "read"
LOCK_ACCESS_WRITE = "write"
_LOCK_ACCESSES = frozenset({LOCK_ACCESS_READ, LOCK_ACCESS_WRITE})
# Option A: a host-held shared flock on the same validated inode, acquired after
# `check-idle` and retained across the final snapshot, the fresh container verification
# and the journal outcome append/fsync. It is part of the publication procedure, never a
# substitute for the web process's own fdinfo record.
LOCK_WITNESS_ACCESS = "read"
# The closed interval an `observed` outcome describes. Nothing after the fsync is claimed.
LOCK_OBSERVATION_BOUNDARY = "final_verification_through_publication"
_LOCK_EVIDENCE_KEYS = frozenset(
    {
        "container_id",
        "state_volume",
        "lock_present",
        "lock_device",
        "lock_inode",
        "idle_exit",
        "idle_code",
        "holder_pid",
        "holder_start_ticks",
        "holder_access",
        "observations",
        "witness_access",
        "observation_boundary",
    }
)
_LOCK_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LOCK_DEVICE = re.compile(r"\d+:\d+")
_VOLUME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _lock_exit_code(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 255
    )


def _lock_code(value: object) -> bool:
    return value is None or (isinstance(value, str) and bool(_LOCK_CODE.fullmatch(value)))


def _lock_count(value: object, *, minimum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= minimum
    )


def lock_handoff_satisfied(evidence: Mapping[str, Any]) -> bool:
    """The exact positive observation of HOF-03 item 4, and nothing weaker.

    Every conjunct is a fact the kernel-level proof produces: both ownership snapshots
    agreed (`observations`), the state volume has the lock file, the validated web
    process itself holds a FLOCK READ on it (`holder_*`), the frozen `check-idle`
    observation was refused with the lock-held code, and the host's shared witness was
    held through the final verification and the outcome publication. A refusal for any
    other reason, a holder whose lock is WRITE, a missing/ambiguous holder, or a missing
    publication witness is not the lock handoff and can never be recorded `observed`.
    """
    return (
        evidence.get("lock_present") is True
        and evidence.get("observations") == LOCK_HANDOFF_OBSERVATIONS
        and evidence.get("idle_exit") == 3
        and evidence.get("idle_code") == LOCK_EXCLUSIVE_REFUSAL_CODE
        and _lock_count(evidence.get("holder_pid"), minimum=1)
        and _lock_count(evidence.get("holder_start_ticks"), minimum=0)
        and evidence.get("holder_access") == LOCK_ACCESS_READ
        and evidence.get("witness_access") == LOCK_WITNESS_ACCESS
        and evidence.get("observation_boundary") == LOCK_OBSERVATION_BOUNDARY
    )


def _validate_lock_evidence(
    evidence: object, outcome: str, where: str
) -> None:
    """Structural and semantic validation of one outcome's evidence.

    The exit status and its semantic code are validated as one coherent result (a
    successful exit cannot carry a refusal code), the holder triple is all-or-nothing, and
    an `observed` outcome must reproduce exactly the success condition. A record that
    cannot be read as one coherent observation is journal corruption, never a milder
    outcome.
    """
    if not isinstance(evidence, Mapping) or set(evidence) != _LOCK_EVIDENCE_KEYS:
        raise EnrollmentError(
            "lock_handoff_journal", "a lock handoff evidence record is malformed", where
        )
    if not isinstance(evidence.get("container_id"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", evidence["container_id"]
    ):
        raise EnrollmentError(
            "lock_handoff_journal", "a lock handoff names no container", where
        )
    volume = evidence.get("state_volume")
    if not isinstance(volume, str) or not _VOLUME_NAME.fullmatch(volume):
        raise EnrollmentError(
            "lock_handoff_journal", "a lock handoff names no state volume", where
        )
    if not isinstance(evidence.get("lock_present"), bool):
        raise EnrollmentError(
            "lock_handoff_journal", "a lock handoff does not say whether the lock file exists", where
        )
    # The validated lock inode is recorded with the observation. It is present exactly
    # when the lock file exists, and it is what the host witness was taken on.
    device = evidence.get("lock_device")
    inode = evidence.get("lock_inode")
    if evidence["lock_present"]:
        if not isinstance(device, str) or not _LOCK_DEVICE.fullmatch(device):
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff lock device is malformed", where
            )
        if not _lock_count(inode, minimum=1):
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff lock inode is malformed", where
            )
    elif device is not None or inode is not None:
        raise EnrollmentError(
            "lock_handoff_journal",
            "a lock handoff names a lock file identity but no lock file",
            where,
        )
    witness = evidence.get("witness_access")
    boundary = evidence.get("observation_boundary")
    if witness not in (None, LOCK_WITNESS_ACCESS):
        raise EnrollmentError(
            "lock_handoff_journal", "a lock handoff witness access is not recognized", where
        )
    if witness is not None and evidence["lock_present"] is not True:
        raise EnrollmentError(
            "lock_handoff_journal",
            "a lock handoff records a witness without a lock file",
            where,
        )
    if witness == LOCK_WITNESS_ACCESS:
        if boundary != LOCK_OBSERVATION_BOUNDARY:
            raise EnrollmentError(
                "lock_handoff_journal",
                "a lock handoff witness does not name its publication boundary",
                where,
            )
    elif boundary is not None:
        raise EnrollmentError(
            "lock_handoff_journal",
            "a lock handoff names a publication boundary without a witness",
            where,
        )
    if not _lock_exit_code(evidence.get("idle_exit")):
        raise EnrollmentError(
            "lock_handoff_journal", "a lock handoff exit status is malformed", where
        )
    if not _lock_code(evidence.get("idle_code")):
        raise EnrollmentError(
            "lock_handoff_journal", "a lock handoff refusal code is malformed", where
        )
    if evidence.get("observations") != LOCK_HANDOFF_OBSERVATIONS:
        raise EnrollmentError(
            "lock_handoff_journal",
            "a lock handoff was not established by the required two observations",
            where,
        )
    exit_code = evidence["idle_exit"]
    code = evidence["idle_code"]
    # Exit status and semantic code are one result: dbtool prints a code only for its
    # refused (3) and unusable (4) classes, and never for success.
    if exit_code == 0 and code is not None:
        raise EnrollmentError(
            "lock_handoff_journal",
            "a successful idle check carries a refusal code",
            where,
        )
    if exit_code in (3, 4) and code is None:
        raise EnrollmentError(
            "lock_handoff_journal",
            "a refused idle check has no semantic code",
            where,
        )
    holder_fields = (
        evidence.get("holder_pid"),
        evidence.get("holder_start_ticks"),
        evidence.get("holder_access"),
    )
    if exit_code == 0 and any(value is not None for value in holder_fields):
        raise EnrollmentError(
            "lock_handoff_journal",
            "a successful idle check records a lock holder",
            where,
        )
    if all(value is None for value in holder_fields):
        if outcome == "observed":
            raise EnrollmentError(
                "lock_handoff_journal",
                "an observed lock handoff records no web lock holder",
                where,
            )
    else:
        if any(value is None for value in holder_fields):
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff holder record is incomplete", where
            )
        if not _lock_count(evidence.get("holder_pid"), minimum=1):
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff holder pid is malformed", where
            )
        if not _lock_count(evidence.get("holder_start_ticks"), minimum=0):
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff holder start time is malformed", where
            )
        if evidence.get("holder_access") not in _LOCK_ACCESSES:
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff holder access is not recognized", where
            )
        if evidence.get("lock_present") is not True:
            raise EnrollmentError(
                "lock_handoff_journal",
                "a lock handoff names a holder but no lock file",
                where,
            )
    satisfied = lock_handoff_satisfied(evidence)
    if outcome == "observed" and not satisfied:
        raise EnrollmentError(
            "lock_handoff_journal",
            "an observed lock handoff contradicts its evidence",
            where,
        )
    if outcome == "failed" and satisfied:
        raise EnrollmentError(
            "lock_handoff_journal",
            "a failed lock handoff records the exact success condition",
            where,
        )


def lock_handoff_events(records: Iterable[Record]) -> tuple[Record, ...]:
    """The valid lock-handoff records, in journal order, as one paired history.

    The history is strictly sequential: one intent, then its outcome, then (only after
    that outcome) another intent. An intent opened while an earlier intent has no outcome
    is corruption - it would let a later success conceal an unresolved UNKNOWN - and so is
    an outcome that does not resolve the one pending intent, a duplicate op id, or a
    second outcome for one op id.

    A later intent *is* allowed after any outcome, because the observation is about a
    running process at one time and the web container can be replaced. Each intent/outcome
    pair is one observation epoch; the latest outcome is the current state. That is why
    `lock_handoff_state` - not "any observed record in history" - decides whether the
    handoff is observed now. The operator label is recorded on each record and is not
    required to match, because a later invocation may reconcile an earlier intent after a
    different operator was named.
    """
    found = tuple(record for record in records if record.kind == LOCK_HANDOFF_KIND)
    intents: dict[str, Record] = {}
    outcomes: dict[str, Record] = {}
    pending: Optional[Record] = None
    for record in found:
        payload = record.payload
        where = f"seq {record.seq}"
        state = payload.get("state")
        if state not in ("intent", "outcome"):
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff record has an unknown state", where
            )
        expected_keys = _LOCK_INTENT_KEYS if state == "intent" else _LOCK_OUTCOME_KEYS
        if set(payload) != expected_keys:
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff record has unexpected fields", where
            )
        if payload.get("action") != LOCK_HANDOFF_ACTION:
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff record names a foreign action", where
            )
        if payload.get("handoff") != LOCK_HANDOFF_EVENT:
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff record names a foreign event", where
            )
        operator = payload.get("operator")
        if not isinstance(operator, str) or not operator:
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff record has no operator", where
            )
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not _HANDOFF_RUN_ID.fullmatch(run_id):
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff record has no run id", where
            )
        if state == "intent":
            if pending is not None:
                raise EnrollmentError(
                    "lock_handoff_journal",
                    "a lock handoff intent overlaps an earlier intent with no outcome",
                    where,
                )
            if run_id in intents or run_id in outcomes:
                raise EnrollmentError(
                    "lock_handoff_journal",
                    "a lock handoff operation id appears more than once",
                    where,
                )
            intents[run_id] = record
            pending = record
            continue
        outcome = payload.get("outcome")
        if not isinstance(outcome, str) or outcome not in LOCK_HANDOFF_OUTCOMES:
            raise EnrollmentError(
                "lock_handoff_journal", "a lock handoff outcome is not recognized", where
            )
        if pending is None:
            raise EnrollmentError(
                "lock_handoff_journal",
                "a lock handoff outcome has no pending intent to resolve",
                where,
            )
        if run_id != pending.payload["run_id"]:
            raise EnrollmentError(
                "lock_handoff_journal",
                "a lock handoff outcome does not resolve the pending intent",
                where,
            )
        if run_id in outcomes:
            raise EnrollmentError(
                "lock_handoff_journal",
                "a lock handoff operation has more than one outcome",
                where,
            )
        # The operator label is not compared with the intent's: a later invocation (a
        # different operator resolving an UNKNOWN) records its own label, exactly as the
        # grant-handoff outcomes do.
        _validate_lock_evidence(payload.get("evidence"), outcome, where)
        outcomes[run_id] = record
        pending = None
    return found


@dataclass(frozen=True)
class LockHandoffState:
    """The latest lock-handoff state, derived from the validated paired history.

    `pending` is an intent with no outcome (UNKNOWN until resolved). `latest_outcome` is
    the most recent decision, whatever it is. `observed` is that decision only when it is
    `observed`; a historical observed outcome followed by a later failed outcome is NOT
    observed now, and must not authorize a re-arm.
    """

    pending: Optional[Record]
    latest_outcome: Optional[Record]

    @property
    def observed(self) -> Optional[Record]:
        # A pending intent is the current state: an earlier outcome is history, not a
        # present observation, until this intent is resolved.
        if self.pending is not None:
            return None
        latest = self.latest_outcome
        if latest is not None and latest.payload["outcome"] == "observed":
            return latest
        return None

    @property
    def status(self) -> str:
        if self.pending is not None:
            return "pending"
        if self.latest_outcome is None:
            return "none"
        return str(self.latest_outcome.payload["outcome"])


def lock_handoff_state(records: Iterable[Record]) -> LockHandoffState:
    """Validate the whole lock history and return its latest state.

    The order matters: `lock_handoff_events` refuses overlapping intents, an outcome
    without its intent, a mismatched run id and contradictory evidence before any state
    is derived. Only after that is the last outcome authoritative.
    """
    events = lock_handoff_events(records)
    pending: Optional[Record] = None
    latest_outcome: Optional[Record] = None
    for record in events:
        if record.payload["state"] == "intent":
            pending = record
            continue
        pending = None
        latest_outcome = record
    return LockHandoffState(pending=pending, latest_outcome=latest_outcome)


def pending_lock_handoff_intent(records: Iterable[Record]) -> Optional[Record]:
    """LCK-08: the lock-handoff intent with no outcome, if one exists.

    The strict history admits at most one such intent and it is the last record.
    """
    return lock_handoff_state(records).pending


def pending_lock_handoff_refusal(records: Iterable[Record]) -> Optional[str]:
    """A non-secret reason when a lock-handoff intent is UNKNOWN, else None.

    Calling this validates the whole lock history: a malformed or overlapping history
    raises `EnrollmentError` instead of being silently ignored by a caller that only
    inspects grant events. Every operation that advances the cutover machine calls it.
    """
    pending = pending_lock_handoff_intent(records)
    if pending is None:
        return None
    return (
        f"the lock handoff intent at seq {pending.seq} has no observed outcome; run "
        "`enroll.py lock-handoff --approved-by <owner>` to reconcile it before another "
        "operation"
    )


def lock_handoff_observed(records: Iterable[Record]) -> Optional[Record]:
    """The latest lock-handoff outcome, only when that outcome is `observed`.

    Never a historical observed outcome that a later outcome superseded, and never a
    `failed` record. A caller deciding whether the re-arm condition holds now must use
    this (or `lock_handoff_state`), not a scan for any observed record ever written.
    """
    return lock_handoff_state(records).observed


def acl_state_equal(left: DatabaseAclState, right: DatabaseAclState) -> bool:
    """Total equality: materialization, every explicit entry and every effective value."""
    return (
        left.acl_materialized == right.acl_materialized
        and left.entries == right.entries
        and left.effective == right.effective
    )


def database_acl_failures(
    state: DatabaseAclState,
    *,
    records: Iterable[Record],
    authorized_roles: Iterable[str] = (),
) -> tuple[str, ...]:
    """The complete ACL security invariant for the serving database.

    It is checked both before any handoff (the fence must already be intact) and after a
    transition. Every failure is a stable, non-secret label:

    * the ACL must be materialized; PUBLIC must hold nothing;
    * no grant option may appear on a login role;
    * `CREATE` and `TEMPORARY` are forbidden to every login role;
    * a login role may hold CONNECT only when it is `hh_migrate` (pre-handoff) or has a
      journaled successful handoff (`authorized_roles` lets the caller add the role that the
      outcome currently being judged is about);
    * a role that may hold CONNECT must hold it, exactly once, non-grantable, with no other
      explicit entry; a role that may not hold it must hold no explicit entry at all;
    * no explicit entry may name a role outside the project inventory.
    """
    provenance = set(successful_handoff_roles(records)) | set(authorized_roles)
    allowed_connect = set(PRE_HANDOFF_CONNECT_ROLES) | provenance
    failures: list[str] = []
    if not state.acl_materialized:
        failures.append("acl_not_materialized")
    for grantee, privilege, grantable in state.entries:
        if grantee == "PUBLIC":
            failures.append("public_database_privilege")
            continue
        if grantee not in contract.PROJECT_ROLE_NAMES:
            failures.append(f"foreign_grantee:{grantee}")
            continue
        if grantable and grantee != contract.OWNER_ROLE:
            failures.append(f"grant_option:{grantee}:{privilege}")
    for role in contract.PROJECT_ROLE_NAMES:
        for privilege in DATABASE_PRIVILEGES:
            if state.privilege(role, privilege) is None:
                failures.append("acl_unrecognized_observation")
                return tuple(dict.fromkeys(failures))
        if role == contract.OWNER_ROLE:
            # The NOLOGIN owner's rights are inherent; its explicit representation may
            # legitimately be absent or complete. Collateral changes to it are still
            # caught by the total pre/post comparison.
            continue
        connect = state.privilege(role, "CONNECT")
        if role in allowed_connect:
            if connect is not True:
                failures.append(f"connect_missing:{role}")
        elif connect is not False:
            failures.append(f"unrecorded_connect:{role}")
        if state.privilege(role, "CREATE") is not False:
            failures.append(f"create:{role}")
        if state.privilege(role, "TEMPORARY") is not False:
            failures.append(f"temporary:{role}")
        role_entries = state.entries_for(role)
        connect_entries = [entry for entry in role_entries if entry[1] == "CONNECT"]
        for _grantee, privilege, _grantable in role_entries:
            if privilege != "CONNECT":
                failures.append(f"unexpected_privilege:{role}:{privilege}")
        if role in allowed_connect:
            if len(connect_entries) != 1:
                failures.append(f"connect_entry_count:{role}")
            elif connect_entries[0][2] is not False:
                failures.append(f"grant_option:{role}:CONNECT")
        elif connect_entries:
            failures.append(f"unexpected_entry:{role}:CONNECT")
    return tuple(dict.fromkeys(failures))


def expected_after_handoff(before: DatabaseAclState, role: str) -> DatabaseAclState:
    """The exact post-state a successful `GRANT CONNECT` to `role` may produce.

    A role that already holds CONNECT is a no-op, so the expected state is the pre-state
    itself.
    """
    if before.privilege(role, "CONNECT") is True:
        return before
    entries = tuple(sorted(before.entries + ((role, "CONNECT", False),)))
    effective = tuple(
        sorted(
            (
                observed_role,
                privilege,
                True
                if (observed_role, privilege) == (role, "CONNECT")
                else value,
            )
            for observed_role, privilege, value in before.effective
        )
    )
    return DatabaseAclState(
        acl_materialized=before.acl_materialized,
        entries=entries,
        effective=effective,
    )


def _multiset_difference(
    expected: Sequence[tuple[str, str, bool]],
    observed: Sequence[tuple[str, str, bool]],
) -> tuple[tuple[str, str, bool], ...]:
    remaining = list(observed)
    missing: list[tuple[str, str, bool]] = []
    for item in expected:
        if item in remaining:
            remaining.remove(item)
        else:
            missing.append(item)
    return tuple(missing) + tuple(remaining)


def acl_transition_failures(
    before: DatabaseAclState,
    after: DatabaseAclState,
    role: str,
    *,
    records: Iterable[Record],
) -> tuple[str, ...]:
    """The observed post-state must equal the pre-state plus exactly one CONNECT.

    This is the Finding-3 correction: only selected fields were checked before, so a
    grant option, a collateral `CREATE`, or an unjournaled CONNECT on another role could
    pass. The comparison here is total, and the invariant is then re-checked on the
    resulting state with the current role's provenance admitted.
    """
    expected = expected_after_handoff(before, role)
    failures: list[str] = []
    if after.acl_materialized != expected.acl_materialized:
        failures.append(
            "acl_materialization_changed:"
            + ("materialized" if after.acl_materialized else "unmaterialized")
        )
    if after.entries != expected.entries:
        for grantee, privilege, grantable in _multiset_difference(
            expected.entries, after.entries
        ):
            failures.append(f"acl_entry:{grantee}:{privilege}:{int(grantable)}")
    if after.effective != expected.effective:
        expected_map = {
            (observed_role, privilege): value
            for observed_role, privilege, value in expected.effective
        }
        for observed_role, privilege, value in after.effective:
            if expected_map.get((observed_role, privilege)) != value:
                failures.append(
                    f"privilege_changed:{observed_role}:{privilege}:{int(value)}"
                )
    # The role's provenance is admitted only when this transition is what creates its
    # CONNECT. A pre-existing CONNECT must already have a journaled handoff; otherwise the
    # baseline below reports `unrecorded_connect:<role>` instead of blessing it.
    adds_connect = before.privilege(role, "CONNECT") is not True
    failures.extend(
        database_acl_failures(
            after,
            records=records,
            authorized_roles=(role,) if adds_connect else (),
        )
    )
    return tuple(dict.fromkeys(failures))


def reconcile_acl_decision(
    before: DatabaseAclState,
    after: DatabaseAclState,
    role: str,
    *,
    records: Iterable[Record],
) -> tuple[str, tuple[str, ...]]:
    """Decide an UNKNOWN operation's outcome from the complete ACL state alone.

    * `not_applied`: the state is exactly the recorded pre-state and the fence is intact;
      the GRANT did not commit.
    * `applied`: the state is exactly the pre-state plus this role's single CONNECT and the
      fence is intact with the role's provenance admitted.
    * `unsafe`: anything else. It is never reconciled; the caller must retain UNKNOWN.
    """
    if acl_state_equal(before, after):
        failures = database_acl_failures(after, records=records)
        return ("unsafe", failures) if failures else ("not_applied", ())
    failures = acl_transition_failures(before, after, role, records=records)
    return ("unsafe", failures) if failures else ("applied", ())


# --- handoff transport termination (Finding 1) ----------------------------------------------


@dataclass(frozen=True)
class HandoffTransportEvidence:
    """The container-side completion and liveness evidence of ONE handoff operation.

    It is evidence only. `observed=False` means the probe itself failed and is never
    interpreted as termination. `psql_exit` is the status the container-side wrapper
    recorded after psql returned; its presence proves the client terminated. `client_alive`
    and `session_alive` are positive evidence that it has not.
    """

    run_id: str = ""
    observed: bool = False
    started: bool = False
    stopped: bool = False
    psql_exit: Optional[int] = None
    client_alive: bool = False
    session_alive: bool = False


def handoff_termination_failures(
    evidence: HandoffTransportEvidence,
) -> tuple[str, ...]:
    """Why the original operation's termination is not positively established.

    An instantaneous privilege snapshot is never enough (Finding 1): a delayed GRANT may
    still execute. Termination is established only when the tagged session/client is gone
    AND either its recorded completion status exists or the wrapper was observed to have
    started. A wrapper that never started cannot be distinguished from one that is about to
    start, so it stays UNKNOWN.
    """
    if not evidence.observed:
        return ("transport:unobserved",)
    if evidence.client_alive:
        return ("transport:client_alive",)
    if evidence.session_alive:
        return ("transport:session_alive",)
    if evidence.psql_exit is not None:
        return ()
    if evidence.started:
        return ()
    return ("transport:never_started",)


# --- the E0-E7 dispatch ----------------------------------------------------------------------


def _pristine(observation: Observation) -> tuple[str, ...]:
    failures = list(
        contract.initializer_inventory_failures(
            observation.roles,
            observation.databases,
            observation.predefined_roles,
            observation.memberships,
        )
    )
    if observation.database is not None and observation.database.exists:
        failures.append("unexpected_database:house_helper")
    if observation.marker:
        failures.append("unexpected_marker")
    return tuple(failures)


def _hardened_failures(
    observation: Observation, *, allow_marker: bool = False
) -> tuple[str, ...]:
    """ENR-04: the complete hardening - database-level AND schema/default-ACL (F8).

    The adapter has run the accepted `db/bootstrap.sql` harden/enable phases and then
    `db/schema.sql` under the authenticated `hh_migrate` -> `hh_owner` session. This is an
    independent observation of the resulting catalog, not a trust in either mutation call.
    Database-level hardening alone is never "hardened": the `public` schema ownership/ACL
    and the exact ROL-14 default-ACL matrix must be observed, and no application relation
    may exist yet.
    """
    facts = observation.database
    if facts is None or not facts.exists:
        return ("database:not_observed",)
    failures = list(
        contract.database_acl_failures(
            database=contract.SERVING_DATABASE, facts=facts, grantee="hh_migrate"
        )
    )
    if facts.owner != contract.OWNER_ROLE:
        failures.append("database:ownership")
    if facts.encoding != contract.EXPECTED_ENCODING:
        failures.append("database:encoding")
    if (facts.collate, facts.ctype, facts.provider) != contract.EXPECTED_LOCALE:
        failures.append("database:locale")
    if facts.allowconn is not True:
        failures.append("database:not_connectable")
    if facts.connect.get(contract.OWNER_ROLE) is not True:
        failures.append("owner_connect_missing")
    if facts.connect.get("hh_migrate") is not True:
        failures.append("migrate_connect_missing")
    for role in contract.RUNTIME_ROLES:
        if facts.connect.get(role) is not False:
            failures.append(f"runtime_connect:{role}")
        if facts.temporary.get(role) is not False:
            failures.append(f"runtime_temporary:{role}")
    acl_failures = contract.default_acl_failures(observation.default_acls)
    failures.extend(acl_failures)
    # F8: the schema half of ENR-04 is verified from the catalog, not trusted from the
    # one-shot that ran it. This includes `public` ownership and the exact ACL matrix.
    failures.extend(contract.public_schema_acl_failures(observation.schema_acl))
    if observation.schema_versions or observation.public_tables:
        failures.append("schema:application_objects")
    if observation.target_inventory is None:
        failures.append("target:not_observed")
    else:
        failures.extend(
            contract.authorized_inventory_failures(
                observation.target_inventory,
                default_acls_ok=not acl_failures,
                alembic_initialized=False,
            )
        )
    if observation.marker and not allow_marker:
        failures.append("marker:present_before_hardening")
    return tuple(failures)


def _inventory_mismatches(
    intent: Mapping[str, Any], observation: Observation
) -> tuple[str, ...]:
    failures: list[str] = []
    recorded_marker = intent.get("marker")
    if not isinstance(recorded_marker, str):
        failures.append("intent:marker")
    elif observation.marker:
        try:
            token, identifier = parse_marker(observation.marker)
        except JournalError as error:
            failures.append(f"marker:{error.code}")
        else:
            try:
                expected_token, expected_identifier = parse_marker(recorded_marker)
            except JournalError:
                failures.append("intent:marker")
            else:
                if token != expected_token:
                    failures.append("marker:token")
                if identifier != expected_identifier:
                    failures.append("marker:system_identifier")
                if identifier != observation.identity.system_identifier:
                    failures.append("marker:live_system_identifier")
    recorded_predefined = intent.get("predefined_roles")
    if isinstance(recorded_predefined, list):
        if sorted(str(name) for name in recorded_predefined) != sorted(observation.predefined_roles):
            failures.append("predefined_roles")
    failures.extend(contract.predefined_role_failures(observation.predefined_roles))
    recorded_predefined_edges = intent.get("predefined_memberships")
    if isinstance(recorded_predefined_edges, list):
        actual = [
            {
                "member": edge.member,
                "granted": edge.granted,
                "admin": edge.admin,
                "inherit": edge.inherit,
                "set": edge.set_option,
            }
            for edge in observation.predefined_memberships
        ]
        if recorded_predefined_edges != actual:
            failures.append("predefined_memberships")
    failures.extend(
        contract.membership_failures(observation.memberships, observation.predefined_roles)
    )
    failures.extend(contract.existing_role_failures(observation.roles))
    failures.extend(
        contract.foreign_role_failures(observation.roles, observation.predefined_roles)
    )
    return tuple(failures)


def _observed_state(intent: Mapping[str, Any], observation: Observation) -> tuple[str, tuple[str, ...]]:
    failures: list[str] = []
    if observation.identity.system_identifier != str(intent.get("system_identifier")):
        failures.append("system_identifier")
    recorded_container = intent.get("container") or {}
    recorded_volume = intent.get("volume")
    for key in ("container_name", "container_id", "image_digest"):
        if recorded_container.get(key) != getattr(observation.identity, key):
            failures.append(f"container:{key}")
    if recorded_volume != observation.identity.volume:
        failures.append("volume")
    if failures:
        return FAIL, tuple(failures)

    unexpected_databases = set(observation.databases) - set(contract.BASE_DATABASES) - {
        contract.SERVING_DATABASE
    }
    for name in sorted(unexpected_databases):
        failures.append(f"unexpected_database:{name}")
    for name in observation.roles:
        if name.startswith("hh_") and name not in contract.PROJECT_ROLE_NAMES:
            failures.append(f"unexpected_role:{name}")
    failures.extend(
        contract.foreign_role_failures(observation.roles, observation.predefined_roles)
    )
    failures.extend(contract.predefined_role_failures(observation.predefined_roles))
    if failures:
        return FAIL, tuple(failures)

    role_failures = contract.existing_role_failures(observation.roles)
    edge_failures = contract.existing_membership_failures(
        observation.memberships, observation.predefined_roles
    )
    predefined_failures = contract.predefined_membership_failures(
        observation.memberships, observation.predefined_roles
    )
    if role_failures or edge_failures or predefined_failures:
        # A project role that exists in the wrong shape is never "a partial state".
        return FAIL, tuple(
            list(role_failures) + list(edge_failures) + list(predefined_failures)
        )
    bootstrap = observation.roles.get(contract.BOOTSTRAP_ROLE)
    if bootstrap is None or not bootstrap.superuser:
        return FAIL, ("bootstrap_superuser",)
    for name in contract.BASE_DATABASES:
        if name not in observation.databases:
            return FAIL, (f"missing_database:{name}",)

    present = [name for name in contract.PROJECT_ROLE_NAMES if name in observation.roles]
    if not present:
        return E1, ()
    if len(present) < len(contract.PROJECT_ROLE_NAMES):
        return E2, ()
    if contract.SERVING_DATABASE not in observation.databases:
        return E3, ()
    if observation.database is None or not observation.database.exists:
        return FAIL, ("database:not_observed",)

    marker_failures = _inventory_mismatches(intent, observation)
    if marker_failures:
        return FAIL, marker_failures

    inventory = observation.target_inventory
    facts = observation.database
    if facts.owner != contract.OWNER_ROLE:
        return FAIL, ("database:ownership",)
    if facts.encoding != contract.EXPECTED_ENCODING:
        return FAIL, ("database:encoding",)
    if (facts.collate, facts.ctype, facts.provider) != contract.EXPECTED_LOCALE:
        return FAIL, ("database:locale",)

    version_table_present = "alembic_version" in set(observation.public_tables)
    acl_failures = contract.default_acl_failures(observation.default_acls)
    if observation.default_acls and acl_failures:
        # Default ACLs that are not exactly ROL-14 are never a recognized partial state.
        return FAIL, tuple(acl_failures)
    if facts.allowconn is False and observation.default_acls:
        # BOOT-06: target-schema work while connections are disabled is not an accepted
        # state. The schema contract is installed only after E3-c.
        return FAIL, ("schema:present_while_disabled",)
    if observation.default_acls:
        schema_failures = contract.public_schema_acl_failures(observation.schema_acl)
        if schema_failures:
            return FAIL, tuple(schema_failures)
    if inventory is not None:
        inventory_failures = contract.authorized_inventory_failures(
            inventory,
            default_acls_ok=not acl_failures,
            alembic_initialized=version_table_present,
        )
        if inventory_failures:
            return FAIL, tuple(inventory_failures)

    # The inventory was authorized above by state; the classifier's own "any target object
    # is foreign" rule is the pre-F6 rule and must not reject the authorized partial state.
    verdict = contract.classify_facts(
        contract.DatabaseFacts(
            exists=True,
            owner=facts.owner,
            encoding=facts.encoding,
            collate=facts.collate,
            ctype=facts.ctype,
            provider=facts.provider,
            allowconn=facts.allowconn,
            acl_materialized=facts.acl_materialized,
            acl_entries=tuple(facts.acl_entries),
            connect=dict(facts.connect),
            temporary=dict(facts.temporary),
            sessions=tuple(facts.sessions),
            probe_pid=facts.probe_pid,
            target_inventory=None,
            subscriptions=tuple(facts.subscriptions),
            default_acls=tuple(facts.default_acls),
        ),
        database=contract.SERVING_DATABASE,
        grantee="hh_migrate",
    )
    if verdict.fail_closed:
        return FAIL, tuple(verdict.failures or ("e4_fail_closed",))
    if verdict.state in (
        contract.E4_DISABLED_UNHARDENED,
        contract.E4_DISABLED_HARDENED,
    ):
        if observation.marker:
            return FAIL, ("marker:present_while_disabled",)
        return E4, ()
    if verdict.state == contract.E4_ENABLED_MAINTENANCE_ONLY:
        if inventory is None:
            return FAIL, ("target:not_observed",)
        if observation.marker:
            if not observation.default_acls:
                # ENR-05 follows ENR-04: a marker without the schema/default-ACL half is
                # never E6, and never E7.
                return FAIL, ("marker:present_without_schema",)
            return E6, ()
        if not observation.default_acls:
            # E3-c is complete; the ENR-04 schema/default-ACL half is outstanding. This is
            # the E4 state (hardening incomplete), not E5 (hardening complete).
            return E4, ()
        return E5, ()
    return FAIL, (f"e4_state:{verdict.state}",)


def dispatch(records: Iterable[Record], observation: Observation) -> Plan:
    """The pure E0-E7 decision. It never mutates anything and never calls the adapter."""
    records = tuple(records)
    rank, completed, intent = _recorded_rank(records)
    completes = _completed_records(records)
    if len(completes) > 1:
        return Plan(FAIL, ACTION_REMEDIATE, ("journal:duplicate_completed",), rank, rank, True, intent)
    if completed:
        return Plan(E7, ACTION_MODE_B, (), rank, 7, True, intent)
    if intent is None:
        failures = _pristine(observation)
        if failures:
            return Plan(FAIL, ACTION_REMEDIATE, failures, rank, 0, False, None)
        return Plan(E0, ACTION_BEGIN, (), rank, 0, False, None)

    state, failures = _observed_state(intent, observation)
    if failures:
        return Plan(FAIL, ACTION_REMEDIATE, failures, rank, 0, False, intent)
    observed_rank = _RANK[state]
    if observed_rank < rank:
        return Plan(
            FAIL,
            ACTION_REMEDIATE,
            ("journal:observed_state_behind_journal",),
            rank,
            observed_rank,
            False,
            intent,
        )
    return Plan(
        state,
        _state_action(state, observation),
        (),
        rank,
        observed_rank,
        False,
        intent,
    )


def _state_action(state: str, observation: Observation) -> str:
    """The action for an observed state, including the two E4 sub-cases (F8).

    E4 covers both "database-level hardening incomplete" (connections disabled, or an
    accepted E3-b state) and "E3-c complete but the ENR-04 schema/default-ACL half
    outstanding". The latter must not re-run the database-level phases (the adapter's
    enablement check would correctly refuse an already-enabled database); it initializes
    the schema instead.
    """
    if state == E4:
        facts = observation.database
        if facts is not None and facts.allowconn is True:
            return ACTION_INITIALIZE_SCHEMA
    return STATE_ACTIONS[state]


# --- Mode A orchestration --------------------------------------------------------------------


def _intent_identity(intent: Mapping[str, Any]) -> ClusterIdentity:
    """The identity the Mode A intent recorded (ENR-02/ENR-13).

    The E4 schema step re-observes the identity and must prove it is still THIS intent's
    before it issues the schema authorization (F8): the host, not the database login, is
    the authority for the container/volume/image and the physical `system_identifier`.
    """
    container = intent.get("container") or {}
    return ClusterIdentity.from_journal(
        {
            "container_name": container.get("container_name", ""),
            "container_id": container.get("container_id", ""),
            "image_digest": container.get("image_digest", ""),
            "volume": intent.get("volume", ""),
            "system_identifier": intent.get("system_identifier", ""),
        }
    )


def _intent_payload(
    identity: ClusterIdentity,
    marker: str,
    observation: Observation,
) -> Mapping[str, Any]:
    return {
        "mode": "A",
        "state": "intent",
        "system_identifier": identity.system_identifier,
        "container": {
            "container_name": identity.container_name,
            "container_id": identity.container_id,
            "image_digest": identity.image_digest,
        },
        "volume": identity.volume,
        "databases": [
            {
                "name": contract.SERVING_DATABASE,
                "owner": contract.OWNER_ROLE,
                "encoding": contract.EXPECTED_ENCODING,
                "collate": contract.EXPECTED_LOCALE[0],
                "ctype": contract.EXPECTED_LOCALE[1],
            }
        ],
        "roles": [
            {"name": name, "attributes": dict(contract.ROLE_MATRIX[name])}
            for name in contract.PROJECT_ROLE_NAMES
        ],
        "predefined_roles": list(contract.FROZEN_PREDEFINED_ROLES),
        "predefined_memberships": [
            {
                "member": edge.member,
                "granted": edge.granted,
                "admin": edge.admin,
                "inherit": edge.inherit,
                "set": edge.set_option,
            }
            for edge in observation.predefined_memberships
        ],
        "marker": marker,
    }


class ModeA:
    """The accepted ENR-02..ENR-12 state machine.

    `token_source` is injected so a test gets a deterministic marker; production uses
    `secrets.token_hex(16)`, which is non-secret by construction (it identifies, it does
    not authorize).
    """

    def __init__(
        self,
        journal: Journal,
        cluster: Cluster,
        *,
        approved_by: str = "",
        token_source=None,
        clock=None,
    ) -> None:
        self.journal = journal
        self.cluster = cluster
        self.approved_by = approved_by
        self.token_source = token_source or (lambda: secrets.token_hex(16))
        self.clock = clock or utcnow

    def run(self) -> EnrollmentReport:
        # F1/F9: the whole read/dispatch/mutate/complete interval is one host-side
        # operation. A concurrent caller is refused (`operation_busy`) instead of
        # observing E0 twice and creating a competing intent. Journal appends still take
        # their own `journal.lock` inside this interval; the two locks are distinct.
        with self.journal.operation_lock():
            return self._run_locked()

    def _append_at_view(
        self, view: JournalView, kind: str, payload: Mapping[str, Any]
    ) -> Record:
        """Append a milestone only if the journal head is the one just observed."""
        record = self.journal.append_if_head(
            view.head, view.head_seq, kind, payload, at=self.clock()
        )
        if record is None:
            raise EnrollmentError(
                "journal_head_changed",
                "the enrollment journal changed while the operation was in progress",
            )
        return record

    def _run_locked(self) -> EnrollmentReport:
        view = self.journal.read()
        # LCK-08: an unresolved lock handoff blocks every later transition. Mode A is an
        # enrollment transition, so it may not proceed through that UNKNOWN; only
        # `enroll.py lock-handoff` may resolve it.
        lock_unknown = pending_lock_handoff_refusal(view.records)
        if lock_unknown is not None:
            raise EnrollmentError("lock_handoff_unknown", lock_unknown)
        # `read_target` is a request to look; the adapter only reads the target catalog
        # when connections are enabled and the database exists.
        plan = dispatch(view.records, self.cluster.observe(read_target=True))
        if plan.state == FAIL:
            raise EnrollmentError(
                "enrollment_refused",
                "the observed cluster is not the authorized enrollment state",
                ", ".join(plan.failures) or "fail_closed",
            )
        if plan.state == E7:
            raise EnrollmentError(
                "already_enrolled",
                "the enrollment is complete; only Mode B verification is permitted",
            )

        actions: list[str] = []
        written: list[int] = []

        if plan.state == E0:
            identity = self.cluster.identity()
            observation = self.cluster.observe(read_target=True)
            plan = dispatch(view.records, observation)
            if plan.state != E0:
                raise EnrollmentError(
                    "enrollment_state_changed",
                    "the cluster changed before the intent could be recorded",
                    plan.state,
                )
            marker = make_marker(self.token_source(), identity.system_identifier)
            record = self.journal.append_if_head(
                view.head,
                view.head_seq,
                "cluster_enrollment",
                {
                    "mode": "A",
                    "state": "intent",
                    "approved_by": self.approved_by,
                    **_intent_payload(identity, marker, observation),
                },
                at=self.clock(),
            )
            if record is None:
                # The journal head moved after this run observed E0. Never write a second
                # intent: re-read and resume only the intent that is now durable.
                view = self.journal.read()
                plan = dispatch(view.records, self.cluster.observe(read_target=True))
                if plan.state == E0:
                    raise EnrollmentError(
                        "journal_head_changed",
                        "the enrollment journal changed before the intent was recorded",
                    )
                if plan.state == FAIL:
                    raise EnrollmentError(
                        "enrollment_refused",
                        "the journal changed and the cluster is not a resumable intent",
                        ", ".join(plan.failures),
                    )
                if plan.state == E7:
                    raise EnrollmentError(
                        "already_enrolled",
                        "another enrollment completed before this one recorded its intent",
                    )
            else:
                written.append(record.seq)
                actions.append(ACTION_BEGIN)
                view = self.journal.read()
                plan = dispatch(view.records, self.cluster.observe(read_target=True))
            if plan.state == FAIL:
                raise EnrollmentError(
                    "enrollment_refused",
                    "the cluster is not a resumable partial enrollment",
                    ", ".join(plan.failures),
                )

        # A milestone the journal has not recorded but the cluster already shows is
        # reconciled explicitly (ENR-12); the absence of a record is never inferred.
        iterations = 0
        if plan.intent is not None and plan.observed_rank > plan.recorded_rank:
            observed_name = {
                3: "roles",
                4: "database",
                5: "hardened",
                6: "marker",
            }.get(plan.observed_rank)
            if observed_name is not None:
                record = self._append_at_view(
                    view,
                    "reconcile",
                    {"observed_state": observed_name, "reason": "ENR-12 milestone reconciliation"},
                )
                written.append(record.seq)
                actions.append(f"reconcile:{observed_name}")

        while True:
            iterations += 1
            if iterations > 24:
                raise EnrollmentError(
                    "enrollment_stalled",
                    "the enrollment stopped advancing between observed states",
                )
            view = self.journal.read()
            observation = self.cluster.observe(read_target=True)
            plan = dispatch(view.records, observation)
            if plan.state == FAIL:
                raise EnrollmentError(
                    "enrollment_refused",
                    "the observed cluster is not the authorized enrollment state",
                    ", ".join(plan.failures) or "fail_closed",
                )
            if plan.state == E7:
                completed = _completed_records(view.records)[-1]
                return EnrollmentReport(
                    state=E7,
                    actions=tuple(actions),
                    marker=str(completed.payload.get("marker", "")),
                    system_identifier=str(completed.payload.get("system_identifier", "")),
                    records_written=tuple(written),
                )
            if plan.state in (E1, E2):
                self.cluster.apply_roles()
                after = self.cluster.observe(read_target=True)
                missing = [name for name in contract.PROJECT_ROLE_NAMES if name not in after.roles]
                if (
                    missing
                    or contract.project_role_failures(after.roles)
                    or contract.membership_failures(after.memberships, after.predefined_roles)
                    or contract.foreign_role_failures(
                        after.roles, after.predefined_roles
                    )
                    or contract.predefined_role_failures(after.predefined_roles)
                    or contract.predefined_membership_failures(
                        after.memberships, after.predefined_roles
                    )
                ):
                    raise EnrollmentError(
                        "role_contract",
                        "roles.sql did not leave the frozen role inventory",
                        ", ".join(missing),
                    )
                record = self._append_at_view(
                    view,
                    "cluster_enrollment",
                    {"mode": "A", "state": "roles", "observed": True},
                )
                written.append(record.seq)
                actions.append(ACTION_ROLES if plan.state == E1 else ACTION_RESUME_ROLES)
                continue
            if plan.state == E3:
                self.cluster.create_database()
                record = self._append_at_view(
                    view,
                    "cluster_enrollment",
                    {"mode": "A", "state": "database", "observed": True},
                )
                written.append(record.seq)
                actions.append(ACTION_CREATE_DATABASE)
                continue
            if plan.state == E4:
                facts = observation.database
                if facts is None or facts.allowconn is not True:
                    # E3-a/E3-b: finish the database-level phases. `db/bootstrap.sql`
                    # itself verifies E3-b before it enables connections.
                    self.cluster.harden_database()
                    self.cluster.enable_database()
                # F8/ENR-04: after E3-c, the unchanged `db/schema.sql` hardens `public`
                # and installs the ROL-14 default ACLs, as an authenticated `hh_migrate`
                # session with `current_user = hh_owner` (ROL-03/ROL-06). The adapter
                # re-observes the cluster identity and binds it to the intent before it
                # issues the schema authorization; a changed container/volume/image or
                # `system_identifier` is refused here, before any authorization exists.
                # A failure leaves the journal at the `database` milestone: resumable,
                # never E7.
                self.cluster.harden_schema(_intent_identity(plan.intent or {}))
                # Re-observe the combined result independently. The `hardened` milestone
                # is never recorded from the mutation calls, and never for database-level
                # hardening alone.
                hardened = self.cluster.observe(read_target=True)
                failures = _hardened_failures(hardened)
                if failures:
                    raise EnrollmentError(
                        "enrollment_refused",
                        "the database is not fully hardened; the milestone was not recorded",
                        ", ".join(failures)[:400],
                    )
                record = self._append_at_view(
                    view,
                    "cluster_enrollment",
                    {"mode": "A", "state": "hardened", "observed": True},
                )
                written.append(record.seq)
                actions.append(ACTION_FINISH_DATABASE)
                continue
            if plan.state == E5:
                intent_marker = (plan.intent or {}).get("marker")
                if not isinstance(intent_marker, str):
                    raise EnrollmentError("enrollment_refused", "the intent has no marker")
                self.cluster.write_marker(intent_marker)
                after = self.cluster.observe(read_target=True)
                contract.marker_matches(
                    after.marker,
                    marker=parse_marker(intent_marker)[0],
                    system_identifier=after.identity.system_identifier,
                )
                record = self._append_at_view(
                    view,
                    "cluster_enrollment",
                    {"mode": "A", "state": "marker", "observed": True},
                )
                written.append(record.seq)
                actions.append(ACTION_MARKER)
                continue
            if plan.state == E6:
                observation = self.cluster.observe(read_target=True)
                failures = _completion_failures(plan.intent or {}, observation)
                if failures:
                    raise EnrollmentError(
                        "enrollment_refused",
                        "the completed inventory could not be observed",
                        ", ".join(failures),
                    )
                record = self._append_at_view(
                    view,
                    "cluster_enrollment",
                    _completion_payload(plan.intent or {}, observation),
                )
                written.append(record.seq)
                actions.append(ACTION_COMPLETE)
                continue
            raise EnrollmentError("enrollment_state", "the enrollment reached an unknown state", plan.state)

def _completion_failures(intent: Mapping[str, Any], observation: Observation) -> tuple[str, ...]:
    failures = list(_pristine_project_state(observation))
    if contract.SERVING_DATABASE not in observation.databases:
        failures.append("database:missing")
    if observation.database is None or not observation.database.exists:
        failures.append("database:not_observed")
    else:
        facts = observation.database
        failures.extend(_hardened_failures(observation, allow_marker=True))
        if facts.owner != contract.OWNER_ROLE:
            failures.append("database:ownership")
        if facts.encoding != contract.EXPECTED_ENCODING:
            failures.append("database:encoding")
        if (facts.collate, facts.ctype, facts.provider) != contract.EXPECTED_LOCALE:
            failures.append("database:locale")
    if not observation.marker:
        failures.append("marker:absent")
    else:
        try:
            token, identifier = parse_marker(observation.marker)
        except JournalError as error:
            failures.append(f"marker:{error.code}")
        else:
            try:
                expected_token, expected_identifier = parse_marker(str(intent.get("marker", "")))
            except JournalError:
                failures.append("intent:marker")
            else:
                if token != expected_token or identifier != expected_identifier:
                    failures.append("marker:mismatch")
                if identifier != observation.identity.system_identifier:
                    failures.append("marker:system_identifier")
    failures.extend(contract.project_role_failures(observation.roles))
    failures.extend(
        contract.foreign_role_failures(observation.roles, observation.predefined_roles)
    )
    failures.extend(contract.predefined_role_failures(observation.predefined_roles))
    failures.extend(
        contract.membership_failures(observation.memberships, observation.predefined_roles)
    )
    failures.extend(
        contract.predefined_membership_failures(
            observation.memberships, observation.predefined_roles
        )
    )
    return tuple(failures)


def _pristine_project_state(observation: Observation) -> Iterable[str]:
    """The completion observation must be the E6 inventory, not an arbitrary database."""
    failures = []
    for name in set(observation.databases) - set(contract.BASE_DATABASES) - {
        contract.SERVING_DATABASE
    }:
        failures.append(f"unexpected_database:{name}")
    if observation.target_inventory is None:
        failures.append("target:not_observed")
    else:
        # F8: the ENR-04 schema/default-ACL half is part of the completed enrollment, so
        # the `pg_default_acl` objects it installs are authorized; an application relation
        # is still foreign (it belongs to the migrate executor, reached only through Mode B).
        acl_failures = contract.default_acl_failures(observation.default_acls)
        failures.extend(
            contract.authorized_inventory_failures(
                observation.target_inventory,
                default_acls_ok=not acl_failures,
                alembic_initialized=False,
            )
        )
    return failures


def _completion_payload(intent: Mapping[str, Any], observation: Observation) -> Mapping[str, Any]:
    facts = observation.database
    assert facts is not None
    explicit_edges = [
        {
            "member": edge.member,
            "granted": edge.granted,
            "admin": edge.admin,
            "inherit": edge.inherit,
            "set": edge.set_option,
        }
        for edge in observation.memberships
    ]
    return {
        "mode": "A",
        "state": "completed",
        "system_identifier": observation.identity.system_identifier,
        "container": {
            "container_name": observation.identity.container_name,
            "container_id": observation.identity.container_id,
            "image_digest": observation.identity.image_digest,
        },
        "volume": observation.identity.volume,
        "databases": [
            {
                "name": contract.SERVING_DATABASE,
                "owner": facts.owner,
                "encoding": facts.encoding,
                "collate": facts.collate,
                "ctype": facts.ctype,
                "allowconn": facts.allowconn,
            }
        ],
        "roles": [
            {
                "name": name,
                "login": observation.roles[name].login,
                "superuser": observation.roles[name].superuser,
                "createdb": observation.roles[name].createdb,
                "createrole": observation.roles[name].createrole,
                "inherit": observation.roles[name].inherit,
                "replication": observation.roles[name].replication,
                "bypassrls": observation.roles[name].bypassrls,
                "settings": list(observation.roles[name].settings),
            }
            for name in contract.PROJECT_ROLE_NAMES
        ],
        "predefined_roles": list(observation.predefined_roles),
        "predefined_memberships": [
            {
                "member": edge.member,
                "granted": edge.granted,
                "admin": edge.admin,
                "inherit": edge.inherit,
                "set": edge.set_option,
            }
            for edge in observation.predefined_memberships
        ],
        "explicit_memberships": explicit_edges,
        "effective_membership": [
            {"login": login, "role": role, "inquiry": inquiry, "result": bool(result)}
            for login, role, inquiry, result in observation.effective_membership
        ],
        "marker": observation.marker,
        "approved_by": intent.get("approved_by", ""),
    }


# --- Mode B ----------------------------------------------------------------------------------


def mode_b(records: Iterable[Record], observation: Observation) -> ModeBVerdict:
    """The authoritative later check. Read-only; the caller journals a mismatch."""
    records = tuple(records)
    completes = _completed_records(records)
    if len(completes) != 1:
        return ModeBVerdict(
            ok=False,
            mismatches=("completed_record:missing" if not completes else "completed_record:duplicate",),
            completed=None,
            observed=observation,
            journal_seq=records[-1].seq if records else -1,
        )
    completed = completes[0]
    payload = completed.payload
    mismatches: list[str] = []
    container = payload.get("container") or {}
    identity = ClusterIdentity(
        container_name=str(container.get("container_name", "")),
        container_id=str(container.get("container_id", "")),
        image_digest=str(container.get("image_digest", "")),
        volume=str(payload.get("volume", "")),
        system_identifier=str(payload.get("system_identifier", "")),
    )
    if identity.container_name != observation.identity.container_name:
        mismatches.append("container:name")
    if identity.container_id != observation.identity.container_id:
        mismatches.append("container:id")
    if identity.image_digest != observation.identity.image_digest:
        mismatches.append("container:image_digest")
    if identity.volume != observation.identity.volume:
        mismatches.append("volume")
    if str(payload.get("system_identifier")) != observation.identity.system_identifier:
        mismatches.append("system_identifier")
    bootstrap = observation.roles.get(contract.BOOTSTRAP_ROLE)
    if bootstrap is None or not bootstrap.superuser:
        mismatches.append("bootstrap_superuser")
    for name in contract.BASE_DATABASES:
        if name not in observation.databases:
            mismatches.append(f"missing_database:{name}")

    try:
        token, recorded_identifier = parse_marker(payload.get("marker"))
    except JournalError as error:
        mismatches.append(f"completed_marker:{error.code}")
    else:
        try:
            live_token, live_identifier = parse_marker(observation.marker)
        except JournalError as error:
            mismatches.append(
                "marker:absent" if error.code == "marker_absent" else f"marker:{error.code}"
            )
        else:
            if live_token != token:
                mismatches.append("marker:token")
            if live_identifier != recorded_identifier:
                mismatches.append("marker:system_identifier")
            if live_identifier != observation.identity.system_identifier:
                mismatches.append("marker:live_system_identifier")
        if recorded_identifier != observation.identity.system_identifier:
            mismatches.append("recorded_system_identifier")

    mismatches.extend(contract.project_role_failures(observation.roles))
    mismatches.extend(
        contract.foreign_role_failures(observation.roles, observation.predefined_roles)
    )
    mismatches.extend(contract.predefined_role_failures(observation.predefined_roles))
    mismatches.extend(
        contract.membership_failures(observation.memberships, observation.predefined_roles)
    )
    mismatches.extend(
        contract.predefined_membership_failures(
            observation.memberships, observation.predefined_roles
        )
    )
    expected_predefined = payload.get("predefined_roles")
    if isinstance(expected_predefined, list):
        if sorted(str(name) for name in expected_predefined) != sorted(observation.predefined_roles):
            mismatches.append("predefined_roles")
    recorded_predefined_edges = payload.get("predefined_memberships")
    if isinstance(recorded_predefined_edges, list):
        live_edges = [
            {
                "member": edge.member,
                "granted": edge.granted,
                "admin": edge.admin,
                "inherit": edge.inherit,
                "set": edge.set_option,
            }
            for edge in observation.predefined_memberships
        ]
        if recorded_predefined_edges != live_edges:
            mismatches.append("predefined_memberships")
    recorded_effective = payload.get("effective_membership")
    if isinstance(recorded_effective, list):
        live_effective = [
            {"login": login, "role": role, "inquiry": inquiry, "result": bool(result)}
            for login, role, inquiry, result in observation.effective_membership
        ]
        if recorded_effective != live_effective:
            mismatches.append("effective_membership")
    # TMP-01: the expected inventory is the enrolled base plus the exact databases
    # authorized by active (non-retired) temporary-database operation records. A name
    # prefix is never evidence; only an exact record broadens the inventory, and a
    # record for another cluster's system_identifier authorizes nothing.
    from house_helper.persistence import pg_backup

    authorized = pg_backup.active_temporary_databases(records)
    valid_temp: set[str] = set()
    for name, temp_payload in sorted(authorized.items()):
        if temp_payload.get("system_identifier") != observation.identity.system_identifier:
            mismatches.append(f"temp_database_identity:{name}")
        else:
            valid_temp.add(name)
    allowed_databases = (
        set(contract.BASE_DATABASES) | {contract.SERVING_DATABASE} | valid_temp
    )
    for name in sorted(set(observation.databases) - allowed_databases):
        mismatches.append(f"unexpected_database:{name}")
    if contract.SERVING_DATABASE not in observation.databases:
        mismatches.append("database:missing")
    if observation.database is None or not observation.database.exists:
        mismatches.append("database:not_observed")
    else:
        if observation.database.owner != contract.OWNER_ROLE:
            mismatches.append("database:ownership")
        if observation.database.encoding != contract.EXPECTED_ENCODING:
            mismatches.append("database:encoding")
        if (
            observation.database.collate,
            observation.database.ctype,
            observation.database.provider,
        ) != contract.EXPECTED_LOCALE:
            mismatches.append("database:locale")
        if observation.database.allowconn is not True:
            mismatches.append("database:not_connectable")
    # F6: state-specific authorized inventory. Initial enrollment admits only the
    # untouched baseline; a completed/upgrade-required schema admits exactly this image's
    # objects, derived toast storage and the ROL-14 default-ACL matrix; the recognized
    # partial state (schema contract committed, Alembic chain not) admits the exact
    # default-ACL matrix and no relation. Anything else stays foreign and fails closed.
    _state, state_failures = contract.classify_schema_state(
        versions=observation.schema_versions,
        public_tables=observation.public_tables,
        schema_contract_present=bool(observation.default_acls),
    )
    mismatches.extend(state_failures)
    initialized = bool(observation.schema_versions) or bool(observation.public_tables)
    if initialized:
        mismatches.extend(contract.default_acl_failures(observation.default_acls))
    elif observation.default_acls:
        # A schema-only partial state is recognized only when its default-ACL matrix is
        # exactly the frozen one; otherwise schema work appeared without authority.
        mismatches.extend(contract.default_acl_failures(observation.default_acls))
    acl_ok = not contract.default_acl_failures(observation.default_acls)
    mismatches.extend(
        contract.authorized_inventory_failures(
            observation.target_inventory,
            default_acls_ok=acl_ok,
            # F6: the authorized relation set depends on the OBSERVED Alembic version
            # table, so the schema-only partial state cannot smuggle in a relation.
            alembic_initialized=bool(observation.schema_versions),
        )
    )
    if _state == contract.SCHEMA_HEAD and observation.target_inventory is not None:
        # At the head, every named relation this image's models define must exist. A
        # missing index or sequence is schema drift, not an "upgrade candidate".
        present = {
            name
            for class_, schema, name in observation.target_inventory.entries
            if class_ == "relation" and schema == "public"
        }
        for name in sorted(contract.expected_relation_names() - present):
            mismatches.append(f"missing_relation:{name}")
    if observation.database is not None and observation.database.subscriptions:
        mismatches.append("subscriptions")

    return ModeBVerdict(
        ok=not mismatches,
        mismatches=tuple(dict.fromkeys(mismatches)),
        completed=payload,
        observed=observation,
        journal_seq=completed.seq,
        journal_head=records[-1].digest if records else GENESIS,
    )


def record_mismatch(journal: Journal, verdict: ModeBVerdict, *, clock=None) -> Record:
    """ENR-16: name the failed item, never a credential; never fall back to Mode A."""
    return journal.append(
        "cluster_identity",
        {"state": "mismatch", "items": list(verdict.mismatches)},
        at=(clock or utcnow)(),
    )


# --- LCK-08: an intent without an observed outcome is UNKNOWN -------------------------------


def migration_outcomes(records: Iterable[Record]) -> Mapping[str, Mapping[str, Any]]:
    """The last outcome record per migrate run id."""
    outcomes: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if record.kind != "migration" or record.payload.get("state") != "outcome":
            continue
        run_id = record.payload.get("run_id")
        if isinstance(run_id, str):
            outcomes[run_id] = record.payload
    return outcomes


def pending_migration_unknown(records: Iterable[Record]) -> Optional[Record]:
    """The newest migrate intent whose outcome is missing or explicitly UNKNOWN.

    LCK-08: an `intent` without an `outcome` is UNKNOWN, never "did not happen". A later
    `unknown` outcome stays pending until a reconciliation record observes the cluster.
    """
    records = tuple(records)
    outcomes = migration_outcomes(records)
    pending: Optional[Record] = None
    for record in records:
        if record.kind != "migration" or record.payload.get("state") != "intent":
            continue
        run_id = record.payload.get("run_id")
        if not isinstance(run_id, str):
            pending = record
            continue
        outcome = outcomes.get(run_id)
        if outcome is None or outcome.get("outcome") == "unknown":
            pending = record
    return pending


# The Docker container states that mean "this worker may still be executing". Any state
# outside both sets is not interpreted at all: an unrecognized lifecycle is UNKNOWN.
WORKER_ACTIVE_STATES = frozenset(
    {"created", "running", "restarting", "paused", "removing"}
)
WORKER_TERMINATED_STATES = frozenset({"exited", "dead"})

# The frozen Compose identity the migrate worker is launched under (F10). The container
# name is deterministic from the run id, so evidence can be bound to exactly one run.
MIGRATE_PROJECT = contract.DEPLOYMENT_COMPOSE_PROJECT
MIGRATE_SERVICE = contract.MIGRATE_SERVICE_NAME
_WORKER_RUN_ID = re.compile(r"[0-9a-f]{32}")
_WORKER_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


def worker_container_name(run_id: str) -> str:
    """The exact Compose container name a migrate run launches (F10).

    `docker compose run` would otherwise pick an unpredictable suffix. The run id is the
    same bounded 32-hex value the authorization carries, so an observed container can be
    attributed to one run - and a retained container from another run can never be
    mistaken for the current worker.
    """
    if not isinstance(run_id, str) or not _WORKER_RUN_ID.fullmatch(run_id):
        raise EnrollmentError("worker_binding", "the migrate run id is malformed")
    return f"{MIGRATE_PROJECT}-{MIGRATE_SERVICE}-run-{run_id}"


@dataclass(frozen=True)
class WorkerContainer:
    """One observed migrate one-shot container, from `docker ps -a` + `docker inspect`.

    Every field is observed, never supplied by the caller's intent. `exit_code` is the
    container's terminal execution outcome; `None` means it could not be established.
    """

    container_id: str = ""
    name: str = ""
    state: str = ""
    project: str = ""
    service: str = ""
    exit_code: Optional[int] = None


@dataclass(frozen=True)
class WorkerEvidence:
    """The observed lifecycle of THE migrate run's one-shot container (F10).

    `run_id`, `container_name`, `project` and `service` bind the evidence to the
    previously launched run. `observed` contains every container the adapter saw with its
    observed identity; the resolver itself filters on the exact expected name and checks
    each entry's Compose project/service. It is evidence only: an empty tuple is not "no
    worker", it is "no observation", and it never releases UNKNOWN. Docker's client
    process ending (a timeout or an interrupt) is deliberately NOT evidence.
    """

    run_id: str = ""
    container_name: str = ""
    project: str = ""
    service: str = ""
    observed: tuple[WorkerContainer, ...] = ()


def worker_termination_failures(
    evidence: WorkerEvidence, *, run_id: Optional[str] = None
) -> tuple[str, ...]:
    """Why the evidence does not positively establish worker termination.

    The resolver enforces the binding itself, without trusting a caller (or the CLI
    adapter) to have filtered correctly (F10):

    * the evidence's run id must be the pending intent's run id;
    * the container name must be the exact deterministic name for that run;
    * the Compose project and service must be the frozen `migrate` worker's;
    * a container with that exact name must have been observed under that project and
      service, with a full container id, a terminal lifecycle state (`exited`/`dead`)
      and an established terminal exit code.

    A running (or created/restarting) container is never reconciled. An absent container
    is ambiguous - it may never have been created - and is not proof. Containers under
    other names are ignored: an old retained container neither authorizes nor blocks the
    current run.
    """
    bound_run = evidence.run_id if run_id is None else run_id
    if not isinstance(bound_run, str) or not _WORKER_RUN_ID.fullmatch(bound_run):
        return ("worker:unbound",)
    failures: list[str] = []
    if evidence.run_id != bound_run:
        failures.append("worker:run_mismatch")
    expected_name = worker_container_name(bound_run)
    if evidence.container_name != expected_name:
        failures.append("worker:name_mismatch")
    if evidence.project != MIGRATE_PROJECT:
        failures.append("worker:project_mismatch")
    if evidence.service != MIGRATE_SERVICE:
        failures.append("worker:service_mismatch")

    matched = tuple(
        entry
        for entry in evidence.observed
        if isinstance(entry, WorkerContainer) and entry.name == expected_name
    )
    if not matched:
        failures.append("worker:not_observed")
    if len(matched) > 1:
        # A container name is unique in Docker; two rows with it are not one worker.
        failures.append("worker:ambiguous")
    for entry in matched:
        if not _WORKER_CONTAINER_ID.fullmatch(entry.container_id or ""):
            failures.append("worker:unrecognized_identity")
            continue
        if entry.project != MIGRATE_PROJECT or entry.service != MIGRATE_SERVICE:
            failures.append("worker:identity_mismatch")
        state = str(entry.state)
        if state in WORKER_ACTIVE_STATES:
            failures.append("worker:active")
        elif state not in WORKER_TERMINATED_STATES:
            failures.append(f"worker:unrecognized:{state}")
        elif isinstance(entry.exit_code, bool) or not isinstance(entry.exit_code, int):
            failures.append("worker:outcome_unknown")
    return tuple(dict.fromkeys(failures))


def _remain_unknown(
    journal: Journal, run_id: str, evidence: str, clock
) -> None:
    """Record that the UNKNOWN was NOT resolved, with a non-secret reason class."""
    journal.append(
        "migration",
        {
            "state": "outcome",
            "run_id": run_id,
            "outcome": "unknown",
            "evidence": evidence,
        },
        at=(clock or utcnow)(),
    )


def reconcile_migration(
    journal: Journal,
    observation: Observation,
    *,
    worker: WorkerEvidence,
    clock=None,
) -> Record:
    """Resolve an UNKNOWN migrate outcome from a fresh observation AND worker evidence.

    F10: an unchanged `alembic_version` is never proof that the timed-out worker did not
    start or has stopped. The UNKNOWN is released only when:

    1. the observation is the ENROLLED cluster - the full Mode B identity and inventory
       check, not merely the schema revision;
    2. the worker lifecycle evidence is bound to this intent's run id and the exact
       deterministic container name `house-helper-pg-migrate-run-<run_id>`, carries the
       frozen Compose project/service identity, has a full container id, an established
       exit code, and is exited/dead; and
    3. the observed schema state is exactly the pre-migration revision (nothing
       committed) or the image head (the whole transactional chain committed).

    Anything else appends an unresolved outcome and keeps UNKNOWN, which continues to
    block another migrate. Automatic recovery is deliberately not invented where the
    one-shot lifecycle cannot supply the evidence.
    """
    view = journal.read()
    intent = pending_migration_unknown(view.records)
    if intent is None:
        raise EnrollmentError(
            "migration_not_pending", "there is no unresolved migrate intent to reconcile"
        )
    run_id = str(intent.payload.get("run_id", ""))

    # 1. Full cluster identity and database state. A changed container/volume/image,
    #    identifier, marker or inventory is not the enrolled cluster; it is never resolved
    #    from a revision that happens to look right.
    verdict = mode_b(view.records, observation)
    if not verdict.ok:
        detail = ", ".join(verdict.mismatches)
        _remain_unknown(
            journal,
            run_id,
            "the observation is not the enrolled cluster: " + detail[:300],
            clock,
        )
        raise EnrollmentError(
            "cluster_identity",
            "the reconciliation observation is not the enrolled cluster",
            detail[:400],
        )

    # 2. Positive worker termination, bound by the resolver itself to THIS intent's run
    #    id, its exact deterministic container name and the frozen Compose project/service.
    #    The Docker client's exit is not evidence; the worker's container lifecycle is.
    worker_failures = worker_termination_failures(worker, run_id=run_id)
    if worker_failures:
        _remain_unknown(
            journal,
            run_id,
            "worker termination is not positively established: "
            + ", ".join(worker_failures),
            clock,
        )
        raise EnrollmentError(
            "migration_unknown",
            "the prior worker's termination is not positively established; the outcome "
            "remains UNKNOWN and another migrate stays blocked",
            ", ".join(worker_failures),
        )

    # 3. The transactional outcome.
    from_revision = str(intent.payload.get("from_revision", ""))
    head, _chart = contract.known_revision_chart()
    versions = tuple(observation.schema_versions)
    observed = versions[0] if len(versions) == 1 else ""
    if versions == (head,):
        outcome = "reconciled_completed"
    elif versions == (from_revision,) or (not versions and not from_revision):
        outcome = "reconciled_not_started"
    else:
        _remain_unknown(
            journal,
            run_id,
            "the observed schema state is neither the pre-migration revision nor the "
            "image head (observed revision " + (observed or "<none>") + ")",
            clock,
        )
        raise EnrollmentError(
            "migration_unknown",
            "the observed schema state does not resolve the interrupted migrate run",
            "observed revision " + (observed or "<none>"),
        )
    return journal.append(
        "migration",
        {
            "state": "outcome",
            "run_id": run_id,
            "outcome": outcome,
            "evidence": (
                "worker container observed exited; observed revision "
                + (observed or "<none>")
            ),
        },
        at=(clock or utcnow)(),
    )


# --- the bootstrap credential retirement plan (designed, never executed) ---------------------


BOOTSTRAP_RETIREMENT_PLAN = (
    "Verify Mode B passes and the bootstrap superuser answers on the LOCAL socket only "
    "(docker exec -u postgres ... psql -U postgres), with no remote superuser line in pg_hba.conf.",
    "Record a migration/retirement intent in the host journal before changing the surface.",
    "Run ALTER ROLE postgres PASSWORD NULL through the local peer connection; the superuser "
    "keeps local peer access and no password verifier remains in pg_authid.",
    "Verify rolpassword IS NULL for postgres and that a remote TCP superuser attempt is refused "
    "by pg_hba.conf (the explicit reject line precedes the scram line).",
    "Remove POSTGRES_PASSWORD_FILE from the running compose surface and delete the host "
    "postgres_superuser.password file; do not pass it to any one-shot.",
    "Re-run Mode B and record the observed outcome. Do not rotate or populate any other role's "
    "credential: OD-8 remains the Owner's open decision.",
)


def bootstrap_password_retirement_plan() -> tuple[str, ...]:
    """The ordered plan. This function returns text; it has no database or Docker handle."""
    return BOOTSTRAP_RETIREMENT_PLAN
