# House Helper — PP-01 UX Product Specification

| Field | Value |
|---|---|
| Version | 1.0 |
| Date | 2026-11-26 |
| Package | PP-01 — UX Completion |
| Stream | Product Delivery |
| Product | House Helper / Fedya / Kitchen |
| Status | PRODUCT REQUIREMENTS AGREED IN DISCUSSION — DOCUMENT READY FOR OWNER REVIEW |
| Repository destination | `docs/product/PP-01_UX_Product_Specification_v1.0.md` |
| Planning baseline | `b086ac2` on `main`; CI #97 PASS (Owner-confirmed) |
| Implementation / release | NOT AUTHORIZED |

## 1. Purpose and authority

Improve the everyday usability of Fedya across the journey from finding a conversation to choosing a dish and using its recipe. The goal is a useful, natural assistant, not a menu-driven cookbook.

This record consolidates the Owner's product decisions of 26 November 2026 and separates them from proposed technical mechanisms and unresolved product details. Document acceptance does not authorize implementation, provider calls, database changes, Git branching or production deployment. Each implementation increment requires a separately accepted scope and applicable evidence.

Governing references (repository-relative):

- `docs/architecture/HH-BRAND-01-FIX-02_Research_Charter_v1.0.md`.
- `docs/architecture/HH-BRAND-01-FIX-02_Incremental_Delivery_Strategy_v1.0.md`.
- `docs/architecture/HH-BRAND-01-FIX-02_AG-01_Architecture_Assessment_Record.md`.
- `docs/product/HH-BRAND-01-FIX-01_Behavioral_Evaluation_Record.md`.
- `docs/DECISIONS.md` (including KHD-48–KHD-52, as applicable).

The current main baseline contains the behaviorally unaccepted FIX-01 candidate (24 PASS / 6 FAIL); its production release remains blocked. AG-01 is closed. RP-01 / AG-02 is separately authorized and in progress in the Intelligence Research stream. PP-01 does not change its scope. Current serving-image provenance is still an open release prerequisite; `afd83cb` is a reported candidate source baseline, not a newly verified running-image mapping.

## 2. Product principles

The following principles consolidate the agreed interaction direction. Their general application beyond PP-01 is proposed for documentary Owner acceptance.

| ID | Principle | Meaning |
|---|---|---|
| UX-P01 | Conversation First | Natural conversation remains a first-class way of using Fedya. |
| UX-P02 | Interaction Freedom | Controls supplement conversation; they do not replace it or require users to adopt a rigid interaction flow. |
| UX-P03 | User Control | Explicit user choices take precedence over automatic UI decisions. |
| UX-P04 | Reversibility | Users can undo their own presentation settings where meaningful, without losing underlying content. |
| UX-P05 | Semantic Freedom | A conversation title is navigation metadata, not an instruction, topic constraint or domain selector. |
| UX-P06 | Object Integrity | Viewing a saved object must not regenerate or silently alter it. |
| UX-P07 | Cross-channel Consistency | Shared conversation and recipe data remain consistent across Web and Telegram; controls may follow channel-native conventions. |
| UX-P08 | Graceful Degradation | Failure of an auxiliary UX operation must not unnecessarily interrupt normal conversation. |

Russian user-facing copy must be natural, readable and consistent with the existing informal address policy. Do not introduce new scripted conversational responses. Preserve existing Russian spelling conventions (including `е` rather than `ё` where applicable). The behavioral quality of informal address remains within I-01, not a PP-01 prompt-remediation task.

## 3. Scope inventory

| ID | User-facing requirement | Product decision | Dependency classification (preliminary) |
|---|---|---|---|
| PP-01.1A | Stable meaningful automatic conversation titles | Agreed | Semantic title creation and persistence; possible inference dependency |
| PP-01.1B | Manual conversation renaming | Agreed | Persistent owner-scoped mutation |
| PP-01.1C | Explanation of title formation | Agreed | Presentation / help |
| PP-01.1D | Restore original automatic title | Agreed | Title lifecycle and persistence |
| PP-01.2 | Conversation creation and last-message timestamps; message chronology | Agreed | Data / presentation |
| PP-01.3 | Select a suggested dish using a `Полный рецепт` button | Agreed, primary recipe-interaction priority | Semantic option reference, normal turn execution and recipe generation |
| PP-01.4 | Expandable completed-recipe card | Agreed | Existing structured recipe object / presentation |
| PP-01.5 | Separate convenient full-recipe view | Agreed | Existing structured recipe object / presentation |

Dependencies are planning classifications, not approved architecture. In particular, automatic title creation and dish selection cannot be assumed to be display-only changes. Persistent rename is not a purely read-only UI change.

## 4. PP-01.1 — Conversation titles and management

### 4.1 Automatic titles (PP-01.1A)

- Generate a concise, meaningful title using the first substantive messages of the conversation. The title should capture its initial topic, not merely copy an opening greeting or vague first message.
- One sufficiently informative request may be enough; do not require an arbitrary fixed number of turns as a product rule.
- Once a meaningful automatic title has been established, preserve it. Subsequent changes in topic do not automatically rename the conversation.
- A user may discuss any number of unrelated recipes or topics in the same conversation. Do not warn, redirect, create a new conversation, or limit answers merely because the title no longer describes the latest topic.
- The title must not become a model instruction or semantic constraint on future conversation.
- Do not automatically retitle the entire existing conversation history. Preserve existing titles and access to old dialogues; define any opt-in or transitional handling separately.
- The displayed title and its changes must be consistent across Web and Telegram wherever the conversation is presented.

Illustrative examples (not templates or fixed outputs):

| Initial messages | Possible title |
|---|---|
| `Привет, Федь!` followed by a request about chicken for dinner | `Ужин из курицы` |
| `У меня вопрос` followed by a discussion of bread | `Выпечка домашнего хлеба` |
| Several messages about preparing zucchini | `Блюда из кабачков` |

A greeting alone must not be interpreted as evidence for a fabricated culinary topic. A proposed temporary label is `Новый диалог` until sufficient substantive information exists. Exact provisional-state handling, title generation timing and fallback policy remain open for scope lock.

### 4.2 Manual renaming (PP-01.1B)

The user can set a custom title without modifying messages, recipe objects, favorites or conversation identity. The custom title overrides the automatic title and must not be overwritten by any later automatic title update or topic change. The change persists across reloads and synchronized surfaces. Access is restricted to the authenticated conversation owner.

The initial automatic title must remain recoverable even while a custom title is displayed. The logical distinction between automatic and user-controlled title is required; no particular table, column or storage design is approved.

### 4.3 Explanation (PP-01.1C)

Provide an accessible explanation near the title or in its menu, and make it available through general help where supported. It must be usable by touch as well as by pointer; hover alone is insufficient. Do not require the user to ask Fedya or make an inference request to read the explanation.

Illustrative Russian copy, subject to final UI review:

> Название создается автоматически по первым содержательным сообщениям диалога. Если тема изменится, название останется прежним. Ты можешь переименовать диалог или восстановить его автоматическое название в любое время.

### 4.4 Restore automatic title (PP-01.1D)

The user can remove the custom override and restore the original established automatic title. Restoring must not generate a new title from the later conversation, send a new model request, change messages, or reset conversation chronology. The original title remains stable even after multiple rename/restore cycles.

For legacy conversations without an established automatic title, or a manual rename performed before automatic naming completes, the exact initialization/restore behavior remains an explicit scope-lock decision. Never fabricate a supposedly original title or overwrite the user's chosen title to resolve that state.

### 4.5 Reliability and interaction constraints

- Automatic naming failure must not interrupt a normal chat turn or lose an answer.
- Avoid an auxiliary model request on every message. Any inference-based title mechanism needs separately justified call accounting, cost, latency, privacy and failure handling.
- A manual rename racing with automatic naming must preserve the manual override. Retried operations must not cause unexpected reversion or duplicate state.
- Validate titles for usability and safe rendering. Exact maximum length, whitespace handling, invalid-input feedback and duplicate-title policy remain open; do not silently truncate a user's submitted custom title without an agreed policy.

## 5. PP-01.2 — Conversation and message chronology

### 5.1 Conversation list

Expose two distinct absolute date/time values in each conversation entry:

1. Start/creation date and time of the conversation.
2. Date and time of its last message.

Show their meaning clearly. A relative label such as `вчера` must not substitute for the creation date or the absolute last-message date. Relative hints may be supplementary only if useful.

Illustrative display (fictional values):

```text
Рецепты домашнего хлеба
Создан: 23.11.2026, 09:15
Последнее сообщение: 25.11.2026, 22:17
```

Use `DD.MM.YYYY` and 24-hour `HH:mm` for the conversation list. A conversation's creation time must remain stable after later replies, rename, restore or automatic naming. Returning to an old conversation moves it according to its newest message, not its creation date. Sort by last message descending. Renaming is not message activity and does not by itself change either timestamp or ordering.

### 5.2 Inside the conversation

- Show a readable time for each message.
- Introduce a date separator when the displayed calendar day changes, including the first displayed day, so that a long conversation can be followed across multiple days.
- Do not require repeating the full date beside every individual message when a date separator already establishes it.
- Retain readable chronology when loading older messages and returning to an existing dialogue.
- Respect the existing user text-size setting and responsive mobile layout.

### 5.3 Time semantics requiring verification

The intended timestamp meanings are agreed; exact data mappings are not yet verified against the selected release base. Determine whether creation time represents record creation or first substantive message, and how to handle an empty dialogue, pending/failed turns, retention-expired messages and historical records with missing dates. Do not label a timestamp `Последнее сообщение` if it actually reflects an unrelated row update.

Proposed policy: store instants consistently in UTC and display in the user's intended time zone. Confirm the desired time-zone source, behavior across travel and device changes, Web browser-local versus Telegram display, and day-boundary rules before implementation. Existing timestamps and any migration requirements must be inspected, not assumed.

## 6. PP-01.3 — Recipe Selection (`Полный рецепт`)

### 6.1 Primary user journey

The Owner's original priority is to select a dish proposed by Fedya without typing a new chat message manually. A button beside or below an offered dish is an alternative way of communicating the same intention as `Дай рецепт второго`.

Example interaction:

```text
User: Федь, что можно приготовить из курицы, картофеля, грибов и сливок?
Fedya:
1. Курица с грибами в сливочном соусе
   [Полный рецепт]
2. Картофельная запеканка с курицей
   [Полный рецепт]
3. Жюльен с курицей и грибами
   [Полный рецепт]
```

Selecting the second button sends a request for the full recipe of the second specific dish to Fedya. Fedya responds in the current conversation, using relevant established context. The user can instead type `Дай рецепт второго` or `Давай второй, только без сливок` without using any control.

### 6.2 Functional requirements

| ID | Requirement |
|---|---|
| RS-01 | Each actionable offered dish in a recipe-selection response has its own unambiguously associated `Полный рецепт` control. Incidental mentions of dishes are not automatically selections. |
| RS-02 | Activating a control initiates a normal request to Fedya for that specific dish's full recipe; it is not merely a display expansion. |
| RS-03 | Selection requires no manual text entry. |
| RS-04 | Preserve an unambiguous reference to the selected offer; later messages, duplicate dish names or list positions must not silently retarget it. |
| RS-05 | Preserve applicable previously agreed ingredients, availability, servings, equipment where known, constraints and preferences. Do not override them or ask again for information already known and applicable. Clarify only when genuinely needed. |
| RS-06 | Generate a full recipe only on request. Do not generate all possible recipes in advance just to present controls. |
| RS-07 | Ordinary conversational selection, modifications and follow-up remain first-class supported paths. |
| RS-08 | The resulting recipe is delivered in the current dialogue using the ordinary conversation lifecycle. |
| RS-09 | Support the intended Web and Telegram interaction through channel-appropriate controls and shared semantics; any staged surface limitation requires explicit scope approval. |
| RS-10 | If control interaction fails or is unavailable, the user can continue in text; do not falsely report that a request or recipe was completed. |

Record the button-originated request visibly in the conversation as a user action understandable on later review. Its exact copy and internal representation are scope-lock matters. It must not masquerade as a message authored by the assistant.

### 6.3 Semantic integrity and safety

Both interaction paths should converge on the same intellectual interaction, not independently generate recipes through separate pipelines. This is a product/architecture principle, not a mandate for a specific API or protocol. The system must preserve the selected offer and relevant original constraints while respecting subsequent explicit user changes. It must not treat an old suggestion as fresh authority over current user instructions.

An expired, inaccessible, stale or ambiguous offer must not silently resolve to a different dish. Define a truthful recovery or clarification path. Protect against foreign conversation references, tampered callbacks and duplicate delivery. Reuse the existing authorization, admission, idempotency, turn lifecycle and recipe-validation foundations where appropriate.

The button flow is distinct from opening an already completed recipe. Whether repeated deliberate selection should create a fresh recipe, reuse an existing result or offer a choice remains open; accidental double activation and retries must not produce unintended duplicate turns.

Do not prescribe regex extraction from arbitrary assistant prose, a structured protocol extension, a new classifier or a specific semantic operation layer. Establish an evidence-based design. If a new model-visible contract or changed prompt is necessary, apply relevant research and behavioral gates before release. Ordinary textual selection is a non-regression requirement, not a workaround for an unreliable button.

## 7. PP-01.4 — Expandable completed-recipe card

Present a valid, already obtained structured recipe in a compact form that can be expanded to read its complete contents and collapsed again. The initial default expansion state is an open UI decision; users must be able to reach the full recipe without scrolling through unrelated conversation or issuing another request.

Use the stored recipe's actual title, servings, ingredients, steps and any other available supported metadata. Do not invent a cooking time, portion count, summary or missing facts to fill the card. Preserve quantities, units, ingredient names, preparation sequence and qualifications; respect existing deterministic presentation/measure contracts. Do not silently reconstruct a structured recipe from arbitrary legacy prose.

Expanding/collapsing must not call the LLM, mutate the recipe or modify conversation chronology. Preserve surrounding conversational explanations and safety information, access to existing favorites and shopping-list functions, and the correct association when one reply contains multiple recipes. If no valid structured object is available, retain readable original text and an honest unavailable state; define any fallback scope separately.

## 8. PP-01.5 — Separate full-recipe view

Allow a user to open an existing valid stored recipe in a convenient dedicated reading view without surrounding conversation clutter. The control is distinct from `Полный рецепт` on an offered dish; it opens existing content rather than requesting generation. Proposed labels include `Открыть рецепт` or `Режим просмотра`, subject to UI review.

The view must contain the complete stored recipe and applicable presentation annotations, support readable text on smartphone and desktop, honor text-size preferences, and allow a return to the originating conversation without losing its contents. No additional inference or recipe transformation is implied by opening it. Do not invent unavailable recipe data. Respect ownership, retention and existing saved-copy semantics. A missing or expired source must not be silently recreated. Existing independent favorite copies remain governed by their own contracts.

The exact Web/Telegram presentation pattern and scope for existing favorites versus conversation recipes require design confirmation. Telegram may use a channel-appropriate equivalent rather than a literal copy of a Web panel. No assumption of identical interface mechanics is made.

Interactive cooking progress (ingredient/step checkmarks, persistence, timers and related automation) is future scope, not an acceptance requirement for the dedicated reading view.

## 9. Acceptance scenarios

These are product-level acceptance scenarios for future scope locks; they are not a claim of implemented behavior or a frozen executable test suite. Each applicable scenario must have evidence and a reasoned verdict. Channel-specific coverage is to be declared explicitly.

| ID | Scenario | Expected result |
|---|---|---|
| CT-01 | Greeting or vague opening only | No fabricated topic; conversation remains usable. |
| CT-02 | First substantive request, possibly after a greeting | A meaningful title reflects the initial topic, not merely greeting text. |
| CT-03 | Switch from soup to unrelated baking | Automatic title remains unchanged; normal answers continue. |
| CT-04 | Rename, reload and open another linked surface | Custom title persists and is consistent. |
| CT-05 | Later topic change or automatic naming attempt after rename | Custom title remains authoritative. |
| CT-06 | Rename twice, restore, reload | Original automatic title is restored; no new inference request. |
| CT-07 | Automatic naming fails | Normal user message and answer remain available; no false success. |
| CT-08 | Rename before title completion or concurrent requests | Manual override preserved; agreed lifecycle handles original title. |
| CT-09 | Open legacy history | No forced bulk renaming or content loss; agreed fallback. |
| CT-10 | Touch help control | Explanation readable without hover or inference. |
| TS-01 | Conversation created on one date, continued on another | Both correct absolute date/time values shown distinctly. |
| TS-02 | Return to an old conversation | Creation unchanged; last-message value and sorting reflect the new message. |
| TS-03 | Rename/restore without a chat message | Both message-related timestamps and ordering unchanged. |
| TS-04 | Messages cross midnight; older messages loaded | Correct calendar separation and per-message time under agreed zone policy. |
| TS-05 | Small-screen display and enlarged text | Titles and timestamps remain legible without obscuring controls. |
| RS-AC01 | Three proposed dishes; click second | One intended turn requests second dish; complete corresponding recipe or truthful failure/clarification. |
| RS-AC02 | Same scenario via ordinary text | Equivalent intended selection remains supported. |
| RS-AC03 | Established servings and exclusions | Chosen recipe respects applicable constraints; no needless re-questioning. |
| RS-AC04 | Similar names or later list of offers | Control references its intended offer, not a newer list position. |
| RS-AC05 | Rapid duplicate activation or retry | No unintended duplicate turn or false success. |
| RS-AC06 | Invalid/expired/foreign offer or tampered callback | No unauthorized action or silent retargeting; safe recovery. |
| RS-AC07 | No action taken on offered dishes | No unnecessary pre-generation of full recipes. |
| RS-AC08 | Textual selection with a new modification | Natural chat and authorized transformation remain available. |
| RP-AC01 | Expand, collapse, reopen completed recipe | Same stored object; no new model request or mutation. |
| RP-AC02 | Reply contains several valid recipes | Each control opens the correct recipe; surrounding text is preserved. |
| RP-AC03 | Open dedicated view and return | Complete readable recipe and return to dialogue; no regeneration. |
| RP-AC04 | Missing/expired/invalid structured recipe | Honest fallback; no invented recipe or metadata. |
| RP-AC05 | Use existing favorites and shopping actions | Existing functionality and ownership preserved; correct recipe selected. |
| SEC-01 | Attempt to access or change another user's object | Rejected according to established isolation contracts. |
| NR-01 | Ordinary chat, history, recipe generation and linked channels | No regression attributable to the accepted increment. |

For new intellectual behavior, include applicable frozen FIX-01 regression scenarios and additional targeted tests, according to the accepted research and delivery governance. No existing frozen verdict is changed by this specification.

## 10. Proposed incremental delivery

This is a provisional grouping, not an implementation order, release commitment or approved worktree strategy.

| Increment | Scope | Independence considerations |
|---|---|---|
| A — Conversation Management | PP-01.1A–D and PP-01.2 | Separate semantic title generation from persistent title management and timestamp presentation if useful; avoid releasing a misleading restore control without a defined original title. |
| B — Recipe Selection | PP-01.3 | Primary recipe UX priority; likely needs semantic offer representation, context continuity and behavioral evidence. |
| C — Recipe Presentation | PP-01.4 and PP-01.5 | Potentially earlier independent read-only release if verified stored-object access is available on the accepted base. |
| Future — Cooking Mode | Not PP-01 | Interactive progress, timers and related features require separate scope. |

Do not force A, B and C into one release. C may precede B if it delivers useful value independently. Conversely, do not create a release branch solely to meet cadence without a meaningful user benefit. Each increment must have a coherent user-facing outcome rather than only an internal technical component.

## 11. Architecture and release preparation (not authorized by this document)

A separately authorized read-only assessment / Scope Lock proposal should:

- Verify the actual existing title, timestamp, recipe metadata and authenticated recipe read contracts on the relevant baseline; distinguish current `main` from the verified accepted release source.
- Map each requirement to affected files, surfaces, data lifecycle and existing tests; identify schema/retention needs, if any, rather than assuming no migration.
- Compare title generation options, explicit offer-reference mechanisms and read-only recipe presentation alternatives; quantify inference, latency, complexity and behavioral dependencies where relevant.
- Propose bounded increments, channel coverage, UX decisions and acceptance evidence; identify shared-file conflicts with FIX-01 and RP-01 integration risks.
- Resolve exact serving-image/revision/Compose/accepted-baseline and rollback provenance before selecting an independent release candidate.
- Compare sequential `main` after accepted remediation with a short-lived isolated product branch, only where justified and proven. Do not assume feature flags isolate FIX-01. Require exact-candidate CI (including applicable Python/database suites), independent audit, behavioral/non-regression checks as appropriate, backup/rollback readiness and separate production authorization. Reintegration must not silently import blocked behavior.

The Owner performs Git operations. Only one active package per stream, and no concurrent modifications to shared pipeline files without an agreed integration boundary. RP-01 work stays isolated; no research candidate or experimental prompt is included in PP-01 merely because it is available.

## 12. Open decisions register

The following remain unresolved and must not be silently fixed by the developer. Proposed defaults above may be evaluated and confirmed in the next product/technical Scope Lock.

| ID | Decision needed | Timing |
|---|---|---|
| OD-01 | Title-generation mechanism, timing, sufficient-context rule, fallback/retry and auxiliary inference budget | Before automatic title design freeze |
| OD-02 | Title length/validation, provisional and legacy title lifecycle, early manual rename and restore semantics | Before title increment scope lock |
| OD-03 | Exact timestamp data mappings, empty/pending/expired behavior and time-zone policy across channels | Before chronology implementation |
| OD-04 | Placement and visual treatment of title controls; channel-native Web/Telegram controls and any staged rollout | Before affected UX scope lock |
| OD-05 | Actionable dish representation and reference semantics, older-offer handling, repeat-click and recovery policies | Before recipe selection design freeze |
| OD-06 | Card default expansion, available metadata, dedicated reading-view navigation and applicable favorites coverage | Before presentation scope lock |
| OD-07 | Exact accepted release baseline, serving provenance, Git strategy, CI support, reintegration and operational compatibility | Before implementation/release authorization as applicable |
| OD-08 | Increment order, exact affected scope, estimates and acceptance evidence | Before each increment authorization |

No numerical prompt ceiling replacement is approved by PP-01. KHD-52 permits justified future revision; the existing production constraint remains in force until a separate decision.

## 13. Explicit exclusions

- General FIX-02 remediation, including intent under history, recipe fidelity, capability truth and natural-language quality beyond PP-01 requirements and applicable non-regression.
- Native tools, adaptive memory, orchestration, new model/provider choice or unapproved live inference experiments.
- Standalone saved shopping lists (KH-SHOP-02), equipment/profile expansion, new culinary calculation or preservation functions.
- Garden product design or implementation, Automotive Assistant and domain-routing architecture.
- Bulk historical renaming, unrelated redesign, new cooking progress persistence, checklists, timers, reminders or proactive actions.
- Production modifications, Git strategy selection, schema migration, source implementation or deployment authorization.

## 14. Governance and next step

Owner: product decisions, scope lock, Git, budgets and final acceptance. Teya: product/architecture coordination and review. Claude Code: separately authorized development/research. Codex: independent read-only audit. Cursor Agent: separately authorized production operations.

Next: Owner reviews this product specification; it may then be recorded in the repository through the Owner's normal documentation workflow. A later task may prepare a targeted technical assessment and increment scope proposals. No agent may treat this product specification as authority to implement or deploy. Keep the accepted Charter and Incremental Delivery Strategy intact.

**PP-01 PRODUCT REQUIREMENTS CONSOLIDATED — READY FOR OWNER DOCUMENTARY REVIEW.**
