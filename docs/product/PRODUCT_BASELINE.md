# House Helper (Kitchen domain) — Product baseline (Stage 2A summary)

**Source:** owner handoff dated 2026-10-18, summarizing approved Stage 2A dated 2026-10-16. The product is now named **House Helper**; **Kitchen** is its first domain. The historical name "Kitchen Helper" in the source is preserved.

**Authority level:** summary of an accepted specification; **not the original 25 acceptance criteria**. Obtain the original before changing scope or making claims about exact AC wording.

## User and purpose

- First real user: project owner's mother; Russian-speaking, uses smartphone and desktop; the browser interface is important to her.
- All end-user interface labels, errors, recipe content, settings, instructions and voice interactions must be in **Russian**. English may be used for technical documents and code.
- The product is a personal cooking assistant, potentially an initial part of future House Helper, **not** a generic household platform in this MVP.
- Prioritize an actual usable Web MVP while preserving security and data separation.

## Equal-priority conversation scenarios

1. "Что приготовить из имеющихся продуктов?"
2. "Как приготовить конкретное блюдо?"
3. "Что приготовить вообще?" — open-ended inspiration.

All three belong to the same free-form conversation, rather than three rigid wizard flows.

## Accepted MVP capabilities — product commitments, not claims of implementation

| Area | Accepted requirement |
|---|---|
| Devices | Responsive full Web UI for desktop and smartphone with equivalent capability |
| Chat | Text conversation; continuation of multiple conversations per user; Web history sync across devices |
| Voice | STT and TTS; read a particular recipe step; configurable auto-read; text chat continues if voice fails |
| History | Permanently store user conversations for continuation; automatically delete **each message** 365 days after its creation |
| Favorites | Structured saved recipes with ingredients, amounts and steps; independently managed from chat history |
| Preferences | Extract durable preferences automatically; distinguish explicitly stated preferences, justified hypotheses, one-off wishes |
| Preference control | User can view, correct and delete extracted preferences; explicit correction wins; do not infer medical restrictions from repeated requests |
| Identity | Main login: Telegram; fallback: pre-approved, confirmed passwordless email; both methods map to the **same internal User** |
| Access | Initially closed allowlist, no public signup |
| Screens | Chat / Home, History, Favorite recipes, My preferences, Settings / Account, plus necessary login screens |

## Deliberately out of MVP

Meal planning calendar, automatic grocery shopping, calorie counting, generalized House Helper capabilities.

## Product vs implementation

**Current state (2026-10-31): the owner ACCEPTED the Functional Alpha on 2026-10-31 (REPORTED; production facts independently verified by Cursor).** The accepted alpha covers a two-user Kitchen deployment: successful account isolation, Telegram authentication, cooking workflows, history, favorites and cross-device synchronization. Production runs image `house-helper:3d09d45f6318` at schema `0009_message_attachments`; Telegram sign-in and the in-process Telegram bot are enabled, and email sign-in is disabled.

This table remains the list of **product commitments**, not a claim that every row is delivered. Still not delivered or not part of the accepted alpha: voice STT/TTS and per-step reading; the automatic extraction and user management of long-term preferences; equipment-aware cooking; family recipe digitization; Web photo upload. Passwordless email authentication exists in code but its operational activation (real SMTP/TLS/SPF/DKIM/DMARC) is deferred. The post-alpha proposals and their acceptance scope are in [`BACKLOG.md`](BACKLOG.md); do not present them as implemented.

**Historical implementation state:** as of the R3B-2 release report (2026-10-20), the backend persistence/FastAPI foundation, Telegram OIDC and passwordless email authentication were released in Git, and no user-facing Web Chat API, cooking Web UI, voice UI, favorites UI or preferences UI existed. That state is superseded by the deployed Functional Alpha above.

## Unresolved documentation requirement

Recover the original Stage 2A artifact listing all **25 acceptance criteria**. Link it here or replace this summary with a versioned reference without losing its original wording. Do **not** invent the missing detailed ACs from this summary.
