# HH-BRAND-01-FIX-01 — Implementation Record

**Status:** AUDIT ACCEPTED / DOCUMENTATION RECONCILED / READY FOR OWNER CODE ACCEPTANCE AND COMMIT.
The independent Codex audit returned **PASS WITH NON-BLOCKING NOTES**: no confirmed blocking
implementation defect, and the audited implementation is preserved unchanged. This document records
a local implementation on baseline `afd83cb65bd7cc6ab1ab6d638187115b05610c63`; it claims no commit,
push, CI PASS, real-provider behavioural acceptance, owner acceptance, release or deployment.

**Updated 2026-11-25 (behavioral evaluation closure):** the implementation was subsequently
committed as `77e2bfd2f513e9285e0219d1d0f89538acf58610`; GitHub Actions CI #94 PASS and the
independent Codex audit PASS WITH NON-BLOCKING NOTES are recorded in the Owner-supplied accepted
baseline. The separately authorized real-provider behavioral evaluation then ran as
`owner-approved-2026-11-25-a`: 30/30 requests completed, **24 PASS / 6 FAIL**, behavioral acceptance
**FAILED**, production release **BLOCKED** and remediation **OPEN**. The authoritative record is
[`HH-BRAND-01-FIX-01_Behavioral_Evaluation_Record.md`](HH-BRAND-01-FIX-01_Behavioral_Evaluation_Record.md).
Sections 1–11 below remain the historical implementation record written on baseline `afd83cb`; they
are not rewritten by that outcome.

**Date:** 2026-11-25

**Scope:** `docs/product/HH-BRAND-01-FIX-01_Scope_Lock.md` and KHD-45–KHD-48 in
`docs/DECISIONS.md`. The Owner's quality-first amendment of 2026-11-25 (priority order: safety and
accuracy, intent, answer quality, natural interaction, latency, cost) is applied: the prompt fits the
agreed ceiling without trimming any accepted clause.

The budget principles completed in KHD-48 and the Scope Lock: optimize the total cost of
successfully resolving a user task, not merely the cost of an individual completion; make reasonable
assumptions when uncertainty is immaterial; clarify only when missing information materially affects
correctness, safety or fulfillment of intent; prompt compactness must not compromise answer quality;
a justified budget increase is preferable to degraded behavior.

## 1. What changed

Three findings from the Owner's production functional acceptance were corrected, prompt- and
copy-only:

1. **F01 — one greeting.** «Привет! Я Федя — твой домашний помощник. / Помогу придумать, что
   приготовить, подобрать рецепт, заменить ингредиенты, пересчитать порции и составить список
   покупок по рецепту. / Расскажи, что тебе нужно, — разберемся вместе.» is now the greeting on the
   Web login lead (one `<p class="lead">`), the Web empty conversation (three paragraphs) and
   Telegram `/start`. The functional help is a separate text shown by the «Помощь» panel; the
   previous one-shared-string design is withdrawn. The first example question is now «Что
   приготовить на ужин?».
2. **F02 — intent.** A new prompt bullet directly before the full-recipe rule: the current message
   decides, history refines references but is not a request, typos are read by sense, greetings and
   thanks get no recipe, a full recipe follows a request including the implicit choice of a
   suggested dish, and separate questions are answered in substance without repeating the whole
   recipe. No classifier, keyword gate, filter or typo mapping.
3. **F03 — voice.** A new prompt paragraph: natural, concrete Russian, terms explained when useful,
   no calques, bureaucratic wording or guesses about the person including gender, only implemented
   capabilities described, no automatic agreement.

The shared prompt opening now positions Fedya as a domestic assistant whose current scope is the
kitchen. `PERSONA` and `RECIPE_PROTOCOL_PROMPT` are byte-identical.

## 2. Files changed

Exactly the fourteen authorised paths; no other tracked file changed.

**Production (6)**

- `house_helper/web/greeting.py` (new) — `GREETING_PARTS` (three approved sentences, the name from
  `ASSISTANT_NAME`) and `GREETING_TEXT`; no I/O, no interface or provider import.
- `house_helper/web/ui.py` — the login lead is the joined greeting in the existing lead paragraph.
- `house_helper/web/telegram_chat.py` — `GREETING = GREETING_TEXT + "\n\n" + HELP`; `HELP` changed
  only in the authorised sentence (shopping lists are edited on the site too).
- `house_helper/web/static/chat-logic.js` — `GREETING_PARTS` and `HELP_BODY` replace
  `HELP_EXPLANATION`; `TEXT.emptyThread` is the greeting array, `TEXT.help` the functional help.
- `house_helper/web/static/app.js` — the empty conversation draws the three greeting paragraphs;
  `SUGGESTIONS[0]` is «Что приготовить на ужин?»; the help panel still draws `TEXT.help`.
- `house_helper/core/kitchen_prompt.py` — the opening, the F02 bullet and the F03 paragraph;
  docstring updated.

**Tests (4)**

- `tests/test_assistant_identity.py` — independent greeting/help/prompt literals, greeting module,
  three-surface parity, greeting/help separation, Telegram composition and `HELP` sentence.
- `tests/test_kitchen_prompt.py` — 10,764 / `< 10,800`, the F02/F03 clauses, their placement, no
  typo mapping in the prompt, new opening.
- `tests/test_web_ui.py` — the login lead is the canonical greeting.
- `tests/js/dom_scenarios.js` — the empty conversation shows the three greeting paragraphs, the
  three chips with the new first one, and the help panel shows functional help, not the greeting.

**Documentation (4)**

- `docs/product/HH-BRAND-01-FIX-01_Scope_Lock.md` (new).
- `docs/product/HH-BRAND-01-FIX-01_Implementation_Record.md` (this file, new).
- `docs/DECISIONS.md` — KHD-45–KHD-48.
- `docs/STATUS.md` — one dated entry.

## 3. Implemented Russian copy

- Greeting: the three approved sentences, identical on the login lead, the empty conversation and
  Telegram `/start` (layout only differs; the browser literal is cross-checked against the Python
  module).
- Functional help body: «Спроси, что приготовить из имеющихся продуктов или как приготовить блюдо.
  Могу предложить идеи блюд: выбери понравившееся — дам полный рецепт, который можно сохранить в
  избранное кнопкой рядом с рецептом.»
- Telegram `HELP`: unchanged except «Редактировать рецепты, заметки и списки покупок можно только
  на сайте House Helper.»; relay conditions, `/dialogs`, the five commands and `PRIVACY` verbatim.
- Suggestions: «Что приготовить на ужин?», «Как приготовить борщ?», «Чем заменить сметану в
  рецепте?».

All new product-written copy uses «е», never «ё» (the orthography scan passes).

## 4. Exact prompt amendments and measured length

Opening: «Ты — Федя, домашний помощник. Сейчас помогаешь с кухней и готовкой. Всегда отвечай
по-русски, без лишних вступлений.»

F02 (bullet before `- Рецепт давай целиком:`): «Определяй намерение по текущему сообщению с учетом
контекста. История уточняет отсылки, но не заменяет запрос. Понятные опечатки трактуй по смыслу,
при неясности задай короткий вопрос. На приветствия и благодарности отвечай без рецепта. Полный
рецепт давай по запросу, включая неявный выбор предложенного блюда. На отдельные вопросы отвечай по
существу, не повторяя весь рецепт без необходимости.»

F03 (paragraph after `PERSONA`): «Пиши естественным русским языком, конкретно и понятно. При
необходимости объясняй кулинарные термины. Избегай калек, канцеляризмов и догадок о пользователе,
включая его пол. Описывай только доступные возможности. Не соглашайся автоматически: если человек
ошибается, спокойно объясни почему.»

| Measurement | Value |
|---|---|
| Prompt before | 10,044 |
| Opening | 87 → 116 (+29) |
| F02 bullet | +399 |
| F03 paragraph + blank line | +292 |
| Prompt after | **10,764** (+720, +7.17%) |
| Ceiling | **< 10,800** (36 characters of headroom) |
| `RECIPE_PROTOCOL_PROMPT` | 1,831, byte-identical |

## 5. Clause preservation

A diff of `house_helper/core/kitchen_prompt.py` against `HEAD` shows exactly three additions — the
F02 bullet, the F03 paragraph and the opening replacement — and nothing else. Every previously
pinned functional, context, ingredient, occasion/budget, photo, safety, output and boundary clause
still passes its existing parametrization; `PERSONA` and `RECIPE_PROTOCOL_PROMPT` are unchanged; both
channels still send the same prompt object (`api_client.SYSTEM_MESSAGE is KITCHEN_SYSTEM_PROMPT is
openai_chat.KITCHEN_SYSTEM_PROMPT`).

## 6. Deterministic regression results

- Targeted matrix (identity, prompt, menu, dialogs, Telegram chat/binding/favorites/photos, Web
  UI/assets/logic/DOM/integration, orthography, auth/email/invite/mailer, characterization, backend,
  api_client): **2,339 passed / 0 failed / 0 skipped** (3 min 16 s).
- Earlier focused runs: identity + prompt + Web UI 252 passed; Node-backed Web UI, menu and
  orthography 716 passed; Telegram chat/dialogs/binding 131 passed.
- New coverage: greeting module equality; three-surface parity; greeting/help separation; the
  authorised `HELP` sentence; the first suggestion; the F02/F03 clauses, order and count; no
  `Рответ` mapping; the measured length; the login lead; the empty-conversation greeting paragraphs.

Deterministic tests pin text and wiring only. They are not evidence that the real model follows the
intent or voice rules; that is the separately authorised evaluation in section 8.

## 7. Full local test results and skips

All commands ran in the repository root on Windows, Python 3.12.10. No external provider was called
and no production system was accessed.

| Run | Result |
|---|---|
| Targeted matrix | **2,339 passed / 0 failed / 0 skipped** (3 min 16 s) |
| `pytest -q -ra -m "not postgres and not sqlite"` | **10,963 passed / 181 skipped / 1,453 deselected / 0 failed** (25 min 13 s) |
| `pytest -q -ra -m sqlite` | **1,087 passed / 3 skipped / 11,507 deselected / 0 failed** (1 min 53 s) |
| Documentation checks (`test_deploy_scheduler`, `test_deploy_kit`, `test_deploy_offsite_docs`, `test_repo_secrets`) after the documentation edits | **115 passed / 1 skipped** (5.4 s) |
| `git diff --check` | no whitespace errors (LF→CRLF warnings only) |

The 181 generic skips are the repository's existing environment-dependent ones (POSIX modes and
signals, `/proc` and `flock(2)`, a Docker image variable) and are unrelated to this change; the 3
SQLite skips are POSIX file-lock/mode tests on Windows. No new skip was introduced. **PostgreSQL-marked
suites (164 tests) were not run locally:** no verified isolated PostgreSQL 16 environment was
provisioned, and production connection details were never used. They remain a mandatory CI gate, not
a passing result.

## 8. Behavioural evaluation contract (design only at the time of this record — subsequently executed on 2026-11-25)

The **complete** authoritative contract — configuration, the 15-scenario matrix, acceptance and
stopping rules, and evidence requirements — is recorded in section 7 of the Scope Lock. It is not
duplicated here; this record states only that it is design only.

**KHD-46:** executing the real-provider evaluation requires separate Owner authorization. No
execution authorization currently exists. The contract is not executed in this task, no provider
credentials are used, and no provider call is made. Raw evidence, when authorised, stays outside the
repository.

*[Superseded 2026-11-25: the evaluation was subsequently separately authorized and executed as
`owner-approved-2026-11-25-a`. See section 12 and
`HH-BRAND-01-FIX-01_Behavioral_Evaluation_Record.md`. The statement above remains the historical
state at the time of this implementation record.]*

## 9. Limitations and deferred items

- Prompt compliance is model behaviour; only the evaluation can establish it.
- Repetition is the model's best effort, not retrieval: no exact-favorite retrieval or zero-cost
  repetition is promised.
- The legacy `bot.py` `/start` still says «Kitchen Helper» (D9) and the BotFather display name is
  unchanged.
- Deferred, non-blocking test coverage: there is no complete automated prompt-reconstruction
  regression assertion. The independent audit verified the complete current prompt by byte-for-byte
  reconstruction; a test that pins the full effective structured prompt remains a deferred item.
  No test is modified in this documentation task.
- Deferred: deterministic intent classifier, response filter, recipe caching or deduplication,
  Equipment/Budget domains, UX Package, and any prompt trim.

## 10. Independent audit evidence

The independent Codex audit of 2026-11-25 returned **PASS WITH NON-BLOCKING NOTES**. Recorded
evidence:

- Exact 14-path scope: **11 modified tracked files and 3 new files** — `house_helper/web/greeting.py`,
  `docs/product/HH-BRAND-01-FIX-01_Scope_Lock.md` and this record.
- Independent targeted matrices: **2,033 passed, 1 skipped, 2 deselected.**
- Initial temporary-directory permission errors were resolved by an authorized rerun; the rerun
  produced the matrix above.
- A complete independent prompt reconstruction matched the shipped prompt **byte-for-byte**.
- `PERSONA` and `RECIPE_PROTOCOL_PROMPT` were preserved.
- No unavoidable semantic contradiction was confirmed.
- All **443** tracked or unignored files remained unchanged during the audit.
- PostgreSQL CI and the real-provider evaluation remain mandatory gates.
- No confirmed blocking implementation defect; the audited implementation is preserved unchanged.

Prompt measurements verified by that reconstruction:

- `KITCHEN_SYSTEM_PROMPT`: **10,764** characters.
- `RECIPE_PROTOCOL_PROMPT`: **1,831** characters.
- Complete effective structured prompt: **12,595** characters.
- Hard ceiling for `KITCHEN_SYSTEM_PROMPT`: strictly below **10,800**.

SHA-256 values:

- Shared prompt:
  `079ee60d80b007d92adced32762c87389f16746b06f421652586af49dc3b37cf`
- Complete effective structured prompt:
  `bbb1b95c852cf6752a2203a6e98699e10031f1f5e8b03aae26ea72a4f0b3fcf4`

Non-blocking deferred test-coverage item: there is no complete automated
prompt-reconstruction regression assertion. The independent audit verified the complete current
prompt; no test is modified in this documentation task (see section 9).

The audited production and test files were byte-identical before and after this documentation
reconciliation. Their SHA-256 values at the audited revision:

| Path | SHA-256 |
|---|---|
| `house_helper/web/greeting.py` | `57beda52bb2967bf5deb0106a80bafe3602b7b302f2852136ec5030cddb76a47` |
| `house_helper/web/ui.py` | `bef018ab230592524dd540486c7907083e92e073db46d86850ea89e39ebca100` |
| `house_helper/web/telegram_chat.py` | `8817f17066f61dd34915c2e0b175ad03e801a753d0f54e1e59e47a9f7c31f236` |
| `house_helper/web/static/chat-logic.js` | `6853a490b3e66ae39783835a312f7bafeccd829c569708bbd6c3c819e1a20b82` |
| `house_helper/web/static/app.js` | `aab4756820aa9f78c0ff8ee5bc86ddb4f712994a3604581d916dfa50ecdab554` |
| `house_helper/core/kitchen_prompt.py` | `70c79d008cac79e6503b1034b5ff9c749d1d25e6fa89ed776d64440ac1f23fcc` |
| `tests/test_assistant_identity.py` | `5788b1e45efb609c3e0092e257b126db09914f80aa4b5fb6cb3ef78e7e71cbe1` |
| `tests/test_kitchen_prompt.py` | `cfbffcf839ef704940233a1a4213358bef20c007bcd0ffbb8fdcd102f0315d9c` |
| `tests/test_web_ui.py` | `971020d09f7fcd298bde211e91a03e1f59d604a27a27be1170f0f4438c607d58` |
| `tests/js/dom_scenarios.js` | `e93c41bb2f7a89f4c1d08867ccc68401d4068ab7b91818e0cadf0cd519bc482d` |

This documentation closure modifies only the Scope Lock, this record and `docs/DECISIONS.md`, all
within the accepted 14-file scope. No production code or test file changed.

## 11. Git diff summary and status

**Eleven modified tracked files** plus **three new files** (`house_helper/web/greeting.py`,
`docs/product/HH-BRAND-01-FIX-01_Scope_Lock.md` and this record); the twelve pre-existing untracked
artifacts are untouched; nothing is staged. Not committed, pushed, released or deployed. Independent
audit: **PASS WITH NON-BLOCKING NOTES**. Owner code acceptance and commit are the next gates.

## 12. Behavioral evaluation outcome (2026-11-25)

The separately authorized real-provider behavioral evaluation was executed as
`owner-approved-2026-11-25-a` against commit `77e2bfd2f513e9285e0219d1d0f89538acf58610`. It
completed all 30 planned requests with no early termination: **24 PASS / 6 FAIL**, zero critical
safety violations. The six failures are samples 1–2 (intent resolution), 21–22 (recipe fidelity) and
24–25 (capability accuracy).

Recorded statuses: technical implementation **ACCEPTED**; evaluation execution **COMPLETED**;
behavioral acceptance **FAILED**; production release **BLOCKED**; remediation **OPEN**.
HH-BRAND-01-FIX-02 is registered as a pending architectural investigation. No architecture is
selected, and no deployment of `77e2bfd` is authorized.

The authoritative record, including evidence verification, the failed cases, the supplementary
language observations and the release gate, is
[`HH-BRAND-01-FIX-01_Behavioral_Evaluation_Record.md`](HH-BRAND-01-FIX-01_Behavioral_Evaluation_Record.md).
This section changes no implementation, prompt, test, harness, manifest or rubric; it adds only the
outcome and the pointer.
