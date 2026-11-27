# House Helper — PP-01 Technical Assessment and Production Baseline Record

| Field | Value |
|---|---|
| Date | 2026-11-26 |
| Purpose | Record the completed PP-01 technical starting-point assessment and the reported Production baseline verification that preceded PP-01-A1 |
| Evidence grade | Code facts below were read from the repository at `def03eaf6b2d0902c6bf2a76bd659adc6cedf04e`; the Production facts are **previously reported operational evidence**, not re-verified while writing this record |
| Implementation status | Assessment only; the authorized increment is recorded separately in [`PP-01-A1_Implementation_Record.md`](../product/PP-01-A1_Implementation_Record.md) |

## 1. Authority and scope

This record consolidates the technical starting point used for the PP-01-A1 Scope
Lock. It does not authorize implementation, a migration, a provider call, a production
change or a release. It repeats no entire assessment where a concrete finding is enough,
and it distinguishes repository evidence from operational reporting.

Governing product input:

- [`PP-01_UX_Product_Specification_v1.0.md`](../product/PP-01_UX_Product_Specification_v1.0.md)
- `docs/DECISIONS.md` (KHD-04, KHD-05, KHD-30, KHD-34, KHD-52, KHD-53)

## 2. Technical findings at the planning baseline

### 2.1 Conversation title

- `conversations.title` already exists: `VARCHAR(200) NULL`, with a
  `length(title) <= 200` CHECK, from migration `0001_persistence_foundation`
  (`house_helper/persistence/migrations/versions/0001_persistence_foundation.py`).
- No code stored a title. `persistence.conversations` derived one on every read from the
  first non-expired user message, shortened to 60 code points plus an ellipsis
  (`conversation_title`, `_first_question_subquery`), exactly as KHD-34 recorded.
- `conversations.updated_at` was the list's ordering key and the Telegram dialogs'
  displayed time. It moved only when a message was added; no title mutation existed.
- Therefore a persisted manual title required **no migration**, and the derived/fallback
  title remained available for reset without an archive.

### 2.2 Conversation and message timestamps

- Messages carry timezone-aware UTC `created_at` and their own `expires_at`
  (retention at most 365 days). Reads exclude expired messages at the caller's `now`.
- `ConversationSummary` exposed only `created_at` and `updated_at`; the first available
  message's instant was not read for the list.
- The Web page formatted times in the browser's local zone (`formatMoment`,
  `conversationLabel`); Telegram already used Europe/Moscow with an explicit МСК label.
- A conversation without messages was named by its creation date in the Web list and by
  `updated_at` in Telegram.

### 2.3 Web surfaces

- The conversation header already had a contextual actions menu (`#chat-actions`,
  `#chat-actions-toggle`, `#chat-actions-menu`) containing only «Удалить разговор».
- The thread renderer drew messages by id order with a single `msg-meta` time per
  message, no calendar-day separators.
- The page logic is a DOM-free script (`chat-logic.js`) with a Node harness; the drawing
  script (`app.js`) runs against a mini DOM. This is the existing test architecture and
  was reused.

### 2.4 Telegram surface

- `/dialogs` reads `persistence.conversations.list_conversations` (newest `updated_at`
  first), renders one button per conversation with `_moscow_time(updated_at) — title`,
  eight per page, and selects through the existing ownership-checked cursor.
- The bot runs inside the Web process and shares the conversation store; there is no
  separate title or history store.

### 2.5 Persistence and retention

- The retention purge deletes expired messages and turn requests individually, and
  deletes a conversation only when it is empty and both `created_at` and `updated_at`
  are at or before the 365-day cutoff (`admin/retention.py`).
- Favourites and shopping lists are independent tables with no foreign key to a
  conversation; the purge does not know them.
- A title stored as a column of `conversations` is removed with the row and adds no
  message or activity, so it is compatible with KHD-34's retention model provided the
  rename does not move `updated_at`.

### 2.6 Security foundations to reuse

- Authenticated mutations already enforce a server-side session (`require_user`), an
  Origin check (`authenticated_mutation`), strict JSON body rules, a fixed error
  catalogue with correlation ids, and ownership through the query's own
  `WHERE ... user_id`.
- The Web page renders user text only through `textContent`; Telegram sends plain text.
- These foundations are sufficient for a title PATCH; no new concurrency architecture is
  needed.

## 3. Scope implications

The assessment supported the approved increment A decisions:

- use the existing `conversations.title` column; no migration;
- make rename/reset a small owner-scoped UPDATE that preserves `updated_at`;
- read the latest available message for display and ordering instead of trusting
  `updated_at`;
- centralize the ordering expression so Web and Telegram cannot diverge;
- present conversation times in Europe/Moscow independent of the viewer;
- derive Web day separators from the loaded history on every render rather than storing
  them.

It also identified the approved limits: semantic automatic titles and an archived
original semantic title are not available on this baseline and were not part of A1.

## 4. Reported Production baseline verification (2026-11-26)

The following is **previously reported operational evidence** supplied for this record.
It was not re-verified by running a production command while writing this document, and
this record does not extend it.

| Item | Reported value |
|---|---|
| Serving revision | `afd83cb65bd7cc6ab1ab6d638187115b05610c63` |
| Serving image | `house-helper:afd83cb65bd7` |
| Image ID | `sha256:83e9955bd752d2097df743019760fc56a79b40af60272863889b4761ec252457` |
| PostgreSQL | 16.15 |
| Schema | `0014_user_measure_overrides` |
| Accounts | Two |
| Public tables | 21 |
| Running services | Web, Telegram, Traefik, PostgreSQL operational |
| Purge and off-host backup (2026-11-26) | PASS |
| Committed off-host recovery points | Ten |
| Latest recovery point restored during verification | No |

### 4.1 Operational limitations preserved

The reported verification carried known limitations. They are recorded, not remediated
by PP-01-A1:

- the actual image-to-source mapping was a reported candidate, not an independently
  rebuilt digest comparison performed for this record;
- the latest off-host recovery point was not restored as part of that verification;
- deployment, backup/restore and runtime health remain operational concerns outside
  PP-01-A1;
- no claim is made that the working-tree PP-01-A1 code is deployed or that it replaces
  the reported serving revision.

## 5. What this record does not claim

- It does not claim the PP-01-A1 implementation existed at the planning baseline.
- It does not claim the reported Production values were re-measured here.
- It does not close PP-01.1A or full PP-01.1D.
- It does not change the HH-BRAND-01-FIX-01 release block (24 PASS / 6 FAIL).
