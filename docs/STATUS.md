# Kitchen Helper — Release status and next milestones

**Snapshot:** 2026-10-18 (updated for the R3B-3 release). **Evidence grade:** entries up to R3B-2 are reported in the owner handoff; the R3B-3 entry rests on the release preflight (`git fetch`, working-tree review, local test run). No production probe. Revalidate before updating.

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
| **R3B-3 Admin Bootstrap** | **ACCEPTED (final independent Codex PASS, owner ACCEPT); pending GitHub CI release confirmation. Not yet RELEASED.** | commit SHA and CI run are recorded here only after the push-triggered CI succeeds |

Latest reported successful R3B-2 CI: <https://github.com/eliv1982/kitchen-helper-telegram-bot/actions/runs/39073791846> — Python 3.12: 3405 passed / 0 skipped; Python 3.10: 3404 passed / 1 skipped; 13 Node-backed magic-link tests passed on both jobs. **Not real-browser or production verification.**

## Existing vs not yet built

- **Released in Git (reported):** persistence schema, FastAPI service foundation, Telegram OIDC, per-identity session provenance/revocation, passwordless email login.
- **Accepted, pending CI (R3B-3):** operator CLI `python -m house_helper.admin provision-email` (`house_helper/admin/`): creates the first `User` and its approved email allowlist entry in one transaction, email-first; refuses when a user already exists, never creates a Telegram identity, never applies migrations. Offline tests run on SQLite; the optional real-PostgreSQL suite (`tests/test_admin_postgres.py`) skips unless `HOUSE_HELPER_TEST_POSTGRES_URL` is set and is not run by CI. Details: root `README.md`, section "Первый пользователь".
- **Not verified in a real environment:** BotFather/OIDC flow; real SMTP delivery; magic-link browser E2E; final reverse proxy/cookie topology; `provision-email` against any real (production) database.
- **Not yet implemented as user-facing Web product:** Web Chat API, responsive chat page, durable conversation handling in Web app, history page, favorites UI, preferences UI, STT/TTS.
- **Not implemented (decided or proposed only):** Telegram initial linking (verified `sub` acquisition); House Helper domain selection and non-Kitchen domains (KHD-15–KHD-19); n8n error monitoring and correlation IDs (KHD-20, KHD-22); the Web UI palette (KHD-21).
- **Not deployed to mother:** the Web MVP; no production release is claimed.

## Closest next step

1. Confirm the R3B-3 push-triggered CI result, then record the commit SHA, run URL and results in the ledger and mark R3B-3 RELEASED (a separate documentation change).
2. Prioritize minimal Web Chat API and adaptive Russian UI; do not spend several stages polishing unrelated infrastructure.
3. Design and verify initial Telegram `sub` linking separately; configure real SMTP/Telegram credentials and test proper browser/HTTPS flows before real-user launch.

## Deployment gates / known risks

- Existing DB migrations `0002`–`0004` not applied as of handoff: require explicit authorization, stop/backup/upgrade/integrity/restart/re-login procedure.
- `provision-email` has not been run against any real database; no real user is provisioned. Run it only after the migrations are applied and finished: migrations and `provision-email` must never run concurrently (an operational rule, not enforced by code). Real provisioning needs explicit owner authorization.
- Timing differentiation in email login request path (reported approx. 4.14 ms approved vs 1.94 ms unknown) needs production mitigation with request throttling.
- Rate limit auth endpoints at trusted ingress; filter secret-bearing callback query strings at all logging layers; verify SMTP SPF/DKIM/DMARC, TLS and browser cookies.
- No arbitrary migration, deployment, account merging or re-enabling revoked legacy email without owner approval and independent verification.
- GitHub Actions Node 20 warnings and Ubuntu runner transition are maintenance items, not automatic scope expansions; act on real failures or approved cleanup.

## Status update checklist

For each released increment, record: name + scope; implementation and audit verdict; exact commit; GitHub Actions run and results; local integration checks; real-provider checks; migration/deployment status; remaining gates; one proposed next increment. Mark each claim VERIFIED, REPORTED, PROPOSED or DEFERRED.
