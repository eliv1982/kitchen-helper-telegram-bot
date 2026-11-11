# House Helper / Kitchen — KH-F01 Smart Measures & Portion Scaling

**Product Specification — Consolidated Product Baseline v0.3 (owner review)**\
**Date:** 11 November 2026\
**Scope:** KH-F01 only\
**Status:** D01–D19 approved as product decisions; KH-F01-A deterministic measurement core accepted internally (`c4f852c`, not deployed); KH-F01-B0 technical design approved; **implementation of KH-F01-B0/B1/B2 NOT authorized**.

## 0. Project baseline and authority

- Product: **House Helper**; Kitchen is the first module, served from the site root. The mom-facing interface, buttons, explanations and errors are **Russian**; brand remains in English.
- Production as reported by owner: `https://house.elivcloud.org`, `eliv1982/kitchen-helper-telegram-bot`, production `HEAD 59d1b806099d86a0a2037e7f6a5ecd1ca9756010`, image `house-helper:59d1b806099d`, schema `0013_user_text_size`; two accounts, data preserved. Repository `main` has since advanced to `c4f852c` (KH-F01-A measurement core, internal accept); that commit is **not** deployed.
- Previously released: KH-SHOP-01, KH-PROFILE-01, KH-PROFILE-01A-FIX-01; no reopening without new reproducible defect.
- Ops: first normal purge/backup timer runs on 11 November 2026 still need separate evidence; not part of this KH-F01 product work.
- Workflow: Lena + Teya decide product logic; Claude Code implements **only after approval**; Codex independently audits; Lena personally commits/pushes; Cursor Agent handles VPS/deploy; Lena performs owner smoke and final acceptance.
- This document reflects the owner's product decisions D01–D19 and prior desk review of public reference sources. Codex separately inspected local repository at the reported HEAD; no code/database/production changes, tests, live datasets or APIs were executed during that preflight. Production claims remain owner-reported.

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
| **KH-F01-B0 — Calculation Core Extension** | `MeasureVolumes`; source/target measurement contexts; configurable household volumes; target default resolver; opt-in parser support for nonstandard declared glass capacities (`allow_nonstandard_glass_sizes=True`); validation and deterministic calculations; backward-compatible public contracts; focused tests and existing regression suite | Technical design **APPROVED**; implementation **NOT AUTHORIZED**. Must preserve legacy calls and AC01–AC50 exactly: default `False` keeps 200/250 parsing, personal capacities affect target measures only, and an undeclared source glass is never resolved from personal settings or the system default. |
| **KH-F01-B1 — Profile Integration** | «Мои кухонные меры» settings for personal **target** capacities; persistence and account isolation; default restoration; Web integration; integration with the B0 calculation contract; user-facing validation and clarification UX | Depends on the approved B0 contract. Any persistence/migration design is reviewed separately; no personal values are seeded or migrated; these settings never influence source-recipe interpretation. |
| **KH-F01-B2 — Qualified Ingredient Reference Data** | Verified ingredient-dependent coefficients; practical kitchen equivalents; reliability and uncertainty safeguards; initial curated reference baseline | Individual coefficients still pass the D05–D07/D11/D17 evidence gates before use; the current reference-data baseline remains research, not runtime data. |
| **KH-F01-C — scaling, actions, durable variants** | D08/D09/D13–D16; consistent ingredient list and step text, alternatives, serving-range clarification, server-validated separate saved variants, correct shopping-list snapshots | Before any release claiming scaling+save completion, test ownership, idempotency, catalogue versions, legacy recipes and both channels. |
| **KH-F01-D — controlled automatic enrichment** | Candidate fetch/evidence, deterministic verification, source independence, auto-promotion gates, suspension/revalidation and safe fallback | Separate authorization and security/ops review; D17 auto-promotion only after allowlist, test corpus and failure cases are independently audited. |

The B0/B1/B2 split replaces the former single F01-B phase: B0 is the calculation contract, B1 the profile experience, B2 the qualified data. Phases can be independently reviewed and staged. **KH-F01 fully complete** only after committed in-scope requirements are shipped and accepted; partial releases must be labeled partial and never claim full D01–D19 completion.

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

D01–D19 **product decisions approved**. KH-F01-A is complete (internal accept, committed at `c4f852c`, not deployed). The KH-F01-B0 technical design is **approved**; implementation of KH-F01-B0, B1 and B2 is **NOT authorized**, not started, not committed, not released and not deployed. This v0.3 revision is documentation only; no implementation, migrations, database access, background workers, pushes or production changes are authorized by it. The **next single action** is owner review and acceptance of this specification update, followed by authorization of a bounded KH-F01-B0 task (no automatic start).

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
