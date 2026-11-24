# HH-BRAND-01 — Implementation Record

**Status:** IMPLEMENTED / READY FOR INDEPENDENT CODEX REVIEW — the implementation and the
Scope Amendment 01 test correction (section 7) are complete in the working tree, and the
local targeted matrix, the Telegram chat suite and the documentation-dependent checks are
green. This document records local implementation and testing evidence only. It does not
claim a commit, a push, CI success, independent review acceptance, owner acceptance,
real-provider behavioural acceptance or deployment.

**Date:** 2026-11-24

**Baseline:** branch `main`, HEAD `93f39f498131705e52dd42dad466253f0a57a6e1`, tracked
worktree clean, nothing staged, the twelve pre-existing untracked artifacts untouched.

**Scope:** `docs/product/HH-BRAND-01_Scope_Lock.md` and KHD-44 in
`docs/DECISIONS.md`.

## 1. What changed

The assistant now has one name, **Федя**, and one tone paragraph shared by every channel.
A new domain-neutral module holds the name and the verbatim owner-provided persona; the
shared Kitchen prompt composes them; the Web and Telegram copy use the approved informal
voice. The platform brand remains **House Helper**, and neutral account, privacy, error
and operational wording remains formal.

The prompt grew from 9,810 to **10,044** characters (+234, +2.39%), under the approved
hard ceiling of **10,200**. The only prompt changes are:

1. the opening sentence (name + persona + retained language/length lines);
2. P1, the shopping-check phrase: «проверь, есть ли у тебя это; купи, если нужно»;
3. P2, the dish-choice offer: «Выбери понравившееся блюдо…».

A diff against `HEAD` confirms that every other clause is byte-identical.
`RECIPE_PROTOCOL_PROMPT` is untouched.

## 2. Files changed

Nineteen allowlisted paths (eighteen as originally authorized plus
`tests/test_telegram_chat.py` under Scope Amendment 01); no other tracked file changed.

**Production (6)**

- `house_helper/core/assistant_identity.py` (new) — `ASSISTANT_NAME`, `PERSONA`; no
  imports, no I/O, no domain constants.
- `house_helper/core/kitchen_prompt.py` — imports the identity module; composes the new
  opening; applies P1/P2; docstring updated.
- `house_helper/web/ui.py` — W1 login lead via `ASSISTANT_NAME`; informal first-visit
  hint.
- `house_helper/web/static/chat-logic.js` — W2 shared help/empty-state, W4/W5 status
  messages, W6 both composer hints, W7/W8 favorites-empty hints.
- `house_helper/web/static/app.js` — W3 accessibility attribution «Федя, помощник:».
- `house_helper/web/telegram_chat.py` — T1 greeting, T2 help, T3/T4/T5/T6/T7/T8;
  `PRIVACY`, the command list and all operational strings untouched.

**Tests (8)**

- `tests/test_assistant_identity.py` (new) — independent name/persona literals,
  import-free/domain-neutral checks, single persona definition, prompt opening and
  integration, Telegram greeting/help/relay, Web lead, browser-literal cross-check,
  accessibility attribution, presentation-only greeting.
- `tests/test_kitchen_prompt.py` — ceiling 10,200 and measured 10,044; P1/P2 expectations.
- `tests/test_menu_recommendations.py` — P1 in two places, `RECIPE_OFFER`, help and Web
  help expectations.
- `tests/test_telegram_dialogs.py` — dialog header and selected-conversation sentence.
- `tests/test_telegram_chat.py` (Scope Amendment 01) — only the two independent expected
  literals in
  `test_the_bot_says_that_the_website_is_copied_to_telegram_and_how_to_stop_it`, from
  «пересылаю сюда» to «пересылается сюда», for `HELP` and `GREETING`; the `LINKED`
  parametrization, the `/unlink` assertion and every other test are unchanged.
- `tests/test_web_ui.py` — login lead and first-visit hint.
- `tests/js/dom_scenarios.js` — speaker attribution and composer hint.
- `tests/js/logic_scenarios.js` — composer hint.

**Documentation (5)**

- `docs/DECISIONS.md` — KHD-44.
- `docs/product/HH-BRAND-01_Scope_Lock.md` (new).
- `docs/product/HH-BRAND-01_Implementation_Record.md` (new, this file).
- `docs/STATUS.md` — one dated implementation/testing entry; historical rows untouched.
- `README.md` — one identity line; stale «Кухонный помощник» corrected to «House Helper».

## 3. Final copy evidence

- Web login lead: «Привет! Я Федя — твой помощник по домашней кухне. Подскажу, что
  приготовить из того, что есть дома, и расскажу, как приготовить блюдо.»
- Shared help / empty thread: «Я Федя. Спроси, что приготовить из имеющихся продуктов
  или как приготовить блюдо. Могу предложить идеи блюд: выбери понравившееся — дам
  полный рецепт, который можно сохранить в избранное кнопкой рядом с рецептом.»
- Telegram greeting: «Привет! Я Федя — твой помощник в House Helper.» followed by the
  revised `HELP` (existing composition preserved).
- Telegram `HELP`: informal body; save button «под моим ответом»; relay «все, что ты
  пишешь мне на сайте, вместе с моим ответом пересылается сюда» with both the
  before-selection and after-selection conditions; command list and `PRIVACY` unchanged.
- Recipe unavailable: «Этот рецепт больше недоступен. Попроси меня показать его снова.»
- Selected conversation, both variants: «Продолжай писать — я учту контекст этого
  разговора.»
- First-visit hint: «Здесь впервые? Открой ссылку-приглашение, которую тебе прислали:
  она нужна только для первого входа.»
- Status and attribution: «Федя ответил.», «Федя готовит ответ…», «Федя, помощник:».
- All new or revised copy uses «е», not «ё».

## 4. Test evidence

All commands ran in the repository root on Windows, Python 3.12.10, Node.js v24.14.0.
No external provider was called and no production system was accessed.

### 4.1 Targeted regression matrix

```
python -m pytest -q tests/test_assistant_identity.py tests/test_kitchen_prompt.py \
  tests/test_menu_recommendations.py tests/test_telegram_dialogs.py \
  tests/test_telegram_chat.py tests/test_telegram_binding.py \
  tests/test_telegram_favorites*.py tests/test_telegram_photos.py \
  tests/test_web_ui.py tests/test_web_ui_assets.py tests/test_web_ui_logic.py \
  tests/test_web_ui_dom.py tests/test_web_ui_integration.py \
  tests/test_orthography.py tests/test_web_auth*.py tests/test_web_email_*.py \
  tests/test_web_invite_*.py tests/test_web_mailer.py \
  tests/test_characterization_*.py tests/test_backend.py tests/test_api_client.py
```

Pre-amendment result (preserved as recorded; not retroactively revised): **2 failed,
2,264 passed** (3 min 18 s). The two failures are the exact test ids recorded in section
7; every other targeted test, including all Node-backed Web UI scenarios, passed. The
post-amendment rerun is in section 4.4.

### 4.2 Full applicable local suites

```
python -m pytest -q -ra -m "not postgres and not sqlite"
python -m pytest -q -ra -m sqlite
```

Results:

- Generic suite (pre-amendment, preserved as recorded): **2 failed, 10,937 passed, 181
  skipped, 1,453 deselected** (27 min 39 s). The two failures are the same test ids as the
  targeted run.
- SQLite-scoped suite: **1,087 passed, 3 skipped, 11,483 deselected** (1 min 56 s). No
  failures. The skips are POSIX file-lock/mode tests on Windows. No SQLite-scoped test
  exercises the corrected literal, whose outcome is the same on either backend.

**Not run:** the PostgreSQL-marked suites (164 tests). They require a verified isolated
PostgreSQL 16 environment under the existing safety procedures; none was provisioned for
this local stage, and production connection details were never used. Their CI run remains
a mandatory gate, not a passing result.

### 4.3 Specific coverage

- The exact name «Федя» is asserted independently of the module that defines it.
- The identity module is parsed and shown to contain no imports and no public constant
  beyond `ASSISTANT_NAME` and `PERSONA`; `PERSONA` is defined in exactly one module.
- The prompt opens with the name, contains the persona exactly once, and has no obsolete
  formal-address instruction.
- The prompt diff against `HEAD` shows only the opening, P1 and P2; all functional,
  context, ingredient, occasion/budget, photo, safety and output-contract clauses are
  unchanged.
- Telegram greeting, help, relay conditions, command list and privacy appendix are
  checked; no formal address remains in the assistant’s own help body.
- Web login lead, first-visit hint, browser literals and accessibility attribution are
  checked; the shell still has exactly the `House Helper` headings, title and brand.
- The greeting is produced by `render_shell` from flags only; it is not a stored message.
- Conversation selection and relay behaviour, privacy text and technical message
  contracts are unchanged and covered by the existing suites.
- Existing functional and security regressions ran in the full local suites above. All
  passed except the two stale relay-literal expectations recorded in section 7.

Deterministic tests pin text and wiring only. They are **not** proof that the real model
follows the persona; that is a separate mandatory real-provider evaluation gate.

### 4.4 Post-amendment verification (Scope Amendment 01)

Commands ran in the repository root on Windows, Python 3.12.10. No external provider was
called and no production system was accessed.

- The exact two previously failing cases:
  `python -m pytest -q "tests/test_telegram_chat.py::test_the_bot_says_that_the_website_is_copied_to_telegram_and_how_to_stop_it"`
  → **3 passed** (both corrected parametrizations and `LINKED`).
- The whole module: `python -m pytest -q tests/test_telegram_chat.py` → **63 passed**.
- The targeted regression matrix of section 4.1, rerun with the amended file →
  **2,266 passed, 0 failed, 0 skipped** (3 min 22 s). This is the same 2,266 collected
  tests as the pre-amendment run (2,264 passed + 2 failed); every one now passes.
- Documentation-dependent checks after the documentation edits:
  `python -m pytest -q tests/test_deploy_scheduler.py tests/test_deploy_kit.py tests/test_deploy_offsite_docs.py tests/test_repo_secrets.py`
  → **115 passed, 1 skipped**.
- `git diff --check` → no whitespace errors (only the repository's LF→CRLF warnings).
- `git diff tests/test_telegram_chat.py` → exactly one line changed, as shown in section
  7.1. Production files are byte-identical to the implementation recorded in sections 1–3.

A repeated full generic or SQLite run was not required for this test-and-documentation-only
correction, and none was performed. A complete green CI run remains a requirement before
release acceptance. The existing CI workflow (`.github/workflows/ci.yml`) **does** include
PostgreSQL coverage on both Python legs: a digest-pinned PostgreSQL 16 service on loopback
port 55432, a preflight, a collection assertion, `alembic check`, the `-m postgres` suites
and the generic suite on the PostgreSQL backend, plus a separate SQLite-scoped step.
PostgreSQL tests have **not** been run for this working tree.

## 5. Boundaries and residual risks

- `bot.py`, `api_client.py`, `context_manager.py`, `config.py`, the diagnostic catalogue,
  authentication/invite/mailer code, persistence, migrations, deployment, the accepted
  KH-F01 Product Specification and all historical records are unchanged.
- No schema, API, authentication, provider, dependency or configuration change.
- `PRIVACY` remains formal by design; the Telegram help is a mixed-register message.
- The legacy standalone bot still introduces itself as «Kitchen Helper»; it inherits the
  revised prompt indirectly. The BotFather display name is unchanged. Both are accepted
  residuals.
- P1 and P2 are model-output wording inside the prompt; no production parser or
  exact-match consumer reads them. The only exact-match consumers are tests, updated
  here.
- The prompt costs 234 more characters on every request in both channels.

## 6. Git state and gates

- Working tree: only the nineteen allowlisted paths; the twelve pre-existing untracked
  artifacts are untouched; nothing is staged.
- **Ready for independent Codex review.** The two previously failing tests are corrected
  under Scope Amendment 01 (section 7); nothing outside the amended allowlist changed.
- **Not done:** commit, push, independent Codex review, CI, real-provider behavioural
  evaluation, owner acceptance, deployment. A12 remains a separate pending operational
  gate.

## 7. Blocking scope gap — required decision

`tests/test_telegram_chat.py` is not one of the eighteen authorized paths; the approved
Scope Lock lists it as regression-only, with no edits. It pins the old relay marker
«пересылаю сюда» in two parametrizations:

- `tests/test_telegram_chat.py::test_the_bot_says_that_the_website_is_copied_to_telegram_and_how_to_stop_it[HELP-пересылаю сюда]`
- `tests/test_telegram_chat.py::test_the_bot_says_that_the_website_is_copied_to_telegram_and_how_to_stop_it[GREETING-пересылаю сюда]`

The Owner-approved T2 copy replaces the relay description with «все, что ты пишешь мне на
сайте, вместе с моим ответом пересылается сюда». The test's expected marker is therefore
stale, while the behaviour it guards is unchanged: before a conversation is selected the
bot still relays site messages and says so, and after selection only the selected
conversation is relayed. Because the file is outside the allowlist, it was not edited.

**Proposed amendment (as recorded before authorization):** authorize
`tests/test_telegram_chat.py` and change only the parametrized mirror literal for `HELP`
and `GREETING` from «пересылаю сюда» to «пересылается сюда». No production code or other
test change is needed.

### 7.1 Scope Amendment 01 — authorized and applied

The Owner authorized the amendment on 2026-11-24 (Scope Lock section 5.1): exactly one
file added, the maximum count raised to 19. The applied change is the two expected
literals only:

```diff
-    [("HELP", "пересылаю сюда"), ("GREETING", "пересылаю сюда"), ("LINKED", "будут приходить и сюда")],
+    [("HELP", "пересылается сюда"), ("GREETING", "пересылается сюда"), ("LINKED", "будут приходить и сюда")],
```

The semantic intent is preserved. The test still proves, for `HELP` and `GREETING`, that
the bot tells the person the site is copied to Telegram, and still proves that both texts
name `/unlink` as the one way to stop the copies; the `LINKED` parametrization, its
separate expected phrase and the relay conditions are untouched. The expected values
remain independent literals, not production-derived constants, and every parametrization
remains. No production file changed.

Post-amendment evidence is recorded in section 4.4. PostgreSQL-marked suites remain
unrun locally (section 4.2); CI's PostgreSQL coverage is confirmed but not executed for
this working tree.
