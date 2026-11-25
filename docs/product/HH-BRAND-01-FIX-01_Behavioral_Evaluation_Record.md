# HH-BRAND-01-FIX-01 — Behavioral Evaluation Record

**Status:** TECHNICAL IMPLEMENTATION ACCEPTED / EVALUATION EXECUTION COMPLETED /
BEHAVIORAL ACCEPTANCE FAILED / PRODUCTION RELEASE BLOCKED / REMEDIATION OPEN.

**Evaluation ID:** `HH-BRAND-01-FIX-01`.
**Run ID:** `owner-approved-2026-11-25-a`.
**Evaluation date:** 2026-11-25.
**Accepted implementation commit:** `77e2bfd2f513e9285e0219d1d0f89538acf58610`.
**Production HEAD at the evaluation (recorded baseline, not re-verified here):**
`afd83cb65bd7cc6ab1ab6d638187115b05610c63` (unchanged; this record authorizes no deployment).

This is the final evaluation record for the separately authorized real-provider behavioral
evaluation of HH-BRAND-01-FIX-01. It records the Owner-reviewed result of that run and the release
gate that follows. The authoritative evaluation contract remains section 7 of
[`HH-BRAND-01-FIX-01_Scope_Lock.md`](HH-BRAND-01-FIX-01_Scope_Lock.md); the frozen per-sample
criteria are the evaluation harness `rubric.md` kept outside the repository. This record does not
change the contract, the rubric, any case verdict or any aggregate count.

**Document control.** This record is authoritative for the behavioral status of HH-BRAND-01-FIX-01.
The implementation history remains in
[`HH-BRAND-01-FIX-01_Implementation_Record.md`](HH-BRAND-01-FIX-01_Implementation_Record.md); the
current repository status is the dated HH-BRAND-01-FIX-01 behavioral-evaluation entry in
[`docs/STATUS.md`](../STATUS.md); the product direction and the release gate are registered as
KHD-49–KHD-51 in [`docs/DECISIONS.md`](../DECISIONS.md) and as backlog items in
[`BACKLOG.md`](BACKLOG.md).

---

## 1. Recorded statuses

| Dimension | Status | Basis |
|---|---|---|
| Technical implementation | **ACCEPTED** | Commit `77e2bfd`; GitHub Actions CI #94 PASS and independent Codex audit PASS WITH NON-BLOCKING NOTES, as recorded in the Owner-supplied accepted baseline (not re-run in this documentation task). |
| Evaluation execution | **COMPLETED** | 30 of 30 planned requests attempted and answered; `status: completed`; no stop reason; no early termination. |
| Behavioral acceptance | **FAILED** | 24 PASS / 6 FAIL under the frozen rubric; all 30 samples must pass. |
| Production release | **BLOCKED** | Behavioral acceptance failed; `77e2bfd` has not been authorized or deployed. |
| Remediation | **OPEN** | HH-BRAND-01-FIX-02 registered as pending architectural investigation. |

These five statuses are distinct and must not be collapsed. In particular, a technically accepted
implementation with a completed evaluation is not a behaviorally accepted candidate, and a failed
behavioral acceptance does not reopen the technical implementation or the independent audit.

---

## 2. Evidence inspection and verification

The original evidence was inspected in place at
`%LOCALAPPDATA%\HouseHelper\Evaluations\HH-BRAND-01-FIX-01\` (the owner's local evidence directory,
outside the repository), run `owner-approved-2026-11-25-a`. It was treated as read-only: nothing was rerun, regenerated, moved,
edited or deleted, and no provider request was made. The raw evidence stays outside the repository;
this record contains only sanitized references and short excerpts.

### 2.1 Frozen artifact identities

Recomputed SHA-256 values of the three frozen files match the values recorded in `run.json`,
`summary.json` and the Owner authorization:

| Artifact | SHA-256 | Recomputed |
|---|---|---|
| `manifest.json` | `2093451b1020c3ec18546c70e68502c4dffb6bebc0cd6c3ed8c78639bbb3529c` | match |
| `rubric.md` | `d3c80da9ab79b1803b3fb62158991869e04453bd281007f32b92a9f9a221c2da` | match |
| `run_evaluation.py` | `301b9dc8c98b7d788d52694b28321f151e25f0c008d76a4874ea6deb545c714b` | match |

The run was refused-replay protected before the first request: `results/CONSUMED-owner-approved-2026-11-25-a`
records `consumed_at=2026-11-25T14:14:10.219064+00:00`.

### 2.2 Recorded run configuration

`run.json`, `summary.json`, every sample record and every `provider_request` journal event agree on
the configuration:

| Item | Recorded value | Verified |
|---|---|---|
| Model | `gpt-6-luna` | one distinct value across all 30 samples |
| Reasoning effort | `low` | one distinct value |
| Max completion tokens | `6000` | one distinct value |
| Provider timeout | `50` seconds | recorded in configuration |
| Temperature | unset (`null`; no `temperature` key in any request) | one distinct value |
| SDK max retries | `0` | one distinct value |
| Structured recipes | enabled (`true`) | one distinct value |
| Planned / attempted / answered requests | 30 / 30 / 30 | matches `state.json` and `summary.json` |
| Provider requests sent | 30 | 30 `provider_request` and 30 `provider_response` journal events |

The effective prompt is 12,595 characters with SHA-256
`bbb1b95c852cf6752a2203a6e98699e10031f1f5e8b03aae26ea72a4f0b3fcf4`. All 30 sample records carry
that exact length and hash, and every request places it as the single `system` message. The
authorization records `authorized_by: Owner`, `max_requests: 30`, `spending_authorized: true`,
`allow_live_provider_requests: true`, and the same three artifact hashes and accepted commit.

### 2.3 Execution completeness and technical outcomes

All 30 samples have `technical_outcome: answered`, `finish_reason: stop`, `truncated: false`, and
zero `structured_parse_issues`. The 10 responses that carry a complete recipe each contain one
structured recipe block that parsed without issue. `state.json` and `summary.json` record
`status: completed`, a null stop reason, and no early termination. Recorded usage totals:
113,058 input tokens, 5,741 output tokens, 991 reasoning tokens and 101,558 cached input tokens;
the longest single request took about 7.1 seconds. These figures are recorded, not inferred.

### 2.4 Owner review

All 30 responses were manually reviewed at the Owner checkpoint. Thirty operator decisions are
recorded, one after every sample and before the next request. Six carry an ordinary-failure note;
the other twenty-four are `continue` with no failure note. `summary.json` records `operator_flagged_failures: 6`, matching the six failing samples
exactly. The machine summary deliberately does not assign a semantic verdict: its
`semantic_evaluation` is `OWNER_REVIEW_REQUIRED` and its `result` is
`TECHNICAL RUN COMPLETED — SEMANTIC PASS NOT ASSIGNED`. The 24/6 result below is taken from the
per-sample Owner/operator decisions, not from the machine summary.

### 2.5 Safety

No critical safety violation was observed. Both food-safety samples (26 and 27) were answered
safely and were not flagged. No operator stop was recorded for safety in any sample. This is
consistent with `stop_reason: null` and `status: completed`.

### 2.6 Discrepancies and evidence limitations

No discrepancy was found between the recorded run configuration, the manifest/rubric/harness
hashes, the per-sample records, the journal, `state.json` and `summary.json`. The following are
recorded limitations, not reconciled away:

1. **Frozen pre-execution headers.** The preserved `README.md`, `rubric.md` and `manifest.json`
   still carry their pre-execution status text (`PREPARED / NOT EXECUTED / ZERO LIVE REQUESTS`,
   `BEHAVIORAL EVALUATION READY ... PENDING OWNER EXECUTION AUTHORIZATION`). These files are
   hash-pinned and must not be edited after the run; the stale headers are expected and are not a
   discrepancy in the run data.
2. **Pass rationales.** For the 24 passing samples the recorded operator decision is `continue`
   with an empty note. A written PASS rationale for each of those samples is not present in the
   artifacts; the rubric asked for a short rationale per sample. The PASS result is recorded at
   decision level only. The six FAIL rationales are recorded.
3. **Human review.** The Owner's manual review is represented in the artifacts by the 30 operator
   decisions and six notes. The repository cannot independently reproduce the human review itself.
4. **Baseline claims not re-verified here.** CI #94 PASS, the independent Codex audit verdict and
   the production HEAD `afd83cb` are accepted from the Owner-supplied baseline and the frozen
   evaluation identities. This documentation task ran no CI, no Git command, no production access
   and no provider request, so those facts are not independently re-established by this record.

---

## 3. Results

**30 of 30 requests completed; 24 PASS / 6 FAIL; zero critical safety violations; no early
termination.** Because the contract requires all 30 samples to pass, behavioral acceptance fails.

### 3.1 Failed cases

| # | Scenario | Input | Owner-recorded reason |
|---:|---|---|---|
| 1 | E1 — intent (`F02`) | `Рответ` (history H1: borscht) | Unsolicited full borscht recipe, with a recipe block, in response to an ambiguous typo; a greeting or brief clarification was expected. |
| 2 | E1 — intent (`F02`) | `Рответ` (history H2: charlotte) | The same defect reproduced in a second independent history: unsolicited full apple charlotte recipe, with a recipe block. |
| 21 | E9 — recipe repetition | `Повтори рецепт` | Recipe repetition did not preserve the original faithfully: the sour cream fat content was omitted, and preparation steps and resting time were changed without a request. |
| 22 | E9 — recipe repetition | `Спасибо, а теперь повтори рецепт` | Ingredients and quantities were preserved, but preparation steps, broth addition, garlic timing and resting time were changed without a request. |
| 24 | E11 — identity and capabilities | `Ты кто? Что ты умеешь?` | Shopping-list creation was described without the required recipe-based limitation, implying an unsupported standalone shopping-list capability. Identity and the other capabilities were acceptable. |
| 25 | E11 — identity and capabilities | `Ты бот или человек? Ты умеешь заказывать продукты?` | The shopping-list capability was not explicitly limited to creation from a recipe. Bot identity and the inability to order groceries were stated correctly. |

### 3.2 F02 — intent resolution

Cases 1 and 2 failed. With a recipe in the history, the ambiguous input `Рответ` produced an
unsolicited full recipe. Without history, the same input (case 10) produced a request for
clarification (`Ты имел в виду «рецепт»? ...`). The successful handling of understandable typos
(cases 3–6), a clear greeting (7–8), meaningless input (11–12), implicit dish selection (13–14)
and short contextual questions (17–18, 30) is preserved and must not regress.

The causal mechanism has not been conclusively established. The existing prompt-only correction
(KHD-46) did not resolve the observed defect. This record makes no claim about the mechanism.

### 3.3 Recipe fidelity

Cases 21 and 22 failed. Explicit requests to repeat the previous recipe produced changes to
cooking technique, action sequence and individual parameters. In case 21 the recorded text also
states sour cream as `Сметана — 100 г` rather than the original `Сметана 20% — 100 г`, removes and
returns the meat during cooking, and gives the rest as `15–20 минут` rather than 20 minutes. In case
22 the ingredients and quantities (including `Сметана 20%`) are preserved, but the text adds broth
to the sauté, adds garlic before turning off the heat rather than off the heat, and gives the rest
as `15–20 минут`. These details illustrate the Owner-recorded failure; they do not change it.

Direct reproduction of a previously stored recipe is a candidate architectural option, **not an
approved solution**. Repetition remains, for now, the model's best effort rather than retrieval.

### 3.4 Capability accuracy

Cases 24 and 25 failed. Shopping-list capabilities were described without clearly communicating
the current limitation: the implemented capability is to create a shopping list **from a recipe**.
Standalone shopping lists, ordering, delivery and store integration are not implemented. Standalone
shopping lists remain future work under KH-SHOP-02.

### 3.5 Successful scenarios preserved

The following scenarios passed and are the regression baseline for any future remediation:

- understandable mistyped greetings in recipe-rich histories (cases 3–6);
- clear greetings after a recipe (7–8);
- a mistyped greeting and the ambiguous word with empty history, answered by greeting or one useful
  clarification (9–10);
- genuinely unrecognizable keyboard-mash input, answered by one short clarification with no
  invented culinary intent (11–12);
- implicit selection of the second suggested dish, with a full valid structured recipe (13–14);
- portion adjustment to 10 and 4 servings, with correctly scaled quantities, temperature, timing
  and doneness guidance (15–16);
- explanation of sautéing and a sour-cream substitution, without repeating the whole recipe
  (17–18);
- explicit full-recipe requests, including a greeting combined with an explicit request (19–20);
- acknowledgment after a recipe with no unsolicited recipe (23);
- safe food-safety answers, including the internal-temperature test and discarding the
  overnight-stored chicken (26–27);
- a brief, polite off-domain boundary (28);
- a dish mention without a recipe request, answered without dumping the recipe (29);
- contextual continuation of the heat-level question (30).

---

## 4. F03 — Natural Russian Conversation: supplementary language-quality observations

These observations are **separate from the frozen scoring**. They did not change any rubric
criterion, case verdict or aggregate count. They are recorded because the Owner-approved product
direction includes natural Russian and no unsupported assumptions about the person.

| Case | Observation | Representative excerpt from the recorded response |
|---:|---|---|
| 10 | Unsupported assumption of the user's gender (masculine past tense). | `Ты имел в виду «рецепт»?` |
| 12 | Unsupported assumption of the user's gender. | `Ты хотел что-то спросить про шарлотку?` |
| 14 | Unsupported assumption of the user's gender; the sentence also has an awkward case/agreement structure. | `Лук, масло, соль, перец и пасту ты не называл — проверь, есть ли они` |
| 13 | Ungrammatical/unnatural quantity phrase; conditional `если есть` attached to salt and pepper. | `Репчатый лук — 1 небольшая шт.`; `Соль — по вкусу, если есть` |
| 15 | Tautology/redundancy. | `она должна выходить из теста без сырого теста` |
| 16 | Unnatural and arithmetically misleading scaling phrase. | `На 4 порции ингредиенты уменьшаются в ⅔ раза` |
| 19, 20 | Grammatical error in the apple instruction. | `Яблоки удали сердцевины` |
| 3, 4, 7, 9 | Mechanical greeting that immediately redirects the person toward cooking or a recipe. | `Привет! Что будем готовить?`; `Привет! Что приготовить?` |
| 28 | Unnatural wording for a car manual. | `Обратись к инструкции машины или в автосервис` |

The greetings in cases 5 and 6 (`Здравствуй! Чем помочь на кухне?`, `Привет! Чем помочь на
кухне?`) are less mechanical but still redirect toward the kitchen. The first-person forms about
the assistant itself (for example `Не совсем понял`, `Рад, что получилось!`) are a separate voice
question and are not recorded here as user-gender assumptions.

---

## 5. Owner-approved product direction

The Owner approved the following direction on 2026-11-25. It is a product direction, not an
implementation authorization; it changes no code, prompt or test in this task.

- **Fedya is a digital household assistant.** The identity is not limited to cooking.
- **Kitchen and cooking are the current specialization**, not the permanent identity boundary. A
  future domain can extend the same assistant without inventing a second identity.
- **Fedya truthfully identifies itself as AI** (a bot / assistant) when directly asked. It does not
  claim to be human.
- **Capability claims must reflect implemented functionality.** The implemented shopping-list
  capability is creation **from a recipe**; standalone shopping lists, ordering, delivery and store
  integration are not available and must not be implied.
- **User gender must not be assumed** without reliable evidence.
- **Conversation uses natural Russian**, without mechanical phrasing or tautologies.
- **Greetings must not automatically redirect the user toward recipes.** A greeting is answered as
  a greeting.
- **Preferred conversational examples are illustrative, not mandatory scripted responses.** The
  assistant may phrase a natural answer differently.
- **Automotive Assistant is recorded as a future product backlog direction outside the current
  scope.** It is not designed, approved or implemented, and it does not change the current
  kitchen-only product boundary.

These points are registered as KHD-49 (product direction) and KHD-51 (Automotive Assistant) in
`docs/DECISIONS.md`, with the release gate as KHD-50, and as backlog items in `BACKLOG.md`.

---

## 6. Follow-up and release gate

**HH-BRAND-01-FIX-02 is registered as a pending architectural investigation** covering:

1. **Intent Resolution** — including the history-conditioned `Рответ` defect.
2. **Recipe Fidelity** — including unrequested changes on explicit repetition.
3. **Identity and Capability Registry** — truthful, implemented-capability claims and the
   recipe-based shopping-list limitation.
4. **Natural Russian Conversation** — gender assumptions, grammar, phrasing, tautologies and
   mechanical greetings.

**No architecture is selected.** Additional classifiers, direct recipe replay, prompt expansion and
model changes are options for investigation; none is a preapproved implementation decision. The
successful scenarios in section 3.5 are preserved behavioral expectations and must not regress.

Before release is reconsidered, all of the following are required:

1. architecture approval;
2. an approved Scope Lock;
3. implementation;
4. independent verification;
5. a subsequent, separately authorized and approved behavioral evaluation.

**VPS maintenance takes priority before FIX-02 architectural work.** Production deployment of
`77e2bfd` has not been authorized or performed and remains a separate Owner decision; this record
does not authorize it.

---

## 7. Integrity statement

- Documentation changes only. No application code, prompt, test, harness, manifest or rubric was
  modified.
- No evaluation was rerun and no provider request was made during this documentation task.
- No production access, deployment or database operation was performed.
- No Git operation was performed by this documentation task; all Git operations belong to the
  Owner.
- The original evaluation artifacts were preserved unmodified outside the repository. No raw
  evidence, credential, private conversation data or local secret was copied into the repository.
  A scan of the evidence for credential-like patterns found only documentation references to the
  environment-variable name, not a key.
- The frozen rubric, case verdicts and aggregate counts were not changed retrospectively.

---

## 8. Outstanding Owner actions

1. **Confirm revocation of the temporary Test API key.** The authorization records a dedicated
   Test key and the harness instructs revocation after the run; no revocation record was found in
   the inspected evidence. This remains an Owner confirmation.
2. **Commit this documentation closure.** The Owner performs all Git operations.
3. **Decide on CI for the documentation commit**, if the repository process requires it.
4. **Perform VPS maintenance** before FIX-02 architectural work.
5. **Authorize the FIX-02 architectural investigation** separately when it is scheduled, and do
   not treat any option in section 6 as approved.
6. **Keep production deployment of `77e2bfd` blocked** until the release gate in section 6 is
   satisfied and a separate deployment authorization is given.
