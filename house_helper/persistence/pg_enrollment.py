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
    for name in sorted(
        set(observation.databases) - set(contract.BASE_DATABASES) - {contract.SERVING_DATABASE}
    ):
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
