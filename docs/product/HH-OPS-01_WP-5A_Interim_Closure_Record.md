# HH-OPS-01-WP-5A — Interim Documentary Closure

**Date:** 2026-11-28
**Authorization:** OWNER APPROVED — documentation only.
**Status:** HH-OPS-01 remains **OPEN / PARTIALLY COMPLETED / DEFERRED REMAINING OBLIGATIONS**. This is an **interim** documentary closure, not final closure. The deferred recovery lifecycle obligations in §5 remain explicitly open.

**Relationship to the release record.** This record sits beside, and does not modify, [`HH-REL-01_Release_Record.md`](HH-REL-01_Release_Record.md). It records the first post-release operational checkpoint and the HH-OPS-01 housekeeping decisions; it rewrites no accepted historical evidence. The current deployment facts remain in [`docs/STATUS.md`](../STATUS.md), "Current deployment state".

---

## 1. Repository and production baseline

- Repository: `eliv1982/kitchen-helper-telegram-bot`
- Branch: `main`
- Accepted repository HEAD: `26e81d6c9510368155d5171fd44550898f21b5a0`
- GitHub Actions CI **#104: PASS**.
- HH-REL-01-R8-CI-FIX-01: **CLOSED**.
- Production HEAD: `9c03b75f9815fd5f071a2134c8f3b7fc8330b9ca`
- PostgreSQL **16.15**, schema `0014_user_measure_overrides`.
- Two user accounts.
- HH-REL-01: **RELEASED / PRELIMINARY OWNER ACCEPT**.
- Final product acceptance and closure of the application rollback window remain **pending**.

## 2. Accepted operational checkpoint — 2026-11-28

The first post-release scheduled maintenance cycle was observed and accepted. These are the actual scheduled executions recorded by the Owner; the recipient-side monitoring records were not independently inspected and the remote survivor listing was not independently repeated.

| Run | Window (UTC) | Exit | Observed result |
|---|---|---|---|
| Retention purge | 03:33:42–03:33:44 | 0 | Four expired Telegram update admission rows removed; no conversations or messages removed |
| Off-host backup | 04:22:58–04:23:59 | 0 | PostgreSQL v2 backup, 21 tables, 179 rows; archive verification, encrypted publication, read-back and commit-record verification passed; ten committed recovery points retained and two superseded points removed |

Both n8n emitter calls returned HTTP 200 and both Cronitor pings succeeded.

**Verdict: CLOSED / OWNER ACCEPT WITH NON-BLOCKING NOTES.** The non-blocking notes are the two evidence limitations above: recipient-side monitoring records were not independently inspected, and the remote survivor listing was not independently repeated.

## 3. HH-OPS-01 work package status

| Work package | Status |
|---|---|
| WP-1 Resource Inventory | **CLOSED / OWNER ACCEPT** |
| WP-2A Recovery Lifecycle Design | **CLOSED / OWNER ACCEPT** |
| WP-2B Ciphertext Transfer and Verification | **CLOSED / OWNER ACCEPT** |
| WP-2B-K1 Recovery Key Protection | **DEFERRED** |
| WP-2C Plaintext Retirement | **DEFERRED / NOT AUTHORIZED** |
| WP-3 Maintenance Reconciliation | **CLOSED / OWNER ACCEPT / NO IMPLEMENTATION REQUIRED** |
| WP-4A Production Historical Image Cleanup | **CLOSED / OWNER ACCEPT** |
| WP-4B Other Production Cleanup | **DEFERRED** |
| WP-4C Workstation Cleanup | **DEFERRED** |
| WP-5A Interim Documentary Closure | **CURRENT ASSIGNMENT (this record)** |

**Overall HH-OPS-01: OPEN / PARTIALLY COMPLETED / DEFERRED REMAINING OBLIGATIONS.**

## 4. Recovery lifecycle evidence

The following artifacts are distinct and must not be conflated. None may be deleted under this closure.

### 4.1 Archive A — rehearsed plaintext

- Production path: `/var/lib/house-helper/preflight/house-helper-r5-20261127T145004Z.tar`
- Size: **122880 bytes**
- SHA-256: `9d0f8c2e5c8fa0d0fdcfa59bd6263272081544510a98a57186a1c3144bb7c93d`
- Root-owned, mode `0600`.
- Rehearsal run `795a5073ae0c24918281d27f52296cdf`: **passed**, cleanup confirmed.
- **Still present. Deletion is not authorized.**

### 4.2 Archive A′ — exact encrypted successor

- The production ciphertext remains present. Size: **123096 bytes**.
- SHA-256: `80aabc945baf13ffbc428182a0d7843a32fd7d9d7d866707d0cd8548ab4b503d`
- An independent verified ciphertext copy is retained on the Owner workstation, **outside the repository and outside synchronized folders**, with NTFS ACL restricted to the Owner and trusted Windows system principals.
- Local decrypt-to-hash completed with exit 0, empty stderr, exactly **122880** plaintext bytes, and the exact Archive A SHA-256. **No additional plaintext archive was created.**
- No repeated restore rehearsal is required for the identical payload.

### 4.3 Snapshot B and later backups

- `house-helper-20261127T145346Z-2d36c10c.tar.age` is a **different snapshot**, not the rehearsed payload. Its continued remote presence was **not independently verified**.
- Its projected scheduled retention expiry is **2026-12-04**, assuming uninterrupted daily backups and normal retention.
- The 2026-11-28 scheduled backup is a separate post-release recovery point.
- **Neither snapshot replaces the exact rehearsed payload A.**

## 5. Deferred recovery obligations (WP-2B-K1 / WP-2C)

The existing age identity on the Owner workstation was proven operational. **Independent key recovery remains unverified.**

Owner decision (see KHD-57 in [`docs/DECISIONS.md`](../DECISIONS.md)):

- Complete key protection **after the Owner returns from vacation**.
- Preferred mechanism: a **client-side encrypted key archive independently retained on Yandex Disk**, plus an **independent high-entropy passphrase recovery record retained securely offline**.
- Verify archive integrity and key recoverability before accepting the protection step.
- Do **not** store an unencrypted private key in cloud storage.
- Do **not** disclose private-key contents, passwords or sensitive key material in repository documentation.
- Correct the previously documented but outdated identity-location information using a **non-sensitive reference** to the Owner-controlled WSL identity.

**WP-2C requires separate Owner authorization** after successful key protection and immediate revalidation of the approved deletion gates. The plaintext archive (A), the production ciphertext (A′), the rehearsal record and the rollback image remain protected until then.

## 6. Maintenance reconciliation decisions

### 6.1 Maintenance pin (KHD-58)

Retain:

- Image `house-helper:d02b34ed369c`
- Checkout `d02b34ed369c986fcb8c9a1074c5d4491097d2fa`

The scheduled purge, backup, verification and schema contracts do **not** require repinning.

The isolated rehearsal defect is fixed in the `maintenance.py` blob `35a167525daf58183beca9f1ebabe4a849f9b557`. Future isolated rehearsals must use that fixed implementation or an independently accepted successor. **Do not use the older timer checkout's rehearsal implementation.**

### 6.2 PostgreSQL configuration checkout (KHD-59)

Retain the protected configuration checkout `/home/elvi/apps/house_helper-staging-ddca096`. The active bind mounts and the backup Compose reference depend on this exact checkout path; configuration blobs match later revisions. **No mount relocation or PostgreSQL recreation is authorized.**

### 6.3 Healthcheck (KHD-60)

Confirmed cause of the repeated peer-authentication failures: `pg_isready` executes as root while requesting the PostgreSQL role `postgres`. This is **monitoring noise, not an application authentication failure.**

Proposed future correction: `gosu postgres pg_isready -U postgres -d postgres`.

The correction requires a separately approved maintenance window, implementation verification and PostgreSQL container recreation. **Do not weaken authentication rules.**

### 6.4 Telegram admission ledger

A single HH-REL-01 startup duplicate-key event is attributed to the expected savepoint-based conflict handling. The ledger initialized successfully and no recurrence was observed. **No remediation is authorized.**

## 7. WP-4A production historical image cleanup — outcome

All **26** explicitly allowlisted historical House Helper images were removed successfully, without `--force` and without `docker prune`.

| Metric | Before | After |
|---|---|---|
| House Helper images | 30 | 4 |
| Docker images across the host | 86 | 60 |
| Docker image accounting | — | reduced by 4174440121 bytes |
| Observed filesystem used space | — | reduced by 3347169280 bytes |
| Build cache | 10.52 GB | 10.52 GB (unchanged; no cache pruning) |

No container restarts or recreation; no volume, checkout or archive deletion; unrelated image references preserved.

Protected remaining images:

- `house-helper:9c03b75f9815` — production
- `house-helper:afd83cb65bd7` — application rollback
- `house-helper:d02b34ed369c` — maintenance
- `house-helper:4a3249fadb04` — historical SQLite stack

The `ddca096` Docker image was removed, but the configuration checkout remains protected (§6.2). Application health, schema, account count, maintenance pins and active timers remained unchanged.

## 8. Deferred cleanup decisions (KHD-61)

**Retain** the historical SQLite stack and its database volume.

**Defer:**

- Minor historical production scratch files and build logs.
- The local disposable acceptance environment.
- Local experimental Docker images and volumes.
- Other workstation resources.

The disposable acceptance environment is recorded in the handoff as `C:\Users\eliv\AppData\Local\Temp\pp01-a1-owner-acceptance-2026-11-27`. Its current existence and dependencies have **not** been verified.

Docker build-cache cleanup is a **separate host-wide maintenance task**, not part of the House Helper cleanup authorization.

## 9. Engineering lessons carried forward

These are evidence-based retrospective inputs. They do not draft or freeze the Engineering Playbook, which remains a separate future work package.

- **Dependency First.**
- **Recovery Before Cleanup.**
- **Exact Recovery Payload Identity** (A and A′ are the rehearsed payload; Snapshot B is not).
- **Independent Key Recovery** (a copy on the same workstation is not independent).
- **Explicit Resource Allowlisting** (cleanup acts only on a named allowlist, with no force or prune).
- **Proportionate Verification** (do not repeat an identical rehearsal when payload identity is proven).
- **Executable Documentation Contracts** (documentation backing is tested; the R8 CI #103 incident is the reason).
- **Operational improvement must justify operational intervention** (the healthcheck fix is diagnosed and proposed, not applied).
- **Different component revisions are not inherently defects** (serving, maintenance and rollback images may legitimately differ).
- **Application rollback and database restoration are separate recovery operations.**

## 10. Documentary scope and executable documentation contracts

This closure changed documentation only and added no executable content. The affected executable documentation contracts were identified and their targeted tests were run:

- [`tests/test_deploy_scheduler.py`](../../tests/test_deploy_scheduler.py) — parses `docs/STATUS.md`, "Current deployment state".
- [`tests/test_deploy_offsite_docs.py`](../../tests/test_deploy_offsite_docs.py) — parses `docs/DECISIONS.md` and the RUNBOOK.
- [`tests/test_deploy_kit.py`](../../tests/test_deploy_kit.py) — the shared RUNBOOK/STATUS documentation contract helpers.
- [`tests/test_repo_secrets.py`](../../tests/test_repo_secrets.py) — scans documentation for secret-shaped content.

The targeted results are recorded in §12. No test code was modified. No executable documentation contract required a test-code change.

## 11. Restrictions observed

- Documentation changes only.
- No production access or infrastructure change.
- No provider call.
- No key access or disclosure.
- No deletion or cleanup.
- No implementation or test-code change.
- No broad documentation restructuring.
- No reopening of accepted work packages.
- No full local test-suite run.
- No staging, commit or push.

## 12. Targeted documentation-contract test results

Run locally on 2026-11-28 against the working tree, Windows / Python 3.12.10, without touching Git:

| Target | Result |
|---|---|
| `tests/test_deploy_scheduler.py` | PASS |
| `tests/test_deploy_offsite_docs.py` | PASS |
| `tests/test_deploy_kit.py` | PASS |
| `tests/test_repo_secrets.py` | PASS |

These are targeted documentation-contract runs only; this record claims no full-suite run and no CI run. The concrete command and counts are recorded in the deliverable reported to the Owner.

## 13. Remaining limitations

- This is an **interim** closure: WP-2B-K1 and WP-2C are open, and WP-4B and WP-4C are deferred.
- Independent key recovery is unverified.
- Recipient-side monitoring records were not independently inspected.
- The remote survivor listing was not independently repeated.
- Snapshot B's continued remote presence was not independently verified.
- The disposable acceptance environment's current existence and dependencies were not verified.
- No healthcheck correction has been implemented; its maintenance window is not approved.

## 14. Readiness for Owner review

The documentary changes are complete and the targeted documentation-contract tests are green. The work is **ready for Owner review**. Git operations (staging, commit, push) are deliberately stopped.
