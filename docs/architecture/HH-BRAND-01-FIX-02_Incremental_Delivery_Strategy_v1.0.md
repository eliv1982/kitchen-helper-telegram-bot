# House Helper — HH-BRAND-01-FIX-02
## Incremental Delivery Strategy v1.0

| Document control | Value |
|---|---|
| Date | 2026-11-25 |
| Version | 1.0 — assembled for Owner document review |
| Conceptual direction | Owner agreed on 2026-11-25, including Garden as the next priority module |
| Document status | READY FOR OWNER REVIEW |
| Companion document | `docs/architecture/HH-BRAND-01-FIX-02_Research_Charter_v1.0.md` (RD-01) |
| Intended repository path | `docs/architecture/HH-BRAND-01-FIX-02_Incremental_Delivery_Strategy_v1.0.md` |
| Repository | `eliv1982/kitchen-helper-telegram-bot` |
| Decision authority | Owner |
| Research execution / implementation | NOT AUTHORIZED |
| FIX-01 Production Release | BLOCKED |

> **Authorization boundary.** This document proposes a delivery and governance strategy for implementing the conceptually agreed incremental direction. It is not an execution order, an approved release plan, a branch strategy, a budget authorization, or a scope lock. It does not change the Research Charter or authorize any agent task, code modification, Git operation, provider call, access to production data, or deployment. VPS Maintenance retains priority until a separate Owner decision. Formal acceptance of the companion documents and execution authorization remain distinct decisions.

## 1. Purpose and product strategy

House Helper has two complementary purposes: a reliable personal product for the Owner's mother and an applied AI research environment that produces transferable architectural knowledge for future assistants, including Teya 2.0.

The Research Charter establishes what must be investigated and how to obtain credible evidence. This companion strategy establishes how to continue delivering useful product improvements during that research. The full research program must not become a universal prerequisite for unrelated product releases. Equally, the desire to ship a feature must not bypass the behavioral and technical acceptance of the candidate actually being released.

The agreed direction is **Incremental Intelligence Development**: improve practical usefulness and intelligence together through bounded, independently reviewable increments. Preserve Hybrid Intelligence, AP-01–AP-19, Quality First, Adaptive Memory, Agentic Level 2 with a Level 3 horizon, and Alternative C (Tool-Enabled Intelligence) as the preferred research direction with Alternative D (Adaptive Cognitive Orchestration) as its strategic evolution. Alternatives A and B remain available for evidence-based comparison. No architecture is selected for implementation yet.

Kitchen remains the first active domain. Garden (Garden & Vegetable Garden; Russian user-facing terminology to be agreed) is the next priority domain, driven by seasonal needs. Completing every Kitchen backlog item is not a prerequisite to beginning Garden. Automotive Assistant is deferred to the strategic horizon. Other future domains, including Budget Helper, remain backlog or planning topics rather than commitments under this strategy.

The mother receives accepted, reliable product capabilities; experimentation takes place in controlled research environments, not in her live account.

## 2. Baseline and immediate release constraint

The following state is reported in the 2026-11-25 handoff and Research Charter; it is not a fresh Git or VPS inspection.

| Item | Reported state |
|---|---|
| Local checkout | `C:\Users\eliv\Pet_Projects\house_helper` |
| Branch | `main` |
| Accepted FIX-01 implementation | `77e2bfd2f513e9285e0219d1d0f89538acf58610` |
| Documentary closure | `bc0986f` |
| Git / CI | Both commits pushed; CI PASS confirmed by Owner |
| Production URL | `https://house.elivcloud.org` |
| Production HEAD | `afd83cb65bd7cc6ab1ab6d638187115b05610c63` |
| Database | PostgreSQL 16.15; schema `0014_user_measure_overrides`; two user accounts |
| Maintenance image | `d02b34ed369c`, intentionally retained after compatibility verification |
| PostgreSQL Compose baseline | `ddca0966bd11` |
| FIX-01 technical implementation | ACCEPTED; independent audit PASS WITH NON-BLOCKING NOTES |
| FIX-01 behavioral evaluation | COMPLETED / FAILED: 24 PASS, 6 FAIL, 30/30 completed, no critical safety violations observed |
| FIX-01 documentary closure | CLOSED / OWNER ACCEPT / COMMITTED / PUSHED / CI PASS |
| FIX-01 production release | BLOCKED; not deployed |
| FIX-02 remediation | OPEN |

The main branch contains behaviorally unaccepted FIX-01 changes. Adding a product improvement to `main` and deploying its latest HEAD would not automatically be a permissible independent product release. A safe release boundary must be established first (Section 8).

Authority remains with the actual repository, `docs/STATUS.md`, `docs/DECISIONS.md` (including KHD-49–KHD-51), `docs/product/BACKLOG.md`, FIX-01 implementation and behavioral evaluation records, and the preserved external evidence. No earlier accepted milestone is reopened merely by adopting this strategy.

## 3. Delivery streams and limits on concurrent work

| Stream | Purpose | Work-in-progress limit |
|---|---|---|
| Product Delivery | Kitchen and Garden product improvements, usability, accepted capability releases | One active product work package |
| Intelligence Research | FIX-02 diagnosis, experiments, architecture decisions and intellectual capability increments | One active research/intelligence work package |
| Infrastructure & Operations | Stability, maintenance, backups, monitoring and authorized deployments | One operational package when required; operational priority takes precedence |

Garden belongs to Product Delivery, not a third permanent development stream. The WIP limits are ceilings, not targets for simultaneous execution. The Owner's cross-project limit remains in effect; House Helper and Rise and Shine already compete for Owner attention and agent capacity.

Parallelism means independent, authorized work and useful handoffs, not simultaneous modification of overlapping code. During independent review of a product package, a developer may work on an authorized research package if the workspace and dependencies are isolated. While research awaits a decision, product work can progress within its own approved scope. If both streams need the same component or reviewer, sequence the work, establish an explicit handoff, or pause the lower-priority package.

Research findings may inform a product package, but an unaccepted experiment cannot be imported directly into the production path. Product changes that affect research fixtures or assumptions require baseline/version updates and explicit reassessment of comparability; original evidence remains preserved.

## 4. Incremental Intelligence Development

The following are logical release objectives, not frozen implementation packages or a mandatory pipeline. AG-01 and subsequent evidence may justify merging, splitting or reordering them. Each implementation increment requires its own bounded scope and acceptance.

| ID | Increment | Target outcome | Illustrative product touchpoints |
|---|---|---|---|
| I-01 | Reliable Conversation | Resolve the observed FIX-02 defects: intent resolution, recipe fidelity, identity/capability integrity and natural Russian conversation; preserve successful behavior | Existing Kitchen interactions, repeat requests, clarification and topic switching |
| I-02 | Grounded Intelligence | Work reliably with authoritative objects and user context, including references, originals and verified computations | Recipes, profile, equipment, household measures |
| I-03 | Action Intelligence | Select and use permitted tools; execute operations with accurate results, authorization and failure handling | Favorites, shopping lists and other accepted operations |
| I-04 | Adaptive Intelligence | Handle multi-step tasks, richer memory, changing context and adaptive tool/orchestration choices | Cross-domain and long-running tasks, including Garden as a later use case |

I-01 must not become a demand to implement the complete preferred architecture C. If evidence supports a smaller reliable solution, that solution remains admissible. Conversely, implementation of a tool mechanism may be necessary earlier if it is required to resolve an observed defect. Architecture choice follows evidence rather than labels or release order.

The full FIX-02 remediation remains open until all required remediation acceptance conditions have been satisfied. A successful partial increment does not silently close the whole remediation program. The long-term I-04 horizon does not become a mandatory condition for delivering I-01 or Garden Alpha.

## 5. Product backlog and dependencies

Retain the existing product roadmap as a planning baseline while revisiting dependencies and seasonal priorities. The following sequence and dependency classifications summarize the Owner–Teya planning discussion; the latest repository backlog and actual code must be checked during separately authorized assessment. They do not establish task status or approved implementation scope.

| Prior planning order | Product direction | Preliminary architectural dependency / delivery approach |
|---:|---|---|
| 1 | HH-BRAND-01 | Unified identity; FIX-02 remediation and behavioral acceptance required for the blocked candidate |
| 2 | UX Package | Low or partial dependency for presentation/history improvements; conversational style changes need behavioral review |
| 3 | KH-F04 — Equipment | Medium; equipment/profile data and appropriate recipe use are potential grounded-intelligence integration points |
| 4 | KH-F01 — Household Measures | Partial; retain existing accepted mechanisms; continue only remaining scope, separating verified calculations from LLM interpretation |
| 5 | KH-SHOP-02 — Standalone Shopping Lists | Medium; standalone product function with explicit operation contract, potential early action-intelligence integration |
| 6 | KH-F02 — Concentrations and Dilution | Lower for verified computation; preserve safety and validation; no speculative unsafe calculations |
| 7 | Consistency Analysis | High; coordinate with FIX-02 diagnosis and behavioral evaluation |
| 8 | Profile Development | Medium; align with user-context and memory semantics |
| 9 | Culinary Memory | High; coordinate with Adaptive Memory research |
| 10 | KH-F03 — Preservation and Storage | Medium; distinguish verified domain safety from generative explanation; potential seasonal integration with Garden |
| 11 | Budget Helper | Future domain; use validated architecture knowledge when separately prioritized |

UX Package examples discussed include conversation titles, date/time presentation, history usability and full recipe presentation. The exact backlog scope must be verified; informal address in Russian is not automatically a presentation-only change.

KH-F04 is a candidate for practicing the distinction between an authoritative fact (available equipment) and a preference or requirement (whether to use it). KH-SHOP-02 is a candidate for demonstrating a complete tool-enabled action: understand a request, use an authorized operation, observe its result and report it truthfully. These are opportunities, not a commitment to a particular implementation or tool interface.

Existing operational items, including C7-P4 follow-up and PostgreSQL healthcheck noise, remain subject to their authoritative status records and separate authorization. They are not automatically part of FIX-02.

## 6. Garden: seasonal product priority

### 6.1 Product direction

Garden is the next priority module after Kitchen. It may enter Product Delivery before Kitchen is functionally complete. Priority derives from the mother's actual seasonal needs, not from a requirement to demonstrate a new module by an arbitrary date.

The proposed initial release target is **by February 2027**, earlier if needed for preparation and feasible after acceptance. This is a planning target, not a promised release date or a universal horticultural timetable. The required date must be refined using the user's actual location, crops, conditions and preparation schedule. If early preparation needs precede February, narrow the initial scope and reconsider product ordering rather than waiting for advanced intelligence features.

### 6.2 Proposed maturity stages (not scope locks)

| Stage | Illustrative capabilities | Boundary |
|---|---|---|
| Garden Alpha — season preparation | Advice on crop and variety selection, soil preparation, sowing and seedling care, suitability for local conditions, plant compatibility where supported | A useful initial advisory module; no mandatory live weather, photo analysis, calendar or proactive notifications |
| Garden Beta — planting and growing | Plot characteristics, lighting, soil, beds and greenhouses; planting plans, irrigation, fertilization and care | Separate specification and acceptance |
| Garden Intelligence — season support | Plant photo interpretation, weather-aware recommendations, work calendars, records and reminders | Future capabilities; no claim of current availability; safety, data quality and permissions required |
| Garden & Kitchen Integration | Harvest-aware recipe suggestions, storage, freezing and preservation | Coordinate with KH-F03; no unsafe improvisation for preservation |

Advice about growing conditions must distinguish known local/plot facts from generic recommendations and assumptions. High-consequence recommendations (e.g., chemical application or food preservation) require appropriate safety and reliable evidence. Do not claim live weather, image diagnostics, persistent records or reminders unless those functions actually exist and are available to the user.

### 6.3 Discovery before specification

Use the planned December 19 discussion with the mother, if it takes place, to gather Garden requirements alongside the existing visual-identity discussion. This is a proposed agenda addition, not a change to an existing reminder or appointment. If the seasonal timeline demands earlier discovery, the Owner may choose a separate conversation.

Topics: crops and varieties currently grown or planned; actual growing location; greenhouse, beds, soil, light and other known constraints; customary preparation dates; recurring information needs; common problems; desired calendar/reminders; willingness to keep planting records or share plant photographs. Determine which tasks would genuinely benefit from an initial module rather than inferring needs from the proposed maturity stages.

After discovery, separately approve a Garden product specification, smallest useful scope, knowledge/evidence requirements, module integration boundaries, evaluation and release conditions. Do not assume that the Garden Alpha needs an entirely new memory system, agent platform, or a full UI redesign.

### 6.4 One assistant across domains

Fedya remains a single digital home assistant. Kitchen and Garden are domain competencies, not separate personalities. Shared logical concerns include identity, capability truth, user context, memory governance, object integrity, permissions and tool access; no shared framework or physical component layout is mandated in advance.

Domain-specific data and safety requirements remain explicit. A future cross-domain interaction might connect a recorded harvest to meal suggestions and safe preservation methods. The user should not be forced to reconstruct context merely because the subject crosses a module boundary. Specialist interfaces may still be useful when justified by product needs.

Garden adds useful future validation of AP-02, AP-12, AP-19 and domain-transfer research: gardening tasks can span a season rather than a single cooking session. Garden is an architectural planning constraint and future practical validation opportunity, not an authorization to expand FIX-02 to production Garden implementation or to change an already frozen corpus. Any new synthetic Garden research scenarios require the applicable corpus/design approval; RC-A remains unchanged and RC-E remains independent.

## 7. Planning horizons and delivery cadence

### 7.1 Rough effort estimates

The estimates below are discussion-level ranges, not measured velocity or commitments. An active project day denotes a day of meaningful project activity, including agent work and Owner review, not a standardized eight-hour person-day. They are not automatically additive across concurrent work, and they cannot be converted to dates without capacity and dependencies.

| Research / delivery activity | Active project days |
|---|---:|
| Read-only architectural assessment | 1–2 |
| Analysis of causes and priorities | 1–2 |
| Research environment preparation | 2–4 |
| Investigation of the four defect directions | 3–6 |
| Initial Tool-Enabled Intelligence experiments | 3–5 |
| Comparative evaluation and independent audit | 2–4 |
| Architecture decision and scope lock | 1–2 |
| Indicative total for the principal research work | 13–25 |
| First architectural implementation, audit and acceptance (additional) | 5–10 |

Indicative calendar scenarios discussed: narrow remediation in 2–3 weeks if causes are local; first substantial intelligence release in 4–8 weeks; extended research in 2–4 months. These are low-confidence alternatives, not cumulative obligations. The broad program can continue after earlier accepted increments. Re-estimate after AG-01, experimental design and each substantial scope or capacity change.

### 7.2 Rolling eight-active-week horizon

| Active weeks | Product Delivery | Intelligence | Operational / release considerations |
|---|---|---|---|
| 1–2 | Prepare UX Package | AG-01 diagnosis and prioritize remediation | Complete priority maintenance; verify release isolation before any independent release |
| 3–4 | Independent UX improvements if accepted; progress KH-F04 | Bounded FIX-02 research | Review actual release candidate and dependencies |
| 5–6 | Progress remaining KH-F01 scope | Target I-01 if evidence, audit and acceptance permit | Integrate only accepted changes |
| 7–8 | Progress KH-SHOP-02 or seasonally prioritized Garden package | Initial grounded/tool research and next bounded increment | Replan with actual capacity and release status |

The week numbers represent active project weeks after applicable authorization and operational readiness, not fixed calendar dates. A Garden requirement may displace a planned Kitchen package, rather than exceed the Product Delivery WIP limit. Research gates, dependencies and quality take precedence over these targets.

### 7.3 Seasonal planning view

| Calendar period | Kitchen planning direction | Intelligence planning direction | Garden planning direction |
|---|---|---|---|
| December 2026 | UX Package; initial KH-F04 work if feasible | FIX-02 diagnosis | Requirements discovery |
| January 2027 | KH-F04; remaining KH-F01 work | Reliable Conversation | Product specification and module design |
| February 2027 | Progress KH-SHOP-02 as capacity permits | Grounded Intelligence | Target Garden Alpha; prioritize if season requires |
| March–April 2027 | Continue prioritized backlog | Tool & Action Intelligence | Seedling and planting support; plot context as approved |
| May–June 2027 | Concentrations and other approved improvements | Adaptive Intelligence research / increments as justified | Growing-season support |
| Summer–autumn 2027 | KH-F03 preservation and storage, if approved | Cross-domain interaction as justified | Harvest and Kitchen integration opportunities |

This is a seasonal orientation, not a concurrent commitment to all listed items. The Owner's planned leave from November 27 to December 13, 2026, existing project commitments, infrastructure and review capacity must be reflected in the next actual schedule. There is no assumed obligation to work during leave.

Aim for a useful accepted product improvement every 1–2 active project weeks where scope and evidence allow. An improvement may be bundled into a later release if no safe independent release boundary exists. Never force a deployment to satisfy cadence.

## 8. Release isolation and controlled integration

No specific Git strategy is selected. AG-01 should establish the actual divergence between production and `main`, shared-file and runtime dependencies, migrations, deployment/maintenance compatibility, and available controls. This is an extension of the proposed read-only assessment scope, subject to explicit authorization.

| Candidate strategy | Conditions to investigate |
|---|---|
| Sequential release from `main` | Appropriate if bounded remediation is accepted soon enough and all included changes are admissible |
| Isolated product release branch | Appropriate if the blocked candidate can be excluded and product changes can be developed or transferred safely on a verified accepted baseline |
| Feature flags or equivalent isolation | Appropriate only if existing or separately approved isolation is demonstrated across all relevant execution paths; a UI-only switch is insufficient |
| Isolated research workspace | Keep experimental scripts, tools, datasets and prototypes out of the production execution path; physical layout and Git treatment separately agreed |

The decision must consider merge/reintegration cost, duplicated work, migrations and compatibility, safety, CI, and rollback. Do not assume cherry-picking, reverting, branching or flags are safe before dependency inspection. Owner alone performs Git operations. No use of the live user database as an experiment source or workspace.

For any independently proposed release, establish the exact accepted baseline, included and excluded changes, dependencies, affected surfaces, required migrations and operational compatibility. The actual candidate (not an unrelated tested branch) requires applicable technical tests, CI, independent audit, behavioral/non-regression evidence, Owner acceptance and separate deployment approval. Where relevant, require a verified backup, migration rehearsal, rollback readiness, maintenance/purge/backup compatibility and production smoke checks in line with established project procedures. A successful CI run alone is not a release authorization.

For the blocked FIX-01 candidate or its remediation successor, preserve the frozen 30-case regression corpus and require the applicable agreed behavioral acceptance and additional evidence. Independent product changes must receive risk-proportionate evaluation on affected behavior and must not reintroduce a known blocked candidate through a different deployment route.

## 9. Governance addendum: package-scoped application of the Charter

This is the proposed explicit governance extension agreed in concept during the delivery discussion. It becomes operative only upon Owner documentary acceptance; it does not itself open any execution gate. Research Charter v1.0 remains unchanged as a historical and methodological baseline. All its safeguards, Stop Conditions, research integrity and evidence requirements continue to apply.

Apply AG-00–AG-06 to bounded research packages and implementation increments, rather than interpreting the entire research program as a single indivisible delivery prerequisite. AG-01 may establish shared baseline evidence for several later packages, provided that its continued validity is checked. AG-02–AG-04 may be scoped to a particular hypothesis, comparison or increment; applicability and omissions must be explicitly documented. Any provider call, including a judge call during AG-04, must be within approved provider/budget limits. No automatic gate skipping.

AG-05 can approve a bounded architecture decision and Scope Lock for an increment without resolving every research hypothesis in the full Charter. AG-06 remains separately authorized for that increment. Any unresolved mandatory prerequisite for the increment blocks it; unresolved research questions unrelated to that increment may remain open with recorded limitations. Independent audit, behavioral acceptance and separate production approval still follow.

Independent Product Delivery packages use their own approved product scope, implementation, audit and release process; they do not require closure of unrelated research. If a package introduces or depends upon a new intellectual mechanism, it must satisfy the relevant research, architecture and acceptance gates rather than bypass them under a product label.

Track RD-01–RD-15 as logical deliverables at the relevant package and program levels. Preserve hypothesis and Research Question traceability, mark unevaluated questions honestly, and accumulate RD-14 Transferable Knowledge Record as evidence becomes available. Program closure remains separate from increment closure. No favorable partial outcome implies that the entire FIX-02 program is complete.

For each work package, record at least: unique package identity; stream and intended user/research value; exact baseline and scope; exclusions; dependencies and affected files/surfaces once known; authorization and constraints; acceptance criteria and evidence requirements; responsible developer/reviewer; status and blockers; integration/release eligibility; and unresolved follow-up. This proposed operational record is a lightweight planning aid, not a new compulsory software system.

## 10. Roles, review cadence and reprioritization

| Role | Responsibility |
|---|---|
| Owner | Product priority, scope, budgets, gate authorization, Git operations and final acceptance/release decisions |
| Teya | Architecture, methodology, package planning, preparation of agent instructions after authorization, analysis and product evaluation |
| Claude Code | Authorized research, prototypes and product implementation; respect workspace and scope boundaries |
| Codex | Independent read-only audit; verify evidence and methodology without tuning the candidate under review |
| Cursor Agent | Separately authorized infrastructure and production operations |
| LLM-as-a-Judge | Auxiliary evaluation under approved rubric and budget; not release authority |

Review the rolling plan after AG-01, each package acceptance or blocking finding, and whenever Garden seasonality, maintenance, capacity or significant dependency changes require reprioritization. Keep the latest status/backlog authoritative in the repository through separately approved documentary changes. Avoid repeated audits of closed work without a concrete reason.

When tradeoffs arise, protect operational stability and mandatory guarantees first; then prioritize useful near-term product outcomes, seasonal deadlines and evidence necessary for the next increment. Preserve the research value of House Helper without making speculative future features a compulsory dependency for immediate usefulness.

## 11. Immediate next decisions and open items

1. Review and documentarily accept the Research Charter and this companion Delivery Strategy. Saving a file alone does not establish formal acceptance or commit status.
2. Complete or explicitly reprioritize VPS Maintenance; verify relevant operational checkpoints from actual reports. Do not presume the pending Kasm work or system update has been completed or authorized.
3. Separately authorize AG-01 when appropriate. Include a bounded dependency and release-isolation assessment alongside the Charter's diagnostic scope.
4. Based on evidence, define the first research package and the first independent product package, with explicit scopes and integration boundaries. UX Package is the preliminary product candidate, not an authorized implementation.
5. Gather Garden requirements and establish the smallest seasonally useful Alpha scope. Update product priority against actual preparation needs.
6. Re-estimate schedule, capacity and budget. Select any branch/workspace strategy and approve each subsequent gate separately.

Open items include actual repository dependencies; current backlog statuses; release-isolation mechanism; exact incremental boundaries; model and provider choices; budgets, thresholds and sample sizes; Garden requirements, geography and knowledge sources; interface/module integration; and operational readiness. No specific solution for any of these is asserted by this document.

| Decision / status | State at assembly |
|---|---|
| Incremental Intelligence Development and dual delivery streams | CONCEPTUALLY APPROVED |
| I-01–I-04 delivery objectives | CONCEPTUALLY APPROVED; boundaries subject to evidence |
| Garden next priority, before full Kitchen backlog if useful | OWNER DIRECTION |
| Garden Alpha by February 2027 | Planning target; not committed |
| Automotive Assistant | DEFERRED / STRATEGIC HORIZON |
| Research Charter | Saved locally by Owner; formal documentary acceptance not inferred |
| Delivery Strategy | READY FOR OWNER REVIEW |
| AG-01 and subsequent execution gates | NOT AUTHORIZED |
| Product implementation / release branch strategy | NOT AUTHORIZED / NOT SELECTED |
| FIX-01 Production Release | BLOCKED |
| VPS Maintenance | PRIORITY RETAINED |

**Delivery principle:** Do not wait for a fully intelligent Fedya before making him useful. Make him progressively more useful and more intelligent, with each release supported by evidence and each research result contributing to transferable knowledge.
