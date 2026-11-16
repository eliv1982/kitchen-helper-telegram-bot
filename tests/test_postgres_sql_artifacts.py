"""HH-DB-01 Stage 2 WP-1B - the SQL artifacts on a real PostgreSQL 16 (opt-in).

Scope: `db/roles.sql`, `db/bootstrap.sql`, `db/privileges.sql` and the validated runner in
`tests/postgres_sql.py`, against the SAME disposable cluster of
compose.test-postgres.yaml that Stage 1 uses, through the same independent identity and
safety boundary (`tests/postgres_harness.py`). Nothing here is deployed, and no Production,
n8n or ETL service is contacted.

The tests create the seven LITERAL project roles (`hh_owner` ... `hh_restore`) and two
databases (`house_helper`, `house_helper_restore_<token>`). They refuse to adopt a
pre-existing role or database, and every fixture drops what it created. Test-only
credentials are generated per process for the six logins AFTER roles.sql; no credential is
in the artifacts and none is printed.

One negative test needs an UNRELATED default-ACL recipient. It creates a disposable
NOLOGIN role under a unique generated name and drops it again, so the suite never depends
on a role (the bootstrap user included) happening to exist on the cluster.

The setup-failure lifecycle tests (which need a cluster with NO project resources, so they
cannot share this module's module-scoped `project` fixture) live in
`tests/test_postgres_sql_setup_failures.py` and import this module's provisioning helpers.

Opt-in, like the Stage 1 suites:

    docker compose -f compose.test-postgres.yaml up -d
    HOUSE_HELPER_TEST_POSTGRES_URL=postgresql+psycopg://hh_test:hh_test_only_not_a_secret@127.0.0.1:55432/postgres \\
        pytest tests/test_postgres_sql_artifacts.py -v -ra
"""

import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace

import psycopg
import pytest
import sqlalchemy as sa

from house_helper.persistence.database import create_session_factory, upgrade_database
from house_helper.persistence.identifiers import normalize_identity
from house_helper.persistence.models import MAX_SUBJECT_LENGTH
from tests import postgres_contract as contract
from tests import postgres_harness as harness
from tests import postgres_sql as artifacts
from tests.persistence_support import entry_row, new_user, raw_insert
from tests.postgres_harness import (
    POSTGRES_URL_ENV,
    PURGE_COLUMN_UPDATES,
    PURGE_TABLES,
    PROVISION_TABLES,
    run_from_environment,
)
from tests.revisions import HEAD_REVISION
from tests.test_persistence_identifiers import SUBJECTS

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_URL_ENV),
        reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL "
        "(see tests/test_admin_postgres.py)",
    ),
]

NON_PURGE_TABLES = (
    "users",
    "allowlist_entries",
    "identity_links",
    "favorite_recipes",
    "shopping_lists",
    "shopping_list_items",
    "shopping_list_requests",
    "message_recipes",
    "telegram_links",
    "telegram_active_conversations",
)
APPLICATION_TABLES = frozenset(PURGE_TABLES) | frozenset(NON_PURGE_TABLES)
RESTORE_NAME = re.compile(r"house_helper_restore_[a-z0-9]{1,32}")
# The complete frozen default-ACL set: schema-specific (`public`) entries for tables and
# sequences, every one without a grant option. Any GLOBAL entry naming a non-owner grantee,
# any PUBLIC entry and any grant option is drift (R1-01/R1-02).
EXPECTED_DEFAULTS = frozenset({
    ("public", "S", "hh_app", "USAGE", False),
    ("public", "S", "hh_backup", "SELECT", False),
    ("public", "r", "hh_app", "DELETE", False),
    ("public", "r", "hh_app", "INSERT", False),
    ("public", "r", "hh_app", "SELECT", False),
    ("public", "r", "hh_app", "UPDATE", False),
    ("public", "r", "hh_backup", "SELECT", False),
})
# Every database THIS process created for these tests. The last-resort teardown drops
# only these; a pre-existing database is refused at setup, never adopted or dropped.
_CREATED_BY_THIS_MODULE: set = set()
# R1-05/R2-01: every project role this process may have created and every maintenance
# grant it may have made. Roles are registered BEFORE their creation transaction starts,
# so a failure the moment the COMMIT lands (or anywhere later) is still cleaned up; dropping
# a claimed name that a clean rollback never created is a no-op.
_CREATED_ROLE_NAMES: set = set()
_MAINTENANCE_GRANTS: list = []  # (role, database) pairs granted CONNECT by this process
# CI-FAIL-01: the disposable NOLOGIN roles this process created as an UNRELATED default-ACL
# recipient. Each name is registered BEFORE its CREATE ROLE, so a failure after the
# statement (or an ambiguous acknowledgement) still drops it if it exists. The test that
# creates one drops it again; a drop that fails stays registered and the module teardown
# retries it once the databases holding the dependency are gone.
_CREATED_UNRELATED_ROLES: set = set()
UNRELATED_ROLE = re.compile(r"hh_unrelated_[0-9a-f]{16}")


# --- fixtures ---------------------------------------------------------------------------------


@dataclass
class Project:
    run: harness.Run
    script: artifacts.RolesScript
    passwords: dict = field(repr=False)  # generated test-only credentials: never printed
    created_with_null_passwords: bool


@pytest.fixture(scope="module")
def project_run():
    run = run_from_environment()
    run.verified_cluster()  # the independent identity, before any artifact is applied
    yield run
    run.close()


def _claim_roles(names: frozenset) -> None:
    """Register the project roles this run may create, BEFORE the transaction runs.

    The names come from `RolesScript.apply`'s pre-execution snapshot, so a role that
    already existed is never claimed - the registry can never authorize dropping a role
    this process did not create. A claimed name that a rolled-back transaction never
    created is dropped with IF EXISTS and is otherwise absent."""
    _CREATED_ROLE_NAMES.update(names)


def _provision_project(run, control) -> Project:
    """Create the seven literal roles, then their test-only credentials, then the
    maintenance CONNECT handoff.

    Every resource is registered before or as it is created, and a failure anywhere - role
    creation, verification before COMMIT, password initialization, the maintenance grant
    or even the COMMIT acknowledgement itself - runs the same verified cleanup before it
    propagates, so nothing created before the fixture yielded can leak. A pre-existing role
    or database is refused, never adopted and never dropped."""
    run.verified_cluster()
    script = artifacts.RolesScript()
    pre_existing = [
        row[0]
        for row in control.execute(
            "SELECT datname FROM pg_database WHERE datname = 'house_helper' "
            "OR datname ~ '^house_helper_restore_[a-z0-9]{1,32}$'"
        ).fetchall()
    ]
    if pre_existing:
        pytest.fail(
            "serving/restore databases already exist on this cluster; refusing to adopt "
            "or drop them: " + ", ".join(sorted(pre_existing))
        )
    present = [
        row[0]
        for row in control.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            (list(artifacts.PROJECT_ROLE_NAMES),),
        ).fetchall()
    ]
    if present:
        pytest.fail(
            "project roles already exist on this cluster; refusing to adopt them: "
            + ", ".join(sorted(present))
            + " (drop them before rerunning)"
        )
    try:
        # R2-01: the claim runs BEFORE the creation transaction. Whatever fails afterwards
        # - role creation, verification before COMMIT, password initialisation, the
        # maintenance grant, or the COMMIT acknowledgement itself - the roles this run may
        # have created are already registered for the verified cleanup below. A role that
        # existed before the run is never claimed (the pre-existing check above also
        # refuses it), so the registry can never authorize dropping someone else's role.
        result = script.apply(control, claim=_claim_roles)
        assert result.created == frozenset(artifacts.PROJECT_ROLE_NAMES)
        initial = {
            login: artifacts.password_verifier(control, login)
            for login in artifacts.LOGIN_ROLE_NAMES
        }
        created_with_null_passwords = all(value is None for value in initial.values())
        assert control.execute("SHOW password_encryption").fetchone()[0] == "scram-sha-256"
        passwords = artifacts.generate_test_passwords()
        for login, password in passwords.items():
            artifacts.set_test_password(control, login, password)
        # RST-03: the restore login is the only non-superuser login allowed on the
        # maintenance database.
        _grant_maintenance_connect(run, control)
        return Project(
            run=run,
            script=script,
            passwords=passwords,
            created_with_null_passwords=created_with_null_passwords,
        )
    except BaseException as setup_error:
        # A cleanup failure is reported with the setup failure, never instead of it.
        try:
            _deprovision_project(run)
        except BaseException as cleanup_error:
            raise setup_error from cleanup_error
        raise


def _grant_maintenance_connect(run, control) -> None:
    """Grant the restore login CONNECT on the maintenance database, registering it FIRST.

    The registration precedes the statement so an ambiguous outcome (a commit whose
    acknowledgement was lost) still gets a REVOKE attempt; revoking a grant that was never
    made is a no-op."""
    maintenance = run.target.maintenance_database
    role = "hh_restore"
    if (role, maintenance) not in _MAINTENANCE_GRANTS:
        _MAINTENANCE_GRANTS.append((role, maintenance))
    control.execute(f'GRANT CONNECT ON DATABASE "{maintenance}" TO "{role}"')


def _deprovision_project(run) -> None:
    """Drop ONLY what this process created, in the `Run.close()` order: databases,
    maintenance grants, session defaults, then roles. Names come from the registries, so a
    resource that existed before the test is never touched.

    A claimed role is not guaranteed to exist: a creation transaction that failed or
    verified false was rolled back, so the name is absent. Absent names are skipped for
    `ALTER`/`REVOKE` and dropped with IF EXISTS (a no-op); they are never adopted. A
    failed removal stays registered and raises - it is never silently recorded as
    cleaned."""
    _drop_leftover_databases(run)
    if not _CREATED_ROLE_NAMES and not _MAINTENANCE_GRANTS:
        return
    with run.control() as control:
        present = {
            row[0]
            for row in control.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (list(artifacts.PROJECT_ROLE_NAMES),),
            ).fetchall()
        }
        for role, maintenance in list(_MAINTENANCE_GRANTS):
            if role in present:  # no role, no grant: nothing to revoke
                control.execute(f'REVOKE CONNECT ON DATABASE "{maintenance}" FROM "{role}"')
        for role in artifacts.LOGIN_ROLE_NAMES:
            if role in _CREATED_ROLE_NAMES and role in present:
                control.execute(f'ALTER ROLE "{role}" RESET ALL')
        for role in reversed(
            [name for name in artifacts.PROJECT_ROLE_NAMES if name in _CREATED_ROLE_NAMES]
        ):
            control.execute(f'DROP ROLE IF EXISTS "{role}"')
    _MAINTENANCE_GRANTS.clear()
    _CREATED_ROLE_NAMES.clear()


def _drop_leftover_databases(run) -> None:
    """Drop ONLY a database this process created, and fail closed on anything else."""
    with run.control() as control:
        rows = control.execute(
            "SELECT datname, pg_get_userbyid(datdba) FROM pg_database "
            "WHERE datname = 'house_helper' OR datname ~ '^house_helper_restore_[a-z0-9]{1,32}$'"
        ).fetchall()
        for name, owner in rows:
            if name not in _CREATED_BY_THIS_MODULE:
                pytest.fail(f"database {name!r} was not created by this run; refusing to drop it")
            if owner != "hh_owner":
                pytest.fail(f"database {name!r} is not owned by hh_owner; not dropped")
            control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
            _CREATED_BY_THIS_MODULE.discard(name)


def _require_unrelated_role_absent(run, name: str) -> None:
    """Refuse a generated name that already exists, BEFORE it is claimed or mutated: a
    pre-existing role is never adopted, and no cleanup path may ever drop it."""
    with run.control() as control:
        if control.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)
        ).fetchone() is not None:
            pytest.fail(f"generated role {name!r} already exists; refusing to adopt it")


def _create_unrelated_role(run, name: str) -> None:
    """Create one uniquely named disposable NOLOGIN role through the run's verified
    privileged boundary (the only way to a cluster-wide statement)."""
    with run.control() as control:
        control.execute(f'CREATE ROLE "{name}" NOLOGIN NOINHERIT')


def _drop_unrelated_role(run, name: str) -> None:
    """Drop one disposable role by its generated name. `IF EXISTS` makes a clean rollback
    that never created it a no-op; a drop that still fails (a default ACL left behind in a
    database that has not been dropped yet) raises and leaves the name registered."""
    with run.control() as control:
        control.execute(f'DROP ROLE IF EXISTS "{name}"')
    _CREATED_UNRELATED_ROLES.discard(name)


def _drop_leftover_unrelated_roles(run) -> None:
    """Last resort at module teardown: retry any disposable role a failed per-test drop
    left registered. The function-scoped databases that could hold a blocking default ACL
    are gone by then, so the retry can succeed."""
    for name in sorted(_CREATED_UNRELATED_ROLES):
        _drop_unrelated_role(run, name)


@contextmanager
def _unrelated_default_recipient(run, recipient: str):
    """The default-ACL recipient of the negative test: `PUBLIC`, or a disposable role.

    `PUBLIC` is built in. The role case creates a UNIQUELY named NOLOGIN role on the
    verified disposable cluster - never a role that merely happens to exist there - and
    drops it after the test, whether the test passed or failed. The absence check runs
    first, then the name is claimed BEFORE the CREATE; a cleanup failure is reported with
    the test/setup failure, never instead of it.

    The caller removes the default ACL it granted to the role before this context exits:
    an existing default ACL would block DROP ROLE."""
    if recipient == "PUBLIC":
        yield recipient
        return
    name = "hh_unrelated_" + secrets.token_hex(8)
    assert UNRELATED_ROLE.fullmatch(name)
    _require_unrelated_role_absent(run, name)
    _CREATED_UNRELATED_ROLES.add(name)
    try:
        _create_unrelated_role(run, name)
        yield name
    except BaseException as test_error:
        try:
            _drop_unrelated_role(run, name)
        except BaseException as cleanup_error:
            raise test_error from cleanup_error
        raise
    else:
        _drop_unrelated_role(run, name)


@pytest.fixture(scope="module")
def project(project_run):
    """Create the seven literal roles ONCE, with test-only credentials.

    The fixture refuses to adopt pre-existing roles or databases; provisioning registers
    every resource as it is created and cleans up after its own failure before yielding."""
    with project_run.control() as control:
        project = _provision_project(project_run, control)
    try:
        yield project
    finally:
        _deprovision_project(project_run)
        _drop_leftover_unrelated_roles(project_run)


def _bootstrap(
    project: Project,
    database: str,
    grantee: str,
    *,
    script=None,
    after_harden=None,
) -> artifacts.BootstrapScript:
    script = script or artifacts.BootstrapScript()
    try:
        with project.run.control() as control:
            script.run(
                control, database=database, grantee=grantee, after_harden=after_harden
            )
    finally:
        # Track a confirmed CREATE even when a later phase failed, so the last-resort
        # teardown knows this name is ours.
        _CREATED_BY_THIS_MODULE.update(script.created)
    return script


def _serving_database(project: Project) -> SimpleNamespace:
    bootstrap = _bootstrap(project, artifacts.SERVING_DATABASE, "hh_migrate")
    try:
        with artifacts.project_connection(
            project.run, artifacts.SERVING_DATABASE, "hh_migrate", project.passwords["hh_migrate"]
        ) as connection:
            artifacts.SchemaContract().apply(connection)
        engine = artifacts.migration_engine(
            project.run, artifacts.SERVING_DATABASE, "hh_migrate", project.passwords["hh_migrate"]
        )
        try:
            upgrade_database(engine, HEAD_REVISION)
        finally:
            engine.dispose()
        with artifacts.project_connection(
            project.run, artifacts.SERVING_DATABASE, "hh_migrate", project.passwords["hh_migrate"]
        ) as connection:
            artifacts.PrivilegesScript().apply(connection, database=artifacts.SERVING_DATABASE)
        return SimpleNamespace(name=artifacts.SERVING_DATABASE, project=project, bootstrap=bootstrap)
    except BaseException:
        with project.run.control() as control:
            if artifacts.SERVING_DATABASE in bootstrap.created:
                bootstrap.drop(control, artifacts.SERVING_DATABASE)
        raise


def _restore_database(project: Project) -> SimpleNamespace:
    name = "house_helper_restore_" + secrets.token_hex(8)
    bootstrap = _bootstrap(project, name, "hh_restore")
    try:
        with artifacts.project_connection(
            project.run, name, "hh_restore", project.passwords["hh_restore"],
            expected_current_user="hh_restore",
        ) as connection:
            artifacts.assume_owner(connection)
            artifacts.SchemaContract().apply(connection)
        engine = artifacts.migration_engine(
            project.run, name, "hh_restore", project.passwords["hh_restore"]
        )
        try:
            upgrade_database(engine, HEAD_REVISION)
        finally:
            engine.dispose()
        with artifacts.project_connection(
            project.run, name, "hh_restore", project.passwords["hh_restore"],
            expected_current_user="hh_restore",
        ) as connection:
            artifacts.assume_owner(connection)
            artifacts.PrivilegesScript().apply(connection, database=name)
        return SimpleNamespace(name=name, project=project, bootstrap=bootstrap)
    except BaseException:
        with project.run.control() as control:
            if name in bootstrap.created:
                bootstrap.drop(control, name)
        raise


@pytest.fixture
def serving(project):
    database = _serving_database(project)
    try:
        yield database
    finally:
        with project.run.control() as control:
            if database.name in database.bootstrap.created:
                database.bootstrap.drop(control, database.name)


@pytest.fixture
def restore(project):
    database = _restore_database(project)
    try:
        yield database
    finally:
        with project.run.control() as control:
            if database.name in database.bootstrap.created:
                database.bootstrap.drop(control, database.name)


@pytest.fixture
def scratch_run(project_run):
    """A bare Run for locale databases; no serving database may be alive in this test."""
    return project_run


# --- helpers ----------------------------------------------------------------------------------


def _control(project: Project):
    return project.run.control()


def _snapshot(project: Project):
    with project.run.control() as control:
        return project.script.snapshot(control)


def _connect_matrix(control, name):
    return {
        role: control.execute(
            "SELECT has_database_privilege(%s, %s, 'CONNECT')", (role, name)
        ).fetchone()[0]
        for role in artifacts.PROJECT_ROLE_NAMES
    }


def _temporary(control, name, role):
    return control.execute(
        "SELECT has_database_privilege(%s, %s, 'TEMPORARY')", (role, name)
    ).fetchone()[0]


def _assert_role_matrix(project: Project):
    snapshot = _snapshot(project)
    for name in artifacts.PROJECT_ROLE_NAMES:
        matrix = artifacts.ROLE_MATRIX[name]
        expected = (
            matrix["login"], matrix["superuser"], matrix["createdb"],
            matrix["createrole"], matrix["inherit"], matrix["replication"],
            matrix["bypassrls"], -1, None, tuple(sorted(artifacts.ROLE_SETTINGS[name])),
        )
        assert snapshot["attributes"][name] == expected, name
    expected_edges = tuple(
        (member, granted, options[0], options[1], options[2])
        for (member, granted), options in sorted(artifacts.EXPECTED_EDGES.items())
    )
    assert snapshot["memberships"] == expected_edges
    assert snapshot["database_settings"] == 0


def _assert_acl_matrix(database: SimpleNamespace):
    project = database.project
    login = "hh_migrate" if database.name == artifacts.SERVING_DATABASE else "hh_restore"
    with artifacts.project_connection(
        project.run, database.name, login, project.passwords[login],
        expected_current_user="hh_restore" if login == "hh_restore" else None,
    ) as connection:
        if login == "hh_restore":
            artifacts.assume_owner(connection)
        measured = artifacts.effective_matrix(connection)
        columns = artifacts.purge_column_updates(connection)
        defaults = artifacts.default_acl_entries(connection)
        public_entries = artifacts.public_acl_entries(connection)
        grant_options = artifacts.grant_option_entries(connection)
        tables = connection.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
        ).fetchall()
        sequences = connection.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'S'"
        ).fetchall()
    table_names = {row[0] for row in tables}
    assert table_names == set(APPLICATION_TABLES) | {"alembic_version"}
    assert sequences  # a typo must not make the sequence assertions vacuous

    all_privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    for table in table_names:
        app = measured["hh_app"]["tables"][table]
        for privilege in all_privileges:
            expected = (privilege == "SELECT") if table == "alembic_version" else (
                privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            )
            assert app[privilege] is expected, ("hh_app", table, privilege)
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            assert measured["hh_backup"]["tables"][table][privilege] is (privilege == "SELECT"), (
                "hh_backup", table, privilege)
        purge = measured["hh_purge"]["tables"][table]
        if table in PURGE_TABLES:
            assert purge["SELECT"] is True and purge["DELETE"] is True
            assert all(
                purge[privilege] is False
                for privilege in ("INSERT", "UPDATE", "TRUNCATE", "REFERENCES", "TRIGGER")
            ), ("hh_purge", table)
        elif table == "alembic_version":
            assert purge["SELECT"] is True
            assert all(
                purge[privilege] is False
                for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
            )
        else:
            assert all(purge[privilege] is False for privilege in all_privileges), table
        provision = measured["hh_provision"]["tables"][table]
        if table in PROVISION_TABLES:
            assert (provision["SELECT"], provision["INSERT"], provision["UPDATE"], provision["DELETE"]) == (
                True, True, True, False)
        elif table in ("identity_links", "alembic_version"):
            assert provision["SELECT"] is True and provision["INSERT"] is False
            assert provision["UPDATE"] is False and provision["DELETE"] is False
        else:
            assert all(provision[privilege] is False for privilege in all_privileges), table

    for sequence in measured["hh_app"]["sequences"]:
        assert measured["hh_app"]["sequences"][sequence] == {"SELECT": False, "USAGE": True, "UPDATE": False}
        assert measured["hh_backup"]["sequences"][sequence] == {"SELECT": True, "USAGE": False, "UPDATE": False}
        for role in ("hh_purge", "hh_provision"):
            assert measured[role]["sequences"][sequence] == {"SELECT": False, "USAGE": False, "UPDATE": False}

    for table, allowed_columns in PURGE_COLUMN_UPDATES.items():
        for column, allowed in columns[table].items():
            assert allowed is (column in allowed_columns), (table, column)
    for table, columns_of_table in columns.items():
        if table not in PURGE_COLUMN_UPDATES:
            assert all(value is False for value in columns_of_table.values()), table

    # The complete default ACL set: GLOBAL and schema-specific entries together. The
    # owner's own entries are PostgreSQL's, not the contract's; every other entry must be
    # exactly one of the seven frozen grants, without a grant option.
    project_defaults = {entry for entry in defaults if entry[2] != "hh_owner"}
    assert project_defaults == set(EXPECTED_DEFAULTS)
    assert all(entry[4] is False for entry in project_defaults)
    # No PUBLIC table/column/sequence entry and no grant option for any non-owner.
    assert public_entries == ()
    assert grant_options == ()


def _app_accepts(provider, subject) -> bool:
    try:
        return normalize_identity(provider, subject) == (provider, subject)
    except ValueError:
        return False


def _accepted_by_database(session, write) -> bool:
    """The Stage 1 identity corpus helper, extended for PostgreSQL's VARCHAR width.

    SQLite's width is advisory and its CHECK rejects the value; PostgreSQL raises
    `StringDataRightTruncation` (a DataError) for the one-too-long value before any CHECK.
    Both are the database's rejection, which is the verdict this test compares."""
    savepoint = session.begin_nested()
    try:
        write()
    except (sa.exc.IntegrityError, sa.exc.DataError):
        savepoint.rollback()
        return False
    savepoint.rollback()
    return True


def _identity_probe_table(connection):
    connection.execute("DROP TABLE IF EXISTS public.wp1b_identity_probe")
    connection.execute(
        f"""
        CREATE TABLE public.wp1b_identity_probe (
            provider text NOT NULL,
            subject text NOT NULL,
            CONSTRAINT provider_valid CHECK (provider IN ('telegram', 'email')),
            CONSTRAINT subject_length CHECK (length(subject) BETWEEN 1 AND {MAX_SUBJECT_LENGTH}),
            CONSTRAINT subject_printable_ascii CHECK (subject ~ '^[!-~]+$'),
            CONSTRAINT email_subject_normalized CHECK (
                provider <> 'email' OR (
                    substr(subject, strpos(subject, '@')) =
                        lower(substr(subject, strpos(subject, '@')))
                    AND length(subject) - length(replace(subject, '@', '')) = 1
                    AND substr(subject, 1, 1) <> '@'
                    AND substr(subject, length(subject), 1) <> '@'
                )
            ),
            CONSTRAINT telegram_subject_not_email CHECK (
                provider <> 'telegram' OR replace(subject, '@', '') = subject
            )
        )
        """
    )


def _probe_verdicts(connection) -> dict:
    """Insert every corpus value into a real table and report the database's verdict."""
    verdicts = {}
    with connection.transaction():
        _identity_probe_table(connection)
        for label, (provider, subject) in SUBJECTS.items():
            connection.execute("SAVEPOINT s")
            try:
                connection.execute(
                    "INSERT INTO public.wp1b_identity_probe (provider, subject) VALUES (%s, %s)",
                    (provider, subject),
                )
                verdicts[label] = True
            except psycopg.errors.IntegrityError:
                verdicts[label] = False
            finally:
                connection.execute("ROLLBACK TO SAVEPOINT s")
        connection.execute("DROP TABLE public.wp1b_identity_probe")
    return verdicts


# --- A. roles.sql -----------------------------------------------------------------------------


def test_the_complete_role_matrix_is_the_frozen_matrix(project):
    """All seven roles, every attribute, the complete graph and the exact ROL-17 defaults."""
    _assert_role_matrix(project)
    snapshot = _snapshot(project)
    assert snapshot["attributes"]["hh_owner"][0] is False  # NOLOGIN
    assert snapshot["attributes"]["hh_restore"][2] is True  # CREATEDB
    assert all(snapshot["attributes"][name][4] is False for name in artifacts.PROJECT_ROLE_NAMES)


def test_new_logins_are_created_without_credentials(project):
    assert project.created_with_null_passwords is True


def test_roles_sql_is_idempotent_and_preserves_credentials(project):
    before = _snapshot(project)
    with project.run.control() as control:
        verifiers = {
            login: artifacts.password_verifier(control, login)
            for login in artifacts.LOGIN_ROLE_NAMES
        }
        assert all(value is not None and value.startswith("SCRAM-SHA-256$") for value in verifiers.values())
        result = project.script.apply(control)
        after = {
            login: artifacts.password_verifier(control, login)
            for login in artifacts.LOGIN_ROLE_NAMES
        }
    assert result.created == frozenset()
    assert result.existing == frozenset(artifacts.PROJECT_ROLE_NAMES)
    assert after == verifiers  # re-execution neither resets nor rewrites a credential
    assert _snapshot(project) == before


def test_roles_sql_refuses_attribute_drift(project):
    with project.run.control() as control:
        control.execute('ALTER ROLE "hh_app" SUPERUSER')
        try:
            with pytest.raises(artifacts.ArtifactError) as refused:
                project.script.apply(control)
        finally:
            control.execute('ALTER ROLE "hh_app" NOSUPERUSER')
        assert refused.value.code == "role_attributes"
        project.script.apply(control)  # the restored state is accepted and converged
    _assert_role_matrix(project)


def test_roles_sql_refuses_an_unauthorized_membership_edge(project):
    with project.run.control() as control:
        control.execute('GRANT "hh_owner" TO "hh_app" WITH SET TRUE')
        try:
            with pytest.raises(artifacts.ArtifactError) as refused:
                project.script.apply(control)
        finally:
            control.execute('REVOKE "hh_owner" FROM "hh_app"')
        assert refused.value.code == "role_membership"
        project.script.apply(control)
    _assert_role_matrix(project)


def test_roles_sql_refuses_membership_option_drift(project):
    with project.run.control() as control:
        control.execute(
            'GRANT "hh_owner" TO "hh_migrate" WITH ADMIN FALSE, INHERIT TRUE, SET TRUE'
        )
        try:
            with pytest.raises(artifacts.ArtifactError) as refused:
                project.script.apply(control)
        finally:
            control.execute(
                'GRANT "hh_owner" TO "hh_migrate" WITH ADMIN FALSE, INHERIT FALSE, SET TRUE'
            )
        assert refused.value.code == "role_membership"
        project.script.apply(control)
    _assert_role_matrix(project)


def test_roles_sql_refuses_a_database_scoped_setting(project):
    maintenance = project.run.target.maintenance_database
    with project.run.control() as control:
        control.execute(
            f'ALTER ROLE "hh_app" IN DATABASE "{maintenance}" SET search_path = \'public\''
        )
        try:
            with pytest.raises(artifacts.ArtifactError) as refused:
                project.script.apply(control)
        finally:
            control.execute(
                f'ALTER ROLE "hh_app" IN DATABASE "{maintenance}" RESET search_path'
            )
        assert refused.value.code == "role_settings"
        project.script.apply(control)
    _assert_role_matrix(project)


def test_roles_sql_requires_autocommit(project):
    connection = psycopg.connect(
        autocommit=False, **project.run.target.connect_params(
            project.run.target.maintenance_database
        )
    )
    try:
        with pytest.raises(artifacts.ArtifactError) as refused:
            project.script.apply(connection)
        assert refused.value.code == "not_autocommit"
    finally:
        connection.close()


def test_the_artifacts_do_not_manage_transactions_themselves():
    for filename in ("roles.sql", "privileges.sql"):
        text = (artifacts.SQL_DIR / filename).read_text(encoding="utf-8")
        without_comments = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("--")
        )
        assert not re.search(
            r"(?im)^\s*(BEGIN|COMMIT|ROLLBACK)\s*;\s*$", without_comments
        ), filename


# --- A2. cluster-wide parameter privileges (R1-03) -------------------------------------------


def test_roles_sql_refuses_an_explicit_parameter_privilege(project):
    """`pg_parameter_acl` is cluster-wide. A row naming a project role or PUBLIC is an
    explicit elevation, and BOTH layers refuse it: the runner's preflight and the file's own
    DO block. The grant is transactional, so the test never leaves one behind and never
    actually changes `session_replication_role`."""
    for target in ("hh_app", "PUBLIC"):
        clause = "PUBLIC" if target == "PUBLIC" else f'"{target}"'
        with project.run.control() as control:
            control.execute("BEGIN")
            try:
                control.execute(
                    f"GRANT SET ON PARAMETER session_replication_role TO {clause}"
                )
                assert artifacts.parameter_acl_entries(control), target
                with pytest.raises(artifacts.ArtifactError) as refused:
                    project.script.preflight(control)
                assert refused.value.code == "role_parameters"
                # The artifact itself is the authoritative second layer.
                with pytest.raises(psycopg.errors.RaiseException):
                    control.execute(project.script.sql)
            finally:
                control.execute("ROLLBACK")
    with project.run.control() as control:
        assert artifacts.parameter_acl_entries(control) == ()
        assert control.execute("SHOW session_replication_role").fetchone()[0] == "origin"


def test_ordinary_parameter_access_is_not_an_explicit_grant(serving):
    """An ordinary user-context parameter needs no grant and has no ACL row; a
    superuser-only parameter is refused without one. Only the explicit row is drift."""
    project = serving.project
    with project.run.control() as control:
        control.execute(f'GRANT CONNECT ON DATABASE "{serving.name}" TO "hh_app"')
    engine = None
    try:
        engine = artifacts.role_engine(
            project.run, serving.name, "hh_app", project.passwords["hh_app"]
        )
        with engine.connect() as connection:
            connection.execute(sa.text("SET work_mem = '8MB'"))  # ordinary, no row exists
            with pytest.raises(sa.exc.ProgrammingError) as refused:
                connection.execute(sa.text("SET session_replication_role = 'origin'"))
            assert refused.value.orig.sqlstate == "42501"
            connection.rollback()
        with project.run.control() as control:
            assert artifacts.parameter_acl_entries(control) == ()
    finally:
        if engine is not None:
            engine.dispose()
        with project.run.control() as control:
            control.execute(f'REVOKE CONNECT ON DATABASE "{serving.name}" FROM "hh_app"')


# --- B. bootstrap.sql -------------------------------------------------------------------------


def test_bootstrap_reaches_e3_a_e3_b_and_e3_c(project):
    name = "house_helper_restore_" + secrets.token_hex(8)
    bootstrap = artifacts.BootstrapScript()
    observed = []
    original_a = bootstrap.verify_e3_a
    original_b = bootstrap.verify_disabled

    def record(control, database):
        observed.append(
            {
                "facts": bootstrap.database_facts(control, database),
                "public": bootstrap.public_entries(control, database),
                "connect": bootstrap.connect_matrix(control, database),
            }
        )

    def spy_a(control, database):
        record(control, database)
        return original_a(control, database)

    def spy_b(control, database, grantee):
        record(control, database)
        return original_b(control, database, grantee)

    bootstrap.verify_e3_a = spy_a  # type: ignore[assignment]
    bootstrap.verify_disabled = spy_b  # type: ignore[assignment]
    try:
        _bootstrap(project, name, "hh_restore", script=bootstrap)
        with project.run.control() as control:
            facts = bootstrap.database_facts(control, name)
            assert (facts["allowconn"], facts["owner"], facts["encoding"]) == (True, "hh_owner", "UTF8")
            assert (facts["collate"], facts["ctype"], facts["provider"]) == ("C", "C", "c")
            assert bootstrap.public_entries(control, name) == 0
            matrix = bootstrap.connect_matrix(control, name)
            assert matrix["hh_restore"] is True and matrix["hh_owner"] is True
            assert all(matrix[role] is False for role in (
                "hh_migrate", "hh_app", "hh_purge", "hh_backup", "hh_provision"))
            assert _temporary(control, name, "hh_restore") is False
            # Re-running against an existing database is refused, never adopted.
            with pytest.raises(artifacts.ArtifactError) as refused:
                bootstrap.run(control, database=name, grantee="hh_restore")
            assert refused.value.code == "database_exists"
    finally:
        with project.run.control() as control:
            if name in bootstrap.created:
                bootstrap.drop(control, name)

    assert observed[0]["facts"]["allowconn"] is False  # E3-a: still disabled
    assert observed[1]["facts"]["allowconn"] is False  # E3-b: still disabled
    assert observed[1]["public"] == 0
    assert observed[1]["connect"]["hh_restore"] is True
    assert observed[1]["connect"]["hh_migrate"] is False


def test_bootstrap_never_enables_connections_after_a_failed_e3_b(project):
    name = "house_helper_restore_" + secrets.token_hex(8)
    bootstrap = artifacts.BootstrapScript()

    def sabotage(control, database):
        control.execute(f'GRANT CONNECT ON DATABASE "{database}" TO "hh_app"')

    with pytest.raises(artifacts.ArtifactError) as refused:
        _bootstrap(
            project, name, "hh_restore", script=bootstrap, after_harden=sabotage
        )
    assert refused.value.code == "e3_b"
    with project.run.control() as control:
        facts = bootstrap.database_facts(control, name)
        assert facts["allowconn"] is False  # the failed gate never reached E3-c
        assert bootstrap.connect_matrix(control, name)["hh_app"] is True
        bootstrap.drop(control, name)
        assert control.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone() is None


def test_create_database_cannot_run_in_a_transaction(project):
    name = "house_helper_restore_" + secrets.token_hex(8)
    connection = psycopg.connect(
        autocommit=False,
        **project.run.target.connect_params(project.run.target.maintenance_database),
    )
    try:
        with pytest.raises(psycopg.errors.ActiveSqlTransaction) as refused:
            connection.execute(
                f'CREATE DATABASE "{name}" OWNER "hh_owner" TEMPLATE template0 '
                f"ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' ALLOW_CONNECTIONS false"
            )
        assert refused.value.sqlstate == "25001"
        connection.rollback()
    finally:
        connection.close()
    with project.run.control() as control:
        assert control.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone() is None
    bad_connection = psycopg.connect(
        autocommit=False,
        **project.run.target.connect_params(project.run.target.maintenance_database),
    )
    try:
        with pytest.raises(artifacts.ArtifactError) as refused:
            artifacts.BootstrapScript().run(
                bad_connection, database=name, grantee="hh_restore"
            )
        assert refused.value.code == "not_autocommit"
    finally:
        bad_connection.close()


def test_bootstrap_verifies_ownership_locale_and_connect_privileges(serving):
    with serving.project.run.control() as control:
        facts = artifacts.BootstrapScript().database_facts(control, serving.name)
        assert (facts["owner"], facts["encoding"]) == ("hh_owner", "UTF8")
        assert (facts["collate"], facts["ctype"], facts["provider"]) == ("C", "C", "c")
        matrix = _connect_matrix(control, serving.name)
        assert matrix["hh_migrate"] is True and matrix["hh_owner"] is True
        assert all(matrix[role] is False for role in (
            "hh_app", "hh_purge", "hh_backup", "hh_provision", "hh_restore"))
        assert _temporary(control, serving.name, "hh_migrate") is False


# --- C. privileges.sql -----------------------------------------------------------------------


def test_privileges_sql_is_idempotent(serving):
    project = serving.project
    with artifacts.project_connection(
        project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
    ) as connection:
        before = artifacts.acl_dump(connection)
        artifacts.PrivilegesScript().apply(connection, database=serving.name)
        after = artifacts.acl_dump(connection)
        artifacts.PrivilegesScript().apply(connection, database=serving.name)
        assert artifacts.acl_dump(connection) == after
    assert before == after


def test_the_privilege_matrix_is_exact(serving):
    _assert_acl_matrix(serving)


def test_the_serving_and_restore_databases_have_the_same_acl_contract(serving, restore):
    project = serving.project
    with artifacts.project_connection(
        project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
    ) as connection:
        serving_dump = artifacts.acl_dump(connection)
        serving_matrix = artifacts.effective_matrix(connection)
    with artifacts.project_connection(
        project.run, restore.name, "hh_restore", project.passwords["hh_restore"],
        expected_current_user="hh_restore",
    ) as connection:
        artifacts.assume_owner(connection)
        restore_dump = artifacts.acl_dump(connection)
        restore_matrix = artifacts.effective_matrix(connection)
    assert serving_dump == restore_dump
    assert serving_matrix == restore_matrix
    _assert_acl_matrix(restore)


def test_global_and_grant_option_default_drift_is_converged(serving):
    """R1-01: a GLOBAL default grant and a default grant option are both removed by a
    re-invocation. A schema-level REVOKE alone could not cancel the global grant.

    `pg_default_acl` is per-database, so the drift is injected in the SERVING database,
    where `hh_owner` owns the default ACLs; the fixture drops that database afterwards."""
    project = serving.project
    with artifacts.project_connection(
        project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
    ) as connection:
        connection.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner GRANT TRUNCATE ON TABLES TO hh_app")
        connection.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner GRANT UPDATE ON SEQUENCES TO hh_backup")
        connection.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public "
            "GRANT SELECT ON TABLES TO hh_backup WITH GRANT OPTION")
        injected = artifacts.default_acl_entries(connection)
        assert ("<global>", "r", "hh_app", "TRUNCATE", False) in injected
        assert ("<global>", "S", "hh_backup", "UPDATE", False) in injected
        assert ("public", "r", "hh_backup", "SELECT", True) in injected
        artifacts.PrivilegesScript().apply(connection, database=serving.name)
        converged = {
            entry
            for entry in artifacts.default_acl_entries(connection)
            if entry[2] != "hh_owner"
        }
    assert converged == set(EXPECTED_DEFAULTS)
    _assert_acl_matrix(serving)


@pytest.mark.parametrize(
    "recipient", ["PUBLIC", "unrelated"], ids=["PUBLIC", "unrelated-role"]
)
def test_an_unauthorized_default_privilege_is_rejected(serving, recipient):
    """R1-01: a default grant to PUBLIC or to an unrelated role cannot be revoked without
    naming it, so a re-invocation refuses instead of silently accepting it; nothing changes.

    The unrelated recipient is a disposable NOLOGIN role this test creates on the verified
    test cluster under a unique name, so the test does not depend on any role (such as the
    bootstrap user) happening to exist there, and it leaves no role behind."""
    project = serving.project
    with _unrelated_default_recipient(project.run, recipient) as grantee:
        with artifacts.project_connection(
            project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
        ) as connection:
            connection.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner GRANT SELECT ON TABLES TO {grantee}")
            try:
                before = artifacts.acl_dump(connection)
                with pytest.raises(artifacts.ArtifactError) as refused:
                    artifacts.PrivilegesScript().apply(connection, database=serving.name)
                assert refused.value.code == "privilege_contract"
                assert artifacts.acl_dump(connection) == before  # nothing was accepted
            finally:
                connection.execute(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner REVOKE ALL ON TABLES FROM {grantee}")
            artifacts.PrivilegesScript().apply(connection, database=serving.name)  # clean state accepted
    _assert_acl_matrix(serving)


def test_a_future_table_and_sequence_get_the_frozen_privileges(serving):
    """R1-01 evidence: after a successful application, a NEW table and a NEW sequence
    created by the owner are inspected directly. The transaction is rolled back, so the
    probe objects leave nothing behind."""
    project = serving.project
    table_privileges = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
    with artifacts.project_connection(
        project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
    ) as connection:
        connection.execute("BEGIN")  # autocommit connection: explicit block
        try:
            connection.execute(
                "CREATE TABLE public.wp1b_future_probe (id integer, content text)")
            connection.execute("CREATE SEQUENCE public.wp1b_future_seq")
            assert connection.execute(
                "SELECT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE oid = 'public.wp1b_future_probe'::regclass"
            ).fetchone()[0] == "hh_owner"
            table_expected = {
                "hh_app": {"SELECT", "INSERT", "UPDATE", "DELETE"},
                "hh_backup": {"SELECT"},
            }
            for role in ("hh_app", "hh_backup", "hh_purge", "hh_provision", "public"):
                expected = table_expected.get(role, set())
                for privilege in table_privileges:
                    assert connection.execute(
                        "SELECT has_table_privilege(%s, 'public.wp1b_future_probe', %s)",
                        (role, privilege),
                    ).fetchone()[0] is (privilege in expected), (role, privilege)
            sequence_expected = {"hh_app": {"USAGE"}, "hh_backup": {"SELECT"}}
            for role in ("hh_app", "hh_backup", "hh_purge", "hh_provision", "public"):
                expected = sequence_expected.get(role, set())
                for privilege in ("USAGE", "SELECT", "UPDATE"):
                    assert connection.execute(
                        "SELECT has_sequence_privilege(%s, 'public.wp1b_future_seq', %s)",
                        (role, privilege),
                    ).fetchone()[0] is (privilege in expected), (role, privilege)
            # A column-level grant would evade has_table_privilege; ask the column question.
            for role in ("hh_provision", "public"):
                assert connection.execute(
                    "SELECT has_column_privilege(%s, 'public.wp1b_future_probe', 'content', 'SELECT')",
                    (role,),
                ).fetchone()[0] is False
        finally:
            connection.execute("ROLLBACK")


def test_a_public_column_grant_is_rejected_not_silently_accepted(serving):
    """R1-02: `GRANT SELECT (content) ON public.messages TO PUBLIC` is invisible to
    `has_table_privilege` but visible to `has_column_privilege`; a re-invocation must refuse.
    The grant is made in the serving database as its owner, so it dies with the fixture."""
    project = serving.project
    with artifacts.project_connection(
        project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
    ) as connection:
        connection.execute("GRANT SELECT (content) ON public.messages TO PUBLIC")
        try:
            # The gap, stated explicitly.
            assert connection.execute(
                "SELECT has_table_privilege('hh_provision', 'public.messages', 'SELECT')"
            ).fetchone()[0] is False
            assert connection.execute(
                "SELECT has_column_privilege('hh_provision', 'public.messages', 'content', 'SELECT')"
            ).fetchone()[0] is True
            assert artifacts.public_acl_entries(connection) != ()
            before = artifacts.acl_dump(connection)
            with pytest.raises(artifacts.ArtifactError) as refused:
                artifacts.PrivilegesScript().apply(connection, database=serving.name)
            assert refused.value.code == "privilege_contract"
            assert artifacts.acl_dump(connection) == before  # nothing was accepted
        finally:
            connection.execute("REVOKE SELECT (content) ON public.messages FROM PUBLIC")
        artifacts.PrivilegesScript().apply(connection, database=serving.name)  # clean state accepted
    _assert_acl_matrix(serving)


def test_provision_cannot_read_messages_content_even_through_public(serving):
    """R1-02: with its own (test-duration) CONNECT handoff, `hh_provision` cannot read
    `messages.content`; a PUBLIC column grant is the one path that would hand it over, and
    the contract refuses that state so it is never accepted."""
    project = serving.project
    with project.run.control() as control:
        control.execute(f'GRANT CONNECT ON DATABASE "{serving.name}" TO "hh_provision"')
    engine = None
    try:
        engine = artifacts.role_engine(
            project.run, serving.name, "hh_provision", project.passwords["hh_provision"]
        )

        def cannot_read():
            with engine.connect() as connection:
                with pytest.raises(sa.exc.ProgrammingError) as refused:
                    connection.execute(sa.text("SELECT content FROM public.messages LIMIT 0"))
                assert refused.value.orig.sqlstate == "42501"
                connection.rollback()

        cannot_read()
        with artifacts.project_connection(
            project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
        ) as owner:
            owner.execute("GRANT SELECT (content) ON public.messages TO PUBLIC")
            try:
                with engine.connect() as connection:  # PUBLIC is the path that leaks it
                    assert connection.execute(
                        sa.text("SELECT content FROM public.messages LIMIT 0")
                    ).fetchall() == []
                    connection.rollback()
                with pytest.raises(artifacts.ArtifactError) as refused:
                    artifacts.PrivilegesScript().apply(owner, database=serving.name)
                assert refused.value.code == "privilege_contract"
            finally:
                owner.execute("REVOKE SELECT (content) ON public.messages FROM PUBLIC")
        cannot_read()  # withdrawn: the frozen state refuses again
    finally:
        if engine is not None:
            engine.dispose()
        with project.run.control() as control:
            control.execute(f'REVOKE CONNECT ON DATABASE "{serving.name}" FROM "hh_provision"')
    _assert_acl_matrix(serving)


def test_privileges_sql_refuses_wrong_phase_identities(serving):
    project = serving.project
    with artifacts.project_connection(
        project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
    ) as owner:
        before = artifacts.acl_dump(owner)
    # A session user that is not one of the two phase logins is refused (the bootstrap
    # superuser can reach the database, but it is not a phase identity).
    stranger = psycopg.connect(
        autocommit=True, **project.run.target.connect_params(serving.name)
    )
    try:
        with pytest.raises(artifacts.ArtifactError) as refused:
            artifacts.PrivilegesScript().apply(stranger, database=serving.name)
        assert refused.value.code == "wrong_phase_identity"
    finally:
        stranger.close()
    with artifacts.project_connection(
        project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
    ) as migrate:
        migrate.execute('SET ROLE "hh_migrate"')
        try:
            with pytest.raises(artifacts.ArtifactError) as refused:
                artifacts.PrivilegesScript().apply(migrate, database=serving.name)
            assert refused.value.code == "wrong_phase_identity"
        finally:
            migrate.execute('SET ROLE "hh_owner"')
        after = artifacts.acl_dump(migrate)
    assert before == after


def test_privileges_sql_refuses_the_wrong_database(serving, restore):
    project = serving.project
    with artifacts.project_connection(
        project.run, restore.name, "hh_restore", project.passwords["hh_restore"],
        expected_current_user="hh_restore",
    ) as connection:
        artifacts.assume_owner(connection)
        before = artifacts.acl_dump(connection)
        with pytest.raises(artifacts.ArtifactError) as refused:
            artifacts.PrivilegesScript().apply(connection, database=serving.name)
        assert refused.value.code == "wrong_database"
        assert artifacts.acl_dump(connection) == before


def test_privileges_sql_requires_alembic_version(project):
    name = "house_helper_restore_" + secrets.token_hex(8)
    bootstrap = artifacts.BootstrapScript()
    try:
        _bootstrap(project, name, "hh_restore", script=bootstrap)
        with artifacts.project_connection(
            project.run, name, "hh_restore", project.passwords["hh_restore"],
            expected_current_user="hh_restore",
        ) as connection:
            artifacts.assume_owner(connection)
            with pytest.raises(artifacts.ArtifactError) as refused:
                artifacts.PrivilegesScript().apply(connection, database=name)
            assert refused.value.code == "schema_not_initialized"
    finally:
        with project.run.control() as control:
            if name in bootstrap.created:
                bootstrap.drop(control, name)


def test_an_application_grant_is_a_warning_that_changes_no_acl(serving):
    """§3.6 negative test 1 on the WP-1B database, with the WP-1A erratum's effect.

    PostgreSQL 16 answers a GRANT from a role without the grant option with WARNING
    SQLSTATE 01007 and grants nothing. The complete ACL snapshot (table, columns, default
    ACLs and every identity's effective privileges) is identical before and after."""
    project = serving.project
    # H1 is out of WP-1B scope: the app login has no CONNECT on the serving database yet.
    # This test simulates that handoff for its duration and withdraws it afterwards.
    with project.run.control() as control:
        control.execute(f'GRANT CONNECT ON DATABASE "{serving.name}" TO "hh_app"')
    engine = None
    try:
        engine = artifacts.role_engine(
            project.run, serving.name, "hh_app", project.passwords["hh_app"]
        )
        with engine.connect() as connection:
            transaction = connection.begin()
            notices, record = contract.notice_recorder()
            driver = connection.connection.driver_connection
            driver.add_notice_handler(record)
            try:
                before = contract.acl_snapshot(
                    connection, table="users", identities=list(artifacts.PROJECT_ROLE_NAMES)
                )
                grant_index = len(notices)
                connection.exec_driver_sql("GRANT SELECT ON users TO public")
                attempted = tuple(notices[grant_index:])
                after = contract.acl_snapshot(
                    connection, table="users", identities=list(artifacts.PROJECT_ROLE_NAMES)
                )
            finally:
                driver.remove_notice_handler(record)
                transaction.rollback()
    finally:
        if engine is not None:
            engine.dispose()
        with project.run.control() as control:
            control.execute(f'REVOKE CONNECT ON DATABASE "{serving.name}" FROM "hh_app"')
    assert before == after
    assert contract.unauthorized_privileges(before) == []
    assert contract.grant_notice_violations(attempted, table="users") == []
    with artifacts.project_connection(
        project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
    ) as connection:
        assert connection.execute(
            "SELECT has_table_privilege('public', 'users', 'SELECT')"
        ).fetchone()[0] is False


# --- D. identifier validation -----------------------------------------------------------------


BAD_DATABASES = (
    "",
    "house_helper ",
    " house_helper",
    "House_helper",
    "house_helper_restore_",
    "house_helper_restore_ABC",
    "house_helper_restore_abc-123",
    "house_helper_restore_abc.123",
    "house_helper_restore_abc;drop",
    'house_helper_restore_abc"',
    "house_helper_restore_abc'",
    "house_helper_restore_ä",
    "house_helper_restore_" + "a" * 33,
    "house_helper_restore_abc\n",
    "house_helper_restore_abc\t",
    "postgres",
    "template1",
)


@pytest.mark.parametrize("name", BAD_DATABASES, ids=repr)
def test_adversarial_database_names_are_refused_before_render(name):
    with pytest.raises(artifacts.ArtifactError) as refused:
        artifacts.validate_database_name(name)
    assert refused.value.code == "database_name"
    with pytest.raises(artifacts.ArtifactError):
        artifacts.render_bootstrap(name, "hh_migrate")
    with pytest.raises(artifacts.ArtifactError):
        artifacts.render_privileges(name)


def test_the_allowed_database_shapes_are_exact():
    assert artifacts.validate_database_name("house_helper") == "house_helper"
    assert artifacts.validate_database_name("house_helper_restore_a") == "house_helper_restore_a"
    longest = "house_helper_restore_" + "a" * 32
    assert artifacts.validate_database_name(longest) == longest
    rendered = artifacts.render_bootstrap(longest, "hh_restore")
    assert f'CREATE DATABASE "{longest}"' in rendered["create"]
    assert 'TO "hh_restore"' in rendered["harden"]
    assert "{{" not in rendered["create"] + rendered["harden"] + rendered["enable"]
    assert "'house_helper'" in artifacts.render_privileges("house_helper")


def test_a_database_grantee_mismatch_is_refused_before_rendering_or_sql():
    """R1-04: the frozen mapping is exact in BOTH directions and is checked before any SQL
    is rendered, any connection is used or anything is mutated."""
    restore = "house_helper_restore_" + "a" * 8
    assert artifacts.validate_database_grantee("house_helper", "hh_migrate") == (
        "house_helper", "hh_migrate")
    assert artifacts.validate_database_grantee(restore, "hh_restore") == (restore, "hh_restore")
    for database, grantee in (
        ("house_helper", "hh_restore"),
        (restore, "hh_migrate"),
    ):
        with pytest.raises(artifacts.ArtifactError) as refused:
            artifacts.validate_database_grantee(database, grantee)
        assert refused.value.code == "grantee_database"
        with pytest.raises(artifacts.ArtifactError) as refused:
            artifacts.render_bootstrap(database, grantee)
        assert refused.value.code == "grantee_database"
        with pytest.raises(artifacts.ArtifactError) as refused:
            artifacts.BootstrapScript().run(
                _RejectingConnection(), database=database, grantee=grantee)
        assert refused.value.code == "grantee_database"


def test_maintenance_databases_grantees_and_roles_are_allowlisted():
    assert artifacts.validate_maintenance_database("postgres") == "postgres"
    assert artifacts.validate_maintenance_database("house_helper_admin") == "house_helper_admin"
    for bad in ("template1", "house_helper", "postgres;drop", "postgres "):
        with pytest.raises(artifacts.ArtifactError):
            artifacts.validate_maintenance_database(bad)
    assert artifacts.validate_grantee("hh_migrate") == "hh_migrate"
    assert artifacts.validate_grantee("hh_restore") == "hh_restore"
    for bad in ("hh_app", "hh_owner", "public", "HH_MIGRATE", "hh_migrate;drop"):
        with pytest.raises(artifacts.ArtifactError):
            artifacts.validate_grantee(bad)
    assert artifacts.validate_role_name("hh_owner") == "hh_owner"
    for bad in ("postgres", "pg_read_all_data", "hh_owner;drop", "owner"):
        with pytest.raises(artifacts.ArtifactError):
            artifacts.validate_role_name(bad)


class _RejectingConnection:
    autocommit = True

    def execute(self, *args, **kwargs):
        raise AssertionError("SQL was executed before the input was validated")


def test_invalid_input_never_reaches_a_connection():
    rejecting = _RejectingConnection()
    with pytest.raises(artifacts.ArtifactError):
        artifacts.BootstrapScript().run(rejecting, database="house_helper;drop", grantee="hh_migrate")
    with pytest.raises(artifacts.ArtifactError):
        artifacts.BootstrapScript().run(rejecting, database="house_helper", grantee="hh_app")
    # The reverse database/grantee combinations are refused before any execute as well.
    with pytest.raises(artifacts.ArtifactError):
        artifacts.BootstrapScript().run(rejecting, database="house_helper", grantee="hh_restore")
    with pytest.raises(artifacts.ArtifactError):
        artifacts.BootstrapScript().run(
            rejecting, database="house_helper_restore_abc", grantee="hh_migrate"
        )
    with pytest.raises(artifacts.ArtifactError):
        artifacts.PrivilegesScript().apply(rejecting, database="house_helper;drop")


# --- E. locale conformance -------------------------------------------------------------------


def test_the_serving_database_is_c_locale_and_stores_utf8(serving):
    project = serving.project
    with artifacts.project_connection(
        project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
    ) as connection:
        evidence = artifacts.assert_c_locale(connection)
    assert evidence["facts"] == {"encoding": "UTF8", "collate": "C", "ctype": "C", "provider": "c"}
    assert evidence["probes"]["Ä"]["lower"] == "Ä"
    assert evidence["probes"]["K"]["lower"] == "K"
    assert evidence["probes"]["Борщ"] == {
        "lower": "Борщ", "octets": 8,
    }


def test_a_scratch_database_keeps_the_cluster_default_unless_a_locale_is_requested(scratch_run, project):
    with scratch_run.control() as control:
        cluster_default = control.execute(
            "SELECT datcollate, datctype FROM pg_database WHERE datname = 'template1'"
        ).fetchone()
    default_name = scratch_run.create_database()
    c_name = scratch_run.create_database(locale="C")
    try:
        with scratch_run.control() as control:
            default = control.execute(
                "SELECT datcollate, datctype FROM pg_database WHERE datname = %s", (default_name,)
            ).fetchone()
            c_locale = control.execute(
                "SELECT datcollate, datctype, pg_encoding_to_char(encoding), datlocprovider "
                "FROM pg_database WHERE datname = %s", (c_name,)
            ).fetchone()
        assert tuple(default) == tuple(cluster_default)  # Stage 1 behavior unchanged
        assert tuple(c_locale) == ("C", "C", "UTF8", "c")
    finally:
        scratch_run.drop_database(c_name)
        scratch_run.drop_database(default_name)
    with pytest.raises(ValueError):
        scratch_run.create_database(locale="bogus")


def test_the_c_gate_refuses_en_us_utf8_even_though_the_corpus_cannot(scratch_run, project):
    """WP-1A's test-design finding, proved: the identity corpus verdicts are identical in
    en_US.utf8, so only a direct lower() probe can reject the inherited locale."""
    name = scratch_run.create_database(locale="en_US.utf8")
    try:
        connection = psycopg.connect(
            autocommit=True, **scratch_run.target.connect_params(name)
        )
        try:
            with pytest.raises(artifacts.ArtifactError) as refused:
                artifacts.assert_c_locale(connection)
            assert refused.value.code == "locale_not_c"
            probes = artifacts.locale_probes(connection)
            assert probes["Ä"]["lower"] == "ä"       # Ä -> ä
            assert probes["K"]["lower"] == "k"             # Kelvin -> k
            assert probes["А"]["lower"] == "а"        # Cyrillic А -> а
            assert probes["Борщ"]["octets"] == 8
            verdicts = _probe_verdicts(connection)
        finally:
            connection.close()
    finally:
        scratch_run.drop_database(name)

    assert verdicts
    assert {label: value for label, value in verdicts.items()} == {
        label: _app_accepts(*pair) for label, pair in SUBJECTS.items()
    }
    assert set(verdicts.values()) == {True, False}


def test_the_complete_existing_identity_corpus_on_the_real_table(serving):
    project = serving.project
    engine = artifacts.role_engine(
        project.run, serving.name, "hh_migrate", project.passwords["hh_migrate"]
    )
    seen = {}
    try:
        with create_session_factory(engine)() as session, session.begin():
            user = new_user(session, display_name="WP-1B corpus")
            for label, (provider, subject) in SUBJECTS.items():
                accepted = _accepted_by_database(
                    session,
                    lambda provider=provider, subject=subject: raw_insert(
                        session, "allowlist_entries",
                        **entry_row(user, provider=provider, subject=subject),
                    ),
                )
                seen[label] = accepted
    finally:
        engine.dispose()
    assert seen == {label: _app_accepts(*pair) for label, pair in SUBJECTS.items()}
    assert set(seen.values()) == {True, False}


# --- F. harness extension --------------------------------------------------------------------


def test_locale_option_is_validated_before_any_connection(project_run):
    with pytest.raises(ValueError):
        project_run.create_database(locale="en_US.utf8;DROP")
    with pytest.raises(ValueError):
        project_run.create_database(locale="C", template="not_a_scratch_database")
