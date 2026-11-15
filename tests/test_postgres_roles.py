"""The frozen role and ACL contract on a real PostgreSQL 16 server (HH-DB-01 CI-16, CI-17).

PROPOSED ERRATUM - OWNER APPROVAL PENDING (R3-05): the frozen §3.6 negative test 1 says an
unauthorized `GRANT` fails with SQLSTATE 42501. PostgreSQL 16 does not do that here: a
`GRANT` from a role without the grant option is answered with a WARNING (SQLSTATE 01007)
and grants nothing. This module therefore asserts the EFFECT - the complete ACL snapshot
and every effective privilege are unchanged before and after, inside the transaction -
and records the 01007 warning as evidence. The frozen specification is not edited, and
this reading is not presented as accepted. If the Owner rejects the erratum, the test
must change, not the server.

Every statement here runs as one of the per-run restricted identities created by
tests/postgres_harness.py (`prepare_roles`), not as the bootstrap superuser: the
application login does DML with no DDL, the migration login is the only DDL identity,
the provisioning login cannot delete or read sessions, the purge login has exactly the
ten retention tables and the columns it locks or writes, and the backup login only reads.
The bootstrap superuser creates and drops the roles and databases and does nothing else.

Negative expectations are the frozen §3.6 ones: SQLSTATE `42501` (insufficient
privilege) or a refused connection. A statement that succeeds where it must fail is a
failure of this module, not a note.

The disposable smoke clone (CI-17) is accessed with the application credential only; the
migration and provisioning credentials are shown not to reach it. This says nothing about
archive recovery, sequence restoration or Production recoverability (that is Stage 4).
"""

import ipaddress
import os
import re
import shutil
import subprocess
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from house_helper.admin import enrollment, provisioning, retention
from house_helper.admin.cli import main
from house_helper.persistence.database import (
    DATABASE_URL_ENV,
    create_session_factory,
    upgrade_database,
)
from house_helper.persistence.models import MessageRecipe
from tests.persistence_support import (
    new_conversation,
    new_login_session,
    new_message,
    new_user,
    utc,
)
from tests import postgres_contract as contract
from tests.postgres_harness import (
    POSTGRES_URL_ENV,
    PURGE_COLUMN_UPDATES,
    PURGE_TABLES,
    ROLE_KEYS,
    Run,
    Scratch,
    leaked,
    run_from_environment,
)
from tests.revisions import HEAD_REVISION
from tests.test_admin_postgres import pg_run, scalar, scratch, template  # noqa: F401  (fixtures)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get(POSTGRES_URL_ENV),
        reason=f"opt-in: set {POSTGRES_URL_ENV} to a local throwaway PostgreSQL "
        "(see tests/test_admin_postgres.py)",
    ),
]

PROVISION_TABLES = ("users", "allowlist_entries", "identity_invitations")
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


def sqlstate(error) -> str | None:
    return getattr(getattr(error, "orig", error), "sqlstate", None)


def refused(engine, statement, params=None, *, state="42501"):
    """`statement` must be refused by the database with `state` (or by the driver when it
    cannot even bind the value). Returns the original error for an assertion on its text."""
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(statement), params or {})
    except sa.exc.DBAPIError as error:
        assert sqlstate(error) == state, f"{statement}: {error.orig}"
        return error
    pytest.fail(f"the database allowed: {statement}")


def works(engine, statement, params=None):
    with engine.begin() as connection:
        result = connection.execute(sa.text(statement), params or {})
        return result.all() if result.returns_rows else []


def run_as(engine, statement, params=None):
    with engine.begin() as connection:
        return connection.execute(sa.text(statement), params or {}).scalar()


def identity(engine) -> tuple[str, str]:
    with engine.connect() as connection:
        row = connection.execute(sa.text("SELECT session_user, current_user")).one()
        connection.rollback()
    return tuple(row)


def connection_identity(connection) -> tuple[str, str]:
    row = connection.execute("SELECT session_user, current_user").fetchone()
    return tuple(row)


def membership_edges(control, roles) -> list[tuple[str, str, bool, bool]]:
    """Every explicit membership edge that touches a project role (R3-06), decoded to the
    harness's role keys; an edge to a non-project role keeps its name and is a violation."""
    key_of = {role.name: key for key, role in roles.items()}
    rows = control.execute(
        "SELECT member.rolname, granted.rolname, m.set_option, m.inherit_option "
        "FROM pg_auth_members m "
        "JOIN pg_roles member ON member.oid = m.member "
        "JOIN pg_roles granted ON granted.oid = m.roleid "
        "WHERE member.rolname = ANY(%s) OR granted.rolname = ANY(%s)",
        (list(key_of), list(key_of)),
    ).fetchall()
    return [
        (key_of.get(member, member), key_of.get(granted, granted), set_option, inherit_option)
        for member, granted, set_option, inherit_option in rows
    ]


def database_owner_matrix(connection, roles) -> dict[str, tuple[bool, bool, bool]]:
    """MEMBER, USAGE and SET for every project login against `pg_database_owner`, measured
    in the connection's current database (MEM-01/MEM-07)."""
    return {
        key: tuple(
            connection.execute(
                "SELECT pg_has_role(%s, 'pg_database_owner', %s)", (role.name, mode)
            ).fetchone()[0]
            for mode in ("MEMBER", "USAGE", "SET")
        )
        for key, role in roles.items()
    }


def test_the_integration_fixture_backend_really_is_postgresql(database_backend, tmp_path):
    """Not the presence of the variable: the database the fixture groups hand out is
    PostgreSQL, and its application login is the restricted one (CI-01, CI-14)."""
    database = database_backend.new_database(tmp_path=tmp_path)
    engine = database.app_engine()
    try:
        assert engine.dialect.name == "postgresql"
        with engine.connect() as connection:
            session_user, current_user = connection.execute(
                sa.text("SELECT session_user, current_user")
            ).one()
            connection.rollback()
        assert session_user == current_user
        assert session_user.startswith("hh_t_") and session_user.endswith("_app")
    finally:
        engine.dispose()
        database.close()


# --- the identities themselves ----------------------------------------------------------------


def test_each_login_is_its_own_restricted_identity(scratch):
    roles = scratch.run.roles
    expected = {
        "app": ("app", "app"),
        "provision": ("provision", "provision"),
        "purge": ("purge", "purge"),
        "backup": ("backup", "backup"),
        "migrate": ("migrate", "owner"),
    }
    for key, (session_user, current_user) in expected.items():
        engine = scratch.run.engine(scratch.name, key)
        try:
            assert identity(engine) == (roles[session_user].name, roles[current_user].name)
        finally:
            engine.dispose()


def test_the_owner_cannot_log_in_and_the_credentials_are_all_different(scratch):
    roles = scratch.run.roles
    assert run_as(scratch.app_engine(), "SELECT rolcanlogin FROM pg_roles WHERE rolname = :n", {"n": roles["owner"].name}) is False
    passwords = {key: identity.password for key, identity in roles.items() if identity.password}
    assert len(set(passwords.values())) == len(passwords)  # no two logins share a credential
    bootstrap = scratch.run.target.password
    assert bootstrap is None or bootstrap not in passwords.values()


def test_the_role_attributes_are_the_frozen_matrix(scratch):
    roles = scratch.run.roles
    attributes = {
        "owner": {"login": False, "super": False, "createdb": False, "createrole": False, "inherit": False},
        "migrate": {"login": True, "super": False, "createdb": False, "createrole": False, "inherit": False},
        "app": {"login": True, "super": False, "createdb": False, "createrole": False, "inherit": False},
        "purge": {"login": True, "super": False, "createdb": False, "createrole": False, "inherit": False},
        "backup": {"login": True, "super": False, "createdb": False, "createrole": False, "inherit": False},
        "provision": {"login": True, "super": False, "createdb": False, "createrole": False, "inherit": False},
        "restore": {"login": True, "super": False, "createdb": True, "createrole": False, "inherit": False},
    }
    with scratch.run.control() as control:
        for key, expected in attributes.items():
            row = control.execute(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s",
                (roles[key].name,),
            ).fetchone()
            assert tuple(row) == (
                expected["login"], expected["super"], expected["createdb"],
                expected["createrole"], expected["inherit"], False,
            ), key
        # R3-06: the COMPLETE project membership graph, not only the owner's inbound
        # edges - an unexpected `purge -> app` (or any other) is reported too.
        edges = membership_edges(control, roles)
    assert contract.membership_violations(edges) == []


def test_the_owner_memberships_are_effective_exactly_as_declared(scratch):
    """R3-06: the explicit options are not the whole story. MEMBER, USAGE and SET against
    the owner are measured independently: both maintenance logins may assume it (SET) but
    do not inherit it (USAGE false); every runtime login has nothing."""
    roles = scratch.run.roles
    expected = {
        "migrate": (True, False, True),
        "restore": (True, False, True),
        "app": (False, False, False),
        "purge": (False, False, False),
        "backup": (False, False, False),
        "provision": (False, False, False),
    }
    with scratch.run.control() as control:
        measured = {
            key: tuple(
                control.execute(
                    "SELECT pg_has_role(%s, %s, %s)",
                    (roles[key].name, roles["owner"].name, mode),
                ).fetchone()[0]
                for mode in ("MEMBER", "USAGE", "SET")
            )
            for key in expected
        }
    assert measured == expected

    # The required outcomes: the migration login's configured default role, an explicit
    # SET ROLE, RESET ROLE with that default, and SET ROLE NONE (frozen §3.2/ROL-02).
    # PostgreSQL 16 documents two different resets: `RESET ROLE` restores the role's own
    # connection default - here `ALTER ROLE <migrate> SET role = <owner>`, so `current_user`
    # stays the owner - while `SET ROLE NONE` always returns `current_user` to
    # `session_user`. Asserting the previous expectation (`RESET ROLE` -> migrate) tested
    # the server against a rule it does not have.
    migrate, owner = roles["migrate"].name, roles["owner"].name
    with scratch.run.connect_as(scratch.name, "migrate") as connection:
        assert connection_identity(connection) == (migrate, owner)  # the login default
        connection.execute(f'SET ROLE "{migrate}"')
        assert connection_identity(connection) == (migrate, migrate)  # explicit SET ROLE
        connection.execute("RESET ROLE")
        assert connection_identity(connection) == (migrate, owner)  # back to the default
        connection.execute(f'SET ROLE "{owner}"')
        assert connection_identity(connection) == (migrate, owner)  # the owner itself
        connection.execute("RESET ROLE")
        assert connection_identity(connection) == (migrate, owner)  # the default again
        connection.execute("SET ROLE NONE")
        assert connection_identity(connection) == (migrate, migrate)  # the session user
    # ... and the application login cannot.
    engine = scratch.app_engine()
    try:
        refused(engine, f'SET ROLE "{roles["owner"].name}"')
    finally:
        engine.dispose()


def test_no_project_role_holds_a_predefined_role(scratch):
    names = [identity.name for identity in scratch.run.roles.values()]
    rows = scratch.app_engine()
    try:
        with rows.connect() as connection:
            granted = connection.execute(
                sa.text(
                    "SELECT member.rolname, granted.rolname FROM pg_auth_members m "
                    "JOIN pg_roles member ON member.oid = m.member "
                    "JOIN pg_roles granted ON granted.oid = m.roleid "
                    "WHERE granted.rolname LIKE 'pg\\_%' "
                    "AND member.rolname = ANY(:names)"
                ),
                {"names": names},
            ).all()
            connection.rollback()
    finally:
        rows.dispose()
    assert granted == []  # pg_database_owner is implicit, not a pg_auth_members row


def test_the_application_cannot_become_the_owner(scratch):
    engine = scratch.app_engine()
    try:
        refused(engine, f'SET ROLE "{scratch.run.roles["owner"].name}"')
    finally:
        engine.dispose()


# --- positive: what each identity may do -------------------------------------------------------


def test_the_application_has_all_four_dml_privileges_on_every_application_table(scratch):
    engine = scratch.app_engine()
    try:
        with engine.connect() as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ]
            for table in tables:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    expected = privilege == "SELECT" if table == "alembic_version" else True
                    assert connection.execute(
                        sa.text("SELECT has_table_privilege(current_user, :t, :p)"),
                        {"t": table, "p": privilege},
                    ).scalar() is expected, (table, privilege)
            connection.rollback()
    finally:
        engine.dispose()


def test_the_application_consumes_the_messages_sequence_and_reads_alembic_version(scratch):
    engine = scratch.app_engine()
    try:
        with create_session_factory(engine)() as db, db.begin():
            user = new_user(db, display_name="Elena")
            conversation = new_conversation(db, user)
            first = new_message(db, conversation, role="user", content="one")
            second = new_message(db, conversation, role="assistant", content="two")
        assert (first.id, second.id) == (1, 2)  # the sequence was consumed by the app login
        with engine.connect() as connection:
            current = connection.execute(sa.text("SELECT currval(pg_get_serial_sequence('messages','id'))")).scalar()
            assert current == 2
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == HEAD_REVISION
            connection.rollback()
        assert works(engine, "SELECT 1") == [(1,)]
    finally:
        engine.dispose()


def test_the_purge_login_can_lock_and_delete_exactly_its_tables(scratch):
    engine = scratch.purge_engine()
    try:
        for table in PURGE_TABLES:
            works(engine, f"SELECT 1 FROM {table} LIMIT 1 FOR UPDATE SKIP LOCKED")
            # R2-07: the DELETE form the retention command actually issues, per table.
            if table == "telegram_update_admissions":
                works(
                    engine,
                    "DELETE FROM telegram_update_admissions WHERE (bot_id, update_id) IN "
                    "(SELECT bot_id, update_id FROM telegram_update_admissions LIMIT 1 "
                    "FOR UPDATE SKIP LOCKED)",
                )
            else:
                works(
                    engine,
                    f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} LIMIT 1 "
                    "FOR UPDATE SKIP LOCKED)",
                )
        works(
            engine,
            "UPDATE turn_requests SET status = status, finished_at = finished_at, updated_at = updated_at "
            "WHERE id IN (SELECT id FROM turn_requests LIMIT 1)",
        )
    finally:
        engine.dispose()


def test_the_backup_login_reads_every_table_and_sequence(scratch):
    engine = scratch.backup_engine()
    try:
        with engine.connect() as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ]
            for table in tables:
                assert connection.execute(
                    sa.text("SELECT has_table_privilege(current_user, :t, 'SELECT')"), {"t": table}
                ).scalar() is True, table
            assert connection.execute(
                sa.text(
                    "SELECT has_sequence_privilege(current_user, "
                    "pg_get_serial_sequence('messages','id'), 'SELECT')"
                )
            ).scalar() is True
            works(engine, "SELECT last_value, is_called FROM messages_id_seq")
            connection.rollback()
    finally:
        engine.dispose()


def test_the_provisioning_login_runs_the_commands(scratch, capsys):
    environ = {DATABASE_URL_ENV: scratch.url_text_for("provision")}
    assert main(["enroll-user", "--display-name", "Elena"], environ=environ) == 0
    enrolled = capsys.readouterr()
    assert main(["enrollment-status"], environ=environ) == 0
    status = capsys.readouterr()
    assert enrolled.out.startswith("ok [enrolled]: ")
    assert "Elena" in status.out and "invitation:" in status.out


def test_the_migration_login_owns_every_object(scratch):
    # The clone was made by the migration login; every object belongs to the NOLOGIN owner.
    engine = scratch.migrate_engine()
    try:
        with engine.connect() as connection:
            owner = scratch.run.roles["owner"].name
            rows = connection.execute(
                sa.text(
                    "SELECT c.relname, r.rolname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_roles r ON r.oid = c.relowner "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r','i','S')"
                )
            ).all()
            assert rows and all(rolname == owner for _, rolname in rows)
            connection.rollback()
    finally:
        engine.dispose()


# --- negative: what each identity may not do ----------------------------------------------------


def test_the_application_can_do_no_ddl_or_temp_work(scratch):
    engine = scratch.app_engine()
    try:
        for statement in (
            "CREATE TABLE app_made (id int)",
            "DROP TABLE users",
            "ALTER TABLE users ADD COLUMN app_made int",
            "CREATE INDEX app_made_idx ON users (display_name)",
            "TRUNCATE messages",
            "CREATE SCHEMA app_schema",
        ):
            refused(engine, statement)
        refused(engine, "CREATE TEMP TABLE app_temp (id int)")
        # PROPOSED ERRATUM - OWNER APPROVAL PENDING (R3-05): PostgreSQL 16 answers a GRANT
        # from a role without the grant option with a WARNING (01007) and grants nothing,
        # not with SQLSTATE 42501 as the frozen text says. The contract that matters is the
        # effect - asserted completely in `test_an_application_grant_is_a_warning_...`.
        works(engine, "GRANT SELECT ON users TO public")
        assert run_as(engine, "SELECT has_table_privilege('public', 'users', 'SELECT')") is False
    finally:
        engine.dispose()


def test_the_application_can_only_read_alembic_version(scratch):
    engine = scratch.app_engine()
    try:
        assert works(engine, "SELECT version_num FROM alembic_version") == [(HEAD_REVISION,)]
        for statement in (
            "INSERT INTO alembic_version (version_num) VALUES ('9999')",
            "UPDATE alembic_version SET version_num = '9999'",
            "DELETE FROM alembic_version",
            "SELECT setval(pg_get_serial_sequence('messages','id'), 100)",
        ):
            refused(engine, statement)
    finally:
        engine.dispose()


def test_the_application_cannot_reach_another_database(scratch):
    # A database of this run that the application was never granted CONNECT on.
    other = scratch.run.create_database(runtime_roles=())
    try:
        engine = scratch.run.engine(other, "app")
        with pytest.raises(sa.exc.OperationalError):
            engine.connect()
        engine.dispose()
        with scratch.run.control() as control:
            granted = control.execute(
                "SELECT has_database_privilege(%s, %s, 'CONNECT')",
                (scratch.run.roles["app"].name, other),
            ).fetchone()[0]
        assert granted is False
    finally:
        scratch.run.drop_database(other)


def test_the_purge_login_has_no_insert_ddl_truncate_or_sequence(scratch):
    engine = scratch.purge_engine()
    try:
        for statement in (
            "INSERT INTO sessions (id, user_id, token_hash, created_at, last_seen_at, expires_at, revoked_at) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), :h, now(), now(), now(), now())",
            "CREATE TABLE purge_made (id int)",
            "ALTER TABLE sessions ADD COLUMN purge_made int",
            "TRUNCATE sessions",
            "SELECT nextval(pg_get_serial_sequence('messages','id'))",
            "CREATE TEMP TABLE purge_temp (id int)",
        ):
            refused(engine, statement, {"h": "0" * 64})
    finally:
        engine.dispose()


def test_the_purge_login_may_update_only_the_columns_it_needs(scratch):
    engine = scratch.purge_engine()
    try:
        for table, columns in PURGE_COLUMN_UPDATES.items():
            granted = ", ".join(columns)
            works(engine, f"UPDATE {table} SET {granted.split(', ')[0]} = {granted.split(', ')[0]} WHERE false")
        for statement in (
            "UPDATE sessions SET token_hash = token_hash WHERE false",
            "UPDATE conversations SET user_id = user_id WHERE false",
            "UPDATE messages SET content = content WHERE false",
            "DELETE FROM alembic_version",
            "UPDATE alembic_version SET version_num = version_num",
        ):
            refused(engine, statement)
    finally:
        engine.dispose()


def test_the_purge_login_touches_no_table_outside_the_retention_set(scratch):
    engine = scratch.purge_engine()
    try:
        for table in NON_PURGE_TABLES:
            refused(engine, f"SELECT 1 FROM {table} LIMIT 1")
    finally:
        engine.dispose()


def test_the_backup_login_cannot_write_or_change_the_schema(scratch):
    engine = scratch.backup_engine()
    try:
        for statement in (
            "INSERT INTO users (id, display_name, created_at, updated_at) "
            "VALUES (gen_random_uuid(), 'x', now(), now())",
            "UPDATE users SET display_name = display_name",
            "DELETE FROM messages",
            "CREATE TABLE backup_made (id int)",
            "TRUNCATE users",
        ):
            refused(engine, statement)
    finally:
        engine.dispose()


def test_the_provisioning_login_cannot_delete_migrate_or_read_sessions(scratch):
    engine = scratch.provision_engine()
    try:
        refused(engine, "DELETE FROM users")
        refused(engine, "CREATE TABLE provision_made (id int)")
        refused(engine, "SELECT 1 FROM sessions LIMIT 1")
        refused(engine, "SELECT 1 FROM messages LIMIT 1")
    finally:
        engine.dispose()


def test_the_purge_effective_privilege_matrix_is_exact(scratch):
    """ROL-11/ROL-15: every table in `public` is compared, so a future table without an
    explicit purge grant fails, and no default ACL gives it one."""
    engine = scratch.purge_engine()
    try:
        with engine.connect() as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ]
            for table in tables:
                for privilege, expected in (
                    ("SELECT", table in PURGE_TABLES or table == "alembic_version"),
                    ("DELETE", table in PURGE_TABLES),
                ):
                    actual = connection.execute(
                        sa.text("SELECT has_table_privilege(current_user, :t, :p)"),
                        {"t": table, "p": privilege},
                    ).scalar()
                    assert actual is expected, (table, privilege)
                # DELETE on a non-retention table would already have failed; a column UPDATE
                # outside the grant set must be absent.
                if table not in PURGE_TABLES and table != "alembic_version":
                    # no column of a table outside the retention set may be updatable
                    updatable = connection.execute(
                        sa.text(
                            "SELECT count(*) FROM pg_attribute a WHERE a.attrelid = CAST(:t AS regclass) "
                            "AND a.attnum > 0 AND NOT a.attisdropped "
                            "AND has_column_privilege(current_user, a.attrelid, a.attname, 'UPDATE')"
                        ),
                        {"t": table},
                    ).scalar()
                    assert updatable == 0, table
            # No default ACL inheritance: a table made now by the owner is not purge-readable.
            connection.rollback()
    finally:
        engine.dispose()


def test_the_provisioning_effective_privilege_matrix_is_exact(scratch):
    engine = scratch.provision_engine()
    try:
        with engine.connect() as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ]
            for table in tables:
                dml = table in PROVISION_TABLES
                readable = dml or table == "identity_links" or table == "alembic_version"
                for privilege, expected in (
                    ("SELECT", readable),
                    ("INSERT", dml),
                    ("UPDATE", dml),
                    ("DELETE", False),
                ):
                    actual = connection.execute(
                        sa.text("SELECT has_table_privilege(current_user, :t, :p)"),
                        {"t": table, "p": privilege},
                    ).scalar()
                    assert actual is expected, (table, privilege)
            connection.rollback()
    finally:
        engine.dispose()


def test_the_application_effective_privilege_matrix_is_exact(scratch):
    engine = scratch.app_engine()
    try:
        with engine.connect() as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ]
            for table in tables:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    expected = privilege == "SELECT" if table == "alembic_version" else True
                    actual = connection.execute(
                        sa.text("SELECT has_table_privilege(current_user, :t, :p)"),
                        {"t": table, "p": privilege},
                    ).scalar()
                    assert actual is expected, (table, privilege)
            connection.rollback()
    finally:
        engine.dispose()


def test_the_purge_run_and_the_message_recipe_cascade_work_as_the_purge_login(scratch):
    from datetime import timedelta

    from house_helper.persistence.models import Message, MessageRecipe as Recipe

    now = utc(2026, 10, 23, 3, 30)
    old = now - timedelta(days=400)
    app = scratch.app_engine()
    try:
        with create_session_factory(app)() as db, db.begin():
            user = new_user(db, display_name="Elena")
            conversation = new_conversation(db, user, created_at=old, updated_at=old)
            answer = new_message(
                db, conversation, role="assistant", content="x", created_at=old,
                expires_at=old + timedelta(days=1),
            )
            db.add(
                Recipe(
                    message_id=answer.id,
                    message_role="assistant",
                    position=0,
                    title="Борщ",
                    ingredients=[],
                    steps=[],
                    created_at=old,
                )
            )
            new_login_session(db, user, created_at=old, expires_at=old + timedelta(days=1))
    finally:
        pass
    purge = scratch.purge_engine()
    try:
        preview = retention.purge_expired(purge, now=now, dry_run=True, pause=lambda: None)
        assert preview.deleted["sessions"] == 1 and preview.deleted["messages"] == 1
        with create_session_factory(app)() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(Recipe)) == 1  # preview deletes nothing
        report = retention.purge_expired(purge, now=now, pause=lambda: None)
        assert report.deleted["sessions"] == 1 and report.deleted["messages"] == 1
        # the appliance-side row went with its message through the FK cascade (owner's rights)
        assert report.total == 3  # session, message, conversation (the recipe goes with its message)
    finally:
        purge.dispose()
    try:
        with create_session_factory(app)() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(Recipe)) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(Message)) == 0
    finally:
        app.dispose()


# --- the fence ----------------------------------------------------------------------------------


def test_the_connection_fence_stops_every_runtime_login_but_the_migration_one(pg_run):
    name = pg_run.create_database()  # empty, owned, with the runtime CONNECT handoffs
    try:
        with pg_run.control() as control:  # the maintenance fence: CONNECT withdrawn, still open
            for key in ("app", "provision", "purge", "backup"):
                control.execute(
                    f'REVOKE CONNECT ON DATABASE "{name}" FROM "{pg_run.roles[key].name}"'
                )
        for key in ("app", "provision", "purge", "backup"):
            engine = pg_run.engine(name, key)
            with pytest.raises(sa.exc.OperationalError):
                engine.connect()
            engine.dispose()
        migrate = pg_run.engine(name, "migrate")
        try:
            assert identity(migrate) == (pg_run.roles["migrate"].name, pg_run.roles["owner"].name)
        finally:
            migrate.dispose()
    finally:
        with pg_run.control() as control:
            for key in ("app", "provision", "purge", "backup"):
                control.execute(
                    f'GRANT CONNECT ON DATABASE "{name}" TO "{pg_run.roles[key].name}"'
                )
        pg_run.drop_database(name)


# --- the disposable smoke clone (CI-17) ----------------------------------------------------------


def test_the_smoke_clone_is_reachable_only_with_the_application_credential(pg_run, template):
    clone = pg_run.create_database(template=template, runtime_roles=())
    try:
        # The handoff that makes it a clone of the application's database: app CONNECT only.
        with pg_run.control() as control:
            control.execute(f'GRANT CONNECT ON DATABASE "{clone}" TO "{pg_run.roles["app"].name}"')
        app = pg_run.engine(clone, "app")
        try:
            with create_session_factory(app)() as db, db.begin():
                user = new_user(db, display_name="Elena")
                conversation = new_conversation(db, user)
                new_message(db, conversation, content="smoke")
            with app.connect() as connection:
                assert connection.execute(sa.text("SELECT count(*) FROM messages")).scalar() == 1
                connection.rollback()
        finally:
            app.dispose()
        # The other credentials do not reach it: no CONNECT was handed to them. (The
        # migration login is a SET member of the owner, which owns the database: its
        # delegated reach is expected, MEM-03, and it is never the credential a smoke
        # clone is read with.)
        for key in ("provision", "purge", "backup"):
            engine = pg_run.engine(clone, key)
            with pytest.raises(sa.exc.OperationalError):
                engine.connect()
            engine.dispose()
    finally:
        pg_run.drop_database(clone)


# --- R2-03: the commands run on their designated identities, at the command boundary ---------------


def test_the_provisioning_commands_execute_as_the_provisioning_identity(scratch, capsys, monkeypatch):
    """R2-03: the URL text is not evidence. The identity is read inside the command's own
    transaction, through both provisioning modules."""
    seen = []
    real_provisioning = provisioning.check_schema_at_head
    real_enrollment = enrollment.check_schema_at_head

    def recorder(real):
        def inner(connection):
            seen.append(tuple(connection.exec_driver_sql("SELECT session_user, current_user").one()))
            return real(connection)

        return inner

    monkeypatch.setattr(provisioning, "check_schema_at_head", recorder(real_provisioning))
    monkeypatch.setattr(enrollment, "check_schema_at_head", recorder(real_enrollment))
    environ = {DATABASE_URL_ENV: scratch.url_text_for("provision")}

    assert main(["provision-email", "--email", "Elena@example.com"], environ=environ) == 0
    capsys.readouterr()
    assert main(["enroll-user", "--display-name", "Мама"], environ=environ) == 0
    capsys.readouterr()

    provision = scratch.run.roles["provision"].name
    assert len(seen) >= 2 and set(seen) == {(provision, provision)}


def test_the_purge_command_executes_as_the_purge_identity(scratch, monkeypatch):
    seen = []
    real = retention.check_schema_at_head

    def recorder(connection):
        seen.append(tuple(connection.exec_driver_sql("SELECT session_user, current_user").one()))
        return real(connection)

    monkeypatch.setattr(retention, "check_schema_at_head", recorder)
    engine = scratch.purge_engine()
    try:
        retention.purge_expired(engine, now=utc(2026, 10, 23, 3, 30), pause=lambda: None)
    finally:
        engine.dispose()

    purge = scratch.run.roles["purge"].name
    assert seen and set(seen) == {(purge, purge)}


# --- R2-07: the exact purge matrix, future objects and complete forbidden privileges ---------------


def test_the_purge_column_update_matrix_is_exact(scratch):
    engine = scratch.purge_engine()
    try:
        with engine.connect() as connection:
            for table in PURGE_TABLES:
                columns = connection.execute(
                    sa.text(
                        "SELECT a.attname FROM pg_attribute a "
                        "WHERE a.attrelid = CAST(:t AS regclass) AND a.attnum > 0 AND NOT a.attisdropped "
                        "ORDER BY a.attnum"
                    ),
                    {"t": table},
                ).scalars().all()
                permitted = set(PURGE_COLUMN_UPDATES.get(table, ()))
                assert permitted <= set(columns), table
                for column in columns:
                    actual = connection.execute(
                        sa.text(
                            "SELECT has_column_privilege(current_user, CAST(:t AS regclass), :c, 'UPDATE')"
                        ),
                        {"t": table, "c": column},
                    ).scalar()
                    assert actual is (column in permitted), (table, column)
            for column in ("version_num",):
                assert connection.execute(
                    sa.text("SELECT has_column_privilege(current_user, 'alembic_version', :c, 'UPDATE')"),
                    {"c": column},
                ).scalar() is False
            connection.rollback()
    finally:
        engine.dispose()


def test_the_purge_login_has_no_other_table_or_sequence_privilege(scratch):
    engine = scratch.purge_engine()
    try:
        with engine.connect() as connection:
            for table in PURGE_TABLES:
                for privilege in ("INSERT", "TRUNCATE", "REFERENCES", "TRIGGER"):
                    actual = connection.execute(
                        sa.text("SELECT has_table_privilege(current_user, :t, :p)"),
                        {"t": table, "p": privilege},
                    ).scalar()
                    assert actual is False, (table, privilege)
            sequences = connection.execute(
                sa.text("SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'")
            ).scalars().all()
            assert sequences  # a typo must not make this vacuous
            for sequence in sequences:
                qualified = f"public.{sequence}"
                for privilege in ("USAGE", "SELECT", "UPDATE"):
                    actual = connection.execute(
                        sa.text("SELECT has_sequence_privilege(current_user, :s, :p)"),
                        {"s": qualified, "p": privilege},
                    ).scalar()
                    assert actual is False, (sequence, privilege)
            connection.rollback()
    finally:
        engine.dispose()


def test_a_future_table_gets_no_purge_grant_from_the_default_acl(scratch):
    """ROL-14/ROL-15: a table made now is app-writable and backup-readable through the
    default ACLs, and not purge-readable at all."""
    migration = scratch.migrate_engine()
    try:
        with migration.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE future_retention (id integer PRIMARY KEY, expires_at timestamptz NOT NULL)"
            )
    finally:
        migration.dispose()

    purge = scratch.purge_engine()
    try:
        refused(purge, "SELECT 1 FROM future_retention LIMIT 1")
        with purge.connect() as connection:
            inherited = connection.execute(
                sa.text(
                    "SELECT count(*) FROM pg_default_acl d "
                    "JOIN LATERAL aclexplode(d.defaclacl) a ON true "
                    "JOIN pg_roles acl_grantee ON acl_grantee.oid = a.grantee "
                    "WHERE acl_grantee.rolname = current_user"
                )
            ).scalar()
            connection.rollback()
        assert inherited == 0
    finally:
        purge.dispose()

    for key, privilege, expected in (("app", "INSERT", True), ("backup", "SELECT", True)):
        engine = scratch.run.engine(scratch.name, key)
        try:
            assert run_as(
                engine,
                "SELECT has_table_privilege(current_user, 'future_retention', :p)",
                {"p": privilege},
            ) is expected, key
        finally:
            engine.dispose()


# --- R2-07: backup, temporary databases, and the maintenance fence --------------------------------

PG_CLIENT_MAJOR = "16"  # the frozen server major (CI-06): another major's client is not used


def _postgres_16_client(tool: str) -> str | None:
    """The host's own `tool`, only while it is a PostgreSQL 16 client. A different client
    major cannot dump a 16 server (and makes the archive evidence ambiguous), so the
    container's matching client is used instead."""
    path = shutil.which(tool)
    if path is None:
        return None
    done = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60)
    if done.returncode != 0 or not done.stdout.startswith(f"{tool} (PostgreSQL) {PG_CLIENT_MAJOR}."):
        return None
    return path


def _container_address(container_id: str) -> str:
    """The verified test container's own non-loopback address.

    The image's pg_hba.conf trusts `127.0.0.1/32` and the Unix socket, but its closing
    rule makes every other address `scram-sha-256`. A client run INSIDE the container must
    therefore connect to the container's bridge address - the only address at which the
    generated restricted login is authenticated by its password rather than trusted by
    address, and never the bootstrap superuser or a local socket."""
    done = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}\n{{end}}",
            container_id,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    addresses = []
    for line in done.stdout.splitlines():
        try:
            address = ipaddress.ip_address(line.strip())
        except ValueError:
            continue
        if address.version == 4 and not address.is_loopback:
            addresses.append(str(address))
    if done.returncode != 0 or not addresses:
        pytest.fail("the verified test container has no non-loopback address for a client")
    return sorted(addresses)[0]


# --- Fix 02: the purge login's refused dump has exactly one acceptable reading --------------
#
# `pg_dump` 16 locks every plain table it will dump in one batched statement before reading
# any rows (`getTables` in pg_dump.c: `LOCK TABLE <a>, <b>, ... IN ACCESS SHARE MODE`). A
# refused lock goes through `ExecuteSqlStatement` -> `die_on_query_failure`
# (pg_backup_db.c), which prints exactly two client records and then `exit(1)`:
#
#     pg_dump: error: query failed: ERROR:  permission denied for table users
#     pg_dump: detail: Query was: LOCK TABLE public.users, public.messages IN ACCESS SHARE MODE
#
# libpq renders the server's primary line as `<severity>:  <text>` (`pqBuildErrorMessage3`:
# two spaces after the colon) and `PQerrorMessage` ends with a newline, which pg_dump's
# logger replaces with its own before printing (`pg_log_generic_v` in src/common/logging.c),
# so the records are adjacent lines with no blank line between them. A blank line is
# tolerated below, but that is robustness, not the shape PostgreSQL 16 produces.
#
# Nothing weaker is accepted, and stderr is never searched for a matching fragment: it is
# parsed as complete pg_dump records. A nonzero exit is required; then the only records
# allowed are that one query failure and its lock context. A warning, a second error, a
# timeout, a denied connection, an HBA or authentication line, a version mismatch, a
# `DETAIL`/`CONTEXT`/`STATEMENT` line or any other client text fails the assertion, as does
# a denial on an object the login may read, a lock context naming the same table in another
# schema (or two same-named ones), or an expected denial quoted inside another error's detail.
_IDENTIFIER = r"[a-z_][a-z0-9_$]*"  # `fmtId` leaves every relation name of this schema bare
_QUALIFIED_NAME = rf"{_IDENTIFIER}\.{_IDENTIFIER}"
_DUMP_RECORD = re.compile(
    r"^pg_dump: (?P<severity>error|warning|notice|info|log|debug|detail|hint): (?P<text>.*)$"
)
_PRIMARY_DENIAL = re.compile(
    rf"^query failed: ERROR:  permission denied for table (?P<object>{_IDENTIFIER})$"
)
_LOCK_CONTEXT = re.compile(
    rf"^Query was: LOCK TABLE (?P<relations>{_QUALIFIED_NAME}(?:, {_QUALIFIED_NAME})*) "
    r"IN ACCESS SHARE MODE$"
)


def assert_the_purge_dump_was_refused(
    stderr: bytes, *, returncode: int, unreadable: frozenset[str]
) -> str:
    """Return the table PostgreSQL refused the purge login's `pg_dump`, or fail.

    `unreadable` is the fixture's own set: the tables and sequences of this scratch database
    that the purge login may not SELECT (everything except its ten retention tables and
    `alembic_version`), read from the catalog's privilege functions. The accepted reading is
    one complete pg_dump 16 query-failure record pair - a nonzero exit, a single primary
    server `permission denied for table <name>` answering the batched table lock, and that
    lock's own SQL context naming exactly one `public.<name>` - with `<name>` in
    `unreadable`. Every other exit status, diagnostic, additional record or object is an
    assertion failure carrying pg_dump's text.
    """
    text = stderr.decode(errors="replace")
    assert returncode != 0, (
        f"the purge dump exited 0, so PostgreSQL refused nothing: {text[-1500:]}"
    )

    records = []
    for line in text.splitlines():
        if not line.strip():
            continue  # empty space between records: tolerated, though PG16 emits none
        record = _DUMP_RECORD.match(line)
        assert record is not None, (
            f"the purge dump printed a line that is not a pg_dump diagnostic record: {line!r}"
        )
        records.append((record.group("severity"), record.group("text")))

    assert [severity for severity, _ in records] == ["error", "detail"], (
        "the purge dump's diagnostics are not the one query failure and its lock context - "
        f"another error, warning or client message is present: {text[-1500:]}"
    )
    (_, failure), (_, context) = records

    denied = _PRIMARY_DENIAL.match(failure)
    assert denied is not None, (
        "the purge dump's error is not PostgreSQL's single primary insufficient-privilege "
        f"ERROR on a table: {failure!r}"
    )
    name = denied.group("object")

    lock = _LOCK_CONTEXT.match(context)
    assert lock is not None, (
        "the purge dump's detail is not the SQL context of the failed table lock "
        f"(`LOCK TABLE ... IN ACCESS SHARE MODE`): {context!r}"
    )
    named = [
        tuple(relation.split("."))
        for relation in lock.group("relations").split(", ")
        if relation.split(".")[1] == name
    ]
    assert len(named) == 1 and named[0][0] == "public", (
        f"the failed lock does not name exactly one public.{name}: {context!r}"
    )
    assert name in unreadable, (
        f"the purge dump was refused for table {name!r}, an object the purge login may "
        f"read: a genuine refusal names one of the {len(unreadable)} relations it may not read"
    )
    return name


def _relations_the_purge_login_cannot_read(engine, login: str) -> frozenset[str]:
    """Every table and sequence of this scratch database that `login` (the generated purge
    identity) may not SELECT, asked of the catalog's own privilege functions - the fixture
    itself, not a list repeated in this file. Views carry no rows to lock, so they are not
    something a dump can be refused for."""
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    """
                    SELECT c.relname
                    FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relkind IN ('r', 'p', 'S')
                      AND NOT CASE c.relkind
                                WHEN 'S' THEN pg_catalog.has_sequence_privilege(
                                    :login, c.oid, 'SELECT')
                                ELSE pg_catalog.has_table_privilege(
                                    :login, c.oid, 'SELECT')
                              END
                    ORDER BY c.relname
                    """
                ),
                {"login": login},
            ).all()
    finally:
        engine.dispose()
    return frozenset(row[0] for row in rows)


def test_the_backup_login_can_run_pg_dump_and_the_purge_login_cannot(scratch, tmp_path):
    """R2-07: the backup login takes a real `pg_dump -Fc` of this scratch database and the
    purge login's identical dump is refused. Both are the generated restricted identities,
    never the bootstrap superuser, and the archive is re-opened with `pg_restore --list`
    to prove its format and the database it came from.

    The host's PostgreSQL 16 client is used when present; otherwise the client inside the
    independently verified test container runs against that container's non-loopback
    address, so the server's password rule applies (the image's loopback rule is `trust`).
    A deliberately wrong password is refused first, which is what shows the credential -
    not the address - authenticated the backup login."""
    target = scratch.run.target
    roles = scratch.run.roles
    backup, purge = roles["backup"], roles["purge"]

    # Both generated logins are live and restricted, so the refusal below can only be a
    # privilege refusal: neither the role nor the credential may be the reason it fails.
    for key in ("backup", "purge"):
        engine = scratch.run.engine(scratch.name, key)
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    sa.text(
                        "SELECT current_database(), session_user, current_user, "
                        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
                    )
                ).one()
                connection.rollback()
        finally:
            engine.dispose()
        assert tuple(row) == (scratch.name, roles[key].name, roles[key].name, False), key

    host_dump = _postgres_16_client("pg_dump")
    host_restore = _postgres_16_client("pg_restore")
    use_host = host_dump is not None and host_restore is not None
    container = address = None
    if not use_host:
        container = scratch.run.verified_cluster().container_id
        address = _container_address(container)

    def run_client(tool, arguments, identity, *, password=None, data=None):
        """`tool` against this scratch database as `identity`, on whichever client won.
        The password never appears in argv: the container path propagates its name from
        this process's environment into the container."""
        secret = identity.password if password is None else password
        env = {name: value for name, value in os.environ.items() if not name.upper().startswith("PG")}
        env["PGPASSWORD"] = secret or ""
        if use_host:
            env.update(
                PGHOST=target.address,
                PGPORT=str(target.port),
                PGDATABASE=scratch.name,
                PGUSER=identity.name,
                PGSSLMODE="disable",
                PGOPTIONS="",
            )
            executable = host_dump if tool == "pg_dump" else host_restore
            return subprocess.run(
                [executable, *arguments], env=env, input=data, capture_output=True, timeout=120
            )
        return subprocess.run(
            [
                "docker", "exec", "-i",
                "-e", "PGPASSWORD",  # by name only: nothing secret is written on the command line
                "-e", f"PGHOST={address}",
                "-e", "PGPORT=5432",  # the image's listen port; the verified port is the host's
                "-e", f"PGDATABASE={scratch.name}",
                "-e", f"PGUSER={identity.name}",
                "-e", "PGSSLMODE=disable",
                "-e", "PGOPTIONS=",
                container,
                tool,
                *arguments,
            ],
            env=env,
            input=data,
            capture_output=True,
            timeout=120,
        )

    # A wrong password on the same client path must not connect: this is the proof that the
    # server checked the credential, rather than trusting the address or a local socket.
    wrong = run_client("pg_dump", ["-Fc"], backup, password="not-the-generated-password")
    assert wrong.returncode != 0
    assert b"password authentication failed" in wrong.stderr.lower()

    done = run_client("pg_dump", ["-Fc"], backup)
    assert done.returncode == 0, done.stderr.decode(errors="replace")  # (names, never a password)
    assert done.stdout[:5] == b"PGDMP"  # the custom-format magic, i.e. `-Fc`
    output = tmp_path / "backup.dump"
    try:
        output.write_bytes(done.stdout)
        assert output.stat().st_size > 0

        # The archive is readable and names the database it was taken from, in custom format.
        listed = run_client("pg_restore", ["--list"], backup, data=done.stdout)
        assert listed.returncode == 0, listed.stderr.decode(errors="replace")
        listing = listed.stdout.decode(errors="replace")
        assert f";     dbname: {scratch.name}" in listing
        assert "Format: CUSTOM" in listing
    finally:
        # Neither the archive nor the rows it carries outlives the test, on either outcome.
        output.unlink(missing_ok=True)
    assert not output.exists()

    # What this fixture proves the purge login may not read: the tables and sequences of the
    # scratch database outside its ten retention tables and the migration marker it may read.
    unreadable = _relations_the_purge_login_cannot_read(scratch.migrate_engine(), purge.name)
    assert set(NON_PURGE_TABLES) <= unreadable
    assert "alembic_version" not in unreadable
    assert set(PURGE_TABLES).isdisjoint(unreadable)

    refused_dump = run_client("pg_dump", ["-Fc"], purge)
    # PostgreSQL's own insufficient-privilege refusal of the dump's table lock, on a table
    # the login may not read - not a connection, credential, HBA, version, timeout or
    # transport failure (Fix 02A).
    assert_the_purge_dump_was_refused(
        refused_dump.stderr, returncode=refused_dump.returncode, unreadable=unreadable
    )


def test_the_application_cannot_read_the_content_or_size_of_an_unauthorized_database(scratch):
    other = scratch.run.create_database(runtime_roles=())
    try:
        migration = scratch.run.migrate_engine(other)
        try:
            with migration.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE secret (value text)")
                connection.exec_driver_sql("INSERT INTO secret (value) VALUES ('private')")
        finally:
            migration.dispose()

        app = scratch.run.engine_as(other, "app")
        try:
            with pytest.raises(sa.exc.OperationalError):
                app.connect()
        finally:
            app.dispose()
        # The catalog row is public (FE-4), the content and the size are not.
        app_on_its_own = scratch.app_engine()
        try:
            refused(app_on_its_own, "SELECT pg_database_size(:name)", {"name": other})
        finally:
            app_on_its_own.dispose()
    finally:
        scratch.run.drop_database(other)


def test_the_runtime_identities_cannot_connect_to_the_maintenance_database(scratch):
    maintenance = scratch.run.target.maintenance_database
    for key in ("app", "purge", "backup", "provision"):
        engine = scratch.run.engine_as(maintenance, key)
        try:
            with pytest.raises(sa.exc.OperationalError):
                engine.connect()
        finally:
            engine.dispose()
    with scratch.run.connect_as(maintenance, "migrate") as connection:
        assert connection.execute("SELECT session_user, current_user").fetchone() == (
            scratch.run.roles["migrate"].name,
            scratch.run.roles["owner"].name,
        )
    with scratch.run.connect_as(maintenance, "restore") as connection:
        assert connection.execute("SELECT session_user, current_user").fetchone() == (
            scratch.run.roles["restore"].name,
            scratch.run.roles["restore"].name,
        )


# --- R2-07: predefined roles, pg_database_owner and the restore identity --------------------------


PREDEFINED_PROHIBITED = (
    "pg_monitor",
    "pg_read_all_settings",
    "pg_read_all_stats",
    "pg_stat_scan_tables",
    "pg_read_all_data",
    "pg_write_all_data",
    "pg_signal_backend",
    "pg_checkpoint",
    "pg_create_subscription",
    "pg_use_reserved_connections",
    "pg_read_server_files",
    "pg_write_server_files",
    "pg_execute_server_program",
)


def test_no_project_login_holds_a_prohibited_predefined_role_in_member_usage_or_set(scratch):
    engine = scratch.app_engine()
    try:
        for key in ROLE_KEYS:
            login = scratch.run.roles[key].name
            for role in PREDEFINED_PROHIBITED:
                measured = tuple(
                    run_as(
                        engine,
                        "SELECT pg_has_role(:login, :role, :mode)",
                        {"login": login, "role": role, "mode": mode},
                    )
                    for mode in ("MEMBER", "USAGE", "SET")
                )
                assert measured == (False, False, False), (key, role, scratch.name)
    finally:
        engine.dispose()


def test_pg_database_owner_membership_is_database_specific(scratch):
    """MEM-01..MEM-04: implicit ownership is evaluated against the current database's
    owner, and MEMBER, USAGE and SET are measured independently. In the owner's database
    the owner MEMBERs, USEs and SETs the implicit role and the two delegated SET chains
    MEMBER and SET it without inheriting it (R4-05); nowhere else may anything be true."""
    roles = scratch.run.roles
    with scratch.run.connect_as(scratch.name, "migrate") as connection:
        in_the_owners_database = database_owner_matrix(connection, roles)
    assert contract.pg_database_owner_violations(in_the_owners_database, owners_database=True) == []

    with scratch.run.control() as control:
        in_the_maintenance_database = database_owner_matrix(control, roles)
    assert contract.pg_database_owner_violations(
        in_the_maintenance_database, owners_database=False
    ) == []


def test_no_project_role_has_an_explicit_predefined_role_grant(scratch):
    with scratch.run.control() as control:
        rows = control.execute(
            "SELECT member.rolname, granted.rolname FROM pg_auth_members m "
            "JOIN pg_roles member ON member.oid = m.member "
            "JOIN pg_roles granted ON granted.oid = m.roleid "
            "WHERE granted.rolname LIKE 'pg\\_%%' AND member.rolname = ANY(%s)",
            ([scratch.run.roles[key].name for key in ROLE_KEYS],),
        ).fetchall()
    assert rows == []


def test_the_restore_login_creates_and_owns_a_database_through_the_role_chain(scratch):
    """R3-07/R4-01: the bounded Stage 1 restore-identity probes of RST-01/RST-08 -
    maintenance identity, the authorized SET ROLE transition, a disposable database
    creation, its expected ownership, ACL hardening under the intended role, object
    creation under the expected identity, and an ownership-verified cleanup. This is not
    the Stage 4 archive restore pipeline.

    R4-01 item 8: the disposable database is created and dropped through the run's guarded,
    ownership-tracked lifecycle (`create_database_as` / `drop_database`), not with raw SQL,
    so the probe cannot leave an untracked database behind."""
    roles = scratch.run.roles
    owner, restore, app = roles["owner"].name, roles["restore"].name, roles["app"].name
    maintenance = scratch.run.target.maintenance_database

    # Phase 3 premise - the restore login's own CREATEDB creates the database, disabled
    # and owned by the NOLOGIN owner; the run records it for cleanup.
    name = scratch.run.create_database_as("restore")
    try:
        with scratch.run.connect_as(maintenance, "restore") as connection:
            # Phase 1 - the maintenance connection is the restore login, in the maintenance DB.
            assert connection_identity(connection) == (restore, restore)
            assert connection.execute("SELECT current_database()").fetchone()[0] == maintenance

            # Phase 2 - the required transition, observed in both directions.
            connection.execute(f'SET ROLE "{owner}"')
            assert connection_identity(connection) == (restore, owner)
            connection.execute("RESET ROLE")
            assert connection_identity(connection) == (restore, restore)

            # Phase 3 - the expected ownership and the disabled state.
            row = connection.execute(
                "SELECT pg_get_userbyid(datdba), datallowconn FROM pg_database WHERE datname = %s",
                (name,),
            ).fetchone()
            assert tuple(row) == (owner, False)

            # Phase 4 - ACL hardening while connections are still disabled, as the owner.
            connection.execute(f'SET ROLE "{owner}"')
            assert connection_identity(connection) == (restore, owner)
            connection.execute("SET search_path = public")
            assert connection.execute("SHOW search_path").fetchone()[0] == "public"
            connection.execute(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC')
            connection.execute(f'GRANT CONNECT ON DATABASE "{name}" TO "{restore}"')
            acl = connection.execute(
                "SELECT d.datallowconn, d.datacl IS NOT NULL, "
                "(SELECT count(*) FROM aclexplode(d.datacl) a WHERE a.grantee = 0) "
                "FROM pg_database d WHERE d.datname = %s",
                (name,),
            ).fetchone()
            assert tuple(acl) == (False, True, 0)  # disabled, ACL materialized, no PUBLIC entry
            connection.execute(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS true')
            assert connection.execute(
                "SELECT datallowconn, has_database_privilege(%s, %s, 'CONNECT'), "
                "has_database_privilege(%s, %s, 'CONNECT') FROM pg_database WHERE datname = %s",
                (restore, name, app, name, name),
            ).fetchone() == (True, True, False)
            connection.execute("RESET ROLE")
            assert connection_identity(connection) == (restore, restore)

        # Phase 5 - object creation under the expected identity, on a verified connection
        # of its own: the session user is the restore login, the current user the owner.
        with scratch.run.connect_as(name, "restore") as probe:
            assert connection_identity(probe) == (restore, restore)
            probe.execute(f'SET ROLE "{owner}"')
            assert connection_identity(probe) == (restore, owner)
            probe.execute("SET search_path = public")
            probe.execute("CREATE TABLE restore_probe (id integer)")
            assert probe.execute(
                "SELECT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE oid = 'restore_probe'::regclass"
            ).fetchone()[0] == owner
        app_engine = scratch.run.engine(name, "app")
        try:
            with pytest.raises(sa.exc.OperationalError):  # no CONNECT was granted to it
                app_engine.connect()
        finally:
            app_engine.dispose()
    finally:
        # Phase 6 - ownership-, token- and identity-verified cleanup through the lifecycle.
        scratch.run.drop_database(name)

    with scratch.run.control() as control:
        # `hh_restore` has no CONNECT on the live application database (RST-03).
        assert control.execute(
            "SELECT has_database_privilege(%s, %s, 'CONNECT')", (restore, scratch.name)
        ).fetchone()[0] is False


# --- R2-07: the §3.6 negative GRANT test, with its before/after ACL evidence -----------------------


def _acl_snapshot(connection, scratch):
    """The complete ACL snapshot (tests/postgres_contract.py) for `users`, with the
    effective privileges of every project login and PUBLIC (R3-05)."""
    identities = [scratch.run.roles[key].name for key in ROLE_KEYS]
    return contract.acl_snapshot(connection, table="users", identities=identities)


def test_an_application_grant_is_a_warning_that_changes_no_acl(scratch):
    """PROPOSED ERRATUM - OWNER APPROVAL PENDING (see the module docstring).

    The frozen §3.6 negative test 1 says SQLSTATE 42501; PostgreSQL 16 answers a GRANT
    from a role without the grant option with a WARNING (01007) and grants nothing. The
    EFFECT is asserted here, before any rollback could conceal a change: complete table and
    column ACLs (with grantors and grant options), `pg_default_acl`, and the effective
    privileges of PUBLIC and every project login are identical before and after the
    attempted statement, and no unauthorized privilege was acquired."""
    engine = scratch.app_engine()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            notices, record = contract.notice_recorder()  # copies values INSIDE the callback
            driver = connection.connection.driver_connection
            driver.add_notice_handler(record)
            try:
                before = _acl_snapshot(connection, scratch)
                # R4-07: the notices captured from here to the re-read are exactly the ones
                # the attempted GRANT produced, so the warning is correlated with it.
                grant_index = len(notices)
                connection.exec_driver_sql("GRANT SELECT ON users TO public")
                attempted = tuple(notices[grant_index:])
                after = _acl_snapshot(connection, scratch)  # read before the rollback
            finally:
                driver.remove_notice_handler(record)
                transaction.rollback()
        assert before == after
        assert contract.unauthorized_privileges(before) == []
        assert contract.unauthorized_privileges(after) == []
        # The `app` login still cannot create, and no role gained a privilege: the
        # snapshot's effective table/column values for every identity are unchanged.
        assert before[5] == after[5] and before[6] == after[6]
    finally:
        engine.dispose()

    # R4-07: exactly one WARNING (01007) naming the attempted table, and nothing else.
    assert contract.grant_notice_violations(attempted, table="users") == [], attempted


def test_a_run_creates_and_drops_its_own_roles(monkeypatch):
    """The lifecycle end to end: a fresh Run makes its seven roles, and close() removes
    exactly them and nothing else."""
    run = run_from_environment()
    try:
        run.prepare_roles()
        names = [identity.name for identity in run.roles.values()]
        with run.control() as control:
            present = control.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (names,)
            ).fetchone()[0]
        assert present == len(names)
    finally:
        run.close()
    with run_from_environment().control() as control:  # a new bootstrap connection
        present = control.execute("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (names,)).fetchone()[0]
    assert present == 0
