# House Helper — PP-01-A1-R5-C4 Functional Recovery and Interaction Closure
## Implementation Record

| Field | Value |
|---|---|
| Date | 2026-11-27 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` (full PP-01-A1 plus R1–R5-C3 uncommitted in the working tree) |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, frozen, unchanged) |
| Architecture authority | [`PP-01-A1-AD-01_Architecture_Lock_v1.0.md`](../architecture/PP-01-A1-AD-01_Architecture_Lock_v1.0.md) (Option B) and its [Erratum 01](../architecture/PP-01-A1-AD-01_Erratum_01.md) |
| Input | Independent R5-C3 closure report: remaining functional recovery (A), list-focus preservation (C1) and shopping-draft interaction ownership (C2) requirements |
| Owner authorization | Bounded Regression-First correction inside Option B; production boundary `chat-logic.js` (history recovery only) and `app.js` (list focus and shopping-draft interaction only) |
| Status | **R5-C4 IMPLEMENTED — RED/GREEN VERIFIED — READY FOR INDEPENDENT CLOSURE.** Working tree only; not committed, independently closed, released or deployed. |
| Preserved baseline | A1-02…A1-08 independently CLOSED; **A1-01 OPEN**; AD-C1, AD-C2, I2/I7/I8 conformance, C3 and C4 independently CLOSED; FIX-01 **24 PASS / 6 FAIL**; KHD-50 release block |

## 1. Executive conclusion

R5-C3 stopped stale completions from acting on state they were not issued against. It did
not give the person a way out of the state a rejected temporary-history answer leaves behind,
it did not preserve a focused list row across the list rebuild a deletion causes, and it did
not treat entering and leaving a shopping draft as interactions newer than a DELETE still out.
R5-C4 closes exactly those three gaps:

- **A — history recovery.** A stale temporary-history settlement applies nothing and starts
  no read. When its thread is still the selected, attached, unsynchronized history, the page
  now starts **one** fresh applicable read, associated with that same thread. Success displays
  the current history and unlocks the composer; failure takes the established
  `thread.status === "error"` state with its retry; a second stale answer does not ask again.
  An already-running applicable read is joined, never duplicated.
- **C1 — list focus.** `drawList` captures the stable conversation identity of the focused row
  before it rebuilds the list and, if that conversation survives the update, focuses its new
  button. A deleted row is never focused, focus outside the list is never pulled in, and the
  drawer and selection are not touched.
- **C2 — shopping-draft interaction.** The existing `interactionSeq` / `noteInteraction()`
  mechanism is advanced by the chat's direct shopping-draft action and by the draft's
  cancellation. A DELETE that is still out keeps its removal and refill but no longer owns the
  keyboard after those interactions.

No product or architecture decision was added. The AD-C1 absence boundary, AD-C2, the
processing boundary, the closed outcome model and the ten invariants are unchanged. Production
changes are confined to the two authorized static files; test changes to the two authorized JS
scenario files (plus one required allowlist entry in the existing attribute-safety test, §9).

## 2. Baseline and snapshot evidence (Phase A)

Before any edit, at `def03ea` with the complete PP-01-A1/R1–R5-C3 working tree:

| Check | Result |
|---|---|
| `git rev-parse HEAD` | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` |
| `node tests/js/run_scenarios.js --tz=UTC` | **436 scenarios, 0 failed** |
| `tests/test_web_ui_logic.py` | **458 passed** |
| `tests/test_web_ui_dom.py` | **199 passed** |

This matches the R5-C3 record. No test was skipped or weakened.

**Pre-correction snapshot.** A filesystem copy of the four touched files was taken before any
edit (no Git mutation; the established local-snapshot procedure, used because this repository
must not be stashed and no file is committed):

`C:\Users\eliv\AppData\Local\Temp\pp01-a1-r5c4-snapshot\` (`SHA256SUMS.txt` retained there)

| File | SHA-256 (pre-correction) |
|---|---|
| `chat-logic.js` | `f009f341b1ad593de36bfe04b869f808f635f5655474820869604031969df0cf` |
| `app.js` | `b01bc881f5fd604e82bd159e49ca5e2d7f41620d5e71df4a4731931133403d64` |
| `logic_scenarios.js` | `e2fdf5e3c991f591e99e638cc98fb99be3ed5f561845755c01adb90cc9523e54` |
| `dom_scenarios.js` | `d334e0aaf7d5cc13e7bdde02e14cc17d390ff4920007679d3cc1218f8fc53e13` |

Post-correction SHA-256 (same four files):

| File | SHA-256 (post-correction) |
|---|---|
| `chat-logic.js` | `795f545617361fd3ba7fe7afd1cb8640811ee1bd2e2aa60b258d43350db031d0` |
| `app.js` | `c96ecd54f83fa00af76b694d3cc71a9197cb35d2ab5b82b57517941d2de828c9` |
| `logic_scenarios.js` | `a489739dde95ea9529c352339d5d517e7dd4b5a884c3a0f5fa6a5ad1e0817608` |
| `dom_scenarios.js` | `b4472e476128d80fa1335944e7ea0d73b4012b801df8cefa126ccda3b108f0ad` |

## 3. Assessment of the independent reviewer's recommendations

### 3.1 Defect A — one fresh applicable read

- **Start one fresh applicable history read, associated with the current conversation and
  request ownership. ADOPTED**, with three bounded properties:
  1. It is asked for by the *settlement* of the stale read, not by a timer or a list event.
  2. It is permitted only while the same thread is still selected, attached to
     `state.threads[id]`, unsynchronized, and the page is not held back. If the person has
     left, the flag is dropped and the ordinary `selectConversation` read covers their return;
     if the page is held back, `letThrough` already re-reads an open unsynchronized thread.
  3. The recovery read is marked `recovering`, so a second stale answer cannot ask for a third
     read. It takes the established error state instead (a bounded, non-looping fallback).
- **Retry from a timer, or repeated automatic retries. REJECTED.** That would be a loop and
  would contradict the brief.
- **Join an already-running read rather than issuing another. ADOPTED.** Recovery goes through
  `loadLatest`, which returns `thread.latest` when a read is already out. The no-duplicate
  scenario holds the recovery read, asks for the history again, and observes no second request.

The existing R5-C3 assertion that the obsolete answer starts no **list** read is preserved and
still passes; recovery is a history (`GET .../messages`) read only.

### 3.2 Defect C1 — smallest coherent mechanism

- **Reuse stable row elements. REJECTED for now.** `drawList` is a full clear-and-rebuild.
  Reuse would require a keyed diff that moves existing `<li>`/`<button>` nodes, a larger change
  to the established rendering architecture than the problem requires.
- **Restore focus to the surviving control by stable conversation identity. ADOPTED.** The
  button carries `data-conversation`. Before clearing, `drawList` captures that identity only
  when the focused element is inside the list (`within(document.activeElement, els.list)`);
  after the rebuild it focuses the new button for the same id, and nothing else. Because the
  capture and restoration are synchronous within one redraw, no interaction can interleave.
  A removed row is not restored; focus outside the list is not moved; the drawer and selection
  are not changed.

### 3.3 Defect C2 — existing interaction-generation mechanism

- **Use the established `interactionSeq` / `noteInteraction()`. ADOPTED.** `app.js` advances it
  in the chat recipe's «Что купить?» handler (after the unsaved-draft question is accepted,
  exactly like `openShoppingList`) and in `cancelDraft()` when the cancellation succeeds.
  Nothing else is instrumented; the distinction between conversation removal applicability,
  `wasCurrent`, the controller's `interaction` result and current UI interaction authority is
  preserved. No general navigation framework was introduced.

## 4. RED evidence (Phase B)

The regressions were added and executed against the unmodified R5-C3 implementation. Each
failed on the requirement, not on a fixture or harness error.

| Defect | Scenario | Observed RED failure |
|---|---|---|
| A (success) | `an_obsolete_temporary_history_absence_cannot_touch_a_relisted_conversation` (extended) | `exactly one fresh applicable history read follows the obsolete settlement` — `3 !== 4`: no recovery read was issued; the thread stayed in `loading`. |
| A (failure) | `a_failed_history_recovery_exposes_the_established_retry` | `the failed recovery takes the history-error state` — `'loading' !== 'error'`: no recovery read was issued, so no failure state was reached. |
| A (no duplicate) | `a_history_recovery_does_not_duplicate_an_already_running_read` | `the stale settlement asks for exactly one fresh read` — `3 !== 4`: no recovery read was issued. |
| C1 | `a_surviving_conversation_row_keeps_the_keyboard_across_a_delete_and_refill` | `the surviving row keeps the keyboard after the removal` — the rebuild detached the focused B row and left focus on it. |
| C2 | `leaving_a_shopping_draft_is_a_newer_interaction_than_a_delayed_delete` | `the composer keeps the keyboard` — the delayed DELETE completion moved focus to the chat title. |

Full RED state: **438 Node scenarios, 3 failed** (434 preserved + the extended scenario passed
before the new assertions; 2 new scenarios failed); **201 DOM tests, 2 failed** (199 preserved
passed). No pre-existing scenario failed. The pre-existing R5-C3 case where a temporary view
closes on its own applicable absence answer was preserved and passed throughout.

## 5. Implementation and enforcement points (Phase C)

### 5.1 Defect A — `house_helper/web/static/chat-logic.js`

- `applyConversationAbsence` now returns `applicable: true` on the owned path (the not-owned
  path already returned `applicable: false`).
- `conversationGone` returns that result to a caller that needs to distinguish a stale answer.
- `fetchLatest(id, recovering)` captures `absenceGeneration = goneMarks[id]` when issued. On
  `404`, if the outcome is not applicable:
  - ordinary read → `thread.needsRecovery = true`;
  - recovery read → `thread.error = { text: TEXT.historyUnavailable, correlationId: null }`,
    `thread.status = "error"` when no messages are held, then `emit()`. No further read.
- `loadLatest(id, recovering)` clears `thread.latest`, then, while
  `thread.needsRecovery && !state.gate && state.currentId === id &&
  state.threads[id] === thread && !thread.synced`, starts exactly one
  `loadLatest(id, true)`. Otherwise the flag is dropped.
- Because recovery goes through `loadLatest`, an already-running `thread.latest` is returned
  instead of a second request.

Enforcement point: the `not_found` branch of `fetchLatest` for the stale decision, and the
settlement callback of `loadLatest` for the one recovery start. `applyConversationAbsence`
remains the only path that applies an authoritative absence, and it is untouched for owned
answers.

### 5.2 Defect C1 — `house_helper/web/static/app.js`, `drawList`

- Each conversation button carries `data-conversation="<id>"`.
- Before `clear(els.list)`, capture `document.activeElement`'s `data-conversation` only when
  the active element is inside `els.list`.
- While building, keep the new button per id; after the rebuild, focus the button whose id was
  captured. If the id is absent (row removed) or focus was elsewhere, do nothing.

### 5.3 Defect C2 — `house_helper/web/static/app.js`

- The chat recipe action («Что купить?») calls `noteInteraction()` after the unsaved-draft
  question is accepted and before the controller transition.
- `cancelDraft()` calls `noteInteraction()` after a successful cancellation.

The controller's DELETE classification (`ownedQuestion`, `wasCurrent`) is unchanged, as are
the removal and its one refill.

## 6. Structural postconditions (§7 of the brief)

| Completion | Origin identity | Permitted when | Allowed change | If ownership changed |
|---|---|---|---|---|
| Stale temporary-history settlement (A) | the read's thread object + the absence generation it was issued against | outcome `applicable === false`; same thread still selected and attached; unsynchronized; page not held back | start exactly one `loadLatest(id, true)` | flag dropped; no read, no state change |
| Hist. recovery read (A) | the one recovery mark | issued only by a stale settlement | ordinary history read; success → `ready`/`synced`/composer; failure → established `error` state | a second stale answer → `error` (retry), never another automatic read |
| List focus (C1) | the focused row's stable conversation id, captured before the rebuild | active element inside the list; the id still exists after the rebuild | focus the rebuilt button for that id | id absent → nothing; focus outside list → nothing |
| Shopping-draft entry/cancel (C2) | a newer `interactionSeq` value | the user's accepted entry / successful cancellation | a delayed DELETE completion returns before moving focus or closing the drawer | the removal and refill still happen |

Duplicate work is prevented by `thread.latest` for reads and by the single keyed focus
restoration per `drawList` call. Indefinite loading is prevented by the one recovery read plus
its terminal success/error states. History recovery is distinct from authoritative removal:
only an applicable absence removes or closes. Keyboard preservation is distinct from DELETE
completion authority: `interaction` and `wasCurrent` still decide whether a removal closed the
view; `interactionSeq` decides whether that completion may move the keyboard.

## 7. GREEN evidence (Phase D)

The same scenarios, with no assertion weakened, after the correction:

| Defect | Scenario | GREEN |
|---|---|---|
| A | `an_obsolete_temporary_history_absence_cannot_touch_a_relisted_conversation` | pass — the re-listed view, confirmed title, fresh operation, draft and no-guard state survive; exactly one fresh history read follows; the history is `ready`, displayed, and the composer is unlocked; the newer list read and the held write still complete normally. |
| A | `a_failed_history_recovery_exposes_the_established_retry` | pass — the failed recovery takes `thread.status === "error"` with the `historyUnavailable` hint; the retry reads again and restores the composer. |
| A | `a_history_recovery_does_not_duplicate_an_already_running_read` | pass — one fresh read is issued; asking again joins it; the joined read supplies the history. |
| C1 | `a_surviving_conversation_row_keeps_the_keyboard_across_a_delete_and_refill` | pass — A is removed, exactly one refill occurs, B survives and keeps the keyboard after the removal and after the refill, and the drawer stays open. |
| C2 | `leaving_a_shopping_draft_is_a_newer_interaction_than_a_delayed_delete` | pass — the removal and exactly one refill happen; the composer keeps the keyboard and stays usable; the refill does not take it either. |

The pre-existing temporary-view closure case and all other R1–R5-C3 scenarios and assertion
intent are preserved.

## 8. Exact focused regression-suite results

Local Windows 11, Python 3.12.10, Node.js 24.14.0. Focused affected-area verification (no
complete repository regression run and no PostgreSQL execution; the change is client-side and
no backend/persistence contract changed). No test was skipped; no existing assertion was
weakened or removed.

| Command / scope | Result |
|---|---|
| `node tests/js/run_scenarios.js --tz=UTC` | **438 scenarios, 0 failed** (436 preserved + 2 new; 1 extended) |
| `tests/test_web_ui_logic.py` | **460 passed** (458 preserved + 2 new wrapper tests) |
| `tests/test_web_ui_dom.py` | **201 passed** (199 preserved + 2 new) |
| `tests/test_web_ui.py` + `test_web_ui_assets.py` + `test_web_ui_integration.py` | **204 passed** |
| `tests/test_web_chat_api.py` (SQLite; includes the real-route PATCH-shape crosscheck) | **197 passed** |
| `tests/test_web_app.py` + `test_admin_retention.py` | **93 passed** (34 + 59) |

New deterministic scenarios (2 logic + 2 mini-DOM) plus one extended logic scenario:

1. `an_obsolete_temporary_history_absence_cannot_touch_a_relisted_conversation` (extended:
   recovery, composer, fresh history)
2. `a_failed_history_recovery_exposes_the_established_retry`
3. `a_history_recovery_does_not_duplicate_an_already_running_read`
4. `a_surviving_conversation_row_keeps_the_keyboard_across_a_delete_and_refill`
5. `leaving_a_shopping_draft_is_a_newer_interaction_than_a_delayed_delete`

## 9. Changed-file inventory and scope compliance

Production (authorized boundary only):

- `house_helper/web/static/chat-logic.js` — the `recovering` parameter and one-recovery start
  in `loadLatest`; the stale-outcome handling in `fetchLatest`; the returned
  `applicable: true` from `applyConversationAbsence` and pass-through in `conversationGone`.
  No absence-ownership rule, absence generation, title queue, list generation, refill or
  pending-turn logic was changed.
- `house_helper/web/static/app.js` — the `data-conversation` identity and keyed focus
  preservation in `drawList`; `noteInteraction()` in the chat shopping-draft action and in
  `cancelDraft()`. No other UI or controller behavior changed.

Tests (authorized):

- `tests/js/logic_scenarios.js` — the extended scenario and two new logic scenarios.
- `tests/js/dom_scenarios.js` — two new mini-DOM scenarios, and the existing
  `the_script_never_sets_a_handler_a_style_a_link_or_an_address_on_anything` allowlist gains
  `data-conversation`. That test's security assertions (`on*`, `style`, `href`, `src`,
  `srcdoc`, `action`, `formaction`, `target`) are unchanged; the entry only permits the new,
  deliberate stable identity attribute.

Documentation:

- `docs/product/PP-01-A1_R5-C4_Correction_Record.md` (this record)
- `docs/product/PP-01-A1_R5_Implementation_Record.md` (§16 addendum)
- `docs/product/PP-01-A1_Implementation_Record.md` (§16.4)
- `docs/STATUS.md` (R5-C4 entry)

No other production or test file was changed. Option B, the frozen D1–D7 Scope Lock,
AD-C1/AD-C2, I2/I7/I8, A1-02…A1-08, C3/C4, backend/API/schema, retention/account-isolation,
Web/Telegram compatibility and HH-BRAND-01-FIX-01 are unchanged. There is no migration,
provider call, research execution or Production access.

## 10. Remaining limitations and evidence gaps

- Ownership and recovery are memory identity plus page-lifetime state (`thread`, absence
  generation, `interactionSeq`). A reload discards them; the rules do not extend across
  reloads, tabs or distributed execution.
- Recovery is deliberately single-shot per stale settlement. If a recovery read is stale
  again, the page shows the established history-error state rather than asking a third time;
  the person's retry is the next read.
- C1 preserves focus only when the focused element was inside the conversation list at the
  moment of a redraw. A programmatic focus outside the list is never pulled in.
- C2 advances the interaction counter only at the two named user gestures. A future UI must
  use `noteInteraction()` for new user navigation, as the current handlers do.
- The scenarios exercise the scripted controller harness and mini-DOM; no real browser was
  used, so this is not real-browser verification. The mini-DOM does not reset
  `document.activeElement` when a focused node is detached; the implementation captures the
  identity before clearing, so it behaves the same whether the browser resets focus to `body`
  or not, but that browser path is not directly exercised here.
- PostgreSQL was not used: the change is client-side and no backend/persistence contract
  changed.
- A1-01 remains OPEN until independently verified.

## 11. Compliance and scope preservation

- Option B preserved; the frozen D1–D7 Scope Lock is not edited; the Architecture Lock and
  its erratum are unchanged.
- A1-02…A1-08 not reopened; AD-C1/AD-C2, I2/I7/I8 and C3/C4 remain independently closed.
- FIX-01 behavioural baseline **24 PASS / 6 FAIL** unchanged; KHD-50 release block in force;
  PP-01-A1 is not Owner-accepted.
- No backend, API, schema, migration, prompt, provider, dependency, configuration, retention,
  account-isolation or Telegram change.
- **No Git mutation** (no commit, push, stash, reset, checkout or stage), **no Production
  access or deployment**, and **no provider call**.

**Verdict: R5-C4 IMPLEMENTED — RED/GREEN VERIFIED — READY FOR INDEPENDENT CLOSURE**
