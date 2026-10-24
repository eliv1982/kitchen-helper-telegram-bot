# House Helper

[![CI](https://github.com/eliv1982/kitchen-helper-telegram-bot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/eliv1982/kitchen-helper-telegram-bot/actions/workflows/ci.yml)

House Helper is a modular household assistance application that started as a Telegram cooking bot. Its first domain, **Kitchen**, focuses on practical recipe guidance and everyday cooking workflows, with a shared application core designed for multiple interfaces and future household domains.

> **Status, 2026-10-24.** A technical web deployment exists at <https://house.elivcloud.org> and is under technical validation. It is not a public demo: sign-in providers and the OpenAI integration are not enabled there for real users, no sign-in method is available, and there is no public sign-up. The complete end-user Kitchen experience has not been tested live in production; the Functional Alpha is pending. Details: [Deployment status](#deployment-status).

This is a personal project under active development. The repository keeps its original name (`kitchen-helper-telegram-bot`) and the Telegram bot is still part of it. The product's interface and the assistant's replies are in Russian; code and this overview are in English. The detailed operator and developer notes are in Russian: see the [technical reference](#technical-reference-russian).

## Why the project exists

It started as a Telegram bot that suggests simple dishes from the ingredients a person already has. The first intended users are family members: Russian-speaking, using both a phone and a desktop, for whom a browser interface matters and for whom earlier conversations should not be lost ([product baseline](docs/product/PRODUCT_BASELINE.md)).

That shaped the design: Russian-language replies and interface, a responsive web page, a closed set of known users instead of public sign-up, and conversation history stored per user. Cooking is the first domain, with the aim of making cooking guidance more accessible and useful in everyday situations, where a good answer depends on context: what is at hand, how much time there is, what was already discussed.

## What exists today

"Tested" means covered by the automated test suite, which runs offline: it makes no real calls to Telegram, OpenAI or a mail server.

| Component | What it is | Status |
|---|---|---|
| **Kitchen domain** | A prompt-guided conversational assistant in Russian for three kinds of request: what to cook from the ingredients on hand, how to cook a named dish, and open-ended "what should I cook". It handles follow-ups, adjacent questions (substitutions, storage, thawing) and keeps to food and the kitchen, with safety boundaries (no medical advice, no unverified canning advice, a kind reply to a person in distress). The same system prompt serves the bot and the web app. | Implemented. The prompt's clauses are pinned by tests. There is no recipe database, saved-recipe feature or preference storage yet, and the web chat has not been evaluated against the real model. |
| **Shared core** (`house_helper/core`) | Transport-neutral pieces used by both interfaces: conversation identity, outcome types, limits, the Kitchen prompt and an AI-backend interface. | Implemented, tested. |
| **Telegram bot** (`bot.py`) | `/start`, `/reset`, a bounded in-memory history (20 messages), a 2000-character message limit, a typing indicator and error handling for Telegram and OpenAI failures. | Implemented, tested. A separate process with no database; it is not part of the container image. |
| **Web application** (`house_helper/web`) | FastAPI service: sign-in routes, a conversation API (create and list conversations, read history, send a message under an idempotency key) and a responsive Russian browser UI served by the app itself (plain HTML, CSS and JavaScript, no build step). | Implemented, tested (Python tests plus UI scenarios run under Node.js). Deployed technically; sign-in is disabled there. |
| **Persistence and identity** (`house_helper/persistence`) | SQLAlchemy 2 models and six Alembic migrations: users, allowlist entries, identity links, sessions, sign-in state, invitations, conversations, messages, turn records. One user can have several ways to sign in; ownership is enforced through the schema. | Implemented, tested on SQLite. SQLite is what the deployment uses; PostgreSQL is covered only by opt-in test suites that CI does not run. |
| **Authentication** | Telegram sign-in (OpenID Connect, Authorization Code with PKCE) and passwordless email links. Closed allowlist, no self-registration. Telegram is linked to a user through a one-time invitation issued by the operator. Server-side sessions, 14 days. | Implemented, tested against stand-ins. Never run against real Telegram or a real mail server. Not enabled in the technical deployment. |
| **Retention and operator tooling** (`house_helper/admin`) | Operator commands: `provision-email`, `enroll-user`, `reissue-invitation`, `revoke-invitation`, `enrollment-status`, `purge-expired`. Messages expire after 365 days; `purge-expired` deletes expired data. | Implemented, tested. The cleanup's systemd timer is installed and active on the technical deployment; its first automatic run, scheduled for 2026-10-25, has not been verified yet (see [Security and data lifecycle](#security-and-data-lifecycle)). |
| **Deployment kit** (`Dockerfile`, `deploy/`) | Docker image and Compose file for a host that already runs Traefik, backup, verify and restore tooling, a fail-closed upgrade script, opt-in systemd units for the cleanup, and an English [runbook](deploy/RUNBOOK.md). | Implemented. Used for the technical deployment. An encrypted off-host backup and a restore rehearsal are not yet completed. |

## Architecture

```mermaid
flowchart LR
    subgraph Interfaces
        TG["Telegram bot<br/>bot.py · aiogram"]
        WEB["Web application<br/>house_helper/web · FastAPI<br/>sign-in · chat API · browser UI"]
    end

    subgraph Core["Shared core · house_helper/core"]
        KITCHEN["Kitchen domain<br/>system prompt"]
        COMMON["Identity · outcomes · limits"]
        BACKEND["AI backend interface<br/>OpenAI adapters"]
    end

    DB[("Persistence · house_helper/persistence<br/>SQLAlchemy + Alembic<br/>SQLite in the deployment")]
    ADMIN["Operator CLI<br/>house_helper/admin"]

    OPENAI(["OpenAI API"])
    TGAPI(["Telegram<br/>Bot API · OIDC sign-in"])
    SMTP(["SMTP server<br/>email sign-in links"])

    TG --> Core
    WEB --> Core
    WEB --> DB
    ADMIN --> DB
    TG --> TGAPI
    BACKEND -.-> OPENAI
    WEB -.-> TGAPI
    WEB -.-> SMTP
```

Dashed arrows are integrations that the technical deployment does not enable yet. Only the Kitchen domain exists; no other household domain has any code.

- **Two interfaces, one core.** The bot and the web app are separate processes and do not import each other. Both use `house_helper/core`: the same system prompt, limits, conversation identity, outcome types and AI-backend interface.
- **Different memory.** The bot keeps its context in process memory and has no database. The web app stores users, sessions and conversations through `house_helper/persistence`, and each stored turn follows a small lifecycle (begin, complete or fail) so a retried request does not produce a second answer.
- **Schema changes are explicit.** The web app never creates or migrates the database; migrations are a separate operator step with a verified backup first.
- **Users are created by an operator only.** `house_helper/admin` is run by hand; neither the web app nor the bot imports it.

In the deployment kit, requests take this path: browser, then Traefik (TLS), then one `house-helper` container running uvicorn, then SQLite on a Docker volume. The container publishes no port of its own.

## Deployment status

*As of 2026-10-24.*

| | State |
|---|---|
| Technical web deployment | **Exists** at <https://house.elivcloud.org>: the web application in a single Docker container behind an existing Traefik reverse proxy, served over HTTPS with a Let's Encrypt certificate. The container is healthy; the HTTPS and `/healthz` checks pass. |
| Deployed version | Commit `6370bf7` (`6370bf78980e5e8fa8df33361e76620d57fffe79`), image `house-helper:6370bf78980e`. |
| Database | SQLite, at migration `0006_oidc_state_invitation`. |
| Purpose of this instance | **Technical validation** of the deployment path (image, reverse proxy, TLS, response headers, health check) before any real user is enabled. |
| Browser check | A visual smoke test of the live page in a real browser passed: it showed the House Helper branding and the setup screen that appears while sign-in is disabled. |
| Sign-in providers (Telegram, email) | **Not enabled.** |
| OpenAI | **Not enabled for real users.** |
| Users | **None enrolled.** |
| Scheduled cleanup | The systemd timer for `purge-expired` is **installed and active**. Its first automatic execution is scheduled for 2026-10-25 and has **not been verified yet**. |
| Backups | An encrypted off-host backup and a restore rehearsal are **not yet completed**. |
| What a visitor can do | Open the page, which reports that sign-in is not connected yet. No sign-in method is enabled, so the chat cannot be used. There is no public sign-up and no demo account. |
| Functional Alpha | **Pending (not started).** |

On 2026-10-24 the public endpoints were checked from outside: `GET /healthz` returns 200, `GET /auth/me` returns 503 `login_unavailable` (no sign-in method is configured), the page shell reports both sign-in methods as off, and the responses carry HSTS, a strict Content-Security-Policy and frame-denial headers.

**Functional Alpha** is the next milestone: the first use of the Kitchen chat by real users, with real sign-in and real AI replies. It is pending. The open items that stand between the current instance and real users are tracked in [`docs/STATUS.md`](docs/STATUS.md); the main ones, none of them claimed as done, are:

- Real Telegram sign-in: a BotFather client and redirect URL, then a first real login. Telegram sign-in has so far run only against a stand-in. Email sign-in is a fallback whose operational activation is deferred; it would need a real mail path (SMTP with SPF, DKIM and DMARC).
- The first users enrolled through one-time invitations.
- An OpenAI project with an enforced spend limit, then a review of real answers and of truncated replies.
- The first automatic run of the scheduled cleanup (the timer is installed; the run is due on 2026-10-25) observed and checked; a backup schedule with an encrypted off-host copy and a recovery rehearsal on the host; an alert path for failures.
- Ingress rate limits verified on the live proxy; checks on real phones and in more than one browser (apart from one visual smoke test of the live page, the UI has been tried only in headless Chrome).

## Security and data lifecycle

### Implemented measures

These are in the code and covered by automated tests. They are design measures, not guarantees; see the list of what is not claimed below.

- **Closed access.** There is no public sign-up. An operator creates every user; a sign-in method works only if its identifier is on that user's allowlist. A Telegram identity is linked only by a verified Telegram sign-in that presents a one-time invitation (two hours, single use, revocable); it is never typed in, guessed from a username, or matched by email.
- **Sessions.** Random 256-bit tokens in an `HttpOnly`, `Secure`, `SameSite=Lax`, `__Host-` prefixed cookie; only the SHA-256 digest is stored; the lifetime is 14 days and is not extended. Revoking a sign-in method revokes the sessions opened through it.
- **Sign-in flows.** Telegram: PKCE, `state` and `nonce`, with the ID token's signature, issuer and audience checked. Email: the answer is the same whether or not the address is known, the secret sits in the URL fragment so it never reaches a server log, and opening the link does not consume it (a separate confirmation does).
- **Per-user data.** The owner of a conversation comes from the server-side session, never from the request. Another person's conversation and a nonexistent one give the same `404`.
- **Request handling.** The JSON endpoints accept only JSON, with size limits; a request whose `Origin` is not the application's own is rejected, and CORS is not enabled. Each message is sent under an idempotency key. The browser page uses a Content-Security-Policy without inline scripts and renders all text as text.
- **Container and secrets.** The image runs as a non-root user with a read-only root filesystem, all Linux capabilities dropped, `no-new-privileges`, CPU, memory and process limits, and no published port. The build context is an allowlist, so `.env` files, databases and tests cannot enter an image. Credentials come from an environment file kept outside the repository; the repository holds only a template of names.
- **Dependencies.** Direct dependencies are pinned to exact versions.

### Data lifecycle

- **What is stored.** Per-user conversations and message text, turn records (which hold a copy of the answer so a retry can return it), sessions, short-lived sign-in state, and SHA-256 digests of secrets. There are no passwords. The Telegram bot keeps its context only in memory, lost on restart or `/reset`.
- **Retention.** Every message and turn record carries an `expires_at` at most 365 days after creation; expired rows are hidden from reads and from the model's context. `purge-expired` physically deletes them, and deletes sign-in artifacts (sessions, sign-in states, email links, invitations) at their own expiry. Users, allowlist entries and identity links are never deleted by it.
- **Limits of deletion.** Deletion is logical, not forensic erasure. A deleted row survives in older backups, so the 365-day promise holds for the live database only until a backup retention policy exists; that policy is not decided.
- **Third party.** When the OpenAI integration is enabled, message text is sent to OpenAI to produce replies. The Telegram bot tells users so on `/start`.

### Not claimed

- That sign-in has been verified with real Telegram or a real mail server, or that the chat has been tested against the real model in production.
- That the scheduled cleanup has run. Its timer is installed and active, but the first automatic execution (2026-10-25) is not yet verified.
- That backups are taken off-host, or that recovery has been rehearsed on the live host. The tooling exists; the operational setup is a documented open gate.
- That the service is monitored, or that a failure raises an alert.
- That ingress controls are verified in production. The rate-limit settings exist as Traefik labels and were exercised only on a throwaway proxy, not on the live one. The chat API itself has no per-user or per-address rate limit and no cap on concurrent model calls.
- That the timing difference in the email-request endpoint is closed. It is a known, documented gap to handle before public exposure.
- That an independent penetration test has been done.

## Project evolution

```text
Telegram Kitchen Bot -> Shared Application Core -> Persistent Web Application -> Technical Deployment -> Functional Alpha (pending)
```

| Step | What changed |
|---|---|
| **Telegram Kitchen Bot** | The original product: `bot.py`, OpenAI replies, conversation context in memory. |
| **Shared Application Core** | The conversation logic moved out of the bot into a transport-neutral core (`house_helper/core`); the bot became a thin adapter over it, and the Kitchen prompt and limits became shared. |
| **Persistent Web Application** | A database layer with migrations, a FastAPI service, Telegram and email sign-in, an authenticated chat API and a Russian browser UI, built on the same core. |
| **Technical Deployment** | Invitation-based enrollment of the first users, a Docker and Traefik deployment kit with backup and upgrade tooling, the retention command, and a technical instance at `house.elivcloud.org`. |
| **Functional Alpha** | Pending, see [Deployment status](#deployment-status). |

There was no ground-up rewrite: the bot is still `bot.py`, now delegating to the core that the web application also uses. The increments are tracked as stages and releases (`R1`, `R2`, `R3A`, `R4A`, `R4B`, `R4C`, and so on) in [`docs/STATUS.md`](docs/STATUS.md).

## Roadmap

Everything in this section is planned. None of it is implemented.

| Area | Direction |
|---|---|
| **Kitchen usability and household measures** | Smoother everyday use of the Kitchen chat, and support for household measures (spoons, cups and similar everyday units). |
| **Food preservation guidance** | Guidance on storing, freezing and preserving food. Today the assistant only answers short storage and freezing questions and avoids unverified canning advice, so this needs careful, sourced content. |
| **Equipment-aware cooking (KH-F04)** | **Planned feature.** Appliance onboarding, so that suggestions fit the equipment a household actually has. |
| **Family recipe digitization (KH-F05)** | **Planned feature.** Turning printed or handwritten family recipes into structured recipes, with a human review step before anything is saved. This builds on saved recipes, which do not exist yet. |
| **Other household domains** | A possible expansion beyond the kitchen. The documented direction ([`docs/DECISIONS.md`](docs/DECISIONS.md), KHD-15 to KHD-19) is one assistant identity, with the user choosing the domain explicitly. No code exists for any other domain. |

Also documented but not built: saved recipes, long-term preferences, voice input and output, optional error-monitoring notifications, and remembered-device sign-in (still only a proposal).

## Development

### Stack

- Python 3.10+
- [aiogram](https://docs.aiogram.dev/) 3.x for the Telegram bot and the official [openai](https://github.com/openai/openai-python) SDK
- [FastAPI](https://fastapi.tiangolo.com/) and [uvicorn](https://www.uvicorn.org/) for the web application
- [SQLAlchemy](https://www.sqlalchemy.org/) 2 and [Alembic](https://alembic.sqlalchemy.org/) for persistence
- [PyJWT](https://pyjwt.readthedocs.io/), [cryptography](https://cryptography.io/) and [httpx](https://www.python-httpx.org/) for Telegram sign-in
- [python-dotenv](https://github.com/theskumar/python-dotenv) for the bot's `.env`
- A front end of plain HTML, CSS and JavaScript, with no build step and no external scripts
- pytest, with Node.js for the browser-UI test scenarios
- Docker, Docker Compose and Traefik for deployment

### Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
```

The Telegram bot needs a token from [@BotFather](https://t.me/BotFather) and an OpenAI key:

```bash
cp .env.example .env               # Windows: copy .env.example .env
# set BOT_TOKEN and OPENAI_API_KEY in .env (never commit this file)
python bot.py
```

The web application needs no `BOT_TOKEN`. Create the database schema, then start it:

```bash
alembic upgrade head               # uses HOUSE_HELPER_DATABASE_URL, default: house_helper.db
python -m uvicorn house_helper.web.asgi:app --host 127.0.0.1 --port 8000
```

The web process does not read `.env` by itself; add `--env-file .env` to take settings such as `OPENAI_API_KEY` from it. With no sign-in method configured, the page at `http://127.0.0.1:8000/` says so. Setting up sign-in, creating the first user and enrolling users are described in the Russian [technical reference](#technical-reference-russian) below; the deployment procedure is in the English [runbook](deploy/RUNBOOK.md).

### Tests and CI

The tests make no real network calls: `tests/network_guard.py` blocks outgoing connections other than loopback during a run, and a test that tries fails. GitHub Actions ([`ci.yml`](.github/workflows/ci.yml)) runs the suite on Python 3.10 and 3.12 for every push and pull request to `main`. At commit `6370bf7` it passed with 5,888 tests on Python 3.12 and 5,887 on 3.10. A further 86 (3.12) and 87 (3.10) are skipped: they are opt-in suites that need a real PostgreSQL server, which CI does not provide, plus one test that needs Python 3.12.

### Working method

Work is split into staged increments. Each one is reviewed independently before it is committed, and a release is recorded only after CI is green. Product and architecture decisions are kept in a register with their status (accepted, deferred, not built). The documents below are working records, written increment by increment.

### Documentation

| Document | Content |
|---|---|
| [`docs/STATUS.md`](docs/STATUS.md) | Current deployment state, release ledger, evidence for each release, open gates before real users |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Decision register |
| [`docs/product/PRODUCT_BASELINE.md`](docs/product/PRODUCT_BASELINE.md) | Product baseline: users, scenarios, accepted requirements |
| [`docs/architecture/ARCHITECTURE_BASELINE.md`](docs/architecture/ARCHITECTURE_BASELINE.md) | Architecture baseline and security invariants |
| [`docs/README.md`](docs/README.md) | Index of the documents above and how they are kept up to date |
| [`deploy/RUNBOOK.md`](deploy/RUNBOOK.md) | Deployment, upgrade, backup and restore, scheduled cleanup (English) |

Some of these documents still carry the earlier name "Kitchen Helper" in their titles.

## Technical reference (Russian)

The sections below are the detailed developer and operator reference. They are in Russian and were written release by release. Notes marked as the state at a release are dated records; the current deployment state is in [Deployment status](#deployment-status) above.

| Topic | Section |
|---|---|
| Telegram bot: features, configuration, running, context | [Возможности](#возможности), [Как создать `.env`](#как-создать-env), [Запуск бота](#запуск-бота) |
| Web application, environment, deployment kit | [Запуск веб-приложения (основа)](#запуск-веб-приложения-основа) |
| Telegram sign-in (OIDC) | [Вход через Telegram (OIDC)](#вход-через-telegram-oidc) |
| Email sign-in | [Вход по ссылке из письма (email)](#вход-по-ссылке-из-письма-email) |
| Creating the first user | [Первый пользователь: `provision-email`](#первый-пользователь-provision-email-команда-оператора) |
| Enrolling users, Telegram invitations | [Начальные пользователи: приглашения Telegram](#начальные-пользователи-приглашения-telegram-команды-оператора), [Привязка в браузере](#привязка-в-браузере-страница-приглашения-r4c-auth-a3) |
| Web chat API | [Веб-чат (API)](#веб-чат-api) |
| Web UI | [Веб-интерфейс (кухня)](#веб-интерфейс-кухня) |
| Limits, privacy, tests, CI | [Ограничения текущей версии](#ограничения-текущей-версии), [Приватность и обработка данных](#приватность-и-обработка-данных), [Тесты](#тесты), [CI](#ci) |

---

## О проекте

House Helper — модульный бытовой помощник. Первый модуль, «Кухня» (Kitchen Helper), помогает с домашней готовкой: подсказывает, что приготовить из имеющихся продуктов, как приготовить названное блюдо, чем заменить продукт и как хранить еду. У модуля два интерфейса: Telegram-бот (контекст диалога хранится только в памяти процесса, базы данных у бота нет) и веб-приложение (вход, API веб-чата и страница на русском языке; диалоги хранятся в базе данных). Это pet-проект в активной разработке. Рецептов как отдельной сущности, избранного и настроек пока нет; других модулей (сад, дом и т. п.) нет.

Состояние развёртывания (технический экземпляр `https://house.elivcloud.org`; провайдеры входа и OpenAI для реальных пользователей не включены) описано в английском разделе «Deployment status» выше.

## Возможности

- `/start` — приветствие и предупреждение о том, что сообщения обрабатываются через OpenAI.
- Обработка текстовых сообщений: бот учитывает историю диалога пользователя в пределах ограниченного окна (см. «Ограничения» ниже) и предлагает варианты блюд.
- Очистка истории командой `/reset` или фразой «очистить контекст» (регистр не важен).
- Ограничение длины одного сообщения и длины хранимой истории диалога.
- Индикатор «печатает…» в Telegram, пока бот ждёт ответ от OpenAI.
- Устойчивая обработка ошибок OpenAI и Telegram (таймауты, rate limit, пустые ответы, сетевые сбои) без падения бота.
- Корректное завершение работы: HTTP-соединения бота и общего клиента OpenAI закрываются при остановке процесса.

## Стек

- Python 3.10+
- [aiogram](https://docs.aiogram.dev/) 3.x — Telegram Bot API
- [openai](https://github.com/openai/openai-python) — официальный SDK OpenAI
- [python-dotenv](https://github.com/theskumar/python-dotenv) — переменные окружения из `.env`
- [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) — веб-приложение (вход, API веб-чата и страница «House Helper»; о техническом развёртывании — английский раздел «Deployment status» выше)
- [PyJWT](https://pyjwt.readthedocs.io/) + [cryptography](https://cryptography.io/) + [httpx](https://www.python-httpx.org/) — вход через Telegram OIDC (проверка подписи ID-токена, обмен кода)

## Структура файлов

| Файл | Назначение |
|------|------------|
| `bot.py` | Telegram-адаптер и точка входа: команды, доставка ответов, индикатор «печатает…», запуск polling, корректное завершение работы; логика диалога вынесена в `ChatService` |
| `config.py` | Загрузка и валидация настроек из `.env` (единая точка проверки конфигурации) |
| `context_manager.py` | Контекст диалога в памяти (один контекст на `ConversationKey`; для Telegram — на отправителя), с ограничением длины истории, и единый на процесс реестр блокировок диалогов |
| `house_helper/core/identity.py` | `ConversationKey(channel, subject)` — идентификатор диалога; для Telegram это `("telegram", str(user_id))` |
| `house_helper/core/outcomes.py` | `OutcomeStatus` и `ChatOutcome` — результат обработки одного сообщения на уровне приложения (без привязки к Telegram) |
| `house_helper/core/messages_ru.py` | Каталог русских текстов для результатов обработки (`/start` и сообщение о сбое доставки остаются в `bot.py`) |
| `house_helper/core/backend.py` | Интерфейс AI-бэкенда `ChatBackend` и адаптер `OpenAIBackend` поверх `api_client.py` (ошибки провайдера → `BackendError`); позволяет подменять бэкенд в тестах |
| `house_helper/core/limits.py` | Общие числа бота и веб-чата без зависимостей: длина сообщения (2000), окно истории (20), ответ по умолчанию (1000 токенов) |
| `house_helper/core/kitchen_prompt.py` | Единый system prompt помощника по кухне для Telegram и веба |
| `house_helper/core/provider_settings.py` | Переменные провайдера (`OPENAI_*`, `TEMPERATURE`, `MAX_TOKENS`), значения по умолчанию и диапазоны — в одном месте для `config.py` и веб-процесса |
| `house_helper/core/openai_chat.py` | `StandaloneOpenAIBackend` — AI-бэкенд веб-чата с явными настройками (без `api_client`, `config`, `BOT_TOKEN`); отмечает ответ, оборванный по лимиту токенов |
| `house_helper/core/chat_service.py` | `ChatService` и `Turn` — обработка одного хода диалога без привязки к Telegram: `service.turn(key)` держит блокировку диалога на всё время хода (включая доставку ответа транспортом), внутри — проверка длины, запрос к бэкенду, откат при сбое или отмене, сохранение обмена |
| `house_helper/web/` | Веб-приложение FastAPI — отдельный процесс, не зависит от Telegram-бота: фабрика `create_app()`, точка входа ASGI `asgi.py`, `GET /healthz`, настройки веб-процесса, `WebDatabase` (вся работа с БД выполняется в рабочих потоках, а не в цикле событий); вход через Telegram OIDC: `auth.py` (маршруты `/auth/...` и зависимость `require_user`), `telegram_oidc.py` (PKCE, обмен кода, проверка ID-токена), `logsafety.py` (маскировка кода и state в access-логе); привязка Telegram по приглашению: `invite_auth.py` (маршруты `/auth/invite...`), `invite_pages.py` (страницы на русском с единственным встроенным скриптом, CSP по хэшу) |
| `house_helper/web/chat_api.py`, `turn_runner.py`, `diagnostics.py` | Веб-чат: маршруты `/api/...` и разбор запросов; исполнитель хода (запись → вызов модели вне транзакции → завершение или `failed`); каталог ошибок на русском, коды, идентификаторы корреляции |
| `house_helper/web/ui.py`, `house_helper/web/static/` | Страница кухни (раздел «Веб-интерфейс (кухня)»): `GET /` — оболочка HTML, которую делает сервер (какие способы входа настроены, предел длины сообщения), и файлы `/static/...`: `app.js` (отрисовка), `chat-logic.js` (логика без DOM: ключи, разбор ответов, восстановление), `app.css`, `icon.svg`. Без сборки, внешних скриптов и новых зависимостей |
| `house_helper/persistence/conversations.py`, `turns.py` | Диалоги и история (владение, чтение без записи, контекст модели); минимальный жизненный цикл хода: `begin_turn` / `complete_turn` / `fail_turn` с «арендой» и сравнением-и-заменой |
| `house_helper/persistence/authn.py` | Хранение входа: состояние OIDC-входа (только хэш `state`, одноразовое), завершение входа через allowlist, серверные сессии (только хэш токена), отзыв |
| `house_helper/admin/` | Команды оператора, запускаются вручную: `python -m house_helper.admin provision-email` (первый пользователь и его email, см. «Первый пользователь») и `enroll-user`, `reissue-invitation`, `revoke-invitation`, `enrollment-status` (начальные пользователи и их одноразовые приглашения Telegram, см. «Начальные пользователи») и `purge-expired` (удаляет истёкшие артефакты входа и историю старше 365 дней; см. `deploy/RUNBOOK.md`, раздел 14). Веб-приложение и бот этот пакет не импортируют |
| `house_helper/persistence/invitations.py` | Одноразовые приглашения Telegram: выдача (с отзывом прежних), отзыв, проверка секрета, «погашение» с записью проверенного `sub` в allowlist пользователя, на которого выписано приглашение. В базе только SHA-256 секрета. Приглашение принимает веб-часть (`web/invite_auth.py`, `web/auth.py`): она выпущена в коде (R4C-Auth A3, RELEASED) и входит в техническое развёртывание, где вход через Telegram не включён; на настоящем Telegram она не проверена |
| `api_client.py` | Ленивая инициализация клиента OpenAI, запросы, system prompt, логирование usage |
| `tests/` | Автоматические тесты (без реальных сетевых запросов к Telegram/OpenAI). `tests/js/` — сценарии на Node.js для страницы (поддельный сервер, мини-DOM, настоящий сервер на loopback) |
| `requirements.txt` / `requirements-dev.txt` | Зависимости проекта (версии закреплены точно) |
| `.env.example` | Шаблон переменных окружения (без секретов) |
| `.github/workflows/ci.yml` | GitHub Actions: установка зависимостей и запуск тестов на push/PR в `main` |

## Как создать `.env`

1. Скопируйте шаблон:

   ```bash
   copy .env.example .env
   ```

   (в Linux/macOS: `cp .env.example .env`)

2. Заполните обязательные поля:

   - `BOT_TOKEN` — токен от [@BotFather](https://t.me/BotFather)
   - `OPENAI_API_KEY` — ключ с [platform.openai.com](https://platform.openai.com/)

3. При необходимости измените остальные переменные:

   | Переменная | Обязательна | По умолчанию | Допустимый диапазон |
   |---|---|---|---|
   | `BOT_TOKEN` | да | — | непустая строка |
   | `OPENAI_API_KEY` | да | — | непустая строка |
   | `OPENAI_MODEL` | нет | `gpt-4.1-mini` | — |
   | `TEMPERATURE` | нет | `0.7` | 0.0–2.0 |
   | `MAX_TOKENS` | нет | `1000` | 1–4096 |
   | `OPENAI_TIMEOUT_SECONDS` | нет | `15` | 1–120 |
   | `OPENAI_MAX_RETRIES` | нет | `1` | 0–5 |

   `MAX_TOKENS` по умолчанию 1000 (раньше 600: полный рецепт по-русски обрывался на полуслове); ограничение остаётся настраиваемым. Если в вашем `.env` стоит `MAX_TOKENS=600`, он продолжает действовать — поправьте его сами.

   Отсутствующие обязательные переменные или числовые значения вне диапазона (или не являющиеся числом) приводят к понятной ошибке при старте бота — процесс не запускает polling и завершается с ненулевым кодом, ошибки печатаются в лог.

**Не коммитьте файл `.env` в репозиторий.**

## Установка зависимостей

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

Для запуска тестов дополнительно установите dev-зависимости: `pip install -r requirements-dev.txt`.

Версии прямых зависимостей в `requirements.txt` и `requirements-dev.txt` закреплены точно (`==`) и соответствуют версиям, на которых проверен тестовый набор. Остальные транзитивные зависимости версией не зафиксированы и разрешаются `pip` на момент установки — то есть речь идёт о стабильности версий прямых зависимостей, а не о полной побитовой воспроизводимости окружения. Исключение — `aiohttp`: обычно это транзитивная зависимость `aiogram`, но здесь она закреплена явно, чтобы гарантировать минимальную безопасную версию независимо от того, что разрешил бы `aiogram` сам по себе.

## Запуск бота

```bash
python bot.py
```

В логах появится сообщение о старте бота. Откройте своего бота в Telegram и отправьте `/start`.

## Запуск веб-приложения (основа)

Веб-приложение (FastAPI, пакет `house_helper/web/`) — отдельный процесс, не связанный с Telegram-ботом. Оно умеет две вещи: `GET /healthz` отвечает `{"status": "ok"}` (проверка «процесс жив»; базу данных она не трогает), и — если настроено — пускает пользователя через Telegram OIDC или по ссылке из письма (разделы ниже; способы входа включаются независимо друг от друга). Диалоги и сообщения доступны через API веб-чата (раздел «Веб-чат (API)»), а людям — через страницу «Кухонный помощник» на `/` (раздел «Веб-интерфейс (кухня)»). Рецептов как отдельной сущности и настроек в нём нет.

```bash
python -m uvicorn house_helper.web.asgi:app --host 127.0.0.1 --port 8000
```

- Для запуска не нужен `BOT_TOKEN` (веб-процессу он не нужен никогда), а `OPENAI_API_KEY` нужен только для ответов веб-чата (см. «Веб-чат (API)»); файл `.env` приложение само не читает (если нужно взять переменные из него: добавьте `--env-file .env` к команде `uvicorn`).
- База данных задаётся переменной `HOUSE_HELPER_DATABASE_URL` (по умолчанию — файл `house_helper.db` в рабочей папке). Для веб-процесса нужен файл SQLite или другая СУБД; SQLite в памяти (`sqlite://`) не поддерживается.
- Приложение **не создаёт и не меняет схему базы данных**. Миграции применяются отдельной командой: `alembic upgrade head`.
- Проверка: `curl http://127.0.0.1:8000/healthz`.
- **Развёртывание на VPS (Docker, Compose, Traefik)** — пакет `Dockerfile`, `deploy/compose.yaml`, шаблон переменных, инструмент `deploy/ops/dbtool.py` (блокировка каталога данных, проверка схемы, резервная копия, проверка копии, восстановление), сценарии `deploy/backup.sh` и `deploy/upgrade.sh` (обновление выполняется только после проверенной резервной копии) и пошаговая инструкция [`deploy/RUNBOOK.md`](deploy/RUNBOOK.md) (на английском). Комплект использован для технического развёртывания `https://house.elivcloud.org` (состояние — в английском разделе «Deployment status» выше): провайдеры входа и OpenAI для реальных пользователей там не включены; каждый шаг, меняющий что-либо вне репозитория, выполняется только по явному разрешению владельца. Очистка по сроку (R4C-D2, коммит `489ff30`): команда `purge-expired` и необязательные юниты systemd (`deploy/systemd/`, раздел 14 инструкции) выпущены в коде; таймер установлен и активен на техническом экземпляре, а первый автоматический запуск назначен на 2026-10-25 и ещё не проверен.

### Вход через Telegram (OIDC)

Вход необязателен. Без переменных ниже приложение запускается как раньше (учётные данные не нужны), `/healthz` работает, а маршруты Telegram-входа отвечают `503 {"detail": "login_unavailable"}`. Вход по email (раздел ниже) не требует ничего из Telegram, а Telegram — ничего из SMTP.

Маршруты (других нет):

| Маршрут | Назначение |
|---|---|
| `GET /auth/telegram/login` | начать вход: перенаправляет в Telegram (Authorization Code + PKCE S256, `state`, `nonce`) |
| `GET /auth/telegram/callback` | вернуться из Telegram: проверка, допуск по allowlist, открытие сессии, перенаправление (`303`) на `/` — страницу приложения (она сама спрашивает `/auth/me`, кто вошёл; раньше назначением был `/auth/me`); в адресе перенаправления нет ни `code`, ни `state`, ни токена |
| `GET /auth/me` | `{"user_id", "session_expires_at"}` текущей сессии; `401`, если сессии нет |
| `POST /auth/logout` | отозвать сессию из cookie **этого запроса**, если она принадлежит пользователю из заголовка `X-Expected-User` (`204` и удаление cookie); чужая сессия — `409 {"detail": "session_mismatch"}`, заголовок отсутствует или не UUID — `400 {"detail": "invalid_request"}`: в обоих случаях ничего не отозвано и cookie не трогается (подробнее — ниже) |

Привязка Telegram-входа к пользователю по приглашению (маршруты `/auth/invite...`, тот же адрес возврата) описана в разделе «Начальные пользователи: приглашения Telegram».

Настройка:

1. В [@BotFather](https://t.me/BotFather) → Login Widget добавьте в **Allowed URLs** адрес возврата `https://<ваш-домен>/auth/telegram/callback` и возьмите Client ID и Client Secret. Алгоритм подписи ID-токена там же (по умолчанию RS256).
2. Задайте переменные окружения (`HOUSE_HELPER_PUBLIC_URL`, Client ID и Client Secret обязательны вместе, если задана хотя бы одна из переменных Telegram; иначе процесс не запустится, а в сообщении будут только имена переменных. Один `HOUSE_HELPER_PUBLIC_URL` без остальных вход не включает: его делят оба способа входа):

   | Переменная | Значение |
   |---|---|
   | `HOUSE_HELPER_PUBLIC_URL` | адрес приложения для пользователей: `https://host[:port]`, без пути. Из него строятся адрес возврата, допустимый `Origin` для выхода и атрибуты cookie; значения из запроса (`Host`, `X-Forwarded-*`) не используются |
   | `HOUSE_HELPER_TELEGRAM_CLIENT_ID` | Client ID |
   | `HOUSE_HELPER_TELEGRAM_CLIENT_SECRET` | Client Secret (не попадает ни в логи, ни в `repr` настроек) |
   | `HOUSE_HELPER_TELEGRAM_ID_TOKEN_ALG` | необязательно: `RS256` (по умолчанию), `ES256` или `EdDSA` — ровно тот, что выбран в @BotFather |

3. Разработка на своём компьютере: `HOUSE_HELPER_PUBLIC_URL=http://localhost:8000` (простой `http` допускается только для `localhost`/`127.0.0.1`/`::1`; cookie тогда без `Secure` и без префикса `__Host-`, чтобы браузер их сохранил). Для любого другого хоста нужен `https`, отдельного переключателя «небезопасных cookie» нет.

Как это устроено и что важно знать:

- **Допуск только по allowlist**, саморегистрации нет. Пользователь определяется по `sub` из проверенного ID-токена (не по username и не по имени); неизвестный, отозванный или отключённый — отказ `403`.
- **Пользователей заводит только оператор; публичной регистрации нет.** Первого пользователя вместе с его email создаёт команда `provision-email` (раздел «Первый пользователь» ниже); начальных пользователей беты, владельца и его маму, — **двоих** — команда `enroll-user` вместе с приглашением Telegram (раздел «Начальные пользователи: приглашения Telegram»; третьего пользователя она не создаёт). Прежний порядок «сначала Telegram» (создать `User` и записать для него `sub` кодом) **выведен из эксплуатации**: запись allowlist `telegram` вручную **не создаётся**, `sub` нельзя ни вводить руками, ни угадывать. Telegram к уже существующему пользователю привязывается по проверенному ID-токену Telegram и приглашению: веб-часть (страница `/auth/invite` и привязка при входе) **выпущена в коде (R4C-Auth A3, RELEASED) и входит в техническое развёртывание, где вход через Telegram не включён**, а **настоящий вход через Telegram не проверялся ни разу**. Маршруты входа через Telegram работают для тех, у кого запись `telegram` уже есть.

- **Сессия** — случайный токен (256 бит) в cookie `__Host-hh_session` (`HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, без `Domain`), срок 14 дней, не продлевается. В базе хранится только SHA-256 токена. Сессия перестаёт действовать при выходе, по истечении срока, при отключении пользователя и пока запись allowlist **того способа входа, через который она открыта**, отсутствует или отозвана (проверяется на каждый запрос). Другие способы входа того же пользователя такую сессию не поддерживают.
- **Выход называет, чей он (`X-Expected-User`).** Страница, которая попросила выйти и не получила ответа, спрашивает снова, а к тому времени единственная cookie браузера может оказаться чужой (в другой вкладке того же браузера вошёл другой человек). Поэтому `POST /auth/logout` несёт заголовок `X-Expected-User: <user_id>` — тот `user_id`, который `GET /auth/me` назвал странице, пока она была открыта; он отправляется с первой попыткой и с каждой повторной. Сервер берёт сессию из cookie **этого** запроса и отзывает её, **только если она принадлежит названному пользователю**: условие `user_id` входит в сам `UPDATE`, так что сессия другого человека этим запросом не затрагивается ни при каких обстоятельствах. Заголовок — только сравнение, а не учётные данные: он ничего не выбирает, не аутентифицирует и не даёт прав (запрос без cookie или с чужой cookie ничего не отзовёт, чьё бы имя в нём ни стояло); `user_id` в теле, строке запроса или в любом другом заголовке по-прежнему не читается.
  - `204` и удаление cookie — сессия отозвана этим запросом, или её уже нет: cookie нет, она не похожа на нашу, сессия неизвестна либо уже отозвана (повтор после потерянного ответа; первое время отзыва сохраняется). Запрос идемпотентен.
  - `409 {"detail": "session_mismatch"}` — в cookie сессия **другого** пользователя (живая, отозванная или истёкшая). Ничего не отозвано, `Set-Cookie` нет (чужая cookie не удаляется). Ответ не называет ни одного пользователя.
  - `400 {"detail": "invalid_request"}` — заголовка нет, он повторён или это не UUID в обычной записи (`8-4-4-4-12`, регистр не важен; фигурные скобки, `urn:`, без дефисов, список через запятую — отказ). Ничего не отозвано, cookie не удаляется.
  - `403 forbidden_origin` — как раньше, проверяется первой. `503`, если базу нельзя спросить, — ни отзыва, ни `Set-Cookie`: при неясности сессия «на всякий случай» не отзывается.
  - Из `curl`: `curl -X POST -H "X-Expected-User: <user_id из /auth/me>" -b <cookie> https://…/auth/logout`.
- **Привязка сессии к способу входа и отзыв (R3B-1).** Сессия запоминает `IdentityLink`, через который она открыта (`sessions.identity_link_id`); база не даёт указать связь другого пользователя (составной внешний ключ), а сессия без привязки допустима только отозванной.
  - Отозвать способ входа — одна функция, одна транзакция: она отзывает запись allowlist и все сессии, открытые через этот способ входа; сессии других способов входа не затрагиваются. Повторный вызов безопасен.

    ```python
    from house_helper.persistence.allowlist import revoke
    from house_helper.persistence.database import create_db_engine, create_session_factory

    engine = create_db_engine()  # HOUSE_HELPER_DATABASE_URL
    with create_session_factory(engine)() as session, session.begin():
        revoke(session, "telegram", "<sub из ID-токена Telegram>")
    ```

  - Повторное включение (`allow` снимает отзыв с той же записи) **не оживляет** отозванные сессии: человек входит заново и получает новую сессию.
  - Только `revoke` гарантирует эти побочные эффекты. Прямое изменение `allowlist_entries` через SQL не поддерживается: триггеров в базе нет, сессии такой запрос не отзывает. Пока отзыв записи действует, сессия по ней не проходит проверку, но если потом снять отзыв прямым SQL, сессия, открытая через этот способ входа, снова начнёт действовать.
  - Способов входа два: Telegram и email (раздел ниже). Сессия, открытая одним из них, не зависит от записи allowlist другого.
  - **Обновление с R3A.** Сначала остановите приложение, затем выполните `alembic upgrade head` (приложение само схему не меняет), затем запустите новую версию. Миграция `0002_session_provenance` отзывает **все** сессии, существовавшие на момент миграции: у них нет надёжных сведений, через какой способ входа они открыты, а угадывать это нельзя. Все, кто был в системе, один раз входят заново; пользователи, записи allowlist, связи, диалоги и сообщения не меняются. Откат (`alembic downgrade 0001_persistence_foundation`) отозванные сессии не возвращает.
- Вход привязан к браузеру: при старте ставится короткоживущая cookie `__Host-hh_login` со значением `state`; обратный вызов без неё отклоняется. `state` одноразовый и живёт 10 минут; повтор того же адреса возврата отклоняется.
- **Логи.** Секреты (`state`, `nonce`, PKCE-верификатор, код, токены, секрет клиента, значения cookie) не пишутся. Адрес возврата содержит `code` и `state`, а строку запроса пишет сам uvicorn: приложение при старте маскирует query для путей `/auth/` в логгере `uvicorn.access` (или запускайте с `--no-access-log`). Не включайте логирование SQL (`echo`, DEBUG для `sqlalchemy.engine`): параметры запросов содержат `nonce` и верификатор.
- Ограничение частоты запросов к `/auth/telegram/login` (и к маршрутам входа по email) приложение не делает: ставьте его на обратном прокси.

### Вход по ссылке из письма (email)

Второй способ входа, без пароля. Он нужен как запасной: человек запрашивает ссылку, получает письмо, открывает ссылку и подтверждает вход. Это **тот же пользователь**, что и при входе через Telegram, если администратор заранее указал оба способа входа для него. Сама почта ничего не объединяет и не создаёт: владение ящиком не доказывает, что адрес принадлежит какому-то из пользователей системы.

Вход по email необязателен и не зависит от Telegram: без переменных SMTP приложение запускается, `/healthz` и вход через Telegram работают, а маршруты email отвечают `503 {"detail": "login_unavailable"}`.

| Маршрут | Назначение |
|---|---|
| `POST /auth/email/request` | запросить ссылку: JSON `{"email": "..."}`. Любой правильно оформленный запрос получает один и тот же ответ `202` |
| `GET /auth/email/confirm?c=<id>` | страница, которую открывает ссылка из письма: показывает адрес в скрытом виде (`e***@example.com`) и кнопку «Войти». **Ссылку не расходует** |
| `POST /auth/email/confirm` | подтверждение (форма этой страницы): расходует ссылку, открывает сессию, `303` на `/` (страницу приложения) и cookie сессии |

Настройка (все переменные читаются из окружения; значения не попадают ни в логи, ни в `repr`, ни в сообщения об ошибках; если задана хотя бы одна переменная ниже, обязательные должны быть заданы все, иначе процесс не запустится):

| Переменная | Значение |
|---|---|
| `HOUSE_HELPER_PUBLIC_URL` | обязательна, общая с Telegram-входом; из неё строится ссылка в письме |
| `HOUSE_HELPER_SMTP_HOST` | обязательна: почтовый сервер |
| `HOUSE_HELPER_EMAIL_FROM` | обязательна: адрес отправителя, домен строчными латинскими буквами, например `login@example.com` |
| `HOUSE_HELPER_SMTP_SECURITY` | `starttls` (по умолчанию, порт 587) или `ssl` (TLS с первого байта, порт 465). Варианта «без шифрования» **нет** |
| `HOUSE_HELPER_SMTP_PORT` | необязательно: порт, если не стандартный |
| `HOUSE_HELPER_SMTP_USERNAME`, `HOUSE_HELPER_SMTP_PASSWORD` | необязательно, только парой: вход на почтовом сервере (выполняется только после включения TLS) |

Соединение всегда защищено TLS: сертификат и имя сервера проверяются по системным корневым сертификатам, TLS не старше 1.2. STARTTLS обязателен, а не желателен: если сервер его не предлагает или рукопожатие не удалось, письмо не отправляется (возврата к открытому соединению нет). Письмо отправляет стандартная библиотека (`smtplib`), сторонних почтовых SDK нет; в логи попадает только класс ошибки.

Как дать ещё один адрес **существующему** пользователю (выполняет администратор; публичной регистрации и автоматической привязки нет). Первого пользователя с первым адресом создаёт `provision-email` (раздел «Первый пользователь»), а этот фрагмент только добавляет адрес тому же пользователю и **не создаёт `User`**; `.one()` останавливает его, если пользователей в базе не ровно один; в базе с двумя начальными пользователями (владелец и мама) фрагмент поэтому не подходит как есть: вместо `.one()` выберите нужного пользователя по `id` из `enrollment-status` (`session.get(User, UUID("..."))`). Фрагмент:

```python
from sqlalchemy import select

from house_helper.persistence.allowlist import allow
from house_helper.persistence.database import create_db_engine, create_session_factory
from house_helper.persistence.models import User

engine = create_db_engine()  # HOUSE_HELPER_DATABASE_URL
with create_session_factory(engine)() as session, session.begin():
    user = session.scalars(select(User)).one()  # единственный пользователь
    allow(session, user, "email", "elena@example.com")
```

Адрес сохраняется в нормальной форме: пробелы по краям убираются, **домен** приводится к строчным (домен от регистра не зависит), а **локальная часть до `@` сохраняется ровно так, как её указал администратор**. По RFC 5321 регистр локальной части решает принимающий почтовый сервер, поэтому `Elena@example.com` и `elena@example.com` могут быть двумя разными ящиками: это два разных адреса в allowlist (у каждого может быть свой пользователь), и разрешение одного никогда не разрешает другой. Письмо уходит ровно на одобренный адрес, с тем же регистром в конверте SMTP и в заголовке `To`; запрос на другой регистр локальной части получает тот же ответ `202`, что и любой неизвестный адрес, и письма не вызывает. Больше ничего не склеивается: точки и `+тег` не убираются, псевдонимы почтового провайдера не учитываются (`cook+tag@example.com` и `cook@example.com` — разные адреса), регистр не «складывается» по правилам Unicode. Допускается только ASCII; адрес с кавычкой, запятой, угловой скобкой и т. п. может лежать в allowlist, но письма на него не отправляются. Отозвать адрес — `revoke(session, "email", "Elena@example.com")` (с тем же регистром локальной части, что при `allow`), как для Telegram.

Как это устроено и что важно знать:

- **Запрос.** Ответ не говорит, зарегистрирован ли адрес: одинаковые статус, тело и заголовки для разрешённого, неизвестного, отозванного, отключённого пользователя, псевдонима и повторного запроса. Отличается только некорректный запрос (`400`: это видно по самому тексту). Письмо отправляется **после** ответа (фоновая задача), чтобы время ответа не выдавало разрешённый адрес; сбой отправки даёт тот же `202` — причина пишется в лог только по имени класса, а ссылка, которую никто не получил, тут же отзывается. Письмо уходит только на адрес из allowlist, в том виде, как он там хранится. Новый пользователь не создаётся никогда.
- **Пауза между запросами:** не чаще одной ссылки в минуту на адрес (считается и после неудачной отправки, и после использованной ссылки). Одновременные запросы не обходят паузу. Тело запроса — только JSON не больше 2 КБ, и если браузер прислал `Origin`, он должен совпадать с `HOUSE_HELPER_PUBLIC_URL`.
- **Ссылка:** `https://<адрес>/auth/email/confirm?c=<id>#<секрет>`. Секрет — 256 случайных бит — стоит во **фрагменте** (после `#`): браузер его серверу не отправляет, поэтому он не попадает ни в журналы доступа, ни в `Referer`, ни в перенаправления. В базе хранится только SHA-256 секрета; `id` — не секрет, он нужен странице, чтобы назвать адрес. Ссылка действует 10 минут (на границе — «не действует»), срабатывает один раз и привязана к адресу и пользователю составным внешним ключом на запись allowlist.
- **Подтверждение — отдельный шаг.** Страница по `GET` ничего не расходует: почтовые сканеры открывают ссылки. Она самодостаточна (один встроенный скрипт и один стиль, никаких внешних ресурсов), отдаётся с `Content-Security-Policy`, разрешающим ровно их по хэшу (`connect-src` нет вовсе: скрипт ничего никуда отправить не может), `Cache-Control: no-store`, `X-Frame-Options: DENY` и `Referrer-Policy: same-origin`. Почему не `no-referrer`: по правилам Fetch при отправке формы с такой политикой браузер пишет `Origin: null`, и настоящий запрос нельзя было бы отличить от чужого (в настоящем браузере это не проверялось: тесты работают без браузера). Скрипт читает секрет из адресной строки, убирает его оттуда (`history.replaceState`) и кладёт в скрытое поле формы; запрос уходит только по нажатию кнопки. **Без JavaScript войти нельзя.**
- **Другое устройство.** Ссылку можно открыть и подтвердить на любом устройстве, не обязательно на том, где запрашивали: с браузером, запросившим ссылку, она не связана.
- **Браузер, уже вошедший как другой пользователь, не переключается:** ответ `409`, ссылка **не** расходуется; нужно выйти (кнопка «Выйти» на странице; для скрипта — `POST /auth/logout` с `X-Expected-User`, см. выше) и открыть ту же ссылку. Тот же пользователь получает новую сессию и новую cookie; прежняя сессия при этом не отзывается (как и при повторном входе через Telegram).
- **Сессия** обычная (14 дней, cookie `__Host-hh_session`, `HttpOnly`, `Secure`, `SameSite=Lax`) и запоминает `IdentityLink` с `provider = 'email'`. Отзыв адреса (`revoke`) завершает его сессии **и** отзывает ещё не использованные ссылки; сессии Telegram не затрагиваются, и наоборот. Повторное включение адреса (`allow`) не оживляет ни сессии, ни ссылки: нужна новая ссылка.
- Ссылка, предъявленная после того, как адрес перестал быть разрешённым (или пользователь отключён), расходуется и никого не впускает.
- **Отключение настройки** (убрали переменные SMTP) останавливает только новые входы по email; уже открытые сессии продолжают действовать до выхода, истечения срока или `revoke` адреса.
- **Логи.** Не пишутся адрес, ссылка, её секрет и `id`, значения cookie; пишутся исход запроса (`issued` / `not_eligible` / `cooldown`) и идентификатор пользователя при входе. Строка запроса `GET /auth/email/confirm?c=...` маскируется тем же фильтром `uvicorn.access`, что и обратный вызов Telegram. Не включайте отладку SMTP и логирование SQL.

Что нужно до публикации в интернете:

- **Ограничение частоты на обратном прокси** для `POST /auth/email/request` и `GET`/`POST /auth/email/confirm`: приложение ограничивает только число писем на один адрес. Заголовки `X-Forwarded-*` и `Forwarded` приложение не читает вообще, адрес клиента для ограничений определяет прокси.
- HTTPS (иначе cookie `__Host-` не сохранится), настоящий отправитель на своём домене с SPF/DKIM/DMARC, чтобы письма не уходили в спам.
- **Время ответа.** Ответ на `POST /auth/email/request` для разрешённого и неизвестного адреса совпадает по статусу, телу и заголовкам, но не по времени: у разрешённого адреса есть запись в базе, и это можно измерить. Выровнять время и поставить ограничение частоты нужно **вместе, до публикации** (сейчас не сделано).
- Миграции `0003_email_login_challenges` и `0004_email_identity_case`: остановить приложение, сделать резервную копию, `alembic upgrade head`, запустить. **Миграцию нужно выполнить до запуска нового кода**: приложение схему не проверяет и не меняет, а отзыв прежних email-адресов (ниже) происходит именно в миграции. `0003` только создаёт пустую таблицу `email_login_challenges`; откат (`alembic downgrade 0002_session_provenance`) удаляет её вместе с невостребованными ссылками. `0004` описана в следующем разделе.

### Обновление: регистр локальной части email (миграция 0004)

Раньше адрес сохранялся целиком строчными: одобрив `Victim@example.com`, администратор разрешал и `victim@example.com`, а это может быть чужой ящик. Теперь строчными приводится только домен. Что было на самом деле одобрено в уже существующих записях, база не знает, и угадывать это нельзя, поэтому `0004` действует по принципу «закрыто по умолчанию»:

- **Каждая существующая запись allowlist с `provider = 'email'` отзывается** (`revoked_at` = время миграции; уже отозванные не меняются и сохраняют своё время). Вместе с ней, так же как делает `revoke`, отзываются сессии, открытые через email-способ входа, и ещё не использованные ссылки из писем. Новых ссылок для этих адресов никто не получит, а выданные ранее больше не сработают.
- **Не затрагиваются:** записи, связи и сессии Telegram (никто, кто вошёл через Telegram, не выходит), пользователи, связи `IdentityLink`, диалоги, сообщения. Записи не переименовываются, не удаляются и **не переходят к другому пользователю**.
- **Повторное подтверждение делает администратор** (`.one()` в примере ниже — как в примере выше: при двух пользователях выберите нужного по `id`): командой `provision-email --reapprove-revoked --verified-mailbox` (раздел «Первый пользователь») или вызвав `allow` с адресом так, как он записан в действительности:

  ```python
  with create_session_factory(engine)() as session, session.begin():
      user = session.scalars(select(User)).one()  # единственный пользователь
      allow(session, user, "email", "Elena@example.com")  # ровно тот ящик, который имели в виду
  ```

  Если адрес совпадает с сохранённым побуквенно (в базе `elena@example.com`, подтверждён `elena@example.com`), отзыв снимается на той же записи. Если регистр другой, создаётся **новая** запись (это другой адрес), а прежняя строчная остаётся отозванной. Другому пользователю чужой адрес `allow` не отдаст. Ни в одном из случаев прежние сессии и ссылки **не оживают**: человек получает новую ссылку и новую сессию.
- Записи, ждущие подтверждения, видны в базе так (отличить их от отозванных вручную раньше нельзя): `SELECT subject, revoked_at FROM allowlist_entries WHERE provider = 'email' AND revoked_at IS NOT NULL;`
- **Схема.** Ограничение `email_subject_normalized` (на `allowlist_entries` и `identity_links`) требовало строчный адрес целиком; теперь оно требует ровно одну `@` с текстом по обе стороны и строчный домен. Новое ограничение принимает всё, что принимало прежнее, поэтому ни одна строка ему не противоречит. PostgreSQL: `DROP CONSTRAINT` и `ADD CONSTRAINT`. SQLite не умеет менять `CHECK` на месте, а перестройка таблиц здесь небезопасна: обе таблицы — «родители» внешних ключей с `ON DELETE CASCADE`, внутри транзакции миграции внешние ключи не отключить, и `DROP TABLE` удалил бы дочерние строки. Поэтому текст двух `CREATE TABLE` в `sqlite_master` правится на месте по процедуре из документации SQLite для ослабления ограничения (`writable_schema`, `schema_version`, `integrity_check`); если ожидаемый текст не найден или сборка SQLite запрещает такую правку, миграция останавливается и откатывается целиком.
- **Откат** (`alembic downgrade 0003_email_login_challenges`) возвращает прежнее ограничение, но не снимает отзывов, и отказывается выполняться, пока есть email-адрес с заглавной буквой в локальной части: прежнее ограничение хранить такой не может. Сначала удалите такие записи (и то, что от них зависит).

### Первый пользователь: `provision-email` (команда оператора)

**Это рекомендуемый и единственный описанный здесь порядок создания первого пользователя (email-first).** Публичной регистрации нет, поэтому первого пользователя и его email-адрес входа создаёт оператор одной командой. Это отдельная утилита (`house_helper/admin/`): веб-приложение и бот её не импортируют и не запускают. Она создаёт **одного** пользователя вместе с одобренной записью allowlist (`provider = 'email'`) в **одной транзакции**: либо есть и пользователь, и запись, либо нет ничего.

> **Не создавайте другого `User` кодом и не пишите его SQL-ом** — ни `User(...)` + `allow(...)`, в том числе по образцу прежней инструкции для Telegram, которая выведена из эксплуатации, ни `INSERT`. Второй пользователь получил бы собственные записи входа и не был бы тем же человеком. Сама эта команда второго пользователя не создаёт: в базе, где пользователь уже есть, она откажет (`user_exists`, `multiple_users`). Начальных пользователей (владельца и его маму) оператор заводит командой `enroll-user` (раздел «Начальные пользователи» ниже): она создаёт пользователя и приглашение одной транзакцией и не даёт создать больше двух. Новые способы входа получает **тот же** пользователь: ещё один email — фрагментом `allow` из раздела про вход по email, Telegram — приглашением (ниже).

Чего команда **не делает**: не создаёт Telegram-вход, не создаёт второго пользователя, не добавляет адрес уже существующему пользователю, не включает отключённого пользователя, не создаёт базу и **не применяет миграции**.

**Telegram.** Записи allowlist `telegram` здесь нет, и ручного способа её завести эта инструкция не даёт намеренно: `sub` оператор не должен ни вводить руками, ни угадывать (он не выводится из имени, номера или username). Telegram к **уже существующему** пользователю привязывается по проверенному ID-токену Telegram (`sub` берётся из проверенного токена, а не вводится оператором): оператор выдаёт приглашение (`reissue-invitation`), человек открывает страницу приглашения (раздел «Начальные пользователи» ниже). Вход по email работает независимо.

**Подготовка базы** (делает оператор до запуска команды):

1. Остановите приложение и сделайте резервную копию базы (если она уже есть).
2. Задайте `HOUSE_HELPER_DATABASE_URL` на нужную базу и примените миграции: `alembic upgrade head`. Для SQLite файл появится при первой миграции; для PostgreSQL базу создаёт администратор заранее.
3. Убедитесь, что `alembic current` показывает `0006_oidc_state_invitation (head)`, и **дождитесь окончания миграции**: миграции и `provision-email` никогда не запускаются одновременно (ограничения ниже). Команды не работают с базой, схема которой не на `head`.

**Адрес PostgreSQL.** Форма для SQLAlchemy с драйвером psycopg 3 — с указанием драйвера через `+psycopg`:

```
postgresql+psycopg://USER:PASSWORD@HOST:PORT/DATABASE
```

`USER`, `PASSWORD`, `HOST`, `PORT` и `DATABASE` здесь — только обозначения: подставьте свои значения (спецсимволы пароля кодируются процентами, например `@` → `%40`) и не записывайте пароль в репозиторий. Форма `postgresql://...` без `+psycopg` выбирает драйвер psycopg2, которого в проекте нет (команда ответит `driver_missing`). Сам драйвер в `requirements.txt` не входит: `pip install "psycopg[binary]"`. Ту же переменную `HOUSE_HELPER_DATABASE_URL` читают `alembic`, приложения и эта команда.

**Запуск.** База выбирается **только** переменной `HOUSE_HELPER_DATABASE_URL` (аргумента с адресом базы нет: в нём пароль попал бы в историю оболочки и список процессов). Значения по умолчанию (файл `house_helper.db` рядом) команда **не** подставляет: нет переменной — отказ.

```bash
# Linux/macOS
export HOUSE_HELPER_DATABASE_URL="sqlite:////srv/kitchen/house_helper.db"
# или PostgreSQL (шаблон, см. выше): "postgresql+psycopg://USER:PASSWORD@HOST:PORT/DATABASE"
python -m house_helper.admin provision-email --email Elena@example.com --display-name "Елена" --note "первый пользователь"
```

```powershell
# Windows PowerShell
$env:HOUSE_HELPER_DATABASE_URL = "sqlite:///C:/data/house_helper.db"
python -m house_helper.admin provision-email --email Elena@example.com --display-name "Елена"
```

| Параметр | Назначение |
|---|---|
| `--email` | обязателен. Домен приводится к строчным, часть до `@` сохраняется **ровно** как введена (`Elena@example.com` и `elena@example.com` — разные адреса); точки и `+тег` не убираются. Адрес, на который письмо отправить нельзя (кавычки, запятая и т. п.), отклоняется |
| `--display-name` | имя нового пользователя; используется только при создании |
| `--note` | заметка в записи allowlist (кто попросил, зачем) |
| `--reapprove-revoked` и `--verified-mailbox` | только **вместе**: разрешить снять отзыв с отозванного адреса и подтвердить, что вы проверили, что ящик именно с таким написанием (включая заглавные буквы до `@`) должен входить |

Что делает команда (одна транзакция; отказ ничего не меняет):

| Состояние базы | Результат | Код |
|---|---|---|
| пользователей нет, адреса нет | создаёт пользователя и запись, `ok [created]` | 0 |
| адрес уже одобрен для включённого пользователя | ничего не меняет, `ok [unchanged]`; другие `--display-name` и `--note` **не** применяются (в сообщении сказано, что они без эффекта) | 0 |
| адрес отозван, оба флага повторного одобрения | снимает отзыв **на той же записи**, `ok [reapproved]`; пользователь не пересоздаётся, отозванные сессии и ссылки из писем **не оживают** (человек запрашивает новую ссылку); данный `--note` заменяет прежний | 0 |
| адрес отозван, флагов нет | `refused [email_revoked]` | 3 |
| пользователь отключён | `refused [user_disabled]` (команда пользователей не включает) | 3 |
| пользователь уже есть, а адрес другой (в том числе другой регистр до `@`) | `refused [user_exists]`; чтобы дать существующему пользователю ещё один адрес, используйте `allow` из раздела выше | 3 |
| в базе больше одного пользователя | `refused [multiple_users]` | 3 |
| адрес записан за пользователем, которого в базе нет, или не за единственным | `refused [identity_other_user]` | 3 |

Коды выхода: `0` успех; `1` непредвиденная ошибка (печатается только имя класса исключения); `2` неверная командная строка или значение (проверяется **до** обращения к базе); `3` отказ по состоянию базы; `4` базой нельзя пользоваться (переменная не задана, SQLite-файла нет или он пуст, база не существует или недоступна, неподдерживаемый тип, нет драйвера, ревизия схемы не `head`). Вывод — одна строка `<статус> [<код>]: <текст>`: `ok` в stdout, остальное в stderr. Команда безопасна при повторном запуске.

Безопасность и ограничения:

- Не печатаются адрес базы и пароль (даже в сообщениях об ошибках), текст ошибок драйвера и SQL с параметрами, трассировки и непроверенные введённые значения; `--display-name` и `--note` не должны содержать управляющих и невидимых символов.
- SQLite: транзакция начинается с записи (общая для слоя хранения договорённость), поэтому одновременные запуски выстраиваются в очередь. **Режим журнала SQLite команда не меняет**: база используется как есть, и при отказе (схема не на `head`, база не от этих миграций, отказ по состоянию базы), и при успехе. Общий для приложений `PRAGMA journal_mode=WAL` меняет сам файл базы при первом же подключении, поэтому здесь он не выполняется; база в режиме WAL (любая, которую открывали приложения или миграции) остаётся в WAL. Внешние ключи и остальные настройки соединения те же, что у слоя хранения.
- PostgreSQL: перед чтением берётся транзакционная advisory-блокировка с фиксированным ключом (снимается с концом транзакции, при коммите и откате), а транзакция идёт на уровне `READ COMMITTED` независимо от умолчания сервера или базы; два одновременных запуска с разным написанием адреса не создадут двух пользователей. **Это гарантия между запусками самой команды.** Любой иной код, создающий пользователей, эту блокировку не берёт.
- **Миграции и `provision-email` никогда не запускаются одновременно.** Это правило эксплуатации; код его не обеспечивает. Advisory-блокировка выстраивает в очередь только запуски команды: Alembic её не берёт, команда не ждёт миграцию, а миграция не ждёт команду. Проверка ревизии схемы (`head`) выполняется дважды: перед транзакцией и ещё раз внутри неё, после блокировки. Это сужает промежуток между проверкой и записью, но **не закрывает его**: на PostgreSQL изменения схемы из идущей миграции не видны до её коммита, поэтому проверка, прошедшая рядом с миграцией, ничего не гарантирует. Порядок: остановить приложения, выполнить `alembic upgrade head`, дождаться её завершения, только потом запускать `provision-email`. Общей блокировки для миграций и команды в этой версии нет — это известное ограничение, не защита.
- Тесты в CI работают только на SQLite. То, что относится к PostgreSQL (advisory-блокировка, уровень изоляции, откат и снятие блокировки, возврат соединения к умолчанию), проверяет необязательный набор `tests/test_admin_postgres.py`, который CI **не запускает** (ниже).
- Старые email-адреса, отозванные миграцией `0004`, подтверждаются так: `python -m house_helper.admin provision-email --email <адрес как есть> --reapprove-revoked --verified-mailbox`. Если написание другое, это другой адрес, а не повторное подтверждение: команда откажет (`user_exists`), и нужен `allow` (см. выше).

**Проверка на настоящем PostgreSQL (необязательна, в CI не запускается).** `tests/test_admin_postgres.py` пропускается (`skipped`), пока не задана переменная `HOUSE_HELPER_TEST_POSTGRES_URL`. Запускайте его только на одноразовом сервере **на этой же машине**: тесты создают и удаляют базы данных. Адрес указывает сервер и служебную базу (обычно `postgres`), к которой тесты подключаются, чтобы создать и удалить свои; данные служебной базы они не читают и не меняют, а `HOUSE_HELPER_DATABASE_URL` не читают совсем. Что защищает от запуска на чужом сервере и в чужой базе (подробно — в `tests/postgres_harness.py`):

- **Адрес проверяется до любого подключения.** Допустим только вид `postgresql+psycopg://USER[:PASSWORD]@HOST:PORT/DATABASE`, где `HOST` — `localhost`, `127.0.0.1` или `::1` (имя не разрешается: `localhost` понимается как `127.0.0.1`); порт и пользователь указаны явно; порт **не 5432** (там обычно живёт постоянная установка — одноразовый сервер пробросьте на другой порт); имя служебной базы состоит из латинских букв, цифр и `_`. **Параметры после `?` запрещены целиком:** драйвер даёт `?host=`, `?hostaddr=`, `?dbname=`, `?service=` и другим приоритет над хостом и базой из адреса, поэтому `localhost` в адресе ничего не доказывает. Пароль не обязателен (пустой пароль — то же, что его отсутствие).
- **Переменные окружения libpq, способные направить соединение в другое место** (`PGHOST`, `PGHOSTADDR`, `PGPORT`, `PGDATABASE`, `PGSERVICE`, `PGSERVICEFILE`, `PGSYSCONFDIR`, `PGTARGETSESSIONATTRS`, `PGLOADBALANCEHOSTS`), не должны быть заданы; иначе отказ до подключения (печатаются имена переменных, не значения).
- **Параметры подключения строятся заново из проверенных полей:** `host` и `hostaddr` задаются одним и тем же IP-адресом (явный параметр сильнее `PGHOSTADDR`), порт, пользователь и имя базы заданы явно, имя создаваемой базы генерируется.
- **Каждое новое соединение проверяется до первого рабочего оператора:** адрес и порт, к которым драйвер реально подключился (должен быть тот самый loopback-адрес), имя базы и, для тестовых баз, метка запуска (`COMMENT ON DATABASE` со случайным токеном этого запуска). При несовпадении соединение закрывается, и миграции и всё остальное на нём не выполняются. Те же проверки идут перед `CREATE DATABASE` на служебном соединении.
- **Для каждого теста создаётся новая база** `house_helper_test_<токен>_<номер>` — копия один раз смигрированной базы-шаблона этого запуска; `TRUNCATE` и иных сбросов данных нет. Удаляются (`DROP DATABASE ... WITH (FORCE)`) только базы, созданные этим запуском и всё ещё несущие его метку, в том числе после сбоя фикстуры или теста. Пользователю нужно право `CREATEDB`.

```bash
docker run -d --rm --name hh-test-pg -e POSTGRES_PASSWORD=PASSWORD -p 127.0.0.1:55432:5432 postgres:16-alpine
pip install "psycopg[binary]"
export HOUSE_HELPER_TEST_POSTGRES_URL="postgresql+psycopg://postgres:PASSWORD@127.0.0.1:55432/postgres"
pytest tests/test_admin_postgres.py -v -rs
```

(`USER:PASSWORD` и адрес — шаблон; в PowerShell: `$env:HOUSE_HELPER_TEST_POSTGRES_URL = "..."`.) Набор проверяет: уровень `READ COMMITTED` внутри транзакции команды при умолчании базы `REPEATABLE READ`; что три одновременных запуска с разным написанием адреса, выстроенные в очередь за блокировкой, создают ровно одного пользователя; откат и снятие блокировки после отказа и после сбоя (блокировка снимается концом транзакции, а не закрытием соединения); возврат соединения к умолчанию базы; полный запуск командной строки; а также сами меры защиты на настоящем драйвере: `PGHOSTADDR`, заданный уже после проверки, не уводит соединение набора (с контролем: без закрепления `hostaddr` соединение уходит по `PGHOSTADDR`), файл служб не меняет маршрут, база без метки запуска отклоняется. Сама защита (разбор адреса, переменные окружения, проверка соединения, очистка) проверяется **без сервера и без psycopg** в `tests/test_admin_postgres_harness.py`: это часть обычного набора и CI. Пропуск не есть проверка. Выполнялся вручную 18.10.2026 (повторная правка по аудиту R3B-3) на PostgreSQL 16.15 (`postgres:16-alpine` в Docker, Windows, `127.0.0.1:55433`) с psycopg 3.3.6: 9 passed, после прогона на сервере не осталось тестовых баз. Ограничения: loopback-адрес не доказывает, что сервер действительно локальный (проброс портов Docker, SSH-туннель, WSL) и одноразовый, поэтому набор отправляет только `CREATE`/`COMMENT`/`ALTER`/`DROP` баз со своими сгенерированными именами и один читающий запрос на соединение; тест командной строки открывает собственный движок по тому же закреплённому адресу (его соединения по одному не проверяются, база проверена при создании); `::1` проверен только без сервера. Другие версии сервера, psycopg2 и удалённые серверы не проверялись.

### Начальные пользователи: приглашения Telegram (команды оператора)

Начальные пользователи — владелец и его мама. У каждого своя запись `User` и свои, независимые диалоги. Telegram-вход каждый привязывает **сам**, одноразовым приглашением, которое выдаёт оператор; привязка происходит только после проверенного входа через Telegram (OIDC), а не по имени, email, username или числовому id.

> **Состояние этапа на момент выпуска (2026-10-21/22; текущее состояние развёртывания — в английском разделе «Deployment status» выше).** Обе части выпущены в коде (R4C-Auth A2 — `c20a26e`, A3 — `9159842`, RELEASED), но не развёрнуты. Готова операторская часть (таблица `identity_invitations`, миграция `0005_identity_invitations`, модуль `persistence/invitations.py` и четыре команды ниже) и веб-часть (страница приглашения и привязка при входе через Telegram, миграция `0006_oidc_state_invitation`, подраздел «Привязка в браузере» ниже). Веб-часть проверена **только** на подставном Telegram (`tests/auth_support.FakeTelegram`): реальный вход через Telegram не выполнялся, BotFather не настроен, развёртывания нет. Пользователь, созданный `enroll-user`, **нигде не может войти**, пока приглашение не принято: запись allowlist появляется только при приёме.

Команды (та же подготовка базы и тот же `HOUSE_HELPER_DATABASE_URL`, что у `provision-email`; база должна быть на `head`, команды её не создают и не мигрируют):

```bash
# оба пользователя заводятся заново (имена подставьте свои)
python -m house_helper.admin enroll-user --display-name "Елена"
python -m house_helper.admin enroll-user --display-name "Мама"

# где что: пользователи, наличие Telegram-входа (не сам sub) и состояние их приглашений
python -m house_helper.admin enrollment-status

# новое приглашение существующему пользователю без Telegram-входа (USER-ID берётся из enrollment-status);
# прежнее действующее приглашение этого пользователя отзывается в той же транзакции
python -m house_helper.admin reissue-invitation --user-id USER-ID

# отозвать приглашение пользователя (повтор безопасен)
python -m house_helper.admin revoke-invitation --user-id USER-ID
```

| Команда | Что делает |
|---|---|
| `enroll-user --display-name ИМЯ` | создаёт **нового** пользователя и его приглашение одной транзакцией. Имя обязательно (по нему вы различаете пользователей) и проверяется так же, как у `provision-email`. Пользователь с тем же именем (без учёта регистра) — отказ `display_name_in_use`: повторная команда не должна занять второе место дублем. Третий пользователь — отказ `enrollment_full` |
| `reissue-invitation --user-id` | новое приглашение **существующему** пользователю; прежнее действующее отзывается. Годится и для первого приглашения пользователю, у которого их не было (например, созданному `provision-email`: email-вход остаётся, Telegram добавляется рядом). Отказ: `unknown_user`, `user_disabled`, `telegram_already_provisioned` (у пользователя уже есть Telegram-вход, в том числе отозванный: приглашение не способ обойти отзыв) |
| `revoke-invitation --user-id` | отзывает действующее приглашение: `ok [revoked]`, или `ok [unchanged]`, если действующего нет. Использованное и истёкшее не трогает (граница исключительная: в момент `expires_at` приглашение уже истекло). Истёкшее неиспользованное остаётся в базе как есть, пока его не заменит `reissue-invitation` |
| `enrollment-status` | только чтение. Для каждого пользователя: включён ли, есть ли Telegram-вход (`linked` / `revoked` / `not linked`, **без значения `sub`**), состояние текущего приглашения (`active`, `used`, `revoked`, `expired`, `none`) и время. Текущее — действующее, если оно есть; иначе то, чьё последнее событие (использовано, отозвано, истекло) позже всех; `created_at` строк не учитывается. Не показывает `sub`, секреты, их хэши и email-адреса |

Код выхода и формат вывода те же, что у `provision-email` (`0`, `1`, `2`, `3`, `4`; `ok` в stdout, остальное в stderr). **Приглашение — это секрет**, и он печатается **один раз**, отдельной строкой stdout, после коммита:

```
ok [enrolled]: created user USER-ID and issued a Telegram invitation, valid until 2026-10-19T14:00:00Z (2 hours, single use). ...
invitation code: INVITATION-ID.SECRET
```

Код `ИДЕНТИФИКАТОР.СЕКРЕТ` передаётся тому человеку, для которого он выписан, и больше никому, в виде ссылки: `https://<адрес приложения>/auth/invite#ИДЕНТИФИКАТОР.СЕКРЕТ` (код стоит **после `#`**; команда по-прежнему печатает только код, ссылку оператор собирает сам: адрес приложения — это `HOUSE_HELPER_PUBLIC_URL`). В базе остаётся только SHA-256 секрета; потерянный код восстановить нельзя — выдайте новый (`reissue-invitation`). Не запускайте `enroll-user` и `reissue-invitation` там, где вывод сохраняется в журнал.

Что гарантируется (и проверяется тестами):

- **Срок и единственность.** Приглашение действует 2 часа (граница исключительная: в момент `expires_at` оно уже недействительно) и принимается один раз: из любого числа одновременных попыток успешна ровно одна. Отозванное, истёкшее, использованное или заменённое новым приглашение больше не работает никогда; БД не даёт одновременно «использовано» и «отозвано» и не допускает двух действующих приглашений одного пользователя (частичный уникальный индекс).
- **Только тому пользователю, на которого выписано.** Принятие записывает проверенный `sub` в allowlist **этого** пользователя. Если такой `sub` уже есть в allowlist (у любого пользователя, действующий или отозванный), принятие отказывает, **приглашение остаётся действительным** и ничего не усыновляется и не «оживает». Автоматического сопоставления по имени, email, username или числовому id нет.
- **Атомарность.** Создание пользователя и приглашения, замена приглашения, отзыв — по одной транзакции: сбой после записи откатывает всё, и секрет не печатается.
- **Параллельность.** Команды, создающие пользователей, берут ту же блокировку, что и `provision-email` (SQLite: транзакция начинается с записи; PostgreSQL: advisory-блокировка на уровне транзакции и `READ COMMITTED`): два одновременных `enroll-user` не создадут трёх пользователей, а `provision-email` и `enroll-user` — двух «первых». Замена приглашения и приём сериализуются блокировкой строки пользователя (`FOR NO KEY UPDATE`): либо приём успел первым, и замена откажет, пользователь уже привязан, либо замена, и приём не найдёт прежнее приглашение.

Ограничения:

- **Предел «два пользователя» — временная политика команды `enroll-user`, а не правило базы**: в схеме нет ограничения на число пользователей (оно сознательно не вводилось), его считает команда в той же транзакции (все строки `users`, отключённые тоже) и только при **создании**; `reissue-invitation` предел не блокирует. После того как создан второй пользователь, `provision-email` по-прежнему отказывает (`multiple_users`), в том числе `--reapprove-revoked`. Принадлежность диалогов не менялась: каждый диалог принадлежит пользователю сессии.
- Срок 2 часа — политика функции выдачи (единственная, кто пишет `expires_at`); база проверяет только `expires_at > created_at`.
- Команды не удаляют, не отключают и не включают пользователей, не заменяют и не отзывают Telegram-вход, не создают записей allowlist и не меняют миграциями ничего существующего. Миграция `0005` только создаёт пустую таблицу (откат `alembic downgrade 0004_email_identity_case` удаляет её вместе с приглашениями); как и все миграции, она не должна идти одновременно с этими командами. Миграция `0006` добавляет в `oidc_states` одну пустую колонку (ниже).
- `enrollment-status` — отчёт, а не блокировка: на PostgreSQL его запросы не образуют единого снимка.
- На настоящем PostgreSQL блокировки и миграцию проверяет необязательный набор `tests/test_admin_enrollment_postgres.py` (как `tests/test_admin_postgres.py`: нужна `HOUSE_HELPER_TEST_POSTGRES_URL`, иначе `skipped`; в CI не запускается).

### Привязка в браузере: страница приглашения (R4C-Auth A3)

> **Состояние на момент выпуска (2026-10-22; текущее состояние развёртывания — в английском разделе «Deployment status» выше).** **RELEASED — выпущено в коде** (коммит `9159842`, принято владельцем после независимой проверки), но **не развёрнуто в рабочей среде** (CODE RELEASED ≠ LIVE DEPLOYED). Проверено только тестами и на подставном Telegram. Миграция `0006` не применялась ни к одной настоящей базе. **Настоящий вход через Telegram не проверялся**, BotFather не настроен, `house.elivcloud.org` не развёрнут (DNS и TLS не настроены), настоящих пользователей и приглашений нет, проверки на настоящем телефоне и в разных браузерах не было. Вход по email сохранён без изменений, но его промышленное включение отложено; запоминание устройства пока не сделано.

Человек открывает ссылку `https://<адрес приложения>/auth/invite#ИДЕНТИФИКАТОР.СЕКРЕТ`, нажимает «Продолжить через Telegram», подтверждает свой аккаунт в Telegram и попадает в приложение под своим пользователем. Приглашение расходуется **только** после успешной проверки ID-токена Telegram и привязки; без Telegram-входа (`HOUSE_HELPER_PUBLIC_URL`, Client ID и Client Secret, см. «Вход через Telegram») маршруты ниже отвечают `503`.

| Маршрут | Назначение |
|---|---|
| `GET /auth/invite` | страница приглашения (по-русски, одна и та же для всех: приглашение она не знает). Ничего не расходует и не пишет в базу |
| `POST /auth/invite/start` | начать вход: тело `{"code": "ИДЕНТИФИКАТОР.СЕКРЕТ"}` (только JSON, до 512 байт). Проверяет секрет и сохраняет состояние входа вместе с id приглашения; `200 {"authorization_url": ...}` и cookie `hh_login` (та же, что у обычного входа). Любая причина отказа (код не тот, неизвестное, использованное, отозванное, заменённое, истёкшее приглашение, пользователь отключён или уже привязан) — один и тот же `400 invitation_invalid`; чужой `Origin` — `403`; браузер, уже вошедший **другим** пользователем, — `409 signed_in_as_another_user`, ничего не сохраняется |
| `GET /auth/telegram/callback` | тот же маршрут, что у обычного входа (в BotFather один адрес возврата). Если вход начат по приглашению, в одной транзакции: приглашение расходуется, проверенный `sub` попадает в allowlist **этого** пользователя, создаётся связь и сессия (14 дней, как всегда); `303` на `/auth/invite/done`. Сбой такого входа — страница на русском, а не JSON; у обычного входа ответы прежние (JSON), кроме одного: браузер, уже вошедший **другим** пользователем, получает `409 signed_in_as_another_user` (страница на русском), и его сессия не меняется |
| `GET /auth/invite/done` | «Готово: Telegram привязан» для браузера с живой сессией; для остальных — перенаправление на `/` |

Как это устроено и где что защищено:

- **Секрет не покидает фрагмент, кроме одного POST.** Часть после `#` браузер серверу не отправляет: ни в строке запроса, ни в логе, ни в `Referer`, ни через перенаправление. Но **до выполнения скрипта фрагмент виден в адресной строке** (пока страница загружается), и в любом случае он остаётся в самой ссылке: в сообщении, из которого её открыли, в буфере обмена и в истории мессенджера, поэтому ссылка — секрет, пока приглашение не использовано. Скрипт страницы (единственный, встроенный; CSP разрешает его по SHA-256 и разрешает ему обращаться только к этому же origin) сразу после загрузки убирает фрагмент из адресной строки и из записи истории (`history.replaceState`), держит код в переменной (не в `localStorage`, `sessionStorage`, cookie или странице) и ничего не отправляет, пока человек не нажмёт кнопку. **Если браузер не дал убрать фрагмент** (`replaceState` бросил исключение или фрагмент остался в адресе), страница закрывается: кнопка выключена, код из переменной стёрт, ничего не отправляется, в Telegram страница не переходит, человеку по-русски сказано, что страница не может безопасно обработать приглашение. Обработчики кнопки и `pageshow` в этом случае не создаются, поэтому восстановление страницы из кэша ничего не включает. Код уходит одним `POST` в теле JSON на этот же origin. Адрес Telegram, в который переходит страница, — тот же, что у обычного входа: в нём нет ничего из приглашения; скрипт идёт только по адресу `https://oauth.telegram.org/auth?...`.
- **Приглашение привязано к входу на сервере.** Идентификатор приглашения хранится в строке `oidc_states` (колонка `invitation_id`) того самого входа, который начат после проверки секрета; на возврате из Telegram он берётся только оттуда. Ни параметр запроса, ни cookie, ни тело его не задают (тест подставляет их и они игнорируются). Привязка к браузеру, PKCE, `nonce` и проверка подписи, `iss` и `aud` ID-токена не менялись.
- **Всё или ничего.** Расход приглашения, запись allowlist, связь и сессия — одна транзакция (`invitations.complete_invitation_login`), и после неё проверяется, что сессия принадлежит приглашённому пользователю. Любой сбой откатывает всё: приглашение остаётся действительным, в allowlist ничего нет, ни связи, ни сессии. `sub` берётся только из проверенного токена. Уже занятый, в том числе отозванный, `sub` не усыновляется и не «оживает».
- **Чужой пользователь в браузере.** Браузер с живой сессией **другого** пользователя не может начать вход по приглашению (`409`) и не может его завершить: проверка повторяется на возврате из Telegram внутри транзакции (приглашение остаётся нетронутым, cookie сессии не заменяется). Тот же пользователь (например, вошёл по email) может привязать Telegram: он получает новую сессию, прежняя не отзывается. **Обычный Telegram-вход (без приглашения) отвечает так же:** возврат из Telegram в браузере с живой сессией другого пользователя — `409 signed_in_as_another_user` (страница на русском, без имён и идентификаторов); сессия и cookie прежнего пользователя остаются как были, сессия другому не выдаётся, ничего не пишется (даже связь, которую создаёт первый вход). Сравнение выполняется в той же транзакции, которая выдаёт сессию (`authn.complete_login`, параметр `browser_session_token`), без промежутка между проверкой и выдачей. Тот же пользователь входит заново как обычно (новая сессия на 14 дней, прежняя не отзывается); сессия, которой уже нет (истекла, отозвана, доступ или пользователь отключены), входу не мешает.
- **Сообщения.** Все тексты на русском и фиксированные: в них нет `sub`, имён, идентификаторов пользователя или приглашения и причины отказа. Приглашение, которое нельзя использовать, получает одну формулировку независимо от причины. После сбоя входа спрашивается только то, что нужно: «откройте ссылку заново».
- **Следующий вход.** После привязки приглашение больше не нужно: с другого устройства человек входит обычной кнопкой «Войти через Telegram». Тот же код второй раз не работает.

Миграция `0006_oidc_state_invitation`: одна пустая колонка `oidc_states.invitation_id` (внешний ключ на `identity_invitations`, `ON DELETE CASCADE`) и индекс. Существующие строки не меняются (`NULL` — обычный вход), сессии не отзываются, вход, идущий в момент миграции, завершается как раньше. На SQLite таблица `oidc_states` перестраивается (на неё ничего не ссылается; в ней только входы последних десяти минут); на PostgreSQL — обычные `ALTER`. Откат (`alembic downgrade 0005_identity_invitations`) сначала удаляет входы, начатые по приглашению (они не должны стать обычными), затем убирает индекс, ключ и колонку; как и все миграции, выполняется при остановленном приложении и **не применялся ни к одной настоящей базе**.

Остаточные риски и ограничения:

- Реальный Telegram не проверялся: неизвестно, что `nonce` возвращается в ID-токене, и каков формат `sub` (при несовпадении вход закрывается с причиной `wrong_nonce` в логе). Живая проверка и её условия — ниже.
- Сервер видит только ту cookie сессии, которую браузер прислал с запросом возврата из Telegram. Если другая вкладка вошла другим пользователем уже **после** отправки этого запроса (пока шёл обмен кодом с Telegram), `409` невозможен: вход завершится, cookie браузера будет заменена, а сессия того пользователя в базе останется действующей и нетронутой. Окно — секунды; зафиксировано тестом.
- Сбои обычного входа остаются JSON (`{"detail": "not_allowed"}` и т. п.); страницей на русском отвечают входы, начатые по приглашению (включая устаревшую или чужую ссылку возврата, если состояние входа известно), и отказ обычного входа `409 signed_in_as_another_user`.
- Пока ссылку приглашения нужно собирать вручную (команда печатает только код). Если ссылка открыта во встроенном браузере мессенджера, а возврат из Telegram произойдёт в другом, вход не завершится (cookie другого браузера): откройте ссылку в обычном браузере.
- **Обновление, «Назад» и кэш «вперёд-назад».** После того как скрипт убрал фрагмент, в адресе страницы кода уже нет. Если страницу **обновить** или браузер **загрузит её заново** при возврате из Telegram кнопкой «Назад», она покажет «ссылка неполная или повреждена», кнопка останется выключенной: откройте ссылку-приглашение заново (пока оно действует и не использовано). Если же браузер **восстановит страницу из кэша «вперёд-назад» (bfcache)**, вместе с ней восстановится и код, оставшийся в памяти страницы: скрипт по событию `pageshow` включает кнопку («Вход через Telegram не завершён…»), и можно нажать её ещё раз; код при этом нигде не сохранён, кроме памяти страницы. Страница отдаётся с `Cache-Control: no-store`; восстанавливают ли её из bfcache конкретные браузеры, **не проверялось**. Закрытая страница (фрагмент не убрался) из кэша не оживает.
- Нет ограничения частоты запросов на `POST /auth/invite/start` (секрет 256 бит, а состояния сохраняются только для действующих приглашений), нет повторного запроса «забыл код»: код выдаёт оператор.

Условия живой проверки (не выполнялась; вне этого этапа; DNS, TLS и `alembic upgrade head` до `0006` на техническом экземпляре с 2026-10-24 уже выполнены, остальное — нет): `HOUSE_HELPER_PUBLIC_URL=https://house.elivcloud.org`, `HOUSE_HELPER_TELEGRAM_CLIENT_ID`, `HOUSE_HELPER_TELEGRAM_CLIENT_SECRET` (и `HOUSE_HELPER_TELEGRAM_ID_TOKEN_ALG`, если в BotFather выбран не RS256); в BotFather — Allowed URLs: origin `https://house.elivcloud.org` и адрес возврата `https://house.elivcloud.org/auth/telegram/callback`; DNS и TLS для `house.elivcloud.org`; `alembic upgrade head` (до `0006_oidc_state_invitation`) на остановленном приложении с резервной копией; затем `enroll-user`, ссылка `https://house.elivcloud.org/auth/invite#КОД` для каждого человека.

Тесты: `tests/test_persistence_invitation_login.py` (оба блока транзакций, откат, гонки), `tests/test_web_invite.py` (поток, оба пользователя, вход после привязки), `tests/test_web_invite_failures.py` (сбои, подмена состояния, чужой пользователь, откат, гонки), `tests/test_web_invite_secrecy.py` (секрет нигде не виден; Telegram не настроен), `tests/test_web_invite_pages.py` (страницы и скрипт; сам скрипт исполняется в Node, если он установлен; в том числе закрытая страница, когда фрагмент не удалось убрать), `tests/test_web_auth_cross_user.py` (обычный Telegram-вход в браузере другого пользователя: `409`, сессия не меняется, вкладки, гонки), `tests/test_persistence_authn.py` (параметр `browser_session_token`), миграция — в `tests/test_persistence_migrations.py`.

## Веб-чат (API)

Бэкенд первого веб-сценария: диалоги и их сообщения хранятся в базе данных, ответы даёт тот же помощник по кухне, что и в боте (один system prompt на оба канала: `house_helper/core/kitchen_prompt.py`). Страница, которая им пользуется, описана ниже («Веб-интерфейс (кухня)»). Личность пользователя берётся **только** из серверной сессии (cookie, см. «Вход через Telegram» и «Вход по ссылке из письма»): `user_id` из запроса не читается нигде.

| Маршрут | Назначение |
|---|---|
| `POST /api/conversations` | новый пустой диалог. Тело `{}`. `201` `{"id", "title": null, "created_at", "updated_at"}` (заголовок не задаётся: это копия сказанного пользователем, а у диалога нет срока хранения) |
| `GET /api/conversations?limit=` | диалоги пользователя, недавняя активность первой. `limit` 1–100 (по умолчанию 50). `{"conversations": [...], "has_more"}` |
| `GET /api/conversations/{id}/messages?limit=&before=` | история, старые сообщения первыми. `limit` 1–100 (50); `before` — id сообщения: страница старее него. `{"conversation_id", "messages": [{"id", "role", "text", "created_at", "status"}], "has_more", "next_before"}`. Чтение ничего не пишет и не меняет `updated_at` |
| `POST /api/conversations/{id}/messages` | отправить сообщение. Заголовок `Idempotency-Key` (1–128 печатных ASCII-символов, свой на каждое сообщение), тело `{"text": "..."}`. `201` `{"conversation_id", "user_message", "assistant_message", "truncated", "replayed": false}` |

- **Повтор.** Тот же ключ и тот же текст для уже отвеченного сообщения даёт `200`, `"replayed": true` и заголовок `Idempotent-Replayed: true`: ответ берётся из сохранённого, модель второй раз не вызывается. Тот же ключ с другим текстом — `409 idempotency_key_reused`: это конфликт с прежним запросом, а не его повтор (ответ на прежний не отдаётся); отправьте сообщение с новым ключом.
- **Чужой, несуществующий и некорректный id диалога** — один и тот же `404 conversation_not_found`.
- **Запросы.** Изменяющие запросы — только `application/json` (UTF-8) до 32 КиБ, без повторяющихся ключей; `Origin`, если браузер его прислал, должен быть адресом приложения (`HOUSE_HELPER_PUBLIC_URL`); CORS не включён и не включается. Текст сообщения: пробелы по краям обрезаются, от 1 до 2000 символов, управляющие символы (кроме табуляции и перевода строки) и одиночные суррогаты отклоняются (`400`, текст в ответе не повторяется).
- **Один ход за раз в диалоге.** Пока готовится ответ, другое сообщение в этом диалоге получает `409 conversation_busy` (повторяемо, `Retry-After`); тот же запрос — `409 turn_in_progress`. Пока ход держит «аренду», это единственный ход диалога. Когда аренда истекла (ход брошен: процесс упал, база не ответила вовремя), ход перестаёт занимать диалог: следующее сообщение под блокировкой диалога помечает его `failed` (`interrupted`) и принимается само, а запоздалый ответ брошенного хода отбрасывается и в историю и контекст не попадает. Тот же ключ повторяет брошенный ход, пока он последний в диалоге.
- **Сбой.** Сообщение пользователя остаётся в истории со статусом `failed` (в `messages` у него есть `idempotency_key` и `error_code`), в контекст модели позднее оно не попадает, а тот же ключ повторяет попытку. Повторить можно, пока оно последнее в диалоге; иначе `409 turn_superseded` — отправьте его заново как новое. Это о сбое хода, который сервер записал; **сам по себе ответ `5xx` такой записи не доказывает**: например, `503 service_unavailable` бывает и для хода, который уже завершён и сохранён (не дошло подтверждение фиксации). Поэтому клиент не считает `5xx` сбоем: он повторяет тем же ключом (сервер отдаёт сохранённый ответ, `200`) или читает историю.
- **Отключение клиента** (закрытая вкладка) ход не прерывает: ответ сохраняется, повтор с тем же ключом отдаёт его. Остановка приложения даёт ходам 10 секунд закончиться, затем прерывает оставшиеся и ещё до 10 секунд ждёт, пока они (и их транзакции в базе, которые нельзя остановить на лету) завершатся: прерванный ход помечается `failed` (`interrupted`), если база принимает запись в этот срок. Если нет (транзакция застряла дольше срока, база уже закрыта, процесс убит), запрос остаётся `processing`, пока не истечёт его аренда; тогда тот же ключ повторяет попытку, а другое сообщение диалога вытесняет брошенный ход. Потерянным может оказаться только ответ, который не успели сохранить; дважды ответ не сохраняется никогда.
- **Контекст модели** — не больше 20 сообщений вместе с текущим (целые обмены, начиная с вопроса), только завершённые обмены; истёкшие сообщения в него не входят. История в API этим числом не ограничена.
- **Ответ, оборванный по `MAX_TOKENS`,** приходит с `"truncated": true`, а в конце текста добавлена пометка «Напишите «продолжи»…».
- **Срок хранения 365 дней** записан в `expires_at` сообщений и запросов; истёкшие сообщения не читаются и не попадают в контекст. **Физическое удаление** выполняет команда оператора `python -m house_helper.admin purge-expired` (R4C-D2): она выпущена в коде (коммит `489ff30`, независимый аудит пройден); таймер systemd установлен и активен на сервере технического экземпляра, а **первый автоматический запуск назначен на 2026-10-25 и ещё не проверен**. Порядок работы, неточность по времени (удаление происходит при ближайшем успешном запуске, а не в миг истечения срока), оговорки о SQLite и о старых резервных копиях — в `deploy/RUNBOOK.md`, раздел 14. Проверенный первый запуск по расписанию остаётся условием перед реальными пользователями.
- Обращение к модели выполняется вне транзакций базы; ход ограничен 60 секундами, «аренда» хода — 90 секунд от момента, когда ход принят под блокировкой диалога (ожидание блокировки или соединения до этого в срок не входит). Сохранить ответ можно только пока аренда не истекла: запоздавший ответ отбрасывается (`409 turn_in_progress`, тот же ключ генерирует заново).

Ошибки одной формы `{"detail": <код>, "message": <по-русски>, "retryable": <bool>, "correlation_id": "hh-…"}` (плюс `retry_after_seconds`, `user_message_id`). Идентификатор делает сервер для каждого запроса (клиентский не читается) и пишет в `X-Correlation-ID`, в тело ошибки и в строку лога; в логе нет текста исключений, SQL-параметров, содержимого сообщений и cookie.

| Код | HTTP | Повторяемо |
|---|---|---|
| `not_authenticated` | 401 | нет |
| `forbidden_origin` | 403 | нет |
| `conversation_not_found` | 404 | нет |
| `invalid_request`, `unsupported_media_type` (415), `request_too_large` (413), `invalid_query`, `empty_message`, `invalid_message_text`, `message_too_long`, `idempotency_key_required`, `invalid_idempotency_key` | 400 | нет |
| `conversation_busy`, `turn_in_progress` | 409 | да (`Retry-After: 5`) |
| `turn_superseded`, `idempotency_key_unusable`, `idempotency_key_reused` | 409 | нет |
| `provider_rate_limited` | 503 | да (`Retry-After: 30`) |
| `provider_unreachable`, `provider_empty_response`, `provider_error` | 502 | да |
| `provider_timeout` | 504 | да |
| `interrupted`, `service_unavailable` | 503 | да |
| `chat_unavailable` | 503 | нет (помощник не настроен) |
| `internal_error` | 500 | да |

**Настройка помощника** — те же переменные, что у бота, кроме `BOT_TOKEN` (веб-процессу он не нужен): `OPENAI_API_KEY` (без него диалоги создаются и читаются, а на отправку сообщения приходит `503 chat_unavailable`), `OPENAI_MODEL`, `TEMPERATURE`, `MAX_TOKENS`, `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_RETRIES` (диапазоны — в таблице выше). Неверное значение при заданном ключе останавливает запуск; сообщение называет переменную, но не значение. Файл `.env` веб-процесс сам не читает (`uvicorn --env-file .env`). Клиент OpenAI создаётся при первом сообщении, не при запуске.

Что в API первой версии **не** сделано: заголовки диалогов, удаление и сброс диалога, страницы в списке диалогов, потоковая передача ответа, ограничение частоты и числа диалогов (ставится на обратном прокси), автоочистка по сроку (команда `purge-expired` появилась позже, в R4C-D2; таймер установлен, первый запуск по расписанию ещё не проверен), привязка Telegram, избранное, голос, n8n-уведомления, другие области (сад, дом).

## Веб-интерфейс (кухня)

Первая рабочая версия страницы для людей: русскоязычный адаптивный интерфейс «House Helper» (название продукта; кухня — его первый модуль) поверх входа (`/auth/...`) и API веб-чата (`/api/...`). Обычные HTML, CSS и JavaScript: без сборки (`npm`, React, Vite), без внешних скриптов, шрифтов и CDN, без новых зависимостей. Файлы — в `house_helper/web/static/`, оболочку страницы делает `house_helper/web/ui.py`.

Если вход не подключён (нет ни Telegram, ни почты, и сервер на `/auth/me` отвечает `503 {"detail": "login_unavailable"}`), страница показывает спокойный экран «Вход пока не подключен. Мы готовим тестовую версию House Helper.», а не ошибку. Любой другой ответ, который не удалось понять (другой 503, 5xx, обрыв связи, чужая страница), по-прежнему показывается как сбой с кнопкой «Повторить».

**Статус на момент выпуска R4B (2026-10-19): реализовано и проверено локально; публично не развёрнуто.** Позже появился технический экземпляр на `https://house.elivcloud.org` (английский раздел «Deployment status» выше): вход и OpenAI там не включены, страница сообщает, что вход не подключён. Всё, что описано ниже, проверялось на локальном стенде и тестами. Публичной бета-версии нет: настоящая почта (SMTP) и вход через Telegram, настоящий ключ OpenAI и оценка качества ответов, ограничения на входе, очистка по сроку хранения — отдельные этапы (см. «Не сделано в этой версии»).

| Маршрут | Назначение |
|---|---|
| `GET /` | оболочка страницы. Сервер кладёт в `<body>` атрибуты `data-telegram` и `data-email` (какие способы входа настроены сейчас; рисуется только настроенный) и `data-max-length` (предел длины сообщения, 2000). Секретов, адресов и имён поставщиков в ней нет, для всех она одна и та же |
| `GET /static/{app.js, chat-logic.js, app.css, icon.svg}` | файлы страницы; только этот список, с `ETag` и `Cache-Control: no-cache` |

Оба маршрута публичны: данные человека приходят только из `/auth/me` и `/api/...` с его собственной cookie. После успешного входа (Telegram или письмо) браузер попадает на `/` (раньше — на `/auth/me`).

**Как открыть локально.** Нужно то же, что для API: схема БД (`alembic upgrade head`), первый пользователь (`provision-email`), способ входа (SMTP для входа по письму и/или Telegram) и, чтобы помощник отвечал, `OPENAI_API_KEY`. `HOUSE_HELPER_PUBLIC_URL` должен совпадать с адресом в браузере буква в букву: `http://127.0.0.1:8000` и `http://localhost:8000` — разные адреса (от этого зависят cookie и проверка `Origin`; обычный `http` разрешён только для localhost). Затем:

```bash
python -m uvicorn house_helper.web.asgi:app --host 127.0.0.1 --port 8000 --env-file .env
```

и откройте этот адрес. Если ни один способ входа не настроен, страница так и скажет; если сервис недоступен — скажет, что он недоступен (а не покажет вход).

**Что делает страница.**

- **Вход.** При открытии спрашивает `GET /auth/me`. Определённое `401` — экран входа: «Войти через Telegram» (обычный переход на `/auth/telegram/login`) и/или поле адреса для письма (`POST /auth/email/request`; ответ нейтральный и одинаковый для любого адреса; регистр локальной части не меняется, обрезаются только пробелы по краям). Любой другой исход (нет сети, `5xx`, не JSON, тайм-аут) — «сервис недоступен» с кнопкой «Повторить», а не экран входа.
- **Разговоры.** Список недавних (до 50), «Новый разговор» (сам разговор создаётся первым сообщением, пустые не плодятся), выбор и продолжение, старые сообщения по кнопке. У разговора без заголовка подпись по дате: «Разговор от 18 октября, 14:30» (заголовков сервер не хранит, и страница их не придумывает и не сохраняет). На узком экране список — выезжающая панель.
- **Отправка.** Кнопка или Ctrl+Enter (Enter — новая строка). Текст обрезается так же, как на сервере (как Python `strip()`, а не `trim()` JavaScript); предел 2000 считается по кодовым точкам Unicode, а не по `.length`. У каждого сообщения свой криптографически случайный ключ идемпотентности (`web-` и 32 hex-символа); без `crypto.getRandomValues` ничего не отправляется. Пока ответ готовится (до минуты, запрос ждёт до 90 с), показано, что идёт отправка; повторное нажатие второй запрос не создаёт. **В существующий разговор отправить нельзя, пока не загрузилась последняя страница его истории**: если загрузка идёт или не удалась, поле ввода заблокировано с пояснением («Загружаем историю разговора…» / «Не удалось загрузить историю…» и кнопка «Повторить»), а разговор не показывается как пустой. Так «id последнего известного сообщения» (отсчёт для неясного исхода) всегда берётся из ответа сервера. Только что созданный разговор заведомо пуст (его только что сделал сервер), ему история не нужна.
- **Неясный исход** (обрыв связи, **любой ответ `5xx`** — и `502`/`504` прокси, и собственный ответ приложения, любой не-JSON ответ, ответ не в формате приложения, тайм-аут, перезагрузка страницы): страница **не считает, что сообщение не дошло**, и пока исход неизвестен, **ключ и текст нельзя отдать**: у такого сообщения одна кнопка — «Повторить» с **тем же ключом и тем же текстом** (сервер отдаст сохранённый ответ, попросит подождать или выполнит заново), а новое сообщение в этом разговоре отправить нельзя (поле ввода заблокировано, на странице объяснено почему; в других разговорах можно). Кнопки «Вернуть в поле ввода» и «Отправить как новое» у неясного сообщения нет, потому что новый ключ мог бы создать второй ход: чтение истории не доказывает, что сообщения **не** было (ответ может быть устаревшим, неполным или не прийти, а запрос — дойти позже). Историю страница читает (только чтение) и берёт из неё одно твёрдое свидетельство: сообщение с **этим ключом** (история отдаёт ключ только у незавершённых сообщений) — дальше им управляет история. Завершённый обмен с тем же текстом после отсчёта — только подсказка («Похоже, сообщение уже дошло…»), не разрешение: ключа у него нет, а то же слово мог отправить другой экран. Подтверждает его «Повторить» с тем же ключом: сервер отвечает `200` с сохранённым ответом, без второго выполнения. **`5xx` — не приговор ходу.** Код состояния — ответ на *этот запрос*, а не запись сервера о ходе: ход мог быть завершён и сохранён, а не дошло только подтверждение (потерянный ответ на фиксацию транзакции даёт `503 service_unavailable` для завершённого хода). Поэтому и `5xx` приложения оставляет сообщение неясным: те же единственная кнопка «Повторить» и тот же ключ, независимо от `retryable` и от кода (`provider_*`, `interrupted`, `chat_unavailable`, `service_unavailable`, `internal_error`). «Номер обращения: hh-…» из ответа сохраняется и показывается для разбора. Сбоем ход считается, только когда **история показывает сообщение с этим ключом и статусом `failed`** — это запись сервера; тогда сообщение передаётся истории (её «Повторить» с тем же ключом; номер обращения остаётся на сообщении), а поле ввода снова доступно. Если сервер при этом недоступен для чтения, сообщение остаётся неясным. Побочное следствие: если помощник не настроен (`chat_unavailable`) и ход не создавался, сообщение нельзя «вернуть в поле ввода» — его можно только повторять тем же ключом, когда помощник появится (записи старше 6 часов удаляются, о чём страница скажет). «Вернуть в поле ввода» и «Отправить как новое сообщение» появляются только когда сервер определённо отказал по причине самого запроса (`4xx` приложения) или сказал, что ключ использовать нельзя (`turn_superseded`, `idempotency_key_unusable`, `idempotency_key_reused`; тогда «Отправить как новое сообщение» — по клику человека). На `409` «занято» / «ещё готовится» страница ждёт столько, сколько сказал сервер (`Retry-After`), и повторяет с тем же ключом (до двух минут, потом просит человека).
- **Создание разговора** не идемпотентно, поэтому при неясном исходе страница его не повторяет: обновляет список, оставляет текст в поле и объясняет, что делать (повторное «Отправить» — решение человека).
- **Ошибки** — по-русски и без технических подробностей. Для ошибок `/api` показан «Номер обращения: hh-…» (его делает сервер); для `/auth/*` номера нет, и страница его не выдумывает. Потеря сессии (`401` где угодно) — экран входа с пояснением; всё, что было на странице, убирается из памяти.
- **Конец сессии.** Срок сессии (14 дней, абсолютный, без простоя — серверная политика не менялась) страница узнаёт из `session_expires_at` в `GET /auth/me` и хранит его только в памяти. Раз в секунду она сравнивает его с часами; когда вкладка возвращается (`visibilitychange`, `resume`, сеть) — сначала сверяет срок (так «проснувшаяся» после сна вкладка выходит сразу, ничего не спрашивая у сервера), затем спрашивает сервер, кто сейчас вошёл (следующий пункт; прервать сессию могли и раньше: выход в другой вкладке, отзыв). Когда срок вышел или пришёл определённый `401`, страница показывает экран входа и **убирает разговор целиком — из видимой и из скрытой частей страницы** (сообщения, список, заголовок, подсказки, поле ввода), а ответы, которые были в пути, игнорирует. Ответ без срока (`session_expires_at` нет или он не разбирается) страница не принимает за сессию (экран «сервис недоступен»), срок не придумывает; срок, который уже прошёл по часам, — это вход, а не сессия. Неподтверждённое сообщение остаётся в записи `sessionStorage` и после нового входа в этой вкладке возвращается как неясное — отправить его заново можно только тем же ключом (после чтения истории разговора). Сессия, открытая в другой вкладке (ссылка из письма), записи этой вкладки не видит: перед повторной отправкой того же текста проверьте историю.
- **Возврат на вкладку и другой пользователь.** Cookie сессии одна на весь браузер: пока вкладка была в стороне, в нём мог войти другой человек. Поэтому каждый возврат (вкладка снова видна, сеть вернулась, «проснулась» после сна) — граница приватности. Страница запоминает `user_id` из `GET /auth/me` (только в памяти) и **с первого мгновения вопроса** до ответа сервера показывает вместо разговоров «Проверяем вход…»: в видимой и скрытой частях страницы нет ни списка, ни сообщений, ни заголовка, ни подсказок, ни черновика (черновик хранится вне страницы, в переменной скрипта), а запросов за разговорами, отправок и повторов не делается — таймеры ожидания останавливаются. Затем ответ решает: **тот же пользователь** — всё возвращается как было (те же неразрешённые сообщения с теми же ключами, загруженные страницы истории, черновик; страница не начинается заново и не запрашивает список снова; остановленные ожидание и чтение истории продолжаются); **другой пользователь** — страница начинается заново, как при первом открытии: память, черновик и запись `sessionStorage` прежнего пользователя убираются, а ответы, которые были в пути для него, ничего не меняют (эпоха); `401` — конец сессии, как обычно; **ответа нет** (нет связи, `5xx`, не JSON, тайм-аут, ответ без срока) — страница остаётся скрытой, пишет «Не удалось проверить, кто сейчас вошёл в аккаунт, поэтому разговоры скрыты…» с кнопкой «Повторить» (вернувшаяся сеть спрашивает снова сама), ничего не утверждая о том, вошёл ли кто-то. Срок сессии по часам и при скрытой странице обрывает её. Цена: при каждом возврате на доли секунды вместо разговора видна «Проверяем вход…», а положение прокрутки сообщений не сохраняется (разговор открывается внизу); при потере связи вкладка остаётся скрытой, пока сервер не ответит. Полная перезагрузка (`pageshow` из кэша «назад/вперёд», кнопка «Повторить» на первом экране) начинается заново целиком.
- **Выход.** В тот момент, когда человек нажимает «Выйти», страница уходит от разговоров, не дожидаясь сервера: эпоха сдвигается (всё, что ещё в пути, игнорируется), память и видимая часть страницы очищаются, таймеры останавливаются, запись `sessionStorage` удаляется. Что покажет страница дальше, зависит от ответа: `2xx` (или `401` — сессии нет) — экран входа «Вы вышли из аккаунта.»; **любой другой исход, в том числе потерянный ответ** (сервер мог отозвать сессию и зафиксировать это, а ответ не дошёл), — экран «Не удалось подтвердить выход из аккаунта: сессия, возможно, ещё открыта. Разговоры скрыты…» с кнопкой «Выйти ещё раз» (запрос идемпотентен: если сессия уже отозвана, ответ `204`). Страница **не утверждает**, что сессия закрыта, пока сервер этого не подтвердил, и **не возвращает прежний экран** из-за потерянного ответа: поздний ответ на сообщение, отправленное перед выходом, ничего не восстанавливает. Каждая попытка выхода — первая и каждая повторная — несёт заголовок `X-Expected-User` с `user_id`, под которым страница была открыта в момент нажатия (страница запоминает его в переменной контроллера **до** очистки экрана и не записывает ни в `sessionStorage`, ни куда-либо ещё; повтор отправляет именно его, а не `user_id` того, кто вошёл позже). Если при повторе сервер отвечает `409 session_mismatch` (в cookie уже сессия другого человека), страница **прекращает** выход: никого не выходит из аккаунта, забывает запомненный `user_id`, по-прежнему не показывает ни одного разговора и пишет «Выход не выполнен: в этом браузере уже вошёл другой пользователь, и его вход не затронут. Прежние разговоры скрыты. Нажмите «Повторить», чтобы открыть страницу заново.» (ни кто вошёл, ни то, что прежняя сессия закрыта, в этом тексте не утверждается: этого не известно). Кнопка «Повторить» спрашивает `GET /auth/me`, кто вошёл, с самого начала: открывается страница нового пользователя, ничего от прежнего. Любой другой исход (`400`, `403`, `409` с другим кодом, `5xx`, потерянный ответ) остаётся «не подтверждён»: «Выйти ещё раз» с тем же `user_id`. Запомненный `user_id` забывается и при успешном выходе, и при любом новом начале страницы (`start`). Неразрешённое сообщение при явном выходе не сохраняется (так задумано; о нём нужно будет посмотреть историю).
- **Текст — только текст.** Всё, что написал человек, помощник или сервер, попадает на страницу как текстовый узел (`textContent`): нет `innerHTML`, `eval`, встроенных обработчиков и сторонних скриптов (это проверяют тесты). Абзацы разделяются пустой строкой, переводы строк внутри абзаца сохраняются.
- **Заголовки.** `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'` (без `unsafe-inline` и `unsafe-eval`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Cache-Control: no-store` для страницы. CORS не включён, проверка `Origin` изменяющих запросов не ослаблена. Страница не задаёт режим `fetch` (`mode`) и политику реферера: браузер сам отправляет настоящий `Origin`; гипотеза «`mode: "same-origin"` даёт `Origin: null`» в Chrome 155 не подтвердилась (в четырёх вариантах пришёл настоящий `Origin`), но в других браузерах не проверялась.
- **Что страница хранит в браузере.** Только `sessionStorage` вкладки и только пока сообщение не разрешилось: чей это ход (`user_id` из `GET /auth/me` — только чтобы запись не вернулась другому человеку; в запросах он не отправляется, кроме заголовка `X-Expected-User` выхода, куда он берётся из памяти страницы, а не из хранилища), идентификатор разговора, ключ, точный текст, id последнего известного сообщения и время (`hh.pending.v1`, до 8 записей). Запись удаляется, когда сообщение разрешилось, при «Вернуть в поле ввода» (оно есть только у сообщения, которое сервер отклонил по причине самого запроса, `4xx`; `5xx` его не открывает) и при выходе (сразу, не дожидаясь ответа сервера). Повреждённое или чужое значение под этим ключом, **запись другого пользователя** (она не показывается, не учитывается и о её возрасте не говорится; запись без `user_id` — ничья) и каждая запись старше 6 часов **удаляются при чтении**, а не просто пропускаются: текст сообщения не лежит дольше, чем он может пригодиться (об удалённой по возрасту записи страница говорит один раз: проверьте историю). Если браузер не позволяет сохранять (приватный режим, переполнение, запрет), страница работает, но у неподтверждённого сообщения написано, что после закрытия или перезагрузки страницы восстановить его не получится и историю надо посмотреть до повторной отправки: восстановление после перезагрузки не гарантируется. Cookie сессии (`HttpOnly`, скрипту недоступна), токены и истории переписки страница не хранит. Если сессия закончилась, запись остаётся до следующего входа в этой вкладке — вернётся она только тому же пользователю (а восстановленное сообщение показывается только после того, как загружена история его разговора: ею же подтверждается, что разговор принадлежит вошедшему).
- **Доступность.** Подписи у полей, видимый фокус с клавиатуры, кнопки не меньше 44×44 px, `aria-live` для статусов и ошибок, контраст текста не ниже 4,5:1 (палитра — токены `:root` в `app.css`, пары проверяет тест), `prefers-reduced-motion`.

**Не сделано в этой версии** (отдельные этапы): избранное и настройки, голос, другие области House Helper, заголовки и удаление разговоров, потоковый ответ, автоочистка по сроку (команда `purge-expired` появилась позже, в R4C-D2; таймер установлен, первый запуск по расписанию ещё не проверен), публичная бета-версия (технический экземпляр уже есть, см. «Deployment status»), ограничения на входе.

### Проверка в браузере (вручную)

Автоматические тесты браузер не заменяют. **Что проверено в настоящем браузере:** Chrome 155.0.8059.80 (headless, Windows), через протокол DevTools, на локальном стенде с поддельными помощником и почтой (стенд не входит в репозиторий): экран входа с нужными вариантами; вход и переход на `/`; сообщение и ответ; перезагрузка с восстановлением истории; `502` от «прокси» после обработки сервером (один пузырь, один вызов помощника); обрыв запроса, перезагрузка и «Повторить» с тем же ключом (одна обработка); медленный ответ; сбой помощника с номером обращения; потеря сессии; вход по письму с `Cook@example.com`; выход; выезжающая панель и `Escape`; 70 сообщений с подгрузкой старых без сдвига; настоящие нажатия Enter и Ctrl+Enter; текст `<img onerror>`/`<script>` показан как текст; после второго аудита (на том же стенде, с подставленными сбоями): ответ `503` приложения после того, как сервер уже сохранил ход (одна кнопка «Повторить», перезагрузка, тот же ключ, один вызов помощника, номер обращения на месте), возврат на вкладку (на время вопроса к серверу в странице нет ни разговора, ни черновика; тот же человек получает их обратно), второй человек в том же браузере (в первой вкладке ничего от первого: ни разговора, ни черновика, ни записи), выход с потерянным ответом и поздним ответом помощника («Выйти ещё раз», ничего не вернулось). **Не проверялось:** Edge, Firefox, Safari, настоящий смартфон, скринридер, IME, реальные Telegram/SMTP/OpenAI.

Порядок ручной проверки перед показом.

*Windows (Chrome или Edge):*

1. Откройте адрес из `HOUSE_HELPER_PUBLIC_URL`, войдите. В адресной строке `/`, не `/auth/me`; видны «House Helper», «Выйти», «Новый разговор».
2. Отправьте «Что приготовить из курицы и риса?»: ваше сообщение, затем «Отправляем…» / «Помощник готовит ответ…», затем ответ; поле очистилось, кнопка снова активна.
3. Enter переносит строку, Ctrl+Enter отправляет. Обновите страницу (F5): тот же разговор и вся история.
4. «Новый разговор»: в списке он появляется только после первого сообщения.
5. DevTools → Network → Offline, отправьте сообщение: «Не удалось получить ответ: связь прервалась…» и одна кнопка «Повторить» (кнопки «Вернуть в поле ввода» нет, поле ввода заблокировано с пояснением). Включите сеть, «Повторить»: одно сообщение и один ответ, не два. DevTools → Application → Session Storage: запись `hh.pending.v1` есть, пока сообщение не разрешилось, в ней нет cookie и токенов; после ответа её нет.
6. «Выйти»: экран входа, прежних сообщений не видно (и по «Назад»). Затем войдите снова, DevTools → Network → Offline и «Выйти»: «Не удалось подтвердить выход из аккаунта…» и кнопка «Выйти ещё раз», разговоров на странице нет; включите сеть и нажмите её: экран входа с «Вы вышли из аккаунта.».
7. Выход с потерянным ответом и другой пользователь: в первой вкладке DevTools → Network → Offline и «Выйти» («Не удалось подтвердить выход…»); во второй вкладке **того же браузера** войдите другим пользователем; в первой включите сеть и нажмите «Выйти ещё раз»: «Выход не выполнен: в этом браузере уже вошёл другой пользователь…», кнопка «Повторить», разговоров прежнего пользователя нет; вторая вкладка по-прежнему вошла (обновите её: тот же пользователь, cookie на месте); «Повторить» в первой вкладке открывает страницу второго пользователя.
8. Смена пользователя: в первой вкладке оставьте разговор и недописанный текст в поле ввода; во второй вкладке **того же браузера** (cookie одна) выйдите и войдите другим пользователем; вернитесь в первую: на мгновение «Проверяем вход…», затем разговоры нового пользователя; ничего от прежнего (ни сообщений, ни черновика) на странице нет. Если вместо этого в другой вкладке тот же пользователь, при возврате разговор и черновик на месте.
9. Табуляция: у каждой кнопки и поля виден фокус, порядок логичен.

*Смартфон:* страница работает при `HOUSE_HELPER_PUBLIC_URL` с `https` (обычный `http` — только для localhost). Варианты: (а) быстрая проверка вёрстки — режим устройства в Chrome (F12, Ctrl+Shift+M); (б) Android по USB: `chrome://inspect/#devices` → Port forwarding `8000` → `localhost:8000`, `HOUSE_HELPER_PUBLIC_URL=http://localhost:8000`, на телефоне открыть `http://localhost:8000`; (в) настоящий телефон и HTTPS-адрес развёртывания. Проверьте: нет горизонтальной прокрутки; «Разговоры» открывает панель, нажатие вне панели закрывает; клавиатура не закрывает поле ввода; кнопки удобно нажимать; длинный рецепт читается; русская клавиатура и перенос строки работают; поворот экрана.

### Тесты страницы

| Файл | Что проверяет | Нужен Node.js |
|---|---|---|
| `tests/test_web_ui.py` | оболочка и признаки способов входа, заголовки и CSP, файлы `/static`, вход и выход с переходом на `/`, запрос письма, ответы API в тех формах, которые ждёт страница (создание, страницы истории, повтор ключа, занято, сбой, «вытеснено», лимит в кодовых точках) | нет |
| `tests/test_web_ui_assets.py` | сканирование файлов: нет `innerHTML`/`eval`/внешних адресов, согласованность `id` оболочки и скрипта, контраст и размеры из `app.css` | нет |
| `tests/test_web_ui_logic.py` | `chat-logic.js` против поддельного сервера (110 сценариев: вход, разговоры, страницы, ключи, неясные исходы и невозможность отдать ключ, чтение истории перед отправкой, `409`, любой `5xx` как неясный исход (в том числе `503` после завершённого и сохранённого хода — один ход и один ключ при любых повторах), восстановление и чистка записи (запись другого пользователя не возвращается), конец сессии по сроку и по `401`, возврат на вкладку — тот же пользователь, другой, нет ответа, запоздавшие ответы, выход с потерянным ответом и поздним ответом, `user_id` выхода в каждой попытке и его забывание, `409 session_mismatch` при повторе (первом и последующем) и только он, время в четырёх часовых поясах) и сверка с сервером: обрезка, подсчёт и проверка текста на 1220 сгенерированных строках, тексты ошибок | да |
| `tests/test_web_ui_dom.py` | `app.js` в мини-DOM с часами сценария (а не сегодняшней датой): чужой текст остаётся текстом, поле ввода и его блокировка, кнопки повтора, панель, выход (в том числе с потерянным ответом и с чужой сессией при повторе), конец сессии (со скрытыми частями страницы), страница, скрытая на время вопроса «кто вошёл», и возврат черновика только тому же пользователю, форма почты | да |
| `tests/test_web_ui_integration.py` | `chat-logic.js` против настоящего приложения на loopback по HTTP: потерянный ответ и потерянный запрос, занятый диалог, сбой помощника (известный только по истории), `503` настоящего маршрута после того, как настоящий исполнитель завершил и сохранил ход, «вытеснено», лимит, конец сессии, выход с потерянным ответом: повтор тем же человеком и повтор, когда в браузере уже другой (его сессия не затронута, проверяется и по БД); подсчёт вызовов помощника и строк в БД | да |

Это **не** проверка в браузере: вёрстка, настоящие события, фокус и прокрутка в них не участвуют. Тесты с Node.js **пропускаются (`skipped`), если `node` не найден**; в CI (ubuntu) он есть.

## Как проверить контекст

1. Напишите боту: «У меня есть курица, рис, яйца и сыр. Что можно приготовить?»
2. Уточните: «А если без духовки?» — бот должен помнить перечисленные продукты.
3. Уточните: «Хочу самый быстрый вариант.» — бот должен учитывать и продукты, и ограничение без духовки.
4. В консоли смотрите логи: `context_len` растёт с каждым обменом сообщениями (до ограничения, см. ниже).

## Как очистить контекст

- Команда `/reset`
- Сообщение: «очистить контекст» (регистр не важен)

После очистки бот не помнит предыдущие реплики в рамках этого `user_id`.

## Ограничения текущей версии

- **Хранение только в памяти (Telegram-бот).** Контекст диалога бота живёт в оперативной памяти процесса и полностью сбрасывается при перезапуске бота — базы данных у бота нет. История веб-чата хранится в базе данных (раздел «Веб-чат (API)»).
- **Ограничение истории диалога.** На пользователя хранится не более `MAX_HISTORY_MESSAGES` сообщений (константа в `context_manager.py`, по умолчанию 20 — то есть последние 10 обменов «пользователь-ассистент»). При превышении бот отбрасывает самые старые сообщения целыми парами «пользователь+ассистент», чтобы не нарушить последовательность ролей.
- **Ограничение длины сообщения.** Сообщения длиннее `DEFAULT_MAX_INPUT_LENGTH` символов (константа в `house_helper/core/chat_service.py`, по умолчанию 2000) отклоняются с вежливым сообщением на русском, не отправляются в OpenAI и не попадают в контекст.
- Только текстовые сообщения; изображения, голосовые сообщения и другие типы контента не поддерживаются.
- Нет очереди/лимита запросов сверх обычной поочерёдной обработки сообщений одного пользователя; веб-интерфейс — первая версия (раздел «Веб-интерфейс (кухня)»); нет планировщика, других доменов (сад, дом и т.п.) — это отдельные направления возможного развития проекта, пока не реализованные.

## Приватность и обработка данных

Текст сообщений, которые пользователь отправляет боту для получения рецептов, передаётся во внешний сервис **OpenAI** для генерации ответа. Бот сам не хранит данные в базе — только в оперативной памяти процесса, и полностью очищает их при перезапуске или по команде `/reset`. Не отправляйте боту личные или конфиденциальные данные. Бот сообщает об этом в ответ на `/start`.

Веб-чат, наоборот, хранит сообщения в базе данных: срок хранения каждого сообщения — не более 365 дней (дата записана в `expires_at`, истёкшие сообщения не показываются и не передаются модели); физическое удаление выполняет команда оператора `purge-expired` (выпущена в коде, аудит пройден; таймер установлен, первый запуск по расписанию ещё не проверен; в более старых резервных копиях удалённые сообщения сохраняются — `deploy/RUNBOOK.md`, раздел 14). Текст сообщений веб-чата тоже передаётся в OpenAI.

## Тесты

Тесты не выполняют реальных сетевых запросов к Telegram или OpenAI (все внешние вызовы замоканы). Это не только договорённость: офлайн-защита (`tests/network_guard.py`) на время тестов блокирует любые исходящие соединения и DNS-запросы, кроме loopback, а тест, который всё же попытался выйти в сеть, падает. Тестовое окружение (`tests/conftest.py`) явно задаёт `BOT_TOKEN`, `OPENAI_API_KEY` и остальные переменные конфигурации перед импортом кода — тесты не зависят от реального `.env` разработчика или уже установленных переменных окружения.

```bash
pytest
```

Асинхронным тестам нужен плагин `pytest-asyncio` (он есть в `requirements-dev.txt`). Если запускать `pytest` с отключённой автозагрузкой плагинов (`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, так делают изолированные окружения), плагин не подключится, и прогон сразу остановится с понятным сообщением, а не упадёт сотнями несвязанных ошибок. В таком режиме подключите плагин явно: `pytest -p pytest_asyncio.plugin`. Сетевая защита от этого не ослабляется.

Тесты скрипта страницы подтверждения входа по email (`tests/test_web_email_pages.py`) и тесты веб-интерфейса (`tests/test_web_ui_logic.py`, `test_web_ui_dom.py`, `test_web_ui_integration.py`, раздел «Тесты страницы») запускают скрипты в Node.js и **пропускаются (`skipped`), если `node` не найден**. Пропуск не ошибка, но и не проверка: убедитесь, что в CI эти тесты выполняются (`pytest -rs` перечисляет пропущенные), либо проверьте скрипт при интеграции с настоящим браузером.

Тесты `provision-email` и команд приглашений на настоящем PostgreSQL (`tests/test_admin_postgres.py`, `tests/test_admin_enrollment_postgres.py`) **пропускаются (`skipped`), пока не задана `HOUSE_HELPER_TEST_POSTGRES_URL`**, и в CI не запускаются; как их запустить и что они защищают от запуска на чужом сервере — в разделе «Первый пользователь».

## CI

При каждом push и pull request в `main` GitHub Actions ([.github/workflows/ci.yml](.github/workflows/ci.yml)) устанавливает зависимости из `requirements-dev.txt` и запускает полный набор тестов (`pytest`) на Python 3.10 и 3.12 — минимальной поддерживаемой версии и текущей версии разработки. Реальные учётные данные Telegram/OpenAI для CI не требуются — тесты используют только фиктивные значения.
