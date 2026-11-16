-- HH-DB-01 Stage 2 WP-1B - the seven project roles.
--
-- Frozen source: docs/architecture/HH-DB-01_Architecture_Frozen_Specification_v1.0.md
--   §3.2 role matrix (ROL-02..ROL-04), §3.3 membership (ROL-03), §3.4 ROL-17 session
--   defaults; accepted Stage 1 matrix in tests/postgres_harness.py.
--
-- Authority and boundary
--   * The bootstrap superuser only, connected to any database. The statement is refused
--     when `current_user` is not a superuser.
--   * ONE transaction. This file contains no BEGIN/COMMIT: the runner
--     (tests/postgres_sql.py, `RolesScript.apply`) supplies the transaction, so a failure
--     leaves no role, membership or setting behind.
--
-- Credentials
--   * No credential is written here. LOGIN roles are created with a NULL password
--     (`LOGIN` without `PASSWORD`), and an existing verifier is never read, reset or
--     overwritten: re-execution preserves whatever the OD-8 credential mechanism set.
--
-- Drift policy (explicit, fail-closed)
--   * An existing role whose security-relevant attributes differ from the matrix below
--     is REFUSED, never silently altered or adopted.
--   * Any membership edge that touches a project role and is not one of the two frozen
--     delegated-owner edges is REFUSED (this includes edges to predefined roles).
--   * A frozen edge whose ADMIN/INHERIT/SET options differ is REFUSED.
--   * A missing frozen role or a missing frozen edge is CREATED, because that converges
--     to the frozen state rather than adopting an unknown privilege.
--   * Session defaults are converged to the frozen values (setting a default can only
--     remove ambient privilege, never add a grant), and then verified EXACTLY: an extra
--     setting or a database-scoped role setting is REFUSED.
--   * No role is ever dropped by this file.

-- 1. Create the roles that do not exist. Attributes are spelled out completely.
DO $hh_create$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper
    ) THEN
        RAISE EXCEPTION 'roles.sql requires the bootstrap superuser (current_user is not a superuser)';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_owner') THEN
        CREATE ROLE hh_owner
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_migrate') THEN
        CREATE ROLE hh_migrate
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_app') THEN
        CREATE ROLE hh_app
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_purge') THEN
        CREATE ROLE hh_purge
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_backup') THEN
        CREATE ROLE hh_backup
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_provision') THEN
        CREATE ROLE hh_provision
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hh_restore') THEN
        CREATE ROLE hh_restore
            LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END
$hh_create$;

-- 2. Refuse attribute drift. The expected matrix is complete: login, superuser, create
--    database, create role, inherit, replication, bypass RLS, connection limit and
--    password validity. A role that does not match it exactly is not adopted.
DO $hh_attributes$
DECLARE
    expected record;
BEGIN
    FOR expected IN
        SELECT * FROM (VALUES
            ('hh_owner',     false, false, false, false, false, false, false, -1::integer),
            ('hh_migrate',   true,  false, false, false, false, false, false, -1::integer),
            ('hh_app',       true,  false, false, false, false, false, false, -1::integer),
            ('hh_purge',     true,  false, false, false, false, false, false, -1::integer),
            ('hh_backup',    true,  false, false, false, false, false, false, -1::integer),
            ('hh_provision', true,  false, false, false, false, false, false, -1::integer),
            ('hh_restore',   true,  false, true,  false, false, false, false, -1::integer)
        ) AS t(name, login, superuser, createdb, createrole, inherit, replication,
               bypassrls, connlimit)
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_roles r
            WHERE r.rolname = expected.name
              AND r.rolcanlogin = expected.login
              AND r.rolsuper = expected.superuser
              AND r.rolcreatedb = expected.createdb
              AND r.rolcreaterole = expected.createrole
              AND r.rolinherit = expected.inherit
              AND r.rolreplication = expected.replication
              AND r.rolbypassrls = expected.bypassrls
              AND r.rolconnlimit = expected.connlimit
              AND r.rolvaliduntil IS NULL
        ) THEN
            RAISE EXCEPTION 'role % does not match the frozen attribute matrix; refusing to adopt it', expected.name;
        END IF;
    END LOOP;
END
$hh_attributes$;

-- 3. Explicit parameter privileges. `pg_parameter_acl` is CLUSTER-WIDE, and it records only
--    EXPLICIT grants: a role may SET an ordinary context-appropriate parameter with no row
--    here at all. A row that names a project role or PUBLIC therefore hands out a capability
--    PostgreSQL's ordinary rules do not, and it is REFUSED rather than repaired - revoking it
--    could withdraw a grant another service relies on, and this file must never change an
--    unrelated cluster-wide privilege. A grant naming only other roles is left alone.
DO $hh_parameters$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_parameter_acl p
        CROSS JOIN LATERAL aclexplode(p.paracl) a
        LEFT JOIN pg_roles grantee ON grantee.oid = a.grantee
        WHERE a.grantee = 0
           OR grantee.rolname IN ('hh_owner', 'hh_migrate', 'hh_app', 'hh_purge',
                                  'hh_backup', 'hh_provision', 'hh_restore')
    ) THEN
        RAISE EXCEPTION 'an explicit parameter privilege names a project role or PUBLIC; refusing to adopt it';
    END IF;
END
$hh_parameters$;

-- 4. Membership edges. Refuse anything that is not the two frozen delegated-owner edges,
--    then create a missing frozen edge / verify an existing one in full.
DO $hh_membership$
DECLARE
    migrator record;
    restorer record;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_auth_members m
        JOIN pg_roles member ON member.oid = m.member
        JOIN pg_roles granted ON granted.oid = m.roleid
        WHERE (member.rolname IN ('hh_owner', 'hh_migrate', 'hh_app', 'hh_purge',
                                  'hh_backup', 'hh_provision', 'hh_restore')
               OR granted.rolname IN ('hh_owner', 'hh_migrate', 'hh_app', 'hh_purge',
                                      'hh_backup', 'hh_provision', 'hh_restore'))
          AND NOT (member.rolname = 'hh_migrate' AND granted.rolname = 'hh_owner')
          AND NOT (member.rolname = 'hh_restore' AND granted.rolname = 'hh_owner')
    ) THEN
        RAISE EXCEPTION 'an unexpected membership edge touches a project role; refusing to adopt it';
    END IF;

    SELECT m.admin_option, m.inherit_option, m.set_option
      INTO migrator
      FROM pg_auth_members m
      JOIN pg_roles member ON member.oid = m.member
      JOIN pg_roles granted ON granted.oid = m.roleid
     WHERE member.rolname = 'hh_migrate' AND granted.rolname = 'hh_owner';
    IF NOT FOUND THEN
        GRANT hh_owner TO hh_migrate WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
    ELSIF migrator.admin_option IS DISTINCT FROM false
       OR migrator.inherit_option IS DISTINCT FROM false
       OR migrator.set_option IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'the hh_migrate -> hh_owner edge has drift; refusing to adopt it';
    END IF;

    SELECT m.admin_option, m.inherit_option, m.set_option
      INTO restorer
      FROM pg_auth_members m
      JOIN pg_roles member ON member.oid = m.member
      JOIN pg_roles granted ON granted.oid = m.roleid
     WHERE member.rolname = 'hh_restore' AND granted.rolname = 'hh_owner';
    IF NOT FOUND THEN
        GRANT hh_owner TO hh_restore WITH ADMIN FALSE, INHERIT FALSE, SET TRUE;
    ELSIF restorer.admin_option IS DISTINCT FROM false
       OR restorer.inherit_option IS DISTINCT FROM false
       OR restorer.set_option IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'the hh_restore -> hh_owner edge has drift; refusing to adopt it';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_db_role_setting s
        JOIN pg_roles r ON r.oid = s.setrole
        WHERE s.setdatabase <> 0
          AND r.rolname IN ('hh_owner', 'hh_migrate', 'hh_app', 'hh_purge',
                            'hh_backup', 'hh_provision', 'hh_restore')
    ) THEN
        RAISE EXCEPTION 'a database-scoped role setting exists for a project role; refusing to adopt it';
    END IF;
END
$hh_membership$;

-- 5. Converge the ROL-17 session defaults. These are `ALTER ROLE ... SET` only; no
--    credential is touched.
ALTER ROLE hh_migrate   SET role = 'hh_owner';
ALTER ROLE hh_migrate   SET application_name = 'hh_migrate';
ALTER ROLE hh_app       SET search_path = 'public';
ALTER ROLE hh_app       SET statement_timeout = '30s';
ALTER ROLE hh_app       SET lock_timeout = '5s';
ALTER ROLE hh_app       SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE hh_app       SET application_name = 'hh_web';
ALTER ROLE hh_purge     SET application_name = 'hh_purge';
ALTER ROLE hh_backup    SET application_name = 'hh_backup';
ALTER ROLE hh_provision SET application_name = 'hh_provision';

-- 6. Verify the complete resulting state, including the exact `rolconfig` sets. An extra
--    setting is refused, never ignored; `hh_owner` and `hh_restore` have none.
DO $hh_verify$
DECLARE
    expected record;
    expected_settings text[];
    actual_settings text[];
BEGIN
    FOR expected IN
        SELECT * FROM (VALUES
            ('hh_owner',     '{}'::text[]),
            ('hh_migrate',   ARRAY['application_name=hh_migrate', 'role=hh_owner']::text[]),
            ('hh_app',       ARRAY['application_name=hh_web',
                                   'idle_in_transaction_session_timeout=60s',
                                   'lock_timeout=5s',
                                   'search_path=public',
                                   'statement_timeout=30s']::text[]),
            ('hh_purge',     ARRAY['application_name=hh_purge']::text[]),
            ('hh_backup',    ARRAY['application_name=hh_backup']::text[]),
            ('hh_provision', ARRAY['application_name=hh_provision']::text[]),
            ('hh_restore',   '{}'::text[])
        ) AS t(name, settings)
    LOOP
        SELECT COALESCE(
                   ARRAY(SELECT setting
                         FROM unnest(r.rolconfig) AS setting
                         ORDER BY setting),
                   '{}'::text[])
          INTO actual_settings
          FROM pg_roles r
         WHERE r.rolname = expected.name;
        expected_settings := expected.settings;
        IF actual_settings IS DISTINCT FROM expected_settings THEN
            RAISE EXCEPTION 'role % has unexpected session defaults; refusing to adopt them', expected.name;
        END IF;
    END LOOP;

    IF (SELECT count(*) FROM pg_roles
         WHERE rolname IN ('hh_owner', 'hh_migrate', 'hh_app', 'hh_purge',
                           'hh_backup', 'hh_provision', 'hh_restore')) <> 7 THEN
        RAISE EXCEPTION 'the seven project roles are not all present';
    END IF;

    -- The cluster-wide parameter ACLs stay exactly as step 3 required: no explicit grant
    -- names a project role or PUBLIC.
    IF EXISTS (
        SELECT 1
        FROM pg_parameter_acl p
        CROSS JOIN LATERAL aclexplode(p.paracl) a
        LEFT JOIN pg_roles grantee ON grantee.oid = a.grantee
        WHERE a.grantee = 0
           OR grantee.rolname IN ('hh_owner', 'hh_migrate', 'hh_app', 'hh_purge',
                                  'hh_backup', 'hh_provision', 'hh_restore')
    ) THEN
        RAISE EXCEPTION 'an explicit parameter privilege names a project role or PUBLIC';
    END IF;
END
$hh_verify$;
