-- HH-DB-01 Stage 2 WP-1B - E3 creation of a serving or restore database.
--
-- Frozen source: §5.1 E3 (BOOT-01/BOOT-02/BOOT-06), §3.4 ROL-09, §8.8 RST-04.
--
-- This file is a PHASE-SEPARATED script, not a runnable whole:
--
--   tests/postgres_sql.py (`BootstrapScript`) parses the `-- @phase:` markers, substitutes
--   `{{database}}` and `{{grantee}}` only after validating both against the fixed
--   allowlists, and enforces the boundary below. It never sends BEGIN or a transaction
--   wrapper around the CREATE.
--
--   * create - ONE statement, sent in autocommit and alone. `CREATE DATABASE` cannot run
--     inside a transaction block or share a query message (SQLSTATE 25001).
--   * harden - PUBLIC loses everything, the single intended login gets CONNECT. These are
--     sent while the database is still DISABLED (`ALLOW_CONNECTIONS false`).
--   * verify - not SQL in this file: the runner reads `pg_database`/`aclexplode` and
--     refuses to continue unless E3-b holds: connections disabled, no PUBLIC entry, and
--     CONNECT exactly for the grantee. Enabling is never reached on a failed check.
--   * enable - ONE statement, sent after the E3-b verification; the runner then re-reads
--     the catalog as E3-c and treats any PUBLIC entry as fail-closed.
--
-- Parameters (validated by the runner before any rendering):
--   {{database}} - `house_helper` or `house_helper_restore_<token>` with
--                  <token> matching [a-z0-9]{1,32}
--   {{grantee}}  - `hh_migrate` (serving) or `hh_restore` (restore database) only
--
-- Fixed facts: owner `hh_owner`, TEMPLATE template0, UTF8, libc provider, and the
-- Owner-approved C/C locale (WP-1A evidence: C is the only setting on this image whose
-- `lower()` agrees with SQLite's ASCII-only rule).

-- @phase: create
CREATE DATABASE {{database}}
    OWNER hh_owner
    TEMPLATE template0
    ENCODING 'UTF8'
    LC_COLLATE 'C'
    LC_CTYPE 'C'
    ALLOW_CONNECTIONS false;
-- E3-a follows: datallowconn = false. Default PUBLIC privileges may still be present
-- here and only here; no connection is attempted and no schema work occurs.

-- @phase: harden
REVOKE ALL ON DATABASE {{database}} FROM PUBLIC;
GRANT CONNECT ON DATABASE {{database}} TO {{grantee}};
-- hh_owner keeps its inherent owner rights; no other role is granted anything.

-- @phase: enable
ALTER DATABASE {{database}} ALLOW_CONNECTIONS true;
