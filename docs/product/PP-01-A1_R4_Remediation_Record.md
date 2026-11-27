# House Helper — PP-01-A1-R4 Invariant-Based Remediation
## Remediation Record

| Field | Value |
|---|---|
| Date | 2026-11-27 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, unchanged) |
| Implementation record | [`PP-01-A1_Implementation_Record.md`](PP-01-A1_Implementation_Record.md) |
| Previous remediations | [`PP-01-A1_R1_Remediation_Record.md`](PP-01-A1_R1_Remediation_Record.md), [`PP-01-A1_R2_Remediation_Record.md`](PP-01-A1_R2_Remediation_Record.md), [`PP-01-A1_R3_Remediation_Record.md`](PP-01-A1_R3_Remediation_Record.md) |
| Audit source | PP-01-A1-R3 targeted re-audit (Codex): A1-02…A1-08 remain independently closed; four further A1-01 defects confirmed (R4-01…R4-04) |
| Status | **Remediation implemented in the working tree; ready for targeted re-audit. Not committed, Owner-accepted, released or deployed.** |
| FIX-01 behavioral baseline | **24 PASS / 6 FAIL** (unchanged; KHD-50 release block in force) |

## 1. Scope and preserved findings

The R3 re-audit independently confirmed the off-page confirmed-title store, the
same-document restoration guard preservation and the 5xx classification introduced in R3.
It then found four further A1-01 defects on execution paths equivalent to the earlier ones.
R4 corrects those four by enforcing four state-management invariants across the existing
title/controller architecture. It does not add another layer of isolated exception handling,
reopen any of the seven independently closed findings (A1-02…A1-08), change a product
decision or expand the frozen D1–D7 Scope Lock.

No migration, persistence schema, system prompt, FIX-01 text, provider call, research
execution, Production access or Git operation was performed.

## 2. The four invariants

| # | Invariant | Where it is established | Where it is preserved | What invalidates it |
|---|---|---|---|---|
| 1 | **Authoritative object existence.** A confirmed "it no longer exists" is not reversed by an older list snapshot. | `conversationGone()` (application `conversation_not_found`) and the confirmed-deletion path both bump `listEpoch` through `invalidateListReads()`. `loadList()` records the generation when it begins and discards an answer recorded under an older one. | Current membership, order, dates and pagination of an applied list stay the server's; `keepConfirmedTitles` still only maps items the server returned. A read begun after the confirmation applies normally. | Only another authoritative event (a newer read, a deletion, a session change). No per-conversation tombstone is kept, so nothing accumulates. |
| 2 | **Current authenticated identity and session verification.** A restoration establishes a fresh verification boundary; an older answer may not authorize disclosure. | Every `/auth/me` question gets a `verifySeq`; `restore()` forces a new question (`recheck(authNotice, true)`). Only the newest question may act (`seq === verifySeq`), and only it may clear `verifyingEpoch`. | The gate keeps private content out of the view until the fresh answer arrives; `letThrough()` restores the same person's queues and guards. Epoch still discards a changed account. | A newer question, `start()` (epoch), a 401, or an unanswerable verification (gate stays `unavailable`). |
| 3 | **Definitive versus indeterminate mutation outcome.** Only a response whose server contract proves the write cannot commit releases the conversation's queue. | `classifyConversationUpdate()` uses the closed `TITLE_REFUSAL_STATUSES` table (code **and** status) plus `conversation_not_found`; every other 4xx, all 5xx, transport failures and unexpected bodies are `indeterminate`. | `titleBlocked[id]`, the serialized per-conversation queue, the non-aborting soft timeout and "no automatic retry" are unchanged. | Only the definitive set releases the queue; everything else guards until reload. |
| 4 | **Requested object and returned object identity.** A success may be applied only if it is the requested conversation and obeys the requested operation. | `classifyConversationUpdate(response, id, request)` checks `conversation.id === id` and `isExpectedTitleState()`: a rename returns exactly the requested normalized text, `title_origin = manual`, `can_reset_title = true`; a reset returns a non-manual origin, `can_reset_title = false`, and any legitimate derived text. | On any mismatch the outcome is `indeterminate`: nothing is merged, no `titleConfirmed` entry is written, and the editor/draft and guard stay. | A structurally valid, identity-matching, operation-conforming `200/201` is required for `updated`. |

## 3. R4-01 — Authoritative object existence

### 3.1 Confirmed defect

After an external deletion, a title PATCH answered `404 conversation_not_found`. The
controller removed the conversation (`conversationGone` → `forgetConversation`), but a list
GET captured before the deletion later returned the conversation and `loadList()` applied
it verbatim. The confirmed non-existence was reversed.

### 3.2 Root cause

Only `deleteConversation()` bumped `listEpoch`. The authoritative `conversation_not_found`
path (title PATCH, history read, older-page read — all through `conversationGone`) removed
the conversation locally without invalidating outstanding list reads. `loadList()` had no
way to know that its snapshot predated an authoritative "it is gone".

### 3.3 Correction (`house_helper/web/static/chat-logic.js`)

- `invalidateListReads()` is the single invalidation point; both the confirmed deletion and
  `conversationGone()` call it. `conversationGone()` is reached by every authoritative
  `conversation_not_found`: the title PATCH (`gone`), `fetchLatest`, `loadOlder` and the
  DELETE route's `gone` outcome.
- `loadList()` already discarded an answer whose recorded `listEpoch` was stale. The stale
  answer is now also prevented from leaving the list stuck in `loading`: when the answer was
  invalidated by an authoritative disappearance (and no newer read has begun), the list is
  marked `ready` from the state that authoritative answer produced. A newer read still owns
  the status otherwise.
- No conversation is remembered as deleted (`listEpoch` is a counter), so nothing
  accumulates; a read that begins after the invalidation records the new generation and
  applies the server's list normally.
- Delayed title **successes** cannot insert anything: `mergeConversationItem()` only
  replaces an item the list still holds, and `keepConfirmedTitles()` maps only items the
  server returned.

### 3.4 Evidence

`tests/js/logic_scenarios.js`:

- `a_title_404_is_not_undone_by_a_list_read_taken_before_the_conversation_vanished` — the
  full audited sequence (rename held → 404 removes A → older list answer with A arrives →
  A stays gone, B's membership/order stay, status is `ready`), then a new read applies the
  server's updated label.
- `a_reset_404_is_not_undone_by_a_list_read_taken_before_the_conversation_vanished` — the
  same invariant on the reset path.
- `several_list_reads_out_before_an_authoritative_404_are_all_discarded` — two outstanding
  reads, both discarded, then a fresh read reconciles normally.
- `a_delayed_title_success_does_not_resurrect_a_conversation_deleted_while_it_was_out` — a
  PATCH 200 for A arriving after the page confirmed A's deletion inserts nothing.

## 4. R4-02 — Authentication freshness on same-document restoration

### 4.1 Confirmed defect

An earlier `/auth/me` response for account A was still in flight when the browser session
changed to account B. On persisted `pageshow`, `restore()` → `recheck()` → `verifySession()`
returned early because `verifyingEpoch === epoch` (a question was already out). The older
answer then let the page through as account A, reopening A's list, title and draft.

### 4.2 Root cause

Verification deduplication was keyed only by epoch. It assumed any in-flight question was
equivalent to a fresh one, so a restoration could not establish a new boundary while one
was out.

### 4.3 Correction

- `verifySeq` counts verification questions. `verifySession(authNotice, force)` forces a new
  question for a restoration; a response acts only while `seq === verifySeq` (and its epoch
  is current). A superseded answer neither lets the page through, nor reports an
  unavailable gate, nor begins a page, nor clears the newer question's `verifyingEpoch`.
- `restore()` calls `recheck(null, true)`. Ordinary `recheck()` (visibility change, online,
  resume) keeps its deduplication.
- **Private-content rule:** a restoration always holds the page back before asking.
  `heldView()` shows nothing of the list, title, thread, draft, favorites, shopping or
  profile until the fresh answer lets the same person through. An unanswerable fresh
  verification leaves the page `unverified`; the old answer cannot change that.
- **No queued private write enters an unverified context.** `sendTitleMutation()` waits on
  `gateBarrier()` while `state.gate` is set and the epoch is unchanged. `letThrough()`
  releases the waiters (same person); `resetData()` releases them on a page start-over, and
  the waiter then sees the changed epoch and drops the write. A queued rename therefore
  stays behind the restoration's verification and is never sent to another account.
- Same-account `letThrough()` keeps `titleQueues`, `titleOps`, `titleBlocked`, `titleSeq`,
  `titleApplied` and `titleConfirmed`; the non-aborting soft timeout is untouched.

### 4.4 Evidence

`tests/js/logic_scenarios.js`:

- `a_restoration_supersedes_an_older_verification_that_arrives_before_the_new_one` — the
  audited reproduction: the older question (A) arrives first, the page stays `verifying`
  with an empty held view; the fresh answer (B) starts the other person's page; the draft
  of the previous person is gone.
- `a_restoration_ignores_an_older_verification_that_arrives_after_the_new_one` — the fresh
  answer (A) lets the page through; the older late answer (B) changes nothing and begins no
  new page (exactly three `/auth/me` requests).
- `a_queued_title_write_waits_for_the_restorations_verification` — a rename queued behind a
  held reset is not sent while the page is unverified; after the same person is verified the
  server sees `{title: null}` then `{title: "Новое имя"}`.
- `a_restoration_of_another_person_drops_the_queued_write_instead_of_sending_it` — the
  queued write settles `stale`; only the reset was ever sent; the other account's page is
  shown.
- `a_delayed_list_answer_of_the_previous_person_never_appears_after_a_restoration` — a list
  read begun on the previous person's page, answered after the other person's page has
  started, is discarded (epoch) and the fresh list stays.
- The R3 scenarios for the same-account queue, the indeterminate guard and the plain
  restored-page 401 remain green (`tests/js/dom_scenarios.js` DOM wiring included).

## 5. R4-03 — Fail-closed mutation outcome classification

### 5.1 Confirmed defect

A proxy-generated HTTP 408 arrived after an earlier reset was dispatched. `classifyConversationUpdate()` returned the fallback `unavailable` type, which `sendTitleMutation()` treated as a definitive refusal: the queue was released. A newer rename committed before the earlier reset, which then cleared it.

### 5.2 Root cause

The classifier's definitive set was "any 4xx that carries an application-shaped error body",
and every remaining 4xx fell into a non-`indeterminate` fallback. Neither rule is proof that
the server did not commit: an intermediary can answer 4xx itself, and the application's
error body alone does not establish which request it belongs to or where it was raised.

### 5.3 Correction — the closed definitive set

`classifyConversationUpdate()` now releases a conversation's title queue only for these
application answers, each of which the route raises **before** its database work:

| Status | Code | Why the server contract proves no commit |
|---|---|---|
| 403 | `forbidden_origin` | `authenticated_title_mutation` refuses the Origin before authentication or any database call. |
| 415 | `unsupported_media_type` | `_read_json_object` refuses the media type before the body is parsed or `database.run` is called. |
| 413 | `request_too_large` | The declared or streamed body size is refused while reading, before `database.run`. |
| 400 | `invalid_request` | FastAPI validation, `ClientDisconnect`, or the route's field-shape check — all before the operation is built. |
| 400 | `empty_title` / `title_too_long` / `invalid_title` | `normalize_conversation_title` raises before `database.run`; no UPDATE has been issued. |
| 404 | `conversation_not_found` | The route's `_conversation_id`, or `ConversationNotFound` from the transaction, which rolls back; classified as `gone`, not as a retryable refusal. |

The status is part of the contract: a code at a status its catalogue entry does not fix is
not accepted as definitive. Everything else is `indeterminate` and guards the conversation:

- proxy 408, 429, 418 or any other 4xx without this application's body;
- a 4xx carrying a code outside the table (e.g. `conversation_busy`, `invalid_query`);
- a 4xx carrying a table code at the wrong status;
- every 5xx, including the application's own `internal_error` and `service_unavailable`
  (the route commits before it builds its answer; a post-commit failure is a 5xx);
- transport interruption, an aborted or soft-timed-out request;
- a `200/201` whose body is not this application's conversation (R4-04);
- any other unclassified answer.

No automatic retry is introduced. The guard, the non-aborting soft timeout and the
serialized queue are unchanged; drafts and the truthful Russian feedback
(`TEXT.titleUnconfirmed`) are preserved, and other conversations stay usable.

### 5.4 Evidence

`tests/js/logic_scenarios.js`:

- `a_proxy_408_after_an_earlier_reset_never_releases_a_newer_rename` — the audited commit
  inversion: the reset's proxy 408 is `indeterminate`; the queued rename is refused without
  a request, its draft stays, the banner is the truthful guard text, and another
  conversation is still renameable.
- `only_the_applications_pre_commit_refusals_release_a_title_write_queue` — each of the
  seven definitive pairs releases the queue and the following rename succeeds; each of a
  proxy 408, a proxy 429, a bare 400, an application `conversation_busy` 409, an
  application code at the wrong status and `invalid_query` guards, sends nothing further and
  preserves the label and draft.
- The R3 5xx, malformed-success and transport scenarios remain green.

`tests/test_web_chat_api.py` (real route; SQLite and PostgreSQL):

- `test_every_definitive_title_refusal_is_answered_before_any_write` — every member of the
  closed set is issued through the real PATCH route; the response carries exactly the
  expected status/code, and the stored `title` and `updated_at` are byte-for-byte the
  pre-request values.
- `test_a_conversation_not_found_refusal_is_also_before_any_write` — the real 404 outcome.
- The R3 real-route commit-inversion and post-commit-500 / pre-commit-503 tests remain
  green on both engines.

## 6. R4-04 — Mutation response identity and integrity

### 6.1 Confirmed defect

A title PATCH targeting conversation A was answered `200` with a structurally valid summary
for conversation B. The controller reported success, replaced A's list item with B's
metadata (duplicating B) while A's stored title was unchanged.

### 6.2 Correction

- `classifyConversationUpdate(response, id, request)` accepts `updated` only when the
  normalized conversation's `id` equals the requested `id` (case-insensitive) **and**
  `isExpectedTitleState()` holds:
  - rename (`request.title` is a string): returned `title` equals the normalized text this
    page sent, `title_origin === "manual"`, `can_reset_title === true`;
  - reset (`request.title === null`): `title_origin !== "manual"` and
    `can_reset_title === false`; the derived text itself may legitimately differ, so it is
    not compared.
- A mismatched or contract-violating success is `indeterminate`: `titleBlocked` is set, no
  `titleConfirmed` entry is written, `mergeConversationItem()` is not called, the editor and
  draft remain, and no automatic retry is made.
- The API response schema is unchanged; the real route already answers the requested
  conversation with the requested state (`tests/test_web_chat_api.py` pins the manual and
  reset shapes).

### 6.3 Evidence

`tests/js/logic_scenarios.js`:

- `a_title_answer_about_another_conversation_is_indeterminate_and_changes_nothing` — B's
  summary for A's request guards; the list keeps exactly `[A, B]` (no duplicate), A keeps
  its title, B keeps its metadata.
- `a_rename_answer_that_is_not_the_requested_manual_title_is_indeterminate` — same id, but
  derived/different text: guarded, nothing applied, draft kept.
- `a_reset_answer_that_claims_a_manual_title_is_indeterminate` — the reset contract is
  enforced in the other direction.

## 7. Verification

Local Windows 11, Python 3.12.10, Node.js 24.14.0. Focused verification only; the complete
regression suite was not re-run.

### SQLite / Node (no PostgreSQL variable set)

| Command / scope | Result |
|---|---|
| `node tests/js/run_scenarios.js --tz=UTC` | **406 scenarios, 0 failed** (392 existing + 14 new) |
| `tests/test_web_ui_logic.py` (scenarios in UTC and the timed groups in Europe/Moscow, America/New_York, Asia/Kolkata, plus the crosschecks) | **428 passed** |
| `tests/test_web_ui_dom.py` (real `app.js` against the server shell) | **196 passed** |
| `tests/test_web_ui_assets.py` + `tests/test_web_ui.py` + `tests/test_web_ui_integration.py` | **204 passed** |
| `tests/test_web_chat_api.py` | **196 passed** |

No test was skipped for a missing dependency; Node.js was present.

### PostgreSQL 16.15 (disposable container)

| Command / scope | Result |
|---|---|
| `python -m tests.postgres_preflight` | `postgresql preflight ok: server_version_num=160015 host=127.0.0.1 port=55432 user=hh_test database=postgres` |
| `tests/test_web_chat_api.py` (includes the new R4-03 route tests and the R3 real-route evidence) | **196 passed** (305.35 s) |

### PostgreSQL environment and cleanup

- Container `house-helper-test-postgres` (image `721873c34ceb`, PostgreSQL 16.15) bound only
  to `127.0.0.1:55432`; it was `Exited (0)` before use and is `Exited (0)` after it.
- Inventory **before** the runs: the only non-template database was `postgres`; no role with
  OID ≥ 16384 (the bootstrap `hh_test`, OID 10, is the only non-built-in role).
- Inventory **after** the runs: identical — no disposable database and no additional role.
  The harness removed its per-run databases and roles.
- No Production database, credential or real user data was used. Native PostgreSQL on port
  5432 was not touched.

## 8. Changed-file inventory

Production:

- `house_helper/web/static/chat-logic.js` — `invalidateListReads()` and the authoritative
  `conversationGone()` invalidation; stale-read status reconciliation; `verifySeq` and the
  forced restoration verification; `gateBarrier()`/`releaseGateWaiters()` and the queued
  write barrier; the closed `TITLE_REFUSAL_STATUSES` contract; identity/state validation in
  `classifyConversationUpdate()`.

Tests:

- `tests/js/logic_scenarios.js` — the 14 R4 scenarios of sections 3–6.
- `tests/test_web_chat_api.py` — the closed-set route evidence (7 parametrized refusal
  cases plus the separate 404 case).

Documentation:

- `docs/product/PP-01-A1_R4_Remediation_Record.md` (new)
- `docs/product/PP-01-A1_Implementation_Record.md` (section 15)
- `docs/STATUS.md` (R4 entry)

No migration, dependency, prompt, provider, deploy, persistence or unrelated file was
changed. `house_helper/web/static/app.js` needed no change: the persisted `pageshow` already
calls `controller.restore()`, and the fresh boundary is enforced inside the controller.

## 9. Remaining limitations (unchanged, not waived)

- No durable ordering across genuine document reloads: reloading still discards the guard,
  and a request already delivered can still commit afterwards.
- No cross-tab synchronization or ordering between independent page instances.
- No server-side operation journal, distributed sequencing or locking; no new migration.
- After a genuinely indeterminate title write, in-session recovery still requires a reload;
  the page never retries automatically.
- A restored page remains a stale view of the list (the list is not re-read on restoration;
  the session is). This matches the existing visibility-change behavior.
- Actual browser back/forward-cache eligibility and eviction remain a manual smoke-test
  item; the DOM tests exercise the event wiring and controller behavior, not the browser's
  cache decision.
- The A1-05 uncaptioned-photo ambiguity and the A1-04 frozen Unicode policy are unchanged.

## 10. Compliance

- No database migration, provider call, system-prompt change, FIX-01 change, research
  execution or unrelated refactoring.
- No Production access, infrastructure change, deployment or Git mutation (no commit, push,
  stash, reset or checkout).
- The frozen Scope Lock (D1–D7) was not edited.
- A1-02…A1-08 were not reopened; their R1/R2/R3 mechanisms are preserved.
- Findings are **not** marked independently closed; PP-01-A1 is not Owner-accepted.
- The FIX-01 behavioral baseline remains **24 PASS / 6 FAIL**.

**Verdict: A1-01 INVARIANTS ENFORCED — READY FOR TARGETED RE-AUDIT**
