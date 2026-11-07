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
| **KH-F06** | Structured Recipe Favorites | **P1 — FIRST** | Implemented | Structured recipes, per-recipe favorites, recipe cards and editing are deployed (migration `0010_structured_recipe_favorites`). Telegram per-recipe save (KH-FAV-TG-01) is implemented in the working tree on 2026-11-06, not released or deployed. |
| **KH-F07** | Ingredient Context Continuity | **P1 — SECOND** | Proposed | KH-F07A (40-message window and prompt rules) implemented in the repository on 2026-11-05; not released or deployed. No persistent ingredient state |
| **KH-MENU-01A** | Smart Meal Recommendations | **P1** — from real-user feedback (owner-reported) | Implemented; reworked by KH-MENU-01A-FIX-01 after a live smoke test, then by its Codex-FAIL remediation | Shared-prompt correction plus one help disclosure; no new mode, state, table or migration. Baseline `3194b5b` is deployed (owner-reported); FIX-01 and its Codex-FAIL remediation are in the working tree, not released or deployed; OWNER ACCEPT pending |
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
- Deployed in a later increment: structured recipes, per-recipe favorites, recipe cards and editing (migration `0010_structured_recipe_favorites`; the `UNIQUE(user_id, source_message_id)` constraint was replaced by `UNIQUE(user_id, source_message_id, source_recipe_index)`).
- **KH-FAV-TG-01 (Telegram per-recipe save) implemented in the working tree on 2026-11-06, NOT committed, released or deployed:** inline `Сохранить рецепт` buttons under answers that carry structured recipes, saving through the existing favorites storage; no schema change and no provider call. See `docs/STATUS.md`, "Working tree — KH-FAV-TG-01 Telegram recipe favorites (IMPLEMENTED, NOT RELEASED)".
- Not built: categories/filtering.

---

## KH-F07 — Ingredient Context Continuity

**Priority:** P1 — **SECOND**.
**Status:** Proposed, with **KH-F07A implemented in the repository on 2026-11-05** (not committed, released or deployed).
**Owner-approved direction?** The owner named this as the second post-alpha item. After the read-only preflight the owner approved implementation of **Option A** (prompt correction plus an expanded 40-message window) as **KH-F07A**; no persistent ingredient state was approved.

**Acceptance scope**

- Retain previously confirmed available ingredients within the conversation context.
- Incorporate additional confirmed ingredients as the conversation continues.
- Distinguish confirmed availability from hypothetical discussion (for example, "if I had..." or "what if I bought...").
- Respect an explicit restriction to only newly provided ingredients.
- Assess a minimal prompt correction before proposing persistent ingredient state.
- Do not promise continuity beyond the available conversation context.

**Dependencies and constraints**

- The existing conversation context is bounded (**40 messages since KH-F07A**, 20 before it) and is not a durable ingredient store; any promise must state that bound.
- The turn runner and shared Kitchen prompt are the points of the KH-F07A correction; no new state was introduced.
- Persistent ingredient state, if ever justified, needs its own product decision, schema design and retention rule. It must not silently create a second source of truth beside conversation history.
- Must not conflict with KH-F06 or HH-ALPHA-01: ingredient availability is not the same thing as a long-term preference.

### KH-F07A — 40-message window and prompt-only ingredient continuity (implementation scope, 2026-11-05)

**Owner-approved scope, implemented in the repository at baseline `9b71713`; not committed, released or deployed.**

- `MAX_HISTORY_MESSAGES` raised from 20 to 40 in `house_helper/core/limits.py`, shared by the web chat and the in-process Telegram bot.
- One request carries at most 40 messages including the one being answered. The web path already did this (`build_context`); the standalone Telegram service now bounds its request view (`context_manager.model_context`) while keeping its deferred post-success trim, so a failed turn still loses nothing.
- Chronological order, the rule that a window never opens on an assistant answer, conversation/account isolation, message expiry (365 days), the reset boundary and failed-turn exclusion are unchanged. No retention or persistence change.
- The shared Russian `KITCHEN_SYSTEM_PROMPT` gains a compact ingredient block: the working set grows, explicit removals and substitutions apply, quantities are limits, confirmed differs from hypothetical, the latest explicit correction wins, a suggested recipe neither consumes ingredients nor proves availability, missing information is asked for rather than pretended, and this set is not a long-term preference.
- Deterministic tests cover the window boundary (exactly 40 and more than 40), add/correct/substitute/quantity visibility, new-conversation and second-account isolation, shared Web/Telegram conversations, failed-turn exclusion, and the presence of every prompt rule. They do **not** prove that a real model follows the rules; that needs a separately approved real-provider evaluation.
- **Future Help copy (draft, NOT implemented — there is no Help interface yet):** «House Helper учитывает до 40 последних сообщений текущего разговора. Вы можете дополнять список продуктов, уточнять количества и сообщать, каких ингредиентов больше нет. В длинных разговорах ранняя информация может перестать учитываться. При необходимости напомните помощнику важные детали.»

### KH-F07B — Long-conversation continuity (optional, future, NOT approved)

If real use shows that ingredient information falls out of the 40-message window often enough to matter, KH-F07B would need its own scoped proposal and owner approval. Candidate directions — none chosen or designed — are summarization of older turns, an explicit structured ingredient state, or a user-visible way to restate the working set. Any of them must define correction, deletion and retention semantics and must not silently become a second source of truth beside conversation history. This item is recorded as a possibility only; it is not part of KH-F07A.

**Implementation state**

- KH-F07A implemented and tested in the working tree; no release, deployment or real-provider evaluation.
- Not built: any persistent ingredient state, pantry, automatic stock deduction, long-term preference extraction or cross-conversation memory.
- The test suite pins the prompt rules' presence and the window's deterministic behavior, not the model's semantic compliance.

---

## KH-MENU-01A — Smart Meal Recommendations

**Priority:** P1 — from real-user feedback (owner-reported, 2026-11-07).
**Status:** Implemented and committed as `3194b5bbd3b75045b89aafce48440157d6e67183` on baseline `8a6a770`, including a Codex-audit remediation (R1–R3: three-state availability, scoped no-shopping restrictions and five scenario tests). The task brief records the baseline as **deployed in production** (REPORTED). A live Web smoke test then found the two behavioral defects and the help gap corrected by **KH-MENU-01A-FIX-01**, which is implemented and tested **in the working tree only** — not committed, pushed, released or deployed. A second independent Codex audit FAILed that working tree on three further points (a salt/oil/water availability exception, the Web help reachable only in an empty conversation, and necessity collapsed into availability); the same working tree now carries that remediation. **OWNER ACCEPT remains pending.**

**User feedback.** A user asked for New Year dishes and supplied the ingredients she had. The suggestions she received were limited to those ingredients. She expected festive, interesting, budget-conscious options, including additional ingredients to buy.

**Acceptance scope (the task brief's criteria)**

- General requests for festive or special-occasion meals are not constrained to previously mentioned ingredients unless the person explicitly asks for that restriction.
- "Special but budget-friendly" produces realistic named dishes, modest-cost ingredient choices and cheaper substitutes where appropriate; no invented current prices.
- Available ingredients are reused when helpful; missing ingredients may be suggested for purchase.
- Explicit no-shopping instructions remain binding within the scope they name.
- Multi-turn changes of intent work: inventory → holiday suggestions → budget refinement → recipe → shopping list.
- Concrete, varied recommendations, not generic dish-category placeholders.
- Never invent known inventory; ask one concise clarification only when necessary.
- Respect explicit dietary restrictions and allergies; do not claim that persistent culinary preferences are stored.
- Identical recommendation semantics in the shared Web and Telegram flow.
- Preserve KH-F07A ingredient-context continuity, KH-TG-DLG-01 dialog selection, favorites and account isolation.

**Implementation**

- The correction is in the one shared `KITCHEN_SYSTEM_PROMPT` (`house_helper/core/kitchen_prompt.py`), which the web backend (`StandaloneOpenAIBackend`) and the Telegram adapter (`api_client.SYSTEM_MESSAGE`) already send byte-for-byte. No new mode, persistent inventory, database table, migration, authentication feature or price API.
- The prompt now states that "cook from what I have", "cook a named dish" and "choose dishes (including for a holiday)" are different tasks whose frames do not carry over unless the person set a common restriction; that ingredient availability has three states — confirmed present, confirmed absent, and unknown because the conversation did not mention it — with unknown never treated as missing, an exhaustive inventory taken at its word, only confirmed-absent required items on the shopping list, and required items of unknown availability introduced as "проверьте, есть ли у вас это; купите, если нужно"; that salt, oil, water and other staples get no exception — unknown unless named, with no stated-assumption shortcut — and are spoken of conditionally rather than asked about one by one; that named ingredients are an optional resource, not a base or a boundary, when the task shifts to an open dish choice with shopping allowed (KH-MENU-01A-FIX-01 supersedes the earlier "base" wording), missing ones may be suggested for purchase, and an open menu may include dishes requiring new main ingredients; that necessity and availability are independent dimensions, so a required ingredient the person never mentioned stays required, goes to the "проверить" section when its availability is unknown, and is not called an optional addition; that a festive/menu request gets 2–4 concrete, varied named dishes rather than generic categories and a full recipe only after a dish is chosen; that budget requests get inexpensive products and cheaper substitutions without invented current or exact prices; that an explicit no-shopping ban is scoped to the frame it names (a dish, a meal, an occasion or a period), with a task-scoped ban not outliving its task, a period ban lasting until the period ends, an indefinite ban lasting until lifted or replaced, a change of task not cancelling an ongoing ban, explicit permission within the task superseding it, and one short question for a materially ambiguous scope; that a shopping-list follow-up names what is missing for the chosen dish; and that preferences stated in the conversation are kept while persistent culinary preferences are not stored and no memory after the conversation is promised.
- Conversation assembly is unchanged: both channels already pass the ordered history (40 messages including the pending one, KH-F07A), so the model is given the inventory turn, the holiday request and the later refinements.
- Tests: `tests/test_kitchen_prompt.py` and `tests/test_menu_recommendations.py` pin the prompt contract, the ordered multi-turn history on both channels, the verbatim no-shopping message, the shared prompt actually sent by both adapters, the five Codex-audit scenarios (a task-scoped restriction, an ongoing restriction, permission after a prohibition, a partial inventory, and allergy/diet continuity), and the KH-MENU-01A-FIX-01 plus Codex-FAIL regressions (inventory → festive menu, explicit inventory opt-out, unknown availability, unknown salt/oil/water, required-but-unmentioned main ingredients, confirmed-missing versus unknown shopping items, and ideas → recipe → favorites). The Web help entry is exercised behaviourally in `tests/js/dom_scenarios.js` (run by `tests/test_web_ui_dom.py`) in an active conversation and in an empty draft on a narrow screen.

**Live smoke remediation (KH-MENU-01A-FIX-01, working tree 2026-11-07)**

The deployed baseline was tested by a real person on the Web. Three findings:

1. **Inventory anchoring.** After an inventory turn, an open festive request that allowed shopping still produced dishes centered on the inventory; after the person explicitly said using it was optional, the menu still mostly built on it. The prompt called the named products a "base" and the working set said not to lose them, without a task boundary. The correction makes them an optional resource after the shift, requires at least one of 2–4 varied dishes not to be built on them, removes the anchoring on an explicit opt-out, forbids artificially excluding useful products, and defines "varied" as different in main product or cooking method.
2. **Ingredient availability.** Salt, water and oil were silently assumed; unmentioned eggs, cucumbers and mayonnaise became definite purchases. The correction keeps the three states, splits the shopping list into `«точно купить»` (confirmed absent and needed only) and `«проверить»` (needed, availability unknown), and keeps the staples rule as a stated assumption — which the Codex-FAIL remediation then removed (below).
3. **Ideas versus recipes.** The intended ideas → chosen dish → full recipe → save-to-favorites workflow worked (a real recipe was saved), but was not explained. The shared prompt now asks for the exact offer `«Выберите понравившееся блюдо, и я дам полный рецепт. Его можно будет сохранить в избранное»` after a list of dishes; the Web empty-conversation hint and Telegram `/help` explain the workflow, including Telegram's limits (a save button only when the answer carries one; editing only on the site).

**Codex-FAIL remediation (the same working tree, 2026-11-07)**

An independent Codex audit FAILed that working tree on three points:

1. **R1 — the staple exception.** The prompt still let salt, oil and water be assumed available if the assumption was stated. The remediation removes the exception entirely: every ingredient, staples included, is confirmed present, confirmed absent or unknown, nothing is assumed available by default or as a stated assumption, and unknown basic products are spoken of conditionally instead of being interrogated one by one.
2. **R2 — the Web help entry.** The explanation was reachable only in an empty conversation. A `«Помощь»` disclosure now sits in the conversation header: it opens the same explanation (one shared string in `chat-logic.js`) in empty and active conversations, on desktop and in the narrow layout; it sends no request and does not clear or change the conversation. Telegram `/help` behavior is preserved.
3. **R3 — necessity versus availability.** The rule "a product not mentioned in a suggestion is a possible addition, not a required ingredient" collapsed the two dimensions. The remediation separates them: an open menu may include dishes requiring new main ingredients; a required ingredient the person never mentioned stays required; unknown availability does not make it optional; required confirmed-missing items go to `«точно купить»`, required unknown-availability items to `«проверить»`, and only genuinely optional additions use `«если есть»` / `«можно добавить»`.

The correction touches `house_helper/core/kitchen_prompt.py`, the Web help entry (`house_helper/web/ui.py`, `house_helper/web/static/app.js`, `house_helper/web/static/app.css`, `house_helper/web/static/chat-logic.js`) and Telegram's existing help text (`house_helper/web/telegram_chat.py`). No intent router, new state, storage, migration or Telegram saving feature was added. The prompt is **8,462 characters** (from **6,960** before FIX-01; **8,049** after the first correction, so +413 / +5.1% for this remediation); the test ceiling is **8,550**.

**Limitations**

- Prompt-only. The deterministic tests do not prove that a real model obeys the rules; that needs a separately approved real-provider evaluation.
- No price data is fetched or stored; the assistant is instructed to avoid current and exact prices, and any cost language remains qualitative.
- Continuity is bounded by the 40-message context window (KH-F07A); there is no persistent inventory or long-term culinary preference store. The prompt says that persistent culinary preferences are not stored and that no memory after the conversation is promised; a future HH-ALPHA-01 preference store would have to revise that clause.
- The no-shopping restriction, its scope, and the dietary/allergy conditions are prompt commitments, not application-enforced state or filters. The application stores no restriction or inventory; the model infers the frame from the conversation and is told to ask one short question when the scope is materially ambiguous.
- A real-provider evaluation (not run) should repeat the reported sequence — inventory, New Year request, "cheaper", recipe choice, "what to buy" — plus the explicit no-shopping, inventory-opt-out, unknown-availability, unknown-staples and required-but-unmentioned-main-ingredient variants, and judge the answers for concrete named dishes, permitted purchase suggestions, correctly separated required/optional and confirmed-missing/unknown items, and no invented prices.
- The Web help entry is verified in the Node mini DOM (no layout, style, real focus or scrolling), not in a real browser or on a real phone; its narrow-width placement is CSS-only and unverified visually.
- The baseline `3194b5b` is deployed (owner-reported); the KH-MENU-01A-FIX-01 remediation and its Codex-FAIL remediation are a working tree only. Their deterministic tests do not prove compliance, and the live smoke test has not been re-run after either correction. No owner acceptance for the baseline or either remediation is claimed here.

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
