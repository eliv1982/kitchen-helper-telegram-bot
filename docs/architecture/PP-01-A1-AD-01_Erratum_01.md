# House Helper — PP-01-A1-AD-01 Erratum 01
## Soft-timeout terminology: intermediate pending/slow vs terminal indeterminate

| Field | Value |
|---|---|
| Date | 2026-11-27 |
| Applies to | [`PP-01-A1-AD-01_Architecture_Lock_v1.0.md`](PP-01-A1-AD-01_Architecture_Lock_v1.0.md), §5 outcome table and §5 closing paragraph |
| Type | Documentation erratum only — no decision, contract or runtime change |
| Origin | R5-C1 review terminology observation, reconciled by PP-01-A1-R5-C2 |

## Erratum

The Architecture Lock's `indeterminate` row lists "transport interruption/soft timeout"
among the answers that map to the `indeterminate` outcome. Read literally, that would treat
the *soft-timeout event* as a terminal verdict. The approved and verified runtime does not,
and must not: the soft timeout does **not** abort the request. The queue stays behind the
request until it really settles.

The correct distinction, which the runtime already implements, is:

1. **Intermediate pending / slow** — the non-aborting soft timeout has fired, but the
   original request is still outstanding. A definitive answer may still arrive. The page
   shows visible pending feedback (`titlePending`), keeps the draft, and sends no newer
   same-conversation write past the outstanding one. This is **not** an outcome.
2. **Confirmed / definitively refused** — a settled answer that proves the write did or did
   not commit.
3. **Terminal indeterminate** — a settled request whose answer cannot confirm the write
   (a transport failure that has actually settled without proof, any 5xx, a malformed or
   non-conforming `200`/`201`, a partial/unreadable answer that is not authoritative). Only
   this state applies the guard (`titleBlocked`) and refuses later writes until reload.

Accordingly, in the §5 table the phrase "transport interruption/soft timeout" is read as:

- "transport interruption" = a settled transport failure that proves nothing;
- "soft timeout" = **not by itself an outcome**; it leaves the request in the intermediate
  pending/slow state, and the outcome is decided when that request settles.

## Why an erratum rather than a rewrite

The Architecture Lock is the frozen, Owner-approved architectural record. Its historical
text is preserved verbatim; this erratum is the authoritative clarification for the
soft-timeout wording. No product decision, transition, invariant or runtime behaviour is
changed by this document.

## Verified runtime evidence (unchanged by R5-C2)

- `http` keeps the non-aborting soft timer; the queue slot's `tail` resolves only when the
  underlying request settles.
- The `onTimeout` callback sets `op.slow` and visible pending feedback only; it does not
  mark the operation unconfirmed.
- `markTitleUnconfirmed` (guard) is reached only from settled, non-confirming outcomes
  (`indeterminate`, `auth_indeterminate`), never from the timer.
- R5-C1 scenarios that assert pending/slow visibility and the guard remain green:
  `a_relisted_conversation_gets_a_fresh_lifetime_and_an_old_404_cannot_touch_it` (pending +
  slow while the write is out), and the outcome-matrix / guard scenarios.

**Status: ERRATUM RECORDED — RUNTIME UNCHANGED**
