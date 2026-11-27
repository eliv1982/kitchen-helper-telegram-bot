# House Helper — PP-01-A1-AD-01 Architecture Lock v1.0
## Bounded Title-Operation Processing Architecture (Option B)

| Field | Value |
|---|---|
| Date | 2026-11-27 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` |
| Package | PP-01-A1 — Conversation Navigation (increment A of PP-01) |
| Product authority | [`PP-01-A1_Scope_Lock_v1.0.md`](../product/PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, frozen, unchanged) |
| Architecture decision | **PP-01-A1-AD-01 — Option B**, Owner-approved 2026-11-27 |
| Approved implementation boundary | `house_helper/web/static/chat-logic.js` (production); the tests named in §9 |
| Status | **OWNER APPROVED / IMPLEMENTATION AUTHORIZED** (local implementation, tests and documentation only) |
| Independent reviewer | Codex (read-only) |
| Release status | Local working tree only. Not committed, independently accepted, released or deployed. |

## 1. Decision and rejected alternatives

The latest independent R4 functional review executed 49 existing checks successfully and
confirmed structural consistency defects in the title-operation controller (recorded in
[`PP-01-A1_R4_Remediation_Record.md`](../product/PP-01-A1_R4_Remediation_Record.md) and the
R4 review). The defects share one cause: each earlier round added a correct local rule
without a single processing boundary that separates identity/contract validation from
state mutation. The approved decision is:

- **Option B — bounded architectural reorganization.** One coherent processing boundary
  for asynchronous title-operation results, scoped to the controller and to title/list
  state. **APPROVED.**
- **Option A — additional localized corrections without a coherent response boundary.**
  **REJECTED**: it repeats the failure mode that produced the R4 defects.
- **Option C — controller-wide reorganization.** **NOT AUTHORIZED**: it exceeds the
  increment and would disturb preserved, independently verified behavior.

No material contradiction or unresolved architectural decision was found while writing
this lock. The frozen D1–D7 Product Scope Lock is preserved verbatim; this lock adds no
product decision.

## 2. Owner decisions OD-1 … OD-8

| ID | Decision |
|---|---|
| OD-1 | Option B — bounded architectural reorganization (see §1). |
| OD-2 | Strict mutation-response validation. Compatibility inference is confined to read responses. |
| OD-3 | Endpoint-specific 401 classification, subject to AD-C2. |
| OD-4 | Title responses update title-specific metadata only. |
| OD-5 | Idempotent authoritative removal, subject to AD-C1. |
| OD-6 | Bounded implementation within the approved components. |
| OD-7 | Invariant-based verification and focused affected-area regression suites. |
| OD-8 | Unified, authoritative mutation-outcome contract (see §5). |

## 3. Clarifications AD-C1 and AD-C2

### AD-C1 — Authoritative absence and pagination

A conversation absent from the displayed list is not necessarily unknown or deleted.
Only this application's `404 conversation_not_found` and a confirmed deletion are
authoritative absence; absence from a page is pagination and is never a signal.

Applicability is decided from the state this page actually holds: the open conversation,
a thread, an unresolved turn, pending title operations, confirmed-title metadata, title
ordering/guard records, and any existing absence marker. The first applicable
confirmation:

- advances the list generation, so a list read begun before it cannot restore obsolete
  conversation state;
- is recorded in a bounded, page-lifetime marker store (one entry per conversation
  confirmed absent on this page; memory only; cleared when the page starts over).

A repeated notification for the same already-reconciled disappearance is a no-op: it
does not advance the generation, does not invalidate a legitimate subsequent list refill
and changes no unrelated state. A read begun after the first confirmation applies the
server's list normally. No permanent, persisted or read-driven tombstone store exists.

### AD-C2 — Indeterminate mutation and session verification

- **Definitive application authentication refusal:** HTTP 401 carrying this
  application's error body for `not_authenticated` (the established pre-write contract —
  the session dependency answers before any database work). The session lifecycle's
  designated owner (`handleAuthLost`) performs the transition.
- **Every other 401** (status-only, malformed, proxy, an unrecognized body) does **not**
  establish non-commit. The mutation outcome is **indeterminate**: the conversation's
  guard and the draft are preserved, no queued write is released, nothing is retried,
  and a fresh session verification is requested through the existing mechanism.
- **Verification confirms the same account:** the guard and draft are preserved, nothing
  is released or retried, and successful verification is not treated as evidence that the
  mutation completed.
- **Verification confirms a different account or an ended session:** the established
  account-isolation and session-cleanup requirements apply. Previous-account private
  state is never exposed or transferred, and old completions cannot modify the new
  session. Session termination is **not** claimed to establish completion of the
  indeterminate mutation.

Scope clarification: genuine document reloads, independent tabs and distributed
execution remain outside this architecture and keep their recorded limitations.
Ordinary same-document, same-account revalidation is not silently expanded into those
limitations. The only pre-existing policy that remains is that a definitive session end
or different account discards in-memory guards and drafts (see §8, L5); that policy is
the established session lifecycle, not a new guarantee or a new limitation introduced by
this lock.

## 4. Components, responsibilities and state ownership

| Component | Responsibility | Owns |
|---|---|---|
| Session lifecycle | Epoch, start-over, auth-loss, verification, gate, `letThrough` | `epoch`, `principal`, `sessionEndsAt`, `verifySeq`, `verifyingEpoch`, `state.gate`, `gateWaiters` |
| List authority | The displayed list, reads and their ordering | `state.list`, `listRead`, `listEpoch`, `goneMarks` |
| Conversation/activity authority | Membership, order, creation/activity timestamps | `state.list.items` membership and order, `touchListItem`, `applyTurn`, `forgetConversation` |
| Confirmed-title store | The three title fields this page confirmed | `titleConfirmed` (title, titleOrigin, canReset, mark) |
| Operation registry | One write per conversation, ordering, guard | `titleQueues`, `titleSeq`, `titleApplied`, `titleOps` (operation tokens), `titleBlocked`, `titleOpCount` |
| Response contract | Read compatibility vs strict mutation validation | `normalizeConversation` (reads), `normalizeTitleMutationResponse` (mutations) |
| Classification/transition | Closed outcome model; non-mutating transition decision | `classifyConversationUpdate`, `decideTitleTransition` |

State ownership rules:

- A title confirmation writes only `titleConfirmed[id]` and, when the item is present,
  the three title fields. It never writes membership, order, `createdAt`, `updatedAt`,
  `lastMessageAt` or unrelated metadata.
- `touchListItem`/`applyTurn` own activity and ordering. No title path may re-sort or
  replace a list item.
- `unresolved-turn` state, favorites, shopping and profile are untouched by title
  processing.
- Session transitions belong only to the session lifecycle owner; the mutation path may
  request them, never perform them itself (except by invoking `handleAuthLost`).

## 5. Unified mutation-outcome contract

One closed table reconciles the historical R3 and R4 documentation. Every PATCH answer
maps to exactly one outcome.

| Outcome | Answer | Evidence that fixes it | Permitted apply |
|---|---|---|---|
| `confirmed` | `200`/`201` with a strictly valid summary for the requested conversation obeying the requested operation | The route answers the requested conversation after its commit | titleConfirmed + three title fields + truthful success feedback |
| `refused` | `403 forbidden_origin`, `415 unsupported_media_type`, `413 request_too_large`, `400 invalid_request`, `400 empty_title`, `400 title_too_long`, `400 invalid_title` — code **and** status must match | Each is raised by the route before `database.run` (R4 real-route evidence) | show the refusal; release the queue |
| `absent` | `404 conversation_not_found` | Route id parse or the rolled-back transaction; authoritative | forget the conversation; mark absence (AD-C1) |
| `indeterminate` | any other 4xx (proxy 408/429/418, unrecognized application code, table code at the wrong status), **any** 5xx, transport interruption/soft timeout, malformed or non-conforming `200`/`201` | None proves non-commit; the route answers after commit | keep guard + draft; release nothing; retry nothing |
| `auth_refused` | `401` with this application's `not_authenticated` error body | Session dependency before any database work | session lifecycle (`handleAuthLost`) |
| `auth_indeterminate` | any other `401` | Proves nothing about the write | keep guard + draft; request fresh verification; release/retry nothing |
| `superseded` / inapplicable | a completion from an earlier session, an invalidated operation (conversation forgotten) or an operation a newer applied answer overtook | Stage 1/3 identity and ordering | nothing at all |

Preserved unchanged: all-5xx indeterminacy, existing transport-interruption handling, the
non-aborting soft timeout, no automatic retry, conflict protection, truthful Russian
feedback, draft preservation, and usability of unrelated conversations. Mutation
certainty, session validity and UI visibility remain separate.

Strict summary validation (OD-2) requires, for a mutation success only:

- `id` — a UUID string equal to the requested conversation (case-insensitive);
- `title` — present; `string` or `null`;
- `title_origin` — present; exactly `manual`, `derived` or `none`;
- `can_reset_title` — present; boolean;
- cross-field consistency: `manual` ⇒ non-empty text + resettable; `derived` ⇒ non-empty
  text + not resettable; `none` ⇒ `null` + not resettable;
- `created_at`, `updated_at` — present, parseable timestamps;
- `last_message_at` — present; `null` or a parseable timestamp;
- operation semantics: rename returns the requested normalized text with manual/resettable
  state; reset returns a non-manual, non-resettable state (the derived text may differ).

Compatibility inference (a missing `title_origin`/`can_reset_title`) remains only in
`normalizeConversation` for reads. No new backend requirement is invented.

## 6. Ten mandatory architectural invariants

| ID | Invariant |
|---|---|
| I1 | Responses from a stale session cannot modify current shared controller state. |
| I2 | An operation may complete or clear only its own pending-operation record. |
| I3 | Mutation success requires the requested identity and strict raw-response contract. |
| I4 | Unconfirmed mutation outcomes cannot release conflicting writes. |
| I5 | Title confirmation updates title-specific fields only. |
| I6 | Membership, ordering and activity metadata belong to their designated list and message authorities. |
| I7 | Authoritative absence is reconciled idempotently without invalidating legitimately newer reads. |
| I8 | Older responses cannot overwrite newer confirmed title state. |
| I9 | Success cannot be reported before identity and contract validation. |
| I10 | Session transitions belong to the designated session lifecycle mechanism. |

Enforcement points are recorded in the R5 Implementation Record (invariant-to-enforcement
matrix), not duplicated here.

## 7. Processing boundary

Every asynchronous title result passes, in order:

1. **Validate identity and applicability** (read-only): session epoch, operation token
   identity, ordering supersession, operation invalidation. Failure ⇒ inapplicable; no
   shared-state write, no clear, no report.
2. **Validate the raw contract** (read-only): strict summary validation and operation
   semantics. No compatibility inference, coercion or repair.
3. **Decide the transition** (read-only): the closed outcome model combined with session,
   operation and freshness state. A superseded-for-display completion is never confused
   with an unknown execution outcome.
4. **Apply scoped changes**: first this operation's own record (identity-scoped), then only
   the changes the selected transition permits, through narrowly scoped apply functions.

## 8. Documented residual limitations

- **L1 — Document reloads.** A genuine reload discards the in-session queue and guard. A
  request already delivered can still commit afterwards; ordering across reloads is not
  guaranteed.
- **L2 — Independent tabs.** No cross-tab synchronization, ordering or locking.
- **L3 — Distributed execution.** No server-side operation journal, distributed
  sequencing or locking.
- **L4 — Recovery.** After a genuinely indeterminate in-session outcome, recovery by
  confirmation requires a reload; the page never retries automatically.
- **L5 — Session end/different account.** A definitive session end or a different account
  discards this page's in-memory guard, draft and queue (established `resetData`
  lifecycle). No cross-session record of an unconfirmed title write exists.
- **L6 — Browser cache decisions.** Actual back/forward-cache eligibility and eviction
  remain a manual smoke-test item.
- **L7 — Deferred product work.** A1-05 uncaptioned-photo ambiguity, A1-04 frozen Unicode
  policy, semantic titles, archived original titles, full PP-01.1D and Telegram title
  commands remain as recorded.

## 9. Authorized implementation and verification boundaries

Production: `house_helper/web/static/chat-logic.js` only.

Tests: `tests/js/logic_scenarios.js`, `tests/js/dom_scenarios.js`, `tests/js/harness.js`,
`tests/js/crosscheck.js`, `tests/test_web_ui_logic.py`, `tests/test_web_chat_api.py`; other
existing affected-area tests may be executed unchanged.

Documentation: this lock, `PP-01-A1_R5_Implementation_Record.md`,
`PP-01-A1_Implementation_Record.md`, `docs/STATUS.md`.

Verification required:

- a deterministic transition matrix over session/operation/outcome/presence/freshness
  combinations, with unreachable combinations identified;
- deterministic regressions for the four R4 review defects (F1–F4) and both clarifications;
- focused affected-area suites: Node scenarios, logic wrapper, DOM, UI assets/integration,
  chat API, and a real-route PATCH-shape crosscheck against the strict client contract;
- PostgreSQL 16.15 only if an affected backend/persistence contract requires it
  (`house-helper-test-postgres`, `127.0.0.1:55432`, bootstrap `hh_test` OID 10); native
  PostgreSQL on 5432 is never touched.

Not authorized: controller-wide reorganization, a new state-management framework,
backend/API/schema/migration changes, a persistent operation journal, distributed
locking, cross-tab synchronization, prompt/provider changes, research, production access,
deployment or any Git mutation.
