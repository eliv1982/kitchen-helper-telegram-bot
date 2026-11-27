# House Helper — PP-01-A1-R5-C5 Desktop Focus Closure
## Correction Record

| Field | Value |
|---|---|
| Date | 2026-11-27 |
| Method | Regression First |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` (full PP-01-A1 plus R1–R5-C4 uncommitted in the working tree) |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, frozen, unchanged) |
| Architecture authority | [`PP-01-A1-AD-01_Architecture_Lock_v1.0.md`](../architecture/PP-01-A1-AD-01_Architecture_Lock_v1.0.md) (Option B) and its [Erratum 01](../architecture/PP-01-A1-AD-01_Erratum_01.md) |
| Input | Independent R5-C4 closure report: the only remaining confirmed defect is C1 desktop focus |
| Owner authorization | Bounded Regression-First correction inside Option B; production boundary `app.js` (the post-delete focus decision only) |
| Status | **R5-C5 IMPLEMENTED — RED/GREEN VERIFIED — READY FOR INDEPENDENT CLOSURE.** Working tree only; not committed, independently closed, released or deployed. |
| Preserved baseline | A1-02…A1-08 independently CLOSED; **A1-01 OPEN**; AD-C1, AD-C2 and the R5-C1…R5-C4 corrections independently CLOSED; FIX-01 **24 PASS / 6 FAIL**; KHD-50 release block |

## 1. Executive conclusion

The R5-C4 list-focus correction gives the keyboard back to the rebuilt row of a surviving
conversation after every list redraw. Its own scenario ran with the drawer open on a narrow
screen, where reopening the drawer is a newer interaction and the DELETE completion therefore
returns before moving focus. On a wide screen the conversation list is always beside the chat.
There, a person can put the keyboard on a surviving row without any interaction the
`interactionSeq` mechanism records, and the held DELETE still owns its question: when it
settles, the completion's ordinary restoration focused `#chat-title` and took the keyboard away
from the row the redraw had just restored.

R5-C5 closes exactly that gap. In the completion's `result.wasCurrent` path, if the page's
active element is a conversation row inside the list (it carries the stable
`data-conversation` identity), the completion leaves the keyboard and the drawer to that row and
returns. With no surviving row focused, the existing ordinary restoration — close the drawer
and focus the chat title, or focus the new-conversation control — is unchanged.

No new focus framework, interaction counter, controller change, production file or behavioural
semantic was added. The change is one guarded decision in `app.js` and one wide-screen mini-DOM
scenario.

## 2. Baseline and snapshot evidence

Before any edit, at `def03ea` with the complete PP-01-A1/R1–R5-C4 working tree:

| Check | Result |
|---|---|
| `git rev-parse HEAD` | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` |
| `node tests/js/run_scenarios.js --tz=UTC` | **438 scenarios, 0 failed** |
| `tests/test_web_ui_dom.py` | **201 passed** |

This matches the R5-C4 record. No test was skipped or weakened.

**Pre-correction snapshot.** A filesystem copy of the two authored files (and the three
documentation files to be amended) was taken before any edit, with no Git mutation — the
established local-snapshot procedure, because this repository must not be stashed and nothing
is committed:

`C:\Users\eliv\AppData\Local\Temp\pp01-a1-r5c5-snapshot\` (`SHA256SUMS.txt` retained there)

| File | SHA-256 (pre-correction) |
|---|---|
| `app.js` | `c96ecd54f83fa00af76b694d3cc71a9197cb35d2ab5b82b57517941d2de828c9` |
| `dom_scenarios.js` | `b4472e476128d80fa1335944e7ea0d73b4012b801df8cefa126ccda3b108f0ad` |
| `docs/STATUS.md` | `e4e6fabfd0f0daca46c98db3450d4980e36f8c132e416f551e7b5d72ad7f1355` |
| `PP-01-A1_R5_Implementation_Record.md` | `827cfa10ee09779036a669a9560bcae1d40c0797471f1144ed951e31959af0a9` |
| `PP-01-A1_Implementation_Record.md` | `99142f82c47dae2dab3219f905e00cfaebaeb34dfbb55044ec55469c1c326f7f` |

The two code/test hashes equal the R5-C4 post-correction values, confirming the expected
starting implementation.

Post-correction SHA-256 (the two authored files):

| File | SHA-256 (post-correction) |
|---|---|
| `app.js` | `8dbbdb535d28f08a8a94c815316a5ff0aba48e44d67f9a89c98044fdbe230d9f` |
| `dom_scenarios.js` | `a19183ab77816a683867f3152e31e1c54b24083cd5b1f2d089f8804f2bf67fab` |

## 3. RED — the reproduced defect

A desktop variant of the existing held-DELETE mobile scenario was added to
`tests/js/dom_scenarios.js`:

`a_surviving_conversation_row_keeps_the_keyboard_across_a_delete_and_refill_on_a_wide_screen`

It opens the page at the default wide width (sidebar beside the chat), holds the DELETE for
conversation A and separately holds the list refill, confirms DELETE, then moves the keyboard
to surviving conversation B's row with `focus()` only — no activation, drawer interaction or
section change. It then settles DELETE, asserts B keeps the keyboard, resolves the refill, and
asserts B still keeps the keyboard, A is removed and exactly one refill occurred.

Executed against the unmodified R5-C4 implementation:

```
AssertionError [ERR_ASSERTION]: the surviving row keeps the keyboard after the removal
  at a_surviving_conversation_row_keeps_the_keyboard_across_a_delete_and_refill_on_a_wide_screen
  (tests/js/dom_scenarios.js:1557)
```

The assertions before it held: the DELETE was out and held, exactly one refill was requested,
the deleted conversation was gone and the list had been rebuilt. The failure is the focus
postcondition: the rebuilt surviving row did not have the keyboard because the DELETE
completion's ordinary restoration had called `els.chatTitle.focus()` after the redraw. The
separately held refill was not the cause. No production code was modified before this RED was
recorded.

## 4. Minimal correction and rationale

The independent reviewer recommended that the DELETE completion respect focus already held by a
surviving conversation row following the list redraw, while preserving legitimate ordinary
restoration when no surviving row owns focus.

**ADOPTED**, using the existing stable identity and mechanism:

- `app.js`, `deleteConversationControls`' completion, inside the existing
  `if (result.wasCurrent)` branch: before `setDrawer(false, false)` and
  `els.chatTitle.focus()`, return when `document.activeElement` is inside `els.list` and carries
  a non-null `data-conversation`. That is precisely the R5-C4 rebuilt row.
- The check reuses `within()` and the `data-conversation` attribute introduced for R5-C4. It
  adds no counter, no state and no controller change.
- When no list row holds focus — an ordinary DELETE on a narrow screen, a deleted question's
  now-detached confirm control, focus on the composer or elsewhere outside the list — the
  branch behaves exactly as before. The `result.interaction` and `interactionSeq` gates that
  precede it are untouched, as are removal, the single refill, drawer, selection and feedback.

**REJECTED:** a new focus-management framework, an additional interaction counter, reusing row
elements or a keyed diff, or touching `chat-logic.js`. None is required by the defect, and all
would exceed the authorized boundary.

Rationale for skipping the drawer call as well as the title focus: the guarded state is a
focused conversation row, which on a wide screen is the visible list beside the chat and on a
narrow screen can only coexist with an open drawer; in either case the row owns the keyboard
and the drawer must be left as it is. The state is unreachable on a narrow screen without a
drawer interaction (which already returns earlier through `interactionSeq`), so no existing
narrow behavior changes.

## 5. GREEN evidence

The same scenario, with no assertion weakened or removed, after the correction:

| Scenario | GREEN |
|---|---|
| `a_surviving_conversation_row_keeps_the_keyboard_across_a_delete_and_refill_on_a_wide_screen` | pass — the deletion is held and out; B holds the keyboard before the settlement and nothing is activated; exactly one refill is requested; A is removed and the list rebuilt; the rebuilt B row keeps the keyboard after the removal and after the refill; the chat returns to the new empty draft. |

## 6. Focused regression results

Local Windows 11, Python 3.12.10, Node.js. The change is client-side and no backend,
persistence or API contract changed; the full repository regression and PostgreSQL were not
run. No test was skipped, and no existing assertion was weakened or removed.

| Command / scope | Result |
|---|---|
| `node tests/js/run_scenarios.js --tz=UTC` | **438 scenarios, 0 failed** (unchanged) |
| `tests/test_web_ui_dom.py` | **202 passed** (201 preserved + the new desktop scenario) |
| `tests/test_web_ui_logic.py` | **460 passed** |
| `tests/test_web_ui.py` | **66 passed** |
| `tests/test_web_ui_assets.py` | **114 passed** |
| `tests/test_web_ui_integration.py` | **24 passed** |

The focused scenarios named in the brief were each re-run and passed:

| Requirement | Scenario(s) |
|---|---|
| Wide-screen surviving-row focus through removal and refill | `a_surviving_conversation_row_keeps_the_keyboard_across_a_delete_and_refill_on_a_wide_screen` (new) |
| Mobile surviving-row focus through removal and refill | `a_surviving_conversation_row_keeps_the_keyboard_across_a_delete_and_refill` |
| Ordinary DELETE focus restoration | `deleting_the_open_conversation_asks_first_and_returns_to_a_new_empty_one`, `the_actions_menu_deletes_the_open_conversation_and_the_list_keeps_the_rest`, `a_failed_deletion_keeps_the_conversation_and_offers_a_retry`, `a_failed_list_refresh_after_a_deletion_offers_its_retry_and_keeps_it_deleted` |
| Duplicate and obsolete DELETE completion | `a_delayed_duplicate_delete_does_not_take_focus_from_another_conversation` |
| Drawer and section interaction ownership | `a_delayed_delete_does_not_close_a_reopened_drawer_or_take_its_focus`, `a_delayed_delete_does_not_interrupt_a_newer_section_choice`, `the_drawer_opens_and_closes_on_a_narrow_screen_and_does_nothing_on_a_wide_one`, `the_mobile_drawer_makes_the_shopping_pane_inert_and_restores_it` |
| Shopping-draft entry and cancellation | `leaving_a_shopping_draft_is_a_newer_interaction_than_a_delayed_delete`, `a_draft_can_be_cancelled_back_to_the_conversation_without_a_list` |
| Focus outside the conversation list | the composer-focus and duplicate-DELETE scenarios above |
| Attribute safety | `the_script_never_sets_a_handler_a_style_a_link_or_an_address_on_anything` (no new attribute was introduced; `data-conversation` was already allowlisted by R5-C4) |

Final focus postcondition is asserted after both the removal and the refill in both surviving-row
scenarios. No previously closed assessment was reopened.

## 7. Changed-file inventory and scope compliance

Production (authorized boundary only — `house_helper/web/static/app.js`):

- `deleteConversationControls`' completion, `result.wasCurrent` branch: the guarded return when
  a surviving conversation row holds focus. No other line of `app.js` changed.

Tests (authorized — `tests/js/dom_scenarios.js`):

- One new wide-screen scenario (above). No existing scenario was edited.

Documentation:

- `docs/product/PP-01-A1_R5-C5_Correction_Record.md` (this record)
- `docs/product/PP-01-A1_R5_Implementation_Record.md` (§17 addendum)
- `docs/product/PP-01-A1_Implementation_Record.md` (§16.5)
- `docs/STATUS.md` (R5-C5 entry)

No change to `chat-logic.js`, backend, API, database schema or migration, prompts,
dependencies, configuration, Telegram, retention or account isolation. Option B, the frozen
D1–D7 Scope Lock, AD-C1/AD-C2, the ten invariants, A1-02…A1-08 and C3/C4 are unchanged.

## 8. Remaining limitations

- Focus ownership is page-lifetime DOM identity. A reload discards it, and the rules do not
  extend across reloads, tabs or distributed execution.
- The guard recognizes a focused conversation row by its `data-conversation` attribute inside
  the list. A future non-row control placed inside the list without that identity would not
  count as a surviving row; no such control exists.
- The scenarios exercise the scripted controller harness and mini-DOM; no real browser was
  used. The mini DOM does not reset `document.activeElement` when it clears a focused node, so
  the ordinary-restoration path is also exercised when the deleted question's confirm control
  is detached and no list row holds focus; in a real browser that element's focus may instead
  fall to `body`. The guarded path makes the same decision in either case because it tests the
  list, not the detached control.
- PostgreSQL and the backend suites were not used: the change is client-side and no server or
  persistence contract changed.
- A1-01 remains OPEN until independently verified.

## 9. Compliance and scope preservation

- Option B preserved; the frozen D1–D7 Scope Lock, the Architecture Lock and its erratum are
  not edited.
- A1-02…A1-08 not reopened; AD-C1/AD-C2 and the R5-C1…R5-C4 corrections remain independently
  closed.
- FIX-01 behavioural baseline **24 PASS / 6 FAIL** unchanged; KHD-50 release block in force;
  PP-01-A1 is not Owner-accepted.
- **No Git mutation** (no commit, push, stash, reset, checkout or stage), **no Production
  access or deployment**, and **no provider call**.

**Verdict: R5-C5 IMPLEMENTED — RED/GREEN VERIFIED — READY FOR INDEPENDENT CLOSURE**

## 10. Subsequent final independent acceptance (2026-11-27)

This section is a later annotation, added by the documentary correction; sections 1–9 above
preserve the pre-acceptance record and are not rewritten.

The final independent Codex acceptance of PP-01-A1-R5-C5 returned **PASS — ALL EIGHT FINDINGS
CLOSED / READY FOR OWNER FUNCTIONAL ACCEPTANCE**. Its recorded disposition is **A1-01 —
Asynchronous Web consistency — Closed with C1 desktop focus verified**, and its final independent
verification was **17 PASS, 0 FAIL, 0 SKIP**, with no remaining material technical defects or
evidence gaps.

The `A1-01 OPEN` and "READY FOR INDEPENDENT CLOSURE" statements in sections 1–9 are the
historically accurate intermediate status at the time of this record. They are superseded as a
final disposition by the acceptance above. The final Codex report itself is not a separate
repository artifact; its verdict, disposition and counts are recorded from the Owner-supplied
final acceptance brief and the 2026-11-27 documentary correction request (see
`PP-01-A1_Owner_Acceptance_Closure_Record.md`, §4.1). No technical audit was reopened or rerun
by this annotation.
