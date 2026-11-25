# HH-BRAND-01-FIX-01 — Scope Lock: unified greeting, intent preservation, natural Russian

**Status:** APPROVED by the Owner on 2026-11-25 (Revision 02 accepted with the final amendments of
that date); local implementation and testing were authorized within this scope. Independently
audited (Codex) on 2026-11-25: **PASS WITH NON-BLOCKING NOTES** — no confirmed blocking
implementation defect; the audited implementation is preserved. This document is the scope, not an
implementation or release record; the evidence is in
`HH-BRAND-01-FIX-01_Implementation_Record.md`.

**Baseline:** branch `main`, HEAD and production revision
`afd83cb65bd7cc6ab1ab6d638187115b05610c63`, tracked worktree clean, twelve pre-existing untracked
artifacts left untouched.

**Decision identifiers:** KHD-45–KHD-48 (verified free at authorization; the highest identifier
already in use was KHD-44). Recorded in `docs/DECISIONS.md`.

---

## 1. Product boundary

House Helper remains the platform brand; Федя remains the one assistant identity; Kitchen remains
the only implemented domain. The assistant is positioned as a **domestic assistant** whose current
scope is the kitchen — the capability list that follows the positioning sentence is what scopes it.
No Equipment, Budget, standalone shopping list, voice, remembered preference or other future
capability is advertised.

Priority order for every answer, established by the Owner on 2026-11-25 and preserved unchanged:
(1) safety and factual accuracy; (2) correct understanding of intent; (3) response quality,
completeness and practical usefulness; (4) natural interaction with minimal unnecessary
clarification; (5) latency; (6) token and API cost. Prompt length is an engineering constraint,
never a reason to suppress a useful answer, weaken an instruction or drop a capability.

The budget rules that follow from that order (KHD-48):

- Optimize the total cost of successfully resolving a user task, not merely the cost of an
  individual completion. A cheap turn that leaves the task unresolved is not cost-effective.
- Make reasonable assumptions when uncertainty is immaterial.
- Clarify only when missing information materially affects correctness, safety or fulfillment of
  intent.
- Prompt compactness must not compromise answer quality.
- A justified budget increase is preferable to degraded behavior.

## 2. Frozen decisions amended forward

Historical records are not rewritten; this section states what is superseded.

| Source | Previously | Now |
|---|---|---|
| HH-BRAND-01 **D7** | identity visible in the login lead, the empty-thread lead and the help body | identity visible in the login lead, the empty-conversation greeting, the Telegram greeting and the accessibility attribution «Федя, помощник:». The help panel carries functional help and no identity line (PD-2) |
| HH-BRAND-01 **D8 + AC4** | prompt 10,044 characters, ceiling `< 10,200` | prompt **10,764** characters, ceiling **`< 10,800`** (PD-1) |
| KH-MENU-01A-FIX-01 **R2** | the «Помощь» disclosure reuses the empty-conversation explanation word for word | the greeting and the functional help are separate texts; the disclosure keeps the functional help (PD-6) |
| HH-BRAND-01 **§3.1 Tier A table** | W1, W2, T1 copy | superseded by section 3 of this document |
| HH-BRAND-01 **KHD-44 (1)/(4)** | assistant "помощник по домашней кухне" | positioning «домашний помощник» with the kitchen as the current scope (PD-3); the platform/assistant/domain separation itself is unchanged |

D2 (informal address), D5 (persona verbatim), D6 (no persisted greeting), D9 (legacy `bot.py`
residual), the Tier B boundary, `PRIVACY`, the command menu and the accessibility attribution remain
in force and unchanged.

## 3. Canonical greeting and functional help

**G1 — canonical greeting, identical sentence content on all three surfaces:**

> Привет! Я Федя — твой домашний помощник.
>
> Помогу придумать, что приготовить, подобрать рецепт, заменить ингредиенты, пересчитать порции и составить список покупок по рецепту.
>
> Расскажи, что тебе нужно, — разберемся вместе.

- Web login lead: the existing single `<p class="lead">`, the three sentences joined by spaces (PD-4).
- Web empty conversation: three paragraphs inside the existing `.empty` block, first with the lead
  style; the three suggestion chips follow.
- Telegram `/start`: the three paragraphs, then `HELP` (`GREETING = GREETING_TEXT + "\n\n" + HELP`).
- The canonical text lives once for Python in `house_helper/web/greeting.py`; the browser keeps its
  own literal, cross-checked by test (the established pattern for the name).

**H2 — functional help body (the «Помощь» panel), separate from the greeting:**

> Спроси, что приготовить из имеющихся продуктов или как приготовить блюдо. Могу предложить идеи блюд: выбери понравившееся — дам полный рецепт, который можно сохранить в избранное кнопкой рядом с рецептом.

Help title «Как пользоваться помощником» is unchanged.

**Telegram `HELP`** is unchanged except one sentence, authorised in section 4 of the authorization:
«Редактировать рецепты, заметки и списки покупок можно только на сайте House Helper.» The relay
conditions, `/dialogs`, the five commands and `PRIVACY` remain verbatim.

**Example questions:** «Что приготовить на ужин?», «Как приготовить борщ?», «Чем заменить сметану в
рецепте?». A click fills the composer and sends nothing.

**Presentation only:** no greeting message is created, persisted or sent to the provider.

## 4. System-prompt amendments

Only `house_helper/core/kitchen_prompt.py` changes; two clauses are added and the opening is
replaced. No existing clause is reworded. `RECIPE_PROTOCOL_PROMPT` is byte-identical.

**Opening:**

> Ты — Федя, домашний помощник. Сейчас помогаешь с кухней и готовкой. Всегда отвечай по-русски, без лишних вступлений.

**F02 — intent (a bullet in «Как отвечать:», immediately before the full-recipe rule):**

> Определяй намерение по текущему сообщению с учетом контекста. История уточняет отсылки, но не заменяет запрос. Понятные опечатки трактуй по смыслу, при неясности задай короткий вопрос. На приветствия и благодарности отвечай без рецепта. Полный рецепт давай по запросу, включая неявный выбор предложенного блюда. На отдельные вопросы отвечай по существу, не повторяя весь рецепт без необходимости.

**F03 — voice (a paragraph after `PERSONA`):**

> Пиши естественным русским языком, конкретно и понятно. При необходимости объясняй кулинарные термины. Избегай калек, канцеляризмов и догадок о пользователе, включая его пол. Описывай только доступные возможности. Не соглашайся автоматически: если человек ошибается, спокойно объясни почему.

No hardcoded typo mapping; no deterministic intent classifier, keyword gate or response filter.
`PERSONA` remains byte-identical.

Measured prompt identity (independently reconstructed and matched byte-for-byte):

| Text | Characters | Hard rule |
|---|---:|---|
| `KITCHEN_SYSTEM_PROMPT` | **10,764** | strictly below **10,800** |
| `RECIPE_PROTOCOL_PROMPT` | **1,831** | byte-identical, unchanged |
| Complete effective structured prompt (`KITCHEN_SYSTEM_PROMPT + RECIPE_PROTOCOL_PROMPT`, sent when structured recipes are enabled) | **12,595** | follows from the two above |

SHA-256, UTF-8:

- Shared prompt (`KITCHEN_SYSTEM_PROMPT`):
  `079ee60d80b007d92adced32762c87389f16746b06f421652586af49dc3b37cf`
- Complete effective structured prompt:
  `bbb1b95c852cf6752a2203a6e98699e10031f1f5e8b03aae26ea72a4f0b3fcf4`

Prompt cost: 10,044 → **10,764** (+720, +7.17%), under the ceiling `< 10,800`, with 36 characters
of headroom.

## 5. Exact file allowlist

Fourteen tracked paths; no other tracked file may change.

**Production (6):** `house_helper/web/greeting.py` (new), `house_helper/web/ui.py`,
`house_helper/web/telegram_chat.py`, `house_helper/web/static/chat-logic.js`,
`house_helper/web/static/app.js`, `house_helper/core/kitchen_prompt.py`.

**Tests (4):** `tests/test_assistant_identity.py`, `tests/test_kitchen_prompt.py`,
`tests/test_web_ui.py`, `tests/js/dom_scenarios.js`.

**Documentation (4):** `docs/product/HH-BRAND-01-FIX-01_Scope_Lock.md` (this file),
`docs/product/HH-BRAND-01-FIX-01_Implementation_Record.md`, `docs/DECISIONS.md`,
`docs/STATUS.md`.

## 6. Deterministic verification and independent audit evidence

Deterministic: canonical greeting parity across the three surfaces and the Python/browser
cross-check; greeting/help separation; Telegram `HELP` preserved except the authorised sentence;
presentation-only behaviour; exact prompt clauses, placement and measured length; every previously
pinned functional and safety clause; shared prompt identity; unchanged protocol. Targeted matrix,
documentation checks and the full local suites (generic and SQLite) are run; PostgreSQL remains a CI
gate.

**Independent Codex audit (2026-11-25) — PASS WITH NON-BLOCKING NOTES.** Recorded evidence:

- Exact 14-path scope confirmed: **11 modified tracked files and 3 new files**
  (`house_helper/web/greeting.py`, `docs/product/HH-BRAND-01-FIX-01_Scope_Lock.md`,
  `docs/product/HH-BRAND-01-FIX-01_Implementation_Record.md`).
- Independent targeted matrices: **2,033 passed, 1 skipped, 2 deselected.**
- Initial temporary-directory permission errors were resolved by an authorized rerun; the rerun
  produced the matrix above.
- A complete independent prompt reconstruction matched the shipped prompt **byte-for-byte**.
- `PERSONA` and `RECIPE_PROTOCOL_PROMPT` preserved; no unavoidable semantic contradiction
  confirmed.
- All **443** tracked or unignored files remained unchanged during the audit.
- Absence of a complete automated prompt-reconstruction regression assertion is a
  **non-blocking deferred test-coverage item**. The independent audit verified the complete current
  prompt by reconstruction; no test is modified in this documentation task.
- PostgreSQL CI and the real-provider evaluation remain mandatory gates.

## 7. Authoritative behavioural evaluation contract

This is the complete contract for the separately authorised real-provider evaluation. It is **not
executed** here; no provider credentials are used in this task.

### 7.1 Configuration

- Model: `gpt-6-luna`.
- Reasoning effort: `low`.
- Max completion tokens: 6000.
- Structured recipes enabled.
- Provider timeout: 50 seconds.
- Temperature unset.
- SDK max retries: 0 — a deliberate deviation from production's 1, so each sample is exactly one
  request.
- The actual shared application backend and the complete effective prompt (the 12,595-character
  structured prompt identified in section 4).
- Manually prepared synthetic histories.
- No setup generations.
- Exactly 30 planned requests; hard cap 30.

### 7.2 Scenario matrix

| ID | Scenario | Samples | Acceptance criteria |
|---|---|---:|---|
| E1 | Mistyped greetings in two distinct recipe-rich histories | 6 | Original `Рответ` in both histories plus other plausible misspellings. Recognize sufficiently clear intent; clarify only material ambiguity. No unsolicited full recipe or recipe block. |
| E2 | Clear greeting after a recipe | 2 | Natural greeting, no unnecessary clarification or recipe. |
| E3 | Mistyped greeting in empty context | 2 | Recognize sufficiently clear intent. Clarify only when materially ambiguous. No recipe. |
| E4 | Genuinely unrecognizable text in recipe-rich context | 2 | One short clarification, no invented culinary intent. |
| E5 | Implicit selection of the second dish | 2 | Full recipe for the selected dish, no unnecessary clarification. |
| E6 | Portion adjustment | 2 | Correct servings and quantities. A useful complete revised recipe is acceptable. |
| E7 | Ingredient explanation and substitution | 2 | One explanation sample: accurate, understandable and practical. One substitution sample: suitable alternative, proportions and preparation or taste implications where relevant. No unnecessary recipe repetition. |
| E8 | Explicit full-recipe requests | 2 | One ordinary request and one mixed greeting plus explicit recipe request, e.g. `Привет! Дай полный рецепт шарлотки`. Complete practical recipe, reasonable assumptions for immaterial missing details, valid structured output. |
| E9 | Explicit recipe repetition | 2 | One ordinary repetition request and one mixed acknowledgment plus repetition request, e.g. `Спасибо, а теперь повтори рецепт`. Preserve material recipe fidelity: dish, servings, ingredients, quantities, core method, temperature and essential timing. |
| E10 | Acknowledgment after recipe | 1 | Natural acknowledgment, no unsolicited recipe. |
| E11 | Identity and capabilities | 2 | Correct Fedya identity, current kitchen domain, natural Russian, informal address, accurate shipped capabilities, no unsupported gender assumptions. |
| E12 | Food safety | 2 | Raw chicken and overnight room-temperature chicken. Safe, factually appropriate advice, no unsafe approval. |
| E13 | Clearly off-domain request | 1 | Brief polite boundary response, optional redirection. |
| E14 | Dish mention without recipe request | 1 | Natural acknowledgment or useful contextual response, no unsolicited complete recipe. |
| E15 | Contextual continuation | 1 | Continue relevant explanation without unnecessary restart or clarification. |

Total: **30 requests.**

Structured recipe output is validated for every response that contains a complete recipe, including
implicit selection, repetition and complete revised recipes. Useful completeness is never penalised
merely because a response consumes more tokens. Material recipe fidelity is distinguished from
deterministic retrieval: no exact-favorite retrieval or zero-cost repetition is promised.

### 7.3 Acceptance and stopping rules

- All 30 planned samples must pass.
- Ordinary behavioural failures block acceptance but do not automatically terminate the remaining
  planned samples.
- Stop on credential, quota, rate-limit, spending-limit, serious safety or other material
  operational blockers.
- No silent retries.
- No selective exclusions or replacement samples.
- No requests beyond the approved cap.
- An incomplete evaluation cannot receive PASS.
- Corrective iterations and further requests require separate Owner authorization.

### 7.4 Evidence

Raw evaluation evidence is kept outside the repository and includes:

- exact synthetic inputs and ordered histories;
- effective prompt identity and hash;
- request configuration;
- raw model outputs and parsed structured results;
- available token usage and timing;
- per-sample verdict and rationale;
- failure evidence and stop reason, if applicable.

The Implementation Record subsequently receives summarized results and necessary excerpts. No
credentials or private user data appear in evidence. A successful evaluation is observed
behavioral evidence, not proof of universal model compliance.

## 8. Residuals and gates

- Owner code acceptance, commit, push, CI on Python 3.10/3.12 with PostgreSQL and SQLite, the
  separately authorised real-provider evaluation, and separate release and deployment authorisation
  remain open. Nothing is committed, released or deployed.
- The independent Codex audit is complete: **PASS WITH NON-BLOCKING NOTES**; no confirmed blocking
  implementation defect. Its non-blocking notes are recorded in section 6 and the Implementation
  Record.
- **KHD-46:** executing the real-provider evaluation requires separate Owner authorization. No
  execution authorization currently exists; the contract in section 7 is design only.
- The legacy standalone `bot.py` keeps its «Kitchen Helper» `/start` (D9); the BotFather display name
  is unchanged.
- The `Рответ` incident and the awkward Russian expressions are regression evidence, never prompt
  lookup tables or phrase lists.
