-- HH-DB-01 Stage 2 WP-1B - the exact ROL-10..ROL-14 privilege contract.
--
-- Frozen source: §3.4 (ROL-09..ROL-18), §3.6 (positive/negative ACL tests), §3.8
-- (MEM-01..MEM-09), §8.8 RST-04/RST-07.
--
-- WP-1C-A R2 narrow integration correction: the transaction guard and the complete ROL-06
-- session preparation/verification were added under the R2 authorization. No ACL, grant,
-- default-privilege or catalog-verification statement below changed.
--
-- Authority and boundary
--   * Invoked ONLY as `hh_migrate` (serving database) or `hh_restore` (restore database)
--     with `current_user = hh_owner`. The runner in tests/postgres_sql.py connects the
--     login, has it assume the owner, and supplies ONE transaction; this file contains no
--     BEGIN/COMMIT. A caller that owns the transaction must BEGIN before the file; a caller
--     that does not is refused by the transaction guard before any protected operation.
--   * `current_database()` must be the database named by the validated `{{database}}`
--     parameter. ROL-06 is established by the script itself, transaction-locally:
--     `search_path = public`, `application_name` = the phase identity (`session_user`,
--     checked against the two permitted logins) and `statement_timeout = 0`. The effective
--     values are verified before the first unqualified statement, never assumed from the
--     caller's session.
--   * `public.alembic_version` MUST exist: this is applied after a migration or a
--     restore, never to an uninitialised schema.
--
-- Exactness
--   * Every project role's table/sequence/schema privileges are first REVOKED and then
--     granted, so the result is the frozen matrix rather than the matrix plus old drift.
--   * `hh_purge` receives exactly the ten retention tables, the columns it locks or
--     writes, and `alembic_version` SELECT. Its obsolete default SELECT is removed; the
--     other roles' default ACLs are converged too.
--   * The final block verifies the EFFECTIVE catalog state before the transaction may
--     commit: table, column, sequence, schema and default ACLs for every role.

-- The unqualified statements below are resolved in `public` only (ROL-06).

-- 0. TRANSACTION GUARD - the FIRST executable statement, before the session preparation and
--    before any protected operation. ROL-06 is transaction-local, so a caller that prealigns
--    the three settings at SESSION level could satisfy the guard's value checks while running
--    outside a transaction; each statement would commit on its own and a later refusal would
--    leave partially applied ACL/default-ACL changes. PostgreSQL accepts a SAVEPOINT only
--    inside a transaction block (SQLSTATE 25P01 otherwise), so this pair refuses a
--    transactionless invocation before anything is prepared or changed. The savepoint is
--    released at once and never left behind; BEGIN/COMMIT/ROLLBACK remain the caller's.
SAVEPOINT hh_privileges_tx_guard;
RELEASE SAVEPOINT hh_privileges_tx_guard;

-- 0b. ROL-06 session contract, established before any unqualified statement. All three
--     settings are transaction-local: at COMMIT/ROLLBACK the session returns to whatever the
--     caller had. `application_name` is the phase identity (`session_user`), which the guard
--     below pins to the two permitted phases.
SET LOCAL search_path = public;
SELECT set_config('application_name', session_user, true);
SELECT set_config('statement_timeout', '0', true);

-- 1. Phase identity, database, ROL-06 and schema guard.
DO $hh_guard$
DECLARE
    session text := session_user;
    current text := current_user;
    foreign_schemas integer;
BEGIN
    IF NOT (session IN ('hh_migrate', 'hh_restore') AND current = 'hh_owner') THEN
        RAISE EXCEPTION
            'privileges.sql must run as hh_migrate or hh_restore with current_user hh_owner (session_user=%, current_user=%)',
            session, current;
    END IF;
    IF current_database() <> '{{database}}' THEN
        RAISE EXCEPTION 'privileges.sql was pointed at the wrong database (current_database=%)',
            current_database();
    END IF;
    -- ROL-06: all three settings must be in force, and `application_name` must name the
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
    IF NOT EXISTS (
        SELECT 1 FROM pg_tables
        WHERE schemaname = 'public' AND tablename = 'alembic_version'
    ) THEN
        RAISE EXCEPTION 'alembic_version is absent; apply privileges only after a migration or a restore';
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
$hh_guard$;

-- 2. Remove every project grant, then install the frozen matrix. The owner is
--    `current_user`; each REVOKE makes the following GRANT the complete statement.
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM hh_app, hh_purge, hh_backup, hh_provision;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM hh_app, hh_purge, hh_backup, hh_provision;

-- ROL-10 - application: DML on every application table, USAGE on sequences, and the
-- `alembic_version` exception (SELECT only).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hh_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO hh_app;
REVOKE ALL ON alembic_version FROM hh_app;
GRANT SELECT ON alembic_version TO hh_app;

-- ROL-11 - purge: exactly the ten retention tables and the columns it locks or writes.
GRANT SELECT, DELETE ON
      sessions, oidc_states, email_login_challenges, identity_invitations,
      telegram_link_codes, telegram_update_admissions, message_attachments,
      messages, conversations, turn_requests TO hh_purge;

GRANT UPDATE (expires_at) ON
      sessions, oidc_states, email_login_challenges, identity_invitations,
      telegram_link_codes, telegram_update_admissions, message_attachments,
      messages TO hh_purge;

GRANT UPDATE (updated_at) ON conversations TO hh_purge;
GRANT UPDATE (status, finished_at, updated_at) ON turn_requests TO hh_purge;

GRANT SELECT ON alembic_version TO hh_purge;

-- ROL-12 - backup: SELECT on every table and sequence, and the marker.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO hh_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO hh_backup;
GRANT SELECT ON alembic_version TO hh_backup;

-- ROL-13 - provisioning: no DELETE, no sessions, no messages.
GRANT SELECT, INSERT, UPDATE ON users, allowlist_entries, identity_invitations TO hh_provision;
GRANT SELECT ON identity_links, alembic_version TO hh_provision;

-- ROL-14 - default privileges for future objects created by the owner. PostgreSQL applies a
-- GLOBAL default ACL (defaclnamespace = 0) and the schema-specific one TOGETHER, so a
-- schema-level REVOKE can never cancel a global grant. The four managed roles are therefore
-- converged globally first, then exactly in `public`. A grant to any OTHER recipient
-- (including PUBLIC) cannot be revoked without naming it, and is deliberately not removed
-- here: the verification below refuses that state (fail closed) so an unexpected
-- cluster-wide default is never silently accepted.
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner
    REVOKE ALL ON TABLES FROM hh_app, hh_purge, hh_backup, hh_provision;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner
    REVOKE ALL ON SEQUENCES FROM hh_app, hh_purge, hh_backup, hh_provision;

-- The obsolete purge default SELECT is removed; app and backup receive exactly their frozen
-- sets.
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
    REVOKE ALL ON TABLES FROM hh_app, hh_purge, hh_backup, hh_provision;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM hh_app, hh_purge, hh_backup, hh_provision;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hh_app;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO hh_app;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO hh_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO hh_backup;

-- 3. Verify the EFFECTIVE state. A failure aborts the whole transaction.
DO $hh_verify$
DECLARE
    relation record;
    column_row record;
    sequence_row record;
    allowed boolean;
    purge_select boolean;
    provision_select boolean;
    provision_write boolean;
    expected_set record;
    schema_owner text;
    public_schema_entries integer;
    observed_defaults text[];
    expected_defaults text[];
BEGIN
    -- 3a. Schema ownership and ACL.
    SELECT n.nspowner::regrole::text,
           (SELECT count(*) FROM aclexplode(n.nspacl) a WHERE a.grantee = 0)
      INTO schema_owner, public_schema_entries
      FROM pg_namespace n WHERE n.nspname = 'public';
    IF schema_owner <> 'hh_owner' OR public_schema_entries <> 0 THEN
        RAISE EXCEPTION 'the public schema is not owned by hh_owner with no PUBLIC entry';
    END IF;
    FOR expected_set IN
        SELECT * FROM (VALUES
            ('hh_app'), ('hh_purge'), ('hh_backup'),
            ('hh_provision'), ('hh_migrate'), ('hh_restore')
        ) AS t(name)
    LOOP
        IF has_schema_privilege(expected_set.name, 'public', 'USAGE') IS DISTINCT FROM true
           OR has_schema_privilege(expected_set.name, 'public', 'CREATE') IS DISTINCT FROM false THEN
            RAISE EXCEPTION 'schema privilege drift for %', expected_set.name;
        END IF;
    END LOOP;

    -- 3a2. PUBLIC holds nothing on a public-schema table, sequence or column. The catalog
    -- itself is inspected: `has_table_privilege()` does NOT consider a column-level grant,
    -- so a `GRANT SELECT (content) ... TO PUBLIC` would pass every table-level check.
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) a
        WHERE n.nspname = 'public' AND a.grantee = 0
    ) THEN
        RAISE EXCEPTION 'PUBLIC holds a table or sequence privilege in public';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_attribute att
        JOIN pg_class c ON c.oid = att.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(att.attacl) a
        WHERE n.nspname = 'public' AND att.attnum > 0 AND NOT att.attisdropped
          AND a.grantee = 0
    ) THEN
        RAISE EXCEPTION 'PUBLIC holds a column privilege in public';
    END IF;

    -- 3b. Table privileges, every application table compared.
    FOR relation IN
        SELECT c.oid, c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
         ORDER BY c.relname
    LOOP
        -- hh_app: all four DML privileges, except SELECT-only on alembic_version.
        IF relation.relname = 'alembic_version' THEN
            IF has_table_privilege('hh_app', relation.oid, 'SELECT') IS DISTINCT FROM true
               OR has_table_privilege('hh_app', relation.oid, 'INSERT') IS DISTINCT FROM false
               OR has_table_privilege('hh_app', relation.oid, 'UPDATE') IS DISTINCT FROM false
               OR has_table_privilege('hh_app', relation.oid, 'DELETE') IS DISTINCT FROM false THEN
                RAISE EXCEPTION 'hh_app privileges on alembic_version are not SELECT-only';
            END IF;
        ELSE
            IF has_table_privilege('hh_app', relation.oid, 'SELECT') IS DISTINCT FROM true
               OR has_table_privilege('hh_app', relation.oid, 'INSERT') IS DISTINCT FROM true
               OR has_table_privilege('hh_app', relation.oid, 'UPDATE') IS DISTINCT FROM true
               OR has_table_privilege('hh_app', relation.oid, 'DELETE') IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'hh_app is missing a DML privilege on %', relation.relname;
            END IF;
        END IF;
        IF has_table_privilege('hh_app', relation.oid, 'TRUNCATE') IS DISTINCT FROM false
           OR has_table_privilege('hh_app', relation.oid, 'REFERENCES') IS DISTINCT FROM false
           OR has_table_privilege('hh_app', relation.oid, 'TRIGGER') IS DISTINCT FROM false THEN
            RAISE EXCEPTION 'hh_app holds a forbidden privilege on %', relation.relname;
        END IF;

        -- hh_backup: SELECT only.
        IF has_table_privilege('hh_backup', relation.oid, 'SELECT') IS DISTINCT FROM true
           OR has_table_privilege('hh_backup', relation.oid, 'INSERT') IS DISTINCT FROM false
           OR has_table_privilege('hh_backup', relation.oid, 'UPDATE') IS DISTINCT FROM false
           OR has_table_privilege('hh_backup', relation.oid, 'DELETE') IS DISTINCT FROM false
           OR has_table_privilege('hh_backup', relation.oid, 'TRUNCATE') IS DISTINCT FROM false THEN
            RAISE EXCEPTION 'hh_backup is not SELECT-only on %', relation.relname;
        END IF;

        -- hh_purge: the ten retention tables (SELECT/DELETE) plus alembic_version SELECT.
        allowed := relation.relname IN (
            'sessions', 'oidc_states', 'email_login_challenges', 'identity_invitations',
            'telegram_link_codes', 'telegram_update_admissions', 'message_attachments',
            'messages', 'conversations', 'turn_requests');
        IF allowed THEN
            IF has_table_privilege('hh_purge', relation.oid, 'SELECT') IS DISTINCT FROM true
               OR has_table_privilege('hh_purge', relation.oid, 'DELETE') IS DISTINCT FROM true
               OR has_table_privilege('hh_purge', relation.oid, 'INSERT') IS DISTINCT FROM false
               OR has_table_privilege('hh_purge', relation.oid, 'UPDATE') IS DISTINCT FROM false
               OR has_table_privilege('hh_purge', relation.oid, 'TRUNCATE') IS DISTINCT FROM false THEN
                RAISE EXCEPTION 'hh_purge privileges on retention table % are not exact', relation.relname;
            END IF;
        ELSIF relation.relname = 'alembic_version' THEN
            IF has_table_privilege('hh_purge', relation.oid, 'SELECT') IS DISTINCT FROM true
               OR has_table_privilege('hh_purge', relation.oid, 'INSERT') IS DISTINCT FROM false
               OR has_table_privilege('hh_purge', relation.oid, 'UPDATE') IS DISTINCT FROM false
               OR has_table_privilege('hh_purge', relation.oid, 'DELETE') IS DISTINCT FROM false THEN
                RAISE EXCEPTION 'hh_purge privileges on alembic_version are not SELECT-only';
            END IF;
        ELSE
            IF has_table_privilege('hh_purge', relation.oid, 'SELECT') IS DISTINCT FROM false
               OR has_table_privilege('hh_purge', relation.oid, 'INSERT') IS DISTINCT FROM false
               OR has_table_privilege('hh_purge', relation.oid, 'UPDATE') IS DISTINCT FROM false
               OR has_table_privilege('hh_purge', relation.oid, 'DELETE') IS DISTINCT FROM false THEN
                RAISE EXCEPTION 'hh_purge can reach non-retention table %', relation.relname;
            END IF;
        END IF;

        -- hh_provision: the three provisioning tables, identity_links and the marker.
        IF relation.relname IN ('users', 'allowlist_entries', 'identity_invitations') THEN
            IF has_table_privilege('hh_provision', relation.oid, 'SELECT') IS DISTINCT FROM true
               OR has_table_privilege('hh_provision', relation.oid, 'INSERT') IS DISTINCT FROM true
               OR has_table_privilege('hh_provision', relation.oid, 'UPDATE') IS DISTINCT FROM true
               OR has_table_privilege('hh_provision', relation.oid, 'DELETE') IS DISTINCT FROM false THEN
                RAISE EXCEPTION 'hh_provision privileges on % are not exact', relation.relname;
            END IF;
        ELSIF relation.relname IN ('identity_links', 'alembic_version') THEN
            IF has_table_privilege('hh_provision', relation.oid, 'SELECT') IS DISTINCT FROM true
               OR has_table_privilege('hh_provision', relation.oid, 'INSERT') IS DISTINCT FROM false
               OR has_table_privilege('hh_provision', relation.oid, 'UPDATE') IS DISTINCT FROM false
               OR has_table_privilege('hh_provision', relation.oid, 'DELETE') IS DISTINCT FROM false THEN
                RAISE EXCEPTION 'hh_provision privileges on % are not SELECT-only', relation.relname;
            END IF;
        ELSE
            IF has_table_privilege('hh_provision', relation.oid, 'SELECT') IS DISTINCT FROM false
               OR has_table_privilege('hh_provision', relation.oid, 'INSERT') IS DISTINCT FROM false
               OR has_table_privilege('hh_provision', relation.oid, 'UPDATE') IS DISTINCT FROM false
               OR has_table_privilege('hh_provision', relation.oid, 'DELETE') IS DISTINCT FROM false THEN
                RAISE EXCEPTION 'hh_provision can reach table %', relation.relname;
            END IF;
        END IF;

        -- Effective COLUMN privileges for every role and every column. PostgreSQL has
        -- column privileges only for SELECT, INSERT, UPDATE and REFERENCES (DELETE and
        -- TRUNCATE are table-only). A table-level grant makes every column of that table
        -- available, and `has_column_privilege()` is the only inquiry that also sees a
        -- column-level grant, directly or through PUBLIC. hh_purge's UPDATE is the one
        -- column-scoped exception.
        purge_select := relation.relname IN (
            'sessions', 'oidc_states', 'email_login_challenges', 'identity_invitations',
            'telegram_link_codes', 'telegram_update_admissions', 'message_attachments',
            'messages', 'conversations', 'turn_requests') OR relation.relname = 'alembic_version';
        provision_select := relation.relname IN (
            'users', 'allowlist_entries', 'identity_invitations', 'identity_links',
            'alembic_version');
        provision_write := relation.relname IN (
            'users', 'allowlist_entries', 'identity_invitations');
        FOR column_row IN
            SELECT a.attname
              FROM pg_attribute a
             WHERE a.attrelid = relation.oid
               AND a.attnum > 0
               AND NOT a.attisdropped
        LOOP
            allowed := (relation.relname, column_row.attname) IN (
                ('sessions', 'expires_at'),
                ('oidc_states', 'expires_at'),
                ('email_login_challenges', 'expires_at'),
                ('identity_invitations', 'expires_at'),
                ('telegram_link_codes', 'expires_at'),
                ('telegram_update_admissions', 'expires_at'),
                ('message_attachments', 'expires_at'),
                ('messages', 'expires_at'),
                ('conversations', 'updated_at'),
                ('turn_requests', 'status'),
                ('turn_requests', 'finished_at'),
                ('turn_requests', 'updated_at'));
            IF has_column_privilege('hh_app', relation.oid, column_row.attname, 'SELECT')
                   IS DISTINCT FROM true
               OR has_column_privilege('hh_app', relation.oid, column_row.attname, 'INSERT')
                   IS DISTINCT FROM (relation.relname <> 'alembic_version')
               OR has_column_privilege('hh_app', relation.oid, column_row.attname, 'UPDATE')
                   IS DISTINCT FROM (relation.relname <> 'alembic_version')
               OR has_column_privilege('hh_app', relation.oid, column_row.attname, 'REFERENCES')
                   IS DISTINCT FROM false
            THEN
                RAISE EXCEPTION 'hh_app effective column privilege drift on %.%',
                    relation.relname, column_row.attname;
            END IF;
            IF has_column_privilege('hh_backup', relation.oid, column_row.attname, 'SELECT')
                   IS DISTINCT FROM true
               OR has_column_privilege('hh_backup', relation.oid, column_row.attname, 'INSERT')
                   IS DISTINCT FROM false
               OR has_column_privilege('hh_backup', relation.oid, column_row.attname, 'UPDATE')
                   IS DISTINCT FROM false
               OR has_column_privilege('hh_backup', relation.oid, column_row.attname, 'REFERENCES')
                   IS DISTINCT FROM false
            THEN
                RAISE EXCEPTION 'hh_backup effective column privilege drift on %.%',
                    relation.relname, column_row.attname;
            END IF;
            IF has_column_privilege('hh_purge', relation.oid, column_row.attname, 'SELECT')
                   IS DISTINCT FROM purge_select
               OR has_column_privilege('hh_purge', relation.oid, column_row.attname, 'INSERT')
                   IS DISTINCT FROM false
               OR has_column_privilege('hh_purge', relation.oid, column_row.attname, 'UPDATE')
                   IS DISTINCT FROM allowed
               OR has_column_privilege('hh_purge', relation.oid, column_row.attname, 'REFERENCES')
                   IS DISTINCT FROM false
            THEN
                RAISE EXCEPTION 'hh_purge effective column privilege drift on %.%',
                    relation.relname, column_row.attname;
            END IF;
            IF has_column_privilege('hh_provision', relation.oid, column_row.attname, 'SELECT')
                   IS DISTINCT FROM provision_select
               OR has_column_privilege('hh_provision', relation.oid, column_row.attname, 'INSERT')
                   IS DISTINCT FROM provision_write
               OR has_column_privilege('hh_provision', relation.oid, column_row.attname, 'UPDATE')
                   IS DISTINCT FROM provision_write
               OR has_column_privilege('hh_provision', relation.oid, column_row.attname, 'REFERENCES')
                   IS DISTINCT FROM false
            THEN
                RAISE EXCEPTION 'hh_provision effective column privilege drift on %.%',
                    relation.relname, column_row.attname;
            END IF;
        END LOOP;
    END LOOP;

    -- 3c. Sequence privileges.
    FOR sequence_row IN
        SELECT c.oid, c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind = 'S'
    LOOP
        IF has_sequence_privilege('hh_app', sequence_row.oid, 'USAGE') IS DISTINCT FROM true
           OR has_sequence_privilege('hh_app', sequence_row.oid, 'SELECT') IS DISTINCT FROM false
           OR has_sequence_privilege('hh_app', sequence_row.oid, 'UPDATE') IS DISTINCT FROM false THEN
            RAISE EXCEPTION 'hh_app sequence privilege drift on %', sequence_row.relname;
        END IF;
        IF has_sequence_privilege('hh_backup', sequence_row.oid, 'SELECT') IS DISTINCT FROM true
           OR has_sequence_privilege('hh_backup', sequence_row.oid, 'USAGE') IS DISTINCT FROM false
           OR has_sequence_privilege('hh_backup', sequence_row.oid, 'UPDATE') IS DISTINCT FROM false THEN
            RAISE EXCEPTION 'hh_backup sequence privilege drift on %', sequence_row.relname;
        END IF;
        IF has_sequence_privilege('hh_purge', sequence_row.oid, 'SELECT') IS DISTINCT FROM false
           OR has_sequence_privilege('hh_purge', sequence_row.oid, 'USAGE') IS DISTINCT FROM false
           OR has_sequence_privilege('hh_purge', sequence_row.oid, 'UPDATE') IS DISTINCT FROM false THEN
            RAISE EXCEPTION 'hh_purge holds a sequence privilege on %', sequence_row.relname;
        END IF;
        IF has_sequence_privilege('hh_provision', sequence_row.oid, 'SELECT') IS DISTINCT FROM false
           OR has_sequence_privilege('hh_provision', sequence_row.oid, 'USAGE') IS DISTINCT FROM false
           OR has_sequence_privilege('hh_provision', sequence_row.oid, 'UPDATE') IS DISTINCT FROM false THEN
            RAISE EXCEPTION 'hh_provision holds a sequence privilege on %', sequence_row.relname;
        END IF;
    END LOOP;

    -- 3c2. No project role other than the owner holds a grant option on a public-schema
    -- table, sequence or column. The owner's own entries carry grant options inherently.
    IF EXISTS (
        SELECT 1
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          CROSS JOIN LATERAL aclexplode(c.relacl) a
          JOIN pg_roles g ON g.oid = a.grantee
         WHERE n.nspname = 'public' AND a.is_grantable
           AND g.rolname IN ('hh_migrate', 'hh_app', 'hh_purge', 'hh_backup',
                             'hh_provision', 'hh_restore')
    ) OR EXISTS (
        SELECT 1
          FROM pg_attribute att
          JOIN pg_class c ON c.oid = att.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          CROSS JOIN LATERAL aclexplode(att.attacl) a
          JOIN pg_roles g ON g.oid = a.grantee
         WHERE n.nspname = 'public' AND att.attnum > 0 AND NOT att.attisdropped
           AND a.is_grantable
           AND g.rolname IN ('hh_migrate', 'hh_app', 'hh_purge', 'hh_backup',
                             'hh_provision', 'hh_restore')
    ) THEN
        RAISE EXCEPTION 'a project role holds an unauthorized grant option';
    END IF;

    -- 3d. Default ACLs: the COMPLETE hh_owner set - GLOBAL (defaclnamespace = 0) and
    -- schema-specific - compared entry by entry. Both kinds apply to a new object, so a
    -- schema-level REVOKE can never cancel a global grant. The four managed roles are
    -- converged above; any OTHER recipient (an unrelated role or PUBLIC) cannot be revoked
    -- without naming it, and is refused here (fail closed). The owner's own entries are
    -- PostgreSQL's, not the contract's.
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
END
$hh_verify$;
