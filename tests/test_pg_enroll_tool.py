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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from deploy.postgres import enroll
from house_helper.persistence import pg_contract as contract
from house_helper.persistence import pg_enrollment as enrollment
from house_helper.persistence import pg_journal as journal

SPEC = enroll.ClusterSpec(
    project="house-helper-pg",
    container_name="house-helper-db",
    image="postgres@sha256:" + "a" * 64,
    volume="house-helper-pgdata",
    network="house-helper-pg-db",
    postgresql_conf_source="/srv/house-helper/postgres/postgresql.conf",
    pg_hba_source="/srv/house-helper/postgres/pg_hba.conf",
    bootstrap_credential_source="/srv/house-helper/postgres/credentials/postgres_superuser.password",
)
CONTAINER_ID = "b" * 64
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
        self.compose_input = None
        self.compose_argv = None
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

    # -- the runner interface ----------------------------------------------------------

    def __call__(self, argv, *, input=None, capture_output=True, timeout=None, check=False, env=None):
        if argv[:3] == ["docker", "inspect", "--type"]:
            target = argv[-1]
            if target in (
                self.container_name,
                self.container_name.lstrip("/"),
                SPEC.container_name,
            ):
                return self._json([self._container()])
            for worker in self.worker_containers:
                if target in (worker["Id"], worker["Names"]):
                    return self._json([self._worker_inspect(worker)])
            return subprocess.CompletedProcess(argv, 1, b"", b"no such container")
        if argv[:3] == ["docker", "volume", "inspect"]:
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
        if argv[:2] == ["docker", "exec"]:
            database = argv[argv.index("-d") + 1]
            return subprocess.CompletedProcess(argv, 0, self._psql(database, input.decode()), b"")
        if argv[:2] == ["docker", "compose"]:
            self.compose_input = input
            self.compose_argv = list(argv)
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
            return _out([])
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
