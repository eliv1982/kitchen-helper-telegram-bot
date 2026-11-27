# House Helper — PP-01-A1-R1 Audit Remediation
## Remediation Record

| Field | Value |
|---|---|
| Date | 2026-11-26 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7, unchanged) |
| Product specification | [`PP-01_UX_Product_Specification_v1.0.md`](PP-01_UX_Product_Specification_v1.0.md) |
| Implementation record | [`PP-01-A1_Implementation_Record.md`](PP-01-A1_Implementation_Record.md) |
| Audit source | PP-01-A1 Independent Implementation Audit (Codex), findings A1-01…A1-08 |
| Status | **Remediation implemented in the working tree; pending independent Codex re-verification. Not committed, Owner-accepted, released or deployed.** |

## 1. Scope of this record

This record covers only the eight accepted audit findings. The PP-01-A1 implementation
remains uncommitted in the working tree on baseline `def03ea`. No product decision
(D1–D7) was reopened, no database migration was added, no provider was called, no
system prompt or FIX-01 behavior was changed, and no Git, Production or infrastructure
operation was performed.

The original audit findings and the original implementation evidence remain part of
the historical record; nothing below rewrites them as if the defects never existed.

## 2. A1-01 — BLOCKER: stale asynchronous Web title state

| Field | Detail |
|---|---|
| Severity | BLOCKER |
| Status | Remediation implemented, pending independent re-verification |

**Root cause.** Title mutations were guarded only by the global session `epoch`.
Response handling was not scoped to the conversation or the editor instance that sent
the write, and there was no per-conversation identity, supersession or ordering:

- `forgetConversation(id)` cleared whatever title editor happened to be open, so a
  delayed 404 for conversation A closed conversation B's editor and dropped its draft.
- A successful response for the open conversation cleared `state.titleEdit`
  unconditionally and showed its banner, even when the editor on screen belonged to a
  newer rename.
- A response replaced the conversation's list item whether or not a newer mutation for
  the same conversation had already been applied. Two overlapping writes could also
  commit on the server in an order different from the page's issue order, so ignoring a
  response alone would have masked inconsistent persisted state.

**Implemented correction.** In `house_helper/web/static/chat-logic.js`:

- `titleSeq[id]` numbers every title write per conversation; `titleApplied[id]` records
  the newest response applied. A response older than one already applied is ignored.
- `titleQueues[id]` serializes title writes of one conversation. The next write starts
  only after the previous one settles, whatever its answer, so the server commits one
  conversation's writes in the order the page issued them. The mechanism is local to the
  title controls; no application-wide concurrency layer was introduced.
- Cleanup is scoped: `forgetConversation(id)` touches the title controls only when the
  conversation that disappeared is the one that is open.
- A success refreshes that conversation's own list item even when it is not open, but
  closes an editor only when `state.titleEdit` is the very edit that sent the write.
- A failure is shown only to the editor that sent it (or, for reset, on the conversation
  that is open); a stale error never lands on another editor.
- Unsaved drafts are never cleared by an unrelated response.

**Changed files.** `house_helper/web/static/chat-logic.js`; regression scenarios in
`tests/js/logic_scenarios.js`; the existing static-asset body scan was adjusted in
`tests/test_web_ui_assets.py` for the shared request helper.

**Regression tests.** Four deterministic deferred-response scenarios:

- `a_delayed_404_for_one_conversation_does_not_clear_another_conversations_draft`
  (audit scenario A);
- `a_delayed_reset_cannot_replace_a_newer_rename_or_close_its_editor`
  (audit scenario B, with the serialized second write);
- `a_delayed_rename_updates_its_own_list_item_without_touching_another_conversation`;
- `a_delayed_failure_for_one_conversation_is_not_shown_on_another`.

**Exact results.** `tests/test_web_ui_logic.py`: **396 passed**; `tests/test_web_ui_dom.py`:
**194 passed**; `tests/test_web_ui_assets.py`: **114 passed**.

**Known limitations.** The queue is in memory and per controller instance. Two browser
tabs are two controllers, so this mechanism does not order a write made in one tab
against a write made in the other; the server remains authoritative and a later list
read reconciles the display. No real-browser concurrency test was run.

## 3. A1-02 — IMPORTANT: explicit Origin requirement

| Field | Detail |
|---|---|
| Severity | IMPORTANT |
| Status | Remediation implemented, pending independent re-verification |

**Root cause.** `PATCH /api/conversations/{id}` used the shared
`authenticated_mutation`, which accepts a request with no `Origin` when `Sec-Fetch-Site`
is `same-origin` or absent. The A1 Scope Lock promises that PATCH is refused without the
application's Origin.

**Implemented correction.** `house_helper/web/chat_api.py` gains
`authenticated_title_mutation`, used only by the rename route. It requires exactly one
`Origin` header equal to `auth.settings.origin` (read with `getlist`, so repeated
headers are seen rather than collapsed) and answers `forbidden_origin` otherwise.
`Sec-Fetch-Site` is never consulted as a substitute. Other mutations keep their existing
Origin contract, and authentication/account isolation are unchanged.

**Changed files.** `house_helper/web/chat_api.py`; `tests/test_web_chat_api.py`.

**Regression tests.** Valid matching Origin; missing Origin; missing Origin with
`Sec-Fetch-Site: same-origin`; missing Origin with `Sec-Fetch-Site: none`; `Origin: null`;
foreign and prefixed-foreign Origin; duplicate matching Origins; conflicting duplicate
Origins in both orders. The existing cross-site and foreign-Origin tests still pass.

**Exact results.** `tests/test_web_chat_api.py`: **184 passed** (within the
affected-area run).

## 4. A1-03 — IMPORTANT: latest available message timestamp

| Field | Detail |
|---|---|
| Severity | IMPORTANT |
| Status | Remediation implemented, pending independent re-verification |

**Root cause.** `_last_message_at_subquery` selected `Message.created_at` ordered by
`Message.id DESC LIMIT 1`. A message id follows insertion order, not time, so a later
row carrying an earlier instant produced the wrong latest-message value. The value is
the shared ordering key (`activity_at`) of the Web list, the Telegram dialogs list and
Telegram's latest-conversation resolution.

**Implemented correction.** The subquery now selects `max(Message.created_at)` with the
same role, ownership-through-correlation and `expires_at > now` filters. The fallback to
`Conversation.created_at` for a conversation with no available message, the total
tie-break by `id DESC`, pagination and message rendering order are unchanged.

**Changed files.** `house_helper/persistence/conversations.py`; tests in
`tests/test_persistence_conversations.py`.

**Regression tests.**
`test_the_latest_instant_is_the_greatest_created_at_not_the_greatest_id` inserts two
eligible messages whose insertion order is the reverse of their timestamps, an expired
message with the greatest instant of all, and a second conversation in between. It
verifies the greater eligible timestamp, the resulting list order, the expired row's
exclusion, and that `persistence.telegram.latest_conversation_id` resolves the same
conversation.

**Exact results.** SQLite: **113 passed** for `tests/test_persistence_conversations.py`
(models-built and migration-built schemas). PostgreSQL 16.15: **113 passed** for the
same file. Affected Telegram suites: **247 passed**.

**Query-plan assessment (proportionate, no migration).** SQLite
`EXPLAIN QUERY PLAN` for the list statement uses
`ix_conversations_user_id_updated_at (user_id=?)`, then the correlated scalar subquery
uses `ix_messages_conversation_id_id (conversation_id=?)`, then a temporary B-tree for
the total order. The aggregate reads the available messages of each conversation; the
existing `(conversation_id, id)` index still narrows the scan. No new index or migration
was added. A future very large per-conversation history may justify a
`(conversation_id, created_at)` index, but that was not introduced speculatively.

## 5. A1-04 — IMPORTANT: invisible titles and forbidden separators

| Field | Detail |
|---|---|
| Severity | IMPORTANT |
| Status | Remediation implemented, pending independent re-verification |

**Root cause.** `normalize_conversation_title` trimmed with `str.strip()` before scanning
for forbidden characters. `str.strip()` treats newline, CR, U+2028 and U+2029 as
whitespace, so a title ending in a newline or beginning with U+2028 was silently
"repaired" instead of refused. The two emoji format characters (ZWJ and the tag
characters) were allowed anywhere, so a standalone U+200D or a tag-only string passed.

**Implemented correction.** Both validators now apply a narrow, documented contract:

1. Forbidden characters are refused before trimming: controls (`Cc`), unpaired
   surrogates (`Cs`), U+2028/U+2029 (`Zl`/`Zp`), and every `Cf` except U+200D and
   U+E0020–U+E007F.
2. After trimming, the permitted `Cf` characters must be well placed:
   - U+200D only between two emoji symbols (`So`), skipping an intervening variation
     selector (U+FE0E/U+FE0F) and skin-tone modifiers (U+1F3FB–U+1F3FF) on the left;
   - U+E0020–U+E007F only in a well-formed subdivision-flag tag sequence:
     U+1F3F4, one or more tag characters, then U+E007F.
3. Ordinary leading/trailing whitespace is still trimmed; empty and over-200-code-point
   titles are still refused; the 200-code-point limit is unchanged.

The Web validator in `house_helper/web/static/chat-logic.js` mirrors the same algorithm
over code points.

**Changed files.** `house_helper/persistence/conversations.py`;
`house_helper/web/static/chat-logic.js`; `tests/test_persistence_conversations.py`;
`tests/test_web_ui_logic.py`.

**Regression tests.** Shared boundary cases in the Python validator and in the
Python-vs-JavaScript cross-check: standalone ZWJ, joiner between Latin letters, leading
and trailing joiner, double joiner, tag-only strings, a lone tag character, tag
characters in ordinary text, an incomplete tag sequence, the end tag without a tag
specification, trailing/leading CR and LF, U+2028 and U+2029 at either end, BOM and
other format characters, unpaired surrogates, permitted ordinary whitespace trimming,
the family/ profession/ skin-tone/ variation-selector/ subdivision-flag sequences,
Cyrillic and Latin text, combining marks, and exactly 200 versus 201 code points.

**Exact results.** `tests/test_persistence_conversations.py`: **113 passed** on SQLite
and **113 passed** on PostgreSQL 16.15; `tests/test_web_ui_logic.py`: **396 passed**,
including the generated-title cross-check.

**Intentional Unicode policy and limitations.** The contract is intentionally narrower
than UAX #29 grapheme segmentation. It does not implement a general emoji-sequence
parser. Consequences:

- A legitimate emoji sequence whose right-hand base is not category `So`, or whose
  structure falls outside the ZWJ/tag rules above, is refused.
- Tag sequences are accepted only with the U+1F3F4 black-flag base.
- The rules use Python 3.12's Unicode 15.0 `unicodedata` on the server and the
  JavaScript engine's Unicode tables in the page. They agree on the tested strings; a
  future Unicode revision that assigns new `So` or `Cf` characters would need review.
- A person literally typing the stored photo placeholder text is treated the same as
  an uncaptioned photo (see A1-05).

## 6. A1-05 — IMPORTANT: truthful title-origin explanation

| Field | Detail |
|---|---|
| Severity | IMPORTANT |
| Status | Remediation implemented, pending independent re-verification |

**Root cause.** The Web explanation had only three states and mapped "no title" to «в
диалоге нет доступных сообщений» even when the conversation still had retained messages
(an assistant-only history or an uncaptioned photo). In addition, the derived-title
query returned the stored photo placeholder (`[Фото]`) as if it were user text, so the
uncaptioned-photo case could not reach the "no eligible text" state at all.

**Implemented correction.**

- The derived-title subquery excludes user messages whose content is
  `attachments.PHOTO_PLACEHOLDER`; assistant-only histories were already excluded by the
  `role = user` filter. The derived-title shortening algorithm itself is unchanged.
- The page adds `TEXT.titleOriginNoText` («В доступных сообщениях нет текста
  пользователя, на основе которого можно сформировать название») and selects it when
  `title_origin` is `none` and `last_message_at` is not null. With no available message,
  the existing empty-history sentence is shown.

**Changed files.** `house_helper/persistence/conversations.py`;
`house_helper/web/static/chat-logic.js`; `tests/test_persistence_conversations.py`;
`tests/js/logic_scenarios.js`.

**Regression tests.** `test_messages_without_eligible_user_text_have_no_derived_title`
(assistant-only, uncaptioned-photo placeholder, ordinary user text),
`test_message_expiry_moves_a_derived_title_to_no_title` (retention transition), and
`the_title_origin_distinguishes_an_empty_conversation_from_one_without_user_text`.

**Exact results.** Included in the persistence and UI-logic counts above.

**Known limitation.** The exclusion is by the stored placeholder text, not by the
presence of an attachment row, so a person who literally writes «[Фото]» as a caption
also has no derived title. This is treated as the same "no eligible user text" state and
is truthful about the mechanism.

## 7. A1-06 — MINOR: missing Moscow timezone label

| Field | Detail |
|---|---|
| Severity | MINOR |
| Status | Remediation implemented, pending independent re-verification |

**Root cause.** The untitled-conversation fallback was built from `moscowDate` and
`moscowClock` separately and omitted the МСК label.

**Implemented correction.** `conversationLabel` now uses `moscowMoment`, producing
«Разговор от ДД.ММ.ГГГГ, ЧЧ:ММ МСК». The same label feeds the Web header and the
sidebar, so both carry the label. Timezone conversion and date formatting are unchanged.

**Changed files.** `house_helper/web/static/chat-logic.js`; assertions in
`tests/js/logic_scenarios.js` and `tests/js/dom_scenarios.js`.

**Exact results.** Included in `tests/test_web_ui_logic.py` (**396 passed**) and
`tests/test_web_ui_dom.py` (**194 passed**).

## 8. A1-07 — MINOR: stale ORM readback after title mutation

| Field | Detail |
|---|---|
| Severity | MINOR |
| Status | Remediation implemented, pending independent re-verification |

**Root cause.** `set_conversation_title`/`reset_conversation_title` update the row with
`sa.update(...).execution_options(synchronize_session=False)`, then read the summary
through a normal ORM select. If the `Conversation` was already in the session's identity
map, the select returned the cached instance with the pre-UPDATE title.

**Implemented correction.** After the UPDATE (and its row-count/ownership check), the
mutation calls `Session.get(Conversation, id, populate_existing=True)`. That refreshes
this one row from the database, so the returned summary and the loaded object carry the
persisted title. No commit was added, the transaction boundary is unchanged, and no
broad session invalidation is used.

**Changed files.** `house_helper/persistence/conversations.py`; tests in
`tests/test_persistence_conversations.py`.

**Regression tests.**
`test_a_rename_returns_the_stored_title_when_the_conversation_is_already_loaded` and
`test_a_reset_returns_the_derived_title_when_the_conversation_is_already_loaded` load
the entity before mutating, then verify the returned manual/derived title, title origin,
reset availability, the refreshed entity, the persisted row and unchanged activity
metadata.

**Exact results.** SQLite **113 passed**; PostgreSQL 16.15 **113 passed**.

## 9. A1-08 — MINOR: user-facing retention documentation and test accuracy

| Field | Detail |
|---|---|
| Severity | MINOR |
| Status | Remediation implemented, pending independent re-verification |

**Root cause.** The user-facing history documentation did not describe manual titles,
their lifetime or their interaction with retention. The retention test seeded the title
with direct SQL instead of the application writer, and one persistence test was named as
a "retention purge" although it exercised only the expiry-filtered read.

**Implemented correction.**

- `README.md` gains a «Названия разговоров» paragraph: derived versus manual titles,
  rename/reset, lifetime until reset or conversation deletion, survival of individual
  message expiry, no retention extension from renaming, deletion with the parent
  conversation, and the title's exclusion from the assistant context.
- The «Ваши данные» disclosure gains `profileDataTitles` with the same facts in Russian,
  rendered by `house_helper/web/static/app.js`.
- `tests/test_admin_retention.py` now writes the title through
  `persistence.conversations.set_conversation_title`. The old empty-conversation purge
  test keeps its assertion that the titled row is deleted. A new
  `test_a_manual_title_survives_a_partial_message_purge_and_reset_uses_the_surviving_text`
  performs a real physical purge: the expired user message is deleted, the newer
  exchange and the conversation stay, the manual title survives, reset derives the name
  from the surviving user text, and no title value is left behind.
- The misleading persistence test is renamed
  `test_after_message_expiry_reads_hide_the_messages_and_keep_the_title`, with a comment
  distinguishing the expiry-filtered read from the physical purge.
- `tests/test_web_ui_logic.py` asserts the new disclosure's wording.

**Changed files.** `README.md`; `house_helper/web/static/chat-logic.js`;
`house_helper/web/static/app.js`; `tests/test_admin_retention.py`;
`tests/test_persistence_conversations.py`; `tests/test_web_ui_logic.py`.

**Exact results.** `tests/test_admin_retention.py`: **59 passed** (within the
affected-area run); `tests/test_web_ui_logic.py`: **396 passed**.

## 10. Consolidated verification

All runs were local on Windows 11, Python 3.12.10, Node 24.14.0. No full 12,000-test run
was repeated, per the remediation instruction; affected-area verification was used.

| Command / scope | Result |
|---|---|
| Affected-area run: persistence conversations, admin retention, UI logic, UI DOM, chat API, persistence Telegram, Telegram dialogs, UI assets, web app, Telegram active conversations, UI integration | **1365 passed** |
| `tests/test_persistence_conversations.py` (SQLite, models-built + migration-built) | 113 passed |
| `tests/test_persistence_conversations.py` (PostgreSQL 16.15, models-built + migration-built) | 113 passed |
| `tests/test_web_ui_logic.py` | 396 passed |
| `tests/test_web_ui_dom.py` | 194 passed |
| `tests/test_web_chat_api.py` | 184 passed |
| `tests/test_admin_retention.py` | 59 passed |
| Telegram suites (persistence Telegram, dialogs, active conversations) | 247 passed |
| UI integration (Node against the real app over loopback) | 24 passed |
| FIX-01 preservation: `tests/test_assistant_identity.py`, `tests/test_menu_recommendations.py` | 39 passed |

### PostgreSQL environment and cleanup

- Container used: `house-helper-test-postgres` (image `721873c34ceb`, PostgreSQL 16.15),
  bound only to `127.0.0.1:55432`, dedicated volume
  `house-helper-test-postgres-data`. It was stopped before use and started only for this
  run.
- `python -m tests.postgres_preflight` reported
  `server_version_num=160015 host=127.0.0.1 port=55432 user=hh_test database=postgres`.
- After the run, the server reported **no** non-system databases and only the `hh_test`
  role. The container was stopped again (`Exited (0)`), restoring its original state.
- No Production database, Production credentials or real user data was used.

## 11. Remaining limitations and Owner smoke tests

- No real browser, real Telegram client or deployed instance was exercised.
- The Unicode contract limitations in section 5 apply.
- The title-write serialization is per page instance; two tabs are independent.
- Suggested Owner smoke tests after deployment: rename conversation A in one tab, switch
  to B while A's request is slow (throttled network), confirm B's draft survives; rename
  A twice quickly and confirm the last title wins; reset a title, then rename, and
  confirm the last action is shown; try an invisible-only title and a trailing newline
  and confirm the Russian refusal; check the header, sidebar and Telegram dialogs carry
  the same title and МСК dates; wait past a message expiry (or use a test clock) and
  confirm the origin explanation distinguishes an empty history from one whose remaining
  messages have no user text.

## 12. Compliance confirmation

- No database migration, provider call, system-prompt change, FIX-01 change, research
  execution or unrelated refactoring was performed.
- No Production access or change, no infrastructure work, no Git mutation (no commit,
  no push, no stash).
- The frozen Scope Lock (D1–D7) was not amended. The A1-05 wording and the
  uncaptioned-photo exclusion are implementation corrections required by the audit, not
  product-decision changes.
- Findings are **not** marked independently closed and PP-01-A1 is **not** Owner
  accepted.

**Verdict: ALL EIGHT REMEDIATIONS IMPLEMENTED — READY FOR CODEX RE-AUDIT**
