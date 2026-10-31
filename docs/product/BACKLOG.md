# House Helper — Post-Alpha Feature Backlog

**Created:** 2026-10-31.

**Authority:** this is the single authoritative backlog for feature proposals after the accepted Functional Alpha. It records what has been discussed and accepted as scope; it does **not** approve implementation. Each item needs its own scoped task, design (where a schema change is involved), independent review and owner authorization.

**Baseline:** Functional Alpha **ACCEPTED by the owner on 2026-10-31** (REPORTED; production facts independently verified by Cursor on 2026-10-31): two-user account isolation, Telegram authentication, cooking workflows, history, favorites and cross-device synchronization. Production runs image `house-helper:3d09d45f6318` (commit `3d09d45f6318dc8e0a007c9b71738a6be65b5a3c`) at schema `0009_message_attachments`; Telegram sign-in and the in-process Telegram bot are enabled; email sign-in is disabled.

**Status legend:** `Proposed` — discussed or approved as a direction, not implemented. `Partially exists` — some foundation is deployed, but the item's acceptance scope is not met. `Implemented` — deployed and inside the accepted baseline (these are listed separately below, not as backlog work).

## Implemented baseline (not backlog items)

These capabilities are deployed and inside the accepted Functional Alpha. They are recorded here only to distinguish them from proposals:

- Two-user closed-allowlist sign-in through Telegram OIDC, with account isolation.
- Russian-language Web chat for the three Kitchen intents, with conversation history and cross-device synchronization.
- Telegram bot inside the Web process, sharing conversations with the page, with photos in history.
- Favorite recipes: saving any assistant answer as flat text, with title derived from the answer's first line, list/detail/delete, 100 per user, no expiry.
- 365-day message retention and the `purge-expired` operator command with a scheduled timer.
- Explicit migrations, startup schema guard, backup/verify/restore tooling, and the deployment kit.
- GPT-6 Luna support through Chat Completions with `OPENAI_REASONING_EFFORT`.

Not part of the accepted baseline and not implemented: voice/STT/TTS, long-term preference extraction and management, Web photo upload, equipment-aware cooking, family recipe digitization, structured recipe editing, and every item below.

## Summary

| ID | Title | Priority | Status | Implementation state |
|---|---|---|---|---|
| **KH-F06** | Structured Recipe Favorites | **P1 — FIRST** | Proposed | Partially exists: flat text favorites are deployed; structured recipes, editing and recipe cards are not built |
| **KH-F07** | Ingredient Context Continuity | **P1 — SECOND** | Proposed | Not built; conversation context is bounded and not a persistent ingredient store |
| **HH-ALPHA-01** | Long-term Preferences Memory | **P1 — THIRD** | Proposed | Not built; there is no preference table, CRUD or context injection |
| **KH-F01** | Household Measures and Recipe Unit Conversions | P2 (provisional; owner has not ranked) | Proposed | Not built |
| **KH-F02** | Concentration and Dilution Calculations | P2 (provisional; owner has not ranked) | Proposed | Not built |
| **KH-F03** | Home Preservation and Contextual Household Settings | P2 (provisional; owner has not ranked) | Proposed | Not built |
| **KH-F04** | Equipment-aware Cooking | P2 (provisional; owner has not ranked) | Proposed | Not built; retains the README meaning |
| **KH-F05** | Family Recipe Digitization | P2 (provisional; owner has not ranked) | Proposed | Not built; retains the README meaning |

ID check: `KH-F06`, `KH-F07` and `HH-ALPHA-01` had **no prior use** in this repository before this document (verified by search on 2026-10-31). `KH-F01`–`KH-F05` remain the planned-feature identifiers already used in the root `README.md`; no identifier is redefined.

---

## KH-F06 — Structured Recipe Favorites

**Priority:** P1 — **FIRST**.
**Status:** Proposed. **Partially exists:** the deployed favorites store an assistant answer as flat text; they are not structured recipes.
**Owner-approved direction?** The owner named this as the first post-alpha item; the acceptance scope below is recorded, not yet designed or implemented.

**Acceptance scope**

- Independently save each recipe from a multi-recipe AI answer (for example, three suggestions in one reply become three separately saveable recipes).
- Show no save-recipe control under ordinary non-recipe messages.
- Store clean structured recipes separately from conversational text.
- Automatically derive editable recipe titles.
- Favorites display recipe titles and open complete recipe cards.
- Manually edit the title, ingredients, quantities, preparation steps and personal notes.
- Preserve all legacy (flat-text) favorites and their accessibility.
- Maintain user isolation and cross-device synchronization.
- Make no additional LLM calls for manual editing or ordinary CRUD.
- Recipe categories and filtering are **P2**, not part of this item's first acceptance.

**Dependencies and constraints**

- Existing favorites foundation: `house_helper/web/favorites_api.py`, `house_helper/persistence/favorites.py`, table `favorite_recipes`, migration `0007_favorite_recipes`.
- The existing **`UNIQUE(user_id, source_message_id)`** constraint on favorites is a compatibility constraint: the current model allows at most one favorite per source message. Saving several recipes from one answer requires a design that either replaces that constraint in a backward-compatible migration or stores the recipes in a related structure, while preserving legacy rows and their access. **No schema migration is designed or approved in this backlog task.**
- Must reuse the server-side ownership model and the existing favorites routes' isolation rules.
- No additional provider calls for editing or CRUD; parsing/extraction happens only where the answer is first saved, under a separately approved design.
- Cross-device behavior follows the existing server-side favorites, not local storage.

**Implementation state**

- Deployed: flat-text favorites, derived title, list/detail/delete, 100-per-user limit, no expiry.
- Not built: structured recipe extraction, multi-recipe save, recipe cards, editing, notes, categories/filtering.
- Not designed: schema change and compatibility plan for the `UNIQUE(user_id, source_message_id)` constraint.

---

## KH-F07 — Ingredient Context Continuity

**Priority:** P1 — **SECOND**.
**Status:** Proposed.
**Owner-approved direction?** The owner named this as the second post-alpha item; no design or implementation exists.

**Acceptance scope**

- Retain previously confirmed available ingredients within the conversation context.
- Incorporate additional confirmed ingredients as the conversation continues.
- Distinguish confirmed availability from hypothetical discussion (for example, "if I had..." or "what if I bought...").
- Respect an explicit restriction to only newly provided ingredients.
- Assess a minimal prompt correction before proposing persistent ingredient state.
- Do not promise continuity beyond the available conversation context.

**Dependencies and constraints**

- The existing conversation context is bounded (20 messages) and is not a durable ingredient store; any promise must state that bound.
- The turn runner and shared Kitchen prompt are the likely points for a prompt correction; that assessment comes first and may be sufficient without new state.
- Persistent ingredient state, if later justified, needs its own product decision, schema design and retention rule. It must not silently create a second source of truth beside conversation history.
- Must not conflict with KH-F06 or HH-ALPHA-01: ingredient availability is not the same thing as a long-term preference.

**Implementation state**

- Not built. There is no ingredient table, no confirmed/hypothetical distinction and no persistent ingredient state.
- Required first step: a scoped assessment of a minimal prompt correction, with evaluation of clarification behavior and refusal to over-promise.

---

## HH-ALPHA-01 — Long-term Preferences Memory

**Priority:** P1 — **THIRD**.
**Status:** Proposed.
**Owner-approved direction?** The owner named this as the third post-alpha item; the two-stage scope below is recorded, not designed or implemented.

**Acceptance scope**

**Stage A — persistent preferences, explicit control**

- Domain-specific persistent preferences (Kitchen first; other domains later, if approved).
- Database storage with migration and ownership constraints.
- CRUD through user-facing management.
- Injection of relevant preferences into the LLM context.

**Stage B — selective extraction and safeguards**

- Selective automatic extraction from conversations.
- Correction precedence: an explicit user correction wins over an inferred preference.
- Safeguards against unsupported inferred preferences (including medical or safety-related inferences).
- Minimized paid API calls for extraction.

**Cross-cutting acceptance**

- Preferences survive conversation-history cleanup.
- Preferences remain independently editable and deletable by their owner.

**Dependencies and constraints**

- No preference storage exists today; `docs/DECISIONS.md` KHD-05 and KHD-06 record the product requirement.
- Stage A must define the domain boundary (KHD-15–KHD-19) so Kitchen preferences are not silently reused elsewhere.
- The `purge-expired` cleanup names its tables one by one (KHD-30); a future preference table must be explicitly added to the retention design as a never-purge table, and the existing guard test must be extended deliberately.
- Stage B depends on Stage A, a cost/rate strategy for extraction, and a review of false-positive risk; it must not block an ordinary answer if extraction fails.
- User-facing wording must be Russian and must explain what is stored and how to correct or delete it.

**Implementation state**

- Not built: no table, no CRUD, no management UI, no context injection, no extraction.
- Not designed: extraction triggers, cost bounds, correction precedence mechanics and retention treatment (beyond the requirement that preferences survive cleanup).

---

## KH-F01 — Household Measures and Recipe Unit Conversions

**Priority:** P2 (provisional; the owner has not ranked the P2 items).
**Status:** Proposed.

**Acceptance scope**

- Support everyday household measures (spoons, cups and similar units) in recipes and conversation.
- Convert between household measures and weight/volume where the assistant can do so safely.
- Keep conversion assumptions visible to the user; do not silently invent density or cup size.

**Dependencies and implementation state**

- Depends on structured recipe storage (KH-F06) if conversions are to be saved and edited with a recipe.
- Needs a defined conversion table and a policy for ambiguous ingredients.
- Not implemented.

---

## KH-F02 — Concentration and Dilution Calculations

**Priority:** P2 (provisional; the owner has not ranked the P2 items).
**Status:** Proposed.

**Acceptance scope**

- Support calculations for brines, syrups, marinades and similar preparations where concentration or dilution matters.
- Show the calculation basis and assumptions, and ask for missing values instead of guessing.
- Keep food-safety boundaries; do not turn the assistant into a medical or industrial calculator.

**Dependencies and implementation state**

- Depends on KH-F01 for household measures and on the preservation safety policy of KH-F03.
- Not implemented.

---

## KH-F03 — Home Preservation and Contextual Household Settings

**Priority:** P2 (provisional; the owner has not ranked the P2 items).
**Status:** Proposed.

**Acceptance scope**

- Contextual home-preservation guidance (storing, freezing, canning and similar), with clear safety boundaries and sourced caution where needed.
- Contextual household settings that affect advice (for example, equipment, household size or storage constraints), under the user's control.
- Avoid unverified canning or preservation advice; distinguish general guidance from a tested recipe.

**Dependencies and implementation state**

- Depends on HH-ALPHA-01 for household settings/preferences and on an approved safety policy for preservation.
- Not implemented. The current prompt only answers short storage/freezing questions and avoids unverified canning advice.

---

## KH-F04 — Equipment-aware Cooking

**Priority:** P2 (provisional; the owner has not ranked the P2 items).
**Status:** Proposed.
**Meaning preserved from the root `README.md`:** **Planned feature.** Appliance onboarding, so that suggestions fit the equipment a household actually has.

**Acceptance scope**

- Record the household's available appliances and constraints with user control.
- Make suggestions and steps fit that equipment, and say when a suggestion assumes an appliance the household does not have.
- Keep the equipment profile editable and separate from conversation history.

**Dependencies and implementation state**

- Depends on HH-ALPHA-01 Stage A (persistent, user-managed household data) or an equivalent approved household-profile design.
- Not implemented.

---

## KH-F05 — Family Recipe Digitization

**Priority:** P2 (provisional; the owner has not ranked the P2 items).
**Status:** Proposed.
**Meaning preserved from the root `README.md`:** **Planned feature.** Turning printed or handwritten family recipes into structured recipes, with a human review step before anything is saved.

**Acceptance scope**

- Capture a printed or handwritten family recipe (for example, from a photo) into a structured draft.
- Require a human review and correction step before saving.
- Store the reviewed result as a structured recipe with user ownership and isolation.
- Preserve the original text or image reference where the design allows, without promising OCR accuracy.

**Dependencies and implementation state**

- Depends on KH-F06 structured recipe storage and editing, and on the approved photo/vision path.
- Requires an explicit decision about image storage and retention; the current photo path is message-scoped and expires after 365 days.
- Not implemented.

---

## Rules for changing this backlog

- Do not mark an item implemented until its acceptance scope is deployed and verified; deployed foundations alone are not completion.
- Do not add an ID that collides with an existing identifier. Search the repository before adopting a new ID.
- Record a priority change, a scope change or a rejection as a dated row or a decision in `docs/DECISIONS.md`, not by silently rewriting history.
- Any schema change, new provider call, retention change or UI change needs its own scoped task and owner authorization.
