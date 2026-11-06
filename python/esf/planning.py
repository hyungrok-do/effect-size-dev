"""Resolve the head agent's choices against verified evidence; never execute model-supplied code.

The model selects inputs and gives scientific reasons. Only these fixed within-study formulas run.
Mirrors site/lib/planning.js. No synthesis across studies or invented precision is performed.
"""
from __future__ import annotations

import copy
import json
import math
import re
from .targets import effect_details, importance_notes, target_notes, validate_domain

EFFECTS = {"mean_difference", "standardized_mean_difference", "risk_difference", "risk_ratio", "odds_ratio", "hazard_ratio"}
RATIOS = {"risk_ratio", "odds_ratio", "hazard_ratio"}
VERSION = 2


def resolve_choice(choice: dict, evidence: dict) -> dict:
    method, keys = choice.get("method"), choice.get("inputs") or []
    counts = {"reported": 1, "mean_difference": 2, "cohen_d": 6, "risk_difference": 4, "risk_ratio": 4, "odds_ratio": 4}
    if method not in counts or len(keys) != counts[method] or len(set(keys)) != len(keys):
        raise ValueError("Wrong method, input count or duplicate input keys.")
    lookup = {v["key"]: v for v in evidence["values"]}
    if any(k not in lookup for k in keys):
        raise ValueError("A selected value is not in the current evidence.")
    vs = [lookup[k] for k in keys]
    if any(not v.get("quote_found") or v.get("flags") or v.get("decision") == "exclude" for v in vs):
        raise ValueError("Selected inputs must have verified quotes and no unresolved verification flags.")
    if any(not isinstance(v["value"], (int, float)) or isinstance(v["value"], bool) or not math.isfinite(v["value"]) for v in vs):
        raise ValueError("Selected inputs must be finite numbers.")
    if len({v.get("result") for v in vs}) != 1 or not vs[0].get("result"):
        raise ValueError("Inputs must come from the same outcome, visit and analysis result in one report.")
    first = vs[0]
    metric = method
    scale = "original outcome units"
    contrast = first["group"]
    formula = "Reported estimate (no numerical transformation)"
    nums = [v["value"] for v in vs]
    if method == "reported":
        metric = first["statistic"]
        if metric not in EFFECTS:
            raise ValueError("A nuisance parameter or CI bound cannot serve as the final treatment effect.")
        value = nums[0]
        if metric in RATIOS and value <= 0:
            raise ValueError("A reported ratio must be positive.")
        scale = first["describes"]
    else:
        if any(v.get("independent_groups") is not True or v.get("adjusted") != "unadjusted" for v in vs):
            raise ValueError("Derived contrasts require unadjusted data from independent parallel groups.")
        record = next((r for r in evidence["records"] if r["key"] == first["record"]), {})
        if re.search(r"cluster|cross.?over|paired|matched", record.get("design", ""), re.I):
            raise ValueError("This design requires a reported design-appropriate contrast.")
        stats = [v["statistic"] for v in vs]
        if method in ("mean_difference", "cohen_d"):
            if first.get("outcome_type") != "continuous" or stats[:2] not in (["mean", "mean"], ["mean_change", "mean_change"]):
                raise ValueError("Use compatible continuous endpoint means or two change means.")
            a, b = vs[0]["group"], vs[1]["group"]
            value = nums[0] - nums[1]
            formula = "mean_A - mean_B"
            if method == "cohen_d":
                sd = "sd_change" if stats[0] == "mean_change" else "sd"
                if stats[2:] != [sd, sd, "n", "n"] or [v["group"] for v in vs[2:]] != [a, b, a, b]:
                    raise ValueError("SDs and sample sizes must match their arms and endpoint/change metric.")
                sa, sb, na, nb = nums[2:]
                if min(sa, sb) <= 0 or min(na, nb) <= 1 or int(na) != na or int(nb) != nb:
                    raise ValueError("Cohen d requires positive SDs and integer arm sizes greater than one.")
                pooled = math.sqrt(((na - 1) * sa**2 + (nb - 1) * sb**2) / (na + nb - 2))
                value /= pooled
                formula = "(mean_A - mean_B) / sqrt(((n_A - 1)*sd_A^2 + (n_B - 1)*sd_B^2)/(n_A + n_B - 2))"
                scale = "pooled within-study SD units (Cohen d)"
            contrast = f"{a} minus {b}"
        else:
            if first.get("outcome_type") != "binary" or stats != ["events", "n", "events", "n"]:
                raise ValueError("Binary contrasts need participants with an event and their analyzed denominators.")
            ea, na, eb, nb = nums
            a, b = vs[0]["group"], vs[2]["group"]
            if [v["group"] for v in vs] != [a, a, b, b] or min(na, nb) <= 0:
                raise ValueError("Binary counts and denominators must match their arms.")
            if any(int(n) != n for n in nums) or not (0 <= ea <= na and 0 <= eb <= nb):
                raise ValueError("Binary counts must be integers between zero and the arm size.")
            pa, pb = ea / na, eb / nb
            if method == "risk_difference":
                value, formula, scale = pa - pb, "events_A/n_A - events_B/n_B", "proportion difference (not percent)"
                contrast = f"{a} minus {b}"
            elif method == "risk_ratio":
                if pb == 0:
                    raise ValueError("Risk ratio is undefined with zero comparator events; no correction is added.")
                value, formula, scale = pa / pb, "(events_A/n_A) / (events_B/n_B)", "risk ratio"
                contrast = f"{a} over {b}"
            else:
                if ea == na or eb == 0:
                    raise ValueError("Odds ratio is undefined for these cells; no correction is added.")
                value = ea * (nb - eb) / ((na - ea) * eb)
                formula, scale = "events_A*(n_B-events_B) / ((n_A-events_A)*events_B)", "odds ratio"
                contrast = f"{a} over {b}"
        if a == b:
            raise ValueError("Two distinct arms are required.")
    if not math.isfinite(value):
        raise ValueError("The derived effect is not finite.")
    details = effect_details(vs, method)
    validate_domain(metric, details["unit"], value)
    basis_value = value
    target = choice.get("target_value")
    if target is not None:
        if isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target) or not choice.get("rationale", "").strip():
            raise ValueError("A proposed target needs a finite value and explicit justification.")
        null = 1 if metric in RATIOS else 0
        if (target - null) * (basis_value - null) <= 0 or (metric in RATIOS and target <= 0):
            raise ValueError("A proposed target must retain the evidence estimate's direction and valid scale.")
        validate_domain(metric, details["unit"], target)
        value = target
    return {"value": value, "basis_value": basis_value,
            "origin": "planning_assumption" if target is not None else "evidence_estimate",
            "metric": metric, "scale": scale, "contrast": contrast, **details,
            "outcome": first["outcome"], "timepoint": first["timepoint"], "record": first["record"],
            "formula": formula, "inputs": keys, "input_values": nums}


def _estimand_notes(key: str | None, evidence: dict) -> list[str]:
    """What the manager's reading of the selected result's estimand leaves open: no reading, an estimand
    close to or different from the planned one, or a model whose quote was not found in the paper."""
    reading = next((e for e in evidence.get("estimands") or [] if e["key"] == key), None)
    if reading is None:
        return ["The manager has not read the selected result's estimand and model."]
    notes = []
    if reading.get("comparison_current") is not True:
        notes.append("The estimand comparison has no current study-plan and extraction basis; read it again before using it.")
    how = "; ".join(reading.get("differences") or [])
    fit = {"different": "different from", "close": "close to, not the same as,"}.get(reading.get("vs_plan"))
    if fit:
        notes.append(f"The manager read the selected result's estimand as {fit} the planned one" + (f": {how}." if how else "."))
    if reading.get("model_quote") == "missing" or (reading.get("model_quote") is None and reading.get("model_kind") != "descriptive"):
        notes.append("The model the manager described for the selected result is not supported by a quote found in the paper.")
    return notes


def _empty(outcome: str) -> dict:
    return {"outcome": outcome, "status": "insufficient_evidence", "estimand": "",
            "choice": None, "resolved": None, "justification": "The head agent did not provide a defensible effect for this outcome.",
            "clinical_importance": "unknown", "importance_basis": "", "realism": "", "evidence_quality": "",
            "assumptions": [], "sensitivity": [], "nuisance_parameters": [],
            "gaps": ["A source-supported planning recommendation is required."], "validation_notes": []}


def finalize_summary(result: dict, evidence: dict, spec: dict) -> dict:
    """Fail closed for invalid numerical recommendations while keeping the descriptive evidence."""
    result = copy.deepcopy(result)
    known = {v["key"]: v for v in evidence["values"]}
    records = {r["key"]: r for r in evidence["records"]}
    context = spec.get("planning") or {}
    recommendations = []
    outcomes = sorted(spec.get("outcomes") or [], key=lambda o: o.get("role") != "primary")
    for outcome in outcomes:
        name = outcome["name"]
        matches = [r for r in result.get("recommendations", []) if r.get("outcome") == name]
        if len(matches) != 1:
            recommendations.append(_empty(name))
            continue
        rec = matches[0]
        rec["resolved"], rec["validation_notes"] = None, []
        try:
            if rec["status"] == "insufficient_evidence":
                rec["choice"], rec["sensitivity"] = None, []
                recommendations.append(rec)
                continue
            if not rec.get("choice") or not all(rec.get(k, "").strip() for k in ("estimand", "justification", "realism", "evidence_quality", "importance_basis")):
                raise ValueError("A numerical recommendation needs an estimand and full justification.")
            narrative = json.dumps(rec, ensure_ascii=False)
            if any(k not in known for k in re.findall(r"\[(V\d+)\]", narrative)) or any(k not in records for k in re.findall(r"\bR\d+\b", narrative)):
                raise ValueError("The recommendation cites a value or study outside the current evidence.")
            resolved = resolve_choice(rec["choice"], evidence)
            if resolved["value"] == (1 if resolved["metric"] in RATIOS else 0):
                raise ValueError("The null effect cannot be the final detectable target difference.")
            if re.search(r"non[\s-]*inferior|equivalence", context.get("objective", ""), re.I):
                raise ValueError("A noninferiority/equivalence margin needs separate clinical justification; it is not a selected observed effect.")
            rec["resolved"] = resolved
            vs = [known[k] for k in resolved["inputs"]]
            source = records[resolved["record"]]
            notes = rec["validation_notes"]
            notes += target_notes(resolved, outcome)
            importance = importance_notes(resolved, outcome)
            if rec["clinical_importance"] != "supported" or importance:
                rec["clinical_importance"] = "unknown"
                notes += importance or ["Clinical importance has not been established from the supplied evidence."]
            if any(not context.get(k, "").strip() for k in ("objective", "effect_measure", "analysis", "analysis_population", "intercurrent_events")):
                notes.append("The planned estimand or analysis still has unspecified components.")
            if any(v.get("decision") != "include" for v in vs):
                notes.append("Some selected results have not yet been included by the investigator.")
            if any(v.get("relevance") != "direct" or v.get("confidence") != "high" or v.get("concerns") for v in vs):
                notes.append("Selected evidence has indirectness or extraction concerns.")
            domains = {a["domain"] for a in source.get("appraisal") or [] if a.get("quote_found")}
            if source.get("fulltext_kind") in (None, "abstract") or not {"randomization", "deviations", "missing_outcomes", "measurement", "selective_reporting"} <= domains:
                notes.append("Full methodological appraisal is unavailable for the selected report.")
            normalize = lambda s: " ".join(s.lower().split())
            if normalize(resolved["outcome"]) not in {normalize(name), normalize(outcome.get("measure", ""))} or normalize(resolved["timepoint"]) != normalize(outcome.get("timepoint", "")):
                notes.append("The source outcome label or timepoint differs from the planned outcome; confirm equivalence.")
            if source.get("study_type") != "rct" or not source.get("matches_spec"):
                notes.append("The selected report is not a confirmed matching randomized trial; assess applicability.")
            notes += _estimand_notes(vs[0].get("estimand"), evidence)
            scenarios = []
            for scenario in rec.get("sensitivity") or []:
                try:
                    alt = resolve_choice(scenario["choice"], evidence)
                    if any(alt[k] != resolved[k] for k in ("metric", "scale", "contrast", "outcome", "timepoint", "unit", "direction", "standardizer")):
                        raise ValueError("Sensitivity scenarios must match the effect metric, scale, contrast, outcome and timepoint.")
                    null = 1 if resolved["metric"] in RATIOS else 0
                    if (alt["value"] - null) * (resolved["value"] - null) <= 0:
                        raise ValueError("Sensitivity scenarios must retain the main effect's non-null direction.")
                    scenario_notes = target_notes(alt, outcome) + _estimand_notes(known[alt["inputs"][0]].get("estimand"), evidence)
                    scenarios.append({**scenario, "resolved": alt, "validation_notes": scenario_notes})
                except ValueError as exc:
                    notes.append(f"Sensitivity scenario omitted: {exc}")
            rec["sensitivity"] = scenarios
            if notes:
                rec["status"] = "provisional"
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError) as exc:
            rec["status"], rec["choice"], rec["resolved"], rec["sensitivity"] = "insufficient_evidence", None, None, []
            rec["validation_notes"].append(str(exc))
        recommendations.append(rec)
    result["recommendations"] = recommendations
    result["planning_version"] = VERSION
    return result
