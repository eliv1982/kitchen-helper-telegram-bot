"""Offline host-tool tests with a controlled Docker/psql fixture (HH-DB-01 WP-2C-A).

The fixture answers the exact command lines `deploy/postgres/enroll.py` issues and mutates
its cluster state only when the adapter's SQL runs. It is a fixture, not a database: no
Docker daemon and no PostgreSQL server are contacted.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from deploy.postgres import enroll
from deploy.postgres import maintenance
from house_helper.persistence import pg_contract as contract
from house_helper.persistence import pg_enrollment as enrollment
from house_helper.persistence import pg_journal as journal

SPEC = enroll.ClusterSpec(
    project="house-helper-pg",
    container_name="house-helper-db",
    image="postgres@sha256:" + "a" * 64,
    volume="house-helper-pgdata",
    network="house-helper-pg-db",
    credentials_directory="/srv/house-helper/postgres/credentials",
    state_volume="house-helper-pg-state",
    postgresql_conf_source="/srv/house-helper/postgres/postgresql.conf",
    pg_hba_source="/srv/house-helper/postgres/pg_hba.conf",
    bootstrap_credential_source="/srv/house-helper/postgres/credentials/postgres_superuser.password",
)
CONTAINER_ID = "b" * 64
WEB_ID = "d" * 64
SID = "726485930221"
MARKER = journal.make_marker("f" * 32, SID)


def _expected_default_acls():
    return tuple(("hh_owner",) + entry for entry in contract.EXPECTED_DEFAULT_ACLS)


def _schema_inventory():
    return [("default acl", None, None, 16400, None, None, None)]


def _public_schema_acl(*, hardened: bool) -> tuple:
    usage = tuple(True for _ in contract.PROJECT_ROLE_NAMES)
    if hardened:
        return ("hh_owner", 0) + usage + tuple(
            role == contract.OWNER_ROLE for role in contract.PROJECT_ROLE_NAMES
        )
    return ("pg_database_owner", 2) + usage + tuple(
        True for _ in contract.PROJECT_ROLE_NAMES
    )


def _head_inventory():
    """The catalog rows a completed migration would leave (F6: pkc included)."""
    rows = _schema_inventory() + [
        ("schema", None, "public", 2200, None, None, None)
    ]
    for index, name in enumerate(sorted(contract.expected_relation_names())):
        rows.append(("relation", 2200, name, 20000 + index, "r", None, None))
    return rows


def _out(rows) -> bytes:
    return "".join("\x1f".join(str(value) for value in row) + "\n" for row in rows).encode()


class FakeDocker:
    """A stateful fixture for the exact argv the host tool issues."""

    def __init__(self) -> None:
        self.container_id = CONTAINER_ID
        self.container_name = "/house-helper-db"
        self.image = SPEC.image
        self.volume = SPEC.volume
        self.network = SPEC.network
        self.published_port = False
        self.extra_mount = False
        self.databases = {
            "postgres": {"allowconn": True, "acl": False, "marker": "", "oid": 5},
            "template0": {"allowconn": False, "acl": False, "marker": "", "oid": 6},
            "template1": {"allowconn": True, "acl": False, "marker": "", "oid": 1},
        }
        self.roles = {"postgres": _role_row("postgres", superuser=True)}
        for name in contract.FROZEN_PREDEFINED_ROLES:
            self.roles[name] = _role_row(name, login=False)
        self.settings = {}
        self.edges = [
            (member, granted, "f", "t" if inherit else "f", "t" if set_option else "f")
            for member, granted, _admin, inherit, set_option in (
                contract.FROZEN_PREDEFINED_MEMBERSHIPS
            )
        ]
        self.acl_entries = []
        self.marker = ""
        self.inventory = []
        self.schema_versions = ()
        self.public_tables = ()
        self.default_acls = []
        self.schema_acl = _public_schema_acl(hardened=False)
        # `SESSIONS_SQL` rows `(datname, pid, usename, backend_type)`. Empty is the quiet
        # cluster; ordinary Web sessions must not break an observation (C7-P3).
        self.sessions = []
        self.compose_input = None
        self.compose_argv = None
        self.compose_env = None
        self.compose_returncode = 0
        self.compose_exception = None
        self.on_compose = None
        self.operations = []
        # F10: observed containers as docker ps -a rows plus their inspect answer.
        self.worker_containers = []
        self.migrate_name = None
        self.rm_argv = []
        self.rm_returncode = 0
        self.on_rm = None
        # A handoff test hook: called with every SQL text before it is answered.
        self.on_sql = None
        self.grants = []
        # The tagged handoff transport. `on_grant(tag, sql)` selects a behaviour; the
        # default applies the grant and returns psql exit 0. The per-operation evidence
        # models what the container-side wrapper would have left behind.
        self.on_grant = None
        self.handoff_transports = {}
        self.delayed = {}
        # A hook that runs once, immediately before the first handoff exec (Finding 5).
        self.on_handoff_exec = None
        # A delayed GRANT that commits between the ACL readback and the probe (Finding 3).
        self.commit_on_acl_read = None
        self._pending_commit = None
        # Counts calls to the complete-ACL read; tests may make the Nth observation fail.
        self.acl_matrix_calls = 0
        self.acl_matrix_fail_on_call = None
        # Every container target the tool actually exec'ed or inspected.
        self.exec_targets = []
        self.inspect_targets = []
        self.handoff_exec_targets = []
        # HOF-03 item 4: the web service row (None = not running / not created) and the
        # frozen `check-idle` answer. The answer is `(exit, stderr)`; a callable is called
        # with the target and returns the answer, so a test can decide per observation.
        # The lock-ownership proof itself is read from a fixture /proc tree by
        # `deploy.postgres.lock_owner`.
        self.web_container = None
        self.lock_answers = {
            "idle": (3, b"refused [maintenance_lock_busy]: the state volume is held"),
        }
        self.lock_probe_targets = []
        self.state_mountpoint = None
        # The host mountpoint is validated once per lock observation; both snapshots and
        # the witness are given that same path.
        self.state_volume_inspections = 0

    # -- the runner interface ----------------------------------------------------------

    def __call__(self, argv, *, input=None, capture_output=True, timeout=None, check=False, env=None):
        if argv[:3] == ["docker", "inspect", "--type"]:
            target = argv[-1]
            self.inspect_targets.append(target)
            # An immutable id resolves only while THAT container exists. A replacement
            # container with the same name gets a new id, so the old id is gone.
            if target == self.container_id:
                return self._json([self._container()])
            if target in (
                self.container_name,
                self.container_name.lstrip("/"),
                SPEC.container_name,
            ):
                return self._json([self._container()])
            if self.web_container is not None and target == self.web_container["Id"]:
                return self._json([self._web_inspect()])
            for worker in self.worker_containers:
                if target in (worker["Id"], worker["Names"]):
                    return self._json([self._worker_inspect(worker)])
            return subprocess.CompletedProcess(argv, 1, b"", b"no such container")
        if argv[:3] == ["docker", "volume", "inspect"]:
            target = argv[-1]
            if target == SPEC.state_volume:
                self.state_volume_inspections += 1
                return self._json(
                    [
                        {
                            "Name": SPEC.state_volume,
                            "Driver": "local",
                            "Scope": "local",
                            "Mountpoint": str(self.state_mountpoint),
                        }
                    ]
                )
            return self._json(
                [{"Name": self.volume, "Driver": "local", "Scope": "local"}]
            )
        if argv[:3] == ["docker", "image", "inspect"]:
            return self._json(
                [{"Id": "sha256:" + "c" * 64, "RepoDigests": [self.image]}]
            )
        if argv[:2] == ["docker", "ps"]:
            if any("com.docker.compose.project" in part for part in argv):
                rows = "".join(
                    f"{worker['Id']}\t{worker['State']}\t{worker['Names']}\t"
                    f"{worker['Project']}\t{worker['Service']}\n"
                    for worker in self.worker_containers
                )
                return subprocess.CompletedProcess(argv, 0, rows.encode(), b"")
            return subprocess.CompletedProcess(argv, 0, (self.container_id + "\n").encode(), b"")
        if argv[:2] == ["docker", "rm"]:
            self.rm_argv.append(list(argv))
            if self.on_rm is not None:
                self.on_rm()
            return subprocess.CompletedProcess(argv, self.rm_returncode, b"", b"")
        if argv[:2] == ["docker", "exec"] and enroll.DBTOOL_PATH in argv:
            # HOF-03 item 4: `docker exec <web id> python <dbtool> check-idle`. The
            # authoritative lock ownership is read separately from the fixture /proc.
            target = argv[2]
            self.exec_targets.append(target)
            self.lock_probe_targets.append(target)
            if self.web_container is None or target != self.web_container["Id"]:
                return subprocess.CompletedProcess(argv, 1, b"", b"no such container")
            which = "idle" if "check-idle" in argv else "unexpected"
            answer = self.lock_answers[which]
            if callable(answer):
                answer = answer(target)
            code, stderr = answer
            return subprocess.CompletedProcess(argv, code, b"", stderr)
        if argv[:2] == ["docker", "exec"]:
            # `docker exec [-i] -u postgres <target> ...`: record the target and refuse an
            # immutable id that no longer names the live container (a replacement).
            kind = self._handoff_kind(argv)
            if self.on_handoff_exec is not None and kind is not None:
                hook = self.on_handoff_exec
                self.on_handoff_exec = None
                hook()
            target = argv[argv.index("-u") + 2]
            self.exec_targets.append(target)
            if kind is not None:
                self.handoff_exec_targets.append(target)
            if target != self.container_name and target != self.container_name.lstrip(
                "/"
            ):
                if target != self.container_id:
                    return subprocess.CompletedProcess(
                        argv, 1, b"", b"no such container"
                    )
            # The two handoff operations run a fixed container-side script; neither is a
            # psql observation. They are recognised by their fixed `$0` argument.
            if kind == "grant":
                return self._grant_wrapper(argv, input)
            if kind == "probe":
                return self._handoff_probe(self._probe_tag(argv))
            database = argv[argv.index("-d") + 1]
            return subprocess.CompletedProcess(argv, 0, self._psql(database, input.decode()), b"")
        if argv[:2] == ["docker", "compose"]:
            self.compose_input = input
            self.compose_argv = list(argv)
            self.compose_env = dict(env or {})
            if "--name" in argv:
                self.migrate_name = argv[argv.index("--name") + 1]
            if self.on_compose is not None:
                self.on_compose()
            if self.compose_exception is not None:
                raise self.compose_exception
            if argv[-1] == "schema":
                self.operations.append("schema")
                self.default_acls = list(_expected_default_acls())
                self.inventory = _schema_inventory()
                self.schema_acl = _public_schema_acl(hardened=True)
                return subprocess.CompletedProcess(argv, self.compose_returncode, b"ok [schema]\n", b"")
            return subprocess.CompletedProcess(argv, self.compose_returncode, b"ok [migrate]\n", b"")
        raise AssertionError(f"unexpected command {argv}")

    @staticmethod
    def _json(value):
        return subprocess.CompletedProcess([], 0, json.dumps(value).encode(), b"")

    @staticmethod
    def _handoff_kind(argv):
        if len(argv) >= 2 and argv[-2] == enroll.HANDOFF_GRANT_WRAPPER_NAME:
            return "grant"
        if argv and argv[-1] == enroll.HANDOFF_PROBE_NAME:
            return "probe"
        return None

    @staticmethod
    def _probe_tag(argv):
        for index, value in enumerate(argv[:-1]):
            if value == "-e" and argv[index + 1].startswith("HANDOFF_PROBE_TAG="):
                return argv[index + 1].split("=", 1)[1]
        raise AssertionError(f"the probe did not carry HANDOFF_PROBE_TAG: {argv}")

    # -- the tagged handoff transport -------------------------------------------------

    def _transport(self, tag, **values):
        state = {
            "started": False,
            "stopped": False,
            "psql_exit": None,
            "client_alive": False,
            "session_alive": False,
        }
        state.update(values)
        self.handoff_transports[tag] = state
        return state

    def _grant_wrapper(self, argv, stdin):
        sql = (stdin or b"").decode("utf-8")
        tag = argv[-1]
        mode = "apply" if self.on_grant is None else self.on_grant(tag, sql)
        if isinstance(mode, tuple) and mode[0] == "client_status":
            # The Docker client returns (2, -15, 124, 125, ...) while the tagged operation
            # is still active. The return must never be read as completion.
            self._transport(tag, started=True, client_alive=True, session_alive=True)
            self.delayed[tag] = sql
            return subprocess.CompletedProcess(argv, mode[1], b"", b"")
        if isinstance(mode, tuple) and mode[0] == "delayed_readback":
            # The GRANT commits in the window between an early ACL readback and the probe.
            self._transport(tag, started=True, client_alive=True, session_alive=True)
            self.delayed[tag] = sql
            self.commit_on_acl_read = (tag, sql, mode[1])
            return subprocess.CompletedProcess(argv, 2, b"", b"")
        if mode in ("timeout_inflight", "delayed_inflight"):
            self._transport(tag, started=True, client_alive=True)
            if mode == "delayed_inflight":
                self.delayed[tag] = sql
            raise subprocess.TimeoutExpired(argv, 120)
        if mode == "interrupt":
            self._transport(tag, started=True, client_alive=True)
            raise KeyboardInterrupt
        if mode == "server_refused":
            self._transport(tag, started=True, stopped=True, psql_exit=3)
            return subprocess.CompletedProcess(argv, 3, b"", b"ERROR: permission denied")
        if mode == "commit_then_client_failure":
            role = sql.split('TO "', 1)[1].split('"', 1)[0]
            if role not in self.grants:
                self.grants.append(role)
            self._grant_connect(role)
            self._transport(tag, started=True, stopped=True, psql_exit=2)
            return subprocess.CompletedProcess(argv, 2, b"", b"connection lost")
        self._psql("postgres", sql)
        self._transport(tag, started=True, stopped=True, psql_exit=0)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def _handoff_probe(self, tag):
        pending = self._pending_commit
        if pending is not None and pending[0] == tag:
            # Finding 3: the delayed GRANT lands after the ACL snapshot and before this
            # probe. A reader that probed after its snapshot would see termination plus a
            # stale pre-state and could resolve the operation incorrectly.
            _tag, _sql, exit_code = pending
            self._pending_commit = None
            self.complete_delayed(tag, apply=True, psql_exit=exit_code)
        state = self.handoff_transports.get(tag, {})
        exit_code = state.get("psql_exit")
        lines = [
            f"started={1 if state.get('started') else 0}",
            f"stopped={1 if state.get('stopped') else 0}",
            f"rc={'' if exit_code is None else exit_code}",
            f"client={1 if state.get('client_alive') else 0}",
        ]
        return subprocess.CompletedProcess([], 0, ("\n".join(lines) + "\n").encode(), b"")

    def complete_delayed(self, tag, *, apply=True, psql_exit=0):
        """Model the delayed operation finally terminating (Finding 1)."""
        sql = self.delayed.pop(tag)
        if apply:
            role = sql.split('TO "', 1)[1].split('"', 1)[0]
            if role not in self.grants:
                self.grants.append(role)
            self._grant_connect(role)
        self._transport(tag, started=True, stopped=True, psql_exit=psql_exit)

    def _container(self):
        mounts = [
            {
                "Type": "volume",
                "Name": self.volume,
                "Destination": "/var/lib/postgresql/data",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/srv/house-helper/postgres/postgresql.conf",
                "Destination": "/etc/house-helper/postgresql.conf",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": "/srv/house-helper/postgres/pg_hba.conf",
                "Destination": "/etc/house-helper/pg_hba.conf",
                "RW": False,
            },
            {
                "Type": "bind",
                "Source": "/srv/house-helper/postgres/credentials/postgres_superuser.password",
                "Destination": "/run/credentials/postgres_superuser.password",
                "RW": False,
            },
        ]
        if self.extra_mount:
            mounts.append(
                {"Type": "volume", "Name": "other", "Destination": "/other", "RW": True}
            )
        ports = {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5432"}]} if self.published_port else {}
        return {
            "Id": self.container_id,
            "Image": "sha256:" + "c" * 64,
            "Name": self.container_name,
            "State": {"Running": True},
            "Config": {
                "Image": self.image,
                "Labels": {
                    "com.docker.compose.project": SPEC.project,
                    "com.docker.compose.service": SPEC.container_name,
                },
            },
            "Mounts": mounts,
            "NetworkSettings": {
                "Ports": ports,
                "Networks": {self.network: {"NetworkID": "d" * 64}},
            },
            "HostConfig": {"PortBindings": ports},
        }

    def _web_inspect(self):
        web = self.web_container
        return {
            "Id": web["Id"],
            "Name": "/" + web["Names"],
            "State": {"Status": web["State"], "Running": web["State"] == "running"},
            "Config": {
                "Image": web.get("Image", SPEC.image),
                "Labels": {
                    "com.docker.compose.project": web["Project"],
                    "com.docker.compose.service": web["Service"],
                },
            },
            "Mounts": web.get(
                "Mounts",
                [
                    {
                        "Type": "volume",
                        "Name": SPEC.state_volume,
                        "Destination": "/state",
                        "RW": True,
                    }
                ],
            ),
        }

    def _worker_inspect(self, worker):
        return {
            "Id": worker["Id"],
            "Name": "/" + worker["Names"],
            "State": {
                "Status": worker["State"],
                "Running": worker["State"] in ("running", "restarting", "created"),
                "ExitCode": worker.get("ExitCode", 0),
            },
            "Config": {
                "Labels": {
                    "com.docker.compose.project": worker["Project"],
                    "com.docker.compose.service": worker["Service"],
                }
            },
        }

    # -- psql answers ------------------------------------------------------------------

    def _psql(self, database: str, sql: str) -> bytes:
        if self.on_sql is not None:
            self.on_sql(sql)
        if sql.strip().startswith('GRANT CONNECT ON DATABASE "house_helper" TO "'):
            role = sql.split('TO "', 1)[1].split('"', 1)[0]
            self.grants.append(role)
            self._grant_connect(role)
            return b""
        if "pg_control_system" in sql:
            return _out([(SID,)])
        if sql.strip() == "SELECT d.datname FROM pg_catalog.pg_database d ORDER BY d.datname":
            return _out([(name,) for name in sorted(self.databases)])
        if sql == contract.DATABASES_SQL:
            rows = []
            for name in sorted(self.databases):
                state = self.databases[name]
                owner = "hh_owner" if name == "house_helper" else "postgres"
                rows.append(
                    (
                        name,
                        "t" if state["allowconn"] else "f",
                        owner,
                        "UTF8",
                        "C",
                        "C",
                        "c",
                        "t" if state["acl"] else "f",
                        state["oid"],
                        state["marker"],
                    )
                )
            return _out(rows)
        if sql == contract.ROLES_SQL:
            return _out(self.roles.values())
        if sql == contract.ROLE_SETTINGS_SQL:
            return _out(
                [
                    (name, setting)
                    for name, settings in self.settings.items()
                    for setting in settings
                ]
            )
        if sql == contract.MEMBERSHIPS_SQL:
            return _out(self.edges)
        if sql == contract.DATABASE_ACLS_SQL:
            return _out(self.acl_entries)
        if sql == contract.database_acl_matrix_sql(contract.SERVING_DATABASE):
            self.acl_matrix_calls += 1
            if self.commit_on_acl_read is not None:
                # Return the current (pre-commit) state on this read, then let the commit
                # land before the next probe. This is the exact Codex interleaving.
                tag, grant_sql, exit_code = self.commit_on_acl_read
                self.commit_on_acl_read = None
                self._pending_commit = (tag, grant_sql, exit_code)
            if self.acl_matrix_fail_on_call == self.acl_matrix_calls:
                raise enroll.DockerError(
                    "handoff_observation", "the ACL matrix is unobservable"
                )
            materialized = self.databases.get("house_helper", {}).get("acl", False)
            entries = tuple(
                (name, privilege, grantable == "t")
                for database_name, _oid, name, privilege, grantable in self.acl_entries
                if database_name == "house_helper"
            )
            row = ["t" if materialized else "f"]
            for privilege in contract.DATABASE_PRIVILEGES:
                for role in contract.PROJECT_ROLE_NAMES:
                    row.append(
                        _take(
                            contract.database_privilege(
                                materialized, entries, role, privilege
                            )
                        )
                    )
            return _out([tuple(row)])
        if sql.startswith("SELECT count(*) FROM pg_catalog.pg_stat_activity"):
            tag = sql.split("application_name = '", 1)[1].split("'", 1)[0]
            state = self.handoff_transports.get(tag, {})
            return _out([(1 if state.get("session_alive") else 0,)])
        if "has_database_privilege('hh_owner'" in sql:
            entries = tuple(
                (
                    "PUBLIC" if oid == "0" else name,
                    privilege,
                    grantable == "t",
                )
                for database_name, oid, name, privilege, grantable in self.acl_entries
                if database_name == "house_helper"
            )
            materialized = self.databases.get("house_helper", {}).get("acl", False)
            connect = [
                _take(contract.database_privilege(materialized, entries, role, "CONNECT"))
                for role in contract.PROJECT_ROLE_NAMES
            ]
            temporary = [
                _take(contract.database_privilege(materialized, entries, role, "TEMPORARY"))
                for role in contract.PROJECT_ROLE_NAMES
            ]
            return _out([connect + temporary])
        if sql == contract.SESSIONS_SQL:
            return _out(self.sessions)
        if sql == contract.SUBSCRIPTIONS_SQL:
            return _out([])
        if sql == contract.INVENTORY_SQL:
            return _out(self.inventory)
        if sql == contract.PUBLIC_TABLES_SQL:
            return _out([(name,) for name in sorted(self.public_tables)])
        if sql == contract.VERSIONS_TABLE_SQL:
            return _out([(1,)] if self.schema_versions else [])
        if sql == contract.VERSIONS_SQL:
            return _out([(version,) for version in self.schema_versions])
        if sql == contract.DEFAULT_ACLS_SQL:
            return _out(self.default_acls)
        if sql == contract.PUBLIC_SCHEMA_ACL_SQL:
            return _out([self.schema_acl])
        if "pg_has_role" in sql:
            return _out([])
        if "shobj_description" in sql:
            return _out([(self.databases.get("house_helper", {}).get("marker", ""),)])
        if "SELECT count(*) FROM pg_catalog.aclexplode" in sql:
            state = self.databases.get("house_helper", {})
            public = sum(1 for row in self.acl_entries if row[1] == "0")
            migrate = any(
                row[2] == "hh_migrate" and row[3] == "CONNECT"
                for row in self.acl_entries
            )
            return _out(
                [
                    (
                        "t" if state.get("allowconn") else "f",
                        "t" if state.get("acl") else "f",
                        public,
                        "t" if migrate else "f",
                    )
                ]
            )
        if "CREATE ROLE hh_owner" in sql:
            self._apply_roles()
            return b""
        if "CREATE DATABASE" in sql:
            self.operations.append("create")
            self.databases["house_helper"] = {
                "allowconn": False,
                "acl": False,
                "marker": "",
                "oid": 20000,
            }
            return b""
        if "REVOKE ALL ON DATABASE" in sql:
            self.operations.append("harden")
            self.databases["house_helper"]["acl"] = True
            self.acl_entries = [
                ("house_helper", "10", "hh_owner", "CONNECT", "f"),
                ("house_helper", "10", "hh_owner", "CREATE", "f"),
                ("house_helper", "10", "hh_owner", "TEMPORARY", "f"),
                ("house_helper", "11", "hh_migrate", "CONNECT", "f"),
            ]
            return b""
        if "ALLOW_CONNECTIONS true" in sql:
            self.operations.append("enable")
            self.databases["house_helper"]["allowconn"] = True
            return b""
        if "COMMENT ON DATABASE" in sql:
            self.operations.append("marker")
            self.marker = sql.split(" IS '", 1)[1].split("';", 1)[0]
            self.databases["house_helper"]["marker"] = self.marker
            return b""
        raise AssertionError(f"unexpected SQL: {sql[:120]}")

    def _grant_connect(self, role: str):
        oids = {"hh_app": "12", "hh_provision": "13", "hh_purge": "15", "hh_backup": "14"}
        self.databases["house_helper"]["acl"] = True
        if not any(
            row[0] == "house_helper" and row[2] == role and row[3] == "CONNECT"
            for row in self.acl_entries
        ):
            self.acl_entries.append(("house_helper", oids[role], role, "CONNECT", "f"))

    def _apply_roles(self):
        self.operations.append("roles")
        for name in contract.PROJECT_ROLE_NAMES:
            matrix = contract.ROLE_MATRIX[name]
            self.roles[name] = _role_row(
                name,
                login=matrix["login"],
                superuser=matrix["superuser"],
                createdb=matrix["createdb"],
                createrole=matrix["createrole"],
                inherit=matrix["inherit"],
                replication=matrix["replication"],
                bypassrls=matrix["bypassrls"],
            )
            self.settings[name] = list(contract.ROLE_SETTINGS[name])
        self.edges = [
            (member, granted, "f", "t" if inherit else "f", "t" if set_option else "f")
            for member, granted, _admin, inherit, set_option in (
                contract.FROZEN_PREDEFINED_MEMBERSHIPS
            )
        ] + [
            ("hh_migrate", "hh_owner", "f", "f", "t"),
            ("hh_restore", "hh_owner", "f", "f", "t"),
        ]


def _take(value: bool) -> str:
    return "t" if value else "f"


def _worker(
    name,
    *,
    state="exited",
    container_id="e" * 64,
    project=SPEC.project,
    service="migrate",
    exit_code=0,
):
    return {
        "Id": container_id,
        "State": state,
        "Names": name,
        "Project": project,
        "Service": service,
        "ExitCode": exit_code,
    }


def _role_row(
    name,
    *,
    login=True,
    superuser=False,
    createdb=False,
    createrole=False,
    inherit=False,
    replication=False,
    bypassrls=False,
):
    return (
        name,
        "t" if login else "f",
        "t" if superuser else "f",
        "t" if createdb else "f",
        "t" if createrole else "f",
        "t" if inherit else "f",
        "t" if replication else "f",
        "t" if bypassrls else "f",
        -1,
        "",
    )


def _adapter(fake: FakeDocker) -> enroll.ClusterAdapter:
    return enroll.ClusterAdapter(enroll.Docker(SPEC, runner=fake))


# --- configuration and container identity ----------------------------------------------------


def test_the_real_compose_file_names_one_identity(monkeypatch):
    monkeypatch.delenv("HOUSE_HELPER_PG_CREDENTIALS_DIR", raising=False)
    spec = enroll.load_spec(enroll.DEFAULT_COMPOSE)
    assert spec.project == "house-helper-pg"
    assert spec.container_name == "house-helper-db"
    assert spec.image.startswith("postgres@sha256:")
    assert spec.volume == "house-helper-pgdata"
    assert spec.network == "house-helper-pg-db"
    deploy = enroll.DEFAULT_COMPOSE.resolve().parent
    assert spec.postgresql_conf_source == enroll._canonical_path(
        deploy / "postgres" / "postgresql.conf"
    )
    assert spec.bootstrap_credential_source == enroll._canonical_path(
        deploy / "postgres" / "credentials" / "postgres_superuser.password"
    )


def test_the_container_identity_fixture_is_accepted():
    fake = FakeDocker()
    enroll.validate_container(SPEC, fake._container())


@pytest.mark.parametrize(
    "change, code",
    [
        (lambda f: setattr(f, "container_name", "/other-db"), "container_name"),
        (lambda f: setattr(f, "container_id", "short"), "container_id"),
        (lambda f: setattr(f, "image", "postgres@sha256:" + "0" * 64), "container_image"),
        (lambda f: setattr(f, "volume", "other-volume"), "volume_identity"),
        (lambda f: setattr(f, "network", "other-network"), "network_identity"),
        (lambda f: setattr(f, "published_port", True), "published_port"),
        (lambda f: setattr(f, "extra_mount", True), "volume_identity"),
    ],
)
def test_each_container_identity_mismatch_is_refused(change, code):
    fake = FakeDocker()
    change(fake)
    with pytest.raises(enroll.DockerError) as refused:
        enroll.validate_container(SPEC, fake._container())
    assert refused.value.code == code


def test_actual_docker_image_ids_use_the_sha256_prefixed_form():
    fake = FakeDocker()
    container = fake._container()
    image = {"Id": "sha256:" + "c" * 64, "RepoDigests": [SPEC.image]}
    enroll.validate_container(SPEC, container)
    enroll.validate_image(SPEC, image)
    assert container["Image"] == image["Id"]
    assert image["Id"] != SPEC.image  # the image ID is not the pinned digest


def test_a_bare_hex_image_id_is_refused():
    fake = FakeDocker()
    image = {"Id": "c" * 64, "RepoDigests": [SPEC.image]}
    with pytest.raises(enroll.DockerError) as refused:
        enroll.validate_image(SPEC, image)
    assert refused.value.code == "image_identity"


def test_the_pinned_digest_must_be_one_of_the_image_repo_digests():
    fake = FakeDocker()
    image = {
        "Id": "sha256:" + "c" * 64,
        "RepoDigests": ["postgres@sha256:" + "0" * 64],
    }
    with pytest.raises(enroll.DockerError) as refused:
        enroll.validate_image(SPEC, image)
    assert refused.value.code == "image_identity"


def test_the_container_image_id_must_be_the_inspected_image():
    fake = FakeDocker()
    fake.container_id = CONTAINER_ID
    adapter = _adapter(fake)

    original = fake._container

    def other_image():
        value = original()
        value["Image"] = "sha256:" + "9" * 64
        return value

    fake._container = other_image
    with pytest.raises(enroll.DockerError) as refused:
        adapter.identity()
    assert refused.value.code == "image_identity"


def _mount_container(*, rw=False, source=None, destination=None):
    fake = FakeDocker()
    value = fake._container()
    for mount in value["Mounts"]:
        if mount.get("Destination") == "/run/credentials/postgres_superuser.password":
            mount["RW"] = rw
            if source is not None:
                mount["Source"] = source
            if destination is not None:
                mount["Destination"] = destination
    return value


def test_the_exact_read_only_bootstrap_credential_mount_is_accepted():
    enroll.validate_container(SPEC, _mount_container())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rw": True},
        {"source": "/srv/house-helper/postgres/credentials/hh_migrate.pgpass"},
        {"destination": "/run/credentials/hh_migrate.pgpass"},
    ],
)
def test_a_substituted_or_writable_bootstrap_credential_mount_is_refused(kwargs):
    with pytest.raises(enroll.DockerError) as refused:
        enroll.validate_container(SPEC, _mount_container(**kwargs))
    assert refused.value.code == "volume_identity"


def test_a_basename_match_from_another_directory_is_refused_f3():
    # Codex F3: the destination and basename are exactly right, but the host source is
    # not the source the recorded deployment configuration authorizes.
    container = _mount_container(
        source="/substituted/credentials/postgres_superuser.password"
    )
    with pytest.raises(enroll.DockerError) as refused:
        enroll.validate_container(SPEC, container)
    assert refused.value.code == "volume_identity"


def test_a_substituted_configuration_bind_is_refused():
    for destination in (
        "/etc/house-helper/postgresql.conf",
        "/etc/house-helper/pg_hba.conf",
    ):
        fake = FakeDocker()
        value = fake._container()
        for mount in value["Mounts"]:
            if mount.get("Destination") == destination:
                mount["Source"] = "/substituted" + destination
        with pytest.raises(enroll.DockerError) as refused:
            enroll.validate_container(SPEC, value)
        assert refused.value.code == "volume_identity"


# Docker Desktop for Windows reports a bind source as its path inside the Linux VM.
# These constants are one real Windows source and its two representations.
WINDOWS_REPO = r"C:\Users\eliv\Pet_Projects\house_helper\deploy\postgres"
WINDOWS_CREDENTIALS = r"C:\Users\eliv\AppData\Local\Temp\hh-wp2c-b\credentials"
DESKTOP_REPO = (
    "/run/desktop/mnt/host/c/Users/eliv/Pet_Projects/house_helper/deploy/postgres"
)
DESKTOP_CREDENTIALS = (
    "/run/desktop/mnt/host/c/Users/eliv/AppData/Local/Temp/hh-wp2c-b/credentials"
)


def _windows_spec():
    return enroll.ClusterSpec(
        project=SPEC.project,
        container_name=SPEC.container_name,
        image=SPEC.image,
        volume=SPEC.volume,
        network=SPEC.network,
        credentials_directory=SPEC.credentials_directory,
        state_volume=SPEC.state_volume,
        postgresql_conf_source=enroll._canonical_path(
            WINDOWS_REPO + "\\postgresql.conf"
        ),
        pg_hba_source=enroll._canonical_path(WINDOWS_REPO + "\\pg_hba.conf"),
        bootstrap_credential_source=enroll._canonical_path(
            WINDOWS_CREDENTIALS + "\\postgres_superuser.password"
        ),
    )


def _desktop_container(*, credential_source=None, credential_rw=False):
    value = FakeDocker()._container()
    sources = {
        "/etc/house-helper/postgresql.conf": DESKTOP_REPO + "/postgresql.conf",
        "/etc/house-helper/pg_hba.conf": DESKTOP_REPO + "/pg_hba.conf",
        "/run/credentials/postgres_superuser.password": (
            DESKTOP_CREDENTIALS + "/postgres_superuser.password"
        ),
    }
    for mount in value["Mounts"]:
        destination = mount.get("Destination")
        if destination in sources:
            mount["Source"] = sources[destination]
        if destination == "/run/credentials/postgres_superuser.password":
            mount["RW"] = credential_rw
            if credential_source is not None:
                mount["Source"] = credential_source
    return value


def test_the_docker_desktop_vm_path_is_translated_to_its_drive_letter_form():
    assert (
        enroll._docker_desktop_windows_path(
            "/run/desktop/mnt/host/c/Users/eliv/postgres/pg_hba.conf"
        )
        == r"C:\Users\eliv\postgres\pg_hba.conf"
    )
    assert (
        enroll._docker_desktop_windows_path("/run/desktop/mnt/host/z/pg_hba.conf")
        == r"Z:\pg_hba.conf"
    )


@pytest.mark.parametrize(
    "value",
    [
        "/host_mnt/c/Users/eliv/pg_hba.conf",
        "/run/desktop/mnt/hostx/c/Users/eliv/pg_hba.conf",
        "/run/desktop/mnt/host/cc/Users/eliv/pg_hba.conf",
        "/run/desktop/mnt/host/1/Users/eliv/pg_hba.conf",
        "/run/desktop/mnt/host/",
        "/srv/house-helper/postgres/pg_hba.conf",
    ],
)
def test_a_lookalike_docker_desktop_path_is_never_translated(value):
    assert enroll._docker_desktop_windows_path(value) == value


def test_native_posix_sources_are_not_reinterpreted(monkeypatch):
    monkeypatch.setattr(enroll, "_IS_WINDOWS", False)
    value = "/run/desktop/mnt/host/c/Users/eliv/pg_hba.conf"
    assert enroll._canonical_path(value) == os.path.normcase(os.path.realpath(value))


def test_docker_desktop_bind_sources_are_accepted_on_windows(monkeypatch):
    monkeypatch.setattr(enroll, "_IS_WINDOWS", True)
    enroll.validate_container(_windows_spec(), _desktop_container())


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "credential_source": (
                DESKTOP_CREDENTIALS + "/../other/postgres_superuser.password"
            )
        },
        {
            "credential_source": (
                "/run/desktop/mnt/host/d/Users/eliv/AppData/Local/Temp/"
                "hh-wp2c-b/credentials/postgres_superuser.password"
            )
        },
        {"credential_source": "/substituted/credentials/postgres_superuser.password"},
        {"credential_rw": True},
    ],
)
def test_docker_desktop_translation_never_relaxes_the_full_source_identity(
    monkeypatch, kwargs
):
    monkeypatch.setattr(enroll, "_IS_WINDOWS", True)
    with pytest.raises(enroll.DockerError) as refused:
        enroll.validate_container(_windows_spec(), _desktop_container(**kwargs))
    assert refused.value.code == "volume_identity"


def test_the_spec_resolves_the_full_compose_sources(tmp_path, monkeypatch):
    compose = tmp_path / "compose.postgres.yaml"
    compose.write_text(
        (enroll.REPOSITORY_ROOT / "deploy" / "compose.postgres.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("HOUSE_HELPER_PG_CREDENTIALS_DIR", raising=False)
    spec = enroll.load_spec(compose)
    assert spec.postgresql_conf_source == enroll._canonical_path(
        tmp_path / "postgres" / "postgresql.conf"
    )
    assert spec.pg_hba_source == enroll._canonical_path(
        tmp_path / "postgres" / "pg_hba.conf"
    )
    assert spec.bootstrap_credential_source == enroll._canonical_path(
        tmp_path / "postgres" / "credentials" / "postgres_superuser.password"
    )
    # The deployment override Compose would honour is the override this tool resolves.
    monkeypatch.setenv("HOUSE_HELPER_PG_CREDENTIALS_DIR", str(tmp_path / "secrets"))
    spec = enroll.load_spec(compose)
    assert spec.bootstrap_credential_source == enroll._canonical_path(
        tmp_path / "secrets" / "postgres_superuser.password"
    )


def _compose_copy(tmp_path):
    compose = tmp_path / "compose.postgres.yaml"
    compose.write_text(
        (enroll.REPOSITORY_ROOT / "deploy" / "compose.postgres.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    return compose


def _container_for(spec, source):
    """The fixture container, bound to `spec`'s image, with the given bootstrap source."""
    value = FakeDocker()._container()
    value["Config"]["Image"] = spec.image
    authorized = {
        "/etc/house-helper/postgresql.conf": spec.postgresql_conf_source,
        "/etc/house-helper/pg_hba.conf": spec.pg_hba_source,
        "/run/credentials/postgres_superuser.password": source,
    }
    for mount in value["Mounts"]:
        if mount["Destination"] in authorized:
            mount["Source"] = authorized[mount["Destination"]]
        if mount.get("Name") == SPEC.volume:
            mount["Name"] = spec.volume
    return value


def test_the_approved_credential_directory_is_explicit_configuration(
    tmp_path, monkeypatch
):
    # F-03: the approved external directory is a non-secret value the operator passes on
    # every host command. It resolves without any inherited environment, which an ordinary
    # `sudo` would not provide.
    compose = _compose_copy(tmp_path)
    approved = tmp_path / "deploy-overlays" / "secrets" / "pg-credentials"
    monkeypatch.delenv(enroll.CREDENTIALS_DIRECTORY_ENV, raising=False)
    spec = enroll.load_spec(compose, approved)
    assert spec.credentials_directory == enroll._canonical_path(approved)
    assert spec.bootstrap_credential_source == enroll._canonical_path(
        approved / "postgres_superuser.password"
    )
    # HOF-03 item 4 records the state volume this surface names.
    assert spec.state_volume == "house-helper-pg-state"
    # The same explicit value wins when the environment happens to agree.
    monkeypatch.setenv(enroll.CREDENTIALS_DIRECTORY_ENV, str(approved))
    assert enroll.load_spec(compose, approved).credentials_directory == (
        enroll._canonical_path(approved)
    )


def test_a_conflicting_credential_directory_is_refused(tmp_path, monkeypatch):
    compose = _compose_copy(tmp_path)
    monkeypatch.setenv(
        enroll.CREDENTIALS_DIRECTORY_ENV, str(tmp_path / "from-the-environment")
    )
    with pytest.raises(enroll.DockerError) as refused:
        enroll.load_spec(compose, tmp_path / "from-the-option")
    assert refused.value.code == "credentials_conflict"


def test_a_mismatched_credential_directory_never_matches_the_container(
    tmp_path, monkeypatch
):
    # The mounted source is compared in full against the explicit configuration: a
    # passfile of the right name from another directory is a substitution, not the
    # approved mount. The approved directory itself is accepted.
    compose = _compose_copy(tmp_path)
    approved = tmp_path / "approved"
    monkeypatch.delenv(enroll.CREDENTIALS_DIRECTORY_ENV, raising=False)
    spec = enroll.load_spec(compose, approved)
    enroll.validate_container(
        spec,
        _container_for(spec, str(approved / "postgres_superuser.password")),
    )
    with pytest.raises(enroll.DockerError) as refused:
        enroll.validate_container(
            spec, _container_for(spec, SPEC.bootstrap_credential_source)
        )
    assert refused.value.code == "volume_identity"


def test_a_duplicate_bootstrap_credential_mount_is_refused():
    fake = FakeDocker()
    value = fake._container()
    value["Mounts"].append(dict(value["Mounts"][-1]))
    with pytest.raises(enroll.DockerError) as refused:
        enroll.validate_container(SPEC, value)
    assert refused.value.code == "volume_identity"


def test_an_extra_mount_is_still_refused():
    fake = FakeDocker()
    value = fake._container()
    value["Mounts"].append(
        {"Type": "volume", "Name": "other", "Destination": "/other", "RW": False}
    )
    with pytest.raises(enroll.DockerError) as refused:
        enroll.validate_container(SPEC, value)
    assert refused.value.code == "volume_identity"


def test_the_volume_must_be_attached_only_to_this_container():
    fake = FakeDocker()
    with pytest.raises(enroll.DockerError) as refused:
        enroll.validate_volume(SPEC, {"Name": SPEC.volume, "Driver": "local", "Scope": "local"}, ["x" * 64], CONTAINER_ID)
    assert refused.value.code == "volume_attachment"


# --- Mode A through the real adapter ---------------------------------------------------------


def test_mode_a_completes_through_the_docker_adapter(tmp_path):
    fake = FakeDocker()
    journal_file = journal.Journal(tmp_path / "cutover")
    report = enrollment.ModeA(
        journal_file,
        _adapter(fake),
        approved_by="owner-fixture",
        token_source=lambda: "f" * 32,
    ).run()
    assert report.state == enrollment.E7
    assert fake.operations == [
        "roles",
        "create",
        "harden",
        "enable",
        "schema",
        "marker",
    ]
    records = journal_file.read().records
    assert [record.payload.get("state") for record in records] == [
        "intent",
        "roles",
        "database",
        "hardened",
        "marker",
        "completed",
    ]
    assert journal.parse_marker(records[-1].payload["marker"]) == ("f" * 32, SID)


def test_check_credentials_validates_against_the_container_runtime_uid(tmp_path, monkeypatch):
    seen: list[tuple[int, int]] = []

    def summary(path, **kwargs):
        seen.append((kwargs.get("owner_uid"), kwargs.get("owner_gid")))
        return {"exists": True, "private": True, "owned": True, "entry": True}

    monkeypatch.setattr(enroll.credentials, "summary", summary)
    code = enroll._check_credentials(tmp_path)
    assert code == 0
    assert seen == [(10001, 10001)] * 6


# --- F-03: check-credentials follows the one credential-directory contract -------------------


def _record_summary(monkeypatch):
    seen: list[Path] = []

    def summary(path, **kwargs):
        seen.append(Path(path))
        return {"exists": True, "private": True, "owned": True, "entry": True}

    monkeypatch.setattr(enroll.credentials, "summary", summary)
    return seen


def _canonical(path):
    return Path(enroll._canonical_path(path))


def test_check_credentials_accepts_an_explicit_directory_without_environment(
    tmp_path, monkeypatch
):
    # An ordinary `sudo` loses the shell environment; the explicit value must still work.
    compose = _compose_copy(tmp_path)
    approved = tmp_path / "approved-credentials"
    approved.mkdir()
    monkeypatch.delenv(enroll.CREDENTIALS_DIRECTORY_ENV, raising=False)
    seen = _record_summary(monkeypatch)
    code = enroll.main(
        [
            "--compose-file",
            str(compose),
            "check-credentials",
            "--credentials-dir",
            str(approved),
        ]
    )
    assert code == enroll.EX_OK
    assert {path.parent for path in seen} == {_canonical(approved)}


def test_check_credentials_accepts_the_top_level_spelling(tmp_path, monkeypatch):
    compose = _compose_copy(tmp_path)
    approved = tmp_path / "approved-credentials"
    approved.mkdir()
    monkeypatch.delenv(enroll.CREDENTIALS_DIRECTORY_ENV, raising=False)
    seen = _record_summary(monkeypatch)
    code = enroll.main(
        [
            "--compose-file",
            str(compose),
            "--credentials-dir",
            str(approved),
            "check-credentials",
        ]
    )
    assert code == enroll.EX_OK
    assert {path.parent for path in seen} == {_canonical(approved)}


def test_check_credentials_refuses_contradictory_top_level_and_subcommand_options(
    tmp_path, monkeypatch, capsys
):
    compose = _compose_copy(tmp_path)
    monkeypatch.delenv(enroll.CREDENTIALS_DIRECTORY_ENV, raising=False)
    seen = _record_summary(monkeypatch)
    code = enroll.main(
        [
            "--compose-file",
            str(compose),
            "--credentials-dir",
            str(tmp_path / "first"),
            "check-credentials",
            "--credentials-dir",
            str(tmp_path / "second"),
        ]
    )
    assert code == enroll.EX_REFUSED
    assert "credentials_conflict" in capsys.readouterr().err
    assert seen == []


def test_check_credentials_refuses_an_explicit_directory_that_conflicts_with_the_environment(
    tmp_path, monkeypatch, capsys
):
    compose = _compose_copy(tmp_path)
    monkeypatch.setenv(enroll.CREDENTIALS_DIRECTORY_ENV, str(tmp_path / "from-env"))
    seen = _record_summary(monkeypatch)
    code = enroll.main(
        [
            "--compose-file",
            str(compose),
            "check-credentials",
            "--credentials-dir",
            str(tmp_path / "from-option"),
        ]
    )
    assert code == enroll.EX_REFUSED
    assert "credentials_conflict" in capsys.readouterr().err
    assert seen == []


def test_check_credentials_accepts_matching_explicit_and_environment_values(
    tmp_path, monkeypatch
):
    compose = _compose_copy(tmp_path)
    approved = tmp_path / "agreed"
    approved.mkdir()
    monkeypatch.setenv(enroll.CREDENTIALS_DIRECTORY_ENV, str(approved))
    seen = _record_summary(monkeypatch)
    assert (
        enroll.main(
            [
                "--compose-file",
                str(compose),
                "check-credentials",
                "--credentials-dir",
                str(approved),
            ]
        )
        == enroll.EX_OK
    )
    assert {path.parent for path in seen} == {_canonical(approved)}


def test_the_credential_directory_precedence_is_one_contract(tmp_path, monkeypatch):
    compose = _compose_copy(tmp_path)
    compose_dir = compose.resolve().parent
    approved = tmp_path / "approved"
    other = tmp_path / "other"
    monkeypatch.delenv(enroll.CREDENTIALS_DIRECTORY_ENV, raising=False)
    # No explicit value and no environment: the Compose default, resolved beside the file.
    assert enroll.resolve_credentials_directory(compose) == _canonical(
        compose_dir / "postgres" / "credentials"
    )
    # The environment value is resolved exactly as Compose resolves it.
    monkeypatch.setenv(enroll.CREDENTIALS_DIRECTORY_ENV, str(other))
    assert enroll.resolve_credentials_directory(compose) == _canonical(other)
    # An explicit value wins when the environment has none.
    monkeypatch.delenv(enroll.CREDENTIALS_DIRECTORY_ENV, raising=False)
    assert enroll.resolve_credentials_directory(
        compose, subcommand=approved
    ) == _canonical(approved)
    # A relative explicit value follows the compose file's directory, like load_spec.
    assert enroll.resolve_credentials_directory(
        compose, top_level=Path("relative/secrets")
    ) == _canonical(compose_dir / "relative" / "secrets")
    # Contradictions are refusals, never a silent override.
    monkeypatch.setenv(enroll.CREDENTIALS_DIRECTORY_ENV, str(other))
    with pytest.raises(enroll.DockerError) as refused:
        enroll.resolve_credentials_directory(compose, subcommand=approved)
    assert refused.value.code == "credentials_conflict"
    with pytest.raises(enroll.DockerError) as refused:
        enroll.resolve_credentials_directory(
            compose, top_level=approved, subcommand=other
        )
    assert refused.value.code == "credentials_conflict"


def test_check_credentials_verifies_the_effective_directory_identity(
    tmp_path, monkeypatch, capsys
):
    # Passfiles alone are not enough: the effective directory must exist and be a
    # directory, and the path is named (non-secret) so the operator can compare it with
    # the container's mount source.
    compose = _compose_copy(tmp_path)
    missing = tmp_path / "missing-credentials"
    monkeypatch.delenv(enroll.CREDENTIALS_DIRECTORY_ENV, raising=False)
    seen = _record_summary(monkeypatch)
    code = enroll.main(
        [
            "--compose-file",
            str(compose),
            "check-credentials",
            "--credentials-dir",
            str(missing),
        ]
    )
    assert code == enroll.EX_REFUSED
    assert "credentials_directory" in capsys.readouterr().err
    assert seen == []

    approved = tmp_path / "approved-credentials"
    approved.mkdir()
    assert (
        enroll.main(
            [
                "--compose-file",
                str(compose),
                "check-credentials",
                "--credentials-dir",
                str(approved),
            ]
        )
        == enroll.EX_OK
    )
    assert f"credentials directory: {_canonical(approved)}" in capsys.readouterr().out


def test_mode_a_refuses_a_container_identity_that_does_not_match_the_record(tmp_path):
    fake = FakeDocker()
    fake.container_name = "/not-the-database"
    journal_file = journal.Journal(tmp_path / "cutover")
    with pytest.raises(enroll.DockerError) as refused:
        enrollment.ModeA(journal_file, _adapter(fake)).run()
    assert refused.value.code == "container_name"
    assert journal_file.read().records == ()


def test_the_schema_step_refuses_an_identity_that_is_not_the_intent_s():
    fake = FakeDocker()
    adapter = _adapter(fake)
    expected = enrollment.ClusterIdentity(
        container_name=SPEC.container_name,
        container_id=CONTAINER_ID,
        image_digest=SPEC.image,
        volume=SPEC.volume,
        system_identifier="999999999999",
    )
    with pytest.raises(enrollment.EnrollmentError) as refused:
        adapter.harden_schema(expected)
    assert refused.value.code == "cluster_identity"
    assert "system_identifier" in str(refused.value)
    # No authorization was issued and the schema one-shot was never invoked.
    assert fake.compose_input is None
    assert fake.operations == []


def test_the_schema_step_authorizes_only_the_bound_identity():
    fake = FakeDocker()
    adapter = _adapter(fake)
    expected = adapter.identity()
    adapter.harden_schema(expected)
    value = journal.decode_schema_authorization(fake.compose_input)
    assert value["role"] == "schema"
    assert value["database"] == "house_helper"
    assert value["cluster"] == dict(expected.as_journal())
    assert fake.operations == ["schema"]


# --- Mode B and the migrate invocation -------------------------------------------------------


def _enrolled(tmp_path):
    fake = FakeDocker()
    journal_file = journal.Journal(tmp_path / "cutover")
    enrollment.ModeA(
        journal_file, _adapter(fake), token_source=lambda: "f" * 32
    ).run()
    # Mode A now uses the same one-shot service for the ENR-04 schema step; a later
    # migrate test asserts on the record the MIGRATE invocation received.
    fake.compose_input = None
    return fake, journal_file


def test_mode_b_verifies_the_enrolled_fixture(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    report = enroll.verify_mode_b(journal_file, _adapter(fake))
    assert report.exit_code == 0
    value = journal.decode_authorization(report.authorization)
    assert value["cluster"]["container_id"] == CONTAINER_ID
    assert value["cluster"]["volume"] == SPEC.volume
    assert value["marker"] == MARKER


def test_a_changed_container_id_is_a_journaled_mismatch(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    fake.container_id = "9" * 64
    before = len(journal_file.read().records)
    report = enroll.verify_mode_b(journal_file, _adapter(fake))
    assert report.exit_code == 3
    assert report.authorization is None
    records = journal_file.read().records
    assert len(records) == before + 1
    assert records[-1].kind == "cluster_identity"
    assert "container:id" in records[-1].payload["items"]
    # No Mode A fallback: no new intent.
    assert not any(
        record.kind == "cluster_enrollment" and record.payload.get("state") == "intent"
        for record in records[before:]
    )


def test_run_migrate_passes_the_record_on_stdin_and_journals_the_outcome(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    docker = enroll.Docker(SPEC, runner=fake)
    code = enroll.run_migrate(
        docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
    )
    assert code == 0
    value = journal.decode_authorization(fake.compose_input)
    assert value["mode"] == "B"
    records = journal_file.read().records
    assert records[-2].kind == "migration" and records[-2].payload["state"] == "intent"
    assert records[-1].kind == "migration" and records[-1].payload["state"] == "outcome"
    assert records[-1].payload["exit_code"] == 0
    # F-03: the child Compose resolves the same approved credential directory the host
    # tool resolved, instead of the repository default under an ordinary `sudo`.
    assert (
        fake.compose_env[enroll.CREDENTIALS_DIRECTORY_ENV]
        == SPEC.credentials_directory
    )


def test_run_migrate_refuses_before_compose_when_mode_b_fails(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    fake.marker = ""
    fake.databases["house_helper"]["marker"] = ""
    docker = enroll.Docker(SPEC, runner=fake)
    code = enroll.run_migrate(
        docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
    )
    assert code == 3
    assert fake.compose_input is None


def test_run_migrate_refuses_before_compose_while_a_handoff_is_unknown(tmp_path, capsys):
    """LCK-08/LCK-13: reconcile the handoff before any later operation advances the machine."""
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: "interrupt"
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN
    fake.on_grant = None
    assert enrollment.pending_handoff_intents(journal_file.read().records)
    before = len(journal_file.read().records)

    code = enroll.run_migrate(
        enroll.Docker(SPEC, runner=fake),
        enroll.DEFAULT_COMPOSE,
        journal_file,
        _adapter(fake),
    )

    assert code == enroll.EX_UNKNOWN
    # Nothing was launched and no migration intent was appended.
    assert fake.compose_input is None
    assert len(journal_file.read().records) == before
    error = capsys.readouterr().err
    assert "handoff" in error and "hh_app" in error


def test_a_reconciled_handoff_does_not_block_a_later_migrate(tmp_path):
    """The gate is the unresolved intent, not the handoff itself."""
    fake, journal_file = _enrolled_at_head(tmp_path)
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()

    code = enroll.run_migrate(
        enroll.Docker(SPEC, runner=fake),
        enroll.DEFAULT_COMPOSE,
        journal_file,
        _adapter(fake),
    )

    assert code == enroll.EX_OK


# --- F1: the Mode B -> worker interval is one host operation ---------------------------------


def test_the_operation_interval_is_held_across_mode_b_compose_and_post_check(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    observed: list[str] = []

    def during_compose():
        # A second supported host operation cannot enter its own Mode B/use interval.
        with pytest.raises(journal.JournalError) as refused:
            with journal_file.operation_lock():
                pass
        assert refused.value.code == "operation_busy"
        observed.append("compose")

    fake.on_compose = during_compose
    docker = enroll.Docker(SPEC, runner=fake)
    code = enroll.run_migrate(
        docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
    )
    assert code == 0
    assert observed == ["compose"]
    states = [
        (record.kind, record.payload.get("state"))
        for record in journal_file.read().records
        if record.kind == "migration"
    ]
    assert states == [("migration", "intent"), ("migration", "outcome")]


def test_a_concurrent_migrate_is_refused_during_the_interval(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def during_compose():
        started.set()
        assert release.wait(10)

    fake.on_compose = during_compose
    docker = enroll.Docker(SPEC, runner=fake)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            enroll.run_migrate,
            docker,
            enroll.DEFAULT_COMPOSE,
            journal_file,
            _adapter(fake),
        )
        assert started.wait(10)
        with pytest.raises(journal.JournalError) as refused:
            enroll.run_migrate(
                docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
            )
        assert refused.value.code == "operation_busy"
        release.set()
        assert first.result(10) == 0
    # Exactly one intent/outcome pair was written.
    intents = [
        record
        for record in journal_file.read().records
        if record.kind == "migration" and record.payload.get("state") == "intent"
    ]
    assert len(intents) == 1


def test_a_cluster_identity_change_during_the_interval_fails_closed(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    fake.on_compose = lambda: setattr(fake, "container_id", "9" * 64)
    docker = enroll.Docker(SPEC, runner=fake)
    code = enroll.run_migrate(
        docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
    )
    assert code == 3
    records = journal_file.read().records
    assert records[-1].kind == "cluster_identity"
    assert "container:id" in records[-1].payload["items"]


# --- F10: unknown outcomes and reconciliation ------------------------------------------------


def test_a_client_timeout_is_journaled_unknown_not_not_started(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    fake.compose_exception = subprocess.TimeoutExpired("docker compose", 900)
    docker = enroll.Docker(SPEC, runner=fake)
    code = enroll.run_migrate(
        docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
    )
    assert code == enroll.EX_UNKNOWN
    outcome = [
        record.payload
        for record in journal_file.read().records
        if record.kind == "migration" and record.payload.get("state") == "outcome"
    ][-1]
    assert outcome["outcome"] == "unknown"
    assert outcome["outcome"] != "not_started"
    # Another conflicting operation is refused until the observation is reconciled.
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enroll.run_migrate(
            docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
        )
    assert refused.value.code == "migration_unknown"


def _timed_out_worker(fake, journal_file):
    """Leave one migrate intent whose client timed out, as the host would."""
    fake.compose_exception = subprocess.TimeoutExpired("docker compose", 900)
    docker = enroll.Docker(SPEC, runner=fake)
    assert (
        enroll.run_migrate(
            docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
        )
        == enroll.EX_UNKNOWN
    )
    fake.compose_exception = None
    return docker


def _pending(journal_file) -> bool:
    return enrollment.pending_migration_unknown(journal_file.read().records) is not None


def _pending_run_id(journal_file) -> str:
    pending = enrollment.pending_migration_unknown(journal_file.read().records)
    assert pending is not None
    return str(pending.payload["run_id"])


def test_a_leftover_container_from_another_run_is_not_evidence(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    _timed_out_worker(fake, journal_file)
    # An exited container from a DIFFERENT run must not release this run's UNKNOWN.
    fake.worker_containers = [_worker("house-helper-pg-migrate-run-" + "0" * 32)]
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.reconcile_migration(
            journal_file,
            _adapter(fake).mode_b_observation(),
            worker=_adapter(fake).worker_evidence(_pending_run_id(journal_file)),
        )
    assert refused.value.code == "migration_unknown"
    assert "worker:not_observed" in str(refused.value)
    assert _pending(journal_file)


def test_reconcile_migration_resolves_an_unchanged_schema_as_not_started(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    docker = _timed_out_worker(fake, journal_file)
    # F10: positive termination evidence is required; the unchanged revision alone is not.
    fake.worker_containers = [_worker(fake.migrate_name)]
    record = enrollment.reconcile_migration(
        journal_file,
        _adapter(fake).mode_b_observation(),
        worker=_adapter(fake).worker_evidence(_pending_run_id(journal_file)),
    )
    assert record.payload["outcome"] == "reconciled_not_started"
    # The reconciliation clears the pending unknown, so a new migrate may proceed.
    assert (
        enroll.run_migrate(
            docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
        )
        == enroll.EX_OK
    )


def test_reconcile_migration_refuses_without_worker_lifecycle_evidence(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    _timed_out_worker(fake, journal_file)
    # `docker compose run --rm` can leave no container behind; absence is ambiguous, not
    # proof that the worker is gone.
    fake.worker_containers = []
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.reconcile_migration(
            journal_file,
            _adapter(fake).mode_b_observation(),
            worker=_adapter(fake).worker_evidence(_pending_run_id(journal_file)),
        )
    assert refused.value.code == "migration_unknown"
    assert _pending(journal_file)
    # The UNKNOWN continues to block another migration.
    docker = enroll.Docker(SPEC, runner=fake)
    with pytest.raises(enrollment.EnrollmentError) as blocked:
        enroll.run_migrate(
            docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
        )
    assert blocked.value.code == "migration_unknown"


def test_an_unchanged_revision_with_a_still_active_worker_is_not_reconciled(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    _timed_out_worker(fake, journal_file)
    # The client timed out but the one-shot container is still running; the revision has
    # not changed yet. The reconciler must refuse.
    fake.worker_containers = [_worker(fake.migrate_name, state="running")]
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.reconcile_migration(
            journal_file,
            _adapter(fake).mode_b_observation(),
            worker=_adapter(fake).worker_evidence(_pending_run_id(journal_file)),
        )
    assert refused.value.code == "migration_unknown"
    assert "worker:active" in str(refused.value)
    assert _pending(journal_file)
    # A second migrate is refused while the first worker may still be executing.
    docker = enroll.Docker(SPEC, runner=fake)
    with pytest.raises(enrollment.EnrollmentError) as blocked:
        enroll.run_migrate(
            docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
        )
    assert blocked.value.code == "migration_unknown"


def test_a_changed_cluster_identity_prevents_reconciliation(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    _timed_out_worker(fake, journal_file)
    fake.worker_containers = [_worker(fake.migrate_name)]
    fake.container_id = "9" * 64
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.reconcile_migration(
            journal_file,
            _adapter(fake).mode_b_observation(),
            worker=_adapter(fake).worker_evidence(_pending_run_id(journal_file)),
        )
    assert refused.value.code == "cluster_identity"
    assert _pending(journal_file)


def test_a_terminated_worker_observed_at_the_head_is_reconciled_completed(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    _timed_out_worker(fake, journal_file)
    fake.worker_containers = [_worker(fake.migrate_name)]
    head, _chart = contract.known_revision_chart()
    fake.schema_versions = (head,)
    fake.public_tables = tuple(sorted(contract.expected_tables()))
    fake.inventory = _head_inventory()
    record = enrollment.reconcile_migration(
        journal_file,
        _adapter(fake).mode_b_observation(),
        worker=_adapter(fake).worker_evidence(_pending_run_id(journal_file)),
    )
    assert record.payload["outcome"] == "reconciled_completed"
    assert not _pending(journal_file)


def test_reconcile_migration_refuses_an_unresolvable_observation(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    _timed_out_worker(fake, journal_file)
    fake.worker_containers = [_worker(fake.migrate_name)]
    fake.schema_versions = ("0013_user_text_size",)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.reconcile_migration(
            journal_file,
            _adapter(fake).mode_b_observation(),
            worker=_adapter(fake).worker_evidence(_pending_run_id(journal_file)),
        )
    assert refused.value.code == "migration_unknown"
    assert _pending(journal_file)


def test_a_confirmed_launch_failure_is_not_started(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    fake.compose_exception = OSError("no docker")
    docker = enroll.Docker(SPEC, runner=fake)
    with pytest.raises(enroll.DockerLaunchError):
        enroll.run_migrate(
            docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
        )
    outcome = [
        record.payload
        for record in journal_file.read().records
        if record.kind == "migration" and record.payload.get("state") == "outcome"
    ][-1]
    assert outcome["outcome"] == "not_started"
    # A confirmed non-launch is resolved; the next migrate may proceed.
    fake.compose_exception = None
    assert (
        enroll.run_migrate(
            docker, enroll.DEFAULT_COMPOSE, journal_file, _adapter(fake)
        )
        == enroll.EX_OK
    )


# --- F10: resolver-level binding, retained workers and cleanup --------------------------------


def _fixed_run_id(monkeypatch, run_id: str = "07" * 16) -> str:
    """Make the authorization run id deterministic for the command/cleanup tests."""
    monkeypatch.setattr(enroll.os, "urandom", lambda length: bytes.fromhex(run_id)[:length])
    return run_id


def _evidence(**overrides):
    run_id = overrides.pop("run_id", "07" * 16)
    expected_name = enrollment.worker_container_name(run_id)
    name = overrides.pop("container_name", expected_name)
    observed_name = overrides.pop("observed_name", expected_name)
    values = dict(
        run_id=run_id,
        container_name=name,
        project=SPEC.project,
        service="migrate",
        observed=(
            enrollment.WorkerContainer(
                container_id="e" * 64,
                name=observed_name,
                state="exited",
                project=SPEC.project,
                service="migrate",
                exit_code=0,
            ),
        ),
    )
    values.update(overrides)
    return enrollment.WorkerEvidence(**values)


def test_the_resolver_rejects_worker_evidence_from_another_run(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    _timed_out_worker(fake, journal_file)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.reconcile_migration(
            journal_file,
            _adapter(fake).mode_b_observation(),
            worker=_evidence(run_id="1" * 32),
        )
    assert refused.value.code == "migration_unknown"
    assert "worker:run_mismatch" in str(refused.value)
    assert _pending(journal_file)


@pytest.mark.parametrize(
    "field, value, item",
    [
        ("container_name", "house-helper-pg-migrate-run-" + "1" * 32, "worker:name_mismatch"),
        ("project", "another-project", "worker:project_mismatch"),
        ("service", "another-service", "worker:service_mismatch"),
    ],
)
def test_the_resolver_rejects_misbound_worker_evidence(tmp_path, field, value, item):
    fake, journal_file = _enrolled(tmp_path)
    _timed_out_worker(fake, journal_file)
    run_id = _pending_run_id(journal_file)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.reconcile_migration(
            journal_file,
            _adapter(fake).mode_b_observation(),
            worker=_evidence(run_id=run_id, **{field: value}),
        )
    assert item in str(refused.value)
    assert _pending(journal_file)


@pytest.mark.parametrize(
    "observed_name, observed_project, observed_service, exit_code, item",
    [
        (
            "house-helper-pg-migrate-run-" + "0" * 32,
            SPEC.project,
            "migrate",
            0,
            "worker:not_observed",
        ),
        (None, "another-project", "migrate", 0, "worker:identity_mismatch"),
        (None, SPEC.project, "migrate", None, "worker:outcome_unknown"),
    ],
)
def test_the_resolver_rejects_misbound_observed_containers(
    tmp_path, observed_name, observed_project, observed_service, exit_code, item
):
    fake, journal_file = _enrolled(tmp_path)
    _timed_out_worker(fake, journal_file)
    run_id = _pending_run_id(journal_file)
    name = observed_name or enrollment.worker_container_name(run_id)
    evidence = _evidence(
        run_id=run_id,
        observed=(
            enrollment.WorkerContainer(
                container_id="e" * 64,
                name=name,
                state="exited",
                project=observed_project,
                service=observed_service,
                exit_code=exit_code,
            ),
        ),
    )
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.reconcile_migration(
            journal_file,
            _adapter(fake).mode_b_observation(),
            worker=evidence,
        )
    assert item in str(refused.value)
    assert _pending(journal_file)


def test_reconcile_without_a_pending_intent_refuses_cleanly(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    docker = enroll.Docker(SPEC, runner=fake)
    # No valid run id: unbound evidence, never a Docker/inspect error.
    evidence = docker.worker_evidence("")
    assert evidence.observed == ()
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.reconcile_migration(
            journal_file,
            _adapter(fake).mode_b_observation(),
            worker=evidence,
        )
    assert refused.value.code == "migration_not_pending"


def test_an_old_retained_container_neither_authorizes_nor_blocks_the_run(tmp_path):
    fake, journal_file = _enrolled(tmp_path)
    _timed_out_worker(fake, journal_file)
    run_id = _pending_run_id(journal_file)
    old_name = enrollment.worker_container_name("0" * 32)
    fake.worker_containers = [_worker(old_name)]
    docker = enroll.Docker(SPEC, runner=fake)
    assert docker.worker_name_conflict(run_id) is False
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.reconcile_migration(
            journal_file,
            _adapter(fake).mode_b_observation(),
            worker=docker.worker_evidence(run_id),
        )
    assert "worker:not_observed" in str(refused.value)
    assert _pending(journal_file)


def test_the_generated_migrate_command_keeps_evidence_and_the_deterministic_name(tmp_path):
    fake = FakeDocker()
    docker = enroll.Docker(SPEC, runner=fake)
    run_id = "07" * 16
    docker.compose_migrate(enroll.DEFAULT_COMPOSE, b"{}", run_id=run_id)
    argv = fake.compose_argv
    assert argv[:2] == ["docker", "compose"]
    assert argv[argv.index("--profile") + 1] == "ops"
    assert "-T" in argv
    assert "--rm" not in argv
    assert argv[argv.index("--name") + 1] == enrollment.worker_container_name(run_id)
    assert argv[-1] == "migrate"
    assert "HOUSE_HELPER_PG_AUTHORIZATION_STDIN=1" in argv
    # No port publication flag is introduced by the host; the Compose service is unchanged.
    assert "--publish" not in argv and "-p" not in argv


def test_the_schema_one_shot_is_not_the_migration_worker(tmp_path):
    fake = FakeDocker()
    docker = enroll.Docker(SPEC, runner=fake)
    docker.compose_schema(enroll.DEFAULT_COMPOSE, b"{}")
    # The F10 evidence-retention change is scoped to the migration worker only.
    assert "--rm" in fake.compose_argv


def test_a_retained_container_with_this_run_s_name_blocks_the_launch(tmp_path, monkeypatch):
    fake, journal_file = _enrolled(tmp_path)
    run_id = _fixed_run_id(monkeypatch)
    fake.compose_argv = None
    fake.worker_containers = [_worker(enrollment.worker_container_name(run_id))]
    code = enroll.run_migrate(
        enroll.Docker(SPEC, runner=fake),
        enroll.DEFAULT_COMPOSE,
        journal_file,
        _adapter(fake),
    )
    assert code == enroll.EX_REFUSED
    assert fake.compose_argv is None
    outcome = [
        record.payload
        for record in journal_file.read().records
        if record.kind == "migration" and record.payload.get("state") == "outcome"
    ][-1]
    assert outcome["outcome"] == "not_started"
    assert outcome["evidence"] == "worker_name_conflict"


def test_a_successful_migration_removes_only_the_confirmed_terminal_worker(
    tmp_path, monkeypatch
):
    fake, journal_file = _enrolled(tmp_path)
    run_id = _fixed_run_id(monkeypatch)
    name = enrollment.worker_container_name(run_id)
    # The worker appears when Compose starts it, not before (before launch it would be a
    # name conflict - a separate test).
    fake.on_compose = lambda: setattr(fake, "worker_containers", [_worker(name)])
    code = enroll.run_migrate(
        enroll.Docker(SPEC, runner=fake),
        enroll.DEFAULT_COMPOSE,
        journal_file,
        _adapter(fake),
    )
    assert code == enroll.EX_OK
    assert fake.rm_argv == [["docker", "rm", "e" * 64]]
    outcome = [
        record.payload
        for record in journal_file.read().records
        if record.kind == "migration" and record.payload.get("state") == "outcome"
    ][-1]
    assert outcome["outcome"] == "completed"


def test_evidence_is_retained_until_the_outcome_is_durably_recorded(tmp_path, monkeypatch):
    fake, journal_file = _enrolled(tmp_path)
    run_id = _fixed_run_id(monkeypatch)
    name = enrollment.worker_container_name(run_id)
    fake.on_compose = lambda: setattr(fake, "worker_containers", [_worker(name)])
    original = journal_file.append

    def fail_outcome(kind, payload, *, at=None):
        if kind == "migration" and payload.get("state") == "outcome":
            raise RuntimeError("injected journal failure")
        return original(kind, payload, at=at)

    monkeypatch.setattr(journal_file, "append", fail_outcome)
    with pytest.raises(RuntimeError):
        enroll.run_migrate(
            enroll.Docker(SPEC, runner=fake),
            enroll.DEFAULT_COMPOSE,
            journal_file,
            _adapter(fake),
        )
    # The outcome was never durable, so the evidence must still exist.
    assert fake.rm_argv == []


def test_cleanup_never_removes_a_running_or_unverified_worker(tmp_path):
    fake = FakeDocker()
    docker = enroll.Docker(SPEC, runner=fake)
    run_id = "07" * 16
    name = enrollment.worker_container_name(run_id)
    for worker in (
        _worker(name, state="running"),
        _worker(name, project="another-project"),
        _worker("house-helper-pg-migrate-run-" + "0" * 32),
    ):
        fake.worker_containers = [worker]
        enroll.cleanup_worker(docker, run_id)
        assert fake.rm_argv == []
    # A running worker cannot be removed even when the caller names its exact id.
    fake.worker_containers = [_worker(name, state="running")]
    with pytest.raises(enroll.DockerError) as refused:
        docker.remove_worker(run_id=run_id, container_id="e" * 64)
    assert refused.value.code == "worker_cleanup"
    assert fake.rm_argv == []


def test_cleanup_refuses_an_unrelated_container_identity(tmp_path):
    fake = FakeDocker()
    docker = enroll.Docker(SPEC, runner=fake)
    run_id = "07" * 16
    wrong_name = "house-helper-pg-migrate-run-" + "0" * 32
    fake.worker_containers = [_worker(wrong_name)]
    with pytest.raises(enroll.DockerError) as refused:
        docker.remove_worker(run_id=run_id, container_id="e" * 64)
    assert refused.value.code == "worker_cleanup"
    assert fake.rm_argv == []


def test_a_cleanup_failure_is_not_an_uncertain_migration_outcome(
    tmp_path, monkeypatch, capsys
):
    fake, journal_file = _enrolled(tmp_path)
    run_id = _fixed_run_id(monkeypatch)
    name = enrollment.worker_container_name(run_id)
    fake.on_compose = lambda: setattr(fake, "worker_containers", [_worker(name)])
    fake.rm_returncode = 1
    code = enroll.run_migrate(
        enroll.Docker(SPEC, runner=fake),
        enroll.DEFAULT_COMPOSE,
        journal_file,
        _adapter(fake),
    )
    captured = capsys.readouterr()
    assert code == enroll.EX_OK
    assert "warning [migrate-cleanup]" in captured.err
    outcome = [
        record.payload
        for record in journal_file.read().records
        if record.kind == "migration" and record.payload.get("state") == "outcome"
    ][-1]
    # The migration outcome stays known and successful; containment of the retained
    # container is an operational follow-up, not an UNKNOWN.
    assert outcome["outcome"] == "completed"
    assert outcome["exit_code"] == 0


# --- C7-P3: session observation with ordinary Web connections -------------------------------


def test_observe_with_no_client_session_is_the_quiet_cluster(tmp_path):
    fake, _journal = _enrolled_at_head(tmp_path)
    observation = _adapter(fake).mode_b_observation()
    assert observation.database is not None
    assert observation.database.sessions == ()


def test_observe_accepts_one_client_session_on_the_serving_database(tmp_path):
    # The regression: a live Web connection used to be unpacked as four fields from a
    # two-field pair, so every scheduled backup failed while Web was serving.
    fake, _journal = _enrolled_at_head(tmp_path)
    fake.sessions = [("house_helper", 4242, "hh_app", "client backend")]
    observation = _adapter(fake).mode_b_observation()
    assert observation.database is not None
    assert observation.database.sessions == ((4242, "hh_app"),)


def test_observe_accepts_multiple_client_sessions_and_ignores_other_databases_and_backends(
    tmp_path,
):
    fake, _journal = _enrolled_at_head(tmp_path)
    fake.sessions = [
        ("house_helper", 4242, "hh_app", "client backend"),
        ("house_helper", 4243, "hh_app", "client backend"),
        ("postgres", 99, "postgres", "client backend"),
        ("house_helper", 77, "hh_backup", "walsender"),
    ]
    observation = _adapter(fake).mode_b_observation()
    assert observation.database is not None
    assert observation.database.sessions == ((4242, "hh_app"), (4243, "hh_app"))


@pytest.mark.parametrize(
    "row",
    [
        ("house_helper", 4242),
        ("house_helper", "not-a-pid", "hh_app", "client backend"),
        ("house_helper", "0", "hh_app", "client backend"),
    ],
)
def test_a_malformed_session_row_is_a_controlled_refusal(tmp_path, row):
    fake, _journal = _enrolled_at_head(tmp_path)
    fake.sessions = [row]
    with pytest.raises(enroll.DockerError) as refused:
        _adapter(fake).mode_b_observation()
    assert refused.value.code == "sessions_observation"


# --- §7.3 CONNECT handoffs (P0-1) -------------------------------------------------------------


def _enrolled_at_head(tmp_path):
    """A completed Mode A/Mode B fixture whose schema is at this image's head."""
    fake, journal_file = _enrolled(tmp_path)
    head, _chart = contract.known_revision_chart()
    fake.schema_versions = (head,)
    fake.public_tables = tuple(sorted(contract.expected_tables()))
    fake.inventory = _head_inventory()
    return fake, journal_file


def _handoff_records(journal_file):
    return [record for record in journal_file.read().records if record.kind == "handoff"]


def _run_handoff(fake, journal_file, role="hh_app", approved_by="owner"):
    return enroll.run_handoff(
        enroll.Docker(SPEC, runner=fake),
        journal_file,
        _adapter(fake),
        role=role,
        approved_by=approved_by,
    )


HANDOFF_OIDS = {
    "hh_app": "12",
    "hh_provision": "13",
    "hh_purge": "15",
    "hh_backup": "14",
}


@pytest.mark.parametrize(
    "role, event",
    [
        ("hh_app", "app"),
        ("hh_provision", "provision"),
        ("hh_purge", "scheduled"),
        ("hh_backup", "scheduled"),
    ],
)
def test_each_p0_handoff_grants_only_connect_and_journals_intent_then_outcome(
    tmp_path, role, event, capsys
):
    fake, journal_file = _enrolled_at_head(tmp_path)
    assert _run_handoff(fake, journal_file, role=role) == enroll.EX_OK
    assert fake.grants == [role]
    records = _handoff_records(journal_file)
    assert [record.payload["state"] for record in records] == ["intent", "outcome"]
    intent, outcome = records
    assert intent.payload["action"] == f"grant_connect:{role}"
    assert intent.payload["handoff"] == event
    assert intent.payload["database"] == "house_helper"
    assert intent.payload["operator"] == "owner"
    assert intent.payload["role"] == role
    assert outcome.payload["run_id"] == intent.payload["run_id"]
    assert outcome.payload["outcome"] == "observed"
    assert outcome.payload["evidence"]["has_database_privilege"] is True
    assert outcome.payload["evidence"]["has_temporary"] is False
    assert outcome.payload["evidence"]["has_create"] is False
    assert outcome.payload["evidence"]["changed"] is True
    # The operation is journaled with its session tag and its complete pre-state.
    assert intent.payload["application_name"] == enrollment.handoff_application_name(
        intent.payload["run_id"]
    )
    assert "before" in intent.payload
    # Exactly CONNECT: no TEMPORARY, no PUBLIC entry and no grant option.
    entries = [
        row for row in fake.acl_entries if row[0] == "house_helper" and row[2] == role
    ]
    assert entries == [
        ("house_helper", HANDOFF_OIDS[role], role, "CONNECT", "f")
    ]
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()
    assert "ok [handoff]" in capsys.readouterr().out


def test_the_h3_purge_handoff_preserves_the_existing_h3_backup_records(tmp_path, capsys):
    # C7-P3 adds the second H3 grantee without rewriting or reinterpreting the records the
    # earlier P0 cutover wrote for hh_backup. Both carry the frozen `handoff:scheduled`
    # event and remain independently resolvable.
    fake, journal_file = _enrolled_at_head(tmp_path)
    assert _run_handoff(fake, journal_file, role="hh_backup") == enroll.EX_OK
    before = _handoff_records(journal_file)
    assert _run_handoff(fake, journal_file, role="hh_purge") == enroll.EX_OK
    records = _handoff_records(journal_file)
    assert records[: len(before)] == before
    assert [record.payload["role"] for record in records] == [
        "hh_backup",
        "hh_backup",
        "hh_purge",
        "hh_purge",
    ]
    assert [record.payload["handoff"] for record in records] == ["scheduled"] * 4
    assert enrollment.successful_handoff_roles(journal_file.read().records) == frozenset(
        {"hh_backup", "hh_purge"}
    )
    assert fake.grants == ["hh_backup", "hh_purge"]
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()


def test_the_h3_purge_handoff_refuses_an_unjournaled_connect(tmp_path, capsys):
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake._grant_connect("hh_purge")  # an out-of-band GRANT: privilege, but no history
    assert _run_handoff(fake, journal_file, role="hh_purge") == enroll.EX_REFUSED
    assert fake.grants == []
    assert "handoff_unrecorded" in capsys.readouterr().err


def test_a_repeated_handoff_is_an_idempotent_observed_noop(tmp_path, capsys):
    fake, journal_file = _enrolled_at_head(tmp_path)
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
    # The second run issues no second GRANT; it records what it observed.
    assert fake.grants == ["hh_app"]
    outcomes = [
        record.payload
        for record in _handoff_records(journal_file)
        if record.payload["state"] == "outcome"
    ]
    assert [outcome["outcome"] for outcome in outcomes] == ["observed", "observed"]
    assert [outcome["evidence"]["changed"] for outcome in outcomes] == [True, False]
    assert "nothing was changed" in capsys.readouterr().out


def test_a_pre_existing_grant_without_a_journaled_handoff_is_refused(tmp_path, capsys):
    fake, journal_file = _enrolled_at_head(tmp_path)
    # Simulate an out-of-band GRANT: the privilege is there, the journal has no record.
    fake._grant_connect("hh_app")
    assert _run_handoff(fake, journal_file) == enroll.EX_REFUSED
    assert fake.grants == []
    assert _handoff_records(journal_file) == []
    assert "handoff_unrecorded" in capsys.readouterr().err


def test_the_maintenance_target_refuses_while_a_handoff_is_unknown(tmp_path, monkeypatch):
    """Backup and restore share `verify_target`; neither may advance through UNKNOWN."""
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: "interrupt"
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN

    real_journal = journal.Journal

    def portable_journal(directory, require_root=False):
        # The fixture runs unprivileged on POSIX CI; the gate under test is not ownership.
        return real_journal(directory, require_root=False)

    monkeypatch.setattr(maintenance.enroll, "load_spec", lambda path: SPEC)
    monkeypatch.setattr(
        maintenance.enroll,
        "Docker",
        lambda spec: types.SimpleNamespace(inspect_image=lambda: {}),
    )
    monkeypatch.setattr(
        maintenance.enroll, "validate_image", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        maintenance.enroll,
        "verify_mode_b",
        lambda journal_file, adapter, **kwargs: types.SimpleNamespace(
            verdict=types.SimpleNamespace(ok=True, mismatches=(), completed={})
        ),
    )
    monkeypatch.setattr(maintenance.journal, "Journal", portable_journal)

    with pytest.raises(maintenance.MaintenanceError) as refused:
        maintenance.verify_target(tmp_path / "compose.yaml", tmp_path / "cutover")

    assert refused.value.code == "handoff_unknown"
    assert "hh_app" in refused.value.message


def test_the_maintenance_cli_reports_a_pending_handoff_as_unknown(
    tmp_path, monkeypatch, capsys
):
    def refuse(*args, **kwargs):
        raise maintenance.MaintenanceError(
            "handoff_unknown", "the hh_app handoff intent has no observed outcome"
        )

    real_journal = journal.Journal

    def portable_journal(directory, require_root=False):
        # P0: the command takes the host operation interval first, so the journal it
        # opens must be a test-private one; ownership is not what this test exercises.
        return real_journal(directory, require_root=False)

    monkeypatch.setattr(maintenance.journal, "Journal", portable_journal)
    monkeypatch.setattr(maintenance, "verify_target", refuse)

    code = maintenance.main(
        [
            "--journal-dir",
            str(tmp_path / "cutover"),
            "--work-dir",
            str(tmp_path),
            "backup",
            "--output",
            str(tmp_path / "point.tar"),
        ]
    )

    assert code == maintenance.EX_UNKNOWN
    assert "handoff_unknown" in capsys.readouterr().err


def test_a_wrong_cluster_is_refused_and_never_grants(tmp_path):
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.container_id = "9" * 64
    assert _run_handoff(fake, journal_file) == enroll.EX_REFUSED
    assert fake.grants == []
    assert _handoff_records(journal_file) == []
    assert journal_file.read().records[-1].kind == "cluster_identity"
    assert "container:id" in journal_file.read().records[-1].payload["items"]


def test_a_mismatched_marker_is_refused_before_the_grant(tmp_path):
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.databases["house_helper"]["marker"] = journal.make_marker("0" * 32, SID)
    assert _run_handoff(fake, journal_file) == enroll.EX_REFUSED
    assert fake.grants == []
    assert _handoff_records(journal_file) == []
    assert "marker:token" in journal_file.read().records[-1].payload["items"]


def test_an_incomplete_enrollment_is_refused(tmp_path, capsys):
    fake = FakeDocker()
    journal_file = journal.Journal(tmp_path / "cutover")
    assert _run_handoff(fake, journal_file) == enroll.EX_REFUSED
    assert fake.grants == []
    assert "completed_record:missing" in capsys.readouterr().err


def test_a_schema_that_is_not_at_the_head_is_refused(tmp_path, capsys):
    fake, journal_file = _enrolled(tmp_path)  # schema contract only, no Alembic chain
    assert _run_handoff(fake, journal_file) == enroll.EX_REFUSED
    assert fake.grants == []
    assert _handoff_records(journal_file) == []
    assert "schema_not_head" in capsys.readouterr().err


def test_a_pending_migration_unknown_blocks_a_handoff(tmp_path):
    fake, journal_file = _enrolled_at_head(tmp_path)
    _timed_out_worker(fake, journal_file)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        _run_handoff(fake, journal_file)
    assert refused.value.code == "migration_unknown"
    assert fake.grants == []


def test_an_interrupted_grant_stays_pending_until_the_client_is_proven_gone(tmp_path):
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: "interrupt"
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN
    # The grant never reached the fixture; the intent is UNKNOWN, never success.
    assert fake.grants == []
    assert [record.payload["state"] for record in _handoff_records(journal_file)] == [
        "intent"
    ]
    pending = enrollment.pending_handoff_intents(journal_file.read().records)
    assert len(pending) == 1 and pending[0].payload["role"] == "hh_app"
    tag = enrollment.handoff_application_name(pending[0].payload["run_id"])

    # Finding 1: an instantaneous ACL snapshot is not enough. While the tagged client may
    # still execute, a later handoff is refused and no new intent is appended.
    fake.on_grant = None
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN
    assert fake.grants == []
    assert [record.payload["state"] for record in _handoff_records(journal_file)] == [
        "intent"
    ]

    # Once the tagged client/session is positively gone, the complete ACL shows the
    # recorded pre-state, so the intent resolves as unchanged and the handoff reruns.
    fake.handoff_transports[tag].update(
        {"client_alive": False, "session_alive": False}
    )
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
    assert fake.grants == ["hh_app"]
    payloads = [record.payload for record in _handoff_records(journal_file)]
    assert [payload["state"] for payload in payloads] == [
        "intent",
        "outcome",
        "intent",
        "outcome",
    ]
    assert payloads[1]["outcome"] == "reconciled"
    assert payloads[1]["run_id"] == payloads[0]["run_id"]
    assert payloads[1]["evidence"]["has_database_privilege"] is False
    assert payloads[1]["evidence"]["privileges_unchanged"] is True
    assert payloads[3]["outcome"] == "observed"
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()


def test_a_postgresql_refusal_resolves_the_intent_as_failed(tmp_path):
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: "server_refused"
    assert _run_handoff(fake, journal_file) == enroll.EX_REFUSED
    assert fake.grants == []
    payloads = [record.payload for record in _handoff_records(journal_file)]
    assert [payload["state"] for payload in payloads] == ["intent", "outcome"]
    assert payloads[1]["outcome"] == "failed"
    assert payloads[1]["evidence"] == {
        "code": "server_refused",
        "psql_exit": 3,
        "privileges_unchanged": True,
    }
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()


def test_a_committed_grant_followed_by_a_client_failure_is_not_failed(tmp_path):
    """Finding 2: psql exit 2 after a committed GRANT is not a PostgreSQL refusal."""
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: "commit_then_client_failure"
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
    assert fake.grants == ["hh_app"]
    outcomes = [
        record.payload
        for record in _handoff_records(journal_file)
        if record.payload["state"] == "outcome"
    ]
    assert [outcome["outcome"] for outcome in outcomes] == ["reconciled"]
    assert outcomes[0]["evidence"]["has_database_privilege"] is True
    assert outcomes[0]["evidence"]["applied"] is True
    assert outcomes[0]["evidence"]["psql_exit"] == 2
    assert all(outcome["outcome"] != "failed" for outcome in outcomes)
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()


def test_a_pending_noop_intent_resolves_without_transport_termination(tmp_path):
    """A no-op intent never issued a GRANT, so no tagged operation exists to terminate."""
    fake, journal_file = _enrolled_at_head(tmp_path)
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
    docker = enroll.Docker(SPEC, runner=fake)
    before = enroll._database_acl_state(docker, container_id=CONTAINER_ID)
    assert before.privilege("hh_app", "CONNECT") is True

    run_id = "1" * 32
    journal_file.append(
        "handoff",
        {
            "state": "intent",
            "action": "grant_connect:hh_app",
            "handoff": "app",
            "role": "hh_app",
            "database": "house_helper",
            "operator": "owner",
            "run_id": run_id,
            "application_name": enrollment.handoff_application_name(run_id),
            "before": before.as_journal(),
        },
    )
    assert enrollment.pending_handoff_intents(journal_file.read().records)
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
    payloads = [record.payload for record in _handoff_records(journal_file)]
    reconciled = payloads[3]
    assert reconciled["state"] == "outcome"
    assert reconciled["run_id"] == run_id
    assert reconciled["outcome"] == "reconciled"
    assert reconciled["evidence"]["privileges_unchanged"] is True
    assert reconciled["evidence"]["has_database_privilege"] is True
    assert reconciled["evidence"]["transport"] == "not_attempted"
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()


def test_a_noop_intent_whose_acl_changed_stays_unknown(tmp_path):
    fake, journal_file = _enrolled_at_head(tmp_path)
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
    docker = enroll.Docker(SPEC, runner=fake)
    before = enroll._database_acl_state(docker, container_id=CONTAINER_ID)
    run_id = "2" * 32
    journal_file.append(
        "handoff",
        {
            "state": "intent",
            "action": "grant_connect:hh_app",
            "handoff": "app",
            "role": "hh_app",
            "database": "house_helper",
            "operator": "owner",
            "run_id": run_id,
            "application_name": enrollment.handoff_application_name(run_id),
            "before": before.as_journal(),
        },
    )
    fake._grant_connect("hh_backup")  # an out-of-band change after the intent
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN
    outcomes = [
        record.payload
        for record in _handoff_records(journal_file)
        if record.payload["run_id"] == run_id and record.payload["state"] == "outcome"
    ]
    assert outcomes == []
    pending = enrollment.pending_handoff_intents(journal_file.read().records)
    assert len(pending) == 1 and pending[0].payload["run_id"] == run_id


def test_a_confirmed_launch_failure_is_resolved_not_started(tmp_path):
    """A Popen failure with positive container-side absence is not a running operation."""
    fake, journal_file = _enrolled_at_head(tmp_path)

    def launch_error(tag, sql):
        raise OSError("the docker client could not be launched")

    fake.on_grant = launch_error
    assert _run_handoff(fake, journal_file) == enroll.EX_REFUSED
    assert fake.grants == []
    payloads = [record.payload for record in _handoff_records(journal_file)]
    assert [payload["state"] for payload in payloads] == ["intent", "outcome"]
    assert payloads[1]["outcome"] == "failed"
    assert payloads[1]["evidence"]["code"] == "client_not_started"
    assert payloads[1]["evidence"]["privileges_unchanged"] is True
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()


def test_an_oserror_after_launch_stays_unknown(tmp_path):
    """A client error is not proof that the operation never started (Finding 2)."""
    fake, journal_file = _enrolled_at_head(tmp_path)

    def post_launch_error(tag, sql):
        fake._transport(tag, started=True, client_alive=True)
        fake.delayed[tag] = sql
        raise OSError("the pipe broke after launch")

    fake.on_grant = post_launch_error
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN
    assert fake.grants == []
    assert [record.payload["state"] for record in _handoff_records(journal_file)] == [
        "intent"
    ]
    assert enrollment.pending_handoff_intents(journal_file.read().records)


def test_a_delayed_grant_cannot_be_cleared_by_a_later_handoff(tmp_path):
    """Finding 1: H1 timeout -> H2 attempt -> delayed H1 completion."""
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: "delayed_inflight"
    assert _run_handoff(fake, journal_file, role="hh_app") == enroll.EX_UNKNOWN
    assert [record.payload["role"] for record in _handoff_records(journal_file)] == [
        "hh_app"
    ]

    # H2 is attempted while H1 is still in flight: it must be blocked, with no H2 intent.
    fake.on_grant = None
    assert _run_handoff(fake, journal_file, role="hh_provision") == enroll.EX_UNKNOWN
    assert [record.payload["role"] for record in _handoff_records(journal_file)] == [
        "hh_app"
    ]
    assert "hh_provision" not in fake.grants

    # The delayed H1 now commits and terminates. Only now may the H2 handoff proceed.
    delayed_tag = next(iter(fake.delayed))
    fake.complete_delayed(delayed_tag, apply=True)
    assert _run_handoff(fake, journal_file, role="hh_provision") == enroll.EX_OK
    payloads = [record.payload for record in _handoff_records(journal_file)]
    assert [payload["role"] for payload in payloads] == [
        "hh_app",
        "hh_app",
        "hh_provision",
        "hh_provision",
    ]
    assert payloads[1]["outcome"] == "reconciled"
    assert payloads[1]["evidence"]["has_database_privilege"] is True
    assert payloads[1]["evidence"]["applied"] is True
    assert payloads[3]["outcome"] == "observed"
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()


@pytest.mark.parametrize("status", [2, -15, 124, 125])
def test_a_docker_client_return_never_completes_an_active_operation(tmp_path, status):
    """Finding 2: statuses 2, -15, 124 and 125 never stand in for termination.

    Reproduces the Codex scenario: three overlapping H1 attempts were allowed while the
    first operation was still active, and H2 then proceeded. Every return now probes the
    tagged client/session first, and no new intent is appended until H1 is resolved.
    """
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: ("client_status", status)
    assert _run_handoff(fake, journal_file, role="hh_app") == enroll.EX_UNKNOWN
    assert fake.grants == []

    for _ in range(3):
        assert _run_handoff(fake, journal_file, role="hh_app") == enroll.EX_UNKNOWN
    assert [record.payload["state"] for record in _handoff_records(journal_file)] == [
        "intent"
    ]

    # H2 must not proceed while H1 is unresolved.
    assert (
        _run_handoff(fake, journal_file, role="hh_provision") == enroll.EX_UNKNOWN
    )
    assert [record.payload["role"] for record in _handoff_records(journal_file)] == [
        "hh_app"
    ]
    assert "hh_provision" not in fake.grants

    # Only positive termination of H1 releases H2.
    fake.on_grant = None
    tag = next(iter(fake.delayed))
    fake.complete_delayed(tag, apply=True, psql_exit=status)
    assert _run_handoff(fake, journal_file, role="hh_provision") == enroll.EX_OK
    payloads = [record.payload for record in _handoff_records(journal_file)]
    assert [payload["role"] for payload in payloads] == [
        "hh_app",
        "hh_app",
        "hh_provision",
        "hh_provision",
    ]
    assert payloads[1]["outcome"] == "reconciled"
    assert payloads[1]["evidence"]["applied"] is True
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()


@pytest.mark.parametrize("psql_exit", [0, 2])
def test_a_grant_committing_between_readback_and_probe_is_never_resolved_stale(
    tmp_path, psql_exit
):
    """Finding 3: the ACL is read only after termination, never before it.

    The delayed GRANT lands between the old readback point and the probe. Under the
    corrected order the probe runs first and reports the operation still active, so the
    intent stays UNKNOWN; the stale pre-state can never become `failed` (psql exit 0) or
    `reconciled unchanged` (psql exit 2).
    """
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: ("delayed_readback", psql_exit)
    assert _run_handoff(fake, journal_file, role="hh_app") == enroll.EX_UNKNOWN
    assert [record.payload["state"] for record in _handoff_records(journal_file)] == [
        "intent"
    ]
    assert fake.grants == []

    tag = next(iter(fake.delayed))
    fake.complete_delayed(tag, apply=True, psql_exit=psql_exit)
    fake.on_grant = None
    assert _run_handoff(fake, journal_file, role="hh_app") == enroll.EX_OK
    payloads = [record.payload for record in _handoff_records(journal_file)]
    assert payloads[1]["outcome"] == "reconciled"
    assert payloads[1]["evidence"]["applied"] is True
    assert payloads[1]["evidence"]["psql_exit"] == psql_exit
    assert all(
        payload["outcome"] != "failed"
        for payload in payloads
        if payload["state"] == "outcome"
    )
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()


def test_a_container_replaced_at_grant_launch_is_never_granted(tmp_path):
    """Finding 5: a replacement container with the same name is never a GRANT target."""
    fake, journal_file = _enrolled_at_head(tmp_path)

    def replace_container():
        fake.container_id = "9" * 64

    fake.on_handoff_exec = replace_container
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN
    assert fake.grants == []
    assert [record.payload["state"] for record in _handoff_records(journal_file)] == [
        "intent"
    ]
    assert enrollment.pending_handoff_intents(journal_file.read().records)


def test_the_handoff_mutation_and_probe_run_against_the_immutable_container_id(
    tmp_path,
):
    """Finding 5: GRANT and probe never exec against the mutable Compose name."""
    fake, journal_file = _enrolled_at_head(tmp_path)
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
    assert fake.handoff_exec_targets
    assert set(fake.handoff_exec_targets) == {CONTAINER_ID}


def test_an_unobservable_readback_keeps_the_intent_pending(tmp_path, capsys):
    """Finding 2: an uncertain readback never becomes a resolution."""
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.acl_matrix_fail_on_call = 2  # the post-grant observation
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN
    assert fake.grants == ["hh_app"]
    assert [record.payload["state"] for record in _handoff_records(journal_file)] == [
        "intent"
    ]
    assert enrollment.pending_handoff_intents(journal_file.read().records)
    assert "remains pending" in capsys.readouterr().err


def test_a_pending_grant_with_a_grant_option_and_create_is_never_reconciled(
    tmp_path, capsys
):
    """Finding 3.1: an unsafe pending ACL is never marked as reconciled."""
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: "delayed_inflight"
    assert _run_handoff(fake, journal_file, role="hh_app") == enroll.EX_UNKNOWN
    tag = next(iter(fake.delayed))
    fake.delayed.pop(tag)

    # The delayed GRANT committed an unsafe effect: CONNECT WITH GRANT OPTION plus CREATE.
    fake.grants.append("hh_app")
    fake._grant_connect("hh_app")
    fake.acl_entries[-1] = ("house_helper", "12", "hh_app", "CONNECT", "t")
    fake.acl_entries.append(("house_helper", "12", "hh_app", "CREATE", "f"))
    fake._transport(
        tag, started=True, stopped=True, psql_exit=0, client_alive=False
    )
    capsys.readouterr()

    assert _run_handoff(fake, journal_file, role="hh_provision") == enroll.EX_UNKNOWN
    assert [record.payload["state"] for record in _handoff_records(journal_file)] == [
        "intent"
    ]
    assert enrollment.pending_handoff_intents(journal_file.read().records)
    error = capsys.readouterr().err
    assert "remains UNKNOWN" in error
    assert "hh_provision" not in fake.grants


def test_an_unjournaled_connect_on_another_role_is_refused(tmp_path, capsys):
    """Finding 3.2: CONNECT provenance is checked for every login role."""
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake._grant_connect("hh_backup")
    assert _run_handoff(fake, journal_file, role="hh_app") == enroll.EX_REFUSED
    assert fake.grants == []
    assert _handoff_records(journal_file) == []
    error = capsys.readouterr().err
    assert "handoff_privilege" in error
    assert "unrecorded_connect:hh_backup" in error


def test_a_collateral_grant_to_another_role_fails_the_handoff(tmp_path):
    """Finding 3.3: any privilege change outside the single CONNECT is collateral."""
    fake, journal_file = _enrolled_at_head(tmp_path)

    def collateral(tag, sql):
        role = sql.split('TO "', 1)[1].split('"', 1)[0]
        fake._grant_connect(role)
        fake._grant_connect("hh_backup")
        fake.acl_entries.append(("house_helper", "14", "hh_backup", "CREATE", "f"))
        return "apply"

    fake.on_grant = collateral
    assert _run_handoff(fake, journal_file, role="hh_app") == enroll.EX_REFUSED
    outcome = _handoff_records(journal_file)[-1].payload
    assert outcome["outcome"] == "failed"
    items = outcome["evidence"]["items"]
    assert "acl_entry:hh_backup:CONNECT:0" in items
    assert "unexpected_privilege:hh_backup:CREATE" in items
    assert "create:hh_backup" in items


def test_the_owners_inherent_grant_options_are_not_collateral_privileges():
    """Real PostgreSQL materializes the owner's `CTc` entries with grant option."""
    effective = tuple(
        (
            role,
            privilege,
            role == contract.OWNER_ROLE or (role == "hh_migrate" and privilege == "CONNECT"),
        )
        for privilege in contract.DATABASE_PRIVILEGES
        for role in contract.PROJECT_ROLE_NAMES
    )
    state = enrollment.DatabaseAclState(
        acl_materialized=True,
        entries=(
            ("hh_owner", "CONNECT", True),
            ("hh_owner", "CREATE", True),
            ("hh_owner", "TEMPORARY", True),
            ("hh_migrate", "CONNECT", False),
        ),
        effective=effective,
    )
    assert enrollment.database_acl_failures(state, records=()) == ()


def test_handoff_termination_requires_positive_client_liveness_evidence():
    evidence = enrollment.HandoffTransportEvidence
    assert enrollment.handoff_termination_failures(evidence(observed=False)) == (
        "transport:unobserved",
    )
    assert enrollment.handoff_termination_failures(
        evidence(observed=True, client_alive=True)
    ) == ("transport:client_alive",)
    assert enrollment.handoff_termination_failures(
        evidence(observed=True, session_alive=True)
    ) == ("transport:session_alive",)
    assert enrollment.handoff_termination_failures(evidence(observed=True)) == (
        "transport:never_started",
    )
    assert (
        enrollment.handoff_termination_failures(evidence(observed=True, started=True))
        == ()
    )
    assert (
        enrollment.handoff_termination_failures(
            evidence(observed=True, psql_exit=2)
        )
        == ()
    )


def test_an_intent_with_a_foreign_application_name_is_journal_corruption(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    payload = _handoff_intent_payload("hh_app", "0" * 32)
    payload["application_name"] = enrollment.handoff_application_name("1" * 32)
    journal_file.append("handoff", payload)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.pending_handoff_intents(journal_file.read().records)
    assert refused.value.code == "handoff_journal"


def test_an_intent_without_its_recorded_pre_state_is_journal_corruption(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    payload = _handoff_intent_payload("hh_app", "0" * 32)
    del payload["before"]
    journal_file.append("handoff", payload)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.pending_handoff_intents(journal_file.read().records)
    assert refused.value.code == "handoff_journal"


def _handoff_outcome_payload(role, run_id, *, outcome="observed", evidence=None):
    return {
        "state": "outcome",
        "action": f"grant_connect:{role}",
        "handoff": enrollment.HANDOFF_EVENTS[role],
        "role": role,
        "database": "house_helper",
        "operator": "owner",
        "run_id": run_id,
        "outcome": outcome,
        "evidence": (
            evidence
            if evidence is not None
            else {"has_database_privilege": True, "changed": True}
        ),
    }


def _connected_acl_state_journal(role):
    base = enrollment.DatabaseAclState.from_journal(_acl_state_journal())
    return enrollment.expected_after_handoff(base, role).as_journal()


def test_a_structurally_valid_orphan_outcome_is_refused(tmp_path):
    """Finding 4: an outcome with no matching prior intent can never authorize CONNECT."""
    journal_file = journal.Journal(tmp_path / "cutover")
    journal_file.append("handoff", _handoff_outcome_payload("hh_app", "0" * 32))
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.pending_handoff_intents(journal_file.read().records)
    assert refused.value.code == "handoff_journal"
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.successful_handoff_roles(journal_file.read().records)


def test_an_orphan_outcome_cannot_justify_an_otherwise_unjournaled_connect(tmp_path):
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake._grant_connect("hh_app")  # an out-of-band CONNECT with no journaled handoff
    journal_file.append("handoff", _handoff_outcome_payload("hh_app", "0" * 32))
    with pytest.raises(enrollment.EnrollmentError) as refused:
        _run_handoff(fake, journal_file)
    assert refused.value.code == "handoff_journal"
    assert fake.grants == []


def test_a_duplicate_outcome_is_journal_corruption(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    run_id = "0" * 32
    journal_file.append("handoff", _handoff_intent_payload("hh_app", run_id))
    journal_file.append("handoff", _handoff_outcome_payload("hh_app", run_id))
    journal_file.append("handoff", _handoff_outcome_payload("hh_app", run_id))
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.pending_handoff_intents(journal_file.read().records)
    assert refused.value.code == "handoff_journal"


def test_a_duplicate_operation_id_is_journal_corruption(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    run_id = "0" * 32
    journal_file.append("handoff", _handoff_intent_payload("hh_app", run_id))
    journal_file.append("handoff", _handoff_intent_payload("hh_app", run_id))
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.pending_handoff_intents(journal_file.read().records)
    assert refused.value.code == "handoff_journal"


def test_an_outcome_before_any_intent_is_journal_corruption(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    journal_file.append("handoff", _handoff_outcome_payload("hh_app", "0" * 32))
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.successful_handoff_roles(journal_file.read().records)
    assert refused.value.code == "handoff_journal"


@pytest.mark.parametrize(
    "outcome, evidence",
    [
        # claims a change its pre-state already contained
        ("observed", {"has_database_privilege": True, "changed": True}),
        # a failed outcome cannot establish the privilege
        ("failed", {"has_database_privilege": True, "code": "server_refused"}),
        # a reconciliation must be exactly applied or exactly unchanged
        (
            "reconciled",
            {
                "has_database_privilege": True,
                "reconciled": True,
                "applied": True,
                "privileges_unchanged": True,
            },
        ),
        # an observed outcome must record the observed privilege
        ("observed", {"changed": True}),
    ],
)
def test_a_semantically_inconsistent_history_is_refused(tmp_path, outcome, evidence):
    journal_file = journal.Journal(tmp_path / "cutover")
    run_id = "0" * 32
    journal_file.append(
        "handoff",
        _handoff_intent_payload(
            "hh_app", run_id, before=_connected_acl_state_journal("hh_app")
        ),
    )
    journal_file.append(
        "handoff",
        _handoff_outcome_payload("hh_app", run_id, outcome=outcome, evidence=evidence),
    )
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.pending_handoff_intents(journal_file.read().records)
    assert refused.value.code == "handoff_journal"


def test_a_valid_noop_outcome_confirms_earlier_provenance(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    first = "0" * 32
    journal_file.append("handoff", _handoff_intent_payload("hh_app", first))
    journal_file.append("handoff", _handoff_outcome_payload("hh_app", first))
    noop = "1" * 32
    journal_file.append(
        "handoff",
        _handoff_intent_payload(
            "hh_app", noop, before=_connected_acl_state_journal("hh_app")
        ),
    )
    journal_file.append(
        "handoff",
        _handoff_outcome_payload(
            "hh_app",
            noop,
            evidence={"has_database_privilege": True, "changed": False},
        ),
    )
    assert enrollment.pending_handoff_intents(journal_file.read().records) == ()
    assert enrollment.successful_handoff_roles(journal_file.read().records) == {
        "hh_app"
    }


def test_a_lone_noop_outcome_cannot_create_provenance(tmp_path):
    """A no-op only confirms an earlier journaled CONNECT; it never launders one."""
    journal_file = journal.Journal(tmp_path / "cutover")
    run_id = "0" * 32
    journal_file.append(
        "handoff",
        _handoff_intent_payload(
            "hh_app", run_id, before=_connected_acl_state_journal("hh_app")
        ),
    )
    journal_file.append(
        "handoff",
        _handoff_outcome_payload(
            "hh_app",
            run_id,
            evidence={"has_database_privilege": True, "changed": False},
        ),
    )
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.successful_handoff_roles(journal_file.read().records)
    assert refused.value.code == "handoff_journal"


def test_public_access_or_temporary_privilege_is_refused_before_the_intent(tmp_path):
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.acl_entries.append(("house_helper", "0", "PUBLIC", "CONNECT", "f"))
    fake.databases["house_helper"]["acl"] = True
    assert _run_handoff(fake, journal_file) == enroll.EX_REFUSED
    assert fake.grants == []
    assert _handoff_records(journal_file) == []

    fake2, journal_file2 = _enrolled_at_head(tmp_path / "second")
    fake2.acl_entries.append(("house_helper", "12", "hh_app", "TEMPORARY", "f"))
    fake2.databases["house_helper"]["acl"] = True
    assert _run_handoff(fake2, journal_file2) == enroll.EX_REFUSED
    assert fake2.grants == []
    assert _handoff_records(journal_file2) == []


def test_a_grant_with_a_grant_option_is_an_observed_failure(tmp_path):
    class WithGrantOption(FakeDocker):
        def _grant_connect(self, role):
            self.databases["house_helper"]["acl"] = True
            self.acl_entries.append(("house_helper", "12", role, "CONNECT", "t"))

    fake = WithGrantOption()
    journal_file = journal.Journal(tmp_path / "cutover")
    enrollment.ModeA(
        journal_file, _adapter(fake), token_source=lambda: "f" * 32
    ).run()
    head, _chart = contract.known_revision_chart()
    fake.schema_versions = (head,)
    fake.public_tables = tuple(sorted(contract.expected_tables()))
    fake.inventory = _head_inventory()
    assert _run_handoff(fake, journal_file) == enroll.EX_REFUSED
    outcome = _handoff_records(journal_file)[-1].payload
    assert outcome["state"] == "outcome" and outcome["outcome"] == "failed"
    assert "grant_option:hh_app:CONNECT" in outcome["evidence"]["items"]
    assert "acl_entry:hh_app:CONNECT:1" in outcome["evidence"]["items"]


def test_a_grant_that_also_adds_another_privilege_is_an_observed_failure(tmp_path):
    class WithExtraPrivilege(FakeDocker):
        def _grant_connect(self, role):
            super()._grant_connect(role)
            self.acl_entries.append(("house_helper", "12", role, "CREATE", "f"))

    fake = WithExtraPrivilege()
    journal_file = journal.Journal(tmp_path / "cutover")
    enrollment.ModeA(
        journal_file, _adapter(fake), token_source=lambda: "f" * 32
    ).run()
    head, _chart = contract.known_revision_chart()
    fake.schema_versions = (head,)
    fake.public_tables = tuple(sorted(contract.expected_tables()))
    fake.inventory = _head_inventory()
    assert _run_handoff(fake, journal_file) == enroll.EX_REFUSED
    outcome = _handoff_records(journal_file)[-1].payload
    assert outcome["outcome"] == "failed"
    assert "unexpected_privilege:hh_app:CREATE" in outcome["evidence"]["items"]
    assert "create:hh_app" in outcome["evidence"]["items"]


def test_grant_connect_names_only_the_serving_database_and_authorized_roles(tmp_path):
    fake = FakeDocker()
    docker = enroll.Docker(SPEC, runner=fake)
    with pytest.raises(enroll.DockerError) as refused:
        enroll.grant_connect(
            docker, "postgres", run_id="0" * 32, container_id=CONTAINER_ID
        )
    assert refused.value.code == "handoff_role"
    with pytest.raises(enroll.DockerError) as refused:
        enroll.grant_connect(
            docker,
            "hh_app",
            run_id="0" * 32,
            container_id=CONTAINER_ID,
            database="other_database",
        )
    assert refused.value.code == "handoff_database"
    assert fake.grants == []


def test_the_handoff_command_refuses_an_unauthorized_role_before_docker(monkeypatch):
    class Args:
        # A maintenance login is not an H1/H2/H3 handoff grantee; hh_purge IS one since
        # C7-P3 and is covered by the parametrized authorized-role test above.
        role = "hh_restore"
        approved_by = "owner"

    monkeypatch.setattr(
        enroll, "load_spec", lambda *a, **k: pytest.fail("Docker must not be touched")
    )
    assert enroll._handoff_command(Args()) == enroll.EX_REFUSED


@pytest.mark.parametrize(
    "label",
    ["  ", "bad\nlabel", "bad\x85label", "bad label", "x" * 201],
)
def test_the_handoff_command_requires_a_single_printable_operator_label(label):
    class Args:
        role = "hh_app"
        approved_by = label

    assert enroll._handoff_command(Args()) == enroll.EX_USAGE


def test_the_handoff_cli_refuses_an_unauthorized_role_without_touching_docker(tmp_path):
    code = enroll.main(
        [
            "--journal-dir",
            str(tmp_path / "cutover"),
            "handoff",
            "--role",
            "hh_purge",
            "--approved-by",
            "owner",
        ]
    )
    assert code == enroll.EX_REFUSED


def test_the_handoff_cli_refuses_a_blank_operator_label(tmp_path):
    code = enroll.main(
        [
            "--journal-dir",
            str(tmp_path / "cutover"),
            "handoff",
            "--role",
            "hh_app",
            "--approved-by",
            "  ",
        ]
    )
    assert code == enroll.EX_USAGE


def test_a_malformed_handoff_payload_is_journal_corruption(tmp_path):
    journal_file = journal.Journal(tmp_path / "cutover")
    journal_file.append(
        "handoff",
        {
            "state": "intent",
            "action": "grant_connect:postgres",
            "handoff": "app",
            "role": "postgres",
            "database": "house_helper",
            "operator": "owner",
            "run_id": "0" * 32,
        },
    )
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.pending_handoff_intents(journal_file.read().records)
    assert refused.value.code == "handoff_journal"


def _handoff_intent_payload(role, run_id, *, before=None):
    return {
        "state": "intent",
        "action": f"grant_connect:{role}",
        "handoff": enrollment.HANDOFF_EVENTS[role],
        "role": role,
        "database": "house_helper",
        "operator": "owner",
        "run_id": run_id,
        "application_name": enrollment.handoff_application_name(run_id),
        "before": before if before is not None else _acl_state_journal(),
    }


def _acl_state_journal():
    effective = tuple(
        (role, privilege, False)
        for privilege in contract.DATABASE_PRIVILEGES
        for role in contract.PROJECT_ROLE_NAMES
    )
    return enrollment.DatabaseAclState(
        acl_materialized=True, entries=(), effective=effective
    ).as_journal()


def test_a_cross_role_outcome_is_journal_corruption(tmp_path):
    """Finding 4: an outcome may not be paired with another role's intent."""
    journal_file = journal.Journal(tmp_path / "cutover")
    run_id = "0" * 32
    journal_file.append(
        "handoff",
        _handoff_intent_payload("hh_app", run_id),
    )
    journal_file.append(
        "handoff",
        {
            "state": "outcome",
            "action": "grant_connect:hh_provision",
            "handoff": "provision",
            "role": "hh_provision",
            "database": "house_helper",
            "operator": "owner",
            "run_id": run_id,
            "outcome": "observed",
            "evidence": {"has_database_privilege": True, "changed": True},
        },
    )
    with pytest.raises(enrollment.EnrollmentError) as refused:
        enrollment.pending_handoff_intents(journal_file.read().records)
    assert refused.value.code == "handoff_journal"
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.successful_handoff_roles(journal_file.read().records)


def test_a_tampered_journal_is_refused_before_a_handoff(tmp_path):
    fake, journal_file = _enrolled_at_head(tmp_path)
    with journal_file.path.open("ab") as handle:
        handle.write(b'{"not":"a valid record"}\n')
    with pytest.raises(journal.JournalError):
        _run_handoff(fake, journal_file)
    assert fake.grants == []


def test_status_reports_a_pending_handoff_as_unknown(tmp_path, capsys):
    fake, journal_file = _enrolled_at_head(tmp_path)
    fake.on_grant = lambda tag, sql: "interrupt"
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN
    capsys.readouterr()
    assert enroll._print_status(journal_file) == enroll.EX_OK
    output = capsys.readouterr().out
    assert "handoff UNKNOWN" in output
    assert "hh_app" in output


# --- LCK-08: a pending lock handoff fences every advancement ---------------------------------


def _pending_lock_intent(journal_file, run_id="e" * 32):
    journal_file.append(
        "handoff_lock",
        {
            "state": "intent",
            "action": "observe_lock_handoff",
            "handoff": "lock",
            "operator": "owner",
            "run_id": run_id,
        },
    )


def _failed_lock_outcome(journal_file, run_id="e" * 32):
    journal_file.append(
        "handoff_lock",
        {
            "state": "outcome",
            "action": "observe_lock_handoff",
            "handoff": "lock",
            "operator": "owner",
            "run_id": run_id,
            "outcome": "failed",
            "evidence": {
                "container_id": WEB_ID,
                "state_volume": SPEC.state_volume,
                "lock_present": True,
                "lock_device": "8:1",
                "lock_inode": 12345,
                "idle_exit": 0,
                "idle_code": None,
                "holder_pid": None,
                "holder_start_ticks": None,
                "holder_access": None,
                "observations": 2,
                "witness_access": None,
                "observation_boundary": None,
            },
        },
    )


def test_a_pending_lock_handoff_blocks_a_grant_handoff(tmp_path, capsys):
    fake, journal_file = _enrolled_at_head(tmp_path)
    _pending_lock_intent(journal_file)
    before = journal_file.read().records
    assert _run_handoff(fake, journal_file) == enroll.EX_UNKNOWN
    assert "lock handoff intent" in capsys.readouterr().err
    assert fake.grants == []
    assert journal_file.read().records == before


def test_a_pending_lock_handoff_blocks_a_migration(tmp_path, capsys):
    fake, journal_file = _enrolled_at_head(tmp_path)
    _pending_lock_intent(journal_file)
    before_operations = list(fake.operations)
    code = enroll.run_migrate(
        enroll.Docker(SPEC, runner=fake),
        enroll.DEFAULT_COMPOSE,
        journal_file,
        _adapter(fake),
    )
    assert code == enroll.EX_UNKNOWN
    assert "no migration was started" in capsys.readouterr().err
    assert fake.operations == before_operations


def test_a_pending_lock_handoff_blocks_reconciliation(tmp_path, capsys):
    _, journal_file = _enrolled_at_head(tmp_path)
    _pending_lock_intent(journal_file)
    # The gate is ahead of every Docker/database observation, so placeholders suffice.
    code = enroll.reconcile_migration_command(object(), journal_file, object())
    assert code == enroll.EX_UNKNOWN
    assert "no migration outcome" in capsys.readouterr().err


def test_an_invalid_lock_history_blocks_even_when_grant_records_are_valid(tmp_path):
    # A caller that only inspects grant events must not silently ignore a corrupted lock
    # history: the shared gate validates it and fails closed.
    fake, journal_file = _enrolled_at_head(tmp_path)
    _pending_lock_intent(journal_file, run_id="a" * 32)
    _pending_lock_intent(journal_file, run_id="b" * 32)
    with pytest.raises(enrollment.EnrollmentError) as refused:
        _run_handoff(fake, journal_file)
    assert refused.value.code == "lock_handoff_journal"
    assert fake.grants == []


def test_a_resolved_failed_lock_outcome_does_not_block_advancement(tmp_path):
    # A coherent negative is a resolution, not an UNKNOWN; only an unresolved intent (or
    # corrupt evidence) fences later transitions.
    fake, journal_file = _enrolled_at_head(tmp_path)
    _pending_lock_intent(journal_file)
    _failed_lock_outcome(journal_file)
    assert _run_handoff(fake, journal_file) == enroll.EX_OK
