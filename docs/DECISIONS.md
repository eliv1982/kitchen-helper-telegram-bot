# Kitchen Helper — Decision register

**Compiled:** 2026-10-18 from owner-provided handoff. **These IDs are new index labels, not original ADR numbers.** Do not claim more precision than the source supports.

| ID | Decision | State / source |
|---|---|---|
| KHD-01 | Kitchen Helper prioritizes a real cooking Web MVP for the owner's mother, not a generic household platform. | ACCEPTED — Stage 2A summary |
| KHD-02 | All end-user UI, messages, recipe text and errors are in Russian. | ACCEPTED — Stage 2A summary |
| KHD-03 | Three equally important chat intents: ingredients, specific dish, free inspiration. | ACCEPTED — Stage 2A summary |
| KHD-04 | Multiple user conversations, durable history, device sync; each message expires after 365 days. | ACCEPTED product requirement; not implemented as Web product |
| KHD-05 | Saved recipes and long-term preferences are separate from history and survive history cleanup. | ACCEPTED product requirement; not fully implemented |
| KHD-06 | Preference extraction distinguishes explicit preferences, justified hypotheses and one-off wishes; user corrections take precedence. | ACCEPTED product requirement; not implemented |
| KHD-07 | Shared User with independently approved Telegram OIDC and passwordless email identities; closed allowlist. No automatic merging. | ACCEPTED; backend pieces RELEASED |
| KHD-08 | Modular Python monolith; Telegram and FastAPI processes share transport-neutral core. | ACCEPTED architecture |
| KHD-09 | SQLite, SQLAlchemy and Alembic, with explicit DB migration; no automatic migration on Web startup. | ACCEPTED architecture |
| KHD-10 | User ownership checks are server-side; `User`, `conversation_id` and `ConversationKey` are distinct; Web/Telegram histories not automatically unified. | ACCEPTED architecture |
| KHD-11 | Absolute Web session expiration is 14 days; idle timeout deferred. | ACCEPTED for current release; later review deferred |
| KHD-12 | Email matching preserves case-sensitive local-part; lowercases only domain; legacy emails from pre-0004 must be independently reverified. | RELEASED R3B-2 correction |
| KHD-13 | No auto commit/push; independent Codex risk audit before explicit ACCEPT and separate release command. | ACCEPTED engineering workflow |
| KHD-14 | Product-level Web voice STT/TTS, per-step speech and graceful fallback belong to MVP. | ACCEPTED requirement; not implemented |

## Proposed / deferred — NOT approved implementation decisions

- **R3B-3 Admin Bootstrap — no longer only proposed:** the narrow operator CLI `python -m house_helper.admin provision-email` is implemented and was ACCEPTED by the owner after a final independent Codex PASS; it is RELEASED only once GitHub CI succeeds (see `STATUS.md`). Its scope is exactly: create the first `User` with an approved email allowlist entry in one transaction (email-first), or re-approve an explicitly confirmed revoked email. It does not create Telegram identities, a second user, merges or migrations. The command contract is in the root `README.md`, section "Первый пользователь"; the code and tests are the authority.
- **Telegram initial linking — operator side RELEASED in R4C-Auth A2 (2026-10-21), Web side RELEASED in R4C-Auth A3 (2026-10-22) — code only, not live deployed:** the owner ACCEPTED (after a final independent Codex PASS) and released as `c20a26e` a closed, operator-controlled enrollment: the operator creates a user and issues a Telegram invitation (two hours, single use, revocable; migration `0005_identity_invitations`; `enroll-user`, `reissue-invitation`, `revoke-invitation`, `enrollment-status`). The limit of two users is a temporary policy of the enrollment command, not a database rule. The owner then ACCEPTED (after a final independent Codex PASS) and released as `9159842` the Web side: the person opens a secret-in-fragment invitation link, signs in through Telegram OIDC, and the verified `sub` is linked to the invited user — invitation consumption, `sub` allowlisting, identity link and session issuance in one atomic transaction, with the invitation bound to the OIDC state on the server (migration `0006_oidc_state_invitation`). This completes the accepted Telegram Web onboarding decision in code: initial `sub` acquisition is only through a verified Telegram login, never typed in or guessed; the two users stay independent; later logins use the ordinary Telegram button; the 14-day absolute session (KHD-11) is unchanged. **It is not verified against a real Telegram, BotFather is not configured, nothing is deployed, migration `0006` is applied nowhere and no user or invitation exists** (see `STATUS.md`). The earlier email-first linking workflow remains a **proposal**, not an accepted security design. Remembered-device authentication is not decided or built.
- **R3C idle timeout:** deferred; current absolute session lifetime remains 14 days.
- **Live domain, proxy topology, SMTP provider and real Telegram integration:** not selected or verified as of the handoff. *[Updated 2026-10-23: the domain (`house.elivcloud.org`) and the proxy topology (the existing Traefik) are now recorded as accepted deployment direction, KHD-23, with the R4C-D1 kit — neither is verified live. The SMTP provider and real Telegram integration remain not selected / not verified.]*


## Approved direction — House Helper domains, UX and diagnostics (2026-10-18)

These decisions were confirmed by the project owner in the Kitchen Helper project discussion on **2026-10-18**. They are **approved product/architecture direction**, not evidence that any related feature has been implemented, audited, or deployed. They do not enlarge the currently approved Kitchen MVP scope.

| ID | Decision | Status |
|---|---|---|
| **KHD-15** | **House Helper** is the long-term umbrella product; **Kitchen** is the first working domain, with **Garden, Vegetable Garden and Home** as potential later domains. Keep current repo/name and the accepted Kitchen MVP scope unchanged for now. | ACCEPTED direction; later domains DEFERRED |
| **KHD-16** | One consistent assistant identity/interaction style, with domain-specific instructions, allowed capabilities, and safeguards. Do not build separate independent agents or user accounts merely because a new domain is introduced. | ACCEPTED design direction; implementation DEFERRED |
| **KHD-17** | When multiple domains exist, the user explicitly chooses the current domain from the UI. Within that domain, natural conversation remains possible. A cross-domain switch is offered to the user rather than performed silently by automatic topic guessing. | ACCEPTED UX direction; multi-domain UI DEFERRED |
| **KHD-18** | Separate domain-specific preferences, memories, histories, and permissions as appropriate. Reuse shared services where justified, but do not automatically transfer kitchen preferences into garden/home behavior or merge unrelated histories. | ACCEPTED privacy/architecture direction; implementation DEFERRED |
| **KHD-19** | Soft topical boundaries: full assistance in-domain, brief useful responses for adjacent topics, short polite redirection for unrelated requests. Avoid repetitive rigid refusals; respond appropriately to a user in distress. Topic boundaries are not a replacement for domain-specific safety rules. | ACCEPTED behavior direction; Kitchen-specific implementation PLANNED |
| **KHD-20** | Use the existing n8n deployment for an optional, shared **House Helper — Error Monitoring** notification workflow, not for core request handling. Server logs are primary; n8n receives a sanitized, minimal event and can alert the operator via Telegram. An n8n outage must never block the assistant's response. | ACCEPTED design direction; integration NOT IMPLEMENTED |
| **KHD-21** | First user's visual preference: a **light UI** with **royal/deep blue**, **rich green**, and **light blue**. Use **red** for legible error feedback. Exact HEX values remain a *design proposal*, subject to contrast/readability checks. | USER PREFERENCE CONFIRMED; precise palette PROPOSED |
| **KHD-22** | For Web beta, failures should display a readable Russian message, an unobtrusive correlation/error ID, and a useful retry action where applicable. Match that ID to safe backend logs so a screenshot supports diagnosis. Never surface internal tracebacks or secrets. | ACCEPTED beta UX direction; NOT IMPLEMENTED |

### Why / alternatives / boundaries

- **House Helper umbrella vs separate apps:** favors reuse of auth, safe diagnostics and shared core while keeping each domain's rules and data distinct. Building several independent agents/apps now was rejected as unnecessary complexity.
- **Explicit domain selection vs automatic switching:** keeps user control, predictability and clear topic boundaries; automatic guess-based transitions are not approved.
- **Soft redirection vs blanket refusals:** preserves a natural conversation without turning Kitchen Helper into a general-purpose chatbot. The current release is still about cooking.
- **n8n as optional notifications vs n8n in the critical path:** keeps cooking functionality and local diagnostic evidence available even if automation is offline; no recipes, transcripts, credentials, magic links, or personal data in n8n diagnostic payloads.
- **Visual palette:** user confirmed family of colors only; sample HEX codes in chat were illustrative, not design-system tokens.

### Implementation boundary

Do **not** modify the accepted R3B-3 Admin Bootstrap code or its audit to implement these decisions. Design the Kitchen Web UI and its error correlation in a later scoped increment; implement n8n notifications only after local structured error reporting exists and its privacy/safety boundaries are verified. Future Garden/Vegetable Garden/Home features need their own product criteria before implementation.

## Accepted deployment direction — R4C-D1 (2026-10-23)

These decisions record the deployment approach the owner ACCEPTED with R4C-D1 (final independent Codex PASS, owner ACCEPT; code RELEASED as `2f51d2f`). They describe a **repository-only kit: nothing is deployed**, and none of them is evidence of a live deployment. They add no product decision. The target host and proxy come from the read-only R4C-0 infrastructure inventory (2026-10-19). Procedures live in `deploy/RUNBOOK.md`, not here; the gates are in `STATUS.md`.

| ID | Decision | Status |
|---|---|---|
| **KHD-23** | Reuse the existing Netherlands VPS (`eliv-server`) with its existing Traefik, Let's Encrypt resolver and external Docker network. The target public URL is `https://house.elivcloud.org`. No new VPS, domain, second proxy or certificate manager. | ACCEPTED — R4C-D1; kit RELEASED, NOT deployed; DNS and TLS not provisioned |
| **KHD-24** | The initial application image is built on the existing VPS from an approved commit's checkout. | ACCEPTED — R4C-D1; not performed |
| **KHD-25** | Database migrations are separate, explicit operator steps; they never run automatically on Web startup. A startup guard refuses to serve a database whose schema is not the image's. (Applies KHD-09.) | ACCEPTED — R4C-D1; RELEASED in the kit |
| **KHD-26** | SQLite on a persistent Docker volume. Every upgrade is preceded by a verified backup (the upgrade script refuses to migrate otherwise), and rollback is the saved database plus the version that wrote it, not `alembic downgrade`. Backup and recovery procedures must be rehearsed before they are relied on. | ACCEPTED — R4C-D1; tooling RELEASED; off-host copy, schedule, retention and a live recovery rehearsal NOT done |
| **KHD-27** | Runtime configuration and secrets are supplied outside the repository: the repository holds only a sanitized template of names, and the real environment file is created on the host outside the checkout (mode 0600). | ACCEPTED — R4C-D1; template RELEASED, no real file exists |
| **KHD-28** | The initial HSTS `max-age` is 86400 seconds, subject to live validation; a longer value is for after HTTPS has been proven in real browsers. | ACCEPTED as the initial value — R4C-D1; live validation NOT done |
| **KHD-29** | No live deployment — DNS, VPS, BotFather, a real database, real users — happens without separate, explicit owner approval. Every kit step that changes something outside the repository is marked **[OWNER]**. | ACCEPTED — R4C-D1; in force |

### Why / alternatives / boundaries

- **Reuse vs new infrastructure:** R4C-0 found Traefik, the resolver, the network and spare capacity already on the VPS; a second proxy, a certificate manager, another VPS or another domain were rejected as unnecessary. The kit does not touch the shared Traefik or the other applications on the host.
- **Build on the VPS:** no image registry or build pipeline is introduced. Image delivery by building on the server is documented but has not been exercised on the VPS.
- **Explicit migrations and verified backup first:** a schema change cannot be undone by running it backwards (`alembic downgrade` deletes data), so a migration happens only right after a backup that has been checked and verified; a start-up that migrates by itself was rejected (KHD-09).
- **Short initial HSTS:** a browser keeps the commitment, so it starts short and is raised only after live validation.
- **Not decided here:** the off-host backup destination, backup frequency and retention; the scheduled cleanup of authentication artifacts and the 365-day physical deletion (a separate increment, proposed as R4C-D2 — Scheduled Cleanup & Retention, scope to be designed; both stay mandatory before their respective real-user-data use, KHD-04); Remembered Device; operational SMTP/email activation; any additional Kitchen functionality.

## Change discipline

Add a new decision (date, rationale, alternatives and approval source) only after an actual decision. For a change to an earlier decision, reference its ID and record the replacement instead of silently rewriting history. Link full Stage 2A/2B originals when recovered.
