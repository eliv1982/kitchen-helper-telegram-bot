# Kitchen Helper — Release status and next milestones

**Snapshot:** 2026-10-18 (finalized for the R4A release). **Evidence grade:** entries up to R3B-2 are reported in the owner handoff; the R3B-3 and R4A entries are verified against git refs and the push-triggered GitHub Actions runs. No production probe. Revalidate before updating.

## Release ledger

| Scope | Status | Commit / evidence |
|---|---|---|
| Stage 1B Shared Core | CLOSED (reported) | previously accepted |
| Stage 2A Product Discovery | ACCEPT (reported) | 2026-10-16; original 25 AC not included here |
| Stage 2B Architecture Design | ACCEPT (reported) | original design not included here |
| R1 Persistence Foundation | RELEASED (reported) | `a870e17873e477c5bf5e892b0b294b42fe063a13` |
| R2 FastAPI Foundation | RELEASED (reported) | `ff710da050c01036aa4cf81e0475771482ed5c98` |
| R3A Telegram OIDC & Sessions | RELEASED (reported) | `ee55a97a3ccbbb944602a1353ece5a813039db6a` |
| R3B-1 Session Provenance | RELEASED (reported) | `d5979e85fe1110c8ad5fdff72062707ec8910d5c` |
| **R3B-2 Passwordless Email** | **RELEASED (reported)** | **`6e2b432d460bfca18ed7d697bb8c05490fcc7512`** |
| **R3B-3 Admin Bootstrap** | **RELEASED (final independent Codex PASS, owner ACCEPT; push-triggered CI green)** | **`8934536a75d7da42934ef3452857bfabc44d51e4`** |
| **R4A Authenticated Web Chat API** | **RELEASED (independent Codex re-audit PASS, owner ACCEPT; push-triggered CI green)** | **`b14322163c531fff649c8b2378f297d918009058`**, built on baseline `65026a9ca696d655750c64c56f3d47b8b8efcc13` |

R4A local verification before commit (Windows, pinned `requirements-dev.txt`; **not CI, not Linux**): Python 3.12.10 — 4211 passed / 26 skipped; Python 3.11.9 and 3.10.22 — 4210 passed / 27 skipped; `compileall` and `pip check` clean on all three. The 26 skips are the opt-in real-PostgreSQL suites (`HOUSE_HELPER_TEST_POSTGRES_URL` unset: 9 admin + 17 chat tests); 3.10/3.11 skip one more, the defensive-SQLite `setconfig` test that needs 3.12+. The 17 R4A PostgreSQL 16 tests passed earlier on a throwaway container, outside this run. No real OpenAI call was made.

Verified successful R4A CI (push-triggered, head SHA `b14322163c531fff649c8b2378f297d918009058`): <https://github.com/eliv1982/kitchen-helper-telegram-bot/actions/runs/39142339871> — Python 3.12: 4208 passed / 26 skipped; Python 3.10: 4207 passed / 27 skipped. The skips are the opt-in real-PostgreSQL suites (not run by CI), plus the 3.12-only `setconfig` test on 3.10. **Not real-PostgreSQL, real-provider, real-environment or production verification.**

Verified successful R3B-3 CI (push-triggered, head SHA `8934536a75d7da42934ef3452857bfabc44d51e4`): <https://github.com/eliv1982/kitchen-helper-telegram-bot/actions/runs/39122308108> — Python 3.12: 3692 passed / 9 skipped; Python 3.10: 3691 passed / 10 skipped. **Not real-PostgreSQL, real-environment or production verification.**

Reported successful R3B-2 CI: <https://github.com/eliv1982/kitchen-helper-telegram-bot/actions/runs/39073791846> — Python 3.12: 3405 passed / 0 skipped; Python 3.10: 3404 passed / 1 skipped; 13 Node-backed magic-link tests passed on both jobs. **Not real-browser or production verification.**

## Existing vs not yet built

- **Released in Git (reported):** persistence schema, FastAPI service foundation, Telegram OIDC, per-identity session provenance/revocation, passwordless email login.
- **Released in Git (R3B-3):** operator CLI `python -m house_helper.admin provision-email` (`house_helper/admin/`): creates the first `User` and its approved email allowlist entry in one transaction, email-first; refuses when a user already exists, never creates a Telegram identity, never applies migrations. Offline tests run on SQLite; the optional real-PostgreSQL suite (`tests/test_admin_postgres.py`) skips unless `HOUSE_HELPER_TEST_POSTGRES_URL` is set and is not run by CI. Details: root `README.md`, section "Первый пользователь".
- **Released in Git (R4A):** authenticated, persistent Web Chat API (`house_helper/web/chat_api.py`, `turn_runner.py`, `diagnostics.py`; `house_helper/persistence/conversations.py` and the `begin_turn`/`complete_turn`/`fail_turn` lifecycle in `turns.py`; standalone OpenAI backend `house_helper/core/openai_chat.py`): create/list conversations, read history, send a message under an `Idempotency-Key`. One turn at a time per conversation, lease-fenced turn recovery, model call outside DB transactions, 365-day `expires_at` recorded on stored messages. **No schema change and no migration** (`0001`–`0004` unchanged). The Telegram bot's behavior is unchanged apart from the shared system prompt and the default `MAX_TOKENS` 600 → 1000 (an existing `.env` value is kept). Details: root `README.md`, section "Веб-чат (API)".
- **Not verified in a real environment:** BotFather/OIDC flow; real SMTP delivery; magic-link browser E2E; final reverse proxy/cookie topology; `provision-email` against any real (production) database; the Web Chat API against the real OpenAI API (no real provider call has been made; prompt quality, redirects and distress wording are unevaluated); the Web Chat HTTP layer on real PostgreSQL (persistence and runner were run on PostgreSQL 16 through the opt-in suite, not through HTTP).
- **Not yet implemented as user-facing Web product:** responsive chat page (no Web UI exists), history page, favorites UI, preferences UI, STT/TTS; also conversation titles, deleting or resetting a conversation, response streaming, per-user rate limits.
- **Not implemented (decided or proposed only):** Telegram initial linking (verified `sub` acquisition); House Helper domain selection and non-Kitchen domains (KHD-15–KHD-19); n8n error monitoring and correlation IDs (KHD-20, KHD-22); the Web UI palette (KHD-21).
- **Not deployed to mother:** the Web MVP; no production release is claimed. No real user is provisioned and no production migration has been applied.

## Closest next step

1. The Web Chat API is built (R4A); the remaining first-scenario work is the adaptive Russian chat UI on top of it (proposed, not started). Do not spend several stages polishing unrelated infrastructure.
2. Design and verify initial Telegram `sub` linking separately; configure real SMTP/Telegram credentials and test proper browser/HTTPS flows before real-user launch.

## Deployment gates / known risks

- Existing DB migrations `0002`–`0004` not applied as of handoff: require explicit authorization, stop/backup/upgrade/integrity/restart/re-login procedure.
- `provision-email` has not been run against any real database; no real user is provisioned. Run it only after the migrations are applied and finished: migrations and `provision-email` must never run concurrently (an operational rule, not enforced by code). Real provisioning needs explicit owner authorization.
- **Physical retention purge (R4A gate):** messages and turn requests carry a 365-day `expires_at` and expired rows are hidden from reads and model context, but **nothing deletes them yet**. A purge job (or an owner-approved equivalent) must exist before real user data is stored.
- **Public ingress security (R4A gate):** the Web Chat API has no global provider-concurrency cap, per-user or per-IP rate limit, or conversation count limit; these belong to the trusted ingress/reverse proxy and are not built or verified. Required before any public exposure, together with the existing auth-endpoint rate limits below.
- **Real-provider evaluation (R4A gate):** run the chat against the real OpenAI API with the owner's authorization and review answer quality and `truncated` behavior before real use; the shared system prompt and the 1000-token default have never met a real model in this project's tests.
- Timing differentiation in email login request path (reported approx. 4.14 ms approved vs 1.94 ms unknown) needs production mitigation with request throttling.
- Rate limit auth endpoints at trusted ingress; filter secret-bearing callback query strings at all logging layers; verify SMTP SPF/DKIM/DMARC, TLS and browser cookies.
- No arbitrary migration, deployment, account merging or re-enabling revoked legacy email without owner approval and independent verification.
- GitHub Actions Node 20 warnings and Ubuntu runner transition are maintenance items, not automatic scope expansions; act on real failures or approved cleanup.

## Status update checklist

For each released increment, record: name + scope; implementation and audit verdict; exact commit; GitHub Actions run and results; local integration checks; real-provider checks; migration/deployment status; remaining gates; one proposed next increment. Mark each claim VERIFIED, REPORTED, PROPOSED or DEFERRED.
