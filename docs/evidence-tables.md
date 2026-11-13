# Evidence tables and agent orchestration

Stage one extracts and presents findings. The report is tables; it does not select a planning
number, transform effects, transport populations, or pool estimates. Stage two is specified in
[the synthesis blueprint](synthesis-blueprint.md).

## Tables

| Table | Row identity | Columns |
|---|---|---|
| Population-specific findings | Source population × analysis set/subgroup × contrast × outcome/time × native target | Population, comparison, outcome/time, parameter, report/analysis IDs, exact values and uncertainty, combination status, combined effect (not calculated) |
| Analysis extraction | Report ID + extracted result index | Population, formulation, parameter definition, reported values, verification, unresolved information |
| Mathematical feasibility | Proposed set of analysis IDs | Desired population/parameter, differences, symbolic mapping, inputs/availability, assumptions/references, dependence, decision and validation notes |
| Reported values | Value ID | Analysis, group, statistic, printed value, stated CI level, meaning, source passage, source check |
| Field evidence | Analysis ID + field path | Value, reported/reconstructed/unclear/not-reported/conflicting status, quote/location, derivation, source check |

All tables have CSV downloads. The project JSON preserves the structured profiles, audit attempts,
raw table plan, validated table decisions, value IDs and source passages. Numeric values are looked
up from the extraction; table-planning agents cannot supply a pooled result. The original flat
CSV remains available for existing integrations.

An analysis is split when contrast, subgroup, fitted model, adjustment, outcome, time or analysis
set changes. `R<paper-id>:<zero-based-result-index>` is its identity within an extraction version.
Re-extraction invalidates the report basis; IDs are not longitudinal identifiers across versions.
Reports from the same participants remain linked by the existing overlap check.

## Structured extraction

`ResultContext.profile` is an `AnalysisProfile` with three typed components:

| Component | Fields |
|---|---|
| Population | Intended population, eligibility, recruited sample, analysis set, subgroup, setting, sample sizes, covariate distribution |
| Formulation | Design, equation, treatment, comparator, outcome, time horizon, adjustment/coding/interactions, dependence/random effects, estimation, missingness/censoring, intercurrent events |
| Target | Scientific estimand, reported native parameter, coefficient mapping, conditioning, interpretation, scale, units, direction |

Each field is a `SourceFact(value, status, quote, location, rationale)`. A reconstructed field needs
its derivation and source support. Missing means missing, not a conventional default. A partial
model stays partial. Eligibility does not determine the covariate distribution. A scientific causal
question does not make the fitted coefficient a causal estimand. A fitted adjusted model may report
a marginal standardized contrast. Source support is not a formal risk-of-bias assessment.

## Orchestration implemented in both editions

| Order | Role / operation | Input | Output / routing |
|---|---|---|---|
| 1 | Search manager and source retrieval | Study specification | Papers, supplements when retrieved, registry records; retained source text |
| 2 | Extraction worker | Source and specification | Separate analyses and printed numbers with quotes and table context |
| 3 | Deterministic checks; escalation manager | Values and original source | Quote/numeric/plausibility checks; existing flag-driven or sampled re-extraction |
| 4 | Statistical interpreter (`context`) | Final extraction and the same source | Population/formulation/parameter profile for every analysis |
| 5 | Semantic auditor (`context_audit`) | Original source, extraction, proposed profile in a separate call | One verdict per analysis; field-specific issues, source ambiguity, association and mathematical interpretation checks |
| 6 | Bounded repair | Fixable audit findings or invalid source fields | At most one revised reading and one repeat audit; no infinite self-review loop |
| 7 | Overlap manager | Report identities and extracted study information | Duplicate studies, reviews/primary studies and possible overlapping participants |
| 8 | Table planner (`summarize`) | Retained analyses, profiles, checks, original values and overlap | Population groups and mathematical feasibility cells using existing analysis IDs |
| 9 | Deterministic table builder | Proposed plan and retained evidence | Complete analysis coverage; conservative status validation; native-population findings; no calculated combined effect |

Interpreter, auditor and planner are distinct calls using the configured manager model/provider;
this is separation of responsibilities, not a claim of statistically independent model errors.
Numeric extraction retains the configured worker, concurrency limits, source fallback and error
handling. Source retrieval is not an agent's arbitrary new browsing permission.

A source cannot repair information it never reported. An audit can therefore return
`insufficient_information`; supported partial fields stay visible. Authentication/quota errors keep
the completed extraction and stop further calls as before. Audit failures remain recorded and cannot
approve a repaired reading using an earlier audit. Audit history records both attempts and their
source checks. Human inclusion/exclusion remains optional: undecided analyses are explicitly marked.

## Combination decisions

| Status | Meaning |
|---|---|
| DIRECT SYNTHESIS | Compatible native definitions, source/audit checks and usable uncertainty; no synthesis is calculated |
| TRANSFORM THEN SYNTHESIZE | Proposed symbolic mapping, methodological reference and inputs are supplied; mathematical execution is deferred |
| ASSUMPTION-DEPENDENT | Proposed transport/approximation depends on explicit assumptions and sensitivity requirements |
| INSUFFICIENT INFORMATION | Missing input, interpretation, uncertainty, mapping or dependence information |
| KEEP SEPARATE | Different targets without a justified common mapping |

Automatic gates are deliberately conservative. Matching normalized definitions is not proof of
scientific equivalence; that also requires the agent's source interpretation and reasoning. Differences
or missing fields cannot be labeled numerically small. Known overlap is not resolved merely by writing
"independent". Multiple estimates from one report need a covariance/selection strategy; they are not
independent studies. A review and its constituent trials are not independent corroboration.

The population findings table is partitioned from native definitions, independently of the planner's
labels. Unknown critical definitions retain separate rows. The feasibility table may discuss a proposed
mapping across those rows, but does not merge their original populations or estimates.

## Compatibility and validation

Legacy context strings and the old numerical planning engine remain readable for saved projects and
integrations. New report generation renders tables even when a legacy model response provides no table
plan: every retained analysis gets a conservative fallback row. Old population prose is not promoted to
a verified structured profile; use the existing context re-read action on the original saved source.
Changes to extraction, decisions, profiles, audit checks or target specification mark reports stale.

Tests cover source/semantic disagreement, bounded repair, missing audits, different populations with
identical estimates, duplicate IDs and omitted analyses, overlap, unknown CI level, unrelated uncertainty,
Python/JavaScript parity, rendering and exports. Synthetic fixtures establish software behavior only.
Real-paper accuracy and grouping judgments still require an annotated evaluation corpus; no live model
quality or clinical validity is asserted by these tests.
