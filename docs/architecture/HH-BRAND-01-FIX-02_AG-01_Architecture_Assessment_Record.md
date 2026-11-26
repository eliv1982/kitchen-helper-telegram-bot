# House Helper — HH-BRAND-01-FIX-02
## AG-01 — Architecture Assessment Record (Consolidated)

| Document control | Value |
|---|---|
| Deliverable | Consolidated AG-01 record: RD-02 (Current Architecture Assessment) and initial RD-03 (Root Cause & Findings Register), reconciled against the independent targeted review |
| Date | 2026-11-26 |
| Version | 1.0 — documentary reconciliation |
| Repository | `eliv1982/kitchen-helper-telegram-bot` |
| Branch / documentation baseline | `main`, `3247528059c16c1f99d484b397f83f30503fad4b` |
| Original AG-01 verdict | `AG-01 ASSESSMENT COMPLETED — READY FOR ARCHITECTURAL REVIEW` |
| Independent review verdict | `CONDITIONAL PASS — TARGETED EVIDENCE REQUIRED` |
| Findings reconciled | R-01–R-10 |
| FIX-01 behavioral result | 24 PASS / 6 FAIL; behavioral acceptance FAILED |
| FIX-01 production release | BLOCKED |
| Architecture | NOT SELECTED |
| AG-02 execution | NOT AUTHORIZED |
| Formal document status | READY FOR OWNER ACCEPTANCE |

> **Authorization boundary.** This record is documentation only. It creates no source, test, prompt, migration, dependency or deployment change; it authorizes no AG-02 execution, provider call, judge call, Git operation, server operation, real-user-data access or production release. The Research Charter v1.0 and the Incremental Delivery Strategy v1.0 are preserved unchanged.

---

## 1. Purpose, provenance and relationship to the original assessment

AG-01 was performed read-only on 2026-11-26 and delivered in the conversation as one report. That report was not created as a repository file. An independent targeted review then returned **CONDITIONAL PASS — TARGETED EVIDENCE REQUIRED** and accepted R-01–R-10 as the basis for documentary corrections.

This record consolidates the original report so that later research and delivery packages have a self-contained, citable artifact. It:

- preserves the substantive RD-02 architecture map, the initial RD-03 findings register, the architectural strengths, the RQ-A01–RQ-A27 coverage, the provisional hypothesis matrix, the product dependency analysis and the evolution-readiness conclusions;
- applies every correction required by R-01–R-10;
- records explicitly where the original assessment's wording was wrong, incomplete or too strong, rather than presenting the original report as if it had contained the corrected positions;
- preserves the original evidence grades and the frozen 24 PASS / 6 FAIL result.

The original report's ten executive conclusions are therefore retained in substance but not verbatim. Section 3 states the corrected executive summary; Section 17 maps each independent finding to its correction and to the evidence that still limits it.

**Historical status.**

| Artifact | Status |
|---|---|
| Original AG-01 report (conversation only) | Delivered; superseded for documentary purposes by this record |
| Independent targeted review | `CONDITIONAL PASS — TARGETED EVIDENCE REQUIRED` |
| R-01–R-10 | Accepted by the architectural reviewer as the basis for documentary corrections |
| This record | Consolidated, corrected, self-contained |
| AG-02 | Not authorized |

This record does not reopen FIX-01's technical acceptance, the frozen rubric, the six verdicts or the Owner's behavioral-acceptance decision. It adds documentary reconciliation only.

---

## 2. Baseline and scope verification

### 2.1 Supplied baseline

| Item | State | Grade |
|---|---|---|
| Repository / branch | `eliv1982/kitchen-helper-telegram-bot`, `main` | Owner-supplied; working checkout |
| Documentation baseline | `3247528` (FIX-02 research charter and delivery strategy) | Owner-supplied |
| CI #96 | PASS, Owner-confirmed for `main` | Owner-reported; not re-run by AG-01 or this reconciliation |
| FIX-01 implementation | `77e2bfd2f513e9285e0219d1d0f89538acf58610` | Owner-supplied; present in the repository history |
| FIX-01 evaluation documentation | `bc0986f` | Owner-supplied |
| FIX-01 technical implementation | ACCEPTED | Owner-recorded; independent audit PASS WITH NON-BLOCKING NOTES |
| FIX-01 evaluation | COMPLETED: 30/30 requests, 24 PASS / 6 FAIL, zero critical safety violations | Frozen artifacts; Owner review |
| FIX-01 behavioral acceptance | FAILED | KHD-50 |
| FIX-01 production release | BLOCKED; `77e2bfd` not deployed | KHD-50 |
| Production database | PostgreSQL 16.15, schema `0014_user_measure_overrides`, two user accounts | A12 verification (2026-11-25) for the database facts; reported for later state |
| PostgreSQL Compose baseline | `ddca0966bd11` | Owner-supplied Charter/Strategy baseline; see 2.2 |
| Maintenance image | `d02b34ed369c` | A12 verification |

### 2.2 Production provenance — dated mapping and the open gap

This replaces the original report's unresolved "baseline ambiguity" (§2) with the dated mapping established by A12 and the later handoff.

**At the A12 verification (2026-11-25), the following were read on the production host and are VERIFIED in `docs/STATUS.md`:**

- **Serving application image:** `house-helper:ddca0966bd11`, image ID `sha256:81a8775d30b7d254864f2bbca5f8e9e9e323b0f69905ffda308f7b3d86b77487`, revision label `ddca0966bd11a4ff65bdccbdef87438202e059aa`; container started 2026-11-21T06:08:38Z, `RestartCount=0`, healthy.
- **Maintenance image:** `house-helper:d02b34ed369c`, image ID `sha256:8237d42bcf31e9b420ab0720ddae44712072f5fe35fe86ed411addb14cdef817`, revision label `d02b34ed369c986fcb8c9a1074c5d4491097d2fa`; maintenance checkout `/home/elvi/apps/house_helper-c7p3-d02b34e` at that commit with a clean tree.
- **Installed backup unit:** pins `--postgres-compose-file /home/elvi/apps/house_helper-staging-ddca096/deploy/compose.postgres.yaml` — the **serving checkout** at `ddca096`.
- **Cluster:** PostgreSQL 16.15, system identifier `7710180270781173795`, `alembic_version=0014_user_measure_overrides`.

The Charter and Delivery Strategy describe `ddca0966bd11` as the "PostgreSQL Compose baseline". A12 describes the same revision as the serving application image and records that the installed backup unit reads the Compose file from the `ddca096` serving checkout. **Both descriptions can be true at A12**: the serving checkout supplies both the running image revision and the Compose file used by the installed maintenance unit. They are one dated mapping, not two independent facts.

**Later handoff.** The FIX-01 records and the FIX-02 Charter/Strategy identify `afd83cb65bd7cc6ab1ab6d638187115b05610c63` as the reported production HEAD and as the parent of the FIX-01 implementation. That establishes `afd83cb` as a **candidate source baseline**. It does **not** establish that the container running at A12 was rebuilt from `afd83cb` after A12, nor that the serving container's immutable image ID corresponds to `afd83cb`.

**Minimum missing evidence (open release prerequisite).** An existing or separately authorized Owner-supplied deployment record pairing:

1. the running application container's immutable image ID or digest and full revision label;
2. the serving checkout HEAD and the effective Compose source;
3. the accepted product revision;
4. the rollback image.

Until that record exists, the running-container-to-`afd83cb` mapping remains open. If the running image still maps to `ddca0966bd11`, the runtime changes from `ddca096` to `afd83cb` must be accounted for before `afd83cb` is used as a presentation-only release base.

### 2.3 Scope actually assessed

AG-01 was a read-only inspection of the repository, the effective prompt and request construction, the frozen evaluation artifacts and the authoritative documents. It did not verify CI, production configuration, the running container, the image build provenance or any live provider behavior. This reconciliation also verifies nothing live. Every statement below is labelled as a repository fact, an observed frozen-artifact fact, a supported static conclusion, a hypothesis or an open unknown.

---

## 3. Executive summary (corrected)

1. **The implemented architecture is a single-pass, prompt-centred design (Alternative A in practice), with strong operational foundations.** Web and the in-process Telegram bot share one turn runner, one conversation store, one 40-message context window and one shared system prompt. There is no classifier, intent router, operation layer, tool layer or separate memory service. Channel-specific admission, binding, redelivery and delivery behavior sits around the shared runner; the legacy standalone `bot.py` is a separate in-memory path.

2. **The four defect directions have different, partly independent architectural causes.** The static evidence supports: history being associated with the different handling of an ambiguous request (intent, cases 1–2); regeneration without a REPRODUCE operation or a fidelity rule (recipe repetition, cases 21–22); capability knowledge existing only as prompt prose with no authoritative source (cases 24–25); and a combination of prompt exemplars, informal address, model defaults and instruction load plausibly contributing to Russian-language defects. **None of these is experimentally proven.** The frozen harness called the backend directly with manifest-assembled messages; it did not exercise the production database context builder, so the contrast is observed but its production-path contribution and the relative weight of history, typo interpretation, instruction wording and inference settings remain open.

3. **The system stores structured recipes and authoritative user data but does not give the structured objects back to the model.** `message_recipes` stores validated title, servings, ingredients and steps; the context builder sends only the rendered message text. Favorites, saved lists, household measures and profile data are outside model context. Rendered recipe text, and preferences a person states in an eligible message, can reach the model through history. This is the central "grounded intelligence" gap.

4. **There is no authoritative capability context supplied to the model.** The prompt contains «Описывай только доступные возможности» and describes shopping-list work as recipe-scoped; the static greeting also names the recipe boundary. The model receives no authoritative account of how the saved-list feature actually works: the application creates saved interactive lists only from a stored message recipe or a structured favorite, and standalone lists are not implemented. Cases 24–25 omitted that material restriction; case 25 nevertheless denied ordering correctly, so pure capability invention is not established. Model generalization from recipe-scoped prompt rules is a hypothesis.

5. **Prompt budget headroom is 35 whole characters, not an unqualified "exhausted" budget.** `KITCHEN_SYSTEM_PROMPT` is recorded at 10,764 characters against a hard ceiling strictly below 10,800 (KHD-48), so 35 additional whole characters fit under the strict inequality. The optional 1,831-character recipe-protocol suffix made the frozen effective prompt 12,595 characters. Character count, total request tokens and the completion cap measure different things; the request also carries history and can carry images. The Owner now permits a justified ceiling revision under Quality First (KHD-52), but no replacement value is approved, and the prompt and its tests are unchanged.

6. **The release block is real and cannot be discharged by CI.** `main` contains behaviorally unaccepted FIX-01 changes (`77e2bfd`) on top of `afd83cb`. Any deployment from current `main` would carry them. A docs-only CI PASS changes none of this.

7. **The UX Package is smaller than the roadmap wording suggests, and its meaning needs Owner scoping.** Derived conversation titles and message timestamps already exist. Manual renaming is absent and would be a persistent owner-scoped mutation. A Full Recipe control that opens an existing stored recipe is a read-only UI candidate; turning an idea, incomplete answer or legacy prose into a full recipe would require generation, reconstruction or a new retrieval contract. These three meanings must not be collapsed.

8. **The architecture is incrementally extensible without a rewrite, but not Garden-ready.** Identity is domain-neutral, and persistence operations have strong ownership and integrity contracts. Missing are a domain-neutral prompt composition mechanism, explicit domain state/selection (KHD-17 is accepted direction, not implemented), an authoritative capability source, a model-facing interface to authoritative objects, and domain-specific context partitioning (KHD-18). Garden is reuse potential plus open prerequisites, not an approved Garden design.

9. **Alternative C remains plausible but has an unresolved provider precondition.** The source documents that GPT-6 Luna supports function calling on Chat Completions only at effort `none`, but this is a source-code claim, not independently verified; the implementation sends no tool definitions and consumes text. The frozen run establishes text generation at effort `low`. Any native-tool package must confirm current model, endpoint, SDK and effort compatibility from authoritative provider evidence before relying on tools; live confirmation needs separate authorization. This does not block RP-01 design.

10. **No architecture is selected, no hypothesis is resolved, and no implementation is authorized.** RP-01 is ready to propose for separately authorized AG-02 design. PP-01 is pending Owner-confirmed product scope. Sequential release from `main` after bounded I-01 acceptance is the lowest-integration path; one short-lived display-only release branch is conditionally feasible on a verified accepted base.

---

## 4. Evidence inventory and limitations

### 4.1 Repository evidence read during AG-01

- Research Charter v1.0 and Incremental Delivery Strategy v1.0.
- FIX-01 Behavioral Evaluation Record and Implementation Record.
- `BACKLOG.md`, `DECISIONS.md`, `ARCHITECTURE_BASELINE.md`, and the release/deployment sections of `STATUS.md`.
- `HH-BRAND-01_Scope_Lock.md` section 7 and its exclusion list.

### 4.2 Implementation inspected

Core: `kitchen_prompt.py`, `assistant_identity.py`, `recipes.py`, `openai_chat.py`, `backend.py`, `chat_service.py`, `limits.py`, `provider_settings.py`, `outcomes.py`, `measures.py`, `ingredient_reference.py`, `step_scan.py`, `provenance.py`.

Web/persistence: `app.py`, `chat_api.py`, `turn_runner.py`, `settings.py`, `greeting.py`, `telegram_chat.py`, `telegram_botapi.py`, `conversations.py`, `turns.py`, `message_recipes.py`, `shopping_lists.py`, `profile.py`, `models.py`, `measure_targets.py`; legacy `api_client.py`, `context_manager.py`, `bot.py`.

Tests: function inventories of `test_kitchen_prompt.py` and `test_assistant_identity.py`; `test_context_continuity.py`.

### 4.3 Frozen evaluation artifacts

Inspected in place at `%LOCALAPPDATA%\HouseHelper\Evaluations\HH-BRAND-01-FIX-01\`, run `owner-approved-2026-11-25-a`: `manifest.json`, `rubric.md`, `summary.json`, selected sample records (`001`, `010`, `021`, `024`, `025`) and operator notes. Nothing was rerun, regenerated, moved, edited or uploaded.

### 4.4 Independent verification added by the review

The independent reviewer recomputed the SHA-256 values of `manifest.json`, `rubric.md` and `run_evaluation.py`; all match the frozen record. The reviewer checked all 30 operator decisions, inspected the six failing sample records and read the relevant supplementary and regression samples. The reviewer's conclusions are reported here as REVIEW-VERIFIED.

### 4.5 Known limitations (preserved, not waived)

1. **No individual PASS rationales.** The 24 `continue` operator records carry no written per-sample PASS rationale, as the Behavioral Evaluation Record already discloses. The machine summary records technical completion and six flags; the 24/6 semantic result comes from the Owner's review. Future evaluations must justify every verdict, including PASS.
2. **Selected sample inspection.** Not every one of the 30 sample records was opened during AG-01; the reviewer inspected the six failures and relevant supplementary/regression samples. The four defect directions are not undermined by this, but per-sample completeness is not claimed.
3. **Original hash omission.** AG-01 did not originally recompute the artifact hashes; the independent reviewer did, and all match. This record records the reviewer's verification, not a second recomputation.
4. **Production and CI facts are Owner-reported.** CI #96, the running image, the container-to-`afd83cb` mapping and production configuration were not independently verified here.
5. **No fresh behavioral run.** None is required for this review. The frozen 24 PASS / 6 FAIL result is preserved.
6. **Process deviation by the reviewer.** The independent auditor ran one read-only `git status` command in parallel with opening the assignment, before reading its prohibition on Git operations. No repository mutation was reported and no further Git command was run. This is recorded as a process deviation, not a finding about the repository.

---

## 5. RD-02 — Current Architecture Assessment

### 5.1 Entry points and shared versus channel-specific mechanics

| Path | Implementation | Status |
|---|---|---|
| Web chat | `POST /api/conversations/{id}/messages` → `WebTurnRunner.submit` → `begin_turn` → provider call → `complete_turn` | Implemented; production lineage |
| Telegram (in-process) | Long polling in `telegram_chat.py` → the same `WebTurnRunner`, with Telegram-specific admission, binding checks and redelivery behavior; key `tg-<update id>`, `reclaim=False` | Implemented; the production bot path |
| Telegram (legacy standalone) | `bot.py` → `ChatService` → `OpenAIBackend` → `api_client` → in-memory `context_manager` | Separate process; not the production Telegram path; no structured recipes; self-names "Kitchen Helper" |

**Shared:** one turn runner, one database, one conversation store, one shared prompt, one 40-message context rule, one turn lifecycle (idempotency, leases, compare-and-swap completion, failed-turn exclusion, retention).

**Web-specific:** HTTP routes, cookie session, client-supplied idempotency key, browser UI.

**Telegram-specific:** `getUpdates`, the durable admission ledger, link/binding checks under locks, `tg-` idempotency namespace, redelivery semantics, mirroring, callback security, dialog cursor, message splitting. These are channel behaviors around the shared runner, not a second intelligence pipeline.

**Legacy:** `bot.py` is a separate in-memory implementation and must not be described as sharing the Web turn pipeline.

### 5.2 LLM request assembly

`StandaloneOpenAIBackend.complete` sends exactly:

```text
system: KITCHEN_SYSTEM_PROMPT [+ RECIPE_PROTOCOL_PROMPT when structured recipes are on]
...history messages from build_context, ending with the pending user message
```

There is one system message, no developer message, no tool definition, no per-request metadata block and no dynamic context section. `to_openai_messages` converts text or text-plus-image parts. The legacy `api_client` sends `SYSTEM_MESSAGE` = `KITCHEN_SYSTEM_PROMPT` without the recipe protocol.

The frozen evaluation's effective prompt was 12,595 characters because structured recipes were enabled: 10,764 + 1,831. That is a **configuration of the frozen run**, not a constant of the architecture; with structured recipes off, the system message is 10,764 characters.

### 5.3 Prompt composition and authority — corrected

The shared prompt is one monolithic string containing, in order:

- assistant name and "domestic assistant, currently kitchen" framing;
- `PERSONA` (informal «ты», friendly, no self-presentation);
- a natural-Russian / no-gender-guessing rule;
- three request frames and follow-up rules;
- the ingredient working-set block (three availability states, necessity versus availability, shopping-list sections);
- festive/budget rules;
- response rules, including the FIX-01 intent bullet and full-recipe formatting;
- photo rules;
- safety and boundary rules.

Static facts:

- The prompt forbids guessing the person's gender (`kitchen_prompt.py:90-93`).
- The same prompt models the person with masculine-generic forms («человек сказал / выбрал / назвал», «он») in several places (`kitchen_prompt.py:118-152`), while the only address instruction is informal «ты» (`assistant_identity.py:15-21`). Russian past tense then requires a gender choice, and the prompt supplies masculine exemplars but no neutralization strategy. Informal address itself does not require gender inference; the exemplars are a plausible influence, not a proven cause.
- The prompt's phrase «посуда и приёмы на кухне» (`kitchen_prompt.py:114-116`) lexically resembles the Owner-observed «кухонные приемы». That resemblance supports a research question; it is not proof that this prompt text produced that output, because the exact prompt served during the observed interaction is not confirmed.

**Authority correction (R-05).** Identity, capability-adjacent statements, task rules, object rules, style and safety rules share one `system` role and one authority level. Splitting the source string into blocks can help review or selective inclusion, but sending the same text in sections does **not** shorten it and does **not** confer higher authority. Wording inside any block can still express priorities. Whether a block structure changes behavior requires a controlled comparison; it cannot be asserted from composition alone.

### 5.4 Context assembly

`build_context` (`conversations.py:335-428`) selects:

- messages older than the current one;
- assistant messages, and user messages whose turn request is `COMPLETED`;
- excluding expired messages and everything up to the latest system note (the latest reset);
- the newest `MAX_HISTORY_MESSAGES - 1` (39), then the current message — at most 40 including the current one;
- dropping leading assistant entries so the model never sees an answer without its question;
- including the newest two eligible photos as image parts.

There is no relevance selection, recency weighting, task-state object, summary, user profile, capability block, favorites/recipe object injection or domain marker. The legacy Telegram `model_context` applies the same 40-message bound to an in-memory list.

### 5.5 Identity, capability, user and domain context

- **Identity:** `ASSISTANT_NAME = "Федя"` and `PERSONA` in `assistant_identity.py:13-21`; reused by the prompt, greeting and surfaces.
- **Capability:** no registry and no code-owned list. The model sees the behavioral instruction «Описывай только доступные возможности», recipe-scoped shopping-list rules, and whatever the static greeting says; it does not see an authoritative description of the saved-list operation.
- **User context:** `profile.py` stores display name, text size and three household measures; these are used by the profile API, measure adapters and Telegram identity display, **not** by `build_context` or the provider request. No gender, age, household composition, equipment, skill or durable preference is stored or supplied to the model.
- **Domain:** `Conversation` has no domain field; KHD-17 explicit domain selection is accepted direction only.

### 5.6 Recipes: lifecycle and representations

| Stage | Representation | Sent back to the model? |
|---|---|---|
| Provider answer | Raw text with optional `<<<RECIPE>>>` JSON blocks | — |
| Parse | `Recipe` objects, strict schema, max 5, validated lengths/control characters/markers (`recipes.py:412-482`) | No |
| Render | Plain Russian text (`render_parsed_reply`), with a notice for invalid or truncated blocks | — |
| Store | `messages.content` = rendered text; `message_recipes` rows keyed `(message_id, position)` (`message_recipes.py:9-11`) | **Only the rendered text is sent** |
| Favorite | Copy of the text or structured recipe; no foreign key to the source message | No |
| Shopping list | Snapshot of every ingredient; no foreign key to the source | No |
| Measure presentation | Deterministic annotations computed on demand by `favorites_api` / `measure_targets` | No |

Consequences:

- The model can read the previous recipe as prose in history, so reproduction is theoretically possible.
- The model cannot see the validated structured object, the recipe identity, structured servings, or any marker distinguishing a stored recipe from ordinary prose.
- There is no operation selector and no fidelity rule for "repeat the previous recipe". The nearest prompt rule is «На отдельные вопросы отвечай по существу, не повторяя весь рецепт без необходимости» — a rule against unnecessary repetition, not a rule for faithful reproduction.
- `Recipe.recipe_id` is a content fingerprint and is deliberately not stored; the persistent identity is `(message_id, position)`.

### 5.7 Favorites and saved shopping lists

- **Favorites:** save any answer or one structured recipe; list/detail/edit/delete; 100 per user; no expiry; server-side ownership; no provider call.
- **Saved shopping lists:** created **only** from a stored message recipe or a structured favorite. Every ingredient is copied, including those already at home; the list is a snapshot, not a filter. Editing is limited to active lists; 100 lists / 200 items; no expiry; no provider call. Standalone lists are not implemented.
- **Callbacks:** Telegram save/browse callbacks are names, not capabilities; sender and user are resolved server-side.
- **Reuse boundary (R-01):** ownership checks, authenticated recipe reads, snapshot semantics, validation, leases, idempotency and atomic completion are reusable foundations. A future model-facing tool would still need its own argument schema, authorization injection, retry policy, result contract and false-success prevention. The existing operations are not a tool layer.

### 5.8 Deterministic calculations and integrity

- `core/measures.py`: exact rational conversion, 5% rounding gate, household measure contexts; pure core; not used in the chat path.
- `ingredient_reference.py`: versioned registry, exact-name matching, refusal-only unless a record is APPROVED; the shipped registry approves no record, so no mass conversion occurs.
- `step_scan.py`: deterministic step scanner and explicit ingredient binder; Stage B only; no arithmetic or persistence.
- `provenance.py`: descriptive audit of source measures.
- `measure_targets.py` + `favorites_api.py`: wired into favorites presentation, not into the LLM.

Deterministic integrity exists in libraries and favorites presentation, but **not in the generation path** for ingredients, proportions, cooking method or repetition.

### 5.9 Authorization, isolation and safety

- Every user-owned operation takes the server-side session user id; conversation ownership is the SQL statement's own condition; unknown, foreign and malformed ids are indistinguishable.
- Telegram linking is explicit pairing-code based; binding is rechecked under locks before send/save; callbacks carry no identity.
- The turn lifecycle has idempotency keys, leases, compare-and-swap completion, failed-turn exclusion from later context, and retention.
- Safety is **prompt-level**: the application enforces length, storage, ownership, permission, retention and structural recipe validity, but it does not enforce food-safety facts, capability truth, language quality or recipe fidelity. The two safety cases passed because the model followed the prompt, not because an application gate checked the answer.

### 5.10 Response processing and presentation

Before storage:

- `replace_yo` (ё → е) on the generated answer (`turn_runner.py:619-622`);
- `clean_reply` removes NUL and replaces lone surrogates;
- a fixed truncation note is appended when the provider hits the length cap;
- structured blocks are parsed and re-rendered into plain text **before** storage;
- Telegram splits long text at paragraph/line/space boundaries;
- fixed Russian error copy is produced by the application, not the model.

No template rewrites the assistant's wording.

### 5.11 Tests and observability — two levels, corrected

- The repository has an extensive deterministic suite. Prompt tests pin clause presence, placement, count and exact strings; they explicitly do not prove model compliance. `test_context_continuity.py` states the same limitation for the 40-message window.
- The frozen evaluation harness captures exact ordered history, prompt hash/length, raw provider response, parsed text, structured recipes, usage, finish reason and timing. It is external to the repository and is not used by the application.
- **Application diagnostics:** production logging records model, limits, token usage, conversation label, failure class/status/provider code, correlation IDs and turn state. It deliberately does not record prompt text, message content or response text. Correlation IDs, stable error codes, safe logs and retry feedback are implemented (`web/diagnostics.py`).
- **Application error-notification integration:** KHD-20's n8n error-notification workflow is **not implemented** in the inspected application path. The older blanket statement that "correlation IDs are not implemented" is stale.
- **External maintenance monitoring:** Ops Sentinel, n8n and Cronitor cover the scheduled purge and the offsite backup. A12 records successful host delivery to both monitoring paths and persisted n8n rows for executions `63411` and `63415`; Cronitor dashboard health is Owner-confirmed. These observations cover **maintenance outcomes and heartbeat delivery**. They establish no coverage for incorrect model answers, and one successful cycle alone does not verify every failure-alert path.

### 5.12 Documented intention versus implemented mechanism

| Intended | Implemented? |
|---|---|
| One assistant identity across domains | Identity module yes; only Kitchen domain exists |
| Explicit user domain selection (KHD-17) | No; `conversations` has no domain field |
| Domain-specific memory/history partitioning (KHD-18) | No |
| Long-term preferences (KHD-05/06, HH-ALPHA-01) | No store, CRUD or injection |
| Truthful capability claims (KHD-49) | Direction only; no authoritative capability source |
| Tool-Enabled Intelligence (Alternative C) | Research direction only; no tools |
| Adaptive memory / proactive behavior | Not implemented |
| Structured recipe objects | Yes, stored and used by favorites/shopping; not fed back to the model |
| Deterministic measures | Yes as libraries/favorites presentation; not in chat generation |
| KHD-20 n8n application error notifications | Not implemented |
| Correlation IDs, stable error codes, safe logs | Implemented in the application |
| External maintenance monitoring (purge/backup) | Implemented and verified at A12 |

---

## 6. Current end-to-end data and control flow

```text
WEB
client POST /api/conversations/{id}/messages  (Idempotency-Key, {"text"})
  -> chat_api.send_message
  -> WebTurnRunner.submit (shielded task)
     -> begin_turn (one DB transaction)
          lock conversation row (ownership)
          retire expired attempts; refuse busy conversation
          store user Message + TurnRequest (same savepoint)
          claim request (attempt, lease)
          build_context (40 messages, completed turns only, newest 2 photos)
     -> backend.complete
          system = KITCHEN_SYSTEM_PROMPT + optional RECIPE_PROTOCOL_PROMPT
          messages = stored history + current user message
          provider (OpenAI Chat Completions)
          parse_reply -> render_parsed_reply
     -> clean / replace_yo / truncation note
     -> complete_turn (one DB transaction)
          store assistant Message + message_recipes rows
          mark request COMPLETED
     -> completion hooks (Telegram mirror if linked/selected)
  -> JSON response

TELEGRAM (in-process)
getUpdates -> resolve linked/enrolled user -> admission ledger
  -> resolve conversation cursor
  -> runner.submit(origin="telegram", key="tg-<update id>", reclaim=False,
                   authorize=admit_update)
  -> identical turn lifecycle and context assembly
  -> split_text delivery; structured-recipe save buttons from stored recipes
```

**What the model never receives:** stored structured recipe fields, favorites, saved shopping lists, user profile, display name, household measures, equipment, capability list, domain state, task state, current date/time, or any application operation result. Rendered recipe text, and preferences stated in eligible messages, can reach the model through history.

**What the application never does:** classify intent, select a semantic operation, retrieve an object for the model, enforce recipe fidelity, enforce capability truth, or post-edit content beyond spelling/format/truncation handling.

---

## 7. RD-03 — Findings register (corrected)

Evidence labels: **V** verified implementation fact; **O** observed frozen-artifact evidence; **S** supported static conclusion; **H** plausible unconfirmed hypothesis; **R** risk; **U** unknown requiring experiment.

| ID | Finding (corrected) | Evidence | Severity | Remaining uncertainty |
|---|---|---|---|---|
| F-01 | History is **associated** with the different handling of `Рответ`; cases 1–2 produced unsolicited recipes with recipe-rich histories, case 10 produced a clarification with empty history. This is not proof of causation, and the harness bypassed the production database context builder. | O + S; mechanism H | S2 | Relative contribution of history, typo interpretation, instruction wording, inference settings; production-path contribution |
| F-02 | No REPRODUCE operation, no fidelity rule, no object retrieval. Material changes in cases 21–22 first appear in **raw model output**; normalization does not explain them. | O + S | S1 for explicit reproduction | Whether a fidelity instruction, structured context or deterministic reproduction suffices |
| F-03 | Capability claims have no authoritative source. Cases 24–25 omit the recipe-source restriction; case 25 correctly denies ordering. The saved-list feature is created from a stored message recipe or structured favorite. | V + O | S2 (mandatory capability gate) | Whether a capability statement, registry or tool-availability representation is most truthful; model generalization is a hypothesis |
| F-04 | Identity and capability are not separated architecturally. The prompt already contains recipe-scoped shopping guidance and the greeting states the boundary, but the model has no authoritative account of the saved-list operation. | V | S2 architectural | Behavioral effect requires RP-01 |
| F-05 | Russian gender assumption is observed (samples 10, 12, 14) and structurally plausible. Informal address itself requires no gender inference; masculine-generic prompt exemplars are a plausible influence. | O + S; causal weight H | S3 per occurrence; aggregate S2 | Relative contribution of exemplars, «ты» + past tense, model default |
| F-06 | Greetings still redirect toward cooking in supplementary observations (3, 4, 7, 9). | O + S | S3 | Prompt framing vs model default |
| F-07 | Grammar, tautology and unnatural phrasing are model-side, with no application correction beyond spelling/format. | O; cause H | S3 | Model/inference vs prompt overload |
| F-08 | **Corrected:** the claim that prompt wording "leaks" into output is withdrawn as stated. «посуда и приёмы на кухне» lexically resembles «кухонные приемы», but the exact prompt served during the observed interaction is unconfirmed. This is a research question, not an established causal finding. | Owner-reported observation; lexical source H | S3 | Controlled prompt-vocabulary contrast |
| F-09 | **Corrected:** no dedicated mechanism extracts, stores, corrects or injects durable preferences. Ordinary conversation messages persist and can be reused within the history window; the application does not claim that feedback cannot persist conversationally. | V | S2 relative to memory goals; not a current defect | Whether durable persistence is desired; HH-ALPHA-01 design |
| F-10 | Structured recipes are stored but not returned to the model; repetition is reconstruction from prose. | V | S2 | RP-02 comparison |
| F-11 | No user, capability or domain context is supplied to the model. | V | S2 architectural | Architecture package |
| F-12 | **Corrected:** the prompt has 35 whole characters of headroom under a strict `< 10,800` ceiling, not zero. The frozen effective prompt was 12,595 because of the optional 1,831-character suffix. The Owner permits a justified ceiling revision (KHD-52); no replacement value is approved. | V (documented) | S2 architectural risk | Replacement ceiling only if evidence justifies it |
| F-13 | **Corrected:** one prompt carries different authority classes, but splitting it into blocks does not by itself shorten it or raise authority. All clauses share the `system` role; wording can still express priorities. Behavioral effect requires controlled comparison. | V | S2 architectural | RP-01/RP-04; AG-05 decision |
| F-14 | No model-facing operation/tool layer. The documented GPT-6 Luna function-calling restriction is a source-code claim; the implementation sends no tool definitions and consumes text; the frozen run establishes text at effort `low`. | V + documented claim | S2 architectural risk | Authoritative provider verification; RP-05 |
| F-15 | No domain state or routing; Garden would require explicit domain selection and prompt composition. | V | S2 for Garden readiness | Garden architecture package after discovery |
| F-16 | Production observability is insufficient for behavior diagnosis, but the inventory is two-level: application correlation IDs/error codes/logs exist; KHD-20 n8n error notification does not; external maintenance monitoring covers purge/backup only. | V | S3 (privacy trade-off); S2 for RQ-A27 | Research-environment capture; no production logging change without a decision |
| F-17 | `main` carries behaviorally unaccepted FIX-01 changes. FIX-01 changed **six product files**; `assistant_identity.py` was unchanged. | V (documented baseline; Owner Git evidence) | S1 release risk | Running-container mapping; exact-candidate CI |
| F-18 | Application post-processing changes the displayed answer (`replace_yo`, `clean_reply`, truncation note, protocol rendering). | V | S3 | Must be accounted for in fidelity experiments; do not remove |

### 7.1 Direction B1 — Intent resolution (cases 1–2)

**Actual path.** No special handling exists. `Рответ` is stored as ordinary text and sent with the same system prompt and history as any other message. There is no classifier, keyword gate, typo map or routing layer (KHD-46 explicitly rejected one; tests pin that no `Рответ` mapping exists).

**What the frozen evidence establishes.** Cases 1 and 2 sent `Рответ` with different recipe histories and received unsolicited full recipes; case 10 sent the same text with empty history and received a clarification. The responses establish the observed contrast.

**What the evidence does not establish.** The harness assembled messages from the manifest and called the application backend directly. Those records therefore cannot validate the production database context builder or rule out an application-path contribution to a production incident. The original assessment's statement that "the harness representation matches `build_context`" is withdrawn as an established fact: it was an inference, not a verified equivalence. The contrast also leaves open the relative effects of history, typo interpretation, instruction wording and inference settings.

**Preserve.** Successful short follow-ups, implicit dish selection, clear and mistyped greetings, and proportional clarification.

### 7.2 Direction B2 — Recipe fidelity (cases 21–22)

**Representation and path.** Raw provider text → strict parse → rendered plain text stored as the assistant message; validated structured fields stored separately; `build_context` sends only the rendered text. There is no retrieval path keyed to "repeat", no version link and no REPRODUCE instruction.

**Where change arises.** In case 21, loss of the sour cream fat percentage, altered meat handling and the changed rest time already appear in the **raw model output**. In case 22, the raw output adds broth to the sauté, changes garlic timing, and shortens the lower bound of the rest time; it also adds «или по вкусу» to the measured salt amount, so the earlier "all quantities preserved" claim is too strong. Formatting and spelling normalization (`replace_yo`, protocol re-rendering) do not account for these changes. The application does not alter ingredients or steps.

**Three operations must remain distinct.**

- **Exact reproduction** — not implemented and not promised; direct replay is a candidate, not approved.
- **Semantic reproduction** — what the model attempts; the frozen rubric judges material facts, not wording.
- **Authorized transformation** — scaling and substitution; cases 15–18 passed and must not be suppressed by a fidelity rule.

**Open.** Whether a fidelity instruction, structured context or deterministic reproduction is sufficient is unresolved. Direct reproduction remains a candidate mechanism.

### 7.3 Direction B3 — Identity and capability (cases 24–25)

**Actual source of identity.** `assistant_identity.py` supplies the name and persona; the prompt opening supplies "domestic assistant, currently kitchen"; greeting/help supply surface copy. This part passed evaluation.

**Actual source of capability information.** None authoritative. The model sees behavioral instructions, the sentence «Описывай только доступные возможности», recipe-scoped shopping-list rules and the static greeting's recipe boundary. It does not see how the saved-list feature works.

**What the application actually does.** Saved interactive lists are created only from a stored message recipe or a structured favorite; every ingredient is copied; editing is limited to active lists; standalone lists are not implemented. The prompt already describes recipe-scoped shopping tasks, and the static greeting states the recipe boundary, but the model receives no authoritative account of the saved-list feature.

**Case reading.** Case 24 was a general capability question; case 25 correctly denied ordering. The omission is best described as **absent authoritative context plus model generalization**, not inaccurate capability data and not pure invention. Do not assume a separate registry must be implemented; a code-owned capability block, a prompt statement or a tool-availability representation are candidates for RP-01.

### 7.4 Direction B4 — Natural Russian conversation

| Factor | Contribution | Evidence |
|---|---|---|
| Prompt style paragraph and persona | Defines «ты», friendliness, no gender guessing | V |
| Masculine-generic exemplars in prompt | Plausible priming of masculine past tense | S/H |
| Informal address + Russian past tense | Forces a gender choice with no neutralization instruction | S |
| Kitchen framing | Plausible source of greeting redirection | S/H |
| Model/inference (`gpt-6-luna`, low effort) | Plausible source of grammar, tautology, awkward phrasing | H |
| Application post-processing | Spelling/format only; not a source of grammar | V |
| Greeting/help copy | Presentation, not in model context | V |

**Gender.** Samples 10, 12 and 14 use masculine forms about the person. The artifacts also corroborate mechanical cooking redirects, grammatical errors, tautology and awkward wording. These are supplementary quality observations, separate from the frozen 24/6 scoring. First-person forms such as «Рад» concern Fedya's voice, not the person's gender. Informal address itself requires no gender inference. «Речь о рецепте?» and «Что хочешь уточнить?» illustrate natural alternatives.

**«Кухонные приемы».** The Owner observed successful handling of feedback about «кухонные приемы» within one interaction on the production baseline: the assistant explained the intended meaning, proposed «полезные приемы и хитрости на кухне» and accepted the correction. The current prompt contains «посуда и приёмы на кухне». The implementation record documents preservation of earlier functional clauses across FIX-01, while the exact prompt served during the observed interaction remains unconfirmed. The lexical resemblance supports a research question; the "prompt wording leaks" finding is removed as stated.

**Feedback versus durable preference.** The inspected chat path persists ordinary conversation messages and can reuse them within the history window. It has no dedicated mechanism to extract, store, correct or inject durable culinary or language preferences. That is an architectural limitation for future memory work, not a claim that feedback cannot persist conversationally.

---

## 8. Existing strengths and non-regression requirements

**Strengths to preserve.**

- One identity module, domain-neutral and import-free.
- One shared prompt and one turn runner across Web and in-process Telegram; channel-specific behavior remains around the shared runner.
- Server-side ownership in every SQL statement; indistinguishable 404s; strong Telegram binding/callback security.
- Idempotency, leases, compare-and-swap completion, failed-turn exclusion and retention.
- Strict structured-recipe validation with explicit invalid/truncated notices.
- Real favorites and saved-list operations with copy semantics and ownership.
- Deterministic measures core and provenance audit, with explicit refusal behavior.
- Frozen evaluation harness and preserved artifacts; extensive deterministic tests.
- Privacy-aware logging and correlation IDs.

**Non-regression requirements.** The 24 passing scenarios in the Behavioral Evaluation Record §3.5 remain frozen expectations: clear mistyped greetings, clear greetings, empty-history greeting/clarification, keyboard mash, implicit dish selection, portion scaling, explanation/substitution without whole-recipe repetition, explicit full-recipe requests, acknowledgment without recipe, both food-safety answers, off-domain boundary, dish mention without recipe, and contextual heat-level continuation. Also: account isolation, history, favorites, Telegram linking, structured output validity, and no unsolicited recipe on greetings or thanks.

**Do not replace** the turn runner, persistence operations, recipe validation, identity module or deployment/retention mechanisms merely because a different architecture looks cleaner. The evidence does not support that.

---

## 9. RQ-A01–RQ-A27 coverage matrix

Status: **R** resolved by static inspection; **P** partially resolved; **U** unresolved without experiment.

| RQ | Answer (corrected where marked) | Evidence | Status |
|---|---|---|---|
| A01 | Web and in-process Telegram assemble context through `begin_turn` → `build_context`; legacy `bot.py` uses in-memory `context_manager`. One shared system prompt. | `turns.py`, `conversations.py`, `chat_service.py` | R |
| A02 | Only system prompt + user/assistant history + newest two photos. No separate identity/capability/user/task/object/safety blocks; one `system` authority level. | `build_context`, `openai_chat` | R |
| A03 | Overlapping rules exist (kitchen framing vs no gender guessing; greeting rule vs kitchen scope; many task rules). No contradiction detector; full linguistic duplication audit not performed. | prompt text | P |
| A04 | **Corrected:** yes, the whole prior window is supplied without relevance filtering. Cases 1–2 **associate** history with the failure; that is not proof of causation, and the frozen harness bypassed the production database context builder. | `build_context`; samples 001/010 | P (causal H) |
| A05 | Intent is determined by the model alone from the prompt bullet and history. No classifier/router. | `kitchen_prompt.py:194-198` | R |
| A06 | References are resolved from prose history only; no object ids, task state or retrieval. | `build_context` | R |
| A07 | **Corrected:** the frozen harness called the backend directly with manifest-assembled messages; it does not establish the production context-assembly path. The prompt contains a direct intent rule and the model still generated a recipe. The exact layer is unresolved. | manifest/harness; sample 001 | P |
| A08 | Model + history + prompt rules; successful cases 13–14, 17–18, 30 preserved. | evaluation record | R (observed) |
| A09 | Raw text, parsed `Recipe`, rendered text, `message_recipes`, favorite copy, shopping snapshot, measure annotations. | `recipes.py`, `message_recipes.py` | R |
| A10 | Text and structured fields are stored; no identity/version link from later context to the structured object; `recipe_id` not stored. | `message_recipes.py`, `conversations.py` | R |
| A11 | CREATE/EXPLAIN/TRANSFORM partially distinguished; RETRIEVE/REPRODUCE not implemented. | prompt; no retrieval code | R |
| A12 | **Corrected:** material changes first appear in raw model output; the application only renders and normalizes. | samples 021/022; `recipes.py` | R for first appearance; mechanism P |
| A13 | Structural recipe validation and length caps; deterministic measures in libraries/favorites; no chat-path ingredient/proportion/method integrity. | `recipes.py`, `measures.py`, `measure_targets.py` | R |
| A14 | `assistant_identity.py` + prompt opening + greeting/help; no per-user identity data. | code | R |
| A15 | **Corrected:** no authoritative capability source. The model sees one prompt sentence, recipe-scoped shopping rules and the static greeting's boundary; it does not see the saved-list operation. | grep; prompt; greeting | R |
| A16 | Web and in-process Telegram identical; legacy bot differs (no structured recipes, «Kitchen Helper»). | code | R |
| A17 | **Corrected:** no authoritative capability context; shopping lists appear only as recipe-scoped task rules; the model generalized. Case 25 still denied ordering. | samples 024/025; prompt | P (generalization H) |
| A18 | `PERSONA`, natural-Russian paragraph, greeting/help. | code | R |
| A19 | No gender data; informal address; masculine-generic prompt exemplars; model default. Informal address itself does not require gender inference. | prompt; samples 010/012/014 | P |
| A20 | Prompt says no recipe on greeting but frames kitchen; no greeting templates in model context. | prompt; samples 3/4/7/9 | P |
| A21 | No personalized context in model input; profile not injected; no enduring-versus-situational distinction. | `build_context`, `profile.py` | R |
| A22 | `replace_yo`, `clean_reply`, protocol rendering, truncation note, Telegram splitting, fixed error copy. | `turn_runner.py`, `recipes.py` | R |
| A23 | Application is authority for data/permissions/state; model for language/reasoning; no tool boundary. | architecture | R |
| A24 | Turn lifecycle, recipe parse/store, favorites, saved lists, profile, measures, retention, auth. | code | R |
| A25 | **Corrected:** no tool protocol; Chat Completions request with no tools; the effort-`none` function-calling restriction is a source-code claim, not independently verified; no operation abstraction; limited message roles; prompt budget. | `openai_chat.py`, `provider_settings.py` | R/P |
| A26 | Backend abstraction, turn runner, persistence operations, ownership, recipe validation, measures, identity, deployment kit. | code | R |
| A27 | **Corrected:** evaluation harness is strong; production context/response observability is weak by design; correlation IDs and error codes exist; KHD-20 notification integration does not; external monitoring covers maintenance only. | logging code; harness; A12 | R |

---

## 10. Initial hypothesis evidence matrix

Verdicts remain deliberately provisional; AG-01 cannot resolve causal hypotheses.

| H | Initial status | Basis |
|---|---|---|
| H-01 Context may over-influence current intent | **PARTIALLY SUPPORTED (association only; not confirmed)** | Case 1 vs 10 contrast; no controlled replay; harness bypassed the production context builder |
| H-02 Architecture may insufficiently distinguish creation/reproduction/transformation | **SUPPORTED (static)** | No REPRODUCE operation, no fidelity rule, prose-only object context |
| H-03 Supplying an original may be insufficient to guarantee preservation | **INCONCLUSIVE** | Original text is supplied as prose; no test of structured injection |
| H-04 Identity and actual capabilities may not be separated | **SUPPORTED (static)** | Identity module exists; capability has no authoritative source |
| H-05 Model may conflate intellectual abilities with implemented functions | **PARTIALLY SUPPORTED** | Cases 24–25; case 25 correctly denied ordering; generalization is a hypothesis |
| H-06 Russian defects may arise from conflicting/overloaded instructions | **INCONCLUSIVE** | Plausible gender/greeting conflicts; no controlled test |
| H-07 Inference/model may materially affect behavior | **INCONCLUSIVE** | Single model/config evaluated |
| H-08 Directions may need different remedies | **INCONCLUSIVE (plausible)** | Different mechanisms indicated; not tested |
| H-09 Reliability without limiting freedom | **NOT EVALUATED** | No candidate tested |
| H-10 Tools may improve factual integrity | **NOT EVALUATED** | No tools |
| H-11 LLM may select tools without classifier | **NOT EVALUATED** | No tools |
| H-12 Tool contracts may improve reliability | **NOT EVALUATED** | No tools |
| H-13 Multi-step tools may improve complex tasks | **NOT EVALUATED** | No tools |
| H-14 C may suffice without D | **NOT EVALUATED** | No comparison |
| H-15 General mechanisms may transfer across domains | **PARTIALLY SUPPORTED (architecture only)** | Identity is neutral; persistence operations are reusable; no cross-domain evidence |
| H-16 Autonomy without contracts may reduce reliability | **NOT EVALUATED** | No autonomous actions |
| H-17 Complexity may not yield proportional quality | **NOT EVALUATED** | No comparative evidence |

---

## 11. Product dependency matrix

| Direction | Current status | Data/code dependencies | Shared surfaces | FIX-01/02 dependency | Impl. independence | Release independence | Required checks | Rework risk | Early intelligence fit |
|---|---|---|---|---|---|---|---|---|---|
| **UX Package** | Partly exists: derived titles and message timestamps implemented; manual renaming and a Full Recipe control absent; not a BACKLOG item; roadmap in Scope Lock §7 | Nullable `Conversation.title` exists; no rename route; `message_recipes`; `ui.py`, static JS/CSS | Web UI, chat API, recipes | Low for presentation; any greeting/style change is FIX-01-adjacent and needs behavioral review | High if presentation-only | High on an accepted baseline | API/UI/DOM tests, browser smoke, isolation | Merge conflicts in UI/static; low logic | Low |
| **KH-F04 Equipment** | Proposed; not built | New household/equipment data (migration), profile API/UI; prompt injection if used in answers | Profile, context assembly, prompt | Medium (capability truth, context authority) | CRUD: high; answer use: no | CRUD: high; answer use: requires behavioral evaluation | Migration, profile tests, prompt behavior | Low for storage; medium if generic context layer later | High (authoritative fact injection) |
| **Remaining KH-F01** | Code present: `core/measures`, `measure_targets`, favorites annotations, profile measures; BACKLOG still "Proposed"; mass registry approves nothing | Measures core, favorites API/UI, profile | Favorites presentation, profile, UI | Low | High (deterministic) | High | Measure/UI tests; no model eval | Low | Medium (deterministic tool example) |
| **KH-SHOP-02** | Proposed; no design, schema, UI or retention rule | New table/migration/retention; shopping API/UI; capability copy/prompt | Saved lists, prompt capability wording | Medium (capability truth; action intelligence) | Yes, but changes capability boundary | Yes if existing claims unchanged; current limitation becomes obsolete | Ownership, retention, capability behavior | High if a generic object/list model later | High (action tool) |
| **KH-F02 Concentrations** | Proposed; not built | KH-F01 measures + KH-F03 safety policy; deterministic calculator | Measures, safety copy | Low | High | High | Safety review, calculation tests | Low | Medium |
| **Consistency Analysis** | Named in delivery strategy, not in BACKLOG; no scope | Depends on diagnosis and object/context model | FIX-02 research, recipes | **High** | Low until FIX-02 evidence | Low | Research gates | High | Medium |
| **Profile Development** | Profile API exists (name, text size, measures); further development not scoped | `users` columns, profile API/UI; context injection if personalized | Profile, context assembly | Medium | CRUD: high; injection: no | CRUD: high; injection: evaluation required | Profile tests; behavior if injected | Low/medium | High (authoritative user context) |
| **Culinary Memory (HH-ALPHA-01)** | Proposed; no table/CRUD/extraction/injection | New preference store, retention exception, correction precedence, context injection | Profile, context, prompt, purge | **High** | CRUD possible; value requires context/behavior | Not independently releaseable as intelligence | Research + behavioral evaluation | High | High but later |
| **KH-F03 Preservation** | Proposed; prompt has cautious storage rules | Safety policy, possibly Garden integration | Prompt safety, Garden | Medium | Yes, with safety review | Yes | Safety review | Medium | Medium |
| **Automotive Assistant** | Deferred direction; no identifier or scope | None | Identity/topic boundaries | Low | N/A | N/A | N/A | N/A | Not now |

### 11.1 UX specifics — display, persistent rename and recipe meaning

| UX item | Existing mechanism and remaining decision |
|---|---|
| **Automatic titles** | The server prefers a stored title if present and otherwise derives a label from the first unexpired user message, limited to 60 code points. Web derives an immediate label for a newly created thread too. This gives a useful preview. A stable or topic-based title requires an Owner decision. |
| **Manual renaming** | The nullable title column and read precedence exist. The conversation API has no rename operation. A rename adds a persistent, owner-scoped mutation and needs decisions about validation, clearing the title, and supported surfaces. The existing column makes a migration unnecessary in principle. |
| **Date and time** | Web renders message times and conversation recency using browser-local time. Telegram dialog labels use Moscow time. Confirm the desired surfaces, time-zone policy and display format before closing the roadmap item. |
| **Full Recipe control** | Chat already renders the full answer text. Its metadata exposes recipe positions and titles, and the existing authenticated recipe GET returns servings, ingredients and steps for an owned, unexpired message recipe. A Web control that opens that existing object can be a read-only UI change. It depends on having a stored, valid recipe. A control that turns an idea, an incomplete answer or legacy prose into a full recipe requires generation, reconstruction or a new retrieval contract. The Owner must define which meaning is intended. |
| **Informal address** | The persona instructs «ты». Fixed product text and generated dialogue follow separate paths. Conversational naturalness and consistent informal address remain behavioral requirements. |

### 11.2 Changes that are not presentation-only

- Any greeting, help, persona, tone or capability copy change that reaches the model prompt.
- Any user, equipment or preference context injection.
- Any capability statement change that alters what the model says.
- Any recipe retrieval or reproduction behavior.
- Any domain selection or Garden prompt.
- Any change to the recipe protocol, structured output or response-rendering semantics.

These require behavioral review or a research package, not a UI-only release label.

---

## 12. Release isolation and strategy

### 12.1 The constraint

`main` contains FIX-01 changes that are behaviorally unaccepted. Production is reported at `afd83cb`. A release of current `main` would deploy the unaccepted prompt, greeting and voice changes. CI success on a documentation commit does not change this. The release gate is KHD-50.

### 12.2 FIX-01 exclusion at the proposed source boundary

The Owner-supplied Git evidence establishes:

- `afd83cb` is an ancestor of `main`.
- The three subsequent commits (the FIX-01 implementation, the behavioral-evaluation documentation closure and the FIX-02 Charter/Strategy documentation) contain FIX-01 implementation and documentation changes: a three-commit, 18-file comparison.
- No migration or deployment file changed in that interval.

The unchanged migration path and the complete file classification support the narrower conclusion that this interval introduces no migration, configuration or deployment-file change. They cannot establish compatibility across a different production-to-base interval, or for a future patch.

**Corrected FIX-01 inventory (R-08).** FIX-01 changed **six product files**: `house_helper/core/kitchen_prompt.py`, `house_helper/web/greeting.py`, `house_helper/web/ui.py`, `house_helper/web/telegram_chat.py`, `house_helper/web/static/chat-logic.js` and `house_helper/web/static/app.js`. `house_helper/core/assistant_identity.py` was **unchanged**. The other audited paths are four test files and documentation. `ui.py`, `app.js` and `chat-logic.js` are the likely overlap for Web UX, with greeting, help and suggestion tests nearby. Reintegration conflicts are possible and depend on the hunks.

### 12.3 Strategy comparison

| Strategy | Feasibility | Conditions / risks |
|---|---|---|
| **1. Sequential release through `main` after bounded I-01 acceptance** | Feasible only after I-01 remediation is accepted. Lowest integration cost. | Delays any product release until FIX-02 produces acceptable behavior. Requires architecture approval, Scope Lock, implementation, audit and a new behavioral evaluation. |
| **2. One short-lived, display-only product release branch from a verified accepted baseline** | Conditionally feasible. | Requires the production provenance mapping in 2.2 and the evidence steps in 12.4. Must exclude FIX-01 completely by starting from the verified accepted base. UI/static files overlap with FIX-01; plan for reintegration conflicts. No migration expected for display-only work. |
| **3. Feature flags or equivalent runtime isolation** | Not demonstrated. | The only existing switch is `HOUSE_HELPER_STRUCTURED_RECIPES`; it changes the recipe protocol and does not isolate FIX-01 across model requests and user-facing surfaces. A UI-only switch is insufficient. |
| **4. Separate isolated research workspace** | Already the de facto research model. | Suitable for AG-02–AG-04; not a product release mechanism. |

The structured-recipe switch changes the recipe protocol. There is no demonstrated flag that isolates FIX-01 across model requests and user-facing surfaces. The Owner retains the release-strategy decision and deployment authorization.

### 12.4 Evidence required for a display-only release candidate

1. Review the candidate against the accepted serving source and its known rollback image. Include shared UI and tests in that review. Confirm the app's expected Alembic head, SQL and privilege artifacts, build inputs and effective deployment configuration. The Owner's migration diff supports a no-migration plan for the supplied interval.
2. Require exact-SHA CI, an independent audit, affected UI/DOM and browser checks, and risk-proportionate non-regression evidence. Build the serving image from that SHA and retain the immutable image identity and revision label. Use the PostgreSQL startup gate and the applicable operational procedure, with backup and rollback readiness, the reported PostgreSQL 16.15 and schema `0014_user_measure_overrides` compatibility checked, and smoke checks for the affected surfaces. Keep the serving change separate from the pinned maintenance image, Compose paths, locks, schedules and monitoring configuration.
3. After acceptance, transfer only the approved product patch to `main`, review overlapping hunks against FIX-01, and rerun integration CI on the resulting SHA. Retire the release branch after reintegration. Repeated releases from it would duplicate testing and accumulate UI, schema and operational drift.

### 12.5 CI requirements

CI runs automatically on pushes to `main` and on pull requests targeting `main`, and it declares `workflow_dispatch`. Manual CI on the selected release branch is the smallest existing route **if dispatch is available for that ref**. Verify that the run checks out the exact candidate SHA and completes both Python legs (3.10 and 3.12) and the required database suites. CI #96 is Owner-confirmed for `main`. A pull request into `main` can test integration with the blocked code and cannot establish release isolation. If dispatch is unavailable on the selected baseline, a workflow change requires separate approval.

### 12.6 Preliminary conclusion

Sequential release from `main` after I-01 acceptance has the lowest integration cost. If a specific UX improvement is worth shipping sooner, one short-lived release branch for one display-only patch is the only strategy that can deliver it without carrying FIX-01 — conditional on the production mapping in 2.2 and the evidence in 12.4. No branch is selected and no release is authorized.

---

## 13. Architectural evolution and Garden readiness

### 13.1 AP-01–AP-19 assessment

| Principle | Current state |
|---|---|
| AP-01 Intelligence First | Single-pass LLM is the core; no unjustified command routing. Strong. |
| AP-02 Single Identity | Strong: domain-neutral `assistant_identity.py`; surfaces agree by test. |
| AP-03 Authoritative Truth | Weak for model claims: authoritative data is stored but not supplied; capability claims are prompt-derived. |
| AP-04 Semantic Continuity | History continuity exists; relevance/authority control absent; cases 1–2 show the risk. |
| AP-05 Operation Integrity | Weak: no operation vocabulary; repeat is regeneration. |
| AP-06 Proportional Clarification | Prompt-only; mixed results (some passes, cases 1–2 fail). |
| AP-07 Model Independence | Moderate: backend abstraction and shared prompt; provider is OpenAI-only; tool constraint may bind. |
| AP-08 Context Authority | Weak: one flat prompt/history; no category separation. |
| AP-09 Memory Integrity | No preference/memory store, so integrity rules are unused; profile data not in context. |
| AP-10 Task Continuity | History-only; no task/version state. |
| AP-11 User Sovereignty | Strong for stored data (edit/delete); no preference model. |
| AP-12 Modular Extensibility | Partial: identity is modular; prompt/domain/context are monolithic. |
| AP-13 Proportional Complexity | Good discipline: no premature tools/agents; the risk is under-separation, not over-complexity. |
| AP-14 Evidence-Based Architecture | Strong process: frozen evaluation, explicit gates; no architecture selected. |
| AP-15 Observability | Strong in the research harness; weak in production by privacy design; application diagnostics exist, but behavior reconstruction is limited. |
| AP-16 Behavioral Non-Regression | Strong: 24 passing scenarios preserved as explicit expectations. |
| AP-17 Research-to-Product Separation | Strong: external artifacts, release block, no live experiments. |
| AP-18 Transferable Knowledge | Program designed for it; evidence not yet accumulated. |
| AP-19 Domain-Neutral Intelligence | Identity is neutral; knowledge/operations are Kitchen-specific; no shared framework (correctly avoided). |

### 13.2 Reuse potential for Alternative C

Credible foundations:

- conversation/history read (user-scoped);
- stored recipe retrieval by `(message_id, position)`;
- favorites list/get/save/edit/delete;
- saved-list create-from-source/list/get/edit/status;
- profile read/update;
- deterministic measure conversion;
- turn idempotency and completion.

They are Python functions with explicit exceptions and server-side ownership. A model-facing tool layer would need strict argument schemas, an authenticated user id injected outside model control, structured results, retry/failure semantics and no-false-success guarantees. None of that exists yet.

### 13.3 Provider-specific coupling

- Two adapters target OpenAI Chat Completions; errors are mapped to backend errors.
- The recipe protocol is prompt-level and provider-neutral in principle, but tied to the Chat Completions message shape.
- GPT-6 Luna reasoning mode omits `temperature` and counts reasoning tokens inside the completion cap.
- The documented tool constraint — function calling only at effort `none` for GPT-6 Luna on Chat Completions — is a source-code claim. If true, C may require a different effort (quality trade-off), a different API surface or a different model — all Owner decisions. Confirm against authoritative provider evidence before any native-tool package; live confirmation is separately authorized.

### 13.4 I-01–I-04 readiness

| Increment | Readiness | Main prerequisites |
|---|---|---|
| **I-01 Reliable Conversation** | Partially ready | Capability context source; intent/ambiguity policy; reproduction mechanism; language strategy; prompt-budget decision |
| **I-02 Grounded Intelligence** | Foundation ready, interface missing | Object/context injection or read tools; authoritative data boundaries; result representation |
| **I-03 Action Intelligence** | Operations ready, tool layer missing | Tool contracts; authorization; confirmation; failure/result handling; false-success prevention |
| **I-04 Adaptive Intelligence** | Not ready | Orchestration, richer memory, cross-domain state; must not be a prerequisite for I-01 or Garden |

### 13.5 Garden readiness — reuse potential plus open prerequisites

**Reusable:** one Fedya identity; shared user isolation and permissions; the authenticated turn infrastructure; the shared conversation store; honest refusal boundaries; deployment and retention discipline; existing data-integrity mechanisms.

**Open architectural prerequisites:**

- No domain field or explicit domain selection (KHD-17 is accepted direction, not implemented).
- One monolithic Kitchen prompt with 35 whole characters of headroom; adding Garden material requires composition or a justified ceiling amendment.
- No authoritative capability source; Garden capability claims would repeat the cases 24–25 defect at larger scale.
- No domain-specific data partitioning (KHD-18).
- No cross-domain object model; Kitchen–Garden interaction would currently be prose-only.

Garden is the next seasonal product priority. Its requirements discovery and product specification remain separate, and I-04 is unnecessary for beginning that discovery. Alternative C remains the preferred research direction; D is the strategic horizon. This record selects no Garden architecture and approves no Garden scope.

---

## 14. Candidate bounded research packages

These are proposals, not authorizations. Each would need AG-02 and, for live calls, AG-03/AG-04 approval.

### RP-01 — Capability truth and context authority (ready to propose)

- **Status:** ready to propose for separately authorized AG-02 design once AG-01 is reconciled.
- **Smallest scope:** compare the frozen-style prompt-only baseline with a minimal authoritative capability statement. Then compare placement or message role with the wording held constant where supported. Do not assume a registry is required.
- **Cases:** RC-A 24–25 and CF-07 contrasts for available and unavailable capabilities, truthful AI identity, and the distinction between composing a list in conversation and creating a saved list. Derive the test facts from the implemented Web and Telegram contracts.
- **Controls:** hold model, inference settings, histories and recipe protocol fixed for the initial comparison.
- **Evidence:** effective characters, tokens, source of each capability fact, material omissions, false claims, naturalness.
- **Corpora:** the original regression cases and an independent holdout as applicable, with recorded rationales for every verdict.
- **Targets:** RQ-A14–A17, RQ-B16–B18, H-04/H-05.
- **Boundary:** synthetic, outside the production path, fixed scope, explicit artifact custody. Preserve the turn lifecycle, isolation, error handling, identity, recipe validation and successful conversational behaviors. Keep recipe reproduction, broader intent and language remediation, memory, model changes and Garden design outside RP-01.
- **Dependencies:** a prompt-budget decision if the statement cannot fit within the current ceiling; no schema change. Native tool support is **not** a prerequisite.
- **Risks:** prompt expansion; over-formal capability lists; the model ignoring the block.

### RP-02 — Object reference and reproduction

- **Smallest scope:** compare current regeneration, an explicit fidelity instruction with the rendered original, a structured original injected as authoritative context, and deterministic app-side replay.
- **Cases:** RC-A 21–22, CF-05, scaling/substitution controls (15–18) to ensure transformation freedom is preserved.
- **Evidence:** material-fact preservation, no unrequested changes, structured validity, no regression of authorized transformations.
- **Dependencies:** decide whether structured rows may enter context; harness representation.
- **Risks:** a fidelity instruction may over-constrain creativity; replay may bypass conversational nuance; direct reproduction is not approved.

### RP-03 — Intent under history

- **Smallest scope:** controlled replays of the same ambiguous inputs across H0/H1/H2/H3/H6 and typo/clarification variants; isolate history presence, prompt variant and reasoning effort.
- **Cases:** RC-A 1–2, 10; CF-01/CF-03/CF-04; preserve 3–6, 7–8, 11–12, 13–14.
- **Evidence:** clarification versus generation, unnecessary-clarification rate, preservation of implicit selection.
- **Dependencies:** none beyond the existing harness.
- **Risks:** over-clarification; loss of legitimate short follow-ups.

### RP-04 — Natural Russian and address

- **Smallest scope:** prompt-variant contrast for gender-neutral past tense, greeting behavior and prompt-vocabulary influence; include model/config as a separate factor only if authorized.
- **Cases:** supplementary observations from 10/12/14, 13/15/16/19/20, 3/4/7/9 and 28; RC-C C-01/C-04.
- **Evidence:** gender assumptions, grammaticality, naturalness, mechanical-greeting rate, tautology.
- **Dependencies:** none; keep frozen scores unchanged.
- **Risks:** neutralization may sound stilted; a model change confounds attribution.

### RP-05 — Tool feasibility spike (C)

- **Smallest scope:** a synthetic read tool (get stored recipe) and one action tool (save favorite / create saved list) with strict contracts, failure injection, cross-user attempts and false-success checks.
- **Cases:** RC-D D-01–D-04 synthetic.
- **Evidence:** tool selection, argument correctness, grounding, failure recovery, authorization, no false success, latency/cost.
- **Dependencies:** authoritative provider tool-support and effort decision; isolated synthetic fixtures.
- **Risks:** provider constraint; multi-step failure; complexity without quality gain.

**Prioritization.** RP-01 is ready to propose for AG-02 design. AG-02 design can proceed without resolving native tool support. Move provider feasibility ahead of any later comparison or I-01 decision that actually depends on native tools. RP-03 and RP-01 are the smallest evidence-bearing packages; RP-02 is required for recipe fidelity but may need an architecture decision first; RP-04 can run in parallel; RP-05 follows, not precedes, a signal on whether context-only mechanisms suffice.

---

## 15. Candidate independent product packages

### PP-01 — UX completion (pending Owner-confirmed product scope)

The safest first scope is **one Owner-confirmed Web history readability improvement**, such as timestamp presentation or clearer display of existing titles. It uses data already in the view. A read-only recipe control is also a candidate if the requirement is to open an existing stored recipe and that mechanism is present on the verified release base. Manual renaming belongs in a separately scoped persistent UX change. PP-01 still needs a concrete usability gap and acceptance criteria.

- **Exclusions:** prompt, greeting, persona, capability copy, model behavior.
- **Dependencies:** existing `Conversation.title`, the recipe metadata and the authenticated recipe GET; a rename route only if rename is separately scoped.
- **Release:** one display-only patch from a verified accepted baseline; no prompt change; UI/API/ownership tests and a browser smoke required; behavioral evaluation of the assistant not required for display-only work.

### PP-02 — Equipment profile Stage A (data + CRUD only)

- **Scope:** store household appliances/constraints with owner control; profile API/UI; no answer-behavior change.
- **Exclusions:** prompt injection into answers (separate intelligence package).
- **Dependencies:** migration, profile API/UI, retention decision.
- **Release:** independent if no model-visible change; migration/deployment rehearsal required.

### PP-03 — KH-F01 remaining deterministic presentation

- **Scope:** expose already-accepted measure conversions/annotations in the intended UI; no mass↔volume without approved coefficients.
- **Exclusions:** new coefficients, unsafe conversions, prompt changes.
- **Dependencies:** `measures.py`, `measure_targets.py`, favorites API/UI.
- **Release:** independent; deterministic tests.

### PP-04 — Garden Alpha (not yet scoped)

Blocked on requirements discovery and a Garden specification; needs domain selection and prompt composition. Do not implement under AG-01 or this record.

---

## 16. Revised preliminary effort and schedule estimates

Planning ranges, not commitments. An active project day includes agent work and Owner review.

| Activity | Active days |
|---|---:|
| AG-01 assessment and documentary reconciliation | 1–2 |
| RP-03 intent | 1–3 |
| RP-01 capability | 2–4 |
| RP-02 reproduction | 2–4 |
| RP-04 Russian | 2–4 |
| RP-05 tool spike | 3–5 |
| Live experiment runs (per package) | 1–2 + approved budget |
| I-01 implementation, audit, evaluation | 5–10 |
| PP-01 UX completion | 1–3 |
| PP-02 Equipment Stage A | 2–4 |
| PP-03 KH-F01 presentation | 1–3 |
| Garden discovery + specification | 2–4 |
| Garden Alpha | 5–10 (very low confidence; not scoped) |

**Sequencing implication.** The earliest useful product release (PP-01) can be prepared in parallel with the first intelligence research, provided release isolation is confirmed. I-01 should not wait for RP-05; a tool spike should not delay the cheaper, more directly diagnostic context/capability experiments.

---

## 17. Reconciliation matrix — R-01–R-10

| ID | Independent finding | Correction applied in this record | Remaining evidence limitation |
|---|---|---|---|
| **R-01** | Shared turn pipeline and context boundary: Web and in-process Telegram share `WebTurnRunner`; standalone `bot.py` is separate; the runner bounds at 40 messages including the current one, excludes expired and unsuccessful prior user turns, respects the latest reset, drops leading assistant entries and can include the two newest eligible photos; one system message with an optional recipe-protocol suffix; 12,595 characters was the frozen configuration; stored recipe fields, favorites, saved lists, household measures and profile data are outside model context; rendered recipe text and stated preferences can reach the model through history; the recipe protocol validates output but supplies no callable operation; existing operations are reusable foundations but a future tool needs its own authorization, retry and result contract. | RD-02 now distinguishes shared mechanics from channel-specific behavior and configuration-dependent prompt construction (5.1–5.3, 6). The context boundary is stated explicitly (5.4–5.7, 6). | None material for the architecture map. Behavioral effect of context composition remains experimental. |
| **R-02** | Intent failures correlate with history in the frozen samples; the harness called the backend directly, so it cannot validate the production database context builder; causal and coverage claims must be corrected. | F-01, B1 (7.1), RQ-A04/A07 and executive summary item 2 are corrected. The "harness matches `build_context`" inference is withdrawn. | Relative contributions of history, typo interpretation, instruction wording and inference settings remain open; controlled replays required. |
| **R-03** | Recipe fidelity and capability truth are distinct defects. Material recipe changes first appear in raw model output; case 22 also adds «или по вкусу» to salt, so "all quantities preserved" is too strong. Capability truth: cases 24–25 omit the recipe-source restriction; case 25 correctly denies ordering; case 24 was a general capability question; the application creates saved lists only from a stored message recipe or structured favorite; the model receives no authoritative account of the feature. | F-02, F-03, F-04, B2 and B3 (7.2–7.3) are corrected. Raw-output attribution and the transformation/reproduction distinction are recorded. Conversational list help is separated from saved-list creation. | Whether fidelity instruction, structured context or deterministic reproduction suffices; whether a capability statement, registry or tool contract is most truthful. Both are experimental. |
| **R-04** | Natural Russian and feedback: samples 10/12/14 use masculine forms about the person; supplementary quality observations corroborate redirects, grammar errors, tautology and awkward wording. Informal address permits gender-neutral phrasing; first-person forms concern Fedya's voice. The "prompt wording leaks" finding is removed as stated; the lexical resemblance is a research question. The chat path persists ordinary messages and can reuse them in the history window, but has no dedicated durable-preference mechanism. | F-05, F-08, F-09 and B4 (7.4) are corrected. Gender is treated as an observed language defect with a plausible, unconfirmed prompt influence. Durable-preference handling is recorded as an architectural limitation. | Exact prompt served during the Owner-observed interaction; relative contribution of exemplars, «ты» + past tense, and model default; whether durable persistence is desired (HH-ALPHA-01). |
| **R-05** | Prompt size, authority and tool support: KHD-48 sets a strict `< 10,800` threshold; 10,764 leaves 35 whole characters; the 1,831-character suffix made the frozen effective prompt 12,595; character counts, tokens and completion cap differ; block splitting neither shortens nor confers authority; the provider restriction is a source-code claim; the implementation sends no tool definitions and consumes text; the frozen run establishes text at `low`. | Executive summary item 5, F-12, F-13, F-14 and 5.3 are corrected. KHD-52 records the Owner's permission to revise the ceiling when justified; no replacement value is approved. Native-tool compatibility is recorded as a precondition for tool-dependent packages, not for RP-01. | Replacement ceiling only if evidence justifies it; authoritative provider evidence for function calling; separate live confirmation. |
| **R-06** | Monitoring exists at two levels: KHD-20 application error notification is unimplemented, while correlation IDs, safe logs, stable error codes and retry feedback exist; external Ops Sentinel, n8n and Cronitor cover scheduled purge and offsite backup; A12 records host delivery and persisted n8n rows for executions `63411` and `63415`; Cronitor dashboard health is Owner-confirmed; these cover maintenance outcomes, not incorrect model answers. | §5.11 and F-16 are corrected; the stale blanket statement about correlation IDs is corrected in `docs/STATUS.md`. | One successful cycle does not verify every failure-alert path; no coverage for model-answer correctness; KHD-20 remains unimplemented. |
| **R-07** | Production checkout, serving image and maintenance image: A12 records serving `house-helper:ddca0966bd11` with image ID and revision label, and the installed backup unit pins its Compose file to the `ddca096` serving checkout; A12 records maintenance `d02b34ed369c` and its checkout; later records identify `afd83cb` as reported production HEAD; the later serving container-to-image mapping is open. | §2.2 replaces the original ambiguity with the dated A12 mapping and the open mapping; §12.4 states the minimum missing evidence. | Running container's immutable image ID/digest, full revision label, serving checkout HEAD, effective Compose source, accepted product revision and rollback image. |
| **R-08** | FIX-01 exclusion is demonstrated at the proposed source boundary: the ancestry check and three-commit, 18-file comparison establish that a clean branch from `afd83cb` excludes FIX-01 implementation; the interval introduces no migration/configuration/deployment-file change. FIX-01 changed six product files; `assistant_identity.py` was unchanged; `ui.py`, `app.js` and `chat-logic.js` are the likely Web UX overlap. CI handles `main` and PRs to `main`, and declares `workflow_dispatch`; exact-SHA CI must be verified; a PR into `main` cannot establish release isolation. | §12.2 and §12.5 are corrected, including the six-file inventory, overlap analysis and exact-candidate CI requirements. | Compatibility across a different production-to-base interval; dispatch availability on the selected baseline; reintegration hunks. |
| **R-09** | PP-01 must distinguish display, persistent rename and new recipe behavior: titles may be stored or derived; rename has a nullable column and read precedence but no route; Web uses browser-local time while Telegram uses Moscow time; Full Recipe can mean opening an existing stored recipe (read-only) or generating/reconstructing one (new contract); informal address is a behavioral requirement. | §11.1 and §15 (PP-01) are corrected. | Owner must confirm the concrete history display gap, the meaning of Full Recipe, and whether rename is a separate package. |
| **R-10** | RP-01 and second-domain readiness: reuse potential in neutral identity, authenticated turn infrastructure and data-integrity mechanisms; no implemented domain selection or domain-specific context partition; Kitchen is the only competency; Garden discovery and specification are separate; I-04 is unnecessary for beginning discovery; C is preferred, D the horizon; no architecture selection. | §13.5 and §14 (RP-01) are corrected. Garden is stated as reuse potential plus open prerequisites, not a design. | Garden requirements, geography, knowledge sources and product specification remain undiscovered; domain architecture undecided. |

---

## 18. Owner decisions and open evidence

The independent review defines the minimum remaining Owner decisions. This record records them without resolving them.

1. **AG-01 acceptance.** Accept this reconciled record. The remaining production and provider questions may stay explicitly open at this gate.
2. **Release path.** Supply or confirm the dated serving image, revision, Compose and acceptance mapping in §2.2. Choose sequential delivery or one bounded display-only branch based on the actual PP-01 value and timing. Exact-candidate CI evidence follows once a candidate exists.
3. **PP-01.** Confirm the concrete history display gap. Decide whether "Full Recipe" means opening existing content or obtaining a recipe, and whether persistent rename is a separate package.
4. **RP-01.** Confirm the package boundary and authorize AG-02 separately. A replacement prompt ceiling is a later decision only if evidence justifies one. Provider configuration, call limits and budget are decisions for the applicable experiment gate.

Additional items that remain open and are not decided here:

- the running-container-to-`afd83cb` mapping and the rollback image;
- authoritative provider evidence for GPT-6 Luna function calling;
- the research budget, request limits, credentials, judge identity, artifact custody and holdout independence;
- Garden requirements discovery and product specification;
- any retention decision for new tables (equipment, preferences, standalone lists, Garden);
- direct-reproduction status as an architecture decision;
- architecture selection (none) and FIX-02 implementation scope (none).

**Stop conditions.** No HS-01–HS-09 or CS-01–CS-09 condition was triggered. The only stop-like condition is evidentiary: production, CI and Git facts remain Owner-reported, and the release-isolation conclusion remains provisional pending the mapping in §2.2.

---

## 19. Documentation-only compliance statement

This reconciliation was documentation-only.

- Files changed: this record, `docs/DECISIONS.md` (KHD-52), `docs/STATUS.md` (dated documentary entry and one stale monitoring-status correction) and `docs/product/BACKLOG.md` (one wording reconciliation; no new commitment).
- No source, test, prompt, migration, dependency or deployment configuration was changed.
- No Git operation was performed.
- No provider call or judge call was made.
- No application or test suite was executed.
- No VPS, production service or real user data was accessed.
- The original evaluation artifacts were not modified, regenerated, relocated or uploaded.
- No unrelated untracked file was changed.
- AG-02 was not executed and no production release was authorized.
- The Research Charter v1.0 and Incremental Delivery Strategy v1.0 are unchanged.

**Unresolved discrepancies or blockers.** The running-container-to-`afd83cb` mapping is open. The provider function-calling claim is unverified. The 24 passing samples lack individual PASS rationales in the original artifacts. These are recorded as limitations, not waived.

---

**AG-01 DOCUMENTARY RECONCILIATION COMPLETE — READY FOR OWNER ACCEPTANCE**
