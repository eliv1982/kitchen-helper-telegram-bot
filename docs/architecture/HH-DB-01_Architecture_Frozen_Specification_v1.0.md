# HH-DB-01 - House Helper PostgreSQL Cutover
## Architecture Frozen Specification v1.0

**Status:** **FROZEN SPECIFICATION v1.0 - DOCUMENTATION D2 REMEDIATED / CODEX PASS (R2.4 ARCHITECTURE, D2 DOCUMENTATION) / OWNER ACCEPT (2026-11-14) / IMPLEMENTATION NOT STARTED**
**Version:** 1.0
**Date:** 2026-11-14
**Repository:** `eliv1982/kitchen-helper-telegram-bot`
**Verified architecture baseline:** `3fec30f` (HEAD of `main`) · Alembic migrations `0001-0014` · 20 application tables · 28 indexes
**Consolidates:** Architecture R2 (Clean-Slate PostgreSQL Cutover), R2.1 (Targeted Remediation), R2.2 (Bootstrap, Restore and Sequence Corrections), R2.3 (State-Transition and Parser Addendum), R2.4 (Final Bootstrap Fence and Role-Check Amendment), the independent Codex Final R2.4 Delta Review **PASS**, and its four approved nonblocking documentation errata.
**Binding Owner decision:** `HH-DB-01-DATA-RESET-01` - existing SQLite application data is abandoned; two accounts are recreated; no ETL; Production is not modified by this specification.

### Evidence and status legend

| Mark | Meaning |
|---|---|
| `[T]` | Verified in this repository at `3fec30f` during the R2 analysis. |
| `[D]` | Documented in the repository, not re-executed for this specification. |
| `[P]` | Design proposal that is now architecture-frozen by acceptance, but not implemented. |
| `[U]` | Unverified Production assumption requiring operational evidence. |
| `[O]` | Genuine Owner decision. Listed in §12; never silently treated as approved. |
| `[SPEC]` | A test or acceptance check that is **specified but not executed**. |

### Status of this document - read this before relying on anything below

1. **Architecture acceptance is not implementation evidence.** The design is accepted by the reported independent Codex Final R2.4 delta review (PASS; see Evidence provenance below). **No implementation exists for this architecture at the baseline.** No role, database, container, timer, credential, archive or test in this document has been created or run.
2. **No test was executed for this specification.** Every test, fixture and acceptance check is marked `[SPEC]`. Nothing here is a reported test result.
3. **No Production operation was performed.** Production remains untouched: no cutover, no migration, no container change, no credential change, no backup, no deletion. All Production statements are prerequisites or `[U]` assumptions, not evidence.
4. **This is consolidation, not redesign.** Where this document and an earlier amendment differ, §14 names the earlier text as superseded. No new architectural decision was introduced by this consolidation, except the four explicitly listed errata (§14.4), which were already approved by the R2.4 PASS.

### Evidence provenance

This subsection records how the evidence behind this specification was obtained. It is deliberately explicit about what was **not** done.

1. **Source recovery.** The full R2, R2.1, R2.2, R2.3 and R2.4 source documents were recovered **read-only from a prior-session transcript** (`89053e6c-*`), not from repository files. No R2-R2.4 document exists in the repository or under `docs/`. Recovery was a mechanical text extraction, not a source-fidelity review.
2. **PASS and errata supply.** The final Codex R2.4 Architecture PASS and its four Freeze Errata were supplied through the consolidation task brief. The consolidating agent did **not** independently verify the original PASS artifact.
3. **Not performed.** The consolidating agent did **not** perform a complete historical source-fidelity comparison between R2-R2.4 and this specification. The consolidation applied the explicit supersession clauses stated in the source documents and the task brief.
4. **Documentation review.** The subsequent independent Documentation Acceptance Review (D1) examined the **current consolidated file**. It did **not** independently compare all original source documents either. Its findings are documentation-consistency findings, not a source-fidelity verification.
5. **Distinct evidence claims.** Architecture PASS is a reported and accepted project decision. Documentation source-fidelity verification, Owner acceptance and implementation testing are **separate** claims. None of them is implied by another, and none beyond what is written here is claimed.

### Normative language

- **MUST / MUST NOT** - a binding requirement of the frozen architecture. An implementation that violates it is non-conforming.
- **SHOULD** - the accepted default; a deviation requires an explicit Owner decision recorded in the journal and in this document's successor.
- Every normative statement carries an identifier (`ROL-xx`, `BOOT-xx`, `SEQ-xx`, ...). Acceptance gates in §11 cite them.
- **Exactly one interpretation.** Where two readings seem possible, §14.2 (supersession matrix) and §14.3 (withdrawn statements register) decide. A withdrawn statement is not a permissible alternative reading.

---

## 1. Scope, binding Owner decisions and exclusions

### 1.1 Purpose

`HH-DB-01` replaces the deployed SQLite store with a dedicated PostgreSQL 16 database for the two-user House Helper deployment, using a **clean-slate initialization**: the application's complete Alembic chain is executed against an empty database; no historical application data is migrated, converted or preserved in the target.

**GOV-01.** There is no ETL, no import step, no source-revision strategy and no data reconciliation from SQLite. The live SQLite revision is irrelevant to the target. It matters only to the frozen recovery pair and the preserved old image (GOV-05).

**GOV-02.** The two existing accounts are recreated through the product's supported invitation and Telegram OIDC flows (§5.4). Account recreation is not a row insertion operation.

**GOV-03.** The target is one dedicated PostgreSQL 16 instance in a dedicated container on a private database network (§2).

**GOV-04.** Email login remains disabled. No email-auth redesign is part of this work. `provision-email` cannot independently enroll both users into a two-user database and MUST NOT be used as the two-user bootstrap path.

**GOV-05.** Existing SQLite recovery assets are preserved pending a later retention decision: the historical `house-helper-recovery-point/1` archives, the pre-cutover `NAME.db` + `NAME.db.sha256` pair, and the matching old image and old `deploy/` checkout. None is converted, deleted or rewritten by this architecture.

**GOV-06.** Production remains untouched until a separately authorized cutover (§12). Reading Production for an inventory is itself a separate authorization.

### 1.2 In scope

- PostgreSQL 16 topology, container, volume and network isolation.
- Cluster enrollment, identity verification, resume and identity-update operations.
- Six runtime roles plus one `NOLOGIN` owner; privileges; authentication; negative security tests.
- Fresh schema initialization by executing `0001-0014`; fresh two-account bootstrap.
- Telegram cutover, the update router's scoping, and pending-update constraints.
- Maintenance locks, fencing, permission handoffs and the durable journal.
- PostgreSQL backup format v2, archive-derived sequence consistency, restore identity and isolated verification.
- Conservative rollback and recovery contracts.
- PostgreSQL-first development and CI.
- Nine implementation stages and their acceptance gates.
- Residual risks and the supersession/traceability record.

### 1.3 Out of scope and explicit exclusions

**GOV-07.** No architectural redesign of the application, its identity model, its conversation model or its Telegram transport. The only application-level adaptations are those named in §5.4 (`dbtool` target handling, per-role one-shot services, start-gate assertions).

**GOV-08.** No new feature scope. No new table, column, migration or product capability is designed here. The migration chain remains exactly `0001-0014`; this document adds no migration.

**GOV-09.** `KH-F01-B2-DYN-01` remains **PAUSED**. No DYN table is designed, merged or compared under this work. The deferred comparison, when it is resumed, is specifically `reference_identity_events` vs `reference_cache_events`. `telegram_update_admissions` is a different object and is **not** a merging candidate.

**GOV-10.** No row-level security at cutover. The financial-data access-isolation gate before `HH-BUDGET-01` remains **mandatory and deferred, not waived** (§3.10).

**GOV-11.** No new architecture audit and no re-litigation of accepted blockers. B1, B2, B3, B5 and B6 are accepted in design; R2.4 did not reopen them, and this specification preserves them.

**GOV-12.** TLS on the private database network is an **open Owner choice (OD-6, §12)**. This specification does not select it or impose the recommended "no TLS" option as an approved decision. Until the Owner decides, the unencrypted internal bridge is a stated residual risk (RSK-08), not an architectural choice.

**GOV-13.** No point-in-time recovery, no logical replication, no cluster-role backup, no `pg_upgrade` procedure. PostgreSQL major-version change is a replacement cluster, never an in-place operation (§4.6, ENR-19).

### 1.4 Protected paths and repository conduct

**GOV-14.** This work does not open, edit or stage `.claude/`, `deploy/monitoring/` or `docs/product/KH-F01B_Reference_Data_Baseline_2026-11-11.md`. All untracked B2/DYN drafts are preserved. There is no `git add .`, no `git clean`, no commit, push or deployment as part of producing this specification.

---

## 2. System architecture and infrastructure isolation

### 2.1 Component inventory

| Component | Verified today `[T]` | Frozen target `[P]` |
|---|---|---|
| Edge | Shared Traefik on `n8n_n8n_network`, four routers with explicit priorities, no published application port. | Unchanged. |
| Web | Single uvicorn worker. FastAPI lifespan opens no connection and does not migrate. `/healthz` is liveness-only and touches no database. | Unchanged except the DSN. |
| Telegram | Long polling **inside** the web process. One consumer per token; a second consumer gets `409`. | Unchanged. |
| Database | SQLite file on the `house-helper-data` volume; WAL; `HOUSE_HELPER_DATABASE_URL` set literally under `environment:` in `compose.yaml`. | `postgres:16.x` digest-pinned container, dedicated named volume, `internal: true` network, **no `ports:`**. |
| Operator tooling | `house-helper-ops`: same image and volume, `network_mode: none`, no env file, `dbtool` entrypoint, no log driver. | One one-shot service **per role** (`migrate`, `provision`, `purge`, `backup`, `restore`), each attached to the database network, each receiving **only its own** credential material. |
| Scheduling | Two systemd units and timers (`Persistent=true`), host-side. | Same shape; targets change to the matching one-shot service. |
| Offsite | Host Python + `age` + `rclone`; recovery-point format v1 (`MANIFEST.json`, `snapshot.db.sha256`, `snapshot.db`). | Format v2 for PostgreSQL; the v1 reader is retained forever. |

**ARC-01.** The application container's `environment` names exactly **one** DSN. After cutover it MUST NOT mount the SQLite volume. The old stack is stopped before the new one starts. No configuration is permitted in which both databases are writable by the application.

### 2.2 Network topology

| Network | Members | Purpose |
|---|---|---|
| `n8n_n8n_network` (existing bridge) | Traefik, the web service | Ingress only. |
| `house-helper-db` (**`internal: true`**) | PostgreSQL, the web service, each per-role one-shot **while it runs** | Database traffic only. No route to the internet, no published port. |

**ARC-02.** Only the web service joins both networks. **No one-shot joins the proxy network.** No one-shot publishes a port. The PostgreSQL service publishes no port at all; it is reachable only by container name on `house-helper-db`.

**ARC-03.** A preflight MUST refuse to start if a runtime service's env file contains `HOUSE_HELPER_DATABASE_URL`. The DSN has exactly one source per service: a literal in `compose.yaml`.

The following credential controls are **binding**, independent of the mechanism the Owner later selects:

- no database password is embedded in the application DSN;
- no credentials appear in committed configuration;
- privileged credentials are never shared between runtime roles;
- each service receives only the credentials required by its designated role;
- credential material is access-controlled, non-public and excluded from unsafe logs or environment dumps (AUTH-04);
- PostgreSQL CLI tooling uses a valid libpq-compatible authentication mechanism (AUTH-03).

The **concrete credential source and placement for the production password is an open Owner choice (OD-8, §12.6)**. This specification approves **neither** `PGPASSWORD` **nor** `PGPASSFILE`: root-controlled per-role files, `PGPASSFILE`, environment-based injection or another mechanism that satisfies the binding controls above are all permitted, and the production deployment and rotation procedure follows the same decision.

**ARC-04.** No credential, password or DSN secret may appear on a command line (`ps` visibility), in a URL query string, in a log line, in `docker inspect` output that is echoed, or in the journal.

### 2.3 Trust boundaries

| # | Boundary | What crosses it | Control |
|---|---|---|---|
| B1 | Internet -> Traefik -> app | Untrusted HTTP; OIDC callback `code`/`state`. | Application auth, CSP, rate limits, no access log of callback query strings. |
| B2 | App process -> database | SQL as one shared login. | `hh_app`: DML only, no DDL, `USAGE` on sequences, `SELECT` on `alembic_version`. |
| B3 | Operator -> database | DDL, provisioning, purge, dump, restore. | One login per authority (§3); superuser only through the container's local socket. |
| B4 | Host -> Docker | Root-equivalent. | Out of scope: stated, not solved. |
| B5 | App -> Telegram / provider | Message text, tokens. | Existing redaction; the bot token is never logged. |
| B6 | Host -> offsite | Encrypted archive plus public key only. | `age` recipients; the private identity never reaches the host. |
| B7 | Frozen SQLite -> recovery | Old pair plus matching old image. | The volume is not mounted by the new stack; read-only archive. |

### 2.4 Verified baseline facts `[T]`

These are facts about the baseline, not proposals. An implementation MUST verify them again rather than assume they persist.

| # | Fact |
|---|---|
| ARC-05 | Migration head is `0014_user_measure_overrides`; 20 application tables; 28 indexes (25 plain, 3 unique); `messages.id` is the only sequence (`BIGSERIAL`). |
| ARC-06 | `psycopg` is absent from `requirements.txt`; the opt-in PostgreSQL tests skip without it. |
| ARC-07 | `dbtool.py` refuses any non-SQLite URL; the web start gate depends on `dbtool`. |
| ARC-08 | `compose.yaml` sets `HOUSE_HELPER_DATABASE_URL: sqlite:////data/house_helper.db` under `environment:`, which overrides `env_file`. |
| ARC-09 | `house-helper-ops` has `network_mode: none`, no env file and no log driver. |
| ARC-10 | `backup.sh`, `dbtool backup/verify/restore/sanitize` and both offsite tools process SQLite only. |
| ARC-11 | PostgreSQL-aware code already exists where it matters: `pg_advisory_xact_lock` in provisioning/enrollment; `FOR UPDATE SKIP LOCKED` and isolation-level handling in retention; `FOR SHARE` / `FOR NO KEY UPDATE` in allowlist, invitations and Telegram. |
| ARC-12 | The opt-in PostgreSQL harness refuses port `5432`, non-loopback hosts and **any** query parameter. |
| ARC-13 | CI has no PostgreSQL service and installs `requirements-dev.txt` only. |
| ARC-14 | The Telegram admission ledger is implemented and tested: the cutover row is written once per bot at first preparation, from the process start instant; the poller reads nothing until the ledger is READY; an update dated before the cutover that no marker or turn request knows is refused with the fixed "pre-migration" sentence. |
| ARC-15 | `ENROLLMENT_USER_CAP = 2`; `enroll-user` creates a user plus an invitation; claiming an invitation creates an `allowlist_entries` row, an `identity_links` row and a session in one transaction; bot pairing is a separate step (`POST /api/telegram/link` -> `/start <code>`). |
| ARC-16 | The web lifespan opens no connection until first use; `check_schema_at_head` reads `alembic_version` and is used by the admin CLI and the start gate. |
| ARC-17 | No `alembic stamp` exists anywhere in the repository. The chain is executed, never declared. |
| ARC-18 | `0002`, `0004` and `0010` contain one-shot data steps. On an empty fresh database they are no-ops. |
| ARC-19 | `0013` and `0014` carry explicit SQLite/PostgreSQL dialect branches; `0004` also has one. |

**ARC-20.** Unverified Production assumptions `[U]` that MUST be measured before a cutover window: running image tag and SQLite revision; active timers/units; free disk and memory; absence of a legacy `bot.py` process; absence of an n8n Telegram trigger using the same token; availability of both users to re-enroll; VPS PostgreSQL performance.

---

## 3. PostgreSQL roles, authentication, privileges and security boundaries

### 3.1 Database and schema

**ROL-01.** One dedicated database `house_helper`, owned by `hh_owner`, with the hardened `public` schema. Reasons: Alembic's `alembic_version` lands in the default schema without extra configuration; the database is dedicated, so a custom schema buys no isolation; the models and migrations use unqualified names `[T]`, so a custom schema would require coordinated `search_path` for seven identities and would add a "created in the wrong schema" failure mode.

**ROL-02.** Creation parameters: `TEMPLATE template0`, `ENCODING 'UTF8'`, explicit validated `LC_COLLATE` and `LC_CTYPE`. The identity CHECK constraints call `lower()`; the chosen collation/ctype MUST NOT fold differently from SQLite's ASCII-only `lower()`. A conformance fixture over the existing identity value matrix is the acceptance check for this requirement, verified in stage 2 (§11).

### 3.2 Role matrix

| Role | Login | Attributes | Authority | Used only by |
|---|---|---|---|---|
| *(bootstrap superuser)* | Local socket only (`local`/`peer`) | `SUPERUSER` | Cluster: create roles and databases, `ALTER SCHEMA public OWNER`, initial `REVOKE`. | `docker exec ... psql` from the host. Never a container credential, never the app. |
| `hh_owner` | **NOLOGIN** | No attributes | Owns the database, schema and every object. | Target of `SET ROLE` only. |
| `hh_migrate` | LOGIN | `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOBYPASSRLS`; member of `hh_owner` `WITH SET TRUE, INHERIT FALSE`; `ALTER ROLE ... SET role = hh_owner` | DDL: `alembic upgrade head`; grants on existing objects. | Migration one-shot. |
| `hh_app` | LOGIN | `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOBYPASSRLS`, `NOINHERIT` | DML on all 20 application tables; `USAGE` on sequences; `SELECT` on `alembic_version`; **no** DDL, `TRUNCATE`, `REFERENCES`, `TRIGGER` or `TEMPORARY`. | Web process and its in-process Telegram poller. |
| `hh_purge` | LOGIN | As `hh_app` | `SELECT` + `DELETE` on the ten retention tables; exact column-level `UPDATE`; `SELECT` on `alembic_version`. **No default privilege.** | Purge one-shot. |
| `hh_backup` | LOGIN | As `hh_app` | `SELECT` on all tables **and all sequences**; `SELECT` on `alembic_version`. **Not** a member of `pg_read_all_data`. | `pg_dump`. |
| `hh_provision` | LOGIN | As `hh_app` | `SELECT`/`INSERT`/`UPDATE` on `users`, `allowlist_entries`, `identity_invitations`; `SELECT` on `identity_links` and `alembic_version`; **no `DELETE`**. | `enroll-user`, `reissue`, `revoke`, `status`. |
| `hh_restore` | LOGIN | `NOSUPERUSER`, **`CREATEDB`**, `NOBYPASSRLS`; member of `hh_owner` `WITH SET TRUE, INHERIT FALSE` | Create a new database owned by `hh_owner`; `pg_restore` as effective `hh_owner`; apply the ACL contract. | Recovery only; credential stored offline. |

**ROL-03.** `hh_owner` is `NOLOGIN` so that "the owner" is a privilege target, never a service identity. Only `hh_migrate` and `hh_restore` assume it, with `SET ROLE` (or the configured default role), so objects stay owned by `hh_owner` and `session_user` records the real login.

**ROL-04.** The seven project roles are exactly `hh_owner`, `hh_migrate`, `hh_app`, `hh_purge`, `hh_backup`, `hh_provision`, `hh_restore`. No other project-named role is created.

### 3.3 Membership, `SET ROLE` and session settings

**ROL-05.** `SET ROLE` changes `current_user`. It does **not** apply the target role's `ALTER ROLE ... SET` defaults, because `rolconfig` is applied at session establishment for the session user. No migration, restore or maintenance step may rely on `hh_owner`'s configured settings after assuming it.

**ROL-06.** Before any unqualified SQL after assuming `hh_owner`, the session MUST set and then verify:

```sql
SET search_path = public;              -- pg_catalog remains implicitly first
SET application_name = 'hh_migrate';   -- or 'hh_restore'
SET statement_timeout = 0;             -- migrations/restore only

SELECT session_user, current_user, current_database(),
       current_setting('search_path'),
       (SELECT nspname FROM pg_namespace WHERE oid = 'public'::regnamespace);
```

Required: the `(session_user, current_user)` pair of the applicable phase matrix (§8.8), the intended `current_database()`, `search_path = public`, and `public` as the only non-system schema.

**ROL-07.** `pg_restore` is exempt from ROL-06: it emits independently qualified SQL and its own `SET` statements. The restore script's own pre/post checks still set and verify `search_path`; it never assumes what `pg_restore` left behind.

**ROL-08.** `password_encryption = scram-sha-256` is confirmed at bootstrap. A role whose verifier was set under `md5` is reset, not trusted.

### 3.4 Privilege contract

This section is the single authoritative privilege contract. It incorporates Freeze Erratum FE-2: default table `SELECT` belongs to `hh_backup` only; `hh_purge` receives its explicit ten-table ACL including column-level `UPDATE` and its `alembic_version` `SELECT` exception.

**ROL-09 - bootstrap database and schema (executed by the bootstrap superuser / effective `hh_owner`, with connections disabled; see §5.1):**

```sql
CREATE DATABASE house_helper
    OWNER hh_owner TEMPLATE template0
    ENCODING 'UTF8' LC_COLLATE '<validated>' LC_CTYPE '<validated>'
    ALLOW_CONNECTIONS false;

REVOKE ALL ON DATABASE house_helper FROM PUBLIC;          -- CONNECT and TEMPORARY
GRANT CONNECT ON DATABASE house_helper TO hh_migrate;     -- the only pre-handoff login
-- hh_restore receives NO CONNECT on house_helper (see RST-03)

ALTER DATABASE house_helper ALLOW_CONNECTIONS true;       -- only after ACL verification

-- after reconnecting as hh_migrate and assuming hh_owner:
SET search_path = public;
ALTER SCHEMA public OWNER TO hh_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;                  -- no CREATE, no USAGE
GRANT USAGE ON SCHEMA public TO
      hh_app, hh_purge, hh_backup, hh_provision, hh_migrate, hh_restore;
```

**ROL-10 - application privileges (`db/privileges.sql`, idempotent, re-applied after every migration):**

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hh_app;
REVOKE ALL ON alembic_version FROM hh_app;
GRANT SELECT ON alembic_version TO hh_app;                -- the start gate reads it
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO hh_app;  -- nextval/currval only
```

**ROL-11 - purge: exactly the ten tables and the columns it locks or writes.** `SELECT ... FOR UPDATE` requires `UPDATE` privilege on at least one column of the locked table, so table `SELECT` and `DELETE` are not sufficient.

```sql
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

GRANT SELECT ON alembic_version TO hh_purge;   -- check_schema_at_head
```

No `INSERT`, no other column, no other table, no sequence, no DDL, no `TRUNCATE`.

**ROL-12 - backup:**

```sql
GRANT SELECT ON ALL TABLES    IN SCHEMA public TO hh_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO hh_backup;
GRANT SELECT ON alembic_version TO hh_backup;
```

`pg_dump` needs `SELECT` on tables and on sequences (it reads sequence state). `hh_backup` holds no predefined role.

**ROL-13 - provisioning:**

```sql
GRANT SELECT, INSERT, UPDATE ON users, allowlist_entries, identity_invitations TO hh_provision;
GRANT SELECT ON identity_links, alembic_version TO hh_provision;
```

**ROL-14 - default privileges for future objects created by `hh_owner` (final form; the obsolete `hh_purge` default `SELECT` is removed):**

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hh_app;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
  GRANT USAGE ON SEQUENCES TO hh_app;
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO hh_backup;          -- backup only; never hh_purge
ALTER DEFAULT PRIVILEGES FOR ROLE hh_owner IN SCHEMA public
  GRANT SELECT ON SEQUENCES TO hh_backup;
```

**ROL-15.** A future retention table requires an explicit new grant to `hh_purge`, deliberately. It does not inherit purge access. This is tested by comparing the effective privilege matrix for every table in `pg_class` against the expectation frozen by ROL-11 and §3.6 negative test 13.

**ROL-16 - connection grants are handoffs, not bootstrap.** `hh_app`, `hh_provision`, `hh_purge` and `hh_backup` receive `CONNECT` only at their individually authorized handoff (§7.3 H1/H2/H3):

```sql
GRANT CONNECT ON DATABASE house_helper TO hh_app;                 -- H1
GRANT CONNECT ON DATABASE house_helper TO hh_provision;           -- H2
GRANT CONNECT ON DATABASE house_helper TO hh_purge, hh_backup;    -- H3
```

**ROL-17 - per-role session defaults (set for the login itself, verified at connection time):**

```sql
ALTER ROLE hh_app SET search_path = public;
ALTER ROLE hh_app SET statement_timeout = '30s';
ALTER ROLE hh_app SET lock_timeout = '5s';
ALTER ROLE hh_app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE hh_app SET application_name = 'hh_web';

ALTER ROLE hh_purge     SET application_name = 'hh_purge';
ALTER ROLE hh_backup    SET application_name = 'hh_backup';
ALTER ROLE hh_provision SET application_name = 'hh_provision';
ALTER ROLE hh_migrate   SET application_name = 'hh_migrate';
```

The application also sets `application_name` through the engine (`connect_args`), not in the URL, so the DSN stays clean and the test harness's "no query parameters" rule is untouched.

**ROL-18.** `TEMPORARY` is granted to no runtime role. `search_path = public` is pinned per role in the database; with `TEMPORARY` revoked, no role can create a temporary object to shadow an application relation.

### 3.5 What each existing command actually needs `[T]`

| Command | Reads | Writes | Additional authority |
|---|---|---|---|
| `python -m house_helper.admin enroll-user / reissue / revoke / status` | `users`, `allowlist_entries`, `identity_invitations`, `alembic_version` | `users`, `identity_invitations` | `pg_advisory_xact_lock` (no privilege). |
| `purge-expired` | The ten retention tables, `alembic_version` | `DELETE`; `UPDATE turn_requests` | `FOR UPDATE SKIP LOCKED` needs the column `UPDATE` of ROL-11; isolation levels are set by the tool. |
| `pg_dump` | All tables and sequences | - | No `pg_read_all_data`. |
| `alembic upgrade head` | `alembic_version` | DDL | Owner membership. |
| Restore | - | New database, objects | `CREATEDB`, `SET` membership, explicit `--role`. |

**ROL-19.** The provisioning commands work with the restricted role only if `alembic_version` is readable and the advisory-lock path is used. Both are already true in code `[T]`; they become explicit grants and negative tests.

### 3.6 Positive and negative ACL tests (real PostgreSQL 16) `[SPEC]`

**Positive** - each in a transaction rolled back after the assertion:

1. `hh_app`: `SELECT`/`INSERT`/`UPDATE`/`DELETE` on each of the 20 application tables.
2. `hh_app`: `INSERT INTO messages ...` succeeds and consumes the `messages` sequence; `currval` returns the id.
3. `hh_app`: `SELECT * FROM alembic_version` succeeds.
4. `hh_purge`: for **each of the ten tables**, `SELECT <pk> ... LIMIT 1 FOR UPDATE SKIP LOCKED` succeeds; the `DELETE ... WHERE <pk> IN (...)` form succeeds; the composite-key form succeeds for `telegram_update_admissions`.
5. `hh_purge`: the `turn_requests` cancel `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)` succeeds, setting exactly `status`, `finished_at`, `updated_at`.
6. `hh_purge`: a full `purge-expired` run and a `--dry-run` succeed, leaving other rows untouched; two concurrent `SKIP LOCKED` runs delete disjoint sets without blocking.
7. `hh_purge`: the FK cascade from `messages` to `message_recipes` works under the purge role (RI triggers run with the owner's rights - proved, not assumed).
8. `hh_backup`: `pg_dump -Fc` completes; `SELECT last_value, is_called FROM <resolved sequence>` works.
9. `hh_provision`: `enroll-user`, `enrollment-status` complete on a scratch database.
10. `hh_migrate`: `alembic upgrade head` on an empty database; `pg_class.relowner` is `hh_owner` for every table, index and sequence.
11. `hh_restore`: create a database owned by `hh_owner`, restore, apply the ACL script, and pass a real `hh_app` read/write smoke.
12. The application integration suite passes connected as `hh_app`; any DDL-requiring test asks for `hh_owner` explicitly.

**Negative** - each MUST fail with SQLSTATE `42501` or a connection refusal:

1. `hh_app`: `CREATE TABLE`, `DROP TABLE`, `ALTER TABLE`, `CREATE INDEX`, `TRUNCATE`, `CREATE SCHEMA`, `GRANT`, `SET ROLE hh_owner`.
2. `hh_app`: any `INSERT`/`UPDATE`/`DELETE` on `alembic_version` (only `SELECT` is permitted); `UPDATE` on the sequence relation.
3. `hh_app`: connect to `postgres` or any other database; `CREATE TEMP TABLE`.
4. `hh_app`: access the content or size of a temporary restore/smoke database it is not granted `CONNECT` on (Freeze Erratum FE-4). **This test does not require concealment of publicly accessible PostgreSQL catalog metadata** such as database names or role names in `pg_database`/`pg_roles`; it requires that content and size are inaccessible.
5. `hh_purge`: `INSERT`; `UPDATE` a non-granted column (for example `sessions.token_hash`, `conversations.user_id`, `messages.content`); DDL; `TRUNCATE`; sequence access; `CREATE TEMP TABLE`.
6. `hh_purge`: any access to a table outside the retention set, including `users`, `allowlist_entries`, `identity_links`, `favorite_recipes`, `shopping_lists`, `shopping_list_items`, `shopping_list_requests`, `message_recipes`, `telegram_links`, `telegram_active_conversations`.
7. `hh_purge`: `DELETE` or `UPDATE` on `alembic_version`.
8. `hh_backup`: any write; any DDL.
9. `hh_provision`: `DELETE`; DDL; read `sessions`.
10. No project role holds an explicit grant of, and effective membership in, the prohibited predefined roles (§3.8). Explicit grants are checked in `pg_auth_members`, independently of `pg_has_role`.
11. With the maintenance fence active, `hh_app`, `hh_purge`, `hh_backup` and `hh_provision` cannot connect at all (`FATAL: permission denied for database`); `hh_migrate` can.
12. `hh_owner` cannot log in (`NOLOGIN`).
13. The `hh_purge` effective-privilege fixture matches exactly: every table in `pg_class` (excluding system catalogs) is compared through `information_schema.role_table_grants`, `has_table_privilege` and `has_column_privilege`. A future table without an explicit purge grant fails. `hh_purge` has no role membership and no default-ACL inheritance.

**ROL-20.** A stray `psql` as `hh_migrate` during maintenance can write. This is a **known residual**, not a claim that the fence is absolute: the credential is maintenance-only and root-held.

**ROL-21.** There is no "widen to table-level `UPDATE`" fallback. Single-column `UPDATE` is valid for ordinary row locking under PostgreSQL 16. If an implementation test contradicted that, the contradiction is a finding requiring an amendment, never a silent widening.

### 3.7 Authentication and credential handling

**AUTH-01.** `pg_hba.conf` permits `scram-sha-256` only from the database network's CIDR. The bootstrap superuser is `local`/`peer` only. No `trust` line exists for any remote address.

**AUTH-02.** Each login has its own credential source. No general-purpose ops container holds every credential. Whatever mechanism OD-8 selects, credential material MUST be access-controlled and provided only to the service that uses it (for a file-based arrangement: mode `0600`, owned by the running user, mounted only into the one-shot that uses it).

**AUTH-03.** CLI tools (`psql`, `pg_dump`, `pg_restore`) cannot consume a SQLAlchemy URL. Each role's tool container receives non-secret libpq settings from the same source - `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGAPPNAME`, `PGSSLMODE` - plus a libpq-compatible credential mechanism selected under OD-8. The concrete variable (for example `PGPASSFILE` or `PGPASSWORD`) is **not approved by this specification**.

**AUTH-04.** Preflight/read-back prints only non-secret allowlisted fields: `HOUSE_HELPER_DATABASE_URL` (which contains no password), `PGUSER`, `PGDATABASE`, `PGHOST`, `PGPORT`, `PGAPPNAME`. It never prints `PGPASSWORD` or a whole environment; `docker inspect` is filtered by exact names, never dumped.

**AUTH-05.** The effective DSN is read back from the **process**, not the file: `docker inspect` the container's environment, then `SELECT current_user, current_database(), inet_server_addr(), inet_server_port()`. The start gate refuses a wrong role, database or `search_path`.

**AUTH-06.** Rejected credential practices: superuser as a runtime credential; `pg_read_all_data` for backup; `UPDATE`/`SELECT` on sequences for the app; one general-purpose ops container holding every credential; `pg_restore --clean` over a live database; a `?password=` in a connection string.

### 3.8 Predefined-role membership: MEMBER, USAGE, SET

**MEM-01.** Three independent inquiries are measured separately for each `(login, predefined role, current database)`:

| Inquiry | Meaning | Measurement |
|---|---|---|
| **MEMBER** | Direct or indirect membership, including chains. | `pg_has_role(<login>, <role>, 'MEMBER')` |
| **USAGE** | The role's privileges are available **without** `SET ROLE`, per inheritance. | `pg_has_role(<login>, <role>, 'USAGE')` |
| **SET** | Permission to assume the role. | `pg_has_role(<login>, <role>, 'SET')` |

`MEMBER` being true does not imply `USAGE` or `SET`, and a chain granting `SET` does not imply `USAGE`.

**MEM-02.** `pg_database_owner` has **no `pg_auth_members` row**; its membership is implicit and evaluated against the current database's owner.

**MEM-03.** In `house_helper` (owned by `hh_owner`), `hh_owner` is a MEMBER of `pg_database_owner` by ownership. Because `hh_migrate` and `hh_restore` are `SET` members of `hh_owner`, indirect MEMBER may also be true for them **in a database owned by `hh_owner`**. In another database (for example `postgres`), the same login is not a member. The result is database-specific.

**MEM-04.** This relationship is expected and MUST NOT cause a false security-test failure. The allowed cache is: `hh_owner` as owner, and the delegated `hh_migrate`/`hh_restore` chains through it, **in a database owned by `hh_owner` only**.

**MEM-05.** Prohibited: any row in `pg_auth_members` whose `member` is a project role and whose `roleid` is a predefined role. Every predefined role other than `pg_database_owner` - including `pg_read_all_data`, `pg_write_all_data`, `pg_signal_backend`, `pg_read_server_files`, `pg_write_server_files`, `pg_execute_server_program`, `pg_monitor` and the rest of the frozen set - MUST be false in MEMBER, USAGE and SET for every project login, in every database.

**MEM-06.** An effective privilege arriving through an unintended path (not ownership/delegation) fails, even when it is not an explicit grant.

**MEM-07.** Acceptance checks MUST name the tested login and `current_database()`; inspect explicit grants separately through `pg_auth_members`; verify MEMBER, USAGE and SET independently; accept only the intended owner and delegated-owner shapes; and report a failure naming the login, role and database only - never a credential.

**MEM-08.** The exact boolean matrix is measured on the pinned PostgreSQL 16 image and frozen in the acceptance fixture. The architecture fixes the allowed shapes; the fixture fixes the observed values, and a `true` outside those shapes fails the run.

**MEM-09.** The enrollment record stores **both** observations: the explicit `pg_auth_members` matrix and the effective `pg_has_role` results.

### 3.9 Privilege preparation order relative to connection enablement

**ROL-22.** Database-level ACL work is read and changed **from the maintenance database**, never by connecting to a database whose connections are disabled. The order is fixed: create disabled -> revoke `PUBLIC` -> grant `CONNECT` to the single intended login -> verify ACL -> enable connections -> only then reconnect.

**ROL-23.** PUBLIC `CONNECT` observed after enablement is a **fail-closed** condition. It is never repaired by "granting the rest" or by continuing; it requires explicit remediation under the E4 matrix (§5.2).

### 3.10 Honest isolation statement

**ROL-24.** The application login can read every row of every user-owned table. Per-user isolation is enforced by application queries and composite foreign keys `[T]`, not by the database. This is unchanged by the architecture and is **not** row-level security.

**ROL-25.** The mandatory gate before `HH-BUDGET-01` - deferred, not waived - requires: non-owner `NOBYPASSRLS` serving credentials; transaction-scoped user context with fail-closed missing-context behavior; cross-user read/write and pooled-connection tests; explicit purge/backup behavior under RLS; and a written threat model.

**ROL-26.** RLS rationale (correction retained from R2): a non-owner role is subject to enabled policies **without** `FORCE ROW LEVEL SECURITY`.

---

## 4. Initial trusted cluster enrollment, resume and identity-update operations

### 4.1 Two modes, never one fallback

| | **Mode A - Initial Trusted Enrollment** | **Mode B - Subsequent Maintenance / Restore** |
|---|---|---|
| When | The first time, on a fresh dedicated cluster. | Every later migration, dump, restore, purge or provisioning session. |
| Trusted input | The recorded deployment configuration (Compose file at the recorded path, checked-out commit, image digest, explicit container name and PGDATA volume). **Not** network discovery. | The durable enrollment record written by Mode A. |
| Required evidence | Fresh-cluster inventory: only `postgres`, `template0`, `template1`; no `house_helper`; no `hh_*` role; no marker. | Recorded `system_identifier` **equals** current; marker present and matching; container/volume identity equals the record; database and role inventory equals the approved record plus authorized temporary databases (§8.9). |
| Identifier | **Obtained now** (`SELECT system_identifier FROM pg_control_system()`), then recorded. | **Required**; missing or mismatched means fail closed, no mutation. |
| Fallback | Not applicable. | **Never** automatically falls back to Mode A. A new cluster requires a separately authorized new Mode A enrollment. |
| Writes | Seven project roles, the application database, the marker, the enrollment record. | Only the authorized operation; identity mismatches abort before any statement. |

**ENR-01.** Mode B never adopts an unknown cluster, never recreates or `ALTER`s a conflicting role, never drops or ignores an unexpected database/role/object, and never treats a missing completion record as "nothing happened".

### 4.2 Mode A - exact contract

**Preconditions (all observed, or abort):**

1. The container is the explicitly named House Helper PostgreSQL 16 container named by the deployment configuration. It is not the n8n container, not "whatever listens on 5432", not a socket.
2. Its `PGDATA` is the dedicated named volume recorded in the configuration; it is not the n8n volume.
3. The image is the pinned `postgres:16.x` digest.
4. The cluster inventory is fresh: databases are a subset of `{postgres, template0, template1}`; roles contain the bootstrap superuser and no `hh_*` project role; there is no `house_helper` database and no enrollment marker.
5. `SELECT system_identifier FROM pg_control_system()` returns the new cluster's identifier.

**Actions (idempotent, fail on error):**

**ENR-02.** Append a durable `cluster_enrollment:intent` record **outside `PGDATA`** (root-owned `0600`, append-only, `fsync` of file and directory, `prev` digest chain). It contains: container name/ID, image digest, volume name, the observed `system_identifier`, the approved inventory (seven project roles, one application database, the marker value), and the frozen predefined-role set.

**ENR-03.** Create exactly the seven project roles of §3.2, with the attributes and memberships stated there. No project role receives an explicit predefined role.

**ENR-04.** Create the application database owned by `hh_owner` from `template0`, with connections disabled, revoke `PUBLIC`, harden `public`, and install the default ACLs. The enablement order is §5.1.

**ENR-05.** Create the identification marker. Exactly one mechanism is chosen once and frozen for the deployment: a database `COMMENT` carrying the enrollment token, or a dedicated marker row. Both spellings are never in use at once.

**ENR-06.** Append `cluster_enrollment:completed` with the observed identifier, container/volume identity and the verified inventory: role names and attributes, explicit memberships, effective membership matrix, database name/owner/encoding/locale, marker and predefined-role set.

**Predefined roles are handled explicitly, never treated as "unexpected project roles":**

**ENR-07.** The inventory check distinguishes three sets:

- **Predefined cluster roles** of the pinned PostgreSQL 16 minor, read from the image and frozen in the enrollment record - at minimum `pg_monitor`, `pg_read_all_settings`, `pg_read_all_stats`, `pg_stat_scan_tables`, `pg_read_all_data`, `pg_write_all_data`, `pg_database_owner`, `pg_signal_backend`, `pg_checkpoint`, `pg_create_subscription`, `pg_use_reserved_connections`, `pg_read_server_files`, `pg_write_server_files`, `pg_execute_server_program`. The exact list is **observed and frozen**, never hand-assumed.
- **The bootstrap superuser** (`postgres`).
- **The seven project roles.**

Anything else is unexpected and aborts the enrollment.

**ENR-08 - partial failure.** The script is idempotent per role/database: existence is checked, attributes are verified, and an existing role with the same name but different attributes aborts. Re-running with the same intent record completes the enrollment, provided the cluster identifier still matches the intent and no foreign object appeared. If the identifier differs, the cluster is not the one the intent named: stop and require a separately authorized enrollment.

### 4.3 Durable-state dispatch and resume

**ENR-09.** The freshness check is a **pre-intent** test only. Once an intent exists, the cluster is no longer "fresh", and resumption is governed by that intent.

| Durable state | Observed cluster | Allowed action |
|---|---|---|
| **E0** - no enrollment intent | Pristine: only `postgres`, `template0`, `template1`; no `house_helper`; no `hh_*` project role; no marker. | **Begin Mode A**: verify the authorized container/volume/image, obtain `system_identifier`, append `enrollment:intent`, then create. |
| **E0** - no intent | Non-pristine, foreign objects, a project-named role/database, or an identifier that a record claims. | **Fail closed.** Never adopt. A deliberately authorized repair or a new enrollment is a separate Owner act. |
| **E1-E6** - matching intent, recognized partial state | A **verified subset** of the authorized seven roles, the application database, the ACL state and the marker. | **Resume that exact enrollment.** |
| **E1-E6** - intent present | Unknown object, conflicting attributes, a foreign database/role, a different `system_identifier`. | **Fail closed.** Never recreate a conflicting role; never discard unexpected state. |
| **E7** - `enrollment:completed` | The recorded inventory. | **Mode B only.** |

**ENR-10.** "Recognized partial state" means every existing object is **in the authorized inventory and matches its intended attributes**, and nothing outside the inventory exists. A subset is permitted; an extra is not.

**ENR-11 - reconciliation of each interrupted stage.** "Verify" means: role attributes (`rolcanlogin`, `rolsuper`, `rolcreatedb`, `rolcreaterole`, `rolbypassrls`, `rolreplication`, `rolconnlimit`), explicit memberships (`pg_auth_members`), ownership (`pg_class.relowner`, `pg_database.datdba`) and privileges (`has_*_privilege`, ACL entries) against the intended contract.

| Interrupted after | Observed | Reconciliation |
|---|---|---|
| **E1** - intent recorded, no mutations | Pristine with respect to project objects, same identifier. | Re-verify container/volume/image and identifier; proceed from role creation. If anything project-named appeared, fail closed. |
| **E2** - some roles created | A subset of the seven; each matches. | Verify each; create only the missing ones; reject an existing role whose attributes/memberships differ (do not "fix" it); reject any `hh_*` name outside the seven. |
| **E3** - all roles created, database absent | Seven roles, no `house_helper`. | Verify roles; create the database from `template0` owned by `hh_owner` under §5.1; continue. |
| **E4** - database created, hardening incomplete | Database exists; owner/encoding/locale match; `datallowconn`/ACL in an **allowed** state of §5.2; **no application tables**. | Complete database-level hardening from the maintenance connection; enable only after verification. If any application table exists, fail closed. |
| **E5** - hardening complete, marker absent | Roles, database and ACL verified; no marker. | Create the marker idempotently from the same intent value; continue. |
| **E6** - marker present, completion absent | Everything verified; marker matches. | Append `enrollment:completed` with the observed evidence. No further creation. |
| **E7** - completion recorded | Recorded inventory. | Mode B; any mismatch is an identity failure, not a resume. |

**ENR-12.** A milestone may be written for an observed state the journal had not yet recorded (`reconcile:milestone`), but the state itself is never inferred from absence.

### 4.4 Enrollment journal shape

**ENR-13.** `enrollment:intent` (approved inventory + observed `system_identifier` + container/volume/image) -> `enrollment:milestone` records (`roles`, `database`, `hardened`, `marker`) -> `enrollment:completed` (observed inventory, predefined-role set, explicit-vs-implicit membership record).

```json
{"kind":"cluster_enrollment","mode":"A","state":"completed",
 "system_identifier":"<observed>",
 "container":{"name":"house-helper-db","id":"...","image_digest":"sha256:..."},
 "volume":"house-helper-pgdata",
 "databases":[{"name":"house_helper","owner":"hh_owner","encoding":"UTF8","collate":"...","ctype":"..."}],
 "roles":[{"name":"hh_owner","login":false,"createdb":false}],
 "predefined_roles":["pg_monitor"],
 "explicit_predefined_grants":[],
 "effective_membership":{"pg_database_owner":{"hh_owner":true}},
 "marker":"...","approved_by":"<owner>","at":"...Z","prev":"sha256:..."}
```

**ENR-14.** The journal is outside `PGDATA` (it MUST survive volume loss), uses the separate journal lock of §7.2, and is never written under the maintenance flock.

### 4.5 Mode B - exact preflight

**ENR-15.** Before any statement of a migration, dump, purge, provisioning session or restore:

1. Read the enrollment record and require `cluster_enrollment:completed`.
2. Compare `SELECT system_identifier FROM pg_control_system()` with the recorded value.
3. Compare the container identity (name/ID, image digest) and the `PGDATA` volume with the record.
4. Compare the database/role inventory with the approved record plus active temporary-database records, excluding the frozen predefined-role set and the bootstrap superuser.
5. Read the marker and compare it with the record.

**ENR-16.** Any missing or mismatched item means **fail closed**, touch nothing, and write a `cluster_identity:mismatch` journal record naming which item failed (never a credential). There is no automatic Mode A fallback.

**ENR-17.** A replacement cluster - a new `system_identifier` - requires a separate, authorized Mode A enrollment. The backup archive's original `system_identifier` is recorded in the restore manifest as **provenance** ("this archive came from that cluster"), never as the replacement cluster's identity and never as a Mode B precondition.

### 4.6 Authorized container identity updates

**ENR-18.** Routine Docker re-creation (Compose changes, image rebuild over the same PostgreSQL data) and PostgreSQL 16 **patch** updates are neither Mode B maintenance nor a new enrollment. They are a distinct journaled operation.

**Before the change:**

1. Verify the current enrolled identity (Mode B preflight against the completed enrollment record).
2. Append `identity_update:intent` with: operation ID, Owner/operator authorization, the exact intended image digest or container configuration change, the current container identity, the current `system_identifier`, the `PGDATA` volume mapping and the marker.
3. Stop/drain the affected processes under the maintenance contract (§7): timers and services stopped, web stopped and confirmed, one-shots gone, exclusive lock held.
4. Append `identity_update:transition` **before** the new container starts: old identity -> intended new identity.

**After the change:**

1. Verify the intended new container name/ID, image digest and configuration.
2. Require the **unchanged** PostgreSQL `system_identifier` and the **unchanged** `PGDATA` volume identity (same volume, same data directory).
3. Require the matching marker, the seven project roles and the authorized database inventory (base plus active temporary records).
4. Append `identity_update:completed` with the new container identity and the observed evidence; update the enrolled identity record. Mode B thereafter expects the new identity.

**ENR-19 - failure and interruption.**

- An interrupted update is reconciled **only** against its `identity_update:intent`/`:transition`. A new container that matches the intended transition but lacks `:completed` is completed after verification. A new container that does not match the intent fails closed.
- After a completed identity update, the recorded identity is the new one; anything else is a failure.
- A **changed `system_identifier`** is a **replacement cluster**, never an identity update, and requires a separately authorized Mode A enrollment.
- A PostgreSQL **major-version change** (including `pg_upgrade`, which initializes a new cluster) is a replacement cluster by definition.
- A PG 16 **patch** update reuses `PGDATA` and MUST preserve the identifier; if it does not, the operation fails closed and becomes a replacement-cluster decision.

### 4.7 Cluster identification and n8n non-interference

**ENR-20.** The target cluster is positively identified by container name/ID, image digest and `PGDATA` volume - never by "whatever is on 5432", never by a socket. A mismatch stops the operation before any statement.

**ENR-21.** The shared n8n PostgreSQL cluster is never used, connected to, or altered. No cluster-wide `ALTER SYSTEM`, `pg_hba.conf` edit, role creation or role attribute change may reach it. An identification or marker mismatch aborts.

**ENR-22.** Session termination belongs to the explicitly authorized local administrator (`docker exec ... psql` as the superuser). No runtime role holds `pg_signal_backend`.

---

## 5. Schema initialization and fresh two-account bootstrap

### 5.1 E3 - creating the database behind a connection fence

This section incorporates Freeze Errata FE-1 and FE-3. The three checks below are **separate** and MUST NOT be collapsed into one.

**State E3-a - immediately after `CREATE DATABASE`:**

- Connections are disabled: `datallowconn = false`.
- The database's **default `PUBLIC` `CONNECT`/`TEMPORARY` privileges may still exist**. This is a **recognized intermediate state** and only permissible while connections are disabled.
- No connection is attempted in this state, and no schema work occurs.

**State E3-b - after ACL preparation, still disabled:**

- `PUBLIC` has been revoked (`REVOKE ALL ON DATABASE house_helper FROM PUBLIC`).
- The ACL has been **verified**: no `aclexplode` entry with `grantee = 0` for `CONNECT` or `TEMPORARY`; `CONNECT` exactly for `hh_migrate`; the owner's inherent rights.
- Connections are still disabled.
- Only now may connections be enabled.

**State E3-c - after enablement:** connections are enabled, and the ACL is re-verified. `PUBLIC` `CONNECT` observed at or after enablement is **fail-closed** (ROL-23); it is a defect, not an accepted state.

**BOOT-01 - exact E3 sequence:**

```sql
-- Step 1: authorized local cluster administrator, connected to the maintenance database
CREATE DATABASE house_helper
    OWNER hh_owner
    TEMPLATE template0
    ENCODING 'UTF8'
    LC_COLLATE '<validated>'
    LC_CTYPE '<validated>'
    ALLOW_CONNECTIONS false;
-- Check E3-a: datallowconn = false. Default PUBLIC privileges may exist here and only here.

-- Step 2: still the maintenance connection - PUBLIC loses everything
REVOKE ALL ON DATABASE house_helper FROM PUBLIC;

-- Step 3: the only login that may connect initially
GRANT CONNECT ON DATABASE house_helper TO hh_migrate;
-- hh_owner keeps its inherent owner privileges; no other role is granted anything

-- Step 4: verify the ACL before any connection is permitted  (Check E3-b)
SELECT datallowconn, datacl FROM pg_database WHERE datname = 'house_helper';
-- require: datallowconn = false; no aclexplode entry with grantee = 0;
--          CONNECT for hh_migrate; owner's inherent rights

-- Step 5: enable connections only after the ACL is verified
ALTER DATABASE house_helper ALLOW_CONNECTIONS true;

-- Check E3-c: re-read datacl; PUBLIC CONNECT present at this point is fail-closed
```

**BOOT-02.** Reconnect to `house_helper` as the authorized migration login (`session_user = hh_migrate`).

**BOOT-03.** Assume `hh_owner` where required. Whether this is the session's configured default role or an explicit `SET ROLE hh_owner`, the required end state is `session_user = hh_migrate`, `current_user = hh_owner`. It is **verified**, never assumed (`SELECT session_user, current_user, current_database()`).

**BOOT-04 - schema contract as `hh_owner`, with explicit `search_path` (ROL-05/ROL-06):**

```sql
SET search_path = public;
ALTER SCHEMA public OWNER TO hh_owner;      -- resolves to the database owner; made explicit
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO
      hh_app, hh_purge, hh_backup, hh_provision, hh_migrate, hh_restore;
-- default privileges per ROL-14, for future objects created by hh_owner
-- then: alembic upgrade head (as hh_owner), db/privileges.sql, alembic check
```

Verify `current_setting('search_path') = public`, the schema owner, the schema ACL, `pg_default_acl`, and that `public` is the only non-system schema.

**BOOT-05 - the fence remains effective for runtime roles after schema work:**

```sql
SELECT has_database_privilege('hh_app',       'house_helper', 'CONNECT'),  -- false
       has_database_privilege('hh_purge',     'house_helper', 'CONNECT'),  -- false
       has_database_privilege('hh_backup',    'house_helper', 'CONNECT'),  -- false
       has_database_privilege('hh_provision', 'house_helper', 'CONNECT');  -- false
```

They are granted only at H1/H2/H3 (§7.3).

**BOOT-06.** Never attempt target-schema operations while connections are disabled. Database-level ACL is read and changed **from the maintenance database**; schema work happens only after Step 5 and the reconnect.

### 5.2 E4 - allowed-state matrix for an interrupted or resumed database

**BOOT-07.** The permissive "any intermediate connection/ACL combination" reading is withdrawn. Only these states are allowed:

| Observed | Allowed action |
|---|---|
| Database **absent** | Creation may proceed under the matching enrollment intent (BOOT-01 Steps 1-5). |
| Present, `datallowconn = false`, ACL **not fully hardened** | Finish database-level ACL hardening from the maintenance connection (`REVOKE`/`GRANT`/verify); do not connect. Default `PUBLIC` entries are tolerated **only** here. |
| Present, `datallowconn = false`, ACL **hardened** | Enable connections only after verification (`ALTER DATABASE ... ALLOW_CONNECTIONS true`). |
| Present, `datallowconn = true`, ACL **hardened**, **only intended maintenance access** (`hh_migrate`; no app/purge/backup/provision) | Proceed to schema initialization and validation. |
| Present with **unexpected `PUBLIC` access**, `datallowconn = true` while the ACL is **not** hardened, **unauthorized sessions**, **conflicting ownership**, **unexpected tables**, or any other unrecognized state | **Fail closed**; require explicit remediation. Never "fix forward" by enabling access or adopting objects. |

**BOOT-08.** A journal record is evidence only of what it says. **A missing journal acknowledgment is never proof that an operation did not occur.** The matrix is applied to the **observed** cluster, and a missing milestone is reconciled (`reconcile:milestone`) against that observation.

### 5.3 Schema initialization by execution

**BOOT-09 - ordered initialization:**

1. Bootstrap: roles, database (disabled), `PUBLIC` revoke, `hh_migrate` `CONNECT`, ACL verification, enablement, schema hardening, `ALTER DEFAULT PRIVILEGES`.
2. `alembic upgrade head` as `hh_migrate` (which assumes `hh_owner`). The complete chain `0001-0014` runs in the caller's transaction; on PostgreSQL DDL is transactional, so a failure leaves **no** application tables. An interrupted chain MUST leave the database at zero application tables and `alembic_version` absent.
3. `alembic check` (autogenerate) reports no drift against the models; `alembic_version` contains exactly `0014_user_measure_overrides`; it lives in `public`, is owned by `hh_owner`, and is `SELECT`-only for `hh_app` and `hh_purge`.
4. Re-apply `db/privileges.sql` (idempotent). Default privileges do not cover objects that already exist.
5. The historical data steps (`0002`, `0004`, `0010`) execute against **zero rows** and are no-ops. This is a property of initializing an empty database, not a suppression.
6. `alembic stamp` is never used. There is no code path for it, and this architecture forbids adding one.

**BOOT-10.** Object ownership after the chain: `pg_class.relowner = hh_owner` for every table, index and sequence. `alembic_version` is one of those objects, not an exception to ownership.

### 5.4 The two accounts - supported flows, not row inserts

**BOOT-11.** The current Production shape is an **unverified assumption `[U]`**: Telegram login enabled, email login disabled, two users, and the bot enabled. It MUST be measured in the stage 7 inventory before any cutover window. The supported recreation is three consecutive, independent steps per person `[T]`:

1. **Provision** - `python -m house_helper.admin enroll-user --display-name "..."`, twice. `ENROLLMENT_USER_CAP = 2` counts every row and blocks a third; a repeated display name is refused. Each run prints an invitation secret **once** on stdout; the ops service has no log driver for this reason. No email address, no Telegram `sub` and no identity link is created here.
2. **Claim** - the person opens `https://<host>/auth/invite#<secret>`, completes Telegram OIDC, and the callback writes `allowlist_entries` (Telegram), `identity_links`, a session, and consumes the invitation in one transaction. The `sub` is bound by verified OIDC only and is never guessed.
3. **Pair the bot** - signed in, the person calls `POST /api/telegram/link`, receives a ten-minute pairing code, opens `https://t.me/<bot>?start=<code>`, and `telegram_links` is written. This step **requires the bot to be running**, so it happens after the bot is enabled at C6 (§7.6).

**BOOT-12.** Account recreation is not "insert two rows into `users`". The invitation cap and its advisory lock, the allowlist entry that makes a Telegram `sub` resolvable on later ordinary logins, the single-use invitation secret, the identity link's composite foreign keys and session provenance, and the separate bot-pairing table are all required. A hand-inserted user can sign in nowhere until these artifacts exist.

**BOOT-13.** Settings are recreated through normal workflows: `PATCH /api/profile` (display name, normal/large text), personal measures (`PATCH /api/profile`, migration `0014`), favorites and shopping lists by ordinary use. Nothing is copied from SQLite.

**BOOT-14 - necessary adaptations, verified against the code:**

- The admin CLI already supports PostgreSQL (`SUPPORTED_BACKENDS` includes it; `check_schema_at_head` reads `alembic_version`; the advisory lock is PostgreSQL-only) `[T]`. It needs **grants**, not code changes.
- `dbtool` refuses non-SQLite URLs and MUST be extended to name a database target. `check-schema` already delegates to `open_target` `[T]`.
- `house-helper-ops` cannot keep `network_mode: none` and "no credentials". Every database command needs the database network and its own login. Mitigation: one `internal` network containing only PostgreSQL and the one-shots; one credential source per service (OD-8); `--rm`; `logging: driver: none` for the provisioning service; no Traefik labels; no published ports.

**BOOT-15 - sequence on a fresh database.** A fresh database creates the `messages` sequence with `is_called = false`; the first message receives `1`. No `sqlite_sequence` reconstruction exists. The surviving sequence obligation is restore correctness (§8.7), not initialization.

### 5.5 Bootstrap acceptance `[SPEC]`

Acceptance is split by the state in which it can be observed. **No C5 criterion may require evidence that can exist only after the Telegram poller is enabled (C6).** Requirement identifiers are preserved from the earlier text; each is verified in the state named here.

#### 5.5.1 C5 - account enrollment (poller disabled)

**BOOT-16.** Two users exist, each with one active Telegram allowlist entry and one identity link (verified at C5). Each additionally completes bot pairing and ends with one Telegram chat link per account (verified at C6, §5.5.2). The chat link is **not** a C5 condition.

**BOOT-17.** Each signs in through ordinary `/auth/telegram/login`, not only through the invitation.

**BOOT-18.** Each signs out; the session is gone; the cookie cannot be replayed.

**BOOT-20.** Isolation negatives with two live cookies: user A cannot read user B's conversation, history, favorite, shopping list, profile or attachment (`404`/`403`, one answer for "not found"); a crafted callback or recipe id from B is "not found" for A.

**BOOT-21.** Cross-user database facts: every `conversations.user_id`, `favorite_recipes.user_id`, `shopping_lists.user_id` and `telegram_links.user_id` belongs to the right account; no row is orphaned to the other user. The first three are verifiable at C5; the `telegram_links.user_id` clause is verified at C6 (§5.5.2) because the row cannot exist before pairing.

**BOOT-22.** A fresh `enrollment-status` reports both users, no `sub`, no secret and no email.

#### 5.5.2 C6 - bot enabled (poller running)

The C6 clause of **BOOT-16** is verified here: each user completes bot pairing (`POST /api/telegram/link` -> `https://t.me/<bot>?start=<code>`), and one Telegram chat link exists per account.

**BOOT-19.** An unlinked third account receives one fixed notice from the bot, no assistant call, no row.

The C6 clause of **BOOT-21** is verified here: every `telegram_links.user_id` belongs to the right account; no row is orphaned to the other user.

**BOOT-23.** The admission ledger has exactly one `cutover` row after the bot's first start, no markers, and the sequence is at 1 (no message yet) or at the first message id. The backfill is empty.

The Telegram requirements of §6.1-§6.5 (TG-01..TG-08) are verified in this state: the routing matrix, the acknowledgment/claim combinations, one-poller evidence, and the no-webhook check.

---

## 6. Telegram cutover and pending-update constraints

### 6.1 Writers and the fence

| Writer | Mechanism | Fence |
|---|---|---|
| Web + in-process Telegram poller | Long-lived uvicorn; one process. | Container stopped and confirmed; then `hh_app` has no `CONNECT` until H1. |
| Purge one-shot | systemd, `Persistent=true`. | Timer **and** service stopped; `hh_purge` has no `CONNECT` until H3. |
| Offsite backup | Host Python -> `backup.sh` -> ops -> `pg_dump`. | Timer and service stopped; `hh_backup` has no `CONNECT` until H3. |
| Orphaned `compose run` one-offs | Client death leaves a container. | Enumerate by label; remove only this project's one-offs. |
| Admin commands | Manual. | `hh_provision` has no `CONNECT` until H2; the advisory lock serialises them after it. |
| Migration | One-shot. | `hh_migrate` (and `hh_restore` for recovery) are the only pre-handoff logins. |
| Stray `psql` as `hh_migrate` | Bypasses the flock. | **Residual:** credential is root-only and used only in the window (ROL-20). |
| Legacy `bot.py` | Separate process; does not import persistence `[T]`. | Not a database writer; it would only fight for the token (`409`). |

**TG-01.** Layer 0 (cooperative): host `flock` on the state volume (§7.1). Layer 1 (server-enforced): `CONNECT` privileges. Layer 2: session enumeration and termination by the authorized administrator (`pg_stat_activity`, `pg_terminate_backend`) before loading. Layer 3: the app container has one DSN and mounts neither the SQLite volume nor another role's credential.

### 6.2 Permission, write and external effect - kept distinct

| Event | Permission to connect | First possible persistent write | Externally visible effect |
|---|---|---|---|
| Schema migrated, ACLs applied | App: **no** | Impossible. | None. |
| `GRANT CONNECT ... TO hh_app` (H1) | **Yes** | None until a statement runs. | None. |
| App starts with bot disabled | Yes | Lifespan opens nothing; a public page load writes nothing. | None. |
| First invitation claim / sign-in | Yes | `oidc_states`, `allowlist_entries`, `identity_links`, `sessions`. | Telegram OIDC exchange. |
| `enroll-user` | Provision: yes | `users`, `identity_invitations`. | Invitation secret handed to the owner. |
| Bot enabled; first poller start | Yes | `telegram_update_admissions` cutover row (backfill empty). | `setMyCommands`; `getUpdates`; replies to pending updates. |
| Purge | Purge: yes | `DELETE`s; `turn_requests` column update. | None. |
| Backup | Backup: yes | None (read-only). | Offsite upload. |
| Restore | Restore: yes | Replacement database. | None until the app restarts. |

**TG-02.** A permission grant is a conservative potential-divergence boundary, not proof that a write occurred. This specification never infers "nothing happened" from a missing acknowledgment; it reconciles observed state (§7.8).

### 6.3 Scoping of the update router `[T]`

The timestamp, cutover and admission policies apply **only** to conversational messages addressed to the assistant by an enrolled sender. The router handles updates in this order:

| Update | Behavior after a fresh cutover |
|---|---|
| `/start <code>` (pairing) | **Processed** - commands are not admitted; pairing can succeed. |
| Other commands (`/dialogs`, `/new`, `/favorites`, `/help`, `/unlink`) | **Processed** - not admitted. |
| Callback queries (save / list / select) | **Processed** - not admitted. |
| Unpaired sender (any message) | Fixed stranger notice, rate-limited to once per 10 minutes. |
| Group/channel/bot update | Dropped without an answer. |
| Enrolled sender, photo/text, dated **before** the cutover, no marker or turn request | Refused with the fixed pre-migration sentence (Policy A, **only if authorized**). |
| Enrolled sender, photo/text, within the timestamp policy | Normal turn. |

**TG-03.** The pre-cutover refusal behavior described in the row above is a **proposal**, not an authorized discard. Until the Owner authorizes a pending-update policy (§12), the bot is not enabled at cutover.

### 6.4 Acknowledgment and the durable-turn boundary

**TG-04.** Acknowledgment to Telegram and the claim transaction are **independent**; either can happen without the other. The poll loop advances its in-memory offset and spawns handlers as soon as updates are received (`telegram_chat._poll` -> `_spawn`). Acknowledgment happens on the **next** `getUpdates` call, not when a handler finishes.

**A committed claim alone does not prove Telegram acknowledgment.** **An update fetched by the poller is not necessarily acknowledged; fetching an update alone does not prove acknowledgment.** Claim commitment and acknowledgment are separate events, and neither implies the other.

| Telegram acknowledged | Claim committed | Contract |
|---|---|---|
| No | No | No durable turn. Redelivery is possible; the update may be processed later. |
| No | Yes | Durable turn exists. Redelivery is possible; the claim/idempotency key controls repeat handling, and the recorded state governs (completed replay / attempted not rerun). |
| **Yes** | **No** | **Silent loss is possible:** no durable turn, no answer and no redelivery. The person sees nothing and no record explains it. |
| **Yes** | **Yes** | Durable turn; no redelivery; the recorded state governs follow-up (a completed answer is replayable, an unfinished attempt is not rerun). |

**TG-05.** A durable turn exists **only if its claim transaction committed**. "Exactly one stored turn" is a property of the claim/idempotency key, not of delivery. **An interrupted handler is not guaranteed to leave an attempted/not-answered durable turn.**

**TG-06.** The fake-Bot-API interruption matrix MUST include: kill after fetch and before the next `getUpdates` call (no acknowledgment assumed; redelivery possible); kill after acknowledgment but before the claim commit (**silent loss**: no redelivery, no durable turn, no answer, and no fabricated "not answered" record); kill after the claim commit **but before any acknowledgment** (durable turn exists; **redelivery is still possible** because the claim commit does not acknowledge the update, and the existing claim/idempotency protection governs repeat handling); kill after **both** Telegram acknowledgment **and** the claim commit (durable turn; no redelivery); duplicate delivery after completion (replay); pre-cutover conversational message (refusal under A, unchanged under B); post-cutover command and `/start` pairing (processed); unpaired sender (notice); group update (dropped); a `409` second-consumer scenario. The matrix MUST assert the four acknowledgment/claim combinations of TG-04 explicitly, and MUST NOT assume that fetching, holding or interrupting a handler proves acknowledgment or leaves a durable turn.

### 6.5 Exactly one poller

**TG-07.** Stop the old web container and confirm it is not running; confirm no orphaned one-off still uses the old image; check the new container's log for "The Telegram bot is polling"; the authenticated bot-status endpoint reports `failure = None`. A `409` in the log means another consumer or a webhook holds the token, and polling MUST NOT be declared healthy until it is gone.

**TG-08.** `getMe` is safe. A `getUpdates` probe is **not** used, because it competes for the token. `getWebhookInfo` MUST show no webhook, or polling gets `409`.

### 6.6 Failure during startup `[T]`

- Ledger preparation fails -> polling does not start, no update is acknowledged, no message is read; the poller retries with backoff; a handler that needs admission refuses with the fixed "temporarily unavailable" text.
- Process killed after the cutover row commits but before polling -> the row persists; the next start does not move it; a retry cannot reclassify updates.
- Process killed with updates in hand -> the outcome is governed by the two independent events of TG-04, not by the fact that the process held the update. A fetched update is not necessarily acknowledged. If Telegram has not acknowledged them, redelivery is possible. If it has acknowledged them and no claim committed, they are **silently lost**. If a claim committed but Telegram has not acknowledged, a durable turn exists and **redelivery is still possible**, with the claim/idempotency protection controlling repeat handling. Only when **both** acknowledgment and a claim commit occurred does the durable turn prevent redelivery and leave an unfinished attempt not rerun. Holding an update at termination is **not** evidence of acknowledgment, a committed claim is **not** evidence of acknowledgment, and an interrupted handler is **not** guaranteed to leave an attempted/not-answered durable turn.
- Container crash-loops on `schema_not_head` -> the start gate refuses before uvicorn; nothing writes; the journal records `START_FAILED`; fix the schema or image, not the journal.

### 6.7 Pending-update policies (Owner decision, pending)

**TG-09.** Two lawful policies exist. The Owner MUST select one, or select none (in which case the bot stays disabled):

- **A - refuse with the fixed "please send again" sentence (recommended default; no new code).** Bounded by Telegram's 24-hour retention and the two users. In the **intended normal path**, each eligible pending conversational message is answered once with that fixed sentence. This is **not a delivery guarantee**: handler interruption, acknowledgment timing, network failures and the existing admission rules can produce other outcomes, including no reply (TG-04..TG-06). Nothing is processed; nothing is stored.
- **B - silent drain before the poller starts.** A one-shot drains `getUpdates` to advance the offset, acknowledging without replying, then the operator sends one notice. Requires new tooling and a second code path through Telegram. It is *less visible*, not less destructive.

**TG-10.** Neither policy is authorized by this specification. The selected policy is recorded in the journal before the bot is enabled (C6).

---

## 7. Maintenance locks, fencing, role handoffs and journal state machine

### 7.1 Four lock modes

The host `flock` is a **cooperative** lock on a state volume (`/state/.house-helper.lock`), inherited across `exec` exactly as today `[T]`. It never replaces the privilege fence and does not do so now.

**LCK-01.**

| Operation | Lock | Additional serialization | Why |
|---|---|---|---|
| Serving (web + in-process Telegram) | **Shared, lifetime** (held by uvicorn) | None needed. | It must coexist with backup, purge and provisioning. |
| Online `pg_dump` backup | **Shared** | **Independent backup mutex** (its own lock file in the state volume). | Shared with serving; the mutex stops two backup runs interleaving staging and manifest. |
| Purge | **Shared** | systemd oneshot one-at-a-time plus `FOR UPDATE SKIP LOCKED`. | No extra host lock; the database serializes the batches. |
| Provisioning / enrollment | **Shared** | Existing transactional advisory lock (`pg_advisory_xact_lock`) `[T]`. | Serialises provisioning runs with each other. |
| Schema migration, target replacement, exclusive maintenance | **Exclusive**, only after competing shared holders are stopped/drained. | None beyond the lock. | Must not run beside the app, a backup, a purge or provisioning. |
| Isolated restore verification | **Independent scratch lock** inside the disposable environment. | None; it must **not** take the production lock. | Verification has no business with production state. |

**LCK-02.** `exec --exclusive` is refused while any shared holder exists, and a shared holder cannot start while it is held `[T]` - preserve that behavior on the state volume.

**LCK-03.** The migration/target-replacement step runs with the web **stopped and confirmed**, not merely asked to stop.

**LCK-04.** The app's shared lock is taken by `dbtool serve` before it execs uvicorn and is released by the kernel when the process ends `[T]`; no stale lock is possible.

**LCK-05.** The lock handoff is explicit: the cutover runner holds the flock exclusively for each mutation and **releases it before** the web container starts. Starting the app while an exclusive holder remains is forbidden. If the app cannot start, re-acquire exclusive and continue from the journal's observed state.

### 7.2 The journal is not written under the maintenance flock

**LCK-06.** Journal records must be appendable while the app serves and while shared holders run. Serialization is a **separate journal lock** (`flock` on `journal.lock`, or `O_APPEND` plus a per-record `write()` under a dedicated lock file), one run at a time. The maintenance flock protects database-changing operations; it does not protect log lines.

**LCK-07.** The journal is append-only canonical JSON lines, one record per intent and one per observed outcome, `fsync` of file and directory, `prev` digest chain:

```json
{"seq":17,"prev":"sha256:...","run":"2026-11-20T...","action":"grant_connect:hh_app",
 "intent":"...","evidence":{"acl":"...","operator":"..."},"at":"...Z"}
{"seq":18,"prev":"sha256:...","run":"...","action":"grant_connect:hh_app",
 "outcome":"observed","evidence":{"has_database_privilege":true},"at":"...Z"}
```

**LCK-08.** An `intent` without an `outcome` is **UNKNOWN**, never "did not happen". The next step is the reconciler (§7.8), not the next transition.

**LCK-09.** The journal lives outside `PGDATA` (for example `/var/lib/house-helper/cutover/`), is root-owned `0700`, and is archived with the cutover record. It is not a backup of the database.

### 7.3 Three permission handoffs

**HOF-01.**

| Handoff | Grant | Before | Journal event |
|---|---|---|---|
| **H1 - serving** | `GRANT CONNECT ON DATABASE house_helper TO hh_app` | The web container is started (C4). | `handoff:app`, with `has_database_privilege` observed. |
| **H2 - enrollment** | `GRANT CONNECT ... TO hh_provision` | The first `enroll-user` (C5). | `handoff:provision`. |
| **H3 - scheduled operations** | `GRANT CONNECT ... TO hh_purge, hh_backup` | The timers are re-armed (C7). | `handoff:scheduled`. |

**HOF-02.** The maintenance logins (`hh_migrate`, `hh_restore`) keep their bootstrap authority; they are the only pre-handoff database logins. `REVOKE`/`GRANT` is one statement per role, and its observed effect is recorded. An intent without an observed outcome is UNKNOWN.

### 7.4 Timer re-arm - ordered and verified

**HOF-03.** Re-arm only after all of the following are **observed**, not assumed:

1. Each role exists with the intended attributes and **exactly** the intended memberships (`pg_roles`, `pg_auth_members`).
2. Each role's credential source is present, access-controlled as approved under OD-8, contains only that role's credential, and authenticates a `SELECT 1` as that role.
3. Each systemd `ExecStart` names the right one-shot service **and** that service's environment source (no shared credentials).
4. The lock handoff is observed: the web process holds the shared lock (`dbtool check-idle` refuses; the journal records `handoff:lock`), and no exclusive holder remains.
5. One manual `--dry-run` purge and one manual backup complete.

**HOF-04.** Do not promise a catch-up run. `Persistent=true` fires only if the schedule was actually missed, based on the timer's stamp. Read `systemctl show <unit> -p LastTriggerUSec`, `systemctl list-timers` and the stamp file **before and after** re-arm, and record whether a catch-up occurred. Acceptance is "the observed behavior is recorded and understood", not "exactly one run happened".

### 7.5 Stopping competitors - order

**LCK-10.** Stop both timers **and** both services (purge, offsite backup); verify all four units inactive; stop the web container and confirm it is not running; remove only this project's orphaned `com.docker.compose.oneoff=True` containers; then, and only then, take the exclusive lock.

**LCK-11.** A `docker run` that ignores both the lock and the credential boundary remains the stated residual. The privilege fence is the server-enforced layer.

### 7.6 Cutover states C0-C8

`C0 SERVING_SQLITE` -> `C1 MAINTENANCE` -> `C2 LEGACY_FROZEN` -> `C3 PG_READY` -> `C4 APP_STARTED` -> `C5 ENROLLED` -> `C6 BOT_ENABLED` -> `C7 STABILIZING` -> `C8 SEALED`.

| State | Entry | Invariants | Verification before leaving | On failure |
|---|---|---|---|---|
| **C0** | Normal operation. | App shared-locks; timers armed; `hh_app` has no PostgreSQL `CONNECT`. | - | - |
| **C1 MAINTENANCE** | Window declared; journal run created. | Both timers **and** services stopped; old web stopped and confirmed; orphaned one-offs removed. | `ActiveState=inactive` x4; `docker ps` empty for the project; `pg_stat_activity` has no `hh_*` except the operator; no webhook. | Restart timers/app -> C0. |
| **C2 LEGACY_FROZEN** | Final SQLite pair published by the **running** image. | Pair is `NAME.db` + one-line `NAME.db.sha256`, byte-gated by the script; old image tag and old `deploy/` preserved; the SQLite volume is not mounted by the new stack. | Pair gate passes; size/hash/revision recorded; recovery command printed into the journal; one rehearsal restore of the pair on a scratch volume. | Restore the pair; restart the old app -> C0. |
| **C3 PG_READY** | Empty PG 16 instance; bootstrap; `alembic upgrade head`; ACL script. | App/purge/backup/provision have **no `CONNECT`**; zero application rows; `alembic_version` = head; sequence `is_called=false`. | ACL positive/negative matrix; `pg_is_in_recovery()` false; per-table count = 0; `alembic check` clean; all object owners `hh_owner`. | Destroy and recreate the empty database -> C3. |
| **C4 APP_STARTED** | H1 `GRANT CONNECT` to `hh_app`; **bot disabled**; app started. | The permission boundary is crossed; **potential divergence is assumed**; no users; no bot; no external Telegram call. | Start gate: `current_user`/`current_database()`/`search_path`/`application_name`; `/healthz`; a read-only page fetch; `pg_stat_activity` shows only `hh_web`. | Stop the app, leave PostgreSQL empty, restore the old stack -> C0 **only if** the RBK-01 lossless exception is proved in full (§9.1); otherwise fail closed and keep PostgreSQL. |
| **C5 ENROLLED** | Provision two users; invitations claimed; web sign-ins verified. | Authoritative writes now exist; **rollback is lossy**; the Telegram poller is still disabled. | **C5 acceptance (§5.5.1)**: two users enrolled, identity links, sign-in/session checks and the non-bot isolation negatives. No C5 criterion may depend on a running bot. Journal records the first authoritative write. | Forward-fix only, or explicit Owner data-loss acceptance. |
| **C6 BOT_ENABLED** | Bot token/username added; container restarted. | Exactly one poller; pending-update policy applied; cutover row written once. | **C6 acceptance (§5.5.2)**: bot pairing, stranger notice, admission-ledger cutover row, one-poller evidence (TG-07/TG-08) and the applicable Telegram command/message matrix (§6.3, §6.4). | Disable the bot (env), keep web; re-enable after diagnosis. |
| **C7 STABILIZING** | Timers re-armed against PostgreSQL; observation starts. | SQLite pair frozen and untouched; backups verified; one scheduled purge and one offsite run. | Timer behavior observed and recorded (HOF-04); purge dry-run then real run; backup plus isolated restore; readiness daily. | Forward-fix; rollback is lossy. |
| **C8 SEALED** | Owner accepts. | The SQLite pair's retention is decided; v2 backups are authoritative; the rollback path is closed. | Closure note; retention action executed; monitoring green. | - |

**ST-01.** No transition is declared on an intent alone. Each transition requires its observed verification. A missing observation is reconciled (§7.8), never assumed.

### 7.7 DSN switch - one unambiguous source

**ST-02.** Today `compose.yaml` sets `HOUSE_HELPER_DATABASE_URL` under `environment:`, which wins over `env_file` `[T]`. The architecture keeps the **single-source DSN property**: each service's DSN is a literal in `compose.yaml`, and no DSN appears in any env file. **How the password is supplied, and the corresponding production deployment and rotation procedure, are an open Owner choice (OD-8, §12.6).** The recommended arrangement - `PGPASSWORD` or `PGPASSFILE` in a root-only per-role credential source - is **not approved** by this specification. Whatever the Owner selects MUST satisfy the approved credential constraints of ARC-03 and AUTH-02..AUTH-04: per-login credential separation, no secret on a command line, in a URL query, in a log line, in the journal or in echoed `docker inspect` output.

```yaml
x-database-app: &database-app
  HOUSE_HELPER_DATABASE_URL: postgresql+psycopg://hh_app@house-helper-db:5432/house_helper
x-database-migrate: &database-migrate
  HOUSE_HELPER_DATABASE_URL: postgresql+psycopg://hh_migrate@house-helper-db:5432/house_helper
# ... one per role; no password in any URL, no DSN in any env file
```

**ST-03.** There is no `ALTER DATABASE ... RENAME` and no database-name swap. Recovery is "create a new database, restore, change the literal, restart" (§8).

**ST-04.** The old stack keeps its old compose file from the preserved old checkout, so its DSN remains SQLite and cannot be half-switched.

### 7.8 Reconciler for uncertain outcomes

**LCK-12.** For any action with an `intent` and no `outcome`, observe and record; never infer:

`pg_database.datacl` / `has_database_privilege` per role · `pg_roles` attributes and memberships · `pg_stat_activity` by `application_name` · `alembic_version` · per-table counts · the resolved sequence's raw `(last_value, is_called)` · `docker ps -a` by project label · `docker inspect` effective env · `docker volume inspect` · `systemctl is-active/is-enabled` · `getWebhookInfo` (never `getUpdates`) · presence of the SQLite pair and its digest.

**LCK-13.** The journal gains a `reconciled` record naming the observed state; only then may the machine advance.

---

## 8. Backup, archive format, sequence consistency and restore contracts

### 8.1 Three artefacts, three contracts

| Artefact | Format | Reader | Lifetime |
|---|---|---|---|
| Historical SQLite recovery points | `house-helper-recovery-point/1`: tar with `MANIFEST.json`, `snapshot.db.sha256`, `snapshot.db` `[T]`. | Existing tool, read-only. | Forever; never converted. |
| Pre-cutover SQLite pair | `NAME.db` + byte-exact `NAME.db.sha256` `[T]`. | Old `dbtool restore` and old image. | Until the Owner's retention decision. |
| PostgreSQL recovery points | `house-helper-recovery-point/2`. | New tool dispatching on `format`. | 7 daily + 4 weekly (unchanged). |

### 8.2 Format v2

**BAK-01.** The outer envelope is unchanged: same `.tar.age` name shape; same `.commit` record protocol (object + size + SHA-256/SHA-1/MD5; no record means not a recovery point); same retention arithmetic; same exit codes (6 = closing listing, 7 = unresolved/uncertain); same rule that no run ever deletes a `.partial`.

**BAK-02.** Members, in order: `MANIFEST.json`, `snapshot.dump.sha256`, `snapshot.dump`.

```json
{
  "format": "house-helper-recovery-point/2",
  "database": "postgresql",
  "server_version_num": 160000,
  "system_identifier": "...",
  "pg_dump_version": "16.x",
  "dump_format": "custom", "compression": "zstd",
  "alembic_revision": "0014_user_measure_overrides",
  "encoding": "UTF8", "datcollate": "...", "datctype": "...",
  "roles_expected": ["hh_owner","hh_app","hh_purge","hh_backup","hh_provision","hh_migrate","hh_restore"],
  "acl_contract_sha256": "...",
  "tables": {"users": {"rows": 2, "digest": "..."}},
  "sequence": {
    "name": "messages_id_seq",
    "last_value": 41,
    "is_called": true,
    "next_allocatable": 42,
    "max_messages_id": 41,
    "source": "archive:SEQUENCE SET",
    "unowned": false,
    "owner": "hh_owner"
  },
  "snapshot": {"bytes": 123456, "sha256": "..."},
  "dump_settings": {"datestyle": "ISO, YMD", "timezone": "UTC", "bytea_output": "hex"}
}
```

**BAK-03 - internal consistency rule.** `pg_dump` and every count/digest MUST come from **one exported snapshot**: open `REPEATABLE READ`, `SELECT pg_export_snapshot()`, run `pg_dump --snapshot=<id>`, compute counts/digests in that same transaction, then commit. A post-dump `count(*)` describes a different dataset and is not evidence.

**BAK-04.** Sequences are **not** part of the snapshot. Their state is archive content (§8.7), never a later live read.

**BAK-05 - not promised:** byte-identical dumps (`pg_dump` output is not deterministic); point-in-time recovery; cluster roles inside the archive (`pg_dump` excludes them); a dump as a host backup.

### 8.3 Frozen `pg_dump` flags

**BAK-06.**

```
pg_dump --format=custom --compress=zstd
        --schema=public --encoding=UTF8
        --no-owner --no-privileges
        --no-tablespaces --no-subscriptions --no-publications
        --no-large-objects
        --snapshot=<exported id>
```

- `--no-owner --no-privileges` prevents archived ownership and ACLs from being applied. It does **not** remove the TOC owner field (§8.5).
- `--no-large-objects`: the schema uses `bytea`, not large objects. The dump **refuses** if `pg_largeobject_metadata` is non-empty.
- `--schema=public` freezes the surface. The dump refuses if any non-system schema other than `public` exists.
- `--no-tablespaces`: no tablespace references.

**BAK-07.** The exact flags are frozen in one constant in the tool and asserted by a test. Changing them requires a format-version decision.

### 8.4 Normalized object contract

**BAK-08.** The archive contract is a **normalized PostgreSQL 16 object contract**, generated from the real migration chain and locked by tests - never a literal object count.

| Class | Compared as |
|---|---|
| Schema | Presence and normalized definition of `public` (exact behavior frozen by the generated fixture). |
| Table definition | Relation name; columns (name, type, typmod, not-null, default expression, collation); `relkind`. |
| Table data | One `TABLE DATA` entry per table; logical digests against the manifest. |
| Sequence | `pg_sequence` parameters (`seqtypid`, `seqstart`, `seqincrement`, `seqmax`, `seqmin`, `seqcache`, `seqcycle`); `OWNED BY`; column `DEFAULT nextval(...)`. |
| `SEQUENCE SET` | `last_value`, `is_called` - the archive's own payload (§8.7). |
| Indexes | Explicit `CREATE INDEX` and constraint-backed indexes, distinguished by `pg_constraint.conindid`; `pg_get_indexdef`; uniqueness; predicate. |
| Constraints | `pg_constraint` by type (PK, FK, UQ, CHECK); `pg_get_constraintdef` including every CHECK. |
| ACL / default ACL | Absent from the archive; verified in the restored catalog against `db/privileges.sql` and `pg_default_acl`. |
| Ownership | Every object has `relowner = hh_owner`. |
| Roles | Not in the archive; verified against `db/roles.sql`. |

**BAK-09.** Explicitly **excluded** from logical equivalence: archive TOC IDs, catalog OIDs, `relfilenode`, `xmin`/`ctid`, physical row order, dump header timestamps, `pg_dump` version strings, `pg_stat` counters, comments (the identification marker is recorded separately) and any instance-specific identifier.

**BAK-10.** The exact inventory is **generated by an implementation test** against the real migration chain and checked in as a fixture. A new migration deliberately regenerates it; a hand edit to the fixture is a review event.

### 8.5 Archive ownership and ACL semantics

**BAK-11.** Ownership metadata **is present** in a custom-format archive. `pg_dump --no-owner` affects what is emitted and applied on restore, not the TOC's owner field.

- `pg_restore --no-owner` prevents archived ownership from being applied.
- `pg_restore --role=hh_owner` establishes the intended effective creator, so unowned objects are created by `hh_owner`.
- `pg_dump --no-privileges` suppresses ACL entries (`GRANT`/`REVOKE`, including default ACLs) in the archive; `pg_restore --no-privileges` prevents any archived ACL from being applied. The restored database has only bootstrap/default ACLs until `db/privileges.sql` is applied.
- The TOC owner field is **provenance**, excluded from logical equivalence. Post-restore catalog ownership assertions remain mandatory.

### 8.6 Verification levels `[SPEC]`

1. **Archive integrity** - `pg_restore --list` parses; SHA-256 of the dump matches the manifest; the object contract passes.
2. **Disposable isolated restore** - a throwaway `postgres:16` container with `--network none`, tmpfs `PGDATA`, no production volume and no production credential. `initdb`, start, create a database owned by `hh_owner`, `pg_restore --no-owner --no-privileges`, apply the ACL script, then verify:
   - `alembic_version` equals the manifest;
   - per-table `count(*)` and canonical digest equal the manifest (pinned `datestyle`, `timezone`, `bytea_output`, `extra_float_digits`);
   - FK integrity (`NOT VALID`-free), the sequence invariant and object-contract equality;
   - an attachment byte total and a small sample read through the application's own loader;
   - a **real `hh_app` smoke** on a **discarded clone**: connect as `hh_app`, `SELECT`, `INSERT` a synthetic message (consuming the sequence), `DELETE` it in a rolled-back transaction; `SELECT alembic_version`;
   - `alembic check` reports no drift.
3. **Replacement-instance recovery** - a new PostgreSQL container/volume: bootstrap roles, create the database, restore, apply ACLs, start the app, run the readiness probe, one real sign-in, one message. The new `system_identifier` is recorded as provenance, not as a requirement.
4. **New-database recovery in the existing instance** - `CREATE DATABASE house_helper_restore_<ts> OWNER hh_owner TEMPLATE template0`, restore, ACL, verify, then switch the DSN. The live database is untouched; the old one is kept until acceptance and then dropped deliberately.
5. **Never** `pg_restore --clean` over the live database; never restore into a database the app can connect to; never verify on a production-volume scratch copy.

**BAK-12.** A successful `pg_restore` is not successful application recovery. Level 2's `hh_app` smoke and level 3's start plus sign-in are the application proof.

### 8.7 Sequence consistency - archive content, not a live read

**SEQ-01 - the defect that is frozen out.** A later read of the live database is **not** evidence of the archived value: the sequence may advance between the dump and the read, and sequence allocation is not transactional with the snapshot. `pg_sequence_last_value()` returns `NULL` when `is_called = false`, so it cannot reconstruct the raw pair; an earlier use of it is withdrawn.

**SEQ-02 - read the raw pair.** Resolve the sequence and read its raw state without advancing it:

```sql
SELECT pg_get_serial_sequence('public.messages', 'id');
-- split the result into schema/name, quote each part (quote_ident / psycopg.sql.Identifier):
SELECT last_value, is_called FROM public.messages_id_seq;
```

The identifier is resolved from `pg_get_serial_sequence` and confirmed through `pg_depend`/`pg_class` to be owned by `messages.id`. It is quoted, never interpolated from an unvalidated string. The query is a plain `SELECT` on the sequence relation; it does not advance the sequence.

**SEQ-03 - scoped archive extraction.** The sequence state is read **from the archive**, scoped to exactly one TOC entry so that no user-table content passes through the sequence parser:

1. Parse `pg_restore -l snapshot.dump` into TOC entries (class, schema, name, owner).
2. The selection MUST contain **exactly one** entry whose class is `SEQUENCE SET` for the resolved expected sequence. Zero or more than one means refuse.
3. Write a list file containing only that entry:
   ```
   215; 1259 16389 SEQUENCE SET public messages_id_seq hh_owner
   ```
4. Extract: `pg_restore --use-list=sequence-set.list --no-owner --no-privileges --file=- snapshot.dump`.
5. The output is parsed as **data** and **never executed** - not during manifest generation, not during verification.

**SEQ-04.** The optional `SEQUENCE` definition entry of an earlier draft is **withdrawn**. If the pinned `pg_restore` cannot render the `SEQUENCE SET` entry alone, that is an implementation finding requiring a new amendment; the contract is not silently broadened.

**SEQ-05 - pinned-client output envelope.** The parser recognizes **only** these line classes:

1. **Known generated comments and whitespace** - only the pinned `pg_dump`/`pg_restore` comment forms (header; `-- Name: <seq>; Type: SEQUENCE SET; Schema: public; Owner: ...`; separators; empty lines). A comment outside that grammar is rejected. Comments are never used to hide or excuse anything.
2. **Allowlisted session-setting statements, exact form and exact value** - the pinned PostgreSQL 16 client set, for example:

   ```sql
   SET statement_timeout = 0;
   SET lock_timeout = 0;
   SET idle_in_transaction_session_timeout = 0;
   SET client_encoding = 'UTF8';
   SET standard_conforming_strings = on;
   SELECT pg_catalog.set_config('search_path', '', false);
   SET check_function_bodies = false;
   SET xmloption = content;
   SET client_min_messages = warning;
   SET row_security = off;
   ```

   A non-allowlisted variable, a different value, or any other `SELECT` is rejected. The exact accepted set and values are frozen in the implementation fixture.
3. **`\restrict` / `\unrestrict` wrappers, if the pinned client emits them** - exactly one pair, matching keys, balanced, non-nested. Any other backslash command (`\copy`, `\i`, `\o`, `\connect`, `\set`, ...) is rejected, whatever it looks like.
4. **Exactly one expected `pg_catalog.setval` statement**, matching the pinned rendering with an anchored grammar, the resolved schema-qualified sequence, a 64-bit integer within `[seqmin, seqmax]`, and a literal `true`/`false`:

   ```
   ^SELECT pg_catalog\.setval\('((?:[^']|'')+)'(::regclass)?, ([0-9]+), (true|false)\);$
   ```

**SEQ-06.** **Rejected:** any other executable SQL (`INSERT`/`UPDATE`/`DELETE`/`CREATE`/`ALTER`/`DROP`/`DO`/`CALL`/`COPY`/`TRUNCATE`/...); any non-allowlisted `SET`; any comment outside the grammar; duplicate `setval`; an entry for another sequence; unmatched or duplicated wrappers; unknown objects in the selection; extraction failure; and any parse that does not consume the entire output. Rejection fails closed with no manifest.

**SEQ-07 - exact comparison before any smoke write.** On the **untouched** restored verification instance, before any `hh_app` write:

```
raw_restored = (last_value, is_called)     -- SEQ-02
raw_archive  = (last_value, is_called)     -- SEQ-03..SEQ-06
require raw_restored == raw_archive        -- both fields, exact
next = raw_restored.last_value + (raw_restored.is_called ? 1 : 0)
require next > max(messages.id)            -- or max is NULL
require next within [seqmin, seqmax]
```

A mismatch, a `NULL` read, a missing sequence or a `next <= max(id)` fails closed: the archive is not restored into a serving database.

**SEQ-08 - catalog contract.** `seqincrement = 1`, `seqcycle = false`, `seqstart >= seqmin`, `seqmin >= 1`, `seqmax <= 2^63 - 1`, `seqcache` as produced by the migration chain, and the sequence owned by `messages.id`. Exhaustion (`next > seqmax`) is refused, never wrapped.

**SEQ-09 - fixtures `[SPEC]`:**

| Fixture | Expected |
|---|---|
| Never-used sequence | Raw `(1, false)`; `next = 1`; valid. |
| Concurrent advancement during the dump | Archive pair ahead of snapshot rows; `next > max`; valid, gap recorded. |
| Rolled-back insert before the dump | Sequence advanced, no row; valid; gap recorded. |
| Fully purged table | `max(id)` low or `NULL`, pair high; valid. |
| `setval(..., N, false)` state | `next = N`; valid if `> max`. |
| Invalid next-candidate (`next <= max`) | Refused. |
| `last_value` outside `[seqmin, seqmax]` | Refused. |
| Raw pair mismatch (restored != archive) | Refused. |
| Missing or duplicate `SEQUENCE SET` entry | Refused. |
| Restore failure / truncated archive | No pair claimed; fail closed. |

**SEQ-10.** A rolled-back `INSERT` does not roll back sequence allocation. The sequence may legitimately be ahead of the rows; ahead is accepted and recorded; behind is refused.

### 8.8 Restore identity and provisioning

**RST-01 - exact role matrix:**

| Phase | `session_user` | `current_user` | Authority used |
|---|---|---|---|
| Connect to the maintenance database | `hh_restore` | `hh_restore` | `CONNECT` on the maintenance database (explicit grant) plus `CREATEDB`. |
| `CREATE DATABASE ... OWNER hh_owner` | `hh_restore` | `hh_restore` | `CREATEDB`; ownership transfer requires `SET` membership in `hh_owner`. |
| Database ACL hardening | `hh_restore` | `hh_owner` | Explicit `SET ROLE hh_owner` (database owner). |
| `pg_restore` object creation | `hh_restore` | `hh_owner` | `pg_restore --role=hh_owner --no-owner --no-privileges`. |

**RST-02.** There is no blanket claim that restoration always runs as `hh_owner`. `CREATE DATABASE` runs as `hh_restore`; ownership is set at creation and assumed explicitly afterwards.

**RST-03 - maintenance-database authority.** `REVOKE ALL ON DATABASE postgres FROM PUBLIC` remains. `hh_restore` receives only the minimum explicit authority it needs:

```sql
GRANT CONNECT ON DATABASE postgres TO hh_restore;   -- the only non-superuser login allowed there
```

- No `PUBLIC` grant is restored.
- `hh_restore` receives **no** `CONNECT` on `house_helper`; it never needs the live database.
- `hh_migrate`, `hh_app`, `hh_purge`, `hh_backup` and `hh_provision` receive no `CONNECT` on `postgres`.
- The n8n cluster is never used, never connected to, and its cluster-wide privileges are never altered.
- **Alternative (Owner choice, §12):** a dedicated empty `house_helper_admin` database owned by `hh_owner`, `CONNECT` for `hh_restore` only. Either way there is exactly one explicit grant, never broad `PUBLIC` access.

**RST-04 - ordered restore sequence:**

1. **Connect as `hh_restore` to the authorized maintenance database.** `psql -v ON_ERROR_STOP=1`; no transaction around `CREATE DATABASE`.
2. **Verify identity before any mutation:**
   ```sql
   SELECT current_database(), session_user, current_user,
          (SELECT system_identifier::text FROM pg_control_system());
   ```
   Require `current_database()` = the authorized maintenance database, `session_user = current_user = 'hh_restore'`, and the identifier equal to the Mode B record.
3. **Create the database with connections disabled:**
   ```sql
   CREATE DATABASE "house_helper_restore_<token>"
       OWNER hh_owner TEMPLATE template0
       ENCODING 'UTF8' LC_COLLATE '<validated>' LC_CTYPE '<validated>'
       ALLOW_CONNECTIONS false;
   ```
4. **Switch to the owner explicitly:**
   ```sql
   SET ROLE hh_owner;
   SELECT session_user, current_user;          -- hh_restore / hh_owner
   ```
5. **Harden while connections are still disabled** (database-level ACL from the maintenance database):
   ```sql
   REVOKE ALL ON DATABASE "house_helper_restore_<token>" FROM PUBLIC;
   GRANT CONNECT ON DATABASE "house_helper_restore_<token>" TO hh_restore;
   ALTER DATABASE "house_helper_restore_<token>" ALLOW_CONNECTIONS true;
   ```
   The order is mandatory: revoke and grant **before** enabling connections.
6. **Verify the ACL and the absence of unintended access:**
   ```sql
   SELECT datallowconn, datacl FROM pg_database WHERE datname = 'house_helper_restore_<token>';
   ```
   Require `datallowconn = true`, no `=c`/`=CT` entry for `PUBLIC`, and `CONNECT` exactly for `hh_restore` plus the owner's implicit rights. Attempt a connection as a role other than `hh_restore`/`hh_owner` and require a refusal. **The negative test concerns content and size only; it does not require concealment of publicly accessible catalog metadata (FE-4).**
7. **Restore objects with the explicit effective owner:**
   ```
   pg_restore --exit-on-error --role=hh_owner --no-owner --no-privileges \
              --dbname="house_helper_restore_<token>" snapshot.dump
   ```
8. **Verify restored ownership and the application ACL contract:** `pg_class.relowner = hh_owner` for every table, index and sequence; `pg_namespace.nspacl`, `pg_database.datacl`, `pg_default_acl`, and the exact role attributes/memberships against `db/roles.sql` and `db/privileges.sql`. Apply `db/privileges.sql` (idempotent, database-name-parameterized) and re-verify.

**RST-05 - the pre-hardening window is closed to every ordinary client.** `CREATE DATABASE ... ALLOW_CONNECTIONS false` sets `datallowconn = false`; PostgreSQL refuses **ordinary backend connections, including ordinary superuser connections**, until `ALTER DATABASE ... ALLOW_CONNECTIONS true`. The sequence is:

```
CREATE ... ALLOW_CONNECTIONS false   ->  no ordinary client can connect
REVOKE ALL ... FROM PUBLIC           ->  PUBLIC loses CONNECT and TEMPORARY
GRANT CONNECT ... TO hh_restore      ->  the one intended login
ALTER ... ALLOW_CONNECTIONS true     ->  the database becomes usable
```

**RST-06.** Non-ordinary server-internal paths are outside this statement; no claim is made about them.

**RST-07 - parameterization.** `db/bootstrap.sql` and `db/privileges.sql` take the database name as a parameter, never a literal `house_helper` only. The test suite runs them for `house_helper_restore_<token>` and asserts the ACL contract is identical to the serving database. The restore database is dropped after verification, or kept only under an explicit Owner-approved recovery decision.

**RST-08 - acceptance `[SPEC]`:**

- Each step asserts `session_user` and `current_user` against **its own phase** of RST-01:
  - Maintenance connection and `CREATE DATABASE`: `session_user = hh_restore`, `current_user = hh_restore`.
  - Database ACL hardening after explicit `SET ROLE hh_owner`: `session_user = hh_restore`, `current_user = hh_owner`.
  - Object restoration via `pg_restore --role=hh_owner --no-owner --no-privileges`: `session_user = hh_restore`, effective `current_user = hh_owner`.
  - Bootstrap: `postgres`/`postgres`; migrations: `hh_migrate`/`hh_owner`; dump: `hh_backup`/`hh_backup`; app: `hh_app`/`hh_app`.
  No phase is reported as `(hh_owner, hh_owner)`, and no phase may be described as "always running as `hh_owner`".
- Each phase also verifies `current_database()` and `search_path = public` where ROL-06 applies; the restore script re-checks these after `pg_restore` rather than assuming the client's settings.
- A deliberately wrong `--role`, a missing membership, a wrong owner, a `PUBLIC` grant left on the restore database, or `datallowconn` still `false` makes the run **fail**, never "mostly succeed".
- A test connects as `hh_app`/`hh_purge`/`hh_backup` to the restore database **before** hardening and requires refusal; after hardening, `hh_restore` connects and the others still fail.
- Mode A/B identity tests: a wrong `system_identifier`, a missing marker, a wrong container/volume, or an unexpected project role aborts before any statement. Mode B never falls back to Mode A.
- A test asserts the tool refuses a target that is not the recorded container/volume/identifier; the n8n cluster is not contacted.

### 8.9 Temporary database inventory and lifecycle

**TMP-01 - expected inventory:**

```
expected = enrolled base inventory
         ∪ { databases authorized by active durable operation records }
```

The **base** is recorded at enrollment: `postgres`, `template0`, `template1`, the serving database and (if the Owner chooses one) the dedicated maintenance database. Temporary restore/smoke databases are added only by an operation record. **Any database not in the union fails closed**, including one whose authorization was retired.

**TMP-02 - temporary database record.** Before `CREATE DATABASE`, append `temp_db:authorized` with: operation ID and token; the **exact database name** (identity, not a prefix rule); purpose (`restore_verification`, `smoke_clone`, or a named recovery operation); the PostgreSQL `system_identifier`; expected owner (`hh_owner`) and expected privileges (`CONNECT` for the single intended login); source archive identity and digest when applicable; and intended lifecycle and cleanup conditions.

**TMP-03.** A database whose name merely *looks* temporary is not authorized. A name prefix is not evidence.

**TMP-04 - states and observed transitions:**

| State | Meaning | Observed reconciliation |
|---|---|---|
| `authorized` | Recorded, not yet created. | Absent -> proceed; present -> adopt **only** if it matches owner/ACL/`datallowconn` and is empty, else fail closed. |
| `created_fenced` | Exists, `datallowconn = false`, ACL revoked. | Present and fenced -> proceed; absent -> mark retired (absence verified); present but unfenced or unexpected -> fail closed. |
| `restoring` | `pg_restore` in progress. | Present -> it is a partial restore; the record is never advanced to `verified`; the database is recreated, not trusted. |
| `verified` | Digests, catalog contract and sequence verified on the untouched instance. | The only state from which promotion or smoke cloning may proceed. |
| `smoke_testing` | A disposable clone is used by `hh_app`. | The clone is its own record; the verification database stays untouched. |
| `cleanup_pending` | No longer needed; drop authorized. | Present -> drop after ensuring no sessions; absent -> mark retired. |
| `retired` | Absence verified and authorization retired. | Absent -> done; present -> fail closed (something recreated it). |

**TMP-05.** Interrupted creation or deletion is reconciled against actual `pg_database` **and** `pg_database_size`/owner/ACL, never inferred from the record alone. Dropping a temporary database is done by `hh_owner` after the tool's own connections are closed. `DROP DATABASE ... WITH (FORCE)` is not used to terminate arbitrary sessions; a session that remains is reported and handled by the authorized local administrator.

**TMP-06 - actor and authority matrix:**

| Operation | Authorized actor | Constraints |
|---|---|---|
| Cluster and catalog identification; `pg_database` existence; `pg_roles` metadata; `pg_auth_members` | The authorized **maintenance connection** of the recorded enrollment (local administrator or the recorded maintenance login). | Use accessible catalog views; **do not assume access to `pg_authid`**. |
| Database ACL read/write (`datacl`, `aclexplode`, `REVOKE`, `GRANT`, `ALTER DATABASE`) | **`hh_owner`** via explicit `SET ROLE`, **from the maintenance database**. | Never connect to a database while it is disabled. |
| Database size (`pg_database_size`) | **Only after** the existence check; `hh_owner` (owner `CONNECT`) or a role explicitly holding `pg_read_all_stats`. | **Supporting evidence only** - not proof of logical emptiness, ownership or identity. |
| Session termination; cluster-wide operations | The **separately authorized local administrator**. | No runtime role holds `pg_signal_backend`. |
| Temporary database creation/drop | `hh_restore` (`CREATEDB`) with `OWNER hh_owner`; drop as `hh_owner` after its own connections close. | Cluster-administrator authority only where strictly required. |
| Content validation (digests, catalog, sequence, smoke) | After hardening **and** `ALLOW_CONNECTIONS true`. | Never while the database is disabled. |

**TMP-07.** `hh_app`, `hh_backup`, `hh_purge` and `hh_provision` receive **no** additional cross-database or administrative privileges: no `CREATEDB`, no `pg_read_all_stats`, no extra `CONNECT`, no membership that would grant any of them.

**TMP-08 - ordering rules:**

1. **Existence first, then size:** `SELECT 1 FROM pg_database WHERE datname = $1` before any `pg_database_size` call. A missing database is "absent", not "size unknown".
2. **Do not assume `pg_database_size` is available to an arbitrary login.** The function requires `CONNECT` on the database or `pg_read_all_stats`; a refusal is a permission fact, not an error to work around (FE-4).
3. **Size is not content.** Emptiness, ownership and identity are established by catalog definitions (`pg_class`, `pg_namespace`, `pg_database.datdba`, `pg_roles`) and, once enabled, by logical validation.
4. **Content validation only after hardening and enablement.** No query against application tables while `datallowconn = false`.
5. Journal-authorized temporary inventory and its fail-closed rules are retained: unregistered database -> fail closed; an authorized-but-unexpected state -> reconcile against `pg_database`, never infer.

### 8.10 Promotion of a verified recovery database

**TMP-09.** Promotion is an **explicit journaled transition**, never inferred from a name or contents:

`serving_promotion:intent` (old serving database, new serving database, DSN literal, image tag, operator) -> quiesce and verify the target (identity, schema head, ACL, inventory) -> change the DSN literal and restart -> verify `current_database()`, health, one sign-in -> `serving_promotion:completed`. The old serving database becomes `retained` with its own cleanup condition.

**TMP-10.** Reversal of a promotion is **never automatic**, and the absence of an *observed* write is **not** sufficient. Reverting the DSN and retiring the intent is permitted only when the RBK-01 lossless exception is satisfied in full:

- full quiescence and effective fencing (app stopped, no sessions, no one-shot, no exclusive holder);
- independently verified absence of persistent divergence (row counts and canonical digests against the baseline, not merely "no user rows");
- independently verified absence of external effects, including any successful **or attempted** OIDC/Telegram/provider exchange;
- resolved outcomes of every uncertain operation through the reconciler (§7.8), with no `intent` lacking an `outcome`;
- the required journal and operator evidence.

If any of that evidence is missing, incomplete or ambiguous, **fail closed and preserve the potentially authoritative database**; do not revert the DSN. The conservative rollback contract of §9 (RBK-01..RBK-09) governs every recovery and promotion scenario, and this specification creates **no automatic rollback exception**.

### 8.11 Client binaries and offsite integration

**BAK-13.** `pg_dump` and `pg_restore` come from the **same digest-pinned `postgres:16.x` image** as the server, used as a disposable tool container. The application image contains the Python driver but no client binaries.

**BAK-14.** `pg_dump` does not dump roles. `db/roles.sql` and `db/privileges.sql` are checked-in, versioned artifacts, applied before/after restore and hashed into the manifest (`acl_contract_sha256`). A restore that used the archive's embedded owners or ACLs is refused.

**BAK-15.** `offsite_backup.py` gains a v2 path: `pg_dump` (no `dbtool sanitize` - a logical dump contains only live rows, so "deleted rows leave no trace" is inherent; this step is **removed**, not weakened) -> tar -> `age` -> `rclone` -> read-back -> `.commit` -> retention. The commit-record, retention, unresolved-object, `.partial`, clock-skew and exit-code semantics are unchanged.

**BAK-16.** `offsite_rehearsal.py` dispatches on `format`: v1 opens SQLite; v2 starts a disposable PostgreSQL 16 server and runs §8.6 level 2. The private `age` identity never reaches the host.

**BAK-17.** Monitoring/alert integration in `deploy/monitoring/` is owned elsewhere; any new manifest fields are additive and that contract is not modified here.

---

## 9. Conservative rollback and recovery

### 9.1 The divergence boundary

**RBK-01.**

| Point | Claim |
|---|---|
| **Before the application permission handoff** (`hh_app` has no `CONNECT`, C0-C3) | SQLite can be restored from its verified frozen recovery point, **provided** the old authoritative source stayed fenced (no old writer, no timer, no volume mounted). |
| **After the application permission handoff** (H1), including C4 | **Potential divergence is assumed**, even if no successful write has been observed. There is no automatic lossless return. |
| **Any lossless exception** | Requires **full quiescence** (app stopped, no sessions, no one-shots) **and** independently verified absence of persistent writes **and** external effects. Missing or contradictory evidence means **not lossless**. |
| **Unknown outcome** | Fails closed: treated as divergence. |
| **After PostgreSQL-specific user or Telegram activity** | Returning to SQLite is a **separately authorized, potentially lossy** recovery operation with written Owner acceptance of the lost delta. |
| **72 hours** | Stabilization and observation only, beginning after the deliberate write-enable decision. Never rollback eligibility. |

**RBK-02.** The C4 "provably lossless" claim of an earlier draft is withdrawn: the public app is not read-only. `GET /auth/telegram/login` writes an `oidc_states` row before redirecting `[T]`; `/auth/invite/start` verifies an invitation but can also begin a login. "Zero rows" is therefore not a stable invariant.

**RBK-03.** "Independently verified absence of persistent writes" means: row counts and canonical digests of every table equal the post-migration baseline (not merely "no user rows"); no admission ledger cutover row; no sessions; no `oidc_states`; no invitation consumed; no external effect recorded. "No external effect" includes any successful **or attempted** OIDC/Telegram/provider exchange. If that cannot be excluded, the answer is **not lossless**.

### 9.2 Three journal events, kept distinct

**RBK-04.**

| Event | Meaning | Evidence |
|---|---|---|
| `PERMISSION_HANDOFF` | `CONNECT` granted to a role. | `has_database_privilege` observed. |
| `FIRST_PERSISTENT_WRITE` | The first commit that changes application state. | Counts/digests against the baseline; transaction logs or the reconciler's observation. |
| `FIRST_EXTERNAL_EFFECT` | The first outbound effect (OIDC redirect, Telegram API call, bot reply). | The operator's recorded observation or the service log. |

**RBK-05.** A handoff is not a write; a write is not an external effect; an external effect may exist with no write (an OIDC redirect) and vice versa. The rollback decision reads the journal, not the assumption that "the bot was off".

### 9.3 Rollback eligibility

**RBK-06.**

| Phase | Artefact | Claim |
|---|---|---|
| C0-C3 | SQLite pair plus old image | **Lossless**; PostgreSQL is discarded. No write was possible. |
| C4 | SQLite pair | **Never assumed lossless.** Permitted only under RBK-01's full-quiescence exception with independently verified evidence; then abandon PostgreSQL. |
| C5-C6 (any authoritative write, any bot reply) | Pair plus journal | **Lossy.** Returning to SQLite requires explicit written acceptance of the lost delta (invitations, sessions, links, conversations, profile). A PostgreSQL backup is PostgreSQL recovery, not a reverse migration. |
| C7-C8 | PostgreSQL backups | Rollback closed, or requires a newly authorized reverse project. |

**RBK-07.** 72 hours is observation and stabilization, not eligibility. It begins at C7, after the deliberate write-enable decision, and never extends a rollback promise.

### 9.4 Recovery paths

**RBK-08.** Recovery is always one of: (a) destroy and recreate the empty database and re-run initialization (C3); (b) create a new database, restore into it, verify, and switch the DSN (§8.8, TMP-09); (c) a replacement container/volume and §8.6 level 3. There is no database rename, no in-place `--clean` restore, and no reverse migration.

**RBK-09.** Any uncertain step is resolved by the reconciler (§7.8) before the machine advances. A missing journal record is never evidence of non-occurrence.

---

## 10. PostgreSQL-first development and CI

### 10.1 Policy

**CI-01.** PostgreSQL becomes the **default** for application integration tests, current-head migrations and future development.

**CI-02.** SQLite remains only for explicitly scoped historical/dialect tests: the `0004` `writable_schema` path, `-wal`/`-shm` restore probes, the SQLite `dbtool`, SQLite `PRAGMA`/batch behavior, and historical fixtures pinned to a revision.

**CI-03.** No SQLite test may depend on a future DYN migration. No new migration may be SQLite-only.

**CI-04.** `psycopg[binary]` is added to `requirements.txt`, pinned, so the runtime image and CI use the tested driver.

### 10.2 CI shape

**CI-05.** Add to the CI workflow:

- a `postgres:16.x` service (digest-pinned) mapped to loopback **55432** (the harness refuses `5432` `[T]`), health-checked with `pg_isready`, with a named superuser and a `postgres` maintenance database;
- `HOUSE_HELPER_TEST_POSTGRES_URL=postgresql+psycopg://...@127.0.0.1:55432/postgres` for both matrix legs (3.10 and 3.12); no query parameters, no routing environment variables;
- `pytest` with the PostgreSQL suites **required**, not skipped;
- the existing `compileall` and offline guard (loopback only, so the service is reachable).

**CI-06 - preflight.** `pg_isready`; `server_version_num` in `160000-169999`; the URL has no query parameters; the driver is installed; the test role can connect.

**CI-07 - collection assertions.** `pytest --collect-only` proves the PostgreSQL-marked tests are collected; `-ra` is recorded; a session hook **fails** the run if a PostgreSQL-marked test is skipped while `HOUSE_HELPER_TEST_POSTGRES_URL` is set. Zero unexpected PostgreSQL skips.

### 10.3 Test-policy corrections

**CI-08.** All direct-head fixture helpers, not only a shared `make_database`, MUST lose the `revision="head"` default: `tests/admin_support.py`, `tests/auth_support.py`, `tests/email_support.py`, `tests/persistence_support.py`, `tests/deploy_support.py`, plus the opt-in PostgreSQL modules. A historical test states its revision. A dedicated `make_head_database` is the only head fixture.

**CI-09.** A chain test walks `0001 -> 0014` from empty on real PostgreSQL 16 (and on SQLite), asserting after each step that the revision is the one just applied. A second `upgrade head` is a no-op.

**CI-10.** The head-vs-models drift test runs on **PostgreSQL** (live catalog), not only on rendered SQL. `alembic check` is a CI step.

**CI-11.** `0004` offline: the SQLite offline call MUST fail with its documented error; that refusal is the asserted behavior. PostgreSQL offline SQL for `0004` is rendered and checked. The chain test does not attempt SQLite offline for `0004`.

**CI-12.** `0013` and `0014` are tested on both dialect branches.

**CI-13.** The opt-in PostgreSQL harness stops being opt-in in CI; locally it remains opt-in so a developer without Docker can run the SQLite-scoped subset.

**CI-14.** The integration fixtures (`web_support`, `auth_support`, `email_support`, `invite_support`, `chat_support`, `persistence_support`, `admin_support`) gain a backend-parameterized database fixture. In CI the default is PostgreSQL; locally SQLite is the fallback only for the scoped subset.

**CI-15.** A test asserts the pool relation still holds: `MAX_CONCURRENT_UNITS_OF_WORK = 10` < (`pool_size` + `max_overflow`) `[T]`, with an explicit PostgreSQL pool configuration rather than SQLAlchemy defaults.

**CI-16.** An ACL test suite runs as each role and asserts §3.6 on real PostgreSQL 16. A "full-suite-under-`hh_app`" gate proves the integration suite passes without DDL or ownership; any DDL-requiring test asks for `hh_owner` explicitly. A migration test proves an interrupted chain leaves zero tables.

### 10.4 Separate test credentials

**CI-17.** Scratch-database creation and migrations use the maintenance role; the application suite connects as the restricted app role; provisioning tests use a provisioning role. Test role names are unique per run (roles are cluster-wide) and dropped in teardown. Production role names are never reused on a shared instance. The disposable **smoke clone** is accessed with the `hh_app` credential, never the migration or provisioning credential.

---

## 11. Implementation stages 1-9 and acceptance gates

**STG-01.** Stages 1-6 use no Production access or credential. Stage 7 is a separately authorized read-only inventory. Stage 8 is a separately authorized Production cutover. Stage 9 is stabilization and Owner acceptance.

Every acceptance check below is `[SPEC]`. **No stage has been executed.**

### Stage 1 - Local PostgreSQL 16 and CI

Scope: pinned PostgreSQL 16 service on 55432 in both matrix legs; `psycopg` pinned; full chain from empty; `alembic check` clean; PostgreSQL-rendered schema test against the live catalog; previously opt-in suites green; SQLite suite unchanged; offline guard still passes.

Acceptance: CI green on 3.10 and 3.12 with a real server; no skipped PostgreSQL suite; chain/transactionality test green; separate per-run migration/provisioning/app credentials and the `hh_app` smoke-clone credential rule. (CI-01..CI-17)

### Stage 2 - Application compatibility, schema, roles, ACLs

Scope: `dbtool` accepts PostgreSQL (target plus lock path); start gate asserts role/database/`search_path`/`application_name`; `db/roles.sql` and `db/privileges.sql` idempotent; ACL matrix green; `alembic_version` `SELECT`-only; sequence `USAGE`-only; no `pg_read_all_data`; identity `lower()` conformance matrix on the chosen locale.

Acceptance: positive and negative ACL tests on real PostgreSQL 16 (§3.6); full integration suite as `hh_app`; a planted schema difference is detected; Mode A/B enrollment tests; predefined-role exclusion; idempotent partial enrollment; Mode B fail-closed on identifier/marker/container/volume/inventory mismatch; no automatic fallback; exact `(session_user, current_user)` per restore phase; pre-hardening access refused; maintenance-database `CONNECT` only for `hh_restore`; exact-grant test for `hh_purge`; schema initialization behind the E3/E4 connection fence. (ROL-01..ROL-26, AUTH-01..AUTH-02, MEM-01..MEM-09, ENR-01..ENR-17, ENR-20..ENR-22, BOOT-01..BOOT-15, RST-01..RST-08; restore identity is exercised again under gate 4)

### Stage 3 - Fresh-account bootstrap and Telegram cutover safety

Scope: two users via `enroll-user` on a scratch PostgreSQL; invitation claim; bot pairing; isolation negatives; admission-ledger semantics on a fresh database (pre-cutover updates refused; one cutover row; no double answer); pending-update policy A or B rehearsed against a fake Bot API **only after authorization**; `409` detection; bot-disabled startup.

Acceptance: §5.5.1 green at C5 (no bot required) and §5.5.2 green at C6; ledger tests; one-poller evidence; no external call before the Owner-authorized step; the Telegram case matrix (§6.4) including acknowledged-but-unclaimed silent loss; commands, `/start` and callbacks processed; no discard authorized without the Owner. (BOOT-16..BOOT-23, TG-01..TG-10)

### Stage 4 - PostgreSQL backup, off-host, isolated restore

Scope: v2 archive; snapshot-consistent manifest; normalized object contract; sequence invariant; isolated `--network none` restore; digest equality; `hh_app` smoke; v1 still readable; commit record, retention and exit codes unchanged; truncated or wrong-version archive refused; rehearsal tool dispatches on format.

Acceptance: v2 round-trip and v1 backward compatibility; a real app-role recovery proof; failure injection (truncated dump, wrong tool version, sequence behind rows); scoped TOC extraction with exactly one `SEQUENCE SET`; strict parser; raw-pair exact comparison; all §8.7 fixtures; `ALLOW_CONNECTIONS false` window; smoke on a discarded clone; ownership and ACL post-restore assertions; the journal-authorized temporary-database inventory and its states. (BAK-01..BAK-17, SEQ-01..SEQ-10, RST-01..RST-08, TMP-01..TMP-08)

### Stage 5 - Maintenance, DSN and deployment tooling

Scope: real Docker and real PostgreSQL rig: per-role one-shots; systemd stop/verify; catch-up observation; orphan removal; journal intent/outcome/reconcile; lock handoff; DSN read-back; a nonparticipating writer cannot write while fenced; a timer firing during the fence fails closed; disk-full, wrong password, missing role.

Acceptance: C1->C4 and C4->C0 exercised end to end; no transition without an observed outcome; the four lock modes; the separate journal lock; H1/H2/H3 handoffs observed; timer re-arm with actual missed-schedule evidence; read-back allowlist; authorization-container identity updates (Docker re-creation and PG 16 patch) journaled and verified; promotion of a verified recovery database is an explicit journaled transition and its reversal obeys TMP-10. (LCK-01..LCK-13, HOF-01..HOF-04, ST-01..ST-04, AUTH-03..AUTH-06, ENR-18..ENR-19, TMP-09..TMP-10)

### Stage 6 - Synthetic fresh-install and failure-injection rehearsal

Scope: from an empty host to a serving PostgreSQL app with two synthetic users; `kill -9` at each journal step; connection loss; disk full; SIGTERM during handoff; expired sequence; wrong image. Each ends in a defined state and never "serving something unverified". The SQLite pair is unchanged and restorable; rollback before the boundary is lossless; after it, only forward-fix.

Acceptance: every injected failure maps to a state; no orphaned credential or container; the journal replays to the observed state; the conservative rollback cases (handoff-only, handoff plus OIDC state, first write, external effect, unknown outcome) each map to a journal state and never to an assumed lossless return; a failed promotion with incomplete evidence fails closed and preserves the potentially authoritative database. (ST-01, RBK-01..RBK-09, TMP-10)

### Stage 7 - Separately authorized Production inventory (read-only)

Scope: image tag and SQLite revision; timers/units; containers/volumes/disk; bot token singleton and `getWebhookInfo`; DNS/Traefik; PostgreSQL image availability; off-host destination and last run; user availability. No write.

Acceptance: a written inventory that either confirms the plan or revises it before any window; positive cluster identification (container/volume/identifier) and evidence that the shared n8n cluster is untouched. (ARC-20, ENR-20..ENR-22)

### Stage 8 - Separately authorized Production cutover and enrollment

Scope: execute C1->C6 with the Owner present and a pre-agreed abort rule; record every journal transition; verify §5.5.1 at C5 and §5.5.2 at C6 in Production.

Acceptance: every transition carries its observed verification; the pending-update policy is recorded; rollback eligibility is read from the journal, not assumed; Production remains untouched outside the authorized window. (GOV-06, PRD-01, PRD-02, STG-01)

### Stage 9 - Monitoring, stabilization, Owner acceptance

Scope: 72 hours observation; one scheduled purge; one scheduled backup; one offsite run; one isolated restore; readiness daily; then C8 and the retention decision.

Acceptance: the observations are recorded; monitoring is green; the SQLite pair's retention action is executed; the closure note is written.

### Gate matrix

| Gate | Requirements | Evidence status |
|---|---|---|
| 1. PostgreSQL in CI | CI-01..CI-17 | NOT EXECUTED |
| 2. Roles, schema and ACLs | ROL-01..ROL-26, AUTH-01..AUTH-02, MEM-01..MEM-09, ENR-01..ENR-17, ENR-20..ENR-22, BOOT-01..BOOT-15, RST-01..RST-08 (phase identities re-exercised) | NOT EXECUTED |
| 3. Bootstrap and Telegram | BOOT-16..BOOT-23, TG-01..TG-10 | NOT EXECUTED |
| 4. Backup and restore | BAK-01..BAK-17, SEQ-01..SEQ-10, RST-01..RST-08, TMP-01..TMP-08 | NOT EXECUTED |
| 5. Maintenance and deployment | LCK-01..LCK-13, HOF-01..HOF-04, ST-01..ST-04, AUTH-03..AUTH-06, ENR-18..ENR-19, TMP-09..TMP-10 | NOT EXECUTED |
| 6. Failure injection | ST-01, RBK-01..RBK-09, TMP-10 (failure cases) | NOT EXECUTED |
| 7. Production inventory | ARC-20, ENR-20..ENR-22 | NOT AUTHORIZED |
| 8. Production cutover | GOV-06, PRD-01, PRD-02, STG-01 | NOT AUTHORIZED |
| 9. Acceptance and retention | PRD-03 (documentation/governance review only), PRD-06, OD-1..OD-9 (open-choice closure); PRD-04/PRD-05 preservation review | NOT STARTED |

Gate 9 verifies decision closure and records Owner acceptance. Its PRD-03 reference is a **documentation/governance review**, not an implementation gate, and it does **not** authorize deferring any open decision past its first dependent step (PRD-03, PRD-06).

### Requirement-to-gate coverage check

Every normative identifier is mapped below. A range means every identifier in it shares the primary gate; identifiers named again in another gate are exercised there as a secondary check.

| Requirement family | Identifiers | Primary gate | Secondary / note |
|---|---|---|---|
| Governance | GOV-01..GOV-05, GOV-07..GOV-08 | 8 | Scope and exclusions preserved by review |
| Governance (runtime constraints) | GOV-06 | 8 | Production untouched |
| Governance (constraints) | GOV-10..GOV-13 | 2 (RLS), 5 (TLS/DSN/patch), 4 (PITR/replacement) | See §12.5/§12.6 |
| Governance (process) | GOV-09, GOV-14 | none | Process/review constraints; not implementation-tested (see below) |
| Architecture and topology | ARC-01..ARC-04 | 2, 5 | Network, DSN single source |
| Baseline facts | ARC-05..ARC-19 | 1, 2 | Re-verified, not assumed |
| Production inventory facts | ARC-20 | 7 | Read-only |
| Roles and database | ROL-01..ROL-08 | 2 | |
| Privileges and ACLs | ROL-09..ROL-26 | 2 | ROL-16 handoffs re-checked in 5 |
| Authentication | AUTH-01..AUTH-02 | 2 | |
| Authentication and read-back | AUTH-03..AUTH-06 | 5 | |
| Predefined-role membership | MEM-01..MEM-09 | 2 | |
| Enrollment and resume | ENR-01..ENR-17 | 2 | |
| Identity updates | ENR-18..ENR-19 | 5 | |
| Cluster identity / n8n | ENR-20..ENR-22 | 2, 7 | Non-interference evidence at 7 |
| Schema initialization | BOOT-01..BOOT-15 | 2 | E3/E4 fence, chain, session |
| Account and bot acceptance | BOOT-16..BOOT-23 | 3 | BOOT-16/BOOT-21 split across C5/C6 |
| Telegram | TG-01..TG-10 | 3 | TG-07/TG-08 also C6 evidence |
| Locks and journal | LCK-01..LCK-13 | 5 | |
| Handoffs and timers | HOF-01..HOF-04 | 5 | |
| DSN and states | ST-01..ST-04 | 5 | |
| Backup and archive | BAK-01..BAK-17 | 4 | |
| Sequence | SEQ-01..SEQ-10 | 4 | |
| Restore identity | RST-01..RST-08 | 4 | Identity re-exercised in 2 |
| Temporary databases | TMP-01..TMP-08 | 4 | |
| Promotion | TMP-09..TMP-10 | 5 | Failure cases in 6 |
| Rollback | RBK-01..RBK-09 | 6 | Contract also applied in 5 |
| CI and test policy | CI-01..CI-17 | 1 | |
| Stage governance | STG-01 | 8 | |
| Decision and authorization | PRD-01..PRD-02 | 8 | Production authorization |
| Decision closure | PRD-03, PRD-06, OD-1..OD-9 | 9 | OD-1 before C6; OD-2 before gate 4 restore work; OD-4 resolved at C8 |
| Binding decisions and constraints | PRD-04..PRD-05 | Review at 2, 5, 8 | Preservation, not closure |

**Process-only identifiers (reported, not silently omitted):** GOV-09 (the DYN pause is a scope prohibition verified by review, not an implementation test), GOV-14 (repository conduct), and PRD-03 (the decision-classification rule) are document/governance controls with no implementation gate. RSK-01..RSK-13 are risk records reviewed at gate 9, not acceptance requirements. W-01..W-25 are the withdrawn-statement register (§14.3), not requirements. No other identifier is unmappable.

---

## 12. Production prerequisites and separately authorized decisions

### 12.1 Production remains untouched

**PRD-01.** Nothing in stages 1-6 touches Production. Production changes require, in order: a written read-only inventory (stage 7), an Owner-authorized window (stage 8), and an accepted closure (stage 9).

**PRD-02.** The cutover is not authorized by this document. This document is an architecture specification, not an execution order.

### 12.2 Prerequisites before a window is requested

- Both users are available to re-enroll and re-pair (otherwise the cutover cannot complete).
- The old image tag, the old `deploy/` checkout and the frozen SQLite pair are preserved and restorable.
- The PostgreSQL image is available and digest-pinned; disk and memory are measured, not assumed.
- The off-host destination is reachable and its last run is known.
- A pending-update policy is selected and recorded.
- The maintenance-database option is selected.
- Monitoring ownership for the new failure modes (database down, disk, backup) is agreed.

### 12.3 Decision classification

**PRD-03.** Every decision is recorded in exactly **one** of three categories below. A recommendation is never an approval, and the same decision MUST NOT appear in two categories. Where a topic mixes a settled constraint with an open detail, the settled part is in §12.5 and only the genuinely open detail is in §12.6.

**Each open Owner decision MUST be explicitly approved and recorded before its first dependent operational step. Gate 9 verifies decision closure and does not defer prerequisites.**

### 12.4 Binding Owner decisions already made

**PRD-04.** Binding by `HH-DB-01-DATA-RESET-01` (and not open to implementation choice):

- Existing SQLite application data is abandoned: no ETL, no import, no source-revision strategy and no preservation of old application records in the target.
- Two fresh accounts are recreated through the supported invitation and Telegram OIDC flows.
- Email login remains disabled; no email-auth redesign.
- Existing SQLite recovery assets are preserved pending a later retention decision.
- Production remains untouched until a separately authorized inventory and window.
- `KH-F01-B2-DYN-01` remains paused.
- The financial-data access-isolation gate remains mandatory before `HH-BUDGET-01`.

### 12.5 Approved architectural constraints (Architecture PASS)

**PRD-05.** These are established by the accepted architecture. They are not per-deployment Owner choices; changing one is an architecture revision, not a configuration decision.

- PostgreSQL 16 as the tested and deployed target.
- One dedicated database container with its own dedicated `PGDATA` volume.
- A dedicated `internal: true` database network; the database service publishes **no port**.
- The `house_helper` database with a hardened `public` schema and the role/privilege contract of §3.
- Seven project roles (six logins plus the `NOLOGIN` owner) and the three connection handoffs.
- No row-level security at cutover; application-enforced per-user isolation.
- The conservative rollback contract (§9), the normalized backup contract and archive-derived sequence state (§8).

### 12.6 Open Owner choices

**PRD-06.** The following remain **OPEN**. They are not approved by the R2.4 PASS, and no implementation may treat a recommendation as an acceptance.

| # | Decision | Recommendation (not approved) | Consequence if different |
|---|---|---|---|
| **OD-1** | **Telegram pending-update policy**: A (refuse-with-resend), B (silent drain), or none. | **None until after enrollment; then A.** | B needs new tooling and is less visible, not less destructive. Without a selection the bot stays disabled. |
| **OD-2** | **Administrative maintenance database**: `postgres` with a restricted `CONNECT` for `hh_restore`, or a dedicated `house_helper_admin` owned by `hh_owner`. | `postgres` with exactly one explicit grant. | Both are architecturally permitted; the enrollment record and `db/bootstrap.sql` are parameterized. Never broad `PUBLIC` access. |
| **OD-3** | **Production cutover window and downtime.** | Measure in stage 6; promise no number now. | Enrollment is interactive and dominates the window. |
| **OD-4** | **Retention of the frozen SQLite pair and old image after C8.** | Keep 30 days, then archive offline and destroy. | Bounds private data; keeps a cutover-evidence window. |
| **OD-5** | **Retention and monitoring arrangements** for PostgreSQL backups and the new failure modes. | Coordinate with the existing `deploy/monitoring/` owner; no change proposed here. | New failure modes need an alert path. |
| **OD-6** | **TLS on the private database network.** | No for a single-tenant host; revisit if the host is shared. | Residual: bridge traffic is unencrypted (RSK-08). |
| **OD-7** | **Remaining PostgreSQL patch and configuration details**: patch cadence, the image-bump procedure, and any optional configuration **beyond** the binding topology constraints of §12.5. | Patch by a deliberate digest-pinned image bump plus backup plus rehearsal. | The binding topology (dedicated PostgreSQL 16 container/volume, `internal` network, no published database port) is **not** reopened by this decision; only the patch/configuration details are open. |
| **OD-8** | **Where the app password lives and how it is operated**: the credential-supply mechanism within the binding ARC-03/AUTH-02..AUTH-04 controls, plus the production deployment and rotation procedure. | `PGPASSWORD` or `PGPASSFILE` in a root-only per-role credential source; DSN literal without a password in `compose.yaml`. | The single-source DSN property and the ARC-03 credential controls are binding; the credential mechanism and its operational procedure are not. Any choice must satisfy ARC-03 and AUTH-02..AUTH-04. |
| **OD-9** | **Availability of both users to re-enroll and re-pair.** | Confirm before stage 8. | The cutover cannot complete without them. |

**First dependent step (PRD-06).** The approval requirement of PRD-03 applies as follows, and each approval is recorded in the journal before the step:

- **OD-1** before Production Telegram bot enablement (C6).
- **OD-2** before any dependent Production provisioning or recovery operation (the first restore or enrollment that uses the maintenance database).
- **OD-8** before any Production credential is configured or used.
- **OD-9** before the interactive enrollment window begins.
- **OD-3** before the Production window is declared.
- **OD-4** before any retention action at C8.
- **OD-5** before PostgreSQL backup/monitoring scheduling is enabled.
- **OD-6** before the database is first reached by the application in Production.
- **OD-7** before the first PostgreSQL image bump in Production.

Gate 9 verifies that every decision is closed and recorded; it does not authorize any dependent step to proceed while its decision remains open.

---

## 13. Residual risks and deferred work

### 13.1 Residual risks (stated, not hidden)

| # | Residual | Consequence | Mitigation / bound |
|---|---|---|---|
| **RSK-01** | One shared `hh_app` login can read every row; per-user isolation is application-enforced, not RLS. | A compromised app credential reads all user data. | The financial-data gate remains mandatory before `HH-BUDGET-01` (ROL-25). |
| **RSK-02** | The host `flock` is cooperative. | A `docker run` that ignores it can write while fenced. | The privilege fence (`CONNECT`) is the server-enforced layer (ROL-20, LCK-11). |
| **RSK-03** | `hh_migrate` and `hh_restore` can connect during maintenance by design. | A stray `psql` as `hh_migrate` can write. | Root-only credential, used only in the window. |
| **RSK-04** | The exact rendering of `pg_dump`/`pg_restore` (TOC owner field, `SEQUENCE SET` SQL) is version-specific. | A different rendering could break the parser. | Pinned by implementation tests against the exact PostgreSQL 16 minor; a different rendering is a test failure and a deliberate fixture change. |
| **RSK-05** | Mode A partial failure relies on idempotent re-run under the same intent. | A conflicting pre-existing role or foreign object stops enrollment. | Stop and require explicit remediation; never auto-repair (ENR-08, ENR-12). |
| **RSK-06** | Sequence gaps are normal and accepted. | Message ids are not contiguous. | `next > max(id)` is the only invariant; ahead is recorded, behind is refused. |
| **RSK-07** | `pg_dump` is not PITR and not byte-deterministic. | Recovery point granularity is the dump. | Stated in BAK-05; no PITR promise. |
| **RSK-08** | The database network is unencrypted. | Traffic on the internal bridge is readable by a host-level actor. | OD-6; single-tenant assumption. |
| **RSK-09** | The old SQLite pair is only as good as its last verified backup and the preserved old image. | A lossy rollback may be impossible if either is gone. | C2 gate verifies the pair and rehearses a restore. |
| **RSK-10** | Exactly one Telegram poller is an operational property, not an enforced one. | A second consumer gets `409`; replies may be delayed. | TG-07/TG-08 detection; one Web instance. |
| **RSK-11** | Telegram acknowledged-but-unclaimed updates are a **silent loss**. | A person gets no answer and no record exists. | Bounded by the two users and the 24-hour retention; stated in TG-04. |
| **RSK-12** | No production monitoring exists for the new failure modes. | A database or backup failure may go unnoticed. | OD-5; `deploy/monitoring/` is owned elsewhere. |
| **RSK-13** | A PostgreSQL major-version change or `pg_upgrade` is a replacement cluster. | It requires a new authorized enrollment, not a patch. | ENR-19. |

### 13.2 Deferred work (explicitly not done here)

- **DYN consolidation** (`KH-F01-B2-DYN-01`) remains PAUSED. The deferred comparison is `reference_identity_events` vs `reference_cache_events`; `telegram_update_admissions` is not part of it.
- **RLS and per-user database isolation** are deferred behind the `HH-BUDGET-01` gate.
- **Point-in-time recovery, logical replication, high availability** are not designed.
- **TLS inside the database network** is deferred to OD-6.
- **Monitoring and alerting** for the new failure modes are deferred to OD-5 and the `deploy/monitoring/` owner.
- **Retention of the SQLite pair** is deferred to OD-4.
- **A deployed beta** is not part of this architecture; the current deployment remains the SQLite alpha.

---

## 14. Supersession and traceability appendix

**Evidence provenance (clarification).** The lineage and matrices below record **document relationships**, not a verified chain of custody. The R2-R2.4 texts were recovered read-only from a prior-session transcript, not from repository files; the Final Codex R2.4 PASS and the four Freeze Errata were supplied through the task brief and were not independently re-verified against the original artifact. The consolidating agent did not perform a complete historical source-fidelity comparison. The D1 Documentation Acceptance Review examined this consolidated file but did not independently compare all original sources. Architecture PASS is a reported and accepted project decision; documentation source-fidelity verification, Owner acceptance and implementation testing remain separate, unclaimed evidence layers.

### 14.1 Document lineage

| Document | Role | Status at freeze |
|---|---|---|
| **R2** - Clean-Slate PostgreSQL Cutover | First clean-slate architecture; six Codex blockers found. | Superseded in the areas listed in §14.2; its retained decisions are incorporated. |
| **R2.1** - Targeted Remediation | Resolved six Codex blockers (B1-B6) plus narrow Telegram/CI/network/email corrections. | Superseded by R2.2/R2.3/R2.4 where listed; otherwise accepted. |
| **R2.2** - Bootstrap, Restore and Sequence Corrections | Mode A/B enrollment, exact restore identity, `SET ROLE` settings, raw sequence pair, scoped extraction, purge default removal, ownership/ACL semantics, durable-turn boundary. | Superseded by R2.3/R2.4 where listed; otherwise accepted. |
| **R2.3** - State-Transition and Parser Addendum | Dispatch/resume, identity updates, temporary database inventory, parser envelope, three factual corrections. | Superseded by R2.4 in E3/E4, temp-DB authority, membership; otherwise accepted. |
| **R2.4** - Final Bootstrap Fence and Role-Check Amendment | E3 connection fence, E4 allowed-state matrix, reconciliation authority, MEMBER/USAGE/SET. | Accepted in full by the reported Final Codex R2.4 Delta Review PASS (see provenance above). |
| **Four Codex-approved errata** | Nonblocking documentation clarifications. | Integrated as FE-1..FE-4 (§14.4). |
| **D1 Documentation Acceptance Review** | Seven substantive findings and four editorial corrections against this consolidated file. | Remediated in place (§14.8); independent recheck pending. |
| **D2 Documentation Recheck** | D1 recheck FAIL on D1-02 and D1-06, plus three narrow editorial issues. | Remediated in place (§14.9); final independent recheck pending. |
| **This specification v1.0** | Single authoritative consolidation. | **FROZEN SPECIFICATION v1.0 - DOCUMENTATION D2 REMEDIATED / CODEX PASS (R2.4 ARCHITECTURE, D2 DOCUMENTATION) / OWNER ACCEPT (2026-11-14) / IMPLEMENTATION NOT STARTED.** |

### 14.2 Supersession matrix

| Topic | R2 | R2.1 | R2.2 | R2.3 | R2.4 | Frozen (this document) |
|---|---|---|---|---|---|---|
| Purge ACL | "`SELECT` is enough" | Ten-table matrix plus column `UPDATE` | Accepted; default `SELECT` removed; no table-wide fallback | Unchanged | Unchanged | ROL-11, ROL-14, ROL-21 |
| Default privileges | `SELECT` to `hh_purge` and `hh_backup` | - | `hh_backup` only | Unchanged | Unchanged | ROL-14 (FE-2) |
| Locks | One flock | Four modes plus separate journal lock | Accepted | Unchanged | Unchanged | LCK-01..LCK-13 |
| Handoffs | One handoff | H1/H2/H3 | Accepted | Unchanged | Unchanged | HOF-01..HOF-02 |
| Sequence | Live read | Archive-derived | Raw `(last_value, is_called)`; scoped extraction | Parser envelope | Unchanged | SEQ-01..SEQ-10 |
| Restore identity | Owner for all | Ordered roles; `--role` | Mode A/B; exact matrix; pre-hardening window | Temp-DB inventory; promotion | Actor/authority; MEMBER/USAGE/SET | RST-01..RST-08, TMP-01..TMP-10, MEM-01..MEM-09 |
| Session settings | Implied | Implied | Explicit after `SET ROLE` | Unchanged | Unchanged | ROL-05..ROL-07 |
| Rollback | C4 lossless | Conservative contract | Accepted | Unchanged | Unchanged | RBK-01..RBK-09 |
| Archive contract | Literal count | Normalized contract | Ownership/ACL semantics corrected | Unchanged | Unchanged | BAK-06..BAK-11 |
| Telegram | Universal refusal/ack | Scoping plus ack gap | Durable-turn boundary | Acknowledgment/claim matrix | Unchanged | TG-02..TG-06 |
| Connection fence at creation | - | - | `ALLOW_CONNECTIONS false` for restore | - | E3 sequence; E4 matrix | BOOT-01..BOOT-08 (FE-1, FE-3) |
| Predefined roles | - | - | "No membership" | Implicit `pg_database_owner` recognized | MEMBER/USAGE/SET split | MEM-01..MEM-09 |
| CI/credentials | Placeholder | Accepted contract | Accepted | Unchanged | Unchanged | CI-01..CI-17 |

### 14.3 Withdrawn statements register

These statements appear in the source amendments and are **withdrawn**. They MUST NOT be reintroduced as requirements, tests or review criteria.

| # | Withdrawn statement | Source | Replaced by |
|---|---|---|---|
| W-01 | "`FOR UPDATE SKIP LOCKED` needs `SELECT` only." | R2 §3.4 | ROL-11 (column `UPDATE` required) |
| W-02 | "If PostgreSQL demands table-level `UPDATE`, widen." | R2.1 §B1.2 | ROL-21 (finding, not widening) |
| W-03 | Default `SELECT ON TABLES ... TO hh_purge, hh_backup`. | R2 §3.3 | ROL-14 (`hh_backup` only) |
| W-04 | One flock covers every operation, including journal writes. | R2 §5.1 | LCK-01, LCK-06 |
| W-05 | "`GRANT CONNECT` plus zero rows is provably lossless." | R2 §5.7 C4 | RBK-01, RBK-02 |
| W-06 | "Re-arming produces exactly one catch-up run." | R2 §5.10 | HOF-04 |
| W-07 | Read `last_value` from the live database after the dump. | R2 §4.3/§6.3 | SEQ-01, SEQ-03 |
| W-08 | Use `pg_sequence_last_value()` to reconstruct the pair. | R2.1 §B3.2 | SEQ-01, SEQ-02 |
| W-09 | TOC must contain a literal count of 20 tables/28 indexes and nothing else. | R2 §6.2 | BAK-08..BAK-10 |
| W-10 | "The archive is ownership- and privilege-neutral." | R2.1 §B6.2 | BAK-11 |
| W-11 | All pending updates are refused / each gets one reply / fetched updates are always acknowledged. | R2 §5.3-§5.4 | TG-02, TG-04 |
| W-12 | "An unfinished handler is recorded as not answered." | R2 §5.5 | TG-04, TG-05 |
| W-13 | A committed claim proves Telegram acknowledged the update. | R2.2 §6.3 | TG-04 |
| W-14 | `system_identifier`/marker required before the first enrollment. | R2.1 §B4.1 | ENR-02..ENR-06 (Mode A) |
| W-15 | Restoration always runs as `current_user = hh_owner`. | R2.1 §B4.2 | RST-01, RST-02 |
| W-16 | `SET ROLE` loads the target role's `ALTER ROLE` defaults. | R2.1 §B4.2 | ROL-05, ROL-06 |
| W-17 | A fresh cluster is required on every run; no resume. | R2.2 §1.2 | ENR-09..ENR-12 |
| W-18 | Mode B requires absolute database-inventory equality. | R2.2 §1.3 | TMP-01..TMP-03 |
| W-19 | A superuser can always connect while `datallowconn = false`. | R2.2 §2.4 | RST-05 |
| W-20 | Optional `SEQUENCE` definition entry in the extraction list. | R2.2 §5.2 | SEQ-04 |
| W-21 | "No project role is a member of any predefined role." | R2.2 §1.2 | MEM-01..MEM-06 |
| W-22 | E4 permits "any intermediate connection/ACL combination". | R2.3 §1.2 | BOOT-07 |
| W-23 | Temporary-database reconciliation may be inferred from the record alone. | R2.3 §3.3 | TMP-04, TMP-05 |
| W-24 | `hh_restore` receives `CONNECT` on `house_helper`. | R2 §3.3 | RST-03 (no `CONNECT` on the live database) |
| W-25 | The `postgres` maintenance database is the only possible choice. | R2.1 §B4.2 | RST-03, OD-2 (parameterized) |

### 14.4 Freeze errata incorporation

| Erratum | Requirement | Where incorporated |
|---|---|---|
| **FE-1** - Default `PUBLIC` `CONNECT`/`TEMPORARY` after creation are a recognized intermediate state only while `datallowconn = false`; they must be revoked and verified before connections are enabled; `PUBLIC` `CONNECT` after enablement fails closed. | BOOT-01, BOOT-07, ROL-22, ROL-23 | §3.4, §3.9, §5.1, §5.2 |
| **FE-2** - Default privileges as amended by R2.2 §6.1: default table `SELECT` belongs to `hh_backup` only; `hh_purge` receives its explicit ten-table ACL, including the required column-level `UPDATE` grants and its `alembic_version` `SELECT` exception. | ROL-11, ROL-14, ROL-15 | §3.4 |
| **FE-3** - Separate the initial E3 checks: after CREATE (disabled, default `PUBLIC` may exist); after ACL preparation (still disabled, `PUBLIC` revoked, `hh_migrate` `CONNECT` granted); only then enable connections and proceed with schema work. | BOOT-01 (E3-a/E3-b/E3-c), BOOT-02..BOOT-06 | §5.1 |
| **FE-4** - The negative temporary-database access test concerns unauthorized access to content or size; it does not require concealment of publicly accessible PostgreSQL catalog metadata. | §3.6 negative test 4; RST-04 step 6; TMP-08 item 2 | §3.6, §8.8, §8.9 |

### 14.5 Codex blocker disposition

| Blocker | Design disposition | Implementation evidence at freeze |
|---|---|---|
| **B1** - Purge ACL | Corrected; exact ten-table matrix plus column `UPDATE`; no default privilege; no widening fallback. | **NONE** - matrix specified in §3.6. |
| **B2** - Locks and ordinary operations | Corrected; four modes; separate journal lock; three handoffs; no catch-up promise. | **NONE** - specified in §7. |
| **B3** - Sequence and archive consistency | Corrected; archive-derived raw pair; scoped extraction; parser envelope; exact comparison; smoke on a discarded clone. | **NONE** - specified in §8.7. |
| **B4** - Restore identity and provisioning | Corrected; Mode A/B; exact role matrix; pre-hardening window; maintenance-DB authority; actor matrix; MEMBER/USAGE/SET. | **NONE** - specified in §4, §8.8, §8.9. |
| **B5** - Conservative rollback | Corrected; C4 lossless claim withdrawn; three journal events; fail closed. | **NONE** - the contract is the design. |
| **B6** - Normalized backup contract | Corrected; frozen flags; object classes; OID exclusion; ownership/ACL semantics. | **NONE** - fixture generated by an implementation test. |

### 14.6 Acceptance status

| Layer | Status |
|---|---|
| Architecture (this document) | Consolidated; **reported Codex Final R2.4 Delta Review PASS**; four errata integrated. Architecture PASS remains valid and unchanged by D1 and D2. |
| Documentation acceptance | **D1 findings remediated; D2 findings remediated; D2 documentation recheck PASS.** The PASS is a documentation-consistency claim only; no historical source-fidelity verification is claimed (see "Evidence provenance"). |
| Owner acceptance | **ACCEPTED 2026-11-14** - frozen specification v1.0 is the single normative implementation baseline. Acceptance of the architecture does not approve any open Owner Decision (OD-1..OD-9, §12) and is not implementation evidence. |
| Implementation | **NOT STARTED** - no code, migration, role, database, container or credential exists for this architecture. |
| Implementation tests | **NONE EXECUTED** - every test in this document is `[SPEC]`. |
| Production inventory | **NOT AUTHORIZED** - stage 7 requires a separate authorization. |
| Production cutover | **NOT AUTHORIZED** - stage 8 requires a separate authorization. |

### 14.7 Confirmation of scope

This specification was produced by document consolidation and, in the D1 pass, by in-place documentation remediation of this same file. No other file was created, edited, staged, committed or pushed; no database, container, timer, credential or Production system was accessed or changed; no test was executed; the protected paths were not opened; and all untracked B2/DYN drafts were preserved.

### 14.8 D1 documentation remediation record

| Finding | Affected sections | Resolution |
|---|---|---|
| **D1-01** Evidence provenance | Front matter "Evidence provenance"; §14 lead clarification; §14.1; §14.6 | Added explicit provenance: sources recovered read-only from a prior-session transcript; PASS and errata supplied by the brief; no independent PASS verification; no complete source-fidelity comparison; D1 examined only the consolidated file; architecture PASS distinct from documentation and implementation evidence. |
| **D1-02** Telegram delivery guarantees | TG-04, TG-05, TG-06, §6.6, §14.3 | Replaced the contract table with the four acknowledged/claim combinations; added that a committed claim does not prove acknowledgment and fetching does not prove acknowledgment; stated that an interrupted handler is not guaranteed to leave an attempted/not-answered turn; removed the §6.6 claim that held updates were necessarily acknowledged. W-11 and W-13 remain registered and withdrawn. |
| **D1-03** Restore identities by phase | RST-08 (with RST-01) | Acceptance now asserts `hh_restore`/`hh_restore` for connection and `CREATE DATABASE`, `hh_restore`/`hh_owner` after `SET ROLE` for ACL hardening, and `hh_restore`/effective `hh_owner` for object restoration; no phase is reported as `(hh_owner, hh_owner)`. W-15 remains withdrawn. |
| **D1-04** Promotion and rollback | TMP-10, C4 row, §11 stages 5-6 | Removed automatic DSN reversion on "no observed write". Reversal now requires full quiescence and effective fencing, verified absence of persistent divergence, verified absence of external effects, resolved uncertain operations and journal/operator evidence; otherwise fail closed and preserve the potentially authoritative database. RBK-01 governs; no automatic exception. |
| **D1-05** C5/C6 dependencies | §5.5 (now 5.5.1/5.5.2), §7.6 C5/C6 rows, stage 3 and 8 acceptance | Split acceptance by state. C5 covers enrollment, identity links, sign-in/session and non-bot isolation. C6 covers bot pairing, stranger notice, ledger cutover row, one-poller evidence and Telegram matrices. BOOT-16 and BOOT-21 are split within the same identifier across C5/C6; no C5 criterion depends on a running bot; no state requires evidence available only later. |
| **D1-06** Decision classification | GOV-12, ST-02, §12.3-§12.6 | Three categories: binding Owner decisions (PRD-04), approved architectural constraints (PRD-05), open choices (PRD-06, OD-1..OD-9). OD-6 (TLS), OD-8 (password placement) and the patch/configuration remainder of OD-7 stay open; the binding topology constraints are in PRD-05 and are not also listed as undecided. Email login disabled and the clean-slate/two-account decisions remain binding; AUTH-02..AUTH-04 are not weakened. |
| **D1-07** Gate traceability | §11 stage acceptance and gate matrix; new coverage table | Mapped ENR-01..ENR-17 to gate 2 and ENR-18..ENR-19 to gate 5; ENR-20..ENR-22 to gates 2 and 7; TMP-01..TMP-08 to gate 4 and TMP-09..TMP-10 to gates 5-6; CI-17 to gate 1; AUTH-01..AUTH-02 to gate 2 and AUTH-03..AUTH-06 to gate 5. Added a compact requirement-to-gate coverage table and named the process-only identifiers. All nine stages preserved. |
| **D1-08** Editorial references | ROL-02, ROL-15, GOV-13, GOV-02, GOV-07, BOOT-11, BOOT-21 | `ROL-33` references resolved independently (identity locale to the ROL-02 acceptance in stage 2; purge matrix to ROL-11 and §3.6 negative test 13). GOV-13 now cites §4.6/ENR-19. GOV-02 and GOV-07 now cite §5.4. BOOT-11 now preserves `[U]` and requires measurement. Adjacent references checked (including the C4 rollback reference, now §9.1/RBK-01). |
| **Editorial 1-4** | As above | All four corrected; no mechanical ID substitution. |

### 14.9 D2 documentation remediation record

D2 reopened only D1-02 and D1-06. D1-01, D1-03, D1-04, D1-05, D1-07 and D1-08 were left unchanged.

| Finding | Affected sections | Resolution |
|---|---|---|
| **D2-01** Telegram acknowledgment and redelivery | TG-04, TG-06, §6.6, TG-09, §12.6 OD-1 | TG-06 now separates "claim committed, not acknowledged" (redelivery still possible, idempotency governs) from "both acknowledged and committed" (no redelivery). §6.6 no longer derives no-redelivery from a committed claim alone. TG-04 states an update fetched by the poller is not necessarily acknowledged. Policy A's single reply is qualified as intended normal-path behavior, not a delivery guarantee, with interruption/acknowledgment timing/network/admission caveats. Routing distinctions and Policy A's proposed (unauthorized) status are unchanged. W-11 and W-13 remain withdrawn. |
| **D2-02** Password placement | ARC-03 (with AUTH-02, AUTH-03, ST-02, OD-8) | ARC-03 now states the binding credential controls (no password in the DSN, no credentials in committed configuration, no shared privileged credentials, per-role least credential, access-controlled/non-public material, libpq-compatible tooling) and makes the concrete source/placement explicitly conditional on OD-8. Neither `PGPASSWORD` nor `PGPASSFILE` is approved. AUTH-02/AUTH-03 describe the mechanism as OD-8-dependent; ST-02 and OD-8 include the production deployment and rotation procedure as open. |
| **D2-03** Decision timing | §12.3 PRD-03, §12.6 PRD-06 | Added the normative rule that each open Owner decision must be explicitly approved and recorded before its first dependent operational step, and that Gate 9 verifies closure without deferring prerequisites. Added the per-decision first-use mapping (OD-1 before bot enablement; OD-2 before dependent provisioning/recovery; OD-8 before production credentials; OD-9 before the enrollment window; OD-3/4/5/6/7 before their first dependent operations). The decisions themselves are unchanged. |
| **D2-04** Editorial | §5.5.2, §11 gate matrix | Corrected the Telegram reference to §6.1-§6.5. Clarified that Gate 9's PRD-03 reference is a documentation/governance review, not an implementation gate or an authorization to defer decisions. |
| **Regression** | D1 accepted groups | D1-01, D1-03, D1-04, D1-05, D1-07 and D1-08 remain in force; FE-1..FE-4 and W-01..W-25 remain intact. |
