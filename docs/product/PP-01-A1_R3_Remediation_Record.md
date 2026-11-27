# House Helper — PP-01-A1-R3 Targeted Remediation
## Remediation Record

| Field | Value |
|---|---|
| Date | 2026-11-27 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, unchanged) |
| Implementation record | [`PP-01-A1_Implementation_Record.md`](PP-01-A1_Implementation_Record.md) |
| Previous remediations | [`PP-01-A1_R1_Remediation_Record.md`](PP-01-A1_R1_Remediation_Record.md), [`PP-01-A1_R2_Remediation_Record.md`](PP-01-A1_R2_Remediation_Record.md) |
| Audit source | PP-01-A1-R2 independent re-audit (Codex): A1-01 remains the only open BLOCKER, with three confirmed sub-findings A1-01a/b/c |
| Status | **Remediation implemented in the working tree; pending final targeted Codex re-audit. Not committed, Owner-accepted, released or deployed.** |
| FIX-01 behavioral baseline | **24 PASS / 6 FAIL** (unchanged; KHD-50 release block in force) |

## 1. Scope and preserved findings

The R2 independent re-audit closed **A1-02, A1-03, A1-04, A1-05, A1-06, A1-07 and
A1-08** and left only **A1-01** open, as three confirmed defects. R3 remediates exactly
those three. The seven independently closed findings were not reopened and their verified
invariants are preserved. Nothing in this record claims an independent closure; the
findings remain open until Codex re-verifies them.

The R2 mechanism that the audit verified — the non-aborting soft timeout and the
serialized per-conversation title queue, which prevent commit inversion within an
uninterrupted controller epoch — is preserved and used unchanged. No migration,
persistent operation journal, distributed lock, cross-tab synchronization, provider call,
system-prompt change, FIX-01 change, research execution, Production access or Git
operation was performed. The frozen D1–D7 Scope Lock was not edited.

## 2. A1-01a — confirmed title state must survive pagination

### 2.1 Root cause

`keepConfirmedTitles` sourced the three title fields (`title`, `titleOrigin`, `canReset`)
from the item already in `state.list.items`, and `mergeConversationItem` could only
replace an item that was present. That made the current list page the sole store of
confirmed title metadata. In the audited transition the conversation left the page while
its rename was held: the confirmation arrived with no list item to update, and an older
list read released afterwards carried the conversation's previous title. Because no
previous item existed at that moment, the older snapshot was applied verbatim and the
confirmed title was lost.

### 2.2 Correction (`house_helper/web/static/chat-logic.js`)

A new page-memory store, `titleConfirmed`, is keyed by conversation and holds
`{title, titleOrigin, canReset, mark}`. It is written only from a **confirmed** PATCH
answer (`outcome.type === "updated"`), independently of list membership:

- `keepConfirmedTitles(items, titleMarkMine)` consults `titleConfirmed`, not
  `state.list.items`. For an incoming item whose entry has `mark > titleMarkMine` (the
  confirmation happened while this read was out), only the three title fields are taken
  from the store; every other incoming field is applied unchanged.
- Membership, ordering, `createdAt`, `updatedAt`, `lastMessageAt`, `hasMore` and any other
  metadata remain the server's. The function maps over the server's items only, so it
  never inserts a conversation, never resurrects a deleted one and never changes order.
- A read that begins after the confirmation records `titleMarkMine >= mark`; it applies
  the server's title and **drops the entry**, so a title changed by another tab or channel
  is reflected, and metadata cannot accumulate.
- Reset is stored the same way (`title = derived/none`, `canReset = false`), so the
  mechanism is identical for rename and reset.
- `forgetConversation` (a confirmed deletion or a 404) and `resetData` (session/account
  change) delete the entries. Entries are one per conversation, replaced in place; there
  is no per-operation log, so accumulation is bounded by the number of conversations this
  page has mutated, not by the number of operations.

`titleAppliedMark` was superseded by the entry's `mark`; `titleMutationCount` remains the
global read counter. `titleApplied`/`titleSeq` (mutation supersession) and
`titleQueues`/`titleOps`/`titleBlocked` (ordering and guard) are unchanged.

### 2.3 Deterministic evidence (`tests/js/logic_scenarios.js`)

- `a_confirmed_rename_survives_the_audited_fifty_item_list_transition` — the full audited
  sequence: 50 items; a held rename; A leaves the list when another conversation is
  deleted and the page reads the list again; an older read that began before the
  confirmation and carries A with its old title; the rename commits while A is not
  listed; the read is released. A reappears with the confirmed title, the read's order and
  membership, and the read's `created_at`/`last_message_at`; a later read then shows a
  title changed elsewhere, proving the store is not permanent.
- `a_confirmed_reset_survives_an_off_page_list_transition` — the same for a reset (the
  older snapshot's manual title is refused; the derived title and `canReset = false`
  remain).
- `deleting_a_conversation_while_its_title_write_is_pending_never_resurrects_it` — the
  conversation is deleted while its rename is out; the delayed 404 removes it, nothing is
  re-inserted by title metadata, and another conversation stays usable.
- The R2 scenarios remain and pass unchanged, including
  `a_stale_list_read_cannot_restore_a_title_a_confirmed_rename_replaced`,
  `..._a_confirmed_reset_removed`, `..._an_inactive_conversations_old_title` and
  `overlapping_list_reads_never_restore_a_confirmed_title_and_a_later_read_agrees`; the
  new tests add the absent-item case the R2 tests did not cover.

## 3. A1-01b — ordering guards across same-document restoration

### 3.1 Root cause

The persisted `pageshow` handler called `controller.start()`, which increments the epoch
and calls `resetData()`, clearing `titleQueues`, `titleOps` and `titleBlocked`. A
back/forward-cache restoration of the *same* document was therefore treated as a fresh
page: an indeterminate reset was forgotten, and a newer rename could be sent while the
earlier request could still commit, re-creating the commit inversion the R2 queue
prevents. The same reset also discarded unsaved page state that a restoration should keep.

### 3.2 Correction

- `house_helper/web/static/chat-logic.js` gains `restore()`. On the app screen it calls
  `recheck(null)`, the existing same-document return path: the page is held back, the
  server is asked who is signed in, and the same person is let through with threads,
  drafts, `titleQueues`, `titleOps`, `titleBlocked`, `titleSeq`, `titleApplied` and
  `titleConfirmed` intact. A different person is sent to `start()` (which clears
  everything), a definite 401 ends the session (clearing everything), and an unanswerable
  check keeps the page held back. Any other screen has no signed-in title state and still
  starts over.
- `recheck` and `verifySession` accept an optional 401 notice; restoration passes `null`
  so a restored page the server no longer knows shows the plain sign-in (as it did
  before), while a session that ends during use still says it expired.
- `house_helper/web/static/app.js` wires `pageshow` (`event.persisted`) to
  `controller.restore()` instead of `controller.start()`.
- The soft timeout is untouched: a title PATCH still attaches no abort signal and the
  queue remains behind it. `verifySession`/`holdBack` do not cancel or abort it, and the
  epoch is unchanged for the same account, so its real outcome is still accounted for.
- Account isolation is unchanged: a delayed answer from the previous context is dropped
  by the epoch after `start()`, and `resetData()` removes the previous account's guard and
  confirmed titles.

### 3.3 Deterministic evidence

`tests/js/logic_scenarios.js`:

- `a_same_document_restoration_keeps_a_pending_title_write_queued` — a reset is out; a
  restoration asks `/auth/me` again but does not re-read the list; a rename is issued and
  stays queued; the released reset commits first and the rename second, in the page's
  order.
- `a_same_document_restoration_keeps_an_unconfirmed_title_write_guarded` — after a
  transport loss the guard survives the restoration; a save is refused without a request,
  the draft stays, and another conversation is still renameable.
- `a_restored_page_of_another_person_discards_the_previous_title_state` — the restored
  document belongs to another account: the page starts over, the previous list and guard
  are gone, and the delayed answer to the old request changes nothing.

`tests/js/dom_scenarios.js` (the real `app.js` and persisted `pageshow` wiring):

- `a_page_restored_from_the_back_forward_cache_keeps_an_unconfirmed_title_write_guarded` —
  fires `pageshow` with `persisted: true`; asserts `/auth/me` is asked again, the
  conversation list is not re-read, the page stays on the app screen, the guard text
  remains, and a rename sends no request while keeping its draft.
- `a_page_restored_from_the_back_forward_cache_keeps_a_pending_title_write_in_order` —
  fires the same event while a reset is out; the soft-timeout pending text appears, a
  rename is queued, and after release the PATCH bodies are `{title: null}` then
  `{title: "Новое имя"}`.
- The existing `a_page_restored_from_the_back_forward_cache_asks_again_who_is_signed_in`
  still passes: a persisted restore answered by a 401 shows the plain sign-in with the
  page cleared.

**Persistence consequence through the real PATCH path.** The audited hazard itself — an
older reset committing last and clearing a newer renamed title — is reproduced on SQLite
and PostgreSQL by
`tests/test_web_chat_api.py::test_an_overlapping_reset_can_commit_after_a_newer_rename_through_the_real_route`
(held at `persistence.conversations.reset_conversation_title` before it touches the
database); its companion
`test_the_pages_order_reset_then_rename_leaves_the_rename_as_the_persisted_title` shows
the order the page's queue now preserves across a same-document restoration. Both pass on
both engines (section 7).

## 4. A1-01c — correct mutation outcome classification

### 4.1 Root cause

`classifyConversationUpdate` treated a 5xx carrying the application's error catalogue as a
definitive failure, on the assumption that the catalogue proves a rollback. But the PATCH
route commits inside `WebDatabase.run`'s session block and only then builds the
`JSONResponse`; an exception during response construction is caught by the route wrapper
and answered as the application's own `500 internal_error` **after the title was
persisted**. The same response shape therefore proves nothing about commit.

### 4.2 Correction

`classifyConversationUpdate` now classifies for title mutations as:

| Answer | Class | Effect |
|---|---|---|
| 200/201 with this application's conversation | definitive success | applied; queue proceeds |
| 404 `conversation_not_found` | definitive gone | conversation removed; no retry |
| Other 4xx with the application's error | definitive refusal | error shown to its editor; queue proceeds |
| 401 | session ended | existing auth-loss flow |
| Any 5xx (with or without the catalogue) | **indeterminate** | guard; no retry; nothing marked applied |
| 200/201 without this application's conversation | **indeterminate** | guard |
| No answer (transport, abort, soft timeout) | **indeterminate** | guard |

The per-conversation guard, the non-aborting soft timeout and the serialized queue are
unchanged. A 5xx no longer marks the write applied, advances the queue or reports a
rollback; the conversation stays guarded until the page is reloaded.

`TEXT.titleUnconfirmed` now carries the required recovery wording:

> Не удалось подтвердить результат изменения названия. Запрос еще может завершиться на
> сервере. Обновление страницы позволит проверить текущее название, но не гарантирует,
> что предыдущая операция уже завершилась.

It distinguishes checking the current saved title from proving the earlier operation
finished, and does not promise that a reload cancels or rolls back the request. The short
guard label and the 4xx messages (`titleRenameFailed`, `titleResetFailed`) are unchanged.

### 4.3 Evidence

`tests/js/logic_scenarios.js`:

- `every_5xx_for_a_title_write_is_indeterminate_and_guards_the_conversation` — an
  application 500 `internal_error`, an application 503 and a proxy 502 all guard; the
  wording assertions pin the "may still complete" and "reload only checks" facts and
  reject the old "not saved" phrasing.
- `a_success_status_whose_body_is_not_the_conversations_is_indeterminate` — a 200 without
  the conversation guards, claims nothing, and is not retried automatically.
- `a_definitive_failure_lets_the_next_queued_title_write_proceed` now uses a 400 refusal
  (the previous 503 was the defect's incorrect assumption) and still proves progress.
- `a_manual_title_is_validated_saved_and_reset_with_server_state` uses a 400 refusal for
  its failed-save step.
- The transport-interruption, soft-timeout, proxy, draft-preservation, guard and
  other-conversation scenarios from R2 remain and pass.

`tests/test_web_chat_api.py` (real route and persistence, SQLite and PostgreSQL):

- `test_a_failure_while_building_the_answer_comes_after_the_commit_and_the_title_is_stored`
  — `chat_api._conversation_json` is made to raise after `database.run` returned; the
  route answers 500 `internal_error`, and an independent ORM read shows the title stored.
- `test_a_database_failure_before_the_commit_is_a_definitive_refusal` — the mutation
  itself raises `OperationalError` before the session block commits; the route answers 503
  and the stored title is unchanged. The two cases differ only by when the failure
  happens, which is exactly why a client cannot infer rollback from a 5xx body.

## 5. Cross-instance limitations (unchanged, not waived)

- No durable operation ordering across genuine document reloads: reloading discards the
  guard; a request already delivered can still commit afterwards.
- No cross-tab synchronization or ordering between independent page instances.
- No distributed server-side operation sequencing and no persistent operation journal.
- A restored page can be a stale view of the list (the list is not re-read on restoration;
  the session is). This matches the existing visibility-change behavior.
- Actual browser back/forward-cache eligibility and eviction remain a manual smoke-test
  item, as stated for A1 in R2; the DOM test exercises the wiring, not a real cache.

None of these waives A1-01b: the same-document restoration defect is corrected.

## 6. Changed-file inventory

Production:

- `house_helper/web/static/chat-logic.js` — `titleConfirmed` store and reconciliation;
  `restore()`; all-5xx outcome classification; recovery wording; comment corrections.
- `house_helper/web/static/app.js` — persisted `pageshow` calls `controller.restore()`.

Tests:

- `tests/js/logic_scenarios.js` — the scenarios of sections 2–4, plus the updated
  definitive-refusal scenarios.
- `tests/js/dom_scenarios.js` — the two persisted-restoration scenarios.
- `tests/test_web_chat_api.py` — the post-commit 500 and pre-commit 500 tests.

Documentation:

- `docs/product/PP-01-A1_R3_Remediation_Record.md` (new)
- `docs/product/PP-01-A1_Implementation_Record.md` (section 14)
- `docs/STATUS.md` (R3 entry)

No migration, dependency, prompt, provider, deploy, persistence or unrelated file was
changed. `house_helper/web/chat_api.py` was already modified by R1/R2 and was not changed
by R3 (the post-commit failure is produced in the test by substituting
`_conversation_json`, not by changing the route).

## 7. Verification evidence

Local Windows 11, Python 3.12.10, Node.js 24.14.0. Affected-area verification only, per the
R3 instruction; the complete pre-existing suite was not re-run.

### SQLite (no PostgreSQL variable set)

| Command / scope | Result |
|---|---|
| `tests/test_web_ui_logic.py` (Node scenarios in UTC and the timed scenarios in Europe/Moscow, America/New_York, Asia/Kolkata) | **414 passed** |
| `tests/test_web_ui_dom.py` (`app.js` against the server shell) | **196 passed** |
| `tests/test_web_ui_assets.py` | **114 passed** |
| `tests/test_web_chat_api.py` | **188 passed** |
| Affected-area run of the four modules above | **912 passed** |
| `tests/test_web_ui.py` + `tests/test_web_ui_integration.py` | **90 passed** |
| Persistence and adjacent: `test_persistence_conversations.py`, `test_persistence_telegram.py`, `test_persistence_telegram_active_conversations.py`, `test_telegram_dialogs.py`, `test_admin_retention.py`, `test_web_app.py`, `test_assistant_identity.py`, `test_menu_recommendations.py` | **610 passed** |
| Full JS scenario file in UTC (`node tests/js/run_scenarios.js`) | **392 scenarios, 0 failed** |

No test was skipped for a missing dependency; Node.js was present.

### PostgreSQL 16.15 (disposable container)

| Command / scope | Result |
|---|---|
| `python -m tests.postgres_preflight` | `server_version_num=160015 host=127.0.0.1 port=55432 user=hh_test database=postgres` |
| `tests/test_web_chat_api.py` + `tests/test_persistence_conversations.py` (includes the two new A1-01c tests and the real-route commit-inversion reproduction) | **329 passed** (7 min 3 s) |

### PostgreSQL environment and cleanup

- Container `house-helper-test-postgres` (image `721873c34ceb`, PostgreSQL 16.15), bound
  only to `127.0.0.1:55432`; it was `Exited (0)` before use and is `Exited (0)` after it.
- Preflight matched the documented cluster (`160015`, loopback, non-5432).
- Inventory **before** the runs: databases `postgres`, `template0`, `template1`; the only
  non-built-in role was the bootstrap `hh_test` (OID 10).
- Inventory **after** the runs: identical — no non-system database and no additional role
  (`pg_roles` with OID ≥ 16384 returned nothing). The harness removed its disposable
  databases and per-run roles.
- No Production database, credential or real user data was used. Native PostgreSQL on
  port 5432 was not touched.

## 8. Remaining manual verification and Owner decisions

1. Real-browser back/forward-cache behavior (whether a page is actually cached, and when)
   is still a manual smoke-test item; the automated coverage verifies the event wiring,
   session re-check and title state, not the browser's cache decision.
2. The R2 limitation stands: in-session recovery after a genuinely indeterminate title
   write requires a reload; there is no server-side operation sequence and R3 adds none.
3. Ordering across genuine reloads and between tabs is not guaranteed and is not claimed.
4. The A1-05 uncaptioned-photo ambiguity and the A1-04 Unicode table update policy are
   unchanged from R2.
5. Independent targeted Codex re-audit; Owner review; the separate combined
   FIX-01 + PP-01-A1 release decision. The FIX-01 release block remains.

## 9. Compliance confirmation

- No database migration, provider call, system-prompt change, FIX-01 change, research
  execution or unrelated refactoring.
- No Production access, infrastructure change, deployment or Git mutation (no commit,
  push, stash, reset or checkout).
- The frozen Scope Lock (D1–D7) was not edited.
- The seven findings the R2 re-audit closed (A1-02, A1-03, A1-04, A1-05, A1-06, A1-07,
  A1-08) were not reopened; their invariants and R2 mechanisms are preserved.
- Findings are **not** marked independently closed; PP-01-A1 is not Owner-accepted.
- The FIX-01 behavioral baseline remains **24 PASS / 6 FAIL**.

**Verdict: A1-01 REMEDIATED — READY FOR FINAL TARGETED RE-AUDIT**
