# Kitchen Helper — Product baseline (Stage 2A summary)

**Source:** owner handoff dated 2026-10-18, summarizing approved Stage 2A dated 2026-10-16.

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

As of the **R3B-2 release report**, the backend persistence/FastAPI foundation, Telegram OIDC and passwordless email authentication were released in Git. There is **no user-facing Web Chat API, cooking Web UI, voice UI, favorites UI or preferences UI**. Production deployment and first real-user validation have not occurred. Do not present this requirements table as a list of delivered features.

## Unresolved documentation requirement

Recover the original Stage 2A artifact listing all **25 acceptance criteria**. Link it here or replace this summary with a versioned reference without losing its original wording. Do **not** invent the missing detailed ACs from this summary.
