"""Structured outputs exchanged with Claude.

Every field is required (nullable where a value can be absent) so the schemas stay within
structured-output limits, and every reported number carries the verbatim text it came from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Design = Literal[
    "parallel_rct",
    "cluster_rct",
    "crossover_rct",
    "single_arm",
    "cohort",
    "case_control",
    "cross_sectional",
    "other",
]
OutcomeType = Literal["continuous", "binary", "count", "time_to_event", "ordinal", "unclear"]


class StatedValue(BaseModel):
    parameter: str = Field(description="What the value is, e.g. 'expected reduction in PHQ-9'.")
    value: str = Field(description="The value as written, e.g. '3 points' or '20%'.")
    quote: str = Field(description="The sentence from the paragraph that states it, verbatim.")


Statistic = Literal[
    "n",
    "mean",
    "sd",
    "se",
    "ci_lower",
    "ci_upper",
    "median",
    "q1",
    "q3",
    "min",
    "max",
    "events",
    "percent",
    "mean_change",
    "sd_change",
    "mean_difference",
    "standardized_mean_difference",
    "odds_ratio",
    "risk_ratio",
    "hazard_ratio",
    "risk_difference",
    "p_value",
    "t_statistic",
    "icc",
    "correlation",
    "other",
]


ComparatorKind = Literal["placebo", "waitlist", "usual_care", "no_intervention", "active", "other"]
# What a screening rule is about, and which concept a group of search terms describes.
Element = Literal["population", "intervention", "comparator", "outcome", "design", "other"]
Concept = Literal["population", "condition", "intervention", "comparator", "outcome", "design", "other"]


class Population(BaseModel):
    description: str = Field(description="Who is studied, in a short phrase.")
    condition: str = Field(description="The condition or severity that defines them; empty if none.")
    age: str = Field(description="Age range or group; empty if not stated.")
    setting: str = Field(description="Where, e.g. universities or primary care; empty if not stated.")


class Intervention(BaseModel):
    name: str = Field(description="What is tested (or the exposure), in a short phrase.")
    delivery: str = Field(description="How it is delivered, e.g. smartphone app or group sessions; empty if not stated.")
    dose: str = Field(description="How much and how often, e.g. 10 minutes a day; empty if not stated.")
    duration: str = Field(description="For how long, e.g. 12 weeks; empty if not stated.")


class Comparator(BaseModel):
    name: str = Field(description="What it is compared with, in a short phrase; empty for a single-arm study.")
    kind: ComparatorKind


class Outcome(BaseModel):
    name: str = Field(description="What is measured, e.g. depressive symptoms.")
    measure: str = Field(description="The instrument or definition, e.g. PHQ-9 total score (0-27); empty if not stated.")
    role: Literal["primary", "secondary"]
    type: OutcomeType
    timepoint: str = Field(description="When it is measured, e.g. 12 weeks after randomization; empty if not stated.")


class NeededStatistic(BaseModel):
    outcome: str = Field(description="Which outcome, by its name in outcomes.")
    statistics: list[Statistic] = Field(description="What to find, e.g. ['mean', 'sd', 'n'].")
    per: Literal["each_arm", "between_groups", "one_group"] = Field(
        description="each_arm: a value per arm; between_groups: a contrast such as a mean difference or hazard "
        "ratio; one_group: a single group, e.g. the control group's event rate or a correlation."
    )
    timepoint: str = Field(description="When; empty if any timepoint will do.")
    purpose: str = Field(description="What it is for in the sample-size calculation, in a few words, e.g. 'the SD'.")


class Criterion(BaseModel):
    about: Element
    rule: str = Field(description="One short rule, e.g. 'randomized trials'.")


class TermGroup(BaseModel):
    concept: Concept
    terms: list[str] = Field(description="Synonyms, MeSH headings and instrument names for this concept.")


class PlanningContext(BaseModel):
    objective: str = Field(description="Superiority, noninferiority, equivalence, estimation, or unknown.")
    effect_measure: str = Field(description="Primary contrast, scale, units and direction, if specified.")
    analysis: str = Field(description="Planned primary analysis, including adjustment, if specified.")
    analysis_population: str = Field(description="Target analysis population, if specified.")
    intercurrent_events: str = Field(description="How discontinuation, rescue treatment or death are handled. Do not infer a strategy.")
    clinical_importance: str = Field(description="Investigator-supplied important difference and stakeholder rationale, or empty.")
    clinical_importance_source: str = Field(description="Exact supporting sentence from the paragraph, or empty. Never invent stakeholder agreement.")


class SearchSpec(BaseModel):
    """What the planned study is and what the literature should supply."""

    question: str = Field(description="One-sentence restatement of the planned comparison.")
    design: Design
    population: Population
    intervention: Intervention
    comparator: Comparator
    outcomes: list[Outcome] = Field(description="Primary outcome first.")
    statistics_needed: list[NeededStatistic] = Field(
        description="What the literature should supply for the sample-size calculation, one row per outcome, "
        "grouping and timepoint."
    )
    stated_values: list[StatedValue]
    inclusion_criteria: list[Criterion]
    exclusion_criteria: list[Criterion]
    search_terms: list[TermGroup] = Field(description="One group per concept.")
    open_questions: list[str] = Field(description="Ambiguities the investigator should resolve.")
    planning: PlanningContext | None = Field(description="Clinical planning context. Null for a legacy specification.")

    @model_validator(mode="before")
    @classmethod
    def legacy_planning(cls, data):
        if isinstance(data, dict) and "planning" not in data:
            return {**data, "planning": None}
        return data


def upgrade_spec(spec: dict) -> dict:
    """A specification saved before the structured fields, in the current shape. Mirrors upgradeSpec
    in site/lib/spec-panel.js."""
    if isinstance(spec.get("population"), dict):
        return {"planning": None, **spec}
    timepoint = spec.get("timepoint") or ""
    outcomes = [
        {"name": name, "measure": "", "role": "secondary" if i else "primary",
         "type": spec.get("outcome_type") or "unclear", "timepoint": timepoint}
        for i, name in enumerate(spec.get("outcomes") or [])
    ]
    first = outcomes[0]["name"] if outcomes else ""
    return {
        "planning": None,
        "question": spec.get("question") or "",
        "design": spec.get("design") or "other",
        "population": {"description": spec.get("population") or "", "condition": "", "age": "", "setting": ""},
        "intervention": {"name": spec.get("intervention") or "", "delivery": "", "dose": "", "duration": ""},
        "comparator": {"name": spec.get("comparator") or "", "kind": "other"},
        "outcomes": outcomes,
        # The old free-text rows keep their wording as the purpose.
        "statistics_needed": [
            {"outcome": first, "statistics": [], "per": "each_arm", "timepoint": timepoint, "purpose": text}
            for text in spec.get("statistics_needed") or []
        ],
        "stated_values": spec.get("stated_values") or [],
        "inclusion_criteria": [{"about": "other", "rule": r} for r in spec.get("inclusion_criteria") or []],
        "exclusion_criteria": [{"about": "other", "rule": r} for r in spec.get("exclusion_criteria") or []],
        "search_terms": [{"concept": "other", "terms": list(spec["search_terms"])}] if spec.get("search_terms") else [],
        "open_questions": spec.get("open_questions") or [],
    }


# The search runs from the top down: reviews first, then primary studies in the target population,
# then those whose full text reports the outcome (Europe PMC), then wider ones. Trials cited by the
# included reviews are added after screening. Every level but full_text searches PubMed.
Level = Literal["reviews", "primary", "full_text", "wider", "outcome_data"]
LEVELS: tuple[str, ...] = ("reviews", "primary", "full_text", "wider", "outcome_data")
# How each way of finding a record is named on the page; "cited", "registry" and "named" are added by code.
LEVEL_NAMES = {
    "reviews": "Reviews and meta-analyses",
    "primary": "Studies in the target population",
    "full_text": "Outcome reported in the full text (Europe PMC)",
    "wider": "Studies in related populations",
    "outcome_data": "Outcome data in the target population",
    "cited": "Cited by the included reviews",
    "registry": "ClinicalTrials.gov",
    "named": "Named by the search agent",
}


class SearchLevel(BaseModel):
    level: Level = Field(
        description="reviews: systematic reviews and meta-analyses; primary: studies of the planned design in the "
        "target population; full_text: studies whose full text names the outcome, searched in Europe PMC; wider: "
        "the primary level with its narrowest concept relaxed; outcome_data: studies reporting the outcome in the "
        "target population, any design."
    )
    query: str = Field(description="The query for this level: Europe PMC syntax for full_text, PubMed for the others.")
    why: str = Field(description="One sentence on what this level adds.")


class SearchPlan(BaseModel):
    levels: list[SearchLevel]
    clinicaltrials_queries: list[str]
    extra_ids: list[str]
    summary: str


class ScreenDecision(BaseModel):
    decision: Literal["include", "exclude", "unsure"]
    reason: str = Field(description="One sentence.")
    study_type: Literal[
        "rct",
        "systematic_review_or_meta_analysis",
        "observational",
        "pilot_or_feasibility",
        "protocol_without_results",
        "other",
    ]
    reports_relevant_outcome: Literal["yes", "no", "unclear"]


class ReportedValue(BaseModel):
    group: str = Field(
        description="Arm label as the paper names it, or 'A vs B' for a between-group contrast, or 'overall'."
    )
    statistic: Statistic
    value: float = Field(description="The number exactly as printed (percent as 23.5, not 0.235).")
    ci_level: float | None = Field(
        gt=0, lt=100,
        description="For ci_lower/ci_upper only: the reported confidence level as a percent (e.g. 90, 95, 99). "
        "Null for other statistics or when the level is not stated. Never assume 95%.",
    )
    describes: str = Field(
        description="What the number is, e.g. 'PHQ-9 at 12 weeks', '95% CI of the mean difference'."
    )
    quote: str = Field(description="Verbatim text from the document that contains this number.")
    location: str = Field(description="Where it is: 'Table 2', 'Abstract', 'Results, paragraph 3'.")

    @model_validator(mode="before")
    @classmethod
    def legacy_confidence_level(cls, data):
        # Existing saved extractions have no structured confidence level. Keep that unknown while
        # requiring new model outputs to explicitly supply the nullable field in their JSON schema.
        if isinstance(data, dict) and "ci_level" not in data:
            return {**data, "ci_level": None}
        return data


class OutcomeResult(BaseModel):
    outcome_measure: str
    timepoint: str
    analysis_population: Literal["itt", "modified_itt", "per_protocol", "completers", "unclear"]
    adjusted: Literal["adjusted", "unadjusted", "unclear"]
    relevance: Literal["direct", "partial", "indirect"] = Field(
        description="direct: same outcome and timepoint as the spec; partial: related; indirect: different."
    )
    values: list[ReportedValue]
    confidence: Literal["high", "medium", "low"]
    concerns: list[str] = Field(
        description="e.g. 'SD may be SE', 'values read from a figure', 'multi-arm trial', 'change-score SD'."
    )
    outcome_type: OutcomeType = Field(description="Type of the extracted outcome, not merely the planned outcome.")
    independent_groups: bool | None = Field(description="True only for independent parallel participant groups; false for clustered, paired or crossover groups; null if unclear.")

    @model_validator(mode="before")
    @classmethod
    def legacy_design(cls, data):
        if isinstance(data, dict):
            return {"outcome_type": "unclear", "independent_groups": None, **data}
        return data


class AppraisalItem(BaseModel):
    domain: Literal["randomization", "deviations", "missing_outcomes", "measurement", "selective_reporting",
                    "confounding", "clinical_importance", "estimand"]
    assessment: str = Field(description="What the quoted information implies, including limitations. Not a formal risk-of-bias score.")
    quote: str = Field(description="Verbatim supporting text. Omit the item when no supporting text is available.")
    location: str


class PaperExtraction(BaseModel):
    study_design: str
    population: str
    groups: list[str] = Field(description="Each arm with its randomized or analyzed size, as reported.")
    matches_spec: bool
    mismatch_reasons: list[str]
    results: list[OutcomeResult]
    notes: str
    appraisal: list[AppraisalItem] = Field(description="Source-supported methodological and clinical-importance information; empty if unavailable.")

    @model_validator(mode="before")
    @classmethod
    def legacy_appraisal(cls, data):
        if isinstance(data, dict) and "appraisal" not in data:
            return {**data, "appraisal": []}
        return data


class OverlapGroup(BaseModel):
    records: list[str] = Field(description="The records involved, by key (R12, R40).")
    relation: Literal["same_study", "review_includes_study", "reviews_share_studies", "possible_overlap"] = Field(
        description="same_study: reports of one study (a registry record and its paper, duplicate or secondary "
        "publications); review_includes_study: a review whose pooled estimate includes a study also in the set; "
        "reviews_share_studies: reviews that pool some of the same studies; possible_overlap: suspected, unconfirmed."
    )
    evidence: str = Field(description="What shows the overlap: shared registration number, citation, authors, sample sizes.")
    use: str = Field(
        description="The key of the record whose numbers to use for this group, or empty when they can be used "
        "together or it can't be decided from the evidence."
    )


class OverlapReport(BaseModel):
    groups: list[OverlapGroup]
    notes: str = Field(description="Anything else about double counting; empty if nothing.")


class NeedSummary(BaseModel):
    need: str = Field(description="The effect size to find, as the specification describes it.")
    values: list[str] = Field(
        description="Keys of the reported values that bear on it (V3, V7), the most directly relevant first; empty if none."
    )
    comparison: str = Field(
        description="How these values compare: how closely each matches the plan, how much they vary, the size and "
        "reporting of their studies, shared participants. Values by key in brackets, e.g. [V3]."
    )


class EffectChoice(BaseModel):
    method: Literal["reported", "mean_difference", "cohen_d", "risk_difference", "risk_ratio", "odds_ratio"]
    inputs: list[str] = Field(description="Value keys in exact order. reported: [effect]. mean_difference: [mean_A, mean_B]. "
                             "cohen_d: [mean_A, mean_B, sd_A, sd_B, n_A, n_B]. Binary methods: [events_A, n_A, events_B, n_B]. "
                             "A is the intervention, B the comparator. Values must be from one result. Code calculates the number.")
    rationale: str = Field(description="Why this particular estimate and these inputs are appropriate, including units, direction and any limitations.")
    target_value: float | None = Field(description="Null to use the evidence estimate. Otherwise an explicit proposed planning assumption on the SAME scale and direction, justified separately from the observed estimate. Not a reported number or calculated pooled effect.")

    @model_validator(mode="before")
    @classmethod
    def default_target(cls, data):
        if isinstance(data, dict) and "target_value" not in data:
            return {**data, "target_value": None}
        return data


class EffectScenario(BaseModel):
    label: str
    choice: EffectChoice


class PlanningRecommendation(BaseModel):
    outcome: str = Field(description="Exact outcome name from the specification. One recommendation per outcome, primary first.")
    status: Literal["recommended", "provisional", "insufficient_evidence"]
    estimand: str = Field(description="Population, treatment contrast, outcome/scale, timepoint, summary measure and intercurrent-event strategy; identify unknowns.")
    choice: EffectChoice | None = Field(description="The final selected planning effect, or null if no defensible number is available.")
    justification: str = Field(description="Explain the selection against the alternatives, using R and [V] references.")
    clinical_importance: Literal["supported", "unknown"]
    importance_basis: str = Field(description="Source-supported clinical/stakeholder rationale, distinguishing group differences from individual change thresholds.")
    realism: str = Field(description="How relevant studies support a realistic effect in the planned setting.")
    evidence_quality: str = Field(description="Directness, risk-of-bias information, uncertainty, pilot limitations and overlap. Quote verification is not study quality.")
    assumptions: list[str]
    sensitivity: list[EffectScenario] = Field(description="Alternative evidence-supported effects on the SAME metric and direction; no invented CI or automatic percentage discount.")
    nuisance_parameters: list[str] = Field(description="Needed SDs, control risks, ICCs, correlations or dropout, with [V] references where available; keep separate from the target effect.")
    gaps: list[str]


class EvidenceSummary(BaseModel):
    overview: str = Field(description="Two to four sentences on what was found and how close it is to the planned study.")
    needs: list[NeedSummary] = Field(description="One per effect size to find, in the specification's order.")
    overlap: str = Field(description="Which values come from the same participants, and why; empty if none.")
    caveats: list[str] = Field(description="What to check before using the values.")
    gaps: list[str] = Field(description="Effect sizes the plan needs that the sources don't give, and where to look next.")
    recommendations: list[PlanningRecommendation] = Field(description="Head agent's final planning effects and justifications, primary outcomes first.")

    @model_validator(mode="before")
    @classmethod
    def legacy_recommendations(cls, data):
        if isinstance(data, dict) and "recommendations" not in data:
            return {**data, "recommendations": []}
        return data
