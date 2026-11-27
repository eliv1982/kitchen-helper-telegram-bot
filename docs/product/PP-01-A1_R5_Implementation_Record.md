# House Helper — PP-01-A1-R5 Bounded Architectural Reorganization
## Implementation Record

| Field | Value |
|---|---|
| Date | 2026-11-27 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, frozen, unchanged) |
| Architecture authority | [`PP-01-A1-AD-01_Architecture_Lock_v1.0.md`](../architecture/PP-01-A1-AD-01_Architecture_Lock_v1.0.md) |
| Previous remediations | [`PP-01-A1_R1_Remediation_Record.md`](PP-01-A1_R1_Remediation_Record.md), [`R2`](PP-01-A1_R2_Remediation_Record.md), [`R3`](PP-01-A1_R3_Remediation_Record.md), [`R4`](PP-01-A1_R4_Remediation_Record.md) |
| Audit source | Latest independent R4 functional review (49 existing checks executed successfully; F1–F4 structural consistency defects confirmed) |
| Owner decision | **Option B approved** (OD-1…OD-8, AD-C1, AD-C2) |
| Status | **OPTION B IMPLEMENTED — READY FOR INDEPENDENT FUNCTIONAL ACCEPTANCE.** Working tree only; not committed, independently accepted, released or deployed. |
| Preserved baseline | A1-02…A1-08 independently CLOSED; **A1-01 OPEN**; FIX-01 behavioral baseline **24 PASS / 6 FAIL**; KHD-50 release block in force |

## 1. Executive conclusion

The title-operation controller now has one processing boundary — **validate identity →
validate the raw contract → decide the transition → apply scoped changes** — instead of a
series of independent conditionals. The four confirmed R4 review defects (F1–F4) and both
Owner clarifications (AD-C1, AD-C2) are corrected structurally, and the complete R1–R4
regression corpus remains green without weakening an assertion.

No product decision was added. No backend, API, schema, migration, prompt, provider or
deployment change was made. The implementation is confined to
`house_helper/web/static/chat-logic.js` and the authorized tests.

## 2. Approved architecture and actual structural changes

The normative architecture is the
[Architecture Lock](../architecture/PP-01-A1-AD-01_Architecture_Lock_v1.0.md). This record
reports what was actually built.

### 2.1 Response contract split

- `normalizeConversation(item)` — unchanged; still the **read** normalizer. It keeps the
  compatibility inference for the older title shape (a missing `title_origin`, a missing
  `can_reset_title`). Read compatibility is deliberately not removed.
- `normalizeTitleMutationResponse(item)` — new, strict, mutation-only. It applies no
  inference or repair. A PATCH success must carry every field `_conversation_json`
  actually answers (`id`, `title`, `title_origin`, `can_reset_title`, `created_at`,
  `updated_at`, `last_message_at`) with the correct types and a consistent title state:
  - `manual` ⇒ non-empty text and `can_reset_title = true`;
  - `derived` ⇒ non-empty text and `can_reset_title = false`;
  - `none` ⇒ `title = null` and `can_reset_title = false`.
  A body that fails any check is not a confirmed success; it is indeterminate.

### 2.2 Closed outcome model

`classifyConversationUpdate(response, id, request)` now returns exactly one of:
`confirmed`, `refused`, `absent`, `indeterminate`, `auth_refused`, `auth_indeterminate`.
The controller adds `superseded` for an inapplicable completion (stage 3). The definitive
refusal table (`TITLE_REFUSAL_STATUSES`, code **and** status) and the all-5xx
indeterminacy are unchanged. Any unrecognized 4xx, a settled transport failure and a
non-conforming 200/201 stay indeterminate. (Corrected terminology, R5-C2: the non-aborting
**soft timeout** is not itself an outcome — the request stays intermediate pending/slow
until it settles; only a settled request whose answer cannot confirm the write is terminal
indeterminate. See [`PP-01-A1-AD-01_Erratum_01.md`](../architecture/PP-01-A1-AD-01_Erratum_01.md).)

### 2.3 Operation identity

- `titleOps[id]` now holds the operation object `{token, sequence, kind, slow}` itself; the
  structural identity check is object identity (`titleOps[id] === op`).
- `titleOpCount` is a page-lifetime, never-reset counter that only makes the identity
  legible. Sequence numbers may legitimately restart at 1 after the page starts over; the
  operation token does not.
- `clearTitleOperation(id, op)` deletes the record only when the caller owns it.
- The soft-timeout callback checks the epoch and the operation identity before touching
  `op.slow` or the view.

### 2.4 Scoped apply

- `mergeTitleFields(id, conversation)` replaces `mergeConversationItem`. A title
  confirmation writes exactly `title`, `titleOrigin`, `canReset` on an item the list
  still holds. It never replaces the item, reorders it, or takes `createdAt`,
  `updatedAt`, `lastMessageAt` or unrelated fields from the mutation answer.
- `applyConfirmedTitle`, `applyTitleRefusal` and `markTitleUnconfirmed` are the only
  title-apply functions. `titleConfirmed` still outlives the list page (A1-01a).

### 2.5 Processing boundary

`sendTitleMutation` order:

1. session epoch check;
2. operation identity read (`titleOps[id] === op`);
3. strict raw-response validation;
4. transition decision (`decideTitleTransition`);
5. scoped apply, beginning with clearing its own operation record.

An operation the page invalidated (the conversation was forgotten) applies nothing,
with the single exception of the application's authoritative `absent` answer, which is
idempotent (AD-C1) and preserves the R4 `gone` outcome.

### 2.6 AD-C1 — idempotent authoritative absence

- `goneMarks[id]` records the list generation of the **first applicable** confirmation
  that a conversation no longer exists.
- `confirmConversationAbsent(id)` advances `listEpoch` only on that first confirmation.
  A repeated notification for the same already-reconciled disappearance is a no-op: it
  does not invalidate a list read begun after the first confirmation and changes no
  unrelated state.
- `conversationStillRelevant(id)` decides applicability from the state the page actually
  holds (open conversation, thread, unresolved turn, title ordering/guard/confirmed
  records, list item, an existing marker). A delayed answer about a conversation this
  page never held changes nothing.
- A conversation missing from a page is **not** a marker: only a `404
  conversation_not_found` or a confirmed deletion marks one.
- The marker is bounded and page-lifetime: one entry per conversation confirmed absent on
  this page, memory only, cleared by `resetData`, revoked only if an authoritative read
  begun after it lists the conversation again. No persistent or read-driven tombstone
  store exists.

### 2.7 AD-C2 — endpoint-specific 401

- `401` with this application's `not_authenticated` error body (detail, valid correlation
  id) ⇒ `auth_refused`; the session lifecycle owner (`handleAuthLost`) performs the
  transition, as before.
- Every other `401` ⇒ `auth_indeterminate`. The conversation is guarded, the draft and
  editor are preserved, nothing is released or retried, and a **fresh** session
  verification is requested through the existing mechanism (a fresh `recheck(…, true)`).
- Same account: the guard and draft survive (`letThrough` re-shows the page; neither is
  discarded), no queued write is released, no retry occurs, and successful verification
  is not treated as evidence of completion.
- Different account or ended session: the established `start()`/`handleAuthLost()`
  lifecycle applies. Previous-account private state is dropped, and an old completion is
  discarded by the epoch.

## 3. Component and state ownership boundaries

| Component | Owns | Never writes |
|---|---|---|
| Session lifecycle (`start`, `handleAuthLost`, `verifySession`, `recheck`, `letThrough`) | `epoch`, `principal`, `sessionEndsAt`, `verifySeq`, `verifyingEpoch`, `state.gate`, `gateWaiters` | title/list state except through `resetData` |
| List authority (`loadList`, `reloadList`) | `state.list`, `listRead`, `listEpoch`, `goneMarks` revocation | title operations |
| Conversation/activity authority (`touchListItem`, `applyTurn`, `forgetConversation`) | membership, order, `createdAt`, `updatedAt`, `lastMessageAt` | title text |
| Confirmed-title store | `titleConfirmed[id]` and the three title fields of a held item | activity, membership, order |
| Operation registry | `titleQueues`, `titleSeq`, `titleApplied`, `titleOps`, `titleBlocked`, `titleOpCount` | list membership |
| Response contract | read vs strict mutation normalization and the closed outcome model | controller state |
| Mutation apply (stage 4 only) | the permitted delta of one transition | another operation's record |

## 4. Invariant-to-enforcement matrix

| Invariant | Enforcement point | Evidence |
|---|---|---|
| I1 — a stale session cannot modify current state | `sendTitleMutation` epoch check before classification; `onTimeout` epoch check; `resetData` wipes the registry and `epoch` guards every apply | `a_stale_session_completion_never_clears_a_current_operation_record`; `a_restored_page_of_another_person_discards_the_previous_title_state` |
| I2 — an operation completes/clears only its own record | `titleOps[id] === op`; `clearTitleOperation`; `onTimeout` token check | `a_stale_session_completion_never_clears_a_current_operation_record` |
| I3 — success needs identity + strict contract | `normalizeTitleMutationResponse`, id equality, `isExpectedTitleState` | `a_title_answer_outside_the_strict_summary_contract_is_never_confirmed`; `a_title_answer_about_another_conversation_is_indeterminate_and_changes_nothing` |
| I4 — unknown outcomes cannot release writes | `markTitleUnconfirmed`; `titleBlocked` checked before every send | `an_unconfirmed_title_write_guards_the_conversation_and_leaves_others_usable`; `a_401_outside_the_pre_write_contract_is_indeterminate_and_verifies_the_session` |
| I5 — confirmation updates title fields only | `applyConfirmedTitle` → `mergeTitleFields` | `a_title_confirmation_changes_only_the_three_title_fields` |
| I6 — membership/order/activity belong to their authorities | no item replacement; `touchListItem`/`applyTurn` untouched | same scenario; all list scenarios |
| I7 — absence is idempotent and does not invalidate newer reads | `confirmConversationAbsent`; `goneMarks`; `loadList` generation check | `a_repeated_absence_never_discards_a_legitimate_later_list_refill`; `a_title_404_is_not_undone_by_a_list_read_taken_before_the_conversation_vanished` |
| I8 — older responses cannot overwrite newer confirmed state | per-conversation serialization + `titleMutationSuperseded` stage-3 guard + `titleConfirmed` marks | `a_delayed_reset_cannot_replace_a_newer_rename_or_close_its_editor` (ordering); supersession itself unreachable by construction (§8) |
| I9 — no success before validation | `confirmed` is produced only after stage 2/3 | `a_title_outcome_matrix_applies_only_the_permitted_delta`; all strict-contract scenarios |
| I10 — session transitions belong to the lifecycle | `auth_refused` → `handleAuthLost`; `auth_indeterminate` → a fresh `recheck(…, true)`; the mutation path never transitions the session itself | `the_applications_pre_write_401_ends_the_session`; the three AD-C2 401 scenarios |

## 5. F1–F4 evidence

### F1 — conversation lifecycle

- **Whole-item replacement overwriting activity.** `mergeConversationItem` was replaced by
  `mergeTitleFields`; a title answer no longer supplies dates, membership or order.
  Deterministic regression: `a_title_confirmation_changes_only_the_three_title_fields`
  (the answer carries an older snapshot; the creation/latest dates and order stay the
  page's).
- **Repeated absence invalidating a legitimate refill.** `invalidateListReads()` was
  replaced by the idempotent, applicable `confirmConversationAbsent()` + `goneMarks`.
  Deterministic regression: `a_repeated_absence_never_discards_a_legitimate_later_list_refill`
  (a refill begun after the first confirmation applies although a duplicate absence
  arrives while it is out), and `a_conversation_missing_from_a_page_is_not_treated_as_deleted`
  (pagination is not absence).

### F2 — session lifecycle

- **Stale completion clearing a current operation's marker.** The old code deleted
  `titleOps[id]` by per-session sequence number before checking the epoch; after a page
  start-over the same sequence number identified a new operation. The record is now an
  operation object cleared only by identity, after the epoch check.
  Deterministic regression: `a_stale_session_completion_never_clears_a_current_operation_record`
  (an earlier session's held answer arrives while the current write is slow; `pending` and
  `slow` stay true and the current write still commits).

### F3 — operation lifecycle

- **Generic 401 discarding state after an unconfirmed mutation.** The 401 branch is now
  endpoint-specific (AD-C2). A malformed/status-only/proxy 401 is `auth_indeterminate`:
  guard + draft preserved, fresh verification requested, nothing released or retried.
  Deterministic regressions: `a_401_outside_the_pre_write_contract_is_indeterminate_and_verifies_the_session`
  (same account: guard, draft and list survive; a later save is refused without a request),
  `an_indeterminate_401_verified_as_another_account_preserves_isolation`,
  `an_indeterminate_401_whose_verification_finds_no_session_changes_nothing_it_should_not`,
  and the definitive path `the_applications_pre_write_401_ends_the_session`.

### F4 — response consistency

- **Compatibility normalization masking contradictions / impossible states.** Mutation
  responses now use `normalizeTitleMutationResponse`; read compatibility is untouched.
  Deterministic regressions:
  `a_title_answer_outside_the_strict_summary_contract_is_never_confirmed` (ten invalid
  shapes: missing origin/reset flag/timestamps, unparseable time, and every impossible
  origin × text × reset combination), plus the R4 operation-semantics scenarios, plus
  `a_reset_to_no_title_at_all_is_a_confirmed_success` (a legitimate sparse reset is not
  over-rejected) and `a_list_read_may_still_use_the_older_title_shape` (read compatibility
  remains).

## 6. Unified mutation-outcome contract

The normative table is Architecture Lock §5. Implementation mapping:

| Answer | Outcome | Apply |
|---|---|---|
| strict valid 200/201, requested id, expected rename/reset state | `confirmed` | `titleConfirmed` + three title fields + success feedback |
| the seven pre-write code/status refusal pairs | `refused` | truthful refusal in the owning editor; queue released |
| `404 conversation_not_found` | `absent` | idempotent authoritative removal (AD-C1) |
| any other 4xx, any 5xx, transport/soft timeout, non-conforming 200/201 | `indeterminate` | guard + draft; release/retry nothing |
| `401` + application `not_authenticated` body | `auth_refused` | `handleAuthLost()` |
| any other `401` | `auth_indeterminate` | guard + draft; a fresh `recheck(…, true)`; release/retry nothing |
| earlier session / invalidated operation / overtaken by a newer applied answer | `superseded` | nothing (except the idempotent `absent` case) |

## 7. Transition-matrix coverage

Dimensions: session (current/stale) × operation (current/invalidated/overtaken) ×
outcome (six) × conversation presence (on page/off page/authoritatively removed) ×
list-read freshness (read begun before/after a confirmation).

| Combination | Kind | Coverage |
|---|---|---|
| current × current × confirmed × on page | reachable | `the_title_outcome_matrix_applies_only_the_permitted_delta`; `a_manual_title_is_validated_saved_and_reset_with_server_state` |
| current × current × confirmed × off page | reachable | `a_confirmed_rename_survives_the_audited_fifty_item_list_transition`; `a_confirmed_reset_survives_an_off_page_list_transition` |
| current × current × refused × any presence | reachable | `the_title_outcome_matrix...`; `only_the_applications_pre_commit_refusals_release_a_title_write_queue` |
| current × current × absent × on page | reachable | `the_title_outcome_matrix...`; `a_title_404_is_not_undone_by_a_list_read_taken_before_the_conversation_vanished` |
| current × current × absent × removed while out | reachable | `a_repeated_absence_never_discards_a_legitimate_later_list_refill`; `deleting_a_conversation_while_its_title_write_is_pending_never_resurrects_it` |
| current × current × indeterminate × any presence | reachable | `the_title_outcome_matrix...`; `an_unconfirmed_title_write_guards_the_conversation_and_leaves_others_usable`; `every_5xx_for_a_title_write_is_indeterminate_and_guards_the_conversation` |
| current × current × auth_refused | reachable | `the_title_outcome_matrix...`; `the_applications_pre_write_401_ends_the_session` |
| current × current × auth_indeterminate × same account | reachable | `the_title_outcome_matrix...`; `a_401_outside_the_pre_write_contract_is_indeterminate_and_verifies_the_session` |
| current × current × auth_indeterminate × different account | reachable | `an_indeterminate_401_verified_as_another_account_preserves_isolation` |
| current × current × auth_indeterminate × ended session | reachable | `an_indeterminate_401_whose_verification_finds_no_session_changes_nothing_it_should_not` |
| current × invalidated × confirmed/refused/indeterminate/auth | reachable | the same `!operative` decision; success branch exercised by `a_title_answer_for_an_invalidated_operation_changes_nothing` |
| current × invalidated × absent | reachable | AD-C1 idempotent path; `a_repeated_absence_never_discards...` (history-read duplicate) and the deletion scenario |
| stale session × any outcome | reachable (the answer arrives) | `a_stale_session_completion_never_clears_a_current_operation_record`; `a_restored_page_of_another_person_discards_the_previous_title_state` |
| list read begun before an absence | reachable | `a_title_404_is_not_undone...`; `a_delayed_list_answer_of_the_previous_person_never_appears_after_a_restoration` |
| list read begun after an absence | reachable | `a_repeated_absence_never_discards_a_legitimate_later_list_refill` |
| current × overtaken by a newer **applied** answer × any outcome | **unreachable by construction** | per-conversation serialization makes a newer applied answer and an older in-flight completion impossible to hold at once. The stage-3 guard is retained as a structural invariant (I8); no meaningless scenario was manufactured. |
| stale session × **write/apply** | **unreachable** | Stage 1 returns before classification; tested through the stale-session scenario. |
| current × invalidated × auth_refused | **not individually manufactured** | it is the same `!operative` → `superseded` decision as the success branch; an authentication refusal for a conversation the page has already forgotten is not an application transition (the session may still be valid for the page). |

## 8. Verification — exact results

Local Windows 11, Python 3.12.10, Node.js 24.14.0. Focused affected-area verification;
the complete repository regression suite was not re-run (not required at this local stage).

| Command / scope | Result |
|---|---|
| `node tests/js/run_scenarios.js --tz=UTC` | **419 scenarios, 0 failed** (406 preserved + 13 new) |
| `tests/test_web_ui_logic.py` (UTC scenarios, timed groups in Europe/Moscow, America/New_York, Asia/Kolkata, and the crosschecks) | **441 passed** |
| `tests/test_web_ui_dom.py` (real `app.js` against the server shell) | **196 passed** |
| `tests/test_web_ui.py` + `tests/test_web_ui_assets.py` + `tests/test_web_ui_integration.py` | **204 passed** |
| `tests/test_web_chat_api.py` (SQLite; includes the new real-route strict-contract crosscheck) | **197 passed** |
| `tests/test_web_app.py` + `tests/test_admin_retention.py` (relevant adjacent regressions) | **93 passed** |

No test was skipped: Node.js was present, so the crosscheck test ran. The real-route
crosscheck (`test_the_real_title_patch_shape_is_accepted_by_the_strict_client_contract`)
feeds the actual rename and reset PATCH answers to the strict client validator through
`tests/js/crosscheck.js` and requires `confirmed` for both.

### 8.1 PostgreSQL

Not used. The change is client-side; no backend, API, schema, persistence or migration
contract changed. The PATCH response shape is pinned by the real route on SQLite and was
already verified on PostgreSQL 16.15 in R4; no affected backend/persistence requirement
made a disposable PostgreSQL environment necessary. Native PostgreSQL on 5432 was not
touched.

## 9. Changed-file inventory (R5 delta)

Production:

- `house_helper/web/static/chat-logic.js` — `hasOwn`; `normalizeTitleMutationResponse`;
  the closed outcome model and endpoint-specific 401 classification; `goneMarks` and
  `confirmConversationAbsent`/`conversationStillRelevant`; operation objects and
  `clearTitleOperation`; `decideTitleTransition`; `markTitleUnconfirmed`,
  `applyConfirmedTitle`, `applyTitleRefusal`, `mergeTitleFields`; the restructured
  `sendTitleMutation`; the bounded marker lifecycle in `loadList`, `conversationGone`,
  `deleteConversation`, `resetData`.

Tests:

- `tests/js/logic_scenarios.js` — the 13 R5 scenarios (§5–§7).
- `tests/js/crosscheck.js` — the `title_mutation` strict-contract task.
- `tests/test_web_chat_api.py` — the real-route PATCH-shape crosscheck test.
- `tests/test_web_ui_assets.py` — updated the two source pins for the new
  `sendTitleMutation(mine, id, op, …)` call; the assertion's intent (the body names only
  the conversation's own title or reset, never an owner) is unchanged.

Documentation:

- `docs/architecture/PP-01-A1-AD-01_Architecture_Lock_v1.0.md` (new)
- `docs/product/PP-01-A1_R5_Implementation_Record.md` (new)
- `docs/product/PP-01-A1_Implementation_Record.md` (section 16)
- `docs/STATUS.md` (R5 entry)

The production delta is approximately 300 restructured/added lines. The AD-01 range
(250–450) is indicative, not a target; no padding or artificial splitting was used.

## 10. Deviations from the Architecture Lock

None material. Two implementation details are recorded for completeness:

1. The prescribed stage 1 includes operation identity; the identity is read in stage 1,
   but whether an inoperative operation may still apply an authoritative `absent` outcome
   is decided in stage 3, because it depends on the classified answer. This is the
   Lock's own AD-C1/idempotence rule, not a departure from the processing order; stages
   1–3 still write no shared state.
2. Strict summary validation requires `created_at`, `updated_at` and
   `last_message_at`, because the real `_conversation_json` always answers them. This is
   the actual backend contract, not an invented requirement (proven by the real-route
   crosscheck).

## 11. Remaining limitations (unchanged, not waived)

- No durable ordering across genuine document reloads; a delivered request can still
  commit afterwards.
- No cross-tab synchronization or ordering.
- No server-side operation journal, distributed sequencing or locking.
- In-session recovery after a genuinely indeterminate write requires a reload; no
  automatic retry exists.
- A definitive session end or a different account discards this page's in-memory guard,
  draft and queue (established `resetData` lifecycle); there is no cross-session record of
  an unconfirmed title write.
- Browser back/forward-cache eligibility and eviction remain a manual smoke-test item.
- A1-05 uncaptioned-photo ambiguity, A1-04 frozen Unicode policy, semantic titles,
  archived original titles, full PP-01.1D and Telegram title commands remain deferred.

## 12. Compliance and scope preservation

- Frozen D1–D7 Scope Lock not edited.
- A1-02…A1-08 not reopened; their R1/R2/R3 mechanisms preserved.
- **A1-01 remains OPEN**; PP-01-A1 is **not** Owner-accepted; no finding is marked
  independently closed.
- FIX-01 behavioral baseline **24 PASS / 6 FAIL** unchanged; KHD-50 release block in force.
- No backend API, schema, migration, prompt, provider, dependency, configuration,
  retention, account-isolation or Telegram change.
- No Production access, deployment, provider call, research execution or Git mutation
  (no commit, push, stash, reset, checkout or stage).

**Verdict: OPTION B IMPLEMENTED — READY FOR INDEPENDENT FUNCTIONAL ACCEPTANCE**

## 13. Addendum — R5-C1 architecture conformance correction (2026-11-27)

The independent R5 functional review executed all 85 selected existing checks successfully
and confirmed four implementation deviations inside the approved Option B architecture.
The Owner authorized a bounded correction. Section 1–12 above are preserved as the
historical R5 record; the correction and its evidence are in
[`PP-01-A1_R5-C1_Correction_Record.md`](PP-01-A1_R5-C1_Correction_Record.md).

Summary of the delta:

- **C1 — unified absence reconciliation.** `applyConversationAbsence` now owns every
  authoritative removal (history/turn/title `404`, confirmed DELETE). The first applicable
  confirmation owns the marker, the list generation, the lifetime retirement and the one
  explicit-delete refill; a duplicate confirmation starts no read and invalidates no
  legitimate refill.
- **C2 — conversation lifetimes.** The per-conversation queue slot is a stable record
  (queue tail, live title work, outstanding DELETEs, retirement generation, refill
  ownership). Title writes and DELETEs capture it at issue; retired queued work sends
  nothing; an old completion cannot touch a fresh lifetime after an authoritative
  re-listing.
- **C3 — partial HTTP metadata.** A body-read failure keeps the response status. Only the
  title classifier reads it, mapping a partial `401` to `auth_indeterminate`; a partial
  `404` is not an absence.
- **C4 — strict timestamps.** Mutation answers validate Gregorian dates, leap years,
  years `0001`–`9999`, clock and offset ranges before conversion; reads keep the
  compatibility parser. `parseTimestampStrict` is exported for direct testing.

The processing boundary, the closed outcome model, the ten invariants and the frozen
D1–D7 scope are unchanged. Verification after the correction: **428 Node scenarios (0
failed), 450 logic-wrapper, 196 DOM, 204 UI/asset/integration, 197 chat-API and 93
adjacent tests passed**, no skip and no weakened assertion. A1-01 remains **OPEN**;
FIX-01 remains 24 PASS / 6 FAIL; KHD-50 remains a release block. Working tree only — no
commit, deployment or Production access.

**Corrected verdict: R5-C1 IMPLEMENTED — READY FOR INDEPENDENT FUNCTIONAL ACCEPTANCE**

## 14. Addendum — R5-C2 ownership boundary correction (2026-11-27)

The independent R5-C1 acceptance review executed 89 selected checks successfully and
confirmed three remaining deviations: A1-01, C1 (unified absence reconciliation), C2
(lifetime and operation ownership), AD-C1 (authoritative absence) and the related I2/I7/I8
conformance. The Owner authorized a bounded functional correction. Sections 1–13 above are
preserved as the historical R5/R5-C1 record; the correction and its full evidence are in
[`PP-01-A1_R5-C2_Correction_Record.md`](PP-01-A1_R5-C2_Correction_Record.md).

Summary of the delta:

- **One ownership contract at the absence boundary.** `absenceIsOwned` decides, before any
  read or write, whether a confirmation was issued against state the page still holds: a
  latest/older read carries its thread object, a turn carries its turn record, and a title
  write or DELETE carries the conversation lifetime it captured. A stale confirmation
  returns before any effect, announcement or read.
- **Retired DELETE.** A retired DELETE whose marker a re-listing revoked cannot remove the
  re-listed conversation, close its view, clear its draft/guard/operation or invalidate a
  legitimate newer list read. A live DELETE still reconciles even when pagination dropped
  the item, and still owns its one refill. Both confirmed DELETE forms (204 and 404) are
  covered.
- **Late history and PATCH absence.** A detached thread's latest/older `404` cannot remove a
  re-listed conversation or close a replacement view; applicable off-page removal is
  preserved. A retired title acknowledgement is idempotent and does not close a temporary
  view owned by a different request; the temporary view closes on its own answer without a
  generation advance or refill.
- **Post-delete focus.** The DELETE result separates `wasCurrent` (this call closed the open
  conversation) from `interaction` (this completion still owns the delete question the page
  shows). `app.js` moves the keyboard only for the latter; a delayed duplicate DELETE cannot
  take focus from an unrelated conversation or disturb its draft.

The processing boundary, the closed outcome model and the ten invariants are unchanged.
Verification after the correction: **434 Node scenarios (0 failed), 456 logic-wrapper, 197
DOM, 204 UI/asset/integration, 197 chat-API and 93 adjacent tests passed**, no skip and no
weakened assertion. A1-01 remains **OPEN**; C3 and C4 remain closed; FIX-01 remains
24 PASS / 6 FAIL; KHD-50 remains a release block. Working tree only — no commit, deployment
or Production access.

Documentation errata recorded in the same round: the soft-timeout terminology is clarified
by [`PP-01-A1-AD-01_Erratum_01.md`](../architecture/PP-01-A1-AD-01_Erratum_01.md), and the
R5-C1 invalid-summary count is corrected (five C4-labelled cases plus direct-parser
coverage, not six labelled cases).

**Corrected verdict: R5-C2 IMPLEMENTED — READY FOR INDEPENDENT FUNCTIONAL ACCEPTANCE**

## 15. Addendum — R5-C3 regression-first ownership closure (2026-11-27)

The independent R5-C2 closure report confirmed three remaining ownership/interaction
defects. The Owner authorized a bounded, regression-first correction. Sections 1–14 above
are preserved as the historical R5/R5-C1/R5-C2 record; the correction and its full evidence
are in [`PP-01-A1_R5-C3_Correction_Record.md`](PP-01-A1_R5-C3_Correction_Record.md).

Summary of the delta:

- **Temporary-history lifetime.** A history read issued for a temporary view captures the
  AD-C1 absence generation that made its view temporary. If an authoritative re-listing
  revokes that marker, the read is stale before any effect — even though its thread object
  is still attached — so it cannot remove the re-listed conversation, clear a confirmed
  title, a fresh operation, its draft or guard, or invalidate a legitimate newer list read.
  The reviewer's "detach the thread and refresh" alternative was rejected as more coupling
  and more state.
- **Pending-turn reconciliation identity.** `reconcile()` clears `checking` only while
  `state.pending[id]` is still the turn record it captured; a replacement turn owns its own
  checking state.
- **Post-delete keyboard ownership.** `app.js` adds one explicit interaction counter,
  advanced only by the person's navigation gestures. The post-delete focus restoration
  captures it when the DELETE is clicked and applies only while no newer interaction has
  taken over. `wasCurrent` and the controller's `interaction` keep their meanings; the
  removal and its refill remain the deletion's, independent of the keyboard.

The processing boundary, the closed outcome model and the ten invariants are unchanged.
Regression-first evidence: each new scenario failed on the original implementation and
passes after the correction. Verification after the correction: **436 Node scenarios (0
failed), 458 logic-wrapper, 199 DOM, 204 UI/asset/integration, 197 chat-API and 93 adjacent
tests passed**, no skip and no weakened assertion. A1-01 remains **OPEN**; C3 and C4 remain
closed; FIX-01 remains 24 PASS / 6 FAIL; KHD-50 remains a release block. Working tree only —
no Git mutation, deployment or Production access.

**Corrected verdict: R5-C3 IMPLEMENTED — RED/GREEN VERIFIED — READY FOR INDEPENDENT CLOSURE**

## 16. Addendum — R5-C4 functional recovery and interaction closure (2026-11-27)

The independent R5-C3 closure report confirmed three remaining functional/interaction
requirements. The Owner authorized a bounded, regression-first correction. Sections 1–15
above are preserved as the historical R5/R5-C1/R5-C2/R5-C3 record; the correction and its
full evidence are in [`PP-01-A1_R5-C4_Correction_Record.md`](PP-01-A1_R5-C4_Correction_Record.md).

Summary of the delta:

- **History recovery.** A stale temporary-history settlement still applies nothing and starts
  no read. When its thread is still the selected, attached, unsynchronized history, `loadLatest`
  now starts exactly one fresh applicable read (`recovering`); success displays the history and
  unlocks the composer, failure takes the established `thread.status === "error"` state with
  its retry, and a second stale answer does not ask again. Recovery goes through `loadLatest`,
  so an already-running applicable read is joined, not duplicated.
- **List focus.** `drawList` captures the focused row's `data-conversation` identity before it
  rebuilds and, if that conversation survives, focuses the rebuilt button. Neither a removed
  row nor focus outside the list is touched; drawer and selection are unchanged.
- **Shopping-draft interactions.** The chat's direct «Что купить?» action and `cancelDraft()`
  advance the existing `interactionSeq`. A DELETE still out keeps its removal and one refill
  but yields the keyboard after those interactions.

The processing boundary, the closed outcome model, AD-C1/AD-C2 and the ten invariants are
unchanged. Regression-first evidence: the extended scenario and both new logic scenarios and
both new mini-DOM scenarios failed on the R5-C3 implementation and pass after the correction.
Verification after the correction: **438 Node scenarios (0 failed), 460 logic-wrapper, 201 DOM,
204 UI/asset/integration, 197 chat-API and 93 adjacent tests passed**, no skip and no weakened
assertion. A1-01 remains **OPEN**; C3 and C4 remain closed; FIX-01 remains 24 PASS / 6 FAIL;
KHD-50 remains a release block. Working tree only — no Git mutation, deployment or Production
access.

**Corrected verdict: R5-C4 IMPLEMENTED — RED/GREEN VERIFIED — READY FOR INDEPENDENT CLOSURE**

## 17. Addendum — R5-C5 desktop focus closure (2026-11-27)

The independent R5-C4 closure report confirmed one remaining defect: on a wide screen the
DELETE completion took the keyboard from a surviving conversation row that the removal's list
redraw had just restored. The Owner authorized a bounded regression-first correction. Sections
1–16 above are preserved as the historical record; the correction and its full evidence are in
[`PP-01-A1_R5-C5_Correction_Record.md`](PP-01-A1_R5-C5_Correction_Record.md).

In `deleteConversationControls`' `result.wasCurrent` path, `app.js` now returns before closing
the drawer or focusing the chat title when the page's active element is a conversation row
inside the list (a non-null `data-conversation`). With no surviving row focused, the ordinary
DELETE restoration is unchanged; the `interaction`/`interactionSeq` gates and the removal, its
one refill, drawer and selection behaviour are untouched. No new mechanism, counter,
controller change or production file was added.

Regression-first evidence: one new wide-screen mini-DOM scenario failed on the R5-C4
implementation ("the surviving row keeps the keyboard after the removal") and passes after the
correction. Verification after the correction: **438 Node scenarios (0 failed), 460
logic-wrapper, 202 DOM, 204 UI/asset/integration tests passed**, no skip and no weakened
assertion. A1-01 remains **OPEN**; C3/C4, AD-C1 and AD-C2 remain closed; FIX-01 remains 24
PASS / 6 FAIL; KHD-50 remains a release block. Working tree only — no Git mutation, deployment
or Production access.

**Corrected verdict: R5-C5 IMPLEMENTED — RED/GREEN VERIFIED — READY FOR INDEPENDENT CLOSURE**

## 18. Addendum — Owner functional acceptance and documentary closure (2026-11-27)

The Owner completed functional acceptance and targeted retesting of PP-01-A1 and decided
**ACCEPT WITH NON-BLOCKING NOTES**. Sections 1–17 above remain the historical R5/R5-C1…R5-C5
technical record and are not rewritten. The final independent Codex acceptance of PP-01-A1-R5-C5
(2026-11-27) returned **PASS — ALL EIGHT FINDINGS CLOSED / READY FOR OWNER FUNCTIONAL
ACCEPTANCE**; it supersedes the §17 "READY FOR INDEPENDENT CLOSURE" status without rewriting that
intermediate chronology. The full acceptance record is
[`PP-01-A1_Owner_Acceptance_Closure_Record.md`](PP-01-A1_Owner_Acceptance_Closure_Record.md).

The acceptance is a later, separate product layer:

- **UX-01…UX-07** were accepted. The Owner-requested corrections (dialog terminology, compact
  rows, menu layout, muted success notices, neutral information styling, plain-language title
  explanations and desktop composer focus after deletion) were implemented in the working tree
  after R5-C5; UX-06 supersedes only the exact Scope Lock D2 explanation **wording**, not its
  mechanism or reset semantics.
- **UX-08** is accepted as a non-blocking limitation: the developer confirmed Tab traversal,
  focus ring, scrolling and native activation in Chrome; the Owner did not reproduce it in Edge,
  so Owner verification is **NOT PASS / NOT REPRODUCED**, not a pass. Arrow-key navigation is
  not required and no code change was made.
- **UX-09** is accepted with `Cache-Control: no-store` preserved; actual BFCache restoration
  remains unverified and the policy was not weakened.
- Title reset per D2, deferred iPhone acceptance (Production post-deployment verification) and
  offline Web/Telegram parity with live Telegram-client verification pending are recorded in the
  closure record.

The UX-correction layer was verified with **438 Node scenarios, 0 failed** and **1165 affected
Python tests passed**; real-browser checks covered the accepted layout, focus and Tab behavior.
No full-suite or PostgreSQL run was made for that layer (client-side plus copy; no server or
persistence contract changed).

**Technical status:** the final independent Codex acceptance returned **PASS — ALL EIGHT FINDINGS
CLOSED**, with **A1-01 — Asynchronous Web consistency — Closed with C1 desktop focus verified**
and **17 PASS / 0 FAIL / 0 SKIP**; no remaining material technical defects or evidence gaps were
recorded. A1-02…A1-08 and AD-C1/AD-C2 remain independently closed as recorded; R5-C1…R5-C4 were
independently closed, and R5-C5 was RED/GREEN verified and closed by the final acceptance. The
`A1-01 OPEN` statements in sections 1–17 (including §17's
"READY FOR INDEPENDENT CLOSURE" verdict) are the historically accurate pre-acceptance
intermediate status and are preserved as chronology, not as the final disposition. The final
Codex acceptance covers the R5-C5 baseline; the later UX-01…UX-09 corrections are the subsequent
Owner acceptance layer and are not covered by that Codex audit. Option B, the ten invariants, the
frozen D1–D7 Scope Lock and the Architecture Lock plus Erratum 01 are unchanged. The final Codex
report itself is not a separate repository artifact; its verdict, disposition and counts are
recorded from the Owner-supplied final acceptance brief (see the closure record, §4.1).

**Release boundary:** FIX-01 remains **24 PASS / 6 FAIL**; KHD-50 remains a release block; the
latest Owner release strategy remains the combined **HH-BRAND-01-FIX-01 + PP-01-A1** candidate
(KHD-54). The acceptance is not a release, deployment or commit. Working tree only — no Git
mutation, provider call, deployment or Production access.

**Verdict: OWNER ACCEPTED WITH NON-BLOCKING NOTES — READY FOR OWNER COMMIT.**
