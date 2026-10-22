"""What the overlap check and the conclusion are given.

overlap_records: the extracted records, with what tells reports of one study apart (registration
numbers, authors, arms and their sizes) and which reviews in the set cite them.

build_evidence: the candidate values for the conclusion. Every reported value the investigator has
not excluded (V1, V2, ...), and summaries this code computes across studies (S1, S2, ...): a pooled
SD where every arm reports its size, and otherwise the median and range. Claude recommends
candidates by key; every number in a conclusion comes from here, never from the model.

Mirrors site/lib/evidence.js; the tests require identical output.
"""

from __future__ import annotations

import math
import re
from typing import Any

PAPER = -1  # result_index of a decision about the whole paper
# Statistics that summarize across studies; the rest (n, bounds, test statistics) are only listed.
SUMMARIZED = {
    "mean", "sd", "se", "median", "events", "percent", "mean_change", "sd_change", "mean_difference",
    "standardized_mean_difference", "odds_ratio", "risk_ratio", "hazard_ratio", "risk_difference", "icc", "correlation",
}
POOLED = {"sd", "sd_change"}  # pooled across arms and studies, weighted by n - 1


def record_key(paper: dict) -> str:
    return f"R{paper['id']}"


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _extracted(papers: list[dict]) -> list[dict]:
    return [p for p in papers if p["status"] == "extracted" and p.get("extraction")]


def overlap_records(papers: list[dict]) -> list[dict]:
    extracted = _extracted(papers)
    by_pmid = {p["pmid"]: record_key(p) for p in extracted if p.get("pmid")}
    out = []
    for p in extracted:
        x = p["extraction"]
        outcomes = {f"{r['outcome_measure']} at {r['timepoint']}" if r["timepoint"] else r["outcome_measure"] for r in x["results"]}
        out.append({
            "key": record_key(p),
            "source": p["source"],
            "pmid": p.get("pmid"),
            "nct_ids": p.get("nct_ids") or [],
            "doi": p.get("doi"),
            "title": p.get("title") or "",
            "authors": p.get("authors") or "",
            "year": p.get("year") or "",
            "journal": p.get("journal") or "",
            "publication_types": p.get("pubtypes") or [],
            "design": x["study_design"],
            "population": x["population"],
            "groups": x["groups"],
            "outcomes": sorted(outcomes),
            "cited_by": [by_pmid[pmid] for pmid in p.get("cited_by") or [] if pmid in by_pmid],
        })
    return out


def _decision(decisions: dict, paper_id: Any, index: int) -> str | None:
    """A result's decision, else its paper's. Keys may be numbers or, from JSON, strings."""
    mine = decisions.get(paper_id) or decisions.get(str(paper_id)) or {}
    own = mine.get(index) or mine.get(str(index))
    whole = mine.get(PAPER) or mine.get(str(PAPER))
    chosen = own or whole
    return chosen.get("decision") if chosen else None


def build_evidence(papers: list[dict], decisions: dict, overlap: dict | None) -> dict:
    dropped: set[str] = set()  # records the overlap check says not to use
    for group in (overlap or {}).get("groups") or []:
        if group.get("use"):
            dropped |= {r for r in group["records"] if r != group["use"]}

    values: list[dict] = []
    for p in _extracted(papers):
        checks = (p.get("verification") or {}).get("values") or []
        for i, r in enumerate(p["extraction"]["results"]):
            if _decision(decisions, p["id"], i) == "exclude":
                continue
            row = checks[i] if i < len(checks) else []
            sizes = {v["group"]: v["value"] for v in r["values"] if v["statistic"] == "n"}
            for j, v in enumerate(r["values"]):
                check = row[j] if j < len(row) else None
                values.append({
                    "key": f"V{len(values) + 1}",
                    "record": record_key(p),
                    "outcome": r["outcome_measure"],
                    "timepoint": r["timepoint"],
                    "group": v["group"],
                    "statistic": v["statistic"],
                    "value": v["value"],
                    "describes": v["describes"],
                    "n": None if v["statistic"] == "n" else sizes.get(v["group"]),
                    "relevance": r["relevance"],
                    "confidence": r["confidence"],
                    "quote_found": bool(check and check["quote"] != "missing" and check["number_in_quote"]),
                    "dropped_for_overlap": record_key(p) in dropped,
                })

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for v in values:
        if v["dropped_for_overlap"] or v["statistic"] not in SUMMARIZED:
            continue
        groups.setdefault((_norm(v["outcome"]), _norm(v["timepoint"]), v["statistic"]), []).append(v)

    summaries: list[dict] = []
    for (_, _, statistic), group in groups.items():
        if len(group) < 2:
            continue
        numbers = sorted(v["value"] for v in group)
        middle = len(numbers) // 2
        base = {
            "outcome": group[0]["outcome"],
            "timepoint": group[0]["timepoint"],
            "statistic": statistic,
            "records": list(dict.fromkeys(v["record"] for v in group)),
            "values": [v["key"] for v in group],
            "count": len(group),
            "min": numbers[0],
            "max": numbers[-1],
        }
        if statistic in POOLED and all(v["n"] is not None and v["n"] > 1 for v in group):
            # Plain loops, not sum(): Python's sum() of floats is compensated, JavaScript's isn't.
            squares = 0.0
            freedom = 0.0
            total = 0.0
            for v in group:
                squares += (v["n"] - 1) * (v["value"] * v["value"])
                freedom += v["n"] - 1
                total += v["n"]
            summaries.append({"key": f"S{len(summaries) + 1}", "method": "pooled_sd", "value": math.sqrt(squares / freedom),
                              "n": total, **base})
        median = numbers[middle] if len(numbers) % 2 else (numbers[middle - 1] + numbers[middle]) / 2
        summaries.append({"key": f"S{len(summaries) + 1}", "method": "median", "value": median, "n": None, **base})
    return {"values": values, "summaries": summaries}


def decisions_basis(papers: list[dict], decisions: dict) -> list:
    """What a conclusion was based on, to tell when it is out of date."""
    return [[p["id"], p["status"], _decision_list(decisions, p["id"])] for p in _extracted(papers)]


def _decision_list(decisions: dict, paper_id: Any) -> list:
    mine = decisions.get(paper_id) or decisions.get(str(paper_id)) or {}
    return sorted([int(k), d.get("decision")] for k, d in mine.items())
