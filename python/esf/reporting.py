"""Stage-one evidence tables. No pooled effects or numerical transformations are calculated.

Mirrored by site/lib/reporting.js. IDs and values come from extraction, never from a
model's table cells. Incomplete/legacy/failed readings remain visible and fail closed.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

from .verify import Source

VERSION = 1
STATUSES = {
    "direct_synthesis": "DIRECT SYNTHESIS",
    "transform_then_synthesize": "TRANSFORM THEN SYNTHESIZE",
    "assumption_dependent": "ASSUMPTION-DEPENDENT",
    "insufficient_information": "INSUFFICIENT INFORMATION",
    "keep_separate": "KEEP SEPARATE",
}
CRITICAL = (
    "population.intended", "population.eligibility", "population.analysis_set", "population.subgroup", "population.setting",
    "formulation.treatment", "formulation.comparator", "formulation.outcome", "formulation.time_horizon",
    "formulation.intercurrent_events", "target.reported_parameter", "target.conditioning",
    "target.scale", "target.units", "target.direction", "target.interpretation", "target.coefficient_mapping",
)


def facts(profile: dict | None) -> list[dict]:
    return [{"field": f"{section}.{name}", **fact}
            for section, fields in (profile or {}).items() for name, fact in fields.items()]


def fact_value(profile: dict | None, path: str) -> str:
    section, name = path.split(".")
    return ((profile or {}).get(section, {}).get(name) or {}).get("value") or "Not reported"


def profile_checks(context: dict, text: str, count: int, audit: dict | None = None) -> list[dict]:
    source = Source(text)
    rows = []
    for number in range(1, count + 1):
        readings = [r for r in context.get("results", []) if r.get("result") == number]
        reviews = [r for r in (audit or {}).get("results", []) if r.get("result") == number]
        profile = readings[0].get("profile") if len(readings) == 1 else None
        flags = [] if profile else ["Structured population/formulation/parameter reading unavailable"]
        checked = []
        for fact in facts(profile):
            found = source.find_quote(fact["quote"]) if fact.get("quote", "").strip() else None
            invalid = (fact["status"] in ("reported", "reconstructed") and
                       (not fact.get("value", "").strip() or not fact.get("location", "").strip() or found not in ("exact", "approximate")))
            if fact["status"] == "reconstructed" and not fact.get("rationale", "").strip():
                invalid = True
            if fact["status"] == "not_reported" and fact.get("value", "").strip():
                invalid = True
            if invalid:
                flags.append(f"{fact['field']}: unsupported field or missing derivation")
            checked.append({**fact, "quote_check": found, "supported": not invalid and fact["status"] in ("reported", "reconstructed")})
        if len(reviews) != 1:
            flags.append("Independent semantic audit unavailable or incomplete")
        elif reviews[0].get("verdict") != "supported" or reviews[0].get("issues"):
            flags.extend(reviews[0].get("issues") or ["Source information remains insufficient"])
        rows.append({"result": number, "status": "verified" if not flags else "needs_attention", "flags": flags, "facts": checked})
    return rows


def analysis_rows(papers: list[dict], decisions: dict) -> list[dict]:
    # Local import avoids the evidence -> reporting -> evidence cycle.
    from .evidence import _decision
    rows = []
    for paper in papers:
        if paper.get("status") != "extracted" or not paper.get("extraction"):
            continue
        context = paper.get("context") or {}
        readings = context.get("result", {}).get("results", []) if context.get("status") == "done" else []
        for index, result in enumerate(paper["extraction"]["results"]):
            if _decision(decisions, paper["id"], index) == "exclude":
                continue
            reading = next((r for r in readings if r["result"] == index + 1), {})
            check = next((r for r in context.get("profile_checks", []) if r["result"] == index + 1), {})
            rows.append({
                "id": f"R{paper['id']}:{index}", "record": f"R{paper['id']}", "title": paper.get("title") or paper["ext_id"],
                "outcome": result["outcome_measure"], "timepoint": result["timepoint"],
                "population": reading.get("population") or paper["extraction"].get("population") or "Not reported",
                "analysis_set": result["analysis_population"], "adjusted": result["adjusted"],
                "profile": reading.get("profile"), "verification": check.get("status", "needs_attention"),
                "flags": check.get("flags", ["Structured reading and independent audit required"]),
                "facts": check.get("facts", facts(reading.get("profile"))),
                "source_type": paper.get("fulltext_kind"), "decision": _decision(decisions, paper["id"], index),
            })
    return rows


def finalize_tables(plan: dict | None, evidence: dict, overlap: dict | None = None) -> dict:
    analyses = deepcopy(evidence.get("analyses") or [])
    by_id = {a["id"]: a for a in analyses}
    known_values = {v["key"]: v for v in evidence.get("values", [])}
    for analysis in analyses:
        analysis["values"] = [v["key"] for v in known_values.values() if v["result"] == analysis["id"]]
    groups, used, warnings = [], set(), []
    for proposed in (plan or {}).get("groups", []):
        raw_ids = proposed.get("analyses", [])
        ids = list(dict.fromkeys(i for i in raw_ids if i in by_id and i not in used))
        if not ids:
            warnings.append("Empty, unknown or repeated analysis group omitted")
            continue
        invalid_ids = len(ids) != len(raw_ids)
        used.update(ids)
        group = deepcopy(proposed)
        members = [by_id[i] for i in ids]
        flags = []
        if invalid_ids:
            flags.append("Unknown or repeated analysis IDs were removed")
        records = list(dict.fromkeys(a["record"] for a in members))
        if len(records) < 2:
            flags.append("Fewer than two independent study reports; no cross-study synthesis")
        if any(a["verification"] != "verified" for a in members):
            flags.append("Structured extraction or independent semantic audit needs attention")
        values = [v for v in known_values.values() if v["result"] in ids]
        if not values or any(not v.get("quote_found") or v.get("flags") for v in values):
            flags.append("Reported values are missing or have unresolved source/numerical checks")
        if any(sum(a["record"] == r for a in members) > 1 for r in records):
            flags.append("Repeated analyses within a study require covariance or an explicit selection rule")
        for related in (overlap or {}).get("groups", []):
            if len(set(records).intersection(related.get("records", []))) > 1:
                flags.append("Study overlap/dependence unresolved: " + related["relation"])
        status = group.get("status", "insufficient_information")
        if status not in STATUSES:
            status = "insufficient_information"
            flags.append("Unknown combination status")
        if not group.get("reason", "").strip() or not group.get("dependence", "").strip():
            flags.append("Compatibility reasoning or dependence assessment missing")
        if status == "direct_synthesis":
            declared = {"population": "population.intended", "outcome": "formulation.outcome",
                        "timepoint": "formulation.time_horizon", "parameter": "target.reported_parameter"}
            for label, path in declared.items():
                if any(group.get(label, "").strip().lower() != fact_value(a.get("profile"), path).strip().lower() for a in members):
                    flags.append(f"Requested {label} differs from the source definition; a mapping is required")
            for path in CRITICAL:
                vals = [fact_value(a.get("profile"), path).strip().lower() for a in members]
                supporting = [next((f for f in a["facts"] if f["field"] == path), {}) for a in members]
                if len(set(vals)) != 1 or any(not f.get("supported") for f in supporting):
                    flags.append(f"{path}: definitions differ or are unresolved")
            if group.get("inputs_available") != "yes" or group.get("mapping", "").strip().lower() != "identity":
                flags.append("Direct synthesis requires an identity mapping and available inputs")
            if any(re.search(r"\bconditional\b", fact_value(a.get("profile"), "target.conditioning"), re.I) for a in members):
                adjusted = [fact_value(a.get("profile"), "formulation.adjustment").strip().lower() for a in members]
                if len(set(adjusted)) != 1 or "not reported" in adjusted:
                    flags.append("Conditional parameters have differing or unknown adjustment sets")
            for a in members:
                av = [v for v in values if v["result"] == a["id"]]
                metrics = {v["statistic"] for v in av}
                effects = metrics.intersection({"mean_difference", "standardized_mean_difference", "odds_ratio", "risk_ratio", "risk_difference", "hazard_ratio", "coefficient"})
                native = [v for v in av if v["statistic"] in effects]
                usable = False
                if len(native) == 1:
                    same = [v for v in av if v["group"] == native[0]["group"]]
                    ses = [v for v in same if v["statistic"] == "se" and v["value"] > 0]
                    low = [v for v in same if v["statistic"] == "ci_lower"]
                    high = [v for v in same if v["statistic"] == "ci_upper"]
                    usable = len(ses) == 1 or (len(low) == len(high) == 1 and low[0].get("ci_level") is not None
                        and low[0].get("ci_level") == high[0].get("ci_level") and low[0]["value"] < high[0]["value"])
                if not usable:
                    flags.append(f"{a['id']}: native effect and usable uncertainty must be identified")
        if status in ("transform_then_synthesize", "assumption_dependent"):
            if not group.get("mapping", "").strip() or not group.get("methodological_sources") or not group.get("required_inputs"):
                flags.append("Mapping, methodological reference or required inputs missing")
            if status == "transform_then_synthesize" and group.get("inputs_available") != "yes":
                status = "assumption_dependent"
            if status == "assumption_dependent" and not group.get("assumptions"):
                flags.append("Approximation/transport assumptions and sensitivity requirements missing")
        if flags and status != "keep_separate":
            status = "insufficient_information"
        group.update({"id": f"G{len(groups)+1}", "analyses": ids, "records": records, "status": status,
                      "validation_notes": list(dict.fromkeys(flags)), "values": [v["key"] for v in values],
                      "combined_effect": None, "combined_uncertainty": None,
                      "calculation_status": "Not calculated — stage-two blueprint"})
        groups.append(group)
    for a in analyses:
        if a["id"] in used:
            continue
        p = a.get("profile")
        groups.append({"id": f"G{len(groups)+1}", "analyses": [a["id"]], "records": [a["record"]],
                       "population": a["population"], "comparison": fact_value(p, "formulation.treatment") + " vs " + fact_value(p, "formulation.comparator"),
                       "outcome": a["outcome"], "timepoint": a["timepoint"], "parameter": fact_value(p, "target.reported_parameter"),
                       "status": "insufficient_information", "reason": "No verified multi-study grouping provided",
                       "differences": [], "mapping": "", "required_inputs": [], "assumptions": [], "methodological_sources": [],
                       "inputs_available": "unknown", "dependence": "Not assessed", "validation_notes": a["flags"],
                       "values": a["values"], "combined_effect": None, "combined_uncertainty": None,
                       "calculation_status": "Not calculated — stage-two blueprint"})
    # Findings are partitioned by SOURCE definitions, not a planner's free-text population label.
    # Unknown fields never cause two unknown populations/targets to be silently grouped together.
    findings = []
    partitions = {}
    for a in analyses:
        p = a.get("profile")
        vals = [fact_value(p, path).strip().lower() for path in CRITICAL]
        uncertain = any(not next((f for f in a["facts"] if f["field"] == path), {}).get("supported") for path in CRITICAL)
        if re.search(r"\bconditional\b", fact_value(p, "target.conditioning"), re.I):
            vals.append(fact_value(p, "formulation.adjustment").strip().lower())
        signature = json.dumps(vals + ([a["id"]] if uncertain else []))
        if signature not in partitions:
            partitions[signature] = len(findings)
            findings.append({"id": f"P{len(findings)+1}", "population": fact_value(p, "population.intended") if p else a["population"],
                "analysis_set": fact_value(p, "population.analysis_set") if p else a["analysis_set"],
                "subgroup": fact_value(p, "population.subgroup"),
                "eligibility": fact_value(p, "population.eligibility"), "setting": fact_value(p, "population.setting"),
                "conditioning": fact_value(p, "target.conditioning"), "scale": fact_value(p, "target.scale"),
                "units": fact_value(p, "target.units"), "direction": fact_value(p, "target.direction"),
                "comparison": fact_value(p, "formulation.treatment") + " vs " + fact_value(p, "formulation.comparator"),
                "outcome": a["outcome"], "timepoint": a["timepoint"], "parameter": fact_value(p, "target.reported_parameter"),
                "analyses": [], "records": [], "groups": [], "values": []})
        row = findings[partitions[signature]]
        row["analyses"].append(a["id"])
        row["values"].extend(a["values"])
        row["records"] = list(dict.fromkeys(row["records"] + [a["record"]]))
        row["groups"] = list(dict.fromkeys(row["groups"] + [g["id"] for g in groups if a["id"] in g["analyses"]]))
    return {"version": VERSION, "analyses": analyses, "findings": findings, "groups": groups, "warnings": warnings, "statuses": STATUSES}
