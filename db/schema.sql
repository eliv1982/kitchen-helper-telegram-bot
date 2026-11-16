-- HH-DB-01 Stage 2 WP-1C-A - the BOOT-04 schema contract.
--
-- Frozen source: §3.3 ROL-05/ROL-06, §5.1 BOOT-04 (with ROL-09 and ROL-14),
-- §5.3 BOOT-09 step 1, §8.8 RST-01/RST-04 step 8.
--
-- Authority and boundary
--   * Invoked ONLY as `hh_migrate` (serving database) or `hh_restore` (restore database)
--     with `current_user = hh_owner`, after E3-c has enabled connections and BEFORE the
--     schema is initialized. The runner in tests/postgres_sql.py connects the login, has it
--     assume the owner, and supplies ONE transaction; this file contains no BEGIN/COMMIT.
--     A caller that owns the transaction must therefore BEGIN before the file; a caller that
--     does not is refused by the transaction guard below BEFORE the session preparation or
--     any catalog mutation (a SAVEPOINT is legal only inside a transaction block).
--   * The caller's own ROL-06 preamble is NOT trusted and is NOT required: this file
--     establishes the full ROL-06 session contract itself (search_path, application_name,
--     statement_timeout - step 0) and re-verifies it in the acceptance block. An initializer
--     that calls the artifact directly therefore cannot skip the mandatory preparation; a
--     caller whose session differs is converged, not believed. All three settings are
--     transaction-local (`SET LOCAL` / `set_config(..., is_local => true)`), so nothing
--     outlives the caller's transaction and no unrelated session is changed (ROL-05: the
--     owner's `rolconfig` is never relied on either).
--   * `current_database()` must be the database named by the validated `{{database}}`
--     parameter, and `public` must be the only non-system schema.
--   * This file establishes the schema contract only. It creates no application object and
--     does NOT require `alembic_version`: schema hardening precedes `alembic upgrade head`
--     (BOOT-04/BOOT-09). It never drops or adopts an object that already exists.
--
-- Exactness and idempotency
--   * `public` belongs to `hh_owner`, PUBLIC keeps no schema privilege, exactly the six
--     LOGIN roles receive USAGE and no CREATE, and the four ROL-14 default ACLs are
--     installed for future objects created by the owner.
--   * The acceptance state is verified before COMMIT: schema ownership, the exact exploded
--     schema ACL (no PUBLIC entry, no grant option, no recipient outside the seven), the
--     effective USAGE/CREATE matrix of all seven roles, the exact hh_owner default ACL set
--     (global + schema-specific), `public` as the only non-system schema, and the effective
--     ROL-06 session settings (phase identity, `statement_timeout = 0`).
--   * Every statement converges to the same catalog state, so re-execution is a no-op. A
--     state this file cannot converge (an unrelated recipient's schema grant, an unexpected
--     default ACL, a foreign schema, a conflicting schema owner) is refused, and the
--     transaction rolls back with nothing changed.

-- 0. TRANSACTION GUARD - the FIRST executable statement, before the session preparation and
--    before any catalog mutation. ROL-06 is established transaction-locally (`SET LOCAL`,
--    `set_config(..., is_local => true)`), so a caller that prealigns the three settings at
--    SESSION level can satisfy step 1's value checks while running outside a transaction:
--    every statement would then commit on its own, and a late verification failure (for
--    example an unrelated schema grant refused only in step 4) would leave partially
--    committed ownership and default-ACL changes. PostgreSQL accepts a SAVEPOINT only inside
--    a transaction block (SQLSTATE 25P01 otherwise), so this pair refuses a transactionless
--    invocation before anything is prepared or changed. The savepoint is released at once and
--    never left behind; BEGIN/COMMIT/ROLLBACK remain the caller's (this file adds neither).
SAVEPOINT hh_schema_tx_guard;
RELEASE SAVEPOINT hh_schema_tx_guard;

-- 0b. ROL-06 session contract, established BEFORE any protected schema operation. The
--    authority for `application_name` is `session_user`, which the guard below pins to the
--    two permitted phases, so the value is exactly the frozen one ('hh_migrate' serving,
--    'hh_restore' restore). All three settings are transaction-local: at COMMIT/ROLLBACK the
--    session returns to whatever it had, and a value supplied by the caller (or by
--    `rolconfig`) is not carried past the transaction. Step 4 re-reads the EFFECTIVE values.
SET LOCAL search_path = public;                          -- pg_catalog remains implicitly first
SELECT set_config('application_name', session_user, true);
SELECT set_config('statement_timeout', '0', true);       -- migrations/restore only

-- 1. Phase identity, session contract, database and pre-existing-state guard. This is the
--    authoritative second layer for a psql caller; the runner checks the same facts before
--    it executes.
DO $hh_schema_guard$
DECLARE
    session text := session_user;
    current text := current_user;
    schema_owner text;
    foreign_schemas integer;
BEGIN
    IF NOT (session IN ('hh_migrate', 'hh_restore') AND current = 'hh_owner') THEN
        RAISE EXCEPTION
            'schema.sql must run as hh_migrate or hh_restore with current_user hh_owner (session_user=%, current_user=%)',
            session, current;
    END IF;
    -- ROL-06: the three settings must be in force, and `application_name` must name the
    -- phase. A caller that could not set them (no transaction block) fails closed here.
    IF current_setting('search_path') <> 'public' THEN
        RAISE EXCEPTION 'search_path is not exactly public (search_path=%)',
            current_setting('search_path');
    END IF;
    IF current_setting('application_name') <> session THEN
        RAISE EXCEPTION 'application_name is not the phase identity (application_name=%, session_user=%)',
            current_setting('application_name'), session;
    END IF;
    IF current_setting('statement_timeout') <> '0' THEN
        RAISE EXCEPTION 'statement_timeout is not 0 (statement_timeout=%)',
            current_setting('statement_timeout');
    END IF;
    IF current_database() <> '{{database}}' THEN
        RAISE EXCEPTION 'schema.sql was pointed at the wrong database (current_database=%)',
            current_database();
    END IF;
    SELECT n.nspowner::regrole::text
      INTO schema_owner
      FROM pg_namespace n
     WHERE n.nspname = 'public';
    IF schema_owner IS NULL THEN
        RAISE EXCEPTION 'the public schema is absent';
    END IF;
    -- A fresh database has `public` owned by pg_database_owner; a re-run has hh_owner. Any
    -- other owner is conflicting ownership (BOOT-07) and is refused, never adopted.
    IF schema_owner NOT IN ('pg_database_owner', 'hh_owner') THEN
        RAISE EXCEPTION 'the public schema is owned by an unexpected role (%)', schema_owner;
    END IF;
    SELECT count(*) INTO foreign_schemas
      FROM pg_namespace
     WHERE nspname <> 'public'
       AND nspname <> 'information_schema'
       AND nspname NOT LIKE 'pg\_%';
    IF foreign_schemas <> 0 THEN
        RAISE EXCEPTION 'public is not the only non-system schema';
    END IF;
END
$hh_schema_guard$;

-- 2. Ownership, PUBLIC removal and the exact USAGE set (BOOT-04 / ROL-09).
ALTER SCHEMA public OWNER TO hh_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO
      hh_app, hh_purge, hh_backup, hh_provision, hh_migrate, hh_restore;

-- 3. ROL-14 default privileges for future objects created by hh_owner. BOOT-09 step 4
--    re-applies and re-verifies the same set after the migration; this is the
--    bootstrap-time installation that BOOT-09 step 1 places before `alembic upgrade head`.
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hh_app;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO hh_app;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO hh_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO hh_backup;

-- 4. Verify the EFFECTIVE catalog state before the runner may COMMIT. A failure aborts the
--    whole transaction: no partially hardened schema is ever committed.
DO $hh_schema_verify$
DECLARE
    schema_owner text;
    public_schema_entries integer;
    observed_acl text[];
    expected_acl text[];
    observed_defaults text[];
    expected_defaults text[];
    expected_role record;
    usage boolean;
    create_priv boolean;
BEGIN
    -- 4a. Ownership and the absence of any PUBLIC schema entry.
    SELECT n.nspowner::regrole::text,
           (SELECT count(*) FROM aclexplode(n.nspacl) a WHERE a.grantee = 0)
      INTO schema_owner, public_schema_entries
      FROM pg_namespace n WHERE n.nspname = 'public';
    IF schema_owner <> 'hh_owner' OR public_schema_entries <> 0 THEN
        RAISE EXCEPTION
            'the public schema is not owned by hh_owner with no PUBLIC entry (owner=%, public_entries=%)',
            schema_owner, public_schema_entries;
    END IF;

    -- 4b. The EXACT exploded schema ACL: the owner (USAGE, CREATE) and the six logins
    --     (USAGE only), every entry without a grant option. An unrelated recipient cannot
    --     be revoked without naming it, so it is refused here (fail closed).
    SELECT array_agg(entry ORDER BY entry)
      INTO observed_acl
      FROM (
        SELECT format('%s|%s|%s',
                      COALESCE(g.rolname, 'PUBLIC'),
                      a.privilege_type,
                      CASE WHEN a.is_grantable THEN 'true' ELSE 'false' END) AS entry
          FROM pg_namespace n
          CROSS JOIN LATERAL aclexplode(n.nspacl) a
          LEFT JOIN pg_roles g ON g.oid = a.grantee
         WHERE n.nspname = 'public'
      ) exploded;

    SELECT array_agg(entry ORDER BY entry)
      INTO expected_acl
      FROM unnest(ARRAY[
          'hh_app|USAGE|false',
          'hh_backup|USAGE|false',
          'hh_migrate|USAGE|false',
          'hh_owner|CREATE|false',
          'hh_owner|USAGE|false',
          'hh_provision|USAGE|false',
          'hh_purge|USAGE|false',
          'hh_restore|USAGE|false'
      ]::text[]) AS entry;

    IF observed_acl IS DISTINCT FROM expected_acl THEN
        RAISE EXCEPTION 'the public schema ACL is not the frozen contract (%)', observed_acl;
    END IF;

    -- 4c. The EFFECTIVE USAGE/CREATE matrix of all seven project roles. The owner holds
    --     both privileges inherently; every LOGIN holds USAGE and no CREATE.
    FOR expected_role IN
        SELECT unnest(ARRAY[
            'hh_owner', 'hh_migrate', 'hh_app', 'hh_purge',
            'hh_backup', 'hh_provision', 'hh_restore'
        ]) AS name
    LOOP
        SELECT has_schema_privilege(expected_role.name, 'public', 'USAGE'),
               has_schema_privilege(expected_role.name, 'public', 'CREATE')
          INTO usage, create_priv;
        IF expected_role.name = 'hh_owner' THEN
            IF usage IS DISTINCT FROM true OR create_priv IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'the schema owner lost a privilege';
            END IF;
        ELSE
            IF usage IS DISTINCT FROM true OR create_priv IS DISTINCT FROM false THEN
                RAISE EXCEPTION 'schema privilege drift for %', expected_role.name;
            END IF;
        END IF;
    END LOOP;

    -- 4d. Default ACLs: the COMPLETE hh_owner set - GLOBAL (defaclnamespace = 0) and
    --     schema-specific - compared entry by entry. Both kinds apply to a new object, so a
    --     schema-level REVOKE could not cancel a global grant. Any recipient outside the
    --     frozen ROL-14 matrix (an unrelated role or PUBLIC) is refused, never silently
    --     accepted. The owner's own entries are PostgreSQL's, not the contract's.
    SELECT array_agg(entry ORDER BY entry)
      INTO observed_defaults
      FROM (
        SELECT format('%s|%s|%s|%s|%s',
                      COALESCE(n.nspname, '<global>'),
                      d.defaclobjtype,
                      CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE g.rolname END,
                      a.privilege_type,
                      CASE WHEN a.is_grantable THEN 'true' ELSE 'false' END) AS entry
          FROM pg_default_acl d
          JOIN pg_roles defacl_owner ON defacl_owner.oid = d.defaclrole
          LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
          CROSS JOIN LATERAL aclexplode(d.defaclacl) a
          LEFT JOIN pg_roles g ON g.oid = a.grantee
         WHERE defacl_owner.rolname = 'hh_owner'
           AND a.grantee <> defacl_owner.oid
      ) exploded;

    SELECT array_agg(entry ORDER BY entry)
      INTO expected_defaults
      FROM unnest(ARRAY[
          'public|S|hh_app|USAGE|false',
          'public|S|hh_backup|SELECT|false',
          'public|r|hh_app|DELETE|false',
          'public|r|hh_app|INSERT|false',
          'public|r|hh_app|SELECT|false',
          'public|r|hh_app|UPDATE|false',
          'public|r|hh_backup|SELECT|false'
      ]::text[]) AS entry;

    IF observed_defaults IS DISTINCT FROM expected_defaults THEN
        RAISE EXCEPTION
            'the hh_owner default ACL set is not the frozen matrix (global+schema) %',
            observed_defaults;
    END IF;

    -- 4e. ROL-06, re-read as the EFFECTIVE state after every change (step 0 established it;
    --     this proves it was not lost, and that it matches the phase).
    IF current_setting('search_path') <> 'public' THEN
        RAISE EXCEPTION 'search_path is not exactly public after the contract (%)',
            current_setting('search_path');
    END IF;
    IF current_setting('application_name') <> session_user THEN
        RAISE EXCEPTION 'application_name is not the phase identity (%)',
            current_setting('application_name');
    END IF;
    IF current_setting('statement_timeout') <> '0' THEN
        RAISE EXCEPTION 'statement_timeout is not 0 after the contract (%)',
            current_setting('statement_timeout');
    END IF;
END
$hh_schema_verify$;
