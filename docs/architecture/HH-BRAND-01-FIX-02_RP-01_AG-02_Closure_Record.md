# House Helper — HH-BRAND-01-FIX-02
## RP-01 / AG-02 — Documentary Closure Record

| Document control | Value |
|---|---|
| Work package | HH-BRAND-01-FIX-02 / RP-01 — Capability Truth & Context Authority |
| Deliverable | Consolidated AG-02 documentary closure: design scope, review history, remediation, verification, limitations, artifact custody and gate status |
| Date | 2026-11-26 |
| Version | 1.0 — documentary closure |
| Repository | `eliv1982/kitchen-helper-telegram-bot` |
| Documentation baseline | `main`, `b934397` (PP-01 UX product specification v1.0), Owner-supplied; no Git command was run |
| Research workspace | `C:\Users\eliv\AppData\Local\HouseHelper\Research\HH-BRAND-01-FIX-02\RP-01\` |
| Final independent verdict | `PASS WITH NON-BLOCKING NOTES — READY FOR AG-02 OWNER ACCEPTANCE` |
| Material AG-02 blockers | **None** |
| AG-02 status | **CLOSED / OWNER ACCEPT WITH NON-BLOCKING NOTES** |
| AG-03 status | **NOT AUTHORIZED / DEFERRED** |
| Intelligence research stream | **PAUSED BEFORE AG-03** |
| Product delivery stream | **ACTIVE / PRIMARY PRIORITY** |
| Owner decision | **KHD-53** |
| Architecture | **NOT SELECTED** |
| FIX-01 behavioral baseline | **24 PASS / 6 FAIL**; behavioral acceptance FAILED |
| FIX-01 production release | **BLOCKED** |
| Provider / judge calls during AG-02 | **0 / 0** |
| Formal document status | **READY FOR OWNER COMMIT** |

> **Authorization boundary.** This record is documentation only. It creates no source, test, prompt, migration, dependency, configuration or deployment change; it authorizes no AG-03 or AG-04 execution, no provider request, no judge call, no credential use, no server operation, no real-user-data access, no holdout creation and no production release. It performs no Git operation. The Research Charter v1.0 and the Incremental Delivery Strategy v1.0 are preserved unchanged.

---

## 1. Purpose and provenance

RP-01 is the bounded research package proposed by AG-01 to test **capability truth and context authority**: whether supplying reliable information about implemented capabilities improves capability truth, contextual understanding and conversational usefulness, and whether organizing the same facts as a distinct semantic section changes the effect without imposing unnecessary restrictions on the model's legitimate intelligence.

AG-02 was the design stage: offline corpus, candidate, evaluation, accounting, custody and verification design. It was performed in an isolated workspace outside the product repository and CI. It made no live provider call, used no credential, created no holdout content and executed no AG-03 or AG-04 work. The design was corrected through three remediation packages following independent review, and the final independent delta review returned `PASS WITH NON-BLOCKING NOTES — READY FOR AG-02 OWNER ACCEPTANCE` with no material AG-02 blockers.

On 2026-11-26 the Owner accepted the AG-02 design with non-blocking notes. This record consolidates that stage so later packages have a self-contained, citable artifact. It records what was designed, what was reviewed, what was corrected, what was verified and what remains outstanding. It records **no behavioral result**: every answer used in verification was a synthetic fixture, and no candidate was compared against another under live conditions.

**Historical status.**

| Artifact | Status |
|---|---|
| AG-02 design stage | CLOSED / OWNER ACCEPT WITH NON-BLOCKING NOTES |
| AG-02-FIX-01 | COMPLETE; preserved for provenance |
| AG-02-FIX-02 | COMPLETE; preserved for provenance |
| AG-02-FIX-03 | COMPLETE; current package at closure |
| R-01–R-05 | CLOSED offline |
| R-06 | DEFERRED to the mandatory AG-03 preflight |
| L-46 | RECORDED; correction mandatory before AG-03; **not repaired here** |
| AG-03 | NOT AUTHORIZED / DEFERRED |
| AG-04 | NOT AUTHORIZED |
| Architecture selection | NONE |
| FIX-02 implementation | NOT AUTHORIZED |

---

## 2. Original AG-02 design scope

AG-02 was scoped to design, entirely offline, a controlled comparison of three prompt conditions under a fixed model configuration:

- **`RP-01-A-v1`** — the frozen-style control: the effective prompt of the FIX-01 evaluation (12,595 characters), unchanged.
- **`RP-01-B-v3`** — the same control plus a minimal capability statement of ten sentences (S2–S11), appended as a block.
- **`RP-01-C-v3`** — the same ten sentences organized as a distinct, structured section.

The design package covered:

1. A synthetic development corpus derived from the frozen RC-A reference and extended with diagnostic, confirmatory-only and conversational cases, with every case traceable to a static implementation contract.
2. A capability-fact inventory (CF-01–CF-24) grounded in the implemented Web and Telegram behaviour, distinguishing what the application does, what the model does and what neither can do.
3. A candidate specification that separates the factual sentences from their organization, with exact block, effective-prompt and request-configuration identities.
4. An evaluation specification with global-integrity and directly-tested gates, three evaluation units and a written rationale for every verdict, including PASS.
5. A request-accounting model: 288 exploratory requests under a proposed hard cap of 300, and a 554-request preliminary confirmatory base before the holdout.
6. An offline budget-control design: a durable pre-dispatch reservation ledger with exact Decimal arithmetic, conservative token bounds, one-use authorization and fail-closed accounting.
7. An offline harness with fixture preflight, holdout rejection at the run entry, exact candidate verification, rendered sequential history, terminal status and non-zero failure exits.
8. Custody and blinding: a 14-slot independent holdout template, pair-local labels, blinded evidence references and a separate holdout-creation assignment.
9. Separation of AG-02 acceptance, AG-03 execution preflight, AG-04 confirmatory/holdout work and I-01 product release.

**Out of scope by design:** intent resolution (RP-03), recipe fidelity (RP-02), general Russian remediation (RP-04), native tools (RP-05), durable memory, model comparison, Garden, Automotive, standalone shopping lists, UI and any production change. RP-01 tests prompt/context-only capability truth; it neither selects nor rejects Alternative C.

---

## 3. Independent design review and remediation history

| Stage | Independent verdict | Result |
|---|---|---|
| AG-02 design review cycle | `CONDITIONAL PASS — TARGETED CORRECTIONS REQUIRED` (as recorded by the AG-02-FIX-01 and AG-02-FIX-02 assignments) | V-01–V-10 and R-01–R-06 tracked; remediation staged |
| AG-02-FIX-01 | Addressed V-01–V-10 | 20/20 synthetic checks PASS; package ready for targeted re-review |
| AG-02-FIX-02 | Addressed R-01–R-05; R-06 excluded as AG-03-only | T-01–T-05 5/5 PASS; v3 candidate set established |
| Final delta review | Required four localized corrections (FIX-01, FIX-02, FIX-03, DOC-01) | Addressed by AG-02-FIX-03 |
| AG-02-FIX-03 | F-01–F-11 11/11 PASS | Ready for final delta review |
| Final independent delta review | **`PASS WITH NON-BLOCKING NOTES — READY FOR AG-02 OWNER ACCEPTANCE`** | **No material AG-02 blockers** |
| Owner acceptance | ACCEPT WITH NON-BLOCKING NOTES | KHD-53; this record |

### 3.1 AG-02-FIX-01

Addressed the initial review's V-01–V-10 findings offline:

- immutable index of every result file and an explicit record of the historical B-v1/C-v1 identifier reuse;
- v2 candidates with S1 retired and S4/S6/S7/S10 corrected, bound by exact sealed block hashes and request configuration;
- reconciled corpus and evaluation contract, including A-010 restored to non-regression and two confirmatory-only coverage cases (B-19, B-20);
- evaluation workload and blinding procedure;
- corrected request accounting (288 exploratory; 554 confirmatory base before the holdout), with the optional D-condition arithmetic withdrawn;
- provider-independent reservation ledger;
- strengthened offline runner;
- 20-check synthetic suite: 20/20 PASS.

### 3.2 AG-02-FIX-02

Addressed R-01–R-05 offline:

- **R-01 request identity and provenance** — five canonical identities (candidate content, effective request configuration, frozen schedule, per-attempt request, transport) plus the pinned package identity; the entry point refuses to start unless every identity matches an independently supplied pin; the v3 manifest-digest discrepancy reconciled as far as preserved evidence allows.
- **R-02 capability truth** — S4, S7, S8 and S11 corrected; new immutable B-v3/C-v3 versions; every statement re-verified against static implementation contracts.
- **R-03 scoring and comparison** — B-01, B-16 and C-10 corrected; three evaluation units frozen (288 answer reviews, 216 interaction verdicts, 216 pairwise comparisons over complete matched interactions); pair-local labels and blinded evidence references.
- **R-04 accounting validation and event semantics** — strict integer/Decimal validation, finite monetary values, reservation at least the request completion limit, and distinct events/counters for reservation, dispatch intent, transport invocation, returned response, reconciliation, usable answer and completed interaction.
- **R-05 offline safety evidence** — descriptions corrected by evidence layer; T-01–T-05 added the checks the preserved suite did not exercise.

R-06 (live execution controls) was **not attempted** and remains an AG-03 prerequisite.

### 3.3 AG-02-FIX-03

Closed the final delta review's three localized defects and one documentary correction:

- **FIX-01 mandatory independent pin** — `run_offline` requires an independently supplied pin at every execution entry point, including execution with an authorization file. Omission, an empty file, a malformed file and a mismatch are refused before authorization consumption, reservation, transport invocation and run-directory creation. The runner never derives a pin from the package it checks.
- **FIX-02 strict reasoning-token accounting** — explicit `reasoning_tokens: null` is invalid accounting and is refused without releasing the unreconciled reservation. Absent, explicit null and integer zero are distinct. Unknown reasoning is never inferred as zero, and no visible-output breakdown is invented. Total completion accounting and all ceilings are unchanged.
- **FIX-03 correct verification assertions** — a bounded synthetic suite (F-01–F-11) proves the required assertions through the real runner and durable ledger, with side-effect absence asserted immediately after each refusal and before cleanup.
- **DOC-01 historical provenance qualification** — the claim that every historical v3 input hash matches the current files was corrected; four revised corpus/candidate files are identified and historical evidence is distinguished from current revisions. No historical run record was modified.

Final suite result: **F-01–F-11, 11/11 PASS.** The preserved FIX-01 suite reports 19/20 on the current runner because of L-46; the saved artifacts are unchanged.

---

## 4. Final independent T-01–T-05 results

The final independent review accepted the following results. They are **mechanism results under synthetic fixtures**, not candidate behavior.

| ID | Check | Result | Key evidence |
|---|---|---|---|
| T-01 | Pinned request identity accepted; candidate content, roles, configuration, tools declaration and manifest identity mutations rejected before a run directory exists | **PASS** | Each mutation rejected; no run directory created |
| T-02 | Planned-request admission and invocation | **PASS** | 288 planned → 288 invocations; 300 → 300; 306 under cap 300 refused; 288 under cap 287 refused; 301st reservation refused with zero invocations |
| T-03 | Failure after the transport is actually invoked and before reconciliation | **PASS** | Event order `reservation → dispatch_intent → transport_invoked → ambiguous → stop`; 1 invocation; 0 responses; 0 reconciliations; 6,000 completion tokens held |
| T-04 | Strict accounting and independent ceilings | **PASS** | Six malformed usage shapes refused; non-finite monetary values refused; six malformed authorizations refused; attempt/input/completion/monetary ceilings each refused; reservation 6,000 ≥ request limit |
| T-05 | Full tree digests and write containment | **PASS** | 317 product files and 100 frozen files with identical full path-and-content hash maps; every new file contained; execution-id traversal and path escapes refused; no marker written |

Supporting current suite: `validate` PASS (60 base cases, 2 confirmatory-only, 14 holdout slots, 288 exploratory, 554 confirmatory); `self-test` 15/15 PASS; `verify-fix03` 11/11 PASS; `verify` (T-01–T-05) 5/5 PASS; `tools/verify_manifests.py` OK after regeneration; `estimate --stage exploratory` 288 requests.

**Preserved-suite status:** the AG-02-FIX-01 suite reports 19/20 on the current runner. V-05 (interruption after reservation and before dispatch) is an AG-02-FIX-02 runner-ordering regression recorded as L-46, unrelated to FIX-03. V-19 passes after the manifests are rebuilt.

---

## 5. Closure of R-01–R-05 and deferral of R-06

| Finding | Severity / gate | Closure | Evidence |
|---|---|---|---|
| **R-01** Request identity and provenance | High · AG-02 | **CLOSED offline** | Five identities defined; per-attempt identity binds ordered roles/contents, settings, omissions, tools absence and transport; entry point verifies an independently supplied pin; T-01 |
| **R-02** Capability truth | High · AG-02 | **CLOSED offline** | S4 excludes text-only favorites containing recipe prose; S7 does not imply application ordering; S8 ties the Telegram button to an eligible stored structured recipe and scopes Web editing; S11 is an age exemption with an account-deletion caveat |
| **R-03** Scoring and comparison instructions | Moderate · AG-02 | **CLOSED offline** | One rule per corrected case; one comparison unit; 216 comparisons; pair-local labels; blinded references; custodian-only hashes and paths |
| **R-04** Accounting validation and event semantics | High · AG-02 | **CLOSED offline** | Strict rejection of booleans, fractions, strings, negatives and unknown fields; finite monetary values; reservation ≥ request limit; distinct events/counters; T-04 |
| **R-05** Offline safety evidence | High · AG-02 | **CLOSED offline** | Descriptions corrected by evidence layer; T-01–T-05 exercise pinning, admission/invocation, post-invocation interruption, ceilings, full tree digests and containment |
| **R-06** Live execution controls | High · **AG-03 only** | **DEFERRED — not attempted** | Remains a mandatory AG-03 preflight prerequisite |

R-06 is not an AG-02 acceptance condition. It is recorded in the AG-03 preflight register in §11 and must be satisfied before any live request.

---

## 6. Accepted experimental design

### 6.1 Candidates

| Candidate | Condition | Block chars | Block SHA-256 | Effective chars | Effective SHA-256 | Fingerprint |
|---|---|---:|---|---:|---|---|
| `RP-01-A-v1` | Frozen-style control | 0 | — | 12,595 | `bbb1b95c852cf6752a2203a6e98699e10031f1f5e8b03aae26ea72a4f0b3fcf4` | `a5379b5cfcd2d6696814727d179fd67ae017120657e51699e22b680e8b4b0846` |
| `RP-01-B-v3` | Minimal capability statement | 1,499 | `039342ac57d784b2fc3fdda38198b5353344e9f3629a489978d5c029141ea51f` | 14,096 | `5c0149812bc6d4b7e39d6e33946ea252d9603f62ce4b6fa405412f1a14d94a1b` | `948c9f51a6a59e1b7b162a941517bda05c7443cded9d727270ba761b38be4416` |
| `RP-01-C-v3` | Same facts, structured section | 1,630 | `9408bcfe964a4e9aecce6f81a3733101a379e0b53eb2cfbd38e335e5a74374d3` | 14,227 | `bb7054e874b3f7c210968f3baea9953dc7aa9cd6b8cd66241d14d96ae3f594ee` | `f9b2ee3c0f197705b9980882646dc464cc0fbf23cf07027be3ef69fb1c22c49b` |

B-v2/C-v2/D1-v2/D2-v2 remain bound as **superseded** and executable so the preserved AG-02-FIX-01 verification remains reproducible; they are not part of the AG-03 default package. B-v3 and C-v3 contain the same ten sentences; the structural delta is 131 characters (heading, group labels, bullets). This confounds organization with formatting to some degree and is recorded as L-11.

Candidate manifest SHA-256: `fe8c111a101ee63ae6810abd81ba10c4f89c4dd94d27bcdc22bf25418d802c7c`.

### 6.2 Approved S2–S11 wording

The Owner approved the following exact sentences as the research statement. S1 is retired; it is not renumbered. S2–S11 are identical in B-v3 and C-v3.

**S2** В разговоре можно придумать блюда и дать рецепт.

**S3** Полный рецепт можно сохранить в избранное.

**S4** Сохранённый список покупок можно создать только на сайте и только из полного рецепта, который приложение сохранило как рецепт, — из рецепта в ответе или из избранного; обычный текст и избранное, сохранённое как текст, не подходят, даже если в них есть описание рецепта, и это снимок рецепта.

**S5** Профиль и мерные настройки доступны на сайте.

**S6** Отдельного списка покупок, который можно вести и сохранять сам по себе, без рецепта, нет; заказа, доставки и связи с магазинами тоже нет.

**S7** Сам я не сохраняю рецепты в избранное, не создаю списки покупок и не проверяю, что сохранено: сохранять и проверять может человек в приложении. Заказывать я тоже не могу: такой возможности нет ни у меня, ни в приложении. Список, написанный в разговоре, остаётся текстом, а не сохранённым списком.

**S8** В Telegram под ответом с полным рецептом, который приложение сохранило как рецепт, есть кнопка «Сохранить рецепт»; открыть избранное можно командой /favorites. Редактировать сохранённые рецепты, заметки к ним и списки покупок можно только на сайте.

**S9** В Telegram можно прислать фото; на сайте фото видны в истории, но отправить фото с сайта нельзя.

**S10** Сообщения и фотографии хранятся до 365 дней и могут быть удалены раньше; постоянных предпочтений приложение не хранит; помнишь только недавнюю часть текущего разговора.

**S11** Избранное и списки покупок не удаляются по возрасту: они хранятся, пока человек сам их не удалит или не удалит аккаунт.

**Semantic departures from the Owner wording, preserved:** S4 adds the stored-structured-recipe and text-favorite exclusions; S7 splits model/user actions from the absolute denial of ordering; S8 ties the Telegram button to a stored structured recipe and scopes editing; S11 records the age exemption plus the account-deletion cascade. S2, S3, S5, S6, S9 and S10 are unchanged. No S1 or broad precedence cue was reintroduced.

### 6.3 Accepted parameters

| Parameter | Accepted value |
|---|---|
| Exploratory requests | **288 planned** |
| Proposed exploratory hard cap | **300** |
| Preliminary confirmatory requests, excluding holdout | **554** |
| Answer reviews | **288** |
| Interaction verdicts | **216** |
| Interaction-level pairwise comparisons | **216** (72 matched groups × 3 pairs) |
| Independent holdout slots | **14**, empty; holdout absent and unused |
| Automatic retries | **0** |
| Judge calls | **0** |
| Exploratory cases | 26 scenario definitions: RC-A 24/25; all RC-B; RC-C C-01–C-06 |
| Repetitions | 3 primary / 2 secondary, as scheduled |
| B-19 / B-20 | Confirmatory-only |
| Model configuration | `gpt-6-luna`, reasoning effort `low`, 6,000 max completion tokens, no tools, structured recipes on |

**These are accepted design parameters, not authorization to execute requests.** The exploratory and confirmatory figures are separate stages. The final execution freeze, independently controlled pin custody and monetary authorization remain pending.

---

## 7. Known limitation L-46

L-46 is a **non-blocking offline observability limitation**. It is recorded, not waived, and it was **not repaired during this documentary closure**.

The preserved AG-02-FIX-01 suite is not fully green on the current runner. V-05 (interruption after reservation and before dispatch) reports an empty `ambiguous` list because the AG-02-FIX-02 runner assigns `current_key` after the `after_reserve` hook. The preserved FIX-01 report (`results/offline-verification-ag02-fix01/report.json`, 2026-11-26T10:55) records V-05 PASS with a populated `ambiguous` list.

Independent verification confirmed the safety properties that matter for the reservation:

- the full reservation is retained;
- zero dispatch intents are recorded;
- zero transport invocations occur;
- zero reconciliations occur;
- the terminal state is **interrupted**;
- the exit code is **5**;
- ordinary same-workspace authorization reuse does not succeed;
- the unreconciled reservation is **not** erroneously released.

**Impact.** The preserved suite reports 19/20 rather than 20/20. Only the ambiguity annotation is missing; the reservation and isolation behaviour are correct.

**Cause and scope.** The regression was introduced by an earlier AG-02-FIX-02 runner-ordering change, before and independently of AG-02-FIX-03. The mandatory-pin and reasoning-accounting changes do not touch the hook ordering. T-03 covers the post-invocation interruption boundary and retains the reservation.

**Disposition.** Correction is **mandatory before AG-03**. It is not part of AG-02 acceptance and was outside the authorized documentary scope of this closure.

---

## 8. Accepted offline limitations

The full register (L-01–L-48) is preserved in the research workspace at `docs/RP-01_Evidence_Limitations.md`. The following clusters are accepted as part of the evidence and travel with any later result.

| Cluster | IDs | Accepted limitation | Consequence |
|---|---|---|---|
| Environment and method | L-01–L-05 | No provider call, application server, database, production suite, VPS or Git operation was used; running-container mapping remains open | No behavioral or production claim; production-specific statements remain unverified |
| Context path | L-06–L-09 | Manifest-assembled histories, not the production `build_context`; no channel metadata, application state or tools | Controlled-context test only; not production equivalence |
| Candidate and corpus | L-10–L-15 | Authored candidates, 131-character structural delta, bounded synthetic corpus, absent holdout, no per-sample PASS rationales in the frozen baseline | Results are conditional on this wording and corpus; independence is a procedure, not an accomplished fact |
| Model and provider | L-16–L-20 | Model identifier is not an immutable version; SDK version unrecorded; function-calling restriction unverified; no prices; no calibrated judge | No version generalization, no monetary precision, no judge execution |
| Evaluation | L-21–L-25 | Regex flags are not verdicts; Russian naturalness requires human judgment; sample size supports description only; hidden reasoning unobservable; exploratory stage selects at most one candidate | No statistical-significance or causal claim; no production readiness |
| Scope | L-26–L-30 | RP-01 does not address intent, fidelity, general Russian remediation, memory, tools, agents, model comparison, Garden, Automotive, standalone lists or UI; workspace outside CI; no real user data; AG-03 controls are proposals | No adjacent defect is fixed; no execution is authorized |
| Budget and isolation | L-31–L-37, L-42, L-43 | Local ledger is not provider-side spending control; socket guard is best-effort; no OS-level read-only mount or network namespace; interruptions are in-process exceptions | Write containment is hash-based; no hard-crash durability; provider enforcement belongs to AG-03 |
| Provenance and versions | L-34, L-38–L-41, L-44, L-45 | v1 results are not evidence for v2/v3; historical v3 manifest bytes unrecoverable; pin custody is not authenticated; later live turns are bound at runtime; 301 cannot be built as a full three-candidate plan; blinded packet is synthetic | Historical evidence is distinguished from current revisions; no reconstruction |
| Accounting | L-47, L-48 | Unavailable reasoning depth is not reconstructible; the pin is only as trustworthy as its custody | Unknown reasoning is recorded as unavailable, never inferred as zero; Owner custody and final freeze remain separate |

### 8.1 Historical manifest provenance limitation

The v3 run recorded candidate-manifest digest `ee870dc7798bd6a61479271ddaf64ca4c33bb27ea701db1593f1c95904966a4e`. `tools/build_candidate_manifest.py` was modified and the manifest regenerated **after** the run. The earlier manifest's exact bytes are not recoverable, and no serialization or plausible structural variant reproduces that digest. The limitation is recorded under L-38 and is **not repaired by reconstruction**.

What remains recoverable and reconciles:

- The v3 evidence records the candidate identities it actually used: every block character count, block SHA-256, effective character count, effective SHA-256 and fingerprint matches the current sealed manifest.
- Seven of the twelve `developer_fingerprint` entries still match the current files. Four were revised after the v3 run during AG-02-FIX-02:

  | Input | v3 recorded SHA-256 | Current revision |
  |---|---|---|
  | `corpus/rc_b_diagnostic.json` | `f0a215bc02ee…` | `07beac3389c3…` |
  | `corpus/rc_c_conversational.json` | `7711c23d5e89…` | `f97d81022c58…` |
  | `candidates/capability_facts.json` | `bff6d253dcfa…` | `c0dd924b7c2b…` |
  | `candidates/candidates.json` | `171651306a22…` | `cb08f3657863…` |

- The twelfth entry, `candidates/candidate_manifest.json`, is the container discrepancy above.
- The v3 ledger, evidence and samples are preserved.

The v3 values are historical evidence of the inputs that run used; they are not restated as the current revision. A future run binds the manifest and every input through the pinned package identity, not through a self-declared digest.

---

## 9. Artifact custody, counts and digests

### 9.1 External locations

| Artifact class | Exact location | State |
|---|---|---|
| RP-01 research workspace | `C:\Users\eliv\AppData\Local\HouseHelper\Research\HH-BRAND-01-FIX-02\RP-01\` | Preserved; outside the repository and CI |
| Current verification results | `…\RP-01\results\offline-verification-ag02-fix03\` | Current; 3 files |
| Preserved T-01–T-05 results | `…\RP-01\results\offline-verification-ag02-fix02\` | Preserved; 3 files |
| Preserved V-01–V-20 results | `…\RP-01\results\offline-verification-ag02-fix01\` | Preserved; 3 files |
| Preserved v3 dry run | `…\RP-01\results\offline-dry-run-v3\` | Preserved; 41 files |
| Preserved earlier dry runs | `…\RP-01\results\offline-dry-run\` and `…\offline-dry-run-v2\` | Preserved; 38 files each |
| Frozen FIX-01 evaluation artifacts | `C:\Users\eliv\AppData\Local\HouseHelper\Evaluations\HH-BRAND-01-FIX-01\` | Read-only; `manifest.json`, `rubric.md`, `run_evaluation.py`, `results\run-owner-approved-2026-11-25-a\`, `results\CONSUMED-owner-approved-2026-11-25-a` |
| Disposable verification copies | `%TEMP%\…` | Removed after assertions |
| One-use authorization markers | `…\RP-01\results\consumed\` | Operational |

The frozen FIX-01 artifacts were not modified, regenerated, relocated or uploaded.

### 9.2 Verified counts

The closure-time read-only recomputation `tools/verify_manifests.py` returned:

```text
OK artifacts=65 result_files=126
```

| Manifest | Declared count | Breakdown |
|---|---:|---|
| `docs/research_artifact_hashes.json` | **65 research artifacts** | Research artifacts excluding runtime results and the manifest itself |
| `docs/result_evidence_index.json` | **126 result files** | 38 + 38 + 41 + 3 + 3 + 3 across the six result directories |

This recomputation is a **local read-only check**, not an independent audit. Independence is established by the final independent review recorded in §3–§4.

### 9.3 Relevant manifest digests (SHA-256)

| Artifact | SHA-256 | Notes |
|---|---|---|
| `docs/research_artifact_hashes.json` | `836e74c2df4d9fd81b20f9c81270967c711cc619c2afee6ffe2aa279f64d7adc` | 65 artifacts; generated 2026-11-26T12:23:38Z |
| `docs/result_evidence_index.json` | `bafb223b23c1aa6877acabddd156fc214fd090476d9feac7bbba1aee305641f9` | 126 files; generated 2026-11-26T12:23:38Z |
| `candidates/candidate_manifest.json` | `fe8c111a101ee63ae6810abd81ba10c4f89c4dd94d27bcdc22bf25418d802c7c` | Sealed schema 3; active and superseded candidates |
| `results/offline-verification-ag02-fix03/report.json` | `ffada80f3ad050aa8646e730ce347a4309be667d72118fef1ca629e1b5b56bd7` | F-01–F-11, 11/11 PASS |
| `results/offline-verification-ag02-fix02/report.json` | `954a70df1a0c58f018f50541239c4d756f230106e9ab21d9c4b229846b8f9b88` | T-01–T-05, 5/5 PASS |
| `results/offline-verification-ag02-fix03/artifact_hashes.json` | `14ab27fca395e0be12dd9f2fe70ea0e844b26c74f504b221d2879c3d071bea2a` | Hashes of the current result set |

Every earlier result file remains indexed and unchanged. No earlier result directory was overwritten.

---

## 10. Evidence classification

The closure preserves the distinction between independently verified evidence, developer-reported evidence and Owner decisions.

| Class | Items | Treatment |
|---|---|---|
| **Independently verified** | Final independent delta review verdict; T-01–T-05 results; F-01–F-11 results; absence of provider/judge execution; L-46 safety properties (reservation retained, zero dispatch/invocation/reconciliation, interrupted exit 5, no authorization reuse, no erroneous release) | Recorded as verified evidence at the stated layer |
| **Closure-time read-only verification** | `tools/verify_manifests.py` → `OK artifacts=65 result_files=126`; manifest digests recomputed above | Local recomputation, not independent audit |
| **Developer-reported** | AG-02-FIX-01/02/03 reports; 20/20, 5/5 and 11/11 suite results as authored; `validate`/`self-test`/`estimate` figures; candidate and corpus content; harness mechanism descriptions | Recorded as the package's own report; not restated as externally verified |
| **Owner decisions** | AG-02 design acceptance with non-blocking notes; AG-03 deferral; research pause; product-stream priority; KHD-52 prompt-ceiling permission (earlier); preserved FIX-01 gate | Recorded as decisions, not as evidence |
| **Historical evidence** | Preserved dry-run and verification result trees; v3 recorded input hashes | Distinguished from current revisions; never rewritten |

---

## 11. AG-03 preflight register

The following prerequisites are **outstanding**. They are not implemented by this closure and must be satisfied before any AG-03 request.

1. **L-46 correction and targeted verification** — repair the `current_key`/`after_reserve` ordering regression and demonstrate V-05 with a populated `ambiguous` list and unchanged reservation retention.
2. **Owner-approved experimental package freeze** — freeze the exact candidate, corpus, evaluation, harness and schedule revisions.
3. **Independent pin custody** — an independently controlled trust anchor, supplied from outside the files it checks, with documented provenance and custody.
4. **Signed one-use execution authorization** — an exact manifest carrying candidate, corpus, rubric, harness and holdout-digest hashes, runtime identity, caps, prices and stop conditions.
5. **Protected authorization consumption registry** — durable, one-use, resistant to ordinary reuse and path traversal.
6. **Authoritative provider pricing and accounting evidence** — approved endpoint, model/version and service tier, context and completion limits, input counting, usage fields, current rates and currency, cache treatment, and billing for errors, cancellations and ambiguous timeouts.
7. **Verified input and completion token bounds** — enforceable bounds, not planning averages.
8. **Independently enforced request, token and monetary limits** — enforced outside the model's and the runner's discretion.
9. **Verified provider-side hard spending enforcement** — a soft budget or dashboard notification is not sufficient; propagation delay, in-flight exposure and shared usage must be established.
10. **Dedicated isolated and revocable research credential** — not the production key and not the revoked FIX-01 key; closure and revocation evidence required.
11. **Live runtime and transport controls** — a separately built live transport wired to real usage, with a documented retry/reconciliation rule; the base remains zero retries.
12. **Controlled filesystem and network access** — write containment and egress control stronger than the offline hash-based and in-process guards.
13. **Evidence custody and operator checkpoints** — write-once run directories, ledger location, shared consumption marker, reviewer access, secret scan and no-Git rule.
14. **Assigned blinded evaluation and adjudication capacity** — Reviewer/Teya capacity for 288 answer reviews, 216 interaction verdicts and 216 pairwise comparisons, with written rationales and dispute handling.
15. **Holdout absent and unused** — the 14 slots remain empty; holdout creation is a separate bounded assignment; the current review context has seen the development scenarios and must not author it.

R-06 remains the umbrella prerequisite covering items 3–12. None of these is authorized or implemented by AG-02 acceptance.

---

## 12. What this closure does not establish

- **No behavioral finding.** Every verification answer was a synthetic fixture. No statement about how `gpt-6-luna` would answer A-v1, B-v3 or C-v3 under live conditions is made.
- **No candidate superiority claim.** No candidate was preferred, ranked or shown to be more truthful, more natural or more useful than another.
- **No architecture selection.** RP-01 neither selects nor rejects Alternative C or any other architecture. It is one evidence stream.
- **No production readiness.** No production prompt, context path, application server, database, credential, billing or provider limit was exercised.
- **No resolution of adjacent defects.** Intent resolution (cases 1–2/10), recipe fidelity (21–22), general Russian remediation, durable memory, native tools, Garden, Automotive, standalone lists and UI remain open.
- **No holdout result.** The holdout is absent and unused; independent generalization is a future procedure, not an accomplished fact.

---

## 13. Project status

| Stream / item | Status |
|---|---|
| **RP-01 / AG-02** | **CLOSED / OWNER ACCEPT WITH NON-BLOCKING NOTES** |
| **RP-01 / AG-03** | **NOT AUTHORIZED / DEFERRED** |
| **INTELLIGENCE RESEARCH STREAM** | **PAUSED BEFORE AG-03** |
| **PRODUCT DELIVERY STREAM** | **ACTIVE / PRIMARY PRIORITY** |
| HH-BRAND-01-FIX-01 behavioral baseline | **24 PASS / 6 FAIL**; behavioral acceptance FAILED |
| HH-BRAND-01-FIX-01 production release | **BLOCKED**; `77e2bfd` not deployed |
| Architecture | **NOT SELECTED** |
| FIX-02 implementation | **NOT AUTHORIZED** |
| PP-01 UX product specification | **PRESERVED**; independent delivery scope unchanged |
| Research Charter v1.0 / Incremental Delivery Strategy v1.0 | **UNCHANGED** |

**Preserved production and deployment boundaries.** The serving image and production provenance recorded at A12 are unaffected by this closure. The running-container-to-`afd83cb` mapping remains open. No deployment, image build, migration, configuration or operational action was performed. The release gate KHD-50 remains in force.

**Preserved product scope.** PP-01 remains a separately scoped product-delivery item with its own acceptance criteria and release isolation requirements. It does not depend on AG-03 and is not enlarged by this record.

---

## 14. Decision record

| ID | Decision | Status |
|---|---|---|
| **KHD-53** | **HH-BRAND-01-FIX-02 / RP-01 — AG-02 design acceptance and research pause.** (1) The AG-02 offline design is **CLOSED / OWNER ACCEPT WITH NON-BLOCKING NOTES** on the final independent verdict `PASS WITH NON-BLOCKING NOTES — READY FOR AG-02 OWNER ACCEPTANCE`; there are no material AG-02 blockers. (2) The Owner accepts A-v1/B-v3/C-v3, the exact S2–S11 wording with S1 retired, the 288-request exploratory plan under a proposed hard cap of 300, the 554-request preliminary confirmatory base before the holdout, the three evaluation units (288 answer reviews, 216 interaction verdicts, 216 pairwise comparisons), 14 empty holdout slots, zero retries and zero judge calls, as **design parameters only**. (3) R-01–R-05 are closed offline; R-06 is deferred to the mandatory AG-03 preflight. L-46 is a non-blocking offline observability limitation whose correction is mandatory before AG-03 and is not repaired here. (4) **AG-03 is NOT AUTHORIZED / DEFERRED; the intelligence research stream is PAUSED BEFORE AG-03.** Final execution freeze, independently controlled pin custody and monetary authorization remain pending. (5) The **product delivery stream is ACTIVE / PRIMARY PRIORITY**; PP-01 and its independent delivery scope are preserved. (6) No architecture is selected, no FIX-02 implementation is authorized, and the FIX-01 behavioral baseline remains 24 PASS / 6 FAIL with production release **BLOCKED**. | **ACCEPTED — Owner-approved AG-02 acceptance with non-blocking notes (2026-11-26); no execution, provider, judge, credential, holdout, production or Git operation authorized** |

### Why / boundaries

- **Acceptance is not execution.** AG-02 acceptance approves a design to be executed later, under a separate authorization. It does not approve any provider request, spending, credential, holdout use or live run.
- **The research stream pauses before AG-03.** The next research step is the AG-03 preflight register in §11, not an experiment. Product delivery takes priority meanwhile.
- **L-46 blocks the live stage, not the design stage.** The reservation and isolation properties hold; only the ambiguity annotation is missing. Correcting it is mandatory before AG-03 because the preflight requires a fully green offline suite.
- **R-06 stays with AG-03.** Provider enforcement, credential isolation, signed authorization, pricing evidence and runtime controls cannot be established offline; accepting the design does not waive them.
- **No silent rewrite.** AG-02-FIX-01, FIX-02 and FIX-03 reports remain historical records. This closure consolidates; it does not amend their evidence. The Research Charter and Incremental Delivery Strategy are unchanged.
- **Not decided here:** any execution authorization; any architecture selection; any prompt, model, schema, API, test or deployment change; any holdout creation; any PP-01 scope change.

---

## 15. Documentation-only compliance statement

This closure was documentation-only.

- **Files created:** this record.
- **Files changed:** `docs/DECISIONS.md` (KHD-53), `docs/STATUS.md` (dated documentary entry and the closest-next-step annotation) and `docs/product/BACKLOG.md` (HH-BRAND-01-FIX-02 status reconciliation).
- No product source, prompt, test, configuration, migration, dependency, evaluation harness or candidate file was changed.
- No Git operation was performed, including `git status`.
- No network, provider or judge call was made; no credential was read.
- No AG-03 or AG-04 execution occurred; no holdout content was created or read.
- No application or test suite was executed. The only execution was the read-only manifest recomputation `tools/verify_manifests.py`, which wrote nothing.
- No VPS, production service or real user data was accessed.
- The frozen evaluation artifacts and every preserved result directory were not modified, regenerated, relocated or uploaded.
- The Research Charter v1.0 and Incremental Delivery Strategy v1.0 are unchanged.
- No unrelated untracked file was changed.

**Unresolved items preserved as limitations.** L-46 correction (mandatory before AG-03); the historical v3 manifest container digest is unrecoverable; pin custody is not authenticated; the running-container mapping remains open; the holdout is absent and unused.

---

**RP-01 / AG-02 DOCUMENTARY CLOSURE COMPLETE — READY FOR OWNER COMMIT**
