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


class SearchSpec(BaseModel):
    """What the planned study is and what the literature should supply."""

    question: str = Field(description="One-sentence restatement of the planned comparison.")
    population: str
    intervention: str = Field(description="Intervention or exposure.")
    comparator: str
    outcomes: list[str] = Field(description="Outcome measures, primary first; name instruments where given.")
    timepoint: str = Field(description="Primary assessment time; empty if not stated.")
    design: Design
    outcome_type: OutcomeType
    statistics_needed: list[str] = Field(
        description="Statistics to look for in papers, e.g. 'mean and SD of PHQ-9 at 12 weeks per arm', "
        "'control-group event rate', 'hazard ratio with 95% CI', 'ICC'."
    )
    stated_values: list[StatedValue]
    inclusion_criteria: list[str]
    exclusion_criteria: list[str]
    search_terms: list[str] = Field(description="Synonyms, MeSH terms and instrument names useful for searching.")
    open_questions: list[str] = Field(description="Ambiguities the investigator should resolve.")


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
