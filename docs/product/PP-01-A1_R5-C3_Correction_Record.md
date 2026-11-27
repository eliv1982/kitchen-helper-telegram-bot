# House Helper — PP-01-A1-R5-C3 Regression-First Ownership Closure
## Implementation Record

| Field | Value |
|---|---|
| Date | 2026-11-27 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` (full PP-01-A1 plus R1–R5-C2 uncommitted in the working tree) |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, frozen, unchanged) |
| Architecture authority | [`PP-01-A1-AD-01_Architecture_Lock_v1.0.md`](../architecture/PP-01-A1-AD-01_Architecture_Lock_v1.0.md) (Option B) and its [Erratum 01](../architecture/PP-01-A1-AD-01_Erratum_01.md) |
| Input | Independent R5-C2 closure report: three remaining confirmed ownership/interaction defects (temporary-history lifetime, pending-turn reconciliation identity, post-delete keyboard interaction) |
| Owner authorization | Bounded functional correction inside Option B; production boundary `chat-logic.js`, plus `app.js` only for the approved post-delete interaction behavior |
| Status | **R5-C3 IMPLEMENTED — RED/GREEN VERIFIED — READY FOR INDEPENDENT CLOSURE.** Working tree only; not committed, independently closed, released or deployed. |
| Preserved baseline | A1-02…A1-08 independently CLOSED; **A1-01 OPEN**; C3 and C4 independently CLOSED; FIX-01 **24 PASS / 6 FAIL**; KHD-50 release block |

## 1. Executive conclusion

The three remaining defects all came from a completion that identified its owner by
*conversation id* after an asynchronous boundary, rather than by the record it was issued
against. R5-C3 closes each at the smallest explicit ownership point:

- **Temporary history (A).** A history read issued for a temporary view now also captures
  the AD-C1 absence generation that made its view temporary. If an authoritative re-listing
  revokes that marker, the read speaks about a lifetime the page no longer holds and is
  stale — even though its thread object is still attached. This is the reviewer's
  "bind temporary history to the originating absence generation" option, chosen over
  detaching the thread because it adds no refresh path and no new state.
- **Pending-turn reconciliation (B).** `reconcile()` keeps the turn record it captured and
  clears `checking` only while `state.pending[id]` is still that very record.
- **Post-delete keyboard (C).** `app.js` adds one explicit interaction counter. The
  post-delete focus restoration captures it when the DELETE is clicked and applies only
  while no newer navigation interaction has taken over. The controller's `interaction`
  result (delete-question ownership) and `wasCurrent` (this call closed the open
  conversation) keep their meanings.

No product or architecture decision was added. The removal and its required list refill
remain consequences of the authoritative deletion and are independent of keyboard
ownership. Production changes are confined to `house_helper/web/static/chat-logic.js` and
the authorized post-delete interaction block in `house_helper/web/static/app.js`; test
changes to the two authorized JS scenario files.

## 2. Baseline and snapshot evidence (Phase A)

Before any edit, at `def03ea` with the complete PP-01-A1/R1–R5-C2 working tree:

| Check | Result |
|---|---|
| `git rev-parse HEAD` | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` |
| `node tests/js/run_scenarios.js --tz=UTC` | **434 scenarios, 0 failed** |
| `tests/test_web_ui_logic.py` + `tests/test_web_ui_dom.py` | **653 passed** (456 + 197) |

This matches the R5-C2 record. No test was skipped or weakened.

**Pre-correction snapshot.** A filesystem copy of the four touched files was taken (no Git
mutation; the established local-snapshot procedure, used because this repository must not
be stashed and no file is committed):

`C:\Users\eliv\AppData\Local\Temp\hh-r5c3-snapshot-20261127-093333\`

| File | SHA-256 (pre-correction) |
|---|---|
| `chat-logic.js` | `426f7c6dcd3e4c79d4cc73c4b558e9ff34718c1c94de650ebf06e19c3d6489d3` |
| `app.js` | `19f8b13a8c2b1ca2be3a2ea3ecf76aa72ac8564ff720f9f048893971f7d04a3a` |
| `logic_scenarios.js` | `35536faecb4a021d57dca99013564ab110b9ff6cc017dbced04b87f51710d03f` |
| `dom_scenarios.js` | `a1a561f609c5f0dd716feaf2b23e7fcf18bcb32ed554b6f76bbaafd8e368a9c1` |

Post-correction SHA-256 (same four files):

| File | SHA-256 (post-correction) |
|---|---|
| `chat-logic.js` | `f009f341b1ad593de36bfe04b869f808f635f5655474820869604031969df0cf` |
| `app.js` | `b01bc881f5fd604e82bd159e49ca5e2d7f41620d5e71df4a4731931133403d64` |
| `logic_scenarios.js` | `e2fdf5e3c991f591e99e638cc98fb99be3ed5f561845755c01adb90cc9523e54` |
| `dom_scenarios.js` | `d334e0aaf7d5cc13e7bdde02e14cc17d390ff4920007679d3cc1218f8fc53e13` |

## 3. Assessment of the independent reviewer's recommendations

The reviewer's recommendations were advisory and were implemented as ownership evidence at
the existing absence boundary, not as caller-specific patches.

### 3.1 Defect A — two options evaluated

- **Bind temporary history to the originating absence generation. ADOPTED.** The read
  captures `goneMarks[id]` when it is issued. `absenceIsOwned` requires the same generation
  to still be live for a read that had one. This is the smallest coherent change: one
  comparison, no new state, and it generalises the existing "was issued against state the
  page still holds" rule to the temporary lifetime.
- **Detach the temporary thread when the re-listing revokes its generation, with a refresh.
  REJECTED.** It requires the list path to reach into the thread/lifecycle owner, clear an
  in-flight `thread.latest`, and decide whether to re-read a conversation the person may
  already have left. That is a new cross-authority coupling (I6) and more state than the
  problem needs.

The generation is captured only in `fetchLatest` and `loadOlder` — the two history
completion paths that can reach an absence. An ordinary read of a live conversation has no
generation (`undefined`) and keeps its thread-identity rule, because an authoritative
removal always detaches that thread. The turn path keeps its own record identity; a
temporary view cannot start a turn (a successful history read, which would set
`thread.synced`, is what sending requires), so no generation is needed there.

### 3.2 Defect B — captured identity

The reviewer's "preserve the captured pending-turn identity across the asynchronous
boundary" is implemented literally. The old code re-read `state.pending[id]`; the new code
requires `state.pending[id] === turn` before clearing `checking`. Normal reconciliation and
the pending-turn feedback are unchanged.

### 3.3 Defect C — explicit interaction ownership

The reviewer's direction was followed: conversation-lifetime applicability does not
establish keyboard-interaction ownership, and the fix must not rely solely on the selected
conversation id or the retained delete-question identity. `app.js` now has one explicit
counter (`interactionSeq`) and one helper (`noteInteraction()`) incremented only by the
person's navigation gestures — opening/closing the drawer, choosing a section, a
conversation, a new draft, a saved list or a recipe. The post-delete completion captures the
counter when the confirm button is clicked and returns before touching focus or the drawer
if a newer interaction has occurred. `result.interaction` still gates first, so the
previously verified duplicate and obsolete completion behavior is intact.

No general focus-management framework was introduced: the counter is read at exactly one
completion site, the post-delete restoration.

## 4. RED evidence (Phase B)

The three regressions were added and executed against the unmodified R5-C2 implementation.
Each failed on the requirement, not on a fixture or harness error.

| Defect | Scenario | Observed RED failure |
|---|---|---|
| A | `an_obsolete_temporary_history_absence_cannot_touch_a_relisted_conversation` | `AssertionError: it does not close the re-listed view` — `actual: null`, `expected: CONV_A`: the held temporary `404` closed the view and removed the re-listed conversation. |
| B | `a_reconciliation_does_not_clear_a_replacement_turns_checking_state` | `AssertionError: the original reconciliation does not clear the replacement's check` — `false !== true`: the old reconciliation cleared the replacement turn's `checking`. |
| C (drawer) | `a_delayed_delete_does_not_close_a_reopened_drawer_or_take_its_focus` | `AssertionError: the reopened drawer stays open` — `'closed' !== 'open'`: the delayed DELETE closed the drawer the person had reopened. |
| C (section) | `a_delayed_delete_does_not_interrupt_a_newer_section_choice` | `AssertionError: its focus is undisturbed` — the delayed DELETE moved focus back into the conversation after the person had chosen the favorites section. |

Full RED state: **436 Node scenarios, 2 failed** (434 preserved passed); **199 DOM tests,
2 failed** (197 preserved passed). No pre-existing scenario failed.

The two DOM scenarios compare `document.activeElement` by object identity with
`assert.ok(...)`; comparing the circular mini-DOM node with `assert.equal` makes Node's
mismatch formatter allocate unboundedly. This is a reporting detail of the harness, not a
behavior under test; the assertions themselves are unchanged in intent.

## 5. Implementation and enforcement points (Phase C)

### 5.1 Defect A — temporary-history lifetime

`house_helper/web/static/chat-logic.js`:

- `fetchLatest(id)`: captures `var absenceGeneration = goneMarks[id]` when the read is
  issued; passes it to `conversationGone`.
- `loadOlder()`: same capture and pass (after the guard that establishes a non-null `id`).
- `conversationGone(id, evidence)`: copies `evidence.absenceGeneration` into the notice.
- `absenceIsOwned(id, notice)`: for a thread-bearing notice, a defined
  `absenceGeneration` must still equal `goneMarks[id]` before the thread identity is
  checked. A revoked marker therefore makes the read stale before any read or write.

Enforcement point: the first statement of `applyConversationAbsence`, which remains the only
path that applies an authoritative absence. A stale read returns
`{ first: false, refill: false, applicable: false }` before any state change, announcement
or emit.

### 5.2 Defect B — reconciliation identity

`reconcile(id)` keeps its captured `turn` and after `await loadLatest(id)` applies
`if (state.pending[id] === turn)`. A replacement record is never cleared by an older
reconciliation.

### 5.3 Defect C — post-delete interaction

`house_helper/web/static/app.js`:

- `interactionSeq` / `noteInteraction()` near the existing focus state.
- The `deleteConversationControls` confirm handler captures `interactionSeq` before
  calling `controller.deleteConversation()` and, after the existing `result.interaction`
  gate, returns if the counter has changed.
- `noteInteraction()` is called by explicit navigation: the drawer toggle, backdrop click,
  drawer Escape, `selectConversation`, `newConversation`, the three section tabs,
  `profileOpen`, `openShoppingList`, `openFavorite`, and the narrow «Выбрать рецепт»
  drawer opener. Programmatic redraws do not call it.

The authoritative deletion, `applyConversationAbsence`, the list generation and the one
refill are untouched: removal and refill happen whether or not the completion still owns the
keyboard.

## 6. Structural verification (§7 of the brief)

| Completion | Identity captured before the await | Check after the boundary | Permitted effect |
|---|---|---|---|
| Latest history absence | thread object + absence generation at issue | `absenceIsOwned`: generation still live, then `state.threads[id] === thread` | reconcile an absence only if owned; otherwise nothing |
| Older history absence | thread object + absence generation at issue | same | same |
| Pending-turn reconciliation | the turn record itself | `state.pending[id] === turn` | clear only that turn's `checking` |
| Title write / DELETE absence | conversation lifetime | `absenceIsOwned` lifetime rule (R5-C2) | idempotent acknowledgement of its own removal |
| Post-delete focus restoration | `interactionSeq` at click + controller `interaction` | `result.interaction` then `interactionSeq === captured` | move focus / close the drawer only if both hold |

The four identities stay distinct: **session** (`epoch`), **conversation lifetime**
(`titleQueues` record), **pending operation** (turn record / operation object) and **user
interaction** (`interactionSeq`). I2, I7 and I8 are preserved: R5-C3 only narrows when a
completion may speak; it adds no new write to shared state and does not change title
operation clearing, absence idempotence or per-conversation ordering.

## 7. GREEN evidence (Phase D)

The same scenarios, with no assertion weakened, after the correction:

| Defect | Scenario | GREEN |
|---|---|---|
| A | `an_obsolete_temporary_history_absence_cannot_touch_a_relisted_conversation` | pass — the re-listed view, confirmed title, fresh operation, draft and no-guard state survive; the pending list read applies when released; the held write then confirms normally |
| B | `a_reconciliation_does_not_clear_a_replacement_turns_checking_state` | pass — the replacement's `checking` stays visible until its own retry settles it |
| C (drawer) | `a_delayed_delete_does_not_close_a_reopened_drawer_or_take_its_focus` | pass — removal and refill occur; the drawer stays open and keeps its focus |
| C (section) | `a_delayed_delete_does_not_interrupt_a_newer_section_choice` | pass — the favorites section and its focus are undisturbed while the deletion still refills |

## 8. Exact focused regression-suite results

Local Windows 11, Python 3.12.10, Node.js 24.14.0. Focused affected-area verification (no
complete repository regression run and no PostgreSQL execution; the change is client-side
and no backend/persistence contract changed). No test was skipped; no existing assertion
was weakened or removed.

| Command / scope | Result |
|---|---|
| `node tests/js/run_scenarios.js --tz=UTC` | **436 scenarios, 0 failed** (434 preserved + 2 new) |
| `tests/test_web_ui_logic.py` | **458 passed** (456 preserved + 2 new) |
| `tests/test_web_ui_dom.py` (real `app.js` against the server shell) | **199 passed** (197 preserved + 2 new) |
| `tests/test_web_ui.py` + `test_web_ui_assets.py` + `test_web_ui_integration.py` | **204 passed** |
| `tests/test_web_chat_api.py` (SQLite; includes the real-route PATCH-shape crosscheck) | **197 passed** |
| `tests/test_web_app.py` + `test_admin_retention.py` | **93 passed** |

New deterministic scenarios (2 logic + 2 mini-DOM):

1. `an_obsolete_temporary_history_absence_cannot_touch_a_relisted_conversation`
2. `a_reconciliation_does_not_clear_a_replacement_turns_checking_state`
3. `a_delayed_delete_does_not_close_a_reopened_drawer_or_take_its_focus`
4. `a_delayed_delete_does_not_interrupt_a_newer_section_choice`

All R1–R5-C2 scenarios and their assertion intent are preserved.

## 9. Changed-file inventory and scope compliance

Production (authorized boundary only):

- `house_helper/web/static/chat-logic.js` — the absence-generation capture in
  `fetchLatest` and `loadOlder`; its passage through `conversationGone`; the generation
  check in `absenceIsOwned`; the identity check in `reconcile`.
- `house_helper/web/static/app.js` — `interactionSeq` / `noteInteraction()`; the
  `deleteConversationControls` capture and check; the `noteInteraction()` calls at the
  explicit navigation handlers listed in §5.3. No other UI or controller behavior changed.

Tests (authorized):

- `tests/js/logic_scenarios.js` — the two R5-C3 logic scenarios.
- `tests/js/dom_scenarios.js` — the two R5-C3 mini-DOM scenarios.

Documentation:

- `docs/product/PP-01-A1_R5-C3_Correction_Record.md` (this record)
- `docs/product/PP-01-A1_R5_Implementation_Record.md` (§15 addendum)
- `docs/product/PP-01-A1_Implementation_Record.md` (§16.3)
- `docs/STATUS.md` (R5-C3 entry)

No other production or test file was changed. Option B, frozen D1–D7, AD-C1, AD-C2,
A1-02…A1-08, C3, C4, backend/API/schema, retention/account-isolation, Web/Telegram
compatibility and HH-BRAND-01-FIX-01 are unchanged.

## 10. Remaining limitations and evidence gaps

- Ownership is memory identity plus a page-lifetime counter. A reload discards every
  lifetime, thread, turn and interaction generation, so the rules do not extend across
  reloads, tabs or distributed execution (L1–L3).
- A section chosen or drawer reopened programmatically (not by one of the instrumented
  user gestures) does not advance the interaction counter. All current user-facing
  navigation goes through the instrumented handlers; a future UI must use them.
- The new scenarios exercise the scripted client harness and mini-DOM; no real browser or
  real backend was used. The real-route PATCH-shape crosscheck still runs unchanged on
  SQLite.
- PostgreSQL was not used: the change is client-side and no backend/persistence contract
  changed.
- A1-01 remains OPEN until independently verified.

## 11. Compliance and scope preservation

- Option B preserved; the frozen D1–D7 Scope Lock is not edited; the Architecture Lock and
  its erratum are unchanged.
- A1-02…A1-08 not reopened; C3 and C4 remain independently closed.
- FIX-01 behavioural baseline **24 PASS / 6 FAIL** unchanged; KHD-50 release block in force;
  PP-01-A1 is not Owner-accepted.
- No backend, API, schema, migration, prompt, provider, dependency, configuration, retention,
  account-isolation or Telegram change.
- **No Git mutation** (no commit, push, stash, reset, checkout or stage), **no Production
  access or deployment**, and **no provider call**.

**Verdict: R5-C3 IMPLEMENTED — RED/GREEN VERIFIED — READY FOR INDEPENDENT CLOSURE**
