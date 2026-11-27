# House Helper — PP-01-A1-R5-C1 Architecture Conformance Correction
## Implementation Record

| Field | Value |
|---|---|
| Date | 2026-11-27 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` (full PP-01-A1 plus R1–R5 uncommitted in the working tree) |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, frozen, unchanged) |
| Architecture authority | [`PP-01-A1-AD-01_Architecture_Lock_v1.0.md`](../architecture/PP-01-A1-AD-01_Architecture_Lock_v1.0.md) (Option B) |
| Input | Independent R5 functional review (four confirmed deviations; 85 selected existing checks passed) and its targeted remediation proposal |
| Owner authorization | Bounded correction of the confirmed deviations inside the Architecture Lock; engineering judgment permitted; new architecture/product decisions require Owner approval |
| Status | **R5-C1 IMPLEMENTED — READY FOR INDEPENDENT FUNCTIONAL ACCEPTANCE.** Working tree only; not committed, independently accepted, released or deployed. |
| Preserved baseline | A1-02…A1-08 independently CLOSED; **A1-01 OPEN**; FIX-01 **24 PASS / 6 FAIL**; KHD-50 release block |

## 1. Executive conclusion

The four deviations confirmed by the R5 functional review are corrected inside the
approved Option B architecture. The processing boundary is unchanged:
**Validate Identity → Validate Raw Contract → Decide Transition → Apply Scoped Changes.**

Two of the corrections share one mechanism. A conversation **lifetime record** replaces
the bare promise in the per-conversation queue slot, and one **authoritative-absence
reconciliation** now owns every removal path. This makes "which work is still applicable"
and "which call owns the one cleanup/refill" structural rather than incidental. The other
two corrections are local: preserve the HTTP status when a response body cannot be read,
and validate raw calendar components before converting a mutation timestamp.

No product or architecture decision was added. No backend, API, schema, migration,
prompt, provider or deployment change was made. Production changes are confined to
`house_helper/web/static/chat-logic.js`; regression coverage to
`tests/js/logic_scenarios.js`.

## 2. Baseline verification

Before any edit, at `def03ea` with the complete PP-01-A1/R1–R5 working tree:

| Check | Result |
|---|---|
| `node tests/js/run_scenarios.js --tz=UTC` | **419 scenarios, 0 failed** |
| `tests/test_web_ui_logic.py` | **441 passed** |
| `tests/test_web_ui_dom.py`, `test_web_ui.py`, `test_web_ui_assets.py`, `test_web_ui_integration.py` | **400 passed** |
| `tests/test_web_chat_api.py` | **197 passed** |
| `tests/test_web_app.py`, `test_admin_retention.py` | **93 passed** |

## 3. Assessment of the independent remediation proposal

The reviewer's four recommendations were reviewed against the actual source before
editing. All four mechanisms were accepted; two were refined so the code expresses the
requirement more exactly.

### 3.1 Accepted as proposed

- **One shared absence reconciliation.** `conversationGone` (read/turn/title 404) and the
  confirmed DELETE now pass through `applyConversationAbsence`. The first applicable
  confirmation records the AD-C1 marker, advances `listEpoch`, retires the title lifetime,
  forgets the conversation and owns the explicit-delete refill. A later confirmation only
  drops what the page currently holds of the conversation, which preserves the audited
  temporary-view behaviour without repeating global effects.
- **A stable lifetime record in the queue slot.** The slot is now
  `{tail, queued, deletes, retiredGeneration, refillOwned}` instead of a bare tail promise.
  A title write and an explicit DELETE capture the record when issued; applicability is
  decided from the captured record, never from whatever the slot holds when the answer
  arrives. `delete titleQueues[id]` (which left chained callbacks runnable) is gone:
  retirement sets the generation and detaches the slot.
- **Retain the status on a body-read failure.** `http` answers
  `{kind: "network", partial: true, status}`; only the title classifier reads it. The
  partial answer stays in the network category, so every other endpoint classifier is
  bit-for-bit unchanged.
- **A strict mutation-only timestamp mode.** `parseTimestampIn(value, strict)` shares the
  lexical pattern and component extraction; `normalizeTitleMutationResponse` uses
  `parseTimestampStrict`; read parsing keeps its historical range.

### 3.2 Refinements (technical judgment, not a departure)

1. **DELETE return value.** The proposal said a duplicate DELETE returns
   `{ok: true, wasCurrent: false}`. Implemented literally, `wasCurrent` would describe the
   page at arrival; the only consumer (`app.js`) uses it to decide whether to close the
   drawer and focus the title. The correction returns
   `wasCurrent: removed.first && wasCurrentAtArrival`, so the flag means "this call closed
   the open view", which is what the duplicate-safety requirement asks for.
2. **Bounded lifetime cleanup.** The proposal's "release a DELETE-only record when its
   last DELETE settles … and no title work uses it" needs a count of live title work.
   A `queued` counter was added; `releaseTitleLifetime` removes a record only when
   `queued == 0`, `deletes == 0`, it is live and its slot still points at it. A retired
   record is never in a slot, so an old completion can never release a newer record with
   the same conversation id.
3. **Absence acknowledgement test.** Implemented as `titleAbsenceIsCurrent(id, op)`:
   the operation's `retiredGeneration` must equal the **current** `goneMarks[id]`. An
   authoritative re-listing revokes the marker, so an old write's late 404 becomes stale
   instead of removing the fresh lifetime — exactly the proposal's intent.
4. **Strict offset range.** Offsets are validated as `0…23` hours and `0…59` minutes.
   `datetime.isoformat()` never emits anything wider; the values are checked before the
   offset can roll a date over.

No simpler equivalent was found that met all requirements. The lifetime record is the
smallest construction that gives writes, DELETEs, retirement and re-listing a shared
identity without a persistent journal or cross-tab state.

## 4. C1 — Unified authoritative-absence reconciliation

`applyConversationAbsence(id, notice)` is the only path that applies an authoritative
absence. Callers:

| Path | Caller | `closeCurrent` | Refill |
|---|---|---|---|
| Latest / older history `404` | `conversationGone` | true | only if a DELETE is outstanding |
| Turn `conversation_not_found` | `settle` → `conversationGone` | true | only if a DELETE is outstanding |
| Title PATCH `404 conversation_not_found` | `sendTitleMutation` → `conversationGone` | true | only if a DELETE is outstanding |
| DELETE `204` / `404` (first confirmation) | `deleteConversation` → `applyConversationAbsence` | false (first still forgets) | one, owned here |
| DELETE `204` / `404` (duplicate) | same | false | none |

Rules:

- **First applicable confirmation** = no marker exists and `conversationStillRelevant(id)`,
  or the confirmation is a DELETE the page itself issued (`force: true`). That request was
  about a conversation the page held, so its confirmed deletion is authoritative even if a
  meanwhile re-listed page has already dropped the item (the list must still be filled back
  up), which preserves the established DELETE refill behaviour. It sets
  `goneMarks[id] = ++listEpoch`, retires the lifetime, forgets the conversation, applies
  the caller's banner and, if the retired lifetime has an unsettled DELETE
  (`deletes > 0`, `refillOwned == false`), starts its **one** list refill — immediately, or
  through `listRefreshOnUnlock` when the session gate is closed.
- **Duplicate confirmation** of an already-marked removal advances nothing, starts no read
  and changes no list state. It still drops a temporary view the page created after the
  removal (that view's own 404 is its answer); a duplicate DELETE passes
  `closeCurrent: false` and leaves the current view and any newer delete question alone.
- **Pagination is not absence**: `loadList` is unchanged and only an application
  `conversation_not_found` or a confirmed DELETE reaches this function.
- The accepted response contract is not widened: only `404` with
  `conversation_not_found` (title/read/turn) and DELETE `204`/`404` are authoritative.

This removes the confirmed defect: `deleteConversation` previously ignored
`confirmConversationAbsent`'s answer and always called `loadList`, which incremented
`listRead` and invalidated a legitimate post-removal read.

## 5. C2 — Conversation lifetime and operation ownership

Three identities are kept distinct:

1. **Session identity** — `epoch`; a completion from another session returns at stage 1.
2. **Conversation lifetime identity** — the record captured when the work was issued.
   A removal retires it (`retiredGeneration`); an authoritative re-listing creates a fresh
   record for the same id.
3. **Operation identity** — the per-request object in `titleOps[id]` (object identity).

Enforcement:

- `saveTitle`/`resetTitle` capture `titleLifetimeFor(id)` **before** queueing and embed it
  in the operation; `queueTitleMutation(id, lifetime, work)` chains on `lifetime.tail`, so
  FIFO order per conversation is unchanged.
- `sendTitleMutation` checks `mine === epoch && titleLifetimeApplicable(op.lifetime)`
  before dispatch **and after every session-gate wait**. A retired queued write settles
  `{ok: false, reason: "stale"}` and never sends a PATCH.
- Completion: `operative = titleOps[id] === op && titleLifetimeApplicable(op.lifetime)`.
  A retired operation's non-absence answers are `superseded`; only an absence whose
  generation still equals the live marker is applied (idempotently).
- Slow feedback (`onTimeout`) checks epoch, operation identity **and** lifetime
  applicability before touching `op.slow` or the view.
- The session gate and `resetData` behaviour is unchanged: same-account verification keeps
  the live queue and guard; an account change clears the registry by epoch and reset.
- Off-page conversations are unaffected: `loadList` never touches a lifetime record, and
  an in-flight write applies through `titleConfirmed` as before.
- `releaseTitleLifetime` bounds the records: one per conversation the page holds, memory
  only, cleared by `resetData`, never removing a newer record.

## 6. C3 — Partial HTTP response metadata

- `http` now returns `{kind: "network", timedOut, partial: true, status: response.status}`
  when the headers arrived and the body read failed. A failure before the headers still has
  no status.
- `classifyConversationUpdate`'s network branch maps `partial && status === 401` to
  `auth_indeterminate`, which reuses the existing AD-C2 apply path: guard + draft
  preserved, nothing released or retried, one fresh `recheck(undefined, true)`.
- A partial `200`, `404`, `5xx` or anything else stays `indeterminate`. In particular an
  unreadable `404` is **not** an authoritative absence.
- A complete `401` with the application's `not_authenticated` body remains `auth_refused`.
- The result stays in the network category, so `classifyRead`, `classifyCreate`,
  `classifyMessageSend`, `classifyConversationDelete` and `classifyMe` are unchanged (each
  returns from its network branch before reading a status).

## 7. C4 — Strict timestamp validation

`parseTimestampIn(value, strict)` validates every component before conversion:

- real Gregorian month lengths and leap-year rules (`2026-02-29`, `2026-04-31`,
  `2026-11-31` are impossible);
- year `0001`–`9999` (`0000` is rejected); conversion uses `setUTCFullYear`, so `0099` is
  the year 99, not 1999 (the compatibility parser keeps `Date.UTC` and its old mapping);
- clock `00–23:00–59:00–59` in strict mode (second 60 is rejected; the read parser keeps
  its historical 1–60 range);
- offset hours `0–23` and minutes `0–59` in strict mode, validated before conversion;
- the accepted spellings and fractional-second handling are unchanged (T or space, `Z`,
  `±HH:MM`, `±HHMM`, 1–9 fractional digits).

`normalizeTitleMutationResponse` uses the strict parser for `created_at` and `updated_at`
(required) and for a non-null `last_message_at` (which may explicitly be `null`). A body
that fails is indeterminate; it never updates the title, closes the editor or reports
success. The default parser used for reads is unchanged except for the shared extraction.

## 8. Invariant enforcement after C1

| Invariant | Enforcement point (C1 delta) | Evidence |
|---|---|---|
| I1 — stale session cannot modify current state | `sendTitleMutation` epoch check before classification, after every gate wait; `onTimeout`; `resetData` | `a_stale_session_completion_never_clears_a_current_operation_record`; `a_restored_page_of_another_person_discards_the_previous_title_state`; `a_title_write_waiting_on_the_session_gate_is_never_sent_after_its_lifetime_is_retired` |
| I2 — an operation clears only its own record | `titleOps[id] === op`; `clearTitleOperation`; lifetime identity on completion | same scenarios; `a_relisted_conversation_gets_a_fresh_lifetime_and_an_old_404_cannot_touch_it` |
| I3 — success needs identity + strict contract | `normalizeTitleMutationResponse` + `parseTimestampStrict`; id equality; `isExpectedTitleState` | `a_title_answer_outside_the_strict_summary_contract_is_never_confirmed` (five new C4-labelled calendar cases, plus direct-parser coverage — see §10 correction note); `strict_mutation_timestamps_validate_the_calendar_before_any_conversion` |
| I4 — unknown outcomes cannot release writes | `markTitleUnconfirmed`; `titleBlocked` before every send | `an_unreadable_401_answer_is_indeterminate_and_verifies_the_session`; `an_unreadable_404_answer_is_not_an_authoritative_absence`; the outcome matrix |
| I5 — confirmation updates title fields only | `applyConfirmedTitle` → `mergeTitleFields` | `a_title_confirmation_changes_only_the_three_title_fields` |
| I6 — membership/order/activity belong to their authorities | `applyConversationAbsence` only removes; `touchListItem`/`applyTurn` untouched | all list scenarios; `a_confirmed_rename_survives_the_audited_fifty_item_list_transition` |
| I7 — absence is idempotent and does not invalidate newer reads | `applyConversationAbsence` first/duplicate rule; `goneMarks`; `loadList` generation check | `a_delayed_duplicate_delete_does_not_discard_a_legitimate_post_removal_refill`; `a_deletion_confirmed_while_the_page_is_held_back_refills_the_list_exactly_once`; `a_repeated_absence_never_discards_a_legitimate_later_list_refill` |
| I8 — older responses cannot overwrite newer confirmed state | per-conversation serialization; operation/lifetime identity; `titleMutationSuperseded` | `a_delayed_reset_cannot_replace_a_newer_rename_or_close_its_editor`; `a_relisted_conversation_gets_a_fresh_lifetime_and_an_old_404_cannot_touch_it` |
| I9 — no success before validation | `confirmed` only after stages 2/3 | all strict-contract scenarios |
| I10 — session transitions belong to the lifecycle | `auth_refused` → `handleAuthLost`; `auth_indeterminate` → fresh `recheck(…, true)` | the three AD-C2 scenarios; the unreadable-401 scenario |

## 9. Soft-timeout terminology

The runtime mechanism is unchanged (non-aborting timer; the queue stays behind the
request until it really settles). The documentation now distinguishes:

- **Intermediate pending / slow** — the original request is still outstanding; a definitive
  answer may still arrive (the `onTimeout` comment and the `http` soft-timeout comment say
  so explicitly).
- **Confirmed / definitively refused** — a settled answer that proves the outcome.
- **Terminal indeterminate** — a settled request whose answer cannot confirm the write.

The word *indeterminate* is reserved for the third. No behaviour changed.

## 10. Verification — exact results

Local Windows 11, Python 3.12.10, Node.js 24.14.0. Focused affected-area verification; the
full repository regression suite was not re-run (not required at this stage).

| Command / scope | Result |
|---|---|
| `node tests/js/run_scenarios.js --tz=UTC` | **428 scenarios, 0 failed** (419 preserved + 9 new) |
| `tests/test_web_ui_logic.py` (multi-zone wrapper + crosschecks) | **450 passed** |
| `tests/test_web_ui_dom.py` (real `app.js` against the server shell) | **196 passed** |
| `tests/test_web_ui.py` + `tests/test_web_ui_assets.py` + `tests/test_web_ui_integration.py` | **204 passed** |
| `tests/test_web_chat_api.py` (SQLite; includes the real-route strict-contract crosscheck) | **197 passed** |
| `tests/test_web_app.py` + `tests/test_admin_retention.py` | **93 passed** |

No test was skipped. No existing assertion was weakened. The real-route rename/reset
crosscheck still feeds the actual PATCH answers to the strict client contract, now with
the calendar-strict parser, and requires `confirmed` for both.

New deterministic scenarios (9):

1. `a_delayed_duplicate_delete_does_not_discard_a_legitimate_post_removal_refill`
2. `a_deletion_confirmed_while_the_page_is_held_back_refills_the_list_exactly_once`
3. `a_queued_rename_is_dropped_when_the_reset_ahead_of_it_finds_the_conversation_gone`
4. `a_title_write_waiting_on_the_session_gate_is_never_sent_after_its_lifetime_is_retired`
5. `a_relisted_conversation_gets_a_fresh_lifetime_and_an_old_404_cannot_touch_it`
6. `an_unreadable_401_answer_is_indeterminate_and_verifies_the_session`
7. `an_unreadable_404_answer_is_not_an_authoritative_absence`
8. `a_strict_mutation_answer_accepts_a_leap_day_an_early_year_and_fractional_seconds`
9. `strict_mutation_timestamps_validate_the_calendar_before_any_conversion`

The strict-contract table gained five C4-labelled calendar cases (impossible
created/updated/last-message day, year zero, rollover offset), plus direct-parser coverage in
`strict_mutation_timestamps_validate_the_calendar_before_any_conversion`.

> **Documentary correction (2026-11-27, R5-C2).** The original wording here said "six".
> The evidence is **five** C4-labelled cases in `INVALID_RENAME_ANSWERS`; the remaining
> calendar/clock/offset bounds are exercised directly against `parseTimestampStrict` in the
> same scenario set. The tests are unchanged; only the count is corrected. See
> [`PP-01-A1_R5-C2_Correction_Record.md`](PP-01-A1_R5-C2_Correction_Record.md) §11.

### 10.1 PostgreSQL

Not used. The change is client-side; no backend, API, schema, persistence or migration
contract changed, and the PATCH response shape is pinned by the real route on SQLite (and
was verified on PostgreSQL 16.15 in R4). Native PostgreSQL on 5432 was not touched.

## 11. Changed-file inventory

Production:

- `house_helper/web/static/chat-logic.js` — `parseTimestampIn` / `parseTimestampStrict`;
  strict timestamps in `normalizeTitleMutationResponse`; partial status in `http`; the
  partial-401 branch in `classifyConversationUpdate`; the lifetime record
  (`newTitleLifetime`, `titleLifetimeFor`, `titleLifetimeApplicable`,
  `releaseTitleLifetime`), `newTitleOperation(…, lifetime)`, `queueTitleMutation(id,
  lifetime, work)`, `decideTitleTransition`/`titleAbsenceIsCurrent`, applicability checks in
  `sendTitleMutation`, `applyConversationAbsence` (replacing `confirmConversationAbsent`
  and the body of `conversationGone`), the DELETE dispatch/settlement, `forgetConversation`
  (no longer deletes the queue slot), and the soft-timeout comments.

Tests:

- `tests/js/logic_scenarios.js` — the nine new scenarios and the five C4-labelled calendar
  invalid cases (plus direct-parser coverage; see the §10 correction note).

Documentation:

- `docs/product/PP-01-A1_R5-C1_Correction_Record.md` (this record)
- `docs/product/PP-01-A1_R5_Implementation_Record.md` (R5-C1 section)
- `docs/product/PP-01-A1_Implementation_Record.md` (section 16 update)
- `docs/STATUS.md` (R5-C1 entry)

No other production or test file was changed.

## 12. Remaining limitations and evidence gaps

- No durable ordering across genuine document reloads; a delivered request can still
  commit afterwards.
- No cross-tab synchronization or ordering, and no server-side operation journal,
  distributed sequencing or locking.
- Recovery after a genuinely indeterminate write still requires a reload; there is no
  automatic retry.
- A definitive session end or a different account discards this page's in-memory guard,
  draft and queue (established lifecycle).
- Browser back/forward-cache eligibility and eviction remain a manual smoke-test item.
- A duplicate DELETE still announces the deletion (`announce(TEXT.deleted)`) even though it
  changes no state: it is a transient, truthful message, not a state change. This is
  recorded as a deliberate choice; no requirement asks for it to be suppressed.
- The strict parser rejects an offset above `±23:59`. No `datetime`-derived ISO timestamp
  can carry one, but a future non-Python producer with a wider offset would be treated as
  an invalid mutation answer rather than normalized.
- No independent execution of the proposed regression matrix beyond the nine scenarios was
  performed; the reviewer's proposal described scenarios, not an executable suite.

## 13. Compliance and scope preservation

- Frozen D1–D7 Scope Lock not edited; Architecture Lock not silently amended.
- A1-02…A1-08 not reopened; A1-01 remains **OPEN**.
- FIX-01 behavioural baseline **24 PASS / 6 FAIL** unchanged; KHD-50 release block in
  force; PP-01-A1 is not Owner-accepted.
- No backend/API/schema/migration/prompt/provider/dependency/configuration/retention/
  account-isolation/Telegram change.
- No Production access, deployment, provider call, research execution or Git mutation
  (no commit, push, stash, reset, checkout or stage).

**Verdict: R5-C1 IMPLEMENTED — READY FOR INDEPENDENT FUNCTIONAL ACCEPTANCE**
