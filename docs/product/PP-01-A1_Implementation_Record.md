# House Helper — PP-01-A1 Conversation Navigation
## Implementation Record

| Field | Value |
|---|---|
| Date | 2026-11-26 |
| Baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` |
| Scope authority | [`PP-01-A1_Scope_Lock_v1.0.md`](PP-01-A1_Scope_Lock_v1.0.md) (D1–D7) |
| Product specification | [`PP-01_UX_Product_Specification_v1.0.md`](PP-01_UX_Product_Specification_v1.0.md) |
| Technical starting point | [`PP-01_Technical_Assessment_Record.md`](../architecture/PP-01_Technical_Assessment_Record.md) |
| Release candidate intent | **HH-BRAND-01-FIX-01 + PP-01-A1** |
| Final independent acceptance | **PASS — ALL EIGHT FINDINGS CLOSED** (2026-11-27); **17 PASS / 0 FAIL / 0 SKIP**; A1-01 closed with C1 desktop focus verified |
| Owner acceptance | **ACCEPT WITH NON-BLOCKING NOTES** (2026-11-27); see [`PP-01-A1_Owner_Acceptance_Closure_Record.md`](PP-01-A1_Owner_Acceptance_Closure_Record.md) |
| Status | Committed as `f55e089cf1397adbb32aedde5c3a4878831c7d79`; independently accepted in full and Owner-accepted with non-blocking notes. The PP-01-A1-CI-FIX-01 correction (§18) is in the working tree and **READY FOR OWNER COMMIT**; the full CI matrix is pending. The intermediate `OPEN` statements in sections 12–16 are chronology, not the final disposition. |

## 1. Executive summary

PP-01-A1 adds a practical conversation-navigation increment without changing the
assistant's conversational intelligence:

- a validated, persisted, owner-scoped manual title on the existing
  `conversations.title` column, with an authoritative reset that restores the current
  derived (or missing) title;
- truthful title-origin explanations in the Web header menu;
- absolute conversation creation and latest-available-message times in Europe/Moscow,
  with the list ordered by the latest available message rather than a stale
  `updated_at`;
- absolute Moscow calendar-day separators and `ЧЧ:ММ` message times in the Web history;
- the same stored titles, ordering and dates in Telegram's `/dialogs` navigation.

No database migration, provider call, prompt change or production change was made.
HH-BRAND-01-FIX-01 is preserved in full.

## 2. Technical approach

### 2.1 Persistence (`house_helper/persistence/conversations.py`)

`ConversationSummary` gained:

- `last_message_at`: the `created_at` of the newest available message
  (`expires_at > now`, role `user` or `assistant`), or `None`;
- `title_origin`: `"manual"`, `"derived"` or `"none"`;
- `can_reset_title`: true exactly when a stored user title exists.

`_summary` resolves the display title in this order: a non-blank stored
`conversations.title` (manual), else `conversation_title(first available user
message)`, else `None` (the page falls back to the creation date).

New functions:

- `normalize_conversation_title(raw)` — the authoritative D1 rule: `str.strip`,
  non-empty, ≤ 200 code points, no `Cc`, `Cs`, `Zl`, `Zp`, no `Cf` except U+200D and
  U+E0020–U+E007F. Raises `InvalidConversationTitle` with reason `empty`, `too_long` or
  `characters`.
- `set_conversation_title(...)` and `reset_conversation_title(...)` — one
  `UPDATE ... WHERE id AND user_id`, with `updated_at=Conversation.updated_at` so the
  rename is not activity and cannot extend purge eligibility. They return the summary
  read back in the same transaction.
- `_last_message_at_subquery(now)` and `activity_at(now)` — the shared ordering key:
  `coalesce(latest available message instant, conversation.created_at)`.

`list_conversations` and `conversation_summary` use that key. `list_conversations`
orders by `activity_at(now).desc(), id.desc()` and selects the latest-message instant for
display. The existing total order is preserved, so offset pagination is stable.

`house_helper/persistence/telegram.py` now uses the same `activity_at(now)` in
`latest_conversation_id`, so Telegram selects the same "latest" conversation as the
website.

### 2.2 Web API (`house_helper/web/chat_api.py`, `diagnostics.py`)

New route:

```
PATCH /api/conversations/{id}
body {"title": "<text>"}  -> 200, the conversation as the list shows it
body {"title": null}      -> 200, the stored title removed, derived/fallback restored
```

- The session user is the only owner; a foreign, unknown, deleted or malformed id is
  the same 404 `conversation_not_found`.
- The mutation requires the application's Origin (`authenticated_mutation`), JSON, and
  exactly the field `title`; anything else is `invalid_request`.
- Validation failures answer 400 with new catalogue codes:
  `empty_title`, `title_too_long`, `invalid_title`, with Russian fixed text.
- The answer carries `id`, `title`, `title_origin`, `can_reset_title`, `created_at`,
  `updated_at`, `last_message_at`.
- `GET /api/conversations` and `POST /api/conversations` now include the same new
  fields; existing fields are unchanged.

### 2.3 Web page (`house_helper/web/static/chat-logic.js`, `app.js`, `app.css`, `ui.py`)

- `normalizeConversation` accepts and validates the new fields; older shapes default to
  a derived/none origin.
- `prepareConversationTitle` mirrors the backend rule using Unicode property escapes and
  the two allowed `Cf` kinds; `tests/test_web_ui_logic.py` cross-checks the two.
- Moscow presentation is independent of the viewer's zone:
  `moscowParts` / `moscowDate` / `moscowClock` / `moscowMoment` / `moscowDayKey` use
  `Intl.DateTimeFormat` with `timeZone: "Europe/Moscow"` and Latin digits.
- The list item now carries `createdText`
  (`Создан: ДД.ММ.ГГГГ, ЧЧ:ММ МСК`) and `lastText`
  (`Последнее сообщение: … МСК` or `Последнее сообщение: Нет сообщений`).
- The header menu gains «Переименовать», «Как создано название» and
  «Сбросить название». Reset is hidden unless `can_reset_title`. The rename form and
  the origin explanation are the two new regions under the header
  (`#chat-title-edit`, `#chat-title-info`), with keyboard operation, focus management,
  cancellation and visible Russian errors.
- A successful rename/reset replaces the list item in place from the server's answer;
  it is never re-sorted locally.
- `buildTimeline` inserts absolute Moscow day separators before the first dated entry of
  each day and whenever the day changes. The separator carries `ДД.ММ.ГГГГ` and «МСК».
  Message times are `ЧЧ:ММ` in Moscow; a message with no authoritative timestamp gets
  neither.
- Existing message, recipe, favorite, shopping, text-size and mobile behavior is
  untouched.

### 2.4 Telegram (`house_helper/web/telegram_chat.py`)

- `_moscow_time` now formats `%d.%m.%Y, %H:%M` + «МСК».
- `/dialogs` shows, in the message text, each conversation's title, creation instant and
  latest-available-message instant (or «Нет сообщений»); the button carries the number
  or check mark and the title. The approved dialogs hint is unchanged; each displayed
  date carries its own «МСК» label.
- Ordering is the shared `list_conversations` order. A stored Web title appears on the
  next `/dialogs` read. No new callback, command or history replay exists.

## 3. Changed-file inventory

Production code:

- `house_helper/persistence/conversations.py`
- `house_helper/persistence/models.py` (docstring only: the ordering claim)
- `house_helper/persistence/telegram.py`
- `house_helper/web/chat_api.py`
- `house_helper/web/diagnostics.py`
- `house_helper/web/telegram_chat.py`
- `house_helper/web/ui.py`
- `house_helper/web/static/chat-logic.js`
- `house_helper/web/static/app.js`
- `house_helper/web/static/app.css`

Tests:

- `tests/test_persistence_conversations.py`
- `tests/test_persistence_telegram.py`
- `tests/test_web_chat_api.py`
- `tests/test_web_ui.py`
- `tests/test_web_ui_logic.py`
- `tests/test_web_ui_assets.py`
- `tests/test_web_app.py`
- `tests/test_telegram_dialogs.py`
- `tests/test_admin_retention.py`
- `tests/test_persistence_telegram_active_conversations.py` (the empty-conversation ordering expectation)
- `tests/js/harness.js`
- `tests/js/logic_scenarios.js`
- `tests/js/crosscheck.js`
- `tests/js/dom_scenarios.js`
- `tests/js/integration_journeys.js`

Documentation:

- `docs/product/PP-01-A1_Scope_Lock_v1.0.md` (new)
- `docs/product/PP-01-A1_Implementation_Record.md` (new)
- `docs/architecture/PP-01_Technical_Assessment_Record.md` (new)
- `docs/DECISIONS.md` (KHD-54)
- `docs/STATUS.md` (PP-01-A1 working-tree entry)

No migration file was added or changed. No dependency was added.

## 4. API and persistence implications

- No schema change: `conversations.title VARCHAR(200) NULL` and its 200-code-point CHECK
  already exist from migration `0001_persistence_foundation`.
- The stored title is the only new durable state. It is deleted with its conversation by
  the existing `ON DELETE CASCADE`/row deletion.
- The list's ordering key changes from `updated_at` to the latest available message
  (creation time fallback). This is a read-side behavior change only; no data is
  rewritten. Consumers are the Web list, the Telegram dialogs list and Telegram's
  latest-conversation resolution, all updated together.
- `updated_at` remains in the API for compatibility, but is no longer the activity shown
  or sorted on.

## 5. Retention and KHD-34 compatibility

| Question | Finding |
|---|---|
| Does a manual title change message retention? | No. Messages still expire individually 365 days after `created_at`; the title is a separate column. |
| Does a rename count as activity? | No. `updated_at` is assigned to itself, verified by persistence and retention tests. |
| Can a title prevent purge of an eligible conversation? | No. The purge requires old `updated_at`/`created_at` and no messages/requests; the title adds neither. |
| Is the title removed when the conversation is deleted? | Yes. It is a column of the deleted row; the retention test asserts the row and title are gone. |
| Does a title survive partial message retention while the conversation survives? | Yes, as approved: it remains until reset or deletion. |
| Does reset fabricate an original title? | No. It returns the current derived title or none; no archive exists. |
| Can a title reach the assistant? | No. `build_context` never reads it; a persistence test asserts the title is absent from the context. |

## 6. Cross-channel behavior

- Web rename/reset is authoritative and visible on the next Web reload and the next
  Telegram `/dialogs` read.
- Telegram displays the same stored/derived title and the same two dates.
- Both channels use the shared `ConversationSummary` and shared ordering expression.
- Telegram rename/reset/explanation commands are deferred, as approved.

## 7. Test evidence

Focused and affected-area commands were run locally on Windows 11, Python 3.12.10,
Node 24.14.0:

| Command | Result |
|---|---|
| Affected-area run (persistence, Telegram dialogs, chat API, UI logic, DOM, assets, integration, app, retention) | **1255 passed** |
| `python -m pytest -q tests/test_persistence_conversations.py` | 77 passed |
| `python -m pytest -q tests/test_persistence_telegram.py` | 139 passed |
| `python -m pytest -q tests/test_web_chat_api.py` | 175 passed |
| `python -m pytest -q tests/test_telegram_dialogs.py` | 49 passed (includes the Web-rename cross-channel and message-length tests) |
| `python -m pytest -q tests/test_web_ui_logic.py` | 391 passed |
| `python -m pytest -q tests/test_web_ui_dom.py` | 194 passed |
| `python -m pytest -q tests/test_web_ui_assets.py` | 114 passed |
| `python -m pytest -q tests/test_web_ui_integration.py` | 24 passed |
| `python -m pytest -q tests/test_admin_retention.py -k manual_title` | 1 passed |
| `python -m pytest -q tests/test_assistant_identity.py tests/test_menu_recommendations.py` | 39 passed (FIX-01 prompt/identity and recommendation copy unchanged) |
| `python -m pytest -q tests/test_persistence_telegram_active_conversations.py` | 59 passed (empty conversations ordered by creation time) |

The final full offline suite result is recorded in section 7.1. Node-backed tests are
skipped if `node` is absent; in this environment Node was present and they ran.

### 7.1 Full offline suite

`python -m pytest -q -k "not test_the_gate_refuses_a_file_name_backup_sh_would_not_have_made"`
on the final tree returned:

- **12102 passed, 547 skipped, 12 deselected, 59 warnings, in 31:30.**

The 12 deselected tests are one deploy-script parametrization
(`test_the_gate_refuses_a_file_name_backup_sh_would_not_have_made…`) that hangs in this
Windows/Git-Bash environment inside `deploy/upgrade.sh`; it is unrelated to PP-01-A1 and
was not classified PASS. The 59 warnings are pre-existing `pytest.mark.asyncio` notices
in `tests/test_telegram_cross_owner.py` (sync tests under a module-level async mark); none
is produced by this increment. An earlier run exposed four tests in
`tests/test_persistence_telegram_active_conversations.py` that still told empty
conversations apart by `updated_at`; they were corrected to distinct creation times (the
approved ordering), and the final run above is green.

## 8. New regression coverage

Title validation: ordinary Russian/English, emoji including ZWJ family and subdivision
flag, exactly 200 and 201 code points, trimming, empty/whitespace, controls, line
breaks, bidi controls, BOM, unpaired surrogates, duplicates and the safe rendering of
HTML/Telegram punctuation.

Title lifecycle: derived title unchanged, manual rename persists over reload, shared
with Telegram, reset restores the derived/fallback title, repeated rename, reset of an
already-reset conversation, empty conversation, truthful origin explanation.

Authorization/integrity: unauthenticated PATCH, foreign origin, cross-account rename and
reset, nonexistent conversation, no changes to messages/turns/other objects, no
reordering.

Chronology: creation stable, rename/reset do not reorder, latest available message
determines activity, stale `updated_at` ignored, total order for ties, empty fallback,
pagination, retention-expired messages.

Timezone/presentation: Moscow midnight boundary (21:00 UTC), absolute formatting,
explicit МСК, Web day separators and message times, incremental history without
duplicate separators, cross-channel consistency.

Retention: unchanged message expiry, title survives while the conversation survives,
title deleted with the conversation, rename does not extend eligibility.

## 9. Known limitations

- The Web form has no live character counter. The field's label states the 200-code-point
  limit, and both validators enforce it; a title over the limit is refused with a visible
  error and is never truncated.
- Telegram shows the dates in the message text rather than on each button, because a
  Telegram inline-button label cannot hold both dates within its size bound. The full
  legal title (up to 200 code points) is shown verbatim in the message text; only the
  button uses a smaller, channel-native bound and collapses internal whitespace to one
  line.
- Telegram rename/reset/explanation commands are deferred.
- The Moscow conversion relies on the runtime's IANA tz database (`Intl` in the browser,
  `zoneinfo` on the server), with the existing fixed +03:00 server fallback.
- No real browser, real Telegram client or live deployment was used. DOM checks are the
  existing mini-DOM scenarios, not visual or layout proof.
- While the first message of an empty conversation is in flight, the sidebar can briefly
  show «Нет сообщений» beside the activity note; the server has not yet returned an
  authoritative latest-message instant, and the value corrects when the turn completes or
  the list is read.
- The title limit and character policy are implemented against Unicode 15.0 categories
  (Python 3.12); a future Unicode revision that assigns new format characters would
  require an explicit review.

## 10. Requirement-level completion

| Requirement | Status |
|---|---|
| D1 title validation | Implemented, tested |
| D2 title management (rename, origin, reset) | Implemented, tested |
| D3 retention/privacy compatibility | Verified, tested |
| D4 timestamps and ordering | Implemented, tested |
| D5 Moscow presentation | Implemented, tested |
| D6 Web day separators/message times | Implemented, tested |
| D7 cross-channel consistency | Implemented for titles/ordering/dates; Telegram management deferred as approved |
| PP-01.1B manual renaming | Implemented (A1) |
| PP-01.1C title-formation explanation | Implemented (A1) |
| PP-01.1A semantic titles | Not implemented (deferred) |
| Full PP-01.1D original-title restore | Not implemented (deferred; no stored original title exists) |
| Database migration | Not required, none added |
| FIX-01 preservation | Unchanged |

## 11. Remaining Owner acceptance requirements

- Independent Codex audit.
- Owner review of this record and the Scope Lock.
- Owner Git commit/release decision for the combined **FIX-01 + PP-01-A1** candidate.
- Separate combined release acceptance; the FIX-01 release block remains.
- Any desired real-browser and real-Telegram manual smoke test.
- No claim of deployment, production verification or behavioral acceptance is made.

*[Updated 2026-11-27: this list is preserved as the state before Owner acceptance. The final
independent Codex acceptance closed all eight findings (PASS; 17 PASS / 0 FAIL / 0 SKIP), and the
Owner functional acceptance is complete (**ACCEPT WITH NON-BLOCKING NOTES**; see §17 and
[`PP-01-A1_Owner_Acceptance_Closure_Record.md`](PP-01-A1_Owner_Acceptance_Closure_Record.md)).
The Owner Git commit and the separate combined release acceptance remain; KHD-50 is unchanged.]*

## 12. R1 audit remediation (2026-11-26)

The independent PP-01-A1 audit returned eight findings (A1-01 … A1-08). All eight were
remediated in the working tree on the same baseline; the product decisions D1–D7 were
not reopened and no migration was added. The full finding-by-finding record, including
root causes, changed files, regression tests, exact results, PostgreSQL cleanup and
limitations, is [`PP-01-A1_R1_Remediation_Record.md`](PP-01-A1_R1_Remediation_Record.md).

In summary:

- **A1-01:** per-conversation mutation identity, supersession and serialized title writes
  in the Web page; cleanup is scoped to the conversation and editor instance that sent
  the write.
- **A1-02:** the title PATCH now requires exactly one Origin equal to the application's
  Origin; Fetch Metadata is not accepted in its place and other mutations are unchanged.
- **A1-03:** the latest available message instant is `max(created_at)`, not the row with
  the greatest id; the Web list and Telegram share it.
- **A1-04:** forbidden line separators/controls are refused before trimming, and ZWJ/tag
  characters are accepted only inside a legitimate emoji sequence, in both validators.
- **A1-05:** assistant-only histories and uncaptioned photos are distinguished from an
  empty conversation; the derived-title query no longer treats the photo placeholder as
  user text.
- **A1-06:** the untitled fallback carries the explicit «МСК» label in the header and
  sidebar.
- **A1-07:** title mutations refresh an already-loaded `Conversation` row before reading
  the summary, so the returned metadata is authoritative.
- **A1-08:** user-facing title/retention documentation was added, and retention coverage
  now writes titles through the application and exercises a real physical purge.

The original evidence in sections 7–8 is preserved as the record of the pre-remediation
implementation. The R1 verification is affected-area based (1365 passed locally, plus
113 persistence tests on PostgreSQL 16.15 with the disposable container restored to
stopped); it is not a new full-suite run. Findings remain open until independent
re-verification and are not marked closed by this record.

## 13. R2 targeted remediation (2026-11-26)

The independent R1 re-audit closed **A1-02**, **A1-03**, **A1-06** and **A1-08**
independently and left **A1-01** (BLOCKER), **A1-04**, **A1-05** and **A1-07** open. R2
remediates exactly those four in the working tree, on the same baseline `def03ea`, without
reopening a product decision, adding a migration or changing FIX-01. The four closed
findings were not reopened and their verified invariants are preserved.

In summary:

- **A1-01:** a stale conversation-list answer can no longer restore a title that was
  confirmed after the read began (list reads carry a title-mutation generation in
  `chat-logic.js`). Title writes now use a non-aborting **soft timeout**: the request is
  retained, the visible state says it is pending, and the next same-conversation write
  stays queued until the underlying request really settles. A transport outcome that
  cannot be confirmed (no answer, or a proxy 5xx without the application's error body)
  sets a per-conversation guard that refuses later title writes with a truthful Russian
  message until the page is reloaded; it never sends a conflicting write past the unknown
  one, and never retries automatically. Definitive application failures still let the
  queue proceed. The audited commit inversion was reproduced through the real PATCH
  handler and persistence on SQLite and PostgreSQL; the page's order prevents it.
- **A1-04:** emoji bases are the frozen Extended_Pictographic table (Unicode 17.0 / ICU
  78.2) in `house_helper/core/title_unicode.py`, shared with a generated block embedded in
  `chat-logic.js` and parity-tested at every table boundary. Titles made only of
  default-ignorable or blank-looking characters are refused; the subdivision-flag tag
  alphabet is letters a–z only; the six audit counterexamples are regression data on both
  sides.
- **A1-05:** the photo placeholder is skipped only when the message has an attachment, so
  a literal `[Фото]` names its conversation while an uncaptioned photo does not and the
  search continues to the next eligible user text. The exact-caption ambiguity is
  documented; no migration was added. The derived-title explanation now names the
  eligibility.
- **A1-07:** the title mutation reconciles only the `title` attribute (scalar read under
  `no_autoflush`, `set_committed_value`), so unrelated pending ORM state is neither
  overwritten nor flushed; the caller's transaction and rollback still decide persistence.

Full finding-by-finding evidence, the exact concurrency/recovery contract, the Unicode
policy, the PostgreSQL environment inventories and the remaining limitations are in
[`PP-01-A1_R2_Remediation_Record.md`](PP-01-A1_R2_Remediation_Record.md). Affected-area
verification is green locally (**1540 passed**) and the persistence (141) and chat API
(186) suites pass on PostgreSQL 16.15 with the disposable container restored to `Exited
(0)`. Findings are not independently closed; the increment still awaits Codex re-audit and
Owner acceptance, and the FIX-01 release block (24 PASS / 6 FAIL) remains in force.

## 14. R3 targeted remediation (2026-11-27)

The R2 independent re-audit closed **A1-02**, **A1-03**, **A1-04**, **A1-05**, **A1-06**,
**A1-07** and **A1-08**, and confirmed **A1-01** as the only remaining BLOCKER, in three
sub-findings. R3 remediates exactly those three in the working tree, on the same baseline
`def03ea`, without reopening a product decision, adding a migration or changing FIX-01.
The R2 soft timeout and serialized title queue are preserved; the seven closed findings
were not reopened.

In summary:

- **A1-01a (pagination):** confirmed title metadata (`title`, `titleOrigin`, `canReset`,
  and its mutation mark) is now kept in a per-conversation page store, independent of the
  current list page. A conversation that leaves the 50-item list while its rename or
  reset is confirmed and returns in an older read keeps the confirmed title fields, while
  membership, order, dates and unrelated metadata stay the server's; nothing is inserted
  or resurrected, and the entry is dropped when a read begun after the confirmation
  applies (or the conversation is forgotten), so external changes are reflected and
  metadata stays bounded.
- **A1-01b (same-document restoration):** a persisted `pageshow` now calls
  `controller.restore()`, which holds the page back and re-checks who is signed in without
  discarding this document's title queue or its indeterminate-write guard. A newer rename
  is never released past an unanswered earlier write; a different account or a definite
  401 still clears everything, and a delayed answer from the old context is dropped. The
  non-aborting soft timeout is unchanged.
- **A1-01c (outcome classification):** every 5xx is now an **indeterminate** title
  outcome, because the PATCH handler commits before it builds its answer and an
  application `internal_error` can therefore follow a stored write. Only 4xx refusals are
  definitive. The guard, no-retry rule and queue are unchanged; the recovery wording now
  distinguishes checking the current saved title from proving the earlier request ended.
  The post-commit 500 and pre-commit 503 are reproduced through the real route and
  persistence on SQLite and PostgreSQL.

Full evidence, the exact reconciliation lifecycle, the restoration behavior, the outcome
table, changed files, test commands, PostgreSQL inventories and the cross-instance
limitations are in [`PP-01-A1_R3_Remediation_Record.md`](PP-01-A1_R3_Remediation_Record.md).
Affected-area verification is green locally (**912 passed** in the Web logic/DOM/assets/
chat-API group, **610 passed** in persistence and adjacent suites, **392** Node scenarios
in UTC) and the chat API (188) plus persistence (141) suites pass on PostgreSQL 16.15
(**329 passed**) with the disposable container restored to `Exited (0)`. Findings are not
independently closed; the increment still awaits targeted Codex re-audit and Owner
acceptance, and the FIX-01 release block (24 PASS / 6 FAIL) remains in force.

## 15. R4 invariant-based remediation (2026-11-27)

The R3 re-audit confirmed the R3 corrections and found four further A1-01 defects on
equivalent execution paths. R4 corrects them by enforcing four invariants rather than
adding isolated exception handling, on the same baseline `def03ea`, without reopening a
product decision, adding a migration or changing FIX-01.

In summary:

- **R4-01 (authoritative object existence):** the confirmed-deletion and application
  `conversation_not_found` paths now share one list-generation invalidation
  (`invalidateListReads()`), so a list read begun before an authoritative disappearance is
  discarded instead of resurrecting the conversation. The invalidated read no longer leaves
  the list stuck in `loading`; a read begun afterwards applies normally, and no per-
  conversation tombstone is kept.
- **R4-02 (authentication freshness):** every `/auth/me` question is numbered (`verifySeq`)
  and a same-document restoration forces a fresh one. A superseded answer can neither let
  the old page through nor clear the newer question. The gate keeps all private content out
  of view until the fresh answer, and a queued title write waits on `gateBarrier()` so it
  is never sent into an unverified or changed authenticated context. Same-account
  verification still preserves the title queue and guards; `start()` still clears them.
- **R4-03 (fail-closed classification):** only a closed table of the application's
  pre-commit title refusals (code **and** status) is definitive; a proxy 408/429, any other
  unrecognized 4xx, every 5xx, transport failures and unexpected bodies are indeterminate
  and guard the conversation. The real PATCH route proves each definitive pair is answered
  before any write on SQLite and PostgreSQL.
- **R4-04 (response identity):** a title answer is applied only when it names the requested
  conversation and obeys the operation: a rename returns the requested normalized manual
  title and reset state; a reset returns a non-manual, non-resettable conversation. Any
  mismatch is indeterminate, applies nothing, duplicates nothing and keeps the draft and
  guard.

Full evidence, the invariant matrix, the closed refusal table, changed files, test
commands and PostgreSQL inventories are in
[`PP-01-A1_R4_Remediation_Record.md`](PP-01-A1_R4_Remediation_Record.md). Affected-area
verification is green locally (406 Node scenarios, 428 logic-wrapper tests, 196 DOM tests,
204 UI/asset/integration tests, 196 chat-API tests) and the chat API suite passes on
PostgreSQL 16.15 (**196 passed**) with the disposable container restored to `Exited (0)`
and no leftover database or role. Findings are not independently closed; the increment
still awaits targeted Codex re-audit and Owner acceptance, and the FIX-01 release block
(24 PASS / 6 FAIL) remains in force.

## 16. R5 bounded architectural reorganization (2026-11-27)

The latest independent R4 functional review executed 49 existing checks successfully and
confirmed structural consistency defects in the title-operation controller (F1–F4). The
Owner approved **Option B** of PP-01-A1-AD-01: one processing boundary — *validate
identity → validate the raw contract → decide the transition → apply scoped changes* —
instead of further isolated corrections. The normative architecture and the ten
invariants are in
[`PP-01-A1-AD-01_Architecture_Lock_v1.0.md`](../architecture/PP-01-A1-AD-01_Architecture_Lock_v1.0.md);
the implementation and evidence are in
[`PP-01-A1_R5_Implementation_Record.md`](PP-01-A1_R5_Implementation_Record.md).

In summary:

- **Response contract:** read normalization keeps the compatibility inference; mutation
  success now requires the strict `_conversation_json` contract (every field the real
  route has always answered), the requested identity, consistent title state and the
  requested rename/reset
  semantics. A 401 is endpoint-specific: the application's pre-write `not_authenticated`
  body remains definitive; every other 401 is indeterminate, keeps the guard and draft,
  releases and retries nothing, and forces a fresh session verification (AD-C2).
- **Operation identity:** a title record is an operation object cleared only by object
  identity, never by a per-session sequence number. A stale session's completion can no
  longer clear the current session's slow-write feedback.
- **Scoped apply:** a confirmation writes only the three title fields of an item the list
  still holds; activity dates, membership and order are never taken from a mutation
  answer. Unknown and invalidated operations apply nothing.
- **Idempotent absence (AD-C1):** the first applicable authoritative confirmation marks a
  conversation in a bounded, page-lifetime store and advances the list generation; a
  repeated notification is a no-op and cannot invalidate a legitimate later refill.
  Pagination is never treated as absence.
- **Verification:** the 13 new deterministic scenarios include a closed outcome matrix,
  the F1–F4 regressions and both clarifications. 419 Node scenarios, 441 logic-wrapper
  tests, 196 DOM tests, 204 UI/asset/integration tests, 197 chat-API tests (including a
  new real-route PATCH-shape crosscheck against the strict client contract) and 93
  adjacent app/retention tests pass locally, with no skip. PostgreSQL was not used: the
  change is client-side and no backend/persistence contract changed.

A1-01 remains **OPEN**; A1-02…A1-08 are preserved as independently closed. PP-01-A1 is not
Owner-accepted; FIX-01 remains 24 PASS / 6 FAIL and KHD-50 remains a release block. No
migration, provider call, prompt change, Production access, deployment or Git operation
was performed.

### 16.1 R5-C1 architecture conformance correction (2026-11-27)

The independent R5 functional review confirmed four deviations inside Option B and the
Owner authorized a bounded correction. The full evidence is in
[`PP-01-A1_R5-C1_Correction_Record.md`](PP-01-A1_R5-C1_Correction_Record.md). In summary:
authoritative absence now has one reconciliation (`applyConversationAbsence`) whose first
applicable confirmation owns the marker, the list generation, the title-lifetime
retirement and the one explicit-delete refill; each title write and DELETE captures its
conversation lifetime, so retired queued work sends nothing and an old completion cannot
touch a fresh lifetime after a re-listing; a body-read failure keeps the HTTP status, and
only the title classifier maps a partial `401` to indeterminate-with-verification (a
partial `404` is not an absence); mutation timestamps validate Gregorian dates, leap
years, years `0001`–`9999`, clock and offset ranges before conversion while reads keep the
compatibility parser. The processing boundary, the closed outcome model and the ten
invariants are unchanged. Verification: 428 Node scenarios, 450 logic-wrapper, 196 DOM,
204 UI/asset/integration, 197 chat-API and 93 adjacent tests passed, no skip. A1-01
remains **OPEN**; FIX-01 remains 24 PASS / 6 FAIL; KHD-50 remains a release block. Working
tree only — no commit, deployment or Production access.

### 16.2 R5-C2 ownership boundary correction (2026-11-27)

The independent R5-C1 acceptance review confirmed three remaining deviations (A1-01, C1/C2,
AD-C1 and the related I2/I7/I8 conformance) and the Owner authorized a bounded correction.
The full evidence is in
[`PP-01-A1_R5-C2_Correction_Record.md`](PP-01-A1_R5-C2_Correction_Record.md). In summary:
one ownership contract (`absenceIsOwned`) is applied at the absence boundary before any
read or write — a read carries its thread, a turn its turn record, and a title write or
DELETE the conversation lifetime it captured. A retired DELETE whose re-listing revoked the
marker, a detached history thread, a stale turn and a retired title acknowledgement can no
longer remove a fresh conversation, close a replacement or temporary view, clear a
draft/guard/operation or invalidate a legitimate newer list read; applicable off-page
removal and a live DELETE's single refill are preserved. The DELETE result now separates
`wasCurrent` from `interaction`, and `app.js` moves the keyboard only for the completing
delete question, so a delayed duplicate cannot steal focus from another conversation.
Verification: 434 Node scenarios, 456 logic-wrapper, 197 DOM, 204 UI/asset/integration, 197
chat-API and 93 adjacent tests passed, no skip. Documented alongside: the Architecture Lock
soft-timeout erratum (`PP-01-A1-AD-01_Erratum_01.md`) and the R5-C1 invalid-summary count
correction. A1-01 remains **OPEN**; C3/C4 remain closed; FIX-01 remains 24 PASS / 6 FAIL;
KHD-50 remains a release block. Working tree only — no commit, deployment or Production
access.

### 16.3 R5-C3 regression-first ownership closure (2026-11-27)

The independent R5-C2 closure report confirmed three remaining ownership/interaction
defects and the Owner authorized a regression-first correction. The full RED/GREEN evidence
is in [`PP-01-A1_R5-C3_Correction_Record.md`](PP-01-A1_R5-C3_Correction_Record.md). In
summary: a temporary view's history read now captures the AD-C1 absence generation it was
issued against and is stale once an authoritative re-listing revokes it; `reconcile()`
clears `checking` only for the turn record it began with; and `app.js` captures an explicit
interaction generation when the DELETE is clicked, so a delayed completion that is still
the deletion (removal + refill) no longer closes a drawer the person reopened or moves focus
out of a section they chose. Each regression failed on the original implementation and
passes after the correction. Verification: 436 Node scenarios, 458 logic-wrapper, 199 DOM,
204 UI/asset/integration, 197 chat-API and 93 adjacent tests passed, no skip. A1-01 remains
**OPEN**; C3/C4 remain closed; FIX-01 remains 24 PASS / 6 FAIL; KHD-50 remains a release
block. Working tree only — no Git mutation, deployment or Production access.

### 16.4 R5-C4 functional recovery and interaction closure (2026-11-27)

The independent R5-C3 closure report confirmed three remaining functional/interaction
requirements and the Owner authorized a regression-first correction. The full RED/GREEN
evidence is in
[`PP-01-A1_R5-C4_Correction_Record.md`](PP-01-A1_R5-C4_Correction_Record.md). In summary: a
history read whose temporary view lost its AD-C1 marker still applies nothing, but if its
thread is still the selected, attached, unsynchronized history the page now starts exactly one
fresh applicable read — success displays the history and unlocks the composer, failure takes the
established history-error state with its retry, a second stale answer does not ask again, and an
already-running read is joined; `drawList` gives the keyboard back to the rebuilt row of the
focused conversation when it survives the update (across removal and refill) without focusing a
removed row or pulling focus into the list; and the chat's direct shopping-draft action and the
draft's cancellation advance the existing `interactionSeq`, so a DELETE still out keeps its
removal and one refill but no longer owns the keyboard after those interactions. Each new
regression failed on the R5-C3 implementation and passes after the correction. Verification:
438 Node scenarios, 460 logic-wrapper, 201 DOM, 204 UI/asset/integration, 197 chat-API and 93
adjacent tests passed, no skip. A1-01 remains **OPEN**; C3/C4, AD-C1 and AD-C2 remain closed;
FIX-01 remains 24 PASS / 6 FAIL; KHD-50 remains a release block. Working tree only — no Git
mutation, deployment or Production access.

### 16.5 R5-C5 desktop focus closure (2026-11-27)

The independent R5-C4 closure report confirmed one remaining defect and the Owner authorized a
regression-first correction; the full RED/GREEN evidence is in
[`PP-01-A1_R5-C5_Correction_Record.md`](PP-01-A1_R5-C5_Correction_Record.md). On a wide screen
the conversation list stays beside the chat, so the person can focus a surviving row without
any interaction the `interactionSeq` gate records. When a held DELETE settled, its ordinary
restoration focused the chat title and took that keyboard away. The completion's
`result.wasCurrent` path now returns before closing the drawer or focusing the title when the
active element is a conversation row inside the list (a non-null `data-conversation`). With no
surviving row focused, ordinary DELETE restoration is unchanged; removal, its one refill,
drawer, selection, `interaction` and `interactionSeq` are untouched, and no new mechanism was
introduced. One new wide-screen mini-DOM scenario failed on the R5-C4 implementation and passes
after the correction. Verification: 438 Node scenarios (0 failed), 460 logic-wrapper, 202 DOM
and 204 UI/asset/integration tests passed, no skip and no weakened assertion. A1-01 remains
**OPEN**; C3/C4, AD-C1 and AD-C2 remain closed; FIX-01 remains 24 PASS / 6 FAIL; KHD-50 remains
a release block. Working tree only — no Git mutation, deployment or Production access.

## 17. Owner functional acceptance and documentary closure (2026-11-27)

The Owner completed functional acceptance and targeted retesting of PP-01-A1 and decided
**ACCEPT WITH NON-BLOCKING NOTES**. The full acceptance record is
[`PP-01-A1_Owner_Acceptance_Closure_Record.md`](PP-01-A1_Owner_Acceptance_Closure_Record.md).
This section records the status transition only; sections 1–16 above remain the implementation
and technical history and are not rewritten.

**UX-01…UX-07 accepted.** The Owner-requested corrections were implemented in the working tree
after the R5-C5 technical closure and accepted on retest:

- **UX-01:** user-facing conversation wording is «диалог» across the Web shell, labels, status
  lines, title controls, API human-readable errors and Telegram copy; internal identifiers, API
  codes, database fields and the `conversation` concept are untouched.
- **UX-02:** each timestamp is a label plus an unbreakable date+«МСК» run, with smaller meta type
  and tighter rows; creation/last-message times and Moscow semantics are unchanged.
- **UX-03:** the actions menu is `width: max-content` with a minimum width; commands fit on one
  line at desktop width and may wrap instead of overflowing at narrow widths.
- **UX-04:** successful actions use the muted, slimmer `.banner-info` strip and a compact
  `.notice-ok`; error banners keep their full treatment and announcements remain intact.
- **UX-05:** the title-origin panel is neutral information styling, not a red `.confirm` box;
  the delete question remains red.
- **UX-06:** plain-language title-origin wording — «Вы сами задали название этого диалога.»,
  «Название взято из начала вашего первого сообщения с текстом.» and «В доступных сообщениях
  нет вашего текста, поэтому название не задано.» This supersedes the exact explanation
  **wording** in Scope Lock D2; the mechanism and reset semantics are unchanged.
- **UX-07:** ordinary desktop deletion focuses `#composer-input`; narrow/mobile keeps
  `#chat-title`; the R5-C5 surviving-row path is preserved.

**UX-08 accepted as a non-blocking limitation.** The developer confirmed in Chrome that rows are
native buttons reachable and traversable with Tab/Shift+Tab, with a visible focus ring, scrolling
into view and native Enter/Space activation; ArrowUp/ArrowDown do nothing. The Owner did not
reproduce this in Edge. Owner verification is recorded as **NOT PASS / NOT REPRODUCED**, not as a
pass. Arrow-key navigation is not required for this increment and no code change was made.

**UX-09 accepted with the existing security policy preserved.** Conversation pages and API
payloads keep `Cache-Control: no-store`; static assets keep `no-cache` plus ETag. Chromium/Edge
refuse BFCache for no-store documents. The policy was not weakened; actual BFCache restoration
remains unverified and the existing `pageshow`/`persisted` guard remains the covered behavior.

**Other Owner decisions:** title reset per Scope Lock D2 is accepted, with semantic/original-title
restoration left to PP-01.1A/full PP-01.1D; actual iPhone acceptance is deferred to Production
post-deployment verification; offline Web/Telegram parity is accepted while live Telegram-client
verification remains pending.

**Regression evidence for the UX-correction layer:** `node tests/js/run_scenarios.js --tz=UTC`
returned **438 scenarios, 0 failed**; the affected Python suites returned **1165 passed** in the
final run after the last class rename; real-browser checks covered labels, row compaction at
1440 px and 390 px, menu fit, the neutral title panel, desktop/mobile deletion focus, R5-C5
surviving-row focus through removal and refill, Tab order, the focus ring and focused-row
scrolling. No assertion was weakened; wording assertions were updated to the accepted
terminology, and the wide-screen delete scenario asserts composer focus. No full-suite or
PostgreSQL run was made for this layer: the changes are client-side plus user-facing copy and
change no server or persistence contract. The prior R5-C5 evidence (438 Node, 460 logic-wrapper,
202 DOM, 204 UI/asset/integration, no skip) is preserved.

**Technical status:** the final independent Codex acceptance of PP-01-A1-R5-C5 (2026-11-27)
returned **PASS — ALL EIGHT FINDINGS CLOSED / READY FOR OWNER FUNCTIONAL ACCEPTANCE**, with
**A1-01 — Asynchronous Web consistency — Closed with C1 desktop focus verified** and **17 PASS /
0 FAIL / 0 SKIP**. A1-02…A1-08 and AD-C1/AD-C2 remain independently closed as recorded;
R5-C1…R5-C4 were independently closed, and R5-C5 was RED/GREEN verified and closed by the final
acceptance. The `A1-01 OPEN` statements in sections 12–16 (including §16.5) are the historically
accurate pre-acceptance intermediate status and are preserved as chronology, not as the final
disposition. The final Codex acceptance covers the R5-C5 baseline; the later UX-01…UX-09
corrections are a subsequent Owner acceptance layer and are not covered by that Codex audit. No
technical audit was reopened, no additional correction was implemented, and A2/FIX-02 were not
started. The final Codex report itself is not a separate repository artifact; its verdict,
disposition and counts are recorded from the Owner-supplied final acceptance brief and the
2026-11-27 documentary correction request (see the closure record, §4.1).

**Release boundary:** the acceptance is code/product acceptance only. KHD-50 is unchanged —
FIX-01 remains **24 PASS / 6 FAIL**, behavioural acceptance is FAILED and production release is
**BLOCKED**. The latest documented Owner release strategy remains the combined
**HH-BRAND-01-FIX-01 + PP-01-A1** candidate (KHD-54); no later decision changes it. No commit,
release, deployment, provider call or Production access was performed by this closure. The
working tree is **READY FOR OWNER COMMIT**.

## 18. PP-01-A1-CI-FIX-01 — failed CI run 45987365414 (2026-11-27)

PP-01-A1 was committed as `f55e089cf1397adbb32aedde5c3a4878831c7d79`; the push-triggered CI
run **`45987365414`** for that commit failed. The two defects below account for the failure:
**F1** is visible only on Python 3.10, and **F2** is deterministic on every Python version.
Both are corrected in the working tree without a functional change beyond the corrections
themselves, without a migration, and without reopening the PP-01-A1 acceptance or its
product decisions.

### 18.1 F1 — the format-character check depended on the Python Unicode version

**Root cause.** The server classified a format character with the running Python's own table
(`unicodedata.category(character) == "Cf"`), while the page classified it with `\p{Cf}` from
the frozen block (Node.js 24.14.0 / ICU 78.2 / Unicode 17.0). The tables are not the same:
Python 3.10 ships Unicode 13.0, Python 3.12 ships Unicode 15.0, and U+0890/U+0891 became `Cf`
in Unicode 14.0 while U+13439..U+1343F became `Cf` in Unicode 15.0. On Python 3.10 those
code points are still unassigned (`Cn`), so the server accepted a title containing one while
the page refused it.
`tests/test_web_ui_logic.py::test_the_page_validates_a_conversation_title_exactly_as_the_server_does`
already listed those boundaries and therefore failed on the Python 3.10 job; it passed on
Python 3.12, which is why the defect was invisible in the local development runs.

**Correction.** `house_helper/core/title_unicode.py` now carries a third frozen table,
`FORMAT_RANGES` (`General_Category=Cf`, Unicode 17.0 / ICU 78.2, 21 ranges), and an
`is_format(point)` helper. `persistence.conversations._forbidden_title_character` uses that
table instead of the runtime category; the generated block adds `TITLE_FORMAT_RANGES`, and
`chat-logic.js` tests membership in it instead of `\p{Cf}`. The U+200D and tag-character
exceptions (`_is_emoji_format` / `emojiFormatCharacter`) are unchanged. The parity test now
derives boundary texts from `FORMAT_RANGES` (each range's start, end and one code point
outside, alone and inside ordinary text), so the two validators are compared over the whole
frozen set rather than a hand-listed sample. The module docstring records the provenance and
the version, and distinguishes the formal `Cc`/`Cs` stability guarantee from the `Zl`/`Zp`
singleton definitions and the verified cross-runtime agreement.

### 18.2 F2 — user-facing copy contained «ё»

**Root cause.** The PP-01-A1 title-origin explanation in `chat-logic.js` contained «Своё»
(line 617 at `f55e089`). The product's spelling rule requires the product's own Russian text
to use «е», and
`tests/test_orthography.py::test_no_text_the_product_shows_is_written_with_yo` scans every
source file whose text people can see, including `chat-logic.js`. The literal therefore
failed the scan on every Python version.

**Correction.** The literal is spelled «Свое». No other wording, markup, behavior or test
expectation changed.

### 18.3 Developer RED/GREEN evidence (2026-11-27)

- **F1 RED (Python 3.10.22, Unicode 13.0).** A focused standard-library reproduction of the
  baseline rule in `python:3.10-slim` shows U+0890, U+0891, U+13439 and U+1343F as `Cn`: the
  baseline server accepted each while the page's frozen `\p{Cf}` refused
  (`baseline_match=False`); U+13430 is `Cf` in Unicode 13.0, so both refused. With the frozen
  table, all five are refused on both sides (`fixed_match=True`).
- **F1 GREEN (Python 3.12.10, Node.js 24.14.0).** The parity test passes, including the new
  `FORMAT_RANGES` boundaries.
- **F2 RED.** Applying the scan's `_FORBIDDEN` pattern to
  `git show f55e089:house_helper/web/static/chat-logic.js` finds exactly one offender, line
  617 («Своё»). **F2 GREEN.** The same scan over the working tree finds none, and
  `test_no_text_the_product_shows_is_written_with_yo` passes.
- **Affected-area runs on the corrected tree** (Windows 11, Python 3.12.10, Node.js 24.14.0):
  `tests/test_web_ui_logic.py` + `tests/test_orthography.py` — **575 passed**;
  `tests/test_persistence_conversations.py` — **141 passed**; `tests/test_web_chat_api.py
  -k title` — **25 passed**; `tests/test_web_ui.py -k title` — **1 passed**. These are
  affected-area results, not a full-suite or CI run.

### 18.4 Independent review and remaining gates

The independent Codex review returned **PASS WITH NON-BLOCKING NOTES — no material
defects**; CI-FIX-01 is accepted. Among the accepted non-blocking notes was that the module
docstring described the four remaining categories as "fixed by the Unicode stability policy"
without distinguishing the formal `Cc`/`Cs` guarantee from the `Zl`/`Zp` singleton definitions
and the verified cross-runtime agreement; that is corrected in
`house_helper/core/title_unicode.py` by this closure. Verification limitations recorded, not waived: the per-job CI output of run
`45987365414` is not stored in the repository; the Python 3.10 RED evidence is a focused
standard-library reproduction, not a full Python 3.10 suite in this environment; and no new
push-triggered run was made.

**Full CI matrix pending.** The next push-triggered run must be green on Python 3.10 and
Python 3.12, including the existing PostgreSQL and SQLite workflow gates. Until then no CI
PASS is claimed.

**Changed files for CI-FIX-01:**

- `house_helper/core/title_unicode.py` — frozen `FORMAT_RANGES`, `is_format`, docstring.
- `house_helper/persistence/conversations.py` — use `title_unicode.is_format`.
- `house_helper/web/static/chat-logic.js` — frozen `TITLE_FORMAT_RANGES`; «Свое».
- `tests/test_web_ui_logic.py` — `FORMAT_RANGES` boundary parity.
- `docs/product/PP-01-A1_Implementation_Record.md` — this section.
- `docs/STATUS.md` — the CI-FIX-01 entry.

**Status:** **READY FOR OWNER COMMIT.** No migration, provider call, prompt change,
Production access, deployment or Git mutation was performed. KHD-50 and KHD-54 are unchanged —
FIX-01 remains **24 PASS / 6 FAIL**, production release remains **BLOCKED**, and the latest
documented release strategy remains the combined **HH-BRAND-01-FIX-01 + PP-01-A1** candidate.