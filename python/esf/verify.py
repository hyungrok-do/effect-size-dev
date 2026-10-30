"""Deterministic checks on an extraction: is each quote in the source, is each number in its quote,
and are the numbers plausible together. Anything that fails is escalated to the manager."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from .schemas import OutcomeResult, PaperExtraction, StudyContext

_DASHES = dict.fromkeys(map(ord, "−‒–—‐‑﹣－"), "-")
_NUMBER = re.compile(r"(?<![\d.])-?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)(?!\d)")
RATIO_MEASURES = {"odds_ratio", "risk_ratio", "hazard_ratio"}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_DASHES)
    text = re.sub(r"(?<=\d)[·‧∙](?=\d)", ".", text)  # Lancet-style decimal point
    return " ".join(text.lower().split())


def squash(text: str) -> str:
    """Normalize and keep only letters, digits, '.', '-' and '%', so layout differences don't matter."""
    return re.sub(r"[^a-z0-9.%-]", "", normalize(text))


def numbers_in(text: str) -> list[float]:
    return [float(m.group(0).replace(",", "")) for m in _NUMBER.finditer(normalize(text))]


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def number_in(value: float, text: str) -> bool:
    """Is the value printed in the text, including its sign? Never infer a sign from prose."""
    return any(_same(n, value) for n in numbers_in(text))


class Source:
    """A source text prepared once for repeated lookups."""

    def __init__(self, text: str):
        self.text = text
        self.squashed = squash(text)
        words = normalize(text).split()
        self.trigrams = {tuple(words[i : i + 3]) for i in range(len(words) - 2)}
        self.numbers = numbers_in(text)

    def find_quote(self, quote: str) -> str:
        """'exact', 'approximate' (most of the quote's word triples occur in the source) or 'missing'."""
        target = squash(quote)
        if target and target in self.squashed:
            return "exact"
        words = normalize(quote).split()
        grams = [tuple(words[i : i + 3]) for i in range(len(words) - 2)]
        if grams and sum(g in self.trigrams for g in grams) / len(grams) >= 0.8:
            return "approximate"
        return "missing"

    def has_number(self, value: float) -> bool:
        return any(_same(n, value) for n in self.numbers)


@dataclass
class ValueCheck:
    quote: str  # exact | approximate | missing
    number_in_quote: bool
    number_in_source: bool

    def ok(self, quotes_reliable: bool) -> bool:
        if not self.number_in_quote or not self.number_in_source:
            return False
        if self.quote != "missing":
            return True
        # A PDF's text layer often scrambles tables; then accept a number that occurs in the text.
        return not quotes_reliable and self.number_in_source


@dataclass
class Verification:
    values: list[list[ValueCheck]] = field(default_factory=list)  # per result, per value
    flags: list[str] = field(default_factory=list)
    appraisal: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"values": [[asdict(v) for v in result] for result in self.values], "flags": self.flags,
                "appraisal": self.appraisal}


def plausibility(result: OutcomeResult) -> list[str]:
    flags: list[str] = []
    by_group: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for v in result.values:
        by_group[v.group][v.statistic].append(v.value)
        if v.statistic == "p_value" and not 0 <= v.value <= 1:
            flags.append(f"p-value {v.value:g} outside [0, 1]")
        if v.statistic in RATIO_MEASURES and v.value <= 0:
            flags.append(f"{v.statistic.replace('_', ' ')} {v.value:g} is not positive")
        if v.statistic in ("sd", "se", "sd_change") and v.value < 0:
            flags.append(f"negative {v.statistic.upper()} ({v.value:g}) for {v.group}")
        if v.statistic == "percent" and not 0 <= v.value <= 100:
            flags.append(f"percent {v.value:g} outside [0, 100] for {v.group}")

    for group, stats in by_group.items():
        lows, highs = stats.get("ci_lower", []), stats.get("ci_upper", [])
        if len(lows) == 1 and len(highs) == 1 and lows[0] > highs[0]:
            flags.append(f"CI lower bound above upper bound for {group}")
        ns, events = stats.get("n", []), stats.get("events", [])
        if len(ns) == 1 and len(events) == 1 and events[0] > ns[0]:
            flags.append(f"events exceed n for {group}")
        sds, ses = stats.get("sd", []), stats.get("se", [])
        if len(ns) == 1 and len(sds) == 1 and len(ses) == 1 and ns[0] > 1:
            implied = sds[0] / math.sqrt(ns[0])
            if not math.isclose(ses[0], implied, rel_tol=0.15):
                flags.append(f"SD and SE for {group} disagree given n = {ns[0]:g}")

    sd_values = [stats["sd"][0] for stats in by_group.values() if len(stats.get("sd", [])) == 1 and stats["sd"][0] > 0]
    if len(sd_values) >= 2 and max(sd_values) / min(sd_values) > 3:
        flags.append("SDs differ more than threefold between groups; one may be an SE")

    has_means = any("mean" in stats or "mean_change" in stats for stats in by_group.values())
    has_n = any("n" in stats for stats in by_group.values())
    if has_means and not has_n:
        flags.append("means reported without group sizes")
    return flags


def verify(extraction: PaperExtraction, source_text: str, quotes_reliable: bool = True) -> Verification:
    source = Source(source_text)
    report = Verification()
    for index, item in enumerate(extraction.appraisal):
        found = source.find_quote(item.quote)
        report.appraisal.append(found)
        if found == "missing":
            report.flags.append(f"appraisal {index + 1}: supporting quote not found in the source")
    for index, result in enumerate(extraction.results):
        checks = []
        for v in result.values:
            check = ValueCheck(
                quote=source.find_quote(v.quote),
                number_in_quote=number_in(v.value, v.quote),
                number_in_source=source.has_number(v.value),
            )
            checks.append(check)
            if not check.ok(quotes_reliable):
                if not check.number_in_quote:
                    problem = "is not in its quote"
                elif check.number_in_source:
                    problem = "has a quote not found in the source"
                else:
                    problem = "was not found in the source"
                report.flags.append(
                    f"result {index + 1} ({result.outcome_measure}): {v.statistic} {v.value:g} for {v.group} {problem}"
                )
        report.values.append(checks)
        report.flags += [f"result {index + 1} ({result.outcome_measure}): {f}" for f in plausibility(result)]
    return report


def check_context(context: StudyContext, source_text: str) -> dict:
    """Whether the quotes in the manager's reading of a paper's context are in the paper: 'exact',
    'approximate' or 'missing' for the sample-size calculation and for each result's model, in the
    order of context.results; None where there is no quote."""
    source = Source(source_text)

    def found(quote: str) -> str | None:
        return source.find_quote(quote) if quote.strip() else None

    return {"sample_size": found(context.sample_size), "results": [found(r.quote) for r in context.results]}


def escalation_reasons(extraction: PaperExtraction | None, report: Verification | None) -> list[str]:
    """Why a worker extraction should go to the manager; empty if it can stand."""
    if extraction is None or report is None:
        return ["the worker could not complete the extraction"]
    reasons = list(report.flags)
    for index, result in enumerate(extraction.results):
        if result.confidence == "low":
            reasons.append(f"result {index + 1}: the worker reported low confidence")
        reasons += [f"result {index + 1}: worker concern: {c}" for c in result.concerns]
    return reasons


def in_audit_sample(key: str, fraction: float) -> bool:
    """Deterministic: the same paper is always in or out of the audit sample."""
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32 < fraction
