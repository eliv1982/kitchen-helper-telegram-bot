"""HH-DB-01 Stage 2 WP-1B - setup-failure lifecycle of the SQL artifact fixtures (opt-in).

R1-05/R2-01/R2-02: the seven LITERAL project roles are cluster-wide, so a test that watches
`_provision_project` fail must start from a cluster with no project resources at all. That
requirement cannot be expressed inside a module whose module-scoped `project` fixture owns
those roles for the whole module: the tests would only be correct when defined - and
selected - before that fixture is first requested. They live in their own module instead,
which defines no project fixture, so the file order, a `-k` selection or a single node id
all start from the same empty state. `_require_empty_cluster` is NOT relaxed: an unexpected
leftover is a hard failure that names it, never a skip.

Order independence is module-granular: pytest finalizes a module's scoped fixtures before the
next module's items run, so this module and the `project` fixture's module never hold the
literal roles at the same time (for example
`pytest tests/test_postgres_sql_artifacts.py::test_the_complete_role_matrix_is_the_frozen_matrix tests/test_postgres_sql_setup_failures.py`
passes in either order). A runner that executed test modules concurrently would break the
premise; this suite has none and must not introduce one.

The provisioning helpers stay beside the module-scoped fixture that uses them
(`tests/test_postgres_sql_artifacts.py`); importing that module instantiates no fixture. Each
test here drives `_provision_project`/`RolesScript.apply` directly with a fresh `Run`.

Opt-in, like the other PostgreSQL suites:

    docker compose -f compose.test-postgres.yaml up -d
    HOUSE_HELPER_TEST_POSTGRES_URL=postgresql+psycopg://hh_test:hh_test_only_not_a_secret@127.0.0.1:55432/postgres pytest tests/test_postgres_sql_setup_failures.py -v -ra
"""

import os

import psycopg
import pytest

from tests import postgres_sql as artifacts
from tests import test_postgres_sql_artifacts as artifact_tests
from tests.postgres_harness import POSTGRES_URL_ENV, run_from_environment

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_URL_ENV),
        reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL "
        "(see tests/test_admin_postgres.py)",
    ),
]


@pytest.fixture(scope="module")
def project_run():
    """A bare Run: these tests own no serving database and never use the module fixture."""
    run = run_from_environment()
    run.verified_cluster()  # the independent identity, before any artifact is applied
    yield run
    run.close()


# --- the empty-cluster contract ---------------------------------------------------------------


def _require_empty_cluster(run) -> None:
    """The precondition of every test here, checked strictly at the START of the test.

    A non-empty cluster is a hard failure naming the leftovers: skipping would hide the
    very lifecycle fault these tests exist to catch."""
    with run.control() as control:
        roles = [
            row[0]
            for row in control.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (list(artifacts.PROJECT_ROLE_NAMES),),
            ).fetchall()
        ]
        databases = [
            row[0]
            for row in control.execute(
                "SELECT datname FROM pg_database WHERE datname = 'house_helper' "
                "OR datname ~ '^house_helper_restore_[a-z0-9]{1,32}$'"
            ).fetchall()
        ]
    if roles or databases:
        pytest.fail(
            "these setup-failure tests need a cluster with no project resources; found: "
            + ", ".join(sorted(roles) + sorted(databases))
        )


def _assert_no_project_residue(run) -> None:
    """The pre-test inventory: no project role, credential, database or explicit grant."""
    with run.control() as control:
        assert control.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
            (list(artifacts.PROJECT_ROLE_NAMES),),
        ).fetchone()[0] == 0
        assert control.execute(
            "SELECT count(*) FROM pg_authid WHERE rolname = ANY(%s)",
            (list(artifacts.PROJECT_ROLE_NAMES),),
        ).fetchone()[0] == 0
        assert control.execute(
            "SELECT count(*) FROM pg_database WHERE datname = 'house_helper' "
            "OR datname ~ '^house_helper_restore_[a-z0-9]{1,32}$'"
        ).fetchone()[0] == 0
        assert artifacts.parameter_acl_entries(control) == ()
        assert artifacts.default_acl_entries(control) == ()
        maintenance = run.target.maintenance_database
        assert control.execute(
            "SELECT count(*) FROM pg_database d "
            "CROSS JOIN LATERAL aclexplode(d.datacl) a "
            "JOIN pg_roles g ON g.oid = a.grantee "
            "WHERE d.datname = %s AND g.rolname = 'hh_restore' "
            "AND a.privilege_type IN ('CONNECT', 'TEMPORARY')",
            (maintenance,),
        ).fetchone()[0] == 0


# --- 0. R2-01: a failure across the creation/verification boundary leaves nothing ----------
#
# This is Codex's exact reproduction: `RolesScript.apply` used to COMMIT the seven roles and
# only then verify, so a RuntimeError from `verify` escaped before `_provision_project`
# learned what it had created and cleanup received an empty registry. Verification now runs
# inside the creation transaction (a verification failure rolls creation back) AND the
# fixture claims the absent-before names before the transaction starts, so the registry is
# correct even for an implementation that commits first.


def test_setup_failure_after_the_creation_transaction_leaves_no_residue(project_run, monkeypatch):
    _require_empty_cluster(project_run)
    real = artifacts.RolesScript.verify
    visible = {}

    def failing(self, connection):
        # Prove the roles really were created before the injected failure: all seven are
        # visible to this (uncommitted) transaction when verification is reached.
        visible["roles"] = {
            row[0]
            for row in connection.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (list(artifacts.PROJECT_ROLE_NAMES),),
            ).fetchall()
        }
        raise RuntimeError("injected post-creation verification failure")

    monkeypatch.setattr(artifacts.RolesScript, "verify", failing)
    with project_run.control() as control:
        with pytest.raises(RuntimeError, match="injected post-creation verification failure"):
            artifact_tests._provision_project(project_run, control)
    assert visible["roles"] == set(artifacts.PROJECT_ROLE_NAMES)  # verified: creation happened
    assert artifact_tests._CREATED_ROLE_NAMES == set()  # a completed cleanup clears the claim
    _assert_no_project_residue(project_run)


def test_setup_failure_during_role_creation_leaves_no_residue(project_run, monkeypatch):
    """A statement failing INSIDE the creation transaction, after the roles were created but
    before COMMIT: the transaction rolls back and the claimed names are absent, not adopted."""
    _require_empty_cluster(project_run)
    real = artifacts.RolesScript

    def sabotaged():
        return real(sql=real().sql + "\nSELECT 1 / 0;")

    monkeypatch.setattr(artifacts, "RolesScript", sabotaged)
    with project_run.control() as control:
        with pytest.raises(psycopg.errors.DivisionByZero):
            artifact_tests._provision_project(project_run, control)
    assert artifact_tests._CREATED_ROLE_NAMES == set()
    _assert_no_project_residue(project_run)


def test_setup_failure_during_password_initialisation_leaves_no_residue(project_run, monkeypatch):
    _require_empty_cluster(project_run)
    real = artifacts.set_test_password
    calls = {"count": 0}

    def failing(connection, login, password):
        calls["count"] += 1
        if calls["count"] == 3:
            raise RuntimeError("injected password-initialisation failure")
        return real(connection, login, password)

    monkeypatch.setattr(artifacts, "set_test_password", failing)
    with project_run.control() as control:
        with pytest.raises(RuntimeError, match="injected password-initialisation failure"):
            artifact_tests._provision_project(project_run, control)
    assert calls["count"] == 3
    _assert_no_project_residue(project_run)


def test_setup_failure_after_the_maintenance_grant_revokes_it(project_run, monkeypatch):
    _require_empty_cluster(project_run)
    real = artifact_tests._grant_maintenance_connect

    def failing(run, control):
        real(run, control)  # the grant really exists ...
        raise RuntimeError("injected maintenance-grant failure")  # ... and setup then fails

    monkeypatch.setattr(artifact_tests, "_grant_maintenance_connect", failing)
    with project_run.control() as control:
        with pytest.raises(RuntimeError, match="injected maintenance-grant failure"):
            artifact_tests._provision_project(project_run, control)
    _assert_no_project_residue(project_run)


def test_an_incomplete_cleanup_is_reported_with_the_setup_failure_and_can_be_retried(
    project_run, monkeypatch
):
    """Cleanup ITSELF failing (a role that still owns a database cannot be dropped):

    * the setup exception stays the one raised - a cleanup failure is attached, never
      substituted for it, and never silently swallowed;
    * the unfinished records stay registered (nothing is recorded as cleaned);
    * once the obstacle is gone, the retained registry lets the same verified cleanup
      finish, and the cluster is clean."""
    _require_empty_cluster(project_run)
    dependency = "wp1b_dependency_db"

    def failing_passwords():
        with project_run.control() as control:
            control.execute(f'CREATE DATABASE "{dependency}" OWNER "hh_app"')
        raise RuntimeError("injected setup failure after role creation")

    monkeypatch.setattr(artifacts, "generate_test_passwords", failing_passwords)
    try:
        with project_run.control() as control:
            with pytest.raises(
                RuntimeError, match="injected setup failure after role creation"
            ) as raised:
                artifact_tests._provision_project(project_run, control)
        # The setup error is primary; the cleanup failure is the attached cause.
        assert isinstance(raised.value.__cause__, psycopg.errors.DependentObjectsStillExist)
        assert dependency in str(raised.value.__cause__)
        assert artifact_tests._CREATED_ROLE_NAMES == frozenset(artifacts.PROJECT_ROLE_NAMES)
        with project_run.control() as control:
            assert control.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'hh_app'"
            ).fetchone() is not None  # the owner blocking the drop is still there
    finally:
        with project_run.control() as control:
            control.execute(f'DROP DATABASE IF EXISTS "{dependency}" WITH (FORCE)')
        artifact_tests._deprovision_project(project_run)  # the retry the registry enables
    assert artifact_tests._CREATED_ROLE_NAMES == set()
    _assert_no_project_residue(project_run)


# --- 0b. ownership of pre-existing resources is never taken over ---------------------------


def test_setup_refuses_a_pre_existing_role_and_does_not_drop_it(project_run):
    _require_empty_cluster(project_run)
    with project_run.control() as control:
        control.execute('CREATE ROLE "hh_provision" NOLOGIN')  # a decoy, not ours
    try:
        with project_run.control() as control:
            with pytest.raises(pytest.fail.Exception):
                artifact_tests._provision_project(project_run, control)
        with project_run.control() as control:
            remaining = [
                row[0]
                for row in control.execute(
                    "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                    (list(artifacts.PROJECT_ROLE_NAMES),),
                ).fetchall()
            ]
        assert remaining == ["hh_provision"]  # the decoy survives; nothing else was made
    finally:
        with project_run.control() as control:
            control.execute('DROP ROLE IF EXISTS "hh_provision"')
    _assert_no_project_residue(project_run)


def test_the_claim_never_covers_a_role_that_already_existed(project_run):
    """The ownership basis is ABSENCE before execution, not a successful CREATE: a role this
    test created (and `roles.sql` converged) is never in the claim, so the verified cleanup
    cannot drop it."""
    _require_empty_cluster(project_run)
    # hh_owner's frozen matrix is NOLOGIN NOINHERIT (the CREATE ROLE defaults otherwise
    # match), so preflight accepts it as an existing, converged role.
    # This role is this test's, not the artifact's.
    with project_run.control() as control:
        control.execute('CREATE ROLE "hh_owner" NOLOGIN NOINHERIT')
    claimed = []
    observed = []

    def claim(names):
        # The claim must arrive BEFORE the creation transaction: at this instant only the
        # pre-existing role exists.
        with project_run.control() as ordering:
            observed.append(
                {
                    row[0]
                    for row in ordering.execute(
                        "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                        (list(artifacts.PROJECT_ROLE_NAMES),),
                    ).fetchall()
                }
            )
        claimed.append(names)
        artifact_tests._claim_roles(names)

    try:
        with project_run.control() as control:
            result = artifacts.RolesScript().apply(control, claim=claim)
        assert observed == [{"hh_owner"}]  # nothing was created yet when the claim ran
        assert claimed == [frozenset(artifacts.PROJECT_ROLE_NAMES) - {"hh_owner"}]
        assert result.created == frozenset(artifacts.PROJECT_ROLE_NAMES) - {"hh_owner"}
        assert result.existing == frozenset({"hh_owner"})
    finally:
        artifact_tests._deprovision_project(project_run)  # only the claimed six
        with project_run.control() as control:
            control.execute('DROP ROLE IF EXISTS "hh_owner"')  # this test's decoy
    _assert_no_project_residue(project_run)


def test_cleanup_does_not_touch_an_unrelated_role(project_run, monkeypatch):
    _require_empty_cluster(project_run)
    with project_run.control() as control:
        control.execute('CREATE ROLE "wp1b_unrelated" NOLOGIN')

    def failing(connection, login, password):
        raise RuntimeError("injected password-initialisation failure")

    monkeypatch.setattr(artifacts, "set_test_password", failing)
    try:
        with project_run.control() as control:
            with pytest.raises(RuntimeError, match="injected"):
                artifact_tests._provision_project(project_run, control)
        with project_run.control() as control:
            assert control.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'wp1b_unrelated'"
            ).fetchone() is not None
    finally:
        with project_run.control() as control:
            control.execute('DROP ROLE IF EXISTS "wp1b_unrelated"')
    _assert_no_project_residue(project_run)
