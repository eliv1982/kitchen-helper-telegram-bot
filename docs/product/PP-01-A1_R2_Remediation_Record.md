# House Helper — PP-01-A1-R2 Targeted Remediation
## Remediation Record

| Field | Value |
|---|---|
| Date | 2026-11-26 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, unchanged) |
| Implementation record | [`PP-01-A1_Implementation_Record.md`](PP-01-A1_Implementation_Record.md) |
| Previous remediation | [`PP-01-A1_R1_Remediation_Record.md`](PP-01-A1_R1_Remediation_Record.md) |
| Audit source | PP-01-A1 Independent Re-audit (Codex), findings A1-01, A1-04, A1-05, A1-07 still open |
| Status | **Remediation implemented in the working tree; pending independent targeted Codex re-audit. Not committed, Owner-accepted, released or deployed.** |
| FIX-01 behavioral baseline | **24 PASS / 6 FAIL** (unchanged; KHD-50 release block in force) |

## 1. Scope and preserved findings

This record covers only the four findings the R1 re-audit left open. The four findings the
same re-audit closed independently — **A1-02** (explicit Origin), **A1-03** (latest
eligible message chronology), **A1-06** (Moscow label on the fallback) and **A1-08**
(retention disclosure and physical purge) — were not reopened, and their verified
invariants are preserved. Nothing here claims an independent closure: the findings remain
open until Codex re-verifies them.

No product decision D1–D7 was reopened. No database migration, persistent operation
journal, distributed lock, provider call, system-prompt change, FIX-01 change, research
execution, Production access or Git operation was performed. The FIX-01 release block
remains in force. The Scope Lock is frozen and was not edited.

## 2. A1-01 — BLOCKER: asynchronous title consistency

### 2.1 R1 shortcoming: a stale list answer could restore an old title

R1 serialized title writes per conversation and rejected superseded *mutation* answers,
but the conversation **list** read was not reconciled with title confirmations. A list GET
that began before a successful rename or reset could be answered from the pre-write
snapshot; applying it replaced the confirmed title metadata item.

**Root cause.** `loadList` classed an answer only by the session epoch and by which read
was newest (`listRead`). Neither knows that a title write of one of the answered
conversations was *confirmed while the read was out*. Overlapping reads were dropped by
`listRead` only when a newer read existed; the confirmed title had no generation that the
list application consulted.

**Correction (`house_helper/web/static/chat-logic.js`).**

- Every applied title response increments a global `titleMutationCount` and records
  `titleAppliedMark[id]` for its conversation.
- A list read records the counter when it begins (`titleMarkMine`).
- Applying a list answer calls `keepConfirmedTitles(items, titleMarkMine)`: for a
  conversation whose mark is **above** the read's mark (a title write was confirmed after
  the read began), the three title fields (`title`, `titleOrigin`, `canReset`) are kept
  from the item already shown; every other field — order, `createdAt`, `lastMessageAt`,
  category-level metadata, deletion — is the server's. Every conversation without such a
  mark is applied exactly as before.
- Overlapping/superseded reads are still dropped by the existing `listRead` guard, so a
  read that a newer read has overtaken changes nothing.
- Because the next read begins **after** the confirmation, its mark is at or above
  `titleAppliedMark[id]`, so it applies the server's title again. A title changed by
  another tab or channel is therefore not suppressed permanently; eventual agreement
  with the server is preserved.
- A stale error or a stale success never touches another conversation's editor or draft
  (the R1 scoping is unchanged).

**Recovery and guarantees.** The confirmed title survives a stale answer only until the
next read, which is authoritative. The mechanism is page memory, per page instance; two
tabs remain independent. A title the page confirmed is never described as lost because an
older read arrived.

### 2.2 R1 shortcoming: an indeterminate write could invert the persisted order

The R1 queue treated the 20-second client timeout as the end of a title write. The real
transport is an `AbortController` timeout: the page aborted the fetch, which means *the
page* stopped waiting, not that the server stopped working. `web.database.run` is explicit
that a disconnected client's function runs to the end and its transaction commits or rolls
back as usual. A held reset could therefore commit **after** a newer rename that the page
had already sent and seen accepted, clearing the persisted title. This was reproduced on
SQLite and PostgreSQL through the real PATCH handler.

**Root cause.** Two distinct things were conflated:

1. the client-side decision to stop displaying a pending write, and
2. the server-side outcome of that write.

An aborted fetch, a transport failure or a timeout gives (2) no information. R1 used (1)
as if it settled (2) and released the next write. The only server-side primitive that
orders this conversation's title writes is the single owner-scoped `UPDATE`; there is no
operation sequence, journal or lock service, and the restrictions forbid adding one. The
correction therefore had to be on the client, without weakening the invariant.

**Correction (`chat-logic.js`).**

- `http` gains a **soft timeout** (`softTimeout: true`, `onTimeout`). For a title write the
  timer marks the operation "slow" and tells the open conversation so; it **does not
  abort** the request and attaches no abort signal. The returned promise settles only when
  the underlying request really settles.
- `titleOps[id]` records the write that is out. The per-conversation queue waits on that
  real settlement, so a newer same-conversation write stays queued while the earlier
  request is still in flight. The queue is not released because the client stopped
  waiting.
- `classifyConversationUpdate` now distinguishes three outcome classes:
  - **definitive success** — a 2xx carrying the conversation;
  - **definitive failure** — an application 4xx (including `conversation_not_found`), or an
    application 5xx carrying the API's own error catalogue, which proves the transaction
    rolled back; progress continues after these;
  - **indeterminate** — no answer at all (transport failure, timeout, abort), a 5xx
    without the application's error shape (a proxy's 502/504, which can be written before
    or after the upstream committed), or a 2xx whose body is not the application's
    conversation (a proxy or cache answered, so the server's outcome is unknown).
- On an indeterminate outcome the write is **not** marked applied. `titleBlocked[id]` is
  set and every later title write of that conversation is **refused, not sent**, including
  writes already waiting in the queue. There is no automatic retry and no GET is treated
  as proof that the earlier write cannot commit.

**Ordering and recovery contract (exact).**

| Situation | Behaviour | What is guaranteed |
|---|---|---|
| Earlier write still in flight, page's soft timeout reached | Request retained; visible Russian pending message; next same-conversation write queued | Server receives and commits the writes in the page's issue order; no later write is sent past an earlier request whose outcome is unknown |
| Earlier write settles definitively (2xx/4xx/application 5xx) | Outcome applied; queue proceeds | The next write is sent only after the earlier outcome is known; progress after a definitive failure is preserved |
| Earlier write's transport ends without a definitive answer | Guard set; later writes refused with a truthful message; draft preserved; item marked on the list | No conflicting write is sent past the unknown one; a persisted inversion caused by *this page's* queue cannot occur |
| User reloads the page | Guard is gone (page memory); the list shows the server's state at that moment | The reload is the recovery. The unknown request may still commit afterwards, so ordering **across reloads or between tabs is not guaranteed and is not claimed**; the existing independent-tab semantics are unchanged |
| Another conversation's title | Unaffected | The guard is per conversation; ordinary navigation and other conversations stay usable |

The pending and unconfirmed states are visible, never hidden: a status line in the rename
form, a banner on the open conversation, and a marker on the conversation's own list item
even while another conversation is open. Russian words are in `TEXT.titlePending` and
`TEXT.titleUnconfirmed`.

**Audited commit-inversion scenario, reproduced through the real API and persistence.**
`tests/test_web_chat_api.py::test_an_overlapping_reset_can_commit_after_a_newer_rename_through_the_real_route`
holds `persistence.conversations.reset_conversation_title` before it touches the database,
lets a newer rename be accepted and committed, then releases the reset: the reset commits
last and the persisted title is `NULL`. That is the hazard the page's serialized,
non-aborting queue prevents. The companion
`test_the_pages_order_reset_then_rename_leaves_the_rename_as_the_persisted_title` verifies
the order the queue enforces. Both run on SQLite and on PostgreSQL 16.15 (section 7).

**Deterministic regression scenarios (`tests/js/logic_scenarios.js`).** Deferred promises,
no sleeps:

- `a_stale_list_read_cannot_restore_a_title_a_confirmed_rename_replaced` — stale GET after a
  rename; the confirmed title survives, an unrelated conversation's newer title from
  another channel is still applied, and the server's order is used.
- `a_stale_list_read_cannot_restore_a_title_a_confirmed_reset_removed` — the same after a
  reset.
- `overlapping_list_reads_never_restore_a_confirmed_title_and_a_later_read_agrees` — two
  reads out; superseded and pre-confirmation answers do not restore the old title; a later
  read applies the server's title, including one changed by another tab.
- `a_stale_list_read_cannot_restore_an_inactive_conversations_old_title` — an inactive
  conversation keeps its confirmed title.
- `a_definitive_failure_lets_the_next_queued_title_write_proceed`.
- `a_soft_timeout_keeps_the_request_alive_and_the_next_write_queued` — the reset outlives
  the soft timeout, the request is not aborted, the rename stays queued, and the final
  persisted-order title is the rename.
- `an_unconfirmed_title_write_guards_the_conversation_and_leaves_others_usable` — transport
  failure after the soft timeout: no newer request, truthful feedback, draft preserved,
  repeat save refused, another conversation still renameable.
- `deleting_a_guarded_conversation_clears_it_and_leaves_the_other_usable`.
- `a_proxy_5xx_for_a_title_write_is_indeterminate_but_the_applications_own_is_not`.

The two original R1 race scenarios and the four R1 scenarios remain in the suite and pass
unchanged.

**Limitations.** The queue and guard are page memory. A reload discards the guard; a
request that was already delivered can still commit after the reload, so ordering across
reloads is not guaranteed. Two tabs are two page instances and are not ordered against
each other (unchanged). A proxy 5xx is deliberately treated as indeterminate even when it
in fact followed a commit, because the client cannot know; the cost is a guarded
conversation until reload, never a wrong order.

## 3. A1-04 — IMPORTANT: Unicode validation consistency

**R1 shortcoming.** The accepted emoji base was general category `So`; a standalone
variation selector, combining grapheme joiner or Hangul filler was not "characters"; any
tag character was accepted as flag content; and the emoji-base decision came from each
runtime's own Unicode tables (Python 3.12 = Unicode 15.0, Node.js 24 = Unicode 17.0), so
U+1FAE9 was classified differently by the two validators.

**Correction.**

- New `house_helper/core/title_unicode.py` holds two **frozen, explicitly versioned**
  tables — `Extended_Pictographic` (156 ranges) and
  `Default_Ignorable_Code_Point` (17 ranges) — generated from **Unicode 17.0 / ICU 78.2**
  over U+0000–U+10FFFF and recorded with that provenance.
- **Emoji base** is now membership in the frozen Extended_Pictographic table, not `So`.
  `★ ZWJ ★` (U+2605 is `So`, not an emoji) is refused; family, profession, skin-tone,
  variation-selector and subdivision-flag sequences are preserved; U+1FAE9 is an emoji
  base on **both** sides regardless of each runtime's table.
- **Blank-looking titles** are refused: after trimming, a title whose every code point is
  default-ignorable, or U+2800 BRAILLE PATTERN BLANK, is `characters`. This rejects
  standalone U+FE0F, U+034F and U+3164 while leaving combining marks and those characters
  inside meaningful text untouched.
- **Flag tag alphabet**: a subdivision-flag sequence is U+1F3F4, one or more tag **letters**
  U+E0061–U+E007A, then U+E007F. A tag space, a digit tag, the end tag alone, an
  unterminated sequence and any tag character outside that sequence are refused. A bare
  U+1F3F4 with no tag sequence remains ordinary emoji text.
- Both validators use the same tables. `chat-logic.js` embeds a generated block produced by
  `title_unicode.javascript_policy_block()`; a test asserts the exact block is present, so
  the two copies cannot drift. The affected decisions no longer depend on the runtime's
  Unicode version.
- Forbidden characters are still refused **before** trimming; the 200-code-point limit,
  backend authority, ordinary whitespace trimming and combining characters are unchanged.

**Evidence.** The six independent audit counterexamples are in the shared regression data
(`tests/test_persistence_conversations.py` invalid cases and
`tests/test_web_ui_logic.py::_title_texts`, which is cross-checked against JavaScript):
standalone U+FE0F, standalone U+034F, standalone U+3164, U+2605 ZWJ U+2605,
U+1F3F4 TAG SPACE CANCEL TAG, and U+1FAE9 (plus U+1FAE9 ZWJ U+1FAE9). A generated
Python-vs-JavaScript parity test walks **every boundary** of both frozen tables (start,
end, and the code points just outside), each alone, around a ZWJ, and inside ordinary
text, plus every tag character after U+1F3F4. The previously passing valid and boundary
cases were retained.

**Limitations.** The contract is not UAX #29 grapheme segmentation or a full RGI emoji
validator, and no new dependency or large data set was added. `Cc`/`Cs`/`Zl`/`Zp`/`Cf`
category decisions still come from each runtime; a future Unicode revision that assigns a
new format character would be known by one runtime and not the other, which is recorded.
A future emoji revision needs an explicit table update, not a silent change.

## 4. A1-05 — IMPORTANT: technical photo placeholders versus user text

**R1 shortcoming.** The derived-title query excluded *every* user message whose text is
`attachments.PHOTO_PLACEHOLDER`, including a message where the person literally typed
`[Фото]` with no photo. The attachment metadata that distinguishes the two was ignored.

**Correction (`house_helper/persistence/conversations.py::_first_question_subquery`).**
The placeholder is skipped only when the message has a corresponding
`message_attachments.message_id` row. The condition is a correlated `EXISTS` over the
existing unique index on `message_attachments.message_id` (one SQL statement, no per-row
query), added to the existing role/ownership/`expires_at > now` filters:

```
Message.content != :placeholder OR NOT EXISTS (attachment for this message)
```

Consequences: a literal standalone `[Фото]` is eligible and names the conversation; an
uncaptioned attached photo is skipped and the query continues to the first eligible user
text; an ordinary caption on an attached photo is eligible; retention and ownership
filters are unchanged.

**Known ambiguity, documented and not resolved by migration.** An uncaptioned photo and a
photo whose caption is exactly `[Фото]` have identical stored text and identical
attachment metadata, so they cannot be told apart. Both are skipped. No persisted
metadata in the current schema distinguishes them; no migration or message-content change
was introduced.

**Explanation wording.** The derived-title explanation now says the name is formed from the
beginning of the first available user message **containing text suitable for a title**.
This refines the D2 table's wording to the actual mechanism; the frozen Scope Lock was not
edited.

**Evidence.** New persistence tests cover: standalone literal `[Фото]`; uncaptioned attached
photo; ordinary caption; exact-placeholder caption; photo followed by ordinary text;
assistant-only history; empty history; retention-driven removal of the first eligible text
and of a skipped photo; rename/reset across those states; and unchanged history and
assistant-context content (the placeholder text and the photo remain in the history, and
the assistant is still told «Человек прислал фото без подписи.»). The tests run on SQLite
and PostgreSQL.

## 5. A1-07 — MINOR: preserve unrelated pending ORM state

**R1 shortcoming.** `Session.get(Conversation, id, populate_existing=True)` refreshed the
whole row after the bulk title `UPDATE`; an unrelated pending `updated_at` the caller had
set was discarded (reproduced on SQLite and PostgreSQL).

**Correction (`conversations.py`).** `_reconcile_loaded_title` now:

1. takes the identity-mapped `Conversation` (or leaves it alone if it is not loaded);
2. reads only `conversations.title` with one scalar `SELECT`, under `session.no_autoflush`;
3. sets it with `set_committed_value`, which does not mark it dirty and does not flush.

The bulk `UPDATE` and the summary read-back also run under `no_autoflush`, so the mutation
neither writes nor discards unrelated pending changes. No commit, expire-all or whole-row
refresh was added; the caller's transaction and rollback still govern persistence.
`updated_at` self-assignment and owner-scoped writes are unchanged.

**Evidence.** New persistence tests on SQLite and PostgreSQL: rename and reset with an
already-loaded conversation; a pending `updated_at` under `no_autoflush`; the same without
that guard; pending state on a separate ORM object; fresh title/origin in the returned
summary; unchanged persisted activity until the caller flushes its own change (verified by
attribute history and by an independent scalar read); the caller's rollback removing the
title. Tests inspect ORM attribute history/dirty state, not only displayed values.

## 6. Changed-file inventory

Production:

- `house_helper/core/title_unicode.py` (new)
- `house_helper/persistence/conversations.py`
- `house_helper/web/static/chat-logic.js`
- `house_helper/web/static/app.js`

Tests:

- `tests/test_persistence_conversations.py`
- `tests/test_web_chat_api.py`
- `tests/test_web_ui_logic.py`
- `tests/js/logic_scenarios.js`

Documentation:

- `docs/product/PP-01-A1_R2_Remediation_Record.md` (new)
- `docs/product/PP-01-A1_Implementation_Record.md`
- `docs/STATUS.md`
- `README.md` (derived-title wording)

No migration, dependency, prompt, provider, deploy or unrelated file was changed.

## 7. Verification evidence

Local Windows 11, Python 3.12.10, Node.js 24.14.0. No full 12,000-test run was repeated, per
the remediation instruction; verification is affected-area based.

### SQLite (no PostgreSQL variable set)

| Command / scope | Result |
|---|---|
| Affected-area run: persistence conversations, attachments, persistence Telegram, active conversations, chat API, UI logic, UI DOM, UI assets, web app, UI integration, Telegram dialogs, admin retention, FIX-01 identity/menu | **1540 passed** |
| `tests/test_persistence_conversations.py` | 141 passed |
| `tests/test_web_chat_api.py` | 186 passed |
| `tests/test_web_ui_logic.py` | 407 passed |
| `tests/test_web_ui_dom.py` | 194 passed |
| `tests/test_web_ui_assets.py` | 114 passed |
| `tests/test_web_ui.py` | 66 passed |
| `tests/test_web_ui_integration.py` | 24 passed |
| `tests/test_admin_retention.py` | 59 passed |
| FIX-01 preservation: `tests/test_assistant_identity.py`, `tests/test_menu_recommendations.py` | 39 passed (within the affected-area run) |

The new title scenarios are plain (non-`time_`) scenarios and therefore run in the
harness's UTC pass; the existing `time_` scenarios run in UTC, Europe/Moscow,
America/New_York and Asia/Kolkata. Every skip is reported by pytest; no test was skipped
for a missing dependency in this environment (Node.js was present).

### PostgreSQL 16.15 (disposable container)

| Command / scope | Result |
|---|---|
| `python -m tests.postgres_preflight` | `server_version_num=160015 host=127.0.0.1 port=55432 user=hh_test database=postgres` |
| `tests/test_persistence_conversations.py` (models-built and migrated schemas) | 141 passed |
| `tests/test_web_chat_api.py` (includes the commit-inversion reproduction) | 186 passed |
| `tests/test_chat_postgres.py` + `tests/test_persistence_attachments.py` | 80 passed |

### PostgreSQL environment and cleanup

- Container `house-helper-test-postgres` (image `721873c34ceb`, PostgreSQL 16.15), bound
  only to `127.0.0.1:55432`, dedicated volume `house-helper-test-postgres-data` and
  network. It was `Exited (0)` before use and `Exited (0)` afterwards.
- Preflight matched the documented cluster (`160015`, loopback, non-5432).
- Inventory **before** the runs: databases `postgres`, `template0`, `template1`; only the
  bootstrap role `hh_test` (OID 10).
- Inventory **after** the runs: identical — no non-system database and no additional role.
  The harness's disposable databases and per-run roles were removed by its own cleanup.
- No Production database, credential or real user data was used. Native PostgreSQL on
  port 5432 was not touched.

## 8. Remaining limitations and Owner decisions

1. **Title-write guard recovery.** After a genuinely indeterminate transport outcome the
   conversation's title writes stay guarded until the page is reloaded; there is no
   in-session reconciliation, because the existing architecture has no server-side
   operation sequence and the restrictions forbid adding one (no migration/journal).
   Reloading shows the stored title. This is the precise architectural limitation. If the
   Owner wants in-session recovery, the minimal alternative is a future server-side
   per-conversation title sequence (a schema change) that lets the server reject an
   out-of-order write; that is explicitly out of R2 scope and is not implemented.
2. Ordering across page reloads and between independent tabs is not guaranteed (unchanged
   from the product's existing concurrency semantics).
3. The A1-05 uncaptioned-photo versus exact-`[Фото]`-caption ambiguity is documented and
   intentionally unresolved without a migration.
4. The A1-04 policy needs an explicit table update on a future Unicode/emoji revision; the
   current data is Unicode 17.0.
5. No real browser, real Telegram client or deployment was exercised.

## 9. Compliance confirmation

- No database migration, provider call, system-prompt change, FIX-01 change, research
  execution or unrelated refactoring.
- No Production access, no infrastructure change, no Git mutation (no commit, push, stash,
  reset or checkout).
- The frozen Scope Lock (D1–D7) was not edited; the A1-05 explanation wording is an
  implementation correction required by the audit.
- Findings are **not** marked independently closed; PP-01-A1 is not Owner-accepted.
- The FIX-01 behavioral baseline remains **24 PASS / 6 FAIL**.

**Verdict: ALL FOUR REMEDIATIONS IMPLEMENTED — READY FOR TARGETED CODEX RE-AUDIT**
