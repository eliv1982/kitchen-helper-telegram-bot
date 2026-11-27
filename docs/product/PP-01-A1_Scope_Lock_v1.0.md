# House Helper — PP-01-A1 Conversation Navigation
## Scope Lock v1.0

| Field | Value |
|---|---|
| Version | 1.0 |
| Date | 2026-11-26 |
| Package | PP-01-A1 — Conversation Navigation (increment A of PP-01) |
| Product | House Helper / Fedya / Kitchen |
| Authoritative product specification | [`PP-01_UX_Product_Specification_v1.0.md`](PP-01_UX_Product_Specification_v1.0.md) |
| Planning baseline | `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e` on `main` |
| Release strategy | HH-BRAND-01-FIX-01 preserved in full; the intended future candidate is **FIX-01 + PP-01-A1** |
| Status | **OWNER APPROVED / IMPLEMENTATION AUTHORIZED** (local implementation, documentation and offline testing only) |
| Independent auditor | Codex (read-only) |
| Implementation / release | Code implemented in the working tree; **not committed, audited, accepted, released or deployed** |

## 1. Authority and boundaries

This Scope Lock records the Owner-approved decisions **D1–D7** for PP-01-A1 and
authorizes local implementation, documentation and offline testing. It authorizes no
Git operation, no production change, no database migration, no provider call, no
system-prompt change and no research execution. The Owner performs all Git operations.

The current baseline contains the behaviorally unaccepted HH-BRAND-01-FIX-01
candidate (24 PASS / 6 FAIL). That release block remains in force. This increment does
not resolve any of the six failures, does not reopen HH-BRAND-01-FIX-02, and does not
implement SR-01 Selective Release Baseline Restoration. The implementation preserves
FIX-01 in its entirety, including all system-prompt changes.

Out of scope: semantic automatic title generation, persistence of an original
semantic title, full PP-01.1D, Recipe Reader, Compact Recipe Cards, Recipe Selection,
new assistant intelligence, provider configuration, operational infrastructure
cleanup, production changes and unrelated refactoring. PP-01.1A and full PP-01.1D are
not closed by this increment.

## 2. Approved functional decisions

### D1 — Conversation title validation

Manual renaming uses the existing persisted `conversations.title` column (no migration).

- Maximum length: **200 Unicode code points**.
- Leading and trailing whitespace is trimmed; empty or whitespace-only input is refused.
- Line breaks, control characters, unpaired surrogates and invisible format characters
  are refused. Format characters that a valid emoji sequence needs — ZERO WIDTH JOINER
  (U+200D) and the tag characters U+E0020–U+E007F of a subdivision flag — are allowed.
- Cyrillic, Latin, combining marks and ordinary emoji are preserved without lossy
  transformation.
- Duplicate titles are allowed; there is no rename-count limit.
- A title over the limit is refused, never silently truncated.
- Validation is authoritative on the backend; the Web page mirrors the same rule before
  it sends.
- Russian feedback names the failure: empty, too long, or forbidden characters.

No new LLM call and no database migration are authorized or used.

### D2 — Title management

The Web conversation header's existing contextual menu («⋯») offers:

- «Переименовать»
- «Как создано название»
- «Сбросить название»

The origin explanation is truthful to the mechanism actually implemented:

| State | Explanation |
|---|---|
| Stored user title | «Вы самостоятельно задали название этого диалога». |
| Derived title | «Название сформировано на основе начала первого доступного сообщения пользователя». |
| No title and no available message | «Название пока не задано: в диалоге нет доступных сообщений». |

The explanation is a visible status region — usable by touch and keyboard, not a
hover-only hint.

Reset is offered only while a stored user title exists. It removes the override, and
the server returns the currently applicable derived (or missing) title; the visible
interface is updated from that authoritative answer. Reset preserves conversation
identity, messages, favorites and all related objects. It does not restore a persisted
"original semantic title", because no such title is stored. Full PP-01.1D remains a
future deliverable.

The form supports keyboard operation (Enter submits, Escape cancels), cancellation,
understandable error feedback and mobile use through the existing responsive layout.

### D3 — Title retention and privacy

A user-defined title is explicit conversation metadata. It remains stored until it is
reset or the conversation is deleted. Message retention is unchanged: each message and
its photo still expire 365 days after their own creation (KHD-04, KHD-30, KHD-34).

This is a deliberate, Owner-approved policy exception: unlike the derived title (a copy
of a message, which expires with it), the stored title is the person's own named
navigation metadata. It is never sent to the assistant, never becomes an instruction and
never appears in `build_context`; it is content the person explicitly created and can
remove at any time.

Consequences confirmed against the existing purge:

- A manual title adds no message and no activity. `set_conversation_title` and
  `reset_conversation_title` assign `updated_at` to itself, so a rename does not extend
  the eligibility of an otherwise empty, old conversation.
- The purge still deletes an empty conversation whose `created_at` and `updated_at` are
  both at or before the cutoff; the title row is deleted with the conversation.
- Derived titles continue to come from the first available user message and disappear
  with it; reset then returns the next derived title or no title.
- No historical title archive and no additional retention mechanism exist.

A stored title is navigation metadata. It is never sent to the assistant, never becomes
an instruction, and never appears in `build_context`.

### D4 — Conversation timestamps and ordering

The list exposes, per conversation:

1. Its creation date and time.
2. The date and time of its latest **still available** message.

The second value is a correlated read of the newest visible message
(`messages.expires_at > now`, user or assistant), not `conversations.updated_at`.
`updated_at` remains the row's last-activity write but is not displayed and is not the
ordering key, so a stale value cannot produce a false date.

- Conversations are ordered newest first by that latest-message instant.
- A conversation without an available message is ordered by its creation time.
- The empty state is displayed as «Нет сообщений».
- Ties are broken by conversation id (descending), giving a total, deterministic order.
- The same ordering expression is used by the Web list, the Telegram dialogs list and
  Telegram's "latest conversation" resolution.
- Rename and reset do not change either timestamp or the position in the list.
- Offset pagination uses the same total order, so pages are stable absent concurrent
  activity.
- After retention deletes messages, the displayed value and the order reflect the
  surviving data.

The list is one SQL statement with correlated scalar subqueries (title and latest
message) served by the existing `(conversation_id, id)` index; there is no
per-conversation client query.

### D5 — Timezone and date presentation

UTC remains the canonical storage and interchange basis. Conversation navigation is
presented in **Europe/Moscow**.

- Absolute format: `ДД.ММ.ГГГГ, ЧЧ:ММ`.
- Every displayed conversation date carries the explicit label **МСК**.
- Web converts instants with `Intl.DateTimeFormat` and an explicit
  `timeZone: "Europe/Moscow"`; it does not use the browser's or the host's zone.
- Telegram uses `zoneinfo.ZoneInfo("Europe/Moscow")`, with the existing fixed +03:00
  fallback when the host has no tz database.
- UTC↔Moscow calendar-day boundaries are handled by the conversion itself.
- No user timezone preference, database field or profile setting is added.

### D6 — Web message chronology

- A calendar-day separator precedes the first displayed message of each Moscow day and
  whenever the Moscow day changes. The separator is absolute (`ДД.ММ.ГГГГ`) and labelled
  «МСК»; relative labels such as «Сегодня» or «Вчера» are not used.
- Each message shows its original time as `ЧЧ:ММ` (Moscow).
- The timeline is derived from the whole loaded history on every render, so loading
  older messages recalculates separators instead of duplicating or misplacing them.
- A message without authoritative timestamp data gets neither a fabricated time nor a
  separator.
- The optimistic pending message shows the Moscow clock of its own client-side send
  time and takes its place in the same day sequence; the authoritative history replaces
  it through the existing reconciliation path.
- Existing message rendering, voice/text interaction, recipe rendering, favorites,
  shopping lists, text-size preferences and the visual identity are preserved. No
  horizontal overflow is introduced: separators are ordinary flex rows, and the
  existing text-size attribute scales them with the rest.

### D7 — Cross-channel consistency

Web is the management interface for A1:

- rename, origin explanation and reset;
- creation and latest-message timestamps;
- calendar-day separators and per-message times.

Telegram:

- displays the same authoritative stored or derived titles from the same persistence
  layer;
- reflects Web rename and reset on the next `/dialogs` read;
- uses the same ordering and metadata semantics;
- shows each conversation's creation date and latest-message date in the dialogs
  message, in Moscow time labelled МСК;
- does not replay history or use its own delivery timestamps as conversation message
  timestamps.

Telegram rename, reset and title-explanation commands are deferred. There is no separate
title store and no duplicated business rule.

## 3. Security and object integrity

- Every title mutation is one `UPDATE ... WHERE id = :id AND user_id = :session_user`.
  A conversation that is unknown, foreign or malformed is the same 404
  `conversation_not_found`; the two are not told apart.
- Reads and mutations use the existing session, Origin check, JSON body rules,
  correlation-id and error catalogue. PATCH is refused without the application's
  Origin, like every other mutation.
- A title is untrusted display text: the Web page renders it through `textContent`
  only, and Telegram sends plain text (no `parse_mode`).
- A title never enters the assistant context, a prompt, a model call or another
  account's metadata.
- Rename and reset touch only `conversations.title`; they do not modify messages,
  structured recipes, favorites, shopping lists, preferences or other rows.
- Stale requests, deleted conversations and failed mutations are handled by the
  existing 404/error paths; no new concurrency architecture is introduced.

## 4. Acceptance criteria

The increment is acceptable for independent audit when all of the following hold:

1. D1–D7 are implemented as described, with no database migration.
2. Manual titles validate exactly as specified on the backend and in the page.
3. Rename and reset persist, survive reload, are visible in Telegram on the next list
   read, and do not change chronology or ordering.
4. Reset is available only for a stored user title and returns the authoritative
   derived/fallback title.
5. The list shows both absolute Moscow-time dates (or «Нет сообщений») and orders by
   the latest available message.
6. Web history shows absolute Moscow day separators and per-message `ЧЧ:ММ`.
7. Unauthenticated, cross-account, foreign, deleted and malformed mutations are refused
   with no side effects.
8. Retention behavior is unchanged; a manual title neither extends retention nor
   prevents purge, and is deleted with its conversation.
9. FIX-01 is preserved in full.
10. Focused, affected-area and offline suites pass; every skip is reported.
11. Documentation records the implementation, evidence, limitations and deferred work.

## 5. Release strategy and deferred work

The intended future release candidate is **HH-BRAND-01-FIX-01 + PP-01-A1**. The existing
FIX-01 release block remains until a separate combined release acceptance decision. The
six documented behavioral failures are not resolved by this increment.

Deferred: semantic automatic titles (PP-01.1A), persistence of an original semantic
title, full PP-01.1D, Telegram rename/reset/explanation commands, a user timezone
preference, relative date labels, and all PP-01.3–PP-01.5 recipe work.
