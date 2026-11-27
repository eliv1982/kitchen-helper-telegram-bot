# House Helper — HH-REL-01
## Combined Release Record and Owner Decision

| Field | Value |
|---|---|
| Record | HH-REL-01 — combined release record |
| Decision date | 2026-11-27 |
| Owner decision | **KHD-56** — HH-REL-01 Conditional Exception to KHD-50 (see §3) |
| Accepted executable baseline | `26f6d1536789f1b9cb54fa2f29ae8d54be181cc6` (`main`) |
| Combined release scope | **HH-BRAND-01-FIX-01** + **PP-01-A1** + **PP-01-A1-CI-FIX-01** |
| Production revision (unchanged) | `afd83cb65bd7cc6ab1ab6d638187115b05610c63` |
| Current status | **TECHNICALLY ACCEPTED / CONDITIONAL RELEASE EXCEPTION APPROVED / OPERATIONAL GATES PENDING / DEPLOYMENT NOT AUTHORIZED** |
| Deployment | **NOT AUTHORIZED.** Deployment requires separate explicit Owner authorization under KHD-29 and successful completion of the R5–R8 gates in §9. |

## 1. Purpose and status

This record consolidates the release work R1–R4 for the combined **HH-BRAND-01-FIX-01 + PP-01-A1 +
PP-01-A1-CI-FIX-01** candidate and records the Owner's conditional release decision of 2026-11-27.
It is an additive documentary record. It rewrites no earlier evidence: the FIX-01 behavioral
evaluation remains **FAILED — 24 PASS / 6 FAIL** with zero observed critical safety violations, and
KHD-50, KHD-54 and KHD-55 remain the historical decisions they were.

It is **documentation only**. It changes no executable code, test, configuration or deployment
asset; it performs no Git operation, test run, provider call, Telegram call, database change,
backup or deployment. Production remains unchanged.

## 2. Confirmed baseline

- Repository: `eliv1982/kitchen-helper-telegram-bot`; local path `C:\Users\eliv\Pet_Projects\house_helper`; branch `main`.
- Accepted implementation baseline: `26f6d1536789f1b9cb54fa2f29ae8d54be181cc6`.
- PP-01-A1 implementation: `f55e089`.
- PP-01-A1-CI-FIX-01: `26f6d15`.
- CI run **#101**: **PASS** (Owner-confirmed).
- Production revision: `afd83cb65bd7cc6ab1ab6d638187115b05610c63`, PostgreSQL 16.15, schema
  `0014_user_measure_overrides`, two existing user accounts. Production is unchanged by this record.
- Owner Git evidence: eight commits, 59 changed files, 18,226 insertions, 388 deletions; tracked
  worktree clean; Production and FIX-01 ancestry verified.

## 3. Owner decision — KHD-56

**KHD-56 — HH-REL-01 Conditional Exception to KHD-50.** Registered as the next available decision
identifier (KHD-56 was free; the highest previously registered identifier was KHD-55).

The Owner approved the following terms on 2026-11-27:

1. A narrow, release-specific conditional exception to KHD-50 is approved for HH-REL-01.
2. The exception applies **exclusively** to the accepted executable baseline
   `26f6d1536789f1b9cb54fa2f29ae8d54be181cc6`.
3. A subsequent documentation-only commit is permitted, subject to confirmation that executable
   code, tests, configuration and operational assets remain unchanged and CI passes at the final
   release SHA.
4. Completion of FIX-02 and an additional Behavioral Evaluation are **not** prerequisites for this
   specific release.
5. The original FIX-01 Behavioral Evaluation remains **FAILED — 24 PASS / 6 FAIL**, with zero
   observed critical safety violations. Historical evidence and acceptance results must not be
   rewritten or reclassified.
6. The Owner explicitly accepts the residual risks represented by cases **1, 2, 21, 22, 24 and 25**:
   - incorrect intent resolution for malformed greetings;
   - recipe fidelity defects when repeating a previous recipe;
   - inaccurate description of shopping-list capabilities.
7. Recipe fidelity remains a **higher-priority** remediation direction within FIX-02.
8. Comparative behavioral regression against the current Production baseline is **not established
   in either direction**; the Owner explicitly accepts this uncertainty for HH-REL-01.
9. No additional pre-release Behavioral Evaluation or provider calls are authorized.
10. **FIX-02 remains OPEN / PAUSED.** Existing research, architecture and remediation requirements
    remain unchanged. **AG-03 and AG-04 remain unauthorized.**
11. The exception becomes effective only after successful Production preflight, exact release
    identity and provenance verification, verified backup, isolated restore and rollback readiness.
12. Actual deployment requires **separate explicit Owner authorization under KHD-29**.
13. This exception does **not** automatically extend to subsequent releases.
14. Previously accepted UX-08 and UX-09 limitations remain unchanged. Live iPhone and Telegram
    acceptance remains a post-deployment requirement.

KHD-50, KHD-54 and KHD-55 are preserved as historical decisions with explicit cross-references in
[`docs/DECISIONS.md`](../DECISIONS.md). KHD-56 does not remove KHD-50; it narrows it for this one
release candidate only.

## 4. R1 — Release scope and decision reconciliation

**Status: CLOSED.**

- The candidate scope (FIX-01 + PP-01-A1 + PP-01-A1-CI-FIX-01), the commit range and the
  Production/FIX-01 ancestry were confirmed by the Owner (eight commits, 59 files, 18,226
  insertions, 388 deletions; tracked worktree clean).
- KHD-50 remained active until the separate Owner decision recorded above as KHD-56.
- KHD-54 and KHD-55 did not independently authorize release; they recorded the scope lock and the
  Owner functional acceptance, and both preserved the release block.

## 5. R2 — Combined candidate verification

**Status: TECHNICAL VERIFICATION PASS WITH NON-BLOCKING NOTES.**

- C1–C7: **PASS**.
- Test result: **1,882 passed, 2 expected SQLite-specific skips, 0 failures.**
- PostgreSQL 16.15 compatibility confirmed on isolated synthetic data.
- Alembic check: **no new upgrade operations**.
- Offline Web/Telegram parity, persistence, ownership, retention and prompt integrity verified.
- **No new candidate code defect** was found.
- The Owner acceptance environment was untouched.

**HTTP evidence correction (documentary).** The R2 HTTP verification is correctly described as:

- **21 named Node integration journeys**;
- **2 direct HTTP checks**;
- **1 harness consistency check**;
- **all 24 tests passed.**

This corrects the earlier summary wording; the underlying results and counts are unchanged.

## 6. R3 — Targeted independent acceptance

**Status: CLOSED / ACCEPT WITH NON-BLOCKING NOTES — TECHNICAL RELEASE CANDIDATE READY.**

- C1–C7 evidence independently accepted.
- No new release-blocking technical defects.

Three clarifications resolved:

**N1 — Schema guard.** FastAPI's lifespan does **not** perform an Alembic revision check. The
supported PostgreSQL Compose path invokes `dbtool.py serve`, which performs the required schema
checks before starting Uvicorn.

**N2 — Prompt integrity.**

| Artifact | Value |
|---|---|
| `kitchen_prompt.py` file SHA-256 | `70c79d008cac79e6503b1034b5ff9c749d1d25e6fa89ed776d64440ac1f23fcc` |
| Base prompt | 10,764 characters |
| Base SHA-256 | `079ee60d80b007d92adced32762c87389f16746b06f421652586af49dc3b37cf` |
| Effective structured prompt | 12,595 characters |
| Effective SHA-256 | `bbb1b95c852cf6752a2203a6e98699e10031f1f5e8b03aae26ea72a4f0b3fcf4` |

The accepted FIX-01 prompt contract remains intact.

**N3 — Proportionality.** The retention selector unintentionally executed both test modules in
full. The **200 passes remain valid**; no additional rerun is required.

## 7. R4 — Documentary reconciliation

**Status: COMPLETE — this record and the documentation updates listed in §11.**

- KHD-56 registered with explicit cross-references; KHD-50, KHD-54 and KHD-55 preserved.
- R1–R3 closure recorded above.
- Stale statements corrected in `docs/DECISIONS.md`, `docs/STATUS.md`,
  `docs/product/PP-01-A1_Implementation_Record.md` and
  `docs/product/PP-01-A1_Owner_Acceptance_Closure_Record.md`: the working-tree-only implementation,
  the pending Owner commit, the pending full CI matrix and the CI-FIX-01 closure are reconciled to
  `f55e089`, `26f6d15`, CI #101 PASS and completed Owner acceptance.

The SHA and CI result of the documentation-only commit itself are **not yet known** and are not
invented here; the next push-triggered CI run at that SHA is the first eligible confirmation of
KHD-56 term 3.

## 8. Process observations preserved for the Engineering Retrospective

These are non-blocking **process** observations, not candidate defects. They are preserved for the
future Engineering Retrospective; no retrospective or playbook is created here.

- R2 used an overbroad test selector.
- R2's HTTP journey count was stated incorrectly and has been corrected above.
- R3 executed one unauthorized read-only `git status --short --branch` before reading the
  restriction; it was independently disclosed and no repository mutation occurred.

## 9. Remaining operational gates

| Gate | Content | State |
|---|---|---|
| **R5** | Production provenance, PostgreSQL-specific deployment preflight, verified backup, isolated restore and rollback readiness | **PENDING** |
| **R6** | Production deployment, under its own explicit Owner authorization | **PENDING / NOT AUTHORIZED** |
| **R7** | Infrastructure, data integrity, PostgreSQL, desktop Web, iPhone and Telegram verification | **PENDING** |
| **R8** | Final release documentary closure | **PENDING** |

**Important operational correction.** `deploy/RUNBOOK.md` **§9** describes the **SQLite** upgrade
procedure (`deploy/upgrade.sh`, Docker volume, the SQLite backup/verify/restore pair). It must
**not** be mechanically applied to the PostgreSQL Production deployment, which runs PostgreSQL
16.15 at schema `0014_user_measure_overrides`. The effective PostgreSQL deployment workflow must be
confirmed by Cursor during **R5**. It is **not designed, modified or assumed** by this record.

The existing disposable Owner acceptance environment remains untouched.

## 10. What this record does not establish

- It does not claim that deployment is authorized or performed.
- It does not claim that KHD-50 is universally removed; KHD-50 remains in force for every release
  other than the `26f6d15` HH-REL-01 candidate.
- It does not claim comparative behavioral regression against Production in either direction.
- It does not reopen, reclassify or rewrite the FIX-01 Behavioral Evaluation (**FAILED — 24 PASS /
  6 FAIL**), its frozen rubric or its case verdicts.
- It does not select a FIX-02 architecture, authorize AG-03/AG-04, or start PP-01-A2.

## 11. Documentation files changed by R4

- `docs/product/HH-REL-01_Release_Record.md` (new, this record)
- `docs/DECISIONS.md` (KHD-56; cross-reference notes on KHD-50, KHD-54 and KHD-55)
- `docs/STATUS.md` (dated HH-REL-01 entry and next-step reconciliation)
- `docs/product/PP-01-A1_Implementation_Record.md` (header status and §19 closure note)
- `docs/product/PP-01-A1_Owner_Acceptance_Closure_Record.md` (header status and §10 closure note)

No executable code, test, configuration, migration, dependency, prompt, asset or deployment file is
changed by this record.

## 12. Compliance statement

- Documentation changes only; no Git operation, test run, audit, provider call, Telegram call,
  Production access, backup, deployment or database change was performed to write this record.
- Unrelated untracked working-tree content was not included.
- Historical decisions and behavioral evidence are preserved; the record is additive and dated.

**Final conclusion: R4 DOCUMENTARY RECONCILIATION COMPLETE — READY FOR OWNER COMMIT AND CI.**
