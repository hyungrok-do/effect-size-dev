"""Structured outputs exchanged with Claude.

Every field is required (nullable where a value can be absent) so the schemas stay within
structured-output limits, and every reported number carries the verbatim text it came from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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


def upgrade_spec(spec: dict) -> dict:
    """A specification saved before the structured fields, in the current shape. Mirrors upgradeSpec
    in site/lib/spec-panel.js."""
    if isinstance(spec.get("population"), dict):
        return spec
    timepoint = spec.get("timepoint") or ""
    outcomes = [
        {"name": name, "measure": "", "role": "secondary" if i else "primary",
         "type": spec.get("outcome_type") or "unclear", "timepoint": timepoint}
        for i, name in enumerate(spec.get("outcomes") or [])
    ]
    first = outcomes[0]["name"] if outcomes else ""
    return {
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
    describes: str = Field(
        description="What the number is, e.g. 'PHQ-9 at 12 weeks', '95% CI of the mean difference'."
    )
    quote: str = Field(description="Verbatim text from the document that contains this number.")
    location: str = Field(description="Where it is: 'Table 2', 'Abstract', 'Results, paragraph 3'.")


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


class PaperExtraction(BaseModel):
    study_design: str
    population: str
    groups: list[str] = Field(description="Each arm with its randomized or analyzed size, as reported.")
    matches_spec: bool
    mismatch_reasons: list[str]
    results: list[OutcomeResult]
    notes: str


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


class EvidenceSummary(BaseModel):
    overview: str = Field(description="Two to four sentences on what was found and how close it is to the planned study.")
    needs: list[NeedSummary] = Field(description="One per effect size to find, in the specification's order.")
    overlap: str = Field(description="Which values come from the same participants, and why; empty if none.")
    caveats: list[str] = Field(description="What to check before using the values.")
    gaps: list[str] = Field(description="Effect sizes the plan needs that the sources don't give, and where to look next.")
