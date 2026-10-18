# Kitchen Helper — Documentation index

**Updated:** 2026-10-18

**Baseline release:** R3B-2 (`6e2b432d460bfca18ed7d697bb8c05490fcc7512`) — **REPORTED from release handoff; confirm against Git before making changes.** R3B-3 Admin Bootstrap is ACCEPTED and counts as released only after GitHub CI confirms it; see [`STATUS.md`](STATUS.md).

## Purpose and reading order

1. [`product/PRODUCT_BASELINE.md`](product/PRODUCT_BASELINE.md) — approved product direction, **summary** of Stage 2A. It is not a replacement for the original 25 acceptance criteria.
2. [`architecture/ARCHITECTURE_BASELINE.md`](architecture/ARCHITECTURE_BASELINE.md) — summary of Stage 2B architecture and security invariants, not a substitute for the original full design.
3. [`DECISIONS.md`](DECISIONS.md) — recorded product/architecture decisions and deferred questions.
4. [`STATUS.md`](STATUS.md) — dated release snapshot, outstanding gates and proposed next work.
5. Repository root `README.md` — setup, commands, configuration and currently implemented functionality.

**Source provenance:** These documents were prepared from `Kitchen_Helper_HANDOFF_2026-10-18.md` supplied by the project owner. The original approved Stage 2A (25 detailed acceptance criteria) and Stage 2B full design were not available at the time of preparation. If originals already exist in the repository or another authoritative location, link to or import **them**, preserving their content; reconcile any difference with the owner rather than silently overwriting.

## Sources of truth

- **Implemented behavior:** actual source code, Alembic migrations and executable tests, backed by successful CI. A green CI run does not prove live third-party authentication or production deployment.
- **Product commitments:** full owner-approved Stage 2A specification, when recovered; until then the summary above plus decisions explicitly approved by the owner.
- **Architectural constraints:** full approved Stage 2B design, when recovered; use its summary until then.
- **Release evidence:** Git commits/CI and deployment verification, clearly separated.
- **Conversation handoffs:** historical snapshots; never silently override current implementation or accepted specifications.

## Maintenance rule

At every accepted increment, update `STATUS.md` (stage, commit, CI, verified vs reported vs deferred, next task) as part of a **separate documentation change or the approved release scope**. Update `DECISIONS.md` only when a decision actually changes or is newly approved. Change product and architecture documents only when the approved baseline changes. Add references from the root README after verification.

**Agent workflow:** scoped task -> implementation without commit/push -> independent risk-based Codex audit -> justified fixes -> owner ACCEPT -> separately authorized commit/push -> CI confirmation. No automatic production migrations, deployment or account linking. Avoid unbounded test campaigns and unrelated rewrites.
