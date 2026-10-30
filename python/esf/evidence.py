"""What the overlap check and the summary are given.

overlap_records: the extracted records, with what tells reports of one study apart (registration
numbers, authors, arms and their sizes) and which reviews in the set cite them.

build_evidence: what the summary describes: the records the investigator kept something from, and
every reported value they have not excluded (V1, V2, ...). A value from a report of a study that is
reported more than once (a same_study group) is marked with the record whose numbers to use instead;
reviews and the studies they pool are separate studies and are not marked. Nothing is combined across
studies. Claude refers to values by key, so every number in a summary comes from
here, never from the model.

Where the manager has read a paper's statistical context, its kept results carry their estimands
(E1, E2, ...), with whether the quote of each one's model was found in the paper, each value names
its result's, and its record carries the design and the paper's own sample-size calculation as the
manager stated them.

Mirrors site/lib/evidence.js; the tests require identical output.
"""

from __future__ import annotations

from typing import Any
from copy import deepcopy

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


def _context(paper: dict) -> dict | None:
    """The manager's reading of a paper's statistical context (a StudyContext), when it has one."""
    context = paper.get("context") or {}
    return context.get("result") if context.get("status") == "done" else None


def _readings(paper: dict) -> dict[int, tuple[dict, str | None]]:
    """The manager's reading of each result, by its number (from 1), with the check of its model's quote;
    the first reading of a number when there are more, as the review and the exports take it."""
    found = ((paper.get("context") or {}).get("checks") or {}).get("results") or []
    read: dict[int, tuple[dict, str | None]] = {}
    for k, c in enumerate((_context(paper) or {}).get("results") or []):
        read.setdefault(c["result"], (c, found[k] if k < len(found) else None))
    return read


# What the summary is told about a result's estimand, from the manager's reading (ResultContext).
ESTIMAND_FIELDS = (
    "population", "treatments", "variable", "intercurrent_events", "summary_measure", "model", "missing_data",
    "numbers_are", "vs_plan", "differences", "use",
)


def build_evidence(papers: list[dict], decisions: dict, overlap: dict | None) -> dict:
    use_instead: dict[str, str] = {}  # record -> the record of the same participants to use instead
    for group in (overlap or {}).get("groups") or []:
        if group.get("use") and group.get("relation") == "same_study":
            for r in group["records"]:
                if r != group["use"]:
                    use_instead.setdefault(r, group["use"])

    values: list[dict] = []
    estimands: list[dict] = []
    for p in _extracted(papers):
        checks = (p.get("verification") or {}).get("values") or []
        read = _readings(p)
        for i, r in enumerate(p["extraction"]["results"]):
            if _decision(decisions, p["id"], i) == "exclude":
                continue
            row = checks[i] if i < len(checks) else []
            sizes = {v["group"]: v["value"] for v in r["values"] if v["statistic"] == "n"}
            estimand = None
            if i + 1 in read:
                reading, model_quote = read[i + 1]
                estimand = f"E{len(estimands) + 1}"
                estimands.append({
                    "key": estimand, "record": record_key(p), "result": f"{record_key(p)}:{i}",
                    "outcome": r["outcome_measure"], "timepoint": r["timepoint"],
                    **{name: reading.get(name) for name in ESTIMAND_FIELDS},
                    "model_quote": model_quote,  # 'exact', 'approximate', 'missing', or None with no quote
                })
            for j, v in enumerate(r["values"]):
                check = row[j] if j < len(row) else None
                values.append({
                    "key": f"V{len(values) + 1}",
                    "record": record_key(p),
                    "result": f"{record_key(p)}:{i}",
                    "outcome": r["outcome_measure"],
                    "timepoint": r["timepoint"],
                    "group": v["group"],
                    "statistic": v["statistic"],
                    "value": v["value"],
                    "ci_level": v.get("ci_level"),
                    "describes": v["describes"],
                    "n": None if v["statistic"] == "n" else sizes.get(v["group"]),
                    "relevance": r["relevance"],
                    "confidence": r["confidence"],
                    "decision": _decision(decisions, p["id"], i),
                    "analysis_population": r["analysis_population"],
                    "adjusted": r["adjusted"],
                    "outcome_type": r.get("outcome_type", "unclear"),
                    "independent_groups": r.get("independent_groups"),
                    "concerns": r.get("concerns") or [],
                    "flags": (p.get("verification") or {}).get("flags") or [],
                    "quote": v["quote"],
                    "location": v["location"],
                    "quote_found": bool(check and check["quote"] != "missing" and check["number_in_quote"]
                                        and check["number_in_source"]),
                    "same_participants_as": use_instead.get(record_key(p)),
                    "estimand": estimand,
                })
    kept = {v["record"] for v in values}
    # Importance/anchor studies can contribute methodological evidence without a usable effect.
    for p in _extracted(papers):
        if p["extraction"].get("appraisal") and (
            any(_decision(decisions, p["id"], i) != "exclude" for i in range(len(p["extraction"]["results"])))
            or (not p["extraction"]["results"] and _decision(decisions, p["id"], PAPER) != "exclude")
        ):
            kept.add(record_key(p))
    records = [r for r in overlap_records(papers) if r["key"] in kept]
    by_key = {record_key(p): p for p in papers}
    for record in records:
        paper = by_key[record["key"]]
        extraction = paper["extraction"]
        checks = (paper.get("verification") or {}).get("appraisal") or []
        record.update({
            "fulltext_kind": paper.get("fulltext_kind"),
            "study_type": (paper.get("screen") or {}).get("study_type"),
            "matches_spec": extraction.get("matches_spec", False),
            "mismatch_reasons": extraction.get("mismatch_reasons") or [],
            "notes": extraction.get("notes") or "",
            "appraisal": [{**a, "quote_found": i < len(checks) and checks[i] in ("exact", "approximate")}
                          for i, a in enumerate(extraction.get("appraisal") or [])],
            "context": _record_context(paper),
        })
    return {"records": records, "estimands": [e for e in estimands if e["record"] in kept], "values": values}


def _record_context(paper: dict) -> dict | None:
    """The manager's statement of a record's design and its own sample-size calculation, if it read them."""
    context = _context(paper)
    if context is None:
        return None
    found = ((paper.get("context") or {}).get("checks") or {}).get("sample_size")
    return {"design": context.get("design") or "", "sample_size": context.get("sample_size") or "",
            "sample_size_found": found in ("exact", "approximate"), "notes": context.get("notes") or ""}


def decisions_basis(papers: list[dict], decisions: dict) -> list:
    """What a summary was based on, to tell when it is out of date: each extracted record, its
    decisions, and the values it reports (a re-extraction changes them), and the manager's reading of
    its statistical context once there is one."""
    rows = []
    for p in _extracted(papers):
        row = [p["id"], p["status"], _decision_list(decisions, p["id"]), _values_of(p), p["extraction"],
               p.get("verification"), p.get("fulltext_kind"), p.get("screen")]
        if _context(p) is not None:  # a summary written before then goes out of date when it is read
            row.append(_context(p))
        rows.append(row)
    return deepcopy(rows)


def _values_of(paper: dict) -> list:
    return [[r["outcome_measure"], r["timepoint"], [[v["group"], v["statistic"], v["value"], v.get("ci_level")] for v in r["values"]]]
            for r in paper["extraction"]["results"]]


def _decision_list(decisions: dict, paper_id: Any) -> list:
    mine = decisions.get(paper_id) or decisions.get(str(paper_id)) or {}
    return sorted([int(k), d.get("decision")] for k, d in mine.items())
