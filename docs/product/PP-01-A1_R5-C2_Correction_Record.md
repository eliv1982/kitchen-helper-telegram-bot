# House Helper — PP-01-A1-R5-C2 Ownership Boundary Correction
## Implementation Record

| Field | Value |
|---|---|
| Date | 2026-11-27 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` (full PP-01-A1 plus R1–R5-C1 uncommitted in the working tree) |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, frozen, unchanged) |
| Architecture authority | [`PP-01-A1-AD-01_Architecture_Lock_v1.0.md`](../architecture/PP-01-A1-AD-01_Architecture_Lock_v1.0.md) (Option B) and its [Erratum 01](../architecture/PP-01-A1-AD-01_Erratum_01.md) |
| Input | Independent R5-C1 acceptance review: 89 selected checks passed, three implementation deviations confirmed (A1-01, C1, C2, AD-C1 and the related I2/I7/I8 conformance) |
| Owner authorization | Bounded functional correction inside Option B; production boundary `chat-logic.js`, plus `app.js` only for post-delete focus and directly related interaction applicability |
| Status | **R5-C2 IMPLEMENTED — READY FOR INDEPENDENT FUNCTIONAL ACCEPTANCE.** Working tree only; not committed, independently accepted, released or deployed. |
| Preserved baseline | A1-02…A1-08 independently CLOSED; **A1-01 OPEN**; C3 and C4 independently CLOSED; FIX-01 **24 PASS / 6 FAIL**; KHD-50 release block |

## 1. Executive conclusion

The three remaining deviations share one recurring cause: absence reconciliation enforced
idempotence from the marker store alone, and therefore did not enforce *ownership* — which
conversation lifetime, thread or turn a completion belongs to. The correction adds one
structural applicability contract at the absence application boundary:

**a confirmation may change state only if it was issued against state the page still holds.**

Three identities are now carried by every absence caller:

- a **history read** carries the thread object it read (`state.threads[id] === thread`);
- an **unresolved turn** carries its own turn record (`state.pending[id] === turn`);
- a **title write or DELETE** carries the conversation lifetime it captured when issued (a
  live lifetime may apply a first removal; a retired one may only acknowledge the marker
  that retired it).

The first applicable confirmation keeps its single ownership of the AD-C1 marker, the list
generation, the lifetime retirement and the one explicit-delete refill. A retired
confirmation whose marker a re-listing revoked is stale and changes nothing. A retired title
acknowledgement is idempotent and cannot close a temporary view owned by another request.
The DELETE result now separates `wasCurrent` (this call closed the open conversation) from
`interaction` (this completion still owns the delete question the page shows); `app.js`
moves the keyboard only for the latter.

No product or architecture decision was added. No backend, API, schema, migration, prompt,
provider or deployment change was made. Production changes are confined to
`house_helper/web/static/chat-logic.js` and the authorized post-delete focus block in
`house_helper/web/static/app.js`; regression coverage to the two authorized JS test files.

## 2. Baseline verification

Before any edit, at `def03ea` with the complete PP-01-A1/R1–R5-C1 working tree:

| Check | Result |
|---|---|
| `node tests/js/run_scenarios.js --tz=UTC` | **428 scenarios, 0 failed** |
| `tests/test_web_ui_logic.py` | **450 passed** |
| `tests/test_web_ui_dom.py` | **196 passed** |
| `tests/test_web_ui.py` + `test_web_ui_assets.py` + `test_web_ui_integration.py` | **204 passed** |
| `tests/test_web_chat_api.py` | **197 passed** |
| `tests/test_web_app.py` + `test_admin_retention.py` | **93 passed** |

This matches the R5-C1 record. No test was skipped or weakened.

## 3. Assessment of the independent reviewer's recommendations

The reviewer's recommendations were advisory. They were evaluated against the actual source
and implemented as one coherent contract rather than as caller-specific patches.

### 3.1 Accepted

- **A single absence-applicability boundary.** Ownership is decided inside
  `applyConversationAbsence`, before any read or write, through `absenceIsOwned`. Callers
  supply evidence; they do not each re-implement the rule.
- **Lifetime ownership for the DELETE.** A live DELETE may reconcile and start its one
  refill even when pagination dropped the item (`force`); a retired DELETE may only
  acknowledge the marker that retired it. `force` is retained but is now subordinate to
  lifetime ownership, as the brief required. Removing `force` alone was verified insufficient
  and was not done.
- **Thread ownership for latest/older history.** A detached thread's `404` cannot remove a
  re-listed conversation or close a replacement view. Applicable off-page removal is
  preserved because ownership is the thread object, not the open conversation.
- **Turn ownership preserved.** The turn path keeps its existing `state.pending[id] === turn`
  guard and now also passes the turn as the reconciliation's evidence, so the rule is
  uniform and structural.
- **Retired title acknowledgement is idempotent, not closing.** The title path passes its
  captured lifetime and applies `closeCurrent` only while the lifetime is operative. A
  retired acknowledgement cannot close a temporary view; that view closes on its own
  history answer.
- **Post-delete focus gated by interaction applicability.** `wasCurrent` keeps its meaning.
  A new narrowly scoped `interaction` result says whether this completion still owns the
  delete question the page shows.

### 3.2 Refinements (technical judgment, not a departure)

1. **One predicate, not three copies.** `absenceIsOwned` gives lifetime checks precedence,
   then thread, then turn, then defaults to true only for callers that pass no evidence
   (none remain in the production paths; the default preserves any future read-only caller).
2. **The DELETE result contract.** The brief permitted a narrowly scoped internal
   result-contract refinement. `interaction` is computed before the reconciliation can
   clear `state.deleting`, so it describes the arrival state. Its only consumer is the
   `deleteConversationControls` `.then` in `app.js`, which was re-read; `wasCurrent`
   consumers are unchanged.
3. **A retired acknowledgement may not even announce.** A stale (not-owned) confirmation
   returns before `announce`, because the page's newer state says the conversation exists.
   An owned duplicate (marker current) still announces the truthful deletion, preserving the
   R5-C1 deliberate choice.

### 3.3 No material disagreement

No recommendation was rejected on technical grounds. The only judgment call is that
`interaction` is a better home for keyboard ownership than overloading `wasCurrent`, which
the brief itself directed.

## 4. Complete absence-caller ownership map

| Caller | Path | Evidence passed | `closeCurrent` | First-effect owner | Refill |
|---|---|---|---|---|---|
| `fetchLatest` | latest history `404` | `{thread}` | true | this call if owned and relevant | only if an explicit DELETE is outstanding |
| `loadOlder` | older history `404` | `{thread}` | true | same | only if an explicit DELETE is outstanding |
| `settle` | turn `conversation_not_found` | `{turn}` | true | same | only if an explicit DELETE is outstanding |
| `sendTitleMutation` | PATCH `404 conversation_not_found` | `{lifetime}`, `closeCurrent: operative` | true only while operative | this call if owned | only if an explicit DELETE is outstanding |
| `deleteConversation` | DELETE `204` / `404` | `{lifetime, force: true}` | false | this call if its lifetime is live | this lifetime's one refill |
| `deleteConversation` | duplicate acknowledgement | `{lifetime}` retired and marker matches | false | none | none |
| `deleteConversation` | retired + marker revoked | `{lifetime}` | (returns before any effect) | none | none |
| temporary view | its own history `404` | `{thread}` (the view's thread) | true | none (marker exists) | none |
| — | pagination (missing from a page) | n/a | n/a | never an absence | none |

Ownership rules:

```
absenceIsOwned(id, notice):
  lifetime given:
      lifetime.retiredGeneration === null        -> true   (live: may apply a first removal)
      goneMarks[id] === lifetime.retiredGeneration -> true  (acknowledges its own removal)
      otherwise                                   -> false  (retired and superseded)
  thread given:  state.threads[id] === notice.thread
  turn given:    state.pending[id] === notice.turn
  otherwise:     true
```

- **Applicable first absence:** marker absent, owned, and (`conversationStillRelevant(id)` or
  a live DELETE's `force`). Owns `goneMarks[id] = ++listEpoch`, lifetime retirement, the
  caller's notice/closure and, if the retired lifetime has `deletes > 0` and
  `refillOwned == false`, its one refill (immediate or `listRefreshOnUnlock`).
- **Duplicate acknowledgement:** marker present and owned. No generation advance, no read,
  no refill. It drops only what the page currently holds when the caller's `closeCurrent`
  says so (the temporary view's own history answer; never a duplicate DELETE, never a
  retired title acknowledgement).
- **Stale / inapplicable completion:** ownership false. Returns
  `{first:false, refill:false, applicable:false}` before any state change, announcement or
  emit.
- **Pagination** remains outside this function: `loadList` never marks an absence.

## 5. The common applicability mechanism

`applyConversationAbsence(id, notice)` is still the only path that applies an authoritative
absence. Its new first statement is:

```js
if (!absenceIsOwned(id, notice)) {
  return { first: false, refill: false, applicable: false };
}
```

No new framework, no persistent journal, no permanent tombstone and no global list
invalidation were introduced. The existing bounded marker store and lifetime record are
kept; the lifetime record is unchanged in shape. `forgetConversation` still never deletes
the queue slot (retirement detaches it first).

## 6. Evidence for the three confirmed deviations

### 6.1 Retired DELETE (both response forms)

`deleteConversation` previously called `applyConversationAbsence` with `force: true`
unconditionally. After an earlier confirmation had retired the lifetime and a subsequent
authoritative re-listing had revoked the marker, the retired DELETE looked like a first
applicable confirmation: it advanced `listEpoch`, retired the *fresh* lifetime (invalidating
a legitimate newer list read), forgot the re-listed conversation (clearing its draft, guard
and operation), closed the replacement view and started another refill.

Now the captured lifetime is the evidence. A retired lifetime whose marker was revoked is
stale and returns before any effect. Regression:
`a_retired_delete_cannot_touch_a_relisted_lifetime_204` and
`..._404` run the same scenario with both confirmed DELETE forms and assert that fresh
membership, the fresh title operation, its draft, the absence guard and a legitimate pending
list read are all unchanged, and that the pending read still applies.

### 6.2 Late latest/older history and PATCH absence

`conversationGone` passed no ownership evidence. A latest or older read of a thread the page
had already detached could still find the marker revoked by a re-listing and remove the
fresh conversation or close the replacement view. Now the read passes its thread object and
the turn path passes its turn record; a detached answer returns before any effect.

The title path previously closed the current view even when the write's lifetime was
retired. A retired PATCH `404` therefore closed a temporary view owned by a different
history request. Now `closeCurrent` is `operative` and the lifetime is passed as evidence;
a retired acknowledgement cannot close it. Regressions:
`a_detached_latest_history_absence_cannot_touch_a_relisted_conversation`,
`a_detached_older_history_absence_cannot_touch_a_relisted_conversation`,
`an_applicable_older_absence_still_removes_an_off_page_conversation` (applicable removal
preserved) and `a_retired_title_acknowledgement_does_not_close_a_temporary_view` (the view's
own answer later closes it with no further read).

### 6.3 Post-delete focus

`app.js` previously moved focus to `newConversation` for every successful DELETE that did
not close the open conversation, including delayed duplicates. A delayed duplicate DELETE
therefore took the keyboard from an unrelated conversation. The DELETE result now carries
`interaction = ownedQuestion && wasCurrent` (computed at arrival, before the reconciliation
can clear the question), and `app.js` returns before touching focus unless `interaction` is
true. `wasCurrent` is unchanged. Regression:
`a_delayed_duplicate_delete_does_not_take_focus_from_another_conversation` (mini-DOM) deletes
A, reconciles the removal, opens B, focuses B's composer with a draft, settles the delayed
DELETE, and asserts B's focus, draft and view are undisturbed. The existing DOM scenarios
that assert legitimate `chat-title` focus after deleting the open conversation still pass.

## 7. Structural postconditions and request-count evidence

| Postcondition | Evidence |
|---|---|
| A stale DELETE starts no list read and does not advance the generation | retired-204/404 scenarios assert the GET count is unchanged and a pending legitimate read still applies |
| A stale DELETE leaves the fresh operation, draft and guard intact | same: `title.pending`, `title.draft`, `title.blocked === false`; the fresh PATCH later returns `ok` |
| A duplicate DELETE starts no further refill | preserved R5-C1 scenario `a_delayed_duplicate_delete_does_not_discard_a_legitimate_post_removal_refill` |
| A live DELETE (even when the item is off the page) still owns exactly one refill | preserved `deleting_a_visible_conversation_reveals_the_one_beyond_the_page`; held-back refill exactly once |
| A detached latest/older absence changes nothing and reads nothing | both detached scenarios assert the GET count is unchanged |
| Applicable off-page removal still removes the conversation and leaves the open one alone | `an_applicable_older_absence_still_removes_an_off_page_conversation` |
| A retired title acknowledgement does not close the temporary view | `a_retired_title_acknowledgement_does_not_close_a_temporary_view`: `currentId` and `thread.status === "loading"` preserved, banner null |
| The temporary view's own answer closes it without generation advance or refill | same scenario: `currentId === null`, GET count unchanged |
| A delayed DELETE moves no focus and gives no current-view feedback | mini-DOM scenario: `activeElement`, draft, title preserved; banner hidden |

## 8. I1–I10 conformance after R5-C2

| Invariant | Enforcement point (R5-C2 delta) | Evidence |
|---|---|---|
| I1 — a stale session cannot modify current state | unchanged epoch checks before classification and after every gate wait | the R5-C1 session scenarios (unchanged) |
| I2 — an operation clears only its own record | `titleOps[id] === op` unchanged; absence ownership now also protects the *lifetime* that a newer operation uses | `a_retired_delete_cannot_touch_a_relisted_lifetime_*`; `a_relisted_conversation_gets_a_fresh_lifetime_and_an_old_404_cannot_touch_it` |
| I3 — success needs identity + strict contract | unchanged | strict-contract scenarios (unchanged) |
| I4 — unknown outcomes cannot release writes | unchanged guards | the outcome matrix and guard scenarios (unchanged) |
| I5 — confirmation updates title fields only | unchanged | `a_title_confirmation_changes_only_the_three_title_fields` (unchanged) |
| I6 — membership/order/activity belong to their authorities | `applyConversationAbsence` only removes, and now only when owned | all absence scenarios |
| I7 — absence is idempotent and does not invalidate newer reads | marker rule **plus** ownership; stale lifetime cannot advance the generation or start a read | retired-DELETE, detached-history and temporary-view scenarios |
| I8 — older responses cannot overwrite newer confirmed state | thread/turn/lifetime identity at the absence boundary; existing per-conversation serialization unchanged | detached-history scenarios; R5-C1 re-listing scenario |
| I9 — no success before validation | unchanged | strict-contract scenarios |
| I10 — session transitions belong to the lifecycle | unchanged | AD-C2 scenarios |

## 9. Exact test results

Local Windows 11, Python 3.12.10, Node.js 24.14.0. Focused affected-area verification; no
complete repository regression run was performed (not required at this stage). No test was
skipped; no existing assertion was weakened or removed.

| Command / scope | Result |
|---|---|
| `node tests/js/run_scenarios.js --tz=UTC` | **434 scenarios, 0 failed** (428 preserved + 6 new) |
| `tests/test_web_ui_logic.py` | **456 passed** (450 preserved + 6 new) |
| `tests/test_web_ui_dom.py` (real `app.js` against the server shell) | **197 passed** (196 preserved + 1 new) |
| `tests/test_web_ui.py` + `tests/test_web_ui_assets.py` + `tests/test_web_ui_integration.py` | **204 passed** |
| `tests/test_web_chat_api.py` (SQLite; includes the real-route PATCH-shape crosscheck) | **197 passed** |
| `tests/test_web_app.py` + `tests/test_admin_retention.py` | **93 passed** |

New deterministic scenarios (6 logic + 1 mini-DOM):

1. `a_retired_delete_cannot_touch_a_relisted_lifetime_204`
2. `a_retired_delete_cannot_touch_a_relisted_lifetime_404`
3. `a_detached_latest_history_absence_cannot_touch_a_relisted_conversation`
4. `a_detached_older_history_absence_cannot_touch_a_relisted_conversation`
5. `an_applicable_older_absence_still_removes_an_off_page_conversation`
6. `a_retired_title_acknowledgement_does_not_close_a_temporary_view`
7. `a_delayed_duplicate_delete_does_not_take_focus_from_another_conversation`

All R1–R5-C1 scenarios and their assertion intent are preserved.

## 10. Changed-file inventory and scope compliance

Production (authorized boundary only):

- `house_helper/web/static/chat-logic.js` — `absenceIsOwned`; the ownership gate in
  `applyConversationAbsence`; `conversationGone(id, evidence)` and its three read/turn
  callers; the title `absent` branch (lifetime + `operative` closure); the DELETE
  `lifetime`/`interaction` result and its stale path.
- `house_helper/web/static/app.js` — only the `deleteConversationControls` `.then`: focus
  is applied only when `result.interaction` is true. No other UI or controller change.

Tests (authorized):

- `tests/js/logic_scenarios.js` — the six R5-C2 logic scenarios.
- `tests/js/dom_scenarios.js` — the R5-C2 mini-DOM focus scenario.

Documentation:

- `docs/product/PP-01-A1_R5-C2_Correction_Record.md` (this record)
- `docs/architecture/PP-01-A1-AD-01_Erratum_01.md` (soft-timeout terminology)
- `docs/product/PP-01-A1_R5_Implementation_Record.md` (R5-C2 addendum; §2.2 wording)
- `docs/product/PP-01-A1_R5-C1_Correction_Record.md` (documentary count correction, §11)
- `docs/product/PP-01-A1_Implementation_Record.md` (§16.2)
- `docs/STATUS.md` (R5-C2 entry)

No other production or test file was changed. Option B, frozen D1–D7, AD-C1, AD-C2,
A1-02…A1-08, C3, C4, backend/API/schema, retention/account-isolation, Web/Telegram
compatibility and HH-BRAND-01-FIX-01 are unchanged.

## 11. Documentation errata

- **Soft-timeout terminology (Architecture Lock erratum).** The R5 record's §2.2 sentence
  "Any unrecognized 4xx, transport failure, soft timeout and non-conforming 200/201 stay
  indeterminate" conflates two different states. After the non-aborting soft timeout the
  request is still **intermediate pending/slow**: it may still settle definitively. Only a
  settled request whose answer cannot confirm the write is **terminal indeterminate**. The
  runtime behaviour is unchanged. This is recorded in
  [`PP-01-A1-AD-01_Erratum_01.md`](../architecture/PP-01-A1-AD-01_Erratum_01.md); the frozen
  lock text is not rewritten.
- **Invalid-summary case count.** The R5-C1 record said the strict-contract table gained
  "six calendar cases". The actual evidence is **five** C4-labelled cases in
  `INVALID_RENAME_ANSWERS` (impossible created/updated/last-message day, year zero, offset
  that rolls over) **plus** direct-parser coverage in
  `strict_mutation_timestamps_validate_the_calendar_before_any_conversion` (three
  impossible days, year zero, clock `24`/`:60`/`:60s`, offset `+24:00`/`+03:60`) and a
  valid leap-day/early-year/fractional-seconds acceptance scenario. The count is corrected
  in the R5-C1 record with a dated note; C3 and C4 remain independently closed and are not
  reopened.

## 12. Remaining limitations and evidence gaps

- No durable ordering across genuine document reloads; a delivered request can still commit
  afterwards.
- No cross-tab synchronization or ordering, and no server-side operation journal,
  distributed sequencing or locking.
- Recovery after a genuinely indeterminate write still requires a reload; there is no
  automatic retry.
- A definitive session end or a different account discards this page's in-memory guard,
  draft and queue (established lifecycle).
- Browser back/forward-cache eligibility and eviction remain a manual smoke-test item.
- An owned duplicate DELETE still announces the truthful deletion (deliberate R5-C1
  choice); a stale DELETE now announces nothing.
- Ownership is memory identity only. A reload discards every lifetime, thread and turn, so
  the rule does not extend across reloads (L1).
- The new scenarios exercise the scripted client harness and mini-DOM; no real browser or
  real backend was used. The real-route PATCH-shape crosscheck still runs unchanged on
  SQLite.
- PostgreSQL was not used: the change is client-side and no backend/persistence contract
  changed.

## 13. Compliance and scope preservation

- Option B preserved; frozen D1–D7 Scope Lock not edited; the Architecture Lock is amended
  only by an explicit erratum, never silently.
- A1-02…A1-08 not reopened; C3 and C4 remain closed; **A1-01 remains OPEN** until
  independently verified.
- FIX-01 behavioural baseline **24 PASS / 6 FAIL** unchanged; KHD-50 release block in force;
  PP-01-A1 is not Owner-accepted.
- No backend, API, schema, migration, prompt, provider, dependency, configuration, retention,
  account-isolation or Telegram change.
- No Production access, deployment, provider call, research execution or Git mutation
  (no commit, push, stash, reset, checkout or stage).

**Verdict: R5-C2 IMPLEMENTED — READY FOR INDEPENDENT FUNCTIONAL ACCEPTANCE**
