"""What the overlap check and the summary are given.

overlap_records: the extracted records, with what tells reports of one study apart (registration
numbers, authors, arms and their sizes) and which reviews in the set cite them.

build_evidence: what the summary describes: the records the investigator kept something from, and
every reported value they have not excluded (V1, V2, ...), each marked with the record whose
participants it shares when the overlap check says to use that record's numbers instead. Nothing is
combined across studies. Claude refers to values by key, so every number in a summary comes from
here, never from the model.

Mirrors site/lib/evidence.js; the tests require identical output.
"""

from __future__ import annotations

from typing import Any

PAPER = -1  # result_index of a decision about the whole paper


def record_key(paper: dict) -> str:
    return f"R{paper['id']}"


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
    use_instead: dict[str, str] = {}  # record -> the record of the same participants to use instead
    for group in (overlap or {}).get("groups") or []:
        if group.get("use"):
            for r in group["records"]:
                if r != group["use"]:
                    use_instead.setdefault(r, group["use"])

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
                    "same_participants_as": use_instead.get(record_key(p)),
                })
    kept = {v["record"] for v in values}
    return {"records": [r for r in overlap_records(papers) if r["key"] in kept], "values": values}


def decisions_basis(papers: list[dict], decisions: dict) -> list:
    """What a summary was based on, to tell when it is out of date."""
    return [[p["id"], p["status"], _decision_list(decisions, p["id"])] for p in _extracted(papers)]


def _decision_list(decisions: dict, paper_id: Any) -> list:
    mine = decisions.get(paper_id) or decisions.get(str(paper_id)) or {}
    return sorted([int(k), d.get("decision")] for k, d in mine.items())
