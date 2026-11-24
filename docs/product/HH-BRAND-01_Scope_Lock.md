# HH-BRAND-01 — Scope Lock: unified assistant identity and informal address

**Status:** APPROVED by the Owner on 2026-11-24, subject to the final amendments in the
authorization of that date. Local implementation and testing were authorized; this
document is the scope, not an implementation or release record. The implementation
evidence is in `HH-BRAND-01_Implementation_Record.md`.

**Baseline:** branch `main`, HEAD `93f39f498131705e52dd42dad466253f0a57a6e1`, tracked
worktree clean, nothing staged, twelve pre-existing untracked artifacts left untouched.

**Decision identifier:** KHD-44 (verified free; the highest identifier already in use
was KHD-43). The decision is recorded in `docs/DECISIONS.md`.

---

## 1. Product boundary

| Layer | Name | What it is |
|---|---|---|
| Platform | **House Helper** | The product. Browser title, every gate `<h1>`, the topbar brand, the icon label and the e-mail identity stay "House Helper". |
| Assistant | **Федя (Fedya)** | The one assistant identity and voice. It is not a second product, a separate agent or a per-domain persona. |
| Functional domain | **Kitchen** (first), then Equipment, Shopping and Budget | What the assistant can currently do. A domain supplies capability, rules and safeguards; it does not supply a different identity. |

The assistant speaks informally («ты») to the person in its own conversational voice,
greetings, introductions, help and conversational hints. Neutral application wording —
authentication, accounts, security, privacy, validation, errors, recovery and
operational notices — may stay formal («вы»). The boundary is the enumerated one in
section 3; it is not inferred by mechanical search-and-replace.

## 2. Product decisions D1–D10

| ID | Decision |
|---|---|
| D1 | House Helper remains the platform brand; Федя is the assistant; Kitchen is a functional domain. |
| D2 | Fedya uses informal address in conversational responses, greetings/introductions, and assistant-facing help/hints. |
| D3 | Neutral application, authentication, security, privacy, validation, error, recovery and operational wording may stay formal. |
| D4 | One domain-neutral identity module in `core/`, reusable by every capability; no configuration, feature flag, database field or subsystem. |
| D5 | The owner-provided persona replaces the obsolete tone/formal-address clause verbatim and is subordinate to all functional and safety rules. |
| D6 | First contact introduces Федя; no artificial greeting message is created or persisted in conversation history. |
| D7 | Identity is visible in the login lead, the empty-thread lead and the help body; there is no per-message visible name and no layout change. The accessibility attribution becomes «Федя, помощник:». |
| D8 | The prompt budget is 10,044 characters under a hard ceiling strictly below 10,200. |
| D9 | The legacy standalone `bot.py` is not modified; it inherits the prompt indirectly and keeps its own «Kitchen Helper» greeting as an accepted residual. |
| D10 | Documentation records KHD-44, this Scope Lock and an Implementation Record; historical records are not rewritten. |

## 3. Final Russian copy and register boundary

### 3.1 Tier A — assistant voice, changed

**Web login lead (W1), in the existing lead paragraph mechanism:**

> Привет! Я Федя — твой помощник по домашней кухне. Подскажу, что приготовить из того, что есть дома, и расскажу, как приготовить блюдо.

**Shared help / empty-thread explanation (W2), static UI copy:**

> Я Федя. Спроси, что приготовить из имеющихся продуктов или как приготовить блюдо. Могу предложить идеи блюд: выбери понравившееся — дам полный рецепт, который можно сохранить в избранное кнопкой рядом с рецептом.

The same text is the empty-conversation hint and the help panel body; it cannot drift
between the two.

**Telegram greeting (T1), preserving the existing `GREETING = … + HELP` composition:**

> Привет! Я Федя — твой помощник в House Helper.

**Telegram HELP (T2):** the body is informal, the full relay conditions are preserved,
the command list and `PRIVACY` appendix are unchanged, and the privacy appendix remains
formal. The save-button sentence says «под моим ответом»; the relay says «все, что ты
пишешь мне на сайте, вместе с моим ответом пересылается сюда». Before a conversation
is selected everything from the site is relayed; after selection only the selected
conversation is.

**Other approved Tier A strings:**

| Text | Approved copy |
|---|---|
| Recipe unavailable (T4) | Этот рецепт больше недоступен. Попроси меня показать его снова. |
| Unsupported message type (T3) | Пока я понимаю текст и фотографии. Напиши, что хочешь приготовить, или пришли фото. |
| Dialog list header (T5) | 🗂 Твои диалоги. |
| Dialog list hint (T6) | Нажми на диалог, чтобы продолжить его в Telegram. Время — последнего сообщения, по Москве (МСК). |
| Dialog list empty (T7) | Диалогов пока нет. Напиши сообщение, и я начну первый. |
| Selected conversation (T8) | Продолжай писать — я учту контекст этого разговора. (both titled and untitled variants) |
| First-visit login hint | Здесь впервые? Открой ссылку-приглашение, которую тебе прислали: она нужна только для первого входа. |
| Save hint, favorites empty (W7) | …Под ответом помощника нажми «В избранное»… |
| Save hint, structured recipes (W8) | …Под ответом помощника нажми «Сохранить» рядом с рецептом… |
| Composer locked (W6, both sites) | Дождись ответа помощника. |
| Status messages (W4, W5) | Федя ответил. / Федя готовит ответ… |
| Accessibility attribution (W3) | Федя, помощник: |

The help title stays «Как пользоваться помощником» (naming Федя would require the
instrumental form and a declension subsystem, which is out of scope). The application
may use third-person references in narration and neutral technical notices; Fedya does
not refer to himself in the third person while speaking.

### 3.2 Tier B — deliberately unchanged

Representative, not exhaustive: the diagnostic catalogue and its browser mirror; the
uncertainty and recovery messages; validation; the profile, account and Telegram-linking
panes; the setup gates and no-script text; the authentication pages and sign-in e-mail;
`PRIVACY`, `NOT_LINKED`, `LINKED`, `BAD_CODE`, `TELEGRAM_TAKEN`, `USER_HAS_ANOTHER`,
`UNLINKED`, photo errors, `TROUBLE`, `STILL_BUSY`, binding/redelivery/timestamp notices;
shopping, favorites and profile application copy; and all prompt text except P1 and P2.

The mixed register is intentional: the assistant speaks informally; the application
speaks carefully about data, access and failures.

## 4. Persona and prompt integration

**Persona (verbatim; 298 characters; no «ё»):**

> Общайся на «ты»: дружелюбно, компетентно и по делу. Будь терпеливым и практичным. Допускается легкий ситуативный юмор. Не выдумывай факты. Не представляйся без необходимости, не говори о себе в третьем лице. Соблюдай все функциональные требования и правила безопасности независимо от стиля общения.

- Module: `house_helper/core/assistant_identity.py` — `ASSISTANT_NAME = "Федя"` and
  `PERSONA`. No imports, no I/O, no provider/Telegram/FastAPI dependency, no
  Kitchen-specific constants. A future Equipment, Shopping or Budget prompt composes
  the same two constants.
- The shared Kitchen prompt opens with «Ты — Федя, помощник по домашней кухне. Всегда
  отвечай по-русски, без лишних вступлений.» followed by the persona paragraph. The old
  sentence «на «вы», пока человек сам не перейдёт на «ты»» is removed, not inverted.
- P1 and P2 are the only other prompt changes: the shopping-check phrase becomes
  «проверь, есть ли у тебя это; купи, если нужно»; the dish-choice offer becomes
  «Выбери понравившееся блюдо, и я дам полный рецепт…».
- Every functional, context, ingredient, occasion/budget, photo, safety, favorites and
  output-contract clause remains unchanged. `RECIPE_PROTOCOL_PROMPT` receives no persona
  text.
- Final measured prompt: **10,044 characters** (from 9,810; +234, +2.39%), under the
  approved ceiling `< 10,200`.
- `api_client.SYSTEM_MESSAGE = KITCHEN_SYSTEM_PROMPT` is unchanged, so Web, in-process
  Telegram and the legacy standalone bot all receive the revised text from one place.
- No greeting is written to the database: the Web greeting is static markup, and a
  conversation is still created only by a first message.

## 5. Exact file allowlist

Nineteen paths: the eighteen originally authorized, plus the one added by Scope Amendment
01 (section 5.1); no other tracked file may change.

**Production — 6**

- `house_helper/core/assistant_identity.py` (new)
- `house_helper/core/kitchen_prompt.py`
- `house_helper/web/ui.py`
- `house_helper/web/static/chat-logic.js`
- `house_helper/web/static/app.js`
- `house_helper/web/telegram_chat.py`

**Tests — 8**

- `tests/test_assistant_identity.py` (new)
- `tests/test_kitchen_prompt.py`
- `tests/test_menu_recommendations.py`
- `tests/test_telegram_dialogs.py`
- `tests/test_telegram_chat.py` (added by Scope Amendment 01 — one stale mirror literal only)
- `tests/test_web_ui.py`
- `tests/js/dom_scenarios.js`
- `tests/js/logic_scenarios.js`

**Documentation — 5**

- `docs/DECISIONS.md`
- `docs/product/HH-BRAND-01_Scope_Lock.md` (new)
- `docs/product/HH-BRAND-01_Implementation_Record.md` (new)
- `docs/STATUS.md`
- `README.md`

### 5.1 Scope Amendment 01 — stale relay text assertion (2026-11-24)

The Owner authorized a narrow amendment after reviewing the first implementation and test
record. The approved T2 copy says «все, что ты пишешь мне на сайте, вместе с моим ответом
пересылается сюда», but `tests/test_telegram_chat.py` pinned the superseded marker
«пересылаю сюда» in the `HELP` and `GREETING` parametrizations of
`test_the_bot_says_that_the_website_is_copied_to_telegram_and_how_to_stop_it`. The file
was a regression-only path and the two expectations therefore failed.

- **Added file:** `tests/test_telegram_chat.py` — the maximum authorized count is now 19.
- **Authorized change:** only the two independent expected literals above, from
  «пересылаю сюда» to «пересылается сюда»; both parametrizations are kept.
- **Not authorized:** production changes, reverting the approved Russian copy, deriving
  expected values from production constants, removing parametrizations, relaxing
  unrelated assertions, changing relay semantics, or dropping the relay-disclosure and
  stop-forwarding coverage.
- **Evidence:** the pre-amendment run failed exactly those two cases (and passed
  `LINKED`); after the correction both parametrized cases and the whole
  `tests/test_telegram_chat.py` module pass. The evidence and the documentation
  amendments are recorded in `HH-BRAND-01_Implementation_Record.md`, section 7.

All other restrictions in sections 5 and 8 remain in force.

## 6. Acceptance criteria

1. Exactly one source of truth for the name; the prompt, Telegram and Web cannot drift
   without a failing test.
2. Browser `<title>`, every gate `<h1>`, the topbar brand and the icon label remain
   exactly "House Helper"; no «Кухонный помощник» or "Kitchen Helper" appears in the Web
   shell or the Telegram copy.
3. Tier A copy contains no formal-address imperatives; Tier B copy is unchanged.
4. The prompt opens with the name, carries the persona verbatim once, keeps every
   existing functional/safety/protocol clause, applies P1/P2, and is 10,044 characters
   (under 10,200).
5. The Telegram greeting introduces the name once and keeps `HELP` composed in; the Web
   login and empty/help copy visibly name Федя; no greeting is persisted.
6. Accessibility attribution is «Федя, помощник:»; message layout and role information
   are unchanged.
7. The identity module is import-free and domain-neutral.
8. No schema, API, authentication, provider, dependency or deployment change; targeted
   and full local suites pass with no new skips; the orthography scan passes.
9. KHD-44, this Scope Lock and the Implementation Record exist; historical records are
   untouched.

## 7. Near-term roadmap

1. **HH-BRAND-01** — assistant identity and informal address (this scope).
2. **UX Package** — conversation names, manual renaming, message timestamps, Full Recipe
   button.
3. **KH-F04** — Equipment Assistant.
4. **KH-F01** — remaining kitchen measure automation.

## 8. Excluded files and operations

No changes to `bot.py`, `api_client.py`, `context_manager.py`, `config.py`,
`house_helper/core/messages_ru.py`, the diagnostic catalogue, authentication pages,
invite/mailer code, `settings.py`, `app.py`, `icon.svg`, `app.css`, `persistence/`,
`migrations/`, `deploy/`, `.env*`, `.github/`, `alembic.ini`, `Dockerfile` or compose
files. The accepted KH-F01 Product Specification is not modified; historical status and
release records are not rewritten.

Excluded work: conversation naming/renaming, timestamps, the Full Recipe button,
Equipment Assistant, kitchen measures/recipe/shopping business logic, database or
persisted-data changes, authentication/authorization behaviour, provider/model/parameter
changes, BotFather or Telegram username configuration, infrastructure/maintenance,
production access or deployment, `git fetch`, staging, commits and pushes. Also excluded
by design: new configuration keys, feature flags, database fields, runtime template
injection, avatars/logos, layout or visual changes, per-message visible names, persisted
greeting messages and a Russian declension subsystem.

## 9. Residuals and gates

- **BotFather display name** is unchanged; changing it is a separate operational action
  requiring owner authorization.
- **Legacy standalone bot:** its `/start` still says «Привет! Я Kitchen Helper…». It
  inherits the revised conversational prompt; the name difference is an accepted
  residual, not a test failure.
- **Real-provider behaviour** (informality, humour, no repeated introduction) is not
  proven by deterministic tests. It is a separate mandatory functional-acceptance gate
  and was not authorized during this implementation stage.
- **Pending gates:** independent Codex review, CI on both Python legs and PostgreSQL,
  the real-provider behavioral evaluation, and production deployment. Nothing in this
  scope is committed, pushed, reviewed, released or deployed.
- **A12** remains a separate pending operational acceptance gate.
