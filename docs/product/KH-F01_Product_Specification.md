# House Helper / Kitchen — KH-F01 Smart Measures & Portion Scaling

**Product Specification — Consolidated Product Baseline v0.6 (B1.1 DECISIONS OWNER APPROVED — IMPLEMENTATION NOT AUTHORIZED)**\
**Date:** 12 November 2026\
**Scope:** KH-F01 only; v0.6 locks the owner-approved KH-F01-B1.1 product decisions OD-01–OD-03 and OD-05–OD-13, freezes the final technical contracts, and retains the deferred KH-F01-B1.2 consistency scope\
**Status:** D01–D19 approved as product decisions. KH-F01-A deterministic measurement core accepted internally (`c4f852c`). KH-F01-B0 and KH-F01-B1 are **RELEASED / OWNER ACCEPT** at `f175f773cb055ee9c6b0ff6951cf9612eb92b2c3`, with the documented limitations in §12. **KH-F01-B1.1 product decisions OD-01–OD-03 and OD-05–OD-13 are OWNER APPROVED** (v0.6), with **OD-03 fixed as NO AUTOMATIC INHERITANCE**; **OD-04 and OD-14 remain deferred to KH-F01-B1.2**. **KH-F01-B1.1 implementation is NOT authorized, NOT started, NOT migrated, NOT committed and NOT deployed.** v0.6 incorporates the independent Codex audit verdict **PASS WITH REQUIRED AMENDMENTS** (3 BLOCKER, 8 IMPORTANT, 1 MINOR) and the final owner decisions. B1.1 acceptance criteria AC58–AC62 and AC65–AC88 are approved product requirements; AC63/AC64 remain deferred to B1.2. None of them change AC01–AC57. No application code, test, migration, commit, push or deployment is authorized by this document.

## 0. Project baseline and authority

- Product: **House Helper**; Kitchen is the first module, served from the site root. The mom-facing interface, buttons, explanations and errors are **Russian**; brand remains in English.
- Production as reported by owner: `https://house.elivcloud.org`, `eliv1982/kitchen-helper-telegram-bot`, production release `f175f773cb055ee9c6b0ff6951cf9612eb92b2c3` (KH-F01-B1, RELEASED / OWNER ACCEPT); two accounts, data preserved. Repository `main` is at the same commit. The B1 release covers personal target measures and the documented limitations in §12; it does not include the owner-approved B1.1 scope in §15, whose implementation is not authorized.
- Previously released: KH-SHOP-01, KH-PROFILE-01, KH-PROFILE-01A-FIX-01; no reopening without new reproducible defect.
- Ops: first normal purge/backup timer runs on 11 November 2026 still need separate evidence; not part of this KH-F01 product work.
- Workflow: Lena + Teya decide product logic; Claude Code implements **only after approval**; Codex independently audits; Lena personally commits/pushes; Cursor Agent handles VPS/deploy; Lena performs owner smoke and final acceptance.
- This document reflects the owner's product decisions D01–D19 and prior desk review of public reference sources. Codex separately inspected local repository at the reported HEAD; no code/database/production changes, tests, live datasets or APIs were executed during that preflight. Production claims remain owner-reported. The B1.1 design was independently audited by Codex; its required amendments are incorporated in §15. In the v0.6 freeze the owner approved OD-01–OD-03 and OD-05–OD-13 (OD-03 = no automatic inheritance), kept OD-04/OD-14 deferred, and the deferred consistency scope is recorded in §15.19.

## 1. Goal and scope

**User need.** In a recipe, show familiar ways to measure a specified ingredient without kitchen scales (e.g. `40 г пшеничной муки (≈ … ст. л. без горки)`), while preserving the author's original quantity. Let the user scale recipe portions and request sensible alternatives when a mathematically correct quantity is awkward.

**In scope:** mass/volume/unit parsing; personal household-measure capacities and independent source/target measurement contexts (D18/D19); ingredient-specific household equivalents; verified and extensible ingredient-coefficient registry; serving-scale arithmetic; context-sensitive alternative suggestions; honest precision; graceful partial results; Russian Web UX and corresponding Telegram presentation where that recipe interaction is supported.

**Not in scope:** KH-F02 concentrations/dilution (especially vinegar); KH-PROFILE-02 configuration; HH-ALPHA-01 inferred preferences; KH-F03 food preservation/storage; purchasing/shopping-list redesign; appliance training. Do not rewrite the recipe storage model or authentication absent a demonstrated need.

## 2. Approved product decisions (D01–D19)

| ID | Binding product decision |
|---|---|
| D01 | g/kg, mL/L, tsp=5 mL, tbsp=15 mL, glass=200 or 250 mL only after identifying which is meant. Piece/clove/head/slice are countable, not automatically grams. Preserve `щепотка`, `немного`, `по вкусу`. |
| D02 | Combination UX: short equivalent inline in recipe; detailed alternative measures and explanation on request. Preserve original quantity. |
| D03 | Starter catalogue contains 15 agreed everyday ingredients; expand as new ones appear. |
| D04 | Controlled automatic addition: newly found ingredient may enter the catalogue; measurement coefficient becomes usable only after verified-source gates. |
| D05 | One authoritative primary source **with transparent measuring method**, or at least two independent and agreeing sources. AI output itself is never evidence. |
| D06 | Initial disagreement threshold **10%** between sources under identical measurement conditions. This is a consistency threshold, not expected real-life accuracy. |
| D07 | Round approximate alternatives to usable measures (whole tbsp/tsp, if needed 1/2 tsp). Rounding error at most **5%** relative to the unrounded calculated target; otherwise do not automatically show a misleading hint. |
| D08 | Proportionally scale quantified ingredients without silently rounding fractional eggs or pieces. Offer **«Предложить альтернативу»** button only where useful. Alternatives do not replace the computed quantity without user choice. |
| D09 | Decide scaling by ingredient's **role in this recipe**: a measured ingredient in dough scales; oil for greasing a pan and water for boiling often depend on equipment/technique. Preserve instructions instead of inventing quantities. |
| D10 | Return partial result when only some ingredients can be converted. Ask focused questions only when required; do not re-ask answered questions in the current recipe/dialogue. |
| D11 | Evaluate practical measurement reliability as well as numerical rounding. Critical small doses favor scales; even with a known coefficient, an approximate spoon equivalent must not imply unwarranted accuracy. |
| D12 | Source policy: NIST/FDA for measurement definitions/methods; USDA FoodData Central as a candidate evidence feed, not automatic blanket approval; other sources only after quality, suitability and usage-rights checks. No individual coefficient approved merely by adopting a source. |
| D13 | A scaled recipe can be saved **only on user action** as a **separate variant**; original favorite remains untouched. Shopping-list quantities must correspond to the chosen saved variant, not the original. |
| D14 | Scaling checks quantities both in ingredient lines **and** cooking steps; update clear matches, flag ambiguous or conflicting text, never present inconsistent saved recipes as fully validated. Do not scale cooking time or temperatures mechanically. |
| D15 | For serving ranges (e.g. `2–3 порции`) or unknown base servings, ask for a concrete base before calculating; reuse the choice in current context; no silent midpoint. |
| D16 | Support leveled/heaped spoon distinctions (only separately evidenced heaped coefficients); ¼, ½, 1½ measures; glasses 200/250 mL; discrete counts, package sizes only when declared, and quantity ranges without silently replacing them by a midpoint. Respect the user's chosen measurement form when practical. |
| D17 | After **all pre-approved and testable** source, conditions, consistency, rights, unit and contradiction checks pass, candidate coefficients may be auto-promoted without an owner click. If any gate is inconclusive, keep candidate inactive. Preserve evidence, version, suspension and revalidation; LLM is not the authority. No enrichment implementation is authorized yet. |
| D18 | System default glass capacity is **250 mL**. A user may independently configure a personal glass capacity in **50–1000 mL** inclusive. No personal glass capacity is preconfigured for any account; a missing personal setting uses the system default; removing the override restores it. A user's glass capacity never determines an unknown source-recipe glass capacity. |
| D19 | Personal household measures are **glass** (system 250 mL; 50–1000 mL), **teaspoon** (system 5 mL; 1–20 mL) and **tablespoon** (system 15 mL; 5–50 mL). Each is independently configurable and isolated by account; no personal values are seeded, hardcoded or migrated for existing users. Profile UI collects whole millilitres; no cross-field rule requires tablespoon capacity to exceed teaspoon capacity. Source-recipe and target-user measurement contexts are independent: personal capacities affect target measures only. Ordinary level source teaspoons/tablespoons use conventional 5/15 mL unless reliable source metadata establishes otherwise; heaped and ambiguous spoon measures remain unsupported as exact measures. Original recipes are never automatically modified. |

### 2.1 Decision relationships and limited supersession

D18 and D19 extend the approved baseline; they do not reopen D01–D17.

- **Limited supersession.** D18 supersedes the fixed `200/250 mL` glass rule of D01 and the `200/250 mL` glass enumeration of D16 **only for personal target (user-facing) household volumes**. D19 supersedes the fixed target values `tsp = 5 mL` / `tbsp = 15 mL` of D01 **only for personal target measures**. All other D01–D17 requirements remain in force unchanged.
- **Case 1 — legacy KH-F01-A behavior.** Existing KH-F01-A callers retain their original 200/250 mL glass restrictions and validation semantics. No legacy call gains a default glass size, accepts a nonstandard declared size, or otherwise changes meaning.
- **Case 2 — personal target measures.** Configurable target glass, teaspoon and tablespoon capacities follow the approved D18/D19 ranges (§3.3). These values affect target measures only; they never resolve or reinterpret a source recipe.
- **Case 3 — explicit source-recipe declarations.** With `allow_nonstandard_glass_sizes=True`, an explicitly declared source glass capacity may be any validated value, including nonstandard and fractional millilitre volumes (e.g. 237.5 mL), and is used exactly. With the legacy default (`False`), previous parsing behavior remains unchanged, including the 200/250 mL restriction.
- **Undeclared source glass.** An undeclared source glass is never resolved using a personal target setting or the system default; it requires clarification (§3.4).
- **No retroactive invalidation.** KH-F01-A's accepted behavior, public contract and acceptance criteria AC01–AC50 remain valid. The extension is additive and opt-in.

## 3. Household units and context

### 3.1 Dimensional rules

- **Exact within declared units:** 1 kg = 1000 g; 1 L = 1000 mL. These conversions do not depend on an ingredient.
- **Defined household volumes:** the system defaults are 1 teaspoon = 5 mL and 1 tablespoon = 15 mL. These define volume, **not gram weights**. A user's personal spoon capacities (D19) replace these values for that user's **target** measures only; ordinary level spoons in a source recipe stay 5/15 mL unless reliable source metadata establishes otherwise (§3.3–§3.4).
- **Glass:** in the legacy default (`allow_nonstandard_glass_sizes=False`), a source recipe's glass is 200 mL or 250 mL, as explicitly stated/confirmed, and an unspecified source glass is never defaulted. With `allow_nonstandard_glass_sizes=True`, an explicitly declared source capacity may be any validated value, including nonstandard and fractional millilitre volumes (e.g. 237.5 mL), and is used exactly (§3.4). The system default for a **target** glass is 250 mL; a user's personal capacity (50–1000 mL) replaces it for that account only and never resolves a source glass. If a recipe says `1 стакан` and the size matters, ask once: `Какой стакан: 200 или 250 мл?`; in extended mode also offer another valid volume.
- **Countable:** шт., зубчик, головка, ломтик. Mass equivalents require specific size/state evidence, not a universal `1 шт = X г`.
- **Fuzzy instructions:** preserve `щепотка`, `на глаз`, `немного`, `по вкусу`, no fabricated numeric conversion.

### 3.2 Critical incompatibility with US sources

US references usually define **1 US cup as approximately 240 mL** (NIST/FDA). It is **not** automatically a Russian 250-mL or 200-mL glass. The system must carry the original source measure with its volume. For particulate ingredients (flour, oats, salt), extrapolating a published US cup's mass to a tablespoon or a 250-mL glass is *not automatically allowed*: packing, leveling, granularity and preparation may change apparent bulk density. A direct measured tablespoon, or a validated bulk-density relationship with matching technique, is needed for automatic conversion.

### 3.3 Personal household measures (D18/D19)

| Measure | System default | Allowed personal range |
|---|---|---|
| Glass | 250 mL | 50–1000 mL |
| Teaspoon | 5 mL | 1–20 mL |
| Tablespoon | 15 mL | 5–50 mL |

- Each measure is independently configurable; the profile UI collects whole millilitres within the stated range.
- No personal value is preconfigured, seeded, hardcoded or migrated for any account. An account without an override resolves to the system default, and removing an override restores it.
- Settings are isolated by account.
- There is no cross-field restriction requiring the tablespoon capacity to exceed the teaspoon capacity.
- The system defaults (250/5/15 mL) are fixed product constants; a personal capacity changes that account's target measures only.

### 3.4 Source and target measurement contexts

D18/D19 make the source-recipe and target-user measurement contexts independent.

- **Source context.** A source recipe's own declaration governs its household measures. With `allow_nonstandard_glass_sizes=True`, an explicitly declared glass capacity may be any validated value, including values outside legacy 200/250 mL and fractional values such as 237.5 mL, and is used exactly with rational arithmetic. With the legacy default (`False`), previous parsing behavior is unchanged. A missing source glass capacity is never inferred from the system default or from personal settings; it requires clarification. Conflicting source declarations or reliable context metadata require clarification.
- **Clarification.** In extended mode, when a source glass capacity must be clarified, offer 200 mL, 250 mL and an option to specify another valid volume. With the legacy default, the 200/250-only prompt and parsing behavior remain unchanged.
- **Target context.** The target glass is the user's personal capacity, or 250 mL when none is set; target spoons are the user's personal capacities, or 5/15 mL.
- **Independence.** Personal capacities affect target measures only. They never interpret a source recipe, never resolve an unknown source glass and never override a declared source value. A user's glass capacity never determines an unknown source-recipe glass capacity.
- **Spoons.** Ordinary level source teaspoons/tablespoons use conventional 5/15 mL unless reliable source metadata establishes otherwise. Heaped, `с верхом` and ambiguous spoon measures remain unsupported as exact measures.
- **Originals.** Original recipe quantities and text are never automatically modified; target capacities are a calculation/presentation context, not a data rewrite.
- **US cups.** US cups remain distinct from Russian glasses of any capacity and are never silently read as a glass.

### 3.5 Backward compatibility with KH-F01-A

D18/D19 extend the requirements without retroactively invalidating accepted KH-F01-A behavior or its acceptance criteria (AC01–AC50).

- Legacy calls keep their current interpretation and restrictions: an unspecified glass is never defaulted, legacy callers accept only the 200/250 mL glass sizes, and mass↔volume conversion still requires an approved ingredient coefficient.
- The explicit nonstandard source-capacity parser policy is opt-in through `allow_nonstandard_glass_sizes`: `False` (the default) keeps legacy parsing and the 200/250 mL restriction; `True` enables explicitly declared nonstandard and fractional capacities. The default remains legacy-compatible.
- The extended public contract describes source and target measurement contexts (`MeasureVolumes`) and a target default resolver; the legacy `glass_ml` argument is not combined with these contexts, and the legacy interpretation remains available unchanged.
- No accepted KH-F01-A acceptance criterion is changed, removed or retroactively narrowed.

## 4. Core user experience

1. Recipe displays original numeric amount unchanged: `40 г пшеничной муки`.
2. Where a verified coefficient and a valid measurement method exist, append a **short** labeled household equivalent, e.g. `40 г пшеничной муки (≈ [calculated amount] ст. л. без горки)`; never use fabricated placeholder numbers in live UX.
3. On `А сколько это в ложках?`, give a more detailed conversion, assumptions (`без горки`, volume), possible alternatives and approximate status.
4. If the product/source/size is ambiguous, ask a single targeted question; do not block the rest of the recipe.
5. When a scaled result is awkward (e.g. `1½ яйца`), show `Предложить альтернативу` **as a contextual action**. On click, display no more than 1–2 options grounded in available details. The baseline `1½ яйца` remains visible.
6. Web button and Telegram inline action should have equivalent semantics where supported; a message-level action in Telegram must retain the selected recipe/ingredient and not inadvertently act on another recipe.
7. Changes in original serving count do not overwrite the original stored recipe unless the user explicitly requests an edit. Scaled quantities are a view/calculation, not silent data mutation.

## 5. Starter ingredient catalogue — 15 entries

1. Water
2. Milk (specify fresh milk type when needed)
3. Sunflower oil
4. Wheat flour (ordinary; distinguish other flours)
5. Potato starch
6. Granulated sugar
7. Powdered sugar
8. Fine table salt
9. Honey
10. Sour cream
11. Dry rice
12. Dry buckwheat
13. Dry semolina
14. Dry rolled oats
15. Cocoa powder

Names, synonyms and grammatical forms in Russian are supported (e.g. `мука`, `муки`; `столовая ложка`, `ст. л.`); a fuzzy name is not proof of the same physical product. An ingredient may be present in the catalogue **without an approved coefficient**. Do not claim all 15 are initially covered by verified coefficients: this requires a separate data qualification pass.

## 6. Registry design requirements (product-level, not an implementation architecture)

Maintain **conceptually distinct objects**:

- **Ingredient catalogue:** canonical name, aliases, variety, preparation/state, disambiguation needs. New entries may be detected from recipes.
- **Unverified measurement candidate:** source name/URL/identifier, fetch/verification date, source release, food identity, original quoted measure, exact source-cup volume when applicable, method (`без горки`, sifted, packed, etc.), mass, source independence/provenance, status/rejection reason.
- **Approved measurement coefficient:** unit-specific, source-backed amount, measurement conditions, review date/version, status (`approved`, `under_review`, `rejected`, `deprecated`) and traceable links to evidence.

A candidate must pass all gates before publication:

1. Product identity and state **match** recipe ingredient.
2. Source method and physical measure/volume are unambiguous; no assumption that US cup = 250 mL.
3. Evidence supports D05 (one transparent authoritative primary OR two independent sources). Two mirrors of a single original table count as **one** source.
4. With multiple same-condition measurements, discrepancy must be within D06's 10% threshold; otherwise no automatic approval. For a first technical calculation, use symmetric relative difference `|A-B| / ((A+B)/2) × 100`; for 3+ observations, check the worst valid pair. **This formula is an implementation proposal for audit, not a separately approved user-facing decision.**
5. No contrary high-quality evidence is ignored; when newly detected contradictions arise, suspend re-use until revalidation as appropriate.
6. Unit derivation is permitted only where the measurement methodology supports it. `cup → spoon` is not inherently valid for powders.
7. Source use/republication is permitted by licensing and access rules. No unauthorised bulk scraping or republishes.
8. All data are versioned and usable without exposing other users' private recipe texts. External lookups use a sanitized canonical **ingredient** query, not complete private recipes.

**Operational product behavior:** recipe response must not be delayed indefinitely by external lookup. For a newly encountered ingredient, continue with the original quantity and allow a candidate to be validated separately; never show unapproved coefficients as if trusted. This does **not** authorize a new unattended job, service or scheduler before implementation approval.

## 7. Verification-source register — desk review 10 November 2026

| Source | Verified property | Proposed role | Important limit / gate |
|---|---|---|---|
| **NIST Metric Kitchen**, `https://www.nist.gov/pml/owm/metric-si/metric-kitchen/metric-kitchen-cooking-measurement-equivalencies` | 5 mL tsp, 15 mL tbsp, 240 mL US cup (approximate household equivalencies) | **Accepted unit-definition reference** | Does **not** provide ingredient-specific gram weights. |
| **FDA Guidelines for Determining Metric Equivalents of Household Measures**, `https://www.fda.gov/regulatory-information/search-fda-guidance-documents/guidance-industry-guidelines-determining-metric-equivalents-household-measures` | Techniques for weighing solids/liquids and household unit definitions; 240 mL US cup | **Accepted methodology reference** | Guidance for US nutrition labeling, not a mass table for arbitrary Russian kitchen tools. |
| **USDA FoodData Central**, `https://fdc.nal.usda.gov/api-guide/`, `https://fdc.nal.usda.gov/Foundation_Foods_Documentation/` | Food portions are sometimes recorded with gram weights, measure descriptions/metadata; access via API and bulk files; data licensed **CC0** | **Eligible upstream feed for measurement candidates** | Not every record has the measure, method and provenance needed for approval. `Branded` manufacturer serving sizes are not equivalent to independently measured bulk density. API key is server-side; observe rate limits. Needs a per-product qualification pass. |
| **King Arthur Baking Ingredient Weight Chart**, `https://www.kingarthurbaking.com/learn/ingredient-weight-chart`, and flour method `https://www.kingarthurbaking.com/videos/baking-skills/how-to-measure-flour` | Ingredient weight chart (e.g. flour 120 g per US cup) and technique | **Manual/curated cross-check candidate** | Not an approved automatic ingestion feed until data-use rights and item-specific method are verified. Never equate its US cup with 250 mL glass. |
| **FSANZ Australian Food Composition Database**, `https://www.foodstandards.gov.au/science-data/food-nutrient-databases/afcd/data-files` | Published official food-composition data, exportable files | **Supplementary investigation only** | Do not automatically ingest as kitchen-volume coefficients; database suitability and attribution/share-alike licence need review before use. |

**Policy:** only NIST/FDA **unit/method references** are outright accepted as written. USDA is approved for **investigating and proposing** candidate measurements, not bulk auto-approval of every food entry. Ingredient coefficients and their initial 15 records are **not yet individually verified**. King Arthur and FSANZ do not silently become operational data sources.

## 8. Portion scaling and ingredient role

- If the original serves `P` and the user requests `Q`, scale explicit numeric quantities by `Q/P`. Preconditions: `P > 0`, `Q > 0` and both refer to the same serving notion. Unknown `P` → ask instead of guessing.
- Retain exact arithmetic result internally. Preserve fractional discrete quantities without automatically snapping to whole eggs/onions.
- `По вкусу` remains a qualitative direction. Quantified spices/salt are arithmetically scalable, with context-specific guidance rather than asserting the result is automatically ideal.
- Dough ingredients and measured liquids normally scale. Purpose-dependent cooking media (oil for frying, water for boiling, oil for greasing a pan) are not blindly multiplied; retain context and give an appropriate explanatory note.
- Temperature and cooking time are **not** multiplied by the portion factor. Size of vessel, batch thickness and cooking process are separate considerations.
- Button `Предложить альтернативу` appears only if an actual meaningful suggestion can be given; e.g. 1½ eggs may be prepared by lightly beating 2 eggs and using ¾ of their edible mass; an option involving egg sizes must be conditioned on matching weight/recipe tolerance, not presented as an equivalence by default.

## 9. Precision and no-guess behavior

- Display approximately measured household equivalents using `≈` and clear qualifiers (`без горки`, glass volume).
- Rounding gate **≤5%** applies to additional rounding to practical unit increments versus unrounded computed amount; it does not cover the uncertainty of the underlying food measurement.
- Prefer integer tbsp/tsp, then half-tsp where needed; avoid cumbersome decimal spoon counts. If no simple representation meets the gate, omit automatic shortcut and offer scales or explanation on request.
- D11 reliability override: small amounts of yeast/leavening or other texture/process-sensitive ingredients may need weighing even where a numerical spoon conversion would pass 5%. No false precision.
- No ingredient-to-gram coefficient from an LLM's own answer. If an authoritative coefficient is unavailable, show original g/mL; don't display a plausible but invented equivalent.
- Never use recipe-specific ingredient names, personal descriptions, saved recipes or private preferences from user A to populate an unfiltered visible catalogue for user B. Any shared dictionary stores generic canonical terms only.

## 10. Acceptance criteria — consolidated for owner review

**Units, recognition and context**

- **AC01** Mass g↔kg and volume mL↔L convert exactly, including fractional amounts.
- **AC02** `ч. л.` resolves to 5 mL and `ст. л.` to 15 mL as volumes; no implicit grams.
- **AC03** An unknown `стакан` prompts once for 200/250 mL when conversion requires it; choice is reused within relevant recipe context.
- **AC04** US 240-mL cups remain distinguishable from 200/250-mL glasses; no silent reuse.
- **AC05** Qualitative amounts like `щепотка` and `по вкусу` remain text, not fabricated numbers.
- **AC06** Canonical alias matching handles common Russian inflections without equating different flour/salt/oat varieties.

**Evidence and catalogue growth**

- **AC07** The 15 starter ingredients exist in catalogue, with source-verified coefficients only where independently qualified.
- **AC08** A new ingredient is recognizable/registrable without an approved measurement coefficient.
- **AC09** No candidate is published without source, source identity/date, measure conditions and D05 evidence.
- **AC10** Mirrored or circular sources do not count as independent corroboration.
- **AC11** Two valid independent readings with >10% discrepancy for equal conditions are not auto-approved; ≤10% permits further validation, not unconditional approval.
- **AC12** Source measurements for a 240-mL US cup are not silently treated as either 200- or 250-mL glasses.
- **AC13** Rejected/suspended/under-review coefficients never appear as verified live hints.
- **AC14** External source outage does not prevent normal recipe display or permit an invented conversion.
- **AC15** No recipe text or user-specific sensitive data leaks through shared ingredient records/external lookup; 2-account isolation is preserved.

**Recipe presentation, precision and UX**

- **AC16** Original ingredient quantity remains visible and unchanged beside any approximate equivalent.
- **AC17** A verified equivalent is short inline; detailed breakdown appears on user request.
- **AC18** Kitchen hint rounding error is at most 5% of the **unrounded converted quantity**, and output is practical (whole spoons / half teaspoon where appropriate).
- **AC19** A precision-sensitive small amount does not receive a misleading auto-hint, even if arithmetic rounding fits 5%.
- **AC20** Missing data for one ingredient does not block verified conversions for other ingredients in the same recipe; uncertainty is stated locally.

**Scaling and alternatives**

- **AC21** Scaling 2→3 portions applies ×1.5 to numeric dough ingredients and retains arithmetic fractional results (`1 яйцо` → `1½ яйца`), with no silent substitution.
- **AC22** `Предложить альтернативу` is contextual, optional, and offers only grounded plausible guidance; original 1½ eggs remains until user action.
- **AC23** `Масло в тесто` and `масло для смазывания формы` get different scaling behavior based on ingredient purpose.
- **AC24** `Вода для варки`, when amount is equipment-dependent, is not multiplied as if it were water in dough.
- **AC25** Cooking time/temperature do not automatically scale by portion factor.
- **AC26** Scaling with unknown original serving count requests only that missing number and uses it after the answer.
- **AC27** If the original user recipe is saved, merely viewing a scaled version does not overwrite it or saved shopping-list data.
- **AC28** Mom-facing UX uses Russian text for hints, button labels and errors. Web and supported Telegram recipe interactions preserve the same core rules.

**Release and workflow gates**

- **AC29** Tests must include positive and negative cases for same-volume/different-weight foods, variable cup sizes, source conflicts, incomplete metadata, small sensitive doses, fractional eggs, half-tsp rounding and two-account isolation.
- **AC30** Production changes are prohibited until the owner accepts this specification, a bounded implementation scope is agreed, Codex audit passes, and Lena performs the commit/push and owner smoke steps per project workflow.

**Additional acceptance criteria derived from D13–D17 and Codex preflight**

- **AC31** For the same source recipe, saving a scaled variant creates an independent favorite with its own identity; neither the original favorite nor previously created shopping lists are mutated.
- **AC32** Creating a shopping list from that variant uses the **server-validated scaled ingredients**, not the source recipe, and matches the quantities visibly chosen by the user.
- **AC33** Retries, favorite version checks, catalogue changes, and repeated list creation preserve snapshot semantics and idempotency; a later coefficient update cannot silently alter a saved result.
- **AC34** For scaling 2 → 4 portions, an instruction with an unambiguous `200 г муки` updates consistently to `400 г` together with the ingredient line; quantities in unrelated instructions remain unchanged.
- **AC35** When step-to-ingredient correspondence cannot be verified or quantities disagree, the system exposes the unresolved discrepancy and does **not** label the derived recipe fully validated or silently persist it as such.
- **AC36** For a `2–3 порции` base, 6 target portions prompts for 2 or 3 (or a valid precise base if applicable); resulting factors are ×3 or ×2, never an implicit ×2.4 midpoint.
- **AC37** A missing/invalid base count requires focused clarification; confirmed base count is not re-requested within current calculation context.
- **AC38** Quantities expressed with decimal comma, fractions `¼`, `½`, `1½`, and mixed spoons are parsed conservatively; ambiguous or unsupported amounts remain unchanged and marked unresolved.
- **AC39** Level vs heaped spoons never share a coefficient by default; heaped measurements appear only if individually evidenced. Quantity ranges are retained rather than silently averaged.
- **AC40** For `пачка/банка/упаковка`, a conversion requires explicitly known net content and units; the system never infers package capacity from the noun alone.
- **AC41** Source evidence is attached to any automatically promoted coefficient, recording source identity, original measured unit, product state, measurement conditions, verification/approval date, rule version and record version.
- **AC42** A candidate is never auto-promoted if source origin/independence, measurement comparability, license/access permission, or unit interpretation cannot be checked deterministically; pass/fail/unknown are distinct.
- **AC43** D06's 10% disagreement criterion is checked only across **comparable** measurements under equivalent conditions; a detected contradiction from credible evidence suspends or triggers review according to the approved policy.
- **AC44** Approved coefficients are the only values used by the runtime; missing, rejected, under-review, suspended or outdated versions are never silently used as verified.
- **AC45** The LLM may assist with extraction/explanation but cannot override a failing deterministic gate or invent a missing source or measurement condition.
- **AC46** Unavailable external sources or unsuccessful enrichment do not block chat, recipes, existing approved conversions, favorites, or shopping operations.
- **AC47** Common conversion core is used by Web and integrated Telegram; callback/recipe/favorite ownership and current-account bindings remain enforced.
- **AC48** Historic text-only recipes and saved favorites remain readable; new KH-F01 behaviors do not force destructive backfill or silently change data already stored.
- **AC49** The strict current recipe protocol remains backwards compatible. Changing its keys or ingredient schema requires a separately reviewed versioning/compatibility plan; don't add keys that make v1 validation reject recipes.
- **AC50** Public release smoke checks original/derived recipe consistency, shopping creation from a saved variant, Russian UX on desktop/iPhone, and source-suspension behavior for enabled enrichment.

**Additional acceptance criteria derived from D18/D19 (configurable household measures)**

- **AC51** System defaults are glass 250 mL, teaspoon 5 mL and tablespoon 15 mL. A personal value is accepted only within its approved range — glass 50–1000 mL, teaspoon 1–20 mL, tablespoon 5–50 mL — and is collected as whole millilitres in the profile UI; an out-of-range or non-integer value is rejected with a validation message and is never clamped, rounded into range or silently replaced by the default.
- **AC52** No account receives preconfigured, seeded, hardcoded or migrated personal household-measure values. A measure without a personal override resolves to its system default; removing an override restores the default; settings are isolated by account; no cross-field rule requires the tablespoon capacity to exceed the teaspoon capacity.
- **AC53** Personal glass/teaspoon/tablespoon capacities affect target measures only. They never determine an unknown source-recipe glass capacity, reinterpret a source spoon or override a declared source value; ordinary level source spoons remain 5/15 mL unless reliable source metadata establishes otherwise; heaped and ambiguous spoon measures remain unsupported.
- **AC54** With `allow_nonstandard_glass_sizes=True`, a source recipe that explicitly declares a validated glass capacity is converted using that exact capacity, including nonstandard capacities outside 200/250 mL and fractional values such as 237.5 mL, with exact rational arithmetic. With the default `False`, previous parsing behavior is unchanged; conflicting declarations or reliable metadata require clarification, and an unspecified capacity is never defaulted.
- **AC55** With `allow_nonstandard_glass_sizes=True`, when a source glass capacity requires clarification, the user is offered 200 mL, 250 mL and an option to specify another valid volume; no capacity is assumed before the user chooses. With the default `False`, the legacy 200/250 prompt is unchanged.
- **AC56** All conversions preserve exact `Fraction` arithmetic, the ≤5% practical-rounding gate and the ingredient-coefficient qualification rules. Volume-to-volume conversions never require an ingredient coefficient; mass-to-volume conversions require an approved ingredient-specific coefficient; original recipes are never automatically modified.
- **AC57** KH-F01-A callers keep their current behavior and restrictions without new defaults; `allow_nonstandard_glass_sizes` defaults to `False`, so nonstandard source-capacity parsing is enabled only by an explicit `True`. D18/D19 and AC51–AC57 extend AC01–AC50 without changing or invalidating any of them.

## 11. Codex read-only technical preflight: verified architecture (10 Nov 2026)

- Repo `main` HEAD `59d1b806099d86a0a2037e7f6a5ecd1ca9756010`, no tracked modifications. Existing untracked `.claude/` and `deploy/monitoring/` files left untouched. Latest repo migration definition is `0013_user_text_size`; production DB was not examined.
- `house_helper/core/recipes.py` uses `Ingredient.amount: str` and `Recipe.servings: str | None`; no existing numeric quantity or unit fields. Protocol v1 has strict keys.
- Model output is parsed before presentation by `StandaloneOpenAIBackend._reply_from()` (`house_helper/core/openai_chat.py`), and structured recipes + text are persisted through `complete_turn()` (`house_helper/persistence/turns.py`).
- `save_favorite()` (`house_helper/persistence/favorites.py`) and `create_list()` (`house_helper/persistence/shopping_lists.py`) use **independent snapshots**. `create_list()` reads source quantities from stored recipe, not a client-side changed ingredient list.
- The existing prompt asks the LLM to recalculate portions, but deterministic scaling, a coefficient registry, source acquisition and approval do not exist in inspected code.
- Web recipe UI is in `house_helper/web/static/app.js` and shopping draft logic in `house_helper/web/static/chat-logic.js`; integrated Telegram in `house_helper/web/telegram_chat.py`. A separate legacy `bot.py` entry path exists and may not share persistence. Runtime status of that legacy path was not verified.
- The tests were inspected but **not executed**. Production structured-recipes feature state and production DB were not inspected.

**Architecture consequence:** retain raw text, normalize conservatively in an additional layer, calculate against approved-only versioned values, and preserve server-side consistency of displayed/saved results. Do not infer exact schema, endpoint, migration or worker design solely from this preflight. A migration-free transient preview appears possible but is **not verified** as a final design.

### 11.1 KH-F01-B0 technical design review — approved (11 November 2026)

Read-only design work at repository `main @ c4f852c` (KH-F01-A accepted internally). Verified in the current core: `house_helper/core/measures.py` has no production callers; one glass context serves both source and target resolution; teaspoon/tablespoon are fixed at 5/15 mL; glass is 200/250 mL only; an unknown glass is never defaulted. The KH-F01-A suite (67 test functions / 195 cases on Python 3.12.10) passes.

**Approved design, not implemented:**

- one immutable measurement-context value `MeasureVolumes(glass_ml, teaspoon_ml, tablespoon_ml)` describing household capacities;
- validation ranges glass 50–1000 mL, teaspoon 1–20 mL, tablespoon 5–50 mL; invalid contexts are rejected at construction;
- fixed system constants 250/5/15 mL used by the target resolver;
- `resolve_target_measures(overrides)` applies defaults on the target side only and never to source resolution;
- source and target contexts are keyword-only additions to `convert_quantity` / `convert_text`; the legacy `glass_ml` argument cannot be combined with them;
- the nonstandard source-glass parser policy is opt-in (`allow_nonstandard_glass_sizes=True`); the default `False` keeps legacy parsing and the 200/250 mL restriction;
- no new status values are introduced.

**Status:** technical design **APPROVED**; implementation **NOT AUTHORIZED** — not started, not committed, not released, not deployed.

## 12. Bounded delivery plan — proposed, not implementation authorization

| Phase | Acceptance goal | Dependencies / release rule |
|---|---|---|
| **KH-F01-A — deterministic arithmetic core** | Strict mass/volume math, nominal 5/15 mL spoon volumes, 200/250 mL glass clarification, conservative quantity parsing and error statuses; pure calculation + unit tests | **COMPLETE — internal accept** at `c4f852c`; committed and not deployed; not a standalone owner-facing feature release. Its behavior and AC01–AC50 are the compatibility baseline. |
| **KH-F01-B0 — Calculation Core Extension** | `MeasureVolumes`; source/target measurement contexts; configurable household volumes; target default resolver; opt-in parser support for nonstandard declared glass capacities (`allow_nonstandard_glass_sizes=True`); validation and deterministic calculations; backward-compatible public contracts; focused tests and existing regression suite | **RELEASED / OWNER ACCEPT** as part of the B1 release `f175f773`; preserves legacy calls and AC01–AC50: default `False` keeps 200/250 parsing, personal capacities affect target measures only, and an undeclared source glass is never resolved from personal settings or the system default. |
| **KH-F01-B1 — Profile Integration** | «Мои кухонные меры» settings for personal **target** capacities; persistence and account isolation; default restoration; Web integration; integration with the B0 calculation contract; user-facing validation and clarification UX | **RELEASED / OWNER ACCEPT** at `f175f773`, with documented limitations: ingredient-list presentation only; no step-level hints; no source-measure provenance in generation; mass↔volume and ingredient coefficients remain B2; profile badge/field copy correction is deferred to B1.1. |
| **KH-F01-B1.1 — Source Measure Provenance and Step-level Measure Conversion** | Generated-recipe source declarations; honest handling of undeclared source measures; deterministic, explicitly-bound step amount detection and ingredient linkage; supplemental personal-measure hints in steps; safe Unicode/version-bound annotations; B1 copy correction; Web Favorites UX only | **PRODUCT DECISIONS OWNER APPROVED (v0.6) — IMPLEMENTATION NOT AUTHORIZED.** OD-01–OD-03 and OD-05–OD-13 approved, with OD-03 = **no automatic inheritance**; OD-04/OD-14 deferred to B1.2. Full design in §15; no implementation, migration, API change, commit or deployment is authorized. Depends on the released B0/B1 contracts and must not regress AC01–AC57. **Consistency analysis is not part of B1.1.** |
| **KH-F01-B1.2 — Recipe Quantity Consistency Analysis (deferred future scope)** | Cross-comparison of ingredient totals and step usage totals; additive consumption events; `balanced`/`partial`/`excess`/`near` and equivalent statuses; completeness and quantity-consistency reports; consistency API, UX and acceptance tests | **BACKLOG — DEFERRED.** Recorded in §15.19; **not** an implementation dependency of B1.1 and not authorized. OD-04 and OD-14 belong here. |
| **KH-F01-B2 — Qualified Ingredient Reference Data** | Verified ingredient-dependent coefficients; practical kitchen equivalents; reliability and uncertainty safeguards; initial curated reference baseline | Individual coefficients still pass the D05–D07/D11/D17 evidence gates before use; the current reference-data baseline remains research, not runtime data. |
| **KH-F01-C — scaling, actions, durable variants** | D08/D09/D13–D16; consistent ingredient list and step text, alternatives, serving-range clarification, server-validated separate saved variants, correct shopping-list snapshots | Before any release claiming scaling+save completion, test ownership, idempotency, catalogue versions, legacy recipes and both channels. |
| **KH-F01-D — controlled automatic enrichment** | Candidate fetch/evidence, deterministic verification, source independence, auto-promotion gates, suspension/revalidation and safe fallback | Separate authorization and security/ops review; D17 auto-promotion only after allowlist, test corpus and failure cases are independently audited. |

The B0/B1/B2 split replaces the former single F01-B phase: B0 is the calculation contract, B1 the profile experience, B2 the qualified data. Within B1, **B1.1** (source-measure provenance, step hints, Web Favorites UX; product decisions owner-approved, implementation not authorized) and **B1.2** (deferred consistency backlog) are further sub-increments. Phases can be independently reviewed and staged. **KH-F01 fully complete** only after committed in-scope requirements are shipped and accepted; partial releases must be labeled partial and never claim full D01–D19 completion.

**Release workflow:** approved scoped implementation prompt → Claude Code changes + tests, no automatic commit/push → independent Codex audit → Lena review and commit/push → CI → Cursor Agent controlled deployment → Lena Web/iPhone and supported Telegram owner smoke → ACCEPT.

## 13. Remaining engineering gates (not new product decisions)

1. Qualify individual starter coefficients with traceable evidence. There are **currently zero coefficients verified by this document**; source eligibility does not promote a value.
2. Define exact numeric 10% comparator (the draft suggests a symmetric denominator) and independence/circularity detection, including comparable conditions. Audit with positive/negative fixtures; do not treat the comparator as user-approved by implication.
3. Define versioned coefficient approval/audit events, withdrawal and deterministic promotion rules; candidate failure must never pollute the published registry. No new background job or external-source access before separate phase authorization.
4. Determine safe quantity/role parsing and ingredient↔step linkage with conservative unresolved states; protect cases where text is ranged, mixed-unit or qualitative.
5. Design variant-saving and server-side shopping transformation while preserving idempotency, ownership, old saved snapshots and recipe protocol v1. Inspect migration need rather than forcing it upfront.
6. Define minimum supported Web + **integrated** Telegram actions. Verify whether legacy standalone `bot.py` is running/within scope; do not assume it is.
7. Check any live feature flags and deployed data only via a separately scoped, approved read-only operational check. Do not treat Codex's local static findings as live verification.

**Out-of-scope reminder:** KH-F02 concentration/dilution, KH-PROFILE-02, HH-ALPHA-01, appliance instructions and KH-F03 remain separate.

## 14. Current authorization gate

D01–D19 **product decisions approved**. KH-F01-A is complete (internal accept, committed at `c4f852c`). KH-F01-B0 and KH-F01-B1 are **RELEASED / OWNER ACCEPT** at `f175f773cb055ee9c6b0ff6951cf9612eb92b2c3`, with the limitations recorded in §12. KH-F01-B2 remains not authorized.

This v0.6 revision is documentation only. It records the owner-approved **KH-F01-B1.1 product decisions** (OD-01–OD-03 and OD-05–OD-13; OD-03 = no automatic inheritance), freezes the §15 technical contracts, and retains the deferred **KH-F01-B1.2** consistency scope in §15.19. No implementation, migration, database access, background worker, commit, push or production change is authorized by it. The **next single action** is a separate owner authorization of a bounded **B1.1-A** implementation task; the approved decisions do not themselves authorize code. B1.2 is not a prerequisite for B1.1.

## 15. KH-F01-B1.1 — Source Measure Provenance and Step-level Measure Conversion

> **STATUS: PRODUCT DECISIONS OWNER APPROVED (v0.6) — IMPLEMENTATION NOT AUTHORIZED.**
> The KH-F01-B1.1 product decisions OD-01–OD-03 and OD-05–OD-13 are owner-approved; **OD-03
> is fixed as NO AUTOMATIC INHERITANCE**. The technical contracts in §15 are frozen for
> implementation planning. This section does not change AC01–AC57. No application code,
> migration, test, commit, push or deployment is authorized by it. Implementation begins only
> after a separate bounded owner authorization. The approved decision matrix is in §15.16.
>
> **v0.6 freeze.** v0.5 applied the independent Codex audit of v0.4: **PASS WITH REQUIRED
> AMENDMENTS** (3 BLOCKER, 8 IMPORTANT, 1 MINOR). Findings F01–F07 and F09–F12 are fixed
> inside B1.1; F08 (consistency semantics) is deferred to the separate **KH-F01-B1.2** scope
> in §15.19, not silently discarded. v0.6 records the owner's final decisions, including
> **no automatic inheritance** (OD-03) and **no Telegram step-hint implementation** in B1.1
> (OD-08). The B1.1 API contains **no** consistency fields.

### 15.1 Problem, scope and non-goals

KH-F01-B1 presents personal household measures for the **ingredient list** of a structured
favorite. It has three product gaps that this design addresses:

1. A generated recipe may say `1 стакан` without saying which glass, so neither the
   ingredient hint nor any step hint can be calculated. House Helper authored the recipe
   but omitted a fact the user cannot be expected to recover.
2. Preparation steps contain amounts (`500 мл воды`) but carry no personal-measure hint,
   even when the same amount is already converted in the ingredient list.
3. A step amount is free text. Linking it to the right structured ingredient — across
   Russian inflections, several amounts in one step, repeated use, temperatures, times,
   servings and unrelated numbers — is not reliable with regular expressions alone.

**In scope (owner-approved):**

- a generation contract that makes new House Helper recipes declare the capacity/meaning
  of any non-self-explanatory source measure, and a deterministic audit of that contract;
- preservation of that meaning through the existing generation → `message_recipes` →
  favorite-copy pipeline, without a migration and without retroactive changes;
- read-time detection of supported volume amounts inside preparation steps by a **new**
  span-level scanner; this is not existing B0 behavior (§15.2);
- conservative, deterministic, **explicitly local** linking of those amounts to structured
  ingredients;
- supplemental personal-measure hints beside the relevant step amounts, using the released
  B0/B1 arithmetic and rounding policy unchanged;
- repeated use of one ingredient across several steps (each linked amount is converted
  independently; no cross-step total is compared in B1.1);
- per-term source-capacity resolution (**declared text or temporary clarification only —
  no automatic inheritance**, OD-03) with a reported origin for every source term;
- safe, version-bound, non-overlapping original-text spans with explicit Python
  code-point → JavaScript UTF-16 handling;
- the small B1 copy correction (`Указано вами`, measure field labels);
- an additive, typed API shape and a migration-free data model.

**Non-goals (owner-approved):**

- ingredient-density or mass↔volume conversion (KH-F01-B2), including any coefficient for
  flour, sugar, oil, salt, rice and the other starter ingredients;
- portion scaling, scaled variants, alternative-offer actions and shopping-list changes
  (KH-F01-C);
- **cross-comparison of ingredient totals and step usage totals; additive consumption
  events; `balanced`, `partial`, `excess`, `near` and equivalent consistency statuses;
  completeness or quantity-consistency reports; consistency API, UX and tests.** These are
  the separate **KH-F01-B1.2** future scope (§15.19), not an implementation dependency of
  B1.1;
- proximity-only, single-candidate or "nearest mention" ingredient linking; salvaging a
  quantity out of a rejected expression;
- rewriting, normalizing or "fixing" a recipe, its amounts, its units or its steps;
- retroactively assigning capacities to existing recipes or saved favorites;
- a protocol-v2 JSON shape, new recipe keys or a new persistent provenance table (Option A
  is owner-approved; the structured alternatives are rejected for B1.1);
- an LLM call at read time, a new provider call, external source lookup or an enrichment
  job;
- a new morphological/ML dependency for Russian;
- **Telegram step hints. There is no Telegram step-hint implementation in B1.1** (OD-08);
  any future Telegram parity requires a separate scope.

### 15.2 Verified existing architecture and new B1.1 components

The design builds on the released B0/B1 code at `f175f773`. The following was verified in
the repository and is treated as the compatibility baseline.

| Component | What it already provides | What B1.1 may reuse |
|---|---|---|
| `house_helper/core/measures.py` | Exact `Fraction` arithmetic; whole-line `parse_quantity`; `Quantity`/`QuantityPart`; glass declaration in text; opt-in nonstandard source glass; `MeasureVolumes`; `convert_quantity`/`convert_text`; 5% `practical_round`; explicit statuses/reasons | The recognized number/unit vocabulary, declared-glass handling, exact conversion, rounding gate and statuses. B1.1 must **not** add arithmetic of its own. |
| `house_helper/core/recipes.py` | Protocol v1 with exactly four keys; `Ingredient.amount` is free text; `validate_recipe`; `render_recipe`; content fingerprint | Amount strings as the carrier of source declarations. No protocol key change. |
| `house_helper/core/openai_chat.py` | `RECIPE_PROTOCOL_PROMPT` appended only when structured recipes are enabled; one provider call | The place where the generation contract is expressed. No second call. |
| `house_helper/core/kitchen_prompt.py` | Shared Russian content rules, including "quantities are guides; write «примерно» where approximate" | The generation wording must remain consistent with these rules. |
| `house_helper/persistence/message_recipes.py` | Stores validated title/servings/ingredients/steps as JSON; amounts stay strings; `(message_id, position)` identity | Declarations survive storage with no schema change. |
| `house_helper/persistence/favorites.py` | Structured favorite is a **copy** of the validated fields; no link to the source message; edit re-validates; optimistic version | Declarations survive saving and editing; the copy remains independent. |
| `house_helper/web/favorites_api.py` | Additive `measures` block on GET/PATCH; `?source_glass_ml=50..1000`; failure confined to the block | The place to add typed step, provenance and analysis data additively. |
| `house_helper/web/measure_targets.py` | B1 adapter: target capacities from the session profile; declared source glass used exactly; clarification applies only to undeclared glass; mass is not converted | The conversion and presentation rules to reuse for steps. |
| `house_helper/persistence/profile.py` and migration `0014` | Three account-scoped, whole-millilitre target capacities; validated ranges; defaults resolved by B0 | The only source of target capacities. No change. |
| `house_helper/web/static/app.js` | Ingredient hint rendering, exact/approximate wording, source-glass chooser, source-glass draft lifecycle | The rendering and clarification patterns to extend to steps. |
| `house_helper/web/static/chat-logic.js` | Russian copy, fraction formatting, measure grammar, response normalization | The copy correction and step-hint formatting. |

**New B1.1 components (explicit; none exists in B0/B1).** The v0.4 text described some of
these as if the released parser already did them. It does not: `parse_quantity` accepts a
**whole line** as one explicit measurement and refuses a line with any unaccounted
character; `_scan_terms` is module-private and has no prose/span model. B1.1 therefore
introduces, and must test, the following new pure components:

| New component | Responsibility | Explicitly **not** reused from B0 |
|---|---|---|
| `step_scanner` | Split step prose into sentences/tokens with code-point offsets; find candidate amount expressions | No prose scanning exists in B0 |
| `amount_expression` | Tokenize one **maximal** candidate expression atomically and classify it (supported/rejected) | B0 parses a whole line, not a maximal span |
| `amount_tokenizer` | Number forms, unit forms, qualifiers, ranges, compound terms; reject the whole construct without salvage | B0's number/unit vocabulary is reused; its whole-line control flow is not |
| `ingredient_resolver` | Curated inflection/alias matching against this recipe's ingredient names | New; no morphology dependency |
| `explicit_binder` | Apply only the supported local quantity↔ingredient constructions | New; no proximity/single-candidate fallback |
| `span_annotator` | Build sorted, non-overlapping, version-bound code-point spans and exact text echoes | New |
| `provenance_auditor` | Purely descriptive per-term audit of stored ingredient amounts; a separate bounded recognizer for package net-content declarations | New; uses the whole-line parser for glass/metric terms only (it cannot represent `1 пачка (400 г)`) |

**Architectural constraints that bind the design:**

- **Protocol v1 is strict.** `core/recipes.py` accepts exactly `title`, `servings`,
  `ingredients`, `steps`; each ingredient has exactly `name` and `amount`. AC49 forbids
  adding keys that make v1 validation reject recipes. Therefore provenance must live in the
  existing `amount` string, not in a new key, for this increment.
- **Amounts and steps are bounded.** `MAX_AMOUNT_LENGTH = 100`, `MAX_STEP_LENGTH = 1000`,
  `MAX_INGREDIENTS = 100`, `MAX_STEPS = 50`. A declaration adds roughly 8–15 characters;
  the audit must refuse or report an overlong construction rather than truncate it.
- **Favorites are snapshots.** A favorite has no foreign key to the message recipe.
  Anything derived must be recomputed from the favorite's own fields.
- **One provider call per turn.** No read-time model call, no re-generation, no provider
  call from a GET.
- **Exact arithmetic only.** Fractions, no floats; the 5% gate is applied once.
- **No silent mutation.** A read or a conversion never writes a favorite, message recipe,
  ingredient, step, profile or shopping list.
- **No migration is preferred.** The design must prove that the feature works on the
  existing schema; a structural-provenance alternative is an owner decision, not a default.

### 15.3 Requirement 1 — generated recipe source measures

#### 15.3.1 Representation options

| Option | How provenance is carried | Cost / risk | Verdict |
|---|---|---|---|
| **A. Declaration in the amount text (OWNER APPROVED)** | `1 стакан (200 мл)`, `1 банка (400 г)` | No migration, no protocol change; survives copy/edit; human-readable; relies on the existing whole-line parser for glass/metric terms and a separate package-content recognizer for net content | **APPROVED for B1.1** |
| B. New protocol key / v2 (`source_measures`) | Structured field per ingredient | Breaks v1 strict-key compatibility (AC49); needs protocol versioning, migration, dual-read, new validation; larger change | Deferred; only if the owner requires machine-separated provenance |
| C. Sidecar table keyed by `(message_id, position, ingredient_index)` | Separate persisted map | Needs migration; becomes stale on favorite copy and edit; duplicates the amount; no benefit over A | Rejected |
| D. Prompt-only, no deterministic audit | The model is asked to declare | A model omission is invisible; contradicts "should explicitly define"; cannot be tested | Rejected |

**Decision (OWNER APPROVED, v0.6):** Option A. The declaration is part of the recipe the model authored,
is visible to the person, travels through the existing string fields and remains editable.
`core.measures.parse_quantity(..., allow_nonstandard_glass_sizes=True)` already accepts
`1 стакан (200 мл)`, `1 стакан 200 мл`, `1 стакан на 200 мл` and fractional capacities such
as `237,5 мл`; B1 already uses that mode. The parser is applied to the **amount string**,
not to step prose.

#### 15.3.2 Generation contract (proposed prompt addition)

The structured-recipe prompt addition gains a **content rule** (not a JSON-schema change):

> Когда указываешь стакан или другую меру, объем которой не задан самой единицей,
> укажи ее объем или вес сразу после единицы в скобках: «1 стакан (200 мл)»,
> «1 банка (400 г)». Обычные чайная и столовая ложки без горки — 5 и 15 мл, их объем
> указывать не нужно. Когда меру выбираешь ты сам, не пиши «стакан» без объема. Где это
> естественно, предпочитай точные миллилитры и граммы. Если пользователь сам продиктовал
> рецепт и объем не назвал, не придумывай его: оставь как в исходном тексте.

Clarifications:

- **Model-authored recipes.** When House Helper creates a recipe, it is the author of the
  capacity and may state one. This is not a factual claim about an external source and is
  not evidence for a B2 coefficient.
- **User-transcribed recipes.** When the model is merely reformatting a recipe that the
  user supplied (including a family recipe), it must **not invent** a capacity the user did
  not give. The undeclared measure stays undeclared, and the read side explains the gap.
  The prompt says this explicitly.
- **Level spoons.** 5/15 mL are defined nominal volumes, so a declaration is unnecessary.
  Heaped/ambiguous spoons remain unsupported and should be expressed in grams instead.
- **Non-self-explanatory measures (owner-approved fence).** The declaration requirement applies
  to a glass, a cup, a jar/package/bag whose net content matters, and any other household
  measure whose metric meaning is not fixed by the unit. Countable pieces (`2 яйца`,
  `1 луковица`) do not need a capacity; qualitative terms (`щепотка`, `по вкусу`,
  `на глаз`) remain qualitative. A US cup is never relabeled as a Russian glass; if the
  model uses it, it must state millilitres or prefer metric units.
- **Length.** A declaration must keep the amount within `MAX_AMOUNT_LENGTH`; the prompt
  should prefer a short form (`1 стакан (200 мл)`, not a sentence).

#### 15.3.3 Deterministic validation (audit, not rejection)

A pure audit function — proposed as `audit_source_provenance(ingredients)` in the core —
runs over the validated amount strings. It uses the extended parser for glass and metric
terms, and a **separate bounded package-content recognizer** for package terms: the
whole-line parser classifies `1 пачка (400 г)` as `unsupported/package` and returns no
parts, so it cannot be the carrier of a package declaration. It returns, per ingredient
position and term index:

- `declared`: either a glass term with an explicit mL capacity, or a package/jar/bag with
  an explicit net content. A glass capacity is recorded in `capacity_ml` (including a
  fractional one); a package net content is recorded in `declared_content` with its own
  unit (g/kg/mL/L). Origin `declared_text`;
- `undeclared`: a glass term has no capacity in its own text and no temporary
  clarification; there is **no automatic inheritance** (§15.3.6);
- `invalid_capacity`: a declared glass capacity is outside the approved 50–1000 mL fence
  (the parser already refuses it);
- `package_content_missing`: a package/jar/bag is used with no net content;
- `unsupported_other`: a unit or form B1.1 cannot convert (US cup, heaped spoon, range);
- `not_applicable`: a metric amount, an ordinary level spoon (its 5/15 mL is a unit
  definition, not a declaration), a countable amount or a qualitative phrase. These are
  audit-internal and are **not emitted** in the API's `provenance` arrays (§15.8.2).

The audit is **descriptive** and idempotent. It does not rewrite an amount, does not add a
capacity or net content, and does not make the recipe invalid merely because provenance is
incomplete (OD-02). For a **newly generated** recipe the UI may show a neutral explanation;
for an existing or user-supplied recipe the same status is evidence that no fact was
invented. The audit reports what the **stored text says now**; it does **not** reconstruct,
verify or claim a historical external source for that declaration (§15.3.7).

#### 15.3.4 Generation-to-favorites pipeline

1. The model writes the structured block; the content rule asks for declarations.
2. `core.recipes.parse_reply` validates schema, lengths and control characters; amounts are
   stored as strings.
3. `openai_chat` renders the person-facing text and carries the validated `Recipe` objects.
4. `persistence.message_recipes.record_validated_recipes` stores the amount strings.
5. `persistence.favorites.save_favorite` copies the amount strings into the favorite.
6. `GET /api/favorites/{id}` parses the copied amounts and derives the measure block.
7. An edit goes through `core.recipes.validate_recipe` and is stored as entered; the
   derived block is recomputed. The server never adds or removes a declaration on an edit.

No declaration is synthesized at any step. A historical row without one stays undeclared.

#### 15.3.5 Existing recipes and no retroactive invention

- An existing recipe or favorite with `1 стакан` and no declaration keeps exactly that
  text. No capacity is inferred from the system default, the person's profile, another
  ingredient or the number 200/250.
- The recipe remains readable. Ingredient conversion reports
  `clarification_required` (`GLASS_SIZE_REQUIRED`) and steps that depend on that glass show
  no hint until the person supplies a temporary clarification.
- A clarification is a **per-request calculation context**, never persisted into the
  recipe, the favorite or the profile. It never changes the stored amount.

#### 15.3.6 Per-term capacity resolution — no automatic inheritance (OD-03)

Capacity is resolved **per term**, never once per recipe. The order is fixed:

1. **A capacity declared on that exact term** (`1 стакан (200 мл)`) always wins, whatever
   any clarification says. Origin: `declared_text`.
2. **A temporary clarification** (`?source_glass_ml=...`) applies only to terms that have no
   declared capacity. Origin: `temporary_clarification`.
3. Otherwise the term is `clarification_required`; no capacity is chosen and no hint is
   shown. Origin: `unresolved`.

**OD-03 (OWNER APPROVED) is NO AUTOMATIC INHERITANCE.** An undeclared glass in a recipe
step must **not** inherit capacity from a linked ingredient, even when that ingredient
declares exactly one glass capacity. Every step term is resolved only by an explicit
declaration in that term's own text or by a temporary user clarification. The binder must
never silently infer a capacity for a step.

Consequences that are part of the contract:

- Independent explicit declarations in one ingredient or recipe are preserved. A term with
  its own capacity is never replaced by a blanket context; a recipe-level capacity is never
  invented.
- A linked ingredient's declared capacity has **no** effect on a step term. A step
  `Добавьте 1 стакан воды` stays `clarification_required` even when the ingredient says
  `Вода — 1 стакан (200 мл)`, until the person supplies a temporary clarification for this
  view.
- A term with competing declared capacities elsewhere in the recipe is not inherited from
  either of them; it may use a temporary clarification, and without one the result is
  deterministically `clarification_required`, never `ambiguous`. `ambiguous` is reserved for
  competing **ingredient candidates**, not competing declared capacities.
- A temporary clarification never overrides a declared value.
- An ingredient-line conversion and a step-span conversion may therefore use different
  origins in the same request; each **source term** reports its own `capacity_origin`
  (§15.8.2).

#### 15.3.7 Declared text, historical provenance and temporary clarification

B1.1 keeps three concepts strictly apart. They are not interchangeable and must not be
presented as if they were.

| Concept | Carrier | Lifetime | May resolve a source capacity? | Historical-source claim |
|---|---|---|---|---|
| **Declared text** | The exact stored `amount` string in a message recipe or favorite | Persisted with the recipe/favorite; changes only on an explicit edit | Yes, for the term it belongs to | **None.** It records what the recipe text says, not where the fact came from or whether it is true. |
| **Historical provenance** | The same stored text as it existed at generation/save/edit time; B1.1 has **no separate provenance record** | Implied by the stored recipe; not versioned separately | No new capacity; it is only the current stored text | **Not verified.** B1.1 does not reconstruct an external source, does not claim the author measured it, and does not backfill a missing declaration. |
| **Temporary clarification** | The `?source_glass_ml=` request parameter | One request/view; never persisted, never written to a recipe/favorite/profile | Yes, but **only** for terms with no declared capacity (no automatic inheritance, OD-03) | **None.** It is a calculation assumption for this answer, not a recipe fact. |

A per-term `capacity_origin` uses `declared_text`, `temporary_clarification`, `unresolved`
or `not_applicable`, so a client can word each hint honestly. A `provenance` entry uses only `declared_text` or `unresolved`; its audit-internal
`not_applicable` terms are not emitted (§15.8.2). The UI never says that a declaration was
"verified", "confirmed by the source" or "historical" when B1.1 has done no such check.

### 15.4 Requirement 2 — cooking-step conversions

#### 15.4.1 Display contract

- The stored instruction text is never modified. The API returns spans (start/end offsets
  into the original string) plus a conversion; the page renders the original text as text
  nodes and appends a **separate** hint element beside the relevant amount.
- A hint is supplemental. If no reliable conversion exists, the step is shown exactly as
  stored, with no number and no invented approximation.
- Only a span whose link is `linked` and whose conversion is successful may produce a
  number. `ambiguous`, `shared`, `unresolved`, `unsupported`, `clarification_required` and
  `range` never produce a number.
- Spans in one step are sorted by `start` and are non-overlapping; overlapping or
  out-of-range annotations are rejected by the client (§15.8.4).
- One recipe-level clarification control covers both ingredient and step amounts; it is
  not duplicated per step.

#### 15.4.2 Arithmetic and rounding

Step conversions call the same B0/B1 path as ingredients:

- the new scanner identifies the exact span text; the accepted span is then parsed with
  `parse_quantity(..., allow_nonstandard_glass_sizes=True)` (whole-line parser, applied to
  the span only);
- the same target context from the session profile (`resolve_target_measures`);
- `convert_quantity` with exact `Fraction` arithmetic;
- the same 5% `practical_round` gate;
- the same presentation meanings: `rounded` (practical value; `≈` only when the practical
  value differs from the exact one or the source itself is approximate), `exact` (a
  **household** source amount whose exact rational has no practical value passing the gate),
  and `none` (nothing to show). A purely **metric** source amount with no practical value is
  `none`, not `exact`; B1 does not print `Точно` for a metric amount that fails the practical
  gate;
- no mass↔volume conversion; a gram/kilogram amount yields no hint in B1.1 even when its
  ingredient is linked.

#### 15.4.3 Repeated use

A single ingredient may be used across several steps. Each linked volume amount is
converted independently. Example: ingredient `Вода — 1,1 л`; step 3 `добавьте 500 мл
воды`; step 4 `добавьте 600 мл воды`. Both steps receive their own hint. B1.1 makes **no**
claim that the step amounts add up to the ingredient total; that cross-comparison is
KH-F01-B1.2 (§15.19). The ingredient line itself is not split, re-rendered or rewritten.

#### 15.4.4 Source approximation words

If the step says `около 100 мл`, `примерно 100 мл` or `приблизительно 100 мл`, the
arithmetic is unchanged but the presentation must not look exact. The scanner records
`source_approximation = {approximate: true, word: ...}` on the span, and the page shows the
approximation marker even when practical rounding did not change the value. This is a
presentation refinement of the B1 rule (OD-07); it does not change the core statuses.

B1.1 recognizes a leading approximation word only for **step spans**. An ingredient amount
that B1 cannot quantify (for example `Вода — около 1,1 л`) keeps B1 behavior: no hint, and
no invented parse. Ingredient-level approximation handling is a recorded residual, not
smuggled into this increment.

### 15.5 Requirement 3 — reliable ingredient linkage

#### 15.5.1 Why regular expressions alone are insufficient

A regular expression can find `500 мл`, but it cannot answer "500 mL of what?" A recipe
step may say `Добавьте воды`, `в муку`, `мукой посыпьте`, `смешайте с 100 мл молока`,
`доведите 1,1 л до кипения`, `орехи и сахар — по 50 г`, `половину воды оставьте`.
The same word appears in different cases; two ingredients may share a stem; one amount may
refer to a mixture rather than one ingredient; a number may be a temperature, a time, a
serving count or a dimension. A regex-only design either misses links or, worse, attaches a
confident hint to the wrong ingredient. The design therefore separates **amount
detection** (deterministic grammar, safe) from **reference resolution** (a closed set of
explicit local constructions plus explicit unresolved states).

#### 15.5.2 Approach comparison

| Approach | Recall on inflected prose | Determinism / auditability | Dependencies / cost | Risk | Verdict |
|---|---|---|---|---|---|
| A. Regex-only amount + nearest word | Low–medium | High | None | Silent wrong links; no inflections; poor on word order | Rejected as the sole method |
| **B. New deterministic span scanner + curated Russian inflection/alias resolver + explicit local construction binder** | Medium (honest about gaps) | High; fixture-testable | Standard library only; a small curated morphology table | Lower recall than ML; requires curation | **APPROVED** |
| C. Morphological library (e.g. pymorphy) + NER | Higher | Medium (dictionary/version drift) | New runtime dependency, data/licence review, Python-version testing | False positives still need adjudication; heavier deploy | Deferred; only if B's measured recall is insufficient |
| D. LLM extraction at read time | Highest on arbitrary prose | Low (non-deterministic) | Extra provider call, latency, cost, privacy exposure of step text | Cannot be the authority; violates one-call/no-inference constraints | Rejected for B1.1 |
| E. Deterministic runtime + optional LLM annotation at generation time in a new structured field | High for new recipes | Medium | Protocol change/migration; only helps new recipes | Does not help existing favorites; AC49 conflict | Deferred with Option B of §15.3.1 |

**Decision (OWNER APPROVED, v0.6):** B, with explicit `ambiguous`/`shared`/`unresolved`
outcomes and no numeric hint when confidence is not categorical. C and E remain future
options if measured recall justifies the added cost; D is not acceptable as an authority.

#### 15.5.3 Deterministic pipeline

1. **Sentence segmentation.** Split a step on `.`, `;`, `!`, `?`, keeping code-point offsets
   into the original string. Commas are retained as context, not treated as hard
   boundaries.
2. **Tokenization.** Produce tokens with exact `[start, end)` code-point offsets. The
   scanner works on the original string; normalization for matching never changes offsets.
3. **Amount-expression detection.** Walk the tokens for the start of a candidate number.
   Expand to the **maximal** supported/rejected expression (§15.5.8) and classify it
   atomically. A rejected expression is consumed whole and yields no sub-quantity.
4. **Context classification.** Reject or mark a candidate when it is a range, a
   temperature, a time, a serving count, a dimension, a percentage, a bare count, a
   qualitative phrase, a heaped/US-cup/package form, or any unsupported construct. See
   §15.5.4.
5. **Ingredient mention scan.** In the same sentence, find whole-word mentions of the
   structured ingredient names and aliases, using the inflection resolver. Record token
   positions, not just a boolean.
6. **Explicit local binding.** Apply only the constructions in §15.5.6. Produce exactly one
   of `linked`, `ambiguous`, `shared`, `unresolved` or `unsupported` per expression, with a
   machine-readable reason.
7. **Conversion.** For a `linked` span with a volume dimension, call the B1 conversion path.
   For any other status, no conversion is attempted.

All steps are pure functions over the stored recipe and the session profile; nothing is
persisted. There is no consistency aggregation step in B1.1.

#### 15.5.4 Numbers that are not ingredient amounts

The scanner classifies by the **unit**, not by the digits:

| Context | Examples | Result |
|---|---|---|
| Temperature | `180 °C`, `200 градусов`, `180°C` | No amount. `градус` is not a supported unit; a bare number is not a quantity. |
| Time | `20 минут`, `1,5 часа`, `10–12 минут` | No amount. `минут/час` are not supported units. `ч. л.` remains a teaspoon; `ч` alone is not. |
| Servings | `на 4 порции`, `2–3 порции` | No amount. `порция` is not a supported unit. |
| Dimensions | `20 см`, `диаметр 24 см` | No amount. |
| Percentages / ratios | `50%`, `1:2` | No amount. |
| Bare counts | `2 яйца`, `3 зубчика` | No convertible amount. A recognised countable form is consumed as one `unsupported`/countable **rejected span** with no hint; `2 яйца` outside the curated countable list may yield no span at all. Either way: no number, no cross-check. |
| Ranges | `2–3 ст. л.`, `от 100 до 150 мл`, `1,5–2 стакана` | `range`; one rejected span; no midpoint, no sub-quantity, no hint. |
| Qualitative | `щепотка`, `по вкусу`, `немного` | No numeric span and no hint. An ingredient amount may carry B0's `qualitative` status; a step phrase stays text. |
| Heaped / US cup / package | `с горкой`, `1 cup`, `1 пачка` | `unsupported`; one rejected span; no fabricated number and no salvage of the bare number. |

A number is never treated as an amount merely because it is near a unit word in another
clause. The scan requires the unit to follow the number under the core grammar, and the
whole maximal expression must be supported.

#### 15.5.5 Russian inflection handling (curated, deterministic)

The resolver uses the recipe's own ingredient names plus a small curated table of common
Russian food-word forms and aliases. Proposed mechanics:

- normalize case and `ё → е`, collapse whitespace, compare whole words only;
- exact normalized match first;
- then a **guarded stem match**: strip a known noun/adjective ending only if the remaining
  stem is at least 3 characters and the stem maps to exactly one ingredient in this recipe;
- curated irregular forms where stemming is unsafe (`яйцо/яйца/яиц`, `молоко/молока`,
  `мука/муки/муку/мукой`, `вода/воды/воду/водой/воде`, `соль/соли/солью`,
  `масло/масла/маслом`, `сахар/сахара/сахаром`, `лук/лука/луком`);
- adjectives and participles are context, not ingredient identities: `пшеничной муки`
  matches the head noun `мука`; `жареный лук` matches `лук`;
- different products are never merged: `соль` and `морская соль`, `мука` and
  `картофельный крахмал`, `масло` and `сливочное масло` are distinct canonical names; a
  general word only matches a general ingredient, and a specific name requires its
  distinguishing word(s);
- if two ingredients match the same word (for example a generic and a specific one) and the
  step does not disambiguate, the result is `ambiguous`, not a silent preference;
- **unrestricted stemming is forbidden.** Only the curated table, exact normalized matching
  and the guarded stem match above are allowed; stripping arbitrary suffixes from arbitrary
  words, cross-product stem merging or a general morphological analyser is not permitted in
  B1.1;
- **uniqueness-only matching is forbidden.** The fact that a sentence names exactly one
  ingredient — or that exactly one candidate exists — never links an amount by itself (OD-05);
- no transliteration, fuzzy spelling or edit-distance matching in B1.1: a typo stays
  unresolved rather than becoming a confident wrong link.

The curated table is product data, versioned with the code, contains no user data and is
not an external dictionary dependency.

#### 15.5.6 Explicit local binding rules — closed approved list (OD-05)

**Frozen rule (OWNER APPROVED): only one of the four explicit, locally supported
constructions below may link an amount to an ingredient.** A nearby name is not enough.
**Proximity-only matching, uniqueness-only matching ("the sentence names exactly one
ingredient") and unrestricted stemming are forbidden.** The old "nearest preceding mention
within 6 tokens", "nearest following mention within 4 tokens" and "single-candidate
sentence" fallbacks stay **removed**; those situations produce `unresolved` with no hint.

Every allowed construction shares the same preconditions: same sentence; no other
ingredient mention between the amount and the named ingredient; no finite verb between the
amount and the name unless the construction itself names that verb.

| ID | Construction | Positive example (links) | Potentially misleading negative example (does **not** link) | Required binding conditions | Conservative fallback |
|---|---|---|---|---|---|
| **L1** | Name after the amount (governed genitive); reason `name_after` | `Добавьте 500 мл воды.` | `Добавьте 500 мл и перемешайте.` — no adjacent name even though the recipe has water | Amount is immediately followed by a whole-word ingredient mention, with at most one function word between; the name is the **head noun** of the governed phrase; single name, not a coordinated group | `unresolved` / `no_local_binding`, or `no_mention`; no hint |
| **L2** | Name before the amount with an explicit separator; reason `name_before_separator` | `Вода — 500 мл`; `муку: 200 г` | `Вода. 500 мл...` — a sentence boundary, not a separator | Mention is followed by exactly one of `—`, `–`, `:`, `=` and then the amount; no other mention between; same sentence | `unresolved` / `no_local_binding`; no hint |
| **L3** | Name before the amount with a closed copular/imperative verb; reason `name_before_copula` | `Воду добавьте 500 мл.` | `Воду доведите до кипения, добавьте 500 мл.` — `доведите` is an unlisted finite verb between name and amount | Mention + one verb from the closed list `добавьте, влейте, всыпьте, положите, возьмите, нужно, потребуется, отмерьте` + the amount; that named verb is the **only** finite verb between mention and amount | `unresolved` / `no_local_binding`; no hint |
| **L4** | Coordinated list with a name for **each** amount; reason `name_after` per span | `100 мл молока и 100 мл воды` | `100 мл воды и молока` — one amount governs a coordinated name group | Each amount independently satisfies L1 with its own head-noun name; two independent spans | Each amount that fails L1 is `unresolved`; the coordinated group case below is `shared` |

**No other construction links.** In particular:

- **Shared/coordinated name group — classification only, not a link.** An amount governing
  a coordinated name group (`100 мл воды и молока`) is `shared`: no per-ingredient hint and
  no full amount attributed to each. Positive case: `Смешайте 500 мл воды и молока.`;
  misleading negative: `500 мл воды и 500 мл молока` (here each amount binds separately under
  L4). Fallback: `shared`, no numeric hint.
- **Anaphora/portions.** A portion/pronoun word with no number (`его`, `её`, `их`, `этого`,
  `этой`, `половину воды`, `остаток`, `столько же`) produces no span. A **numeric** span
  governed by one (`половину от 500 мл`) is `unresolved` with reason `anaphora`. Fallback:
  no hint.
- **Multiple candidates.** Two equally plausible candidates, or one candidate on each side of
  the amount, yield `ambiguous` with reason `multiple_candidates`; no positional coin flip.
  Fallback: no hint.
- **Cross-sentence references.** A name in another sentence never links. Fallback:
  `unresolved`.
- **Uniqueness is never a link.** A sentence that names exactly one ingredient still does
  **not** bind an unbound amount (`Добавьте 500 мл и перемешайте.` → `unresolved`, OD-05).

Every span carries a machine-readable `link_reason` from a closed set: `name_after`,
`name_before_separator`, `name_before_copula`, `shared_amount`, `multiple_candidates`,
`anaphora`, `no_local_binding`, `no_mention`, `range`, `us_cup`, `package`, `countable`,
`heaped`, `unsupported_construct`, `malformed_expression`. The Russian wording is produced
only by the Web layer.

#### 15.5.7 Unresolved and unsupported behavior (explicit)

- `linked` + successful conversion → hint.
- `linked` + failed conversion → no hint; the failure reason is available to the UI for an
  optional explanation.
- `ambiguous` → no hint; never a "best guess"; the UI may say only that the amount could
  not be matched reliably, without naming a wrong ingredient.
- `shared` → no per-ingredient hint.
- `unresolved` → no hint; no error message per amount (avoid noise). This includes the
  removed proximity-only and single-candidate cases.
- `unsupported`/`range` → no hint; the original text remains. A qualitative phrase
  produces no span, so there is nothing to convert; an ingredient amount may still carry
  B0's `qualitative` status.
- A recipe with many unresolved spans still shows all reliable hints; one failure never
  suppresses the others (the partial-result rule of AC20/D10).

#### 15.5.8 Atomic amount-expression grammar (new B1.1 scanner)

The scanner's expression grammar is deliberately small and is **not** the B0 whole-line
parser. It is specified here so it can be tested and audited (Codex F02/F06):

```text
expression    := part (separator part)* | rejected_form | range_form
part          := number ws* unit capacity? qualifier?
rejected_form := number ws* (us_cup | package | countable)
range_form    := ["от"] number ws* ("до" | "–" | "—" | "-") ws* number ws* unit
                 capacity? qualifier?
number        := integer | decimal | fraction | mixed | vulgar | vulgar_mixed
decimal       := integer [.,] integer
fraction      := integer "/" integer
mixed         := integer ws+ integer "/" integer
vulgar        := one of the core's vulgar-fraction characters
                (¼ ½ ¾ ⅐ ⅑ ⅒ ⅓ ⅔ ⅕ ⅖ ⅗ ⅘ ⅙ ⅚ ⅛ ⅜ ⅝ ⅞)
vulgar_mixed  := integer ws* vulgar
unit          := the core's recognized metric/household unit spellings
us_cup        := the curated "cup"/"чашк" forms
package       := the curated пачка/банка/упаковка forms
countable     := the curated яйцо/яйца/яиц, зубчик, головка, ломтик, шт forms
qualifier     := "без горки" | "с горкой" | "с верхом"
ml_unit       := the core's millilitre spellings (мл, миллилитр, миллилитра, ...)
capacity      := "(" number ml_unit ")" | number ml_unit | "на" number ml_unit
separator     := "+" | "и" | "плюс" | implicit metric juxtaposition ("1 л 200 мл")
```

Atomicity rules:

- The scanner expands a candidate to its **maximal** expression: it consumes a trailing
  qualifier, a glass-capacity declaration, a recognised separator and the next term, and
  stops at a token that cannot belong to the expression. It never scans inside a consumed
  expression again. A `range_form` is recognized before `part` expansion and consumed
  whole, so its individual numbers are never re-scanned as parts.
- **If any part of the maximal expression is unsupported, the whole expression is
  rejected.** It yields one `unsupported`/`range`/`malformed_expression` span with no
  conversion. No sub-quantity is salvaged:
  - `2–3 ст. л.` → `range`; not `3 ст. л.`;
  - `1,5–2 стакана` → `range`; not `1,5 стакана`;
  - `1 ст. л. с горкой` → `unsupported`; not `1 ст. л.`;
  - `1 cup` → `unsupported`; not `1`;
  - `1 стакан (200 мл` (unbalanced) → `malformed_expression`; not `1 стакан` or `200 мл`;
  - `2 зубчика` → `unsupported`/countable; not `2`;
  - `1 стакан 200` (no unit on the declaration) → `malformed_expression`; not `1 стакан`.
- A `rejected_form` is consumed as one `unsupported` span with reason `us_cup`/`package`/
  `countable`. It never falls back to the bare number (`1 cup`, `1 пачка`, `2 яйца`,
  `3 зубчика`). A heaped qualifier on a spoon is consumed by the normal `part` grammar and
  rejected whole as `unsupported` with `link_reason: heaped` (`1 ст. л. с горкой`).
- A supported compound (`1 л 200 мл`, `1 ст. л. и 1 ч. л.`) is one span with the core's
  parts summed by the existing conversion path. It is not split into two spans.
- A bare `number ml_unit` immediately after a **household** unit is the capacity
  declaration, not a second part: `1 стакан 200 мл` is one glass with a 200 mL capacity.
  A bare number immediately after a household unit with no `ml_unit` is a malformed
  capacity declaration and rejects the whole expression (`1 стакан 200`). Implicit
  juxtaposition is permitted only between two **metric** parts (`1 л 200 мл`); a household
  part followed by another part requires an explicit `и`/`+` or a capacity declaration.
  This mirrors the core's `AMBIGUOUS_JUXTAPOSITION` rule.
- `и`/`плюс` is a **measure separator only when the next token starts a supported
  number+unit term**. When it joins a name group (`500 мл воды и молока`), it does not
  extend the amount expression: the span ends at `500 мл` and the binder sees a coordinated
  name group (`shared`). When it joins two amount+name phrases
  (`500 мл молока и 500 мл воды`), the first span ends at `500 мл` because `молока` is not a
  unit, and the second amount is scanned independently.
- A qualitative phrase (`щепотка`, `по вкусу`, `немного`) with no number produces no
  numeric span. An anaphora/portion word with no number (`его`, `половину воды`,
  `столько же`) also produces no span. If a **numeric** span is governed by a portion or
  anaphora word (`половину от 500 мл`), it is `unresolved` with reason `anaphora`.
- A bare number with no supported unit is not an amount. A supported unit with no number is
  not an amount.
- The scanner never uses nested quantifiers or unbounded lookaround; it consumes each
  character a bounded number of times (§15.14).

### 15.6 Requirement 4 — source-capacity resolution and integrity

The v0.4 "consistency and integrity" requirement mixed cross-step totals with source-glass
resolution. B1.1 keeps only the latter; the former is deferred (§15.19).

#### 15.6.1 Per-term precedence

Every source term is resolved independently in the order of §15.3.6:

```
declared capacity on this term
  > temporary clarification for this request
  > clarification_required (no capacity, no hint)
```

This order has **no inheritance step** (OD-03: no automatic inheritance). A term with its
own declaration is never overridden by a clarification or by a recipe-level value. Two
independently declared capacities in one recipe are both used for their own terms; no
artificial conflict is created. A temporary clarification applies only to terms with no
declared capacity. A linked ingredient's declared capacity is never used to resolve a step
term.

#### 15.6.2 Temporary clarification scope

- One temporary `source_glass_ml` clarification applies to **all remaining undeclared glass
  terms** in the recipe view: ingredient amounts and step amounts.
- It is a bounded integer 50–1000 mL, validated as today (B1 `?source_glass_ml=`), and is
  never read from a body.
- It is per request and per answer; it is not written to the favorite, the profile or a
  cache. A later GET without the parameter starts from the recipe's own declarations again.
- A declared capacity always wins; the clarification never changes it. There is no
  inherited capacity to consider (OD-03).
- The clarification is not a declaration, not a profile value and not evidence of the
  recipe author's intent (§15.3.7).

#### 15.6.3 Personal overrides and account isolation

- Step and ingredient conversions use only the signed-in person's profile capacities,
  read server-side through the session user id. No target capacity is accepted from a
  request body, a query parameter or another account.
- The derived step/link/provenance block is computed inside the same request as the
  favorite read, for that user. It is never stored in a shared table, a process-wide cache
  keyed only by recipe id, or a log.
- User A's glass/teaspoon/tablespoon values never affect User B's presentation, even for
  the same favorite text; the favorite is owner-scoped and the profile is owner-scoped.
- The morphology/alias table is generic product data and contains no user recipe text.

#### 15.6.4 Failure confinement

The analysis is split into independent failure domains (Codex F10). Existing ingredient
hints are protected: they are computed independently and are never nulled or emptied by a
step or provenance failure.

| Failure | What is lost | What is still served |
|---|---|---|
| Ingredient conversion fails unexpectedly | `measures` may be `null` (B1 behavior) | The favorite detail and recipe text, as today |
| Provenance audit raises | `provenance.status = "failed"` with empty arrays, `analysis.status = "failed"` | Ingredient conversions, steps, recipe |
| Step analysis raises for the whole recipe | `steps: []`, `analysis.status = "failed"` | Ingredient conversions, provenance, recipe |
| One step is malformed/oversized | That step is omitted or `status = "failed"` | Other steps, ingredient conversions, recipe |
| One span conversion raises | That span has `conversion: null` and a reason | Other spans, ingredients, recipe |
| A fraction is not safely serializable | That conversion is omitted with `unsafe_fraction` | The source amount and all other hints |
| A cap is reached | Remaining spans are omitted, `analysis.status = "incomplete"` | The deterministic prefix already found |
| The profile row is corrupt | `measures` may be `null` (B1 behavior) | The recipe; no invented target |

No analysis failure returns a 4xx/5xx, removes the recipe, or writes anything.

#### 15.6.5 Deferred consistency

The water-total comparison, `balanced`/`partial`/`excess`/`near`, additive consumption
events, completeness reports and all consistency acceptance tests are **not** in B1.1.
They are recorded in §15.19 (KH-F01-B1.2) so the rationale and the deferred decisions
(OD-04, OD-14) are not lost. The B1.1 API has no `consistency` field and must not carry a
dead one.

### 15.7 Requirement 5 — UX

#### 15.7.1 Ingredient presentation

The existing B1 pattern is retained. B1 presents a purely metric volume as a **glass**,
not as a spoon (`_target_unit`); a metric `30 мл` therefore has no spoon hint. Examples
use the system target 250/5/15 mL unless a personal tablespoon is stated:

```text
Вода — 1,1 л
  ≈ 4,5 вашего стакана            (4,4 exact; 4,5 is within 5%, so ≈)

Масло — 2 ст. л.
  (no hint with the default 15 mL tablespoon: the source is already
   2 of your tablespoons — identical)

Масло — 2 ст. л.  (personal tablespoon 20 mL)
  1,5 вашей столовой ложки        (30 / 20 = 1,5 exactly; no ≈)

Мука — 200 г
  (no hint: B2 mass conversion is not authorized)
```

A metric volume is offered only as the person's glass; if no practical glass value passes
the 5% gate, no hint is shown and the metric amount remains. B1 does not turn `30 мл` into
spoons.

- The original line is always visible and unchanged.
- `Точно: …` is used only for a **household** source amount whose exact rational has no
  practical rounded form; a metric amount with no practical glass value gets no hint. `≈`
  is used when the practical representation differs from the exact value or when the source
  itself says "около/примерно/приблизительно".
- A value equal to the source presentation in the same unit is not repeated.

#### 15.7.2 Step presentation

The stored step stays exactly `Доведите до кипения, добавьте 500 мл воды.` The page may
render:

```text
3. Доведите до кипения, добавьте 500 мл воды (2 ваших стакана).
```

The parenthetical hint is a separate element placed immediately after the amount span; it
is not inserted into the stored string. `500 мл` is exactly 2 glasses at 250 mL, so no `≈`.
For a step with a non-exact amount:

```text
4. Влейте 600 мл воды и варите 10 минут.
   600 мл воды — ≈ 2,5 вашего стакана        (10 минут is not an amount)
```

The hint does not cover the whole step and does not imply that the time converts.

#### 15.7.3 Exact versus practical approximation

Three distinct ideas are kept apart in wording:

1. **Source amount** — what the recipe says (`1,1 л`, `около 500 мл`).
2. **Exact converted value** — the rational result for the established source and target
   capacities (`4,4 стакана` for 1.1 L and 250 mL).
3. **Practical presentation** — a usable kitchen value within the 5% gate (`4,5 стакана`),
   marked `≈` only when it differs from the exact value or the source is approximate.

A conversion is never presented as physically exact merely because the arithmetic is exact.
The word `Точно` refers to the arithmetic under the stated capacities, not to a real spoon
or glass.

#### 15.7.4 Unknown source measure — explaining without demanding knowledge

When a recipe contains an undeclared glass, the UI must not require the person to know a
fact House Helper omitted. Proposed recipe-level explanation:

```text
В рецепте не указан объем стакана, поэтому мы не можем точно пересчитать эти количества.
[200 мл] [250 мл] [Другой объем] [Не знаю — оставить исходное количество без пересчета]
```

- The chooser is one per recipe, not one per amount.
- Choosing a value re-reads the favorite with `?source_glass_ml=…`; it applies to the
  remaining undeclared glass terms in ingredient and step hints for this view only and is
  not saved.
- «Не знаю — оставить исходное количество без пересчета» is a first-class safe option
  (OD-10, OWNER APPROVED). Selecting it closes the chooser, suppresses every
  glass-dependent hint, **preserves every independent valid (declared) conversion**, leaves
  the original millilitre/litre and other amounts visible, invents no capacity and does not
  block or error the recipe. The choice **applies only to the current recipe view, is not
  persisted, and clears any previous temporary clarification for this view**. A later view
  starts from the recipe's own declarations again.
- The explanation says what is missing and why a hint is absent; it never asks the person
  to guess silently.
- Because there is **no automatic inheritance** (OD-03), an undeclared step glass never
  borrows an ingredient's declaration or its wording. The explanation says that the step
  itself does not state the capacity and offers a clarification; it never implies the
  ingredient line supplied one.

#### 15.7.5 No consistency notices in B1.1

B1.1 shows no `balanced`/`partial`/`excess`/`near` notice and no "recipe validated" label.
Those notices and their wording are deferred to B1.2 (§15.19). A recipe is never labelled
"validated", "checked", "correct" or "consistent" merely because a conversion succeeded.

#### 15.7.6 Small B1 copy correction (OWNER APPROVED)

| Current B1 copy | Approved copy | Where |
|---|---|---|
| `ваш размер` (profile badge) | **`Указано вами`** | `chat-logic.js` `profileMeasurePersonal`; the badge that distinguishes a personal value from the default |
| `Мой стакан` | **`Объем моего стакана, мл`** | profile measure field label |
| `Моя чайная ложка` | **`Объем моей чайной ложки, мл`** (for consistency) | profile measure field label |
| `Моя столовая ложка` | **`Объем моей столовой ложки, мл`** (for consistency) | profile measure field label |

- The badge change is a copy-only change; it does not change the stored value, the
  default/override behavior or the API.
- Field labels state the unit so the number is not ambiguous; the existing hint already
  gives the range and the default.
- Use `Объем`, matching the existing UI spelling; do not introduce `Объём` in UI copy.
- The correction is included in B1.1 because it touches the same measure UI; it is not a
  reopening of B1's accepted behavior.

### 15.8 Data model, API and migration impact

#### 15.8.1 Data model

| Object | Proposed change | Migration |
|---|---|---|
| `core.recipes.Ingredient.amount` | None: the declaration is part of the existing text | None |
| `message_recipes.ingredients` JSON | None: stores the same amount strings | None |
| `favorite_recipes.ingredients` JSON | None: the copy keeps the same strings | None |
| `favorite_recipes.steps` JSON | None: original step strings unchanged | None |
| Step spans, links, conversions, provenance | Derived per request; not stored | None |
| Provenance audit result | Derived; optionally a metric/log count, never recipe text | None |
| `users.measure_*` | None | None |

**Conclusion: no migration is necessary for the approved Option A.** The schema head
remains `0014_user_measure_overrides`. The structured alternatives (options B/C) are
rejected for B1.1; selecting one later would need a migration and a protocol-compatibility
plan reviewed separately.

#### 15.8.2 Typed API contract (additive)

`GET /api/favorites/{id}` and the `PATCH` answer keep every existing field and add typed
data inside `measures`. The block as a whole remains optional (it may be `null` on a B1
failure), and a client that predates B1.1 can ignore the new fields; inside a
`schema_version: 2` block the fields below are required. The following is the complete B1.1
contract (Codex F09). Existing B1 fields keep their released names and meanings;
`schema_version` is absent on a legacy B1 block.

**`schema_version: 2` scope (binding).** `schema_version: 2` versions **only** the additive
measure-annotation payload described in this subsection. It does **not** change recipe
protocol v1, does **not** add or alter a protocol key, and does **not** require a database
migration (§15.8.1). Backward compatibility and fail-safe optional-field validation are
mandatory: a pre-B1.1 client ignores the block, a new client treats the whole block as
optional, and any malformed optional field discards only the affected annotation or step —
never the recipe or the existing ingredient block (§15.8.4–§15.8.5).

```ts
type FractionJson = { numerator: number; denominator: number };
// Integers; denominator >= 1; gcd(numerator, denominator) = 1;
// 0 <= numerator, denominator <= 2^53 - 1. No float ever represents a quantity.
// A conversion whose exact or rounded fraction cannot satisfy this bound is omitted.

type CapacityOrigin =
  | "declared_text"                // the term itself declares the capacity
  | "temporary_clarification"      // ?source_glass_ml=, this request only
  | "not_applicable"               // metric or spoon term; no per-term glass capacity
  | "unresolved";                  // capacity needed but not established
// OD-03: there is no "inherited_from_ingredient" origin. A step term never
// inherits a capacity from a linked ingredient.

type ConversionStatus =
  | "exact" | "nominal_household" | "clarification_required"
  | "unsupported" | "ambiguous" | "unresolved";

type ConversionReason =
  | "converted" | "source_not_quantified" | "glass_size_required"
  | "glass_size_invalid" | "glass_size_conflict"
  | "mass_volume_coefficient_required";

type SourceApproximation = {
  approximate: boolean;
  word: "около" | "примерно" | "приблизительно" | null;
};

type SourceTerm = {
  unit: "glass" | "teaspoon" | "tablespoon" | "milliliter" | "liter";
  amount: FractionJson;
  capacity_ml: FractionJson | null;   // glass only; null for spoons and metric terms
  capacity_origin: CapacityOrigin;    // spoons/metric terms are "not_applicable"
};

type ConversionJson = {
  target_unit: "glass" | "teaspoon" | "tablespoon";
  status: ConversionStatus;
  reason: ConversionReason;
  exact: FractionJson | null;
  rounded: (FractionJson & { relative_error: FractionJson }) | null;
  identical: boolean;
  presentation: "rounded" | "exact" | "none";
};

type IngredientConversion = {
  position: number;
  source: string;
  source_kind: "household" | "metric_volume";
  target_unit: "glass" | "teaspoon" | "tablespoon";
  status: ConversionStatus;
  reason: ConversionReason;
  exact: FractionJson | null;
  rounded: (FractionJson & { relative_error: FractionJson }) | null;
  identical: boolean;
  presentation: "rounded" | "exact" | "none";
};

// B1.1 extends the released B1 ingredient object with per-term source data.
type IngredientConversionV2 = IngredientConversion & {
  source_terms: SourceTerm[];          // required in a schema_version 2 block
};

type LinkStatus = "linked" | "ambiguous" | "shared" | "unresolved" | "unsupported";

type SpanAnnotation = {
  start: number;                       // Unicode code points, half-open, into step_text
  end: number;
  text: string;                        // step_text[start:end], exact
  ingredient_position: number | null;
  link: LinkStatus;
  link_reason:
    | "name_after" | "name_before_separator" | "name_before_copula"
    | "shared_amount" | "multiple_candidates" | "anaphora"
    | "no_local_binding" | "no_mention" | "range"
    | "us_cup" | "package" | "countable" | "heaped"
    | "unsupported_construct" | "malformed_expression";
  conversion: ConversionJson | null;  // a handled failure is an object with a failure status
  conversion_omitted_reason:
    | "mass" | "unsafe_fraction" | "conversion_failed" | null;
  capacity_origin: CapacityOrigin;
  source_approximation: SourceApproximation;
};

type StepAnnotation = {
  step_index: number;
  step_text: string;                   // exact stored text, echoed for span verification
  text_sha256: string;                 // lowercase hex SHA-256 of UTF-8 step_text
  status: "complete" | "incomplete" | "failed";
  spans: SpanAnnotation[];             // sorted by start, non-overlapping
};

type DeclaredContent = {
  amount: FractionJson;
  unit: "milliliter" | "liter" | "gram" | "kilogram";
};

type ProvenanceEntry = {
  ingredient_position: number;
  term_index: number;                  // 0-based within the amount
  kind: "glass" | "package" | "cup" | "other";
  status:
    | "declared" | "undeclared" | "invalid_capacity"
    | "package_content_missing" | "unsupported_other";
  capacity_ml: FractionJson | null;      // glass only
  declared_content: DeclaredContent | null; // package/jar/bag only
  origin: "declared_text" | "unresolved";
  reason: string | null;
};

type MeasuresBlockV2 = {
  schema_version: 2;                   // absent = legacy B1 block
  favorite_version: number;            // must equal the detail's top-level `version`
  target: { glass_ml: number; teaspoon_ml: number; tablespoon_ml: number };
  source_glass_ml: number | null;
  source_glass_required: boolean;
  steps_source_glass_required: boolean;
  ingredients: IngredientConversionV2[];
  steps: StepAnnotation[];
  provenance: {
    status: "complete" | "incomplete" | "failed";
    declared: ProvenanceEntry[];
    incomplete: ProvenanceEntry[];
  };
  analysis: {
    status: "complete" | "incomplete" | "failed"; // overall derived-block summary
    spans_emitted: number;
    spans_omitted: number;
    omitted_reasons: { limit_exceeded: number; malformed: number; unsafe_fraction: number };
    limits: {
      max_spans_per_step: number;
      max_spans_per_recipe: number;
      max_step_chars: number;
      max_steps: number;
    };
  };
};
```

**Invariants.**

- Arrays are ordered deterministically: `steps` by `step_index`; `spans` by `start`;
  `provenance.*` by `(ingredient_position, term_index)`; `ingredients` by `position`.
- `start`/`end` are half-open Unicode **code-point** offsets into the echoed `step_text`;
  `step_text.slice(start, end) === span.text` in Python and after the client's code-point
  mapping in JavaScript. Spans never overlap.
- `favorite_version` is the same version as the detail's top-level `version`; a response
  whose version is not the currently rendered one is stale and its span hints are ignored.
- `steps` contains one entry for a step that has at least one span, or whose own analysis
  failed; a step with no spans and no failure is omitted, and the client treats an absent
  step as "no hints". This keeps the payload bounded.
- In a `schema_version: 2` block, every ingredient is an `IngredientConversionV2` and
  therefore carries `source_terms`, one entry per parsed source term in deterministic
  order, so AC84's per-term `capacity_origin` is always available. A legacy B1 block has
  the base `IngredientConversion` without it.
- `conversion` is `null` for a span that is not `linked`, for a mass or unsafe-fraction
  span, and for an unexpected conversion exception; a **handled** conversion failure
  returns a `ConversionJson` whose `status`/`reason` say why no hint is shown.
  `conversion_omitted_reason` explains a `null`; it is `null` for a non-linked span (no
  conversion was attempted) and for a successful conversion.
- `link_reason = "no_mention"` means the sentence names no ingredient; `"no_local_binding"`
  means mentions exist but none forms a supported local construction.
- `capacity_origin` is reported for a supported household term even when the link is
  `unresolved` or `ambiguous`; it is `not_applicable` for a rejected construct
  (range/unsupported/malformed), a metric term or a spoon term.
- `source_glass_required` retains its B1 ingredient-list meaning; the new
  `steps_source_glass_required` says the step list contains at least one glass term that is
  not declared in the step text (**there is no inheritance**, OD-03), evaluated before the
  temporary clarification (as B1's `source_glass_required` is). A client may OR them for the
  chooser.
- `analysis.status` summarizes the whole derived block: `failed` if the step pass or the
  provenance audit failed, `incomplete` if any cap or unsafe-fraction omission occurred or
  `provenance.status = "incomplete"`, otherwise `complete`. `provenance.status` and each
  `StepAnnotation.status` give the component detail; a component failure still leaves the
  other component's data intact.
  `provenance.incomplete` and `provenance.status = "incomplete"` never cause a 4xx/5xx.
- `provenance` contains only entries a reader may need: `declared` in `declared`, and
  `undeclared`/`invalid_capacity`/`package_content_missing`/`unsupported_other` in
  `incomplete`. Audit-internal `not_applicable` terms (metric, countable, qualitative) are
  omitted from the payload and never make `provenance.status = "incomplete"`.
- `?source_glass_ml=` remains a single integer 50–1000, validated as today, and now applies
  to both ingredient and step terms that are still undeclared.
- No new endpoint, no new required request field, no change to POST/PATCH/DELETE bodies.
- A text (legacy) favorite has empty but valid `ingredients`, `steps` and `provenance`
  arrays, no hints, and its text exactly as stored.

For a recipe with `Молоко — 1 стакан (200 мл)` the corresponding provenance entry is:

```json
{"ingredient_position": 1, "term_index": 0, "kind": "glass", "status": "declared",
 "capacity_ml": {"numerator": 200, "denominator": 1}, "declared_content": null,
 "origin": "declared_text", "reason": null}
```

A `1 стакан` without a capacity appears under `incomplete`:

```json
{"ingredient_position": 2, "term_index": 0, "kind": "glass", "status": "undeclared",
 "capacity_ml": null, "declared_content": null, "origin": "unresolved",
 "reason": "capacity_not_declared"}
```

#### 15.8.3 Fraction serialization and safe integers

- Every quantity is a reduced rational `{numerator, denominator}` with JSON integers;
  never a float, never a localized decimal string, never a rounded implicit value.
- The B1 fields keep their released numeric encoding. B1.1 adds no second encoding: the
  adapter enforces the safe-integer bound `0 <= numerator, denominator <= 2^53 - 1`
  (JavaScript's exact-integer range) and **omits** a conversion that would exceed it. A
  step span keeps its source text with `conversion: null` and
  `conversion_omitted_reason = "unsafe_fraction"`; an ingredient conversion is omitted from
  `ingredients`. Either omission is counted in `analysis.omitted_reasons.unsafe_fraction`;
  nothing is silently rounded.
- `rounded` carries its own `relative_error` fraction; the page shows `≈` only when that
  error is non-zero or the source is approximate. A zero-error practical value is shown
  without `≈`.
- `identical` keeps the B1 meaning: the converted amount equals the source amount in the
  same unit, so there is nothing new to show.

#### 15.8.4 Span safety, Unicode and staleness

This subsection is the binding contract for Codex F03.

- **Coordinate system.** `start` and `end` are half-open offsets counted in Unicode code
  points of the exact stored step string, from 0. In Python, `step_text[start:end]` is the
  span. They are **not** UTF-16 code-unit indexes, byte offsets or normalized-text offsets.
- **Server guarantees.** Spans are sorted by `start`, non-overlapping, within
  `[0, len(step_text)]`, and each `span.text` equals the exact slice. The server echoes the
  whole `step_text` for each annotated step and its `text_sha256` (lowercase hex SHA-256 of
  the UTF-8 encoding of `step_text`).
- **Python → JavaScript mapping.** JavaScript strings are UTF-16; a character outside the
  Basic Multilingual Plane (for example an emoji) occupies two code units but one code
  point. A client **must not** pass `start`/`end` directly to `String.prototype.slice`.
  It maps a code-point index to a UTF-16 index by iterating code points:

  ```js
  function codePointToUtf16(text, index) {
    let cp = 0, u16 = 0;
    for (const ch of text) {          // for...of iterates code points
      if (cp === index) return u16;
      cp += 1;
      u16 += ch.length;               // an astral character contributes 2
    }
    return cp === index ? u16 : -1;   // -1 means the index is out of range
  }
  const s16 = codePointToUtf16(stepText, span.start);
  const e16 = codePointToUtf16(stepText, span.end);
  const ok = s16 >= 0 && e16 >= 0 && stepText.slice(s16, e16) === span.text;
  ```

- **Mandatory client checks.** Before drawing any hint, the client verifies all of:
  1. `measures.favorite_version` equals the version of the favorite currently rendered,
     and the response is the latest request issued for this view;
  2. `step.step_text` equals the exact text currently rendered for that step;
  3. every span has `0 <= start < end <= codePointLength(step_text)`, the mapped slice
     equals `span.text`, and spans are sorted and non-overlapping;
  4. the annotation belongs to the same `step_index` and text the page is drawing.
  If any check fails, the **whole step's** annotations are discarded: the page draws the
  original text only, with no hint and no error. Ingredient hints and the rest of the
  recipe are unaffected.
- **Stale asynchronous responses.** The page tags each favorite read with a monotonic
  request id and ignores a response that is not the latest for the view. Independently, a
  response whose `favorite_version` differs from the rendered version is stale. A stale
  response never overwrites newer hints, never shifts a hint onto edited text and never
  writes anything.
- **Malformed optional data.** A client validates JSON types and the closed enums, and
  rejects a malformed new field (wrong type, `denominator <= 0`, an unknown status, a
  non-integer offset) by discarding the affected step or annotation, never the ingredient
  block or the recipe. Unknown fields are ignored; absent `steps`, `provenance` or
  `analysis` are treated as empty. No malformed optional value may throw or blank the page.
- **Hash use.** `text_sha256` is an additional integrity field for tests and for clients
  that can verify it asynchronously; the exact `step_text` echo plus the slice equality
  are the mandatory synchronous gate. A hash mismatch discards the step's annotations.
- **Output safety.** The page renders the original text as text nodes and appends separate
  hint elements. No `innerHTML`, no interpolation of recipe text into markup.

#### 15.8.5 Backward compatibility

- AC01–AC57 remain valid and unchanged.
- Protocol v1 remains byte-compatible; no key is added or removed.
- Saved favorites remain readable and editable; no backfill and no silent rewrite.
- Existing `1 стакан 200 мл` and `1 стакан (200 мл)` both parse; old favorites keep their
  exact text.
- A favorite edited to remove a declaration loses it only because the person edited it; the
  server never removes it.
- An old server emits no `schema_version`, `steps`, `provenance` or `analysis`; the new page
  treats those as absent and draws no step hints while still drawing ingredient hints.
- An old client ignores the new block; a new client treats the whole B1.1 block as
  optional, while a `schema_version: 2` block follows the typed contract in §15.8.2.
- The B1 ingredient block and its copy stay as released unless the owner accepts the copy
  correction in §15.7.6.

### 15.9 Explicit ambiguity and failure rules

The Result column names the span's `link` status or its `link_reason`; `range`, `us_cup`,
`package`, `countable`, `heaped` and `malformed_expression` are `link_reason` values on an
`unsupported` span.

| Situation | Result | User-visible behavior |
|---|---|---|
| Amount is qualitative (`по вкусу`, `щепотка`) | No step span; an ingredient amount may carry B0's `qualitative` | Original text; no number |
| Amount is a range (`2–3 ст. л.`, `1,5–2 стакана`) | `range` | Original text; no midpoint; no sub-quantity |
| Heaped/ambiguous spoon, US cup, package in a step | `unsupported` | Original text; no fabricated number; no salvage |
| Unbalanced or malformed expression | `malformed_expression` | Original text; no span/hint; no salvage |
| Bare count (`2 яйца`, `3 зубчика`) | `unsupported`/countable rejected span, or no span if outside the curated list | Original text; no number; no consistency use |
| Undeclared glass, no clarification | `clarification_required` | Recipe-level explanation + chooser; no glass hint |
| Undeclared glass, clarification supplied | Converted with the temporary capacity | Hint; clarification not saved |
| Term declares its own capacity | `declared_text` | Converted with that capacity; clarification ignored |
| Step glass undeclared even though its linked ingredient declares a capacity (OD-03) | `clarification_required` (or `temporary_clarification` if supplied) | No hint from the ingredient; explicit explanation; capacity is never inherited |
| Step glass with competing declared capacities | `clarification_required` (or `temporary_clarification` if supplied) | No hint; explicit explanation; never `ambiguous` |
| No explicit local binding | `unresolved` | No hint; no wrong name |
| Two or more candidate ingredients | `ambiguous` | No hint; no silent choice |
| Shared amount for several ingredients | `shared` | No per-ingredient hint |
| Pronoun/portion reference with no number (`половину воды`, `его`) | No span | No hint |
| Numeric span governed by a portion word (`половину от 500 мл`) | `unresolved`/`anaphora` | No hint |
| Repeated ingredient in several steps | Multiple independent links | Each step hint independently; no total claim |
| Mass amount in a step | `linked`, `conversion: null`, `conversion_omitted_reason: "mass"` | No hint; B2 required |
| Handled conversion failure for one amount | `ConversionJson` with failure status/reason | No hint; other hints still shown |
| Unexpected conversion exception | `conversion: null`, `conversion_omitted_reason: "conversion_failed"` | No hint; other hints still shown |
| Fraction not safely serializable | Step span: `conversion: null`, `unsafe_fraction`; ingredient: omitted from `ingredients` | No hint for that conversion; source visible |
| Malformed/oversized step | Step omitted or `failed` | Recipe still rendered |
| Step analysis failure | `steps: []`, `analysis.status = "failed"` | Ingredient hints and recipe intact |
| Provenance failure | `provenance.status = "failed"` with empty arrays, `analysis.status = "failed"` | Ingredient and step hints intact |
| Corrupt profile row | `measures` block `null` | Recipe readable; no invented target |
| Stale/mismatched annotation | Client discards the step's hints | Original text; no error; ingredient hints intact |
| Span overlap/out of range | Client discards the step's hints | Original text; no error |
| Limit reached | `incomplete`, counted omission | Deterministic prefix; recipe readable |

No case silently substitutes a default, midpoint, personal capacity or invented ingredient.
No case writes to the recipe.

### 15.10 Acceptance criteria (OWNER APPROVED product requirements — implementation not authorized; not part of AC01–AC57)

These criteria are the approved B1.1 product requirements derived from OD-01–OD-03 and
OD-05–OD-13. They do not modify AC01–AC57, and approving them does not authorize
implementation. AC63 and AC64 are **retained but deferred** to KH-F01-B1.2; their
identifiers are not reused for a different meaning.

#### 15.10.1 Revised AC58–AC80

- **AC58 (approved)** A newly generated structured recipe declares a capacity/net content
  for every glass or other non-self-explanatory household measure whose metric meaning is
  not fixed by the unit, or the deterministic audit reports it as incomplete. A declaration
  is the recipe's own stored text; B1.1 never claims that it was verified against a
  historical external source. No capacity is invented for a user-supplied recipe.
- **AC59 (approved)** A declaration in an ingredient amount survives generation →
  `message_recipes` storage → favorite copy → GET → PATCH edit unchanged, with no migration
  and no extra protocol key.
- **AC60 (approved)** An existing recipe or favorite with an undeclared glass receives no
  retroactive capacity; it remains readable and the missing source meaning is explained.
- **AC61 (approved)** Step hints are derived at read time by the new B1.1 scanner. The
  stored step text is byte-identical before and after; a read writes nothing; an edit
  recomputes the block.
- **AC62 (approved)** Step conversions use the released B0/B1 exact arithmetic, target
  context, statuses and 5% gate; no mass↔volume or ingredient-coefficient conversion is
  performed; the B1.1 layer adds no arithmetic.
- **AC63 — DEFERRED to KH-F01-B1.2** (consistency `balanced` for the water example;
  rationale retained in §15.19).
- **AC64 — DEFERRED to KH-F01-B1.2** (report a step/ingredient mismatch without modifying
  any amount; rationale retained in §15.19).
- **AC65 (approved)** Temperatures, times, serving counts, dimensions, percentages, bare
  counts and other non-amount numbers do not produce step amounts or hints.
- **AC66 (approved)** Inflected ingredient names (`мука/муки/муку/мукой`,
  `вода/воду/водой`, `соль/соли`) link under the explicit local constructions; different
  products with a similar stem are not merged.
- **AC67 (approved)** Multiple amounts in one step are tokenized atomically and linked
  independently under supported local constructions, or left unresolved; none is
  blanket-assigned to the first ingredient.
- **AC68 (approved)** Multiple candidates, shared coordinated amounts and anaphora produce
  no hint and an explicit `ambiguous`/`shared`/`unresolved` status; no silent best guess.
- **AC69 (approved)** Repeated use of one ingredient across several steps is supported and
  each linked amount is converted independently; B1.1 makes no cross-step total or
  consistency claim.
- **AC70 (approved)** Ranges, heaped spoons, US cups and packages are rejected as whole
  unsupported spans; qualitative amounts produce no numeric span. Neither path yields a
  midpoint, sub-quantity, invented capacity or fabricated equivalent.
- **AC71 (approved)** A temporary source-glass clarification applies only to terms with no
  declared capacity, in both ingredients and steps; it never overrides a declared value; it
  is never persisted; no capacity is inherited from a linked ingredient (OD-03); competing
  declared capacities produce `clarification_required` (or `temporary_clarification` when
  one is supplied), never `ambiguous`.
- **AC72 (approved)** Target capacities come only from the signed-in person's profile;
  account A's values never affect account B, and no derived step/link data is shared or
  cached across accounts.
- **AC73 (approved)** The API change is additive and typed: existing B1 fields and statuses
  are unchanged; a client that predates B1.1 can ignore the new block; inside a
  `schema_version: 2` block the fields follow §15.8.2; a step or provenance failure does
  not remove the ingredient block; no new endpoint or required request field is
  introduced.
- **AC74 (approved)** No migration is required for the approved Option A; the schema head
  remains `0014_user_measure_overrides`; old favorites remain readable and editable.
- **AC75 (approved)** Russian UX distinguishes exact values from practical approximations;
  the profile badge reads `Указано вами`; measure fields state the millilitre unit; the
  page works on desktop and iPhone.
- **AC76 (approved)** The derived block is deterministic and idempotent for the same
  recipe, profile, clarification and protocol text: identical ordering and values, no
  writes, no dictionary-order tie-breaking.
- **AC77 (approved)** Incomplete provenance does not silently reject a recipe; the audit is
  descriptive; the owner-selected behavior is applied consistently; protocol v1 and AC49
  remain intact.
- **AC78 (approved)** B1.1 is scoped to the **Web favorite view only**. Telegram step hints
  are **not implemented in B1.1** (OD-08); any future Telegram presentation requires a
  separate scope and the same core, ownership and unresolved rules.
- **AC79 (approved)** Input limits are respected: at most 50 steps × 1000 characters and
  100 ingredients × 100 characters; scanning is linear and non-backtracking, makes no
  provider call and does not crash or backtrack catastrophically on adversarial text; a cap
  produces `incomplete`, not an error.
- **AC80 (approved)** No unresolved, ambiguous, shared, unsupported or stale link is
  presented as verified; no source capacity, ingredient identity, span or conversion is
  fabricated.

#### 15.10.2 Added AC81–AC88 (Codex amendments)

- **AC81 (approved, F01)** Only an explicit, locally supported quantity–ingredient
  construction yields `linked`. A nearby mention without that construction, the removed
  single-candidate sentence rule, or any proximity-only fallback yields `unresolved` with
  no hint.
- **AC82 (approved, F02)** Every candidate amount expression is tokenized and classified
  atomically. If any part is a range, heaped/US-cup/package/unsupported/mixed construct, an
  unbalanced or unknown term, the whole expression is rejected and no sub-quantity is
  salvaged or converted.
- **AC83 (approved, F03)** Step spans are half-open Unicode code-point offsets into the
  exact stored step, sorted and non-overlapping. The API binds them to `favorite_version`,
  an exact `step_text` echo and `text_sha256`; the page maps code points to UTF-16 before
  slicing and discards any stale, mismatched, overlapping or out-of-range annotation
  without changing the rendered text or raising an error.
- **AC84 (approved, F05/F07)** Source capacity is resolved per term: a declaration on that
  term always wins; a temporary clarification applies only to terms with no declared
  capacity; **no capacity is inherited from a linked ingredient or any other term, even when
  that ingredient declares exactly one glass capacity (OD-03)**; each source term reports
  its `capacity_origin`; no blanket recipe-level capacity is applied.
- **AC85 (approved, F09)** The API contract is complete and typed: closed enums, explicit
  nullability, exact integer fraction serialization within the safe-integer bound,
  `source_approximation`, `capacity_origin`, and safe handling of malformed or absent
  optional data. No float represents a quantity; an unrepresentable fraction is omitted,
  not silently rounded.
- **AC86 (approved, F10)** Failures are confined: a step-analysis failure leaves the
  ingredient conversions and recipe intact; a provenance failure leaves ingredients and
  steps intact; a per-step failure omits only that step; a per-span conversion failure
  omits only that conversion. No analysis failure returns a 4xx/5xx or removes the recipe.
- **AC87 (approved, F11)** Analysis limits are explicit and measurable (32 spans/step,
  128 spans/recipe, protocol input bounds); behavior at a cap is deterministic
  (`incomplete`, a counted reason and a prefix in step/start order) and suppresses only
  unsafe or incomplete derived results **without losing the original recipe text or the
  existing valid ingredient hints**; analysis is linear and non-backtracking with no
  provider/db/network call; **100 ms p95 is a performance target to be measured during
  implementation, not an already verified guarantee**.
- **AC88 (approved, F04)** The unknown-source chooser offers «Не знаю — оставить исходное
  количество без пересчета»; selecting it closes the chooser, suppresses every
  glass-dependent hint, **preserves every independent valid (declared) conversion**, leaves
  all original amounts visible, invents no capacity, persists nothing, **clears any previous
  temporary clarification for this view**, and does not block or error the recipe.

#### 15.10.3 Codex finding traceability

| Finding | Severity | Disposition | Where fixed / deferred |
|---|---|---|---|
| F01 — no proximity-only or single-candidate binding | BLOCKER | **Fixed in B1.1** | AC81, §15.5.6, TC-B11-35–37 |
| F02 — atomic tokenization, no salvaging | BLOCKER | **Fixed in B1.1** | AC82, §15.5.8, TC-B11-31–34 |
| F03 — safe/version-bound/non-overlapping spans, code-point→UTF-16, stale rejection | BLOCKER | **Fixed in B1.1** | AC83, §15.8.4, TC-B11-38–40 |
| F04 — unsafe unknown-glass option | IMPORTANT | **Fixed in B1.1** | AC88, §15.7.4, TC-B11-04 |
| F05 — declared text vs historical provenance vs temporary clarification | IMPORTANT | **Fixed in B1.1** | AC84, §15.3.7, TC-B11-41 |
| F06 — scanner capabilities described as existing B0 behavior | IMPORTANT | **Fixed in B1.1** | §15.2 new-components table, §15.5.8 |
| F07 — per-term capacity, independent declarations | IMPORTANT | **Fixed in B1.1** | AC84, §15.6.1, TC-B11-41 |
| F08 — consistency semantics | IMPORTANT | **Deferred to B1.2** | §15.19, AC63/AC64, TC-B11-20/21 |
| F09 — complete typed API, rational serialization, source approximation, capacity origin | IMPORTANT | **Fixed in B1.1** | AC85, §15.8.2–15.8.3, TC-B11-42/43 |
| F10 — failure confinement, protect ingredient hints | IMPORTANT | **Fixed in B1.1** | AC86, §15.6.4, TC-B11-44 |
| F11 — measurable limits, failure behavior, performance | IMPORTANT | **Fixed in B1.1** | AC87, §15.14, TC-B11-45 |
| F12 — contradictory conversion examples and fixtures | MINOR | **Fixed in B1.1** | §15.7.1–15.7.3, revised TC-B11-01/02/03/07/13/14/19 |

### 15.11 Realistic test cases

All expected hints assume the system target glass 250 mL unless a personal value is stated.
The table is the B1.1 suite; consistency-specific cases are retained in §15.11.3.

#### 15.11.1 Revised TC-B11-01…30

| ID | Input | Expected |
|---|---|---|
| TC-B11-01 | Ingredients: `Вода — 1,1 л`; steps: `Добавьте 500 мл воды.`, `Добавьте 600 мл воды.` | Ingredient hint `≈ 4,5 вашего стакана`; step hints `2 ваших стакана` (exact practical value, no `≈`) and `≈ 2,5 вашего стакана`; no cross-step consistency claim; no stored change |
| TC-B11-02 | Same as 01 with personal glass 200 mL | Ingredient hint `5,5 вашего стакана` (exact practical value, no `≈`); step hints `2,5 вашего стакана` and `3 ваших стакана`; no consistency claim |
| TC-B11-03 | `Вода — 1 стакан (200 мл)`; step `Добавьте 1 стакан воды.` | Ingredient `Точно: 0,8 вашего стакана`; the step does **not** inherit 200 mL (OD-03) → `clarification_required`, no step hint; with `?source_glass_ml=200` the step shows `Точно: 0,8` using `temporary_clarification`; stored text unchanged |
| TC-B11-04 | `Вода — 1 стакан`; no clarification; then choose «Не знаю — оставить исходное количество без пересчета» | No glass hint; `clarification_required`; chooser and explanation; the safe option suppresses glass-dependent hints, preserves independent declared conversions, leaves the original amount visible, invents no 200/250, persists nothing, clears any previous temporary clarification for this view, and does not block the recipe |
| TC-B11-05 | `Вода — 1 стакан`; `?source_glass_ml=200` | Hint uses 200 mL for the term (`Точно: 0,8 вашего стакана`); clarification not persisted |
| TC-B11-06 | `1 стакан (250 мл)` mixed with `1 стакан` and clarification 200 | Declared 250 stays 250 (identical, no hint); the undeclared term uses 200 (`Точно: 0,8`); no conflict |
| TC-B11-07 | Ingredient `Вода — 1 стакан (200 мл) и 1 стакан (250 мл)`; step `Добавьте 1 стакан воды.` | Each ingredient term uses its own capacity (sum ≈ 1,75 вашего стакана); the step has no declaration of its own and does **not** inherit either ingredient capacity (OD-03) → `clarification_required`, no step hint |
| TC-B11-08 | Steps containing `180 °C`, `20 минут`, `на 4 порции`, `20 см`, `50%`, `2 яйца` | No step amounts/hints for those numbers; countable remains unsupported |
| TC-B11-09 | `2–3 ст. л. масла` | `range`; no hint; no midpoint and no salvaged `3 ст. л.` |
| TC-B11-10 | `1 ст. л. с горкой` | `unsupported`; no hint; no salvaged `1 ст. л.` |
| TC-B11-11 | `1 cup молока` | `unsupported`; not read as a Russian glass; no salvaged `1` |
| TC-B11-12 | `Добавьте муки.` / `Добавьте муку, перемешайте.` | No numeric amount; nothing to convert; recipe readable |
| TC-B11-13 | `Добавьте 500 мл молока и 500 мл воды.` with both ingredients | Each amount explicitly linked to its name; two hints (`2 ваших стакана` each) |
| TC-B11-14 | `Смешайте 500 мл воды и молока.` with both ingredients | `shared`; no per-ingredient 500 mL hint |
| TC-B11-15 | `Возьмите половину воды.` / `оставьте его` / `половину от 500 мл` | The first two produce no span; the numeric `500 мл` in the third is `unresolved` with reason `anaphora`; no hint in any case |
| TC-B11-16 | Step names only one ingredient: `Добавьте 500 мл и перемешайте.` | No explicit local binding; the removed single-candidate rule does not apply → `unresolved`, no hint |
| TC-B11-17 | Step names two ingredients but amount has no local name | `unresolved`; no silent assignment |
| TC-B11-18 | `200 г муки` step with a flour ingredient | `linked` (`name_after`) but no hint: mass conversion is B2; `conversion: null`, `conversion_omitted_reason: "mass"` |
| TC-B11-19 | `около 500 мл воды` | Hint is 2 glasses but marked approximate (`≈ 2 ваших стакана`) because the source says `около` |
| TC-B11-20 | **DEFERRED to KH-F01-B1.2** — ingredient total 1 L; steps 700 + 600 mL | Retained in §15.11.3; no B1.1 behavior |
| TC-B11-21 | **DEFERRED to KH-F01-B1.2** — ingredient total 1.1 L; step 500 mL only | Retained in §15.11.3; no B1.1 behavior |
| TC-B11-22 | Two accounts with different glass capacities open the same favorite | Different hints; each sees only their own profile values; no shared cache |
| TC-B11-23 | Generated recipe with `1 стакан` (model omitted capacity) | Audit marks `undeclared`; recipe still saved/shown; no invented capacity; explanation |
| TC-B11-24 | Generated recipe with `1 стакан (237,5 мл)` | Exact fractional capacity accepted and preserved; conversion uses it exactly; no practical value at 250 mL → `Точно` |
| TC-B11-25 | Generated/user recipe with `1 пачка (400 г)` | Provenance `status: "declared"`, `kind: "package"`, `declared_content` records `400 г` (`unit: "gram"`), `capacity_ml: null`; no package-to-volume conversion; no invented number |
| TC-B11-26 | Legacy text favorite | Valid empty `ingredients`/`steps`/`provenance`; no step hints; text exactly as stored |
| TC-B11-27 | Favorite edit changes a step amount | PATCH answer recomputes links/hints; version increments; no derived data stored; old annotations stale |
| TC-B11-28 | Step with 1000 characters of numbers/units | Bounded scan; no crash; only supported volume units yield spans; no ReDoS |
| TC-B11-29 | Corrupt/out-of-range profile measure row | Measures block null or recipe readable; no clamped or invented target |
| TC-B11-30 | API client without B1.1 fields | Existing ingredient block still renders; absent `steps` is not an error |

#### 15.11.2 Added TC-B11-31…46 (Codex amendments)

| ID | Input | Expected |
|---|---|---|
| TC-B11-31 | `1,5–2 стакана молока` | `range`; one span; no midpoint; no salvaged `2 стакана` |
| TC-B11-32 | `2 ст. л. с горкой масла` | `unsupported`; no hint; no salvaged `2 ст. л.` |
| TC-B11-33 | `1 cup молока` | `unsupported`; no hint; no salvaged `1` |
| TC-B11-34 | `1 стакан (200 мл молока` (unbalanced) | `malformed_expression`; no span/hint; no salvaged `1 стакан` or `200 мл` |
| TC-B11-35 | `Добавьте 500 мл воды.` | `linked`, `name_after`; `2 ваших стакана` |
| TC-B11-36 | `Воду добавьте 500 мл.` | `linked`, `name_before_copula`; `2 ваших стакана` |
| TC-B11-37 | `Воду доведите до кипения, добавьте 500 мл.` | No explicit local binding (`доведите` breaks it) → `unresolved`; no hint even though a mention is nearby |
| TC-B11-38 | Step `Добавьте 🥛 500 мл воды 🥛.` (astral characters) | The span covers exactly `500 мл`; code-point offsets are mapped to UTF-16; the hint lands on the amount; the stored/echoed text is byte-identical |
| TC-B11-39 | `Влейте 500 мл воды и добавьте 250 мл молока.` | Two sorted, non-overlapping spans; 500 mL → 2 glasses; 250 mL → `1 ваш стакан` (no `≈`, metric source, practical value equals exact); each hint on its own amount |
| TC-B11-40 | A response for version 3 arrives after version 4, or `step_text`/span slice differs from the rendered step | All annotations for that step are discarded; original text only; no error; ingredient hints intact |
| TC-B11-41 | Ingredients `Вода — 1 стакан (200 мл)` and `Молоко — 1 стакан`; steps `Добавьте 1 стакан воды.` and `Добавьте 1 стакан молока.`; GET `?source_glass_ml=250`; target glass 250 mL | Water ingredient uses 200 (`declared_text` → `Точно: 0,8`); the water step does **not** inherit 200 (OD-03) — it uses the temporary 250 (`temporary_clarification`, identical → no hint); milk ingredient/step also use 250 (`temporary_clarification`, identical → no hint); the API reports each origin and no `inherited_from_ingredient` value exists; nothing persisted; a GET without the parameter returns water step and both milk terms to `clarification_required` |
| TC-B11-42 | New page with an old server (no `schema_version`, `steps`, `provenance`, `analysis`) | Ingredient hints render; no step hints; no error; absent optional data is tolerated |
| TC-B11-43 | `steps` containing a wrong type, `end < start`, overlapping spans, denominator 0, or an out-of-range offset | The affected step/annotation is discarded; the ingredient block and recipe still render; no exception |
| TC-B11-44 | Injected step-analysis exception | `analysis.status = "failed"`, `steps: []`; ingredient conversions and provenance intact; HTTP 200; recipe readable |
| TC-B11-45 | Maximum-size recipe (50 × 1000 chars) with more resolvable amounts than the 128-span cap, and an adversarial digit/unit step | Analysis stops at the cap; `analysis.status = "incomplete"`; `omitted_reasons.limit_exceeded > 0`; spans are a deterministic prefix (step, start order); linear bounded time; no crash |
| TC-B11-46 | Generated `1 стакан (200 мл)` → `message_recipes` JSON → save favorite copy → GET → edit another field → PATCH | The declaration is unchanged at every step; no migration; no extra protocol key; derived hints recompute without rewriting the declaration |

Cross-user isolation is covered by TC-B11-22 and AC72; the new cases complement it rather
than duplicating it.

#### 15.11.3 Deferred KH-F01-B1.2 test backlog (retained, not deleted)

These cases are **not** part of the B1.1 suite. They are retained so the rationale and
expected behavior are not lost when consistency analysis is designed.

| ID | Case | Why deferred |
|---|---|---|
| TC-B11-20 | Ingredient total 1 L; steps 700 + 600 mL → `excess`; neutral mismatch note; no amount changed | F08 consistency semantics |
| TC-B11-21 | Ingredient total 1.1 L; step 500 mL only → `partial`; no warning by default | F08 consistency semantics |
| B1.2-TC-01 | 1.1 L vs 500 + 600 mL → exact `balanced`; no claim that the recipe is validated | F08 |
| B1.2-TC-02 | Additive consumption events across several steps and partial use; no double counting | F08 / OD-04 |
| B1.2-TC-03 | `near` tolerance (if approved) vs exact comparison; status wording | OD-04 |
| B1.2-TC-04 | Comparison is `incomplete` when a linked span is unresolved/ambiguous/unsupported | F08 |
| B1.2-TC-05 | Mass or qualitative ingredient → `not_compared`; no invented conversion | F08 |
| B1.2-TC-06 | `partial`/`balanced` visibility on request vs default; no "validated" label | OD-14 |

### 15.12 Backward compatibility

- No accepted AC01–AC57 behavior is changed or narrowed.
- Protocol v1 keys and ingredient schema are untouched (AC49).
- Existing favorites, message recipes, text favorites and shopping lists are readable;
  no destructive backfill, no data rewrite.
- The B1 `measures.ingredients` shape and `source_glass_required` meaning remain; new
  fields are additive and optional. The B1 fields keep their numeric fraction encoding and
  their released meaning.
- Old front-end code ignores unknown fields; new front-end code tolerates an older server
  (no `steps`/`schema_version` → no step hints, ingredient block unchanged).
- A recipe edited before B1.1 keeps its text; the new derived block simply has no hints
  where the data is insufficient.
- Deployment order: no schema migration means the image can be rolled back without a data
  downgrade; the API addition is harmless to an older client.
- Tests that pin B1 copy must be updated only with owner approval, because the copy change
  in §15.7.6 intentionally changes user-visible wording.

### 15.13 Security, privacy and safety

- **No new external access.** Step analysis is local and deterministic; no OpenAI call, no
  external source, no network.
- **No new personal data.** The recipe and step text already belong to the favorite owner;
  the derived block is returned only to that owner. Nothing is added to shared tables.
- **Account isolation.** Target capacities are read through the session user id; no body,
  query or cross-account cache can supply them.
- **Logging.** Log only counts/statuses and the correlation id; never step or ingredient
  text, never a person's measure values.
- **Output safety.** The Web layer renders text nodes; the API returns JSON. No HTML is
  constructed from recipe text. The span contract explicitly excludes `innerHTML` and
  string interpolation into markup.
- **Input bounds.** 100 ingredients × 100 characters, 50 steps × 1000 characters; a cap on
  spans per step/recipe; the core's digit bound; no unbounded or nested-quantifier regex.
- **Unicode.** Case folding and `ё → е` for matching; whole-word matching; no NFKC folding
  that would merge distinct characters; protocol control-character checks stay in force;
  offsets are code points and are mapped before UTF-16 slicing.
- **No inference leakage.** Generic morphology/alias data contains no user recipe text; a
  shared dictionary must never be built from one person's recipes for another (AC15).
- **Clarification.** A bounded integer 50–1000; per request; never persisted; never used to
  override a declared source capacity; no capacity is inherited (OD-03).
- **Failure confinement.** A step-analysis error must not turn a readable recipe into an
  error page; it omits hints, not the recipe. Ingredient hints survive.

### 15.14 Performance, determinism and limits

Measurable limits and behavior (Codex F11):

| Limit | Value | Behavior at the limit |
|---|---|---|
| Steps per recipe | 50 (protocol) | Protocol validation already bounds the input |
| Step length | 1000 characters (protocol) | Protocol validation already bounds the input |
| Ingredients / amount length | 100 × 100 characters (protocol) | Protocol validation already bounds the input |
| Spans per step | 32 | Further spans in that step are omitted, counted in `omitted_reasons.limit_exceeded` |
| Spans per recipe | 128 | Further spans in later steps are omitted, counted; analysis is `incomplete` |
| Analysis work | linear in total step code points | No nested quantifiers, no unbounded lookaround; a candidate is consumed once |

- No provider call, database write or external lookup on a GET.
- The same recipe, profile, clarification and protocol text produce the same block in the
  same order. Dictionary iteration is not used to decide ties; ties become `ambiguous`.
- A pathological step (many digits, many units) is bounded by the core's number limit, the
  per-step/per-recipe span caps and a single left-to-right pass; it degrades to
  `incomplete`/no hint rather than an exception.
- **Limit-exhaustion rule.** Reaching the 32-span per-step cap or the 128-span per-recipe cap
  suppresses only unsafe or incomplete **derived** results. It never truncates, rewrites or
  removes the original recipe content, and it never nulls or empties the existing valid
  ingredient hints. Remaining spans are omitted in deterministic step/start order and
  counted in `omitted_reasons.limit_exceeded`; `analysis.status` becomes `incomplete`.
- **Performance target, not a verified guarantee.** 100 ms at p95 on the reference
  deployment for a maximum-size favorite (50 × 1000 characters) is an implementation
  **target to be measured during implementation**. It is **not** an ensured or independently
  verified guarantee at this specification stage. Implementation tests should assert linear
  behavior and a generous wall-clock cap rather than a precise benchmark. If the measured
  target is not met, the block must degrade (omit step hints) rather than delay the recipe.
- The API payload grows with the number of spans; the 128-span cap keeps the worst case to
  roughly 100 KB including exact step echoes, and a typical favorite remains a few
  kilobytes.
- No background worker, scheduler or cache is introduced.

### 15.15 Suggested implementation stages (approved scope; implementation not authorized)

| Stage | Content | Tests / exit criteria |
|---|---|---|
| **B1.1-A — Provenance contract** | Prompt content rule; pure provenance audit; per-term declared/incomplete statuses and origins; no schema change | AC58–AC60, AC77, AC84; parser/protocol regression unchanged; audit fixtures for declared/undeclared/invalid/package/metric |
| **B1.1-B — Scanner and explicit binder** | New span scanner and atomic expression grammar; context classification; inflection/alias resolver; explicit local binding; span safety; no API/UI | AC61–AC62, AC65–AC70, AC76, AC79, AC81–AC83, AC87; fixture corpus of realistic Russian steps; no-salvage and false-link review |
| **B1.1-C — Adapter and typed API** | Extend `web.measure_targets` and the additive typed `measures` block; per-term capacity origins (no inheritance); fraction safety; failure confinement; clarification applies to both lists | AC62, AC71–AC74, AC83–AC86; API compatibility; no-write tests; stale/Unicode tests |
| **B1.1-D — Web UX and copy** | Step hint rendering; stale/overlap rejection; unknown-source explanation and safe `Не знаю`; profile badge/field labels | AC65, AC68, AC70, AC75, AC88; desktop/iPhone visual checks; text-node safety |
| **B1.1-E — Web acceptance and release readiness** | Web owner smoke on desktop/iPhone; documentation release record. **No Telegram step hints** (OD-08) | AC78; Web acceptance; no regression in favorites ownership |

Each stage is independently reviewable. **No stage includes a migration or a commit** under
this design authorization; implementation requires a separate owner-approved task. B1.2 is
not a stage of B1.1.

### 15.16 Product decisions — OWNER APPROVED (v0.6)

The KH-F01-B1.1 decisions below are **OWNER APPROVED** as recorded in v0.6. OD-04 and OD-14
are **not** B1.1 decisions; they remain deferred to KH-F01-B1.2 (§15.19).

#### 15.16.1 Approved decision matrix

| ID | Decision | OWNER-APPROVED outcome |
|---|---|---|
| **OD-01** | Provenance carrier | **Option A**: declarations in the amount text; no migration; survives copy/edit; visible and testable |
| **OD-02** | A newly generated recipe omits a required declaration | **Keep with an "unknown source" explanation**; never reject the structured block; never invent a capacity |
| **OD-03** | May a step's undeclared glass inherit a linked ingredient's declared capacity? | **NO AUTOMATIC INHERITANCE.** Never inherit, even when the ingredient declares exactly one glass capacity. Require an explicit step declaration or a temporary user clarification; never silently infer capacity |
| **OD-05** | May a sentence that names exactly one ingredient link an amount that is not locally bound? | **No.** Uniqueness-only / single-candidate binding is removed; the result is `unresolved` until an explicit local construction exists |
| **OD-06** | Coordinated/shared amount (`500 мл воды и молока`) | **`shared`, no per-ingredient hint**; never attribute the full amount to each |
| **OD-07** | Source words `около/примерно/приблизительно` | **Yes**: force an approximate presentation for step spans; arithmetic unchanged, wording honest |
| **OD-08** | Telegram step hints in B1.1? | **No.** B1.1 is Web Favorites only; **there is no Telegram step-hint implementation in B1.1** |
| **OD-09** | Badge, field labels and unknown-source copy | Badge «Указано вами»; explicit millilitre field labels («Объем моего стакана, мл», and analogous spoon labels); explanation as in §15.7.4 |
| **OD-10** | Safe unknown option | **Yes**: «Не знаю — оставить исходное количество без пересчета» |
| **OD-11** | Metric vs household generation | **Either**, but a household measure without a declaration is incomplete; metric preferred where natural; never invent a source capacity when transcribing |
| **OD-12** | Read-side feature flag | **No mandatory new flag**: fields are additive; the UI degrades to no hints; a flag only if a staged rollout is required |
| **OD-13** | Which non-glass measures require a declaration | Glass and package-like net content (jars/bags/packages); countable pieces and qualitative terms do not; US cups stay unsupported; never invent missing net content or a conversion coefficient |

**Deferred:** OD-04 (exact vs tolerance/`near` comparison) and OD-14 (default visibility of
`partial`/`balanced`) remain **KH-F01-B1.2** decisions (§15.19). They are not part of B1.1.

#### 15.16.2 Resolved implementation questions

The v0.5 open trade-offs are resolved for the implementation task; only genuinely residual
items remain as notes:

1. **OD-03** is resolved to **no automatic inheritance**. The `inherited_from_ingredient`
   origin is removed from the B1.1 contract; an undeclared step glass requires a temporary
   clarification.
2. **OD-08** is resolved to **Web Favorites only**. No Telegram step-hint stage exists in
   B1.1.
3. The closed binding construction list (§15.5.6) is frozen as approved; expanding it
   increases recall but also false-link risk and requires a new decision.
4. **Residual:** ingredient-level approximation words. B1.1 handles `около/примерно` only
   in step spans; an ingredient amount beginning with one keeps B1 behavior (no hint). This
   is a recorded residual, not part of B1.1.
5. `schema_version: 2` and the typed contract are approved; they version **only** the
   additive measure-annotation payload (§15.8.2), not recipe protocol v1, and need no
   migration.
6. Span caps are **32 per step** and **128 per recipe**; **100 ms p95 is a performance
   target** to be measured during implementation, not a verified guarantee (§15.14).
7. The «Не знаю» choice is view-scoped, not persisted, and clears any previous temporary
   clarification (§15.7.4).

### 15.17 Risks and tradeoffs

- **Recall vs honesty.** The deterministic binder will miss paraphrases that an LLM would
  catch, and the removed single-candidate rule intentionally reduces recall. That is
  accepted: a missing hint is better than a confident wrong ingredient. The unresolved
  reasons allow a later, separately reviewed improvement.
- **Provenance in prose.** Option A is not a machine-separated schema; an audit and parser
  must stay aligned. The benefit is no migration and full edit/copy compatibility. A future
  v2 can add a structured field without invalidating these strings.
- **No inheritance, deliberately lower recall.** OD-03 forbids a step from borrowing an
  ingredient's declared capacity, so some step hints a reader might expect will require a
  temporary clarification. That is accepted: the step text must state its own capacity or
  the person supplies one. The per-term precedence and "no automatic inference" rule keep a
  step from silently acquiring a capacity.
- **Curated morphology.** Coverage depends on the table; unknown forms stay unresolved.
  This is auditable and can grow, but it is not a general Russian morphological analyzer.
- **Span and Unicode complexity.** Code-point offsets, UTF-16 mapping, version binding and
  stale rejection add client work. The mandatory discard-on-mismatch rule keeps a mistake
  from shifting a hint onto the wrong text, at the cost of occasionally dropping a valid
  hint.
- **Payload and UI complexity.** Additive step/provenance data increases the response and
  the page code; the caps keep it bounded, and the UI must not fail when it is absent.
- **Copy change touches pinned tests.** The B1 badge/label change will require owner-
  approved test updates; no test may be changed under this design task.
- **No migration is deliberate.** If the owner later wants structured provenance, the
  migration and dual-read plan become a separate, larger project.
- **Consistency is deferred, not forgotten.** B1.1 users receive no quantity-consistency
  report. That is a product decision recorded in §15.19, not an omission.

### 15.18 Approved recommendation and next action

**Approved scope (v0.6):** Option A (in-amount declarations), the new deterministic scanner
with atomic expression rejection, explicit local binding only, per-term capacity origins
with **no automatic inheritance**, read-time step hints, safe Unicode/version-bound spans,
no migration, and the copy correction. This keeps the released B0/B1 contracts intact, adds
no external dependency or provider call, makes every uncertain case honest, and contains no
consistency fields. B1.1 is **Web Favorites only** (OD-08).

**Next action:** a separate owner decision authorizing a bounded **B1.1-A** implementation
task. The product decisions are approved, but no implementation, migration, test, commit,
push or deployment is authorized by this document. KH-F01-B1.2 remains a separate, deferred
future scope (§15.19) and is not a prerequisite.

### 15.19 KH-F01-B1.2 — deferred future scope (backlog)

**Status: BACKLOG — DEFERRED. Not part of B1.1; not authorized; not an implementation
dependency of B1.1.**

**Product question.** An earlier proposal compared the ingredient-list totals with the
sum of amounts used in preparation steps, labelled a recipe `balanced`/`partial`/`excess`/
`near`, and reported completeness. Codex finding F08 found the semantics under-specified
(additive consumption events, repeated use, partial use, tolerance, what "complete" means).
The owner has split this out so B1.1 can ship honest provenance and step hints without dead
fields or unresolved semantics.

**Deferred scope (future product work):**

- cross-comparison of ingredient totals and step usage totals, including repeated and
  partial use;
- additive consumption-event interpretation (the same ingredient used in several steps);
- `balanced`, `partial`, `excess`, `near`, `incomplete`, `unresolved`, `not_compared` and
  equivalent consistency statuses;
- completeness and quantity-consistency reports;
- consistency API fields, UX notices and acceptance tests;
- the deferred decisions **OD-04** (exact vs tolerance/`near`) and **OD-14** (default
  visibility of `partial`/`balanced`).

**Retained rationale and identifiers:**

- AC63 and AC64 remain their original consistency meanings; they are not reused for a
  different requirement.
- TC-B11-20 and TC-B11-21 remain in §15.11.3 with their original expectations.
- B1.2-TC-01…06 record the additional semantics to be designed later.

**Entry condition.** B1.2 may be drafted only after the **B1.1 implementation** is accepted
and only with a separate owner authorization. B1.1 must not add a placeholder `consistency`
field, a "coming soon" status, or any other dead consistency structure in anticipation of
B1.2.


## Appendix. Public source references (desk-reviewed during Product Discovery)

1. NIST, *Metric Kitchen: Cooking Measurement Equivalencies*: https://www.nist.gov/pml/owm/metric-si/metric-kitchen/metric-kitchen-cooking-measurement-equivalencies
2. FDA, *Guidelines for Determining Metric Equivalents of Household Measures*: https://www.fda.gov/regulatory-information/search-fda-guidance-documents/guidance-industry-guidelines-determining-metric-equivalents-household-measures
3. USDA FoodData Central, *API Guide / licensing*: https://fdc.nal.usda.gov/api-guide/
4. USDA, *Foundation Foods Documentation*: https://fdc.nal.usda.gov/Foundation_Foods_Documentation/
5. USDA, *Data Type Documentation*: https://fdc.nal.usda.gov/data-documentation/
6. USDA, *Download Datasets*: https://fdc.nal.usda.gov/download-datasets/
7. King Arthur Baking, *Ingredient Weight Chart*: https://www.kingarthurbaking.com/learn/ingredient-weight-chart
8. King Arthur Baking, *How to Measure Flour*: https://www.kingarthurbaking.com/videos/baking-skills/how-to-measure-flour
9. Food Standards Australia New Zealand, *AFCD Data Files*: https://www.foodstandards.gov.au/science-data/food-nutrient-databases/afcd/data-files
10. Food Standards Australia New Zealand, *Data User Licence*: https://www.foodstandards.gov.au/science-data/monitoringnutrients/afcd/datauserlicenceagreement
