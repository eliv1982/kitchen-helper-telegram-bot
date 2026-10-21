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
- **Telegram initial linking — operator side RELEASED in R4C-Auth A2 (2026-10-21); the Web side is still open:** the owner ACCEPTED (after a final independent Codex PASS) and released as `c20a26e` a closed, operator-controlled enrollment: the operator creates a user and issues a Telegram invitation (two hours, single use, revocable; migration `0005_identity_invitations`; `enroll-user`, `reissue-invitation`, `revoke-invitation`, `enrollment-status`). The limit of two users is a temporary policy of the enrollment command, not a database rule. Telegram `sub` acquisition through a real Telegram login, i.e. the Web claim of an invitation, is **not built** (next increment, A3) and **no real Telegram login has been verified**. The earlier email-first linking workflow remains a **proposal**, not an accepted security design. Remembered-device authentication is not decided or built. See `STATUS.md`.
- **R3C idle timeout:** deferred; current absolute session lifetime remains 14 days.
- **Live domain, proxy topology, SMTP provider and real Telegram integration:** not selected or verified as of the handoff.


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

## Change discipline

Add a new decision (date, rationale, alternatives and approval source) only after an actual decision. For a change to an earlier decision, reference its ID and record the replacement instead of silently rewriting history. Link full Stage 2A/2B originals when recovered.
