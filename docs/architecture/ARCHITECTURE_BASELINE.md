# House Helper (Kitchen domain) — Architecture baseline (Stage 2B summary)

**Source:** project owner handoff dated 2026-10-18. The product is now named **House Helper**; **Kitchen** is its first domain. The historical name "Kitchen Helper" is preserved in the source.

**Authority level:** architecture **summary**, not a verbatim copy of the full accepted Stage 2B design. Verify concrete behavior in code/migrations.

## Overall design

- Modular **Python monolith**, not microservices.
- The original standalone Telegram bot (`bot.py`) and the FastAPI Web run as distinct processes. **The Telegram bot that shares conversations with the Web is different:** it runs **inside the single Web process** (in-process long polling, plain `httpx`), so a Telegram message and a page message use the same turn runner and tables. This is accepted for the two-user SQLite alpha (KHD-35) and requires exactly one Web instance and exactly one `getUpdates` consumer. Sign-in through Telegram (OIDC) is independent of that listener. Reusable transport-agnostic application logic lives in `house_helper/core`.
- Shared Core must not import Telegram, FastAPI or provider configuration just by being imported.
- Storage: SQLite with SQLAlchemy and Alembic; PostgreSQL may follow later.
- Production DB configured explicitly via `HOUSE_HELPER_DATABASE_URL`; **never run migrations automatically during Web startup**.
- Keep Telegram transport and existing commit/rollback semantics stable unless a concrete approved change requires otherwise.

## Identity and conversations

- A stable internal `User` owns identity links, multiple conversations and messages.
- Telegram verified OIDC `sub` and approved email can point to the same internal User only through **explicitly authorized association**.
- Never infer Telegram `sub` from a username or numeric user ID. Never merge accounts simply because an email matches.
- `user_id` is not `conversation_id`. `ConversationKey("web", conversation_id)` identifies a single dialog, not an account.
- Server checks owner authorization on every operation that reads or changes a user-owned resource.
- Do not automatically unify Telegram and Web conversation histories merely because logins share one User. A person's own Web and Telegram conversations become **one store only after an explicit Telegram pairing** (KHD-35, KHD-36); nobody's conversations are unified with anyone else's, and an unlinked Telegram account is answered with one fixed notice and stores nothing.
- Durable history and LLM context are different abstractions. LLM context is bounded to 20 messages and must respect resets, deletions and retention.

## Reported implemented foundation

- **R1**: User, AllowlistEntry, IdentityLink, Session, OidcState, Conversation, Message, TurnRequest; SQLAlchemy persistence, constraints and Alembic `0001`. This does not imply the legacy Telegram runtime is already persisting conversations.
- **R2**: Web app factory/ASGI/lifespan, `/healthz` liveness without DB, bounded Web DB worker execution rather than blocking the async event loop.
- **R3A**: Telegram OIDC Authorization Code Flow + PKCE, state/nonce, ID-token verification, approved allowlist, hashed server sessions, HttpOnly cookie.
- **R3B-1**: session identity provenance, revoke per identity, migration `0002`; old sessions without provenance fail closed.
- **R3B-2**: passwordless email flow with atomic single-use 10-minute challenge; migration `0003`/`0004`; authorized email case-sensitive local part, case-insensitive domain; provider isolation. No automated User creation, merging or transfer.
- **R3B-3 (RELEASED, `8934536`; no new migration)**: operator-run CLI `python -m house_helper.admin provision-email` in `house_helper/admin/`, imported by neither the Web app nor the Telegram bot. It creates the single first `User` plus an approved email allowlist entry in one transaction, or re-approves an explicitly confirmed revoked email; it refuses when a user already exists, creates no Telegram identity, merges nothing and never runs migrations (target database only via `HOUSE_HELPER_DATABASE_URL`; schema must already be at `head`). It is not a Web Chat, Telegram-linking or domain feature. Contract: root `README.md`, section "Первый пользователь".
- **R4A**: an authenticated, persistent Web Chat API on top of the persistence and identity foundation: conversation creation/listing, history, and turns under an idempotency key with a lease-fenced lifecycle.
- **R4B**: a Russian-language responsive browser UI served by the Web app itself (plain HTML/CSS/JavaScript, strict CSP, no build step), backed by the R4A API.
- **R4C-Auth A2/A3**: operator enrollment through two-hour, single-use, revocable Telegram invitations; the invitation is claimed through a verified Telegram OIDC login whose `sub` is bound to the invited user in one atomic transaction. Two users remain independent.
- **R4C-D1/D2**: a hardened Docker/Compose deployment kit (Traefik, explicit migrations, startup schema guard, backup/verify/restore, fail-closed upgrade) and an operator `purge-expired` command with opt-in systemd scheduling.
- **R4C live deployment**: production is a single Web container behind the existing Traefik with SQLite on a persistent volume. Production schema is `0009_message_attachments` (the repository head is the same revision), and the owner accepted the Functional Alpha on 2026-10-31 (`docs/STATUS.md`).
- **In-process Telegram bot and photos** (KHD-35 to KHD-38): long polling inside the Web process; Web/Telegram conversations share one store after an explicit pairing; Web turns are mirrored to Telegram; photos are stored in the database, shown to the model and in Web history, served only to their owner, and expire with their message.

## Security invariants

- Sessions have a **14-day absolute** lifetime, currently without idle timeout or sliding renewal; secret in database only as digest.
- Email challenge token is 256-bit; secret in URL fragment; GET preview does not consume it; explicit POST confirmation consumes atomically.
- Email normalization preserves local-part case, punctuation, plus tags and dots; lowercases domain only. No provider-specific alias folding.
- Migration `0004` revokes legacy email allowlist entries/sessions/challenges pending independent reverification. Never auto-reapprove old email identities.
- Revocation must use supported application transaction/API paths; a re-allow must not reactivate revoked sessions.
- Public Web ingress needs auth request rate limits and timing-enumeration mitigation; forward headers must be trusted only behind configured proxy.
- Never log URL query parameters containing Telegram callback `code`/`state` at upstream/proxy/APM layers. Never log plaintext tokens/SMTP secrets or enable unsafe SQL echo.
- Real Telegram login for two users, HTTPS serving and `/healthz` are verified in the accepted alpha. Real SMTP delivery, cross-browser E2E beyond the alpha's use, an isolated off-host restore rehearsal, and live backup/alerting behavior still require separate verification.

## Deployment caution

Release to Git, local offline tests, real-provider authentication and live deployment are **different states**. **Current state (2026-10-31):** production runs image `house-helper:3d09d45f6318` at schema `0009_message_attachments`; Telegram sign-in and the in-process Telegram bot are enabled; email sign-in is disabled; the owner accepted the Functional Alpha on 2026-10-31 (REPORTED; production facts independently verified by Cursor). Migrations are still explicit operator steps: before any upgrade, stop the service, take and verify a backup, run the migration revision explicitly, check integrity including SQLite `foreign_key_check`, restart, and reauthenticate. `deploy/RUNBOOK.md` section 9 is the procedure; it stays in force for every later migration. PostgreSQL is supported by code and opt-in tests but is not the deployed database.


## Accepted direction after Stage 2B: multi-domain House Helper (2026-10-18)

**This is a dated architecture addendum, not an edit to the original Stage 2B acceptance or an implementation claim.** Stage 2B's accepted modular-monolith foundations remain unchanged. The currently implemented product is still the Kitchen domain.

### Identity, domains and routing

- **Product umbrella:** House Helper; initial domain **Kitchen**. Potential later domains: **Garden**, **Vegetable Garden** and **Home**. No immediate repository or package renaming.
- **Assistant persona:** one consistent helpful voice; domain-dependent prompt policy, retrieval/resources, allowed actions and safety constraints. One domain does not inherently require a new LLM agent, runtime, database or account.
- **User-driven domain context:** when other domains exist, the user explicitly selects a domain in Web UI; server-side conversation context records the chosen domain. Do not automatically switch domains based solely on an LLM guess. A cross-domain proposal requires a user action.
- **Data partitioning:** design domain-specific preferences, memory and conversation access boundaries. Identity may be shared, but content and preferences are not silently shared across domains. Existing Web/Telegram conversation separation remains in force. Domain boundaries will need explicit access-control/data-model review before new domains launch.

### Topic scope and assistant behavior

For Kitchen, provide full answers on cooking, ingredients and adjacent food handling/storage. Give brief useful responses on nearby topics where sensible; for unrelated requests, offer a short friendly reminder of current specialization without repetitive refusal loops. Handle distress sensitively; never treat a topic router as the sole safety mechanism. Future domains require their own scope and risk policies.

### Shared optional error notifications

- The House Helper app creates structured, privacy-aware backend diagnostic events. A user-facing Russian error can include an opaque **error/correlation ID** that matches safe server-side logs.
- Later, an **optional House Helper — Error Monitoring** n8n workflow may accept an authenticated, minimal event (`domain`, `component`, `error_type`, `severity`, `correlation_id`, timestamp), group repetitions and send operator Telegram alerts. These fields are a preliminary contract, **not implemented API schema**.
- Never send message contents, recipes, raw provider errors, user identifiers, email addresses, session tokens or callback URLs to n8n by default. Rate-limit/deduplicate alerts and minimize n8n execution-data retention.
- **Failure isolation:** notifying n8n cannot be a prerequisite for answering the user, and n8n unavailability cannot erase the local error record. Deployment/network details, alert thresholds and exact event schema are deferred.

### UX direction

First-user confirmed preference: light theme with deep/royal blue and rich green, light-blue accents; red should signal errors with clear text and accessible contrast. Exact HEX tokens have not been accepted. In the first Web UI, use appropriately visible font sizes, mobile ergonomics and readable error messages; obtain feedback from the first user before freezing a design system.

### Delivery boundary

No expanded scope for R3B-3. Prioritize usable Kitchen Web Chat and deployment/security gates. Implement these cross-domain concerns incrementally where they provide real value, without turning Kitchen's beta into a full House Helper platform. See `docs/DECISIONS.md` KHD-15–KHD-22.

## Unresolved architecture artifact

Locate the original accepted Stage 2B document and link it here. Any difference between this summary and the original specification is an issue for owner review, not grounds for silent redesign.
